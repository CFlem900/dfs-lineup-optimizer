"""Agent 4 — Narrative-Driven Analysis Reports.

Takes structured LineupAnalysisResult data and generates a
human-readable prose narrative explaining the grade, risks,
and recommendations.

Uses **reasoning tier** (Sonnet / GPT-4o) — quality prose,
supports SSE streaming.
"""

import logging
from typing import Dict, Optional

from app.models.ai import AIRequest
from app.services.agents.sport_context import get_sport_preamble
from app.services.ai_service import AIService

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are an NBA DFS lineup analyst writing concise, actionable analysis.

Given structured lineup analysis data (grades, dimension scores, risks,
swap suggestions), write a narrative analysis using these sections:

## Grade Overview
One sentence on the grade and what it means overall.

## Strengths
- Bullet each key strength (dimension scores, value spots, stacking)
- Use **bold** for player names and numbers

## Risks
- Bullet each risk with severity context
- Name specific players and why they're risky

## Recommended Moves
- Bullet the top 1-2 swap suggestions with reasoning
- Include salary and FP delta numbers

## Verdict
One sentence: is this lineup tournament-ready, cash-viable, or needs work?

Rules:
- Be concise and direct — no filler
- Use player names, not "Player A"
- Include specific numbers (salary, FP projections) in **bold**
- Use DFS terminology naturally (chalk, leverage, stack, GPP, cash)
- Use the exact ## headers above so the UI can parse them
- Use - for bullet points, **bold** for emphasis
- Keep each section to 1-3 bullets max
"""


class NarrativeAgent:
    """Generates human-readable analysis reports from structured data."""

    def __init__(self, ai_service: AIService):
        self._ai = ai_service

    @property
    def is_available(self) -> bool:
        return self._ai.is_available

    def generate_narrative(
        self,
        analysis: Dict,
        lineup: Dict,
        platform: str = "dk",
        sport: str = "nba",
    ) -> Optional[str]:
        """Generate a full narrative report (non-streaming).

        Parameters
        ----------
        analysis : dict
            LineupAnalysisResult data (overall_grade, dimension_scores,
            risks, swap_suggestions, correlation_notes).
        lineup : dict
            OptimizedLineup data (players, total_salary, total_projected_fp).
        platform : str

        Returns None if AI unavailable.
        """
        if not self._ai.is_available:
            return None

        user_prompt = self._build_prompt(analysis, lineup, platform)

        request = AIRequest(
            system_prompt=get_sport_preamble(sport) + SYSTEM_PROMPT,
            user_prompt=user_prompt,
            model_tier="reasoning",
            max_tokens=1024,
            temperature=0.5,
            agent_name="narrative",
        )

        response = self._ai.complete(request)
        if response is None:
            return None

        return response.content

    def stream_narrative(
        self,
        analysis: Dict,
        lineup: Dict,
        platform: str = "dk",
        sport: str = "nba",
    ):
        """Generator yielding text chunks for SSE streaming.

        Yields str chunks that can be sent as SSE data events.
        """
        if not self._ai.is_available:
            return

        user_prompt = self._build_prompt(analysis, lineup, platform)

        request = AIRequest(
            system_prompt=get_sport_preamble(sport) + SYSTEM_PROMPT,
            user_prompt=user_prompt,
            model_tier="reasoning",
            max_tokens=1024,
            temperature=0.5,
            stream=True,
            agent_name="narrative",
        )

        yield from self._ai.stream(request)

    def _build_prompt(self, analysis: Dict, lineup: Dict, platform: str) -> str:
        """Build the user prompt from structured data."""
        players = lineup.get("players", [])
        player_lines = []
        for p in players:
            player_lines.append(
                f"  {p.get('roster_slot', '?')}: {p.get('player_name', '?')} "
                f"({p.get('team_abbreviation', '?')}) "
                f"${p.get('salary', 0):,} | {p.get('projected_fp', 0):.1f}FP"
            )

        dims = analysis.get("dimension_scores", [])
        dim_lines = []
        for d in dims:
            dim_lines.append(
                f"  {d.get('dimension', '?')}: {d.get('score', 0)}/10 — {d.get('detail', '')}"
            )

        risks = analysis.get("risks", [])
        risk_lines = []
        for r in risks:
            risk_lines.append(
                f"  [{r.get('severity', '?').upper()}] {r.get('description', '')} "
                f"({', '.join(r.get('affected_players', []))})"
            )

        swaps = analysis.get("swap_suggestions", [])
        swap_lines = []
        for s in swaps:
            delta = s.get("projected_fp_delta", 0)
            swap_lines.append(
                f"  {s.get('current_player', '?')} -> {s.get('suggested_player', '?')} "
                f"({delta:+.1f}FP) — {s.get('reason', '')}"
            )

        return (
            f"**Platform**: {platform.upper()}\n"
            f"**Grade**: {analysis.get('overall_grade', '?')} "
            f"(Score: {analysis.get('overall_score', 0):.0f}/100)\n"
            f"**Salary**: ${lineup.get('total_salary', 0):,} / "
            f"${lineup.get('salary_cap', 50000):,} "
            f"(${lineup.get('salary_remaining', 0):,} remaining)\n"
            f"**Projected**: {lineup.get('total_projected_fp', 0):.1f}FP "
            f"(Floor: {lineup.get('total_floor_fp', 0):.1f}, "
            f"Ceiling: {lineup.get('total_ceiling_fp', 0):.1f})\n\n"
            f"**Lineup**:\n" + "\n".join(player_lines) + "\n\n"
            f"**Dimension Scores**:\n" + "\n".join(dim_lines) + "\n\n"
            f"**Risks**:\n" + ("\n".join(risk_lines) if risk_lines else "  None") + "\n\n"
            f"**Swap Suggestions**:\n" + ("\n".join(swap_lines) if swap_lines else "  None") + "\n\n"
            f"**Correlation Notes**: {', '.join(analysis.get('correlation_notes', []))}\n\n"
            f"Write a narrative analysis of this lineup."
        )
