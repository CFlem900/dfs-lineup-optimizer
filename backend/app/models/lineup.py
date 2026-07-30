"""Pydantic models for the DFS lineup optimizer.

Defines the request/response schemas for the ``/optimize-lineup``,
``/generate-lineups``, ``/analyze-lineups``, and ``/player-pool``
endpoints, as well as the data structures used by
:class:`LineupOptimizerService` and :class:`LineupAnalysisService`.
"""

from pydantic import AfterValidator, BaseModel, Field, computed_field, field_validator, model_validator
from typing import Annotated, Any, Dict, List, Literal, Optional

# Maximum minutes a player can play (regulation + OT buffer)
_MAX_PLAYER_MINUTES = 53.0


# ──────────────────────────────────────────────────────────────────
# Sport code validation
# ──────────────────────────────────────────────────────────────────
#
# Single source of truth for "which sports does this build support":
# the registry's ``SUPPORTED_SPORTS`` tuple. Every request/response
# model that carries a ``sport`` field uses the ``SportCode`` annotated
# alias below so adding a new sport (e.g. NHL) means updating
# ``SUPPORTED_SPORTS`` once — not chasing down a dozen
# ``Literal["nba", "cbb", "nfl", "mlb"]`` declarations.
#
# History: the original codebase scattered ``Literal["nba", "cbb"]``
# across ~10 models. When MLB and NFL were added, several were missed,
# producing 422 Unprocessable Entity errors on /api/lineups/analyze,
# /api/lineups/lateswap, etc. This central validator fixes the bug
# permanently and prevents the next sport addition from regressing.


def _validate_sport_code(value: str) -> str:
    """Reject unknown sport codes against the live registry.

    Imported lazily to avoid a circular import at module load — the
    registry's ``__init__`` materializes ``SportConfig`` instances,
    which in turn could (in the future) reference response shapes.
    """
    from app.sports import SUPPORTED_SPORTS

    if value not in SUPPORTED_SPORTS:
        raise ValueError(
            f"Unsupported sport '{value}'. Must be one of: "
            f"{sorted(SUPPORTED_SPORTS)}"
        )
    return value


# Use as ``sport: SportCode = "nba"`` on any request/response model.
# The validator runs at construction time so a bad value 422s at the
# API edge rather than crashing deeper in the optimizer.
SportCode = Annotated[str, AfterValidator(_validate_sport_code)]


# ──────────────────────────────────────────────────────────────────
# Player / Pool models
# ──────────────────────────────────────────────────────────────────

class LineupPlayer(BaseModel):
    """A single player slot in an optimized lineup."""

    player_id: int
    player_name: str
    display_name: Optional[str] = None  # DK display name for CSV matching
    position: str  # NBA position from DK draftables (PG, SG, SF, PF, C)
    roster_slot: str  # Lineup slot filled (PG, SG, SF, PF, C, G, F, UTIL)
    team_abbreviation: str
    salary: int
    projected_fp: float
    floor_fp: float
    ceiling_fp: float
    projected_minutes: float
    projected_stats: Optional[Dict[str, float]] = None
    dk_player_id: Optional[int] = None  # For DK CSV export
    game_id: Optional[str] = None       # For late-swap lock detection
    # ── Environmental adjustment (Prompt 7.2) ──────────────────────
    # Carries the optimizer-internal park × wind multiplier so the
    # lineup card can surface "this is a Coors-boosted build" without
    # the frontend having to recompute. Mirrors the same field on
    # :class:`PlayerPoolEntry`.
    adjusted_fp: Optional[float] = None

    # ── Rotation role (Prompt 7.8) ─────────────────────────────────
    # Carries the sport-aware "Starter" / "Bench" / "Out"
    # classification through to the lineup response so the lineup
    # card can render a role chip alongside per-player minutes.
    rotation_role: Optional[str] = None

    @field_validator("projected_minutes")
    @classmethod
    def cap_projected_minutes(cls, v: float) -> float:
        """Safety net: no player can play more than 53 minutes."""
        return min(max(v, 0.0), _MAX_PLAYER_MINUTES)

    @computed_field  # type: ignore[misc]
    @property
    def env_multiplier(self) -> float:
        """Park × wind multiplier as a derived field on the wire (Prompt 7.2).

        Returns ``adjusted_fp / projected_fp`` rounded to 2 decimals when
        both are available; falls back to ``1.0`` for non-MLB sports
        (where ``adjusted_fp`` is None) or any pathological zero-projection
        case. The frontend can therefore unconditionally check
        ``player.env_multiplier !== 1`` to decide whether to render the
        ±% Park/Wind badge.
        """
        if self.adjusted_fp is None or self.projected_fp <= 0:
            return 1.0
        return round(self.adjusted_fp / self.projected_fp, 2)


class PlayerPoolEntry(BaseModel):
    """A player available for lineup construction.

    Core fields are populated during pool building.  Enrichment fields
    (expert_*, sim_*, game_*, is_b2b, rotation_confidence) are set by
    ``_enrich_pool()`` and default to neutral/None when enrichment is
    unavailable.
    """

    player_id: int
    player_name: str
    display_name: Optional[str] = None  # DK display name (e.g. "LeBron James") for CSV matching
    position: str
    team_abbreviation: str
    salary: int
    projected_fp: float
    floor_fp: float
    ceiling_fp: float
    projected_minutes: float
    dk_value: Optional[float] = None
    eligible_slots: List[str]
    dk_player_id: Optional[int] = None
    projected_stats: Optional[Dict[str, float]] = None

    # ── Adjusted projection (Prompt 6.1: MLB park factors) ──────────
    # When set (currently MLB-only, populated by ``_enrich_pool``),
    # this is the projection AFTER stadium-aware adjustment — Coors
    # Field hitters land here at +34%, Petco Park hitters at -10%,
    # and pitchers in the inverse direction. The optimizer's MLB
    # objective reads this field; the UI keeps showing the raw
    # ``projected_fp`` so users see the source CSV value untouched.
    # None = "no adjustment" — non-MLB sports and MLB players whose
    # game has no resolved venue both fall through unchanged.
    adjusted_fp: Optional[float] = None

    # ── Rotation role (Prompt 7.8) ──────────────────────────────────
    # Sport-aware classification derived from projected_minutes:
    #   "Starter" — projected_minutes >= cfg.starter_min_minutes
    #   "Bench"   — 0 < projected_minutes < cfg.starter_min_minutes
    #   "Out"     — projected_minutes <= 0 (or injury_status == "Out")
    # NFL / MLB use ``starter_min_minutes = 0`` so every active player
    # lands in "Starter" (true for these sports — there's no rotation
    # tier to distinguish). The frontend uses this to render a small
    # role badge alongside the Min column for basketball sports.
    rotation_role: Optional[str] = None

    # ── Enrichment: Expert signals ────────────────────────────────
    expert_sentiment: Optional[Literal["bullish", "bearish", "neutral"]] = None
    expert_signal_count: int = 0
    expert_confidence_boost: float = 0.0  # -0.1 to +0.1

    # ── Enrichment: Simulation percentiles ────────────────────────
    sim_p10: Optional[float] = None   # 10th percentile FP
    sim_p50: Optional[float] = None   # median FP
    sim_p90: Optional[float] = None   # 90th percentile FP
    sim_std: Optional[float] = None   # FP standard deviation
    sim_optimal_pct: Optional[float] = None    # % of sims player appears in optimal lineup
    sim_leverage_ratio: Optional[float] = None  # optimal_pct / projected_ownership_pct

    # ── Enrichment: Usage & Assist profile (for cannibalization) ──
    usage_rate: Optional[float] = None          # USG% (0.0-1.0, e.g. 0.28 = 28%)
    ast_per_game: Optional[float] = None        # Assists per game (projected)
    assisted_fg_pct: Optional[float] = None     # % of FG that are assisted (bigs)

    # ── Enrichment: Game context ──────────────────────────────────
    game_pace: Optional[float] = None
    game_total: Optional[float] = None
    opponent_def_rating: Optional[float] = None
    is_b2b: bool = False
    game_id: Optional[str] = None               # NBA game ID (for stacking)
    opponent_abbreviation: Optional[str] = None  # Opponent team abbr (for bring-back)
    vegas_spread: Optional[float] = None         # Team spread (negative = favorite)
    game_commence_time: Optional[str] = None     # Tip-off in ET (e.g. "7:00 PM ET")
    implied_team_total: Optional[float] = None   # Vegas-derived per-team implied total
    boom_probability: Optional[float] = None     # ceiling_fp / projected_fp ratio (upside signal)

    # ── Enrichment: Rotation confidence ───────────────────────────
    rotation_confidence: float = 1.0

    # ── Enrichment: Injury status ─────────────────────────────────
    injury_status: Optional[str] = None  # "Out", "GTD", "Doubtful", "Questionable", or None
    injury_description: Optional[str] = None

    # ── Enrichment: Ownership projection (Agent 7) ──────────────
    estimated_ownership: Optional[float] = None  # 0.0-100.0 percentage

    # ── Enrichment: DK Sportsbook props ────────────────────────
    props_pts_line: Optional[float] = None       # DK prop O/U for points
    props_reb_line: Optional[float] = None       # DK prop O/U for rebounds
    props_ast_line: Optional[float] = None       # DK prop O/U for assists
    props_pra_line: Optional[float] = None       # DK prop O/U for PRA combo
    props_delta_pct: Optional[float] = None      # Avg divergence from market (%)
    props_signal: Optional[str] = None           # "aligned"/"bullish"/"bearish"

    # ── Enrichment: DK FPPG (Available Players) ────────────────
    dk_fppg: Optional[float] = None              # DK's own fantasy pts per game
    dk_fppg_delta: Optional[float] = None        # our_fp - dk_fppg

    # ── Enrichment: Noise profile (sim tuning) ─────────────────
    noise_archetype: Optional[str] = None        # e.g. "High-Usage Alpha", "Volatile Bench Scorer"
    std_dev_multiplier: Optional[float] = None   # σ as fraction of median projection (0.14-0.30)
    ceiling_multiplier: Optional[float] = None   # 99th percentile cap multiplier (1.3-2.0)
    floor_multiplier: Optional[float] = None     # 1st percentile floor multiplier (0.2-0.7)

    # ── Projection source (set by fallback path) ─────────────
    projection_source: Optional[str] = None      # None=rotation, "dk_fppg", "salary_estimate"

    @field_validator("projected_minutes")
    @classmethod
    def cap_projected_minutes(cls, v: float) -> float:
        """Safety net: no player can play more than 53 minutes."""
        return min(max(v, 0.0), _MAX_PLAYER_MINUTES)

    @computed_field  # type: ignore[misc]
    @property
    def env_multiplier(self) -> float:
        """Park × wind multiplier as a derived field on the wire (Prompt 7.2).

        Returns ``adjusted_fp / projected_fp`` rounded to 2 decimals when
        both are available; falls back to ``1.0`` for non-MLB sports
        (where ``adjusted_fp`` is None) or any zero-projection case.

        The frontend reads this directly to decide whether to render
        the ±% Park/Wind badge — no division needed client-side, and
        non-MLB sports get a safe ``1.0`` default that the UI can
        treat as "no badge".
        """
        if self.adjusted_fp is None or self.projected_fp <= 0:
            return 1.0
        return round(self.adjusted_fp / self.projected_fp, 2)


class ExcludedPlayerEntry(BaseModel):
    """A player excluded from the optimizer pool, shown for transparency.

    Returned alongside the main pool so the frontend can display these
    players (grayed out) with their exclusion reason.  The optimizer
    never sees these entries.
    """

    player_id: int
    player_name: str
    display_name: Optional[str] = None
    position: str
    team_abbreviation: str
    salary: int = 0
    injury_status: Optional[str] = None
    injury_description: Optional[str] = None
    exclusion_reason: str  # "injury_out", "injury_doubtful", "zero_minutes",
    #                        "zero_fp", "low_games", "name_mismatch"
    exclusion_detail: Optional[str] = None  # Human-readable detail
    projected_fp: Optional[float] = None
    projected_minutes: Optional[float] = None


# ──────────────────────────────────────────────────────────────────
# Single-lineup request / response  (backward-compatible)
# ──────────────────────────────────────────────────────────────────

class OptimizeRequest(BaseModel):
    """Request body for ``POST /api/optimize-lineup``."""

    platform: Literal["dk", "fd"]
    sport: SportCode = "nba"
    draft_group_id: int
    game_date: Optional[str] = None
    locked_players: List[int] = Field(default_factory=list)
    excluded_players: List[int] = Field(default_factory=list)
    projection_overrides: Optional[Dict[int, Dict[str, float]]] = Field(
        default=None,
        description="Per-player projection overrides from UI edits. "
        "Keys are player_id, values are dicts with any of: "
        "projected_minutes, projected_fp, floor_fp, ceiling_fp",
    )
    seed: Optional[int] = Field(
        default=None,
        description="Random seed for reproducible lineup generation. "
        "None (default) = nondeterministic.",
    )
    mode: Literal["classic", "showdown"] = Field(
        default="classic",
        description="'classic' = full slate, 'showdown' = single-game captain mode",
    )
    game_id: Optional[str] = Field(
        default=None,
        description="Required for showdown mode — the specific game to build for",
    )
    recent_weight: Optional[float] = Field(
        default=None, ge=0.0, le=0.60,
        description="Override recent-form weight in baseline minutes (0.0-0.60). "
        "Default None = use system default (0.25). Higher values weight "
        "recent games more heavily for hot-streak chasing.",
    )
    contest_type: Literal["gpp", "cash", "single_entry"] = Field(
        default="gpp",
        description="Contest type determines solver constraints: GPP adds ceiling "
        "tilt, ownership cap, and pivot rule; cash optimizes for floor.",
    )


class OptimizedLineup(BaseModel):
    """Response from the lineup optimizer."""

    platform: Literal["dk", "fd"]
    # All four sports supported via the SportConfig registry. Kept in
    # sync with MultiLineupRequest.sport (relaxed in Prompt 5.1).
    sport: SportCode = "nba"
    players: List[LineupPlayer]
    total_salary: int
    salary_remaining: int
    total_projected_fp: float
    total_floor_fp: float
    total_ceiling_fp: float
    # ── Environmental adjustment (Prompt 7.2) ─────────────────────────
    # Sum of per-player ``adjusted_fp`` across this lineup. ``None``
    # when no player has an env adjustment (NBA / NFL / CBB or an
    # MLB build with no resolved venues). When set AND different
    # from ``total_projected_fp``, the UI surfaces both numbers so
    # users understand the optimizer's apparent "lower projection"
    # picks were actually environmentally boosted.
    total_adjusted_fp: Optional[float] = None
    salary_cap: int
    roster_slots: List[str]
    warnings: List[str] = Field(default_factory=list)
    quality_score: Optional[float] = Field(
        default=None,
        description="Normalised lineup quality score (0–100).  "
        "Higher is better.  None when quality assessment is skipped.",
    )
    quality_grade: Optional[str] = Field(
        default=None,
        description="Letter grade (A+, A, B+, B, C+, C, D) derived from "
        "quality_score.  None when quality assessment is skipped.",
    )
    # Internal diagnostic — not shown in API unless explicitly requested
    ilp_used: Optional[bool] = Field(
        default=None,
        description="Whether the ILP solver produced this lineup (True) "
        "or greedy fallback was used (False/None).",
    )


# ──────────────────────────────────────────────────────────────────
# Multi-lineup request / response
# ──────────────────────────────────────────────────────────────────

class MultiLineupRequest(BaseModel):
    """Request body for ``POST /api/generate-lineups``."""

    platform: Literal["dk", "fd"]
    # All four sports supported via the SportConfig registry. Kept as a
    # Literal (not free-form str) so unknown codes 422 at the API edge
    # rather than 500 deeper in the optimizer.
    sport: SportCode = "nba"
    draft_group_id: int
    game_date: Optional[str] = None
    locked_players: List[int] = Field(default_factory=list)
    excluded_players: List[int] = Field(default_factory=list)
    num_lineups: int = Field(default=1, ge=1, le=150)
    strategy: Literal["max_projection", "balanced", "ceiling", "contrarian", "pure_max", "sim_optimal"] = "max_projection"
    max_overlap: int = Field(
        default=6, ge=3, le=7,
        description="Maximum shared players between any two lineups",
    )
    contest_type: Literal["gpp", "cash", "single_entry"] = "gpp"
    enable_stacking: bool = Field(
        default=True,
        description="Force game stacking (2-3 players from same game) "
        "and bring-back logic in GPP lineups.",
    )
    salary_floor_pct: float = Field(
        default=0.95, ge=0.90, le=1.0,
        description="Minimum salary usage as fraction of cap (0.95 = 95%).",
    )
    projection_overrides: Optional[Dict[int, Dict[str, float]]] = Field(
        default=None,
        description="Per-player projection overrides from UI edits. "
        "Keys are player_id, values are dicts with any of: "
        "projected_minutes, projected_fp, floor_fp, ceiling_fp",
    )
    seed: Optional[int] = Field(
        default=None,
        description="Random seed for reproducible lineup generation. "
        "None (default) = nondeterministic.",
    )
    mode: Literal["classic", "showdown"] = Field(
        default="classic",
        description="'classic' = full slate, 'showdown' = single-game captain mode",
    )
    game_id: Optional[str] = Field(
        default=None,
        description="Required for showdown mode — the specific game to build for",
    )
    max_exposure: Optional[float] = Field(
        default=None, ge=0.1, le=1.0,
        description="Maximum fraction of lineups any single player can appear in "
        "(0.1-1.0). None = no limit. E.g., 0.5 means a player can appear "
        "in at most 50% of generated lineups.",
    )
    player_max_exposure: Dict[int, float] = Field(
        default_factory=dict,
        description="Per-player maximum exposure overrides. Maps player_id → "
        "max fraction (0.0-1.0). Overrides the global max_exposure for "
        "specific players. E.g. {12345: 0.3} limits player 12345 to "
        "at most 30% of lineups.",
    )
    player_min_exposure: Dict[int, float] = Field(
        default_factory=dict,
        description="Per-player minimum exposure targets. Maps player_id → "
        "min fraction (0.0-1.0). The optimizer will attempt to include "
        "these players in at least the specified fraction of lineups. "
        "E.g. {12345: 0.2} targets player 12345 in ≥20% of lineups.",
    )
    recent_weight: Optional[float] = Field(
        default=None, ge=0.0, le=0.60,
        description="Override recent-form weight in baseline minutes (0.0-0.60). "
        "Default None = use system default (0.25). Higher values weight "
        "recent games more heavily for hot-streak chasing.",
    )
    contest_id: Optional[str] = Field(
        default=None,
        description="DraftKings contest ID. When provided, auto-detects contest "
        "type, field size, and prize structure to determine optimal solver path "
        "and scoring weights. Overrides manual strategy/contest_type selection.",
    )
    optimality_threshold: float = Field(
        default=0.90,
        ge=0.75, le=1.0,
        description="Min fraction of baseline optimal projection score each lineup "
        "must meet (e.g. 0.90 = 90%). Default 0.90.",
    )
    minimum_relaxation_floor: float = Field(
        default=0.75,
        ge=0.50, le=0.90,
        description="Hard floor for dynamic relaxation. If the optimality threshold "
        "is relaxed below this value and still infeasible, lineup generation "
        "raises an error instead of continuing. Default 0.75 (75%).",
    )
    max_cumulative_ownership: Optional[float] = Field(
        default=None, ge=0.0,
        description="Maximum cumulative ownership percentage across all roster "
        "slots. E.g. 150.0 means the sum of all 8 players' projected "
        "ownership cannot exceed 150%. None = no cumulative cap (the "
        "per-player GPP caps from C9b still apply). Auto-relaxes by "
        "10% per retry if infeasible.",
    )
    is_late_swap: bool = Field(
        default=False,
        description="True when generating lineups for a live slate in "
        "late-swap mode. The optimizer will exclude players in "
        "games that have already tipped off (locked).",
    )

    # ── Dynamic stacking overrides (Prompt 5.1) ────────────────────────
    # Optional per-request overrides for the sport's default ``stack_rules``.
    # When ``None`` the optimizer falls back to the SportConfig defaults
    # (NFL: qb_min=1 / qb_max=2 / bring_back=True; MLB: 5-3 + pitcher fade).
    # Sport-specific mapping:
    #   NFL  → primary_stack_size  overrides ``qb_min_pass_catchers``
    #         require_bring_back   overrides ``require_bring_back``
    #         secondary_stack_size is unused (no NFL secondary concept)
    #   MLB  → primary_stack_size  overrides ``primary_stack_size``
    #         secondary_stack_size overrides ``secondary_stack_size``
    #         require_bring_back   ignored (no MLB bring-back rule;
    #         pitcher fade is always-on when stack_rules is populated)
    #   NBA / CBB ignore all three (legacy game-stack params drive them).
    primary_stack_size: Optional[int] = Field(
        default=None, ge=0, le=8,
        description=(
            "User-defined size of the primary stack. NFL: minimum WR/TE "
            "to pair with the QB. MLB: minimum same-team hitters in the "
            "primary stack. None = use sport default."
        ),
    )
    secondary_stack_size: Optional[int] = Field(
        default=None, ge=0, le=8,
        description=(
            "User-defined size of the secondary stack. MLB only — sets "
            "the soft-bonus target for the second-team hitter cluster. "
            "Ignored for NFL/NBA/CBB. None = use sport default."
        ),
    )
    require_bring_back: Optional[bool] = Field(
        default=None,
        description=(
            "User-defined bring-back toggle. NFL only — when True, the "
            "lineup must include a WR/RB/TE from the QB's opponent. "
            "Ignored for MLB/NBA/CBB. None = use sport default."
        ),
    )

    @model_validator(mode="after")
    def _validate_stack_overrides(self) -> "MultiLineupRequest":
        """Sport-aware sanity checks for the dynamic stacking overrides.

        MLB has exactly 8 hitter slots in the Classic roster (10 total -
        2 P), so a primary + secondary that exceeds 8 is unsatisfiable.
        We surface this as a 422 at API parse time rather than a vague
        ILP-infeasibility error 30 seconds into a generation run.
        """
        if self.sport == "mlb":
            primary = self.primary_stack_size or 0
            secondary = self.secondary_stack_size or 0
            if primary + secondary > 8:
                raise ValueError(
                    f"MLB has 8 hitter slots; primary_stack_size "
                    f"({primary}) + secondary_stack_size ({secondary}) "
                    f"= {primary + secondary} exceeds the cap. Use a "
                    f"combination that sums to 8 or less."
                )
        return self


class MultiLineupResponse(BaseModel):
    """Response from the multi-lineup generator."""

    platform: Literal["dk", "fd"]
    sport: SportCode = "nba"
    lineups: List[OptimizedLineup]
    strategy: str
    num_requested: int
    num_generated: int
    pool_size: int
    generation_time_ms: int
    warnings: List[str] = Field(default_factory=list)
    # Overgeneration metadata — total candidates produced before filtering
    num_candidates_generated: Optional[int] = None
    # Optimality floor metadata
    baseline_projection_score: Optional[float] = None
    baseline_optimal_lineup: Optional[OptimizedLineup] = Field(
        default=None,
        description=(
            "The actual unconstrained-optimum lineup used to compute "
            "baseline_projection_score. Lets the UI show users which players "
            "would form the theoretical max, not just the score."
        ),
    )
    min_projection_floor: Optional[float] = None
    # ILP diagnostic metadata
    ilp_accepted_count: Optional[int] = None
    ilp_failed_count: Optional[int] = None
    greedy_fallback_count: Optional[int] = None


# ──────────────────────────────────────────────────────────────────
# Lineup Analysis models
# ──────────────────────────────────────────────────────────────────

class PlayerSwapSuggestion(BaseModel):
    """A specific swap recommendation."""

    slot: str
    current_player: str
    current_player_id: int
    suggested_player: str
    suggested_player_id: int
    reason: str
    projected_fp_delta: float
    confidence: Literal["high", "medium", "low"]


class LineupRisk(BaseModel):
    """A risk flag for a lineup."""

    severity: Literal["high", "medium", "low"]
    category: str  # injury, b2b, low_minutes, correlation, salary
    description: str
    affected_players: List[str] = Field(default_factory=list)


class LineupDimensionScore(BaseModel):
    """Quality score on a single dimension."""

    dimension: str   # projection, floor, ceiling, value, diversity, stacking, expert_consensus
    score: float     # 0.0 - 10.0
    label: str       # Excellent, Good, Fair, Poor
    detail: str


class LineupAnalysisResult(BaseModel):
    """Full analysis of a single lineup."""

    lineup_index: int
    overall_grade: str               # A+, A, B+, B, C+, C, D
    overall_score: float             # 0 - 100
    dimension_scores: List[LineupDimensionScore]
    risks: List[LineupRisk]
    swap_suggestions: List[PlayerSwapSuggestion]
    team_stacking: Dict[str, int]    # team_abbr -> count
    salary_efficiency: float         # percentage of cap used
    correlation_notes: List[str]


class AnalyzeLineupsRequest(BaseModel):
    """Request body for ``POST /api/analyze-lineups``."""

    platform: Literal["dk", "fd"]
    sport: SportCode = "nba"
    draft_group_id: int
    game_date: Optional[str] = None
    lineups: List[OptimizedLineup]


class AnalyzeLineupsResponse(BaseModel):
    """Response from the lineup analysis agent."""

    analyses: List[LineupAnalysisResult]
    portfolio_summary: Dict[str, Any]
    generation_time_ms: int


# ──────────────────────────────────────────────────────────────────
# Lineup Refinement models
# ──────────────────────────────────────────────────────────────────

class RefineLineupsRequest(BaseModel):
    """Request body for ``POST /api/refine-lineups``.

    Takes existing lineups + their analysis and iteratively applies
    the best swap suggestions to improve overall grades.
    """

    platform: Literal["dk", "fd"]
    sport: SportCode = "nba"
    draft_group_id: int
    game_date: Optional[str] = None
    lineups: List[OptimizedLineup]
    max_iterations: int = Field(
        default=3, ge=1, le=10,
        description="Max refinement passes per lineup",
    )
    target_grade: Optional[str] = Field(
        default=None,
        description="Stop refining once lineup reaches this grade (e.g. 'A')",
    )


class RefinedLineupResult(BaseModel):
    """A single lineup's refinement outcome."""

    lineup_index: int
    original_grade: str
    original_score: float
    refined_grade: str
    refined_score: float
    swaps_applied: List[PlayerSwapSuggestion]
    lineup: OptimizedLineup


class RefineLineupsResponse(BaseModel):
    """Response from the lineup refinement endpoint."""

    platform: Literal["dk", "fd"]
    lineups: List[OptimizedLineup]
    refinement_results: List[RefinedLineupResult]
    total_swaps_applied: int
    avg_score_improvement: float
    generation_time_ms: int
    warnings: List[str] = Field(default_factory=list)


# ──────────────────────────────────────────────────────────────────
# Late Swap models
# ──────────────────────────────────────────────────────────────────

class LateSwapSuggestion(BaseModel):
    """A single late swap recommendation."""

    slot: str
    out_player: Dict[str, Any]
    in_player: Dict[str, Any]
    reason: str
    fp_delta: float


class GameSlotStatus(BaseModel):
    """Real-time game status for a team on the slate."""

    team_abbreviation: str
    game_status: Literal["not_started", "in_progress", "final", "unknown"]
    game_status_detail: str = ""
    is_locked: bool
    opponent: Optional[str] = None
    home_team_score: Optional[int] = None
    visitor_team_score: Optional[int] = None


class LateSwapSlotInfo(BaseModel):
    """Per-slot lock/open classification in the late-swap response."""

    roster_slot: str
    player_name: str
    player_id: int
    team_abbreviation: str
    is_locked: bool
    lock_reason: Optional[str] = None
    game_status: Optional[GameSlotStatus] = None


class LateSwapRequest(BaseModel):
    """Request body for ``POST /api/late-swap``."""

    platform: Literal["dk", "fd"]
    sport: SportCode = "nba"
    draft_group_id: int
    game_date: Optional[str] = None
    lineups: List[OptimizedLineup]
    auto_apply: bool = Field(
        default=False,
        description="If true, automatically apply all swaps and return "
        "updated lineups. If false, return suggestions only.",
    )
    use_ilp: bool = Field(
        default=True,
        description="If true, re-run ILP solver for open slots. "
        "If false, use greedy 1-for-1 replacement (legacy behavior).",
    )


class LateSwapResponse(BaseModel):
    """Response from the late swap endpoint."""

    lineups: List[OptimizedLineup]
    swaps: List[List[LateSwapSuggestion]]
    total_swaps: int
    warnings: List[str] = Field(default_factory=list)
    slot_statuses: Optional[List[List[LateSwapSlotInfo]]] = None
    locked_salary: Optional[List[int]] = None
    open_slots_count: Optional[List[int]] = None
    game_statuses: Optional[Dict[str, GameSlotStatus]] = None
    bdl_available: bool = True


class LateSwapMonitorRequest(BaseModel):
    """Request body for ``POST /api/late-swap/monitor/full``."""

    platform: Literal["dk", "fd"]
    sport: SportCode = "nba"
    draft_group_id: int
    game_date: Optional[str] = None
    lineups: List[OptimizedLineup]


# ──────────────────────────────────────────────────────────────────
# Tournament Import / Calibration models
# ──────────────────────────────────────────────────────────────────

class TournamentImportResponse(BaseModel):
    """Response from the tournament CSV import endpoint."""

    contest_id: int
    entries_imported: int
    top_1pct_count: int
    message: str = ""


class TournamentCalibrationEntry(BaseModel):
    """A single active calibration adjustment."""

    calibration_key: str
    category: str
    adjustment_value: float
    raw_adjustment: float
    confidence: float
    based_on_contests: int
    source: str
    reasoning: Optional[str] = None


class TournamentCalibrationsResponse(BaseModel):
    """Active calibrations from tournament and backtest analysis."""

    calibrations: List[TournamentCalibrationEntry] = Field(default_factory=list)
    total_count: int = 0
    last_analysis_date: Optional[str] = None


# ──────────────────────────────────────────────────────────────────
# Simulate-and-Filter request / response
# ──────────────────────────────────────────────────────────────────

class SimFilterRequest(BaseModel):
    """Request body for ``POST /api/sim-filter-lineups``."""

    platform: Literal["dk", "fd"]
    sport: SportCode = "nba"
    draft_group_id: int
    game_date: Optional[str] = None
    locked_players: List[int] = Field(default_factory=list)
    excluded_players: List[int] = Field(default_factory=list)
    num_simulations: int = Field(
        default=1000, ge=100, le=5000,
        description="Monte Carlo iterations to run per game.",
    )
    num_lineups: int = Field(
        default=20, ge=1, le=150,
        description="Number of top-frequency lineups to return.",
    )
    solver_mode: Literal["greedy", "ilp"] = Field(
        default="greedy",
        description="'greedy' (~1ms/iter, fast) or 'ilp' (~100ms/iter, optimal). "
        "Greedy is recommended for num_simulations > 200.",
    )
    contest_type: Literal["gpp", "cash", "single_entry"] = "gpp"
    mode: Literal["classic", "showdown"] = Field(default="classic")
    game_id: Optional[str] = Field(default=None)
    projection_overrides: Optional[Dict[int, Dict[str, float]]] = None
    seed: Optional[int] = None


class SimFilterLineup(BaseModel):
    """A unique lineup with frequency metadata."""

    lineup: OptimizedLineup
    frequency: int = Field(description="Times this lineup appeared as optimal across all iterations")
    frequency_pct: float = Field(description="Frequency as percent of total iterations solved")
    avg_sim_fp: float = Field(description="Mean total FP across iterations where this lineup was optimal")
    max_sim_fp: float = Field(description="Best total FP seen for this lineup across all iterations")


class SimFilterResponse(BaseModel):
    """Response from the simulate-and-filter pipeline."""

    platform: Literal["dk", "fd"]
    sport: SportCode = "nba"
    lineups: List[SimFilterLineup]
    num_simulations: int
    num_iterations_solved: int
    num_unique_lineups: int
    num_returned: int
    solver_mode: str
    pool_size: int
    generation_time_ms: int
