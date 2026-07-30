"""Contest recommendation endpoints.

Provides ranked contest recommendations based on overlay, prize
structure, field strength, and calibration accuracy.
"""

import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request

from app.api.dependencies import get_services
from app.api.rate_limiter import limiter

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/contests/recommendations")
@limiter.limit("10/minute")
async def get_contest_recommendations(
    request: Request,
    platform: str = Query("dk", description="Platform (dk)"),
    sport: str = Query("nba", description="Sport (nba or cbb)"),
    game_date: Optional[str] = Query(
        None, description="Target date YYYY-MM-DD (default: today)"
    ),
    draft_group_id: Optional[int] = Query(
        None, description="Filter to a specific DraftGroup / slate"
    ),
):
    """Get ranked contest recommendations for a slate.

    Fetches all available DK contests, scores each on overlay risk,
    prize structure, field strength, and calibration fit, then returns
    a sorted list with recommended entry counts.
    """
    try:
        svc = get_services()
        # recommend_contests() is synchronous and issues an N+1 fan-out of
        # blocking HTTP calls to DraftKings (lobby + per-contest detail).
        # Run it in a thread so it doesn't stall the asyncio event loop and
        # block every other endpoint on the same worker.
        result = await asyncio.to_thread(
            svc.contest_recommender_service.recommend_contests,
            platform=platform,
            sport=sport,
            game_date=game_date,
            draft_group_id=draft_group_id,
        )
        return result.model_dump()
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[ContestRec] Recommendations failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/contests/{contest_id}/score")
@limiter.limit("20/minute")
async def get_contest_score(
    request: Request,
    contest_id: str,
):
    """Score a single contest by its DraftKings contest ID.

    Returns a detailed breakdown of the four scoring components
    plus an aggregate score and recommended entry count.
    """
    try:
        svc = get_services()
        result = await asyncio.to_thread(
            svc.contest_recommender_service.score_contest_by_id, contest_id
        )
        if not result:
            raise HTTPException(
                status_code=404,
                detail=f"Contest {contest_id} not found or unavailable",
            )
        return result.model_dump()
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[ContestRec] Score failed for {contest_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
