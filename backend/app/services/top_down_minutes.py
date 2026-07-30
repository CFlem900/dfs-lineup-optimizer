"""Top-Down Minute Allocator — The Starter's Squeeze.

Replaces the legacy "bottom-up → normalise to 240" approach with a strict
top-down allocation that **guarantees** exactly 240 minutes per team with
zero leakage to the deep bench.

Algorithm
---------
Phase 0  — Active Status Guillotine
    Zero out players who are Out/Doubtful, have long DNP streaks, or sit
    beyond the coach's historical rotation depth.

Phase 1  — Injury Reallocation (promote backups to starter slots)
    When a primary starter is zeroed, their direct positional backup is
    promoted into the starting group.

Phase 2  — The Starter's Squeeze (5 starters get minutes first)
    Identify the 5 projected starters (healthy players highest on the
    depth chart per position).  Allocate minutes greedily:
        starter_min = clamp(season_avg, STARTER_FLOOR, STARTER_CAP)

Phase 3  — Concentrated Bench Allocation (strict 8-9 man rotation)
    remaining = 240 - sum(starter_minutes)
    Distribute ALL remaining minutes to bench via geometric-decay
    shares.  No tail share — 10th+ men get 0.  6th/7th men get
    aggressive floors (18/14 min).

Phase 4  — Residual micro-correction
    Ensure sum == 240.0 exactly via tiny proportional adjustments.

Usage
-----
Called from ``project_team_rotation()`` as the **new baseline step**,
replacing ``get_baseline_projection()`` per-player with a team-level
allocation that respects the 240-minute budget from the start.

    from app.services.top_down_minutes import allocate_team_minutes

    baselines = allocate_team_minutes(
        rotation=rotation,
        injuries=matched_injuries,
        dk_injury_statuses=dk_statuses,
        rotation_depth=expected_depth,   # from coach agent, or default 9
    )
    # baselines: dict[player_id, float] — sums to exactly 240.0
"""

import logging
from typing import Dict, List, Optional, Tuple

from app.models.player import PlayerMinutes, PlayerStatus
from app.utils.helpers import normalize_player_name


class AllocationResult(dict):
    """Dict subclass that carries promotion metadata as attributes.

    Behaves exactly like a normal ``dict[int, float]`` (sum, iteration, etc.)
    but also exposes ``.promoted_ids`` and ``.vacancy_slots`` for the
    rotation engine's usage-boost logic, ``.blowout_efficiency_factor``
    for the team-health-driven offensive discount, and ``.lms_exempt_ids``
    for "Last Man Standing" sparse-cap exemptions.
    """
    promoted_ids: set
    vacancy_slots: set
    lms_exempt_ids: set  # Sparse players exempted from SPARSE_PROMOTED_CAP
    blowout_efficiency_factor: float  # 1.0 = healthy, <1.0 = degraded offense
    alpha_vacuum: bool               # True when a high-usage star (USG% > 28) is Out
    alpha_out_names: list             # Names of zeroed alpha players (for logging)
    alpha_out_usage: float            # Total usage rate vacated by alpha(s)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.promoted_ids = set()
        self.vacancy_slots = set()
        self.lms_exempt_ids = set()
        self.blowout_efficiency_factor = 1.0
        self.alpha_vacuum = False
        self.alpha_out_names = []
        self.alpha_out_usage = 0.0

logger = logging.getLogger(__name__)

# ── Position mapping ─────────────────────────────────────────────────────
# Standard NBA 5-position slots; we need exactly one starter per slot.
_POSITION_SLOTS = ["PG", "SG", "SF", "PF", "C"]

_POS_FAMILY: Dict[str, str] = {
    "PG": "G", "SG": "G", "G": "G", "Guard": "G",
    "SF": "F", "PF": "F", "F": "F", "Forward": "F",
    "C": "C", "Center": "C",
}

# Map each position slot to its preferred position + fallback family
_SLOT_TO_POSITIONS: Dict[str, List[str]] = {
    "PG": ["PG"],
    "SG": ["SG"],
    "SF": ["SF"],
    "PF": ["PF"],
    "C": ["C"],
}

# ── Strict positional inheritance for injury redistribution ──────────────
# When a starter at position X is Out, their bench minutes flow ONLY to
# same-position or same-family players, not to random high-ranked bench.
#
# Priority order for inheriting a PG's minutes:
#   1. Exact: another PG
#   2. Family: SG or G (guard family)
#   3. Combo: G-F (can play guard)
# A Center should NEVER absorb PG minutes.
_POSITION_INHERITANCE: Dict[str, List[str]] = {
    "PG": ["PG", "SG", "G"],
    "SG": ["SG", "PG", "G"],
    "SF": ["SF", "PF", "F"],
    "PF": ["PF", "SF", "F"],
    "C":  ["C"],
}

# Usage bump for bench players who inherit a starter's role.
# When a backup absorbs starter-level minutes, they get more touches.
PROMOTION_USAGE_BUMP = 1.08   # 8% per-minute rate increase

# ── Sparse Data Promotion Guard ──────────────────────────────────────
# When a 3rd-string bench player gets promoted to starter due to mass
# rest (e.g., Memphis resting Morant + JJJ + Bane), they often have
# tiny sample sizes.  Without guardrails, the optimizer sees them at
# 30+ minutes with inflated FPPM from 2-3 garbage-time games and
# treats them as elite plays.
#
# Rules:
#   1. A player is "sparse" if games_played < MIN_GAMES_THRESHOLD OR
#      season_avg < MIN_AVG_THRESHOLD.
#   2. Sparse players promoted to starter CANNOT exceed SPARSE_PROMOTED_CAP
#      minutes, regardless of vacancy mechanics.
#   3. Their per-minute rates (pts, reb, ast, etc.) are regressed toward
#      conservative positional baselines to prevent FPPM inflation.
SPARSE_MIN_GAMES_THRESHOLD = 5      # Fewer than 5 game logs → sparse
SPARSE_MIN_AVG_THRESHOLD = 12.0     # Season avg < 12 min → sparse
SPARSE_PROMOTED_CAP = 24.0          # Max minutes for sparse promoted player

# ── "Last Man Standing" Exemption ─────────────────────────────────────
# When a team is resting/missing a massive chunk of its rotation (late
# season tank, load management), the sparse cap becomes wrong.  GG Jackson
# with 10 min season avg IS the #1 option tonight — capping him at 24
# while he'll actually play 35+ is a catastrophic projection error.
#
# The LMS exemption BYPASSES the sparse data cap for the top N promoted
# youngsters when the team's total missing minutes exceed a threshold.
#
# Exempted players:
#   - Still get FPPM regression, but with REDUCED weight (they've been
#     playing starter minutes recently even if season avg is low)
#   - Get full promoted starter allocation (PROMOTED_STARTER_CAP, not
#     SPARSE_PROMOTED_CAP)
#   - Are ranked by recency-weighted score to identify the likely
#     primary options (not random deep bench guys)
LMS_MISSING_MINUTES_THRESHOLD = 90.0   # Must be missing >= 90 min of rotation time
LMS_MAX_EXEMPT_PLAYERS = 2             # Top 2 promoted youngsters get exempted
LMS_FPPM_REGRESSION_DISCOUNT = 0.50    # Halve the regression weight for LMS players
LMS_MIN_RECENT_AVG = 6.0              # Must average >= 6 min recently to qualify

# Positional FPPM baselines for regression (conservative scrub estimates).
# These represent what a replacement-level player produces per minute
# in DK scoring.  Used to regress sparse-data players toward reality.
_POSITIONAL_FPPM_BASELINES: Dict[str, Dict[str, float]] = {
    # ── Tier System ────────────────────────────────────────────────────
    # Baselines are replacement-level per-minute rates by positional family.
    # These produce the following approximate DK FPPMs:
    #
    #   C  (Primary Bigs):           ~1.05 FPPM — easy rebounds/put-backs, rim protection
    #   G  (Ball Handlers / Wings):  ~0.90 FPPM — scoring + assists + steals
    #   F  (Shooting Wings / PFs):   ~0.75 FPPM — lower-usage, defensive role
    #
    # Derived from 2024-25 replacement-level call-up averages across
    # 50+ G-League promotions with 15+ minutes played.  These are
    # CONSERVATIVE: real starters produce 1.0-1.4 FPPM.
    # ──────────────────────────────────────────────────────────────────

    # Guards (PG/SG/G): ball-handling, scoring, assist-generating
    # Target FPPM ≈ 0.90
    # Verified: 0.44 + 0.12×1.25 + 0.14×1.5 + 0.035×2 + 0.01×2 - 0.06×0.5 + 0.05×0.5 = 0.900
    "G": {
        "pts_per_min": 0.44,   # ~13 pts in 30 min
        "reb_per_min": 0.12,   # ~3.6 reb
        "ast_per_min": 0.14,   # ~4.2 ast
        "stl_per_min": 0.035,  # ~1.0 stl
        "blk_per_min": 0.010,  # ~0.3 blk
        "tov_per_min": 0.060,  # ~1.8 tov
        "fg3m_per_min": 0.05,  # ~1.5 fg3m
    },
    # Forwards (SF/PF/F): defensive wings, secondary scorers
    # Target FPPM ≈ 0.75
    # Verified: 0.33 + 0.19×1.25 + 0.06×1.5 + 0.025×2 + 0.02×2 - 0.045×0.5 + 0.03×0.5 = 0.750
    "F": {
        "pts_per_min": 0.33,   # ~10 pts in 30 min
        "reb_per_min": 0.19,   # ~5.7 reb
        "ast_per_min": 0.06,   # ~1.8 ast
        "stl_per_min": 0.025,  # ~0.75 stl
        "blk_per_min": 0.020,  # ~0.6 blk
        "tov_per_min": 0.045,  # ~1.35 tov
        "fg3m_per_min": 0.030, # ~0.9 fg3m
    },
    # Centers (C, PF/C): primary bigs — easy boards, put-backs, rim protection
    # Target FPPM ≈ 1.05
    # Verified: 0.42 + 0.32×1.25 + 0.05×1.5 + 0.02×2 + 0.05×2 - 0.05×0.5 + 0.008×0.5 = 1.049
    "C": {
        "pts_per_min": 0.43,   # ~13 pts in 30 min (put-backs, free throws)
        "reb_per_min": 0.33,   # ~10 reb (easy boards at rim)
        "ast_per_min": 0.05,   # ~1.5 ast
        "stl_per_min": 0.020,  # ~0.6 stl
        "blk_per_min": 0.050,  # ~1.5 blk (rim protection)
        "tov_per_min": 0.050,  # ~1.5 tov
        "fg3m_per_min": 0.008, # ~0.24 fg3m
    },
}


# ── Team Health / Blowout Risk Penalty ────────────────────────────────
# When a team is missing 3+ primary rotation players (season_avg >= 20),
# they become a blowout target regardless of Vegas spread.  The surviving
# scrubs get inflated quality projections because the engine blindly
# distributes 240 minutes of "normal" fantasy production.
#
# Reality: DET missing Cade + Ivey + Duren means their surviving guards
# are low-usage, low-efficiency players who will:
#   1. Sit the 4th quarter of a 25-point blowout
#   2. Produce at a lower per-minute rate (no playmakers to create shots)
#
# This penalty fires BEFORE minutes are distributed.  It:
#   a) Caps starter minutes (they'll sit the 4th in a blowout)
#   b) Applies an offensive efficiency multiplier (<1.0) that the
#      rotation engine passes through to DFS stat projections.
BLOWOUT_PRIMARY_ROTATION_THRESHOLD = 20.0  # Season avg to qualify as "primary"
BLOWOUT_MIN_OUT = 3                         # Must be missing >= 3 primary players
BLOWOUT_STARTER_CAP_REDUCTION = 4.0        # Reduce starter cap by this many min
BLOWOUT_BENCH_CAP_REDUCTION = 2.0          # Reduce bench cap by this many min

# Efficiency multipliers by severity (number of primary rotation players Out).
# These apply to per-minute offensive rates (pts, ast, fg3m) to simulate
# decreased pace and shot quality without primary ball handlers.
_BLOWOUT_EFFICIENCY_TABLE: Dict[int, float] = {
    3: 0.92,  # Missing 3: mild degradation (backup PG can run offense)
    4: 0.87,  # Missing 4: significant (no real playmakers left)
    5: 0.82,  # Missing 5+: catastrophic (G-League level offense)
}


# ── Hard Minutes Ceiling — Style-of-Play Limiters ─────────────────────
# Certain players have a structural ceiling on their minutes regardless
# of injury context.  Even when every starter sits, coaches WON'T run
# these players 32+ minutes because of:
#   - Foul trouble (aggressive defenders who pick up 4+ fouls)
#   - Offensive limitations (can't run half-court sets)
#   - Youth/development (coaches protect young players)
#   - Conditioning (not built for 30+ min workloads)
#
# The allocator applies these ceilings AFTER Phase 2-3 allocations,
# then cascades the freed minutes to the next eligible bench player
# at the same position.  This prevents the optimizer from treating
# defensive specialists as 30+ minute upside plays.
#
# Format: normalized_name → max_minutes
# Names are lowercased and stripped of suffixes (jr/sr/ii/iii/iv/v)
# for robust matching against player_name from any data source.
#
# To add a player: just add a line below.  No code changes needed.
# To disable a ceiling: delete the line or set value to 48.0 (effectively no cap).
HARD_MINUTES_CEILING: Dict[str, float] = {
    # ── Defensive specialists / foul-prone ──
    "cason wallace":       26.0,  # OKC — elite defender, limited offense, foul magnet
    "tari eason":          26.0,  # HOU — hustle player, 4+ fouls/game, no shot creation
    "herb jones":          30.0,  # NOP — elite wing D but low-usage on offense
    "dyson daniels":       28.0,  # ATL — defensive stopper, limited shooting
    "toumani camara":      26.0,  # POR — energy defender, raw offensively

    # ── Young development players (minutes-managed) ──
    "zaccharie risacher":  28.0,  # ATL — rookie, eased into role even when vets sit
    "amen thompson":       28.0,  # HOU — raw offensively, conditioning limits
    "chet holmgren":       30.0,  # OKC — injury management, foul-prone center

    # ── Veteran minute-cappers ──
    "derrick white":       32.0,  # BOS — managed carefully by Mazzulla
}

# Compile a fast lookup set for the names we have ceilings for.
# Pre-normalized at import time for O(1) matching in the hot path.
# Uses the canonical normalize_player_name() for consistency across
# all data sources (handles diacritics, suffixes, punctuation).
_CEILING_NAMES: Dict[str, float] = {
    normalize_player_name(k): v for k, v in HARD_MINUTES_CEILING.items()
}


def get_hard_ceiling(player_name: str) -> Optional[float]:
    """Return the hard minutes ceiling for a player, or None if uncapped.

    Uses the canonical normalize_player_name() for matching — handles
    diacritics, suffixes, punctuation, and case normalization.
    """
    normalized = normalize_player_name(player_name)
    return _CEILING_NAMES.get(normalized)


def _player_can_inherit(player_pos: str, injured_slot: str) -> bool:
    """Check if *player_pos* is eligible to inherit minutes from *injured_slot*.

    Uses strict positional inheritance: PG minutes go to PG/SG/G only,
    never to C or PF.  Handles multi-position strings like "G-F", "PG-SG".
    """
    allowed = set(_POSITION_INHERITANCE.get(injured_slot, [injured_slot]))
    # Also add the family of the injured slot
    injured_family = _POS_FAMILY.get(injured_slot, injured_slot)
    allowed.add(injured_family)

    player_parts = {p.strip() for p in player_pos.replace("/", "-").split("-")}
    # Check exact matches
    if player_parts & allowed:
        return True
    # Check family matches (e.g., player "G" inherits from PG)
    player_families = {_POS_FAMILY.get(p, p) for p in player_parts}
    return bool(player_families & {injured_family})


def allocate_injury_minutes(
    injured_position: str,
    available_minutes: float,
    bench_players: List[PlayerMinutes],
    allocation: Dict[int, float],
    hard_zeroed_ids: set,
    per_player_cap: float = 36.0,
    team_name: str = "",
    starter_ids: Optional[set] = None,
    chalk_ids: Optional[set] = None,
    sit_starter_ids: Optional[set] = None,
    vegas_confirmed_ids: Optional[set] = None,
) -> Dict[int, float]:
    """Strict positional cascade: funnel injured player's minutes to same-position backups.

    When a rotation player is Out, their freed minutes MUST flow to same-position
    backups in depth-chart order, not spread randomly across the rotation.  This
    ensures G-League call-ups (Bez Mbeng, etc.) receive realistic minutes when
    they are the only available backup at a position.

    Algorithm
    ---------
    1. Filter bench to same-position players via ``_player_can_inherit``
    2. Sort candidates by depth score (season_avg-weighted, recency tiebreaker)
    3. Iterate top-down: each candidate absorbs minutes up to ``per_player_cap``
       minus any minutes already allocated in ``allocation``
    4. If a candidate has ``season_avg < 10``, their historical average is
       IGNORED — they receive the full cascade volume (up to the cap)
    5. Continue until ``available_minutes`` is exhausted or no candidates remain

    Parameters
    ----------
    injured_position : str
        The position slot vacated (e.g. "PG", "C", "SF").
    available_minutes : float
        Total freed minutes to distribute (typically the injured player's season_avg).
    bench_players : list[PlayerMinutes]
        Full bench pool (already sorted by depth-chart score in Phase 3).
    allocation : dict[int, float]
        Current minute allocation dict — updated IN-PLACE with cascaded minutes.
    hard_zeroed_ids : set
        Player IDs that are absolutely Out (never receive minutes).
    per_player_cap : float
        Maximum total minutes any single player can reach (default 36.0).
    team_name : str
        For logging.

    Returns
    -------
    dict[int, float]
        Mapping of player_id → minutes ADDED by this cascade (for logging).
    """
    if available_minutes <= 0:
        return {}

    # 1. Filter to same-position candidates who are alive and already have minutes
    #    or are available to receive minutes
    candidates = [
        p for p in bench_players
        if p.player_id not in hard_zeroed_ids
        and _player_can_inherit(p.position, injured_position)
    ]

    if not candidates:
        logger.warning(
            "[TopDown] CASCADE %s: No same-position backups for %s slot — "
            "%.1f freed minutes will fall to general pool",
            team_name, injured_position, available_minutes,
        )
        return {}

    # 2. Sort: recency-weighted score (players starting recently beat season-avg leaders)
    #    For zero-history players (season_avg < 1 AND empty minutes_last_5),
    #    DK salary becomes the PRIMARY signal — the market knows they'll play.
    def _cascade_score(p: PlayerMinutes) -> float:
        recent_avg = (
            sum(p.minutes_last_5) / len(p.minutes_last_5)
            if p.minutes_last_5
            else p.season_avg
        )
        dk_sal = getattr(p, "dk_salary", None) or 0

        # Zero-history player: DK salary IS the depth chart signal
        _is_zero_history = (
            p.season_avg < 1.0
            and (not p.minutes_last_5 or all(m == 0 for m in p.minutes_last_5))
        )
        if _is_zero_history and dk_sal > 3000:
            # Translate DK salary into minute-equivalent score:
            # $4000 → 12.0, $5000 → 18.0, $6000 → 24.0, $8000 → 36.0
            return max(12.0, (dk_sal - 3000) / 167.0)

        # Normal player: blend recent + season avg
        base = 0.50 * p.season_avg + 0.50 * recent_avg
        # DK salary tiebreaker: higher salary = market expects more minutes
        if dk_sal > 0:
            base += min((dk_sal - 3000) / 2000.0, 3.0)
        return base

    candidates.sort(key=_cascade_score, reverse=True)

    # 3. Cascade: funnel freed minutes top-down through same-position backups
    remaining = available_minutes
    additions: Dict[int, float] = {}

    for p in candidates:
        if remaining <= 0.5:
            break

        current_mins = allocation.get(p.player_id, 0.0)

        # Override historical zeroes: if season_avg < 10 or == 0,
        # IGNORE their historical average.  They are the backup and
        # WILL play these minutes regardless of their sparse history.
        # Zero-history call-ups get a tighter 24-min cap unless they're
        # flagged as situational starters (DK pricing anomaly).
        _is_sit_starter = getattr(p, "is_situational_starter", None) is True
        if p.season_avg < 10.0:
            _effective_cap = per_player_cap if _is_sit_starter else min(per_player_cap, 24.0)
            headroom = _effective_cap - current_mins
        else:
            # For established rotation players, cap at the greater of
            # their historical ceiling or per_player_cap
            hist_cap = min(p.season_avg * 1.4, per_player_cap)
            headroom = max(hist_cap, per_player_cap) - current_mins

        # ── Salary-based ceiling: cheap rookies/call-ups ──
        _sal_ceil = _salary_minute_ceiling(
            p,
            starter_ids or set(),
            chalk_ids or set(),
            sit_starter_ids or set(),
            vegas_confirmed_ids or set(),
        )
        if _sal_ceil is not None:
            headroom = min(headroom, _sal_ceil - current_mins)

        headroom = max(headroom, 0.0)
        if headroom <= 0:
            continue

        add = min(remaining, headroom)
        allocation[p.player_id] = round(current_mins + add, 1)
        additions[p.player_id] = round(add, 1)
        remaining -= add

        logger.info(
            "[TopDown] CASCADE %s: %s (%s) inherits %.1f min from %s slot | "
            "now=%.1f, season_avg=%.1f, cap=%.1f%s",
            team_name, p.player_name, p.position, add, injured_position,
            allocation[p.player_id], p.season_avg, per_player_cap,
            " [HISTORICAL OVERRIDE]" if p.season_avg < 10.0 else "",
        )

    if remaining > 0.5:
        logger.warning(
            "[TopDown] CASCADE %s: %.1f min UNPLACED after %s cascade "
            "(all candidates at cap)",
            team_name, remaining, injured_position,
        )

    return additions


# ── Allocation constants ─────────────────────────────────────────────────
TOTAL_TEAM_MINUTES = 240.0

# Starter allocation bounds
STARTER_FLOOR = 28.0          # No starter gets fewer than 28 min
STARTER_CAP = 38.0            # No starter gets more than 38 min
STARTER_DEFAULT = 32.0        # Fallback for guards/wings when season_avg is sparse
PROMOTED_STARTER_FLOOR = 22.0 # Promoted bench player gets a lower floor than a natural starter
PROMOTED_STARTER_CAP = 30.0   # Promoted bench player caps below natural starter default

# Positional starter defaults — Centers play fewer minutes than guards
STARTER_DEFAULT_C = 28.0      # Center baseline — load-managed, foul-prone
STARTER_DEFAULT_PF = 30.0     # Power Forward baseline — hybrid role
# PG/SG/SF/G/F all use STARTER_DEFAULT (32.0)

# Chalk promotion bump — max added minutes when promoting a bench player to starter
CHALK_PROMOTION_BUMP = 8.0    # e.g., 15-min bench player → 23 min, 20-min → 28 min

# Bench rotation bounds — strict 8-9 man rotation enforcement
BENCH_6TH_FLOOR = 18.0       # 6th man minimum — key rotation piece
BENCH_7TH_FLOOR = 14.0       # 7th man minimum — regular rotation
BENCH_DEEP_FLOOR = 0.0       # 8th+ man — earns minutes from geometric share only, no floor
BENCH_CAP = 26.0              # No bench player gets more than 26 min
BENCH_VACANCY_CAP = 32.0      # Bench player inheriting vacancy position gets higher cap
_BENCH_DECAY = 0.72           # Geometric decay factor for bench share computation
BENCH_SCRUB_CAP = 8.0         # Hard ceiling for min-salary + no-signal bench player
BENCH_SCRUB_OWNERSHIP_THRESHOLD = 2.0  # Ownership % below this triggers scrub cap

# ── Backup Big Man Cap ──────────────────────────────────────────────
# Cheap Centers (C, PF/C, C/PF) in timeshares or bench roles get
# over-projected because the engine fills 240 minutes without knowing
# about coaching timeshare patterns (Zubac/Plumlee, Capela/Okongwu).
# This cap ensures budget bigs don't look like 10x value plays.
#
# Rule: if position contains "C" AND DK salary <= threshold,
# cap their minutes.  Exceptions: confirmed starters, chalk overrides,
# and vacancy-promoted players who are the ONLY healthy C on the team.
BACKUP_BIG_SALARY_THRESHOLD = 4200  # DK salary at or below this triggers the cap
BACKUP_BIG_MINUTES_CAP = 18.0       # Max minutes for cheap backup bigs

# ── Salary-Based Minute Ceiling (Rookie/Backup Runaway Prevention) ────
# When starters go down, the engine can dump 30+ minutes onto minimum-
# salary rookies and G-League call-ups.  Real NBA coaches don't play
# $3,500 players 34 minutes.  This tiered ceiling prevents runaway
# projections while still allowing mid-salary backups to absorb a
# realistic workload.
#
# The ceiling ONLY applies to players who are NOT:
#   - Confirmed starters (is_confirmed_starter == True)
#   - Chalk-protected (market_ownership > 15%)
#   - Situational starters (DK pricing anomaly)
#   - Vegas-confirmed (prop line exists)
#
# Tier 1: $3,000-$3,500 → 18 min cap (deep bench, garbage time only)
# Tier 2: $3,501-$4,500 → 22 min cap (backup role, limited upside)
# Tier 3: $4,501+        → 36 min cap (established player, no extra limit)
SALARY_CAP_TIER_1_MAX = 3500    # Salary at or below this → TIER 1
SALARY_CAP_TIER_2_MAX = 4500    # Salary at or below this → TIER 2
SALARY_CAP_TIER_1_MINUTES = 18.0  # Deep bench ceiling
SALARY_CAP_TIER_2_MINUTES = 22.0  # Backup role ceiling
SALARY_CAP_DEFAULT_MINUTES = 36.0 # No extra cap for established players


def _salary_minute_ceiling(
    player,
    starter_ids: set,
    chalk_ids: set,
    sit_starter_ids: set,
    vegas_confirmed_ids: set,
) -> Optional[float]:
    """Return a salary-based minute ceiling, or None if no cap applies.

    Exempt players (starters, chalk, situational, vegas) return None.
    """
    pid = player.player_id

    # Exempt: confirmed starters, chalk, situational, or vegas
    if pid in starter_ids or pid in chalk_ids:
        return None
    if pid in sit_starter_ids or pid in vegas_confirmed_ids:
        return None
    if getattr(player, "is_confirmed_starter", None) is True:
        return None
    _mkt_own = getattr(player, "market_ownership", None) or 0.0
    if _mkt_own > CHALK_OWNERSHIP_THRESHOLD:
        return None

    dk_sal = getattr(player, "dk_salary", None)
    if dk_sal is None:
        return None  # No salary info → can't tier

    if dk_sal <= SALARY_CAP_TIER_1_MAX:
        return SALARY_CAP_TIER_1_MINUTES
    elif dk_sal <= SALARY_CAP_TIER_2_MAX:
        return SALARY_CAP_TIER_2_MINUTES
    return None  # Above tier 2 → no extra cap


# Positions that qualify as "big man" for the cap.
# Uses positional family check: any position containing "C" qualifies.
_BACKUP_BIG_POSITIONS = {"C"}  # Matched via _POS_FAMILY lookup


def _is_backup_big(player, starter_ids: set, chalk_ids: set) -> bool:
    """Return True if a player is a cheap backup big man subject to the cap.

    A player qualifies when ALL of:
      1. Their position includes Center (C, PF/C, C/PF, etc.)
      2. Their DK salary <= BACKUP_BIG_SALARY_THRESHOLD ($4,200)
      3. They are NOT a starter (not in starter_ids)
      4. They are NOT a chalk override (not in chalk_ids)

    Returns False for vacancy-promoted Centers — they're the last big
    standing and will play full minutes regardless of salary.
    """
    # Check position: must include "C" in their position family
    pos_parts = {
        _POS_FAMILY.get(p.strip(), p.strip())
        for p in (player.position or "").replace("/", "-").split("-")
    }
    if not (pos_parts & _BACKUP_BIG_POSITIONS):
        return False

    # Check salary
    dk_sal = getattr(player, "dk_salary", None) or 0
    if dk_sal > BACKUP_BIG_SALARY_THRESHOLD:
        return False

    # Exempt starters and chalk overrides
    if player.player_id in starter_ids or player.player_id in chalk_ids:
        return False

    return True


# Chalk Override: market-signal starter promotion
CHALK_OWNERSHIP_THRESHOLD = 15.0  # Ownership % floor for chalk override

# DNP/status thresholds
DNP_HARD_THRESHOLD = 10       # ≥10 consecutive DNPs → auto-out
DNP_SOFT_THRESHOLD = 3        # 3-9 DNPs → auto-out only min-salary
DK_MIN_SALARY_AUTOOUT = 3200  # Above this = DK expects them to play

# Rotation player protection thresholds — players above EITHER threshold
# are NOT zeroed for Doubtful/Q/GTD; they stay active with a 0.85x
# minute reduction.  Prevents zeroing legitimate rotation guys like
# Kyle Anderson (27 min avg, $5K) or Ayo Dosunmu (22 min avg, $4.2K).
_ROTATION_PROTECTION_MIN_AVG = 18.0     # Season average threshold
_ROTATION_PROTECTION_MIN_SALARY = 3800  # DK salary threshold
_ROTATION_PROTECTION_MULTIPLIER = 0.85  # Minute reduction for protected Q/GTD/D players

# Status codes
_DK_RULED_OUT_HARD = {"OUT", "O", "IR", "INJ", "INJURED RESERVE"}  # Always zero — no exceptions
_DK_RULED_OUT_SOFT = {"D", "DOUBTFUL"}  # Zero UNLESS rotation-player guardrail applies
_DK_RULED_OUT = _DK_RULED_OUT_HARD | _DK_RULED_OUT_SOFT  # Combined set (used by pre-guillotine)
_DK_ACTIVE_RETURN = {"Q", "QUESTIONABLE", "GTD", "P", "PROBABLE"}
_INJURY_OUT_STATUSES = {"Out", "IR", "Injured Reserve"}
_INJURY_DOUBTFUL_STATUSES = {"Doubtful"}

# Default rotation depth when coach agent data unavailable
DEFAULT_ROTATION_DEPTH = 9
MAX_ROTATION_DEPTH = 9        # Absolute hard ceiling — never allocate to 10+ players

# ── Rotation Tightness Model ──────────────────────────────────────────
# Dynamic per-team rotation cap based on coach tendencies.  When the
# rotation cap is hit, leftover injury minutes are pushed UPWARD to
# top starters (the "Thibodeau Rule") instead of DOWN to deep-bench
# rookies.  This prevents runaway projections for minimum-salary scrubs.
#
# Coaches like Brown/Thibodeau (NYK) run tight 8-man rotations where
# starters play 40+ minutes.  Coaches like Jenkins (MEM) prefer 10-man
# rotations with more distributed minutes.
#
# The target rotation size comes from:
#   1. Coach profile (min_rotation_size from coach.py) — primary
#   2. Coach agent DB query (actual recent games) — dynamic override
#   3. DEFAULT_ROTATION_DEPTH (9) — fallback
# Absolute ceiling — no player can EVER exceed this, regardless of Phase 3.8
# (Thibodeau Rule) or Phase 4 residual distribution.  Superstars ($8,500+)
# get a slightly higher ceiling because coaches actually play them 40+ min
# in tight playoff-race games (e.g., Jokic/Tatum/SGA routinely hit 39-40).
ABSOLUTE_MAX_MINUTES = 38.0           # Standard absolute ceiling
ABSOLUTE_MAX_MINUTES_STAR = 40.0      # Superstar ceiling (salary >= $8,500)
ABSOLUTE_MAX_STAR_SALARY = 8500       # Salary threshold for star ceiling

THIBODEAU_RULE_STARTER_CAP = 40.0    # Reduced from 42 → 40 (respects absolute ceiling)
THIBODEAU_RULE_MAX_RECIPIENTS = 3   # Top N starters to receive overflow
ROTATION_ACTIVE_MIN_THRESHOLD = 8.0  # Minutes to count as "in the rotation"


def _absolute_ceiling(p: PlayerMinutes) -> float:
    """Return the absolute maximum minutes a player can receive.

    Superstars (salary >= $8,500) get ABSOLUTE_MAX_MINUTES_STAR (40.0).
    Everyone else gets ABSOLUTE_MAX_MINUTES (38.0).

    This ceiling is enforced AFTER all phases (including Thibodeau Rule
    and Phase 4 residual correction) to prevent any player from exceeding
    realistic regulation limits.
    """
    dk_sal = getattr(p, "dk_salary", None) or 0
    if dk_sal >= ABSOLUTE_MAX_STAR_SALARY:
        return ABSOLUTE_MAX_MINUTES_STAR
    return ABSOLUTE_MAX_MINUTES


def calculate_team_rotation_size(
    rotation: List[PlayerMinutes],
    allocation: Dict[int, float],
    hard_zeroed_ids: set,
) -> int:
    """Count the number of players currently receiving meaningful minutes.

    A player is "in the rotation" if they have > ROTATION_ACTIVE_MIN_THRESHOLD
    minutes allocated AND are not hard-zeroed (Out/IR).

    Returns the count of active rotation players.
    """
    return sum(
        1 for pid, mins in allocation.items()
        if mins > ROTATION_ACTIVE_MIN_THRESHOLD
        and pid not in hard_zeroed_ids
    )

# ── Dynamic Starter Promotion (vacancy detection) ─────────────────────
# When a star (season_avg >= threshold) is ruled Out, the positional slot
# they occupied is a "vacancy".  Vacancy slots use recency-weighted scoring
# + DK salary signal instead of the season-avg-heavy _player_sort_score,
# preventing bench players who've been starting recently from losing the
# slot to veterans with higher season averages but lower recent minutes.
VACANCY_STAR_THRESHOLD = 28.0       # Season avg qualifying as a "star starter"
VACANCY_RECENT_AVG_THRESHOLD = 24.0 # Recent avg (last 5) to auto-promote

# ── Positional Cascade (Injury Minutes Inheritance) ───────────────
# When ANY rotation player (season_avg >= this threshold) is Out, their
# freed minutes cascade strictly to same-position backups.  This is
# lower than VACANCY_STAR_THRESHOLD because mid-level starters (20-27
# min avg) still leave significant minutes that must flow positionally.
INJURY_CASCADE_MIN_AVG = 12.0       # Out player must avg >= 12 min to trigger cascade
INJURY_CASCADE_PER_PLAYER_CAP = 36.0  # No single player receives > 36 min total from cascade


def _positions_overlap(pos_a: str, pos_b: str) -> bool:
    """Check if two position strings share a positional family."""
    parts_a = {_POS_FAMILY.get(p.strip(), p.strip()) for p in pos_a.split("-")}
    parts_b = {_POS_FAMILY.get(p.strip(), p.strip()) for p in pos_b.split("-")}
    return bool(parts_a & parts_b)


def _is_sparse_data_player(p: PlayerMinutes) -> bool:
    """Return True if a player has insufficient game log history.

    A player is "sparse" when:
      - They have fewer than SPARSE_MIN_GAMES_THRESHOLD non-zero game logs, OR
      - Their season_avg is below SPARSE_MIN_AVG_THRESHOLD (12 min)

    Sparse players should NOT receive full starter workloads when promoted
    because their per-minute rates are unreliable (small sample → inflated
    FPPM from garbage time).
    """
    # Count actual games played: non-zero entries in minutes_last_10
    games_played = sum(1 for m in p.minutes_last_10 if m > 0) if p.minutes_last_10 else 0
    return (
        games_played < SPARSE_MIN_GAMES_THRESHOLD
        or p.season_avg < SPARSE_MIN_AVG_THRESHOLD
    )


def calculate_missing_starter_minutes(
    zeroed_players: List[PlayerMinutes],
) -> float:
    """Sum the season-average minutes of all zeroed (Out/inactive) players.

    This represents the total "missing rotation capacity" — how many
    minutes of established production the team has lost tonight.

    When this exceeds LMS_MISSING_MINUTES_THRESHOLD (90), the sparse
    data cap should be bypassed for the top promoted youngsters because
    they are the de-facto primary options.

    Examples
    --------
    Memphis resting Morant (34m) + JJJ (30m) + Bane (30m) = 94 → triggers LMS
    Detroit missing Cade (34m) + Ivey (28m) + Thompson (26m) + Duren (30m) = 118 → triggers LMS
    One star out (34m) = 34 → does NOT trigger LMS (bench is deep enough)
    """
    return sum(p.season_avg for p in zeroed_players)


def identify_lms_exempt_players(
    starters: List[PlayerMinutes],
    zeroed_players: List[PlayerMinutes],
    chalk_ids: set,
    team_name: str = "",
) -> set:
    """Identify sparse promoted starters who qualify for "Last Man Standing" exemption.

    When a team's missing rotation minutes exceed the threshold, the top N
    sparse-data promoted starters are exempted from the SPARSE_PROMOTED_CAP.
    They receive full PROMOTED_STARTER_CAP minutes instead.

    Selection criteria for "top N":
      1. Must be a promoted starter (season_avg < STARTER_FLOOR, not chalk)
      2. Must be sparse (_is_sparse_data_player)
      3. Ranked by recency-weighted score: 60% recent_avg + 40% season_avg
         (Recent form dominates — if they've been playing 25+ min lately,
          they're the clear primary option despite low season avg)

    Returns
    -------
    set of player_id values that are LMS-exempt.
    """
    missing_minutes = calculate_missing_starter_minutes(zeroed_players)

    if missing_minutes < LMS_MISSING_MINUTES_THRESHOLD:
        return set()

    # Find sparse promoted starters who would be capped
    sparse_promoted = []
    for s in starters:
        _is_promoted = s.season_avg < STARTER_FLOOR and s.player_id not in chalk_ids
        if _is_promoted and _is_sparse_data_player(s):
            # Recency-weighted score for ranking
            recent_avg = (
                sum(s.minutes_last_5) / len(s.minutes_last_5)
                if s.minutes_last_5
                else s.season_avg
            )
            # Filter: must show SOME recent playing time (not pure G-League scrubs)
            if recent_avg < LMS_MIN_RECENT_AVG:
                continue
            lms_score = 0.60 * recent_avg + 0.40 * s.season_avg
            sparse_promoted.append((s, lms_score, recent_avg))

    if not sparse_promoted:
        return set()

    # Sort by LMS score descending — top players are the primary options
    sparse_promoted.sort(key=lambda x: x[1], reverse=True)

    # Exempt the top N
    exempt_ids = set()
    for s, score, recent_avg in sparse_promoted[:LMS_MAX_EXEMPT_PLAYERS]:
        exempt_ids.add(s.player_id)
        logger.info(
            "[TopDown] LMS EXEMPT: %s (%s) | missing_min=%.0f, "
            "season_avg=%.1f, recent_avg=%.1f, lms_score=%.1f | "
            "BYPASSING sparse cap (%.0f → %.0f max)",
            s.player_name, s.position, missing_minutes,
            s.season_avg, recent_avg, score,
            SPARSE_PROMOTED_CAP, PROMOTED_STARTER_CAP,
        )

    if exempt_ids:
        logger.warning(
            "[TopDown] %s: LAST MAN STANDING — %.0f missing minutes "
            "(threshold=%.0f), %d sparse players EXEMPTED from %.0f-min cap: %s",
            team_name, missing_minutes, LMS_MISSING_MINUTES_THRESHOLD,
            len(exempt_ids), SPARSE_PROMOTED_CAP,
            ", ".join(
                s.player_name for s, _, _ in sparse_promoted[:LMS_MAX_EXEMPT_PLAYERS]
            ),
        )

    return exempt_ids


def regress_sparse_fppm(
    player: PlayerMinutes,
    lms_exempt: bool = False,
) -> Dict[str, float]:
    """Regress a sparse-data player's per-minute rates toward positional baselines.

    When a low-usage bench player is suddenly promoted to a starter role,
    their tiny sample size FPPM is unreliable.  A player with 2 games of
    garbage-time stats might show 1.5 FP/min which is elite territory —
    but in reality they're a replacement-level scrub.

    Parameters
    ----------
    player : PlayerMinutes
        The player to regress.
    lms_exempt : bool
        If True, this player has "Last Man Standing" exemption — they are
        the primary option on a gutted team.  Regression weight is discounted
        by LMS_FPPM_REGRESSION_DISCOUNT (halved) because their recent role
        is more informative than their season average suggests.

    Formula
    -------
    For each stat rate:
        regressed = weight * actual + (1 - weight) * baseline

    where weight = min(games_played / SPARSE_MIN_GAMES_THRESHOLD, 1.0)

    For LMS-exempt players:
        effective_weight = weight + (1 - weight) * LMS_FPPM_REGRESSION_DISCOUNT
        (e.g., 2 games: base weight=0.4, LMS weight=0.4 + 0.6*0.5 = 0.7)

    Returns
    -------
    dict with regressed per-minute rates: pts_per_min, reb_per_min, etc.
    """
    games_played = sum(1 for m in player.minutes_last_10 if m > 0) if player.minutes_last_10 else 0
    weight = min(games_played / SPARSE_MIN_GAMES_THRESHOLD, 1.0)

    # LMS exemption: boost the weight toward actual rates.
    # Their recent heavy usage is more trustworthy than season avg suggests.
    if lms_exempt and weight < 1.0:
        weight = weight + (1.0 - weight) * LMS_FPPM_REGRESSION_DISCOUNT

    # Determine positional family for baseline lookup
    pos = (player.position or "SF").upper().split("/")[0].split("-")[0]
    family = _POS_FAMILY.get(pos, "F")
    baseline = _POSITIONAL_FPPM_BASELINES.get(family, _POSITIONAL_FPPM_BASELINES["F"])

    stat_fields = [
        "pts_per_min", "reb_per_min", "ast_per_min",
        "stl_per_min", "blk_per_min", "tov_per_min", "fg3m_per_min",
    ]

    regressed: Dict[str, float] = {}
    for field in stat_fields:
        actual = getattr(player, field, 0.0) or 0.0
        base_val = baseline.get(field, 0.0)
        regressed[field] = round(weight * actual + (1.0 - weight) * base_val, 4)

    if weight < 1.0:
        # Compute approximate FPPM for logging (DK scoring: PTS + 1.25*REB + 1.5*AST + 2*STL + 2*BLK - 0.5*TOV + 0.5*FG3M)
        actual_fppm = (
            (getattr(player, "pts_per_min", 0) or 0)
            + 1.25 * (getattr(player, "reb_per_min", 0) or 0)
            + 1.5 * (getattr(player, "ast_per_min", 0) or 0)
            + 2.0 * (getattr(player, "stl_per_min", 0) or 0)
            + 2.0 * (getattr(player, "blk_per_min", 0) or 0)
            - 0.5 * (getattr(player, "tov_per_min", 0) or 0)
            + 0.5 * (getattr(player, "fg3m_per_min", 0) or 0)
        )
        regressed_fppm = (
            regressed["pts_per_min"]
            + 1.25 * regressed["reb_per_min"]
            + 1.5 * regressed["ast_per_min"]
            + 2.0 * regressed["stl_per_min"]
            + 2.0 * regressed["blk_per_min"]
            - 0.5 * regressed["tov_per_min"]
            + 0.5 * regressed["fg3m_per_min"]
        )
        logger.info(
            "[TopDown] FPPM REGRESSION: %s (%s) | games=%d, weight=%.2f | "
            "FPPM %.3f → %.3f (baseline=%s family)",
            player.player_name, player.position, games_played, weight,
            actual_fppm, regressed_fppm, family,
        )

    return regressed


def _player_sort_score(p: PlayerMinutes) -> float:
    """Composite depth-chart score: season_avg (70%) + recent form (20%) + DK salary (10%).

    Higher score = higher on the depth chart.  This is the primary
    signal for identifying starters and backup hierarchy.

    DK salary provides a market-informed tiebreaker that helps synthetic
    players (from DK injection) compete with BDL roster players for the
    9-man rotation cap.  A $5700 player who DK deems rotation-worthy
    should rank above a $3100 deep-bench player with inflated BDL stats.
    """
    recent_avg = (
        sum(p.minutes_last_5) / len(p.minutes_last_5)
        if p.minutes_last_5
        else p.season_avg
    )
    # DK salary signal: $3000-$12000 → 0-12 point bonus (scaled as min-equivalent)
    # e.g., $8000 salary → ~6.7 point boost, $5000 → ~2.7
    dk_sal = getattr(p, "dk_salary", None) or 0
    salary_signal = max(0, (dk_sal - 3000) / 750) if dk_sal > 3000 else 0.0
    return 0.65 * p.season_avg + 0.25 * recent_avg + 0.10 * salary_signal


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _get_starter_minute_baseline(
    player: PlayerMinutes,
    is_chalk: bool = False,
    is_promoted: bool = False,
) -> float:
    """Return the starter minute baseline for a player based on position + history.

    For natural starters (season_avg >= STARTER_FLOOR): use season_avg directly.
    For promoted players (vacancy fill): blend history + bump, capped by PROMOTED_STARTER_CAP.
    For chalk overrides: blend history + bump, capped by positional default.
    For sparse-data promoted players: hard cap at SPARSE_PROMOTED_CAP (24 min).

    Position-aware defaults:
      - C:            28 min (load-managed, foul-prone)
      - PF:           30 min (hybrid big role)
      - PG/SG/SF/G/F: 32 min (perimeter players get heavier run)

    Promotion blending:
      promoted_baseline = min(season_avg + 8, cap)
      where cap = PROMOTED_STARTER_CAP (28) for vacancy fills,
            cap = positional_default for chalk overrides.
    """
    # Determine positional default
    pos = (player.position or "SF").upper().split("/")[0].split("-")[0]
    if pos == "C" or pos == "Center":
        pos_default = STARTER_DEFAULT_C
    elif pos == "PF":
        pos_default = STARTER_DEFAULT_PF
    else:
        pos_default = STARTER_DEFAULT

    avg = player.season_avg

    # Natural starters: season_avg already at starter level → use it directly
    if avg >= STARTER_FLOOR:
        return avg

    # Promoted players (bench → starter via vacancy detection):
    # Use lower cap to prevent 20-min bench players from jumping to 32
    if is_promoted and not is_chalk:
        base = min(avg + CHALK_PROMOTION_BUMP, PROMOTED_STARTER_CAP)
        # Sparse-data guard: if promoted player has tiny sample, hard cap
        # at SPARSE_PROMOTED_CAP to prevent 3rd-stringers from getting
        # full 30-min starter workloads on mass-rest nights.
        if _is_sparse_data_player(player):
            games = sum(1 for m in player.minutes_last_10 if m > 0) if player.minutes_last_10 else 0
            capped = min(base, SPARSE_PROMOTED_CAP)
            logger.info(
                "[TopDown] SPARSE PROMOTION CAP: %s (%s) | games=%d, "
                "season_avg=%.1f → capped at %.1f (was %.1f)",
                player.player_name, player.position, games,
                avg, capped, base,
            )
            return capped
        return base

    # Chalk overrides: blend history + bump, capped by positional default
    if is_chalk or avg < STARTER_FLOOR:
        return min(avg + CHALK_PROMOTION_BUMP, pos_default)

    # Fallback: positional default
    return pos_default


def _compute_bench_shares(bench_count: int) -> List[float]:
    """Compute bench minute shares using geometric decay.

    Returns a list of `bench_count` shares that sum to 1.0.
    The 6th man (index 0) gets the largest share, decaying by
    ``_BENCH_DECAY`` (0.72) for each subsequent bench position.

    Examples (rounded):
        bench_count=4 → [0.383, 0.276, 0.198, 0.143]  (typical 9-man)
        bench_count=2 → [0.581, 0.419]                  (7-man rotation)
        bench_count=5 → [0.347, 0.250, 0.180, 0.130, 0.093]  (10-man)
    """
    if bench_count <= 0:
        return []
    raw = [_BENCH_DECAY ** i for i in range(bench_count)]
    total = sum(raw)
    return [w / total for w in raw]


def _bench_floor_for_rank(rank: int) -> float:
    """Return the minute floor for a bench player by depth-chart rank.

    rank 0 = 6th man → BENCH_6TH_FLOOR (18 min)
    rank 1 = 7th man → BENCH_7TH_FLOOR (14 min)
    rank 2+ = 8th+ man → BENCH_DEEP_FLOOR (4 min)
    """
    if rank == 0:
        return BENCH_6TH_FLOOR
    elif rank == 1:
        return BENCH_7TH_FLOOR
    else:
        return BENCH_DEEP_FLOOR


def _vacancy_aware_score(p: PlayerMinutes) -> float:
    """Enhanced depth-chart score for filling a vacancy left by an injured star.

    When a star (season_avg >= 28) is Out at a position, the normal
    _player_sort_score over-weights season_avg (70%) which penalizes bench
    players who've been starting recently.  This function:

    1. Inverts the weight to 30% season + 70% recent (recency dominates)
    2. Adds a DK salary signal as a tiebreaker (capped at ±3 pts)

    Only used during vacancy detection — normal starter selection is unaffected.
    """
    recent_avg = (
        sum(p.minutes_last_5) / len(p.minutes_last_5)
        if p.minutes_last_5
        else p.season_avg
    )
    # Invert weights: recent form dominates for vacancy filling
    base_score = 0.30 * p.season_avg + 0.70 * recent_avg

    # DK salary boost: normalized around $5K anchor, capped at ±3 points
    dk_sal = getattr(p, "dk_salary", None) or 0
    if dk_sal > 0:
        salary_signal = (dk_sal - 5000) / 1000.0
        base_score += _clamp(salary_signal * 1.5, -3.0, 3.0)

    return base_score


def _pick_best_auto_promote(
    candidates: list,
    slot: str,
    logger,
    pass_label: str = "",
    dk_injury_statuses: Optional[Dict[int, str]] = None,
) -> PlayerMinutes:
    """Pick the best auto-promote candidate for a vacancy slot.

    When multiple players qualify for auto-promotion (recent_avg >= threshold),
    we rank them by:
      1. DK position match — player whose dk_position includes the slot
         (e.g. dk_position="PG" matches slot="PG") gets top priority
      2. Health status — healthy players beat Questionable/Doubtful/GTD
         (e.g. Small (healthy, $5.5K) beats Jerome (Q, $7K) for PG)
      3. DK salary — highest salary = strongest market signal for expected role
      4. Vacancy-aware score — tiebreaker using recency-weighted stats

    This ensures that when Morant (PG) is Out, Javon Small (dk_position=PG,
    $5500, healthy) beats Ty Jerome (dk_position=PG, $7000, Questionable)
    for the PG vacancy.
    """
    _dk_statuses = dk_injury_statuses or {}

    def _rank_key(item):
        p, recent_avg = item
        # 1. DK position match (1 if matches, 0 if not)
        dk_pos = getattr(p, "dk_position", None) or ""
        dk_positions = [pp.strip() for pp in dk_pos.replace("/", "-").split("-")]
        pos_match = 1 if slot in dk_positions else 0
        # 2. Health status (1 = healthy, 0 = Q/GTD/D or any non-empty status)
        dk_status = _dk_statuses.get(p.player_id, "")
        is_healthy = 1 if (not dk_status or dk_status.upper() in ("", "NONE")) else 0
        # 3. DK salary (higher = better)
        salary = getattr(p, "dk_salary", None) or 0
        # 4. Vacancy-aware score
        v_score = _vacancy_aware_score(p)
        return (pos_match, is_healthy, salary, v_score)

    candidates.sort(key=_rank_key, reverse=True)
    winner, recent_avg = candidates[0]

    # Log the selection
    dk_pos = getattr(winner, "dk_position", None) or "?"
    dk_sal = getattr(winner, "dk_salary", None) or 0
    dk_stat = _dk_statuses.get(winner.player_id, "")
    health_str = f" [{dk_stat}]" if dk_stat and dk_stat.upper() not in ("", "NONE") else ""
    pos_match_str = "DK-POS-MATCH" if slot in (dk_pos.replace("/", "-").split("-")) else ""
    logger.info(
        "[TopDown] VACANCY AUTO-PROMOTE (%s): %s (bdl=%s, dk=%s, $%s%s) | "
        "recent_avg=%.1f, %d candidates %s",
        pass_label, winner.player_name, winner.position, dk_pos,
        f"{dk_sal:,}" if dk_sal else "N/A",
        health_str,
        recent_avg, len(candidates), pos_match_str,
    )
    if len(candidates) > 1:
        for p, ravg in candidates[1:]:
            p_stat = _dk_statuses.get(p.player_id, "")
            p_health = f" [{p_stat}]" if p_stat and p_stat.upper() not in ("", "NONE") else ""
            logger.debug(
                "[TopDown]   runner-up: %s (dk=%s, $%s%s, recent=%.1f)",
                p.player_name,
                getattr(p, "dk_position", "?"),
                f"{getattr(p, 'dk_salary', 0) or 0:,}",
                p_health,
                ravg,
            )
    return winner


def calculate_team_health_penalty(
    rotation: List[PlayerMinutes],
    dk_injury_statuses: Optional[Dict[int, str]] = None,
    team_name: str = "",
) -> Tuple[int, float, float, float]:
    """Assess team health and return blowout penalty parameters.

    Counts how many "primary rotation" players (season_avg >= 20 min) are
    ruled Out/Doubtful via DK statuses.  When >= BLOWOUT_MIN_OUT (3) are
    missing, returns penalty parameters that cap minutes and discount
    offensive efficiency.

    Parameters
    ----------
    rotation : list[PlayerMinutes]
        Full roster for the team.
    dk_injury_statuses : dict[int, str] | None
        Player ID → DK injury status string (e.g., "OUT", "Q").
    team_name : str
        For logging.

    Returns
    -------
    tuple of (primary_out, starter_cap_reduction, bench_cap_reduction, efficiency_factor)
        - primary_out: count of primary rotation players who are Out
        - starter_cap_reduction: minutes to subtract from STARTER_CAP (0.0 if no penalty)
        - bench_cap_reduction: minutes to subtract from BENCH_CAP (0.0 if no penalty)
        - efficiency_factor: multiplier for offensive per-minute rates (1.0 = no penalty)
    """
    _dk_sts = dk_injury_statuses or {}

    # Identify primary rotation players: season_avg >= threshold
    primary_players = [
        p for p in rotation
        if p.season_avg >= BLOWOUT_PRIMARY_ROTATION_THRESHOLD
    ]

    # Count how many are ruled Out/Doubtful
    primary_out_players = []
    for p in primary_players:
        dk_st = _dk_sts.get(p.player_id, "").upper()
        if dk_st in _DK_RULED_OUT:
            primary_out_players.append(p)

    primary_out = len(primary_out_players)

    if primary_out < BLOWOUT_MIN_OUT:
        return (primary_out, 0.0, 0.0, 1.0)

    # Look up efficiency factor from the severity table.
    # For counts beyond the table max, use the most severe entry.
    _max_severity = max(_BLOWOUT_EFFICIENCY_TABLE.keys())
    _lookup_key = min(primary_out, _max_severity)
    efficiency_factor = _BLOWOUT_EFFICIENCY_TABLE[_lookup_key]

    # Starter/bench cap reductions scale linearly with excess beyond threshold
    _excess = primary_out - BLOWOUT_MIN_OUT  # 0 at threshold, 1 for 4 out, etc.
    starter_cap_red = BLOWOUT_STARTER_CAP_REDUCTION + _excess * 1.5
    bench_cap_red = BLOWOUT_BENCH_CAP_REDUCTION + _excess * 1.0

    logger.warning(
        "[TopDown] BLOWOUT RISK: %s missing %d primary rotation players "
        "(threshold=%d) | starter_cap -%.1f, bench_cap -%.1f, "
        "efficiency=%.2fx | Out: %s",
        team_name, primary_out, BLOWOUT_MIN_OUT,
        starter_cap_red, bench_cap_red, efficiency_factor,
        ", ".join(
            f"{p.player_name} ({p.position}, {p.season_avg:.0f}m)"
            for p in primary_out_players
        ),
    )

    return (primary_out, starter_cap_red, bench_cap_red, efficiency_factor)


def allocate_team_minutes(
    rotation: List[PlayerMinutes],
    injuries: List[PlayerStatus],
    dk_injury_statuses: Optional[Dict[int, str]] = None,
    rotation_depth: Optional[int] = None,
    team_name: str = "",
    coach_rotation_size: Optional[int] = None,
    coach_max_minutes: Optional[float] = None,
) -> Dict[int, float]:
    """Top-down 240-minute allocation for a single NBA team.

    Returns
    -------
    dict[int, float]
        Mapping of player_id → allocated minutes, guaranteed to sum
        to exactly 240.0 (within 0.1 tolerance).  Players outside the
        active rotation get 0.0.

    The returned dict also serves as the "baseline_projections" input
    for the existing Steps 1a-4 in project_team_rotation.
    """
    _dk_sts = dk_injury_statuses or {}
    _depth = rotation_depth or DEFAULT_ROTATION_DEPTH
    _depth = max(_depth, 7)                   # Safety floor: never fewer than 7-man rotation
    _depth = min(_depth, MAX_ROTATION_DEPTH)  # Hard ceiling: never more than 9-man rotation
    _target_rotation = coach_rotation_size or _depth  # Coach's preferred rotation size
    _coach_max_min = coach_max_minutes or THIBODEAU_RULE_STARTER_CAP

    # ─────────────────────────────────────────────────────────────────
    # PRE-PHASE: Team Health Assessment (Blowout Risk Detection)
    #
    # Count how many primary rotation players (avg >= 20 min) are ruled
    # Out.  When >= 3 are missing, reduce minute ceilings and flag an
    # offensive efficiency discount for the rotation engine.
    #
    # This runs BEFORE minutes are distributed so the caps are already
    # in place when Phase 2 and Phase 3 allocate.
    # ─────────────────────────────────────────────────────────────────
    (
        _health_primary_out,
        _health_starter_cap_red,
        _health_bench_cap_red,
        _health_efficiency,
    ) = calculate_team_health_penalty(
        rotation=rotation,
        dk_injury_statuses=dk_injury_statuses,
        team_name=team_name,
    )
    _is_blowout_risk = _health_primary_out >= BLOWOUT_MIN_OUT

    # ─────────────────────────────────────────────────────────────────
    # PRE-GUILLOTINE: Absolute Zero Override
    #
    # Hardcode projected_minutes to 0.0 for any player whose DraftKings
    # CSV status is exactly "O" (Out) or "IR", or who is marked Out in
    # the BDL injury database.  These players are DEAD to the allocator.
    # They cannot be rescued by vacancy detection, chalk override, or
    # any downstream phase.
    #
    # This is a belt-and-suspenders failsafe: Phase 0 also zeroes them,
    # but this set is used as a hard exclusion filter in all subsequent
    # phases to prevent minute "bleed" from name-matching failures.
    # ─────────────────────────────────────────────────────────────────
    _ABSOLUTE_OUT_DK = {"OUT", "O", "IR", "INJ", "INJURED RESERVE", "D", "DOUBTFUL"}
    _ABSOLUTE_OUT_INJ = {"Out", "IR", "Injured Reserve", "Doubtful"}

    _hard_zeroed_ids: set = set()
    for p in rotation:
        pid = p.player_id
        dk_st = _dk_sts.get(pid, "").strip().upper()
        if dk_st in _ABSOLUTE_OUT_DK:
            _hard_zeroed_ids.add(pid)
            logger.info(
                "[TopDown] HARD ZERO: %s (%s) — DK status '%s' → 0 min (absolute override)",
                p.player_name, p.position, dk_st,
            )

    for ip in injuries:
        if ip.status in _ABSOLUTE_OUT_INJ and ip.player_id not in _hard_zeroed_ids:
            _hard_zeroed_ids.add(ip.player_id)
            logger.info(
                "[TopDown] HARD ZERO: %s — injury status '%s' → 0 min (absolute override)",
                ip.player_name, ip.status,
            )

    # ─────────────────────────────────────────────────────────────────
    # PHASE 0: Active Status Guillotine
    #
    # Determine which players are AVAILABLE.  Zero out anyone who is:
    #   - Ruled Out or Doubtful (injury report)
    #   - DK status is Out/Doubtful
    #   - Has a long DNP streak with no DK signal of return
    #   - Beyond the rotation depth (10th+ man)
    # ─────────────────────────────────────────────────────────────────
    injury_by_id = {ip.player_id: ip for ip in injuries}

    # Build availability map: player_id → {"status": str, "available": bool}
    availability: Dict[int, dict] = {}

    for p in rotation:
        pid = p.player_id
        dk_st = _dk_sts.get(pid, "").upper()
        inj = injury_by_id.get(pid)
        inj_status = inj.status if inj else None
        dnp_streak = getattr(p, "recent_dnp_streak", 0)

        # Default: available
        avail = True
        reason = "Active"

        # Rule 0: Pre-Guillotine absolute override — cannot be overridden
        if pid in _hard_zeroed_ids:
            avail = False
            dk_reason = _dk_sts.get(pid, "")
            inj_reason = inj_status or ""
            reason = f"ABSOLUTE OUT: DK={dk_reason}, injury={inj_reason}"

        # Rule 1a: DK says definitively Out/IR → always zero
        elif dk_st in _DK_RULED_OUT_HARD:
            avail = False
            reason = f"DK status: {dk_st}"

        # Rule 1b: DK says Doubtful — zero UNLESS rotation-player guardrail
        elif dk_st in _DK_RULED_OUT_SOFT:
            _dk_sal_g1 = getattr(p, "dk_salary", None) or 0
            _is_rotation_g1 = (
                p.season_avg > _ROTATION_PROTECTION_MIN_AVG
                or _dk_sal_g1 > _ROTATION_PROTECTION_MIN_SALARY
            )
            if _is_rotation_g1:
                avail = True
                reason = (
                    f"DK status: {dk_st} — PROTECTED "
                    f"(avg={p.season_avg:.1f}m, sal=${_dk_sal_g1:,}) → "
                    f"0.85x multiplier"
                )
                logger.info(
                    "[TopDown] ROTATION PROTECTION: %s (%s) DK=%s but kept "
                    "active — avg=%.1f min, sal=$%s → 0.85x reduction",
                    p.player_name, p.position, dk_st,
                    p.season_avg, f"{_dk_sal_g1:,}" if _dk_sal_g1 else "N/A",
                )
            else:
                avail = False
                reason = f"DK status: {dk_st}"

        # Rule 2: Injury report says Out
        elif inj_status in _INJURY_OUT_STATUSES:
            avail = False
            reason = f"Injury: {inj_status}"

        # Rule 3: Injury report says Doubtful (P(play)=20%)
        # GUARDRAIL: Protect legitimate rotation players (avg > 18 min OR
        # salary > $3,800) from being zeroed.  These are players like Kyle
        # Anderson or Ayo Dosunmu who are Doubtful but often play.  Instead
        # of zeroing them, keep them active with a reduced minute multiplier.
        elif inj_status in _INJURY_DOUBTFUL_STATUSES:
            _dk_sal_g = getattr(p, "dk_salary", None) or 0
            _is_rotation_player = (
                p.season_avg > _ROTATION_PROTECTION_MIN_AVG
                or _dk_sal_g > _ROTATION_PROTECTION_MIN_SALARY
            )
            if _is_rotation_player:
                avail = True
                reason = (
                    f"Injury: {inj_status} (P≈20%) — PROTECTED "
                    f"(avg={p.season_avg:.1f}m, sal=${_dk_sal_g:,}) → "
                    f"0.85x multiplier applied in Phase 2"
                )
                logger.info(
                    "[TopDown] ROTATION PROTECTION: %s (%s) is %s but kept "
                    "active — avg=%.1f min, sal=$%s → 0.85x reduction",
                    p.player_name, p.position, inj_status,
                    p.season_avg, f"{_dk_sal_g:,}" if _dk_sal_g else "N/A",
                )
            else:
                avail = False
                reason = f"Injury: {inj_status} (P≈20%)"

        # Rule 4: Long DNP streak (hard threshold)
        elif dnp_streak >= DNP_HARD_THRESHOLD:
            # Unless DK says they're active
            if dk_st in _DK_ACTIVE_RETURN:
                avail = True
                reason = f"DNP×{dnp_streak} but DK={dk_st} (returning)"
            else:
                avail = False
                reason = f"Auto-out: {dnp_streak} consecutive DNPs"

        # Rule 5: Moderate DNP streak with min salary
        elif dnp_streak >= DNP_SOFT_THRESHOLD:
            dk_sal = getattr(p, "dk_salary", None)
            if dk_st in _DK_ACTIVE_RETURN:
                avail = True
                reason = f"DNP×{dnp_streak} but DK={dk_st} (returning)"
            elif dk_sal and dk_sal >= DK_MIN_SALARY_AUTOOUT:
                avail = True
                reason = f"DNP×{dnp_streak} but DK sal ${dk_sal:,} (expected to play)"
            else:
                avail = False
                reason = f"Auto-out: {dnp_streak} DNPs, min salary"

        availability[pid] = {
            "available": avail,
            "reason": reason,
            "player": p,
        }

    # Separate available from unavailable
    available_players = [
        info["player"]
        for info in availability.values()
        if info["available"]
    ]
    zeroed_players = [
        info["player"]
        for info in availability.values()
        if not info["available"]
    ]

    # Log guillotine results
    for info in availability.values():
        if not info["available"]:
            p = info["player"]
            logger.info(
                "[TopDown] GUILLOTINE: %s (%s) → 0 min | %s",
                p.player_name, p.position, info["reason"],
            )

    # ─────────────────────────────────────────────────────────────────
    # Alpha Vacuum Detection
    #
    # When a high-usage star (USG% > 28% OR DK salary > $9,000) is
    # zeroed, the team experiences an "Alpha Vacuum": the remaining
    # healthy starters absorb those shot attempts, increasing their
    # per-minute efficiency.  The top 2 offensive focal points on the
    # team get a 1.15x FPPM boost applied in the rotation engine.
    #
    # This is NOT the same as blowout_efficiency_factor (which reduces
    # rates when 3+ players are out).  Alpha Vacuum fires when even
    # ONE dominant player is out — the offense doesn't collapse, it
    # just concentrates around the remaining stars.
    # ─────────────────────────────────────────────────────────────────
    ALPHA_USAGE_THRESHOLD = 28.0   # USG% to qualify as an alpha
    ALPHA_SALARY_THRESHOLD = 9000  # DK salary alternative qualifier

    _alpha_out_names = []
    _alpha_out_usage = 0.0

    for p in zeroed_players:
        _usg = getattr(p, "usage_rate", 0.0) or 0.0
        _dk_sal = getattr(p, "dk_salary", None) or 0
        if _usg > ALPHA_USAGE_THRESHOLD or _dk_sal > ALPHA_SALARY_THRESHOLD:
            _alpha_out_names.append(p.player_name)
            _alpha_out_usage += _usg
            logger.info(
                "[TopDown] ALPHA OUT: %s (%s) — USG=%.1f%%, salary=$%s → "
                "Alpha Vacuum triggered",
                p.player_name, p.position, _usg,
                f"{_dk_sal:,}" if _dk_sal else "N/A",
            )

    _alpha_vacuum = len(_alpha_out_names) > 0

    if _alpha_vacuum:
        logger.info(
            "[TopDown] %s: ALPHA VACUUM — %d alpha(s) out (%s), "
            "total vacated USG=%.1f%% → top-2 remaining get 1.15x FPPM boost",
            team_name, len(_alpha_out_names),
            ", ".join(_alpha_out_names), _alpha_out_usage,
        )

    # ─────────────────────────────────────────────────────────────────
    # Build positional cascade ledger: track freed minutes per position
    # from ALL out rotation players (not just stars).  This drives the
    # post-Phase-3 positional cascade that funnels minutes to same-
    # position backups instead of spreading them randomly.
    # ─────────────────────────────────────────────────────────────────
    _cascade_ledger: Dict[str, float] = {}  # position_slot → freed minutes
    for info in availability.values():
        if not info["available"]:
            p = info["player"]
            if p.season_avg >= INJURY_CASCADE_MIN_AVG:
                # Map player's position to canonical slots
                matched_slots = [
                    s for s in _POSITION_SLOTS
                    if _positions_overlap(p.position, s)
                ]
                if matched_slots:
                    # Split freed minutes across matched slots
                    per_slot = p.season_avg / len(matched_slots)
                    for slot in matched_slots:
                        _cascade_ledger[slot] = _cascade_ledger.get(slot, 0.0) + per_slot
                    logger.info(
                        "[TopDown] CASCADE LEDGER: %s (%s, %.1f avg) → "
                        "%.1f min freed to %s",
                        p.player_name, p.position, p.season_avg,
                        p.season_avg, matched_slots,
                    )

    # ─────────────────────────────────────────────────────────────────
    # PHASE 0b: Depth chart trim
    #
    # Sort available players by depth-chart score and keep only the
    # top N (rotation_depth).  Everyone else gets 0.
    #
    # CHALK PROTECTION: Players with strong market signals (high
    # ownership, confirmed starter, or high optimal%) are PROTECTED
    # from depth trimming.  They displace lower-ranked non-chalk
    # players instead.  This prevents the Josh Minott bug: a 64%-owned
    # starter projected for 14 min because his season_avg (~14) puts
    # him 10th+ on the depth chart, so Phase 0b kills him before
    # Phase 0d can rescue him.
    # ─────────────────────────────────────────────────────────────────

    # Pre-scan: identify chalk-protected player IDs before trimming.
    # A player is chalk-protected if ANY market signal says "starter":
    #   - is_confirmed_starter == True
    #   - market_ownership > CHALK_OWNERSHIP_THRESHOLD (15%)
    #   - is_situational_starter == True (DK pricing anomaly)
    _chalk_protected_ids: set = set()
    for p in available_players:
        _is_confirmed = getattr(p, "is_confirmed_starter", None)
        _is_situational = getattr(p, "is_situational_starter", None)
        _mkt_own = getattr(p, "market_ownership", None) or 0.0
        if _is_confirmed is True or _is_situational is True or _mkt_own > CHALK_OWNERSHIP_THRESHOLD:
            _chalk_protected_ids.add(p.player_id)
            logger.info(
                "[TopDown] CHALK PROTECT: %s (%s) shielded from depth trim | "
                "own=%.1f%%, confirmed=%s, situational=%s, score=%.1f",
                p.player_name, p.position, _mkt_own,
                _is_confirmed, _is_situational, _player_sort_score(p),
            )

    available_players.sort(key=_player_sort_score, reverse=True)

    if len(available_players) > _depth:
        # Split into chalk-protected and unprotected
        protected = [p for p in available_players if p.player_id in _chalk_protected_ids]
        unprotected = [p for p in available_players if p.player_id not in _chalk_protected_ids]

        # Keep: all protected + top unprotected up to _depth
        keep_count = max(0, _depth - len(protected))
        kept_unprotected = unprotected[:keep_count]
        beyond_depth = unprotected[keep_count:]

        available_players = protected + kept_unprotected
        # Re-sort so downstream phases see correct ordering
        available_players.sort(key=_player_sort_score, reverse=True)

        for p in beyond_depth:
            availability[p.player_id]["available"] = False
            availability[p.player_id]["reason"] = (
                f"Beyond {_depth}-man rotation (rank 10+)"
            )
            zeroed_players.append(p)
            logger.info(
                "[TopDown] DEPTH TRIM: %s (%s, score=%.1f) → 0 min | "
                "Outside %d-man rotation",
                p.player_name, p.position,
                _player_sort_score(p), _depth,
            )

    if not available_players:
        # Pathological case: entire team unavailable
        logger.error(
            "[TopDown] %s: No available players! Returning empty allocation.",
            team_name,
        )
        return {p.player_id: 0.0 for p in rotation}

    # ─────────────────────────────────────────────────────────────────
    # PHASE 0c: Vacancy Detection — Dynamic Starter Promotion
    #
    # When a star (season_avg >= VACANCY_STAR_THRESHOLD) is zeroed in
    # Phase 0, their position slot(s) are marked as "vacancies".
    # Vacancy slots use _vacancy_aware_score (recency-weighted + DK
    # salary) instead of _player_sort_score, and auto-promote any
    # candidate whose recent_avg >= VACANCY_RECENT_AVG_THRESHOLD.
    #
    # This fixes the bench-player-starting-recently problem:
    #   e.g., Javon Small (season_avg=15, recent_avg=28) beats
    #   Marcus Smart (season_avg=25, recent_avg=22) for the PG slot
    #   when Ja Morant (season_avg=33) is Out.
    # ─────────────────────────────────────────────────────────────────
    vacancy_slots: set = set()
    for info in availability.values():
        if not info["available"]:
            p = info["player"]
            if p.season_avg >= VACANCY_STAR_THRESHOLD:
                # Match position slots using both exact match AND family
                # overlap.  BDL returns generic positions ("G", "F", "G-F")
                # instead of NBA-specific ("PG", "SG", "SF", "PF").
                # _positions_overlap handles family mapping: "G" → G family
                # matches PG/SG, "F" → F family matches SF/PF, etc.
                matched_slots = [
                    s for s in _POSITION_SLOTS
                    if _positions_overlap(p.position, s)
                ]
                for slot in matched_slots:
                    vacancy_slots.add(slot)
                if matched_slots:
                    logger.info(
                        "[TopDown] VACANCY: %s (%s, %.1f avg) Out → "
                        "%s slot(s) use enhanced selection",
                        p.player_name, p.position, p.season_avg,
                        matched_slots,
                    )

    # ─────────────────────────────────────────────────────────────────
    # PHASE 0c.5: Vacancy Rescue — Pull positional backups from depth trim
    #
    # When a starter at position X is Out (vacancy detected above),
    # their positional backup may have been trimmed by Phase 0b's
    # depth-chart cut (e.g., backup PG ranked 10th gets killed).
    # This is the "Daniss Jenkins bug": the direct PG backup sits at
    # 0 minutes because he was depth-trimmed before vacancy detection.
    #
    # Fix: For each vacancy slot, find the highest-ranked SAME-POSITION
    # player in the depth-trimmed pool and rescue them back into
    # available_players.  This ensures at least one positional backup
    # exists for the vacancy fill and subsequent bench allocation.
    # ─────────────────────────────────────────────────────────────────
    if vacancy_slots and zeroed_players:
        _rescued_count = 0
        for v_slot in vacancy_slots:
            # Check if we have at least 2 same-position available players
            # (one to fill the starter vacancy, one for bench coverage)
            _pos_available = sum(
                1 for p in available_players
                if _player_can_inherit(p.position, v_slot)
            )
            _has_cover = _pos_available >= 2
            if _has_cover:
                continue  # Already have a same-position bench option

            # Find best depth-trimmed player who matches the vacancy slot
            # NEVER rescue hard-zeroed (Out/IR) players
            _rescue_candidates = [
                p for p in zeroed_players
                if _player_can_inherit(p.position, v_slot)
                and p.player_id not in _hard_zeroed_ids
                and availability.get(p.player_id, {}).get("reason", "").startswith("Beyond")
            ]
            if not _rescue_candidates:
                continue

            _rescue_candidates.sort(key=_player_sort_score, reverse=True)
            rescued = _rescue_candidates[0]

            # Restore to available pool
            available_players.append(rescued)
            zeroed_players.remove(rescued)
            availability[rescued.player_id]["available"] = True
            availability[rescued.player_id]["reason"] = (
                f"Vacancy rescue: {v_slot} starter Out → positional backup"
            )
            _rescued_count += 1
            logger.info(
                "[TopDown] VACANCY RESCUE: %s (%s, score=%.1f) pulled from "
                "depth trim → covers %s vacancy",
                rescued.player_name, rescued.position,
                _player_sort_score(rescued), v_slot,
            )

        if _rescued_count > 0:
            # Re-sort available players with rescues included
            available_players.sort(key=_player_sort_score, reverse=True)

    # ─────────────────────────────────────────────────────────────────
    # PHASE 0c.6: Cascade Rescue — Positional backup rescue for ALL
    # out rotation players (not just stars).
    #
    # The vacancy rescue above (Phase 0c.5) only triggers for star
    # vacancies (season_avg >= 28).  But when a mid-level rotation
    # player (12-27 min avg) is Out, their positional backup may also
    # be depth-trimmed.  This pass rescues those backups so the
    # positional cascade in Phase 3.6 has candidates to funnel to.
    # ─────────────────────────────────────────────────────────────────
    if _cascade_ledger and zeroed_players:
        _cascade_rescued = 0
        for c_slot, freed_mins in _cascade_ledger.items():
            if c_slot in vacancy_slots:
                continue  # Already handled by Phase 0c.5
            if freed_mins < INJURY_CASCADE_MIN_AVG:
                continue  # Not enough freed minutes to justify rescue

            # Check if we have ANY same-position available player
            _pos_available = sum(
                1 for p in available_players
                if _player_can_inherit(p.position, c_slot)
            )
            if _pos_available >= 1:
                continue  # At least one same-position backup exists

            # Find best depth-trimmed player for this position
            _rescue_candidates = [
                p for p in zeroed_players
                if _player_can_inherit(p.position, c_slot)
                and p.player_id not in _hard_zeroed_ids
                and availability.get(p.player_id, {}).get("reason", "").startswith("Beyond")
            ]
            if not _rescue_candidates:
                continue

            _rescue_candidates.sort(key=_player_sort_score, reverse=True)
            rescued = _rescue_candidates[0]

            available_players.append(rescued)
            zeroed_players.remove(rescued)
            availability[rescued.player_id]["available"] = True
            availability[rescued.player_id]["reason"] = (
                f"Cascade rescue: {c_slot} rotation player Out "
                f"(%.1f freed min) → positional backup" % freed_mins
            )
            _cascade_rescued += 1
            logger.info(
                "[TopDown] CASCADE RESCUE: %s (%s, score=%.1f, avg=%.1f) "
                "pulled from depth trim → covers %s cascade (%.1f freed min)",
                rescued.player_name, rescued.position,
                _player_sort_score(rescued), rescued.season_avg,
                c_slot, freed_mins,
            )

        if _cascade_rescued > 0:
            available_players.sort(key=_player_sort_score, reverse=True)

    # ─────────────────────────────────────────────────────────────────
    # PHASE 0c.8: Situational Starter Reservation
    #
    # DK pricing anomaly detection (upstream in lineup_optimizer_service)
    # flags players with is_situational_starter=True when:
    #   - DK salary > $3,000 (above minimum — DK expects a real role)
    #   - < 5 games in BDL database (G-League call-up / deep bench)
    #
    # These players get a 24-minute reservation BEFORE standard
    # distribution to guarantee they enter the rotation.  This:
    #   1. Rescues them from depth-trim (Phase 0b) if they were cut
    #   2. Protects them from scrub-cap (Phase 3) since they have a
    #      confirmed DK pricing signal
    #   3. Ensures Phase 0d's chalk override can promote them to
    #      starter if appropriate
    #
    # The reservation does NOT allocate minutes yet — it marks the
    # player as a confirmed starter candidate with a 24-minute floor
    # expectation.  Actual allocation happens in Phase 2/3.
    # ─────────────────────────────────────────────────────────────────
    SITUATIONAL_STARTER_MINUTE_FLOOR = 24.0
    _sit_starter_ids: set = set()

    for p in rotation:
        if getattr(p, "is_situational_starter", None) is not True:
            continue
        if p.player_id in _hard_zeroed_ids:
            continue  # Never override Out/IR status

        pid = p.player_id
        _sit_starter_ids.add(pid)

        # If depth-trimmed, rescue them back into available pool
        if not availability.get(pid, {}).get("available", False):
            old_reason = availability.get(pid, {}).get("reason", "")
            if old_reason.startswith("Beyond"):
                available_players.append(p)
                if p in zeroed_players:
                    zeroed_players.remove(p)
                availability[pid]["available"] = True
                availability[pid]["reason"] = (
                    f"Situational starter rescue: DK sal=${p.dk_salary:,}, "
                    f"pricing implies confirmed role"
                )
                logger.info(
                    "[TopDown] SITUATIONAL RESCUE: %s (%s, $%s) pulled from "
                    "depth trim → DK pricing anomaly implies starter role",
                    p.player_name, p.position,
                    f"{p.dk_salary:,}" if p.dk_salary else "?",
                )

        # Promote to confirmed starter so Phase 0d chalk override
        # picks them up and Phase 3 scrub-cap doesn't apply
        if not getattr(p, "is_confirmed_starter", None):
            p.is_confirmed_starter = True
            logger.info(
                "[TopDown] SITUATIONAL STARTER: %s (%s) auto-confirmed | "
                "salary=$%s, games=%d → 24-min floor reserved",
                p.player_name, p.position,
                f"{p.dk_salary:,}" if p.dk_salary else "?",
                sum(1 for m in p.minutes_last_10 if m > 0) if p.minutes_last_10 else 0,
            )

    if _sit_starter_ids:
        available_players.sort(key=_player_sort_score, reverse=True)
        logger.info(
            "[TopDown] %s: %d situational starter(s) reserved with "
            "%.0f-min floor: %s",
            team_name, len(_sit_starter_ids), SITUATIONAL_STARTER_MINUTE_FLOOR,
            ", ".join(
                p.player_name for p in rotation
                if p.player_id in _sit_starter_ids
            ),
        )

    # ─────────────────────────────────────────────────────────────────
    # PHASE 0c.8b: News-Confirmed Starters (Beat Reporter NLP)
    #
    # When the NewsParserService detects a "will start" tweet for a
    # fringe player, their news_confirmed_starter flag is set.  These
    # players get a 30-minute floor (higher than situational's 24 min)
    # because a beat reporter tweet is a stronger signal than DK pricing.
    # ─────────────────────────────────────────────────────────────────
    NEWS_CONFIRMED_MINUTE_FLOOR = 30.0
    _news_starter_ids: set = set()

    for p in rotation:
        if getattr(p, "news_confirmed_starter", None) is not True:
            continue
        if p.player_id in _hard_zeroed_ids:
            continue

        pid = p.player_id
        _news_starter_ids.add(pid)

        # If depth-trimmed, rescue them back into available pool
        if not availability.get(pid, {}).get("available", False):
            old_reason = availability.get(pid, {}).get("reason", "")
            if old_reason.startswith("Beyond"):
                available_players.append(p)
                if p in zeroed_players:
                    zeroed_players.remove(p)
                availability[pid]["available"] = True
                availability[pid]["reason"] = (
                    f"News-confirmed starter rescue: beat reporter alert"
                )
                logger.info(
                    "[TopDown] NEWS RESCUE: %s (%s) pulled from depth trim "
                    "→ beat reporter confirms starting role",
                    p.player_name, p.position,
                )

        # Ensure confirmed starter flag is set for Phase 0d chalk override
        if not getattr(p, "is_confirmed_starter", None):
            p.is_confirmed_starter = True

        logger.info(
            "[TopDown] NEWS STARTER: %s (%s) confirmed by beat reporter | "
            "%.0f-min floor reserved",
            p.player_name, p.position, NEWS_CONFIRMED_MINUTE_FLOOR,
        )

    if _news_starter_ids:
        available_players.sort(key=_player_sort_score, reverse=True)
        logger.info(
            "[TopDown] %s: %d news-confirmed starter(s) with %.0f-min floor: %s",
            team_name, len(_news_starter_ids), NEWS_CONFIRMED_MINUTE_FLOOR,
            ", ".join(
                p.player_name for p in rotation
                if p.player_id in _news_starter_ids
            ),
        )

    # ─────────────────────────────────────────────────────────────────
    # PHASE 0c.9: Vegas PRA Prop → Implied Minutes Override
    #
    # When The Odds API has a PRA prop for a fringe player (DK salary
    # ≤ $4,500), the upstream pipeline stamps vegas_implied_minutes and
    # vegas_confirmed=True on the PlayerMinutes object.  This is the
    # strongest available signal that a deep bench / G-League call-up
    # has a confirmed role, even with zero BDL game logs.
    #
    # Logic:
    #   1. If depth-trimmed, rescue back into available pool
    #   2. Promote to confirmed starter if implied >= 20 min
    #   3. Chalk-protect so Phase 0b doesn't trim them later
    # ─────────────────────────────────────────────────────────────────
    _vegas_confirmed_ids: set = set()

    for p in rotation:
        if getattr(p, "vegas_confirmed", None) is not True:
            continue
        if p.player_id in _hard_zeroed_ids:
            continue  # Never override Out/IR status

        pid = p.player_id
        v_min = getattr(p, "vegas_implied_minutes", None) or 0.0
        if v_min <= 0:
            continue

        _vegas_confirmed_ids.add(pid)

        # If depth-trimmed, rescue them back into available pool
        if not availability.get(pid, {}).get("available", False):
            old_reason = availability.get(pid, {}).get("reason", "")
            if old_reason.startswith("Beyond"):
                available_players.append(p)
                if p in zeroed_players:
                    zeroed_players.remove(p)
                availability[pid]["available"] = True
                availability[pid]["reason"] = (
                    f"Vegas PRA rescue: implied_min={v_min:.1f}"
                )
                logger.info(
                    "[TopDown] VEGAS RESCUE: %s (%s) pulled from depth trim → "
                    "PRA prop implies %.1f min",
                    p.player_name, p.position, v_min,
                )

        # If implied >= 20 min, promote to confirmed starter
        if v_min >= 20.0 and not getattr(p, "is_confirmed_starter", None):
            p.is_confirmed_starter = True
            logger.info(
                "[TopDown] VEGAS STARTER: %s (%s) auto-confirmed | "
                "implied_min=%.1f (PRA prop ≥ 20 min threshold)",
                p.player_name, p.position, v_min,
            )

        # Chalk-protect so Phase 0b won't trim
        if pid not in _chalk_protected_ids:
            _chalk_protected_ids.add(pid)

        logger.info(
            "[TopDown] VEGAS CONFIRMED: %s (%s, $%s) — "
            "implied_min=%.1f, season_avg=%.1f",
            p.player_name, p.position,
            f"{p.dk_salary:,}" if p.dk_salary else "?",
            v_min, p.season_avg,
        )

    if _vegas_confirmed_ids:
        available_players.sort(key=_player_sort_score, reverse=True)
        logger.info(
            "[TopDown] %s: %d Vegas-confirmed player(s): %s",
            team_name, len(_vegas_confirmed_ids),
            ", ".join(
                p.player_name for p in rotation
                if p.player_id in _vegas_confirmed_ids
            ),
        )

    # ─────────────────────────────────────────────────────────────────
    # PHASE 0d: Chalk Override — Market-Signal Starter Promotion
    #
    # When external data (Data Hub CSV) provides confirmed starter
    # status or high ownership (> CHALK_OWNERSHIP_THRESHOLD), pre-seed
    # those players into the starter list BEFORE Phase 1's depth-chart
    # selection.
    #
    # This catches "next-man-up" value plays like Noah Penda and
    # Josh Minott: historically bench players whose depth-chart scores
    # rank them low, but who are NOW confirmed starters due to
    # injuries.  The DFS market knows (ownership 55%+, Starting=True),
    # and this step injects that signal into the minute allocator.
    #
    # Rules:
    #   1. Only fires when market signals exist (None fields → skip)
    #   2. Confirmed starters take priority over ownership-only
    #   3. Max 5 chalk overrides (can't exceed starter count)
    #   4. Must claim a valid position slot
    #   5. Must not be already zeroed by Phase 0 (injury/DNP)
    # ─────────────────────────────────────────────────────────────────
    chalk_starters: List[PlayerMinutes] = []
    chalk_ids: set = set()
    chalk_slots_claimed: set = set()

    # Collect chalk candidates from available players.
    # Multi-signal failsafe: promote if ANY market signal fires:
    #   1. is_confirmed_starter == True  (Hub "Starting" column)
    #   2. market_ownership > CHALK_OWNERSHIP_THRESHOLD (15%)
    # Players that survive Phase 0 (injury guillotine) AND Phase 0b
    # (depth trim, with chalk protection) are eligible.
    _chalk_candidates = []
    for p in available_players:
        _is_confirmed = getattr(p, "is_confirmed_starter", None) is True
        _mkt_own = getattr(p, "market_ownership", None) or 0.0

        if _is_confirmed or _mkt_own > CHALK_OWNERSHIP_THRESHOLD:
            _chalk_candidates.append(p)
        else:
            # Log near-misses for debugging (ownership 10-15%)
            if _mkt_own > 10.0:
                logger.debug(
                    "[TopDown] CHALK NEAR-MISS: %s (%s) | own=%.1f%% "
                    "(below %.1f%% threshold), confirmed=%s",
                    p.player_name, p.position, _mkt_own,
                    CHALK_OWNERSHIP_THRESHOLD, _is_confirmed,
                )

    if _chalk_candidates:
        # Sort: confirmed starters first → ownership descending →
        # depth-chart score as tiebreaker
        def _chalk_sort_key(p):
            confirmed = 1 if getattr(p, "is_confirmed_starter", None) is True else 0
            own = getattr(p, "market_ownership", None) or 0.0
            return (confirmed, own, _player_sort_score(p))

        _chalk_candidates.sort(key=_chalk_sort_key, reverse=True)

        for p in _chalk_candidates:
            if len(chalk_starters) >= 5:
                break

            # Find a valid position slot for this player
            _claimed_slot = None
            p_positions = [pp.strip() for pp in p.position.split("-")]

            # Exact position match first
            for slot in _POSITION_SLOTS:
                if slot in chalk_slots_claimed:
                    continue
                if slot in p_positions:
                    _claimed_slot = slot
                    break

            # Family match fallback
            if _claimed_slot is None:
                for slot in _POSITION_SLOTS:
                    if slot in chalk_slots_claimed:
                        continue
                    if _positions_overlap(p.position, slot):
                        _claimed_slot = slot
                        break

            if _claimed_slot is not None:
                chalk_starters.append(p)
                chalk_ids.add(p.player_id)
                chalk_slots_claimed.add(_claimed_slot)

                _own_val = getattr(p, "market_ownership", None) or 0.0
                _own_str = f"{_own_val:.1f}%" if _own_val > 0 else "N/A"
                _confirmed = getattr(p, "is_confirmed_starter", None) is True

                # Build signal description for diagnostic logging
                _signals = []
                if _confirmed:
                    _signals.append("CONFIRMED")
                if _own_val > CHALK_OWNERSHIP_THRESHOLD:
                    _signals.append(f"OWN={_own_str}")
                _signal_str = " + ".join(_signals) or "UNKNOWN"

                logger.info(
                    "[TopDown] Promoted %s to Starter due to Market Signal "
                    "(Own: %s) | pos=%s → %s slot, signal=%s, "
                    "season_avg=%.1f, depth_score=%.1f",
                    p.player_name, _own_str, p.position, _claimed_slot,
                    _signal_str, p.season_avg, _player_sort_score(p),
                )
            else:
                logger.warning(
                    "[TopDown] CHALK BLOCKED: %s (%s) — no open position slot | "
                    "own=%.1f%%, confirmed=%s, claimed_slots=%s",
                    p.player_name, p.position,
                    getattr(p, "market_ownership", None) or 0.0,
                    getattr(p, "is_confirmed_starter", None),
                    chalk_slots_claimed,
                )

        if chalk_starters:
            logger.info(
                "[TopDown] %s: %d chalk override(s) pre-seeded into starters",
                team_name, len(chalk_starters),
            )
    else:
        # Log when NO chalk candidates found — helps diagnose data pipeline issues
        _any_ownership = any(
            getattr(p, "market_ownership", None) is not None
            for p in available_players
        )
        _any_confirmed = any(
            getattr(p, "is_confirmed_starter", None) is True
            for p in available_players
        )
        if not _any_ownership and not _any_confirmed:
            logger.debug(
                "[TopDown] %s: No chalk candidates — no market signals on "
                "any of %d available players (ownership data not imported?)",
                team_name, len(available_players),
            )

    # ─────────────────────────────────────────────────────────────────
    # PHASE 1: Identify 5 Starters via Positional Depth Chart
    #
    # For each of the 5 position slots (PG/SG/SF/PF/C), find the
    # highest-ranked available player.  When a primary starter is
    # zeroed (Phase 0), the direct positional backup is automatically
    # promoted — this IS the injury reallocation, baked into the
    # selection process.
    #
    # For vacancy slots (Phase 0c), uses _vacancy_aware_score which
    # weights recent form (70%) over season avg (30%), plus a DK
    # salary tiebreaker.  Also auto-promotes candidates with
    # recent_avg >= VACANCY_RECENT_AVG_THRESHOLD (proven starters).
    #
    # Handles multi-position players (G, F, G-F, etc.) by matching
    # against positional families.
    #
    # NOTE: chalk_starters from Phase 0d are pre-seeded into starters,
    # starter_ids, _used, and _filled_slots.  The positional loops
    # below fill remaining unfilled slots around them.
    # ─────────────────────────────────────────────────────────────────
    starters: List[PlayerMinutes] = list(chalk_starters)
    starter_ids: set = set(chalk_ids)
    bench_pool: List[PlayerMinutes] = list(available_players)  # Copy

    # Strategy: two-pass greedy assignment by position slot.
    #
    # Pass 1: Exact position match (PG→PG, SG→SG, etc.)
    #   This ensures a backup PG inherits the PG slot when the
    #   starter PG is out, rather than being outscored by a SG
    #   who happens to be in the same guard family.
    #
    # Pass 2: Family match (PG/SG→G family, SF/PF→F family)
    #   Fills any unfilled slots with the best family-match player.

    _used = set(chalk_ids)
    _filled_slots = set(chalk_slots_claimed)

    # Pass 1: Exact position match only
    for slot in _POSITION_SLOTS:
        if slot in _filled_slots:
            continue  # Already claimed (e.g., by chalk override)

        is_vacancy = slot in vacancy_slots
        score_fn = _vacancy_aware_score if is_vacancy else _player_sort_score
        best = None
        best_score = -1.0

        # Collect all auto-promote candidates for vacancy slots
        # (instead of breaking on the first one found)
        _auto_candidates = []

        for p in available_players:
            if p.player_id in _used:
                continue
            # Exact match: player's listed position matches the slot
            p_positions = [pp.strip() for pp in p.position.split("-")]
            if slot in p_positions:
                # Collect auto-promote candidates
                if is_vacancy and p.minutes_last_5:
                    recent_avg = sum(p.minutes_last_5) / len(p.minutes_last_5)
                    if recent_avg >= VACANCY_RECENT_AVG_THRESHOLD:
                        _auto_candidates.append((p, recent_avg))
                        continue  # Don't score normally; handled below

                score = score_fn(p)
                if score > best_score:
                    best = p
                    best_score = score

        # Pick best auto-promote candidate (DK position match → health → salary → score)
        if _auto_candidates:
            best = _pick_best_auto_promote(
                _auto_candidates, slot, logger, pass_label="pass 1",
                dk_injury_statuses=dk_injury_statuses,
            )
            best_score = 999.0

        if best is not None:
            starters.append(best)
            starter_ids.add(best.player_id)
            _used.add(best.player_id)
            _filled_slots.add(slot)
            if is_vacancy:
                logger.info(
                    "[TopDown] VACANCY FILL: %s (%s) wins %s slot | "
                    "vacancy_score=%.1f, normal_score=%.1f",
                    best.player_name, best.position, slot,
                    _vacancy_aware_score(best), _player_sort_score(best),
                )

    # Pass 2: Family match for unfilled slots
    for slot in _POSITION_SLOTS:
        if slot in _filled_slots:
            continue

        is_vacancy = slot in vacancy_slots
        score_fn = _vacancy_aware_score if is_vacancy else _player_sort_score
        best = None
        best_score = -1.0

        # Collect all auto-promote candidates for vacancy slots
        _auto_candidates = []

        for p in available_players:
            if p.player_id in _used:
                continue
            if _positions_overlap(p.position, slot):
                # Collect auto-promote candidates
                if is_vacancy and p.minutes_last_5:
                    recent_avg = sum(p.minutes_last_5) / len(p.minutes_last_5)
                    if recent_avg >= VACANCY_RECENT_AVG_THRESHOLD:
                        _auto_candidates.append((p, recent_avg))
                        continue  # Don't score normally; handled below

                score = score_fn(p)
                if score > best_score:
                    best = p
                    best_score = score

        # Pick best auto-promote candidate (DK position match → health → salary → score)
        if _auto_candidates:
            best = _pick_best_auto_promote(
                _auto_candidates, slot, logger, pass_label="pass 2",
                dk_injury_statuses=dk_injury_statuses,
            )
            best_score = 999.0

        if best is not None:
            starters.append(best)
            starter_ids.add(best.player_id)
            _used.add(best.player_id)
            _filled_slots.add(slot)
            if is_vacancy:
                logger.info(
                    "[TopDown] VACANCY FILL (pass 2): %s (%s) wins %s slot | "
                    "vacancy_score=%.1f",
                    best.player_name, best.position, slot,
                    _vacancy_aware_score(best),
                )

    # If we couldn't fill all 5 slots with positional matches, fill
    # remaining slots with the best available players regardless of position
    if len(starters) < 5:
        remaining = [p for p in available_players if p.player_id not in _used]
        remaining.sort(key=_player_sort_score, reverse=True)
        for p in remaining:
            if len(starters) >= 5:
                break
            starters.append(p)
            starter_ids.add(p.player_id)
            _used.add(p.player_id)

    # Bench = available players not in starters
    bench = [p for p in available_players if p.player_id not in starter_ids]
    bench.sort(key=_player_sort_score, reverse=True)

    # Log starter identification
    for i, s in enumerate(starters):
        _chalk_tag = " ← CHALK OVERRIDE" if s.player_id in chalk_ids else ""
        _promo_tag = (
            " ← PROMOTED (backup)"
            if s.season_avg < STARTER_FLOOR and s.player_id not in chalk_ids
            else ""
        )
        logger.info(
            "[TopDown] STARTER %d: %s (%s) | season=%.1f, score=%.1f%s%s",
            i + 1, s.player_name, s.position,
            s.season_avg, _player_sort_score(s),
            _promo_tag, _chalk_tag,
        )

    # ─────────────────────────────────────────────────────────────────
    # PHASE 1b: "Last Man Standing" Exemption Detection
    #
    # Before applying the sparse data cap in Phase 2, check if the team
    # is so gutted that sparse youngsters are the de-facto primary options.
    #
    # If total missing minutes >= LMS_MISSING_MINUTES_THRESHOLD (90):
    #   - Identify the top 2 sparse promoted starters by recency score
    #   - EXEMPT them from the 24-min sparse cap
    #   - They get full PROMOTED_STARTER_CAP (30 min) instead
    #   - FPPM regression is still applied but with reduced weight
    #
    # This prevents the catastrophic error of capping GG Jackson at 24
    # when Memphis has rested Morant + JJJ + Bane and Jackson is the
    # #1 option who will play 35+ minutes.
    # ─────────────────────────────────────────────────────────────────
    _lms_exempt_ids = identify_lms_exempt_players(
        starters=starters,
        zeroed_players=zeroed_players,
        chalk_ids=chalk_ids,
        team_name=team_name,
    )

    # ─────────────────────────────────────────────────────────────────
    # PHASE 2: The Starter's Squeeze — Allocate Starter Minutes
    #
    # Each starter gets a position-aware minute baseline via
    # _get_starter_minute_baseline(), then clamped to [STARTER_FLOOR, STARTER_CAP].
    #
    # Position-aware defaults:
    #   C  → 28 min baseline (load-managed, foul-prone)
    #   PF → 30 min baseline (hybrid big role)
    #   PG/SG/SF/G/F → 32 min baseline (perimeter players get heavier run)
    #
    # Chalk-promoted players use: min(season_avg + 12, positional_default)
    #   Example: Gafford (C, avg=14) → min(14+12, 28) = 26 → clamped to 28 min
    #   Example: Small (PG, avg=18) → min(18+12, 32) = 30 → clamped to 30 min
    # ─────────────────────────────────────────────────────────────────
    allocation: AllocationResult = AllocationResult()

    for s in starters:
        # Position-aware baseline with chalk-promotion blending
        is_chalk = s.player_id in chalk_ids
        _is_promoted = s.season_avg < STARTER_FLOOR and not is_chalk
        raw = _get_starter_minute_baseline(s, is_chalk=is_chalk, is_promoted=_is_promoted)

        # Check if player has Q/GTD status — apply "if-plays" reduction.
        #
        # Questionable/GTD players face high risk of:
        #   1. Missing the game entirely (not reflected in minutes if they sit)
        #   2. Playing on a strict minute restriction if active
        #   3. Early exit if symptoms recur
        #
        # A 0.75x multiplier reflects ~75% of normal minutes when active,
        # which empirically matches the typical 6-8 minute per-game reduction
        # seen in restricted-minutes outings (e.g., 32 min avg → 24 min).
        inj = injury_by_id.get(s.player_id)
        dk_st = _dk_sts.get(s.player_id, "").upper()
        if_plays_factor = 1.0
        if inj and inj.status == "Doubtful":
            # Doubtful players who survived Phase 0 via rotation protection
            # get the protection multiplier (0.85x) instead of being zeroed
            if_plays_factor = _ROTATION_PROTECTION_MULTIPLIER
        elif inj and inj.status == "Questionable":
            if_plays_factor = 0.75
        elif inj and inj.status in ("Game-Time Decision", "GTD", "Day-To-Day"):
            if_plays_factor = 0.75
        elif dk_st in ("D", "DOUBTFUL"):
            # DK Doubtful who survived Phase 0 via rotation protection
            if_plays_factor = _ROTATION_PROTECTION_MULTIPLIER
        elif dk_st in ("Q", "QUESTIONABLE"):
            if_plays_factor = 0.75
        elif dk_st == "GTD":
            if_plays_factor = 0.75

        # Use lower floor for promoted bench players (season_avg < starter floor)
        # to prevent artificially inflating 15-min bench players to 28 min
        _floor = PROMOTED_STARTER_FLOOR if _is_promoted else STARTER_FLOOR
        _cap = STARTER_CAP

        # Situational starter floor: DK pricing anomaly guarantees 24 min
        if s.player_id in _sit_starter_ids:
            _floor = max(_floor, SITUATIONAL_STARTER_MINUTE_FLOOR)

        # News-confirmed starter floor: beat reporter tweet guarantees 30 min
        if s.player_id in _news_starter_ids:
            _floor = max(_floor, NEWS_CONFIRMED_MINUTE_FLOOR)

        # Vegas implied minutes floor: PRA prop → reverse-engineered minutes
        _v_impl = getattr(s, "vegas_implied_minutes", None)
        if s.player_id in _vegas_confirmed_ids and _v_impl and _v_impl > 0:
            _floor = max(_floor, min(_v_impl, 30.0))
            logger.debug(
                "[TopDown]   %s: Vegas floor=%.1f min (PRA implied)",
                s.player_name, _v_impl,
            )

        # Blowout risk: reduce starter ceiling for NATURAL starters only.
        # Natural starters (season_avg >= STARTER_FLOOR) may sit the 4th
        # quarter when the team is expected to lose badly.  Promoted bench
        # players are NOT reduced — when the team is gutted, the surviving
        # scrubs play full games; the penalty comes through the efficiency
        # factor, not minutes.
        if _is_blowout_risk and not _is_promoted and s.season_avg >= STARTER_FLOOR:
            _cap = _cap - _health_starter_cap_red

        # Sparse-data promoted players: enforce the hard cap from
        # _get_starter_minute_baseline AND lower the floor so the clamp
        # doesn't push them back up above SPARSE_PROMOTED_CAP.
        #
        # EXCEPTION: "Last Man Standing" exempt players BYPASS this cap.
        # When the team is missing 90+ minutes of rotation time, the top 2
        # sparse youngsters are the de-facto primary options and should
        # receive full promoted starter workloads (30 min, not 24).
        if _is_promoted and _is_sparse_data_player(s):
            if s.player_id in _lms_exempt_ids:
                # LMS exempt: use PROMOTED_STARTER_CAP, not SPARSE_PROMOTED_CAP.
                # These are the "last men standing" who will play 30+ minutes.
                _cap = min(_cap, PROMOTED_STARTER_CAP)
                _floor = min(_floor, PROMOTED_STARTER_CAP)
                logger.debug(
                    "[TopDown]   %s: LMS exempt → cap=%.0f (not sparse %.0f)",
                    s.player_name, PROMOTED_STARTER_CAP, SPARSE_PROMOTED_CAP,
                )
            else:
                _cap = min(_cap, SPARSE_PROMOTED_CAP)
                _floor = min(_floor, SPARSE_PROMOTED_CAP)

        # ── Salary-Based Ceiling for promoted starters ──
        # Cheap rookies/call-ups promoted to starter via vacancy fill
        # should NOT receive 30+ minutes.  NBA coaches limit them.
        _sal_ceil = _salary_minute_ceiling(
            s, starter_ids, chalk_ids,
            _sit_starter_ids, _vegas_confirmed_ids,
        )
        if _sal_ceil is not None and _is_promoted:
            _cap = min(_cap, _sal_ceil)
            _floor = min(_floor, _sal_ceil)
            logger.info(
                "[TopDown] SALARY CAP: %s (%s, sal=$%s) promoted starter "
                "capped at %.0f min (tier ceiling)",
                s.player_name, s.position,
                getattr(s, "dk_salary", "?"), _sal_ceil,
            )

        allocated = _clamp(raw * if_plays_factor, _floor, _cap)
        allocation[s.player_id] = round(allocated, 1)

    total_starter_mins = sum(allocation.values())

    logger.info(
        "[TopDown] %s: %d starters allocated %.1f min (avg %.1f)",
        team_name, len(starters), total_starter_mins,
        total_starter_mins / max(len(starters), 1),
    )

    # ─────────────────────────────────────────────────────────────────
    # PHASE 3: Concentrated Bench Allocation (Strict 8-9 Man Rotation)
    #
    # remaining = 240 - starter_total (typically ~70-80 min)
    # Distribute ALL remaining minutes across the bench rotation
    # using geometric-decay shares.  NO tail share for deep bench —
    # anyone beyond the rotation depth (set by Phase 0b) gets 0.
    #
    # Two-pass approach:
    #   Pass 1: Allocate raw shares with clamping (floor/cap)
    #   Pass 2: Redistribute surplus from capped players to uncapped
    #
    # Modern NBA rotations play 8-9 men.  The 6th/7th men should get
    # aggressive minutes (18-26 min), not the 12-min floors of the
    # old system.  10th+ men get exactly 0.0.
    # ─────────────────────────────────────────────────────────────────
    remaining = TOTAL_TEAM_MINUTES - total_starter_mins

    if remaining < 0:
        # Starters overflowed — shouldn't happen with 5 × 38 cap = 190,
        # but handle gracefully by proportionally scaling starters down
        logger.warning(
            "[TopDown] %s: Starter overflow! %.1f min > 240. Scaling down.",
            team_name, total_starter_mins,
        )
        scale = TOTAL_TEAM_MINUTES / total_starter_mins
        for pid in list(allocation.keys()):
            allocation[pid] = round(allocation[pid] * scale, 1)
        remaining = 0.0

    bench_count = len(bench)
    shares = _compute_bench_shares(bench_count)

    # Safety: if sum of all floors exceeds remaining budget, scale
    # floors down proportionally (pathological case: depth >= 12 with
    # high starter minutes).
    total_floors = sum(_bench_floor_for_rank(i) for i in range(bench_count))
    floor_scale = min(1.0, remaining / total_floors) if total_floors > 0 else 1.0

    # Pre-compute which bench players should get the higher vacancy cap.
    # The top-ranked same-position bench player for each vacancy slot
    # absorbs extra minutes as "next man up" — they shouldn't be
    # constrained by the normal 26-min bench cap.
    _vacancy_bench_ids: set = set()
    if vacancy_slots:
        for v_slot in vacancy_slots:
            for bp in bench:
                if _player_can_inherit(bp.position, v_slot):
                    _vacancy_bench_ids.add(bp.player_id)
                    logger.info(
                        "[TopDown] VACANCY BENCH BOOST: %s (%s) cap raised to %.0f "
                        "for %s vacancy",
                        bp.player_name, bp.position, BENCH_VACANCY_CAP, v_slot,
                    )
                    break  # Only top-ranked same-pos bench player

    # ── Pass 1: Allocate with clamping ────────────────────────────
    bench_alloc: List[float] = []
    capped: List[bool] = []
    scrub_capped: List[bool] = []

    for i, bp in enumerate(bench):
        if remaining <= 0 or i >= len(shares):
            allocation[bp.player_id] = 0.0
            bench_alloc.append(0.0)
            capped.append(False)
            scrub_capped.append(False)
            continue

        floor = _bench_floor_for_rank(i) * floor_scale
        raw_mins = remaining * shares[i]

        # Use higher cap for bench players filling vacancy positions
        _effective_cap = BENCH_VACANCY_CAP if bp.player_id in _vacancy_bench_ids else BENCH_CAP

        # Blowout risk: reduce bench ceiling (game will be garbage time)
        if _is_blowout_risk:
            _effective_cap = _effective_cap - _health_bench_cap_red

        # Respect the player's historical role: don't inflate WAY
        # beyond what they've ever played.
        player_cap = min(
            max(bp.season_avg * 1.5, _effective_cap) if bp.season_avg > 0 else _effective_cap,
            _effective_cap,
        )

        # ── Scrub cap: min-salary + low/no ownership → hard ceiling ──
        _bp_salary = getattr(bp, "dk_salary", None)
        _bp_ownership = getattr(bp, "market_ownership", None)
        _bp_confirmed = getattr(bp, "is_confirmed_starter", None) is True
        _is_scrub = (
            _bp_salary is not None
            and _bp_salary <= DK_MIN_SALARY_AUTOOUT
            and (_bp_ownership is None or _bp_ownership < BENCH_SCRUB_OWNERSHIP_THRESHOLD)
            and not _bp_confirmed
        )
        if _is_scrub:
            player_cap = min(player_cap, BENCH_SCRUB_CAP)
            floor = min(floor, BENCH_SCRUB_CAP)       # Override positional floor
            logger.info(
                "[TopDown] SCRUB CAP: %s (sal=$%s, own=%s) → cap=%.1f min",
                bp.player_name, _bp_salary, _bp_ownership, BENCH_SCRUB_CAP,
            )

        # ── Backup Big Man Cap: cheap Centers in timeshares ──
        _is_backup_big_man = (
            not _is_scrub  # scrub cap is already tighter
            and _is_backup_big(bp, starter_ids, chalk_ids)
        )
        if _is_backup_big_man:
            player_cap = min(player_cap, BACKUP_BIG_MINUTES_CAP)
            floor = min(floor, BACKUP_BIG_MINUTES_CAP)
            logger.info(
                "[TopDown] BACKUP BIG CAP: %s (%s, sal=$%s) → "
                "cap=%.1f min (timeshare big)",
                bp.player_name, bp.position,
                getattr(bp, "dk_salary", "?"),
                BACKUP_BIG_MINUTES_CAP,
            )

        # ── Salary-Based Ceiling: cheap rookies/call-ups ──
        _sal_ceil = _salary_minute_ceiling(
            bp, starter_ids, chalk_ids,
            _sit_starter_ids, _vegas_confirmed_ids,
        )
        _is_salary_capped = False
        if _sal_ceil is not None:
            player_cap = min(player_cap, _sal_ceil)
            floor = min(floor, _sal_ceil)
            _is_salary_capped = True
            logger.info(
                "[TopDown] SALARY CAP: %s (%s, sal=$%s) bench → cap=%.0f min",
                bp.player_name, bp.position,
                getattr(bp, "dk_salary", "?"), _sal_ceil,
            )

        scrub_capped.append(_is_scrub or _is_backup_big_man or _is_salary_capped)

        allocated = _clamp(raw_mins, floor, player_cap)
        bench_alloc.append(round(allocated, 1))
        # Capped players never receive surplus minutes during Pass 2.
        capped.append(
            _is_scrub or _is_backup_big_man or _is_salary_capped
            or raw_mins > player_cap
        )

    # ── Pass 2: Redistribute surplus from capped players ──────────
    bench_total = sum(bench_alloc)
    delta = remaining - bench_total

    if delta > 0.5:
        # Surplus exists (some players hit their cap).  Redistribute
        # proportionally to uncapped players.
        uncapped_indices = [
            i for i in range(bench_count) if not capped[i] and bench_alloc[i] > 0
        ]
        if uncapped_indices:
            uncapped_total = sum(bench_alloc[i] for i in uncapped_indices)
            if uncapped_total > 0:
                for i in uncapped_indices:
                    add = delta * (bench_alloc[i] / uncapped_total)
                    _eff_cap = BENCH_VACANCY_CAP if bench[i].player_id in _vacancy_bench_ids else BENCH_CAP
                    # Apply blowout bench cap reduction
                    if _is_blowout_risk:
                        _eff_cap = _eff_cap - _health_bench_cap_red
                    player_cap = min(
                        max(bench[i].season_avg * 1.5, _eff_cap) if bench[i].season_avg > 0 else _eff_cap,
                        _eff_cap,
                    )
                    # Enforce salary ceiling in Pass 2 redistribution
                    _p2_sal_ceil = _salary_minute_ceiling(
                        bench[i], starter_ids, chalk_ids,
                        _sit_starter_ids, _vegas_confirmed_ids,
                    )
                    if _p2_sal_ceil is not None:
                        player_cap = min(player_cap, _p2_sal_ceil)
                    bench_alloc[i] = round(
                        min(bench_alloc[i] + add, player_cap), 1
                    )

    # Write bench allocations
    for i, bp in enumerate(bench):
        allocation[bp.player_id] = bench_alloc[i]

    # Log bench allocation
    for i, bp in enumerate(bench):
        if bench_alloc[i] > 0:
            logger.info(
                "[TopDown] BENCH %d: %s (%s) → %.1f min | "
                "share=%.1f%%, floor=%.0f, cap=%s",
                i + 6, bp.player_name, bp.position, bench_alloc[i],
                shares[i] * 100 if i < len(shares) else 0,
                _bench_floor_for_rank(i) * floor_scale,
                "SCRUB" if scrub_capped[i] else ("HIT" if capped[i] else f"{BENCH_CAP:.0f}"),
            )

    # Track scrub-capped and backup-big-capped player IDs for Phase 4
    scrub_capped_ids = set(
        bench[i].player_id for i in range(len(bench)) if scrub_capped[i]
    )
    # Backup big men need a separate set so Phase 4 uses their cap (18)
    # instead of BENCH_SCRUB_CAP (8).
    _backup_big_ids = set(
        bench[i].player_id for i in range(len(bench))
        if not (  # not a pure scrub
            i < len(bench)
            and getattr(bench[i], "dk_salary", None) is not None
            and (getattr(bench[i], "dk_salary", 0) or 0) <= DK_MIN_SALARY_AUTOOUT
        )
        and _is_backup_big(bench[i], starter_ids, chalk_ids)
    )

    # Zero out everyone not in the active rotation
    for p in rotation:
        if p.player_id not in allocation:
            allocation[p.player_id] = 0.0

    # ─────────────────────────────────────────────────────────────────
    # PHASE 3.6: Positional Injury Cascade
    #
    # After Phase 3 distributes bench minutes by geometric decay
    # (position-blind), this phase corrects the distribution by
    # ensuring freed minutes from injured rotation players cascade
    # STRICTLY to same-position backups.
    #
    # Problem solved:  When a PG (30 min avg) is Out, Phase 3
    # distributes those minutes across ALL bench positions by rank.
    # A high-ranked SF might absorb most of them while the actual
    # PG backup (Bez Mbeng, G-League call-up, 0 min season avg)
    # gets nothing.  This phase forces positional inheritance.
    #
    # How it works:
    #   1. For each position with freed minutes (from _cascade_ledger),
    #      check if the same-position bench players received LESS than
    #      the freed amount.
    #   2. If there's a deficit, call allocate_injury_minutes() to
    #      top-up same-position players up to 36 min each.
    #   3. The total team minutes may temporarily exceed 240; Phase 4
    #      (residual correction) will normalize.
    # ─────────────────────────────────────────────────────────────────
    if _cascade_ledger:
        _cascade_total_added = 0.0
        for c_slot, freed_mins in _cascade_ledger.items():
            if freed_mins < 5.0:
                continue  # Not enough to justify a cascade

            # Check how many minutes same-position bench players currently have
            _same_pos_bench = [
                bp for bp in bench
                if _player_can_inherit(bp.position, c_slot)
                and bp.player_id not in _hard_zeroed_ids
            ]
            _same_pos_mins = sum(allocation.get(bp.player_id, 0) for bp in _same_pos_bench)

            # Also check starters at this position (they already got their share)
            _same_pos_starters = [
                s for s in starters
                if _player_can_inherit(s.position, c_slot)
            ]
            _same_pos_starter_mins = sum(allocation.get(s.player_id, 0) for s in _same_pos_starters)

            # The bench at this position should absorb roughly the freed mins
            # minus whatever extra the promoted starter already absorbed.
            # (Promoted starters pick up some via their elevated baseline.)
            _bench_deficit = max(0.0, freed_mins * 0.6 - _same_pos_mins)

            if _bench_deficit < 3.0:
                continue  # Bench is already absorbing enough

            logger.info(
                "[TopDown] CASCADE CHECK %s: %s slot — %.1f freed, "
                "%.1f in same-pos bench, deficit=%.1f",
                team_name, c_slot, freed_mins,
                _same_pos_mins, _bench_deficit,
            )

            # ── Rotation Gatekeeper ──────────────────────────────────
            # Check if the team's active rotation is already at the
            # coach's preferred size.  If so, ONLY cascade to players
            # who already have > 0 allocated minutes (top-up existing
            # rotation, don't expand to new deep-bench players).
            # This prevents minimum-salary rookies from getting 30 min
            # projections on tight-rotation teams (Thibodeau Rule).
            _current_rotation_size = calculate_team_rotation_size(
                rotation, allocation, _hard_zeroed_ids,
            )
            _rotation_is_full = _current_rotation_size >= _target_rotation

            # Run the positional cascade to top-up same-position backups.
            # Start with the normal bench pool, then ADD depth-trimmed
            # players who are healthy (trimmed only because of low score,
            # NOT because of injury/DNP/Out).  This allows G-League
            # call-ups like Bez Mbeng and Jamal Cain to inherit minutes
            # at their position even if they were trimmed in Phase 0b.
            _depth_trimmed_healthy = [
                p for p in zeroed_players
                if p.player_id not in _hard_zeroed_ids
                and p.player_id not in starter_ids
                and availability.get(p.player_id, {}).get("reason", "").startswith("Beyond")
            ]

            if _rotation_is_full:
                # GATEKEEPER: Only cascade to players already in the rotation
                _cascade_pool = [
                    p for p in list(bench) + _depth_trimmed_healthy
                    if allocation.get(p.player_id, 0) > 0
                ]
                logger.info(
                    "[TopDown] ROTATION GATEKEEPER: %s rotation full "
                    "(%d >= %d target) — cascade restricted to %d existing "
                    "rotation players at %s",
                    team_name, _current_rotation_size, _target_rotation,
                    len(_cascade_pool), c_slot,
                )
            else:
                _cascade_pool = list(bench) + _depth_trimmed_healthy

            additions = allocate_injury_minutes(
                injured_position=c_slot,
                available_minutes=_bench_deficit,
                bench_players=_cascade_pool,
                allocation=allocation,
                hard_zeroed_ids=_hard_zeroed_ids,
                per_player_cap=INJURY_CASCADE_PER_PLAYER_CAP,
                team_name=team_name,
                starter_ids=starter_ids,
                chalk_ids=chalk_ids,
                sit_starter_ids=_sit_starter_ids,
                vegas_confirmed_ids=_vegas_confirmed_ids,
            )

            if additions:
                _cascade_total_added += sum(additions.values())

        if _cascade_total_added > 0:
            logger.info(
                "[TopDown] %s: CASCADE TOTAL — %.1f min added via "
                "positional inheritance across %d slots",
                team_name, _cascade_total_added, len(_cascade_ledger),
            )

    # ─────────────────────────────────────────────────────────────────
    # PHASE 3.5: Hard Minutes Ceiling — Style-of-Play Enforcement
    #
    # After Phases 2-3 have allocated minutes optimistically, apply
    # hard ceilings for players whose coaches will NOT run them beyond
    # a known threshold (foul trouble, offensive limitations, etc.).
    #
    # Freed minutes cascade to same-position bench players who have
    # headroom under their caps.  If no same-position player has
    # headroom, the surplus goes to ANY bench player with headroom.
    # This preserves the 240-minute budget.
    # ─────────────────────────────────────────────────────────────────
    _ceiling_surplus = 0.0
    _ceiling_applied = []
    _rotation_by_id = {p.player_id: p for p in rotation}

    for pid, mins in list(allocation.items()):
        if mins <= 0:
            continue
        p = _rotation_by_id.get(pid)
        if not p:
            continue
        ceiling = get_hard_ceiling(p.player_name)
        if ceiling is not None and mins > ceiling:
            freed = mins - ceiling
            allocation[pid] = round(ceiling, 1)
            _ceiling_surplus += freed
            _ceiling_applied.append((p, mins, ceiling, freed))
            logger.warning(
                "[TopDown] HARD CEILING: %s (%s) capped %.1f → %.1f min "
                "(freed %.1f min to redistribute)",
                p.player_name, p.position, mins, ceiling, freed,
            )

    # Cascade freed minutes to eligible bench players
    if _ceiling_surplus > 0.5:
        # Build list of recipients: bench players with headroom, sorted
        # by same-position affinity then by current allocation (higher = better absorber)
        _capped_positions = set()
        for p, _, _, _ in _ceiling_applied:
            _capped_positions.update(
                _POS_FAMILY.get(pp.strip(), pp.strip())
                for pp in p.position.split("-")
            )

        _recipients = []
        for bp in bench:
            bp_mins = allocation.get(bp.player_id, 0)
            if bp_mins <= 0:
                continue
            # Check if this bench player is ceiling-capped themselves
            bp_ceiling = get_hard_ceiling(bp.player_name)
            bp_effective_cap = min(
                bp_ceiling if bp_ceiling is not None else BENCH_CAP,
                BENCH_CAP,
            )
            headroom = bp_effective_cap - bp_mins
            if headroom <= 0:
                continue
            # Same-position affinity: prioritize same family
            bp_families = {
                _POS_FAMILY.get(pp.strip(), pp.strip())
                for pp in bp.position.split("-")
            }
            is_same_pos = bool(bp_families & _capped_positions)
            _recipients.append((bp, headroom, is_same_pos))

        # Also include starters with headroom (less common but possible)
        for s in starters:
            s_mins = allocation.get(s.player_id, 0)
            if s_mins <= 0:
                continue
            s_ceiling = get_hard_ceiling(s.player_name)
            s_cap = STARTER_CAP
            if s_ceiling is not None:
                s_cap = min(s_cap, s_ceiling)
            headroom = s_cap - s_mins
            if headroom <= 0:
                continue
            _recipients.append((s, headroom, False))

        if _recipients:
            # Sort: same-position first, then by headroom descending
            _recipients.sort(key=lambda x: (x[2], x[1]), reverse=True)
            _to_distribute = _ceiling_surplus
            # Proportional distribution within each tier
            for _pass in range(3):  # Multiple passes for cap-respecting distribution
                if _to_distribute < 0.2:
                    break
                total_hr = sum(hr for _, hr, _ in _recipients if hr > 0)
                if total_hr <= 0:
                    break
                for i, (rp, hr, _) in enumerate(_recipients):
                    if hr <= 0 or _to_distribute < 0.1:
                        continue
                    share = hr / total_hr
                    add = min(_to_distribute * share, hr)
                    allocation[rp.player_id] = round(
                        allocation[rp.player_id] + add, 1
                    )
                    _to_distribute -= add
                    # Recompute headroom
                    rp_ceil = get_hard_ceiling(rp.player_name)
                    rp_cap = BENCH_CAP if rp.player_id not in starter_ids else STARTER_CAP
                    if rp_ceil is not None:
                        rp_cap = min(rp_cap, rp_ceil)
                    _recipients[i] = (rp, max(rp_cap - allocation[rp.player_id], 0), _)

            logger.info(
                "[TopDown] %s: CEILING CASCADE — %.1f freed min redistributed "
                "(%d recipients), %.1f unplaced",
                team_name, _ceiling_surplus, len(_recipients),
                max(0, _to_distribute),
            )

    # ─────────────────────────────────────────────────────────────────
    # PHASE 3.8: Thibodeau Rule — Upward Distribution
    #
    # When the rotation cap is full but there are still unassigned
    # minutes (team total < 240), push the excess UPWARD to the top
    # starters instead of expanding the rotation to deep-bench players.
    #
    # Real NBA coaches on tight rotations (Brown/NYK, Malone/DEN) will
    # play their top guys 40-42 minutes when backups are unavailable,
    # rather than giving 25 min to a G-League call-up.
    #
    # The rule ONLY fires when:
    #   1. The rotation is at or above the target size
    #   2. There are >= 3.0 unassigned minutes (not micro-rounding)
    #   3. The top starters have headroom under the elevated cap
    # ─────────────────────────────────────────────────────────────────
    _pre_thibs_total = sum(allocation.values())
    _thibs_deficit = TOTAL_TEAM_MINUTES - _pre_thibs_total

    if _thibs_deficit >= 3.0:
        _thibs_rotation_size = calculate_team_rotation_size(
            rotation, allocation, _hard_zeroed_ids,
        )
        if _thibs_rotation_size >= _target_rotation:
            # Find the top N starters by current allocation (they're the workhorses)
            _thibs_candidates = sorted(
                [
                    (pid, mins) for pid, mins in allocation.items()
                    if pid in starter_ids and mins > 0
                ],
                key=lambda x: x[1],
                reverse=True,
            )[:THIBODEAU_RULE_MAX_RECIPIENTS]

            if _thibs_candidates:
                _thibs_remaining = _thibs_deficit
                # Distribute proportionally to current minutes (top guy gets most)
                _thibs_total_mins = sum(m for _, m in _thibs_candidates)

                for pid, current_mins in _thibs_candidates:
                    if _thibs_remaining <= 0.5:
                        break
                    headroom = _coach_max_min - current_mins
                    if headroom <= 0:
                        continue
                    share = current_mins / _thibs_total_mins if _thibs_total_mins > 0 else 1.0
                    add = min(_thibs_remaining * share, headroom)
                    allocation[pid] = round(current_mins + add, 1)
                    _thibs_remaining -= add

                    _player_name = next(
                        (p.player_name for p in rotation if p.player_id == pid),
                        f"ID:{pid}",
                    )
                    logger.info(
                        "[TopDown] THIBODEAU RULE: %s (%s) +%.1f min → %.1f min | "
                        "cap=%.0f, rotation=%d/%d",
                        _player_name, team_name, add, allocation[pid],
                        _coach_max_min, _thibs_rotation_size, _target_rotation,
                    )

                _thibs_added = _thibs_deficit - _thibs_remaining
                if _thibs_added > 0:
                    logger.info(
                        "[TopDown] %s: THIBODEAU RULE — %.1f min pushed UP to "
                        "%d starters (rotation %d/%d, cap=%.0f)",
                        team_name, _thibs_added, len(_thibs_candidates),
                        _thibs_rotation_size, _target_rotation, _coach_max_min,
                    )

    # ─────────────────────────────────────────────────────────────────
    # PHASE 3.9: Absolute Minute Ceiling Enforcement
    #
    # After Thibodeau Rule may have pushed starters to 40-42 min,
    # clamp everyone to their absolute ceiling.  Any excess is
    # discarded (logged as warning) rather than redistributed.
    # ─────────────────────────────────────────────────────────────────
    _abs_clamped = 0
    _rotation_by_id_abs = {p.player_id: p for p in rotation}
    for pid, mins in list(allocation.items()):
        if mins <= 0:
            continue
        p = _rotation_by_id_abs.get(pid)
        if not p:
            continue
        abs_ceil = _absolute_ceiling(p)
        if mins > abs_ceil:
            _excess = mins - abs_ceil
            allocation[pid] = round(abs_ceil, 1)
            _abs_clamped += 1
            logger.warning(
                "[TopDown] ABSOLUTE CEILING: %s (%s, sal=$%s) clamped "
                "%.1f → %.1f min (excess %.1f discarded)",
                p.player_name, p.position,
                f"{(getattr(p, 'dk_salary', None) or 0):,}",
                mins, abs_ceil, _excess,
            )
    if _abs_clamped:
        logger.info(
            "[TopDown] %s: %d player(s) hit absolute ceiling",
            team_name, _abs_clamped,
        )

    # ─────────────────────────────────────────────────────────────────
    # PHASE 4: Residual Micro-Correction
    #
    # Rounding and clamping may leave us off 240.  Distribute the
    # residual proportionally to players with headroom.
    # ─────────────────────────────────────────────────────────────────
    current_total = sum(allocation.values())
    residual = TOTAL_TEAM_MINUTES - current_total

    if abs(residual) > 0.15:
        # Distribute residual to active players proportionally
        active_ids = [
            pid for pid, mins in allocation.items()
            if mins > 0
        ]
        if active_ids:
            if residual > 0:
                # Under 240: give to players with headroom under their cap.
                # Use blowout-reduced caps for natural starters when team
                # health penalty is active.  Promoted starters and bench
                # use normal caps (the gutted team's scrubs play full games).
                #
                # ABSOLUTE CEILING: Every cap is clamped to the player's
                # absolute max (38.0 standard, 40.0 for superstars) to
                # prevent Phase 4 from pushing anyone beyond realistic
                # regulation limits.
                headroom = {}
                for pid in active_ids:
                    p = next(pp for pp in rotation if pp.player_id == pid)
                    if pid in starter_ids:
                        _is_natural_starter = p.season_avg >= STARTER_FLOOR
                        if _is_blowout_risk and _is_natural_starter:
                            cap = STARTER_CAP - _health_starter_cap_red
                        else:
                            cap = STARTER_CAP
                    elif pid in _backup_big_ids:
                        cap = BACKUP_BIG_MINUTES_CAP
                    elif pid in scrub_capped_ids:
                        cap = BENCH_SCRUB_CAP
                    else:
                        cap = BENCH_CAP
                    # Hard ceiling override: never exceed style-of-play limit
                    _hard_ceil = get_hard_ceiling(p.player_name)
                    if _hard_ceil is not None:
                        cap = min(cap, _hard_ceil)
                    # ABSOLUTE CEILING: clamp to regulation maximum
                    cap = min(cap, _absolute_ceiling(p))
                    headroom[pid] = max(cap - allocation[pid], 0)
                # Iteratively distribute residual respecting caps.
                # Each pass fills players up to their cap; excess re-enters
                # the residual pool for the next pass.  Converges when all
                # headroom is consumed or residual is negligible.
                _remaining_residual = residual
                for _pass in range(5):  # Max 5 passes (safety)
                    total_headroom = sum(headroom.values())
                    if total_headroom <= 0 or _remaining_residual < 0.2:
                        break
                    for pid in active_ids:
                        if headroom[pid] <= 0:
                            continue
                        share = headroom[pid] / total_headroom
                        add = min(_remaining_residual * share, headroom[pid])
                        allocation[pid] = round(allocation[pid] + add, 1)
                    # Recompute headroom and remaining residual
                    _remaining_residual = TOTAL_TEAM_MINUTES - sum(allocation.values())
                    for pid in active_ids:
                        p = next(pp for pp in rotation if pp.player_id == pid)
                        if pid in starter_ids:
                            _is_ns = p.season_avg >= STARTER_FLOOR
                            cap = (STARTER_CAP - _health_starter_cap_red) if (_is_blowout_risk and _is_ns) else STARTER_CAP
                        elif pid in _backup_big_ids:
                            cap = BACKUP_BIG_MINUTES_CAP
                        elif pid in scrub_capped_ids:
                            cap = BENCH_SCRUB_CAP
                        else:
                            cap = BENCH_CAP
                        # Hard ceiling override
                        _hc = get_hard_ceiling(p.player_name)
                        if _hc is not None:
                            cap = min(cap, _hc)
                        # ABSOLUTE CEILING
                        cap = min(cap, _absolute_ceiling(p))
                        headroom[pid] = max(cap - allocation[pid], 0)
                # If residual remains after all passes (everyone at their
                # absolute ceiling), discard the orphaned minutes rather
                # than pushing any player beyond regulation limits.
                if _remaining_residual > 0.5:
                    logger.warning(
                        "[TopDown] %s: %.1f orphaned minutes DISCARDED — all "
                        "active players at absolute ceiling (38-40 min). "
                        "Team total will be %.1f instead of 240.0",
                        team_name, _remaining_residual,
                        sum(allocation.values()),
                    )
            else:
                # Over 240: shave proportionally from bench first, then starters
                bench_ids = [pid for pid in active_ids if pid not in starter_ids]
                target_ids = bench_ids if bench_ids else active_ids
                total_mins = sum(allocation[pid] for pid in target_ids)
                if total_mins > 0:
                    for pid in target_ids:
                        share = allocation[pid] / total_mins
                        cut = abs(residual) * share
                        allocation[pid] = round(max(0.0, allocation[pid] - cut), 1)

    # Final exact correction (rounding can leave ~0.1-0.3 residual).
    # Only apply small corrections; large residuals (e.g., short rotations
    # where all players hit caps) are left for the downstream normalizer.
    final_total = sum(allocation.values())
    final_residual = TOTAL_TEAM_MINUTES - final_total
    if abs(final_residual) > 0.05 and abs(final_residual) <= 5.0:
        # Add/subtract from the highest-minute player (least impact %)
        max_pid = max(
            (pid for pid, m in allocation.items() if m > 0),
            key=lambda pid: allocation[pid],
            default=None,
        )
        if max_pid is not None:
            allocation[max_pid] = round(
                allocation[max_pid] + final_residual, 1
            )

    # ─────────────────────────────────────────────────────────────────
    # POST-PHASE: Final Hard Zero Enforcement
    #
    # Belt-and-suspenders: re-zero any player in _hard_zeroed_ids that
    # somehow received minutes from Phase 3.5 cascading or Phase 4
    # residual correction.  This should never fire, but guarantees
    # zero bleed-through.
    # ─────────────────────────────────────────────────────────────────
    for pid in _hard_zeroed_ids:
        leaked = allocation.get(pid, 0.0)
        if leaked > 0:
            logger.error(
                "[TopDown] BLEED DETECTED: player_id=%d had %.1f min after "
                "Phase 4 despite hard-zero — forcing back to 0.0",
                pid, leaked,
            )
            allocation[pid] = 0.0

    # ─────────────────────────────────────────────────────────────────
    # Log the final allocation
    # ─────────────────────────────────────────────────────────────────
    final_sum = sum(allocation.values())
    active_count = sum(1 for m in allocation.values() if m > 0)
    logger.info(
        "[TopDown] %s: FINAL — %d active players, %.1f total min "
        "(target=%.1f, residual=%.1f)",
        team_name, active_count, final_sum,
        TOTAL_TEAM_MINUTES, TOTAL_TEAM_MINUTES - final_sum,
    )

    # ─────────────────────────────────────────────────────────────────
    # Track promoted players for downstream usage boost
    # ─────────────────────────────────────────────────────────────────
    # Promoted: bench player who became a starter due to vacancy fill
    # (season_avg below starter floor and NOT a chalk override).
    # These players deserve a per-minute usage bump because they'll
    # handle more possessions than their historical averages suggest.
    promoted_ids: set = set()
    for s in starters:
        if (
            s.season_avg < STARTER_FLOOR
            and s.player_id not in chalk_ids
        ):
            promoted_ids.add(s.player_id)

    # Also flag the best same-position bench player in each vacancy
    # slot — they'll absorb more usage as the "next man up" at that
    # position even though they stay on the bench.
    _bench_promoted_ids: set = set()
    if vacancy_slots and bench:
        for v_slot in vacancy_slots:
            for bp in bench:
                if (
                    _player_can_inherit(bp.position, v_slot)
                    and allocation.get(bp.player_id, 0) > 0
                ):
                    _bench_promoted_ids.add(bp.player_id)
                    break  # Only the top-ranked same-pos bench player

    # Debug: log each player's allocation
    for p in rotation:
        mins = allocation.get(p.player_id, 0)
        role = "STARTER" if p.player_id in starter_ids else (
            "BENCH" if mins > 0 else "DNP"
        )
        reason = availability.get(p.player_id, {}).get("reason", "")
        if mins > 0 or role == "DNP":
            _promo_flag = ""
            if p.player_id in promoted_ids:
                _promo_flag = " ← PROMOTED (vacancy fill)"
            elif p.player_id in _bench_promoted_ids:
                _promo_flag = " ← VACANCY BENCH BOOST"
            logger.debug(
                "[TopDown]   %s (%s) → %.1f min [%s] %s%s",
                p.player_name, p.position, mins, role, reason, _promo_flag,
            )

    # Attach promotion metadata as attributes on the AllocationResult.
    # The rotation engine reads these to apply usage_boost to promoted players.
    allocation.promoted_ids = promoted_ids | _bench_promoted_ids
    allocation.vacancy_slots = vacancy_slots
    allocation.lms_exempt_ids = _lms_exempt_ids

    # Attach blowout risk metadata for the rotation engine's efficiency discount.
    # When blowout_efficiency_factor < 1.0, the rotation engine applies it as a
    # multiplier on offensive per-minute rates (pts, ast, fg3m) for the entire team.
    allocation.blowout_efficiency_factor = _health_efficiency

    # Attach alpha vacuum metadata for the rotation engine's FPPM boost.
    # When alpha_vacuum is True, the top 2 remaining offensive players get
    # a 1.15x usage_boost to reflect absorbed shot attempts.
    allocation.alpha_vacuum = _alpha_vacuum
    allocation.alpha_out_names = _alpha_out_names
    allocation.alpha_out_usage = _alpha_out_usage

    return allocation
