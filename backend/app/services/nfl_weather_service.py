"""NFL-specific weather fetcher.

Mirror of :mod:`app.services.mlb_weather_service` for NFL. Both delegate
to the shared :mod:`app.services.weather_service` for the actual
Open-Meteo HTTP call, caching, and parsing — the only thing that
differs between sports is the venue → ``(lat, lon, has_roof)`` lookup,
which uses :data:`NFL_STADIUM_DATA`.

Public API
==========
``fetch_weather_for_game(venue, game_time_et)`` — single-game lookup
``enrich_games_with_weather(games)``            — slate-level fan-out
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Tuple

from app.services.weather_service import (
    DOME_WEATHER,
    fetch_weather_at_location,
)
from app.sports.nfl_park_factors import NFL_STADIUM_DATA

logger = logging.getLogger(__name__)


def fetch_weather_for_game(
    venue: Optional[str], game_time_et: Optional[str],
) -> Optional[Dict[str, Any]]:
    """Resolve weather for a single NFL game.

    Lookup order (mirrors the MLB pipeline):
      1. Venue not in registry            → ``None``
      2. Venue ``has_roof=True``          → :data:`DOME_WEATHER` (synthetic)
      3. Outdoor → delegate to ``fetch_weather_at_location`` (cached)
      4. Any failure                      → ``None``

    The dome short-circuit is structurally important for NFL: ~10
    stadiums have closed/retractable roofs (ATL, DAL, MIN, IND, NO,
    LV, LAR, ARI, DET, HOU; LAC also plays at SoFi). Inside a closed
    roof there's no wind to penalise, no rain to delay, and Open-Meteo
    has no idea about the structure — so we hard-skip the API call.
    """
    if not venue:
        return None
    record = NFL_STADIUM_DATA.get(venue.strip())
    if record is None:
        return None
    if record.get("has_roof"):
        return dict(DOME_WEATHER)
    return fetch_weather_at_location(
        lat=float(record["lat"]),
        lon=float(record["lon"]),
        game_time_et=game_time_et,
    )


def enrich_games_with_weather(games: List[Any], max_workers: int = 6) -> None:
    """Mutate each NFL game in-place, attaching :data:`GameInfo.weather`.

    Same fan-out pattern as the MLB pipeline. Per-game errors are
    swallowed — weather is best-effort, and a slate render must never
    fail because Open-Meteo returned a 5xx for one ballpark.
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
        max_workers=max_workers, thread_name_prefix="nfl-weather",
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
