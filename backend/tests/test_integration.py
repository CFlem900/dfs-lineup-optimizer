"""Integration tests — verify multi-service pipelines work end-to-end.

These tests exercise real service interactions without external
dependencies (no DB, no API keys).  They cover the critical data
paths that connect rotation → DFS → simulation → lineup optimization.
"""

import math
import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from app.models.player import PlayerMinutes, PlayerProjection
from app.models.rotation import TeamRotation
from app.models.simulation import SimulationConfig
from app.models.game import GameInfo, TeamGameStats
from app.models.coach import CoachProfile, CoachingStyle
from app.services.rotation_engine import RotationEngine
from app.services.dfs_service import DFSService
from app.services.simulation_engine import SimulationEngine
from app.services.calibration_service import CalibrationService, clamp_adjustment
from app.services.fade_service import FadeService
from app.services.expert_signal_service import ExpertSignalService


# ============================================================================
# Pipeline: Rotation → DFS Scoring
# ============================================================================


class TestRotationToDFSPipeline:
    """Verify rotation projections feed correctly into DFS scoring."""

    def test_rotation_produces_nonzero_dk_points(
        self, engine, full_rotation_with_stats, no_injuries, game_info_fixture
    ):
        """Full pipeline: rotation → DFS scoring → non-zero fantasy points."""
        rotation = engine.project_team_rotation(
            team_id=1,
            team_name="Test Team",
            rotation=full_rotation_with_stats,
            injuries=no_injuries,
            game_date="2026-02-08",
            game_info=game_info_fixture,
        )

        assert rotation.total_minutes == pytest.approx(240.0, abs=1.0)

        dfs = DFSService()
        scored_count = 0
        for proj in rotation.projections:
            if proj.adjusted_minutes > 5:
                pm = next(
                    (p for p in full_rotation_with_stats if p.player_id == proj.player_id),
                    None,
                )
                if pm:
                    scored = dfs.project_player_dfs(proj, pm)
                    if scored and scored.dk_points and scored.dk_points > 0:
                        scored_count += 1

        # At least the 8-man rotation should have non-zero DK points
        assert scored_count >= 8

    def test_team_minutes_approximately_240(
        self, engine, full_rotation, no_injuries, game_info_fixture
    ):
        """Team total minutes must be ~240 after rotation projection."""
        rotation = engine.project_team_rotation(
            team_id=1,
            team_name="Test Team",
            rotation=full_rotation,
            injuries=no_injuries,
            game_date="2026-02-08",
            game_info=game_info_fixture,
        )
        assert 238.0 <= rotation.total_minutes <= 242.0

    def test_injury_reduces_player_minutes(
        self, engine, full_rotation, single_injury, game_info_fixture
    ):
        """An Out player should get 0 minutes, and team still ~240."""
        rotation = engine.project_team_rotation(
            team_id=1,
            team_name="Test Team",
            rotation=full_rotation,
            injuries=single_injury,
            game_date="2026-02-08",
            game_info=game_info_fixture,
        )
        # Star Guard (pid=100) is Out
        star = next(p for p in rotation.projections if p.player_id == 100)
        assert star.adjusted_minutes == 0.0
        # Team still near 240
        assert 235.0 <= rotation.total_minutes <= 242.0


# ============================================================================
# Pipeline: Rotation → Simulation
# ============================================================================


class TestRotationToSimulationPipeline:
    """Verify rotation projections feed into Monte Carlo simulation."""

    def test_simulation_probabilities_sum_to_one(
        self, sim_team_rotation, full_rotation_with_stats, game_info_fixture, sim_config
    ):
        """Home + away win probabilities should sum to ~1.0."""
        sim_engine = SimulationEngine(config=sim_config)
        result = sim_engine.simulate_game(
            game_info=game_info_fixture,
            home_rotation=sim_team_rotation,
            home_players=full_rotation_with_stats,
            away_rotation=sim_team_rotation,
            away_players=full_rotation_with_stats,
        )
        total_prob = result.home_win_pct + result.away_win_pct
        assert 0.95 <= total_prob <= 1.05

    def test_simulation_produces_score_distributions(
        self, sim_team_rotation, full_rotation_with_stats, game_info_fixture, sim_config
    ):
        """Simulation should produce non-empty score distributions."""
        sim_engine = SimulationEngine(config=sim_config)
        result = sim_engine.simulate_game(
            game_info=game_info_fixture,
            home_rotation=sim_team_rotation,
            home_players=full_rotation_with_stats,
            away_rotation=sim_team_rotation,
            away_players=full_rotation_with_stats,
        )
        # Should have average scores in reasonable NBA range
        assert 80 < result.home_team.mean_score < 150
        assert 80 < result.away_team.mean_score < 150


# ============================================================================
# Calibration Service
# ============================================================================


class TestCalibrationRoundTrip:
    """Test calibration clamping and service methods."""

    def test_clamp_within_range(self):
        """Values within +-15% should pass through."""
        assert clamp_adjustment(1.0) == 1.0
        assert clamp_adjustment(1.10) == 1.10
        assert clamp_adjustment(0.90) == 0.90

    def test_clamp_caps_high(self):
        """Values above 1.15 should be clamped to 1.15."""
        assert clamp_adjustment(1.50) == 1.15
        assert clamp_adjustment(2.00) == 1.15

    def test_clamp_caps_low(self):
        """Values below 0.85 should be clamped to 0.85."""
        assert clamp_adjustment(0.50) == 0.85
        assert clamp_adjustment(0.00) == 0.85

    def test_service_defaults_to_neutral(self):
        """CalibrationService returns 1.0 for unknown keys."""
        svc = CalibrationService()
        assert svc.get_position_bias("PG") == 1.0
        assert svc.get_salary_tier_adjustment("high") == 1.0
        assert svc.get_stacking_weight("2man_weight") == 1.0
        assert svc.get_dvp_sensitivity() == 1.0
        assert svc.get_stat_rate_adjustment("pts") == 1.0

    def test_cached_calibrations_returned(self):
        """After setting cache, values are returned."""
        svc = CalibrationService()
        svc._cache = {
            "position_PG_bias": 1.05,
            "salary_tier_high": 0.95,
            "dvp_sensitivity": 0.90,
        }
        assert svc.get_position_bias("PG") == 1.05
        assert svc.get_salary_tier_adjustment("high") == 0.95
        assert svc.get_dvp_sensitivity() == 0.90


# ============================================================================
# Fade List Service
# ============================================================================


class TestFadeListScoring:
    """Test the fade/leverage categorization logic."""

    def _make_pool_entry(self, name, salary, projected_fp, ceiling_fp, ownership):
        """Create a mock pool entry."""
        entry = MagicMock()
        entry.player_id = hash(name) % 10000
        entry.player_name = name
        entry.position = "G"
        entry.team_abbreviation = "TST"
        entry.salary = salary
        entry.projected_fp = projected_fp
        entry.ceiling_fp = ceiling_fp
        entry.floor_fp = projected_fp * 0.5
        entry.ownership_projection = ownership
        entry.dk_value = projected_fp / salary * 1000 if salary > 0 else 0
        return entry

    def test_high_ownership_low_ceiling_is_fade(self):
        """Player with 30% ownership but low ceiling → should be a fade."""
        pool = [
            self._make_pool_entry("Chalk Player", 8000, 35, 38, 30.0),  # high own, low ceil
            self._make_pool_entry("Good Player", 7000, 33, 50, 15.0),   # moderate
            self._make_pool_entry("Great Player", 6000, 30, 55, 10.0),  # moderate
            self._make_pool_entry("Hidden Gem", 4000, 22, 52, 3.0),     # low own, high ceil
            self._make_pool_entry("Average Joe", 5000, 25, 42, 12.0),   # moderate
        ]

        svc = FadeService()
        result = svc.generate_fade_list(pool)

        assert result["pool_size"] == 5
        # Chalk Player should appear in fades (high ownership, lowest ceiling)
        fade_names = [f["player_name"] for f in result["fades"]]
        assert "Chalk Player" in fade_names

    def test_low_ownership_high_ceiling_is_leverage(self):
        """Player with 3% ownership but high ceiling → should be leverage."""
        pool = [
            self._make_pool_entry("Chalk Player", 8000, 35, 38, 30.0),
            self._make_pool_entry("Good Player", 7000, 33, 50, 15.0),
            self._make_pool_entry("Great Player", 6000, 30, 55, 10.0),
            self._make_pool_entry("Hidden Gem", 4000, 22, 52, 3.0),     # leverage candidate
            self._make_pool_entry("Average Joe", 5000, 25, 42, 12.0),
        ]

        svc = FadeService()
        result = svc.generate_fade_list(pool)

        leverage_names = [l["player_name"] for l in result["leverage_plays"]]
        assert "Hidden Gem" in leverage_names

    def test_empty_pool_returns_empty(self):
        """Empty pool should return empty results."""
        svc = FadeService()
        result = svc.generate_fade_list([])
        assert result["fades"] == []
        assert result["leverage_plays"] == []

    def test_no_ownership_skips_players(self):
        """Players without ownership data should be skipped."""
        pool = [self._make_pool_entry("No Owner", 5000, 25, 40, None)]
        svc = FadeService()
        result = svc.generate_fade_list(pool)
        assert result["fades"] == []
        assert result["leverage_plays"] == []


# ============================================================================
# Expert Signal Scoring
# ============================================================================


class TestExpertSignalScoring:
    """Test the static signal scoring method."""

    def _make_signal(self, tier, content="test content", minutes_ago=30, engagement=None):
        """Create a mock ExpertSignal."""
        from app.config.expert_sources import ExpertTier
        signal = MagicMock()
        signal.expert_tier = tier
        signal.content = content
        signal.timestamp = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
        signal.engagement = engagement or {"likes": 10, "retweets": 5}
        return signal

    def test_tier1_scores_higher_than_tier3(self):
        """Tier 1 experts should have higher base scores."""
        from app.config.expert_sources import ExpertTier

        t1 = self._make_signal(ExpertTier.TIER_1)
        t3 = self._make_signal(ExpertTier.TIER_3)

        score_t1 = ExpertSignalService._calculate_signal_score(t1)
        score_t3 = ExpertSignalService._calculate_signal_score(t3)

        assert score_t1 > score_t3

    def test_recency_boost(self):
        """Recent signals should score higher than old ones."""
        from app.config.expert_sources import ExpertTier

        recent = self._make_signal(ExpertTier.TIER_2, minutes_ago=30)
        old = self._make_signal(ExpertTier.TIER_2, minutes_ago=60 * 12)

        score_recent = ExpertSignalService._calculate_signal_score(recent)
        score_old = ExpertSignalService._calculate_signal_score(old)

        assert score_recent > score_old

    def test_keyword_bonus(self):
        """Rotation keywords in content should boost the score."""
        from app.config.expert_sources import ExpertTier

        with_kw = self._make_signal(ExpertTier.TIER_2, content="Expected to start tonight, rotation shift")
        without_kw = self._make_signal(ExpertTier.TIER_2, content="General NBA commentary today")

        score_with = ExpertSignalService._calculate_signal_score(with_kw)
        score_without = ExpertSignalService._calculate_signal_score(without_kw)

        assert score_with > score_without

    def test_player_mention_bonus(self):
        """Direct player mention should boost the score."""
        from app.config.expert_sources import ExpertTier

        signal = self._make_signal(ExpertTier.TIER_2, content="LeBron James expected to play tonight")

        score_with_player = ExpertSignalService._calculate_signal_score(
            signal, player_names=["LeBron James"]
        )
        score_without_player = ExpertSignalService._calculate_signal_score(signal)

        assert score_with_player > score_without_player

    def test_score_capped_at_2(self):
        """Score should never exceed 2.0."""
        from app.config.expert_sources import ExpertTier

        signal = self._make_signal(
            ExpertTier.TIER_1,
            content="LeBron James starter rotation minutes tonight",
            minutes_ago=5,
            engagement={"likes": 10000, "retweets": 5000},
        )

        score = ExpertSignalService._calculate_signal_score(
            signal, player_names=["LeBron James"]
        )
        assert score <= 2.0


# ============================================================================
# Agent Graceful Fallback
# ============================================================================


class TestAgentGracefulFallback:
    """Verify agents return None/empty when AI is unavailable."""

    def test_signal_analysis_returns_none_when_unavailable(self):
        """SignalAnalysisAgent should return None when AI is off."""
        from app.services.agents.signal_analysis_agent import SignalAnalysisAgent

        mock_ai = MagicMock()
        mock_ai.is_available = False
        agent = SignalAnalysisAgent(mock_ai)

        result = agent.analyze_signal("test content", {"team_abbr": "LAL"})
        assert result is None

    def test_injury_impact_returns_none_when_unavailable(self):
        """InjuryImpactAgent should return None when AI is off."""
        from app.services.agents.injury_impact_agent import InjuryImpactAgent

        mock_ai = MagicMock()
        mock_ai.is_available = False
        agent = InjuryImpactAgent(mock_ai)

        result = agent.analyze_injury_impact(
            injured_player={"player_name": "Test", "status": "Out"},
            team_roster=[],
        )
        assert result is None

    def test_expert_quality_returns_neutral_weight(self):
        """ExpertQualityAgent should return 1.0 (neutral) with no data."""
        from app.services.agents.expert_quality_agent import ExpertQualityAgent

        mock_ai = MagicMock()
        mock_ai.is_available = False
        agent = ExpertQualityAgent(mock_ai)

        weight = agent.get_quality_adjusted_weight("@unknown_expert")
        assert weight == 1.0

    def test_expert_quality_specialty_returns_none(self):
        """get_specialty_weight should return None with no cached data."""
        from app.services.agents.expert_quality_agent import ExpertQualityAgent

        mock_ai = MagicMock()
        mock_ai.is_available = False
        agent = ExpertQualityAgent(mock_ai)

        result = agent.get_specialty_weight("@unknown", "injury_update")
        assert result is None

    def test_backtesting_returns_none_when_unavailable(self):
        """BacktestingAgent should return None when AI is off."""
        from app.services.agents.backtesting_agent import BacktestingAgent

        mock_ai = MagicMock()
        mock_ai.is_available = False
        agent = BacktestingAgent(mock_ai)

        result = agent.analyze_accuracy([], {})
        assert result is None


# ============================================================================
# Coach Profile Merging
# ============================================================================


class TestCoachProfileMerging:
    """Test that coach profile service correctly merges static + learned data."""

    def test_static_profile_returned_without_db(self):
        """With no DB data, static profiles should be returned unchanged."""
        from app.services.coach_profile_service import CoachProfileService

        svc = CoachProfileService()
        # Without loading anything, _learned is empty
        profile = svc.get_merged_profile(1610612738)  # BOS
        # Should return the static profile for that team (a CoachProfile object)
        assert profile is not None
        assert hasattr(profile, "coach_name") or hasattr(profile, "starter_multiplier")


# ============================================================================
# Late-Swap Monitor
# ============================================================================


class TestLateSwapMonitor:
    """Test late-swap monitor service logic."""

    def test_check_for_updates_returns_structure(self):
        """check_for_updates should return expected keys."""
        from app.services.late_swap_monitor import LateSwapMonitor

        mock_injury = MagicMock()
        mock_injury.get_all_injuries.return_value = {}
        mock_game = MagicMock()
        mock_news = MagicMock()
        mock_news.get_news.return_value = MagicMock(articles=[])

        monitor = LateSwapMonitor(mock_injury, mock_game, mock_news)
        result = monitor.check_for_updates()

        assert "injury_updates" in result
        assert "news_updates" in result
        assert "checked_at" in result
        assert "has_critical_updates" in result
        assert isinstance(result["injury_updates"], list)

    def test_at_risk_detects_out_players(self):
        """Players with 'Out' status should be flagged as high risk."""
        from app.services.late_swap_monitor import LateSwapMonitor

        mock_injury = MagicMock()
        mock_injury.get_all_injuries.return_value = {
            "LAL": [{"player_name": "LeBron James", "status": "Out", "comment": "Rest"}]
        }
        mock_game = MagicMock()
        mock_news = MagicMock()

        monitor = LateSwapMonitor(mock_injury, mock_game, mock_news)

        lineup = MagicMock()
        lineup.players = [
            {"player_name": "LeBron James", "team": "LAL", "position": "SF", "slot": "SF", "salary": 10000, "projected_fp": 50},
        ]

        at_risk = monitor.get_at_risk_players([lineup])
        assert len(at_risk) == 1
        assert at_risk[0]["risk_level"] == "high"
        assert at_risk[0]["player_name"] == "LeBron James"
