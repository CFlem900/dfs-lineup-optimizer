"""Typed Pydantic response models for API endpoints.

These models match the existing JSON response shapes for backward
compatibility while enabling FastAPI ``response_model`` declarations
and OpenAPI documentation generation.
"""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from app.models.lineup import ExcludedPlayerEntry, PlayerPoolEntry
from app.models.pagination import PaginationMeta


# ──────────────────────────────────────────────────────────────────
# Player Pool
# ──────────────────────────────────────────────────────────────────

class PlayerPoolResponse(BaseModel):
    """Response from ``GET /player-pool``."""

    players: List[PlayerPoolEntry]
    count: int
    excluded_players: List[ExcludedPlayerEntry] = Field(default_factory=list)
    excluded_count: int = 0


# ──────────────────────────────────────────────────────────────────
# Teams
# ──────────────────────────────────────────────────────────────────

class TeamsResponse(BaseModel):
    """Response from ``GET /teams``."""

    teams: List[Dict[str, Any]]
    sport: str


class ConferencesResponse(BaseModel):
    """Response from ``GET /teams/conferences``."""

    conferences: Dict[str, List[Dict[str, Any]]]
    sport: str


# ──────────────────────────────────────────────────────────────────
# Accuracy — Paginated record list
# ──────────────────────────────────────────────────────────────────

class AccuracyRecord(BaseModel):
    """A single projected-vs-actual accuracy row."""

    player_id: int
    player_name: str
    team_id: int
    game_date: Optional[str] = None
    projected_minutes: Optional[float] = None
    actual_minutes: Optional[float] = None
    baseline_minutes: Optional[float] = None
    error: float
    position: Optional[str] = None
    was_injured: bool = False


class AccuracyPaginatedResponse(BaseModel):
    """Paginated accuracy records with aggregate metrics."""

    total_records: int
    mean_absolute_error: float
    root_mean_squared_error: float
    records: List[AccuracyRecord]
    pagination: PaginationMeta


# ──────────────────────────────────────────────────────────────────
# Accuracy — Summary / breakdown endpoints
# ──────────────────────────────────────────────────────────────────

class AccuracyMetrics(BaseModel):
    """MAE / RMSE / bias for a metric group."""

    mae: float
    rmse: float
    bias: float
    samples: Optional[int] = None


class AccuracySummaryResponse(BaseModel):
    """Response from ``GET /accuracy/summary``."""

    records: int
    period_days: int
    minutes: AccuracyMetrics
    fantasy_points: AccuracyMetrics
    per_stat: Dict[str, AccuracyMetrics]


class PositionAccuracy(BaseModel):
    """Accuracy metrics for a single position."""

    minutes_mae: float
    minutes_bias: float
    fp_mae: float
    fp_bias: float
    samples: int


class AccuracyByPositionResponse(BaseModel):
    """Response from ``GET /accuracy/by-position``."""

    period_days: int
    positions: Dict[str, PositionAccuracy]


class SalaryTierAccuracy(BaseModel):
    """Accuracy metrics for a salary tier."""

    minutes_mae: float
    minutes_bias: float
    fp_mae: float
    fp_bias: float
    samples: int


class AccuracyBySalaryTierResponse(BaseModel):
    """Response from ``GET /accuracy/by-salary-tier``."""

    period_days: int
    salary_tiers: Dict[str, SalaryTierAccuracy]


class AccuracyTimelineEntry(BaseModel):
    """A single day's accuracy metrics."""

    date: str
    minutes_mae: float
    fp_mae: float
    samples: int


class AccuracyTimelineResponse(BaseModel):
    """Response from ``GET /accuracy/timeline``."""

    period_days: int
    timeline: List[AccuracyTimelineEntry]


class PlayerAccuracyGame(BaseModel):
    """A single game's projection vs actual for a player."""

    game_date: Optional[str] = None
    projected_minutes: Optional[float] = None
    actual_minutes: Optional[float] = None
    projected_fp: Optional[float] = None
    actual_fp: Optional[float] = None
    position: Optional[str] = None
    dk_salary: Optional[int] = None


class PlayerAccuracyResponse(BaseModel):
    """Response from ``GET /accuracy/player/{player_id}``."""

    player_id: int
    player_name: Optional[str] = None
    records: int
    minutes_mae: Optional[float] = None
    minutes_bias: Optional[float] = None
    fp_mae: Optional[float] = None
    fp_bias: Optional[float] = None
    games: List[PlayerAccuracyGame] = Field(default_factory=list)


# ──────────────────────────────────────────────────────────────────
# Tournament Calibration History (paginated)
# ──────────────────────────────────────────────────────────────────

class CalibrationHistoryEntry(BaseModel):
    """A single calibration history record."""

    calibration_key: str
    category: Optional[str] = None
    adjustment_value: float
    raw_adjustment: Optional[float] = None
    confidence: Optional[float] = None
    based_on_contests: Optional[int] = None
    source: Optional[str] = None
    reasoning: Optional[str] = None
    is_active: Optional[bool] = None
    created_at: Optional[str] = None


class CalibrationHistoryResponse(BaseModel):
    """Paginated calibration history."""

    history: List[CalibrationHistoryEntry]
    count: int
    pagination: PaginationMeta


# ──────────────────────────────────────────────────────────────────
# Health & Pipeline Status
# ──────────────────────────────────────────────────────────────────

class HealthResponse(BaseModel):
    """Response from ``GET /health``."""

    status: str
    cache_connected: bool
    db_connected: bool
    ai_available: bool
    nba_api_circuit_breaker: str = "unknown"
    balldontlie_available: bool = False
    balldontlie_circuit_breaker: str = "unknown"
    environment: str
    migration_current: Optional[bool] = None


class PipelineStatusResponse(BaseModel):
    """Response from ``GET /admin/pipeline/status``."""

    last_run_at: Optional[str] = None
    last_status: str = "idle"
    last_result: Optional[Dict[str, Any]] = None
    last_error: Optional[str] = None
    total_runs: int = 0
    next_scheduled_run: Optional[str] = None
