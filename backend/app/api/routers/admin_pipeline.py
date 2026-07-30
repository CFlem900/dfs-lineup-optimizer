"""Automated nightly feedback pipeline — admin endpoints.

Provides manual trigger and status endpoints for the nightly ingestion +
projection analysis + tournament calibration pipeline.

The scheduled job is configured in ``app.main.lifespan()``.

Endpoints:
    POST /admin/pipeline/trigger  — manually kick off the pipeline
    GET  /admin/pipeline/status   — get last-run info and next scheduled time
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException

from app.api.auth import require_admin_key
from app.db.database import is_db_available
from app.models.responses import PipelineStatusResponse

logger = logging.getLogger(__name__)
router = APIRouter()

# ── Module-level pipeline state ─────────────────────────────────────────
_pipeline_lock = asyncio.Lock()

_pipeline_state: Dict[str, Any] = {
    "last_run_at": None,
    "last_status": "idle",       # idle | running | success | failed
    "last_result": None,
    "last_error": None,
    "total_runs": 0,
}


def get_pipeline_state() -> Dict[str, Any]:
    """Return a copy of the current pipeline state (for testing)."""
    return dict(_pipeline_state)


def _build_sim_tuning_pool(svc) -> list:
    """Build a simplified player list for Agent 8 nightly calibration.

    Pulls from the NBA data cache player pool.  Returns a list of dicts
    with the fields ``classify_player_roles()`` needs, or an empty list
    if no cached data is available.
    """
    try:
        cache = getattr(svc, "nba_data_cache", None)
        if not cache:
            return []
        pool = getattr(cache, "get_player_pool", lambda: None)()
        if not pool:
            return []
        result = []
        for p in pool:
            pid = p.get("player_id") or p.get("id")
            if not pid:
                continue
            sal = p.get("salary") or p.get("dk_salary", 0)
            fp = p.get("projected_fp") or p.get("dk_projected_fp", 0)
            result.append({
                "player_id": pid,
                "player_name": p.get("player_name", "?"),
                "position": p.get("position", "?"),
                "salary": sal,
                "projected_fp": fp,
                "floor_fp": p.get("floor_fp", fp * 0.8),
                "ceiling_fp": p.get("ceiling_fp", fp * 1.2),
                "projected_minutes": p.get("projected_minutes", 0),
                "is_b2b": p.get("is_b2b", False),
                "injury_status": p.get("injury_status"),
                "vegas_spread": p.get("vegas_spread"),
            })
        return result
    except Exception as exc:
        logger.debug(f"[Pipeline] Could not build sim tuning pool: {exc}")
        return []


async def run_nightly_pipeline(game_date: Optional[str] = None) -> Dict[str, Any]:
    """Execute the full nightly feedback pipeline.

    Steps:
        1. Ingest yesterday's box scores
        2. Run projection accuracy analysis + save calibrations
        3. Log results

    Parameters
    ----------
    game_date : str, optional
        Override date (YYYY-MM-DD).  Defaults to yesterday (UTC).

    Returns
    -------
    dict with step results.
    """
    from app.api.dependencies import get_services, run_projection_analysis
    from app.services.feature_flags import is_feature_enabled

    if not is_feature_enabled('NIGHTLY_PIPELINE'):
        logger.info("[Pipeline] Nightly pipeline disabled via feature flag")
        return {"skipped": True, "reason": "feature_nightly_pipeline=false"}

    async with _pipeline_lock:
        _pipeline_state["last_status"] = "running"
        _pipeline_state["last_run_at"] = datetime.now(timezone.utc).isoformat()
        _pipeline_state["last_error"] = None

    result: Dict[str, Any] = {
        "game_date": None,
        "rows_ingested": 0,
        "analysis": None,
        "calibrations_saved": 0,
    }

    try:
        svc = get_services()

        # Step 1: Compute target date
        if game_date is None:
            yesterday = datetime.now(timezone.utc) - timedelta(days=1)
            game_date = yesterday.strftime("%Y-%m-%d")
        result["game_date"] = game_date

        # Step 2: Ingest box scores
        if is_db_available():
            try:
                rows = await svc.box_score_ingester.ingest_game_date(game_date)
                result["rows_ingested"] = rows
                logger.info(f"[Pipeline] Ingested {rows} box score rows for {game_date}")
            except Exception as exc:
                logger.warning(f"[Pipeline] Box score ingestion failed: {exc}")
                result["ingestion_error"] = str(exc)

        # Step 3: Run projection analysis
        if is_db_available():
            try:
                analysis = await run_projection_analysis(days=30)
                if analysis:
                    result["analysis"] = analysis.get("analysis")
                    result["calibrations_saved"] = analysis.get("calibrations_saved", 0)
                    logger.info(
                        f"[Pipeline] Analysis complete: "
                        f"{result['calibrations_saved']} calibrations saved"
                    )
                    # Log projection bias summary
                    stats = analysis.get("stats") or {}
                    if not stats and isinstance(analysis.get("analysis"), dict):
                        stats = analysis["analysis"].get("stats") or {}
                    if stats:
                        result["bias_summary"] = {
                            "fp_bias": stats.get("overall_fp_bias"),
                            "min_bias": stats.get("overall_min_bias"),
                            "sample_size": stats.get("sample_size"),
                        }
                        logger.info(
                            f"[Pipeline] Projection bias: "
                            f"FP={stats.get('overall_fp_bias', '?')}, "
                            f"MIN={stats.get('overall_min_bias', '?')}, "
                            f"sample={stats.get('sample_size', '?')}"
                        )
            except Exception as exc:
                logger.warning(f"[Pipeline] Projection analysis failed: {exc}")
                result["analysis_error"] = str(exc)

        # Step 4: Simulation tuning — compute per-role noise calibrations
        try:
            if svc.simulation_tuning_agent:
                _pool = _build_sim_tuning_pool(svc)
                if _pool:
                    noise_cals = svc.simulation_tuning_agent.compute_nightly_calibrations(_pool)
                    if noise_cals:
                        saved = await svc.calibration_service.save_backtest_calibrations(
                            noise_cals,
                            metadata={
                                "source": "simulation_tuning_agent",
                                "reasoning": "Nightly per-role sigma calibration",
                            },
                        )
                        result["noise_calibrations_saved"] = saved
                        await svc.calibration_service.load_calibrations()
                        logger.info(
                            f"[Pipeline] Sim tuning: {saved} noise sigma calibrations saved"
                        )
        except Exception as exc:
            logger.warning(f"[Pipeline] Sim tuning calibration failed: {exc}")
            result["sim_tuning_error"] = str(exc)

        # Step 5: Recompute team-specific injury offsets
        if is_db_available() and svc.calibration_service:
            try:
                offsets = await svc.calibration_service.compute_team_injury_offsets()
                result["injury_offsets_loaded"] = len(offsets)
                logger.info(
                    f"[Pipeline] Injury offsets: {len(offsets)} "
                    f"(team, status) pairs refreshed"
                )
            except Exception as exc:
                logger.warning(f"[Pipeline] Injury offset computation failed: {exc}")
                result["injury_offset_error"] = str(exc)

        async with _pipeline_lock:
            _pipeline_state["last_status"] = "success"
            _pipeline_state["last_result"] = result
            _pipeline_state["total_runs"] += 1
        logger.info(f"[Pipeline] Nightly pipeline complete for {game_date}")

    except Exception as exc:
        async with _pipeline_lock:
            _pipeline_state["last_status"] = "failed"
            _pipeline_state["last_error"] = str(exc)
        logger.error(f"[Pipeline] Nightly pipeline failed: {exc}")
        raise

    return result


# ── Endpoints ───────────────────────────────────────────────────────────

@router.post("/admin/pipeline/trigger")
async def trigger_pipeline(
    _auth=Depends(require_admin_key),
    game_date: Optional[str] = None,
):
    """Manually trigger the nightly feedback pipeline.

    Optionally specify a game_date (YYYY-MM-DD) to override yesterday.
    """
    # Atomically check status and claim the lock to prevent duplicate runs.
    # Without this, two concurrent calls could both read "idle" and both start.
    async with _pipeline_lock:
        if _pipeline_state["last_status"] == "running":
            raise HTTPException(
                status_code=409,
                detail="Pipeline is already running",
            )
        # Mark as running while holding the lock so no other caller can slip in
        _pipeline_state["last_status"] = "running"

    try:
        result = await run_nightly_pipeline(game_date=game_date)
        return {
            "status": "success",
            "result": result,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/admin/pipeline/status", response_model=PipelineStatusResponse)
async def pipeline_status(
    _auth=Depends(require_admin_key),
):
    """Get the current pipeline status and last-run info."""
    async with _pipeline_lock:
        state = dict(_pipeline_state)

    # Add next scheduled run if scheduler is available
    try:
        from app.main import get_next_pipeline_run
        state["next_scheduled_run"] = get_next_pipeline_run()
    except (ImportError, AttributeError):
        state["next_scheduled_run"] = None

    return state


# ── NBA Data Cache Refresh ─────────────────────────────────────────────

@router.post("/admin/refresh-nba-cache")
async def refresh_nba_cache(
    _auth=Depends(require_admin_key),
    season: Optional[str] = None,
):
    """Manually trigger a full NBA data cache refresh.

    Fetches all player game logs, team stats, rosters, and usage rates
    from stats.nba.com and writes them to the PostgreSQL cache tables.

    This takes ~5 minutes and is safe to re-run (uses upsert).

    Query params:
        season: Optional season string (e.g. "2025-26"). Auto-detected if omitted.
    """
    if not is_db_available():
        raise HTTPException(
            status_code=503,
            detail="Database not available — cannot refresh cache",
        )

    try:
        from app.api.dependencies import get_services
        svc = get_services()
        cache_svc = svc.nba_data_cache
        bdl = svc.bdl_service
        result = await cache_svc.refresh_all(
            season=season, balldontlie=bdl
        )
        return result
    except Exception as e:
        logger.error(f"[Admin] NBA cache refresh failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
