"""Agent 11 — Conversational Interface Agent.

Provides a natural language chat interface for DFS lineup management.
Users can describe constraints, ask questions about players, and
receive actionable suggestions.

Uses **reasoning tier** (Sonnet / GPT-4o) — user-facing, needs
high-quality understanding. Supports SSE streaming.
"""

import json
import logging
import uuid
from typing import Dict, List, Optional

from app.models.ai import AIRequest, ChatAction, ChatMessage, ChatRequest, ChatResponse
from app.services.agents.sport_context import get_sport_preamble
from app.services.ai_service import AIService

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are an AI assistant for an NBA DFS (Daily Fantasy Sports) lineup optimizer app.

You help users build winning DFS lineups through natural conversation. You can:

1. **Lock/exclude players**: "Lock LeBron" → lock_player action
2. **Generate lineups**: "Build me 5 ceiling lineups" → generate_lineups action
3. **Set strategy**: "Switch to GPP mode" → set_strategy action
4. **Adjust projections**: "Lower Jokic's minutes to 25" → adjust_projections action
5. **Answer questions**: "How does Jokic look tonight?" → query projection data
6. **Explain analysis**: "Why did my lineup get a B+?" → explain using analysis data
7. **Suggest changes**: "How can I improve lineup 3?" → analyze and suggest

When the user wants to TAKE AN ACTION, include a JSON action block in your response.
Format actions as a JSON array at the END of your message, wrapped in <actions>...</actions> tags:

<actions>
[{"type": "lock_player", "params": {"player_name": "LeBron James"}, "label": "Lock LeBron James"}]
</actions>

Available action types:
- lock_player: {"player_name": str}
- exclude_player: {"player_name": str}
- unlock_player: {"player_name": str}
- generate_lineups: {"num_lineups": int, "strategy": "max_projection"|"balanced"|"ceiling"}
- set_strategy: {"strategy": str}
- set_platform: {"platform": "dk"|"fd"}
- set_contest_type: {"contest_type": "gpp"|"cash"|"single_entry"}
- analyze_lineups: {}
- refine_lineups: {}
- adjust_projections: {"adjustments": [{"player_name": str, "projected_minutes": float, "projected_fp": float}]}

When the user asks you to adjust projections (e.g., "lower Jokic's minutes to 28",
"adjust minutes for guys that are questionable", or "account for injury news"), use
the adjust_projections action. Each adjustment in the array can include any combination
of: projected_minutes, projected_fp, floor_fp, ceiling_fp. Use the injury report and
player context to make informed adjustments.

You can combine multiple actions. For example, if the user says "Lock Jokic and build
5 ceiling lineups for GPP," emit both a lock_player and generate_lineups action.

If the user is just asking a question (no action needed), respond conversationally
WITHOUT the <actions> block.

Use DFS terminology naturally. Be concise but helpful.
Reference specific player names, salaries, projections, and injury statuses from the
context when answering questions.
"""


class ChatAgent:
    """Conversational interface for natural language lineup management."""

    def __init__(
        self,
        ai_service: AIService,
        lineup_optimizer=None,
        expert_signal_service=None,
        game_service=None,
        injury_service=None,
    ):
        self._ai = ai_service
        self._optimizer = lineup_optimizer
        self._experts = expert_signal_service
        self._games = game_service
        self._injuries = injury_service
        self._sessions: Dict[str, List[ChatMessage]] = {}
        self._last_session_id: Optional[str] = None

    @property
    def is_available(self) -> bool:
        return self._ai.is_available

    def chat(self, request: ChatRequest) -> ChatResponse:
        """Process a chat message and return response with optional actions.

        If AI is unavailable, returns a fallback message.
        """
        session_id = request.session_id or str(uuid.uuid4())

        if not self._ai.is_available:
            return ChatResponse(
                message="AI assistant is currently unavailable. Please use the UI controls directly.",
                actions=[],
                session_id=session_id,
            )

        # Get or create session
        if session_id not in self._sessions:
            self._sessions[session_id] = []

        history = self._sessions[session_id]

        # Add user message to history
        history.append(ChatMessage(role="user", content=request.message))

        # Build context-aware system prompt
        system = self._build_context_prompt(request.context)

        # Build conversation for the LLM
        user_prompt = self._build_conversation_prompt(history)

        ai_request = AIRequest(
            system_prompt=system,
            user_prompt=user_prompt,
            model_tier="reasoning",
            max_tokens=1024,
            temperature=0.5,
            agent_name="chat",
        )

        response = self._ai.complete(ai_request)
        if response is None:
            return ChatResponse(
                message="I'm having trouble connecting right now. Try again in a moment.",
                actions=[],
                session_id=session_id,
            )

        # Parse response and extract actions
        message_text, actions = self._extract_actions(response.content)

        # Save assistant response to history
        history.append(ChatMessage(role="assistant", content=message_text))

        # Trim history to last 20 messages to prevent context overflow
        if len(history) > 20:
            self._sessions[session_id] = history[-20:]

        return ChatResponse(
            message=message_text,
            actions=actions,
            session_id=session_id,
        )

    def stream_chat(self, request: ChatRequest):
        """Generator yielding text chunks for SSE streaming.

        Actions are extracted from the complete response by the caller
        (the SSE endpoint in routes.py) after all chunks have been sent.
        """
        session_id = request.session_id or str(uuid.uuid4())
        self._last_session_id = session_id

        if not self._ai.is_available:
            yield "AI assistant is currently unavailable. Please check that the Anthropic API key is configured and the 'anthropic' package is installed."
            return

        if session_id not in self._sessions:
            self._sessions[session_id] = []

        history = self._sessions[session_id]
        history.append(ChatMessage(role="user", content=request.message))

        system = self._build_context_prompt(request.context)
        user_prompt = self._build_conversation_prompt(history)

        ai_request = AIRequest(
            system_prompt=system,
            user_prompt=user_prompt,
            model_tier="reasoning",
            max_tokens=1024,
            temperature=0.5,
            stream=True,
            agent_name="chat",
        )

        full_text = ""
        for chunk in self._ai.stream(ai_request):
            full_text += chunk
            yield chunk

        # Save to history (strip actions block for cleaner history)
        clean_text, _ = self._extract_actions(full_text)
        history.append(ChatMessage(role="assistant", content=clean_text))

        if len(history) > 20:
            self._sessions[session_id] = history[-20:]

    def _build_context_prompt(self, context: Optional[Dict]) -> str:
        """Build system prompt enriched with current app state and live service data."""
        sport = (context or {}).get("sport", "nba")
        prompt = get_sport_preamble(sport) + SYSTEM_PROMPT

        if not context:
            # Still append live service data even without frontend context
            service_ctx = self._enrich_with_services(context)
            if service_ctx:
                prompt += "\n\n--- LIVE DATA ---\n" + service_ctx
            return prompt

        ctx_parts = ["\n\n--- CURRENT APP CONTEXT ---"]

        if context.get("platform"):
            ctx_parts.append(f"Platform: {context['platform'].upper()}")
        if context.get("slate"):
            ctx_parts.append(f"Slate: {context['slate']}")
        if context.get("contest_type"):
            ctx_parts.append(f"Contest type: {context['contest_type']}")
        if context.get("num_lineups"):
            ctx_parts.append(f"Generated lineups: {context['num_lineups']}")
        if context.get("strategy"):
            ctx_parts.append(f"Strategy: {context['strategy']}")
        if context.get("locked_players"):
            ctx_parts.append(f"Locked players: {', '.join(context['locked_players'])}")
        if context.get("excluded_players"):
            ctx_parts.append(f"Excluded players: {', '.join(context['excluded_players'])}")
        if context.get("analysis_summary"):
            ctx_parts.append(f"Analysis: {context['analysis_summary']}")
        if context.get("optimizing"):
            ctx_parts.append("NOTE: Lineup optimization is currently running — wait before generating.")

        # Rich player context from frontend
        if context.get("top_projected"):
            ctx_parts.append("Top projected players:")
            for p in context["top_projected"][:10]:
                ctx_parts.append(f"  - {p}")

        if context.get("injury_players"):
            ctx_parts.append("Low-minutes players (possible injury impact):")
            for p in context["injury_players"][:8]:
                ctx_parts.append(f"  - {p}")

        if context.get("lineup_summary"):
            summaries = context["lineup_summary"]
            ctx_parts.append(f"Current lineups ({len(summaries)}):")
            for s in summaries[:5]:
                ctx_parts.append(
                    f"  #{s['index'] + 1}: ${s['total_salary']} / "
                    f"{s['total_fp']}fp — {s['players']}"
                )

        # Append live service data (injuries, games)
        service_ctx = self._enrich_with_services(context)
        if service_ctx:
            ctx_parts.append("\n--- LIVE INJURY REPORT ---")
            ctx_parts.append(service_ctx)

        return prompt + "\n".join(ctx_parts)

    def _enrich_with_services(self, context: Optional[Dict]) -> str:
        """Fetch live data from injected services to enrich the system prompt."""
        extra = []

        # Live injury report
        if self._injuries:
            try:
                all_injuries = self._injuries.get_all_injuries()
                active = [
                    inj for inj in all_injuries
                    if hasattr(inj, "status") and inj.status in (
                        "Out", "Doubtful", "Questionable", "GTD",
                        "Game Time Decision",
                    )
                ]
                if active:
                    extra.append("Current injuries:")
                    for inj in active[:25]:
                        desc = getattr(inj, "injury_description", "") or ""
                        team = getattr(inj, "team", "") or ""
                        extra.append(
                            f"  - {inj.player_name} ({team}): "
                            f"{inj.status} — {desc}"
                        )
            except Exception as e:
                logger.debug(f"[ChatAgent] Injury enrichment failed: {e}")

        return "\n".join(extra) if extra else ""

    def _build_conversation_prompt(self, history: List[ChatMessage]) -> str:
        """Build the conversation prompt from message history."""
        parts = []
        # Include last 10 messages for context
        for msg in history[-10:]:
            role = msg.role.upper()
            parts.append(f"[{role}]: {msg.content}")
        return "\n\n".join(parts)

    def _extract_actions(self, response: str) -> tuple:
        """Parse structured actions from LLM response.

        Returns (clean_message, list_of_ChatAction).
        """
        actions = []
        clean = response

        # Look for <actions>...</actions> block
        start_tag = "<actions>"
        end_tag = "</actions>"
        start_idx = response.find(start_tag)
        end_idx = response.find(end_tag)

        if start_idx != -1 and end_idx != -1:
            actions_json = response[start_idx + len(start_tag):end_idx].strip()
            clean = response[:start_idx].strip()

            try:
                raw_actions = json.loads(actions_json)
                if isinstance(raw_actions, list):
                    for a in raw_actions:
                        actions.append(ChatAction(
                            type=a.get("type", ""),
                            params=a.get("params", {}),
                            label=a.get("label", a.get("type", "")),
                        ))
            except json.JSONDecodeError as exc:
                logger.debug(f"[ChatAgent] Action parse failed: {exc}")

        return clean, actions
