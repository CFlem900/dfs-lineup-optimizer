"""Vegas odds integration via The Odds API.

Fetches live spreads and over/under totals for CBB and NBA games.
Gracefully degrades when the API key is missing or the API fails.

Pricing: The Odds API offers a free tier (500 requests/month) and
paid tiers.  Each call to ``get_odds()`` costs 1 request.

Usage:
    from app.services.odds_service import OddsService

    svc = OddsService()
    odds = svc.get_odds("cbb", "2026-02-15")
    # {'Duke vs UNC': {'over_under': 145.5, 'spread': -3.5}}
"""

import logging
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx

from app.config.constants import ODDS_API_CACHE_TTL

logger = logging.getLogger(__name__)

# The Odds API base URL
_ODDS_API_BASE = "https://api.the-odds-api.com/v4/sports"

# Sport keys for The Odds API
_SPORT_KEYS = {
    "cbb": "basketball_ncaab",
    "nba": "basketball_nba",
}


class OddsService:
    """Client for The Odds API — spreads and totals for CBB/NBA.

    If ``ODDS_API_KEY`` is not set, all methods return empty results
    (graceful degradation).  The optimizer and game projections work
    fine without odds — they just lose the Vegas anchoring benefit.
    """

    def __init__(self, api_key: str = ""):
        self._api_key = api_key
        self._cache: Dict[str, Tuple[float, Dict[str, Dict[str, float]]]] = {}

    @property
    def is_available(self) -> bool:
        """Return True if an API key is configured."""
        return bool(self._api_key)

    def get_odds(
        self,
        sport: str = "cbb",
        game_date: Optional[str] = None,
    ) -> Dict[str, Dict[str, float]]:
        """Fetch odds for a given sport.

        Parameters
        ----------
        sport : str
            ``"cbb"`` or ``"nba"``.
        game_date : str, optional
            Not directly used for filtering (The Odds API returns
            upcoming events), but used as a cache key.

        Returns
        -------
        dict
            Mapping of ``game_key`` → ``{"over_under": float, "spread": float}``.
            ``game_key`` is formatted as ``"home_team vs away_team"``
            (using The Odds API team names).
            The spread is **home-relative** (negative = home favored).
        """
        if not self._api_key:
            return {}

        cache_key = f"{sport}:{game_date or 'today'}"
        now = time.time()
        if cache_key in self._cache:
            cached_at, cached_odds = self._cache[cache_key]
            if now - cached_at < ODDS_API_CACHE_TTL:
                return cached_odds

        try:
            odds = self._fetch_odds(sport)
            self._cache[cache_key] = (time.time(), odds)
            return odds
        except Exception as e:
            logger.warning(f"Odds API fetch failed for {sport}: {e}")
            return {}

    def get_game_odds(
        self,
        sport: str,
        home_team: str,
        away_team: str,
        game_date: Optional[str] = None,
    ) -> Optional[Dict[str, float]]:
        """Get odds for a specific game by team names.

        Uses fuzzy matching to handle name differences between
        ESPN and The Odds API.

        Returns
        -------
        dict or None
            ``{"over_under": float, "spread": float}`` or None if not found.
        """
        all_odds = self.get_odds(sport, game_date)
        if not all_odds:
            return None

        # Try exact match first
        home_lower = home_team.lower()
        away_lower = away_team.lower()

        for key, odds in all_odds.items():
            key_lower = key.lower()
            if home_lower in key_lower and away_lower in key_lower:
                return odds

        # Try partial word matching
        home_words = {w for w in home_lower.split() if len(w) > 3}
        away_words = {w for w in away_lower.split() if len(w) > 3}

        for key, odds in all_odds.items():
            key_lower = key.lower()
            home_match = any(w in key_lower for w in home_words) if home_words else False
            away_match = any(w in key_lower for w in away_words) if away_words else False
            if home_match and away_match:
                return odds

        return None

    def clear_cache(self):
        """Clear the odds cache."""
        self._cache.clear()

    # ------------------------------------------------------------------
    # Line movement: opening vs current odds tracking
    # ------------------------------------------------------------------

    def capture_snapshot(
        self,
        sport: str = "nba",
        game_date: Optional[str] = None,
    ) -> int:
        """Capture current odds as a snapshot for line movement tracking.

        Stores in an in-memory history keyed by (game_key, game_date).
        The first snapshot for a game becomes the "opening" line.

        Returns the number of games captured.
        """
        odds = self.get_odds(sport, game_date)
        if not odds:
            return 0

        if not hasattr(self, "_snapshots"):
            self._snapshots: Dict[str, List[Dict]] = {}

        count = 0
        for game_key, values in odds.items():
            snap_key = f"{game_key}:{game_date or 'today'}"
            if snap_key not in self._snapshots:
                self._snapshots[snap_key] = []
            self._snapshots[snap_key].append({
                "total": values.get("over_under"),
                "spread": values.get("spread"),
                "captured_at": time.time(),
            })
            count += 1

        return count

    def get_line_movements(
        self,
        sport: str = "nba",
        game_date: Optional[str] = None,
    ) -> List[Dict]:
        """Compute line movement deltas for all tracked games.

        Returns a list of dicts with opening vs current values,
        suitable for passing to LineMovementAgent.
        """
        if not hasattr(self, "_snapshots"):
            return []

        # Get current odds
        current_odds = self.get_odds(sport, game_date)
        movements = []

        for game_key, values in current_odds.items():
            snap_key = f"{game_key}:{game_date or 'today'}"
            history = self._snapshots.get(snap_key, [])

            if not history:
                continue

            # Opening = first snapshot, current = latest fetch
            opening = history[0]
            teams = game_key.split(" vs ", 1)
            home = teams[0] if len(teams) == 2 else game_key
            away = teams[1] if len(teams) == 2 else ""

            movements.append({
                "home_team": home,
                "away_team": away,
                "opening_total": opening.get("total"),
                "current_total": values.get("over_under"),
                "opening_spread": opening.get("spread"),
                "current_spread": values.get("spread"),
            })

        return movements

    # ------------------------------------------------------------------
    # Private: API interaction
    # ------------------------------------------------------------------

    def _fetch_odds(self, sport: str) -> Dict[str, Dict[str, float]]:
        """Fetch odds from The Odds API.

        API endpoint:
            GET /v4/sports/{sport_key}/odds/
            ?apiKey=KEY&regions=us&markets=spreads,totals
        """
        sport_key = _SPORT_KEYS.get(sport, _SPORT_KEYS["cbb"])
        url = f"{_ODDS_API_BASE}/{sport_key}/odds/"
        params = {
            "apiKey": self._api_key,
            "regions": "us",
            "markets": "spreads,totals",
            "oddsFormat": "american",
        }

        resp = httpx.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        # Log remaining API requests from headers
        remaining = resp.headers.get("x-requests-remaining", "?")
        used = resp.headers.get("x-requests-used", "?")
        logger.info(
            f"Odds API: fetched {len(data)} events for {sport} "
            f"(used={used}, remaining={remaining})"
        )

        return self._parse_odds_response(data)

    @staticmethod
    def _parse_odds_response(
        events: List[Dict],
    ) -> Dict[str, Dict[str, float]]:
        """Parse The Odds API response into a game → odds mapping.

        The API returns events with multiple bookmakers.  We take the
        first bookmaker's lines (typically DraftKings or FanDuel) as
        these are the most relevant for DFS players.

        Returns dict of ``"home_team vs away_team"`` → ``{over_under, spread}``.
        """
        odds_map: Dict[str, Dict[str, float]] = {}

        for event in events:
            home_team = event.get("home_team", "")
            away_team = event.get("away_team", "")
            if not home_team or not away_team:
                continue

            game_key = f"{home_team} vs {away_team}"
            bookmakers = event.get("bookmakers", [])
            if not bookmakers:
                continue

            # Prefer DraftKings, then FanDuel, then first available
            bm = _find_preferred_bookmaker(bookmakers)
            if not bm:
                continue

            game_odds: Dict[str, float] = {}

            for market in bm.get("markets", []):
                market_key = market.get("key", "")

                if market_key == "totals" and "over_under" not in game_odds:
                    # Take the "Over" outcome point value
                    for outcome in market.get("outcomes", []):
                        if outcome.get("name", "").lower() == "over":
                            try:
                                game_odds["over_under"] = float(
                                    outcome.get("point", 0)
                                )
                            except (ValueError, TypeError):
                                pass
                            break

                elif market_key == "spreads" and "spread" not in game_odds:
                    # Take the home team's spread
                    for outcome in market.get("outcomes", []):
                        if outcome.get("name", "") == home_team:
                            try:
                                game_odds["spread"] = float(
                                    outcome.get("point", 0)
                                )
                            except (ValueError, TypeError):
                                pass
                            break

            if game_odds:
                odds_map[game_key] = game_odds

        logger.info(
            f"Parsed odds for {len(odds_map)} games "
            f"({sum(1 for g in odds_map.values() if 'over_under' in g)} O/U, "
            f"{sum(1 for g in odds_map.values() if 'spread' in g)} spreads)"
        )
        return odds_map


def _find_preferred_bookmaker(bookmakers: List[Dict]) -> Optional[Dict]:
    """Find the preferred bookmaker from the list.

    Priority: DraftKings → FanDuel → first available.
    """
    preferred = {"draftkings", "fanduel"}
    by_key = {bm.get("key", ""): bm for bm in bookmakers}

    for pref in ["draftkings", "fanduel"]:
        if pref in by_key:
            return by_key[pref]

    return bookmakers[0] if bookmakers else None
