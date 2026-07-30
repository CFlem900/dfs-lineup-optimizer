"""CLI entry point for syncing NBA injury data to PostgreSQL.

Fetches the official NBA.com injury report via the ``nbainjuries``
library and upserts all player records into the ``nba_injuries`` table.

Usage::

    python scripts/sync_injuries.py --sync

Prerequisites:
    - DATABASE_URL environment variable must be set
    - Java Runtime Environment (JRE) 8+ (required by tabula-py, which
      the nbainjuries library depends on for PDF parsing)

The script can be run on a cron schedule (e.g. every 15 minutes on game
days) to keep the injury table current for the Monte Carlo simulator.
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

# Ensure the backend package is importable when run from the scripts/ dir
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db.database import init_db, close_db
from app.services.injury_service import InjuryService


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger("sync_injuries")


async def _run_sync() -> None:
    """Initialise the DB, run the sync, and tear down cleanly."""
    db_ok = await init_db()
    if not db_ok:
        logger.error(
            "Database initialisation failed. "
            "Ensure DATABASE_URL is set and PostgreSQL is reachable."
        )
        sys.exit(1)

    try:
        svc = InjuryService()
        summary = await svc.sync_injuries()
        logger.info(summary)
    except Exception:
        logger.exception("Injury sync failed")
        sys.exit(1)
    finally:
        await close_db()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sync NBA injury report to PostgreSQL.",
    )
    parser.add_argument(
        "--sync",
        action="store_true",
        required=True,
        help="Fetch the latest NBA injury report and upsert into the database.",
    )
    args = parser.parse_args()

    if args.sync:
        asyncio.run(_run_sync())


if __name__ == "__main__":
    main()
