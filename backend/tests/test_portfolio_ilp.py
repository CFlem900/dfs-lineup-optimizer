"""Tests for Phase 4: Joint Portfolio ILP Optimization.

Tests cover:
1. _portfolio_optimize returns correct count
2. Diversity (overlap) enforcement via hard pairwise constraints
3. Quality comparison vs greedy
4. Fallback behavior (PuLP unavailable, small requests, cash mode)
5. Soft min-exposure constraints with shortfall penalty variables
6. Constants verification
"""

import pytest
from typing import List, Set, Tuple
from unittest.mock import patch

from app.models.lineup import (
    PlayerPoolEntry,
    OptimizedLineup,
    LineupPlayer,
)
from app.services.lineup_optimizer_service import (
    LineupOptimizerService,
    DK_SALARY_CAP,
    DK_ROSTER_SLOTS,
    DK_SLOT_ELIGIBILITY,
    _PULP_AVAILABLE,
)


# ============================================================================
# HELPERS
# ============================================================================


def _make_lineup_player(player_id, name, pos, team, salary, fp):
    """Create a minimal LineupPlayer for testing."""
    return LineupPlayer(
        player_id=player_id,
        player_name=name,
        position=pos,
        roster_slot="UTIL",
        team_abbreviation=team,
        salary=salary,
        projected_fp=fp,
        floor_fp=fp * 0.8,
        ceiling_fp=fp * 1.2,
        projected_minutes=25.0,
    )


def _make_optimized_lineup(
    players: List[LineupPlayer],
    salary_cap: int = DK_SALARY_CAP,
) -> OptimizedLineup:
    """Build a minimal OptimizedLineup from players."""
    total_sal = sum(p.salary for p in players)
    total_fp = sum(p.projected_fp for p in players)
    return OptimizedLineup(
        platform="dk",
        players=players,
        total_salary=total_sal,
        salary_remaining=salary_cap - total_sal,
        total_projected_fp=total_fp,
        total_floor_fp=total_fp * 0.8,
        total_ceiling_fp=total_fp * 1.2,
        salary_cap=salary_cap,
        roster_slots=DK_ROSTER_SLOTS,
    )


def _generate_diverse_candidates(n_candidates=20, n_players_per=8):
    """Generate N diverse candidate lineups with different player compositions.

    Creates lineups from a pool of 40 players, each lineup getting a
    somewhat different mix to simulate realistic overgeneration output.
    """
    import random
    rng = random.Random(42)

    positions = ["PG", "SG", "SF", "PF", "C"]
    teams = ["BOS", "LAL", "MIA", "NYK", "CHI", "DEN", "PHX", "MIL"]

    # Build a player pool
    all_players = []
    for i in range(40):
        pos = positions[i % 5]
        team = teams[i % len(teams)]
        sal = 6200 + (i % 12) * 400  # realistic DK salaries (8 × 6200 = 49600 min)
        fp = 12.0 + (i % 15) * 2.5
        all_players.append(
            _make_lineup_player(
                player_id=i + 1,
                name=f"Player_{i+1}",
                pos=pos,
                team=team,
                salary=sal,
                fp=fp,
            )
        )

    candidates = []
    for c in range(n_candidates):
        # Each candidate picks a somewhat random subset
        offset = c % 10
        indices = [(offset + i * 3 + c) % 40 for i in range(n_players_per)]
        # Ensure unique
        indices = list(dict.fromkeys(indices))[:n_players_per]
        while len(indices) < n_players_per:
            new = rng.randint(0, 39)
            if new not in indices:
                indices.append(new)
        players = [all_players[i] for i in indices]
        lineup = _make_optimized_lineup(players)
        # Score: higher for higher total projection, with some randomness
        score = sum(p.projected_fp for p in players) + rng.uniform(-5, 5)
        candidates.append((lineup, score))

    return candidates


# ============================================================================
# PORTFOLIO ILP TESTS
# ============================================================================


@pytest.mark.skipif(not _PULP_AVAILABLE, reason="PuLP not installed")
class TestPortfolioILP:
    """Tests for _portfolio_optimize joint lineup selection (diversity-only)."""

    def test_returns_correct_count(self):
        """ILP should return exactly num_to_select lineups."""
        candidates = _generate_diverse_candidates(30, 8)
        result = LineupOptimizerService._portfolio_optimize(
            candidates, num_to_select=5, max_overlap=6,
        )
        assert result is not None
        assert len(result) == 5

    def test_returns_correct_count_large(self):
        """ILP should work with larger portfolio requests."""
        candidates = _generate_diverse_candidates(50, 8)
        result = LineupOptimizerService._portfolio_optimize(
            candidates, num_to_select=10, max_overlap=7,
        )
        assert result is not None
        assert len(result) == 10

    def test_overlap_hard_constraint(self):
        """No two selected lineups should share more than max_overlap players."""
        candidates = _generate_diverse_candidates(30, 8)
        max_overlap = 4

        result = LineupOptimizerService._portfolio_optimize(
            candidates, num_to_select=5, max_overlap=max_overlap,
        )
        assert result is not None

        for i in range(len(result)):
            ids_i = {p.player_id for p in result[i].players}
            for j in range(i + 1, len(result)):
                ids_j = {p.player_id for p in result[j].players}
                shared = len(ids_i & ids_j)
                assert shared <= max_overlap, (
                    f"Lineups {i} and {j} share {shared} players, "
                    f"max allowed is {max_overlap}"
                )

    def test_quality_non_negative(self):
        """Selected lineups should have positive total score."""
        candidates = _generate_diverse_candidates(25, 8)
        result = LineupOptimizerService._portfolio_optimize(
            candidates, num_to_select=5, max_overlap=6,
        )
        assert result is not None

        score_lookup = {id(lu): sc for lu, sc in candidates}
        total_score = sum(
            score_lookup.get(id(lu), 0) for lu in result
        )
        assert total_score > 0, "Total portfolio score should be positive"

    def test_sorted_by_score_descending(self):
        """Result should be sorted by score descending."""
        candidates = _generate_diverse_candidates(30, 8)
        result = LineupOptimizerService._portfolio_optimize(
            candidates, num_to_select=5, max_overlap=7,
        )
        assert result is not None

        score_lookup = {id(lu): sc for lu, sc in candidates}
        scores = [score_lookup.get(id(lu), 0) for lu in result]
        assert scores == sorted(scores, reverse=True), (
            "Portfolio lineups should be sorted by score descending"
        )

    def test_returns_none_when_too_few_candidates(self):
        """Should return None when candidates < num_to_select."""
        candidates = _generate_diverse_candidates(3, 8)
        result = LineupOptimizerService._portfolio_optimize(
            candidates, num_to_select=5, max_overlap=6,
        )
        assert result is None

    def test_returns_none_without_pulp(self):
        """Should return None when PuLP is not available."""
        candidates = _generate_diverse_candidates(20, 8)
        with patch(
            "app.services.lineup_optimizer_service._PULP_AVAILABLE", False
        ):
            result = LineupOptimizerService._portfolio_optimize(
                candidates, num_to_select=5, max_overlap=6,
            )
        assert result is None


# ============================================================================
# CONSTANTS TESTS
# ============================================================================


class TestPortfolioILPConstants:
    """Tests for Phase 4 portfolio ILP constants."""

    def test_ilp_constants_importable(self):
        """All portfolio ILP constants should be importable."""
        from app.config.constants import (
            PORTFOLIO_ILP_MAX_CANDIDATES,
            PORTFOLIO_ILP_SOLVER_TIMEOUT,
            PORTFOLIO_ILP_MIN_LINEUPS,
            PORTFOLIO_ILP_DIVERSITY_PENALTY,
            PORTFOLIO_ILP_MIN_EXPO_PENALTY,
        )
        assert PORTFOLIO_ILP_MAX_CANDIDATES == 550   # raised for oversampling (500 candidates)
        assert PORTFOLIO_ILP_SOLVER_TIMEOUT == 75    # raised from 45 — early incumbents at 45s on 500-candidate pools
        assert PORTFOLIO_ILP_MIN_LINEUPS == 5
        assert PORTFOLIO_ILP_DIVERSITY_PENALTY == 0.02
        assert PORTFOLIO_ILP_MIN_EXPO_PENALTY == 1000.0

    def test_min_lineups_reasonable(self):
        """Min lineups threshold should be >= 3 and <= 20."""
        from app.config.constants import PORTFOLIO_ILP_MIN_LINEUPS
        assert 3 <= PORTFOLIO_ILP_MIN_LINEUPS <= 20

    def test_solver_timeout_reasonable(self):
        """Solver timeout should be between 5-120 seconds."""
        from app.config.constants import PORTFOLIO_ILP_SOLVER_TIMEOUT
        assert 5 <= PORTFOLIO_ILP_SOLVER_TIMEOUT <= 120

    def test_exposure_penalty_constant(self):
        """Exposure penalty default cap should be importable."""
        from app.config.constants import EXPOSURE_PENALTY_DEFAULT_CAP
        assert 0.0 < EXPOSURE_PENALTY_DEFAULT_CAP <= 1.0
        assert EXPOSURE_PENALTY_DEFAULT_CAP == 0.55

    def test_min_expo_penalty_reasonable(self):
        """Min-exposure shortfall penalty should be large enough to
        discourage shortfalls but not so large it dominates the objective."""
        from app.config.constants import PORTFOLIO_ILP_MIN_EXPO_PENALTY
        assert PORTFOLIO_ILP_MIN_EXPO_PENALTY >= 100.0
        assert PORTFOLIO_ILP_MIN_EXPO_PENALTY <= 10000.0


# ============================================================================
# SOFT MIN-EXPOSURE CONSTRAINT TESTS
# ============================================================================


@pytest.mark.skipif(not _PULP_AVAILABLE, reason="PuLP not installed")
class TestSoftMinExposure:
    """Tests for soft min-exposure constraints via shortfall penalty variables.

    The ILP uses continuous shortfall_p variables so that min-exposure targets
    are "best-effort" — the solver pays a penalty per unit of shortfall rather
    than returning Infeasible when min-exposure conflicts with overlap limits.
    """

    def test_soft_min_exposure_satisfied_when_feasible(self):
        """When min-exposure targets are easily satisfiable, all targets
        should be fully met (shortfall = 0)."""
        candidates = _generate_diverse_candidates(30, 8)

        # Pick a player that appears in many candidates
        pid_counts = {}
        for lu, _sc in candidates:
            for p in lu.players:
                pid_counts[p.player_id] = pid_counts.get(p.player_id, 0) + 1

        # Find a player in >= 5 candidates, request min_exposure of 2
        target_pid = None
        for pid, count in pid_counts.items():
            if count >= 5:
                target_pid = pid
                break
        assert target_pid is not None, "Need a player in >= 5 candidates"

        result = LineupOptimizerService._portfolio_optimize(
            candidates,
            num_to_select=5,
            max_overlap=7,
            player_min_appearances={target_pid: 2},
        )
        assert result is not None
        assert len(result) == 5

        # Verify the target player appears at least twice
        appearances = sum(
            1 for lu in result
            if any(p.player_id == target_pid for p in lu.players)
        )
        assert appearances >= 2, (
            f"Player {target_pid} appeared {appearances} times, expected >= 2"
        )

    def test_soft_min_exposure_no_infeasible(self):
        """Even with conflicting min-exposure + overlap constraints, the
        solver should NOT return Infeasible — shortfall absorbs the gap."""
        candidates = _generate_diverse_candidates(20, 8)

        # Request very high min-exposure for multiple players — likely
        # infeasible as hard constraints but soft should still solve.
        pid_counts = {}
        for lu, _sc in candidates:
            for p in lu.players:
                pid_counts[p.player_id] = pid_counts.get(p.player_id, 0) + 1

        # Pick 3 different players, request they each appear 4+ times
        # in a portfolio of 5 — this would be infeasible as hard constraints
        # with tight overlap limits.
        top_pids = sorted(pid_counts, key=pid_counts.get, reverse=True)[:3]
        min_appearances = {pid: 4 for pid in top_pids}

        result = LineupOptimizerService._portfolio_optimize(
            candidates,
            num_to_select=5,
            max_overlap=4,  # tight overlap limit
            player_min_appearances=min_appearances,
        )
        # Should NOT be None — soft constraints prevent infeasibility
        assert result is not None
        assert len(result) == 5

    def test_soft_min_exposure_with_impossible_player(self):
        """When a player appears in 0 candidates, the solver should still
        succeed (that player is simply skipped)."""
        candidates = _generate_diverse_candidates(20, 8)

        # Player ID 9999 doesn't exist in any candidate
        result = LineupOptimizerService._portfolio_optimize(
            candidates,
            num_to_select=5,
            max_overlap=6,
            player_min_appearances={9999: 3},
        )
        assert result is not None
        assert len(result) == 5

    def test_hard_overlap_preserved_with_soft_exposure(self):
        """Pairwise overlap constraints remain HARD even when soft
        min-exposure is active — no two lineups should exceed max_overlap."""
        candidates = _generate_diverse_candidates(30, 8)
        max_overlap = 4

        # Add min-exposure for a popular player
        pid_counts = {}
        for lu, _sc in candidates:
            for p in lu.players:
                pid_counts[p.player_id] = pid_counts.get(p.player_id, 0) + 1
        top_pid = max(pid_counts, key=pid_counts.get)

        result = LineupOptimizerService._portfolio_optimize(
            candidates,
            num_to_select=5,
            max_overlap=max_overlap,
            player_min_appearances={top_pid: 3},
        )
        assert result is not None

        # Hard overlap constraint must still hold
        for i in range(len(result)):
            ids_i = {p.player_id for p in result[i].players}
            for j in range(i + 1, len(result)):
                ids_j = {p.player_id for p in result[j].players}
                shared = len(ids_i & ids_j)
                assert shared <= max_overlap, (
                    f"Lineups {i} and {j} share {shared} players > "
                    f"max_overlap={max_overlap} (hard constraint violated)"
                )
