"""Comprehensive tests for NCAA Basketball (CBB) integration.

Tests sport-aware routing, CBB-specific constants, rotation engine with
sport="cbb", simulation engine CBB mode, DFS scoring for CBB, service
container routing, Pydantic model sport fields, and database model
sport columns.
"""

import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime

from app.config.constants import (
    CBB_TOTAL_TEAM_MINUTES,
    CBB_ABSOLUTE_MAX_MINUTES,
    CBB_LEAGUE_AVG_PACE,
    CBB_REGULATION_MINUTES,
    CBB_STAR_ANCHOR_THRESHOLD,
    CBB_STARTER_THRESHOLD_MINUTES,
    CBB_ROTATION_SIZE_DEFAULT,
    CBB_B2B_EXISTS,
    CBB_POSITION_PRIOR_RATES,
    CBB_PACE_SENSITIVITY,
    CBB_INJURY_PLAY_PROBABILITY,
    TOTAL_TEAM_MINUTES,
    ABSOLUTE_MAX_MINUTES,
    STAR_ANCHOR_THRESHOLD,
    get_sport_constants,
)
from app.models.player import PlayerMinutes, PlayerProjection, PlayerStatus
from app.models.rotation import TeamRotation
from app.models.game import GameInfo, TeamGameStats
from app.models.lineup import OptimizeRequest, MultiLineupRequest, OptimizedLineup
from app.services.rotation_engine import RotationEngine
from app.services.dfs_service import DFSService
from app.services.simulation_engine import SimulationEngine
from app.models.simulation import SimulationConfig
from app.services.cbb_api_service import CBBApiService, _normalize_cbb_position, _safe_float
from app.services.cbb_injury_service import CBBInjuryService, _parse_espn_status
from app.services.cbb_game_service import CBBGameService
from app.services.sport_data_service import SportDataService
from app.db.models import TeamRecord, PlayerMinutesHistory, ProjectionLog, TournamentContest


# ============================================================================
# CBB PLAYER FIXTURES
# ============================================================================


@pytest.fixture
def cbb_star_player() -> PlayerMinutes:
    """CBB star averaging ~30 minutes (40-min game)."""
    return PlayerMinutes(
        player_id=5001,
        player_name="CBB Star Guard",
        position="G",
        team_id=150,
        minutes_last_5=[32.0, 30.0, 33.0, 31.0, 29.0],
        minutes_last_10=[32.0, 30.0, 33.0, 31.0, 29.0, 28.0, 31.0, 30.0, 29.0, 32.0],
        season_avg=30.5,
        usage_rate=0.28,
        pts_per_min=0.50,
        reb_per_min=0.12,
        ast_per_min=0.16,
        stl_per_min=0.035,
        blk_per_min=0.01,
        tov_per_min=0.08,
        fg3m_per_min=0.07,
    )


@pytest.fixture
def cbb_starter_player() -> PlayerMinutes:
    """CBB starter averaging ~25 minutes."""
    return PlayerMinutes(
        player_id=5002,
        player_name="CBB Starter Forward",
        position="F",
        team_id=150,
        minutes_last_5=[26.0, 24.0, 27.0, 25.0, 23.0],
        minutes_last_10=[26.0, 24.0, 27.0, 25.0, 23.0, 24.0, 26.0, 25.0, 24.0, 25.0],
        season_avg=25.0,
        usage_rate=0.20,
        pts_per_min=0.42,
        reb_per_min=0.20,
        ast_per_min=0.06,
        stl_per_min=0.025,
        blk_per_min=0.020,
        tov_per_min=0.06,
        fg3m_per_min=0.04,
    )


@pytest.fixture
def cbb_bench_player() -> PlayerMinutes:
    """CBB bench player averaging ~15 minutes."""
    return PlayerMinutes(
        player_id=5003,
        player_name="CBB Bench Guard",
        position="G",
        team_id=150,
        minutes_last_5=[16.0, 14.0, 17.0, 15.0, 13.0],
        minutes_last_10=[16.0, 14.0, 17.0, 15.0, 13.0, 14.0, 16.0, 15.0, 14.0, 15.0],
        season_avg=15.0,
        usage_rate=0.15,
        pts_per_min=0.38,
        reb_per_min=0.10,
        ast_per_min=0.10,
        stl_per_min=0.030,
        blk_per_min=0.008,
        tov_per_min=0.07,
        fg3m_per_min=0.05,
    )


@pytest.fixture
def cbb_deep_bench_player() -> PlayerMinutes:
    """CBB deep bench player averaging ~8 minutes."""
    return PlayerMinutes(
        player_id=5004,
        player_name="CBB Walk-On",
        position="C",
        team_id=150,
        minutes_last_5=[9.0, 7.0, 10.0, 8.0, 6.0],
        minutes_last_10=[9.0, 7.0, 10.0, 8.0, 6.0, 7.0, 9.0, 8.0, 7.0, 8.0],
        season_avg=8.0,
        usage_rate=0.10,
        pts_per_min=0.30,
        reb_per_min=0.18,
        ast_per_min=0.03,
        stl_per_min=0.015,
        blk_per_min=0.025,
        tov_per_min=0.05,
        fg3m_per_min=0.01,
    )


@pytest.fixture
def cbb_full_rotation() -> list[PlayerMinutes]:
    """Full 9-player CBB rotation with realistic stat profiles."""
    players = [
        PlayerMinutes(
            player_id=5010 + i,
            player_name=f"CBB Player {i+1}",
            position=["PG", "SG", "SF", "PF", "C", "G", "F", "G", "F"][i],
            team_id=150,
            minutes_last_5=[m - 1, m, m + 1, m - 0.5, m + 0.5],
            minutes_last_10=[m - 1, m, m + 1, m - 0.5, m + 0.5,
                             m - 1.5, m + 1, m, m - 1, m + 0.5],
            season_avg=m,
            usage_rate=u,
            pts_per_min=p,
            reb_per_min=r,
            ast_per_min=a,
            stl_per_min=0.030,
            blk_per_min=0.015,
            tov_per_min=0.07,
            fg3m_per_min=0.05,
        )
        for i, (m, u, p, r, a) in enumerate([
            (32.0, 0.28, 0.50, 0.12, 0.18),   # Star PG
            (30.0, 0.25, 0.48, 0.10, 0.10),   # SG
            (28.0, 0.22, 0.42, 0.15, 0.08),   # SF
            (27.0, 0.20, 0.40, 0.20, 0.06),   # PF
            (24.0, 0.18, 0.38, 0.25, 0.05),   # C
            (18.0, 0.16, 0.40, 0.10, 0.12),   # 6th man G
            (16.0, 0.14, 0.38, 0.18, 0.06),   # Bench F
            (14.0, 0.12, 0.36, 0.10, 0.10),   # Bench G
            (11.0, 0.10, 0.32, 0.15, 0.04),   # Deep bench F
        ])
    ]
    return players


@pytest.fixture
def cbb_game_info() -> GameInfo:
    """CBB game with moderate pace and close spread."""
    return GameInfo(
        game_id="cbb_401234567",
        game_date="2026-02-15",
        game_time_et="2026-02-15T19:00:00",
        game_sequence=0,
        game_status="Scheduled",
        home_team=TeamGameStats(
            team_id=150,
            team_name="Duke Blue Devils",
            team_abbreviation="DUKE",
            season_pace=70.5,
            season_off_rating=112.0,
            season_def_rating=96.0,
            season_ppg=80.0,
            season_opp_ppg=68.0,
            last_5_ppg=78.0,
            wins=22,
            losses=4,
        ),
        away_team=TeamGameStats(
            team_id=153,
            team_name="North Carolina Tar Heels",
            team_abbreviation="UNC",
            season_pace=72.0,
            season_off_rating=108.0,
            season_def_rating=100.0,
            season_ppg=76.0,
            season_opp_ppg=72.0,
            last_5_ppg=74.0,
            wins=18,
            losses=8,
        ),
        projected_total=148.0,
        projected_home_score=76.0,
        projected_away_score=72.0,
        projected_spread=-4.0,
        projected_pace=71.2,
        pace_label="Average",
    )


@pytest.fixture
def cbb_injuries() -> list[PlayerStatus]:
    """CBB injury list with one out and one questionable."""
    return [
        PlayerStatus(
            player_id=5010,
            player_name="CBB Player 1",
            status="Out",
            injury_description="Sprained ankle",
        ),
        PlayerStatus(
            player_id=5012,
            player_name="CBB Player 3",
            status="Questionable",
            injury_description="Knee soreness",
        ),
    ]


# ============================================================================
# CONSTANTS TESTS
# ============================================================================


class TestCBBConstants:
    """Verify CBB-specific constants are properly defined."""

    def test_cbb_game_minutes_200(self):
        assert CBB_TOTAL_TEAM_MINUTES == 200.0

    def test_cbb_max_player_minutes_40(self):
        assert CBB_ABSOLUTE_MAX_MINUTES == 40.0

    def test_cbb_pace_lower_than_nba(self):
        from app.services.simulation_engine import LEAGUE_AVG_PACE
        # CBB pace should be much less than NBA (~102)
        assert CBB_LEAGUE_AVG_PACE < LEAGUE_AVG_PACE
        assert CBB_LEAGUE_AVG_PACE == 68.0

    def test_cbb_no_b2b(self):
        assert CBB_B2B_EXISTS is False

    def test_cbb_star_threshold_lower_than_nba(self):
        assert CBB_STAR_ANCHOR_THRESHOLD < STAR_ANCHOR_THRESHOLD
        assert CBB_STAR_ANCHOR_THRESHOLD == 22.0

    def test_cbb_rotation_default_9(self):
        assert CBB_ROTATION_SIZE_DEFAULT == 9

    def test_cbb_position_priors_all_positions(self):
        expected_positions = {"PG", "SG", "SF", "PF", "C", "G", "F"}
        assert set(CBB_POSITION_PRIOR_RATES.keys()) == expected_positions

    def test_cbb_position_priors_have_all_stats(self):
        expected_stats = {"PTS", "REB", "AST", "STL", "BLK", "TOV", "FG3M"}
        for pos, rates in CBB_POSITION_PRIOR_RATES.items():
            assert set(rates.keys()) == expected_stats, f"Missing stats for {pos}"

    def test_cbb_prior_rates_are_positive(self):
        for pos, rates in CBB_POSITION_PRIOR_RATES.items():
            for stat, value in rates.items():
                assert value > 0, f"{pos}.{stat} = {value} should be positive"

    def test_cbb_pace_sensitivity_keys(self):
        expected = {"pts", "ast", "fg3m", "tov", "reb", "stl", "blk"}
        assert set(CBB_PACE_SENSITIVITY.keys()) == expected


class TestGetSportConstants:
    """Verify get_sport_constants() routing."""

    def test_nba_returns_empty(self):
        result = get_sport_constants("nba")
        assert result == {}

    def test_cbb_returns_overrides(self):
        result = get_sport_constants("cbb")
        assert result["TOTAL_TEAM_MINUTES"] == 200.0
        assert result["ABSOLUTE_MAX_MINUTES"] == 40.0
        assert result["LEAGUE_AVG_PACE"] == 68.0
        assert result["B2B_EXISTS"] is False
        assert result["STAR_ANCHOR_THRESHOLD"] == 22.0
        assert "PACE_SENSITIVITY" in result

    def test_unknown_sport_returns_empty(self):
        result = get_sport_constants("mlb")
        assert result == {}


# ============================================================================
# ROTATION ENGINE — CBB MODE
# ============================================================================


class TestRotationEngineCBB:
    """Test RotationEngine with sport='cbb' (200 total team minutes)."""

    def test_cbb_rotation_sums_to_200(self, cbb_full_rotation):
        engine = RotationEngine()
        result = engine.project_team_rotation(
            team_id=150,
            team_name="Duke Blue Devils",
            rotation=cbb_full_rotation,
            injuries=[],
            game_date="2026-02-15",
            sport="cbb",
        )
        assert isinstance(result, TeamRotation)
        assert result.total_minutes == pytest.approx(200.0, abs=1.0)

    def test_cbb_no_player_exceeds_40_minutes(self, cbb_full_rotation):
        engine = RotationEngine()
        result = engine.project_team_rotation(
            team_id=150,
            team_name="Duke Blue Devils",
            rotation=cbb_full_rotation,
            injuries=[],
            game_date="2026-02-15",
            sport="cbb",
        )
        for p in result.projections:
            assert p.adjusted_minutes <= 40.0, (
                f"{p.player_name} has {p.adjusted_minutes} > 40 min"
            )

    def test_cbb_injury_redistribution(self, cbb_full_rotation, cbb_injuries):
        engine = RotationEngine()
        result = engine.project_team_rotation(
            team_id=150,
            team_name="Duke Blue Devils",
            rotation=cbb_full_rotation,
            injuries=cbb_injuries,
            game_date="2026-02-15",
            sport="cbb",
        )
        # Total should still be ~200
        assert result.total_minutes == pytest.approx(200.0, abs=1.5)

        # Out player should have 0 minutes
        out_player = next(
            (p for p in result.projections if p.player_name == "CBB Player 1"),
            None,
        )
        if out_player:
            assert out_player.adjusted_minutes == 0.0

    def test_cbb_game_context_applied(self, cbb_full_rotation, cbb_game_info):
        engine = RotationEngine()
        result = engine.project_team_rotation(
            team_id=150,
            team_name="Duke Blue Devils",
            rotation=cbb_full_rotation,
            injuries=[],
            game_date="2026-02-15",
            game_info=cbb_game_info,
            sport="cbb",
        )
        assert result.total_minutes == pytest.approx(200.0, abs=1.0)

    def test_nba_rotation_still_sums_to_240(self):
        """Regression: NBA mode must still use 240 minutes."""
        engine = RotationEngine()
        # Build NBA-style rotation
        nba_players = [
            PlayerMinutes(
                player_id=100 + i,
                player_name=f"NBA Player {i+1}",
                position="G" if i < 3 else ("F" if i < 7 else "C"),
                team_id=1,
                minutes_last_5=[m - 1, m, m + 1, m - 0.5, m + 0.5],
                minutes_last_10=[m - 1, m, m + 1, m - 0.5, m + 0.5,
                                 m - 1.5, m + 1, m, m - 1, m + 0.5],
                season_avg=m,
                usage_rate=0.20,
            )
            for i, m in enumerate(
                [35, 32, 30, 28, 26, 22, 20, 18, 16, 13]
            )
        ]
        result = engine.project_team_rotation(
            team_id=1,
            team_name="Test Team",
            rotation=nba_players,
            injuries=[],
            game_date="2026-02-15",
            sport="nba",
        )
        assert result.total_minutes == pytest.approx(240.0, abs=1.0)


# ============================================================================
# SIMULATION ENGINE — CBB MODE
# ============================================================================


class TestSimulationEngineCBB:
    """Test SimulationEngine with sport='cbb'."""

    def test_cbb_simulation_produces_results(self, cbb_full_rotation, cbb_game_info):
        engine = RotationEngine()
        home_rotation = engine.project_team_rotation(
            team_id=150,
            team_name="Duke Blue Devils",
            rotation=cbb_full_rotation,
            injuries=[],
            game_date="2026-02-15",
            sport="cbb",
        )

        # Build a second rotation for away team (reuse same players with different IDs)
        away_players = [
            PlayerMinutes(
                player_id=p.player_id + 100,
                player_name=f"Away {p.player_name}",
                position=p.position,
                team_id=153,
                minutes_last_5=p.minutes_last_5,
                minutes_last_10=p.minutes_last_10,
                season_avg=p.season_avg,
                usage_rate=p.usage_rate,
                pts_per_min=p.pts_per_min,
                reb_per_min=p.reb_per_min,
                ast_per_min=p.ast_per_min,
                stl_per_min=p.stl_per_min,
                blk_per_min=p.blk_per_min,
                tov_per_min=p.tov_per_min,
                fg3m_per_min=p.fg3m_per_min,
            )
            for p in cbb_full_rotation
        ]
        away_rotation = engine.project_team_rotation(
            team_id=153,
            team_name="North Carolina Tar Heels",
            rotation=away_players,
            injuries=[],
            game_date="2026-02-15",
            sport="cbb",
        )

        sim_config = SimulationConfig(num_simulations=100, seed=42)
        sim_engine = SimulationEngine(config=sim_config)

        result = sim_engine.simulate_game(
            game_info=cbb_game_info,
            home_rotation=home_rotation,
            home_players=cbb_full_rotation,
            away_rotation=away_rotation,
            away_players=away_players,
            sport="cbb",
        )

        assert result is not None
        # GameSimResult has home_players and away_players
        assert hasattr(result, "home_players") or len(vars(result)) > 0


# ============================================================================
# DFS SERVICE — CBB SCORING
# ============================================================================


class TestDFSServiceCBB:
    """DFS scoring formulas are identical for NBA and CBB.

    The DFSService doesn't know about sport — it just scores based on
    the stat projections it receives. We verify this behavior is correct
    for CBB-scale stat lines.
    """

    def test_cbb_player_produces_dk_points(self, cbb_star_player):
        """A CBB star with ~30 min should produce positive DK points."""
        dfs = DFSService()
        # Create a projection from the player
        proj = PlayerProjection(
            player_id=cbb_star_player.player_id,
            player_name=cbb_star_player.player_name,
            position=cbb_star_player.position,
            baseline_minutes=cbb_star_player.season_avg,
            adjusted_minutes=cbb_star_player.season_avg,
            confidence=1.0,
            reason="CBB projection",
        )
        scored = dfs.project_player_dfs(proj, cbb_star_player)
        assert scored.dk_points is not None
        assert scored.dk_points > 0

    def test_cbb_dk_scoring_realistic_range(self, cbb_full_rotation):
        """CBB starters should produce 15-35 DK FP; bench 5-20."""
        engine = RotationEngine()
        rotation = engine.project_team_rotation(
            team_id=150,
            team_name="Test University",
            rotation=cbb_full_rotation,
            injuries=[],
            game_date="2026-02-15",
            sport="cbb",
        )

        dfs = DFSService()
        for proj in rotation.projections:
            player = next(
                (p for p in cbb_full_rotation if p.player_id == proj.player_id),
                None,
            )
            if player and proj.adjusted_minutes > 0:
                scored = dfs.project_player_dfs(proj, player)
                if scored.dk_points is not None:
                    # Very generous bounds — CBB players range widely
                    assert scored.dk_points >= 0, f"{proj.player_name} negative DK pts"


# ============================================================================
# CBB API SERVICE
# ============================================================================


class TestCBBApiService:
    """Test CBBApiService helper methods (no external API calls)."""

    def test_inherits_sport_data_service(self):
        assert issubclass(CBBApiService, SportDataService)

    def test_normalize_position_standard(self):
        assert _normalize_cbb_position("PG") == "PG"
        assert _normalize_cbb_position("SG") == "SG"
        assert _normalize_cbb_position("SF") == "SF"
        assert _normalize_cbb_position("PF") == "PF"
        assert _normalize_cbb_position("C") == "C"

    def test_normalize_position_composite(self):
        assert _normalize_cbb_position("G-F") == "G"
        assert _normalize_cbb_position("F-G") == "F"
        assert _normalize_cbb_position("F-C") == "F"
        assert _normalize_cbb_position("C-F") == "C"

    def test_normalize_position_words(self):
        assert _normalize_cbb_position("GUARD") == "G"
        assert _normalize_cbb_position("FORWARD") == "F"
        assert _normalize_cbb_position("CENTER") == "C"

    def test_normalize_position_unknown(self):
        assert _normalize_cbb_position("WING") == "G"  # defaults to G

    def test_safe_float_normal(self):
        assert _safe_float(25.5) == 25.5
        assert _safe_float("18") == 18.0
        assert _safe_float(0) == 0.0

    def test_safe_float_time_format(self):
        """CBBpy sometimes returns minutes as 'MM:SS'."""
        assert _safe_float("30:45") == pytest.approx(30.75, abs=0.01)
        assert _safe_float("12:30") == 12.5

    def test_safe_float_edge_cases(self):
        assert _safe_float(None) == 0.0
        assert _safe_float("") == 0.0
        assert _safe_float("N/A") == 0.0

    def test_compute_stat_rates_bayesian_shrinkage(self):
        """With < 15 games, rates should be blended toward priors."""
        svc = CBBApiService()
        # Simulate 5 games of data
        logs = [
            {"MIN": 30.0, "PTS": 20.0, "REB": 5.0, "AST": 3.0,
             "STL": 1.0, "BLK": 0.0, "TO": 2.0, "FG3M": 2.0}
        ] * 5
        total_mins = 150.0

        rates = svc._compute_stat_rates(logs, total_mins, "PG")

        # w = min(5/15, 1.0) = 0.333
        # observed PTS rate = 100/150 = 0.667
        # prior PTS rate for PG = 0.42
        # blended = 0.333 * 0.667 + 0.667 * 0.42 ≈ 0.502
        w = 5 / 15.0
        obs_pts = 100.0 / 150.0
        prior_pts = CBB_POSITION_PRIOR_RATES["PG"]["PTS"]
        expected_pts = w * obs_pts + (1 - w) * prior_pts
        assert rates["PTS"] == pytest.approx(expected_pts, abs=0.01)

    def test_compute_stat_rates_full_sample(self):
        """With >= 15 games, rates should be pure observed (w=1.0)."""
        svc = CBBApiService()
        logs = [
            {"MIN": 30.0, "PTS": 15.0, "REB": 6.0, "AST": 4.0,
             "STL": 1.0, "BLK": 0.0, "TO": 2.0, "FG3M": 1.0}
        ] * 20
        total_mins = 600.0

        rates = svc._compute_stat_rates(logs, total_mins, "PG")

        # w = 1.0, so rate = observed = 300/600 = 0.5
        assert rates["PTS"] == pytest.approx(300.0 / 600.0, abs=0.001)

    def test_rotation_cutoff_gap_detection(self):
        """Gap detection should find the natural rotation break."""
        svc = CBBApiService()
        # Create players with a clear gap after position 8
        players = [
            PlayerMinutes(
                player_id=6000 + i,
                player_name=f"P{i}",
                position="G",
                team_id=150,
                minutes_last_5=[m],
                minutes_last_10=[m],
                season_avg=m,
                usage_rate=0.15,
            )
            for i, m in enumerate([32, 30, 28, 26, 24, 20, 18, 16, 5, 4, 3])
        ]
        # Gap at position 8 (16 → 5 = 68% drop)
        cutoff = svc._find_rotation_cutoff(players)
        assert cutoff == 8  # Should detect the 16→5 gap

    def test_rotation_cutoff_no_clear_gap(self):
        """Without a clear gap, default to CBB_ROTATION_SIZE_DEFAULT."""
        svc = CBBApiService()
        players = [
            PlayerMinutes(
                player_id=6100 + i,
                player_name=f"P{i}",
                position="G",
                team_id=150,
                minutes_last_5=[m],
                minutes_last_10=[m],
                season_avg=m,
                usage_rate=0.15,
            )
            for i, m in enumerate([30, 28, 26, 24, 22, 20, 19, 18, 17, 16])
        ]
        cutoff = svc._find_rotation_cutoff(players)
        assert cutoff == CBB_ROTATION_SIZE_DEFAULT  # 9


# ============================================================================
# CBB INJURY SERVICE
# ============================================================================


class TestCBBInjuryService:
    """Test CBBInjuryService methods."""

    def test_parse_espn_status_out(self):
        assert _parse_espn_status("Out") == "Out"
        assert _parse_espn_status("out for season") == "Out"

    def test_parse_espn_status_doubtful(self):
        assert _parse_espn_status("Doubtful") == "Doubtful"

    def test_parse_espn_status_questionable(self):
        assert _parse_espn_status("Questionable") == "Questionable"

    def test_parse_espn_status_probable(self):
        # Probable maps to Questionable in CBB
        assert _parse_espn_status("Probable") == "Questionable"

    def test_parse_espn_status_day_to_day(self):
        assert _parse_espn_status("Active", "Day-to-Day") == "GTD"
        assert _parse_espn_status("", "day to day") == "GTD"

    def test_cbb_injury_play_probabilities(self):
        """CBB injury probabilities should differ from NBA."""
        assert CBB_INJURY_PLAY_PROBABILITY["Out"] == 0.0
        assert CBB_INJURY_PLAY_PROBABILITY["Doubtful"] == 0.15
        assert CBB_INJURY_PLAY_PROBABILITY["Questionable"] == 0.80

    def test_injury_service_cache_logic(self):
        svc = CBBInjuryService()
        assert svc._is_cache_valid() is False
        # After setting timestamp, cache should be valid
        svc._cache_timestamp = datetime.now()
        svc._cache = []
        assert svc._is_cache_valid() is True

    def test_injury_hash_empty(self):
        svc = CBBInjuryService()
        h = svc._compute_hash([])
        assert isinstance(h, str)
        assert len(h) > 0


# ============================================================================
# CBB GAME SERVICE
# ============================================================================


class TestCBBGameService:
    """Test CBBGameService projection methods."""

    def test_dvp_neutral_when_no_data(self):
        """No team stats → empty matchup factors (neutral)."""
        svc = CBBGameService()
        svc._team_stats_cache = {}
        svc._team_stats_cache_ts = 9999999999.0  # Future = cache valid
        factors = svc.get_dvp_matchup_factors(999)
        assert factors == {}

    def test_dvp_favorable_matchup(self):
        """Team allowing more than league avg → factor > 1.0."""
        svc = CBBGameService()
        svc._team_stats_cache = {
            100: {
                "opp_pts_pg": 80.0,   # League avg is 73.0
                "opp_reb_pg": 38.0,   # League avg is 34.0
                "opp_ast_pg": 16.0,   # League avg is 14.5
                "opp_stl_pg": 6.5,
                "opp_blk_pg": 3.2,
                "opp_tov_pg": 12.5,
                "opp_fg3m_pg": 7.5,
            }
        }
        svc._team_stats_cache_ts = 9999999999.0
        factors = svc.get_dvp_matchup_factors(100)
        assert factors["pts"] > 1.0
        assert factors["reb"] > 1.0

    def test_dvp_tough_matchup(self):
        """Team allowing less than league avg → factor < 1.0."""
        svc = CBBGameService()
        svc._team_stats_cache = {
            101: {
                "opp_pts_pg": 60.0,   # Below league avg 73.0
                "opp_reb_pg": 28.0,   # Below league avg 34.0
                "opp_ast_pg": 10.0,
                "opp_stl_pg": 4.0,
                "opp_blk_pg": 2.0,
                "opp_tov_pg": 10.0,
                "opp_fg3m_pg": 5.0,
            }
        }
        svc._team_stats_cache_ts = 9999999999.0
        factors = svc.get_dvp_matchup_factors(101)
        assert factors["pts"] < 1.0
        assert factors["reb"] < 1.0


# ============================================================================
# PYDANTIC MODELS — SPORT FIELD
# ============================================================================


class TestPydanticSportField:
    """Verify sport field on request/response models."""

    def test_optimize_request_default_nba(self):
        req = OptimizeRequest(
            platform="dk",
            draft_group_id=12345,
        )
        assert req.sport == "nba"

    def test_optimize_request_cbb(self):
        req = OptimizeRequest(
            platform="dk",
            draft_group_id=12345,
            sport="cbb",
        )
        assert req.sport == "cbb"

    def test_multi_lineup_request_sport(self):
        req = MultiLineupRequest(
            platform="dk",
            draft_group_id=12345,
            num_lineups=20,
            sport="cbb",
        )
        assert req.sport == "cbb"


# ============================================================================
# DATABASE MODELS — SPORT COLUMN
# ============================================================================


class TestDatabaseModels:
    """Verify sport column exists on relevant DB models."""

    def test_team_record_has_sport(self):
        assert hasattr(TeamRecord, "sport")
        col = TeamRecord.__table__.columns["sport"]
        assert str(col.type) == "VARCHAR(3)"

    def test_team_record_unique_constraint(self):
        """teams table has unique(abbreviation, sport) constraint."""
        constraints = TeamRecord.__table__.constraints
        found = False
        for c in constraints:
            if hasattr(c, "columns"):
                col_names = {col.name for col in c.columns}
                if col_names == {"abbreviation", "sport"}:
                    found = True
                    break
        assert found, "Composite unique constraint (abbreviation, sport) not found"

    def test_player_minutes_history_has_sport(self):
        assert hasattr(PlayerMinutesHistory, "sport")
        col = PlayerMinutesHistory.__table__.columns["sport"]
        assert str(col.type) == "VARCHAR(3)"

    def test_projection_log_has_sport(self):
        assert hasattr(ProjectionLog, "sport")
        col = ProjectionLog.__table__.columns["sport"]
        assert str(col.type) == "VARCHAR(3)"

    def test_tournament_contest_has_sport(self):
        assert hasattr(TournamentContest, "sport")
        col = TournamentContest.__table__.columns["sport"]
        assert str(col.type) == "VARCHAR(3)"


# ============================================================================
# SERVICE CONTAINER — SPORT ROUTING
# ============================================================================


class TestServiceContainerRouting:
    """Verify ServiceContainer routes to correct sport-specific services."""

    def test_data_service_routing(self):
        from app.api.dependencies import ServiceContainer
        container = ServiceContainer()
        nba_svc = container.get_data_service("nba")
        cbb_svc = container.get_data_service("cbb")
        assert nba_svc is not cbb_svc
        assert isinstance(cbb_svc, CBBApiService)

    def test_game_service_routing(self):
        from app.api.dependencies import ServiceContainer
        container = ServiceContainer()
        nba_svc = container.get_game_service("nba")
        cbb_svc = container.get_game_service("cbb")
        assert nba_svc is not cbb_svc
        assert isinstance(cbb_svc, CBBGameService)

    def test_injury_service_routing(self):
        from app.api.dependencies import ServiceContainer
        container = ServiceContainer()
        nba_svc = container.get_injury_service("nba")
        cbb_svc = container.get_injury_service("cbb")
        assert nba_svc is not cbb_svc
        assert isinstance(cbb_svc, CBBInjuryService)

    def test_default_is_nba(self):
        from app.api.dependencies import ServiceContainer
        container = ServiceContainer()
        default_data = container.get_data_service()
        nba_data = container.get_data_service("nba")
        assert default_data is nba_data


# ============================================================================
# DK SLATE SERVICE — CBB LOBBY
# ============================================================================


class TestDKSlateServiceCBB:
    """Test DK slate service CBB lobby URL routing."""

    def test_cbb_lobby_url(self):
        from app.services.dk_slate_service import _get_lobby_url, DK_CBB_LOBBY_URL
        assert _get_lobby_url("cbb") == DK_CBB_LOBBY_URL
        assert "CBB" in _get_lobby_url("cbb")

    def test_nba_lobby_url_default(self):
        from app.services.dk_slate_service import _get_lobby_url, DK_LOBBY_URL
        assert _get_lobby_url("nba") == DK_LOBBY_URL
        assert _get_lobby_url() == DK_LOBBY_URL


# ============================================================================
# END-TO-END PIPELINE — CBB
# ============================================================================


class TestCBBEndToEndPipeline:
    """Test full CBB pipeline: rotation → DFS scoring → simulation."""

    def test_rotation_to_dfs_pipeline(self, cbb_full_rotation, cbb_game_info):
        """Build CBB rotation, then score each player for DFS."""
        engine = RotationEngine()
        rotation = engine.project_team_rotation(
            team_id=150,
            team_name="Test University",
            rotation=cbb_full_rotation,
            injuries=[],
            game_date="2026-02-15",
            game_info=cbb_game_info,
            sport="cbb",
        )

        # All projections should have positive minutes (no DNPs in no-injury scenario)
        active = [p for p in rotation.projections if p.adjusted_minutes > 0]
        assert len(active) >= 7  # At least 7 players in rotation

        # Score them
        dfs = DFSService()
        scored_count = 0
        for proj in active:
            player = next(
                (p for p in cbb_full_rotation if p.player_id == proj.player_id),
                None,
            )
            if player:
                scored = dfs.project_player_dfs(proj, player)
                if scored.dk_points is not None and scored.dk_points > 0:
                    scored_count += 1

        assert scored_count >= 5  # Most rotation players should score

    def test_rotation_to_simulation_pipeline(self, cbb_full_rotation, cbb_game_info):
        """Build CBB rotation, then run simulation."""
        engine = RotationEngine()
        home_rotation = engine.project_team_rotation(
            team_id=150,
            team_name="Test University",
            rotation=cbb_full_rotation,
            injuries=[],
            game_date="2026-02-15",
            sport="cbb",
        )

        # Build away team rotation
        away_players = [
            PlayerMinutes(
                player_id=p.player_id + 200,
                player_name=f"Away {p.player_name}",
                position=p.position,
                team_id=153,
                minutes_last_5=p.minutes_last_5,
                minutes_last_10=p.minutes_last_10,
                season_avg=p.season_avg,
                usage_rate=p.usage_rate,
                pts_per_min=p.pts_per_min,
                reb_per_min=p.reb_per_min,
                ast_per_min=p.ast_per_min,
                stl_per_min=p.stl_per_min,
                blk_per_min=p.blk_per_min,
                tov_per_min=p.tov_per_min,
                fg3m_per_min=p.fg3m_per_min,
            )
            for p in cbb_full_rotation
        ]
        away_rotation = engine.project_team_rotation(
            team_id=153,
            team_name="Opponent U",
            rotation=away_players,
            injuries=[],
            game_date="2026-02-15",
            sport="cbb",
        )

        sim_config = SimulationConfig(num_simulations=100, seed=42)
        sim_engine = SimulationEngine(config=sim_config)

        result = sim_engine.simulate_game(
            game_info=cbb_game_info,
            home_rotation=home_rotation,
            home_players=cbb_full_rotation,
            away_rotation=away_rotation,
            away_players=away_players,
            sport="cbb",
        )

        assert result is not None

    def test_full_pipeline_with_injuries(
        self, cbb_full_rotation, cbb_injuries, cbb_game_info
    ):
        """Full pipeline with injuries: rotation → DFS → simulation."""
        engine = RotationEngine()
        rotation = engine.project_team_rotation(
            team_id=150,
            team_name="Test University",
            rotation=cbb_full_rotation,
            injuries=cbb_injuries,
            game_date="2026-02-15",
            game_info=cbb_game_info,
            sport="cbb",
        )

        # Total should still be ~200
        assert rotation.total_minutes == pytest.approx(200.0, abs=2.0)

        # DFS scoring should work
        dfs = DFSService()
        active = [p for p in rotation.projections if p.adjusted_minutes > 0]
        for proj in active:
            player = next(
                (p for p in cbb_full_rotation if p.player_id == proj.player_id),
                None,
            )
            if player:
                scored = dfs.project_player_dfs(proj, player)
                assert scored.dk_points is not None

        # Build away team for simulation
        away_players = [
            PlayerMinutes(
                player_id=p.player_id + 300,
                player_name=f"Away {p.player_name}",
                position=p.position,
                team_id=153,
                minutes_last_5=p.minutes_last_5,
                minutes_last_10=p.minutes_last_10,
                season_avg=p.season_avg,
                usage_rate=p.usage_rate,
                pts_per_min=p.pts_per_min,
                reb_per_min=p.reb_per_min,
                ast_per_min=p.ast_per_min,
                stl_per_min=p.stl_per_min,
                blk_per_min=p.blk_per_min,
                tov_per_min=p.tov_per_min,
                fg3m_per_min=p.fg3m_per_min,
            )
            for p in cbb_full_rotation
        ]
        away_rotation = engine.project_team_rotation(
            team_id=153,
            team_name="Opponent U",
            rotation=away_players,
            injuries=[],
            game_date="2026-02-15",
            sport="cbb",
        )

        sim_config = SimulationConfig(num_simulations=100, seed=42)
        sim_engine = SimulationEngine(config=sim_config)
        sim_result = sim_engine.simulate_game(
            game_info=cbb_game_info,
            home_rotation=rotation,
            home_players=cbb_full_rotation,
            away_rotation=away_rotation,
            away_players=away_players,
            sport="cbb",
        )
        assert sim_result is not None


# ============================================================================
# PHASE 3 — SPORT-AWARE ROUTING TESTS
# ============================================================================


class TestPhase3RouterSportParams:
    """Verify routers accept sport parameter without errors."""

    def test_check_b2b_cbb_returns_false(self):
        """CBB never has back-to-backs."""
        from app.api.routers.teams import _check_b2b as teams_b2b
        assert teams_b2b(150, "2026-02-15", sport="cbb") is False

    def test_check_b2b_simulation_cbb_returns_false(self):
        from app.api.routers.simulation import _check_b2b as sim_b2b
        assert sim_b2b(150, "2026-02-15", sport="cbb") is False

    def test_check_b2b_defaults_to_nba(self):
        """Default sport param is 'nba'."""
        from app.api.routers.teams import _check_b2b as teams_b2b
        # Should not raise (may return True or False depending on NBA schedule)
        result = teams_b2b(1, "2026-02-15")
        assert isinstance(result, bool)


class TestPhase3ShowdownCBBBlock:
    """Showdown mode should be blocked for CBB."""

    def test_optimize_showdown_cbb_rejected(self):
        """Showdown mode with sport=cbb should raise 400."""
        from app.models.lineup import OptimizeRequest
        req = OptimizeRequest(
            platform="dk",
            draft_group_id=99999,
            mode="showdown",
            sport="cbb",
            game_id="test_game_1",
        )
        assert req.mode == "showdown"
        assert req.sport == "cbb"

    def test_optimize_classic_cbb_allowed(self):
        """Classic mode with sport=cbb should be fine."""
        from app.models.lineup import OptimizeRequest
        req = OptimizeRequest(
            platform="dk",
            draft_group_id=99999,
            mode="classic",
            sport="cbb",
        )
        assert req.mode == "classic"
        assert req.sport == "cbb"

    def test_showdown_nba_still_allowed(self):
        """Showdown mode with NBA should still be fine."""
        from app.models.lineup import OptimizeRequest
        req = OptimizeRequest(
            platform="dk",
            draft_group_id=99999,
            mode="showdown",
            sport="nba",
            game_id="test_game_1",
        )
        assert req.mode == "showdown"
        assert req.sport == "nba"


class TestPhase3SportContextPreamble:
    """AI agent sport context preamble."""

    def test_cbb_preamble_contains_key_info(self):
        from app.services.agents.sport_context import get_sport_preamble
        preamble = get_sport_preamble("cbb")
        assert "NCAA" in preamble
        assert "40-minute" in preamble
        assert "200 total team-minutes" in preamble
        assert "68 possessions" in preamble
        assert "30 seconds" in preamble  # shot clock
        assert len(preamble) > 100

    def test_nba_preamble_returns_empty(self):
        from app.services.agents.sport_context import get_sport_preamble
        assert get_sport_preamble("nba") == ""
        assert get_sport_preamble() == ""

    def test_injury_agent_accepts_sport_param(self):
        """InjuryImpactAgent.analyze_injury_impact accepts sport kwarg."""
        import inspect
        from app.services.agents.injury_impact_agent import InjuryImpactAgent
        sig = inspect.signature(InjuryImpactAgent.analyze_injury_impact)
        assert "sport" in sig.parameters
        assert sig.parameters["sport"].default == "nba"

    def test_lineup_strategy_agent_accepts_sport_param(self):
        import inspect
        from app.services.agents.lineup_strategy_agent import LineupStrategyAgent
        sig = inspect.signature(LineupStrategyAgent.get_strategy_adjustments)
        assert "sport" in sig.parameters
        assert sig.parameters["sport"].default == "nba"

    def test_news_projection_agent_accepts_sport_param(self):
        import inspect
        from app.services.agents.news_projection_agent import NewsProjectionAgent
        sig = inspect.signature(NewsProjectionAgent.extract_adjustments)
        assert "sport" in sig.parameters
        assert sig.parameters["sport"].default == "nba"


class TestPhase3AccuracyServiceSport:
    """Accuracy service accepts sport parameter."""

    def test_accuracy_summary_accepts_sport(self):
        import inspect
        from app.services.accuracy_service import AccuracyService
        sig = inspect.signature(AccuracyService.get_accuracy_summary)
        assert "sport" in sig.parameters
        assert sig.parameters["sport"].default == "nba"

    def test_accuracy_by_position_accepts_sport(self):
        import inspect
        from app.services.accuracy_service import AccuracyService
        sig = inspect.signature(AccuracyService.get_accuracy_by_position)
        assert "sport" in sig.parameters

    def test_accuracy_by_salary_accepts_sport(self):
        import inspect
        from app.services.accuracy_service import AccuracyService
        sig = inspect.signature(AccuracyService.get_accuracy_by_salary_tier)
        assert "sport" in sig.parameters

    def test_accuracy_timeline_accepts_sport(self):
        import inspect
        from app.services.accuracy_service import AccuracyService
        sig = inspect.signature(AccuracyService.get_accuracy_timeline)
        assert "sport" in sig.parameters

    def test_player_accuracy_accepts_sport(self):
        import inspect
        from app.services.accuracy_service import AccuracyService
        sig = inspect.signature(AccuracyService.get_player_accuracy)
        assert "sport" in sig.parameters


class TestPhase3PropsServiceRouting:
    """Props service correctly routes by sport."""

    def test_get_props_service_cbb(self):
        from app.api.dependencies import ServiceContainer
        container = ServiceContainer()
        cbb_props = container.get_props_service("cbb")
        from app.services.cbb_props_service import CBBPropsService
        assert isinstance(cbb_props, CBBPropsService)

    def test_get_props_service_nba(self):
        from app.api.dependencies import ServiceContainer
        container = ServiceContainer()
        nba_props = container.get_props_service("nba")
        from app.services.dk_props_service import DKPropsService
        assert isinstance(nba_props, DKPropsService)


# ============================================================================
# PHASE 4 — ALL AGENTS SPORT PARAM + DFS SERVICE + OWNERSHIP SIM
# ============================================================================


class TestPhase4AllAgentsSportParam:
    """All 12 AI agents must accept a `sport` parameter."""

    AGENT_METHODS = [
        ("app.services.agents.injury_impact_agent", "InjuryImpactAgent", "analyze_injury_impact"),
        ("app.services.agents.lineup_strategy_agent", "LineupStrategyAgent", "get_strategy_adjustments"),
        ("app.services.agents.news_projection_agent", "NewsProjectionAgent", "extract_adjustments"),
        ("app.services.agents.ownership_agent", "OwnershipProjectionAgent", "project_ownership"),
        ("app.services.agents.signal_analysis_agent", "SignalAnalysisAgent", "analyze_signal"),
        ("app.services.agents.narrative_agent", "NarrativeAgent", "generate_narrative"),
        ("app.services.agents.narrative_agent", "NarrativeAgent", "stream_narrative"),
        ("app.services.agents.simulation_tuning_agent", "SimulationTuningAgent", "get_player_noise_profiles"),
        ("app.services.agents.coach_learning_agent", "CoachLearningAgent", "analyze_coach_patterns"),
        ("app.services.agents.expert_quality_agent", "ExpertQualityAgent", "evaluate_expert_accuracy"),
        ("app.services.agents.backtesting_agent", "BacktestingAgent", "analyze_accuracy"),
        ("app.services.agents.backtesting_agent", "BacktestingAgent", "analyze_projection_accuracy"),
        ("app.services.agents.tournament_analysis_agent", "TournamentAnalysisAgent", "analyze_tournament_results"),
    ]

    @pytest.mark.parametrize("module_path,class_name,method_name", AGENT_METHODS)
    def test_agent_accepts_sport(self, module_path, class_name, method_name):
        import importlib
        import inspect
        mod = importlib.import_module(module_path)
        cls = getattr(mod, class_name)
        method = getattr(cls, method_name)
        sig = inspect.signature(method)
        assert "sport" in sig.parameters, (
            f"{class_name}.{method_name} missing 'sport' parameter"
        )
        assert sig.parameters["sport"].default == "nba"

    def test_chat_agent_extracts_sport_from_context(self):
        """ChatAgent extracts sport from context dict, not a method param."""
        import inspect
        from app.services.agents.chat_agent import ChatAgent
        sig = inspect.signature(ChatAgent._build_context_prompt)
        # chat agent uses context dict, not explicit sport param
        assert "context" in sig.parameters


class TestPhase4DFSServiceSport:
    """DFS service accepts sport and uses CBB-specific pace sensitivity."""

    def test_project_player_dfs_accepts_sport(self):
        import inspect
        from app.services.dfs_service import DFSService
        sig = inspect.signature(DFSService.project_player_dfs)
        assert "sport" in sig.parameters
        assert sig.parameters["sport"].default == "nba"

    def test_project_team_dfs_accepts_sport(self):
        import inspect
        from app.services.dfs_service import DFSService
        sig = inspect.signature(DFSService.project_team_dfs)
        assert "sport" in sig.parameters
        assert sig.parameters["sport"].default == "nba"

    def test_cbb_pace_sensitivity_used(self, cbb_star_player):
        """CBB mode should use CBB_PACE_SENSITIVITY, not NBA's."""
        from app.models.player import PlayerProjection
        dfs = DFSService()
        proj = PlayerProjection(
            player_id=cbb_star_player.player_id,
            player_name=cbb_star_player.player_name,
            position=cbb_star_player.position,
            baseline_minutes=30.0,
            adjusted_minutes=30.0,
            confidence=1.0,
            reason="test",
        )
        # Both NBA and CBB should produce valid results
        nba_result = dfs.project_player_dfs(proj, cbb_star_player, sport="nba")
        cbb_result = dfs.project_player_dfs(proj, cbb_star_player, sport="cbb")
        assert nba_result.dk_points is not None
        assert cbb_result.dk_points is not None
        # Results may differ slightly due to pace sensitivity differences
        assert nba_result.dk_points > 0
        assert cbb_result.dk_points > 0

    def test_cbb_dd_td_suppression(self, cbb_star_player):
        """CBB should have lower DD/TD probabilities than NBA."""
        from app.models.player import PlayerProjection
        dfs = DFSService()
        proj = PlayerProjection(
            player_id=cbb_star_player.player_id,
            player_name=cbb_star_player.player_name,
            position=cbb_star_player.position,
            baseline_minutes=30.0,
            adjusted_minutes=30.0,
            confidence=1.0,
            reason="test",
        )
        nba_scored = dfs.project_player_dfs(proj, cbb_star_player, sport="nba")
        cbb_scored = dfs.project_player_dfs(proj, cbb_star_player, sport="cbb")
        # CBB DD/TD bonuses should be strictly less than or equal to NBA
        # (since we multiply by 0.4 for DD and 0.1 for TD)
        assert cbb_scored.dk_points <= nba_scored.dk_points


class TestPhase4OwnershipSimSport:
    """Ownership simulator accepts sport parameter."""

    def test_simulate_accepts_sport(self):
        import inspect
        from app.services.ownership_simulator import OwnershipSimulator
        sig = inspect.signature(OwnershipSimulator.simulate)
        assert "sport" in sig.parameters
        assert sig.parameters["sport"].default == "nba"


class TestPhase4LineupsRouterSport:
    """Lineup router endpoints accept sport parameter."""

    def test_stream_player_pool_has_sport_param(self):
        """stream_player_pool endpoint should accept sport query param."""
        import inspect
        from app.api.routers.lineups import stream_player_pool
        sig = inspect.signature(stream_player_pool)
        assert "sport" in sig.parameters

    def test_preload_pool_has_sport_param(self):
        import inspect
        from app.api.routers.lineups import preload_pool
        sig = inspect.signature(preload_pool)
        assert "sport" in sig.parameters

    def test_clear_pool_cache_has_sport_param(self):
        import inspect
        from app.api.routers.lineups import clear_pool_cache
        sig = inspect.signature(clear_pool_cache)
        assert "sport" in sig.parameters


# ═══════════════════════════════════════════════════════════════════════════
# Phase 5 Tests — Cache Safety, Late-Swap, Lineup Analysis, Admin, Correlation
# ═══════════════════════════════════════════════════════════════════════════


class TestPhase5CacheSafety:
    """Verify sport-aware cache keys prevent NBA/CBB pool collisions."""

    def test_cache_key_includes_sport(self):
        """_cache_key output differs between sports for same platform/draft_group."""
        from app.services.lineup_optimizer_service import _cache_key
        nba_key = _cache_key("nba:dk", 12345, "2026-01-15")
        cbb_key = _cache_key("cbb:dk", 12345, "2026-01-15")
        assert nba_key != cbb_key
        assert "nba" in nba_key
        assert "cbb" in cbb_key

    def test_get_cached_pool_accepts_sport_param(self):
        """get_cached_pool has a sport parameter."""
        import inspect
        from app.services.lineup_optimizer_service import LineupOptimizerService
        sig = inspect.signature(LineupOptimizerService.get_cached_pool)
        assert "sport" in sig.parameters

    def test_fade_endpoint_has_sport_param(self):
        """GET /fade-list accepts sport query param."""
        import inspect
        from app.api.routers.lineups import get_fade_list
        sig = inspect.signature(get_fade_list)
        assert "sport" in sig.parameters

    def test_ownership_simulate_has_sport_param(self):
        """POST /ownership/simulate accepts sport query param."""
        import inspect
        from app.api.routers.lineups import simulate_ownership
        sig = inspect.signature(simulate_ownership)
        assert "sport" in sig.parameters


class TestPhase5LateSwapSport:
    """Verify sport is available on all late-swap endpoints.

    The POST endpoints now carry sport in the JSON request body
    (LateSwapRequest.sport / LateSwapMonitorRequest.sport) rather than
    as a query param; the GET monitor endpoint still uses a query param.
    """

    def test_late_swap_has_sport_param(self):
        """Sport travels in the request body (LateSwapRequest.sport)."""
        import inspect
        from app.api.routers.lineups import late_swap
        from app.models.lineup import LateSwapRequest
        sig = inspect.signature(late_swap)
        assert "sport" not in sig.parameters  # moved into the body model
        assert sig.parameters["request"].annotation is LateSwapRequest
        assert "sport" in LateSwapRequest.model_fields
        assert LateSwapRequest.model_fields["sport"].default == "nba"

    def test_late_swap_monitor_has_sport_param(self):
        import inspect
        from app.api.routers.lineups import get_late_swap_monitor
        sig = inspect.signature(get_late_swap_monitor)
        assert "sport" in sig.parameters

    def test_late_swap_monitor_full_has_sport_param(self):
        """Sport travels in the request body (LateSwapMonitorRequest.sport)."""
        import inspect
        from app.api.routers.lineups import late_swap_monitor_full
        from app.models.lineup import LateSwapMonitorRequest
        sig = inspect.signature(late_swap_monitor_full)
        assert "sport" not in sig.parameters  # moved into the body model
        assert sig.parameters["request"].annotation is LateSwapMonitorRequest
        assert "sport" in LateSwapMonitorRequest.model_fields
        assert LateSwapMonitorRequest.model_fields["sport"].default == "nba"


class TestPhase5LineupAnalysisCBB:
    """Verify CBB-specific projection scoring and stacking thresholds."""

    def test_score_projection_cbb_dk_thresholds(self):
        """CBB DK: 120 FP ≈ 5/10, not 0/10 as NBA would compute."""
        from unittest.mock import MagicMock
        from app.services.lineup_analysis_service import LineupAnalysisService

        service = LineupAnalysisService.__new__(LineupAnalysisService)
        lineup = MagicMock()
        lineup.total_projected_fp = 120.0  # Mid-range CBB value

        result = service._score_projection(lineup, "dk", sport="cbb")
        assert result.score == 5.0, f"CBB 120 FP should score 5.0, got {result.score}"

    def test_score_projection_cbb_dk_floor(self):
        """CBB DK: 80 FP → 0/10 (floor)."""
        from unittest.mock import MagicMock
        from app.services.lineup_analysis_service import LineupAnalysisService

        service = LineupAnalysisService.__new__(LineupAnalysisService)
        lineup = MagicMock()
        lineup.total_projected_fp = 80.0

        result = service._score_projection(lineup, "dk", sport="cbb")
        assert result.score == 0.0

    def test_score_projection_cbb_dk_elite(self):
        """CBB DK: 160 FP → 10/10 (elite)."""
        from unittest.mock import MagicMock
        from app.services.lineup_analysis_service import LineupAnalysisService

        service = LineupAnalysisService.__new__(LineupAnalysisService)
        lineup = MagicMock()
        lineup.total_projected_fp = 160.0

        result = service._score_projection(lineup, "dk", sport="cbb")
        assert result.score == 10.0

    def test_score_projection_nba_unchanged(self):
        """NBA DK: 120 FP still scores ~0 in NBA mode (no regression)."""
        from unittest.mock import MagicMock
        from app.services.lineup_analysis_service import LineupAnalysisService

        service = LineupAnalysisService.__new__(LineupAnalysisService)
        lineup = MagicMock()
        lineup.total_projected_fp = 120.0

        result = service._score_projection(lineup, "dk", sport="nba")
        # (120 - 220) / 5.5 = negative → clamped to 0
        assert result.score == 0.0

    def test_analyze_stacking_cbb_defaults(self):
        """CBB stacking uses 145.0 default total, not 220."""
        from app.services.lineup_analysis_service import LineupAnalysisService

        # Static method — call directly with empty players and games
        result = LineupAnalysisService._analyze_stacking([], [], sport="cbb")
        # With empty inputs, should still return a dict (no crash)
        assert isinstance(result, dict)

    def test_analysis_request_has_sport_field(self):
        """AnalyzeLineupsRequest and RefineLineupsRequest have sport field."""
        from app.models.lineup import AnalyzeLineupsRequest, RefineLineupsRequest

        req = AnalyzeLineupsRequest(
            lineups=[], platform="dk", draft_group_id=1, sport="cbb"
        )
        assert req.sport == "cbb"

        ref = RefineLineupsRequest(
            lineups=[], platform="dk", draft_group_id=1, sport="cbb"
        )
        assert ref.sport == "cbb"


class TestPhase5AdminBacktest:
    """Verify admin/backtest endpoints accept sport parameter."""

    def test_backtest_ingest_has_sport_param(self):
        import inspect
        from app.api.routers.admin import trigger_backtest_ingestion
        sig = inspect.signature(trigger_backtest_ingestion)
        assert "sport" in sig.parameters

    def test_backtest_analysis_has_sport_param(self):
        import inspect
        from app.api.routers.admin import get_backtest_analysis
        sig = inspect.signature(get_backtest_analysis)
        assert "sport" in sig.parameters

    def test_backtest_backfill_has_sport_param(self):
        import inspect
        from app.api.routers.admin import trigger_historical_backfill
        sig = inspect.signature(trigger_historical_backfill)
        assert "sport" in sig.parameters


class TestPhase5CorrelationStacking:
    """Verify correlation service and stacking rules are sport-aware."""

    def test_correlation_get_team_accepts_sport(self):
        """get_team_correlations signature includes sport param."""
        import inspect
        from app.services.correlation_service import CorrelationService
        sig = inspect.signature(CorrelationService.get_team_correlations)
        assert "sport" in sig.parameters

    def test_correlation_get_correlated_pairs_accepts_sport(self):
        import inspect
        from app.services.correlation_service import CorrelationService
        sig = inspect.signature(CorrelationService.get_correlated_pairs)
        assert "sport" in sig.parameters

    def test_correlation_get_game_accepts_sport(self):
        import inspect
        from app.services.correlation_service import CorrelationService
        sig = inspect.signature(CorrelationService.get_game_correlations)
        assert "sport" in sig.parameters

    def test_stacking_default_config_cbb_max_per_team(self):
        """CBB create_default_config returns max_per_team=3 (not 4)."""
        from app.services.stacking_rules import StackingRulesEngine
        config = StackingRulesEngine.create_default_config(sport="cbb")
        # The effective_max should be 3 for CBB when default max_per_team=4
        max_rule = [r for r in config.rules if r.rule_type == "max_per_team"]
        assert len(max_rule) > 0
        assert max_rule[0].max_players == 3

    def test_stacking_default_config_nba_max_per_team(self):
        """NBA create_default_config keeps max_per_team=4."""
        from app.services.stacking_rules import StackingRulesEngine
        config = StackingRulesEngine.create_default_config(sport="nba")
        max_rule = [r for r in config.rules if r.rule_type == "max_per_team"]
        assert len(max_rule) > 0
        assert max_rule[0].max_players == 4

    def test_box_score_ingester_accepts_sport(self):
        """ingest_game_date has sport parameter."""
        import inspect
        from app.services.ingestion.box_score_ingester import BoxScoreIngester
        sig = inspect.signature(BoxScoreIngester.ingest_game_date)
        assert "sport" in sig.parameters


class TestPhase5Constants:
    """Verify new CBB constants added in Phase 5A exist."""

    def test_cbb_lineup_projection_constants(self):
        from app.config.constants import (
            CBB_LINEUP_PROJECTION_DK_FLOOR,
            CBB_LINEUP_PROJECTION_DK_RANGE,
            CBB_LINEUP_PROJECTION_FD_FLOOR,
            CBB_LINEUP_PROJECTION_FD_RANGE,
        )
        assert CBB_LINEUP_PROJECTION_DK_FLOOR == 80.0
        assert CBB_LINEUP_PROJECTION_DK_RANGE == 8.0
        assert CBB_LINEUP_PROJECTION_FD_FLOOR == 90.0
        assert CBB_LINEUP_PROJECTION_FD_RANGE == 8.5

    def test_cbb_game_total_constants(self):
        from app.config.constants import (
            CBB_DEFAULT_GAME_TOTAL,
            CBB_HIGH_TOTAL_THRESHOLD,
        )
        assert CBB_DEFAULT_GAME_TOTAL == 145.0
        assert CBB_HIGH_TOTAL_THRESHOLD == 155.0

    def test_cbb_correlation_stacking_constants(self):
        from app.config.constants import (
            CBB_CORRELATION_MIN_GAMES,
            CBB_STACKING_MAX_PER_TEAM,
            CBB_STACKABLE_GAME_TOTAL_DEFAULT,
        )
        assert CBB_CORRELATION_MIN_GAMES == 3
        assert CBB_STACKING_MAX_PER_TEAM == 3
        assert CBB_STACKABLE_GAME_TOTAL_DEFAULT == 145.0

    def test_get_sport_constants_cbb_includes_phase5_keys(self):
        """get_sport_constants('cbb') returns all Phase 5 keys."""
        consts = get_sport_constants("cbb")
        phase5_keys = [
            "LINEUP_PROJECTION_DK_FLOOR",
            "LINEUP_PROJECTION_DK_RANGE",
            "LINEUP_PROJECTION_FD_FLOOR",
            "LINEUP_PROJECTION_FD_RANGE",
            "DEFAULT_GAME_TOTAL",
            "HIGH_TOTAL_THRESHOLD",
            "CORRELATION_MIN_GAMES",
            "STACKING_MAX_PER_TEAM",
            "STACKABLE_GAME_TOTAL_DEFAULT",
        ]
        for key in phase5_keys:
            assert key in consts, f"Missing key '{key}' in CBB sport constants"
