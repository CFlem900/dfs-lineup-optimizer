"""Abstract interface for sport-specific data providers.

Both ``NBAApiService`` and ``CBBApiService`` implement this interface,
allowing the lineup optimizer and rotation engine to work with any
basketball sport without knowing the underlying data source.

Usage:
    from app.services.sport_data_service import SportDataService

    def build_pool(data_service: SportDataService, ...):
        teams = data_service.get_all_teams()
        rotation = data_service.build_team_rotation(team_id)
"""

from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Set

from app.models.player import PlayerMinutes


class SportDataService(ABC):
    """Abstract base for sport-specific data providers.

    Concrete implementations:
      - ``NBAApiService``  — uses nba_api + stats.nba.com
      - ``CBBApiService``  — uses ESPN free API + CBBpy
    """

    @abstractmethod
    def get_all_teams(self) -> List[Dict]:
        """Return all teams for this sport.

        Each dict must contain at minimum:
          - ``id``:           int (sport-specific team ID)
          - ``full_name``:    str (e.g. "Los Angeles Lakers")
          - ``abbreviation``: str (e.g. "LAL")
          - ``nickname``:     str (e.g. "Lakers")
          - ``city``:         str (e.g. "Los Angeles")
        """

    @abstractmethod
    def build_team_rotation(
        self,
        team_id: int,
        season: Optional[str] = None,
        max_players: int = 0,
        draftable_names: Optional[Set[str]] = None,
        **kwargs,
    ) -> List[PlayerMinutes]:
        """Build game-night rotation for a team.

        Returns a list of ``PlayerMinutes`` objects sorted by descending
        season average minutes.  Each entry includes per-minute stat
        rates suitable for DFS projection (pts_per_min, reb_per_min, etc.).

        Parameters
        ----------
        team_id : int
            Sport-specific team identifier.
        season : str, optional
            Season identifier (e.g. "2025-26" for NBA, "2025" for CBB).
        max_players : int
            If > 0, limit rotation to this many players.
        draftable_names : set[str], optional
            When provided, only include players whose names match this set
            (for DFS salary pool alignment).
        **kwargs
            Sport-specific options (e.g. ``cache_service`` for NBA).
        """

    @abstractmethod
    def get_today_scoreboard(self) -> List[Dict]:
        """Fetch today's games.

        Each dict should contain game_id, home_team, away_team, status,
        and start time.
        """

    @abstractmethod
    def find_team_by_name(self, team_name: str) -> Optional[Dict]:
        """Find a team by name, abbreviation, or nickname.

        Returns the team dict (same format as ``get_all_teams()`` entries)
        or ``None`` if no match.
        """
