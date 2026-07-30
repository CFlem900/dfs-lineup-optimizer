import pytest
from typing import Dict, List
from datetime import datetime

from app.models.player import PlayerMinutes, PlayerProjection, PlayerStatus
from app.models.rotation import TeamRotation, RedistributionConfig
from app.models.game import GameInfo, TeamGameStats
from app.models.simulation import SimulationConfig
from app.models.coach import CoachProfile, CoachingStyle
from app.services.rotation_engine import RotationEngine


# ============================================================================
# ENGINE FIXTURES
# ============================================================================


@pytest.fixture
def engine() -> RotationEngine:
    """Default rotation engine with standard config."""
    return RotationEngine()


@pytest.fixture
def custom_engine() -> RotationEngine:
    """Engine with custom redistribution config."""
    config = RedistributionConfig(
        primary_backup_share=0.55,
        rotation_share=0.40,
        star_boost=0.05,
        max_minutes_cap=40.0,
        min_minutes_floor=10.0,
    )
    return RotationEngine(config=config)


# ============================================================================
# PLAYER FIXTURES
# ============================================================================


@pytest.fixture
def star_player() -> PlayerMinutes:
    """Star player averaging ~35 minutes."""
    return PlayerMinutes(
        player_id=100,
        player_name="Star Player",
        position="G",
        team_id=1,
        minutes_last_5=[36.0, 34.0, 38.0, 35.0, 37.0],
        minutes_last_10=[36.0, 34.0, 38.0, 35.0, 37.0, 33.0, 36.0, 35.0, 34.0, 36.0],
        season_avg=35.4,
        usage_rate=0.30,
    )


@pytest.fixture
def starter_player() -> PlayerMinutes:
    """Starter averaging ~30 minutes."""
    return PlayerMinutes(
        player_id=101,
        player_name="Starter Player",
        position="G",
        team_id=1,
        minutes_last_5=[30.0, 32.0, 28.0, 31.0, 29.0],
        minutes_last_10=[30.0, 32.0, 28.0, 31.0, 29.0, 30.0, 31.0, 28.0, 32.0, 29.0],
        season_avg=30.0,
        usage_rate=0.22,
    )


@pytest.fixture
def bench_player() -> PlayerMinutes:
    """Bench player averaging ~18 minutes."""
    return PlayerMinutes(
        player_id=102,
        player_name="Bench Player",
        position="G",
        team_id=1,
        minutes_last_5=[18.0, 20.0, 16.0, 19.0, 17.0],
        minutes_last_10=[18.0, 20.0, 16.0, 19.0, 17.0, 18.0, 20.0, 15.0, 19.0, 18.0],
        season_avg=18.0,
        usage_rate=0.15,
    )


@pytest.fixture
def deep_bench_player() -> PlayerMinutes:
    """Deep bench player averaging ~8 minutes."""
    return PlayerMinutes(
        player_id=103,
        player_name="Deep Bench Player",
        position="G",
        team_id=1,
        minutes_last_5=[8.0, 10.0, 6.0, 9.0, 7.0],
        minutes_last_10=[8.0, 10.0, 6.0, 9.0, 7.0, 8.0, 10.0, 5.0, 9.0, 8.0],
        season_avg=8.0,
        usage_rate=0.10,
    )


# ============================================================================
# ROTATION FIXTURES (10-player team)
# ============================================================================


@pytest.fixture
def full_rotation() -> List[PlayerMinutes]:
    """Full 10-player rotation across all positions."""
    players = [
        ("Star Guard", 100, "G", 35.0),
        ("Starting Guard", 101, "G", 33.0),
        ("Starting Forward", 102, "F", 32.0),
        ("Starting Forward 2", 103, "F", 30.0),
        ("Starting Center", 104, "C", 28.0),
        ("Backup Guard", 105, "G", 22.0),
        ("Backup Forward", 106, "F", 20.0),
        ("Backup Center", 107, "C", 18.0),
        ("Reserve Guard", 108, "G", 15.0),
        ("Reserve Forward", 109, "F", 7.0),
    ]

    return [
        PlayerMinutes(
            player_id=pid,
            player_name=name,
            position=pos,
            team_id=1,
            minutes_last_5=[avg + i for i in [-2, -1, 0, 1, 2]],
            minutes_last_10=[avg + i for i in [-4, -3, -2, -1, 0, 1, 2, 3, 4, 0]],
            season_avg=avg,
            usage_rate=round(avg / 150, 3),
        )
        for name, pid, pos, avg in players
    ]


# ============================================================================
# ROTATION FIXTURES (13-player deep rotation)
# ============================================================================


@pytest.fixture
def full_rotation_13() -> List[PlayerMinutes]:
    """Full 13-player deep rotation for testing expanded rotation cap."""
    players = [
        ("Star Guard", 200, "G", 35.0),
        ("Starting Guard", 201, "G", 33.0),
        ("Starting Forward", 202, "F", 32.0),
        ("Starting Forward 2", 203, "F", 30.0),
        ("Starting Center", 204, "C", 28.0),
        ("Backup Guard", 205, "G", 22.0),
        ("Backup Forward", 206, "F", 20.0),
        ("Backup Center", 207, "C", 18.0),
        ("Reserve Guard", 208, "G", 15.0),
        ("Reserve Forward", 209, "F", 10.0),
        ("Deep Bench Guard", 210, "G", 8.0),
        ("Deep Bench Forward", 211, "F", 6.0),
        ("End of Bench", 212, "C", 3.0),
    ]

    return [
        PlayerMinutes(
            player_id=pid,
            player_name=name,
            position=pos,
            team_id=1,
            minutes_last_5=[avg + i for i in [-2, -1, 0, 1, 2]],
            minutes_last_10=[avg + i for i in [-4, -3, -2, -1, 0, 1, 2, 3, 4, 0]],
            season_avg=avg,
            usage_rate=round(avg / 150, 3),
        )
        for name, pid, pos, avg in players
    ]


# ============================================================================
# INJURY FIXTURES
# ============================================================================


@pytest.fixture
def single_injury() -> List[PlayerStatus]:
    """Single player out with injury."""
    return [
        PlayerStatus(
            player_id=100,
            player_name="Star Guard",
            status="Out",
            injury_description="Knee soreness",
        )
    ]


@pytest.fixture
def multiple_injuries() -> List[PlayerStatus]:
    """Multiple players out."""
    return [
        PlayerStatus(
            player_id=100,
            player_name="Star Guard",
            status="Out",
            injury_description="Knee soreness",
        ),
        PlayerStatus(
            player_id=103,
            player_name="Starting Forward 2",
            status="Out",
            injury_description="Ankle sprain",
        ),
    ]


@pytest.fixture
def mixed_injury_statuses() -> List[PlayerStatus]:
    """Mix of Out, Questionable, and GTD players."""
    return [
        PlayerStatus(
            player_id=100,
            player_name="Star Guard",
            status="Out",
            injury_description="Knee soreness",
        ),
        PlayerStatus(
            player_id=101,
            player_name="Starting Guard",
            status="Questionable",
            injury_description="Sore back",
        ),
        PlayerStatus(
            player_id=106,
            player_name="Backup Forward",
            status="GTD",
            injury_description="Ankle tweak",
        ),
    ]


@pytest.fixture
def no_injuries() -> List[PlayerStatus]:
    """No injuries."""
    return []


# ============================================================================
# BASELINE PROJECTION FIXTURES
# ============================================================================


@pytest.fixture
def baseline_projections_balanced() -> Dict[int, float]:
    """Balanced baseline projections totaling 240 minutes."""
    return {
        100: 35.0,
        101: 33.0,
        102: 32.0,
        103: 30.0,
        104: 28.0,
        105: 22.0,
        106: 20.0,
        107: 18.0,
        108: 15.0,
        109: 7.0,
    }


@pytest.fixture
def baseline_projections_star_heavy() -> Dict[int, float]:
    """Star-heavy projections."""
    return {
        100: 42.0,
        101: 40.0,
        102: 38.0,
        103: 35.0,
        104: 32.0,
        105: 25.0,
        106: 18.0,
        107: 10.0,
    }


# ============================================================================
# COACH FIXTURES
# ============================================================================


@pytest.fixture
def heavy_minutes_profile() -> CoachProfile:
    """Heavy-minutes coach profile (e.g. Nick Nurse, PHI)."""
    return CoachProfile(
        coach_name="Nick Nurse",
        team_id=1610612755,
        style=CoachingStyle.HEAVY_MINUTES,
        starter_multiplier=1.12,
        bench_multiplier=0.88,
        star_multiplier=1.15,
        max_minutes_override=42.0,
        min_rotation_size=8,
    )


# Keep legacy alias so old test names still resolve
thibs_profile = heavy_minutes_profile


@pytest.fixture
def deep_rotation_profile() -> CoachProfile:
    """Deep-rotation coach profile (e.g. Mitch Johnson, SAS)."""
    return CoachProfile(
        coach_name="Mitch Johnson",
        team_id=1610612759,
        style=CoachingStyle.DEEP_ROTATION,
        starter_multiplier=0.98,
        bench_multiplier=1.08,
        star_multiplier=1.05,
        max_minutes_override=38.0,
        min_rotation_size=10,
    )


# Keep legacy alias so old test names still resolve
pop_profile = deep_rotation_profile


@pytest.fixture
def default_profile() -> CoachProfile:
    """Default coach profile (balanced)."""
    return CoachProfile(
        coach_name="Default",
        team_id=0,
        style=CoachingStyle.BALANCED,
        starter_multiplier=1.0,
        bench_multiplier=1.0,
        star_multiplier=1.0,
        max_minutes_override=42.0,
        min_rotation_size=8,
    )


# ============================================================================
# VALIDATION FIXTURES
# ============================================================================


@pytest.fixture
def valid_team_rotation() -> TeamRotation:
    """Valid team rotation object."""
    projections = [
        PlayerProjection(
            player_id=i,
            player_name=f"Player {i}",
            position="G" if i % 2 == 0 else "F",
            baseline_minutes=24.0,
            adjusted_minutes=24.0,
            confidence=1.0,
            reason="Test",
        )
        for i in range(10)
    ]

    return TeamRotation(
        team_id=1,
        team_name="Test Team",
        game_date="2025-01-15",
        projections=projections,
        total_minutes=240.0,
        positions_breakdown={"G": 48.0, "F": 48.0},
    )


# ============================================================================
# CONFIG FIXTURES
# ============================================================================


@pytest.fixture
def default_config() -> RedistributionConfig:
    """Default redistribution config."""
    return RedistributionConfig()


@pytest.fixture
def aggressive_config() -> RedistributionConfig:
    """Aggressive redistribution (more to primary backup)."""
    return RedistributionConfig(
        primary_backup_share=0.75,
        rotation_share=0.20,
        star_boost=0.05,
        max_minutes_cap=44.0,
        min_minutes_floor=5.0,
    )


# ============================================================================
# SIMULATION FIXTURES
# ============================================================================


@pytest.fixture
def sim_config() -> SimulationConfig:
    """Deterministic simulation config (fewer sims for test speed)."""
    return SimulationConfig(num_simulations=1000, seed=42)


@pytest.fixture
def game_info_fixture() -> GameInfo:
    """Realistic BOS vs LAL game for simulation tests."""
    return GameInfo(
        game_id="0022500123",
        game_date="2026-02-08",
        game_status="Scheduled",
        home_team=TeamGameStats(
            team_id=1610612738,
            team_name="Boston Celtics",
            team_abbreviation="BOS",
            season_pace=100.5,
            season_off_rating=118.0,
            season_def_rating=110.0,
            season_ppg=118.5,
            season_opp_ppg=110.2,
            last_5_ppg=120.0,
        ),
        away_team=TeamGameStats(
            team_id=1610612747,
            team_name="Los Angeles Lakers",
            team_abbreviation="LAL",
            season_pace=99.8,
            season_off_rating=113.5,
            season_def_rating=112.0,
            season_ppg=113.0,
            season_opp_ppg=112.5,
            last_5_ppg=110.0,
        ),
        projected_total=225.0,
        projected_home_score=115.0,
        projected_away_score=110.0,
        projected_spread=-5.0,
        projected_pace=100.2,
        pace_label="Average",
    )


@pytest.fixture
def full_rotation_with_stats() -> List[PlayerMinutes]:
    """10-player rotation with per-minute stat rates populated."""
    # (name, pid, pos, avg_min, pts/m, reb/m, ast/m, stl/m, blk/m, tov/m, fg3m/m, usg)
    players = [
        ("Star Guard", 100, "G", 35.0, 0.71, 0.14, 0.21, 0.04, 0.03, 0.09, 0.10, 0.30),
        ("Starting Guard", 101, "G", 33.0, 0.55, 0.11, 0.18, 0.03, 0.01, 0.07, 0.08, 0.24),
        ("Starting Forward", 102, "F", 32.0, 0.60, 0.23, 0.10, 0.03, 0.05, 0.06, 0.06, 0.22),
        ("Starting Forward 2", 103, "F", 30.0, 0.50, 0.25, 0.08, 0.02, 0.06, 0.05, 0.04, 0.20),
        ("Starting Center", 104, "C", 28.0, 0.48, 0.30, 0.07, 0.02, 0.08, 0.06, 0.02, 0.18),
        ("Backup Guard", 105, "G", 22.0, 0.45, 0.10, 0.15, 0.03, 0.01, 0.06, 0.07, 0.18),
        ("Backup Forward", 106, "F", 20.0, 0.40, 0.20, 0.06, 0.02, 0.04, 0.05, 0.03, 0.15),
        ("Backup Center", 107, "C", 18.0, 0.38, 0.28, 0.05, 0.01, 0.06, 0.05, 0.01, 0.14),
        ("Reserve Guard", 108, "G", 15.0, 0.35, 0.08, 0.12, 0.02, 0.01, 0.05, 0.06, 0.12),
        ("Reserve Forward", 109, "F", 7.0, 0.30, 0.18, 0.04, 0.01, 0.03, 0.04, 0.02, 0.10),
    ]

    return [
        PlayerMinutes(
            player_id=pid,
            player_name=name,
            position=pos,
            team_id=1,
            minutes_last_5=[avg + i for i in [-2, -1, 0, 1, 2]],
            minutes_last_10=[avg + i for i in [-4, -3, -2, -1, 0, 1, 2, 3, 4, 0]],
            season_avg=avg,
            usage_rate=usg,
            pts_per_min=pts,
            reb_per_min=reb,
            ast_per_min=ast,
            stl_per_min=stl,
            blk_per_min=blk,
            tov_per_min=tov,
            fg3m_per_min=fg3,
        )
        for name, pid, pos, avg, pts, reb, ast, stl, blk, tov, fg3, usg in players
    ]


@pytest.fixture
def sim_team_rotation(full_rotation_with_stats) -> TeamRotation:
    """TeamRotation with realistic adjusted_minutes for simulation tests."""
    projections = [
        PlayerProjection(
            player_id=pm.player_id,
            player_name=pm.player_name,
            position=pm.position,
            baseline_minutes=pm.season_avg,
            adjusted_minutes=pm.season_avg,
            confidence=1.0,
            reason="Simulation test baseline",
        )
        for pm in full_rotation_with_stats
    ]

    return TeamRotation(
        team_id=1,
        team_name="Test Team",
        game_date="2026-02-08",
        projections=projections,
        total_minutes=240.0,
        positions_breakdown={"G": 105.0, "F": 89.0, "C": 46.0},
    )


# ============================================================================
# GAME CONTEXT FIXTURES
# ============================================================================


@pytest.fixture
def game_info_average() -> GameInfo:
    """Average-pace, close-game GameInfo (no blowout, no pace boost)."""
    return GameInfo(
        game_id="0022500200",
        game_date="2026-02-09",
        game_status="Scheduled",
        home_team=TeamGameStats(
            team_id=1,
            team_name="Home Team",
            team_abbreviation="HOM",
            season_pace=101.9,
            season_off_rating=114.0,
            season_def_rating=112.0,
            season_ppg=114.0,
            season_opp_ppg=112.0,
            last_5_ppg=115.0,
        ),
        away_team=TeamGameStats(
            team_id=2,
            team_name="Away Team",
            team_abbreviation="AWY",
            season_pace=101.9,
            season_off_rating=113.0,
            season_def_rating=113.0,
            season_ppg=113.0,
            season_opp_ppg=113.0,
            last_5_ppg=112.0,
        ),
        projected_total=226.0,
        projected_home_score=114.0,
        projected_away_score=112.0,
        projected_spread=-2.0,   # home favored by 2
        projected_pace=101.9,
        pace_label="Average",
    )


@pytest.fixture
def game_info_fast_pace() -> GameInfo:
    """Fast-paced game (106 pace) for testing pace factor."""
    return GameInfo(
        game_id="0022500201",
        game_date="2026-02-09",
        game_status="Scheduled",
        home_team=TeamGameStats(
            team_id=1,
            team_name="Home Team",
            team_abbreviation="HOM",
            season_pace=106.0,
            season_off_rating=118.0,
            season_def_rating=110.0,
            season_ppg=118.0,
            season_opp_ppg=110.0,
            last_5_ppg=120.0,
        ),
        away_team=TeamGameStats(
            team_id=2,
            team_name="Away Team",
            team_abbreviation="AWY",
            season_pace=106.0,
            season_off_rating=116.0,
            season_def_rating=112.0,
            season_ppg=116.0,
            season_opp_ppg=112.0,
            last_5_ppg=118.0,
        ),
        projected_total=234.0,
        projected_home_score=119.0,
        projected_away_score=115.0,
        projected_spread=-4.0,
        projected_pace=106.0,
        pace_label="Fast",
    )


@pytest.fixture
def game_info_blowout() -> GameInfo:
    """Blowout game (home favored by 10 points)."""
    return GameInfo(
        game_id="0022500202",
        game_date="2026-02-09",
        game_status="Scheduled",
        home_team=TeamGameStats(
            team_id=1,
            team_name="Home Team",
            team_abbreviation="HOM",
            season_pace=101.0,
            season_off_rating=120.0,
            season_def_rating=108.0,
            season_ppg=120.0,
            season_opp_ppg=108.0,
            last_5_ppg=122.0,
        ),
        away_team=TeamGameStats(
            team_id=2,
            team_name="Away Team",
            team_abbreviation="AWY",
            season_pace=100.0,
            season_off_rating=108.0,
            season_def_rating=118.0,
            season_ppg=108.0,
            season_opp_ppg=118.0,
            last_5_ppg=106.0,
        ),
        projected_total=222.0,
        projected_home_score=116.0,
        projected_away_score=106.0,
        projected_spread=-10.0,   # home favored by 10
        projected_pace=100.5,
        pace_label="Average",
    )


@pytest.fixture
def full_rotation_with_ages() -> List[PlayerMinutes]:
    """10-player rotation with age data for B2B fatigue testing."""
    # (name, pid, pos, avg, age)
    players = [
        ("Star Guard", 100, "G", 35.0, 28),
        ("Starting Guard", 101, "G", 33.0, 25),
        ("Starting Forward", 102, "F", 32.0, 33),   # veteran
        ("Starting Forward 2", 103, "F", 30.0, 27),
        ("Starting Center", 104, "C", 28.0, 34),     # veteran
        ("Backup Guard", 105, "G", 22.0, 23),
        ("Backup Forward", 106, "F", 20.0, 22),
        ("Backup Center", 107, "C", 18.0, 24),
        ("Reserve Guard", 108, "G", 15.0, 21),
        ("Reserve Forward", 109, "F", 7.0, 20),
    ]

    return [
        PlayerMinutes(
            player_id=pid,
            player_name=name,
            position=pos,
            team_id=1,
            minutes_last_5=[avg + i for i in [-2, -1, 0, 1, 2]],
            minutes_last_10=[avg + i for i in [-4, -3, -2, -1, 0, 1, 2, 3, 4, 0]],
            season_avg=avg,
            usage_rate=round(avg / 150, 3),
            age=age,
        )
        for name, pid, pos, avg, age in players
    ]
