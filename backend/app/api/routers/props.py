"""Player props endpoints — DK Sportsbook market data."""

import logging
from datetime import date
from typing import Optional

from fastapi import APIRouter, Query

from app.api.dependencies import get_services
from app.models.props import (
    PlayerPropsResponse,
    PropsComparisonResponse,
)

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/props", response_model=PlayerPropsResponse)
def get_player_props(
    game_date: Optional[str] = Query(
        None, description="Date in YYYY-MM-DD format. Defaults to today."
    ),
    sport: str = Query("nba", description="Sport: nba or cbb"),
):
    """Get all DK Sportsbook player prop lines for a given date.

    Returns Over/Under lines for pts, reb, ast, PRA, DD, TD for
    every player with available props.
    """
    svc = get_services()
    target = game_date or date.today().isoformat()

    props_service = svc.get_props_service(sport)
    props_lookup = props_service.get_player_props(target)

    return PlayerPropsResponse(
        game_date=target,
        player_count=len(props_lookup),
        players=list(props_lookup.values()),
    )


@router.get("/props/comparison", response_model=PropsComparisonResponse)
def get_props_comparison(
    draft_group_id: int = Query(..., description="DK DraftGroup ID"),
    game_date: Optional[str] = Query(
        None, description="Date in YYYY-MM-DD format. Defaults to today."
    ),
    sport: str = Query("nba", description="Sport: nba or cbb"),
):
    """Compare our stat projections against DK Sportsbook market lines.

    For each player in the draft group who has both our projection and
    a market prop line, returns the delta and a signal
    (aligned / bullish / bearish).
    """
    svc = get_services()
    target = game_date or date.today().isoformat()
    props_service = svc.get_props_service(sport)

    # Get our projections from the lineup optimizer pool
    comparisons = []
    try:
        # Build a player pool to get our projections
        from app.models.lineup import OptimizeRequest
        pool = svc.lineup_optimizer_service.build_player_pool(
            platform="dk",
            draft_group_id=draft_group_id,
            game_date=target,
            sport=sport,
        )

        for entry in pool:
            if not entry.projected_stats:
                continue

            player_comparisons = props_service.compare_projections(
                player_name=entry.player_name,
                team=entry.team_abbreviation,
                projected_stats=entry.projected_stats,
                game_date=target,
            )
            comparisons.extend(player_comparisons)

    except Exception as e:
        logger.warning(f"[PropsAPI] Comparison failed: {e}")

    # Compute summary stats
    bullish = sum(1 for c in comparisons if c.signal == "bullish")
    bearish = sum(1 for c in comparisons if c.signal == "bearish")
    aligned = sum(1 for c in comparisons if c.signal == "aligned")
    avg_abs_delta = (
        sum(abs(c.delta_pct) for c in comparisons) / len(comparisons)
        if comparisons
        else 0.0
    )

    return PropsComparisonResponse(
        game_date=target,
        comparisons=comparisons,
        avg_abs_delta_pct=round(avg_abs_delta, 1),
        bullish_count=bullish,
        bearish_count=bearish,
        aligned_count=aligned,
    )


@router.get("/props/summary")
def get_props_summary(
    game_date: Optional[str] = Query(None),
    sport: str = Query("nba", description="Sport: nba or cbb"),
):
    """Get a quick summary of available props by stat category."""
    svc = get_services()
    target = game_date or date.today().isoformat()
    props_service = svc.get_props_service(sport)
    summary = props_service.get_props_summary(target)
    return {
        "game_date": target,
        "sport": sport,
        "stats": summary,
        "total_players": sum(summary.values()),
    }
