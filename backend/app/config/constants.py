"""Centralized constants for the Prediction App engine.

All magic numbers previously scattered across rotation_engine.py and
dfs_service.py are collected here so they can be reviewed, tuned, and
tested in one place.
"""

# ============================================================================
# Rotation Engine — Baseline Projection
# ============================================================================

# EMA (Exponential Moving Average) smoothing factors.
# Higher alpha = more weight on recent observations.
EMA_ALPHA_5_GAME: float = 0.6   # Captures hot streaks (last 5 games)
EMA_ALPHA_10_GAME: float = 0.4  # Smoothed trend (last 10 games)

# Baseline blend weights (season vs. recent).
# 50/50 split responds faster to recent role changes (both up and down)
# while still anchoring on season trends.  The old 75/25 split caused
# ghost projections for bench players whose season_avg was stale.
BASELINE_SEASON_WEIGHT: float = 0.50
BASELINE_RECENT_WEIGHT: float = 0.50

# How the "recent" slice is split between the two EMAs.
RECENT_EMA5_SPLIT: float = 0.60   # 60% of recent weight -> EMA-5
RECENT_EMA10_SPLIT: float = 0.40  # 40% of recent weight -> EMA-10

# ── Sparse Data Heuristic ───────────────────────────────────────────
# When a player has fewer combined recent games than this threshold,
# EMA weight is shifted proportionally to season_avg to prevent
# calculate_ema([]) returning 0.0 from dragging down the baseline.
# 0 games → 100% season_avg; 1 game → 67% shifted; 2 → 33%; 3+ → normal
SPARSE_DATA_MIN_RECENT_GAMES: int = 3

# ============================================================================
# Rotation Engine — Injury Model
# ============================================================================

# Conditional probability that an injured player actually plays.
# Source: Historical NBA injury data (2018-2024 seasons).
INJURY_PLAY_PROBABILITY: dict[str, float] = {
    "Out": 0.0,
    "Doubtful": 0.20,
    "GTD": 0.72,
    "Game Time Decision": 0.72,
    "Questionable": 0.85,
    "Probable": 0.95,
}

# Expected minutes fraction *if* the player does suit up.
INJURY_MINUTES_IF_ACTIVE: dict[str, float] = {
    "Out": 0.0,
    "Doubtful": 0.75,
    "GTD": 0.75,
    "Game Time Decision": 0.75,
    "Questionable": 0.75,
    "Probable": 0.98,
}

# ============================================================================
# Rotation Engine — Role Caps & Usage
# ============================================================================

# When redistributing injured-player minutes, a backup's ceiling is
# min(global_cap, baseline * ROLE_CAP_MULTIPLIER) with a floor of
# ROLE_CAP_MIN_FLOOR so deep-bench players can still absorb some load.
ROLE_CAP_MULTIPLIER: float = 1.25
ROLE_CAP_MIN_FLOOR: float = 20.0

# ── Spot-Start Promotion ─────────────────────────────────────────
# When a starter (>= SPOT_START_MIN_INJURED_BASELINE) is Out or
# Doubtful (factor <= SPOT_START_ABSENCE_THRESHOLD), the PRIMARY
# backup receives an elevated role cap based on the INJURED player's
# baseline — not their own.  This prevents deep-bench players who
# inherit a full starting role from being capped at 20 min when
# they should project 25-30 min.
SPOT_START_CAP_FACTOR: float = 0.88           # Cap at 88% of injured starter's baseline
SPOT_START_CAP_FACTOR_GTD: float = 0.50       # Partial promotion blend for Doubtful
SPOT_START_MIN_INJURED_BASELINE: float = 24.0  # Only starters trigger promotion
SPOT_START_ABSENCE_THRESHOLD: float = 0.50    # factor <= 0.50 to trigger (Out + Doubtful)

# Maximum per-minute usage-rate boost a player can receive from a
# teammate's injury (15% cap prevents over-projection).
USAGE_BOOST_CAP: float = 1.15

# ── Usage Boost Diminishing Returns ──────────────────────────────
# Players with high baseline FPPM (already elite per-minute producers)
# should receive a smaller marginal boost when teammates are injured.
# A player with 1.20 FPPM gaining +15% = 1.38 FPPM is unrealistic —
# nobody sustains 1.38 FPPM over a full game.  The dampening ensures
# that high-FPPM players get smaller multipliers while low-FPPM
# beneficiaries (bench players absorbing minutes) get full boosts.
#
# Formula:
#   effective_boost = 1.0 + raw_excess × dampening_factor
#   dampening_factor = max(DAMPENING_FLOOR, 1.0 - (fppm - DAMPENING_ONSET) × DAMPENING_RATE)
#
# Example at FPPM=1.20:
#   dampening = max(0.33, 1.0 - (1.20 - 0.90) × 2.0) = max(0.33, 0.40) = 0.40
#   If raw_boost = 1.15 → excess = 0.15 → dampened = 1.0 + 0.15 × 0.40 = 1.06
USAGE_BOOST_DAMPENING_ONSET: float = 0.90   # FPPM above this triggers dampening
USAGE_BOOST_DAMPENING_RATE: float = 2.0     # How aggressively to dampen per FPPM unit
USAGE_BOOST_DAMPENING_FLOOR: float = 0.33   # Minimum dampening factor (never < 33% of raw boost)

# ── Defensive Attention Penalty ──────────────────────────────────
# When a team's top-2 highest-usage players are both Out, defenses
# can key on the remaining primary scorer with double teams and
# aggressive trapping.  This penalizes the top remaining scorer's
# per-minute offensive rates.
#
# Example: Pelicans missing Zion + Ingram → CJ McCollum is the obvious
# target.  Defenses double him, reducing his efficiency.
DEFENSIVE_ATTENTION_PENALTY: float = 0.05   # FPPM penalty for remaining primary scorer
DEFENSIVE_ATTENTION_MIN_USAGE_OUT: int = 2  # Must be missing >= 2 high-usage players
DEFENSIVE_ATTENTION_USAGE_THRESHOLD: float = 0.22  # "High-usage" = 22%+ usage rate

# ── High-Usage Out: targeted per-minute-rate boost ────────────────
# When a player with projected_usage > HIGH_USAGE_OUT_THRESHOLD is
# ruled "Out" (factor = 0.00), the primary backup and secondary star
# each receive a per-minute stat-rate multiplier of 1.10×.
# This is on TOP of the existing proportional usage redistribution.
HIGH_USAGE_OUT_THRESHOLD: float = 0.25  # 25% usage rate
HIGH_USAGE_OUT_RATE_BOOST: float = 1.10  # 10% per-minute rate bump

# ── Injury Return Performance Decay ──────────────────────────────
# Players in their first N games back from injury show ~15% lower
# per-minute production while recalibrating to game speed.
INJURY_RETURN_DECAY_GAMES: int = 2       # first 2 games back
INJURY_RETURN_MINUTES_REDUCTION: float = 0.85  # 15% minutes reduction

# ── Blowout Script Allocation ──────────────────────────────────
# During overgeneration, reserve this fraction of candidates for
# "blowout" game scripts — lineups stacked toward games with large
# spreads (|spread| >= BLOWOUT_SPREAD_THRESHOLD).  Captures bench
# upside in blow-out scenarios where deep-roster players log
# extra garbage-time minutes.
BLOWOUT_SCRIPT_PCT: float = 0.15  # 15% of overgeneration candidates

# ── Salary Utilization Hard Gate ──────────────────────────────
# Before grading/scoring, discard any candidate that uses less than
# this fraction of the salary cap.  Higher than the structural
# quality gate (LINEUP_QUALITY_MIN_SALARY_PCT = 0.88) to ensure
# competitive candidates.
SALARY_UTILIZATION_HARD_FLOOR: float = 0.95  # 95% of salary cap

# Hard minimum salary for the ILP solver itself (Phase 2 K-Best).
# Lineups MUST spend at least this much — enforced as a >= constraint
# inside the PuLP model so the solver never even considers cheap builds.
MIN_SALARY_FLOOR: int = 49_300

# ============================================================================
# Rotation Engine — Coach & Game Context
# ============================================================================

# Minutes threshold to classify a player as a "starter" for coach
# adjustments and game-context rules (blowout, B2B, etc.).
STARTER_THRESHOLD_MINUTES: float = 24.0

# Below this threshold a player receives the full bench multiplier
# (above it, the bench multiplier is softened).
DEEP_BENCH_THRESHOLD_MINUTES: float = 15.0

# Blowout risk parameters.
BLOWOUT_SPREAD_THRESHOLD: float = 7.0     # |spread| >= this triggers adjustment
BLOWOUT_PENALTY_PER_POINT: float = 0.015  # reduction per spread point beyond threshold
BLOWOUT_MIN_FACTOR: float = 0.90          # floor on the blowout multiplier

# Non-linear blowout penalty curve.
# Exponent < 1.0 applies diminishing returns: big spreads don't linearly
# scale up the penalty because coaches still play starters ~30+ min even
# in 15-point games — they only pull them in the final 5-6 minutes.
#   Linear (old): penalty = excess * 0.015            → 12-pt spread = 7.5%
#   Curve  (new): penalty = (excess ** 0.70) * 0.015  → 12-pt spread = 5.1%
BLOWOUT_PENALTY_EXPONENT: float = 0.70

# Star dampening: top-2 starters (≥ STAR_ANCHOR_THRESHOLD) receive a
# reduced blowout penalty because coaches leave their best players in
# longer than role starters.  0.60 = stars absorb 60% of the penalty.
STAR_BLOWOUT_DAMPENING: float = 0.60

# Back-to-back fatigue parameters.
B2B_STARTER_REDUCTION_MINUTES: float = 2.0  # minutes lost for starters on B2B
B2B_VETERAN_EXTRA_MINUTES: float = 1.0      # additional minutes lost for veterans
VETERAN_AGE: int = 32                        # age threshold for "veteran" classification

# ============================================================================
# Rotation Engine — Normalization (240-minute team constraint)
# ============================================================================

# NBA league-average pace (possessions per 48 minutes).
LEAGUE_AVG_PACE: float = 101.9

# Total regulation minutes available per team per game (5 players x 48 min).
TOTAL_TEAM_MINUTES: float = 240.0

# Top-2 players with baseline >= this are "star-anchored" during
# compression (their minutes are protected first).
# Lowered from 30.0 to capture 26-29 MPG starters who should also
# be protected from compression.
STAR_ANCHOR_THRESHOLD: float = 26.0

# Players compressed below this threshold are effectively DNPs.
MIN_VIABLE_MINUTES: float = 2.0

# ── Top-Heavy Tiered Normalization ──────────────────────────────────
# When the team exceeds 240 minutes, shave from the bottom tier first.
# Only escalate to the next tier when the lower tier is exhausted.
#
# Tier 1 ("Locked"):     Projected >= NORM_LOCK_THRESHOLD
#   - Immune to downward normalization unless mathematically necessary
#     (i.e. all lower tiers have been zeroed and excess remains).
#
# Tier 2 ("Mid-tier"):   NORM_MID_THRESHOLD <= projected < NORM_LOCK_THRESHOLD
#   - Only shaved after bench tier is fully exhausted.
#   - Each player may lose at most NORM_MID_MAX_CUT_PCT of their minutes.
#
# Tier 3 ("Bench"):      Projected < NORM_MID_THRESHOLD
#   - First to absorb compression. Shaved proportionally (lower minutes
#     absorb more per 1/min weighting). May be reduced to 0.
NORM_LOCK_THRESHOLD: float = 30.0      # >=30 min → "locked" starter
NORM_MID_THRESHOLD: float = 20.0       # >=20 min → mid-tier role player
NORM_MID_MAX_CUT_PCT: float = 0.15     # Mid-tier players lose at most 15%

# Short-rotation guardrail: when the active roster is this small or
# smaller, starters may exceed ABSOLUTE_MAX_MINUTES up to this ceiling.
SHORT_ROTATION_SIZE: int = 8
SHORT_ROTATION_STARTER_CEILING: float = 44.0  # OT-buffer ceiling

# When inflating a roster that sums below 240, no player may exceed
# baseline * MAX_INFLATION_CEILING or ABSOLUTE_MAX_MINUTES.
MAX_INFLATION_CEILING: float = 1.25
ABSOLUTE_MAX_MINUTES: float = 42.0

# ============================================================================
# DFS Service — Floor / Ceiling Multipliers
# ============================================================================

# Minutes component of range estimates.
FLOOR_MINUTES_MULT: float = 0.90    # Bad night: 10% fewer minutes
CEILING_MINUTES_MULT: float = 1.10  # Great night: 10% more minutes

# Per-minute stat-rate variance (shooting variance is the dominant FP driver).
FLOOR_RATE_MULT: float = 0.75       # Cold shooting: 25% below season rate
CEILING_RATE_MULT: float = 1.30     # Hot shooting: 30% above season rate

# ============================================================================
# DFS Service — Pace Sensitivity
# ============================================================================

# How strongly each stat category scales with game pace.
#   1.0 = fully proportional to pace
#   0.0 = completely independent of pace
PACE_SENSITIVITY: dict[str, float] = {
    "pts": 1.0,    # Points scale directly with possessions
    "ast": 1.0,    # Assists scale directly with possessions
    "fg3m": 0.9,   # 3PM closely tracks pace (more possessions = more 3PA)
    "tov": 0.8,    # Turnovers are mostly pace-driven
    "reb": 0.6,    # Rebounds are moderately pace-sensitive
    "stl": 0.3,    # Steals are mostly matchup-driven
    "blk": 0.2,    # Blocks are mostly matchup-driven
}

# ============================================================================
# DFS Service — DD/TD Probability Model
# ============================================================================

# Game-to-game stat coefficient of variation for DD/TD EV calculation.
# Derived from NBA game-log analysis (2021-2024 seasons).
DD_STAT_CV: dict[str, float] = {
    "pts": 0.25,   # Points: relatively stable (20-25% CV)
    "reb": 0.28,   # Rebounds: moderate variance
    "ast": 0.35,   # Assists: higher variance (playmaking is game-flow dependent)
    "stl": 0.55,   # Steals: highly volatile (rare event)
    "blk": 0.60,   # Blocks: highly volatile (rare event)
}

# Legacy flat fallback for backward compatibility.
DD_STAT_VARIANCE: float = 0.30

# ============================================================================
# DFS Service — Usage Boost Stat Weights
# ============================================================================

# Per-stat sensitivity to usage boost.  1.0 = full boost, 0.0 = no effect.
# When a teammate is injured and usage increases, scoring and assists
# increase most; rebounds and defensive stats barely change.
USAGE_BOOST_STAT_WEIGHTS: dict[str, float] = {
    "pts": 1.0,    # Scoring: full boost from extra touches
    "ast": 0.8,    # Assists: strong boost (more ball-handling)
    "fg3m": 0.6,   # 3PM: moderate (more shot attempts)
    "tov": 0.5,    # Turnovers: moderate (more touches = more mistakes)
    "reb": 0.3,    # Rebounds: weak (mostly positional, not usage-driven)
    "stl": 0.1,    # Steals: minimal (defensive effort, not usage)
    "blk": 0.0,    # Blocks: none (purely defensive positioning)
}

# ============================================================================
# DFS Service — Opponent-Adjusted Floor/Ceiling
# ============================================================================

# DvP modulation for floor/ceiling rate multipliers.
# floor_mult = FLOOR_RATE_MULT + (dvp_mean - 1.0) * DVP_FLOOR_SENSITIVITY
DVP_FLOOR_SENSITIVITY: float = 0.15      # How much DvP shifts the floor
DVP_CEILING_SENSITIVITY: float = 0.20    # How much DvP shifts the ceiling

# ============================================================================
# Calibration — Validation Bounds
# ============================================================================

# Any calibration outside this range is treated as corrupt data and ignored.
CALIBRATION_VALID_MIN: float = 0.5
CALIBRATION_VALID_MAX: float = 2.0

# ============================================================================
# Rotation Engine — Backup Hierarchy
# ============================================================================

# Blend of season average and recent form for ranking backups.
BACKUP_SEASON_WEIGHT: float = 0.6
BACKUP_RECENT_WEIGHT: float = 0.4

# ============================================================================
# Rotation Engine — Top-Down Minute Allocator
# ============================================================================

# Feature toggle: True = top-down "Starter's Squeeze" allocation (new).
# The top-down allocator replaces the bottom-up baseline + normalize
# approach with a strict 240-minute budget that:
#   - Zeros out inactive players FIRST (Active Status Guillotine)
#   - Identifies 5 starters per positional depth chart
#   - Allocates starter minutes greedily (28-38 min per starter)
#   - Cascades remaining ~70-80 min to bench via depth-chart rank
#   - Guarantees sum == 240 with zero bench bloat
#
# When False, the legacy per-player get_baseline_projection() + Step 4
# normalization is used (original behavior).
USE_TOP_DOWN_MINUTES: bool = True

# ============================================================================
# Rotation Engine — Hierarchical Substitution DAG
# ============================================================================

# Feature toggle: True = position-specific DAG, False = legacy flat waterfall
USE_HIERARCHICAL_DAG: bool = True

# Position-specific cascade DAG.
# Key: vacated position (injured player's specific 5-position code)
# Value: ordered list of (target_position, share_of_freed_minutes)
#   - Shares sum to 1.0 per vacated position
#   - Order matters: overflow from capped absorbers spills to next node
POSITION_SUBSTITUTION_DAG: dict[str, list[tuple[str, float]]] = {
    "PG": [("PG", 0.70), ("SG", 0.30)],
    "SG": [("SG", 0.70), ("PG", 0.30)],
    "SF": [("SF", 0.70), ("PF", 0.20), ("SG", 0.10)],
    "PF": [("PF", 0.70), ("SF", 0.20), ("C", 0.10)],
    "C":  [("C", 0.80), ("PF", 0.20)],
}

# Fallback for simplified/generic position codes → nearest specific position
# for DAG lookup.  When a player's position is "G" or "F" (NBA API format),
# resolve to the nearest specific 5-position code.
POSITION_DAG_FALLBACK: dict[str, str] = {
    "G": "PG",
    "F": "SF",
    "Guard": "PG",
    "Forward": "SF",
    "Center": "C",
}

# Hard minute caps for hierarchical absorption
HIER_STARTER_CEILING: float = 38.0     # Max minutes for starters (baseline >= 24)
HIER_BENCH_CEILING: float = 28.0       # Max minutes for bench players
HIER_STARTER_THRESHOLD: float = 24.0   # Baseline >= this = starter for ceiling selection

# Primary/secondary absorption split within each cascade target
HIER_PRIMARY_SHARE: float = 0.65       # Top-ranked backup absorbs this share
HIER_SECONDARY_SHARE: float = 0.35     # Remaining candidates split this share

# ============================================================================
# Rotation Engine — Continuous Rest-Day Curve
# ============================================================================

# rest_factor = 1.0 + REST_BOOST_PER_EXTRA_DAY * (rest_days - 1)
#             - REST_PENALTY_B2B * max(0, 1 - rest_days)
# Capped to [REST_FACTOR_MIN, REST_FACTOR_MAX].
REST_BOOST_PER_EXTRA_DAY: float = 0.02    # +2% per extra rest day above 1
REST_PENALTY_B2B: float = 0.03            # -3% for B2B (rest_days=0)
REST_FACTOR_MIN: float = 0.93             # Floor (extreme fatigue)
REST_FACTOR_MAX: float = 1.04             # Ceiling (well-rested)

# ============================================================================
# Rotation Engine — Multi-Game Trip Fatigue
# ============================================================================

FATIGUE_LOOKBACK_DAYS: int = 6          # How many days back to count games
FATIGUE_THRESHOLD_GAMES: int = 3        # Games in period that triggers fatigue
FATIGUE_PENALTY_PER_GAME: float = 0.01  # Per-game fatigue penalty above threshold
FATIGUE_MAX_PENALTY: float = 0.04       # Maximum fatigue reduction (4%)

# ============================================================================
# DFS Service — Props Market Calibration
# ============================================================================

# When our stat projection diverges from the DK Sportsbook prop line by
# more than PROPS_DIVERGENCE_THRESHOLD, blend toward the market.
# adjusted = ours × BLEND_WEIGHT + market × (1 - BLEND_WEIGHT)
PROPS_PROJECTION_BLEND_WEIGHT: float = 0.70   # 70% our model, 30% market
PROPS_DIVERGENCE_THRESHOLD: float = 0.15      # 15% divergence triggers blend

# DD/TD probability blending with DK implied probability.
# blended_p_dd = ours × DD_BLEND + dk_implied × (1 - DD_BLEND)
PROPS_DD_BLEND_WEIGHT: float = 0.60           # 60% our Monte Carlo, 40% market

# ============================================================================
# DK Sportsbook — Configuration
# ============================================================================

DK_SPORTSBOOK_NBA_EVENT_GROUP_ID: int = 42648
DK_PROPS_CACHE_TTL: int = 900                 # 15 minutes
DK_FPPG_CACHE_TTL: int = 86400               # 24 hours (daily)
DK_CONTEST_DETAIL_CACHE_TTL: int = 1800      # 30 minutes

# ============================================================================
# Lineup Optimizer — Position-Specific Salary Tiers
# ============================================================================

# Per-position salary thresholds for tier classification.
# Reflects distinct salary distributions: e.g. $7200 is premium for C
# (compressed range) but mid-tier for PG.
POSITION_SALARY_TIERS: dict[str, dict[str, int]] = {
    "PG": {"high": 7500, "mid": 5000},
    "SG": {"high": 7500, "mid": 5000},
    "SF": {"high": 7000, "mid": 4500},
    "PF": {"high": 7000, "mid": 4500},
    "C":  {"high": 7500, "mid": 5500},   # Compressed range; fewer cheap Cs
}

# Default thresholds for positions not in the dict above (UTIL, G, F slots).
POSITION_SALARY_TIERS_DEFAULT: dict[str, int] = {"high": 8000, "mid": 5000}

# Position-specific GPP ceiling-value multipliers.
# Guards have higher variance due to assist/3PM upside; bigs are steadier.
POSITION_GPP_VALUE_MULTIPLIERS: dict[str, float] = {
    "PG": 1.04,    # Assist upside
    "SG": 1.02,    # 3PM upside
    "SF": 1.00,    # Baseline
    "PF": 0.99,    # Slightly lower ceiling variance
    "C":  0.98,    # Lowest ceiling variance (boards are steady)
}

# ============================================================================
# Lineup Optimizer — Correlation-Driven Stacking
# ============================================================================

# Weight blend for stack partner selection.
# First player: selected by projection. Subsequent players use this blend.
STACK_PROJECTION_WEIGHT: float = 0.35        # How much projection matters for 2nd+ player
STACK_CORRELATION_WEIGHT: float = 0.65       # How much correlation to selected matters

# 3-man stacks require avg pairwise correlation above this floor,
# otherwise they are downgraded to 2-man stacks.
STACK_3MAN_CORRELATION_FLOOR: float = 0.20

# Cross-team correlation weight for bring-back (opponent) selection.
BRINGBACK_CROSS_TEAM_CORR_WEIGHT: float = 0.40
BRINGBACK_PROJECTION_WEIGHT: float = 0.35

# ============================================================================
# Lineup Optimizer — Dynamic Stacking Ratios
# ============================================================================

# Default stack size distribution when no calibrations exist.
DEFAULT_STACK_2MAN_RATIO: float = 0.40
DEFAULT_STACK_3MAN_RATIO: float = 0.60
DEFAULT_BRINGBACK_RATE: float = 0.70

# In GPP, avoid heavy stacking of games where avg player ownership
# exceeds this threshold (chalk avoidance).
STACK_OWNERSHIP_GATE_THRESHOLD: float = 25.0

# ============================================================================
# Ownership Model — Learnable Weight Defaults
# ============================================================================

# Default factor weights for the rules-based ownership model.
# Serve as baselines when no learned adjustments exist.
OWNERSHIP_DEFAULT_WEIGHTS: dict[str, float] = {
    "value": 0.35,
    "salary": 0.20,
    "game_env": 0.15,
    "expert": 0.12,
    "projection": 0.15,
    "star_premium": 0.08,
    "scarcity": 0.08,
    "minutes": 0.15,
    "b2b": 0.04,
    "spread": 0.08,
    "multi_position": 0.04,
    "injury_benefit": 0.12,
}

# Maximum deviation from default weight (as fraction). 0.30 = ±30%.
OWNERSHIP_WEIGHT_ADJUSTMENT_CAP: float = 0.30

# Minimum contests required before ownership learning activates.
OWNERSHIP_LEARNING_MIN_CONTESTS: int = 3

# ============================================================================
# Game Service — Vegas Line Anchoring
# ============================================================================

# When Vegas over/under and spread are available, blend model projections
# toward the market.  Vegas lines are typically the most accurate available
# pre-game predictor, so we weight them heavily.
VEGAS_TOTAL_BLEND_WEIGHT: float = 0.80     # 80% Vegas total, 20% model total
VEGAS_SPREAD_BLEND_WEIGHT: float = 0.75    # 75% Vegas spread, 25% model spread

# If our projected total diverges from Vegas by more than this fraction
# of the Vegas line, log a warning (useful for debugging projection model).
VEGAS_DIVERGENCE_WARNING_THRESHOLD: float = 0.10   # 10%

# ============================================================================
# Simulation Engine — Game Script Scenarios
# ============================================================================

# Multi-scenario game-script modeling replaces the single-normal approach.
# For each simulation, a game state is sampled with probabilities derived
# from the spread, then scenario-specific multipliers are applied.

# Scenario: (starter_mult, bench_mult, pace_mult)
#   - starter_mult: scales per-minute rates for starters (>=24 min projected)
#   - bench_mult:   scales per-minute rates for bench (<24 min projected)
#   - pace_mult:    nudge applied to the already-sampled pace
GAME_SCRIPT_SCENARIOS: dict[str, dict[str, float]] = {
    "blowout_win":  {"starter_mult": 0.92, "bench_mult": 1.12, "pace_mult": 0.96},
    "blowout_loss": {"starter_mult": 0.90, "bench_mult": 1.15, "pace_mult": 0.97},
    "close_game":   {"starter_mult": 1.06, "bench_mult": 0.88, "pace_mult": 1.00},
    "shootout":     {"starter_mult": 1.04, "bench_mult": 1.00, "pace_mult": 1.06},
    "grind":        {"starter_mult": 1.00, "bench_mult": 0.94, "pace_mult": 0.94},
}

# Starter threshold for game-script scenario application.
GAME_SCRIPT_STARTER_MINUTES: float = 24.0

# ============================================================================
# Rotation Engine — Garbage Time Quality
# ============================================================================

# When blowout adjustments reduce starter minutes, the redistributed bench
# minutes are "garbage time" quality — lower per-minute production.
GARBAGE_TIME_RATE_DISCOUNT: float = 0.85   # 85% of normal per-min production
# Spread threshold beyond which extra bench minutes are tagged as garbage time.
GARBAGE_TIME_SPREAD_THRESHOLD: float = 7.0

# ============================================================================
# Lineup Optimizer — Portfolio Correlation Diversity
# ============================================================================

# Maximum acceptable pairwise game-stack correlation between any two lineups
# in the portfolio.  Lineups exceeding this are penalized during selection.
PORTFOLIO_MAX_GAME_CORRELATION: float = 0.60

# Weight of the game-stack correlation penalty relative to the base score.
# Higher = more aggressive diversification.
PORTFOLIO_CORRELATION_PENALTY: float = 0.06  # Up to 6% score reduction

# ── Portfolio ILP (Joint Portfolio Optimization) ──────────────────────
# When PuLP is available and candidate count ≤ max, use ILP to jointly
# select the best portfolio of lineups under exposure constraints.
# Falls back to greedy _select_best_diverse when conditions are not met.
PORTFOLIO_ILP_MAX_CANDIDATES: int = 550       # Skip ILP if more candidates (raised for oversampling: 500 raw → ~450 after quality filter)
PORTFOLIO_ILP_SOLVER_TIMEOUT: int = 75        # CBC solver seconds (was 45 — raised after observing the solver accepting early incumbents at 45s ceiling on 500-candidate pools; extra 30s typically yields a measurably better portfolio score)
PORTFOLIO_ILP_MIN_LINEUPS: int = 5            # Need ≥5 requested to justify ILP
PORTFOLIO_ILP_DIVERSITY_PENALTY: float = 0.02 # Per-player-overlap penalty weight
PORTFOLIO_ILP_MIN_EXPO_PENALTY: float = 1000.0 # Penalty per shortfall unit for soft min-exposure

# ── ILP CBC Solver Tuning ─────────────────────────────────────────────
# Fine-grained control over the PuLP/CBC branch-and-bound solver used
# by _ilp_optimize() for individual lineup construction.
ILP_CBC_TIME_LIMIT: int = 8              # Solver timeout (seconds); was 5 — 8s lets CBC explore more B&B nodes
ILP_CBC_PRESOLVE: bool = True            # Simplify constraint matrix before B&B
ILP_CBC_GAP_REL: float = 0.005           # Stop within 0.5% of proven optimal; was 0.001 — declares victory sooner
ILP_CBC_CUTOFF_ENABLED: bool = True       # Pass greedy score as lower-bound cutoff
ILP_CBC_CUTOFF_DISCOUNT: float = 0.98     # 2% headroom for stochastic score_fn jitter; was 0.995 — wider exploration

# ── Multi-Lineup Candidate Generation Tuning ─────────────────────────
# When generating multiple lineups, the overgenerate-then-filter pipeline
# calls the ILP solver per candidate.  These constants cap the total cost.
ILP_CANDIDATE_TIME_LIMIT: int = 8       # Per-candidate ILP timeout; was 5 — matches ILP_CBC_TIME_LIMIT increase
MULTI_LINEUP_TIME_BUDGET: float = 420.0  # Total seconds before early termination (raised for 500-candidate oversampling: ~600 seeds × 0.5s each)
ENRICH_TIER1_TIMEOUT_S: float = 30.0    # Hard timeout for Tier 1 enrichment — must be long enough for game context (stacking depends on game_id)
ENRICH_AI_AGENT_TIMEOUT_S: float = 10.0 # Per-agent AI call timeout within enrichment (ownership, sim tuning, news, strategy)
MULTI_LINEUP_PARALLEL_WORKERS: int = 2  # ThreadPoolExecutor workers; 2 avoids CBC threading contention on Windows
MULTI_LINEUP_ILP_CAP: int = 40          # Max candidates that get ILP refinement (rest greedy-only)
# Budget-mode limits for overgen buffer candidates (skip_ilp=True).
# These skip the O(pool²) two-slot swap and reduce iterative improve
# iterations because these candidates are diversity fodder for Phase 4.
BUDGET_IMPROVE_ITERATIONS: int = 25     # vs 100 for quality candidates
QUALITY_IMPROVE_ITERATIONS: int = 75    # quality candidates (ILP-enabled); was 50 — better warm-start for ILP

# ── K-Best Iterative ILP Tuning ────────────────────────────────────
# Phase 2 now uses a K-Best iterative solver: build the PuLP prob once
# per stack target per noise seed, then iteratively solve and add
# exclusion constraints to generate diverse lineups efficiently.
KBEST_MAX_OVERLAP: int = 7              # Max shared players between K-Best lineups (8 - 1 = at least 1 new player per iteration); diversity enforced later in Phase 4
KBEST_MAX_NOISE_SEEDS: int = 20         # Max noise seed iterations per stack target (up from 10 — more diversity)
KBEST_MIN_LINEUPS_PER_STACK: int = 2    # Minimum allocation per stack target (below this, K-Best has no benefit)

# ── Oversampling Architecture ──────────────────────────────────────
# Decouple generation from curation: Phase 2 generates a massive pool
# of candidates (the Factory) using Monte Carlo noise for diversity,
# with NO overlap exclusion constraints.  Phase 4 (the Portfolio ILP)
# then curates the best N lineups with strict overlap rules.
# This avoids the "greedy sequential trap" where exclusion constraints
# pile up and cause CBC Infeasible before reaching the target count.
OVERSAMPLE_TARGET: int = 500            # Raw candidates Phase 2 produces
OVERSAMPLE_NOISE_SEEDS: int = 600       # Max noise seeds (1 lineup per seed; 600 → ~500 unique after dedup)
OVERSAMPLE_DEDUP: bool = True           # Skip exact-duplicate lineups across seeds

# ── Exposure-Aware Generation ─────────────────────────────────────
# Quadratic penalty applied to ILP objective coefficients during K-Best
# generation.  Steers the solver away from over-drafted players without
# hard-excluding them until they hit the cap.
# ── Absolute Global Exposure Ceiling ────────────────────────────────
# HARD ceiling that NO player can exceed, regardless of value ratio,
# ownership, chalk status, or tier classification.  This prevents
# catastrophic portfolio ruin when a single player busts.
#
# At 55% max, even if the optimizer's #1 projected play has a bad night,
# 45% of your portfolio survives without them.  The existing tier-based
# caps (Elite Core, Mega Chalk, etc.) now operate UNDER this ceiling.
ABSOLUTE_GLOBAL_MAX_EXPOSURE: float = 0.55  # 55% hard ceiling for any single player

EXPOSURE_PENALTY_DEFAULT_CAP: float = 0.55  # Default max_exposure when user provides none (was 0.70)

# ── Additive Novelty + Variance Spike (Exploration Incentive) ────────
# Undrafted players (drafted_count == 0) receive:
#   1. Additive score bonus: base_score + NOVELTY_ADDITIVE_FP
#   2. Variance spike: extra gauss(0, sigma * (SPIKE_MULT - 1.0))
# Both revert to normal once drafted_count >= 1.
NOVELTY_ADDITIVE_FP: float = 3.5              # Flat FP boost for undrafted
NOVELTY_VARIANCE_SPIKE_MULT: float = 1.50     # 50% extra sigma for undrafted
NOVELTY_ENABLED: bool = True                  # Kill switch for A/B testing
NOVELTY_MIN_BASE_SCORE: float = 18.0         # Novelty bonus only applies if base_score > this

# ── Pool Pruning: Strict Projection Floor ──────────────────────────
# Players below this FP threshold are removed from the K-Best solver pool
# UNLESS their value_ratio (FP/$1K) exceeds the value exemption threshold.
# At 20.0 FP, this guillotines junk plays like Gary Payton II (17.5 FP,
# 4.17x) and Pat Spencer (17.2 FP, 3.74x) while preserving elite
# min-salary punts (Taelon Peter 5.67x, Ben Sheppard 5.26x) via value.
KBEST_PROJECTION_FLOOR: float = 20.0
KBEST_PROJECTION_FLOOR_VALUE_EXEMPT: float = 5.0  # FP/$1K above this bypasses floor

# ── Elite Core Overlap Exemption ("Free Squares") ──────────────────
# Players with value_ratio above this threshold are excluded from the
# K-Best overlap exclusion constraint.  This lets elite chalk (e.g.,
# Kam Jones at 7.77x) appear in every lineup without consuming overlap
# budget, allowing the solver to find diverse combinations around them.
ELITE_CORE_VALUE_THRESHOLD: float = 6.5
ELITE_CORE_MAX_EXPOSURE: float = 0.55   # Capped at global ceiling (was 0.95)
ELITE_CORE_MIN_EXPOSURE: float = 0.45   # Strong min-exposure floor for elite core (was 0.80)

# ── Mega Chalk Detection ─────────────────────────────────────────────
# Players that the field will own at extreme rates.  Identified by EITHER
# a high value multiplier (lower bar than elite core) OR high projected
# ownership from Agent 7 / the rules-based model.  Mega Chalk players
# are merged into the elite core set: overlap-exempt in the portfolio
# ILP conflict graph, exempt from Phase 4a-post exposure filtering, and
# assigned exposure caps capped at the global ceiling.
MEGA_CHALK_VALUE_THRESHOLD: float = 6.0     # Value ratio (fp / salary-in-thousands)
MEGA_CHALK_OWNERSHIP_THRESHOLD: float = 50.0  # Projected ownership % (0-100)
MEGA_CHALK_MAX_EXPOSURE: float = 0.50       # Exposure cap for mega chalk (was 0.90, now under global ceiling)

# ── Strong Mid-Tier Exposure Boost ───────────────────────────────────
# Mid-salary players ($5k-$7.5k) with solid value get a relaxed cap
# so the ILP has more valid puzzle pieces to reach 150 lineups.
STRONG_MID_TIER_SALARY_MIN: int = 5000
STRONG_MID_TIER_SALARY_MAX: int = 7500
STRONG_MID_TIER_VALUE_THRESHOLD: float = 4.5   # Min value ratio (fp / salary-in-thousands)
STRONG_MID_TIER_OPTIMAL_THRESHOLD: float = 15.0  # OR min optimal_pct (0-100)
STRONG_MID_TIER_MAX_EXPOSURE: float = 0.45       # Relaxed cap (up from default ~0.30)

# ── Rolling Window Overlap Constraint Limit ──────────────────────────
# CBC solver degrades with 40+ overlap constraints per noise seed.
# Only retain the last N exclusion constraints; older ones are deleted
# from prob.constraints to keep the matrix lean.
KBEST_OVERLAP_WINDOW: int = 25

# ── Correlation Stack Bonus ───────────────────────────────────────────
# Flat FP bonus added to lineup score when it contains a qualifying
# same-team stack.  Qualifying = 2-3 players from the same team whose
# average FP/$1K exceeds the minimum threshold.  Encourages the solver
# to pair high-value teammates (e.g., injury-depleted teams).
# Phase 2: per-player teammate density bonus in composite scoring.
# Phase 4: lineup-level additive FP in _score_lineup.
CORRELATION_STACK_MIN_AVG_VALUE: float = 4.6  # Min avg FP/$1K for stack to qualify (lowered from 5.2)
CORRELATION_STACK_MAX_PLAYERS: int = 3        # Cap: don't reward 4+ stacks
CORRELATION_STACK_TEAMMATE_BONUS: float = 0.03  # 3% Phase 2 per-player bonus for dense teams
CORRELATION_STACK_BASE_BONUS_FP: float = 1.5   # Base FP bonus at threshold
CORRELATION_STACK_BONUS_PER_TICK: float = 0.5  # Additional FP per 0.1x above threshold
CORRELATION_STACK_BONUS_CAP_FP: float = 8.0    # Hard cap on dynamic bonus

# ── Dynamic Variance Scaling (Salary-Tier Sigma Multiplier) ──────────
# Widens Gaussian noise sigma for cheap/volatile players so they can
# occasionally spike into lineup consideration.
VARIANCE_SCALE_LOW_SALARY_THRESHOLD: int = 5000
VARIANCE_SCALE_LOW_SALARY_MULT: float = 1.35     # 35% wider sigma
VARIANCE_SCALE_LOW_CONFIDENCE_MULT: float = 1.25  # 25% wider (rotation_confidence < 0.75)
VARIANCE_SCALE_MAX_COMBINED: float = 1.50          # Cap combined multiplier

# ── GPP Tier-Based Auto-Exposure Caps ────────────────────────────────
# Automatic exposure limits by player tier.  Only active in GPP/single_entry.
# Players are classified by projection rank percentile within the pool.
GPP_AUTO_EXPO_STUD_PERCENTILE: float = 0.15      # Top 15% by projected_fp = "stud"
GPP_AUTO_EXPO_MID_PERCENTILE: float = 0.50        # 15-50% = "mid-tier"
GPP_AUTO_EXPO_MID_CAP: float = 0.55               # Mid-tier max exposure
GPP_AUTO_EXPO_VALUE_CAP: float = 0.50             # Value/punt max exposure
GPP_AUTO_EXPO_VALUE_SALARY_THRESHOLD: int = 5000  # Below this salary = "value"

# ── GPP Min Exposure for Top Projected Players ──────────────────────
GPP_AUTO_MIN_EXPO_TOP1: float = 0.50              # #1 projected: >=50% of lineups
GPP_AUTO_MIN_EXPO_TOP2: float = 0.35              # #2 projected: >=35%
GPP_AUTO_MIN_EXPO_TOP3: float = 0.25              # #3 projected: >=25%

# ── GPP Low-Owned Upside Slot ───────────────────────────────────────
GPP_CONTRARIAN_SLOT_PCT: float = 0.25             # 25% of lineups get a contrarian lock
GPP_CONTRARIAN_MAX_OWNERSHIP: float = 5.0         # Max ownership% for contrarian pick
GPP_CONTRARIAN_MIN_UPSIDE_RATIO: float = 1.5      # ceiling_fp >= proj × 1.5
GPP_CONTRARIAN_MIN_PROJECTION: float = 10.0       # Must project >= 10 FPTS
GPP_CONTRARIAN_MIN_LINEUPS: int = 10              # Only activate for portfolios of 10+

# ── GPP Punt Play Risk Gate ─────────────────────────────────────────
GPP_PUNT_FLOOR_GATE_SALARY: int = 4500            # Salary threshold for floor gate
GPP_PUNT_FLOOR_MILD_THRESHOLD: float = 8.0        # floor_fp < 8 → mild penalty
GPP_PUNT_FLOOR_SEVERE_THRESHOLD: float = 5.0      # floor_fp < 5 → severe penalty
GPP_PUNT_FLOOR_MILD_PENALTY: float = 0.90         # 10% score reduction (softened from 0.85)
GPP_PUNT_FLOOR_SEVERE_PENALTY: float = 0.80       # 20% score reduction (softened from 0.70)

# ── Lineup Quality Gate ────────────────────────────────────────────────
# Minimum quality thresholds used to filter out subpar lineups before
# returning them to the user.  Operates on the lineup-level score from
# _score_lineup() as well as structural checks.
#
# Relative floor: a lineup's score must be ≥ this fraction of the best
# candidate's score.  E.g. 0.70 means any lineup scoring <70% of the
# top candidate is dropped.
LINEUP_QUALITY_RELATIVE_FLOOR: float = 0.70

# Minimum salary usage: lineups below this % of salary cap are dropped.
LINEUP_QUALITY_MIN_SALARY_PCT: float = 0.88

# Minimum distinct teams: lineups must contain players from at least N
# teams (prevents degenerate single-team builds when pool is tiny).
LINEUP_QUALITY_MIN_TEAMS: int = 2

# Minimum projected FP: lineups whose total projected FP falls below
# this fraction of the baseline optimal projection are rejected.
# E.g. 0.75 means a 254 FP optimal → floor of 190.5 FP.
# This catches degenerate lineups with obviously weak projections
# even when their blended score (ceiling/ownership/stacking) is decent.
LINEUP_QUALITY_MIN_PROJECTION_PCT: float = 0.75

# For single-lineup requests: retry up to N times if the initial build
# fails the quality gate before returning the best attempt.
LINEUP_QUALITY_SINGLE_MAX_RETRIES: int = 3

# ============================================================================
# DFS Service — Player-Specific Floor/Ceiling Spread
# ============================================================================

# Instead of using global FLOOR_RATE_MULT / CEILING_RATE_MULT for every
# player, scale the range by each player's game-to-game consistency.
# A high-CV player gets a wider range; a low-CV player gets a tighter range.
#
# effective_floor_rate = FLOOR_RATE_MULT * (1 + (cv - CV_BASELINE) * CV_FLOOR_SCALE)
# effective_ceil_rate  = CEILING_RATE_MULT * (1 + (cv - CV_BASELINE) * CV_CEILING_SCALE)
PLAYER_CV_BASELINE: float = 0.30          # League-average DFS FP CV (~30%)
PLAYER_CV_FLOOR_SCALE: float = 0.40       # Floor widens 40% per unit CV above baseline
PLAYER_CV_CEILING_SCALE: float = 0.35     # Ceiling widens 35% per unit CV above baseline
PLAYER_CV_FALLBACK: float = 0.30          # Fallback CV when history is unavailable

# ============================================================================
# Simulation Engine — Matchup-Quality Variance Adjustment
# ============================================================================

# Scale noise sigma inversely with favorable DvP matchup.
# sigma_adjusted = sigma_base * (DVP_VARIANCE_ANCHOR - dvp_mean + 1.0)
# Against favorable DvP (1.10) → sigma × 0.90 (more predictable)
# Against tough DvP (0.90) → sigma × 1.10 (less predictable)
DVP_VARIANCE_SENSITIVITY: float = 1.0     # Full sensitivity (0.0 = no effect)

# ============================================================================
# Lineup Optimizer — DK FPPG Projection Blending
# ============================================================================

# When our projection diverges from DK's FPPG by more than this threshold,
# blend toward the DK value.  Similar to props blending but for DK's own
# aggregate projection (Available Players endpoint).
DK_FPPG_DIVERGENCE_THRESHOLD: float = 0.15  # 15% divergence triggers blend
DK_FPPG_BLEND_WEIGHT: float = 0.85          # 85% ours, 15% DK (lighter than props)
DK_FPPG_BLEND_ASYMMETRIC: bool = True       # Directional: downward blends use 30% threshold + gentler weights

# ============================================================================
# Lineup Optimizer — Strategy-Specific Noise Bands
# ============================================================================

# Noise multiplier ranges applied to composite score during lineup
# construction to create natural diversity across generated lineups.
# Tighter bands for projection-focused strategies preserve top-projection
# player ordering; wider bands for GPP diversity strategies.
NOISE_GPP_PURE_MAX: tuple = (0.95, 1.05)        # ±5%  — minimal jitter
NOISE_GPP_MAX_PROJECTION: tuple = (0.92, 1.08)  # ±8%  — was ±12%; tighter to preserve projection ordering
NOISE_GPP_DEFAULT: tuple = (0.88, 1.12)         # ±12% — was ±18%; reduced to prevent star/role-player inversion
NOISE_CASH_DEFAULT: tuple = (0.93, 1.07)         # ±7%  — cash games (all strategies)

# Quality floor override for pure_max (tighter since noise is minimal —
# low-scoring candidates are genuinely weak, not unlucky noise rolls).
LINEUP_QUALITY_RELATIVE_FLOOR_PURE_MAX: float = 0.85

# ============================================================================
# Lineup Optimizer — Composite Score Clamp
# ============================================================================

# Maximum allowed composite score as a multiple of raw projected_fp.
# Prevents extreme multiplier cascading from 15+ stacked adjustments.
COMPOSITE_SCORE_MAX_MULTIPLIER: float = 2.5

# ============================================================================
# Lineup Optimizer — Rotation Confidence Penalty
# ============================================================================

# Players with uncertain minute projections receive composite score penalties.
ROTATION_CONFIDENCE_PENALTY_LOW: float = 0.88    # <60% confidence (was 0.95)
ROTATION_CONFIDENCE_PENALTY_MED: float = 0.95    # <80% confidence (was 0.98)

# ============================================================================
# Lineup Optimizer — Continuous Defensive Rating Adjustment
# ============================================================================

# Linear DRtg curve replaces stepwise thresholds (eliminates cliff effects).
# boost = clamp((def_rtg - neutral) * slope, floor, cap)
DRTG_NEUTRAL_NBA: float = 110.0
DRTG_BOOST_PER_POINT_NBA: float = 0.008     # +0.8% per DRtg point above neutral
DRTG_PENALTY_PER_POINT_NBA: float = 0.006   # -0.6% per DRtg point below neutral
DRTG_BOOST_CAP_NBA: float = 0.08            # Max +8%
DRTG_PENALTY_FLOOR_NBA: float = -0.05       # Max -5%

DRTG_NEUTRAL_CBB: float = 104.0
DRTG_BOOST_PER_POINT_CBB: float = 0.007
DRTG_PENALTY_PER_POINT_CBB: float = 0.005
DRTG_BOOST_CAP_CBB: float = 0.08
DRTG_PENALTY_FLOOR_CBB: float = -0.05

# ============================================================================
# Lineup Optimizer — Continuous Pace Adjustment
# ============================================================================

# Linear pace curve replaces stepwise thresholds (eliminates cliff effects).
PACE_NEUTRAL_NBA: float = 100.0
PACE_BOOST_PER_UNIT_NBA: float = 0.008
PACE_PENALTY_PER_UNIT_NBA: float = 0.006
PACE_BOOST_CAP_NBA: float = 0.06
PACE_PENALTY_FLOOR_NBA: float = -0.05

PACE_NEUTRAL_CBB: float = 68.0
PACE_BOOST_PER_UNIT_CBB: float = 0.007
PACE_PENALTY_PER_UNIT_CBB: float = 0.005
PACE_BOOST_CAP_CBB: float = 0.06
PACE_PENALTY_FLOOR_CBB: float = -0.05

# ============================================================================
# Lineup Optimizer — Continuous Game Environment Boost
# ============================================================================

# Replaces stepwise +5%/+10% thresholds with a smooth linear curve.
# boost_pct = (game_total - baseline) * scale, clamped to [floor, cap].
GAME_TOTAL_BOOST_NBA_BASELINE: float = 220.0   # Neutral point (no boost)
GAME_TOTAL_BOOST_NBA_SCALE: float = 0.005      # +0.5% per point above baseline
GAME_TOTAL_BOOST_NBA_CAP: float = 0.15         # Max +15% (was +10%)
GAME_TOTAL_BOOST_NBA_FLOOR: float = -0.10      # Min -10%

GAME_TOTAL_BOOST_CBB_BASELINE: float = 145.0
GAME_TOTAL_BOOST_CBB_SCALE: float = 0.006      # +0.6% per point above baseline
GAME_TOTAL_BOOST_CBB_CAP: float = 0.15
GAME_TOTAL_BOOST_CBB_FLOOR: float = -0.10

# ============================================================================
# Lineup Optimizer — Showdown CPT Ceiling Optimization
# ============================================================================

# In showdown mode, the CPT slot multiplier magnifies upside.  The CPT
# candidate scoring should lean even more toward ceiling than standard GPP.
SHOWDOWN_CPT_CEILING_WEIGHT: float = 0.75   # 75% ceiling, 25% projection for CPT scoring
SHOWDOWN_CPT_FLOOR_PENALTY: float = 0.90    # 10% penalty for low-floor CPT candidates

# ============================================================================
# Rotation Engine — Competitive Context
# ============================================================================

# Win-percentage thresholds for competitive context classification.
COMPETITIVE_TANKING_WP: float = 0.30        # Below 30% → likely tanking
COMPETITIVE_CLINCHED_WP: float = 0.73       # Above 73% → likely clinched
# Minutes adjustments for each context.
COMPETITIVE_TANKING_STAR_MULT: float = 0.92   # Stars lose ~8% minutes in tank
COMPETITIVE_CLINCHED_STAR_MULT: float = 0.96  # Stars lose ~4% in clinched games
COMPETITIVE_PUSH_STAR_MULT: float = 1.03      # Stars gain ~3% in playoff push

# ============================================================================
# Lineup Optimizer — Correlation-Aware Greedy Fill
# ============================================================================

# During greedy fill of non-stack slots, candidates sharing a game with
# already-selected players receive a correlation-informed boost.  This
# creates "secondary correlations" that GPP theory rewards.
#
# Blend: effective_score = (1 - CORR_FILL_WEIGHT) * base_score
#                        + CORR_FILL_WEIGHT * (base_score * (1 + avg_corr * CORR_FILL_BOOST))
GREEDY_FILL_CORRELATION_WEIGHT: float = 0.30   # Fraction of score from correlation signal
GREEDY_FILL_CORRELATION_BOOST: float = 0.15    # Max boost per unit of avg correlation
GREEDY_FILL_SAME_GAME_BONUS: float = 0.03      # Small bonus for same-game even without corr data

# ============================================================================
# Lineup Optimizer — Portfolio-Aware Late Swap
# ============================================================================

# When performing late swaps across a multi-lineup portfolio, prefer diverse
# replacements over raw projection to avoid convergent lineups.
LATE_SWAP_DIVERSITY_WEIGHT: float = 0.25       # 25% of replacement score from diversity
LATE_SWAP_PROJECTION_WEIGHT: float = 0.75      # 75% from projection
LATE_SWAP_MAX_REPLACEMENT_EXPOSURE: float = 0.50  # Max fraction of lineups using same replacement

# ============================================================================
# Lineup Optimizer — Continuous Ownership Leverage Curve
# ============================================================================

# Power-law exponent for ownership leverage: multiplier = 1/(own/baseline)^alpha
# Higher alpha = stronger fade of high-owned players.
OWNERSHIP_LEVERAGE_ALPHA: float = 0.25  # Reduced from 0.45 — capped penalty model makes alpha gentler
OWNERSHIP_LEVERAGE_BASELINE: float = 12.0     # Ownership% where multiplier = 1.0

# Contrarian strategy uses stronger leverage (more aggressive fading)
OWNERSHIP_LEVERAGE_CONTRARIAN_ALPHA: float = 0.40  # Reduced from 0.70 to match capped model
OWNERSHIP_LEVERAGE_CONTRARIAN_BASELINE: float = 10.0

# Maximum ownership penalty cap — score can NEVER be reduced by more than this %.
# Ensures raw EV remains the primary driver of the objective function.
OWNERSHIP_MAX_PENALTY_PCT: float = 0.15  # 15% max penalty even for 50%+ owned players

# Sigmoid ownership penalty: Penalty = Max_Penalty / (1 + e^(-k * (own - midpoint)))
# Replaces power-law model. S-curve protects low/mid chalk from over-suppression.
OWNERSHIP_SIGMOID_K: float = 12.0            # Steepness of sigmoid curve
OWNERSHIP_SIGMOID_MIDPOINT: float = 0.40     # Ownership fraction where penalty = Max_Penalty/2
# Contrarian variant: steeper curve, earlier onset for more aggressive fading
OWNERSHIP_SIGMOID_CONTRARIAN_K: float = 15.0
OWNERSHIP_SIGMOID_CONTRARIAN_MIDPOINT: float = 0.30

# Value-Adjusted Ownership Dampener: high-value players bypass ownership fade.
# value_ratio = projected_fp / (salary / 1000).
# If value_ratio > slate dynamic threshold, penalty is exponentially dampened.
# The static thresholds are FALLBACKS when dynamic threshold isn't computed.
GOOD_CHALK_VALUE_THRESHOLD: float = 5.0   # FP/$1K fallback threshold (lowered from 6.0)
GOOD_CHALK_VALUE_CEILING: float = 7.0     # FP/$1K where penalty is fully zeroed (lowered from 8.0)
# Dynamic threshold percentile: use 90th percentile of slate value distribution
GOOD_CHALK_VALUE_PERCENTILE: float = 0.90

# ============================================================================
# Lineup Optimizer — Fade / Leverage Integration
# ============================================================================

# FadeService-identified fade candidates get score penalty;
# leverage candidates get a boost.  Scales with the fade/leverage score (0-1).
FADE_PENALTY_WEIGHT: float = 0.10             # score *= (1 - fade_score * weight)
LEVERAGE_BOOST_WEIGHT: float = 0.08           # score *= (1 + leverage_score * weight)

# ============================================================================
# Simulation Engine — Overtime Modeling
# ============================================================================

# Close games have ~6.5% overtime probability per NBA historical data.
# OT adds 25 team-minutes (5 players × 5 min OT period) and boosts
# team total by ~8 points.
OT_PROBABILITY_CLOSE_GAME: float = 0.065
OT_EXTRA_TEAM_MINUTES: float = 25.0          # 5 players × 5 min OT
OT_MAX_PLAYER_MINUTES: float = 53.0          # 48 regulation + 5 OT
OT_TOTAL_BOOST: float = 8.0                  # Extra team points in OT

# ============================================================================
# Lineup Optimizer — Correlation-Based Bring-Back Selection
# ============================================================================

# 3-component bring-back scoring: projection + ceiling + negative correlation.
# Negative cross-team correlations are preferred because they provide natural
# hedging (opponent QB throws → opponent stack ceilings rise when our team
# struggles, stabilising the lineup ceiling).
BRINGBACK_CEILING_WEIGHT: float = 0.25
BRINGBACK_NEGATIVE_CORR_WEIGHT: float = 0.40

# ============================================================================
# Vegas Line Movement Detection
# ============================================================================

# Minimum absolute movement to be considered "significant".
LINE_MOVEMENT_TOTAL_SIGNIFICANT: float = 2.0   # Total (over/under) delta threshold
LINE_MOVEMENT_SPREAD_SIGNIFICANT: float = 1.5  # Spread delta threshold

# Ceiling adjustments for significant movements.
# Total goes UP → players in that game get a ceiling boost.
# Total goes DOWN → players get a ceiling penalty.
LINE_MOVEMENT_CEILING_BOOST: float = 0.05   # +5% ceiling per significant move up
LINE_MOVEMENT_CEILING_PENALTY: float = 0.03 # -3% ceiling per significant move down

# ── Game Context Modifier (Line Movement Agent -> Rotation Engine) ──────────
# When the over/under exceeds this threshold, a minor pace multiplier is
# applied to all players in the game (higher totals ≈ more possessions).
GAME_CONTEXT_HIGH_TOTAL_THRESHOLD: float = 235.0
GAME_CONTEXT_HIGH_TOTAL_PACE_BOOST: float = 1.02   # 2% boost when O/U > 235

# ============================================================================
# Opponent Field Modeling — Archetype Ratios
# ============================================================================

# Real DFS fields have optimizer-driven clusters.  We model 3 archetypes:
# - Ownership-weighted random (current approach) — represents casual entrants
# - Chalk optimizer — greedy by value/$ ratio, represents "optimal" builders
# - Contrarian optimizer — greedy by ceiling×(1-own)/$ ratio
FIELD_ARCHETYPE_OWNERSHIP_RANDOM: float = 0.60
FIELD_ARCHETYPE_CHALK_OPTIMIZER: float = 0.20
FIELD_ARCHETYPE_CONTRARIAN_OPTIMIZER: float = 0.20

# ============================================================================
# Multi-Slot Swap Chains
# ============================================================================

# Maximum iterations for 2-slot swap improvement pass.
TWO_SLOT_SWAP_MAX_ITERATIONS: int = 20  # was 10 — 2x more trade-down-to-upgrade opportunities

# ============================================================================
# DFS Service — Shot-Type Decomposition
# ============================================================================

# Minimum game sample to trust a player's individual shooting profile.
# Below this threshold, fall back to league-average rates.
SHOT_DECOMP_MIN_GAMES: int = 10

# NBA league-average shooting percentages (2024-25 season approximations)
SHOT_DECOMP_LEAGUE_AVG_FG3_PCT: float = 0.362    # 3PT field-goal %
SHOT_DECOMP_LEAGUE_AVG_FG2_PCT: float = 0.529    # 2PT field-goal %
SHOT_DECOMP_LEAGUE_AVG_FT_PCT: float = 0.784     # Free-throw %

# ============================================================================
# Lineup Optimizer — Rules-Based News Pipeline
# ============================================================================

# Deterministic keyword-based adjustments applied BEFORE the AI agent.
# Works even when the AI agent is unavailable.  Keyed by headline keyword
# patterns (case-insensitive).
NEWS_RULES_DNP_KEYWORDS: list[str] = [
    "ruled out", "will not play", "out for", "sidelined",
    "will miss", "shut down", "season-ending", "dnp",
]
NEWS_RULES_GTD_KEYWORDS: list[str] = [
    "game-time decision", "questionable", "doubtful", "50-50",
]
NEWS_RULES_STARTER_KEYWORDS: list[str] = [
    "will start", "expected to start", "inserted into starting",
    "moving to the starting", "named starter",
]
NEWS_RULES_EXPANDED_ROLE_KEYWORDS: list[str] = [
    "expanded role", "increased minutes", "bigger role",
    "more involved", "usage increase", "feature more",
]
NEWS_RULES_REDUCED_ROLE_KEYWORDS: list[str] = [
    "minutes restriction", "minute limit", "load management",
    "coming off bench", "reduced role", "will come off the bench",
]
# Usage modifier caps for rules-based adjustments
NEWS_RULES_EXPANDED_USAGE_BOOST: float = 1.08  # +8% usage
NEWS_RULES_REDUCED_USAGE_CUT: float = 0.90     # -10% usage
NEWS_RULES_GTD_MINUTES_FACTOR: float = 0.92    # GTD typically play ~92% minutes

# ============================================================================
# NCAA Basketball (CBB) — Sport-Specific Constants
# ============================================================================
#
# College basketball differs from NBA in game length (40 vs 48 min),
# pace (~68 vs ~102 possessions), rotation depth, and schedule structure
# (no back-to-backs).  DFS scoring rules (DK & FD) are IDENTICAL to NBA.
#
# These constants are used when sport="cbb" is passed to the engine.

# ── Game structure ────────────────────────────────────────────────
CBB_TOTAL_TEAM_MINUTES: float = 200.0         # 5 players × 40 min game
CBB_ABSOLUTE_MAX_MINUTES: float = 40.0        # Full CBB game = 40 min
CBB_LEAGUE_AVG_PACE: float = 68.0             # NCAA D1 avg ~68 poss/game (vs NBA ~102)
CBB_REGULATION_MINUTES: float = 200.0         # For simulation engine

# ── Rotation thresholds ──────────────────────────────────────────
CBB_STARTER_THRESHOLD_MINUTES: float = 20.0   # Starters play ~28-32 in 40-min game
CBB_DEEP_BENCH_THRESHOLD_MINUTES: float = 10.0  # Bench cutoff (vs NBA 15)
CBB_STAR_ANCHOR_THRESHOLD: float = 22.0       # Protected star minutes (vs NBA 26)
CBB_ROTATION_SIZE_DEFAULT: int = 9            # Typical CBB rotation (vs NBA 10-11)

# ── Schedule & fatigue ───────────────────────────────────────────
CBB_B2B_EXISTS: bool = False                  # College teams rarely play back-to-back
CBB_B2B_STARTER_REDUCTION: float = 0.0        # N/A — no B2B in college
CBB_B2B_VETERAN_EXTRA: float = 0.0            # N/A

# ── Injury model (NCAA uses fewer status levels) ─────────────────
CBB_INJURY_PLAY_PROBABILITY: dict[str, float] = {
    "Out": 0.0,
    "Doubtful": 0.15,
    "Questionable": 0.80,
    "Game Time Decision": 0.70,
    "GTD": 0.70,
}
CBB_INJURY_MINUTES_IF_ACTIVE: dict[str, float] = {
    "Out": 0.0,
    "Doubtful": 0.70,
    "Questionable": 0.92,
    "Game Time Decision": 0.90,
    "GTD": 0.90,
}

# ── Bayesian shrinkage priors (per-minute rates for CBB) ─────────
# College players produce at lower per-minute rates than NBA due to
# slower pace and less efficient offensive execution.
CBB_POSITION_PRIOR_RATES: dict[str, dict[str, float]] = {
    "PG": {"PTS": 0.42, "REB": 0.10, "AST": 0.18, "STL": 0.035, "BLK": 0.008, "TOV": 0.090, "FG3M": 0.065},
    "SG": {"PTS": 0.43, "REB": 0.10, "AST": 0.10, "STL": 0.030, "BLK": 0.010, "TOV": 0.075, "FG3M": 0.070},
    "SF": {"PTS": 0.40, "REB": 0.15, "AST": 0.08, "STL": 0.025, "BLK": 0.015, "TOV": 0.065, "FG3M": 0.055},
    "PF": {"PTS": 0.38, "REB": 0.20, "AST": 0.06, "STL": 0.020, "BLK": 0.022, "TOV": 0.060, "FG3M": 0.035},
    "C":  {"PTS": 0.38, "REB": 0.25, "AST": 0.05, "STL": 0.018, "BLK": 0.035, "TOV": 0.055, "FG3M": 0.015},
    "G":  {"PTS": 0.42, "REB": 0.10, "AST": 0.14, "STL": 0.032, "BLK": 0.009, "TOV": 0.082, "FG3M": 0.067},
    "F":  {"PTS": 0.39, "REB": 0.17, "AST": 0.07, "STL": 0.022, "BLK": 0.018, "TOV": 0.062, "FG3M": 0.045},
}

# ── Pace sensitivity (CBB-specific calibration) ──────────────────
# College basketball has more pace variance between conferences.
CBB_PACE_SENSITIVITY: dict[str, float] = {
    "pts": 1.0,
    "ast": 1.0,
    "fg3m": 0.85,
    "tov": 0.80,
    "reb": 0.55,
    "stl": 0.30,
    "blk": 0.20,
}

# ── Team stats computation (Phase 2A) ─────────────────────────────
CBB_STATS_CACHE_TTL: int = 14400                # 4 hours
CBB_STATS_MIN_GAMES: int = 10                   # Full trust after N games
CBB_LEAGUE_AVG_OFF_RATING: float = 104.0        # pts per 100 possessions (D1 avg)
CBB_LEAGUE_AVG_DEF_RATING: float = 104.0        # pts allowed per 100 poss
CBB_LEAGUE_AVG_PPG: float = 73.0                # D1 average points per game

# Per-game league-average stat totals (used for DvP baseline)
CBB_LEAGUE_AVG_STATS_PG: dict[str, float] = {
    "pts": 73.0,
    "reb": 34.0,
    "ast": 14.5,
    "stl": 6.5,
    "blk": 3.2,
    "tov": 12.5,
    "fg3m": 7.5,
}

# ── Vegas blend weights (Phase 2B) ────────────────────────────────
CBB_VEGAS_TOTAL_BLEND_WEIGHT: float = 0.80      # 80% Vegas, 20% model
CBB_VEGAS_SPREAD_BLEND_WEIGHT: float = 0.75     # 75% Vegas, 25% model
ODDS_API_CACHE_TTL: int = 600                   # 10 minutes

# ── Lineup analysis thresholds (CBB FP ranges much lower than NBA) ────
CBB_LINEUP_PROJECTION_DK_FLOOR: float = 80.0   # 0/10 score
CBB_LINEUP_PROJECTION_DK_RANGE: float = 8.0    # (fp - 80) / 8.0 → 10 at 160 FP
CBB_LINEUP_PROJECTION_FD_FLOOR: float = 90.0
CBB_LINEUP_PROJECTION_FD_RANGE: float = 8.5    # 10 at ~175 FP
CBB_DEFAULT_GAME_TOTAL: float = 145.0           # vs NBA 220
CBB_HIGH_TOTAL_THRESHOLD: float = 155.0         # vs NBA 225

# ── Correlation & stacking ────────────────────────────────────────────
CBB_CORRELATION_MIN_GAMES: int = 3              # vs NBA 5 — CBB seasons shorter
CBB_STACKING_MAX_PER_TEAM: int = 3              # vs NBA 4 — fewer high-usage players
CBB_STACKABLE_GAME_TOTAL_DEFAULT: float = 145.0 # vs NBA 220

# ============================================================================
# DK → NBA Abbreviation Aliases
# ============================================================================
#
# DraftKings uses different team abbreviations from nba_api in several cases.
# This shared mapping is used by both game_service (slate game matching) and
# lineup_optimizer_service (player pool team resolution).
DK_TO_NBA_ABBR_ALIASES: dict[str, str] = {
    "PHO": "PHX",   # Phoenix Suns
    "SA":  "SAS",   # San Antonio Spurs
    "GS":  "GSW",   # Golden State Warriors
    "NY":  "NYK",   # New York Knicks
    "NO":  "NOP",   # New Orleans Pelicans
    "BK":  "BKN",   # Brooklyn Nets
    "CHO": "CHA",   # Charlotte Hornets
    "WSH": "WAS",   # Washington Wizards
}

# ============================================================================
# Pre-Lock Polling Service — Timing Configuration
# ============================================================================

# Activate high-frequency polling N minutes before any slate locks.
PRE_LOCK_WINDOW_MINUTES: int = 60

# Polling interval when inside the pre-lock window (seconds).
ACTIVE_POLL_INTERVAL_S: int = 120  # 2 minutes

# Polling interval when outside the pre-lock window (seconds).
DORMANT_POLL_INTERVAL_S: int = 600  # 10 minutes

# How often to re-fetch the DK slate schedule (minutes).
SLATE_SCHEDULE_REFRESH_MINUTES: int = 60

# ── Autonomous Late-Swap Execution ──────────────────────────────
# When a star player is scratched within this window of lock, the
# pre-lock polling service automatically patches saved lineups and
# exports a DK-ready CSV.
LATE_SWAP_AUTO_WINDOW_MINUTES: int = 30

# ── Sim-to-Optimal Leverage Ratio ────────────────────────────────
# Controls how strongly the ILP objective is influenced by the
# Monte Carlo optimal-lineup leverage ratio.
#
# leverage_ratio = optimal_pct / ownership_pct
# multiplier = 1.0 + SCALE × (ratio - 1.0), clamped to [FLOOR, CAP]
#
# Examples with default SCALE=0.08:
#   ratio=2.5 → mult = 1.0 + 0.08 × 1.5 = 1.12 (+12% score boost)
#   ratio=1.0 → mult = 1.0 + 0.08 × 0.0 = 1.00 (no change)
#   ratio=0.5 → mult = 1.0 + 0.08 × -0.5 = 0.96 (-4% penalty)
SIM_LEVERAGE_SCALE: float = 0.08   # Sensitivity per unit of leverage
SIM_LEVERAGE_CAP: float = 1.15     # Max boost (15% even for 5.0x leverage)
SIM_LEVERAGE_FLOOR: float = 0.90   # Max penalty (10% even for 0.0x leverage)

# ── Usage Cannibalization Penalties ──────────────────────────────
# When two high-usage teammates are paired, they compete for the
# same possessions — their combined output is less than the sum of
# their individual projections.  The ILP applies a penalty to the
# objective function for high-USG teammate pairs.
#
# Exception: A high-assist PG/SG paired with a high-assisted-FG% big
# is a POSITIVE correlation (the guard creates shots for the big).
CANNIBALIZATION_USG_THRESHOLD: float = 0.22       # 22% usage rate
CANNIBALIZATION_PENALTY_PER_PAIR: float = 2.5     # FP penalty per cannibalizing pair
CANNIBALIZATION_MAX_SAME_TEAM: int = 3            # Hard cap: max 3 players per team
CANNIBALIZATION_ASSIST_PERCENTILE: float = 5.5    # AST/game threshold (~85th pctile)
CANNIBALIZATION_ASSISTED_FG_PCT: float = 0.55     # 55% assisted FG% for bigs
CANNIBALIZATION_SYNERGY_BONUS: float = 1.5        # FP bonus for PG→Big assist synergy


# ============================================================================
# Simulation Engine — Cross-Team Correlation
# ============================================================================

# Master toggle — set to False to disable all cross-team effects.
CROSS_TEAM_CORRELATION_ENABLED: bool = True

# --- Effect 1: Shared Game Environment ---
# Both teams in the same game experience correlated scoring environments.
# sigma controls the magnitude of this shared factor (~4% scoring variance).
CROSS_TEAM_GAME_ENV_SIGMA: float = 0.04
CROSS_TEAM_GAME_ENV_STATS: list[str] = ["pts", "ast", "fg3m"]

# --- Effect 2: Defensive Depression ---
# When one team scores well (above median), the opponent's STL/BLK are
# depressed.  At 0.35, a 10% scoring spike → ~3.5% DEF depression.
CROSS_TEAM_DEF_SENSITIVITY: float = 0.35
CROSS_TEAM_DEF_CLAMP_MIN: float = 0.80   # max 20% depression
CROSS_TEAM_DEF_CLAMP_MAX: float = 1.10   # max 10% boost

# --- Effect 3: Rebound Coupling ---
# When one team shoots efficiently (high PTS), the opponent has fewer
# rebounding opportunities (fewer misses to rebound).
CROSS_TEAM_REB_SENSITIVITY: float = 0.15
CROSS_TEAM_REB_CLAMP_MIN: float = 0.90   # max 10% depression
CROSS_TEAM_REB_CLAMP_MAX: float = 1.05   # max 5% boost


def get_sport_constants(sport: str = "nba") -> dict:
    """Return sport-specific constant overrides.

    When ``sport == "cbb"``, returns a dict of constants that differ
    from the NBA defaults.  The caller merges these into its local
    namespace or uses them via dict lookup.

    When ``sport == "nba"`` (default), returns an empty dict — all
    module-level constants apply as-is.
    """
    if sport == "cbb":
        return {
            "TOTAL_TEAM_MINUTES": CBB_TOTAL_TEAM_MINUTES,
            "ABSOLUTE_MAX_MINUTES": CBB_ABSOLUTE_MAX_MINUTES,
            "LEAGUE_AVG_PACE": CBB_LEAGUE_AVG_PACE,
            "REGULATION_MINUTES": CBB_REGULATION_MINUTES,
            "STARTER_THRESHOLD_MINUTES": CBB_STARTER_THRESHOLD_MINUTES,
            "DEEP_BENCH_THRESHOLD_MINUTES": CBB_DEEP_BENCH_THRESHOLD_MINUTES,
            "STAR_ANCHOR_THRESHOLD": CBB_STAR_ANCHOR_THRESHOLD,
            "B2B_EXISTS": CBB_B2B_EXISTS,
            "B2B_STARTER_REDUCTION_MINUTES": CBB_B2B_STARTER_REDUCTION,
            "B2B_VETERAN_EXTRA_REDUCTION": CBB_B2B_VETERAN_EXTRA,
            "INJURY_PLAY_PROBABILITY": CBB_INJURY_PLAY_PROBABILITY,
            "INJURY_MINUTES_IF_ACTIVE": CBB_INJURY_MINUTES_IF_ACTIVE,
            "POSITION_PRIOR_RATES": CBB_POSITION_PRIOR_RATES,
            "PACE_SENSITIVITY": CBB_PACE_SENSITIVITY,
            "LINEUP_PROJECTION_DK_FLOOR": CBB_LINEUP_PROJECTION_DK_FLOOR,
            "LINEUP_PROJECTION_DK_RANGE": CBB_LINEUP_PROJECTION_DK_RANGE,
            "LINEUP_PROJECTION_FD_FLOOR": CBB_LINEUP_PROJECTION_FD_FLOOR,
            "LINEUP_PROJECTION_FD_RANGE": CBB_LINEUP_PROJECTION_FD_RANGE,
            "DEFAULT_GAME_TOTAL": CBB_DEFAULT_GAME_TOTAL,
            "HIGH_TOTAL_THRESHOLD": CBB_HIGH_TOTAL_THRESHOLD,
            "CORRELATION_MIN_GAMES": CBB_CORRELATION_MIN_GAMES,
            "STACKING_MAX_PER_TEAM": CBB_STACKING_MAX_PER_TEAM,
            "STACKABLE_GAME_TOTAL_DEFAULT": CBB_STACKABLE_GAME_TOTAL_DEFAULT,
        }
    return {}


# ============================================================================
# GPP Tournament Constraints (ILP Solver)
# ============================================================================
#
# Hard constraints added to the ILP when contest_type is "gpp" or
# "single_entry".  These enforce ownership leverage at the solver level
# (on top of the soft ownership-leverage scoring in _compute_composite_score).

# Max total projected ownership across all rostered players (e.g. 135 = 135%).
# Lower values force more contrarian builds.
GPP_OWNERSHIP_CAP: float = 135.0

# "Pivot" rule: at least N rostered players must have projected ownership
# below this threshold.  Ensures at least one differentiator per lineup.
GPP_PIVOT_OWNERSHIP_THRESHOLD: float = 10.0
GPP_PIVOT_MIN_COUNT: int = 1

# Ceiling objective blend: in GPP mode the ILP objective is
#   (1 - weight) * score_fn(p)  +  weight * ceiling_projection(p)
# where ceiling_projection = sim_p90 (if available) or ceiling_fp.
# Set to 0.0 to disable ceiling tilt; 1.0 for pure ceiling.
GPP_CEILING_WEIGHT: float = 0.15  # ILP C9a ceiling blend; 0.30 was too aggressive — caused ILP to trade projection for ceiling, producing lineups 15-20% below baseline

# Bring-back correlation rule: if the solver selects a high-salary
# "anchor" player from any game, it must also select at least one
# opponent from that same game_id.  This creates natural negative
# correlation (hedge) without relying on pre-selected stack games.
# Salary threshold: only players above this salary trigger the rule.
# Usage threshold: alternative trigger — players with projected usage
# above this rate also activate the bring-back requirement.
GPP_BRINGBACK_SALARY_THRESHOLD: int = 8500
GPP_BRINGBACK_USAGE_THRESHOLD: float = 0.27
# Set to False to disable the ILP bring-back constraint entirely.
GPP_BRINGBACK_ENABLED: bool = True

# Minimum salary utilization (percentage of salary cap).
# Lineups spending less than this % of the cap are penalised/excluded.
# Can be auto-tuned by the GPP post-mortem blueprint.
GPP_SALARY_FLOOR_PCT: float = 96.0

# ============================================================================
# GPP Improvement #1 — Vegas Implied Team Total Scaling
# ============================================================================
# Per-team implied total (derived from O/U and spread) scaling in composite
# score.  Complements the existing game_total boost (which uses total for
# both teams equally) by giving favorites higher implied totals than underdogs.
GPP_IMPLIED_TOTAL_BASELINE: float = 112.0     # League avg implied team total (~224/2)
GPP_IMPLIED_TOTAL_SCALE: float = 0.008        # +0.8% per point above baseline
GPP_IMPLIED_TOTAL_CAP: float = 0.12           # Max +12%
GPP_IMPLIED_TOTAL_FLOOR: float = -0.08        # Min -8%

# ============================================================================
# GPP Improvement #3 — Secondary Game Stack
# ============================================================================
# After the primary stack game (3+1), prefer a secondary mini-stack (2-man)
# from the highest-total remaining game.  Players from the secondary game
# get a scoring bonus to nudge the ILP toward structured 3+1+2 builds.
GPP_SECONDARY_STACK_BONUS: float = 0.04       # 4% scoring bonus for secondary game players
GPP_SECONDARY_STACK_MIN_SIZE: int = 2         # Minimum players from secondary game

# ============================================================================
# GPP Improvement #5 — Slate-Size Adaptive Parameters
# ============================================================================
# Slate size influences optimal strategy: small slates (2-3 games) are more
# correlated and need stronger stacking with less contrarian; large slates
# (7+ games) need more differentiation and contrarian leverage.
GPP_SLATE_SMALL_MAX_GAMES: int = 3            # ≤3 games = small slate
GPP_SLATE_LARGE_MIN_GAMES: int = 7            # ≥7 games = large slate
GPP_SLATE_SMALL_CEILING_MULT: float = 1.67    # Multiply ceiling weight (0.15 → 0.25)
GPP_SLATE_SMALL_ALPHA_MULT: float = 0.80      # Less contrarian on small slates
GPP_SLATE_LARGE_ALPHA_MULT: float = 1.20      # More contrarian on large slates
GPP_SLATE_LARGE_MAX_OVERLAP: int = 6          # Tighter diversity on large slates

# ============================================================================
# GPP Improvement #6 — Boom/Bust Variance Weighting
# ============================================================================
# Replaces flat sim_std bonus.  Players with high boom probability
# (ceiling >> projection) get uplift; consistent players get less.
GPP_BOOM_VARIANCE_SCALE: float = 0.08         # Scale of boom probability bonus
GPP_BOOM_BASELINE: float = 1.5                # Neutral boom ratio (ceiling/proj)

# ============================================================================
# GPP Improvement #7 — Contrarian Captain for Showdown
# ============================================================================
# Apply ownership leverage to CPT scores so chalk captains (20-40% owned)
# are penalized relative to contrarian captains (2-5% owned).
SHOWDOWN_CPT_OWNERSHIP_ALPHA: float = 0.40    # Power-law exponent for CPT ownership
SHOWDOWN_CPT_OWNERSHIP_BASELINE: float = 15.0 # Neutral ownership % for CPT

# ============================================================================
# GPP Improvement #8 — Cross-Game Affinity Scoring
# ============================================================================
# Players from above-average-total games get a bonus; below-average get a
# penalty.  Uses RELATIVE game total (vs slate average), not absolute.
GPP_GAME_AFFINITY_POWER: float = 1.5          # Exponent for relative game_total advantage
GPP_GAME_AFFINITY_CAP: float = 1.15           # Max +15% bonus from game affinity
GPP_GAME_AFFINITY_FLOOR: float = 0.90         # Min -10% penalty from game affinity

# ============================================================================
# GPP Salary-Value Efficiency Bonus
# ============================================================================
# Rewards players with high projected FP relative to salary cost.
# value_ratio = projected_fp / (salary / 1000).  Baseline = 5.0x (typical).
GPP_VALUE_BASELINE: float = 5.0     # FP per $1K salary where multiplier = 1.0
GPP_VALUE_SCALE: float = 0.03       # Sensitivity per unit above/below baseline (was 0.02, originally 0.04)
GPP_VALUE_CAP: float = 1.15         # Max +15% boost for extreme value plays (was 1.12, originally 1.20)
GPP_VALUE_FLOOR: float = 0.93       # Max -7% penalty for expensive low-value (was 0.96, originally 0.92)
