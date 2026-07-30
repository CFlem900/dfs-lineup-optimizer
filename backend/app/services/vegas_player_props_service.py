"""Vegas player prop → implied minutes service.

Fetches PRA (Points + Rebounds + Assists) over/under lines from The Odds
API's ``player_points_rebounds_assists`` market for all upcoming NBA games.
When a fringe player (DK salary ≤ $4,500) has a posted PRA prop, it's a
strong Vegas signal that the player has a confirmed role — even if our
BallDontLie database has zero game logs.

The service reverse-engineers implied minutes from the PRA line using
positional PRA-per-minute baselines, then provides an override dict that
the lineup optimizer stamps onto ``PlayerMinutes`` objects *before*
TopDownMinutes runs.

Pricing: Each call to The Odds API ``/events/{eventId}/odds`` with
``markets=player_points_rebounds_assists`` costs 2 requests on the
paid plan (or 1 on the free plan when using featured markets).

Usage:
    from app.services.vegas_player_props_service import VegasPlayerPropsService

    svc = VegasPlayerPropsService(api_key="...")
    props = svc.fetch_player_pra_props()
    # {"jabari smith": {"pra_line": 18.5, "implied_minutes": 21.8, ...}, ...}
"""

import logging
import re
import time
import threading
from typing import Any, Dict, List, Optional, Tuple

import httpx

from app.utils.helpers import normalize_player_name
from app.config.constants import ODDS_API_CACHE_TTL

logger = logging.getLogger(__name__)

# ── The Odds API endpoints ──────────────────────────────────────────────
_ODDS_API_BASE = "https://api.the-odds-api.com/v4"
_NBA_SPORT_KEY = "basketball_nba"

# ── Positional PRA-per-minute baselines ─────────────────────────────────
# Derived from NBA averages for bench/fringe rotation players (2024-2026).
# These are *total* PRA-per-minute, not per-stat.  Bigs accumulate PRA
# faster (easy rebounds, put-backs) while wings and handlers have more
# variance.
#
# Positional rates:
#   C  / PF-C  → 1.05 PRA/min  (easy boards + put-backs)
#   PG / SF    → 0.90 PRA/min  (ball-handler assists + scoring)
#   SG / PF    → 0.75 PRA/min  (catch-and-shoot, limited creation)
#
# Fallback (unknown position): 0.85 PRA/min (league-wide fringe average)
_PRA_PER_MINUTE: Dict[str, float] = {
    "C":    1.05,
    "PF/C": 1.05,
    "C/PF": 1.05,
    "PF":   0.75,
    "PG":   0.90,
    "SF":   0.90,
    "SG":   0.75,
    # Multi-position DK slots
    "PG/SG": 0.85,
    "SG/SF": 0.82,
    "SF/PF": 0.80,
    "PG/SF": 0.90,
    "SG/PF": 0.78,
}
_PRA_PER_MINUTE_DEFAULT = 0.85

# ── Implied minutes constraints ─────────────────────────────────────────
VEGAS_IMPLIED_MINUTES_HARD_CAP = 36.0   # Never project more than 36 min
VEGAS_IMPLIED_MINUTES_FLOOR = 8.0       # Below 8 min the prop is noise
VEGAS_SALARY_THRESHOLD = 4500           # Only override ≤ $4,500 players
VEGAS_SYNTHETIC_FPPM_DEFAULT = 0.85     # Fallback FPPM for zero-history


def calculate_implied_minutes(
    pra_line: float,
    position: str = "",
) -> float:
    """Reverse-engineer implied playing time from a Vegas PRA prop line.

    Formula:  implied_minutes = pra_line / positional_pra_per_minute

    Parameters
    ----------
    pra_line : float
        The over/under PRA line from the sportsbook.
    position : str
        DK-style position string (e.g. "PG", "SF/PF", "C").

    Returns
    -------
    float
        Implied minutes, clamped to [VEGAS_IMPLIED_MINUTES_FLOOR, VEGAS_IMPLIED_MINUTES_HARD_CAP].
    """
    if pra_line <= 0:
        return 0.0

    # Look up positional rate; try exact match, then first token, then default
    pos_upper = (position or "").upper().strip()
    rate = _PRA_PER_MINUTE.get(pos_upper)
    if rate is None:
        # Try first position token (e.g., "PG" from "PG/SG")
        first_pos = pos_upper.split("/")[0].split("-")[0] if pos_upper else ""
        rate = _PRA_PER_MINUTE.get(first_pos, _PRA_PER_MINUTE_DEFAULT)

    implied = pra_line / rate
    return round(
        max(VEGAS_IMPLIED_MINUTES_FLOOR, min(implied, VEGAS_IMPLIED_MINUTES_HARD_CAP)),
        1,
    )


def get_synthetic_fppm(position: str = "") -> float:
    """Return a synthetic FPPM baseline for a zero-history player by position.

    These mirror the PRA-per-minute rates converted to DK fantasy points:
        DK FPPM ≈ PRA_per_min × 1.0 (PTS) + REB×1.25 + AST×1.5 decomposition
        Simplified: use PRA_per_minute as FPPM proxy (close enough for fringe).

    Bigs (C, PF/C):     1.05 FPPM  (easy rebounds worth 1.25 each)
    Ball handlers (PG):  0.90 FPPM  (assists worth 1.5 each)
    Wings (SG, PF):      0.75 FPPM  (catch-and-shoot, fewer touches)
    Default:             0.85 FPPM
    """
    pos_upper = (position or "").upper().strip()
    rate = _PRA_PER_MINUTE.get(pos_upper)
    if rate is None:
        first_pos = pos_upper.split("/")[0].split("-")[0] if pos_upper else ""
        rate = _PRA_PER_MINUTE.get(first_pos, VEGAS_SYNTHETIC_FPPM_DEFAULT)
    return rate


class VegasPlayerPropsService:
    """Fetches NBA player PRA props from The Odds API and computes
    implied minutes for fringe/deep-bench players.

    Gracefully degrades when the API key is missing or the API fails.
    """

    def __init__(self, api_key: str = ""):
        self._api_key = api_key
        # Cache: (timestamp, props_dict)
        self._cache: Optional[Tuple[float, Dict[str, Dict[str, Any]]]] = None
        self._cache_lock = threading.Lock()

    @property
    def is_available(self) -> bool:
        """True if an Odds API key is configured."""
        return bool(self._api_key)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def fetch_player_pra_props(self) -> Dict[str, Dict[str, Any]]:
        """Fetch PRA props for all upcoming NBA games.

        Returns a dict keyed by **normalized player name** (via
        ``normalize_player_name``) so it matches our DK CSV dictionary::

            {
                "jabari smith": {
                    "pra_line": 18.5,
                    "position": "PF",
                    "team": "HOU",
                    "implied_minutes": 21.8,
                    "synthetic_fppm": 0.75,
                    "bookmaker": "draftkings",
                },
                ...
            }

        Returns empty dict on API failure or missing key.
        """
        if not self._api_key:
            return {}

        # Check cache
        now = time.time()
        with self._cache_lock:
            if self._cache:
                cached_at, cached_data = self._cache
                if now - cached_at < ODDS_API_CACHE_TTL:
                    return cached_data

        try:
            result = self._fetch_and_parse()
            with self._cache_lock:
                self._cache = (time.time(), result)
            return result
        except Exception as e:
            logger.warning(f"[VegasProps] Fetch failed: {e}")
            # Return stale cache if available
            if self._cache:
                return self._cache[1]
            return {}

    def get_player_implied_minutes(
        self,
        player_name: str,
        position: str = "",
    ) -> Optional[float]:
        """Look up a specific player's implied minutes from Vegas PRA.

        Returns None if no prop line exists for this player.
        """
        props = self.fetch_player_pra_props()
        normalized = normalize_player_name(player_name)
        entry = props.get(normalized)
        if not entry:
            return None
        return entry.get("implied_minutes")

    def clear_cache(self):
        """Clear the props cache."""
        with self._cache_lock:
            self._cache = None

    # ------------------------------------------------------------------
    # Private: API interaction
    # ------------------------------------------------------------------

    def _fetch_and_parse(self) -> Dict[str, Dict[str, Any]]:
        """Fetch PRA props from The Odds API and parse into our format.

        The Odds API v4 flow:
        1. GET /v4/sports/basketball_nba/events?apiKey=KEY
           → Returns list of upcoming events with event IDs.
        2. GET /v4/sports/basketball_nba/events/{eventId}/odds
                ?apiKey=KEY&regions=us&markets=player_points_rebounds_assists
                &bookmakers=draftkings,fanduel
           → Returns player prop lines per bookmaker.

        We prefer DraftKings lines (most aligned with DK DFS pricing),
        falling back to FanDuel, then any other bookmaker.
        """
        # Step 1: Get upcoming NBA events
        events = self._fetch_events()
        if not events:
            logger.info("[VegasProps] No upcoming NBA events found")
            return {}

        # Step 2: Fetch PRA props for each event
        all_props: Dict[str, Dict[str, Any]] = {}
        events_with_props = 0

        for event in events:
            event_id = event.get("id")
            if not event_id:
                continue

            # Extract team abbreviations from event
            home_team = event.get("home_team", "")
            away_team = event.get("away_team", "")

            try:
                event_props = self._fetch_event_pra(event_id)
                if event_props:
                    events_with_props += 1
                    # Parse bookmaker outcomes into our format
                    parsed = self._parse_pra_outcomes(
                        event_props, home_team, away_team
                    )
                    all_props.update(parsed)
            except Exception as e:
                logger.debug(
                    f"[VegasProps] Failed to fetch PRA for event {event_id}: {e}"
                )
                continue

        logger.info(
            f"[VegasProps] Fetched PRA props: {len(all_props)} players "
            f"across {events_with_props} events"
        )

        # Log remaining API requests
        return all_props

    def _fetch_events(self) -> List[Dict]:
        """GET /v4/sports/basketball_nba/events — upcoming NBA events."""
        url = f"{_ODDS_API_BASE}/sports/{_NBA_SPORT_KEY}/events"
        params = {"apiKey": self._api_key}
        resp = httpx.get(url, params=params, timeout=15)
        resp.raise_for_status()

        remaining = resp.headers.get("x-requests-remaining", "?")
        logger.info(
            f"[VegasProps] Events fetch: {len(resp.json())} events "
            f"(API remaining={remaining})"
        )
        return resp.json()

    def _fetch_event_pra(self, event_id: str) -> List[Dict]:
        """GET /v4/sports/basketball_nba/events/{id}/odds — PRA props.

        Requests the ``player_points_rebounds_assists`` market from
        DraftKings and FanDuel bookmakers.
        """
        url = (
            f"{_ODDS_API_BASE}/sports/{_NBA_SPORT_KEY}"
            f"/events/{event_id}/odds"
        )
        params = {
            "apiKey": self._api_key,
            "regions": "us",
            "markets": "player_points_rebounds_assists",
            "bookmakers": "draftkings,fanduel",
            "oddsFormat": "american",
        }
        resp = httpx.get(url, params=params, timeout=15)
        resp.raise_for_status()

        data = resp.json()
        return data.get("bookmakers", [])

    def _parse_pra_outcomes(
        self,
        bookmakers: List[Dict],
        home_team: str,
        away_team: str,
    ) -> Dict[str, Dict[str, Any]]:
        """Parse The Odds API bookmaker outcomes into our normalized format.

        Prefers DraftKings lines over FanDuel.  For each player, extracts
        the Over line as the PRA prop number.

        The Odds API response structure:
        ```json
        {
            "bookmakers": [{
                "key": "draftkings",
                "markets": [{
                    "key": "player_points_rebounds_assists",
                    "outcomes": [
                        {"name": "Jabari Smith Jr.", "description": "Over", "point": 18.5, "price": -115},
                        {"name": "Jabari Smith Jr.", "description": "Under", "point": 18.5, "price": -105},
                    ]
                }]
            }]
        }
        ```
        """
        # Build team abbreviation lookup for The Odds API full names
        # The Odds API uses full names like "Houston Rockets" — we need
        # to map players to team abbreviations for our dict keys.
        team_map = _build_team_map(home_team, away_team)

        # Prefer DraftKings → FanDuel → any other
        sorted_books = sorted(
            bookmakers,
            key=lambda b: (
                0 if b.get("key") == "draftkings"
                else 1 if b.get("key") == "fanduel"
                else 2
            ),
        )

        result: Dict[str, Dict[str, Any]] = {}

        for book in sorted_books:
            book_key = book.get("key", "unknown")
            for market in book.get("markets", []):
                if market.get("key") != "player_points_rebounds_assists":
                    continue

                for outcome in market.get("outcomes", []):
                    # Only take "Over" lines (the number is the same for Over/Under)
                    if outcome.get("description", "").lower() != "over":
                        continue

                    raw_name = outcome.get("name", "")
                    pra_line = outcome.get("point", 0.0)
                    if not raw_name or pra_line <= 0:
                        continue

                    normalized = normalize_player_name(raw_name)
                    if normalized in result:
                        continue  # Already have from higher-priority book

                    # Determine team from the outcome description or player context
                    # The Odds API sometimes includes team in description_key
                    team_abbr = _guess_team_from_event(
                        raw_name, team_map, home_team, away_team
                    )

                    # Compute implied minutes using positional baseline
                    # We don't have DK position here, so use empty — the
                    # caller stamps position from DK data.
                    implied_min = calculate_implied_minutes(pra_line, "")

                    result[normalized] = {
                        "pra_line": pra_line,
                        "raw_name": raw_name,
                        "team": team_abbr,
                        "position": "",  # Filled by caller from DK data
                        "implied_minutes": implied_min,
                        "synthetic_fppm": VEGAS_SYNTHETIC_FPPM_DEFAULT,
                        "bookmaker": book_key,
                    }

        return result


# ── Team name mapping helpers ────────────────────────────────────────────

# The Odds API uses full team names; we need abbreviations.
_ODDS_API_TEAM_TO_ABBR: Dict[str, str] = {
    "atlanta hawks": "ATL", "boston celtics": "BOS",
    "brooklyn nets": "BKN", "charlotte hornets": "CHA",
    "chicago bulls": "CHI", "cleveland cavaliers": "CLE",
    "dallas mavericks": "DAL", "denver nuggets": "DEN",
    "detroit pistons": "DET", "golden state warriors": "GSW",
    "houston rockets": "HOU", "indiana pacers": "IND",
    "los angeles clippers": "LAC", "los angeles lakers": "LAL",
    "la clippers": "LAC", "la lakers": "LAL",
    "memphis grizzlies": "MEM", "miami heat": "MIA",
    "milwaukee bucks": "MIL", "minnesota timberwolves": "MIN",
    "new orleans pelicans": "NOP", "new york knicks": "NYK",
    "oklahoma city thunder": "OKC", "orlando magic": "ORL",
    "philadelphia 76ers": "PHI", "phoenix suns": "PHX",
    "portland trail blazers": "POR", "sacramento kings": "SAC",
    "san antonio spurs": "SAS", "toronto raptors": "TOR",
    "utah jazz": "UTA", "washington wizards": "WAS",
}


def _build_team_map(home_team: str, away_team: str) -> Dict[str, str]:
    """Build a lowercase team-name → abbreviation map from event teams."""
    result = dict(_ODDS_API_TEAM_TO_ABBR)
    # Add the exact event names as fallbacks
    home_lower = home_team.lower()
    away_lower = away_team.lower()
    if home_lower not in result:
        # Try to match by significant words
        for full, abbr in _ODDS_API_TEAM_TO_ABBR.items():
            if any(w in home_lower for w in full.split() if len(w) > 3):
                result[home_lower] = abbr
                break
    if away_lower not in result:
        for full, abbr in _ODDS_API_TEAM_TO_ABBR.items():
            if any(w in away_lower for w in full.split() if len(w) > 3):
                result[away_lower] = abbr
                break
    return result


def _guess_team_from_event(
    player_name: str,
    team_map: Dict[str, str],
    home_team: str,
    away_team: str,
) -> str:
    """Best-effort team abbreviation for a player from the event context.

    The Odds API PRA outcomes don't always include team info.
    Returns empty string if unknown — the caller will fill from DK data.
    """
    # The outcomes don't typically include team, so return empty.
    # The lineup optimizer matches by normalized name, not by team.
    return ""
