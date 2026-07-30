"""Tests for Agent 6 — Coach Active Depth Filter.

Tests the ``get_expected_rotation_size``, ``calculate_active_depth_and_filter``,
and ``get_adjusted_profile`` methods added to ``CoachLearningAgent``.
"""

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.ai import CoachPatternUpdate
from app.models.player import PlayerProjection
from app.services.agents.coach_learning_agent import (
    CACHE_KEY_PREFIX,
    DEPTH_CACHE_PREFIX,
    CoachLearningAgent,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_projection(
    player_id: int,
    name: str = "Player",
    baseline: float = 20.0,
    adjusted: float = 20.0,
    confidence: float = 0.8,
    dk_salary: int | None = 5000,
    reason: str = "baseline",
) -> PlayerProjection:
    """Create a minimal ``PlayerProjection`` for testing."""
    return PlayerProjection(
        player_id=player_id,
        player_name=f"{name}_{player_id}",
        position="SG",
        baseline_minutes=baseline,
        adjusted_minutes=adjusted,
        confidence=confidence,
        dk_salary=dk_salary,
        reason=reason,
    )


def _make_agent(cache=None) -> CoachLearningAgent:
    """Create a ``CoachLearningAgent`` with a mocked AI service."""
    ai = MagicMock()
    ai.is_available = True
    return CoachLearningAgent(ai_service=ai, cache_service=cache)


def _build_roster(
    count: int = 12,
    baseline_range: tuple = (35, 10),
    salary_range: tuple = (9000, 3000),
) -> dict[int, PlayerProjection]:
    """Build a roster of ``count`` players with descending baselines/salaries.

    Player 1 gets the highest baseline/salary, player ``count`` gets the lowest.
    """
    projections = {}
    base_high, base_low = baseline_range
    sal_high, sal_low = salary_range
    for i in range(1, count + 1):
        frac = (i - 1) / max(count - 1, 1)
        bl = round(base_high - frac * (base_high - base_low), 1)
        sal = int(sal_high - frac * (sal_high - sal_low))
        projections[i] = _make_projection(
            player_id=i, baseline=bl, adjusted=bl, dk_salary=sal,
        )
    return projections


# ===========================================================================
# TestGetExpectedRotationSize
# ===========================================================================

class TestGetExpectedRotationSize:
    """Tests for ``get_expected_rotation_size`` (cache-only after refactor)."""

    def test_returns_none_when_no_cache(self):
        """No cache service → returns None (cache-only method)."""
        agent = _make_agent()
        result = agent.get_expected_rotation_size(team_id=1, sport="nba")
        assert result is None

    def test_returns_none_when_cache_miss(self):
        """Cache miss → returns None (DB query must be prefetched)."""
        cache = MagicMock()
        cache.get_sync.return_value = None
        agent = _make_agent(cache=cache)

        result = agent.get_expected_rotation_size(team_id=1)
        assert result is None

    def test_cache_returns_int(self):
        """Cached value is a string, should be parsed as int."""
        cache = MagicMock()
        cache.get_sync.return_value = "9"
        agent = _make_agent(cache=cache)

        result = agent.get_expected_rotation_size(team_id=1)
        assert result == 9
        assert isinstance(result, int)

    def test_cache_returns_none_on_invalid_value(self):
        """Non-numeric cached value → returns None."""
        cache = MagicMock()
        cache.get_sync.return_value = "bad"
        agent = _make_agent(cache=cache)

        result = agent.get_expected_rotation_size(team_id=1)
        assert result is None


class TestPrefetchRotationDepth:
    """Tests for ``prefetch_rotation_depth`` (async, uses injected session)."""

    def test_prefetch_populates_cache(self):
        """Prefetch runs DB query and stores result in cache."""
        cache = MagicMock()
        cache.get_sync.return_value = None  # Cache miss
        agent = _make_agent(cache=cache)

        async def mock_query(team_id, sport, session):
            return 10

        agent._query_rotation_depth = mock_query
        mock_session = AsyncMock()

        result = asyncio.run(
            agent.prefetch_rotation_depth(1, mock_session, "nba")
        )
        assert result == 10
        cache.set_sync.assert_called_once()

    def test_prefetch_returns_cached_value(self):
        """Prefetch returns from cache without DB query."""
        cache = MagicMock()
        cache.get_sync.return_value = "9"
        agent = _make_agent(cache=cache)

        mock_session = AsyncMock()
        agent._query_rotation_depth = AsyncMock()

        result = asyncio.run(
            agent.prefetch_rotation_depth(1, mock_session, "nba")
        )
        assert result == 9
        agent._query_rotation_depth.assert_not_called()

    def test_prefetch_returns_none_on_db_failure(self):
        """DB returns None → prefetch returns None, no cache write."""
        cache = MagicMock()
        cache.get_sync.return_value = None
        agent = _make_agent(cache=cache)

        async def mock_query(team_id, sport, session):
            return None

        agent._query_rotation_depth = mock_query
        mock_session = AsyncMock()

        result = asyncio.run(
            agent.prefetch_rotation_depth(1, mock_session, "nba")
        )
        assert result is None
        cache.set_sync.assert_not_called()


# ===========================================================================
# TestCalculateActiveDepthAndFilter
# ===========================================================================

class TestCalculateActiveDepthAndFilter:
    """Tests for the core depth filter logic."""

    def test_zeroes_players_beyond_depth(self):
        """12-player roster with depth=9 zeroes bottom 3."""
        agent = _make_agent()
        projections = _build_roster(12)

        # Mock depth to return 9
        agent.get_expected_rotation_size = MagicMock(return_value=9)

        result = agent.calculate_active_depth_and_filter(
            team_id=1, projections=projections,
        )

        active = [p for p in result.values() if p.adjusted_minutes > 0]
        zeroed = [p for p in result.values() if p.coach_decision_dnp]

        assert len(active) == 9
        assert len(zeroed) == 3

    def test_no_filter_when_active_leq_depth(self):
        """8 active with depth=9 → all untouched."""
        agent = _make_agent()
        projections = _build_roster(8)
        agent.get_expected_rotation_size = MagicMock(return_value=9)

        result = agent.calculate_active_depth_and_filter(
            team_id=1, projections=projections,
        )

        active = [p for p in result.values() if p.adjusted_minutes > 0]
        assert len(active) == 8
        assert not any(p.coach_decision_dnp for p in result.values())

    def test_coach_decision_dnp_flag_set(self):
        """Zeroed players have ``coach_decision_dnp=True``."""
        agent = _make_agent()
        projections = _build_roster(11)
        agent.get_expected_rotation_size = MagicMock(return_value=9)

        result = agent.calculate_active_depth_and_filter(
            team_id=1, projections=projections,
        )

        dnp_players = [p for p in result.values() if p.coach_decision_dnp]
        assert len(dnp_players) == 2
        for p in dnp_players:
            assert p.adjusted_minutes == 0.0
            assert p.confidence == 0.0

    def test_confidence_set_to_zero(self):
        """Zeroed players have ``confidence=0.0``."""
        agent = _make_agent()
        projections = _build_roster(10)
        agent.get_expected_rotation_size = MagicMock(return_value=8)

        result = agent.calculate_active_depth_and_filter(
            team_id=1, projections=projections,
        )

        for p in result.values():
            if p.coach_decision_dnp:
                assert p.confidence == 0.0

    def test_reason_field_appended(self):
        """Reason string properly appended for zeroed players."""
        agent = _make_agent()
        projections = _build_roster(10)
        agent.get_expected_rotation_size = MagicMock(return_value=8)

        result = agent.calculate_active_depth_and_filter(
            team_id=1, projections=projections,
        )

        for p in result.values():
            if p.coach_decision_dnp:
                assert "Coach depth filter" in p.reason
                assert "8-man rotation" in p.reason

    def test_preserves_highest_ranked_players(self):
        """Top players by composite score are preserved."""
        agent = _make_agent()
        projections = _build_roster(12)
        agent.get_expected_rotation_size = MagicMock(return_value=9)

        result = agent.calculate_active_depth_and_filter(
            team_id=1, projections=projections,
        )

        # Player 1 (highest baseline + salary) should always be kept
        assert result[1].adjusted_minutes > 0
        assert result[1].coach_decision_dnp is False

        # Player 12 (lowest baseline + salary) should be zeroed
        assert result[12].adjusted_minutes == 0.0
        assert result[12].coach_decision_dnp is True


# ===========================================================================
# TestInjuryExpansionSafetyValve
# ===========================================================================

class TestInjuryExpansionSafetyValve:
    """Tests for the injury expansion safety valve."""

    def test_3_injured_expands_by_1(self):
        """depth=9, 3 injuries → effective depth=10, only 2 zeroed."""
        agent = _make_agent()

        # 12 active + 3 injured = 15 players
        projections = _build_roster(12)
        # Add 3 "injured" players (adjusted_minutes=0, confidence=0)
        for i in range(13, 16):
            projections[i] = _make_projection(
                player_id=i, baseline=15.0, adjusted=0.0,
                confidence=0.0, dk_salary=3500,
            )

        agent.get_expected_rotation_size = MagicMock(return_value=9)

        result = agent.calculate_active_depth_and_filter(
            team_id=1, projections=projections,
        )

        # With injury expansion: effective_depth=10
        # 12 active - 10 depth = 2 zeroed
        zeroed = [
            p for p in result.values()
            if p.coach_decision_dnp
        ]
        assert len(zeroed) == 2

    def test_2_injured_no_expansion(self):
        """depth=9, 2 injuries → stays 9, 3 zeroed."""
        agent = _make_agent()

        projections = _build_roster(12)
        # Add 2 "injured" players
        for i in range(13, 15):
            projections[i] = _make_projection(
                player_id=i, baseline=15.0, adjusted=0.0,
                confidence=0.0, dk_salary=3500,
            )

        agent.get_expected_rotation_size = MagicMock(return_value=9)

        result = agent.calculate_active_depth_and_filter(
            team_id=1, projections=projections,
        )

        # No expansion: 12 active - 9 depth = 3 zeroed
        zeroed = [
            p for p in result.values()
            if p.coach_decision_dnp
        ]
        assert len(zeroed) == 3


# ===========================================================================
# TestRankingFormula
# ===========================================================================

class TestRankingFormula:
    """Tests for the composite ranking formula."""

    def test_high_baseline_high_salary_ranked_first(self):
        """35min + $8500 ranks higher than 10min + $3500 (zeroed at depth=8)."""
        agent = _make_agent()

        # Build 9-player roster: 8 solid + 1 fringe, depth=8
        projections = {
            1: _make_projection(1, baseline=35.0, adjusted=35.0, dk_salary=8500),
            2: _make_projection(2, baseline=32.0, adjusted=32.0, dk_salary=7500),
            3: _make_projection(3, baseline=28.0, adjusted=28.0, dk_salary=7000),
            4: _make_projection(4, baseline=25.0, adjusted=25.0, dk_salary=6000),
            5: _make_projection(5, baseline=22.0, adjusted=22.0, dk_salary=5500),
            6: _make_projection(6, baseline=18.0, adjusted=18.0, dk_salary=5000),
            7: _make_projection(7, baseline=15.0, adjusted=15.0, dk_salary=4500),
            8: _make_projection(8, baseline=12.0, adjusted=12.0, dk_salary=4000),
            9: _make_projection(9, baseline=10.0, adjusted=10.0, dk_salary=3500),
        }
        agent.get_expected_rotation_size = MagicMock(return_value=8)

        result = agent.calculate_active_depth_and_filter(
            team_id=1, projections=projections,
        )

        # Player 1 (highest composite) should survive
        assert result[1].adjusted_minutes > 0
        # Player 9 (lowest composite) should be zeroed
        assert result[9].coach_decision_dnp is True

    def test_salary_none_uses_baseline_only(self):
        """None salary → salary_component = 0 in composite."""
        agent = _make_agent()

        # 9 players, depth=8 → 1 zeroed.  Player with None salary
        # but high baseline should still survive.
        projections = {
            1: _make_projection(1, baseline=35.0, adjusted=35.0, dk_salary=None),
            2: _make_projection(2, baseline=30.0, adjusted=30.0, dk_salary=9000),
            3: _make_projection(3, baseline=28.0, adjusted=28.0, dk_salary=7000),
            4: _make_projection(4, baseline=25.0, adjusted=25.0, dk_salary=6000),
            5: _make_projection(5, baseline=22.0, adjusted=22.0, dk_salary=5500),
            6: _make_projection(6, baseline=18.0, adjusted=18.0, dk_salary=5000),
            7: _make_projection(7, baseline=15.0, adjusted=15.0, dk_salary=4500),
            8: _make_projection(8, baseline=12.0, adjusted=12.0, dk_salary=4000),
            9: _make_projection(9, baseline=5.0, adjusted=5.0, dk_salary=None),
        }
        agent.get_expected_rotation_size = MagicMock(return_value=8)

        result = agent.calculate_active_depth_and_filter(
            team_id=1, projections=projections,
        )

        # Player 1: composite = 35*0.6 + 0 = 21.0  → survives (high baseline)
        # Player 9: composite = 5*0.6 + 0 = 3.0   → zeroed (lowest)
        assert result[9].coach_decision_dnp is True
        assert result[1].adjusted_minutes > 0
        assert result[2].adjusted_minutes > 0

    def test_salary_tiebreaker_works(self):
        """When baselines are equal, higher salary player survives."""
        agent = _make_agent()

        # 9 players with 2 tied on baseline but different salaries
        projections = {
            1: _make_projection(1, baseline=32.0, adjusted=32.0, dk_salary=8000),
            2: _make_projection(2, baseline=28.0, adjusted=28.0, dk_salary=7000),
            3: _make_projection(3, baseline=25.0, adjusted=25.0, dk_salary=6000),
            4: _make_projection(4, baseline=22.0, adjusted=22.0, dk_salary=5500),
            5: _make_projection(5, baseline=20.0, adjusted=20.0, dk_salary=5000),
            6: _make_projection(6, baseline=18.0, adjusted=18.0, dk_salary=4500),
            7: _make_projection(7, baseline=15.0, adjusted=15.0, dk_salary=4000),
            # Players 8 and 9 tied on baseline, different salary
            8: _make_projection(8, baseline=10.0, adjusted=10.0, dk_salary=8000),
            9: _make_projection(9, baseline=10.0, adjusted=10.0, dk_salary=3000),
        }
        agent.get_expected_rotation_size = MagicMock(return_value=8)

        result = agent.calculate_active_depth_and_filter(
            team_id=1, projections=projections,
        )

        # Player 8: 10*0.6 + 8.0*0.4 = 6 + 3.2 = 9.2  → survives
        # Player 9: 10*0.6 + 3.0*0.4 = 6 + 1.2 = 7.2  → zeroed
        assert result[8].adjusted_minutes > 0
        assert result[9].coach_decision_dnp is True


# ===========================================================================
# TestDepthFilterGuards
# ===========================================================================

class TestDepthFilterGuards:
    """Tests for edge cases and safety guards."""

    def test_depth_below_7_skipped(self):
        """expected_depth=6 → returns projections unchanged."""
        agent = _make_agent()
        projections = _build_roster(10)
        agent.get_expected_rotation_size = MagicMock(return_value=6)

        result = agent.calculate_active_depth_and_filter(
            team_id=1, projections=projections,
        )

        assert not any(p.coach_decision_dnp for p in result.values())
        assert all(p.adjusted_minutes > 0 for p in result.values())

    def test_none_depth_returns_unchanged(self):
        """No DB data (None) → no filtering."""
        agent = _make_agent()
        projections = _build_roster(12)
        agent.get_expected_rotation_size = MagicMock(return_value=None)

        result = agent.calculate_active_depth_and_filter(
            team_id=1, projections=projections,
        )

        assert not any(p.coach_decision_dnp for p in result.values())

    def test_empty_projections_no_crash(self):
        """Empty dict returns safely."""
        agent = _make_agent()
        agent.get_expected_rotation_size = MagicMock(return_value=9)

        result = agent.calculate_active_depth_and_filter(
            team_id=1, projections={},
        )
        assert result == {}

    def test_skipped_for_cbb(self):
        """sport='cbb' skips filter entirely."""
        agent = _make_agent()
        projections = _build_roster(12)
        agent.get_expected_rotation_size = MagicMock(return_value=9)

        result = agent.calculate_active_depth_and_filter(
            team_id=1, projections=projections, sport="cbb",
        )

        # get_expected_rotation_size should NOT even be called for CBB
        agent.get_expected_rotation_size.assert_not_called()
        assert not any(p.coach_decision_dnp for p in result.values())

    def test_exact_depth_no_filtering(self):
        """Active count == expected_depth → no filtering needed."""
        agent = _make_agent()
        projections = _build_roster(9)
        agent.get_expected_rotation_size = MagicMock(return_value=9)

        result = agent.calculate_active_depth_and_filter(
            team_id=1, projections=projections,
        )

        assert not any(p.coach_decision_dnp for p in result.values())


# ===========================================================================
# TestGetAdjustedProfile
# ===========================================================================

class TestGetAdjustedProfile:
    """Tests for ``get_adjusted_profile`` cache retrieval."""

    def test_returns_cached_update(self):
        """Returns CoachPatternUpdate from cache."""
        cache = MagicMock()
        update = CoachPatternUpdate(
            coach_name="Joe Mazzulla",
            team_id=1610612738,
            starter_multiplier_adj=0.02,
            bench_multiplier_adj=-0.01,
            star_multiplier_adj=0.0,
            rotation_size_adj=0,
            b2b_penalty_adj=0.0,
            pace_factor_adj=0.0,
            reasoning="Test pattern",
            based_on_games=5,
            confidence=0.8,
        )
        cache.get_sync.return_value = update.model_dump_json()
        agent = _make_agent(cache=cache)

        result = agent.get_adjusted_profile(team_id=1610612738)
        assert result is not None
        assert result.coach_name == "Joe Mazzulla"
        assert result.starter_multiplier_adj == 0.02

    def test_returns_none_no_cache(self):
        """Returns None when no cached data."""
        agent = _make_agent()  # No cache service
        result = agent.get_adjusted_profile(team_id=1)
        assert result is None

    def test_returns_none_empty_cache(self):
        """Returns None when cache is empty for this key."""
        cache = MagicMock()
        cache.get_sync.return_value = None
        agent = _make_agent(cache=cache)

        result = agent.get_adjusted_profile(team_id=1)
        assert result is None


# ===========================================================================
# TestQueryRotationDepth
# ===========================================================================

class TestQueryRotationDepth:
    """Tests for ``_query_rotation_depth`` with injected session."""

    def test_returns_none_on_exception(self):
        """DB exception → returns None gracefully."""
        agent = _make_agent()
        mock_session = AsyncMock()
        mock_session.execute.side_effect = Exception("DB error")

        result = asyncio.run(
            agent._query_rotation_depth(1, "nba", mock_session)
        )
        assert result is None
