"""Background Redis Pub/Sub subscriber for proactive cache pre-warming.

Listens to the ``rotation_engine_updates`` channel published by
``InjuryService`` whenever a star player's injury status changes.
On receipt, identifies the affected DK slate(s) and triggers
``LineupOptimizerService.build_player_pool()`` to rebuild the cache
*before* any user requests — saving 30-90 seconds of cold-build time.

Architecture:

    ┌────────────────────┐   PUBLISH    ┌───────────────────────┐
    │ InjuryService      │ ──────────►  │ prewarm_subscriber    │
    │ (sync Step 9)      │   Redis Ch.  │ (daemon thread/worker)│
    └────────────────────┘              └───────────┬───────────┘
                                                    │
                                        build_player_pool(slate)
                                                    │
                                        ┌───────────▼───────────┐
                                        │ LineupOptimizerService│
                                        │ (warm cache for user) │
                                        └───────────────────────┘

Concurrency:

    The subscriber runs in a **dedicated daemon thread** spawned at
    FastAPI startup (see ``main.py``).  It uses a synchronous redis-py
    ``pubsub.listen()`` loop — no asyncio interference.  The actual
    ``build_player_pool()`` call is delegated to a sub-thread with a
    hard timeout to avoid blocking the listener.

    Thread-safety is ensured by:
    - LineupOptimizerService's per-key ``_build_locks`` dict (one lock
      per ``platform:draft_group_id:game_date`` key)
    - Module-level ``_pool_lock`` / ``_enriched_lock`` for cache access

Usage:

    # From main.py lifespan (daemon thread):
    from app.services.prewarm_subscriber import start_prewarm_listener
    start_prewarm_listener()

    # Or standalone CLI for testing:
    python -m app.services.prewarm_subscriber
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import date as _date
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Channel name — must match REDIS_PREWARM_CHANNEL in injury_service.py
REDIS_PREWARM_CHANNEL = "rotation_engine_updates"

# Timeout for a single build_player_pool() invocation (seconds).
# Cold builds typically take 30-90s; allow headroom.
_BUILD_TIMEOUT = 180

# Cooldown between consecutive pre-warm builds for the SAME slate
# to avoid stampeding on rapid-fire injury updates.
_COOLDOWN_SECONDS = 30

# Track last build time per slate to enforce cooldown
_last_build_times: Dict[str, float] = {}
_cooldown_lock = threading.Lock()


def _redis_connect():
    """Open a synchronous redis-py connection for the subscriber."""
    try:
        import redis
    except ImportError:
        logger.error("[PreWarm-Sub] redis-py not installed")
        return None

    host = os.environ.get("REDIS_HOST", "localhost")
    port = int(os.environ.get("REDIS_PORT", "6379"))
    db = int(os.environ.get("REDIS_DB", "0"))
    password = os.environ.get("REDIS_PASSWORD") or None

    try:
        r = redis.Redis(
            host=host, port=port, db=db, password=password,
            decode_responses=True, socket_connect_timeout=5,
        )
        r.ping()
        return r
    except Exception as exc:
        logger.warning("[PreWarm-Sub] Redis connection failed: %s", exc)
        return None


def _find_slate_for_team(team_id: int, team_name: str) -> Optional[Dict[str, Any]]:
    """Identify the DK slate containing this team's game today.

    Returns dict with ``draft_group_id``, ``slate_name``, ``game_count``
    or None if no matching slate is found.
    """
    try:
        from app.services.dk_slate_service import DKSlateService
        from nba_api.stats.static import teams as nba_teams

        # Resolve team_id → abbreviation
        abbr = None
        for t in nba_teams.get_teams():
            if t["id"] == team_id:
                abbr = t["abbreviation"]
                break

        if abbr is None:
            logger.warning(
                "[PreWarm-Sub] Could not resolve team_id=%s to abbreviation",
                team_id,
            )
            return None

        today = _date.today().isoformat()
        slate_svc = DKSlateService()
        slates = slate_svc.get_slates(today, sport="nba")

        if not slates:
            return None

        # Find the slate(s) containing this team
        for slate in slates:
            for game in slate.games:
                if game.get("away_abbr") == abbr or game.get("home_abbr") == abbr:
                    return {
                        "draft_group_id": slate.draft_group_id,
                        "slate_name": getattr(slate, "name", "?"),
                        "game_count": getattr(slate, "game_count", 0),
                        "game_date": today,
                    }

        # Fallback: pick the largest slate (Main) — player may be in it
        main_slate = max(slates, key=lambda s: getattr(s, "game_count", 0))
        return {
            "draft_group_id": main_slate.draft_group_id,
            "slate_name": getattr(main_slate, "name", "Main (fallback)"),
            "game_count": getattr(main_slate, "game_count", 0),
            "game_date": today,
        }

    except Exception as exc:
        logger.warning("[PreWarm-Sub] Slate lookup failed: %s", exc)
        return None


def _trigger_prewarm(slate_info: Dict[str, Any], trigger_event: Dict[str, Any]) -> bool:
    """Execute ``build_player_pool()`` for the affected slate.

    Runs in a sub-thread with a hard timeout.  Returns True on success.
    """
    dg_id = slate_info["draft_group_id"]
    game_date = slate_info["game_date"]
    slate_name = slate_info.get("slate_name", "?")
    player_name = trigger_event.get("player_name", "?")

    # Cooldown check
    cache_key = f"dk:{dg_id}:{game_date}"
    with _cooldown_lock:
        last_time = _last_build_times.get(cache_key, 0)
        if time.time() - last_time < _COOLDOWN_SECONDS:
            logger.info(
                "[PreWarm-Sub] Cooldown active for %s — skipping rebuild",
                cache_key,
            )
            return False
        _last_build_times[cache_key] = time.time()

    logger.info(
        "[PreWarm-Sub] Triggering pool rebuild for %s slate (DG %d, %d games) "
        "— caused by %s status change",
        slate_name, dg_id, slate_info.get("game_count", 0), player_name,
    )

    t0 = time.time()
    success = False
    result_box: List[Any] = [None]

    def _build():
        try:
            from app.api.dependencies import get_services
            svc = get_services()

            # Clear stale cache first
            from app.services.lineup_optimizer_service import clear_optimizer_cache
            clear_optimizer_cache(
                platform="dk",
                draft_group_id=dg_id,
                game_date=game_date,
            )

            result_box[0] = svc.lineup_optimizer_service.build_player_pool(
                platform="dk",
                draft_group_id=dg_id,
                game_date=game_date,
                sport="nba",
                data_service=svc.get_data_service("nba"),
                game_service_override=svc.get_game_service("nba"),
                injury_service_override=svc.get_injury_service("nba"),
            )
        except Exception as exc:
            logger.error("[PreWarm-Sub] build_player_pool() failed: %s", exc)

    build_thread = threading.Thread(
        target=_build, daemon=True, name="prewarm-build",
    )
    build_thread.start()
    build_thread.join(timeout=_BUILD_TIMEOUT)

    elapsed = time.time() - t0
    pool = result_box[0]

    if build_thread.is_alive():
        logger.warning(
            "[PreWarm-Sub] Build timed out after %.0fs for %s slate (DG %d)",
            elapsed, slate_name, dg_id,
        )
    elif pool is not None:
        success = True
        logger.warning(
            "[PreWarm-COMPLETE] Pre-warmed %s slate (DG %d): %d players in %.1fs "
            "| Triggered by %s (%s → %s) | "
            "Estimated %.0fs saved for next user request",
            slate_name, dg_id, len(pool), elapsed,
            player_name,
            trigger_event.get("old_status", "?"),
            trigger_event.get("new_status", "?"),
            max(0, elapsed - 0.5),  # ~0.5s warm-cache hit vs cold build time
        )
    else:
        logger.warning(
            "[PreWarm-Sub] Build returned None after %.1fs for %s slate",
            elapsed, slate_name,
        )

    return success


def _listener_loop(redis_client) -> None:
    """Blocking subscribe-and-process loop.  Runs in a daemon thread."""
    pubsub = redis_client.pubsub()
    pubsub.subscribe(REDIS_PREWARM_CHANNEL)
    logger.info(
        "[PreWarm-Sub] Subscribed to Redis channel '%s' — listening for "
        "star injury changes...",
        REDIS_PREWARM_CHANNEL,
    )

    for message in pubsub.listen():
        if message["type"] != "message":
            continue

        try:
            data = json.loads(message["data"])
        except (json.JSONDecodeError, TypeError) as exc:
            logger.warning("[PreWarm-Sub] Bad message: %s — %s", message["data"], exc)
            continue

        event_type = data.get("event")

        # ── Handle news change events from PreLockPollingService ──
        if event_type == "news_data_change":
            injury_relevant = data.get("injury_relevant", 0)
            logger.info(
                "[PreWarm-Sub] News changed (%d items, %d injury-relevant)",
                data.get("item_count", 0), injury_relevant,
            )
            if injury_relevant > 0:
                from datetime import date as _date_cls
                today = _date_cls.today().isoformat()
                try:
                    from app.services.dk_slate_service import DKSlateService
                    slates = DKSlateService().get_slates(today, sport="nba")
                    if slates:
                        main_slate = max(
                            slates,
                            key=lambda s: getattr(s, "game_count", 0),
                        )
                        _trigger_prewarm(
                            {
                                "draft_group_id": main_slate.draft_group_id,
                                "slate_name": getattr(main_slate, "name", "Main"),
                                "game_count": getattr(main_slate, "game_count", 0),
                                "game_date": today,
                            },
                            data,
                        )
                except Exception as exc:
                    logger.warning(
                        "[PreWarm-Sub] News-triggered rebuild failed: %s", exc
                    )
            continue

        if event_type != "star_injury_change":
            logger.debug("[PreWarm-Sub] Ignoring event type: %s", event_type)
            continue

        player_name = data.get("player_name", "?")
        team_id = data.get("team_id")
        team_name = data.get("team_name", "?")
        severity = data.get("severity", "?")

        logger.info(
            "[PreWarm-Sub] Received: %s | %s (%s) team_id=%s | %s → %s",
            severity.upper(), player_name, team_name, team_id,
            data.get("old_status", "?"), data.get("new_status", "?"),
        )

        if team_id is None:
            logger.warning("[PreWarm-Sub] No team_id in message — cannot find slate")
            continue

        # Find the DK slate for this team
        slate_info = _find_slate_for_team(team_id, team_name)
        if slate_info is None:
            logger.info(
                "[PreWarm-Sub] No active DK slate for %s today — skipping",
                team_name,
            )
            continue

        # Trigger the pre-warm build
        _trigger_prewarm(slate_info, data)


def start_prewarm_listener() -> Optional[threading.Thread]:
    """Start the pre-warm subscriber in a background daemon thread.

    Returns the thread object (or None if Redis is unavailable).
    Safe to call from the FastAPI lifespan — the thread is daemonic
    and will be killed when the main process exits.
    """
    redis_client = _redis_connect()
    if redis_client is None:
        logger.info(
            "[PreWarm-Sub] Redis unavailable — pre-warm subscriber disabled"
        )
        return None

    t = threading.Thread(
        target=_listener_loop,
        args=(redis_client,),
        daemon=True,
        name="prewarm-subscriber",
    )
    t.start()
    logger.info("[PreWarm-Sub] Daemon thread started (name='prewarm-subscriber')")
    return t


# ============================================================================
# Standalone CLI for testing
# ============================================================================

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )
    logger.info("Starting pre-warm subscriber in standalone mode...")
    thread = start_prewarm_listener()
    if thread is None:
        print("ERROR: Redis unavailable — cannot start subscriber")
        raise SystemExit(1)

    print(f"Listening on Redis channel '{REDIS_PREWARM_CHANNEL}'...")
    print("Press Ctrl+C to stop.")
    try:
        thread.join()  # Block forever (daemon thread)
    except KeyboardInterrupt:
        print("\nShutting down.")
