"""Tests for the CorrelationService."""

import pytest
from types import SimpleNamespace

from app.services.correlation_service import CorrelationService


@pytest.fixture
def service():
    return CorrelationService()


class TestPearson:
    """Unit tests for the static _pearson method."""

    def test_perfect_positive_correlation(self):
        x = [1.0, 2.0, 3.0, 4.0, 5.0]
        y = [2.0, 4.0, 6.0, 8.0, 10.0]
        corr = CorrelationService._pearson(x, y)
        assert corr is not None
        assert abs(corr - 1.0) < 0.0001

    def test_perfect_negative_correlation(self):
        x = [1.0, 2.0, 3.0, 4.0, 5.0]
        y = [10.0, 8.0, 6.0, 4.0, 2.0]
        corr = CorrelationService._pearson(x, y)
        assert corr is not None
        assert abs(corr - (-1.0)) < 0.0001

    def test_zero_correlation(self):
        """Uncorrelated data should yield near-zero correlation."""
        x = [1.0, 2.0, 3.0, 4.0, 5.0]
        y = [3.0, 1.0, 4.0, 1.0, 5.0]
        corr = CorrelationService._pearson(x, y)
        assert corr is not None
        assert abs(corr) < 0.8  # not perfectly correlated

    def test_too_few_observations(self):
        x = [1.0, 2.0, 3.0]
        y = [4.0, 5.0, 6.0]
        assert CorrelationService._pearson(x, y) is None

    def test_unequal_lengths(self):
        x = [1.0, 2.0, 3.0, 4.0, 5.0]
        y = [1.0, 2.0]
        assert CorrelationService._pearson(x, y) is None

    def test_zero_variance(self):
        """Constant series should return None."""
        x = [5.0, 5.0, 5.0, 5.0, 5.0]
        y = [1.0, 2.0, 3.0, 4.0, 5.0]
        assert CorrelationService._pearson(x, y) is None

    def test_nan_handling(self):
        """NaN values should be filtered out; if enough remain, compute."""
        x = [1.0, float("nan"), 3.0, 4.0, 5.0, 6.0, 7.0]
        y = [2.0, 4.0, float("nan"), 8.0, 10.0, 12.0, 14.0]
        corr = CorrelationService._pearson(x, y)
        # After filtering NaN pairs: (1,2), (4,8), (5,10), (6,12), (7,14) = 5 pairs
        assert corr is not None
        assert abs(corr - 1.0) < 0.0001


class TestBuildGameFPMatrix:
    def test_basic_matrix_building(self):
        rows = [
            SimpleNamespace(player_id=1, player_name="Player A", game_date="2025-01-01", dk_actual_fp=30.0),
            SimpleNamespace(player_id=2, player_name="Player B", game_date="2025-01-01", dk_actual_fp=25.0),
            SimpleNamespace(player_id=1, player_name="Player A", game_date="2025-01-02", dk_actual_fp=35.0),
            SimpleNamespace(player_id=2, player_name="Player B", game_date="2025-01-02", dk_actual_fp=28.0),
        ]
        names, matrix = CorrelationService._build_game_fp_matrix(rows)
        assert len(names) == 2
        assert names[1] == "Player A"
        assert names[2] == "Player B"
        assert len(matrix) == 2
        assert matrix["2025-01-01"][1] == 30.0
        assert matrix["2025-01-02"][2] == 28.0

    def test_skips_none_values(self):
        rows = [
            SimpleNamespace(player_id=1, player_name="A", game_date="2025-01-01", dk_actual_fp=30.0),
            SimpleNamespace(player_id=2, player_name="B", game_date=None, dk_actual_fp=25.0),
            SimpleNamespace(player_id=3, player_name="C", game_date="2025-01-01", dk_actual_fp=None),
        ]
        names, matrix = CorrelationService._build_game_fp_matrix(rows)
        assert len(names) == 1
        assert 1 in names
        assert 2 not in names
        assert 3 not in names

    def test_empty_rows(self):
        names, matrix = CorrelationService._build_game_fp_matrix([])
        assert len(names) == 0
        assert len(matrix) == 0


class TestComputePairCorrelations:
    def _make_data(self, num_games=10):
        """Create two correlated players over num_games."""
        import random
        random.seed(42)
        player_names = {1: "Player A", 2: "Player B"}
        matrix = {}
        for i in range(num_games):
            base = 25 + random.random() * 20
            matrix[f"2025-01-{i+1:02d}"] = {
                1: base + random.random() * 5,
                2: base * 0.8 + random.random() * 5,  # correlated
            }
        return player_names, matrix

    def test_correlated_pair_found(self):
        names, matrix = self._make_data(10)
        pairs = CorrelationService._compute_pair_correlations(names, matrix)
        assert len(pairs) == 1
        assert pairs[0]["player_a_id"] == 1
        assert pairs[0]["player_b_id"] == 2
        assert pairs[0]["correlation"] > 0  # should be positively correlated
        assert pairs[0]["shared_games"] == 10

    def test_min_games_filter(self):
        names, matrix = self._make_data(3)  # only 3 games
        pairs = CorrelationService._compute_pair_correlations(names, matrix, min_games=5)
        assert len(pairs) == 0

    def test_three_players(self):
        """3 players produce 3 pairs."""
        player_names = {1: "A", 2: "B", 3: "C"}
        matrix = {}
        for i in range(8):
            matrix[f"2025-01-{i+1:02d}"] = {
                1: 20.0 + i * 2,
                2: 25.0 + i * 1.5,
                3: 30.0 - i * 1,
            }
        pairs = CorrelationService._compute_pair_correlations(player_names, matrix)
        assert len(pairs) == 3  # (1,2), (1,3), (2,3)
        # Players 1 and 2 should be positively correlated (both increase)
        pair_12 = next(p for p in pairs if p["player_a_id"] == 1 and p["player_b_id"] == 2)
        assert pair_12["correlation"] > 0.9
        # Players 1 and 3 should be negatively correlated (opposite trends)
        pair_13 = next(p for p in pairs if p["player_a_id"] == 1 and p["player_b_id"] == 3)
        assert pair_13["correlation"] < -0.9


class TestCorrelationServiceCache:
    def test_cache_set_and_get(self, service):
        service._cache_set("test_key", {"data": 123})
        assert service._cache_get("test_key") == {"data": 123}

    def test_cache_miss(self, service):
        assert service._cache_get("nonexistent") is None

    def test_cache_expiry(self, service):
        import time as _t
        service._cache_set("exp_key", "value")
        # Manually expire by backdating timestamp
        service._cache_ts["exp_key"] = _t.time() - service._CACHE_TTL - 10
        assert service._cache_get("exp_key") is None

    def test_cache_ttl_is_30_minutes(self, service):
        assert service._CACHE_TTL == 1800
