"""G-League Stats Service — Fetch, cache, and translate G-League data.

Fetches player stats from the NBA G-League (LeagueID="20") via nba_api,
calculates per-minute rates, and caches them in PostgreSQL for FPPM
translation when NBA game logs are unavailable.

Translation Tax
---------------
G-League production translates to NBA at approximately 0.75x.  This is
a conservative estimate based on:
  - Lower defensive intensity in the G-League
  - Smaller arenas and weaker competition
  - Pace and minute-inflation differences

The tax is applied once during the cache write (``nba_translated_fppm``)
and consumed directly by the rotation engine's blank-slate fallback.

Usage
-----
    service = GLeagueStatsService(db_pool)

    # Full refresh (called during 4 AM background job)
    await service.refresh_gleague_cache(season="2025-26")

    # Lookup for a single player
    entry = await service.get_player(normalized_name)
    if entry:
        fppm = entry["nba_translated_fppm"]
"""

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.config import get_settings
from app.utils.helpers import normalize_player_name

logger = logging.getLogger(__name__)
settings = get_settings()

# ── Constants ──────────────────────────────────────────────────────────
GLEAGUE_LEAGUE_ID = "20"           # NBA G-League
TRANSLATION_TAX = 0.75             # G-League → NBA efficiency discount
MIN_GLEAGUE_MINUTES = 50.0         # Minimum G-League minutes to trust the data
MIN_GLEAGUE_GAMES = 3              # Minimum games to trust per-minute rates

# DK scoring weights for FPPM calculation
_DK_WEIGHTS = {
    "PTS": 1.0, "REB": 1.25, "AST": 1.5,
    "STL": 2.0, "BLK": 2.0, "TOV": -0.5,
    "FG3M": 0.5,
}


def _calculate_gleague_fppm(row: Dict[str, Any], minutes: float) -> float:
    """Calculate raw G-League FPPM from a stats row.

    Uses DK scoring: PTS + 1.25*REB + 1.5*AST + 2*STL + 2*BLK - 0.5*TOV + 0.5*FG3M
    """
    if minutes <= 0:
        return 0.0

    pts = float(row.get("PTS", 0) or 0)
    reb = float(row.get("REB", 0) or 0)
    ast = float(row.get("AST", 0) or 0)
    stl = float(row.get("STL", 0) or 0)
    blk = float(row.get("BLK", 0) or 0)
    tov = float(row.get("TOV", 0) or 0)
    fg3m = float(row.get("FG3M", 0) or 0)

    total_dk_fp = (
        pts * _DK_WEIGHTS["PTS"]
        + reb * _DK_WEIGHTS["REB"]
        + ast * _DK_WEIGHTS["AST"]
        + stl * _DK_WEIGHTS["STL"]
        + blk * _DK_WEIGHTS["BLK"]
        + tov * _DK_WEIGHTS["TOV"]
        + fg3m * _DK_WEIGHTS["FG3M"]
    )
    return round(total_dk_fp / minutes, 4)


def _per_min_rates(row: Dict[str, Any], minutes: float) -> Dict[str, float]:
    """Extract per-minute stat rates from a totals row."""
    if minutes <= 0:
        return {}
    return {
        "pts_per_min": round(float(row.get("PTS", 0) or 0) / minutes, 4),
        "reb_per_min": round(float(row.get("REB", 0) or 0) / minutes, 4),
        "ast_per_min": round(float(row.get("AST", 0) or 0) / minutes, 4),
        "stl_per_min": round(float(row.get("STL", 0) or 0) / minutes, 4),
        "blk_per_min": round(float(row.get("BLK", 0) or 0) / minutes, 4),
        "tov_per_min": round(float(row.get("TOV", 0) or 0) / minutes, 4),
        "fg3m_per_min": round(float(row.get("FG3M", 0) or 0) / minutes, 4),
    }


class GLeagueStatsService:
    """Fetches G-League stats, computes FPPM, and caches to PostgreSQL."""

    def __init__(self, db_pool=None):
        self._db = db_pool
        # In-memory lookup: normalized_name → cache dict
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._cache_ts: float = 0.0
        self._cache_ttl: float = 3600.0 * 4  # 4-hour in-memory TTL

    # ──────────────────────────────────────────────────────────────
    # PUBLIC: Lookup
    # ──────────────────────────────────────────────────────────────

    def get_player(self, normalized_name: str) -> Optional[Dict[str, Any]]:
        """Return cached G-League data for a player, or None.

        The returned dict includes:
            gleague_fppm, nba_translated_fppm, gleague_minutes,
            gleague_games, pts_per_min, reb_per_min, ..., fg3m_per_min
        """
        return self._cache.get(normalized_name)

    def get_translated_fppm(self, player_name: str) -> Optional[float]:
        """Convenience: normalize name and return NBA-translated FPPM or None."""
        norm = normalize_player_name(player_name)
        entry = self._cache.get(norm)
        if entry and entry.get("gleague_minutes", 0) >= MIN_GLEAGUE_MINUTES:
            return entry["nba_translated_fppm"]
        return None

    def get_translated_rates(self, player_name: str) -> Optional[Dict[str, float]]:
        """Return per-minute rates with translation tax applied, or None."""
        norm = normalize_player_name(player_name)
        entry = self._cache.get(norm)
        if not entry or entry.get("gleague_minutes", 0) < MIN_GLEAGUE_MINUTES:
            return None
        # Apply translation tax to each per-minute rate
        return {
            k: round(v * TRANSLATION_TAX, 4)
            for k, v in entry.items()
            if k.endswith("_per_min") and v is not None
        }

    # ──────────────────────────────────────────────────────────────
    # PUBLIC: Full Refresh (background job)
    # ──────────────────────────────────────────────────────────────

    async def refresh_gleague_cache(
        self, season: Optional[str] = None
    ) -> Dict[str, Any]:
        """Fetch G-League stats from nba_api and upsert into DB + memory cache.

        Called during the 4 AM background refresh alongside NBA data.
        """
        from app.utils.helpers import get_current_nba_season

        _season = season or get_current_nba_season()
        t0 = time.monotonic()
        logger.info("[G-League] Starting refresh for season %s", _season)

        # Fetch from nba_api in a thread (blocking HTTP call)
        try:
            rows = await asyncio.to_thread(self._fetch_gleague_stats, _season)
        except Exception as e:
            logger.error("[G-League] Fetch failed: %s", e)
            return {"status": "error", "error": str(e), "count": 0}

        if not rows:
            logger.warning("[G-League] No data returned for season %s", _season)
            return {"status": "empty", "count": 0}

        # Process rows into cache entries
        entries = []
        for row in rows:
            player_name = row.get("PLAYER_NAME", "")
            if not player_name:
                continue

            minutes = float(row.get("MIN", 0) or 0)
            games = int(row.get("GP", 0) or 0)

            if minutes < MIN_GLEAGUE_MINUTES or games < MIN_GLEAGUE_GAMES:
                continue

            norm_name = normalize_player_name(player_name)
            fppm = _calculate_gleague_fppm(row, minutes)
            rates = _per_min_rates(row, minutes)

            entry = {
                "player_name_normalized": norm_name,
                "player_name_display": player_name,
                "gleague_fppm": fppm,
                "nba_translated_fppm": round(fppm * TRANSLATION_TAX, 4),
                "gleague_minutes": minutes,
                "gleague_games": games,
                "gleague_team": row.get("TEAM_ABBREVIATION", ""),
                "season": _season,
                **rates,
            }
            entries.append(entry)

            logger.debug(
                "[G-League] %s: %.0f min / %d GP | "
                "G-League FPPM=%.3f → NBA FPPM=%.3f",
                player_name, minutes, games,
                fppm, entry["nba_translated_fppm"],
            )

        # Write to DB
        db_count = 0
        if self._db and entries:
            try:
                db_count = await self._upsert_to_db(entries, _season)
            except Exception as e:
                logger.error("[G-League] DB upsert failed: %s", e)

        # Update in-memory cache
        for entry in entries:
            self._cache[entry["player_name_normalized"]] = entry
        self._cache_ts = time.monotonic()

        elapsed = time.monotonic() - t0
        logger.info(
            "[G-League] Refresh complete: %d players cached (%d to DB) in %.1fs",
            len(entries), db_count, elapsed,
        )

        return {
            "status": "ok",
            "count": len(entries),
            "db_count": db_count,
            "elapsed_s": round(elapsed, 1),
        }

    async def load_from_db(self, season: Optional[str] = None) -> int:
        """Load G-League cache from DB into memory (startup / warm-up)."""
        if not self._db:
            return 0

        from app.utils.helpers import get_current_nba_season
        _season = season or get_current_nba_season()

        try:
            async with self._db.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT player_name_normalized, player_name_display,
                           gleague_fppm, nba_translated_fppm, gleague_minutes,
                           gleague_games, gleague_team, season,
                           pts_per_min, reb_per_min, ast_per_min,
                           stl_per_min, blk_per_min, tov_per_min, fg3m_per_min
                    FROM gleague_cache
                    WHERE season = $1
                    """,
                    _season,
                )
                for r in rows:
                    self._cache[r["player_name_normalized"]] = dict(r)
                self._cache_ts = time.monotonic()
                logger.info(
                    "[G-League] Loaded %d entries from DB for season %s",
                    len(rows), _season,
                )
                return len(rows)
        except Exception as e:
            logger.warning("[G-League] DB load failed (table may not exist): %s", e)
            return 0

    # ──────────────────────────────────────────────────────────────
    # PRIVATE: nba_api fetch
    # ──────────────────────────────────────────────────────────────

    def _fetch_gleague_stats(self, season: str) -> List[Dict[str, Any]]:
        """Blocking call to nba_api for G-League player stats.

        Uses LeagueDashPlayerStats with LeagueID="20" (G-League).
        Returns list of dicts with player totals.
        """
        from nba_api.stats.endpoints import leaguedashplayerstats

        logger.info(
            "[G-League] Fetching LeagueDashPlayerStats for season=%s, LeagueID=%s",
            season, GLEAGUE_LEAGUE_ID,
        )

        stats = leaguedashplayerstats.LeagueDashPlayerStats(
            season=season,
            league_id_nullable=GLEAGUE_LEAGUE_ID,
            per_mode_detailed="Totals",
            timeout=settings.nba_api_timeout,
        )

        data = stats.get_normalized_dict()
        rows = data.get("LeagueDashPlayerStats", [])

        logger.info("[G-League] Fetched %d player rows", len(rows))
        return rows

    # ──────────────────────────────────────────────────────────────
    # PRIVATE: DB upsert
    # ──────────────────────────────────────────────────────────────

    async def _upsert_to_db(
        self, entries: List[Dict[str, Any]], season: str
    ) -> int:
        """Upsert G-League cache entries into PostgreSQL."""
        if not self._db:
            return 0

        sql = """
            INSERT INTO gleague_cache (
                player_name_normalized, player_name_display,
                gleague_fppm, nba_translated_fppm, gleague_minutes,
                gleague_games, gleague_team, season,
                pts_per_min, reb_per_min, ast_per_min,
                stl_per_min, blk_per_min, tov_per_min, fg3m_per_min,
                updated_at
            ) VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8,
                $9, $10, $11, $12, $13, $14, $15, $16
            )
            ON CONFLICT (player_name_normalized) DO UPDATE SET
                player_name_display   = EXCLUDED.player_name_display,
                gleague_fppm          = EXCLUDED.gleague_fppm,
                nba_translated_fppm   = EXCLUDED.nba_translated_fppm,
                gleague_minutes       = EXCLUDED.gleague_minutes,
                gleague_games         = EXCLUDED.gleague_games,
                gleague_team          = EXCLUDED.gleague_team,
                season                = EXCLUDED.season,
                pts_per_min           = EXCLUDED.pts_per_min,
                reb_per_min           = EXCLUDED.reb_per_min,
                ast_per_min           = EXCLUDED.ast_per_min,
                stl_per_min           = EXCLUDED.stl_per_min,
                blk_per_min           = EXCLUDED.blk_per_min,
                tov_per_min           = EXCLUDED.tov_per_min,
                fg3m_per_min          = EXCLUDED.fg3m_per_min,
                updated_at            = EXCLUDED.updated_at
        """

        now = datetime.now(timezone.utc)
        count = 0

        async with self._db.acquire() as conn:
            for entry in entries:
                try:
                    await conn.execute(
                        sql,
                        entry["player_name_normalized"],
                        entry["player_name_display"],
                        entry["gleague_fppm"],
                        entry["nba_translated_fppm"],
                        entry["gleague_minutes"],
                        entry["gleague_games"],
                        entry.get("gleague_team", ""),
                        entry["season"],
                        entry.get("pts_per_min"),
                        entry.get("reb_per_min"),
                        entry.get("ast_per_min"),
                        entry.get("stl_per_min"),
                        entry.get("blk_per_min"),
                        entry.get("tov_per_min"),
                        entry.get("fg3m_per_min"),
                        now,
                    )
                    count += 1
                except Exception as e:
                    logger.warning(
                        "[G-League] Upsert failed for %s: %s",
                        entry["player_name_display"], e,
                    )
        return count
