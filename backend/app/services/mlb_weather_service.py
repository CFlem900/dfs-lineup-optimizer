"""MLB-specific weather fetcher.

Thin wrapper around :mod:`app.services.weather_service` (extracted in
Prompt 7.5). This module owns the venue → ``(lat, lon, has_roof)``
lookup against ``MLB_STADIUM_DATA``; the actual Open-Meteo HTTP call,
caching, and time-snapping live in the shared service so NFL / future
sports can reuse the same pipeline without code duplication.

Public API kept stable:
  ``fetch_weather_for_game(venue, game_time_et)`` — single-game lookup
  ``enrich_games_with_weather(games)``            — slate-level fan-out
  ``DOME_WEATHER``                                — re-exported sentinel
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Tuple

from app.services.weather_service import (
    DOME_WEATHER,
    fetch_weather_at_location,
)
from app.sports.mlb_park_factors import MLB_STADIUM_DATA

logger = logging.getLogger(__name__)


def fetch_weather_for_game(
    venue: Optional[str], game_time_et: Optional[str],
) -> Optional[Dict[str, Any]]:
    """Resolve weather for a single MLB game.

    Lookup order (unchanged from Prompt 4.3):
      1. Venue not in registry            → ``None``
      2. Venue ``has_roof=True``          → :data:`DOME_WEATHER` (synthetic)
      3. Outdoor → delegate to ``fetch_weather_at_location`` (cached)
      4. Any failure                      → ``None``
    """
    if not venue:
        return None
    record = MLB_STADIUM_DATA.get(venue.strip())
    if record is None:
        return None
    if record.get("has_roof"):
        # Defensive copy so callers can't mutate the constant.
        return dict(DOME_WEATHER)
    return fetch_weather_at_location(
        lat=float(record["lat"]),
        lon=float(record["lon"]),
        game_time_et=game_time_et,
    )


def enrich_games_with_weather(games: List[Any], max_workers: int = 6) -> None:
    """Mutate each MLB game in-place, attaching :data:`GameInfo.weather`.

    Fans out per-game fetches across a small thread pool so a slow
    Open-Meteo response on one ballpark can't sequentially stall the
    rest. Per-game errors are swallowed — weather is best-effort and
    a slate-render must never fail because the wind feed is down.
    """
    if not games:
        return

    def _worker(game: Any) -> Tuple[Any, Optional[Dict[str, Any]]]:
        try:
            return game, fetch_weather_for_game(
                getattr(game, "venue", None),
                getattr(game, "game_time_et", None),
            )
        except Exception as exc:
            logger.debug(
                "[Weather] Unexpected per-game failure for %s: %s",
                getattr(game, "game_id", "?"), exc,
            )
            return game, None

    with ThreadPoolExecutor(
        max_workers=max_workers, thread_name_prefix="mlb-weather",
    ) as pool:
        for fut in as_completed(pool.submit(_worker, g) for g in games):
            try:
                game, weather = fut.result()
            except Exception:
                continue
            if weather is not None:
                game.weather = weather


__all__ = [
    "DOME_WEATHER",
    "fetch_weather_for_game",
    "enrich_games_with_weather",
]
