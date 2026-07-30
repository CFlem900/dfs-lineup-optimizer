"""Agent 7 — Ownership Projection.

Estimates likely DFS ownership percentages based on salary, news
volume, expert sentiment, value rating, and game environment.

Uses **default tier** (Haiku / GPT-4o-mini) — one call per slate.
"""

import logging
from typing import Dict, List, Optional

from app.models.ai import AIRequest
from app.services.agents.sport_context import get_sport_preamble
from app.services.ai_service import AIService

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a DFS ownership projection specialist.

Given a player pool for a DraftKings/FanDuel slate, estimate each player's
likely ownership percentage. Consider:

1. **Salary-to-projection value**: Cheap players with high projections → high ownership
2. **News / hype**: Players in the news → higher ownership
3. **Expert consensus**: Players experts are talking about → higher ownership
4. **Stud plays**: Expensive players in good spots → moderate-high ownership
5. **Contrarian opportunities**: Good players that people might overlook → low ownership
6. **Game environment**: Players in high-total/shootout games → higher ownership
7. **Injury beneficiaries**: Players benefiting from someone else's injury → higher ownership

Return JSON: { "player_id": ownership_pct, ... }
Ownership is 0.0-100.0 (percentage). All players should have an estimate.
Total across all players should be roughly: num_roster_spots × 100 / pool_size × 100.
For DK (8 spots), if 50 players are in pool, average ownership = 16%.
"""


class OwnershipProjectionAgent:
    """Estimates DFS ownership percentages."""

    def __init__(self, ai_service: AIService):
        self._ai = ai_service

    @property
    def is_available(self) -> bool:
        return self._ai.is_available

    def project_ownership(
        self,
        pool: List[Dict],
        slate_context: Optional[Dict] = None,
        sport: str = "nba",
    ) -> Dict[int, float]:
        """Estimate ownership for each player in the pool.

        Parameters
        ----------
        pool : list of dict
            Each with: player_id, player_name, position, salary,
            projected_fp, expert_sentiment, game_total.
        slate_context : dict, optional
            platform, num_games, total_players.

        Returns player_id -> ownership % (0-100).
        Falls back to rules-based model if AI unavailable.
        """
        if not pool:
            return {}

        ctx = slate_context or {}
        platform = ctx.get("platform", "dk")
        num_slots = 8 if platform == "dk" else 9
        num_games = ctx.get("num_games", 0)

        # Try AI-based ownership projection first
        ai_result = self._try_ai_ownership(pool, ctx, platform, num_slots, sport)
        if ai_result:
            return ai_result

        # Fall back to rules-based ownership model
        return self._fallback_ownership(pool, platform, num_games)

    def _try_ai_ownership(
        self,
        pool: List[Dict],
        ctx: Dict,
        platform: str,
        num_slots: int,
        sport: str,
    ) -> Dict[int, float]:
        """Attempt AI-based ownership projection. Returns {} on failure."""
        if not self._ai.is_available:
            return {}

        # Build compact player summary
        lines = []
        for p in pool[:60]:  # Limit to 60 for prompt size
            expert = p.get("expert_sentiment", "")
            game_total = p.get("game_total", "")
            news = " [IN NEWS]" if p.get("news_mentions", 0) > 0 else ""
            lines.append(
                f"  ID:{p['player_id']} {p['player_name']} ({p['position']}) "
                f"${p['salary']:,} | Proj:{p['projected_fp']:.1f}FP "
                f"| Val:{p.get('dk_value', 0):.1f}"
                f"{f' | Expert:{expert}' if expert else ''}"
                f"{f' | GameTotal:{game_total}' if game_total else ''}"
                f"{news}"
            )

        user_prompt = (
            f"**Platform**: {platform.upper()} ({num_slots} roster slots)\n"
            f"**Pool size**: {len(pool)} players\n"
            f"**Games**: {ctx.get('num_games', '?')}\n\n"
            f"Estimate ownership % for these players:\n"
            + "\n".join(lines) + "\n\n"
            'Return JSON: { "player_id": ownership_pct, ... }'
        )

        request = AIRequest(
            system_prompt=get_sport_preamble(sport) + SYSTEM_PROMPT,
            user_prompt=user_prompt,
            model_tier="default",
            max_tokens=2048,
            temperature=0.3,
            response_format="json",
            agent_name="ownership",
        )

        data = self._ai.complete_json(request)
        if data is None:
            return {}

        ownership: Dict[int, float] = {}
        for pid_str, pct in data.items():
            try:
                pid = int(pid_str)
                ownership[pid] = max(0.0, min(100.0, float(pct)))
            except (ValueError, TypeError):
                continue

        logger.info(f"[Ownership] AI projected ownership for {len(ownership)} players")
        return ownership

    def _fallback_ownership(
        self,
        pool: List[Dict],
        platform: str,
        num_games: int,
    ) -> Dict[int, float]:
        """Rules-based ownership fallback using salary curves and projections."""
        try:
            from app.services.ownership_model import project_ownership
            result = project_ownership(pool, platform=platform, num_games=num_games)
            if result:
                logger.info(
                    f"[Ownership] Rules-based fallback: {len(result)} players "
                    f"(AI unavailable)"
                )
                return result
        except Exception as e:
            logger.warning(f"[Ownership] Rules-based fallback failed: {e}")
        return {}
