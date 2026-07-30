"""Agent 3 — Dynamic Injury Impact Assessment.

Analyses cascading effects of injuries beyond simple minute
redistribution: usage rate changes, pace impacts, defensive
matchup shifts, and role reassignments.

Additionally provides a **Hierarchical Substitution Tree (DAG)** that
routes vacated minutes through position-specific cascades instead of
the legacy flat 65/35 waterfall.  The DAG is deterministic (no LLM)
and always available regardless of AI service configuration.

Uses **reasoning tier** (Sonnet / GPT-4o) — complex analysis,
called once per injured player (not per-player in the pool).
"""

import logging
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

from app.config.constants import (
    BACKUP_RECENT_WEIGHT,
    BACKUP_SEASON_WEIGHT,
    HIER_BENCH_CEILING,
    HIER_PRIMARY_SHARE,
    HIER_SECONDARY_SHARE,
    HIER_STARTER_CEILING,
    HIER_STARTER_THRESHOLD,
    POSITION_DAG_FALLBACK,
    POSITION_SUBSTITUTION_DAG,
)
from app.models.ai import AIRequest, InjuryImpactAnalysis
from app.services.ai_service import AIService
from app.services.agents.sport_context import get_sport_preamble

logger = logging.getLogger(__name__)


# ======================================================================
# Hierarchical Substitution Tree (DAG) — standalone functions
# ======================================================================
#
# These functions are module-level (not class methods) so the rotation
# engine can import and call them directly without requiring an
# InjuryImpactAgent instance or AI service.
# ======================================================================

def _resolve_cascade(position: str) -> List[Tuple[str, float]]:
    """Resolve any position string to a DAG cascade.

    Handles specific 5-position codes (PG, SG, SF, PF, C), generic
    codes (G, F, Guard, Forward, Center), and multi-position strings
    like ``"G-F"`` or ``"F-C"``.

    Parameters
    ----------
    position : str
        Player's position string (e.g. ``"PG"``, ``"G-F"``, ``"C"``).

    Returns
    -------
    list of (target_position, share) tuples
        Shares sum to 1.0.  Empty list if position is unrecognised.

    Examples
    --------
    >>> _resolve_cascade("PG")
    [("PG", 0.70), ("SG", 0.30)]

    >>> _resolve_cascade("G-F")
    [("PG", 0.35), ("SG", 0.20), ("SF", 0.35), ("PF", 0.10)]
    """
    parts = [p.strip() for p in position.split("-") if p.strip()]
    if not parts:
        return []

    # Resolve each part to a specific 5-position code
    resolved = []
    for part in parts:
        specific = POSITION_DAG_FALLBACK.get(part, part)
        if specific in POSITION_SUBSTITUTION_DAG:
            resolved.append(specific)

    if not resolved:
        return []

    # Single position — return its cascade directly
    if len(resolved) == 1:
        return list(POSITION_SUBSTITUTION_DAG[resolved[0]])

    # Multi-position — merge cascades and average overlapping targets
    merged: Dict[str, float] = defaultdict(float)
    for pos in resolved:
        cascade = POSITION_SUBSTITUTION_DAG[pos]
        for target, share in cascade:
            merged[target] += share

    # Average across the number of resolved positions
    count = len(resolved)
    for target in merged:
        merged[target] /= count

    # Renormalize to sum to 1.0
    total = sum(merged.values())
    if total <= 0:
        return []

    return [
        (target, round(share / total, 4))
        for target, share in sorted(merged.items())
    ]


def _player_fits_dag_target(
    player_position: str,
    dag_target: str,
) -> Tuple[bool, bool]:
    """Check if a player's position can fill a specific DAG target.

    Parameters
    ----------
    player_position : str
        The player's position string (e.g. ``"PG"``, ``"G"``, ``"F-C"``).
    dag_target : str
        The specific 5-position code from the DAG (e.g. ``"PG"``).

    Returns
    -------
    (fits, is_exact_match)
        ``fits``: True if the player can play the target position.
        ``is_exact_match``: True if the player's position directly
        resolves to the target (used for ranking priority).

    Notes
    -----
    A generic "G" player fits both "PG" and "SG" targets (exact match
    for both, since "G" resolves to "PG" via fallback but covers both
    guard slots).  The ``_POS_FAMILY`` grouping from the rotation engine
    is implicitly captured here.

    Examples
    --------
    >>> _player_fits_dag_target("PG", "PG")
    (True, True)

    >>> _player_fits_dag_target("SG", "PG")
    (True, False)   # SG can fill PG slot (both guards), but not exact

    >>> _player_fits_dag_target("C", "PG")
    (False, False)  # Center cannot fill point guard slot
    """
    # Map of specific positions to their positional families
    _POS_TO_FAMILY = {
        "PG": "G", "SG": "G",
        "SF": "F", "PF": "F",
        "C": "C",
    }

    parts = [p.strip() for p in player_position.split("-") if p.strip()]

    # Resolve each part to its specific code(s)
    resolved_specific = set()
    resolved_families = set()

    for part in parts:
        specific = POSITION_DAG_FALLBACK.get(part, part)
        resolved_specific.add(specific)
        # Also add family membership
        family = _POS_TO_FAMILY.get(specific)
        if family:
            resolved_families.add(family)
        # If the original part is a generic code (G, F), it covers
        # all specifics in that family
        if part in ("G", "Guard"):
            resolved_specific.update(["PG", "SG"])
            resolved_families.add("G")
        elif part in ("F", "Forward"):
            resolved_specific.update(["SF", "PF"])
            resolved_families.add("F")
        elif part in ("C", "Center"):
            resolved_specific.add("C")
            resolved_families.add("C")

    # Check exact match (specific position resolves to target)
    is_exact = dag_target in resolved_specific

    # Check family match (same positional family)
    target_family = _POS_TO_FAMILY.get(dag_target)
    fits = is_exact or (target_family is not None and target_family in resolved_families)

    return fits, is_exact


def _backup_sort_key(player) -> float:
    """Compute backup ranking score (higher = more likely to absorb minutes).

    Uses the same blended formula as ``RotationEngine.identify_backup_hierarchy()``:
    ``BACKUP_SEASON_WEIGHT * season_avg + BACKUP_RECENT_WEIGHT * recent_avg``.
    """
    recent_avg = (
        sum(player.minutes_last_5) / len(player.minutes_last_5)
        if player.minutes_last_5 else player.season_avg
    )
    return BACKUP_SEASON_WEIGHT * player.season_avg + BACKUP_RECENT_WEIGHT * recent_avg


def _allocate_to_candidates(
    candidates: List[Tuple],
    pool: float,
    target_pos: str,
    baseline_projections: Dict[int, float],
    adjusted: Dict[int, "PlayerProjection"],
    spot_start_caps: Dict[int, float],
    reduced_player_name: str,
) -> float:
    """Allocate a pool of freed minutes to a list of candidates.

    Uses ``HIER_PRIMARY_SHARE`` / ``HIER_SECONDARY_SHARE`` split from
    constants.  Returns any overflow minutes that could not be absorbed.

    Parameters
    ----------
    candidates : list of (player, is_exact_match) tuples
        Pre-sorted candidates eligible for this target position.
    pool : float
        Minutes available to distribute.
    target_pos : str
        The DAG target position (for logging/reason strings).
    baseline_projections : dict
        ``player_id → baseline_minutes``.
    adjusted : dict
        ``player_id → PlayerProjection`` (modified in-place).
    spot_start_caps : dict
        ``backup_player_id → elevated_cap``.
    reduced_player_name : str
        Name of the injured player (for reason strings).

    Returns
    -------
    float
        Overflow minutes that could not be absorbed.
    """
    overflow = 0.0
    n_candidates = len(candidates)

    for i, (candidate, _is_exact) in enumerate(candidates):
        if i == 0:
            allocation = pool * HIER_PRIMARY_SHARE
        else:
            allocation = pool * HIER_SECONDARY_SHARE / max(n_candidates - 1, 1)

        # Determine ceiling based on role
        candidate_baseline = baseline_projections.get(
            candidate.player_id, 0
        )
        if candidate.player_id in spot_start_caps:
            ceiling = max(
                spot_start_caps[candidate.player_id],
                HIER_STARTER_CEILING,
            )
        elif candidate_baseline >= HIER_STARTER_THRESHOLD:
            ceiling = HIER_STARTER_CEILING   # 38 min
        else:
            # Proportional bench ceiling: deep-bench players
            # (3-5 min baseline) can't suddenly absorb 25+ min.
            # Cap at 2× baseline with a floor of 12 min and a
            # hard cap of HIER_BENCH_CEILING.
            proportional = candidate_baseline * 2.0
            ceiling = min(
                max(proportional, 12.0),
                HIER_BENCH_CEILING,
            )

        current = adjusted[candidate.player_id].adjusted_minutes
        can_absorb = max(0.0, ceiling - current)
        actual = min(allocation, can_absorb)
        overflow += allocation - actual

        if actual > 0:
            new_minutes = round(current + actual, 1)
            adjusted[candidate.player_id].adjusted_minutes = new_minutes
            adjusted[candidate.player_id].reason = (
                f"Received {round(actual, 1)} min from "
                f"{reduced_player_name} (DAG: {target_pos})"
            )

            # Track DAG decisions on the projection for transparency
            dag_list = getattr(
                adjusted[candidate.player_id], "dag_redistributions", None
            )
            if dag_list is not None:
                dag_list.append({
                    "from_player": reduced_player_name,
                    "minutes": round(actual, 1),
                    "cascade_target": target_pos,
                    "role": "primary" if i == 0 else "secondary",
                    "ceiling": round(ceiling, 1),
                })

            logger.info(
                "[DAG]     %s %s absorbs %.1f min (%.1f → %.1f, "
                "ceiling=%.1f, baseline=%.1f)",
                "★" if i == 0 else "·",
                candidate.player_name,
                actual,
                current,
                new_minutes,
                ceiling,
                candidate_baseline,
            )
        elif allocation > 0.5:
            logger.debug(
                "[DAG]     ✗ %s at ceiling (%.1f/%.1f) — "
                "%.1f min overflow",
                candidate.player_name,
                current,
                ceiling,
                allocation - actual,
            )

    return overflow


def calculate_hierarchical_redistribution(
    rotation,
    reduced_players: Dict[int, float],
    baseline_projections: Dict[int, float],
    adjusted: Dict[int, "PlayerProjection"],
    spot_start_caps: Dict[int, float],
    all_reduced_ids: set,
) -> Dict[int, "PlayerProjection"]:
    """Redistribute freed minutes using the position-specific DAG.

    Replaces the legacy flat 65/35 waterfall in Phase 2 of
    ``RotationEngine.redistribute_minutes()``.  Routes vacated minutes
    through position-specific cascade paths (e.g. PG → backup PG 70% +
    SG 30%) with overflow spill logic and role-based ceiling caps.

    Includes **recursive overflow cascade**: when all candidates for a
    target position are at their ceiling and overflow remains, the
    overflow is routed through the target position's own DAG cascade
    (one level deep) to find adjacent absorbers.

    Parameters
    ----------
    rotation : list of PlayerMinutes
        Full team roster.
    reduced_players : dict
        ``player_id → freed_minutes`` for each injured/reduced player.
    baseline_projections : dict
        ``player_id → baseline_minutes`` (pre-injury).
    adjusted : dict
        ``player_id → PlayerProjection`` (post Phase 1 injury reductions,
        pre-redistribution).  Modified in-place and returned.
    spot_start_caps : dict
        ``backup_player_id → elevated_cap`` from Phase 1b spot-start
        detection.
    all_reduced_ids : set
        Player IDs with any injury reduction (excludes from receiving
        redistributed minutes).

    Returns
    -------
    dict
        The modified ``adjusted`` dict with redistributed minutes.
    """
    for reduced_id, freed_minutes in reduced_players.items():
        if freed_minutes <= 0:
            continue

        reduced_player = next(
            (p for p in rotation if p.player_id == reduced_id), None
        )
        if not reduced_player:
            continue

        cascade = _resolve_cascade(reduced_player.position)
        if not cascade:
            logger.info(
                "[DAG] No cascade for position %r (player %s) — "
                "%.1f min will flow to normalization",
                reduced_player.position,
                reduced_player.player_name,
                freed_minutes,
            )
            continue

        logger.info(
            "[DAG] Processing %s (%s) — %.1f freed minutes → cascade %s",
            reduced_player.player_name,
            reduced_player.position,
            freed_minutes,
            [(pos, f"{s:.0%}") for pos, s in cascade],
        )

        overflow = 0.0

        for target_pos, share in cascade:
            pool = (freed_minutes * share) + overflow
            overflow = 0.0

            if pool <= 0:
                continue

            # Find healthy candidates matching this DAG target
            candidates: List[Tuple] = []
            for p in rotation:
                if p.player_id in all_reduced_ids:
                    continue
                fits, is_exact = _player_fits_dag_target(
                    p.position, target_pos
                )
                if fits:
                    candidates.append((p, is_exact))

            logger.info(
                "[DAG]   → %s slot (%.0f%% share): %.1f min pool, "
                "%d candidates",
                target_pos,
                share * 100,
                pool,
                len(candidates),
            )

            if not candidates:
                overflow += pool
                logger.info(
                    "[DAG]     No candidates for %s — "
                    "%.1f min overflow",
                    target_pos,
                    pool,
                )
                continue

            # Sort: exact-match first, then by backup ranking (descending)
            candidates.sort(
                key=lambda x: (-int(x[1]), -_backup_sort_key(x[0]))
            )

            # Allocate minutes using configurable primary/secondary split
            overflow += _allocate_to_candidates(
                candidates=candidates,
                pool=pool,
                target_pos=target_pos,
                baseline_projections=baseline_projections,
                adjusted=adjusted,
                spot_start_caps=spot_start_caps,
                reduced_player_name=reduced_player.player_name,
            )

        # ── Recursive overflow cascade (one level deep) ──────────
        # If significant overflow remains after the primary cascade,
        # try routing it through secondary cascades from each target
        # position in the original cascade.  This captures scenarios
        # where e.g. all PGs are capped → overflow to SG → SF.
        if overflow > 1.0:
            logger.info(
                "[DAG]   %.1f min overflow — attempting recursive cascade",
                overflow,
            )
            # Collect all positions already visited to avoid loops
            visited_positions = {pos for pos, _ in cascade}
            remaining = overflow
            overflow = 0.0

            for target_pos, _ in cascade:
                if remaining <= 0.5:
                    break
                secondary_cascade = _resolve_cascade(target_pos)
                for sec_pos, sec_share in secondary_cascade:
                    if sec_pos in visited_positions:
                        continue  # Skip already-visited positions
                    visited_positions.add(sec_pos)

                    sec_pool = remaining * sec_share
                    if sec_pool <= 0.5:
                        continue

                    # Find candidates for the secondary target
                    sec_candidates: List[Tuple] = []
                    for p in rotation:
                        if p.player_id in all_reduced_ids:
                            continue
                        fits, is_exact = _player_fits_dag_target(
                            p.position, sec_pos
                        )
                        if fits:
                            sec_candidates.append((p, is_exact))

                    if not sec_candidates:
                        overflow += sec_pool
                        continue

                    sec_candidates.sort(
                        key=lambda x: (-int(x[1]), -_backup_sort_key(x[0]))
                    )

                    logger.info(
                        "[DAG]     Recursive → %s (%.0f%%): "
                        "%.1f min, %d candidates",
                        sec_pos,
                        sec_share * 100,
                        sec_pool,
                        len(sec_candidates),
                    )

                    sec_overflow = _allocate_to_candidates(
                        candidates=sec_candidates,
                        pool=sec_pool,
                        target_pos=sec_pos,
                        baseline_projections=baseline_projections,
                        adjusted=adjusted,
                        spot_start_caps=spot_start_caps,
                        reduced_player_name=reduced_player.player_name,
                    )
                    overflow += sec_overflow
                    remaining -= (sec_pool - sec_overflow)

        if overflow > 1.0:
            logger.warning(
                "[DAG] %.1f min unallocated from %s (%s) — "
                "absorbed by 240-min normalization",
                overflow,
                reduced_player.player_name,
                reduced_player.position,
            )
        else:
            logger.info(
                "[DAG] %s redistribution complete — %.1f min residual",
                reduced_player.player_name,
                overflow,
            )

    return adjusted


# ======================================================================
# AI-driven cascading injury analysis (LLM)
# ======================================================================

SYSTEM_PROMPT = """You are an NBA rotation and minutes analyst specialising in injury impact.

When a player is injured/out, analyse the CASCADING effects on the entire team:

1. **Direct replacement**: Who starts or gets the backup's minutes?
2. **Usage redistribution**: Which players see usage rate increases?
3. **Pace impact**: Does losing this player speed up or slow down the team?
4. **Role changes**: Any position shifts? (e.g., PF moves to C in small-ball)
5. **Defensive impact**: Does the opponent exploit a mismatch?

Return JSON with:
{
  "injured_player": "Player Name",
  "cascading_effects": [
    {
      "player_id": int,
      "player_name": "Name",
      "minutes_delta": float,
      "usage_delta": float,
      "role_change": "bench_to_starter" | "expanded_role" | "position_shift" | null,
      "reasoning": "Brief explanation"
    }
  ],
  "pace_impact": float,
  "defensive_shift": "description or null",
  "confidence": float 0.0-1.0
}

Be conservative. Focus on the 3-5 most affected players.
"""


class InjuryImpactAgent:
    """Analyses cascading injury effects beyond simple minute redistribution."""

    def __init__(self, ai_service: AIService):
        self._ai = ai_service

    @property
    def is_available(self) -> bool:
        return self._ai.is_available

    def analyze_injury_impact(
        self,
        injured_player: Dict,
        team_roster: List[Dict],
        team_context: Optional[Dict] = None,
        sport: str = "nba",
    ) -> Optional[InjuryImpactAnalysis]:
        """Analyse full cascading impact of an injury.

        Returns ``None`` if AI is unavailable.
        """
        if not self._ai.is_available:
            return None

        ctx = team_context or {}
        roster_lines = []
        for p in team_roster[:12]:
            roster_lines.append(
                f"  - {p.get('player_name', '?')} ({p.get('position', '?')}) "
                f"| {p.get('projected_minutes', 0):.0f} min "
                f"| USG {p.get('usage_rate', 0):.1%} "
                f"| ID {p.get('player_id', 0)}"
            )

        user_prompt = (
            f"**Injured player**: {injured_player.get('player_name', '?')} "
            f"({injured_player.get('position', '?')}) — "
            f"{injured_player.get('projected_minutes', 0):.0f} min/game, "
            f"USG {injured_player.get('usage_rate', 0):.1%}\n\n"
            f"**Team context**: Coach {ctx.get('coach_name', 'Unknown')}, "
            f"Pace {ctx.get('game_pace', 100):.0f}, "
            f"vs {ctx.get('opponent', 'Unknown')}"
            f"{', B2B' if ctx.get('is_b2b') else ''}\n\n"
            f"**Current rotation**:\n" + "\n".join(roster_lines) + "\n\n"
            f"Analyse the cascading effects of losing "
            f"{injured_player.get('player_name', '?')}."
        )

        request = AIRequest(
            system_prompt=get_sport_preamble(sport) + SYSTEM_PROMPT,
            user_prompt=user_prompt,
            model_tier="reasoning",
            max_tokens=1024,
            temperature=0.3,
            response_format="json",
            agent_name="injury_impact",
        )

        data = self._ai.complete_json(request)
        if data is None:
            return None

        try:
            return InjuryImpactAnalysis(**data)
        except Exception as exc:
            logger.warning(f"[InjuryAgent] Parse failed: {exc}")
            return None
