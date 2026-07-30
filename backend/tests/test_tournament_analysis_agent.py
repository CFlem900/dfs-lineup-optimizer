"""Tests for Agent 12 — Tournament Analysis Agent.

Covers the deterministic ``compute_field_statistics()`` function
and the ``TournamentAnalysisAgent.build_player_team_map()`` helper.
"""

import pytest
from app.services.agents.tournament_analysis_agent import (
    compute_field_statistics,
    _compute_pool_stats,
    _tier_label,
    _mean,
    _median,
    _normalize_name,
    DK_SALARY_CAP,
    TournamentAnalysisAgent,
)


# ---------------------------------------------------------------------------
# Fixtures — realistic DraftKings lineup data
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


WINNER_1 = _make_entry(1, 320.0, 49_800, [
    _make_player("PG", "Luka Doncic", 10_200, 65.0),
    _make_player("SG", "Jaylen Brown", 7_800, 42.0),
    _make_player("SF", "LeBron James", 9_500, 55.0),
    _make_player("PF", "Jayson Tatum", 8_200, 48.0),
    _make_player("C", "Nikola Jokic", 10_800, 70.0),
    _make_player("G", "Derrick White", 4_500, 22.0),
    _make_player("F", "Cam Johnson", 3_800, 18.0),
    _make_player("UTIL", "No Name", 0, 0),  # dummy
])

WINNER_2 = _make_entry(2, 305.0, 49_200, [
    _make_player("PG", "Shai Gilgeous-Alexander", 9_800, 58.0),
    _make_player("SG", "Anthony Edwards", 8_000, 45.0),
    _make_player("SF", "Kevin Durant", 8_600, 50.0),
    _make_player("PF", "Giannis Antetokounmpo", 10_400, 62.0),
    _make_player("C", "Bam Adebayo", 7_200, 38.0),
    _make_player("G", "Marcus Smart", 4_200, 20.0),
    _make_player("F", "Herb Jones", 3_600, 15.0),
    _make_player("UTIL", "No Name", 0, 0),
])

FIELD_1 = _make_entry(500, 240.0, 48_000, [
    _make_player("PG", "Tyrese Haliburton", 7_000, 30.0),
    _make_player("SG", "Jaylen Brown", 7_800, 35.0),
    _make_player("SF", "Paul George", 6_200, 28.0),
    _make_player("PF", "Julius Randle", 6_000, 25.0),
    _make_player("C", "Nikola Jokic", 10_800, 60.0),
    _make_player("G", "Tyrese Maxey", 5_500, 22.0),
    _make_player("F", "Dorian Finney-Smith", 3_200, 12.0),
    _make_player("UTIL", "No Name", 0, 0),
])

FIELD_2 = _make_entry(800, 220.0, 47_500, [
    _make_player("PG", "Luka Doncic", 10_200, 50.0),
    _make_player("SG", "Norman Powell", 4_800, 20.0),
    _make_player("SF", "OG Anunoby", 5_200, 22.0),
    _make_player("PF", "Draymond Green", 4_600, 18.0),
    _make_player("C", "Bam Adebayo", 7_200, 35.0),
    _make_player("G", "Derrick White", 4_500, 18.0),
    _make_player("F", "Aaron Wiggins", 3_500, 14.0),
    _make_player("UTIL", "No Name", 0, 0),
])


# Player-team mapping for stacking tests
TEAM_MAP = {
    "luka doncic": "DAL",
    "jaylen brown": "BOS",
    "lebron james": "LAL",
    "jayson tatum": "BOS",
    "nikola jokic": "DEN",
    "derrick white": "BOS",
    "cam johnson": "BKN",
    "shai gilgeous-alexander": "OKC",
    "anthony edwards": "MIN",
    "kevin durant": "PHX",
    "giannis antetokounmpo": "MIL",
    "bam adebayo": "MIA",
    "marcus smart": "MEM",
    "herb jones": "NOP",
    "tyrese haliburton": "IND",
    "paul george": "PHI",
    "julius randle": "MIN",
    "tyrese maxey": "PHI",
    "dorian finney-smith": "BKN",
    "norman powell": "LAC",
    "og anunoby": "NYK",
    "draymond green": "GSW",
    "aaron wiggins": "OKC",
}


# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------

class TestHelpers:
    def test_tier_label_high(self):
        assert _tier_label(10_000) == "high"
        assert _tier_label(8_000) == "high"

    def test_tier_label_mid(self):
        assert _tier_label(7_999) == "mid"
        assert _tier_label(5_000) == "mid"

    def test_tier_label_value(self):
        assert _tier_label(4_999) == "value"
        assert _tier_label(3_000) == "value"
        assert _tier_label(0) == "value"

    def test_mean_normal(self):
        assert _mean([10.0, 20.0, 30.0]) == 20.0

    def test_mean_empty(self):
        assert _mean([]) == 0.0

    def test_median_odd(self):
        assert _median([1.0, 3.0, 5.0]) == 3.0

    def test_median_even(self):
        assert _median([1.0, 2.0, 3.0, 4.0]) == 2.5

    def test_median_empty(self):
        assert _median([]) == 0.0

    def test_normalize_name(self):
        assert _normalize_name("LeBron James") == "lebron james"
        assert _normalize_name("  Luka Doncic  ") == "luka doncic"
        assert _normalize_name("") == ""
        assert _normalize_name(None) == ""


# ---------------------------------------------------------------------------
# compute_field_statistics tests
# ---------------------------------------------------------------------------

class TestComputeFieldStatistics:
    """Tests for the main pre-computation function."""

    def test_basic_structure(self):
        stats = compute_field_statistics(
            [WINNER_1, WINNER_2], [FIELD_1, FIELD_2],
        )
        assert "winners" in stats
        assert "field" in stats
        assert "salary_allocation_delta" in stats
        assert "winning_threshold" in stats

    def test_winner_entry_count(self):
        stats = compute_field_statistics(
            [WINNER_1, WINNER_2], [FIELD_1, FIELD_2],
        )
        assert stats["winners"]["entry_count"] == 2
        assert stats["field"]["entry_count"] == 2

    def test_salary_allocation(self):
        stats = compute_field_statistics(
            [WINNER_1, WINNER_2], [FIELD_1, FIELD_2],
        )
        w_alloc = stats["winners"]["salary_allocation"]
        # PG: (10200 + 9800) / 2 = 10000
        assert w_alloc["PG"]["avg_salary"] == 10_000.0
        # C: (10800 + 7200) / 2 = 9000
        assert w_alloc["C"]["avg_salary"] == 9_000.0
        # Check pct of cap
        assert w_alloc["PG"]["pct_of_cap"] == round(10_000 / 50_000 * 100, 1)

    def test_salary_allocation_delta(self):
        stats = compute_field_statistics(
            [WINNER_1, WINNER_2], [FIELD_1, FIELD_2],
        )
        delta = stats["salary_allocation_delta"]
        # PG winners avg 10000, field avg (7000+10200)/2 = 8600 → delta 1400
        assert delta["PG"] == 1_400.0

    def test_salary_tiers(self):
        stats = compute_field_statistics(
            [WINNER_1, WINNER_2], [FIELD_1, FIELD_2],
        )
        w_tiers = stats["winners"]["salary_tiers"]
        # Winner 1: high=[10200,9500,8200,10800]=4, mid=[7800,4500]=2*, value=[3800,0]=2
        # Actually: 7800 is mid, 4500 is value, 3800 is value, 0 is value
        # high: 10200,9500,8200,10800 = 4
        # mid: 7800 = 1
        # value: 4500,3800,0 = 3
        # Winner 2: high=9800,8000,8600,10400=4, mid=7200=1, value=4200,3600,0=3
        # avg high = (4+4)/2 = 4.0
        assert w_tiers["high"] == 4.0

    def test_salary_utilization(self):
        stats = compute_field_statistics(
            [WINNER_1, WINNER_2], [FIELD_1, FIELD_2],
        )
        w_util = stats["winners"]["salary_utilization"]
        # (49800 + 49200) / 2 = 49500
        assert w_util["avg_total_salary"] == 49_500.0
        assert w_util["avg_remaining"] == 500.0
        assert w_util["pct_used"] == 99.0

    def test_points_distribution(self):
        stats = compute_field_statistics(
            [WINNER_1, WINNER_2], [FIELD_1, FIELD_2],
        )
        w_points = stats["winners"]["points"]
        assert w_points["avg"] == _mean([320.0, 305.0])
        assert w_points["min"] == 305.0
        assert w_points["max"] == 320.0

    def test_winning_threshold(self):
        stats = compute_field_statistics(
            [WINNER_1, WINNER_2], [FIELD_1, FIELD_2],
        )
        assert stats["winning_threshold"] == 305.0

    def test_ownership_unique_players(self):
        stats = compute_field_statistics(
            [WINNER_1, WINNER_2], [FIELD_1, FIELD_2],
        )
        w_own = stats["winners"]["ownership"]
        # Each winner has 8 players, but some names are unique across the 2 lineups
        # Winner1: Luka, Jaylen, LeBron, Tatum, Jokic, White, CamJ, NoName
        # Winner2: SGA, Ant, KD, Giannis, Bam, Smart, Herb, NoName
        # NoName appears in both → 15 unique
        assert w_own["unique_players"] == 15  # 2×8 - 1 shared "no name"

    def test_stacking_without_team_map(self):
        stats = compute_field_statistics(
            [WINNER_1, WINNER_2], [FIELD_1, FIELD_2],
        )
        assert stats["winners"]["stacking"]["data_available"] is False

    def test_stacking_with_team_map(self):
        stats = compute_field_statistics(
            [WINNER_1, WINNER_2], [FIELD_1, FIELD_2],
            player_team_map=TEAM_MAP,
        )
        w_stacking = stats["winners"]["stacking"]
        assert w_stacking["data_available"] is True
        # Winner 1 has BOS×3 (Jaylen, Tatum, White) → 3-man stack
        assert w_stacking["pct_with_3man_stack"] > 0
        # Winner 2 has no same-team pairs → 0
        # So pct_with_2man_stack = 1/2 = 0.5 (only winner 1)
        assert w_stacking["pct_with_2man_stack"] == 0.5

    def test_empty_winners(self):
        stats = compute_field_statistics([], [FIELD_1, FIELD_2])
        assert stats["winners"] == {}

    def test_empty_field(self):
        stats = compute_field_statistics([WINNER_1], [])
        assert stats["field"] == {}
        assert stats["winners"]["entry_count"] == 1

    def test_both_empty(self):
        stats = compute_field_statistics([], [])
        assert stats["winners"] == {}
        assert stats["field"] == {}

    def test_single_entry(self):
        stats = compute_field_statistics([WINNER_1], [FIELD_1])
        assert stats["winners"]["entry_count"] == 1
        assert stats["field"]["entry_count"] == 1


# ---------------------------------------------------------------------------
# _compute_pool_stats tests
# ---------------------------------------------------------------------------

class TestComputePoolStats:
    def test_empty_entries(self):
        assert _compute_pool_stats([]) == {}

    def test_entry_with_no_lineup_data(self):
        entry = _make_entry(1, 100, 40_000, [])
        result = _compute_pool_stats([entry])
        assert result["entry_count"] == 1
        assert result["salary_allocation"] == {}

    def test_salary_median(self):
        result = _compute_pool_stats([WINNER_1, WINNER_2])
        # PG salaries: 10200, 9800 → median = (9800+10200)/2 = 10000
        assert result["salary_allocation"]["PG"]["median_salary"] == 10_000.0


# ---------------------------------------------------------------------------
# build_player_team_map tests
# ---------------------------------------------------------------------------

class TestBuildPlayerTeamMap:
    def test_basic_mapping(self):
        pool = [
            {"player_name": "LeBron James", "team_abbreviation": "LAL"},
            {"player_name": "Luka Doncic", "team_abbreviation": "DAL"},
        ]
        mapping = TournamentAnalysisAgent.build_player_team_map(pool)
        assert mapping["lebron james"] == "LAL"
        assert mapping["luka doncic"] == "DAL"

    def test_alternate_keys(self):
        pool = [{"name": "Kevin Durant", "team": "PHX"}]
        mapping = TournamentAnalysisAgent.build_player_team_map(pool)
        assert mapping["kevin durant"] == "PHX"

    def test_empty_pool(self):
        assert TournamentAnalysisAgent.build_player_team_map([]) == {}

    def test_missing_fields(self):
        pool = [{"player_name": "Test", "team_abbreviation": ""}]
        mapping = TournamentAnalysisAgent.build_player_team_map(pool)
        assert len(mapping) == 0  # Empty team should be excluded

    def test_case_normalization(self):
        pool = [
            {"player_name": "  LEBRON JAMES  ", "team_abbreviation": "lal"},
        ]
        mapping = TournamentAnalysisAgent.build_player_team_map(pool)
        assert mapping["lebron james"] == "LAL"


# ---------------------------------------------------------------------------
# TournamentAnalysis model backward compatibility
# ---------------------------------------------------------------------------

class TestTournamentAnalysisModel:
    def test_per_adjustment_reasoning_default(self):
        from app.models.ai import TournamentAnalysis

        # Should work without per_adjustment_reasoning (backward compat)
        analysis = TournamentAnalysis(
            contest_count=5,
            winning_entry_count=10,
            calibration_adjustments={"position_PG_bias": 1.03},
            reasoning="Test reasoning",
        )
        assert analysis.per_adjustment_reasoning == {}

    def test_per_adjustment_reasoning_populated(self):
        from app.models.ai import TournamentAnalysis

        analysis = TournamentAnalysis(
            contest_count=5,
            winning_entry_count=10,
            calibration_adjustments={"position_PG_bias": 1.03},
            per_adjustment_reasoning={
                "position_PG_bias": "Winners spent more at PG",
            },
            reasoning="Test reasoning",
        )
        assert analysis.per_adjustment_reasoning["position_PG_bias"] == "Winners spent more at PG"
