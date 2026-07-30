"""Sport-config base model.

Defines the canonical schema for per-sport configuration. Each supported
sport (NBA, CBB, and later NFL/MLB) ships a ``SportConfig`` instance with
its own roster shape, salary cap, slot eligibility map, and DK lobby URL.

This is intentionally minimal — the goal is a single source of truth for
sport-specific values that today are duplicated as hardcoded ``if sport
== 'cbb'`` branches across the codebase. Additional fields (FD slots,
scoring formulas, classic game-type IDs, etc.) will be added in later
refactor phases as they're consumed by the registry.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field


class SportConfig(BaseModel):
    """Per-sport configuration record.

    Instances are immutable — treat as a read-only fact table indexed by
    ``code``. Mutate by editing the per-sport module (e.g. ``nba.py``)
    rather than the live instance.
    """

    code: str = Field(
        ...,
        description=(
            "Lowercase short code used as the sport key throughout the API "
            "and DB (e.g. 'nba', 'cbb', 'nfl', 'mlb')."
        ),
    )
    display_name: str = Field(
        ...,
        description="Human-readable name shown in the UI (e.g. 'NBA', 'NCAA').",
    )
    dk_lobby_url: str = Field(
        ...,
        description="DraftKings lobby URL that returns the sport's contests.",
    )
    dk_roster_slots: List[str] = Field(
        ...,
        description=(
            "Ordered list of DK roster slot labels for a Classic lineup. "
            "Length equals roster size."
        ),
    )
    dk_slot_eligibility: Dict[str, List[str]] = Field(
        ...,
        description=(
            "Map of slot label -> list of player positions that may fill that "
            "slot. Used by the lineup ILP solver to constrain assignments."
        ),
    )
    salary_cap_dk: int = Field(
        ...,
        gt=0,
        description="DK Classic salary cap for this sport.",
    )
    # ── Added in Prompt 0.2 to absorb value branches in the optimizer ──
    dk_slot_order: List[str] = Field(
        default_factory=list,
        description=(
            "Slot processing order (most-constrained → least-constrained). "
            "Used by the slot-fill phase of greedy lineup construction. "
            "Length MUST equal len(dk_roster_slots) but the order can differ "
            "(e.g. NBA puts C first because it has the fewest eligible "
            "players)."
        ),
    )
    dk_classic_game_type_ids: Tuple[int, ...] = Field(
        default_factory=tuple,
        description=(
            "Tuple of legal DraftKings ``gameTypeId`` values for this sport's "
            "Classic contest format. Used to validate that a given DraftGroup "
            "is actually Classic. NBA = (70,); CBB = (70, 98) because DK has "
            "labelled both IDs as Classic at various points."
        ),
    )
    max_player_minutes: float = Field(
        default=53.0,
        gt=0,
        description=(
            "Hard ceiling on a player's projected minutes per game. NBA = 53 "
            "(rare overtime games); CBB = 45 (40-min games + small OT cap)."
        ),
    )
    starter_min_minutes: float = Field(
        default=28.0,
        ge=0,
        description=(
            "Threshold below which a player is no longer treated as a starter "
            "for rotation-locking purposes. Set to 0 for sports that don't "
            "track minutes (e.g. NFL uses snap counts instead)."
        ),
    )
    max_team_workers: int = Field(
        default=2,
        gt=0,
        description=(
            "Concurrency cap for parallel per-team rotation builds during "
            "pool construction. CBB sets this to 1 because cbbpy is not "
            "thread-safe; NBA defaults to 2."
        ),
    )
    # ── Same-team stacking cap (added in Prompt 2.2 for MLB) ──────────
    # Hard ceiling on how many players from a single team can appear in
    # one lineup. NBA/CBB enforce 3 (small rosters, 5-on-5 game). MLB's
    # DK rule is 5 hitters from any one team — pitchers don't count
    # because there are only 2 P slots league-wide. NFL skips the cap
    # (most stack-heavy sport, QB+5 receivers is legal).
    max_same_team_count: int = Field(
        default=3,
        gt=0,
        description=(
            "Max players from any single team that can appear together in a "
            "lineup. Enforced as a hard ILP constraint."
        ),
    )
    team_stack_cap_class: Optional[str] = Field(
        default=None,
        description=(
            "When set (e.g. 'hitter' for MLB), only players whose "
            "``scoring_class`` equals this value count toward the team "
            "stack cap. None = every player counts (default for NBA/CBB)."
        ),
    )
    # Small-slate guard rail: when the slate has very few teams, the salary
    # cap is unreachable with the available player pool; relax it so the ILP
    # remains feasible.
    small_slate_team_threshold: int = Field(
        default=0,
        ge=0,
        description=(
            "Trigger the small-slate salary-floor relaxation when the pool "
            "spans this many teams or fewer. Set 0 to disable (NBA)."
        ),
    )
    small_slate_min_salary_floor_pct: float = Field(
        default=0.60,
        gt=0.0,
        le=1.0,
        description=(
            "Salary-floor cap used when the small-slate guard rail fires. "
            "Has no effect when ``small_slate_team_threshold == 0``."
        ),
    )
    # ── Scoring (added in Prompt 1.1 alongside NFL) ────────────────────
    dk_scoring: Dict[str, float] = Field(
        default_factory=dict,
        description=(
            "Per-stat scoring coefficients for DraftKings Classic. Keys are "
            "lower-snake-case stat names (e.g. 'pass_yd', 'rec_td'); values "
            "are FP-per-unit. Empty dict means scoring still lives in the "
            "legacy ``dfs_service`` formulas (NBA / CBB) and will be "
            "migrated in a later prompt. NFL/MLB populate this directly."
        ),
    )
    dk_scoring_bonuses: List[Dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "Threshold bonuses that fire when a stat exceeds a value. Each "
            "entry is a dict like ``{'name': 'pass_300y', 'stat': 'pass_yd', "
            "'threshold': 300, 'bonus': 3}``. Empty list = no bonuses (NBA "
            "uses dd/td bonuses but those live in dk_scoring as ``p_dd`` "
            "and ``p_td`` Monte-Carlo probability multipliers — see "
            "dfs_service._dk_score for the legacy form)."
        ),
    )
    # ── Polymorphic scoring (added in Prompt 2.1 for MLB) ──────────────
    # MLB hitters and pitchers score on completely different stat lines,
    # and DK rules state pitcher-position players are scored ONLY on
    # pitcher stats (Shohei Ohtani's batting line is ignored when he's
    # listed at the P slot). This requires a class-of-player dispatch
    # that the flat ``dk_scoring`` field cannot express. NFL/NBA/CBB
    # leave ``scoring_map`` and ``pos_to_class`` empty — those sports
    # use the flat coefficient model, with ``_calculate_score`` falling
    # back to ``dk_scoring`` (NFL) or the legacy formulas (NBA/CBB).
    scoring_map: Dict[str, Dict[str, float]] = Field(
        default_factory=dict,
        description=(
            "Polymorphic scoring tables keyed by player class. Example: "
            "``{'hitter': {'s': 3, 'hr': 10, ...}, 'pitcher': {'ip': 2.25, "
            "'k': 2, ...}}``. When non-empty, ``_calculate_score`` looks "
            "up the player's position in ``pos_to_class`` and applies the "
            "matching sub-dict — pitcher stats don't leak into hitter "
            "scoring and vice versa. Empty dict falls back to "
            "``dk_scoring``."
        ),
    )
    pos_to_class: Dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Map from player-position string (as it appears in DK draftables) "
            "to the player class key in ``scoring_map``. Example for MLB: "
            "``{'P': 'pitcher', 'SP': 'pitcher', '1B': 'hitter', 'OF': "
            "'hitter', ...}``. Required when ``scoring_map`` is populated."
        ),
    )
    # ── Sport-specific stacking rules (added in Prompt 1.6 for NFL) ────
    # Free-form per-sport dict consumed by ``_ilp_optimize`` when
    # ``enable_stacking`` is True. Empty default = no stacking behavior
    # (NBA / CBB / MLB use the legacy game-stack parameters instead).
    #
    # NFL keys (see app/sports/nfl.py):
    #   qb_min_pass_catchers : int — when a QB is selected, lineup must
    #       include AT LEAST this many WR/TE from his team.
    #   qb_max_pass_catchers : int — soft ceiling on same-team pass-
    #       catchers paired with the QB; prevents 4-WR/TE stacks that
    #       eat too much of the salary budget.
    #   require_bring_back   : bool — when True, the lineup must include
    #       at least one offensive player (WR/RB/TE) from the QB's
    #       opponent in the same game (the run-it-back).
    stack_rules: Dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Per-sport stacking rules. Schema is sport-specific — see "
            "consumer in lineup_optimizer_service._ilp_optimize."
        ),
    )
    is_active: bool = Field(
        default=True,
        description=(
            "When False, the registry will reject lookups — useful for "
            "scaffolding a sport that's still being built out."
        ),
    )

    model_config = {"frozen": True}
