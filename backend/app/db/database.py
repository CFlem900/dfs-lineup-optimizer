"""Async database engine and session factory for RotationEngine.

Uses SQLAlchemy 2.0 asyncio extension with asyncpg driver.
The DATABASE_URL environment variable controls the connection.

If DATABASE_URL is not set, the database layer is silently disabled —
the app still works with full functionality, just without historical
data logging.
"""

import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Optional

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

logger = logging.getLogger(__name__)

DATABASE_URL: Optional[str] = os.environ.get("DATABASE_URL")

engine: Optional[AsyncEngine] = None
async_session_factory: Optional[async_sessionmaker] = None


def _create_engine() -> Optional[AsyncEngine]:
    """Create the async engine if DATABASE_URL is configured."""
    if not DATABASE_URL:
        logger.info("DATABASE_URL not set — database layer disabled")
        return None

    from app.config import get_settings
    settings = get_settings()

    return create_async_engine(
        DATABASE_URL,
        echo=False,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_pre_ping=settings.db_pool_pre_ping,
    )


async def init_db() -> bool:
    """Initialise the database: create engine, session factory, and tables.

    Returns True if the database was connected successfully, False otherwise.
    Safe to call even when DATABASE_URL is not set.
    """
    global engine, async_session_factory

    engine = _create_engine()
    if engine is None:
        return False

    async_session_factory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    try:
        async with engine.begin() as conn:
            from app.db.models import Base
            await conn.run_sync(Base.metadata.create_all)
        logger.info(
            "Database tables created/verified (fallback mode). "
            "For production, use: alembic upgrade head"
        )
        return True
    except Exception as e:
        logger.warning(f"Database initialisation failed (non-fatal): {e}")
        engine = None
        async_session_factory = None
        return False


async def seed_teams() -> int:
    """Populate the teams table with all 30 NBA teams from nba_api.

    Uses an upsert-style approach: only inserts teams that don't already
    exist (matched by primary key).  Returns the number of teams inserted.
    Safe to call on every startup — it's a no-op when the table is already
    populated.
    """
    if async_session_factory is None:
        return 0

    from nba_api.stats.static import teams as nba_teams
    from app.db.models import TeamRecord

    all_teams = nba_teams.get_teams()

    inserted = 0
    async with async_session_factory() as session:
        try:
            for t in all_teams:
                existing = await session.get(TeamRecord, t["id"])
                if existing is None:
                    session.add(TeamRecord(
                        id=t["id"],
                        full_name=t["full_name"],
                        abbreviation=t["abbreviation"],
                        city=t["city"],
                        nickname=t["nickname"],
                        sport="nba",
                    ))
                    inserted += 1
            await session.commit()
            if inserted:
                logger.info(f"Seeded {inserted} NBA teams into the database")
            else:
                logger.info("NBA teams table already populated — seed skipped")
        except Exception as e:
            await session.rollback()
            logger.warning(f"NBA team seeding failed (non-fatal): {e}")
            inserted = 0

    return inserted


async def seed_cbb_teams() -> int:
    """Populate the teams table with NCAA D1 basketball teams from ESPN.

    Fetches all ~360 Division 1 teams from the ESPN API and inserts them
    with ``sport='cbb'``.  Uses ESPN team IDs as primary keys (offset by
    a large number to avoid collision with NBA team IDs).

    Safe to call on every startup — skips already-seeded CBB teams.
    Returns the number of new teams inserted.
    """
    if async_session_factory is None:
        return 0

    from sqlalchemy import select
    from app.db.models import TeamRecord

    # Check if CBB teams are already seeded
    async with async_session_factory() as session:
        try:
            result = await session.execute(
                select(TeamRecord).where(TeamRecord.sport == "cbb").limit(1)
            )
            if result.scalars().first() is not None:
                logger.info("CBB teams already seeded — skipping")
                return 0
        except Exception as e:
            logger.warning(f"CBB seed check failed: {e}")
            return 0

    # Fetch teams from ESPN API
    try:
        from app.services.cbb_teams import CBBTeamRegistry
        registry = CBBTeamRegistry()
        espn_teams = registry.get_all_teams()
    except Exception as e:
        logger.warning(f"CBB team fetch from ESPN failed (non-fatal): {e}")
        return 0

    if not espn_teams:
        logger.warning("No CBB teams returned from ESPN — seed skipped")
        return 0

    # ESPN team IDs are typically in the 1-9999 range which can collide
    # with NBA team IDs (1-30).  We offset CBB IDs by 100000 for DB
    # primary key uniqueness.
    CBB_ID_OFFSET = 100_000

    inserted = 0
    async with async_session_factory() as session:
        try:
            for t in espn_teams:
                espn_id = t.get("id")
                if not espn_id:
                    continue

                db_id = int(espn_id) + CBB_ID_OFFSET
                existing = await session.get(TeamRecord, db_id)
                if existing is None:
                    session.add(TeamRecord(
                        id=db_id,
                        full_name=t.get("full_name", t.get("displayName", "")),
                        abbreviation=t.get("abbreviation", ""),
                        city=t.get("location", ""),
                        nickname=t.get("name", ""),
                        sport="cbb",
                    ))
                    inserted += 1

            await session.commit()
            if inserted:
                logger.info(f"Seeded {inserted} CBB teams into the database")
            else:
                logger.info("CBB teams already present — seed skipped")
        except Exception as e:
            await session.rollback()
            logger.warning(f"CBB team seeding failed (non-fatal): {e}")
            inserted = 0

    return inserted


async def close_db():
    """Dispose of the engine connection pool (main + sync usage pool)."""
    global engine, async_session_factory
    if engine:
        await engine.dispose()
        logger.info("Database connection pool disposed")
    engine = None
    async_session_factory = None

    # Also dispose the sync AI-usage logging pool
    try:
        from app.services.ai_usage_service import dispose_sync_pool
        dispose_sync_pool()
    except Exception as e:
        logger.warning(f"Sync usage pool cleanup failed (non-fatal): {e}")



@asynccontextmanager
async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Yield an async session for database operations.

    Usage::

        async with get_session() as session:
            session.add(record)
            await session.commit()

    Raises RuntimeError if the database layer is not initialised.
    """
    if async_session_factory is None:
        raise RuntimeError(
            "Database not initialised. Ensure DATABASE_URL is set and "
            "init_db() was called during startup."
        )

    async with async_session_factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


def is_db_available() -> bool:
    """Check whether the database layer is active."""
    return engine is not None and async_session_factory is not None
