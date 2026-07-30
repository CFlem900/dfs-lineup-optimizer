"""NFL service barrel + remaining skeletons.

The data + game services moved to dedicated modules in Prompt 1.4:

    NFLDataService → app.services.nfl_data_service  (real team table + rotation hook)
    NFLGameService → app.services.nfl_game_service  (real ESPN scoreboard fetch)

This file keeps the lighter skeletons (Injury, Props) plus a re-export
of the real services so anything that still imports from
``app.services.nfl_services`` keeps working.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

# Re-export the real services so legacy imports still resolve.
from app.services.nfl_data_service import NFLDataService  # noqa: F401
from app.services.nfl_game_service import NFLGameService  # noqa: F401

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Injury service skeleton — no NFL feed wired yet
# ─────────────────────────────────────────────────────────────────────


class NFLInjuryService:
    """Skeleton — no NFL injury feed wired up yet."""

    def get_team_injuries(self, team_name: str) -> List[Any]:
        return []

    def get_all_injuries(self) -> List[Any]:
        return []

    def get_injury_hash(self, team_names: Optional[List[str]] = None) -> str:
        return ""


# ─────────────────────────────────────────────────────────────────────
# Props service skeleton
# ─────────────────────────────────────────────────────────────────────


class NFLPropsService:
    """Skeleton — no NFL props feed yet.

    The router contracts (see ``api/routers/props.py``) require:
      - ``get_player_props(game_date)`` returns a dict[player_name, props]
      - ``compare_projections(player_name, team, projected_stats, game_date)``
        returns a list of comparison entries
      - ``get_props_summary(game_date)`` returns a dict[stat_category, int]
        (player count per category — must support ``.values()`` and ``sum()``)
    All return empty containers so the props endpoints respond with
    well-formed empty payloads instead of 500-ing.
    """

    def get_player_props(self, game_date: Optional[str] = None) -> Dict[str, Any]:
        return {}

    def compare_projections(self, **kwargs) -> List[Any]:
        return []

    def get_props_summary(self, game_date: Optional[str] = None) -> Dict[str, int]:
        return {}
