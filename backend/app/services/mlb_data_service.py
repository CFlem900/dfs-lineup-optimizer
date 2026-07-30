"""MLB data service.

30-franchise registry with ESPN IDs, logos, and home-park names. The
parks attached here are the seed data for future park-factor
integration (Coors at altitude, Fenway's monster, etc.). The rotation
hook is a stub — `build_team_rotation` returns `None` so the lineup
builder uses the DK-fallback path.

ESPN MLB team IDs run 1..30 contiguously (no gaps, unlike NFL).
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


_ESPN_MLB_TEAMS_URL = (
    "https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/teams?limit=50"
)
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


def _espn_logo_url(abbr: str) -> str:
    """ESPN's stable CDN pattern for MLB team logos."""
    return f"https://a.espncdn.com/i/teamlogos/mlb/500/{abbr.lower()}.png"


# 30 MLB franchises with ESPN ids + home parks. The DK-side abbreviation
# (used in draftables team_abbreviation) is the canonical key.
_MLB_TEAMS: List[Dict[str, Any]] = [
    {"id": 1,  "abbreviation": "ARI", "full_name": "Arizona Diamondbacks", "city": "Arizona",       "league": "NL", "division": "West",    "espn_id": 29, "home_park": "Chase Field"},
    {"id": 2,  "abbreviation": "ATL", "full_name": "Atlanta Braves",       "city": "Atlanta",       "league": "NL", "division": "East",    "espn_id": 15, "home_park": "Truist Park"},
    {"id": 3,  "abbreviation": "BAL", "full_name": "Baltimore Orioles",    "city": "Baltimore",     "league": "AL", "division": "East",    "espn_id": 1,  "home_park": "Camden Yards"},
    {"id": 4,  "abbreviation": "BOS", "full_name": "Boston Red Sox",       "city": "Boston",        "league": "AL", "division": "East",    "espn_id": 2,  "home_park": "Fenway Park"},
    {"id": 5,  "abbreviation": "CHC", "full_name": "Chicago Cubs",         "city": "Chicago",       "league": "NL", "division": "Central", "espn_id": 16, "home_park": "Wrigley Field"},
    {"id": 6,  "abbreviation": "CWS", "full_name": "Chicago White Sox",    "city": "Chicago",       "league": "AL", "division": "Central", "espn_id": 4,  "home_park": "Guaranteed Rate Field"},
    {"id": 7,  "abbreviation": "CIN", "full_name": "Cincinnati Reds",      "city": "Cincinnati",    "league": "NL", "division": "Central", "espn_id": 17, "home_park": "Great American Ball Park"},
    {"id": 8,  "abbreviation": "CLE", "full_name": "Cleveland Guardians",  "city": "Cleveland",     "league": "AL", "division": "Central", "espn_id": 5,  "home_park": "Progressive Field"},
    {"id": 9,  "abbreviation": "COL", "full_name": "Colorado Rockies",     "city": "Colorado",      "league": "NL", "division": "West",    "espn_id": 27, "home_park": "Coors Field"},
    {"id": 10, "abbreviation": "DET", "full_name": "Detroit Tigers",       "city": "Detroit",       "league": "AL", "division": "Central", "espn_id": 6,  "home_park": "Comerica Park"},
    {"id": 11, "abbreviation": "HOU", "full_name": "Houston Astros",       "city": "Houston",       "league": "AL", "division": "West",    "espn_id": 18, "home_park": "Minute Maid Park"},
    {"id": 12, "abbreviation": "KC",  "full_name": "Kansas City Royals",   "city": "Kansas City",   "league": "AL", "division": "Central", "espn_id": 7,  "home_park": "Kauffman Stadium"},
    {"id": 13, "abbreviation": "LAA", "full_name": "Los Angeles Angels",   "city": "Los Angeles",   "league": "AL", "division": "West",    "espn_id": 3,  "home_park": "Angel Stadium"},
    {"id": 14, "abbreviation": "LAD", "full_name": "Los Angeles Dodgers",  "city": "Los Angeles",   "league": "NL", "division": "West",    "espn_id": 19, "home_park": "Dodger Stadium"},
    {"id": 15, "abbreviation": "MIA", "full_name": "Miami Marlins",        "city": "Miami",         "league": "NL", "division": "East",    "espn_id": 28, "home_park": "loanDepot park"},
    {"id": 16, "abbreviation": "MIL", "full_name": "Milwaukee Brewers",    "city": "Milwaukee",     "league": "NL", "division": "Central", "espn_id": 8,  "home_park": "American Family Field"},
    {"id": 17, "abbreviation": "MIN", "full_name": "Minnesota Twins",      "city": "Minnesota",     "league": "AL", "division": "Central", "espn_id": 9,  "home_park": "Target Field"},
    {"id": 18, "abbreviation": "NYM", "full_name": "New York Mets",        "city": "New York",      "league": "NL", "division": "East",    "espn_id": 21, "home_park": "Citi Field"},
    {"id": 19, "abbreviation": "NYY", "full_name": "New York Yankees",     "city": "New York",      "league": "AL", "division": "East",    "espn_id": 10, "home_park": "Yankee Stadium"},
    {"id": 20, "abbreviation": "OAK", "full_name": "Oakland Athletics",    "city": "Oakland",       "league": "AL", "division": "West",    "espn_id": 11, "home_park": "Sutter Health Park"},
    {"id": 21, "abbreviation": "PHI", "full_name": "Philadelphia Phillies","city": "Philadelphia",  "league": "NL", "division": "East",    "espn_id": 22, "home_park": "Citizens Bank Park"},
    {"id": 22, "abbreviation": "PIT", "full_name": "Pittsburgh Pirates",   "city": "Pittsburgh",    "league": "NL", "division": "Central", "espn_id": 23, "home_park": "PNC Park"},
    {"id": 23, "abbreviation": "SD",  "full_name": "San Diego Padres",     "city": "San Diego",     "league": "NL", "division": "West",    "espn_id": 25, "home_park": "Petco Park"},
    {"id": 24, "abbreviation": "SF",  "full_name": "San Francisco Giants", "city": "San Francisco", "league": "NL", "division": "West",    "espn_id": 26, "home_park": "Oracle Park"},
    {"id": 25, "abbreviation": "SEA", "full_name": "Seattle Mariners",     "city": "Seattle",       "league": "AL", "division": "West",    "espn_id": 12, "home_park": "T-Mobile Park"},
    {"id": 26, "abbreviation": "STL", "full_name": "St. Louis Cardinals",  "city": "St. Louis",     "league": "NL", "division": "Central", "espn_id": 24, "home_park": "Busch Stadium"},
    {"id": 27, "abbreviation": "TB",  "full_name": "Tampa Bay Rays",       "city": "Tampa Bay",     "league": "AL", "division": "East",    "espn_id": 30, "home_park": "Tropicana Field"},
    {"id": 28, "abbreviation": "TEX", "full_name": "Texas Rangers",        "city": "Texas",         "league": "AL", "division": "West",    "espn_id": 13, "home_park": "Globe Life Field"},
    {"id": 29, "abbreviation": "TOR", "full_name": "Toronto Blue Jays",    "city": "Toronto",       "league": "AL", "division": "East",    "espn_id": 14, "home_park": "Rogers Centre"},
    {"id": 30, "abbreviation": "WSH", "full_name": "Washington Nationals", "city": "Washington",    "league": "NL", "division": "East",    "espn_id": 20, "home_park": "Nationals Park"},
]


# Inject ESPN CDN logo URLs onto every team — keeps the table block above
# legible. fetch_mlb_teams() can override per-team with live ESPN logos.
for _t in _MLB_TEAMS:
    _t["logo_url"] = _espn_logo_url(_t["abbreviation"])


_BY_ID: Dict[int, Dict[str, Any]] = {t["id"]: t for t in _MLB_TEAMS}
_BY_ESPN_ID: Dict[int, Dict[str, Any]] = {t["espn_id"]: t for t in _MLB_TEAMS}
_BY_ABBR: Dict[str, Dict[str, Any]] = {t["abbreviation"].upper(): t for t in _MLB_TEAMS}


class MLBDataService:
    """Authoritative MLB team registry. Mirrors NFLDataService's contract."""

    def __init__(self):
        self._db_cache = None
        self._bdl = None

    def get_all_teams(self) -> List[Dict[str, Any]]:
        return [dict(t) for t in _MLB_TEAMS]

    def get_team_by_id(self, team_id: int) -> Optional[Dict[str, Any]]:
        t = _BY_ID.get(int(team_id))
        return dict(t) if t else None

    def get_team_by_espn_id(self, espn_id: int) -> Optional[Dict[str, Any]]:
        t = _BY_ESPN_ID.get(int(espn_id))
        return dict(t) if t else None

    def get_team_by_abbreviation(self, abbreviation: str) -> Optional[Dict[str, Any]]:
        if not abbreviation:
            return None
        t = _BY_ABBR.get(abbreviation.upper())
        return dict(t) if t else None

    def build_team_rotation(self, team_id: int, **kwargs) -> None:
        """Skeleton — no batting-order engine yet. The DK-fallback path
        in the lineup builder picks up draftables-only pools."""
        logger.debug(
            "[MLBDataService] build_team_rotation(%s) — skeleton, returning None",
            team_id,
        )
        return None

    def get_source_status(self) -> Dict[str, Any]:
        return {
            "balldontlie_available": False,
            "db_cache_available": False,
            "mlb_engine_available": False,
            "team_table_count": len(_MLB_TEAMS),
            "live_fetch_at": getattr(self, "_last_fetch_ts", None),
        }

    def fetch_mlb_teams(
        self,
        force_refresh: bool = False,
        ttl_seconds: int = 86_400,
    ) -> List[Dict[str, Any]]:
        """Refresh logos + display names from ESPN's hidden teams API.

        Same TTL-cached pattern as NFLDataService.fetch_nfl_teams: 24-hour
        cache, falls back to the hardcoded snapshot on failure. ``home_park``
        is preserved from our table — ESPN's team payload doesn't include
        the venue name (that comes from the scoreboard endpoint).
        """
        from app.services.http_resilience import APIGroup, resilient_get

        now = time.time()
        last = getattr(self, "_last_fetch_ts", 0) or 0
        cached = getattr(self, "_live_teams", None)
        if not force_refresh and cached and (now - last) < ttl_seconds:
            return cached

        try:
            resp = resilient_get(
                _ESPN_MLB_TEAMS_URL,
                group=APIGroup.ESPN_MLB,
                headers={"User-Agent": _USER_AGENT},
            )
            payload = resp.json()
        except Exception as exc:
            logger.warning(
                "[MLBDataService] ESPN teams fetch failed (%s) — using "
                "hardcoded fallback", exc,
            )
            return cached or self.get_all_teams()

        try:
            espn_teams = (
                payload.get("sports", [{}])[0]
                .get("leagues", [{}])[0]
                .get("teams", [])
            )
        except (IndexError, AttributeError):
            espn_teams = []

        merged: List[Dict[str, Any]] = []
        for base in _MLB_TEAMS:
            row = dict(base)
            for wrapped in espn_teams:
                t = wrapped.get("team") or {}
                try:
                    if int(t.get("id", -1)) == base["espn_id"]:
                        logos = t.get("logos") or []
                        if logos:
                            best = max(
                                logos,
                                key=lambda lg: lg.get("width", 0) or 0,
                            )
                            row["logo_url"] = best.get("href") or row["logo_url"]
                        dn = (t.get("displayName") or "").strip()
                        if dn and dn != row["full_name"]:
                            row["full_name"] = dn
                        break
                except (TypeError, ValueError):
                    continue
            merged.append(row)

        self._live_teams = merged
        self._last_fetch_ts = now
        logger.info(
            "[MLBDataService] Refreshed %d teams from ESPN", len(merged),
        )
        return merged
