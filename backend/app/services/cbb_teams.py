"""NCAA D1 men's basketball team registry.

Fetches the full list of ~360 D1 teams from ESPN's public API and
provides lookup by team ID, name, or abbreviation.  Results are
cached aggressively (24-hour TTL) because the team list only changes
once per year.

Usage:
    registry = CBBTeamRegistry()
    teams = registry.get_all_teams()
    team = registry.find_team_by_name("Duke")
"""

import logging
import time
from typing import Dict, List, Optional

import httpx

from app.services.http_resilience import resilient_get, APIGroup

logger = logging.getLogger(__name__)

_ESPN_TEAMS_URL = (
    "https://site.api.espn.com/apis/site/v2/sports/basketball/"
    "mens-college-basketball/teams?limit=400"
)

# Module-level cache shared across instances
_teams_cache: Optional[List[Dict]] = None
_teams_cache_ts: float = 0.0
_TEAMS_CACHE_TTL = 86400  # 24 hours — team list barely changes


class CBBTeamRegistry:
    """ESPN-backed registry of NCAA D1 men's basketball teams."""

    def get_all_teams(self) -> List[Dict]:
        """Return all D1 teams in the standard team dict format.

        Each dict contains:
          - ``id``:           int (ESPN team ID)
          - ``full_name``:    str (e.g. "Duke Blue Devils")
          - ``abbreviation``: str (e.g. "DUKE")
          - ``nickname``:     str (e.g. "Blue Devils")
          - ``city``:         str (school location or empty)
          - ``conference``:   str (e.g. "ACC")
        """
        global _teams_cache, _teams_cache_ts
        now = time.time()
        if _teams_cache and (now - _teams_cache_ts) < _TEAMS_CACHE_TTL:
            return _teams_cache

        try:
            teams = self._fetch_espn_teams()
            _teams_cache = teams
            _teams_cache_ts = time.time()
            logger.info(f"CBB team registry loaded: {len(teams)} teams")
            return teams
        except Exception as e:
            logger.error(f"Failed to fetch CBB teams from ESPN: {e}")
            if _teams_cache:
                logger.info("Returning stale CBB team cache")
                return _teams_cache
            return []

    def find_team_by_id(self, team_id: int) -> Optional[Dict]:
        """Look up a team by ESPN team ID."""
        for t in self.get_all_teams():
            if t["id"] == team_id:
                return t
        return None

    def find_team_by_name(self, team_name: str) -> Optional[Dict]:
        """Find a team by name, abbreviation, or nickname.

        Uses case-insensitive substring matching.  Returns the first
        match or ``None``.
        """
        if not team_name:
            return None

        query = team_name.lower().strip()
        teams = self.get_all_teams()

        # 1. Exact abbreviation match (fastest)
        for t in teams:
            if t["abbreviation"].lower() == query:
                return t

        # 2. Exact nickname match
        for t in teams:
            if t["nickname"].lower() == query:
                return t

        # 3. Full name match
        for t in teams:
            if t["full_name"].lower() == query:
                return t

        # 4. Substring match (broadest)
        for t in teams:
            if (
                query in t["full_name"].lower()
                or query in t["nickname"].lower()
                or query in t["abbreviation"].lower()
            ):
                return t

        return None

    def get_teams_by_conference(self) -> Dict[str, List[Dict]]:
        """Group all teams by conference name."""
        by_conf: Dict[str, List[Dict]] = {}
        for t in self.get_all_teams():
            conf = t.get("conference", "Unknown")
            by_conf.setdefault(conf, []).append(t)
        return by_conf

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @staticmethod
    def _fetch_espn_teams() -> List[Dict]:
        """Fetch team list from ESPN public API."""
        resp = resilient_get(_ESPN_TEAMS_URL, group=APIGroup.ESPN_CBB)
        data = resp.json()

        teams: List[Dict] = []
        for entry in data.get("sports", [{}])[0].get("leagues", [{}])[0].get("teams", []):
            t = entry.get("team", {})
            team_id = int(t.get("id", 0))
            if not team_id:
                continue

            # Extract conference from groups
            groups = t.get("groups", {})
            conference = ""
            if groups and groups.get("parent"):
                conference = groups["parent"].get("shortName", "")
            elif groups:
                conference = groups.get("shortName", "")

            teams.append({
                "id": team_id,
                "full_name": t.get("displayName", t.get("name", "")),
                "abbreviation": t.get("abbreviation", ""),
                "nickname": t.get("shortDisplayName", t.get("name", "")),
                "city": t.get("location", ""),
                "conference": conference,
            })

        return teams
