"""Pydantic models for DraftKings Available Players data.

Represents DK-computed FPPG (fantasy points per game) and per-game
historical DK fantasy scores for each draftable player.
"""

from pydantic import BaseModel
from typing import List, Optional


class DKGameLog(BaseModel):
    """A single historical DK fantasy score entry."""

    game_date: str                   # "2026-02-10" or similar
    opponent: str                    # Team abbreviation (e.g., "BOS")
    dk_fpts: float                   # Actual DK fantasy points scored
    salary: Optional[int] = None     # DK salary for that game


class DKAvailablePlayer(BaseModel):
    """DK-computed FPPG and game logs for a draftable player."""

    dk_player_id: int
    display_name: str
    team_abbreviation: str
    position: str = ""
    dk_fppg: float                   # DK's own fantasy points per game
    salary: int = 0
    game_logs: List[DKGameLog] = []  # Recent game-by-game DK scores
