"""Service for fetching DraftKings Available Players data (FPPG + Game Logs).

Endpoint:
    GET https://www.draftkings.com/lineup/getavailableplayers?draftGroupId={id}

Returns DK-computed FPPG (fantasy points per game) and per-game
historical DK fantasy scores for each player in a DraftGroup.
FPPG is DK's own projection signal; game logs provide actual DK
scoring for backtesting.

Caching: Daily (same as draftables — FPPG is static once published).
"""

import logging
import re
import time
import unicodedata
from datetime import date
from typing import Dict, List, Optional

import httpx

from app.models.dk_available import DKAvailablePlayer, DKGameLog
from app.services.http_resilience import resilient_get, APIGroup

logger = logging.getLogger(__name__)

DK_AVAILABLE_PLAYERS_URL = (
    "https://www.draftkings.com/lineup/getavailableplayers"
)

# HTTP settings (match existing DK services)
REQUEST_TIMEOUT = 15
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


def _normalize_name(name: str) -> str:
    """Normalize a player name for matching.

    Same logic as dk_draftables_service for consistency.
    """
    n = name.strip().lower()
    n = unicodedata.normalize("NFKD", n)
    n = "".join(c for c in n if not unicodedata.combining(c))
    n = re.sub(r"\b(jr\.?|sr\.?|iii|iv|ii)\s*$", "", n).strip()
    n = n.replace(".", "")
    n = re.sub(r"\s+", " ", n).strip()
    return n


class DKAvailablePlayersService:
    """Fetches and caches DK-computed FPPG and game logs for a DraftGroup."""

    def __init__(self):
        # Cache keyed by draft_group_id
        self._cache: Dict[int, Dict[int, DKAvailablePlayer]] = {}
        self._cache_date: Optional[str] = None

    def _is_cache_valid(self, draft_group_id: int) -> bool:
        today = date.today().isoformat()
        if self._cache_date != today:
            self._cache.clear()
            self._cache_date = today
            return False
        return draft_group_id in self._cache

    def get_available_players(
        self, draft_group_id: int
    ) -> Dict[int, DKAvailablePlayer]:
        """Get DK-computed FPPG and game logs for all players in a DraftGroup.

        Returns a dict keyed by DK player ID.
        Cached for the entire day since FPPG doesn't change once published.
        """
        if self._is_cache_valid(draft_group_id):
            return self._cache[draft_group_id]

        try:
            players = self._fetch_available_players(draft_group_id)
            self._cache[draft_group_id] = players
            self._cache_date = date.today().isoformat()
            logger.info(
                f"[AvailPlayers] Fetched {len(players)} players "
                f"for DG {draft_group_id}"
            )
            return players
        except Exception as e:
            logger.error(
                f"[AvailPlayers] Fetch failed for DG {draft_group_id}: {e}"
            )
            return self._cache.get(draft_group_id, {})

    def build_fppg_lookup(
        self, draft_group_id: int
    ) -> Dict[str, float]:
        """Build a name:team -> dk_fppg lookup for matching.

        Keys are ``"normalized_name:TEAM"`` strings.
        """
        players = self.get_available_players(draft_group_id)
        lookup: Dict[str, float] = {}

        for player in players.values():
            if player.dk_fppg <= 0:
                continue
            team = player.team_abbreviation.upper()
            normalized = _normalize_name(player.display_name)
            key = f"{normalized}:{team}"
            lookup[key] = player.dk_fppg

        return lookup

    def build_game_logs_lookup(
        self, draft_group_id: int
    ) -> Dict[str, List[DKGameLog]]:
        """Build a name:team -> game_logs lookup for backtesting.

        Keys are ``"normalized_name:TEAM"`` strings.
        """
        players = self.get_available_players(draft_group_id)
        lookup: Dict[str, List[DKGameLog]] = {}

        for player in players.values():
            if not player.game_logs:
                continue
            team = player.team_abbreviation.upper()
            normalized = _normalize_name(player.display_name)
            key = f"{normalized}:{team}"
            lookup[key] = player.game_logs

        return lookup

    def _fetch_available_players(
        self, draft_group_id: int
    ) -> Dict[int, DKAvailablePlayer]:
        """Fetch available players from DK API."""
        resp = resilient_get(
            f"{DK_AVAILABLE_PLAYERS_URL}?draftGroupId={draft_group_id}",
            group=APIGroup.DRAFTKINGS,
            headers={"User-Agent": USER_AGENT},
        )
        data = resp.json()

        # The response structure can vary; try common patterns
        players_list = data.get("playerList", [])
        if not players_list:
            players_list = data.get("players", [])
        if not players_list:
            # Sometimes the response is a flat list
            if isinstance(data, list):
                players_list = data

        result: Dict[int, DKAvailablePlayer] = {}

        for p in players_list:
            try:
                player = self._parse_player(p)
                if player:
                    result[player.dk_player_id] = player
            except Exception as e:
                logger.debug(f"[AvailPlayers] Failed to parse player: {e}")
                continue

        return result

    def _parse_player(self, raw: dict) -> Optional[DKAvailablePlayer]:
        """Parse a single player from the available players response."""
        # Player ID
        player_id = (
            raw.get("pid")
            or raw.get("playerId")
            or raw.get("draftableId")
            or 0
        )
        if not player_id:
            return None

        # Display name
        display_name = (
            raw.get("fn", "") + " " + raw.get("ln", "")
        ).strip()
        if not display_name or display_name == " ":
            display_name = raw.get("displayName") or raw.get("name") or ""
        if not display_name:
            return None

        # Team
        team = (
            raw.get("teamAbbreviation")
            or raw.get("team")
            or raw.get("t")
            or ""
        ).upper()

        # Position
        position = raw.get("position") or raw.get("pos") or raw.get("pn") or ""

        # FPPG — DK's own fantasy points per game calculation
        fppg = 0.0
        for key in ("ppg", "fppg", "dkFppg", "fantasyPointsPerGame"):
            val = raw.get(key)
            if val is not None:
                try:
                    fppg = float(val)
                    break
                except (ValueError, TypeError):
                    continue

        # Salary
        salary = raw.get("salary") or raw.get("s") or 0

        # Game logs
        game_logs = self._parse_game_logs(raw)

        return DKAvailablePlayer(
            dk_player_id=int(player_id),
            display_name=display_name,
            team_abbreviation=team,
            position=str(position),
            dk_fppg=fppg,
            salary=int(salary) if salary else 0,
            game_logs=game_logs,
        )

    def _parse_game_logs(self, raw: dict) -> List[DKGameLog]:
        """Parse game log entries from player data."""
        logs = []
        game_log_data = raw.get("gameLog") or raw.get("gameLogs") or []

        if not isinstance(game_log_data, list):
            return logs

        for entry in game_log_data:
            try:
                fpts = entry.get("fpts") or entry.get("fantasyPoints") or 0.0
                game_date = entry.get("date") or entry.get("gameDate") or ""
                opponent = entry.get("opp") or entry.get("opponent") or ""
                salary = entry.get("salary") or entry.get("sal")

                logs.append(DKGameLog(
                    game_date=str(game_date),
                    opponent=str(opponent).upper(),
                    dk_fpts=float(fpts),
                    salary=int(salary) if salary else None,
                ))
            except Exception:
                continue

        return logs
