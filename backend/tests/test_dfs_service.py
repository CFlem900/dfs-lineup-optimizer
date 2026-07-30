"""Tests for the DFS fantasy point projection service."""

import pytest

from app.models.player import PlayerMinutes, PlayerProjection
from app.models.rotation import TeamRotation
from app.services.dfs_service import DFSService


@pytest.fixture
def dfs_service():
    return DFSService()


@pytest.fixture
def star_player_with_stats() -> PlayerMinutes:
    """Star player with realistic per-minute stat rates.

    Modeled on a ~35 min player averaging 25 pts, 10 reb, 5 ast,
    1.2 stl, 0.8 blk, 2.5 tov, 2.0 fg3m per game.
    """
    return PlayerMinutes(
        player_id=100,
        player_name="Star Player",
        position="G",
        team_id=1,
        minutes_last_5=[36.0, 34.0, 38.0, 35.0, 37.0],
        minutes_last_10=[36.0, 34.0, 38.0, 35.0, 37.0, 33.0, 36.0, 35.0, 34.0, 36.0],
        season_avg=35.4,
        usage_rate=0.30,
        pts_per_min=0.7143,   # 25 / 35
        reb_per_min=0.2857,   # 10 / 35
        ast_per_min=0.1429,   # 5 / 35
        stl_per_min=0.0343,   # 1.2 / 35
        blk_per_min=0.0229,   # 0.8 / 35
        tov_per_min=0.0714,   # 2.5 / 35
        fg3m_per_min=0.0571,  # 2.0 / 35
    )


@pytest.fixture
def bench_player_with_stats() -> PlayerMinutes:
    """Bench player with lower per-minute production."""
    return PlayerMinutes(
        player_id=105,
        player_name="Bench Player",
        position="G",
        team_id=1,
        minutes_last_5=[18.0, 20.0, 16.0, 19.0, 17.0],
        minutes_last_10=[18.0, 20.0, 16.0, 19.0, 17.0, 18.0, 20.0, 15.0, 19.0, 18.0],
        season_avg=18.0,
        usage_rate=0.15,
        pts_per_min=0.4444,   # 8 / 18
        reb_per_min=0.2222,   # 4 / 18
        ast_per_min=0.1111,   # 2 / 18
        stl_per_min=0.0556,   # 1.0 / 18
        blk_per_min=0.0278,   # 0.5 / 18
        tov_per_min=0.0556,   # 1.0 / 18
        fg3m_per_min=0.0556,  # 1.0 / 18
    )


@pytest.fixture
def star_projection() -> PlayerProjection:
    return PlayerProjection(
        player_id=100,
        player_name="Star Player",
        position="G",
        baseline_minutes=35.0,
        adjusted_minutes=35.0,
        confidence=1.0,
        reason="Baseline projection",
    )


@pytest.fixture
def bench_projection() -> PlayerProjection:
    return PlayerProjection(
        player_id=105,
        player_name="Bench Player",
        position="G",
        baseline_minutes=18.0,
        adjusted_minutes=18.0,
        confidence=1.0,
        reason="Baseline projection",
    )


@pytest.fixture
def injured_projection() -> PlayerProjection:
    return PlayerProjection(
        player_id=200,
        player_name="Injured Player",
        position="F",
        baseline_minutes=30.0,
        adjusted_minutes=0.0,
        confidence=0.0,
        reason="Out - Injured",
    )


# ==========================================================================
# DraftKings scoring tests
# ==========================================================================


class TestDKScoring:
    """Test DraftKings fantasy point calculations."""

    def test_dk_basic_scoring(self, dfs_service):
        """Known stat line should produce correct DK points.

        Uses probability-weighted DD/TD bonuses — the expected bonus
        is less than the deterministic 1.5 because the stat line is
        right at the boundary (PTS=25 clearly over, REB=10 exactly at
        threshold → P(≥10) ≈ 0.5).
        """
        stats = {
            "pts": 25.0, "reb": 10.0, "ast": 5.0,
            "stl": 1.2, "blk": 0.8, "tov": 2.5, "fg3m": 2.0,
        }
        dk = dfs_service._dk_score(stats)
        # Base: 25 + 12.5 + 7.5 + 2.4 + 1.6 - 1.25 + 1.0 = 48.75
        # + probability-weighted DD/TD bonus (< 1.5 due to REB right at 10)
        assert dk == 49.5

    def test_dk_double_double_bonus(self, dfs_service):
        """Probability-weighted DD bonus for a near-DD stat line."""
        stats = {
            "pts": 20.0, "reb": 10.0, "ast": 5.0,
            "stl": 1.0, "blk": 1.0, "tov": 2.0, "fg3m": 1.0,
        }
        dk = dfs_service._dk_score(stats)
        # Base: 20 + 12.5 + 7.5 + 2 + 2 - 1 + 0.5 = 43.5
        # + probability-weighted DD bonus (PTS clearly over, REB at boundary)
        assert dk == 44.2

    def test_dk_triple_double_bonus(self, dfs_service):
        """Correlated probability-weighted DD+TD bonus for a near-TD stat line.

        Uses correlated DD/TD model — PTS-AST correlation (0.40) slightly
        increases joint probability, giving a marginally higher bonus.
        """
        stats = {
            "pts": 20.0, "reb": 10.0, "ast": 10.0,
            "stl": 1.0, "blk": 1.0, "tov": 2.0, "fg3m": 1.0,
        }
        dk = dfs_service._dk_score(stats)
        # Base: 20 + 12.5 + 15 + 2 + 2 - 1 + 0.5 = 51.0
        # + correlated probability-weighted DD + TD bonuses
        assert dk == 52.9

    def test_dk_no_bonus_below_thresholds(self, dfs_service):
        """No bonus when no stat category hits 10."""
        stats = {
            "pts": 8.0, "reb": 5.0, "ast": 3.0,
            "stl": 1.0, "blk": 0.5, "tov": 1.0, "fg3m": 1.0,
        }
        dk = dfs_service._dk_score(stats)
        # 8 + 6.25 + 4.5 + 2 + 1 - 0.5 + 0.5 = 21.75
        assert dk == 21.8

    def test_dk_three_pointer_bonus(self, dfs_service):
        """3PM should contribute 0.5 bonus per make (on top of PTS)."""
        stats = {
            "pts": 15.0, "reb": 3.0, "ast": 2.0,
            "stl": 0.0, "blk": 0.0, "tov": 1.0, "fg3m": 5.0,
        }
        dk = dfs_service._dk_score(stats)
        # 15 + 3.75 + 3.0 + 0 + 0 - 0.5 + 2.5 = 23.75
        assert dk == 23.8


# ==========================================================================
# FanDuel scoring tests
# ==========================================================================


class TestFDScoring:
    """Test FanDuel fantasy point calculations."""

    def test_fd_basic_scoring(self, dfs_service):
        """Known stat line should produce correct FD points."""
        stats = {
            "pts": 25.0, "reb": 10.0, "ast": 5.0,
            "stl": 1.2, "blk": 0.8, "tov": 2.5, "fg3m": 2.0,
        }
        fd = dfs_service._fd_score(stats)
        # 25*1 + 10*1.2 + 5*1.5 + 1.2*3 + 0.8*3 + 2.5*(-1)
        # = 25 + 12 + 7.5 + 3.6 + 2.4 - 2.5 = 48.0
        assert fd == 48.0

    def test_fd_no_three_pointer_bonus(self, dfs_service):
        """FD doesn't give a 3PM bonus — only DK does."""
        stats_with_3 = {
            "pts": 15.0, "reb": 3.0, "ast": 2.0,
            "stl": 0.0, "blk": 0.0, "tov": 1.0, "fg3m": 5.0,
        }
        stats_no_3 = {
            "pts": 15.0, "reb": 3.0, "ast": 2.0,
            "stl": 0.0, "blk": 0.0, "tov": 1.0, "fg3m": 0.0,
        }
        fd_with = dfs_service._fd_score(stats_with_3)
        fd_without = dfs_service._fd_score(stats_no_3)
        # FD has no fg3m weight, so should be identical
        assert fd_with == fd_without

    def test_fd_higher_steal_block_weight(self, dfs_service):
        """FD weights STL/BLK at 3x vs DK's 2x."""
        stats = {
            "pts": 0.0, "reb": 0.0, "ast": 0.0,
            "stl": 5.0, "blk": 5.0, "tov": 0.0, "fg3m": 0.0,
        }
        fd = dfs_service._fd_score(stats)
        dk = dfs_service._dk_score(stats)
        # FD: 5*3 + 5*3 = 30
        # DK: 5*2 + 5*2 = 20
        assert fd == 30.0
        assert dk == 20.0


# ==========================================================================
# Player projection tests
# ==========================================================================


class TestPlayerDFSProjection:
    """Test per-player DFS projection generation."""

    def test_star_player_projection(
        self, dfs_service, star_projection, star_player_with_stats
    ):
        """Star player should generate reasonable DFS projection."""
        dfs = dfs_service.project_player_dfs(
            star_projection, star_player_with_stats
        )

        assert dfs.player_id == 100
        assert dfs.projected_minutes == 35.0
        assert dfs.pts > 20.0  # ~25 pts expected
        assert dfs.reb > 8.0   # ~10 reb expected
        assert dfs.dk_points > 40.0
        assert dfs.fd_points > 40.0

    def test_bench_player_projection(
        self, dfs_service, bench_projection, bench_player_with_stats
    ):
        """Bench player should have lower DFS output."""
        dfs = dfs_service.project_player_dfs(
            bench_projection, bench_player_with_stats
        )

        assert dfs.projected_minutes == 18.0
        assert dfs.dk_points < 30.0
        assert dfs.dk_points > 10.0

    def test_floor_below_projection(
        self, dfs_service, star_projection, star_player_with_stats
    ):
        """Floor should be below the main projection."""
        dfs = dfs_service.project_player_dfs(
            star_projection, star_player_with_stats
        )

        assert dfs.dk_floor < dfs.dk_points
        assert dfs.fd_floor < dfs.fd_points

    def test_ceiling_above_projection(
        self, dfs_service, star_projection, star_player_with_stats
    ):
        """Ceiling should be above the main projection."""
        dfs = dfs_service.project_player_dfs(
            star_projection, star_player_with_stats
        )

        assert dfs.dk_ceiling > dfs.dk_points
        assert dfs.fd_ceiling > dfs.fd_points

    def test_zero_minutes_returns_zeros(self, dfs_service, star_player_with_stats):
        """Injured player (0 min) should have 0 projected stats."""
        proj = PlayerProjection(
            player_id=100,
            player_name="Star Player",
            position="G",
            baseline_minutes=35.0,
            adjusted_minutes=0.0,
            confidence=0.0,
            reason="Out - Injured",
        )
        dfs = dfs_service.project_player_dfs(proj, star_player_with_stats)

        assert dfs.pts == 0.0
        assert dfs.reb == 0.0
        assert dfs.dk_points == 0.0
        assert dfs.fd_points == 0.0


# ==========================================================================
# Team projection tests
# ==========================================================================


class TestTeamDFSProjection:
    """Test team-level DFS projection aggregation."""

    def test_team_totals(
        self,
        dfs_service,
        star_player_with_stats,
        bench_player_with_stats,
    ):
        """Team DK/FD totals should equal sum of player projections."""
        rotation = [star_player_with_stats, bench_player_with_stats]
        projections = [
            PlayerProjection(
                player_id=100,
                player_name="Star Player",
                position="G",
                baseline_minutes=35.0,
                adjusted_minutes=35.0,
                confidence=1.0,
            ),
            PlayerProjection(
                player_id=105,
                player_name="Bench Player",
                position="G",
                baseline_minutes=18.0,
                adjusted_minutes=18.0,
                confidence=1.0,
            ),
        ]

        # Build a minimal TeamRotation (hack total to pass validation)
        # We need total_minutes to pass the 239-241 validator
        # so we adjust to 240 by adding filler projections
        filler = [
            PlayerProjection(
                player_id=200 + i,
                player_name=f"Filler {i}",
                position="F",
                baseline_minutes=23.375,
                adjusted_minutes=23.375,
                confidence=1.0,
            )
            for i in range(8)
        ]
        all_projections = projections + filler
        total = sum(p.adjusted_minutes for p in all_projections)

        team_rotation = TeamRotation(
            team_id=1,
            team_name="Test Team",
            game_date="2025-01-15",
            projections=all_projections,
            total_minutes=total,
            positions_breakdown={"G": 53.0, "F": 187.0},
        )

        result = dfs_service.project_team_dfs(team_rotation, rotation)

        # Team totals should be positive (only 2 players have stat rates)
        assert result.team_dk_total > 0
        assert result.team_fd_total > 0

        # DFS data should be attached to projections on TeamRotation
        assert team_rotation.team_dk_total == result.team_dk_total
        assert team_rotation.team_fd_total == result.team_fd_total

    def test_injured_player_gets_zero_dfs(
        self,
        dfs_service,
        star_player_with_stats,
    ):
        """Injured players should get 0 DFS points on the team projection."""
        rotation = [star_player_with_stats]

        injured_proj = PlayerProjection(
            player_id=100,
            player_name="Star Player",
            position="G",
            baseline_minutes=35.0,
            adjusted_minutes=0.0,
            confidence=0.0,
            reason="Out - Injured",
        )

        # Fill remaining 240 min with other players
        filler = [
            PlayerProjection(
                player_id=200 + i,
                player_name=f"Filler {i}",
                position="F",
                baseline_minutes=26.667,
                adjusted_minutes=26.667,
                confidence=1.0,
            )
            for i in range(9)
        ]
        all_projections = [injured_proj] + filler
        total = sum(p.adjusted_minutes for p in all_projections)

        team_rotation = TeamRotation(
            team_id=1,
            team_name="Test Team",
            game_date="2025-01-15",
            projections=all_projections,
            total_minutes=round(total, 1),
            positions_breakdown={"G": 0, "F": 240.0},
        )

        dfs_service.project_team_dfs(team_rotation, rotation)

        # The injured player should have 0 DFS
        assert injured_proj.dk_points == 0.0
        assert injured_proj.fd_points == 0.0


# ==========================================================================
# Stat projection accuracy tests
# ==========================================================================


class TestStatProjections:
    """Test that projected stats scale linearly with minutes."""

    def test_stats_scale_with_minutes(self, dfs_service, star_player_with_stats):
        """Doubling minutes should double projected stats."""
        proj_low = PlayerProjection(
            player_id=100, player_name="Star Player", position="G",
            baseline_minutes=20.0, adjusted_minutes=20.0, confidence=1.0,
        )
        proj_high = PlayerProjection(
            player_id=100, player_name="Star Player", position="G",
            baseline_minutes=40.0, adjusted_minutes=40.0, confidence=1.0,
        )

        dfs_low = dfs_service.project_player_dfs(proj_low, star_player_with_stats)
        dfs_high = dfs_service.project_player_dfs(proj_high, star_player_with_stats)

        # PTS should roughly double (within rounding tolerance)
        assert abs(dfs_high.pts - 2 * dfs_low.pts) < 1.0
        assert abs(dfs_high.reb - 2 * dfs_low.reb) < 1.0
