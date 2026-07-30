"""Pydantic schemas for Monte Carlo game simulation.

Defines configuration, per-player input/output, team-level results,
and the top-level game simulation result with win probabilities,
over/under analysis, and individual player stat distributions.
"""

from pydantic import BaseModel, Field
from typing import Dict, List, Optional


class SimulationConfig(BaseModel):
    """Configuration parameters for Monte Carlo simulation."""

    num_simulations: int = Field(
        default=10_000,
        ge=100,
        le=100_000,
        description="Number of Monte Carlo iterations",
    )
    minutes_variance: float = Field(
        default=0.20,
        ge=0.0,
        le=0.50,
        description="Fractional std dev for player minutes (0.20 = ±20%)",
    )
    pace_variance: float = Field(
        default=0.04,
        ge=0.0,
        le=0.15,
        description="Fractional std dev for game pace sampling",
    )
    scoring_variance: float = Field(
        default=0.15,
        ge=0.0,
        le=0.40,
        description="Fractional std dev for per-minute scoring noise",
    )
    seed: Optional[int] = Field(
        default=None,
        description="Random seed for reproducibility (None = random)",
    )


class PlayerSimInput(BaseModel):
    """Pre-computed simulation parameters for one player.

    Built from PlayerMinutes + PlayerProjection before the
    simulation loop so the hot path touches only NumPy arrays.
    """

    player_id: int
    player_name: str
    position: str
    projected_minutes: float  # PlayerProjection.adjusted_minutes
    usage_rate: float  # PlayerMinutes.usage_rate
    # Per-minute stat rates (from PlayerMinutes)
    pts_per_min: float = 0.0
    reb_per_min: float = 0.0
    ast_per_min: float = 0.0
    stl_per_min: float = 0.0
    blk_per_min: float = 0.0
    tov_per_min: float = 0.0
    fg3m_per_min: float = 0.0
    # Garbage time discount (1.0 = normal, <1.0 = reduced quality minutes)
    garbage_time_factor: float = 1.0

    # ── Bimodal GMM parameters (bench player ceiling modeling) ────
    # When ceiling_probability > 0, the sim uses a Gaussian Mixture
    # Model instead of a single Normal for minutes sampling.
    # Component 1 (base): N(projected_minutes, base_std)  ← normal workload
    # Component 2 (ceiling): N(ceiling_minutes, ceiling_std) ← blowout/injury
    ceiling_probability: float = 0.0   # 0.0 = single Gaussian (default)
    ceiling_minutes: float = 0.0       # Mean minutes in ceiling state
    ceiling_std: float = 0.0           # Std dev of ceiling state

    # ── Foul trouble risk ─────────────────────────────────────────
    # Probability that the player hits foul trouble (4+ fouls by Q3),
    # resulting in reduced minutes.  Derived from positional prior +
    # historical variance.  0.0 = no foul risk modeled.
    foul_trouble_probability: float = 0.0  # 0.0-0.25 range
    foul_trouble_minutes_pct: float = 0.70 # % of projected minutes if fouled out


class PlayerSimResult(BaseModel):
    """Simulation output distribution for one player."""

    player_id: int
    player_name: str
    position: str
    # Mean projected stats across all simulations
    mean_minutes: float
    mean_pts: float
    mean_reb: float
    mean_ast: float
    mean_stl: float
    mean_blk: float
    mean_tov: float
    mean_fg3m: float
    mean_dk_points: float
    mean_fd_points: float
    # Standard deviations
    std_pts: float
    std_reb: float
    std_ast: float
    std_dk_points: float
    std_fd_points: float
    # Percentile bands: {"p10": ..., "p25": ..., "p50": ..., "p75": ..., "p90": ...}
    dk_percentiles: Dict[str, float]
    fd_percentiles: Dict[str, float]
    pts_percentiles: Dict[str, float]


class TeamSimResult(BaseModel):
    """Simulation output for one team."""

    team_id: int
    team_name: str
    mean_score: float
    std_score: float
    median_score: float
    score_percentiles: Dict[str, float]  # p10, p25, p50, p75, p90
    players: List[PlayerSimResult]


class GameSimResult(BaseModel):
    """Full simulation output for a game matchup."""

    game_id: str
    num_simulations: int
    home_team: TeamSimResult
    away_team: TeamSimResult
    # Win probabilities
    home_win_pct: float
    away_win_pct: float
    # Projected totals distribution
    mean_total: float
    std_total: float
    # Over/under analysis (populated when a line is provided)
    over_under_line: Optional[float] = None
    over_pct: Optional[float] = None
    under_pct: Optional[float] = None
    # Spread analysis
    projected_spread: float  # positive = home favored
    spread_std: float
