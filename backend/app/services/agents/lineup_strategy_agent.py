"""Agent 2 — Intelligent Lineup Construction with Game Theory.

Provides game-theory-aware lineup construction guidance: ownership
leverage, correlation-aware stacking, and contest-type strategies.

**New**: ``determine_contest_strategy()`` analyses DraftKings contest
metadata (field size, prize structure, contest category) to produce a
``LineupStrategy`` that controls the optimizer's solver path and
scoring weights.

Uses **reasoning tier** (Sonnet / GPT-4o) — complex game theory,
called once per lineup generation batch.
"""

import logging
from typing import Dict, List, Optional

from app.models.ai import AIRequest, LineupStrategy, StrategyAdjustments
from app.services.agents.sport_context import get_sport_preamble
from app.services.ai_service import AIService

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────

_NON_STANDARD_KEYWORDS = [
    "satellite", "qualifier", "multiplier", "step", "mega",
    "ticket", "seat", "madness", "special",
]

_LARGE_GPP_THRESHOLD = 5000  # max_entries above this → sim-optimal

# ── System prompts ───────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a DFS (Daily Fantasy Sports) game theory strategist.

Given a player pool with ownership projections and a contest type, provide
strategic guidance for lineup construction.

**Contest Types**:
- "gpp" (Guaranteed Prize Pool / tournament): Need to be DIFFERENT. Fade high-ownership
  plays, seek leverage (low ownership + high ceiling). Stacking is key.
- "cash" (50/50, Double-Up): Need to be SAFE. Maximize floor. Avoid variance.
  Use the most-projected players regardless of ownership.
- "single_entry": Between GPP and cash. Slight leverage but don't get too cute.

**Strategy Elements**:
1. **Player score modifiers**: Adjust player scores based on ownership leverage
   - GPP: Lower score for high-ownership, boost low-ownership high-ceiling plays
   - Cash: No ownership adjustment, just project accuracy
2. **Recommended stacks**: Groups of 2-3 players from same game/team to correlate
3. **Leverage plays**: Low ownership + high ceiling players (GPP gold)
4. **Avoid players**: Chalk traps (high ownership, modest ceiling)

Return JSON:
{
  "player_score_modifiers": { "player_id": multiplier, ... },
  "recommended_stacks": [[pid1, pid2], [pid3, pid4, pid5]],
  "leverage_plays": [pid1, pid2],
  "avoid_players": [pid1],
  "reasoning": "2-3 sentence strategy summary"
}

Multipliers: 0.8 = reduce score 20%, 1.2 = boost 20%. Keep between 0.7-1.3.
Only include players that need adjustment (skip 1.0 multiplier players).
"""

_AI_CONTEST_STRATEGY_PROMPT = """You are a DFS contest strategy classifier.

Given contest metadata from DraftKings, determine the optimal solver
configuration. The contest may be non-standard (satellite, qualifier,
multiplier, etc.) and requires nuanced judgment.

Return JSON with these exact fields:
{
  "solver_path": "composite" or "sim_optimal",
  "strategy_override": "max_projection"|"balanced"|"ceiling"|"contrarian"|"pure_max"|null,
  "contest_type_override": "gpp"|"cash"|"single_entry"|null,
  "w_p50": 0.0-1.0,
  "w_p90": 0.0-1.0,
  "w_floor": 0.0-1.0,
  "ownership_leverage_alpha": 0.0-2.0,
  "ownership_leverage_baseline": 1.0-50.0,
  "enable_stacking": true/false,
  "reasoning": "1-2 sentence explanation",
  "contest_category": "satellite"|"qualifier"|"multiplier"|etc.
}

Guidelines:
- Satellites/qualifiers: Top N win seats. Play like cash if N is large
  relative to field, or like GPP if only top 1-5% win.
- Multipliers: Everyone above a threshold multiplies entry. Play like cash.
- Featured/special: Treat as GPP unless prize structure says otherwise.
- w_p50 + w_p90 + w_floor should sum to approximately 1.0.
"""


class LineupStrategyAgent:
    """Game-theory-aware lineup construction guidance."""

    def __init__(self, ai_service: AIService):
        self._ai = ai_service

    @property
    def is_available(self) -> bool:
        return self._ai.is_available

    # ------------------------------------------------------------------
    # Contest-level strategy determination (NEW)
    # ------------------------------------------------------------------

    def determine_contest_strategy(self, contest) -> LineupStrategy:
        """Determine solver path and scoring weights from contest metadata.

        Accepts a ``ContestDetail`` object from ``DKContestDetailService``.

        Decision tree
        -------------
        1. Non-standard (satellite, qualifier, multiplier) → AI fallback
        2. Cash / H2H → median-optimal (heavy P50 + floor, alpha=0.0)
        3. Single-entry GPP → balanced composite
        4. Large GPP (≥ 5 000 entries) → sim-optimal (MC iteration ILP)
        5. Small-Medium GPP (< 5 000 entries) → composite (40% P50 / 60% P90)
        """
        name_lower = contest.contest_name.lower()

        # ── Non-standard contest types → AI reasoning ────────────────
        if any(kw in name_lower for kw in _NON_STANDARD_KEYWORDS):
            logger.info(
                f"[ContestStrategy] Non-standard contest detected: "
                f"{contest.contest_name!r} — delegating to AI"
            )
            return self._ai_contest_strategy(contest)

        inferred = contest.inferred_contest_type()  # "gpp", "cash", "single_entry"

        # ── Cash / H2H ──────────────────────────────────────────────
        if inferred == "cash":
            return LineupStrategy(
                solver_path="composite",
                strategy_override="balanced",
                contest_type_override="cash",
                w_p50=0.65,
                w_p90=0.10,
                w_floor=0.25,
                ownership_leverage_alpha=0.0,
                ownership_leverage_baseline=10.0,
                enable_stacking=False,
                min_stack_size=0,
                noise_floor=0.97,
                noise_ceiling=1.03,
                overgen_multiplier=2.0,
                source="rules",
                reasoning=(
                    f"Cash game ({contest.contest_name}): median-optimal build "
                    f"prioritising floor safety with no ownership leverage."
                ),
                contest_category="cash",
            )

        # ── Single-entry GPP ────────────────────────────────────────
        if inferred == "single_entry":
            return LineupStrategy(
                solver_path="composite",
                strategy_override="balanced",
                contest_type_override="single_entry",
                w_p50=0.45,
                w_p90=0.45,
                w_floor=0.10,
                ownership_leverage_alpha=0.40,
                ownership_leverage_baseline=12.0,
                enable_stacking=True,
                min_stack_size=2,
                noise_floor=0.93,
                noise_ceiling=1.07,
                overgen_multiplier=3.0,
                source="rules",
                reasoning=(
                    f"Single-entry GPP ({contest.contest_name}): balanced "
                    f"composite with moderate ownership leverage."
                ),
                contest_category="single_entry_gpp",
            )

        # ── GPP: size classification ────────────────────────────────
        field_size = contest.max_entries

        if field_size >= _LARGE_GPP_THRESHOLD:
            # Large GPP → sim-optimal
            return LineupStrategy(
                solver_path="sim_optimal",
                strategy_override=None,
                contest_type_override="gpp",
                w_p50=0.0,
                w_p90=0.0,
                w_floor=0.0,
                ownership_leverage_alpha=0.70,
                ownership_leverage_baseline=8.0,
                enable_stacking=True,
                min_stack_size=2,
                noise_floor=0.90,
                noise_ceiling=1.10,
                overgen_multiplier=3.0,
                source="rules",
                reasoning=(
                    f"Large GPP ({contest.contest_name}, {field_size:,} entries): "
                    f"sim-optimal with Monte Carlo iteration ILP for maximum "
                    f"ceiling correlation."
                ),
                contest_category="large_gpp",
            )

        # Small-Medium GPP → composite with ceiling lean
        alpha = round(0.50 + (field_size / _LARGE_GPP_THRESHOLD) * 0.20, 2)
        top_heavy = contest.top_heavy_pct

        if top_heavy > 50:
            w_p90 = 0.70
            w_p50 = 0.30
            strat_override = "ceiling"
        else:
            w_p90 = 0.60
            w_p50 = 0.40
            strat_override = None

        return LineupStrategy(
            solver_path="composite",
            strategy_override=strat_override,
            contest_type_override="gpp",
            w_p50=w_p50,
            w_p90=w_p90,
            w_floor=0.0,
            ownership_leverage_alpha=alpha,
            ownership_leverage_baseline=10.0,
            enable_stacking=True,
            min_stack_size=2,
            noise_floor=0.90,
            noise_ceiling=1.10,
            overgen_multiplier=3.0,
            source="rules",
            reasoning=(
                f"Small/Medium GPP ({contest.contest_name}, {field_size:,} entries, "
                f"top-heavy {top_heavy:.0f}%): composite scoring with "
                f"{w_p50:.0%} P50 / {w_p90:.0%} P90 blend, alpha={alpha:.2f}."
            ),
            contest_category="small_medium_gpp",
        )

    # ------------------------------------------------------------------
    # AI fallback for non-standard contests
    # ------------------------------------------------------------------

    def _ai_contest_strategy(self, contest) -> LineupStrategy:
        """Use Claude Sonnet reasoning tier for non-standard contest types."""
        if not self._ai.is_available:
            logger.warning(
                "[ContestStrategy] AI unavailable for non-standard contest, "
                "defaulting to small GPP"
            )
            return self._default_gpp_strategy(contest)

        user_prompt = (
            f"Contest: {contest.contest_name}\n"
            f"Entry Fee: ${contest.entry_fee:.2f}\n"
            f"Total Prizes: ${contest.total_prizes:,.0f}\n"
            f"Max Entries: {contest.max_entries:,}\n"
            f"Max Entries Per User: {contest.max_entries_per_user}\n"
            f"Current Entries: {contest.current_entries:,}\n"
            f"Contest Type (DK): {contest.contest_type}\n"
            f"Is Guaranteed: {contest.is_guaranteed}\n"
            f"Top-Heavy %: {contest.top_heavy_pct:.1f}%\n"
            f"Breakeven Percentile: {contest.roi_breakeven_percentile:.1f}%\n"
            f"Overlay Risk: {contest.overlay_risk:.1f}%\n"
        )

        request = AIRequest(
            system_prompt=_AI_CONTEST_STRATEGY_PROMPT,
            user_prompt=user_prompt,
            model_tier="reasoning",
            max_tokens=1024,
            temperature=0.3,
            response_format="json",
            agent_name="contest_strategy",
        )

        data = self._ai.complete_json(request)
        if data is None:
            logger.warning("[ContestStrategy] AI returned None, using defaults")
            return self._default_gpp_strategy(contest)

        try:
            # Validate and normalise weights to sum ≈ 1.0
            w_p50 = max(0.0, min(1.0, float(data.get("w_p50", 0.5))))
            w_p90 = max(0.0, min(1.0, float(data.get("w_p90", 0.5))))
            w_floor = max(0.0, min(1.0, float(data.get("w_floor", 0.0))))
            total_w = w_p50 + w_p90 + w_floor
            if total_w > 0:
                w_p50 /= total_w
                w_p90 /= total_w
                w_floor /= total_w

            solver = data.get("solver_path", "composite")
            if solver not in ("composite", "sim_optimal"):
                solver = "composite"

            strat = data.get("strategy_override")
            valid_strats = {
                "max_projection", "balanced", "ceiling",
                "contrarian", "pure_max", None,
            }
            if strat not in valid_strats:
                strat = None

            ct = data.get("contest_type_override")
            if ct not in ("gpp", "cash", "single_entry", None):
                ct = None

            result = LineupStrategy(
                solver_path=solver,
                strategy_override=strat,
                contest_type_override=ct,
                w_p50=round(w_p50, 3),
                w_p90=round(w_p90, 3),
                w_floor=round(w_floor, 3),
                ownership_leverage_alpha=max(0.0, min(2.0,
                    float(data.get("ownership_leverage_alpha", 0.60))
                )),
                ownership_leverage_baseline=max(1.0, min(50.0,
                    float(data.get("ownership_leverage_baseline", 10.0))
                )),
                enable_stacking=bool(data.get("enable_stacking", True)),
                min_stack_size=2,
                noise_floor=0.90,
                noise_ceiling=1.10,
                overgen_multiplier=3.0,
                source="ai_fallback",
                reasoning=str(data.get("reasoning", "AI-determined strategy")),
                contest_category=str(data.get("contest_category", "non_standard")),
            )
            logger.info(
                f"[ContestStrategy] AI fallback: path={result.solver_path}, "
                f"category={result.contest_category}, "
                f"weights=({result.w_p50:.2f}/{result.w_p90:.2f}/{result.w_floor:.2f})"
            )
            return result
        except Exception as exc:
            logger.warning(f"[ContestStrategy] AI parse failed: {exc}")
            return self._default_gpp_strategy(contest)

    @staticmethod
    def _default_gpp_strategy(contest) -> LineupStrategy:
        """Safe fallback: small GPP composite strategy."""
        return LineupStrategy(
            solver_path="composite",
            contest_type_override="gpp",
            w_p50=0.40,
            w_p90=0.60,
            w_floor=0.0,
            ownership_leverage_alpha=0.60,
            ownership_leverage_baseline=10.0,
            source="rules",
            reasoning=(
                f"Fallback strategy for {contest.contest_name}: "
                f"composite GPP with 40/60 P50/P90 blend."
            ),
            contest_category="unknown",
        )

    # ------------------------------------------------------------------
    # Per-player strategy adjustments (existing)
    # ------------------------------------------------------------------

    def get_strategy_adjustments(
        self,
        pool: List[Dict],
        contest_type: str = "gpp",
        ownership: Optional[Dict[int, float]] = None,
        sport: str = "nba",
    ) -> Optional[StrategyAdjustments]:
        """Get strategy adjustments for lineup construction.

        Parameters
        ----------
        pool : list of dict
            Player pool with player_id, player_name, position, salary,
            projected_fp, floor_fp, ceiling_fp, team_abbreviation, game_total.
        contest_type : str
            "gpp", "cash", or "single_entry".
        ownership : dict, optional
            player_id -> ownership % from OwnershipProjectionAgent.

        Returns None if AI unavailable.
        """
        if not self._ai.is_available or not pool:
            return None

        own = ownership or {}

        # Build player summary with ownership
        lines = []
        for p in pool[:50]:
            pid = p["player_id"]
            own_pct = own.get(pid, 0)
            lines.append(
                f"  ID:{pid} {p['player_name']} ({p['position']}) "
                f"{p.get('team_abbreviation', '?')} "
                f"${p['salary']:,} | Proj:{p['projected_fp']:.1f} "
                f"Floor:{p.get('floor_fp', 0):.1f} Ceil:{p.get('ceiling_fp', 0):.1f} "
                f"| Own:{own_pct:.0f}%"
                + (f" | Game:{p.get('game_total', '')}" if p.get('game_total') else "")
            )

        user_prompt = (
            f"**Contest type**: {contest_type.upper()}\n"
            f"**Pool size**: {len(pool)} players\n\n"
            f"Players (top 50 by projection):\n"
            + "\n".join(lines) + "\n\n"
            f"Provide strategic adjustments for {contest_type.upper()} lineup construction."
        )

        request = AIRequest(
            system_prompt=get_sport_preamble(sport) + SYSTEM_PROMPT,
            user_prompt=user_prompt,
            model_tier="reasoning",
            max_tokens=2048,
            temperature=0.4,
            response_format="json",
            agent_name="lineup_strategy",
        )

        data = self._ai.complete_json(request)
        if data is None:
            return None

        try:
            # Convert string keys to int for player_score_modifiers
            if "player_score_modifiers" in data:
                data["player_score_modifiers"] = {
                    int(k): float(v)
                    for k, v in data["player_score_modifiers"].items()
                }
            if "recommended_stacks" in data:
                data["recommended_stacks"] = [
                    [int(pid) for pid in stack]
                    for stack in data["recommended_stacks"]
                ]
            if "leverage_plays" in data:
                data["leverage_plays"] = [int(pid) for pid in data["leverage_plays"]]
            if "avoid_players" in data:
                data["avoid_players"] = [int(pid) for pid in data["avoid_players"]]

            result = StrategyAdjustments(**data)
            logger.info(
                f"[Strategy] {contest_type}: {len(result.player_score_modifiers)} modifiers, "
                f"{len(result.recommended_stacks)} stacks, "
                f"{len(result.leverage_plays)} leverage plays"
            )
            return result
        except Exception as exc:
            logger.warning(f"[StrategyAgent] Parse failed: {exc}")
            return None
