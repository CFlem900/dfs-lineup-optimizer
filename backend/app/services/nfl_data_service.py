"""NFL data service.

Provides the team reference table and per-team rotation hooks. Currently
no rotation engine — ``build_team_rotation`` returns ``None`` so the
lineup builder falls through to the DK-fallback path. The team table
itself is real and authoritative: 32 NFL franchises with their canonical
DK abbreviations, conference/division metadata, and the ESPN team IDs
required to merge ESPN scoreboard data into our internal schema.

External-id translation:
  - ``id``       — internal sequential ID (1..32). Used as the polymorphic
                    ``team_id`` foreign key in the polymorphic DB tables.
  - ``espn_id``  — ESPN's NFL team ID (1..34, with gaps). Used by
                    :class:`NFLGameService` to resolve scoreboard
                    competitors back into our team records.

If ESPN ever renames a franchise (Commanders, Athletics, etc.) the
``abbreviation`` and ``full_name`` fields here are the authoritative
DK-side strings; the ESPN ID is what we use to match feed data.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


_ESPN_NFL_TEAMS_URL = (
    "https://site.api.espn.com/apis/site/v2/sports/football/nfl/teams?limit=50"
)
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


def _espn_logo_url(abbr: str) -> str:
    """ESPN's stable CDN pattern for NFL team logos.

    Same scheme ESPN's own apps use, so it works without going through
    the team API. Used as the offline default so a fresh
    ``NFLDataService()`` doesn't have to make a network call to render
    the team table.
    """
    return f"https://a.espncdn.com/i/teamlogos/nfl/500/{abbr.lower()}.png"


# ── 32 NFL franchises ─────────────────────────────────────────────────
# Layout: id, abbr, full name, city, conference, division, espn_id.
# Sourced from ESPN's NFL team listing (site.api.espn.com/.../nfl/teams).
# ESPN IDs are stable historical numbers with gaps (no IDs 31, 32 — the
# range goes up to 34).
_NFL_TEAMS: List[Dict[str, Any]] = [
    {"id": 1,  "abbreviation": "ARI", "full_name": "Arizona Cardinals",    "city": "Arizona",        "conference": "NFC", "division": "West",    "espn_id": 22},
    {"id": 2,  "abbreviation": "ATL", "full_name": "Atlanta Falcons",      "city": "Atlanta",        "conference": "NFC", "division": "South",   "espn_id": 1},
    {"id": 3,  "abbreviation": "BAL", "full_name": "Baltimore Ravens",     "city": "Baltimore",      "conference": "AFC", "division": "North",   "espn_id": 33},
    {"id": 4,  "abbreviation": "BUF", "full_name": "Buffalo Bills",        "city": "Buffalo",        "conference": "AFC", "division": "East",    "espn_id": 2},
    {"id": 5,  "abbreviation": "CAR", "full_name": "Carolina Panthers",    "city": "Carolina",       "conference": "NFC", "division": "South",   "espn_id": 29},
    {"id": 6,  "abbreviation": "CHI", "full_name": "Chicago Bears",        "city": "Chicago",        "conference": "NFC", "division": "North",   "espn_id": 3},
    {"id": 7,  "abbreviation": "CIN", "full_name": "Cincinnati Bengals",   "city": "Cincinnati",     "conference": "AFC", "division": "North",   "espn_id": 4},
    {"id": 8,  "abbreviation": "CLE", "full_name": "Cleveland Browns",     "city": "Cleveland",      "conference": "AFC", "division": "North",   "espn_id": 5},
    {"id": 9,  "abbreviation": "DAL", "full_name": "Dallas Cowboys",       "city": "Dallas",         "conference": "NFC", "division": "East",    "espn_id": 6},
    {"id": 10, "abbreviation": "DEN", "full_name": "Denver Broncos",       "city": "Denver",         "conference": "AFC", "division": "West",    "espn_id": 7},
    {"id": 11, "abbreviation": "DET", "full_name": "Detroit Lions",        "city": "Detroit",        "conference": "NFC", "division": "North",   "espn_id": 8},
    {"id": 12, "abbreviation": "GB",  "full_name": "Green Bay Packers",    "city": "Green Bay",      "conference": "NFC", "division": "North",   "espn_id": 9},
    {"id": 13, "abbreviation": "HOU", "full_name": "Houston Texans",       "city": "Houston",        "conference": "AFC", "division": "South",   "espn_id": 34},
    {"id": 14, "abbreviation": "IND", "full_name": "Indianapolis Colts",   "city": "Indianapolis",   "conference": "AFC", "division": "South",   "espn_id": 11},
    {"id": 15, "abbreviation": "JAX", "full_name": "Jacksonville Jaguars", "city": "Jacksonville",   "conference": "AFC", "division": "South",   "espn_id": 30},
    {"id": 16, "abbreviation": "KC",  "full_name": "Kansas City Chiefs",   "city": "Kansas City",    "conference": "AFC", "division": "West",    "espn_id": 12},
    {"id": 17, "abbreviation": "LV",  "full_name": "Las Vegas Raiders",    "city": "Las Vegas",      "conference": "AFC", "division": "West",    "espn_id": 13},
    {"id": 18, "abbreviation": "LAC", "full_name": "Los Angeles Chargers", "city": "Los Angeles",    "conference": "AFC", "division": "West",    "espn_id": 24},
    {"id": 19, "abbreviation": "LAR", "full_name": "Los Angeles Rams",     "city": "Los Angeles",    "conference": "NFC", "division": "West",    "espn_id": 14},
    {"id": 20, "abbreviation": "MIA", "full_name": "Miami Dolphins",       "city": "Miami",          "conference": "AFC", "division": "East",    "espn_id": 15},
    {"id": 21, "abbreviation": "MIN", "full_name": "Minnesota Vikings",    "city": "Minnesota",      "conference": "NFC", "division": "North",   "espn_id": 16},
    {"id": 22, "abbreviation": "NE",  "full_name": "New England Patriots", "city": "New England",    "conference": "AFC", "division": "East",    "espn_id": 17},
    {"id": 23, "abbreviation": "NO",  "full_name": "New Orleans Saints",   "city": "New Orleans",    "conference": "NFC", "division": "South",   "espn_id": 18},
    {"id": 24, "abbreviation": "NYG", "full_name": "New York Giants",      "city": "New York",       "conference": "NFC", "division": "East",    "espn_id": 19},
    {"id": 25, "abbreviation": "NYJ", "full_name": "New York Jets",        "city": "New York",       "conference": "AFC", "division": "East",    "espn_id": 20},
    {"id": 26, "abbreviation": "PHI", "full_name": "Philadelphia Eagles",  "city": "Philadelphia",   "conference": "NFC", "division": "East",    "espn_id": 21},
    {"id": 27, "abbreviation": "PIT", "full_name": "Pittsburgh Steelers",  "city": "Pittsburgh",     "conference": "AFC", "division": "North",   "espn_id": 23},
    {"id": 28, "abbreviation": "SF",  "full_name": "San Francisco 49ers",  "city": "San Francisco",  "conference": "NFC", "division": "West",    "espn_id": 25},
    {"id": 29, "abbreviation": "SEA", "full_name": "Seattle Seahawks",     "city": "Seattle",        "conference": "NFC", "division": "West",    "espn_id": 26},
    {"id": 30, "abbreviation": "TB",  "full_name": "Tampa Bay Buccaneers", "city": "Tampa Bay",      "conference": "NFC", "division": "South",   "espn_id": 27},
    {"id": 31, "abbreviation": "TEN", "full_name": "Tennessee Titans",     "city": "Tennessee",      "conference": "AFC", "division": "South",   "espn_id": 10},
    {"id": 32, "abbreviation": "WAS", "full_name": "Washington Commanders","city": "Washington",     "conference": "NFC", "division": "East",    "espn_id": 28},
]

# Inject ESPN CDN logo URLs onto every team. Doing this once at module
# import keeps the table definition above readable. Real ESPN responses
# can override these per-team via NFLDataService.fetch_nfl_teams().
for _t in _NFL_TEAMS:
    _t["logo_url"] = _espn_logo_url(_t["abbreviation"])


# Pre-built lookup indices for O(1) translation
_BY_ID: Dict[int, Dict[str, Any]] = {t["id"]: t for t in _NFL_TEAMS}
_BY_ESPN_ID: Dict[int, Dict[str, Any]] = {t["espn_id"]: t for t in _NFL_TEAMS}
_BY_ABBR: Dict[str, Dict[str, Any]] = {t["abbreviation"].upper(): t for t in _NFL_TEAMS}


class NFLDataService:
    """Authoritative NFL team registry + (eventually) rotation engine.

    Today the rotation hook is a stub — `build_team_rotation` returns
    `None` so the lineup builder uses the DK-fallback path. The team
    table itself is real, used by :class:`NFLGameService` to translate
    ESPN scoreboard competitors and by the lineup builder to render
    team metadata.
    """

    def __init__(self):
        # Mirror NBAMultiSourceService's instance attributes so the
        # lineup builder's defensive ``getattr(svc, '_db_cache', None)``
        # reads find a None and skip the cache path.
        self._db_cache = None
        self._bdl = None

    # ── Public API used by routers / lineup builder ──────────────────

    def get_all_teams(self) -> List[Dict[str, Any]]:
        """Return all 32 NFL teams. Shape matches NBA / CBB output so the
        ``/api/teams?sport=nfl`` endpoint can return rows polymorphically."""
        return [dict(t) for t in _NFL_TEAMS]

    def get_team_by_id(self, team_id: int) -> Optional[Dict[str, Any]]:
        """Look up a team by internal id (1..32). None if unknown."""
        t = _BY_ID.get(int(team_id))
        return dict(t) if t else None

    def get_team_by_espn_id(self, espn_id: int) -> Optional[Dict[str, Any]]:
        """Look up a team by ESPN's NFL team id. Used by NFLGameService."""
        t = _BY_ESPN_ID.get(int(espn_id))
        return dict(t) if t else None

    def get_team_by_abbreviation(self, abbreviation: str) -> Optional[Dict[str, Any]]:
        """Case-insensitive lookup by DK-style 2-3 letter abbreviation."""
        if not abbreviation:
            return None
        t = _BY_ABBR.get(abbreviation.upper())
        return dict(t) if t else None

    def build_team_rotation(self, team_id: int, **kwargs) -> None:
        """Skeleton — no rotation engine yet for NFL. Returning None
        forces the DK-fallback path so the lineup builder still works
        when the user has uploaded projections via CSV."""
        logger.debug(
            "[NFLDataService] build_team_rotation(team_id=%s) — skeleton "
            "returning None (no rotation engine)", team_id,
        )
        return None

    def get_source_status(self) -> Dict[str, Any]:
        return {
            "balldontlie_available": False,
            "db_cache_available": False,
            "nfl_engine_available": False,
            "team_table_count": len(_NFL_TEAMS),
            "live_fetch_at": getattr(self, "_last_fetch_ts", None),
        }

    # ── Live ESPN team fetch ─────────────────────────────────────────

    def fetch_nfl_teams(
        self,
        force_refresh: bool = False,
        ttl_seconds: int = 86_400,
    ) -> List[Dict[str, Any]]:
        """Refresh the team table from ESPN's hidden teams endpoint.

        Returns the same shape as ``get_all_teams()`` but with ESPN's
        live ``logos`` array merged into each team's ``logo_url`` (so
        any branding update — Commanders rename, alternate logos, etc.
        — flows through). Called manually rather than on every request:
        the franchise list and logos change at most a few times per year.

        Always returns a usable list:
          - On success: enriched list, written to a per-instance cache.
          - On failure: the existing cache (if any), else the hardcoded
            ``_NFL_TEAMS`` snapshot — every caller can rely on the
            method returning 32 teams.

        Parameters
        ----------
        force_refresh : bool
            Bypass the TTL cache and re-fetch.
        ttl_seconds : int
            Skip the network call if a fetch happened within this window.
        """
        from app.services.http_resilience import APIGroup, resilient_get

        now = time.time()
        last = getattr(self, "_last_fetch_ts", 0) or 0
        cached = getattr(self, "_live_teams", None)
        if not force_refresh and cached and (now - last) < ttl_seconds:
            return cached

        try:
            resp = resilient_get(
                _ESPN_NFL_TEAMS_URL,
                group=APIGroup.ESPN_NFL,
                headers={"User-Agent": _USER_AGENT},
            )
            payload = resp.json()
        except Exception as exc:
            logger.warning(
                "[NFLDataService] ESPN teams fetch failed (%s) — using "
                "hardcoded fallback", exc,
            )
            return cached or self.get_all_teams()

        # ESPN response shape:
        #   sports[0].leagues[0].teams[].team{id, abbreviation, displayName, logos[].href}
        try:
            espn_teams = (
                payload.get("sports", [{}])[0]
                .get("leagues", [{}])[0]
                .get("teams", [])
            )
        except (IndexError, AttributeError):
            espn_teams = []

        # Merge ESPN logos onto our hardcoded table indexed by ESPN id.
        merged: List[Dict[str, Any]] = []
        for base in _NFL_TEAMS:
            row = dict(base)
            for wrapped in espn_teams:
                t = wrapped.get("team") or {}
                try:
                    if int(t.get("id", -1)) == base["espn_id"]:
                        # Prefer the largest available logo for crispness.
                        logos = t.get("logos") or []
                        if logos:
                            best = max(
                                logos,
                                key=lambda lg: lg.get("width", 0) or 0,
                            )
                            row["logo_url"] = best.get("href") or row["logo_url"]
                        # Use ESPN's displayName when it differs (Commanders
                        # rename, etc.) — keeps the table self-correcting.
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
            "[NFLDataService] Refreshed %d teams from ESPN (live logos)",
            len(merged),
        )
        return merged
