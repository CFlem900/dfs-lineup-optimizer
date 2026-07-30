"""Centralised CBBpy call wrapper with parallelism throttling.

CBBpy internally uses ``joblib.Parallel(n_jobs=cpu_count()-1)`` which
spawns 15+ sub-processes per ``get_games_team()`` call.  When multiple
callers (prewarm, pool builder, stats service) run concurrently this
creates 50+ Python processes hammering ESPN, causing network saturation,
memory pressure, and rate-limit errors (``'game_day'`` KeyError).

This module provides:
    * ``cbbpy_get_games_team()`` — a drop-in wrapper that:
        1. Limits joblib to 2 workers via ``LOKY_MAX_CPU_COUNT``
        2. Serialises calls through a global ``threading.Lock`` so only
           one CBBpy scrape runs at a time (each call already uses
           internal parallelism)
        3. Applies a per-call timeout via a daemon thread + join()
    * A module-level lock + env-var setup that's applied once on import.
"""

import logging
import os
import threading
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# ── Limit joblib parallelism before it's ever imported ──────────
# ``LOKY_MAX_CPU_COUNT`` caps the n_jobs that loky (joblib's backend) uses.
# Setting this BEFORE cbbpy imports joblib ensures it takes effect.
os.environ["LOKY_MAX_CPU_COUNT"] = "4"

# Also set JOBLIB_START_METHOD to prevent fork-bomb behaviour on Windows
os.environ.setdefault("JOBLIB_START_METHOD", "loky")

# ── Global serialisation lock ──────────────────────────────────
# Only one CBBpy scrape at a time.  Each call already uses 2 internal
# workers, so serialising keeps total sub-process count at 2–3.
_cbbpy_lock = threading.Lock()

# Default per-call timeout (seconds).  CBBpy scraping a 25-game season
# with 2 workers should finish in ~90 s.  Allow 180 s for safety.
_DEFAULT_TIMEOUT = 180


def cbbpy_get_games_team(
    team: str,
    season: int,
    *,
    info: bool = True,
    box: bool = True,
    pbp: bool = False,
    timeout: Optional[float] = None,
) -> Optional[Tuple]:
    """Thread-safe, throttled wrapper around ``ms.get_games_team()``.

    Parameters
    ----------
    team : str
        Team name as CBBpy expects (e.g. ``"Purdue"``).
    season : int
        Season year (e.g. ``2026``).
    info, box, pbp : bool
        Which DataFrames to request (forwarded to CBBpy).
    timeout : float | None
        Max seconds to wait.  Defaults to ``_DEFAULT_TIMEOUT`` (180 s).

    Returns
    -------
    tuple | None
        ``(game_info_df, boxscore_df, pbp_df)`` on success, ``None``
        on error or timeout.
    """
    _timeout = timeout or _DEFAULT_TIMEOUT

    # Acquire the global lock — queue behind any other CBBpy call
    acquired = _cbbpy_lock.acquire(timeout=_timeout)
    if not acquired:
        logger.warning(
            f"CBBpy lock timeout ({_timeout}s) waiting for {team} "
            f"season {season} — another scrape is running"
        )
        return None

    try:
        return _run_with_timeout(team, season, info, box, pbp, _timeout)
    finally:
        _cbbpy_lock.release()


def _run_with_timeout(
    team: str,
    season: int,
    info: bool,
    box: bool,
    pbp: bool,
    timeout: float,
) -> Optional[Tuple]:
    """Run the CBBpy call in a daemon thread with a hard timeout."""
    result_box: list = [None]
    error_box: list = [None]

    def _worker():
        try:
            from cbbpy import mens_scraper as ms
            result_box[0] = ms.get_games_team(
                team, season, info=info, box=box, pbp=pbp,
            )
        except Exception as exc:
            error_box[0] = exc

    t = threading.Thread(target=_worker, daemon=True, name=f"cbbpy-{team}")
    t.start()
    t.join(timeout=timeout)

    if t.is_alive():
        logger.warning(
            f"CBBpy scrape timed out after {timeout}s for {team} "
            f"season {season}"
        )
        return None

    if error_box[0] is not None:
        logger.warning(
            f"CBBpy scrape failed for {team} season {season}: "
            f"{error_box[0]}"
        )
        return None

    return result_box[0]
