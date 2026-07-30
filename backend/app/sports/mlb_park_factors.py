"""MLB park factors + stadium environmental data.

Single-source-of-truth registry for everything the optimizer + weather
pipeline need to know about an MLB ballpark. Each entry carries:

    lat                   : decimal degrees, geocoded to home plate.
                            Used for Open-Meteo lat/lon lookups.
    lon                   : decimal degrees, west-negative.
    has_roof              : True for closed/retractable-roof parks. The
                            weather pipeline emits a "Dome" flag and
                            skips the wind multiplier when True.
    center_field_heading  : compass bearing (0-359, °) of CF as seen
                            from home plate. Wind blowing FROM home
                            plate TOWARD this heading carries fly balls
                            "out" — the canonical HR boost direction.
    run_factor            : run-environment multiplier (the headline
                            number). Hitter projections are scaled by
                            ``run_factor * wind_mult``.
    hr_factor             : home-run-specific factor. Reserved for a
                            future split-scoring path that distinguishes
                            HR-driven scoring (HR/RBI via slugging) from
                            non-HR scoring (singles + walks). Currently
                            informational only.
    pitcher_factor        : inverse factor for pitchers (≈ 1 / run, but
                            not always — Coors degrades K-rate and IP
                            beyond what the run factor alone implies).

Sourcing notes
==============
Run / HR / pitcher factors come from 3-year rolling Statcast / Baseball
Savant data. Numbers cluster around 1.0 with the canonical extremes
(Coors, GABP, Yankee Stadium on the high side; Petco, Oracle, T-Mobile
on the low side) holding their published positions. Re-tune annually
as parks change dimensions or configurations.

Lat/lon are geocoded to home plate. Center-field headings are
approximate (within ~5° of true). Wrigley's heading is fixed at 45°
per the Prompt 4.2 spec — the real value is closer to 32° but the
spec wins because the test suite asserts it.

History
=======
This file used to hold two parallel dicts (``MLB_PARK_FACTORS`` carrying
``{run, hr, pitcher}`` and ``MLB_STADIUM_DATA`` carrying lat/lon/etc).
They drifted easily and required an explicit drift-detection test.
Prompt 7.1 merged them into the single ``MLB_STADIUM_DATA`` dict below;
``get_park_factor()`` now derives the legacy ``{run, hr, pitcher}``
subset on demand so existing call sites keep working.
"""

from __future__ import annotations

import math
from typing import Dict


# Default for unknown / missing venues — every multiplier is 1.0 so
# callers can multiply unconditionally without checking. Lat/lon set
# to (0, 0) intentionally — the weather pipeline already null-checks
# venue before doing geocoded lookups, so the bogus coordinates are
# never actually queried.
NEUTRAL_STADIUM_DATA: Dict[str, float | bool | int] = {
    "lat": 0.0, "lon": 0.0,
    "has_roof": False,
    "center_field_heading": 0,
    "run_factor": 1.0,
    "hr_factor": 1.0,
    "pitcher_factor": 1.0,
}


# Master registry. Keys MUST match the ``venue`` strings that
# ``mlb_game_service`` extracts from ESPN — which in turn matches the
# ``home_park`` values in ``mlb_data_service.MLB_TEAMS``. When ESPN
# ships an alternate spelling (rebranded park, temporary venue), add
# it as a duplicate entry rather than mutating the schema, so lookup
# stays O(1).
MLB_STADIUM_DATA: Dict[str, Dict[str, float | bool | int]] = {
    # ── Extreme hitter parks ─────────────────────────────────────────
    "Coors Field": {
        "lat": 39.7559, "lon": -104.9942,
        "has_roof": False, "center_field_heading": 0,
        "run_factor": 1.34, "hr_factor": 1.15, "pitcher_factor": 0.75,
    },
    "Great American Ball Park": {
        "lat": 39.0975, "lon": -84.5066,
        "has_roof": False, "center_field_heading": 50,
        "run_factor": 1.10, "hr_factor": 1.20, "pitcher_factor": 0.85,
    },
    "Globe Life Field": {
        "lat": 32.7474, "lon": -97.0846,
        "has_roof": True, "center_field_heading": 50,
        "run_factor": 1.05, "hr_factor": 1.10, "pitcher_factor": 0.90,
    },
    "Yankee Stadium": {
        "lat": 40.8296, "lon": -73.9262,
        "has_roof": False, "center_field_heading": 75,
        "run_factor": 1.05, "hr_factor": 1.18, "pitcher_factor": 0.90,
    },
    "Citizens Bank Park": {
        "lat": 39.9061, "lon": -75.1665,
        "has_roof": False, "center_field_heading": 5,
        "run_factor": 1.05, "hr_factor": 1.10, "pitcher_factor": 0.90,
    },
    "Fenway Park": {
        "lat": 42.3467, "lon": -71.0972,
        "has_roof": False, "center_field_heading": 73,
        "run_factor": 1.08, "hr_factor": 0.95, "pitcher_factor": 0.93,
    },
    "Truist Park": {
        "lat": 33.8908, "lon": -84.4678,
        "has_roof": False, "center_field_heading": 50,
        "run_factor": 1.05, "hr_factor": 1.05, "pitcher_factor": 0.92,
    },
    "Wrigley Field": {
        "lat": 41.9484, "lon": -87.6553,
        "has_roof": False, "center_field_heading": 45,
        "run_factor": 1.02, "hr_factor": 1.05, "pitcher_factor": 0.97,
    },
    "Chase Field": {
        "lat": 33.4453, "lon": -112.0667,
        "has_roof": True, "center_field_heading": 25,
        "run_factor": 1.04, "hr_factor": 1.05, "pitcher_factor": 0.93,
    },
    "Camden Yards": {
        "lat": 39.2839, "lon": -76.6217,
        "has_roof": False, "center_field_heading": 55,
        "run_factor": 1.02, "hr_factor": 1.05, "pitcher_factor": 0.95,
    },
    "Rogers Centre": {
        "lat": 43.6414, "lon": -79.3894,
        "has_roof": True, "center_field_heading": 0,
        "run_factor": 1.02, "hr_factor": 1.05, "pitcher_factor": 0.95,
    },

    # ── Roughly neutral parks ────────────────────────────────────────
    "Minute Maid Park": {
        "lat": 29.7572, "lon": -95.3556,
        "has_roof": True, "center_field_heading": 20,
        "run_factor": 1.00, "hr_factor": 1.05, "pitcher_factor": 0.98,
    },
    "Daikin Park": {  # 2025 HOU rebrand alias
        "lat": 29.7572, "lon": -95.3556,
        "has_roof": True, "center_field_heading": 20,
        "run_factor": 1.00, "hr_factor": 1.05, "pitcher_factor": 0.98,
    },
    "Nationals Park": {
        "lat": 38.8729, "lon": -77.0074,
        "has_roof": False, "center_field_heading": 30,
        "run_factor": 1.00, "hr_factor": 1.00, "pitcher_factor": 1.00,
    },
    "Citi Field": {
        "lat": 40.7571, "lon": -73.8458,
        "has_roof": False, "center_field_heading": 32,
        "run_factor": 0.98, "hr_factor": 0.95, "pitcher_factor": 1.02,
    },
    "PNC Park": {
        "lat": 40.4469, "lon": -80.0057,
        "has_roof": False, "center_field_heading": 50,
        "run_factor": 0.98, "hr_factor": 0.92, "pitcher_factor": 1.03,
    },
    "American Family Field": {
        "lat": 43.0280, "lon": -87.9712,
        "has_roof": True, "center_field_heading": 30,
        "run_factor": 1.00, "hr_factor": 1.05, "pitcher_factor": 0.97,
    },
    "Target Field": {
        "lat": 44.9817, "lon": -93.2776,
        "has_roof": False, "center_field_heading": 50,
        "run_factor": 0.98, "hr_factor": 0.95, "pitcher_factor": 1.02,
    },
    "Progressive Field": {
        "lat": 41.4962, "lon": -81.6852,
        "has_roof": False, "center_field_heading": 70,
        "run_factor": 0.98, "hr_factor": 0.97, "pitcher_factor": 1.02,
    },
    "Comerica Park": {
        "lat": 42.3390, "lon": -83.0485,
        "has_roof": False, "center_field_heading": 50,
        "run_factor": 0.97, "hr_factor": 0.92, "pitcher_factor": 1.03,
    },
    "Kauffman Stadium": {
        "lat": 39.0517, "lon": -94.4803,
        "has_roof": False, "center_field_heading": 50,
        "run_factor": 0.99, "hr_factor": 0.92, "pitcher_factor": 1.02,
    },
    "Angel Stadium": {
        "lat": 33.8003, "lon": -117.8827,
        "has_roof": False, "center_field_heading": 30,
        "run_factor": 1.00, "hr_factor": 1.00, "pitcher_factor": 1.00,
    },
    "loanDepot park": {
        "lat": 25.7781, "lon": -80.2197,
        "has_roof": True, "center_field_heading": 40,
        "run_factor": 0.95, "hr_factor": 0.92, "pitcher_factor": 1.05,
    },
    "Tropicana Field": {
        "lat": 27.7683, "lon": -82.6534,
        "has_roof": True, "center_field_heading": 65,
        "run_factor": 0.95, "hr_factor": 0.95, "pitcher_factor": 1.05,
    },
    "Steinbrenner Field": {  # 2025 TB temporary home (open-air)
        "lat": 27.9799, "lon": -82.5040,
        "has_roof": False, "center_field_heading": 70,
        "run_factor": 0.97, "hr_factor": 1.00, "pitcher_factor": 1.03,
    },
    "Guaranteed Rate Field": {
        "lat": 41.8300, "lon": -87.6338,
        "has_roof": False, "center_field_heading": 35,
        "run_factor": 1.00, "hr_factor": 1.05, "pitcher_factor": 0.98,
    },
    "Rate Field": {  # 2024 CWS rebrand alias
        "lat": 41.8300, "lon": -87.6338,
        "has_roof": False, "center_field_heading": 35,
        "run_factor": 1.00, "hr_factor": 1.05, "pitcher_factor": 0.98,
    },
    "Busch Stadium": {
        "lat": 38.6226, "lon": -90.1928,
        "has_roof": False, "center_field_heading": 50,
        "run_factor": 0.97, "hr_factor": 0.93, "pitcher_factor": 1.03,
    },
    "Sutter Health Park": {  # 2025 OAK temporary home
        "lat": 38.5807, "lon": -121.5132,
        "has_roof": False, "center_field_heading": 45,
        "run_factor": 1.00, "hr_factor": 1.00, "pitcher_factor": 1.00,
    },

    # ── Extreme pitcher parks ────────────────────────────────────────
    "Dodger Stadium": {
        "lat": 34.0739, "lon": -118.2400,
        "has_roof": False, "center_field_heading": 25,
        "run_factor": 0.97, "hr_factor": 1.02, "pitcher_factor": 1.03,
    },
    "Petco Park": {
        "lat": 32.7073, "lon": -117.1566,
        "has_roof": False, "center_field_heading": 0,
        "run_factor": 0.90, "hr_factor": 0.95, "pitcher_factor": 1.10,
    },
    "T-Mobile Park": {
        "lat": 47.5914, "lon": -122.3325,
        "has_roof": True, "center_field_heading": 45,
        "run_factor": 0.92, "hr_factor": 0.92, "pitcher_factor": 1.08,
    },
    "Oracle Park": {
        "lat": 37.7786, "lon": -122.3893,
        "has_roof": False, "center_field_heading": 90,
        "run_factor": 0.93, "hr_factor": 0.85, "pitcher_factor": 1.10,
    },

    # ── Neutral fallback ─────────────────────────────────────────────
    "Neutral": dict(NEUTRAL_STADIUM_DATA),
}


# ─────────────────────────────────────────────────────────────────────
# Public lookup helpers
# ─────────────────────────────────────────────────────────────────────


def get_stadium_data(venue: str | None) -> Dict[str, float | bool | int]:
    """Look up the full environmental record for a venue.

    Returns a defensive copy so callers can't mutate the registry.
    Falls through to :data:`NEUTRAL_STADIUM_DATA` (lat=0, lon=0,
    has_roof=False, all factors=1.0) for None / empty / unknown
    inputs so the caller can chain lookups without null-checking.

    The shape (keys present in every return value) is invariant — even
    the Neutral fallback ships with all seven fields.
    """
    if not venue:
        return dict(NEUTRAL_STADIUM_DATA)
    key = venue.strip()
    record = MLB_STADIUM_DATA.get(key)
    if record is None:
        return dict(NEUTRAL_STADIUM_DATA)
    return dict(record)


def get_park_factor(venue: str | None) -> Dict[str, float]:
    """Legacy three-key view of the unified registry: ``{run, hr, pitcher}``.

    Kept as a thin derivation over :data:`MLB_STADIUM_DATA` so any
    historical caller that imports ``get_park_factor`` keeps working
    without code changes. New code should prefer ``get_stadium_data``
    for the full record.

    Returns a defensive copy. Falls through to ``{1.0, 1.0, 1.0}`` for
    unknown / missing venues.
    """
    rec = get_stadium_data(venue)
    return {
        "run": float(rec["run_factor"]),
        "hr": float(rec["hr_factor"]),
        "pitcher": float(rec["pitcher_factor"]),
    }


# ─────────────────────────────────────────────────────────────────────
# Wind-alignment math
# ─────────────────────────────────────────────────────────────────────


def calculate_wind_multiplier(
    wind_speed: float,
    wind_direction: float,
    stadium_heading: float,
) -> float:
    """Return a scoring multiplier reflecting how the wind aligns with
    the park's center-field axis.

    Both ``wind_direction`` and ``stadium_heading`` are compass bearings
    in degrees (0 = north, 90 = east, …). Convention: ``wind_direction``
    is the direction the wind is BLOWING TOWARD — so a value equal to
    ``stadium_heading`` means the wind is heading from home plate
    toward CF (blowing OUT — fly balls carry farther).

    Algorithm
    ---------
    1. Compute the signed angle difference, normalized to ``[-180, 180]``.
    2. Cosine of the difference (radians):
       - ``+1.0`` → wind blowing straight out (perfectly aligned with CF).
       - ``0.0``  → wind perpendicular (no run-direction effect).
       - ``-1.0`` → wind blowing straight in.
    3. Asymmetric scaling:
       - Tailwind (alignment > 0): ``1.0 + alignment * speed * 0.01``.
       - Headwind (alignment < 0): ``1.0 + alignment * speed * 0.008``.
       The 0.01 vs 0.008 split reflects empirical asymmetry — a 15 mph
       blow-out adds more carry than a 15 mph blow-in subtracts because
       hitters can adjust launch angle into a headwind.

    Returns the multiplier rounded to 3 decimals so downstream caching
    layers see stable keys.
    """
    diff_deg = ((wind_direction - stadium_heading) + 180) % 360 - 180
    alignment = math.cos(math.radians(diff_deg))
    if alignment > 0:
        multiplier = 1.0 + alignment * wind_speed * 0.01
    else:
        multiplier = 1.0 + alignment * wind_speed * 0.008
    return round(multiplier, 3)


# ─────────────────────────────────────────────────────────────────────
# Combined park × weather multiplier (consumed by the optimizer)
# ─────────────────────────────────────────────────────────────────────


def compute_environmental_multiplier(
    venue: str | None,
    weather: Dict[str, float | str] | None,
    position: str | None,
    pos_to_class: Dict[str, str] | None = None,
) -> float:
    """Combine park factor + live wind into a single per-player multiplier.

    Canonical math used by the MLB enrichment pass. Kept here so the
    formula can be unit-tested in isolation without booting the whole
    optimizer pipeline.

    Rules
    -----
    * Pitchers (``pos_to_class[pos] == 'pitcher'``) ride
      ``pitcher_factor`` only. Wind doesn't meaningfully shift K/IP/W
      production at MVP scope.
    * Hitters ride ``run_factor * wind_mult``.
    * ``wind_mult`` collapses to ``1.0`` when:
        - the venue is unknown / Neutral (no heading to project onto)
        - the venue has a closed roof (``has_roof=True``)
        - ``weather`` is ``None`` (Open-Meteo fetch failed)
        - the weather payload's ``condition`` isn't ``"Outdoor"``
        - parsing the wind speed/direction floats raises
      Otherwise it comes from :func:`calculate_wind_multiplier`.
    """
    pos_to_class = pos_to_class or {}
    stadium = get_stadium_data(venue)
    primary_pos = (position or "").split("/")[0].strip().upper()
    cls = pos_to_class.get(primary_pos)

    if cls == "pitcher":
        return float(stadium["pitcher_factor"])

    wind_mult = 1.0
    if (
        not stadium.get("has_roof")
        and weather
        and weather.get("condition") == "Outdoor"
    ):
        try:
            wind_mult = calculate_wind_multiplier(
                wind_speed=float(weather.get("wind_speed", 0) or 0),
                wind_direction=float(weather.get("wind_direction", 0) or 0),
                stadium_heading=float(stadium["center_field_heading"]),
            )
        except (TypeError, ValueError):
            wind_mult = 1.0

    return float(stadium["run_factor"]) * wind_mult


__all__ = [
    "MLB_STADIUM_DATA",
    "NEUTRAL_STADIUM_DATA",
    "get_stadium_data",
    "get_park_factor",
    "calculate_wind_multiplier",
    "compute_environmental_multiplier",
]
