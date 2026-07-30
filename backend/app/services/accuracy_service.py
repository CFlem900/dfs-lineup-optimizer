"""Projection Accuracy Service.

Provides detailed accuracy metrics (MAE, RMSE, bias) broken down by
position, salary tier, stat category, and over time.  Powers the
Accuracy Dashboard UI.
"""

import logging
import math
import time as _time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

STATS = ["pts", "reb", "ast", "stl", "blk", "tov", "fg3m"]
POSITIONS = ["PG", "SG", "SF", "PF", "C"]


def _safe_mae(errors: List[float]) -> float:
    return round(sum(abs(e) for e in errors) / len(errors), 2) if errors else 0.0


def _safe_rmse(errors: List[float]) -> float:
    if not errors:
        return 0.0
    return round(math.sqrt(sum(e ** 2 for e in errors) / len(errors)), 2)


def _safe_bias(errors: List[float]) -> float:
    """Positive bias = we over-project; negative = under-project."""
    return round(sum(errors) / len(errors), 2) if errors else 0.0


class AccuracyService:
    """Computes projection accuracy metrics from historical data."""

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

    async def get_accuracy_summary(self, days: int = 30, sport: str = "nba") -> Dict[str, Any]:
        """Overall accuracy summary: MAE, RMSE, bias for FP and minutes.

        Also includes per-stat breakdown.
        """
        cache_key = f"summary_{sport}_{days}"
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached

        from app.db.database import is_db_available, get_session
        from app.db.models import PlayerMinutesHistory

        if not is_db_available():
            return {"error": "Database not available", "records": 0}

        from sqlalchemy import select

        cutoff = datetime.now(timezone.utc) - timedelta(days=days)

        async with get_session() as session:
            stmt = (
                select(PlayerMinutesHistory)
                .where(PlayerMinutesHistory.actual_minutes.isnot(None))
                .where(PlayerMinutesHistory.projected_minutes.isnot(None))
                .where(PlayerMinutesHistory.game_date >= cutoff)
                .where(PlayerMinutesHistory.sport == sport)
            )
            result = await session.execute(stmt)
            rows = result.scalars().all()

        if not rows:
            return {
                "records": 0,
                "period_days": days,
                "minutes": {"mae": 0.0, "rmse": 0.0, "bias": 0.0, "samples": 0},
                "fantasy_points": {"mae": 0.0, "rmse": 0.0, "bias": 0.0, "samples": 0},
                "per_stat": {},
            }

        # Minutes errors
        min_errors = [
            (r.projected_minutes or 0) - (r.actual_minutes or 0) for r in rows
        ]

        # FP errors (where available)
        fp_errors = [
            (r.dk_projected_fp or 0) - (r.dk_actual_fp or 0)
            for r in rows
            if r.dk_projected_fp is not None and r.dk_actual_fp is not None
        ]

        # Per-stat errors
        stat_accuracy = {}
        for stat in STATS:
            proj_col = f"projected_{stat}"
            actual_col = f"actual_{stat}"
            errors = [
                (getattr(r, proj_col, 0) or 0) - (getattr(r, actual_col, 0) or 0)
                for r in rows
                if getattr(r, proj_col, None) is not None
                and getattr(r, actual_col, None) is not None
            ]
            if errors:
                stat_accuracy[stat] = {
                    "mae": _safe_mae(errors),
                    "rmse": _safe_rmse(errors),
                    "bias": _safe_bias(errors),
                    "samples": len(errors),
                }

        result = {
            "records": len(rows),
            "period_days": days,
            "minutes": {
                "mae": _safe_mae(min_errors),
                "rmse": _safe_rmse(min_errors),
                "bias": _safe_bias(min_errors),
            },
            "fantasy_points": {
                "mae": _safe_mae(fp_errors),
                "rmse": _safe_rmse(fp_errors),
                "bias": _safe_bias(fp_errors),
                "samples": len(fp_errors),
            },
            "per_stat": stat_accuracy,
        }
        self._cache_set(cache_key, result)
        return result

    async def get_accuracy_by_position(self, days: int = 30, sport: str = "nba") -> Dict[str, Any]:
        """Accuracy broken down by player position."""
        cache_key = f"by_position_{sport}_{days}"
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached

        from app.db.database import is_db_available, get_session
        from app.db.models import PlayerMinutesHistory
        from sqlalchemy import select

        if not is_db_available():
            return {"error": "Database not available"}

        cutoff = datetime.now(timezone.utc) - timedelta(days=days)

        async with get_session() as session:
            stmt = (
                select(PlayerMinutesHistory)
                .where(PlayerMinutesHistory.actual_minutes.isnot(None))
                .where(PlayerMinutesHistory.projected_minutes.isnot(None))
                .where(PlayerMinutesHistory.game_date >= cutoff)
                .where(PlayerMinutesHistory.sport == sport)
            )
            result = await session.execute(stmt)
            rows = result.scalars().all()

        by_pos: Dict[str, List[float]] = {p: [] for p in POSITIONS}
        fp_by_pos: Dict[str, List[float]] = {p: [] for p in POSITIONS}

        for r in rows:
            pos = (r.position or "").upper()
            if pos not in by_pos:
                continue
            by_pos[pos].append(
                (r.projected_minutes or 0) - (r.actual_minutes or 0)
            )
            if r.dk_projected_fp is not None and r.dk_actual_fp is not None:
                fp_by_pos[pos].append(
                    (r.dk_projected_fp or 0) - (r.dk_actual_fp or 0)
                )

        positions = {}
        for pos in POSITIONS:
            errors = by_pos[pos]
            fp_errors = fp_by_pos[pos]
            positions[pos] = {
                "minutes_mae": _safe_mae(errors),
                "minutes_bias": _safe_bias(errors),
                "fp_mae": _safe_mae(fp_errors),
                "fp_bias": _safe_bias(fp_errors),
                "samples": len(errors),
            }

        result = {"period_days": days, "positions": positions}
        self._cache_set(cache_key, result)
        return result

    async def get_accuracy_by_salary_tier(self, days: int = 30, sport: str = "nba") -> Dict[str, Any]:
        """Accuracy broken down by DK salary tier."""
        cache_key = f"by_salary_{sport}_{days}"
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached

        from app.db.database import is_db_available, get_session
        from app.db.models import PlayerMinutesHistory
        from sqlalchemy import select

        if not is_db_available():
            return {"error": "Database not available"}

        cutoff = datetime.now(timezone.utc) - timedelta(days=days)

        async with get_session() as session:
            stmt = (
                select(PlayerMinutesHistory)
                .where(PlayerMinutesHistory.actual_minutes.isnot(None))
                .where(PlayerMinutesHistory.projected_minutes.isnot(None))
                .where(PlayerMinutesHistory.game_date >= cutoff)
                .where(PlayerMinutesHistory.sport == sport)
            )
            result = await session.execute(stmt)
            rows = result.scalars().all()

        tiers: Dict[str, List[float]] = {"high": [], "mid": [], "value": []}
        fp_tiers: Dict[str, List[float]] = {"high": [], "mid": [], "value": []}

        for r in rows:
            sal = r.dk_salary or 0
            if sal >= 8000:
                tier = "high"
            elif sal >= 5000:
                tier = "mid"
            else:
                tier = "value"

            tiers[tier].append(
                (r.projected_minutes or 0) - (r.actual_minutes or 0)
            )
            if r.dk_projected_fp is not None and r.dk_actual_fp is not None:
                fp_tiers[tier].append(
                    (r.dk_projected_fp or 0) - (r.dk_actual_fp or 0)
                )

        salary_tiers = {}
        for tier in ["high", "mid", "value"]:
            errors = tiers[tier]
            fp_errors = fp_tiers[tier]
            salary_tiers[tier] = {
                "minutes_mae": _safe_mae(errors),
                "minutes_bias": _safe_bias(errors),
                "fp_mae": _safe_mae(fp_errors),
                "fp_bias": _safe_bias(fp_errors),
                "samples": len(errors),
            }

        result = {"period_days": days, "salary_tiers": salary_tiers}
        self._cache_set(cache_key, result)
        return result

    async def get_accuracy_timeline(self, days: int = 90, sport: str = "nba") -> Dict[str, Any]:
        """Daily MAE trend for time series charts."""
        cache_key = f"timeline_{sport}_{days}"
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached

        from app.db.database import is_db_available, get_session
        from app.db.models import PlayerMinutesHistory
        from sqlalchemy import select

        if not is_db_available():
            return {"error": "Database not available"}

        cutoff = datetime.now(timezone.utc) - timedelta(days=days)

        async with get_session() as session:
            stmt = (
                select(PlayerMinutesHistory)
                .where(PlayerMinutesHistory.actual_minutes.isnot(None))
                .where(PlayerMinutesHistory.projected_minutes.isnot(None))
                .where(PlayerMinutesHistory.game_date >= cutoff)
                .where(PlayerMinutesHistory.sport == sport)
                .order_by(PlayerMinutesHistory.game_date)
            )
            result = await session.execute(stmt)
            rows = result.scalars().all()

        # Group by date
        by_date: Dict[str, List[float]] = {}
        fp_by_date: Dict[str, List[float]] = {}
        for r in rows:
            d = r.game_date.strftime("%Y-%m-%d") if r.game_date else "unknown"
            by_date.setdefault(d, []).append(
                abs((r.projected_minutes or 0) - (r.actual_minutes or 0))
            )
            if r.dk_projected_fp is not None and r.dk_actual_fp is not None:
                fp_by_date.setdefault(d, []).append(
                    abs((r.dk_projected_fp or 0) - (r.dk_actual_fp or 0))
                )

        timeline = []
        for d in sorted(by_date.keys()):
            errors = by_date[d]
            fp_errors = fp_by_date.get(d, [])
            timeline.append({
                "date": d,
                "minutes_mae": round(sum(errors) / len(errors), 2) if errors else 0,
                "fp_mae": round(sum(fp_errors) / len(fp_errors), 2) if fp_errors else 0,
                "samples": len(errors),
            })

        result = {"period_days": days, "timeline": timeline}
        self._cache_set(cache_key, result)
        return result

    async def get_player_accuracy(
        self, player_id: int, days: int = 90, sport: str = "nba"
    ) -> Dict[str, Any]:
        """Individual player projection history."""
        cache_key = f"player_{sport}_{player_id}_{days}"
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached

        from app.db.database import is_db_available, get_session
        from app.db.models import PlayerMinutesHistory
        from sqlalchemy import select

        if not is_db_available():
            return {"error": "Database not available"}

        cutoff = datetime.now(timezone.utc) - timedelta(days=days)

        async with get_session() as session:
            stmt = (
                select(PlayerMinutesHistory)
                .where(PlayerMinutesHistory.player_id == player_id)
                .where(PlayerMinutesHistory.actual_minutes.isnot(None))
                .where(PlayerMinutesHistory.game_date >= cutoff)
                .where(PlayerMinutesHistory.sport == sport)
                .order_by(PlayerMinutesHistory.game_date.desc())
                .limit(50)
            )
            result = await session.execute(stmt)
            rows = result.scalars().all()

        if not rows:
            return {"player_id": player_id, "records": 0}

        min_errors = [
            (r.projected_minutes or 0) - (r.actual_minutes or 0) for r in rows
        ]
        fp_errors = [
            (r.dk_projected_fp or 0) - (r.dk_actual_fp or 0)
            for r in rows
            if r.dk_projected_fp is not None and r.dk_actual_fp is not None
        ]

        games = []
        for r in rows:
            games.append({
                "game_date": r.game_date.isoformat() if r.game_date else None,
                "projected_minutes": r.projected_minutes,
                "actual_minutes": r.actual_minutes,
                "projected_fp": r.dk_projected_fp,
                "actual_fp": r.dk_actual_fp,
                "position": r.position,
                "dk_salary": r.dk_salary,
            })

        result = {
            "player_id": player_id,
            "player_name": rows[0].player_name if rows else "",
            "records": len(rows),
            "minutes_mae": _safe_mae(min_errors),
            "minutes_bias": _safe_bias(min_errors),
            "fp_mae": _safe_mae(fp_errors),
            "fp_bias": _safe_bias(fp_errors),
            "games": games,
        }
        self._cache_set(cache_key, result)
        return result
