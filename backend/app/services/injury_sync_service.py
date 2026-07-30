"""Async injury synchronisation service — BALLDONTLIE API powered.

Fetches daily NBA injury reports from the BALLDONTLIE REST API, enriches
with DNP decay analysis, upserts to PostgreSQL, and manages cache state
for the LineupOptimizerService via SHA-256 injury hashing.

Pipeline (via ``run_sync()``):

    1. **Fetch** — Paginated GET ``/v1/player_injuries`` via async
       ``httpx.AsyncClient`` with rate-limiting and circuit-breaker
       awareness.
    2. **DNP Decay** — SQL CTE against ``player_minutes_history`` to
       detect players with consecutive 0-minute games.  Forces
       ``effective_factor = 0.00`` at 5+ consecutive DNPs.
    3. **Upsert** — SQLAlchemy async ``INSERT … ON CONFLICT DO UPDATE``
       into ``nba_injuries``.
    4. **SHA-256 Hash** — After upsert, computes a SHA-256 digest of
       active injury statuses.  Compares to the previous hash stored
       in Redis.  If changed, triggers a global cache bust.
    5. **Star Monitor** — Players with baseline >= 26.0 min whose
       status changes are logged for AI Agent 3 (InjuryImpactAgent)
       and published to Redis pub/sub for pre-warm.

Official Factor Table (§1.2):

    | Status       | P(plays) | E[min|plays] | Factor |
    |------------- |----------|-------------- |--------|
    | Out          |    0.00  |         0.00  |  0.00  |
    | Doubtful     |    0.20  |         0.75  |  0.15  |
    | GTD          |    0.72  |         0.92  |  0.66  |
    | Questionable |    0.85  |         0.95  |  0.81  |

DNP Decay (linear over 5 games):

    decay = max(0.0, 1.0 - consecutive_dnps / 5)
    effective_factor = min(injury_factor, dnp_decay)

Usage::

    svc = InjurySyncService()
    result = await svc.run_sync()
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.config.constants import (
    INJURY_MINUTES_IF_ACTIVE,
    INJURY_PLAY_PROBABILITY,
    STAR_ANCHOR_THRESHOLD,
)

logger = logging.getLogger(__name__)

# ============================================================================
# Official Factor Table
# ============================================================================

OFFICIAL_FACTORS: Dict[str, float] = {
    "Out": 0.00,
    "Doubtful": round(
        INJURY_PLAY_PROBABILITY.get("Doubtful", 0.20)
        * INJURY_MINUTES_IF_ACTIVE.get("Doubtful", 0.75),
        2,
    ),  # 0.15
    "GTD": round(
        INJURY_PLAY_PROBABILITY.get("GTD", 0.72)
        * INJURY_MINUTES_IF_ACTIVE.get("GTD", 0.92),
        2,
    ),  # 0.66
    "Game Time Decision": round(
        INJURY_PLAY_PROBABILITY.get("Game Time Decision", 0.72)
        * INJURY_MINUTES_IF_ACTIVE.get("Game Time Decision", 0.92),
        2,
    ),  # 0.66
    "Questionable": round(
        INJURY_PLAY_PROBABILITY.get("Questionable", 0.85)
        * INJURY_MINUTES_IF_ACTIVE.get("Questionable", 0.95),
        2,
    ),  # 0.81
    "Available": 1.00,
}

# ============================================================================
# DNP Decay Model
# ============================================================================

DNP_WINDOW: int = 5
DNP_AUTO_OUT_THRESHOLD: int = 5


def _dnp_decay_factor(consecutive_dnps: int) -> float:
    """Linear decay: 0 DNPs → 1.00, 5+ → 0.00."""
    if consecutive_dnps >= DNP_AUTO_OUT_THRESHOLD:
        return 0.0
    return round(max(0.0, 1.0 - (consecutive_dnps / DNP_AUTO_OUT_THRESHOLD)), 2)


def _dnp_label(consecutive_dnps: int) -> str:
    """Human-readable DNP decay label for the reason column."""
    if consecutive_dnps >= DNP_AUTO_OUT_THRESHOLD:
        return "DNP-CD/AUTO-OUT"
    if consecutive_dnps > 0:
        return f"DNP-CD/{consecutive_dnps}"
    return ""


# ============================================================================
# Status Normalisation
# ============================================================================

_STATUS_MAP: Dict[str, str] = {
    "out": "Out",
    "doubtful": "Doubtful",
    "questionable": "Questionable",
    "gtd": "GTD",
    "game time decision": "GTD",
    "day-to-day": "GTD",
    "day to day": "GTD",
    "probable": "Available",
    "available": "Available",
    "not yet submitted": "Questionable",
}

_VALID_STATUSES = set(_STATUS_MAP.values())


def _normalise_status(raw: str) -> str:
    """Map raw BDL status text to a canonical category."""
    lower = raw.lower().strip()
    mapped = _STATUS_MAP.get(lower)
    if mapped:
        return mapped
    if raw in _VALID_STATUSES:
        return raw
    return "Questionable"


# ============================================================================
# Redis Constants
# ============================================================================

_REDIS_INJURY_HASH_KEY = "injury_sync:hash"
_REDIS_INJURY_HASH_TTL = 3600
_REDIS_PREWARM_CHANNEL = "rotation_engine_updates"

# ============================================================================
# BDL Team Mapping (BDL ID → abbreviation, verified Feb 2026)
# ============================================================================

_BDL_TEAMS: Dict[int, str] = {
    1: "ATL", 2: "BOS", 3: "BKN", 4: "CHA", 5: "CHI",
    6: "CLE", 7: "DAL", 8: "DEN", 9: "DET", 10: "GSW",
    11: "HOU", 12: "IND", 13: "LAC", 14: "LAL", 15: "MEM",
    16: "MIA", 17: "MIL", 18: "MIN", 19: "NOP", 20: "NYK",
    21: "OKC", 22: "ORL", 23: "PHI", 24: "PHX", 25: "POR",
    26: "SAC", 27: "SAS", 28: "TOR", 29: "UTA", 30: "WAS",
}

# ============================================================================
# SQL — DNP Streak CTE (runs against player_minutes_history)
# ============================================================================

_DNP_CTE_SQL = text("""
WITH ranked_games AS (
    SELECT
        player_name,
        actual_minutes,
        ROW_NUMBER() OVER (
            PARTITION BY player_name
            ORDER BY game_date DESC
        ) AS rn
    FROM player_minutes_history
    WHERE sport = 'nba'
),
last_n AS (
    SELECT player_name, actual_minutes, rn
    FROM ranked_games
    WHERE rn <= :window
),
first_played AS (
    SELECT player_name, MIN(rn) AS first_played_rn
    FROM last_n
    WHERE actual_minutes > 0
    GROUP BY player_name
),
dnp_counts AS (
    SELECT
        d.player_name,
        COALESCE(fp.first_played_rn - 1, :window) AS consecutive_dnps
    FROM (SELECT DISTINCT player_name FROM last_n) d
    LEFT JOIN first_played fp ON fp.player_name = d.player_name
)
SELECT player_name, consecutive_dnps
FROM dnp_counts
WHERE consecutive_dnps > 0
ORDER BY consecutive_dnps DESC;
""")

# ============================================================================
# SQL — Star Baselines (season-average minutes for change detection)
# ============================================================================

_STAR_BASELINES_SQL = text("""
SELECT player_name, AVG(actual_minutes) AS avg_min, COUNT(*) AS gp
FROM player_minutes_history
WHERE sport = 'nba'
  AND actual_minutes IS NOT NULL
GROUP BY player_name
HAVING COUNT(*) >= 10;
""")

# ============================================================================
# SQL — Read previous state (for star-change detection)
# ============================================================================

_SELECT_PREVIOUS_SQL = text("""
SELECT player_name, injury_status, effective_factor
FROM nba_injuries;
""")

# ============================================================================
# SQL — Read active injuries for SHA-256 hash
# ============================================================================

_SELECT_ACTIVE_FOR_HASH_SQL = text("""
SELECT player_name, injury_status, effective_factor
FROM nba_injuries
WHERE injury_status != 'Available'
  AND effective_factor < 1.0
ORDER BY player_name;
""")

# ============================================================================
# SQL — Upsert
# ============================================================================

_UPSERT_SQL = text("""
INSERT INTO nba_injuries (
    player_name, team_name, team_id, injury_status, official_status,
    effective_factor, reason, consecutive_dnp_count, last_updated
)
VALUES (
    :player_name, :team_name, :team_id, :injury_status,
    :official_status, :effective_factor, :reason,
    :consecutive_dnp_count, :last_updated
)
ON CONFLICT (player_name) DO UPDATE SET
    team_name             = EXCLUDED.team_name,
    team_id               = EXCLUDED.team_id,
    injury_status         = EXCLUDED.injury_status,
    official_status       = EXCLUDED.official_status,
    effective_factor      = EXCLUDED.effective_factor,
    reason                = EXCLUDED.reason,
    consecutive_dnp_count = EXCLUDED.consecutive_dnp_count,
    last_updated          = EXCLUDED.last_updated;
""")


# ============================================================================
# InjurySyncService
# ============================================================================


class InjurySyncService:
    """Async injury sync service powered by the BALLDONTLIE REST API.

    Designed to run as a periodic background task (e.g. every 10 min via
    FastAPI ``BackgroundTasks`` or APScheduler).  All I/O is async —
    HTTP via httpx, DB via SQLAlchemy async, Redis via aioredis/redis.

    Args:
        cache_service: Optional async CacheService for Redis operations.
            If not provided, Redis operations are skipped gracefully.
    """

    BDL_BASE_URL = "https://api.balldontlie.io/v1"
    BDL_MAX_PAGES = 10
    BDL_PER_PAGE = 100
    BDL_TIMEOUT = 15.0

    def __init__(self, cache_service: Optional[Any] = None):
        _settings = get_settings()
        self._api_key: str = _settings.balldontlie_api_key
        self._cache_service = cache_service

        # Lazy-loaded team mapping: BDL team_id → (nba_team_id, full_name, abbr)
        self._team_map: Optional[Dict[int, Tuple[int, str, str]]] = None

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def is_available(self) -> bool:
        """True if a BDL API key is configured."""
        return bool(self._api_key)

    # ------------------------------------------------------------------
    # Team ID Resolution
    # ------------------------------------------------------------------

    def _ensure_team_map(self) -> Dict[int, Tuple[int, str, str]]:
        """Build BDL team_id → (nba_team_id, full_name, abbreviation).

        Uses nba_api static data for NBA.com IDs.  Falls back to
        abbreviation-only mapping if nba_api is unavailable.
        """
        if self._team_map is not None:
            return self._team_map

        mapping: Dict[int, Tuple[int, str, str]] = {}

        try:
            from nba_api.stats.static import teams as nba_teams_static

            nba_by_abbr: Dict[str, Tuple[int, str]] = {}
            for t in nba_teams_static.get_teams():
                nba_by_abbr[t["abbreviation"].upper()] = (
                    t["id"],
                    t["full_name"],
                )

            for bdl_id, abbr in _BDL_TEAMS.items():
                nba_info = nba_by_abbr.get(abbr)
                if nba_info:
                    mapping[bdl_id] = (nba_info[0], nba_info[1], abbr)
                else:
                    mapping[bdl_id] = (0, abbr, abbr)

        except Exception as exc:
            logger.warning(
                "[InjurySyncService] nba_api unavailable for team mapping: %s — "
                "falling back to abbreviation-only map",
                exc,
            )
            for bdl_id, abbr in _BDL_TEAMS.items():
                mapping[bdl_id] = (0, abbr, abbr)

        self._team_map = mapping
        logger.info(
            "[InjurySyncService] Team mapping loaded: %d teams", len(mapping)
        )
        return mapping

    # ------------------------------------------------------------------
    # BDL API — Fetch Injuries (async with pagination)
    # ------------------------------------------------------------------

    async def _fetch_injuries(self) -> List[Dict[str, Any]]:
        """Fetch all active injuries from BALLDONTLIE /v1/player_injuries.

        Returns a list of enriched dicts ready for upsert::

            {
                "player_name": "LeBron James",
                "team_name": "Los Angeles Lakers",
                "team_id": 1610612747,          # nba_api team ID
                "injury_status": "Questionable", # normalised
                "official_status": "Questionable", # raw from BDL
                "reason": "Left ankle soreness",
            }

        Paginates via cursor.  Respects BDL per_page=100 max.
        Handles HTTP errors, timeouts, and unexpected JSON gracefully.
        """
        if not self._api_key:
            logger.warning(
                "[InjurySyncService] No BALLDONTLIE_API_KEY configured — "
                "cannot fetch injuries"
            )
            return []

        team_map = self._ensure_team_map()
        headers = {"Authorization": self._api_key}
        all_injuries: List[Dict[str, Any]] = []
        cursor: Optional[int] = None

        try:
            async with httpx.AsyncClient(timeout=self.BDL_TIMEOUT) as client:
                for page in range(self.BDL_MAX_PAGES):
                    url = (
                        f"{self.BDL_BASE_URL}/player_injuries"
                        f"?per_page={self.BDL_PER_PAGE}"
                    )
                    if cursor is not None:
                        url += f"&cursor={cursor}"

                    resp = await client.get(url, headers=headers)
                    resp.raise_for_status()

                    try:
                        body = resp.json()
                    except (ValueError, KeyError) as parse_err:
                        logger.error(
                            "[InjurySyncService] JSON parse failed on page %d: %s",
                            page + 1, parse_err,
                        )
                        break

                    data = body.get("data")
                    if not isinstance(data, list):
                        logger.error(
                            "[InjurySyncService] Unexpected response shape on "
                            "page %d — 'data' is %s, expected list",
                            page + 1, type(data).__name__,
                        )
                        break

                    for entry in data:
                        try:
                            row = self._parse_bdl_entry(entry, team_map)
                            if row is not None:
                                all_injuries.append(row)
                        except Exception as entry_err:
                            logger.warning(
                                "[InjurySyncService] Skipping malformed entry: %s",
                                entry_err,
                            )
                            continue

                    # Cursor-based pagination
                    meta = body.get("meta") or {}
                    cursor = meta.get("next_cursor")
                    if cursor is None:
                        break

                    logger.debug(
                        "[InjurySyncService] Page %d: %d entries, "
                        "next_cursor=%s",
                        page + 1, len(data), cursor,
                    )

        except httpx.HTTPStatusError as exc:
            logger.error(
                "[InjurySyncService] BDL API HTTP %d: %s",
                exc.response.status_code, exc,
            )
        except httpx.TimeoutException:
            logger.error(
                "[InjurySyncService] BDL API timed out (%.0fs)",
                self.BDL_TIMEOUT,
            )
        except httpx.ConnectError as exc:
            logger.error(
                "[InjurySyncService] BDL API connection failed: %s", exc
            )
        except Exception as exc:
            logger.error(
                "[InjurySyncService] BDL API unexpected error: %s", exc
            )

        logger.info(
            "[InjurySyncService] Fetched %d injuries from BALLDONTLIE API",
            len(all_injuries),
        )
        return all_injuries

    @staticmethod
    def _parse_bdl_entry(
        entry: Dict[str, Any],
        team_map: Dict[int, Tuple[int, str, str]],
    ) -> Optional[Dict[str, Any]]:
        """Parse a single BDL player_injuries entry into our internal format.

        Expected BDL shape::

            {
                "player": {
                    "id": 123,
                    "first_name": "LeBron",
                    "last_name": "James",
                    "team_id": 14
                },
                "status": "Questionable",
                "description": "Left ankle soreness",
                "return_date": "2026-02-28"
            }

        Returns None if the entry is missing required fields.
        """
        player = entry.get("player")
        if not isinstance(player, dict):
            return None

        first = (player.get("first_name") or "").strip()
        last = (player.get("last_name") or "").strip()
        player_name = f"{first} {last}".strip()
        if not player_name:
            return None

        bdl_team_id = player.get("team_id")
        team_info = team_map.get(bdl_team_id, (None, "", ""))

        raw_status = entry.get("status") or "Questionable"
        normalised = _normalise_status(raw_status)

        return {
            "player_name": player_name,
            "team_name": team_info[1],  # full_name or abbreviation
            "team_id": team_info[0],    # nba_api team ID or None
            "injury_status": normalised,
            "official_status": raw_status,
            "reason": entry.get("description") or "",
        }

    # ------------------------------------------------------------------
    # Database — DNP Streak Query
    # ------------------------------------------------------------------

    @staticmethod
    async def _query_dnp_streaks(session: AsyncSession) -> Dict[str, int]:
        """Execute the DNP CTE against player_minutes_history.

        Returns {player_name: consecutive_dnps} for players with DNPs > 0.
        """
        streaks: Dict[str, int] = {}
        try:
            result = await session.execute(
                _DNP_CTE_SQL, {"window": DNP_WINDOW}
            )
            for row in result.fetchall():
                streaks[row[0]] = int(row[1])
        except Exception as exc:
            logger.warning(
                "[InjurySyncService] DNP streak query failed "
                "(expected on first run): %s",
                exc,
            )
        return streaks

    # ------------------------------------------------------------------
    # Database — Star Baselines
    # ------------------------------------------------------------------

    @staticmethod
    async def _fetch_star_baselines(
        session: AsyncSession,
    ) -> Dict[str, float]:
        """Query season-average minutes for all players (>= 10 GP)."""
        baselines: Dict[str, float] = {}
        try:
            result = await session.execute(_STAR_BASELINES_SQL)
            for row in result.fetchall():
                baselines[row[0]] = round(float(row[1]), 1)
        except Exception as exc:
            logger.warning(
                "[InjurySyncService] Star baselines query failed: %s", exc
            )
        return baselines

    # ------------------------------------------------------------------
    # Database — Previous State (for change detection)
    # ------------------------------------------------------------------

    @staticmethod
    async def _read_previous_state(
        session: AsyncSession,
    ) -> Dict[str, Tuple[str, float]]:
        """Snapshot current DB state before upsert for delta detection."""
        state: Dict[str, Tuple[str, float]] = {}
        try:
            result = await session.execute(_SELECT_PREVIOUS_SQL)
            for row in result.fetchall():
                state[row[0]] = (
                    row[1],
                    float(row[2]) if row[2] is not None else 1.0,
                )
        except Exception as exc:
            logger.debug(
                "[InjurySyncService] No previous state (first run?): %s", exc
            )
        return state

    # ------------------------------------------------------------------
    # Database — Upsert
    # ------------------------------------------------------------------

    @staticmethod
    async def _upsert_injuries(
        session: AsyncSession,
        rows: List[Dict[str, Any]],
    ) -> int:
        """Batch upsert enriched injury rows into nba_injuries.

        Returns the number of rows upserted.
        """
        count = 0
        for row in rows:
            await session.execute(_UPSERT_SQL, row)
            count += 1
        return count

    # ------------------------------------------------------------------
    # SHA-256 Injury Hash
    # ------------------------------------------------------------------

    @staticmethod
    async def _compute_injury_hash(session: AsyncSession) -> str:
        """Generate a SHA-256 hash of all active (non-Available) injuries."""
        try:
            result = await session.execute(_SELECT_ACTIVE_FOR_HASH_SQL)
            rows = result.fetchall()
        except Exception as exc:
            logger.warning(
                "[InjurySyncService] Failed to read injuries for hash: %s", exc
            )
            return ""

        if not rows:
            return hashlib.sha256(b"NO_ACTIVE_INJURIES").hexdigest()

        parts = [f"{r[0]}:{r[1]}:{r[2]:.2f}" for r in rows]
        payload = "\n".join(parts).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    # ------------------------------------------------------------------
    # Redis — Hash Comparison + Cache Bust
    # ------------------------------------------------------------------

    async def _compare_and_bust(self, new_hash: str) -> Tuple[bool, int]:
        """Compare new_hash to Redis, bust caches if changed.

        Returns (hash_changed: bool, keys_cleared: int).
        """
        cache = self._cache_service
        if cache is None:
            logger.debug(
                "[InjurySyncService] No CacheService — assuming hash changed"
            )
            return True, 0

        # Read previous hash
        previous_hash: Optional[str] = None
        try:
            previous_hash = await cache.get(_REDIS_INJURY_HASH_KEY)
        except Exception as exc:
            logger.warning(
                "[InjurySyncService] Redis GET failed for injury hash: %s", exc
            )

        changed = previous_hash != new_hash

        # Store new hash
        try:
            await cache.set(
                _REDIS_INJURY_HASH_KEY, new_hash, ttl=_REDIS_INJURY_HASH_TTL
            )
        except Exception as exc:
            logger.warning(
                "[InjurySyncService] Redis SET failed for injury hash: %s", exc
            )

        if not changed:
            logger.debug(
                "[InjurySyncService] Injury hash unchanged: %s…",
                new_hash[:16],
            )
            return False, 0

        logger.info(
            "[InjurySyncService] Injury hash CHANGED: %s… → %s…",
            (previous_hash or "<none>")[:16],
            new_hash[:16],
        )

        # Bust caches
        cleared = 0
        patterns = ["injury:*", "pool:*", "slate:*", "rotation:*"]
        for pattern in patterns:
            try:
                cleared += await cache.clear_pattern(pattern)
            except Exception as exc:
                logger.warning(
                    "[InjurySyncService] Redis pattern clear '%s' failed: %s",
                    pattern, exc,
                )

        # Also bust the in-process LineupOptimizerService caches
        try:
            from app.services.lineup_optimizer_service import (
                clear_optimizer_cache,
            )
            cleared += clear_optimizer_cache()
        except ImportError:
            pass
        except Exception as exc:
            logger.warning(
                "[InjurySyncService] In-process optimizer cache clear "
                "failed: %s",
                exc,
            )

        logger.info(
            "[InjurySyncService] Cache bust complete: %d entries cleared", cleared
        )
        return True, cleared

    # ------------------------------------------------------------------
    # Redis — Star Player Pub/Sub Pre-Warm
    # ------------------------------------------------------------------

    async def _publish_star_changes(
        self,
        star_changes: List[Dict[str, Any]],
    ) -> int:
        """PUBLISH star-player change events to the pre-warm channel."""
        cache = self._cache_service
        if cache is None or not star_changes:
            return 0

        published = 0
        for change in star_changes:
            payload = {
                "event": "star_injury_change",
                "player_name": change["player_name"],
                "team_name": change.get("team_name", ""),
                "team_id": change.get("team_id"),
                "baseline_minutes": change.get("baseline_minutes", 0.0),
                "old_status": change.get("old_status", "Available"),
                "new_status": change.get("new_status", ""),
                "old_factor": change.get("old_factor", 1.0),
                "new_factor": change.get("new_factor", 0.0),
                "severity": change.get("severity", "unknown"),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            try:
                await cache.publish(
                    _REDIS_PREWARM_CHANNEL,
                    json.dumps(payload, default=str),
                )
                published += 1
                logger.warning(
                    "[PREWARM-PUBLISH] %s | %s (%s) | %s → %s | "
                    "baseline %.1f min",
                    change.get("severity", "?").upper(),
                    change["player_name"],
                    change.get("team_name", "?"),
                    change.get("old_status", "?"),
                    change.get("new_status", "?"),
                    change.get("baseline_minutes", 0),
                )
            except Exception as exc:
                logger.error(
                    "[PREWARM-PUBLISH] Failed for %s: %s",
                    change["player_name"],
                    exc,
                )

        return published

    # ------------------------------------------------------------------
    # Star Change Detection
    # ------------------------------------------------------------------

    @staticmethod
    def _detect_star_changes(
        previous: Dict[str, Tuple[str, float]],
        current_rows: List[Dict[str, Any]],
        baselines: Dict[str, float],
    ) -> List[Dict[str, Any]]:
        """Compare previous vs current state for star players.

        A "star" is any player with season-average baseline minutes >=
        STAR_ANCHOR_THRESHOLD (26.0).
        """
        changes: List[Dict[str, Any]] = []

        for row in current_rows:
            pname = row["player_name"]
            baseline = baselines.get(pname, 0.0)

            if baseline < STAR_ANCHOR_THRESHOLD:
                continue

            new_status = row["injury_status"]
            new_factor = row["effective_factor"]

            if pname in previous:
                old_status, old_factor = previous[pname]
                if (
                    old_status == new_status
                    and abs(old_factor - new_factor) < 0.01
                ):
                    continue
                severity = (
                    "escalation" if new_factor < old_factor else "de-escalation"
                )
            else:
                old_status = "Available"
                old_factor = 1.0
                if new_status == "Available" and new_factor >= 1.0:
                    continue
                severity = "new_injury"

            changes.append({
                "player_name": pname,
                "team_name": row.get("team_name", ""),
                "team_id": row.get("team_id"),
                "baseline_minutes": baseline,
                "old_status": old_status,
                "new_status": new_status,
                "old_factor": old_factor,
                "new_factor": new_factor,
                "severity": severity,
            })

        return changes

    # ------------------------------------------------------------------
    # Main Sync Pipeline
    # ------------------------------------------------------------------

    async def run_sync(self, session: AsyncSession) -> Dict[str, Any]:
        """Execute the full injury sync pipeline.

        Steps:
            1. Snapshot previous DB state for change detection.
            2. Fetch current injuries from BDL API.
            3. Query DNP streaks via CTE.
            4. Fetch star baselines.
            5. Enrich rows (effective_factor = min(injury, dnp_decay)).
            6. Upsert into nba_injuries.
            7. Compute SHA-256 hash of post-upsert state.
            8. Compare hash to Redis — bust caches if changed.
            9. Detect star changes → publish pre-warm events.

        Args:
            session: An active SQLAlchemy AsyncSession.

        Returns:
            Summary dict with counts, hash, star changes, etc.
        """
        now = datetime.now(timezone.utc)

        # Step 1: Snapshot previous state
        previous_state = await self._read_previous_state(session)

        # Step 2: Fetch from BDL API
        injury_rows = await self._fetch_injuries()
        if not injury_rows:
            return {
                "upserted": 0,
                "dnp_decayed": 0,
                "star_changes": [],
                "injury_hash": "",
                "hash_changed": False,
                "optimizer_caches_cleared": 0,
                "prewarm_published": 0,
                "message": "No injury data from BDL API",
                "timestamp": now.isoformat(),
            }

        # Step 3: DNP streaks
        dnp_streaks = await self._query_dnp_streaks(session)

        # Step 4: Star baselines
        star_baselines = await self._fetch_star_baselines(session)

        # Step 5: Enrich with effective_factor + DNP decay
        enriched_rows: List[Dict[str, Any]] = []
        dnp_decayed = 0

        for row in injury_rows:
            player_name = row["player_name"]
            raw_status = row.get("official_status", "Questionable")
            normalised = row.get("injury_status", "Questionable")
            reason = row.get("reason", "")

            injury_factor = OFFICIAL_FACTORS.get(
                normalised,
                OFFICIAL_FACTORS.get("Questionable", 0.81),
            )

            consecutive_dnps = dnp_streaks.get(player_name, 0)
            decay = _dnp_decay_factor(consecutive_dnps)
            effective = round(min(injury_factor, decay), 2)

            # Annotate reason with DNP info
            tag = _dnp_label(consecutive_dnps)
            if tag and decay < injury_factor:
                enriched_reason = (
                    f"{reason} [{tag}: {consecutive_dnps} consecutive DNPs, "
                    f"decay={decay:.2f} overrides "
                    f"injury factor={injury_factor:.2f}]"
                ).strip()
                dnp_decayed += 1
            elif tag:
                enriched_reason = (
                    f"{reason} [{tag}: {consecutive_dnps} "
                    f"consecutive DNPs]"
                ).strip()
            else:
                enriched_reason = reason

            params = {
                "player_name": player_name,
                "team_name": row.get("team_name", ""),
                "team_id": row.get("team_id"),
                "injury_status": normalised,
                "official_status": raw_status,
                "effective_factor": effective,
                "reason": enriched_reason,
                "consecutive_dnp_count": consecutive_dnps,
                "last_updated": now,
            }
            enriched_rows.append(params)

        # Step 6: Upsert
        upserted = await self._upsert_injuries(session, enriched_rows)
        await session.commit()

        # Step 7: SHA-256 hash
        injury_hash = await self._compute_injury_hash(session)

        # Step 8: Compare hash + bust caches
        hash_changed, optimizer_cleared = await self._compare_and_bust(
            injury_hash
        )

        # Step 9: Star change detection + pub/sub
        star_changes = self._detect_star_changes(
            previous_state, enriched_rows, star_baselines
        )

        prewarm_published = 0
        if star_changes and hash_changed:
            prewarm_published = await self._publish_star_changes(star_changes)

        for sc in star_changes:
            logger.warning(
                "[STAR-INJURY-CHANGE] %s | %s (%s) | %s → %s | "
                "factor: %.2f → %.2f | baseline: %.1f min",
                sc["severity"].upper(),
                sc["player_name"],
                sc.get("team_name", "?"),
                sc["old_status"],
                sc["new_status"],
                sc["old_factor"],
                sc["new_factor"],
                sc["baseline_minutes"],
            )

        result = {
            "upserted": upserted,
            "dnp_decayed": dnp_decayed,
            "star_changes": star_changes,
            "injury_hash": injury_hash,
            "hash_changed": hash_changed,
            "optimizer_caches_cleared": optimizer_cleared,
            "prewarm_published": prewarm_published,
            "timestamp": now.isoformat(),
        }

        logger.info(
            "[InjurySyncService] Sync complete: %d upserted, %d DNP-decayed, "
            "%d star changes, hash_changed=%s, optimizer_cleared=%d, "
            "prewarm_published=%d",
            upserted,
            dnp_decayed,
            len(star_changes),
            hash_changed,
            optimizer_cleared,
            prewarm_published,
        )
        return result

    # ------------------------------------------------------------------
    # Static Helpers (public — for external callers)
    # ------------------------------------------------------------------

    @staticmethod
    def get_official_factor(status: str) -> float:
        """Return the expected-minutes factor for an injury status."""
        return OFFICIAL_FACTORS.get(
            status, OFFICIAL_FACTORS.get("Questionable", 0.81)
        )

    @staticmethod
    def get_all_factors() -> Dict[str, float]:
        """Return the full Official Factor lookup table."""
        return dict(OFFICIAL_FACTORS)

    @staticmethod
    def get_dnp_decay(consecutive_dnps: int) -> float:
        """Compute the DNP decay factor for a given streak length."""
        return _dnp_decay_factor(consecutive_dnps)

    @staticmethod
    def get_dnp_label(consecutive_dnps: int) -> str:
        """Return the human-readable DNP decay label."""
        return _dnp_label(consecutive_dnps)


# ============================================================================
# CLI Entrypoint
# ============================================================================

async def _cli_run_sync() -> None:
    """Standalone CLI runner for the sync pipeline."""
    from app.db.database import init_db, get_session

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )

    db_ok = await init_db()
    if not db_ok:
        print("ERROR: Cannot connect to PostgreSQL (check DATABASE_URL)")
        return

    svc = InjurySyncService()

    async with get_session() as session:
        result = await svc.run_sync(session)

    if "error" in result:
        print(f"ERROR: {result['error']}")
        return

    print(f"{'=' * 60}")
    print("Injury Sync Complete")
    print(f"{'=' * 60}")
    print(f"  Upserted:          {result['upserted']}")
    print(f"  DNP-Decayed:       {result['dnp_decayed']}")
    print(f"  Star Changes:      {len(result.get('star_changes', []))}")
    print(f"  Hash Changed:      {result.get('hash_changed', False)}")
    print(f"  Optimizer Cleared:  {result.get('optimizer_caches_cleared', 0)}")
    print(f"  PreWarm Published:  {result.get('prewarm_published', 0)}")
    print(f"  Injury Hash:       {result.get('injury_hash', '')[:32]}…")
    print(f"  Timestamp:         {result.get('timestamp', 'N/A')}")

    for sc in result.get("star_changes", []):
        print(
            f"  ⚠ {sc['severity'].upper()}: {sc['player_name']} "
            f"({sc.get('team_name', '?')}) "
            f"{sc['old_status']} → {sc['new_status']} "
            f"factor: {sc['old_factor']:.2f} → {sc['new_factor']:.2f}"
        )


if __name__ == "__main__":
    import asyncio
    asyncio.run(_cli_run_sync())
