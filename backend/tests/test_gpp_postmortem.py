"""Tests for Agent 9 GPP Post-Mortem — compute_gpp_blueprint(),
compute_deterministic_gpp_constraints(), GPPContestBlueprint model,
and CalibrationService GPP getters.
"""

import pytest
from app.services.agents.backtesting_agent import (
    compute_gpp_blueprint,
    compute_deterministic_gpp_constraints,
)
from app.models.ai import GPPContestBlueprint, GPPBlueprintStats, GPPConstraintRecommendation


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_entry(rank, points, total_salary, lineup_data):
    return {
        "rank": rank,
        "points": points,
        "total_salary": total_salary,
        "lineup_data": lineup_data,
    }


def _make_player(slot, name, salary, fpts=0):
    return {
        "roster_slot": slot,
        "player_name": name,
        "salary": salary,
        "fpts": fpts,
    }


# Entry 1: BOS x3 stack (Jaylen, Tatum, White), $49,800 salary
TOP_1 = _make_entry(1, 350.0, 49_800, [
    _make_player("PG", "Luka Doncic", 10_200, 72.0),
    _make_player("SG", "Jaylen Brown", 7_800, 48.0),
    _make_player("SF", "LeBron James", 9_500, 58.0),
    _make_player("PF", "Jayson Tatum", 8_200, 52.0),
    _make_player("C", "Nikola Jokic", 10_800, 75.0),
    _make_player("G", "Derrick White", 4_500, 25.0),
    _make_player("F", "Cam Johnson", 3_800, 20.0),
    _make_player("UTIL", "Aaron Wiggins", 3_500, 15.0),
])

# Entry 2: DAL x2 stack, lower salary
TOP_2 = _make_entry(2, 335.0, 48_500, [
    _make_player("PG", "Luka Doncic", 10_200, 65.0),
    _make_player("SG", "Anthony Edwards", 8_400, 52.0),
    _make_player("SF", "Kyrie Irving", 7_600, 45.0),
    _make_player("PF", "Pascal Siakam", 6_800, 38.0),
    _make_player("C", "Anthony Davis", 9_200, 60.0),
    _make_player("G", "Tyrese Maxey", 6_300, 35.0),
    _make_player("F", "Herbert Jones", 3_500, 18.0),
    _make_player("UTIL", "Jalen Williams", 4_200, 22.0),
])


TEAM_MAP = {
    "luka doncic": "DAL",
    "jaylen brown": "BOS",
    "lebron james": "LAL",
    "jayson tatum": "BOS",
    "nikola jokic": "DEN",
    "derrick white": "BOS",
    "cam johnson": "BKN",
    "aaron wiggins": "OKC",
    "anthony edwards": "MIN",
    "kyrie irving": "DAL",
    "pascal siakam": "IND",
    "anthony davis": "LAL",
    "tyrese maxey": "PHI",
    "herbert jones": "NOP",
    "jalen williams": "OKC",
}

OWN_MAP = {
    "luka doncic": 28.0,
    "jaylen brown": 15.0,
    "lebron james": 22.0,
    "jayson tatum": 18.0,
    "nikola jokic": 30.0,
    "derrick white": 8.0,
    "cam johnson": 5.0,
    "aaron wiggins": 3.0,
    "anthony edwards": 20.0,
    "kyrie irving": 12.0,
    "pascal siakam": 11.0,
    "anthony davis": 25.0,
    "tyrese maxey": 14.0,
    "herbert jones": 4.0,
    "jalen williams": 9.0,
}


# ---------------------------------------------------------------------------
# compute_gpp_blueprint
# ---------------------------------------------------------------------------


class TestComputeGppBlueprint:
    def test_empty_entries(self):
        assert compute_gpp_blueprint([]) == {}

    def test_basic_structure(self):
        stats = compute_gpp_blueprint([TOP_1])
        assert stats["sample_size"] == 1
        assert stats["avg_total_salary"] == 49_800.0
        assert "salary_by_slot" in stats
        assert "PG" in stats["salary_by_slot"]
        assert stats["salary_by_slot"]["PG"] == 10_200.0
        assert stats["salary_by_slot"]["C"] == 10_800.0

    def test_multi_entry_averaging(self):
        stats = compute_gpp_blueprint([TOP_1, TOP_2])
        assert stats["sample_size"] == 2
        # avg salary: (49800 + 48500) / 2 = 49150
        assert stats["avg_total_salary"] == 49_150.0
        # avg points: (350 + 335) / 2 = 342.5
        assert stats["avg_points"] == 342.5

    def test_salary_floor_pct(self):
        stats = compute_gpp_blueprint([TOP_1, TOP_2])
        # min salary is 48500, floor_pct = 48500 / 50000 * 100 = 97.0
        assert stats["salary_floor_pct"] == 97.0

    def test_stacking_detection_with_team_map(self):
        stats = compute_gpp_blueprint([TOP_1], player_team_map=TEAM_MAP)
        # TOP_1 has BOS x3 (Jaylen, Tatum, White)
        assert stats["pct_with_3man_stack"] == 1.0
        assert stats["pct_with_2man_stack"] == 1.0
        assert stats["avg_max_stack_size"] == 3.0

    def test_stacking_2man_only(self):
        stats = compute_gpp_blueprint([TOP_2], player_team_map=TEAM_MAP)
        # TOP_2 has DAL x2 (Luka, Kyrie)
        assert stats["pct_with_2man_stack"] == 1.0
        assert stats["pct_with_3man_stack"] == 0.0
        assert stats["avg_max_stack_size"] == 2.0

    def test_no_stacking_without_team_map(self):
        stats = compute_gpp_blueprint([TOP_1])
        assert stats["pct_with_2man_stack"] == 0
        assert stats["pct_with_3man_stack"] == 0

    def test_ownership_tracking(self):
        stats = compute_gpp_blueprint([TOP_1], ownership_map=OWN_MAP)
        # Sum: 28+15+22+18+30+8+5+3 = 129
        assert stats["avg_total_ownership"] == 129.0

    def test_low_own_and_chalk_counts(self):
        stats = compute_gpp_blueprint([TOP_1], ownership_map=OWN_MAP)
        # Low (<10%): White(8), Cam(5), Wiggins(3) = 3
        assert stats["avg_low_own_players"] == 3.0
        # Chalk (>25%): Luka(28), Jokic(30) = 2
        assert stats["avg_chalk_players"] == 2.0

    def test_no_ownership_without_map(self):
        stats = compute_gpp_blueprint([TOP_1])
        assert stats["avg_total_ownership"] == 0.0
        assert stats["avg_low_own_players"] == 0.0
        assert stats["avg_chalk_players"] == 0.0

    def test_bringback_detection(self):
        stats = compute_gpp_blueprint([TOP_1], player_team_map=TEAM_MAP)
        # TOP_1 has players from multiple teams (BOS, DAL, LAL, DEN, BKN, OKC)
        # with 3+ players spanning 2+ teams → bringback detected
        assert stats["pct_with_bringback"] == 1.0

    def test_median_points(self):
        stats = compute_gpp_blueprint([TOP_1, TOP_2])
        # sorted points: [335, 350], median = (335+350)/2 = 342.5
        assert stats["median_points"] == 342.5

    def test_entry_with_missing_lineup_data(self):
        """Entries with None lineup_data should still compute (empty lineup)."""
        entry = _make_entry(3, 300.0, 49_000, [])
        stats = compute_gpp_blueprint([entry])
        assert stats["sample_size"] == 1
        assert stats["avg_total_salary"] == 49_000.0


# ---------------------------------------------------------------------------
# compute_deterministic_gpp_constraints
# ---------------------------------------------------------------------------


class TestComputeDeterministicGppConstraints:
    def test_empty_stats(self):
        assert compute_deterministic_gpp_constraints({}) == {}

    def test_small_sample_guard(self):
        assert compute_deterministic_gpp_constraints({"sample_size": 3}) == {}

    def test_ownership_cap_recommendation(self):
        stats = {"sample_size": 20, "avg_total_ownership": 120.0}
        overrides = compute_deterministic_gpp_constraints(stats)
        # Blended: 120*0.7 + 135*0.3 = 84+40.5 = 124.5
        assert "gpp_ownership_cap" in overrides
        assert overrides["gpp_ownership_cap"] == 124.5

    def test_ownership_cap_with_custom_defaults(self):
        stats = {"sample_size": 20, "avg_total_ownership": 100.0}
        defaults = {"gpp_ownership_cap": 150.0}
        overrides = compute_deterministic_gpp_constraints(stats, defaults)
        # 100*0.7 + 150*0.3 = 70+45 = 115.0
        assert overrides["gpp_ownership_cap"] == 115.0

    def test_pivot_min_increase(self):
        stats = {"sample_size": 20, "avg_low_own_players": 2.5}
        overrides = compute_deterministic_gpp_constraints(stats)
        assert overrides.get("gpp_pivot_min_count") == 2.0

    def test_pivot_min_decrease(self):
        stats = {"sample_size": 20, "avg_low_own_players": 0.3}
        overrides = compute_deterministic_gpp_constraints(stats)
        assert overrides.get("gpp_pivot_min_count") == 0.0

    def test_pivot_min_no_change(self):
        stats = {"sample_size": 20, "avg_low_own_players": 1.0}
        overrides = compute_deterministic_gpp_constraints(stats)
        assert "gpp_pivot_min_count" not in overrides

    def test_bringback_relaxation(self):
        stats = {"sample_size": 20, "pct_with_bringback": 0.2}
        overrides = compute_deterministic_gpp_constraints(stats)
        assert overrides.get("gpp_bringback_salary_threshold") == 9500.0

    def test_bringback_no_change_when_common(self):
        stats = {"sample_size": 20, "pct_with_bringback": 0.6}
        overrides = compute_deterministic_gpp_constraints(stats)
        assert "gpp_bringback_salary_threshold" not in overrides

    def test_salary_floor(self):
        stats = {"sample_size": 20, "salary_floor_pct": 99.0}
        overrides = compute_deterministic_gpp_constraints(stats)
        # 99 - 2 = 97.0
        assert overrides.get("gpp_salary_floor_pct") == 97.0

    def test_salary_floor_minimum(self):
        stats = {"sample_size": 20, "salary_floor_pct": 95.0}
        overrides = compute_deterministic_gpp_constraints(stats)
        # 95 - 2 = 93, clamped to 94
        assert overrides.get("gpp_salary_floor_pct") == 94.0


# ---------------------------------------------------------------------------
# GPPContestBlueprint model
# ---------------------------------------------------------------------------


class TestGPPContestBlueprintModel:
    def test_default_construction(self):
        bp = GPPContestBlueprint()
        assert bp.contest_count == 0
        assert bp.constraint_overrides == {}
        assert bp.observed_stats.sample_size == 0

    def test_full_construction(self):
        bp = GPPContestBlueprint(
            contest_count=5,
            top_n_analyzed=10,
            observed_stats=GPPBlueprintStats(
                sample_size=50,
                avg_total_salary=49500,
                avg_total_ownership=120.0,
            ),
            constraint_overrides={"gpp_ownership_cap": 125.0},
            reasoning="Winners used tight salary",
        )
        assert bp.observed_stats.avg_total_salary == 49500
        assert bp.constraint_overrides["gpp_ownership_cap"] == 125.0

    def test_recommendation_model(self):
        rec = GPPConstraintRecommendation(
            constraint_key="gpp_ownership_cap",
            current_value=135.0,
            recommended_value=125.0,
            confidence=0.8,
            reasoning="Winners averaged 120% total ownership",
        )
        assert rec.confidence == 0.8

    def test_blueprint_with_dict_stats(self):
        """GPPBlueprintStats can be constructed from a dict (compute_gpp_blueprint output)."""
        stats_dict = {
            "sample_size": 10,
            "avg_total_salary": 49600.0,
            "salary_floor_pct": 97.0,
            "avg_total_ownership": 118.0,
            "median_total_ownership": 115.0,
            "avg_points": 340.0,
            "median_points": 338.0,
            "pct_with_2man_stack": 0.8,
            "pct_with_3man_stack": 0.3,
            "pct_with_bringback": 0.6,
            "avg_max_stack_size": 2.5,
            "avg_low_own_players": 2.0,
            "avg_chalk_players": 1.5,
            "salary_by_slot": {"PG": 8500, "C": 9000},
        }
        bp = GPPContestBlueprint(
            contest_count=3,
            observed_stats=stats_dict,
            constraint_overrides={"gpp_ownership_cap": 120.0},
        )
        assert bp.observed_stats.pct_with_2man_stack == 0.8


# ---------------------------------------------------------------------------
# CalibrationService GPP getters
# ---------------------------------------------------------------------------


class TestCalibrationServiceGppGetters:
    def _make_svc(self, cache=None):
        from app.services.calibration_service import CalibrationService
        svc = CalibrationService()
        svc._cache = cache or {}
        return svc

    def test_get_gpp_ownership_cap_empty(self):
        svc = self._make_svc()
        assert svc.get_gpp_ownership_cap() is None

    def test_get_gpp_ownership_cap_valid(self):
        svc = self._make_svc({"gpp_ownership_cap": 125.0})
        assert svc.get_gpp_ownership_cap() == 125.0

    def test_get_gpp_ownership_cap_invalid_low(self):
        svc = self._make_svc({"gpp_ownership_cap": 5.0})
        assert svc.get_gpp_ownership_cap() is None

    def test_get_gpp_ownership_cap_invalid_high(self):
        svc = self._make_svc({"gpp_ownership_cap": 300.0})
        assert svc.get_gpp_ownership_cap() is None

    def test_get_gpp_pivot_threshold_valid(self):
        svc = self._make_svc({"gpp_pivot_threshold": 8.0})
        assert svc.get_gpp_pivot_threshold() == 8.0

    def test_get_gpp_pivot_threshold_out_of_range(self):
        svc = self._make_svc({"gpp_pivot_threshold": 1.0})
        assert svc.get_gpp_pivot_threshold() is None

    def test_get_gpp_pivot_min_count_valid(self):
        svc = self._make_svc({"gpp_pivot_min_count": 2.0})
        assert svc.get_gpp_pivot_min_count() == 2

    def test_get_gpp_pivot_min_count_returns_int(self):
        svc = self._make_svc({"gpp_pivot_min_count": 1.5})
        result = svc.get_gpp_pivot_min_count()
        assert isinstance(result, int)

    def test_get_gpp_ceiling_weight_valid(self):
        svc = self._make_svc({"gpp_ceiling_weight": 0.35})
        assert svc.get_gpp_ceiling_weight() == 0.35

    def test_get_gpp_ceiling_weight_too_high(self):
        svc = self._make_svc({"gpp_ceiling_weight": 0.80})
        assert svc.get_gpp_ceiling_weight() is None

    def test_get_gpp_bringback_salary_valid(self):
        svc = self._make_svc({"gpp_bringback_salary_threshold": 9000.0})
        assert svc.get_gpp_bringback_salary_threshold() == 9000

    def test_get_gpp_bringback_salary_returns_int(self):
        svc = self._make_svc({"gpp_bringback_salary_threshold": 8750.0})
        result = svc.get_gpp_bringback_salary_threshold()
        assert isinstance(result, int)

    def test_get_gpp_salary_floor_pct_valid(self):
        svc = self._make_svc({"gpp_salary_floor_pct": 97.0})
        assert svc.get_gpp_salary_floor_pct() == 97.0

    def test_get_gpp_salary_floor_pct_too_low(self):
        svc = self._make_svc({"gpp_salary_floor_pct": 85.0})
        assert svc.get_gpp_salary_floor_pct() is None
