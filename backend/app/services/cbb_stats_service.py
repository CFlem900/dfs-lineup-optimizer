"""CBB team statistics service — computes real pace, efficiency, and DvP.

Replaces hardcoded league-average stats with actual per-team values
computed from CBBpy box score data.  This is the foundation for
accurate CBB game projections and DvP matchup analysis.

Data flow:
    CBBpy game logs (box scores)
        -> _compute_pace()       -> per-team possessions/game
        -> _compute_efficiency() -> off/def rating per 100 possessions
        -> _compute_opponent_allowed_stats() -> DvP stat averages
        -> Bayesian shrinkage for small samples
        -> Cached in-memory (4-hour TTL)

Usage:
    from app.services.cbb_stats_service import CBBStatsService

    stats_svc = CBBStatsService()
    team_stats = await stats_svc.get_team_stats(team_id=150, team_name="Duke")
"""

import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from app.config.constants import (
    CBB_LEAGUE_AVG_PACE,
)

logger = logging.getLogger(__name__)

# ── Cache configuration ─────────────────────────────────────────────
_CBB_STATS_CACHE_TTL = 14400  # 4 hours — stats don't change intraday
_CBB_STATS_MIN_GAMES = 10     # Minimum games for full confidence

# ── League averages (D1 2024-25 approximations) ─────────────────────
# Used as Bayesian priors for small-sample teams and as DvP baselines.
CBB_LEAGUE_AVG_OFF_RATING: float = 104.0   # pts per 100 possessions
CBB_LEAGUE_AVG_DEF_RATING: float = 104.0   # pts allowed per 100 poss
CBB_LEAGUE_AVG_PPG: float = 73.0           # points per game

CBB_LEAGUE_AVG_STATS_PG: Dict[str, float] = {
    "pts": 73.0,
    "reb": 34.0,
    "ast": 14.5,
    "stl": 6.5,
    "blk": 3.2,
    "tov": 12.5,
    "fg3m": 7.5,
}


class CBBStatsService:
    """Computes real per-team stats from CBBpy game log data.

    Provides pace, offensive/defensive ratings, points per game,
    opponent points allowed, and DvP (defense vs position) stats.
    All values use Bayesian shrinkage toward league averages for
    teams with fewer than ``_CBB_STATS_MIN_GAMES`` games.
    """

    def __init__(self):
        self._cache: Dict[int, Tuple[float, Dict[str, Any]]] = {}
        self._all_stats_cache: Optional[Dict[int, Dict[str, Any]]] = None
        self._all_stats_ts: float = 0.0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def get_team_stats(
        self,
        team_id: int,
        team_name: str,
        season: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Get computed stats for a single team.

        Checks in-memory cache first, then computes from CBBpy box
        scores with Bayesian shrinkage applied.

        Parameters
        ----------
        team_id : int
            ESPN team ID.
        team_name : str
            Team display name (passed to CBBpy for game log lookup).
        season : str, optional
            CBBpy season year (e.g. "2026"). Defaults to current season.

        Returns
        -------
        dict
            Keys: team_id, name, season_pace, season_off_rating,
            season_def_rating, season_ppg, season_opp_ppg, games_played,
            opp_pts_pg, opp_reb_pg, opp_ast_pg, opp_stl_pg, opp_blk_pg,
            opp_tov_pg, opp_fg3m_pg
        """
        now = time.time()

        # Check per-team cache
        if team_id in self._cache:
            cached_at, cached_stats = self._cache[team_id]
            if now - cached_at < _CBB_STATS_CACHE_TTL:
                return cached_stats

        season = season or self._get_current_season()
        stats = await self._compute_team_stats(team_id, team_name, season)
        self._cache[team_id] = (time.time(), stats)
        return stats

    async def get_all_team_stats(
        self,
        team_ids_and_names: List[Tuple[int, str]],
        season: Optional[str] = None,
    ) -> Dict[int, Dict[str, Any]]:
        """Compute stats for multiple teams (e.g. all slate teams).

        Only computes for teams not already cached.  This is the main
        entry point for CBBGameService when building game projections.

        Teams are fetched concurrently with ``asyncio.gather`` (capped at
        4 at a time to avoid overwhelming CBBpy) and each individual
        team fetch has a **15-second timeout**.  Teams that time out get
        league-average defaults.

        Parameters
        ----------
        team_ids_and_names : list of (team_id, team_name)
            Teams to compute stats for.
        season : str, optional
            CBBpy season year.

        Returns
        -------
        dict[int, dict]
            Mapping of team_id -> stats dict.
        """
        import asyncio

        season = season or self._get_current_season()
        result: Dict[int, Dict[str, Any]] = {}

        # Separate cached from uncached
        uncached: List[Tuple[int, str]] = []
        now = time.time()
        for team_id, team_name in team_ids_and_names:
            if team_id in self._cache:
                cached_at, cached_stats = self._cache[team_id]
                if now - cached_at < _CBB_STATS_CACHE_TTL:
                    result[team_id] = cached_stats
                    continue
            uncached.append((team_id, team_name))

        if not uncached:
            return result

        # Fetch uncached teams concurrently (with per-team timeout)
        sem = asyncio.Semaphore(2)  # max 2 concurrent CBBpy calls (keep low to avoid starving event loop)

        async def _fetch_one(tid: int, tname: str) -> Tuple[int, Dict[str, Any]]:
            async with sem:
                try:
                    stats = await asyncio.wait_for(
                        self.get_team_stats(tid, tname, season),
                        timeout=15.0,
                    )
                    return tid, stats
                except asyncio.TimeoutError:
                    logger.warning(
                        f"CBBpy stats timed out for {tname} ({tid}) — "
                        "using league averages"
                    )
                    defaults = self._default_team_stats(tid, tname)
                    self._cache[tid] = (time.time(), defaults)
                    return tid, defaults
                except Exception as e:
                    logger.warning(
                        f"CBBpy stats failed for {tname} ({tid}): {e} — "
                        "using league averages"
                    )
                    defaults = self._default_team_stats(tid, tname)
                    return tid, defaults

        tasks = [_fetch_one(tid, tname) for tid, tname in uncached]
        completed = await asyncio.gather(*tasks, return_exceptions=True)

        for item in completed:
            if isinstance(item, Exception):
                logger.warning(f"Team stats gather exception: {item}")
                continue
            tid, stats = item
            result[tid] = stats

        return result

    def get_league_averages(self) -> Dict[str, float]:
        """Return the league-average stats used as priors."""
        return {
            "pace": CBB_LEAGUE_AVG_PACE,
            "off_rating": CBB_LEAGUE_AVG_OFF_RATING,
            "def_rating": CBB_LEAGUE_AVG_DEF_RATING,
            "ppg": CBB_LEAGUE_AVG_PPG,
            **{f"opp_{k}_pg": v for k, v in CBB_LEAGUE_AVG_STATS_PG.items()},
        }

    def clear_cache(self):
        """Clear all cached team stats (useful for testing)."""
        self._cache.clear()
        self._all_stats_cache = None
        self._all_stats_ts = 0.0

    # ------------------------------------------------------------------
    # Private: compute from CBBpy box scores
    # ------------------------------------------------------------------

    async def _compute_team_stats(
        self, team_id: int, team_name: str, season: str
    ) -> Dict[str, Any]:
        """Fetch game logs and compute all stats for one team."""
        import asyncio

        # Run the blocking CBBpy call in a thread so asyncio timeouts
        # can cancel it properly
        box_scores = await asyncio.to_thread(
            self._fetch_team_box_scores, team_name, season
        )

        if not box_scores:
            logger.info(
                f"No CBBpy box scores for {team_name} ({team_id}) — "
                f"using league averages"
            )
            return self._default_team_stats(team_id, team_name)

        games_played = len(box_scores)

        # Compute raw stats
        raw_pace = self._compute_pace(box_scores)
        raw_off_rtg, raw_def_rtg = self._compute_efficiency(
            box_scores, raw_pace
        )
        raw_ppg, raw_opp_ppg = self._compute_scoring_averages(box_scores)
        raw_opp_stats = self._compute_opponent_allowed_stats(box_scores)

        # Apply Bayesian shrinkage for small samples
        w = self._shrinkage_weight(games_played)

        pace = self._blend(raw_pace, CBB_LEAGUE_AVG_PACE, w)
        off_rtg = self._blend(raw_off_rtg, CBB_LEAGUE_AVG_OFF_RATING, w)
        def_rtg = self._blend(raw_def_rtg, CBB_LEAGUE_AVG_DEF_RATING, w)
        ppg = self._blend(raw_ppg, CBB_LEAGUE_AVG_PPG, w)
        opp_ppg = self._blend(raw_opp_ppg, CBB_LEAGUE_AVG_PPG, w)

        # Blend opponent-allowed stats
        opp_stats = {}
        for stat_name, raw_val in raw_opp_stats.items():
            league_avg = CBB_LEAGUE_AVG_STATS_PG.get(stat_name, raw_val)
            opp_stats[f"opp_{stat_name}_pg"] = round(
                self._blend(raw_val, league_avg, w), 2
            )

        # Sanity clamps
        pace = max(55.0, min(85.0, pace))
        off_rtg = max(80.0, min(130.0, off_rtg))
        def_rtg = max(80.0, min(130.0, def_rtg))
        ppg = max(50.0, min(100.0, ppg))
        opp_ppg = max(50.0, min(100.0, opp_ppg))

        stats = {
            "team_id": team_id,
            "name": team_name,
            "games_played": games_played,
            "shrinkage_weight": round(w, 3),
            "season_pace": round(pace, 1),
            "season_off_rating": round(off_rtg, 1),
            "season_def_rating": round(def_rtg, 1),
            "season_ppg": round(ppg, 1),
            "season_opp_ppg": round(opp_ppg, 1),
            **opp_stats,
        }

        logger.info(
            f"CBB stats for {team_name}: pace={pace:.1f}, "
            f"off_rtg={off_rtg:.1f}, def_rtg={def_rtg:.1f}, "
            f"ppg={ppg:.1f}, opp_ppg={opp_ppg:.1f} "
            f"(games={games_played}, w={w:.2f})"
        )

        return stats

    # ------------------------------------------------------------------
    # Stat computation formulas
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_pace(box_scores: List[Dict]) -> float:
        """Compute team pace (possessions per 40 minutes).

        Standard possession formula:
            Possessions = FGA + 0.44 * FTA - OREB + TOV

        Pace = Possessions * (40 / total_minutes_played)

        For team-level data from CBBpy, we sum all player stats per game
        and estimate possessions.
        """
        total_possessions = 0.0
        total_minutes = 0.0

        for game in box_scores:
            fga = game.get("FGA", 0)
            fta = game.get("FTA", 0)
            oreb = game.get("OREB", 0)
            tov = game.get("TOV", 0)
            minutes = game.get("TEAM_MIN", 200.0)

            # Possessions for this game
            poss = fga + 0.44 * fta - oreb + tov
            total_possessions += poss
            total_minutes += minutes

        if total_minutes <= 0:
            return CBB_LEAGUE_AVG_PACE

        # Pace = possessions per 40 minutes (one team's perspective)
        pace = (total_possessions / total_minutes) * 200.0
        return pace

    @staticmethod
    def _compute_efficiency(
        box_scores: List[Dict], pace: float
    ) -> Tuple[float, float]:
        """Compute offensive and defensive ratings.

        OffRtg = (Team PTS / Total Possessions) * 100
        DefRtg = (Opponent PTS / Total Possessions) * 100
        """
        total_pts = 0.0
        total_opp_pts = 0.0
        total_poss = 0.0

        for game in box_scores:
            pts = game.get("PTS", 0)
            opp_pts = game.get("OPP_PTS", 0)
            fga = game.get("FGA", 0)
            fta = game.get("FTA", 0)
            oreb = game.get("OREB", 0)
            tov = game.get("TOV", 0)

            poss = fga + 0.44 * fta - oreb + tov
            total_pts += pts
            total_opp_pts += opp_pts
            total_poss += poss

        if total_poss <= 0:
            return CBB_LEAGUE_AVG_OFF_RATING, CBB_LEAGUE_AVG_DEF_RATING

        off_rtg = (total_pts / total_poss) * 100.0
        def_rtg = (total_opp_pts / total_poss) * 100.0

        return off_rtg, def_rtg

    @staticmethod
    def _compute_scoring_averages(
        box_scores: List[Dict],
    ) -> Tuple[float, float]:
        """Compute average points per game and opponent ppg."""
        if not box_scores:
            return CBB_LEAGUE_AVG_PPG, CBB_LEAGUE_AVG_PPG

        total_pts = sum(g.get("PTS", 0) for g in box_scores)
        total_opp = sum(g.get("OPP_PTS", 0) for g in box_scores)
        n = len(box_scores)

        return total_pts / n, total_opp / n

    @staticmethod
    def _compute_opponent_allowed_stats(
        box_scores: List[Dict],
    ) -> Dict[str, float]:
        """Compute average stats that opponents score against this team.

        These are the raw DvP components: what does the opponent
        score/rebound/assist when playing against this team?
        """
        if not box_scores:
            return dict(CBB_LEAGUE_AVG_STATS_PG)

        n = len(box_scores)
        return {
            "pts": sum(g.get("OPP_PTS", 0) for g in box_scores) / n,
            "reb": sum(g.get("OPP_REB", 0) for g in box_scores) / n,
            "ast": sum(g.get("OPP_AST", 0) for g in box_scores) / n,
            "stl": sum(g.get("OPP_STL", 0) for g in box_scores) / n,
            "blk": sum(g.get("OPP_BLK", 0) for g in box_scores) / n,
            "tov": sum(g.get("OPP_TOV", 0) for g in box_scores) / n,
            "fg3m": sum(g.get("OPP_FG3M", 0) for g in box_scores) / n,
        }

    # ------------------------------------------------------------------
    # CBBpy data fetching (team-level box scores)
    # ------------------------------------------------------------------

    @staticmethod
    def _fetch_team_box_scores(
        team_name: str, season: str
    ) -> List[Dict]:
        """Fetch team-level game box scores via CBBpy.

        Returns a list of per-game dicts with team totals for all
        stat categories needed for pace/efficiency/DvP computation.

        Each dict has keys:
            PTS, OPP_PTS, FGA, FTA, OREB, TOV, REB, AST, STL, BLK,
            FG3M, OPP_REB, OPP_AST, OPP_STL, OPP_BLK, OPP_TOV,
            OPP_FG3M, TEAM_MIN
        """
        try:
            from app.services.cbbpy_throttle import cbbpy_get_games_team
            import pandas as pd

            season_int = int(season)
            games_data = cbbpy_get_games_team(
                team_name, season_int, info=True, box=True, pbp=False,
            )

            # CBBpy returns (game_info_df, boxscore_df, pbp_df) —
            # a SINGLE boxscore DataFrame with a "team" column, NOT
            # separate home/away DataFrames.
            if games_data is None or not isinstance(games_data, tuple) or len(games_data) < 2:
                logger.warning(
                    f"Unexpected CBBpy format for {team_name}"
                )
                return []

            game_info_df = games_data[0]
            boxscore_df = games_data[1]

            if game_info_df is None or game_info_df.empty:
                return []
            if boxscore_df is None or boxscore_df.empty:
                return []

            team_name_lower = team_name.lower()
            team_games: List[Dict] = []

            # Iterate over game info rows to determine home/away
            for idx, game_row in game_info_df.iterrows():
                game_id = str(game_row.get("game_id", ""))

                # CBBpy uses "home_team" and "away_team" column names
                home_team_name = str(
                    game_row.get("home_team", "")
                ).lower()
                away_team_name = str(
                    game_row.get("away_team", "")
                ).lower()

                # Determine if our team is home or away
                is_home = team_name_lower in home_team_name
                is_away = team_name_lower in away_team_name

                if not is_home and not is_away:
                    # Try abbreviated/partial matching
                    is_home = any(
                        word in home_team_name
                        for word in team_name_lower.split()
                        if len(word) > 3
                    )
                    is_away = not is_home and any(
                        word in away_team_name
                        for word in team_name_lower.split()
                        if len(word) > 3
                    )

                if not is_home and not is_away:
                    continue

                # Determine team and opponent names for box score filtering
                our_team_full = home_team_name if is_home else away_team_name
                opp_team_full = away_team_name if is_home else home_team_name

                # Filter box score by game_id
                game_box = boxscore_df[
                    boxscore_df["game_id"].astype(str) == game_id
                ] if "game_id" in boxscore_df.columns else pd.DataFrame()

                if game_box.empty:
                    continue

                # Split into team rows vs opponent rows using the
                # "team" column.  CBBpy uses full team names.
                if "team" in game_box.columns:
                    box_teams = game_box["team"].str.lower().unique()
                    # Match our team name against box score team names
                    our_match = [
                        t for t in box_teams
                        if team_name_lower in t or any(
                            w in t for w in team_name_lower.split()
                            if len(w) > 3
                        )
                    ]
                    if our_match:
                        team_rows = game_box[
                            game_box["team"].str.lower() == our_match[0]
                        ]
                        opp_rows = game_box[
                            game_box["team"].str.lower() != our_match[0]
                        ]
                    else:
                        team_rows = pd.DataFrame()
                        opp_rows = pd.DataFrame()
                else:
                    team_rows = pd.DataFrame()
                    opp_rows = pd.DataFrame()

                # Sum team stats for this game
                team_totals = _aggregate_box_score(team_rows)
                opp_totals = _aggregate_box_score(opp_rows)

                game_entry = {
                    "PTS": team_totals.get("PTS", 0),
                    "FGA": team_totals.get("FGA", 0),
                    "FTA": team_totals.get("FTA", 0),
                    "OREB": team_totals.get("OREB", 0),
                    "TOV": team_totals.get("TOV", 0),
                    "REB": team_totals.get("REB", 0),
                    "AST": team_totals.get("AST", 0),
                    "STL": team_totals.get("STL", 0),
                    "BLK": team_totals.get("BLK", 0),
                    "FG3M": team_totals.get("FG3M", 0),
                    "TEAM_MIN": 200.0,  # 5 players x 40 min
                    # Opponent stats (for DvP and def rating)
                    "OPP_PTS": opp_totals.get("PTS", 0),
                    "OPP_REB": opp_totals.get("REB", 0),
                    "OPP_AST": opp_totals.get("AST", 0),
                    "OPP_STL": opp_totals.get("STL", 0),
                    "OPP_BLK": opp_totals.get("BLK", 0),
                    "OPP_TOV": opp_totals.get("TOV", 0),
                    "OPP_FG3M": opp_totals.get("FG3M", 0),
                }

                # Only include games where we got meaningful data
                if game_entry["PTS"] > 0 or game_entry["OPP_PTS"] > 0:
                    team_games.append(game_entry)

            logger.info(
                f"CBBpy team box scores for {team_name}: "
                f"{len(team_games)} games parsed"
            )
            return team_games

        except ImportError:
            logger.warning(
                "CBBpy not installed — cannot compute real team stats"
            )
            return []
        except Exception as e:
            logger.error(
                f"CBBpy team box score fetch failed for {team_name}: {e}"
            )
            return []

    # ------------------------------------------------------------------
    # Bayesian shrinkage
    # ------------------------------------------------------------------

    @staticmethod
    def _shrinkage_weight(games_played: int) -> float:
        """Compute Bayesian shrinkage weight.

        Returns a value in [0, 1] indicating how much to trust the
        observed data vs. the league-average prior.

        w = min(games_played / _CBB_STATS_MIN_GAMES, 1.0)

        With 10+ games, we fully trust the observed data.
        With 5 games, it's 50% observed + 50% prior.
        """
        return min(games_played / _CBB_STATS_MIN_GAMES, 1.0)

    @staticmethod
    def _blend(observed: float, prior: float, weight: float) -> float:
        """Blend observed value with prior using shrinkage weight."""
        return weight * observed + (1.0 - weight) * prior

    # ------------------------------------------------------------------
    # Defaults & helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _default_team_stats(
        team_id: int, team_name: str
    ) -> Dict[str, Any]:
        """Return league-average defaults when no data is available."""
        return {
            "team_id": team_id,
            "name": team_name,
            "games_played": 0,
            "shrinkage_weight": 0.0,
            "season_pace": CBB_LEAGUE_AVG_PACE,
            "season_off_rating": CBB_LEAGUE_AVG_OFF_RATING,
            "season_def_rating": CBB_LEAGUE_AVG_DEF_RATING,
            "season_ppg": CBB_LEAGUE_AVG_PPG,
            "season_opp_ppg": CBB_LEAGUE_AVG_PPG,
            "opp_pts_pg": CBB_LEAGUE_AVG_STATS_PG["pts"],
            "opp_reb_pg": CBB_LEAGUE_AVG_STATS_PG["reb"],
            "opp_ast_pg": CBB_LEAGUE_AVG_STATS_PG["ast"],
            "opp_stl_pg": CBB_LEAGUE_AVG_STATS_PG["stl"],
            "opp_blk_pg": CBB_LEAGUE_AVG_STATS_PG["blk"],
            "opp_tov_pg": CBB_LEAGUE_AVG_STATS_PG["tov"],
            "opp_fg3m_pg": CBB_LEAGUE_AVG_STATS_PG["fg3m"],
        }

    @staticmethod
    def _get_current_season() -> str:
        """Return the current CBB season year (e.g. '2026')."""
        from datetime import datetime
        now = datetime.now()
        if now.month >= 7:
            return str(now.year + 1)
        return str(now.year)


# ======================================================================
# Module-level helpers
# ======================================================================

def _aggregate_box_score(df) -> Dict[str, float]:
    """Sum a DataFrame of player box score rows into team totals.

    Handles CBBpy column naming.  CBBpy uses lowercase names:
        pts, fga, fta, oreb, to, reb, ast, stl, blk, 3pm
    """
    import pandas as pd

    if df is None or (isinstance(df, pd.DataFrame) and df.empty):
        return {}

    result: Dict[str, float] = {}

    # Priority order: CBBpy lowercase first, then uppercase fallbacks
    col_map = {
        "PTS": ["pts", "PTS"],
        "FGA": ["fga", "FGA"],
        "FTA": ["fta", "FTA"],
        "OREB": ["oreb", "OREB", "or", "OR"],
        "TOV": ["to", "TO", "tov", "TOV"],
        "REB": ["reb", "REB"],
        "AST": ["ast", "AST"],
        "STL": ["stl", "STL"],
        "BLK": ["blk", "BLK"],
        "FG3M": ["3pm", "3PM", "fg3m", "FG3M"],
    }

    for stat_name, possible_cols in col_map.items():
        for col in possible_cols:
            if col in df.columns:
                try:
                    result[stat_name] = pd.to_numeric(
                        df[col], errors="coerce"
                    ).fillna(0).sum()
                except Exception:
                    result[stat_name] = 0
                break
        if stat_name not in result:
            result[stat_name] = 0

    return result
