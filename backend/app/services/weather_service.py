"""Sport-agnostic Open-Meteo weather fetcher.

Extracted from ``mlb_weather_service`` in Prompt 7.5 so MLB and NFL
(and any future sport with outdoor games) can share one Open-Meteo
client, one cache, and one parsing path. Each sport's
``<sport>_weather_service.py`` is a thin wrapper that:

  1. Maps ``venue`` → ``{lat, lon, has_roof}`` via its own registry.
  2. Returns :data:`DOME_WEATHER` for closed-roof venues without
     hitting the API.
  3. Delegates outdoor games here via :func:`fetch_weather_at_location`.

Design notes
============
* The cache is keyed on ``(lat, lon, hour_iso)`` so two stadiums at
  different lat/lons never share a cache entry by accident, and a
  single stadium's two consecutive game-times don't collide.
* Cache TTL is 30 min — forecast accuracy doesn't meaningfully change
  inside that window, and a hot scoreboard endpoint hit dozens of
  times during a slate-build session produces zero outbound calls
  after the first.
* The cache is process-local. A multi-worker uvicorn deployment has
  separate caches per worker — inefficient but never broken.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from app.services.http_resilience import APIGroup, resilient_get

logger = logging.getLogger(__name__)


_OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"


# Synthetic payload for closed-roof venues. Domes are climate-controlled
# 72°F with no wind and structurally zero precipitation risk. The
# ``condition: "Dome"`` sentinel is the contract downstream consumers
# (UI rain badge, env-multiplier helpers) use to skip wind / postpone
# math entirely.
DOME_WEATHER: Dict[str, Any] = {
    "temp": 72.0,
    "wind_speed": 0.0,
    "wind_direction": 0.0,
    "precip_prob": 0,
    "condition": "Dome",
}


# Cache TTL — see module docstring for rationale.
_CACHE_TTL_S: float = 30 * 60.0

# Process-local cache. Key: (lat, lon, hour_iso); val: (weather_dict, fetched_at_epoch).
# Threading.RLock because the per-game enrichment fan-out is parallel
# (ThreadPoolExecutor in the per-sport wrappers).
_weather_cache: Dict[Tuple[float, float, str], Tuple[Dict[str, Any], float]] = {}
_cache_lock = threading.RLock()


# ─────────────────────────────────────────────────────────────────────
# Time-parsing helpers
# ─────────────────────────────────────────────────────────────────────


def parse_et_iso_to_hour(game_time_et: Optional[str]) -> Optional[str]:
    """Truncate an ET ISO-8601 timestamp to ``YYYY-MM-DDTHH:00`` form.

    Open-Meteo's hourly arrays are aligned to the top of the hour, so
    we collapse minute/second precision before lookup. Returns ``None``
    when the input is missing / unparseable so the caller can fall
    through to ``weather=None`` rather than crashing.
    """
    if not game_time_et:
        return None
    try:
        # ``game_time_et`` may include a TZ offset like
        # "2026-05-02T19:05:00-04:00". Python's fromisoformat handles
        # both naive and aware variants in 3.11+.
        dt = datetime.fromisoformat(game_time_et)
        return dt.strftime("%Y-%m-%dT%H:00")
    except Exception:
        return None


def closest_hour_index(hours: List[str], target_hour: str) -> Optional[int]:
    """Return the index in ``hours`` closest to ``target_hour``.

    Open-Meteo aligns to the top of every hour and we already truncated
    our target — so 95% of the time this is an exact-match lookup.
    The minute-distance fallback handles edge cases where the API
    emits a half-hour entry (some marine forecasts) or DST transitions
    cause a one-hour drift in the array.
    """
    if not hours:
        return None
    try:
        return hours.index(target_hour)
    except ValueError:
        pass
    try:
        target_dt = datetime.fromisoformat(target_hour)
    except Exception:
        return None
    best_idx: Optional[int] = None
    best_delta_s = float("inf")
    for i, ts in enumerate(hours):
        try:
            dt = datetime.fromisoformat(ts)
        except Exception:
            continue
        delta = abs((dt - target_dt).total_seconds())
        if delta < best_delta_s:
            best_delta_s = delta
            best_idx = i
    return best_idx


# ─────────────────────────────────────────────────────────────────────
# Cache plumbing
# ─────────────────────────────────────────────────────────────────────


def _cache_get(
    lat: float, lon: float, hour_iso: str
) -> Optional[Dict[str, Any]]:
    with _cache_lock:
        entry = _weather_cache.get((lat, lon, hour_iso))
        if entry is None:
            return None
        weather, fetched_at = entry
        if (time.time() - fetched_at) > _CACHE_TTL_S:
            _weather_cache.pop((lat, lon, hour_iso), None)
            return None
        return dict(weather)


def _cache_put(
    lat: float, lon: float, hour_iso: str, weather: Dict[str, Any],
) -> None:
    with _cache_lock:
        _weather_cache[(lat, lon, hour_iso)] = (dict(weather), time.time())


# ─────────────────────────────────────────────────────────────────────
# Open-Meteo HTTP fetch + extraction
# ─────────────────────────────────────────────────────────────────────


def _fetch_open_meteo(
    lat: float, lon: float, target_hour_iso: str,
) -> Optional[Dict[str, Any]]:
    """Hit Open-Meteo and pluck the (temp, wind_speed, wind_direction,
    precip_prob) tuple for ``target_hour_iso`` (in ET).

    Returns ``None`` on any failure — timeout, malformed JSON, missing
    arrays, hour not found. The caller falls back to weather=None.
    """
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": (
            "temperature_2m,windspeed_10m,winddirection_10m,"
            "precipitation_probability"
        ),
        "temperature_unit": "fahrenheit",
        "windspeed_unit": "mph",
        "timezone": "America/New_York",
    }
    try:
        resp = resilient_get(
            _OPEN_METEO_URL,
            group=APIGroup.OPEN_METEO,
            params=params,
        )
        data = resp.json()
    except Exception as exc:
        logger.debug(
            "[Weather] Open-Meteo fetch failed for (%.4f, %.4f): %s",
            lat, lon, exc,
        )
        return None

    hourly = data.get("hourly") or {}
    times = hourly.get("time") or []
    temps = hourly.get("temperature_2m") or []
    speeds = hourly.get("windspeed_10m") or []
    dirs = hourly.get("winddirection_10m") or []
    precips = hourly.get("precipitation_probability") or []
    if not (len(times) == len(temps) == len(speeds) == len(dirs)):
        logger.debug(
            "[Weather] Open-Meteo returned mismatched array lengths "
            "for (%.4f, %.4f): times=%d temps=%d speeds=%d dirs=%d",
            lat, lon, len(times), len(temps), len(speeds), len(dirs),
        )
        return None

    idx = closest_hour_index(times, target_hour_iso)
    if idx is None:
        logger.debug(
            "[Weather] No hourly slot near %s in Open-Meteo response "
            "for (%.4f, %.4f)",
            target_hour_iso, lat, lon,
        )
        return None

    try:
        precip_raw = precips[idx] if idx < len(precips) else 0
        if precip_raw is None:
            precip_pct = 0
        else:
            precip_pct = int(round(float(precip_raw)))
        precip_pct = max(0, min(100, precip_pct))

        return {
            "temp": float(temps[idx]),
            "wind_speed": float(speeds[idx]),
            "wind_direction": float(dirs[idx]),
            "precip_prob": precip_pct,
            "condition": "Outdoor",
        }
    except (TypeError, ValueError) as exc:
        logger.debug(
            "[Weather] Open-Meteo array parse failed at idx=%d: %s", idx, exc,
        )
        return None


# ─────────────────────────────────────────────────────────────────────
# Public sport-agnostic API
# ─────────────────────────────────────────────────────────────────────


def fetch_weather_at_location(
    lat: float, lon: float, game_time_et: Optional[str],
) -> Optional[Dict[str, Any]]:
    """Resolve weather for an outdoor venue at a given ET game time.

    Sport-agnostic: takes raw coordinates (no venue lookup, no dome
    awareness — that's the per-sport wrapper's job). Hits the cache
    first; on miss, calls Open-Meteo, snaps to the closest hour to
    ``game_time_et``, and stores the result.

    Returns ``None`` if the game time can't be parsed or the
    Open-Meteo call fails.
    """
    target_hour = parse_et_iso_to_hour(game_time_et)
    if not target_hour:
        return None

    cached = _cache_get(lat, lon, target_hour)
    if cached is not None:
        return cached

    weather = _fetch_open_meteo(lat, lon, target_hour)
    if weather is not None:
        _cache_put(lat, lon, target_hour, weather)
    return weather


__all__ = [
    "DOME_WEATHER",
    "fetch_weather_at_location",
    "parse_et_iso_to_hour",
    "closest_hour_index",
]
