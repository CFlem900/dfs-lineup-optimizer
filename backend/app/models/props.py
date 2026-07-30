"""Pydantic models for DraftKings player prop lines.

Represents Over/Under lines from the DK Sportsbook for individual
player stats (pts, reb, ast, PRA, DD, TD), including vig-adjusted
implied probabilities.
"""

from pydantic import BaseModel
from typing import List, Optional


class PlayerPropLine(BaseModel):
    """A single O/U prop line for one stat category."""

    stat: str                        # "pts", "reb", "ast", "pra", "dd", "td"
    line: float                      # The O/U number (e.g., 24.5)
    over_odds: int                   # American odds (e.g., -110)
    under_odds: int                  # American odds (e.g., -110)
    implied_over_prob: float         # Vig-removed probability (0.0–1.0)
    implied_under_prob: float        # Vig-removed probability (0.0–1.0)


class PlayerProps(BaseModel):
    """All available prop lines for a single player."""

    player_name: str
    team_abbreviation: str
    props: List[PlayerPropLine] = []

    def get_line(self, stat: str) -> Optional[PlayerPropLine]:
        """Get the prop line for a specific stat category."""
        return next((p for p in self.props if p.stat == stat), None)

    def has_stat(self, stat: str) -> bool:
        """Check if a prop line exists for the given stat."""
        return any(p.stat == stat for p in self.props)


class PropsComparison(BaseModel):
    """Comparison of our projection vs. market for a single stat."""

    player_name: str
    team_abbreviation: str
    stat: str
    our_projection: float
    market_line: float
    delta: float                     # our - market (positive = we project higher)
    delta_pct: float                 # delta / market * 100
    implied_over_prob: float
    signal: str                      # "aligned" (±5%), "bullish" (>5%), "bearish" (<-5%)


class PlayerPropsResponse(BaseModel):
    """API response containing all player props for a game date."""

    game_date: str
    player_count: int
    players: List[PlayerProps] = []


class PropsComparisonResponse(BaseModel):
    """API response comparing our projections vs. market lines."""

    game_date: str
    comparisons: List[PropsComparison] = []
    avg_abs_delta_pct: float = 0.0   # average absolute delta across all comparisons
    bullish_count: int = 0
    bearish_count: int = 0
    aligned_count: int = 0
