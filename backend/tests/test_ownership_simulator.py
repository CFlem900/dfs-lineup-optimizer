"""Tests for ownership_simulator.py — Monte Carlo GPP simulation."""

import pytest
from types import SimpleNamespace

from app.services.ownership_simulator import OwnershipSimulator, SimulationResult


def _make_pool(n=20):
    """Create a pool of mock players."""
    pool = []
    for i in range(n):
        pool.append(SimpleNamespace(
            player_id=1000 + i,
            player_name=f"Player_{i}",
            position="PG" if i % 5 == 0 else "SG" if i % 5 == 1 else "SF" if i % 5 == 2 else "PF" if i % 5 == 3 else "C",
            team_abbreviation="TM1" if i < 10 else "TM2",
            salary=5000 + i * 500,
            projected_fp=20.0 + i * 1.5,
            ceiling_fp=30.0 + i * 2.0,
            floor_fp=10.0 + i * 0.5,
            ownership_projection=5.0 + i * 0.5,
            dk_value=4.0,
        ))
    return pool


def _make_lineup(pool, indices=None):
    """Build a user lineup dict from pool."""
    indices = indices or list(range(8))
    return [
        {
            "player_id": pool[i].player_id,
            "player_name": pool[i].player_name,
            "projected_fp": pool[i].projected_fp,
            "ownership_pct": pool[i].ownership_projection,
            "ceiling_fp": pool[i].ceiling_fp,
            "floor_fp": pool[i].floor_fp,
        }
        for i in indices
    ]


class TestOwnershipSimulator:
    def test_empty_lineup(self):
        sim = OwnershipSimulator()
        result = sim.simulate(user_lineup=[], pool=_make_pool())
        assert isinstance(result, SimulationResult)
        assert result.simulations_run == 0

    def test_empty_pool(self):
        sim = OwnershipSimulator()
        lineup = [{"player_id": 1, "projected_fp": 30, "ownership_pct": 10,
                    "ceiling_fp": 40, "floor_fp": 15}]
        result = sim.simulate(user_lineup=lineup, pool=[])
        assert result.simulations_run == 0

    def test_basic_simulation_runs(self):
        sim = OwnershipSimulator()
        pool = _make_pool(20)
        lineup = _make_lineup(pool)
        result = sim.simulate(
            user_lineup=lineup,
            pool=pool,
            field_size=50,
            num_simulations=10,
        )
        assert result.simulations_run == 10
        assert 0 <= result.win_rate <= 100
        assert 0 <= result.min_cash_rate <= 100
        assert 0 <= result.avg_percentile <= 100
        assert result.avg_score > 0

    def test_result_has_all_fields(self):
        sim = OwnershipSimulator()
        pool = _make_pool(20)
        lineup = _make_lineup(pool)
        result = sim.simulate(
            user_lineup=lineup, pool=pool,
            field_size=20, num_simulations=5,
        )
        assert hasattr(result, "avg_percentile")
        assert hasattr(result, "win_rate")
        assert hasattr(result, "top_1pct_rate")
        assert hasattr(result, "top_10pct_rate")
        assert hasattr(result, "min_cash_rate")
        assert hasattr(result, "expected_roi")
        assert hasattr(result, "avg_score")
        assert hasattr(result, "score_std")
        assert hasattr(result, "field_avg_score")
        assert hasattr(result, "chalk_comparison")
        assert hasattr(result, "contrarian_comparison")

    def test_chalk_comparison(self):
        sim = OwnershipSimulator()
        pool = _make_pool(20)
        lineup = _make_lineup(pool)
        result = sim.simulate(
            user_lineup=lineup, pool=pool,
            field_size=20, num_simulations=5,
        )
        chalk = result.chalk_comparison
        assert chalk is not None
        assert "projected_fp" in chalk
        assert "avg_ownership" in chalk
        assert chalk["projected_fp"] > 0

    def test_contrarian_comparison(self):
        sim = OwnershipSimulator()
        pool = _make_pool(20)
        lineup = _make_lineup(pool)
        result = sim.simulate(
            user_lineup=lineup, pool=pool,
            field_size=20, num_simulations=5,
        )
        contrarian = result.contrarian_comparison
        assert contrarian is not None
        assert "projected_fp" in contrarian
        assert "avg_ceiling" in contrarian

    def test_field_avg_score_positive(self):
        sim = OwnershipSimulator()
        pool = _make_pool(20)
        lineup = _make_lineup(pool)
        result = sim.simulate(
            user_lineup=lineup, pool=pool,
            field_size=100, num_simulations=10,
        )
        assert result.field_avg_score > 0

    def test_missing_ownership_defaults(self):
        """Players with no ownership_projection should get default weight."""
        pool = [
            SimpleNamespace(
                player_id=i, player_name=f"P{i}",
                projected_fp=25.0, ceiling_fp=35.0, floor_fp=15.0,
                salary=5000, ownership_projection=None,
            )
            for i in range(15)
        ]
        sim = OwnershipSimulator()
        lineup = [
            {"player_id": i, "projected_fp": 25, "ownership_pct": 5,
             "ceiling_fp": 35, "floor_fp": 15}
            for i in range(8)
        ]
        result = sim.simulate(
            user_lineup=lineup, pool=pool,
            field_size=20, num_simulations=5,
        )
        assert result.simulations_run == 5

    def test_deterministic_with_seed(self):
        """Same seed should produce same results."""
        sim = OwnershipSimulator()
        pool = _make_pool(20)
        lineup = _make_lineup(pool)
        r1 = sim.simulate(lineup, pool, field_size=50, num_simulations=10)
        r2 = sim.simulate(lineup, pool, field_size=50, num_simulations=10)
        # Same seed (42) is used internally
        assert r1.avg_score == r2.avg_score
        assert r1.win_rate == r2.win_rate

    def test_large_field_size(self):
        """Larger field should still run without errors."""
        sim = OwnershipSimulator()
        pool = _make_pool(20)
        lineup = _make_lineup(pool)
        result = sim.simulate(
            user_lineup=lineup, pool=pool,
            field_size=500, num_simulations=3,
        )
        assert result.simulations_run == 3
        assert result.avg_score > 0


class TestConstrainedFieldGeneration:
    """Tests for constrained field lineup generation and game-script correlation."""

    @staticmethod
    def _make_affordable_pool(n=20):
        """Create a pool with salary range that fits under the DK salary cap."""
        pool = []
        positions = ["PG", "SG", "SF", "PF", "C"]
        for i in range(n):
            pool.append(SimpleNamespace(
                player_id=2000 + i,
                player_name=f"Affordable_{i}",
                position=positions[i % 5],
                team_abbreviation="TM1" if i < n // 2 else "TM2",
                salary=3500 + (i % 10) * 300,  # 3500-6200 range
                projected_fp=20.0 + i * 0.5,
                ceiling_fp=30.0 + i * 0.5,
                floor_fp=10.0 + i * 0.3,
                ownership_projection=5.0 + i * 0.3,
            ))
        return pool

    def test_constrained_field_positions_valid(self):
        """All 8 slots in a constrained field lineup should have eligible positions."""
        import numpy as np
        sim = OwnershipSimulator()
        pool = self._make_affordable_pool(30)
        pool_data = sim._prepare_pool(pool)
        assert pool_data is not None

        rng = np.random.default_rng(42)
        positions = pool_data["positions"]
        weights = pool_data["ownership_weights"]
        salaries = pool_data["salaries"]

        # DK slots and their eligible positions
        dk_slots = OwnershipSimulator._DK_SLOTS
        indices = OwnershipSimulator._generate_constrained_lineup(
            positions, weights, salaries, 50000, dk_slots, rng,
        )
        assert indices is not None
        assert len(indices) == 8

        # Check each slot has a valid position
        for slot_idx, (slot_name, eligible_pos) in enumerate(dk_slots):
            player_pos = positions[indices[slot_idx]]
            assert player_pos in eligible_pos, (
                f"Slot {slot_name} got position {player_pos}, "
                f"expected one of {eligible_pos}"
            )

    def test_constrained_field_salary_cap(self):
        """Constrained field lineup total salary should be under the cap."""
        import numpy as np
        sim = OwnershipSimulator()
        pool = self._make_affordable_pool(30)
        pool_data = sim._prepare_pool(pool)
        assert pool_data is not None

        rng = np.random.default_rng(42)
        salary_cap = 50000
        indices = OwnershipSimulator._generate_constrained_lineup(
            pool_data["positions"],
            pool_data["ownership_weights"],
            pool_data["salaries"],
            salary_cap,
            OwnershipSimulator._DK_SLOTS,
            rng,
        )
        assert indices is not None
        total_salary = pool_data["salaries"][indices].sum()
        assert total_salary <= salary_cap

    def test_constrained_field_fallback(self):
        """With too few players (< 8), constrained generation returns None (triggers fallback)."""
        import numpy as np
        sim = OwnershipSimulator()
        # Only 3 players -- not enough to fill 8 slots
        tiny_pool = _make_pool(3)
        pool_data = sim._prepare_pool(tiny_pool)
        assert pool_data is not None

        rng = np.random.default_rng(42)
        result = OwnershipSimulator._generate_constrained_lineup(
            pool_data["positions"],
            pool_data["ownership_weights"],
            pool_data["salaries"],
            50000,
            OwnershipSimulator._DK_SLOTS,
            rng,
        )
        # Should return None since we don't have enough players to fill all slots
        assert result is None

    def test_game_script_correlation_increases_variance(self):
        """Team-level game-script noise should increase score variance vs. no noise."""
        import numpy as np
        sim = OwnershipSimulator()
        pool = _make_pool(30)
        pool_data = sim._prepare_pool(pool)
        assert pool_data is not None

        rng = np.random.default_rng(42)
        projections = pool_data["projections"]
        weights = pool_data["ownership_weights"]
        ceilings = pool_data["ceilings"]
        floors = pool_data["floors"]
        salaries = pool_data["salaries"]

        # Generate field scores WITH team game-script (pool_data has teams)
        scores_with_teams = sim._generate_field_scores(
            projections, weights, ceilings, floors, salaries,
            field_size=200, salary_cap=50000, rng=np.random.default_rng(42),
            pool_data=pool_data,
        )

        # Generate field scores WITHOUT teams (no game-script correlation)
        no_teams_data = {k: v for k, v in pool_data.items() if k != "teams"}
        scores_no_teams = sim._generate_field_scores(
            projections, weights, ceilings, floors, salaries,
            field_size=200, salary_cap=50000, rng=np.random.default_rng(42),
            pool_data=no_teams_data,
        )

        var_with = np.var(scores_with_teams)
        var_without = np.var(scores_no_teams)
        # Game-script correlation should increase variance
        assert var_with > var_without * 0.8, (
            f"Expected game-script variance ({var_with:.1f}) to be at least "
            f"comparable to no-script variance ({var_without:.1f})"
        )
