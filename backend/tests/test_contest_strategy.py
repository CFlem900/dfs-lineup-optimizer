"""Tests for LineupStrategyAgent.determine_contest_strategy().

Covers the rules-based decision tree for all contest categories:
  - Cash / H2H  →  median-optimal (composite, alpha=0.0)
  - Single-entry GPP  →  balanced composite
  - Small-Medium GPP (<5000)  →  composite (40/60 P50/P90)
  - Large GPP (≥5000)  →  sim-optimal
  - Non-standard (satellite, etc.)  →  AI fallback
"""

import pytest
from unittest.mock import MagicMock

from app.models.ai import LineupStrategy
from app.services.agents.lineup_strategy_agent import (
    LineupStrategyAgent,
    _LARGE_GPP_THRESHOLD,
)


# ---------------------------------------------------------------------------
# Helpers — lightweight mock for ContestDetail
# ---------------------------------------------------------------------------

class MockContestDetail:
    """Mimics dk_contest_detail_service.ContestDetail (uses __slots__)."""

    def __init__(
        self,
        contest_name: str = "Test Contest",
        max_entries: int = 1000,
        max_entries_per_user: int = 150,
        current_entries: int = 800,
        contest_type: str = "Tournament",
        entry_fee: float = 20.0,
        total_prizes: float = 50000.0,
        is_guaranteed: bool = True,
        top_heavy_pct: float = 35.0,
        roi_breakeven_percentile: float = 25.0,
        overlay_risk: float = 5.0,
    ):
        self.contest_id = "99999"
        self.contest_name = contest_name
        self.draft_group_id = 1
        self.entry_fee = entry_fee
        self.total_prizes = total_prizes
        self.max_entries = max_entries
        self.max_entries_per_user = max_entries_per_user
        self.current_entries = current_entries
        self.contest_type = contest_type
        self.is_guaranteed = is_guaranteed
        self.prize_structure = []
        self.overlay_risk = overlay_risk
        self.top_heavy_pct = top_heavy_pct
        self.roi_breakeven_percentile = roi_breakeven_percentile
        self._inferred = None

    def inferred_contest_type(self) -> str:
        if self._inferred:
            return self._inferred
        name_lower = self.contest_name.lower()
        if self.max_entries_per_user == 1:
            return "single_entry"
        if any(kw in name_lower for kw in ["double up", "50/50", "head-to-head", "h2h"]):
            return "cash"
        if self.top_heavy_pct > 30:
            return "gpp"
        if self.top_heavy_pct < 10:
            return "cash"
        return "gpp"


def _make_agent(ai_available: bool = False) -> LineupStrategyAgent:
    """Create agent with a mock AI service."""
    ai = MagicMock()
    ai.is_available = ai_available
    return LineupStrategyAgent(ai)


# ---------------------------------------------------------------------------
# Cash / H2H
# ---------------------------------------------------------------------------

class TestCashStrategy:
    def test_double_up_returns_cash_strategy(self):
        agent = _make_agent()
        contest = MockContestDetail(
            contest_name="$5 Double Up",
            max_entries=200,
            top_heavy_pct=5.0,
        )
        result = agent.determine_contest_strategy(contest)

        assert isinstance(result, LineupStrategy)
        assert result.solver_path == "composite"
        assert result.contest_type_override == "cash"
        assert result.strategy_override == "balanced"
        assert result.ownership_leverage_alpha == 0.0
        assert result.w_p50 == 0.65
        assert result.w_p90 == 0.10
        assert result.w_floor == 0.25
        assert result.enable_stacking is False
        assert result.source == "rules"
        assert result.contest_category == "cash"

    def test_h2h_returns_cash_strategy(self):
        agent = _make_agent()
        contest = MockContestDetail(
            contest_name="$20 Head-to-Head",
            max_entries=2,
            top_heavy_pct=0.0,
        )
        result = agent.determine_contest_strategy(contest)

        assert result.solver_path == "composite"
        assert result.contest_type_override == "cash"
        assert result.ownership_leverage_alpha == 0.0

    def test_fifty_fifty_returns_cash(self):
        agent = _make_agent()
        contest = MockContestDetail(
            contest_name="$3 50/50",
            max_entries=100,
            top_heavy_pct=0.0,
        )
        result = agent.determine_contest_strategy(contest)
        assert result.contest_category == "cash"


# ---------------------------------------------------------------------------
# Single-entry GPP
# ---------------------------------------------------------------------------

class TestSingleEntryStrategy:
    def test_single_entry_returns_balanced(self):
        agent = _make_agent()
        contest = MockContestDetail(
            contest_name="$10 Single Entry Tournament",
            max_entries=5000,
            max_entries_per_user=1,
            top_heavy_pct=40.0,
        )
        result = agent.determine_contest_strategy(contest)

        assert result.solver_path == "composite"
        assert result.contest_type_override == "single_entry"
        assert result.strategy_override == "balanced"
        assert result.w_p50 == 0.45
        assert result.w_p90 == 0.45
        assert result.w_floor == 0.10
        assert result.ownership_leverage_alpha == 0.40
        assert result.enable_stacking is True
        assert result.contest_category == "single_entry_gpp"


# ---------------------------------------------------------------------------
# Small-Medium GPP (< 5000 entries)
# ---------------------------------------------------------------------------

class TestSmallMediumGPP:
    def test_small_gpp_composite_weights(self):
        agent = _make_agent()
        contest = MockContestDetail(
            contest_name="$5 GPP Tournament",
            max_entries=2000,
            top_heavy_pct=35.0,
        )
        result = agent.determine_contest_strategy(contest)

        assert result.solver_path == "composite"
        assert result.contest_type_override == "gpp"
        assert result.w_p50 == 0.40
        assert result.w_p90 == 0.60
        assert result.w_floor == 0.0
        assert result.contest_category == "small_medium_gpp"
        assert result.source == "rules"

    def test_alpha_scales_with_field_size(self):
        agent = _make_agent()
        # 2000 entries → alpha = 0.50 + (2000/5000)*0.20 = 0.58
        contest = MockContestDetail(max_entries=2000, top_heavy_pct=35.0)
        result = agent.determine_contest_strategy(contest)
        assert result.ownership_leverage_alpha == 0.58

        # 4000 entries → alpha = 0.50 + (4000/5000)*0.20 = 0.66
        contest2 = MockContestDetail(max_entries=4000, top_heavy_pct=35.0)
        result2 = agent.determine_contest_strategy(contest2)
        assert result2.ownership_leverage_alpha == 0.66

    def test_top_heavy_boosts_ceiling(self):
        agent = _make_agent()
        contest = MockContestDetail(
            contest_name="Top Heavy GPP",
            max_entries=3000,
            top_heavy_pct=55.0,
        )
        result = agent.determine_contest_strategy(contest)

        assert result.w_p90 == 0.70
        assert result.w_p50 == 0.30
        assert result.strategy_override == "ceiling"

    def test_just_below_threshold(self):
        agent = _make_agent()
        contest = MockContestDetail(
            max_entries=_LARGE_GPP_THRESHOLD - 1,
            top_heavy_pct=35.0,
        )
        result = agent.determine_contest_strategy(contest)
        assert result.solver_path == "composite"
        assert result.contest_category == "small_medium_gpp"


# ---------------------------------------------------------------------------
# Large GPP (≥ 5000 entries)
# ---------------------------------------------------------------------------

class TestLargeGPP:
    def test_large_gpp_sim_optimal(self):
        agent = _make_agent()
        contest = MockContestDetail(
            contest_name="Milly Maker $10M",
            max_entries=50000,
            top_heavy_pct=60.0,
        )
        result = agent.determine_contest_strategy(contest)

        assert result.solver_path == "sim_optimal"
        assert result.contest_type_override == "gpp"
        assert result.strategy_override is None
        assert result.ownership_leverage_alpha == 0.70
        assert result.ownership_leverage_baseline == 8.0
        assert result.contest_category == "large_gpp"

    def test_exactly_at_threshold(self):
        agent = _make_agent()
        contest = MockContestDetail(
            max_entries=_LARGE_GPP_THRESHOLD,
            top_heavy_pct=40.0,
        )
        result = agent.determine_contest_strategy(contest)
        assert result.solver_path == "sim_optimal"
        assert result.contest_category == "large_gpp"

    def test_sim_optimal_weights_zeroed(self):
        """Sim-optimal path doesn't use composite weights."""
        agent = _make_agent()
        contest = MockContestDetail(max_entries=10000, top_heavy_pct=45.0)
        result = agent.determine_contest_strategy(contest)
        assert result.w_p50 == 0.0
        assert result.w_p90 == 0.0
        assert result.w_floor == 0.0


# ---------------------------------------------------------------------------
# Non-standard contests (AI fallback)
# ---------------------------------------------------------------------------

class TestNonStandardContests:
    def test_satellite_triggers_ai_fallback(self):
        agent = _make_agent(ai_available=False)
        contest = MockContestDetail(
            contest_name="$5 Satellite to Main Event",
            max_entries=500,
        )
        result = agent.determine_contest_strategy(contest)

        # AI unavailable → falls back to default GPP
        assert result.source == "rules"
        assert result.contest_category == "unknown"
        assert result.solver_path == "composite"

    def test_qualifier_triggers_ai_fallback(self):
        agent = _make_agent(ai_available=False)
        contest = MockContestDetail(
            contest_name="$25 Qualifier Round 1",
            max_entries=1000,
        )
        result = agent.determine_contest_strategy(contest)
        assert result.contest_category == "unknown"

    def test_multiplier_triggers_ai_fallback(self):
        agent = _make_agent(ai_available=False)
        contest = MockContestDetail(
            contest_name="$1 Multiplier Mania",
            max_entries=300,
        )
        result = agent.determine_contest_strategy(contest)
        assert result.contest_category == "unknown"

    def test_satellite_with_ai_available(self):
        """When AI is available, the agent calls complete_json."""
        agent = _make_agent(ai_available=True)
        # Mock a successful AI response
        agent._ai.complete_json.return_value = {
            "solver_path": "composite",
            "strategy_override": "balanced",
            "contest_type_override": "cash",
            "w_p50": 0.60,
            "w_p90": 0.20,
            "w_floor": 0.20,
            "ownership_leverage_alpha": 0.10,
            "ownership_leverage_baseline": 10.0,
            "enable_stacking": False,
            "reasoning": "Satellite with wide payout → play like cash",
            "contest_category": "satellite",
        }
        contest = MockContestDetail(
            contest_name="$5 Satellite to Main Event",
            max_entries=500,
        )
        result = agent.determine_contest_strategy(contest)

        assert result.source == "ai_fallback"
        assert result.contest_category == "satellite"
        assert result.solver_path == "composite"
        assert result.contest_type_override == "cash"
        agent._ai.complete_json.assert_called_once()

    def test_ai_returns_none_fallback(self):
        """AI available but returns None → default GPP."""
        agent = _make_agent(ai_available=True)
        agent._ai.complete_json.return_value = None
        contest = MockContestDetail(
            contest_name="$5 Satellite to Main Event",
            max_entries=500,
        )
        result = agent.determine_contest_strategy(contest)
        assert result.source == "rules"
        assert result.contest_category == "unknown"

    def test_ai_weight_normalisation(self):
        """AI returns weights that don't sum to 1 → normalised."""
        agent = _make_agent(ai_available=True)
        agent._ai.complete_json.return_value = {
            "solver_path": "composite",
            "w_p50": 0.50,
            "w_p90": 0.50,
            "w_floor": 0.50,
            "ownership_leverage_alpha": 0.40,
            "reasoning": "Test",
            "contest_category": "special",
        }
        contest = MockContestDetail(
            contest_name="$10 Special Event",
            max_entries=1000,
        )
        result = agent.determine_contest_strategy(contest)

        # 0.5+0.5+0.5 = 1.5 → each ÷ 1.5 → 0.333
        assert abs(result.w_p50 + result.w_p90 + result.w_floor - 1.0) < 0.01


# ---------------------------------------------------------------------------
# LineupStrategy model validation
# ---------------------------------------------------------------------------

class TestLineupStrategyModel:
    def test_defaults(self):
        ls = LineupStrategy()
        assert ls.solver_path == "composite"
        assert ls.strategy_override is None
        assert ls.w_p50 == 0.50
        assert ls.w_p90 == 0.50
        assert ls.w_floor == 0.0
        assert ls.ownership_leverage_alpha == 0.60
        assert ls.source == "rules"

    def test_serialisation_roundtrip(self):
        ls = LineupStrategy(
            solver_path="sim_optimal",
            w_p50=0.0,
            w_p90=0.0,
            contest_category="large_gpp",
        )
        data = ls.model_dump()
        ls2 = LineupStrategy(**data)
        assert ls2.solver_path == "sim_optimal"
        assert ls2.contest_category == "large_gpp"
