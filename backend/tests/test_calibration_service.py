"""Unit tests for CalibrationService (sync-only, no DB required)."""

import pytest

from app.services.calibration_service import (
    CalibrationService,
    clamp_adjustment,
    _BASE_INJURY_FACTORS,
    _MIN_OFFSET_SAMPLES,
)


# ── clamp_adjustment tests ────────────────────────────────────────────


def test_clamp_at_lower_bound():
    """Values below 0.85 are clamped to 0.85."""
    assert clamp_adjustment(0.5) == 0.85


def test_clamp_at_upper_bound():
    """Values above 1.15 are clamped to 1.15."""
    assert clamp_adjustment(1.5) == 1.15


def test_clamp_within_range():
    """Values already within [0.85, 1.15] pass through unchanged."""
    assert clamp_adjustment(1.05) == 1.05


def test_clamp_exact_boundary():
    """Boundary values 0.85 and 1.15 are returned as-is."""
    assert clamp_adjustment(0.85) == 0.85
    assert clamp_adjustment(1.15) == 1.15


# ── get_position_bias tests ──────────────────────────────────────────


def test_get_position_bias_empty_cache():
    """With no cache loaded, position bias defaults to 1.0."""
    svc = CalibrationService()
    assert svc.get_position_bias("PG") == 1.0


def test_get_position_bias_populated():
    """When cache contains a position bias key, it is returned."""
    svc = CalibrationService()
    svc._cache = {"position_PG_bias": 1.05}
    assert svc.get_position_bias("PG") == 1.05


# ── get_salary_tier_adjustment tests ─────────────────────────────────


def test_get_salary_tier_empty():
    """With no cache loaded, salary tier adjustment defaults to 1.0."""
    svc = CalibrationService()
    assert svc.get_salary_tier_adjustment("high") == 1.0


def test_get_salary_tier_populated():
    """When cache contains a salary tier key, it is returned."""
    svc = CalibrationService()
    svc._cache = {"salary_tier_high": 0.97, "salary_tier_value": 1.08}
    assert svc.get_salary_tier_adjustment("high") == 0.97
    assert svc.get_salary_tier_adjustment("value") == 1.08


# ── get_all tests ────────────────────────────────────────────────────


def test_get_all_empty():
    """A fresh service with no cache returns an empty dict."""
    svc = CalibrationService()
    assert svc.get_all() == {}


def test_get_all_with_cache():
    """get_all() returns the full cache dict when populated."""
    svc = CalibrationService()
    data = {
        "position_PG_bias": 1.03,
        "salary_tier_mid": 0.98,
        "dvp_sensitivity": 1.10,
    }
    svc._cache = data
    result = svc.get_all()
    assert result == data
    # Verify it's the same object (no copy), matching implementation
    assert result is data


# ── Agent 9: Team-Specific Injury Offset tests ─────────────────────


def test_get_team_injury_offset_no_cache():
    """With no offsets loaded, returns 1.0 (neutral)."""
    svc = CalibrationService()
    assert svc.get_team_injury_offset(1610612737, "GTD") == 1.0
    assert svc.get_team_injury_offset(1610612738, "Doubtful") == 1.0
    assert svc.get_team_injury_offset(1610612739, "Out") == 1.0


def test_get_team_injury_offset_populated():
    """When offsets are loaded, returns the learned multiplier."""
    svc = CalibrationService()
    svc._team_injury_offsets = {
        (1610612737, "GTD"): 1.09,       # ATL GTD players play 9% more
        (1610612738, "Doubtful"): 0.92,   # BOS Doubtful players play 8% less
        (1610612739, "Questionable"): 1.04,
    }
    assert svc.get_team_injury_offset(1610612737, "GTD") == 1.09
    assert svc.get_team_injury_offset(1610612738, "Doubtful") == 0.92
    assert svc.get_team_injury_offset(1610612739, "Questionable") == 1.04


def test_get_team_injury_offset_unknown_team():
    """Unknown team returns 1.0 even when other offsets exist."""
    svc = CalibrationService()
    svc._team_injury_offsets = {
        (1610612737, "GTD"): 1.09,
    }
    assert svc.get_team_injury_offset(9999999, "GTD") == 1.0


def test_get_team_injury_offset_unknown_status():
    """Unknown status returns 1.0 even when team has other offsets."""
    svc = CalibrationService()
    svc._team_injury_offsets = {
        (1610612737, "GTD"): 1.09,
    }
    assert svc.get_team_injury_offset(1610612737, "Doubtful") == 1.0


def test_get_team_injury_offset_gtd_alias():
    """'Game Time Decision' should resolve to the GTD offset."""
    svc = CalibrationService()
    svc._team_injury_offsets = {
        (1610612737, "GTD"): 1.09,
    }
    # "Game Time Decision" normalises to "GTD" in the getter
    assert svc.get_team_injury_offset(1610612737, "Game Time Decision") == 1.09


def test_get_all_team_injury_offsets_empty():
    """No offsets returns empty dict."""
    svc = CalibrationService()
    assert svc.get_all_team_injury_offsets() == {}


def test_get_all_team_injury_offsets_flat_keys():
    """Flat dict uses 'injury_offset_{team_id}_{status}' format."""
    svc = CalibrationService()
    svc._team_injury_offsets = {
        (1610612737, "GTD"): 1.09,
        (1610612738, "Doubtful"): 0.92,
    }
    flat = svc.get_all_team_injury_offsets()
    assert flat == {
        "injury_offset_1610612737_GTD": 1.09,
        "injury_offset_1610612738_Doubtful": 0.92,
    }


def test_base_injury_factors_match_expectations():
    """Verify the base factors used for offset computation."""
    assert _BASE_INJURY_FACTORS["Out"] == 0.0
    assert _BASE_INJURY_FACTORS["Doubtful"] == 0.15
    assert _BASE_INJURY_FACTORS["GTD"] == 0.66
    assert _BASE_INJURY_FACTORS["Questionable"] == 0.81


def test_min_offset_samples_reasonable():
    """Min sample size should be > 0 and practical."""
    assert _MIN_OFFSET_SAMPLES >= 5
    assert _MIN_OFFSET_SAMPLES <= 50


# ── Per-stat DvP sensitivity tests ────────────────────────────────────


def test_get_dvp_sensitivity_for_stat_per_stat():
    """Per-stat key found should return its value."""
    svc = CalibrationService()
    svc._cache = {"dvp_sensitivity_pts": 1.08}
    assert svc.get_dvp_sensitivity_for_stat("pts") == 1.08


def test_get_dvp_sensitivity_for_stat_fallback_global():
    """Without per-stat key, falls back to global dvp_sensitivity."""
    svc = CalibrationService()
    svc._cache = {"dvp_sensitivity": 0.95}
    # No dvp_sensitivity_reb key, should fall back to global
    assert svc.get_dvp_sensitivity_for_stat("reb") == 0.95


def test_get_dvp_sensitivity_for_stat_fallback_default():
    """With empty cache, returns default 1.0."""
    svc = CalibrationService()
    assert svc.get_dvp_sensitivity_for_stat("ast") == 1.0


# ── Shot rate adjustment tests ────────────────────────────────────────


def test_get_shot_rate_adjustment():
    """shot_rate key found should return its value."""
    svc = CalibrationService()
    svc._cache = {"shot_rate_fg3a": 1.03}
    assert svc.get_shot_rate_adjustment("fg3a") == 1.03


def test_get_shot_rate_adjustment_default():
    """With empty cache, returns default 1.0."""
    svc = CalibrationService()
    assert svc.get_shot_rate_adjustment("fga") == 1.0
