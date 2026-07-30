"""MLB service barrel + remaining skeletons.

The data + game services moved to dedicated modules in Prompt 2.2:

    MLBDataService → app.services.mlb_data_service
    MLBGameService → app.services.mlb_game_service

This file keeps the lighter skeletons (Injury, Props) plus a re-export
of the real services so anything that still imports from
``app.services.mlb_services`` keeps working.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

# Re-export the real services so legacy imports still resolve.
from app.services.mlb_data_service import MLBDataService  # noqa: F401
from app.services.mlb_game_service import MLBGameService  # noqa: F401

logger = logging.getLogger(__name__)


class MLBInjuryService:
    """Skeleton — no MLB injury feed wired yet."""

    def get_team_injuries(self, team_name: str) -> List[Any]:
        return []

    def get_all_injuries(self) -> List[Any]:
        return []

    def get_injury_hash(self, team_names: Optional[List[str]] = None) -> str:
        return ""


class MLBPropsService:
    """Skeleton — see NFLPropsService docstring for contract details."""

    def get_player_props(self, game_date: Optional[str] = None) -> Dict[str, Any]:
        return {}

    def compare_projections(self, **kwargs) -> List[Any]:
        return []

    def get_props_summary(self, game_date: Optional[str] = None) -> Dict[str, int]:
        return {}
