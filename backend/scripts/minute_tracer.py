#!/usr/bin/env python3
"""Minute Tracer — Step-by-step diagnostic of a single player's minute projection.

Traces the FULL lifecycle of a player's minute projection through every
stage of the RotationEngine pipeline, printing a color-coded console log
showing exactly where minutes are added, removed, or redistributed.

Usage
-----
  python scripts/minute_tracer.py --player "Nikola Jokic"
  python scripts/minute_tracer.py --player-id 203999
  python scripts/minute_tracer.py --player "Nikola Jokic" --spread -12.0
  python scripts/minute_tracer.py --player "Nikola Jokic" --team DEN --game-date 2026-02-27
  python scripts/minute_tracer.py --player "Nikola Jokic" --verbose

The script fetches the player's actual game-log data and simulates the
rotation engine pipeline, intercepting each stage to show the math.

If --spread is provided, it overrides the game's Vegas/projected spread
for "what if" analysis (e.g., --spread -14.0 for a big home favorite).
"""

import argparse
import io
import math
import os
import random
import sys
import time
from datetime import date as _date
from typing import Dict, List, Optional, Tuple

# ── Bootstrap — add backend/ to sys.path ──────────────────────────────
_BACKEND = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, _BACKEND)

from app.config.constants import (
    EMA_ALPHA_5_GAME,
    EMA_ALPHA_10_GAME,
    BASELINE_SEASON_WEIGHT,
    BASELINE_RECENT_WEIGHT,
    RECENT_EMA5_SPLIT,
    RECENT_EMA10_SPLIT,
    BLOWOUT_SPREAD_THRESHOLD,
    BLOWOUT_PENALTY_PER_POINT,
    BLOWOUT_PENALTY_EXPONENT,
    BLOWOUT_MIN_FACTOR,
    STAR_BLOWOUT_DAMPENING,
    STARTER_THRESHOLD_MINUTES,
    STAR_ANCHOR_THRESHOLD,
    TOTAL_TEAM_MINUTES,
    MIN_VIABLE_MINUTES,
    MAX_INFLATION_CEILING,
    ABSOLUTE_MAX_MINUTES,
    B2B_STARTER_REDUCTION_MINUTES,
    B2B_VETERAN_EXTRA_MINUTES,
    VETERAN_AGE,
    REST_BOOST_PER_EXTRA_DAY,
    REST_PENALTY_B2B,
    REST_FACTOR_MIN,
    REST_FACTOR_MAX,
    FATIGUE_THRESHOLD_GAMES,
    FATIGUE_PENALTY_PER_GAME,
    FATIGUE_MAX_PENALTY,
    GARBAGE_TIME_RATE_DISCOUNT,
    GARBAGE_TIME_SPREAD_THRESHOLD,
    COMPETITIVE_TANKING_WP,
    COMPETITIVE_CLINCHED_WP,
    COMPETITIVE_TANKING_STAR_MULT,
    COMPETITIVE_CLINCHED_STAR_MULT,
    COMPETITIVE_PUSH_STAR_MULT,
    DEEP_BENCH_THRESHOLD_MINUTES,
    INJURY_RETURN_DECAY_GAMES,
    INJURY_RETURN_MINUTES_REDUCTION,
    NORM_LOCK_THRESHOLD,
    NORM_MID_THRESHOLD,
    NORM_MID_MAX_CUT_PCT,
    SHORT_ROTATION_SIZE,
    SHORT_ROTATION_STARTER_CEILING,
)
from app.models.coach import get_coach_profile, CoachProfile
from app.models.player import PlayerMinutes, PlayerProjection


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

def trace(label: str, value, color=C.WHT, unit="min"):
    print(f"{C.MAG}  │{C.RST}   {C.DIM}{label:<40}{C.RST} {color}{value:>8} {unit}{C.RST}")

def trace_delta(label: str, before: float, after: float, unit="min"):
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

def trace_factor(label: str, factor: float, minutes_before: float):
    after = round(minutes_before * factor, 1)
    delta = after - minutes_before
    clr = C.GRN if delta > 0 else C.RED if delta < 0 else C.DIM
    sign = "+" if delta > 0 else ""
    print(
        f"{C.MAG}  │{C.RST}   {label:<40} "
        f"×{factor:.4f}  →  "
        f"{clr}{after:>7.1f}{C.RST}  "
        f"{clr}({sign}{delta:.1f} min){C.RST}"
    )

def trace_info(msg: str):
    print(f"{C.MAG}  │{C.RST}   {C.DIM}{msg}{C.RST}")

def trace_warn(msg: str):
    print(f"{C.MAG}  │{C.RST}   {C.YEL}⚠ {msg}{C.RST}")

def endstep(label: str, value: float):
    print(f"{C.MAG}  │{C.RST}")
    print(f"{C.MAG}  └─▶{C.RST} {C.BLD}{label}: {C.GRN}{value:.1f} min{C.RST}")


def calculate_ema(minutes: List[float], alpha: float) -> float:
    """Same EMA implementation as RotationEngine.calculate_ema()."""
    if not minutes:
        return 0.0
    chronological = list(reversed(minutes))
    ema = chronological[0]
    for m in chronological[1:]:
        ema = alpha * m + (1 - alpha) * ema
    return ema


# ── Data fetching ─────────────────────────────────────────────────────

def fetch_player_data(
    player_name: Optional[str],
    player_id: Optional[int],
    team_abbr: Optional[str],
) -> Tuple[Optional[PlayerMinutes], int, str]:
    """Fetch the target player's rotation data via NBAApiService.

    Returns (PlayerMinutes, team_id, team_name) or (None, 0, "").
    Uses build_team_rotation() which hits the rotation cache, then
    live API if needed.
    """
    from app.services.nba_api_service import NBAApiService

    nba = NBAApiService()

    if player_id:
        # Search by player ID — need to find the team first
        from nba_api.stats.static import teams as nba_teams
        all_teams = nba_teams.get_teams()
        for team_info in all_teams:
            tid = team_info["id"]
            if team_abbr and team_info["abbreviation"].upper() != team_abbr.upper():
                continue
            try:
                rotation = nba.build_team_rotation(tid)
                if rotation:
                    for p in rotation:
                        if p.player_id == player_id:
                            return p, tid, team_info["full_name"]
            except Exception:
                continue
        return None, 0, ""

    if not player_name:
        return None, 0, ""

    # Search by name — fuzzy match across teams
    name_lower = player_name.lower().strip()
    from nba_api.stats.static import teams as nba_teams
    all_teams = nba_teams.get_teams()

    # If team_abbr is specified, filter to that team first
    if team_abbr:
        all_teams = [
            t for t in all_teams
            if t["abbreviation"].upper() == team_abbr.upper()
        ] + all_teams  # Try specified team first, then fall back

    checked_teams = set()
    for team_info in all_teams:
        tid = team_info["id"]
        if tid in checked_teams:
            continue
        checked_teams.add(tid)
        try:
            rotation = nba.build_team_rotation(tid)
            if not rotation:
                continue
            for p in rotation:
                if name_lower in p.player_name.lower():
                    return p, tid, team_info["full_name"]
        except Exception as e:
            print(f"  {C.DIM}  ... {team_info['abbreviation']} failed: {e}{C.RST}")
            continue

    return None, 0, ""


def fetch_team_rotation(team_id: int) -> List[PlayerMinutes]:
    """Fetch full team rotation for normalization simulation."""
    from app.services.nba_api_service import NBAApiService
    nba = NBAApiService()
    try:
        return nba.build_team_rotation(team_id) or []
    except Exception:
        return []


# ── Synthetic (mock) data for offline testing ────────────────────────

# Realistic NBA team templates: (name, pos, season_avg, usage_rate, age, last5, last10)
_SYNTHETIC_TEAMS: Dict[str, list] = {
    "DEN": [
        ("Nikola Jokic",       "C",  34.6, 0.310, 30,
         [35, 36, 34, 33, 35], [35, 36, 34, 33, 35, 34, 32, 36, 33, 35]),
        ("Jamal Murray",       "PG", 33.2, 0.250, 28,
         [32, 34, 33, 31, 34], [32, 34, 33, 31, 34, 33, 35, 32, 34, 33]),
        ("Michael Porter Jr.", "SF", 32.8, 0.210, 27,
         [33, 32, 34, 31, 33], [33, 32, 34, 31, 33, 32, 34, 31, 33, 32]),
        ("Aaron Gordon",       "PF", 31.0, 0.180, 30,
         [31, 30, 32, 31, 30], [31, 30, 32, 31, 30, 31, 30, 32, 31, 30]),
        ("Christian Braun",    "SG", 28.5, 0.170, 23,
         [28, 29, 27, 30, 28], [28, 29, 27, 30, 28, 29, 27, 30, 28, 29]),
        ("Russell Westbrook",  "PG", 18.0, 0.220, 37,
         [18, 17, 19, 18, 17], [18, 17, 19, 18, 17, 18, 17, 19, 18, 17]),
        ("Julian Strawther",   "SG", 16.5, 0.140, 23,
         [17, 16, 15, 18, 16], [17, 16, 15, 18, 16, 17, 16, 15, 18, 16]),
        ("Peyton Watson",      "SF", 14.0, 0.120, 22,
         [14, 13, 15, 14, 13], [14, 13, 15, 14, 13, 14, 13, 15, 14, 13]),
        ("DeAndre Jordan",     "C",  10.5, 0.080, 37,
         [10, 11, 10, 11, 10], [10, 11, 10, 11, 10, 10, 11, 10, 11, 10]),
        ("Dario Saric",        "PF",  9.0, 0.100, 31,
         [9, 10, 8, 9, 10],   [9, 10, 8, 9, 10, 9, 10, 8, 9, 10]),
        ("Hunter Tyson",       "SF",  6.0, 0.070, 25,
         [6, 5, 7, 6, 5],     [6, 5, 7, 6, 5, 6, 5, 7, 6, 5]),
        ("Jalen Pickett",      "PG",  4.0, 0.060, 25,
         [4, 3, 5, 4, 3],     [4, 3, 5, 4, 3, 4, 3, 5, 4, 3]),
    ],
}

# Team metadata: (team_id, full_name)
_SYNTHETIC_TEAM_META: Dict[str, Tuple[int, str]] = {
    "DEN": (1610612743, "Denver Nuggets"),
    "LAL": (1610612747, "Los Angeles Lakers"),
    "MIL": (1610612749, "Milwaukee Bucks"),
}


def build_synthetic_rotation(
    player_name: str, team_abbr: str
) -> Tuple[PlayerMinutes, int, str, List[PlayerMinutes]]:
    """Build a synthetic rotation for offline pipeline testing.

    Returns (target, team_id, team_name, rotation).
    Raises SystemExit if the player/team isn't in the mock data.
    """
    team_abbr = team_abbr.upper()
    if team_abbr not in _SYNTHETIC_TEAMS:
        print(f"  {C.RED}ERROR: No synthetic data for team '{team_abbr}'.{C.RST}")
        print(f"  {C.DIM}Available: {', '.join(sorted(_SYNTHETIC_TEAMS))}{C.RST}")
        sys.exit(1)

    team_id, team_name = _SYNTHETIC_TEAM_META.get(
        team_abbr, (1610612743, f"Team {team_abbr}")
    )

    rotation: List[PlayerMinutes] = []
    target: Optional[PlayerMinutes] = None
    name_lower = player_name.lower().strip()

    for idx, (pname, pos, savg, usg, age, last5, last10) in enumerate(
        _SYNTHETIC_TEAMS[team_abbr]
    ):
        pm = PlayerMinutes(
            player_id=200000 + idx,
            player_name=pname,
            position=pos,
            team_id=team_id,
            minutes_last_5=last5,
            minutes_last_10=last10,
            season_avg=savg,
            usage_rate=usg,
            age=age,
        )
        rotation.append(pm)
        if name_lower in pname.lower():
            target = pm

    if target is None:
        print(f"  {C.RED}ERROR: Player '{player_name}' not in synthetic {team_abbr} data.{C.RST}")
        print(f"  {C.DIM}Available: {', '.join(p[0] for p in _SYNTHETIC_TEAMS[team_abbr])}{C.RST}")
        sys.exit(1)

    return target, team_id, team_name, rotation


# ── Pipeline simulation ──────────────────────────────────────────────

def run_trace(
    target: PlayerMinutes,
    team_id: int,
    team_name: str,
    rotation: List[PlayerMinutes],
    spread_override: Optional[float],
    is_b2b: bool,
    rest_days: Optional[int],
    win_pct: Optional[float],
    verbose: bool,
):
    """Run the full minute-projection trace for the target player."""

    pid = target.player_id

    # ── Summary header ────────────────────────────────────────────────
    banner(f"MINUTE TRACER: {target.player_name} ({target.position})")
    print(f"  {C.DIM}Player ID: {pid}  |  Team: {team_name} ({team_id}){C.RST}")
    print(f"  {C.DIM}Season Avg: {target.season_avg:.1f} min  |  "
          f"USG%: {target.usage_rate*100:.1f}%  |  "
          f"Age: {target.age or 'N/A'}{C.RST}")

    if target.minutes_last_5:
        last5_str = ", ".join(f"{m:.0f}" for m in target.minutes_last_5)
        print(f"  {C.DIM}Last 5 games: [{last5_str}]{C.RST}")
    if target.minutes_last_10:
        last10_str = ", ".join(f"{m:.0f}" for m in target.minutes_last_10)
        print(f"  {C.DIM}Last 10 games: [{last10_str}]{C.RST}")

    # ══════════════════════════════════════════════════════════════════
    # STEP 1: Baseline Projection (EMA blend)
    # ══════════════════════════════════════════════════════════════════
    step("1", "Baseline Projection (Weighted EMA Blend)")

    ema_5 = calculate_ema(target.minutes_last_5, alpha=EMA_ALPHA_5_GAME)
    ema_10 = calculate_ema(target.minutes_last_10, alpha=EMA_ALPHA_10_GAME)
    season_avg = target.season_avg

    season_w = BASELINE_SEASON_WEIGHT
    recent_w = BASELINE_RECENT_WEIGHT

    # Renormalize
    total_w = season_w + recent_w
    season_w /= total_w
    recent_w /= total_w
    ema_5_w = recent_w * RECENT_EMA5_SPLIT
    ema_10_w = recent_w * RECENT_EMA10_SPLIT

    baseline = (ema_5_w * ema_5) + (ema_10_w * ema_10) + (season_w * season_avg)
    baseline = round(baseline, 1)

    trace("Season Avg (75% weight)", f"{season_avg:.1f}", C.WHT)
    trace(f"EMA-5  (α={EMA_ALPHA_5_GAME})", f"{ema_5:.1f}", C.WHT)
    trace(f"EMA-10 (α={EMA_ALPHA_10_GAME})", f"{ema_10:.1f}", C.WHT)
    trace_info(f"Blend: {season_w:.0%} season + {ema_5_w:.0%} EMA5 + {ema_10_w:.0%} EMA10")
    endstep("Raw Baseline", baseline)

    current_minutes = baseline

    # ══════════════════════════════════════════════════════════════════
    # STEP 1a: Injury-Return Decay Check
    # ══════════════════════════════════════════════════════════════════
    step("1a", "Injury-Return Decay Detection")

    last5 = target.minutes_last_5 or []
    games_back = 0
    for m in last5:
        if m > 0:
            games_back += 1
        else:
            break

    decay_applied = False
    if (
        baseline >= STAR_ANCHOR_THRESHOLD
        and len(last5) >= 3
        and 1 <= games_back <= INJURY_RETURN_DECAY_GAMES
        and games_back < len(last5)
    ):
        remaining = last5[games_back:]
        if any(m == 0 for m in remaining):
            old = current_minutes
            current_minutes = round(current_minutes * INJURY_RETURN_MINUTES_REDUCTION, 1)
            trace_factor(
                f"Injury return ({games_back} game(s) back)",
                INJURY_RETURN_MINUTES_REDUCTION,
                old,
            )
            decay_applied = True

    if not decay_applied:
        trace_info("No injury-return decay detected")
    endstep("Post Injury-Return", current_minutes)

    # ══════════════════════════════════════════════════════════════════
    # STEP 2: Injury Redistribution (team context)
    # ══════════════════════════════════════════════════════════════════
    step("2", "Injury Redistribution (Team Context)")
    trace_info(
        "Scanning team roster for injured players and their minutes..."
    )
    trace_info(
        f"Team rotation size: {len(rotation)} players"
    )
    # Show which players have high baselines (context)
    sorted_rot = sorted(rotation, key=lambda p: p.season_avg, reverse=True)
    if verbose:
        for i, p in enumerate(sorted_rot[:8]):
            marker = " ◄ TARGET" if p.player_id == pid else ""
            print(
                f"{C.MAG}  │{C.RST}   "
                f"{C.DIM}{i+1:>2}. {p.player_name:<24} "
                f"{p.season_avg:>5.1f} avg  ({p.position}){marker}{C.RST}"
            )
    trace_info(
        "Note: Without live injury data, this trace shows baseline minutes."
    )
    trace_info(
        "Run via the API (GET /rotation/<team_id>) to see real-time injury impact."
    )
    endstep("Post Redistribution", current_minutes)

    # ══════════════════════════════════════════════════════════════════
    # STEP 3: Game Context — Blowout Penalty
    # ══════════════════════════════════════════════════════════════════
    step("3a", "Game Context — Blowout Risk")

    # Determine spread
    abs_spread = abs(spread_override) if spread_override is not None else 0.0
    spread_val = spread_override if spread_override is not None else 0.0

    if spread_override is not None:
        trace("Spread (override)", f"{spread_val:+.1f}", C.YEL, "pts")
    else:
        trace("Spread", "N/A (no game data)", C.DIM, "")
        trace_info("Use --spread to simulate a spread (e.g., --spread -12.0)")

    pre_blowout = current_minutes
    blowout_applied = False
    effective_factor = 1.0  # Will be overwritten if blowout fires

    # Pre-compute star IDs for use across multiple steps
    sorted_team = sorted(rotation, key=lambda p: p.season_avg, reverse=True)
    star_ids = set()
    for p in sorted_team[:2]:
        if p.season_avg >= STAR_ANCHOR_THRESHOLD:
            star_ids.add(p.player_id)

    if abs_spread >= BLOWOUT_SPREAD_THRESHOLD:
        excess = abs_spread - BLOWOUT_SPREAD_THRESHOLD
        trace("Excess over threshold", f"{excess:.1f}", C.WHT, f"pts (threshold={BLOWOUT_SPREAD_THRESHOLD})")

        # Non-linear penalty (new curve)
        penalty_pct = (excess ** BLOWOUT_PENALTY_EXPONENT) * BLOWOUT_PENALTY_PER_POINT
        blowout_factor = max(BLOWOUT_MIN_FACTOR, 1.0 - penalty_pct)

        trace_info(f"Penalty curve: ({excess:.1f} ** {BLOWOUT_PENALTY_EXPONENT}) × {BLOWOUT_PENALTY_PER_POINT} = {penalty_pct:.4f}")
        trace("Base blowout factor", f"{blowout_factor:.4f}", C.WHT, "×")

        is_star = pid in star_ids

        if is_star:
            star_factor = max(
                BLOWOUT_MIN_FACTOR,
                1.0 - penalty_pct * STAR_BLOWOUT_DAMPENING,
            )
            trace_info(
                f"★ STAR-DAMPENED: penalty × {STAR_BLOWOUT_DAMPENING} → "
                f"star_factor = {star_factor:.4f}"
            )
            effective_factor = star_factor
        else:
            effective_factor = blowout_factor
            trace_info("Regular player (not star-dampened)")

        old = current_minutes
        current_minutes = round(current_minutes * effective_factor, 1)
        trace_factor("Blowout adjustment", effective_factor, old)
        blowout_applied = True

        # Show old linear comparison
        old_linear_penalty = excess * BLOWOUT_PENALTY_PER_POINT
        old_linear_factor = max(BLOWOUT_MIN_FACTOR, 1.0 - old_linear_penalty)
        old_linear_min = round(pre_blowout * old_linear_factor, 1)
        saved = current_minutes - old_linear_min
        if abs(saved) > 0.05:
            trace_info(
                f"Old linear: ×{old_linear_factor:.4f} → {old_linear_min:.1f} min  "
                f"({C.GRN}new curve saved {saved:+.1f} min{C.DIM})"
            )
    else:
        if abs_spread > 0:
            trace_info(f"Spread {abs_spread:.1f} < threshold {BLOWOUT_SPREAD_THRESHOLD} — no penalty")
        else:
            trace_info("No spread data — no blowout penalty applied")

    endstep("Post Blowout", current_minutes)

    # ══════════════════════════════════════════════════════════════════
    # STEP 3b: Game Context — Rest / B2B Fatigue
    # ══════════════════════════════════════════════════════════════════
    step("3b", "Game Context — Rest / B2B Fatigue")

    pre_rest = current_minutes
    rest_factor = 1.0  # Will be overwritten if rest_days is set

    if rest_days is not None:
        rest_factor = (
            1.0
            + REST_BOOST_PER_EXTRA_DAY * (rest_days - 1)
            - REST_PENALTY_B2B * max(0, 1 - rest_days)
        )
        rest_factor = max(REST_FACTOR_MIN, min(REST_FACTOR_MAX, rest_factor))
        trace("Rest days", f"{rest_days}", C.WHT, "days")
        if current_minutes >= STARTER_THRESHOLD_MINUTES:
            vet_penalty = 0.0
            if rest_days == 0 and target.age is not None and target.age >= VETERAN_AGE:
                vet_penalty = B2B_VETERAN_EXTRA_MINUTES / max(current_minutes, 1.0)
                trace_info(f"Veteran penalty (age {target.age} ≥ {VETERAN_AGE}): {vet_penalty:.3f}")
            effective_rest = max(rest_factor - vet_penalty, REST_FACTOR_MIN)
            old = current_minutes
            current_minutes = round(max(current_minutes * effective_rest, 0), 1)
            trace_factor("Rest adjustment", effective_rest, old)
        else:
            trace_info("Below starter threshold — rest adjustment skipped")
    elif is_b2b:
        trace("B2B (binary)", "Yes", C.YEL, "")
        if current_minutes >= STARTER_THRESHOLD_MINUTES:
            reduction = B2B_STARTER_REDUCTION_MINUTES
            if target.age is not None and target.age >= VETERAN_AGE:
                reduction += B2B_VETERAN_EXTRA_MINUTES
                trace_info(f"Veteran extra reduction: +{B2B_VETERAN_EXTRA_MINUTES:.1f} min")
            old = current_minutes
            current_minutes = round(max(current_minutes - reduction, 0), 1)
            trace_delta(f"B2B reduction (-{reduction:.1f} min)", old, current_minutes)
        else:
            trace_info("Below starter threshold — B2B reduction skipped")
    else:
        trace_info("No rest/B2B data — skipping fatigue adjustment")

    endstep("Post Rest/B2B", current_minutes)

    # ══════════════════════════════════════════════════════════════════
    # STEP 3c: Game Context — Pace Factor (stat scaling, not minutes)
    # ══════════════════════════════════════════════════════════════════
    step("3c", "Game Context — Pace Factor")
    trace_info("Pace factor affects stat rates, NOT minutes directly.")
    trace_info("Included here for completeness — no minute change.")
    endstep("Post Pace", current_minutes)

    # ══════════════════════════════════════════════════════════════════
    # STEP 3d: Game Context — Calibration Blowout (anti-double-dip)
    # ══════════════════════════════════════════════════════════════════
    step("3d", "Calibration Blowout Multiplier (ML)")

    if blowout_applied and abs_spread >= BLOWOUT_SPREAD_THRESHOLD:
        trace_info("Base blowout penalty already applied in Step 3a.")
        trace_info(
            "If ML calibration blowout multiplier is active, it will be "
            "half-dampened to prevent double-dip."
        )
        trace_info("Example: calibration=0.96 → dampened to 0.98")
    else:
        trace_info("No blowout fired — calibration would apply at full strength if active.")
    trace_info("(Calibration data requires live CalibrationService — shown for awareness)")
    endstep("Post Calibration", current_minutes)

    # ══════════════════════════════════════════════════════════════════
    # STEP 3e: Game Context — Competitive Context (anti-double-dip)
    # ══════════════════════════════════════════════════════════════════
    step("3e", "Competitive Context (Win-Loss Record)")

    pre_comp = current_minutes
    wp = win_pct
    comp_ctx = None
    ctx_mult = 1.0

    if wp is not None:
        trace("Win %", f"{wp:.3f}", C.WHT, "")
        comp_ctx = None
        if wp <= COMPETITIVE_TANKING_WP:
            comp_ctx = "tanking"
            ctx_mult = COMPETITIVE_TANKING_STAR_MULT
        elif wp >= COMPETITIVE_CLINCHED_WP:
            comp_ctx = "clinched"
            ctx_mult = COMPETITIVE_CLINCHED_STAR_MULT
        elif 0.45 <= wp <= 0.55:
            comp_ctx = "playoff_push"
            ctx_mult = COMPETITIVE_PUSH_STAR_MULT
        else:
            comp_ctx = None
            ctx_mult = 1.0

        if comp_ctx:
            trace("Context", comp_ctx, C.YEL, "")
            trace("Raw multiplier", f"{ctx_mult:.4f}", C.WHT, "×")

            # Anti-double-dip
            if blowout_applied and comp_ctx == "clinched" and ctx_mult < 1.0:
                original = ctx_mult
                ctx_mult = 1.0 + (ctx_mult - 1.0) * 0.5
                trace_warn(
                    f"Anti-double-dip: dampened from {original:.2f} → "
                    f"{ctx_mult:.2f} (blowout already applied)"
                )

            if ctx_mult != 1.0 and current_minutes >= STARTER_THRESHOLD_MINUTES:
                old = current_minutes
                current_minutes = round(current_minutes * ctx_mult, 1)
                trace_factor(f"Competitive ({comp_ctx})", ctx_mult, old)
            else:
                trace_info("No adjustment (neutral context or below starter threshold)")
        else:
            trace_info(f"Win% {wp:.3f} — no competitive context triggered")
            trace_info(
                f"  Tanking: ≤{COMPETITIVE_TANKING_WP}  |  "
                f"Push: 0.45-0.55  |  "
                f"Clinched: ≥{COMPETITIVE_CLINCHED_WP}"
            )
    else:
        trace_info("No win_pct data — use --win-pct to simulate (e.g., --win-pct 0.75)")

    endstep("Post Competitive Context", current_minutes)

    # ══════════════════════════════════════════════════════════════════
    # STEP 3.5: Coach Adjustments
    # ══════════════════════════════════════════════════════════════════
    step("3.5", "Coach Adjustments")

    coach = get_coach_profile(team_id)
    trace("Coach", coach.coach_name, C.CYN, "")
    trace("Style", coach.style.value, C.CYN, "")
    trace("Star multiplier", f"{coach.star_multiplier:.2f}", C.WHT, "×")
    trace("Starter multiplier", f"{coach.starter_multiplier:.2f}", C.WHT, "×")
    trace("Bench multiplier", f"{coach.bench_multiplier:.2f}", C.WHT, "×")
    trace("Max minutes override", f"{coach.max_minutes_override:.0f}", C.WHT, "min")

    # Determine player classification (star_ids already computed in Step 3a)
    is_star_coach = pid in star_ids
    is_starter_coach = current_minutes >= STARTER_THRESHOLD_MINUTES

    if is_star_coach:
        coach_mult = coach.star_multiplier
        role = "Star (top-2)"
    elif is_starter_coach:
        coach_mult = coach.starter_multiplier
        role = "Starter"
    else:
        if current_minutes >= DEEP_BENCH_THRESHOLD_MINUTES:
            coach_mult = (coach.bench_multiplier + 1.0) / 2
            role = "Rotation (softened bench)"
        else:
            coach_mult = coach.bench_multiplier
            role = "Deep bench"

    trace("Player classification", role, C.CYN, "")
    pre_coach = current_minutes
    current_minutes = round(current_minutes * coach_mult, 1)
    current_minutes = min(current_minutes, coach.max_minutes_override)
    trace_factor(f"Coach {role} multiplier", coach_mult, pre_coach)

    endstep("Post Coach Adjustment", current_minutes)

    # ══════════════════════════════════════════════════════════════════
    # STEP 3.75: Active Depth Filter
    # ══════════════════════════════════════════════════════════════════
    step("3.75", "Active Depth Filter (Agent 6)")
    trace_info("AI-driven depth filter — may zero out fringe players.")
    trace_info("Requires live CoachLearningAgent — not simulated in trace.")
    trace_info(f"Min rotation size for coach: {coach.min_rotation_size}")
    endstep("Post Depth Filter", current_minutes)

    # ══════════════════════════════════════════════════════════════════
    # STEP 4: 240-Minute Normalization (Top-Heavy Tiered Shaving)
    # ══════════════════════════════════════════════════════════════════
    step("4", "240-Minute Normalization (Top-Heavy Tiered Shaving)")

    # Build approximate projections for all teammates
    # (Using season_avg as proxy — real pipeline would have run all steps)
    team_total_raw = 0.0
    player_minutes_map: Dict[int, float] = {}
    for p in rotation:
        if p.player_id == pid:
            player_minutes_map[pid] = current_minutes
            team_total_raw += current_minutes
        else:
            # Approximate teammate projection using season_avg + coach mult
            tm = p.season_avg
            tm_is_star = p.player_id in star_ids
            if tm_is_star:
                tm *= coach.star_multiplier
            elif tm >= STARTER_THRESHOLD_MINUTES:
                tm *= coach.starter_multiplier
            elif tm >= DEEP_BENCH_THRESHOLD_MINUTES:
                tm *= (coach.bench_multiplier + 1.0) / 2
            else:
                tm *= coach.bench_multiplier
            tm = min(tm, coach.max_minutes_override)
            player_minutes_map[p.player_id] = round(tm, 1)
            team_total_raw += round(tm, 1)

    n_active = sum(1 for m in player_minutes_map.values() if m > 0)
    is_short_rotation = n_active <= SHORT_ROTATION_SIZE
    effective_abs_max = SHORT_ROTATION_STARTER_CEILING if is_short_rotation else ABSOLUTE_MAX_MINUTES

    trace("Team raw total", f"{team_total_raw:.1f}", C.WHT)
    trace("Target", f"{TOTAL_TEAM_MINUTES:.0f}", C.WHT)
    excess = team_total_raw - TOTAL_TEAM_MINUTES
    trace("Excess / Deficit", f"{excess:+.1f}", C.RED if excess > 0 else C.GRN)
    trace("Active players", f"{n_active}", C.WHT, "")
    if is_short_rotation:
        trace_warn(
            f"Short rotation ({n_active} ≤ {SHORT_ROTATION_SIZE}) — "
            f"starter ceiling raised to {SHORT_ROTATION_STARTER_CEILING:.0f} min"
        )

    # Classify target player into a tier
    if current_minutes >= NORM_LOCK_THRESHOLD:
        player_tier = "Locked (≥30 min)"
    elif current_minutes >= NORM_MID_THRESHOLD:
        player_tier = "Mid-tier (20-30 min)"
    else:
        player_tier = "Bench (<20 min)"
    trace("Player tier", player_tier, C.CYN, "")

    # Count teammates by tier
    n_locked = sum(1 for m in player_minutes_map.values() if m >= NORM_LOCK_THRESHOLD)
    n_mid = sum(1 for m in player_minutes_map.values() if NORM_MID_THRESHOLD <= m < NORM_LOCK_THRESHOLD)
    n_bench = sum(1 for m in player_minutes_map.values() if 0 < m < NORM_MID_THRESHOLD)
    trace_info(f"Tiers: {n_locked} locked, {n_mid} mid, {n_bench} bench")

    pre_norm = current_minutes

    if abs(team_total_raw - TOTAL_TEAM_MINUTES) > 0.1 and team_total_raw > 0:
        if excess > 0:
            trace_info("Over 240 → top-heavy tiered shaving")

            # ── Simulate FULL team normalization (mirrors rotation_engine.py)
            # We need to process ALL players to get accurate remaining_excess
            # for each pass, then report what happens to the target.

            # Build baseline map for coach inflation reclaim
            baseline_map: Dict[int, float] = {}
            for p in rotation:
                if p.player_id == pid:
                    baseline_map[pid] = baseline
                else:
                    baseline_map[p.player_id] = p.season_avg  # approximate

            # Classify ALL players into tiers using the team minutes map
            _LOCK = NORM_LOCK_THRESHOLD
            _MID = NORM_MID_THRESHOLD
            _MID_MAX = NORM_MID_MAX_CUT_PCT

            locked_ids = [
                pid_ for pid_, m in player_minutes_map.items()
                if m >= _LOCK
            ]
            mid_ids = [
                pid_ for pid_, m in player_minutes_map.items()
                if _MID <= m < _LOCK
            ]
            bench_ids = [
                pid_ for pid_, m in player_minutes_map.items()
                if 0 < m < _MID
            ]

            remaining_excess = excess

            # ── Pass 0: Reclaim coach inflation from ALL locked starters
            p0_reclaimed_total = 0.0
            p0_target_delta = 0.0
            for lid in locked_ids:
                if remaining_excess <= 0.1:
                    break
                cur = player_minutes_map[lid]
                floor_bl = baseline_map.get(lid, cur)
                if cur > floor_bl:
                    giveback = min(cur - floor_bl, remaining_excess)
                    player_minutes_map[lid] = round(cur - giveback, 1)
                    remaining_excess -= giveback
                    p0_reclaimed_total += giveback
                    if lid == pid:
                        p0_target_delta = -giveback

            if p0_reclaimed_total > 0.05:
                trace_info(
                    f"★ Pass 0: reclaimed {p0_reclaimed_total:.1f} min of coach "
                    f"inflation across {len(locked_ids)} locked starters"
                )
                if pid in locked_ids:
                    old = current_minutes
                    current_minutes = player_minutes_map[pid]
                    if abs(p0_target_delta) > 0.05:
                        trace_delta(
                            f"Coach inflation reclaim (→ baseline {baseline:.1f})",
                            old, current_minutes,
                        )
                    else:
                        trace_info(
                            f"  → Target at/below baseline ({baseline:.1f}) — "
                            f"fully protected in Pass 0"
                        )
                trace_info(f"  Remaining excess after Pass 0: {remaining_excess:.1f}")
            elif pid in locked_ids:
                trace_info(
                    f"★ Locked at {current_minutes:.1f} min — at/below "
                    f"baseline ({baseline:.1f}), fully protected"
                )

            # ── Pass 1: Shave bench (<20 min) proportionally (1/min weighting)
            p1_shaved = 0.0
            p1_target_delta = 0.0
            if remaining_excess > 0.1 and bench_ids:
                bench_weights = {
                    bid: 1.0 / max(player_minutes_map[bid], 0.5)
                    for bid in bench_ids
                }
                total_bw = sum(bench_weights.values())
                for bid in bench_ids:
                    if remaining_excess <= 0.1:
                        break
                    share = bench_weights[bid] / total_bw
                    cut = min(remaining_excess * share, player_minutes_map[bid])
                    player_minutes_map[bid] = round(
                        max(player_minutes_map[bid] - cut, 0), 1
                    )
                    remaining_excess -= cut
                    p1_shaved += cut
                    if bid == pid:
                        p1_target_delta = -cut
                # Recompute remaining after rounding
                remaining_excess = (
                    sum(player_minutes_map.values()) - TOTAL_TEAM_MINUTES
                )

            trace_info(
                f"Pass 1: Shaved bench tier "
                f"({len(bench_ids)} players, removed {p1_shaved:.1f} min)"
            )
            if pid in bench_ids and abs(p1_target_delta) > 0.05:
                old = current_minutes
                current_minutes = player_minutes_map[pid]
                trace_delta("Bench shave (target)", old, current_minutes)
            elif pid not in bench_ids:
                trace_info("  → Target is NOT in bench tier — immune to Pass 1")
            trace_info(f"  Remaining excess after Pass 1: {remaining_excess:.1f}")

            # ── Pass 2: Shave mid-tier (20-30 min) — max 15% cut each
            p2_shaved = 0.0
            p2_target_delta = 0.0
            if remaining_excess > 0.1 and mid_ids:
                mid_weights = {
                    mid: 1.0 / max(player_minutes_map[mid], 1.0)
                    for mid in mid_ids
                }
                total_mw = sum(mid_weights.values())
                for mid in mid_ids:
                    if remaining_excess <= 0.1:
                        break
                    share = mid_weights[mid] / total_mw
                    max_cut = player_minutes_map[mid] * _MID_MAX
                    cut = min(remaining_excess * share, max_cut)
                    player_minutes_map[mid] = round(
                        player_minutes_map[mid] - cut, 1
                    )
                    remaining_excess -= cut
                    p2_shaved += cut
                    if mid == pid:
                        p2_target_delta = -cut
                remaining_excess = (
                    sum(player_minutes_map.values()) - TOTAL_TEAM_MINUTES
                )

            if mid_ids:
                trace_info(
                    f"Pass 2: Shaved mid-tier "
                    f"({len(mid_ids)} players, max {_MID_MAX:.0%} each, "
                    f"removed {p2_shaved:.1f} min)"
                )
                if pid in mid_ids and abs(p2_target_delta) > 0.05:
                    old = current_minutes
                    current_minutes = player_minutes_map[pid]
                    trace_delta("Mid-tier shave (target)", old, current_minutes)
                elif pid not in mid_ids:
                    trace_info("  → Target is NOT in mid-tier — immune to Pass 2")
                trace_info(f"  Remaining excess after Pass 2: {remaining_excess:.1f}")
            else:
                trace_info("Pass 2: No mid-tier players — skipped")

            # ── Pass 3: Emergency — shave locked starters (max 5% each)
            p3_shaved = 0.0
            p3_target_delta = 0.0
            if remaining_excess > 0.1 and locked_ids:
                trace_warn(
                    "Pass 3: Emergency — bench+mid exhausted, "
                    "shaving locked starters (max 5% each)"
                )
                for lid in locked_ids:
                    if remaining_excess <= 0.1:
                        break
                    max_cut = player_minutes_map[lid] * 0.05
                    cut = min(remaining_excess, max_cut)
                    player_minutes_map[lid] = round(
                        player_minutes_map[lid] - cut, 1
                    )
                    remaining_excess -= cut
                    p3_shaved += cut
                    if lid == pid:
                        p3_target_delta = -cut

                trace_info(f"  Emergency removed {p3_shaved:.1f} min from locked starters")
                if pid in locked_ids and abs(p3_target_delta) > 0.05:
                    old = current_minutes
                    current_minutes = player_minutes_map[pid]
                    trace_delta("Emergency locked shave (target)", old, current_minutes)
                elif pid in locked_ids:
                    trace_info("  → Target locked but no cut needed — protected!")
            elif remaining_excess > 0.1:
                trace_info(f"  No emergency needed — excess resolved (residual {remaining_excess:.1f})")
            else:
                trace_info(f"  ✓ Fully resolved by Pass 0-2 — no emergency needed!")

            # Update current_minutes to final map value
            current_minutes = player_minutes_map[pid]

        else:
            # Under 240 — inflate proportionally
            norm_factor = TOTAL_TEAM_MINUTES / team_total_raw
            trace_info(f"Under 240 → proportional inflation (×{norm_factor:.4f})")
            if baseline > 0:
                role_ceiling = min(baseline * MAX_INFLATION_CEILING, effective_abs_max)
            else:
                role_ceiling = effective_abs_max
            old = current_minutes
            inflated = current_minutes * norm_factor
            inflated = min(inflated, role_ceiling)
            current_minutes = round(inflated, 1)
            trace_factor("Inflation", norm_factor, old)
            if inflated >= role_ceiling:
                trace_warn(f"Capped at role ceiling: {role_ceiling:.1f} min")

        # Min viable check
        if 0 < current_minutes < MIN_VIABLE_MINUTES:
            trace_warn(
                f"Below MIN_VIABLE ({MIN_VIABLE_MINUTES} min) — zeroed out!"
            )
            current_minutes = 0.0
    else:
        trace_info("Team already at ~240 — no normalization needed")

    endstep("Post 240-Min Normalization", current_minutes)

    # ══════════════════════════════════════════════════════════════════
    # FINAL SUMMARY
    # ══════════════════════════════════════════════════════════════════
    banner("PROJECTION SUMMARY")

    total_delta = current_minutes - baseline
    clr = C.GRN if total_delta >= 0 else C.RED
    sign = "+" if total_delta >= 0 else ""

    # Compute per-step deltas for clean summary waterfall
    # Each delta is the actual minute change at that step.
    blowout_delta = round(pre_blowout * effective_factor, 1) - pre_blowout if blowout_applied else 0.0
    post_blowout = round(pre_blowout * effective_factor, 1) if blowout_applied else pre_blowout

    # Rest delta: difference between pre-rest and post-rest
    rest_delta = 0.0
    if rest_days is not None and pre_rest >= STARTER_THRESHOLD_MINUTES:
        _eff_rest = max(rest_factor, REST_FACTOR_MIN)
        rest_delta = round(pre_rest * _eff_rest, 1) - pre_rest
    elif is_b2b and pre_rest >= STARTER_THRESHOLD_MINUTES:
        rest_delta = -B2B_STARTER_REDUCTION_MINUTES

    # Competitive context delta
    comp_delta = 0.0
    if comp_ctx and ctx_mult != 1.0 and pre_comp >= STARTER_THRESHOLD_MINUTES:
        comp_delta = round(pre_comp * ctx_mult, 1) - pre_comp

    # Coach adjustment delta (the actual minute gain/loss from coach multiplier)
    coach_delta = round(pre_coach * coach_mult, 1) - pre_coach
    coach_capped = min(round(pre_coach * coach_mult, 1), coach.max_minutes_override)
    if coach_capped < round(pre_coach * coach_mult, 1):
        coach_delta = coach_capped - pre_coach

    # 240-minute normalization delta
    norm_delta = current_minutes - pre_norm

    print(f"  {C.DIM}{'─' * 54}{C.RST}")
    print(f"  {'Raw Rolling Baseline:':<42} {C.WHT}{baseline:>7.1f} min{C.RST}")

    # Blowout
    bclr = C.RED if blowout_delta < 0 else C.GRN if blowout_delta > 0 else C.DIM
    print(f"  {'Agent 14 Blowout Penalty:':<42} {bclr}{blowout_delta:>+7.1f} min{C.RST}")

    # Rest/B2B
    if rest_days is not None or is_b2b:
        rclr = C.RED if rest_delta < 0 else C.GRN if rest_delta > 0 else C.DIM
        print(f"  {'Rest / B2B Fatigue:':<42} {rclr}{rest_delta:>+7.1f} min{C.RST}")

    # Competitive context
    if comp_delta != 0:
        cclr = C.RED if comp_delta < 0 else C.GRN
        label = f"Competitive Context ({comp_ctx}):" if comp_ctx else "Competitive Context:"
        print(f"  {label:<42} {cclr}{comp_delta:>+7.1f} min{C.RST}")

    # Coach — now shown as additive delta + multiplier annotation
    cch_clr = C.GRN if coach_delta > 0 else C.RED if coach_delta < 0 else C.DIM
    print(f"  {'Coach Adjustment (×' + f'{coach_mult:.2f}):':<42} {cch_clr}{coach_delta:>+7.1f} min{C.RST}")

    # 240-minute norm
    nclr = C.RED if norm_delta < 0 else C.GRN if norm_delta > 0 else C.DIM
    print(f"  {'240-Minute Engine Adjustment:':<42} {nclr}{norm_delta:>+7.1f} min{C.RST}")

    print(f"  {C.DIM}{'─' * 54}{C.RST}")
    print(f"  {C.BLD}{'FINAL SLATE PROJECTION:':<42} {clr}{current_minutes:>7.1f} min{C.RST}")
    print(f"  {C.DIM}{'Net delta from baseline:':<42} {clr}{sign}{total_delta:.1f} min{C.RST}")
    print()

    # ── Contextual warnings ──
    if baseline > 0:
        if current_minutes < baseline * 0.85:
            print(f"  {C.YEL}⚠ WARNING: Final projection is {(1 - current_minutes/baseline)*100:.0f}% below baseline.{C.RST}")
            print(f"  {C.YEL}  This may indicate over-penalization from stacking adjustments.{C.RST}")
        if current_minutes > baseline * 1.15:
            print(f"  {C.GRN}ℹ  Final projection is {(current_minutes/baseline - 1)*100:.0f}% above baseline.{C.RST}")
            print(f"  {C.GRN}  Coach inflation or injury redistribution may be elevating minutes.{C.RST}")

    # ── Comparison table (if verbose) ──
    if verbose and rotation:
        print()
        banner("FULL TEAM SNAPSHOT (Approximate)")
        print(f"  {'#':>3}  {'Player':<24} {'Pos':>4}  {'SznAvg':>7}  {'~Proj':>7}")
        print(f"  {C.DIM}{'─' * 54}{C.RST}")
        for i, p in enumerate(sorted_rot):
            proj = player_minutes_map.get(p.player_id, p.season_avg)
            if p.player_id == pid:
                proj = current_minutes
            marker = f" {C.CYN}◄{C.RST}" if p.player_id == pid else ""
            print(
                f"  {i+1:>3}  {p.player_name:<24} {p.position:>4}  "
                f"{p.season_avg:>7.1f}  {proj:>7.1f}{marker}"
            )
        approx_total = sum(
            current_minutes if p.player_id == pid else player_minutes_map.get(p.player_id, p.season_avg)
            for p in rotation
        )
        print(f"  {C.DIM}{'─' * 54}{C.RST}")
        print(f"  {'':>3}  {'TOTAL':<24} {'':>4}  {'':>7}  {approx_total:>7.1f}")
        print()


# ── CLI ───────────────────────────────────────────────────────────────

def main():
    # ── Force UTF-8 stdout on Windows (cp1252 can't handle box-drawing chars) ─
    if sys.platform == "win32" and hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="Trace a player's minute projection through the RotationEngine pipeline.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python scripts/minute_tracer.py --player "Nikola Jokic"
  python scripts/minute_tracer.py --player "Nikola Jokic" --spread -12.0
  python scripts/minute_tracer.py --player "LeBron James" --team LAL --b2b --verbose
  python scripts/minute_tracer.py --player-id 203999 --spread -14.0 --win-pct 0.78
        """,
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--player", type=str, help="Player name (fuzzy match)")
    group.add_argument("--player-id", type=int, help="NBA API player ID")

    parser.add_argument("--team", type=str, default=None, help="Team abbreviation (e.g., DEN, LAL)")
    parser.add_argument("--game-date", type=str, default=None, help="Game date YYYY-MM-DD (default: today)")
    parser.add_argument("--spread", type=float, default=None, help="Override spread (e.g., -12.0 for 12-pt home favorite)")
    parser.add_argument("--b2b", action="store_true", help="Simulate back-to-back game")
    parser.add_argument("--rest-days", type=int, default=None, help="Override rest days (0=B2B, 1=normal, 2+=rested)")
    parser.add_argument("--win-pct", type=float, default=None, help="Override team win percentage (0.0-1.0)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show full team rotation snapshot")
    parser.add_argument(
        "--synthetic", action="store_true",
        help="Use synthetic (mock) data instead of live API — works offline",
    )

    args = parser.parse_args()

    banner("MINUTE TRACER — Fetching Data...")

    target = None
    team_id = 0
    team_name = ""
    rotation: List[PlayerMinutes] = []

    if args.synthetic:
        # ── Synthetic data mode ──────────────────────────────────
        print(f"  {C.YEL}Using SYNTHETIC data (no API calls){C.RST}")
        target, team_id, team_name, rotation = build_synthetic_rotation(
            args.player or "Nikola Jokic",
            args.team or "DEN",
        )
        print(f"  {C.GRN}Built synthetic rotation: {len(rotation)} players{C.RST}")
    else:
        # ── Live data mode ────────────────────────────────────────
        print(f"  {C.DIM}Looking up player...{C.RST}")
        t0 = time.time()

        target, team_id, team_name = fetch_player_data(
            args.player, args.player_id, args.team,
        )
        if target is None:
            print(f"\n  {C.RED}ERROR: Player not found.{C.RST}")
            if args.player:
                print(f"  {C.DIM}Searched for: '{args.player}'{C.RST}")
            else:
                print(f"  {C.DIM}Searched for player_id: {args.player_id}{C.RST}")
            print(f"  {C.DIM}Try a different name or specify --team to narrow the search.{C.RST}")
            print(f"  {C.DIM}Hint: use --synthetic to test with mock data (no API needed).{C.RST}")
            sys.exit(1)

        elapsed = time.time() - t0
        print(f"  {C.GRN}Found: {target.player_name} on {team_name} ({elapsed:.1f}s){C.RST}")

        # Fetch full team rotation
        print(f"  {C.DIM}Fetching team rotation...{C.RST}")
        rotation = fetch_team_rotation(team_id)
        if not rotation:
            print(f"  {C.YEL}WARNING: Could not fetch team rotation — normalization will be approximate.{C.RST}")
            rotation = [target]
        else:
            print(f"  {C.GRN}Loaded {len(rotation)} players{C.RST}")

    # Run the trace
    run_trace(
        target=target,
        team_id=team_id,
        team_name=team_name,
        rotation=rotation,
        spread_override=args.spread,
        is_b2b=args.b2b,
        rest_days=args.rest_days,
        win_pct=args.win_pct,
        verbose=args.verbose,
    )


if __name__ == "__main__":
    main()
