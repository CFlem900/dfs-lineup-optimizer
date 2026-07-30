import logging
import time
from typing import List, Dict, Optional, Any
from datetime import date, datetime, timedelta, timezone

from nba_api.stats.endpoints import (
    scoreboardv2,
    leaguedashteamstats,
    teamgamelog,
)
from nba_api.stats.static import teams as nba_teams

from app.config import get_settings

from app.config.constants import (
    DK_TO_NBA_ABBR_ALIASES,
    VEGAS_TOTAL_BLEND_WEIGHT,
    VEGAS_SPREAD_BLEND_WEIGHT,
    VEGAS_DIVERGENCE_WARNING_THRESHOLD,
)
from app.models.game import DFSSlate, GameInfo, Schedule, TeamGameStats
from app.services.dk_slate_service import DK_TEAM_ID_TO_NBA_ABBR, DKSlateService
from app.utils.helpers import get_current_nba_season

logger = logging.getLogger(__name__)
settings = get_settings()

# League-average pace and efficiency for normalization
# 2025-26 season: ~101.9 pace, ~114.3 ORtg (through early season data)
# These are used as fallbacks; the real values are fetched from the NBA API
from app.config.constants import LEAGUE_AVG_PACE  # noqa: E402 — centralized constant
LEAGUE_AVG_OFF_RATING = 114.3
LEAGUE_AVG_DEF_RATING = 114.3  # ≈ OFF_RATING in league aggregate; separate constant for clarity

# League-average per-game stats (used to compute DvP matchup ratios).
# These are recalculated dynamically from the fetched data; the defaults
# here are approximate 2025-26 averages as a safety fallback.
LEAGUE_AVG_STATS_PG = {
    "pts": 113.5,
    "reb": 43.5,
    "ast": 26.0,
    "stl": 7.8,
    "blk": 5.0,
    "tov": 14.0,
    "fg3m": 13.0,
}

# Static abbreviation → NBA team_id lookup built from nba_api.
# Used by the DK fallback scheduler when ScoreboardV2 is unavailable.
_NBA_ABBR_TO_ID: Dict[str, int] = {
    t["abbreviation"]: t["id"] for t in nba_teams.get_teams()
}


class GameService:
    """Service for game-day projections: pace, totals, over/under."""

    def __init__(self):
        self._last_request_time = 0.0
        self._min_request_interval = 0.6
        self._team_stats_cache: Optional[Dict[int, Dict]] = None
        self._team_stats_cache_date: Optional[str] = None
        self._schedule_cache: Dict[str, Schedule] = {}  # date → Schedule
        self._schedule_cache_ts: Dict[str, float] = {}  # date → cache timestamp
        self._SCHEDULE_TTL_TODAY_S: float = 300.0  # 5 min TTL for today (game statuses change)
        self._scoreboard_header_cache: Dict[str, List[Dict]] = {}  # date → raw headers
        self._dk_slate_service = DKSlateService()
        self._db_cache = None  # Optional NBADataCacheService for DB-cached reads
        self._data_service = None  # Optional NBAMultiSourceService for BDL-first scoreboard
        self._bdl = None  # Optional BallDontLieService for BDL odds fallback
        self._odds_service = None  # Optional OddsService (The Odds API) for NBA odds
        self._odds_fetcher = None  # Optional OddsFetcherService (unified: API + heuristic)
        self._last_5_cache: Dict[int, float] = {}  # team_id → last-5 PPG

    def _rate_limit(self):
        elapsed = time.time() - self._last_request_time
        if elapsed < self._min_request_interval:
            time.sleep(self._min_request_interval - elapsed)
        self._last_request_time = time.time()

    def _retry_request(self, func, *args, **kwargs) -> Any:
        for attempt in range(settings.nba_api_max_retries):
            try:
                self._rate_limit()
                return func(*args, **kwargs)
            except Exception as e:
                err_msg = str(e).lower()
                # DNS failure — no point retrying, fail immediately
                if "getaddrinfo failed" in err_msg or "name resolution" in err_msg:
                    logger.warning(
                        f"Game service DNS failure (attempt {attempt + 1}): {e}. "
                        f"Skipping retries — network unreachable."
                    )
                    raise
                wait_time = settings.nba_api_retry_delay * (2 ** attempt)
                logger.warning(
                    f"Game service request failed (attempt {attempt + 1}): {e}. "
                    f"Retrying in {wait_time}s..."
                )
                if attempt < settings.nba_api_max_retries - 1:
                    time.sleep(wait_time)
                else:
                    raise

    # ------------------------------------------------------------------
    # Fetch league-wide team stats (pace, off/def rating, ppg)
    # ------------------------------------------------------------------

    def _get_all_team_stats(
        self, season: str = None, *, skip_api: bool = False,
        background_mode: bool = False,
    ) -> Dict[int, Dict]:
        """Fetch season stats for all teams. Cached for 1 day.

        Merges three API calls to build a complete picture:
        1. **Base** (PerGame) — PPG, FGA, FTA, OREB, TOV, etc.
        2. **Advanced** — Real OFF_RATING, DEF_RATING, PACE from the NBA
        3. **Opponent** (PerGame) — OPP_PTS, OPP_REB, OPP_AST, etc.
           for Defense-vs-Position (DvP) matchup adjustments.

        When a DB cache (``_db_cache``) is available, reads from
        PostgreSQL first before falling back to the live NBA API.

        Args:
            skip_api: If True, only use in-memory / DB caches and
                return ``{}`` if both miss.  Used by the DK fallback
                path to avoid blocking on slow NBA API calls — the
                game builder handles empty stats gracefully with
                league-average defaults.
            background_mode: If True, allow live NBA API calls (used by
                the 4 AM cache refresh job only).  When False (default),
                never call stats.nba.com — BDL + DB cache only.
        """
        season = season or get_current_nba_season()
        today = date.today().isoformat()
        if self._team_stats_cache and self._team_stats_cache_date == today:
            return self._team_stats_cache

        # ── Try DB cache first ────────────────────────────────────
        if self._db_cache is not None:
            try:
                db_stats = self._db_cache.get_all_team_stats_sync(season)
                if db_stats and len(db_stats) >= 20:
                    self._team_stats_cache = db_stats
                    self._team_stats_cache_date = today
                    logger.info(
                        f"[TeamStats] DB cache hit: {len(db_stats)} teams"
                    )
                    return db_stats
            except Exception as e:
                logger.warning(f"[TeamStats] DB cache read failed: {e}")

        # Never call stats.nba.com from user-facing requests.
        # The 4 AM background refresh populates the DB cache; if both
        # in-memory and DB caches miss, league-average defaults are used.
        if skip_api or (settings.skip_nba_api_live and not background_mode):
            logger.info(
                "[TeamStats] No cache available — "
                "returning empty stats (league-average defaults will be used)"
            )
            return {}

        def _fetch_base():
            stats = leaguedashteamstats.LeagueDashTeamStats(
                season=season,
                measure_type_detailed_defense="Base",
                per_mode_detailed="PerGame",
                timeout=settings.nba_api_timeout,
            )
            return stats.get_normalized_dict()["LeagueDashTeamStats"]

        def _fetch_advanced():
            stats = leaguedashteamstats.LeagueDashTeamStats(
                season=season,
                measure_type_detailed_defense="Advanced",
                per_mode_detailed="PerGame",
                timeout=settings.nba_api_timeout,
            )
            return stats.get_normalized_dict()["LeagueDashTeamStats"]

        def _fetch_opponent():
            stats = leaguedashteamstats.LeagueDashTeamStats(
                season=season,
                measure_type_detailed_defense="Opponent",
                per_mode_detailed="PerGame",
                timeout=settings.nba_api_timeout,
            )
            return stats.get_normalized_dict()["LeagueDashTeamStats"]

        try:
            base_rows = self._retry_request(_fetch_base)

            # Fetch advanced + opponent stats (best-effort — degrade gracefully)
            adv_lookup: Dict[int, Dict] = {}
            opp_lookup: Dict[int, Dict] = {}
            try:
                adv_rows = self._retry_request(_fetch_advanced)
                adv_lookup = {r["TEAM_ID"]: r for r in adv_rows}
            except Exception as e:
                logger.warning(f"Advanced team stats fetch failed: {e}")

            try:
                opp_rows = self._retry_request(_fetch_opponent)
                opp_lookup = {r["TEAM_ID"]: r for r in opp_rows}
            except Exception as e:
                logger.warning(f"Opponent team stats fetch failed: {e}")

            cache = {}
            for row in base_rows:
                tid = row["TEAM_ID"]
                gp = row.get("GP", 1) or 1
                adv = adv_lookup.get(tid, {})
                opp = opp_lookup.get(tid, {})

                # Prefer real off/def ratings and pace from Advanced endpoint
                real_pace = adv.get("PACE", 0)
                real_off = adv.get("OFF_RATING", 0)
                real_def = adv.get("DEF_RATING", 0)

                cache[tid] = {
                    "team_id": tid,
                    "team_name": row.get("TEAM_NAME", ""),
                    "gp": gp,
                    "ppg": round(row.get("PTS", 0), 1),
                    "fgm": row.get("FGM", 0),
                    "fga": row.get("FGA", 0),
                    "ftm": row.get("FTM", 0),
                    "fta": row.get("FTA", 0),
                    "oreb": row.get("OREB", 0),
                    "dreb": row.get("DREB", 0),
                    "tov": row.get("TOV", 0),
                    # Use real ratings/pace from Advanced, fallback to estimate
                    "pace": round(real_pace, 1) if real_pace else self._estimate_pace_from_box(row),
                    "off_rating": round(real_off, 1) if real_off else LEAGUE_AVG_OFF_RATING,
                    "def_rating": round(real_def, 1) if real_def else LEAGUE_AVG_DEF_RATING,
                    "w": row.get("W", 0),
                    "l": row.get("L", 0),
                    # Opponent stats allowed per game (for DvP)
                    "opp_pts_pg": round(float(opp.get("OPP_PTS", 0) or 0), 1),
                    "opp_reb_pg": round(float(opp.get("OPP_REB", 0) or 0), 1),
                    "opp_ast_pg": round(float(opp.get("OPP_AST", 0) or 0), 1),
                    "opp_stl_pg": round(float(opp.get("OPP_STL", 0) or 0), 1),
                    "opp_blk_pg": round(float(opp.get("OPP_BLK", 0) or 0), 1),
                    "opp_tov_pg": round(float(opp.get("OPP_TOV", 0) or 0), 1),
                    "opp_fg3m_pg": round(float(opp.get("OPP_FG3M", 0) or 0), 1),
                }
            self._team_stats_cache = cache
            self._team_stats_cache_date = today
            logger.info(
                f"Fetched team stats: {len(cache)} teams "
                f"(adv={len(adv_lookup)}, opp={len(opp_lookup)})"
            )
            return cache
        except Exception as e:
            logger.error(f"Failed to fetch team stats: {e}")
            return {}

    @staticmethod
    def _estimate_pace_from_box(row: Dict) -> float:
        """Estimate pace from box-score averages using the standard formula.
        Pace ≈ 48 * (FGA + 0.44*FTA - OREB + TOV) / MIN_per_game
        Simplified: since per-game data, assume 48 min denominator.
        """
        fga = row.get("FGA", 85)
        fta = row.get("FTA", 22)
        oreb = row.get("OREB", 10)
        tov = row.get("TOV", 14)
        possessions = fga + 0.44 * fta - oreb + tov
        # This gives possessions per game; normalize
        return round(possessions, 1)

    def get_dvp_matchup_factors(self, opponent_team_id: int) -> Dict[str, float]:
        """Compute DvP (Defense vs Position) matchup factors for a given opponent.

        Returns a dict mapping stat category → multiplier.  A value of 1.05
        means the opponent allows 5% more than league average in that stat;
        0.93 means they allow 7% less (good defense).

        These factors are applied to per-minute stat rates in the DFS
        projection to model the matchup effect on player production.
        """
        all_stats = self._get_all_team_stats()
        opp = all_stats.get(opponent_team_id, {})

        if not opp or not opp.get("opp_pts_pg"):
            return {}  # No opponent data available

        # Compute live league averages from the fetched data
        n_teams = len(all_stats) or 1
        lg_pts = sum(s.get("opp_pts_pg", 0) for s in all_stats.values()) / n_teams or LEAGUE_AVG_STATS_PG["pts"]
        lg_reb = sum(s.get("opp_reb_pg", 0) for s in all_stats.values()) / n_teams or LEAGUE_AVG_STATS_PG["reb"]
        lg_ast = sum(s.get("opp_ast_pg", 0) for s in all_stats.values()) / n_teams or LEAGUE_AVG_STATS_PG["ast"]
        lg_stl = sum(s.get("opp_stl_pg", 0) for s in all_stats.values()) / n_teams or LEAGUE_AVG_STATS_PG["stl"]
        lg_blk = sum(s.get("opp_blk_pg", 0) for s in all_stats.values()) / n_teams or LEAGUE_AVG_STATS_PG["blk"]
        lg_tov = sum(s.get("opp_tov_pg", 0) for s in all_stats.values()) / n_teams or LEAGUE_AVG_STATS_PG["tov"]
        lg_fg3 = sum(s.get("opp_fg3m_pg", 0) for s in all_stats.values()) / n_teams or LEAGUE_AVG_STATS_PG["fg3m"]

        # DvP ratio = what opponent allows / league average
        # Clamped to [0.80, 1.20] to avoid extreme swings from small samples
        def _ratio(opp_val, lg_val):
            if lg_val <= 0:
                return 1.0
            return max(0.80, min(1.20, opp_val / lg_val))

        return {
            "pts": round(_ratio(opp.get("opp_pts_pg", 0), lg_pts), 3),
            "reb": round(_ratio(opp.get("opp_reb_pg", 0), lg_reb), 3),
            "ast": round(_ratio(opp.get("opp_ast_pg", 0), lg_ast), 3),
            "stl": round(_ratio(opp.get("opp_stl_pg", 0), lg_stl), 3),
            "blk": round(_ratio(opp.get("opp_blk_pg", 0), lg_blk), 3),
            "tov": round(_ratio(opp.get("opp_tov_pg", 0), lg_tov), 3),
            "fg3m": round(_ratio(opp.get("opp_fg3m_pg", 0), lg_fg3), 3),
        }

    # Position-stat sensitivity weights: how much a position's DFS output
    # depends on each stat category.  A center's FP is dominated by
    # rebounds and blocks, so their DvP should weight those heavily.
    # A PG's FP is driven by assists and steals.  These weights amplify
    # the team-level DvP signal for the stats that matter most to each
    # position, effectively creating position-specific DvP.
    _POSITION_DvP_WEIGHTS: Dict[str, Dict[str, float]] = {
        "PG": {"pts": 1.0, "reb": 0.5, "ast": 1.4, "stl": 1.3, "blk": 0.3, "tov": 1.0, "fg3m": 1.2},
        "SG": {"pts": 1.2, "reb": 0.5, "ast": 0.8, "stl": 1.1, "blk": 0.3, "tov": 0.9, "fg3m": 1.3},
        "SF": {"pts": 1.1, "reb": 0.9, "ast": 0.7, "stl": 1.0, "blk": 0.6, "tov": 0.9, "fg3m": 1.1},
        "PF": {"pts": 1.0, "reb": 1.3, "ast": 0.6, "stl": 0.8, "blk": 1.1, "tov": 0.8, "fg3m": 0.9},
        "C":  {"pts": 0.9, "reb": 1.4, "ast": 0.5, "stl": 0.6, "blk": 1.4, "tov": 0.7, "fg3m": 0.5},
    }

    def get_position_dvp_factors(
        self, opponent_team_id: int, position: str
    ) -> Dict[str, float]:
        """Compute position-adjusted DvP matchup factors.

        Takes the team-level DvP and amplifies/dampens each stat based
        on how important that stat is for the given position.  A center
        facing a team that allows a lot of rebounds will see a bigger
        rebound boost than a PG facing the same team.

        The adjustment is applied to the *deviation* from 1.0, not the
        raw DvP, so neutral matchups (1.0) stay neutral.
        """
        base_dvp = self.get_dvp_matchup_factors(opponent_team_id)
        if not base_dvp:
            return {}

        pos = position.upper().split("/")[0].split("-")[0]  # Handle "PG/SG" or "G-F"
        weights = self._POSITION_DvP_WEIGHTS.get(pos, {})
        if not weights:
            return base_dvp

        result = {}
        for stat, dvp_val in base_dvp.items():
            w = weights.get(stat, 1.0)
            # Amplify the deviation from neutral (1.0)
            deviation = dvp_val - 1.0
            adjusted = 1.0 + deviation * w
            # Clamp to [0.80, 1.20] — same bounds as team-level
            result[stat] = round(max(0.80, min(1.20, adjusted)), 3)
        return result

    def _get_team_last_5(
        self, team_id: int, season: str = None, *, skip_api: bool = False,
        background_mode: bool = False,
    ) -> float:
        """Get a team's average PPG over last 5 games.

        Results are cached in-memory for the lifetime of the service
        instance so that building a full schedule (24 teams) doesn't
        make 24 separate API calls on every request.

        Args:
            skip_api: If True, only use cache; don't hit the NBA API.
            background_mode: If True, allow live NBA API calls (4 AM
                refresh only).  When False (default), never call
                stats.nba.com — cache-only.
        """
        if team_id in self._last_5_cache:
            return self._last_5_cache[team_id]

        # Never call stats.nba.com from user-facing requests.
        # Last-5 PPG has only a 20% blend weight; using season PPG
        # (the 0.0 fallback) has minimal impact on projections.
        if skip_api or (settings.skip_nba_api_live and not background_mode):
            return 0.0

        season = season or get_current_nba_season()

        def _fetch():
            log = teamgamelog.TeamGameLog(
                team_id=team_id,
                season=season,
                timeout=settings.nba_api_timeout,
            )
            games = log.get_normalized_dict()["TeamGameLog"]
            last_5 = games[:5] if len(games) >= 5 else games
            if not last_5:
                return 0.0
            return round(
                sum(float(g.get("PTS", 0) or 0) for g in last_5) / len(last_5),
                1,
            )

        try:
            result = self._retry_request(_fetch)
            self._last_5_cache[team_id] = result
            return result
        except Exception as e:
            logger.warning(f"Failed to fetch last-5 PPG for team {team_id}: {e}")
            return 0.0

    # ------------------------------------------------------------------
    # Build team game stats object
    # ------------------------------------------------------------------

    def _build_team_game_stats(
        self, team_id: int, all_stats: Dict[int, Dict],
        *, skip_api: bool = False,
    ) -> TeamGameStats:
        """Build a TeamGameStats from cached league-wide data.

        Uses real OFF_RATING, DEF_RATING, and PACE from the NBA Advanced
        endpoint (fetched in ``_get_all_team_stats``).  Also populates
        opponent per-game stat fields for DvP matchup adjustments.

        Args:
            skip_api: If True, don't make live NBA API calls for
                per-team data like last-5 PPG.
        """
        row = all_stats.get(team_id, {})

        team_info = None
        for t in nba_teams.get_teams():
            if t["id"] == team_id:
                team_info = t
                break

        ppg = row.get("ppg", 110.0)
        pace = row.get("pace", LEAGUE_AVG_PACE)

        # Use real ratings from the Advanced endpoint
        off_rating = row.get("off_rating", LEAGUE_AVG_OFF_RATING)
        def_rating = row.get("def_rating", LEAGUE_AVG_DEF_RATING)

        # Derive opponent PPG from real def_rating + pace
        opp_ppg = round(def_rating * pace / 100, 1) if pace else 110.0

        last_5 = self._get_team_last_5(team_id, skip_api=skip_api)

        return TeamGameStats(
            team_id=team_id,
            team_name=team_info["full_name"] if team_info else row.get("team_name", "Unknown"),
            team_abbreviation=team_info["abbreviation"] if team_info else "???",
            season_pace=pace,
            season_off_rating=off_rating,
            season_def_rating=def_rating,
            season_ppg=ppg,
            season_opp_ppg=opp_ppg,
            last_5_ppg=last_5 if last_5 > 0 else ppg,
            # Win-loss record for competitive context
            wins=row.get("w", 0),
            losses=row.get("l", 0),
            # Opponent per-game stats for DvP adjustments
            opp_pts_pg=row.get("opp_pts_pg", 0.0),
            opp_reb_pg=row.get("opp_reb_pg", 0.0),
            opp_ast_pg=row.get("opp_ast_pg", 0.0),
            opp_stl_pg=row.get("opp_stl_pg", 0.0),
            opp_blk_pg=row.get("opp_blk_pg", 0.0),
            opp_tov_pg=row.get("opp_tov_pg", 0.0),
            opp_fg3m_pg=row.get("opp_fg3m_pg", 0.0),
        )

    # ------------------------------------------------------------------
    # Odds / over-under
    # ------------------------------------------------------------------

    def _fetch_odds(self) -> Dict[str, Dict[str, float]]:
        """Fetch today's over/under AND spread lines from the NBA live odds endpoint.

        Returns a dict mapping game_id (10-digit NBA game ID) → {
            "over_under": total line,
            "spread": home spread (negative = home favored),
        }.
        Gracefully returns an empty dict on any failure.
        """
        try:
            from nba_api.live.nba.endpoints.odds import Odds

            odds = Odds(timeout=10)
            games_data = odds.get_games()
            if not games_data or not games_data.data:
                return {}

            odds_map: Dict[str, Dict[str, float]] = {}
            for game in games_data.data:
                game_id = game.get("gameId", "")
                if not game_id:
                    continue

                # Zero-pad to 10 digits to match the scoreboard GAME_ID format
                padded_id = game_id.zfill(10)
                game_odds: Dict[str, float] = {}

                for market in game.get("markets", []):
                    market_name = (market.get("name") or "").lower()

                    if "total" in market_name and "over_under" not in game_odds:
                        # Over/under (total) market
                        for book in market.get("books", []):
                            for outcome in book.get("outcomes", []):
                                spread_val = outcome.get("spread")
                                if spread_val is not None:
                                    try:
                                        game_odds["over_under"] = float(spread_val)
                                    except (ValueError, TypeError):
                                        continue
                                    break
                            if "over_under" in game_odds:
                                break

                    elif "spread" in market_name and "spread" not in game_odds:
                        # Point spread market (home team perspective)
                        for book in market.get("books", []):
                            for outcome in book.get("outcomes", []):
                                outcome_name = (outcome.get("name") or "").lower()
                                spread_val = outcome.get("spread")
                                if spread_val is not None and "home" in outcome_name:
                                    try:
                                        game_odds["spread"] = float(spread_val)
                                    except (ValueError, TypeError):
                                        continue
                                    break
                                elif spread_val is not None and "away" in outcome_name:
                                    # Flip sign for home-relative convention
                                    try:
                                        game_odds["spread"] = -float(spread_val)
                                    except (ValueError, TypeError):
                                        continue
                                    break
                            if "spread" in game_odds:
                                break

                if game_odds:
                    odds_map[padded_id] = game_odds

            ou_count = sum(1 for g in odds_map.values() if "over_under" in g)
            sp_count = sum(1 for g in odds_map.values() if "spread" in g)
            if odds_map:
                logger.info(
                    f"[Odds] Fetched lines for {len(odds_map)} games "
                    f"(O/U: {ou_count}, spreads: {sp_count})"
                )
            return odds_map

        except Exception as e:
            logger.debug(f"[Odds] Failed to fetch odds: {e}")
            return {}

    def _fetch_odds_bdl(self, target_date: str) -> Dict[str, Dict[str, float]]:
        """Fetch odds from BallDontLie as fallback, keyed by team abbreviation.

        Called when ``_fetch_odds()`` returns empty (the NBA live odds
        endpoint is dead).  Returns a dict mapping team abbreviation →
        ``{"over_under": float, "spread": float}``.

        The home team entry stores the canonical spread (negative = home
        favored); the away team entry has the spread sign flipped.
        """
        bdl = self._bdl
        if not bdl or not getattr(bdl, "is_available", False):
            return {}

        try:
            bdl_games = bdl.get_games(target_date)
            bdl_odds = bdl.get_odds_by_game(target_date)

            if not bdl_odds:
                return {}

            # BDL game_id → (home_abbr, away_abbr)
            game_teams: Dict[int, tuple] = {}
            for g in bdl_games:
                gid = g.get("id")
                home = g.get("home_team") or {}
                visitor = g.get("visitor_team") or {}
                if gid:
                    game_teams[gid] = (
                        (home.get("abbreviation") or "").upper(),
                        (visitor.get("abbreviation") or "").upper(),
                    )

            result: Dict[str, Dict[str, float]] = {}
            for bdl_gid, odds in bdl_odds.items():
                teams = game_teams.get(bdl_gid)
                if not teams:
                    continue
                home_abbr, away_abbr = teams

                total = odds.get("total")
                spread_home = odds.get("spread_home")

                entry: Dict[str, float] = {}
                if total is not None:
                    try:
                        entry["over_under"] = float(total)
                    except (TypeError, ValueError):
                        pass
                if spread_home is not None:
                    try:
                        entry["spread"] = float(spread_home)
                    except (TypeError, ValueError):
                        pass

                if entry and home_abbr:
                    result[home_abbr] = dict(entry)
                if entry and away_abbr:
                    away_entry = dict(entry)
                    if "spread" in away_entry:
                        away_entry["spread"] = -away_entry["spread"]
                    result[away_abbr] = away_entry

            ou_count = sum(1 for v in result.values() if "over_under" in v) // 2
            sp_count = sum(1 for v in result.values() if "spread" in v) // 2
            if result:
                logger.info(
                    f"[Odds] BDL fallback: {ou_count} games with O/U, "
                    f"{sp_count} with spreads"
                )
            return result

        except Exception as e:
            logger.debug(f"[Odds] BDL odds fetch failed: {e}")
            return {}

    def _fetch_odds_api(
        self, target_date: str
    ) -> Dict[str, Dict[str, float]]:
        """Fetch NBA odds via The Odds API (OddsService), keyed by team abbreviation.

        Returns mapping of team_abbreviation → ``{"over_under": float, "spread": float}``.
        The Odds API uses full team names; we map them to NBA abbreviations.
        Returns ``{}`` if the OddsService is not configured or fails.
        """
        svc = self._odds_service
        if not svc or not svc.is_available:
            return {}

        try:
            # OddsService returns "home_team vs away_team" → {over_under, spread}
            raw = svc.get_odds("nba", target_date)
            if not raw:
                return {}

            # Map full team names → NBA abbreviations
            # Use nba_api static teams for lookups
            _name_to_abbr: Dict[str, str] = {}
            for t in nba_teams.get_teams():
                full = t["full_name"].lower()
                city = t.get("city", "").lower()
                name = t.get("nickname", "").lower()
                abbr = t["abbreviation"]
                _name_to_abbr[full] = abbr
                if city:
                    _name_to_abbr[city] = abbr
                if name:
                    _name_to_abbr[name] = abbr
                # Also map "city nickname" patterns
                for word in full.split():
                    if len(word) > 4:
                        _name_to_abbr[word] = abbr

            result: Dict[str, Dict[str, float]] = {}
            for game_key, odds in raw.items():
                # game_key format: "Home Team vs Away Team"
                parts = game_key.split(" vs ", 1)
                if len(parts) != 2:
                    continue
                home_name, away_name = parts[0].strip(), parts[1].strip()

                # Resolve abbreviations
                home_abbr = _name_to_abbr.get(home_name.lower(), "")
                away_abbr = _name_to_abbr.get(away_name.lower(), "")

                if not home_abbr or not away_abbr:
                    continue

                entry: Dict[str, float] = {}
                if "over_under" in odds:
                    entry["over_under"] = odds["over_under"]
                if "spread" in odds:
                    entry["spread"] = odds["spread"]

                if entry:
                    result[home_abbr] = dict(entry)
                    away_entry = dict(entry)
                    if "spread" in away_entry:
                        away_entry["spread"] = -away_entry["spread"]
                    result[away_abbr] = away_entry

            if result:
                logger.info(
                    f"[Odds] The Odds API: {len(result) // 2} games with lines"
                )
            return result

        except Exception as e:
            logger.debug(f"[Odds] The Odds API fetch failed: {e}")
            return {}

    # ------------------------------------------------------------------
    # Projection math
    # ------------------------------------------------------------------

    @staticmethod
    def _project_game(
        home: TeamGameStats,
        away: TeamGameStats,
        vegas_over_under: Optional[float] = None,
        vegas_spread: Optional[float] = None,
    ) -> Dict[str, float]:
        """Project game totals using pace-adjusted scoring estimates.

        When Vegas lines are available they are blended with the model
        projection — Vegas totals are typically the most accurate pre-game
        predictor available.

        Method:
        1. Projected game pace = avg of both teams' pace
        2. Each team's adjusted rating = their off_rating + (LG_AVG - opp def_rating)
           This means: if opp defense is worse than average, the team scores more.
        3. Projected score = adjusted_rating × game_pace / 100
        4. Blend in last-5 trend (20% weight)
        5. Home court advantage: +2.5 pts
        6. **NEW** Vegas anchoring: blend model total/spread toward market lines
        """
        game_pace = (home.season_pace + away.season_pace) / 2

        # Home team scores against away defense
        # Standard matchup formula: off_rating + (lg_avg - opp_def)
        home_base = home.season_off_rating + (LEAGUE_AVG_OFF_RATING - away.season_def_rating)
        home_projected = home_base * game_pace / 100

        # Away team scores against home defense
        away_base = away.season_off_rating + (LEAGUE_AVG_OFF_RATING - home.season_def_rating)
        away_projected = away_base * game_pace / 100

        # Blend in L5 trend (20%)
        home_projected = 0.80 * home_projected + 0.20 * home.last_5_ppg
        away_projected = 0.80 * away_projected + 0.20 * away.last_5_ppg

        # Dynamic home-court advantage
        # Modern NBA HCA is ~2.5 points total (declining from ~3.5 in 2010s).
        # Split asymmetrically: home team gets most of the boost.
        # Scale slightly by net-rating gap — teams with similar strength
        # see a larger venue effect, while lopsided matchups are less
        # affected by crowd (the talent gap dominates).
        BASE_HCA = 2.5
        home_net = home.season_off_rating - home.season_def_rating
        away_net = away.season_off_rating - away.season_def_rating
        net_gap = abs(home_net - away_net)
        # Dampen HCA in lopsided matchups (gap > 10 → reduce to 70% of base)
        hca_scale = max(0.70, 1.0 - net_gap * 0.03)
        hca = BASE_HCA * hca_scale
        home_projected += hca * 0.65  # Home gets ~65% of the advantage
        away_projected -= hca * 0.35  # Away loses ~35%

        model_total = home_projected + away_projected
        model_spread = away_projected - home_projected  # negative = home favored

        # ── Vegas Line Anchoring ─────────────────────────────────────
        # When Vegas lines are available, blend model projections toward
        # them.  The blend preserves the model's relative team-strength
        # split while anchoring the totals to the market's view.
        total = model_total
        spread = model_spread

        if vegas_over_under is not None:
            # Divergence check (informational)
            divergence = abs(model_total - vegas_over_under) / max(vegas_over_under, 1.0)
            if divergence > VEGAS_DIVERGENCE_WARNING_THRESHOLD:
                logger.info(
                    f"Model total ({model_total:.1f}) diverges from Vegas O/U "
                    f"({vegas_over_under}) by {divergence:.1%}"
                )
            # Blend total toward Vegas
            total = (
                VEGAS_TOTAL_BLEND_WEIGHT * vegas_over_under
                + (1.0 - VEGAS_TOTAL_BLEND_WEIGHT) * model_total
            )

        if vegas_spread is not None:
            # Vegas spread is home-relative (negative = home favored), same
            # convention as our model_spread.
            spread = (
                VEGAS_SPREAD_BLEND_WEIGHT * vegas_spread
                + (1.0 - VEGAS_SPREAD_BLEND_WEIGHT) * model_spread
            )

        # Derive per-team scores from blended total + spread:
        #   total = home + away
        #   spread = away - home  (negative = home favored)
        # Solving: home = (total - spread) / 2
        #          away = (total + spread) / 2
        home_projected = (total - spread) / 2.0
        away_projected = (total + spread) / 2.0

        # Pace label
        if game_pace >= 101.5:
            pace_label = "Fast"
        elif game_pace >= 97.5:
            pace_label = "Average"
        else:
            pace_label = "Slow"

        return {
            "projected_home_score": round(home_projected, 1),
            "projected_away_score": round(away_projected, 1),
            "projected_total": round(total, 1),
            "projected_spread": round(spread, 1),
            "projected_pace": round(game_pace, 1),
            "pace_label": pace_label,
        }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_games(self, game_date: Optional[str] = None) -> Schedule:
        """Build schedule with projections for a given date.

        Results are cached per date within the same service instance,
        so repeat calls (e.g. from B2B checks and rotation routes)
        don't trigger additional NBA API requests.

        Args:
            game_date: Date string in YYYY-MM-DD format.
                       Defaults to today if not provided.
        """
        target_date = game_date or date.today().isoformat()

        # Return cached schedule if available (with TTL for today's date)
        if target_date in self._schedule_cache:
            _is_today = (target_date == date.today().isoformat())
            _cache_age = time.time() - self._schedule_cache_ts.get(target_date, 0.0)
            if not _is_today or _cache_age < self._SCHEDULE_TTL_TODAY_S:
                return self._schedule_cache[target_date]
            # Today's cache is stale — re-fetch so game statuses update
            logger.info(
                "[Schedule] Today's cache is %.0fs old (TTL=%.0fs) — "
                "refreshing game statuses",
                _cache_age,
                self._SCHEDULE_TTL_TODAY_S,
            )
            del self._schedule_cache[target_date]
            del self._schedule_cache_ts[target_date]

        # For future dates, skip ScoreboardV2 entirely — the NBA API
        # doesn't serve schedules for dates that haven't started yet
        # and will hang/timeout (~33s wasted).  Go straight to DK
        # fallback which resolves from the DraftKings lobby instantly.
        try:
            target_dt = date.fromisoformat(target_date)
        except ValueError:
            target_dt = None

        if target_dt and target_dt > date.today():
            logger.info(
                "Target date %s is in the future — skipping ScoreboardV2, "
                "using DK fallback directly",
                target_date,
            )
            schedule = self._build_schedule_from_dk(target_date)
            if schedule.game_count > 0:
                self._schedule_cache[target_date] = schedule
                self._schedule_cache_ts[target_date] = time.time()
            return schedule

        # 1. Get scoreboard for the target date
        #    Try multi-source (BDL → NBA API) first, direct ScoreboardV2 as
        #    final live fallback.  Line scores only available from direct call.
        game_headers = None
        line_scores = []
        _skip_nba_api = False  # True when BDL sourced the scoreboard

        if self._data_service is not None:
            try:
                game_headers = self._data_service.get_scoreboard_for_date(
                    target_date
                )
                if game_headers:
                    # BDL sourced the scoreboard — skip per-team NBA API
                    # calls (last-5 PPG) which may timeout when stats.nba.com
                    # is unreachable.  Season PPG is used instead (20% blend).
                    _skip_nba_api = True
                    logger.info(
                        f"[GameService] Scoreboard via data_service: "
                        f"{len(game_headers)} games"
                    )
            except Exception as e:
                logger.warning(
                    f"[GameService] data_service scoreboard failed: {e}"
                )

        # Fallback: direct ScoreboardV2 (stats.nba.com) — only in background mode
        if not game_headers and not settings.skip_nba_api_live:
            def _fetch_scoreboard():
                sb = scoreboardv2.ScoreboardV2(
                    game_date=target_date,
                    timeout=settings.nba_api_timeout,
                )
                return sb.get_normalized_dict()

            try:
                sb_data = self._retry_request(_fetch_scoreboard)
                game_headers = sb_data.get("GameHeader", [])
                line_scores = sb_data.get("LineScore", [])
            except Exception as e:
                logger.error(f"Failed to fetch scoreboard: {e}")
                logger.info("Attempting DraftKings fallback for game schedule")
                return self._build_schedule_from_dk(target_date)
        elif not game_headers and settings.skip_nba_api_live:
            logger.info(
                "[GameService] BDL scoreboard empty, skipping stats.nba.com "
                "(skip_nba_api_live=True) — falling through to DK fallback"
            )

        if not game_headers:
            logger.info("All scoreboard sources returned no games, trying DK fallback")
            return self._build_schedule_from_dk(target_date)

        # Filter out All-Star / special event games and deduplicate.
        # NBA game IDs: "002" = regular season, "004" = playoffs.
        # "003" = All-Star events (Rising Stars, Skills, All-Star Game).
        # BDL game IDs are short integers — use franchise ID validation instead.
        _valid_franchise_ids = set(_NBA_ABBR_TO_ID.values())
        seen_game_ids: set = set()
        filtered_headers = []
        for h in game_headers:
            gid = h.get("GAME_ID", "")
            home_id = h.get("HOME_TEAM_ID")
            away_id = h.get("VISITOR_TEAM_ID")

            # Skip All-Star / special events:
            # - NBA API: game ID prefix "003"
            # - BDL: validate team IDs are real NBA franchises
            if gid.startswith("003"):
                continue
            if h.get("_source") == "balldontlie":
                if home_id not in _valid_franchise_ids or away_id not in _valid_franchise_ids:
                    logger.debug(f"Filtering non-franchise game from BDL: {gid}")
                    continue

            # Skip entries with missing team IDs
            if not home_id or not away_id:
                continue

            # Deduplicate by game_id
            if gid in seen_game_ids:
                continue
            seen_game_ids.add(gid)

            filtered_headers.append(h)

        game_headers = filtered_headers

        if not game_headers:
            logger.info("All games filtered out (All-Star?), trying DK fallback")
            return self._build_schedule_from_dk(target_date)

        # 2. Get league-wide stats
        all_stats = self._get_all_team_stats()

        # 3. Build line-score lookup for live scores
        live_scores: Dict[str, Dict] = {}
        for ls in line_scores:
            gid = ls.get("GAME_ID", "")
            tid = ls.get("TEAM_ID", 0)
            if gid not in live_scores:
                live_scores[gid] = {}
            live_scores[gid][tid] = {
                "pts": ls.get("PTS", 0),
            }

        # 4. Fetch live odds via OddsFetcherService (API → BDL → heuristic)
        #    When the unified fetcher is wired, it handles the full chain
        #    and guarantees a result for every game.  Fall back to the
        #    legacy chain only when the new service is not yet injected.
        odds_map: Dict[str, Dict[str, float]] = {}
        _team_odds: Dict[str, Dict[str, float]] = {}

        if self._odds_fetcher is not None:
            _team_odds = self._odds_fetcher.get_game_odds(
                target_date, all_stats, game_headers
            )
        else:
            # Legacy path: dead NBA API → BDL → The Odds API
            odds_map = self._fetch_odds()
            if not odds_map:
                _team_odds = self._fetch_odds_bdl(target_date)
            if not odds_map and not _team_odds:
                _team_odds = self._fetch_odds_api(target_date)

        # 5. Build each GameInfo
        games: List[GameInfo] = []
        for header in game_headers:
            try:
                game_id = header.get("GAME_ID", "")
                home_id = header.get("HOME_TEAM_ID", 0)
                away_id = header.get("VISITOR_TEAM_ID", 0)
                status_id = header.get("GAME_STATUS_ID", 1)

                if status_id == 1:
                    game_status = "Scheduled"
                elif status_id == 2:
                    game_status = "In Progress"
                else:
                    game_status = "Final"

                game_time_raw = header.get("GAME_STATUS_TEXT", "")
                game_sequence = header.get("GAME_SEQUENCE", 0)

                # GAME_STATUS_TEXT only contains the time for scheduled
                # games (status_id == 1). For in-progress/final it shows
                # "Final", "3rd Qtr", etc.  Preserve the original time if
                # parseable, otherwise store the raw text for display.
                game_time = game_time_raw.strip() if game_time_raw else None

                home_stats = self._build_team_game_stats(
                    home_id, all_stats, skip_api=_skip_nba_api
                )
                away_stats = self._build_team_game_stats(
                    away_id, all_stats, skip_api=_skip_nba_api
                )

                # Fetch Vegas lines *before* projection so they can be
                # blended into the model output.
                game_odds = odds_map.get(game_id, {})
                if not game_odds and _team_odds:
                    # Fallback odds are keyed by team abbreviation
                    game_odds = _team_odds.get(
                        home_stats.team_abbreviation, {}
                    )
                ou_line = game_odds.get("over_under")
                vegas_spread = game_odds.get("spread")  # home-relative

                projection = self._project_game(
                    home_stats,
                    away_stats,
                    vegas_over_under=ou_line,
                    vegas_spread=vegas_spread,
                )

                # Over/under edge = our blended total vs. the raw Vegas line
                ou_edge = None
                if ou_line is not None:
                    ou_edge = round(projection["projected_total"] - ou_line, 1)

                games.append(GameInfo(
                    game_id=game_id,
                    game_date=target_date,
                    game_time_et=game_time,
                    game_sequence=game_sequence,
                    game_status=game_status,
                    home_team=home_stats,
                    away_team=away_stats,
                    projected_total=projection["projected_total"],
                    projected_home_score=projection["projected_home_score"],
                    projected_away_score=projection["projected_away_score"],
                    projected_spread=projection["projected_spread"],
                    projected_pace=projection["projected_pace"],
                    pace_label=projection["pace_label"],
                    over_under=ou_line,
                    over_under_edge=ou_edge,
                    vegas_spread=vegas_spread,
                ))
            except Exception as e:
                logger.error(f"Failed to build game info for {header.get('GAME_ID')}: {e}")
                continue

        slates = self._build_slates(games, target_date)

        schedule = Schedule(
            date=target_date,
            game_count=len(games),
            games=games,
            slates=slates,
        )

        # Cache the result so repeat calls for the same date are free
        self._schedule_cache[target_date] = schedule
        self._schedule_cache_ts[target_date] = time.time()

        return schedule

    # ------------------------------------------------------------------
    # DFS slate grouping (DraftKings API-based)
    # ------------------------------------------------------------------

    def _build_slates(self, games: List[GameInfo], target_date: str = None) -> List[DFSSlate]:
        """Group games into DK-style time-based slates using real DK API data.

        Fetches actual DraftKings Classic DraftGroups and matches each
        game to the corresponding NBA API GameInfo by team abbreviation.
        Falls back to a single "All Games" slate if DK data is unavailable.
        """
        if not games:
            return []

        # Build lookup: team abbreviation → GameInfo list
        abbr_to_games: Dict[str, List[GameInfo]] = {}
        for game in games:
            home_abbr = game.home_team.team_abbreviation
            away_abbr = game.away_team.team_abbreviation
            abbr_to_games.setdefault(home_abbr, []).append(game)
            abbr_to_games.setdefault(away_abbr, []).append(game)

        # Fetch real DK slates
        dk_slates = self._dk_slate_service.get_slates(target_date)

        if not dk_slates:
            logger.info("No DK slates available, using single 'All Games' slate")
            return self._fallback_single_slate(games)

        slates: List[DFSSlate] = []

        for dk_slate in dk_slates:
            matched_games: List[GameInfo] = []
            matched_game_ids: set = set()

            for dk_game in dk_slate.games:
                away_abbr = dk_game.get("away_abbr", "")
                home_abbr = dk_game.get("home_abbr", "")

                # Normalize DK abbreviations to NBA API standard
                away_abbr = DK_TO_NBA_ABBR_ALIASES.get(away_abbr, away_abbr)
                home_abbr = DK_TO_NBA_ABBR_ALIASES.get(home_abbr, home_abbr)

                # Fallback: use DK team ID → NBA abbreviation mapping
                if not away_abbr or away_abbr == "???":
                    dk_away_id = dk_game.get("away_team_dk_id")
                    if dk_away_id:
                        away_abbr = DK_TEAM_ID_TO_NBA_ABBR.get(dk_away_id, away_abbr)
                if not home_abbr or home_abbr == "???":
                    dk_home_id = dk_game.get("home_team_dk_id")
                    if dk_home_id:
                        home_abbr = DK_TEAM_ID_TO_NBA_ABBR.get(dk_home_id, home_abbr)

                # Find matching GameInfo by team abbreviation
                match = self._find_matching_game(
                    away_abbr, home_abbr, games, matched_game_ids
                )
                if match:
                    matched_games.append(match)
                    matched_game_ids.add(match.game_id)
                else:
                    logger.warning(
                        "[Slate] No NBA game match for DK game: %s (resolved: %s @ %s)",
                        dk_game.get("description", "?"), away_abbr, home_abbr,
                    )

            if matched_games:
                # Sort by game_sequence within each slate
                matched_games.sort(key=lambda g: g.game_sequence)
                slates.append(DFSSlate(
                    name=dk_slate.name,
                    label=dk_slate.label,
                    game_count=len(matched_games),
                    games=matched_games,
                    draft_group_id=dk_slate.draft_group_id,
                ))

        if not slates:
            logger.warning("DK slates fetched but no games matched, falling back")
            return self._fallback_single_slate(games)

        # ── Create a separate slate for orphaned (locked) games ────────
        # DK removes slates from the lobby once they lock (typically
        # ~15 min before tip-off).  Games that already started won't
        # appear in any DK DraftGroup, but they should still be visible
        # on the dashboard.  Create a new slate for them so that
        # existing DK slates (e.g. Late/Night) stay distinct.
        #
        # IMPORTANT: Only include games that are live or final.
        # Pre-game games not in any Classic DraftGroup are typically
        # Showdown-only matchups (e.g. single-game DGs with
        # gameTypeId=81) and should NOT generate a phantom slate.
        # The sticky cache in DKSlateService already preserves locked
        # Classic DGs for late-swap, so truly orphaned pre-game
        # Classic games are extremely unlikely.
        slated_game_ids = set()
        for sl in slates:
            for g in sl.games:
                slated_game_ids.add(g.game_id)

        _not_slated = [g for g in games if g.game_id not in slated_game_ids]
        orphaned = [
            g for g in _not_slated
            if g.game_status in ("In Progress", "Final")
        ]
        if _not_slated and not orphaned:
            _skipped = [
                f"{g.away_team.team_abbreviation}@{g.home_team.team_abbreviation}"
                for g in _not_slated
            ]
            logger.info(
                "[Slate] Skipping %d pre-game games not in any Classic DG "
                "(likely Showdown-only): %s", len(_skipped), _skipped,
            )
        if orphaned:
            orphaned.sort(key=lambda g: g.game_sequence)
            orphan_slate = DFSSlate(
                name="Main",
                label=f"Main — {len(orphaned)} games",
                game_count=len(orphaned),
                games=orphaned,
                draft_group_id=None,
            )
            # Insert orphan slate at the front (these are earlier games)
            slates.insert(0, orphan_slate)
            logger.info(
                f"[Slate] Created 'Main' slate for {len(orphaned)} orphaned "
                f"games from locked DK slates."
            )

            # Re-name all slates now that we have an extra one
            self._rename_slates_with_orphans(slates)

        # ── Compute is_live / late_swap_active per slate ──────────────
        for sl in slates:
            has_in_progress = any(
                g.game_status == "In Progress" for g in sl.games
            )
            all_final = all(
                g.game_status == "Final" for g in sl.games
            )
            # A slate is "live" if at least one game has tipped off
            # but not all games are final (still worth showing).
            sl.is_live = has_in_progress or (
                not all_final and any(
                    g.game_status == "Final" for g in sl.games
                )
            )
            # Late-swap is only possible when we have a DK DraftGroup ID
            # (needed for player pool / salary data) and the slate is live.
            sl.late_swap_active = sl.is_live and sl.draft_group_id is not None

        return slates

    @staticmethod
    def _find_matching_game(
        away_abbr: str,
        home_abbr: str,
        games: List[GameInfo],
        exclude_ids: set,
    ) -> Optional[GameInfo]:
        """Find a GameInfo matching the given away/home abbreviations."""
        for game in games:
            if game.game_id in exclude_ids:
                continue
            if (game.away_team.team_abbreviation == away_abbr
                    and game.home_team.team_abbreviation == home_abbr):
                return game
        return None

    @staticmethod
    def _fallback_single_slate(games: List[GameInfo]) -> List[DFSSlate]:
        """Create a single 'All Games' slate when DK data is unavailable."""
        sorted_games = sorted(games, key=lambda g: g.game_sequence)
        has_live = any(g.game_status == "In Progress" for g in sorted_games)
        has_final = any(g.game_status == "Final" for g in sorted_games)
        all_final = all(g.game_status == "Final" for g in sorted_games)
        is_live = has_live or (has_final and not all_final)
        return [
            DFSSlate(
                name="Main",
                label=f"All Games ({len(sorted_games)})",
                game_count=len(sorted_games),
                games=sorted_games,
                is_live=is_live,
                late_swap_active=False,  # No DK DraftGroup ID in fallback
            )
        ]

    @staticmethod
    def _rename_slates_with_orphans(slates: List[DFSSlate]):
        """Re-name slates after inserting an orphan slate at index 0.

        The orphan slate (locked/in-progress games) is always first.
        Remaining DK slates are renamed based on position:
        - 2 total → "Main" (orphans), "Late" (DK)
        - 3 total → "Main" (orphans), "Late" (DK), "Night" (DK)
        - 4+ total → "Main", "Late", "Late 2", ..., "Night"
        """
        if len(slates) <= 1:
            return

        # First slate is always the orphan slate → "Main"
        slates[0].name = "Main"
        slates[0].label = f"Main — {slates[0].game_count} games"

        if len(slates) == 2:
            slates[1].name = "Late"
            slates[1].label = f"Late — {slates[1].game_count} games"
        else:
            for i in range(1, len(slates)):
                if i == len(slates) - 1:
                    slates[i].name = "Night"
                elif i == 1:
                    slates[i].name = "Late"
                else:
                    slates[i].name = f"Late {i}"
                slates[i].label = f"{slates[i].name} — {slates[i].game_count} games"

    # ------------------------------------------------------------------
    # DraftKings fallback (when ScoreboardV2 is unavailable)
    # ------------------------------------------------------------------

    def _build_schedule_from_dk(self, target_date: str) -> Schedule:
        """Build a Schedule from DraftKings slate data when NBA API is down.

        Uses DK draft-group games as the source of matchups and start
        times, then enriches each game with team stats (from DB cache or
        ``LeagueDashTeamStats``) and pace-model projections.

        Vegas odds are unavailable in this path because the NBA live-odds
        endpoint keys on NBA game_id.  The frontend already handles
        ``over_under=None`` gracefully.
        """
        dk_slates = self._dk_slate_service.get_slates(target_date)
        if not dk_slates:
            logger.info("DK fallback: no slates found for %s", target_date)
            return Schedule(date=target_date, game_count=0, games=[])

        # Deduplicate games across slates (Main includes Early's games, etc.)
        seen_matchups: set = set()
        unique_dk_games: List[Dict] = []
        for slate in dk_slates:
            for dk_game in slate.games:
                key = (dk_game.get("away_abbr", ""), dk_game.get("home_abbr", ""))
                if key not in seen_matchups and key != ("???", "???"):
                    seen_matchups.add(key)
                    unique_dk_games.append(dk_game)

        if not unique_dk_games:
            return Schedule(date=target_date, game_count=0, games=[])

        # Sort by start time so game_sequence is correct
        unique_dk_games.sort(
            key=lambda g: g.get("start_utc") or datetime.max.replace(tzinfo=timezone.utc)
        )

        # ET timezone for game_time display
        try:
            import zoneinfo
            eastern = zoneinfo.ZoneInfo("America/New_York")
        except Exception:
            eastern = None

        # Team stats for projections — prefer cache, but don't block on
        # slow NBA API calls.  If both caches miss, league-average defaults
        # are used (handled gracefully in _build_team_game_stats).
        all_stats = self._get_all_team_stats(skip_api=True)

        games: List[GameInfo] = []
        for seq, dk_game in enumerate(unique_dk_games, start=1):
            away_abbr = dk_game.get("away_abbr", "???")
            home_abbr = dk_game.get("home_abbr", "???")

            # Normalize DK abbreviations to NBA API standard
            away_abbr = DK_TO_NBA_ABBR_ALIASES.get(away_abbr, away_abbr)
            home_abbr = DK_TO_NBA_ABBR_ALIASES.get(home_abbr, home_abbr)

            # Fallback: DK team ID → NBA abbreviation
            if away_abbr == "???" or not away_abbr:
                dk_away_id = dk_game.get("away_team_dk_id")
                if dk_away_id:
                    away_abbr = DK_TEAM_ID_TO_NBA_ABBR.get(dk_away_id, away_abbr)
            if home_abbr == "???" or not home_abbr:
                dk_home_id = dk_game.get("home_team_dk_id")
                if dk_home_id:
                    home_abbr = DK_TEAM_ID_TO_NBA_ABBR.get(dk_home_id, home_abbr)

            away_id = _NBA_ABBR_TO_ID.get(away_abbr)
            home_id = _NBA_ABBR_TO_ID.get(home_abbr)

            if not away_id or not home_id:
                logger.warning(
                    "DK fallback: unknown team abbreviation %s or %s, skipping",
                    away_abbr, home_abbr,
                )
                continue

            away_stats = self._build_team_game_stats(
                away_id, all_stats, skip_api=True
            )
            home_stats = self._build_team_game_stats(
                home_id, all_stats, skip_api=True
            )

            projection = self._project_game(home_stats, away_stats)

            # Convert DK UTC start → ET display string
            game_time_et = None
            start_utc = dk_game.get("start_utc")
            if start_utc:
                if eastern is not None:
                    et_dt = start_utc.astimezone(eastern)
                else:
                    et_dt = start_utc + timedelta(hours=-5)
                hour = et_dt.hour % 12 or 12
                ampm = "AM" if et_dt.hour < 12 else "PM"
                game_time_et = f"{hour}:{et_dt.strftime('%M')} {ampm} ET"

            # Synthesize a game_id from DK data
            dk_gid = dk_game.get("dk_game_id", seq)
            game_id = f"dk_{dk_gid}"

            # Infer game status from start time (DK fallback doesn't
            # have live score data, but we can detect started games).
            _now_utc = datetime.now(timezone.utc)
            if start_utc and start_utc <= _now_utc - timedelta(hours=3):
                _dk_game_status = "Final"
            elif start_utc and start_utc <= _now_utc:
                _dk_game_status = "In Progress"
            else:
                _dk_game_status = "Scheduled"

            games.append(GameInfo(
                game_id=game_id,
                game_date=target_date,
                game_time_et=game_time_et,
                game_sequence=seq,
                game_status=_dk_game_status,
                home_team=home_stats,
                away_team=away_stats,
                projected_total=projection["projected_total"],
                projected_home_score=projection["projected_home_score"],
                projected_away_score=projection["projected_away_score"],
                projected_spread=projection["projected_spread"],
                projected_pace=projection["projected_pace"],
                pace_label=projection["pace_label"],
                over_under=None,
                over_under_edge=None,
                vegas_spread=None,
            ))

        logger.info(
            "ScoreboardV2 unavailable — built schedule from DraftKings (%d games)",
            len(games),
        )

        slates = self._build_slates(games, target_date)

        schedule = Schedule(
            date=target_date,
            game_count=len(games),
            games=games,
            slates=slates,
        )
        self._schedule_cache[target_date] = schedule
        self._schedule_cache_ts[target_date] = time.time()
        return schedule

    def get_team_game(self, team_id: int, game_date: Optional[str] = None) -> Optional[GameInfo]:
        """Get game for a specific team on a given date, or None if no game."""
        schedule = self.get_games(game_date)
        for game in schedule.games:
            if game.home_team.team_id == team_id or game.away_team.team_id == team_id:
                return game
        return None

    def has_game_on_date(self, team_id: int, game_date: str) -> bool:
        """Lightweight check: did this team play on a given date?

        Only fetches the raw scoreboard headers (a single API call)
        without building full GameInfo objects (which require league
        stats and per-team last-5 PPG).  Used for B2B detection where
        we only need a yes/no answer, not full projections.

        Uses multi-source data service (BDL → NBA API) when available.
        """
        # Check the full schedule cache first — if get_games() was
        # already called for this date, reuse it for free.
        if game_date in self._schedule_cache:
            sched = self._schedule_cache[game_date]
            return any(
                g.home_team.team_id == team_id or g.away_team.team_id == team_id
                for g in sched.games
            )

        # Check the lightweight header cache
        if game_date in self._scoreboard_header_cache:
            headers = self._scoreboard_header_cache[game_date]
        else:
            headers = []

            # Try multi-source (BDL → NBA API) first
            if self._data_service is not None:
                try:
                    headers = self._data_service.get_scoreboard_for_date(
                        game_date
                    )
                except Exception:
                    headers = []

            # Direct ScoreboardV2 fallback — only when stats.nba.com is enabled
            if not headers and not settings.skip_nba_api_live:
                def _fetch():
                    sb = scoreboardv2.ScoreboardV2(
                        game_date=game_date,
                        timeout=settings.nba_api_timeout,
                    )
                    return sb.get_normalized_dict().get("GameHeader", [])

                try:
                    headers = self._retry_request(_fetch)
                except Exception as e:
                    logger.warning(
                        f"B2B scoreboard check failed for {game_date}: {e}"
                    )
                    self._scoreboard_header_cache[game_date] = []
                    return False
            elif not headers:
                logger.debug(
                    f"[GameService] B2B check: no scoreboard data for "
                    f"{game_date}, skip_nba_api_live=True"
                )
                self._scoreboard_header_cache[game_date] = []
                return False

            self._scoreboard_header_cache[game_date] = headers

        return any(
            h.get("HOME_TEAM_ID") == team_id or h.get("VISITOR_TEAM_ID") == team_id
            for h in headers
        )
