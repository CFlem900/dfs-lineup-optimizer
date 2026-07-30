import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
import numpy as np
from typing import List, Dict, Optional, Tuple
from app.models.player import PlayerMinutes, PlayerProjection, PlayerStatus
from app.utils.helpers import normalize_player_name
from app.models.rotation import TeamRotation, RedistributionConfig
from app.models.game import GameInfo
from app.models.coach import CoachProfile, get_coach_profile
from app.config.constants import (
    LEAGUE_AVG_PACE,
    EMA_ALPHA_5_GAME,
    EMA_ALPHA_10_GAME,
    BASELINE_SEASON_WEIGHT,
    BASELINE_RECENT_WEIGHT,
    RECENT_EMA5_SPLIT,
    RECENT_EMA10_SPLIT,
    INJURY_PLAY_PROBABILITY,
    INJURY_MINUTES_IF_ACTIVE,
    ROLE_CAP_MULTIPLIER,
    ROLE_CAP_MIN_FLOOR,
    USAGE_BOOST_CAP,
    STARTER_THRESHOLD_MINUTES,
    DEEP_BENCH_THRESHOLD_MINUTES,
    BLOWOUT_SPREAD_THRESHOLD,
    BLOWOUT_PENALTY_PER_POINT,
    BLOWOUT_PENALTY_EXPONENT,
    BLOWOUT_MIN_FACTOR,
    STAR_BLOWOUT_DAMPENING,
    B2B_STARTER_REDUCTION_MINUTES,
    B2B_VETERAN_EXTRA_MINUTES,
    VETERAN_AGE,
    TOTAL_TEAM_MINUTES,
    STAR_ANCHOR_THRESHOLD,
    MIN_VIABLE_MINUTES,
    MAX_INFLATION_CEILING,
    ABSOLUTE_MAX_MINUTES,
    BACKUP_SEASON_WEIGHT,
    BACKUP_RECENT_WEIGHT,
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
    HIGH_USAGE_OUT_THRESHOLD,
    HIGH_USAGE_OUT_RATE_BOOST,
    INJURY_RETURN_DECAY_GAMES,
    INJURY_RETURN_MINUTES_REDUCTION,
    SPOT_START_CAP_FACTOR,
    SPOT_START_CAP_FACTOR_GTD,
    SPOT_START_MIN_INJURED_BASELINE,
    SPOT_START_ABSENCE_THRESHOLD,
    USE_HIERARCHICAL_DAG,
    NORM_LOCK_THRESHOLD,
    NORM_MID_THRESHOLD,
    NORM_MID_MAX_CUT_PCT,
    SHORT_ROTATION_SIZE,
    SHORT_ROTATION_STARTER_CEILING,
    SPARSE_DATA_MIN_RECENT_GAMES,
    USAGE_BOOST_DAMPENING_ONSET,
    USAGE_BOOST_DAMPENING_RATE,
    USAGE_BOOST_DAMPENING_FLOOR,
    DEFENSIVE_ATTENTION_PENALTY,
    DEFENSIVE_ATTENTION_MIN_USAGE_OUT,
    DEFENSIVE_ATTENTION_USAGE_THRESHOLD,
)
from app.services.agents.injury_impact_agent import (
    calculate_hierarchical_redistribution,
)
from app.config.constants import USE_TOP_DOWN_MINUTES
from app.services.top_down_minutes import allocate_team_minutes

logger = logging.getLogger(__name__)


# ── Usage Boost Diminishing Returns ──────────────────────────────────────
# High-FPPM players get smaller marginal boosts because there's a ceiling
# on how efficient any player can be per minute.  A 1.20 FPPM player
# gaining +15% = 1.38 is historically unprecedented for a full game.

def dampened_usage_boost(
    raw_boost: float,
    player_fppm: float,
) -> float:
    """Apply diminishing returns to a usage boost based on baseline FPPM.

    Parameters
    ----------
    raw_boost : float
        The raw usage_boost multiplier (e.g., 1.15 = 15% boost).
    player_fppm : float
        The player's baseline fantasy points per minute.  Estimated as
        ``dk_fppg / season_avg`` or computed from per-minute rates.

    Returns
    -------
    float
        Dampened boost multiplier, always >= 1.0.

    Formula
    -------
    If player_fppm <= DAMPENING_ONSET (0.90): return raw_boost unchanged.
    Otherwise:
        dampening = max(FLOOR, 1.0 - (fppm - ONSET) × RATE)
        dampened  = 1.0 + (raw_boost - 1.0) × dampening

    Examples (with default constants):
        FPPM=0.70, raw=1.15 → dampening=1.0 → 1.15 (no change, low FPPM)
        FPPM=1.00, raw=1.15 → dampening=0.80 → 1.12
        FPPM=1.10, raw=1.15 → dampening=0.60 → 1.09
        FPPM=1.20, raw=1.15 → dampening=0.40 → 1.06
        FPPM=1.30, raw=1.15 → dampening=0.33 → 1.05 (floor)
    """
    if raw_boost <= 1.0 or player_fppm <= USAGE_BOOST_DAMPENING_ONSET:
        return raw_boost

    excess = raw_boost - 1.0
    dampening = max(
        USAGE_BOOST_DAMPENING_FLOOR,
        1.0 - (player_fppm - USAGE_BOOST_DAMPENING_ONSET) * USAGE_BOOST_DAMPENING_RATE,
    )
    return round(1.0 + excess * dampening, 4)


def _estimate_fppm(player) -> float:
    """Estimate a player's baseline DK fantasy points per minute.

    Uses per-minute stat rates × DK scoring weights.  This is an
    approximation (ignores DD/TD bonuses) but is accurate enough
    for dampening calculations.

    DK scoring: PTS×1.0 + REB×1.25 + AST×1.5 + STL×2.0 + BLK×2.0
                - TOV×0.5 + FG3M×0.5
    """
    pts = getattr(player, "pts_per_min", 0) or 0
    reb = getattr(player, "reb_per_min", 0) or 0
    ast = getattr(player, "ast_per_min", 0) or 0
    stl = getattr(player, "stl_per_min", 0) or 0
    blk = getattr(player, "blk_per_min", 0) or 0
    tov = getattr(player, "tov_per_min", 0) or 0
    fg3m = getattr(player, "fg3m_per_min", 0) or 0
    return (
        pts * 1.0 + reb * 1.25 + ast * 1.5
        + stl * 2.0 + blk * 2.0 - tov * 0.5 + fg3m * 0.5
    )


# Re-usable suffix pattern for name normalisation
_SUFFIX_RE = re.compile(r'\b(jr|sr|ii|iii|iv|v)\b')

# ---------------------------------------------------------------------------
# Position-family mapping — normalizes DK-style 5-position codes (PG, SG,
# SF, PF, C) and NBA API simplified codes (G, F, C) to positional families.
# Without this, _positions_overlap("PG", "SG") = False, which breaks
# guard-to-guard and forward-to-forward backup detection.
# ---------------------------------------------------------------------------
_POS_FAMILY: Dict[str, str] = {
    "PG": "G", "SG": "G", "G": "G", "Guard": "G",
    "SF": "F", "PF": "F", "F": "F", "Forward": "F",
    "C": "C", "Center": "C",
}


def _normalize_for_match(name: str) -> str:
    """Normalize a player name for cross-source matching.

    Delegates to the canonical normalize_player_name() which handles
    diacritics (Dončić→doncic), suffixes (Jr./Sr./II/III/IV),
    punctuation (periods, apostrophes, hyphens), and case normalization.
    """
    return normalize_player_name(name)


class RotationEngine:
    def __init__(self, config: RedistributionConfig = None, injury_impact_agent=None, coach_learning_agent=None, calibration_service=None, gleague_service=None):
        self.config = config or RedistributionConfig()
        self._injury_agent = injury_impact_agent   # Agent 3
        self._coach_agent = coach_learning_agent    # Agent 6
        self._calibration = calibration_service     # CalibrationService
        self._gleague = gleague_service             # GLeagueStatsService

    def calculate_ema(self, minutes: List[float], alpha: float = 0.4) -> float:
        """Exponential moving average with proper recency weighting.

        Input ``minutes`` is in reverse chronological order (index 0 =
        most recent game).  We reverse it so the EMA initialises from
        the **oldest** game and walks toward the most recent, giving
        exponentially more weight to recent games — which is the correct
        EMA behavior.
        """
        if not minutes:
            return 0.0
        chronological = list(reversed(minutes))  # oldest → newest
        ema = chronological[0]
        for minute in chronological[1:]:
            ema = alpha * minute + (1 - alpha) * ema
        return ema

    def get_baseline_projection(
        self,
        player: PlayerMinutes,
        recent_weight_override: Optional[float] = None,
    ) -> float:
        """Calculate baseline minutes using weighted blend.

        Base weights (industry-standard, season-anchored):
          - 15% EMA of last 5 games (alpha=0.6 — captures hot streaks)
          - 10% EMA of last 10 games (alpha=0.4 — smoothed trend)
          - 75% Season average (anchor — role changes slowly)

        The heavy season_avg weight matches consensus industry models
        (e.g. NumberFire, Fantasy Labs) which use ~75% season + 25%
        recent.  NBA minutes are highly stable game-to-game for
        established players; over-weighting recent variance causes
        projections to swing 3-5 minutes on a good/bad week.

        ML calibrations can adjust the blend weights via
        ``minutes_blend_season_weight`` and ``minutes_blend_recent_weight``.

        Parameters
        ----------
        recent_weight_override : float, optional
            User-provided recent weight (0.0-0.60).  When set, overrides
            both the base constant and ML calibration blend weights.
            ``season_weight = 1.0 - recent_weight_override``.
        """
        ema_5 = self.calculate_ema(player.minutes_last_5, alpha=EMA_ALPHA_5_GAME)
        ema_10 = self.calculate_ema(player.minutes_last_10, alpha=EMA_ALPHA_10_GAME)

        # Base weights — adjustable via ML calibration or user override
        if recent_weight_override is not None:
            # User override takes precedence over ML calibration
            season_w = 1.0 - recent_weight_override
            recent_w = recent_weight_override
        else:
            season_w = BASELINE_SEASON_WEIGHT
            recent_w = BASELINE_RECENT_WEIGHT

            if self._calibration:
                season_w *= self._calibration.get_minutes_blend_adjustment("season")
                recent_w *= self._calibration.get_minutes_blend_adjustment("recent")

        # Renormalize so weights sum to 1.0
        total_w = season_w + recent_w
        if total_w > 0:
            season_w /= total_w
            recent_w /= total_w

        # Split recent weight proportionally (60/40 between ema_5 and ema_10)
        ema_5_w = recent_w * RECENT_EMA5_SPLIT
        ema_10_w = recent_w * RECENT_EMA10_SPLIT

        # ── Sparse data heuristic ─────────────────────────────────────
        # When recent game logs are empty/sparse, calculate_ema([])
        # returns 0.0 which drags baseline down by up to 50%.
        # Fix: detect sparse data and shift EMA weight to season_avg
        # proportionally to the data gap.
        _n5 = len(player.minutes_last_5) if player.minutes_last_5 else 0
        _n10 = len(player.minutes_last_10) if player.minutes_last_10 else 0
        _n_recent = _n5 + _n10
        if _n_recent < SPARSE_DATA_MIN_RECENT_GAMES and player.season_avg > 0:
            _data_frac = _n_recent / SPARSE_DATA_MIN_RECENT_GAMES
            _shifted = (ema_5_w + ema_10_w) * (1.0 - _data_frac)
            ema_5_w *= _data_frac
            ema_10_w *= _data_frac
            season_w += _shifted
            logger.info(
                "Sparse data heuristic: %s (id=%d) has %d recent game(s) "
                "(last5=%d, last10=%d), shifted %.0f%% of EMA weight to "
                "season_avg (%.1f min)",
                player.player_name, player.player_id,
                _n_recent, _n5, _n10,
                (1.0 - _data_frac) * 100, player.season_avg,
            )

        baseline = (ema_5_w * ema_5) + (ema_10_w * ema_10) + (season_w * player.season_avg)

        # DNP streak soft decay: players with recent consecutive DNPs get a
        # multiplicative reduction.  The auto-Out threshold (Step 1b) handles
        # streaks >= 3, but 2-DNP players should also get dampened to reduce
        # phantom projections from stale season averages.
        # NOTE: minutes_last_5 excludes 0-min games (filtered at data source),
        # so we MUST use recent_dnp_streak as the decay signal.
        #
        # Threshold is 2+ (not 1+) because a single DNP is a normal rest
        # day in the NBA — players sit out B2B legs, get coach's rest, etc.
        # Applying 0.75× for one rest day incorrectly drops a 27-min player
        # to ~20 min baseline.  The injury report + auto-out system handles
        # genuinely inactive players.
        #
        # DK salary override: DK pricing is the most authoritative signal
        # about whether a player will play.  If DK prices someone at
        # $3,200+ despite consecutive DNPs, they have intel that the
        # player is expected to return.  In our "if plays" model, we
        # project what happens IF the player plays (full production),
        # and manage the risk of not playing via play_probability and
        # exposure caps.  NO decay for high-salary players — the auto-out
        # system handles the "definitely inactive" case.
        #
        # Without this: a $5,600 player with 5 DNPs gets 0.85× decay
        # PLUS 240-min normalization bench shaving = double compression
        # that under-projects by 20+ FP.
        dnp_streak = getattr(player, "recent_dnp_streak", 0)
        if dnp_streak >= 2 and player.season_avg > 10.0:
            _dk_sal = getattr(player, "dk_salary", None)
            if _dk_sal and _dk_sal >= 3200:
                # DK expects this player to play — no decay.
                # "If plays" model: project full minutes, handle risk
                # via play_probability / exposure caps.
                pass
            else:
                decay = max(0.15, 0.75 ** dnp_streak)
                baseline *= decay

        # Apply learned position-bias calibration (auto from backtesting/tournament)
        if self._calibration:
            pos_adj = self._calibration.get_position_bias(player.position)
            if pos_adj != 1.0:
                baseline *= pos_adj

        return round(baseline, 1)

    @staticmethod
    def _positions_overlap(pos_a: str, pos_b: str) -> bool:
        """Check if two NBA position strings share any positional family.

        Normalizes DK-style 5-position codes (PG, SG, SF, PF, C) and
        NBA API simplified codes (G, F, C) to positional families before
        comparing.  This ensures:
          - "PG" overlaps with "SG" (both guards)
          - "SF" overlaps with "PF" (both forwards)
          - "G"  overlaps with "PG" (mixed format)
          - "G-F" overlaps with "PG" (multi-position guard/forward)
          - "C"  does NOT overlap with "PG"
        """
        parts_a = {_POS_FAMILY.get(p.strip(), p.strip()) for p in pos_a.split("-")}
        parts_b = {_POS_FAMILY.get(p.strip(), p.strip()) for p in pos_b.split("-")}
        return bool(parts_a & parts_b)

    def identify_backup_hierarchy(
        self,
        rotation: List[PlayerMinutes],
        injured_player: PlayerMinutes,
    ) -> List[Tuple[PlayerMinutes, float]]:
        """Build a weighted list of backups who absorb freed minutes.

        The primary backup (highest season avg at the same position)
        receives ``primary_backup_share + star_boost`` of the freed
        minutes.  Remaining same-position players split ``rotation_share``
        evenly.  This ensures 100% of freed minutes are redistributed:

            primary_backup_share (0.60) + star_boost (0.05)
            + rotation_share (0.35) = 1.00
        """
        same_position = [
            p
            for p in rotation
            if self._positions_overlap(p.position, injured_player.position)
            and p.player_id != injured_player.player_id
        ]
        # Sort by blended score of season average and recent form.
        # A player surging in recent minutes should rank above one
        # coasting on a higher season average.
        def _backup_sort_key(p: PlayerMinutes) -> float:
            recent_avg = (
                sum(p.minutes_last_5) / len(p.minutes_last_5)
                if p.minutes_last_5 else p.season_avg
            )
            return BACKUP_SEASON_WEIGHT * p.season_avg + BACKUP_RECENT_WEIGHT * recent_avg

        same_position.sort(key=_backup_sort_key, reverse=True)

        if not same_position:
            return []

        hierarchy = []
        # Primary backup gets the star_boost on top of their base share
        primary_share = self.config.primary_backup_share + self.config.star_boost
        hierarchy.append((same_position[0], primary_share))

        if len(same_position) > 1:
            remaining_weight = self.config.rotation_share / (len(same_position) - 1)
            for player in same_position[1:]:
                hierarchy.append((player, remaining_weight))

        return hierarchy

    # Conditional minutes model by injury status.
    #
    # Instead of a flat multiplier, we model expected minutes as:
    # DFS "if-plays" model:
    #   minute_factor = E[min | plays]  (only the active-game reduction)
    #   play_probability = P(plays)     (stored separately for scoring/exposure)
    #
    # Historical NBA injury data (2018-2024 seasons):
    #   - Out:          P(plays) = 0.00, min_if_active = 0.00 → 0 min
    #   - Doubtful:     P(plays) = 0.20, min_if_active = 0.75 → 75% if plays
    #   - GTD:          P(plays) = 0.72, min_if_active = 0.75 → 75% if plays
    #   - Questionable: P(plays) = 0.85, min_if_active = 0.75 → 75% if plays
    #
    # Old model multiplied P × E[min|plays] = 0.81 for Questionable,
    # producing ~28 min for a 35-min player.  New model projects 33 min
    # (if he plays) and uses P(plays)=0.85 for exposure/confidence only.
    _INJURY_PLAY_PROBABILITY = INJURY_PLAY_PROBABILITY
    _INJURY_MINUTES_IF_ACTIVE = INJURY_MINUTES_IF_ACTIVE

    @classmethod
    def _injury_minute_factor(
        cls,
        status: str,
        play_prob: dict = None,
        min_if_active_map: dict = None,
    ) -> Tuple[Optional[float], Optional[float]]:
        """Compute DFS-appropriate injury factors.

        Returns a **(minute_factor, play_probability)** tuple:

        - ``minute_factor`` = ``E[min | plays]`` — the "if he plays"
          reduction applied to the minute projection.  For DFS, we do NOT
          multiply by P(plays) because the scenario where the player sits
          is handled by late swap / exposure caps, not by reducing the
          projection.
        - ``play_probability`` = ``P(plays)`` — stored separately for
          downstream use in composite scoring penalties and auto-exposure
          caps.

        Old model (removed): ``factor = P(plays) × E[min|plays]`` —
        penalised minutes *and* confidence doubly, producing projections
        ~19% too low for Questionable players (e.g. LeBron 28 min vs
        real 33 min).

        Parameters ``play_prob`` / ``min_if_active_map`` allow sport-specific
        override tables (CBB vs NBA).  When *None*, the NBA class defaults
        are used.
        """
        _play_prob = play_prob or cls._INJURY_PLAY_PROBABILITY
        _min_active = min_if_active_map or cls._INJURY_MINUTES_IF_ACTIVE
        p_play = _play_prob.get(status)
        if p_play is None:
            return None, None
        m_active = _min_active.get(status, 0.0)
        return round(m_active, 4), round(p_play, 4)

    def redistribute_minutes(
        self,
        rotation: List[PlayerMinutes],
        injured_players: List[PlayerStatus],
        baseline_projections: Dict[int, float],
        play_prob: dict = None,
        min_if_active_map: dict = None,
    ) -> Dict[int, PlayerProjection]:
        # Build a quick lookup for roster_change_detected flag
        _roster_change_ids = {
            p.player_id for p in rotation
            if getattr(p, "roster_change_detected", False)
        }

        adjusted = {
            pid: PlayerProjection(
                player_id=pid,
                player_name=next(
                    p.player_name for p in rotation if p.player_id == pid
                ),
                position=next(p.position for p in rotation if p.player_id == pid),
                baseline_minutes=baseline,
                adjusted_minutes=baseline,
                confidence=1.0,
                reason="Baseline projection",
                roster_change_detected=(pid in _roster_change_ids),
            )
            for pid, baseline in baseline_projections.items()
        }

        # -----------------------------------------------------------------
        # Phase 1: Apply minute reductions for ALL injury statuses.
        #
        # DFS "if-plays" model (March 2026):
        #   Minute projection uses ONLY E[min|plays] — the reduction
        #   when the player IS active (e.g. Questionable → 0.95× baseline).
        #   P(plays) is stored separately as play_probability and used
        #   downstream for:
        #     - Composite score penalty (rotation_confidence)
        #     - Auto-exposure caps (< 0.7 → max 15% exposure)
        #   This prevents double-penalising: old model applied
        #   P(plays) × E[min|plays] = 0.81× for Questionable, producing
        #   28 min projections for 35-min players.
        #
        # "Out"         → 0 min (P=0.00, full redistribution)
        # "Doubtful"    → 0.75× min (P=0.20, very likely sits)
        # "GTD"         → 0.75× min (P=0.72)
        # "Questionable"→ 0.75× min (P=0.85)
        #
        # The freed minutes (baseline × (1 - min_if_active)) from each
        # status level are redistributed to backups.
        # -----------------------------------------------------------------
        injured_ids = set()  # Players with status == "Out" (for full redistribution)
        reduced_players = {}  # player_id → minutes freed (for GTD/Doubtful/Questionable)

        for ip in injured_players:
            minute_factor, p_play = self._injury_minute_factor(
                ip.status, play_prob=play_prob,
                min_if_active_map=min_if_active_map,
            )
            if minute_factor is None:
                continue  # Unknown status, skip
            if ip.player_id not in baseline_projections:
                continue

            # ── Agent 9: Apply team-specific injury offset ──────────
            # If the CalibrationService has learned that this team's
            # players with this status historically play more/fewer
            # minutes than the static factor predicts, adjust the
            # factor accordingly (capped at ±15%).
            offset_tag = ""
            if minute_factor > 0.0 and self._calibration is not None:
                # Resolve team_id — try PlayerStatus first (if extended),
                # then fall back to the rotation roster entry.
                _team_id = getattr(ip, "team_id", None)
                if _team_id is None:
                    _rot_player = next(
                        (p for p in rotation if p.player_id == ip.player_id), None
                    )
                    if _rot_player is not None:
                        _team_id = getattr(_rot_player, "team_id", None)

                if _team_id is not None:
                    offset = self._calibration.get_team_injury_offset(
                        _team_id, ip.status,
                    )
                    if offset != 1.0:
                        original_factor = minute_factor
                        minute_factor = round(min(1.0, minute_factor * offset), 4)
                        offset_tag = f" [team offset ×{offset:.2f}]"
                        logger.debug(
                            "Agent9 offset: %s (%s) team=%s factor %.4f → %.4f",
                            ip.player_name, ip.status,
                            _team_id, original_factor, minute_factor,
                        )

            baseline = baseline_projections[ip.player_id]

            if p_play == 0.0:
                # Out — zero minutes, full redistribution
                injured_ids.add(ip.player_id)
                adjusted[ip.player_id].adjusted_minutes = 0.0
                adjusted[ip.player_id].reason = "Out - Injured"
                adjusted[ip.player_id].confidence = 0.0
                adjusted[ip.player_id].play_probability = 0.0
                reduced_players[ip.player_id] = baseline
            else:
                # GTD / Doubtful / Questionable — "if plays" reduction
                # minute_factor = E[min|plays] (e.g. 0.75 for Questionable)
                # p_play = P(plays) (e.g. 0.85 for Questionable)
                #
                # Minutes use only minute_factor (the "if plays" projection).
                # Confidence uses p_play (risk of not playing at all).
                new_minutes = round(baseline * minute_factor, 1)
                freed_minutes = baseline - new_minutes
                adjusted[ip.player_id].adjusted_minutes = new_minutes
                adjusted[ip.player_id].reason = (
                    f"{ip.status} - {minute_factor:.0%} if active "
                    f"({new_minutes:.1f} min, P(plays)={p_play:.0%})"
                    f"{offset_tag}"
                )
                adjusted[ip.player_id].confidence = p_play
                adjusted[ip.player_id].play_probability = p_play
                reduced_players[ip.player_id] = freed_minutes

        total_freed_minutes = sum(reduced_players.values())

        if total_freed_minutes == 0:
            # No injuries — pass through raw baselines without
            # normalization; the pipeline normalizes once at the end.
            return adjusted

        # -----------------------------------------------------------------
        # Phase 1b: Detect spot-start promotions.
        #
        # When a STARTER (baseline >= 24 min) is Out or Doubtful
        # (injury factor <= 0.50), the primary backup in the hierarchy
        # receives an elevated role cap based on the injured player's
        # baseline, not their own.  This prevents deep-bench players
        # who inherit a full starting role from being capped at 20 min.
        #
        # The registry maps backup_player_id → elevated_cap for use
        # in Phase 2's role-cap calculation.
        # -----------------------------------------------------------------
        spot_start_caps: Dict[int, float] = {}
        all_reduced_ids = set(reduced_players.keys())

        for reduced_id, minutes_freed in reduced_players.items():
            if minutes_freed <= 0:
                continue

            injured_baseline = baseline_projections.get(reduced_id, 0)
            if injured_baseline < SPOT_START_MIN_INJURED_BASELINE:
                continue  # Not a starter — no spot-start promotion

            # Look up the injury status/factor for this player
            ip_status = next(
                (ip.status for ip in injured_players
                 if ip.player_id == reduced_id),
                None,
            )
            if ip_status is None:
                continue
            _ss_min_factor, _ss_p_play = self._injury_minute_factor(
                ip_status, play_prob=play_prob,
                min_if_active_map=min_if_active_map,
            )
            if _ss_p_play is None or _ss_p_play > SPOT_START_ABSENCE_THRESHOLD:
                continue  # GTD / Questionable — no promotion

            # Find the injured player object for hierarchy lookup
            reduced_player = next(
                (p for p in rotation if p.player_id == reduced_id), None
            )
            if not reduced_player:
                continue

            backup_hierarchy = self.identify_backup_hierarchy(
                rotation, reduced_player
            )
            if not backup_hierarchy:
                continue

            # Find first healthy backup in the hierarchy
            primary_backup = None
            for bp, _w in backup_hierarchy:
                if bp.player_id not in all_reduced_ids:
                    primary_backup = bp
                    break
            if primary_backup is None:
                continue  # All backups also injured

            # Compute the elevated cap
            if _ss_p_play == 0.0:
                # Out — full spot-start cap
                elevated_cap = injured_baseline * SPOT_START_CAP_FACTOR
            else:
                # Doubtful — blend between normal cap and full spot-start
                normal_cap = max(
                    baseline_projections.get(primary_backup.player_id, 0)
                    * ROLE_CAP_MULTIPLIER,
                    ROLE_CAP_MIN_FLOOR,
                )
                full_spot_cap = injured_baseline * SPOT_START_CAP_FACTOR
                elevated_cap = (
                    normal_cap
                    + SPOT_START_CAP_FACTOR_GTD * (full_spot_cap - normal_cap)
                )

            # Only elevate if it actually raises the cap
            existing_cap = spot_start_caps.get(primary_backup.player_id, 0)
            if elevated_cap > existing_cap:
                spot_start_caps[primary_backup.player_id] = round(
                    elevated_cap, 1
                )
                logger.info(
                    "Spot-start promotion: %s inherits starting role from "
                    "%s (%s, %.1f min baseline) → elevated cap %.1f min",
                    primary_backup.player_name,
                    reduced_player.player_name,
                    ip_status,
                    injured_baseline,
                    elevated_cap,
                )
                # Tag the projection for downstream normalization protection
                proj = adjusted.get(primary_backup.player_id)
                if proj:
                    proj.is_spot_starter = True

        # -----------------------------------------------------------------
        # Phase 2: Redistribute freed minutes to backups.
        #
        # When USE_HIERARCHICAL_DAG is True, minutes flow through a
        # position-specific DAG (e.g. PG → backup PG 70% + SG 30%)
        # with overflow spill and role-based ceiling caps (38/28 min).
        #
        # When False, the legacy flat 65/35 waterfall is used as fallback.
        # -----------------------------------------------------------------
        if USE_HIERARCHICAL_DAG:
            adjusted = calculate_hierarchical_redistribution(
                rotation=rotation,
                reduced_players=reduced_players,
                baseline_projections=baseline_projections,
                adjusted=adjusted,
                spot_start_caps=spot_start_caps,
                all_reduced_ids=all_reduced_ids,
            )
        else:
            for reduced_id, minutes_to_distribute in reduced_players.items():
                if minutes_to_distribute <= 0:
                    continue

                reduced_player = next(
                    (p for p in rotation if p.player_id == reduced_id), None
                )
                if not reduced_player:
                    continue

                backup_hierarchy = self.identify_backup_hierarchy(
                    rotation, reduced_player
                )

                for backup_player, weight in backup_hierarchy:
                    if backup_player.player_id in all_reduced_ids:
                        continue

                    additional_minutes = minutes_to_distribute * weight
                    current = adjusted[backup_player.player_id].adjusted_minutes

                    player_baseline = baseline_projections.get(
                        backup_player.player_id, current
                    )
                    standard_cap = max(
                        player_baseline * ROLE_CAP_MULTIPLIER,
                        ROLE_CAP_MIN_FLOOR,
                    )
                    if backup_player.player_id in spot_start_caps:
                        effective_cap = max(
                            standard_cap,
                            spot_start_caps[backup_player.player_id],
                        )
                    else:
                        effective_cap = standard_cap
                    role_cap = min(self.config.max_minutes_cap, effective_cap)

                    new_minutes = min(
                        current + additional_minutes,
                        role_cap,
                    )

                    adjusted[backup_player.player_id].adjusted_minutes = round(
                        new_minutes, 1
                    )
                    adjusted[backup_player.player_id].reason = (
                        f"Received {round(new_minutes - current, 1)} min "
                        f"from {reduced_player.player_name}"
                    )

        # -----------------------------------------------------------------
        # Phase 2b: Usage rate boost for injury beneficiaries.
        #
        # When a high-usage player is out, the remaining players don't
        # just get more minutes — they get more touches per minute
        # (higher usage rate → higher per-minute stat production).
        #
        # Model: freed_usage is redistributed to same-position players
        # in proportion to their existing usage_rate.  The boost is
        # capped at 15% to avoid over-projecting.
        # -----------------------------------------------------------------
        for reduced_id, freed_min in reduced_players.items():
            reduced_player = next(
                (p for p in rotation if p.player_id == reduced_id), None
            )
            if not reduced_player:
                continue

            # Only boost when a significant-usage player is out
            injured_usage = reduced_player.usage_rate
            if injured_usage < 0.10:
                continue  # Low-usage players don't create meaningful redistribution

            # Freed usage ≈ injured_usage × (freed_minutes / baseline)
            baseline = baseline_projections.get(reduced_id, 0)
            if baseline <= 0:
                continue
            freed_fraction = freed_min / baseline
            freed_usage = injured_usage * freed_fraction

            # Find same-position active beneficiaries (using overlap matching
            # to handle multi-position players like "G", "F-C", etc.)
            beneficiaries = [
                p for p in rotation
                if p.player_id not in all_reduced_ids
                and self._positions_overlap(p.position, reduced_player.position)
                and p.usage_rate > 0
            ]
            if not beneficiaries:
                continue

            total_benef_usage = sum(b.usage_rate for b in beneficiaries)
            if total_benef_usage <= 0:
                continue

            for b in beneficiaries:
                # Each beneficiary gets freed usage in proportion to their
                # existing share of team usage at the position.
                share = b.usage_rate / total_benef_usage
                added_usage = freed_usage * share
                new_usage = b.usage_rate + added_usage
                # Per-minute rate boost = new_usage / old_usage, capped
                boost = min(new_usage / b.usage_rate, USAGE_BOOST_CAP)
                # Diminishing returns: high-FPPM players get dampened
                boost = dampened_usage_boost(boost, _estimate_fppm(b))
                proj = adjusted.get(b.player_id)
                if proj:
                    proj.usage_boost = round(boost, 3)

        # -----------------------------------------------------------------
        # Phase 2c: High-usage Out player → targeted rate boost.
        #
        # When a star with projected usage > 25% is ruled Out, the
        # primary backup and the secondary star (highest-baseline
        # active same-position player who is NOT the primary backup)
        # each receive a flat 1.10× per-minute rate multiplier.
        #
        # This is on top of Phase 2b's proportional redistribution
        # and captures the "alpha consolidation" effect: with fewer
        # mouths to feed, the top remaining options produce at a
        # higher per-minute rate than their season average suggests.
        # -----------------------------------------------------------------
        for reduced_id, freed_min in reduced_players.items():
            # Only apply for Out players (factor == 0.0 → full baseline freed)
            reduced_player = next(
                (p for p in rotation if p.player_id == reduced_id), None
            )
            if not reduced_player:
                continue

            # Check if Out (freed_min == full baseline) and high-usage
            baseline_val = baseline_projections.get(reduced_id, 0)
            if baseline_val <= 0:
                continue
            if freed_min < baseline_val * 0.99:
                continue  # Not fully Out — skip (GTD/Doubtful get Phase 2b only)
            if reduced_player.usage_rate < HIGH_USAGE_OUT_THRESHOLD:
                continue  # Not a high-usage player

            # Identify primary backup (first in hierarchy)
            backup_hierarchy = self.identify_backup_hierarchy(rotation, reduced_player)
            backup_hierarchy = [
                (bp, w) for bp, w in backup_hierarchy
                if bp.player_id not in all_reduced_ids
            ]
            if not backup_hierarchy:
                continue

            primary_backup = backup_hierarchy[0][0]

            # Secondary star: highest-baseline active same-position player
            # that is NOT the primary backup and NOT reduced.
            secondary_star = None
            _secondary_best_baseline = 0.0
            for p in rotation:
                if p.player_id == primary_backup.player_id:
                    continue
                if p.player_id in all_reduced_ids:
                    continue
                if not self._positions_overlap(p.position, reduced_player.position):
                    continue
                p_bl = baseline_projections.get(p.player_id, 0)
                if p_bl > _secondary_best_baseline:
                    _secondary_best_baseline = p_bl
                    secondary_star = p

            # Apply the flat rate boost to primary backup (with dampening)
            proj_primary = adjusted.get(primary_backup.player_id)
            if proj_primary:
                existing_boost = getattr(proj_primary, "usage_boost", 1.0)
                _dampened_pb = dampened_usage_boost(
                    HIGH_USAGE_OUT_RATE_BOOST,
                    _estimate_fppm(primary_backup),
                )
                proj_primary.usage_boost = round(
                    max(existing_boost, _dampened_pb), 3
                )
                logger.info(
                    "Usage-Rate Scaling: %s (%.0f%% usage) Out → "
                    "primary backup %s rate boost %.3f",
                    reduced_player.player_name,
                    reduced_player.usage_rate * 100,
                    primary_backup.player_name,
                    proj_primary.usage_boost,
                )

            # Apply the flat rate boost to secondary star (with dampening)
            if secondary_star:
                proj_secondary = adjusted.get(secondary_star.player_id)
                if proj_secondary:
                    existing_boost = getattr(proj_secondary, "usage_boost", 1.0)
                    _dampened_ss = dampened_usage_boost(
                        HIGH_USAGE_OUT_RATE_BOOST,
                        _estimate_fppm(secondary_star),
                    )
                    proj_secondary.usage_boost = round(
                        max(existing_boost, _dampened_ss), 3
                    )
                    logger.info(
                        "Usage-Rate Scaling: %s Out → "
                        "secondary star %s rate boost %.3f",
                        reduced_player.player_name,
                        secondary_star.player_name,
                        proj_secondary.usage_boost,
                    )

        # ── AI: Cascading injury impact (Agent 3) ────────────────
        # Apply second-order effects (usage changes, pace impact, role shifts).
        # Triggered for ALL reduced players (Out, Doubtful, GTD, Questionable),
        # not just "Out".  Cascading effects are scaled by absence_weight
        # (probability of the player actually sitting).
        if self._injury_agent and all_reduced_ids:
            try:
                if self._injury_agent.is_available:
                    # Build all injury analysis requests upfront
                    _injury_requests = []
                    for reduced_id in all_reduced_ids:
                        reduced_player = next(
                            (p for p in rotation if p.player_id == reduced_id), None
                        )
                        if not reduced_player:
                            continue

                        _ai_min_factor, _ai_p_play = self._injury_minute_factor(
                            next(
                                (ip.status for ip in injured_players
                                 if ip.player_id == reduced_id),
                                None,
                            ),
                            play_prob=play_prob,
                            min_if_active_map=min_if_active_map,
                        )
                        absence_weight = 1.0 - (_ai_p_play if _ai_p_play is not None else 0.0)
                        if absence_weight < 0.15:
                            continue

                        team_context = {
                            "team_name": "",
                            "injured_player_minutes": baseline_projections.get(reduced_id, 0),
                            "absence_probability": round(absence_weight, 3),
                        }
                        rotation_dicts = [
                            {"player_id": p.player_id, "player_name": p.player_name,
                             "position": p.position,
                             "projected_minutes": adjusted[p.player_id].adjusted_minutes
                             if p.player_id in adjusted else 0}
                            for p in rotation if p.player_id not in all_reduced_ids
                        ]
                        _injury_requests.append({
                            "reduced_id": reduced_id,
                            "absence_weight": absence_weight,
                            "reduced_player": reduced_player,
                            "injured_player": {
                                "player_id": reduced_id,
                                "player_name": reduced_player.player_name,
                                "position": reduced_player.position,
                                "minutes": baseline_projections.get(reduced_id, 0),
                            },
                            "team_rotation": rotation_dicts,
                            "team_context": team_context,
                        })

                    # Run all injury analyses in parallel (max 3 concurrent)
                    def _analyze_one(req):
                        return (req, self._injury_agent.analyze_injury_impact(
                            injured_player=req["injured_player"],
                            team_rotation=req["team_rotation"],
                            team_context=req["team_context"],
                        ))

                    if _injury_requests:
                        _workers = min(3, len(_injury_requests))
                        with ThreadPoolExecutor(max_workers=_workers) as inj_pool:
                            futures = {inj_pool.submit(_analyze_one, r): r for r in _injury_requests}
                            results = []
                            for f in as_completed(futures):
                                try:
                                    results.append(f.result())
                                except Exception as e:
                                    logger.debug(f"[AI] Injury impact call failed: {e}")

                        # Apply cascading effects in original order
                        order = {r["reduced_id"]: i for i, r in enumerate(_injury_requests)}
                        results.sort(key=lambda x: order.get(x[0]["reduced_id"], 999))

                        for req, impact in results:
                            absence_weight = req["absence_weight"]
                            reduced_player = req["reduced_player"]
                            if impact and impact.cascading_effects:
                                for effect in impact.cascading_effects:
                                    for pid, proj in adjusted.items():
                                        if proj.player_name.lower() == effect.player_name.lower():
                                            if effect.minutes_delta:
                                                scaled_delta = effect.minutes_delta * absence_weight
                                                new_min = proj.adjusted_minutes + scaled_delta
                                                proj.adjusted_minutes = round(
                                                    max(0, min(new_min, self.config.max_minutes_cap)), 1
                                                )
                                            if effect.reasoning:
                                                proj.reason += f" | AI: {effect.reasoning}"
                                            break
                                logger.info(
                                    f"[AI] Injury cascade: {len(impact.cascading_effects)} effects "
                                    f"for {reduced_player.player_name} "
                                    f"(absence_weight={absence_weight:.2f})"
                                )
            except Exception as exc:
                logger.debug(f"[AI] Injury impact agent failed: {exc}")

        return adjusted

    def _match_injuries_to_roster(
        self,
        rotation: List[PlayerMinutes],
        injuries: List[PlayerStatus],
        all_injuries: Optional[List[PlayerStatus]] = None,
    ) -> List[PlayerStatus]:
        """Match injury reports to the team's rotation.

        Performs two passes:

        1. **Team-filtered pass** — matches the team-specific injury
           list against the rotation by last name (fast, handles 95%
           of cases).

        2. **Full-list pass** — for any rotation player NOT matched in
           pass 1, checks the full (unfiltered) injury list.  This
           catches mid-season trades where a player's injury-source
           team differs from their NBA-API roster team (e.g. Jimmy
           Butler listed under Golden State in injury reports but
           still showing Miami Heat game logs).

        The full-list pass uses a stricter **first + last name** match
        to avoid false positives when players on different teams share
        a last name (e.g. multiple "Williams" across the league).
        """
        matched_injuries = []
        matched_player_ids = set()

        # Statuses that warrant minute adjustments
        _ACTIONABLE_STATUSES = {"Out", "Doubtful", "GTD", "Game Time Decision", "Questionable", "Probable", "IR", "Injured Reserve"}

        # --- Pass 1: Team-filtered injuries (last-name match) ---
        for injury in injuries:
            if injury.status not in _ACTIONABLE_STATUSES:
                continue

            for player in rotation:
                inj_norm = _normalize_for_match(injury.player_name)
                plr_norm = _normalize_for_match(player.player_name)
                injury_last_name = inj_norm.split()[-1] if inj_norm else ""
                player_last_name = plr_norm.split()[-1] if plr_norm else ""

                if injury_last_name and injury_last_name == player_last_name:
                    matched_injuries.append(
                        PlayerStatus(
                            player_id=player.player_id,
                            player_name=player.player_name,
                            status=injury.status,
                            injury_description=injury.injury_description,
                            last_updated=injury.last_updated,
                        )
                    )
                    matched_player_ids.add(player.player_id)
                    break

        # --- Pass 2: Full injury list for unmatched players ---
        # Catches trades / team mismatches in the injury source.
        if all_injuries:
            unmatched = [
                p for p in rotation
                if p.player_id not in matched_player_ids
            ]
            for player in unmatched:
                plr_norm = _normalize_for_match(player.player_name)
                player_parts = plr_norm.split()
                if len(player_parts) < 2:
                    continue
                player_first = player_parts[0]
                player_last = player_parts[-1]

                for injury in all_injuries:
                    if injury.status not in _ACTIONABLE_STATUSES:
                        continue

                    inj_norm = _normalize_for_match(injury.player_name)
                    inj_parts = inj_norm.split()
                    if len(inj_parts) < 2:
                        continue
                    inj_first = inj_parts[0]
                    inj_last = inj_parts[-1]

                    # Require both first AND last name match to avoid
                    # false positives with common surnames
                    if inj_last == player_last and inj_first == player_first:
                        matched_injuries.append(
                            PlayerStatus(
                                player_id=player.player_id,
                                player_name=player.player_name,
                                status=injury.status,
                                injury_description=injury.injury_description,
                                last_updated=injury.last_updated,
                            )
                        )
                        matched_player_ids.add(player.player_id)
                        logger.info(
                            f"Cross-team injury match: {player.player_name} "
                            f"found in full injury list as '{injury.player_name}' "
                            f"({injury.status})"
                        )
                        break

        return matched_injuries

    def apply_coach_adjustments(
        self,
        projections: Dict[int, PlayerProjection],
        rotation: List[PlayerMinutes],
        coach_profile: CoachProfile,
    ) -> Dict[int, PlayerProjection]:
        """Apply coach-specific minute multipliers.

        Classification uses a minutes-threshold approach rather than
        strict positional ranking to avoid edge cases where a clear
        starter (e.g. KAT at 28 min) gets misclassified as bench
        just because they rank 6th after normalization.

        Thresholds:
          - Star:    top-2 by minutes AND baseline >= 28 min
          - Starter: baseline >= STARTER_THRESHOLD (24 min) — matches
                     the league-average starter floor
          - Bench:   everyone else

        A "deep bench" player (< 15 min) receives the full bench
        multiplier, while a "rotation" player (15-24 min) receives
        a softened multiplier halfway between bench and 1.0.  This
        prevents harsh bench penalties on solid rotation players.
        """
        active_projections = [
            p for p in projections.values() if p.adjusted_minutes > 0
        ]
        sorted_by_minutes = sorted(
            active_projections, key=lambda x: x.adjusted_minutes, reverse=True
        )

        # Stars: top-2 by projected minutes, but only if they're
        # getting meaningful starter-level minutes
        star_ids = set()
        for p in sorted_by_minutes[:2]:
            if p.adjusted_minutes >= STARTER_THRESHOLD_MINUTES:
                star_ids.add(p.player_id)

        # Starters: anyone at or above the starter threshold
        # (but not already a star)
        starter_ids = set()
        for p in sorted_by_minutes:
            if (
                p.player_id not in star_ids
                and p.adjusted_minutes >= STARTER_THRESHOLD_MINUTES
            ):
                starter_ids.add(p.player_id)

        for player_id, proj in projections.items():
            base_minutes = proj.adjusted_minutes

            if player_id in star_ids:
                proj.adjusted_minutes *= coach_profile.star_multiplier
            elif player_id in starter_ids:
                proj.adjusted_minutes *= coach_profile.starter_multiplier
            else:
                # Soften bench multiplier for rotation players (15-24 min)
                # to avoid over-penalizing solid contributors
                if base_minutes >= DEEP_BENCH_THRESHOLD_MINUTES:
                    softened = (coach_profile.bench_multiplier + 1.0) / 2
                    proj.adjusted_minutes *= softened
                else:
                    proj.adjusted_minutes *= coach_profile.bench_multiplier

            proj.adjusted_minutes = min(
                proj.adjusted_minutes, coach_profile.max_minutes_override
            )

            if proj.adjusted_minutes != base_minutes and base_minutes > 0:
                multiplier = proj.adjusted_minutes / base_minutes
                proj.reason = f"{proj.reason} | Coach adj: {multiplier:.2f}x"

        # No normalization here — the pipeline normalizes once at
        # the end in project_team_rotation().  Normalizing inside
        # coach adjustments AND again in the pipeline caused the
        # double-inflation that pushed starters above real averages.
        return projections

    def apply_game_context_adjustments(
        self,
        projections: Dict[int, PlayerProjection],
        rotation: List[PlayerMinutes],
        game_info: GameInfo,
        team_id: int,
        is_b2b: bool = False,
        sport: str = "nba",
    ) -> Dict[int, PlayerProjection]:
        """Apply game-context adjustments to minutes projections.

        Three adjustments, applied in order:

        1. **Blowout risk** (spread >= 7): Starters lose minutes when
           a game is expected to be lopsided.  Coaches pull starters
           in the 4th quarter of blowouts.  Factor:
           ``1 - ((abs(spread) - 7) * 0.015)``, capped at 0.90.

        2. **B2B fatigue**: On the second night of a back-to-back,
           starters lose ~2 min and veterans (age >= 32) lose an
           additional 1 min.  Bench players are unaffected (they
           absorb the extra minutes).

        3. **Pace factor**: Stored on each projection for DFS stat
           scaling.  Does NOT change minutes — pace affects stat
           production rate, not playing time.
        """

        # Build a lookup for player ages
        age_lookup = {p.player_id: p.age for p in rotation}

        # Determine team's spread (positive = team is favored)
        # Prefer Vegas spread when available (more accurate than model projection)
        # game_info.vegas_spread / projected_spread: negative = home favored
        raw_spread = (
            game_info.vegas_spread
            if getattr(game_info, "vegas_spread", None) is not None
            else game_info.projected_spread
        )
        spread_source = "vegas" if getattr(game_info, "vegas_spread", None) is not None else "model"
        if game_info.home_team.team_id == team_id:
            team_spread = -raw_spread  # flip: home fav → positive
        else:
            team_spread = raw_spread  # away fav → positive

        # --- 1. Blowout risk ---
        # Prefer pre-computed modifier from Line Movement Agent (live odds).
        # Fall back to static computation from projected/vegas spread.
        _ctx_modifier = getattr(game_info, "context_modifier", None)
        abs_spread = abs(team_spread)
        _blowout_penalty_applied = False  # Track to prevent double-dip

        if _ctx_modifier and _ctx_modifier.is_blowout_risk:
            # ── Live odds-driven blowout penalty ──
            blowout_factor = _ctx_modifier.blowout_factor_starters
            _starter_minutes_lost = 0.0
            for proj in projections.values():
                if proj.adjusted_minutes >= STARTER_THRESHOLD_MINUTES:
                    old_min = proj.adjusted_minutes
                    proj.adjusted_minutes = round(proj.adjusted_minutes * blowout_factor, 1)
                    _starter_minutes_lost += old_min - proj.adjusted_minutes
                    proj.reason = f"{proj.reason} | Blowout risk (live): {blowout_factor:.2f}x"

            # Garbage-time quality discount (same logic, uses abs_spread)
            if _starter_minutes_lost > 0 and abs_spread >= GARBAGE_TIME_SPREAD_THRESHOLD:
                _gt_ramp = min(1.0, (abs_spread - GARBAGE_TIME_SPREAD_THRESHOLD) / 8.0)
                _gt_factor = 1.0 - _gt_ramp * (1.0 - GARBAGE_TIME_RATE_DISCOUNT)
                for proj in projections.values():
                    if proj.adjusted_minutes < STARTER_THRESHOLD_MINUTES and proj.adjusted_minutes > 0:
                        proj.garbage_time_factor = round(_gt_factor, 3)
                        proj.reason = (
                            f"{proj.reason} | Garbage time: {_gt_factor:.2f}x rate"
                            if proj.reason else f"Garbage time: {_gt_factor:.2f}x rate"
                        )
                logger.info(
                    f"Garbage time discount: factor={_gt_factor:.3f} for bench "
                    f"(starter mins lost={_starter_minutes_lost:.1f})"
                )

            _blowout_penalty_applied = True
            logger.info(
                f"Blowout adjustment (live): spread={_ctx_modifier.spread}, "
                f"factor={blowout_factor:.3f}"
            )

        elif abs_spread >= BLOWOUT_SPREAD_THRESHOLD:
            # ── Static fallback (no live odds available) ──
            # Non-linear penalty: diminishing returns for large spreads
            _excess = abs_spread - BLOWOUT_SPREAD_THRESHOLD
            _penalty_pct = (_excess ** BLOWOUT_PENALTY_EXPONENT) * BLOWOUT_PENALTY_PER_POINT
            blowout_factor = max(BLOWOUT_MIN_FACTOR, 1.0 - _penalty_pct)
            # Star-dampened factor (top-2 starters absorb less)
            _star_factor = max(
                BLOWOUT_MIN_FACTOR,
                1.0 - _penalty_pct * STAR_BLOWOUT_DAMPENING,
            )

            # Identify top-2 stars for dampened penalty
            _sorted_projs = sorted(
                [p for p in projections.values() if p.adjusted_minutes >= STARTER_THRESHOLD_MINUTES],
                key=lambda x: x.adjusted_minutes, reverse=True,
            )
            _star_pids = {
                p.player_id for p in _sorted_projs[:2]
                if p.baseline_minutes >= STAR_ANCHOR_THRESHOLD
            }

            # Track how many starter minutes are lost (will become bench garbage time)
            _starter_minutes_lost = 0.0
            for proj in projections.values():
                if proj.adjusted_minutes >= STARTER_THRESHOLD_MINUTES:
                    old_min = proj.adjusted_minutes
                    _f = _star_factor if proj.player_id in _star_pids else blowout_factor
                    proj.adjusted_minutes = round(proj.adjusted_minutes * _f, 1)
                    _starter_minutes_lost += old_min - proj.adjusted_minutes
                    proj.reason = (
                        f"{proj.reason} | Blowout risk: {_f:.2f}x"
                        + (" [star-dampened]" if proj.player_id in _star_pids else "")
                    )

            # Tag bench players with garbage-time quality discount.
            # When starters sit in a blowout, bench players absorb extra
            # minutes — but those minutes are lower-quality (garbage time).
            # Scale the discount by how large the spread is.
            if _starter_minutes_lost > 0 and abs_spread >= GARBAGE_TIME_SPREAD_THRESHOLD:
                # Discount intensity: ramps linearly from 1.0 (at threshold)
                # to GARBAGE_TIME_RATE_DISCOUNT (at spread=15+)
                _gt_ramp = min(1.0, (abs_spread - GARBAGE_TIME_SPREAD_THRESHOLD) / 8.0)
                _gt_factor = 1.0 - _gt_ramp * (1.0 - GARBAGE_TIME_RATE_DISCOUNT)
                for proj in projections.values():
                    if proj.adjusted_minutes < STARTER_THRESHOLD_MINUTES and proj.adjusted_minutes > 0:
                        proj.garbage_time_factor = round(_gt_factor, 3)
                        proj.reason = (
                            f"{proj.reason} | Garbage time: {_gt_factor:.2f}x rate"
                            if proj.reason else f"Garbage time: {_gt_factor:.2f}x rate"
                        )
                logger.info(
                    f"Garbage time discount: factor={_gt_factor:.3f} for bench "
                    f"(starter mins lost={_starter_minutes_lost:.1f})"
                )

            _blowout_penalty_applied = True
            logger.info(
                f"Blowout adjustment: spread={team_spread:+.1f} ({spread_source}), "
                f"factor={blowout_factor:.3f}"
            )

        # ── Point Spread Guillotine: Extreme Blowout Hard Cap ───────
        # When the spread is 13+ points, NBA coaches aggressively rest
        # starters in the 4th quarter.  The soft penalty above (max 10%)
        # is insufficient — starters routinely play only 24-28 min in
        # these games.  Hard-cap starters (salary > $6,000) at 28 min
        # and push the freed minutes to the deep bench.
        EXTREME_BLOWOUT_SPREAD = 13.0
        EXTREME_BLOWOUT_STARTER_CAP = 28.0
        EXTREME_BLOWOUT_SALARY_FLOOR = 6000

        if abs_spread >= EXTREME_BLOWOUT_SPREAD:
            _extreme_freed = 0.0
            _capped_count = 0

            for proj in projections.values():
                if proj.adjusted_minutes <= 0:
                    continue
                # Identify starters/expensive players
                _pobj = next(
                    (p for p in rotation if p.player_id == proj.player_id),
                    None,
                )
                _dk_sal = getattr(_pobj, "dk_salary", None) or 0 if _pobj else 0
                is_starter_or_expensive = (
                    proj.adjusted_minutes >= STARTER_THRESHOLD_MINUTES
                    or _dk_sal > EXTREME_BLOWOUT_SALARY_FLOOR
                )

                if is_starter_or_expensive and proj.adjusted_minutes > EXTREME_BLOWOUT_STARTER_CAP:
                    old_min = proj.adjusted_minutes
                    proj.adjusted_minutes = EXTREME_BLOWOUT_STARTER_CAP
                    freed = old_min - EXTREME_BLOWOUT_STARTER_CAP
                    _extreme_freed += freed
                    _capped_count += 1
                    proj.reason = (
                        f"{proj.reason} | 4Q Guillotine: "
                        f"{old_min:.1f} → {EXTREME_BLOWOUT_STARTER_CAP:.0f} min "
                        f"(spread {abs_spread:+.1f})"
                    )

            # Redistribute freed minutes to deep bench (garbage time unit)
            if _extreme_freed > 0.5:
                _bench_projs = sorted(
                    [
                        p for p in projections.values()
                        if 0 < p.adjusted_minutes < STARTER_THRESHOLD_MINUTES
                    ],
                    key=lambda x: x.adjusted_minutes,
                )
                if _bench_projs:
                    # Distribute evenly across bench, cap each at 24 min
                    _remaining = _extreme_freed
                    for bp in _bench_projs:
                        if _remaining <= 0.1:
                            break
                        headroom = 24.0 - bp.adjusted_minutes
                        if headroom <= 0:
                            continue
                        add = min(_remaining / max(1, len(_bench_projs)), headroom)
                        bp.adjusted_minutes = round(bp.adjusted_minutes + add, 1)
                        _remaining -= add
                        bp.reason = (
                            f"{bp.reason} | Garbage time: +{add:.1f} min "
                            f"(4Q blowout redistribution)"
                        )

                logger.warning(
                    "[Blowout] POINT SPREAD GUILLOTINE: spread=%.1f, "
                    "%d starters capped at %.0f min, %.1f min → bench",
                    abs_spread, _capped_count,
                    EXTREME_BLOWOUT_STARTER_CAP, _extreme_freed,
                )

        # --- 2. Rest-day fatigue/boost (continuous curve) ---
        # Uses rest_days when available (0=B2B, 1=normal, 2+=rested).
        # Falls back to binary B2B flag when rest_days is not populated.
        # NOTE: CBB teams rarely play back-to-back — skip entirely for cbb.
        rest_days = getattr(game_info, "rest_days", None)
        if sport == "cbb":
            rest_days = None  # Suppress B2B/rest logic for college
            is_b2b = False
        if rest_days is not None:
            rest_factor = (
                1.0
                + REST_BOOST_PER_EXTRA_DAY * (rest_days - 1)
                - REST_PENALTY_B2B * max(0, 1 - rest_days)
            )
            rest_factor = max(REST_FACTOR_MIN, min(REST_FACTOR_MAX, rest_factor))

            if rest_factor != 1.0:
                for proj in projections.values():
                    if proj.adjusted_minutes >= STARTER_THRESHOLD_MINUTES:
                        player_age = age_lookup.get(proj.player_id)
                        # Veterans get extra penalty on short rest
                        vet_penalty = 0.0
                        if rest_days == 0 and player_age is not None and player_age >= VETERAN_AGE:
                            vet_penalty = B2B_VETERAN_EXTRA_MINUTES / max(proj.adjusted_minutes, 1.0)
                        effective_factor = max(rest_factor - vet_penalty, REST_FACTOR_MIN)
                        proj.adjusted_minutes = round(
                            max(proj.adjusted_minutes * effective_factor, 0), 1
                        )
                        proj.reason = f"{proj.reason} | Rest({rest_days}d): {effective_factor:.3f}x"
                logger.info(f"Rest-day adjustment: rest_days={rest_days}, factor={rest_factor:.3f}")
        elif is_b2b:
            # Fallback to binary B2B when rest_days is not available
            for proj in projections.values():
                if proj.adjusted_minutes >= STARTER_THRESHOLD_MINUTES:
                    reduction = B2B_STARTER_REDUCTION_MINUTES
                    player_age = age_lookup.get(proj.player_id)
                    if player_age is not None and player_age >= VETERAN_AGE:
                        reduction += B2B_VETERAN_EXTRA_MINUTES
                    proj.adjusted_minutes = round(
                        max(proj.adjusted_minutes - reduction, 0), 1
                    )
                    proj.reason = f"{proj.reason} | B2B fatigue: -{reduction:.1f} min"
            logger.info("B2B fatigue adjustments applied (binary fallback)")

        # --- 2b. Multi-game trip fatigue ---
        games_last_n = getattr(game_info, "games_in_last_n_days", None)
        if games_last_n is not None and games_last_n >= FATIGUE_THRESHOLD_GAMES:
            extra_games = games_last_n - FATIGUE_THRESHOLD_GAMES
            trip_penalty = min(extra_games * FATIGUE_PENALTY_PER_GAME, FATIGUE_MAX_PENALTY)
            trip_factor = 1.0 - trip_penalty
            if trip_factor < 1.0:
                for proj in projections.values():
                    if proj.adjusted_minutes >= STARTER_THRESHOLD_MINUTES:
                        proj.adjusted_minutes = round(proj.adjusted_minutes * trip_factor, 1)
                        proj.reason = f"{proj.reason} | Trip fatigue({games_last_n}g): {trip_factor:.3f}x"
                logger.info(
                    f"Multi-game trip fatigue: {games_last_n} games in lookback, "
                    f"factor={trip_factor:.3f}"
                )

        # --- 3. Pace factor (for DFS stat scaling, not minutes) ---
        # Use sport-specific league average pace so CBB games are compared
        # against the CBB mean (~68), not the NBA mean (~102).  Without
        # this, every CBB stat gets multiplied by ~0.67 — a double-penalty
        # since per-minute rates are already derived from CBB game logs.
        if sport == "cbb":
            from app.config.constants import CBB_LEAGUE_AVG_PACE
            _league_pace = CBB_LEAGUE_AVG_PACE
        else:
            _league_pace = LEAGUE_AVG_PACE
        pace_factor = (
            game_info.projected_pace / _league_pace
            if _league_pace > 0
            else 1.0
        )
        for proj in projections.values():
            if proj.adjusted_minutes > 0:
                proj.pace_factor = round(pace_factor, 4)

        # --- 3b. High-total pace boost from live odds modifier ---
        # When the over/under exceeds 235, apply a 2% pace multiplier to
        # account for elevated pace / possession counts expected in a
        # high-scoring game.  Only fires when a live GameContextModifier
        # is attached to the GameInfo (i.e. BDL odds were available).
        if _ctx_modifier and _ctx_modifier.high_total_pace_boost != 1.0:
            for proj in projections.values():
                if proj.adjusted_minutes > 0:
                    proj.pace_factor = round(
                        proj.pace_factor * _ctx_modifier.high_total_pace_boost, 4
                    )
            logger.info(
                f"High-total pace boost (live): "
                f"{_ctx_modifier.high_total_pace_boost:.3f}x "
                f"(O/U={_ctx_modifier.over_under})"
            )

        # --- 4. Learned game-context calibrations (auto from tournament/backtest) ---
        # IMPORTANT: The blowout calibration multiplier adjusts the BASE
        # penalty intensity learned from historical data.  It must NOT
        # stack multiplicatively with the Agent 14 penalty (which already
        # fired in section 1).  When the base penalty already fired, we
        # apply only the INCREMENTAL deviation from 1.0 (halved to blend)
        # to avoid double-penalizing starters on big-spread games.
        if self._calibration:
            if abs_spread >= BLOWOUT_SPREAD_THRESHOLD:
                blowout_cal = self._calibration.get_game_context_multiplier("blowout")
                if blowout_cal != 1.0:
                    if _blowout_penalty_applied:
                        # Already penalized — apply only half the incremental
                        # delta so the calibration fine-tunes, not stacks.
                        blowout_cal = 1.0 + (blowout_cal - 1.0) * 0.5
                        logger.debug(
                            "Calibration blowout half-dampened to %.3f "
                            "(base penalty already applied)", blowout_cal,
                        )
                    for proj in projections.values():
                        if proj.adjusted_minutes >= STARTER_THRESHOLD_MINUTES:
                            proj.adjusted_minutes = round(
                                proj.adjusted_minutes * blowout_cal, 1
                            )
            if is_b2b:
                b2b_cal = self._calibration.get_game_context_multiplier("b2b")
                if b2b_cal != 1.0:
                    for proj in projections.values():
                        if proj.adjusted_minutes >= STARTER_THRESHOLD_MINUTES:
                            proj.adjusted_minutes = round(
                                proj.adjusted_minutes * b2b_cal, 1
                            )

        # --- 5. Competitive context ---
        # Classify team's competitive situation from W-L record and adjust
        # star minutes accordingly.  Tanking teams rest stars; playoff-push
        # teams increase star usage; clinched teams ease up slightly.
        # NOTE: Not applicable for NCAA — college teams don't tank or rest stars.
        if sport == "cbb":
            return projections
        comp_ctx = getattr(game_info, "competitive_context", None)

        # Auto-derive context from W-L when not explicitly set
        if not comp_ctx:
            this_team = (
                game_info.home_team
                if game_info.home_team.team_id == team_id
                else game_info.away_team
            )
            wp = getattr(this_team, "win_pct", 0.5)
            total_games = this_team.wins + this_team.losses
            if total_games >= 20:  # Need enough games for meaningful signal
                if wp <= COMPETITIVE_TANKING_WP:
                    comp_ctx = "tanking"
                elif wp >= COMPETITIVE_CLINCHED_WP:
                    comp_ctx = "clinched"
                elif 0.45 <= wp <= 0.55:
                    # Bubble teams in playoff push (around .500)
                    comp_ctx = "playoff_push"

        if comp_ctx:
            if comp_ctx == "tanking":
                ctx_mult = COMPETITIVE_TANKING_STAR_MULT
            elif comp_ctx == "clinched":
                ctx_mult = COMPETITIVE_CLINCHED_STAR_MULT
            elif comp_ctx == "playoff_push":
                ctx_mult = COMPETITIVE_PUSH_STAR_MULT
            else:
                ctx_mult = 1.0

            # ── Anti-double-dip: when a blowout penalty already fired,
            # the "clinched" context is redundant for starters — both
            # model the same phenomenon (good team → starters sit early).
            # Dampen the competitive penalty to only the incremental
            # delta (halved) so it fine-tunes rather than stacks.
            if _blowout_penalty_applied and comp_ctx == "clinched" and ctx_mult < 1.0:
                original_mult = ctx_mult
                ctx_mult = 1.0 + (ctx_mult - 1.0) * 0.5  # e.g., 0.96 → 0.98
                logger.info(
                    f"Competitive context ({comp_ctx}): dampened from "
                    f"{original_mult:.2f} → {ctx_mult:.2f} (blowout already applied)"
                )

            if ctx_mult != 1.0:
                for proj in projections.values():
                    if proj.adjusted_minutes >= STARTER_THRESHOLD_MINUTES:
                        proj.adjusted_minutes = round(
                            proj.adjusted_minutes * ctx_mult, 1
                        )
                        proj.reason = (
                            f"{proj.reason} | {comp_ctx}: {ctx_mult:.2f}x"
                            if proj.reason else f"{comp_ctx}: {ctx_mult:.2f}x"
                        )
                logger.info(
                    f"Competitive context: {comp_ctx}, star mult={ctx_mult:.2f}"
                )

        # --- Stub: Opponent coach matchup (Improvement 14) ---
        opp_coach = getattr(game_info, "opponent_coach_id", None)
        if opp_coach:
            logger.info(f"Opponent coach: {opp_coach} (stub, no effect yet)")

        return projections

    def project_team_rotation(
        self,
        team_id: int,
        team_name: str,
        rotation: List[PlayerMinutes],
        injuries: List[PlayerStatus],
        game_date: str,
        apply_coach_adjustments: bool = True,
        game_info: Optional[GameInfo] = None,
        is_b2b: bool = False,
        all_injuries: Optional[List[PlayerStatus]] = None,
        sport: str = "nba",
        recent_weight_override: Optional[float] = None,
        dk_injury_statuses: Optional[Dict[int, str]] = None,
    ) -> TeamRotation:
        matched_injuries = self._match_injuries_to_roster(
            rotation, injuries, all_injuries=all_injuries
        )

        # =================================================================
        # MINUTE ALLOCATION: Top-Down vs Legacy
        #
        # USE_TOP_DOWN_MINUTES=True:
        #   New "Starter's Squeeze" allocator replaces Steps 1-2.
        #   - Zeros out inactive players FIRST (Active Status Guillotine)
        #   - Identifies 5 starters via positional depth chart
        #   - Allocates starter minutes greedily (28-38 min)
        #   - Cascades remaining ~70-80 min to bench by rank
        #   - Guarantees sum == 240 from the start
        #   Skips Steps 1a-pre through 1b (redundant — all handled by
        #   the allocator) and Step 2 (redistribute_minutes).
        #
        # USE_TOP_DOWN_MINUTES=False:
        #   Legacy per-player baseline + normalize approach.
        # =================================================================

        if USE_TOP_DOWN_MINUTES and sport == "nba":
            # ── Top-Down Allocation (Steps 1+2 combined) ──────────────
            # Get rotation depth from coach agent if available
            _td_depth = None
            if self._coach_agent:
                try:
                    _td_depth = self._coach_agent.get_expected_rotation_size(
                        team_id, sport
                    )
                except Exception:
                    pass

            # Fetch coach rotation preferences for the Thibodeau Rule
            _coach_rotation_size = None
            _coach_max_min = None
            try:
                _coach_prof = get_coach_profile(team_id)
                if _coach_prof:
                    _coach_rotation_size = _coach_prof.min_rotation_size
                    _coach_max_min = _coach_prof.max_minutes_override
            except Exception:
                pass

            baseline_projections = allocate_team_minutes(
                rotation=rotation,
                injuries=matched_injuries,
                dk_injury_statuses=dk_injury_statuses,
                rotation_depth=_td_depth,
                team_name=team_name,
                coach_rotation_size=_coach_rotation_size,
                coach_max_minutes=_coach_max_min,
            )

            # Log vacancy promotions: players with starter-level minutes
            # despite low season average — means vacancy detection fired.
            for _p in rotation:
                _bl = baseline_projections.get(_p.player_id, 0)
                if _bl >= 28 and _p.season_avg < 20:
                    logger.info(
                        "[TopDown] %s: PROMOTED — %s (%s) allocated %.1f min "
                        "(season_avg=%.1f, dk_sal=$%s)",
                        team_name, _p.player_name, _p.position, _bl,
                        _p.season_avg,
                        f"{_p.dk_salary:,}" if _p.dk_salary else "N/A",
                    )

            # Build adjusted_projections dict from top-down baselines.
            # The top-down allocator has already handled:
            #   - Active Status Guillotine (DNP zeroing)
            #   - Injury reallocation (backup promotion)
            #   - 240-minute budget distribution
            # So we skip Steps 1a-pre, 1a-post, 1a, 1b, and 2.
            _roster_change_ids = {
                p.player_id for p in rotation
                if getattr(p, "roster_change_detected", False)
            }
            adjusted_projections = {
                pid: PlayerProjection(
                    player_id=pid,
                    player_name=next(
                        p.player_name for p in rotation if p.player_id == pid
                    ),
                    position=next(
                        p.position for p in rotation if p.player_id == pid
                    ),
                    baseline_minutes=baseline,
                    adjusted_minutes=baseline,
                    confidence=1.0 if baseline > 0 else 0.0,
                    reason=(
                        "Top-down allocation"
                        if baseline > 0
                        else "Zeroed (inactive/depth)"
                    ),
                    roster_change_detected=(pid in _roster_change_ids),
                )
                for pid, baseline in baseline_projections.items()
            }

            # Propagate injury play_probability and reason for injured players
            _dk_sts_td = dk_injury_statuses or {}
            for ip in matched_injuries:
                proj = adjusted_projections.get(ip.player_id)
                if not proj:
                    continue
                _, p_play = self._injury_minute_factor(ip.status)
                if p_play is not None and p_play < 1.0:
                    proj.play_probability = p_play
                    proj.confidence = p_play
                if ip.status in ("Out", "IR", "Injured Reserve") or _dk_sts_td.get(
                    ip.player_id, ""
                ).upper() in {"OUT", "O", "D", "DOUBTFUL", "IR", "INJ", "INJURED RESERVE"}:
                    proj.play_probability = 0.0
                    proj.confidence = 0.0
                    proj.adjusted_minutes = 0.0
                    proj.reason = f"Out - Injured ({ip.status})"
                elif p_play is not None and p_play < 1.0:
                    proj.reason = (
                        f"{ip.status} - if active "
                        f"(P(plays)={p_play:.0%})"
                    )

            # ---------------------------------------------------------
            # Phase 2b/2c: Usage Boost for Injury Beneficiaries.
            #
            # The top-down allocator handles MINUTE redistribution but
            # not EFFICIENCY scaling.  When a high-usage star is out,
            # remaining players don't just get more minutes — they get
            # more touches per minute (higher usage rate → higher FPPM).
            #
            # This mirrors the Phase 2b/2c logic in redistribute_minutes()
            # (legacy path), applied to the top-down adjusted_projections.
            # ---------------------------------------------------------

            # Build reduced_players: injured player_id → freed minutes
            _td_reduced: Dict[int, float] = {}
            for ip in matched_injuries:
                _td_mf, _ = self._injury_minute_factor(ip.status)
                if _td_mf is None:
                    continue
                # Use season_avg as the "what they would have played" baseline
                _td_bl = next(
                    (p.season_avg for p in rotation if p.player_id == ip.player_id),
                    0,
                )
                if _td_bl <= 0:
                    continue
                _td_freed = _td_bl * (1.0 - _td_mf)
                if _td_freed > 0:
                    _td_reduced[ip.player_id] = _td_freed

            _td_all_reduced = set(_td_reduced.keys())

            if _td_reduced:
                # ── Phase 2b: Proportional usage redistribution ──────
                for _rd_id, _rd_freed in _td_reduced.items():
                    _rd_player = next(
                        (p for p in rotation if p.player_id == _rd_id), None
                    )
                    if not _rd_player:
                        continue
                    _rd_usage = _rd_player.usage_rate
                    if _rd_usage < 0.10:
                        continue  # Low-usage → no meaningful redistribution

                    _rd_baseline = next(
                        (p.season_avg for p in rotation if p.player_id == _rd_id),
                        0,
                    )
                    if _rd_baseline <= 0:
                        continue
                    _rd_frac = _rd_freed / _rd_baseline
                    _freed_usage = _rd_usage * _rd_frac

                    # Same-position active beneficiaries
                    _beneficiaries = [
                        p for p in rotation
                        if p.player_id not in _td_all_reduced
                        and self._positions_overlap(p.position, _rd_player.position)
                        and p.usage_rate > 0
                    ]
                    if not _beneficiaries:
                        continue

                    _total_benef_usage = sum(b.usage_rate for b in _beneficiaries)
                    if _total_benef_usage <= 0:
                        continue

                    for b in _beneficiaries:
                        _share = b.usage_rate / _total_benef_usage
                        _added = _freed_usage * _share
                        _new_usage = b.usage_rate + _added
                        _boost = min(_new_usage / b.usage_rate, USAGE_BOOST_CAP)
                        # Diminishing returns: high-FPPM players get dampened
                        _boost = dampened_usage_boost(_boost, _estimate_fppm(b))
                        _proj_b = adjusted_projections.get(b.player_id)
                        if _proj_b:
                            _proj_b.usage_boost = round(
                                max(_proj_b.usage_boost, _boost), 3
                            )

                # ── Phase 2c: High-usage Out → targeted boost ────────
                for _rd_id, _rd_freed in _td_reduced.items():
                    _rd_player = next(
                        (p for p in rotation if p.player_id == _rd_id), None
                    )
                    if not _rd_player:
                        continue
                    _rd_baseline = next(
                        (p.season_avg for p in rotation if p.player_id == _rd_id),
                        0,
                    )
                    if _rd_baseline <= 0:
                        continue
                    # Only fully Out + high-usage
                    if _rd_freed < _rd_baseline * 0.99:
                        continue
                    if _rd_player.usage_rate < HIGH_USAGE_OUT_THRESHOLD:
                        continue

                    # Primary backup via hierarchy
                    _bh = self.identify_backup_hierarchy(rotation, _rd_player)
                    _bh = [
                        (bp, w) for bp, w in _bh
                        if bp.player_id not in _td_all_reduced
                    ]
                    if not _bh:
                        continue
                    _primary_bp = _bh[0][0]

                    _proj_pb = adjusted_projections.get(_primary_bp.player_id)
                    if _proj_pb:
                        _ex_boost = getattr(_proj_pb, "usage_boost", 1.0)
                        # Dampen the flat rate boost by player FPPM
                        _dampened_huo = dampened_usage_boost(
                            HIGH_USAGE_OUT_RATE_BOOST,
                            _estimate_fppm(_primary_bp),
                        )
                        _proj_pb.usage_boost = round(
                            max(_ex_boost, _dampened_huo), 3
                        )
                        logger.info(
                            "[TopDown] Usage-Rate Scaling: %s (%.0f%% usage) Out → "
                            "primary backup %s rate boost %.3f "
                            "(raw=%.3f, dampened=%.3f)",
                            _rd_player.player_name,
                            _rd_player.usage_rate * 100,
                            _primary_bp.player_name,
                            _proj_pb.usage_boost,
                            HIGH_USAGE_OUT_RATE_BOOST,
                            _dampened_huo,
                        )

                    # Secondary star: highest-baseline same-position active
                    _sec_star = None
                    _sec_best = 0.0
                    for p in rotation:
                        if p.player_id == _primary_bp.player_id:
                            continue
                        if p.player_id in _td_all_reduced:
                            continue
                        if not self._positions_overlap(
                            p.position, _rd_player.position
                        ):
                            continue
                        _p_bl = baseline_projections.get(p.player_id, 0)
                        if _p_bl > _sec_best:
                            _sec_best = _p_bl
                            _sec_star = p

                    if _sec_star:
                        _proj_ss = adjusted_projections.get(_sec_star.player_id)
                        if _proj_ss:
                            _ex_boost = getattr(_proj_ss, "usage_boost", 1.0)
                            _dampened_huo_ss = dampened_usage_boost(
                                HIGH_USAGE_OUT_RATE_BOOST,
                                _estimate_fppm(_sec_star),
                            )
                            _proj_ss.usage_boost = round(
                                max(_ex_boost, _dampened_huo_ss), 3
                            )
                            logger.info(
                                "[TopDown] Usage-Rate Scaling: %s Out → "
                                "secondary star %s rate boost %.3f",
                                _rd_player.player_name,
                                _sec_star.player_name,
                                _proj_ss.usage_boost,
                            )

                # Count boosted players
                _n_boosted = sum(
                    1 for p in adjusted_projections.values()
                    if p.usage_boost > 1.0
                )
                if _n_boosted:
                    logger.info(
                        "[TopDown] %s: %d players received usage boost "
                        "(injury beneficiaries)",
                        team_name, _n_boosted,
                    )

            # ── Phase 2c½: Defensive Attention Penalty ─────────────────
            # When a team's top-2 highest-usage players are BOTH Out,
            # the remaining primary scorer faces increased defensive
            # attention (double teams, traps).  Penalize their offensive
            # per-minute rates to reflect decreased efficiency.
            #
            # Example: NOP missing Zion (30% USG) + Ingram (28% USG) →
            # CJ McCollum is the only real scoring threat → defenses key
            # on him → his efficiency drops despite more touches.
            if _td_reduced:
                _rotation_by_id = {p.player_id: p for p in rotation}
                # Find high-usage players who are fully Out
                _high_usage_out = []
                for _rd_id, _rd_freed in _td_reduced.items():
                    _rd_p = _rotation_by_id.get(_rd_id)
                    if not _rd_p:
                        continue
                    _rd_bl = _rd_p.season_avg
                    if _rd_bl <= 0:
                        continue
                    # Must be fully Out (freed ~100% of baseline)
                    if _rd_freed < _rd_bl * 0.99:
                        continue
                    if _rd_p.usage_rate >= DEFENSIVE_ATTENTION_USAGE_THRESHOLD:
                        _high_usage_out.append(_rd_p)

                if len(_high_usage_out) >= DEFENSIVE_ATTENTION_MIN_USAGE_OUT:
                    # Identify the remaining primary scorer: highest-usage
                    # active player who is NOT in the reduced set
                    _active_scorers = [
                        p for p in rotation
                        if p.player_id not in _td_all_reduced
                        and p.usage_rate > 0
                        and adjusted_projections.get(p.player_id)
                        and adjusted_projections[p.player_id].adjusted_minutes > 0
                    ]
                    _active_scorers.sort(key=lambda p: p.usage_rate, reverse=True)

                    if _active_scorers:
                        _target = _active_scorers[0]
                        _target_pm = _rotation_by_id.get(_target.player_id)
                        if _target_pm:
                            # Apply penalty to offensive per-minute rates
                            for _off_field in ("pts_per_min", "ast_per_min", "fg3m_per_min"):
                                _old = getattr(_target_pm, _off_field, 0.0) or 0.0
                                _new = max(0.0, _old - DEFENSIVE_ATTENTION_PENALTY)
                                setattr(_target_pm, _off_field, round(_new, 4))
                            _proj_target = adjusted_projections.get(_target.player_id)
                            if _proj_target:
                                _proj_target.reason = (
                                    f"{_proj_target.reason} | "
                                    f"Defensive attention: -{DEFENSIVE_ATTENTION_PENALTY} FPPM "
                                    f"({len(_high_usage_out)} high-usage OUT: "
                                    f"{', '.join(p.player_name for p in _high_usage_out)})"
                                )
                            logger.warning(
                                "[TopDown] DEFENSIVE ATTENTION: %s (%s) penalized "
                                "-%s FPPM — team missing %d high-usage players: %s",
                                _target.player_name, team_name,
                                DEFENSIVE_ATTENTION_PENALTY,
                                len(_high_usage_out),
                                ", ".join(
                                    f"{p.player_name} ({p.usage_rate*100:.0f}%)"
                                    for p in _high_usage_out
                                ),
                            )

            # ── Phase 2d: Promotion Usage Bump ────────────────────────
            # Bench players promoted to starter (vacancy fill) or who
            # are the next-man-up at a vacant position get a flat
            # per-minute rate increase.  This captures the effect of
            # more touches/possessions when absorbing a starter's role.
            from app.services.top_down_minutes import (
                PROMOTION_USAGE_BUMP,
                _is_sparse_data_player,
                regress_sparse_fppm,
            )
            _promoted_ids = getattr(baseline_projections, "promoted_ids", set()) or set()
            _vacancy_slots = getattr(baseline_projections, "vacancy_slots", set()) or set()
            _lms_exempt_ids = getattr(baseline_projections, "lms_exempt_ids", set()) or set()

            if _promoted_ids:
                # Build a lookup of rotation PlayerMinutes by player_id
                _rotation_by_id = {p.player_id: p for p in rotation}

                for pid in _promoted_ids:
                    proj = adjusted_projections.get(pid)
                    if proj and proj.adjusted_minutes > 0:
                        existing_boost = getattr(proj, "usage_boost", 1.0)
                        proj.usage_boost = round(
                            max(existing_boost, PROMOTION_USAGE_BUMP), 3
                        )
                        proj.is_spot_starter = True

                        # ── FPPM Regression for sparse-data promotions ──
                        # When a 3rd-string scrub gets promoted with <5
                        # games of data, regress their per-minute rates
                        # toward conservative positional baselines so the
                        # optimizer doesn't treat them as elite plays.
                        # We modify the PlayerMinutes object directly since
                        # DFSService reads per-minute rates from it (not
                        # from PlayerProjection).
                        #
                        # LMS-exempt players still get regressed, but with
                        # reduced weight — their recent heavy usage makes
                        # their actual rates more trustworthy.
                        _pm = _rotation_by_id.get(pid)
                        _was_regressed = False
                        _is_lms = pid in _lms_exempt_ids
                        if _pm and _is_sparse_data_player(_pm):
                            regressed = regress_sparse_fppm(_pm, lms_exempt=_is_lms)
                            _pm.pts_per_min = regressed["pts_per_min"]
                            _pm.reb_per_min = regressed["reb_per_min"]
                            _pm.ast_per_min = regressed["ast_per_min"]
                            _pm.stl_per_min = regressed["stl_per_min"]
                            _pm.blk_per_min = regressed["blk_per_min"]
                            _pm.tov_per_min = regressed["tov_per_min"]
                            _pm.fg3m_per_min = regressed.get("fg3m_per_min", _pm.fg3m_per_min)
                            _was_regressed = True

                        _lms_tag = " [LMS EXEMPT]" if _is_lms else ""
                        logger.info(
                            "[TopDown] PROMOTION USAGE BUMP: %s (%s) → "
                            "usage_boost=%.3f (%.1f min, vacancy=%s)%s%s",
                            proj.player_name, proj.position,
                            proj.usage_boost, proj.adjusted_minutes,
                            _vacancy_slots,
                            " [FPPM REGRESSED]" if _was_regressed else "",
                            _lms_tag,
                        )

            # ── Phase 2d½: Blank Slate FPPM Fallback ──────────────────
            # Players with < 5 games logged (or ALL per-minute rates at 0.0)
            # produce FPPM=0.0 → dk_points=0.0, which is useless for the
            # optimizer.  This catches ALL sparse-data players with allocated
            # minutes — not just promoted starters (handled above) — and
            # applies the same positional regression to give them a synthetic
            # FPPM baseline.
            #
            # This is the "Bez Mbeng fix": G-League call-ups and deep-bench
            # players who receive minutes via the positional cascade but have
            # no BDL game logs need a valid FP projection.
            #
            # The positional baselines produce these approximate FPPMs:
            #   Guards (PG/SG/G):  ~0.58 FPPM (scoring-oriented scrubs)
            #   Forwards (SF/PF/F): ~0.56 FPPM (rebounding-oriented)
            #   Centers (C):       ~0.63 FPPM (easy boards + rim protection)
            # ──────────────────────────────────────────────────────────────
            if not hasattr(self, '_blank_slate_rotation_by_id'):
                _blank_slate_rotation_by_id = {p.player_id: p for p in rotation}
            else:
                _blank_slate_rotation_by_id = {p.player_id: p for p in rotation}

            _blank_slate_count = 0
            for pid, proj in adjusted_projections.items():
                if proj.adjusted_minutes <= 0:
                    continue
                if pid in _promoted_ids:
                    continue  # Already handled above

                _pm = _blank_slate_rotation_by_id.get(pid)
                if not _pm:
                    continue

                # Check if this player needs the blank-slate fallback:
                # Either sparse data (< 5 games) or all per-min rates are zero
                _all_rates_zero = (
                    (_pm.pts_per_min or 0) == 0
                    and (_pm.reb_per_min or 0) == 0
                    and (_pm.ast_per_min or 0) == 0
                )
                _needs_fallback = _is_sparse_data_player(_pm) or _all_rates_zero

                if not _needs_fallback:
                    continue

                # ── FPPM Hierarchy ──────────────────────────────────
                # Step A: >= 5 NBA games → use real NBA FPPM (not here)
                # Step B: G-League cache → translated rates (0.75x tax)
                # Step C: No G-League data → positional baseline
                # ────────────────────────────────────────────────────
                _gleague_rates = None
                _fppm_source = "positional baseline"

                if self._gleague:
                    _gleague_rates = self._gleague.get_translated_rates(
                        _pm.player_name
                    )

                if _gleague_rates:
                    # Step B: G-League data available — use translated rates
                    _pm.pts_per_min = _gleague_rates.get("pts_per_min", 0)
                    _pm.reb_per_min = _gleague_rates.get("reb_per_min", 0)
                    _pm.ast_per_min = _gleague_rates.get("ast_per_min", 0)
                    _pm.stl_per_min = _gleague_rates.get("stl_per_min", 0)
                    _pm.blk_per_min = _gleague_rates.get("blk_per_min", 0)
                    _pm.tov_per_min = _gleague_rates.get("tov_per_min", 0)
                    _pm.fg3m_per_min = _gleague_rates.get("fg3m_per_min", _pm.fg3m_per_min)
                    _fppm_source = "G-League (0.75x tax)"

                    _gl_entry = self._gleague.get_player(
                        normalize_player_name(_pm.player_name)
                    )
                    _raw_gl_fppm = _gl_entry["gleague_fppm"] if _gl_entry else 0
                    _translated_fppm = _gl_entry["nba_translated_fppm"] if _gl_entry else 0
                    logger.info(
                        "[G-League Translator] %s: %.3f G-League FPPM → "
                        "%.3f NBA FPPM (0.75x tax, %d G-League min)",
                        _pm.player_name, _raw_gl_fppm, _translated_fppm,
                        _gl_entry.get("gleague_minutes", 0) if _gl_entry else 0,
                    )
                else:
                    # Step C: No G-League data — fall back to positional baseline
                    regressed = regress_sparse_fppm(_pm, lms_exempt=False)
                    _pm.pts_per_min = regressed["pts_per_min"]
                    _pm.reb_per_min = regressed["reb_per_min"]
                    _pm.ast_per_min = regressed["ast_per_min"]
                    _pm.stl_per_min = regressed["stl_per_min"]
                    _pm.blk_per_min = regressed["blk_per_min"]
                    _pm.tov_per_min = regressed["tov_per_min"]
                    _pm.fg3m_per_min = regressed.get("fg3m_per_min", _pm.fg3m_per_min)

                _blank_slate_count += 1

                # Compute the resulting FPPM for logging
                _synth_fppm = (
                    (_pm.pts_per_min or 0)
                    + 1.25 * (_pm.reb_per_min or 0)
                    + 1.5 * (_pm.ast_per_min or 0)
                    + 2.0 * (_pm.stl_per_min or 0)
                    + 2.0 * (_pm.blk_per_min or 0)
                    - 0.5 * (_pm.tov_per_min or 0)
                    + 0.5 * (_pm.fg3m_per_min or 0)
                )
                _synth_fp = round(proj.adjusted_minutes * _synth_fppm, 1)

                proj.reason = (
                    f"{proj.reason} | FPPM ({_fppm_source}): "
                    f"{_synth_fppm:.3f} → {_synth_fp} projected FP"
                )
                logger.info(
                    "[TopDown] BLANK SLATE: %s (%s, %.1f min) | "
                    "source=%s, FPPM=%.3f → %.1f projected FP | "
                    "games=%d, all_rates_zero=%s",
                    proj.player_name, proj.position,
                    proj.adjusted_minutes, _fppm_source,
                    _synth_fppm, _synth_fp,
                    sum(1 for m in (_pm.minutes_last_10 or []) if m > 0),
                    _all_rates_zero,
                )

            if _blank_slate_count > 0:
                logger.info(
                    "[TopDown] %s: BLANK SLATE applied to %d bench players "
                    "with sparse/zero FPPM data",
                    team_name, _blank_slate_count,
                )

            # ── Phase 2e: Team Health Blowout Efficiency Discount ───────
            # When a team is missing 3+ primary rotation players, the
            # surviving scrubs produce at a lower per-minute rate because
            # there are no real playmakers/shooters to create offense.
            # Apply the efficiency factor to offensive per-minute rates
            # on the PlayerMinutes objects (read by DFSService downstream).
            _blowout_eff = getattr(baseline_projections, "blowout_efficiency_factor", 1.0)
            if _blowout_eff < 1.0:
                _rotation_by_id_eff = {p.player_id: p for p in rotation}
                _offensive_fields = [
                    "pts_per_min", "ast_per_min", "fg3m_per_min",
                ]
                for pid, proj in adjusted_projections.items():
                    if proj.adjusted_minutes <= 0:
                        continue
                    _pm = _rotation_by_id_eff.get(pid)
                    if not _pm:
                        continue
                    for field in _offensive_fields:
                        old_val = getattr(_pm, field, 0.0) or 0.0
                        setattr(_pm, field, round(old_val * _blowout_eff, 4))
                    proj.reason = (
                        f"{proj.reason} | Team health penalty: {_blowout_eff:.2f}x offense"
                    )
                logger.warning(
                    "[TopDown] %s: BLOWOUT EFFICIENCY applied — %.2fx to "
                    "offensive rates (pts, ast, fg3m) for %d active players",
                    team_name, _blowout_eff,
                    sum(1 for p in adjusted_projections.values() if p.adjusted_minutes > 0),
                )

            # ── Phase 2f: Alpha Vacuum — FPPM Boost for Remaining Stars ──
            # When a high-usage alpha (USG% > 28% or salary > $9K) is Out,
            # the remaining healthy starters absorb their shot attempts.
            # The top 2 highest-minute active players get a 1.15x boost to
            # their usage_boost, increasing their per-minute fantasy
            # production.  This stacks with the existing PROMOTION_USAGE_BUMP
            # and is dampened by the same diminishing-returns formula.
            #
            # This is the INVERSE of blowout_efficiency_factor: when 1 alpha
            # is out, the remaining stars get BETTER.  When 3+ players are
            # out, the whole team gets WORSE.  Both can fire simultaneously
            # (e.g., alpha PG out + 2 rotation bigs out = vacuum boost for
            # the remaining SF star + blowout penalty for everyone else).
            _alpha_vacuum = getattr(baseline_projections, "alpha_vacuum", False)
            if _alpha_vacuum:
                ALPHA_VACUUM_BOOST = 1.15
                _alpha_out_names = getattr(baseline_projections, "alpha_out_names", [])
                _alpha_out_usage = getattr(baseline_projections, "alpha_out_usage", 0.0)

                # Find top 2 active players by projected minutes (the offensive focal points)
                _active_sorted = sorted(
                    (
                        (pid, proj)
                        for pid, proj in adjusted_projections.items()
                        if proj.adjusted_minutes > 0
                    ),
                    key=lambda x: x[1].adjusted_minutes,
                    reverse=True,
                )
                _top_2 = _active_sorted[:2]

                _rotation_by_id_av = {p.player_id: p for p in rotation}
                for pid, proj in _top_2:
                    _pm = _rotation_by_id_av.get(pid)
                    existing_boost = getattr(proj, "usage_boost", 1.0) or 1.0

                    # Apply alpha vacuum boost, respecting dampening
                    raw_boost = max(existing_boost, ALPHA_VACUUM_BOOST)
                    if _pm:
                        player_fppm = _estimate_fppm(_pm)
                        dampened = dampened_usage_boost(raw_boost, player_fppm)
                    else:
                        dampened = raw_boost

                    proj.usage_boost = round(dampened, 3)
                    proj.reason = (
                        f"{proj.reason} | Alpha Vacuum: "
                        f"{', '.join(_alpha_out_names)} out "
                        f"(USG={_alpha_out_usage:.0f}%) → {dampened:.2f}x boost"
                    )

                    logger.info(
                        "[TopDown] ALPHA VACUUM BOOST: %s (%s, %.1f min) → "
                        "usage_boost=%.3f (was %.3f) | alpha out: %s",
                        proj.player_name, proj.position,
                        proj.adjusted_minutes, proj.usage_boost,
                        existing_boost, ", ".join(_alpha_out_names),
                    )

            logger.info(
                "[TopDown] %s: Allocation complete — skipping legacy "
                "Steps 1-2, proceeding to game context (Step 3)",
                team_name,
            )

        else:
            # ── Legacy Bottom-Up Baseline (original Steps 1-2) ────────
            # -----------------------------------------------------------------
            # Step 1: Raw baselines — reflect actual playing time.
            # No pre-normalization to 240.  Pre-normalizing inflates every
            # player when the rotation sums to < 240, then coach
            # adjustments inflate starters *again*, causing systematic
            # over-projection for top players.
            # -----------------------------------------------------------------
            baseline_projections = {
                player.player_id: self.get_baseline_projection(
                    player, recent_weight_override=recent_weight_override
                )
                for player in rotation
            }

            # ---------------------------------------------------------
            # Step 1a-pre: Undo DNP soft-decay for injury-report
            # players with active DK statuses (Q/GTD/Probable).
            # ---------------------------------------------------------
            _DK_ACTIVE_RETURN = {"Q", "QUESTIONABLE", "GTD", "P", "PROBABLE"}
            _DK_RULED_OUT = {"OUT", "O", "D", "DOUBTFUL"}
            _dk_sts = dk_injury_statuses or {}
            _matched_inj_by_id = {ip.player_id: ip for ip in matched_injuries}
            for player in rotation:
                pid = player.player_id
                dnp_streak = getattr(player, "recent_dnp_streak", 0)
                if dnp_streak < 2:
                    continue
                _mi = _matched_inj_by_id.get(pid)
                _dk_st = _dk_sts.get(pid, "")
                if _dk_st.upper() in _DK_RULED_OUT:
                    continue
                _is_dk_active = _dk_st.upper() in _DK_ACTIVE_RETURN
                _is_inj_active = (_mi and _mi.status in (
                    "Questionable", "Probable", "Game-Time Decision", "GTD",
                    "Day-To-Day",
                ))
                if _is_dk_active or _is_inj_active:
                    _old_bl = baseline_projections.get(pid, 0)
                    _undecayed = player.season_avg
                    if _old_bl < _undecayed * 0.95:
                        baseline_projections[pid] = round(_undecayed, 1)
                        logger.info(
                            f"DNP-decay override: {player.player_name} "
                            f"({dnp_streak} DNPs, status="
                            f"{'DK:' + _dk_st if _is_dk_active else _mi.status}) "
                            f"baseline {_old_bl:.1f} → {_undecayed:.1f} "
                            f"(season_avg restored)"
                        )

            # ---------------------------------------------------------
            # Step 1a-post: DK OUT overrides stale injury-report status.
            # ---------------------------------------------------------
            for mi in matched_injuries:
                _dk_st_mi = _dk_sts.get(mi.player_id, "")
                if _dk_st_mi.upper() in _DK_RULED_OUT and mi.status not in ("Out",):
                    logger.info(
                        f"DK status override: {mi.player_name} injury report "
                        f"says '{mi.status}' but DK says '{_dk_st_mi}' "
                        f"— overriding to Out"
                    )
                    mi.status = "Out"

            # ---------------------------------------------------------
            # Step 1a: Injury-return performance decay.
            # ---------------------------------------------------------
            _injured_ids_set = {ip.player_id for ip in injuries}
            for player in rotation:
                if player.player_id in _injured_ids_set:
                    continue

                bl = baseline_projections.get(player.player_id, 0)
                if bl < STAR_ANCHOR_THRESHOLD:
                    continue

                last5 = getattr(player, "minutes_last_5", None) or []
                if len(last5) < 3:
                    continue

                games_back = 0
                for m in last5:
                    if m > 0:
                        games_back += 1
                    else:
                        break

                if games_back < 1 or games_back > INJURY_RETURN_DECAY_GAMES:
                    continue
                if games_back >= len(last5):
                    continue

                remaining = last5[games_back:]
                if not any(m == 0 for m in remaining):
                    continue

                original_bl = bl
                baseline_projections[player.player_id] = round(
                    bl * INJURY_RETURN_MINUTES_REDUCTION, 1
                )
                logger.info(
                    "Injury-return decay: %s (%d games back) baseline "
                    "%.1f → %.1f (×%.2f)",
                    player.player_name, games_back, original_bl,
                    baseline_projections[player.player_id],
                    INJURY_RETURN_MINUTES_REDUCTION,
                )

            # ---------------------------------------------------------
            # Step 1b: Auto-detect suspended / inactive players.
            # ---------------------------------------------------------
            _DNP_HARD_THRESHOLD = 10
            _DNP_SOFT_THRESHOLD = 3
            _DK_MIN_SALARY_AUTOOUT = 3200
            _already_injured_ids = {ip.player_id for ip in matched_injuries}
            _DK_ACTIVE_STATUSES = {"Q", "QUESTIONABLE", "GTD", "P", "PROBABLE"}
            _dk_statuses = dk_injury_statuses or {}

            for player in rotation:
                if player.player_id in _already_injured_ids:
                    continue

                dnp_streak = getattr(player, "recent_dnp_streak", 0)
                if dnp_streak < _DNP_SOFT_THRESHOLD:
                    continue

                _dk_st = _dk_statuses.get(player.player_id, "")
                if _dk_st.upper() in _DK_ACTIVE_STATUSES:
                    _old_bl = baseline_projections.get(player.player_id, 0)
                    _undecayed_bl = player.season_avg
                    if _old_bl < _undecayed_bl * 0.5:
                        baseline_projections[player.player_id] = round(
                            _undecayed_bl, 1
                        )
                    logger.info(
                        f"Auto-Out SKIPPED: {player.player_name} has "
                        f"{dnp_streak} consecutive DNPs but DK status "
                        f"is '{_dk_st}' — treating as Questionable "
                        f"instead of Out (baseline {_old_bl:.1f} → "
                        f"{baseline_projections[player.player_id]:.1f})"
                    )
                    matched_injuries.append(
                        PlayerStatus(
                            player_id=player.player_id,
                            player_name=player.player_name,
                            status="Questionable",
                            injury_description=(
                                f"DK status: {_dk_st} "
                                f"({dnp_streak} consecutive DNPs, "
                                f"targeting return)"
                            ),
                        )
                    )
                    continue

                _dk_sal = getattr(player, "dk_salary", None)
                if dnp_streak < _DNP_HARD_THRESHOLD and _dk_sal and _dk_sal >= _DK_MIN_SALARY_AUTOOUT:
                    logger.info(
                        f"Auto-Out SKIPPED (salary): {player.player_name} has "
                        f"{dnp_streak} DNPs but DK salary ${_dk_sal:,} > "
                        f"${_DK_MIN_SALARY_AUTOOUT:,} — likely returning "
                        f"(baseline {baseline_projections.get(player.player_id, 0):.1f})"
                    )
                    continue

                logger.warning(
                    f"Auto-Out: {player.player_name} has {dnp_streak} "
                    f"consecutive DNPs (suspended/inactive?) — treating "
                    f"as Out (baseline was {baseline_projections.get(player.player_id, 0):.1f})"
                )
                matched_injuries.append(
                    PlayerStatus(
                        player_id=player.player_id,
                        player_name=player.player_name,
                        status="Out",
                        injury_description=(
                            f"Auto-detected: {dnp_streak} consecutive "
                            f"DNPs — likely suspended/inactive"
                        ),
                    )
                )

            # ---------------------------------------------------------
            # Step 2: Redistribute injured-player minutes.
            # ---------------------------------------------------------
            _inj_play_prob = None
            _inj_min_active = None
            if sport == "cbb":
                from app.config.constants import (
                    CBB_INJURY_PLAY_PROBABILITY,
                    CBB_INJURY_MINUTES_IF_ACTIVE,
                )
                _inj_play_prob = CBB_INJURY_PLAY_PROBABILITY
                _inj_min_active = CBB_INJURY_MINUTES_IF_ACTIVE

            adjusted_projections = self.redistribute_minutes(
                rotation, matched_injuries, baseline_projections,
                play_prob=_inj_play_prob,
                min_if_active_map=_inj_min_active,
            )
            # ── End of legacy Steps 1-2 ───────────────────────────────

        # -----------------------------------------------------------------
        # Step 3: Game context adjustments (blowout, B2B, pace factor).
        # Applied BEFORE coach adjustments so fatigue and blowout
        # penalties act on raw baselines, not coach-inflated minutes.
        # Coach shaping then distributes the adjusted total.
        # -----------------------------------------------------------------
        if game_info is not None:
            adjusted_projections = self.apply_game_context_adjustments(
                adjusted_projections, rotation, game_info, team_id, is_b2b,
                sport=sport,
            )

        # -----------------------------------------------------------------
        # Step 3.5: Coach adjustments (no internal normalization —
        # removed the 240 norm from apply_coach_adjustments so it
        # only shapes the distribution, not the total).
        # -----------------------------------------------------------------
        if apply_coach_adjustments:
            coach_profile = get_coach_profile(team_id)

            # ── AI: Coach pattern learning (Agent 6) ──────────────
            # If AI-adjusted coach data is available, override the
            # static multipliers.  Falls back to static profile.
            if self._coach_agent and coach_profile:
                try:
                    if self._coach_agent.is_available:
                        ai_profile = self._coach_agent.get_adjusted_profile(team_id)
                        if ai_profile and ai_profile.coach_name:
                            # Apply AI-learned deltas to the static profile
                            if ai_profile.starter_multiplier_delta:
                                coach_profile.starter_multiplier += ai_profile.starter_multiplier_delta
                            if ai_profile.bench_multiplier_delta:
                                coach_profile.bench_multiplier += ai_profile.bench_multiplier_delta
                            if ai_profile.rotation_size_adj:
                                coach_profile.rotation_size += ai_profile.rotation_size_adj
                            if ai_profile.b2b_penalty_adj:
                                coach_profile.b2b_penalty += ai_profile.b2b_penalty_adj
                            logger.info(
                                f"[AI] Coach adjustments applied for {ai_profile.coach_name}"
                            )
                except Exception as exc:
                    logger.debug(f"[AI] Coach learning agent failed: {exc}")

            adjusted_projections = self.apply_coach_adjustments(
                adjusted_projections, rotation, coach_profile
            )

        # -----------------------------------------------------------------
        # Step 3.75: Active depth filter — zero out fringe players beyond
        # the coach's historical rotation depth.  Freed minutes are
        # redistributed by Step 4's 240-min normalization.
        #
        # SKIP when USE_TOP_DOWN_MINUTES — the top-down allocator already
        # trims to rotation_depth in Phase 0b, so this would double-filter.
        # -----------------------------------------------------------------
        if self._coach_agent and sport == "nba" and not USE_TOP_DOWN_MINUTES:
            try:
                adjusted_projections = (
                    self._coach_agent.calculate_active_depth_and_filter(
                        team_id=team_id,
                        projections=adjusted_projections,
                        sport=sport,
                    )
                )
            except Exception as exc:
                logger.debug(f"[DepthFilter] Skipped: {exc}")

        # -----------------------------------------------------------------
        # Step 4: Normalize to team-minutes using weighted compression.
        # Target: 240 for NBA (48 min × 5), 200 for CBB (40 min × 5).
        #
        # Flat proportional scaling (old approach) cuts every player
        # by the same %, which under-projects starters by 3-5 min.
        # In reality, when a rotation sums to > target, it's the
        # lowest-minute players who lose time — not the stars.
        #
        # Weighted compression assigns each player an "elasticity"
        # inversely proportional to their minutes.  High-minute
        # starters are protected; low-minute bench players absorb
        # the bulk of the reduction.  This matches how coaches
        # manage minutes.
        #
        # For CBB, we override key constants from get_sport_constants().
        from app.config.constants import get_sport_constants
        _sc = get_sport_constants(sport)
        _TEAM_MINUTES = _sc.get("TOTAL_TEAM_MINUTES", TOTAL_TEAM_MINUTES)
        _ABS_MAX = _sc.get("ABSOLUTE_MAX_MINUTES", ABSOLUTE_MAX_MINUTES)
        _STAR_THRESH = _sc.get("STAR_ANCHOR_THRESHOLD", STAR_ANCHOR_THRESHOLD)
        # -----------------------------------------------------------------
        total_raw = sum(
            p.adjusted_minutes for p in adjusted_projections.values()
        )
        if total_raw > 0 and abs(total_raw - _TEAM_MINUTES) > 0.1:
            excess = total_raw - _TEAM_MINUTES
            active = [
                p for p in adjusted_projections.values()
                if p.adjusted_minutes > 0
            ]

            # ── Short-rotation guardrail ──────────────────────────
            # When injuries have thinned the active roster to ≤8
            # players, starters naturally absorb more minutes.  Lift
            # the per-player ceiling so the normalizer doesn't
            # artificially cap them at ABSOLUTE_MAX_MINUTES.
            _is_short_rotation = len(active) <= SHORT_ROTATION_SIZE
            _effective_abs_max = (
                SHORT_ROTATION_STARTER_CEILING
                if _is_short_rotation
                else _ABS_MAX
            )
            if _is_short_rotation:
                logger.info(
                    f"[Norm] Short rotation ({len(active)} active): "
                    f"starter ceiling raised to "
                    f"{SHORT_ROTATION_STARTER_CEILING:.0f} min"
                )

            if excess > 0:
                # ── Over 240: Top-Heavy Tiered Shaving ──────────
                #
                # Instead of 1/min² elasticity across everyone, shave
                # exclusively from the bottom tier first, then escalate
                # to mid-tier only when the bench is exhausted.  Locked
                # starters (≥30 min) are immune unless all lower tiers
                # are zeroed.
                #
                # Tier 3 — Bench:     < NORM_MID_THRESHOLD (< 20 min)
                # Tier 2 — Mid-tier:  20-30 min (capped at 15% cut)
                # Tier 1 — Locked:    ≥ NORM_LOCK_THRESHOLD (≥ 30 min)
                #
                # Pass 0 first reclaims any coach-inflation above
                # baseline for locked starters (same as before — this
                # is "free" excess that doesn't reduce real minutes).
                # -------------------------------------------------

                _LOCK = _sc.get("NORM_LOCK_THRESHOLD", NORM_LOCK_THRESHOLD)
                _MID = _sc.get("NORM_MID_THRESHOLD", NORM_MID_THRESHOLD)
                _MID_MAX = _sc.get("NORM_MID_MAX_CUT_PCT", NORM_MID_MAX_CUT_PCT)

                # Classify players into tiers by pre-norm minutes
                locked = [p for p in active if p.adjusted_minutes >= _LOCK]
                mid_tier = [
                    p for p in active
                    if _MID <= p.adjusted_minutes < _LOCK
                ]
                bench = [p for p in active if p.adjusted_minutes < _MID]

                remaining_excess = excess

                # ── Pass 0: Reclaim coach inflation from locked
                # starters (above baseline → give back to baseline).
                # This is free headroom — it doesn't cut real minutes.
                for p in locked:
                    if remaining_excess <= 0.1:
                        break
                    floor = p.baseline_minutes
                    if p.adjusted_minutes > floor:
                        giveback = min(
                            p.adjusted_minutes - floor,
                            remaining_excess,
                        )
                        p.adjusted_minutes = round(
                            p.adjusted_minutes - giveback, 1
                        )
                        remaining_excess -= giveback

                # ── Pass 0.5: Trade-detected player discount ──────
                # Players added via trade detection (DK lists them but
                # BDL roster doesn't include them yet) have the most
                # uncertain minutes.  They may be:
                #   - Recently traded — role not yet defined on new team
                #   - BDL roster lag — player has been here all season
                #     but BDL static data hasn't updated
                # Either way, their baseline (from previous team stats)
                # likely OVERSTATES their minutes on the new team.
                # Absorb excess from them first (up to 25% of their
                # minutes) before touching established rotation.
                _TRADE_MAX_CUT_PCT = 0.25
                trade_detected = [
                    p for p in active
                    if getattr(p, "roster_change_detected", False)
                    and p.adjusted_minutes > 0
                ]
                if remaining_excess > 0.1 and trade_detected:
                    for p in trade_detected:
                        if remaining_excess <= 0.1:
                            break
                        max_cut = p.adjusted_minutes * _TRADE_MAX_CUT_PCT
                        cut = min(remaining_excess, max_cut)
                        p.adjusted_minutes = round(
                            p.adjusted_minutes - cut, 1
                        )
                        remaining_excess -= cut
                    remaining_excess = (
                        sum(p.adjusted_minutes for p in active)
                        - _TEAM_MINUTES
                    )

                # ── Pass 1: Shave bench (< 20 min) proportionally
                # using 1/min weighting (lower minutes → more cut).
                # Bench players CAN be shaved to 0.  Previous attempts
                # to add a floor (baseline × 0.30) caused deep-bench
                # over-projection because it prevented the normalizer
                # from compressing enough, pushing excess into mid-tier
                # and inflating FP for players who won't get minutes.
                if remaining_excess > 0.1 and bench:
                    bench_weights = {
                        p.player_id: 1.0 / max(p.adjusted_minutes, 0.5)
                        for p in bench
                    }
                    total_bw = sum(bench_weights.values())
                    for p in bench:
                        if remaining_excess <= 0.1:
                            break
                        share = bench_weights[p.player_id] / total_bw
                        cut = min(
                            remaining_excess * share,
                            p.adjusted_minutes,
                        )
                        cut = max(0.0, cut)
                        p.adjusted_minutes = round(
                            p.adjusted_minutes - cut, 1
                        )
                        remaining_excess -= cut
                    # Recompute remaining after rounding
                    remaining_excess = (
                        sum(p.adjusted_minutes for p in active)
                        - _TEAM_MINUTES
                    )

                # ── Pass 2: Shave mid-tier (20-30 min) proportionally
                # Each player loses at most NORM_MID_MAX_CUT_PCT of
                # their current minutes.  Only fires when bench is
                # exhausted.
                if remaining_excess > 0.1 and mid_tier:
                    mid_weights = {
                        p.player_id: 1.0 / max(p.adjusted_minutes, 1.0)
                        for p in mid_tier
                    }
                    total_mw = sum(mid_weights.values())
                    for p in mid_tier:
                        if remaining_excess <= 0.1:
                            break
                        share = mid_weights[p.player_id] / total_mw
                        max_cut = p.adjusted_minutes * _MID_MAX
                        cut = min(remaining_excess * share, max_cut)
                        p.adjusted_minutes = round(
                            p.adjusted_minutes - cut, 1
                        )
                        remaining_excess -= cut
                    remaining_excess = (
                        sum(p.adjusted_minutes for p in active)
                        - _TEAM_MINUTES
                    )

                # ── Pass 3: Emergency — if bench + mid-tier weren't
                # enough, shave locked starters proportionally.
                # This should be rare (only on very deep teams with
                # heavy coach inflation).  Spot-starters are protected.
                if remaining_excess > 0.1 and locked:
                    spot_starter_ids = {
                        p.player_id for p in active
                        if getattr(p, "is_spot_starter", False)
                    }
                    eligible_locked = [
                        p for p in locked
                        if p.player_id not in spot_starter_ids
                    ]
                    if eligible_locked:
                        for p in eligible_locked:
                            if remaining_excess <= 0.1:
                                break
                            # Each locked player loses at most 5%
                            max_cut = p.adjusted_minutes * 0.05
                            cut = min(remaining_excess, max_cut)
                            p.adjusted_minutes = round(
                                p.adjusted_minutes - cut, 1
                            )
                            remaining_excess -= cut

                # ── Pass 4: Floor check — players compressed below
                # MIN_VIABLE are effectively DNPs.  Zero them out so
                # the micro-correction can redistribute.
                for p in active:
                    if 0 < p.adjusted_minutes < MIN_VIABLE_MINUTES:
                        p.adjusted_minutes = 0.0
                        p.reason = (
                            f"{p.reason} | Compressed out of rotation"
                            if p.reason else "Compressed out of rotation"
                        )

                logger.info(
                    f"[Norm] Tiered shaving: excess={excess:.1f}, "
                    f"locked={len(locked)}, mid={len(mid_tier)}, "
                    f"bench={len(bench)}, residual={remaining_excess:.1f}"
                )

            else:
                # ── Under 240: inflate proportionally, capped per
                # player so bench players don't inflate past a
                # realistic ceiling for their role.
                norm_factor = _TEAM_MINUTES / total_raw
                for proj in adjusted_projections.values():
                    inflated = proj.adjusted_minutes * norm_factor
                    if proj.baseline_minutes > 0:
                        role_ceiling = min(
                            proj.baseline_minutes * MAX_INFLATION_CEILING,
                            _effective_abs_max,
                        )
                        inflated = min(inflated, role_ceiling)
                    proj.adjusted_minutes = round(inflated, 1)

            # ── Micro-correction: rounding and tier caps may leave
            # us off 240.  Redistribute the residual only to players
            # who still have headroom.
            final_total = sum(
                p.adjusted_minutes for p in adjusted_projections.values()
            )
            if final_total > 0 and abs(final_total - _TEAM_MINUTES) > 0.5:
                residual = _TEAM_MINUTES - final_total
                eligible = [
                    p for p in adjusted_projections.values()
                    if p.adjusted_minutes > 0
                ]
                if residual > 0:
                    # Under 240 — give extra minutes to players with
                    # room under their role ceiling, weighted by
                    # headroom (starters absorb more).
                    MAX_PASSES = 5
                    for _pass in range(MAX_PASSES):
                        ceilings = {}
                        headroom = {}
                        for p in eligible:
                            ceiling = (
                                min(
                                    p.baseline_minutes * MAX_INFLATION_CEILING,
                                    _effective_abs_max,
                                )
                                if p.baseline_minutes > 0
                                else _effective_abs_max
                            )
                            ceilings[p.player_id] = ceiling
                            headroom[p.player_id] = max(
                                ceiling - p.adjusted_minutes, 0
                            )
                        total_headroom = sum(headroom.values())
                        if total_headroom <= 0 or residual < 0.2:
                            break

                        for p in eligible:
                            share = headroom[p.player_id] / total_headroom
                            p.adjusted_minutes = round(
                                p.adjusted_minutes + residual * share, 1
                            )
                            cap = ceilings[p.player_id]
                            if p.adjusted_minutes > cap:
                                p.adjusted_minutes = cap

                        new_total = sum(
                            p.adjusted_minutes
                            for p in adjusted_projections.values()
                        )
                        residual = _TEAM_MINUTES - new_total
                        if residual < 0.2:
                            break
                else:
                    # Over 240 — trim proportionally as last resort
                    micro = _TEAM_MINUTES / final_total
                    for proj in adjusted_projections.values():
                        proj.adjusted_minutes = round(
                            proj.adjusted_minutes * micro, 1
                        )

        positions_breakdown = {}
        for proj in adjusted_projections.values():
            pos = proj.position
            positions_breakdown[pos] = (
                positions_breakdown.get(pos, 0) + proj.adjusted_minutes
            )

        total_minutes = sum(
            p.adjusted_minutes for p in adjusted_projections.values()
        )

        return TeamRotation(
            team_id=team_id,
            team_name=team_name,
            game_date=game_date,
            projections=list(adjusted_projections.values()),
            total_minutes=round(total_minutes, 1),
            positions_breakdown=positions_breakdown,
        )
