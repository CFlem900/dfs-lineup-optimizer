#!/usr/bin/env python3
"""Fantasy Point Tracer — Step-by-step diagnostic of a player's DK fantasy point projection.

Traces the FULL lifecycle of a player's fantasy point projection through the
DFS pipeline, printing a color-coded console log showing exactly how projected
minutes convert to per-minute stat rates, contextual modifiers, a raw stat
line, and finally DraftKings fantasy points.

This is the "next layer" after minute_tracer.py — it picks up from the final
projected minutes and traces every mathematical step to the final DKFP.

Usage
-----
  python scripts/fantasy_point_tracer.py --player "Nikola Jokic" --synthetic
  python scripts/fantasy_point_tracer.py --player "Nikola Jokic" --synthetic --minutes 34.6
  python scripts/fantasy_point_tracer.py --player "Nikola Jokic" --synthetic --pace 106.5 --dvp 1.10
  python scripts/fantasy_point_tracer.py --player "Nikola Jokic" --synthetic --usage-boost 1.12 --verbose
  python scripts/fantasy_point_tracer.py --player "Jamal Murray" --team DEN --synthetic --verbose

Steps traced:
  1. Ingest Final Minutes + Per-Minute Stat Rates
  2. Baseline (Unadjusted) Stat Line
  3. Contextual Modifiers (Usage Boost, Pace, DvP)
  4. Adjusted Stat Line (with optional Shot Decomposition)
  5. DraftKings Scoring Engine (base + DD/TD Monte Carlo)
  6. Floor / Ceiling Range
  7. Final Summary (Waterfall)
"""

import argparse
import io
import math
import os
import sys
from typing import Dict, List, Optional, Tuple

import numpy as np

# ── Bootstrap — add backend/ to sys.path ──────────────────────────────
_BACKEND = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, _BACKEND)

from app.config.constants import (
    LEAGUE_AVG_PACE,
    PACE_SENSITIVITY,
    DD_STAT_CV,
    DD_STAT_VARIANCE,
    USAGE_BOOST_STAT_WEIGHTS,
    FLOOR_MINUTES_MULT,
    CEILING_MINUTES_MULT,
    FLOOR_RATE_MULT,
    CEILING_RATE_MULT,
    DVP_FLOOR_SENSITIVITY,
    DVP_CEILING_SENSITIVITY,
    PLAYER_CV_BASELINE,
    PLAYER_CV_FLOOR_SCALE,
    PLAYER_CV_CEILING_SCALE,
    PLAYER_CV_FALLBACK,
    SHOT_DECOMP_LEAGUE_AVG_FG3_PCT,
    SHOT_DECOMP_LEAGUE_AVG_FG2_PCT,
    SHOT_DECOMP_LEAGUE_AVG_FT_PCT,
)
from app.models.player import PlayerMinutes


# ── ANSI color helpers ────────────────────────────────────────────────
class C:
    RED   = "\033[91m"
    YEL   = "\033[93m"
    GRN   = "\033[92m"
    CYN   = "\033[96m"
    MAG   = "\033[95m"
    WHT   = "\033[97m"
    DIM   = "\033[90m"
    BLD   = "\033[1m"
    UND   = "\033[4m"
    RST   = "\033[0m"


def banner(msg: str):
    w = 66
    print(f"\n{C.BLD}{C.CYN}{'═' * w}")
    print(f"  {msg}")
    print(f"{'═' * w}{C.RST}")

def step(num: str, msg: str):
    print(f"\n{C.BLD}{C.MAG}  ┌─ Step {num}: {msg}{C.RST}")
    print(f"{C.MAG}  │{C.RST}")

def trace(label: str, value, color=C.WHT, unit=""):
    if isinstance(value, float):
        print(f"{C.MAG}  │{C.RST}   {C.DIM}{label:<40}{C.RST} {color}{value:>8.3f} {unit}{C.RST}")
    else:
        print(f"{C.MAG}  │{C.RST}   {C.DIM}{label:<40}{C.RST} {color}{str(value):>8} {unit}{C.RST}")

def trace_val(label: str, value, color=C.WHT, unit="", fmt=".1f"):
    """Trace with configurable float format."""
    if isinstance(value, float):
        print(f"{C.MAG}  │{C.RST}   {C.DIM}{label:<40}{C.RST} {color}{value:>8{fmt}} {unit}{C.RST}")
    else:
        print(f"{C.MAG}  │{C.RST}   {C.DIM}{label:<40}{C.RST} {color}{str(value):>8} {unit}{C.RST}")

def trace_delta(label: str, before: float, after: float, unit=""):
    delta = after - before
    if abs(delta) < 0.01:
        clr = C.DIM
        sign = " "
    elif delta > 0:
        clr = C.GRN
        sign = "+"
    else:
        clr = C.RED
        sign = ""
    print(
        f"{C.MAG}  │{C.RST}   {label:<40} "
        f"{C.DIM}{before:>7.1f}{C.RST} → "
        f"{clr}{after:>7.1f}{C.RST}  "
        f"{clr}({sign}{delta:.1f} {unit}){C.RST}"
    )

def trace_info(msg: str):
    print(f"{C.MAG}  │{C.RST}   {C.DIM}{msg}{C.RST}")

def trace_warn(msg: str):
    print(f"{C.MAG}  │{C.RST}   {C.YEL}⚠ {msg}{C.RST}")

def endstep(label: str, value: float, unit=""):
    print(f"{C.MAG}  │{C.RST}")
    print(f"{C.MAG}  └─▶{C.RST} {C.BLD}{label}: {C.GRN}{value:.1f} {unit}{C.RST}")

def trace_row(cols: List[str], widths: List[int], colors: Optional[List[str]] = None):
    """Print a table row within a step's vertical connector."""
    parts = []
    for i, (col, w) in enumerate(zip(cols, widths)):
        clr = colors[i] if colors and i < len(colors) else C.WHT
        parts.append(f"{clr}{col:>{w}}{C.RST}")
    print(f"{C.MAG}  │{C.RST}     {'  '.join(parts)}")


# ── DK Scoring Constants ─────────────────────────────────────────────

DK_WEIGHTS: Dict[str, float] = {
    "pts": 1.0, "reb": 1.25, "ast": 1.5,
    "stl": 2.0, "blk": 2.0, "tov": -0.5, "fg3m": 0.5,
}
DK_DD_BONUS = 1.5
DK_TD_BONUS = 3.0

STAT_ORDER = ["pts", "reb", "ast", "stl", "blk", "tov", "fg3m"]
STAT_LABELS = {
    "pts": "PTS", "reb": "REB", "ast": "AST", "stl": "STL",
    "blk": "BLK", "tov": "TOV", "fg3m": "FG3M",
}
RATE_ATTRS = {
    "pts": "pts_per_min", "reb": "reb_per_min", "ast": "ast_per_min",
    "stl": "stl_per_min", "blk": "blk_per_min", "tov": "tov_per_min",
    "fg3m": "fg3m_per_min",
}


# ── Position DvP Weights (from game_service.py) ──────────────────────

_POSITION_DvP_WEIGHTS: Dict[str, Dict[str, float]] = {
    "PG": {"pts": 1.0, "reb": 0.5, "ast": 1.4, "stl": 1.3, "blk": 0.3, "tov": 1.0, "fg3m": 1.2},
    "SG": {"pts": 1.2, "reb": 0.5, "ast": 0.8, "stl": 1.1, "blk": 0.3, "tov": 0.9, "fg3m": 1.3},
    "SF": {"pts": 1.1, "reb": 0.9, "ast": 0.7, "stl": 1.0, "blk": 0.6, "tov": 0.9, "fg3m": 1.1},
    "PF": {"pts": 1.0, "reb": 1.3, "ast": 0.6, "stl": 0.8, "blk": 1.1, "tov": 0.8, "fg3m": 0.9},
    "C":  {"pts": 0.9, "reb": 1.4, "ast": 0.5, "stl": 0.6, "blk": 1.4, "tov": 0.7, "fg3m": 0.5},
}


def build_position_dvp(dvp_uniform: float, position: str) -> Dict[str, float]:
    """Build position-specific DvP from a uniform override.

    Replicates game_service.get_position_dvp_factors() logic:
    deviation = dvp_uniform - 1.0
    dvp[stat] = clamp(1.0 + deviation * weight, 0.80, 1.20)
    """
    pos_key = position.upper().split("/")[0].split("-")[0]
    weights = _POSITION_DvP_WEIGHTS.get(pos_key, {})
    result = {}
    deviation = dvp_uniform - 1.0
    for stat in STAT_ORDER:
        w = weights.get(stat, 1.0)
        adjusted = 1.0 + deviation * w
        result[stat] = round(max(0.80, min(1.20, adjusted)), 3)
    return result


# ── Monte Carlo DD/TD Engine ─────────────────────────────────────────
# Replicated from dfs_service.py lines 264-324

_DD_STATS = ["pts", "reb", "ast", "stl", "blk"]
_DD_CORR = np.array([
    # pts   reb   ast   stl   blk
    [1.00, 0.15, 0.40, 0.05, 0.02],  # pts
    [0.15, 1.00, 0.10, 0.05, 0.20],  # reb
    [0.40, 0.10, 1.00, 0.10, 0.00],  # ast
    [0.05, 0.05, 0.10, 1.00, 0.15],  # stl
    [0.02, 0.20, 0.00, 0.15, 1.00],  # blk
])


def compute_dd_td_probs(stats: dict) -> Tuple[float, float]:
    """Compute P(DD) and P(TD) using correlated Monte Carlo simulation.

    Uses N=1000 samples with a fixed seed (42) and the 5x5 stat correlation
    matrix from dfs_service.py.  Returns (p_dd, p_td).
    """
    n_sims = 1000
    n_cats = len(_DD_STATS)

    means = np.array([stats.get(s, 0.0) for s in _DD_STATS])
    cvs = np.array([DD_STAT_CV.get(s, DD_STAT_VARIANCE) for s in _DD_STATS])
    sigmas = means * cvs

    # Early exit: if fewer than 2 stats have any chance of reaching 10
    viable = np.sum(means > 3.0)
    if viable < 2:
        return 0.0, 0.0

    # Build covariance from correlation + sigmas
    sigma_outer = np.outer(sigmas, sigmas)
    cov_matrix = _DD_CORR * sigma_outer

    # Ensure positive semi-definite
    cov_matrix = (cov_matrix + cov_matrix.T) / 2
    eigvals = np.linalg.eigvalsh(cov_matrix)
    if eigvals.min() < 0:
        cov_matrix += np.eye(n_cats) * (abs(eigvals.min()) + 1e-6)

    rng = np.random.default_rng(seed=42)
    samples = rng.multivariate_normal(means, cov_matrix, size=n_sims)
    hits = (samples >= 10.0).sum(axis=1)

    p_dd = float((hits >= 2).mean())
    p_td = float((hits >= 3).mean())
    return p_dd, p_td


def prob_over_10(projected: float, stat: str) -> float:
    """Marginal probability that a stat exceeds 10, using normal CDF."""
    if projected <= 0:
        return 0.0
    cv = DD_STAT_CV.get(stat, DD_STAT_VARIANCE)
    sigma = projected * cv
    if sigma <= 0:
        return 1.0 if projected >= 10.0 else 0.0
    # P(X >= 10) = 1 - Phi((10 - mean) / sigma)
    z = (10.0 - projected) / sigma
    return 1.0 - _normal_cdf(z)


def _normal_cdf(z: float) -> float:
    """Standard normal CDF via the error function."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2)))


# ── Stat Projection Functions ─────────────────────────────────────────

def project_stats(
    minutes: float,
    player: PlayerMinutes,
    pace_factor: float = 1.0,
    dvp_factors: Optional[Dict[str, float]] = None,
    usage_boost: float = 1.0,
) -> dict:
    """Project counting stats from minutes, per-min rates, pace, DvP, and usage boost.

    Replicates dfs_service._project_stats() (line 156):
      value = minutes * rate * effective_boost * pace_adj * dvp
    """
    stats = {}
    for field in STAT_ORDER:
        rate = getattr(player, RATE_ATTRS[field], 0.0)

        # Pace adjustment
        sens = PACE_SENSITIVITY.get(field, 0.5)
        pace_adj = 1.0 + (pace_factor - 1.0) * sens

        # DvP
        dvp = dvp_factors.get(field, 1.0) if dvp_factors else 1.0

        # Usage boost with per-stat sensitivity
        stat_weight = USAGE_BOOST_STAT_WEIGHTS.get(field, 0.5)
        effective_boost = 1.0 + (usage_boost - 1.0) * stat_weight

        value = minutes * rate * effective_boost * pace_adj * dvp
        stats[field] = round(value, 1)
    return stats


def project_stats_decomposed(
    minutes: float,
    player: PlayerMinutes,
    pace_factor: float = 1.0,
    dvp_factors: Optional[Dict[str, float]] = None,
    usage_boost: float = 1.0,
) -> Tuple[dict, Optional[dict]]:
    """Project stats with optional shot-type decomposition for PTS and FG3M.

    Replicates dfs_service._project_stats_decomposed() (lines 165-259).
    Returns (stats, decomp_info) where decomp_info is None if no shooting
    profile, otherwise a dict with intermediate values.
    """
    # Base stats for all non-scoring categories
    stats = project_stats(minutes, player, pace_factor, dvp_factors, usage_boost)

    if not getattr(player, "has_shooting_profile", False):
        return stats, None

    # Decompose PTS and FG3M from shooting profile
    pts_sens = PACE_SENSITIVITY.get("pts", 0.5)
    pace_adj = 1.0 + (pace_factor - 1.0) * pts_sens

    dvp_pts = dvp_factors.get("pts", 1.0) if dvp_factors else 1.0

    stat_weight = USAGE_BOOST_STAT_WEIGHTS.get("pts", 0.5)
    effective_boost = 1.0 + (usage_boost - 1.0) * stat_weight

    # Shooting rates with league-average fallbacks
    fg3_pct = player.fg3_pct if player.fg3_pct is not None else SHOT_DECOMP_LEAGUE_AVG_FG3_PCT
    fg2_pct = player.fg2_pct if player.fg2_pct is not None else SHOT_DECOMP_LEAGUE_AVG_FG2_PCT
    ft_pct = player.ft_pct if player.ft_pct is not None else SHOT_DECOMP_LEAGUE_AVG_FT_PCT

    fg3a_rate = player.fg3a_rate or 0.0
    fga_rate = player.fga_rate or 0.0
    fta_rate = player.fta_rate or 0.0
    fg2a_rate = max(0.0, fga_rate - fg3a_rate)

    # Project made shots
    base_mult = minutes * effective_boost * pace_adj * dvp_pts
    fg3m = base_mult * fg3a_rate * fg3_pct
    fg2m = base_mult * fg2a_rate * fg2_pct
    ftm = base_mult * fta_rate * ft_pct

    decomposed_pts = fg3m * 3.0 + fg2m * 2.0 + ftm * 1.0

    # Replace PTS and FG3M with decomposed values
    stats["pts"] = round(decomposed_pts, 1)
    stats["fg3m"] = round(fg3m, 1)

    decomp_info = {
        "fg3a_rate": fg3a_rate, "fg2a_rate": fg2a_rate, "fta_rate": fta_rate,
        "fg3_pct": fg3_pct, "fg2_pct": fg2_pct, "ft_pct": ft_pct,
        "base_mult": base_mult,
        "fg3m": fg3m, "fg2m": fg2m, "ftm": ftm,
        "decomposed_pts": decomposed_pts,
    }
    return stats, decomp_info


def dk_score_base(stats: dict) -> float:
    """DK fantasy points from stat line (no DD/TD bonus)."""
    return sum(stats.get(s, 0.0) * w for s, w in DK_WEIGHTS.items())


def dk_score_full(stats: dict) -> Tuple[float, float, float, float]:
    """DK fantasy points with DD/TD. Returns (total, base, dd_ev, td_ev)."""
    base = dk_score_base(stats)
    p_dd, p_td = compute_dd_td_probs(stats)
    dd_ev = p_dd * DK_DD_BONUS
    td_ev = p_td * DK_TD_BONUS
    return round(base + dd_ev + td_ev, 1), round(base, 1), dd_ev, td_ev


# ── Synthetic Data ────────────────────────────────────────────────────

_SYNTHETIC_PLAYERS: Dict[str, Dict[str, dict]] = {
    "DEN": {
        "Nikola Jokic": {
            "player_id": 203999, "position": "C", "team_id": 1610612743,
            "season_avg": 34.6, "usage_rate": 0.310, "age": 30,
            "minutes_last_5": [35, 36, 34, 33, 35],
            "minutes_last_10": [35, 36, 34, 33, 35, 34, 32, 36, 33, 35],
            "pts_per_min": 0.75, "reb_per_min": 0.37, "ast_per_min": 0.28,
            "stl_per_min": 0.04, "blk_per_min": 0.03,
            "tov_per_min": 0.10, "fg3m_per_min": 0.04,
            "fg3a_rate": 0.10, "fga_rate": 0.55, "fta_rate": 0.18,
            "fg3_pct": 0.333, "fg2_pct": 0.583, "ft_pct": 0.822,
        },
        "Jamal Murray": {
            "player_id": 1627750, "position": "PG", "team_id": 1610612743,
            "season_avg": 33.2, "usage_rate": 0.250, "age": 28,
            "minutes_last_5": [32, 34, 33, 31, 34],
            "minutes_last_10": [32, 34, 33, 31, 34, 33, 35, 32, 34, 33],
            "pts_per_min": 0.65, "reb_per_min": 0.12, "ast_per_min": 0.18,
            "stl_per_min": 0.03, "blk_per_min": 0.01,
            "tov_per_min": 0.08, "fg3m_per_min": 0.09,
            "fg3a_rate": 0.22, "fga_rate": 0.52, "fta_rate": 0.12,
            "fg3_pct": 0.410, "fg2_pct": 0.490, "ft_pct": 0.860,
        },
        "Michael Porter Jr.": {
            "player_id": 1629008, "position": "SF", "team_id": 1610612743,
            "season_avg": 32.8, "usage_rate": 0.210, "age": 27,
            "minutes_last_5": [33, 32, 34, 31, 33],
            "minutes_last_10": [33, 32, 34, 31, 33, 32, 34, 31, 33, 32],
            "pts_per_min": 0.58, "reb_per_min": 0.24, "ast_per_min": 0.06,
            "stl_per_min": 0.03, "blk_per_min": 0.02,
            "tov_per_min": 0.05, "fg3m_per_min": 0.08,
            "fg3a_rate": 0.18, "fga_rate": 0.46, "fta_rate": 0.08,
            "fg3_pct": 0.420, "fg2_pct": 0.540, "ft_pct": 0.830,
        },
        "Aaron Gordon": {
            "player_id": 203932, "position": "PF", "team_id": 1610612743,
            "season_avg": 31.0, "usage_rate": 0.180, "age": 30,
            "minutes_last_5": [31, 30, 32, 31, 30],
            "minutes_last_10": [31, 30, 32, 31, 30, 31, 30, 32, 31, 30],
            "pts_per_min": 0.45, "reb_per_min": 0.19, "ast_per_min": 0.10,
            "stl_per_min": 0.02, "blk_per_min": 0.02,
            "tov_per_min": 0.05, "fg3m_per_min": 0.04,
        },
        "Christian Braun": {
            "player_id": 1631128, "position": "SG", "team_id": 1610612743,
            "season_avg": 28.5, "usage_rate": 0.170, "age": 23,
            "minutes_last_5": [28, 29, 27, 30, 28],
            "minutes_last_10": [28, 29, 27, 30, 28, 29, 27, 30, 28, 29],
            "pts_per_min": 0.42, "reb_per_min": 0.15, "ast_per_min": 0.08,
            "stl_per_min": 0.03, "blk_per_min": 0.01,
            "tov_per_min": 0.04, "fg3m_per_min": 0.06,
        },
    },
}

# Team metadata: (team_id, full_name)
_SYNTHETIC_TEAM_META: Dict[str, Tuple[int, str]] = {
    "DEN": (1610612743, "Denver Nuggets"),
}


def build_synthetic_player(
    player_name: str, team_abbr: str
) -> Tuple[PlayerMinutes, str]:
    """Build a synthetic PlayerMinutes for offline testing.

    Returns (PlayerMinutes, team_name).
    """
    team_abbr = team_abbr.upper()
    if team_abbr not in _SYNTHETIC_PLAYERS:
        print(f"  {C.RED}ERROR: No synthetic data for team '{team_abbr}'.{C.RST}")
        print(f"  {C.DIM}Available: {', '.join(sorted(_SYNTHETIC_PLAYERS))}{C.RST}")
        sys.exit(1)

    name_lower = player_name.lower().strip()
    team_data = _SYNTHETIC_PLAYERS[team_abbr]

    for pname, data in team_data.items():
        if name_lower in pname.lower():
            pm = PlayerMinutes(player_name=pname, **data)
            _, team_name = _SYNTHETIC_TEAM_META.get(
                team_abbr, (data.get("team_id", 0), f"Team {team_abbr}")
            )
            return pm, team_name

    print(f"  {C.RED}ERROR: Player '{player_name}' not in synthetic {team_abbr} data.{C.RST}")
    print(f"  {C.DIM}Available: {', '.join(team_data.keys())}{C.RST}")
    sys.exit(1)


def fetch_player_data_live(
    player_name: Optional[str],
    player_id: Optional[int],
    team_abbr: Optional[str],
) -> Tuple[Optional[PlayerMinutes], str]:
    """Fetch player data via NBAApiService (live/cached).

    Returns (PlayerMinutes, team_name) or (None, "").
    """
    from app.services.nba_api_service import NBAApiService
    nba = NBAApiService()

    if not player_name:
        return None, ""

    name_lower = player_name.lower().strip()
    from nba_api.stats.static import teams as nba_teams
    all_teams = nba_teams.get_teams()

    if team_abbr:
        all_teams = [
            t for t in all_teams
            if t["abbreviation"].upper() == team_abbr.upper()
        ] + all_teams

    checked = set()
    for team_info in all_teams:
        tid = team_info["id"]
        if tid in checked:
            continue
        checked.add(tid)
        try:
            rotation = nba.build_team_rotation(tid)
            if not rotation:
                continue
            for p in rotation:
                if name_lower in p.player_name.lower():
                    return p, team_info["full_name"]
        except Exception:
            continue

    return None, ""


# ── Main Trace Pipeline ──────────────────────────────────────────────

def run_fp_trace(
    player: PlayerMinutes,
    team_name: str,
    projected_minutes: float,
    pace: float,
    dvp_override: Optional[float],
    usage_boost: float,
    verbose: bool,
):
    """Run the full fantasy point projection trace."""

    position = player.position

    # ══════════════════════════════════════════════════════════════════
    # HEADER
    # ══════════════════════════════════════════════════════════════════
    banner(f"FANTASY POINT TRACER: {player.player_name} ({position})")
    print(f"  {C.DIM}Player ID: {player.player_id}  |  Team: {team_name}{C.RST}")
    print(f"  {C.DIM}Season Avg: {player.season_avg:.1f} min  |  "
          f"USG%: {player.usage_rate*100:.1f}%  |  "
          f"Age: {player.age or 'N/A'}{C.RST}")

    # ══════════════════════════════════════════════════════════════════
    # STEP 1: Ingest Final Minutes + Per-Minute Stat Rates
    # ══════════════════════════════════════════════════════════════════
    step("1", "Final Projected Minutes + Per-Minute Stat Rates")

    trace_val("Projected Minutes", projected_minutes, C.CYN, "min", ".1f")
    trace_info("")
    trace_info("Per-Minute Stat Rates (season):")
    for stat in STAT_ORDER:
        rate = getattr(player, RATE_ATTRS[stat], 0.0)
        label = f"  {STAT_LABELS[stat]}/min"
        trace(label, rate, C.WHT)

    # Shooting profile
    has_profile = getattr(player, "has_shooting_profile", False)
    if has_profile:
        trace_info("")
        trace_info("Shooting Profile (for shot decomposition):")
        trace(f"  FG3A/min", player.fg3a_rate or 0.0, C.WHT)
        trace(f"  FGA/min", player.fga_rate or 0.0, C.WHT)
        trace(f"  FTA/min", player.fta_rate or 0.0, C.WHT)
        trace(f"  FG3%", player.fg3_pct or 0.0, C.WHT)
        trace(f"  FG2%", player.fg2_pct or 0.0, C.WHT)
        trace(f"  FT%", player.ft_pct or 0.0, C.WHT)
    else:
        trace_info("")
        trace_info("No shooting profile — using aggregate pts_per_min rate")

    endstep("Minutes for projection", projected_minutes, "min")

    # ══════════════════════════════════════════════════════════════════
    # STEP 2: Baseline (Unadjusted) Stat Line
    # ══════════════════════════════════════════════════════════════════
    step("2", "Baseline (Unadjusted) Stat Line")

    trace_info(f"  {'Stat':<6} {'Rate/min':>9}  {'× Minutes':>10}  {'= Raw':>8}")
    trace_info(f"  {'─' * 42}")

    raw_stats = {}
    for stat in STAT_ORDER:
        rate = getattr(player, RATE_ATTRS[stat], 0.0)
        raw = round(projected_minutes * rate, 1)
        raw_stats[stat] = raw
        clr = C.WHT
        print(
            f"{C.MAG}  │{C.RST}     {C.CYN}{STAT_LABELS[stat]:<6}{C.RST} "
            f"{C.DIM}{rate:>9.3f}{C.RST}  "
            f"{C.DIM}× {projected_minutes:>7.1f}{C.RST}  "
            f"{clr}= {raw:>6.1f}{C.RST}"
        )

    raw_dk_base = dk_score_base(raw_stats)
    trace_info("")
    trace_val("Raw baseline DKFP (no modifiers)", raw_dk_base, C.WHT, "DKFP", ".1f")

    endstep("Raw Baseline DKFP", raw_dk_base, "DKFP")

    # ══════════════════════════════════════════════════════════════════
    # STEP 3: Contextual Modifiers
    # ══════════════════════════════════════════════════════════════════
    step("3", "Contextual Modifiers (Usage, Pace, DvP)")

    # Compute pace factor
    pace_factor = pace / LEAGUE_AVG_PACE if LEAGUE_AVG_PACE > 0 else 1.0

    trace_val("Usage Boost (raw)", usage_boost, C.CYN if usage_boost > 1.0 else C.WHT, "×", ".3f")
    trace_val("Game Pace", pace, C.WHT, "poss/48", ".1f")
    trace_val(f"Pace Factor (÷ league avg {LEAGUE_AVG_PACE})", pace_factor, C.CYN if abs(pace_factor - 1.0) > 0.005 else C.WHT, "×", ".4f")

    # Build position DvP
    if dvp_override is not None:
        dvp_factors = build_position_dvp(dvp_override, position)
        trace_val(f"DvP Override (uniform)", dvp_override, C.CYN, "×", ".3f")
        trace_info(f"Position-adjusted DvP ({position}):")
        for stat in STAT_ORDER:
            d = dvp_factors[stat]
            clr = C.GRN if d > 1.005 else C.RED if d < 0.995 else C.DIM
            trace_info(f"  {STAT_LABELS[stat]:>5}: {clr}{d:.3f}{C.RST}")
    else:
        dvp_factors = {s: 1.0 for s in STAT_ORDER}
        trace_info("DvP: neutral (1.000) — use --dvp to simulate opponent matchup")

    # Compute per-stat modifiers
    modifiers: Dict[str, dict] = {}
    for stat in STAT_ORDER:
        stat_weight = USAGE_BOOST_STAT_WEIGHTS.get(stat, 0.5)
        eff_boost = 1.0 + (usage_boost - 1.0) * stat_weight
        p_sens = PACE_SENSITIVITY.get(stat, 0.5)
        p_adj = 1.0 + (pace_factor - 1.0) * p_sens
        dvp = dvp_factors.get(stat, 1.0)
        combined = eff_boost * p_adj * dvp
        modifiers[stat] = {
            "stat_wt": stat_weight,
            "eff_boost": eff_boost,
            "pace_sens": p_sens,
            "pace_adj": p_adj,
            "dvp": dvp,
            "combined": combined,
        }

    if verbose:
        trace_info("")
        trace_info("Per-Stat Modifier Breakdown:")
        # Header
        hdr = (f"  {'Stat':<6} {'UsageWt':>8} {'EffBoost':>9} "
               f"{'PaceSns':>8} {'PaceAdj':>8} {'DvP':>8} {'Combined':>9}")
        trace_info(hdr)
        trace_info(f"  {'─' * 62}")
        for stat in STAT_ORDER:
            m = modifiers[stat]
            comb_clr = C.GRN if m["combined"] > 1.005 else C.RED if m["combined"] < 0.995 else C.DIM
            print(
                f"{C.MAG}  │{C.RST}     {C.CYN}{STAT_LABELS[stat]:<6}{C.RST} "
                f"{C.DIM}{m['stat_wt']:>8.2f}{C.RST} "
                f"{C.DIM}{m['eff_boost']:>9.3f}{C.RST} "
                f"{C.DIM}{m['pace_sens']:>8.2f}{C.RST} "
                f"{C.DIM}{m['pace_adj']:>8.4f}{C.RST} "
                f"{C.DIM}{m['dvp']:>8.3f}{C.RST} "
                f"{comb_clr}{m['combined']:>9.4f}{C.RST}"
            )
    else:
        trace_info("")
        trace_info("Combined modifiers per stat:")
        for stat in STAT_ORDER:
            m = modifiers[stat]
            clr = C.GRN if m["combined"] > 1.005 else C.RED if m["combined"] < 0.995 else C.DIM
            trace_info(f"  {STAT_LABELS[stat]:>5}: {clr}{m['combined']:.4f}{C.RST}")
        trace_info(f"  {C.DIM}(use --verbose for full breakdown){C.RST}")

    trace_info("")
    trace_info("CalibrationService not applied (standalone mode)")
    trace_info("Props blending not applied (standalone mode)")

    endstep("Per-stat modifiers computed", 0.0, "")

    # ══════════════════════════════════════════════════════════════════
    # STEP 4: Adjusted Stat Line
    # ══════════════════════════════════════════════════════════════════
    step("4", "Adjusted Stat Line")

    # Compute adjusted stats using production formula
    adj_stats, decomp_info = project_stats_decomposed(
        projected_minutes, player, pace_factor, dvp_factors, usage_boost,
    )

    trace_info(f"  {'Stat':<6} {'Raw':>8}  {'× Modifier':>11}  {'= Adjusted':>11}  {'Delta':>8}")
    trace_info(f"  {'─' * 52}")

    for stat in STAT_ORDER:
        raw_v = raw_stats[stat]
        adj_v = adj_stats[stat]
        m = modifiers[stat]["combined"]
        delta = adj_v - raw_v
        d_clr = C.GRN if delta > 0.05 else C.RED if delta < -0.05 else C.DIM
        d_sign = "+" if delta > 0 else ""
        print(
            f"{C.MAG}  │{C.RST}     {C.CYN}{STAT_LABELS[stat]:<6}{C.RST} "
            f"{C.DIM}{raw_v:>8.1f}{C.RST}  "
            f"{C.DIM}× {m:>8.4f}{C.RST}  "
            f"{C.WHT}= {adj_v:>8.1f}{C.RST}  "
            f"{d_clr}{d_sign}{delta:>7.1f}{C.RST}"
        )

    # Shot decomposition trace
    if decomp_info:
        trace_info("")
        trace_info("Shot Decomposition (shooting profile active):")
        bm = decomp_info["base_mult"]
        trace_info(f"  base_mult = {projected_minutes:.1f} min × "
                   f"{modifiers['pts']['eff_boost']:.3f} boost × "
                   f"{modifiers['pts']['pace_adj']:.4f} pace × "
                   f"{modifiers['pts']['dvp']:.3f} dvp = {bm:.2f}")
        trace_info(f"  FG3M = {bm:.2f} × {decomp_info['fg3a_rate']:.3f} rate × "
                   f"{decomp_info['fg3_pct']:.3f} pct = {decomp_info['fg3m']:.2f}")
        trace_info(f"  FG2M = {bm:.2f} × {decomp_info['fg2a_rate']:.3f} rate × "
                   f"{decomp_info['fg2_pct']:.3f} pct = {decomp_info['fg2m']:.2f}")
        trace_info(f"  FTM  = {bm:.2f} × {decomp_info['fta_rate']:.3f} rate × "
                   f"{decomp_info['ft_pct']:.3f} pct = {decomp_info['ftm']:.2f}")
        trace_info(f"  Decomposed PTS = {decomp_info['fg3m']:.2f}×3 + "
                   f"{decomp_info['fg2m']:.2f}×2 + {decomp_info['ftm']:.2f}×1 = "
                   f"{decomp_info['decomposed_pts']:.1f}")

    adj_dk_base = dk_score_base(adj_stats)
    trace_info("")
    trace_val("Adjusted baseline DKFP", adj_dk_base, C.WHT, "DKFP", ".1f")

    endstep("Adjusted stat line computed", adj_dk_base, "DKFP")

    # ══════════════════════════════════════════════════════════════════
    # STEP 5: DraftKings Scoring Engine
    # ══════════════════════════════════════════════════════════════════
    step("5", "DraftKings Scoring Engine")

    trace_info(f"  {'Stat':<6} {'Projected':>10}  {'× Weight':>9}  {'= DKFP':>8}")
    trace_info(f"  {'─' * 40}")

    total_base = 0.0
    for stat in STAT_ORDER:
        val = adj_stats[stat]
        wt = DK_WEIGHTS[stat]
        fp_contrib = val * wt
        total_base += fp_contrib
        wt_str = f"{wt:+.2f}" if wt < 0 else f" {wt:.2f}"
        clr = C.RED if fp_contrib < 0 else C.WHT
        print(
            f"{C.MAG}  │{C.RST}     {C.CYN}{STAT_LABELS[stat]:<6}{C.RST} "
            f"{C.WHT}{val:>10.1f}{C.RST}  "
            f"{C.DIM}× {wt_str}{C.RST}  "
            f"{clr}= {fp_contrib:>6.1f}{C.RST}"
        )

    trace_info(f"  {'─' * 40}")
    trace_val("Base DKFP (before bonuses)", total_base, C.BLD + C.WHT, "DKFP", ".1f")

    # DD/TD Monte Carlo
    trace_info("")
    trace_info("DD/TD Probability (Monte Carlo, N=1000, seed=42):")
    p_dd, p_td = compute_dd_td_probs(adj_stats)
    dd_ev = p_dd * DK_DD_BONUS
    td_ev = p_td * DK_TD_BONUS

    # Show per-stat P(>=10) marginals
    trace_info("  Per-stat P(>=10):")
    p10_parts = []
    for stat in _DD_STATS:
        p = prob_over_10(adj_stats.get(stat, 0.0), stat)
        p_str = f"{p*100:.1f}%"
        clr = C.GRN if p > 0.5 else C.YEL if p > 0.1 else C.DIM
        p10_parts.append(f"{STAT_LABELS[stat]}: {clr}{p_str}{C.RST}")
    trace_info("    " + "  ".join(p10_parts))

    if verbose:
        trace_info("")
        trace_info("  Correlation matrix (5×5): pts, reb, ast, stl, blk")
        for i, row in enumerate(_DD_CORR):
            row_str = "  ".join(f"{v:>5.2f}" for v in row)
            trace_info(f"    {_DD_STATS[i]:>3}: [{row_str}]")

        means = [adj_stats.get(s, 0.0) for s in _DD_STATS]
        cvs = [DD_STAT_CV.get(s, DD_STAT_VARIANCE) for s in _DD_STATS]
        sigmas = [m * c for m, c in zip(means, cvs)]
        trace_info(f"  Means:  [{', '.join(f'{m:.1f}' for m in means)}]")
        trace_info(f"  CVs:    [{', '.join(f'{c:.2f}' for c in cvs)}]")
        trace_info(f"  Sigmas: [{', '.join(f'{s:.1f}' for s in sigmas)}]")

    trace_info("")
    dd_clr = C.GRN if p_dd > 0.3 else C.YEL if p_dd > 0.1 else C.DIM
    td_clr = C.GRN if p_td > 0.1 else C.YEL if p_td > 0.02 else C.DIM
    trace_info(f"  P(DD) = {dd_clr}{p_dd*100:.1f}%{C.RST}    "
               f"EV = {p_dd:.3f} × {DK_DD_BONUS} = {dd_clr}+{dd_ev:.2f} DKFP{C.RST}")
    trace_info(f"  P(TD) = {td_clr}{p_td*100:.1f}%{C.RST}    "
               f"EV = {p_td:.3f} × {DK_TD_BONUS} = {td_clr}+{td_ev:.2f} DKFP{C.RST}")
    trace_info(f"  Combined DD/TD bonus = {C.GRN}+{dd_ev + td_ev:.2f} DKFP{C.RST}")

    final_dk = round(total_base + dd_ev + td_ev, 1)
    endstep("Total DKFP", final_dk, "DKFP")

    # ══════════════════════════════════════════════════════════════════
    # STEP 6: Floor / Ceiling Range
    # ══════════════════════════════════════════════════════════════════
    step("6", "Floor / Ceiling Range")

    # Player CV
    cv = getattr(player, "minutes_cv", PLAYER_CV_FALLBACK)
    cv_delta = cv - PLAYER_CV_BASELINE
    trace_val("Player CV (minutes volatility)", cv, C.WHT, "", ".3f")
    trace_val(f"CV delta from baseline ({PLAYER_CV_BASELINE:.2f})", cv_delta, C.WHT, "", ".3f")

    # Floor rate
    floor_rate = FLOOR_RATE_MULT
    ceiling_rate = CEILING_RATE_MULT
    if dvp_override is not None:
        dvp_mean = sum(dvp_factors.values()) / len(dvp_factors)
        floor_rate = max(0.50, FLOOR_RATE_MULT + (dvp_mean - 1.0) * DVP_FLOOR_SENSITIVITY)
        ceiling_rate = min(1.60, CEILING_RATE_MULT + (dvp_mean - 1.0) * DVP_CEILING_SENSITIVITY)

    # CV adjustment
    if abs(cv_delta) > 0.01:
        floor_rate = max(0.45, floor_rate * (1.0 - cv_delta * PLAYER_CV_FLOOR_SCALE))
        ceiling_rate = min(1.70, ceiling_rate * (1.0 + cv_delta * PLAYER_CV_CEILING_SCALE))

    floor_minutes = projected_minutes * FLOOR_MINUTES_MULT
    ceiling_minutes = projected_minutes * CEILING_MINUTES_MULT

    trace_info("")
    trace_info("Floor Parameters:")
    trace_val(f"  Minutes: {projected_minutes:.1f} × {FLOOR_MINUTES_MULT}", floor_minutes, C.WHT, "min", ".1f")
    trace_val(f"  Rate multiplier (base)", FLOOR_RATE_MULT, C.WHT, "", ".3f")
    if dvp_override is not None:
        dvp_mean = sum(dvp_factors.values()) / len(dvp_factors)
        trace_val(f"  DvP-adjusted", max(0.50, FLOOR_RATE_MULT + (dvp_mean - 1.0) * DVP_FLOOR_SENSITIVITY), C.WHT, "", ".3f")
    trace_val(f"  CV-adjusted floor rate", floor_rate, C.WHT, "", ".3f")
    floor_rate_final = floor_rate * usage_boost
    trace_val(f"  Final (× {usage_boost:.3f} usage)", floor_rate_final, C.YEL, "", ".3f")

    trace_info("")
    trace_info("Ceiling Parameters:")
    trace_val(f"  Minutes: {projected_minutes:.1f} × {CEILING_MINUTES_MULT}", ceiling_minutes, C.WHT, "min", ".1f")
    trace_val(f"  Rate multiplier (base)", CEILING_RATE_MULT, C.WHT, "", ".3f")
    if dvp_override is not None:
        trace_val(f"  DvP-adjusted", min(1.60, CEILING_RATE_MULT + (dvp_mean - 1.0) * DVP_CEILING_SENSITIVITY), C.WHT, "", ".3f")
    trace_val(f"  CV-adjusted ceiling rate", ceiling_rate, C.WHT, "", ".3f")
    ceiling_rate_final = ceiling_rate * usage_boost
    trace_val(f"  Final (× {usage_boost:.3f} usage)", ceiling_rate_final, C.GRN, "", ".3f")

    # Project floor and ceiling stats
    floor_stats, _ = project_stats_decomposed(
        floor_minutes, player, pace_factor, dvp_factors,
        usage_boost=floor_rate_final,
    )
    ceiling_stats, _ = project_stats_decomposed(
        ceiling_minutes, player, pace_factor, dvp_factors,
        usage_boost=ceiling_rate_final,
    )

    floor_dk_total, floor_dk_base, _, _ = dk_score_full(floor_stats)
    ceiling_dk_total, ceiling_dk_base, _, _ = dk_score_full(ceiling_stats)

    trace_info("")
    trace_val("DKFP Floor", floor_dk_total, C.RED, "DKFP", ".1f")
    trace_val("DKFP Baseline", final_dk, C.WHT, "DKFP", ".1f")
    trace_val("DKFP Ceiling", ceiling_dk_total, C.GRN, "DKFP", ".1f")
    range_width = ceiling_dk_total - floor_dk_total
    pct_range = (range_width / final_dk * 100) if final_dk > 0 else 0
    trace_val("Range width", range_width, C.DIM, f"DKFP (±{pct_range/2:.0f}%)", ".1f")

    endstep(f"Floor: {floor_dk_total:.1f} | Baseline: {final_dk:.1f} | Ceiling: {ceiling_dk_total:.1f}", final_dk, "DKFP")

    # ══════════════════════════════════════════════════════════════════
    # STEP 7: Final Summary (Waterfall)
    # ══════════════════════════════════════════════════════════════════

    # Compute incremental waterfall contributions
    # 1. Raw baseline (no modifiers)
    raw_dkfp = dk_score_base(raw_stats)

    # 2. + Usage only
    usage_only_stats = project_stats(projected_minutes, player, 1.0, None, usage_boost)
    usage_dkfp = dk_score_base(usage_only_stats)
    usage_delta = usage_dkfp - raw_dkfp

    # 3. + Pace (usage + pace, no DvP)
    pace_only_stats = project_stats(projected_minutes, player, pace_factor, None, usage_boost)
    pace_dkfp = dk_score_base(pace_only_stats)
    pace_delta = pace_dkfp - usage_dkfp

    # 4. + DvP (all three modifiers, aggregate rates)
    all_mod_stats = project_stats(projected_minutes, player, pace_factor, dvp_factors, usage_boost)
    all_mod_dkfp = dk_score_base(all_mod_stats)
    dvp_delta = all_mod_dkfp - pace_dkfp

    # 5. Shot decomposition delta (if applicable)
    adj_dk_base_val = dk_score_base(adj_stats)
    decomp_delta = adj_dk_base_val - all_mod_dkfp

    # 6. DD/TD
    bonus_total = dd_ev + td_ev

    banner("FANTASY POINT PROJECTION SUMMARY")
    print(f"  {C.DIM}Player: {player.player_name} ({position}, {team_name}){C.RST}")
    print(f"  {C.DIM}Projected Minutes: {projected_minutes:.1f} min{C.RST}")
    print(f"  {C.DIM}{'─' * 54}{C.RST}")

    def _summary_line(label: str, value: float, is_delta: bool = False):
        if is_delta:
            clr = C.GRN if value > 0.05 else C.RED if value < -0.05 else C.DIM
            sign = "+" if value > 0 else ""
            print(f"  {label:<44} {clr}{sign}{value:>7.1f} DKFP{C.RST}")
        else:
            print(f"  {label:<44} {C.WHT}{value:>7.1f} DKFP{C.RST}")

    _summary_line("Raw baseline (min × rates):", raw_dkfp)
    _summary_line("Usage boost impact:", usage_delta, is_delta=True)
    _summary_line("Pace adjustment impact:", pace_delta, is_delta=True)
    _summary_line("DvP matchup impact:", dvp_delta, is_delta=True)
    if abs(decomp_delta) > 0.05:
        _summary_line("Shot decomposition delta:", decomp_delta, is_delta=True)
    _summary_line("DD/TD expected value:", bonus_total, is_delta=True)
    print(f"  {C.DIM}{'─' * 54}{C.RST}")
    print(f"  {C.BLD}{'FINAL DK PROJECTION:':<44} {C.GRN}{final_dk:>7.1f} DKFP{C.RST}")
    print(f"  {C.DIM}{'DK Floor / Ceiling:':<44} "
          f"{C.RED}{floor_dk_total:.1f}{C.RST}{C.DIM} — {C.RST}"
          f"{C.GRN}{ceiling_dk_total:.1f}{C.RST}{C.DIM} DKFP{C.RST}")
    print(f"  {C.DIM}{'─' * 54}{C.RST}")

    # Stat line summary
    stat_parts = []
    for stat in STAT_ORDER:
        stat_parts.append(f"{adj_stats[stat]:.1f} {STAT_LABELS[stat]}")
    stat_line_1 = " | ".join(stat_parts[:4])
    stat_line_2 = " | ".join(stat_parts[4:])
    print(f"  {C.DIM}Stat line: {stat_line_1}{C.RST}")
    print(f"  {C.DIM}           {stat_line_2}{C.RST}")
    print()


# ── CLI ───────────────────────────────────────────────────────────────

def main():
    # ── Force UTF-8 stdout on Windows (cp1252 can't handle box-drawing chars) ─
    if sys.platform == "win32" and hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="Trace a player's DK fantasy point projection through the DFS pipeline.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python scripts/fantasy_point_tracer.py --player "Nikola Jokic" --synthetic
  python scripts/fantasy_point_tracer.py --player "Nikola Jokic" --synthetic --minutes 34.6
  python scripts/fantasy_point_tracer.py --player "Nikola Jokic" --synthetic --pace 106.5 --dvp 1.10
  python scripts/fantasy_point_tracer.py --player "Nikola Jokic" --synthetic --usage-boost 1.12 --verbose
  python scripts/fantasy_point_tracer.py --player "Jamal Murray" --team DEN --synthetic --verbose
        """,
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--player", type=str, help="Player name (fuzzy match)")
    group.add_argument("--player-id", type=int, help="NBA API player ID")

    parser.add_argument("--team", type=str, default=None, help="Team abbreviation (e.g., DEN)")
    parser.add_argument("--minutes", type=float, default=None,
                        help="Override projected minutes (default: season_avg)")
    parser.add_argument("--spread", type=float, default=None,
                        help="Game spread (for context display only)")
    parser.add_argument("--pace", type=float, default=None,
                        help=f"Game pace in poss/48 (default: {LEAGUE_AVG_PACE})")
    parser.add_argument("--dvp", type=float, default=None,
                        help="Uniform DvP multiplier (e.g., 1.10 = weak defense)")
    parser.add_argument("--usage-boost", type=float, default=None,
                        help="Usage rate boost (e.g., 1.12 = injured teammate)")
    parser.add_argument("--win-pct", type=float, default=None,
                        help="Team win pct (context display only)")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Show detailed per-stat modifier breakdown tables")
    parser.add_argument("--synthetic", action="store_true",
                        help="Use synthetic mock data (no API/DB needed)")

    args = parser.parse_args()

    banner("FANTASY POINT TRACER — Loading Data...")

    # ── Resolve player data ──────────────────────────────────────────
    player: Optional[PlayerMinutes] = None
    team_name = ""

    if args.synthetic:
        print(f"  {C.YEL}Using SYNTHETIC data (no API calls){C.RST}")
        player, team_name = build_synthetic_player(
            args.player or "Nikola Jokic",
            args.team or "DEN",
        )
        print(f"  {C.GRN}Loaded: {player.player_name} ({player.position}){C.RST}")
    else:
        print(f"  {C.DIM}Looking up player...{C.RST}")
        player, team_name = fetch_player_data_live(
            args.player, args.player_id, args.team,
        )
        if player is None:
            print(f"\n  {C.RED}ERROR: Player not found.{C.RST}")
            if args.player:
                print(f"  {C.DIM}Searched for: '{args.player}'{C.RST}")
            print(f"  {C.DIM}Hint: use --synthetic to test with mock data (no API needed).{C.RST}")
            sys.exit(1)
        print(f"  {C.GRN}Found: {player.player_name} on {team_name}{C.RST}")

    # ── Resolve parameters ───────────────────────────────────────────
    projected_minutes = args.minutes if args.minutes is not None else player.season_avg
    pace = args.pace if args.pace is not None else LEAGUE_AVG_PACE
    usage_boost = args.usage_boost if args.usage_boost is not None else 1.0
    dvp_override = args.dvp  # None = neutral

    # Check per-min rates are populated
    has_rates = any(
        getattr(player, RATE_ATTRS[s], 0.0) > 0 for s in STAT_ORDER
    )
    if not has_rates:
        print(f"\n  {C.RED}ERROR: Player has no per-minute stat rates.{C.RST}")
        print(f"  {C.DIM}The synthetic data or API returned zero rates for all stats.{C.RST}")
        sys.exit(1)

    if args.minutes is None:
        print(f"  {C.DIM}Using season_avg ({player.season_avg:.1f}) as projected minutes.{C.RST}")
        print(f"  {C.DIM}Hint: use --minutes to override (e.g., from minute_tracer output).{C.RST}")

    # ── Run the trace ────────────────────────────────────────────────
    run_fp_trace(
        player=player,
        team_name=team_name,
        projected_minutes=projected_minutes,
        pace=pace,
        dvp_override=dvp_override,
        usage_boost=usage_boost,
        verbose=args.verbose,
    )


if __name__ == "__main__":
    main()
