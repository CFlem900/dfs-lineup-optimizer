"""Pre-Lock Polling Service — high-frequency data pipeline before slate lock.

Runs as a background asyncio task started from the FastAPI lifespan.
When within PRE_LOCK_WINDOW_MINUTES of any DraftKings slate lock, switches
from dormant mode (10-min checks) to active mode (2-min polls) and
forces fresh injury sync + news refresh each cycle.

Architecture:

    ┌─────────────────────────┐
    │ PreLockPollingService   │  asyncio.Task (background)
    │ (every 2 min pre-lock)  │
    └──────┬──────────┬───────┘
           │          │
    ┌──────▼─────────┐  ┌▼───────────────────┐
    │ InjuryService  │  │ NewsService         │
    │.sync_injuries()│  │ (force _refresh())  │
    └──────┬─────────┘  └──────┬──────────────┘
           │                │
    Already handles:   News hash change →
    - SHA-256 hash      publish to Redis
    - Redis bust        Pub/Sub channel
    - Optimizer clear   → PrewarmSubscriber
    - Pub/Sub event       rebuilds pool

Concurrency:
    The task runs on the main asyncio event loop. Both run_sync() and
    get_news() are synchronous, so they are called via asyncio.to_thread().
    Only one worker runs the poller (guarded by SCHEDULER_ENABLED env var).
"""

import asyncio
import hashlib
import json
import logging
import time
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

from app.config.constants import (
    ACTIVE_POLL_INTERVAL_S,
    DORMANT_POLL_INTERVAL_S,
    PRE_LOCK_WINDOW_MINUTES,
    SLATE_SCHEDULE_REFRESH_MINUTES,
    LATE_SWAP_AUTO_WINDOW_MINUTES,
)

logger = logging.getLogger(__name__)


class PreLockPollingService:
    """Background polling service that activates before DK slate locks.

    Lifecycle:
        1. Started as an asyncio task from main.py lifespan
        2. Fetches today's slate lock times from DKSlateService
        3. Sleeps in dormant mode until within PRE_LOCK_WINDOW_MINUTES
           of any slate lock
        4. Switches to active mode and polls injury + news every cycle
        5. After all slates lock, returns to dormant mode
        6. Refreshes slate schedule hourly or when empty
    """

    def __init__(
        self,
        injury_service=None,
        news_service=None,
        cache_service=None,
        lineup_optimizer_service=None,
        notification_service=None,
        discord_news_service=None,
        # Legacy alias — ignore if injury_service is provided
        injury_sync_service=None,
    ):
        self._injury_sync = injury_service or injury_sync_service
        self._news = news_service
        self._cache = cache_service  # Injected in main.py after cache connects
        self._lineup_optimizer = lineup_optimizer_service
        self._notification = notification_service
        self._discord_out = discord_news_service  # For outbound alerts

        # Task state
        self._running = False
        self._task: Optional[asyncio.Task] = None

        # Slate schedule: [{slate_name, lock_time_utc, draft_group_id}]
        self._slate_schedule: List[Dict] = []
        self._schedule_fetched_at: Optional[datetime] = None

        # News change detection
        self._last_news_hash: str = ""

        # Autonomous late-swap state
        self._saved_lineups: Optional[List] = None  # Set via set_lineups()
        self._saved_pool: Optional[List] = None
        self._already_swapped: set = set()  # Player IDs already auto-swapped

        # Observability
        self._last_poll_at: Optional[str] = None
        self._poll_count: int = 0
        self._active_window: bool = False
        self._last_injury_result: Optional[Dict] = None
        self._last_news_changed: bool = False
        self._last_auto_swap: Optional[Dict] = None
        self._errors: List[str] = []  # Last 10 errors

    # ------------------------------------------------------------------
    # Start / Stop
    # ------------------------------------------------------------------

    def start(self):
        """Start the background polling task on the current event loop."""
        self._running = True
        loop = asyncio.get_running_loop()
        self._task = loop.create_task(self._run_loop())
        logger.info("[PreLock] Polling service started")

    def stop(self):
        """Cancel the polling task."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
        logger.info("[PreLock] Polling service stopped")

    # ------------------------------------------------------------------
    # Core loop
    # ------------------------------------------------------------------

    async def _run_loop(self):
        """Main polling loop. Runs until cancelled."""
        # Brief startup delay so the server is fully initialised
        await asyncio.sleep(5)

        while self._running:
            try:
                # Refresh slate schedule if stale or empty
                if self._should_refresh_schedule():
                    await self._refresh_slate_schedule()

                # Determine mode based on proximity to lock
                now_utc = datetime.now(timezone.utc)
                minutes_to_lock = self._minutes_to_nearest_lock(now_utc)

                if (
                    minutes_to_lock is not None
                    and minutes_to_lock <= PRE_LOCK_WINDOW_MINUTES
                ):
                    # ACTIVE MODE — within pre-lock window
                    if not self._active_window:
                        logger.info(
                            "[PreLock] Entering active window — %.1f min to lock",
                            minutes_to_lock,
                        )
                    self._active_window = True
                    await self._poll_cycle()
                    interval = ACTIVE_POLL_INTERVAL_S
                else:
                    # DORMANT MODE — no imminent locks
                    if self._active_window:
                        logger.info("[PreLock] Exiting active window — all slates locked or distant")
                    self._active_window = False
                    interval = DORMANT_POLL_INTERVAL_S

                await asyncio.sleep(interval)

            except asyncio.CancelledError:
                logger.info("[PreLock] Polling task cancelled")
                break
            except Exception as exc:
                self._errors.append(
                    f"{datetime.now(timezone.utc).isoformat()}: {exc}"
                )
                self._errors = self._errors[-10:]
                logger.error("[PreLock] Poll loop error: %s", exc, exc_info=True)
                await asyncio.sleep(DORMANT_POLL_INTERVAL_S)

    # ------------------------------------------------------------------
    # Poll cycle
    # ------------------------------------------------------------------

    async def _poll_cycle(self):
        """Execute one full poll: injury sync + news refresh."""
        self._poll_count += 1
        self._last_poll_at = datetime.now(timezone.utc).isoformat()
        t0 = time.time()

        # 1. Injury sync — sync_injuries() is synchronous (psycopg2 + sync redis)
        try:
            result = await asyncio.to_thread(self._injury_sync.sync_injuries)
            self._last_injury_result = result
            if result.get("hash_changed"):
                logger.warning(
                    "[PreLock] Injury data CHANGED — %d caches cleared, "
                    "%d prewarm published, %d star changes",
                    result.get("optimizer_caches_cleared", 0),
                    result.get("prewarm_published", 0),
                    len(result.get("star_changes", [])),
                )
            else:
                logger.debug("[PreLock] Injury sync complete — no changes")
        except Exception as exc:
            logger.error("[PreLock] Injury sync failed: %s", exc)
            self._errors.append(
                f"{datetime.now(timezone.utc).isoformat()}: injury_sync: {exc}"
            )
            self._errors = self._errors[-10:]

        # 2. News refresh with change detection
        try:
            await self._poll_news()
        except Exception as exc:
            logger.error("[PreLock] News refresh failed: %s", exc)
            self._errors.append(
                f"{datetime.now(timezone.utc).isoformat()}: news: {exc}"
            )
            self._errors = self._errors[-10:]

        # 3. Autonomous late-swap execution (if within auto-swap window)
        if (
            self._last_injury_result
            and self._last_injury_result.get("hash_changed")
            and self._saved_lineups
        ):
            try:
                await self._auto_swap_on_scratch(self._last_injury_result)
            except Exception as exc:
                logger.error("[PreLock] Auto-swap failed: %s", exc, exc_info=True)
                self._errors.append(
                    f"{datetime.now(timezone.utc).isoformat()}: auto_swap: {exc}"
                )
                self._errors = self._errors[-10:]

        elapsed = time.time() - t0
        logger.info(
            "[PreLock] Poll #%d complete in %.1fs (active=%s)",
            self._poll_count, elapsed, self._active_window,
        )

    async def _poll_news(self):
        """Force news refresh and detect changes via ID hash."""
        # Force the NewsService to refetch by clearing its cache timestamp
        self._news._cache_timestamp = None

        # get_news() is synchronous
        items, _ = await asyncio.to_thread(self._news.get_news, limit=100)

        new_hash = self._compute_news_hash(items)

        if new_hash != self._last_news_hash and self._last_news_hash != "":
            # News changed
            self._last_news_changed = True
            injury_count = sum(
                1 for item in items
                if getattr(item, "relevance", "") == "injury"
            )
            logger.warning(
                "[PreLock] News data CHANGED — %d items (%d injury-relevant), "
                "hash %s → %s",
                len(items), injury_count,
                self._last_news_hash[:12], new_hash[:12],
            )

            # Bust pool caches so next lineup build picks up new news
            if self._cache:
                try:
                    await self._cache.clear_pattern("pool:*")
                except Exception as exc:
                    logger.warning("[PreLock] Cache clear failed: %s", exc)

            # Publish to Redis Pub/Sub for PrewarmSubscriber
            await self._publish_news_change(len(items), injury_count)
        else:
            self._last_news_changed = False

        self._last_news_hash = new_hash

    @staticmethod
    def _compute_news_hash(items: list) -> str:
        """Compute MD5 of sorted news item IDs for change detection."""
        ids = sorted(getattr(item, "id", "") for item in items)
        return hashlib.md5("|".join(ids).encode()).hexdigest()

    async def _publish_news_change(self, item_count: int, injury_relevant: int):
        """Publish news change event to Redis Pub/Sub channel."""
        if not self._cache or not getattr(self._cache, "_redis", None):
            return

        payload = json.dumps({
            "event": "news_data_change",
            "item_count": item_count,
            "injury_relevant": injury_relevant,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

        try:
            await self._cache._redis.publish("rotation_engine_updates", payload)
            logger.info("[PreLock] Published news_data_change to Redis Pub/Sub")
        except Exception as exc:
            logger.warning("[PreLock] Redis publish failed: %s", exc)

    # ------------------------------------------------------------------
    # Autonomous Late-Swap Execution
    # ------------------------------------------------------------------

    def set_lineups(self, lineups: list, pool: list = None):
        """Register saved lineups + pool for autonomous late-swap patching.

        Call this after generating lineups so the pre-lock poller can
        auto-patch them when a scratch is detected.
        """
        self._saved_lineups = lineups
        self._saved_pool = pool
        self._already_swapped.clear()
        logger.info(
            "[PreLock] Registered %d lineups for auto-swap (pool=%d players)",
            len(lineups), len(pool) if pool else 0,
        )

    async def _auto_swap_on_scratch(self, injury_result: Dict):
        """Detect newly-scratched players and auto-patch saved lineups.

        Only fires when:
        1. We have saved lineups (set_lineups() was called)
        2. We're within LATE_SWAP_AUTO_WINDOW_MINUTES of lock
        3. A star player's status just changed to Out (factor=0.0)
        4. We haven't already swapped for this player
        """
        if not self._saved_lineups:
            return
        if not injury_result.get("hash_changed"):
            return

        # Check we're within the auto-swap window
        now_utc = datetime.now(timezone.utc)
        minutes_to_lock = self._minutes_to_nearest_lock(now_utc)
        if minutes_to_lock is None or minutes_to_lock > LATE_SWAP_AUTO_WINDOW_MINUTES:
            return

        star_changes = injury_result.get("star_changes", [])
        if not star_changes:
            return

        # Find newly-scratched players (escalation to Out / factor=0.0)
        new_scratches = [
            sc for sc in star_changes
            if sc.get("new_factor", 1.0) == 0.0
            and sc.get("severity") in ("escalation", "new_injury")
            and sc.get("team_id") not in self._already_swapped
        ]

        if not new_scratches:
            return

        from app.services.late_swap_service import combo_patch_lineups

        # Build game_start_times from LiveGameStateService if available
        _game_started: Dict[str, bool] = {}
        if hasattr(self, "_live_game_state") and self._live_game_state:
            try:
                states = await asyncio.to_thread(
                    self._live_game_state.get_all_game_states
                )
                for gid, gs in (states or {}).items():
                    _game_started[gid] = getattr(gs, "has_started", False)
            except Exception:
                pass  # Fall back to no game state (all slots unlocked)

        all_reports = []
        for scratch in new_scratches:
            player_name = scratch["player_name"]

            # Find the player_id from the pool
            player_id = None
            if self._saved_pool:
                from app.utils.helpers import normalize_player_name
                scratch_norm = normalize_player_name(player_name)
                for p in self._saved_pool:
                    if normalize_player_name(p.player_name) == scratch_norm:
                        player_id = p.player_id
                        break

            if player_id is None:
                logger.warning(
                    "[AutoSwap] Could not find player_id for '%s' — skipping",
                    player_name,
                )
                continue

            # Mark as swapped (prevent duplicate execution)
            self._already_swapped.add(player_id)

            # Execute the combinatorial sub-slate optimization
            # (upgrades from greedy 1-for-1 to mini-ILP across all unlocked slots)
            report = await asyncio.to_thread(
                combo_patch_lineups,
                scratched_player_id=player_id,
                player_pool=self._saved_pool,
                lineups=self._saved_lineups,
                game_start_times=_game_started,
            )

            all_reports.append((scratch, report))

            logger.warning(
                "[AutoSwap] %s SCRATCHED → patched %d/%d lineups "
                "(%.1fms, %d failed, FP gained: %+.1f)",
                player_name, report.lineups_patched,
                report.lineups_affected, report.elapsed_ms,
                report.lineups_failed, report.total_fp_gained,
            )

        if not all_reports:
            return

        # Export patched lineups to DK CSV
        csv_path = await self._export_patched_csv()

        # Send alerts
        await self._send_swap_alerts(all_reports, csv_path, minutes_to_lock)

        self._last_auto_swap = {
            "timestamp": now_utc.isoformat(),
            "minutes_to_lock": round(minutes_to_lock, 1),
            "scratches": [
                {
                    "player": sc["player_name"],
                    "team": sc.get("team_name", ""),
                    "patched": rpt.lineups_patched,
                    "failed": rpt.lineups_failed,
                    "elapsed_ms": rpt.elapsed_ms,
                }
                for sc, rpt in all_reports
            ],
            "csv_path": csv_path,
        }

    async def _export_patched_csv(self) -> Optional[str]:
        """Export current saved lineups to a DK-ready CSV file."""
        if not self._saved_lineups:
            return None

        try:
            from app.api.routers.lineups import _lineups_to_dk_csv
            import os

            csv_text = _lineups_to_dk_csv(self._saved_lineups, "nba")
            export_dir = os.path.join(
                os.path.dirname(os.path.dirname(__file__)),
                "..", "exports",
            )
            os.makedirs(export_dir, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = os.path.join(export_dir, f"dk_autoswap_{ts}.csv")
            with open(path, "w", encoding="utf-8") as f:
                f.write(csv_text)
            path = os.path.abspath(path)
            logger.info("[AutoSwap] Exported %d lineups to %s", len(self._saved_lineups), path)
            return path
        except Exception as exc:
            logger.error("[AutoSwap] CSV export failed: %s", exc)
            return None

    async def _send_swap_alerts(
        self,
        reports: list,
        csv_path: Optional[str],
        minutes_to_lock: float,
    ):
        """Send Discord + email alerts for auto-swap execution."""
        # Build alert message
        lines = ["[LATE SWAP EXECUTED]"]
        total_patched = 0
        total_failed = 0
        for scratch, report in reports:
            lines.append(
                f"  Scratched: {scratch['player_name']} ({scratch.get('team_name', '?')})"
                f" | Patched {report.lineups_patched}/{report.lineups_affected} lineups"
                f" in {report.elapsed_ms:.0f}ms"
            )
            total_patched += report.lineups_patched
            total_failed += report.lineups_failed

            # Log individual swaps
            for p in report.patches[:5]:  # Top 5 swaps
                lines.append(
                    f"    L{p.lineup_index}: {p.swapped_out} → {p.swapped_in} "
                    f"(FP {p.fp_delta:+.1f}, Sal {p.salary_delta:+,})"
                )
            if len(report.patches) > 5:
                lines.append(f"    ... and {len(report.patches) - 5} more swaps")

        lines.append(f"  Total: {total_patched} patched, {total_failed} failed")
        lines.append(f"  Lock in: {minutes_to_lock:.0f} min")
        if csv_path:
            lines.append(f"  CSV: {csv_path}")

        alert_text = "\n".join(lines)
        logger.warning(alert_text)

        # 1. Discord outbound alert
        try:
            await self._send_discord_alert(alert_text)
        except Exception as exc:
            logger.error("[AutoSwap] Discord alert failed: %s", exc)

        # 2. Email alert with CSV attached
        try:
            await self._send_email_alert(alert_text, csv_path)
        except Exception as exc:
            logger.error("[AutoSwap] Email alert failed: %s", exc)

    async def _send_discord_alert(self, message: str):
        """Post an alert message to the Discord news channel."""
        import os
        import httpx

        bot_token = os.getenv("DISCORD_BOT_TOKEN", "")
        channel_id = os.getenv("DISCORD_NEWS_CHANNEL_ID", "")
        if not bot_token or not channel_id:
            logger.debug("[AutoSwap] Discord not configured — skipping alert")
            return

        url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
        headers = {
            "Authorization": f"Bot {bot_token}",
            "Content-Type": "application/json",
        }

        # Truncate to Discord's 2000-char limit
        if len(message) > 1950:
            message = message[:1950] + "\n... (truncated)"

        payload = {"content": f"```\n{message}\n```"}

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                url, json=payload, headers=headers, timeout=10.0,
            )
            if resp.status_code in (200, 201):
                logger.info("[AutoSwap] Discord alert sent to channel %s", channel_id)
            else:
                logger.warning(
                    "[AutoSwap] Discord alert failed: HTTP %d — %s",
                    resp.status_code, resp.text[:200],
                )

    async def _send_email_alert(self, message: str, csv_path: Optional[str]):
        """Send an email alert to SIMULATION_RECIPIENT with the patched CSV."""
        if not self._notification:
            logger.debug("[AutoSwap] NotificationService not configured — skipping email")
            return

        import os
        recipients = os.getenv("SIMULATION_RECIPIENT", "")
        if not recipients:
            return

        file_paths = [csv_path] if csv_path and os.path.isfile(csv_path) else []

        await asyncio.to_thread(
            self._notification.send_slate_reports,
            subject="[LATE SWAP] Lineups Auto-Patched — Upload Before Lock!",
            body=message,
            file_paths=file_paths,
            recipient=recipients,
        )
        logger.info("[AutoSwap] Email alert sent to %s", recipients)

    # ------------------------------------------------------------------
    # Slate schedule
    # ------------------------------------------------------------------

    def _should_refresh_schedule(self) -> bool:
        """Check if the slate schedule needs refreshing."""
        if not self._slate_schedule:
            return True
        if self._schedule_fetched_at is None:
            return True
        age_min = (
            datetime.now(timezone.utc) - self._schedule_fetched_at
        ).total_seconds() / 60
        return age_min >= SLATE_SCHEDULE_REFRESH_MINUTES

    async def _refresh_slate_schedule(self):
        """Fetch today's slate lock times from DKSlateService."""
        try:
            from app.services.dk_slate_service import DKSlateService

            slate_svc = DKSlateService()
            today = date.today().isoformat()

            slates = await asyncio.to_thread(slate_svc.get_slates, today, "nba")
            self._slate_schedule = [
                {
                    "slate_name": getattr(s, "name", "?"),
                    "lock_time_utc": s.min_start_utc,
                    "draft_group_id": s.draft_group_id,
                    "game_count": getattr(s, "game_count", 0),
                }
                for s in (slates or [])
                if hasattr(s, "min_start_utc") and s.min_start_utc
            ]
            self._schedule_fetched_at = datetime.now(timezone.utc)

            if self._slate_schedule:
                lock_times = [
                    s["lock_time_utc"].strftime("%H:%M UTC")
                    for s in self._slate_schedule
                ]
                logger.info(
                    "[PreLock] Slate schedule: %d slates, locks at %s",
                    len(self._slate_schedule), lock_times,
                )
            else:
                logger.info("[PreLock] No NBA slates found for today")
        except Exception as exc:
            logger.error("[PreLock] Slate schedule fetch failed: %s", exc)
            self._errors.append(
                f"{datetime.now(timezone.utc).isoformat()}: slate_refresh: {exc}"
            )
            self._errors = self._errors[-10:]

    def _minutes_to_nearest_lock(self, now_utc: datetime) -> Optional[float]:
        """Return minutes until the nearest upcoming slate lock, or None."""
        upcoming = [
            (s["lock_time_utc"] - now_utc).total_seconds() / 60
            for s in self._slate_schedule
            if s["lock_time_utc"] > now_utc
        ]
        return min(upcoming) if upcoming else None

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def get_status(self) -> Dict[str, Any]:
        """Return current service status for API monitoring."""
        now_utc = datetime.now(timezone.utc)
        minutes_to_lock = self._minutes_to_nearest_lock(now_utc)

        return {
            "running": self._running,
            "active_window": self._active_window,
            "poll_count": self._poll_count,
            "last_poll_at": self._last_poll_at,
            "minutes_to_nearest_lock": (
                round(minutes_to_lock, 1) if minutes_to_lock is not None else None
            ),
            "slate_schedule": [
                {
                    "name": s["slate_name"],
                    "lock_time_utc": s["lock_time_utc"].isoformat(),
                    "draft_group_id": s["draft_group_id"],
                    "game_count": s.get("game_count", 0),
                    "locked": s["lock_time_utc"] <= now_utc,
                }
                for s in self._slate_schedule
            ],
            "last_injury_hash_changed": (
                (self._last_injury_result or {}).get("hash_changed", False)
            ),
            "last_news_changed": self._last_news_changed,
            "auto_swap": {
                "lineups_registered": len(self._saved_lineups) if self._saved_lineups else 0,
                "pool_size": len(self._saved_pool) if self._saved_pool else 0,
                "already_swapped_ids": list(self._already_swapped),
                "last_execution": self._last_auto_swap,
                "auto_window_minutes": LATE_SWAP_AUTO_WINDOW_MINUTES,
            },
            "recent_errors": self._errors[-5:],
        }
