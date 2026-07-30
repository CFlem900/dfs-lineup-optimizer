"""CBB injury service using ESPN data.

ESPN provides injury data for college basketball through their public
injuries endpoint.  The service follows the same interface as the NBA
:class:`InjuryService` for interchangeable usage.

Note: CBB injury reporting is less formalized than NBA — fewer
official designations and less consistent reporting.
"""

import hashlib
import logging
from datetime import datetime
from typing import Dict, List, Optional

import httpx

from app.models.player import PlayerStatus
from app.services.http_resilience import resilient_get, APIGroup

logger = logging.getLogger(__name__)

_ESPN_INJURIES_URL = (
    "https://site.api.espn.com/apis/site/v2/sports/basketball/"
    "mens-college-basketball/injuries"
)


class CBBInjuryService:
    """Service for fetching CBB injury reports from ESPN.

    Mirrors the interface of :class:`InjuryService` so the rotation
    engine and player pool builder can use either interchangeably.
    """

    def __init__(self):
        self._cache: Optional[List[Dict]] = None
        self._cache_timestamp: Optional[datetime] = None
        self._cache_ttl_seconds = 600  # 10 minutes (CBB updates less frequently)
        self._injury_hash: Optional[str] = None

    def _is_cache_valid(self) -> bool:
        if self._cache_timestamp is None:
            return False
        elapsed = (datetime.now() - self._cache_timestamp).total_seconds()
        return elapsed < self._cache_ttl_seconds

    def _get_raw_injuries(self) -> List[Dict]:
        """Return cached raw injury list, fetching if stale."""
        if self._is_cache_valid() and self._cache is not None:
            return self._cache
        self._cache = self._fetch_from_espn()
        self._cache_timestamp = datetime.now()
        self._injury_hash = self._compute_hash(self._cache)
        return self._cache

    @staticmethod
    def _compute_hash(injuries: List[Dict]) -> str:
        """Compute a stable fingerprint of the injury list."""
        parts = sorted(
            f"{inj.get('player', '')}:{inj.get('status', '')}"
            for inj in injuries
        )
        return hashlib.md5("|".join(parts).encode()).hexdigest()[:12]

    def get_injury_hash(self, team_names: Optional[List[str]] = None) -> str:
        """Return a short hash representing current injury state.

        Args:
            team_names: If provided, only include injuries for these teams
                in the hash to avoid unrelated cache invalidation.
        """
        injuries = self._get_raw_injuries()
        if team_names:
            lowered = {t.lower() for t in team_names}
            filtered = [
                inj for inj in injuries
                if any(t in inj.get("team", "").lower() for t in lowered)
            ]
            return self._compute_hash(filtered)
        return self._injury_hash or ""

    def get_injuries(self, team_name: Optional[str] = None) -> List[PlayerStatus]:
        """Fetch current injury reports. Optionally filter by team."""
        try:
            injuries = self._get_raw_injuries()

            if team_name:
                team_lower = team_name.lower()
                injuries = [
                    inj
                    for inj in injuries
                    if team_lower in inj.get("team", "").lower()
                ]

            return self._parse_injuries(injuries)
        except Exception as e:
            logger.error(f"Failed to fetch CBB injuries: {e}")
            return []

    def get_all_injuries(self) -> List[PlayerStatus]:
        """Return the full (unfiltered) injury list."""
        return self.get_injuries(team_name=None)

    def get_team_injuries(self, team_name: str) -> List[PlayerStatus]:
        """Get injuries filtered by team name."""
        return self.get_injuries(team_name=team_name)

    # ------------------------------------------------------------------
    # ESPN fetch
    # ------------------------------------------------------------------

    @staticmethod
    def _fetch_from_espn() -> List[Dict]:
        """Fetch injury data from ESPN's college basketball injuries endpoint."""
        try:
            resp = resilient_get(_ESPN_INJURIES_URL, group=APIGroup.ESPN_CBB)
            data = resp.json()

            injuries: List[Dict] = []
            for team_entry in data.get("items", []):
                team_name = team_entry.get("team", {}).get("displayName", "")
                team_abbr = team_entry.get("team", {}).get("abbreviation", "")

                for athlete in team_entry.get("injuries", []):
                    player_name = athlete.get("athlete", {}).get(
                        "displayName", ""
                    )
                    status = athlete.get("status", "")
                    description = athlete.get("longComment", "") or athlete.get(
                        "shortComment", ""
                    )
                    injury_type = athlete.get("type", {}).get(
                        "description", ""
                    )

                    parsed_status = _parse_espn_status(status, description)

                    injuries.append({
                        "player": player_name,
                        "team": team_name,
                        "team_abbreviation": team_abbr,
                        "status": parsed_status,
                        "description": description or injury_type,
                    })

            logger.info(f"Fetched {len(injuries)} CBB injuries from ESPN")
            return injuries

        except Exception as e:
            logger.error(f"ESPN CBB injuries fetch failed: {e}")
            return []

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_injuries(raw_injuries: List[Dict]) -> List[PlayerStatus]:
        """Convert raw injury dicts into PlayerStatus objects."""
        statuses = []
        for inj in raw_injuries:
            player_name = inj.get("player", "Unknown")
            status_text = inj.get("status", "Questionable")
            description = inj.get("description", "")

            valid_statuses = {
                "Available", "Out", "Questionable", "Doubtful",
                "GTD", "Game Time Decision",
            }
            if status_text not in valid_statuses:
                status_text = _parse_espn_status(status_text, description)

            statuses.append(
                PlayerStatus(
                    player_id=0,
                    player_name=player_name,
                    status=status_text,
                    injury_description=description,
                )
            )
        return statuses


def _parse_espn_status(status: str, description: str = "") -> str:
    """Map ESPN injury status strings to our standard categories."""
    status_lower = (status or "").lower()
    desc_lower = (description or "").lower()
    combined = f"{status_lower} {desc_lower}"

    if "out" in status_lower or "out for season" in combined:
        return "Out"
    if "doubtful" in status_lower:
        return "Doubtful"
    if "day-to-day" in combined or "day to day" in combined:
        return "GTD"
    if "questionable" in status_lower:
        return "Questionable"
    if "probable" in status_lower:
        return "Questionable"  # Map probable to Questionable

    return "Questionable"  # Default
