"""Tests for Agent 8 — Simulation Tuning Agent.

Covers the deterministic role classification pipeline and sigma profile
computation.  AI refinement is tested via mocking.
"""

import pytest
from unittest.mock import MagicMock, patch

from app.services.agents.simulation_tuning_agent import (
    classify_player_role,
    classify_player_roles,
    compute_role_sigma_profiles,
    compute_role_summary,
    SimulationTuningAgent,
    DEFAULT_SIGMAS,
    ROLE_SIGMA_MULTIPLIERS,
    SIGMA_MIN,
    SIGMA_MAX,
    STAR_SALARY_FLOOR,
    PUNT_SALARY_CEILING,
    PUNT_MINUTES_CEILING,
    STAR_FLOOR_RATIO,
    VOLATILITY_RATIO_THRESHOLD,
    B2B_SIGMA_BOOST,
    INJURY_RETURN_SIGMA_BOOST,
    BLOWOUT_STARTER_BOOST,
    BLOWOUT_SPREAD_THRESHOLD,
    STAT_NAMES,
)


# ---------------------------------------------------------------------------
# Fixtures — realistic player data
# ---------------------------------------------------------------------------

def _make_player(pid, name, pos, salary, fp, floor_fp=None, ceil_fp=None,
                 minutes=28.0, is_b2b=False, injury_status=None,
                 vegas_spread=None):
    """Build a player dict suitable for the tuning agent."""
    floor_fp = floor_fp if floor_fp is not None else round(fp * 0.8, 1)
    ceil_fp = ceil_fp if ceil_fp is not None else round(fp * 1.2, 1)
    return {
        "player_id": pid,
        "player_name": name,
        "position": pos,
        "salary": salary,
        "projected_fp": fp,
        "floor_fp": floor_fp,
        "ceiling_fp": ceil_fp,
        "projected_minutes": minutes,
        "is_b2b": is_b2b,
        "injury_status": injury_status,
        "vegas_spread": vegas_spread,
    }


# Star: high salary, high floor
STAR_PLAYER = _make_player(
    1, "Nikola Jokic", "C", 10_800, 55.0,
    floor_fp=42.0, ceil_fp=70.0, minutes=34.0,
)

# Microwave: mid salary, high volatility
MICROWAVE_PLAYER = _make_player(
    2, "Jordan Clarkson", "SG", 5_500, 25.0,
    floor_fp=10.0, ceil_fp=42.0, minutes=26.0,
)

# 3-and-D: mid salary, low volatility
THREE_D_PLAYER = _make_player(
    3, "Mikal Bridges", "SF", 6_000, 28.0,
    floor_fp=22.0, ceil_fp=35.0, minutes=32.0,
)

# Punt: low salary
PUNT_PLAYER = _make_player(
    4, "Aaron Wiggins", "SF", 3_500, 12.0,
    floor_fp=6.0, ceil_fp=22.0, minutes=20.0,
)

# Punt: low minutes (despite mid salary)
LOW_MIN_PUNT = _make_player(
    5, "Bench Guy", "PF", 5_000, 10.0,
    minutes=14.0,
)

# High salary but volatile floor → microwave
VOLATILE_STAR = _make_player(
    6, "Boom Bust Star", "PG", 9_000, 40.0,
    floor_fp=18.0, ceil_fp=60.0, minutes=32.0,
)


ALL_PLAYERS = [STAR_PLAYER, MICROWAVE_PLAYER, THREE_D_PLAYER,
               PUNT_PLAYER, LOW_MIN_PUNT, VOLATILE_STAR]


# ---------------------------------------------------------------------------
# classify_player_role tests
# ---------------------------------------------------------------------------

class TestClassifyPlayerRole:
    def test_star_classification(self):
        role = classify_player_role(STAR_PLAYER)
        assert role == "star"

    def test_microwave_classification(self):
        role = classify_player_role(MICROWAVE_PLAYER)
        assert role == "microwave"

    def test_three_and_d_classification(self):
        role = classify_player_role(THREE_D_PLAYER)
        assert role == "three_and_d"

    def test_punt_low_salary(self):
        role = classify_player_role(PUNT_PLAYER)
        assert role == "punt"

    def test_punt_low_minutes(self):
        role = classify_player_role(LOW_MIN_PUNT)
        assert role == "punt"

    def test_volatile_star_becomes_microwave(self):
        """High salary + low floor ratio → microwave, not star."""
        role = classify_player_role(VOLATILE_STAR)
        assert role == "microwave"

    def test_star_floor_ratio_boundary(self):
        """Player exactly at STAR_FLOOR_RATIO should be star."""
        player = _make_player(
            10, "Boundary Star", "PF", STAR_SALARY_FLOOR, 40.0,
            floor_fp=40.0 * STAR_FLOOR_RATIO,
            ceil_fp=52.0,
            minutes=30.0,
        )
        assert classify_player_role(player) == "star"

    def test_punt_salary_boundary(self):
        """Player at exact PUNT_SALARY_CEILING should be punt."""
        player = _make_player(
            11, "Edge Punt", "SG", PUNT_SALARY_CEILING, 15.0, minutes=22.0,
        )
        assert classify_player_role(player) == "punt"

    def test_zero_projection_safe(self):
        """Zero projected_fp shouldn't cause division by zero."""
        player = _make_player(12, "Zero", "PG", 6_000, 0.0, minutes=20.0)
        role = classify_player_role(player)
        assert role in ("star", "microwave", "three_and_d", "punt")

    def test_empty_dict_defaults_to_punt(self):
        """Missing fields default to low values → punt."""
        role = classify_player_role({})
        assert role == "punt"


# ---------------------------------------------------------------------------
# classify_player_roles batch tests
# ---------------------------------------------------------------------------

class TestClassifyPlayerRoles:
    def test_batch_classification(self):
        result = classify_player_roles(ALL_PLAYERS)
        assert len(result) == 6
        assert result[1][0] == "star"
        assert result[2][0] == "microwave"
        assert result[3][0] == "three_and_d"
        assert result[4][0] == "punt"

    def test_skips_missing_player_id(self):
        players = [{"player_name": "No ID", "salary": 5000}]
        result = classify_player_roles(players)
        assert len(result) == 0

    def test_summary_data_attached(self):
        result = classify_player_roles([STAR_PLAYER])
        _, summary = result[1]
        assert summary["player_name"] == "Nikola Jokic"
        assert summary["salary"] == 10_800
        assert summary["role"] == "star"

    def test_empty_list(self):
        assert classify_player_roles([]) == {}


# ---------------------------------------------------------------------------
# compute_role_sigma_profiles tests
# ---------------------------------------------------------------------------

class TestComputeRoleSigmaProfiles:
    def test_star_sigmas_below_baseline(self):
        """Star should have tighter (lower) sigmas than defaults."""
        role_map = classify_player_roles([STAR_PLAYER])
        profiles = compute_role_sigma_profiles(role_map)
        star_sigmas = profiles[1]
        for stat in STAT_NAMES:
            assert star_sigmas[stat] < DEFAULT_SIGMAS[stat], (
                f"Star sigma for {stat} should be < baseline"
            )

    def test_microwave_pts_above_baseline(self):
        """Microwave scorer should have wider pts sigma than baseline."""
        role_map = classify_player_roles([MICROWAVE_PLAYER])
        profiles = compute_role_sigma_profiles(role_map)
        mw_sigmas = profiles[2]
        assert mw_sigmas["pts"] > DEFAULT_SIGMAS["pts"]
        assert mw_sigmas["fg3m"] > DEFAULT_SIGMAS["fg3m"]

    def test_three_d_sigmas_below_baseline(self):
        """3-and-D should have tighter sigmas."""
        role_map = classify_player_roles([THREE_D_PLAYER])
        profiles = compute_role_sigma_profiles(role_map)
        td_sigmas = profiles[3]
        for stat in STAT_NAMES:
            assert td_sigmas[stat] <= DEFAULT_SIGMAS[stat], (
                f"3-and-D sigma for {stat} should be <= baseline"
            )

    def test_punt_sigmas_above_baseline(self):
        """Punt play should have wider sigmas than baseline."""
        role_map = classify_player_roles([PUNT_PLAYER])
        profiles = compute_role_sigma_profiles(role_map)
        punt_sigmas = profiles[4]
        for stat in STAT_NAMES:
            assert punt_sigmas[stat] > DEFAULT_SIGMAS[stat], (
                f"Punt sigma for {stat} should be > baseline"
            )

    def test_all_sigmas_within_clamp_range(self):
        """All computed sigmas should be within [SIGMA_MIN, SIGMA_MAX]."""
        role_map = classify_player_roles(ALL_PLAYERS)
        profiles = compute_role_sigma_profiles(role_map)
        for pid, sigmas in profiles.items():
            for stat, val in sigmas.items():
                assert SIGMA_MIN <= val <= SIGMA_MAX, (
                    f"Player {pid} stat {stat}: {val} outside [{SIGMA_MIN}, {SIGMA_MAX}]"
                )

    def test_all_stats_present(self):
        """Every player profile should have all 7 stats."""
        role_map = classify_player_roles(ALL_PLAYERS)
        profiles = compute_role_sigma_profiles(role_map)
        for pid, sigmas in profiles.items():
            assert set(sigmas.keys()) == set(STAT_NAMES), (
                f"Player {pid} missing stats: {set(STAT_NAMES) - set(sigmas.keys())}"
            )

    def test_empty_role_map(self):
        profiles = compute_role_sigma_profiles({})
        assert profiles == {}


# ---------------------------------------------------------------------------
# Situational adjustment tests
# ---------------------------------------------------------------------------

class TestSituationalAdjustments:
    def test_b2b_boosts_sigma(self):
        """B2B player should have higher sigmas than same player without B2B."""
        normal = _make_player(10, "Player", "PG", 6_000, 28.0, minutes=30.0)
        b2b = _make_player(10, "Player", "PG", 6_000, 28.0, minutes=30.0, is_b2b=True)

        normal_map = classify_player_roles([normal])
        b2b_map = classify_player_roles([b2b])

        normal_profiles = compute_role_sigma_profiles(normal_map)
        b2b_profiles = compute_role_sigma_profiles(b2b_map)

        for stat in STAT_NAMES:
            assert b2b_profiles[10][stat] > normal_profiles[10][stat], (
                f"B2B sigma for {stat} should be higher"
            )

    def test_injury_return_boosts_sigma(self):
        """Questionable/GTD player should have much higher sigmas."""
        normal = _make_player(20, "Player", "SG", 7_000, 30.0, minutes=28.0)
        injured = _make_player(
            20, "Player", "SG", 7_000, 30.0, minutes=28.0,
            injury_status="Questionable",
        )

        normal_map = classify_player_roles([normal])
        inj_map = classify_player_roles([injured])

        normal_profiles = compute_role_sigma_profiles(normal_map)
        inj_profiles = compute_role_sigma_profiles(inj_map)

        for stat in STAT_NAMES:
            assert inj_profiles[20][stat] > normal_profiles[20][stat], (
                f"Injury return sigma for {stat} should be higher"
            )

    def test_out_status_no_boost(self):
        """'Out' players should NOT get injury return boost."""
        normal = _make_player(30, "Player", "C", 7_000, 30.0, minutes=28.0)
        out = _make_player(
            30, "Player", "C", 7_000, 30.0, minutes=28.0,
            injury_status="Out",
        )

        normal_profiles = compute_role_sigma_profiles(classify_player_roles([normal]))
        out_profiles = compute_role_sigma_profiles(classify_player_roles([out]))

        # 'Out' should not trigger the INJURY_RETURN_SIGMA_BOOST
        for stat in STAT_NAMES:
            assert out_profiles[30][stat] == normal_profiles[30][stat]

    def test_blowout_risk_boosts_starters(self):
        """Starter in blowout game should have higher sigma."""
        normal = _make_player(40, "Starter", "SF", 7_500, 32.0, minutes=32.0)
        blowout = _make_player(
            40, "Starter", "SF", 7_500, 32.0, minutes=32.0,
            vegas_spread=-8.0,
        )

        normal_profiles = compute_role_sigma_profiles(classify_player_roles([normal]))
        blowout_profiles = compute_role_sigma_profiles(classify_player_roles([blowout]))

        for stat in STAT_NAMES:
            assert blowout_profiles[40][stat] > normal_profiles[40][stat]

    def test_blowout_not_applied_to_bench(self):
        """Bench player (< 24 min) in blowout should NOT get boost."""
        bench = _make_player(
            50, "Bench", "PF", 4_000, 12.0, minutes=18.0,
            vegas_spread=-9.0,
        )
        normal_bench = _make_player(
            50, "Bench", "PF", 4_000, 12.0, minutes=18.0,
        )

        bench_profiles = compute_role_sigma_profiles(classify_player_roles([bench]))
        normal_profiles = compute_role_sigma_profiles(classify_player_roles([normal_bench]))

        # Low minutes → punt classification; blowout shouldn't apply
        for stat in STAT_NAMES:
            assert bench_profiles[50][stat] == normal_profiles[50][stat]

    def test_stacked_situational_boosts(self):
        """B2B + Questionable + blowout should all stack multiplicatively."""
        base = _make_player(60, "Unlucky Star", "PG", 9_000, 42.0,
                            floor_fp=35.0, ceil_fp=55.0, minutes=32.0)
        stacked = _make_player(
            60, "Unlucky Star", "PG", 9_000, 42.0,
            floor_fp=35.0, ceil_fp=55.0, minutes=32.0,
            is_b2b=True, injury_status="GTD", vegas_spread=-8.0,
        )

        base_profiles = compute_role_sigma_profiles(classify_player_roles([base]))
        stacked_profiles = compute_role_sigma_profiles(classify_player_roles([stacked]))

        # All three boosts compound: 1.10 × 1.50 × 1.15 ≈ 1.8975
        for stat in STAT_NAMES:
            ratio = stacked_profiles[60][stat] / base_profiles[60][stat]
            expected_min = B2B_SIGMA_BOOST * INJURY_RETURN_SIGMA_BOOST * BLOWOUT_STARTER_BOOST * 0.95
            assert ratio >= expected_min or stacked_profiles[60][stat] == SIGMA_MAX, (
                f"Stacked boost for {stat}: ratio={ratio:.3f}, "
                f"expected >= {expected_min:.3f}"
            )


# ---------------------------------------------------------------------------
# compute_role_summary tests
# ---------------------------------------------------------------------------

class TestComputeRoleSummary:
    def test_counts(self):
        role_map = classify_player_roles(ALL_PLAYERS)
        summary = compute_role_summary(role_map)
        assert summary["player_count"] == 6
        assert summary["role_counts"]["star"] == 1
        assert summary["role_counts"]["punt"] == 2  # low salary + low minutes

    def test_avg_salary_populated(self):
        role_map = classify_player_roles(ALL_PLAYERS)
        summary = compute_role_summary(role_map)
        assert summary["avg_salary_by_role"]["star"] == 10_800.0

    def test_sample_names_capped(self):
        """Role samples should be ≤ 5 names per role."""
        # Create 10 punt players
        punts = [_make_player(100 + i, f"Punt{i}", "PG", 3_000, 8.0)
                 for i in range(10)]
        role_map = classify_player_roles(punts)
        summary = compute_role_summary(role_map)
        assert len(summary["role_samples"]["punt"]) <= 5

    def test_empty_role_map(self):
        summary = compute_role_summary({})
        assert summary["player_count"] == 0
        assert all(v == 0 for v in summary["role_counts"].values())


# ---------------------------------------------------------------------------
# SimulationTuningAgent.get_player_noise_profiles tests
# ---------------------------------------------------------------------------

class TestSimulationTuningAgent:
    @pytest.fixture
    def mock_ai(self):
        ai = MagicMock()
        ai.is_available = True
        ai.complete_json.return_value = None  # No AI adjustments by default
        return ai

    @pytest.fixture
    def agent(self, mock_ai):
        return SimulationTuningAgent(mock_ai)

    def test_deterministic_always_returns_profiles(self, agent):
        """Agent always returns profiles even without AI."""
        agent._ai.is_available = False
        result = agent.get_player_noise_profiles(ALL_PLAYERS)
        assert len(result) == 6
        for pid in (1, 2, 3, 4, 5, 6):
            assert pid in result
            assert len(result[pid]) == 7

    def test_empty_players(self, agent):
        assert agent.get_player_noise_profiles([]) == {}

    def test_ai_refinement_merges(self, agent):
        """AI adjustments should overlay onto role-based profiles."""
        agent._ai.complete_json.return_value = {
            "adjustments": {
                "1": {
                    "sigma_overrides": {"pts": 0.10},
                    "reason": "Jokic ultra-consistent lately",
                }
            },
            "reasoning": "Jokic has been scoring very consistently",
        }
        # Use a star with a situational flag so AI refinement is triggered
        flagged_star = _make_player(
            1, "Nikola Jokic", "C", 10_800, 55.0,
            floor_fp=42.0, ceil_fp=70.0, minutes=34.0,
            is_b2b=True,  # Edge case flag → triggers AI call
        )
        result = agent.get_player_noise_profiles([flagged_star])
        # AI override should replace pts sigma for player 1
        assert result[1]["pts"] == 0.10
        # Other stats should still be role-based (star) + B2B boost
        # but NOT the default baseline — should be star-level
        assert result[1]["reb"] == pytest.approx(
            DEFAULT_SIGMAS["reb"] * ROLE_SIGMA_MULTIPLIERS["star"]["reb"] * B2B_SIGMA_BOOST,
            abs=0.001,
        )

    def test_ai_failure_still_returns_deterministic(self, agent):
        """If AI raises, deterministic profiles should still be returned."""
        agent._ai.complete_json.side_effect = RuntimeError("API down")
        result = agent.get_player_noise_profiles(ALL_PLAYERS)
        assert len(result) == 6

    def test_ai_skipped_when_no_edge_cases(self, agent):
        """AI should NOT be called if no edge cases exist."""
        # All players have no situational flags
        clean_players = [
            _make_player(i, f"Player{i}", "PG", 6_000, 25.0, minutes=28.0)
            for i in range(5)
        ]
        agent.get_player_noise_profiles(clean_players)
        # AI should not have been called
        agent._ai.complete_json.assert_not_called()

    def test_ai_called_when_edge_cases_exist(self, agent):
        """AI should be called when players have situational flags."""
        flagged = [
            _make_player(1, "B2B Guy", "PG", 6_000, 25.0, minutes=28.0, is_b2b=True),
        ]
        agent.get_player_noise_profiles(flagged)
        agent._ai.complete_json.assert_called_once()

    def test_ai_invalid_response_handled(self, agent):
        """Invalid AI response should be gracefully ignored."""
        agent._ai.complete_json.return_value = "not a dict"
        flagged = [
            _make_player(1, "Injured", "SG", 7_000, 30.0,
                         injury_status="GTD", minutes=28.0),
        ]
        result = agent.get_player_noise_profiles(flagged)
        assert len(result) == 1  # Deterministic still works

    def test_ai_sigma_clamped(self, agent):
        """AI-provided sigmas outside bounds should be clamped."""
        agent._ai.complete_json.return_value = {
            "adjustments": {
                "1": {"sigma_overrides": {"pts": 0.01, "reb": 0.99}}
            }
        }
        flagged = [
            _make_player(1, "Edge", "PG", 7_000, 30.0,
                         injury_status="Questionable", minutes=28.0),
        ]
        result = agent.get_player_noise_profiles(flagged)
        assert result[1]["pts"] == SIGMA_MIN  # Clamped up from 0.01
        assert result[1]["reb"] == SIGMA_MAX  # Clamped down from 0.99


# ---------------------------------------------------------------------------
# compute_nightly_calibrations tests
# ---------------------------------------------------------------------------

class TestNightlyCalibrations:
    @pytest.fixture
    def agent(self):
        ai = MagicMock()
        ai.is_available = False  # Nightly doesn't use AI
        return SimulationTuningAgent(ai)

    def test_produces_noise_sigma_keys(self, agent):
        calibrations = agent.compute_nightly_calibrations(ALL_PLAYERS)
        assert len(calibrations) == 7
        for stat in STAT_NAMES:
            key = f"noise_sigma_{stat}"
            assert key in calibrations
            # Value should be a multiplier around 1.0
            assert 0.5 <= calibrations[key] <= 2.0

    def test_star_heavy_pool_tightens_sigmas(self, agent):
        """A pool of all stars should produce multipliers < 1.0."""
        stars = [
            _make_player(i, f"Star{i}", "PG", 10_000, 50.0,
                         floor_fp=40.0, ceil_fp=60.0, minutes=34.0)
            for i in range(10)
        ]
        calibrations = agent.compute_nightly_calibrations(stars)
        for stat in STAT_NAMES:
            key = f"noise_sigma_{stat}"
            assert calibrations[key] < 1.0, (
                f"All-star pool should have multiplier < 1.0 for {stat}"
            )

    def test_punt_heavy_pool_widens_sigmas(self, agent):
        """A pool of all punt plays should produce multipliers > 1.0."""
        punts = [
            _make_player(i, f"Punt{i}", "SG", 3_500, 10.0, minutes=20.0)
            for i in range(10)
        ]
        calibrations = agent.compute_nightly_calibrations(punts)
        for stat in STAT_NAMES:
            key = f"noise_sigma_{stat}"
            assert calibrations[key] > 1.0, (
                f"All-punt pool should have multiplier > 1.0 for {stat}"
            )

    def test_empty_pool(self, agent):
        assert agent.compute_nightly_calibrations([]) == {}

    def test_mixed_pool_near_baseline(self, agent):
        """A balanced pool should produce multipliers near 1.0."""
        calibrations = agent.compute_nightly_calibrations(ALL_PLAYERS)
        for stat in STAT_NAMES:
            key = f"noise_sigma_{stat}"
            assert 0.8 <= calibrations[key] <= 1.3, (
                f"Mixed pool multiplier for {stat} should be near 1.0"
            )


# ---------------------------------------------------------------------------
# SimulationTuningProfile model tests
# ---------------------------------------------------------------------------

class TestSimulationTuningProfileModel:
    def test_default_construction(self):
        from app.models.ai import SimulationTuningProfile
        profile = SimulationTuningProfile(player_id=1)
        assert profile.role == "three_and_d"
        assert profile.sigma_overrides == {}
        assert profile.situational_flags == []

    def test_full_construction(self):
        from app.models.ai import SimulationTuningProfile
        profile = SimulationTuningProfile(
            player_id=1,
            player_name="Nikola Jokic",
            role="star",
            sigma_overrides={"pts": 0.12, "reb": 0.13},
            situational_flags=["b2b"],
        )
        assert profile.role == "star"
        assert profile.sigma_overrides["pts"] == 0.12
        assert "b2b" in profile.situational_flags


# ---------------------------------------------------------------------------
# Merge logic tests
# ---------------------------------------------------------------------------

class TestMergeAIAdjustments:
    def test_overlay_preserves_unmodified_stats(self):
        base = {1: {"pts": 0.12, "reb": 0.13, "ast": 0.20}}
        ai = {1: {"pts": 0.08}}
        merged = SimulationTuningAgent._merge_ai_adjustments(base, ai)
        assert merged[1]["pts"] == 0.08  # Overridden
        assert merged[1]["reb"] == 0.13  # Preserved
        assert merged[1]["ast"] == 0.20  # Preserved

    def test_new_player_added(self):
        base = {1: {"pts": 0.12}}
        ai = {99: {"pts": 0.20}}
        merged = SimulationTuningAgent._merge_ai_adjustments(base, ai)
        assert 99 in merged
        assert merged[99]["pts"] == 0.20
        assert merged[1]["pts"] == 0.12  # Unchanged

    def test_empty_ai_no_change(self):
        base = {1: {"pts": 0.12}}
        merged = SimulationTuningAgent._merge_ai_adjustments(base, {})
        assert merged == base
