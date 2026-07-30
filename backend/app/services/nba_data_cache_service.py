"""PostgreSQL cache layer for NBA API data.

Eliminates the ~140+ live PlayerGameLog calls that cause multi-lineup
generation timeouts.  Data is refreshed daily by a background APScheduler
job; the lineup optimizer reads from the DB cache instead.

All read methods return data in the **exact same dict format** as the
original NBA API, so no caller changes are needed beyond passing a
``cache_service`` parameter.
"""

import asyncio
import logging
import time
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional, Set

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db.database import get_session, is_db_available
from app.db.models import NBAPlayerGameLog, NBATeamStats
from app.utils.helpers import get_current_nba_season

logger = logging.getLogger(__name__)

# ── NBA.com ↔ BallDontLie Team ID Translation ─────────────────────────
# The 4 AM background refresh may use BDL (IDs 1-30) or NBA API (IDs
# 1610612737-1610612766).  The live rotation path always passes NBA.com
# IDs.  When a query returns 0 rows, we retry with the translated ID
# to handle cross-source mismatches.
#
# Authoritative mapping: NBA.com team_id → BDL team_id
_NBA_TO_BDL_TEAM: Dict[int, int] = {
    1610612737: 1,   # ATL
    1610612738: 2,   # BOS
    1610612751: 3,   # BKN
    1610612766: 4,   # CHA
    1610612741: 5,   # CHI
    1610612739: 6,   # CLE
    1610612742: 7,   # DAL
    1610612743: 8,   # DEN
    1610612765: 9,   # DET
    1610612744: 10,  # GSW
    1610612745: 11,  # HOU
    1610612754: 12,  # IND
    1610612746: 13,  # LAC
    1610612747: 14,  # LAL
    1610612763: 15,  # MEM
    1610612748: 16,  # MIA
    1610612749: 17,  # MIL
    1610612750: 18,  # MIN
    1610612740: 19,  # NOP
    1610612752: 20,  # NYK
    1610612760: 21,  # OKC
    1610612753: 22,  # ORL
    1610612755: 23,  # PHI
    1610612756: 24,  # PHX
    1610612757: 25,  # POR
    1610612758: 26,  # SAC
    1610612759: 27,  # SAS
    1610612761: 28,  # TOR
    1610612762: 29,  # UTA
    1610612764: 30,  # WAS
}
_BDL_TO_NBA_TEAM: Dict[int, int] = {v: k for k, v in _NBA_TO_BDL_TEAM.items()}


def _translate_team_id(team_id: int) -> Optional[int]:
    """Translate a team_id to the OTHER format.

    If given an NBA.com ID (1610612xxx), returns the BDL ID (1-30).
    If given a BDL ID (1-30), returns the NBA.com ID (1610612xxx).
    Returns None if the ID is unknown.
    """
    if team_id in _NBA_TO_BDL_TEAM:
        return _NBA_TO_BDL_TEAM[team_id]
    if team_id in _BDL_TO_NBA_TEAM:
        return _BDL_TO_NBA_TEAM[team_id]
    return None


class NBADataCacheService:
    """Reads/writes NBA API data from/to PostgreSQL cache tables.

    Usage in the lineup pipeline:
        cache = NBADataCacheService()
        logs = cache.get_player_game_logs_sync(player_id, season)
        # Returns List[Dict] with the same keys as nba_api PlayerGameLog

    Background refresh:
        await cache.refresh_all()   # Called by APScheduler at 4 AM ET
    """

    # Reference to the main FastAPI event loop, set during app startup.
    # Used by _run_async() to schedule DB coroutines from worker threads
    # via asyncio.run_coroutine_threadsafe() instead of asyncio.run(),
    # which would create a new event loop that can't share the
    # SQLAlchemy async engine's connection pool.
    _main_loop: Optional[asyncio.AbstractEventLoop] = None

    # Persistent background event loop for standalone contexts (scripts,
    # tests, CLI).  Created lazily by _get_standalone_loop() and shared
    # across all sync wrapper calls so the asyncpg connection pool stays
    # on a single loop.
    _standalone_loop: Optional[asyncio.AbstractEventLoop] = None
    _standalone_thread = None  # type: ignore[assignment]
    _standalone_lock = None  # type: ignore[assignment]  # set below

    @classmethod
    def set_main_loop(cls, loop: asyncio.AbstractEventLoop) -> None:
        """Store the main event loop reference for cross-thread DB access."""
        cls._main_loop = loop
        logger.info("[NBACache] Main event loop reference stored for sync wrappers")

    @classmethod
    def _get_standalone_loop(cls) -> asyncio.AbstractEventLoop:
        """Return a persistent background event loop for standalone contexts.

        Creates the loop + daemon thread on first call, auto-initializes
        the DB engine so that standalone scripts (comparison, tests, CLI)
        get DB cache access without manually calling ``init_db()``.
        """
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
                name="nba-cache-standalone-loop",
            )
            t.start()
            cls._standalone_loop = loop
            cls._standalone_thread = t

            # Auto-init DB on this loop (non-blocking for the caller,
            # but we wait here so the first query can use the DB).
            from app.db.database import is_db_available, init_db

            if not is_db_available():
                fut = asyncio.run_coroutine_threadsafe(init_db(), loop)
                try:
                    ok = fut.result(timeout=10)
                    if ok:
                        logger.info(
                            "[NBACache] Auto-initialized DB for standalone context"
                        )
                    else:
                        logger.warning(
                            "[NBACache] DB auto-init returned False "
                            "(DATABASE_URL may be unset)"
                        )
                except Exception as e:
                    logger.warning(
                        "[NBACache] DB auto-init failed: %s", e,
                    )

            return loop

    # ──────────────────────────────────────────────────────────────
    # READ helpers  (async + sync wrappers)
    # ──────────────────────────────────────────────────────────────

    async def get_player_game_logs(
        self,
        player_id: int,
        season: str | None = None,
        last_n: int = 82,
    ) -> List[Dict[str, Any]]:
        """Read cached game logs for a player, newest-first.

        Returns a list of dicts with **uppercase** keys matching the
        NBA API ``PlayerGameLog`` endpoint format, e.g.::

            {"GAME_DATE": "2026-02-10", "MIN": 34, "PTS": 28, ...}
        """
        if not is_db_available():
            return []

        season = season or get_current_nba_season()

        async with get_session() as session:
            stmt = (
                select(NBAPlayerGameLog)
                .where(
                    NBAPlayerGameLog.player_id == player_id,
                    NBAPlayerGameLog.season == season,
                )
                .order_by(NBAPlayerGameLog.game_date.desc())
                .limit(last_n)
            )
            result = await session.execute(stmt)
            rows = result.scalars().all()

        return [self._gamelog_row_to_dict(r) for r in rows]

    async def get_team_game_logs(
        self,
        team_id: int,
        season: str | None = None,
    ) -> Dict[int, List[Dict[str, Any]]]:
        """Read all cached game logs for players on a team.

        Returns ``{player_id: [game_log_dicts]}`` — same shape as if
        you called ``get_player_game_logs`` for every roster member.

        Defensive: if the primary team_id returns 0 rows, retries with
        the translated ID (NBA.com ↔ BDL) to handle cross-source
        mismatches from mixed 4 AM refreshes.
        """
        if not is_db_available():
            return {}

        season = season or get_current_nba_season()

        async with get_session() as session:
            stmt = (
                select(NBAPlayerGameLog)
                .where(
                    NBAPlayerGameLog.team_id == team_id,
                    NBAPlayerGameLog.season == season,
                )
                .order_by(
                    NBAPlayerGameLog.player_id,
                    NBAPlayerGameLog.game_date.desc(),
                )
            )
            result = await session.execute(stmt)
            rows = result.scalars().all()

            # ── Defensive retry with translated team_id ───────────────
            if not rows:
                alt_id = _translate_team_id(team_id)
                if alt_id is not None:
                    logger.warning(
                        "[NBACache] *** TEAM ID MISMATCH *** "
                        "get_team_game_logs(%d) returned 0 rows — "
                        "retrying with translated ID %d "
                        "(NBA↔BDL cross-source)",
                        team_id, alt_id,
                    )
                    stmt_alt = (
                        select(NBAPlayerGameLog)
                        .where(
                            NBAPlayerGameLog.team_id == alt_id,
                            NBAPlayerGameLog.season == season,
                        )
                        .order_by(
                            NBAPlayerGameLog.player_id,
                            NBAPlayerGameLog.game_date.desc(),
                        )
                    )
                    result_alt = await session.execute(stmt_alt)
                    rows = result_alt.scalars().all()
                    if rows:
                        logger.warning(
                            "[NBACache] *** RECOVERED *** "
                            "Translated ID %d returned %d game log rows. "
                            "DB was populated with %s IDs, queried with %s.",
                            alt_id, len(rows),
                            "BDL" if alt_id <= 30 else "NBA.com",
                            "NBA.com" if team_id > 30 else "BDL",
                        )
                else:
                    logger.error(
                        "[NBACache] *** UNKNOWN TEAM ID *** "
                        "get_team_game_logs(%d) returned 0 rows and "
                        "ID is NOT in NBA↔BDL translation map! "
                        "This team mapping is MISSING.",
                        team_id,
                    )

        by_player: Dict[int, List[Dict]] = {}
        for r in rows:
            pid = r.player_id
            if pid not in by_player:
                by_player[pid] = []
            by_player[pid].append(self._gamelog_row_to_dict(r))
        return by_player

    async def get_team_roster(
        self, team_id: int, season: str | None = None
    ) -> List[Dict[str, Any]]:
        """Read cached roster for a team.

        Returns a list of dicts matching the NBA API ``CommonTeamRoster``
        format::

            {"PLAYER_ID": 123, "PLAYER": "LeBron James",
             "POSITION": "F", "BIRTH_DATE": "1984-12-30T00:00:00"}

        Defensive: if the primary team_id returns no roster, retries with
        the translated ID (NBA.com ↔ BDL).
        """
        if not is_db_available():
            return []

        season = season or get_current_nba_season()
        today = date.today()

        async with get_session() as session:
            # Filter for rows where rosters is an actual JSON array
            # (BDL-only refresh days may store JSON null instead of
            # roster data; only NBA API refresh days have real rosters).
            stmt = (
                select(NBATeamStats)
                .where(
                    NBATeamStats.team_id == team_id,
                    NBATeamStats.season == season,
                    func.jsonb_typeof(NBATeamStats.rosters) == "array",
                )
                .order_by(NBATeamStats.stat_date.desc())
                .limit(1)
            )
            result = await session.execute(stmt)
            row = result.scalars().first()

            # ── Defensive retry with translated team_id ───────────────
            if (not row or not row.rosters):
                alt_id = _translate_team_id(team_id)
                if alt_id is not None:
                    logger.warning(
                        "[NBACache] *** TEAM ID MISMATCH *** "
                        "get_team_roster(%d) found no roster — "
                        "retrying with translated ID %d",
                        team_id, alt_id,
                    )
                    stmt_alt = (
                        select(NBATeamStats)
                        .where(
                            NBATeamStats.team_id == alt_id,
                            NBATeamStats.season == season,
                            func.jsonb_typeof(NBATeamStats.rosters) == "array",
                        )
                        .order_by(NBATeamStats.stat_date.desc())
                        .limit(1)
                    )
                    result_alt = await session.execute(stmt_alt)
                    row_alt = result_alt.scalars().first()
                    if row_alt and row_alt.rosters:
                        row = row_alt
                        logger.warning(
                            "[NBACache] *** RECOVERED *** "
                            "Translated ID %d returned roster with "
                            "%d players.",
                            alt_id, len(row.rosters),
                        )
                else:
                    logger.error(
                        "[NBACache] *** UNKNOWN TEAM ID *** "
                        "get_team_roster(%d) found no roster and "
                        "ID is NOT in NBA↔BDL translation map!",
                        team_id,
                    )

        if not row or not row.rosters:
            return []

        # Rosters are stored as list of dicts with lowercase keys;
        # convert back to NBA API uppercase format.
        out = []
        for p in row.rosters:
            out.append({
                "PLAYER_ID": p.get("player_id"),
                "PLAYER": p.get("player_name", ""),
                "POSITION": p.get("position", "F"),
                "BIRTH_DATE": p.get("birth_date"),
                "TeamID": team_id,
            })
        return out

    async def get_all_team_stats(
        self, season: str | None = None
    ) -> Dict[int, Dict[str, Any]]:
        """Read cached team stats for all teams.

        Returns ``{team_id: {...}}`` matching the shape returned by
        ``GameService._get_all_team_stats()``.
        """
        if not is_db_available():
            return {}

        season = season or get_current_nba_season()

        async with get_session() as session:
            # Get the most recent stat_date for this season
            sub = (
                select(NBATeamStats.stat_date)
                .where(NBATeamStats.season == season)
                .order_by(NBATeamStats.stat_date.desc())
                .limit(1)
                .scalar_subquery()
            )
            stmt = select(NBATeamStats).where(
                NBATeamStats.season == season,
                NBATeamStats.stat_date == sub,
            )
            result = await session.execute(stmt)
            rows = result.scalars().all()

        cache: Dict[int, Dict] = {}
        for r in rows:
            cache[r.team_id] = {
                "team_id": r.team_id,
                "team_name": "",  # Not stored separately; callers tolerate empty
                "gp": r.gp or 0,
                "ppg": r.ppg or 0.0,
                "fgm": r.fgm or 0.0,
                "fga": r.fga or 0.0,
                "ftm": r.ftm or 0.0,
                "fta": r.fta or 0.0,
                "oreb": r.oreb or 0.0,
                "dreb": r.dreb or 0.0,
                "tov": r.tov or 0.0,
                "pace": r.pace or 0.0,
                "off_rating": r.off_rating or 0.0,
                "def_rating": r.def_rating or 0.0,
                "w": r.w or 0,
                "l": r.l or 0,
                "opp_pts_pg": r.opp_pts_pg or 0.0,
                "opp_reb_pg": r.opp_reb_pg or 0.0,
                "opp_ast_pg": r.opp_ast_pg or 0.0,
                "opp_stl_pg": r.opp_stl_pg or 0.0,
                "opp_blk_pg": r.opp_blk_pg or 0.0,
                "opp_tov_pg": r.opp_tov_pg or 0.0,
                "opp_fg3m_pg": r.opp_fg3m_pg or 0.0,
            }
        return cache

    async def get_usage_rates(
        self, season: str | None = None
    ) -> Dict[int, float]:
        """Read cached usage rates for all players.

        Returns ``{player_id: usg_pct}`` (0.0–0.40 scale).
        """
        if not is_db_available():
            return {}

        season = season or get_current_nba_season()

        async with get_session() as session:
            # Get the most recent stat_date
            sub = (
                select(NBATeamStats.stat_date)
                .where(NBATeamStats.season == season)
                .order_by(NBATeamStats.stat_date.desc())
                .limit(1)
                .scalar_subquery()
            )
            stmt = select(NBATeamStats.usage_rates).where(
                NBATeamStats.season == season,
                NBATeamStats.stat_date == sub,
                NBATeamStats.usage_rates.isnot(None),
            ).limit(1)
            result = await session.execute(stmt)
            row = result.scalars().first()

        if not row:
            return {}

        # JSONB keys are strings; convert to int
        return {int(k): float(v) for k, v in row.items() if v}

    async def is_cache_fresh(self, season: str | None = None) -> bool:
        """Check if today's data exists in the cache."""
        if not is_db_available():
            return False

        season = season or get_current_nba_season()
        today = date.today()

        async with get_session() as session:
            stmt = (
                select(NBATeamStats.id)
                .where(
                    NBATeamStats.season == season,
                    NBATeamStats.stat_date >= datetime(
                        today.year, today.month, today.day,
                        tzinfo=timezone.utc,
                    ),
                )
                .limit(1)
            )
            result = await session.execute(stmt)
            return result.scalars().first() is not None

    # ── Sync wrappers (for use inside ThreadPoolExecutor) ─────────
    #
    # Each wrapper calls ``_run_async_with_retry`` which dispatches the
    # coroutine to the main event loop.  If the first attempt fails due
    # to a transient connection-pool error (common when many threads
    # dispatch DB queries concurrently), it retries once after a short
    # delay with a fresh coroutine.

    def get_player_game_logs_sync(
        self,
        player_id: int,
        season: str | None = None,
        last_n: int = 82,
    ) -> List[Dict[str, Any]]:
        """Synchronous wrapper for ``get_player_game_logs``."""
        return self._run_async_with_retry(
            lambda: self.get_player_game_logs(player_id, season, last_n)
        )

    def get_team_game_logs_sync(
        self, team_id: int, season: str | None = None
    ) -> Dict[int, List[Dict[str, Any]]]:
        """Synchronous wrapper for ``get_team_game_logs``."""
        return self._run_async_with_retry(
            lambda: self.get_team_game_logs(team_id, season)
        )

    def get_team_roster_sync(
        self, team_id: int, season: str | None = None
    ) -> List[Dict[str, Any]]:
        """Synchronous wrapper for ``get_team_roster``."""
        return self._run_async_with_retry(
            lambda: self.get_team_roster(team_id, season)
        )

    def get_all_team_stats_sync(
        self, season: str | None = None
    ) -> Dict[int, Dict[str, Any]]:
        """Synchronous wrapper for ``get_all_team_stats``."""
        return self._run_async_with_retry(
            lambda: self.get_all_team_stats(season)
        )

    def get_usage_rates_sync(
        self, season: str | None = None
    ) -> Dict[int, float]:
        """Synchronous wrapper for ``get_usage_rates``."""
        return self._run_async_with_retry(
            lambda: self.get_usage_rates(season)
        )

    def is_cache_fresh_sync(self, season: str | None = None) -> bool:
        """Synchronous wrapper for ``is_cache_fresh``."""
        return self._run_async_with_retry(
            lambda: self.is_cache_fresh(season)
        )

    # ──────────────────────────────────────────────────────────────
    # WRITE / REFRESH  (called by background scheduler)
    # ──────────────────────────────────────────────────────────────

    async def refresh_all(
        self, season: str | None = None, balldontlie=None
    ) -> Dict[str, Any]:
        """Full cache refresh — fetch all data from NBA API and write to DB.

        Steps:
        1. Check DB availability (fail fast before expensive API work)
        2. Fetch LeagueDashTeamStats (Base + Advanced + Opponent)
        3. Fetch LeagueDashPlayerBioStats (usage rates)
        4. Fetch CommonTeamRoster for all 30 teams
        5. Fetch PlayerGameLog for every rostered player
        6. Upsert everything into PostgreSQL

        When a ``balldontlie`` service is provided and available, Steps 4-5
        use BDL bulk endpoints instead of per-player NBA API calls (~60
        BDL calls replace ~450 nba_api calls).

        All synchronous NBA API calls are wrapped in ``asyncio.to_thread``
        to avoid blocking the event loop during the 4 AM refresh.

        Returns a summary dict with counts and elapsed time.
        """
        # ── Fail fast if DB is unavailable ────────────────────────
        if not is_db_available():
            logger.error(
                "[NBACache] DB not available — skipping refresh to avoid "
                "wasting API calls"
            )
            return {"status": "error", "detail": "database_unavailable"}

        from app.services.nba_api_service import NBAApiService

        season = season or get_current_nba_season()
        t_start = time.time()
        nba = NBAApiService()
        today = date.today()
        stat_date = datetime(today.year, today.month, today.day, tzinfo=timezone.utc)

        logger.info(f"[NBACache] Starting full refresh for season {season}...")

        # ── Step 1: Team stats (Base + Advanced + Opponent) ───────
        # Wrapped in to_thread so the blocking nba_api HTTP calls
        # don't stall the event loop.  background_mode=True allows the
        # NBA API calls (this is the 4 AM refresh, not a user request).
        teams_updated = 0
        all_team_stats: Dict[int, Dict] = {}
        try:
            from app.services.game_service import GameService
            game_svc = GameService()
            all_team_stats = await asyncio.to_thread(
                lambda: game_svc._get_all_team_stats(season, background_mode=True)
            )
            if all_team_stats:
                teams_updated = len(all_team_stats)
                logger.info(
                    f"[NBACache] Fetched team stats for {teams_updated} teams"
                )
        except Exception as e:
            all_team_stats = {}
            logger.error(f"[NBACache] Team stats fetch failed: {e}")

        # ── Step 2: Usage rates ───────────────────────────────────
        # Try NBA API first (background mode); compute from game logs as fallback
        usage_map: Dict[int, float] = {}
        try:
            usage_map = await asyncio.to_thread(nba._get_usage_rates, season)
            logger.info(
                f"[NBACache] Fetched usage rates for {len(usage_map)} players"
            )
        except Exception as e:
            logger.warning(f"[NBACache] Usage rates API fetch failed: {e}")
            # Will be computed from BDL game logs below if available

        # ── Step 3: Rosters for all teams ─────────────────────────
        from nba_api.stats.static import teams as nba_teams_static
        all_nba_teams = nba_teams_static.get_teams()
        rosters_by_team: Dict[int, List[Dict]] = {}

        # Track BDL game logs if BDL is used for roster fetching
        _bdl_game_logs: Dict[int, Dict[str, List[Dict]]] = {}  # team_id → {name: [logs]}
        _use_bdl_for_logs = False

        if balldontlie and getattr(balldontlie, "is_available", False):
            # Use BDL for bulk roster + game log fetch
            logger.info("[NBACache] Using BallDontLie for roster + game log fetch")
            _use_bdl_for_logs = True
            for team in all_nba_teams:
                tid = team["id"]
                try:
                    bdl_roster, bdl_logs = await asyncio.to_thread(
                        balldontlie.get_team_game_logs_bulk, tid, season
                    )
                    if bdl_roster:
                        # Convert BDL roster to NBA API format
                        nba_roster = []
                        for bp in bdl_roster:
                            nba_roster.append({
                                "PLAYER_ID": bp.get("id"),  # BDL ID (used as key)
                                "PLAYER": f"{bp['first_name']} {bp['last_name']}",
                                "POSITION": bp.get("position", "F") or "F",
                                "BIRTH_DATE": None,
                            })
                        rosters_by_team[tid] = nba_roster
                        _bdl_game_logs[tid] = bdl_logs
                except Exception as e:
                    logger.warning(
                        f"[NBACache] BDL fetch failed for team {tid}: {e}"
                    )
        else:
            # Fall back to nba_api for rosters
            for team in all_nba_teams:
                tid = team["id"]
                try:
                    roster = await asyncio.to_thread(
                        nba.get_team_roster, tid, season
                    )
                    rosters_by_team[tid] = roster
                except Exception as e:
                    logger.warning(
                        f"[NBACache] Roster fetch failed for team {tid}: {e}"
                    )

        logger.info(
            f"[NBACache] Fetched rosters for {len(rosters_by_team)} teams "
            f"({sum(len(r) for r in rosters_by_team.values())} total players)"
            f"{' (via BDL)' if _use_bdl_for_logs else ''}"
        )

        # ── Step 3b: BDL fallback for team stats + usage rates ────
        # If NBA API team stats failed, compute from BDL game-log data
        if not all_team_stats and _use_bdl_for_logs and _bdl_game_logs:
            logger.info("[NBACache] Computing team stats from BDL game logs...")
            all_team_stats = self._aggregate_team_stats_from_bdl(_bdl_game_logs)
            teams_updated = len(all_team_stats)
            logger.info(f"[NBACache] BDL-aggregated team stats for {teams_updated} teams")

        # If usage rates failed, compute from BDL game logs
        if not usage_map and _use_bdl_for_logs and _bdl_game_logs:
            logger.info("[NBACache] Computing usage rates from BDL game logs...")
            usage_map = self._compute_usage_from_bdl_logs(_bdl_game_logs)
            logger.info(f"[NBACache] BDL-computed usage rates for {len(usage_map)} players")

        # ── Step 4: Write team stats + rosters + usage to DB ──────
        async with get_session() as session:
            for tid, stats in all_team_stats.items():
                roster_data = None
                if tid in rosters_by_team:
                    roster_data = [
                        {
                            "player_id": p.get("PLAYER_ID"),
                            "player_name": p.get("PLAYER", ""),
                            "position": p.get("POSITION", "F"),
                            "birth_date": str(p.get("BIRTH_DATE", "")),
                        }
                        for p in rosters_by_team[tid]
                    ]

                values = {
                    "team_id": tid,
                    "season": season,
                    "stat_date": stat_date,
                    "gp": stats.get("gp"),
                    "ppg": stats.get("ppg"),
                    "fgm": stats.get("fgm"),
                    "fga": stats.get("fga"),
                    "ftm": stats.get("ftm"),
                    "fta": stats.get("fta"),
                    "oreb": stats.get("oreb"),
                    "dreb": stats.get("dreb"),
                    "tov": stats.get("tov"),
                    "pace": stats.get("pace"),
                    "off_rating": stats.get("off_rating"),
                    "def_rating": stats.get("def_rating"),
                    "w": stats.get("w"),
                    "l": stats.get("l"),
                    "opp_pts_pg": stats.get("opp_pts_pg"),
                    "opp_reb_pg": stats.get("opp_reb_pg"),
                    "opp_ast_pg": stats.get("opp_ast_pg"),
                    "opp_stl_pg": stats.get("opp_stl_pg"),
                    "opp_blk_pg": stats.get("opp_blk_pg"),
                    "opp_tov_pg": stats.get("opp_tov_pg"),
                    "opp_fg3m_pg": stats.get("opp_fg3m_pg"),
                    "usage_rates": {str(k): v for k, v in usage_map.items()},
                    "fetched_at": datetime.now(timezone.utc),
                }

                # Only include roster data when actually fetched.
                # Prevents overwriting good roster data with JSON null
                # on days when BDL/NBA API roster fetch fails.
                if roster_data is not None:
                    values["rosters"] = roster_data

                stmt = pg_insert(NBATeamStats).values(**values)
                update_keys = {
                    k: v for k, v in values.items()
                    if k not in ("team_id", "season", "stat_date")
                }
                stmt = stmt.on_conflict_do_update(
                    constraint="uq_nts_team_season_date",
                    set_=update_keys,
                )
                await session.execute(stmt)

            await session.commit()
            logger.info(
                f"[NBACache] Upserted team stats for {teams_updated} teams"
            )

        # ── Step 5: Player game logs for all rostered players ─────
        # Batched per team (one DB session + commit per team) to reduce
        # connection overhead vs. the previous per-player approach.
        # When BDL was used (Step 3), game logs are already fetched in
        # _bdl_game_logs — no additional API calls needed.
        players_updated = 0
        games_inserted = 0

        for tid, roster in rosters_by_team.items():
            team_batch: List[Dict] = []

            for player in roster:
                pid = player.get("PLAYER_ID")
                pname = player.get("PLAYER", "?")
                if not pid:
                    continue

                try:
                    # Use pre-fetched BDL game logs if available
                    if _use_bdl_for_logs and tid in _bdl_game_logs:
                        game_log = _bdl_game_logs[tid].get(pname, [])
                    else:
                        game_log = await asyncio.to_thread(
                            nba.get_player_game_log, pid, season, 82
                        )
                    if not game_log:
                        continue

                    for g in game_log:
                        game_date_str = g.get("GAME_DATE", "")
                        try:
                            _ds = str(game_date_str)
                            if "T" in _ds:
                                gd = datetime.fromisoformat(_ds)
                            elif "-" in _ds and len(_ds) == 10:
                                # ISO date without time: "2026-03-15" (BDL)
                                gd = datetime.strptime(_ds, "%Y-%m-%d")
                            else:
                                # NBA API format: "Mar 15, 2026"
                                gd = datetime.strptime(_ds, "%b %d, %Y")
                            if gd.tzinfo is None:
                                gd = gd.replace(tzinfo=timezone.utc)
                        except (ValueError, TypeError):
                            continue

                        game_id = str(g.get("Game_ID", g.get("GAME_ID", "")))
                        if not game_id:
                            continue

                        team_batch.append({
                            "player_id": pid,
                            "player_name": pname,
                            "team_id": tid,
                            "season": season,
                            "game_date": gd,
                            "matchup": g.get("MATCHUP", ""),
                            "wl": g.get("WL", ""),
                            "min": self._safe_float(g.get("MIN")),
                            "pts": self._safe_float(g.get("PTS")),
                            "reb": self._safe_float(g.get("REB")),
                            "ast": self._safe_float(g.get("AST")),
                            "stl": self._safe_float(g.get("STL")),
                            "blk": self._safe_float(g.get("BLK")),
                            "tov": self._safe_float(g.get("TOV")),
                            "fg3m": self._safe_float(g.get("FG3M")),
                            "fgm": self._safe_float(g.get("FGM")),
                            "fga": self._safe_float(g.get("FGA")),
                            "fg_pct": self._safe_float(g.get("FG_PCT")),
                            "fg3a": self._safe_float(g.get("FG3A")),
                            "fg3_pct": self._safe_float(g.get("FG3_PCT")),
                            "ftm": self._safe_float(g.get("FTM")),
                            "fta": self._safe_float(g.get("FTA")),
                            "ft_pct": self._safe_float(g.get("FT_PCT")),
                            "oreb": self._safe_float(g.get("OREB")),
                            "dreb": self._safe_float(g.get("DREB")),
                            "pf": self._safe_float(g.get("PF")),
                            "plus_minus": self._safe_float(g.get("PLUS_MINUS")),
                            "game_id": game_id,
                            "raw_data": g,
                            "fetched_at": datetime.now(timezone.utc),
                        })

                    players_updated += 1

                except Exception as e:
                    logger.warning(
                        f"[NBACache] Game log fetch failed for "
                        f"{pname} (pid={pid}): {e}"
                    )

            # Commit entire team's game logs in one batch
            if team_batch:
                try:
                    async with get_session() as session:
                        stmt = pg_insert(NBAPlayerGameLog).values(team_batch)
                        stmt = stmt.on_conflict_do_update(
                            constraint="uq_npgl_player_game",
                            set_={
                                "min": stmt.excluded.min,
                                "pts": stmt.excluded.pts,
                                "reb": stmt.excluded.reb,
                                "ast": stmt.excluded.ast,
                                "stl": stmt.excluded.stl,
                                "blk": stmt.excluded.blk,
                                "tov": stmt.excluded.tov,
                                "fg3m": stmt.excluded.fg3m,
                                "fgm": stmt.excluded.fgm,
                                "fga": stmt.excluded.fga,
                                "fg_pct": stmt.excluded.fg_pct,
                                "fg3a": stmt.excluded.fg3a,
                                "fg3_pct": stmt.excluded.fg3_pct,
                                "ftm": stmt.excluded.ftm,
                                "fta": stmt.excluded.fta,
                                "ft_pct": stmt.excluded.ft_pct,
                                "oreb": stmt.excluded.oreb,
                                "dreb": stmt.excluded.dreb,
                                "pf": stmt.excluded.pf,
                                "plus_minus": stmt.excluded.plus_minus,
                                "raw_data": stmt.excluded.raw_data,
                                "fetched_at": stmt.excluded.fetched_at,
                            },
                        )
                        await session.execute(stmt)
                        await session.commit()
                        games_inserted += len(team_batch)
                except Exception as e:
                    logger.exception(
                        f"[NBACache] CRITICAL DB WRITE FAILURE for team "
                        f"{tid} ({len(team_batch)} rows): {e}"
                    )
                    # Surface first failing row's keys for schema debugging
                    if team_batch:
                        sample = team_batch[0]
                        logger.error(
                            f"[NBACache] Sample row keys: {sorted(sample.keys())}"
                        )

            logger.info(
                f"[NBACache] Team {tid}: {len(team_batch)} game log rows "
                f"({players_updated} players so far, {games_inserted} total games)"
            )

        elapsed = time.time() - t_start
        status = "ok" if games_inserted > 0 else "error_no_inserts"
        summary = {
            "status": status,
            "season": season,
            "teams_updated": teams_updated,
            "players_updated": players_updated,
            "games_inserted": games_inserted,
            "elapsed_s": round(elapsed, 1),
        }
        log_fn = logger.info if status == "ok" else logger.error
        log_fn(f"[NBACache] Refresh complete: {summary}")
        return summary

    # ──────────────────────────────────────────────────────────────
    # BDL aggregation helpers (background refresh fallback)
    # ──────────────────────────────────────────────────────────────

    @staticmethod
    def _aggregate_team_stats_from_bdl(
        bdl_game_logs: Dict[int, Dict[str, List[Dict]]],
    ) -> Dict[int, Dict]:
        """Compute team-level season stats from BDL player game logs.

        Aggregates player box scores per game date to derive per-game
        team averages for PPG, FGA, FTA, rebounds, turnovers, and
        estimated pace/ratings.  Used when stats.nba.com is unavailable
        during the background refresh.
        """
        result: Dict[int, Dict] = {}
        for tid, logs_by_name in bdl_game_logs.items():
            # Collect all unique game dates and sum stats per game
            games: Dict[str, Dict[str, float]] = {}  # date → stat totals
            for _name, player_logs in logs_by_name.items():
                for g in player_logs:
                    gd = g.get("GAME_DATE", "")
                    if not gd:
                        continue
                    if gd not in games:
                        games[gd] = {
                            "PTS": 0, "FGM": 0, "FGA": 0, "FTM": 0, "FTA": 0,
                            "OREB": 0, "DREB": 0, "TOV": 0, "FG3M": 0,
                        }
                    for stat in games[gd]:
                        games[gd][stat] += float(g.get(stat, 0) or 0)

            gp = len(games)
            if gp == 0:
                continue

            # Average per game
            avg = {k: round(sum(gm[k] for gm in games.values()) / gp, 1) for k in games[next(iter(games))]}

            # Estimate pace from box score: Poss ≈ FGA + 0.44*FTA - OREB + TOV
            est_pace = round(avg["FGA"] + 0.44 * avg["FTA"] - avg["OREB"] + avg["TOV"], 1)
            # Rough off/def rating from PPG and pace
            est_off = round(avg["PTS"] / est_pace * 100, 1) if est_pace > 0 else 114.3

            result[tid] = {
                "team_id": tid, "team_name": "", "gp": gp,
                "ppg": avg["PTS"], "fgm": avg["FGM"], "fga": avg["FGA"],
                "ftm": avg["FTM"], "fta": avg["FTA"],
                "oreb": avg["OREB"], "dreb": avg["DREB"], "tov": avg["TOV"],
                "pace": est_pace, "off_rating": est_off, "def_rating": 114.3,
                "w": 0, "l": 0,
                "opp_pts_pg": 0.0, "opp_reb_pg": 0.0, "opp_ast_pg": 0.0,
                "opp_stl_pg": 0.0, "opp_blk_pg": 0.0, "opp_tov_pg": 0.0,
                "opp_fg3m_pg": 0.0,
            }
        return result

    @staticmethod
    def _compute_usage_from_bdl_logs(
        bdl_game_logs: Dict[int, Dict[str, List[Dict]]],
    ) -> Dict[int, float]:
        """Compute per-player USG% from BDL game logs.

        Uses the simplified formula: USG ≈ (FGA + 0.44*FTA + TOV) / (MIN * 0.4167).
        """
        from app.services.nba_api_service import compute_usage_from_gamelog

        usage_map: Dict[int, float] = {}
        for _tid, logs_by_name in bdl_game_logs.items():
            for _name, player_logs in logs_by_name.items():
                if not player_logs:
                    continue
                # Get player_id from first log entry (if available via raw_data)
                pid = player_logs[0].get("PLAYER_ID", 0)
                if not pid:
                    continue
                mins = []
                for g in player_logs:
                    m = g.get("MIN", 0)
                    if isinstance(m, str) and ":" in m:
                        parts = m.split(":")
                        mins.append(float(parts[0]) + float(parts[1]) / 60)
                    else:
                        mins.append(float(m or 0))
                usg = compute_usage_from_gamelog(player_logs, mins)
                usage_map[int(pid)] = usg
        return usage_map

    # ──────────────────────────────────────────────────────────────
    # INTERNAL helpers
    # ──────────────────────────────────────────────────────────────

    @staticmethod
    def _gamelog_row_to_dict(row: NBAPlayerGameLog) -> Dict[str, Any]:
        """Convert an ORM game-log row back to NBA API dict format.

        Keys are **uppercase** to match what ``_build_player_minutes_from_gamelog``
        expects (e.g. ``MIN``, ``PTS``, ``PLUS_MINUS``).
        """
        # If we stored the raw API response, return it directly —
        # it's already in the right format.
        if row.raw_data and isinstance(row.raw_data, dict):
            return row.raw_data

        # Fallback: reconstruct from typed columns
        return {
            "PLAYER_ID": row.player_id,
            "PLAYER_NAME": row.player_name,
            "GAME_DATE": (
                row.game_date.strftime("%b %d, %Y")
                if row.game_date else ""
            ),
            "MATCHUP": row.matchup or "",
            "WL": row.wl or "",
            "MIN": row.min or 0,
            "PTS": row.pts or 0,
            "REB": row.reb or 0,
            "AST": row.ast or 0,
            "STL": row.stl or 0,
            "BLK": row.blk or 0,
            "TOV": row.tov or 0,
            "FG3M": row.fg3m or 0,
            "FGM": row.fgm or 0,
            "FGA": row.fga or 0,
            "FG_PCT": row.fg_pct or 0,
            "FG3A": row.fg3a or 0,
            "FG3_PCT": row.fg3_pct or 0,
            "FTM": row.ftm or 0,
            "FTA": row.fta or 0,
            "FT_PCT": row.ft_pct or 0,
            "OREB": row.oreb or 0,
            "DREB": row.dreb or 0,
            "PF": row.pf or 0,
            "PLUS_MINUS": row.plus_minus or 0,
            "Game_ID": row.game_id or "",
        }

    @staticmethod
    def _safe_float(val: Any) -> Optional[float]:
        """Safely convert a value to float, handling None and strings."""
        if val is None:
            return None
        try:
            if isinstance(val, str):
                val = val.strip()
                if ":" in val:
                    # Handle "MM:SS" minute format
                    parts = val.split(":")
                    return float(parts[0]) + float(parts[1]) / 60
                if not val:
                    return None
            return float(val)
        except (ValueError, TypeError):
            return None

    @classmethod
    def _run_async_with_retry(cls, coro_factory, retries: int = 1):
        """Run an async coroutine with automatic retry on transient failures.

        ``coro_factory`` is a zero-arg callable that returns a *fresh*
        coroutine each time (coroutines are single-use).  On failure the
        method waits briefly and retries with a new coroutine.
        """
        result = cls._run_async(coro_factory())
        if result is not None:
            return result

        for attempt in range(retries):
            time.sleep(0.15 * (attempt + 1))
            result = cls._run_async(coro_factory())
            if result is not None:
                logger.info(
                    "[NBACache] Retry %d/%d succeeded", attempt + 1, retries,
                )
                return result

        return None

    @classmethod
    def _run_async(cls, coro):
        """Run an async coroutine from a sync context.

        Called from ``ThreadPoolExecutor`` threads (e.g. inside
        ``build_team_rotation``).

        Strategy (in order of preference):

        1. **Main-loop dispatch** (preferred for FastAPI worker threads):
           If ``_main_loop`` is set and running, schedule the coroutine
           on the main event loop via ``run_coroutine_threadsafe`` and
           block until the result is ready.  The coroutine runs on the
           same loop that owns the SQLAlchemy async engine's connection
           pool, so sessions work correctly.

        2. **New event loop** (standalone scripts, tests, CLI):
           If no main loop is available, fall back to ``asyncio.run()``,
           which creates a temporary loop.  This only works outside
           FastAPI (the connection pool won't be shared).

        3. **Graceful None** (inside a running loop in the current thread):
           If called directly from an async context in the *same* thread
           as the running loop (e.g. accidentally calling a sync wrapper
           from an ``async def``), we can't block — return ``None`` so
           callers fall back to the live API.
        """
        import threading
        _thread = threading.current_thread().name

        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None

        # ── Case 3: We're inside a running loop in THIS thread ──
        # Can't block here — .result() would deadlock the loop.
        if current_loop is not None:
            logger.debug(
                "[NBACache._run_async] Case 3 (loop in thread) — "
                "thread=%s, returning None", _thread,
            )
            coro.close()
            return None

        # ── Case 1: Dispatch to the main FastAPI event loop ──
        main_loop = cls._main_loop
        if main_loop is not None and main_loop.is_running():
            future = asyncio.run_coroutine_threadsafe(coro, main_loop)
            try:
                result = future.result(timeout=15)
                logger.debug(
                    "[NBACache._run_async] Case 1 — thread=%s, "
                    "type=%s, len=%s", _thread,
                    type(result).__name__,
                    len(result) if hasattr(result, "__len__") else "N/A",
                )
                return result
            except Exception as e:
                logger.warning(
                    "[NBACache] Cross-thread DB query failed: %s "
                    "(thread=%s)", e, _thread,
                )
                return None

        # ── Case 2: Standalone context — use persistent background loop ──
        # Instead of asyncio.run() (which creates/destroys a loop each
        # call, breaking asyncpg's connection pool), dispatch to a
        # long-lived background loop that also auto-initializes the DB.
        loop = cls._get_standalone_loop()
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        try:
            result = future.result(timeout=15)
            logger.debug(
                "[NBACache._run_async] Case 2 (standalone) — thread=%s, "
                "type=%s, len=%s", _thread,
                type(result).__name__,
                len(result) if hasattr(result, "__len__") else "N/A",
            )
            return result
        except Exception as e:
            logger.warning(
                "[NBACache] Standalone DB query failed: %s "
                "(thread=%s)", e, _thread,
            )
            return None
