# RotationEngine DFS Application — Full Architecture Overview

## What This Application Does

RotationEngine is an **NBA & College Basketball Daily Fantasy Sports (DFS) lineup optimizer**. It ingests real-time injury news, player stats, Vegas odds, and expert signals, then projects every player's fantasy point output for tonight's DraftKings slate. It generates optimally-diverse tournament lineups using Integer Linear Programming, automatically patches lineups when late scratches hit, and exports directly to DraftKings CSV format.

The system is designed to run autonomously on game day: a background pipeline polls for injury updates every 2 minutes before lock, auto-patches saved lineups when a star is scratched, and emails the updated DK-ready CSV to the team within 30 seconds.

---

## Technology Stack

| Layer | Technology |
|-------|-----------|
| Backend API | FastAPI (Python 3.13) on Uvicorn |
| Frontend | React 18 + Vite + TailwindCSS |
| Database | PostgreSQL (async via SQLAlchemy 2.0 + asyncpg) |
| Cache / Pub-Sub | Redis 7.4 (password-authenticated) |
| Optimizer | PuLP (CBC Integer Linear Programming solver) |
| Simulation | NumPy/SciPy vectorized Monte Carlo |
| AI Agents | 13 LLM-powered agents (OpenAI + Anthropic Claude) |
| Job Scheduler | APScheduler (background cron + interval jobs) |
| Browser Automation | Playwright (DraftKings entry management) |
| Notifications | Email (SMTP) + SMS (Twilio) + Discord (REST Bot API) |

---

## Project Structure

```
Prediction_App/
  backend/
    app/
      api/routers/          19 FastAPI endpoint groups
      services/             76 specialized service modules
      services/agents/      13 AI-powered analysis agents
      db/models.py          23 SQLAlchemy ORM models
      config/constants.py   Central configuration (all tunable parameters) (all tunable parameters)
      models/               Pydantic request/response schemas
      main.py               FastAPI entrypoint + APScheduler lifespan
    alembic/versions/       12 database migrations
    requirements.txt        46 Python dependencies
  frontend/
    src/
      components/           43 React components
      hooks/                6 custom hooks (usePlayerPool, useLineupState, etc.)
      services/api.js       Centralized API client
      context/              Auth + App global state providers
    vite.config.js          Dev server proxy to backend :8000
```

---

## Data Source Architecture

The application pulls data from 8+ external sources with automatic fallback:

| Source | What It Provides | Service File |
|--------|-----------------|-------------|
| BallDontLie API | Player stats, game logs, injuries, rosters | `balldontlie_service.py` |
| stats.nba.com | Team stats, pace, advanced metrics (4 AM refresh only) | `nba_api_service.py` |
| DraftKings API | Player salaries, contest metadata, injury statuses | `dk_draftables_service.py` |
| DraftKings Sportsbook | Player prop lines (PTS, REB, AST, PRA) | `dk_props_service.py` |
| The Odds API | Vegas spreads, over/unders, moneylines | `odds_service.py` |
| ESPN / RotoWire | Breaking news, injury updates, beat reporter tweets | `news_service.py`, `web_scraper.py` |
| Twitter/X | Expert NBA analysis and breaking news | `twitter_scraper.py` |
| Discord | Underdog NBA relay bot (fastest public injury signal) | `discord_news_service.py` |
| Underdog Fantasy | Pick'em lines for edge comparison | `underdog_api_service.py` |
| ESPN CBBpy | College basketball stats, schedules, game logs | `cbb_api_service.py` |

**Fallback chain for live requests:** DB cache (from 4 AM refresh) -> BallDontLie API -> DK salary-based fallback. stats.nba.com is only used in the nightly background refresh.

---

## Core Algorithms

### 1. Top-Down Minutes Allocator ("The Starter's Squeeze")

Located in `top_down_minutes.py`. Allocates exactly 240 team minutes across 8-9 players using a 4-phase algorithm:

- **Phase 0 — Active Status Guillotine:** Zero out players who are Out/Doubtful, have long DNP streaks, or sit beyond rotation depth.
- **Phase 1 — Injury Reallocation:** Promote backups to starter slots when a primary starter is zeroed. Uses strict positional inheritance (PG minutes only go to PG/SG/G, never to a Center).
- **Phase 2 — The Starter's Squeeze:** 5 starters get minutes first via `clamp(season_avg, STARTER_FLOOR, STARTER_CAP)`.
- **Phase 3 — Concentrated Bench Allocation:** Remaining minutes distributed to bench via geometric-decay shares. 6th/7th men get aggressive floors (18/14 min). 10th+ men get 0.

**Sub-algorithms within TopDownMinutes:**
- **Sparse Data Promotion Cap:** Players with <5 game logs capped at 24 minutes when promoted. FPPM regressed toward positional baselines.
- **Last Man Standing Exemption:** When a team is missing >=90 minutes of starter time, top-2 youngsters bypass the sparse cap and get full 32-36 minute workloads.
- **Blowout Risk Penalty:** When >=3 primary rotation players (avg >20 min) are Out, a team health penalty fires: offensive efficiency multiplied by 0.82-0.92x depending on severity.
- **Hard Minutes Ceiling:** Defensive specialists (e.g., Cason Wallace, Tari Eason) have hard caps regardless of injury promotions.
- **Backup Big Man Cap:** Min-salary Centers/PF-Cs capped at 18 minutes.

### 2. Integer Linear Programming (ILP) Lineup Optimizer

Located in `lineup_optimizer_service.py`. Uses PuLP/CBC to solve for optimal DFS lineups with constraints:

- Salary cap ($50,000 max)
- Roster composition (PG, SG, SF, PF, C, G, F, UTIL)
- Game stacking bonuses (correlation between teammates)
- Ownership leverage (power-law penalty for high-ownership chalk)
- Exposure caps (no player exceeds 55% across portfolio)
- Diversity enforcement (each lineup must differ from all previous)
- Correlation avoidance (max player co-ownership across lineups)

**Multi-lineup generation** produces 1-150 diverse lineups per run with soft min-exposure constraints ensuring all viable players get allocated.

### 3. Monte Carlo Game Simulation

Located in `simulation_engine.py`. Vectorized NumPy simulation producing:

- Per-player stat distributions (PTS, REB, AST, STL, BLK, TOV, FG3M)
- DraftKings fantasy point distributions with percentile outputs (P10, P50, P90)
- Game score simulations with overtime probability
- Cross-team and within-team stat correlations
- Game script scenarios (blowout vs. tight game distributions)

### 4. Usage Boost Dampening

When a high-usage star is injured, remaining players get more touches per minute. The boost uses diminishing returns:

```
dampening = max(0.33, 1.0 - (player_FPPM - 0.90) x 2.0)
effective_boost = 1.0 + (raw_boost - 1.0) x dampening
```

This prevents already-elite players (like CJ McCollum at 1.10 FPPM) from being projected at unrealistic levels. A bench scrub at 0.65 FPPM gets the full 15% boost; CJ gets only 6%.

**Defensive Attention Penalty:** When a team's top-2 highest-usage players are both Out, the remaining primary scorer gets a -0.05 FPPM penalty on offensive rates (simulating double teams).

### 5. Ownership Projection Model

Rules-based aggregation combining salary tier, minutes projection, news signals, and expert sentiment into a projected ownership %. Used by the ILP solver's leverage module to penalize chalk and boost contrarian plays.

---

## AI Agent System

13 LLM-powered agents in `services/agents/`, each following the pattern `__init__(ai_service, cache_service=None)` with an `is_available` flag and a main analysis method returning `Optional[PydanticModel]`:

| # | Agent | Purpose |
|---|-------|---------|
| 1 | Signal Analysis | NLP understanding of expert signals (bullish/bearish/neutral) |
| 2 | Lineup Strategy | Game theory for lineup construction (stacking, correlation) |
| 3 | Injury Impact | Dynamic cascading injury impact assessment |
| 4 | Narrative | Human-readable analysis report generation |
| 5 | News Projection | News headline to projection adjustment pipeline |
| 6 | Coach Learning | Learn coaching patterns from historical rotation data |
| 7 | Ownership | Projected ownership modeling |
| 8 | Simulation Tuning | Auto-calibrate simulation parameters from backtest results |
| 9 | Backtesting | Backtest feedback loop for projection accuracy |
| 10 | Expert Quality | Monitor and score expert source reliability |
| 11 | Chat | Conversational interface for ad-hoc queries |
| 12 | Tournament Analysis | Pattern extraction from tournament winning lineups |
| 14 | Line Movement | Vegas line movement analysis for live odds signals |

---

## Pre-Lock Automation Pipeline

The system runs autonomously in the hours before DraftKings slate lock:

```
T-24h:  4 AM nightly refresh (stats.nba.com + BDL bulk data)
T-12h:  APScheduler: injury sync every 15 minutes
T-2h:   Pre-lock simulation fires (builds pool + emails report)
T-1h:   PreLockPollingService switches to ACTIVE mode (2-min polls)
T-30m:  Auto-swap window opens (LATE_SWAP_AUTO_WINDOW_MINUTES)
T-xx:   Any star scratch detected:
          1. fast_patch_lineups() patches all saved lineups (<50ms)
          2. DK CSV exported to exports/ directory
          3. Discord alert posted + email sent to team
T-0:    Slate locks
```

**Injury data flow:**
```
BDL API -> InjuryService.sync_injuries() -> PostgreSQL nba_injuries table
  -> SHA-256 hash comparison
  -> If changed: Redis cache bust + Pub/Sub event
  -> PrewarmSubscriber daemon: auto-rebuild player pool
  -> Frontend: 30-second injury-hash polling triggers auto-refresh
```

---

## Database Schema (23 Tables)

| Table | Purpose |
|-------|---------|
| `users` | OAuth-authenticated users |
| `user_sessions` | Server-side session management |
| `teams` | NBA + CBB teams (sport-partitioned) |
| `player_minutes_history` | Actual vs projected minutes tracking |
| `projection_log` | Full TeamRotation snapshots per game |
| `nba_player_game_log` | Cached NBA player game logs |
| `nba_team_stats` | Cached team stats (base, advanced, opponent, usage) |
| `nba_injuries` | Synced injury reports from BallDontLie |
| `cbb_player_game_log` | College basketball player game logs |
| `gleague_cache` | G-League stats with FPPM + NBA translation tax |
| `expert_signal_log` | Archived expert signals |
| `expert_quality_score` | Expert source reliability tracking |
| `backtest_calibration` | Learned projection adjustments |
| `tournament_contest` | Imported DraftKings contest metadata |
| `tournament_entry` | Individual lineup entries with P&L |
| `tournament_calibration` | Learned adjustments from tournament results |
| `coach_profile_update` | AI-learned coaching pattern deltas |
| `player_prop_snapshot` | Cached player prop data |
| `ai_usage_log` | LLM API usage tracking and cost monitoring |
| `solver_configurations` | Deduplicated optimizer setting snapshots |
| `lineup_batches` | Per-run metadata (date, timing, counts) |
| `lineup_results` | Per-lineup projected vs actual scores |
| `active_entries` | Imported DraftKings contest entries for late-swap |

---

## API Endpoint Groups (19 Routers)

| Router | Prefix | Key Endpoints |
|--------|--------|--------------|
| `lineups.py` | `/player-pool` | GET pool, POST generate-lineups, POST export-dk-csv, POST late-swap/fast-patch |
| `simulation.py` | `/simulation` | POST run, GET results, GET percentiles |
| `teams.py` | `/teams` | GET roster, GET rotation, GET stats |
| `entries.py` | `/slates` | POST parse-entries-csv, POST fill-entries-csv, GET export-csv |
| `props.py` | `/props` | GET player-props, GET edges |
| `signals.py` | `/signals` | GET expert-signals, GET quality-scores |
| `accuracy.py` | `/accuracy` | GET projection-accuracy, GET calibrations |
| `tournament.py` | `/tournament` | POST import, GET analysis, GET calibrations |
| `contests.py` | `/contests` | GET contest-detail, GET recommendations |
| `correlations.py` | `/correlations` | GET correlation-groups |
| `auth.py` | `/auth` | GET login/{provider}, GET callback, POST logout |
| `chat.py` | `/chat` | POST message (conversational AI) |
| `admin.py` | `/admin` | GET status, POST cache-clear |
| `pre_lock.py` | `/pre-lock` | GET status, POST force-sync |
| `injuries.py` | `/injuries` | GET current, POST sync |

---

## Frontend Component Architecture

The frontend is a single-page React 18 application with TanStack Query for server state management:

**Core Flow:** Sport Selection -> Date/Slate Selection -> Player Pool -> Lineup Generation -> Analysis -> DK Export

**Key Components:**
- `LineupBuilder.jsx` — Main workspace: player pool + lineup grid + action bar
- `SlatePlayersPanel.jsx` — Sortable/filterable player pool table
- `LineupGrid.jsx` — Generated lineups with inline editing
- `DKExporterModal.jsx` — 3-step DK CSV fill workflow (upload -> review -> download)
- `TournamentPanel.jsx` — Historical tournament result analysis
- `AccuracyDashboard.jsx` — Projection accuracy metrics over time

**State Management:**
- `usePlayerPool.js` — TanStack Query hook with 30-second injury-hash polling for auto-refresh
- `useLineupState.js` — Lineup generation, sorting, export, clipboard copy
- `useExpertSignals.js` — Expert signal fetch and filtering

---

## Key Configuration (constants.py)

All tunable parameters are centralized in `backend/app/config/constants.py`:

- **Rotation Engine:** EMA weights, baseline splits, starter floor/cap, bench decay
- **Injury Model:** Play probabilities per status (Out=0.0, GTD=0.72, Q=0.85)
- **Blowout Detection:** Spread thresholds, penalty curves, garbage time discounts
- **ILP Solver:** Salary bounds, stacking bonuses, ownership leverage alpha, exposure caps
- **Simulation:** Stat noise sigma, correlation matrices, game script distributions
- **Usage Boost:** Dampening onset/rate/floor, per-stat sensitivity weights
- **Pre-Lock:** Polling intervals (2min active, 10min dormant), auto-swap window (30min)
- **Coach Profiles:** Rotation depth, bench allocation, starter minute ranges

---

## Name Matching & Player ID Resolution

DraftKings, BallDontLie, and stats.nba.com all use different player names and IDs. The app uses a multi-stage resolution pipeline:

1. **Canonical normalizer** (`normalize_player_name`): NFKD Unicode decomposition, suffix stripping (Jr/Sr/II/III/IV), punctuation removal, hyphen-to-space
2. **Known alias dictionaries**: `KNOWN_ALIASES` (player_id_mapper.py) and `_DK_NAME_ALIASES` (balldontlie_service.py) for nickname/legal name mapping
3. **Fuzzy matching**: SequenceMatcher with 0.82 threshold (player_id_mapper) and 0.75 (BDL service)
4. **Nuclear normalize fallback**: Strip ALL spaces for edge cases
5. **Custom projections CSV**: Manual injection for players BDL can't find (G-League call-ups)

---

## Notification & Alerting

| Channel | Trigger | Content |
|---------|---------|---------|
| Email (SMTP) | Pre-lock sim, auto-swap, daily projections | CSV/XLSX attachments to SIMULATION_RECIPIENT list |
| Discord (Bot API) | Late scratch auto-swap | Code-block formatted swap report to news channel |
| SMS (Twilio) | Configurable alerts | Short text alerts for critical events |

---

## External Dependencies Summary

**Backend (46 packages):** FastAPI, Uvicorn, SQLAlchemy, asyncpg, psycopg2, Redis, NumPy, Pandas, SciPy, PuLP, Pydantic, httpx, requests, BeautifulSoup4, Playwright, nba_api, cbbpy, OpenAI, Anthropic, APScheduler, authlib, tenacity, Twilio

**Frontend (9 packages):** React 18, Vite 5, TanStack Query, TanStack Virtual, TailwindCSS, PostCSS, Autoprefixer, lucide-react
