"""Correlation endpoints."""

import logging

from fastapi import APIRouter, Query

from app.api.dependencies import get_services

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/correlations/{team_id}")
async def get_team_correlations(
    team_id: int,
    days: int = Query(60, ge=7, le=365, description="Days of history"),
):
    """Get player-pair FP correlations for a team."""
    svc = get_services()
    return await svc.correlation_service.get_team_correlations(team_id, days)


@router.get("/correlations/{team_id}/pairs")
async def get_correlated_pairs(
    team_id: int,
    min_correlation: float = Query(0.3, ge=0.0, le=1.0, description="Minimum |correlation|"),
    days: int = Query(60, ge=7, le=365, description="Days of history"),
):
    """Get strongly correlated player pairs for a team."""
    svc = get_services()
    return await svc.correlation_service.get_correlated_pairs(team_id, min_correlation, days)


@router.get("/correlations/game/{home_team_id}/{away_team_id}")
async def get_game_correlations(
    home_team_id: int,
    away_team_id: int,
    days: int = Query(60, ge=7, le=365, description="Days of history"),
):
    """Get intra-team and cross-team correlations for a game matchup."""
    svc = get_services()
    return await svc.correlation_service.get_game_correlations(home_team_id, away_team_id, days)
