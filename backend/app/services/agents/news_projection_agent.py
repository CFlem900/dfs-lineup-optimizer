"""Agent 5 — News-to-Projection Pipeline.

Monitors news feed items and extracts actionable projection
adjustments: minutes restrictions, role changes, rest decisions,
surprise starts, and DNP notices.

Uses **default tier** (Haiku / GPT-4o-mini) — extraction task,
one call per news batch.
"""

import logging
from typing import Dict, List

from app.models.ai import AIRequest, NewsProjectionAdjustment
from app.services.ai_service import AIService
from app.services.agents.sport_context import get_sport_preamble

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are an NBA DFS projection adjustment engine.

Analyse news items and extract ONLY actionable projection changes. Ignore general news.

For each item that affects a player's projection, output:
{
  "player_name": "Full Name",
  "adjustment_type": "minutes_cap" | "minutes_boost" | "role_change" | "rest" | "surprise_start" | "dnp",
  "minutes_override": float or null,
  "usage_modifier": float or null (multiplier, e.g. 1.1 = +10% usage),
  "confidence": float 0.0-1.0,
  "source_news_id": "the id from the news item",
  "reasoning": "Brief explanation"
}

Rules:
- "minutes_cap": Player will play but with a limit (e.g. "25-minute restriction")
- "minutes_boost": Player getting expanded role
- "role_change": Position or role shift
- "rest": Load management — treat as minutes_override=0
- "surprise_start": Unexpected starter — boost minutes
- "dnp": Will not play — minutes_override=0

Only include items with clear projection impact. Return a JSON array.
If no items have projection impact, return an empty array [].
"""


class NewsProjectionAgent:
    """Extracts projection-relevant adjustments from news items."""

    def __init__(self, ai_service: AIService):
        self._ai = ai_service

    @property
    def is_available(self) -> bool:
        return self._ai.is_available

    def extract_adjustments(
        self, news_items: List[Dict], max_items: int = 30, sport: str = "nba",
    ) -> List[NewsProjectionAdjustment]:
        """Extract projection adjustments from a batch of news items.

        Returns empty list if AI is unavailable.
        """
        if not self._ai.is_available or not news_items:
            return []

        items = news_items[:max_items]
        lines = []
        for item in items:
            nid = item.get("id", "")
            headline = item.get("headline", item.get("title", ""))
            desc = item.get("description", "")[:200]
            relevance = item.get("relevance", "general")
            lines.append(f"[{nid}] ({relevance}) {headline} — {desc}")

        user_prompt = (
            f"Analyse these {len(items)} news items and extract projection "
            f"adjustments. Return a JSON array:\n\n"
            + "\n".join(lines)
        )

        request = AIRequest(
            system_prompt=get_sport_preamble(sport) + SYSTEM_PROMPT,
            user_prompt=user_prompt,
            model_tier="default",
            max_tokens=1536,
            temperature=0.2,
            response_format="json",
            agent_name="news_projection",
        )

        data = self._ai.complete_json(request)
        if data is None:
            return []

        raw = data if isinstance(data, list) else data.get("adjustments", [])
        results = []
        for item in raw:
            try:
                results.append(NewsProjectionAdjustment(**item))
            except Exception as exc:
                logger.debug(f"[NewsAgent] Skip invalid adjustment: {exc}")

        logger.info(f"[NewsAgent] Extracted {len(results)} adjustments from {len(items)} news items")
        return results
