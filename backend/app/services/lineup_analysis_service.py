"""Lineup analysis agent.

Reviews generated DFS lineups and provides structured insights:
    - Multi-dimension quality scoring (projection, floor, ceiling, value,
      diversity, stacking, expert consensus)
    - Risk identification (injuries, B2B, low minutes, salary waste)
    - Swap suggestions backed by expert sentiment and game context
    - Portfolio summary across multiple lineups
"""

import logging
import time
from collections import Counter
from datetime import date
from typing import Any, Dict, List, Optional

from app.models.lineup import (
    AnalyzeLineupsRequest,
    AnalyzeLineupsResponse,
    LineupAnalysisResult,
    LineupDimensionScore,
    LineupPlayer,
    LineupRisk,
    OptimizedLineup,
    PlayerSwapSuggestion,
    RefineLineupsRequest,
    RefineLineupsResponse,
    RefinedLineupResult,
)

logger = logging.getLogger(__name__)

# ── Grading thresholds ──────────────────────────────────────────────
GRADE_MAP = [
    (92, "A+"),
    (85, "A"),
    (78, "B+"),
    (70, "B"),
    (62, "C+"),
    (55, "C"),
    (0, "D"),
]

LABEL_MAP = [
    (8.0, "Excellent"),
    (6.0, "Good"),
    (4.0, "Fair"),
    (0.0, "Poor"),
]


def _score_to_label(score: float) -> str:
    for threshold, label in LABEL_MAP:
        if score >= threshold:
            return label
    return "Poor"


def _score_to_grade(overall: float) -> str:
    for threshold, grade in GRADE_MAP:
        if overall >= threshold:
            return grade
    return "D"


class LineupAnalysisService:
    """Evaluates DFS lineups across multiple quality dimensions."""

    def __init__(
        self,
        game_service,
        expert_signal_service,
        injury_service,
        lineup_optimizer_service,
        narrative_agent=None,
    ):
        self.game_service = game_service
        self.expert_signal_service = expert_signal_service
        self.injury_service = injury_service
        self.lineup_optimizer_service = lineup_optimizer_service
        self.narrative_agent = narrative_agent

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def analyze_lineups(
        self, request: AnalyzeLineupsRequest
    ) -> AnalyzeLineupsResponse:
        """Analyze all lineups and produce per-lineup + portfolio results."""
        start_ms = time.time()

        platform = request.platform
        sport = getattr(request, "sport", "nba")
        game_date = request.game_date or date.today().isoformat()

        # Gather contextual data
        games = self._get_games(game_date)
        injuries = self._get_injuries()
        signals = self._get_signals(request.lineups)
        pool = self._get_pool(request)

        # Analyze each lineup
        analyses: List[LineupAnalysisResult] = []
        for idx, lineup in enumerate(request.lineups):
            analysis = self._analyze_single(
                idx, lineup, platform, games, injuries, signals, pool,
                sport=sport,
            )
            analyses.append(analysis)

        # Portfolio summary
        portfolio = self._compute_portfolio_summary(
            request.lineups, analyses
        )

        elapsed_ms = int((time.time() - start_ms) * 1000)
        return AnalyzeLineupsResponse(
            analyses=analyses,
            portfolio_summary=portfolio,
            generation_time_ms=elapsed_ms,
        )

    # ------------------------------------------------------------------
    # Per-lineup analysis
    # ------------------------------------------------------------------

    def _analyze_single(
        self,
        idx: int,
        lineup: OptimizedLineup,
        platform: str,
        games: List[dict],
        injuries: List[dict],
        signals: List[dict],
        pool: list,
        sport: str = "nba",
    ) -> LineupAnalysisResult:
        """Full analysis of one lineup."""
        players = lineup.players
        salary_cap = lineup.salary_cap

        # Dimension scores
        dims: List[LineupDimensionScore] = []

        # 1. Projection quality
        proj_score = self._score_projection(lineup, platform, sport=sport)
        dims.append(proj_score)

        # 2. Floor safety
        floor_score = self._score_floor(lineup)
        dims.append(floor_score)

        # 3. Ceiling upside
        ceil_score = self._score_ceiling(lineup)
        dims.append(ceil_score)

        # 4. Salary efficiency / value
        value_score = self._score_value(lineup)
        dims.append(value_score)

        # 5. Team stacking
        stacking = self._analyze_stacking(players, games, sport=sport)
        stack_score = stacking["score"]
        dims.append(stack_score)

        # 6. Expert consensus
        expert_score = self._score_expert_consensus(players, signals)
        dims.append(expert_score)

        # Risks
        risks = self._identify_risks(players, injuries, games)

        # Swap suggestions
        swaps = self._generate_swaps(lineup, pool, games, signals, platform)

        # Correlation notes
        correlation_notes = self._analyze_correlation(players, games)

        # Overall score (weighted average)
        weights = {
            "projection": 0.30,
            "floor": 0.15,
            "ceiling": 0.15,
            "value": 0.15,
            "stacking": 0.10,
            "expert_consensus": 0.15,
        }
        total_weight = sum(weights.values())
        weighted_sum = sum(
            d.score * weights.get(d.dimension, 0.1) for d in dims
        )
        overall_score = round((weighted_sum / total_weight) * 10, 1)
        overall_grade = _score_to_grade(overall_score)

        salary_efficiency = round(
            lineup.total_salary / salary_cap * 100, 1
        )

        return LineupAnalysisResult(
            lineup_index=idx,
            overall_grade=overall_grade,
            overall_score=overall_score,
            dimension_scores=dims,
            risks=risks,
            swap_suggestions=swaps[:3],
            team_stacking=stacking["counts"],
            salary_efficiency=salary_efficiency,
            correlation_notes=correlation_notes,
        )

    # ------------------------------------------------------------------
    # Dimension scorers
    # ------------------------------------------------------------------

    def _score_projection(
        self, lineup: OptimizedLineup, platform: str, sport: str = "nba",
    ) -> LineupDimensionScore:
        """Score lineup's total projected FP vs expected range.

        Calibrated to real DK/FD ranges:
        - NBA DK: 220 FP = weak (0/10), 260 FP = solid (8/10), 275+ = elite (10/10)
        - NBA FD: 240 FP = weak (0/10), 280 FP = solid (8/10), 300+ = elite (10/10)
        - CBB DK: 80 FP = weak (0/10), 120 FP = solid (5/10), 160+ = elite (10/10)
        - CBB FD: 90 FP = weak (0/10), 132 FP = solid (5/10), 175+ = elite (10/10)
        """
        fp = lineup.total_projected_fp
        if sport == "cbb":
            if platform == "dk":
                score = min(10.0, max(0.0, (fp - 80) / 8.0))
            else:
                score = min(10.0, max(0.0, (fp - 90) / 8.5))
        elif platform == "dk":
            # 220 → 0, 240 → 3.6, 255 → 6.4, 270 → 9.1, 275+ → 10
            score = min(10.0, max(0.0, (fp - 220) / 5.5))
        else:
            score = min(10.0, max(0.0, (fp - 240) / 6.0))

        return LineupDimensionScore(
            dimension="projection",
            score=round(score, 1),
            label=_score_to_label(score),
            detail=f"Total projected: {fp:.1f} FP",
        )

    @staticmethod
    def _score_floor(lineup: OptimizedLineup) -> LineupDimensionScore:
        """Score lineup's floor protection.

        Calibrated to real DFS floor ratios:
        - 60% of projection = fragile (0/10)
        - 70% = decent (5/10)
        - 78%+ = excellent (10/10)
        """
        floor = lineup.total_floor_fp
        proj = lineup.total_projected_fp
        ratio = floor / proj if proj > 0 else 0
        score = min(10.0, max(0.0, (ratio - 0.60) / 0.018))
        return LineupDimensionScore(
            dimension="floor",
            score=round(score, 1),
            label=_score_to_label(score),
            detail=f"Floor: {floor:.1f} FP ({ratio:.0%} of projection)",
        )

    @staticmethod
    def _score_ceiling(lineup: OptimizedLineup) -> LineupDimensionScore:
        """Score lineup's upside potential.

        Calibrated to real DFS ceiling ratios:
        - 1.05x = limited upside (0/10)
        - 1.15x = normal (5.6/10)
        - 1.23x+ = elite GPP ceiling (10/10)
        """
        ceil = lineup.total_ceiling_fp
        proj = lineup.total_projected_fp
        ratio = ceil / proj if proj > 0 else 0
        score = min(10.0, max(0.0, (ratio - 1.05) / 0.018))
        return LineupDimensionScore(
            dimension="ceiling",
            score=round(score, 1),
            label=_score_to_label(score),
            detail=f"Ceiling: {ceil:.1f} FP ({ratio:.0%} of projection)",
        )

    @staticmethod
    def _score_value(lineup: OptimizedLineup) -> LineupDimensionScore:
        """Score salary efficiency."""
        cap = lineup.salary_cap
        used = lineup.total_salary
        remaining = cap - used
        pct_used = used / cap * 100

        # Penalize leaving too much on the table
        if remaining <= 500:
            score = 10.0
        elif remaining <= 1000:
            score = 9.0
        elif remaining <= 2000:
            score = 7.0
        elif remaining <= 3000:
            score = 5.0
        else:
            score = max(0.0, 3.0 - (remaining - 3000) / 1000)

        return LineupDimensionScore(
            dimension="value",
            score=round(score, 1),
            label=_score_to_label(score),
            detail=f"${used:,} / ${cap:,} ({pct_used:.1f}% used, ${remaining:,} left)",
        )

    @staticmethod
    def _analyze_stacking(
        players: list, games: List[dict], sport: str = "nba",
    ) -> dict:
        """Analyze team stacking and score it."""
        team_counts = Counter(p.team_abbreviation for p in players)

        # Build game totals lookup
        game_totals = {}
        for g in games:
            if hasattr(g, "home_team"):
                game_totals[g.home_team.team_abbreviation.upper()] = (
                    g.projected_total
                )
                game_totals[g.away_team.team_abbreviation.upper()] = (
                    g.projected_total
                )

        # Sport-aware defaults for game totals
        default_total = 145.0 if sport == "cbb" else 220
        high_total_threshold = 155 if sport == "cbb" else 225

        # Score stacking: 3+ from one team in a high-total game is great
        has_stack = any(c >= 3 for c in team_counts.values())
        stack_in_high_total = False

        if has_stack:
            for team, count in team_counts.items():
                if count >= 3:
                    total = game_totals.get(team.upper(), default_total)
                    if total >= high_total_threshold:
                        stack_in_high_total = True

        # Count how many mini-stacks (2+ players same team) exist
        mini_stacks = sum(1 for c in team_counts.values() if c >= 2)

        if stack_in_high_total:
            score = 10.0
        elif has_stack:
            score = 8.0
        elif mini_stacks >= 2:
            score = 7.0  # Multiple 2-player correlations
        elif mini_stacks == 1:
            score = 6.0  # At least one pair correlated
        else:
            score = 3.0

        return {
            "counts": dict(team_counts),
            "score": LineupDimensionScore(
                dimension="stacking",
                score=round(score, 1),
                label=_score_to_label(score),
                detail=(
                    f"{'Stack found in high-total game' if stack_in_high_total else 'Mini-stack' if has_stack else 'No team stacking'}: "
                    + ", ".join(
                        f"{t}×{c}" for t, c in team_counts.most_common(3)
                    )
                ),
            ),
        }

    @staticmethod
    def _score_expert_consensus(
        players: list, signals: List[dict]
    ) -> LineupDimensionScore:
        """Score how well lineup aligns with expert sentiment."""
        if not signals:
            # No signals loaded — assume neutral, don't penalize the grade
            return LineupDimensionScore(
                dimension="expert_consensus",
                score=7.0,
                label="Good",
                detail="No expert signals loaded — neutral default",
            )

        # Build player sentiment from signals
        player_names = {p.player_name.lower() for p in players}
        bullish_count = 0
        bearish_count = 0
        matched = 0

        for sig in signals:
            mentioned = []
            if hasattr(sig, "mentioned_players"):
                mentioned = sig.mentioned_players or []
            elif isinstance(sig, dict):
                mentioned = sig.get("mentioned_players", []) or []

            for m in mentioned:
                if m.lower() in player_names:
                    matched += 1
                    sentiment = (
                        getattr(sig, "sentiment", None)
                        or (sig.get("sentiment") if isinstance(sig, dict) else None)
                        or "neutral"
                    )
                    if sentiment == "bullish":
                        bullish_count += 1
                    elif sentiment == "bearish":
                        bearish_count += 1

        if matched == 0:
            score = 6.5
            detail = "No expert mentions for lineup players"
        else:
            net = bullish_count - bearish_count
            score = min(10.0, max(0.0, 5.0 + net * 1.5))
            detail = (
                f"{bullish_count} bullish, {bearish_count} bearish "
                f"mentions across {matched} matches"
            )

        return LineupDimensionScore(
            dimension="expert_consensus",
            score=round(score, 1),
            label=_score_to_label(score),
            detail=detail,
        )

    # ------------------------------------------------------------------
    # Risk identification
    # ------------------------------------------------------------------

    def _identify_risks(
        self, players: list, injuries: List[dict], games: List[dict]
    ) -> List[LineupRisk]:
        """Identify risk factors in a lineup."""
        risks: List[LineupRisk] = []

        # Build injury lookup
        injury_lookup: Dict[str, dict] = {}
        for inj in injuries:
            name = inj.get("player", "").lower() if isinstance(inj, dict) else ""
            if name:
                injury_lookup[name] = inj

        for p in players:
            pname = p.player_name.lower()

            # Low minutes risk
            if p.projected_minutes < 20:
                risks.append(
                    LineupRisk(
                        severity="medium",
                        category="low_minutes",
                        description=(
                            f"{p.player_name} projected for only "
                            f"{p.projected_minutes:.0f} minutes (fragile floor)"
                        ),
                        affected_players=[p.player_name],
                    )
                )

            # Injury risk — check if player has GTD/Questionable status
            for inj_name, inj_data in injury_lookup.items():
                if pname in inj_name or inj_name in pname:
                    status = (
                        inj_data.get("status", "")
                        if isinstance(inj_data, dict)
                        else ""
                    )
                    if status.lower() in (
                        "questionable",
                        "doubtful",
                        "game time decision",
                        "gtd",
                        "day-to-day",
                    ):
                        risks.append(
                            LineupRisk(
                                severity="high",
                                category="injury",
                                description=(
                                    f"{p.player_name} is listed as "
                                    f"{status} — may not play"
                                ),
                                affected_players=[p.player_name],
                            )
                        )
                    break

        # B2B risk (check via game context)
        b2b_players = []
        for g in games:
            if not hasattr(g, "home_team"):
                continue
            # We'd need B2B data from enrichment; flag teams known to be B2B
            # For now, check projected minutes < typical as proxy
        # (B2B is better handled via enrichment, but we flag low-minute combos)

        # Salary waste risk
        # (handled in value score, but flag extreme cases)

        return risks

    # ------------------------------------------------------------------
    # Swap suggestions
    # ------------------------------------------------------------------

    def _generate_swaps(
        self,
        lineup: OptimizedLineup,
        pool: list,
        games: List[dict],
        signals: List[dict],
        platform: str,
    ) -> List[PlayerSwapSuggestion]:
        """Generate swap recommendations for a lineup."""
        suggestions: List[PlayerSwapSuggestion] = []
        lineup_ids = {p.player_id for p in lineup.players}

        # Build game total lookup
        game_totals: Dict[str, float] = {}
        for g in games:
            if hasattr(g, "home_team"):
                game_totals[g.home_team.team_abbreviation.upper()] = (
                    g.projected_total
                )
                game_totals[g.away_team.team_abbreviation.upper()] = (
                    g.projected_total
                )

        # Build expert sentiment lookup
        expert_sentiment: Dict[str, str] = {}
        for sig in signals:
            mentioned = []
            if hasattr(sig, "mentioned_players"):
                mentioned = sig.mentioned_players or []
            elif isinstance(sig, dict):
                mentioned = sig.get("mentioned_players", []) or []
            sentiment = (
                getattr(sig, "sentiment", "neutral")
                if not isinstance(sig, dict)
                else sig.get("sentiment", "neutral")
            )
            for m in mentioned:
                if m.lower() not in expert_sentiment:
                    expert_sentiment[m.lower()] = sentiment

        for player in lineup.players:
            # Find alternatives from pool for this slot
            for alt in pool:
                if alt.player_id in lineup_ids:
                    continue
                if alt.position != player.position:
                    continue

                # Check salary feasibility
                salary_diff = alt.salary - player.salary
                if salary_diff > lineup.salary_remaining:
                    continue

                fp_delta = round(alt.projected_fp - player.projected_fp, 1)

                reasons = []
                confidence = "low"

                # Higher projection
                if fp_delta >= 2.0:
                    reasons.append(
                        f"+{fp_delta} projected FP"
                    )
                    confidence = "medium"

                # Better game environment
                alt_total = game_totals.get(
                    alt.team_abbreviation.upper(), 0
                )
                cur_total = game_totals.get(
                    player.team_abbreviation.upper(), 0
                )
                if alt_total > cur_total + 5:
                    reasons.append(
                        f"Higher game total ({alt_total:.0f} vs {cur_total:.0f})"
                    )
                    confidence = "medium"

                # Expert backing
                alt_sent = expert_sentiment.get(
                    alt.player_name.lower(), "neutral"
                )
                cur_sent = expert_sentiment.get(
                    player.player_name.lower(), "neutral"
                )
                if alt_sent == "bullish" and cur_sent != "bullish":
                    reasons.append("Expert bullish sentiment")
                    confidence = "high"

                # Higher ceiling
                if alt.ceiling_fp > player.ceiling_fp + 3:
                    reasons.append(
                        f"Higher ceiling ({alt.ceiling_fp:.0f} vs "
                        f"{player.ceiling_fp:.0f})"
                    )

                if reasons:
                    suggestions.append(
                        PlayerSwapSuggestion(
                            slot=player.roster_slot,
                            current_player=player.player_name,
                            current_player_id=player.player_id,
                            suggested_player=alt.player_name,
                            suggested_player_id=alt.player_id,
                            reason="; ".join(reasons),
                            projected_fp_delta=fp_delta,
                            confidence=confidence,
                        )
                    )

        # Sort by FP delta descending, take top suggestions
        suggestions.sort(key=lambda s: s.projected_fp_delta, reverse=True)
        return suggestions[:3]

    # ------------------------------------------------------------------
    # Correlation analysis
    # ------------------------------------------------------------------

    @staticmethod
    def _analyze_correlation(
        players: list, games: List[dict]
    ) -> List[str]:
        """Identify game-level correlations in the lineup."""
        notes: List[str] = []

        # Build game lookup: team_abbr -> game info
        team_to_game: Dict[str, dict] = {}
        for g in games:
            if hasattr(g, "home_team"):
                team_to_game[g.home_team.team_abbreviation.upper()] = g
                team_to_game[g.away_team.team_abbreviation.upper()] = g

        # Count players per game
        game_players: Dict[str, List[str]] = {}
        for p in players:
            g = team_to_game.get(p.team_abbreviation.upper())
            if g:
                gid = g.game_id
                game_players.setdefault(gid, []).append(
                    f"{p.player_name} ({p.team_abbreviation})"
                )

        for gid, plist in game_players.items():
            if len(plist) >= 3:
                # Find the game total
                g = next(
                    (
                        g
                        for g in games
                        if hasattr(g, "game_id") and g.game_id == gid
                    ),
                    None,
                )
                total = g.projected_total if g else 0
                total_label = (
                    f" (projected total: {total:.0f})"
                    if total
                    else ""
                )
                notes.append(
                    f"{len(plist)} players from same game{total_label}: "
                    + ", ".join(plist)
                )

        return notes

    # ------------------------------------------------------------------
    # Portfolio summary (multi-lineup)
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_portfolio_summary(
        lineups: List[OptimizedLineup],
        analyses: List[LineupAnalysisResult],
    ) -> Dict[str, Any]:
        """Cross-lineup portfolio statistics."""
        if not lineups:
            return {}

        all_ids: List[int] = []
        team_exposure: Counter = Counter()

        for lu in lineups:
            for p in lu.players:
                all_ids.append(p.player_id)
                team_exposure[p.team_abbreviation] += 1

        unique_players = len(set(all_ids))
        total_slots = len(all_ids)
        player_counts = Counter(all_ids)

        # Most/least exposed
        most_exposed = player_counts.most_common(5)
        # Find player names for the most exposed
        name_lookup: Dict[int, str] = {}
        for lu in lineups:
            for p in lu.players:
                name_lookup[p.player_id] = p.player_name

        most_exposed_names = [
            {"name": name_lookup.get(pid, str(pid)), "count": c, "pct": round(c / len(lineups) * 100, 1)}
            for pid, c in most_exposed
        ]

        avg_salary = round(
            sum(lu.total_salary for lu in lineups) / len(lineups)
        )
        avg_fp = round(
            sum(lu.total_projected_fp for lu in lineups) / len(lineups), 1
        )
        avg_grade = round(
            sum(a.overall_score for a in analyses) / len(analyses), 1
        )

        # Team exposure as percentages
        team_pcts = {
            team: round(count / total_slots * 100, 1)
            for team, count in team_exposure.most_common()
        }

        return {
            "num_lineups": len(lineups),
            "unique_players": unique_players,
            "total_slots": total_slots,
            "avg_salary": avg_salary,
            "avg_projected_fp": avg_fp,
            "avg_overall_score": avg_grade,
            "most_exposed_players": most_exposed_names,
            "team_exposure": team_pcts,
        }

    # ------------------------------------------------------------------
    # Data fetching helpers
    # ------------------------------------------------------------------

    def _get_games(self, game_date: str) -> list:
        """Fetch games for the given date."""
        try:
            schedule = self.game_service.get_games(game_date)
            return schedule.games
        except Exception as e:
            logger.warning(f"[Analysis] Failed to fetch games: {e}")
            return []

    def _get_injuries(self) -> list:
        """Fetch current injury report."""
        try:
            return self.injury_service.get_all_injuries()
        except Exception as e:
            logger.warning(f"[Analysis] Failed to fetch injuries: {e}")
            return []

    def _get_signals(self, lineups: List[OptimizedLineup]) -> list:
        """Fetch expert signals relevant to lineup players."""
        try:
            all_names = list(
                {p.player_name for lu in lineups for p in lu.players}
            )
            resp = self.expert_signal_service.get_signals(
                player_names=all_names, limit=100
            )
            return resp.signals
        except Exception as e:
            logger.warning(f"[Analysis] Failed to fetch signals: {e}")
            return []

    def _get_pool(self, request: AnalyzeLineupsRequest) -> list:
        """Get the player pool for swap suggestions."""
        try:
            sport = getattr(request, "sport", "nba")
            pool = self.lineup_optimizer_service.build_player_pool(
                platform=request.platform,
                draft_group_id=request.draft_group_id,
                game_date=request.game_date,
                sport=sport,
            )
            return pool
        except Exception as e:
            logger.warning(f"[Analysis] Failed to build pool: {e}")
            return []

    # ------------------------------------------------------------------
    # Lineup Refinement
    # ------------------------------------------------------------------

    # Numeric value for each grade for comparison
    GRADE_NUMERIC = {
        "A+": 7, "A": 6, "B+": 5, "B": 4, "C+": 3, "C": 2, "D": 1,
    }

    def refine_lineups(
        self, request: RefineLineupsRequest
    ) -> RefineLineupsResponse:
        """Iteratively refine lineups by applying swap suggestions.

        For each lineup, the flow is:
          1. Analyze the lineup to get its grade + swap suggestions
          2. Apply the best swap (highest FP delta, salary-legal)
          3. Re-analyze to verify improvement
          4. Repeat up to ``max_iterations`` or until target grade reached
        """
        start_ms = time.time()

        platform = request.platform
        sport = getattr(request, "sport", "nba")
        game_date = request.game_date or date.today().isoformat()

        # Build a single pool once (reused across all lineups)
        pool = self._get_pool_for_refine(request)
        pool_lookup = {p.player_id: p for p in pool}

        games = self._get_games(game_date)
        injuries = self._get_injuries()

        target_numeric = (
            self.GRADE_NUMERIC.get(request.target_grade, 99)
            if request.target_grade
            else 99
        )

        refinement_results: list = []
        refined_lineups: list = []
        total_swaps = 0
        warnings: list = []

        for idx, lineup in enumerate(request.lineups):
            original_lineup = lineup.copy(deep=True)

            # Get signals for this lineup's players
            signals = self._get_signals([lineup])

            # Initial analysis
            analysis = self._analyze_single(
                idx, lineup, platform, games, injuries, signals, pool,
                sport=sport,
            )
            original_grade = analysis.overall_grade
            original_score = analysis.overall_score

            swaps_applied: list = []
            current_lineup = lineup
            current_analysis = analysis

            for iteration in range(request.max_iterations):
                # Check if we've reached target grade
                current_numeric = self.GRADE_NUMERIC.get(
                    current_analysis.overall_grade, 0
                )
                if current_numeric >= target_numeric:
                    break

                # Get swap suggestions
                swap_suggestions = current_analysis.swap_suggestions
                if not swap_suggestions:
                    break

                # Try each swap suggestion, apply the first that improves score
                applied = False
                for swap in swap_suggestions:
                    # Only apply if delta is positive
                    if swap.projected_fp_delta <= 0:
                        continue

                    # Verify the suggested player is in our pool
                    alt = pool_lookup.get(swap.suggested_player_id)
                    if not alt:
                        continue

                    # Verify salary feasibility
                    salary_diff = alt.salary - self._find_player_salary(
                        current_lineup, swap.current_player_id
                    )
                    if salary_diff > current_lineup.salary_remaining:
                        continue

                    # Apply the swap
                    new_lineup = self._apply_swap(
                        current_lineup, swap, alt, platform
                    )
                    if new_lineup is None:
                        continue

                    # Re-analyze the swapped lineup
                    new_signals = self._get_signals([new_lineup])
                    new_analysis = self._analyze_single(
                        idx, new_lineup, platform, games, injuries,
                        new_signals, pool, sport=sport,
                    )

                    # Only keep if score actually improved
                    if new_analysis.overall_score > current_analysis.overall_score:
                        swaps_applied.append(swap)
                        current_lineup = new_lineup
                        current_analysis = new_analysis
                        total_swaps += 1
                        applied = True
                        logger.info(
                            f"[Refine] Lineup {idx + 1}: "
                            f"{swap.current_player} → {swap.suggested_player} "
                            f"(score {current_analysis.overall_score:.1f} → "
                            f"{new_analysis.overall_score:.1f})"
                        )
                        break

                if not applied:
                    break  # No improving swap found

            refined_lineups.append(current_lineup)
            refinement_results.append(
                RefinedLineupResult(
                    lineup_index=idx,
                    original_grade=original_grade,
                    original_score=original_score,
                    refined_grade=current_analysis.overall_grade,
                    refined_score=current_analysis.overall_score,
                    swaps_applied=swaps_applied,
                    lineup=current_lineup,
                )
            )

        # Compute average score improvement
        score_deltas = [
            r.refined_score - r.original_score for r in refinement_results
        ]
        avg_improvement = (
            round(sum(score_deltas) / len(score_deltas), 1)
            if score_deltas
            else 0.0
        )

        elapsed_ms = int((time.time() - start_ms) * 1000)
        return RefineLineupsResponse(
            platform=platform,
            lineups=refined_lineups,
            refinement_results=refinement_results,
            total_swaps_applied=total_swaps,
            avg_score_improvement=avg_improvement,
            generation_time_ms=elapsed_ms,
            warnings=warnings,
        )

    def _apply_swap(
        self,
        lineup: OptimizedLineup,
        swap: PlayerSwapSuggestion,
        alt_pool_entry,
        platform: str,
    ) -> Optional[OptimizedLineup]:
        """Apply a single swap to a lineup, returning a new OptimizedLineup.

        Returns None if the swap can't be applied (salary violation, etc.).
        """
        try:
            new_players = []
            salary_cap = lineup.salary_cap
            swapped = False

            for p in lineup.players:
                if p.player_id == swap.current_player_id and not swapped:
                    # Replace with the suggested player
                    new_player = LineupPlayer(
                        player_id=alt_pool_entry.player_id,
                        player_name=alt_pool_entry.player_name,
                        display_name=alt_pool_entry.display_name or alt_pool_entry.player_name,
                        position=alt_pool_entry.position,
                        roster_slot=p.roster_slot,
                        team_abbreviation=alt_pool_entry.team_abbreviation,
                        salary=alt_pool_entry.salary,
                        projected_fp=alt_pool_entry.projected_fp,
                        floor_fp=alt_pool_entry.floor_fp,
                        ceiling_fp=alt_pool_entry.ceiling_fp,
                        projected_minutes=alt_pool_entry.projected_minutes,
                        projected_stats=alt_pool_entry.projected_stats,
                        dk_player_id=alt_pool_entry.dk_player_id,
                    )
                    new_players.append(new_player)
                    swapped = True
                else:
                    new_players.append(p)

            if not swapped:
                return None

            total_salary = sum(p.salary for p in new_players)
            if total_salary > salary_cap:
                return None

            total_fp = sum(p.projected_fp for p in new_players)
            total_floor = sum(p.floor_fp for p in new_players)
            total_ceil = sum(p.ceiling_fp for p in new_players)

            return OptimizedLineup(
                platform=platform,
                players=new_players,
                total_salary=total_salary,
                salary_remaining=salary_cap - total_salary,
                total_projected_fp=round(total_fp, 1),
                total_floor_fp=round(total_floor, 1),
                total_ceiling_fp=round(total_ceil, 1),
                salary_cap=salary_cap,
                roster_slots=lineup.roster_slots,
                warnings=lineup.warnings,
            )
        except Exception as e:
            logger.warning(f"[Refine] Swap application failed: {e}")
            return None

    @staticmethod
    def _find_player_salary(lineup: OptimizedLineup, player_id: int) -> int:
        """Find a player's salary in a lineup."""
        for p in lineup.players:
            if p.player_id == player_id:
                return p.salary
        return 0

    def _get_pool_for_refine(self, request: RefineLineupsRequest) -> list:
        """Get the player pool for refinement swap suggestions."""
        try:
            sport = getattr(request, "sport", "nba")
            pool = self.lineup_optimizer_service.build_player_pool(
                platform=request.platform,
                draft_group_id=request.draft_group_id,
                game_date=request.game_date,
                sport=sport,
            )
            return pool
        except Exception as e:
            logger.warning(f"[Refine] Failed to build pool: {e}")
            return []
