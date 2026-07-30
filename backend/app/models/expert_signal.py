"""Pydantic schemas for expert signals and raw tweets.

Models the pipeline from raw scrape data (RawTweet) through to the
scored, classified ExpertSignal returned by the API.
"""

from datetime import datetime
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field

from app.config.expert_sources import ExpertTier


class RawTweet(BaseModel):
    """Raw scraped tweet before relevance processing."""

    tweet_id: str
    handle: str
    display_name: str
    text: str
    timestamp: datetime
    likes: int = 0
    retweets: int = 0
    url: str
    is_reply: bool = False
    is_retweet: bool = False


class ExpertSignal(BaseModel):
    """Processed, scored expert signal ready for the frontend."""

    id: str = Field(..., description="Dedup hash of source + content")
    expert_name: str
    expert_handle: str
    expert_tier: ExpertTier
    expert_specialty: str
    avatar_url: Optional[str] = None
    content: str                           # Tweet text or web blurb
    summary: Optional[str] = None          # Shortened version for long content
    timestamp: datetime
    source_platform: Literal["twitter", "web"]
    source_url: str
    signal_type: Literal[
        "rotation_change", "injury_update", "minutes_projection",
        "lineup_note", "trade_rumor", "general_take",
    ]
    sentiment: Literal["bullish", "bearish", "neutral"]
    mentioned_players: List[str] = []
    mentioned_team: Optional[str] = None
    relevance_score: float = Field(
        0.0, ge=0.0, le=2.0, description="Computed relevance 0.0-2.0"
    )
    engagement: Optional[Dict[str, int]] = None  # {"likes": N, "retweets": N}


class ExpertSignalsResponse(BaseModel):
    """API response wrapper for the /expert-signals endpoint."""

    signals: List[ExpertSignal]
    total: int
    experts_tracked: int
    cached: bool = False
    sentiment_summary: Dict[str, float] = {}  # {"bullish": 0.6, "bearish": 0.2, ...}
