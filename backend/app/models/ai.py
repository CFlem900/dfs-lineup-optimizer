"""Pydantic schemas for AI agent services.

Defines request/response contracts used by the central AIService
and all 12 specialised agents.
"""

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


# ── Core AI service models ────────────────────────────────────────

class AIRequest(BaseModel):
    """Standardised request to the central AI service."""

    system_prompt: str
    user_prompt: str
    model_tier: Literal["default", "reasoning"] = "default"
    max_tokens: int = 1024
    temperature: float = 0.3
    response_format: Optional[Literal["json", "text"]] = None
    stream: bool = False
    agent_name: Optional[str] = None


class AIResponse(BaseModel):
    """Standardised response from the central AI service."""

    content: str
    model_used: str
    provider: str
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    cached: bool = False


class AgentResult(BaseModel):
    """Generic wrapper for any agent's output with fallback info."""

    success: bool
    data: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    used_fallback: bool = False
    latency_ms: int = 0


# ── Chat agent models ─────────────────────────────────────────────

class ChatMessage(BaseModel):
    """A single message in a conversation."""

    role: Literal["user", "assistant", "system"]
    content: str
    timestamp: datetime = Field(default_factory=datetime.now)


class ChatAction(BaseModel):
    """A structured action the chat agent wants the frontend to execute."""

    type: str  # e.g. "lock_player", "exclude_player", "generate_lineups", etc.
    params: Dict[str, Any] = Field(default_factory=dict)
    label: str = ""  # Human-readable description for the UI button


class ChatRequest(BaseModel):
    """Request for the conversational agent."""

    message: str
    session_id: Optional[str] = None
    context: Optional[Dict[str, Any]] = None  # Current lineup, slate, etc.


class ChatResponse(BaseModel):
    """Response from the conversational agent."""

    message: str
    actions: List[ChatAction] = Field(default_factory=list)
    session_id: str


# ── Agent 1: Signal Analysis models ───────────────────────────────

class StatPrediction(BaseModel):
    """A specific stat prediction extracted from expert content."""

    player: str
    stat: str  # "minutes", "points", "rebounds", etc.
    value: Optional[float] = None
    direction: Optional[Literal["over", "under"]] = None
    raw_text: str = ""


class SignalAnalysis(BaseModel):
    """LLM-enhanced analysis of a single expert signal."""

    sentiment: Literal["bullish", "bearish", "neutral"]
    confidence: float = Field(ge=0.0, le=1.0)
    sarcasm_detected: bool = False
    conditional: bool = False
    condition_text: Optional[str] = None
    mentioned_players: List[str] = Field(default_factory=list)
    stat_predictions: List[StatPrediction] = Field(default_factory=list)
    impact_summary: str = ""
    signal_type: Literal[
        "rotation_change", "injury_update", "minutes_projection",
        "lineup_note", "trade_rumor", "general_take",
    ] = "general_take"


# ── Agent 3: Injury Impact models ─────────────────────────────────

class PlayerImpactAdjustment(BaseModel):
    """A single player's adjustment due to another player's injury."""

    player_id: int
    player_name: str
    minutes_delta: float = 0.0
    usage_delta: float = 0.0
    role_change: Optional[str] = None  # "bench_to_starter", "expanded_role"
    reasoning: str = ""


class InjuryImpactAnalysis(BaseModel):
    """Full cascading analysis of an injury."""

    injured_player: str
    cascading_effects: List[PlayerImpactAdjustment] = Field(default_factory=list)
    pace_impact: float = 0.0
    defensive_shift: Optional[str] = None
    confidence: float = 0.5


# ── Agent 5: News Projection models ───────────────────────────────

class NewsProjectionAdjustment(BaseModel):
    """Projection adjustment derived from a news item."""

    player_name: str
    adjustment_type: Literal[
        "minutes_cap", "minutes_boost", "role_change",
        "rest", "surprise_start", "dnp",
    ]
    minutes_override: Optional[float] = Field(default=None, le=53.0)
    usage_modifier: Optional[float] = None
    confidence: float = 0.5
    source_news_id: str = ""
    reasoning: str = ""


# ── Agent 6: Coach Pattern models ─────────────────────────────────

class CoachPatternUpdate(BaseModel):
    """AI-detected coaching pattern changes."""

    coach_name: str
    team_id: int
    starter_multiplier_adj: float = 0.0
    bench_multiplier_adj: float = 0.0
    star_multiplier_adj: float = 0.0
    rotation_size_adj: int = 0
    b2b_penalty_adj: float = 0.0
    pace_factor_adj: float = 0.0
    reasoning: str = ""
    based_on_games: int = 0
    confidence: float = 0.5


# ── Agent 7: Ownership Projection models ──────────────────────────

# Ownership data is a simple Dict[player_id, float].
# No dedicated model needed beyond what's on PlayerPoolEntry.


# ── Agent 2: Strategy Adjustment models ───────────────────────────

class StrategyAdjustments(BaseModel):
    """Game-theory-aware lineup construction guidance."""

    player_score_modifiers: Dict[int, float] = Field(
        default_factory=dict,
        description="player_id -> score multiplier (e.g. 1.1 = +10%)",
    )
    recommended_stacks: List[List[int]] = Field(
        default_factory=list,
        description="Groups of player_ids to stack together",
    )
    leverage_plays: List[int] = Field(
        default_factory=list,
        description="player_ids with low ownership + high ceiling",
    )
    avoid_players: List[int] = Field(
        default_factory=list,
        description="Chalk traps / over-owned players",
    )
    reasoning: str = ""


class LineupStrategy(BaseModel):
    """Contest-aware solver configuration from Agent 2.

    Determines which optimizer path fires and what scoring weights
    to use, based on DraftKings contest metadata (field size, prize
    structure, contest category).
    """

    # Solver path selection
    solver_path: Literal["composite", "sim_optimal"] = "composite"

    # Strategy override (maps to MultiLineupRequest.strategy)
    strategy_override: Optional[Literal[
        "max_projection", "balanced", "ceiling", "contrarian", "pure_max"
    ]] = None

    # Contest type override
    contest_type_override: Optional[Literal["gpp", "cash", "single_entry"]] = None

    # Composite score weights (should sum to ~1.0)
    w_p50: float = Field(
        default=0.50, ge=0.0, le=1.0,
        description="Weight on median projection (P50 / projected_fp)",
    )
    w_p90: float = Field(
        default=0.50, ge=0.0, le=1.0,
        description="Weight on ceiling / upside (P90 / ceiling_fp)",
    )
    w_floor: float = Field(
        default=0.0, ge=0.0, le=1.0,
        description="Weight on floor (P10 / floor_fp)",
    )

    # Ownership leverage
    ownership_leverage_alpha: float = Field(
        default=0.60, ge=0.0, le=2.0,
        description="Power-law exponent for ownership fading. "
        "0.0 = no ownership penalty (cash). Higher = stronger fade.",
    )
    ownership_leverage_baseline: float = Field(
        default=10.0, ge=1.0, le=50.0,
        description="Ownership pct where leverage multiplier = 1.0",
    )

    # Stacking controls
    enable_stacking: bool = True
    min_stack_size: int = Field(default=2, ge=0, le=4)

    # Diversity noise
    noise_floor: float = Field(
        default=0.90,
        description="Min noise multiplier for diversity injection",
    )
    noise_ceiling: float = Field(
        default=1.10,
        description="Max noise multiplier for diversity injection",
    )
    overgen_multiplier: float = Field(
        default=3.0, ge=1.0, le=10.0,
        description="Candidates to generate per requested lineup",
    )

    # Metadata
    source: Literal["rules", "ai_fallback"] = "rules"
    reasoning: str = ""
    contest_category: str = ""


# ── Contest Recommender models ───────────────────────────────────


class ContestScore(BaseModel):
    """Scoring breakdown for a single DraftKings contest."""

    contest_id: str
    contest_name: str
    contest_type: str
    entry_fee: float
    total_prizes: float
    max_entries: int
    current_entries: int
    max_entries_per_user: int

    # Derived metrics
    overlay_risk: float
    top_heavy_pct: float
    breakeven_percentile: float

    # Scoring components (each 0-100)
    overlay_score: float = Field(ge=0.0, le=100.0)
    structure_score: float = Field(ge=0.0, le=100.0)
    field_strength_score: float = Field(ge=0.0, le=100.0)
    calibration_fit_score: float = Field(ge=0.0, le=100.0)

    # Aggregate
    overall_score: float = Field(ge=0.0, le=100.0)
    recommended_entries: int = Field(ge=0)
    confidence: float = Field(ge=0.0, le=1.0)

    # Human-readable
    reasoning: List[str] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)


class ContestRecommendation(BaseModel):
    """Full slate recommendation from ContestRecommenderService."""

    slate_date: str
    platform: str
    sport: str
    draft_group_id: Optional[int] = None
    contests: List[ContestScore] = Field(default_factory=list)
    total_contests_scanned: int = 0
    total_entries_recommended: int = 0
    generation_time_ms: int = 0


# ── Agent 8: Simulation Tuning models ─────────────────────────────


class SimulationTuningProfile(BaseModel):
    """Per-player simulation noise profile from Agent 8.

    Each player is classified into an archetype role and assigned
    per-stat sigma values for the Monte Carlo noise model.
    """

    player_id: int
    player_name: str = ""
    role: Literal["star", "microwave", "three_and_d", "punt"] = "three_and_d"
    sigma_overrides: Dict[str, float] = Field(
        default_factory=dict,
        description="stat_name -> effective sigma (e.g. {'pts': 0.12, 'reb': 0.13})",
    )
    situational_flags: List[str] = Field(
        default_factory=list,
        description="Active situational modifiers: 'b2b', 'injury_return', 'blowout_risk'",
    )


# ── Agent 9: Backtesting models ───────────────────────────────────

class ProjectionBias(BaseModel):
    """A systematic projection bias found by backtesting."""

    category: str  # "position_C", "b2b", "coach_Thibs", etc.
    direction: Literal["over", "under"]
    magnitude: float  # average error magnitude
    sample_size: int = 0
    confidence: float = 0.5
    suggested_adjustment: float = 0.0


class BacktestAnalysis(BaseModel):
    """AI-analysed accuracy report."""

    period: str  # "last_7_days", "last_30_days", etc.
    overall_mae: float = 0.0
    overall_rmse: float = 0.0
    biases: List[ProjectionBias] = Field(default_factory=list)
    recommendations: List[str] = Field(default_factory=list)
    calibration_adjustments: Dict[str, float] = Field(default_factory=dict)


# ── Agent 9: GPP Post-Mortem Blueprint ───────────────────────────


class GPPBlueprintStats(BaseModel):
    """Raw observed statistics from top GPP finishers."""

    sample_size: int = 0
    avg_total_salary: float = 0.0
    salary_floor_pct: float = 0.0           # min salary used as % of cap
    avg_total_ownership: float = 0.0        # sum of player ownerships
    median_total_ownership: float = 0.0
    avg_points: float = 0.0
    median_points: float = 0.0
    pct_with_2man_stack: float = 0.0        # fraction with 2+ same-team
    pct_with_3man_stack: float = 0.0
    pct_with_bringback: float = 0.0
    avg_max_stack_size: float = 0.0
    avg_low_own_players: float = 0.0        # avg players <10% own per lineup
    avg_chalk_players: float = 0.0          # avg players >25% own per lineup
    salary_by_slot: Dict[str, float] = Field(default_factory=dict)


class GPPConstraintRecommendation(BaseModel):
    """A single recommended GPP ILP constraint override with reasoning."""

    constraint_key: str
    current_value: float
    recommended_value: float
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    reasoning: str = ""


class GPPContestBlueprint(BaseModel):
    """Full GPP post-mortem output from Agent 9.

    Captures the structural profile of top GPP finishers and maps
    observed patterns to concrete ILP constraint overrides.
    """

    contest_count: int = 0
    top_n_analyzed: int = 0
    date_range: str = ""
    observed_stats: GPPBlueprintStats = Field(default_factory=GPPBlueprintStats)
    recommended_constraints: List[GPPConstraintRecommendation] = Field(
        default_factory=list,
    )
    constraint_overrides: Dict[str, float] = Field(default_factory=dict)
    reasoning: str = ""
    recommendations: List[str] = Field(default_factory=list)


# ── Agent 10: Expert Quality models ───────────────────────────────

class ExpertQualityEvaluation(BaseModel):
    """Quality evaluation for a single expert source."""

    expert_handle: str
    accuracy_score: float = 0.0  # 0.0 - 1.0
    total_predictions: int = 0
    correct_predictions: int = 0
    specialties: Dict[str, float] = Field(
        default_factory=dict,
        description="Signal type -> accuracy (e.g. {'injury': 0.85})",
    )
    tier_recommendation: Optional[str] = None
    weight_modifier: float = 1.0  # Multiplier for this expert's signals


# ── Agent 12: Tournament Analysis models ─────────────────────────

class TournamentPattern(BaseModel):
    """A single pattern detected in winning tournament lineups."""

    category: str  # position_bias, salary_tier, stacking, ownership_leverage, game_context
    pattern_key: str  # e.g. "position_PG_boost", "stack_2man_weight"
    direction: Literal["boost", "reduce"]
    magnitude: float  # raw suggested adjustment delta (before capping)
    sample_size: int = 0
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    description: str = ""


class TournamentAnalysis(BaseModel):
    """AI-analysed tournament result patterns."""

    contest_count: int = 0
    winning_entry_count: int = 0
    patterns: List[TournamentPattern] = Field(default_factory=list)
    salary_allocation: Dict[str, float] = Field(
        default_factory=dict,
        description="Position -> avg pct of salary cap used by winners",
    )
    stacking_summary: Dict[str, float] = Field(
        default_factory=dict,
        description="Stack type -> frequency in winning lineups",
    )
    ownership_insights: List[str] = Field(default_factory=list)
    calibration_adjustments: Dict[str, float] = Field(
        default_factory=dict,
        description="calibration_key -> adjustment multiplier",
    )
    per_adjustment_reasoning: Dict[str, str] = Field(
        default_factory=dict,
        description="calibration_key -> human-readable reasoning for that adjustment",
    )
    reasoning: str = ""


# ── Line Movement Agent (Agent 13) ─────────────────────────────────

class GameLineMovement(BaseModel):
    """Line movement delta for a single game."""

    home_team: str
    away_team: str
    opening_total: Optional[float] = None
    current_total: Optional[float] = None
    total_delta: float = 0.0        # positive = total went UP
    opening_spread: Optional[float] = None
    current_spread: Optional[float] = None
    spread_delta: float = 0.0       # positive = home spread moved UP (more dog)
    is_significant: bool = False
    ceiling_adjustment: float = 0.0  # multiplier: >0 = boost, <0 = penalty
    ownership_shift: float = 0.0     # hint: positive = expect ownership increase
    reasoning: str = ""


class LineMovementAnalysis(BaseModel):
    """Full analysis of line movements across a slate."""

    game_movements: List[GameLineMovement] = Field(default_factory=list)
    significant_count: int = 0
    summary: str = ""
    recommendations: List[str] = Field(default_factory=list)


class GameContextModifier(BaseModel):
    """Per-game context modifiers derived from live Vegas odds.

    Computed by ``LineMovementAgent.compute_game_context_modifier_for_game()``
    and consumed by ``RotationEngine.apply_game_context_adjustments()``.

    When a modifier is present on ``GameInfo.context_modifier``, the rotation
    engine uses the pre-computed blowout / pace values instead of recomputing
    them from the raw spread.  This lets live odds drive minute adjustments
    while the static computation serves as a fallback when BDL data is
    unavailable.
    """

    home_team: str
    away_team: str

    # Live Vegas lines
    spread: Optional[float] = None          # Home-relative (negative = home fav)
    over_under: Optional[float] = None

    # Implied team totals
    implied_home_total: Optional[float] = None
    implied_away_total: Optional[float] = None

    # Pre-computed blowout penalty
    blowout_factor_starters: float = 1.0    # < 1.0 = minute penalty for favored starters
    blowout_factor_bench: float = 1.0       # > 1.0 = garbage-time boost for bench
    is_blowout_risk: bool = False

    # Pace modifier
    high_total_pace_boost: float = 1.0      # 1.02x when O/U > 235

    # Line movement ceiling adjustments (from analyze_line_movements)
    ceiling_adjustment: float = 0.0
    ownership_shift: float = 0.0

    # Source tracking
    source: str = "bdl_live"                # "bdl_live" or "model_projected"
