"""PostgreSQL cache layer for CBB (College Basketball) game-log data.

Mirrors the NBA cache pattern: a nightly background job scrapes CBBpy
box scores for all teams on upcoming slates and upserts them into the
``cbb_player_game_log`` table.  The live CBB lineup generator reads
exclusively from this table, eliminating the ~110s cold-scrape latency.

All read methods return game-log dicts in the same format that
``CBBApiService._fetch_cbbpy_game_logs`` returns, so no caller changes
are needed beyond swapping the data source.
"""

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db.database import get_session, is_db_available
from app.db.models import CBBPlayerGameLog

logger = logging.getLogger(__name__)


class CBBDataCacheService:
    """Reads/writes CBB game-log data from/to the PostgreSQL cache.

    Read path (live lineup generation):
        cache = CBBDataCacheService()
        logs = cache.get_team_game_logs_sync("Duke", "2025")
        # Returns {player_name_lower: [{"MIN": ..., "PTS": ...}, ...]}

    Write path (background refresh):
        await cache.refresh_all(season="2025")
    """

    # ── Event loop plumbing (same pattern as NBADataCacheService) ───
    _main_loop: Optional[asyncio.AbstractEventLoop] = None
    _standalone_loop: Optional[asyncio.AbstractEventLoop] = None
    _standalone_thread = None
    _standalone_lock = None

    @classmethod
    def set_main_loop(cls, loop: asyncio.AbstractEventLoop) -> None:
        cls._main_loop = loop
        logger.info("[CBBCache] Main event loop reference stored")

    @classmethod
    def _get_standalone_loop(cls) -> asyncio.AbstractEventLoop:
        import threading

        if cls._standalone_lock is None:
            cls._standalone_lock = threading.Lock()

        with cls._standalone_lock:
            if cls._standalone_loop is not None and cls._standalone_loop.is_running():
                return cls._standalone_loop

            loop = asyncio.new_event_loop()
            t = threading.Thread(
                target=loop.run_forever,
                daemon=True,
                name="cbb-cache-standalone-loop",
            )
            t.start()
            cls._standalone_loop = loop
            cls._standalone_thread = t

            from app.db.database import init_db

            if not is_db_available():
                fut = asyncio.run_coroutine_threadsafe(init_db(), loop)
                try:
                    fut.result(timeout=10)
                except Exception as e:
                    logger.warning("[CBBCache] DB auto-init failed: %s", e)

            return loop

    # ──────────────────────────────────────────────────────────────
    # READ helpers
    # ──────────────────────────────────────────────────────────────

    async def get_team_game_logs(
        self,
        team_name: str,
        season: str,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Read cached game logs for a CBB team.

        Returns a dict mapping ``player_name_lower`` to a list of
        game-stat dicts with uppercase keys (MIN, PTS, REB, etc.) —
        the same shape as ``CBBApiService._fetch_cbbpy_game_logs``.
        """
        if not is_db_available():
            return {}

        async with get_session() as session:
            stmt = (
                select(CBBPlayerGameLog)
                .where(
                    func.lower(CBBPlayerGameLog.team_name) == team_name.lower(),
                    CBBPlayerGameLog.season == season,
                )
                .order_by(CBBPlayerGameLog.game_date.asc())
            )
            result = await session.execute(stmt)
            rows = result.scalars().all()

        player_logs: Dict[str, List[Dict[str, Any]]] = {}
        for r in rows:
            pname = r.player_name.lower()
            entry = {
                "MIN": r.min or 0.0,
                "PTS": r.pts or 0.0,
                "REB": r.reb or 0.0,
                "AST": r.ast or 0.0,
                "STL": r.stl or 0.0,
                "BLK": r.blk or 0.0,
                "TO": r.tov or 0.0,
                "FG3M": r.fg3m or 0.0,
            }
            player_logs.setdefault(pname, []).append(entry)

        return player_logs

    async def get_team_game_logs_by_id(
        self,
        team_id: int,
        season: str,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Same as get_team_game_logs but lookup by ESPN team_id."""
        if not is_db_available():
            return {}

        async with get_session() as session:
            stmt = (
                select(CBBPlayerGameLog)
                .where(
                    CBBPlayerGameLog.team_id == team_id,
                    CBBPlayerGameLog.season == season,
                )
                .order_by(CBBPlayerGameLog.game_date.asc())
            )
            result = await session.execute(stmt)
            rows = result.scalars().all()

        player_logs: Dict[str, List[Dict[str, Any]]] = {}
        for r in rows:
            pname = r.player_name.lower()
            entry = {
                "MIN": r.min or 0.0,
                "PTS": r.pts or 0.0,
                "REB": r.reb or 0.0,
                "AST": r.ast or 0.0,
                "STL": r.stl or 0.0,
                "BLK": r.blk or 0.0,
                "TO": r.tov or 0.0,
                "FG3M": r.fg3m or 0.0,
            }
            player_logs.setdefault(pname, []).append(entry)

        return player_logs

    async def get_cached_team_count(self, season: str) -> int:
        """Return number of distinct teams cached for a season."""
        if not is_db_available():
            return 0

        async with get_session() as session:
            stmt = (
                select(func.count(func.distinct(CBBPlayerGameLog.team_name)))
                .where(CBBPlayerGameLog.season == season)
            )
            result = await session.execute(stmt)
            return result.scalar() or 0

    # ── Sync wrappers (ThreadPoolExecutor) ────────────────────────

    def get_team_game_logs_sync(
        self,
        team_name: str,
        season: str,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Synchronous wrapper for ``get_team_game_logs``."""
        return self._run_async_with_retry(
            lambda: self.get_team_game_logs(team_name, season)
        )

    def get_team_game_logs_by_id_sync(
        self,
        team_id: int,
        season: str,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Synchronous wrapper for ``get_team_game_logs_by_id``."""
        return self._run_async_with_retry(
            lambda: self.get_team_game_logs_by_id(team_id, season)
        )

    # ──────────────────────────────────────────────────────────────
    # WRITE / REFRESH (background scheduler)
    # ──────────────────────────────────────────────────────────────

    async def refresh_all(
        self,
        season: Optional[str] = None,
        team_names: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Full CBB cache refresh via CBBpy scraping.

        Parameters
        ----------
        season : str, optional
            CBB season year (e.g. "2025"). Defaults to current.
        team_names : list[str], optional
            Specific teams to refresh. If None, fetches all D1 teams
            from the CBBTeamRegistry that are on upcoming DK slates,
            plus any team with existing cache data.

        Returns
        -------
        dict with status, teams_updated, players_updated, games_inserted
        """
        if not is_db_available():
            return {"status": "error", "detail": "database_unavailable"}

        t_start = time.time()

        if not season:
            season = self._get_current_cbb_season()

        teams_updated = 0
        players_updated = 0
        games_inserted = 0
        teams_failed = 0

        # Resolve team list
        teams_to_refresh = team_names or self._get_slate_teams(season)

        if not teams_to_refresh:
            logger.warning("[CBBCache] No teams to refresh")
            return {
                "status": "ok",
                "season": season,
                "teams_updated": 0,
                "players_updated": 0,
                "games_inserted": 0,
                "elapsed_s": 0.0,
            }

        logger.info(
            f"[CBBCache] Starting refresh: {len(teams_to_refresh)} teams, "
            f"season {season}"
        )

        # Fetch each team's box scores via CBBpy (in thread to avoid
        # blocking the event loop — CBBpy is sync + CPU-bound parsing)
        for team_name in teams_to_refresh:
            try:
                team_batch, player_count = await asyncio.to_thread(
                    self._scrape_team_box_scores, team_name, season
                )

                if not team_batch:
                    logger.info(
                        f"[CBBCache] No box-score data for {team_name}"
                    )
                    continue

                # Resolve ESPN team ID for cross-reference
                team_id = self._resolve_team_id(team_name)

                # Stamp team_id onto all rows
                if team_id:
                    for row in team_batch:
                        row["team_id"] = team_id

                # Upsert into DB
                try:
                    async with get_session() as session:
                        stmt = pg_insert(CBBPlayerGameLog).values(team_batch)
                        stmt = stmt.on_conflict_do_update(
                            constraint="uq_cbb_pgl_player_game",
                            set_={
                                "min": stmt.excluded.min,
                                "pts": stmt.excluded.pts,
                                "reb": stmt.excluded.reb,
                                "ast": stmt.excluded.ast,
                                "stl": stmt.excluded.stl,
                                "blk": stmt.excluded.blk,
                                "tov": stmt.excluded.tov,
                                "fg3m": stmt.excluded.fg3m,
                                "fg3a": stmt.excluded.fg3a,
                                "fgm": stmt.excluded.fgm,
                                "fga": stmt.excluded.fga,
                                "ftm": stmt.excluded.ftm,
                                "fta": stmt.excluded.fta,
                                "oreb": stmt.excluded.oreb,
                                "dreb": stmt.excluded.dreb,
                                "pf": stmt.excluded.pf,
                                "team_id": stmt.excluded.team_id,
                                "fetched_at": stmt.excluded.fetched_at,
                            },
                        )
                        await session.execute(stmt)
                        await session.commit()
                        games_inserted += len(team_batch)
                        players_updated += player_count
                        teams_updated += 1
                except Exception as e:
                    logger.exception(
                        f"[CBBCache] CRITICAL DB WRITE FAILURE for "
                        f"{team_name} ({len(team_batch)} rows): {e}"
                    )
                    if team_batch:
                        sample = team_batch[0]
                        logger.error(
                            f"[CBBCache] Sample row keys: "
                            f"{sorted(sample.keys())}"
                        )
                    teams_failed += 1

                logger.info(
                    f"[CBBCache] {team_name}: {len(team_batch)} rows "
                    f"({player_count} players)"
                )

            except Exception as e:
                logger.exception(
                    f"[CBBCache] Scrape failed for {team_name}: {e}"
                )
                teams_failed += 1

        elapsed = time.time() - t_start
        status = "ok" if games_inserted > 0 else "error_no_inserts"
        summary = {
            "status": status,
            "season": season,
            "teams_updated": teams_updated,
            "teams_failed": teams_failed,
            "players_updated": players_updated,
            "games_inserted": games_inserted,
            "elapsed_s": round(elapsed, 1),
        }
        log_fn = logger.info if status == "ok" else logger.error
        log_fn(f"[CBBCache] Refresh complete: {summary}")
        return summary

    # ──────────────────────────────────────────────────────────────
    # Internal helpers
    # ──────────────────────────────────────────────────────────────

    @staticmethod
    def _scrape_team_box_scores(
        team_name: str, season: str
    ) -> Tuple[List[Dict[str, Any]], int]:
        """Scrape one team's season box scores via CBBpy (sync, blocking).

        Returns (list_of_row_dicts, distinct_player_count).
        Each row dict is ready for pg_insert into CBBPlayerGameLog.
        """
        try:
            from app.services.cbbpy_throttle import cbbpy_get_games_team
            import pandas as pd
        except ImportError:
            logger.warning("[CBBCache] CBBpy not installed")
            return [], 0

        season_int = int(season)

        # Ensure CBBpy CSV has this season
        try:
            from app.services.cbb_api_service import CBBApiService
            CBBApiService._ensure_cbbpy_season(season_int)
        except Exception:
            pass

        result = cbbpy_get_games_team(
            team_name, season_int,
            info=False, box=True, pbp=False,
            timeout=120.0,
        )
        if result is None:
            return [], 0

        # Extract the box-score DataFrame
        box_df = None
        if isinstance(result, tuple):
            for item in result:
                if isinstance(item, pd.DataFrame) and not item.empty:
                    box_df = item
                    break
        elif isinstance(result, pd.DataFrame) and not result.empty:
            box_df = result

        if box_df is None:
            return [], 0

        # Filter to requested team only (CBBpy returns both teams per game)
        team_col = "team" if "team" in box_df.columns else None
        if team_col:
            team_lower = team_name.lower()
            box_df = box_df[
                box_df[team_col].astype(str).str.lower().str.contains(
                    team_lower, na=False
                )
            ]

        now = datetime.now(timezone.utc)
        rows: List[Dict[str, Any]] = []
        players_seen: Set[str] = set()

        for _, row in box_df.iterrows():
            pname = str(row.get("player", "")).strip()
            if not pname or pname.lower() in ("nan", "team"):
                continue

            players_seen.add(pname.lower())

            # Build a game_id from available data (date + teams)
            game_date_raw = row.get("date", row.get("game_date", None))
            game_date = _parse_date(game_date_raw)
            game_id_str = row.get(
                "game_id",
                f"{team_name}_{game_date.isoformat() if game_date else 'unknown'}",
            )

            rows.append({
                "player_name": pname,
                "team_name": team_name,
                "team_id": None,  # Filled by caller
                "season": season,
                "game_date": game_date,
                "min": _safe_float(row.get("min", 0)),
                "pts": _safe_float(row.get("pts", 0)),
                "reb": _safe_float(row.get("reb", 0)),
                "ast": _safe_float(row.get("ast", 0)),
                "stl": _safe_float(row.get("stl", 0)),
                "blk": _safe_float(row.get("blk", 0)),
                "tov": _safe_float(row.get("to", 0)),
                "fg3m": _safe_float(row.get("3pm", 0)),
                "fg3a": _safe_float(row.get("3pa", 0)),
                "fgm": _safe_float(row.get("fgm", 0)),
                "fga": _safe_float(row.get("fga", 0)),
                "ftm": _safe_float(row.get("ftm", 0)),
                "fta": _safe_float(row.get("fta", 0)),
                "oreb": _safe_float(row.get("oreb", 0)),
                "dreb": _safe_float(row.get("dreb", 0)),
                "pf": _safe_float(row.get("pf", 0)),
                "game_id": str(game_id_str),
                "fetched_at": now,
            })

        return rows, len(players_seen)

    @staticmethod
    def _resolve_team_id(team_name: str) -> Optional[int]:
        """Resolve CBBpy team name to ESPN team ID."""
        try:
            from app.services.cbb_teams import CBBTeamRegistry
            registry = CBBTeamRegistry()
            team = registry.find_team_by_name(team_name)
            return int(team["id"]) if team else None
        except Exception:
            return None

    @staticmethod
    def _get_current_cbb_season() -> str:
        """Return current CBB season year (e.g. '2025' for 2024-25 season)."""
        now = datetime.now()
        # CBB season spans October-April; if before August, current
        # academic year is still the "season year"
        return str(now.year) if now.month >= 8 else str(now.year)

    @staticmethod
    def _get_slate_teams(season: str) -> List[str]:
        """Get team names that should be cached.

        Pulls from CBBTeamRegistry — in a production setup you'd filter
        to teams on upcoming DK slates, but for the nightly job we cache
        all ~360 D1 teams (CBBpy is the bottleneck, not DB writes).

        For efficiency, we only refresh teams that had games in the last
        7 days or have upcoming DK slate presence.
        """
        try:
            from app.services.cbb_teams import CBBTeamRegistry
            registry = CBBTeamRegistry()
            all_teams = registry.get_all_teams()
            return [t["full_name"] for t in all_teams if t.get("full_name")]
        except Exception as e:
            logger.error(f"[CBBCache] Failed to get team list: {e}")
            return []

    # ── Async/Sync bridge (mirrors NBADataCacheService) ───────────

    @classmethod
    def _run_async_with_retry(cls, coro_factory, retries: int = 1):
        result = cls._run_async(coro_factory())
        if result is not None:
            return result

        for attempt in range(retries):
            time.sleep(0.15 * (attempt + 1))
            result = cls._run_async(coro_factory())
            if result is not None:
                logger.info(
                    "[CBBCache] Retry %d/%d succeeded", attempt + 1, retries,
                )
                return result

        # Return empty dict (not None) so callers get the expected type
        return {}

    @classmethod
    def _run_async(cls, coro):
        """Run an async coroutine from a sync context."""
        import threading

        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None

        # If called from inside a running loop in THIS thread, can't block
        if current_loop is not None:
            if cls._main_loop and cls._main_loop.is_running():
                if current_loop is cls._main_loop:
                    # Same loop — can't block, return empty
                    coro.close()
                    return None
            # Different loop thread — fall through to dispatch
            coro.close()
            return None

        # Preferred: dispatch to main loop
        if cls._main_loop and cls._main_loop.is_running():
            try:
                fut = asyncio.run_coroutine_threadsafe(coro, cls._main_loop)
                return fut.result(timeout=15)
            except Exception as e:
                logger.warning("[CBBCache] Main loop dispatch failed: %s", e)
                return None

        # Fallback: standalone loop
        try:
            loop = cls._get_standalone_loop()
            fut = asyncio.run_coroutine_threadsafe(coro, loop)
            return fut.result(timeout=15)
        except Exception as e:
            logger.warning("[CBBCache] Standalone loop failed: %s", e)
            return None


# ======================================================================
# Module-level helpers
# ======================================================================

def _safe_float(val) -> float:
    """Safely convert a value to float."""
    if val is None:
        return 0.0
    try:
        s = str(val).strip()
        if not s or s.lower() == "nan":
            return 0.0
        if ":" in s:
            parts = s.split(":")
            return float(parts[0]) + float(parts[1]) / 60.0
        return float(s)
    except (ValueError, TypeError):
        return 0.0


def _parse_date(val) -> Optional[datetime]:
    """Parse a date from various CBBpy formats."""
    if val is None:
        return None
    if isinstance(val, datetime):
        return val
    try:
        import pandas as pd
        if isinstance(val, pd.Timestamp):
            return val.to_pydatetime()
    except (ImportError, Exception):
        pass
    try:
        s = str(val).strip()
        # Try common formats
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%b %d, %Y"):
            try:
                return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
    except Exception:
        pass
    return None
