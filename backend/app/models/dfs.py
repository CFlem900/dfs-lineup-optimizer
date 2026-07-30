from pydantic import BaseModel
from typing import List


class DFSProjection(BaseModel):
    """DFS fantasy point projection for a single player."""

    player_id: int
    player_name: str
    position: str
    projected_minutes: float
    # Projected counting stats
    pts: float
    reb: float
    ast: float
    stl: float
    blk: float
    tov: float
    fg3m: float
    # DFS scores
    dk_points: float
    fd_points: float
    # Floor / ceiling range
    dk_floor: float
    dk_ceiling: float
    fd_floor: float
    fd_ceiling: float


class TeamDFSProjection(BaseModel):
    """DFS projections for an entire team rotation."""

    team_id: int
    team_name: str
    game_date: str
    projections: List[DFSProjection]
    team_dk_total: float
    team_fd_total: float
