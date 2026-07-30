"""Standalone NBA injury sync service using psycopg2.

Scrapes the official NBA.com injury report via the ``nbainjuries``
library and synchronises it with a PostgreSQL ``nba_injuries`` table
using a pure-sync psycopg2 connection.  Designed to run independently
of the FastAPI application — no async runtime required.

Usage::

    python injury_service.py --sync

Environment:
    DATABASE_URL  — PostgreSQL DSN (required).
                    Example: ``postgresql://user:pass@localhost:5432/rotationengine``

Prerequisites:
    - ``pip install nbainjuries psycopg2-binary``
    - Java Runtime Environment (JRE) 8+  (tabula-py dependency)
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from urllib.parse import urlparse

import psycopg2
import psycopg2.extras

logger = logging.getLogger(__name__)

# ── SQL ──────────────────────────────────────────────────────────────

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS nba_injuries (
    player_name  VARCHAR(128) PRIMARY KEY,
    team_name    VARCHAR(64)  NOT NULL,
    injury_status VARCHAR(24) NOT NULL,
    reason       TEXT,
    last_updated TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_nba_inj_team   ON nba_injuries (team_name);
CREATE INDEX IF NOT EXISTS ix_nba_inj_status ON nba_injuries (injury_status);
"""

_UPSERT_SQL = """
INSERT INTO nba_injuries (player_name, team_name, injury_status, reason, last_updated)
VALUES (%(player_name)s, %(team_name)s, %(injury_status)s, %(reason)s, %(last_updated)s)
ON CONFLICT (player_name) DO UPDATE SET
    team_name     = EXCLUDED.team_name,
    injury_status = EXCLUDED.injury_status,
    reason        = EXCLUDED.reason,
    last_updated  = EXCLUDED.last_updated;
"""


# ── Database helpers ─────────────────────────────────────────────────


def _get_dsn() -> str:
    """Return the DATABASE_URL, converting asyncpg scheme if necessary."""
    raw = os.environ.get("DATABASE_URL", "")
    if not raw:
        raise EnvironmentError(
            "DATABASE_URL is not set.  "
            "Example: postgresql://user:pass@localhost:5432/rotationengine"
        )
    # The codebase may store an asyncpg DSN; normalise it for psycopg2.
    parsed = urlparse(raw)
    if parsed.scheme in ("postgresql+asyncpg", "postgres+asyncpg"):
        raw = raw.replace(parsed.scheme, "postgresql", 1)
    elif parsed.scheme == "postgres":
        raw = raw.replace("postgres://", "postgresql://", 1)
    return raw


def _connect() -> psycopg2.extensions.connection:
    """Open a psycopg2 connection using DATABASE_URL."""
    dsn = _get_dsn()
    conn = psycopg2.connect(dsn)
    conn.autocommit = False
    return conn


def _ensure_table(conn: psycopg2.extensions.connection) -> None:
    """Create the nba_injuries table and indexes if they don't exist."""
    with conn.cursor() as cur:
        cur.execute(_CREATE_TABLE_SQL)
    conn.commit()


# ── nbainjuries library fetch ────────────────────────────────────────


def fetch_injury_report() -> List[Dict[str, str]]:
    """Pull the latest injury report from NBA.com via ``nbainjuries``.

    Probes half-hour slots starting from the current time and walking
    back up to 3 hours.  Returns a list of dicts with keys::

        player_name, team_name, injury_status, reason

    The ``nbainjuries`` library uses tabula-py under the hood, which
    requires a Java Runtime Environment (JRE) 8+.
    """
    from nbainjuries import injury  # defer import — requires Java

    now = datetime.now()
    # Round down to the nearest 30-min boundary
    minute = 30 if now.minute >= 30 else 0
    probe = now.replace(minute=minute, second=0, microsecond=0)

    df = None
    for _ in range(6):
        try:
            df = injury.get_reportdata(probe, return_df=True)
            if df is not None and not df.empty:
                logger.info("Found report at %s", probe.strftime("%Y-%m-%d %H:%M"))
                break
        except Exception as exc:
            logger.debug("Probe %s failed: %s", probe, exc)
        probe -= timedelta(minutes=30)

    if df is None or df.empty:
        logger.warning("No injury report found for today")
        return []

    results: List[Dict[str, str]] = []
    for _, row in df.iterrows():
        player: str = str(row.get("Player Name", "")).strip()
        team: str = str(row.get("Team", "")).strip()
        status: str = str(row.get("Current Status", "")).strip()
        reason: str = str(row.get("Reason", "")).strip()

        if not player or player.lower() in ("player name", "nan"):
            continue

        # Normalise "Last, First" → "First Last"
        if "," in player:
            parts = [p.strip() for p in player.split(",", 1)]
            player = f"{parts[1]} {parts[0]}"

        results.append({
            "player_name": player,
            "team_name": team,
            "injury_status": status,
            "reason": reason,
        })

    logger.info("Parsed %d players from NBA injury PDF", len(results))
    return results


# ── Core sync function ───────────────────────────────────────────────


def sync_injuries(conn: Optional[psycopg2.extensions.connection] = None) -> str:
    """Fetch the official NBA injury report and upsert into PostgreSQL.

    Args:
        conn: An existing psycopg2 connection.  If ``None``, a new
              connection is opened (and closed) automatically using
              ``DATABASE_URL``.

    Returns:
        A human-readable summary, e.g.
        ``"Successfully updated 42 players"``.
    """
    own_conn = conn is None
    if own_conn:
        conn = _connect()

    try:
        _ensure_table(conn)

        rows = fetch_injury_report()
        if not rows:
            return "No injury data returned from nbainjuries library"

        now = datetime.now()
        upserted = 0

        with conn.cursor() as cur:
            for row in rows:
                cur.execute(_UPSERT_SQL, {
                    "player_name": row["player_name"],
                    "team_name": row["team_name"],
                    "injury_status": row["injury_status"],
                    "reason": row["reason"],
                    "last_updated": now,
                })
                upserted += 1

        conn.commit()
        summary = f"Successfully updated {upserted} players"
        logger.info(summary)
        return summary

    except Exception:
        conn.rollback()
        raise
    finally:
        if own_conn:
            conn.close()


# ── CLI ──────────────────────────────────────────────────────────────


def main() -> None:
    """CLI entry point: ``python injury_service.py --sync``."""
    parser = argparse.ArgumentParser(
        description="Sync NBA injury report from NBA.com into PostgreSQL.",
    )
    parser.add_argument(
        "--sync",
        action="store_true",
        required=True,
        help="Fetch the latest NBA injury report and upsert into the database.",
    )
    parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    )

    try:
        result = sync_injuries()
        print(result)
    except EnvironmentError as exc:
        logger.error(str(exc))
        sys.exit(1)
    except Exception:
        logger.exception("Injury sync failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
