"""Agent 12 — Tournament Results Pattern Analysis.

Analyses winning DFS tournament lineups to identify patterns
in salary allocation, stacking, ownership leverage, and
positional preferences that lead to top-1% finishes.

Architecture: **Hybrid rule-based pre-computation + AI interpretation**.
``compute_field_statistics()`` does all arithmetic in Python (like
Agent 9's ``compute_accuracy_stats``), then the AI only interprets
patterns — no manual counting from raw text.

Uses **reasoning tier** — runs as background analysis job, cached 24hrs.
"""

import json
import logging
from collections import defaultdict
from typing import Dict, List, Optional

from app.models.ai import AIRequest, TournamentAnalysis
from app.services.agents.sport_context import get_sport_preamble
from app.services.ai_service import AIService

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Deterministic field statistics (computed in code, NOT by the LLM)
# ---------------------------------------------------------------------------

DK_SALARY_CAP = 50_000
SALARY_TIER_HIGH = 8_000   # >= this = "high"
SALARY_TIER_MID = 5_000    # >= this = "mid"; below = "value"


def _tier_label(salary: int) -> str:
    """Classify a player salary into high / mid / value."""
    if salary >= SALARY_TIER_HIGH:
        return "high"
    if salary >= SALARY_TIER_MID:
        return "mid"
    return "value"


def _mean(vals: List[float]) -> float:
    return round(sum(vals) / len(vals), 2) if vals else 0.0


def _median(vals: List[float]) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    n = len(s)
    if n % 2 == 1:
        return s[n // 2]
    return (s[n // 2 - 1] + s[n // 2]) / 2.0


def _normalize_name(name: str) -> str:
    """Lowercase, strip whitespace for player-name matching."""
    return (name or "").strip().lower()


def _compute_pool_stats(
    entries: List[Dict],
    player_team_map: Optional[Dict[str, str]] = None,
) -> Dict:
    """Compute salary, tier, stacking, and ownership stats for one pool.

    Each entry is ``{rank, points, lineup_data, total_salary}`` where
    ``lineup_data`` is a list of ``{roster_slot, player_name, salary, fpts}``.
    """
    if not entries:
        return {}

    # Accumulators
    slot_salaries: Dict[str, List[float]] = defaultdict(list)
    tier_counts: Dict[str, List[int]] = {"high": [], "mid": [], "value": []}
    total_salaries: List[float] = []
    all_points: List[float] = []
    player_appearances: Dict[str, int] = defaultdict(int)

    # Stacking accumulators
    has_2man: int = 0
    has_3man: int = 0
    max_stack_sizes: List[int] = []

    for entry in entries:
        lineup = entry.get("lineup_data") or []
        pts = entry.get("points", 0) or 0
        sal = entry.get("total_salary", 0) or 0

        all_points.append(float(pts))
        total_salaries.append(float(sal))

        tier_count = {"high": 0, "mid": 0, "value": 0}
        team_counts: Dict[str, int] = defaultdict(int)

        for p in lineup:
            slot = p.get("roster_slot", "UTIL")
            salary = p.get("salary", 0) or 0
            name = _normalize_name(p.get("player_name", ""))

            slot_salaries[slot].append(float(salary))
            tier_count[_tier_label(salary)] += 1
            player_appearances[name] += 1

            # Stacking: look up team
            if player_team_map and name in player_team_map:
                team_counts[player_team_map[name]] += 1

        for tier, cnt in tier_count.items():
            tier_counts[tier].append(cnt)

        # Stacking detection
        if team_counts:
            max_stack = max(team_counts.values()) if team_counts else 0
            max_stack_sizes.append(max_stack)
            if max_stack >= 2:
                has_2man += 1
            if max_stack >= 3:
                has_3man += 1

    n = len(entries)

    # Per-position salary allocation
    salary_alloc = {}
    for slot, salaries in sorted(slot_salaries.items()):
        avg = _mean(salaries)
        salary_alloc[slot] = {
            "avg_salary": avg,
            "median_salary": round(_median(salaries), 0),
            "pct_of_cap": round(avg / DK_SALARY_CAP * 100, 1),
        }

    # Salary tiers (avg count per lineup)
    tier_dist = {
        tier: round(_mean(counts), 2) for tier, counts in tier_counts.items()
    }

    # Salary utilization
    avg_total = _mean(total_salaries)
    sal_util = {
        "avg_total_salary": round(avg_total, 0),
        "avg_remaining": round(DK_SALARY_CAP - avg_total, 0),
        "pct_used": round(avg_total / DK_SALARY_CAP * 100, 1) if DK_SALARY_CAP else 0,
    }

    # Stacking
    stacking: Dict = {"data_available": bool(player_team_map)}
    if player_team_map and max_stack_sizes:
        stacking["pct_with_2man_stack"] = round(has_2man / n, 3) if n else 0
        stacking["pct_with_3man_stack"] = round(has_3man / n, 3) if n else 0
        stacking["avg_max_stack_size"] = round(_mean(max_stack_sizes), 2)

    # Ownership (player frequency)
    ownership_pcts = {
        name: round(count / n * 100, 2)
        for name, count in player_appearances.items()
    } if n else {}
    sorted_own = sorted(ownership_pcts.values(), reverse=True)
    ownership = {
        "avg_player_ownership": _mean(list(ownership_pcts.values())),
        "avg_total_lineup_ownership": round(
            _mean([
                sum(
                    ownership_pcts.get(_normalize_name(p.get("player_name", "")), 0)
                    for p in (e.get("lineup_data") or [])
                )
                for e in entries
            ]), 1
        ),
        "chalk_top3_avg": _mean(sorted_own[:3]) if len(sorted_own) >= 3 else 0,
        "unique_players": len(ownership_pcts),
    }

    # Points
    points = {
        "avg": _mean(all_points),
        "median": round(_median(all_points), 1),
        "min": round(min(all_points), 1) if all_points else 0,
        "max": round(max(all_points), 1) if all_points else 0,
    }

    return {
        "entry_count": n,
        "salary_allocation": salary_alloc,
        "salary_tiers": tier_dist,
        "salary_utilization": sal_util,
        "stacking": stacking,
        "ownership": ownership,
        "points": points,
    }


def compute_field_statistics(
    winning_entries: List[Dict],
    field_entries: List[Dict],
    player_team_map: Optional[Dict[str, str]] = None,
) -> Dict:
    """Compute deterministic tournament statistics from entry data.

    Returns a dict with winners vs field comparisons across salary
    allocation, salary tiers, salary utilization, stacking, ownership,
    and points — all computed in pure Python.

    The LLM only needs to interpret patterns, not do arithmetic.

    Parameters
    ----------
    winning_entries : list of dict
        Top 1% entries: ``{rank, points, lineup_data, total_salary}``
    field_entries : list of dict
        Sample of non-winning entries (same shape).
    player_team_map : dict, optional
        Normalized ``player_name -> team_abbreviation`` mapping for
        stacking detection.  If None, stacking stats are skipped.
    """
    winners = _compute_pool_stats(winning_entries, player_team_map)
    field = _compute_pool_stats(field_entries, player_team_map)

    # Salary allocation deltas (per position)
    sal_delta = {}
    w_alloc = winners.get("salary_allocation", {})
    f_alloc = field.get("salary_allocation", {})
    for slot in set(list(w_alloc.keys()) + list(f_alloc.keys())):
        w_avg = w_alloc.get(slot, {}).get("avg_salary", 0)
        f_avg = f_alloc.get(slot, {}).get("avg_salary", 0)
        sal_delta[slot] = round(w_avg - f_avg, 0)

    return {
        "winners": winners,
        "field": field,
        "salary_allocation_delta": sal_delta,
        "winning_threshold": winners.get("points", {}).get("min", 0),
    }


# ---------------------------------------------------------------------------
# System prompt — instructs Claude to interpret pre-computed stats
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are a DFS tournament strategy analyst.

You are given PRE-COMPUTED statistics comparing winning lineups (top 1%) to
the field from DraftKings NBA GPP tournaments.  These statistics were computed
deterministically in code and are mathematically correct — do NOT re-calculate
them.  Your job is to INTERPRET the patterns and explain WHY they matter.

Analyse these dimensions using the provided statistics:

1. **Position salary allocation**: Where do winners spend more/less than field?
2. **Salary tier preferences**: Do winners favour studs ($8K+) or value ($4-5K)?
3. **Stacking patterns** (if data available): 2-man vs 3-man stack frequency.
4. **Ownership leverage**: Are winners lower-owned (contrarian) or chalk-heavy?
5. **Game context**: Any patterns in total ownership or salary utilization?

Return JSON:
{
  "contest_count": int,
  "winning_entry_count": int,
  "patterns": [
    {
      "category": "position_bias | salary_tier | stacking | ownership_leverage | game_context",
      "pattern_key": "one of the exact calibration_adjustments keys listed below",
      "direction": "boost | reduce",
      "magnitude": float (suggested multiplier delta, e.g. 0.05 = boost 5%),
      "sample_size": int,
      "confidence": float 0.0-1.0,
      "description": "human-readable description citing specific numbers"
    }
  ],
  "salary_allocation": { "PG": 18.5, "SG": 15.2, ... },
  "stacking_summary": { "2man_same_team": 0.73, "3man_game_stack": 0.41 },
  "ownership_insights": ["Winners averaged 12% lower total ownership than field", ...],
  "calibration_adjustments": {
    "position_PG_bias": 1.03,
    "position_SG_bias": 1.0,
    "position_SF_bias": 1.0,
    "position_PF_bias": 1.0,
    "position_C_bias": 0.97,
    "salary_tier_high": 1.02,
    "salary_tier_mid": 1.0,
    "salary_tier_value": 1.05,
    "stacking_2man_weight": 1.08,
    "stacking_3man_weight": 1.0,
    "ownership_threshold_adj": 0.92,
    "game_context_high_total": 1.05,
    "game_context_b2b": 0.95,
    "game_context_blowout": 1.0
  },
  "per_adjustment_reasoning": {
    "position_PG_bias": "Winners spent 16.5% of cap at PG vs 15.6% for field...",
    "stacking_2man_weight": "73% of winners used 2-man stacks vs 65% of field..."
  },
  "reasoning": "2-3 sentence overall summary of key findings"
}

IMPORTANT — calibration_adjustments keys MUST use ONLY these exact key names:
  Position bias:    position_PG_bias, position_SG_bias, position_SF_bias, position_PF_bias, position_C_bias
  Salary tier:      salary_tier_high, salary_tier_mid, salary_tier_value
  Stacking:         stacking_2man_weight, stacking_3man_weight
  Ownership:        ownership_threshold_adj
  Game context:     game_context_high_total, game_context_b2b, game_context_blowout
Do NOT invent other key names. Use 1.0 for any category where data is insufficient.

For EVERY key in calibration_adjustments, provide a matching entry in per_adjustment_reasoning
that cites specific numbers from the pre-computed statistics.

Be data-driven. Only report patterns with sample_size >= 5 and confidence >= 0.6.
Calibration adjustments should be conservative multipliers close to 1.0 (0.85-1.15).
"""

CACHE_KEY_PREFIX = "ai:tournament_analysis:"
CACHE_TTL = 86400  # 24 hours


class TournamentAnalysisAgent:
    """AI-powered tournament result pattern analysis (Agent 12).

    Uses a hybrid architecture:
    1. ``compute_field_statistics()`` pre-computes all metrics in Python
    2. Claude Sonnet interprets patterns and generates calibrations
    3. Each calibration comes with per-key reasoning tied to data
    """

    def __init__(self, ai_service: AIService, cache_service=None):
        self._ai = ai_service
        self._cache = cache_service

    @property
    def is_available(self) -> bool:
        return self._ai.is_available

    @staticmethod
    def build_player_team_map(
        player_pool: List[Dict],
    ) -> Dict[str, str]:
        """Build normalised name -> team mapping from a player pool.

        Accepts dicts with ``player_name`` + ``team_abbreviation``
        (e.g. from PlayerPoolEntry or NBADataCache).
        """
        mapping: Dict[str, str] = {}
        for p in player_pool:
            name = _normalize_name(
                p.get("player_name") or p.get("name") or ""
            )
            team = (
                p.get("team_abbreviation")
                or p.get("team")
                or ""
            ).strip().upper()
            if name and team:
                mapping[name] = team
        return mapping

    def analyze_tournament_results(
        self,
        winning_entries: List[Dict],
        field_entries: List[Dict],
        contest_metadata: Optional[Dict] = None,
        sport: str = "nba",
        player_team_map: Optional[Dict[str, str]] = None,
    ) -> Optional[TournamentAnalysis]:
        """Analyse winning vs field lineup patterns.

        Parameters
        ----------
        winning_entries : list of dict
            Top 1% entries with lineup_data, points, rank, total_salary.
        field_entries : list of dict
            Sample of non-winning entries for comparison.
        contest_metadata : dict, optional
            contest_count, date_range, etc.
        player_team_map : dict, optional
            Normalised player_name -> team abbreviation for stacking.

        Returns
        -------
        TournamentAnalysis or None if AI unavailable.
        """
        if not self._ai.is_available or not winning_entries:
            return None

        cache_key = f"{CACHE_KEY_PREFIX}latest"
        cached = self._cache_get(cache_key)
        if cached:
            return cached

        ctx = contest_metadata or {}

        # ── Step 1: Pre-compute all statistics in Python ──────────
        stats = compute_field_statistics(
            winning_entries, field_entries, player_team_map,
        )

        # ── Step 2: Build a small sample of raw lineups for context ──
        winner_lines = []
        for entry in winning_entries[:10]:
            players = entry.get("lineup_data", [])
            player_str = ", ".join(
                f"{p.get('roster_slot', '?')} {p.get('player_name', '?')} "
                f"(${p.get('salary', 0):,})"
                for p in players
            )
            winner_lines.append(
                f"  Rank {entry.get('rank', '?')} | "
                f"{entry.get('points', 0):.1f}pts | "
                f"${entry.get('total_salary', 0):,} | {player_str}"
            )

        # ── Step 3: Build prompt with stats + sample lineups ──────
        user_prompt = (
            f"**Contests analysed**: {ctx.get('contest_count', len(winning_entries))}\n"
            f"**Date range**: {ctx.get('date_range', 'recent')}\n"
            f"**Winner entries**: {len(winning_entries)}\n"
            f"**Field entries**: {len(field_entries)}\n\n"
            f"**Pre-computed field statistics** (computed in code, verified correct):\n\n"
            f"```json\n{json.dumps(stats, indent=2)}\n```\n\n"
            f"These statistics are mathematically correct — do NOT re-calculate them.\n\n"
            f"**Sample winning lineups** (top 10 for qualitative context):\n"
            + "\n".join(winner_lines)
            + "\n\n"
            "Using the pre-computed statistics above, identify systematic patterns "
            "that distinguish winning lineups from the field. For each calibration "
            "adjustment you suggest, provide a specific reason tied to the data."
        )

        request = AIRequest(
            system_prompt=get_sport_preamble(sport) + SYSTEM_PROMPT,
            user_prompt=user_prompt,
            model_tier="reasoning",
            max_tokens=3072,
            temperature=0.3,
            response_format="json",
            agent_name="tournament_analysis",
        )

        data = self._ai.complete_json(request)
        if data is None:
            return None

        try:
            result = TournamentAnalysis(**data)
            self._cache_set(cache_key, result)
            logger.info(
                f"[TournamentAgent] {len(result.patterns)} patterns found, "
                f"{len(result.calibration_adjustments)} calibration adjustments, "
                f"{len(result.per_adjustment_reasoning)} per-key reasonings"
            )
            return result
        except Exception as exc:
            logger.warning(f"[TournamentAgent] Parse failed: {exc}")
            return None

    def get_calibration_adjustments(self) -> Optional[Dict[str, float]]:
        """Return just the calibration dict from cached analysis."""
        cached = self._cache_get(f"{CACHE_KEY_PREFIX}latest")
        if cached:
            return cached.calibration_adjustments
        return None

    # ── Cache helpers (identical pattern to BacktestingAgent) ─────

    def _cache_get(self, key: str) -> Optional[TournamentAnalysis]:
        if not self._cache:
            return None
        try:
            data = self._cache.get_sync(key)
            if data:
                parsed = json.loads(data) if isinstance(data, str) else data
                return TournamentAnalysis(**parsed)
        except Exception:
            pass
        return None

    def _cache_set(self, key: str, analysis: TournamentAnalysis):
        if not self._cache:
            return
        try:
            self._cache.set_sync(
                key, analysis.model_dump_json(), ttl=CACHE_TTL
            )
        except Exception:
            pass
