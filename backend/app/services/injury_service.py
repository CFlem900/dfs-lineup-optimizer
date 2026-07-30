"""NBA injury report service — BDL-powered with DNP decay and star monitoring.

Single source of truth for the RotationEngine DFS optimizer.  Fetches injury
data from the BALLDONTLIE ``/v1/player_injuries`` REST API, enriches with
DNP decay from ``player_minutes_history``, and manages cache state for the
LineupGenerator via SHA-256 injury hashing.

Pipeline (via ``sync_injuries()``):

    1. **Fetch** — Pulls injury data from BDL ``/v1/player_injuries``
       via async ``httpx.AsyncClient`` with cursor-based pagination.
    2. **DNP Decay** — Executes a SQL CTE against ``player_minutes_history``
       to detect players with 0 actual minutes in their last 5 games.
       Forces ``effective_factor = 0.00`` and labels as ``DNP-CD/AUTO-OUT``.
    3. **Upsert** — Writes enriched rows into ``nba_injuries`` using psycopg2
       ``INSERT … ON CONFLICT DO UPDATE``.
    4. **SHA-256 Injury Hash** — Generates a SHA-256 digest of active injury
       statuses.  Compares to previous hash in Redis; if different, triggers
       global cache bust on ``LineupOptimizerService``.
    5. **Star Monitor** — If a player with baseline >= 26.0 minutes has a
       status change, logs a structured event for AI Agent 3.
    6. **Pub/Sub Pre-Warm** — Publishes star changes to Redis channel
       ``rotation_engine_updates`` for the pre-warm subscriber daemon.

Official Factor Table (from architecture doc §1.2):
    Maps injury status → expected minutes factor using a conditional
    probability model: ``E[minutes] = P(plays) × E[minutes | plays]``.

    | Status       | P(plays) | E[min|plays] | Factor |
    |------------- |----------|-------------- |--------|
    | Out          |    0.00  |         0.00  |  0.00  |
    | Doubtful     |    0.20  |         0.75  |  0.15  |
    | GTD          |    0.72  |         0.92  |  0.66  |
    | Questionable |    0.85  |         0.95  |  0.81  |

DNP Decay (linear over last 5 games):

    | Consecutive DNPs | Decay Factor | Label           |
    |------------------|-------------|-----------------|
    | 0                | 1.00        | —               |
    | 1                | 0.80        | DNP-CD/1        |
    | 2                | 0.60        | DNP-CD/2        |
    | 3                | 0.40        | DNP-CD/3        |
    | 4                | 0.20        | DNP-CD/4        |
    | 5+               | 0.00        | DNP-CD/AUTO-OUT |

    Final effective_factor = min(injury_factor, dnp_decay_factor)

CLI Usage:

    python -m app.services.injury_service --sync-injuries
    python -m app.services.injury_service --hash-only
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import httpx

from app.config import get_settings
from app.config.constants import (
    INJURY_MINUTES_IF_ACTIVE,
    INJURY_PLAY_PROBABILITY,
    STAR_ANCHOR_THRESHOLD,
)
from app.models.player import PlayerStatus

logger = logging.getLogger(__name__)

# ============================================================================
# Official Factor Table
# ============================================================================
# E[minutes_factor] = P(plays) × E[minutes_pct | plays]
# These are the canonical values from SYSTEM_ARCHITECTURE.md §1.2.
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
# Linear decay over 5 consecutive DNPs.
# formula: decay = max(0.0, 1.0 - (consecutive_dnps / 5))
# 0 → 1.00,  1 → 0.80,  2 → 0.60,  3 → 0.40,  4 → 0.20,  5+ → 0.00

DNP_WINDOW: int = 5  # number of recent games to inspect
DNP_AUTO_OUT_THRESHOLD: int = 5  # >= this → force factor to 0.00


def dnp_decay_factor(consecutive_dnps: int) -> float:
    """Compute the linear DNP decay factor.

    Returns a float in [0.0, 1.0].  The factor degrades linearly:
        0 DNPs → 1.00 (full minutes expected)
        5+ DNPs → 0.00 (AUTO-OUT)
    """
    if consecutive_dnps >= DNP_AUTO_OUT_THRESHOLD:
        return 0.0
    return round(max(0.0, 1.0 - (consecutive_dnps / DNP_AUTO_OUT_THRESHOLD)), 2)


def dnp_label(consecutive_dnps: int) -> str:
    """Human-readable DNP decay label for the reason column."""
    if consecutive_dnps >= DNP_AUTO_OUT_THRESHOLD:
        return "DNP-CD/AUTO-OUT"
    if consecutive_dnps > 0:
        return f"DNP-CD/{consecutive_dnps}"
    return ""


# ============================================================================
# Redis Constants
# ============================================================================

REDIS_INJURY_HASH_KEY = "injury_sync:hash"
REDIS_INJURY_HASH_TTL = 3600  # 1 hour — re-computes on every sync anyway
REDIS_PREWARM_CHANNEL = "rotation_engine_updates"


# ============================================================================
# SQL
# ============================================================================

# Schema migration: add columns if they don't exist yet.
# Safe to run repeatedly (IF NOT EXISTS / ADD COLUMN IF NOT EXISTS).
_MIGRATE_SQL = """
-- Ensure table exists with original schema
CREATE TABLE IF NOT EXISTS nba_injuries (
    player_name   VARCHAR(128) PRIMARY KEY,
    team_name     VARCHAR(64)  NOT NULL,
    injury_status VARCHAR(24)  NOT NULL,
    reason        TEXT,
    last_updated  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- Add extended columns (idempotent)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'nba_injuries' AND column_name = 'team_id'
    ) THEN
        ALTER TABLE nba_injuries ADD COLUMN team_id INTEGER;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'nba_injuries' AND column_name = 'official_status'
    ) THEN
        ALTER TABLE nba_injuries ADD COLUMN official_status VARCHAR(32);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'nba_injuries' AND column_name = 'effective_factor'
    ) THEN
        ALTER TABLE nba_injuries ADD COLUMN effective_factor FLOAT DEFAULT 1.0;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'nba_injuries' AND column_name = 'consecutive_dnp_count'
    ) THEN
        ALTER TABLE nba_injuries ADD COLUMN consecutive_dnp_count INTEGER DEFAULT 0;
    END IF;
END
$$;

-- Indexes (idempotent)
CREATE INDEX IF NOT EXISTS ix_nba_inj_team     ON nba_injuries (team_name);
CREATE INDEX IF NOT EXISTS ix_nba_inj_team_id  ON nba_injuries (team_id);
CREATE INDEX IF NOT EXISTS ix_nba_inj_status   ON nba_injuries (injury_status);
CREATE INDEX IF NOT EXISTS ix_nba_inj_factor   ON nba_injuries (effective_factor);
"""

# Optimized CTE-based DNP streak query.
_DNP_CTE_SQL = """
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
    WHERE rn <= %(window)s
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
        COALESCE(fp.first_played_rn - 1, %(window)s) AS consecutive_dnps
    FROM (SELECT DISTINCT player_name FROM last_n) d
    LEFT JOIN first_played fp ON fp.player_name = d.player_name
)
SELECT player_name, consecutive_dnps
FROM dnp_counts
WHERE consecutive_dnps > 0
ORDER BY consecutive_dnps DESC;
"""

# Full upsert with all extended columns.
_UPSERT_SQL = """
INSERT INTO nba_injuries (
    player_name, team_name, team_id, injury_status, official_status,
    effective_factor, reason, consecutive_dnp_count, last_updated
)
VALUES (
    %(player_name)s, %(team_name)s, %(team_id)s, %(injury_status)s,
    %(official_status)s, %(effective_factor)s, %(reason)s,
    %(consecutive_dnp_count)s, %(last_updated)s
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
"""

# Read previous state for change detection (star monitoring).
_SELECT_PREVIOUS_SQL = """
SELECT player_name, injury_status, effective_factor
FROM nba_injuries;
"""

# Post-upsert: read all active (non-Available) injuries for SHA-256 hash.
_SELECT_ACTIVE_FOR_HASH_SQL = """
SELECT player_name, injury_status, effective_factor
FROM nba_injuries
WHERE injury_status != 'Available'
  AND effective_factor < 1.0
ORDER BY player_name;
"""

_SELECT_ALL_SQL = """
SELECT player_name, team_name, injury_status, reason, last_updated
FROM nba_injuries
ORDER BY last_updated DESC;
"""


# ============================================================================
# psycopg2 connection helpers
# ============================================================================

def _get_dsn() -> str:
    """Return normalised PostgreSQL DSN from DATABASE_URL."""
    raw = os.environ.get("DATABASE_URL", "")
    if not raw:
        raise EnvironmentError(
            "DATABASE_URL is not set.  "
            "Example: postgresql://user:pass@localhost:5432/rotationengine"
        )
    parsed = urlparse(raw)
    if parsed.scheme in ("postgresql+asyncpg", "postgres+asyncpg"):
        raw = raw.replace(parsed.scheme, "postgresql", 1)
    elif parsed.scheme == "postgres":
        raw = raw.replace("postgres://", "postgresql://", 1)
    return raw


def _pg_connect():
    """Open a psycopg2 connection, or return None if unavailable."""
    try:
        import psycopg2
    except ImportError:
        logger.debug("psycopg2 not installed — DB operations disabled")
        return None
    try:
        dsn = _get_dsn()
    except EnvironmentError:
        logger.debug("DATABASE_URL not set — DB operations disabled")
        return None
    conn = psycopg2.connect(dsn)
    conn.autocommit = False
    return conn


# ============================================================================
# Synchronous Redis helpers (redis-py, NOT async)
# ============================================================================

def _redis_connect():
    """Open a synchronous redis-py connection.

    Reads connection parameters from environment (same as CacheService):
        REDIS_HOST (default localhost), REDIS_PORT (6379), REDIS_DB (0),
        REDIS_PASSWORD (optional).

    Returns a ``redis.Redis`` instance or ``None`` if unavailable.
    """
    try:
        import redis
    except ImportError:
        logger.debug("redis-py not installed — Redis operations disabled")
        return None

    host = os.environ.get("REDIS_HOST", "localhost")
    port = int(os.environ.get("REDIS_PORT", "6379"))
    db = int(os.environ.get("REDIS_DB", "0"))
    password = os.environ.get("REDIS_PASSWORD") or None

    try:
        r = redis.Redis(
            host=host, port=port, db=db, password=password,
            decode_responses=True, socket_connect_timeout=3,
        )
        r.ping()
        return r
    except Exception as exc:
        logger.warning("Redis unavailable (sync): %s", exc)
        return None


# ============================================================================
# Team-name → team_id resolution
# ============================================================================

_TEAM_NAME_TO_ID: Optional[Dict[str, int]] = None


def _get_team_name_to_id() -> Dict[str, int]:
    """Build a case-insensitive team name → team_id lookup."""
    global _TEAM_NAME_TO_ID
    if _TEAM_NAME_TO_ID is not None:
        return _TEAM_NAME_TO_ID

    try:
        from nba_api.stats.static import teams as nba_teams

        mapping: Dict[str, int] = {}
        for t in nba_teams.get_teams():
            tid = t["id"]
            mapping[t["full_name"].lower()] = tid
            mapping[t["abbreviation"].lower()] = tid
            mapping[t["nickname"].lower()] = tid
            mapping[t["city"].lower()] = tid
            mapping[f"{t['city']} {t['nickname']}".lower()] = tid
        _TEAM_NAME_TO_ID = mapping
    except Exception as exc:
        logger.warning("Failed to build team_name→id lookup: %s", exc)
        _TEAM_NAME_TO_ID = {}

    return _TEAM_NAME_TO_ID


def resolve_team_id(team_name: str) -> Optional[int]:
    """Resolve a team name string to an nba_api team_id."""
    if not team_name:
        return None
    lookup = _get_team_name_to_id()
    key = team_name.lower().strip()

    if key in lookup:
        return lookup[key]

    for name, tid in lookup.items():
        if key in name or name in key:
            return tid

    return None


# ============================================================================
# Star Baseline Cache
# ============================================================================

def _fetch_star_baselines(conn) -> Dict[str, float]:
    """Query season-average minutes for all players from history table."""
    sql = """
    SELECT player_name, AVG(actual_minutes) AS avg_min, COUNT(*) AS gp
    FROM player_minutes_history
    WHERE sport = 'nba'
      AND actual_minutes IS NOT NULL
    GROUP BY player_name
    HAVING COUNT(*) >= 10;
    """
    baselines: Dict[str, float] = {}
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            for row in cur.fetchall():
                baselines[row[0]] = round(float(row[1]), 1)
    except Exception as exc:
        logger.warning("Failed to fetch star baselines: %s", exc)
    return baselines


# ============================================================================
# SHA-256 Injury Hashing
# ============================================================================

def compute_injury_hash(conn) -> str:
    """Generate a SHA-256 hash of all active injury statuses post-upsert."""
    try:
        with conn.cursor() as cur:
            cur.execute(_SELECT_ACTIVE_FOR_HASH_SQL)
            rows = cur.fetchall()
    except Exception as exc:
        logger.warning("Failed to read injuries for hash: %s", exc)
        return ""

    if not rows:
        return hashlib.sha256(b"NO_ACTIVE_INJURIES").hexdigest()

    parts = [
        f"{row[0]}:{row[1]}:{row[2]:.2f}"
        for row in rows
    ]
    payload = "\n".join(parts).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _compare_and_store_hash(redis_client, new_hash: str) -> bool:
    """Compare ``new_hash`` to the stored SHA-256 in Redis.

    Returns ``True`` if injury state changed and caches need busting.
    """
    if redis_client is None:
        logger.debug("No Redis client — assuming injury hash changed")
        return True

    try:
        previous_hash = redis_client.get(REDIS_INJURY_HASH_KEY)
    except Exception as exc:
        logger.warning("Redis GET failed for injury hash: %s", exc)
        return True

    changed = previous_hash != new_hash

    try:
        redis_client.setex(REDIS_INJURY_HASH_KEY, REDIS_INJURY_HASH_TTL, new_hash)
    except Exception as exc:
        logger.warning("Redis SETEX failed for injury hash: %s", exc)

    if changed:
        logger.info(
            "Injury hash CHANGED: %s → %s",
            (previous_hash or "<none>")[:16] + "…",
            new_hash[:16] + "…",
        )
    else:
        logger.debug("Injury hash unchanged: %s", new_hash[:16] + "…")

    return changed


def _bust_lineup_optimizer_caches(redis_client) -> int:
    """Trigger a global cache bust on LineupOptimizerService.

    Two mechanisms:
    1. In-process — calls ``clear_optimizer_cache()`` directly.
    2. Redis patterns — SCAN+DEL for injury/pool/slate/rotation keys.
    """
    cleared = 0

    try:
        from app.services.lineup_optimizer_service import clear_optimizer_cache
        cleared += clear_optimizer_cache()
        logger.info(
            "[InjuryHash] LineupOptimizerService in-process caches cleared (%d entries)",
            cleared,
        )
    except ImportError:
        logger.debug("LineupOptimizerService not importable (standalone CLI mode)")
    except Exception as exc:
        logger.warning("Failed to clear optimizer in-process cache: %s", exc)

    if redis_client is not None:
        patterns = ["injury:*", "pool:*", "slate:*", "rotation:*"]
        for pattern in patterns:
            try:
                cursor = 0
                while True:
                    cursor, keys = redis_client.scan(
                        cursor=cursor, match=pattern, count=200,
                    )
                    if keys:
                        redis_client.delete(*keys)
                        cleared += len(keys)
                    if cursor == 0:
                        break
            except Exception as exc:
                logger.warning("Redis SCAN+DEL failed for '%s': %s", pattern, exc)

        logger.info(
            "[InjuryHash] Redis patterns cleared (%d total keys across %d patterns)",
            cleared, len(patterns),
        )

    return cleared


# ============================================================================
# Redis Pub/Sub — Star Player Pre-Warm Trigger
# ============================================================================

def _publish_star_prewarm(
    redis_client,
    star_changes: List[Dict[str, Any]],
) -> int:
    """PUBLISH star-player change events to the pre-warm channel."""
    import json as _json

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
            receivers = redis_client.publish(
                REDIS_PREWARM_CHANNEL,
                _json.dumps(payload, default=str),
            )
            published += 1
            logger.warning(
                "[PREWARM-PUBLISH] %s | %s (%s) | %s → %s | "
                "baseline %.1f min | receivers=%d",
                change.get("severity", "?").upper(),
                change["player_name"],
                change.get("team_name", "?"),
                change.get("old_status", "?"),
                change.get("new_status", "?"),
                change.get("baseline_minutes", 0),
                receivers,
            )
        except Exception as exc:
            logger.error(
                "[PREWARM-PUBLISH] Failed to publish for %s: %s",
                change["player_name"], exc,
            )

    return published


# ============================================================================
# InjuryService
# ============================================================================

class InjuryService:
    """BDL-powered injury service with DNP decay, SHA-256 hashing, and star monitoring.

    This is the **single source of truth** for injury data in the
    RotationEngine DFS optimizer.  Replaces the former four-source
    waterfall (PDF/ESPN/BBRef) and the separate InjurySyncService.

    Data flows through:
        1. BDL ``/v1/player_injuries`` API (primary fetch)
        2. DNP decay enrichment from ``player_minutes_history``
        3. PostgreSQL upsert (``nba_injuries`` table)
        4. SHA-256 hash comparison + Redis cache bust
        5. Star player monitoring + Pub/Sub pre-warm

    Public API (preserved for backward compatibility):
        - ``get_injuries(team_name=None) -> List[PlayerStatus]``
        - ``get_all_injuries() -> List[PlayerStatus]``
        - ``get_team_injuries(team_name) -> List[PlayerStatus]``
        - ``get_active_injuries(team_name=None) -> List[Dict]``
        - ``get_injury_hash(team_names=None) -> str``
        - ``sync_injuries(conn=None) -> Dict[str, Any]``
        - ``get_db_injuries(conn=None) -> List[Dict]``
        - ``get_official_factor(status) -> float``
        - ``get_all_factors() -> Dict[str, float]``
    """

    def __init__(self, cache_service: Optional[Any] = None):
        # In-memory cache
        self._cache: Optional[List[Dict]] = None
        self._cache_timestamp: Optional[datetime] = None
        self._cache_ttl_seconds = 300  # 5 minutes
        self._injury_hash: Optional[str] = None

        # Redis cache service (injected or lazy-resolved)
        self._cache_service = cache_service

        # Track DB max(last_updated) for cache-bust detection
        self._last_db_updated: Optional[datetime] = None

        # BDL API configuration
        _settings = get_settings()
        self._bdl_api_key: str = _settings.balldontlie_api_key
        self._bdl_base_url: str = "https://api.balldontlie.io/v1"
        # Lazy-loaded BDL team_id → (nba_team_id, full_name, abbreviation)
        self._bdl_team_map: Optional[Dict[int, Tuple[int, str, str]]] = None

    # ------------------------------------------------------------------
    # Official Factor helpers
    # ------------------------------------------------------------------

    @staticmethod
    def get_official_factor(status: str) -> float:
        """Return the expected-minutes factor for an injury status.

        Uses the conditional probability model:
            ``E[minutes] = P(plays) × E[minutes | plays]``

        Returns:
            float — 0.00 (Out) .. 1.00 (Available/healthy).
        """
        return OFFICIAL_FACTORS.get(status, OFFICIAL_FACTORS.get("Questionable", 0.81))

    @staticmethod
    def get_all_factors() -> Dict[str, float]:
        """Return the full Official Factor lookup table."""
        return dict(OFFICIAL_FACTORS)

    # ------------------------------------------------------------------
    # In-memory cache
    # ------------------------------------------------------------------

    def _is_cache_valid(self) -> bool:
        if self._cache_timestamp is None:
            return False
        elapsed = (datetime.now() - self._cache_timestamp).total_seconds()
        return elapsed < self._cache_ttl_seconds

    def _get_raw_injuries(self) -> List[Dict]:
        """Return the cached raw injury list from the LOCAL DATABASE.

        **NEVER** makes live HTTP calls to BDL during lineup generation.
        Reads exclusively from the ``nba_injuries`` PostgreSQL table which
        is populated by the background ``sync_nba_injuries`` APScheduler
        job every 15 minutes.

        Fallback chain:
            1. In-memory cache (5-min TTL)
            2. Local ``nba_injuries`` DB table
            3. Empty list (safe — DK CSV failsafe catches Out players)
        """
        if self._is_cache_valid() and self._cache is not None:
            return self._cache

        # Read from LOCAL DATABASE — never hit BDL live
        db_rows = self._read_injuries_from_db()
        if db_rows:
            self._cache = db_rows
        else:
            logger.warning(
                "InjuryService: nba_injuries table returned no rows — "
                "is the background sync_nba_injuries job running?"
            )
            self._cache = []

        self._cache_timestamp = datetime.now()
        self._injury_hash = self._compute_hash(self._cache)
        return self._cache

    def _read_injuries_from_db(self) -> List[Dict]:
        """Read all injuries from the local ``nba_injuries`` table.

        Returns them in the same internal format as ``_bdl_rows_to_internal``
        so all downstream consumers (``get_injuries``, ``get_active_injuries``,
        ``get_injury_hash``) work unchanged.

        This is the **DB-only** read path — no HTTP calls.
        """
        conn = _pg_connect()
        if conn is None:
            logger.warning(
                "InjuryService._read_injuries_from_db: "
                "Database unavailable — returning empty"
            )
            return []

        try:
            rows: List[Dict] = []
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT player_name, team_name, injury_status,
                           reason, effective_factor
                    FROM nba_injuries
                    ORDER BY last_updated DESC;
                """)
                for r in cur.fetchall():
                    status = r[2]
                    # Skip Available / fully healthy players
                    eff = r[4] if r[4] is not None else 1.0
                    if status == "Available" and eff >= 1.0:
                        continue
                    rows.append({
                        "player": r[0],
                        "team": r[1],
                        "status": status,
                        "description": r[3] or "",
                    })
            logger.info(
                "InjuryService: loaded %d active injuries from local DB",
                len(rows),
            )
            return rows
        except Exception as exc:
            logger.error(
                "InjuryService._read_injuries_from_db failed: %s", exc
            )
            return []
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Hash (for downstream cache invalidation)
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_hash(injuries: List[Dict]) -> str:
        """Compute a stable fingerprint of the injury list."""
        parts = sorted(
            f"{inj.get('player', '')}:{inj.get('status', '')}"
            for inj in injuries
        )
        return hashlib.md5("|".join(parts).encode()).hexdigest()[:12]

    def get_injury_hash(self, team_names: Optional[List[str]] = None) -> str:
        """Return a short hash representing the current injury state.

        Forces a fetch if the cache is stale.  Used by the player pool
        builder to detect when injuries have changed and bust its own
        cache.

        Args:
            team_names: If provided, only include injuries for these teams
                in the hash.
        """
        injuries = self._get_raw_injuries()
        if team_names:
            lowered = {t.lower() for t in team_names}
            filtered = [
                inj for inj in injuries
                if any(t in inj.get("team", "").lower() for t in lowered)
            ]
            return self._compute_hash(filtered)
        return self._injury_hash or ""

    # ------------------------------------------------------------------
    # Public interface — PlayerStatus list (unchanged signatures)
    # ------------------------------------------------------------------

    def get_injuries(self, team_name: Optional[str] = None) -> List[PlayerStatus]:
        """Fetch current injury reports. Optionally filter by team."""
        try:
            injuries = self._get_raw_injuries()

            if team_name:
                team_lower = team_name.lower()
                injuries = [
                    inj
                    for inj in injuries
                    if team_lower in inj.get("team", "").lower()
                ]

            return self._parse_injuries(injuries)
        except Exception as e:
            logger.error(f"Failed to fetch injuries: {e}")
            return []

    def get_all_injuries(self) -> List[PlayerStatus]:
        """Return the full (unfiltered) injury list."""
        return self.get_injuries(team_name=None)

    def get_team_injuries(self, team_name: str) -> List[PlayerStatus]:
        """Get injuries filtered by team name."""
        return self.get_injuries(team_name=team_name)

    # ------------------------------------------------------------------
    # Public interface — InjuryImpactAgent format
    # ------------------------------------------------------------------

    def get_active_injuries(
        self,
        team_name: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Return injury data formatted for the InjuryImpactAgent (Agent 3).

        Each entry is a dict with the keys the agent expects::

            {
                "player_name":       str,
                "status":            str,     # "Out", "Doubtful", "GTD", "Questionable"
                "injury_description": str,
                "official_factor":   float,   # 0.00 – 1.00
                "p_plays":           float,
                "e_minutes_if_active": float,
                "team_name":         str,
                "last_updated":      str,     # ISO 8601
            }

        Players with status "Available" are excluded.
        Sorted by severity: Out → Doubtful → GTD → Questionable.
        """
        raw = self._get_raw_injuries()

        if team_name:
            team_lower = team_name.lower()
            raw = [
                inj for inj in raw
                if team_lower in inj.get("team", "").lower()
            ]

        severity_order = {"Out": 0, "Doubtful": 1, "GTD": 2, "Questionable": 3}
        results: List[Dict[str, Any]] = []

        for inj in raw:
            status = inj.get("status", "Questionable")
            # Normalize IR/INJ → Out before validation
            if status.upper() in ("IR", "INJ", "INJURED RESERVE"):
                status = "Out"
            valid = {"Available", "Out", "Questionable", "Doubtful", "GTD", "Game Time Decision"}
            if status not in valid:
                status = self._parse_status_text(
                    inj.get("description", "") or status
                )

            if status == "Available":
                continue

            canonical_status = "GTD" if status == "Game Time Decision" else status

            factor = OFFICIAL_FACTORS.get(canonical_status, 0.81)
            p_plays = INJURY_PLAY_PROBABILITY.get(
                canonical_status, INJURY_PLAY_PROBABILITY.get(status, 0.85)
            )
            e_min = INJURY_MINUTES_IF_ACTIVE.get(
                canonical_status, INJURY_MINUTES_IF_ACTIVE.get(status, 0.95)
            )

            results.append({
                "player_name": inj.get("player", "Unknown"),
                "status": canonical_status,
                "injury_description": inj.get("description", ""),
                "official_factor": factor,
                "p_plays": p_plays,
                "e_minutes_if_active": e_min,
                "team_name": inj.get("team", ""),
                "last_updated": datetime.now(timezone.utc).isoformat(),
            })

        results.sort(key=lambda x: severity_order.get(x["status"], 99))
        return results

    # ------------------------------------------------------------------
    # BDL API — fetch injuries
    # ------------------------------------------------------------------

    def _ensure_bdl_team_map(self) -> Dict[int, Tuple[int, str, str]]:
        """Build BDL team_id → (nba_team_id, full_name, abbreviation) mapping."""
        if self._bdl_team_map is not None:
            return self._bdl_team_map

        _BDL_TEAMS = {
            "ATL": 1, "BOS": 2, "BKN": 3, "CHA": 4, "CHI": 5,
            "CLE": 6, "DAL": 7, "DEN": 8, "DET": 9, "GSW": 10,
            "HOU": 11, "IND": 12, "LAC": 13, "LAL": 14, "MEM": 15,
            "MIA": 16, "MIL": 17, "MIN": 18, "NOP": 19, "NYK": 20,
            "OKC": 21, "ORL": 22, "PHI": 23, "PHX": 24, "POR": 25,
            "SAC": 26, "SAS": 27, "TOR": 28, "UTA": 29, "WAS": 30,
        }
        abbr_to_bdl = _BDL_TEAMS

        mapping: Dict[int, Tuple[int, str, str]] = {}
        try:
            from nba_api.stats.static import teams as nba_teams

            for t in nba_teams.get_teams():
                abbr = t["abbreviation"].upper()
                bdl_id = abbr_to_bdl.get(abbr)
                if bdl_id is not None:
                    mapping[bdl_id] = (t["id"], t["full_name"], abbr)
        except Exception as exc:
            logger.warning("Failed to build BDL team mapping from nba_api: %s", exc)

        if not mapping:
            logger.warning(
                "BDL team map is empty — falling back to abbreviation-only map"
            )
            for abbr, bdl_id in abbr_to_bdl.items():
                mapping[bdl_id] = (0, abbr, abbr)

        self._bdl_team_map = mapping
        return mapping

    async def _fetch_injuries_async(self) -> List[Dict[str, str]]:
        """Fetch today's injury report from BALLDONTLIE API.

        Uses ``httpx.AsyncClient`` with cursor-based pagination.
        Maps BDL player/team data to our standard format.

        Returns list of dicts: [{player_name, team_name, injury_status, reason}]
        """
        if not self._bdl_api_key:
            logger.warning(
                "[InjuryService] No BDL API key configured — cannot fetch injuries"
            )
            return []

        team_map = self._ensure_bdl_team_map()
        headers = {"Authorization": self._bdl_api_key}
        all_injuries: List[Dict[str, str]] = []
        cursor: Optional[int] = None
        max_pages = 10

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                for page in range(max_pages):
                    url = f"{self._bdl_base_url}/player_injuries?per_page=100"
                    if cursor is not None:
                        url += f"&cursor={cursor}"

                    resp = await client.get(url, headers=headers)
                    resp.raise_for_status()
                    body = resp.json()

                    for entry in body.get("data", []):
                        player = entry.get("player") or {}
                        first = (player.get("first_name") or "").strip()
                        last = (player.get("last_name") or "").strip()
                        player_name = f"{first} {last}".strip()
                        if not player_name:
                            continue

                        bdl_team_id = player.get("team_id")
                        team_info = team_map.get(bdl_team_id, (None, "", ""))

                        all_injuries.append({
                            "player_name": player_name,
                            "team_name": team_info[1],
                            "injury_status": entry.get("status", "Questionable"),
                            "reason": entry.get("description", ""),
                        })

                    meta = body.get("meta") or {}
                    cursor = meta.get("next_cursor")
                    if cursor is None:
                        break

                    logger.debug(
                        "[InjuryService] BDL page %d: %d entries, next_cursor=%s",
                        page + 1, len(body.get("data", [])), cursor,
                    )

        except httpx.HTTPStatusError as exc:
            logger.error(
                "[InjuryService] BDL API HTTP error %d: %s",
                exc.response.status_code, exc,
            )
        except httpx.TimeoutException:
            logger.error("[InjuryService] BDL API request timed out (15s)")
        except Exception as exc:
            logger.error("[InjuryService] BDL API request failed: %s", exc)

        logger.info(
            "[InjuryService] Fetched %d injuries from BALLDONTLIE API",
            len(all_injuries),
        )
        return all_injuries

    def _fetch_injuries(self) -> List[Dict[str, str]]:
        """Synchronous wrapper for the async BDL injury fetch.

        Uses ``asyncio.run()`` to execute the async method.  Safe to call
        from ``sync_injuries()`` which is invoked in a thread pool worker.
        """
        return asyncio.run(self._fetch_injuries_async())

    # ------------------------------------------------------------------
    # Sync pipeline (full enrichment + upsert + hash + star monitor)
    # ------------------------------------------------------------------

    def sync_injuries(self, conn=None) -> Dict[str, Any]:
        """Execute the full sync pipeline.

        Steps:
            1. Read previous injury state from DB (for change detection).
            2. Fetch current injuries from BDL API.
            3. Query DNP streaks via CTE against player_minutes_history.
            4. Compute effective_factor = min(injury_factor, dnp_decay).
            5. Upsert into nba_injuries.
            6. Compute SHA-256 hash of post-upsert active injuries.
            7. Compare hash to Redis — if different, bust LineupOptimizer caches.
            8. Detect star status changes → log Agent 3 signal.
            9. Pub/Sub pre-warm trigger for star changes.

        Args:
            conn: Optional psycopg2 connection (for testing).

        Returns:
            Summary dict with counts, hash, and star change details.
        """
        own_conn = conn is None
        if own_conn:
            conn = _pg_connect()
            if conn is None:
                return {"error": "Database unavailable", "upserted": 0}

        redis_client = _redis_connect()

        try:
            # Step 0: Ensure schema is up to date
            self._migrate_schema(conn)

            # Step 1: Snapshot previous state for change detection
            previous_state = self._read_previous_state(conn)

            # Step 2: Fetch current injuries from BDL API
            injury_rows = self._fetch_injuries()
            if not injury_rows:
                return {
                    "upserted": 0,
                    "dnp_decayed": 0,
                    "star_changes": [],
                    "cache_busted_teams": [],
                    "injury_hash": "",
                    "hash_changed": False,
                    "optimizer_caches_cleared": 0,
                    "message": "No injury data from BDL API",
                }

            # Step 3: Query DNP streaks (optimized CTE)
            dnp_streaks = self._query_dnp_streaks(conn)

            # Step 4: Fetch star baselines for monitoring
            star_baselines = _fetch_star_baselines(conn)

            # Step 5: Enrich and upsert
            now = datetime.now(timezone.utc)
            upserted = 0
            dnp_decayed = 0
            upserted_rows: List[Dict[str, Any]] = []

            with conn.cursor() as cur:
                for row in injury_rows:
                    player_name = row["player_name"]
                    team_name = row.get("team_name", "")
                    raw_status = row.get("injury_status", "Questionable")
                    reason = row.get("reason", "")

                    normalised = self._normalise_status(raw_status)

                    injury_factor = OFFICIAL_FACTORS.get(
                        normalised, OFFICIAL_FACTORS.get("Questionable", 0.81)
                    )

                    consecutive_dnps = dnp_streaks.get(player_name, 0)
                    decay = dnp_decay_factor(consecutive_dnps)

                    effective = round(min(injury_factor, decay), 2)

                    dnp_tag = dnp_label(consecutive_dnps)
                    if dnp_tag and decay < injury_factor:
                        enriched_reason = (
                            f"{reason} [{dnp_tag}: {consecutive_dnps} consecutive DNPs, "
                            f"decay={decay:.2f} overrides injury factor={injury_factor:.2f}]"
                        ).strip()
                        dnp_decayed += 1
                    elif dnp_tag:
                        enriched_reason = (
                            f"{reason} [{dnp_tag}: {consecutive_dnps} consecutive DNPs]"
                        ).strip()
                    else:
                        enriched_reason = reason

                    team_id = resolve_team_id(team_name)

                    params = {
                        "player_name": player_name,
                        "team_name": team_name,
                        "team_id": team_id,
                        "injury_status": normalised,
                        "official_status": raw_status,
                        "effective_factor": effective,
                        "reason": enriched_reason,
                        "consecutive_dnp_count": consecutive_dnps,
                        "last_updated": now,
                    }
                    cur.execute(_UPSERT_SQL, params)
                    upserted += 1
                    upserted_rows.append(params)

            conn.commit()

            # Step 6: SHA-256 injury hash
            injury_hash = compute_injury_hash(conn)

            # Step 7: Compare hash to Redis + bust caches if changed
            hash_changed = _compare_and_store_hash(redis_client, injury_hash)
            optimizer_cleared = 0
            if hash_changed:
                optimizer_cleared = _bust_lineup_optimizer_caches(redis_client)
                # Also bust via async CacheService
                self._bust_redis_cache()
                # Invalidate in-memory cache
                self._cache = None
                self._cache_timestamp = None
                self._injury_hash = None
                logger.warning(
                    "[InjuryService] Injury hash CHANGED → busted %d "
                    "optimizer cache entries",
                    optimizer_cleared,
                )

            # Step 8: Detect star status changes
            star_changes = self._detect_star_changes(
                previous_state, upserted_rows, star_baselines
            )

            busted_teams = self._bust_team_caches(star_changes)

            # Step 9: Pub/Sub pre-warm trigger
            prewarm_published = 0
            if star_changes and hash_changed and redis_client is not None:
                prewarm_published = _publish_star_prewarm(
                    redis_client, star_changes
                )

            result = {
                "upserted": upserted,
                "dnp_decayed": dnp_decayed,
                "star_changes": star_changes,
                "cache_busted_teams": busted_teams,
                "injury_hash": injury_hash,
                "hash_changed": hash_changed,
                "optimizer_caches_cleared": optimizer_cleared,
                "prewarm_published": prewarm_published,
                "timestamp": now.isoformat(),
            }

            logger.info(
                "InjuryService: synced %d players (%d DNP-decayed, "
                "%d star changes, hash_changed=%s, optimizer_cleared=%d, "
                "prewarm_published=%d)",
                upserted, dnp_decayed, len(star_changes),
                hash_changed, optimizer_cleared, prewarm_published,
            )
            return result

        except Exception:
            conn.rollback()
            logger.exception("InjuryService: sync failed")
            raise
        finally:
            if redis_client is not None:
                try:
                    redis_client.close()
                except Exception:
                    pass
            if own_conn and conn is not None:
                conn.close()

    # ------------------------------------------------------------------
    # Database queries
    # ------------------------------------------------------------------

    def get_db_injuries(self, conn=None) -> List[Dict[str, Any]]:
        """Read all rows from the ``nba_injuries`` table via psycopg2."""
        own_conn = conn is None
        if own_conn:
            conn = _pg_connect()
            if conn is None:
                return []

        try:
            rows: List[Dict[str, Any]] = []
            with conn.cursor() as cur:
                cur.execute(_SELECT_ALL_SQL)
                for r in cur.fetchall():
                    status = r[2]
                    rows.append({
                        "player_name": r[0],
                        "team_name": r[1],
                        "injury_status": status,
                        "reason": r[3],
                        "last_updated": r[4].isoformat() if r[4] else None,
                        "official_factor": OFFICIAL_FACTORS.get(status, 0.81),
                    })
            return rows
        finally:
            if own_conn and conn is not None:
                conn.close()

    def get_enriched_injuries(self, conn=None) -> List[Dict[str, Any]]:
        """Read all enriched injuries from the database.

        Returns rows with official_status, effective_factor,
        consecutive_dnp_count, and the DNP decay label.
        """
        own_conn = conn is None
        if own_conn:
            conn = _pg_connect()
            if conn is None:
                return []

        try:
            rows: List[Dict[str, Any]] = []
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT player_name, team_name, team_id, injury_status,
                           official_status, effective_factor, reason,
                           consecutive_dnp_count, last_updated
                    FROM nba_injuries
                    ORDER BY effective_factor ASC, last_updated DESC;
                """)
                for r in cur.fetchall():
                    dnp_count = r[7] or 0
                    rows.append({
                        "player_name": r[0],
                        "team_name": r[1],
                        "team_id": r[2],
                        "injury_status": r[3],
                        "official_status": r[4],
                        "effective_factor": r[5],
                        "reason": r[6],
                        "consecutive_dnp_count": dnp_count,
                        "dnp_label": dnp_label(dnp_count),
                        "last_updated": r[8].isoformat() if r[8] else None,
                    })
            return rows
        finally:
            if own_conn and conn is not None:
                conn.close()

    # ------------------------------------------------------------------
    # Pipeline helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _migrate_schema(conn) -> None:
        """Ensure nba_injuries table has all extended columns."""
        with conn.cursor() as cur:
            cur.execute(_MIGRATE_SQL)
        conn.commit()

    @staticmethod
    def _read_previous_state(conn) -> Dict[str, Tuple[str, float]]:
        """Read current DB state for change detection."""
        state: Dict[str, Tuple[str, float]] = {}
        try:
            with conn.cursor() as cur:
                cur.execute(_SELECT_PREVIOUS_SQL)
                for row in cur.fetchall():
                    state[row[0]] = (row[1], float(row[2]) if row[2] is not None else 1.0)
        except Exception as exc:
            logger.debug("No previous state (first run?): %s", exc)
        return state

    @staticmethod
    def _query_dnp_streaks(conn) -> Dict[str, int]:
        """Execute the DNP CTE query against player_minutes_history."""
        streaks: Dict[str, int] = {}
        try:
            with conn.cursor() as cur:
                cur.execute(_DNP_CTE_SQL, {"window": DNP_WINDOW})
                for row in cur.fetchall():
                    streaks[row[0]] = int(row[1])
        except Exception as exc:
            logger.debug("DNP streak query failed (expected on first run): %s", exc)
            try:
                conn.rollback()
            except Exception:
                pass
        return streaks

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
                if old_status == new_status and abs(old_factor - new_factor) < 0.01:
                    continue

                severity = (
                    "escalation" if new_factor < old_factor
                    else "de-escalation"
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

    def _bust_team_caches(self, star_changes: List[Dict[str, Any]]) -> List[str]:
        """Bust Redis caches for teams with star status changes."""
        if not star_changes:
            return []

        busted: List[str] = []

        for change in star_changes:
            team_name = change.get("team_name", "")
            team_id = change.get("team_id")
            player_name = change["player_name"]
            severity = change["severity"]

            logger.warning(
                "[STAR-INJURY-CHANGE] %s | %s (%s) | %s -> %s | "
                "factor: %.2f -> %.2f | baseline: %.1f min | "
                "ACTION: InjuryImpactAgent re-analysis required for team %s",
                severity.upper(),
                player_name,
                team_name,
                change["old_status"],
                change["new_status"],
                change["old_factor"],
                change["new_factor"],
                change["baseline_minutes"],
                team_name,
            )

            team_label = str(team_id) if team_id else team_name
            busted.append(team_label)

        self._bust_redis_patterns(busted)
        return busted

    def _bust_redis_patterns(self, team_ids: List[str]) -> None:
        """Purge Redis keys for affected teams and global injury caches."""
        cache_svc = self._cache_service
        if cache_svc is None:
            try:
                from app.api.dependencies import get_services
                svc = get_services()
                if hasattr(svc, "cache_service"):
                    cache_svc = svc.cache_service
            except Exception:
                pass

        if cache_svc is None:
            logger.debug("No CacheService available — async Redis bust skipped")
            return

        patterns = ["injury:*", "pool:*", "slate:*"]
        for tid in team_ids:
            patterns.append(f"rotation:{tid}:*")

        async def _do_bust():
            for pattern in patterns:
                await cache_svc.clear_pattern(pattern)
            logger.info(
                "Redis (async): cleared %d patterns for %d affected teams",
                len(patterns), len(team_ids),
            )

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_do_bust())
        except RuntimeError:
            asyncio.run(_do_bust())

    # ------------------------------------------------------------------
    # Redis cache bust (legacy — used by in-memory cache invalidation)
    # ------------------------------------------------------------------

    def _bust_redis_cache(self) -> None:
        """Purge all ``injury:*`` keys from Redis via async CacheService."""
        cache_svc = self._cache_service
        if cache_svc is None:
            try:
                from app.api.dependencies import get_services
                svc = get_services()
                if hasattr(svc, "cache_service"):
                    cache_svc = svc.cache_service
            except Exception:
                pass

        if cache_svc is None:
            logger.debug(
                "No CacheService available — Redis bust skipped "
                "(in-memory TTL will expire in %ds)", self._cache_ttl_seconds,
            )
            return

        async def _do_bust():
            await cache_svc.clear_pattern("injury:*")
            logger.info("Redis: cleared injury:* cache keys")

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_do_bust())
        except RuntimeError:
            asyncio.run(_do_bust())

    # ------------------------------------------------------------------
    # Format converters
    # ------------------------------------------------------------------

    @staticmethod
    def _bdl_rows_to_internal(rows: List[Dict[str, str]]) -> List[Dict]:
        """Convert BDL API row format → internal dict format."""
        internal: List[Dict] = []
        for r in rows:
            raw_status = r.get("injury_status", "Questionable")
            status = InjuryService._normalise_status(raw_status)
            internal.append({
                "player": r["player_name"],
                "team": r["team_name"],
                "description": r.get("reason", ""),
                "status": status,
            })
        return internal

    @staticmethod
    def _normalise_status(raw: str) -> str:
        """Map raw injury status text to one of our canonical categories."""
        lower = raw.lower().strip()
        mapping = {
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
        return mapping.get(lower, raw if raw in mapping.values() else "Questionable")

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_status_text(description: str) -> str:
        """Parse injury description into a status category.

        Handles common phrasing from NBA.com, ESPN, RotoWire, Twitter.
        """
        desc_lower = description.lower()

        if "expected to play" in desc_lower or "probable" in desc_lower:
            return "Probable"

        if "day to day" in desc_lower or "day-to-day" in desc_lower or "gtd" in desc_lower:
            return "GTD"

        if "out for season" in desc_lower or "out for the season" in desc_lower:
            return "Out"
        if "ruled out" in desc_lower:
            return "Out"
        if "will not play" in desc_lower or "will miss" in desc_lower:
            return "Out"
        if "sidelined" in desc_lower:
            return "Out"
        if "inactive" in desc_lower:
            return "Out"
        if "suspended" in desc_lower:
            return "Out"
        if "not with team" in desc_lower or "not with the team" in desc_lower:
            return "Out"
        if desc_lower.strip() == "dnp" or "did not participate" in desc_lower:
            return "Out"
        if "injured reserve" in desc_lower or desc_lower.strip() in ("ir", "inj"):
            return "Out"

        if "doubtful" in desc_lower:
            return "Doubtful"
        if "questionable" in desc_lower:
            return "Questionable"
        if desc_lower.startswith("out") or "out (" in desc_lower or " out " in desc_lower:
            return "Out"
        return "Questionable"

    def _parse_injuries(self, raw_injuries: List[Dict]) -> List[PlayerStatus]:
        """Convert raw injury dicts into PlayerStatus objects."""
        statuses = []
        for inj in raw_injuries:
            player_name = inj.get("player", inj.get("Player", "Unknown"))
            status_text = inj.get("status", inj.get("Status", "Questionable"))
            description = inj.get("description", inj.get("Description", ""))

            # Normalize IR/INJ → Out before validation
            if status_text.upper() in ("IR", "INJ", "INJURED RESERVE"):
                status_text = "Out"

            valid_statuses = {"Available", "Out", "Questionable", "Doubtful", "GTD", "Probable"}
            if status_text not in valid_statuses:
                status_text = self._parse_status_text(description or status_text)

            statuses.append(
                PlayerStatus(
                    player_id=0,
                    player_name=player_name,
                    status=status_text,
                    injury_description=description,
                )
            )
        return statuses

    # ------------------------------------------------------------------
    # Public helpers (from former InjurySyncService)
    # ------------------------------------------------------------------

    @staticmethod
    def get_status_factor(status: str) -> float:
        """Look up the effective minutes factor for an injury status."""
        return OFFICIAL_FACTORS.get(status, 0.81)

    @staticmethod
    def get_dnp_decay(consecutive_dnps: int) -> float:
        """Compute the DNP decay factor for a given streak length."""
        return dnp_decay_factor(consecutive_dnps)


# ============================================================================
# CLI Entrypoint
# ============================================================================

def _cli_main() -> None:
    """CLI entrypoint for standalone sync execution.

    Usage::

        python -m app.services.injury_service --sync-injuries
        python -m app.services.injury_service --hash-only
    """
    import argparse

    parser = argparse.ArgumentParser(
        description="RotationEngine Injury Service — BDL-Powered Source of Truth",
    )
    parser.add_argument(
        "--sync-injuries",
        action="store_true",
        help="Execute full injury sync pipeline (fetch → DNP → upsert → hash → bust)",
    )
    # Legacy flag alias
    parser.add_argument(
        "--sync",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--hash-only",
        action="store_true",
        help="Only compute and display the current SHA-256 injury hash (no sync)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable DEBUG logging",
    )
    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.hash_only:
        conn = _pg_connect()
        if conn is None:
            print("ERROR: Cannot connect to PostgreSQL (check DATABASE_URL)")
            sys.exit(1)
        try:
            h = compute_injury_hash(conn)
            print(f"SHA-256 injury hash: {h}")
        finally:
            conn.close()
        return

    if args.sync_injuries or args.sync:
        svc = InjuryService()
        result = svc.sync_injuries()

        if "error" in result:
            print(f"ERROR: {result['error']}")
            sys.exit(1)

        print(f"{'='*60}")
        print(f"Injury Sync Complete")
        print(f"{'='*60}")
        print(f"  Upserted:          {result['upserted']}")
        print(f"  DNP-Decayed:       {result['dnp_decayed']}")
        print(f"  Star Changes:      {len(result.get('star_changes', []))}")
        print(f"  Hash Changed:      {result.get('hash_changed', False)}")
        print(f"  Optimizer Cleared:  {result.get('optimizer_caches_cleared', 0)}")
        print(f"  PreWarm Published:  {result.get('prewarm_published', 0)}")
        print(f"  Injury Hash:       {result.get('injury_hash', '')[:32]}...")
        print(f"  Timestamp:         {result.get('timestamp', 'N/A')}")

        for sc in result.get("star_changes", []):
            print(
                f"  STAR: {sc['player_name']} ({sc['team_name']}) "
                f"-- {sc['severity'].upper()}: "
                f"{sc['old_status']}({sc['old_factor']:.2f}) -> "
                f"{sc['new_status']}({sc['new_factor']:.2f}) "
                f"[baseline {sc['baseline_minutes']:.1f} min]"
            )

        print(f"{'='*60}")
        return

    parser.print_help()


if __name__ == "__main__":
    _cli_main()
