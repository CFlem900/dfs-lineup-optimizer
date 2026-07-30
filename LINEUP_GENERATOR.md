# Lineup Generator — Technical Documentation

## Overview

The lineup generator is the core engine of RotationEngine, a DFS (Daily Fantasy Sports) optimizer for DraftKings and FanDuel. It produces salary-cap-compliant lineups by integrating minutes projections, fantasy point scoring, Monte Carlo simulation, AI agent analysis, and mathematical optimization.

The system supports **NBA** (classic + showdown) and **CBB** (college basketball) contests across both DraftKings ($50K cap, 8 slots) and FanDuel ($60K cap, 9 slots).

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        API Layer                                │
│  /player-pool  /optimize-lineup  /generate-lineups  /analyze    │
└───────────────────────────┬─────────────────────────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────────────┐
│              LineupOptimizerService (5,689 lines)                │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌────────────────┐  │
│  │ Pool     │→ │ Enrich   │→ │ Optimize │→ │ Select + Grade │  │
│  │ Building │  │ (3-tier) │  │ (ILP +   │  │ (Diversity +   │  │
│  │          │  │          │  │  Greedy)  │  │  Quality)      │  │
│  └──────────┘  └──────────┘  └──────────┘  └────────────────┘  │
└────────┬─────────────┬──────────────┬───────────────────────────┘
         │             │              │
┌────────▼──┐  ┌───────▼────┐  ┌──────▼───────┐
│ Data Layer│  │ AI Agents  │  │ Solver Layer │
│           │  │ (14 total) │  │              │
│ Rotation  │  │ Ownership  │  │ PuLP/CBC ILP │
│ Engine    │  │ Strategy   │  │ Greedy+Swap  │
│ DFS Svc   │  │ News       │  │ Portfolio    │
│ Sim Engine│  │ SimTuning  │  │ Selection    │
│ DK Slates │  │ Narrative  │  │              │
│ Injuries  │  │ ...        │  │              │
└───────────┘  └────────────┘  └──────────────┘
```

---

## Pipeline Stages

### Stage 1: Player Pool Building (`build_player_pool`)

Constructs the universe of eligible players for a slate by merging DraftKings salary/position data with RotationEngine's minutes and fantasy point projections.

**Input**: Platform (dk/fd), DraftGroup ID, game date
**Output**: `List[PlayerPoolEntry]` — 60-120 players with salary, projections, and slot eligibility

#### Process

1. **Fetch DK Draftables** — salary, position, DK player ID, injury status
2. **Group by Team** — each team processed independently
3. **Parallel Team Processing** (up to 8 workers for NBA, 1 for CBB):
   - `NBAApiService.build_team_rotation()` — fetches roster with per-minute stat rates
   - `RotationEngine.project_team_rotation()` — calculates baseline + adjusted minutes
   - `DFSService.project_team_dfs()` — converts minutes to fantasy points
4. **Name Matching** — maps DK display names to NBA API player names (handles suffixes, periods, hyphens)
5. **Filtering** — removes Out/Doubtful players, two-way/G-League players (< 3 games + minimum salary)
6. **Deduplication** — players listed under multiple positions get merged (slots combined, best projection kept)
7. **Ownership Projection** — rules-based model assigns estimated ownership to every player
8. **Caching** — result stored in memory (30 min TTL) + file cache (2 hr TTL)

#### Caching Architecture

```
Request → Memory Cache → File Cache → Build from Scratch
           (30 min)      (2 hours)
```

- **Injury-hash invalidation**: a SHA hash of injury statuses for slate teams is computed; if it changes, all caches for that slate are busted
- **Per-slate build locks**: prevents duplicate builds when prewarm daemon and user request hit simultaneously
- **Thread-safe**: all cache access protected by `threading.Lock`

#### Key Filters

| Filter | Threshold | Purpose |
|--------|-----------|---------|
| Minimum games played | 3 | Excludes unreliable two-way players |
| DK salary gate | $3,600 | Low-game players above min salary are kept (new signings) |
| Zero FP safety | > 0 FP | Never include players with no projected production |
| DK status fallback | "O"/"D" | If injury service missed a player, DK's own status field is authoritative |

---

### Stage 2: Pool Enrichment (`_enrich_pool`)

Enriches the base pool with simulation data, expert signals, game context, DK sportsbook props, and AI-driven adjustments. Organized into **three parallel tiers** for speed.

#### Tier 1 — Independent Data Fetching (parallel, 6 workers)

| Task | Source | Data Attached |
|------|--------|---------------|
| Game context | GameService schedule | `game_pace`, `game_total`, `opponent_def_rating`, `is_b2b`, `game_id` |
| Expert signals | ExpertSignalService | `expert_sentiment`, `expert_signal_count`, `expert_confidence_boost` |
| Sim tuning | Agent 8 (SimulationTuning) | Per-player noise profiles for Monte Carlo |
| News fetch | NewsService + Agent 4 | Rules-based + AI-driven projection adjustments |
| DK props | DKPropsService | `props_pts_line`, `props_reb_line`, `props_ast_line`, `props_signal` |
| DK FPPG | DKAvailablePlayersService | `dk_fppg`, `dk_fppg_delta` (blend toward consensus) |

#### Tier 2 — Simulation + Ownership (concurrent)

- **Monte Carlo Simulation** (1,000 iterations per game):
  - Runs in parallel across games (6 workers), with a 30-second time budget
  - Produces `sim_p10`, `sim_p50`, `sim_p90`, `sim_std` per player
  - Uses position-specific stat correlation matrices (Guard / Wing / Big)
  - Applies game-script scenarios (blowout, competitive, OT) with weighted probabilities
  - DvP matchup factors add variance to stat multipliers

- **Ownership Projection** (Agent 7):
  - LLM-based ownership estimates for the top 50 players
  - 15-second timeout (runs alongside simulation)
  - Results override the rules-based ownership from Stage 1

#### Tier 3 — Strategy Adjustments

- **Strategy Agent** (Agent 2): AI-driven per-player score modifiers based on contest type and field ownership
- Runs after ownership is applied (strategy depends on ownership data)

#### News Pipeline (Two-Phase)

1. **Rules-based** (deterministic, no AI required):
   - DNP keywords → zero minutes
   - GTD keywords → reduced minutes factor
   - Starter keywords → boost to 28+ min (NBA) / 24+ min (CBB)
   - Expanded/reduced role → usage multipliers
2. **AI refinement** (Agent 4): can override rules-based adjustments with context-aware analysis

#### DK FPPG Consensus Blending

When our projection diverges significantly from DK's FPPG (season average):
- **Asymmetric blending**: only blend when we project LOWER than DK
- When we project higher with game-specific context, our edge is preserved
- Divergence threshold and blend weight are configurable constants

---

### Stage 3: Optimization

Two solver paths are available; the system tries ILP first and falls back to greedy.

#### ILP Solver (PuLP/CBC) — Primary Path

Formulates lineup construction as a Binary Integer Linear Program:

- **Decision variables**: `x[player_id, slot]` = 1 if player assigned to slot
- **Objective**: maximize `SUM(score_fn(player) * x[player, slot])`
- **Constraints**:
  - C1: `SUM(salary * x) <= salary_cap` (salary cap)
  - C2: Each slot filled exactly once
  - C3: Each player used at most once
  - C5: Locked players must appear (user locks)
  - C7: `SUM(salary * x) >= salary_floor` (optional minimum salary usage)
  - C8: Stacking constraints — minimum players from primary team + optional bring-back
- **Solver**: CBC with 5-second timeout
- **Showdown CPT**: Captain slot uses ceiling-weighted scoring (75% ceiling + 25% projection) × 1.5x multiplier

#### Greedy + Swap — Fallback Path

When ILP is unavailable or fails:

1. **Lock Assignment** — pre-assign user-locked and stack-locked players
2. **Greedy Fill** — most-constrained-first slot filling:
   - For each slot, find eligible players within salary budget
   - Budget = remaining salary minus minimum salary needed for future slots
   - Correlation-aware scoring boosts candidates sharing a game with selected players
3. **Single-Slot Swap** (up to 100 iterations) — for each slot, try replacing the current player with a better-scoring eligible alternative within salary cap
4. **Two-Slot Swap** (up to 50 iterations) — simultaneously replace two slots to find "trade down at PG to fund upgrade at C" improvements
5. **Salary Floor Enforcement** — if total salary is below the floor, swap low-salary players for higher-salary alternatives

---

### Stage 4: Multi-Lineup Generation (`generate_lineups`)

The core public method for producing a portfolio of diverse lineups. Uses an **overgenerate-then-filter** pipeline.

#### Phase 0: Build & Enrich Pool
Same as Stages 1-2, with enrichment caching (30 min TTL).

#### Phase 1: Overgeneration Target

| Request Size | Multiplier | Example |
|-------------|------------|---------|
| 1-20 lineups | 3x | 10 requested → 30 candidates |
| 21-80 lineups | 2x | 50 requested → 100 candidates |
| 81-150 lineups | 1.5x | 100 requested → 150 candidates |

- Minimum: 6 candidates even for single-lineup requests
- Maximum: 450 candidates (hard cap)

#### Phase 2: Candidate Generation

For each candidate:

1. **Build scoring function** with strategy-specific logic and noise
2. **Select stacking target** — weighted random game selection (high-total games preferred)
3. **Dynamic stack parameters** from tournament-learned calibrations (2-man vs 3-man ratio, bring-back rate)
4. **Build lineup** via ILP or greedy path
5. **Quality gate** — reject candidates failing structural checks (salary floor, team diversity, player count)
6. **Track exposure** — count per-player appearances across candidates

Exposure is dampened during overgeneration (0.3-0.5x) so later candidates aren't crippled.

#### Phase 3: Scoring & Quality Floor

Each candidate is scored holistically:

| Component | Weight | GPP | Cash |
|-----------|--------|-----|------|
| Projected FP | varies | 50% proj + 50% secondary | 60% proj + 40% secondary |
| Secondary metric | varies | ceiling FP | floor FP |
| Salary efficiency | multiplicative | ≥95% usage → 1.0x | same |
| Ownership leverage | multiplicative | low avg own → 1.08x | N/A |
| Game stacking quality | multiplicative | 3-man stack → 1.08x | N/A |
| Correlation quality | multiplicative | high avg corr → 1.06x | N/A |

Candidates below a relative quality floor (percentage of best score) are dropped.

#### Phase 4: Portfolio Selection

Two approaches, tried in order:

1. **Portfolio ILP** (when conditions met: GPP, enough lineups, PuLP available):
   - Binary ILP maximizing total portfolio score
   - Per-player exposure constraints
   - Hard overlap constraints (max shared players between any two lineups)
   - Soft diversity penalty for pairwise overlap

2. **Greedy Diverse Selection** (fallback):
   - **Hard constraint**: player overlap — reject if > `max_overlap` shared players with any selected lineup
   - **Soft penalty**: game-stack correlation — cosine similarity between game-exposure vectors
   - Remaining candidates re-scored after each selection

If strict diversity can't fill the request, a relaxed pass runs with `max_overlap + 1`.

#### Phase 5: Quality Grading

Each selected lineup receives a quality score (0-100) and letter grade:

| Component | Weight | Description |
|-----------|--------|-------------|
| Salary efficiency | 25% | ≥98% cap usage → 1.0 |
| Projection quality | 35% | ratio vs theoretical max (value-per-dollar approximation) |
| Team diversity | 15% | ≥4 teams → 1.0 |
| Floor safety | 15% | floor-to-projection ratio |
| Relative ranking | 10% | ratio to best candidate score |

Grades: A+ (≥90), A (≥80), B+ (≥70), B (≥60), C+ (≥50), C (≥40), D (<40)

---

## Scoring System

### Composite Player Score (`_compute_composite_score`)

The per-player scoring function used during lineup construction. Varies by strategy and contest type.

#### Strategy Variants

| Strategy | GPP Formula | Cash Formula |
|----------|------------|--------------|
| `pure_max` | projection × boost | projection × boost |
| `max_projection` | 50% projection + 50% P90 | projection × boost |
| `balanced` | 35% proj + 15% floor + 50% ceiling | 50% proj + 25% floor + 25% ceiling |
| `ceiling` | 15% proj + 85% P90 | 40% proj + 60% P90 |
| `contrarian` | 25% proj + 75% P90 + heavy ownership fade | N/A |

#### Scoring Modifiers (Applied Multiplicatively)

1. **Expert confidence boost**: ±10% from expert signal sentiment
2. **Ownership leverage** (GPP): continuous power-law curve `1/(own/baseline)^alpha`
   - Below baseline ownership → multiplier > 1.0 (boost)
   - Above baseline ownership → multiplier < 1.0 (fade)
   - Alpha and baseline are learnable via CalibrationService
3. **Game total boost**: linear `(total - baseline) × scale`, clamped
4. **Variance bonus** (GPP): `1 + min(sim_std, 15) × 0.005`
5. **Opponent defense**: bad defense (high def rating) → up to +6%
6. **Rotation confidence**: < 0.6 → -5%, < 0.8 → -2%
7. **Game pace**: fast pace → up to +4%
8. **Ceiling-to-salary value** (GPP): elite ceiling per $1K → up to +8%
9. **Noise**: strategy-specific random multiplier for diversity
10. **AI strategy modifiers** (Agent 2): per-player score adjustments
11. **Calibration adjustments**: salary tier preference, ownership threshold, game context
12. **Fade/leverage integration**: FadeService targeted multipliers
13. **Exposure penalty**: capped at 80% reduction (softer for GPP, stronger for cash)

---

## Game Stacking

### Target Game Selection

Games are selected for stacking with probability weighted by game total:
- Weight = `max(1.0, (total - 200)^1.5)` for totals > 200
- High-total games receive exponentially more stacking attention

### Dynamic Stack Parameters

Controlled by tournament-learned calibrations:

- **Stack size**: 2-man vs 3-man (default ~50/50, adjustable via calibration)
- **Bring-back rate**: probability of including an opposing team player (default ~65%)
- **Ownership gating** (GPP): if average ownership in a game exceeds threshold, reduce 3-man stacking (chalk avoidance)

### Stack Player Selection

1. Primary team players sorted by ceiling FP
2. When correlation data available, weighted by pairwise historical correlations
3. Opposing bring-back player selected by ceiling FP with ownership leverage
4. Stack player IDs passed as constraints to both ILP and greedy solvers

---

## Platform Configuration

### DraftKings Classic (NBA)

- **Salary cap**: $50,000
- **Roster slots**: PG, SG, SF, PF, C, G, F, UTIL
- **Slot order** (most constrained first): C, PG, SG, SF, PF, G, F, UTIL

### DraftKings Showdown (Single-Game)

- **Salary cap**: $50,000
- **Roster slots**: CPT, FLEX, FLEX, FLEX, FLEX, FLEX
- **CPT multiplier**: 1.5x fantasy points
- **CPT scoring**: ceiling-weighted (75% ceiling + 25% projection)

### DraftKings CBB

- **Salary cap**: $50,000
- **Roster slots**: G, G, G, F, F, F, UTIL, UTIL
- **CBB abbreviation resolver**: 3-tier matching (direct → known alias → fuzzy substring)

### FanDuel

- **Salary cap**: $60,000 (estimated via 1.2x DK salary ratio)
- **Roster slots**: PG, PG, SG, SG, SF, SF, PF, PF, C
- **No flex/utility slots** — strict positional

---

## Supporting Services

### RotationEngine

Produces baseline + adjusted minutes projections per player:

- **Baseline**: weighted blend of season avg (75%) + EMA of last 5 (15%) + EMA of last 10 (10%)
- **Injury redistribution**: 240-minute team constraint; freed minutes allocated via backup hierarchy
- **Adjustments**: back-to-back fatigue, blowout spread penalty, rest days, veteran age, garbage time, competitive context (tanking/clinched/push)
- **AI agents**: InjuryImpactAgent (Agent 3) for cascading effects, CoachLearningAgent (Agent 6) for coach tendencies

### DFSService

Converts projected minutes to DraftKings/FanDuel fantasy points:

- **Formula**: `minutes × per_min_rate × pace_adj × matchup_factor × usage_boost × calibration`
- **Stat categories**: PTS, REB, AST, STL, BLK, TOV, FG3M
- **Shot decomposition**: when shooting profile available, PTS decomposed into FG3M×3 + FG2M×2 + FTM
- **DD/TD bonuses**: probability-weighted using normal distribution approximation
- **DvP matchup factors**: opponent defensive strength per stat category
- **Floor/ceiling**: rate multipliers + slight minutes variance

### SimulationEngine

Monte Carlo game simulator (vectorized NumPy):

- **Default**: 1,000 simulations per game
- **Variance sources**: game pace, player minutes allocation, per-stat noise multipliers
- **Stat correlations**: position-specific matrices (Guard: high pts↔ast, Big: high reb↔blk)
- **Game scripts**: blowout/competitive/OT scenarios with weighted probabilities
- **Output**: percentile distributions (P10, P25, P50, P75, P90), standard deviation

### CalibrationService

Bridges tournament analysis / backtesting feedback into the optimizer:

- Auto-applies learned adjustments with ±15% caps (0.85-1.15)
- Calibrates: minutes blend weights, stat rate adjustments, salary tier preferences, ownership leverage parameters, stacking weights, DvP sensitivity, pace sensitivity, position bias

---

## AI Agent Integration

The optimizer integrates with 14 AI agents, all optional (graceful degradation when unavailable):

| # | Agent | Tier | Purpose | Impact |
|---|-------|------|---------|--------|
| 2 | LineupStrategyAgent | reasoning | Per-player score modifiers based on game theory | Multiplicative score adjustment |
| 3 | InjuryImpactAgent | reasoning | Cascading injury effects (usage, pace, role changes) | Minutes + usage redistribution |
| 4 | NewsProjectionAgent | fast | Extract projection adjustments from news headlines | Minutes/usage overrides |
| 6 | CoachLearningAgent | reasoning | Coach rotation tendencies and preferences | Minutes adjustment |
| 7 | OwnershipAgent | fast | Field ownership projection for top 50 players | Ownership leverage scoring |
| 8 | SimulationTuningAgent | fast | Per-player noise profiles for Monte Carlo | Simulation variance tuning |
| 9 | NarrativeAgent | reasoning | Natural language lineup analysis | Streaming narrative output |
| 10 | ExpertQualityAgent | fast | Expert signal quality assessment | Signal weighting |
| 11 | BacktestingAgent | reasoning | Historical accuracy analysis | Calibration inputs |
| 12 | TournamentAnalysisAgent | reasoning | Winning lineup pattern analysis | Calibration inputs |
| 13 | ChatAgent | reasoning | Interactive Q&A about lineups | User-facing chat |
| 14 | LineMovementAgent | fast | Vegas line movement analysis | Game context enrichment |

All agents follow the pattern: `__init__(ai_service, cache_service=None)`, `.is_available` property, main analysis method returning `Optional[PydanticModel]`.

---

## API Endpoints

### `GET /api/lineups/player-pool`
Returns the enriched player pool for a slate. Used by the frontend to display all available players with projections.

### `GET /api/lineups/player-pool/stream`
SSE (Server-Sent Events) stream for pool building progress. Sends real-time step updates as teams are processed.

### `POST /api/lineups/preload-pool`
Pre-warms the pool cache for a slate (called by the prewarm daemon on app startup).

### `POST /api/lineups/optimize-lineup`
Generates a single optimal lineup. Supports locked/excluded players, projection overrides, and sport/platform selection.

### `POST /api/lineups/generate-lineups`
Generates N diverse lineups with the full overgenerate-then-filter pipeline. Accepts strategy, contest type, stacking preferences, exposure limits, and salary floor.

### `POST /api/lineups/analyze-lineups`
Analyzes generated lineups for risks, correlations, and swap suggestions.

### `POST /api/lineups/refine-lineups`
Iteratively applies swap suggestions to improve existing lineups.

### `POST /api/lineups/analyze-lineups/narrative`
Streams a natural language narrative analysis of the lineup portfolio.

### `POST /api/lineups/player-pool/clear-cache`
Invalidates all optimizer caches (memory + file).

---

## Request Models

### `MultiLineupRequest` (Primary)

```
platform: "dk" | "fd"
draft_group_id: int
game_date: str (YYYY-MM-DD)
num_lineups: int (1-150)
strategy: "balanced" | "max_projection" | "ceiling" | "contrarian" | "pure_max"
contest_type: "gpp" | "cash" | "single_entry"
max_overlap: int (max shared players between lineups, default 5)
max_exposure: float (0.1-1.0, max player appearance rate)
salary_floor_pct: float (0.0-1.0, minimum salary usage %)
locked_players: List[int] (player IDs forced into every lineup)
excluded_players: List[int] (player IDs removed from pool)
projection_overrides: Dict[int, Dict[str, float]] (manual overrides)
enable_stacking: bool (game correlation stacking, default true)
seed: int (for reproducibility)
sport: "nba" | "cbb"
mode: "classic" | "showdown"
game_id: str (required for showdown — single game target)
recent_weight: float (0.0-0.6, override recent vs season blend)
```

---

## Key Constants

All constants defined in `backend/app/config/constants.py` (793 lines):

### Minutes Projection
- `BASELINE_SEASON_WEIGHT`: 0.75 (season average anchor)
- `BASELINE_RECENT_WEIGHT`: 0.25 (recent form)
- `STAR_ANCHOR_THRESHOLD`: 26.0 min (star player floor)
- `TOTAL_TEAM_MINUTES`: 240 (5 players x 48 min)
- `ABSOLUTE_MAX_MINUTES`: 48.0

### Scoring
- `FLOOR_MINUTES_MULT` / `CEILING_MINUTES_MULT`: floor/ceiling minutes variance
- `FLOOR_RATE_MULT` / `CEILING_RATE_MULT`: floor/ceiling stat rate variance
- DK weights: PTS 1.0, REB 1.25, AST 1.5, STL 2.0, BLK 2.0, TO -0.5, 3PM 0.5, DD +1.5, TD +3.0
- FD weights: PTS 1.0, REB 1.2, AST 1.5, STL 3.0, BLK 3.0, TO -1.0

### Optimizer
- `LINEUP_QUALITY_SINGLE_MAX_RETRIES`: retry count for single lineup quality gate
- `LINEUP_QUALITY_RELATIVE_FLOOR`: minimum score as % of best candidate
- `PORTFOLIO_ILP_SOLVER_TIMEOUT`: ILP time limit
- `TWO_SLOT_SWAP_MAX_ITERATIONS`: max two-slot swap rounds

### Ownership Leverage
- `OWNERSHIP_LEVERAGE_ALPHA`: power-law exponent (standard)
- `OWNERSHIP_LEVERAGE_BASELINE`: ownership % where multiplier = 1.0
- `OWNERSHIP_LEVERAGE_CONTRARIAN_ALPHA`: steeper fade for contrarian
- `OWNERSHIP_LEVERAGE_CONTRARIAN_BASELINE`: lower baseline for contrarian

---

## File Map

| File | Lines | Role |
|------|-------|------|
| `services/lineup_optimizer_service.py` | 5,689 | Core optimizer: pool building, enrichment, ILP/greedy solve, multi-lineup, stacking, quality grading |
| `services/dfs_service.py` | ~600 | Fantasy point projections: per-minute rates, DvP, pace, DD/TD probabilities, shot decomposition |
| `services/rotation_engine.py` | ~1,200 | Minutes projections: EMA baselines, injury redistribution, B2B, blowout, rest, competitive context |
| `services/simulation_engine.py` | ~700 | Monte Carlo: vectorized NumPy, stat correlations, game scripts, percentile distributions |
| `services/ownership_model.py` | ~200 | Rules-based ownership projection (standalone, no AI) |
| `models/lineup.py` | ~300 | Pydantic models: PlayerPoolEntry (54 fields), OptimizeRequest, MultiLineupRequest, OptimizedLineup |
| `models/player.py` | ~112 | PlayerMinutes (with per-min stat rates, shooting profile), PlayerProjection |
| `models/simulation.py` | ~150 | SimulationConfig, GameSimResult, PlayerSimResult |
| `config/constants.py` | 793 | All tunable constants for the entire system |
| `api/routers/lineups.py` | ~400 | REST endpoints: pool, optimize, generate, analyze, refine, narrative stream |
| `services/agents/` | 14 files | AI agents: ownership, strategy, news, sim tuning, injury impact, coach, narrative, etc. |

---

## Performance Characteristics

| Operation | Typical Time | Notes |
|-----------|-------------|-------|
| Pool build (cold) | 15-45s | Parallel team processing, DvP pre-fetch |
| Pool build (cached) | < 100ms | Memory cache hit |
| Pool enrichment | 5-30s | Tier 1 parallel (2-5s), simulation (5-20s), ownership (3-15s) |
| Single lineup (ILP) | < 1s | 5-second solver timeout |
| Single lineup (greedy) | < 500ms | Fill + swap iterations |
| 20 lineups (full pipeline) | 20-60s | Pool + enrich + 60 candidates + selection |
| Portfolio ILP selection | 1-5s | Depends on candidate count |

### Caching Strategy

| Layer | TTL | Scope | Invalidation |
|-------|-----|-------|-------------|
| Memory pool cache | 30 min | Per slate (platform:DG:date) | Injury hash change |
| File pool cache | 2 hours | Per slate (MD5 hash key) | Injury hash change |
| Enrichment cache | 30 min | Per slate | Injury hash change (via pool cache bust) |
| Strategy cache | 30 min | Per slate | Co-invalidated with enrichment |
| Correlation pre-fetch | Per request | Per slate | Not cached across requests |
