"""NFL stadium environmental data + position-aware wind penalty.

Mirrors the MLB ``mlb_park_factors`` module's structural shape but with
NFL-appropriate fields. NFL doesn't have run/HR/pitcher park factors
the way MLB does — what matters for NFL DFS is dome identification
(weather is structurally a no-op indoors) and wind penalty for
high-speed outdoor games.

Schema per venue:
    lat       : decimal degrees, geocoded to the stadium center
    lon       : decimal degrees, west-negative
    has_roof  : True for closed/retractable-roof stadiums. The weather
                pipeline emits a "Dome" sentinel and skips the wind
                penalty when True. There are 10–11 of these in the
                NFL — see the constant block below.

Wind penalty model
==================
Empirical NFL DFS observation: passing and kicking projections crater
once wind exceeds ~15 mph. Running projections are roughly neutral or
slightly elevated (game scripts go run-heavy in wind games). The
:func:`compute_nfl_environmental_multiplier` function encodes the
position-aware penalty:

    Trigger    : wind_speed >= 15 mph AND condition == "Outdoor"
    K / DST    : 0.85   (kicker FG range collapses; DST takes the
                         same bucket per the prompt — points-allowed
                         math actually slightly favors DST in low-
                         scoring weather games but the spec wins)
    QB / WR/TE : 0.92   (deep balls don't carry; YAC opportunities
                         shrink; PPR receiving production drops)
    RB         : 1.02   (game script tilts run-heavy in wind games,
                         goal-line carries become more valuable)
    Other      : 1.00   (e.g. unknown or future positions — neutral)

Domes / no-weather → multiplier = 1.0 always. The dome short-circuit
matters because Open-Meteo would still return wind values for an
indoor stadium (the model doesn't know about the roof) and we'd
otherwise apply a phantom penalty.
"""

from __future__ import annotations

from typing import Dict


# Defensive default for unknown venues / fetch failures. Lat/lon=0 is a
# poison value the weather pipeline already null-checks before doing
# any geocoded lookup, so the bogus coordinates never reach Open-Meteo.
NEUTRAL_NFL_STADIUM_DATA: Dict[str, float | bool] = {
    "lat": 0.0, "lon": 0.0, "has_roof": False,
}


# NFL stadium registry. Keys MUST match the ``venue`` strings that
# ``nfl_game_service`` extracts from ESPN's scoreboard. When ESPN
# ships an alternate spelling (rebrand, sponsor change), add it as
# a duplicate entry rather than mutating the schema, so lookup stays
# O(1). 32 active stadiums + a handful of rebrand aliases + Neutral.
NFL_STADIUM_DATA: Dict[str, Dict[str, float | bool]] = {
    # ── Outdoor, AFC East ────────────────────────────────────────────
    "Highmark Stadium":            {"lat": 42.7738, "lon": -78.7869, "has_roof": False},  # BUF
    "Hard Rock Stadium":           {"lat": 25.9580, "lon": -80.2389, "has_roof": False},  # MIA
    "Gillette Stadium":            {"lat": 42.0909, "lon": -71.2643, "has_roof": False},  # NE
    "MetLife Stadium":             {"lat": 40.8135, "lon": -74.0745, "has_roof": False},  # NYJ + NYG

    # ── Outdoor, AFC North ───────────────────────────────────────────
    "M&T Bank Stadium":            {"lat": 39.2780, "lon": -76.6227, "has_roof": False},  # BAL
    "Paycor Stadium":              {"lat": 39.0954, "lon": -84.5161, "has_roof": False},  # CIN
    "Cleveland Browns Stadium":    {"lat": 41.5061, "lon": -81.6995, "has_roof": False},  # CLE
    "Huntington Bank Field":       {"lat": 41.5061, "lon": -81.6995, "has_roof": False},  # CLE 2024 alias
    "Acrisure Stadium":            {"lat": 40.4468, "lon": -80.0158, "has_roof": False},  # PIT

    # ── Outdoor, AFC South ───────────────────────────────────────────
    "Nissan Stadium":              {"lat": 36.1665, "lon": -86.7713, "has_roof": False},  # TEN
    "EverBank Stadium":            {"lat": 30.3239, "lon": -81.6373, "has_roof": False},  # JAX
    "TIAA Bank Field":             {"lat": 30.3239, "lon": -81.6373, "has_roof": False},  # JAX older alias

    # ── Outdoor, AFC West ────────────────────────────────────────────
    "Empower Field at Mile High":  {"lat": 39.7439, "lon": -105.0201, "has_roof": False}, # DEN
    "Arrowhead Stadium":           {"lat": 39.0489, "lon": -94.4839, "has_roof": False},  # KC
    "GEHA Field at Arrowhead Stadium": {"lat": 39.0489, "lon": -94.4839, "has_roof": False},  # KC alias

    # ── Outdoor, NFC North ───────────────────────────────────────────
    "Lambeau Field":               {"lat": 44.5013, "lon": -88.0622, "has_roof": False},  # GB
    "Soldier Field":               {"lat": 41.8623, "lon": -87.6167, "has_roof": False},  # CHI

    # ── Outdoor, NFC East ────────────────────────────────────────────
    "Lincoln Financial Field":     {"lat": 39.9008, "lon": -75.1675, "has_roof": False},  # PHI
    "FedExField":                  {"lat": 38.9077, "lon": -76.8645, "has_roof": False},  # WAS (older)
    "Northwest Stadium":           {"lat": 38.9077, "lon": -76.8645, "has_roof": False},  # WAS 2024 rebrand
    "Commanders Field":            {"lat": 38.9077, "lon": -76.8645, "has_roof": False},  # WAS alt rebrand

    # ── Outdoor, NFC South ───────────────────────────────────────────
    "Bank of America Stadium":     {"lat": 35.2259, "lon": -80.8528, "has_roof": False},  # CAR
    "Raymond James Stadium":       {"lat": 27.9759, "lon": -82.5033, "has_roof": False},  # TB

    # ── Outdoor, NFC West ────────────────────────────────────────────
    "Levi's Stadium":              {"lat": 37.4032, "lon": -121.9698, "has_roof": False}, # SF
    "Lumen Field":                 {"lat": 47.5952, "lon": -122.3316, "has_roof": False}, # SEA

    # ── Closed / retractable roof (the user's prompt list of 10) ─────
    # SoFi (LAR + LAC) is "indoor" for weather purposes — the roof is
    # always closed to the elements even though it has open sides.
    "Mercedes-Benz Stadium":       {"lat": 33.7553, "lon": -84.4006, "has_roof": True},   # ATL
    "AT&T Stadium":                {"lat": 32.7473, "lon": -97.0945, "has_roof": True},   # DAL
    "U.S. Bank Stadium":           {"lat": 44.9737, "lon": -93.2580, "has_roof": True},   # MIN
    "Lucas Oil Stadium":           {"lat": 39.7601, "lon": -86.1639, "has_roof": True},   # IND
    "Caesars Superdome":           {"lat": 29.9509, "lon": -90.0814, "has_roof": True},   # NO
    "Allegiant Stadium":           {"lat": 36.0909, "lon": -115.1830, "has_roof": True},  # LV
    "SoFi Stadium":                {"lat": 33.9534, "lon": -118.3387, "has_roof": True},  # LAR + LAC
    "State Farm Stadium":          {"lat": 33.5276, "lon": -112.2625, "has_roof": True},  # ARI
    "Ford Field":                  {"lat": 42.3400, "lon": -83.0456, "has_roof": True},   # DET
    "NRG Stadium":                 {"lat": 29.6847, "lon": -95.4107, "has_roof": True},   # HOU

    # ── Neutral fallback (always last for clarity) ───────────────────
    "Neutral":                     dict(NEUTRAL_NFL_STADIUM_DATA),
}


def get_nfl_stadium_data(venue: str | None) -> Dict[str, float | bool]:
    """Look up a venue in :data:`NFL_STADIUM_DATA`.

    Returns a defensive copy. Falls back to :data:`NEUTRAL_NFL_STADIUM_DATA`
    for None / empty / unknown so callers can chain lookups without
    null-checking. Whitespace is stripped before lookup.
    """
    if not venue:
        return dict(NEUTRAL_NFL_STADIUM_DATA)
    rec = NFL_STADIUM_DATA.get(venue.strip())
    if rec is None:
        return dict(NEUTRAL_NFL_STADIUM_DATA)
    return dict(rec)


# ─────────────────────────────────────────────────────────────────────
# Position-aware wind penalty
# ─────────────────────────────────────────────────────────────────────


# Position → bucket map. Pulled out as a module constant so tests can
# verify the classification independently of the multiplier rules.
_NFL_POS_BUCKET: Dict[str, str] = {
    "QB":  "pass",
    "WR":  "pass",
    "TE":  "pass",
    "RB":  "run",
    "K":   "kick",
    "DST": "kick",   # grouped with kicker per the prompt's K/DST spec
}


# Penalty thresholds — kept as module constants so a future tuning
# prompt (or test) can adjust them without hunting through code.
_WIND_PENALTY_MIN_MPH = 15.0
_WIND_MULT_KICK = 0.85
_WIND_MULT_PASS = 0.92
_WIND_MULT_RUN  = 1.02


def _nfl_position_bucket(position: str | None) -> str:
    """Classify a player's primary position into a wind-impact bucket."""
    primary = (position or "").split("/")[0].strip().upper()
    return _NFL_POS_BUCKET.get(primary, "other")


def compute_nfl_environmental_multiplier(
    weather: Dict[str, float | str] | None,
    position: str | None,
) -> float:
    """Position-aware NFL wind penalty.

    Returns a multiplier in ``{0.85, 0.92, 1.0, 1.02}`` based on:

    1. **Triggering condition** — only outdoor games with sustained
       wind ≥ 15 mph qualify. Domes / no-weather / sub-15 mph wind all
       collapse to ``1.0``. This is the key "structural skip" — domes
       in the registry have ``has_roof=True`` and the calling enrichment
       pass should already be passing dome-flagged weather; this
       function is double-defensive and rejects any non-Outdoor payload.
    2. **Position bucket** — ``compute_nfl_environmental_multiplier``
       reads only ``position``; the caller in ``_enrich_pool`` is
       responsible for routing each pool entry through this function
       and stamping the result on ``adjusted_fp``.

    Malformed weather payloads (non-numeric ``wind_speed``, missing
    ``condition`` key) collapse to ``1.0`` rather than raising — the
    weather pipeline is best-effort and a parsing edge case must
    never abort lineup generation.
    """
    if not weather:
        return 1.0
    if weather.get("condition") != "Outdoor":
        return 1.0

    try:
        wind = float(weather.get("wind_speed", 0) or 0)
    except (TypeError, ValueError):
        return 1.0

    if wind < _WIND_PENALTY_MIN_MPH:
        return 1.0

    bucket = _nfl_position_bucket(position)
    if bucket == "kick":
        return _WIND_MULT_KICK
    if bucket == "pass":
        return _WIND_MULT_PASS
    if bucket == "run":
        return _WIND_MULT_RUN
    return 1.0


__all__ = [
    "NFL_STADIUM_DATA",
    "NEUTRAL_NFL_STADIUM_DATA",
    "get_nfl_stadium_data",
    "compute_nfl_environmental_multiplier",
]
