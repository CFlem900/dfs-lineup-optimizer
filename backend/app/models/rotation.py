from pydantic import BaseModel, field_validator
from typing import List, Dict, Optional


class RedistributionConfig(BaseModel):
    primary_backup_share: float = 0.60
    rotation_share: float = 0.35
    star_boost: float = 0.05
    max_minutes_cap: float = 42.0
    min_minutes_floor: float = 8.0


class TeamRotation(BaseModel):
    team_id: int
    team_name: str
    game_date: str
    projections: List["PlayerProjection"]
    total_minutes: float
    positions_breakdown: Dict[str, float]
    # DFS team totals (populated by DFSService)
    team_dk_total: Optional[float] = None
    team_fd_total: Optional[float] = None

    @field_validator("total_minutes")
    @classmethod
    def validate_total_minutes(cls, v):
        # Allow under-240 totals for small rotations where all players
        # are capped at 42 min and there aren't enough to reach 240.
        # Still reject over-241 (normalization should never overshoot).
        if v > 241.0:
            raise ValueError(f"Total minutes must be ≤240, got {v}")
        return v


from app.models.player import PlayerProjection  # noqa: E402

TeamRotation.model_rebuild()
