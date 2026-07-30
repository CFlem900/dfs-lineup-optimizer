"""Unified Vegas odds fetcher with guaranteed mathematical fallback.

Provides a single entry point — ``get_game_odds()`` — that resolves
over/under and spread for every NBA game on a given date.  Results are
keyed by team abbreviation and include derived **implied team totals**.

Resolution order:
    1. **The Odds API** (free tier, 500 req/month) — gold standard
    2. **BallDontLie V2 Odds** (premium BDL tier) — secondary
    3. **Pace / Efficiency heuristic** — always available, zero API cost

The heuristic produces surprisingly reasonable estimates by combining
each team's season pace, offensive rating, and defensive rating into a
game-level total and spread, then deriving implied team totals.

Usage::

    fetcher = OddsFetcherService(
        odds_api_key="...",          # from .env (empty = skip)
        bdl_service=bdl_instance,    # or None
    )
    odds = fetcher.get_game_odds("2026-03-10", team_stats, game_headers)
    atl = odds["ATL"]
    # {
    #     "over_under": 231.5,
    #     "spread": -6.5,
    #     "implied_home_total": 119.0,
    #     "implied_away_total": 112.5,
    #     "source": "the_odds_api",  # or "balldontlie" or "heuristic"
    # }
"""

import logging
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx
from nba_api.stats.static import teams as nba_teams

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────
_ODDS_API_BASE = "https://api.the-odds-api.com/v4/sports"
_NBA_SPORT_KEY = "basketball_nba"
_CACHE_TTL_S = 600  # 10 minutes

# League-average fallbacks for the heuristic (2025-26 season)
_LG_AVG_PACE = 101.9
_LG_AVG_ORTG = 114.3
_LG_AVG_DRTG = 114.3

# Home-court advantage (points added to home score estimate)
_HCA_POINTS = 2.5

# ── Team name ↔ abbreviation mapping (built once) ────────────────────
_NAME_TO_ABBR: Dict[str, str] = {}


def _build_name_map() -> Dict[str, str]:
    """Build a lowercase full-name / city / nickname → abbreviation map."""
    if _NAME_TO_ABBR:
        return _NAME_TO_ABBR
    for t in nba_teams.get_teams():
        abbr = t["abbreviation"]
        _NAME_TO_ABBR[t["full_name"].lower()] = abbr
        city = (t.get("city") or "").lower()
        nick = (t.get("nickname") or "").lower()
        if city:
            _NAME_TO_ABBR[city] = abbr
        if nick:
            _NAME_TO_ABBR[nick] = abbr
        # Individual words > 4 chars (handles "Timberwolves", "Celtics" etc.)
        for word in t["full_name"].lower().split():
            if len(word) > 4:
                _NAME_TO_ABBR[word] = abbr
    return _NAME_TO_ABBR


# ======================================================================
# Service
# ======================================================================


class OddsFetcherService:
    """Resilient odds fetcher with API + mathematical-heuristic fallback.

    Parameters
    ----------
    odds_api_key : str
        The Odds API key (empty string disables this source).
    bdl_service : object, optional
        An initialised ``BallDontLieService`` instance.  Its
        ``get_games()`` and ``get_odds_by_game()`` methods are called
        when the BDL path is attempted.
    """

    def __init__(
        self,
        odds_api_key: str = "",
        bdl_service: Any = None,
    ):
        self._api_key = odds_api_key
        self._bdl = bdl_service

        # In-memory cache: (timestamp, date_str, odds_dict)
        self._cache: Optional[Tuple[float, str, Dict[str, Dict]]] = None

    # ── Public API ────────────────────────────────────────────────────

    def get_game_odds(
        self,
        target_date: str,
        team_stats: Dict[int, Dict],
        game_headers: List[Dict],
    ) -> Dict[str, Dict[str, float]]:
        """Return odds for every game on *target_date*, keyed by team abbreviation.

        Always returns a non-empty dict (the heuristic guarantees a
        result for every game in *game_headers*).

        Each value dict contains::

            over_under      – game total
            spread          – home-relative (neg = home favored)
            implied_home    – (over_under − spread) / 2
            implied_away    – (over_under + spread) / 2
            source          – "the_odds_api" | "balldontlie" | "heuristic"

        Parameters
        ----------
        target_date : str
            ``YYYY-MM-DD`` date string.
        team_stats : dict
            Mapping ``team_id`` → season stats dict (pace, off_rating,
            def_rating).  Used exclusively by the heuristic fallback.
        game_headers : list[dict]
            Scoreboard headers with ``HOME_TEAM_ID`` and
            ``VISITOR_TEAM_ID`` — used to enumerate the games and
            resolve team abbreviations for the heuristic.
        """
        # Check cache
        now = time.time()
        if self._cache:
            ts, cached_date, cached_data = self._cache
            if cached_date == target_date and (now - ts) < _CACHE_TTL_S:
                return cached_data

        result: Dict[str, Dict[str, float]] = {}

        # ── Source 1: The Odds API ────────────────────────────────────
        if self._api_key:
            api_odds = self._fetch_the_odds_api(target_date)
            if api_odds:
                result = api_odds
                logger.info(
                    "[OddsFetcher] Source 1 (The Odds API): "
                    "%d teams with lines",
                    len(result),
                )

        # ── Source 2: BallDontLie V2 ──────────────────────────────────
        if not result:
            bdl_odds = self._fetch_bdl(target_date)
            if bdl_odds:
                result = bdl_odds
                logger.info(
                    "[OddsFetcher] Source 2 (BDL): "
                    "%d teams with lines",
                    len(result),
                )

        # ── Source 3: Pace / Efficiency heuristic ─────────────────────
        # Fill any games NOT covered by API sources (could be all of
        # them if both APIs are unavailable, or just the few the API
        # missed for whatever reason).
        heuristic = self._estimate_all_games(
            team_stats, game_headers
        )
        filled = 0
        for abbr, entry in heuristic.items():
            if abbr not in result:
                result[abbr] = entry
                filled += 1
        if filled:
            logger.info(
                "[OddsFetcher] Source 3 (heuristic): "
                "filled %d team entries (%d games)",
                filled,
                filled // 2,
            )

        # Cache and return
        self._cache = (now, target_date, result)
        return result

    @property
    def is_available(self) -> bool:
        """True if at least one live API source might return data."""
        return bool(self._api_key) or (
            self._bdl is not None
            and getattr(self._bdl, "is_available", False)
        )

    # ==================================================================
    # Source 1: The Odds API
    # ==================================================================

    def _fetch_the_odds_api(
        self, target_date: str
    ) -> Dict[str, Dict[str, float]]:
        """Call The Odds API free tier and return team-abbreviation-keyed dict."""
        try:
            url = f"{_ODDS_API_BASE}/{_NBA_SPORT_KEY}/odds/"
            params = {
                "apiKey": self._api_key,
                "regions": "us",
                "markets": "spreads,totals",
                "oddsFormat": "american",
            }
            resp = httpx.get(url, params=params, timeout=15)

            # Surface rate-limit info
            remaining = resp.headers.get("x-requests-remaining", "?")
            used = resp.headers.get("x-requests-used", "?")

            # Catch tier-related failures gracefully
            if resp.status_code in (401, 403):
                logger.warning(
                    "[OddsFetcher] The Odds API auth failed (%d) "
                    "— falling through to next source "
                    "(used=%s, remaining=%s)",
                    resp.status_code,
                    used,
                    remaining,
                )
                return {}
            resp.raise_for_status()

            events = resp.json()
            logger.info(
                "[OddsFetcher] The Odds API: %d events "
                "(used=%s, remaining=%s)",
                len(events),
                used,
                remaining,
            )
            return self._parse_api_events(events)

        except httpx.HTTPStatusError as e:
            logger.warning("[OddsFetcher] The Odds API HTTP %s", e)
            return {}
        except Exception as e:
            logger.warning("[OddsFetcher] The Odds API error: %s", e)
            return {}

    @staticmethod
    def _parse_api_events(
        events: List[Dict],
    ) -> Dict[str, Dict[str, float]]:
        """Parse The Odds API response into team-abbreviation-keyed dict."""
        name_map = _build_name_map()
        result: Dict[str, Dict[str, float]] = {}

        for event in events:
            home_name = event.get("home_team", "")
            away_name = event.get("away_team", "")
            if not home_name or not away_name:
                continue

            home_abbr = name_map.get(home_name.lower(), "")
            away_abbr = name_map.get(away_name.lower(), "")
            if not home_abbr or not away_abbr:
                # Fallback: try matching individual words
                for word in home_name.lower().split():
                    if word in name_map:
                        home_abbr = name_map[word]
                        break
                for word in away_name.lower().split():
                    if word in name_map:
                        away_abbr = name_map[word]
                        break
            if not home_abbr or not away_abbr:
                continue

            bookmakers = event.get("bookmakers", [])
            bm = _pick_bookmaker(bookmakers)
            if not bm:
                continue

            ou: Optional[float] = None
            spread: Optional[float] = None

            for market in bm.get("markets", []):
                key = market.get("key", "")
                if key == "totals" and ou is None:
                    for oc in market.get("outcomes", []):
                        if oc.get("name", "").lower() == "over":
                            try:
                                ou = float(oc["point"])
                            except (KeyError, TypeError, ValueError):
                                pass
                            break
                elif key == "spreads" and spread is None:
                    for oc in market.get("outcomes", []):
                        if oc.get("name", "") == home_name:
                            try:
                                spread = float(oc["point"])
                            except (KeyError, TypeError, ValueError):
                                pass
                            break

            if ou is None and spread is None:
                continue

            _attach(result, home_abbr, away_abbr, ou, spread, "the_odds_api")

        return result

    # ==================================================================
    # Source 2: BallDontLie V2 Odds
    # ==================================================================

    def _fetch_bdl(
        self, target_date: str
    ) -> Dict[str, Dict[str, float]]:
        """Try BDL V2 odds endpoint (premium tier)."""
        bdl = self._bdl
        if not bdl or not getattr(bdl, "is_available", False):
            return {}

        try:
            games = bdl.get_games(target_date)
            odds_by_game = bdl.get_odds_by_game(target_date)
            if not odds_by_game:
                return {}

            # game_id → (home_abbr, away_abbr)
            game_teams: Dict[int, Tuple[str, str]] = {}
            for g in games:
                gid = g.get("id")
                home = g.get("home_team") or {}
                vis = g.get("visitor_team") or {}
                if gid:
                    game_teams[gid] = (
                        (home.get("abbreviation") or "").upper(),
                        (vis.get("abbreviation") or "").upper(),
                    )

            result: Dict[str, Dict[str, float]] = {}
            for gid, odds in odds_by_game.items():
                teams = game_teams.get(gid)
                if not teams:
                    continue
                home_abbr, away_abbr = teams
                total = odds.get("total")
                spread_home = odds.get("spread_home")

                ou = float(total) if total is not None else None
                sp = float(spread_home) if spread_home is not None else None

                _attach(result, home_abbr, away_abbr, ou, sp, "balldontlie")

            return result

        except Exception as e:
            logger.debug("[OddsFetcher] BDL odds failed: %s", e)
            return {}

    # ==================================================================
    # Source 3: Pace / Efficiency Heuristic (guaranteed)
    # ==================================================================

    def _estimate_all_games(
        self,
        team_stats: Dict[int, Dict],
        game_headers: List[Dict],
    ) -> Dict[str, Dict[str, float]]:
        """Estimate O/U and spread for every game using team season stats.

        This is the last-resort fallback.  It uses real team pace and
        efficiency data (populated from the DB cache via the 4 AM
        refresh) so it still differentiates games meaningfully.
        """
        # Pre-build team_id → abbreviation lookup
        _id_to_abbr: Dict[int, str] = {
            t["id"]: t["abbreviation"] for t in nba_teams.get_teams()
        }

        result: Dict[str, Dict[str, float]] = {}

        for h in game_headers:
            home_id = h.get("HOME_TEAM_ID", 0)
            away_id = h.get("VISITOR_TEAM_ID", 0)
            if not home_id or not away_id:
                continue

            home_abbr = _id_to_abbr.get(home_id, "")
            away_abbr = _id_to_abbr.get(away_id, "")
            if not home_abbr or not away_abbr:
                continue

            home_row = team_stats.get(home_id, {})
            away_row = team_stats.get(away_id, {})

            ou, spread = self._estimate_vegas_lines(home_row, away_row)
            _attach(result, home_abbr, away_abbr, ou, spread, "heuristic")

        return result

    @staticmethod
    def _estimate_vegas_lines(
        home: Dict[str, Any],
        away: Dict[str, Any],
    ) -> Tuple[float, float]:
        """Estimate Over/Under and Spread from season pace + efficiency.

        The model mirrors the structure of a Vegas total:

        1. **Game Pace** = average of both teams' season pace.
        2. **Adjusted Offensive Rating** for each team accounts for the
           opponent's defensive rating relative to the league average::

               adj_ortg = team_ortg + (LG_AVG_DRTG − opp_drtg)

           A team facing a below-average defense gets a boost.
        3. **Projected Score** per team = ``adj_ortg × game_pace / 100``
        4. **Home-Court Advantage** = +2.5 points (split 65/35 home/away,
           consistent with ``GameService._project_game``).
        5. **Over/Under** = home_score + away_score.
        6. **Spread** = away_score − home_score (negative = home favored).

        Returns
        -------
        (over_under, spread)
            Both as floats rounded to 1 decimal.
        """
        # Extract with sane league-average defaults
        h_pace = home.get("pace", _LG_AVG_PACE)
        a_pace = away.get("pace", _LG_AVG_PACE)
        h_ortg = home.get("off_rating", _LG_AVG_ORTG)
        a_ortg = away.get("off_rating", _LG_AVG_ORTG)
        h_drtg = home.get("def_rating", _LG_AVG_DRTG)
        a_drtg = away.get("def_rating", _LG_AVG_DRTG)

        # Step 1: game pace
        game_pace = (h_pace + a_pace) / 2.0

        # Step 2: matchup-adjusted ratings
        # Home offense vs Away defense
        h_adj_ortg = h_ortg + (_LG_AVG_DRTG - a_drtg)
        # Away offense vs Home defense
        a_adj_ortg = a_ortg + (_LG_AVG_DRTG - h_drtg)

        # Step 3: raw projected scores
        h_score = h_adj_ortg * game_pace / 100.0
        a_score = a_adj_ortg * game_pace / 100.0

        # Step 4: home-court advantage
        h_score += _HCA_POINTS * 0.65
        a_score -= _HCA_POINTS * 0.35

        # Step 5 & 6
        over_under = round(h_score + a_score, 1)
        spread = round(a_score - h_score, 1)  # negative = home favored

        return over_under, spread

    # ==================================================================
    # Convenience: extract lines for one game from the full result
    # ==================================================================

    @staticmethod
    def implied_team_total(
        over_under: float,
        spread: float,
        is_home: bool = True,
    ) -> float:
        """Derive a team's implied total from the game total + spread.

        For the **home** team::

            implied = (over_under − spread) / 2

        For the **away** team::

            implied = (over_under + spread) / 2

        (Spread convention: negative = home favored.)
        """
        if is_home:
            return round((over_under - spread) / 2.0, 1)
        return round((over_under + spread) / 2.0, 1)


# ── Helpers ───────────────────────────────────────────────────────────


def _pick_bookmaker(bookmakers: List[Dict]) -> Optional[Dict]:
    """Select preferred bookmaker: DraftKings → FanDuel → first."""
    by_key = {bm.get("key", ""): bm for bm in bookmakers}
    for pref in ("draftkings", "fanduel"):
        if pref in by_key:
            return by_key[pref]
    return bookmakers[0] if bookmakers else None


def _attach(
    result: Dict[str, Dict[str, float]],
    home_abbr: str,
    away_abbr: str,
    over_under: Optional[float],
    spread: Optional[float],
    source: str,
) -> None:
    """Insert home + away entries into *result*, computing implied totals."""
    if over_under is None and spread is None:
        return

    # Fill in missing half from the other when possible
    if over_under is not None and spread is None:
        spread = 0.0  # assume pick'em
    if spread is not None and over_under is None:
        over_under = 2 * _LG_AVG_ORTG * _LG_AVG_PACE / 100.0  # ~232

    implied_home = round((over_under - spread) / 2.0, 1)
    implied_away = round((over_under + spread) / 2.0, 1)

    if home_abbr:
        result[home_abbr] = {
            "over_under": over_under,
            "spread": spread,
            "implied_home": implied_home,
            "implied_away": implied_away,
            "source": source,
        }
    if away_abbr:
        result[away_abbr] = {
            "over_under": over_under,
            "spread": -spread,  # flip for away perspective
            "implied_home": implied_home,
            "implied_away": implied_away,
            "source": source,
        }
