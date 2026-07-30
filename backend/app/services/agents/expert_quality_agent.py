"""Agent 10 — Expert Source Quality Monitoring.

Tracks which expert sources have historically provided the most
accurate signals and adjusts their weighting in the signal scoring
pipeline accordingly.

Uses **default tier** (Haiku / GPT-4o-mini) — background job.
"""

import json
import logging
from typing import Dict, List, Optional

from app.models.ai import AIRequest, ExpertQualityEvaluation
from app.services.agents.sport_context import get_sport_preamble
from app.services.ai_service import AIService

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are an expert signal quality evaluator for NBA DFS.

Given an expert's past predictions and actual outcomes, evaluate their accuracy.

For each prediction-outcome pair:
- Did the expert correctly predict the direction? (bullish player outperformed, etc.)
- How close were any specific stat predictions?
- Were their injury/rotation calls accurate?

Return JSON:
{
  "expert_handle": "@handle",
  "accuracy_score": float 0.0-1.0 (overall accuracy),
  "total_predictions": int,
  "correct_predictions": int,
  "specialties": { "signal_type": accuracy_score, ... },
  "tier_recommendation": "TIER_1" | "TIER_2" | "TIER_3" | null,
  "weight_modifier": float (0.5 = half weight, 1.5 = 50% more weight)
}

Be strict: a correct call requires the outcome to match the prediction direction.
A "neutral" prediction that a player performed normally = correct if within 20% of baseline.
"""

CACHE_KEY_PREFIX = "ai:expert_quality:"
CACHE_TTL = 86400  # 24 hours


class ExpertQualityAgent:
    """Tracks and scores expert accuracy over time."""

    def __init__(self, ai_service: AIService, cache_service=None):
        self._ai = ai_service
        self._cache = cache_service
        self._quality_cache: Dict[str, ExpertQualityEvaluation] = {}

    @property
    def is_available(self) -> bool:
        return self._ai.is_available

    def evaluate_expert_accuracy(
        self,
        expert_handle: str,
        predictions: List[Dict],
        actuals: List[Dict],
        sport: str = "nba",
    ) -> Optional[ExpertQualityEvaluation]:
        """Evaluate an expert's prediction accuracy.

        Parameters
        ----------
        expert_handle : str
        predictions : list of dict
            Past signals: content, sentiment, mentioned_players, timestamp.
        actuals : list of dict
            Actual outcomes: player_name, actual_fp, actual_minutes, game_date.
        """
        if not self._ai.is_available or not predictions:
            return None

        pred_lines = []
        for p in predictions[:20]:
            pred_lines.append(
                f"  [{p.get('timestamp', '?')}] {p.get('sentiment', '?')}: "
                f"{p.get('content', '')[:150]}"
            )

        actual_lines = []
        for a in actuals[:20]:
            actual_lines.append(
                f"  {a.get('player_name', '?')}: "
                f"{a.get('actual_fp', 0):.1f}FP, "
                f"{a.get('actual_minutes', 0):.0f}min "
                f"({a.get('game_date', '?')})"
            )

        user_prompt = (
            f"**Expert**: {expert_handle}\n"
            f"**Predictions** ({len(predictions)}):\n"
            + "\n".join(pred_lines) + "\n\n"
            f"**Actual outcomes**:\n"
            + "\n".join(actual_lines) + "\n\n"
            f"Evaluate this expert's accuracy."
        )

        request = AIRequest(
            system_prompt=get_sport_preamble(sport) + SYSTEM_PROMPT,
            user_prompt=user_prompt,
            model_tier="default",
            max_tokens=512,
            temperature=0.2,
            response_format="json",
            agent_name="expert_quality",
        )

        data = self._ai.complete_json(request)
        if data is None:
            return None

        try:
            result = ExpertQualityEvaluation(**data)
            self._quality_cache[expert_handle] = result
            self._persist_quality(expert_handle, result)
            logger.info(
                f"[ExpertQuality] {expert_handle}: accuracy={result.accuracy_score:.2f}, "
                f"weight={result.weight_modifier:.2f}"
            )
            return result
        except Exception as exc:
            logger.warning(f"[ExpertQuality] Parse failed: {exc}")
            return None

    def get_quality_adjusted_weight(self, expert_handle: str) -> float:
        """Get the quality-adjusted weight modifier for an expert.

        Returns 1.0 (neutral) if no quality data available.
        """
        if expert_handle in self._quality_cache:
            return self._quality_cache[expert_handle].weight_modifier

        # Try loading from cache
        cached = self._load_quality(expert_handle)
        if cached:
            self._quality_cache[expert_handle] = cached
            return cached.weight_modifier

        return 1.0

    def get_specialty_weight(
        self, expert_handle: str, signal_type: str
    ) -> Optional[float]:
        """Get signal-type-specific accuracy weight for an expert.

        Looks up the ``specialties`` dict on the expert's quality
        evaluation.  If the expert has a recorded accuracy for the
        given signal type, returns that as the weight.  Otherwise
        returns ``None`` so the caller can fall back to the generic
        ``weight_modifier``.
        """
        evaluation = self._quality_cache.get(expert_handle)
        if evaluation is None:
            cached = self._load_quality(expert_handle)
            if cached:
                self._quality_cache[expert_handle] = cached
                evaluation = cached

        if evaluation and evaluation.specialties:
            return evaluation.specialties.get(signal_type)

        return None

    def _persist_quality(self, handle: str, evaluation: ExpertQualityEvaluation):
        if not self._cache:
            return
        try:
            key = f"{CACHE_KEY_PREFIX}{handle}"
            self._cache.set_sync(key, evaluation.model_dump_json(), ttl=CACHE_TTL)
        except Exception:
            pass

    def _load_quality(self, handle: str) -> Optional[ExpertQualityEvaluation]:
        if not self._cache:
            return None
        try:
            key = f"{CACHE_KEY_PREFIX}{handle}"
            data = self._cache.get_sync(key)
            if data:
                parsed = json.loads(data) if isinstance(data, str) else data
                return ExpertQualityEvaluation(**parsed)
        except Exception:
            pass
        return None
