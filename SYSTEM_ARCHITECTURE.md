# RotationEngine: NBA DFS Lineup Generation System

## System Overview

RotationEngine is an NBA Daily Fantasy Sports (DFS) lineup optimizer built on FastAPI (Python 3.13) + React 18 + PostgreSQL + Redis. It projects player minutes under a strict 240-minute team constraint, converts those projections into fantasy point estimates, then constructs salary-cap-compliant lineups using integer linear programming (ILP) with game-theory-aware ownership leverage.

The system targets DraftKings Classic contests ($50,000 salary cap, 8 roster slots: PG/SG/SF/PF/C/G/F/UTIL) with support for FanDuel, Showdown, and college basketball (CBB).

---

## Architecture at a Glance

```
Data Sources                    Projection Pipeline              Optimization
-----------                    -------------------              ------------
NBA API (stats.nba.com)   -->  Rotation Engine (240-min)   -->  Player Pool Builder
DraftKings API            -->  DFS Service (stat rates)    -->  Monte Carlo Simulation
ESPN/NBA.com Injuries     -->  13 AI Agents (LLM)         -->  ILP Solver (PuLP/CBC)
DK Sportsbook Props       -->  Calibration Service (ML)   -->  Portfolio Selection
Expert Signals (Twitter)  -->  Game Context Engine         -->  Quality Assessment
News (ESPN/RotoWire)      -->                                   DK Entry Automation
Vegas Lines (NBA Live)    -->
```

---

## 1. Minutes Projection: The Rotation Engine

**File**: `backend/app/services/rotation_engine.py` (1322 lines)

The rotation engine is the foundation of the entire system. Every fantasy point projection starts with a minutes projection, because in the NBA, playing time is the single strongest predictor of fantasy output.

### 1.1 Baseline Projection (Step 1)

Each player's expected minutes start from a weighted blend of historical data:

- **75% season average** (stability anchor)
- **25% recent performance**, split as:
  - 60% EMA-5 (exponential moving average, last 5 games, alpha=0.6 -- captures hot streaks)
  - 40% EMA-10 (last 10 games, alpha=0.4 -- smoothed trend)

This 75/25 split matches industry-standard approaches (NumberFire, FantasyLabs). The CalibrationService can dynamically adjust these weights based on backtesting accuracy analysis.

Players with 5+ consecutive DNPs are auto-classified as "Out" regardless of the injury report, catching suspensions and coaching decisions that drop off the injury feeds.

### 1.2 Injury Redistribution (Step 2)

When a player is injured, their minutes don't disappear -- they redistribute to teammates under a conditional probability model:

| Status | P(plays) | E[minutes if plays] | Effective Factor |
|--------|----------|---------------------|-----------------|
| Out | 0.00 | 0.00 | 0.00 |
| Doubtful | 0.20 | 0.75 | 0.15 |
| GTD | 0.72 | 0.92 | 0.66 |
| Questionable | 0.85 | 0.95 | 0.81 |

Minutes freed by absences flow through a backup hierarchy:
- **Primary backup** (position match, highest baseline): receives 65% of freed minutes
- **Remaining same-position players**: split the other 35%
- **Role cap**: No player can exceed min(42 min absolute cap, 125% of their own baseline)
- **Usage boost**: When a high-usage player is out, beneficiaries receive per-minute stat rate boosts (capped at 15%)

AI Agent 3 (InjuryImpactAgent) analyzes second-order cascading effects: usage rate shifts, pace changes, defensive matchup impacts, and role reassignments beyond simple proportional redistribution.

### 1.3 Game Context Adjustments (Step 3)

Applied in sequence:

1. **Blowout risk**: When |spread| >= 7 points, starters lose minutes proportional to spread magnitude (1.5% per point beyond threshold, capped at 10% total). Bench players in blowouts receive a garbage-time quality discount on their per-minute production rates.

2. **Back-to-back fatigue**: Starters lose ~2 minutes on B2B games. Veterans (age >= 32) lose an additional minute. Multi-game trip fatigue applies when a team has played 3+ games in 6 days.

3. **Pace factor**: Stored on projections for downstream DFS stat scaling. Does not change minutes directly -- it scales per-minute production rates. NBA league average pace is ~102 possessions/48 min.

4. **Competitive context**: Derived from win-loss record. Tanking teams (win% < 0.30) rest stars (~8% reduction). Playoff-push teams increase star usage (~3% boost). Clinched teams ease up (~4% reduction).

### 1.4 Coach Profile Adjustments (Step 3.5)

All 30 NBA head coaches have a static profile with multipliers for starter/bench/star minutes and parameters for rotation depth, B2B penalty severity, and pace tendency. Coaching styles are classified as: HEAVY_MINUTES, BALANCED, DEEP_ROTATION, STAR_DEPENDENT, or DEVELOPMENTAL.

AI Agent 6 (CoachLearningAgent) runs daily to detect rotation pattern shifts from recent game data. Learned deltas override static profiles when the agent has high confidence (e.g., a coach switching from 8-man to 10-man rotation mid-season).

### 1.5 Star-Anchored Normalization (Step 4)

The final step enforces the hard constraint: **every NBA team gets exactly 240 minutes per game** (5 players x 48 minutes). CBB uses 200 minutes (5 x 40).

When projected minutes exceed 240:
1. **Pass 1**: Clamp stars (>= 26 min baseline) at their baseline -- they absorb coach inflation first
2. **Pass 2**: Non-star players absorb remaining excess via inverse-square elasticity (1/min^2), so bench players absorb far more than rotation players
3. **Pass 3**: Players compressed below the viable threshold (2 min) are zeroed out

When projected minutes fall below 240:
- Proportional inflation, capped at 125% of baseline and 42 min absolute maximum

A multi-pass residual correction redistributes any remaining delta using headroom-weighted allocation until the team hits exactly 240.

---

## 2. Fantasy Point Projections: The DFS Service

**File**: `backend/app/services/dfs_service.py`

Once minutes are projected, the DFS service converts them into per-stat projections and fantasy points.

### 2.1 Per-Stat Projection

For each of 7 stat categories (PTS, REB, AST, STL, BLK, TOV, FG3M):

```
projected_stat = projected_minutes x per_minute_rate x dvp_factor x pace_factor
```

- **Per-minute rates**: Computed from game logs (season + recent blend)
- **DvP (Defense vs Position)**: Opponent's allowed stats relative to league average. Per-stat sensitivity constants control how much DvP matters (e.g., PTS is highly DvP-sensitive, BLK is less so).
- **Pace factor**: Game-level pace estimate relative to league average, with per-stat sensitivity weights (PTS/AST scale at 1.0x, REB at 0.6x, STL at 0.3x, BLK at 0.2x).

### 2.2 Shot-Type Decomposition

Points are decomposed into shot types for more accurate projection:
- 3PA rate, FGA rate, FTA rate (per minute)
- 3P%, 2P%, FT% (shooting percentages)
- Points = FG3M x 3 + FG2M x 2 + FTM x 1

### 2.3 DraftKings Scoring Formula

```
DK_FP = PTS x 1.0 + REB x 1.25 + AST x 1.5 + STL x 2.0 + BLK x 2.0 - TOV x 0.5 + FG3M x 0.5
      + (1.5 if double-double) + (3.0 if triple-double)
```

Double-double and triple-double probabilities are estimated via normal CDF approximation across all stat pairs.

### 2.4 Floor and Ceiling

- **Floor**: 90% of projected minutes x 75% of per-minute rates x DvP floor sensitivity
- **Ceiling**: 110% of projected minutes x 130% of per-minute rates x DvP ceiling sensitivity

The asymmetric rate multipliers (75% floor vs 130% ceiling) reflect real NBA variance: bad games cluster near a floor, but breakout games have a long right tail.

### 2.5 Market Calibration

When DraftKings Sportsbook prop lines are available:
- If our projection diverges > 15% from the market, blend toward the market (70% model / 30% market)
- DK's own FPPG (fantasy points per game) is blended asymmetrically: only blend downward when we project higher than DK (85% model / 15% DK FPPG), never blend upward

---

## 3. Monte Carlo Simulation Engine

**File**: `backend/app/services/simulation_engine.py`

The simulation engine runs 1,000 iterations per game to generate probability distributions for each player's fantasy output.

### 3.1 Noise Model

Each simulation iteration samples from correlated multivariate noise:
- Per-player, per-stat noise sigma values (default ~0.15-0.25, customizable by AI Agent 8)
- Within-team correlation (teammates' stats are positively correlated -- a high-scoring game benefits everyone)
- Cross-team negative correlation (opposing team's stats are inversely correlated with the winning team)

### 3.2 Game Script Scenarios

Each iteration randomly selects from 5 game-script scenarios:
- **Blowout win**: Starters play fewer minutes, bench gets expanded time
- **Blowout loss**: Similar pattern but with different usage distribution
- **Close game**: Starters play heavy minutes, bench shrinks
- **Shootout**: High pace, elevated scoring rates for everyone
- **Grind**: Low pace, elevated rebounding and defensive stats

Each scenario has multipliers for starter minutes, bench minutes, and pace.

### 3.3 Overtime Modeling

A small probability (~5%) of overtime adds 5 extra minutes, disproportionately benefiting starters.

### 3.4 Output

Per-player simulation produces:
- **P10**: 10th percentile (floor scenario)
- **P50**: Median projection
- **P90**: 90th percentile (ceiling scenario)
- **Standard deviation**: Volatility measure

These feed directly into the optimizer's strategy selection: ceiling strategies weight P90, floor strategies weight P10, balanced strategies weight P50.

---

## 4. Lineup Optimization Pipeline

**File**: `backend/app/services/lineup_optimizer_service.py`

### 4.1 Player Pool Construction

The pool builder merges data from multiple sources into a unified `PlayerPoolEntry` per player:

1. **DraftKings draftables**: Salary, position eligibility, DK player ID, team
2. **Rotation engine**: Projected minutes, baseline, confidence
3. **DFS service**: Projected stats, DK fantasy points, floor/ceiling
4. **Name matching**: Normalized matching (strip Jr./III, transliterate accents) with team-constrained fuzzy fallback

Players are filtered out if: marked Out/Doubtful, zero projected FP, two-way player with < 5 games, or DK status is "O" (Out).

### 4.2 Pool Enrichment (3-Tier Parallel Architecture)

Before optimization, the pool is enriched with contextual signals:

**Tier 1** (6 parallel workers):
- Game context (pace, total, spread, B2B detection)
- Expert signals (Twitter/web sentiment, signal count, confidence boost)
- AI simulation tuning (per-player noise profiles from Agent 8)
- News-based adjustments (minutes overrides, usage modifiers)
- DK Sportsbook prop lines (bullish/bearish/aligned signals)
- DK FPPG blend (asymmetric downward-only adjustment)

**Tier 2** (parallel):
- Monte Carlo simulation (1K iterations, 6 workers, 30-second time budget)
- AI ownership projection (Agent 7, launched concurrently)

**Tier 3** (sequential, depends on Tier 2):
- AI strategy adjustments (Agent 2: game-theory-aware score modifiers based on ownership landscape)

### 4.3 Ownership Projection

A rules-based softmax model with 12 attractiveness factors:
- Value efficiency, salary, game environment (total/pace), expert consensus, projection strength, star premium, positional scarcity, minutes certainty, B2B discount, spread impact, multi-position eligibility, injury-beneficiary boost

Each factor's weight can be dynamically adjusted by the CalibrationService's tournament learning loop (clamped to +/-30% from defaults).

AI Agent 7 (OwnershipProjectionAgent) provides a separate LLM-based estimate. The final ownership is a blend of rules-based and AI estimates.

### 4.4 Composite Scoring

Each player receives a composite score that the optimizer maximizes. The scoring function is strategy-aware:

| Strategy | Projection Weight | Ceiling Weight | Ownership Leverage |
|----------|------------------|----------------|-------------------|
| pure_max | 1.0 | 0.0 | None |
| max_projection | 0.65 | 0.10 | Light |
| balanced | 0.45 | 0.20 | Moderate |
| ceiling | 0.30 | 0.35 | Moderate |
| contrarian | 0.25 | 0.25 | Heavy |

**Ownership leverage** uses a continuous power-law curve:

```
leverage_score = 1.0 + alpha * (baseline / (ownership + baseline))
```

Where alpha=0.60 and baseline=10.0 for standard mode; alpha=0.85 and baseline=8.0 for contrarian. This smoothly boosts low-ownership players without creating cliff effects.

### 4.5 Game Stacking (GPP Mode)

For tournament (GPP) lineups, the optimizer builds correlated stacks:

1. **Game selection**: Weighted random selection favoring high-total, close-spread games (these have the most fantasy upside variance)
2. **Primary stack (2-3 players)**: Selected from one team in the target game using a blend of projection strength (35%) and pairwise Pearson correlation (65%). Minimum average correlation threshold of 0.20 for 3-man stacks.
3. **Bring-back (1 player)**: Selected from the opposing team. Uses negative correlation weighting to hedge -- a player whose ceiling scenarios inversely correlate with the primary stack provides portfolio diversification.
4. **Stack ratios**: 40% 2-man stacks, 60% 3-man stacks, 70% include a bring-back (all adjustable by CalibrationService)

The correlation data comes from `CorrelationService`, which computes historical Pearson correlations between all teammate and opponent pairs from game logs.

### 4.6 ILP Solver (PuLP/CBC)

The core optimizer uses integer linear programming via PuLP with the CBC solver:

**Decision variables**: Binary (0/1) for each player -- include in lineup or not.

**Objective**: Maximize sum of (composite_score[i] * x[i]) for all players i.

**Constraints**:
- Salary cap: sum(salary[i] * x[i]) <= 50,000
- Salary floor: sum(salary[i] * x[i]) >= 49,000 (98% utilization target)
- Exactly 8 players selected
- Per-position slot limits (1 PG, 1 SG, 1 SF, 1 PF, 1 C, 1 G, 1 F, 1 UTIL)
- Position eligibility: each player can only fill slots matching their eligible positions
- Locked players forced in (x[i] = 1)
- Excluded players forced out (x[i] = 0)
- Game stack constraints (when stacking enabled)
- Per-player exposure limits (across multi-lineup generation)

**Solver settings**: CBC with 5-second timeout. Falls back to greedy construction + iterative swap improvement if ILP fails.

### 4.7 Multi-Lineup Generation (Overgenerate-Then-Filter)

For generating multiple lineups:

1. **Overgeneration**: Generate 1.5-3x the requested count (up to 450 candidates). Each candidate uses noise-injected composite scores to create diversity.
2. **Exposure dampening**: During overgeneration, player exposure limits are reduced (0.3-0.5x) to prevent the same players from appearing in every lineup.
3. **Quality gate**: Each candidate must use >= 88% of salary cap and include >= 2 different teams. Failing candidates are retried with fresh noise.
4. **Scoring**: All candidates are scored on their un-noised composite values.
5. **Portfolio selection**: The top N lineups are selected subject to a maximum overlap constraint (default: 4 shared players between any two lineups). This uses a greedy selection algorithm that penalizes game-stack correlation (max 60% pairwise correlation, with a 6% score penalty for overlap).

### 4.8 Quality Assessment

Every generated lineup receives a quality score and letter grade (A+ through D) based on:
- Total projected fantasy points relative to optimal
- Salary utilization (penalizes unused cap)
- Team diversity (minimum 2 teams)
- Ceiling upside (P90 potential)
- Floor safety (P10 downside)

---

## 5. The 13 AI Agents

The system uses 13 specialized AI agents, each targeting a specific analytical task. Most use Claude Haiku or GPT-4o-mini for high-volume extraction tasks, with Claude Sonnet or GPT-4o for complex reasoning tasks.

| # | Agent | Purpose | Model Tier |
|---|-------|---------|------------|
| 1 | SignalAnalysisAgent | NLP on expert tweets: sarcasm detection, conditional statements, stat predictions | Default (Haiku) |
| 2 | LineupStrategyAgent | Game-theory lineup strategy: ownership leverage, stack recommendations, contest-type adjustments | Reasoning (Sonnet) |
| 3 | InjuryImpactAgent | Cascading injury effects: usage shifts, pace changes, role reassignments | Reasoning (Sonnet) |
| 4 | NarrativeAgent | Human-readable prose analysis from structured lineup grades | Reasoning (Sonnet) |
| 5 | NewsProjectionAgent | Extract actionable projection adjustments from news items | Default (Haiku) |
| 6 | CoachLearningAgent | Detect coaching rotation pattern shifts from recent game data | Reasoning (Sonnet) |
| 7 | OwnershipProjectionAgent | Estimate DFS ownership percentages per player | Default (Haiku) |
| 8 | SimulationTuningAgent | Per-player Monte Carlo noise sigma profiles | Default (Haiku) |
| 9 | BacktestingAgent | Analyse historical accuracy, generate calibration multipliers | Reasoning (Sonnet) |
| 10 | ExpertQualityAgent | Track and score expert source accuracy over time | Default (Haiku) |
| 11 | ChatAgent | Conversational DFS assistant with structured action dispatch | Reasoning (Sonnet) |
| 12 | TournamentAnalysisAgent | Analyse winning tournament lineups for pattern extraction | Reasoning (Sonnet) |
| 13 | LineMovementAgent | Vegas line movement analysis (rule-based, always available) | None (deterministic) |

---

## 6. ML Feedback Loop: The Calibration Service

**File**: `backend/app/services/calibration_service.py`

The system learns from its own mistakes and from tournament winners through two feedback channels:

### 6.1 Backtest Calibrations (Nightly)

A nightly pipeline (3 AM ET via APScheduler):
1. Ingest yesterday's box scores into `PlayerMinutesHistory` table
2. Compare projected vs actual for all stats (MAE, RMSE, bias)
3. AI Agent 9 interprets patterns and generates calibration multipliers
4. Calibrations saved to PostgreSQL, auto-expire after 14 days

Produces adjustments like: position-level projection biases, per-stat rate corrections, DvP sensitivity tuning, pace sensitivity adjustments, salary-tier projection corrections, B2B impact recalibration.

### 6.2 Tournament Calibrations (User-Triggered)

When the user imports DraftKings contest CSV exports:
1. AI Agent 12 analyses winning lineups (top 1%) vs the field
2. Identifies patterns in salary allocation, stacking, ownership leverage, positional preferences
3. Generates calibration adjustments saved to `TournamentCalibration` table (30-day expiry)
4. Tournament calibrations take precedence over backtest calibrations when keys overlap

Produces adjustments like: stacking weight ratios (2-man vs 3-man vs bring-back), ownership model factor weights, salary tier FP corrections, game context multipliers.

### 6.3 Safety Mechanisms

All learned adjustments are clamped to +/-15% (multiplier range 0.85 to 1.15). Values outside 0.5-2.0 are treated as corrupt and ignored. Individual calibrations can be rolled back via API endpoint. Full audit history is maintained.

### 6.4 Where Calibrations Apply

The CalibrationService injects learned adjustments into:
- **Rotation Engine**: Minutes blend weights, position bias corrections
- **DFS Service**: Per-stat rate adjustments, DvP sensitivity, pace sensitivity
- **Lineup Optimizer**: Salary tier FP corrections, stack parameters, ownership leverage curve
- **Simulation Engine**: Per-stat noise sigma multipliers
- **Ownership Model**: Factor weight adjustments

---

## 7. Caching Architecture

Four layers prevent redundant computation and API calls:

1. **In-memory** (threading.Lock): Player pool (30 min), DK slates (5 min), injuries (5 min), news (10 min), props (15 min)
2. **File-based JSON**: Player pool (2 hr) -- survives server restarts
3. **Redis**: General-purpose caching, session management
4. **PostgreSQL**: Team stats, game logs -- nightly refresh, persistent fallback when NBA API is unavailable

Cache invalidation is driven by injury hash changes: when any player's injury status changes, all affected slate caches are busted.

---

## 8. DraftKings Integration

Five services handle the DK data pipeline:

- **DKSlateService**: Fetches draft group structure from DK lobby API, identifies Early/Main/Night slates
- **DKDraftablesService**: Player salaries, position eligibility, DK player IDs. Name normalization handles Jr./III suffixes and accented characters.
- **DKPropsService**: Sportsbook player prop O/U lines with vig-removed probabilities
- **DKContestDetailService**: Contest metadata (entry count, prize pool, overlay risk, ROI breakeven)
- **DK Entry Automation**: Playwright browser automation for downloading entry templates, filling with optimized lineups, and uploading back to DK -- all SSE-streamed for real-time progress

---

## 9. Data Flow Summary

```
1. DATA INGESTION
   NBA API -> team stats, rosters, game logs, scoreboard
   DraftKings -> salaries, slates, props, contests
   ESPN/NBA.com/BBRef -> injuries (3-source waterfall)
   ESPN/NBA.com/RotoWire -> news
   Twitter/web -> expert signals

2. ROTATION PROJECTION (per team, per game)
   Baseline (75% season + 25% recent EMA) -> Injury redistribution -> Game context
   -> Coach adjustments -> Star-anchored 240-min normalization

3. DFS PROJECTION (per player)
   Projected minutes x per-minute stat rates x DvP x pace -> DK fantasy points
   + floor/ceiling + props calibration + DD/TD probability

4. POOL ENRICHMENT (3-tier parallel)
   Game context + Expert signals + Sim tuning + News + Props + FPPG
   -> Monte Carlo simulation (1K iterations) + Ownership projection
   -> AI strategy adjustments

5. LINEUP OPTIMIZATION
   Strategy-aware composite scoring -> Game stacking (correlation-weighted)
   -> ILP solver (PuLP/CBC, 5s timeout) -> Quality gate
   -> Overgenerate candidates -> Portfolio selection (diversity + exposure)

6. OUTPUT
   Optimized lineups with grades + DK entry automation
```

---

## 10. Key Constants Reference

| Constant | Value | Purpose |
|----------|-------|---------|
| TOTAL_TEAM_MINUTES | 240.0 | Hard constraint: 5 players x 48 minutes |
| BASELINE_SEASON_WEIGHT | 0.75 | Season average weight in baseline blend |
| EMA_ALPHA_5_GAME | 0.6 | 5-game EMA smoothing factor |
| STAR_ANCHOR_THRESHOLD | 26.0 | Minutes threshold for star protection during compression |
| ABSOLUTE_MAX_MINUTES | 42.0 | No player can exceed this |
| BLOWOUT_SPREAD_THRESHOLD | 7.0 | Spread triggering blowout adjustments |
| B2B_STARTER_REDUCTION | 2.0 | Minutes lost for starters on back-to-backs |
| USAGE_BOOST_CAP | 1.15 | Max 15% usage rate increase from teammate injury |
| ROLE_CAP_MULTIPLIER | 1.25 | Max inflation of any player's minutes from injury redistribution |
| VEGAS_TOTAL_BLEND_WEIGHT | 0.80 | 80% Vegas / 20% model for game totals |
| OWNERSHIP_LEVERAGE_ALPHA | 0.60 | Power-law ownership leverage curve steepness |
| STACK_CORRELATION_WEIGHT | 0.65 | Weight of pairwise correlation in stack partner selection |
| PROPS_PROJECTION_BLEND | 0.70 | 70% model / 30% market when projections diverge |
| DK_FPPG_BLEND_WEIGHT | 0.85 | 85% model / 15% DK FPPG (downward only) |
| FLOOR_RATE_MULT | 0.75 | Stat rate multiplier for floor scenarios |
| CEILING_RATE_MULT | 1.30 | Stat rate multiplier for ceiling scenarios |
| PORTFOLIO_MAX_CORRELATION | 0.60 | Max game-stack overlap between portfolio lineups |
| LINEUP_QUALITY_MIN_SALARY | 0.88 | Minimum salary utilization (88% of cap) |

---

## 11. API Surface (50+ Endpoints)

### Lineup Operations
- `GET /player-pool` -- Full player pool with projections + salaries
- `POST /generate-lineups` -- Multi-lineup generation with strategy controls
- `POST /optimize-lineup` -- Single lineup optimization
- `POST /analyze-lineups` -- Structured analysis with swap suggestions
- `POST /late-swap` -- Injury-triggered replacement suggestions

### Team & Rotation
- `GET /teams/{id}/rotation` -- Full rotation projection
- `GET /scoreboard` -- Schedule with game projections

### DraftKings
- `POST /dk-entries/auto` -- Full automated download/fill/upload

### Tournament Learning
- `POST /tournament/import` -- Upload contest CSV
- `GET /tournament/analysis` -- AI pattern analysis
- `GET /tournament/calibrations` -- View learned adjustments

### Simulation & Accuracy
- `GET /games/{id}/simulate` -- Monte Carlo simulation (10K iterations)
- `GET /accuracy/summary` -- MAE, RMSE, bias metrics
