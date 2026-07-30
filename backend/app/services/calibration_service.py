"""Calibration Application Service.

Reads learned calibration adjustments from both backtesting and
tournament analysis, applies category-aware magnitude caps, and
provides them to the rotation engine and lineup optimizer.

This is the bridge between the learning agents and the production
pipeline.  Adjustments auto-apply with no manual review needed.

Agent 9 Logic — Team-Specific Injury Offsets:

    Replaces static Effective Factors (Out: 0.00, Doubtful: 0.15,
    GTD: 0.66, Questionable: 0.81) with learned team-level offsets.

    For each team × status pair, queries ``player_minutes_history``
    joined with ``nba_injuries`` to compute the Observed Availability:

        observed_ratio = AVG(actual_minutes / baseline_minutes)
        expected_factor = static factor (e.g. 0.66 for GTD)
        offset = observed_ratio / expected_factor   (capped ±15%)

    A team whose GTD players historically play 72% of baseline
    (vs the static 66% expectation) would get offset ≈ 1.09.
"""

import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

MAX_ADJUSTMENT = 0.15  # +-15% cap
MIN_MULTIPLIER = 1.0 - MAX_ADJUSTMENT  # 0.85
MAX_MULTIPLIER = 1.0 + MAX_ADJUSTMENT  # 1.15

# Validation bounds — any calibration outside this range is treated as corrupt
# data and the getter returns the default value (typically 1.0).
VALID_MIN = 0.5
VALID_MAX = 2.0

# Minimum sample size for team-specific injury offset to be trusted.
# Below this, the offset is attenuated toward 1.0 (neutral).
_MIN_OFFSET_SAMPLES = 10

# Category-specific clamp bounds — different calibration types need different ranges.
# Position biases vary more than stat rates; ownership factors need wide range.
_CATEGORY_CLAMP_BOUNDS: Dict[str, Tuple[float, float]] = {
    "position": (0.75, 1.25),          # ±25% — positions have wide projection variance
    "salary_tier": (0.85, 1.15),       # ±15% — correlated with projection accuracy
    "ownership_factor": (0.50, 2.00),  # ±100% — ownership is highly variable
    "ownership_leverage": (0.50, 2.00),
    "stat_rate": (0.90, 1.10),         # ±10% — stable per-minute production
    "dvp_sensitivity": (0.80, 1.20),   # ±20% — matchup effect strength
    "noise_sigma": (0.70, 1.40),       # ±30-40% — simulation noise tuning
    "pace_sensitivity": (0.80, 1.20),  # ±20%
    "shot_rate": (0.85, 1.15),         # ±15%
    "game_context": (0.85, 1.15),      # ±15%
    "stacking": (0.85, 1.15),          # ±15%
}

# Static base factors that the offsets are relative to.
_BASE_INJURY_FACTORS: Dict[str, float] = {
    "Out": 0.00,
    "Doubtful": 0.15,
    "GTD": 0.66,
    "Game Time Decision": 0.66,
    "Questionable": 0.81,
}

# SQL: Compute observed availability ratio per team_id × injury_status.
# Joins player_minutes_history (actual minutes) with nba_injuries (status
# at game time).  Only considers games in the last 90 days with non-zero
# baseline to avoid division-by-zero and stale data.
_TEAM_INJURY_OFFSET_SQL = """
WITH injury_games AS (
    SELECT
        pmh.team_id,
        inj.injury_status,
        pmh.actual_minutes,
        pmh.baseline_minutes,
        pmh.actual_minutes / NULLIF(pmh.baseline_minutes, 0) AS availability_ratio
    FROM player_minutes_history pmh
    JOIN nba_injuries inj
        ON pmh.player_name = inj.player_name
    WHERE pmh.sport = 'nba'
      AND pmh.baseline_minutes > 0
      AND pmh.game_date >= (CURRENT_DATE - INTERVAL '90 days')
      AND inj.injury_status IN ('Out', 'Doubtful', 'GTD', 'Game Time Decision', 'Questionable')
)
SELECT
    team_id,
    injury_status,
    AVG(availability_ratio) AS observed_ratio,
    COUNT(*)                AS sample_size
FROM injury_games
WHERE availability_ratio IS NOT NULL
GROUP BY team_id, injury_status
HAVING COUNT(*) >= 3
ORDER BY team_id, injury_status;
"""


def clamp_adjustment(value: float, category: str = None) -> float:
    """Clamp a multiplier to the appropriate range for its category."""
    if category:
        # Match category prefix against bounds dict
        for prefix, (lo, hi) in _CATEGORY_CLAMP_BOUNDS.items():
            if category.startswith(prefix):
                return max(lo, min(hi, value))
    return max(MIN_MULTIPLIER, min(MAX_MULTIPLIER, value))


def _confidence_scale(clamped: float, confidence: float) -> float:
    """Scale a calibration adjustment by its confidence level.

    Instead of applying every adjustment at full strength, this attenuates
    low-confidence adjustments toward 1.0 (no effect):

        effective = 1.0 + (clamped - 1.0) × confidence

    Examples:
        clamped=0.95, confidence=1.0  → 0.95  (full -5%)
        clamped=0.95, confidence=0.5  → 0.975 (half: -2.5%)
        clamped=1.10, confidence=0.3  → 1.03  (30% of +10%)
    """
    if confidence >= 0.99:
        return clamped  # Fast path: full confidence
    deviation = clamped - 1.0
    return round(1.0 + deviation * confidence, 6)


class CalibrationService:
    """Reads and applies calibration adjustments from DB."""

    def __init__(self):
        self._cache: Optional[Dict[str, float]] = None
        self._cache_by_category: Optional[Dict[str, Dict[str, float]]] = None

    async def load_calibrations(self) -> Dict[str, float]:
        """Load all active calibrations from both tables, merge, and cap.

        Tournament calibrations take precedence over backtest calibrations
        when keys overlap (more specific signal).

        Returns dict of calibration_key -> capped multiplier.
        """
        from app.db.database import is_db_available, get_session

        if not is_db_available():
            return {}

        calibrations: Dict[str, float] = {}
        by_category: Dict[str, Dict[str, float]] = {}

        try:
            from sqlalchemy import select, or_
            from app.db.models import BacktestCalibration, TournamentCalibration

            now = datetime.now(timezone.utc)

            async with get_session() as session:
                # Load backtest calibrations first (lower priority)
                # Filter: is_active=True AND (no expiry OR not yet expired)
                bt_stmt = select(BacktestCalibration).where(
                    BacktestCalibration.is_active == True,  # noqa: E712
                    or_(
                        BacktestCalibration.expires_at.is_(None),
                        BacktestCalibration.expires_at > now,
                    ),
                )
                bt_result = await session.execute(bt_stmt)
                for row in bt_result.scalars().all():
                    clamped = clamp_adjustment(row.adjustment_value, getattr(row, 'category', None))
                    # Time decay: exponentially attenuate older calibrations
                    if hasattr(row, 'analyzed_at') and row.analyzed_at:
                        days_old = (now - row.analyzed_at).total_seconds() / 86400
                        decay = math.exp(-0.05 * days_old)  # ~5% decay per day
                        clamped = 1.0 + (clamped - 1.0) * decay
                    calibrations[row.calibration_key] = clamped

                # Load tournament calibrations (higher priority, overwrites)
                # Scale each adjustment by its confidence level so low-confidence
                # adjustments are attenuated toward 1.0 (neutral).
                tc_stmt = select(TournamentCalibration).where(
                    TournamentCalibration.is_active == True,  # noqa: E712
                    or_(
                        TournamentCalibration.expires_at.is_(None),
                        TournamentCalibration.expires_at > now,
                    ),
                )
                tc_result = await session.execute(tc_stmt)
                for row in tc_result.scalars().all():
                    if row.category == "gpp_constraint":
                        # GPP constraints are raw values, not multipliers —
                        # skip clamping and confidence scaling.
                        effective = row.adjustment_value
                    else:
                        clamped = clamp_adjustment(row.adjustment_value, row.category)
                        if hasattr(row, 'analyzed_at') and row.analyzed_at:
                            days_old = (now - row.analyzed_at).total_seconds() / 86400
                            decay = math.exp(-0.05 * days_old)
                            clamped = 1.0 + (clamped - 1.0) * decay
                        confidence = row.confidence if row.confidence is not None else 0.5
                        effective = _confidence_scale(clamped, confidence)
                    calibrations[row.calibration_key] = effective
                    if row.category not in by_category:
                        by_category[row.category] = {}
                    by_category[row.category][row.calibration_key] = effective

            self._cache = calibrations
            self._cache_by_category = by_category
            logger.info(
                f"[Calibration] Loaded {len(calibrations)} active calibrations"
            )

        except Exception as e:
            logger.warning(f"[Calibration] Failed to load: {e}")
            calibrations = {}

        return calibrations

    def get_all(self) -> Dict[str, float]:
        """Return cached calibrations (call load_calibrations first)."""
        return self._cache or {}

    def _validated_get(self, key: str, default: float = 1.0) -> float:
        """Get a calibration value with bounds validation.

        If the value is outside [VALID_MIN, VALID_MAX], log a warning
        and return the default.  Protects against corrupt DB data.
        """
        value = self.get_all().get(key, default)
        if value == default:
            return default
        if not (VALID_MIN <= value <= VALID_MAX):
            logger.warning(
                f"[Calibration] Invalid value for '{key}': {value} "
                f"(outside [{VALID_MIN}, {VALID_MAX}]), using default {default}"
            )
            return default
        return value

    def get_by_category(self, category: str) -> Dict[str, float]:
        """Return calibrations for a specific category."""
        if not self._cache_by_category:
            return {}
        return self._cache_by_category.get(category, {})

    def get_position_bias(self, position: str) -> float:
        """Get position-specific bias multiplier (e.g. 'PG' -> 1.03)."""
        key = f"position_{position.upper()}_bias"
        return self._validated_get(key, 1.0)

    def get_salary_tier_adjustment(self, tier_key: str) -> float:
        """Get salary tier preference multiplier.

        tier_key: 'high' ($9K+), 'mid' ($6-9K), 'value' (<$6K)
        """
        return self._validated_get(f"salary_tier_{tier_key}", 1.0)

    def get_stacking_weight(self, stack_key: str) -> float:
        """Get stacking weight multiplier."""
        return self._validated_get(f"stacking_{stack_key}", 1.0)

    def get_ownership_threshold_adj(self) -> float:
        """Get ownership threshold adjustment."""
        return self._validated_get("ownership_threshold_adj", 1.0)

    def get_ownership_leverage_alpha(self, strategy: str = "gpp") -> float:
        """Get the power-law exponent for the ownership leverage curve.

        strategy: 'contrarian' uses a separate (stronger) key; all others use 'gpp'.
        """
        if strategy == "contrarian":
            return self._validated_get("ownership_leverage_contrarian_alpha", 1.0)
        return self._validated_get("ownership_leverage_alpha", 1.0)

    def get_ownership_leverage_baseline(self, strategy: str = "gpp") -> float:
        """Get the baseline ownership% where the leverage multiplier equals 1.0."""
        if strategy == "contrarian":
            return self._validated_get("ownership_leverage_contrarian_baseline", 1.0)
        return self._validated_get("ownership_leverage_baseline", 1.0)

    def get_ownership_factor_weight(self, factor: str) -> Optional[float]:
        """Get a learned ownership factor weight multiplier.

        Returns the learned multiplier for the given factor, or None
        if no learned value exists.  The caller applies this as:
            effective_weight = default_weight * multiplier

        Factor names: value, salary, game_env, expert, projection,
        star_premium, scarcity, minutes, b2b, spread, multi_position,
        injury_benefit.
        """
        key = f"ownership_factor_{factor}_weight"
        val = self.get_all().get(key)
        if val is not None and VALID_MIN <= val <= VALID_MAX:
            return val
        return None

    def get_all_ownership_weights(self) -> Dict[str, float]:
        """Return all learned ownership factor weight multipliers.

        Returns dict of factor_name → multiplier.  Only includes factors
        that have learned values in the calibration store.
        """
        prefix = "ownership_factor_"
        suffix = "_weight"
        result: Dict[str, float] = {}
        for key, val in self.get_all().items():
            if key.startswith(prefix) and key.endswith(suffix):
                factor = key[len(prefix):-len(suffix)]
                if VALID_MIN <= val <= VALID_MAX:
                    result[factor] = val
        return result

    def get_game_context_multiplier(self, context_key: str) -> float:
        """Get game context multiplier (e.g. 'blowout', 'b2b', 'high_total')."""
        return self._validated_get(f"game_context_{context_key}", 1.0)

    # ── Projection-layer calibrations ─────────────────────────────────

    def get_stat_rate_adjustment(self, stat: str) -> float:
        """Per-stat rate multiplier (e.g. 'pts' -> 'stat_rate_pts').

        Adjusts per-minute stat production rates in DFSService.
        """
        return self._validated_get(f"stat_rate_{stat}", 1.0)

    def get_dvp_sensitivity(self) -> float:
        """Global DvP matchup factor strength adjustment.

        < 1.0 means our DvP adjustments are too strong (dampen them).
        > 1.0 means our DvP adjustments are too weak (amplify them).
        Applied as: adjusted_dvp = 1.0 + (raw_dvp - 1.0) * sensitivity
        """
        return self._validated_get("dvp_sensitivity", 1.0)

    def get_dvp_sensitivity_for_stat(self, stat: str) -> float:
        """Per-stat DvP sensitivity. Falls back to global, then 1.0.

        When BDL advanced calibration data is available, each stat
        (pts, reb, ast, stl, blk, tov, fg3m) gets its own DvP
        sensitivity multiplier reflecting how much defensive matchup
        actually affects that stat category.
        """
        per_stat = self._validated_get(f"dvp_sensitivity_{stat}", None)
        if per_stat is not None:
            return per_stat
        return self.get_dvp_sensitivity()

    def get_shot_rate_adjustment(self, rate_type: str) -> float:
        """Shot attempt rate calibration (fg3a, fga, fta). Default 1.0.

        Adjusts per-minute attempt rates in the decomposed projection
        pipeline based on observed bias between projected and actual
        shot attempt rates.
        """
        return self._validated_get(f"shot_rate_{rate_type}", 1.0)

    def get_pace_sensitivity_adjustment(self, stat: str) -> float:
        """Per-stat pace sensitivity adjustment.

        Scales the pace sensitivity coefficient for a given stat.
        """
        return self._validated_get(f"pace_sensitivity_{stat}", 1.0)

    def get_salary_tier_projection_adj(self, tier: str) -> float:
        """Projection-level salary tier bias correction.

        tier: 'high' ($8K+), 'mid' ($5-8K), 'value' (<$5K)
        Adjusts raw projections to correct systematic over/under-projection
        by salary tier (separate from lineup selection tier adjustments).
        """
        return self._validated_get(f"salary_tier_{tier}_projection", 1.0)

    def get_minutes_blend_adjustment(self, component: str) -> float:
        """Adjust minutes blend weights.

        component: 'season' or 'recent'
        Scales the weight of that component in the baseline blend.
        """
        return self._validated_get(f"minutes_blend_{component}_weight", 1.0)

    def get_noise_overrides(self) -> Optional[Dict[str, float]]:
        """Get learned per-stat noise sigma multipliers for simulation.

        Returns a dict of stat_name -> sigma_multiplier, or None if
        no noise calibrations exist.  These multiply the default
        STAT_NOISE_SIGMA values in the simulation engine.
        """
        overrides = {}
        for stat in ("pts", "reb", "ast", "stl", "blk", "tov", "fg3m"):
            key = f"noise_sigma_{stat}"
            value = self.get_all().get(key)
            if value is not None:
                # Validate the override value
                if VALID_MIN <= value <= VALID_MAX:
                    overrides[stat] = value
                else:
                    logger.warning(
                        f"[Calibration] Invalid noise override for '{key}': {value}, skipping"
                    )
        return overrides if overrides else None

    # ── Agent 9: Team-Specific Injury Offsets ────────────────────────

    async def compute_team_injury_offsets(self) -> Dict[Tuple[int, str], float]:
        """Compute per-team injury factor offsets from historical data.

        Queries ``player_minutes_history`` joined with ``nba_injuries``
        to calculate observed availability for each (team_id, status) pair.

        Returns:
            Dict mapping (team_id, status) → offset multiplier (0.85–1.15).
            Example: ``{(1610612737, "GTD"): 1.09}`` means ATL's GTD
            players historically play 9% more than the static 0.66 factor
            predicts, so the rotation engine should use ``0.66 × 1.09``.
        """
        from app.db.database import is_db_available, get_session
        from sqlalchemy import text

        if not is_db_available():
            return {}

        offsets: Dict[Tuple[int, str], float] = {}

        try:
            async with get_session() as session:
                result = await session.execute(text(_TEAM_INJURY_OFFSET_SQL))
                rows = result.fetchall()

            for row in rows:
                team_id, status, observed_ratio, sample_size = (
                    row[0], row[1], float(row[2]), int(row[3]),
                )

                base_factor = _BASE_INJURY_FACTORS.get(status)
                if base_factor is None or base_factor == 0.0:
                    # Can't compute meaningful offset for "Out" (factor=0.0)
                    continue

                # Raw offset: how much does the team deviate from expected?
                raw_offset = observed_ratio / base_factor

                # Confidence attenuation: shrink toward 1.0 when sample is small
                confidence = min(1.0, sample_size / _MIN_OFFSET_SAMPLES)
                attenuated = 1.0 + (raw_offset - 1.0) * confidence

                # Clamp to ±15%
                clamped = clamp_adjustment(attenuated)

                offsets[(team_id, status)] = clamped

            # Persist to in-memory cache for fast lookup
            self._team_injury_offsets = offsets

            logger.info(
                "[Calibration/Agent9] Computed %d team-injury offsets "
                "(from %d raw DB rows)",
                len(offsets), len(rows),
            )

        except Exception as e:
            logger.warning(f"[Calibration/Agent9] Failed to compute offsets: {e}")

        return offsets

    def get_team_injury_offset(self, team_id: int, status: str) -> float:
        """Get the learned injury factor offset for a team × status pair.

        Returns a multiplier in [0.85, 1.15].  The rotation engine applies
        this as::

            adjusted_factor = base_factor × offset

        If no learned offset exists, returns 1.0 (neutral — use the static
        factor as-is).
        """
        offsets = getattr(self, "_team_injury_offsets", None)
        if offsets is None:
            return 1.0

        # Normalise GTD aliases
        lookup_status = "GTD" if status == "Game Time Decision" else status

        offset = offsets.get((team_id, lookup_status))
        if offset is not None:
            return offset

        # Also try the original status in case the alias was stored differently
        if lookup_status != status:
            offset = offsets.get((team_id, status))
            if offset is not None:
                return offset

        return 1.0

    def get_injury_return_factor(self, games_back: int) -> float:
        """Get minute scaling factor for a player returning from injury.

        Players returning from injury typically ramp up over 3 games:
            Game 1 back: ~70% of baseline minutes
            Game 2 back: ~85% of baseline minutes
            Game 3 back: ~95% of baseline minutes
            Game 4+:     100% (full minutes)

        This is applied as a multiplier on top of the projected minutes.
        """
        if games_back <= 0 or games_back > 3:
            return 1.0
        ramp = {1: 0.70, 2: 0.85, 3: 0.95}
        return ramp.get(games_back, 1.0)

    def get_all_team_injury_offsets(self) -> Dict[str, float]:
        """Return all team-injury offsets as a flat dict for inspection.

        Keys are formatted as ``"injury_offset_{team_id}_{status}"``
        for consistency with other calibration getters.
        """
        offsets = getattr(self, "_team_injury_offsets", None)
        if not offsets:
            return {}
        return {
            f"injury_offset_{tid}_{status}": val
            for (tid, status), val in offsets.items()
        }

    # ── Agent 9: GPP Constraint Overrides ────────────────────────────

    def get_gpp_ownership_cap(self) -> Optional[float]:
        """Get learned GPP ownership cap override, or None."""
        val = self.get_all().get("gpp_ownership_cap")
        if val is not None and 80.0 <= val <= 200.0:
            return val
        return None

    def get_gpp_pivot_threshold(self) -> Optional[float]:
        """Get learned GPP pivot ownership threshold override, or None."""
        val = self.get_all().get("gpp_pivot_threshold")
        if val is not None and 3.0 <= val <= 25.0:
            return val
        return None

    def get_gpp_pivot_min_count(self) -> Optional[int]:
        """Get learned GPP minimum pivot player count override, or None."""
        val = self.get_all().get("gpp_pivot_min_count")
        if val is not None and 0 <= val <= 4:
            return int(val)
        return None

    def get_gpp_ceiling_weight(self) -> Optional[float]:
        """Get learned GPP ceiling weight override, or None."""
        val = self.get_all().get("gpp_ceiling_weight")
        if val is not None and 0.0 <= val <= 0.60:
            return val
        return None

    def get_gpp_bringback_salary_threshold(self) -> Optional[int]:
        """Get learned GPP bring-back salary threshold override, or None."""
        val = self.get_all().get("gpp_bringback_salary_threshold")
        if val is not None and 5000 <= val <= 12000:
            return int(val)
        return None

    def get_gpp_salary_floor_pct(self) -> Optional[float]:
        """Get learned GPP salary floor as % of cap, or None."""
        val = self.get_all().get("gpp_salary_floor_pct")
        if val is not None and 90.0 <= val <= 100.0:
            return val
        return None

    async def save_gpp_blueprint_calibrations(
        self,
        constraint_overrides: Dict[str, float],
        metadata: Dict,
        reasoning_map: Optional[Dict[str, str]] = None,
    ) -> int:
        """Persist GPP blueprint constraint overrides to TournamentCalibration.

        Unlike multiplier-based calibrations, GPP constraints are raw parameter
        values (e.g., ownership_cap=128.0), so we skip the ±15% clamping.
        """
        from app.db.database import is_db_available, get_session
        from app.db.models import TournamentCalibration
        from sqlalchemy import select

        if not is_db_available() or not constraint_overrides:
            return 0

        _reasoning_map = reasoning_map or {}
        count = 0

        async with get_session() as session:
            for key, value in constraint_overrides.items():
                reasoning = _reasoning_map.get(
                    key, metadata.get("reasoning", "")
                )

                stmt = select(TournamentCalibration).where(
                    TournamentCalibration.calibration_key == key
                )
                result = await session.execute(stmt)
                existing = result.scalar_one_or_none()

                now = datetime.now(timezone.utc)
                expires = now + timedelta(days=30)

                if existing:
                    existing.adjustment_value = value
                    existing.raw_adjustment = value
                    existing.category = "gpp_constraint"
                    existing.based_on_contests = metadata.get("contest_count", 0)
                    existing.based_on_entries = metadata.get("entry_count", 0)
                    existing.confidence = metadata.get("confidence", 0.5)
                    existing.analysis_date = now
                    existing.reasoning = reasoning
                    existing.expires_at = expires
                    existing.is_active = True
                else:
                    session.add(
                        TournamentCalibration(
                            calibration_key=key,
                            category="gpp_constraint",
                            adjustment_value=value,
                            raw_adjustment=value,
                            based_on_contests=metadata.get("contest_count", 0),
                            based_on_entries=metadata.get("entry_count", 0),
                            confidence=metadata.get("confidence", 0.5),
                            reasoning=reasoning,
                            source="gpp_postmortem",
                            expires_at=expires,
                        )
                    )
                count += 1

            await session.commit()

        logger.info(f"[Calibration] Saved {count} GPP blueprint constraints")
        return count

    async def save_backtest_calibrations(
        self,
        adjustments: Dict[str, float],
        metadata: Optional[Dict] = None,
    ) -> int:
        """Persist backtest analysis calibrations to BacktestCalibration table.

        Uses upsert logic: updates existing keys, inserts new ones.
        Returns number of rows upserted.
        """
        from app.db.database import is_db_available, get_session
        from app.db.models import BacktestCalibration
        from sqlalchemy import select

        if not is_db_available() or not adjustments:
            return 0

        meta = metadata or {}
        count = 0
        async with get_session() as session:
            for key, raw_value in adjustments.items():
                clamped = clamp_adjustment(raw_value)

                stmt = select(BacktestCalibration).where(
                    BacktestCalibration.calibration_key == key
                )
                result = await session.execute(stmt)
                existing = result.scalar_one_or_none()

                now = datetime.now(timezone.utc)
                expires = now + timedelta(days=14)

                if existing:
                    existing.adjustment_value = clamped
                    existing.based_on_games = meta.get("game_count", 0)
                    existing.analysis_date = now
                    existing.reasoning = meta.get("reasoning", "")
                    existing.expires_at = expires
                    existing.is_active = True
                else:
                    session.add(
                        BacktestCalibration(
                            calibration_key=key,
                            adjustment_value=clamped,
                            based_on_games=meta.get("game_count", 0),
                            reasoning=meta.get("reasoning", ""),
                            expires_at=expires,
                        )
                    )
                count += 1

            await session.commit()

        logger.info(f"[Calibration] Saved {count} backtest calibrations")
        return count

    async def save_tournament_calibrations(
        self,
        adjustments: Dict[str, float],
        category_map: Dict[str, str],
        metadata: Dict,
        reasoning_map: Optional[Dict[str, str]] = None,
    ) -> int:
        """Persist tournament analysis calibrations to the DB.

        Uses upsert logic: updates existing keys, inserts new ones.
        Returns number of rows upserted.

        Parameters
        ----------
        reasoning_map : dict, optional
            Per-key reasoning strings (``calibration_key -> explanation``).
            When provided, each row gets its own reasoning instead of the
            global ``metadata["reasoning"]`` fallback.
        """
        from app.db.database import is_db_available, get_session
        from app.db.models import TournamentCalibration
        from sqlalchemy import select

        if not is_db_available():
            return 0

        _reasoning_map = reasoning_map or {}

        count = 0
        async with get_session() as session:
            for key, raw_value in adjustments.items():
                clamped = clamp_adjustment(raw_value)
                category = category_map.get(key, "unknown")
                reasoning = _reasoning_map.get(
                    key, metadata.get("reasoning", "")
                )

                stmt = select(TournamentCalibration).where(
                    TournamentCalibration.calibration_key == key
                )
                result = await session.execute(stmt)
                existing = result.scalar_one_or_none()

                now = datetime.now(timezone.utc)
                expires = now + timedelta(days=30)

                if existing:
                    existing.adjustment_value = clamped
                    existing.raw_adjustment = raw_value
                    existing.category = category
                    existing.based_on_contests = metadata.get("contest_count", 0)
                    existing.based_on_entries = metadata.get("entry_count", 0)
                    existing.confidence = metadata.get("confidence", 0.5)
                    existing.analysis_date = now
                    existing.reasoning = reasoning
                    existing.expires_at = expires
                    existing.is_active = True
                else:
                    session.add(
                        TournamentCalibration(
                            calibration_key=key,
                            category=category,
                            adjustment_value=clamped,
                            raw_adjustment=raw_value,
                            based_on_contests=metadata.get("contest_count", 0),
                            based_on_entries=metadata.get("entry_count", 0),
                            confidence=metadata.get("confidence", 0.5),
                            reasoning=reasoning,
                            source="tournament",
                            expires_at=expires,
                        )
                    )
                count += 1

            await session.commit()

        logger.info(f"[Calibration] Saved {count} tournament calibrations")
        return count

    async def reset_calibrations(self, source: Optional[str] = None) -> int:
        """Clear calibrations from the database.

        If source is specified ("tournament" or "backtest"), only clears
        that source.  Otherwise clears all.
        """
        from app.db.database import is_db_available, get_session
        from app.db.models import TournamentCalibration, BacktestCalibration
        from sqlalchemy import delete

        if not is_db_available():
            return 0

        count = 0
        async with get_session() as session:
            if source is None or source == "tournament":
                result = await session.execute(delete(TournamentCalibration))
                count += result.rowcount
            if source is None or source == "backtest":
                result = await session.execute(delete(BacktestCalibration))
                count += result.rowcount
            await session.commit()

        self._cache = None
        self._cache_by_category = None
        logger.info(
            f"[Calibration] Reset {count} calibrations (source={source})"
        )
        return count

    async def rollback_calibration(
        self, key: str, source: Optional[str] = None
    ) -> bool:
        """Deactivate a specific calibration key (set is_active=False).

        Returns True if a matching row was found and deactivated.
        """
        from app.db.database import is_db_available, get_session
        from app.db.models import BacktestCalibration, TournamentCalibration
        from sqlalchemy import select

        if not is_db_available():
            return False

        found = False
        async with get_session() as session:
            if source is None or source == "backtest":
                stmt = select(BacktestCalibration).where(
                    BacktestCalibration.calibration_key == key
                )
                result = await session.execute(stmt)
                row = result.scalar_one_or_none()
                if row:
                    row.is_active = False
                    found = True

            if source is None or source == "tournament":
                stmt = select(TournamentCalibration).where(
                    TournamentCalibration.calibration_key == key
                )
                result = await session.execute(stmt)
                row = result.scalar_one_or_none()
                if row:
                    row.is_active = False
                    found = True

            await session.commit()

        if found:
            # Refresh in-memory cache
            await self.load_calibrations()
            logger.info(f"[Calibration] Rolled back key={key}")
        return found

    async def get_calibration_history(
        self, key: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Return all calibration records (active + inactive) for audit.

        If key is given, filters to that specific calibration_key.
        """
        from app.db.database import is_db_available, get_session
        from app.db.models import BacktestCalibration, TournamentCalibration
        from sqlalchemy import select

        if not is_db_available():
            return []

        history: List[Dict[str, Any]] = []
        async with get_session() as session:
            bt_stmt = select(BacktestCalibration)
            if key:
                bt_stmt = bt_stmt.where(
                    BacktestCalibration.calibration_key == key
                )
            bt_result = await session.execute(bt_stmt)
            for row in bt_result.scalars().all():
                history.append({
                    "source": "backtest",
                    "key": row.calibration_key,
                    "value": row.adjustment_value,
                    "is_active": row.is_active,
                    "analysis_date": (
                        row.analysis_date.isoformat()
                        if row.analysis_date else None
                    ),
                    "expires_at": (
                        row.expires_at.isoformat()
                        if row.expires_at else None
                    ),
                    "based_on_games": row.based_on_games,
                    "reasoning": row.reasoning,
                })

            tc_stmt = select(TournamentCalibration)
            if key:
                tc_stmt = tc_stmt.where(
                    TournamentCalibration.calibration_key == key
                )
            tc_result = await session.execute(tc_stmt)
            for row in tc_result.scalars().all():
                history.append({
                    "source": "tournament",
                    "key": row.calibration_key,
                    "category": row.category,
                    "value": row.adjustment_value,
                    "raw_adjustment": row.raw_adjustment,
                    "is_active": row.is_active,
                    "confidence": row.confidence,
                    "analysis_date": (
                        row.analysis_date.isoformat()
                        if row.analysis_date else None
                    ),
                    "expires_at": (
                        row.expires_at.isoformat()
                        if row.expires_at else None
                    ),
                    "based_on_contests": row.based_on_contests,
                    "reasoning": row.reasoning,
                })

        # Sort by analysis_date descending
        history.sort(
            key=lambda x: x.get("analysis_date", ""),
            reverse=True,
        )
        return history
