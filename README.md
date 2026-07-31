# RotationEngine — DFS Lineup Optimizer

An NBA & College Basketball daily-fantasy lineup optimizer. It ingests injury news, player stats, Vegas odds, and expert signals; projects every player's fantasy output for tonight's DraftKings slate; and generates optimally-diverse tournament lineups with **integer linear programming** (PuLP/CBC). On game day it runs autonomously: a background pipeline polls for injury updates every two minutes before lock, auto-patches saved lineups when a star is scratched, and exports DraftKings-ready CSVs.

> Personal research project, published as a portfolio piece. All credentials and generated data are excluded; `.env.example` files document configuration.

## What's technically interesting

- **ILP lineup generation** — lineup construction is modeled as an integer program (salary cap, roster slots, team stacking rules, cross-lineup diversity constraints) and solved with CBC, rather than greedy heuristics.
- **Monte Carlo simulation layer** — vectorized NumPy/SciPy simulation estimates lineup outcome distributions, not just point projections.
- **14 AI projection agents** — specialized LLM-powered agents (minutes projection, blowout risk, injury interpretation, vegas movement, etc.) contribute structured signals that blend with the statistical model.
- **Late-swap automation** — the injury-polling pipeline detects scratches, re-solves affected lineups under locked-player constraints, and ships updated CSVs within seconds.
- **Multi-source data layer with fallback** — 8+ external data sources with automatic failover so a single dead API doesn't kill a slate.

## Architecture

```
React + Vite frontend  ──proxy──▶  FastAPI backend (Python 3.13)
                                     ├── 19 API routers
                                     ├── 76 service modules (+14 LLM agents)
                                     ├── PuLP/CBC optimizer + NumPy Monte Carlo
                                     ├── APScheduler background jobs (injury polling, late swap)
                                     ├── PostgreSQL (SQLAlchemy 2 async, Alembic)
                                     └── Redis cache/pub-sub
```

Deep dives: [APPLICATION_OVERVIEW.md](APPLICATION_OVERVIEW.md) · [SYSTEM_ARCHITECTURE.md](SYSTEM_ARCHITECTURE.md) · [LINEUP_GENERATOR.md](LINEUP_GENERATOR.md)

## Stack

FastAPI · PostgreSQL (asyncpg) · Redis · PuLP · NumPy/SciPy · APScheduler · Playwright · React 18 · Vite · TailwindCSS · Docker Compose.

## Running it

```bash
docker compose -f docker-compose.dev.yml up -d    # Postgres + Redis
cd backend && pip install -r requirements.txt
cp .env.example .env                              # fill in API keys as needed
uvicorn app.main:app --port 8000
# frontend
cd ../frontend && npm install && npm run dev      # http://localhost:5173, proxies to :8000
```

## Tests

```bash
cd backend && pytest tests/
```
