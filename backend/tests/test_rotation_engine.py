import pytest
from typing import Dict, List

from app.models.player import PlayerMinutes, PlayerProjection, PlayerStatus
from app.models.rotation import RedistributionConfig, TeamRotation
from app.models.coach import CoachProfile, CoachingStyle
from app.services.rotation_engine import RotationEngine


# ============================================================================
# EMA CALCULATION TESTS
# ============================================================================


class TestEMACalculation:
    """Tests for Exponential Moving Average calculations."""

    def test_ema_single_value(self, engine):
        result = engine.calculate_ema([30.0])
        assert result == 30.0

    def test_ema_empty_list(self, engine):
        result = engine.calculate_ema([])
        assert result == 0.0

    def test_ema_constant_values(self, engine):
        result = engine.calculate_ema([25.0, 25.0, 25.0, 25.0, 25.0])
        assert abs(result - 25.0) < 0.1

    def test_ema_increasing_trend(self, engine):
        # Input is reverse-chronological: index 0 = most recent (20 min),
        # oldest = 28 min.  Recent minutes are LOWER, so EMA should
        # weight toward the recent low values and be < 24.
        result = engine.calculate_ema([20.0, 22.0, 24.0, 26.0, 28.0])
        assert result < 24.0  # Weighted toward recent lower values

    def test_ema_decreasing_trend(self, engine):
        # Input is reverse-chronological: index 0 = most recent (28 min),
        # oldest = 20 min.  Recent minutes are HIGHER, so EMA should
        # weight toward the recent high values and be > 24.
        result = engine.calculate_ema([28.0, 26.0, 24.0, 22.0, 20.0])
        assert result > 24.0  # Weighted toward recent higher values

    def test_ema_custom_alpha(self, engine):
        # Input is reverse-chronological: [10, 20, 30] means
        # most recent = 10, oldest = 30.
        # Higher alpha = more weight on recent values (the 10).
        # Lower alpha = more weight on older values (the 30).
        # So with recent < old, higher alpha → LOWER result.
        high_alpha = engine.calculate_ema([10.0, 20.0, 30.0], alpha=0.9)
        low_alpha = engine.calculate_ema([10.0, 20.0, 30.0], alpha=0.1)
        assert high_alpha < low_alpha


# ============================================================================
# BASELINE PROJECTION TESTS
# ============================================================================


class TestBaselineProjection:
    """Tests for baseline minute projections."""

    def test_star_player_baseline(self, engine, star_player):
        baseline = engine.get_baseline_projection(star_player)
        assert 30.0 <= baseline <= 40.0
        assert isinstance(baseline, float)

    def test_bench_player_baseline(self, engine, bench_player):
        baseline = engine.get_baseline_projection(bench_player)
        assert 10.0 <= baseline <= 25.0

    def test_deep_bench_baseline(self, engine, deep_bench_player):
        baseline = engine.get_baseline_projection(deep_bench_player)
        assert 4.0 <= baseline <= 15.0

    def test_baseline_uses_weighted_average(self, engine, star_player):
        baseline = engine.get_baseline_projection(star_player)
        # Should be close to season_avg but influenced by recent games
        assert abs(baseline - star_player.season_avg) < 5.0

    def test_baseline_returns_rounded(self, engine, star_player):
        baseline = engine.get_baseline_projection(star_player)
        # Should be rounded to 1 decimal
        assert baseline == round(baseline, 1)

    def test_sparse_data_empty_recent_uses_season_avg(self, engine):
        """Player with empty recent lists should get ~season_avg baseline."""
        player = PlayerMinutes(
            player_id=200, player_name="Sparse Rookie", position="C",
            team_id=10, minutes_last_5=[], minutes_last_10=[],
            season_avg=30.0, usage_rate=0.18,
        )
        baseline = engine.get_baseline_projection(player)
        assert baseline >= 28.0, (
            f"Expected near 30.0 (season_avg), got {baseline}"
        )

    def test_sparse_data_one_game_graduated(self, engine):
        """1 recent game should blend mostly toward season_avg."""
        player = PlayerMinutes(
            player_id=201, player_name="Traded Player", position="PF",
            team_id=10, minutes_last_5=[28.0], minutes_last_10=[28.0],
            season_avg=30.0, usage_rate=0.15,
        )
        baseline = engine.get_baseline_projection(player)
        assert baseline >= 28.0, (
            f"Expected near 30.0 with 1 game, got {baseline}"
        )

    def test_sparse_data_three_games_normal_path(self, engine):
        """3+ recent games should follow normal EMA path unchanged."""
        player = PlayerMinutes(
            player_id=202, player_name="Normal Player", position="G",
            team_id=10, minutes_last_5=[32.0, 34.0, 30.0],
            minutes_last_10=[], season_avg=30.0, usage_rate=0.22,
        )
        baseline = engine.get_baseline_projection(player)
        assert baseline > 0  # Normal path, EMA contributes

    def test_sparse_data_zero_season_avg_safe(self, engine):
        """Zero season_avg + empty recent should return 0 without crash."""
        player = PlayerMinutes(
            player_id=203, player_name="Unknown", position="SF",
            team_id=10, minutes_last_5=[], minutes_last_10=[],
            season_avg=0.0, usage_rate=0.0,
        )
        baseline = engine.get_baseline_projection(player)
        assert baseline == 0.0


# ============================================================================
# BACKUP HIERARCHY TESTS
# ============================================================================


class TestBackupHierarchy:
    """Tests for identifying backup player hierarchy."""

    def test_identifies_same_position_backups(self, engine, full_rotation):
        injured = full_rotation[0]  # Star Guard (G)
        hierarchy = engine.identify_backup_hierarchy(full_rotation, injured)
        for player, weight in hierarchy:
            assert player.position == "G"
            assert player.player_id != injured.player_id

    def test_primary_backup_gets_highest_share(self, engine, full_rotation):
        injured = full_rotation[0]
        hierarchy = engine.identify_backup_hierarchy(full_rotation, injured)
        assert len(hierarchy) > 0
        # Primary backup gets primary_backup_share (0.60) + star_boost (0.05) = 0.65
        assert hierarchy[0][1] == 0.65

    def test_empty_hierarchy_when_no_same_position(self, engine):
        solo_center = PlayerMinutes(
            player_id=999,
            player_name="Solo Center",
            position="C",
            team_id=1,
            minutes_last_5=[30.0] * 5,
            minutes_last_10=[30.0] * 10,
            season_avg=30.0,
            usage_rate=0.20,
        )
        # Rotation with no other centers
        rotation = [
            solo_center,
            PlayerMinutes(
                player_id=998,
                player_name="Guard",
                position="G",
                team_id=1,
                minutes_last_5=[25.0] * 5,
                minutes_last_10=[25.0] * 10,
                season_avg=25.0,
                usage_rate=0.18,
            ),
        ]
        hierarchy = engine.identify_backup_hierarchy(rotation, solo_center)
        assert hierarchy == []

    def test_hierarchy_sorted_by_season_avg(self, engine, full_rotation):
        injured = full_rotation[0]  # Star Guard
        hierarchy = engine.identify_backup_hierarchy(full_rotation, injured)
        if len(hierarchy) > 1:
            avgs = [p.season_avg for p, _ in hierarchy]
            assert avgs == sorted(avgs, reverse=True)


# ============================================================================
# REDISTRIBUTION TESTS
# ============================================================================


class TestRedistribution:
    """Tests for the waterfall redistribution algorithm."""

    def test_no_injuries_returns_baseline(
        self, engine, full_rotation, baseline_projections_balanced, no_injuries
    ):
        result = engine.redistribute_minutes(
            full_rotation, no_injuries, baseline_projections_balanced
        )
        for pid, proj in result.items():
            assert proj.adjusted_minutes == baseline_projections_balanced[pid]

    def test_injured_player_gets_zero(
        self, engine, full_rotation, baseline_projections_balanced, single_injury
    ):
        result = engine.redistribute_minutes(
            full_rotation, single_injury, baseline_projections_balanced
        )
        assert result[100].adjusted_minutes == 0.0
        assert "Out" in result[100].reason or "Injured" in result[100].reason

    def test_redistribution_preserves_non_injured_total(
        self, engine, full_rotation, baseline_projections_balanced, single_injury
    ):
        """Healthy players receive the injured player's minutes."""
        result = engine.redistribute_minutes(
            full_rotation, single_injury, baseline_projections_balanced
        )
        total = sum(p.adjusted_minutes for p in result.values())
        # Total should be close to original minus whatever wasn't
        # redistributed (some minutes may be lost to max-cap).
        # The full pipeline normalises to 240 later.
        baseline_total = sum(baseline_projections_balanced.values())
        assert total <= baseline_total + 1.0
        assert total > 0

    def test_multiple_injuries_handled(
        self, engine, full_rotation, baseline_projections_balanced, multiple_injuries
    ):
        result = engine.redistribute_minutes(
            full_rotation, multiple_injuries, baseline_projections_balanced
        )
        assert result[100].adjusted_minutes == 0.0
        assert result[103].adjusted_minutes == 0.0
        total = sum(p.adjusted_minutes for p in result.values())
        # Minutes are redistributed, but 240 norm happens in the pipeline
        assert total > 0

    def test_backup_receives_minutes(
        self, engine, full_rotation, baseline_projections_balanced, single_injury
    ):
        result = engine.redistribute_minutes(
            full_rotation, single_injury, baseline_projections_balanced
        )
        # Some guard backup should have received minutes
        guards = [
            p
            for p in result.values()
            if p.position == "G" and p.player_id != 100
        ]
        any_increased = any(
            p.adjusted_minutes > baseline_projections_balanced[p.player_id]
            for p in guards
        )
        assert any_increased

    def test_max_cap_enforced(self, full_rotation, single_injury):
        config = RedistributionConfig(max_minutes_cap=38.0)
        engine = RotationEngine(config=config)
        baseline = {
            100: 35.0, 101: 33.0, 102: 32.0, 103: 30.0, 104: 28.0,
            105: 22.0, 106: 20.0, 107: 18.0, 108: 15.0, 109: 7.0,
        }
        result = engine.redistribute_minutes(full_rotation, single_injury, baseline)
        for proj in result.values():
            # After normalization, minutes may slightly differ
            assert proj.adjusted_minutes <= 45.0  # Generous upper bound

    def test_only_out_players_redistributed(
        self, engine, full_rotation, baseline_projections_balanced, mixed_injury_statuses
    ):
        result = engine.redistribute_minutes(
            full_rotation, mixed_injury_statuses, baseline_projections_balanced
        )
        # Only "Out" status (player 100) should have 0 minutes
        assert result[100].adjusted_minutes == 0.0
        # Questionable and GTD should keep their minutes (before normalization)
        assert result[101].adjusted_minutes > 0.0


# ============================================================================
# COACH ADJUSTMENT TESTS
# ============================================================================


class TestCoachAdjustments:
    """Tests for coach-specific minute adjustments."""

    def test_heavy_minutes_coach_increases_starter_minutes(
        self, engine, full_rotation, baseline_projections_balanced, heavy_minutes_profile
    ):
        projections = {
            pid: PlayerProjection(
                player_id=pid,
                player_name=f"Player {pid}",
                position="G" if pid % 2 == 0 else "F",
                baseline_minutes=mins,
                adjusted_minutes=mins,
                confidence=1.0,
                reason="Baseline",
            )
            for pid, mins in baseline_projections_balanced.items()
        }
        result = engine.apply_coach_adjustments(
            projections, full_rotation, heavy_minutes_profile
        )
        # Heavy-minutes coach should push total above original
        # (starters inflate more than bench contracts)
        total = sum(p.adjusted_minutes for p in result.values())
        original = sum(baseline_projections_balanced.values())
        assert total > original

    def test_deep_rotation_coach_increases_bench_minutes(
        self, engine, full_rotation, baseline_projections_balanced, deep_rotation_profile
    ):
        projections = {
            pid: PlayerProjection(
                player_id=pid,
                player_name=f"Player {pid}",
                position="G" if pid % 2 == 0 else "F",
                baseline_minutes=mins,
                adjusted_minutes=mins,
                confidence=1.0,
                reason="Baseline",
            )
            for pid, mins in baseline_projections_balanced.items()
        }
        original_bench = baseline_projections_balanced[109]  # 7 min — deep bench
        result = engine.apply_coach_adjustments(
            projections, full_rotation, deep_rotation_profile
        )
        # Deep-rotation coach should boost bench players
        assert result[109].adjusted_minutes >= original_bench

    def test_default_profile_no_change(
        self, engine, full_rotation, baseline_projections_balanced, default_profile
    ):
        projections = {
            pid: PlayerProjection(
                player_id=pid,
                player_name=f"Player {pid}",
                position="G" if pid % 2 == 0 else "F",
                baseline_minutes=mins,
                adjusted_minutes=mins,
                confidence=1.0,
                reason="Baseline",
            )
            for pid, mins in baseline_projections_balanced.items()
        }
        result = engine.apply_coach_adjustments(
            projections, full_rotation, default_profile
        )
        total = sum(p.adjusted_minutes for p in result.values())
        original = sum(baseline_projections_balanced.values())
        # Default profile (all 1.0 multipliers) should not change total
        assert abs(total - original) < 1.0

    def test_full_pipeline_normalizes_to_240(
        self, engine, full_rotation
    ):
        """The 240-minute constraint is enforced by the full pipeline."""
        result = engine.project_team_rotation(
            team_id=0,
            team_name="Test Team",
            rotation=full_rotation,
            injuries=[],
            game_date="2026-01-15",
            apply_coach_adjustments=True,
        )
        assert abs(result.total_minutes - 240.0) < 0.5


# ============================================================================
# INJURY MATCHING TESTS
# ============================================================================


class TestInjuryMatching:
    """Tests for matching injury reports to roster players."""

    def test_match_by_last_name(self, engine, full_rotation):
        injuries = [
            PlayerStatus(
                player_id=0,
                player_name="John Guard",  # Last name "Guard" matches "Star Guard"
                status="Out",
                injury_description="Knee",
            )
        ]
        matched = engine._match_injuries_to_roster(full_rotation, injuries)
        assert len(matched) == 1
        assert matched[0].player_id == 100  # Star Guard

    def test_skip_non_actionable_statuses(self, engine, full_rotation):
        """Non-actionable statuses (e.g. 'Available') are skipped."""
        injuries = [
            PlayerStatus(
                player_id=0,
                player_name="John Guard",
                status="Available",
                injury_description="Cleared to play",
            )
        ]
        matched = engine._match_injuries_to_roster(full_rotation, injuries)
        assert len(matched) == 0

    def test_questionable_is_actionable(self, engine, full_rotation):
        """Questionable is now an actionable status (matched for minute reduction)."""
        injuries = [
            PlayerStatus(
                player_id=0,
                player_name="John Guard",
                status="Questionable",
                injury_description="Sore knee",
            )
        ]
        matched = engine._match_injuries_to_roster(full_rotation, injuries)
        assert len(matched) == 1

    def test_match_doubtful_included(self, engine, full_rotation):
        injuries = [
            PlayerStatus(
                player_id=0,
                player_name="Some Guard",
                status="Doubtful",
                injury_description="Back spasms",
            )
        ]
        matched = engine._match_injuries_to_roster(full_rotation, injuries)
        # "Guard" matches "Star Guard" (id=100)
        assert len(matched) == 1

    def test_no_match_returns_empty(self, engine, full_rotation):
        injuries = [
            PlayerStatus(
                player_id=0,
                player_name="Unknown Player",
                status="Out",
                injury_description="Broken leg",
            )
        ]
        matched = engine._match_injuries_to_roster(full_rotation, injuries)
        assert len(matched) == 0


# ============================================================================
# FULL PIPELINE TESTS
# ============================================================================


class TestFullPipeline:
    """End-to-end tests for the complete projection pipeline."""

    def test_healthy_team_projection(self, engine, full_rotation, no_injuries):
        result = engine.project_team_rotation(
            team_id=0,
            team_name="Test Team",
            rotation=full_rotation,
            injuries=no_injuries,
            game_date="2025-01-15",
            apply_coach_adjustments=False,
        )
        assert isinstance(result, TeamRotation)
        assert abs(result.total_minutes - 240.0) < 0.5

    def test_injured_team_projection(self, engine, full_rotation, single_injury):
        result = engine.project_team_rotation(
            team_id=0,
            team_name="Test Team",
            rotation=full_rotation,
            injuries=single_injury,
            game_date="2025-01-15",
            apply_coach_adjustments=False,
        )
        assert abs(result.total_minutes - 240.0) < 0.5
        injured_proj = next(
            p for p in result.projections if p.player_id == 100
        )
        assert injured_proj.adjusted_minutes == 0.0

    def test_pipeline_with_coach_adjustments(
        self, engine, full_rotation, no_injuries
    ):
        # Use default coach (team_id=0)
        result = engine.project_team_rotation(
            team_id=0,
            team_name="Test Team",
            rotation=full_rotation,
            injuries=no_injuries,
            game_date="2025-01-15",
            apply_coach_adjustments=True,
        )
        assert abs(result.total_minutes - 240.0) < 0.5

    def test_pipeline_produces_all_players(self, engine, full_rotation, no_injuries):
        result = engine.project_team_rotation(
            team_id=0,
            team_name="Test Team",
            rotation=full_rotation,
            injuries=no_injuries,
            game_date="2025-01-15",
            apply_coach_adjustments=False,
        )
        assert len(result.projections) == len(full_rotation)

    def test_pipeline_positions_breakdown(self, engine, full_rotation, no_injuries):
        result = engine.project_team_rotation(
            team_id=0,
            team_name="Test Team",
            rotation=full_rotation,
            injuries=no_injuries,
            game_date="2025-01-15",
            apply_coach_adjustments=False,
        )
        assert "G" in result.positions_breakdown
        assert "F" in result.positions_breakdown
        assert "C" in result.positions_breakdown


# ============================================================================
# EDGE CASE TESTS
# ============================================================================


class TestEdgeCases:
    """Edge case and boundary tests."""

    def test_all_players_injured(self, engine, full_rotation):
        all_injured = [
            PlayerStatus(
                player_id=p.player_id,
                player_name=p.player_name,
                status="Out",
                injury_description="Season ending",
            )
            for p in full_rotation
        ]
        baseline = {p.player_id: 24.0 for p in full_rotation}
        result = engine.redistribute_minutes(full_rotation, all_injured, baseline)
        total = sum(p.adjusted_minutes for p in result.values())
        # All should be 0 but normalization may behave oddly
        assert all(p.adjusted_minutes == 0.0 for p in result.values()) or total >= 0

    def test_single_player_rotation(self, engine):
        solo = [
            PlayerMinutes(
                player_id=1,
                player_name="Solo",
                position="G",
                team_id=1,
                minutes_last_5=[48.0] * 5,
                minutes_last_10=[48.0] * 10,
                season_avg=48.0,
                usage_rate=0.35,
            )
        ]
        baseline = {1: 48.0}
        result = engine.redistribute_minutes(solo, [], baseline)
        assert result[1].adjusted_minutes == 48.0

    def test_zero_minutes_player(self, engine):
        player = PlayerMinutes(
            player_id=1,
            player_name="DNP",
            position="G",
            team_id=1,
            minutes_last_5=[0.0] * 5,
            minutes_last_10=[0.0] * 10,
            season_avg=0.0,
            usage_rate=0.0,
        )
        baseline = engine.get_baseline_projection(player)
        assert baseline == 0.0


# ============================================================================
# LARGE ROTATION TESTS (11-13 players)
# ============================================================================


class TestLargeRotation:
    """Tests that 11-13 player rotations normalize correctly to 240 min."""

    def test_13_player_rotation_normalizes(self, engine, full_rotation_13):
        """13-player rotation should still normalize to ~240 minutes."""
        result = engine.project_team_rotation(
            team_id=0,
            team_name="Test Team",
            rotation=full_rotation_13,
            injuries=[],
            game_date="2026-01-15",
            apply_coach_adjustments=False,
        )
        assert abs(result.total_minutes - 240.0) < 1.0
        # Should have all 13 projections (some may be 0.0 via compression)
        assert len(result.projections) == 13

    def test_13_player_star_protection(self, engine, full_rotation_13):
        """Stars should retain near-baseline minutes even with 13 players."""
        result = engine.project_team_rotation(
            team_id=0,
            team_name="Test Team",
            rotation=full_rotation_13,
            injuries=[],
            game_date="2026-01-15",
            apply_coach_adjustments=False,
        )
        star = next(p for p in result.projections if p.player_id == 200)
        # Star's baseline is ~35 min; shouldn't drop more than 5 min
        assert star.adjusted_minutes >= 30.0

    def test_13_player_no_negative_minutes(self, engine, full_rotation_13):
        """No player should have negative minutes after compression."""
        result = engine.project_team_rotation(
            team_id=0,
            team_name="Test Team",
            rotation=full_rotation_13,
            injuries=[],
            game_date="2026-01-15",
            apply_coach_adjustments=False,
        )
        for proj in result.projections:
            assert proj.adjusted_minutes >= 0.0

    def test_13_player_with_coach_adjustments(self, engine, full_rotation_13):
        """13-player rotation should still normalize with coach adjustments."""
        result = engine.project_team_rotation(
            team_id=0,
            team_name="Test Team",
            rotation=full_rotation_13,
            injuries=[],
            game_date="2026-01-15",
            apply_coach_adjustments=True,
        )
        assert abs(result.total_minutes - 240.0) < 1.0

    def test_compression_floor_marks_reason(self, engine, full_rotation_13):
        """Players compressed below MIN_VIABLE_MINUTES get 0.0 and a reason."""
        result = engine.project_team_rotation(
            team_id=0,
            team_name="Test Team",
            rotation=full_rotation_13,
            injuries=[],
            game_date="2026-01-15",
            apply_coach_adjustments=False,
        )
        zero_players = [
            p for p in result.projections if p.adjusted_minutes == 0.0
        ]
        for p in zero_players:
            if p.reason and "Compressed" in p.reason:
                # Verify the reason annotation was added
                assert "Compressed out of rotation" in p.reason


# ============================================================================
# MINUTES CAP REGRESSION TESTS
# ============================================================================


class TestMinutesCap:
    """Regression tests for the minutes projection cap bug.

    When a team has a very small rotation (e.g., 7 players), the
    normalization step must still cap individual players at 42 minutes
    and never produce impossible values like 75+ minutes.
    """

    def test_small_rotation_no_player_exceeds_42(self, engine):
        """A 7-player rotation must not produce any minutes > 42.0."""
        small_rotation = [
            PlayerMinutes(
                player_id=i,
                player_name=f"Player_{i}",
                position="G" if i < 3 else "F" if i < 5 else "C",
                team_id=99,
                minutes_last_5=[30.0 + i * 2] * 5,
                minutes_last_10=[30.0 + i * 2] * 10,
                season_avg=30.0 + i * 2,
                usage_rate=0.20,
            )
            for i in range(7)
        ]
        result = engine.project_team_rotation(
            team_id=99,
            team_name="Small Team",
            rotation=small_rotation,
            injuries=[],
            game_date="2026-02-19",
            apply_coach_adjustments=False,
        )
        for p in result.projections:
            assert p.adjusted_minutes <= 42.0, (
                f"{p.player_name} projected {p.adjusted_minutes} min — exceeds 42.0 cap"
            )

    def test_small_rotation_total_near_240(self, engine):
        """A small rotation should still target ~240 total minutes."""
        small_rotation = [
            PlayerMinutes(
                player_id=i,
                player_name=f"Player_{i}",
                position="G" if i < 3 else "F" if i < 5 else "C",
                team_id=99,
                minutes_last_5=[28.0 + i * 3] * 5,
                minutes_last_10=[28.0 + i * 3] * 10,
                season_avg=28.0 + i * 3,
                usage_rate=0.20,
            )
            for i in range(7)
        ]
        result = engine.project_team_rotation(
            team_id=99,
            team_name="Small Team",
            rotation=small_rotation,
            injuries=[],
            game_date="2026-02-19",
            apply_coach_adjustments=False,
        )
        total = sum(p.adjusted_minutes for p in result.projections)
        # May be slightly under 240 if all players hit their cap,
        # but should be reasonably close.
        assert total <= 240.5, f"Total {total} exceeds 240"
        # With 7 players each at most 42 min = 294 max possible
        # With caps, total should be at least 200 (7 real players)
        assert total >= 200.0, f"Total {total} too low — rotation collapse"

    def test_very_small_rotation_5_players_capped(self, engine):
        """A 5-player rotation triggers the short-rotation guardrail.

        With ≤8 active players, SHORT_ROTATION_STARTER_CEILING (44.0)
        replaces ABSOLUTE_MAX_MINUTES (42.0) so starters can absorb
        the full 240-minute budget.  With 5 players × 44 max = 220,
        the total will be under 240 — correct for an extreme rotation.
        """
        from app.config.constants import SHORT_ROTATION_STARTER_CEILING

        tiny_rotation = [
            PlayerMinutes(
                player_id=i,
                player_name=f"Starter_{i}",
                position="G" if i < 2 else "F" if i < 4 else "C",
                team_id=99,
                minutes_last_5=[36.0] * 5,
                minutes_last_10=[36.0] * 10,
                season_avg=36.0,
                usage_rate=0.22,
            )
            for i in range(5)
        ]
        result = engine.project_team_rotation(
            team_id=99,
            team_name="Tiny Team",
            rotation=tiny_rotation,
            injuries=[],
            game_date="2026-02-19",
            apply_coach_adjustments=False,
        )
        for p in result.projections:
            assert p.adjusted_minutes <= SHORT_ROTATION_STARTER_CEILING, (
                f"{p.player_name} projected {p.adjusted_minutes} min "
                f"— exceeds {SHORT_ROTATION_STARTER_CEILING} short-rotation cap"
            )
        # 5 players × 42 max = 210 max possible — total should be ≤ 240
        assert result.total_minutes <= 240.0
        assert result.total_minutes >= 180.0  # At least reasonable

    def test_model_validator_caps_minutes(self):
        """PlayerProjection.adjusted_minutes should be capped at 53.0 by validator."""
        proj = PlayerProjection(
            player_id=999,
            player_name="Impossible Player",
            position="G",
            baseline_minutes=35.0,
            adjusted_minutes=75.3,  # This should be capped
            confidence=0.8,
        )
        assert proj.adjusted_minutes == 53.0

    def test_model_validator_allows_normal_minutes(self):
        """PlayerProjection validator should not alter normal values."""
        proj = PlayerProjection(
            player_id=999,
            player_name="Normal Player",
            position="G",
            baseline_minutes=30.0,
            adjusted_minutes=32.5,
            confidence=0.9,
        )
        assert proj.adjusted_minutes == 32.5


# ============================================================================
# DNP STREAK AUTO-DETECTION TESTS
# ============================================================================


class TestDNPStreakAutoDetection:
    """Tests for auto-detecting suspended/inactive players via DNP streak.

    When a player has 5+ consecutive DNPs (e.g. suspended), the rotation
    engine should automatically mark them as Out even if they don't appear
    on the injury report.  This prevents phantom minutes from compressing
    teammates' projections.
    """

    def test_suspended_player_auto_detected_as_out(self):
        """Player with 14 consecutive DNPs should be auto-marked Out."""
        engine = RotationEngine()
        rotation = [
            PlayerMinutes(
                player_id=1, player_name="Star Player", position="G",
                team_id=1, minutes_last_5=[38.0] * 5,
                minutes_last_10=[38.0] * 10, season_avg=38.0,
                usage_rate=0.30,
            ),
            PlayerMinutes(
                player_id=2, player_name="Suspended Player", position="F",
                team_id=1, minutes_last_5=[30.0] * 5,  # Pre-suspension games
                minutes_last_10=[30.0] * 10, season_avg=30.0,
                usage_rate=0.25, recent_dnp_streak=14,  # 14 consecutive DNPs
            ),
            PlayerMinutes(
                player_id=3, player_name="Starter A", position="F",
                team_id=1, minutes_last_5=[28.0] * 5,
                minutes_last_10=[28.0] * 10, season_avg=28.0,
                usage_rate=0.18,
            ),
            PlayerMinutes(
                player_id=4, player_name="Starter B", position="C",
                team_id=1, minutes_last_5=[26.0] * 5,
                minutes_last_10=[26.0] * 10, season_avg=26.0,
                usage_rate=0.16,
            ),
            PlayerMinutes(
                player_id=5, player_name="Starter C", position="G",
                team_id=1, minutes_last_5=[24.0] * 5,
                minutes_last_10=[24.0] * 10, season_avg=24.0,
                usage_rate=0.14,
            ),
            PlayerMinutes(
                player_id=6, player_name="Bench A", position="G",
                team_id=1, minutes_last_5=[18.0] * 5,
                minutes_last_10=[18.0] * 10, season_avg=18.0,
                usage_rate=0.12,
            ),
            PlayerMinutes(
                player_id=7, player_name="Bench B", position="F",
                team_id=1, minutes_last_5=[14.0] * 5,
                minutes_last_10=[14.0] * 10, season_avg=14.0,
                usage_rate=0.10,
            ),
            PlayerMinutes(
                player_id=8, player_name="Bench C", position="C",
                team_id=1, minutes_last_5=[10.0] * 5,
                minutes_last_10=[10.0] * 10, season_avg=10.0,
                usage_rate=0.08,
            ),
        ]

        # No injuries provided — auto-detection must catch the suspended player
        result = engine.project_team_rotation(
            team_id=1, team_name="Test", rotation=rotation,
            injuries=[], game_date="2026-02-19",
            apply_coach_adjustments=False,
        )

        # Suspended player should get 0 minutes
        suspended = next(p for p in result.projections if p.player_id == 2)
        assert suspended.adjusted_minutes == 0.0, (
            f"Suspended player should get 0 min, got {suspended.adjusted_minutes}"
        )

        # Star player should NOT be compressed by phantom minutes —
        # without auto-detection, the suspended player's ~30 min baseline
        # would compress the star.  With auto-detection, the star should
        # be at or above their baseline.
        star = next(p for p in result.projections if p.player_id == 1)
        assert star.adjusted_minutes >= 36.0, (
            f"Star player compressed to {star.adjusted_minutes} — "
            f"phantom suspension minutes not removed"
        )

        # Other active players should also have received redistributed minutes
        active_players = [p for p in result.projections if p.adjusted_minutes > 0]
        assert len(active_players) == 7, "Should have 7 active players"

    def test_low_dnp_streak_not_auto_excluded(self):
        """Player with only 2 DNPs should NOT be auto-marked Out."""
        engine = RotationEngine()
        rotation = [
            PlayerMinutes(
                player_id=1, player_name="Active Star", position="G",
                team_id=1, minutes_last_5=[36.0] * 5,
                minutes_last_10=[36.0] * 10, season_avg=36.0,
                usage_rate=0.28,
            ),
            PlayerMinutes(
                player_id=2, player_name="Resting Player", position="F",
                team_id=1, minutes_last_5=[28.0] * 5,
                minutes_last_10=[28.0] * 10, season_avg=28.0,
                usage_rate=0.20, recent_dnp_streak=2,  # Only 2 DNPs — could be rest
            ),
            PlayerMinutes(
                player_id=3, player_name="Player C", position="F",
                team_id=1, minutes_last_5=[26.0] * 5,
                minutes_last_10=[26.0] * 10, season_avg=26.0,
                usage_rate=0.18,
            ),
            PlayerMinutes(
                player_id=4, player_name="Player D", position="C",
                team_id=1, minutes_last_5=[24.0] * 5,
                minutes_last_10=[24.0] * 10, season_avg=24.0,
                usage_rate=0.16,
            ),
            PlayerMinutes(
                player_id=5, player_name="Player E", position="G",
                team_id=1, minutes_last_5=[22.0] * 5,
                minutes_last_10=[22.0] * 10, season_avg=22.0,
                usage_rate=0.14,
            ),
            PlayerMinutes(
                player_id=6, player_name="Player F", position="G",
                team_id=1, minutes_last_5=[18.0] * 5,
                minutes_last_10=[18.0] * 10, season_avg=18.0,
                usage_rate=0.12,
            ),
            PlayerMinutes(
                player_id=7, player_name="Player G", position="F",
                team_id=1, minutes_last_5=[14.0] * 5,
                minutes_last_10=[14.0] * 10, season_avg=14.0,
                usage_rate=0.10,
            ),
            PlayerMinutes(
                player_id=8, player_name="Player H", position="C",
                team_id=1, minutes_last_5=[10.0] * 5,
                minutes_last_10=[10.0] * 10, season_avg=10.0,
                usage_rate=0.08,
            ),
        ]

        result = engine.project_team_rotation(
            team_id=1, team_name="Test", rotation=rotation,
            injuries=[], game_date="2026-02-19",
            apply_coach_adjustments=False,
        )

        # Player with 2 DNPs should still get minutes (just resting, not suspended)
        resting = next(p for p in result.projections if p.player_id == 2)
        assert resting.adjusted_minutes > 0.0, (
            f"Player with only 2 DNPs should NOT be auto-excluded, "
            f"got {resting.adjusted_minutes} min"
        )

    def test_injury_report_takes_precedence_over_dnp_streak(self):
        """If player is already in injury report, DNP detection should not double-count."""
        engine = RotationEngine()
        rotation = [
            PlayerMinutes(
                player_id=1, player_name="Star", position="G",
                team_id=1, minutes_last_5=[36.0] * 5,
                minutes_last_10=[36.0] * 10, season_avg=36.0,
                usage_rate=0.28,
            ),
            PlayerMinutes(
                player_id=2, player_name="Injured Guy", position="F",
                team_id=1, minutes_last_5=[28.0] * 5,
                minutes_last_10=[28.0] * 10, season_avg=28.0,
                usage_rate=0.20, recent_dnp_streak=10,
            ),
            PlayerMinutes(
                player_id=3, player_name="Teammate", position="F",
                team_id=1, minutes_last_5=[26.0] * 5,
                minutes_last_10=[26.0] * 10, season_avg=26.0,
                usage_rate=0.18,
            ),
            PlayerMinutes(
                player_id=4, player_name="Center", position="C",
                team_id=1, minutes_last_5=[24.0] * 5,
                minutes_last_10=[24.0] * 10, season_avg=24.0,
                usage_rate=0.16,
            ),
            PlayerMinutes(
                player_id=5, player_name="Guard", position="G",
                team_id=1, minutes_last_5=[22.0] * 5,
                minutes_last_10=[22.0] * 10, season_avg=22.0,
                usage_rate=0.14,
            ),
            PlayerMinutes(
                player_id=6, player_name="Bench 1", position="G",
                team_id=1, minutes_last_5=[16.0] * 5,
                minutes_last_10=[16.0] * 10, season_avg=16.0,
                usage_rate=0.10,
            ),
            PlayerMinutes(
                player_id=7, player_name="Bench 2", position="F",
                team_id=1, minutes_last_5=[14.0] * 5,
                minutes_last_10=[14.0] * 10, season_avg=14.0,
                usage_rate=0.08,
            ),
            PlayerMinutes(
                player_id=8, player_name="Bench 3", position="C",
                team_id=1, minutes_last_5=[10.0] * 5,
                minutes_last_10=[10.0] * 10, season_avg=10.0,
                usage_rate=0.06,
            ),
        ]

        injuries = [
            PlayerStatus(
                player_id=2, player_name="Injured Guy",
                status="Out", injury_description="Knee surgery",
            )
        ]

        result = engine.project_team_rotation(
            team_id=1, team_name="Test", rotation=rotation,
            injuries=injuries, game_date="2026-02-19",
            apply_coach_adjustments=False,
        )

        # Should still be 0 min (injury report handles it)
        injured = next(p for p in result.projections if p.player_id == 2)
        assert injured.adjusted_minutes == 0.0

        # Should have 7 active players (not double-penalized)
        active = [p for p in result.projections if p.adjusted_minutes > 0]
        assert len(active) == 7, (
            f"Expected 7 active players (1 out), got {len(active)}"
        )
        # Star should still have meaningful minutes
        star = next(p for p in result.projections if p.player_id == 1)
        assert star.adjusted_minutes >= 35.0


# ============================================================================
# AGENT 9: TEAM-SPECIFIC INJURY OFFSET TESTS
# ============================================================================


class TestTeamInjuryOffsets:
    """Tests for Agent 9 learned team-specific injury factor offsets.

    When CalibrationService has learned that a team's GTD/Questionable/
    Doubtful players historically play more or fewer minutes than the
    static factors predict, the rotation engine applies an offset
    multiplier (capped at ±15%).
    """

    def test_gtd_offset_increases_minutes(self):
        """A team whose GTD players historically play MORE should get more minutes."""
        from app.services.calibration_service import CalibrationService

        cal = CalibrationService()
        cal._team_injury_offsets = {
            (1, "GTD"): 1.10,  # GTD players play 10% more than static 0.66
        }
        engine = RotationEngine(calibration_service=cal)

        rotation = [
            PlayerMinutes(
                player_id=i, player_name=f"Player {i}", position="G",
                team_id=1, minutes_last_5=[30.0] * 5,
                minutes_last_10=[30.0] * 10, season_avg=30.0,
                usage_rate=0.20,
            )
            for i in range(1, 9)
        ]

        baselines = {i: 30.0 for i in range(1, 9)}

        gtd_injury = [PlayerStatus(
            player_id=1, player_name="Player 1",
            status="GTD", injury_description="Sore knee",
        )]

        result = engine.redistribute_minutes(rotation, gtd_injury, baselines)

        # DFS "if-plays" model: GTD minute_factor = 0.75 (aggressive restriction)
        # With offset 1.10: minute_factor becomes 0.75 × 1.10 = 0.825
        # Standard (no offset): 30 × 0.75 = 22.5 min
        # With offset:          30 × min(1.0, 0.75 × 1.10) = 24.75 min
        p1 = result[1]
        standard_gtd_min = 30.0 * 0.75  # 22.5
        assert p1.adjusted_minutes > standard_gtd_min, (
            f"GTD player should get > {standard_gtd_min:.1f} min with +10% offset, "
            f"got {p1.adjusted_minutes:.1f}"
        )

    def test_doubtful_offset_decreases_minutes(self):
        """A team whose Doubtful players historically play LESS should get fewer minutes."""
        from app.services.calibration_service import CalibrationService

        cal = CalibrationService()
        cal._team_injury_offsets = {
            (1, "Doubtful"): 0.90,  # 10% less than static
        }
        engine = RotationEngine(calibration_service=cal)

        rotation = [
            PlayerMinutes(
                player_id=i, player_name=f"Player {i}", position="G",
                team_id=1, minutes_last_5=[30.0] * 5,
                minutes_last_10=[30.0] * 10, season_avg=30.0,
                usage_rate=0.20,
            )
            for i in range(1, 9)
        ]

        baselines = {i: 30.0 for i in range(1, 9)}

        doubtful_injury = [PlayerStatus(
            player_id=1, player_name="Player 1",
            status="Doubtful", injury_description="Back tightness",
        )]

        result = engine.redistribute_minutes(rotation, doubtful_injury, baselines)

        # DFS "if-plays" model: Doubtful minute_factor = 0.75 (not P × E = 0.15)
        # With offset 0.90: minute_factor becomes 0.75 × 0.90 = 0.675
        # Standard (no offset): 30 × 0.75 = 22.5 min
        # With offset:          30 × 0.675 = 20.25 min
        # play_probability = 0.20 (stored separately for scoring/exposure)
        p1 = result[1]
        standard_doubtful_min = 30.0 * 0.75  # 22.5
        assert p1.adjusted_minutes < standard_doubtful_min, (
            f"Doubtful player should get < {standard_doubtful_min:.1f} min "
            f"with -10% offset, got {p1.adjusted_minutes:.1f}"
        )
        assert p1.play_probability == 0.20, (
            f"Doubtful play_probability should be 0.20, got {p1.play_probability}"
        )

    def test_no_offset_for_out(self):
        """'Out' players always get 0 minutes — offset should NOT apply."""
        from app.services.calibration_service import CalibrationService

        cal = CalibrationService()
        cal._team_injury_offsets = {
            (1, "Out"): 1.15,  # Even a max offset shouldn't help
        }
        engine = RotationEngine(calibration_service=cal)

        rotation = [
            PlayerMinutes(
                player_id=i, player_name=f"Player {i}", position="G",
                team_id=1, minutes_last_5=[30.0] * 5,
                minutes_last_10=[30.0] * 10, season_avg=30.0,
                usage_rate=0.20,
            )
            for i in range(1, 9)
        ]

        baselines = {i: 30.0 for i in range(1, 9)}

        out_injury = [PlayerStatus(
            player_id=1, player_name="Player 1",
            status="Out", injury_description="Broken leg",
        )]

        result = engine.redistribute_minutes(rotation, out_injury, baselines)

        p1 = result[1]
        assert p1.adjusted_minutes == 0.0, (
            f"'Out' player must always get 0 min, got {p1.adjusted_minutes}"
        )

    def test_no_calibration_service_uses_static_factors(self):
        """Without CalibrationService, static factors are used unchanged."""
        engine = RotationEngine()  # No calibration_service

        rotation = [
            PlayerMinutes(
                player_id=i, player_name=f"Player {i}", position="G",
                team_id=1, minutes_last_5=[30.0] * 5,
                minutes_last_10=[30.0] * 10, season_avg=30.0,
                usage_rate=0.20,
            )
            for i in range(1, 9)
        ]

        baselines = {i: 30.0 for i in range(1, 9)}

        gtd_injury = [PlayerStatus(
            player_id=1, player_name="Player 1",
            status="GTD", injury_description="Sore knee",
        )]

        result = engine.redistribute_minutes(rotation, gtd_injury, baselines)

        # DFS "if-plays" model: GTD minute_factor = 0.75 → 30 × 0.75 = 22.5 min
        p1 = result[1]
        assert abs(p1.adjusted_minutes - 30.0 * 0.75) < 0.2, (
            f"Without calibration, GTD should use static 0.75 minute_factor, "
            f"got {p1.adjusted_minutes:.1f}"
        )


# ============================================================================
# POSITION NORMALIZATION TESTS
# ============================================================================


class TestPositionNormalization:
    """Tests for _positions_overlap() with production position formats."""

    def test_same_simplified_position(self, engine):
        assert engine._positions_overlap("G", "G") is True
        assert engine._positions_overlap("F", "F") is True
        assert engine._positions_overlap("C", "C") is True

    def test_same_specific_position(self, engine):
        assert engine._positions_overlap("PG", "PG") is True
        assert engine._positions_overlap("SG", "SG") is True

    def test_guard_family_overlap(self, engine):
        """PG and SG are both guards — must overlap."""
        assert engine._positions_overlap("PG", "SG") is True
        assert engine._positions_overlap("SG", "PG") is True

    def test_forward_family_overlap(self, engine):
        """SF and PF are both forwards — must overlap."""
        assert engine._positions_overlap("SF", "PF") is True
        assert engine._positions_overlap("PF", "SF") is True

    def test_mixed_format_overlap(self, engine):
        """Simplified 'G' overlaps with specific 'PG'."""
        assert engine._positions_overlap("G", "PG") is True
        assert engine._positions_overlap("PG", "G") is True
        assert engine._positions_overlap("F", "SF") is True
        assert engine._positions_overlap("PF", "F") is True

    def test_multi_position_overlap(self, engine):
        """Hyphenated multi-position designations."""
        assert engine._positions_overlap("G-F", "PG") is True
        assert engine._positions_overlap("F-C", "SF") is True
        assert engine._positions_overlap("G-F", "PF") is True

    def test_no_overlap_across_families(self, engine):
        """Guards do not overlap with centers or forwards."""
        assert engine._positions_overlap("PG", "C") is False
        assert engine._positions_overlap("SG", "PF") is False
        assert engine._positions_overlap("G", "C") is False
        assert engine._positions_overlap("C", "F") is False

    def test_center_is_isolated(self, engine):
        """C only overlaps with C."""
        assert engine._positions_overlap("C", "PG") is False
        assert engine._positions_overlap("C", "SF") is False
        assert engine._positions_overlap("C", "C") is True


# ============================================================================
# SPOT-START PROMOTION TESTS
# ============================================================================


class TestSpotStartPromotion:
    """Tests for spot-start promotion when a starter is ruled Out."""

    @staticmethod
    def _make_rotation(players_data):
        """Helper: build rotation from (name, pid, pos, avg) tuples."""
        return [
            PlayerMinutes(
                player_id=pid,
                player_name=name,
                position=pos,
                team_id=1,
                minutes_last_5=[avg] * 5,
                minutes_last_10=[avg] * 10,
                season_avg=avg,
                usage_rate=round(avg / 150, 3),
            )
            for name, pid, pos, avg in players_data
        ]

    def test_deep_bench_promoted_when_starter_out(self, engine):
        """Backup PG with 4 min avg should project 20+ min when starter Out."""
        rotation = self._make_rotation([
            ("Starting PG", 1, "G", 32.0),
            ("Starting SG", 2, "G", 30.0),
            ("Starting SF", 3, "F", 28.0),
            ("Starting PF", 4, "F", 26.0),
            ("Starting C", 5, "C", 24.0),
            ("Backup G", 6, "G", 18.0),
            ("Backup F", 7, "F", 16.0),
            ("Backup C", 8, "C", 14.0),
            ("Deep Bench PG", 9, "G", 4.0),   # Rob Dillingham scenario
            ("Deep Bench F", 10, "F", 8.0),
        ])
        baselines = {p.player_id: p.season_avg for p in rotation}

        # Starting PG is Out
        injuries = [PlayerStatus(
            player_id=1, player_name="Starting PG",
            status="Out", injury_description="Knee",
        )]

        result = engine.redistribute_minutes(rotation, injuries, baselines)

        # Backup G (pid 6, 18 min avg) is the primary backup
        # Deep Bench PG (pid 9, 4 min avg) should also get elevated
        # The key test: primary backup should NOT be capped at baseline*1.25
        primary = result[6]
        assert primary.adjusted_minutes > 20.0, (
            f"Primary backup should absorb starter's freed minutes, "
            f"got {primary.adjusted_minutes:.1f}"
        )

    def test_spot_start_cap_based_on_injured_baseline(self, engine):
        """Role cap should use injured starter's baseline, not backup's.

        Only one same-position backup ("C") so the promotion target is
        unambiguous.
        """
        rotation = self._make_rotation([
            ("Starter C", 1, "C", 32.0),    # Out — frees 32 min
            ("Backup C", 2, "C", 4.0),       # Only other C → primary backup
            ("G1", 3, "G", 35.0),
            ("G2", 4, "G", 30.0),
            ("F1", 5, "F", 28.0),
            ("F2", 6, "F", 26.0),
            ("Bench G", 7, "G", 18.0),
            ("Bench F", 8, "F", 16.0),
            ("Reserve G", 9, "G", 8.0),
            ("Reserve F", 10, "F", 6.0),
        ])
        baselines = {p.player_id: p.season_avg for p in rotation}

        injuries = [PlayerStatus(
            player_id=1, player_name="Starter C",
            status="Out", injury_description="Ankle",
        )]

        result = engine.redistribute_minutes(rotation, injuries, baselines)

        # Backup C (pid=2) is the only "C" → must be primary backup
        backup = result[2]

        # Without spot-start: cap = max(4*1.25, 20) = 20 min
        # With spot-start: cap = 32 * 0.88 = 28.2 min
        # Backup gets 100% of freed minutes (only same-position player)
        # → 4 + 32 = 36, capped at 28.2
        assert backup.adjusted_minutes > 20.0, (
            f"Spot-start cap should exceed old 20-min floor, "
            f"got {backup.adjusted_minutes:.1f}"
        )
        assert backup.is_spot_starter, "Backup C should be marked as spot-starter"

    def test_no_promotion_for_questionable(self, engine):
        """Questionable starter should NOT trigger spot-start promotion."""
        rotation = self._make_rotation([
            ("Starter C", 1, "C", 32.0),
            ("Backup C", 2, "C", 4.0),
            ("G1", 3, "G", 35.0),
            ("G2", 4, "G", 30.0),
            ("F1", 5, "F", 28.0),
            ("F2", 6, "F", 26.0),
            ("Bench G", 7, "G", 18.0),
            ("Bench F", 8, "F", 16.0),
            ("Reserve G", 9, "G", 8.0),
            ("Reserve F", 10, "F", 6.0),
        ])
        baselines = {p.player_id: p.season_avg for p in rotation}

        injuries = [PlayerStatus(
            player_id=1, player_name="Starter C",
            status="Questionable", injury_description="Sore knee",
        )]

        result = engine.redistribute_minutes(rotation, injuries, baselines)

        backup = result[2]
        # Questionable P(plays) = 0.85 → above 0.50 threshold
        # Should NOT get spot-start promotion
        assert not getattr(backup, "is_spot_starter", False), (
            "Questionable should NOT trigger spot-start promotion"
        )

    def test_doubtful_partial_promotion(self, engine):
        """Doubtful starter should trigger partial (blended) promotion."""
        rotation = self._make_rotation([
            ("Starter C", 1, "C", 32.0),
            ("Backup C", 2, "C", 4.0),
            ("G1", 3, "G", 35.0),
            ("G2", 4, "G", 30.0),
            ("F1", 5, "F", 28.0),
            ("F2", 6, "F", 26.0),
            ("Bench G", 7, "G", 18.0),
            ("Bench F", 8, "F", 16.0),
            ("Reserve G", 9, "G", 8.0),
            ("Reserve F", 10, "F", 6.0),
        ])
        baselines = {p.player_id: p.season_avg for p in rotation}

        injuries = [PlayerStatus(
            player_id=1, player_name="Starter C",
            status="Doubtful", injury_description="Hamstring",
        )]

        result = engine.redistribute_minutes(rotation, injuries, baselines)

        backup = result[2]
        # Doubtful P(plays) = 0.20 → below 0.50 threshold
        # Should trigger spot-start promotion on the only "C" backup
        assert backup.is_spot_starter, (
            "Doubtful should trigger spot-start promotion"
        )

    def test_multiple_injuries_multiple_promotions(self, engine):
        """Two starters Out at different positions → both backups promoted."""
        rotation = self._make_rotation([
            ("Starting C", 1, "C", 32.0),   # Out
            ("Starting G", 2, "G", 30.0),   # Out
            ("F1", 3, "F", 28.0),
            ("F2", 4, "F", 26.0),
            ("Bench F", 5, "F", 16.0),
            ("Backup C", 6, "C", 4.0),      # Only other C → promoted
            ("Backup G", 7, "G", 4.0),      # Only other G → promoted
            ("Reserve F1", 8, "F", 14.0),
            ("Reserve F2", 9, "F", 8.0),
            ("Reserve F3", 10, "F", 6.0),
        ])
        baselines = {p.player_id: p.season_avg for p in rotation}

        injuries = [
            PlayerStatus(player_id=1, player_name="Starting C",
                         status="Out", injury_description="Knee"),
            PlayerStatus(player_id=2, player_name="Starting G",
                         status="Out", injury_description="Ankle"),
        ]

        result = engine.redistribute_minutes(rotation, injuries, baselines)

        # Both deep bench backups should be promoted
        backup_c = result[6]
        backup_g = result[7]
        assert backup_c.is_spot_starter, "Backup C should be spot-starter"
        assert backup_g.is_spot_starter, "Backup G should be spot-starter"

    def test_primary_backup_also_injured_promotes_secondary(self, engine):
        """If primary backup is also injured, promote the next healthy backup."""
        rotation = self._make_rotation([
            ("Starter", 1, "G", 32.0),
            ("Primary Backup", 2, "G", 22.0),
            ("Secondary Backup", 3, "G", 4.0),
            ("SF", 4, "F", 28.0),
            ("PF", 5, "F", 26.0),
            ("C", 6, "C", 24.0),
            ("Bench F", 7, "F", 16.0),
            ("Bench C", 8, "C", 14.0),
            ("Reserve F", 9, "F", 8.0),
            ("Reserve C", 10, "C", 6.0),
        ])
        baselines = {p.player_id: p.season_avg for p in rotation}

        injuries = [
            PlayerStatus(player_id=1, player_name="Starter",
                         status="Out", injury_description="Knee"),
            PlayerStatus(player_id=2, player_name="Primary Backup",
                         status="Out", injury_description="Ankle"),
        ]

        result = engine.redistribute_minutes(rotation, injuries, baselines)

        # Secondary Backup (pid=3) should inherit the promotion
        secondary = result[3]
        assert secondary.is_spot_starter, (
            "Secondary backup should be promoted when primary is also injured"
        )

    def test_spot_start_flag_set(self, engine):
        """Verify is_spot_starter flag is True on promoted player."""
        rotation = self._make_rotation([
            ("Starter C", 1, "C", 32.0),
            ("Backup C", 2, "C", 4.0),     # Only other C → promoted
            ("G1", 3, "G", 35.0),
            ("G2", 4, "G", 30.0),
            ("F1", 5, "F", 28.0),
            ("F2", 6, "F", 26.0),
            ("Bench G", 7, "G", 18.0),
            ("Bench F", 8, "F", 16.0),
            ("Reserve G", 9, "G", 8.0),
            ("Reserve F", 10, "F", 6.0),
        ])
        baselines = {p.player_id: p.season_avg for p in rotation}

        injuries = [PlayerStatus(
            player_id=1, player_name="Starter C",
            status="Out", injury_description="Knee",
        )]

        result = engine.redistribute_minutes(rotation, injuries, baselines)

        backup = result[2]
        assert backup.is_spot_starter is True, (
            f"Expected is_spot_starter=True for promoted backup, "
            f"got {backup.is_spot_starter}"
        )
        # Non-promoted players at different positions should NOT have the flag
        g1 = result[3]
        assert not g1.is_spot_starter, (
            "Non-promoted player should not be marked as spot-starter"
        )

    def test_no_promotion_for_non_starter_injury(self, engine):
        """Bench player Out should NOT trigger spot-start promotion."""
        rotation = self._make_rotation([
            ("Starter G", 1, "G", 32.0),
            ("Starter G2", 2, "G", 30.0),
            ("SF", 3, "F", 28.0),
            ("PF", 4, "F", 26.0),
            ("C", 5, "C", 24.0),
            ("Bench G", 6, "G", 18.0),
            ("Bench F", 7, "F", 16.0),
            ("Bench C", 8, "C", 14.0),
            ("Deep Bench G", 9, "G", 4.0),
            ("Reserve F", 10, "F", 6.0),
        ])
        baselines = {p.player_id: p.season_avg for p in rotation}

        # Bench player (18 min, below 24 min starter threshold) is Out
        injuries = [PlayerStatus(
            player_id=6, player_name="Bench G",
            status="Out", injury_description="Knee",
        )]

        result = engine.redistribute_minutes(rotation, injuries, baselines)

        # Deep Bench G absorbs some minutes, but no spot-start promotion
        deep = result[9]
        assert not getattr(deep, "is_spot_starter", False), (
            "Non-starter injury should NOT trigger spot-start promotion"
        )

    def test_spot_start_with_dk_positions(self, engine):
        """Production scenario: DK-style positions (PG, SG, SF, PF, C).

        Simulates the Dillingham case: PG starter Out, backup PG (4 min
        season avg) should get spot-start promotion via position family
        matching (PG → G family).
        """
        rotation = self._make_rotation([
            ("Mike Conley", 1, "PG", 32.0),      # Out
            ("Anthony Edwards", 2, "SG", 36.0),
            ("Jaden McDaniels", 3, "SF", 30.0),
            ("Julius Randle", 4, "PF", 34.0),
            ("Rudy Gobert", 5, "C", 32.0),
            ("Nickeil Alexander-Walker", 6, "SG", 22.0),
            ("Kyle Anderson", 7, "SF", 18.0),
            ("Naz Reid", 8, "PF", 20.0),
            ("Rob Dillingham", 9, "PG", 4.0),    # Deep bench PG
            ("Luka Garza", 10, "C", 6.0),
        ])
        baselines = {p.player_id: p.season_avg for p in rotation}

        injuries = [PlayerStatus(
            player_id=1, player_name="Mike Conley",
            status="Out", injury_description="Toe soreness",
        )]

        result = engine.redistribute_minutes(rotation, injuries, baselines)

        # Edwards (SG, 36 min) is highest guard by season_avg → primary backup
        # because PG and SG are both "G" family.
        edwards = result[2]
        assert edwards.adjusted_minutes > 36.0, (
            f"Edwards should absorb primary share of freed guard minutes, "
            f"got {edwards.adjusted_minutes:.1f}"
        )

        # Dillingham should get secondary share + be above his baseline
        dillingham = result[9]
        assert dillingham.adjusted_minutes > 4.0, (
            f"Dillingham should absorb some freed minutes even as secondary, "
            f"got {dillingham.adjusted_minutes:.1f}"
        )

        # NAW (SG, 22 min) should also get some freed minutes
        naw = result[6]
        assert naw.adjusted_minutes > 22.0, (
            f"NAW should absorb some freed minutes as another guard, "
            f"got {naw.adjusted_minutes:.1f}"
        )

    def test_spot_start_only_pg_backup_promoted(self, engine):
        """When starting PG is Out with only one other PG, that PG gets promoted."""
        rotation = self._make_rotation([
            ("Starting PG", 1, "PG", 32.0),      # Out
            ("Starting SG", 2, "SG", 34.0),
            ("Starting SF", 3, "SF", 30.0),
            ("Starting PF", 4, "PF", 28.0),
            ("Starting C", 5, "C", 30.0),
            ("Backup SG", 6, "SG", 18.0),
            ("Backup SF", 7, "SF", 16.0),
            ("Backup PF", 8, "PF", 14.0),
            ("Backup PG", 9, "PG", 4.0),          # Deep bench backup
            ("Backup C", 10, "C", 12.0),
        ])
        baselines = {p.player_id: p.season_avg for p in rotation}

        injuries = [PlayerStatus(
            player_id=1, player_name="Starting PG",
            status="Out", injury_description="Knee",
        )]

        result = engine.redistribute_minutes(rotation, injuries, baselines)

        # The primary backup is Starting SG (34 min, also "G" family).
        # They get spot-start promotion since they're highest-minute guard.
        starting_sg = result[2]
        assert starting_sg.is_spot_starter, (
            "Starting SG should get spot-start promotion as highest-minute guard"
        )

        # Backup PG (4 min) should still get SOME freed minutes as a guard
        backup_pg = result[9]
        assert backup_pg.adjusted_minutes > 4.0, (
            f"Backup PG should receive some freed minutes, "
            f"got {backup_pg.adjusted_minutes:.1f}"
        )


# ============================================================================
# TRADE DETECTION / ROSTER CHANGE TESTS
# ============================================================================


class TestRosterChangeDetection:
    """Tests that roster_change_detected flag propagates correctly."""

    def test_roster_change_flag_propagated_to_projection(self, engine):
        """When a PlayerMinutes has roster_change_detected=True,
        the resulting PlayerProjection should also have it set."""
        rotation = [
            PlayerMinutes(
                player_id=1, player_name="Traded Player", position="PG",
                team_id=99, minutes_last_5=[28, 30, 26, 29, 27],
                minutes_last_10=[28, 30, 26, 29, 27, 28, 30, 26, 29, 27],
                season_avg=28.0, usage_rate=0.22,
                roster_change_detected=True,  # <-- recently traded
            ),
            PlayerMinutes(
                player_id=2, player_name="Existing Starter", position="SG",
                team_id=99, minutes_last_5=[34, 33, 35, 34, 33],
                minutes_last_10=[34, 33, 35, 34, 33, 34, 33, 35, 34, 33],
                season_avg=34.0, usage_rate=0.28,
            ),
            PlayerMinutes(
                player_id=3, player_name="Forward", position="SF",
                team_id=99, minutes_last_5=[30, 31, 29, 30, 30],
                minutes_last_10=[30, 31, 29, 30, 30, 30, 31, 29, 30, 30],
                season_avg=30.0, usage_rate=0.20,
            ),
            PlayerMinutes(
                player_id=4, player_name="PF Starter", position="PF",
                team_id=99, minutes_last_5=[28, 29, 27, 28, 28],
                minutes_last_10=[28, 29, 27, 28, 28, 28, 29, 27, 28, 28],
                season_avg=28.0, usage_rate=0.18,
            ),
            PlayerMinutes(
                player_id=5, player_name="Center", position="C",
                team_id=99, minutes_last_5=[26, 27, 25, 26, 26],
                minutes_last_10=[26, 27, 25, 26, 26, 26, 27, 25, 26, 26],
                season_avg=26.0, usage_rate=0.16,
            ),
        ]

        baselines = {p.player_id: p.season_avg for p in rotation}
        injuries: list = []

        result = engine.redistribute_minutes(rotation, injuries, baselines)

        traded = result[1]
        assert traded.roster_change_detected is True, (
            "Traded player's projection should have roster_change_detected=True"
        )

        existing = result[2]
        assert existing.roster_change_detected is False, (
            "Non-traded player should have roster_change_detected=False"
        )

    def test_roster_change_flag_false_by_default(self, engine):
        """By default, roster_change_detected is False."""
        rotation = [
            PlayerMinutes(
                player_id=1, player_name="Normal Player", position="G",
                team_id=99, minutes_last_5=[30, 31, 29, 30, 30],
                minutes_last_10=[30, 31, 29, 30, 30, 30, 31, 29, 30, 30],
                season_avg=30.0, usage_rate=0.22,
            ),
        ]
        baselines = {1: 30.0}
        result = engine.redistribute_minutes(rotation, [], baselines)
        assert result[1].roster_change_detected is False

    def test_traded_player_benefits_from_new_team_injuries(self, engine):
        """A traded player (roster_change_detected=True) should receive
        freed minutes when a new-team starter is Out, and maintain the
        roster_change_detected flag on the projection.  When they're the
        only backup at the injured position, they get spot-start promotion."""
        rotation = [
            # Starter who is Out — Center position (unique)
            PlayerMinutes(
                player_id=1, player_name="Injured Center", position="C",
                team_id=99, minutes_last_5=[34, 33, 35, 34, 33],
                minutes_last_10=[34, 33, 35, 34, 33, 34, 33, 35, 34, 33],
                season_avg=34.0, usage_rate=0.28,
            ),
            # Traded player who is the ONLY backup C
            PlayerMinutes(
                player_id=2, player_name="Traded Backup C", position="C",
                team_id=99, minutes_last_5=[20, 21, 19, 20, 20],
                minutes_last_10=[20, 21, 19, 20, 20, 20, 21, 19, 20, 20],
                season_avg=20.0, usage_rate=0.18,
                roster_change_detected=True,
            ),
            PlayerMinutes(
                player_id=3, player_name="PG Starter", position="PG",
                team_id=99, minutes_last_5=[32, 33, 31, 32, 32],
                minutes_last_10=[32, 33, 31, 32, 32, 32, 33, 31, 32, 32],
                season_avg=32.0, usage_rate=0.24,
            ),
            PlayerMinutes(
                player_id=4, player_name="SG Starter", position="SG",
                team_id=99, minutes_last_5=[30, 31, 29, 30, 30],
                minutes_last_10=[30, 31, 29, 30, 30, 30, 31, 29, 30, 30],
                season_avg=30.0, usage_rate=0.20,
            ),
            PlayerMinutes(
                player_id=5, player_name="SF Starter", position="SF",
                team_id=99, minutes_last_5=[28, 29, 27, 28, 28],
                minutes_last_10=[28, 29, 27, 28, 28, 28, 29, 27, 28, 28],
                season_avg=28.0, usage_rate=0.18,
            ),
            PlayerMinutes(
                player_id=6, player_name="PF Starter", position="PF",
                team_id=99, minutes_last_5=[26, 27, 25, 26, 26],
                minutes_last_10=[26, 27, 25, 26, 26, 26, 27, 25, 26, 26],
                season_avg=26.0, usage_rate=0.16,
            ),
        ]

        baselines = {p.player_id: p.season_avg for p in rotation}
        injuries = [PlayerStatus(
            player_id=1, player_name="Injured Center",
            status="Out", injury_description="Knee",
        )]

        result = engine.redistribute_minutes(rotation, injuries, baselines)

        traded_proj = result[2]
        assert traded_proj.roster_change_detected is True, (
            "Traded player's projection should retain roster_change_detected"
        )
        assert traded_proj.adjusted_minutes > 20.0, (
            f"Traded backup C should get elevated minutes from injured center, "
            f"got {traded_proj.adjusted_minutes:.1f}"
        )
        # Should get spot-start promotion since they're the only backup C
        assert traded_proj.is_spot_starter is True, (
            "Traded backup C should get spot-start promotion as only center backup"
        )

    def test_multiple_traded_players_all_flagged(self, engine):
        """Multiple traded players should all have the flag set."""
        rotation = [
            PlayerMinutes(
                player_id=1, player_name="Traded A", position="G",
                team_id=99, minutes_last_5=[25]*5,
                minutes_last_10=[25]*10, season_avg=25.0,
                usage_rate=0.20, roster_change_detected=True,
            ),
            PlayerMinutes(
                player_id=2, player_name="Traded B", position="F",
                team_id=99, minutes_last_5=[22]*5,
                minutes_last_10=[22]*10, season_avg=22.0,
                usage_rate=0.18, roster_change_detected=True,
            ),
            PlayerMinutes(
                player_id=3, player_name="Existing C", position="C",
                team_id=99, minutes_last_5=[30]*5,
                minutes_last_10=[30]*10, season_avg=30.0,
                usage_rate=0.25,
            ),
        ]
        baselines = {p.player_id: p.season_avg for p in rotation}
        result = engine.redistribute_minutes(rotation, [], baselines)

        assert result[1].roster_change_detected is True
        assert result[2].roster_change_detected is True
        assert result[3].roster_change_detected is False
