"""Extended tests for CalibrationService — RL feedback math and validation.

Covers:
  - clamp_adjustment() edge cases and precision
  - _confidence_scale() attenuation math
  - _validated_get() bounds checking
  - Ownership factor weight parsing and filtering
  - get_all_ownership_weights() key extraction
  - Team injury offset math (compute formula verification)
  - Projection-layer calibration getters
  - Noise overrides validation
  - get_by_category() filtering
  - Ownership leverage alpha/baseline with strategy routing

No DB required — all tests use injected cache dicts.
"""

import pytest

from app.services.calibration_service import (
    CalibrationService,
    clamp_adjustment,
    _confidence_scale,
    _BASE_INJURY_FACTORS,
    _MIN_OFFSET_SAMPLES,
    MAX_ADJUSTMENT,
    MIN_MULTIPLIER,
    MAX_MULTIPLIER,
    VALID_MIN,
    VALID_MAX,
)


# ---------------------------------------------------------------------------
# Constants verification
# ---------------------------------------------------------------------------

class TestConstants:
    def test_max_adjustment_is_15_percent(self):
        assert MAX_ADJUSTMENT == 0.15

    def test_min_multiplier(self):
        assert MIN_MULTIPLIER == 0.85

    def test_max_multiplier(self):
        assert MAX_MULTIPLIER == 1.15

    def test_valid_bounds(self):
        assert VALID_MIN == 0.5
        assert VALID_MAX == 2.0

    def test_base_injury_factors_completeness(self):
        expected_statuses = {"Out", "Doubtful", "GTD", "Game Time Decision", "Questionable"}
        assert set(_BASE_INJURY_FACTORS.keys()) == expected_statuses

    def test_gtd_alias_consistent(self):
        assert _BASE_INJURY_FACTORS["GTD"] == _BASE_INJURY_FACTORS["Game Time Decision"]


# ---------------------------------------------------------------------------
# clamp_adjustment() — edge cases
# ---------------------------------------------------------------------------

class TestClampAdjustment:
    def test_negative_value(self):
        assert clamp_adjustment(-1.0) == MIN_MULTIPLIER

    def test_zero(self):
        assert clamp_adjustment(0.0) == MIN_MULTIPLIER

    def test_neutral(self):
        assert clamp_adjustment(1.0) == 1.0

    def test_slightly_above_max(self):
        assert clamp_adjustment(1.16) == MAX_MULTIPLIER

    def test_slightly_below_min(self):
        assert clamp_adjustment(0.84) == MIN_MULTIPLIER

    def test_extreme_positive(self):
        assert clamp_adjustment(100.0) == MAX_MULTIPLIER

    def test_extreme_negative(self):
        assert clamp_adjustment(-100.0) == MIN_MULTIPLIER

    @pytest.mark.parametrize("value", [0.85, 0.90, 0.95, 1.0, 1.05, 1.10, 1.15])
    def test_in_range_passthrough(self, value):
        assert clamp_adjustment(value) == value


# ---------------------------------------------------------------------------
# _confidence_scale() — attenuation math
# ---------------------------------------------------------------------------

class TestConfidenceScale:
    def test_full_confidence_passthrough(self):
        """confidence >= 0.99 returns clamped as-is."""
        assert _confidence_scale(0.95, 1.0) == 0.95
        assert _confidence_scale(1.10, 0.99) == 1.10

    def test_zero_confidence_returns_neutral(self):
        """confidence=0 attenuates to exactly 1.0."""
        assert _confidence_scale(0.90, 0.0) == 1.0
        assert _confidence_scale(1.15, 0.0) == 1.0

    def test_half_confidence_halves_deviation(self):
        """confidence=0.5 should halve the deviation from 1.0."""
        # clamped=0.90 → deviation=-0.10 → half=-0.05 → result=0.95
        result = _confidence_scale(0.90, 0.5)
        assert abs(result - 0.95) < 1e-6

    def test_half_confidence_positive(self):
        # clamped=1.10 → deviation=+0.10 → half=+0.05 → result=1.05
        result = _confidence_scale(1.10, 0.5)
        assert abs(result - 1.05) < 1e-6

    def test_30_percent_confidence(self):
        """clamped=1.10, confidence=0.3 → 1.0 + 0.10 × 0.3 = 1.03."""
        result = _confidence_scale(1.10, 0.3)
        assert abs(result - 1.03) < 1e-6

    def test_neutral_value_unaffected_by_confidence(self):
        """clamped=1.0 always returns 1.0 regardless of confidence."""
        assert _confidence_scale(1.0, 0.0) == 1.0
        assert _confidence_scale(1.0, 0.5) == 1.0
        assert _confidence_scale(1.0, 1.0) == 1.0

    def test_negative_deviation_with_low_confidence(self):
        # clamped=0.85 → deviation=-0.15 → ×0.2 = -0.03 → 0.97
        result = _confidence_scale(0.85, 0.2)
        assert abs(result - 0.97) < 1e-6


# ---------------------------------------------------------------------------
# _validated_get() — bounds checking
# ---------------------------------------------------------------------------

class TestValidatedGet:
    def test_missing_key_returns_default(self):
        svc = CalibrationService()
        svc._cache = {}
        assert svc._validated_get("nonexistent", 1.0) == 1.0

    def test_valid_value_returned(self):
        svc = CalibrationService()
        svc._cache = {"test_key": 1.08}
        assert svc._validated_get("test_key", 1.0) == 1.08

    def test_value_below_valid_min_returns_default(self):
        svc = CalibrationService()
        svc._cache = {"bad_key": 0.3}  # Below VALID_MIN=0.5
        assert svc._validated_get("bad_key", 1.0) == 1.0

    def test_value_above_valid_max_returns_default(self):
        svc = CalibrationService()
        svc._cache = {"bad_key": 2.5}  # Above VALID_MAX=2.0
        assert svc._validated_get("bad_key", 1.0) == 1.0

    def test_value_at_valid_boundary(self):
        svc = CalibrationService()
        svc._cache = {"low": VALID_MIN, "high": VALID_MAX}
        assert svc._validated_get("low", 1.0) == VALID_MIN
        assert svc._validated_get("high", 1.0) == VALID_MAX

    def test_value_equals_default_returns_default(self):
        """When the stored value equals the default, return it (no validation)."""
        svc = CalibrationService()
        svc._cache = {"key": 1.0}
        assert svc._validated_get("key", 1.0) == 1.0


# ---------------------------------------------------------------------------
# Ownership factor weights
# ---------------------------------------------------------------------------

class TestOwnershipFactorWeights:
    def test_get_ownership_factor_weight_exists(self):
        svc = CalibrationService()
        svc._cache = {"ownership_factor_salary_weight": 1.12}
        result = svc.get_ownership_factor_weight("salary")
        assert result == 1.12

    def test_get_ownership_factor_weight_missing(self):
        svc = CalibrationService()
        svc._cache = {}
        result = svc.get_ownership_factor_weight("salary")
        assert result is None

    def test_get_ownership_factor_weight_out_of_bounds(self):
        svc = CalibrationService()
        svc._cache = {"ownership_factor_salary_weight": 0.3}  # < VALID_MIN
        result = svc.get_ownership_factor_weight("salary")
        assert result is None

    def test_get_all_ownership_weights(self):
        svc = CalibrationService()
        svc._cache = {
            "ownership_factor_salary_weight": 1.12,
            "ownership_factor_value_weight": 0.95,
            "ownership_factor_game_env_weight": 0.88,
            "unrelated_key": 1.0,
            "ownership_factor_bad_weight": 0.1,  # Out of bounds
        }
        result = svc.get_all_ownership_weights()
        assert result == {
            "salary": 1.12,
            "value": 0.95,
            "game_env": 0.88,
        }
        assert "bad" not in result  # Filtered out

    def test_get_all_ownership_weights_empty(self):
        svc = CalibrationService()
        svc._cache = {"some_other_key": 1.0}
        assert svc.get_all_ownership_weights() == {}


# ---------------------------------------------------------------------------
# Ownership leverage routing
# ---------------------------------------------------------------------------

class TestOwnershipLeverage:
    def test_leverage_alpha_gpp(self):
        svc = CalibrationService()
        svc._cache = {"ownership_leverage_alpha": 1.08}
        assert svc.get_ownership_leverage_alpha("gpp") == 1.08

    def test_leverage_alpha_contrarian(self):
        svc = CalibrationService()
        svc._cache = {
            "ownership_leverage_alpha": 1.08,
            "ownership_leverage_contrarian_alpha": 1.14,
        }
        assert svc.get_ownership_leverage_alpha("contrarian") == 1.14

    def test_leverage_baseline_gpp(self):
        svc = CalibrationService()
        svc._cache = {"ownership_leverage_baseline": 0.92}
        assert svc.get_ownership_leverage_baseline("gpp") == 0.92

    def test_leverage_baseline_contrarian(self):
        svc = CalibrationService()
        svc._cache = {"ownership_leverage_contrarian_baseline": 0.88}
        assert svc.get_ownership_leverage_baseline("contrarian") == 0.88

    def test_leverage_defaults_to_1(self):
        svc = CalibrationService()
        svc._cache = {}
        assert svc.get_ownership_leverage_alpha() == 1.0
        assert svc.get_ownership_leverage_baseline() == 1.0


# ---------------------------------------------------------------------------
# Projection-layer calibration getters
# ---------------------------------------------------------------------------

class TestProjectionCalibrations:
    def test_stat_rate_adjustment(self):
        svc = CalibrationService()
        svc._cache = {"stat_rate_pts": 1.05, "stat_rate_ast": 0.92}
        assert svc.get_stat_rate_adjustment("pts") == 1.05
        assert svc.get_stat_rate_adjustment("ast") == 0.92
        assert svc.get_stat_rate_adjustment("blk") == 1.0  # Missing key

    def test_dvp_sensitivity(self):
        svc = CalibrationService()
        svc._cache = {"dvp_sensitivity": 0.85}
        assert svc.get_dvp_sensitivity() == 0.85

    def test_pace_sensitivity_adjustment(self):
        svc = CalibrationService()
        svc._cache = {"pace_sensitivity_pts": 1.10}
        assert svc.get_pace_sensitivity_adjustment("pts") == 1.10
        assert svc.get_pace_sensitivity_adjustment("reb") == 1.0  # Missing

    def test_salary_tier_projection_adj(self):
        svc = CalibrationService()
        svc._cache = {
            "salary_tier_high_projection": 0.97,
            "salary_tier_value_projection": 1.04,
        }
        assert svc.get_salary_tier_projection_adj("high") == 0.97
        assert svc.get_salary_tier_projection_adj("value") == 1.04
        assert svc.get_salary_tier_projection_adj("mid") == 1.0

    def test_minutes_blend_adjustment(self):
        svc = CalibrationService()
        svc._cache = {"minutes_blend_season_weight": 1.05}
        assert svc.get_minutes_blend_adjustment("season") == 1.05
        assert svc.get_minutes_blend_adjustment("recent") == 1.0


# ---------------------------------------------------------------------------
# Noise overrides
# ---------------------------------------------------------------------------

class TestNoiseOverrides:
    def test_no_overrides(self):
        svc = CalibrationService()
        svc._cache = {}
        assert svc.get_noise_overrides() is None

    def test_valid_overrides(self):
        svc = CalibrationService()
        svc._cache = {
            "noise_sigma_pts": 1.10,
            "noise_sigma_reb": 0.85,
        }
        result = svc.get_noise_overrides()
        assert result is not None
        assert result["pts"] == 1.10
        assert result["reb"] == 0.85

    def test_out_of_bounds_overrides_filtered(self):
        svc = CalibrationService()
        svc._cache = {
            "noise_sigma_pts": 1.10,
            "noise_sigma_reb": 0.3,   # Below VALID_MIN
            "noise_sigma_ast": 2.5,   # Above VALID_MAX
        }
        result = svc.get_noise_overrides()
        assert result == {"pts": 1.10}

    def test_all_overrides_out_of_bounds(self):
        svc = CalibrationService()
        svc._cache = {
            "noise_sigma_pts": 0.1,
            "noise_sigma_reb": 5.0,
        }
        assert svc.get_noise_overrides() is None


# ---------------------------------------------------------------------------
# get_by_category()
# ---------------------------------------------------------------------------

class TestGetByCategory:
    def test_empty_cache(self):
        svc = CalibrationService()
        assert svc.get_by_category("stacking") == {}

    def test_populated(self):
        svc = CalibrationService()
        svc._cache_by_category = {
            "stacking": {"stacking_bring_back": 1.05, "stacking_team": 0.98},
            "position": {"position_PG_bias": 1.03},
        }
        result = svc.get_by_category("stacking")
        assert len(result) == 2
        assert result["stacking_bring_back"] == 1.05

    def test_missing_category(self):
        svc = CalibrationService()
        svc._cache_by_category = {"stacking": {"key": 1.0}}
        assert svc.get_by_category("nonexistent") == {}


# ---------------------------------------------------------------------------
# Game context multiplier
# ---------------------------------------------------------------------------

class TestGameContextMultiplier:
    def test_blowout(self):
        svc = CalibrationService()
        svc._cache = {"game_context_blowout": 0.92}
        assert svc.get_game_context_multiplier("blowout") == 0.92

    def test_b2b(self):
        svc = CalibrationService()
        svc._cache = {"game_context_b2b": 0.95}
        assert svc.get_game_context_multiplier("b2b") == 0.95

    def test_high_total(self):
        svc = CalibrationService()
        svc._cache = {"game_context_high_total": 1.08}
        assert svc.get_game_context_multiplier("high_total") == 1.08

    def test_missing_defaults_to_1(self):
        svc = CalibrationService()
        svc._cache = {}
        assert svc.get_game_context_multiplier("unknown") == 1.0


# ---------------------------------------------------------------------------
# Team injury offset math — formula verification
# ---------------------------------------------------------------------------

class TestTeamInjuryOffsetMath:
    """Verify the mathematical formula for team-specific injury offsets.

    The compute function (which queries DB) follows this formula:
        raw_offset = observed_ratio / base_factor
        confidence = min(1.0, sample_size / _MIN_OFFSET_SAMPLES)
        attenuated = 1.0 + (raw_offset - 1.0) × confidence
        clamped = clamp_adjustment(attenuated)

    We test the formula components here without DB access.
    """

    def test_neutral_when_observed_matches_expected(self):
        """If observed ratio equals the base factor, offset should be ~1.0."""
        base_factor = _BASE_INJURY_FACTORS["GTD"]  # 0.66
        observed_ratio = 0.66
        raw_offset = observed_ratio / base_factor  # 1.0
        confidence = min(1.0, 20 / _MIN_OFFSET_SAMPLES)
        attenuated = 1.0 + (raw_offset - 1.0) * confidence
        result = clamp_adjustment(attenuated)
        assert result == 1.0

    def test_positive_offset_when_observed_higher(self):
        """Team's GTD players play more than expected → offset > 1.0."""
        base_factor = _BASE_INJURY_FACTORS["GTD"]  # 0.66
        observed_ratio = 0.72  # Players play 72% of baseline
        raw_offset = observed_ratio / base_factor  # ~1.09
        confidence = 1.0  # Full sample
        attenuated = 1.0 + (raw_offset - 1.0) * confidence
        result = clamp_adjustment(attenuated)
        assert 1.05 < result < 1.15

    def test_negative_offset_when_observed_lower(self):
        """Team's GTD players play less than expected → offset < 1.0."""
        base_factor = _BASE_INJURY_FACTORS["GTD"]  # 0.66
        observed_ratio = 0.50  # Players only play 50% of baseline
        raw_offset = observed_ratio / base_factor  # ~0.76
        confidence = 1.0
        attenuated = 1.0 + (raw_offset - 1.0) * confidence
        result = clamp_adjustment(attenuated)
        assert result == MIN_MULTIPLIER  # Clamped at 0.85

    def test_confidence_attenuation_with_small_sample(self):
        """Small sample (5 of 10 required) should halve the deviation."""
        base_factor = _BASE_INJURY_FACTORS["Questionable"]  # 0.81
        observed_ratio = 0.90  # Higher than expected
        raw_offset = observed_ratio / base_factor  # ~1.111
        sample_size = 5
        confidence = min(1.0, sample_size / _MIN_OFFSET_SAMPLES)  # 0.5
        attenuated = 1.0 + (raw_offset - 1.0) * confidence
        # deviation = 0.111, halved = 0.0556, attenuated ≈ 1.0556
        result = clamp_adjustment(attenuated)
        assert 1.03 < result < 1.08

    def test_confidence_saturates_above_threshold(self):
        """Samples above threshold give confidence=1.0."""
        confidence = min(1.0, 20 / _MIN_OFFSET_SAMPLES)
        assert confidence == 1.0

    def test_extreme_observed_ratio_clamped(self):
        """Even extreme observed ratios are clamped to ±15%."""
        base_factor = _BASE_INJURY_FACTORS["Doubtful"]  # 0.15
        observed_ratio = 0.80  # Way higher than 0.15 expected
        raw_offset = observed_ratio / base_factor  # 5.33!
        confidence = 1.0
        attenuated = 1.0 + (raw_offset - 1.0) * confidence  # ~5.33
        result = clamp_adjustment(attenuated)
        assert result == MAX_MULTIPLIER  # 1.15

    def test_out_status_skipped(self):
        """'Out' has base_factor=0.0, so offset can't be computed."""
        assert _BASE_INJURY_FACTORS["Out"] == 0.0
        # Division by zero is avoided in compute_team_injury_offsets()
        # by checking `base_factor == 0.0` → continue

    def test_get_team_injury_offset_from_precomputed(self):
        """Verify the getter returns precomputed offsets."""
        svc = CalibrationService()
        svc._team_injury_offsets = {
            (100, "GTD"): 1.09,
            (100, "Questionable"): 0.96,
            (200, "Doubtful"): 1.05,
        }
        assert svc.get_team_injury_offset(100, "GTD") == 1.09
        assert svc.get_team_injury_offset(100, "Questionable") == 0.96
        assert svc.get_team_injury_offset(200, "Doubtful") == 1.05
        assert svc.get_team_injury_offset(300, "GTD") == 1.0  # Unknown team


# ---------------------------------------------------------------------------
# End-to-end calibration pipeline: simulated RL feedback loop
# ---------------------------------------------------------------------------

class TestFeedbackLoopSimulation:
    """Simulate the full calibration feedback loop math without DB.

    The RL loop is:
    1. Backtesting agent computes raw adjustments
    2. clamp_adjustment() caps to ±15%
    3. Tournament agent adds confidence-scaled adjustments
    4. Tournament adjustments override backtest on key conflicts
    5. Getters validate bounds before returning
    """

    def test_full_pipeline_backtest_only(self):
        """Simulate backtest → clamp → cache → getter flow."""
        svc = CalibrationService()

        # Step 1: Backtest agent produces raw adjustments
        raw_adjustments = {
            "stat_rate_pts": 1.08,       # PTS slightly underprojected
            "stat_rate_reb": 0.75,       # REB way overprojected (will clamp)
            "dvp_sensitivity": 0.90,     # DvP too strong
            "position_PG_bias": 1.20,    # PG bias extreme (will clamp)
        }

        # Step 2: Clamp each adjustment
        clamped = {k: clamp_adjustment(v) for k, v in raw_adjustments.items()}
        assert clamped["stat_rate_pts"] == 1.08    # In range
        assert clamped["stat_rate_reb"] == 0.85    # Clamped from 0.75
        assert clamped["dvp_sensitivity"] == 0.90  # In range
        assert clamped["position_PG_bias"] == 1.15 # Clamped from 1.20

        # Step 3: Store in cache
        svc._cache = clamped

        # Step 4: Getters return validated values
        assert svc.get_stat_rate_adjustment("pts") == 1.08
        assert svc.get_stat_rate_adjustment("reb") == 0.85
        assert svc.get_dvp_sensitivity() == 0.90
        assert svc.get_position_bias("PG") == 1.15

    def test_full_pipeline_tournament_overrides_backtest(self):
        """Tournament adjustments (with confidence) override backtest."""
        svc = CalibrationService()

        # Backtest says PTS is +8%
        backtest = {"stat_rate_pts": 1.08}

        # Tournament analysis says PTS is +12% at 60% confidence
        tournament_raw = 1.12
        tournament_confidence = 0.6
        tournament_clamped = clamp_adjustment(tournament_raw)  # 1.12
        tournament_effective = _confidence_scale(tournament_clamped, tournament_confidence)
        # deviation = 0.12, × 0.6 = 0.072, result = 1.072
        assert abs(tournament_effective - 1.072) < 1e-3

        # Tournament overrides backtest
        calibrations = {**backtest, "stat_rate_pts": tournament_effective}
        svc._cache = calibrations
        assert abs(svc.get_stat_rate_adjustment("pts") - 1.072) < 1e-3

    def test_corrupt_data_rejected_by_getters(self):
        """If corrupt data somehow enters cache, getters reject it."""
        svc = CalibrationService()
        svc._cache = {
            "stat_rate_pts": 0.1,   # Way below VALID_MIN=0.5
            "stat_rate_reb": 5.0,   # Way above VALID_MAX=2.0
            "dvp_sensitivity": 1.05,  # Valid
        }
        assert svc.get_stat_rate_adjustment("pts") == 1.0  # Rejected → default
        assert svc.get_stat_rate_adjustment("reb") == 1.0  # Rejected → default
        assert svc.get_dvp_sensitivity() == 1.05            # Accepted

    def test_multiple_iterations_converge(self):
        """Simulate 3 backtesting iterations — adjustments should converge."""
        adjustments = 1.0  # Start neutral

        for iteration in range(3):
            # Simulate: model is 5% under-projecting, so raw adjustment is +5%
            raw_error = 1.05
            raw_adj = adjustments * raw_error
            adjustments = clamp_adjustment(raw_adj)

        # After 3 iterations of +5% compounding: 1.0 × 1.05³ ≈ 1.157 → clamped to 1.15
        assert adjustments == MAX_MULTIPLIER
