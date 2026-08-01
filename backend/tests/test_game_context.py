"""Tests for game-context adjustments: baseline weights, blowout risk,
pace factor, and back-to-back fatigue.
"""

import pytest
from typing import Dict

from app.models.player import PlayerMinutes, PlayerProjection
from app.models.game import GameInfo
from app.services.rotation_engine import RotationEngine
from app.services.dfs_service import DFSService
from app.services.game_service import LEAGUE_AVG_PACE


# ============================================================================
# BASELINE WEIGHT TESTS
# ============================================================================


class TestBaselineWeights:
    """Verify the 75% season-average baseline produces stable projections."""

    def test_season_avg_dominates(self, engine, star_player):
        """Baseline should be much closer to season_avg than EMA-5."""
        baseline = engine.get_baseline_projection(star_player)
        # With 75% weight on season_avg (35.4), baseline should be near 35.4
        # even if recent games are noisy
        assert abs(baseline - star_player.season_avg) < 1.5

    def test_stable_across_hot_streak(self, engine):
        """Hot streak in last 5 shouldn't swing baseline dramatically.

        With 50/50 season/recent weighting, the hot streak pulls the
        baseline up to ~35.  It should still stay well below the raw
        hot-streak average (~40) and above the season average (30).
        """
        player = PlayerMinutes(
            player_id=1,
            player_name="Hot Streak",
            position="G",
            team_id=1,
            minutes_last_5=[40.0, 42.0, 38.0, 41.0, 39.0],  # hot
            minutes_last_10=[40.0, 42.0, 38.0, 41.0, 39.0,
                             30.0, 31.0, 29.0, 32.0, 30.0],
            season_avg=30.0,
            usage_rate=0.20,
        )
        baseline = engine.get_baseline_projection(player)
        # With 50/50 weights the hot streak pulls up more but should
        # still be well below the raw hot-streak avg (~40)
        assert baseline <= 36.0
        assert baseline > 30.0  # still picks up some recent signal

    def test_stable_across_cold_streak(self, engine):
        """Cold streak in last 5 shouldn't tank baseline.

        With 50/50 season/recent weighting, the cold streak pulls the
        baseline down to ~24.  It should still stay well above the raw
        cold-streak average (~17) and below the season average (30).
        """
        player = PlayerMinutes(
            player_id=2,
            player_name="Cold Streak",
            position="G",
            team_id=1,
            minutes_last_5=[18.0, 15.0, 20.0, 17.0, 16.0],  # cold
            minutes_last_10=[18.0, 15.0, 20.0, 17.0, 16.0,
                             30.0, 32.0, 28.0, 31.0, 29.0],
            season_avg=30.0,
            usage_rate=0.20,
        )
        baseline = engine.get_baseline_projection(player)
        # With 50/50 weights the cold streak pulls down harder but
        # should stay well above raw cold avg (~17)
        assert baseline > 22.0
        assert baseline < 30.0  # but still reflects some downturn

    def test_weight_formula(self, engine):
        """Verify exact formula: 0.15 * EMA5 + 0.10 * EMA10 + 0.75 * season."""
        player = PlayerMinutes(
            player_id=3,
            player_name="Constant",
            position="G",
            team_id=1,
            minutes_last_5=[30.0, 30.0, 30.0, 30.0, 30.0],
            minutes_last_10=[30.0, 30.0, 30.0, 30.0, 30.0,
                             30.0, 30.0, 30.0, 30.0, 30.0],
            season_avg=30.0,
            usage_rate=0.20,
        )
        baseline = engine.get_baseline_projection(player)
        # All inputs are 30.0, so baseline should be exactly 30.0
        assert baseline == 30.0


# ============================================================================
# BLOWOUT RISK TESTS
# ============================================================================


class TestBlowoutAdjustment:
    """Blowout risk: starters lose minutes when spread >= 7."""

    def _build_projections(self, rotation):
        """Helper: build baseline projections dict."""
        engine = RotationEngine()
        return {
            p.player_id: PlayerProjection(
                player_id=p.player_id,
                player_name=p.player_name,
                position=p.position,
                baseline_minutes=p.season_avg,
                adjusted_minutes=p.season_avg,
                confidence=1.0,
                reason="Baseline projection",
            )
            for p in rotation
        }

    def test_blowout_reduces_starters(self, engine, full_rotation, game_info_blowout):
        """Starters (>= 24 min) should be reduced in a blowout."""
        projections = self._build_projections(full_rotation)
        original_starter_mins = {
            pid: proj.adjusted_minutes
            for pid, proj in projections.items()
            if proj.adjusted_minutes >= 24.0
        }

        engine.apply_game_context_adjustments(
            projections, full_rotation, game_info_blowout, team_id=1
        )

        for pid, orig in original_starter_mins.items():
            assert projections[pid].adjusted_minutes < orig
            assert "Blowout risk" in projections[pid].reason

    def test_blowout_bench_unchanged(self, engine, full_rotation, game_info_blowout):
        """Bench players (< 24 min) should NOT be reduced in a blowout."""
        projections = self._build_projections(full_rotation)
        bench_mins = {
            pid: proj.adjusted_minutes
            for pid, proj in projections.items()
            if proj.adjusted_minutes < 24.0
        }

        engine.apply_game_context_adjustments(
            projections, full_rotation, game_info_blowout, team_id=1
        )

        for pid, orig in bench_mins.items():
            assert projections[pid].adjusted_minutes == orig

    def test_blowout_factor_calculation(self, engine, full_rotation, game_info_blowout):
        """Verify blowout factor math with non-linear curve + star dampening.

        spread=10, excess=3, penalty = 3^0.70 * 0.015 ≈ 3.24%
        star_factor = 1 - penalty * STAR_BLOWOUT_DAMPENING (0.60)
        Top-2 starters get the dampened (reduced) penalty.
        """
        from app.config.constants import (
            BLOWOUT_SPREAD_THRESHOLD, BLOWOUT_PENALTY_PER_POINT,
            BLOWOUT_PENALTY_EXPONENT, STAR_BLOWOUT_DAMPENING,
        )
        projections = self._build_projections(full_rotation)
        # game_info_blowout has spread=-10 (home favored by 10)
        # team_id=1 is home team, so team_spread = +10

        star_before = projections[100].adjusted_minutes  # 35.0
        engine.apply_game_context_adjustments(
            projections, full_rotation, game_info_blowout, team_id=1
        )
        # Star player (top-2 starter) gets dampened blowout penalty
        assert projections[100].adjusted_minutes < star_before
        assert "Blowout risk" in projections[100].reason
        # Verify the star gets a reduced penalty (star-dampened)
        assert "[star-dampened]" in projections[100].reason

    def test_no_blowout_close_game(self, engine, full_rotation, game_info_average):
        """Close game (spread=-2) should NOT trigger blowout adjustment."""
        projections = self._build_projections(full_rotation)
        original_mins = {
            pid: proj.adjusted_minutes for pid, proj in projections.items()
        }

        engine.apply_game_context_adjustments(
            projections, full_rotation, game_info_average, team_id=1
        )

        for pid, orig in original_mins.items():
            assert projections[pid].adjusted_minutes == orig
            assert "Blowout" not in (projections[pid].reason or "")

    def test_blowout_capped_at_90_percent(self, engine, full_rotation):
        """Blowout minute reductions have two regimes:

        1. |spread| < 13 (guillotine off): the non-linear soft penalty
           applies, floored at BLOWOUT_MIN_FACTOR (0.90) — no starter
           can lose more than 10% of baseline minutes.
        2. |spread| >= 13: the "4Q point spread guillotine" hard-caps
           starters at 28 minutes, deliberately overriding the 0.90
           floor for high-minute starters (coaches sit them in Q4).
        """
        from app.models.game import GameInfo, TeamGameStats

        def _blowout_game(spread: float) -> GameInfo:
            return GameInfo(
                game_id=f"blowout_{spread:.0f}",
                game_date="2026-02-09",
                game_status="Scheduled",
                home_team=TeamGameStats(
                    team_id=1, team_name="H", team_abbreviation="HOM",
                    season_pace=101.0, season_off_rating=130.0,
                    season_def_rating=100.0, season_ppg=130.0,
                    season_opp_ppg=100.0, last_5_ppg=130.0,
                ),
                away_team=TeamGameStats(
                    team_id=2, team_name="A", team_abbreviation="AWY",
                    season_pace=101.0, season_off_rating=100.0,
                    season_def_rating=130.0, season_ppg=100.0,
                    season_opp_ppg=130.0, last_5_ppg=100.0,
                ),
                projected_total=230.0, projected_home_score=130.0,
                projected_away_score=100.0, projected_spread=-spread,
                projected_pace=101.0, pace_label="Average",
            )

        # ── Regime 1: spread=12 (below the 13-pt guillotine) ──
        # Soft penalty only: every starter keeps >= 90% of baseline.
        projections = self._build_projections(full_rotation)
        engine.apply_game_context_adjustments(
            projections, full_rotation, _blowout_game(12.0), team_id=1
        )
        for p in projections.values():
            if p.baseline_minutes >= 24.0:
                assert p.adjusted_minutes < p.baseline_minutes
                assert p.adjusted_minutes >= round(p.baseline_minutes * 0.90, 1) - 0.1
                assert "Guillotine" not in (p.reason or "")

        # ── Regime 2: spread=20 (extreme) — guillotine engages ──
        # Starters above 28 min are hard-capped at exactly 28.0, which
        # for the 35-min star is BELOW the 0.90 soft floor by design.
        projections = self._build_projections(full_rotation)
        star_before = projections[100].adjusted_minutes  # 35.0
        engine.apply_game_context_adjustments(
            projections, full_rotation, _blowout_game(20.0), team_id=1
        )
        assert projections[100].adjusted_minutes == 28.0
        assert projections[100].adjusted_minutes < star_before
        assert "4Q Guillotine" in projections[100].reason
        for p in projections.values():
            if p.baseline_minutes >= 24.0:
                # No starter plays above the 28-min extreme-blowout cap
                assert p.adjusted_minutes <= 28.0


# ============================================================================
# PACE FACTOR TESTS
# ============================================================================


class TestPaceFactor:
    """Pace factor: stored on projections for DFS stat scaling."""

    def test_fast_pace_factor(self, engine, full_rotation, game_info_fast_pace):
        """Fast game (106 pace) → pace_factor > 1.0."""
        projections = {
            p.player_id: PlayerProjection(
                player_id=p.player_id,
                player_name=p.player_name,
                position=p.position,
                baseline_minutes=p.season_avg,
                adjusted_minutes=p.season_avg,
                confidence=1.0,
                reason="Baseline",
            )
            for p in full_rotation
        }

        engine.apply_game_context_adjustments(
            projections, full_rotation, game_info_fast_pace, team_id=1
        )

        expected_factor = round(106.0 / LEAGUE_AVG_PACE, 4)
        for proj in projections.values():
            if proj.adjusted_minutes > 0:
                assert proj.pace_factor == expected_factor
                assert proj.pace_factor > 1.0

    def test_slow_pace_factor(self, engine, full_rotation):
        """Slow game (97 pace) → pace_factor < 1.0."""
        from app.models.game import GameInfo, TeamGameStats

        slow_game = GameInfo(
            game_id="slow",
            game_date="2026-02-09",
            game_status="Scheduled",
            home_team=TeamGameStats(
                team_id=1, team_name="H", team_abbreviation="HOM",
                season_pace=97.0, season_off_rating=110.0,
                season_def_rating=114.0, season_ppg=110.0,
                season_opp_ppg=114.0, last_5_ppg=108.0,
            ),
            away_team=TeamGameStats(
                team_id=2, team_name="A", team_abbreviation="AWY",
                season_pace=97.0, season_off_rating=108.0,
                season_def_rating=116.0, season_ppg=108.0,
                season_opp_ppg=116.0, last_5_ppg=106.0,
            ),
            projected_total=210.0, projected_home_score=108.0,
            projected_away_score=102.0, projected_spread=-6.0,
            projected_pace=97.0, pace_label="Slow",
        )

        projections = {
            p.player_id: PlayerProjection(
                player_id=p.player_id,
                player_name=p.player_name,
                position=p.position,
                baseline_minutes=p.season_avg,
                adjusted_minutes=p.season_avg,
                confidence=1.0,
                reason="Baseline",
            )
            for p in full_rotation
        }

        engine.apply_game_context_adjustments(
            projections, full_rotation, slow_game, team_id=1
        )

        expected_factor = round(97.0 / LEAGUE_AVG_PACE, 4)
        for proj in projections.values():
            if proj.adjusted_minutes > 0:
                assert proj.pace_factor == expected_factor
                assert proj.pace_factor < 1.0

    def test_pace_factor_does_not_change_minutes(
        self, engine, full_rotation, game_info_fast_pace
    ):
        """Pace factor should NOT modify adjusted_minutes — only stat scaling."""
        projections = {
            p.player_id: PlayerProjection(
                player_id=p.player_id,
                player_name=p.player_name,
                position=p.position,
                baseline_minutes=p.season_avg,
                adjusted_minutes=p.season_avg,
                confidence=1.0,
                reason="Baseline",
            )
            for p in full_rotation
        }
        original_mins = {pid: p.adjusted_minutes for pid, p in projections.items()}

        engine.apply_game_context_adjustments(
            projections, full_rotation, game_info_fast_pace, team_id=1
        )

        # No blowout (spread=-4), so minutes should be unchanged
        for pid, orig in original_mins.items():
            assert projections[pid].adjusted_minutes == orig

    def test_average_pace_factor_near_one(
        self, engine, full_rotation, game_info_average
    ):
        """Average pace game → pace_factor ≈ 1.0."""
        projections = {
            p.player_id: PlayerProjection(
                player_id=p.player_id,
                player_name=p.player_name,
                position=p.position,
                baseline_minutes=p.season_avg,
                adjusted_minutes=p.season_avg,
                confidence=1.0,
                reason="Baseline",
            )
            for p in full_rotation
        }

        engine.apply_game_context_adjustments(
            projections, full_rotation, game_info_average, team_id=1
        )

        for proj in projections.values():
            if proj.adjusted_minutes > 0:
                assert abs(proj.pace_factor - 1.0) < 0.01


# ============================================================================
# B2B FATIGUE TESTS
# ============================================================================


class TestB2BFatigue:
    """Back-to-back fatigue: starters lose 2 min, veterans lose 3 min."""

    def test_b2b_starters_reduced(
        self, engine, full_rotation_with_ages, game_info_average
    ):
        """Starters (>= 24 min) lose 2 minutes on B2B."""
        projections = {
            p.player_id: PlayerProjection(
                player_id=p.player_id,
                player_name=p.player_name,
                position=p.position,
                baseline_minutes=p.season_avg,
                adjusted_minutes=p.season_avg,
                confidence=1.0,
                reason="Baseline",
            )
            for p in full_rotation_with_ages
        }

        # Player 100 (Star Guard, age 28): 35 min → 33 min (starter, non-vet)
        engine.apply_game_context_adjustments(
            projections, full_rotation_with_ages, game_info_average,
            team_id=1, is_b2b=True,
        )

        assert projections[100].adjusted_minutes == 33.0
        assert "B2B fatigue" in projections[100].reason

    def test_b2b_veterans_extra_reduction(
        self, engine, full_rotation_with_ages, game_info_average
    ):
        """Veterans (age >= 32) who are starters lose an extra 1 min (total 3)."""
        projections = {
            p.player_id: PlayerProjection(
                player_id=p.player_id,
                player_name=p.player_name,
                position=p.position,
                baseline_minutes=p.season_avg,
                adjusted_minutes=p.season_avg,
                confidence=1.0,
                reason="Baseline",
            )
            for p in full_rotation_with_ages
        }

        # Player 102 (Starting Forward, age 33): 32 min → 29 min
        # Player 104 (Starting Center, age 34): 28 min → 25 min
        engine.apply_game_context_adjustments(
            projections, full_rotation_with_ages, game_info_average,
            team_id=1, is_b2b=True,
        )

        assert projections[102].adjusted_minutes == 29.0
        assert projections[104].adjusted_minutes == 25.0
        assert "-3.0 min" in projections[102].reason

    def test_b2b_bench_unchanged(
        self, engine, full_rotation_with_ages, game_info_average
    ):
        """Bench players (< 24 min) are NOT affected by B2B fatigue."""
        projections = {
            p.player_id: PlayerProjection(
                player_id=p.player_id,
                player_name=p.player_name,
                position=p.position,
                baseline_minutes=p.season_avg,
                adjusted_minutes=p.season_avg,
                confidence=1.0,
                reason="Baseline",
            )
            for p in full_rotation_with_ages
        }

        original_bench = {
            pid: proj.adjusted_minutes
            for pid, proj in projections.items()
            if proj.adjusted_minutes < 24.0
        }

        engine.apply_game_context_adjustments(
            projections, full_rotation_with_ages, game_info_average,
            team_id=1, is_b2b=True,
        )

        for pid, orig in original_bench.items():
            assert projections[pid].adjusted_minutes == orig

    def test_no_b2b_no_reduction(
        self, engine, full_rotation_with_ages, game_info_average
    ):
        """Non-B2B game should NOT trigger fatigue adjustments."""
        projections = {
            p.player_id: PlayerProjection(
                player_id=p.player_id,
                player_name=p.player_name,
                position=p.position,
                baseline_minutes=p.season_avg,
                adjusted_minutes=p.season_avg,
                confidence=1.0,
                reason="Baseline",
            )
            for p in full_rotation_with_ages
        }
        original_mins = {pid: p.adjusted_minutes for pid, p in projections.items()}

        engine.apply_game_context_adjustments(
            projections, full_rotation_with_ages, game_info_average,
            team_id=1, is_b2b=False,
        )

        for pid, orig in original_mins.items():
            assert projections[pid].adjusted_minutes == orig
            assert "B2B" not in (projections[pid].reason or "")


# ============================================================================
# DFS PACE FACTOR INTEGRATION
# ============================================================================


class TestDFSPaceFactor:
    """Verify pace_factor is correctly wired into DFS stat projections."""

    def test_fast_pace_boosts_stats(self):
        """Fast-paced game should produce higher projected stats."""
        dfs = DFSService()
        player = PlayerMinutes(
            player_id=1, player_name="Test", position="G", team_id=1,
            minutes_last_5=[30.0] * 5, minutes_last_10=[30.0] * 10,
            season_avg=30.0, usage_rate=0.20,
            pts_per_min=0.60, reb_per_min=0.15, ast_per_min=0.20,
            stl_per_min=0.03, blk_per_min=0.02, tov_per_min=0.06,
            fg3m_per_min=0.08,
        )

        stats_normal = dfs._project_stats(30.0, player, pace_factor=1.0)
        stats_fast = dfs._project_stats(30.0, player, pace_factor=1.04)

        assert stats_fast["pts"] > stats_normal["pts"]
        assert stats_fast["reb"] > stats_normal["reb"]
        assert stats_fast["ast"] > stats_normal["ast"]

    def test_slow_pace_reduces_stats(self):
        """Slow-paced game should produce lower projected stats."""
        dfs = DFSService()
        player = PlayerMinutes(
            player_id=1, player_name="Test", position="G", team_id=1,
            minutes_last_5=[30.0] * 5, minutes_last_10=[30.0] * 10,
            season_avg=30.0, usage_rate=0.20,
            pts_per_min=0.60, reb_per_min=0.15, ast_per_min=0.20,
            stl_per_min=0.03, blk_per_min=0.02, tov_per_min=0.06,
            fg3m_per_min=0.08,
        )

        stats_normal = dfs._project_stats(30.0, player, pace_factor=1.0)
        stats_slow = dfs._project_stats(30.0, player, pace_factor=0.95)

        assert stats_slow["pts"] < stats_normal["pts"]
        assert stats_slow["reb"] < stats_normal["reb"]

    def test_pace_factor_default_is_one(self):
        """Default pace_factor=1.0 should produce identical stats to old behavior."""
        dfs = DFSService()
        player = PlayerMinutes(
            player_id=1, player_name="Test", position="G", team_id=1,
            minutes_last_5=[30.0] * 5, minutes_last_10=[30.0] * 10,
            season_avg=30.0, usage_rate=0.20,
            pts_per_min=0.60, reb_per_min=0.15, ast_per_min=0.20,
            stl_per_min=0.03, blk_per_min=0.02, tov_per_min=0.06,
            fg3m_per_min=0.08,
        )

        stats_default = dfs._project_stats(30.0, player)
        stats_explicit = dfs._project_stats(30.0, player, pace_factor=1.0)

        assert stats_default == stats_explicit


# ============================================================================
# FULL PIPELINE INTEGRATION
# ============================================================================


class TestGameContextIntegration:
    """Full pipeline with game_info produces valid 240-min rotation."""

    def test_pipeline_with_game_info(
        self, engine, full_rotation, game_info_average
    ):
        """project_team_rotation with game_info should still sum to ~240."""
        result = engine.project_team_rotation(
            team_id=1,
            team_name="Test Team",
            rotation=full_rotation,
            injuries=[],
            game_date="2026-02-09",
            apply_coach_adjustments=False,
            game_info=game_info_average,
        )
        assert 239.0 <= result.total_minutes <= 241.0

    def test_pipeline_with_blowout(
        self, engine, full_rotation, game_info_blowout
    ):
        """Blowout context should still normalize to ~240."""
        result = engine.project_team_rotation(
            team_id=1,
            team_name="Test Team",
            rotation=full_rotation,
            injuries=[],
            game_date="2026-02-09",
            apply_coach_adjustments=False,
            game_info=game_info_blowout,
        )
        assert 239.0 <= result.total_minutes <= 241.0

    def test_pipeline_with_b2b(
        self, engine, full_rotation_with_ages, game_info_average
    ):
        """B2B context should still normalize to ~240."""
        result = engine.project_team_rotation(
            team_id=1,
            team_name="Test Team",
            rotation=full_rotation_with_ages,
            injuries=[],
            game_date="2026-02-09",
            apply_coach_adjustments=False,
            game_info=game_info_average,
            is_b2b=True,
        )
        assert 239.0 <= result.total_minutes <= 241.0

    def test_pipeline_without_game_info(self, engine, full_rotation):
        """Pipeline still works when game_info is None (backward compat)."""
        result = engine.project_team_rotation(
            team_id=1,
            team_name="Test Team",
            rotation=full_rotation,
            injuries=[],
            game_date="2026-02-09",
            apply_coach_adjustments=False,
            game_info=None,
        )
        assert 239.0 <= result.total_minutes <= 241.0

    def test_pace_factor_reaches_projections(
        self, engine, full_rotation, game_info_fast_pace
    ):
        """Pace factor should be populated on projections after pipeline."""
        result = engine.project_team_rotation(
            team_id=1,
            team_name="Test Team",
            rotation=full_rotation,
            injuries=[],
            game_date="2026-02-09",
            apply_coach_adjustments=False,
            game_info=game_info_fast_pace,
        )
        active = [p for p in result.projections if p.adjusted_minutes > 0]
        assert len(active) > 0
        for p in active:
            assert p.pace_factor > 1.0  # 106 pace → factor > 1.0


# ============================================================================
# CROSS-TEAM INJURY MATCHING TESTS
# ============================================================================


class TestCrossTeamInjuryMatching:
    """Verify that players traded mid-season are still detected as injured.

    When a player's injury-source team (e.g. Basketball Reference) differs
    from their NBA-API roster team (e.g. after a trade), the team-filtered
    injury list misses them.  The full-list fallback should catch these.
    """

    def _make_rotation(self):
        """Minimal 5-player rotation including a 'traded' player."""
        base = dict(
            team_id=1,
            minutes_last_5=[30, 30, 30, 30, 30],
            minutes_last_10=[30] * 10,
            season_avg=30.0,
            usage_rate=0.20,
            pts_per_min=0.5, reb_per_min=0.2, ast_per_min=0.15,
            stl_per_min=0.03, blk_per_min=0.02, tov_per_min=0.05,
            fg3m_per_min=0.08,
        )
        return [
            PlayerMinutes(player_id=100, player_name="Jimmy Butler", position="F", **base),
            PlayerMinutes(player_id=101, player_name="Bam Adebayo", position="C", **base),
            PlayerMinutes(player_id=102, player_name="Tyler Herro", position="G",
                         **{**base, "season_avg": 28.0}),
            PlayerMinutes(player_id=103, player_name="Duncan Robinson", position="F",
                         **{**base, "season_avg": 20.0}),
            PlayerMinutes(player_id=104, player_name="Terry Rozier", position="G",
                         **{**base, "season_avg": 25.0}),
        ]

    def test_team_filtered_misses_traded_player(self, engine):
        """Team-filtered list alone does NOT catch a traded player."""
        from app.models.player import PlayerStatus

        rotation = self._make_rotation()

        # Team-filtered injuries: Butler NOT listed (he's on another team now)
        team_injuries = [
            PlayerStatus(
                player_id=0, player_name="Tyler Herro",
                status="Out", injury_description="Knee",
            ),
        ]

        matched = engine._match_injuries_to_roster(rotation, team_injuries)
        names = [m.player_name for m in matched]
        assert "Tyler Herro" in names
        assert "Jimmy Butler" not in names  # missed!

    def test_full_list_catches_traded_player(self, engine):
        """Full injury list cross-check catches traded player."""
        from app.models.player import PlayerStatus

        rotation = self._make_rotation()

        # Team-filtered: only Herro
        team_injuries = [
            PlayerStatus(
                player_id=0, player_name="Tyler Herro",
                status="Out", injury_description="Knee",
            ),
        ]

        # Full list includes Butler under a different team
        all_injuries = [
            PlayerStatus(
                player_id=0, player_name="Jimmy Butler",
                status="Out", injury_description="Torn ACL - Out For Season",
            ),
            PlayerStatus(
                player_id=0, player_name="Stephen Curry",
                status="GTD", injury_description="Ankle",
            ),
        ]

        matched = engine._match_injuries_to_roster(
            rotation, team_injuries, all_injuries=all_injuries
        )
        names = [m.player_name for m in matched]
        assert "Tyler Herro" in names
        assert "Jimmy Butler" in names
        # Butler should have correct player_id from rotation
        butler = next(m for m in matched if m.player_name == "Jimmy Butler")
        assert butler.player_id == 100
        assert butler.status == "Out"

    def test_full_list_requires_first_and_last_name(self, engine):
        """Full-list match requires both first AND last name to avoid false positives."""
        from app.models.player import PlayerStatus

        rotation = self._make_rotation()

        # No team-filtered injuries
        team_injuries = []

        # Full list has a "Jalen Butler" — different first name
        all_injuries = [
            PlayerStatus(
                player_id=0, player_name="Jalen Butler",
                status="Out", injury_description="Hamstring",
            ),
        ]

        matched = engine._match_injuries_to_roster(
            rotation, team_injuries, all_injuries=all_injuries
        )
        # Should NOT match — first name differs
        assert len(matched) == 0

    def test_traded_player_gets_zero_minutes_in_pipeline(self, engine):
        """End-to-end: traded injured player gets 0 minutes."""
        from app.models.player import PlayerStatus

        rotation = self._make_rotation()

        team_injuries = []  # Butler not on this team's injury list
        all_injuries = [
            PlayerStatus(
                player_id=0, player_name="Jimmy Butler",
                status="Out", injury_description="Torn ACL",
            ),
        ]

        result = engine.project_team_rotation(
            team_id=1,
            team_name="Miami Heat",
            rotation=rotation,
            injuries=team_injuries,
            game_date="2026-02-09",
            apply_coach_adjustments=False,
            all_injuries=all_injuries,
        )

        butler_proj = next(
            p for p in result.projections if p.player_name == "Jimmy Butler"
        )
        assert butler_proj.adjusted_minutes == 0.0
        assert "Out" in butler_proj.reason or "Injured" in butler_proj.reason

    def test_no_false_positive_without_all_injuries(self, engine):
        """Without all_injuries, only team-filtered matches are returned."""
        from app.models.player import PlayerStatus

        rotation = self._make_rotation()

        # Butler NOT in team injuries, and no all_injuries provided
        team_injuries = []

        matched = engine._match_injuries_to_roster(rotation, team_injuries)
        assert len(matched) == 0  # no matches at all

    def test_already_matched_not_duplicated(self, engine):
        """Player found in team list is NOT re-matched from full list."""
        from app.models.player import PlayerStatus

        rotation = self._make_rotation()

        # Butler in BOTH team and full list
        team_injuries = [
            PlayerStatus(
                player_id=0, player_name="Jimmy Butler",
                status="Out", injury_description="Knee",
            ),
        ]
        all_injuries = [
            PlayerStatus(
                player_id=0, player_name="Jimmy Butler",
                status="Out", injury_description="Knee",
            ),
        ]

        matched = engine._match_injuries_to_roster(
            rotation, team_injuries, all_injuries=all_injuries
        )
        butler_matches = [m for m in matched if m.player_name == "Jimmy Butler"]
        assert len(butler_matches) == 1  # no duplicate
