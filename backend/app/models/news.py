"""Pydantic schemas for NBA news items.

Defines the NewsItem model used by the NewsService and /news endpoint
to return scraped, deduplicated, and relevance-tagged NBA news.
"""

from pydantic import BaseModel, Field
from typing import List, Literal, Optional
from datetime import datetime


class NewsItem(BaseModel):
    """A single news article/update."""

    id: str = Field(..., description="Unique hash of headline + source for dedup")
    headline: str
    summary: Optional[str] = None
    url: Optional[str] = None
    source: Literal["ESPN", "NBA.com", "RotoWire", "FantasyPros", "NumberFire", "Manual"]
    source_icon: Optional[str] = None  # emoji or short label
    published_at: datetime
    # Relevance tagging
    relevance: Literal["injury", "trade", "lineup", "general"] = "general"
    # Associated entities (lowercased for matching)
    team_ids: List[int] = []
    player_ids: List[int] = []
    team_names: List[str] = []       # abbreviations like "DEN", "LAL"
    player_names: List[str] = []     # full names for display


class NewsResponse(BaseModel):
    """API response wrapper for the /news endpoint."""

    items: List[NewsItem]
    total: int
    cached: bool = False
    last_fetched: Optional[datetime] = None
