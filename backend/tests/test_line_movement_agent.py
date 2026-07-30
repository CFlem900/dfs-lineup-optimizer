"""Tests for Phase 4: Vegas Line Movement Agent.

Tests cover:
1. LineMovementAgent rule-based analysis
2. Significance detection thresholds
3. Ceiling and ownership adjustments
4. OddsService snapshot and movement tracking
5. Pydantic model validation
6. Constants verification
"""

import pytest
from typing import Dict, List
from unittest.mock import MagicMock, patch

from app.services.agents.line_movement_agent import LineMovementAgent
from app.models.ai import GameLineMovement, LineMovementAnalysis
from app.services.odds_service import OddsService


# ============================================================================
# HELPERS
# ============================================================================

def _make_movement(
    home: str = "BOS",
    away: str = "LAL",
    opening_total: float = 225.0,
    current_total: float = 225.0,
    opening_spread: float = -5.0,
    current_spread: float = -5.0,
) -> Dict:
    return {
        "home_team": home,
        "away_team": away,
        "opening_total": opening_total,
        "current_total": current_total,
        "opening_spread": opening_spread,
        "current_spread": current_spread,
    }


# ============================================================================
# AGENT TESTS
# ============================================================================


class TestLineMovementAgent:
    """Tests for LineMovementAgent rule-based analysis."""

    def test_agent_always_available(self):
        """Agent should be available even without AI service."""
        agent = LineMovementAgent(ai_service=None)
        assert agent.is_available is True

    def test_empty_movements_returns_none(self):
        """Empty input should return None."""
        agent = LineMovementAgent()
        result = agent.analyze_line_movements([])
        assert result is None

    def test_no_movement_not_significant(self):
        """No change in lines should not be flagged as significant."""
        agent = LineMovementAgent()
        movements = [_make_movement()]
        result = agent.analyze_line_movements(movements)
        assert result is not None
        assert result.significant_count == 0
        assert result.game_movements[0].is_significant is False

    def test_significant_total_increase(self):
        """Total going UP by >= 2.0 should be significant with ceiling boost."""
        from app.config.constants import LINE_MOVEMENT_CEILING_BOOST

        agent = LineMovementAgent()
        movements = [_make_movement(
            opening_total=225.0, current_total=228.0,  # +3.0
        )]
        result = agent.analyze_line_movements(movements)
        gm = result.game_movements[0]

        assert gm.is_significant is True
        assert gm.total_delta == 3.0
        assert gm.ceiling_adjustment == LINE_MOVEMENT_CEILING_BOOST
        assert gm.ownership_shift > 0

    def test_significant_total_decrease(self):
        """Total going DOWN by >= 2.0 should be significant with ceiling penalty."""
        from app.config.constants import LINE_MOVEMENT_CEILING_PENALTY

        agent = LineMovementAgent()
        movements = [_make_movement(
            opening_total=225.0, current_total=221.0,  # -4.0
        )]
        result = agent.analyze_line_movements(movements)
        gm = result.game_movements[0]

        assert gm.is_significant is True
        assert gm.total_delta == -4.0
        assert gm.ceiling_adjustment == -LINE_MOVEMENT_CEILING_PENALTY
        assert gm.ownership_shift < 0

    def test_significant_spread_movement(self):
        """Spread moving >= 1.5 should be significant."""
        agent = LineMovementAgent()
        movements = [_make_movement(
            opening_spread=-5.0, current_spread=-3.0,  # +2.0
        )]
        result = agent.analyze_line_movements(movements)
        gm = result.game_movements[0]

        assert gm.is_significant is True
        assert gm.spread_delta == 2.0

    def test_below_threshold_not_significant(self):
        """Movement below thresholds should not be significant."""
        agent = LineMovementAgent()
        movements = [_make_movement(
            opening_total=225.0, current_total=226.0,  # +1.0 (below 2.0)
            opening_spread=-5.0, current_spread=-4.5,  # +0.5 (below 1.5)
        )]
        result = agent.analyze_line_movements(movements)
        gm = result.game_movements[0]

        assert gm.is_significant is False
        assert gm.ceiling_adjustment == 0.0

    def test_multiple_games_counted(self):
        """Multiple significant games should all be counted."""
        agent = LineMovementAgent()
        movements = [
            _make_movement(home="BOS", away="LAL",
                          opening_total=225.0, current_total=220.0),
            _make_movement(home="MIA", away="NYK",
                          opening_total=210.0, current_total=215.0),
            _make_movement(home="GSW", away="DEN"),  # no movement
        ]
        result = agent.analyze_line_movements(movements)
        assert result.significant_count == 2
        assert len(result.game_movements) == 3

    def test_get_game_adjustments(self):
        """get_game_adjustments should return per-game adjustment dict."""
        agent = LineMovementAgent()
        movements = [
            _make_movement(home="BOS", away="LAL",
                          opening_total=225.0, current_total=220.0),
            _make_movement(home="GSW", away="DEN"),  # no movement
        ]
        adjustments = agent.get_game_adjustments(movements)

        assert "BOS vs LAL" in adjustments
        assert "ceiling_adj" in adjustments["BOS vs LAL"]
        assert "GSW vs DEN" not in adjustments  # not significant

    def test_reasoning_populated(self):
        """Significant movements should have non-empty reasoning."""
        agent = LineMovementAgent()
        movements = [_make_movement(
            opening_total=225.0, current_total=230.0,
        )]
        result = agent.analyze_line_movements(movements)
        gm = result.game_movements[0]

        assert len(gm.reasoning) > 0
        assert "Total UP" in gm.reasoning


# ============================================================================
# ODDS SERVICE SNAPSHOT TESTS
# ============================================================================


class TestOddsServiceSnapshots:
    """Tests for OddsService line movement tracking."""

    def test_capture_snapshot_no_api_key(self):
        """Snapshot capture should return 0 when no API key."""
        svc = OddsService(api_key="")
        count = svc.capture_snapshot("nba")
        assert count == 0

    def test_get_line_movements_empty(self):
        """No snapshots should return empty movements."""
        svc = OddsService(api_key="")
        movements = svc.get_line_movements("nba")
        assert movements == []

    def test_odds_service_is_available(self):
        """Service availability based on API key."""
        svc_no_key = OddsService(api_key="")
        assert svc_no_key.is_available is False

        svc_with_key = OddsService(api_key="test-key-123")
        assert svc_with_key.is_available is True


# ============================================================================
# PYDANTIC MODEL TESTS
# ============================================================================


class TestPydanticModels:
    """Tests for line movement Pydantic models."""

    def test_game_line_movement_defaults(self):
        """GameLineMovement should have sensible defaults."""
        gm = GameLineMovement(home_team="BOS", away_team="LAL")
        assert gm.total_delta == 0.0
        assert gm.spread_delta == 0.0
        assert gm.is_significant is False
        assert gm.ceiling_adjustment == 0.0

    def test_line_movement_analysis_defaults(self):
        """LineMovementAnalysis should have sensible defaults."""
        analysis = LineMovementAnalysis()
        assert analysis.game_movements == []
        assert analysis.significant_count == 0

    def test_game_line_movement_serialization(self):
        """Model should serialize to dict."""
        gm = GameLineMovement(
            home_team="BOS", away_team="LAL",
            total_delta=3.0, is_significant=True,
            ceiling_adjustment=0.05, reasoning="test",
        )
        data = gm.model_dump()
        assert data["home_team"] == "BOS"
        assert data["total_delta"] == 3.0
        assert data["is_significant"] is True


# ============================================================================
# CONSTANTS TESTS
# ============================================================================


class TestLineMovementConstants:
    """Tests for line movement constants."""

    def test_constants_importable(self):
        """All line movement constants should be importable."""
        from app.config.constants import (
            LINE_MOVEMENT_TOTAL_SIGNIFICANT,
            LINE_MOVEMENT_SPREAD_SIGNIFICANT,
            LINE_MOVEMENT_CEILING_BOOST,
            LINE_MOVEMENT_CEILING_PENALTY,
        )
        assert LINE_MOVEMENT_TOTAL_SIGNIFICANT == 2.0
        assert LINE_MOVEMENT_SPREAD_SIGNIFICANT == 1.5
        assert LINE_MOVEMENT_CEILING_BOOST == 0.05
        assert LINE_MOVEMENT_CEILING_PENALTY == 0.03

    def test_boost_greater_than_penalty(self):
        """Ceiling boost should be greater than penalty (asymmetric)."""
        from app.config.constants import (
            LINE_MOVEMENT_CEILING_BOOST,
            LINE_MOVEMENT_CEILING_PENALTY,
        )
        assert LINE_MOVEMENT_CEILING_BOOST > LINE_MOVEMENT_CEILING_PENALTY


# ============================================================================
# DEPENDENCIES WIRING TEST
# ============================================================================


class TestDependenciesWiring:
    """Test that the agent is wired into the service container."""

    def test_line_movement_agent_in_container(self):
        """ServiceContainer should have line_movement_agent."""
        from app.api.dependencies import ServiceContainer
        container = ServiceContainer()
        container._ensure_init()
        assert hasattr(container, "line_movement_agent")
        assert container.line_movement_agent.is_available is True


# ============================================================================
# BDL LIVE ODDS INTEGRATION TESTS
# ============================================================================


class TestBDLOddsIntegration:
    """Tests for LineMovementAgent BDL V2 live odds gateway."""

    def test_fetch_live_odds_no_bdl(self):
        """Returns {} when no BDL service is provided."""
        agent = LineMovementAgent(ai_service=None, bdl_service=None)
        result = agent.fetch_live_odds("2026-02-24")
        assert result == {}

    def test_fetch_live_odds_bdl_unavailable(self):
        """Returns {} when BDL service is not available."""
        mock_bdl = MagicMock()
        mock_bdl.is_available = False
        agent = LineMovementAgent(ai_service=None, bdl_service=mock_bdl)
        result = agent.fetch_live_odds("2026-02-24")
        assert result == {}

    def test_fetch_live_odds_with_bdl(self):
        """Returns team-keyed dict with spread/over_under from BDL."""
        mock_bdl = MagicMock()
        mock_bdl.is_available = True
        mock_bdl.get_games.return_value = [
            {
                "id": 9001,
                "home_team": {"abbreviation": "BOS"},
                "visitor_team": {"abbreviation": "LAL"},
            }
        ]
        mock_bdl.get_odds_by_game.return_value = {
            9001: {
                "spread_home": -7.5,
                "total": 224.5,
                "vendor": "DraftKings",
            }
        }

        agent = LineMovementAgent(ai_service=None, bdl_service=mock_bdl)
        result = agent.fetch_live_odds("2026-02-24")

        assert "BOS" in result
        assert "LAL" in result
        assert result["BOS"]["spread"] == -7.5
        assert result["BOS"]["over_under"] == 224.5
        assert result["BOS"]["vendor"] == "DraftKings"
        assert result["BOS"]["bdl_game_id"] == 9001

    def test_fetch_live_odds_caching(self):
        """Second call within TTL hits cache; BDL called only once."""
        mock_bdl = MagicMock()
        mock_bdl.is_available = True
        mock_bdl.get_games.return_value = [
            {
                "id": 9001,
                "home_team": {"abbreviation": "BOS"},
                "visitor_team": {"abbreviation": "LAL"},
            }
        ]
        mock_bdl.get_odds_by_game.return_value = {
            9001: {
                "spread_home": -7.5,
                "total": 224.5,
                "vendor": "DraftKings",
            }
        }

        agent = LineMovementAgent(ai_service=None, bdl_service=mock_bdl)
        result1 = agent.fetch_live_odds("2026-02-24")
        result2 = agent.fetch_live_odds("2026-02-24")

        # Both calls should return the same data
        assert result1 == result2
        # BDL should have been called only once
        assert mock_bdl.get_games.call_count == 1
        assert mock_bdl.get_odds_by_game.call_count == 1

    def test_fetch_live_odds_error_graceful(self):
        """BDL error returns {} gracefully (no exception raised)."""
        mock_bdl = MagicMock()
        mock_bdl.is_available = True
        mock_bdl.get_games.side_effect = RuntimeError("BDL API down")

        agent = LineMovementAgent(ai_service=None, bdl_service=mock_bdl)
        result = agent.fetch_live_odds("2026-02-24")
        assert result == {}

    def test_convenience_get_live_spread(self):
        """get_live_spread_for_team returns spread for a team."""
        mock_bdl = MagicMock()
        mock_bdl.is_available = True
        mock_bdl.get_games.return_value = [
            {
                "id": 9001,
                "home_team": {"abbreviation": "CHI"},
                "visitor_team": {"abbreviation": "MIA"},
            }
        ]
        mock_bdl.get_odds_by_game.return_value = {
            9001: {
                "spread_home": -3.0,
                "total": 218.0,
                "vendor": "FanDuel",
            }
        }

        agent = LineMovementAgent(ai_service=None, bdl_service=mock_bdl)
        spread = agent.get_live_spread_for_team("2026-02-24", "CHI")
        assert spread == -3.0

    def test_convenience_get_live_over_under(self):
        """get_live_over_under_for_team returns O/U for a team."""
        mock_bdl = MagicMock()
        mock_bdl.is_available = True
        mock_bdl.get_games.return_value = [
            {
                "id": 9001,
                "home_team": {"abbreviation": "CHI"},
                "visitor_team": {"abbreviation": "MIA"},
            }
        ]
        mock_bdl.get_odds_by_game.return_value = {
            9001: {
                "spread_home": -3.0,
                "total": 218.0,
                "vendor": "FanDuel",
            }
        }

        agent = LineMovementAgent(ai_service=None, bdl_service=mock_bdl)
        ou = agent.get_live_over_under_for_team("2026-02-24", "chi")
        assert ou == 218.0

    def test_convenience_unknown_team_returns_none(self):
        """Convenience methods return None for unknown teams."""
        agent = LineMovementAgent(ai_service=None, bdl_service=None)
        assert agent.get_live_spread_for_team("2026-02-24", "XYZ") is None
        assert agent.get_live_over_under_for_team("2026-02-24", "XYZ") is None


# ============================================================================
# GAME CONTEXT MODIFIER TESTS
# ============================================================================


class TestGameContextModifier:
    """Tests for GameContextModifier computation from live odds.

    The compute_game_context_modifier_for_game() method translates live
    Vegas odds into pre-computed blowout penalties, implied team totals,
    and pace modifiers consumed by the RotationEngine.
    """

    def test_blowout_game_modifiers(self):
        """10-point spread should produce ~3.2% starter penalty (non-linear).

        Penalty math: excess = 10.0 - 7.0 = 3.0
                      penalty = 3.0^0.70 * 0.015 ≈ 0.0324
                      starter_factor = 1.0 - 0.0324 = 0.9676
                      bench_factor = 1.0 + 0.0324 = 1.0324
        """
        agent = LineMovementAgent()
        modifier = agent.compute_game_context_modifier_for_game(
            spread=-10.0, over_under=220.0,
            home_team="BOS", away_team="LAL",
        )
        assert modifier.is_blowout_risk is True
        assert modifier.blowout_factor_starters == pytest.approx(0.9676, abs=0.001)
        assert modifier.blowout_factor_bench == pytest.approx(1.0324, abs=0.001)
        assert modifier.home_team == "BOS"
        assert modifier.away_team == "LAL"

    def test_close_game_no_blowout(self):
        """5-point spread should NOT trigger blowout risk."""
        agent = LineMovementAgent()
        modifier = agent.compute_game_context_modifier_for_game(
            spread=-5.0, over_under=225.0,
            home_team="BOS", away_team="LAL",
        )
        assert modifier.is_blowout_risk is False
        assert modifier.blowout_factor_starters == 1.0
        assert modifier.blowout_factor_bench == 1.0

    def test_exact_threshold_triggers_blowout(self):
        """Spread exactly at 7.0 should trigger blowout but with factor=1.0."""
        agent = LineMovementAgent()
        modifier = agent.compute_game_context_modifier_for_game(
            spread=-7.0, over_under=225.0,
            home_team="BOS", away_team="LAL",
        )
        assert modifier.is_blowout_risk is True
        # excess = 0.0, penalty = 0.0, so factor stays 1.0
        assert modifier.blowout_factor_starters == 1.0

    def test_implied_totals_home_favourite(self):
        """Implied totals: home favourite (negative spread).

        With O/U=225, spread=-7.5:
          home = (225 - (-7.5)) / 2 = 116.25
          away = (225 + (-7.5)) / 2 = 108.75
        """
        agent = LineMovementAgent()
        modifier = agent.compute_game_context_modifier_for_game(
            spread=-7.5, over_under=225.0,
            home_team="BOS", away_team="LAL",
        )
        assert modifier.implied_home_total == pytest.approx(116.25, abs=0.01)
        assert modifier.implied_away_total == pytest.approx(108.75, abs=0.01)

    def test_implied_totals_away_favourite(self):
        """Implied totals: away favourite (positive spread).

        With O/U=220, spread=+4.0:
          home = (220 - 4.0) / 2 = 108.0
          away = (220 + 4.0) / 2 = 112.0
        """
        agent = LineMovementAgent()
        modifier = agent.compute_game_context_modifier_for_game(
            spread=4.0, over_under=220.0,
            home_team="CHA", away_team="MIL",
        )
        assert modifier.implied_home_total == pytest.approx(108.0, abs=0.01)
        assert modifier.implied_away_total == pytest.approx(112.0, abs=0.01)

    def test_implied_totals_pick_em(self):
        """Pick'em (spread=0) should split evenly."""
        agent = LineMovementAgent()
        modifier = agent.compute_game_context_modifier_for_game(
            spread=0.0, over_under=220.0,
            home_team="DEN", away_team="PHX",
        )
        assert modifier.implied_home_total == pytest.approx(110.0, abs=0.01)
        assert modifier.implied_away_total == pytest.approx(110.0, abs=0.01)

    def test_high_total_pace_boost(self):
        """O/U=240 → graduated model: (240-225)/5=3 tiers → 1.03 boost."""
        agent = LineMovementAgent()
        modifier = agent.compute_game_context_modifier_for_game(
            spread=-3.0, over_under=240.0,
            home_team="BOS", away_team="LAL",
        )
        assert modifier.high_total_pace_boost == pytest.approx(1.03, abs=0.001)

    def test_normal_total_no_pace_boost(self):
        """O/U=220 (below 225 baseline) → no pace boost."""
        agent = LineMovementAgent()
        modifier = agent.compute_game_context_modifier_for_game(
            spread=-3.0, over_under=220.0,
            home_team="BOS", away_team="LAL",
        )
        assert modifier.high_total_pace_boost == 1.0

    def test_exactly_235_no_pace_boost(self):
        """O/U=235 → graduated model: (235-225)/5=2 tiers → 1.02 boost."""
        agent = LineMovementAgent()
        modifier = agent.compute_game_context_modifier_for_game(
            spread=-3.0, over_under=235.0,
            home_team="BOS", away_team="LAL",
        )
        assert modifier.high_total_pace_boost == pytest.approx(1.02, abs=0.001)

    def test_blowout_floor_enforced(self):
        """Extreme spreads should be capped at BLOWOUT_MIN_FACTOR (0.90).

        Non-linear penalty: excess=13, penalty = 13^0.70 * 0.015 ≈ 0.0903
        starter_factor = 1.0 - 0.0903 = 0.9097 (above 0.90 floor)
        """
        agent = LineMovementAgent()
        modifier = agent.compute_game_context_modifier_for_game(
            spread=-20.0, over_under=210.0,
            home_team="BOS", away_team="LAL",
        )
        assert modifier.blowout_factor_starters >= 0.90
        assert modifier.blowout_factor_starters == pytest.approx(0.9097, abs=0.001)

    def test_bench_factor_capped(self):
        """Bench boost should be capped symmetrically at 1.10."""
        agent = LineMovementAgent()
        modifier = agent.compute_game_context_modifier_for_game(
            spread=-20.0, over_under=210.0,
            home_team="BOS", away_team="LAL",
        )
        # bench_factor capped at 1.0 + (1.0 - 0.90) = 1.10
        assert modifier.blowout_factor_bench <= 1.10

    def test_modifier_source_tag(self):
        """Source should be tagged as 'bdl_live'."""
        agent = LineMovementAgent()
        modifier = agent.compute_game_context_modifier_for_game(
            spread=-5.0, over_under=225.0,
            home_team="BOS", away_team="LAL",
        )
        assert modifier.source == "bdl_live"

    def test_modifier_spread_and_ou_stored(self):
        """Spread and O/U values should be stored on the modifier."""
        agent = LineMovementAgent()
        modifier = agent.compute_game_context_modifier_for_game(
            spread=-8.5, over_under=231.0,
            home_team="MIA", away_team="NYK",
        )
        assert modifier.spread == -8.5
        assert modifier.over_under == 231.0

    def test_positive_spread_blowout(self):
        """Positive spread (away favourite) should also trigger blowout.

        Non-linear penalty: excess=2, penalty = 2^0.70 * 0.015 ≈ 0.0244
        starter_factor = 1.0 - 0.0244 = 0.9756
        """
        agent = LineMovementAgent()
        modifier = agent.compute_game_context_modifier_for_game(
            spread=9.0, over_under=215.0,
            home_team="CHA", away_team="MIL",
        )
        assert modifier.is_blowout_risk is True
        assert modifier.blowout_factor_starters == pytest.approx(0.9756, abs=0.001)


# ============================================================================
# GAME CONTEXT MODIFIER PYDANTIC MODEL TESTS
# ============================================================================


class TestGameContextModifierModel:
    """Tests for the GameContextModifier Pydantic model itself."""

    def test_defaults(self):
        from app.models.ai import GameContextModifier as GCM
        m = GCM(home_team="BOS", away_team="LAL")
        assert m.blowout_factor_starters == 1.0
        assert m.blowout_factor_bench == 1.0
        assert m.is_blowout_risk is False
        assert m.high_total_pace_boost == 1.0
        assert m.ceiling_adjustment == 0.0
        assert m.source == "bdl_live"

    def test_serialization(self):
        from app.models.ai import GameContextModifier as GCM
        m = GCM(
            home_team="BOS", away_team="LAL",
            spread=-7.5, over_under=225.0,
            blowout_factor_starters=0.955,
            is_blowout_risk=True,
        )
        data = m.model_dump()
        assert data["home_team"] == "BOS"
        assert data["is_blowout_risk"] is True
        assert data["blowout_factor_starters"] == 0.955
