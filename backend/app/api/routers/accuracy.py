"""Accuracy and scoreboard endpoints."""

import asyncio
import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.auth import require_api_key
from app.api.dependencies import get_services
from app.db.database import is_db_available, get_session
from app.models.pagination import PaginationMeta, encode_cursor, decode_cursor
from app.models.responses import (
    AccuracyPaginatedResponse,
    AccuracyRecord,
    AccuracySummaryResponse,
    AccuracyByPositionResponse,
    AccuracyBySalaryTierResponse,
    AccuracyTimelineResponse,
    PlayerAccuracyResponse,
)

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/scoreboard")
async def get_scoreboard(
    game_date: Optional[str] = Query(None, description="Date YYYY-MM-DD (default: today)"),
    sport: str = Query("nba", description="Sport: nba or cbb"),
):
    """Get full schedule with projections for a given date."""
    try:
        svc = get_services()
        game_svc = svc.get_game_service(sport)
        # Pre-warm real CBB stats before sync get_games()
        if sport == "cbb" and hasattr(game_svc, "warm_stats_for_slate"):
            await game_svc.warm_stats_for_slate(game_date)

        # Pre-warm team stats cache from DB (async-safe) so the sync
        # game_svc.get_games() path never calls _run_async/asyncio.run()
        # from a thread-pool thread (which creates event-loop conflicts).
        _db_cache = getattr(game_svc, '_db_cache', None)
        if _db_cache is not None and sport == "nba":
            try:
                from app.utils.helpers import get_current_nba_season
                from datetime import date as _date
                _season = get_current_nba_season()
                _today = _date.today().isoformat()
                # Only fetch if the in-memory cache is stale/empty
                if not game_svc._team_stats_cache or game_svc._team_stats_cache_date != _today:
                    db_stats = await _db_cache.get_all_team_stats(_season)
                    if db_stats and len(db_stats) >= 20:
                        game_svc._team_stats_cache = db_stats
                        game_svc._team_stats_cache_date = _today
                        logger.info(f"[Scoreboard] Pre-warmed team stats: {len(db_stats)} teams")
            except Exception as e:
                logger.warning(f"[Scoreboard] Team stats pre-warm failed: {e}")

        loop = asyncio.get_running_loop()
        schedule = await loop.run_in_executor(
            None, lambda: game_svc.get_games(game_date)
        )
        return schedule
    except Exception as e:
        logger.error(f"Failed to fetch scoreboard: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/accuracy", response_model=AccuracyPaginatedResponse)
async def get_projection_accuracy(
    _auth=Depends(require_api_key),
    team_id: Optional[int] = Query(None, description="Filter by team ID"),
    start_date: Optional[str] = Query(None, description="Start date YYYY-MM-DD"),
    end_date: Optional[str] = Query(None, description="End date YYYY-MM-DD"),
    limit: int = Query(50, ge=1, le=500, description="Max rows to return"),
    cursor: Optional[str] = Query(None, description="Opaque pagination cursor"),
    sport: str = Query("nba", description="Sport: nba or cbb"),
):
    """Compare projected vs actual minutes for backtesting.

    Returns per-player accuracy data from the player_minutes_history
    table.  Requires PostgreSQL to be running and historical data to
    have been logged by a post-game ingestion job.

    Supports cursor-based pagination: pass the ``next_cursor`` value
    from a previous response to fetch the next page.
    """
    if not is_db_available():
        raise HTTPException(
            status_code=503,
            detail="Database not available. Start PostgreSQL and set DATABASE_URL.",
        )

    try:
        from sqlalchemy import select, func, and_, or_, tuple_
        from app.db.models import PlayerMinutesHistory

        async with get_session() as session:
            stmt = (
                select(PlayerMinutesHistory)
                .where(PlayerMinutesHistory.projected_minutes.isnot(None))
                .where(PlayerMinutesHistory.sport == sport)
            )

            if team_id:
                stmt = stmt.where(PlayerMinutesHistory.team_id == team_id)
            if start_date:
                stmt = stmt.where(
                    PlayerMinutesHistory.game_date >= datetime.fromisoformat(start_date)
                )
            if end_date:
                stmt = stmt.where(
                    PlayerMinutesHistory.game_date <= datetime.fromisoformat(end_date)
                )

            # Cursor-based keyset pagination (game_date DESC, id DESC)
            if cursor:
                try:
                    cursor_data = decode_cursor(cursor)
                    cursor_date = datetime.fromisoformat(cursor_data["game_date"])
                    cursor_id = int(cursor_data["id"])
                    # Keyset condition: rows "before" the cursor in our sort order
                    stmt = stmt.where(
                        or_(
                            PlayerMinutesHistory.game_date < cursor_date,
                            and_(
                                PlayerMinutesHistory.game_date == cursor_date,
                                PlayerMinutesHistory.id < cursor_id,
                            ),
                        )
                    )
                except (KeyError, ValueError) as exc:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Invalid cursor: {exc}",
                    )

            # Fetch limit + 1 to detect if there are more pages
            stmt = (
                stmt
                .order_by(
                    PlayerMinutesHistory.game_date.desc(),
                    PlayerMinutesHistory.id.desc(),
                )
                .limit(limit + 1)
            )
            result = await session.execute(stmt)
            rows = result.scalars().all()

            # Determine pagination
            has_more = len(rows) > limit
            page_rows = rows[:limit] if has_more else rows

            # Build next_cursor from the last row on this page
            next_cursor = None
            if has_more and page_rows:
                last = page_rows[-1]
                next_cursor = encode_cursor(
                    game_date=last.game_date.isoformat() if last.game_date else "",
                    id=last.id,
                )

            # Compute aggregate accuracy metrics for this page
            if page_rows:
                errors = [
                    abs(r.projected_minutes - r.actual_minutes) for r in page_rows
                ]
                mae = sum(errors) / len(errors)
                rmse = (sum(e ** 2 for e in errors) / len(errors)) ** 0.5
            else:
                mae = 0.0
                rmse = 0.0

            return AccuracyPaginatedResponse(
                total_records=len(page_rows),
                mean_absolute_error=round(mae, 2),
                root_mean_squared_error=round(rmse, 2),
                records=[
                    AccuracyRecord(
                        player_id=r.player_id,
                        player_name=r.player_name,
                        team_id=r.team_id,
                        game_date=r.game_date.isoformat() if r.game_date else None,
                        projected_minutes=r.projected_minutes,
                        actual_minutes=r.actual_minutes,
                        baseline_minutes=r.baseline_minutes,
                        error=round(abs(r.projected_minutes - r.actual_minutes), 1),
                        position=r.position,
                        was_injured=bool(r.was_injured),
                    )
                    for r in page_rows
                ],
                pagination=PaginationMeta(
                    limit=limit,
                    has_more=has_more,
                    next_cursor=next_cursor,
                ),
            )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to fetch accuracy data: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/accuracy/summary", response_model=AccuracySummaryResponse)
async def get_accuracy_summary(
    _auth=Depends(require_api_key),
    days: int = Query(30, ge=7, le=365, description="Days of history to analyze"),
    sport: str = Query("nba", description="Sport: nba or cbb"),
):
    """Overall projection accuracy: MAE, RMSE, bias for FP, minutes, per-stat."""
    try:
        svc = get_services()
        return await svc.accuracy_service.get_accuracy_summary(days=days, sport=sport)
    except Exception as e:
        logger.error(f"Accuracy summary failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/accuracy/by-position", response_model=AccuracyByPositionResponse)
async def get_accuracy_by_position(
    _auth=Depends(require_api_key),
    days: int = Query(30, ge=7, le=365),
    sport: str = Query("nba", description="Sport: nba or cbb"),
):
    """Projection accuracy broken down by player position."""
    try:
        svc = get_services()
        return await svc.accuracy_service.get_accuracy_by_position(days=days, sport=sport)
    except Exception as e:
        logger.error(f"Accuracy by position failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/accuracy/by-salary-tier", response_model=AccuracyBySalaryTierResponse)
async def get_accuracy_by_salary_tier(
    _auth=Depends(require_api_key),
    days: int = Query(30, ge=7, le=365),
    sport: str = Query("nba", description="Sport: nba or cbb"),
):
    """Projection accuracy broken down by DK salary tier."""
    try:
        svc = get_services()
        return await svc.accuracy_service.get_accuracy_by_salary_tier(days=days, sport=sport)
    except Exception as e:
        logger.error(f"Accuracy by salary tier failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/accuracy/timeline", response_model=AccuracyTimelineResponse)
async def get_accuracy_timeline(
    _auth=Depends(require_api_key),
    days: int = Query(90, ge=7, le=365),
    sport: str = Query("nba", description="Sport: nba or cbb"),
):
    """Daily MAE trend for time-series charts."""
    try:
        svc = get_services()
        return await svc.accuracy_service.get_accuracy_timeline(days=days, sport=sport)
    except Exception as e:
        logger.error(f"Accuracy timeline failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/accuracy/player/{player_id}", response_model=PlayerAccuracyResponse)
async def get_player_accuracy(
    player_id: int,
    _auth=Depends(require_api_key),
    days: int = Query(90, ge=7, le=365),
    sport: str = Query("nba", description="Sport: nba or cbb"),
):
    """Individual player projection accuracy history."""
    try:
        svc = get_services()
        return await svc.accuracy_service.get_player_accuracy(player_id=player_id, days=days, sport=sport)
    except Exception as e:
        logger.error(f"Player accuracy failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
