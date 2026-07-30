"""Vegas Line Movement Agent (Agent 14).

Analyses live Vegas odds (spreads and totals) from the BALLDONTLIE API
and dynamically adjusts player minute projections before the 240-minute
team normalization engine runs.

Two independent adjustment pipelines:

1. **Blowout Penalty** (spread-driven):
   When ``|spread| >= 7.0``, starters in the favoured team's game lose
   minutes because coaches pull them in the 4th quarter.  Those exact
   minutes are redistributed evenly to bench players, preserving the
   240-minute team constraint.

   Math::

       excess      = |spread| - 7.0
       penalty_pct = excess * 0.015

   Example — 10-point spread::

       excess      = 10.0 - 7.0 = 3.0
       penalty_pct = 3.0 * 0.015 = 0.045  (4.5%)

       Starter with 32 min: 32 × (1 - 0.045) = 30.56 min  (lost 1.44)
       If 5 starters each lose ~1.4 min → 7.2 total lost minutes
       → redistributed evenly across 5 bench players → +1.44 min each

   The penalty percentage is capped so that the starter factor never
   drops below ``BLOWOUT_MIN_FACTOR`` (0.90 = 10% max reduction).

2. **Pace Adjustment** (total-driven):
   When the over/under exceeds a configurable baseline (default 225.0),
   offensive peripheral stat rates (AST, REB) are boosted to account
   for the elevated pace and increased possession count.

   Math::

       excess_pts = over_under - 225.0      (clamped to 0 minimum)
       tiers      = excess_pts / 5.0        (one tier per 5 points)
       boost      = tiers * 0.01            (1% boost per tier)
       pace_mult  = 1.0 + boost

   Example — O/U = 240::

       excess = 240 - 225 = 15
       tiers  = 15 / 5   = 3
       boost  = 3 × 0.01 = 0.03
       pace_mult = 1.03  (3% offensive peripheral boost)

   The boost is capped at ``PACE_BOOST_MAX`` (1.08 = 8%) to prevent
   runaway projections on outlier totals.

Data Flow::

    BDL V2 /odds API
         │
         ▼
    fetch_live_odds() ─→ team-keyed spread + O/U dict
         │
         ├─→ compute_game_context_modifier_for_game()
         │        → GameContextModifier (blowout factors, pace boost)
         │        → attached to GameInfo.context_modifier
         │        → consumed by RotationEngine.apply_game_context_adjustments()
         │
         └─→ compute_minute_adjustments()
                  → Dict[int, float] (player_id → adjusted_minutes)
                  → ingested directly by 240-min normalization engine

Usage::

    agent = LineMovementAgent(ai_service, bdl_service=bdl)
    odds  = agent.fetch_live_odds("2026-02-25")
    mods  = agent.compute_minute_adjustments(roster, odds, game_lookup)
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx

from app.config import get_settings
from app.config.constants import (
    BLOWOUT_MIN_FACTOR,
    BLOWOUT_PENALTY_PER_POINT,
    BLOWOUT_PENALTY_EXPONENT,
    BLOWOUT_SPREAD_THRESHOLD,
    GAME_CONTEXT_HIGH_TOTAL_PACE_BOOST,
    GAME_CONTEXT_HIGH_TOTAL_THRESHOLD,
    LINE_MOVEMENT_CEILING_BOOST,
    LINE_MOVEMENT_CEILING_PENALTY,
    LINE_MOVEMENT_SPREAD_SIGNIFICANT,
    LINE_MOVEMENT_TOTAL_SIGNIFICANT,
    STAR_BLOWOUT_DAMPENING,
    STARTER_THRESHOLD_MINUTES,
)
from app.models.ai import (
    GameContextModifier,
    GameLineMovement,
    LineMovementAnalysis,
)

logger = logging.getLogger(__name__)


# ============================================================================
# Pace-adjustment constants (new — graduated model)
# ============================================================================
# These are local to the agent so they can be tuned without touching the
# shared constants file.  The rotation engine's binary 1.02x path at 235
# remains as the fallback when BDL odds are unavailable.

#: Baseline over/under below which no pace adjustment is applied.
#: 225.0 represents a "league-average scoring expectation" for an NBA game.
PACE_BASELINE_TOTAL: float = 225.0

#: Number of points above the baseline that constitute one adjustment tier.
#: Each tier applies 1% additional boost.
PACE_TIER_SIZE: float = 5.0

#: Boost per tier — 1% per 5-point increment above baseline.
PACE_BOOST_PER_TIER: float = 0.01

#: Maximum pace multiplier cap to prevent runaway projections on extreme totals.
#: 1.08 = 8% max boost (corresponds to O/U = 265, i.e. +40 over baseline).
PACE_BOOST_MAX: float = 1.08

#: The bench inverse-boost cap for blowout redistribution.
#: Prevents bench players from getting unrealistically inflated minutes.
BENCH_BOOST_MAX: float = 1.10


class LineMovementAgent:
    """Analyses Vegas line movements and computes minute/stat adjustments.

    This agent is **rule-based** — it accepts an ``ai_service`` for
    interface compliance but never makes LLM calls.  It is always
    available (``is_available = True``).

    Parameters
    ----------
    ai_service : optional
        Central AI service instance (accepted for DI compliance; unused).
    bdl_service : optional
        ``BallDontLieService`` instance for synchronous odds fetching
        via the resilient-HTTP layer.  When ``None``, the agent falls
        back to direct async ``httpx`` calls using the API key from
        environment settings.
    """

    # BDL V1/V2 base URLs (async httpx path when bdl_service is None)
    BDL_BASE_URL = "https://api.balldontlie.io/v1"
    BDL_BASE_URL_V2 = "https://api.balldontlie.io/v2"
    BDL_TIMEOUT = 8.0  # seconds

    def __init__(self, ai_service=None, bdl_service=None):
        self._ai = ai_service
        self._bdl = bdl_service

        # In-memory odds cache: (timestamp, date_str, result_dict)
        self._odds_cache: Optional[Tuple[float, str, Dict]] = None
        self._ODDS_CACHE_TTL = 300  # 5 minutes

        # API key for direct httpx path
        _settings = get_settings()
        self._api_key: str = _settings.balldontlie_api_key

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def is_available(self) -> bool:
        """Agent is always available (rule-based; no AI dependency)."""
        return True

    # ══════════════════════════════════════════════════════════════════
    # Section 1: Line Movement Analysis (opening vs current)
    # ══════════════════════════════════════════════════════════════════

    def analyze_line_movements(
        self,
        movements: List[Dict],
        pool_context: Optional[Dict] = None,
    ) -> Optional[LineMovementAnalysis]:
        """Analyze line movements and generate ceiling/ownership adjustments.

        Parameters
        ----------
        movements : list of dict
            Each dict has: ``home_team``, ``away_team``, ``opening_total``,
            ``current_total``, ``opening_spread``, ``current_spread``.
        pool_context : dict, optional
            Extra context (slate size, game count, etc.).

        Returns
        -------
        LineMovementAnalysis or None
        """
        if not movements:
            return None

        game_movements: List[GameLineMovement] = []
        significant_count = 0
        recommendations: List[str] = []

        for m in movements:
            gm = self._analyze_single_game(m)
            game_movements.append(gm)
            if gm.is_significant:
                significant_count += 1
                recommendations.append(gm.reasoning)

        summary = (
            f"{significant_count}/{len(movements)} games with significant "
            f"line movement"
        )

        return LineMovementAnalysis(
            game_movements=game_movements,
            significant_count=significant_count,
            summary=summary,
            recommendations=recommendations[:5],
        )

    def _analyze_single_game(self, movement: Dict) -> GameLineMovement:
        """Analyze line movement for a single game (rule-based).

        Thresholds:
        - Total delta >= 2.0 pts: significant
        - Spread delta >= 1.5 pts: significant
        - Ceiling boost: +5% when total moves up significantly
        - Ceiling penalty: -3% when total moves down significantly
        - Ownership shift: +/- 0.02 per significant total move
        """
        home = movement.get("home_team", "?")
        away = movement.get("away_team", "?")

        opening_total = movement.get("opening_total")
        current_total = movement.get("current_total")
        opening_spread = movement.get("opening_spread")
        current_spread = movement.get("current_spread")

        # Compute deltas
        total_delta = 0.0
        if opening_total is not None and current_total is not None:
            total_delta = current_total - opening_total

        spread_delta = 0.0
        if opening_spread is not None and current_spread is not None:
            spread_delta = current_spread - opening_spread

        # Significance check
        total_significant = abs(total_delta) >= LINE_MOVEMENT_TOTAL_SIGNIFICANT
        spread_significant = (
            abs(spread_delta) >= LINE_MOVEMENT_SPREAD_SIGNIFICANT
        )
        is_significant = total_significant or spread_significant

        # Ceiling adjustment + ownership shift
        ceiling_adj = 0.0
        ownership_shift = 0.0
        reasons: List[str] = []

        if total_significant:
            if total_delta > 0:
                ceiling_adj += LINE_MOVEMENT_CEILING_BOOST
                ownership_shift += 0.02
                reasons.append(
                    f"Total UP {total_delta:+.1f} pts "
                    f"({opening_total:.1f}\u2192{current_total:.1f})"
                    f" \u2014 ceiling boost for {home}/{away}"
                )
            else:
                ceiling_adj -= LINE_MOVEMENT_CEILING_PENALTY
                ownership_shift -= 0.02
                reasons.append(
                    f"Total DOWN {total_delta:+.1f} pts "
                    f"({opening_total:.1f}\u2192{current_total:.1f})"
                    f" \u2014 ceiling penalty for {home}/{away}"
                )

        if spread_significant:
            reasons.append(
                f"Spread moved {spread_delta:+.1f} pts "
                f"({opening_spread:.1f}\u2192{current_spread:.1f})"
            )

        reasoning = (
            "; ".join(reasons) if reasons else "No significant movement"
        )

        return GameLineMovement(
            home_team=home,
            away_team=away,
            opening_total=opening_total,
            current_total=current_total,
            total_delta=round(total_delta, 1),
            opening_spread=opening_spread,
            current_spread=current_spread,
            spread_delta=round(spread_delta, 1),
            is_significant=is_significant,
            ceiling_adjustment=round(ceiling_adj, 4),
            ownership_shift=round(ownership_shift, 4),
            reasoning=reasoning,
        )

    def get_game_adjustments(
        self,
        movements: List[Dict],
    ) -> Dict[str, Dict[str, float]]:
        """Return per-game ceiling/ownership adjustments for optimizer.

        Returns
        -------
        dict
            Mapping ``"HOME vs AWAY"`` to
            ``{"ceiling_adj": float, "ownership_shift": float}``.
        """
        analysis = self.analyze_line_movements(movements)
        if not analysis:
            return {}

        adjustments: Dict[str, Dict[str, float]] = {}
        for gm in analysis.game_movements:
            if gm.is_significant:
                key = f"{gm.home_team} vs {gm.away_team}"
                adjustments[key] = {
                    "ceiling_adj": gm.ceiling_adjustment,
                    "ownership_shift": gm.ownership_shift,
                }

        return adjustments

    # ══════════════════════════════════════════════════════════════════
    # Section 2: Game Context Modifier (live odds → rotation engine)
    # ══════════════════════════════════════════════════════════════════

    def compute_game_context_modifier_for_game(
        self,
        spread: float,
        over_under: float,
        home_team: str,
        away_team: str,
    ) -> GameContextModifier:
        """Compute a ``GameContextModifier`` from live Vegas odds.

        This method translates live Vegas lines into pre-computed factors
        that the ``RotationEngine`` applies during game-context adjustments,
        **before** 240-minute normalization (Step 3 of the pipeline).

        Parameters
        ----------
        spread : float
            Home-relative Vegas spread.  Negative means the home team is
            favoured (e.g. ``-7.5`` = home favoured by 7.5 points).
            This follows the BDL ``spread_home_value`` sign convention.
        over_under : float
            Vegas total (over/under) for the game.
        home_team : str
            Home team abbreviation (e.g. ``"BOS"``).
        away_team : str
            Away team abbreviation (e.g. ``"LAL"``).

        Returns
        -------
        GameContextModifier
            Fully-populated modifier ready for attachment to ``GameInfo``.

        Blowout Penalty Math
        --------------------
        Computed identically to the rotation engine's static fallback path
        so that live-odds and model-projected paths produce consistent
        results when the spread values are the same.

        ::

            abs_spread = abs(spread)

            if abs_spread >= BLOWOUT_SPREAD_THRESHOLD (7.0):
                excess         = abs_spread - 7.0
                penalty_pct    = excess * BLOWOUT_PENALTY_PER_POINT (0.015)
                starter_factor = max(BLOWOUT_MIN_FACTOR (0.90), 1.0 - penalty_pct)
                bench_factor   = min(1.0 + penalty_pct, BENCH_BOOST_MAX (1.10))

        Example — 10-point spread::

            excess         = 10.0 - 7.0  = 3.0
            penalty_pct    = 3.0 * 0.015 = 0.045  (4.5%)
            starter_factor = 1.0 - 0.045 = 0.955
            bench_factor   = 1.0 + 0.045 = 1.045

        Implied Team Totals
        --------------------
        Uses the standard sportsbook formula with BDL sign convention
        (``spread_home`` negative when home is favoured)::

            implied_home_total = (over_under - spread) / 2
            implied_away_total = (over_under + spread) / 2

        Example — O/U = 225, spread = -7.5 (home fav by 7.5)::

            home = (225 - (-7.5)) / 2 = 116.25
            away = (225 + (-7.5)) / 2 = 108.75

        Pace Modifier (graduated)
        -------------------------
        The graduated model replaces the binary 1.02x threshold:

        ::

            excess     = max(0, over_under - PACE_BASELINE_TOTAL)  # 225.0
            tiers      = excess / PACE_TIER_SIZE                   # 5.0
            boost      = tiers * PACE_BOOST_PER_TIER               # 0.01
            pace_mult  = min(1.0 + boost, PACE_BOOST_MAX)          # cap 1.08

        Example — O/U = 240::

            excess    = 240 - 225 = 15
            tiers     = 15 / 5   = 3.0
            boost     = 3 * 0.01 = 0.03
            pace_mult = 1.03  (3% peripheral stat boost)

        The ``high_total_pace_boost`` field on the returned modifier carries
        this value.  The rotation engine multiplies each player's
        ``pace_factor`` by it (Step 3b).
        """
        # ── 1. Implied team totals ────────────────────────────────────
        implied_home = round((over_under - spread) / 2, 2)
        implied_away = round((over_under + spread) / 2, 2)

        # ── 2. Blowout penalty ────────────────────────────────────────
        # penalty_pct = (|spread| - 7.0) * 0.015
        # Clamped so starter_factor never drops below 0.90 (10% max hit).
        abs_spread = abs(spread)
        is_blowout_risk = abs_spread >= BLOWOUT_SPREAD_THRESHOLD
        starter_factor = 1.0
        bench_factor = 1.0

        if is_blowout_risk:
            excess = abs_spread - BLOWOUT_SPREAD_THRESHOLD
            # Non-linear penalty: diminishing returns for large spreads.
            # A 12-pt spread (excess=5) no longer hits as hard as 5×0.015:
            #   old linear: 5 * 0.015 = 7.5%
            #   new curve:  5^0.70 * 0.015 = 5.1%
            # This matches reality — coaches pull starters in the last
            # 5-6 min of blowouts, but the first 42 min are normal.
            penalty_pct = (excess ** BLOWOUT_PENALTY_EXPONENT) * BLOWOUT_PENALTY_PER_POINT
            starter_factor = max(BLOWOUT_MIN_FACTOR, 1.0 - penalty_pct)
            # Bench inverse boost: capped at BENCH_BOOST_MAX (1.10)
            bench_factor = min(1.0 + penalty_pct, BENCH_BOOST_MAX)

        # ── 3. Graduated pace boost ──────────────────────────────────
        # For every 5 points above the 225.0 baseline, apply 1% boost
        # to pace_factor (which scales offensive peripheral stats).
        pace_boost = self._compute_pace_boost(over_under)

        # ── 4. Ceiling adjustment from line movement context ──────────
        # These are populated later by analyze_line_movements() when
        # opening vs current lines are available.  Defaults to 0.0
        # so the modifier is safe to attach even without movement data.
        ceiling_adj = 0.0
        ownership_shift = 0.0

        return GameContextModifier(
            home_team=home_team,
            away_team=away_team,
            spread=spread,
            over_under=over_under,
            implied_home_total=implied_home,
            implied_away_total=implied_away,
            blowout_factor_starters=round(starter_factor, 4),
            blowout_factor_bench=round(bench_factor, 4),
            is_blowout_risk=is_blowout_risk,
            high_total_pace_boost=pace_boost,
            ceiling_adjustment=ceiling_adj,
            ownership_shift=ownership_shift,
            source="bdl_live",
        )

    @staticmethod
    def _compute_pace_boost(over_under: float) -> float:
        """Compute the graduated pace multiplier from the game total.

        Math::

            excess = max(0, over_under - 225.0)
            tiers  = excess / 5.0
            boost  = tiers * 0.01
            result = min(1.0 + boost, 1.08)

        Returns 1.0 (no boost) when ``over_under <= PACE_BASELINE_TOTAL``.
        """
        excess = max(0.0, over_under - PACE_BASELINE_TOTAL)
        if excess <= 0:
            return 1.0
        tiers = excess / PACE_TIER_SIZE
        boost = tiers * PACE_BOOST_PER_TIER
        return round(min(1.0 + boost, PACE_BOOST_MAX), 4)

    # ══════════════════════════════════════════════════════════════════
    # Section 3: Minute Adjustment Dictionary
    #            (player_id → adjusted_minutes for 240-min engine)
    # ══════════════════════════════════════════════════════════════════

    def compute_minute_adjustments(
        self,
        roster: List[Dict[str, Any]],
        odds_by_team: Dict[str, dict],
        game_lookup: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> Dict[int, float]:
        """Compute per-player minute adjustments from live odds.

        This is the primary integration point for the 240-minute
        normalization engine.  It returns a flat dictionary mapping
        ``player_id → adjusted_minutes`` that can be ingested directly.

        For each game on the slate:

        1. **Blowout penalty** — If ``|spread| >= 7.0``, reduce starter
           minutes by ``penalty_pct`` and redistribute those *exact*
           minutes evenly across bench players.  This keeps the team
           total at 240 (the redistribution is minutes-neutral).

        2. **Pace stat boost** — Stored as metadata (not a minute change)
           via the ``pace_boost`` key in the returned per-player dict.

        Parameters
        ----------
        roster : list of dict
            Player entries.  Each dict must have at minimum::

                {
                    "player_id": int,
                    "player_name": str,
                    "team_abbreviation": str,
                    "baseline_minutes": float,   # pre-adjustment
                    "position": str,             # e.g. "PG", "SG/SF"
                }

        odds_by_team : dict
            Team-keyed odds dict from ``fetch_live_odds()``.  Each entry::

                {"spread": float, "over_under": float, ...}

        game_lookup : dict, optional
            Team → game context dict (provides ``is_starter`` flag when
            available).  If ``None``, starters are inferred from
            ``baseline_minutes >= STARTER_THRESHOLD_MINUTES``.

        Returns
        -------
        dict
            ``{player_id: adjusted_minutes}`` for every player in
            ``roster``.  Players unaffected by blowout logic retain
            their ``baseline_minutes`` unchanged.
        """
        adjustments: Dict[int, float] = {}

        if not roster or not odds_by_team:
            # No odds → return baselines unchanged
            for p in roster:
                pid = p.get("player_id", 0)
                adjustments[pid] = p.get("baseline_minutes", 0.0)
            return adjustments

        # Group roster by team
        teams: Dict[str, List[Dict[str, Any]]] = {}
        for p in roster:
            team = (p.get("team_abbreviation") or "").upper()
            teams.setdefault(team, []).append(p)

        # Process each team
        for team_abbr, players in teams.items():
            odds = odds_by_team.get(team_abbr)

            if not odds or odds.get("spread") is None:
                # No odds for this team → pass through baselines
                for p in players:
                    adjustments[p["player_id"]] = p.get(
                        "baseline_minutes", 0.0
                    )
                continue

            spread = float(odds["spread"])
            over_under = float(odds.get("over_under", 0) or 0)

            # ── Blowout penalty + redistribution ──────────────────────
            abs_spread = abs(spread)
            is_blowout = abs_spread >= BLOWOUT_SPREAD_THRESHOLD

            if is_blowout:
                self._apply_blowout_redistribution(
                    players, abs_spread, adjustments,
                )
            else:
                # No blowout → pass through baselines
                for p in players:
                    adjustments[p["player_id"]] = p.get(
                        "baseline_minutes", 0.0
                    )

        return adjustments

    def _apply_blowout_redistribution(
        self,
        players: List[Dict[str, Any]],
        abs_spread: float,
        adjustments: Dict[int, float],
    ) -> None:
        """Apply blowout penalty to starters and redistribute to bench.

        This method modifies ``adjustments`` in-place.

        Math::

            excess      = |spread| - 7.0
            penalty_pct = excess * 0.015     (capped at 10%)

            For each starter (baseline >= 24.0 min):
                lost = baseline × penalty_pct
                new  = baseline - lost

            total_lost = sum of all starter lost minutes
            per_bench  = total_lost / n_bench   (even redistribution)

            For each bench player (baseline < 24.0 min):
                new = baseline + per_bench

        The redistribution is **minutes-neutral**: the total team minutes
        are preserved exactly, satisfying the 240-minute constraint.

        Parameters
        ----------
        players : list of dict
            All players on one team.
        abs_spread : float
            Absolute value of the game spread (already >= 7.0).
        adjustments : dict
            Mutable ``{player_id: adjusted_minutes}`` dict (updated in-place).
        """
        excess = abs_spread - BLOWOUT_SPREAD_THRESHOLD
        # Non-linear penalty with diminishing returns for large spreads
        penalty_pct = min(
            (excess ** BLOWOUT_PENALTY_EXPONENT) * BLOWOUT_PENALTY_PER_POINT,
            1.0 - BLOWOUT_MIN_FACTOR,  # cap at 10%
        )

        # Classify starters vs bench, and identify top-2 stars
        starters: List[Dict[str, Any]] = []
        bench: List[Dict[str, Any]] = []
        for p in players:
            baseline = p.get("baseline_minutes", 0.0)
            if baseline >= STARTER_THRESHOLD_MINUTES:
                starters.append(p)
            else:
                bench.append(p)

        # Identify top-2 stars for dampened penalty
        starters_sorted = sorted(
            starters,
            key=lambda x: x.get("baseline_minutes", 0.0),
            reverse=True,
        )
        star_ids = {
            p["player_id"]
            for p in starters_sorted[:2]
            if p.get("baseline_minutes", 0.0) >= 28.0
        }

        # Phase 1: Penalize starters (stars get dampened penalty)
        # ─────────────────────────────────────────────────────────────
        # Role starters lose full penalty_pct of their baseline.
        # Star players (top-2, ≥28 min) lose a dampened fraction because
        # coaches leave their best players in longer than role starters.
        total_minutes_lost = 0.0
        for p in starters:
            pid = p["player_id"]
            baseline = p.get("baseline_minutes", 0.0)

            # Stars absorb less penalty (e.g., 60% of the hit)
            effective_penalty = (
                penalty_pct * STAR_BLOWOUT_DAMPENING
                if pid in star_ids
                else penalty_pct
            )

            lost = round(baseline * effective_penalty, 2)
            new_minutes = round(baseline - lost, 1)
            adjustments[pid] = new_minutes
            total_minutes_lost += lost

            logger.debug(
                "[LineMovement] Blowout penalty: %s (%.1f min) "
                "→ %.1f min (−%.1f, %.1f%% penalty%s)",
                p.get("player_name", "?"),
                baseline,
                new_minutes,
                lost,
                effective_penalty * 100,
                " [star-dampened]" if pid in star_ids else "",
            )

        # Phase 2: Redistribute lost minutes evenly to bench
        # ─────────────────────────────────────────────────────────────
        # The exact minutes removed from starters are spread evenly
        # across bench players.  This preserves the 240-minute total.
        if bench and total_minutes_lost > 0:
            per_bench = round(total_minutes_lost / len(bench), 2)
            for p in bench:
                pid = p["player_id"]
                baseline = p.get("baseline_minutes", 0.0)
                new_minutes = round(baseline + per_bench, 1)
                adjustments[pid] = new_minutes

                logger.debug(
                    "[LineMovement] Blowout redistribution: %s "
                    "(%.1f min) → %.1f min (+%.1f)",
                    p.get("player_name", "?"),
                    baseline,
                    new_minutes,
                    per_bench,
                )
        elif not bench:
            # Edge case: no bench players → starters keep reduced minutes
            # (minutes are simply lost — unlikely in practice)
            logger.warning(
                "[LineMovement] No bench players for redistribution"
            )
        else:
            # No minutes lost (shouldn't happen if is_blowout is True)
            for p in bench:
                adjustments[p["player_id"]] = p.get(
                    "baseline_minutes", 0.0
                )

        logger.info(
            "[LineMovement] Blowout redistribution: |spread|=%.1f, "
            "penalty=%.1f%%, starters=%d (lost %.1f min), bench=%d "
            "(+%.2f min each)",
            abs_spread + BLOWOUT_SPREAD_THRESHOLD,  # reconstruct original
            penalty_pct * 100,
            len(starters),
            total_minutes_lost,
            len(bench),
            (total_minutes_lost / len(bench)) if bench else 0,
        )

    # ══════════════════════════════════════════════════════════════════
    # Section 4: Pace Adjustment for Offensive Peripheral Stats
    # ══════════════════════════════════════════════════════════════════

    def compute_pace_adjustments(
        self,
        roster: List[Dict[str, Any]],
        odds_by_team: Dict[str, dict],
    ) -> Dict[int, Dict[str, float]]:
        """Compute per-player pace-driven stat multipliers.

        For every 5 points the game total exceeds the 225.0 baseline,
        offensive peripheral stats (AST, REB) receive a 1% boost.

        Parameters
        ----------
        roster : list of dict
            Player entries with ``player_id`` and ``team_abbreviation``.
        odds_by_team : dict
            Team-keyed odds from ``fetch_live_odds()``.

        Returns
        -------
        dict
            ``{player_id: {"pace_boost": float, "ast_mult": float, "reb_mult": float}}``

            - ``pace_boost``: raw multiplier (1.0 = no change, 1.03 = +3%)
            - ``ast_mult``: same value (applied to assist projections)
            - ``reb_mult``: same value (applied to rebound projections)
        """
        result: Dict[int, Dict[str, float]] = {}

        for p in roster:
            pid = p.get("player_id", 0)
            team = (p.get("team_abbreviation") or "").upper()
            odds = odds_by_team.get(team)

            if not odds or odds.get("over_under") is None:
                result[pid] = {
                    "pace_boost": 1.0,
                    "ast_mult": 1.0,
                    "reb_mult": 1.0,
                }
                continue

            over_under = float(odds["over_under"])
            boost = self._compute_pace_boost(over_under)

            result[pid] = {
                "pace_boost": boost,
                "ast_mult": boost,
                "reb_mult": boost,
            }

            if boost > 1.0:
                logger.debug(
                    "[LineMovement] Pace boost: %s (%s) — O/U=%.1f "
                    "→ %.1f%% peripheral boost",
                    p.get("player_name", "?"),
                    team,
                    over_under,
                    (boost - 1.0) * 100,
                )

        return result

    # ══════════════════════════════════════════════════════════════════
    # Section 5: BDL Live Odds Fetching
    # ══════════════════════════════════════════════════════════════════

    def fetch_live_odds(self, date_str: str) -> Dict[str, dict]:
        """Fetch live odds from BDL and return a team-keyed dict.

        Tries the injected ``BallDontLieService`` first (sync resilient-HTTP
        with circuit breaker).  Falls back to direct async ``httpx`` when
        the service is unavailable.

        Returns a dict mapping uppercase team abbreviation to::

            {
                "spread": float,        # home-relative (neg = home fav)
                "over_under": float,
                "vendor": str,
                "bdl_game_id": int,
            }

        Results are cached in-memory for 5 minutes.  Returns ``{}`` on
        any error (graceful degradation).
        """
        # Check cache first
        now = time.time()
        if self._odds_cache:
            cached_time, cached_date, cached_data = self._odds_cache
            if (
                cached_date == date_str
                and (now - cached_time) < self._ODDS_CACHE_TTL
            ):
                return cached_data

        # Try injected BDL service (sync, with circuit breaker)
        if self._bdl and self._bdl.is_available:
            result = self._fetch_odds_via_bdl_service(date_str)
            if result:
                self._odds_cache = (now, date_str, result)
                return result

        # Fallback: direct async httpx
        if self._api_key:
            import asyncio

            try:
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = None

                if loop and loop.is_running():
                    import concurrent.futures

                    with concurrent.futures.ThreadPoolExecutor() as tex:
                        result = tex.submit(
                            asyncio.run,
                            self._fetch_odds_async(date_str),
                        ).result(timeout=self.BDL_TIMEOUT + 2)
                else:
                    result = asyncio.run(
                        self._fetch_odds_async(date_str)
                    )

                if result:
                    self._odds_cache = (now, date_str, result)
                    return result
            except Exception as exc:
                logger.warning(
                    "[LineMovement] Async odds fetch failed: %s", exc
                )

        return {}

    def _fetch_odds_via_bdl_service(
        self, date_str: str
    ) -> Dict[str, dict]:
        """Fetch odds using the injected BallDontLieService (sync path).

        Joins ``get_games()`` (for team abbreviations) with
        ``get_odds_by_game()`` (for spread/total values).
        """
        try:
            # Step 1: games → game_id → team abbreviations
            games = self._bdl.get_games(date_str)
            game_teams: Dict[int, Dict[str, str]] = {}
            for g in games:
                gid = g.get("id")
                home = g.get("home_team") or {}
                visitor = g.get("visitor_team") or {}
                if gid:
                    game_teams[gid] = {
                        "home_abbr": (
                            home.get("abbreviation") or ""
                        ).upper(),
                        "away_abbr": (
                            visitor.get("abbreviation") or ""
                        ).upper(),
                    }

            # Step 2: odds (V2) → game_id → spread/total
            odds_by_game = self._bdl.get_odds_by_game(date_str)

            # Step 3: Join → team-keyed result
            result: Dict[str, dict] = {}
            for bdl_game_id, odds in odds_by_game.items():
                teams = game_teams.get(bdl_game_id)
                if not teams:
                    continue

                home_abbr = teams["home_abbr"]
                away_abbr = teams["away_abbr"]

                spread_home = odds.get("spread_home")
                if spread_home is not None:
                    try:
                        spread_home = float(spread_home)
                    except (TypeError, ValueError):
                        spread_home = None

                total = odds.get("total")
                if total is not None:
                    try:
                        total = float(total)
                    except (TypeError, ValueError):
                        total = None

                entry = {
                    "spread": spread_home,
                    "over_under": total,
                    "vendor": odds.get("vendor", ""),
                    "bdl_game_id": bdl_game_id,
                }

                if home_abbr:
                    result[home_abbr] = entry
                if away_abbr:
                    result[away_abbr] = entry

            logger.info(
                "[LineMovement] BDL live odds fetched (sync): "
                "%d games, %d team entries",
                len(odds_by_game),
                len(result),
            )
            return result

        except Exception as e:
            logger.warning(
                "[LineMovement] BDL odds fetch failed (sync): %s", e
            )
            return {}

    async def _fetch_odds_async(
        self, date_str: str
    ) -> Dict[str, dict]:
        """Fetch games + odds via async httpx (fallback when BDL service
        is unavailable).

        Uses the ``BALLDONTLIE_API_KEY`` environment variable directly.
        Enforces an 8-second timeout.
        """
        headers = {"Authorization": self._api_key}
        result: Dict[str, dict] = {}

        try:
            async with httpx.AsyncClient(
                timeout=self.BDL_TIMEOUT
            ) as client:
                # Step 1: Fetch games
                games_resp = await client.get(
                    f"{self.BDL_BASE_URL}/games?dates[]={date_str}",
                    headers=headers,
                )
                games_resp.raise_for_status()
                games_data = games_resp.json().get("data", [])

                game_teams: Dict[int, Dict[str, str]] = {}
                for g in games_data:
                    gid = g.get("id")
                    home = g.get("home_team") or {}
                    visitor = g.get("visitor_team") or {}
                    if gid:
                        game_teams[gid] = {
                            "home_abbr": (
                                home.get("abbreviation") or ""
                            ).upper(),
                            "away_abbr": (
                                visitor.get("abbreviation") or ""
                            ).upper(),
                        }

                # Step 2: Fetch odds (V2)
                odds_resp = await client.get(
                    f"{self.BDL_BASE_URL_V2}/odds"
                    f"?dates[]={date_str}&per_page=100",
                    headers=headers,
                )
                odds_resp.raise_for_status()
                odds_data = odds_resp.json().get("data", [])

                # Group by game_id, prefer DraftKings vendor
                odds_by_game: Dict[int, dict] = {}
                for entry in odds_data:
                    gid = entry.get("game_id")
                    if not gid:
                        continue
                    vendor = (entry.get("vendor") or "").lower()
                    existing = odds_by_game.get(gid)
                    # Prefer DraftKings, then first available
                    if (
                        not existing
                        or "draftkings" in vendor
                    ):
                        odds_by_game[gid] = entry

                # Step 3: Join
                for bdl_game_id, odds in odds_by_game.items():
                    teams = game_teams.get(bdl_game_id)
                    if not teams:
                        continue

                    home_abbr = teams["home_abbr"]
                    away_abbr = teams["away_abbr"]

                    spread_val = odds.get("spread_home_value")
                    if spread_val is not None:
                        try:
                            spread_val = float(spread_val)
                        except (TypeError, ValueError):
                            spread_val = None

                    total_val = odds.get("total_value")
                    if total_val is not None:
                        try:
                            total_val = float(total_val)
                        except (TypeError, ValueError):
                            total_val = None

                    entry_dict = {
                        "spread": spread_val,
                        "over_under": total_val,
                        "vendor": odds.get("vendor", ""),
                        "bdl_game_id": bdl_game_id,
                    }

                    if home_abbr:
                        result[home_abbr] = entry_dict
                    if away_abbr:
                        result[away_abbr] = entry_dict

                logger.info(
                    "[LineMovement] BDL live odds fetched (async): "
                    "%d games, %d team entries",
                    len(odds_by_game),
                    len(result),
                )

        except httpx.TimeoutException:
            logger.error(
                "[LineMovement] BDL odds timed out (%.0fs)",
                self.BDL_TIMEOUT,
            )
        except httpx.HTTPStatusError as exc:
            logger.error(
                "[LineMovement] BDL odds HTTP %d: %s",
                exc.response.status_code,
                exc,
            )
        except Exception as exc:
            logger.error(
                "[LineMovement] BDL odds unexpected error: %s", exc
            )

        return result

    # ------------------------------------------------------------------
    # Convenience lookups
    # ------------------------------------------------------------------

    def get_live_spread_for_team(
        self, date_str: str, team_abbr: str
    ) -> Optional[float]:
        """Get the live Vegas spread for a team (home-relative)."""
        odds = self.fetch_live_odds(date_str)
        entry = odds.get(team_abbr.upper())
        return entry.get("spread") if entry else None

    def get_live_over_under_for_team(
        self, date_str: str, team_abbr: str
    ) -> Optional[float]:
        """Get the live over/under for a team's game."""
        odds = self.fetch_live_odds(date_str)
        entry = odds.get(team_abbr.upper())
        return entry.get("over_under") if entry else None
