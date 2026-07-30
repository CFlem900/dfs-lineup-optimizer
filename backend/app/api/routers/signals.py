"""News and expert signal endpoints."""

import logging
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query

from app.api.dependencies import get_services
from app.models.news import NewsResponse
from app.models.expert_signal import ExpertSignalsResponse

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/news", response_model=NewsResponse)
def get_news(
    team_id: Optional[List[int]] = Query(None, description="Filter by team IDs"),
    player_id: Optional[List[int]] = Query(None, description="Filter by player IDs"),
    limit: int = Query(50, ge=1, le=100, description="Max items to return"),
    sport: str = Query("nba", description="Sport: nba or cbb"),
):
    """Get aggregated news from ESPN, NBA.com, and RotoWire.

    Supports filtering by team and player IDs. Results are cached for 10 minutes,
    deduplicated across sources, and tagged with relevance categories
    (injury, trade, lineup, general).

    Note: CBB news is not yet available — returns empty results gracefully.
    """
    try:
        # CBB news not yet supported — return empty gracefully
        if sport == "cbb":
            return NewsResponse(items=[], total=0, cached=False, last_fetched=None)

        svc = get_services()
        items, cached = svc.news_service.get_news(
            team_ids=team_id,
            player_ids=player_id,
            limit=limit,
        )

        return NewsResponse(
            items=items,
            total=len(items),
            cached=cached,
            last_fetched=datetime.fromtimestamp(
                svc.news_service._cache_timestamp, tz=timezone.utc
            )
            if svc.news_service._cache_timestamp
            else None,
        )
    except Exception as e:
        logger.error(f"Failed to fetch news: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/expert-signals", response_model=ExpertSignalsResponse)
def get_expert_signals(
    team_abbr: Optional[str] = Query(None, description="Team abbreviation (e.g., DEN, LAL, DUKE)"),
    player_names: Optional[str] = Query(None, description="Comma-separated player names"),
    limit: int = Query(30, ge=1, le=100, description="Max signals to return"),
    sport: str = Query("nba", description="Sport: nba or cbb"),
):
    """Get aggregated expert signals from Twitter and web sources.

    Signals are scored by expert tier, recency, engagement, and keyword
    relevance, then returned sorted by relevance_score descending.
    Includes a sentiment summary (bullish/bearish/neutral percentages).

    Works for both NBA and CBB team abbreviations.
    """
    # CBB expert signals not yet supported — return empty gracefully
    if sport == "cbb":
        return ExpertSignalsResponse(
            signals=[],
            total=0,
            sentiment_summary={"bullish": 0, "bearish": 0, "neutral": 0},
            experts_tracked=0,
        )

    try:
        svc = get_services()
        names_list = (
            [n.strip() for n in player_names.split(",") if n.strip()]
            if player_names
            else None
        )
        result = svc.expert_signal_service.get_signals(
            team_abbr=team_abbr,
            player_names=names_list,
            limit=limit,
        )
        return result
    except Exception as e:
        logger.error(f"Failed to fetch expert signals: {e}")
        raise HTTPException(status_code=500, detail=str(e))
