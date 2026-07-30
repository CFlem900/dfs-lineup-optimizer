"""CBB game-day projections service.

Parallel to :class:`GameService` but uses ESPN data for NCAA basketball.
Provides game-level projections (pace, totals, spread) and DvP matchup
factors for the simulation engine and DFS projections.

Data sources:
    - ESPN public API: scoreboard, team records
    - CBBStatsService: real pace, efficiency, DvP (from CBBpy box scores)

The public API (``get_games``, ``get_dvp_matchup_factors``) is **synchronous**
to maintain interface compatibility with :class:`GameService`.  When a
``CBBStatsService`` is injected, call :meth:`warm_stats_for_slate` (async)
from a router to pre-populate the cache with real stats before the sync
methods are invoked.
"""

import asyncio
import logging
import time
from datetime import date, datetime
from typing import Any, Dict, List, Optional, TYPE_CHECKING

import httpx

from app.services.http_resilience import resilient_get, APIGroup
from app.config.constants import (
    CBB_LEAGUE_AVG_PACE,
    CBB_LEAGUE_AVG_OFF_RATING,
    CBB_LEAGUE_AVG_PPG,
    CBB_LEAGUE_AVG_STATS_PG,
    CBB_VEGAS_TOTAL_BLEND_WEIGHT,
    CBB_VEGAS_SPREAD_BLEND_WEIGHT,
)
from app.models.game import DFSSlate, GameInfo, Schedule, TeamGameStats
from app.services.cbb_teams import CBBTeamRegistry
from app.services.dk_slate_service import DKSlateService

if TYPE_CHECKING:
    from app.services.cbb_stats_service import CBBStatsService
    from app.services.odds_service import OddsService

logger = logging.getLogger(__name__)

_ESPN_BASE = (
    "https://site.api.espn.com/apis/site/v2/sports/basketball/"
    "mens-college-basketball"
)

# DK → ESPN abbreviation mapping for CBB game matching.
# The lineup_optimizer_service has a separate map for player-pool building;
# this one is specifically for DK game ↔ ESPN game pairing in slates.
_DK_TO_ESPN_GAME_ABBR: Dict[str, str] = {
    "BAMA": "ALA",       # Alabama
    "VAND": "VAN",       # Vanderbilt
    "MIZZ": "MIZ",       # Missouri
    "CRE": "CREI",       # Creighton
    "UCONN": "CONN",     # UConn
    "STJN": "SJU",       # St. John's
    "IL": "ILL",         # Illinois
    "HALL": "SHU",       # Seton Hall
    "GTOWN": "GTWN",     # Georgetown
    "TXAM": "TA&M",      # Texas A&M
    "NCST": "NCSU",      # NC State
    "PURD": "PUR",       # Purdue
    "WISC": "WIS",       # Wisconsin
    "MIOH": "M-OH",      # Miami (OH)
    "UH": "HOU",         # Houston
    "WASH": "WASH",      # Washington
    "PITT": "PITT",      # Pittsburgh
}


class CBBGameService:
    """Service for CBB game-day projections using ESPN data + real stats.

    When a ``stats_service`` is injected, :meth:`warm_stats_for_slate`
    pre-populates ``_real_stats_cache`` with real per-team pace,
    efficiency, and DvP.  The sync ``get_games`` and
    ``get_dvp_matchup_factors`` methods read from that cache.
    """

    def __init__(
        self,
        stats_service: Optional["CBBStatsService"] = None,
        odds_service: Optional["OddsService"] = None,
    ):
        self._team_registry = CBBTeamRegistry()
        self._stats_service = stats_service
        self._odds_service = odds_service
        self._dk_slate_service = DKSlateService()

        # ESPN-sourced team data (records, league-avg defaults)
        self._team_stats_cache: Optional[Dict[int, Dict]] = None
        self._team_stats_cache_ts: float = 0.0
        self._team_stats_cache_ttl = 3600  # 1 hour

        # Real computed stats from CBBStatsService (pre-warmed)
        self._real_stats_cache: Dict[int, Dict] = {}
        self._real_stats_ts: float = 0.0
        self._real_stats_ttl = 14400  # 4 hours

        self._schedule_cache: Dict[str, Schedule] = {}

    # ------------------------------------------------------------------
    # Async pre-warming (called from routers before sync methods)
    # ------------------------------------------------------------------

    async def warm_stats_for_slate(self, game_date: str = None) -> int:
        """Pre-populate real stats cache for all teams on today's slate.

        Call this from async router handlers **before** calling the sync
        ``get_games()`` or ``get_dvp_matchup_factors()`` methods.

        The warm-up runs with a **30-second timeout** so that the
        scoreboard endpoint can still return ESPN-only data promptly
        even when CBBpy is slow.  A background task continues warming
        after the timeout if needed.

        Returns the number of teams warmed.
        """
        if not self._stats_service:
            return 0

        now = time.time()
        if self._real_stats_cache and (now - self._real_stats_ts) < self._real_stats_ttl:
            return len(self._real_stats_cache)

        if not game_date:
            game_date = date.today().isoformat()

        raw_games = self._fetch_scoreboard(game_date)
        if not raw_games:
            return 0

        # Collect unique team IDs and names
        teams_needed: list = []
        seen: set = set()
        for game in raw_games:
            for side in ("home", "away"):
                tid = game[side]["id"]
                if tid not in seen:
                    seen.add(tid)
                    teams_needed.append((tid, game[side]["name"]))

        if not teams_needed:
            return 0

        try:
            real_stats = await asyncio.wait_for(
                self._stats_service.get_all_team_stats(teams_needed),
                timeout=30.0,
            )
            # Merge with ESPN records
            espn_stats = self._get_espn_team_stats_cache()
            for tid, stats in real_stats.items():
                espn = espn_stats.get(tid, {})
                stats.setdefault("wins", espn.get("wins", 0))
                stats.setdefault("losses", espn.get("losses", 0))
                stats.setdefault("abbreviation", espn.get("abbreviation", ""))

            self._real_stats_cache = real_stats
            self._real_stats_ts = time.time()
            logger.info(
                f"Warmed real CBB stats for {len(real_stats)} slate teams"
            )
            return len(real_stats)
        except asyncio.TimeoutError:
            logger.warning(
                "CBB stats warm-up timed out (30s) — scoreboard will "
                "use ESPN defaults.  Warming continues in background."
            )
            # Fire-and-forget: continue warming in the background so the
            # *next* scoreboard request can use real stats from cache.
            # But skip if the prewarm pool build is likely still running
            # — we don't want both competing for the global CBBpy lock.
            from app.services.cbbpy_throttle import _cbbpy_lock
            if _cbbpy_lock.acquire(blocking=False):
                _cbbpy_lock.release()
                # Lock is free — safe to start background warm
                asyncio.create_task(self._background_warm(teams_needed))
            else:
                logger.info(
                    "CBB background warm skipped — CBBpy lock is held "
                    "(prewarm likely running)"
                )
            return 0
        except Exception as e:
            logger.warning(f"CBB stats warm-up failed: {e}")
            return 0

    async def _background_warm(
        self, teams_needed: list
    ) -> None:
        """Continue CBB stats warming in the background.

        Uses a generous 120s timeout to avoid tying up asyncio threads
        indefinitely if CBBpy is very slow.
        """
        try:
            real_stats = await asyncio.wait_for(
                self._stats_service.get_all_team_stats(teams_needed),
                timeout=120.0,
            )
            espn_stats = self._get_espn_team_stats_cache()
            for tid, stats in real_stats.items():
                espn = espn_stats.get(tid, {})
                stats.setdefault("wins", espn.get("wins", 0))
                stats.setdefault("losses", espn.get("losses", 0))
                stats.setdefault("abbreviation", espn.get("abbreviation", ""))

            self._real_stats_cache = real_stats
            self._real_stats_ts = time.time()
            logger.info(
                f"[Background] Warmed real CBB stats for "
                f"{len(real_stats)} teams"
            )
        except asyncio.TimeoutError:
            logger.warning(
                "[Background] CBB stats warm-up timed out (120s) — "
                "some teams will use league averages"
            )
        except Exception as e:
            logger.warning(f"[Background] CBB stats warm-up failed: {e}")

    # ------------------------------------------------------------------
    # Public API (sync — matches GameService interface)
    # ------------------------------------------------------------------

    def get_games(self, game_date: str = None) -> Schedule:
        """Fetch and project CBB games for a given date.

        Parameters
        ----------
        game_date : str, optional
            Date in "YYYY-MM-DD" format. Defaults to today.
        """
        if not game_date:
            game_date = date.today().isoformat()

        # Check cache
        if game_date in self._schedule_cache:
            return self._schedule_cache[game_date]

        # Fetch scoreboard
        raw_games = self._fetch_scoreboard(game_date)

        # Get best available team stats (real if warmed, else ESPN defaults)
        team_stats = self._get_best_team_stats()

        # Fetch Vegas odds if available
        odds_map: Dict[str, Dict[str, float]] = {}
        if self._odds_service and self._odds_service.is_available:
            try:
                odds_map = self._odds_service.get_odds("cbb", game_date)
            except Exception as e:
                logger.warning(f"Odds fetch failed, projecting without Vegas: {e}")

        # Project each game
        game_infos: List[GameInfo] = []
        for game in raw_games:
            try:
                # Find Vegas lines for this game
                game_odds = None
                if odds_map and self._odds_service:
                    game_odds = self._odds_service.get_game_odds(
                        "cbb",
                        game["home"]["name"],
                        game["away"]["name"],
                        game_date,
                    )
                info = self._project_game(game, team_stats, game_odds)
                if info:
                    game_infos.append(info)
            except Exception as e:
                logger.error(f"Failed to project CBB game {game.get('id')}: {e}")

        # Build DK slates — same approach as NBA GameService
        slates = self._build_slates(game_infos, game_date)

        schedule = Schedule(
            date=game_date,
            game_count=len(game_infos),
            games=game_infos,
            slates=slates,
        )

        self._schedule_cache[game_date] = schedule
        return schedule

    def get_team_game(
        self, team_id: int, game_date: Optional[str] = None
    ) -> Optional[GameInfo]:
        """Get game for a specific team on a given date, or None."""
        schedule = self.get_games(game_date)
        for game in schedule.games:
            if (
                game.home_team.team_id == team_id
                or game.away_team.team_id == team_id
            ):
                return game
        return None

    def has_game_on_date(self, team_id: int, game_date: str) -> bool:
        """Lightweight check: does this team play on a given date?

        Reuses the schedule cache when available, otherwise fetches the
        ESPN scoreboard to check without building full GameInfo objects.
        """
        # Check full schedule cache first
        if game_date in self._schedule_cache:
            sched = self._schedule_cache[game_date]
            return any(
                g.home_team.team_id == team_id
                or g.away_team.team_id == team_id
                for g in sched.games
            )

        # Lightweight: just check raw scoreboard headers
        raw_games = self._fetch_scoreboard(game_date)
        for game in raw_games:
            if (
                game["home"]["id"] == team_id
                or game["away"]["id"] == team_id
            ):
                return True
        return False

    # ------------------------------------------------------------------
    # DK slate matching
    # ------------------------------------------------------------------

    def _build_slates(
        self, games: List[GameInfo], target_date: str
    ) -> List[DFSSlate]:
        """Build DK-linked slates for CBB games.

        Fetches DraftKings Classic DraftGroups for CBB and matches each
        game to our ESPN GameInfo objects.  Because DK and ESPN often
        use different team abbreviations for college teams, matching is
        done with a multi-layer approach (exact, normalized, substring).

        Falls back to a single "All Games" slate if no DK data.
        """
        if not games:
            return []

        # Build lookup: multiple keys → GameInfo for fuzzy matching
        abbr_to_game: Dict[str, GameInfo] = {}
        name_to_game: Dict[str, GameInfo] = {}
        for game in games:
            h = game.home_team.team_abbreviation.upper()
            a = game.away_team.team_abbreviation.upper()
            abbr_to_game[h] = game
            abbr_to_game[a] = game
            # Also index by full team name (lowered) for fuzzy matching
            h_name = game.home_team.team_name.lower() if game.home_team.team_name else ""
            a_name = game.away_team.team_name.lower() if game.away_team.team_name else ""
            if h_name:
                name_to_game[h_name] = game
            if a_name:
                name_to_game[a_name] = game

        # Fetch DK slates for CBB
        dk_slates = self._dk_slate_service.get_slates(target_date, sport="cbb")
        if not dk_slates:
            logger.info("No DK CBB slates available, using single 'All Games' slate")
            return self._fallback_single_slate(games)

        slates: List[DFSSlate] = []

        for dk_slate in dk_slates:
            matched_games: List[GameInfo] = []
            matched_ids: set = set()

            for dk_game in dk_slate.games:
                away_dk = (dk_game.get("away_abbr") or "").upper()
                home_dk = (dk_game.get("home_abbr") or "").upper()

                match = self._match_cbb_game(
                    away_dk, home_dk, games, matched_ids,
                    abbr_to_game, name_to_game,
                )
                if match:
                    matched_games.append(match)
                    matched_ids.add(match.game_id)
                else:
                    logger.warning(
                        f"[CBB] DK game unmatched: {away_dk} @ {home_dk} "
                        f"(DG={dk_slate.draft_group_id})"
                    )

            if matched_games:
                matched_games.sort(
                    key=lambda g: g.game_sequence
                    if hasattr(g, "game_sequence") and g.game_sequence
                    else 0
                )
                slates.append(DFSSlate(
                    name=dk_slate.name,
                    label=dk_slate.label,
                    game_count=len(matched_games),
                    games=matched_games,
                    draft_group_id=dk_slate.draft_group_id,
                ))

        if not slates:
            logger.warning(
                "DK CBB slates fetched but no games matched ESPN data, "
                "falling back to 'All Games'"
            )
            return self._fallback_single_slate(games)

        logger.info(
            f"Built {len(slates)} CBB slates: "
            + ", ".join(f"{s.name} ({s.game_count}g, DG={s.draft_group_id})" for s in slates)
        )
        return slates

    @staticmethod
    def _match_cbb_game(
        away_dk: str,
        home_dk: str,
        games: List[GameInfo],
        exclude_ids: set,
        abbr_to_game: Dict[str, GameInfo],
        name_to_game: Dict[str, GameInfo],
    ) -> Optional[GameInfo]:
        """Match a DK game to an ESPN GameInfo using multi-layer matching.

        1. Exact abbreviation match (raw + normalized via _DK_TO_ESPN_GAME_ABBR)
        2. Partial: at least one abbreviation matches (raw or normalized)
        3. Fuzzy: DK abbreviation is a substring of an ESPN team name
        """
        # Normalize DK abbreviations through known mapping
        away_norm = _DK_TO_ESPN_GAME_ABBR.get(away_dk, away_dk)
        home_norm = _DK_TO_ESPN_GAME_ABBR.get(home_dk, home_dk)

        # 1. Exact match: both away and home abbreviations match
        #    (check raw DK abbrs AND normalized abbrs)
        for game in games:
            if game.game_id in exclude_ids:
                continue
            espn_a = game.away_team.team_abbreviation.upper()
            espn_h = game.home_team.team_abbreviation.upper()
            if ((espn_a == away_dk or espn_a == away_norm)
                    and (espn_h == home_dk or espn_h == home_norm)):
                return game

        # 2. Partial: at least one abbreviation matches (raw or normalized)
        for game in games:
            if game.game_id in exclude_ids:
                continue
            espn_a = game.away_team.team_abbreviation.upper()
            espn_h = game.home_team.team_abbreviation.upper()
            if (espn_a in (away_dk, away_norm)
                    or espn_h in (home_dk, home_norm)):
                return game

        # 3. Fuzzy: DK abbreviation is a substring in ESPN team name
        away_low = away_dk.lower()
        home_low = home_dk.lower()
        for game in games:
            if game.game_id in exclude_ids:
                continue
            a_name = (game.away_team.team_name or "").lower()
            h_name = (game.home_team.team_name or "").lower()
            if ((away_low in a_name or a_name[:3] == away_low[:3])
                    and (home_low in h_name or h_name[:3] == home_low[:3])):
                return game

        return None

    @staticmethod
    def _fallback_single_slate(games: List[GameInfo]) -> List[DFSSlate]:
        """Create a single 'All Games' fallback slate (no DK DraftGroup)."""
        sorted_games = sorted(
            games,
            key=lambda g: g.game_sequence
            if hasattr(g, "game_sequence") and g.game_sequence
            else 0,
        )
        return [
            DFSSlate(
                name="All Games",
                label=f"All Games — {len(sorted_games)} games",
                game_count=len(sorted_games),
                games=sorted_games,
                draft_group_id=None,
            )
        ]

    def get_dvp_matchup_factors(
        self, opponent_team_id: int
    ) -> Dict[str, float]:
        """Compute DvP (Defense vs Position) matchup multipliers.

        Returns a dict of stat_name -> factor where > 1.0 means the
        opponent allows more of that stat (favorable matchup) and
        < 1.0 means they're tougher.

        Uses real stats from ``_real_stats_cache`` when available
        (populated by ``warm_stats_for_slate``), falling back to
        ESPN defaults.
        """
        # Check real stats first
        if opponent_team_id in self._real_stats_cache:
            return self._compute_dvp_from_stats(
                self._real_stats_cache[opponent_team_id]
            )

        # Try to compute on-demand if stats service available
        if self._stats_service:
            opp_info = self._team_registry.find_team_by_id(opponent_team_id)
            if opp_info:
                try:
                    # Attempt sync fetch from stats service cache
                    if opponent_team_id in self._stats_service._cache:
                        ts, cached_stats = self._stats_service._cache[opponent_team_id]
                        if time.time() - ts < 14400:
                            return self._compute_dvp_from_stats(cached_stats)
                except Exception:
                    pass

        # Fallback to ESPN cached stats
        stats = self._get_espn_team_stats_cache()
        opp = stats.get(opponent_team_id)
        if not opp:
            return {}  # No data = neutral matchup

        return self._compute_dvp_from_stats(opp)

    @staticmethod
    def _compute_dvp_from_stats(opp_stats: Dict) -> Dict[str, float]:
        """Compute DvP factors from a team stats dict.

        DvP factor = what opponent allows / league average.
        Clamped to [0.80, 1.20] to prevent extreme outliers.
        """
        factors: Dict[str, float] = {}
        for stat_name, league_avg in CBB_LEAGUE_AVG_STATS_PG.items():
            if league_avg <= 0:
                continue
            opp_allowed = opp_stats.get(f"opp_{stat_name}_pg", league_avg)
            ratio = opp_allowed / league_avg
            factors[stat_name] = round(
                max(0.80, min(1.20, ratio)), 3
            )
        return factors

    # ------------------------------------------------------------------
    # Team stats helpers
    # ------------------------------------------------------------------

    def _get_best_team_stats(self) -> Dict[int, Dict]:
        """Return the best available team stats.

        If real stats have been warmed via CBBStatsService, use those.
        Otherwise fall back to ESPN-sourced league averages.
        """
        if self._real_stats_cache:
            # Merge: real stats override ESPN defaults
            espn = self._get_espn_team_stats_cache()
            merged = dict(espn)  # Start with all ESPN teams
            merged.update(self._real_stats_cache)  # Override with real stats
            return merged

        return self._get_espn_team_stats_cache()

    def _get_espn_team_stats_cache(self) -> Dict[int, Dict]:
        """Return cached ESPN team stats (records only), refreshing if stale."""
        now = time.time()
        if (
            self._team_stats_cache
            and (now - self._team_stats_cache_ts) < self._team_stats_cache_ttl
        ):
            return self._team_stats_cache

        stats = self._fetch_espn_team_stats()
        self._team_stats_cache = stats
        self._team_stats_cache_ts = time.time()
        return stats

    @staticmethod
    def _fetch_espn_team_stats() -> Dict[int, Dict]:
        """Fetch team records from ESPN teams endpoint.

        Returns team win/loss records and league-average placeholders
        for pace/efficiency.  Real stats come from CBBStatsService
        via the warm-up path.
        """
        try:
            url = f"{_ESPN_BASE}/teams?limit=400"
            resp = resilient_get(url, group=APIGroup.ESPN_CBB)
            data = resp.json()

            team_stats: Dict[int, Dict] = {}
            for entry in (
                data.get("sports", [{}])[0]
                .get("leagues", [{}])[0]
                .get("teams", [])
            ):
                t = entry.get("team", {})
                team_id = int(t.get("id", 0))
                if not team_id:
                    continue

                # Parse record
                record = t.get("record", {})
                items = record.get("items", [{}])
                overall = items[0] if items else {}
                summary = overall.get("summary", "0-0")
                wins, losses = 0, 0
                try:
                    parts = summary.split("-")
                    wins = int(parts[0])
                    losses = int(parts[1])
                except (ValueError, IndexError):
                    pass

                # League-average defaults — replaced by CBBStatsService
                # when real stats are available
                team_stats[team_id] = {
                    "team_id": team_id,
                    "name": t.get("displayName", ""),
                    "abbreviation": t.get("abbreviation", ""),
                    "wins": wins,
                    "losses": losses,
                    "season_pace": CBB_LEAGUE_AVG_PACE,
                    "season_off_rating": CBB_LEAGUE_AVG_OFF_RATING,
                    "season_def_rating": CBB_LEAGUE_AVG_OFF_RATING,
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

            logger.info(f"Fetched CBB team stats for {len(team_stats)} teams")
            return team_stats

        except Exception as e:
            logger.error(f"Failed to fetch CBB team stats: {e}")
            return {}

    # ------------------------------------------------------------------
    # Scoreboard fetch
    # ------------------------------------------------------------------

    @staticmethod
    def _fetch_scoreboard(game_date: str) -> List[Dict]:
        """Fetch CBB scoreboard from ESPN for a date.

        Uses ``groups=50`` to request ALL Division 1 games, not just
        the handful of "featured" games ESPN returns by default.
        """
        date_param = game_date.replace("-", "")  # YYYYMMDD
        url = f"{_ESPN_BASE}/scoreboard?dates={date_param}&groups=50&limit=500"
        try:
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

                games.append({
                    "id": event.get("id", ""),
                    "date": event.get("date", ""),
                    "status": event.get("status", {}).get("type", {}).get(
                        "name", "STATUS_SCHEDULED"
                    ),
                    "home": {
                        "id": int(home["team"]["id"]),
                        "name": home["team"].get("displayName", ""),
                        "abbreviation": home["team"].get("abbreviation", ""),
                        "score": _safe_int(home.get("score", "0")),
                        "records": home.get("records", []),
                    },
                    "away": {
                        "id": int(away["team"]["id"]),
                        "name": away["team"].get("displayName", ""),
                        "abbreviation": away["team"].get("abbreviation", ""),
                        "score": _safe_int(away.get("score", "0")),
                        "records": away.get("records", []),
                    },
                })

            return games
        except Exception as e:
            logger.error(f"Failed to fetch CBB scoreboard for {game_date}: {e}")
            return []

    # ------------------------------------------------------------------
    # Game projection
    # ------------------------------------------------------------------

    def _project_game(
        self,
        raw_game: Dict,
        team_stats: Dict[int, Dict],
        vegas_odds: Optional[Dict[str, float]] = None,
    ) -> Optional[GameInfo]:
        """Project a single CBB game with optional Vegas blending."""
        home_id = raw_game["home"]["id"]
        away_id = raw_game["away"]["id"]

        home_stats = team_stats.get(home_id, {})
        away_stats = team_stats.get(away_id, {})

        # Build TeamGameStats
        home_tgs = self._build_team_game_stats(
            raw_game["home"], home_stats
        )
        away_tgs = self._build_team_game_stats(
            raw_game["away"], away_stats
        )

        # Project pace (average of two teams + regression to league mean)
        home_pace = home_stats.get("season_pace", CBB_LEAGUE_AVG_PACE)
        away_pace = away_stats.get("season_pace", CBB_LEAGUE_AVG_PACE)
        projected_pace = (home_pace + away_pace) / 2.0

        # Project scores
        home_off = home_stats.get("season_ppg", CBB_LEAGUE_AVG_PPG)
        away_off = away_stats.get("season_ppg", CBB_LEAGUE_AVG_PPG)
        home_def = home_stats.get("season_opp_ppg", CBB_LEAGUE_AVG_PPG)
        away_def = away_stats.get("season_opp_ppg", CBB_LEAGUE_AVG_PPG)

        # Simple projection: average of team offense vs opponent defense
        proj_home = (home_off + away_def) / 2.0 + 1.5  # Home court advantage
        proj_away = (away_off + home_def) / 2.0

        model_total = proj_home + proj_away
        model_spread = proj_away - proj_home  # Negative = home favored

        # ── Vegas blending ────────────────────────────────────────
        projected_total = model_total
        projected_spread = model_spread
        vegas_ou = None
        vegas_spread_val = None

        if vegas_odds:
            vegas_ou = vegas_odds.get("over_under")
            vegas_spread_val = vegas_odds.get("spread")

            if vegas_ou is not None:
                projected_total = (
                    CBB_VEGAS_TOTAL_BLEND_WEIGHT * vegas_ou
                    + (1.0 - CBB_VEGAS_TOTAL_BLEND_WEIGHT) * model_total
                )
                logger.debug(
                    f"Vegas blend: model_total={model_total:.1f}, "
                    f"vegas_ou={vegas_ou}, blended={projected_total:.1f}"
                )

            if vegas_spread_val is not None:
                projected_spread = (
                    CBB_VEGAS_SPREAD_BLEND_WEIGHT * vegas_spread_val
                    + (1.0 - CBB_VEGAS_SPREAD_BLEND_WEIGHT) * model_spread
                )

        # Recalculate projected scores from blended total/spread
        proj_home = (projected_total - projected_spread) / 2.0
        proj_away = (projected_total + projected_spread) / 2.0

        # Pace label
        if projected_pace >= CBB_LEAGUE_AVG_PACE + 3:
            pace_label = "Fast"
        elif projected_pace <= CBB_LEAGUE_AVG_PACE - 3:
            pace_label = "Slow"
        else:
            pace_label = "Average"

        # Status mapping
        status_map = {
            "STATUS_SCHEDULED": "Scheduled",
            "STATUS_IN_PROGRESS": "In Progress",
            "STATUS_HALFTIME": "In Progress",
            "STATUS_FINAL": "Final",
            "STATUS_POSTPONED": "Postponed",
        }
        game_status = status_map.get(
            raw_game.get("status", ""), "Scheduled"
        )

        # O/U edge = our blended total vs raw Vegas line
        ou_edge = None
        if vegas_ou is not None:
            ou_edge = round(projected_total - vegas_ou, 1)

        return GameInfo(
            game_id=str(raw_game["id"]),
            game_date=raw_game.get("date", "")[:10],
            game_time_et=raw_game.get("date", ""),
            game_sequence=0,
            game_status=game_status,
            home_team=home_tgs,
            away_team=away_tgs,
            projected_total=round(projected_total, 1),
            projected_home_score=round(proj_home, 1),
            projected_away_score=round(proj_away, 1),
            projected_spread=round(projected_spread, 1),
            projected_pace=round(projected_pace, 1),
            pace_label=pace_label,
            over_under=vegas_ou,
            over_under_edge=ou_edge,
            vegas_spread=vegas_spread_val,
        )

    @staticmethod
    def _build_team_game_stats(
        team_raw: Dict, stats: Dict
    ) -> TeamGameStats:
        """Build a TeamGameStats from team data (real or ESPN)."""
        wins = stats.get("wins", 0)
        losses = stats.get("losses", 0)

        # Try to extract from records if available
        for rec in team_raw.get("records", []):
            summary = rec.get("summary", "")
            try:
                parts = summary.split("-")
                if len(parts) == 2:
                    wins = int(parts[0])
                    losses = int(parts[1])
                    break
            except (ValueError, IndexError):
                pass

        return TeamGameStats(
            team_id=team_raw["id"],
            team_name=team_raw.get("name", ""),
            team_abbreviation=team_raw.get("abbreviation", ""),
            season_pace=stats.get("season_pace", CBB_LEAGUE_AVG_PACE),
            season_off_rating=stats.get(
                "season_off_rating", CBB_LEAGUE_AVG_OFF_RATING
            ),
            season_def_rating=stats.get(
                "season_def_rating", CBB_LEAGUE_AVG_OFF_RATING
            ),
            season_ppg=stats.get("season_ppg", CBB_LEAGUE_AVG_PPG),
            season_opp_ppg=stats.get("season_opp_ppg", CBB_LEAGUE_AVG_PPG),
            last_5_ppg=stats.get("season_ppg", CBB_LEAGUE_AVG_PPG),
            wins=wins,
            losses=losses,
            opp_pts_pg=stats.get("opp_pts_pg", CBB_LEAGUE_AVG_STATS_PG["pts"]),
            opp_reb_pg=stats.get("opp_reb_pg", CBB_LEAGUE_AVG_STATS_PG["reb"]),
            opp_ast_pg=stats.get("opp_ast_pg", CBB_LEAGUE_AVG_STATS_PG["ast"]),
            opp_stl_pg=stats.get("opp_stl_pg", CBB_LEAGUE_AVG_STATS_PG["stl"]),
            opp_blk_pg=stats.get("opp_blk_pg", CBB_LEAGUE_AVG_STATS_PG["blk"]),
            opp_tov_pg=stats.get("opp_tov_pg", CBB_LEAGUE_AVG_STATS_PG["tov"]),
            opp_fg3m_pg=stats.get("opp_fg3m_pg", CBB_LEAGUE_AVG_STATS_PG["fg3m"]),
        )


def _safe_int(val) -> int:
    """Safely convert to int."""
    try:
        return int(val)
    except (ValueError, TypeError):
        return 0
