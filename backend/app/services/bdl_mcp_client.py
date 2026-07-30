"""Typed wrapper for the BallDontLie MCP tool surface.

The BDL MCP exposes exactly four tools:

  - ``get_teams``   → list NBA/MLB/NFL teams
  - ``get_players`` → search/list players (with optional name filter)
  - ``get_games``   → list games by date/season/team
  - ``get_game``    → get a single game by ID

This client enforces that contract with:

  1. **Pydantic-validated inputs and outputs** — every call returns typed
     models from ``app.models.balldontlie_schemas``.
  2. **Explicit ``NotImplemented`` guard** — any attempt to call a ``props``
     method logs a clear error and raises ``BDLMethodNotAvailable`` so
     callers don't silently assume props data exists.
  3. **InjuryService integration** — ``get_active_roster()`` filters the
     raw player list through the injury report, returning only players
     whose status is not ``"Out"`` and who haven't been auto-DNP'd.

Usage
-----
::

    from app.services.bdl_mcp_client import BDLMCPClient

    client = BDLMCPClient(bdl_service, injury_service)

    teams = client.get_teams()
    players = client.get_players(team_abbr="DAL")
    active  = client.get_active_roster("DAL")   # filtered by injuries
    games   = client.get_games(date="2026-02-25")
    game    = client.get_game(game_id=12345)

    client.get_player_props(...)  # raises BDLMethodNotAvailable
"""

from __future__ import annotations

import logging
from typing import List, Literal, Optional, Set

from app.models.balldontlie_schemas import (
    BDLGame,
    BDLPlayer,
    BDLTeam,
)

logger = logging.getLogger(__name__)

# ── Supported league type ─────────────────────────────────────────────────

League = Literal["NBA", "MLB", "NFL"]

# ── Unsupported methods (explicit blocklist) ─────────────────────────────

_UNSUPPORTED_METHODS = frozenset({
    "get_player_props",
    "get_props",
    "get_odds",
    "get_betting_lines",
    "get_player_stats",
    "get_season_averages",
    "get_live_box_scores",
    "get_box_scores",
    "get_stats",
})


# ── Exception ─────────────────────────────────────────────────────────────


class BDLMethodNotAvailable(NotImplementedError):
    """Raised when caller tries to use a BDL MCP endpoint that doesn't exist.

    The BDL MCP surface is limited to four tools: ``get_teams``,
    ``get_players``, ``get_games``, ``get_game``.  Any attempt to call
    props, odds, or stats endpoints through this client will raise this
    exception so callers fail fast rather than silently returning None.
    """


# ── Core client ───────────────────────────────────────────────────────────


class BDLMCPClient:
    """Typed, contract-enforcing wrapper around the BDL MCP tools.

    Parameters
    ----------
    bdl_service : BallDontLieService
        The underlying REST client (``balldontlie_service.py``).
    injury_service : InjuryService, optional
        Used by ``get_active_roster()`` to filter out injured players.
        If ``None``, ``get_active_roster()`` returns the full roster.
    """

    def __init__(self, bdl_service, injury_service=None):
        self._bdl = bdl_service
        self._injury = injury_service

    @property
    def is_available(self) -> bool:
        """True if the underlying BDL service has a valid API key."""
        return bool(self._bdl and getattr(self._bdl, "is_available", True))

    # ── MCP Tool 1: get_teams ─────────────────────────────────────────

    def get_teams(self, league: League = "NBA") -> List[BDLTeam]:
        """List all teams for a league.

        Maps to MCP tool ``get_teams(league)``.

        Returns:
            List of ``BDLTeam`` models, validated from the raw API response.
        """
        self._require_available()

        self._bdl._ensure_team_mapping()
        teams: List[BDLTeam] = []
        for bdl_id, abbr in self._bdl._bdl_team_abbrs.items():
            teams.append(BDLTeam(id=bdl_id, abbreviation=abbr))
        return teams

    # ── MCP Tool 2: get_players ───────────────────────────────────────

    def get_players(
        self,
        league: League = "NBA",
        *,
        team_abbr: Optional[str] = None,
        first_name: Optional[str] = None,
        last_name: Optional[str] = None,
    ) -> List[BDLPlayer]:
        """Search or list players.

        Maps to MCP tool ``get_players(league, firstName, lastName)``.

        Args:
            league: League filter (default ``"NBA"``).
            team_abbr: Optional team abbreviation to filter by.
            first_name: Optional first name search filter.
            last_name: Optional last name search filter.

        Returns:
            List of ``BDLPlayer`` models.
        """
        self._require_available()

        if team_abbr:
            raw = self._fetch_players_by_team(team_abbr)
        else:
            raw = self._fetch_players_search(first_name, last_name)

        players: List[BDLPlayer] = []
        for p in raw:
            try:
                players.append(BDLPlayer.model_validate(p))
            except Exception as e:
                logger.debug(f"[BDLClient] Skipping invalid player: {e}")
        return players

    # ── MCP Tool 3: get_games ─────────────────────────────────────────

    def get_games(
        self,
        league: League = "NBA",
        *,
        date: Optional[str] = None,
        season: Optional[int] = None,
        team_id: Optional[int] = None,
    ) -> List[BDLGame]:
        """List games filtered by date, season, or team.

        Maps to MCP tool ``get_games(league, dates, seasons, teamIds)``.

        Args:
            league: League filter.
            date: ``"YYYY-MM-DD"`` date filter.
            season: Season year (e.g. ``2025`` for 2025-26).
            team_id: BDL team ID filter.

        Returns:
            List of ``BDLGame`` models.
        """
        self._require_available()

        params = []
        if date:
            params.append(f"dates[]={date}")
        if season:
            params.append(f"seasons[]={season}")
        if team_id:
            params.append(f"team_ids[]={team_id}")
        params.append("per_page=100")

        path = f"/games?{'&'.join(params)}"
        result = self._bdl._get(path)

        games: List[BDLGame] = []
        for g in result.get("data", []):
            try:
                games.append(BDLGame.model_validate(g))
            except Exception as e:
                logger.debug(f"[BDLClient] Skipping invalid game: {e}")
        return games

    # ── MCP Tool 4: get_game ──────────────────────────────────────────

    def get_game(
        self, game_id: int, league: League = "NBA"
    ) -> Optional[BDLGame]:
        """Get a single game by its BDL ID.

        Maps to MCP tool ``get_game(league, gameId)``.

        Args:
            game_id: The BDL game ID.
            league: League (default ``"NBA"``).

        Returns:
            A ``BDLGame`` model, or ``None`` if not found.
        """
        self._require_available()

        try:
            result = self._bdl._get(f"/games/{game_id}")
            data = result if "id" in result else result.get("data", result)
            return BDLGame.model_validate(data)
        except Exception as e:
            logger.warning(f"[BDLClient] get_game({game_id}) failed: {e}")
            return None

    # ── InjuryService integration: get_active_roster ──────────────────

    def get_active_roster(
        self,
        team_abbr: str,
        league: League = "NBA",
    ) -> List[BDLPlayer]:
        """Get players for a team filtered to active roster members only.

        Fetches the full player list via ``get_players()``, then removes
        any player whose InjuryService status is ``"Out"`` or whose
        effective factor is ``0.00`` (DNP-CD/AUTO-OUT).

        If no InjuryService was provided at construction time, returns the
        unfiltered roster.

        Args:
            team_abbr: Team abbreviation (e.g. ``"DAL"``).
            league: League filter (default ``"NBA"``).

        Returns:
            List of ``BDLPlayer`` models for active (non-Out) players.
        """
        all_players = self.get_players(league, team_abbr=team_abbr)

        if not self._injury:
            logger.debug(
                "[BDLClient] No InjuryService — returning full roster "
                f"({len(all_players)} players) for {team_abbr}"
            )
            return all_players

        # Build set of player names who are ruled Out or DNP-auto-out
        out_names: Set[str] = set()
        try:
            injuries = self._injury.get_active_injuries(team_name=team_abbr)
            for inj in injuries:
                status = inj.get("status", "")
                factor = inj.get("official_factor", 1.0)
                if status == "Out" or factor == 0.0:
                    name = inj.get("player_name", "")
                    if name:
                        out_names.add(name.lower())
        except Exception as e:
            logger.warning(
                f"[BDLClient] InjuryService lookup failed for {team_abbr}, "
                f"returning full roster: {e}"
            )
            return all_players

        if not out_names:
            return all_players

        active = [
            p for p in all_players
            if p.full_name.lower() not in out_names
        ]

        logger.info(
            f"[BDLClient] Active roster for {team_abbr}: "
            f"{len(active)}/{len(all_players)} "
            f"(filtered out: {out_names})"
        )
        return active

    # ── Props guard (__getattr__) ─────────────────────────────────────

    def __getattr__(self, name: str):
        """Intercept calls to unsupported MCP methods.

        If any code tries to call ``client.get_player_props()``,
        ``client.get_props()``, or any other blocklisted method, this
        logs a clear ERROR and raises ``BDLMethodNotAvailable`` so the
        caller fails fast instead of silently getting ``None``.
        """
        if name in _UNSUPPORTED_METHODS:
            logger.error(
                f"[BDLClient] '{name}' is NOT available in the BDL MCP. "
                f"The MCP only supports: get_teams, get_players, get_games, "
                f"get_game. Use BallDontLieService directly for REST-only "
                f"endpoints like odds/stats."
            )
            raise BDLMethodNotAvailable(
                f"BDL MCP does not support '{name}'. "
                f"Available tools: get_teams, get_players, get_games, get_game."
            )
        raise AttributeError(
            f"'{type(self).__name__}' object has no attribute '{name}'"
        )

    # ── Private helpers ───────────────────────────────────────────────

    def _require_available(self) -> None:
        """Raise RuntimeError if the BDL service is not configured."""
        if not self.is_available:
            raise RuntimeError(
                "[BDLClient] BallDontLie service is not available. "
                "Check that BALLDONTLIE_API_KEY is set."
            )

    def _fetch_players_by_team(self, team_abbr: str) -> List[dict]:
        """Fetch all players for a team abbreviation via the REST API."""
        self._bdl._ensure_team_mapping()

        # Reverse-lookup: abbreviation → BDL team ID
        bdl_team_id: Optional[int] = None
        for tid, abbr in self._bdl._bdl_team_abbrs.items():
            if abbr.upper() == team_abbr.upper():
                bdl_team_id = tid
                break

        if bdl_team_id is None:
            logger.warning(
                f"[BDLClient] No BDL team mapping for abbreviation '{team_abbr}'"
            )
            return []

        path = f"/players?team_ids[]={bdl_team_id}&per_page=100"
        result = self._bdl._get(path)
        return result.get("data", [])

    def _fetch_players_search(
        self,
        first_name: Optional[str],
        last_name: Optional[str],
    ) -> List[dict]:
        """Search players by name via the REST API."""
        params = ["per_page=100"]
        if first_name:
            params.append(f"search={first_name}")
        if last_name:
            # BDL search parameter handles both first and last name
            params.append(f"search={last_name}")

        if len(params) == 1:
            logger.debug("[BDLClient] get_players called with no filters")
            return []

        path = f"/players?{'&'.join(params)}"
        result = self._bdl._get(path)
        return result.get("data", [])
