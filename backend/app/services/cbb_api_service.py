"""CBB data service using ESPN free API + CBBpy.

Implements the :class:`SportDataService` abstract interface, providing
team data, player rotations (with per-minute stat rates), and
scoreboard data for NCAA Division 1 men's basketball.

Data sources:
    - ESPN public API: teams, rosters, scoreboard
    - CBBpy package:   game box scores, season game logs

Usage:
    from app.services.cbb_api_service import CBBApiService
    svc = CBBApiService()
    teams = svc.get_all_teams()
    rotation = svc.build_team_rotation(team_id=150, season="2025")
"""

import logging
import threading
import time
from typing import Dict, List, Optional, Set, Tuple

import httpx

from app.config.constants import (
    CBB_POSITION_PRIOR_RATES,
    CBB_ROTATION_SIZE_DEFAULT,
)
from app.models.player import PlayerMinutes
from app.services.cbb_teams import CBBTeamRegistry
from app.services.http_resilience import resilient_get, APIGroup
from app.services.sport_data_service import SportDataService

logger = logging.getLogger(__name__)

# ESPN API base URLs
_ESPN_BASE = (
    "https://site.api.espn.com/apis/site/v2/sports/basketball/"
    "mens-college-basketball"
)

# Module-level rotation cache
_cbb_rotation_cache: Dict[int, Tuple[float, List[PlayerMinutes]]] = {}
_CBB_ROTATION_CACHE_TTL = 3600  # 1 hour

# Module-level CBBpy game-log cache — prevents duplicate CBBpy scraping
# when prewarm and stream requests both build the same pool.
# Key: (team_name_lower, season) → (timestamp, game_logs_dict)
_cbbpy_game_log_cache: Dict[Tuple[str, str], Tuple[float, Dict[str, List[Dict]]]] = {}
_CBBPY_GAME_LOG_CACHE_TTL = 3600  # 1 hour (game logs don't change intra-day)
_cbbpy_game_log_lock = threading.Lock()

# Tracks whether we've already patched the CBBpy CSV for the current season
_cbbpy_csv_patched: bool = False


class CBBApiService(SportDataService):
    """NCAA D1 basketball data provider (ESPN + CBBpy).

    Mirrors the interface of :class:`NBAApiService` so the lineup
    optimizer, rotation engine, and simulation engine can work with
    either sport transparently.
    """

    def __init__(self):
        self._team_registry = CBBTeamRegistry()

    # ------------------------------------------------------------------
    # SportDataService interface
    # ------------------------------------------------------------------

    def get_all_teams(self) -> List[Dict]:
        """Return all D1 teams."""
        return self._team_registry.get_all_teams()

    def find_team_by_name(self, team_name: str) -> Optional[Dict]:
        """Find a team by name, abbreviation, or nickname."""
        return self._team_registry.find_team_by_name(team_name)

    def get_today_scoreboard(self) -> List[Dict]:
        """Fetch today's CBB scoreboard from ESPN.

        Uses ``groups=50`` to request ALL Division 1 games.
        """
        try:
            url = f"{_ESPN_BASE}/scoreboard?groups=50&limit=500"
            resp = resilient_get(url, group=APIGroup.ESPN_CBB)
            data = resp.json()

            games = []
            for event in data.get("events", []):
                competition = event.get("competitions", [{}])[0]
                competitors = competition.get("competitors", [])
                if len(competitors) < 2:
                    continue

                home = away = None
                for c in competitors:
                    if c.get("homeAway") == "home":
                        home = c
                    else:
                        away = c
                if not home or not away:
                    continue

                status_obj = event.get("status", {})
                status_type = status_obj.get("type", {}).get("name", "STATUS_SCHEDULED")
                status_map = {
                    "STATUS_SCHEDULED": "Scheduled",
                    "STATUS_IN_PROGRESS": "In Progress",
                    "STATUS_HALFTIME": "In Progress",
                    "STATUS_FINAL": "Final",
                    "STATUS_POSTPONED": "Postponed",
                }

                games.append({
                    "game_id": event.get("id", ""),
                    "home_team": {
                        "id": int(home["team"]["id"]),
                        "name": home["team"].get("displayName", ""),
                        "abbreviation": home["team"].get("abbreviation", ""),
                        "score": home.get("score", "0"),
                    },
                    "away_team": {
                        "id": int(away["team"]["id"]),
                        "name": away["team"].get("displayName", ""),
                        "abbreviation": away["team"].get("abbreviation", ""),
                        "score": away.get("score", "0"),
                    },
                    "status": status_map.get(status_type, "Scheduled"),
                    "start_time": event.get("date", ""),
                })

            return games
        except Exception as e:
            logger.error(f"Failed to fetch CBB scoreboard: {e}")
            return []

    def build_team_rotation(
        self,
        team_id: int,
        season: Optional[str] = None,
        max_players: int = 0,
        draftable_names: Optional[Set[str]] = None,
        **kwargs,
    ) -> List[PlayerMinutes]:
        """Build game-night rotation for a CBB team.

        Uses ESPN roster endpoint + CBBpy game logs to build per-player
        minute averages and per-minute stat rates, with Bayesian
        shrinkage toward CBB position priors for small sample sizes.

        Parameters
        ----------
        team_id : int
            ESPN team ID.
        season : str, optional
            CBB season year (e.g. "2025" for the 2024-25 season).
            Defaults to current season.
        max_players : int
            If > 0, limit rotation to this many players. 0 = auto (~9).
        draftable_names : set[str], optional
            When provided, only include players whose names match this
            set (for DFS salary pool alignment).
        """
        # --- Check rotation cache ---
        now = time.time()
        if team_id in _cbb_rotation_cache:
            cached_at, cached_rotation = _cbb_rotation_cache[team_id]
            if now - cached_at < _CBB_ROTATION_CACHE_TTL:
                logger.info(
                    f"CBB rotation cache hit for team {team_id} "
                    f"({len(cached_rotation)} players)"
                )
                return cached_rotation

        season = season or self._get_current_cbb_season()

        # Step 1: Fetch roster from ESPN
        roster = self._get_espn_roster(team_id)
        if not roster:
            logger.warning(f"Empty roster for CBB team {team_id}")
            return []

        # Step 2: Filter to DK-relevant players if provided
        if draftable_names:
            from app.services.dk_draftables_service import _normalize_name

            normalized_dk = {_normalize_name(n) for n in draftable_names}
            lowered_dk = {n.lower() for n in draftable_names}
            # Pre-split normalized DK names for fallback matching
            dk_parts_list = [
                dk_norm.split() for dk_norm in normalized_dk
            ]

            filtered = []
            for p in roster:
                pname = p.get("name", "")
                norm_p = _normalize_name(pname)

                # Try exact normalized match first
                if norm_p in normalized_dk:
                    filtered.append(p)
                    continue

                # Fallback: last-name + first-3-chars (handles Jr/Sr, P.J. etc.)
                parts_p = norm_p.split()
                if len(parts_p) >= 2:
                    matched = False
                    for parts_dk in dk_parts_list:
                        if (
                            len(parts_dk) >= 2
                            and parts_p[-1] == parts_dk[-1]
                            and len(parts_p[0]) >= 3
                            and len(parts_dk[0]) >= 3
                            and parts_p[0][:3] == parts_dk[0][:3]
                        ):
                            matched = True
                            break
                    if matched:
                        filtered.append(p)
                        continue

                # Last resort: substring matching (catches edge cases)
                plow = pname.lower()
                if any(dk in plow or plow in dk for dk in lowered_dk):
                    filtered.append(p)

            skipped = len(roster) - len(filtered)
            if skipped > 0:
                logger.info(
                    f"Skipping {skipped} CBB roster players not in DK pool"
                )
            roster = filtered

        # Step 3: Fetch game logs — DB cache first, CBBpy live fallback
        team_info = self._team_registry.find_team_by_id(team_id)
        team_name_for_cbbpy = (
            team_info["full_name"] if team_info else str(team_id)
        )
        db_cache_only = kwargs.get("db_cache_only", False)

        game_logs = self._fetch_game_logs_cached(
            team_name_for_cbbpy, team_id, season,
            db_cache_only=db_cache_only,
        )

        # Step 4: Build PlayerMinutes objects
        all_players: List[PlayerMinutes] = []
        for player in roster:
            pm = self._build_player_minutes(
                player, game_logs, team_id, season
            )
            if pm and pm.season_avg > 0:
                all_players.append(pm)

        all_players.sort(key=lambda x: x.season_avg, reverse=True)

        if not all_players:
            logger.warning(f"No valid CBB players for team {team_id}")
            return []

        # Step 5: Find rotation cutoff (same gap-detection as NBA)
        effective_max = min(max_players, 12) if max_players > 0 else 12
        best_cut = self._find_rotation_cutoff(all_players, effective_max)

        rotation = all_players  # Keep all for pool building

        core = best_cut
        deep = len(all_players) - best_cut
        logger.info(
            f"CBB rotation for team {team_id}: {core} core + "
            f"{deep} deep bench, "
            f"total avg_min={sum(p.season_avg for p in rotation):.1f}"
        )

        # Cache
        _cbb_rotation_cache[team_id] = (time.time(), rotation)
        return rotation

    # ------------------------------------------------------------------
    # ESPN data fetching
    # ------------------------------------------------------------------

    @staticmethod
    def _get_espn_roster(team_id: int) -> List[Dict]:
        """Fetch team roster from ESPN public API."""
        url = f"{_ESPN_BASE}/teams/{team_id}/roster"
        try:
            resp = resilient_get(url, group=APIGroup.ESPN_CBB)
            data = resp.json()

            players = []
            for athlete in data.get("athletes", []):
                player_id = int(athlete.get("id", 0))
                if not player_id:
                    continue

                # Parse position
                position = athlete.get("position", {}).get("abbreviation", "G")
                # Normalize CBB positions to standard categories
                position = _normalize_cbb_position(position)

                players.append({
                    "id": player_id,
                    "name": athlete.get("displayName", ""),
                    "position": position,
                    "jersey": athlete.get("jersey", ""),
                    "class_year": athlete.get("experience", {}).get(
                        "displayValue", ""
                    ),
                })

            return players
        except Exception as e:
            logger.error(f"Failed to fetch ESPN roster for team {team_id}: {e}")
            return []

    @staticmethod
    def _get_current_cbb_season() -> str:
        """Return the current CBB season identifier.

        CBB seasons span two calendar years.  The "2025" season is
        the 2024-25 academic year.  If we're before July, the current
        season started last year; otherwise it starts this year.
        """
        from datetime import datetime
        now = datetime.now()
        if now.month >= 7:
            return str(now.year + 1)
        return str(now.year)

    # ------------------------------------------------------------------
    # CBBpy game log integration
    # ------------------------------------------------------------------

    @staticmethod
    def _ensure_cbbpy_season(season_int: int) -> None:
        """Patch CBBpy's bundled team-map CSV if it lacks the requested season.

        CBBpy ships a ``mens_team_map.csv`` that maps team names to ESPN IDs
        per season.  New seasons aren't included until the package is
        updated — but team IDs / names rarely change year-to-year, so we
        clone the most recent season's rows with an updated season column.

        This is a no-op if the season already exists in the CSV.
        """
        global _cbbpy_csv_patched
        if _cbbpy_csv_patched:
            return

        try:
            import pandas as pd
            import cbbpy
            import os

            csv_path = os.path.join(
                os.path.dirname(cbbpy.__file__),
                "utils", "mens_team_map.csv",
            )
            if not os.path.exists(csv_path):
                return

            df = pd.read_csv(csv_path)
            if season_int in df["season"].values:
                _cbbpy_csv_patched = True
                return

            # Find the latest season in the CSV and clone those rows
            max_season = int(df["season"].max())
            clone = df[df["season"] == max_season].copy()
            clone["season"] = season_int
            df = pd.concat([df, clone], ignore_index=True)
            df.to_csv(csv_path, index=False)
            _cbbpy_csv_patched = True
            logger.info(
                f"Patched CBBpy team map: cloned {len(clone)} teams "
                f"from season {max_season} → {season_int}"
            )
        except Exception as exc:
            logger.warning(f"Failed to patch CBBpy CSV: {exc}")
            _cbbpy_csv_patched = True  # Don't retry on every call

    @staticmethod
    def _cbbpy_fetch_box(ms, pd, team_name: str, season_int: int):
        """Low-level CBBpy call that returns a DataFrame or None.

        Uses the throttled wrapper to limit joblib parallelism and
        serialise CBBpy calls across the entire application.
        """
        from app.services.cbbpy_throttle import cbbpy_get_games_team

        result = cbbpy_get_games_team(
            team_name, season_int, info=False, box=True, pbp=False,
        )
        if result is None:
            return None
        if isinstance(result, tuple):
            for item in result:
                if isinstance(item, pd.DataFrame) and not item.empty:
                    return item
        elif isinstance(result, pd.DataFrame) and not result.empty:
            return result
        return None

    @staticmethod
    def _fetch_game_logs_cached(
        team_name: str,
        team_id: int,
        season: str,
        db_cache_only: bool = False,
    ) -> Dict[str, List[Dict]]:
        """Fetch game logs with DB cache → CBBpy live fallback.

        When ``db_cache_only=True`` (live lineup path), NEVER calls
        CBBpy — returns DB data or empty dict with position priors.
        """
        # ── Try DB cache first ────────────────────────────────────
        try:
            from app.services.cbb_data_cache_service import CBBDataCacheService
            cache_svc = CBBDataCacheService()
            db_logs = cache_svc.get_team_game_logs_sync(team_name, season)
            if db_logs:
                logger.info(
                    f"[CBB] DB cache hit for {team_name}: "
                    f"{len(db_logs)} players, "
                    f"{sum(len(v) for v in db_logs.values())} total rows"
                )
                return db_logs
            # Also try by team_id if name lookup failed
            if team_id:
                db_logs = cache_svc.get_team_game_logs_by_id_sync(
                    team_id, season
                )
                if db_logs:
                    logger.info(
                        f"[CBB] DB cache hit (by ID {team_id}): "
                        f"{len(db_logs)} players"
                    )
                    return db_logs
        except Exception as e:
            logger.warning(f"[CBB] DB cache read failed for {team_name}: {e}")

        if db_cache_only:
            logger.info(
                f"[CBB] DB cache miss for {team_name}, "
                f"db_cache_only=True — returning empty (priors will apply)"
            )
            return {}

        # ── Fallback: live CBBpy scrape ───────────────────────────
        logger.info(
            f"[CBB] DB cache miss for {team_name}, "
            f"falling back to live CBBpy scrape"
        )
        return CBBApiService._fetch_cbbpy_game_logs(team_name, season)

    @staticmethod
    def _fetch_cbbpy_game_logs(
        team_name: str, season: str
    ) -> Dict[str, List[Dict]]:
        """Fetch per-team season game logs via CBBpy.

        Uses ``get_games_team(team, season)`` to fetch box scores for a
        single team's games.

        Returns a dict mapping player_name (lowercase) to a list of
        game stat dicts.

        **Important:** CBBpy returns box scores for *both* teams in each
        game.  We filter by the ``team`` column to keep only the
        requested team's players and exclude the ``"TEAM"`` totals rows.

        Results are cached in-memory for 1 hour to prevent duplicate
        CBBpy scraping when multiple requests hit the same team.
        """
        # ── Check game-log cache first ───────────────────────────────
        cache_key = (team_name.lower(), season)
        now = time.time()
        with _cbbpy_game_log_lock:
            if cache_key in _cbbpy_game_log_cache:
                cached_at, cached_logs = _cbbpy_game_log_cache[cache_key]
                if now - cached_at < _CBBPY_GAME_LOG_CACHE_TTL:
                    logger.info(
                        f"CBBpy game-log cache hit for {team_name} "
                        f"({len(cached_logs)} players, "
                        f"age={now - cached_at:.0f}s)"
                    )
                    # Return a deep copy so callers can mutate (del matched keys)
                    return {k: list(v) for k, v in cached_logs.items()}

        try:
            from cbbpy import mens_scraper as ms
            import pandas as pd

            season_int = int(season)

            # Ensure CBBpy's team map CSV has the requested season
            CBBApiService._ensure_cbbpy_season(season_int)

            # Fetch box scores — try current season, fall back to previous
            # if CBBpy has no data (e.g. early in a new season).
            box_df = CBBApiService._cbbpy_fetch_box(
                ms, pd, team_name, season_int
            )
            if box_df is None and season_int > 2020:
                logger.info(
                    f"CBBpy: no data for {team_name} season {season_int}, "
                    f"trying {season_int - 1}"
                )
                CBBApiService._ensure_cbbpy_season(season_int - 1)
                box_df = CBBApiService._cbbpy_fetch_box(
                    ms, pd, team_name, season_int - 1
                )
            if box_df is None:
                # Cache the empty result so we don't re-scrape failures
                with _cbbpy_game_log_lock:
                    _cbbpy_game_log_cache[cache_key] = (time.time(), {})
                return {}

            # ── Filter to requested team only ────────────────────────
            # CBBpy box scores contain rows for BOTH teams in each game.
            # The ``team`` column (lowercase) holds the team name string.
            # Without filtering, opponent players leak into the game logs
            # and corrupt per-minute stat rates.
            team_col = "team" if "team" in box_df.columns else None
            if team_col:
                team_lower = team_name.lower()
                box_df = box_df[
                    box_df[team_col].astype(str).str.lower().str.contains(
                        team_lower, na=False
                    )
                ]

            total_rows = len(box_df)

            # ── CBBpy column names are all lowercase ─────────────────
            # Columns: player, team, min, pts, reb, ast, stl, blk, to,
            #          3pm, 3pa, fgm, fga, ftm, fta, oreb, dreb, pf
            # Group by player
            player_logs: Dict[str, List[Dict]] = {}
            for _, row in box_df.iterrows():
                pname = str(row.get("player", "")).strip().lower()
                if not pname or pname == "nan" or pname == "team":
                    # Skip empty names and "TEAM" totals rows
                    continue

                log_entry = {
                    "MIN": _safe_float(row.get("min", 0)),
                    "PTS": _safe_float(row.get("pts", 0)),
                    "REB": _safe_float(row.get("reb", 0)),
                    "AST": _safe_float(row.get("ast", 0)),
                    "STL": _safe_float(row.get("stl", 0)),
                    "BLK": _safe_float(row.get("blk", 0)),
                    "TO": _safe_float(row.get("to", 0)),
                    "FG3M": _safe_float(row.get("3pm", 0)),
                }
                player_logs.setdefault(pname, []).append(log_entry)

            logger.info(
                f"CBBpy: {len(player_logs)} players from {team_name} "
                f"(filtered from {total_rows} rows), season {season}"
            )

            # ── Cache the result ─────────────────────────────────────
            with _cbbpy_game_log_lock:
                _cbbpy_game_log_cache[cache_key] = (time.time(), player_logs)

            # Return a deep copy so callers can mutate (del matched keys)
            return {k: list(v) for k, v in player_logs.items()}

        except ImportError:
            logger.warning("CBBpy not installed; falling back to ESPN-only data")
            return {}
        except Exception as e:
            logger.error(f"CBBpy game log fetch failed for {team_name}: {e}")
            # Cache failures too (short TTL to allow retry) to prevent
            # hammering CBBpy with failing requests
            with _cbbpy_game_log_lock:
                _cbbpy_game_log_cache[cache_key] = (time.time(), {})
            return {}

    # ------------------------------------------------------------------
    # PlayerMinutes construction
    # ------------------------------------------------------------------

    def _build_player_minutes(
        self,
        player: Dict,
        game_logs: Dict[str, List[Dict]],
        team_id: int,
        season: str,
    ) -> Optional[PlayerMinutes]:
        """Build a PlayerMinutes from ESPN roster + CBBpy game logs."""
        player_name = player.get("name", "")
        player_id = player.get("id", 0)
        position = player.get("position", "G")

        # Match ESPN roster name to CBBpy log name.
        #
        # CBBpy abbreviates first names: "Cam. Boozer", "Cay. Boozer",
        # "C. Foster".  ESPN uses full names: "Cameron Boozer",
        # "Cayden Boozer", "Caleb Foster".
        #
        # Strategy (in order of specificity):
        #   1. First-initial + last-name match (handles "Cam." → "Cameron")
        #   2. Full substring match (handles exact or contains)
        #   3. Last-name only (loose fallback)
        #
        # A matched log_name is removed from candidates to prevent
        # two ESPN players from matching the same CBBpy entry.
        pname_lower = player_name.lower()
        parts = pname_lower.split()
        first_name = parts[0] if parts else ""
        last_name = parts[-1] if parts else ""
        first_initial = first_name[0] if first_name else ""

        logs: List[Dict] = []
        matched_key: Optional[str] = None

        # Pass 1: First-initial + last-name match
        # "Cameron Boozer" → first_initial='c', last_name='boozer'
        # matches "cam. boozer" (starts with 'c', ends with 'boozer')
        if first_initial and last_name:
            for log_name, log_entries in game_logs.items():
                log_parts = log_name.replace(".", "").split()
                log_first = log_parts[0] if log_parts else ""
                log_last = log_parts[-1] if len(log_parts) > 1 else ""
                if (
                    log_last == last_name
                    and log_first
                    and log_first[0] == first_initial
                ):
                    # Disambiguate: if the CBBpy name has >1 char before
                    # the period (e.g. "cam"), check that the ESPN first
                    # name starts with those chars.
                    log_prefix = log_first.rstrip(".")
                    if first_name.startswith(log_prefix):
                        logs = log_entries
                        matched_key = log_name
                        break

        # Pass 2: Full substring match
        if not logs:
            for log_name, log_entries in game_logs.items():
                if log_name in pname_lower or pname_lower in log_name:
                    logs = log_entries
                    matched_key = log_name
                    break

        # Pass 3: Last-name only (loose fallback)
        if not logs and last_name:
            for log_name, log_entries in game_logs.items():
                if last_name in log_name:
                    logs = log_entries
                    matched_key = log_name
                    break

        # Remove matched entry to prevent a second player from reusing it
        # (e.g. Cameron Boozer and Cayden Boozer both matching "cam. boozer")
        if matched_key and matched_key in game_logs:
            del game_logs[matched_key]

        if not logs:
            # Fallback: estimate minutes from position priors when no
            # game-log data is available (CBBpy down or not installed).
            # This allows the player pool to still populate with
            # reasonable defaults.
            priors = CBB_POSITION_PRIOR_RATES.get(
                position, CBB_POSITION_PRIOR_RATES.get("G", {})
            )
            # Estimate ~20 min for bench, ~30 min for starters.
            # Without game logs we can't distinguish, so use a
            # moderate default that the rotation engine will adjust.
            default_min = 22.0
            return PlayerMinutes(
                player_id=player_id,
                player_name=player_name,
                position=position,
                team_id=team_id,
                minutes_last_5=[default_min],
                minutes_last_10=[default_min],
                season_avg=default_min,
                usage_rate=0.20,
                age=None,
                pts_per_min=priors.get("PTS", 0.40),
                reb_per_min=priors.get("REB", 0.15),
                ast_per_min=priors.get("AST", 0.10),
                stl_per_min=priors.get("STL", 0.03),
                blk_per_min=priors.get("BLK", 0.02),
                tov_per_min=priors.get("TOV", 0.05),
                fg3m_per_min=priors.get("FG3M", 0.05),
            )

        # Extract minutes arrays
        all_minutes = [g.get("MIN", 0) for g in logs if g.get("MIN", 0) > 0]
        if not all_minutes:
            return None

        season_avg = sum(all_minutes) / len(all_minutes)
        last_5 = all_minutes[-5:] if len(all_minutes) >= 5 else all_minutes
        last_10 = all_minutes[-10:] if len(all_minutes) >= 10 else all_minutes

        # Compute per-minute stat rates with Bayesian shrinkage
        total_mins = sum(all_minutes)
        stat_rates = self._compute_stat_rates(logs, total_mins, position)

        return PlayerMinutes(
            player_id=player_id,
            player_name=player_name,
            position=position,
            team_id=team_id,
            minutes_last_5=last_5,
            minutes_last_10=last_10,
            season_avg=round(season_avg, 1),
            usage_rate=0.20,  # Default; ESPN doesn't provide USG%
            age=None,  # Not relevant for CBB B2B (disabled)
            pts_per_min=stat_rates.get("PTS", 0.0),
            reb_per_min=stat_rates.get("REB", 0.0),
            ast_per_min=stat_rates.get("AST", 0.0),
            stl_per_min=stat_rates.get("STL", 0.0),
            blk_per_min=stat_rates.get("BLK", 0.0),
            tov_per_min=stat_rates.get("TOV", 0.0),
            fg3m_per_min=stat_rates.get("FG3M", 0.0),
        )

    @staticmethod
    def _compute_stat_rates(
        logs: List[Dict],
        total_mins: float,
        position: str,
    ) -> Dict[str, float]:
        """Compute per-minute stat rates with Bayesian shrinkage.

        For players with fewer than 15 games, we blend observed rates
        with CBB position priors to avoid noisy projections from small
        samples.

        Shrinkage formula:
            blended = w * observed + (1-w) * prior
            where w = min(num_games / 15, 1.0)
        """
        priors = CBB_POSITION_PRIOR_RATES.get(
            position, CBB_POSITION_PRIOR_RATES.get("G", {})
        )
        num_games = len(logs)
        w = min(num_games / 15.0, 1.0)  # Full trust after 15 games

        stat_map = {
            "PTS": "PTS",
            "REB": "REB",
            "AST": "AST",
            "STL": "STL",
            "BLK": "BLK",
            "TOV": "TO",
            "FG3M": "FG3M",
        }

        rates: Dict[str, float] = {}
        for our_key, log_key in stat_map.items():
            total_stat = sum(g.get(log_key, 0) for g in logs)
            observed = total_stat / total_mins if total_mins > 0 else 0
            prior = priors.get(our_key, 0.0)
            blended = w * observed + (1 - w) * prior
            rates[our_key] = round(blended, 4)

        return rates

    # ------------------------------------------------------------------
    # Rotation cutoff (same algorithm as NBAApiService)
    # ------------------------------------------------------------------

    @staticmethod
    def _find_rotation_cutoff(
        players: List[PlayerMinutes], max_rotation: int = 12
    ) -> int:
        """Find natural rotation cutoff via gap detection.

        Scans positions 7-12 for the largest relative drop in minutes.
        Default to CBB_ROTATION_SIZE_DEFAULT (9) if no clear gap.
        """
        if len(players) <= 7:
            return len(players)

        scan_start = 7
        scan_end = min(max_rotation, len(players))

        best_cut = CBB_ROTATION_SIZE_DEFAULT
        best_gap_ratio = 0.0

        for i in range(scan_start, scan_end):
            current = players[i - 1].season_avg
            next_val = players[i].season_avg
            if current <= 0:
                continue
            drop = current - next_val
            ratio = drop / current
            if drop >= 1.5 and ratio >= 0.20 and ratio > best_gap_ratio:
                best_cut = i
                best_gap_ratio = ratio

        return best_cut


# ======================================================================
# Helpers
# ======================================================================

def _normalize_cbb_position(pos: str) -> str:
    """Map ESPN CBB position labels to standard DFS positions."""
    pos = pos.upper().strip()
    mapping = {
        "PG": "PG",
        "SG": "SG",
        "SF": "SF",
        "PF": "PF",
        "C": "C",
        "G": "G",
        "F": "F",
        "G-F": "G",
        "F-G": "F",
        "F-C": "F",
        "C-F": "C",
        "GUARD": "G",
        "FORWARD": "F",
        "CENTER": "C",
    }
    return mapping.get(pos, "G")  # Default to G for unknown positions


def _safe_float(val) -> float:
    """Safely convert a value to float, handling strings like '12:30'."""
    if val is None:
        return 0.0
    try:
        s = str(val).strip()
        if ":" in s:
            # Minutes in MM:SS format
            parts = s.split(":")
            return float(parts[0]) + float(parts[1]) / 60.0
        return float(s)
    except (ValueError, TypeError):
        return 0.0
