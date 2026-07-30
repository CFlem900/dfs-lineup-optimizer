"""Service for comparing our projections to Underdog Fantasy pick'em lines.

Identifies edges (OVER/UNDER recommendations) by:
1. Fetching UD lines via :class:`UnderdogApiService`
2. Matching players to our projection data (from rotation/DFS pipeline)
3. Computing delta, confidence, and recommendation for each line
4. Sorting/filtering by edge strength
5. Building optimal multi-pick entries with correlation awareness

Underdog Fantasy Point scoring is identical to FanDuel:
    PTS × 1.0 + REB × 1.2 + AST × 1.5 + STL × 3.0 + BLK × 3.0 + TO × -1.0
"""

import logging
import math
import re
import unicodedata
from typing import Dict, List, Optional, Tuple

from app.models.underdog import (
    FLEX_PAYOUT,
    POWER_PLAY_PAYOUT,
    PickemComparison,
    PickemEntry,
    UnderdogPickemLine,
    UnderdogPickemResponse,
)
from app.services.underdog_api_service import UnderdogApiService, _normalize_name

logger = logging.getLogger(__name__)

# ── Edge thresholds ───────────────────────────────────────────────────

EDGE_STRONG_PCT = 12.0  # |delta| >= 12% → strong edge
EDGE_MODERATE_PCT = 7.0  # |delta| >= 7% → moderate edge
EDGE_SLIGHT_PCT = 3.0  # |delta| >= 3% → slight edge
SKIP_BELOW_PCT = 3.0  # |delta| < 3% → SKIP (no actionable edge)

# Confidence scaling parameters
CONFIDENCE_BASE = 0.5  # Start at 50%
CONFIDENCE_SCALE = 3.0  # How quickly confidence approaches 1.0 with delta


class UnderdogPickemService:
    """Compares our stat projections to Underdog pick'em lines.

    Usage::

        service = UnderdogPickemService(ud_api, dfs_service)
        edges = service.get_all_edges("2026-02-19", projections_dict)
        entry = service.build_optimal_entry(edges, num_picks=5)
    """

    def __init__(
        self,
        underdog_api: UnderdogApiService,
        dfs_service=None,
    ):
        self._ud_api = underdog_api
        self._dfs = dfs_service  # Optional: used for FD scoring if needed

    # ------------------------------------------------------------------
    # Public API: Edge detection
    # ------------------------------------------------------------------

    def get_all_edges(
        self,
        game_date: str,
        projections: Dict[str, Dict[str, float]],
    ) -> UnderdogPickemResponse:
        """Compare all UD lines to our projections.

        Parameters
        ----------
        game_date : str
            Date to analyse (YYYY-MM-DD).
        projections : dict
            Keyed by ``"normalized_name:TEAM"`` (lowercase),
            values are dicts of stat projections::

                {
                    "pts": 25.3, "reb": 7.1, "ast": 8.0,
                    "fg3m": 1.8, "stl": 1.2, "blk": 0.8,
                    "tov": 3.5, "fd_points": 48.2, "pra": 40.4,
                }

        Returns
        -------
        UnderdogPickemResponse
            All lines with edge comparison data, sorted by |delta_pct| descending.
        """
        slate = self._ud_api.get_pickem_lines(game_date)
        comparisons: List[PickemComparison] = []

        for line in slate.lines:
            comparison = self._compare_single(line, projections)
            if comparison is not None:
                comparisons.append(comparison)

        # Sort by absolute delta % descending (strongest edges first)
        comparisons.sort(key=lambda c: abs(c.delta_pct), reverse=True)

        # Compute summary stats
        strong = sum(1 for c in comparisons if c.edge_strength == "strong")
        moderate = sum(1 for c in comparisons if c.edge_strength == "moderate")
        slight = sum(1 for c in comparisons if c.edge_strength == "slight")
        avg_delta = (
            sum(abs(c.delta_pct) for c in comparisons) / len(comparisons)
            if comparisons
            else 0.0
        )

        return UnderdogPickemResponse(
            game_date=game_date,
            lines=comparisons,
            total_lines=len(comparisons),
            strong_edges=strong,
            moderate_edges=moderate,
            slight_edges=slight,
            avg_abs_delta_pct=round(avg_delta, 1),
        )

    def get_player_edge(
        self,
        player_name: str,
        stat: str,
        projected_val: float,
        game_date: str,
    ) -> Optional[PickemComparison]:
        """Get edge analysis for a single player/stat."""
        ud_line = self._ud_api.get_player_line(player_name, stat, game_date)
        if ud_line is None:
            return None

        return self._build_comparison(ud_line, projected_val)

    # ------------------------------------------------------------------
    # Public API: Entry building
    # ------------------------------------------------------------------

    def build_optimal_entry(
        self,
        edges: List[PickemComparison],
        num_picks: int = 5,
        strategy: str = "max_edge",
        entry_type: str = "flex",
    ) -> PickemEntry:
        """Build an optimal pick'em entry from available edges.

        Parameters
        ----------
        edges : list[PickemComparison]
            Available edges to pick from.
        num_picks : int
            Number of picks (2-6).
        strategy : str
            ``"max_edge"``     — top N by delta percentage.
            ``"diversified"``  — spread across stat categories.
            ``"correlated"``   — pick same-game stacks for variance.
        entry_type : str
            ``"flex"`` or ``"power_play"``.

        Returns
        -------
        PickemEntry
        """
        num_picks = max(2, min(6, num_picks))

        # Filter out SKIP recommendations
        actionable = [e for e in edges if e.recommendation != "SKIP"]

        if len(actionable) < num_picks:
            # Fall back to all edges if not enough actionable
            actionable = list(edges)

        if strategy == "diversified":
            selected = self._select_diversified(actionable, num_picks)
        elif strategy == "correlated":
            selected = self._select_correlated(actionable, num_picks)
        else:  # max_edge
            selected = actionable[:num_picks]

        # Compute entry metrics
        payout_table = POWER_PLAY_PAYOUT if entry_type == "power_play" else FLEX_PAYOUT
        payout = payout_table.get(len(selected), 1.0)

        avg_confidence = (
            sum(p.confidence for p in selected) / len(selected) if selected else 0.0
        )

        # Correlation score: higher if picks are from different games/teams
        teams = [p.team_abbreviation for p in selected]
        unique_teams = len(set(teams))
        correlation_score = unique_teams / len(teams) if teams else 0.0

        return PickemEntry(
            picks=selected,
            entry_type=entry_type,
            num_picks=len(selected),
            correlation_score=round(correlation_score, 2),
            expected_hit_rate=round(avg_confidence, 3),
            payout_multiplier=payout,
        )

    # ------------------------------------------------------------------
    # Internal: comparison logic
    # ------------------------------------------------------------------

    def _compare_single(
        self,
        ud_line: UnderdogPickemLine,
        projections: Dict[str, Dict[str, float]],
    ) -> Optional[PickemComparison]:
        """Compare a single UD line to our projection for that player/stat."""
        norm_name = _normalize_name(ud_line.player_name)

        # Try to find the player in our projections
        # Projections keyed by "normalized_name:TEAM"
        proj = None
        matched_key = None
        team = ud_line.team_abbreviation.upper()

        # Try exact key first
        exact_key = f"{norm_name}:{team}"
        if exact_key in projections:
            proj = projections[exact_key]
            matched_key = exact_key
        else:
            # Fuzzy: try matching by name only (team might be different abbreviation)
            for key, val in projections.items():
                key_name = key.rsplit(":", 1)[0] if ":" in key else key
                if key_name == norm_name:
                    proj = val
                    matched_key = key
                    break

        if proj is None:
            return None

        # Get our projected value for this stat
        our_val = self._get_projected_stat(proj, ud_line.stat)
        if our_val is None or our_val <= 0:
            return None

        # Sanity check: skip obvious data mismatches where our projection
        # is absurdly low relative to the UD line (e.g., 3.5 PTS vs 20.5
        # line).  This usually means wrong player matched or wrong pool.
        if ud_line.line > 0 and our_val < ud_line.line * 0.15:
            logger.info(
                f"[UnderdogPickem] Skipping likely mismatch: "
                f"{ud_line.player_name} {ud_line.stat} "
                f"our={our_val:.1f} << UD={ud_line.line:.1f}"
            )
            return None

        return self._build_comparison(ud_line, our_val)

    def _build_comparison(
        self,
        ud_line: UnderdogPickemLine,
        our_projection: float,
    ) -> PickemComparison:
        """Build a PickemComparison from a UD line and our projected value."""
        delta = our_projection - ud_line.line
        delta_pct = (delta / ud_line.line * 100) if ud_line.line > 0 else 0.0

        # Compute confidence (sigmoid-like curve based on delta magnitude)
        confidence = self._compute_confidence(delta_pct)

        # Recommendation
        recommendation = self._recommend(delta_pct, confidence)

        # Edge strength
        edge_strength = self._classify_edge(abs(delta_pct))

        return PickemComparison(
            player_name=ud_line.player_name,
            team_abbreviation=ud_line.team_abbreviation,
            stat=ud_line.stat,
            our_projection=round(our_projection, 1),
            ud_line=ud_line.line,
            delta=round(delta, 1),
            delta_pct=round(delta_pct, 1),
            confidence=round(confidence, 3),
            recommendation=recommendation,
            edge_strength=edge_strength,
            opponent=ud_line.opponent,
            position=ud_line.position,
            image_url=ud_line.image_url,
            appearance_id=ud_line.appearance_id,
        )

    def _get_projected_stat(
        self, proj: Dict[str, float], stat: str
    ) -> Optional[float]:
        """Extract our projected value for a given stat from projection dict.

        Handles composite stats like PRA (pts+reb+ast) and stl_blk (stl+blk).
        """
        # Direct stat lookup
        if stat in proj:
            return proj[stat]

        # Composite stat: PRA = pts + reb + ast
        if stat == "pra":
            pts = proj.get("pts", 0)
            reb = proj.get("reb", 0)
            ast = proj.get("ast", 0)
            if pts > 0 or reb > 0 or ast > 0:
                return pts + reb + ast
            return None

        # Composite: steals + blocks
        if stat == "stl_blk":
            stl = proj.get("stl", 0)
            blk = proj.get("blk", 0)
            if stl > 0 or blk > 0:
                return stl + blk
            return None

        # Fantasy points = FD scoring (identical to UD)
        if stat == "fantasy_points":
            return proj.get("fd_points") or proj.get("fantasy_points")

        return None

    def _compute_confidence(self, delta_pct: float) -> float:
        """Convert delta percentage to a 0–1 confidence score.

        Uses a sigmoid-like curve: confidence rises steeply at first
        then flattens as delta grows.  A 10% edge → ~0.72 confidence,
        20% edge → ~0.88.
        """
        abs_delta = abs(delta_pct)
        if abs_delta < SKIP_BELOW_PCT:
            return 0.3  # Low confidence for marginal edges

        # Sigmoid: 1 / (1 + exp(-k * (x - midpoint)))
        # Simplified: smooth ramp from 0.5 to ~0.95
        raw = 1.0 - math.exp(-abs_delta / (CONFIDENCE_SCALE * 5.0))
        return min(0.95, CONFIDENCE_BASE + raw * 0.45)

    def _recommend(self, delta_pct: float, confidence: float) -> str:
        """Determine OVER/UNDER/SKIP recommendation."""
        if abs(delta_pct) < SKIP_BELOW_PCT:
            return "SKIP"
        if delta_pct > 0:
            return "OVER"
        return "UNDER"

    def _classify_edge(self, abs_delta_pct: float) -> str:
        """Classify edge strength."""
        if abs_delta_pct >= EDGE_STRONG_PCT:
            return "strong"
        if abs_delta_pct >= EDGE_MODERATE_PCT:
            return "moderate"
        if abs_delta_pct >= EDGE_SLIGHT_PCT:
            return "slight"
        return "no_edge"

    # ------------------------------------------------------------------
    # Internal: entry building strategies
    # ------------------------------------------------------------------

    def _select_diversified(
        self, edges: List[PickemComparison], num_picks: int
    ) -> List[PickemComparison]:
        """Select picks spread across stat categories."""
        selected: List[PickemComparison] = []
        used_stats: set = set()
        used_players: set = set()

        # First pass: one pick per stat category (best edge in each)
        by_stat: Dict[str, List[PickemComparison]] = {}
        for e in edges:
            by_stat.setdefault(e.stat, []).append(e)

        for stat in by_stat:
            by_stat[stat].sort(key=lambda c: abs(c.delta_pct), reverse=True)

        for stat in sorted(by_stat.keys(), key=lambda s: abs(by_stat[s][0].delta_pct) if by_stat[s] else 0, reverse=True):
            if len(selected) >= num_picks:
                break
            for e in by_stat[stat]:
                player_key = _normalize_name(e.player_name)
                if player_key not in used_players:
                    selected.append(e)
                    used_stats.add(stat)
                    used_players.add(player_key)
                    break

        # Fill remaining with best remaining edges (no duplicate players)
        if len(selected) < num_picks:
            remaining = [
                e for e in edges
                if _normalize_name(e.player_name) not in used_players
            ]
            remaining.sort(key=lambda c: abs(c.delta_pct), reverse=True)
            for e in remaining:
                if len(selected) >= num_picks:
                    break
                selected.append(e)
                used_players.add(_normalize_name(e.player_name))

        return selected[:num_picks]

    def _select_correlated(
        self, edges: List[PickemComparison], num_picks: int
    ) -> List[PickemComparison]:
        """Select correlated picks (same-team stacks for variance).

        For GPP/power-play entries where you want high upside.
        Prioritise multiple picks from the same team (pace correlation).
        """
        # Group by team
        by_team: Dict[str, List[PickemComparison]] = {}
        for e in edges:
            by_team.setdefault(e.team_abbreviation, []).append(e)

        # Sort teams by sum of top-2 edge magnitudes
        team_scores = {}
        for team, team_edges in by_team.items():
            team_edges.sort(key=lambda c: abs(c.delta_pct), reverse=True)
            top2 = team_edges[:2]
            team_scores[team] = sum(abs(e.delta_pct) for e in top2)

        ranked_teams = sorted(team_scores.keys(), key=lambda t: team_scores[t], reverse=True)

        # Build entry: take 2 from the best team, then fill with best remaining
        selected: List[PickemComparison] = []
        used_players: set = set()

        for team in ranked_teams:
            if len(selected) >= num_picks:
                break
            for e in by_team[team]:
                if len(selected) >= num_picks:
                    break
                player_key = _normalize_name(e.player_name)
                if player_key not in used_players:
                    selected.append(e)
                    used_players.add(player_key)

        return selected[:num_picks]
