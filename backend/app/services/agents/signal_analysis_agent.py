"""Agent 1 — NLP Expert Signal Understanding.

Replaces keyword-based sentiment classification in ExpertSignalService
with genuine LLM analysis.  Detects sarcasm, conditional statements,
specific stat predictions, and nuanced sentiment from expert tweets
and articles.

Uses **Anthropic Tool Use** to guarantee valid structured output — the
model is forced to call a tool whose ``input_schema`` matches the
``SignalAnalysis`` Pydantic model exactly.  No more JSON parse errors.

Uses **default tier** (Haiku / GPT-4o-mini) — high volume, one call
per signal batch.
"""

import logging
from typing import Dict, List, Optional

from app.models.ai import AIRequest, SignalAnalysis
from app.services.agents.sport_context import get_sport_preamble
from app.services.ai_service import AIService

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are an NBA DFS (Daily Fantasy Sports) expert signal analyst.

Analyse expert tweets/articles and extract structured data. For each signal:
1. Classify sentiment as bullish, bearish, or neutral
2. Rate confidence 0.0-1.0
3. Detect sarcasm (experts often use it)
4. Identify if the take is conditional ("if X plays, then Y benefits")
5. Extract specific player mentions
6. Extract any stat predictions (e.g. "Jokic goes for 60+ FP")
7. Write a one-sentence impact summary
8. Classify the signal type

Important:
- "bullish" means positive for DFS value (more minutes, better matchup, starting)
- "bearish" means negative (fewer minutes, injury concern, rest risk)
- Sarcasm inverts the literal meaning — flag it
- Be precise with player name spelling
- Return exactly one analysis object per input signal, in the same order
"""

# ── Tool Use schema ──────────────────────────────────────────────────
# Matches SignalAnalysis + StatPrediction Pydantic models exactly.
# Anthropic validates the output against this schema before returning.

_STAT_PREDICTION_SCHEMA = {
    "type": "object",
    "properties": {
        "player": {"type": "string"},
        "stat": {"type": "string"},
        "value": {"type": ["number", "null"]},
        "direction": {"type": ["string", "null"], "enum": ["over", "under", None]},
        "raw_text": {"type": "string"},
    },
    "required": ["player", "stat"],
}

_SIGNAL_ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "sentiment": {
            "type": "string",
            "enum": ["bullish", "bearish", "neutral"],
        },
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "sarcasm_detected": {"type": "boolean"},
        "conditional": {"type": "boolean"},
        "condition_text": {"type": ["string", "null"]},
        "mentioned_players": {
            "type": "array",
            "items": {"type": "string"},
        },
        "stat_predictions": {
            "type": "array",
            "items": _STAT_PREDICTION_SCHEMA,
        },
        "impact_summary": {"type": "string"},
        "signal_type": {
            "type": "string",
            "enum": [
                "rotation_change", "injury_update", "minutes_projection",
                "lineup_note", "trade_rumor", "general_take",
            ],
        },
    },
    "required": [
        "sentiment", "confidence", "sarcasm_detected", "conditional",
        "mentioned_players", "impact_summary", "signal_type",
    ],
}

# Top-level tool schema: wraps array of signal analyses
TOOL_NAME = "extract_expert_signals"
TOOL_DESCRIPTION = (
    "Extract structured DFS expert signal analyses from tweets/articles. "
    "Return exactly one analysis object per input signal."
)
TOOL_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "signals": {
            "type": "array",
            "items": _SIGNAL_ANALYSIS_SCHEMA,
            "description": "One analysis object per input signal, in order.",
        },
    },
    "required": ["signals"],
}


class SignalAnalysisAgent:
    """LLM-powered expert signal analysis.

    Uses Anthropic Tool Use to guarantee 100% valid structured JSON.
    Falls back to ``complete_json()`` (with repair) when tool use is
    unavailable (e.g. OpenAI provider).
    """

    def __init__(self, ai_service: AIService):
        self._ai = ai_service

    @property
    def is_available(self) -> bool:
        return self._ai.is_available

    def analyze_signal(
        self, content: str, context: Optional[Dict] = None,
        sport: str = "nba",
    ) -> Optional[SignalAnalysis]:
        """Analyse a single expert signal.

        Falls back to ``None`` if AI is unavailable, letting the
        caller use the existing keyword-based classification.
        """
        results = self.batch_analyze(
            [{"content": content, "context": context or {}}],
            sport=sport,
        )
        return results[0] if results else None

    def batch_analyze(
        self, signals: List[Dict], max_batch: int = 20,
        sport: str = "nba",
    ) -> List[Optional[SignalAnalysis]]:
        """Batch-analyse expert signals in a single LLM call.

        Uses Anthropic's ``tool_choice`` to force the model to return
        a ``signals`` array that exactly matches the ``SignalAnalysis``
        schema.  Zero chance of ``json.JSONDecodeError``.

        Each element in *signals* should have ``content`` (str) and
        optionally ``context`` (dict with team_abbr, player_names).

        Returns a list of ``SignalAnalysis`` objects (or ``None`` for
        entries that couldn't be parsed).
        """
        if not self._ai.is_available or not signals:
            return [None] * len(signals)

        # Truncate to max batch size
        batch = signals[:max_batch]

        # Build numbered signal list for the prompt
        lines = []
        for i, sig in enumerate(batch):
            ctx = sig.get("context", {})
            team = ctx.get("team_abbr", "")
            extra = f" [Team context: {team}]" if team else ""
            lines.append(f"{i + 1}. {sig['content']}{extra}")

        user_prompt = (
            f"Analyse these {len(batch)} expert signals. "
            f"Call the extract_expert_signals tool with exactly "
            f"{len(batch)} analysis objects (one per signal, same order):\n\n"
            + "\n".join(lines)
        )

        request = AIRequest(
            system_prompt=get_sport_preamble(sport) + SYSTEM_PROMPT,
            user_prompt=user_prompt,
            model_tier="default",
            max_tokens=4096,
            temperature=0.2,
            agent_name="signal_analysis",
        )

        # ── Primary path: Anthropic Tool Use (guaranteed valid JSON) ──
        data = self._ai.complete_tool_use(
            request,
            tool_name=TOOL_NAME,
            tool_description=TOOL_DESCRIPTION,
            input_schema=TOOL_INPUT_SCHEMA,
        )

        if data is None:
            logger.warning("[SignalAgent] AI call returned None — using fallback")
            return [None] * len(batch)

        # Tool use returns {"signals": [...]}, complete_json may return
        # a raw list or {"signals": [...]}
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            items = data.get("signals", [])
        else:
            items = []

        results: List[Optional[SignalAnalysis]] = []
        for i in range(len(batch)):
            try:
                item = items[i] if i < len(items) else {}
                results.append(SignalAnalysis(**item))
            except Exception as exc:
                logger.warning(f"[SignalAgent] Parse failed for signal {i}: {exc}")
                results.append(None)

        # Pad if fewer results than batch
        while len(results) < len(signals):
            results.append(None)

        parsed_count = sum(1 for r in results if r is not None)
        logger.info(
            f"[SignalAgent] Parsed {parsed_count}/{len(batch)} signals "
            f"via tool use"
        )

        return results
