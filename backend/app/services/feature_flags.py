"""Lightweight, config-driven feature flag system.

Reads boolean ``feature_*`` fields from the pydantic-settings ``Settings``
class (backed by ``.env`` or environment variables).  Provides both a
class-based API and a module-level convenience function::

    from app.services.feature_flags import is_feature_enabled

    if is_feature_enabled('AGENT_INJURY_IMPACT'):
        agent = InjuryImpactAgent(ai_service)

Env-var mapping (pydantic-settings auto-uppercases)::

    FEATURE_AGENT_INJURY_IMPACT=false   →  is_feature_enabled('AGENT_INJURY_IMPACT') == False
    FEATURE_CALIBRATION=false           →  is_feature_enabled('CALIBRATION') == False
"""

from __future__ import annotations

import logging
from typing import Dict, Optional

logger = logging.getLogger(__name__)


class FeatureFlagService:
    """Lightweight feature flag reader backed by Settings."""

    def __init__(self, settings=None):
        if settings is None:
            from app.config import get_settings
            settings = get_settings()
        self._settings = settings

    def is_enabled(self, flag: str) -> bool:
        """Check if a feature flag is enabled.

        Accepts short names like ``'AGENT_INJURY_IMPACT'`` or
        ``'CALIBRATION'``.  Normalises to the settings field name
        ``feature_<lower>``.  Unknown flags default to ``True`` (safe).
        """
        key = f"feature_{flag.lower()}"
        return bool(getattr(self._settings, key, True))

    def list_flags(self) -> Dict[str, bool]:
        """Return all feature flags and their current values."""
        return {
            field: getattr(self._settings, field)
            for field in sorted(self._settings.model_fields)
            if field.startswith("feature_")
        }


# ── Module-level singleton + convenience helpers ─────────────────────

_instance: Optional[FeatureFlagService] = None


def get_feature_flags() -> FeatureFlagService:
    """Return (or create) the module-level FeatureFlagService singleton."""
    global _instance
    if _instance is None:
        _instance = FeatureFlagService()
    return _instance


def is_feature_enabled(flag: str) -> bool:
    """Module-level shortcut — ``is_feature_enabled('AGENT_INJURY_IMPACT')``."""
    return get_feature_flags().is_enabled(flag)
