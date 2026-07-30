"""Agent 9 — Backtesting Feedback Loop.

Analyses historical projection accuracy to identify systematic
biases and generate calibration adjustments for both minutes
projections and fantasy point scoring.

**Architecture — deterministic + AI hybrid**:

All statistical computation (MAE, RMSE, bias, rolling windows, CV)
is performed in pure Python.  The LLM only interprets detected
patterns and suggests calibration multipliers.  When the AI service
is unavailable, a fully deterministic fallback produces conservative
but functional calibrations.

**Two recalibration pipelines** (new in this rewrite):

1. **DvP Rolling-Window Recalibration** (3-game window):
   For each stat category, groups recent games by opponent and
   computes a 3-game rolling ratio (actual / projected).  If a
   defence consistently allows 15%+ above projected rates, a
   ``dvp_sensitivity_{stat}`` multiplier is generated.

   Math::

       For opponent O and stat S:
           ratio_game_i = sum(actual_S) / sum(projected_S)  per game

       rolling_3 = mean(ratio[-3:])

       If rolling_3 >= 1.15 → defence is weak vs this stat
           → dvp_sensitivity_{stat} *= rolling_3 (capped 1.15)
       If rolling_3 <= 0.85 → defence is strong vs this stat
           → dvp_sensitivity_{stat} *= rolling_3 (capped 0.85)

   The 15% threshold mirrors the CalibrationService's ±15% cap.

2. **Per-Player Volatility (Sigma) Tuning**:
   Tracks game-to-game Usage Rate fluctuation per player.  The
   coefficient of variation (CV) of a player's per-minute stat
   production across recent games is compared to the simulation
   engine's default ``STAT_NOISE_SIGMA`` values.

   Math::

       For player P across last N games:
           rate_i = actual_stat_i / actual_minutes_i
           cv     = std(rates) / mean(rates)

       sigma_ratio = cv / default_sigma
       If sigma_ratio > 1.0 → player is more volatile than modeled
           → raise sigma (higher P90 ceiling, lower P10 floor)
       If sigma_ratio < 1.0 → player is more stable
           → lower sigma (tighter projection band)

   This feeds ``noise_sigma_{stat}`` keys that the simulation engine
   reads via ``CalibrationService.get_noise_overrides()``.

**Data flow**::

    BDL V1 /stats endpoint (yesterday's box scores)
         │
         ▼
    _fetch_yesterday_stats()  ─→  raw player stats dicts
         │
         ├─→  _enrich_with_per_minute_rates()
         │         → actual FGA/min, 3PA/min, FTA/min, USG proxy
         │
         ├─→  _query_projected_rates()  (async DB read)
         │         → yesterday's projected per-minute rates
         │         → from PlayerMinutesHistory
         │
         ├─→  compute_dvp_rolling_window()
         │         → 3-game window per opponent × stat
         │         → dvp_sensitivity_{stat} calibrations
         │
         ├─→  compute_player_volatility_sigma()
         │         → per-player CV across recent games
         │         → noise_sigma_{stat} calibrations
         │
         └─→  run_nightly_backtest()
                   → orchestrates full pipeline
                   → saves via CalibrationService

Uses **reasoning tier** — runs as background batch job.
"""

import json
import logging
import math
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx

from app.config import get_settings
from app.models.ai import AIRequest, BacktestAnalysis
from app.services.agents.sport_context import get_sport_preamble
from app.services.ai_service import AIService

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
STAT_FIELDS = ["pts", "reb", "ast", "stl", "blk", "tov", "fg3m"]
SALARY_TIERS = {"HIGH": 8000, "MID": 5000}  # >= thresholds

#: Minimum number of games per opponent to include in DvP rolling window.
DVP_MIN_GAMES_PER_OPP: int = 3

#: Rolling window size for DvP recalibration (last N games per opponent).
DVP_ROLLING_WINDOW: int = 3

#: Threshold for flagging a defence as significantly over/under-allowing.
#: 0.15 = 15% — matches the CalibrationService's ±15% cap.
DVP_THRESHOLD: float = 0.15

#: Minimum total opponents with sufficient data for DvP recalibration.
DVP_MIN_OPPONENTS: int = 5

#: Minimum games per player for volatility sigma tuning.
SIGMA_MIN_GAMES: int = 5

#: Minimum sample for any deterministic calibration to be produced.
DETERMINISTIC_MIN_SAMPLE: int = 20

#: BDL API base URL and timeout for direct httpx fallback.
BDL_BASE_URL = "https://api.balldontlie.io/v1"
BDL_TIMEOUT = 10.0

# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def _tier_label(salary: int) -> str:
    if salary >= SALARY_TIERS["HIGH"]:
        return "HIGH"
    elif salary >= SALARY_TIERS["MID"]:
        return "MID"
    return "VALUE"


def _mae(errors: List[float]) -> float:
    return sum(abs(e) for e in errors) / len(errors) if errors else 0.0


def _rmse(errors: List[float]) -> float:
    if not errors:
        return 0.0
    return math.sqrt(sum(e * e for e in errors) / len(errors))


def _mean(vals: List[float]) -> float:
    return sum(vals) / len(vals) if vals else 0.0


def _std(vals: List[float]) -> float:
    """Population standard deviation."""
    if len(vals) < 2:
        return 0.0
    m = _mean(vals)
    return math.sqrt(sum((v - m) ** 2 for v in vals) / len(vals))


def _clamp(v: float) -> float:
    """Clamp a multiplier to the ±15% range [0.85, 1.15]."""
    return max(0.85, min(1.15, v))


# ---------------------------------------------------------------------------
# Deterministic accuracy statistics (computed in code, NOT by the LLM)
# ---------------------------------------------------------------------------


def compute_accuracy_stats(accuracy_data: List[Dict]) -> Dict:
    """Compute deterministic accuracy metrics from historical data.

    Returns a dict with overall stats, per-stat breakdowns, position splits,
    salary tier splits, and B2B splits — all computed in pure Python.
    The LLM only needs to interpret patterns, not do arithmetic.
    """
    if not accuracy_data:
        return {}

    # Overall FP errors
    fp_errors = []
    min_errors = []

    # Per-stat errors
    stat_errors: Dict[str, List[float]] = defaultdict(list)

    # Position-level FP and minute errors
    pos_fp_errors: Dict[str, List[float]] = defaultdict(list)
    pos_min_errors: Dict[str, List[float]] = defaultdict(list)

    # Salary tier FP errors
    tier_fp_errors: Dict[str, List[float]] = defaultdict(list)

    # B2B FP errors
    b2b_fp_errors: List[float] = []
    non_b2b_fp_errors: List[float] = []

    # Per-stat, per-position errors
    pos_stat_errors: Dict[str, Dict[str, List[float]]] = defaultdict(
        lambda: defaultdict(list)
    )

    for row in accuracy_data:
        proj_fp = row.get("projected_fp", 0) or 0
        act_fp = row.get("actual_fp", 0) or 0
        fp_err = act_fp - proj_fp
        fp_errors.append(fp_err)

        proj_min = row.get("projected_minutes", 0) or 0
        act_min = row.get("actual_minutes", 0) or 0
        min_errors.append(act_min - proj_min)

        pos = row.get("position", "?")
        pos_fp_errors[pos].append(fp_err)
        pos_min_errors[pos].append(act_min - proj_min)

        salary = row.get("dk_salary", 0) or 0
        tier = _tier_label(salary)
        tier_fp_errors[tier].append(fp_err)

        is_b2b = row.get("was_b2b", False)
        if is_b2b:
            b2b_fp_errors.append(fp_err)
        else:
            non_b2b_fp_errors.append(fp_err)

        for s in STAT_FIELDS:
            p = row.get(f"projected_{s}", 0) or 0
            a = row.get(f"actual_{s}", 0) or 0
            if p > 0 or a > 0:
                err = a - p
                stat_errors[s].append(err)
                pos_stat_errors[pos][s].append(err)

    # Build the stats summary
    result = {
        "sample_size": len(accuracy_data),
        "overall_fp_mae": round(_mae(fp_errors), 2),
        "overall_fp_rmse": round(_rmse(fp_errors), 2),
        "overall_fp_bias": round(_mean(fp_errors), 2),
        "overall_min_mae": round(_mae(min_errors), 2),
        "overall_min_bias": round(_mean(min_errors), 2),
    }

    # Per-stat accuracy
    stat_summary = {}
    for s in STAT_FIELDS:
        errs = stat_errors.get(s, [])
        if errs:
            stat_summary[s] = {
                "mae": round(_mae(errs), 2),
                "bias": round(_mean(errs), 2),
                "n": len(errs),
            }
    result["per_stat"] = stat_summary

    # Position breakdown
    pos_summary = {}
    for pos in sorted(pos_fp_errors.keys()):
        fp_errs = pos_fp_errors[pos]
        min_errs = pos_min_errors[pos]
        pos_summary[pos] = {
            "fp_mae": round(_mae(fp_errs), 2),
            "fp_bias": round(_mean(fp_errs), 2),
            "min_mae": round(_mae(min_errs), 2),
            "min_bias": round(_mean(min_errs), 2),
            "n": len(fp_errs),
        }
        # Per-stat for this position
        pstat = {}
        for s in STAT_FIELDS:
            errs = pos_stat_errors.get(pos, {}).get(s, [])
            if len(errs) >= 5:
                pstat[s] = {
                    "bias": round(_mean(errs), 2),
                    "n": len(errs),
                }
        if pstat:
            pos_summary[pos]["stat_biases"] = pstat
    result["by_position"] = pos_summary

    # Salary tier breakdown
    tier_summary = {}
    for tier in ["HIGH", "MID", "VALUE"]:
        errs = tier_fp_errors.get(tier, [])
        if errs:
            tier_summary[tier] = {
                "fp_mae": round(_mae(errs), 2),
                "fp_bias": round(_mean(errs), 2),
                "n": len(errs),
            }
    result["by_salary_tier"] = tier_summary

    # B2B breakdown
    result["b2b"] = {
        "b2b_fp_mae": round(_mae(b2b_fp_errors), 2),
        "b2b_fp_bias": round(_mean(b2b_fp_errors), 2),
        "b2b_n": len(b2b_fp_errors),
        "non_b2b_fp_mae": round(_mae(non_b2b_fp_errors), 2),
        "non_b2b_fp_bias": round(_mean(non_b2b_fp_errors), 2),
        "non_b2b_n": len(non_b2b_fp_errors),
    }

    return result


def compute_deterministic_calibrations(
    accuracy_data: List[Dict],
    min_sample: int = 20,
    bias_threshold: float = 1.0,
) -> Dict[str, float]:
    """Produce calibration adjustments from accuracy stats without AI.

    Converts detected biases into multipliers:
    - Position bias > threshold  -> position_<pos>_bias adjustment
    - Salary tier bias           -> salary_tier_<tier>_projection adjustment
    - Per-stat bias (>=5% relative) -> stat_rate_<stat> adjustment
    - B2B bias                   -> game_context_b2b adjustment

    All multipliers are clamped to +-15% (0.85-1.15).
    Only produced when sample_size >= min_sample per category.
    """
    if not accuracy_data:
        return {}

    stats = compute_accuracy_stats(accuracy_data)
    if not stats:
        return {}

    adjustments: Dict[str, float] = {}

    # Collect mean projected values per group for multiplier denominators
    pos_proj_fp: Dict[str, List[float]] = defaultdict(list)
    tier_proj_fp: Dict[str, List[float]] = defaultdict(list)
    stat_proj: Dict[str, List[float]] = defaultdict(list)
    b2b_proj_fp: List[float] = []

    for row in accuracy_data:
        proj_fp = row.get("projected_fp", 0) or 0
        pos = row.get("position", "?")
        pos_proj_fp[pos].append(proj_fp)

        salary = row.get("dk_salary", 0) or 0
        tier = _tier_label(salary)
        tier_proj_fp[tier].append(proj_fp)

        if row.get("was_b2b", False):
            b2b_proj_fp.append(proj_fp)

        for s in STAT_FIELDS:
            p = row.get(f"projected_{s}", 0) or 0
            if p > 0:
                stat_proj[s].append(p)

    # Position biases -> position_{POS}_bias
    by_pos = stats.get("by_position", {})
    for pos, pdata in by_pos.items():
        n = pdata.get("n", 0)
        bias = pdata.get("fp_bias", 0)
        if n >= min_sample and abs(bias) >= bias_threshold:
            mean_proj = _mean(pos_proj_fp.get(pos, []))
            if mean_proj > 0:
                adjustments[f"position_{pos}_bias"] = round(
                    _clamp(1.0 + bias / mean_proj), 3
                )

    # Salary tier biases -> salary_tier_{tier}_projection
    by_tier = stats.get("by_salary_tier", {})
    for tier, tdata in by_tier.items():
        n = tdata.get("n", 0)
        bias = tdata.get("fp_bias", 0)
        if n >= min_sample and abs(bias) >= bias_threshold:
            mean_proj = _mean(tier_proj_fp.get(tier, []))
            if mean_proj > 0:
                adjustments[f"salary_tier_{tier.lower()}_projection"] = round(
                    _clamp(1.0 + bias / mean_proj), 3
                )

    # Per-stat biases -> stat_rate_{stat}
    # Use relative threshold (>=5% of mean projected) since stat magnitudes vary
    per_stat = stats.get("per_stat", {})
    for s, sdata in per_stat.items():
        n = sdata.get("n", 0)
        bias = sdata.get("bias", 0)
        mean_proj = _mean(stat_proj.get(s, []))
        if n >= min_sample and mean_proj > 0:
            relative_bias = abs(bias / mean_proj)
            if relative_bias >= 0.05:
                adjustments[f"stat_rate_{s}"] = round(
                    _clamp(1.0 + bias / mean_proj), 3
                )

    # B2B bias -> game_context_b2b
    b2b_data = stats.get("b2b", {})
    b2b_n = b2b_data.get("b2b_n", 0)
    b2b_bias = b2b_data.get("b2b_fp_bias", 0)
    if b2b_n >= min_sample and abs(b2b_bias) >= bias_threshold:
        mean_proj = _mean(b2b_proj_fp)
        if mean_proj > 0:
            adjustments["game_context_b2b"] = round(
                _clamp(1.0 + b2b_bias / mean_proj), 3
            )

    return adjustments


# ---------------------------------------------------------------------------
# BDL Advanced Metric Calibrations (shot decomposition, DvP, noise sigma)
# ---------------------------------------------------------------------------

_SHOT_RATE_TYPES = ["fg3a_rate", "fga_rate", "fta_rate"]
_PCT_TYPES = ["fg3_pct", "fg2_pct", "ft_pct"]


def compute_shot_decomposition_stats(
    accuracy_data: List[Dict],
    min_sample: int = 15,
) -> Dict:
    """Compute per-minute shooting rate accuracy from BDL-enriched data.

    Compares actual per-minute attempt rates (from box scores) against
    the system's projected rates to identify systematic biases in the
    shot-type decomposition pipeline.

    Returns dict with:
        shot_rate_bias: {fg3a_rate: {bias, mae, n, mean_projected}, ...}
        shot_rate_by_position: {POS: {fg3a_rate: {bias, n}, ...}, ...}
        pct_accuracy: {fg3_pct: {bias, mae, n}, ...}
    """
    if not accuracy_data:
        return {}

    # Filter to rows with BDL shooting data
    enriched = [
        r for r in accuracy_data
        if r.get("actual_fg3a_rate") is not None
        and r.get("projected_fg3a_rate") is not None
    ]
    if len(enriched) < min_sample:
        return {}

    result: Dict = {"shot_rate_bias": {}, "shot_rate_by_position": {}, "pct_accuracy": {}}

    # Global shot-rate bias
    for rate_type in _SHOT_RATE_TYPES:
        actual_key = f"actual_{rate_type}"
        proj_key = f"projected_{rate_type}"
        vals = [
            (r[actual_key], r[proj_key])
            for r in enriched
            if r.get(actual_key) is not None and r.get(proj_key) is not None
            and r[proj_key] > 0
        ]
        if len(vals) >= min_sample:
            diffs = [a - p for a, p in vals]
            abs_diffs = [abs(d) for d in diffs]
            mean_proj = sum(p for _, p in vals) / len(vals)
            result["shot_rate_bias"][rate_type] = {
                "bias": round(sum(diffs) / len(diffs), 5),
                "mae": round(sum(abs_diffs) / len(abs_diffs), 5),
                "n": len(vals),
                "mean_projected": round(mean_proj, 5),
            }

    # Position-grouped shot-rate bias
    pos_groups: Dict[str, List[Dict]] = defaultdict(list)
    for r in enriched:
        pos = r.get("position", "?")
        pos_groups[pos].append(r)

    for pos, rows in pos_groups.items():
        if len(rows) < min_sample:
            continue
        pos_stats = {}
        for rate_type in _SHOT_RATE_TYPES:
            actual_key = f"actual_{rate_type}"
            proj_key = f"projected_{rate_type}"
            vals = [
                (r[actual_key], r[proj_key])
                for r in rows
                if r.get(actual_key) is not None and r.get(proj_key) is not None
                and r[proj_key] > 0
            ]
            if len(vals) >= 5:
                diffs = [a - p for a, p in vals]
                pos_stats[rate_type] = {
                    "bias": round(sum(diffs) / len(diffs), 5),
                    "n": len(vals),
                }
        if pos_stats:
            result["shot_rate_by_position"][pos] = pos_stats

    # Percentage accuracy (actual vs league avg or player avg)
    for pct_type in _PCT_TYPES:
        actual_key = f"actual_{pct_type}"
        vals = [
            r[actual_key]
            for r in enriched
            if r.get(actual_key) is not None
        ]
        if len(vals) >= min_sample:
            mean_val = sum(vals) / len(vals)
            result["pct_accuracy"][pct_type] = {
                "mean": round(mean_val, 4),
                "n": len(vals),
            }

    return result


def _shot_rate_to_calibration_keys(
    shot_stats: Dict,
    min_sample: int = 15,
) -> Dict[str, float]:
    """Convert shot decomposition stats into calibration multiplier keys.

    For each rate type with sufficient samples and >=5% relative bias,
    produces a clamped multiplier [0.85, 1.15].
    """
    if not shot_stats:
        return {}

    adjustments: Dict[str, float] = {}
    bias_data = shot_stats.get("shot_rate_bias", {})

    for rate_type, data in bias_data.items():
        n = data.get("n", 0)
        bias = data.get("bias", 0)
        mean_proj = data.get("mean_projected", 0)
        if n < min_sample or mean_proj <= 0:
            continue
        relative_bias = abs(bias / mean_proj)
        if relative_bias >= 0.05:
            # Strip _rate suffix: fg3a_rate -> fg3a
            key_suffix = rate_type.replace("_rate", "")
            adjustments[f"shot_rate_{key_suffix}"] = round(
                _clamp(1.0 + bias / mean_proj), 3
            )

    return adjustments


# ============================================================================
# DvP Recalibration: 3-Game Rolling Window
# ============================================================================


def compute_dvp_recalibration(
    accuracy_data: List[Dict],
    min_sample_per_opp: int = DVP_MIN_GAMES_PER_OPP,
    min_opponents: int = DVP_MIN_OPPONENTS,
) -> Dict[str, float]:
    """Compute per-stat DvP sensitivity multipliers from actual vs projected.

    Groups actual per-minute rates by opponent_team_id.  For each stat,
    measures the variance in (actual/projected) ratio across opponents.
    If the variance is smaller than our DvP predictions imply,
    sensitivity < 1.0 (we over-apply DvP).  If larger, sensitivity > 1.0.

    Returns dict with keys like 'dvp_sensitivity_pts', plus global
    'dvp_sensitivity' for backward compatibility.
    """
    if not accuracy_data:
        return {}

    # Filter to rows with opponent data and sufficient minutes
    eligible = [
        r for r in accuracy_data
        if r.get("opponent_team_id") is not None
        and (r.get("actual_minutes") or 0) >= 10
        and (r.get("projected_minutes") or 0) > 0
    ]
    if not eligible:
        return {}

    # Group by opponent
    by_opp: Dict[int, List[Dict]] = defaultdict(list)
    for r in eligible:
        by_opp[r["opponent_team_id"]].append(r)

    # Filter to opponents with enough data
    valid_opps = {
        opp_id: rows
        for opp_id, rows in by_opp.items()
        if len(rows) >= min_sample_per_opp
    }
    if len(valid_opps) < min_opponents:
        return {}

    # For each stat, compute per-opponent actual/projected ratio
    stat_ratios: Dict[str, List[float]] = defaultdict(list)

    for opp_id, rows in valid_opps.items():
        for stat in STAT_FIELDS:
            actual_key = f"actual_{stat}"
            proj_key = f"projected_{stat}"
            pairs = [
                (r.get(actual_key, 0) or 0, r.get(proj_key, 0) or 0)
                for r in rows
                if (r.get(proj_key) or 0) > 0
            ]
            if len(pairs) < 3:
                continue
            sum_actual = sum(a for a, _ in pairs)
            sum_proj = sum(p for _, p in pairs)
            if sum_proj > 0:
                opp_ratio = sum_actual / sum_proj
                stat_ratios[stat].append(opp_ratio)

    # Compute sensitivity: how much actual production spread across
    # opponents matches our predictions.  The standard deviation of
    # per-opponent ratios indicates how much opponents matter.
    adjustments: Dict[str, float] = {}
    all_sensitivities: List[float] = []

    for stat in STAT_FIELDS:
        ratios = stat_ratios.get(stat, [])
        if len(ratios) < min_opponents:
            continue
        mean_ratio = sum(ratios) / len(ratios)
        if mean_ratio <= 0:
            continue
        variance = sum((r - mean_ratio) ** 2 for r in ratios) / len(ratios)
        std_dev = variance ** 0.5

        # DvP sensitivity is proportional to the observed spread.
        # Baseline expected spread for a well-calibrated DvP is ~0.05-0.15.
        # Scale relative to a reference spread of 0.10.
        _REFERENCE_SPREAD = 0.10
        if _REFERENCE_SPREAD > 0:
            raw_sensitivity = std_dev / _REFERENCE_SPREAD
        else:
            raw_sensitivity = 1.0

        sensitivity = _clamp(raw_sensitivity)
        adjustments[f"dvp_sensitivity_{stat}"] = round(sensitivity, 3)
        all_sensitivities.append(sensitivity)

    # Global average for backward compatibility
    if all_sensitivities:
        global_sens = sum(all_sensitivities) / len(all_sensitivities)
        adjustments["dvp_sensitivity"] = round(_clamp(global_sens), 3)

    return adjustments


def compute_dvp_rolling_window(
    accuracy_data: List[Dict],
    window: int = DVP_ROLLING_WINDOW,
    threshold: float = DVP_THRESHOLD,
    min_opponents: int = DVP_MIN_OPPONENTS,
) -> Dict[str, float]:
    """Compute per-stat DvP multipliers using a 3-game rolling window.

    For each opponent team, this function takes the most recent N games
    and computes the ratio of actual-to-projected production per stat
    category.  If a defence consistently allows >=15% more (or less)
    than projected, a DvP sensitivity adjustment is generated.

    This is a RECENCY-WEIGHTED alternative to ``compute_dvp_recalibration``
    which uses all available data equally.  The rolling window detects
    *recent defensive trends* rather than season-long averages.

    Math per opponent O and stat S::

        # Sort games against O by date, take last `window` games
        games = sorted(games_vs_O, key=game_date)[-window:]

        # Aggregate ratio across the window
        sum_actual = sum(actual_S for g in games)
        sum_proj   = sum(projected_S for g in games)
        window_ratio = sum_actual / sum_proj

    Then across all opponents with sufficient data::

        # Collect window ratios per stat
        ratios = [window_ratio_O1, window_ratio_O2, ...]
        mean_ratio = mean(ratios)

    Decision logic::

        If mean_ratio >= 1 + threshold (1.15):
            → Defence is weak vs this stat recently
            → dvp_sensitivity_{stat} = clamp(mean_ratio)
        If mean_ratio <= 1 - threshold (0.85):
            → Defence is strong vs this stat recently
            → dvp_sensitivity_{stat} = clamp(mean_ratio)
        Otherwise:
            → No adjustment needed

    Parameters
    ----------
    accuracy_data : list of dict
        Historical accuracy data with ``opponent_team_id``, ``game_date``,
        ``actual_{stat}``, ``projected_{stat}`` fields.
    window : int
        Number of most-recent games per opponent to consider (default 3).
    threshold : float
        Minimum deviation from 1.0 to trigger an adjustment (default 0.15).
    min_opponents : int
        Minimum number of opponents with sufficient data to produce
        any adjustments (default 5).

    Returns
    -------
    dict
        ``{dvp_sensitivity_{stat}: float, ...}`` plus a global
        ``dvp_sensitivity`` average.  Multipliers are clamped to [0.85, 1.15].
    """
    if not accuracy_data:
        return {}

    # Filter to eligible rows (have opponent, reasonable minutes)
    eligible = [
        r for r in accuracy_data
        if r.get("opponent_team_id") is not None
        and (r.get("actual_minutes") or 0) >= 10
        and (r.get("projected_minutes") or 0) > 0
        and r.get("game_date")
    ]
    if not eligible:
        return {}

    # Group by opponent, sorted by game_date (most recent last)
    by_opp: Dict[int, List[Dict]] = defaultdict(list)
    for r in eligible:
        by_opp[r["opponent_team_id"]].append(r)

    # Sort each opponent's games by date, take the most recent `window`
    for opp_id in by_opp:
        by_opp[opp_id] = sorted(
            by_opp[opp_id],
            key=lambda r: r.get("game_date", ""),
        )[-window:]

    # Filter to opponents with at least `window` games in the window
    valid_opps = {
        opp_id: rows
        for opp_id, rows in by_opp.items()
        if len(rows) >= window
    }
    if len(valid_opps) < min_opponents:
        return {}

    # Compute per-opponent, per-stat window ratios
    # ─────────────────────────────────────────────────────────────────
    # For each opponent × stat, sum actual and projected across the
    # window games, then compute ratio = sum_actual / sum_projected.
    stat_window_ratios: Dict[str, List[float]] = defaultdict(list)

    for opp_id, rows in valid_opps.items():
        for stat in STAT_FIELDS:
            actual_key = f"actual_{stat}"
            proj_key = f"projected_{stat}"

            sum_actual = 0.0
            sum_proj = 0.0
            valid_games = 0

            for r in rows:
                a = r.get(actual_key, 0) or 0
                p = r.get(proj_key, 0) or 0
                if p > 0:
                    sum_actual += a
                    sum_proj += p
                    valid_games += 1

            if valid_games >= 2 and sum_proj > 0:
                window_ratio = sum_actual / sum_proj
                stat_window_ratios[stat].append(window_ratio)

    # Compute adjustments
    # ─────────────────────────────────────────────────────────────────
    adjustments: Dict[str, float] = {}
    all_sensitivities: List[float] = []

    for stat in STAT_FIELDS:
        ratios = stat_window_ratios.get(stat, [])
        if len(ratios) < min_opponents:
            continue

        mean_ratio = _mean(ratios)

        # Only produce adjustment if deviation exceeds threshold
        # mean_ratio > 1.15  → defence weak (we under-project vs this stat)
        # mean_ratio < 0.85  → defence strong (we over-project vs this stat)
        # 0.85 <= mean_ratio <= 1.15  → within tolerance, no adjustment
        deviation = abs(mean_ratio - 1.0)
        if deviation >= threshold:
            clamped = _clamp(mean_ratio)
            adjustments[f"dvp_sensitivity_{stat}"] = round(clamped, 3)
            all_sensitivities.append(clamped)

            logger.info(
                "[Backtest DvP Rolling] %s: mean_ratio=%.3f across %d opponents "
                "(window=%d) -> dvp_sensitivity_%s=%.3f",
                stat, mean_ratio, len(ratios), window, stat, clamped,
            )
        else:
            logger.debug(
                "[Backtest DvP Rolling] %s: mean_ratio=%.3f (deviation=%.3f "
                "< threshold=%.3f) — no adjustment",
                stat, mean_ratio, deviation, threshold,
            )

    # Global average for backward compatibility
    if all_sensitivities:
        global_sens = _mean(all_sensitivities)
        adjustments["dvp_sensitivity"] = round(_clamp(global_sens), 3)

    return adjustments


# ============================================================================
# Per-Player Volatility (Sigma) Tuning
# ============================================================================


def compute_noise_sigma_adjustments(
    accuracy_data: List[Dict],
    min_sample: int = 30,
) -> Dict[str, float]:
    """Compute per-stat noise sigma adjustments for P90 accuracy.

    Compares the coefficient of variation (CV) of actual per-minute
    production against the current STAT_NOISE_SIGMA defaults.
    If actual outcomes are more/less dispersed than modeled,
    the sigma multiplier adjusts accordingly.

    Returns dict with keys like 'noise_sigma_pts' (multipliers on
    default sigmas, clamped to [0.85, 1.15]).
    """
    if not accuracy_data:
        return {}

    from app.services.simulation_engine import STAT_NOISE_SIGMA

    # Filter to substantial playing time
    eligible = [
        r for r in accuracy_data
        if (r.get("actual_minutes") or 0) >= 10
        and (r.get("projected_minutes") or 0) > 0
    ]
    if len(eligible) < min_sample:
        return {}

    adjustments: Dict[str, float] = {}

    for stat in STAT_FIELDS:
        actual_key = f"actual_{stat}"
        # Compute per-minute rates
        rates = []
        for r in eligible:
            actual_val = r.get(actual_key, 0) or 0
            actual_min = r.get("actual_minutes", 0) or 0
            if actual_min > 0:
                rates.append(actual_val / actual_min)

        if len(rates) < min_sample:
            continue

        mean_rate = sum(rates) / len(rates)
        if mean_rate <= 0.001:
            continue

        variance = sum((r - mean_rate) ** 2 for r in rates) / len(rates)
        actual_cv = (variance ** 0.5) / mean_rate

        current_sigma = STAT_NOISE_SIGMA.get(stat, 0.25)
        if current_sigma <= 0:
            continue

        sigma_multiplier = actual_cv / current_sigma
        adjustments[f"noise_sigma_{stat}"] = round(_clamp(sigma_multiplier), 3)

    return adjustments


def compute_player_volatility_sigma(
    accuracy_data: List[Dict],
    min_games: int = SIGMA_MIN_GAMES,
) -> Dict[str, float]:
    """Compute per-stat sigma adjustments from per-player volatility analysis.

    Unlike ``compute_noise_sigma_adjustments`` which pools all players
    into a single population CV, this function tracks game-to-game
    fluctuation **per player** and then aggregates.  This captures the
    signal that specific players (e.g. boom/bust scorers with high USG%
    variance) drive disproportionate projection uncertainty.

    Math per player P and stat S::

        # Collect per-minute rate for each game
        rates = [actual_S_game1 / minutes_game1,
                 actual_S_game2 / minutes_game2, ...]

        if len(rates) >= min_games:
            player_cv = std(rates) / mean(rates)

    Then across all players::

        # Aggregate player-level CVs
        median_cv = median(all_player_cvs)
        sigma_ratio = median_cv / default_sigma

    Decision::

        sigma_ratio > 1.0 → actual outcomes more volatile than modeled
            → noise_sigma_{stat} increased (wider P10-P90 band)
        sigma_ratio < 1.0 → actual outcomes more stable than modeled
            → noise_sigma_{stat} decreased (tighter band)

    This is critical for DFS GPP strategy: a player whose USG%
    heavily fluctuates game-to-game should have a HIGH sigma so the
    solver knows their P90 ceiling is exceptionally high, but their
    P10 floor is dangerous.

    Parameters
    ----------
    accuracy_data : list of dict
        Historical accuracy data with ``player_id``, ``actual_{stat}``,
        ``actual_minutes`` fields.
    min_games : int
        Minimum games per player to compute their volatility (default 5).

    Returns
    -------
    dict
        ``{noise_sigma_{stat}: float, ...}`` — multipliers on default
        ``STAT_NOISE_SIGMA`` values, clamped to [0.85, 1.15].
    """
    if not accuracy_data:
        return {}

    from app.services.simulation_engine import STAT_NOISE_SIGMA

    # Filter to substantial playing time
    eligible = [
        r for r in accuracy_data
        if (r.get("actual_minutes") or 0) >= 10
        and r.get("player_id") is not None
    ]
    if not eligible:
        return {}

    # Group by player
    by_player: Dict[Any, List[Dict]] = defaultdict(list)
    for r in eligible:
        by_player[r["player_id"]].append(r)

    adjustments: Dict[str, float] = {}

    for stat in STAT_FIELDS:
        actual_key = f"actual_{stat}"
        default_sigma = STAT_NOISE_SIGMA.get(stat, 0.25)
        if default_sigma <= 0:
            continue

        player_cvs: List[float] = []

        for pid, games in by_player.items():
            if len(games) < min_games:
                continue

            # Compute per-minute rate for each game
            rates: List[float] = []
            for g in games:
                actual_val = g.get(actual_key, 0) or 0
                actual_min = g.get("actual_minutes", 0) or 0
                if actual_min > 0:
                    rates.append(actual_val / actual_min)

            if len(rates) < min_games:
                continue

            mean_rate = _mean(rates)
            if mean_rate <= 0.001:
                continue

            cv = _std(rates) / mean_rate
            player_cvs.append(cv)

        if len(player_cvs) < 5:
            continue

        # Use median (robust to outliers) rather than mean
        sorted_cvs = sorted(player_cvs)
        n = len(sorted_cvs)
        if n % 2 == 1:
            median_cv = sorted_cvs[n // 2]
        else:
            median_cv = (sorted_cvs[n // 2 - 1] + sorted_cvs[n // 2]) / 2.0

        sigma_ratio = median_cv / default_sigma
        clamped = _clamp(sigma_ratio)
        adjustments[f"noise_sigma_{stat}"] = round(clamped, 3)

        logger.info(
            "[Backtest Sigma] %s: median_player_cv=%.4f, default_sigma=%.3f, "
            "ratio=%.3f -> noise_sigma_%s=%.3f (n_players=%d)",
            stat, median_cv, default_sigma, sigma_ratio, stat, clamped,
            len(player_cvs),
        )

    return adjustments


# ---------------------------------------------------------------------------
# Deterministic GPP blueprint statistics (computed in code, NOT by the LLM)
# ---------------------------------------------------------------------------

_DK_SALARY_CAP = 50_000


def compute_gpp_blueprint(
    top_entries: List[Dict],
    player_team_map: Optional[Dict[str, str]] = None,
    ownership_map: Optional[Dict[str, float]] = None,
) -> Dict:
    """Compute structural statistics from top GPP finishers.

    Each entry is ``{rank, points, lineup_data, total_salary}`` where
    ``lineup_data`` is ``[{roster_slot, player_name, salary, fpts}, ...]``.

    Parameters
    ----------
    top_entries : list of dict
        Top N finishers from one or more GPP contests.
    player_team_map : dict, optional
        Normalised ``player_name`` -> team abbreviation for stacking detection.
    ownership_map : dict, optional
        Normalised ``player_name`` -> projected ownership % for ownership analysis.

    Returns dict with keys matching ``GPPBlueprintStats`` fields.
    """
    if not top_entries:
        return {}

    n = len(top_entries)
    total_salaries: List[float] = []
    total_ownerships: List[float] = []
    all_points: List[float] = []
    slot_salaries: Dict[str, List[float]] = defaultdict(list)

    has_2man = 0
    has_3man = 0
    has_bringback = 0
    max_stack_sizes: List[int] = []
    low_own_counts: List[int] = []
    chalk_counts: List[int] = []

    for entry in top_entries:
        lineup = entry.get("lineup_data") or []
        pts = float(entry.get("points", 0) or 0)
        sal = float(entry.get("total_salary", 0) or 0)

        all_points.append(pts)
        total_salaries.append(sal)

        team_counts: Dict[str, int] = defaultdict(int)
        lineup_ownership = 0.0
        low_own = 0
        chalk = 0

        for p in lineup:
            slot = p.get("roster_slot", "UTIL")
            salary = float(p.get("salary", 0) or 0)
            name = (p.get("player_name") or "").strip().lower()

            slot_salaries[slot].append(salary)

            # Team tracking for stacking
            if player_team_map and name in player_team_map:
                team = player_team_map[name]
                team_counts[team] += 1

            # Ownership tracking
            if ownership_map and name in ownership_map:
                own = ownership_map[name]
                lineup_ownership += own
                if own < 10.0:
                    low_own += 1
                if own > 25.0:
                    chalk += 1

        total_ownerships.append(lineup_ownership)
        low_own_counts.append(low_own)
        chalk_counts.append(chalk)

        # Stacking detection
        if team_counts:
            max_stack = max(team_counts.values())
            max_stack_sizes.append(max_stack)
            if max_stack >= 2:
                has_2man += 1
            if max_stack >= 3:
                has_3man += 1

        # Bring-back detection
        unique_teams = set(team_counts.keys())
        if len(unique_teams) >= 2 and sum(team_counts.values()) >= 3:
            has_bringback += 1

    def _avg(vals: list) -> float:
        return round(sum(vals) / len(vals), 2) if vals else 0.0

    def _med(vals: list) -> float:
        if not vals:
            return 0.0
        s = sorted(vals)
        m = len(s)
        if m % 2 == 1:
            return round(s[m // 2], 1)
        return round((s[m // 2 - 1] + s[m // 2]) / 2.0, 1)

    return {
        "sample_size": n,
        "avg_total_salary": round(_avg(total_salaries), 0),
        "salary_floor_pct": round(
            (min(total_salaries) / _DK_SALARY_CAP * 100) if total_salaries else 0, 1
        ),
        "avg_total_ownership": _avg(total_ownerships),
        "median_total_ownership": _med(total_ownerships),
        "avg_points": _avg(all_points),
        "median_points": _med(all_points),
        "pct_with_2man_stack": round(has_2man / n, 3) if n else 0,
        "pct_with_3man_stack": round(has_3man / n, 3) if n else 0,
        "pct_with_bringback": round(has_bringback / n, 3) if n else 0,
        "avg_max_stack_size": _avg(max_stack_sizes),
        "avg_low_own_players": _avg(low_own_counts),
        "avg_chalk_players": _avg(chalk_counts),
        "salary_by_slot": {
            slot: round(_avg(sals), 0)
            for slot, sals in sorted(slot_salaries.items())
        },
    }


def compute_deterministic_gpp_constraints(
    blueprint_stats: Dict,
    current_constants: Optional[Dict[str, float]] = None,
) -> Dict[str, float]:
    """Map observed GPP stats to recommended ILP constraint overrides.

    Pure Python, no AI needed.  Produces conservative recommendations
    based on what top finishers actually looked like.

    Returns ``{constraint_key: recommended_value}``.
    """
    if not blueprint_stats or blueprint_stats.get("sample_size", 0) < 5:
        return {}

    defaults = current_constants or {
        "gpp_ownership_cap": 135.0,
        "gpp_pivot_threshold": 10.0,
        "gpp_pivot_min_count": 1,
        "gpp_ceiling_weight": 0.30,
        "gpp_bringback_salary_threshold": 8500,
        "gpp_salary_floor_pct": 96.0,
    }

    overrides: Dict[str, float] = {}

    # Ownership cap: blend 70% observed + 30% current default
    avg_own = blueprint_stats.get("avg_total_ownership", 0)
    if avg_own > 0:
        blended = avg_own * 0.7 + defaults["gpp_ownership_cap"] * 0.3
        overrides["gpp_ownership_cap"] = round(blended, 1)

    # Salary floor: observed min minus 2% buffer, floored at 94%
    floor_pct = blueprint_stats.get("salary_floor_pct", 0)
    if floor_pct > 0:
        overrides["gpp_salary_floor_pct"] = round(
            max(floor_pct - 2.0, 94.0), 1
        )

    # Bring-back: if <30% of winners used bring-backs, relax threshold
    bb_rate = blueprint_stats.get("pct_with_bringback", 0)
    if bb_rate < 0.30 and blueprint_stats.get("sample_size", 0) >= 10:
        overrides["gpp_bringback_salary_threshold"] = 9500.0

    # Pivot rule: if winners average 1.5+ low-own players, require 2
    avg_low = blueprint_stats.get("avg_low_own_players", 0)
    if avg_low >= 1.5:
        overrides["gpp_pivot_min_count"] = 2.0
    elif avg_low < 0.5:
        overrides["gpp_pivot_min_count"] = 0.0

    return overrides


# ============================================================================
# BDL Live Data Fetching (httpx async)
# ============================================================================


async def fetch_yesterday_box_scores(
    date_str: str,
    api_key: str,
    game_ids: Optional[List[int]] = None,
) -> List[Dict[str, Any]]:
    """Fetch per-player box score stats from BDL V1 /stats endpoint.

    Parameters
    ----------
    date_str : str
        Date to fetch in ``YYYY-MM-DD`` format.
    api_key : str
        BallDontLie API key for authentication.
    game_ids : list of int, optional
        Specific game IDs to fetch.  If ``None``, fetches all games
        for the given date (uses ``dates[]`` parameter).

    Returns
    -------
    list of dict
        Raw BDL stat entries.  Each entry has::

            {
                "id": int,
                "player": {"id": int, "first_name": str, "last_name": str, ...},
                "team": {"id": int, "abbreviation": str, ...},
                "game": {"id": int, "date": str, "home_team_id": int,
                         "visitor_team_id": int, ...},
                "min": str (e.g. "34:12"),
                "pts": int, "reb": int, "ast": int, "stl": int,
                "blk": int, "turnover": int,
                "fga": int, "fgm": int, "fg_pct": float,
                "fg3a": int, "fg3m": int, "fg3_pct": float,
                "fta": int, "ftm": int, "ft_pct": float,
                "oreb": int, "dreb": int, "pf": int,
            }
    """
    headers = {"Authorization": api_key}
    all_stats: List[Dict[str, Any]] = []

    try:
        async with httpx.AsyncClient(timeout=BDL_TIMEOUT) as client:
            cursor: Optional[int] = None
            for _ in range(10):  # max 10 pages
                params: Dict[str, Any] = {"per_page": 100}

                if game_ids:
                    for gid in game_ids:
                        params[f"game_ids[]"] = gid
                else:
                    params["dates[]"] = date_str

                if cursor is not None:
                    params["cursor"] = cursor

                resp = await client.get(
                    f"{BDL_BASE_URL}/stats",
                    params=params,
                    headers=headers,
                )
                resp.raise_for_status()
                body = resp.json()

                data = body.get("data", [])
                all_stats.extend(data)

                next_cursor = body.get("meta", {}).get("next_cursor")
                if next_cursor is None:
                    break
                cursor = next_cursor

        logger.info(
            "[Backtest] Fetched %d player stat lines for %s",
            len(all_stats), date_str,
        )

    except httpx.TimeoutException:
        logger.error("[Backtest] BDL stats timed out for %s", date_str)
    except httpx.HTTPStatusError as exc:
        logger.error(
            "[Backtest] BDL stats HTTP %d for %s",
            exc.response.status_code, date_str,
        )
    except Exception as exc:
        logger.error("[Backtest] BDL stats unexpected error: %s", exc)

    return all_stats


def parse_minutes_string(min_str: Optional[str]) -> float:
    """Parse BDL minutes string (e.g. '34:12') to decimal minutes.

    BDL returns minutes as ``"MM:SS"`` or ``"MM"`` strings.
    Returns 0.0 for None/empty/invalid values.
    """
    if not min_str or not isinstance(min_str, str):
        return 0.0
    min_str = min_str.strip()
    if not min_str:
        return 0.0
    try:
        if ":" in min_str:
            parts = min_str.split(":")
            return float(parts[0]) + float(parts[1]) / 60.0
        return float(min_str)
    except (ValueError, IndexError):
        return 0.0


def enrich_box_scores_with_rates(
    raw_stats: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Convert raw BDL stats into enriched dicts with per-minute rates.

    For each player-game entry, computes:
    - ``minutes`` (decimal float)
    - ``fga_per_min``, ``fg3a_per_min``, ``fta_per_min`` (shot attempt rates)
    - ``usg_proxy`` (approximate usage = (FGA + 0.44*FTA + TOV) / minutes)
    - ``ts_pct`` (true shooting = PTS / (2 * (FGA + 0.44*FTA)))
    - Team and opponent identification

    Returns
    -------
    list of dict
        Enriched stat dicts, one per player-game.
    """
    enriched: List[Dict[str, Any]] = []

    for entry in raw_stats:
        player = entry.get("player") or {}
        team = entry.get("team") or {}
        game = entry.get("game") or {}

        minutes = parse_minutes_string(entry.get("min"))
        if minutes < 1.0:
            continue  # Skip DNPs and negligible minutes

        pid = player.get("id")
        first_name = player.get("first_name", "")
        last_name = player.get("last_name", "")
        player_name = f"{first_name} {last_name}".strip()
        team_id = team.get("id")
        team_abbr = (team.get("abbreviation") or "").upper()

        # Determine opponent team ID
        home_team_id = game.get("home_team_id")
        visitor_team_id = game.get("visitor_team_id")
        if team_id == home_team_id:
            opponent_team_id = visitor_team_id
        elif team_id == visitor_team_id:
            opponent_team_id = home_team_id
        else:
            opponent_team_id = None

        pts = entry.get("pts", 0) or 0
        reb = entry.get("reb", 0) or 0
        ast = entry.get("ast", 0) or 0
        stl = entry.get("stl", 0) or 0
        blk = entry.get("blk", 0) or 0
        tov = entry.get("turnover", 0) or 0
        fg3m = entry.get("fg3m", 0) or 0
        fga = entry.get("fga", 0) or 0
        fg3a = entry.get("fg3a", 0) or 0
        fta = entry.get("fta", 0) or 0
        ftm = entry.get("ftm", 0) or 0
        fgm = entry.get("fgm", 0) or 0

        # Per-minute shot attempt rates
        fga_per_min = round(fga / minutes, 4) if minutes > 0 else 0.0
        fg3a_per_min = round(fg3a / minutes, 4) if minutes > 0 else 0.0
        fta_per_min = round(fta / minutes, 4) if minutes > 0 else 0.0

        # Approximate usage rate (simplified, team-agnostic proxy)
        # True USG% = 100 * ((FGA + 0.44*FTA + TOV) * (Tm_MP / 5))
        #             / (MP * (Tm_FGA + 0.44*Tm_FTA + Tm_TOV))
        # We use the numerator component per minute as a proxy.
        usg_proxy = round(
            (fga + 0.44 * fta + tov) / minutes, 4
        ) if minutes > 0 else 0.0

        # True Shooting Percentage
        # TS% = PTS / (2 * (FGA + 0.44 * FTA))
        tsa = fga + 0.44 * fta
        ts_pct = round(pts / (2.0 * tsa), 4) if tsa > 0 else 0.0

        # DK Fantasy Points (standard scoring)
        dk_fp = (
            pts * 1.0 + reb * 1.25 + ast * 1.5
            + stl * 2.0 + blk * 2.0 - tov * 0.5
        )
        # Double-double / Triple-double bonuses
        stat_cats = [pts, reb, ast, stl, blk]
        tens = sum(1 for x in stat_cats if x >= 10)
        if tens >= 2:
            dk_fp += 1.5  # double-double
        if tens >= 3:
            dk_fp += 3.0  # triple-double

        enriched.append({
            "bdl_stat_id": entry.get("id"),
            "player_id": pid,
            "player_name": player_name,
            "team_id": team_id,
            "team_abbreviation": team_abbr,
            "opponent_team_id": opponent_team_id,
            "game_id": game.get("id"),
            "game_date": game.get("date", ""),
            "minutes": round(minutes, 1),
            # Raw stats
            "pts": pts, "reb": reb, "ast": ast,
            "stl": stl, "blk": blk, "tov": tov, "fg3m": fg3m,
            "fga": fga, "fgm": fgm, "fg3a": fg3a,
            "fta": fta, "ftm": ftm,
            # Per-minute rates
            "fga_per_min": fga_per_min,
            "fg3a_per_min": fg3a_per_min,
            "fta_per_min": fta_per_min,
            # Advanced metrics
            "usg_proxy": usg_proxy,
            "ts_pct": ts_pct,
            "dk_actual_fp": round(dk_fp, 1),
        })

    return enriched


# ============================================================================
# AI Prompts
# ============================================================================

SYSTEM_PROMPT = """You are an NBA projection accuracy analyst.

Given historical projection vs actual data, identify systematic biases
and suggest calibration adjustments.

Look for patterns:
1. **Position bias**: Do we consistently over/under-project certain positions?
2. **B2B bias**: Are projections less accurate on back-to-backs?
3. **Minutes bias**: Do we over-project bench players or under-project starters?
4. **Game context**: Are projections worse in blowouts, high-pace games, etc.?
5. **Coach-specific**: Any coach's team consistently off?

Return JSON:
{
  "period": "description of the analysis period",
  "overall_mae": float (mean absolute error in minutes or FP),
  "overall_rmse": float,
  "biases": [
    {
      "category": "descriptive category (e.g. 'position_C', 'b2b_starters')",
      "direction": "over" | "under",
      "magnitude": float (average error),
      "sample_size": int,
      "confidence": float 0.0-1.0,
      "suggested_adjustment": float (multiplier to apply, e.g. 0.95 = reduce 5%)
    }
  ],
  "recommendations": ["actionable suggestion 1", "suggestion 2"],
  "calibration_adjustments": { "category_key": adjustment_float, ... }
}

Only include biases with sample_size >= 10 and confidence >= 0.6.
"""

PROJECTION_ACCURACY_PROMPT = """You are an NBA DFS projection accuracy analyst.

You receive detailed per-player, per-game data comparing our projected stats
to actual box score stats.  Your job is to identify systematic biases in our
projection model and suggest calibration multipliers.

Analyse these dimensions:

1. **Per-stat rate accuracy**: For each stat category (PTS, REB, AST, STL, BLK, TOV, FG3M),
   compare projected vs actual.  Are we systematically over/under-projecting specific stats?

2. **Position-specific stat biases**: Do certain positions have stat projection errors?
   e.g. over-projecting Center rebounds, under-projecting PG assists.

3. **Minutes projection accuracy**: Compare projected vs actual minutes.  Are we over-projecting
   bench players?  Under-projecting starters?

4. **Salary tier bias**: Group players by salary tier (high=$8K+, mid=$5-8K, value=<$5K).
   Do we systematically over/under-project expensive or cheap players?

5. **Fantasy point accuracy**: Compare projected DK FP vs actual DK FP.  What's the MAE?
   Which player archetypes have the worst accuracy?

Return JSON:
{
  "period": "description of the analysis period",
  "overall_mae": float (mean absolute error in DK fantasy points),
  "overall_rmse": float (root mean square error in DK fantasy points),
  "minutes_mae": float (mean absolute error in minutes),
  "biases": [
    {
      "category": "descriptive (e.g. 'stat_rate_reb', 'position_C_ast', 'salary_high')",
      "direction": "over" | "under",
      "magnitude": float (average error),
      "sample_size": int,
      "confidence": float 0.0-1.0,
      "suggested_adjustment": float (multiplier, e.g. 0.95 = reduce by 5%)
    }
  ],
  "recommendations": ["actionable suggestion 1", ...],
  "calibration_adjustments": {
    "position_PG_bias": 0.97,
    "position_SG_bias": 1.0,
    "position_SF_bias": 1.02,
    "position_PF_bias": 1.0,
    "position_C_bias": 0.98,
    "stat_rate_pts": 1.03,
    "stat_rate_reb": 0.95,
    "stat_rate_ast": 1.02,
    "stat_rate_stl": 1.0,
    "stat_rate_blk": 1.0,
    "stat_rate_tov": 1.0,
    "stat_rate_fg3m": 1.0,
    "dvp_sensitivity": 0.90,
    "pace_sensitivity_pts": 1.0,
    "pace_sensitivity_reb": 1.0,
    "salary_tier_high_projection": 0.96,
    "salary_tier_mid_projection": 1.0,
    "salary_tier_value_projection": 1.04,
    "game_context_b2b": 0.94,
    "minutes_blend_season_weight": 1.0,
    "minutes_blend_recent_weight": 1.0
  }
}

Rules for calibration_adjustments:
- All values are multipliers (1.0 = no change, 0.95 = reduce 5%, 1.05 = increase 5%)
- Keep adjustments within 0.85 to 1.15 range (max +-15%)
- Only include keys where you have sufficient evidence (sample_size >= 10)
- Use 1.0 for any category where the data is insufficient or shows no bias
- "over" projection means we predict too high, so adjustment should be < 1.0
- "under" projection means we predict too low, so adjustment should be > 1.0
"""


GPP_POSTMORTEM_PROMPT = """You are a DFS GPP tournament structure analyst.

You receive PRE-COMPUTED statistics about the top finishers in DraftKings
GPP tournaments. These statistics describe the STRUCTURAL profile of
winning lineups -- salary spent, ownership sum, stacking patterns,
contrarian exposure.

Your job is NOT to generate calibration multipliers (Agent 12 does that).
Your job is to recommend CONCRETE ILP constraint parameter values that
the lineup optimizer should use when building GPP lineups.

Given the pre-computed stats, recommend values for these constraint parameters:

1. **gpp_ownership_cap**: Maximum total projected ownership across all
   rostered players. Currently {current_ownership_cap}. If winners averaged
   {avg_own}% total ownership, the cap should be near that level.

2. **gpp_pivot_threshold**: Ownership threshold below which a player
   counts as a "pivot" (differentiator). Currently {current_pivot_threshold}%.

3. **gpp_pivot_min_count**: Minimum number of pivot players required.
   Currently {current_pivot_min_count}.

4. **gpp_ceiling_weight**: Blend weight for ceiling projection in the ILP
   objective (0.0 = pure median, 1.0 = pure ceiling). Currently {current_ceiling_weight}.

5. **gpp_bringback_salary_threshold**: Salary threshold above which a
   player triggers the bring-back constraint. Currently ${current_bringback_threshold}.

6. **gpp_salary_floor_pct**: Minimum salary utilization as % of cap.
   Currently {current_salary_floor_pct}%.

Return JSON:
{{
  "contest_count": int,
  "top_n_analyzed": int,
  "date_range": "description",
  "recommended_constraints": [
    {{
      "constraint_key": "gpp_ownership_cap",
      "current_value": float,
      "recommended_value": float,
      "confidence": float 0.0-1.0,
      "reasoning": "Winners averaged X% total ownership..."
    }}
  ],
  "constraint_overrides": {{
    "gpp_ownership_cap": float,
    "gpp_pivot_threshold": float,
    "gpp_pivot_min_count": int,
    "gpp_ceiling_weight": float,
    "gpp_bringback_salary_threshold": float,
    "gpp_salary_floor_pct": float
  }},
  "reasoning": "2-3 sentence summary of the structural profile",
  "recommendations": ["actionable insight 1", "insight 2"]
}}

Rules:
- Base all recommendations on the pre-computed statistics -- do NOT re-calculate them
- Be conservative -- small changes from current values unless data is overwhelming
- gpp_ownership_cap should be within [80, 200]
- gpp_ceiling_weight should be within [0.0, 0.60]
- gpp_pivot_min_count should be 0, 1, or 2
- gpp_salary_floor_pct should be within [90, 100]
- Only recommend changes where the data strongly supports it (confidence >= 0.6)
- If data is insufficient for a parameter, keep the current value
"""


# ============================================================================
# BacktestingAgent class
# ============================================================================


class BacktestingAgent:
    """AI-powered projection accuracy analysis with BDL integration.

    This agent runs nightly to compare yesterday's projections against
    actual box scores fetched from the BallDontLie API.  It computes
    calibration adjustments across multiple dimensions:

    - Position bias (per-position FP over/under-projection)
    - Salary tier bias (high/mid/value tier accuracy)
    - Per-stat rate accuracy (PTS, REB, AST, etc.)
    - B2B fatigue effects
    - DvP sensitivity (3-game rolling window)
    - Noise sigma tuning (per-player volatility)
    - Shot-rate decomposition (FGA/min, 3PA/min, FTA/min)

    Parameters
    ----------
    ai_service : AIService
        Central AI service for LLM-assisted pattern interpretation.
        When unavailable, all analysis falls back to deterministic
        computation.
    bdl_service : optional
        Injected ``BallDontLieService`` for sync BDL access.  When
        ``None``, the agent uses direct async ``httpx`` calls.
    calibration_service : optional
        Injected ``CalibrationService`` for saving calibrations.
        When ``None``, the ``run_nightly_backtest()`` method returns
        the computed adjustments without persisting.
    """

    def __init__(
        self,
        ai_service: AIService,
        bdl_service=None,
        calibration_service=None,
    ):
        self._ai = ai_service
        self._bdl = bdl_service
        self._calibration = calibration_service

        _settings = get_settings()
        self._api_key: str = _settings.balldontlie_api_key

    @property
    def is_available(self) -> bool:
        return self._ai.is_available

    # ══════════════════════════════════════════════════════════════════
    # Section 1: BDL Data Fetching
    # ══════════════════════════════════════════════════════════════════

    async def fetch_yesterday_stats(
        self,
        date_str: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Fetch and enrich yesterday's box scores from BDL.

        Parameters
        ----------
        date_str : str, optional
            Date in YYYY-MM-DD format.  Defaults to yesterday (UTC).

        Returns
        -------
        list of dict
            Enriched player stat dicts with per-minute rates, USG
            proxy, and true shooting percentage.
        """
        if date_str is None:
            yesterday = datetime.now(timezone.utc) - timedelta(days=1)
            date_str = yesterday.strftime("%Y-%m-%d")

        # Try injected BDL service first (sync path with circuit breaker)
        if self._bdl and self._bdl.is_available:
            try:
                raw = self._bdl.get_player_stats(date=date_str)
                if raw:
                    enriched = enrich_box_scores_with_rates(raw)
                    logger.info(
                        "[Backtest] BDL sync: %d raw -> %d enriched stats for %s",
                        len(raw), len(enriched), date_str,
                    )
                    return enriched
            except Exception as exc:
                logger.warning(
                    "[Backtest] BDL sync fetch failed: %s", exc
                )

        # Fallback: direct async httpx
        if self._api_key:
            raw = await fetch_yesterday_box_scores(date_str, self._api_key)
            enriched = enrich_box_scores_with_rates(raw)
            return enriched

        logger.warning("[Backtest] No BDL API key — cannot fetch stats")
        return []

    # ══════════════════════════════════════════════════════════════════
    # Section 2: DB Query — Projected Rates
    # ══════════════════════════════════════════════════════════════════

    @staticmethod
    async def query_projected_rates(
        date_str: str,
    ) -> Dict[int, Dict[str, float]]:
        """Query yesterday's projected per-minute rates from the database.

        Reads from ``PlayerMinutesHistory`` table to get the system's
        projected values that were stored at ingestion time before
        tip-off.

        Parameters
        ----------
        date_str : str
            Game date in ``YYYY-MM-DD`` format.

        Returns
        -------
        dict
            ``{player_id: {projected_minutes, projected_pts, projected_reb,
            projected_ast, ..., projected_fg3a_rate, ...}}``
        """
        from app.db.database import is_db_available, get_session
        from app.db.models import PlayerMinutesHistory
        from sqlalchemy import select

        if not is_db_available():
            return {}

        result: Dict[int, Dict[str, float]] = {}

        try:
            # Parse date for range query (whole day)
            dt = datetime.strptime(date_str, "%Y-%m-%d").replace(
                tzinfo=timezone.utc
            )
            day_start = dt
            day_end = dt + timedelta(days=1)

            async with get_session() as session:
                stmt = (
                    select(PlayerMinutesHistory)
                    .where(PlayerMinutesHistory.game_date >= day_start)
                    .where(PlayerMinutesHistory.game_date < day_end)
                    .where(PlayerMinutesHistory.sport == "nba")
                )
                rows = await session.execute(stmt)
                records = rows.scalars().all()

            for r in records:
                result[r.player_id] = {
                    "projected_minutes": r.projected_minutes or 0.0,
                    "projected_fp": r.dk_projected_fp or 0.0,
                    "projected_pts": r.projected_pts or 0.0,
                    "projected_reb": r.projected_reb or 0.0,
                    "projected_ast": r.projected_ast or 0.0,
                    "projected_stl": r.projected_stl or 0.0,
                    "projected_blk": r.projected_blk or 0.0,
                    "projected_tov": r.projected_tov or 0.0,
                    "projected_fg3m": r.projected_fg3m or 0.0,
                    "projected_fg3a_rate": r.projected_fg3a_rate,
                    "projected_fga_rate": r.projected_fga_rate,
                    "projected_fta_rate": r.projected_fta_rate,
                    "dk_salary": r.dk_salary or 0,
                    "position": r.position or "?",
                    "team_id": r.team_id,
                    "opponent_team_id": getattr(r, "opponent_team_id", None),
                    "baseline_minutes": r.baseline_minutes or 0.0,
                }

            logger.info(
                "[Backtest] Loaded %d projected rate records for %s",
                len(result), date_str,
            )

        except Exception as exc:
            logger.warning(
                "[Backtest] Failed to query projected rates: %s", exc
            )

        return result

    # ══════════════════════════════════════════════════════════════════
    # Section 3: Calibration Engine (merge actual vs projected)
    # ══════════════════════════════════════════════════════════════════

    @staticmethod
    def build_accuracy_data(
        enriched_actuals: List[Dict[str, Any]],
        projected_rates: Dict[int, Dict[str, float]],
    ) -> List[Dict]:
        """Merge actual BDL stats with projected rates into accuracy_data.

        Produces the same dict structure consumed by
        ``compute_accuracy_stats``, ``compute_dvp_rolling_window``,
        ``compute_player_volatility_sigma``, etc.

        For each player-game in ``enriched_actuals``, looks up the
        corresponding projected values from ``projected_rates`` (keyed
        by ``player_id``).  Only includes players that have BOTH actual
        and projected data.

        Parameters
        ----------
        enriched_actuals : list of dict
            Output of ``enrich_box_scores_with_rates()``.
        projected_rates : dict
            Output of ``query_projected_rates()``.

        Returns
        -------
        list of dict
            Merged accuracy data ready for calibration functions.
        """
        accuracy_data: List[Dict] = []

        for actual in enriched_actuals:
            pid = actual.get("player_id")
            if pid is None or pid not in projected_rates:
                continue

            proj = projected_rates[pid]
            proj_min = proj.get("projected_minutes", 0)
            if proj_min <= 0:
                continue  # No projection -> skip

            actual_min = actual.get("minutes", 0)

            entry = {
                "player_id": pid,
                "player_name": actual.get("player_name", ""),
                "position": proj.get("position", "?"),
                "team_id": proj.get("team_id"),
                "dk_salary": proj.get("dk_salary", 0),
                "opponent_team_id": (
                    actual.get("opponent_team_id")
                    or proj.get("opponent_team_id")
                ),
                "game_date": actual.get("game_date", ""),
                # Minutes
                "projected_minutes": proj_min,
                "actual_minutes": actual_min,
                "baseline_minutes": proj.get("baseline_minutes", 0),
                # Fantasy points
                "projected_fp": proj.get("projected_fp", 0),
                "actual_fp": actual.get("dk_actual_fp", 0),
                # Per-stat
                "projected_pts": proj.get("projected_pts", 0),
                "actual_pts": actual.get("pts", 0),
                "projected_reb": proj.get("projected_reb", 0),
                "actual_reb": actual.get("reb", 0),
                "projected_ast": proj.get("projected_ast", 0),
                "actual_ast": actual.get("ast", 0),
                "projected_stl": proj.get("projected_stl", 0),
                "actual_stl": actual.get("stl", 0),
                "projected_blk": proj.get("projected_blk", 0),
                "actual_blk": actual.get("blk", 0),
                "projected_tov": proj.get("projected_tov", 0),
                "actual_tov": actual.get("tov", 0),
                "projected_fg3m": proj.get("projected_fg3m", 0),
                "actual_fg3m": actual.get("fg3m", 0),
                # Shooting rates
                "actual_fg3a_rate": actual.get("fg3a_per_min"),
                "actual_fga_rate": actual.get("fga_per_min"),
                "actual_fta_rate": actual.get("fta_per_min"),
                "projected_fg3a_rate": proj.get("projected_fg3a_rate"),
                "projected_fga_rate": proj.get("projected_fga_rate"),
                "projected_fta_rate": proj.get("projected_fta_rate"),
                # Advanced metrics (from BDL enrichment)
                "usg_proxy": actual.get("usg_proxy"),
                "ts_pct": actual.get("ts_pct"),
                # B2B flag will be set by caller or pipeline
                "was_b2b": False,
            }
            accuracy_data.append(entry)

        logger.info(
            "[Backtest] Built %d accuracy entries (%d actuals, %d projections)",
            len(accuracy_data), len(enriched_actuals), len(projected_rates),
        )
        return accuracy_data

    # ══════════════════════════════════════════════════════════════════
    # Section 4: Nightly Orchestrator
    # ══════════════════════════════════════════════════════════════════

    async def run_nightly_backtest(
        self,
        date_str: Optional[str] = None,
        historical_data: Optional[List[Dict]] = None,
    ) -> Dict[str, Any]:
        """Run the full nightly backtesting pipeline.

        This is the top-level method called by the nightly pipeline.
        It orchestrates the full flow:

        1. Fetch yesterday's box scores from BDL (or use provided data)
        2. Query projected rates from the database
        3. Merge into accuracy_data
        4. Compute deterministic calibrations (position, salary, stat, B2B)
        5. Compute DvP rolling-window recalibration
        6. Compute per-player volatility sigma tuning
        7. Compute shot-rate decomposition
        8. Optionally run AI-assisted pattern interpretation
        9. Persist all calibrations via CalibrationService

        Parameters
        ----------
        date_str : str, optional
            Date to analyse.  Defaults to yesterday (UTC).
        historical_data : list of dict, optional
            Pre-built accuracy data.  If provided, skips Steps 1-3
            (useful for testing or when called from existing pipeline).

        Returns
        -------
        dict
            Summary with keys: ``calibrations_saved``, ``stats``,
            ``adjustments``, ``source``.
        """
        if date_str is None:
            yesterday = datetime.now(timezone.utc) - timedelta(days=1)
            date_str = yesterday.strftime("%Y-%m-%d")

        result: Dict[str, Any] = {
            "date": date_str,
            "calibrations_saved": 0,
            "adjustments": {},
            "stats": {},
            "source": "none",
        }

        # ── Step 1-3: Build accuracy_data ───────────────────────────
        if historical_data is not None:
            accuracy_data = historical_data
        else:
            enriched = await self.fetch_yesterday_stats(date_str)
            if not enriched:
                logger.warning(
                    "[Backtest] No BDL stats for %s — aborting", date_str
                )
                return result

            projected = await self.query_projected_rates(date_str)
            if not projected:
                logger.warning(
                    "[Backtest] No projected rates for %s — aborting", date_str
                )
                return result

            accuracy_data = self.build_accuracy_data(enriched, projected)

        if not accuracy_data:
            logger.warning("[Backtest] No accuracy data — aborting")
            return result

        # ── Step 4: Core deterministic calibrations ──────────────────
        all_adjustments: Dict[str, float] = {}

        det_calibrations = compute_deterministic_calibrations(accuracy_data)
        all_adjustments.update(det_calibrations)

        # ── Step 5: DvP rolling-window recalibration ─────────────────
        dvp_adjustments = compute_dvp_rolling_window(accuracy_data)
        all_adjustments.update(dvp_adjustments)

        # Also compute the full-history DvP for comparison
        dvp_full = compute_dvp_recalibration(accuracy_data)
        # Prefer rolling window when available; fall back to full-history
        for key, val in dvp_full.items():
            if key not in all_adjustments:
                all_adjustments[key] = val

        # ── Step 6: Per-player volatility sigma tuning ───────────────
        sigma_adjustments = compute_player_volatility_sigma(accuracy_data)
        # Merge with global sigma (prefer player-level when available)
        sigma_global = compute_noise_sigma_adjustments(accuracy_data)
        for key, val in sigma_global.items():
            if key not in sigma_adjustments:
                sigma_adjustments[key] = val
        all_adjustments.update(sigma_adjustments)

        # ── Step 7: Shot-rate decomposition ──────────────────────────
        shot_stats = compute_shot_decomposition_stats(accuracy_data)
        shot_calibrations = _shot_rate_to_calibration_keys(shot_stats)
        all_adjustments.update(shot_calibrations)

        # ── Step 8: AI-assisted interpretation (optional) ────────────
        ai_analysis: Optional[BacktestAnalysis] = None
        if self._ai.is_available:
            has_stat_data = any(
                r.get("actual_pts", 0) > 0 for r in accuracy_data
            )
            if has_stat_data:
                ai_analysis = self.analyze_projection_accuracy(
                    accuracy_data,
                    context={"period": f"nightly_{date_str}"},
                )
            else:
                ai_analysis = self.analyze_accuracy(
                    accuracy_data,
                    context={"period": f"nightly_{date_str}"},
                )

            if ai_analysis and ai_analysis.calibration_adjustments:
                # AI adjustments override deterministic for shared keys
                all_adjustments.update(ai_analysis.calibration_adjustments)
                result["source"] = "ai+deterministic"
            else:
                result["source"] = "deterministic"
        else:
            result["source"] = "deterministic"

        # ── Step 9: Persist via CalibrationService ───────────────────
        stats = compute_accuracy_stats(accuracy_data)
        result["stats"] = stats
        result["adjustments"] = all_adjustments

        if all_adjustments and self._calibration:
            try:
                saved = await self._calibration.save_backtest_calibrations(
                    all_adjustments,
                    metadata={
                        "game_count": len(accuracy_data),
                        "source": result["source"],
                        "reasoning": (
                            f"Nightly backtest {date_str}: "
                            f"FP_bias={stats.get('overall_fp_bias', 0):.2f}, "
                            f"FP_MAE={stats.get('overall_fp_mae', 0):.2f}, "
                            f"{len(all_adjustments)} adjustments "
                            f"({len(dvp_adjustments)} DvP, "
                            f"{len(sigma_adjustments)} sigma, "
                            f"{len(shot_calibrations)} shot-rate)"
                        ),
                    },
                )
                result["calibrations_saved"] = saved

                # Refresh in-memory cache
                await self._calibration.load_calibrations()

                logger.info(
                    "[Backtest] Nightly %s: %d calibrations saved "
                    "(FP_MAE=%.2f, source=%s)",
                    date_str, saved,
                    stats.get("overall_fp_mae", 0),
                    result["source"],
                )
            except Exception as exc:
                logger.error(
                    "[Backtest] Failed to save calibrations: %s", exc
                )
        elif all_adjustments:
            logger.info(
                "[Backtest] %d adjustments computed but no CalibrationService "
                "injected — returning without persistence",
                len(all_adjustments),
            )

        return result

    # ══════════════════════════════════════════════════════════════════
    # Section 5: AI-Assisted Analysis Methods (unchanged API)
    # ══════════════════════════════════════════════════════════════════

    def analyze_accuracy(
        self,
        accuracy_data: List[Dict],
        context: Optional[Dict] = None,
        sport: str = "nba",
    ) -> Optional[BacktestAnalysis]:
        """Analyse historical accuracy data for systematic biases.

        Statistics are computed deterministically in Python and passed
        to the LLM as pre-computed summaries for pattern interpretation.

        Parameters
        ----------
        accuracy_data : list of dict
            Each with: player_name, position, team_id, projected_minutes,
            actual_minutes, projected_fp, actual_fp, was_b2b, coach_name,
            game_date.
        context : dict, optional
            period, total_games, etc.
        """
        if not self._ai.is_available or not accuracy_data:
            return None

        ctx = context or {}

        # ── Pre-compute accuracy stats deterministically ──
        stats_summary = compute_accuracy_stats(accuracy_data)

        user_prompt = (
            f"**Period**: {ctx.get('period', 'recent')}\n"
            f"**Total records**: {stats_summary.get('sample_size', 0)}\n\n"
            f"**Pre-computed accuracy statistics** (computed in code, verified correct):\n\n"
            f"```json\n{json.dumps(stats_summary, indent=2)}\n```\n\n"
            f"These statistics are mathematically correct — do NOT re-calculate them.\n"
            f"Use 'overall_fp_mae' / 'overall_fp_rmse' as-is in your output.\n"
            f"Identify systematic bias patterns and suggest calibration adjustments."
        )

        request = AIRequest(
            system_prompt=get_sport_preamble(sport) + SYSTEM_PROMPT,
            user_prompt=user_prompt,
            model_tier="reasoning",
            max_tokens=2048,
            temperature=0.3,
            response_format="json",
            agent_name="backtesting",
        )

        data = self._ai.complete_json(request)
        if data is None:
            return None

        try:
            result = BacktestAnalysis(**data)
            logger.info(
                f"[Backtest] {len(result.biases)} biases found, "
                f"MAE={result.overall_mae:.1f}, "
                f"{len(result.calibration_adjustments)} adjustments"
            )
            return result
        except Exception as exc:
            logger.warning(f"[BacktestAgent] Parse failed: {exc}")
            return None

    def analyze_projection_accuracy(
        self,
        accuracy_data: List[Dict],
        context: Optional[Dict] = None,
        sport: str = "nba",
    ) -> Optional[BacktestAnalysis]:
        """Enhanced projection accuracy analysis with per-stat breakdowns.

        Statistics (MAE, RMSE, bias) are computed deterministically in
        Python via ``compute_accuracy_stats()`` and passed to the LLM
        as pre-computed summaries.  The LLM's job is ONLY to interpret
        patterns and suggest calibration multipliers — not arithmetic.

        Parameters
        ----------
        accuracy_data : list of dict
            Each with: player_name, position, team_id, dk_salary,
            projected_minutes, actual_minutes,
            projected_fp, actual_fp,
            projected_pts, actual_pts, projected_reb, actual_reb, etc.
        context : dict, optional
            period, total_games, etc.
        """
        if not self._ai.is_available or not accuracy_data:
            return None

        ctx = context or {}

        # ── Pre-compute all accuracy stats deterministically ──
        stats_summary = compute_accuracy_stats(accuracy_data)

        user_prompt = (
            f"**Period**: {ctx.get('period', 'recent')}\n"
            f"**Total records**: {stats_summary.get('sample_size', 0)}\n\n"
            f"**Pre-computed accuracy statistics** (computed in code, verified correct):\n\n"
            f"```json\n{json.dumps(stats_summary, indent=2)}\n```\n\n"
            f"These statistics are mathematically correct — do NOT re-calculate them.\n"
            f"Your job is to:\n"
            f"1. Use the pre-computed 'overall_fp_mae' and 'overall_fp_rmse' as-is for the output\n"
            f"2. Interpret the bias patterns across positions, stats, salary tiers, and B2B\n"
            f"3. Suggest calibration multipliers based on the direction and magnitude of biases\n"
            f"4. A positive bias means we under-project (actual > projected), suggest > 1.0\n"
            f"5. A negative bias means we over-project (actual < projected), suggest < 1.0\n"
        )

        request = AIRequest(
            system_prompt=get_sport_preamble(sport) + PROJECTION_ACCURACY_PROMPT,
            user_prompt=user_prompt,
            model_tier="reasoning",
            max_tokens=3000,
            temperature=0.3,
            response_format="json",
            agent_name="backtesting",
        )

        data = self._ai.complete_json(request)
        if data is None:
            return None

        try:
            result = BacktestAnalysis(**data)
            logger.info(
                f"[Backtest] Enhanced analysis: {len(result.biases)} biases, "
                f"MAE={result.overall_mae:.1f} FP, "
                f"{len(result.calibration_adjustments)} calibration adjustments"
            )
            return result
        except Exception as exc:
            logger.warning(f"[BacktestAgent] Enhanced parse failed: {exc}")
            return None

    def get_calibration_adjustments(
        self, accuracy_data: List[Dict]
    ) -> Dict[str, float]:
        """Convenience method: run analysis and return just the adjustments.

        Returns empty dict if analysis fails.
        """
        analysis = self.analyze_accuracy(accuracy_data)
        if analysis is None:
            return {}
        return analysis.calibration_adjustments

    # ------------------------------------------------------------------
    # GPP Post-Mortem
    # ------------------------------------------------------------------

    def analyze_gpp_postmortem(
        self,
        top_entries: List[Dict],
        contest_metadata: Optional[Dict] = None,
        player_team_map: Optional[Dict[str, str]] = None,
        ownership_map: Optional[Dict[str, float]] = None,
        sport: str = "nba",
    ) -> Optional["GPPContestBlueprint"]:
        """Analyse top GPP finishers and extract structural constraint parameters.

        Statistics are computed deterministically, then the AI interprets
        the patterns and recommends ILP constraint overrides.

        Parameters
        ----------
        top_entries : list of dict
            Top N entries per contest:
            ``{rank, points, lineup_data, total_salary}``
        contest_metadata : dict, optional
            ``contest_count``, ``date_range``, ``top_n``, etc.
        player_team_map : dict, optional
            Normalised name -> team abbreviation for stacking detection.
        ownership_map : dict, optional
            Normalised name -> projected ownership % for ownership analysis.
        """
        from app.models.ai import GPPContestBlueprint

        if not top_entries:
            return None

        ctx = contest_metadata or {}

        # Step 1: Deterministic computation
        stats = compute_gpp_blueprint(top_entries, player_team_map, ownership_map)
        if not stats:
            return None

        # Step 2: Deterministic constraint mapping (fallback)
        from app.config.constants import (
            GPP_OWNERSHIP_CAP,
            GPP_PIVOT_OWNERSHIP_THRESHOLD,
            GPP_PIVOT_MIN_COUNT,
            GPP_CEILING_WEIGHT,
            GPP_BRINGBACK_SALARY_THRESHOLD,
            GPP_SALARY_FLOOR_PCT,
        )
        current_constants = {
            "gpp_ownership_cap": GPP_OWNERSHIP_CAP,
            "gpp_pivot_threshold": GPP_PIVOT_OWNERSHIP_THRESHOLD,
            "gpp_pivot_min_count": GPP_PIVOT_MIN_COUNT,
            "gpp_ceiling_weight": GPP_CEILING_WEIGHT,
            "gpp_bringback_salary_threshold": GPP_BRINGBACK_SALARY_THRESHOLD,
            "gpp_salary_floor_pct": GPP_SALARY_FLOOR_PCT,
        }
        det_overrides = compute_deterministic_gpp_constraints(stats, current_constants)

        if not self._ai.is_available:
            return GPPContestBlueprint(
                contest_count=ctx.get("contest_count", 0),
                top_n_analyzed=ctx.get("top_n", 10),
                date_range=ctx.get("date_range", ""),
                observed_stats=stats,
                constraint_overrides=det_overrides,
                reasoning="Deterministic analysis (AI unavailable)",
            )

        # Step 3: AI-assisted interpretation
        prompt = GPP_POSTMORTEM_PROMPT.format(
            current_ownership_cap=GPP_OWNERSHIP_CAP,
            avg_own=stats.get("avg_total_ownership", "N/A"),
            current_pivot_threshold=GPP_PIVOT_OWNERSHIP_THRESHOLD,
            current_pivot_min_count=GPP_PIVOT_MIN_COUNT,
            current_ceiling_weight=GPP_CEILING_WEIGHT,
            current_bringback_threshold=GPP_BRINGBACK_SALARY_THRESHOLD,
            current_salary_floor_pct=GPP_SALARY_FLOOR_PCT,
        )

        user_prompt = (
            f"**Contests**: {ctx.get('contest_count', 'unknown')}\n"
            f"**Date range**: {ctx.get('date_range', 'recent')}\n"
            f"**Top finishers analysed**: {stats.get('sample_size', 0)}\n\n"
            f"**Pre-computed structural statistics** (computed in code, verified correct):\n\n"
            f"```json\n{json.dumps(stats, indent=2)}\n```\n\n"
            f"These statistics are mathematically correct — do NOT re-calculate them.\n"
            f"Recommend concrete ILP constraint parameter values based on this data."
        )

        request = AIRequest(
            system_prompt=get_sport_preamble(sport) + prompt,
            user_prompt=user_prompt,
            model_tier="reasoning",
            max_tokens=2048,
            temperature=0.3,
            response_format="json",
            agent_name="backtesting",
        )

        data = self._ai.complete_json(request)
        if data is None:
            return GPPContestBlueprint(
                contest_count=ctx.get("contest_count", 0),
                top_n_analyzed=ctx.get("top_n", 10),
                date_range=ctx.get("date_range", ""),
                observed_stats=stats,
                constraint_overrides=det_overrides,
                reasoning="AI call failed; deterministic fallback",
            )

        try:
            data["observed_stats"] = stats
            result = GPPContestBlueprint(**data)
            logger.info(
                f"[GPP PostMortem] {len(result.constraint_overrides)} constraint "
                f"overrides from {stats.get('sample_size', 0)} top finishers"
            )
            return result
        except Exception as exc:
            logger.warning(f"[GPP PostMortem] Parse failed: {exc}")
            return GPPContestBlueprint(
                contest_count=ctx.get("contest_count", 0),
                top_n_analyzed=ctx.get("top_n", 10),
                date_range=ctx.get("date_range", ""),
                observed_stats=stats,
                constraint_overrides=det_overrides,
                reasoning=f"AI parse failed ({exc}); deterministic fallback",
            )
