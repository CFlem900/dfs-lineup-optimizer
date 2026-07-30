"""Sport-config registry.

Single source of truth for per-sport configuration. Consumers should
look up values via ``get_config(sport)`` rather than importing
sport-specific module constants directly. The registry pattern lets new
sports (NFL, MLB, …) plug in by adding a module + a registry entry,
without touching every service that reads sport-specific values.

Adding a new sport:

  1. Create ``app/sports/<code>.py`` exporting ``<CODE>_CONFIG: SportConfig``.
  2. Add a ``"<code>": <CODE>_CONFIG`` entry to ``_REGISTRY`` below.
  3. Add the code to ``SUPPORTED_SPORTS``.

Usage::

    from app.sports import get_config
    cfg = get_config("nba")
    salary_cap = cfg.salary_cap_dk
    slots = cfg.dk_roster_slots
"""

from __future__ import annotations

from typing import Dict, Tuple

from app.sports.base import SportConfig
from app.sports.cbb import CBB_CONFIG
from app.sports.mlb import MLB_CONFIG
from app.sports.nba import NBA_CONFIG
from app.sports.nfl import NFL_CONFIG


# Ordered tuple of supported sport codes. Mirrors the keys in ``_REGISTRY``.
# Tuple-typed so it's hashable and read-only.
SUPPORTED_SPORTS: Tuple[str, ...] = ("nba", "cbb", "nfl", "mlb")


_REGISTRY: Dict[str, SportConfig] = {
    "nba": NBA_CONFIG,
    "cbb": CBB_CONFIG,
    "nfl": NFL_CONFIG,
    "mlb": MLB_CONFIG,
}


def get_config(sport_code: str) -> SportConfig:
    """Return the :class:`SportConfig` for ``sport_code``.

    Parameters
    ----------
    sport_code : str
        Lowercase sport key (e.g. ``"nba"``).

    Raises
    ------
    ValueError
        If the sport code isn't registered or its config is marked
        ``is_active=False``. The error message includes the valid options
        so callers can surface a useful message to users.
    """
    if not isinstance(sport_code, str):
        raise ValueError(
            f"Unsupported sport: {sport_code!r} (must be a string). "
            f"Valid options: {sorted(active_sports())}"
        )
    key = sport_code.lower()
    cfg = _REGISTRY.get(key)
    if cfg is None:
        raise ValueError(
            f"Unsupported sport: {sport_code!r}. "
            f"Valid options: {sorted(active_sports())}"
        )
    if not cfg.is_active:
        raise ValueError(
            f"Sport {sport_code!r} is registered but inactive. "
            f"Active options: {sorted(active_sports())}"
        )
    return cfg


def active_sports() -> Tuple[str, ...]:
    """Return the codes of all currently-active sports."""
    return tuple(code for code, cfg in _REGISTRY.items() if cfg.is_active)


__all__ = ["SportConfig", "get_config", "active_sports", "SUPPORTED_SPORTS"]
