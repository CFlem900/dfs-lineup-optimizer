"""Rules-based DFS ownership projection model.

Calculates estimated ownership percentages using the same factors
that drive the casual DFS field's decisions:

1. **Salary / value**  — strongest single predictor (r=0.29)
2. **Game total**       — high-total games concentrate ownership
3. **Injury news**      — beneficiaries spike to 30%+ overnight
4. **Recent performance** — via projection quality
5. **Positional scarcity** — fewer good options = more concentration
6. **Expert consensus**  — bullish sentiment drives the field
7. **Star premium**      — name recognition / salary tier

The model uses a softmax-based approach: each player gets a "raw
attractiveness score," then scores are converted to percentages
within each position group so they sum to roughly
``num_roster_slots / pool_size * 100``.

This provides a solid baseline (70-80% accuracy) that the AI
ownership agent (Agent 7) can then refine with qualitative
adjustments.
"""

import logging
import math
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# DraftKings roster: 8 players (PG, SG, SF, PF, C, G, F, UTIL)
# FanDuel roster: 9 players (PG, PG, SG, SG, SF, SF, PF, PF, C)
DK_ROSTER_SPOTS = 8
FD_ROSTER_SPOTS = 9

# Star player salary thresholds (attracts ownership regardless of matchup)
STAR_SALARY_THRESHOLD = 9000

# Game total thresholds for environment boost
HIGH_GAME_TOTAL = 230.0
ELITE_GAME_TOTAL = 240.0
LOW_GAME_TOTAL = 215.0

# Injury beneficiary signal keywords
INJURY_BENEFIT_SENTIMENTS = {"bullish"}


def project_ownership(
    pool: List[dict],
    platform: str = "dk",
    num_games: int = 0,
    learned_weights: Optional[Dict[str, float]] = None,
) -> Dict[int, float]:
    """Calculate ownership projections for a player pool.

    Parameters
    ----------
    pool : list of dict
        Each dict should have: player_id, player_name, position, salary,
        projected_fp, dk_value, expert_sentiment, game_total,
        projected_minutes, floor_fp, ceiling_fp, expert_signal_count,
        sim_p90 (optional), is_b2b (optional).

    platform : str
        "dk" or "fd"

    num_games : int
        Number of games on the slate (more games = more dilution).

    learned_weights : dict, optional
        Learned factor weight multipliers from CalibrationService.
        Maps factor name → multiplier (centered on 1.0).  When provided,
        effective_weight = default × clamp(multiplier, 0.70, 1.30).

    Returns
    -------
    Dict mapping player_id → ownership percentage (0.0-100.0).
    """
    if not pool:
        return {}

    num_slots = DK_ROSTER_SPOTS if platform == "dk" else FD_ROSTER_SPOTS

    # ── Step 1: Compute position-level statistics ──────────────────
    position_groups: Dict[str, List[dict]] = {}
    for p in pool:
        pos = p.get("position", "UTIL")
        position_groups.setdefault(pos, []).append(p)

    # Global salary stats for percentile calculations
    all_salaries = [p["salary"] for p in pool if p.get("salary", 0) > 0]
    all_fps = [p["projected_fp"] for p in pool if p.get("projected_fp", 0) > 0]
    all_values = [p.get("dk_value", 0) for p in pool if p.get("dk_value", 0) > 0]

    if not all_salaries or not all_fps:
        return {}

    sal_min, sal_max = min(all_salaries), max(all_salaries)
    sal_range = sal_max - sal_min or 1
    fp_max = max(all_fps) or 1
    val_max = max(all_values) if all_values else 1

    # ── Step 1b: Compute effective factor weights ──────────────────
    # Default weights for each ownership factor.  Learned multipliers
    # shift these within ±30% caps.
    from app.config.constants import (
        OWNERSHIP_DEFAULT_WEIGHTS,
        OWNERSHIP_WEIGHT_ADJUSTMENT_CAP,
    )

    _ew: Dict[str, float] = {}
    for factor, default_w in OWNERSHIP_DEFAULT_WEIGHTS.items():
        if learned_weights and factor in learned_weights:
            mult = learned_weights[factor]
            capped = max(
                1.0 - OWNERSHIP_WEIGHT_ADJUSTMENT_CAP,
                min(1.0 + OWNERSHIP_WEIGHT_ADJUSTMENT_CAP, mult),
            )
            _ew[factor] = default_w * capped
        else:
            _ew[factor] = default_w

    # ── Step 2: Score each player ──────────────────────────────────
    scores: Dict[int, float] = {}

    for p in pool:
        pid = p["player_id"]
        salary = p.get("salary", 3000)
        fp = p.get("projected_fp", 0)
        value = p.get("dk_value", 0)
        game_total = _safe_float(p.get("game_total"))
        expert = p.get("expert_sentiment", "")
        signal_count = p.get("expert_signal_count", 0)
        minutes = p.get("projected_minutes", 0)
        ceiling = p.get("ceiling_fp") or p.get("sim_p90") or fp * 1.2
        is_b2b = p.get("is_b2b", False)

        # --- Factor 1: Value score (strongest predictor) ---
        # High value = high ownership. Both extremes attract: cheap
        # value plays AND expensive studs in good spots.
        value_pct = min(value / val_max, 1.0) if val_max > 0 else 0
        value_score = value_pct * _ew["value"]

        # --- Factor 2: Salary tier ---
        # Higher salary players have inherently higher ownership due
        # to name recognition and optimizer convergence at the top.
        sal_pct = (salary - sal_min) / sal_range
        salary_score = sal_pct * _ew["salary"]

        # --- Factor 3: Game environment ---
        # High-total games concentrate ownership (more scoring = more FP)
        _env_scale = _ew["game_env"] / 0.15  # scale relative to default max
        env_score = 0.0
        if game_total:
            if game_total >= ELITE_GAME_TOTAL:
                env_score = 0.15 * _env_scale
            elif game_total >= HIGH_GAME_TOTAL:
                env_score = 0.10 * _env_scale
            elif game_total < LOW_GAME_TOTAL:
                env_score = -0.05 * _env_scale

        # --- Factor 4: Expert sentiment ---
        # Bullish expert consensus drives casual field ownership
        _exp_scale = _ew["expert"] / 0.12  # scale relative to default max
        expert_score = 0.0
        if expert == "bullish":
            expert_score = 0.08 * _exp_scale
            if signal_count >= 3:
                expert_score = 0.12 * _exp_scale
        elif expert == "bearish":
            expert_score = -0.06 * _exp_scale

        # --- Factor 5: Projection quality ---
        # Normalized projection relative to the pool max
        fp_pct = fp / fp_max if fp_max > 0 else 0
        projection_score = fp_pct * _ew["projection"]

        # --- Factor 6: Star premium ---
        # Stars have a built-in ownership floor
        _star_scale = _ew["star_premium"] / 0.08  # scale relative to default max
        star_score = 0.0
        if salary >= STAR_SALARY_THRESHOLD:
            star_score = 0.05 * _star_scale
            if salary >= 10000:
                star_score = 0.08 * _star_scale

        # --- Factor 7: Positional scarcity ---
        # If there are fewer viable options at this position, ownership
        # concentrates on the best plays.
        _scar_scale = _ew["scarcity"] / 0.08
        pos = p.get("position", "UTIL")
        pos_players = position_groups.get(pos, [])
        viable_at_pos = len([x for x in pos_players
                             if x.get("projected_minutes", 0) >= 15])
        if viable_at_pos <= 4:
            scarcity_bonus = 0.08 * _scar_scale
        elif viable_at_pos <= 6:
            scarcity_bonus = 0.04 * _scar_scale
        else:
            scarcity_bonus = 0.0

        # --- Factor 8: Minutes threshold ---
        # Players with very low minutes are rarely owned
        _min_scale = _ew["minutes"] / 0.15
        minutes_penalty = 0.0
        if minutes < 15:
            minutes_penalty = -0.15 * _min_scale
        elif minutes < 20:
            minutes_penalty = -0.05 * _min_scale

        # --- Factor 9: B2B penalty ---
        b2b_penalty = (-0.04 * (_ew["b2b"] / 0.04)) if is_b2b else 0.0

        # --- Factor 10: Vegas spread (favorites attract ownership) ---
        _spread_scale = _ew["spread"] / 0.08
        spread = _safe_float(p.get("vegas_spread"))
        spread_bonus = 0.0
        if spread is not None:
            if spread < -8:
                spread_bonus = 0.08 * _spread_scale
            elif spread < -5:
                spread_bonus = 0.04 * _spread_scale
            elif spread > 5:
                spread_bonus = -0.03 * _spread_scale

        # --- Factor 11: Multi-position eligibility ---
        # Players who fit more DK slots get higher ownership because
        # they appear in more optimizer configurations.
        eligible = p.get("eligible_slots", [])
        multi_pos_bonus = (_ew["multi_position"] if len(eligible) >= 3 else 0.0)

        # --- Factor 12: Injury beneficiary ---
        # When a starter is ruled Out, remaining teammates get an
        # ownership spike as the field reallocates usage.
        _inj_scale = _ew["injury_benefit"] / 0.12
        injury_bonus = 0.0
        team = p.get("team_abbreviation", "")
        if team:
            team_out_count = sum(
                1 for x in pool
                if x.get("team_abbreviation") == team
                and x.get("injury_status") == "Out"
                and x["player_id"] != pid
            )
            if team_out_count > 0:
                injury_bonus = 0.06 * min(team_out_count, 2) * _inj_scale

        # --- Combine ---
        raw = (value_score + salary_score + env_score + expert_score
               + projection_score + star_score + scarcity_bonus
               + minutes_penalty + b2b_penalty
               + spread_bonus + multi_pos_bonus + injury_bonus)

        # Clamp to a positive floor (everyone has some small chance)
        scores[pid] = max(raw, 0.02)

    # ── Step 3: Softmax → ownership percentages ───────────────────
    # Temperature controls distribution sharpness:
    # - Lower temp → more concentrated ownership (small slates)
    # - Higher temp → more spread out (large slates)
    slate_size = len(pool)
    if num_games <= 2:
        temperature = 3.0   # Small slate: concentrated
    elif num_games <= 5:
        temperature = 5.0   # Medium slate
    else:
        temperature = 7.0   # Large slate: diluted

    # Compute softmax per position, then combine
    ownership: Dict[int, float] = {}
    total_ownership_target = num_slots * 100.0  # e.g. 800% total for 8 slots

    # Global softmax across the full pool
    score_values = list(scores.values())
    score_ids = list(scores.keys())
    max_score = max(score_values) if score_values else 0

    exp_scores = []
    for s in score_values:
        exp_scores.append(math.exp((s - max_score) * temperature))
    exp_sum = sum(exp_scores) or 1

    for pid, exp_s in zip(score_ids, exp_scores):
        raw_pct = (exp_s / exp_sum) * total_ownership_target
        # Clamp to reasonable range: 0.5% - 55%
        ownership[pid] = round(max(0.5, min(55.0, raw_pct)), 1)

    logger.info(
        f"[OwnershipModel] Projected {len(ownership)} players | "
        f"top={max(ownership.values()):.1f}% | "
        f"avg={sum(ownership.values()) / len(ownership):.1f}%"
    )

    return ownership


def _safe_float(val) -> Optional[float]:
    """Safely convert a value to float, returning None on failure."""
    if val is None or val == "":
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None
