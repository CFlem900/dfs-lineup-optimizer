"""Solver Tracking Service — logs optimizer configurations and results to DB.

Writes to three tables after each ``generate_lineups()`` call:
  * ``solver_configurations`` — deduplicated config snapshots (by param hash)
  * ``lineup_batches``        — per-run metadata (slate date, timing, counts)
  * ``lineup_results``        — per-lineup projected scores + P&L (back-filled)

Also provides ``get_strategy_roi_report()`` for analytics: a SQL JOIN
across all three tables grouped by config to show total net profit,
average actual score, and total entry fees per solver configuration.
"""

import hashlib
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError

from app.db.database import get_session, is_db_available
from app.db.models import LineupBatch, LineupResult, SolverConfiguration
from app.models.lineup import MultiLineupRequest, MultiLineupResponse, OptimizedLineup

logger = logging.getLogger(__name__)


class SolverTrackingService:
    """Logs solver runs and queries ROI analytics."""

    # ──────────────────────────────────────────────────────────────
    # Config hashing — deterministic deduplication
    # ──────────────────────────────────────────────────────────────

    @staticmethod
    def _hash_config(request: MultiLineupRequest) -> str:
        """SHA-256 of the normalised parameter tuple.

        Only includes parameters that materially affect solver output.
        Two requests with the same hash produce identical solver
        behaviour (assuming the same pool).
        """
        parts = [
            f"strategy={request.strategy}",
            f"contest_type={getattr(request, 'contest_type', 'gpp')}",
            f"platform={request.platform}",
            f"sport={getattr(request, 'sport', 'nba')}",
            f"optimality_threshold={getattr(request, 'optimality_threshold', None)}",
            f"max_cumulative_ownership={getattr(request, 'max_cumulative_ownership', None)}",
            f"max_exposure={getattr(request, 'max_exposure', None)}",
            f"salary_floor_pct={getattr(request, 'salary_floor_pct', 0.98)}",
            f"max_overlap={getattr(request, 'max_overlap', 6)}",
            f"seed={getattr(request, 'seed', None)}",
        ]
        canonical = "|".join(parts)
        return hashlib.sha256(canonical.encode()).hexdigest()

    # ──────────────────────────────────────────────────────────────
    # Lineup hashing — unique identifier for an 8-player set
    # ──────────────────────────────────────────────────────────────

    @staticmethod
    def _hash_lineup(lineup: OptimizedLineup) -> str:
        """SHA-256 of sorted player IDs.

        Order-independent: the same 8 players in any slot
        arrangement produce the same hash.
        """
        pids = sorted(p.player_id for p in lineup.players)
        return hashlib.sha256(
            ",".join(str(pid) for pid in pids).encode()
        ).hexdigest()

    # ──────────────────────────────────────────────────────────────
    # Logging — called after generate_lineups() succeeds
    # ──────────────────────────────────────────────────────────────

    async def log_generation(
        self,
        request: MultiLineupRequest,
        response: MultiLineupResponse,
    ) -> Optional[int]:
        """Persist the solver run: config + batch + per-lineup results.

        Returns the ``lineup_batches.id`` on success, or ``None`` if
        the database is unavailable or the write fails.  Never raises
        — errors are logged and swallowed so lineup generation is
        never blocked by tracking failures.
        """
        if not is_db_available():
            return None

        try:
            async with get_session() as session:
                # ── 1. Upsert SolverConfiguration ─────────────────
                config_hash = self._hash_config(request)

                stmt = select(SolverConfiguration).where(
                    SolverConfiguration.config_hash == config_hash
                )
                result = await session.execute(stmt)
                config = result.scalar_one_or_none()

                if config is None:
                    config = SolverConfiguration(
                        config_hash=config_hash,
                        optimality_threshold=getattr(
                            request, "optimality_threshold", None
                        ),
                        max_cumulative_ownership=getattr(
                            request, "max_cumulative_ownership", None
                        ),
                        global_max_exposure=getattr(
                            request, "max_exposure", None
                        ),
                        salary_floor_pct=getattr(
                            request, "salary_floor_pct", 0.98
                        ),
                        max_overlap=getattr(request, "max_overlap", 6),
                        strategy=request.strategy,
                        contest_type=getattr(
                            request, "contest_type", "gpp"
                        ),
                        platform=request.platform,
                        sport=getattr(request, "sport", "nba"),
                        seed=getattr(request, "seed", None),
                    )
                    session.add(config)
                    await session.flush()  # materialise config.id

                # ── 2. Create LineupBatch ─────────────────────────
                slate_dt = None
                game_date_str = getattr(request, "game_date", None)
                if game_date_str:
                    try:
                        slate_dt = datetime.fromisoformat(
                            game_date_str
                        ).replace(tzinfo=timezone.utc)
                    except (ValueError, TypeError):
                        pass

                batch = LineupBatch(
                    config_id=config.id,
                    slate_date=slate_dt,
                    draft_group_id=getattr(
                        request, "draft_group_id", None
                    ),
                    total_lineups_requested=response.num_requested,
                    total_lineups_generated=response.num_generated,
                    generation_time_ms=response.generation_time_ms,
                    pool_size=response.pool_size,
                )
                session.add(batch)
                await session.flush()  # materialise batch.id

                # ── 3. Bulk-insert LineupResults ──────────────────
                for lineup in response.lineups:
                    lr = LineupResult(
                        batch_id=batch.id,
                        lineup_hash=self._hash_lineup(lineup),
                        projected_score=lineup.total_projected_fp,
                        total_salary=lineup.total_salary,
                        quality_grade=getattr(
                            lineup, "quality_grade", None
                        ),
                        player_ids=[
                            p.player_id for p in lineup.players
                        ],
                    )
                    session.add(lr)

                await session.commit()
                logger.info(
                    f"[SolverTracking] Logged batch {batch.id}: "
                    f"config={config.id} ({config_hash[:12]}), "
                    f"{response.num_generated} lineups"
                )
                return batch.id

        except SQLAlchemyError as e:
            logger.warning(
                f"[SolverTracking] DB write failed (non-fatal): {e}"
            )
            return None
        except Exception as e:
            logger.warning(
                f"[SolverTracking] Unexpected error (non-fatal): {e}"
            )
            return None

    # ──────────────────────────────────────────────────────────────
    # Analytics — Strategy ROI Report
    # ──────────────────────────────────────────────────────────────

    async def get_strategy_roi_report(
        self,
        sport: Optional[str] = None,
        platform: Optional[str] = None,
        min_batches: int = 1,
    ) -> List[Dict[str, Any]]:
        """Join all three tables and return per-config ROI metrics.

        Returns a list of dicts, each containing:
          - config_id, strategy, contest_type, platform, sport
          - optimality_threshold, max_cumulative_ownership,
            global_max_exposure, salary_floor_pct, max_overlap
          - total_batches          (number of generation runs)
          - total_lineups          (number of lineups across all runs)
          - total_entry_fees       (sum of entry_fee_total)
          - total_winnings         (sum of winnings_total)
          - total_net_profit       (sum of net_profit)
          - avg_projected_score    (mean of projected_score)
          - avg_actual_score       (mean of actual_score, null-safe)
          - roi_pct                (net_profit / entry_fees * 100)

        Ordered by ``total_net_profit`` descending (best configs first).

        Args:
            sport:  Filter to a specific sport (None = all).
            platform: Filter to dk/fd (None = all).
            min_batches: Minimum number of batch runs to include a
                config in the report (default 1 — show everything).
        """
        if not is_db_available():
            return []

        try:
            async with get_session() as session:
                raw_sql = text("""
                    SELECT
                        sc.id                       AS config_id,
                        sc.strategy,
                        sc.contest_type,
                        sc.platform,
                        sc.sport,
                        sc.optimality_threshold,
                        sc.max_cumulative_ownership,
                        sc.global_max_exposure,
                        sc.salary_floor_pct,
                        sc.max_overlap,
                        COUNT(DISTINCT lb.id)        AS total_batches,
                        COUNT(lr.id)                 AS total_lineups,
                        COALESCE(SUM(lr.entry_fee_total), 0)
                                                     AS total_entry_fees,
                        COALESCE(SUM(lr.winnings_total), 0)
                                                     AS total_winnings,
                        COALESCE(SUM(lr.net_profit), 0)
                                                     AS total_net_profit,
                        AVG(lr.projected_score)       AS avg_projected_score,
                        AVG(lr.actual_score)          AS avg_actual_score,
                        CASE
                            WHEN COALESCE(SUM(lr.entry_fee_total), 0) > 0
                            THEN (SUM(lr.net_profit)
                                  / SUM(lr.entry_fee_total)) * 100
                            ELSE 0
                        END                           AS roi_pct
                    FROM solver_configurations sc
                    JOIN lineup_batches lb
                        ON lb.config_id = sc.id
                    JOIN lineup_results lr
                        ON lr.batch_id = lb.id
                    WHERE 1 = 1
                        AND (:sport IS NULL OR sc.sport = :sport)
                        AND (:platform IS NULL OR sc.platform = :platform)
                    GROUP BY
                        sc.id, sc.strategy, sc.contest_type,
                        sc.platform, sc.sport,
                        sc.optimality_threshold,
                        sc.max_cumulative_ownership,
                        sc.global_max_exposure,
                        sc.salary_floor_pct,
                        sc.max_overlap
                    HAVING COUNT(DISTINCT lb.id) >= :min_batches
                    ORDER BY total_net_profit DESC
                """)

                result = await session.execute(
                    raw_sql,
                    {
                        "sport": sport,
                        "platform": platform,
                        "min_batches": min_batches,
                    },
                )
                rows = result.mappings().all()
                return [dict(row) for row in rows]

        except SQLAlchemyError as e:
            logger.warning(
                f"[SolverTracking] ROI query failed: {e}"
            )
            return []

    # ──────────────────────────────────────────────────────────────
    # Back-fill — update actual results after contest settles
    # ──────────────────────────────────────────────────────────────

    async def backfill_results(
        self,
        batch_id: int,
        results: List[Dict[str, Any]],
    ) -> int:
        """Update actual scores and P&L for a settled batch.

        Each dict in ``results`` should contain:
          - lineup_hash: str   (matches LineupResult.lineup_hash)
          - actual_score: float
          - entry_fee_total: float  (optional, default 0)
          - winnings_total: float   (optional, default 0)

        Returns the number of rows updated.
        """
        if not is_db_available() or not results:
            return 0

        try:
            async with get_session() as session:
                updated = 0
                for r in results:
                    lh = r.get("lineup_hash")
                    if not lh:
                        continue

                    stmt = select(LineupResult).where(
                        LineupResult.batch_id == batch_id,
                        LineupResult.lineup_hash == lh,
                    )
                    row = (await session.execute(stmt)).scalar_one_or_none()
                    if row is None:
                        continue

                    row.actual_score = r.get("actual_score")
                    entry_fee = r.get("entry_fee_total", 0.0)
                    winnings = r.get("winnings_total", 0.0)
                    row.entry_fee_total = entry_fee
                    row.winnings_total = winnings
                    row.net_profit = winnings - entry_fee
                    updated += 1

                await session.commit()
                logger.info(
                    f"[SolverTracking] Back-filled {updated} results "
                    f"for batch {batch_id}"
                )
                return updated

        except SQLAlchemyError as e:
            logger.warning(
                f"[SolverTracking] Backfill failed for batch "
                f"{batch_id}: {e}"
            )
            return 0

    # ──────────────────────────────────────────────────────────────
    # Utilities
    # ──────────────────────────────────────────────────────────────

    async def get_batch_detail(
        self, batch_id: int
    ) -> Optional[Dict[str, Any]]:
        """Fetch a single batch with its config and lineup results."""
        if not is_db_available():
            return None

        try:
            async with get_session() as session:
                stmt = (
                    select(LineupBatch)
                    .where(LineupBatch.id == batch_id)
                )
                batch = (await session.execute(stmt)).scalar_one_or_none()
                if batch is None:
                    return None

                return {
                    "batch_id": batch.id,
                    "config_id": batch.config_id,
                    "slate_date": (
                        batch.slate_date.isoformat()
                        if batch.slate_date else None
                    ),
                    "draft_group_id": batch.draft_group_id,
                    "total_lineups_requested": batch.total_lineups_requested,
                    "total_lineups_generated": batch.total_lineups_generated,
                    "generation_time_ms": batch.generation_time_ms,
                    "pool_size": batch.pool_size,
                    "created_at": batch.created_at.isoformat(),
                    "config": {
                        "id": batch.config.id,
                        "strategy": batch.config.strategy,
                        "contest_type": batch.config.contest_type,
                        "optimality_threshold": (
                            batch.config.optimality_threshold
                        ),
                        "max_cumulative_ownership": (
                            batch.config.max_cumulative_ownership
                        ),
                        "global_max_exposure": (
                            batch.config.global_max_exposure
                        ),
                    },
                    "lineups": [
                        {
                            "lineup_hash": lr.lineup_hash,
                            "projected_score": lr.projected_score,
                            "actual_score": lr.actual_score,
                            "total_salary": lr.total_salary,
                            "quality_grade": lr.quality_grade,
                            "entry_fee_total": lr.entry_fee_total,
                            "winnings_total": lr.winnings_total,
                            "net_profit": lr.net_profit,
                        }
                        for lr in batch.results
                    ],
                }

        except SQLAlchemyError as e:
            logger.warning(
                f"[SolverTracking] Batch detail query failed: {e}"
            )
            return None
