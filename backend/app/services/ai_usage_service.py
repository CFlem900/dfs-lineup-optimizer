"""AI Usage tracking and cost monitoring service.

Logs every LLM API call to the database and provides aggregation
endpoints for cost analysis per agent, model, and time period.

ARCHITECTURE NOTE:
  log_usage_sync() is called inline from sync code (AIService.complete)
  which already runs in worker threads.  It uses a dedicated SYNC
  SQLAlchemy engine (psycopg2) so there are no event-loop dependencies,
  no daemon threads, and no race conditions with the FastAPI lifecycle.

  get_usage_summary() uses the main app's async session (it runs
  inside the main FastAPI event loop via route handlers).
"""

import logging
import os
import threading
import time as _time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

logger = logging.getLogger(__name__)

# Estimated cost per 1M tokens (USD) by model — updated 2026-03
MODEL_COSTS: Dict[str, Dict[str, float]] = {
    # Anthropic
    "claude-haiku-4-5-20251001": {"input": 1.00, "output": 5.00},
    "claude-3-5-haiku-20241022": {"input": 1.00, "output": 5.00},  # legacy (retired)
    "claude-sonnet-4-20250514": {"input": 3.00, "output": 15.00},
    # OpenAI
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "gpt-4o": {"input": 2.50, "output": 10.00},
}

# ── Sync connection pool for inline usage logging ───────────────────
# Lazily created on first log_usage_sync() call.  Uses a standard
# (non-async) engine so it works safely from any thread without
# needing an event loop.
_sync_engine = None
_sync_session_factory: Optional[sessionmaker] = None
_sync_init_lock = threading.Lock()


def _async_url_to_sync(url: str) -> str:
    """Convert an asyncpg DATABASE_URL to a psycopg2 one.

    e.g. postgresql+asyncpg://... → postgresql://...
    """
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


def _ensure_sync_pool() -> bool:
    """Lazily create the sync engine + session factory.

    Returns True if the pool is ready, False if DATABASE_URL is unset.
    Thread-safe via _sync_init_lock.
    """
    global _sync_engine, _sync_session_factory

    if _sync_session_factory is not None:
        return True

    with _sync_init_lock:
        # Double-check after acquiring lock
        if _sync_session_factory is not None:
            return True

        db_url = os.environ.get("DATABASE_URL")
        if not db_url:
            return False

        try:
            sync_url = _async_url_to_sync(db_url)
            _sync_engine = create_engine(
                sync_url,
                echo=False,
                pool_size=2,
                max_overflow=3,
                pool_pre_ping=True,
                pool_recycle=600,
            )
            _sync_session_factory = sessionmaker(
                _sync_engine,
                class_=Session,
                expire_on_commit=False,
            )
            logger.info("[AIUsage] Sync usage-logging pool created (pool_size=2)")
            return True
        except Exception as e:
            logger.warning(f"[AIUsage] Failed to create sync pool: {e}")
            _sync_engine = None
            _sync_session_factory = None
            return False


def dispose_sync_pool() -> None:
    """Dispose the sync usage-logging engine.

    Called from close_db() during application shutdown.
    """
    global _sync_engine, _sync_session_factory
    if _sync_engine is not None:
        _sync_engine.dispose()
        logger.info("[AIUsage] Sync usage pool disposed")
    _sync_engine = None
    _sync_session_factory = None


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Estimate USD cost for a single API call."""
    costs = MODEL_COSTS.get(model, {"input": 1.0, "output": 5.0})
    return (
        input_tokens * costs["input"] / 1_000_000
        + output_tokens * costs["output"] / 1_000_000
    )


def log_usage_sync(
    agent_name: str,
    model_used: str,
    provider: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    latency_ms: int = 0,
    cached: bool = False,
) -> None:
    """Write a single usage record using the sync pool.

    Called inline from AIService.complete() which already runs in
    worker threads.  No event loop needed — plain synchronous I/O.
    """
    if not _ensure_sync_pool():
        return

    from app.db.models import AIUsageLog

    try:
        cost = estimate_cost(model_used, input_tokens, output_tokens)
        with _sync_session_factory() as session:
            session.add(
                AIUsageLog(
                    agent_name=agent_name,
                    model_used=model_used,
                    provider=provider,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    estimated_cost_usd=cost,
                    latency_ms=latency_ms,
                    cached=cached,
                )
            )
            session.commit()
    except Exception as e:
        logger.warning(f"[AIUsage] Sync log failed: {e}")


class AIUsageService:
    """Reads and aggregates AI usage logs from the database."""

    _CACHE_TTL = 300  # 5 minutes

    def __init__(self):
        self._cache: Dict[str, Any] = {}
        self._cache_ts: Dict[str, float] = {}

    def _cache_get(self, key: str) -> Optional[Any]:
        if key in self._cache and (_time.time() - self._cache_ts.get(key, 0)) < self._CACHE_TTL:
            return self._cache[key]
        return None

    def _cache_set(self, key: str, value: Any) -> None:
        self._cache[key] = value
        self._cache_ts[key] = _time.time()

    def log_usage(
        self,
        agent_name: str,
        model_used: str,
        provider: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        latency_ms: int = 0,
        cached: bool = False,
    ) -> None:
        """Write a single usage record (delegates to sync pool).

        Synchronous — safe to call from any context (route handlers,
        background tasks, or worker threads).
        """
        log_usage_sync(
            agent_name=agent_name,
            model_used=model_used,
            provider=provider,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=latency_ms,
            cached=cached,
        )

    async def get_usage_summary(self, days: int = 7) -> Dict[str, Any]:
        """Aggregate usage stats over the past N days.

        Uses the main app session (called from async route context).
        """
        cache_key = f"usage_summary_{days}"
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached

        from app.db.database import is_db_available, get_session
        from app.db.models import AIUsageLog
        from sqlalchemy import select, func

        if not is_db_available():
            return {"total_cost": 0, "total_calls": 0, "by_agent": {}}

        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        try:
            async with get_session() as session:
                # Total summary
                totals = await session.execute(
                    select(
                        func.count(AIUsageLog.id).label("calls"),
                        func.coalesce(func.sum(AIUsageLog.input_tokens), 0).label("input_tokens"),
                        func.coalesce(func.sum(AIUsageLog.output_tokens), 0).label("output_tokens"),
                        func.coalesce(func.sum(AIUsageLog.estimated_cost_usd), 0).label("cost"),
                    ).where(AIUsageLog.created_at >= cutoff)
                )
                row = totals.one()

                # Per-agent breakdown
                by_agent_q = await session.execute(
                    select(
                        AIUsageLog.agent_name,
                        func.count(AIUsageLog.id).label("calls"),
                        func.coalesce(func.sum(AIUsageLog.input_tokens), 0).label("input_tokens"),
                        func.coalesce(func.sum(AIUsageLog.output_tokens), 0).label("output_tokens"),
                        func.coalesce(func.sum(AIUsageLog.estimated_cost_usd), 0).label("cost"),
                    )
                    .where(AIUsageLog.created_at >= cutoff)
                    .group_by(AIUsageLog.agent_name)
                    .order_by(func.sum(AIUsageLog.estimated_cost_usd).desc())
                )
                by_agent = {}
                for r in by_agent_q.all():
                    by_agent[r.agent_name] = {
                        "calls": r.calls,
                        "input_tokens": r.input_tokens,
                        "output_tokens": r.output_tokens,
                        "estimated_cost_usd": round(float(r.cost), 6),
                    }

                result = {
                    "days": days,
                    "total_calls": row.calls,
                    "total_input_tokens": row.input_tokens,
                    "total_output_tokens": row.output_tokens,
                    "total_cost_usd": round(float(row.cost), 6),
                    "by_agent": by_agent,
                }
                self._cache_set(cache_key, result)
                return result
        except Exception as e:
            logger.warning(f"[AIUsage] Summary query failed: {e}")
            return {"total_cost": 0, "total_calls": 0, "by_agent": {}, "error": str(e)}
