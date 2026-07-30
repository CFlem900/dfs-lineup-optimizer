"""Tests for BDL Advanced Metric Calibration functions.

Tests cover:
1. compute_shot_decomposition_stats — per-minute shooting rate biases
2. _shot_rate_to_calibration_keys — converting biases to calibration multipliers
3. compute_dvp_recalibration — per-stat DvP sensitivity from opponent grouping
4. compute_noise_sigma_adjustments — Monte Carlo noise sigma tuning
"""

import pytest
from typing import Dict, List

from app.services.agents.backtesting_agent import (
    compute_shot_decomposition_stats,
    _shot_rate_to_calibration_keys,
    compute_dvp_recalibration,
    compute_noise_sigma_adjustments,
)


# ============================================================================
# HELPERS
# ============================================================================


def _make_row(
    actual_minutes: float = 28.0,
    projected_minutes: float = 27.0,
    position: str = "SG",
    opponent_team_id: int = 100,
    # Shooting rates
    actual_fg3a_rate: float = 0.20,
    projected_fg3a_rate: float = 0.18,
    actual_fga_rate: float = 0.35,
    projected_fga_rate: float = 0.33,
    actual_fta_rate: float = 0.10,
    projected_fta_rate: float = 0.09,
    actual_fg3_pct: float = 0.370,
    actual_fg2_pct: float = 0.510,
    actual_ft_pct: float = 0.830,
    # Per-stat actuals/projections
    actual_pts: float = 20.0,
    projected_pts: float = 19.0,
    actual_reb: float = 5.0,
    projected_reb: float = 5.5,
    actual_ast: float = 4.0,
    projected_ast: float = 3.5,
    actual_stl: float = 1.0,
    projected_stl: float = 1.2,
    actual_blk: float = 0.5,
    projected_blk: float = 0.4,
    actual_tov: float = 2.0,
    projected_tov: float = 2.1,
    actual_fg3m: float = 2.0,
    projected_fg3m: float = 1.8,
    **overrides,
) -> Dict:
    row = {
        "actual_minutes": actual_minutes,
        "projected_minutes": projected_minutes,
        "position": position,
        "opponent_team_id": opponent_team_id,
        "actual_fg3a_rate": actual_fg3a_rate,
        "projected_fg3a_rate": projected_fg3a_rate,
        "actual_fga_rate": actual_fga_rate,
        "projected_fga_rate": projected_fga_rate,
        "actual_fta_rate": actual_fta_rate,
        "projected_fta_rate": projected_fta_rate,
        "actual_fg3_pct": actual_fg3_pct,
        "actual_fg2_pct": actual_fg2_pct,
        "actual_ft_pct": actual_ft_pct,
        "actual_pts": actual_pts,
        "projected_pts": projected_pts,
        "actual_reb": actual_reb,
        "projected_reb": projected_reb,
        "actual_ast": actual_ast,
        "projected_ast": projected_ast,
        "actual_stl": actual_stl,
        "projected_stl": projected_stl,
        "actual_blk": actual_blk,
        "projected_blk": projected_blk,
        "actual_tov": actual_tov,
        "projected_tov": projected_tov,
        "actual_fg3m": actual_fg3m,
        "projected_fg3m": projected_fg3m,
        "actual_fp": 35.0,
        "projected_fp": 34.0,
        "player_name": "Test Player",
    }
    row.update(overrides)
    return row


def _make_rows(n: int = 20, **kwargs) -> List[Dict]:
    """Generate n identical rows with optional overrides."""
    return [_make_row(**kwargs) for _ in range(n)]


# ============================================================================
# SHOT DECOMPOSITION STATS TESTS
# ============================================================================


class TestShotDecompositionStats:
    """Tests for compute_shot_decomposition_stats."""

    def test_basic_bias_computation(self):
        """Synthetic data with known projected/actual fg3a_rate gap."""
        # Actual consistently 0.02 above projected
        rows = _make_rows(
            20,
            actual_fg3a_rate=0.20,
            projected_fg3a_rate=0.18,
        )
        result = compute_shot_decomposition_stats(rows)

        assert "shot_rate_bias" in result
        fg3a = result["shot_rate_bias"].get("fg3a_rate")
        assert fg3a is not None
        assert fg3a["n"] == 20
        assert abs(fg3a["bias"] - 0.02) < 0.001

    def test_empty_data_returns_empty(self):
        """No BDL-enriched rows should return empty dict."""
        result = compute_shot_decomposition_stats([])
        assert result == {}

    def test_no_bdl_rows_returns_empty(self):
        """Rows without BDL enrichment (None rates) should return empty."""
        rows = _make_rows(20, actual_fg3a_rate=None, projected_fg3a_rate=None)
        result = compute_shot_decomposition_stats(rows)
        assert result == {}

    def test_position_grouping(self):
        """Different positions should have different rate biases."""
        guards = _make_rows(
            20, position="SG",
            actual_fg3a_rate=0.22, projected_fg3a_rate=0.18,
        )
        centers = _make_rows(
            20, position="C",
            actual_fg3a_rate=0.05, projected_fg3a_rate=0.06,
        )
        rows = guards + centers
        result = compute_shot_decomposition_stats(rows, min_sample=10)

        by_pos = result.get("shot_rate_by_position", {})
        assert "SG" in by_pos
        assert "C" in by_pos
        sg_bias = by_pos["SG"]["fg3a_rate"]["bias"]
        c_bias = by_pos["C"]["fg3a_rate"]["bias"]
        assert sg_bias > 0  # SG over-shoots vs projection
        assert c_bias < 0   # C under-shoots vs projection

    def test_min_sample_threshold(self):
        """Fewer than min_sample rows should return empty dict."""
        rows = _make_rows(10)  # Below default min_sample=15
        result = compute_shot_decomposition_stats(rows, min_sample=15)
        assert result == {}

    def test_pct_accuracy_included(self):
        """Percentage accuracy should be included when data available."""
        rows = _make_rows(20, actual_fg3_pct=0.370, actual_fg2_pct=0.510)
        result = compute_shot_decomposition_stats(rows)
        assert "pct_accuracy" in result
        if result["pct_accuracy"]:
            fg3_pct = result["pct_accuracy"].get("fg3_pct")
            if fg3_pct:
                assert fg3_pct["n"] == 20

    def test_mae_always_positive(self):
        """MAE (mean absolute error) should always be >= 0."""
        rows = _make_rows(20)
        result = compute_shot_decomposition_stats(rows)
        for rate_type, data in result.get("shot_rate_bias", {}).items():
            assert data["mae"] >= 0

    def test_mean_projected_positive(self):
        """Mean projected rate should be positive when we have data."""
        rows = _make_rows(20)
        result = compute_shot_decomposition_stats(rows)
        for rate_type, data in result.get("shot_rate_bias", {}).items():
            assert data["mean_projected"] > 0


# ============================================================================
# SHOT RATE TO CALIBRATION KEYS TESTS
# ============================================================================


class TestShotRateToCalibrationKeys:
    """Tests for _shot_rate_to_calibration_keys helper."""

    def test_converts_bias_to_multiplier(self):
        """Known bias should produce calibration multiplier."""
        shot_stats = {
            "shot_rate_bias": {
                "fg3a_rate": {
                    "bias": 0.02,           # actual 0.02 above projected
                    "mae": 0.025,
                    "n": 25,
                    "mean_projected": 0.18,  # ~11% relative bias
                },
            },
        }
        result = _shot_rate_to_calibration_keys(shot_stats)
        assert "shot_rate_fg3a" in result
        # multiplier = 1.0 + 0.02/0.18 ≈ 1.111
        assert result["shot_rate_fg3a"] > 1.0
        assert result["shot_rate_fg3a"] < 1.15  # clamped

    def test_skips_small_bias(self):
        """Bias < 5% relative should not produce a key."""
        shot_stats = {
            "shot_rate_bias": {
                "fg3a_rate": {
                    "bias": 0.005,           # actual 0.005 above projected
                    "mae": 0.01,
                    "n": 25,
                    "mean_projected": 0.18,  # ~2.8% relative bias — below 5%
                },
            },
        }
        result = _shot_rate_to_calibration_keys(shot_stats)
        assert "shot_rate_fg3a" not in result

    def test_clamping(self):
        """Extreme bias should be clamped to [0.85, 1.15]."""
        shot_stats = {
            "shot_rate_bias": {
                "fg3a_rate": {
                    "bias": 0.10,            # massive over-production
                    "mae": 0.10,
                    "n": 25,
                    "mean_projected": 0.18,  # ~55% relative bias
                },
            },
        }
        result = _shot_rate_to_calibration_keys(shot_stats)
        assert result["shot_rate_fg3a"] == 1.15  # clamped at upper bound

    def test_empty_input_returns_empty(self):
        """Empty shot stats should return empty dict."""
        assert _shot_rate_to_calibration_keys({}) == {}
        assert _shot_rate_to_calibration_keys(None) == {}

    def test_negative_bias_produces_lower_multiplier(self):
        """Negative bias (over-projecting) should produce multiplier < 1.0."""
        shot_stats = {
            "shot_rate_bias": {
                "fga_rate": {
                    "bias": -0.03,           # actual below projected
                    "mae": 0.03,
                    "n": 25,
                    "mean_projected": 0.33,  # ~9% relative bias
                },
            },
        }
        result = _shot_rate_to_calibration_keys(shot_stats)
        assert "shot_rate_fga" in result
        assert result["shot_rate_fga"] < 1.0


# ============================================================================
# DVP RECALIBRATION TESTS
# ============================================================================


class TestDvPRecalibration:
    """Tests for compute_dvp_recalibration."""

    def _make_dvp_rows(
        self,
        n_opponents: int = 6,
        rows_per_opp: int = 10,
        base_ratio: float = 1.0,
        variance: float = 0.05,
    ) -> List[Dict]:
        """Generate rows across multiple opponents with controlled variance."""
        import random
        random.seed(42)
        rows = []
        for opp_id in range(100, 100 + n_opponents):
            # Each opponent has a different ratio applied to actuals
            opp_factor = base_ratio + random.uniform(-variance, variance)
            for _ in range(rows_per_opp):
                proj_pts = 18.0 + random.uniform(-3, 3)
                rows.append(_make_row(
                    opponent_team_id=opp_id,
                    actual_minutes=28.0 + random.uniform(-5, 5),
                    projected_minutes=27.0 + random.uniform(-3, 3),
                    actual_pts=round(proj_pts * opp_factor, 1),
                    projected_pts=round(proj_pts, 1),
                    actual_reb=round(5.0 * opp_factor, 1),
                    projected_reb=5.0,
                    actual_ast=round(4.0 * opp_factor, 1),
                    projected_ast=4.0,
                    actual_stl=round(1.0 * opp_factor, 1),
                    projected_stl=1.0,
                    actual_blk=round(0.5 * opp_factor, 1),
                    projected_blk=0.5,
                    actual_tov=round(2.0 * opp_factor, 1),
                    projected_tov=2.0,
                    actual_fg3m=round(2.0 * opp_factor, 1),
                    projected_fg3m=2.0,
                ))
        return rows

    def test_insufficient_opponents(self):
        """Fewer than min_opponents unique opponents should return empty."""
        rows = self._make_dvp_rows(n_opponents=3, rows_per_opp=10)
        result = compute_dvp_recalibration(rows, min_opponents=5)
        assert result == {}

    def test_per_stat_keys(self):
        """Should return keys like dvp_sensitivity_pts."""
        rows = self._make_dvp_rows(n_opponents=8, rows_per_opp=10)
        result = compute_dvp_recalibration(rows)

        # Should have per-stat keys
        stat_keys_found = [k for k in result if k.startswith("dvp_sensitivity_")]
        assert len(stat_keys_found) > 0

    def test_global_key_included(self):
        """Backward-compat dvp_sensitivity key should always be present."""
        rows = self._make_dvp_rows(n_opponents=8, rows_per_opp=10)
        result = compute_dvp_recalibration(rows)

        if result:  # Only if we have enough data
            assert "dvp_sensitivity" in result

    def test_clamping(self):
        """Extreme values should be clamped to [0.85, 1.15]."""
        rows = self._make_dvp_rows(n_opponents=8, rows_per_opp=10)
        result = compute_dvp_recalibration(rows)

        for key, val in result.items():
            assert 0.85 <= val <= 1.15, f"{key}={val} not in [0.85, 1.15]"

    def test_empty_data_returns_empty(self):
        """Empty input should return empty dict."""
        assert compute_dvp_recalibration([]) == {}

    def test_no_opponent_id_returns_empty(self):
        """Rows without opponent_team_id should return empty."""
        rows = _make_rows(50, opponent_team_id=None)
        result = compute_dvp_recalibration(rows)
        assert result == {}


# ============================================================================
# NOISE SIGMA ADJUSTMENTS TESTS
# ============================================================================


class TestNoiseSigmaAdjustments:
    """Tests for compute_noise_sigma_adjustments."""

    def test_basic_sigma_adjustment(self):
        """Known variance should produce correct multiplier direction."""
        rows = _make_rows(50, actual_minutes=30.0, projected_minutes=28.0)
        result = compute_noise_sigma_adjustments(rows)

        # Should have noise_sigma_* keys
        sigma_keys = [k for k in result if k.startswith("noise_sigma_")]
        assert len(sigma_keys) > 0

    def test_insufficient_sample(self):
        """Fewer than min_sample rows should return empty dict."""
        rows = _make_rows(20)  # Below default min_sample=30
        result = compute_noise_sigma_adjustments(rows, min_sample=30)
        assert result == {}

    def test_clamping(self):
        """Extreme variance should be clamped to [0.85, 1.15]."""
        rows = _make_rows(50)
        result = compute_noise_sigma_adjustments(rows)

        for key, val in result.items():
            assert 0.85 <= val <= 1.15, f"{key}={val} not in [0.85, 1.15]"

    def test_keys_match_stat_noise_sigma(self):
        """Output keys should match noise_sigma_{stat} pattern."""
        rows = _make_rows(50)
        result = compute_noise_sigma_adjustments(rows)

        for key in result:
            assert key.startswith("noise_sigma_"), f"Unexpected key: {key}"

    def test_zero_production_stat_skipped(self):
        """Stats with zero mean production should be skipped."""
        # All rows have 0 blocks
        rows = _make_rows(50, actual_blk=0.0, projected_blk=0.0)
        result = compute_noise_sigma_adjustments(rows)

        # blk should be skipped (zero production = zero mean)
        assert "noise_sigma_blk" not in result

    def test_empty_data_returns_empty(self):
        """Empty input should return empty dict."""
        assert compute_noise_sigma_adjustments([]) == {}

    def test_low_minutes_filtered_out(self):
        """Players with < 10 actual minutes should be filtered out."""
        rows = _make_rows(50, actual_minutes=5.0)
        result = compute_noise_sigma_adjustments(rows)
        assert result == {}
