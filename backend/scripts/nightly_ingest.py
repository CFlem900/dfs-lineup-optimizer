"""Standalone nightly box score ingestion + calibration pipeline.

Designed to be run by cron, Windows Task Scheduler, or systemd timer
without starting the FastAPI application.

Usage:
    python scripts/nightly_ingest.py                    # Ingest yesterday
    python scripts/nightly_ingest.py --date 2026-02-20  # Specific date
    python scripts/nightly_ingest.py --days 3            # Last 3 days
    python scripts/nightly_ingest.py --analysis-only     # Skip ingestion
    python scripts/nightly_ingest.py --ingest-only       # Skip analysis
    python scripts/nightly_ingest.py --no-sim-tuning     # Skip simulation tuning
    python scripts/nightly_ingest.py --no-injury-offsets # Skip injury offsets

Steps:
    1. Initialize database connection (PostgreSQL)
    2. Ingest box scores for target date(s) via NBA API
    3. Run projection accuracy analysis (backtesting agent + deterministic fallback)
    4. Run simulation tuning (Agent 8 noise sigma calibrations)
    5. Recompute team injury offsets

Exit codes:
    0 = success
    1 = database unavailable
    2 = pipeline failed

Prerequisites:
    - DATABASE_URL environment variable
    - OPENAI_API_KEY or ANTHROPIC_API_KEY (for AI agents; deterministic fallback used if unavailable)

Scheduling examples:
    # Windows Task Scheduler:
    #   Program: C:\\Working\\Prediction_App\\backend\\venv\\Scripts\\python.exe
    #   Arguments: scripts\\nightly_ingest.py
    #   Start in: C:\\Working\\Prediction_App\\backend
    #   Trigger: Daily at 3:00 AM
    #
    # Linux cron (3:00 AM Eastern):
    #   0 3 * * * cd /path/to/backend && venv/bin/python scripts/nightly_ingest.py
"""

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Ensure the backend package is importable when run from scripts/ dir
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv(override=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s -- %(message)s",
)
logger = logging.getLogger("nightly_ingest")


async def _run_ingestion(dates: list) -> dict:
    """Ingest box scores for the given dates."""
    from app.services.nba_api_service import NBAApiService
    from app.services.game_service import GameService
    from app.services.ingestion.box_score_ingester import BoxScoreIngester

    nba_svc = NBAApiService()
    game_svc = GameService()
    ingester = BoxScoreIngester(nba_service=nba_svc, game_service=game_svc)

    total_rows = 0
    results = {}
    for gd in dates:
        try:
            rows = await ingester.ingest_game_date(gd)
            results[gd] = {"rows": rows, "status": "ok"}
            total_rows += rows
            logger.info("Ingested %d rows for %s", rows, gd)
        except Exception as exc:
            results[gd] = {"rows": 0, "status": f"error: {exc}"}
            logger.error("Ingestion failed for %s: %s", gd, exc)

    return {"total_rows": total_rows, "dates": results}


async def _run_analysis(days: int = 30) -> dict:
    """Run projection analysis and save calibrations (with deterministic fallback)."""
    from app.api.dependencies import run_projection_analysis

    result = await run_projection_analysis(days=days)
    if result:
        source = "AI"
        analysis = result.get("analysis")
        if isinstance(analysis, dict) and analysis.get("source") == "deterministic":
            source = "deterministic fallback"
        logger.info(
            "Analysis complete (%s): %d calibrations saved",
            source,
            result.get("calibrations_saved", 0),
        )
        return result
    logger.warning("Analysis returned no results (insufficient data?)")
    return {}


async def _run_sim_tuning() -> dict:
    """Run simulation tuning to compute per-role noise sigma calibrations."""
    from app.services.agents.simulation_tuning_agent import SimulationTuningAgent
    from app.services.ai_service import AIService
    from app.services.calibration_service import CalibrationService

    ai_svc = AIService()
    sim_agent = SimulationTuningAgent(ai_svc)
    cal_svc = CalibrationService()

    # Build player pool from DB history (recent games)
    pool = await _build_sim_tuning_pool_from_history()
    if not pool:
        logger.info("No player data available for simulation tuning")
        return {"noise_calibrations_saved": 0}

    noise_cals = sim_agent.compute_nightly_calibrations(pool)
    if not noise_cals:
        logger.info("Simulation tuning produced no calibrations")
        return {"noise_calibrations_saved": 0}

    saved = await cal_svc.save_backtest_calibrations(
        noise_cals,
        metadata={
            "source": "simulation_tuning_agent",
            "reasoning": "Nightly per-role sigma calibration",
        },
    )
    await cal_svc.load_calibrations()
    logger.info("Sim tuning: %d noise sigma calibrations saved", saved)
    return {"noise_calibrations_saved": saved}


async def _build_sim_tuning_pool_from_history() -> list:
    """Build a player pool for sim tuning from recent PlayerMinutesHistory."""
    from sqlalchemy import select, func
    from app.db.database import get_session
    from app.db.models import PlayerMinutesHistory

    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    async with get_session() as session:
        # Get most recent record per player
        subq = (
            select(
                PlayerMinutesHistory.player_name,
                func.max(PlayerMinutesHistory.game_date).label("latest"),
            )
            .where(PlayerMinutesHistory.actual_minutes.isnot(None))
            .where(PlayerMinutesHistory.game_date >= cutoff)
            .group_by(PlayerMinutesHistory.player_name)
            .subquery()
        )
        stmt = (
            select(PlayerMinutesHistory)
            .join(
                subq,
                (PlayerMinutesHistory.player_name == subq.c.player_name)
                & (PlayerMinutesHistory.game_date == subq.c.latest),
            )
        )
        result = await session.execute(stmt)
        rows = result.scalars().all()

    pool = []
    for r in rows:
        fp = r.dk_projected_fp or 0
        pool.append({
            "player_id": r.player_name,
            "player_name": r.player_name,
            "position": r.position or "?",
            "salary": r.dk_salary or 0,
            "projected_fp": fp,
            "floor_fp": fp * 0.8,
            "ceiling_fp": fp * 1.2,
            "projected_minutes": r.projected_minutes or 0,
            "is_b2b": False,
            "injury_status": None,
        })
    return pool


async def _run_injury_offsets() -> dict:
    """Recompute team-specific injury factor offsets."""
    from app.services.calibration_service import CalibrationService

    cal_svc = CalibrationService()
    offsets = await cal_svc.compute_team_injury_offsets()
    logger.info("Injury offsets: %d (team, status) pairs refreshed", len(offsets))
    return {"injury_offsets_loaded": len(offsets)}


async def _main(args):
    from app.db.database import init_db, close_db

    # Step 1: Initialize DB
    db_ok = await init_db()
    if not db_ok:
        logger.error("Database unavailable -- check DATABASE_URL")
        sys.exit(1)

    try:
        result = {
            "ingestion": None,
            "analysis": None,
            "sim_tuning": None,
            "injury_offsets": None,
        }

        # Step 2: Ingest box scores
        if not args.analysis_only:
            if args.date:
                dates = [args.date]
            elif args.days:
                dates = [
                    (datetime.now(timezone.utc) - timedelta(days=d)).strftime(
                        "%Y-%m-%d"
                    )
                    for d in range(1, args.days + 1)
                ]
            else:
                yesterday = (
                    datetime.now(timezone.utc) - timedelta(days=1)
                ).strftime("%Y-%m-%d")
                dates = [yesterday]

            logger.info("Ingesting box scores for: %s", ", ".join(dates))
            result["ingestion"] = await _run_ingestion(dates)

            if result["ingestion"]["total_rows"] == 0 and not args.ingest_only:
                logger.warning(
                    "No rows ingested -- analysis may have insufficient data"
                )

        # Step 3: Run analysis + calibration (with deterministic fallback)
        if not args.ingest_only:
            logger.info("Running projection analysis (last %d days)...", args.analysis_days)
            result["analysis"] = await _run_analysis(days=args.analysis_days)

        # Step 4: Simulation tuning
        if not args.ingest_only and args.sim_tuning:
            logger.info("Running simulation tuning calibrations...")
            try:
                result["sim_tuning"] = await _run_sim_tuning()
            except Exception as exc:
                logger.warning("Simulation tuning failed (safely skipped): %s", exc)
                result["sim_tuning_error"] = str(exc)

        # Step 5: Team injury offsets
        if not args.ingest_only and args.injury_offsets:
            logger.info("Recomputing team injury offsets...")
            try:
                result["injury_offsets"] = await _run_injury_offsets()
            except Exception as exc:
                logger.warning("Injury offset computation failed (safely skipped): %s", exc)
                result["injury_offset_error"] = str(exc)

        # Summary
        _print_summary(result, args)

    except Exception as exc:
        logger.exception("Pipeline failed: %s", exc)
        sys.exit(2)
    finally:
        await close_db()


def _print_summary(result: dict, args):
    """Print a formatted pipeline summary."""
    print("=" * 60)
    print("Nightly Backtest Pipeline Complete")
    print("=" * 60)

    if result["ingestion"]:
        ingestion = result["ingestion"]
        print(f"  Rows ingested: {ingestion['total_rows']}")
        for gd, info in ingestion["dates"].items():
            print(f"    {gd}: {info['rows']} rows ({info['status']})")

    if result["analysis"]:
        analysis = result["analysis"]
        cals = analysis.get("calibrations_saved", 0)
        source = "AI"
        analysis_data = analysis.get("analysis")
        stats = analysis.get("stats") or {}
        if isinstance(analysis_data, dict):
            if analysis_data.get("source") == "deterministic":
                source = "deterministic fallback"
            if not stats:
                stats = analysis_data.get("stats") or {}

        print(f"  Analysis source: {source}")
        if stats:
            fp_bias = stats.get("overall_fp_bias", "?")
            min_bias = stats.get("overall_min_bias", "?")
            n = stats.get("sample_size", "?")
            print(f"  Projection bias: FP={fp_bias}, MIN={min_bias} (n={n})")
        print(f"  Calibrations saved: {cals}")

    if result.get("sim_tuning"):
        saved = result["sim_tuning"].get("noise_calibrations_saved", 0)
        print(f"  Noise sigma calibrations: {saved} saved")
    elif result.get("sim_tuning_error"):
        print(f"  Noise sigma calibrations: FAILED ({result['sim_tuning_error']})")

    if result.get("injury_offsets"):
        loaded = result["injury_offsets"].get("injury_offsets_loaded", 0)
        print(f"  Team injury offsets: {loaded} loaded")
    elif result.get("injury_offset_error"):
        print(f"  Team injury offsets: FAILED ({result['injury_offset_error']})")

    print("=" * 60)


def main():
    parser = argparse.ArgumentParser(
        description="Standalone nightly box score ingestion + calibration pipeline.",
    )
    parser.add_argument(
        "--date",
        type=str,
        default=None,
        help="Specific game date to ingest (YYYY-MM-DD). Default: yesterday.",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=None,
        help="Ingest the last N days of box scores.",
    )
    parser.add_argument(
        "--analysis-only",
        action="store_true",
        help="Skip ingestion, only run projection analysis.",
    )
    parser.add_argument(
        "--ingest-only",
        action="store_true",
        help="Skip analysis, only ingest box scores.",
    )
    parser.add_argument(
        "--analysis-days",
        type=int,
        default=30,
        help="Number of days of history to analyze (default: 30).",
    )
    parser.add_argument(
        "--sim-tuning",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Run simulation tuning step (default: enabled).",
    )
    parser.add_argument(
        "--injury-offsets",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Recompute team injury offsets (default: enabled).",
    )
    args = parser.parse_args()
    asyncio.run(_main(args))


if __name__ == "__main__":
    main()
