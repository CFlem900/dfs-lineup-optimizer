"""Multi-source NBA data provider with automatic fallback.

Implements ``SportDataService`` and orchestrates a 4-tier fallback chain:

    1. Module-level rotation cache (60 min TTL)  ← handled by NBAApiService
    2. PostgreSQL DB cache                        ← handled by NBAApiService
    3. BALLDONTLIE API (primary live source)       ← NEW
    4. stats.nba.com via nba_api (demoted)        ← handled by NBAApiService
    5. DK draftable salary fallback               ← handled downstream

The service wraps ``NBAApiService`` and injects BALLDONTLIE data as
prefetched roster/game-log data, reusing all existing rotation-building
logic (cutoff detection, trade detection, cache storage).

Usage::

    from app.services.nba_multi_source import NBAMultiSourceService

    svc = NBAMultiSourceService(
        nba_api=NBAApiService(),
        balldontlie=BallDontLieService(),
        db_cache=NBADataCacheService(),
    )
    rotation = svc.build_team_rotation(team_id=1610612741)
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any, Dict, List, Optional, Set, Tuple

from nba_api.stats.static import players as nba_players_static

from app.models.player import PlayerMinutes
from app.services.sport_data_service import SportDataService
from app.utils.helpers import normalize_player_name

logger = logging.getLogger(__name__)


class NBAMultiSourceService(SportDataService):
    """Resilient NBA data provider with multi-source fallback.

    Delegates to ``NBAApiService`` for all computation (rotation cutoff,
    trade detection, cache management) but injects BALLDONTLIE data
    as the primary live source before falling back to ``stats.nba.com``.
    """

    def __init__(
        self,
        nba_api,
        balldontlie=None,
        db_cache=None,
    ):
        """
        Args:
            nba_api: ``NBAApiService`` instance (always required).
            balldontlie: ``BallDontLieService`` instance (optional).
            db_cache: ``NBADataCacheService`` instance (optional).
        """
        self._nba_api = nba_api
        self._bdl = balldontlie
        self._db_cache = db_cache

        # Wire DB cache into NBAApiService (same as dependencies.py does)
        if db_cache is not None and not hasattr(nba_api, "_db_cache_set"):
            nba_api._db_cache = db_cache

    # ── SportDataService interface ────────────────────────────────────

    def get_all_teams(self) -> List[Dict[str, Any]]:
        """Return all NBA teams (offline, instant — uses nba_api static)."""
        return self._nba_api.get_all_teams()

    def find_team_by_name(self, team_name: str) -> Optional[Dict[str, Any]]:
        """Find a team by name or abbreviation (offline, instant)."""
        return self._nba_api.find_team_by_name(team_name)

    def get_today_scoreboard(self) -> List[Dict]:
        """Fetch today's games.  BDL only — no stats.nba.com fallback."""
        if self._bdl and self._bdl.is_available:
            try:
                today_str = date.today().isoformat()
                bdl_games = self._bdl.get_games(today_str)
                if bdl_games:
                    converted = self._convert_bdl_scoreboard(bdl_games)
                    logger.info(
                        f"[MultiSource] Scoreboard via BDL: {len(converted)} games"
                    )
                    return converted
            except Exception as e:
                logger.warning(f"[MultiSource] BDL scoreboard failed: {e}")

        # BDL unavailable — return empty; DK slate service provides game
        # data as the ultimate fallback downstream.
        logger.info("[MultiSource] No scoreboard source available, returning []")
        return []

    def get_scoreboard_for_date(self, game_date: str) -> List[Dict]:
        """Fetch games for a specific date.  BDL only — no stats.nba.com fallback.

        Args:
            game_date: Date in YYYY-MM-DD format.

        Returns:
            List of game header dicts in NBA API ScoreboardV2 format.
        """
        if self._bdl and self._bdl.is_available:
            try:
                bdl_games = self._bdl.get_games(game_date)
                if bdl_games:
                    converted = self._convert_bdl_scoreboard(bdl_games)
                    logger.info(
                        f"[MultiSource] Scoreboard for {game_date} via BDL: "
                        f"{len(converted)} games"
                    )
                    return converted
            except Exception as e:
                logger.warning(
                    f"[MultiSource] BDL scoreboard for {game_date} failed: {e}"
                )

        # BDL unavailable — return empty; DK slate service provides game
        # data as the ultimate fallback downstream.
        logger.info(
            f"[MultiSource] No scoreboard source for {game_date}, returning []"
        )
        return []

    def build_team_rotation(
        self,
        team_id: int,
        season: Optional[str] = None,
        max_players: int = 0,
        draftable_names: Optional[Set[str]] = None,
        cache_service=None,
        prefetched_roster=None,
        prefetched_game_logs=None,
        draftable_positions: Optional[Dict[str, str]] = None,
        draftable_salaries: Optional[Dict[str, int]] = None,
        draftable_statuses: Optional[Dict[str, str]] = None,
        db_cache_only: bool = False,
    ) -> List[PlayerMinutes]:
        """Build game-night rotation with multi-source fallback.

        Fallback chain:
          1. Rotation cache (module-level, 60 min TTL)
          2. PostgreSQL DB cache
          3. BALLDONTLIE API  (skipped when db_cache_only=True)
          4. stats.nba.com via nba_api

        When db_cache_only=True (live lineup generation), BDL is never
        called — the pipeline relies exclusively on the DB cache
        populated by the 4 AM nightly refresh.  This eliminates 429
        rate-limit errors and 150s+ backoff delays in the hot path.
        """
        from app.utils.helpers import get_current_nba_season

        season = season or get_current_nba_season()

        # If caller provided prefetched data, pass it straight through.
        # IMPORTANT: check truthiness (not just `is not None`) because
        # DB cache can return an empty dict {} for game logs when the
        # roster exists but no game-log rows have been cached yet.
        # An empty dict is "not None" but has no usable data — letting
        # it through would skip the BDL fallback and produce an empty
        # rotation that falls to the DK salary fallback (24 min baseline).
        if prefetched_roster and prefetched_game_logs:
            _prefetch_result = self._nba_api.build_team_rotation(
                team_id=team_id,
                season=season,
                max_players=max_players,
                draftable_names=draftable_names,
                prefetched_roster=prefetched_roster,
                prefetched_game_logs=prefetched_game_logs,
                draftable_positions=draftable_positions,
                draftable_salaries=draftable_salaries,
                draftable_statuses=draftable_statuses,
            )
            if _prefetch_result:
                return _prefetch_result
            # DB cache had both roster and game logs, but they produced
            # an empty rotation (likely BDL-ID roster vs NBA-API-ID logs
            # mismatch).  Fall through to BDL live fetch.
            logger.warning(
                f"[MultiSource] Prefetched DB cache produced empty rotation "
                f"for team {team_id} — falling through to BDL"
            )

        # Try BDL as the primary live data source.
        # Use exponential backoff for 429 rate-limit errors (3s → 6s → 12s).
        # Three attempts give BDL's rate limiter enough time to recover
        # when multiple teams are being fetched concurrently.
        #
        # SKIP when db_cache_only=True (live lineup generation) — the DB
        # cache from the 4 AM refresh is the sole data source.  BDL is
        # still used by background refresh and admin endpoints.
        bdl_roster = None
        bdl_logs = None
        _BDL_MAX_ATTEMPTS = 3
        _BDL_BASE_DELAY = 3.0  # seconds
        _bdl_gate = self._bdl and self._bdl.is_available and not db_cache_only
        if _bdl_gate:
            for _bdl_attempt in range(_BDL_MAX_ATTEMPTS):
                try:
                    bdl_roster, bdl_logs = self._fetch_bdl_prefetch(
                        team_id, season,
                        draftable_names=draftable_names,
                    )
                    if not (bdl_roster and bdl_logs):
                        logger.warning(
                            f"[MultiSource] BDL returned incomplete data "
                            f"for team {team_id} (attempt "
                            f"{_bdl_attempt + 1}/{_BDL_MAX_ATTEMPTS}): "
                            f"roster={'yes' if bdl_roster else 'no'}, "
                            f"logs={'yes' if bdl_logs else 'no'}"
                        )
                    if bdl_roster and bdl_logs:
                        logger.info(
                            f"[MultiSource] BDL data ready for team {team_id}: "
                            f"{len(bdl_roster)} players, "
                            f"{sum(len(v) for v in bdl_logs.values())} game rows"
                        )
                        break
                except Exception as e:
                    logger.warning(
                        f"[MultiSource] BDL fetch failed for team {team_id} "
                        f"(attempt {_bdl_attempt + 1}/{_BDL_MAX_ATTEMPTS}): {e}"
                    )
                    bdl_roster, bdl_logs = None, None
                    if _bdl_attempt < _BDL_MAX_ATTEMPTS - 1:
                        import time as _time
                        _delay = _BDL_BASE_DELAY * (2 ** _bdl_attempt)
                        _time.sleep(_delay)

        if bdl_roster and bdl_logs:
            # BDL succeeded — pass as prefetched data (skips live NBA API)
            return self._nba_api.build_team_rotation(
                team_id=team_id,
                season=season,
                max_players=max_players,
                draftable_names=draftable_names,
                prefetched_roster=bdl_roster,
                prefetched_game_logs=bdl_logs,
                draftable_positions=draftable_positions,
                draftable_salaries=draftable_salaries,
                draftable_statuses=draftable_statuses,
            )

        # BDL failed — try DB cache only (never fall back to live NBA API)
        effective_cache = cache_service or self._db_cache
        if effective_cache:
            logger.info(
                f"[MultiSource] BDL unavailable for team {team_id}, "
                f"falling back to DB cache"
            )
            result = self._nba_api.build_team_rotation(
                team_id=team_id,
                season=season,
                max_players=max_players,
                draftable_names=draftable_names,
                cache_service=effective_cache,
                draftable_positions=draftable_positions,
                draftable_salaries=draftable_salaries,
                draftable_statuses=draftable_statuses,
            )
            if result:
                logger.info(
                    f"[MultiSource] DB cache hit for team {team_id}: "
                    f"{len(result)} players"
                )
            else:
                logger.warning(
                    f"[MultiSource] DB cache also empty for team {team_id}"
                )
            return result

        # Both BDL and DB cache unavailable — return empty rotation.
        # DK salary fallback projections handle this downstream.
        logger.warning(
            f"[MultiSource] All data sources unavailable for team {team_id}. "
            f"Returning empty rotation."
        )
        return []

    # ── Proxy methods for NBAApiService features ──────────────────────

    def get_team_roster(self, team_id: int, season: str = None):
        """Proxy to NBAApiService.get_team_roster()."""
        return self._nba_api.get_team_roster(team_id, season)

    def get_player_game_log(self, player_id: int, season: str = None, last_n: int = 10):
        """Proxy to NBAApiService.get_player_game_log()."""
        return self._nba_api.get_player_game_log(player_id, season, last_n)

    def build_player_minutes(self, player_id, player_name, position, team_id, birth_date=None):
        """Proxy to NBAApiService.build_player_minutes()."""
        return self._nba_api.build_player_minutes(
            player_id, player_name, position, team_id, birth_date
        )

    # ── Internal: BDL → NBA prefetch conversion ──────────────────────

    def _fetch_bdl_prefetch(
        self,
        team_id: int,
        season: str,
        draftable_names: Optional[Set[str]] = None,
    ) -> Tuple[Optional[List[Dict]], Optional[Dict[int, List[Dict]]]]:
        """Fetch data from BDL and convert to NBAApiService prefetch format.

        Two paths:

        **DK-driven (when ``draftable_names`` provided)**:
            Resolves DK player names → BDL IDs via search (1 API call per
            unique last name, cached), then batch-fetches stats for only
            those players.  Reduces API calls from ~100-200 to ~20-80
            (warm cache: ~6-12).

        **Team-roster (no draftable names — background refresh)**:
            Falls back to ``get_team_game_logs_bulk()`` with capped roster
            pages (max 2 pages = 200 players vs the old 10 pages = 1000).

        Returns:
            ``(roster, game_logs)`` in the format expected by
            ``NBAApiService.build_team_rotation(prefetched_roster=...,
            prefetched_game_logs=...)``

            Returns ``(None, None)`` if BDL fetch fails or returns no data.
        """
        if draftable_names:
            return self._fetch_bdl_dk_driven(
                team_id, season, draftable_names
            )
        return self._fetch_bdl_team_roster(team_id, season)

    def _fetch_bdl_dk_driven(
        self,
        team_id: int,
        season: str,
        draftable_names: Set[str],
    ) -> Tuple[Optional[List[Dict]], Optional[Dict[int, List[Dict]]]]:
        """DK-driven BDL fetch: resolve DK names → BDL IDs → stats.

        Instead of fetching an all-time team roster (200+ players) and
        querying stats for all of them, this resolves only the ~10-15
        DK-listed players by name search, then batch-fetches their stats.
        """
        # Derive team abbreviation from team_id for the BDL search
        team_abbr = self._resolve_team_abbr(team_id)

        # Build (name, team_abbr) tuples for the DK-driven lookup
        dk_players = [
            (name, team_abbr) for name in draftable_names
        ]

        logger.info(
            f"[MultiSource] DK-driven BDL lookup for team {team_id} "
            f"({team_abbr}): {len(dk_players)} DK players"
        )

        bdl_roster, bdl_logs_by_name = self._bdl.get_dk_driven_game_logs(
            dk_players, season
        )
        if not bdl_roster:
            return None, None

        # Map BDL player names → NBA player IDs using offline static data
        nba_by_name = self._build_nba_name_lookup()

        nba_roster: List[Dict] = []
        game_logs_by_id: Dict[int, List[Dict]] = {}

        for bdl_player in bdl_roster:
            bdl_name = f"{bdl_player['first_name']} {bdl_player['last_name']}"
            bdl_norm = normalize_player_name(bdl_name)

            nba_match = nba_by_name.get(bdl_norm)
            if not nba_match:
                for nba_norm, nba_p in nba_by_name.items():
                    if bdl_norm in nba_norm or nba_norm in bdl_norm:
                        nba_match = nba_p
                        break

            if not nba_match:
                logger.debug(
                    f"[MultiSource] No NBA ID match for BDL player: {bdl_name}"
                )
                continue

            nba_pid = nba_match["id"]
            position = self._bdl.get_bdl_position(bdl_player)

            nba_roster.append({
                "PLAYER_ID": nba_pid,
                "PLAYER": nba_match["full_name"],
                "POSITION": position,
                "BIRTH_DATE": None,
            })

            player_logs = bdl_logs_by_name.get(bdl_name, [])
            if player_logs:
                game_logs_by_id[nba_pid] = player_logs

        if not nba_roster:
            logger.warning(
                f"[MultiSource] DK-driven BDL for team {team_id} "
                f"mapped 0 players to NBA IDs"
            )
            return None, None

        logger.info(
            f"[MultiSource] DK-driven BDL→NBA mapping: "
            f"{len(nba_roster)}/{len(bdl_roster)} players matched, "
            f"{len(game_logs_by_id)} with game logs"
        )

        return nba_roster, game_logs_by_id

    def _fetch_bdl_team_roster(
        self,
        team_id: int,
        season: str,
    ) -> Tuple[Optional[List[Dict]], Optional[Dict[int, List[Dict]]]]:
        """Team-roster BDL fetch (used by background refresh, no DK data).

        Uses ``get_team_game_logs_bulk()`` with capped roster pages
        (max 2 = 200 players, down from 10 = 1000).
        """
        bdl_roster, bdl_logs_by_name = self._bdl.get_team_game_logs_bulk(
            team_id, season
        )
        if not bdl_roster:
            return None, None

        nba_by_name = self._build_nba_name_lookup()

        nba_roster: List[Dict] = []
        game_logs_by_id: Dict[int, List[Dict]] = {}

        for bdl_player in bdl_roster:
            bdl_name = f"{bdl_player['first_name']} {bdl_player['last_name']}"
            bdl_norm = normalize_player_name(bdl_name)

            nba_match = nba_by_name.get(bdl_norm)
            if not nba_match:
                for nba_norm, nba_p in nba_by_name.items():
                    if bdl_norm in nba_norm or nba_norm in bdl_norm:
                        nba_match = nba_p
                        break

            if not nba_match:
                logger.debug(
                    f"[MultiSource] No NBA ID match for BDL player: {bdl_name}"
                )
                continue

            nba_pid = nba_match["id"]
            position = self._bdl.get_bdl_position(bdl_player)

            nba_roster.append({
                "PLAYER_ID": nba_pid,
                "PLAYER": nba_match["full_name"],
                "POSITION": position,
                "BIRTH_DATE": None,
            })

            player_logs = bdl_logs_by_name.get(bdl_name, [])
            if player_logs:
                game_logs_by_id[nba_pid] = player_logs

        if not nba_roster:
            logger.warning(
                f"[MultiSource] BDL roster for team {team_id} mapped 0 players"
            )
            return None, None

        logger.info(
            f"[MultiSource] BDL→NBA mapping: {len(nba_roster)}/{len(bdl_roster)} "
            f"players matched, {len(game_logs_by_id)} with game logs"
        )

        return nba_roster, game_logs_by_id

    def _build_nba_name_lookup(self) -> Dict[str, Dict]:
        """Build a normalized-name → NBA player dict lookup from static data."""
        all_nba_players = nba_players_static.get_players()
        nba_by_name: Dict[str, Dict] = {}
        for p in all_nba_players:
            nba_by_name[normalize_player_name(p["full_name"])] = p
        return nba_by_name

    def _resolve_team_abbr(self, team_id: int) -> str:
        """Resolve an NBA team ID to its abbreviation."""
        all_teams = self._nba_api.get_all_teams()
        for t in all_teams:
            if t["id"] == team_id:
                return t["abbreviation"].upper()
        return ""

    # ── Internal: BDL scoreboard conversion ───────────────────────────

    @staticmethod
    def _convert_bdl_scoreboard(bdl_games: List[Dict]) -> List[Dict]:
        """Convert BDL games to the format expected by game_service.

        NBA API ScoreboardV2 ``GameHeader`` format:
            ``{"GAME_ID": str, "HOME_TEAM_ID": int, "VISITOR_TEAM_ID": int,
            "GAME_STATUS_TEXT": str, "GAME_STATUS_ID": int,
            "GAME_SEQUENCE": int, ...}``
        """
        converted = []
        for seq, g in enumerate(bdl_games, start=1):
            home = g.get("home_team", {})
            visitor = g.get("visitor_team", {})

            # Map BDL team IDs to NBA team IDs if possible
            home_nba_id = _bdl_team_to_nba_id(home.get("id"))
            visitor_nba_id = _bdl_team_to_nba_id(visitor.get("id"))

            status = g.get("status", "")
            if status == "Final":
                status_text = "Final"
                status_id = 3
            elif g.get("period", 0) > 0 and status != "Final":
                status_text = f"Q{g['period']} {g.get('time', '')}"
                status_id = 2
            else:
                status_text = g.get("datetime", "")
                status_id = 1

            converted.append({
                "GAME_ID": str(g.get("id", "")),
                "HOME_TEAM_ID": home_nba_id or home.get("id"),
                "VISITOR_TEAM_ID": visitor_nba_id or visitor.get("id"),
                "GAME_STATUS_TEXT": status_text,
                "GAME_STATUS_ID": status_id,
                "GAME_SEQUENCE": seq,
                "HOME_TEAM_SCORE": g.get("home_team_score", 0),
                "VISITOR_TEAM_SCORE": g.get("visitor_team_score", 0),
                "GAME_DATE_EST": g.get("date", ""),
                # Include names for downstream consumers
                "HOME_TEAM_NAME": home.get("full_name", ""),
                "VISITOR_TEAM_NAME": visitor.get("full_name", ""),
                "HOME_TEAM_ABBREVIATION": home.get("abbreviation", ""),
                "VISITOR_TEAM_ABBREVIATION": visitor.get("abbreviation", ""),
                "_source": "balldontlie",
            })

        return converted

    # ── Diagnostics ───────────────────────────────────────────────────

    def get_source_status(self) -> Dict[str, Any]:
        """Return diagnostic info about data source availability."""
        return {
            "balldontlie_available": (
                self._bdl.is_available if self._bdl else False
            ),
            "db_cache_available": self._db_cache is not None,
            "nba_api_live_disabled": True,  # stats.nba.com removed from hot path
        }


def _bdl_team_to_nba_id(bdl_team_id: Optional[int]) -> Optional[int]:
    """Best-effort BDL team ID → NBA team ID mapping.

    Uses the hardcoded reverse mapping.  Returns None if not found.
    """
    if bdl_team_id is None:
        return None

    # Reverse of BallDontLieService._load_hardcoded_mapping
    _BDL_TO_NBA = {
        1: 1610612737,   # ATL
        2: 1610612738,   # BOS
        3: 1610612751,   # BKN
        4: 1610612766,   # CHA
        5: 1610612741,   # CHI
        6: 1610612739,   # CLE
        7: 1610612742,   # DAL
        8: 1610612743,   # DEN
        9: 1610612765,   # DET
        10: 1610612744,  # GSW
        11: 1610612745,  # HOU
        12: 1610612754,  # IND
        13: 1610612746,  # LAC
        14: 1610612747,  # LAL
        15: 1610612763,  # MEM
        16: 1610612748,  # MIA
        17: 1610612749,  # MIL
        18: 1610612750,  # MIN
        19: 1610612740,  # NOP
        20: 1610612752,  # NYK
        21: 1610612760,  # OKC
        22: 1610612753,  # ORL
        23: 1610612755,  # PHI
        24: 1610612756,  # PHX
        25: 1610612757,  # POR
        26: 1610612758,  # SAC
        27: 1610612759,  # SAS
        28: 1610612761,  # TOR
        29: 1610612762,  # UTA
        30: 1610612764,  # WAS
    }
    return _BDL_TO_NBA.get(bdl_team_id)
