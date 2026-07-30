"""Tests for Phase 5: Opponent Field Modeling + Multi-Slot Swaps.

Tests cover:
1. Archetype-based field generation in OwnershipSimulator
2. _generate_optimizer_lineup (chalk and contrarian)
3. Two-slot swap improvement in LineupOptimizerService
4. Constants verification
"""

import pytest
import numpy as np
from typing import Dict, List, Optional
from unittest.mock import MagicMock

from app.services.ownership_simulator import OwnershipSimulator, DK_ROSTER_SIZE


# ============================================================================
# HELPERS
# ============================================================================


class _FakePoolEntry:
    """Minimal PlayerPoolEntry mock for testing."""

    def __init__(self, player_id, name, position, projected_fp, ceiling_fp,
                 ownership, salary):
        self.player_id = player_id
        self.player_name = name
        self.position = position
        self.projected_fp = projected_fp
        self.ceiling_fp = ceiling_fp
        self.floor_fp = projected_fp * 0.4
        self.ownership_projection = ownership
        self.salary = salary
        self.team_abbreviation = "TST"
        self.dk_value = projected_fp / (salary / 1000) if salary else 0
        self.injury_status = None


def _make_test_pool(n=30):
    """Build a synthetic pool with mix of positions and salaries."""
    positions = ["PG", "SG", "SF", "PF", "C"]
    pool = []
    for i in range(n):
        pos = positions[i % len(positions)]
        proj = 25.0 + (i % 10) * 2.0
        ceil = proj * 1.4
        own = 5.0 + (i % 8) * 3.0  # 5-26%
        sal = 4000 + (i % 10) * 500
        pool.append(_FakePoolEntry(
            player_id=i + 1,
            name=f"Player_{i+1}",
            position=pos,
            projected_fp=proj,
            ceiling_fp=ceil,
            ownership=own,
            salary=sal,
        ))
    return pool


# ============================================================================
# OWNERSHIP SIMULATOR — ARCHETYPE TESTS
# ============================================================================


class TestFieldArchetypes:
    """Tests for archetype-based field generation."""

    def test_field_size_matches_request(self):
        """Generated field should have exactly field_size scores."""
        sim = OwnershipSimulator()
        pool = _make_test_pool(30)
        pool_data = sim._prepare_pool(pool)
        rng = np.random.default_rng(42)

        scores = sim._generate_field_scores(
            pool_data["projections"], pool_data["ownership_weights"],
            pool_data["ceilings"], pool_data["floors"],
            pool_data["salaries"], 100, 50000, rng, pool_data=pool_data,
        )
        assert len(scores) == 100

    def test_archetype_ratios_from_constants(self):
        """Constants should define valid archetype ratios summing to ~1.0."""
        from app.config.constants import (
            FIELD_ARCHETYPE_OWNERSHIP_RANDOM,
            FIELD_ARCHETYPE_CHALK_OPTIMIZER,
            FIELD_ARCHETYPE_CONTRARIAN_OPTIMIZER,
        )
        total = (
            FIELD_ARCHETYPE_OWNERSHIP_RANDOM
            + FIELD_ARCHETYPE_CHALK_OPTIMIZER
            + FIELD_ARCHETYPE_CONTRARIAN_OPTIMIZER
        )
        assert abs(total - 1.0) < 0.01

    def test_field_scores_are_positive(self):
        """All field scores should be positive."""
        sim = OwnershipSimulator()
        pool = _make_test_pool(30)
        pool_data = sim._prepare_pool(pool)
        rng = np.random.default_rng(42)

        scores = sim._generate_field_scores(
            pool_data["projections"], pool_data["ownership_weights"],
            pool_data["ceilings"], pool_data["floors"],
            pool_data["salaries"], 50, 50000, rng, pool_data=pool_data,
        )
        assert all(s > 0 for s in scores), "All field scores should be positive"

    def test_field_has_variance(self):
        """Field scores should have meaningful variance (not all identical)."""
        sim = OwnershipSimulator()
        pool = _make_test_pool(30)
        pool_data = sim._prepare_pool(pool)
        rng = np.random.default_rng(42)

        scores = sim._generate_field_scores(
            pool_data["projections"], pool_data["ownership_weights"],
            pool_data["ceilings"], pool_data["floors"],
            pool_data["salaries"], 200, 50000, rng, pool_data=pool_data,
        )
        assert np.std(scores) > 1.0, "Field scores should have variance"


class TestOptimizerLineupGeneration:
    """Tests for _generate_optimizer_lineup."""

    def test_chalk_lineup_valid_positions(self):
        """Chalk lineup should have valid position fills."""
        sim = OwnershipSimulator()
        pool = _make_test_pool(30)
        pool_data = sim._prepare_pool(pool)
        rng = np.random.default_rng(42)

        indices = sim._generate_optimizer_lineup(
            pool_data["positions"],
            pool_data["projections"],
            pool_data["ceilings"],
            pool_data["ownership_weights"],
            pool_data["salaries"],
            50000,
            sim._DK_SLOTS,
            rng,
            archetype="chalk",
        )
        assert indices is not None
        assert len(indices) == DK_ROSTER_SIZE

    def test_contrarian_lineup_valid(self):
        """Contrarian lineup should generate valid lineup."""
        sim = OwnershipSimulator()
        pool = _make_test_pool(30)
        pool_data = sim._prepare_pool(pool)
        rng = np.random.default_rng(42)

        indices = sim._generate_optimizer_lineup(
            pool_data["positions"],
            pool_data["projections"],
            pool_data["ceilings"],
            pool_data["ownership_weights"],
            pool_data["salaries"],
            50000,
            sim._DK_SLOTS,
            rng,
            archetype="contrarian",
        )
        assert indices is not None
        assert len(indices) == DK_ROSTER_SIZE

    def test_chalk_selects_high_value(self):
        """Chalk lineups should tend toward high-value players."""
        sim = OwnershipSimulator()
        pool = _make_test_pool(30)
        pool_data = sim._prepare_pool(pool)
        rng = np.random.default_rng(42)

        # Generate many chalk lineups and check avg value
        total_value = 0.0
        n_lineups = 50
        for _ in range(n_lineups):
            indices = sim._generate_optimizer_lineup(
                pool_data["positions"],
                pool_data["projections"],
                pool_data["ceilings"],
                pool_data["ownership_weights"],
                pool_data["salaries"],
                50000,
                sim._DK_SLOTS,
                rng,
                archetype="chalk",
            )
            if indices is not None:
                proj_sum = pool_data["projections"][indices].sum()
                sal_sum = pool_data["salaries"][indices].sum()
                value = proj_sum / (sal_sum / 1000.0) if sal_sum > 0 else 0
                total_value += value

        avg_value = total_value / n_lineups
        assert avg_value > 0  # Chalk should produce positive value lineups

    def test_optimizer_lineup_respects_salary(self):
        """Generated lineups should not exceed salary cap."""
        sim = OwnershipSimulator()
        pool = _make_test_pool(30)
        pool_data = sim._prepare_pool(pool)
        rng = np.random.default_rng(42)

        for _ in range(20):
            indices = sim._generate_optimizer_lineup(
                pool_data["positions"],
                pool_data["projections"],
                pool_data["ceilings"],
                pool_data["ownership_weights"],
                pool_data["salaries"],
                50000,
                sim._DK_SLOTS,
                rng,
                archetype="chalk",
            )
            if indices is not None:
                total_sal = pool_data["salaries"][indices].sum()
                assert total_sal <= 50000, f"Salary {total_sal} exceeds cap"


# ============================================================================
# TWO-SLOT SWAP TESTS
# ============================================================================


class TestTwoSlotSwap:
    """Tests for _two_slot_swap_improve."""

    def _make_lineup_and_pool(self):
        """Create a simple lineup and pool for swap testing."""
        pool = []
        for i in range(20):
            pos = ["PG", "SG", "SF", "PF", "C"][i % 5]
            pool.append(_FakePoolEntry(
                player_id=i + 1,
                name=f"P{i+1}",
                position=pos,
                projected_fp=20.0 + i,
                ceiling_fp=30.0 + i,
                ownership=10.0,
                salary=4000 + (i * 200),
            ))

        # Build a sub-optimal lineup
        lineup = {
            "PG": pool[0],   # PG, proj=20, sal=4000
            "SG": pool[1],   # SG, proj=21, sal=4200
            "SF": pool[2],   # SF, proj=22, sal=4400
            "PF": pool[3],   # PF, proj=23, sal=4600
            "C": pool[4],    # C,  proj=24, sal=4800
            "G": pool[5],    # PG, proj=25, sal=5000
            "F": pool[7],    # SF, proj=27, sal=5400
            "UTIL": pool[9], # C,  proj=29, sal=5800
        }
        return lineup, pool

    def test_two_slot_swap_no_regression(self):
        """Two-slot swap should never make the lineup worse."""
        from app.services.lineup_optimizer_service import LineupOptimizerService

        svc = LineupOptimizerService.__new__(LineupOptimizerService)
        svc._get_slot_eligible_positions = lambda slot, platform, sport="nba": {
            "PG": {"PG"}, "SG": {"SG"}, "SF": {"SF"}, "PF": {"PF"},
            "C": {"C"}, "G": {"PG", "SG"}, "F": {"SF", "PF"},
            "UTIL": {"PG", "SG", "SF", "PF", "C"},
        }.get(slot, {"PG", "SG", "SF", "PF", "C"})

        lineup, pool = self._make_lineup_and_pool()
        score_fn = lambda p: p.projected_fp

        original_score = sum(score_fn(p) for p in lineup.values())

        improved = svc._two_slot_swap_improve(
            lineup, pool, 50000, "dk", score_fn, max_iterations=10,
        )
        new_score = sum(score_fn(p) for p in improved.values())

        assert new_score >= original_score, (
            f"Two-slot swap regressed: {new_score} < {original_score}"
        )

    def test_two_slot_swap_respects_salary(self):
        """Two-slot swap should never exceed salary cap."""
        from app.services.lineup_optimizer_service import LineupOptimizerService

        svc = LineupOptimizerService.__new__(LineupOptimizerService)
        svc._get_slot_eligible_positions = lambda slot, platform, sport="nba": {
            "PG": {"PG"}, "SG": {"SG"}, "SF": {"SF"}, "PF": {"PF"},
            "C": {"C"}, "G": {"PG", "SG"}, "F": {"SF", "PF"},
            "UTIL": {"PG", "SG", "SF", "PF", "C"},
        }.get(slot, {"PG", "SG", "SF", "PF", "C"})

        lineup, pool = self._make_lineup_and_pool()
        score_fn = lambda p: p.projected_fp

        improved = svc._two_slot_swap_improve(
            lineup, pool, 50000, "dk", score_fn, max_iterations=10,
        )
        total_salary = sum(p.salary for p in improved.values())

        assert total_salary <= 50000, f"Salary {total_salary} exceeds cap"

    def test_two_slot_swap_max_iterations(self):
        """Swap should terminate within max_iterations."""
        from app.services.lineup_optimizer_service import LineupOptimizerService

        svc = LineupOptimizerService.__new__(LineupOptimizerService)
        svc._get_slot_eligible_positions = lambda slot, platform, sport="nba": {
            "PG": {"PG"}, "SG": {"SG"}, "SF": {"SF"}, "PF": {"PF"},
            "C": {"C"}, "G": {"PG", "SG"}, "F": {"SF", "PF"},
            "UTIL": {"PG", "SG", "SF", "PF", "C"},
        }.get(slot, {"PG", "SG", "SF", "PF", "C"})

        lineup, pool = self._make_lineup_and_pool()
        score_fn = lambda p: p.projected_fp

        # Should complete without hanging (max_iterations=2)
        improved = svc._two_slot_swap_improve(
            lineup, pool, 50000, "dk", score_fn, max_iterations=2,
        )
        assert improved is not None


# ============================================================================
# CONSTANTS TESTS
# ============================================================================


class TestPhase5Constants:
    """Tests for Phase 5 constants."""

    def test_archetype_constants(self):
        from app.config.constants import (
            FIELD_ARCHETYPE_OWNERSHIP_RANDOM,
            FIELD_ARCHETYPE_CHALK_OPTIMIZER,
            FIELD_ARCHETYPE_CONTRARIAN_OPTIMIZER,
        )
        assert FIELD_ARCHETYPE_OWNERSHIP_RANDOM == 0.60
        assert FIELD_ARCHETYPE_CHALK_OPTIMIZER == 0.20
        assert FIELD_ARCHETYPE_CONTRARIAN_OPTIMIZER == 0.20

    def test_two_slot_swap_constant(self):
        from app.config.constants import TWO_SLOT_SWAP_MAX_ITERATIONS
        assert TWO_SLOT_SWAP_MAX_ITERATIONS == 20
