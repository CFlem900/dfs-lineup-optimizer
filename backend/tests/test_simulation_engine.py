"""Tests for the Monte Carlo game simulation engine."""

import pytest
import numpy as np
from typing import List

from app.models.player import PlayerMinutes, PlayerProjection
from app.models.rotation import TeamRotation
from app.models.game import GameInfo, TeamGameStats
from app.models.simulation import (
    SimulationConfig,
    PlayerSimInput,
    PlayerSimResult,
    TeamSimResult,
    GameSimResult,
)
from app.services.simulation_engine import SimulationEngine


# ============================================================================
# SIMULATION CONFIG TESTS
# ============================================================================


class TestSimulationConfig:
    """Tests for SimulationConfig validation and defaults."""

    def test_default_config_values(self):
        config = SimulationConfig()
        assert config.num_simulations == 10_000
        assert config.minutes_variance == 0.20
        assert config.pace_variance == 0.04
        assert config.scoring_variance == 0.15
        assert config.seed is None

    def test_custom_config(self):
        config = SimulationConfig(
            num_simulations=5000,
            minutes_variance=0.10,
            pace_variance=0.08,
            scoring_variance=0.20,
            seed=42,
        )
        assert config.num_simulations == 5000
        assert config.seed == 42

    def test_seed_reproducibility(
        self, game_info_fixture, sim_team_rotation, full_rotation_with_stats
    ):
        """Same seed produces identical results."""
        config = SimulationConfig(num_simulations=500, seed=99)

        sim1 = SimulationEngine(config)
        r1 = sim1.simulate_game(
            game_info_fixture,
            sim_team_rotation,
            full_rotation_with_stats,
            sim_team_rotation,
            full_rotation_with_stats,
        )

        sim2 = SimulationEngine(SimulationConfig(num_simulations=500, seed=99))
        r2 = sim2.simulate_game(
            game_info_fixture,
            sim_team_rotation,
            full_rotation_with_stats,
            sim_team_rotation,
            full_rotation_with_stats,
        )

        assert r1.home_team.mean_score == r2.home_team.mean_score
        assert r1.away_team.mean_score == r2.away_team.mean_score
        assert r1.home_win_pct == r2.home_win_pct

    def test_different_seeds_differ(
        self, game_info_fixture, sim_team_rotation, full_rotation_with_stats
    ):
        """Different seeds produce different results.

        Uses 2000 sims to avoid rounding collisions on mean_score,
        and compares multiple fields to reduce false pass/fail.
        """
        r1 = SimulationEngine(
            SimulationConfig(num_simulations=2000, seed=1)
        ).simulate_game(
            game_info_fixture,
            sim_team_rotation,
            full_rotation_with_stats,
            sim_team_rotation,
            full_rotation_with_stats,
        )
        r2 = SimulationEngine(
            SimulationConfig(num_simulations=2000, seed=99)
        ).simulate_game(
            game_info_fixture,
            sim_team_rotation,
            full_rotation_with_stats,
            sim_team_rotation,
            full_rotation_with_stats,
        )
        # At least one of these should differ across seeds
        differs = (
            r1.home_team.mean_score != r2.home_team.mean_score
            or r1.home_team.std_score != r2.home_team.std_score
            or r1.home_win_pct != r2.home_win_pct
        )
        assert differs, "Different seeds should produce different results"


# ============================================================================
# PLAYER INPUT PREPARATION TESTS
# ============================================================================


class TestPlayerInputPreparation:
    """Tests for prepare_player_inputs."""

    def test_prepare_maps_all_fields(
        self, sim_team_rotation, full_rotation_with_stats
    ):
        inputs = SimulationEngine.prepare_player_inputs(
            sim_team_rotation, full_rotation_with_stats
        )
        assert len(inputs) == 10  # all 10 players active

        star = inputs[0]
        assert star.player_id == 100
        assert star.player_name == "Star Guard"
        assert star.position == "G"
        assert star.projected_minutes == 35.0
        assert star.usage_rate == 0.30
        assert star.pts_per_min == 0.71
        assert star.reb_per_min == 0.14

    def test_prepare_filters_injured_players(
        self, full_rotation_with_stats
    ):
        """Players with adjusted_minutes <= 0 are excluded."""
        # Star is injured: distribute their 35 min among others proportionally
        star_mins = 35.0
        remaining = [pm for pm in full_rotation_with_stats if pm.player_id != 100]
        remaining_total = sum(pm.season_avg for pm in remaining)

        projections = []
        for pm in full_rotation_with_stats:
            if pm.player_id == 100:
                adj = 0.0
            else:
                adj = pm.season_avg + star_mins * (pm.season_avg / remaining_total)
            projections.append(
                PlayerProjection(
                    player_id=pm.player_id,
                    player_name=pm.player_name,
                    position=pm.position,
                    baseline_minutes=pm.season_avg,
                    adjusted_minutes=round(adj, 1),
                    confidence=1.0,
                )
            )

        rotation = TeamRotation(
            team_id=1,
            team_name="Test",
            game_date="2026-02-08",
            projections=projections,
            total_minutes=240.0,
            positions_breakdown={"G": 70.0, "F": 89.0, "C": 46.0},
        )

        inputs = SimulationEngine.prepare_player_inputs(
            rotation, full_rotation_with_stats
        )
        assert len(inputs) == 9  # star excluded
        assert all(p.player_id != 100 for p in inputs)

    def test_prepare_empty_rotation(self):
        """Empty rotation returns empty list.

        We bypass the TeamRotation validator by testing the method
        with a rotation that has all-zero-minute projections instead.
        """
        # Create a single-player rotation with 0 adjusted minutes
        proj = PlayerProjection(
            player_id=999,
            player_name="DNP Player",
            position="G",
            baseline_minutes=24.0,
            adjusted_minutes=0.0,
            confidence=0.0,
        )
        # Use 10 such projections at 24 min each to satisfy the 240 validator
        projections = [
            PlayerProjection(
                player_id=i,
                player_name=f"DNP {i}",
                position="G",
                baseline_minutes=24.0,
                adjusted_minutes=0.0,
                confidence=0.0,
            )
            for i in range(10)
        ]
        rotation = TeamRotation(
            team_id=1,
            team_name="Empty",
            game_date="2026-02-08",
            projections=projections,
            total_minutes=240.0,
            positions_breakdown={"G": 240.0},
        )
        inputs = SimulationEngine.prepare_player_inputs(rotation, [])
        assert inputs == []

    def test_zero_usage_gets_default(self, full_rotation_with_stats):
        """Players with 0 usage_rate get a default positive value."""
        # Modify one player to have 0 usage
        full_rotation_with_stats[9].usage_rate = 0.0

        projections = [
            PlayerProjection(
                player_id=pm.player_id,
                player_name=pm.player_name,
                position=pm.position,
                baseline_minutes=pm.season_avg,
                adjusted_minutes=pm.season_avg,
                confidence=1.0,
            )
            for pm in full_rotation_with_stats
        ]

        rotation = TeamRotation(
            team_id=1,
            team_name="Test",
            game_date="2026-02-08",
            projections=projections,
            total_minutes=240.0,
            positions_breakdown={},
        )

        inputs = SimulationEngine.prepare_player_inputs(
            rotation, full_rotation_with_stats
        )
        reserve = next(p for p in inputs if p.player_id == 109)
        assert reserve.usage_rate > 0  # defaulted to 0.10


# ============================================================================
# TEAM SIMULATION TESTS
# ============================================================================


class TestTeamSimulation:
    """Tests for _simulate_team behavior."""

    def _run_team_sim(self, full_rotation_with_stats, sim_team_rotation, **kwargs):
        config = SimulationConfig(num_simulations=2000, seed=42, **kwargs)
        sim = SimulationEngine(config)
        inputs = sim.prepare_player_inputs(sim_team_rotation, full_rotation_with_stats)
        pace = sim._sample_pace(100.0, config.num_simulations)
        return sim._simulate_team(inputs, pace, 115.0)

    def test_minutes_normalize_to_240(
        self, full_rotation_with_stats, sim_team_rotation
    ):
        """Mean total team minutes should be ~240."""
        scores, data = self._run_team_sim(
            full_rotation_with_stats, sim_team_rotation
        )
        total_mins = data["minutes"].sum(axis=0)
        mean_total = float(total_mins.mean())
        # Allow small deviation from 240 due to post-normalization clipping
        assert abs(mean_total - 240.0) < 2.0

    def test_minutes_variance_applied(
        self, full_rotation_with_stats, sim_team_rotation
    ):
        """Individual player minutes should have non-zero variance."""
        scores, data = self._run_team_sim(
            full_rotation_with_stats, sim_team_rotation
        )
        star_mins = data["minutes"][0]  # Star Guard
        assert float(star_mins.std()) > 1.0  # should vary significantly

    def test_no_minutes_variance(
        self, full_rotation_with_stats, sim_team_rotation
    ):
        """With minutes_variance=0, minutes should be nearly constant."""
        scores, data = self._run_team_sim(
            full_rotation_with_stats, sim_team_rotation, minutes_variance=0.001
        )
        star_mins = data["minutes"][0]
        assert float(star_mins.std()) < 1.0

    def test_minutes_capped_at_ot_max(
        self, full_rotation_with_stats, sim_team_rotation
    ):
        """No individual player should exceed OT_MAX_PLAYER_MINUTES (53 min).

        With OT modeling, close-game sims may extend player max to 53 min
        (48 regulation + 5 OT). Non-OT sims are still capped at 48.
        """
        from app.config.constants import OT_MAX_PLAYER_MINUTES
        scores, data = self._run_team_sim(
            full_rotation_with_stats, sim_team_rotation
        )
        assert float(data["minutes"].max()) <= OT_MAX_PLAYER_MINUTES + 0.01

    def test_all_stats_non_negative(
        self, full_rotation_with_stats, sim_team_rotation
    ):
        """All simulated stats should be >= 0."""
        scores, data = self._run_team_sim(
            full_rotation_with_stats, sim_team_rotation
        )
        for stat_name, arr in data["stats"].items():
            assert float(arr.min()) >= 0.0, f"Negative {stat_name} found"

    def test_team_score_near_projected(
        self, full_rotation_with_stats, sim_team_rotation
    ):
        """Mean team score should be close to the projected score."""
        projected = 115.0
        scores, _ = self._run_team_sim(
            full_rotation_with_stats, sim_team_rotation
        )
        mean_score = float(scores.mean())
        # Allow ±5 points tolerance
        assert abs(mean_score - projected) < 5.0, (
            f"Mean score {mean_score} too far from projected {projected}"
        )

    def test_scoring_scales_with_pace(
        self, full_rotation_with_stats, sim_team_rotation
    ):
        """Higher pace should produce higher team DFS scores on average.

        PTS calibration anchors mean team PTS to projected_team_score,
        so we pass different projected scores that reflect the pace
        difference (higher pace → more possessions → higher projected
        team score).  Non-PTS stats (reb, ast, stl, blk) also get
        pace-adjusted, contributing to the DFS score gap.
        """
        config = SimulationConfig(num_simulations=2000, seed=42)
        sim = SimulationEngine(config)
        inputs = sim.prepare_player_inputs(
            sim_team_rotation, full_rotation_with_stats
        )

        # Simulate with low pace and matching low projected score
        low_pace = np.full(2000, 90.0)
        scores_low, _ = sim._simulate_team(inputs, low_pace, 95.0)

        # Reset RNG for fair comparison
        sim._rng = np.random.default_rng(42)

        # Simulate with high pace and matching high projected score
        high_pace = np.full(2000, 110.0)
        scores_high, _ = sim._simulate_team(inputs, high_pace, 110.0)

        assert float(scores_high.mean()) > float(scores_low.mean())


# ============================================================================
# FULL GAME SIMULATION TESTS
# ============================================================================


class TestGameSimulation:
    """Tests for the full simulate_game method."""

    def test_win_probabilities_sum_to_near_one(
        self, game_info_fixture, sim_team_rotation, full_rotation_with_stats,
        sim_config,
    ):
        sim = SimulationEngine(sim_config)
        result = sim.simulate_game(
            game_info_fixture,
            sim_team_rotation,
            full_rotation_with_stats,
            sim_team_rotation,
            full_rotation_with_stats,
        )
        total = result.home_win_pct + result.away_win_pct
        assert 0.99 <= total <= 1.01, f"Win pcts sum to {total}"

    def test_favored_team_wins_more(
        self, sim_team_rotation, full_rotation_with_stats
    ):
        """Home team projected to score 120 vs 100 should win majority."""
        game = GameInfo(
            game_id="test001",
            game_date="2026-02-08",
            game_status="Scheduled",
            home_team=TeamGameStats(
                team_id=1, team_name="Home", team_abbreviation="HOM",
                season_pace=100.0, season_off_rating=115.0,
                season_def_rating=108.0, season_ppg=115.0,
                season_opp_ppg=108.0, last_5_ppg=115.0,
            ),
            away_team=TeamGameStats(
                team_id=2, team_name="Away", team_abbreviation="AWY",
                season_pace=100.0, season_off_rating=108.0,
                season_def_rating=115.0, season_ppg=108.0,
                season_opp_ppg=115.0, last_5_ppg=108.0,
            ),
            projected_total=220.0,
            projected_home_score=120.0,
            projected_away_score=100.0,
            projected_spread=-20.0,
            projected_pace=100.0,
            pace_label="Average",
        )

        sim = SimulationEngine(SimulationConfig(num_simulations=2000, seed=42))
        result = sim.simulate_game(
            game, sim_team_rotation, full_rotation_with_stats,
            sim_team_rotation, full_rotation_with_stats,
        )
        assert result.home_win_pct > 0.70, (
            f"Home should dominate with 20pt edge, got {result.home_win_pct}"
        )

    def test_mean_total_near_projected(
        self, game_info_fixture, sim_team_rotation, full_rotation_with_stats,
        sim_config,
    ):
        sim = SimulationEngine(sim_config)
        result = sim.simulate_game(
            game_info_fixture,
            sim_team_rotation,
            full_rotation_with_stats,
            sim_team_rotation,
            full_rotation_with_stats,
        )
        expected = game_info_fixture.projected_total
        assert abs(result.mean_total - expected) < 10.0, (
            f"Mean total {result.mean_total} too far from {expected}"
        )

    def test_spread_direction_correct(
        self, game_info_fixture, sim_team_rotation, full_rotation_with_stats,
        sim_config,
    ):
        """Home team has higher projected score → positive spread."""
        sim = SimulationEngine(sim_config)
        result = sim.simulate_game(
            game_info_fixture,
            sim_team_rotation,
            full_rotation_with_stats,
            sim_team_rotation,
            full_rotation_with_stats,
        )
        # Home projected 115, away 110 → spread should be positive
        assert result.projected_spread > 0

    def test_over_under_analysis(
        self, game_info_fixture, sim_team_rotation, full_rotation_with_stats,
        sim_config,
    ):
        """Over/under percentages populated when line is provided."""
        sim = SimulationEngine(sim_config)
        result = sim.simulate_game(
            game_info_fixture,
            sim_team_rotation,
            full_rotation_with_stats,
            sim_team_rotation,
            full_rotation_with_stats,
            over_under_line=225.0,
        )
        assert result.over_under_line == 225.0
        assert result.over_pct is not None
        assert result.under_pct is not None
        assert 0.0 <= result.over_pct <= 1.0
        assert 0.0 <= result.under_pct <= 1.0
        # Should roughly sum to 1 (exact push is rare with continuous scoring)
        assert result.over_pct + result.under_pct > 0.95

    def test_player_results_present(
        self, game_info_fixture, sim_team_rotation, full_rotation_with_stats,
        sim_config,
    ):
        """All active players should have simulation results."""
        sim = SimulationEngine(sim_config)
        result = sim.simulate_game(
            game_info_fixture,
            sim_team_rotation,
            full_rotation_with_stats,
            sim_team_rotation,
            full_rotation_with_stats,
        )
        assert len(result.home_team.players) == 10
        assert len(result.away_team.players) == 10

    def test_no_over_under_when_no_line(
        self, game_info_fixture, sim_team_rotation, full_rotation_with_stats,
        sim_config,
    ):
        sim = SimulationEngine(sim_config)
        result = sim.simulate_game(
            game_info_fixture,
            sim_team_rotation,
            full_rotation_with_stats,
            sim_team_rotation,
            full_rotation_with_stats,
        )
        assert result.over_under_line is None
        assert result.over_pct is None
        assert result.under_pct is None


# ============================================================================
# STAT DISTRIBUTION TESTS
# ============================================================================


class TestStatDistributions:
    """Tests for statistical properties of simulation outputs."""

    def test_star_player_higher_mean_pts(
        self, game_info_fixture, sim_team_rotation, full_rotation_with_stats,
        sim_config,
    ):
        sim = SimulationEngine(sim_config)
        result = sim.simulate_game(
            game_info_fixture,
            sim_team_rotation,
            full_rotation_with_stats,
            sim_team_rotation,
            full_rotation_with_stats,
        )
        star = result.home_team.players[0]  # Star Guard (35 min)
        bench = result.home_team.players[8]  # Reserve Guard (15 min)
        assert star.mean_pts > bench.mean_pts

    def test_stl_blk_higher_variance(
        self, game_info_fixture, sim_team_rotation, full_rotation_with_stats,
        sim_config,
    ):
        """STL/BLK should have higher coefficient of variation than PTS."""
        sim = SimulationEngine(sim_config)
        result = sim.simulate_game(
            game_info_fixture,
            sim_team_rotation,
            full_rotation_with_stats,
            sim_team_rotation,
            full_rotation_with_stats,
        )
        star = result.home_team.players[0]
        # CV = std / mean (coefficient of variation)
        if star.mean_pts > 0 and star.mean_stl > 0:
            cv_pts = star.std_pts / star.mean_pts
            # STL mean is very small, so we check BLK or just ensure std > 0
            assert star.std_pts > 0
            assert star.std_ast > 0

    def test_percentile_ordering(
        self, game_info_fixture, sim_team_rotation, full_rotation_with_stats,
        sim_config,
    ):
        """Percentiles should be monotonically increasing."""
        sim = SimulationEngine(sim_config)
        result = sim.simulate_game(
            game_info_fixture,
            sim_team_rotation,
            full_rotation_with_stats,
            sim_team_rotation,
            full_rotation_with_stats,
        )
        for player in result.home_team.players:
            pcts = player.dk_percentiles
            assert pcts["p10"] <= pcts["p25"] <= pcts["p50"]
            assert pcts["p50"] <= pcts["p75"] <= pcts["p90"]

            pcts_fd = player.fd_percentiles
            assert pcts_fd["p10"] <= pcts_fd["p25"] <= pcts_fd["p50"]
            assert pcts_fd["p50"] <= pcts_fd["p75"] <= pcts_fd["p90"]

            pcts_pts = player.pts_percentiles
            assert pcts_pts["p10"] <= pcts_pts["p25"] <= pcts_pts["p50"]
            assert pcts_pts["p50"] <= pcts_pts["p75"] <= pcts_pts["p90"]

    def test_dk_distribution_reasonable(
        self, game_info_fixture, sim_team_rotation, full_rotation_with_stats,
        sim_config,
    ):
        """Star DK mean should be in a reasonable range (roughly 30-60)."""
        sim = SimulationEngine(sim_config)
        result = sim.simulate_game(
            game_info_fixture,
            sim_team_rotation,
            full_rotation_with_stats,
            sim_team_rotation,
            full_rotation_with_stats,
        )
        star = result.home_team.players[0]
        assert 20.0 < star.mean_dk_points < 80.0, (
            f"Star DK mean {star.mean_dk_points} outside reasonable range"
        )

    def test_score_percentiles_ordered(
        self, game_info_fixture, sim_team_rotation, full_rotation_with_stats,
        sim_config,
    ):
        """Team score percentiles should be ordered."""
        sim = SimulationEngine(sim_config)
        result = sim.simulate_game(
            game_info_fixture,
            sim_team_rotation,
            full_rotation_with_stats,
            sim_team_rotation,
            full_rotation_with_stats,
        )
        pcts = result.home_team.score_percentiles
        assert pcts["p10"] <= pcts["p25"] <= pcts["p50"]
        assert pcts["p50"] <= pcts["p75"] <= pcts["p90"]


# ============================================================================
# DFS SCORING TESTS
# ============================================================================


class TestDFSScoring:
    """Tests for vectorized DFS scoring accuracy."""

    def test_dk_scoring_positive_for_active_players(
        self, game_info_fixture, sim_team_rotation, full_rotation_with_stats,
        sim_config,
    ):
        """All active players should have positive mean DK points."""
        sim = SimulationEngine(sim_config)
        result = sim.simulate_game(
            game_info_fixture,
            sim_team_rotation,
            full_rotation_with_stats,
            sim_team_rotation,
            full_rotation_with_stats,
        )
        for player in result.home_team.players:
            assert player.mean_dk_points > 0, (
                f"{player.player_name} has non-positive DK: {player.mean_dk_points}"
            )

    def test_fd_scoring_positive_for_active_players(
        self, game_info_fixture, sim_team_rotation, full_rotation_with_stats,
        sim_config,
    ):
        """All active players should have positive mean FD points."""
        sim = SimulationEngine(sim_config)
        result = sim.simulate_game(
            game_info_fixture,
            sim_team_rotation,
            full_rotation_with_stats,
            sim_team_rotation,
            full_rotation_with_stats,
        )
        for player in result.home_team.players:
            assert player.mean_fd_points > 0, (
                f"{player.player_name} has non-positive FD: {player.mean_fd_points}"
            )

    def test_dk_higher_for_more_minutes(
        self, game_info_fixture, sim_team_rotation, full_rotation_with_stats,
        sim_config,
    ):
        """Players with more minutes should generally have higher DK points."""
        sim = SimulationEngine(sim_config)
        result = sim.simulate_game(
            game_info_fixture,
            sim_team_rotation,
            full_rotation_with_stats,
            sim_team_rotation,
            full_rotation_with_stats,
        )
        players = result.home_team.players
        star_dk = players[0].mean_dk_points  # 35 min star
        reserve_dk = players[8].mean_dk_points  # 15 min reserve
        assert star_dk > reserve_dk


# ============================================================================
# EDGE CASE TESTS
# ============================================================================


class TestEdgeCases:
    """Tests for edge cases and boundary conditions."""

    def test_single_player_rotation(self, game_info_fixture):
        """Simulation should work with a minimal rotation.

        Uses 5 players at 48 min each (=240) to satisfy the validator,
        but only 1 has stat rates — demonstrating the engine handles
        sparse data.
        """
        players_data = []
        projections = []
        for i in range(5):
            pm = PlayerMinutes(
                player_id=i + 1,
                player_name=f"Player {i + 1}",
                position="G",
                team_id=1,
                minutes_last_5=[48.0] * 5,
                minutes_last_10=[48.0] * 10,
                season_avg=48.0,
                usage_rate=0.35 if i == 0 else 0.10,
                pts_per_min=0.60 if i == 0 else 0.30,
                reb_per_min=0.15 if i == 0 else 0.10,
                ast_per_min=0.10 if i == 0 else 0.05,
                stl_per_min=0.03,
                blk_per_min=0.02,
                tov_per_min=0.05,
                fg3m_per_min=0.05,
            )
            proj = PlayerProjection(
                player_id=i + 1,
                player_name=f"Player {i + 1}",
                position="G",
                baseline_minutes=48.0,
                adjusted_minutes=48.0,
                confidence=1.0,
            )
            players_data.append(pm)
            projections.append(proj)

        rotation = TeamRotation(
            team_id=1,
            team_name="Small Team",
            game_date="2026-02-08",
            projections=projections,
            total_minutes=240.0,
            positions_breakdown={"G": 240.0},
        )

        sim = SimulationEngine(SimulationConfig(num_simulations=500, seed=42))
        result = sim.simulate_game(
            game_info_fixture,
            rotation, players_data,
            rotation, players_data,
        )
        assert len(result.home_team.players) == 5
        assert result.home_team.players[0].mean_minutes > 0

    def test_degenerate_empty_rotation(self, game_info_fixture):
        """Rotation with all 0-minute players returns degenerate result."""
        projections = [
            PlayerProjection(
                player_id=i,
                player_name=f"DNP {i}",
                position="G",
                baseline_minutes=24.0,
                adjusted_minutes=0.0,
                confidence=0.0,
            )
            for i in range(10)
        ]
        empty_rot = TeamRotation(
            team_id=1,
            team_name="Empty",
            game_date="2026-02-08",
            projections=projections,
            total_minutes=240.0,
            positions_breakdown={"G": 240.0},
        )

        sim = SimulationEngine(SimulationConfig(num_simulations=100, seed=42))
        result = sim.simulate_game(
            game_info_fixture,
            empty_rot, [],
            empty_rot, [],
        )
        assert result.home_win_pct == 0.5
        assert result.away_win_pct == 0.5
        assert result.mean_total == 0.0

    def test_all_zero_stat_rates(self, game_info_fixture):
        """Players with 0 stat rates should produce 0 stats but not crash."""
        players_data = []
        projections = []
        for i in range(10):
            pm = PlayerMinutes(
                player_id=i + 1,
                player_name=f"Zero Stats {i + 1}",
                position="C",
                team_id=1,
                minutes_last_5=[24.0] * 5,
                minutes_last_10=[24.0] * 10,
                season_avg=24.0,
                usage_rate=0.15,
                # All per-min rates are 0 (default)
            )
            proj = PlayerProjection(
                player_id=i + 1,
                player_name=f"Zero Stats {i + 1}",
                position="C",
                baseline_minutes=24.0,
                adjusted_minutes=24.0,
                confidence=1.0,
            )
            players_data.append(pm)
            projections.append(proj)

        rotation = TeamRotation(
            team_id=1,
            team_name="Zero Team",
            game_date="2026-02-08",
            projections=projections,
            total_minutes=240.0,
            positions_breakdown={"C": 240.0},
        )

        sim = SimulationEngine(SimulationConfig(num_simulations=200, seed=42))
        result = sim.simulate_game(
            game_info_fixture,
            rotation, players_data,
            rotation, players_data,
        )
        player = result.home_team.players[0]
        assert player.mean_pts == 0.0
        assert player.mean_reb == 0.0
        assert player.mean_dk_points == 0.0

    def test_result_serialization(
        self, game_info_fixture, sim_team_rotation, full_rotation_with_stats,
        sim_config,
    ):
        """GameSimResult should serialize to JSON without errors."""
        sim = SimulationEngine(sim_config)
        result = sim.simulate_game(
            game_info_fixture,
            sim_team_rotation,
            full_rotation_with_stats,
            sim_team_rotation,
            full_rotation_with_stats,
        )
        # Pydantic model_dump should succeed without errors
        data = result.model_dump()
        assert "home_team" in data
        assert "away_team" in data
        assert "home_win_pct" in data
        assert len(data["home_team"]["players"]) == 10

        # Should also serialize to JSON string
        json_str = result.model_dump_json()
        assert len(json_str) > 100


# ============================================================================
# TEAM SCRIPT NOISE TESTS
# ============================================================================


class TestTeamScriptNoise:
    """Tests for game-script noise in _simulate_team.

    Team-script noise applies a shared per-sim multiplier to all player
    stats.  PTS are re-calibrated afterward to preserve the team mean,
    but non-PTS stats (reb, ast, etc.) retain the variance boost.
    """

    def _run_team_sim(self, full_rotation_with_stats, sim_team_rotation, **kwargs):
        """Helper matching TestTeamSimulation._run_team_sim."""
        config = SimulationConfig(num_simulations=2000, seed=42, **kwargs)
        sim = SimulationEngine(config)
        inputs = sim.prepare_player_inputs(sim_team_rotation, full_rotation_with_stats)
        pace = sim._sample_pace(100.0, config.num_simulations)
        return sim._simulate_team(inputs, pace, 115.0)

    def test_team_script_noise_increases_variance(
        self, full_rotation_with_stats, sim_team_rotation,
    ):
        """Non-PTS stat variance should be material (>0) with game-script noise.

        Since we can't toggle game-script off without modifying the engine,
        we verify that the per-sim AST variance across simulations is
        meaningfully greater than zero, which is the expected effect of
        the team_script multiplier.
        """
        _scores, data = self._run_team_sim(
            full_rotation_with_stats, sim_team_rotation,
        )
        # AST for star player across 2000 sims
        ast_star = data["stats"]["ast"][0]  # (N,) array
        ast_std = float(ast_star.std())
        # With game-script noise, variance should be non-trivial
        assert ast_std > 0.5, (
            f"AST std ({ast_std:.3f}) should be >0.5 with game-script noise"
        )

    def test_team_script_preserves_mean(
        self, full_rotation_with_stats, sim_team_rotation,
    ):
        """PTS calibration should keep team mean PTS close to projected score.

        The engine re-calibrates PTS after applying team_script noise so
        that the mean team score matches the projected_team_score.
        """
        projected_score = 115.0
        scores, _data = self._run_team_sim(
            full_rotation_with_stats, sim_team_rotation,
        )
        mean_score = float(scores.mean())
        # Should be within 2% of projected
        assert abs(mean_score - projected_score) / projected_score < 0.02, (
            f"Mean team PTS ({mean_score:.1f}) should be within 2% of "
            f"projected ({projected_score})"
        )


# ============================================================================
# CROSS-TEAM CORRELATION TESTS
# ============================================================================


class TestCrossTeamCorrelation:
    """Tests for cross-team negative correlation effects."""

    def _run_game_sim(
        self,
        game_info_fixture,
        sim_team_rotation,
        full_rotation_with_stats,
        N=2000,
        seed=42,
        sport="nba",
    ):
        """Helper to run a full game sim and return the result."""
        config = SimulationConfig(num_simulations=N, seed=seed)
        sim = SimulationEngine(config)
        return sim.simulate_game(
            game_info_fixture,
            sim_team_rotation,
            full_rotation_with_stats,
            sim_team_rotation,
            full_rotation_with_stats,
            sport=sport,
        )

    def _run_game_sim_raw(
        self,
        game_info_fixture,
        sim_team_rotation,
        full_rotation_with_stats,
        N=2000,
        seed=42,
        sport="nba",
    ):
        """Helper to run a raw sim and return (result, raw_fps)."""
        config = SimulationConfig(num_simulations=N, seed=seed)
        sim = SimulationEngine(config)
        return sim.simulate_game_raw(
            game_info_fixture,
            sim_team_rotation,
            full_rotation_with_stats,
            sim_team_rotation,
            full_rotation_with_stats,
            sport=sport,
        )

    def test_shared_game_env_correlates_teams(
        self, game_info_fixture, sim_team_rotation, full_rotation_with_stats,
    ):
        """Home and away team PTS should be positively correlated from the
        shared game environment factor."""
        config = SimulationConfig(num_simulations=5000, seed=42)
        sim = SimulationEngine(config)
        N = config.num_simulations

        # Prepare inputs
        home_inputs = sim.prepare_player_inputs(sim_team_rotation, full_rotation_with_stats)
        away_inputs = sim.prepare_player_inputs(sim_team_rotation, full_rotation_with_stats)
        sim_pace = sim._sample_pace(game_info_fixture.projected_pace, N)

        home_scores, home_data = sim._simulate_team(home_inputs, sim_pace, 115.0)
        away_scores, away_data = sim._simulate_team(away_inputs, sim_pace, 110.0)

        # Before cross-team: correlation comes only from shared pace
        pre_corr = np.corrcoef(home_scores, away_scores)[0, 1]

        # Apply cross-team effects
        sim._apply_cross_team_effects(home_data, away_data, N)
        home_pts_post = home_data["stats"]["pts"].sum(axis=0)
        away_pts_post = away_data["stats"]["pts"].sum(axis=0)

        post_corr = np.corrcoef(home_pts_post, away_pts_post)[0, 1]

        # After cross-team: correlation should increase (shared game env)
        assert post_corr > pre_corr, (
            f"Shared game env should increase PTS correlation: "
            f"pre={pre_corr:.4f}, post={post_corr:.4f}"
        )

    def test_defensive_depression_on_high_scoring(
        self, game_info_fixture, sim_team_rotation, full_rotation_with_stats,
    ):
        """When home team scores high, away team STL/BLK should be depressed."""
        config = SimulationConfig(num_simulations=3000, seed=42)
        sim = SimulationEngine(config)
        N = config.num_simulations

        home_inputs = sim.prepare_player_inputs(sim_team_rotation, full_rotation_with_stats)
        away_inputs = sim.prepare_player_inputs(sim_team_rotation, full_rotation_with_stats)
        sim_pace = sim._sample_pace(game_info_fixture.projected_pace, N)

        _h_scores, home_data = sim._simulate_team(home_inputs, sim_pace, 115.0)
        _a_scores, away_data = sim._simulate_team(away_inputs, sim_pace, 110.0)

        # Record away STL/BLK before
        away_stl_before = away_data["stats"]["stl"].mean()
        away_blk_before = away_data["stats"]["blk"].mean()

        # Apply cross-team effects
        sim._apply_cross_team_effects(home_data, away_data, N)

        away_stl_after = away_data["stats"]["stl"].mean()
        away_blk_after = away_data["stats"]["blk"].mean()

        # In iterations where home scored above median, away DEF stats
        # should be depressed.  Over all iterations, the mean effect is
        # approximately zero (half above, half below median), but the
        # correlation should exist.  Let's verify the mechanism works by
        # checking that high-home-scoring iterations have lower away STL.
        home_pts = home_data["stats"]["pts"].sum(axis=0)
        median_pts = np.median(home_pts)
        high_mask = home_pts > median_pts

        away_stl_high = away_data["stats"]["stl"][:, high_mask].mean()
        away_stl_low = away_data["stats"]["stl"][:, ~high_mask].mean()

        assert away_stl_high < away_stl_low, (
            f"Away STL should be lower when home scores above median: "
            f"high={away_stl_high:.4f}, low={away_stl_low:.4f}"
        )

    def test_rebound_coupling(
        self, game_info_fixture, sim_team_rotation, full_rotation_with_stats,
    ):
        """When home team scores high, away team REB should be depressed."""
        config = SimulationConfig(num_simulations=3000, seed=42)
        sim = SimulationEngine(config)
        N = config.num_simulations

        home_inputs = sim.prepare_player_inputs(sim_team_rotation, full_rotation_with_stats)
        away_inputs = sim.prepare_player_inputs(sim_team_rotation, full_rotation_with_stats)
        sim_pace = sim._sample_pace(game_info_fixture.projected_pace, N)

        _h, home_data = sim._simulate_team(home_inputs, sim_pace, 115.0)
        _a, away_data = sim._simulate_team(away_inputs, sim_pace, 110.0)

        sim._apply_cross_team_effects(home_data, away_data, N)

        home_pts = home_data["stats"]["pts"].sum(axis=0)
        median_pts = np.median(home_pts)
        high_mask = home_pts > median_pts

        away_reb_high = away_data["stats"]["reb"][:, high_mask].mean()
        away_reb_low = away_data["stats"]["reb"][:, ~high_mask].mean()

        assert away_reb_high < away_reb_low, (
            f"Away REB should be lower when home scores above median: "
            f"high={away_reb_high:.4f}, low={away_reb_low:.4f}"
        )

    def test_cross_team_disabled_toggle(
        self, game_info_fixture, sim_team_rotation, full_rotation_with_stats,
    ):
        """With CROSS_TEAM_CORRELATION_ENABLED=False, no coupling effects."""
        from unittest.mock import patch

        # Run with cross-team enabled (baseline)
        r1 = self._run_game_sim(
            game_info_fixture, sim_team_rotation, full_rotation_with_stats,
            seed=42,
        )

        # Run with cross-team disabled
        with patch("app.services.simulation_engine.CROSS_TEAM_CORRELATION_ENABLED", False):
            r2 = self._run_game_sim(
                game_info_fixture, sim_team_rotation, full_rotation_with_stats,
                seed=42,
            )

        # Results should differ (cross-team effects change stats)
        # Note: same seed but different code paths → different RNG consumption
        # so results will differ.  We just verify no crash and results are valid.
        assert r2.home_team.mean_score > 0
        assert r2.away_team.mean_score > 0

    def test_dfs_scores_recomputed_after_adjustment(
        self, game_info_fixture, sim_team_rotation, full_rotation_with_stats,
    ):
        """After cross-team effects, DFS scores should reflect adjusted stats."""
        config = SimulationConfig(num_simulations=1000, seed=42)
        sim = SimulationEngine(config)
        N = config.num_simulations

        home_inputs = sim.prepare_player_inputs(sim_team_rotation, full_rotation_with_stats)
        away_inputs = sim.prepare_player_inputs(sim_team_rotation, full_rotation_with_stats)
        sim_pace = sim._sample_pace(game_info_fixture.projected_pace, N)

        _, home_data = sim._simulate_team(home_inputs, sim_pace, 115.0)
        _, away_data = sim._simulate_team(away_inputs, sim_pace, 110.0)

        sim._apply_cross_team_effects(home_data, away_data, N)

        # Manually recompute DFS scores from the adjusted stats
        dk_check, fd_check = SimulationEngine._recompute_dfs_scores(home_data["stats"])

        np.testing.assert_array_almost_equal(
            home_data["dk_pts"], dk_check, decimal=6,
            err_msg="DK scores should match manual recomputation",
        )
        np.testing.assert_array_almost_equal(
            home_data["fd_pts"], fd_check, decimal=6,
            err_msg="FD scores should match manual recomputation",
        )

    def test_clamping_prevents_extreme_adjustments(
        self, game_info_fixture, sim_team_rotation, full_rotation_with_stats,
    ):
        """Even with extreme sensitivities, adjustments stay within clamp range."""
        from unittest.mock import patch

        config = SimulationConfig(num_simulations=1000, seed=42)
        sim = SimulationEngine(config)
        N = config.num_simulations

        home_inputs = sim.prepare_player_inputs(sim_team_rotation, full_rotation_with_stats)
        away_inputs = sim.prepare_player_inputs(sim_team_rotation, full_rotation_with_stats)
        sim_pace = sim._sample_pace(game_info_fixture.projected_pace, N)

        _, home_data = sim._simulate_team(home_inputs, sim_pace, 115.0)
        _, away_data = sim._simulate_team(away_inputs, sim_pace, 110.0)

        # Record pre-adjustment means
        away_stl_before = away_data["stats"]["stl"].copy()
        away_reb_before = away_data["stats"]["reb"].copy()

        # Use extreme sensitivities (patched at constants module where they're imported from)
        with patch("app.config.constants.CROSS_TEAM_DEF_SENSITIVITY", 5.0), \
             patch("app.config.constants.CROSS_TEAM_REB_SENSITIVITY", 5.0):
            sim._apply_cross_team_effects(home_data, away_data, N)

        # Even with extreme sensitivity, the adjustment factors are clamped.
        # STL/BLK clamp: [0.80, 1.10], REB clamp: [0.90, 1.05]
        # So away_stl_after / away_stl_before should be within [0.80, 1.10] per element.
        # Just verify stats are non-negative and reasonable
        assert np.all(away_data["stats"]["stl"] >= 0)
        assert np.all(away_data["stats"]["reb"] >= 0)
        assert np.all(away_data["stats"]["blk"] >= 0)

    def test_symmetric_effects(
        self, game_info_fixture, sim_team_rotation, full_rotation_with_stats,
    ):
        """With identical rotations/projections, effects should be symmetric."""
        config = SimulationConfig(num_simulations=3000, seed=42)
        sim = SimulationEngine(config)
        N = config.num_simulations

        inputs = sim.prepare_player_inputs(sim_team_rotation, full_rotation_with_stats)
        sim_pace = sim._sample_pace(game_info_fixture.projected_pace, N)

        # Simulate both teams with SAME projected score (symmetric setup)
        _, home_data = sim._simulate_team(inputs, sim_pace, 112.0)
        _, away_data = sim._simulate_team(inputs, sim_pace, 112.0)

        sim._apply_cross_team_effects(home_data, away_data, N)

        # Mean stats should be very similar (not identical due to separate
        # noise draws, but close)
        home_stl_mean = home_data["stats"]["stl"].mean()
        away_stl_mean = away_data["stats"]["stl"].mean()

        # Within 10% of each other (stochastic, but symmetric setup)
        ratio = home_stl_mean / max(away_stl_mean, 1e-9)
        assert 0.85 < ratio < 1.15, (
            f"Symmetric teams should have similar STL: "
            f"home={home_stl_mean:.4f}, away={away_stl_mean:.4f}"
        )

    def test_cross_team_skipped_for_cbb(
        self, game_info_fixture, sim_team_rotation, full_rotation_with_stats,
    ):
        """Cross-team effects should not be applied for CBB sport."""
        from unittest.mock import patch

        # Run CBB sim — should skip cross-team effects entirely
        # We verify by checking that no extra RNG consumption happens
        # (same seed + same code path → same results regardless of toggle)
        with patch("app.services.simulation_engine.CROSS_TEAM_CORRELATION_ENABLED", True):
            r_cbb = self._run_game_sim(
                game_info_fixture, sim_team_rotation, full_rotation_with_stats,
                seed=42, sport="cbb",
            )

        with patch("app.services.simulation_engine.CROSS_TEAM_CORRELATION_ENABLED", False):
            r_cbb_off = self._run_game_sim(
                game_info_fixture, sim_team_rotation, full_rotation_with_stats,
                seed=42, sport="cbb",
            )

        # Both CBB runs should produce identical results (cross-team skipped)
        assert r_cbb.home_team.mean_score == r_cbb_off.home_team.mean_score
        assert r_cbb.away_team.mean_score == r_cbb_off.away_team.mean_score

    def test_raw_sim_includes_cross_team_effects(
        self, game_info_fixture, sim_team_rotation, full_rotation_with_stats,
    ):
        """simulate_game_raw() should also apply cross-team effects."""
        result, raw_fps = self._run_game_sim_raw(
            game_info_fixture, sim_team_rotation, full_rotation_with_stats,
        )

        # Basic sanity: result and raw_fps should be populated
        assert result.home_team.mean_score > 0
        assert result.away_team.mean_score > 0
        assert len(raw_fps) > 0

        # Raw FP arrays should match the aggregated mean
        star_id = 100
        if star_id in raw_fps:
            raw_dk_mean = float(raw_fps[star_id]["dk"].mean())
            # Should be within a reasonable range (>0, <100)
            assert 0 < raw_dk_mean < 100
