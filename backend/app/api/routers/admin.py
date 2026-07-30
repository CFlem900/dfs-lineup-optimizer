"""Admin endpoints: backtest, AI usage, AI status."""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.api.rate_limiter import limiter
from app.api.auth import require_admin_key
from app.api.dependencies import get_services, run_projection_analysis
from app.db.database import is_db_available

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/backtest/ingest")
async def trigger_backtest_ingestion(
    _auth=Depends(require_admin_key),
    game_date: str = Query(..., description="Game date YYYY-MM-DD to ingest"),
    sport: str = Query("nba", description="Sport: nba or cbb"),
):
    """Trigger box score ingestion for a specific date.

    Fetches actual box scores from the NBA API and stores them
    for accuracy comparison.  Automatically triggers projection
    accuracy analysis via the on_complete callback and saves
    calibrations to the BacktestCalibration table.
    """
    if sport == "cbb":
        return {
            "game_date": game_date,
            "rows_inserted": 0,
            "analysis_triggered": False,
            "message": "CBB box score ingestion not yet implemented",
        }

    try:
        svc = get_services()
        # The on_complete callback auto-triggers _run_projection_analysis()
        # after successful ingestion, saving calibrations and refreshing cache.
        count = await svc.box_score_ingester.ingest_game_date(game_date)

        result = {
            "game_date": game_date,
            "rows_inserted": count,
            "analysis_triggered": count > 0,
        }

        return result
    except Exception as e:
        logger.error(f"Backtest ingestion failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/backtest/backfill")
@limiter.limit("2/minute")
async def trigger_historical_backfill(
    request: Request,
    _auth=Depends(require_admin_key),
    start_date: str = Query(..., description="Start date YYYY-MM-DD"),
    end_date: str = Query(..., description="End date YYYY-MM-DD"),
    sport: str = Query("nba", description="Sport: nba or cbb"),
):
    """Backfill historical data with retroactive projections.

    Fetches box scores and generates projections for each date in the
    range.  This populates the accuracy dashboard with real data.

    Rate-limited to 2/min -- each date makes multiple NBA API calls.
    For large ranges (30+ days), use the CLI script instead.
    """
    if sport == "cbb":
        return {
            "dates_processed": 0,
            "message": "CBB historical backfill not yet implemented",
        }

    try:
        from datetime import date as _date

        svc = get_services()
        start = _date.fromisoformat(start_date)
        end = _date.fromisoformat(end_date)
        span = (end - start).days

        if span < 0:
            raise HTTPException(
                status_code=400, detail="start_date must be before end_date"
            )
        if span > 60:
            raise HTTPException(
                status_code=400,
                detail="Maximum 60-day range via API. For larger ranges use "
                "the CLI: python -m app.services.ingestion.historical_backfill "
                f"--start {start_date} --end {end_date}",
            )

        result = await svc.historical_backfill.backfill(start_date, end_date)
        return result

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Historical backfill failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/backtest/analysis")
@limiter.limit("5/minute")
async def get_backtest_analysis(
    request: Request,
    _auth=Depends(require_admin_key),
    days: int = Query(30, ge=7, le=365, description="Number of days to analyse"),
    save_calibrations: bool = Query(
        True, description="Auto-save calibration adjustments to DB"
    ),
    sport: str = Query("nba", description="Sport: nba or cbb"),
):
    """Get AI-analysed projection accuracy report.

    Runs the backtesting agent to identify systematic biases
    and generate calibration adjustments.  When save_calibrations=True,
    adjustments are automatically persisted to the BacktestCalibration
    table and applied to future projections.
    """
    if not is_db_available():
        raise HTTPException(status_code=503, detail="Database not available")

    try:
        result = await run_projection_analysis(days=days, sport=sport)

        if result and result.get("analysis"):
            return {
                "analysis": result["analysis"],
                "calibrations_saved": result.get("calibrations_saved", 0),
            }
        return {"analysis": None, "message": "No historical data or AI unavailable"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Backtest analysis failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/admin/ai-usage")
async def get_ai_usage(
    _auth=Depends(require_admin_key),
    days: int = Query(7, description="Number of days to aggregate"),
):
    """View AI usage summary with per-agent cost breakdown."""
    try:
        svc = get_services()
        summary = await svc.ai_usage_service.get_usage_summary(days=days)
        return summary
    except Exception as e:
        logger.error(f"AI usage query failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/admin/seed-cbb-teams")
async def seed_cbb_teams_endpoint(
    _auth=Depends(require_admin_key),
):
    """Seed (or refresh) CBB teams from ESPN into the database.

    Fetches all ~360 D1 teams and inserts any that are missing.
    Safe to call multiple times — existing teams are skipped.
    """
    from app.db.database import seed_cbb_teams, is_db_available as _db_ok

    if not _db_ok():
        raise HTTPException(status_code=503, detail="Database not available")

    try:
        count = await seed_cbb_teams()
        return {"inserted": count, "message": f"Seeded {count} new CBB teams"}
    except Exception as e:
        logger.error(f"CBB team seeding failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/ai/status")
async def get_ai_status():
    """Get status of AI service and all agents."""
    svc = get_services()
    return {
        "ai_available": svc.ai_service.is_available,
        "provider": svc.ai_service._provider if svc.ai_service.is_available else None,
        "agents": {
            "signal_analysis": svc.signal_analysis_agent.is_available,
            "injury_impact": svc.injury_impact_agent.is_available,
            "news_projection": svc.news_projection_agent.is_available,
            "ownership": svc.ownership_agent.is_available,
            "lineup_strategy": svc.lineup_strategy_agent.is_available,
            "narrative": svc.narrative_agent.is_available,
            "simulation_tuning": svc.simulation_tuning_agent.is_available,
            "coach_learning": svc.coach_learning_agent.is_available,
            "expert_quality": svc.expert_quality_agent.is_available,
            "backtesting": svc.backtesting_agent.is_available,
            "tournament_analysis": svc.tournament_analysis_agent.is_available,
            "chat": svc.chat_agent.is_available,
        },
    }


@router.get("/data-sources")
async def get_data_source_status(
    _auth=Depends(require_admin_key),
):
    """Diagnostic view of all data source availability and circuit breaker states.

    Returns per-source availability and per-API-group circuit breaker
    diagnostics.  Useful for verifying BALLDONTLIE integration is active.
    """
    from app.services.http_resilience import get_all_diagnostics

    svc = get_services()

    source_status = svc.nba_service.get_source_status()

    # NBA API (nba_api_service) has its own standalone circuit breaker
    nba_api_circuit = {}
    try:
        from app.services.nba_api_service import get_circuit_breaker_diagnostics
        nba_api_circuit = get_circuit_breaker_diagnostics()
    except Exception as e:
        nba_api_circuit = {"error": str(e)}

    # http_resilience breakers (DraftKings, ESPN, Underdog, BallDontLie)
    resilience_diagnostics = get_all_diagnostics()

    return {
        "sources": source_status,
        "circuit_breakers": {
            "nba_api": nba_api_circuit,
            **resilience_diagnostics,
        },
    }
