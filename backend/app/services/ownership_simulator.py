"""Ownership Simulation Service.

Runs Monte Carlo simulations to estimate a DFS lineup's expected
tournament performance against a field of opponents constructed
from ownership projections.

Key metrics:
- Win Rate (1st place)
- Min-Cash Rate (top ~20%)
- Top-1% Rate
- Expected ROI
"""

import logging
import random
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class SimulationResult:
    """Simulation output."""

    avg_percentile: float = 0.0
    win_rate: float = 0.0
    top_1pct_rate: float = 0.0
    top_10pct_rate: float = 0.0
    min_cash_rate: float = 0.0
    expected_roi: float = 0.0
    avg_score: float = 0.0
    score_std: float = 0.0
    field_avg_score: float = 0.0
    simulations_run: int = 0
    chalk_comparison: Optional[Dict[str, float]] = None
    contrarian_comparison: Optional[Dict[str, float]] = None


# ── DraftKings Classic Constants ──────────────────────────────────
DK_SALARY_CAP = 50_000
DK_ROSTER_SIZE = 8
MIN_CASH_PERCENTILE = 0.20  # Top 20% ≈ min cash line


class OwnershipSimulator:
    """Monte Carlo ownership simulation for GPP lineup evaluation."""

    def simulate(
        self,
        user_lineup: List[Dict[str, Any]],
        pool: List,
        field_size: int = 1000,
        num_simulations: int = 500,
        salary_cap: int = DK_SALARY_CAP,
        sport: str = "nba",
    ) -> SimulationResult:
        """Run a Monte Carlo field simulation.

        Parameters
        ----------
        user_lineup : list of dict
            Each dict has: player_id, player_name, projected_fp,
            ownership_pct, ceiling_fp, floor_fp.
        pool : list of PlayerPoolEntry
            Full player pool with ownership / projection data.
        field_size : int
            Number of opponent lineups per simulation.
        num_simulations : int
            Number of independent simulations to run.
        salary_cap : int
            Salary cap for opponent lineup generation.

        Returns
        -------
        SimulationResult with percentile/win/ROI metrics.
        """
        if not user_lineup or not pool:
            return SimulationResult()

        # Pre-compute pool arrays for fast sampling
        pool_data = self._prepare_pool(pool)
        if pool_data is None:
            return SimulationResult()

        projections = pool_data["projections"]
        ownership_weights = pool_data["ownership_weights"]
        ceilings = pool_data["ceilings"]
        floors = pool_data["floors"]
        salaries = pool_data["salaries"]

        # User lineup scoring
        user_proj = np.array([
            p.get("projected_fp", 0) or 0 for p in user_lineup
        ], dtype=np.float64)
        user_ceil = np.array([
            p.get("ceiling_fp", 0) or p.get("projected_fp", 0) or 0
            for p in user_lineup
        ], dtype=np.float64)
        user_floor = np.array([
            p.get("floor_fp", 0) or 0 for p in user_lineup
        ], dtype=np.float64)

        # Simulate
        rng = np.random.default_rng(42)
        wins = 0
        top_1pct = 0
        top_10pct = 0
        min_cashes = 0
        percentile_sum = 0.0
        user_scores: List[float] = []
        field_scores_all: List[float] = []

        for _ in range(num_simulations):
            # Score user lineup with variance
            user_score = self._score_lineup_with_variance(
                user_proj, user_ceil, user_floor, rng
            )
            user_scores.append(user_score)

            # Generate field lineups (position/salary constrained)
            field_scores = self._generate_field_scores(
                projections, ownership_weights, ceilings, floors, salaries,
                field_size, salary_cap, rng, pool_data=pool_data,
            )
            field_scores_all.extend(field_scores)

            # Rank user among field
            rank = sum(1 for s in field_scores if s >= user_score) + 1
            percentile = 1.0 - (rank / (field_size + 1))
            percentile_sum += percentile

            if rank == 1:
                wins += 1
            if percentile >= 0.99:
                top_1pct += 1
            if percentile >= 0.90:
                top_10pct += 1
            if percentile >= (1.0 - MIN_CASH_PERCENTILE):
                min_cashes += 1

        n = num_simulations
        avg_user_score = float(np.mean(user_scores))
        std_user_score = float(np.std(user_scores)) if n > 1 else 0.0
        field_avg = float(np.mean(field_scores_all)) if field_scores_all else 0.0

        # Build comparison benchmarks
        chalk = self._build_chalk_lineup(pool_data)
        contrarian = self._build_contrarian_lineup(pool_data)

        return SimulationResult(
            avg_percentile=round(percentile_sum / n * 100, 1),
            win_rate=round(wins / n * 100, 2),
            top_1pct_rate=round(top_1pct / n * 100, 2),
            top_10pct_rate=round(top_10pct / n * 100, 2),
            min_cash_rate=round(min_cashes / n * 100, 2),
            expected_roi=round((avg_user_score / field_avg - 1.0) * 100, 2)
            if field_avg > 0 else 0.0,
            avg_score=round(avg_user_score, 1),
            score_std=round(std_user_score, 1),
            field_avg_score=round(field_avg, 1),
            simulations_run=n,
            chalk_comparison=chalk,
            contrarian_comparison=contrarian,
        )

    def _prepare_pool(self, pool: List) -> Optional[Dict[str, np.ndarray]]:
        """Convert pool to numpy arrays for fast simulation."""
        if not pool:
            return None

        projections = []
        ownership_weights = []
        ceilings = []
        floors = []
        salaries = []

        positions = []
        teams = []

        for p in pool:
            proj = getattr(p, "projected_fp", 0) or 0
            own = getattr(p, "ownership_projection", None)
            ceil_val = getattr(p, "ceiling_fp", 0) or proj * 1.3
            floor_val = getattr(p, "floor_fp", 0) or proj * 0.5
            sal = getattr(p, "salary", 0) or 0

            projections.append(proj)
            # Ownership weight for sampling (default 5% if missing)
            ownership_weights.append(max(own if own is not None else 5.0, 0.1))
            ceilings.append(ceil_val)
            floors.append(floor_val)
            salaries.append(sal)
            positions.append(getattr(p, "position", "UTIL"))
            teams.append(getattr(p, "team_abbreviation", "UNK") or "UNK")

        weights = np.array(ownership_weights, dtype=np.float64)
        weights /= weights.sum()  # normalise

        return {
            "projections": np.array(projections, dtype=np.float64),
            "ownership_weights": weights,
            "ceilings": np.array(ceilings, dtype=np.float64),
            "floors": np.array(floors, dtype=np.float64),
            "salaries": np.array(salaries, dtype=np.float64),
            "positions": positions,
            "teams": np.array(teams),
        }

    @staticmethod
    def _score_lineup_with_variance(
        projections: np.ndarray,
        ceilings: np.ndarray,
        floors: np.ndarray,
        rng: np.random.Generator,
    ) -> float:
        """Score a lineup with random variance using truncated normal."""
        means = projections
        # Standard deviation proportional to ceiling-floor range
        stds = (ceilings - floors) / 4.0
        stds = np.maximum(stds, 0.5)

        scores = rng.normal(means, stds)
        scores = np.maximum(scores, floors * 0.5)  # soft floor
        return float(scores.sum())

    # DraftKings slot eligibility for constrained field generation
    _DK_SLOTS = [
        ("C", {"C"}),
        ("PG", {"PG"}),
        ("SG", {"SG"}),
        ("SF", {"SF"}),
        ("PF", {"PF"}),
        ("G", {"PG", "SG"}),
        ("F", {"SF", "PF"}),
        ("UTIL", {"PG", "SG", "SF", "PF", "C"}),
    ]

    @staticmethod
    def _generate_constrained_lineup(
        positions: List[str],
        weights: np.ndarray,
        salaries: np.ndarray,
        salary_cap: int,
        slots: List,
        rng: np.random.Generator,
    ) -> Optional[np.ndarray]:
        """Generate a position-valid, salary-constrained lineup.

        Returns an array of player indices or None if impossible.
        """
        used = set()
        salary_left = salary_cap
        indices = []
        n = len(positions)

        for _slot_name, eligible_pos in slots:
            candidates = []
            candidate_weights = []
            for i in range(n):
                if (
                    i not in used
                    and positions[i] in eligible_pos
                    and salaries[i] <= salary_left
                ):
                    candidates.append(i)
                    candidate_weights.append(weights[i])

            if not candidates:
                return None  # Cannot fill this slot

            cw = np.array(candidate_weights, dtype=np.float64)
            cw_sum = cw.sum()
            if cw_sum <= 0:
                return None
            cw /= cw_sum
            idx = rng.choice(candidates, p=cw)
            indices.append(idx)
            used.add(idx)
            salary_left -= salaries[idx]

        return np.array(indices, dtype=np.intp)

    @staticmethod
    def _generate_optimizer_lineup(
        positions: List[str],
        projections: np.ndarray,
        ceilings: np.ndarray,
        weights: np.ndarray,
        salaries: np.ndarray,
        salary_cap: int,
        slots: List,
        rng: np.random.Generator,
        archetype: str = "chalk",
    ) -> Optional[np.ndarray]:
        """Generate a greedy optimizer-archetype lineup.

        Parameters
        ----------
        archetype : str
            "chalk" — greedy by projection / salary (value-based)
            "contrarian" — greedy by ceiling × (1-ownership) / salary

        Returns player indices or None if impossible.
        """
        n = len(positions)
        used = set()
        salary_left = salary_cap
        indices = []

        for _slot_name, eligible_pos in slots:
            candidates = []
            for i in range(n):
                if (
                    i not in used
                    and positions[i] in eligible_pos
                    and salaries[i] <= salary_left
                    and salaries[i] > 0
                ):
                    candidates.append(i)

            if not candidates:
                return None

            # Score each candidate based on archetype
            if archetype == "chalk":
                scores = np.array([
                    projections[i] / (salaries[i] / 1000.0)
                    for i in candidates
                ])
            else:  # contrarian
                scores = np.array([
                    ceilings[i] * (1.0 - weights[i]) / (salaries[i] / 1000.0)
                    for i in candidates
                ])

            # Pick from top-3 with small randomisation (not purely greedy)
            top_k = min(3, len(scores))
            top_indices = np.argsort(scores)[-top_k:]
            top_scores = scores[top_indices]
            top_scores = np.maximum(top_scores, 0.01)
            top_scores /= top_scores.sum()

            chosen = rng.choice(top_indices, p=top_scores)
            idx = candidates[chosen]
            indices.append(idx)
            used.add(idx)
            salary_left -= salaries[idx]

        return np.array(indices, dtype=np.intp)

    def _generate_field_scores(
        self,
        projections: np.ndarray,
        weights: np.ndarray,
        ceilings: np.ndarray,
        floors: np.ndarray,
        salaries: np.ndarray,
        field_size: int,
        salary_cap: int,
        rng: np.random.Generator,
        pool_data: Optional[Dict] = None,
    ) -> List[float]:
        """Generate field opponent lineup scores.

        Uses a mix of 3 archetypes to model realistic DFS fields:
        - 60% ownership-weighted random (casual entrants)
        - 20% chalk optimizer (greedy by value/$)
        - 20% contrarian optimizer (greedy by ceiling×(1-own)/$)

        Falls back to unconstrained random if constrained fails.
        """
        from app.config.constants import (
            FIELD_ARCHETYPE_OWNERSHIP_RANDOM,
            FIELD_ARCHETYPE_CHALK_OPTIMIZER,
            FIELD_ARCHETYPE_CONTRARIAN_OPTIMIZER,
        )

        scores = []
        n_players = len(projections)
        positions = pool_data.get("positions") if pool_data else None
        teams_arr = pool_data.get("teams") if pool_data else None

        # Pre-compute archetype counts
        n_random = int(field_size * FIELD_ARCHETYPE_OWNERSHIP_RANDOM)
        n_chalk = int(field_size * FIELD_ARCHETYPE_CHALK_OPTIMIZER)
        n_contrarian = field_size - n_random - n_chalk  # remainder

        def _score_indices(indices):
            """Score a lineup by its player indices."""
            if teams_arr is not None:
                lineup_teams = teams_arr[indices]
                unique_teams = set(lineup_teams)
                team_noise = {}
                for t in unique_teams:
                    noise = rng.normal(1.0, 0.08)
                    team_noise[t] = max(0.80, min(1.25, noise))
                team_mults = np.array(
                    [team_noise[t] for t in lineup_teams], dtype=np.float64
                )
            else:
                team_mults = np.ones(len(indices), dtype=np.float64)

            lineup_proj = projections[indices] * team_mults
            lineup_ceil = ceilings[indices] * team_mults
            lineup_floor = floors[indices] * team_mults
            return self._score_lineup_with_variance(
                lineup_proj, lineup_ceil, lineup_floor, rng
            )

        # ── Ownership-weighted random lineups ──
        for _ in range(n_random):
            indices = None
            if positions is not None:
                indices = self._generate_constrained_lineup(
                    positions, weights, salaries, salary_cap,
                    self._DK_SLOTS, rng,
                )
            if indices is None or len(indices) != DK_ROSTER_SIZE:
                indices = rng.choice(
                    n_players, size=DK_ROSTER_SIZE, replace=False, p=weights,
                )
            scores.append(_score_indices(indices))

        # ── Chalk optimizer lineups ──
        for _ in range(n_chalk):
            indices = None
            if positions is not None:
                indices = self._generate_optimizer_lineup(
                    positions, projections, ceilings, weights, salaries,
                    salary_cap, self._DK_SLOTS, rng, archetype="chalk",
                )
            if indices is None or len(indices) != DK_ROSTER_SIZE:
                indices = rng.choice(
                    n_players, size=DK_ROSTER_SIZE, replace=False, p=weights,
                )
            scores.append(_score_indices(indices))

        # ── Contrarian optimizer lineups ──
        for _ in range(n_contrarian):
            indices = None
            if positions is not None:
                indices = self._generate_optimizer_lineup(
                    positions, projections, ceilings, weights, salaries,
                    salary_cap, self._DK_SLOTS, rng, archetype="contrarian",
                )
            if indices is None or len(indices) != DK_ROSTER_SIZE:
                indices = rng.choice(
                    n_players, size=DK_ROSTER_SIZE, replace=False, p=weights,
                )
            scores.append(_score_indices(indices))

        return scores

    @staticmethod
    def _build_chalk_lineup(
        pool_data: Dict[str, np.ndarray],
    ) -> Dict[str, float]:
        """Build top-ownership 'chalk' lineup stats."""
        weights = pool_data["ownership_weights"]
        projections = pool_data["projections"]

        # Top 8 by ownership
        top_idx = np.argsort(weights)[-DK_ROSTER_SIZE:]
        return {
            "projected_fp": round(float(projections[top_idx].sum()), 1),
            "avg_ownership": round(float(weights[top_idx].mean() * 100), 1),
        }

    @staticmethod
    def _build_contrarian_lineup(
        pool_data: Dict[str, np.ndarray],
    ) -> Dict[str, float]:
        """Build low-ownership, high-ceiling 'contrarian' lineup stats."""
        weights = pool_data["ownership_weights"]
        ceilings = pool_data["ceilings"]
        projections = pool_data["projections"]

        # Contrarian score: high ceiling, low ownership
        contrarian_scores = ceilings * (1.0 - weights)
        top_idx = np.argsort(contrarian_scores)[-DK_ROSTER_SIZE:]
        return {
            "projected_fp": round(float(projections[top_idx].sum()), 1),
            "avg_ownership": round(float(weights[top_idx].mean() * 100), 1),
            "avg_ceiling": round(float(ceilings[top_idx].mean()), 1),
        }
