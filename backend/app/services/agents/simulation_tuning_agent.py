"""Agent 8 — Simulation Tuning Agent.

Classifies players into archetype roles (Star, Microwave Scorer, 3-and-D,
Punt Play) and assigns per-player, per-stat sigma multipliers for the
Monte Carlo simulation engine's noise model.

Architecture
~~~~~~~~~~~~
Hybrid **rule-based pre-computation + AI interpretation**, matching the
Agent 12 pattern:

1.  ``classify_player_roles()`` — deterministic Python function that
    assigns every player an archetype based on salary, projected stats,
    floor/ceiling spread, and usage indicators.
2.  ``compute_role_sigma_profiles()`` — applies archetype-specific sigma
    multiplier tables to the baseline ``STAT_NOISE_SIGMA``, producing a
    ``Dict[player_id, Dict[stat, sigma]]`` ready for the simulation engine.
3.  ``get_player_noise_profiles()`` — *optional* AI refinement.  Sends the
    pre-computed role classifications + summary stats to the LLM for
    edge-case adjustments (injury returns, role changes, B2B fatigue).

The rule-based path (steps 1-2) always runs and produces a valid result.
The AI call (step 3) refines it when available.

Nightly Automation
~~~~~~~~~~~~~~~~~~
``save_role_calibrations()`` persists the per-stat sigma multipliers to
the ``TournamentCalibration`` table (30-day expiry) so the CalibrationService
can reload them on the next ``load_calibrations()`` cycle.

Uses **default tier** (Haiku / GPT-4o-mini) — one call per batch.
"""

import json
import logging
from typing import Any, Dict, List, Literal, Optional, Tuple

from app.models.ai import AIRequest
from app.services.agents.sport_context import get_sport_preamble
from app.services.ai_service import AIService

logger = logging.getLogger(__name__)

# ── Baseline sigmas (mirror simulation_engine.STAT_NOISE_SIGMA) ────────
DEFAULT_SIGMAS: Dict[str, float] = {
    "pts": 0.15, "reb": 0.15, "ast": 0.25,
    "stl": 0.40, "blk": 0.40, "tov": 0.30, "fg3m": 0.30,
}

STAT_NAMES = list(DEFAULT_SIGMAS.keys())

# ── Role definitions ───────────────────────────────────────────────────
# Each role carries a display label and per-stat sigma multipliers.
# Multipliers are applied to DEFAULT_SIGMAS to get effective sigmas.
# Values > 1.0 widen the distribution; < 1.0 narrow it.

PlayerRole = Literal["star", "microwave", "three_and_d", "punt"]

ROLE_SIGMA_MULTIPLIERS: Dict[PlayerRole, Dict[str, float]] = {
    "star": {
        # Stars are high-floor, consistent performers.
        # Tighten all stat distributions — their production is predictable.
        "pts": 0.85, "reb": 0.85, "ast": 0.85,
        "stl": 0.90, "blk": 0.90, "tov": 0.90, "fg3m": 0.85,
    },
    "microwave": {
        # Microwave scorers have high volatility in scoring stats.
        # Boost pts/fg3m sigma; other stats stay near baseline.
        "pts": 1.25, "reb": 1.00, "ast": 1.10,
        "stl": 1.00, "blk": 1.00, "tov": 1.15, "fg3m": 1.25,
    },
    "three_and_d": {
        # 3-and-D wings: low variance overall, slight tightening.
        # Their role is narrow and predictable.
        "pts": 0.90, "reb": 0.95, "ast": 0.85,
        "stl": 0.85, "blk": 0.90, "tov": 0.85, "fg3m": 0.90,
    },
    "punt": {
        # Punt plays: maximum variance — cheap options where you're
        # betting on an outlier performance.
        "pts": 1.40, "reb": 1.30, "ast": 1.30,
        "stl": 1.20, "blk": 1.20, "tov": 1.35, "fg3m": 1.40,
    },
}

# ── Classification thresholds ──────────────────────────────────────────
# Salary thresholds (DraftKings scale)
STAR_SALARY_FLOOR = 8_000         # ≥ $8K = star candidate
PUNT_SALARY_CEILING = 4_500       # ≤ $4.5K = punt candidate
MICROWAVE_SALARY_RANGE = (4_500, 8_000)  # Mid-salary = microwave/3-and-D

# Floor-ceiling spread ratio: (ceiling - floor) / projected_fp
# High spread → volatile (microwave); low spread → consistent (3-and-D)
VOLATILITY_RATIO_THRESHOLD = 0.55  # Above = microwave, below = 3-and-D

# Star consistency gate: floor must be ≥ this fraction of projection
STAR_FLOOR_RATIO = 0.70

# Minutes threshold: below this, player is a deep bench / punt regardless
PUNT_MINUTES_CEILING = 18.0

# ── Situational adjustments ────────────────────────────────────────────
B2B_SIGMA_BOOST = 1.10           # Back-to-back fatigue adds variance
INJURY_RETURN_SIGMA_BOOST = 1.50  # First game(s) back from injury
BLOWOUT_STARTER_BOOST = 1.15     # Starters in blowout risk (may rest early)
BLOWOUT_SPREAD_THRESHOLD = 7.0   # |spread| ≥ 7 = blowout risk

# Sigma clamps (matches simulation_engine expectations)
SIGMA_MIN = 0.05
SIGMA_MAX = 0.60

# ── AI prompt (for optional refinement) ────────────────────────────────
SYSTEM_PROMPT = """You are an NBA simulation variance analyst.

You are given pre-computed player role classifications and their base sigma
multiplier profiles. Your job is to review the classifications and suggest
**targeted adjustments** for edge cases only.

Role definitions:
- Star: High salary ($8K+), high floor (≥70% of projection). Tight sigmas.
- Microwave Scorer: Mid salary ($4.5-8K), high volatility ratio. Wide scoring sigmas.
- 3-and-D: Mid salary, low volatility ratio. Tight, predictable sigmas.
- Punt Play: Low salary (<$4.5K) or low minutes (<18min). Maximum variance.

Baseline sigmas: pts: 0.15, reb: 0.15, ast: 0.25, stl: 0.40, blk: 0.40, tov: 0.30, fg3m: 0.30

Return JSON with ONLY players needing adjustments beyond the pre-computed roles:
{
  "adjustments": {
    "player_id": {
      "reason": "short explanation",
      "sigma_overrides": {"stat": sigma_value, ...}
    }
  },
  "reasoning": "Brief overall reasoning"
}

Keep all sigma values in 0.05-0.60 range.
Only adjust players where the rule-based role is clearly wrong or
where situational context (injury return, role change, matchup) warrants it.
If no adjustments needed, return empty adjustments object.
"""


# ---------------------------------------------------------------------------
# Public deterministic functions
# ---------------------------------------------------------------------------

def classify_player_role(player: Dict[str, Any]) -> PlayerRole:
    """Classify a single player into an archetype role.

    Parameters
    ----------
    player : dict
        Must contain: ``salary``, ``projected_fp``, ``floor_fp``,
        ``ceiling_fp``, ``projected_minutes``.  Optional: ``position``.

    Returns
    -------
    PlayerRole
        One of: "star", "microwave", "three_and_d", "punt".
    """
    salary = player.get("salary", 0)
    proj_fp = player.get("projected_fp", 0.0)
    floor_fp = player.get("floor_fp", 0.0)
    ceil_fp = player.get("ceiling_fp", 0.0)
    minutes = player.get("projected_minutes", 0.0)

    # ── Punt: cheap or low minutes ─────────────────────────────────
    if salary <= PUNT_SALARY_CEILING or minutes < PUNT_MINUTES_CEILING:
        return "punt"

    # ── Star: expensive + consistent ───────────────────────────────
    if salary >= STAR_SALARY_FLOOR:
        floor_ratio = floor_fp / proj_fp if proj_fp > 0 else 0
        if floor_ratio >= STAR_FLOOR_RATIO:
            return "star"
        # High salary but volatile floor → microwave (think boom-bust star)
        return "microwave"

    # ── Mid-salary: Microwave vs 3-and-D ───────────────────────────
    spread = ceil_fp - floor_fp
    vol_ratio = spread / proj_fp if proj_fp > 0 else 0

    if vol_ratio >= VOLATILITY_RATIO_THRESHOLD:
        return "microwave"

    return "three_and_d"


def classify_player_roles(
    players: List[Dict[str, Any]],
) -> Dict[int, Tuple[PlayerRole, Dict[str, Any]]]:
    """Classify a batch of players and return role + summary data.

    Returns
    -------
    Dict mapping ``player_id`` to ``(role, player_summary_dict)``.
    The summary includes the original fields plus the assigned role.
    """
    result: Dict[int, Tuple[PlayerRole, Dict[str, Any]]] = {}
    for p in players:
        pid = p.get("player_id")
        if pid is None:
            continue
        role = classify_player_role(p)
        summary = {
            "player_name": p.get("player_name", "?"),
            "position": p.get("position", "?"),
            "salary": p.get("salary", 0),
            "projected_fp": p.get("projected_fp", 0.0),
            "floor_fp": p.get("floor_fp", 0.0),
            "ceiling_fp": p.get("ceiling_fp", 0.0),
            "projected_minutes": p.get("projected_minutes", 0.0),
            "is_b2b": p.get("is_b2b", False),
            "injury_status": p.get("injury_status"),
            "vegas_spread": p.get("vegas_spread"),
            "role": role,
        }
        result[pid] = (role, summary)
    return result


def compute_role_sigma_profiles(
    role_map: Dict[int, Tuple[PlayerRole, Dict[str, Any]]],
) -> Dict[int, Dict[str, float]]:
    """Convert role classifications into per-player sigma overrides.

    Applies role multipliers to ``DEFAULT_SIGMAS``, then stacks
    situational adjustments (B2B, injury return, blowout risk).

    Returns
    -------
    Dict mapping ``player_id`` to ``{stat_name: effective_sigma}``.
    """
    overrides: Dict[int, Dict[str, float]] = {}

    for pid, (role, info) in role_map.items():
        role_mults = ROLE_SIGMA_MULTIPLIERS[role]

        # Start from role-based sigmas
        sigmas = {
            stat: DEFAULT_SIGMAS[stat] * role_mults[stat]
            for stat in STAT_NAMES
        }

        # ── Situational boosts (multiplicative) ───────────────────
        situational_mult = 1.0

        # Back-to-back fatigue
        if info.get("is_b2b"):
            situational_mult *= B2B_SIGMA_BOOST

        # Injury return (Questionable / GTD — playing through something)
        inj = info.get("injury_status")
        if inj and inj in ("Questionable", "GTD", "Game Time Decision"):
            situational_mult *= INJURY_RETURN_SIGMA_BOOST

        # Blowout risk for starters (may get pulled early → more variance)
        spread = info.get("vegas_spread")
        minutes = info.get("projected_minutes", 0.0)
        if (spread is not None
                and abs(spread) >= BLOWOUT_SPREAD_THRESHOLD
                and minutes >= 24.0):
            situational_mult *= BLOWOUT_STARTER_BOOST

        # Apply situational multiplier and clamp
        if situational_mult != 1.0:
            sigmas = {
                stat: max(SIGMA_MIN, min(SIGMA_MAX, val * situational_mult))
                for stat, val in sigmas.items()
            }
        else:
            # Still clamp even without situational boosts
            sigmas = {
                stat: max(SIGMA_MIN, min(SIGMA_MAX, val))
                for stat, val in sigmas.items()
            }

        overrides[pid] = sigmas

    return overrides


def compute_role_summary(
    role_map: Dict[int, Tuple[PlayerRole, Dict[str, Any]]],
) -> Dict[str, Any]:
    """Build a summary of role distribution for logging / AI context.

    Returns
    -------
    Dict with keys: ``role_counts``, ``role_players`` (truncated lists),
    ``avg_salary_by_role``, ``player_count``.
    """
    counts: Dict[str, int] = {"star": 0, "microwave": 0, "three_and_d": 0, "punt": 0}
    salaries: Dict[str, List[int]] = {"star": [], "microwave": [], "three_and_d": [], "punt": []}
    samples: Dict[str, List[str]] = {"star": [], "microwave": [], "three_and_d": [], "punt": []}

    for pid, (role, info) in role_map.items():
        counts[role] += 1
        salaries[role].append(info.get("salary", 0))
        if len(samples[role]) < 5:  # Keep max 5 sample names per role
            samples[role].append(info.get("player_name", str(pid)))

    avg_sal: Dict[str, float] = {}
    for role, sals in salaries.items():
        avg_sal[role] = round(sum(sals) / len(sals), 0) if sals else 0

    return {
        "player_count": len(role_map),
        "role_counts": counts,
        "avg_salary_by_role": avg_sal,
        "role_samples": samples,
    }


# ---------------------------------------------------------------------------
# Agent class
# ---------------------------------------------------------------------------

class SimulationTuningAgent:
    """Per-player simulation parameter tuning via role classification.

    Deterministic role-based sigma assignment always runs.
    Optional AI refinement adjusts edge cases when available.
    """

    def __init__(self, ai_service: AIService, cache_service=None):
        self._ai = ai_service
        self._cache = cache_service

    @property
    def is_available(self) -> bool:
        return self._ai.is_available

    # ------------------------------------------------------------------
    # Main entry point — called from _enrich_pool() in lineup optimizer
    # ------------------------------------------------------------------

    def get_player_noise_profiles(
        self,
        players: List[Dict],
        context: Optional[Dict] = None,
        sport: str = "nba",
    ) -> Dict[int, Dict[str, float]]:
        """Get per-player noise overrides for simulation.

        Always produces a result via deterministic role classification.
        Optionally refines with AI when available.

        Parameters
        ----------
        players : list of dict
            Player dicts with keys: ``player_id``, ``player_name``,
            ``position``, ``salary``, ``projected_fp``, ``floor_fp``,
            ``ceiling_fp``, ``projected_minutes``.
            Optional: ``is_b2b``, ``injury_status``, ``vegas_spread``.
        context : dict, optional
            Slate context: ``platform``, ``game_date``, ``spread``, etc.
        sport : str
            Sport identifier (default "nba").

        Returns
        -------
        Dict[int, Dict[str, float]]
            ``{player_id: {stat_name: sigma_value, ...}}``
        """
        if not players:
            return {}

        # ── Step 1: Deterministic role classification ─────────────
        role_map = classify_player_roles(players)
        if not role_map:
            return {}

        # ── Step 2: Compute sigma profiles from roles ─────────────
        overrides = compute_role_sigma_profiles(role_map)

        role_summary = compute_role_summary(role_map)
        logger.info(
            f"[SimTuning] Classified {role_summary['player_count']} players: "
            f"star={role_summary['role_counts']['star']}, "
            f"microwave={role_summary['role_counts']['microwave']}, "
            f"3d={role_summary['role_counts']['three_and_d']}, "
            f"punt={role_summary['role_counts']['punt']}"
        )

        # ── Step 3: Optional AI refinement ────────────────────────
        if self._ai.is_available:
            try:
                ai_adjustments = self._ai_refine(
                    role_map, role_summary, context or {}, sport,
                )
                if ai_adjustments:
                    overrides = self._merge_ai_adjustments(
                        overrides, ai_adjustments,
                    )
            except Exception as e:
                logger.warning(f"[SimTuning] AI refinement failed: {e}")

        logger.info(
            f"[SimTuning] {len(overrides)} players with custom noise profiles"
        )
        return overrides

    # ------------------------------------------------------------------
    # Nightly calibration: persist role-based global sigma multipliers
    # ------------------------------------------------------------------

    def compute_nightly_calibrations(
        self,
        players: List[Dict],
    ) -> Dict[str, float]:
        """Compute aggregate noise sigma calibrations for nightly persistence.

        Averages the per-role sigma multipliers across the current player
        pool to produce global ``noise_sigma_<stat>`` calibration keys
        compatible with ``CalibrationService.get_noise_overrides()``.

        Returns
        -------
        Dict[str, float]
            Calibration keys like ``noise_sigma_pts`` → multiplier (e.g. 1.05).
            Values are multipliers relative to ``DEFAULT_SIGMAS``.
        """
        if not players:
            return {}

        role_map = classify_player_roles(players)
        if not role_map:
            return {}

        profiles = compute_role_sigma_profiles(role_map)

        # Average effective sigma per stat across all players
        stat_sums: Dict[str, float] = {s: 0.0 for s in STAT_NAMES}
        count = len(profiles)
        for sigmas in profiles.values():
            for stat, val in sigmas.items():
                stat_sums[stat] += val

        calibrations: Dict[str, float] = {}
        for stat in STAT_NAMES:
            avg_sigma = stat_sums[stat] / count if count > 0 else DEFAULT_SIGMAS[stat]
            # Express as multiplier relative to baseline
            baseline = DEFAULT_SIGMAS[stat]
            multiplier = avg_sigma / baseline if baseline > 0 else 1.0
            calibrations[f"noise_sigma_{stat}"] = round(multiplier, 4)

        logger.info(
            f"[SimTuning] Nightly calibrations computed from "
            f"{count} players across {len(calibrations)} stats"
        )
        return calibrations

    # ------------------------------------------------------------------
    # Private: AI refinement call
    # ------------------------------------------------------------------

    def _ai_refine(
        self,
        role_map: Dict[int, Tuple[PlayerRole, Dict[str, Any]]],
        role_summary: Dict[str, Any],
        context: Dict,
        sport: str,
    ) -> Optional[Dict[int, Dict[str, float]]]:
        """Call AI to refine edge-case sigma assignments.

        Sends the pre-computed role summary + flagged edge cases
        (B2B, injury, blowout) for AI review.

        Returns dict of player_id → {stat: sigma} overrides, or None.
        """
        # Build edge-case list: only players with situational flags
        edge_cases = []
        for pid, (role, info) in role_map.items():
            flags = []
            if info.get("is_b2b"):
                flags.append("B2B")
            if info.get("injury_status"):
                flags.append(f"INJ:{info['injury_status']}")
            spread = info.get("vegas_spread")
            if spread is not None and abs(spread) >= BLOWOUT_SPREAD_THRESHOLD:
                flags.append(f"SPREAD:{spread:+.1f}")

            if flags:
                edge_cases.append(
                    f"  ID:{pid} {info.get('player_name', '?')} "
                    f"({info.get('position', '?')}) ${info.get('salary', 0):,} "
                    f"{info.get('projected_minutes', 0):.0f}min "
                    f"role={role} [{', '.join(flags)}]"
                )

        # If no edge cases, skip AI call entirely
        if not edge_cases:
            logger.debug("[SimTuning] No edge cases — skipping AI refinement")
            return None

        spread_info = ""
        if context.get("spread"):
            spread_info = (
                f"\nSlate context: spread={context['spread']}, "
                f"total={context.get('game_total', '?')}"
            )

        user_prompt = (
            f"Role classification summary:\n"
            f"{json.dumps(role_summary, indent=2)}\n\n"
            f"Edge-case players needing review ({len(edge_cases)}):\n"
            + "\n".join(edge_cases[:30]) + "\n"
            f"{spread_info}\n\n"
            "Review these edge cases and return sigma adjustments "
            "for players where the rule-based role assignment is insufficient."
        )

        request = AIRequest(
            system_prompt=get_sport_preamble(sport) + SYSTEM_PROMPT,
            user_prompt=user_prompt,
            model_tier="default",
            max_tokens=2048,
            temperature=0.2,
            response_format="json",
            agent_name="simulation_tuning",
        )

        data = self._ai.complete_json(request)
        if data is None:
            return None

        # Parse AI response
        adjustments = data.get("adjustments", data)
        if not isinstance(adjustments, dict):
            return None

        result: Dict[int, Dict[str, float]] = {}
        for pid_str, adj in adjustments.items():
            try:
                pid = int(pid_str)
            except (ValueError, TypeError):
                continue

            sigmas = adj if isinstance(adj, dict) else {}
            # Handle nested {"sigma_overrides": {...}, "reason": "..."}
            if "sigma_overrides" in sigmas:
                sigmas = sigmas["sigma_overrides"]

            if not isinstance(sigmas, dict):
                continue

            validated = {}
            for stat, val in sigmas.items():
                if stat in DEFAULT_SIGMAS and isinstance(val, (int, float)):
                    validated[stat] = max(SIGMA_MIN, min(SIGMA_MAX, float(val)))
            if validated:
                result[pid] = validated

        if result:
            logger.info(
                f"[SimTuning] AI refined {len(result)} player sigma profiles"
            )
        return result if result else None

    @staticmethod
    def _merge_ai_adjustments(
        base: Dict[int, Dict[str, float]],
        ai_overrides: Dict[int, Dict[str, float]],
    ) -> Dict[int, Dict[str, float]]:
        """Merge AI adjustments into the base role-computed profiles.

        AI overrides replace individual stat sigmas (not the whole profile).
        """
        merged = dict(base)
        for pid, ai_sigmas in ai_overrides.items():
            if pid in merged:
                # Overlay AI sigmas onto existing role-based profile
                merged[pid] = {**merged[pid], **ai_sigmas}
            else:
                # AI added a player not in the role map (shouldn't happen,
                # but handle gracefully)
                merged[pid] = ai_sigmas
        return merged
