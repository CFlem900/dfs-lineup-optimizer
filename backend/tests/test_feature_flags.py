"""Tests for the feature flag system."""

import pytest
from unittest.mock import patch, MagicMock

from app.services.feature_flags import FeatureFlagService, is_feature_enabled


# ---------------------------------------------------------------------------
# Helpers — build a fake Settings with all feature flags defaulting to True
# ---------------------------------------------------------------------------

def _make_settings(**overrides):
    """Create a mock Settings object with all feature flags = True + overrides."""
    from app.config import Settings

    defaults = {}
    for field_name in Settings.model_fields:
        if field_name.startswith("feature_"):
            defaults[field_name] = True

    defaults.update(overrides)

    settings = MagicMock()
    settings.model_fields = {
        k: None for k in Settings.model_fields if k.startswith("feature_")
    }
    for k, v in defaults.items():
        setattr(settings, k, v)

    return settings


# ---------------------------------------------------------------------------
# FeatureFlagService unit tests
# ---------------------------------------------------------------------------

class TestFeatureFlagService:
    """Core FeatureFlagService behaviour."""

    def test_all_flags_default_true(self):
        settings = _make_settings()
        flags = FeatureFlagService(settings=settings)
        for key in settings.model_fields:
            short = key.replace("feature_", "").upper()
            assert flags.is_enabled(short) is True, f"{key} should default True"

    def test_disable_specific_flag(self):
        settings = _make_settings(feature_agent_injury_impact=False)
        flags = FeatureFlagService(settings=settings)
        assert flags.is_enabled("AGENT_INJURY_IMPACT") is False
        assert flags.is_enabled("AGENT_SIGNAL_ANALYSIS") is True

    def test_case_insensitive(self):
        settings = _make_settings(feature_calibration=False)
        flags = FeatureFlagService(settings=settings)
        assert flags.is_enabled("calibration") is False
        assert flags.is_enabled("CALIBRATION") is False
        assert flags.is_enabled("Calibration") is False

    def test_unknown_flag_defaults_true(self):
        settings = _make_settings()
        flags = FeatureFlagService(settings=settings)
        assert flags.is_enabled("NONEXISTENT_FLAG") is True

    def test_list_flags(self):
        settings = _make_settings(
            feature_agent_chat=False,
            feature_calibration=True,
        )
        flags = FeatureFlagService(settings=settings)
        result = flags.list_flags()
        assert isinstance(result, dict)
        assert "feature_agent_chat" in result
        assert result["feature_agent_chat"] is False
        assert "feature_calibration" in result
        assert result["feature_calibration"] is True

    def test_list_flags_only_feature_keys(self):
        settings = _make_settings()
        flags = FeatureFlagService(settings=settings)
        result = flags.list_flags()
        for key in result:
            assert key.startswith("feature_"), f"Unexpected key: {key}"


class TestModuleLevelHelper:
    """Module-level is_feature_enabled() convenience function."""

    def test_module_level_helper(self):
        """is_feature_enabled() should work with the global singleton."""
        import app.services.feature_flags as ff_module

        settings = _make_settings(feature_nightly_pipeline=False)
        old = ff_module._instance
        try:
            ff_module._instance = FeatureFlagService(settings=settings)
            assert ff_module.is_feature_enabled("NIGHTLY_PIPELINE") is False
            assert ff_module.is_feature_enabled("CALIBRATION") is True
        finally:
            ff_module._instance = old


class TestSettingsIntegration:
    """Verify that the Settings class has all expected feature flag fields."""

    def test_settings_has_all_feature_flags(self):
        from app.config import Settings

        expected = [
            "feature_agent_line_movement",
            "feature_agent_signal_analysis",
            "feature_agent_injury_impact",
            "feature_agent_news_projection",
            "feature_agent_ownership",
            "feature_agent_lineup_strategy",
            "feature_agent_narrative",
            "feature_agent_simulation_tuning",
            "feature_agent_coach_learning",
            "feature_agent_expert_quality",
            "feature_agent_backtesting",
            "feature_agent_tournament_analysis",
            "feature_agent_chat",
            "feature_calibration",
            "feature_nightly_pipeline",
        ]
        for field in expected:
            assert field in Settings.model_fields, f"Missing Settings field: {field}"

    def test_settings_defaults_all_true(self):
        from app.config import Settings

        for field_name, field_info in Settings.model_fields.items():
            if field_name.startswith("feature_"):
                assert field_info.default is True, (
                    f"{field_name} should default to True"
                )


class TestFlagCount:
    """Ensure the expected number of feature flags exist."""

    def test_fifteen_feature_flags(self):
        from app.config import Settings

        feature_fields = [
            f for f in Settings.model_fields if f.startswith("feature_")
        ]
        assert len(feature_fields) == 15, (
            f"Expected 15 feature flags, found {len(feature_fields)}: {feature_fields}"
        )
