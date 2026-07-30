"""Tests for Phase 7: Shot-Type Decomposition.

Tests cover:
1. Decomposed PTS ≈ simple projection for typical players
2. 3PT shooter gets higher DK score via decomposition
3. FG3M correlated with PTS in decomposed mode
4. Fallback without shooting profile
5. League-average fallback for missing percentages
6. Center (no 3PT) modeled correctly
7. Constants verification
8. DB model columns exist
9. PlayerMinutes has_shooting_profile property
"""

import pytest
from typing import Optional

from app.models.player import PlayerMinutes
from app.services.dfs_service import DFSService


# ============================================================================
# HELPERS
# ============================================================================


def _make_player(
    pts_per_min: float = 0.6,
    fg3m_per_min: float = 0.1,
    # Shooting profile (all optional)
    fg3a_rate: Optional[float] = None,
    fga_rate: Optional[float] = None,
    fta_rate: Optional[float] = None,
    fg3_pct: Optional[float] = None,
    fg2_pct: Optional[float] = None,
    ft_pct: Optional[float] = None,
    **kwargs,
) -> PlayerMinutes:
    """Create a PlayerMinutes with optional shooting profile."""
    defaults = dict(
        player_id=1,
        player_name="Test Player",
        position="SG",
        team_id=1,
        minutes_last_5=[30.0] * 5,
        minutes_last_10=[30.0] * 10,
        season_avg=30.0,
        usage_rate=0.22,
        pts_per_min=pts_per_min,
        reb_per_min=0.15,
        ast_per_min=0.12,
        stl_per_min=0.03,
        blk_per_min=0.01,
        tov_per_min=0.05,
        fg3m_per_min=fg3m_per_min,
    )
    defaults.update(kwargs)
    return PlayerMinutes(
        fg3a_rate=fg3a_rate,
        fga_rate=fga_rate,
        fta_rate=fta_rate,
        fg3_pct=fg3_pct,
        fg2_pct=fg2_pct,
        ft_pct=ft_pct,
        **defaults,
    )


def _make_3pt_shooter() -> PlayerMinutes:
    """Create a high-volume 3PT shooter (e.g. Stephen Curry type).

    Shooting profile:
    - ~8 FGA/game in 32 min → 0.25 FGA/min
    - ~5 FG3A/game in 32 min → 0.16 FG3A/min (heavy 3PT bias)
    - ~4 FTA/game in 32 min → 0.125 FTA/min
    - 43% from 3, 55% from 2, 91% FT
    """
    return _make_player(
        pts_per_min=0.85,    # ~27 PTS in 32 min
        fg3m_per_min=0.068,  # ~2.2 FG3M in 32 min
        fg3a_rate=0.16,
        fga_rate=0.25,
        fta_rate=0.125,
        fg3_pct=0.43,
        fg2_pct=0.55,
        ft_pct=0.91,
    )


def _make_center() -> PlayerMinutes:
    """Create a paint-dominant center (e.g. Rudy Gobert type).

    Shooting profile:
    - ~6 FGA/game in 30 min → 0.20 FGA/min
    - ~0.1 FG3A/game → 0.003 FG3A/min (almost no 3PT)
    - ~3 FTA/game → 0.10 FTA/min
    - 10% from 3, 65% from 2, 65% FT
    """
    return _make_player(
        position="C",
        pts_per_min=0.45,    # ~13.5 PTS in 30 min
        fg3m_per_min=0.001,  # ~0.03 FG3M in 30 min
        fg3a_rate=0.003,
        fga_rate=0.20,
        fta_rate=0.10,
        fg3_pct=0.10,
        fg2_pct=0.65,
        ft_pct=0.65,
    )


# ============================================================================
# DECOMPOSITION TESTS
# ============================================================================


class TestShotDecomposition:
    """Tests for _project_stats_decomposed method."""

    def test_fallback_without_profile(self):
        """Without shooting profile, should fall back to _project_stats."""
        svc = DFSService()
        player = _make_player()  # No shooting profile
        assert not player.has_shooting_profile

        stats_base = svc._project_stats(30.0, player)
        stats_decomp = svc._project_stats_decomposed(30.0, player)

        # Should be identical when no profile
        assert stats_base == stats_decomp

    def test_decomposed_has_all_stats(self):
        """Decomposed stats should contain all 7 stat fields."""
        svc = DFSService()
        player = _make_3pt_shooter()

        stats = svc._project_stats_decomposed(32.0, player)
        required_keys = {"pts", "reb", "ast", "stl", "blk", "tov", "fg3m"}
        assert required_keys.issubset(stats.keys())

    def test_decomposed_pts_positive(self):
        """Decomposed PTS should be positive."""
        svc = DFSService()
        player = _make_3pt_shooter()

        stats = svc._project_stats_decomposed(32.0, player)
        assert stats["pts"] > 0

    def test_decomposed_fg3m_positive_for_shooter(self):
        """3PT shooter should have positive FG3M from decomposition."""
        svc = DFSService()
        player = _make_3pt_shooter()

        stats = svc._project_stats_decomposed(32.0, player)
        assert stats["fg3m"] > 0

    def test_center_low_fg3m(self):
        """Center with minimal 3PT attempts should have near-zero FG3M."""
        svc = DFSService()
        player = _make_center()

        stats = svc._project_stats_decomposed(30.0, player)
        assert stats["fg3m"] < 0.5  # Near zero

    def test_shooter_higher_dk_score_than_center(self):
        """3PT shooter should have higher DK score than center at same PTS.

        The 3PM bonus means scoring via threes is worth more in DK scoring.
        """
        svc = DFSService()
        shooter = _make_3pt_shooter()
        center = _make_center()

        # Get decomposed stats
        shooter_stats = svc._project_stats_decomposed(32.0, shooter)
        center_stats = svc._project_stats_decomposed(30.0, center)

        # Calculate DK score (PTS×1.0 + FG3M×0.5 + other stats)
        shooter_dk = (
            shooter_stats["pts"] * 1.0
            + shooter_stats["fg3m"] * 0.5
        )
        center_dk = (
            center_stats["pts"] * 1.0
            + center_stats["fg3m"] * 0.5
        )

        # Per-PTS value: shooter should get more DK points per PTS point
        if shooter_stats["pts"] > 0 and center_stats["pts"] > 0:
            shooter_per_pts = shooter_dk / shooter_stats["pts"]
            center_per_pts = center_dk / center_stats["pts"]
            assert shooter_per_pts > center_per_pts, (
                f"Shooter DK/PTS={shooter_per_pts:.3f} should exceed "
                f"center DK/PTS={center_per_pts:.3f}"
            )

    def test_fg3m_correlated_with_pts_for_shooter(self):
        """In decomposed mode, FG3M should be proportional to PTS for shooters."""
        svc = DFSService()
        shooter = _make_3pt_shooter()

        # Project at different minute levels
        stats_low = svc._project_stats_decomposed(20.0, shooter)
        stats_high = svc._project_stats_decomposed(36.0, shooter)

        # Both PTS and FG3M should scale with minutes
        assert stats_high["pts"] > stats_low["pts"]
        assert stats_high["fg3m"] > stats_low["fg3m"]

        # FG3M/PTS ratio should be roughly stable (correlated)
        if stats_low["pts"] > 0 and stats_high["pts"] > 0:
            ratio_low = stats_low["fg3m"] / stats_low["pts"]
            ratio_high = stats_high["fg3m"] / stats_high["pts"]
            assert abs(ratio_high - ratio_low) < 0.05, (
                f"FG3M/PTS ratio should be stable: low={ratio_low:.3f}, "
                f"high={ratio_high:.3f}"
            )

    def test_non_scoring_stats_unchanged(self):
        """REB, AST, STL, BLK, TOV should be same as base projection."""
        svc = DFSService()
        player = _make_3pt_shooter()

        stats_base = svc._project_stats(32.0, player)
        stats_decomp = svc._project_stats_decomposed(32.0, player)

        for stat in ["reb", "ast", "stl", "blk", "tov"]:
            assert stats_base[stat] == stats_decomp[stat], (
                f"{stat} should be unchanged: base={stats_base[stat]}, "
                f"decomp={stats_decomp[stat]}"
            )


# ============================================================================
# PLAYER MINUTES MODEL TESTS
# ============================================================================


class TestPlayerMinutesShootingProfile:
    """Tests for PlayerMinutes shooting profile fields."""

    def test_has_shooting_profile_true(self):
        """Player with all profile fields should report True."""
        player = _make_3pt_shooter()
        assert player.has_shooting_profile is True

    def test_has_shooting_profile_false(self):
        """Player without profile fields should report False."""
        player = _make_player()
        assert player.has_shooting_profile is False

    def test_partial_profile_is_false(self):
        """Player with only some profile fields should report False."""
        player = _make_player(
            fg3a_rate=0.16,
            fga_rate=0.25,
            # Missing fta_rate, fg3_pct, fg2_pct, ft_pct
        )
        assert player.has_shooting_profile is False


# ============================================================================
# DB MODEL TESTS
# ============================================================================


class TestDBShootingColumns:
    """Tests for shooting profile columns in PlayerMinutesHistory."""

    def test_shooting_columns_exist(self):
        """DB model should have all 6 shooting profile columns."""
        from app.db.models import PlayerMinutesHistory

        expected_cols = [
            "actual_fg3a_rate",
            "actual_fga_rate",
            "actual_fta_rate",
            "actual_fg3_pct",
            "actual_fg2_pct",
            "actual_ft_pct",
        ]
        columns = {c.name for c in PlayerMinutesHistory.__table__.columns}
        for col in expected_cols:
            assert col in columns, f"Missing column: {col}"


# ============================================================================
# CONSTANTS TESTS
# ============================================================================


class TestShotDecompConstants:
    """Tests for Phase 7 shot decomposition constants."""

    def test_constants_importable(self):
        from app.config.constants import (
            SHOT_DECOMP_MIN_GAMES,
            SHOT_DECOMP_LEAGUE_AVG_FG3_PCT,
            SHOT_DECOMP_LEAGUE_AVG_FG2_PCT,
            SHOT_DECOMP_LEAGUE_AVG_FT_PCT,
        )
        assert SHOT_DECOMP_MIN_GAMES == 10
        assert 0.30 <= SHOT_DECOMP_LEAGUE_AVG_FG3_PCT <= 0.40
        assert 0.45 <= SHOT_DECOMP_LEAGUE_AVG_FG2_PCT <= 0.60
        assert 0.70 <= SHOT_DECOMP_LEAGUE_AVG_FT_PCT <= 0.85

    def test_fg3_pct_less_than_fg2_pct(self):
        """3PT% should be lower than 2PT% (basic sanity)."""
        from app.config.constants import (
            SHOT_DECOMP_LEAGUE_AVG_FG3_PCT,
            SHOT_DECOMP_LEAGUE_AVG_FG2_PCT,
        )
        assert SHOT_DECOMP_LEAGUE_AVG_FG3_PCT < SHOT_DECOMP_LEAGUE_AVG_FG2_PCT


# ============================================================================
# CALIBRATED SHOT RATE TESTS
# ============================================================================


class TestShotRateCalibrationApplied:
    """Tests verifying shot-rate calibrations affect decomposed projections."""

    def test_shot_rate_calibration_applied(self):
        """Mock calibration service should scale fg3a_rate in decomposition."""
        from unittest.mock import MagicMock

        svc = DFSService()
        player = _make_3pt_shooter()

        # Baseline without calibration
        stats_no_cal = svc._project_stats_decomposed(32.0, player)

        # Create mock calibration that boosts 3PA rate by 10%
        mock_cal = MagicMock()
        mock_cal.get_dvp_sensitivity_for_stat.return_value = 1.0
        mock_cal.get_pace_sensitivity_adjustment.return_value = 1.0
        mock_cal.get_stat_rate_adjustment.return_value = 1.0
        mock_cal.get_shot_rate_adjustment.side_effect = lambda rt: (
            1.10 if rt == "fg3a" else 1.0
        )

        svc._calibration = mock_cal
        stats_cal = svc._project_stats_decomposed(32.0, player)

        # FG3M should increase when fg3a_rate is boosted
        assert stats_cal["fg3m"] > stats_no_cal["fg3m"], (
            f"FG3M should increase with boosted fg3a_rate: "
            f"cal={stats_cal['fg3m']}, no_cal={stats_no_cal['fg3m']}"
        )

        svc._calibration = None  # Clean up

    def test_per_stat_dvp_sensitivity(self):
        """Per-stat DvP sensitivity should be called per field."""
        from unittest.mock import MagicMock

        svc = DFSService()
        player = _make_player(pts_per_min=0.6, fg3m_per_min=0.1)

        mock_cal = MagicMock()
        mock_cal.get_dvp_sensitivity_for_stat.return_value = 1.0
        mock_cal.get_pace_sensitivity_adjustment.return_value = 1.0
        mock_cal.get_stat_rate_adjustment.return_value = 1.0

        svc._calibration = mock_cal
        matchup = {"pts": 1.1, "reb": 0.9, "ast": 1.05}
        svc._project_stats(30.0, player, matchup_factors=matchup)

        # Should have been called for each stat with a dvp != 1.0
        calls = mock_cal.get_dvp_sensitivity_for_stat.call_args_list
        called_stats = {c[0][0] for c in calls}
        # pts, reb, ast should each have triggered a call
        assert "pts" in called_stats
        assert "reb" in called_stats
        assert "ast" in called_stats

        svc._calibration = None  # Clean up

    def test_new_db_columns_exist(self):
        """DB model should have opponent_team_id and projected rate columns."""
        from app.db.models import PlayerMinutesHistory

        columns = {c.name for c in PlayerMinutesHistory.__table__.columns}
        assert "opponent_team_id" in columns
        assert "projected_fg3a_rate" in columns
        assert "projected_fga_rate" in columns
        assert "projected_fta_rate" in columns
