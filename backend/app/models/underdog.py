"""Pydantic models for Underdog Fantasy integration.

Covers both Pick'em (over/under on player stat lines) and Best Ball
(snake-draft tournament) contest formats.

Underdog scoring for NBA Pick'em Fantasy Points is identical to FanDuel:
    PTS × 1.0 + REB × 1.2 + AST × 1.5 + STL × 3.0 + BLK × 3.0 + TO × -1.0
"""

from pydantic import BaseModel, Field
from typing import Dict, List, Literal, Optional


# ── Pick'em Models ─────────────────────────────────────────────────────


class UnderdogPickemLine(BaseModel):
    """A single Over/Under pick'em line from Underdog Fantasy."""

    player_name: str
    team_abbreviation: str
    stat: str  # "pts", "reb", "ast", "pra", "fg3m", "stl_blk", "fantasy_points"
    line: float  # The O/U number (e.g., 25.5)
    appearance_id: str = ""  # Underdog's internal ID for this line
    player_id: str = ""  # UD player ID
    opponent: str = ""  # Opponent abbreviation
    position: str = ""  # Player position
    image_url: Optional[str] = None


class UnderdogGame(BaseModel):
    """A single game on the Underdog slate."""

    home_team: str
    away_team: str
    game_time: str = ""  # ISO or human-readable start time


class UnderdogSlate(BaseModel):
    """All pick'em lines available for a given date."""

    game_date: str
    lines: List[UnderdogPickemLine] = []
    player_count: int = 0
    games: List[UnderdogGame] = []
    fetched_at: Optional[str] = None  # ISO timestamp


class PickemComparison(BaseModel):
    """Comparison of our projection vs. Underdog line for a single stat."""

    player_name: str
    team_abbreviation: str
    stat: str
    our_projection: float
    ud_line: float
    delta: float  # our - UD  (positive = we project OVER)
    delta_pct: float  # delta / ud_line * 100
    confidence: float = Field(
        default=0.0, ge=0.0, le=1.0,
        description="Confidence score (0–1) based on delta magnitude + historical accuracy",
    )
    recommendation: Literal["OVER", "UNDER", "SKIP"] = "SKIP"
    edge_strength: Literal["strong", "moderate", "slight", "no_edge"] = "no_edge"

    # Player context (carried through for frontend display)
    opponent: str = ""
    position: str = ""
    image_url: Optional[str] = None
    appearance_id: str = ""


class PickemEntry(BaseModel):
    """A constructed pick'em entry (2–6 picks)."""

    picks: List[PickemComparison] = []
    entry_type: Literal["flex", "power_play"] = "flex"
    num_picks: int = 0
    correlation_score: float = 0.0  # How well picks correlate
    expected_hit_rate: float = 0.0
    payout_multiplier: float = 1.0


class UnderdogPickemResponse(BaseModel):
    """API response: all Underdog lines with comparison data."""

    game_date: str
    lines: List[PickemComparison] = []
    total_lines: int = 0
    strong_edges: int = 0
    moderate_edges: int = 0
    slight_edges: int = 0
    avg_abs_delta_pct: float = 0.0


# ── Payout multiplier reference ────────────────────────────────────────
# Standard Underdog payouts (approximate):
#   2 picks  → 3×     3 picks → 6×     4 picks → 10×
#   5 picks  → 20×    6 picks → 40×   (Power Play has higher multipliers)

FLEX_PAYOUT = {2: 3.0, 3: 6.0, 4: 10.0, 5: 20.0, 6: 40.0}
POWER_PLAY_PAYOUT = {2: 6.0, 3: 10.0, 4: 25.0, 5: 50.0, 6: 100.0}
