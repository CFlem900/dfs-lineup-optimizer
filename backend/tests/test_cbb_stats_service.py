"""Tests for CBBStatsService — real team stats computation.

Tests the pace, efficiency, DvP, and Bayesian shrinkage formulas
using known input box scores and expected output values.  All tests
use synthetic data (no network calls).
"""

import pytest
import time
from unittest.mock import patch, MagicMock

from app.services.cbb_stats_service import (
    CBBStatsService,
    CBB_LEAGUE_AVG_PACE,
    CBB_LEAGUE_AVG_OFF_RATING,
    CBB_LEAGUE_AVG_DEF_RATING,
    CBB_LEAGUE_AVG_PPG,
    CBB_LEAGUE_AVG_STATS_PG,
    _aggregate_box_score,
)


# ============================================================================
# FIXTURES
# ============================================================================


def _make_box_score(
    pts=75, opp_pts=70, fga=60, fta=20, oreb=10, tov=13,
    reb=34, ast=14, stl=7, blk=3, fg3m=8,
    opp_reb=32, opp_ast=13, opp_stl=6, opp_blk=3,
    opp_tov=14, opp_fg3m=7, team_min=200.0,
):
    """Create a synthetic team box score dict."""
    return {
        "PTS": pts, "OPP_PTS": opp_pts,
        "FGA": fga, "FTA": fta, "OREB": oreb, "TOV": tov,
        "REB": reb, "AST": ast, "STL": stl, "BLK": blk, "FG3M": fg3m,
        "OPP_REB": opp_reb, "OPP_AST": opp_ast, "OPP_STL": opp_stl,
        "OPP_BLK": opp_blk, "OPP_TOV": opp_tov, "OPP_FG3M": opp_fg3m,
        "TEAM_MIN": team_min,
    }


def _make_n_box_scores(n, **overrides):
    """Create n identical synthetic box scores."""
    return [_make_box_score(**overrides) for _ in range(n)]


# ============================================================================
# PACE COMPUTATION
# ============================================================================


class TestComputePace:
    """Test the possession / pace formula."""

    def test_compute_pace_standard(self):
        """Known box score → predictable pace."""
        # Possessions per game = FGA + 0.44*FTA - OREB + TOV
        # = 60 + 0.44*20 - 10 + 13 = 60 + 8.8 - 10 + 13 = 71.8
        # Pace = (poss / minutes) * 200 = (71.8 / 200) * 200 = 71.8
        games = [_make_box_score()]
        pace = CBBStatsService._compute_pace(games)
        assert abs(pace - 71.8) < 0.01

    def test_compute_pace_multiple_games(self):
        """Pace computation over multiple games should average correctly."""
        # All games identical, so pace should be the same as single-game
        games = _make_n_box_scores(10)
        pace = CBBStatsService._compute_pace(games)
        assert abs(pace - 71.8) < 0.01

    def test_compute_pace_fast_team(self):
        """High FGA + high TOV → high pace."""
        games = [_make_box_score(fga=75, fta=25, oreb=8, tov=16)]
        pace = CBBStatsService._compute_pace(games)
        # 75 + 0.44*25 - 8 + 16 = 75 + 11 - 8 + 16 = 94.0
        assert pace > 80

    def test_compute_pace_slow_team(self):
        """Low FGA + low TOV → low pace."""
        games = [_make_box_score(fga=48, fta=12, oreb=12, tov=9)]
        pace = CBBStatsService._compute_pace(games)
        # 48 + 0.44*12 - 12 + 9 = 48 + 5.28 - 12 + 9 = 50.28
        assert pace < 60

    def test_compute_pace_empty_games(self):
        """No games → league average pace."""
        pace = CBBStatsService._compute_pace([])
        assert pace == CBB_LEAGUE_AVG_PACE

    def test_compute_pace_zero_minutes(self):
        """Zero minutes → league average (avoid division by zero)."""
        games = [_make_box_score(team_min=0)]
        pace = CBBStatsService._compute_pace(games)
        assert pace == CBB_LEAGUE_AVG_PACE


# ============================================================================
# EFFICIENCY (OFF/DEF RATING)
# ============================================================================


class TestComputeEfficiency:
    """Test offensive and defensive rating computation."""

    def test_off_def_rating_standard(self):
        """Known inputs → predictable ratings."""
        games = [_make_box_score(pts=75, opp_pts=70)]
        pace = 71.8  # From previous pace test
        off_rtg, def_rtg = CBBStatsService._compute_efficiency(games, pace)
        # OffRtg = (PTS / Possessions) * 100
        # Possessions = 60 + 0.44*20 - 10 + 13 = 71.8
        # OffRtg = (75 / 71.8) * 100 ≈ 104.46
        # DefRtg = (70 / 71.8) * 100 ≈ 97.49
        assert 103 < off_rtg < 106
        assert 96 < def_rtg < 99

    def test_high_efficiency_team(self):
        """Team scoring a lot on few possessions → high off rating."""
        games = [_make_box_score(pts=90, opp_pts=60, fga=55, tov=8)]
        pace = 60.0
        off_rtg, def_rtg = CBBStatsService._compute_efficiency(games, pace)
        assert off_rtg > 120  # Very efficient
        assert def_rtg < 100  # Good defense

    def test_low_efficiency_team(self):
        """Team scoring little on many possessions → low off rating."""
        # Possessions = 70 + 0.44*20 - 10 + 18 = 70 + 8.8 - 10 + 18 = 86.8
        # OffRtg = (55 / 86.8) * 100 ≈ 63.4  → low
        # DefRtg = (85 / 86.8) * 100 ≈ 97.9  → close to average
        games = [_make_box_score(pts=55, opp_pts=85, fga=70, tov=18)]
        pace = 80.0
        off_rtg, def_rtg = CBBStatsService._compute_efficiency(games, pace)
        assert off_rtg < 80  # Inefficient
        assert def_rtg > off_rtg  # Defense is worse than offense

    def test_efficiency_empty_possessions(self):
        """Zero possessions → league average ratings."""
        games = [_make_box_score(fga=0, fta=0, oreb=0, tov=0)]
        off_rtg, def_rtg = CBBStatsService._compute_efficiency(games, 68.0)
        assert off_rtg == CBB_LEAGUE_AVG_OFF_RATING
        assert def_rtg == CBB_LEAGUE_AVG_DEF_RATING


# ============================================================================
# SCORING AVERAGES
# ============================================================================


class TestScoringAverages:
    """Test PPG and opponent PPG computation."""

    def test_scoring_averages(self):
        """Simple multi-game average."""
        games = [
            _make_box_score(pts=80, opp_pts=70),
            _make_box_score(pts=70, opp_pts=60),
            _make_box_score(pts=90, opp_pts=80),
        ]
        ppg, opp_ppg = CBBStatsService._compute_scoring_averages(games)
        assert abs(ppg - 80.0) < 0.01
        assert abs(opp_ppg - 70.0) < 0.01

    def test_scoring_averages_empty(self):
        """No games → league average."""
        ppg, opp_ppg = CBBStatsService._compute_scoring_averages([])
        assert ppg == CBB_LEAGUE_AVG_PPG
        assert opp_ppg == CBB_LEAGUE_AVG_PPG


# ============================================================================
# OPPONENT-ALLOWED STATS (DvP basis)
# ============================================================================


class TestOpponentAllowedStats:
    """Test DvP stat aggregation."""

    def test_opponent_stats_calculation(self):
        """Average opponent stats across multiple games."""
        games = [
            _make_box_score(
                opp_pts=80, opp_reb=36, opp_ast=16,
                opp_stl=7, opp_blk=4, opp_tov=11, opp_fg3m=9,
            ),
            _make_box_score(
                opp_pts=70, opp_reb=30, opp_ast=12,
                opp_stl=5, opp_blk=2, opp_tov=15, opp_fg3m=5,
            ),
        ]
        stats = CBBStatsService._compute_opponent_allowed_stats(games)
        assert abs(stats["pts"] - 75.0) < 0.01
        assert abs(stats["reb"] - 33.0) < 0.01
        assert abs(stats["ast"] - 14.0) < 0.01
        assert abs(stats["stl"] - 6.0) < 0.01
        assert abs(stats["blk"] - 3.0) < 0.01
        assert abs(stats["tov"] - 13.0) < 0.01
        assert abs(stats["fg3m"] - 7.0) < 0.01

    def test_opponent_stats_empty(self):
        """No games → league averages."""
        stats = CBBStatsService._compute_opponent_allowed_stats([])
        assert stats["pts"] == CBB_LEAGUE_AVG_STATS_PG["pts"]
        assert stats["reb"] == CBB_LEAGUE_AVG_STATS_PG["reb"]


# ============================================================================
# BAYESIAN SHRINKAGE
# ============================================================================


class TestBayesianShrinkage:
    """Test the Bayesian shrinkage weight and blending."""

    def test_shrinkage_weight_small_sample(self):
        """5 games = 50% weight (5/10)."""
        w = CBBStatsService._shrinkage_weight(5)
        assert abs(w - 0.5) < 0.001

    def test_shrinkage_weight_full_sample(self):
        """15+ games = 100% weight."""
        w = CBBStatsService._shrinkage_weight(15)
        assert w == 1.0

    def test_shrinkage_weight_zero_games(self):
        """0 games = 0% weight (pure prior)."""
        w = CBBStatsService._shrinkage_weight(0)
        assert w == 0.0

    def test_shrinkage_weight_exact_threshold(self):
        """Exactly 10 games = 100% weight."""
        w = CBBStatsService._shrinkage_weight(10)
        assert w == 1.0

    def test_shrinkage_weight_caps_at_one(self):
        """Above threshold still caps at 1.0."""
        w = CBBStatsService._shrinkage_weight(30)
        assert w == 1.0

    def test_blend_full_weight(self):
        """Weight=1 → pure observed value."""
        result = CBBStatsService._blend(80.0, 68.0, 1.0)
        assert result == 80.0

    def test_blend_zero_weight(self):
        """Weight=0 → pure prior value."""
        result = CBBStatsService._blend(80.0, 68.0, 0.0)
        assert result == 68.0

    def test_blend_half_weight(self):
        """Weight=0.5 → midpoint between observed and prior."""
        result = CBBStatsService._blend(80.0, 68.0, 0.5)
        assert abs(result - 74.0) < 0.001

    def test_shrinkage_in_full_pipeline(self):
        """5-game sample: pace should be blended toward league avg."""
        svc = CBBStatsService()
        # Create 5 games with pace ≈ 80 (fast)
        games = _make_n_box_scores(5, fga=70, fta=22, oreb=9, tov=15)
        raw_pace = svc._compute_pace(games)
        assert raw_pace > 75  # Raw pace is fast

        w = svc._shrinkage_weight(5)  # 0.5
        blended = svc._blend(raw_pace, CBB_LEAGUE_AVG_PACE, w)
        # Blended should be between raw and league avg
        assert CBB_LEAGUE_AVG_PACE < blended < raw_pace

    def test_full_sample_no_shrinkage(self):
        """15-game sample: pace should be pure observed."""
        svc = CBBStatsService()
        games = _make_n_box_scores(15, fga=70, fta=22, oreb=9, tov=15)
        raw_pace = svc._compute_pace(games)
        w = svc._shrinkage_weight(15)  # 1.0
        blended = svc._blend(raw_pace, CBB_LEAGUE_AVG_PACE, w)
        assert abs(blended - raw_pace) < 0.001


# ============================================================================
# SANITY CLAMPS
# ============================================================================


class TestSanityClamps:
    """Verify that computed values are clamped to realistic ranges."""

    @pytest.mark.asyncio
    async def test_pace_clamped_to_range(self):
        """Computed pace must be in [55, 85]."""
        svc = CBBStatsService()
        # Create extreme games but result should be clamped
        with patch.object(svc, "_fetch_team_box_scores", return_value=[
            _make_box_score(fga=100, fta=50, oreb=2, tov=20)  # Extreme
        ]):
            stats = await svc.get_team_stats(1, "Extreme Team")
            assert 55.0 <= stats["season_pace"] <= 85.0

    @pytest.mark.asyncio
    async def test_off_rating_clamped(self):
        """Offensive rating clamped to [80, 130]."""
        svc = CBBStatsService()
        with patch.object(svc, "_fetch_team_box_scores", return_value=[
            _make_box_score(pts=150, fga=40, fta=10, oreb=5, tov=5)
        ]):
            stats = await svc.get_team_stats(2, "Super Offense")
            assert 80.0 <= stats["season_off_rating"] <= 130.0

    @pytest.mark.asyncio
    async def test_ppg_clamped(self):
        """PPG clamped to [50, 100]."""
        svc = CBBStatsService()
        with patch.object(svc, "_fetch_team_box_scores", return_value=[
            _make_box_score(pts=120)
        ]):
            stats = await svc.get_team_stats(3, "High Scorer")
            assert 50.0 <= stats["season_ppg"] <= 100.0


# ============================================================================
# CACHE
# ============================================================================


class TestCache:
    """Test in-memory caching behavior."""

    @pytest.mark.asyncio
    async def test_cache_hit_avoids_recomputation(self):
        """Second call returns cached data, no recomputation."""
        svc = CBBStatsService()
        mock_games = _make_n_box_scores(10)

        call_count = 0
        original_fetch = svc._fetch_team_box_scores

        def counting_fetch(name, season):
            nonlocal call_count
            call_count += 1
            return mock_games

        with patch.object(svc, "_fetch_team_box_scores", side_effect=counting_fetch):
            stats1 = await svc.get_team_stats(100, "Test Team")
            stats2 = await svc.get_team_stats(100, "Test Team")

        assert call_count == 1  # Only fetched once
        assert stats1 == stats2

    @pytest.mark.asyncio
    async def test_clear_cache(self):
        """clear_cache() forces recomputation."""
        svc = CBBStatsService()
        mock_games = _make_n_box_scores(10)

        call_count = 0

        def counting_fetch(name, season):
            nonlocal call_count
            call_count += 1
            return mock_games

        with patch.object(svc, "_fetch_team_box_scores", side_effect=counting_fetch):
            await svc.get_team_stats(100, "Test Team")
            svc.clear_cache()
            await svc.get_team_stats(100, "Test Team")

        assert call_count == 2  # Fetched twice after cache clear


# ============================================================================
# DEFAULT STATS (no data)
# ============================================================================


class TestDefaultStats:
    """Test fallback to league averages."""

    @pytest.mark.asyncio
    async def test_no_box_scores_returns_defaults(self):
        """Empty box scores → league average stats."""
        svc = CBBStatsService()
        with patch.object(svc, "_fetch_team_box_scores", return_value=[]):
            stats = await svc.get_team_stats(999, "Unknown Team")

        assert stats["season_pace"] == CBB_LEAGUE_AVG_PACE
        assert stats["season_off_rating"] == CBB_LEAGUE_AVG_OFF_RATING
        assert stats["season_ppg"] == CBB_LEAGUE_AVG_PPG
        assert stats["games_played"] == 0
        assert stats["shrinkage_weight"] == 0.0

    def test_default_team_stats_structure(self):
        """Default stats dict has all required keys."""
        stats = CBBStatsService._default_team_stats(1, "Test")
        required_keys = [
            "team_id", "name", "games_played", "shrinkage_weight",
            "season_pace", "season_off_rating", "season_def_rating",
            "season_ppg", "season_opp_ppg",
            "opp_pts_pg", "opp_reb_pg", "opp_ast_pg",
            "opp_stl_pg", "opp_blk_pg", "opp_tov_pg", "opp_fg3m_pg",
        ]
        for key in required_keys:
            assert key in stats, f"Missing key: {key}"

    def test_league_averages_accessor(self):
        """get_league_averages returns a sensible dict."""
        svc = CBBStatsService()
        avgs = svc.get_league_averages()
        assert avgs["pace"] == CBB_LEAGUE_AVG_PACE
        assert avgs["off_rating"] == CBB_LEAGUE_AVG_OFF_RATING
        assert avgs["opp_pts_pg"] == CBB_LEAGUE_AVG_STATS_PG["pts"]


# ============================================================================
# FULL PIPELINE
# ============================================================================


class TestFullPipeline:
    """End-to-end tests for the stats service with mock data."""

    @pytest.mark.asyncio
    async def test_compute_team_stats_realistic(self):
        """10-game sample produces realistic stat values."""
        svc = CBBStatsService()
        games = _make_n_box_scores(10, pts=78, opp_pts=68,
                                    fga=62, fta=18, oreb=11, tov=12)

        with patch.object(svc, "_fetch_team_box_scores", return_value=games):
            stats = await svc.get_team_stats(150, "Duke Blue Devils")

        # With 10 games = full weight, stats should reflect the data
        assert stats["games_played"] == 10
        assert stats["shrinkage_weight"] == 1.0
        assert stats["season_ppg"] == 78.0  # Exact since full weight
        assert stats["season_opp_ppg"] == 68.0
        assert 55 <= stats["season_pace"] <= 85  # Within clamp range
        assert stats["season_off_rating"] > CBB_LEAGUE_AVG_OFF_RATING  # Good offense
        assert stats["season_def_rating"] < CBB_LEAGUE_AVG_DEF_RATING  # Good defense

    @pytest.mark.asyncio
    async def test_compute_multiple_teams(self):
        """get_all_team_stats processes multiple teams."""
        svc = CBBStatsService()
        games = _make_n_box_scores(10)

        with patch.object(svc, "_fetch_team_box_scores", return_value=games):
            result = await svc.get_all_team_stats([
                (150, "Duke"),
                (151, "UNC"),
                (152, "Kentucky"),
            ])

        assert len(result) == 3
        assert 150 in result
        assert 151 in result
        assert 152 in result
        for tid, stats in result.items():
            assert stats["games_played"] == 10
            assert "season_pace" in stats

    @pytest.mark.asyncio
    async def test_small_sample_shrinkage_applied(self):
        """3-game sample: stats pulled toward league average."""
        svc = CBBStatsService()
        # Extreme scoring: 95 ppg (well above league avg of 73)
        games = _make_n_box_scores(3, pts=95, opp_pts=60)

        with patch.object(svc, "_fetch_team_box_scores", return_value=games):
            stats = await svc.get_team_stats(200, "Small Sample Team")

        # With 3 games (w=0.3), PPG should be pulled toward 73
        # Expected: 0.3 * 95 + 0.7 * 73 = 28.5 + 51.1 = 79.6
        assert stats["shrinkage_weight"] == 0.3
        assert 75 < stats["season_ppg"] < 85  # Pulled toward 73 but not fully
        assert stats["season_ppg"] < 95  # Definitely below raw

    @pytest.mark.asyncio
    async def test_dvp_stats_populated(self):
        """Opponent-allowed stats should be populated in output."""
        svc = CBBStatsService()
        games = _make_n_box_scores(10,
                                    opp_pts=80, opp_reb=38,
                                    opp_ast=16, opp_stl=7,
                                    opp_blk=4, opp_tov=11, opp_fg3m=9)

        with patch.object(svc, "_fetch_team_box_scores", return_value=games):
            stats = await svc.get_team_stats(250, "Bad Defense Team")

        # With 10 games = full weight, DvP stats should reflect data
        assert stats["opp_pts_pg"] == 80.0
        assert stats["opp_reb_pg"] == 38.0
        assert stats["opp_ast_pg"] == 16.0


# ============================================================================
# AGGREGATE BOX SCORE HELPER
# ============================================================================


class TestAggregateBoxScore:
    """Test the _aggregate_box_score helper."""

    def test_aggregate_empty_df(self):
        """Empty DataFrame → empty dict."""
        import pandas as pd
        result = _aggregate_box_score(pd.DataFrame())
        assert result == {}

    def test_aggregate_none(self):
        """None → empty dict."""
        result = _aggregate_box_score(None)
        assert result == {}

    def test_aggregate_normal_df(self):
        """Normal DataFrame with standard columns."""
        import pandas as pd
        df = pd.DataFrame({
            "PTS": [20, 15, 10],
            "FGA": [12, 10, 8],
            "FTA": [5, 3, 2],
            "OREB": [3, 2, 1],
            "TO": [2, 3, 1],
            "REB": [8, 6, 4],
            "AST": [5, 3, 2],
            "STL": [1, 2, 1],
            "BLK": [0, 1, 2],
            "3PM": [2, 1, 3],
        })
        result = _aggregate_box_score(df)
        assert result["PTS"] == 45
        assert result["FGA"] == 30
        assert result["FTA"] == 10
        assert result["OREB"] == 6
        assert result["TOV"] == 6  # From "TO" column
        assert result["REB"] == 18
        assert result["AST"] == 10
        assert result["STL"] == 4
        assert result["BLK"] == 3
        assert result["FG3M"] == 6  # From "3PM" column

    def test_aggregate_lowercase_columns(self):
        """Handles lowercase column names from CBBpy."""
        import pandas as pd
        df = pd.DataFrame({
            "pts": [20, 15],
            "fga": [12, 10],
            "fta": [5, 3],
            "oreb": [3, 2],
            "to": [2, 3],
            "reb": [8, 6],
            "ast": [5, 3],
            "stl": [1, 2],
            "blk": [0, 1],
            "fg3m": [2, 1],
        })
        result = _aggregate_box_score(df)
        assert result["PTS"] == 35
        assert result["REB"] == 14


# ============================================================================
# CBBGameService INTEGRATION WITH STATS SERVICE
# ============================================================================


class TestCBBGameServiceWithStats:
    """Test that CBBGameService uses real stats when available."""

    def test_dvp_from_real_stats_cache(self):
        """Real stats in cache → DvP reflects actual values."""
        from app.services.cbb_game_service import CBBGameService

        svc = CBBGameService()
        # Pre-populate real stats cache with a team allowing high pts
        svc._real_stats_cache = {
            100: {
                "opp_pts_pg": 82.0,   # League avg = 73.0 → factor > 1.0
                "opp_reb_pg": 38.0,
                "opp_ast_pg": 16.0,
                "opp_stl_pg": 7.0,
                "opp_blk_pg": 3.5,
                "opp_tov_pg": 13.0,
                "opp_fg3m_pg": 8.5,
            }
        }
        factors = svc.get_dvp_matchup_factors(100)
        assert factors["pts"] > 1.0  # Allowing more points
        assert factors["reb"] > 1.0  # Allowing more rebounds

    def test_dvp_falls_back_to_espn(self):
        """No real stats → falls back to ESPN cache."""
        from app.services.cbb_game_service import CBBGameService

        svc = CBBGameService()
        svc._real_stats_cache = {}  # No real stats
        svc._team_stats_cache = {
            200: {
                "opp_pts_pg": 65.0,   # Below avg → good defense
                "opp_reb_pg": 30.0,
                "opp_ast_pg": 11.0,
                "opp_stl_pg": 5.0,
                "opp_blk_pg": 2.5,
                "opp_tov_pg": 11.0,
                "opp_fg3m_pg": 6.0,
            }
        }
        svc._team_stats_cache_ts = 9999999999.0
        factors = svc.get_dvp_matchup_factors(200)
        assert factors["pts"] < 1.0  # Good defense
        assert factors["reb"] < 1.0

    def test_get_best_stats_prefers_real(self):
        """_get_best_team_stats prefers real stats over ESPN defaults."""
        from app.services.cbb_game_service import CBBGameService

        svc = CBBGameService()
        svc._team_stats_cache = {
            100: {"season_pace": CBB_LEAGUE_AVG_PACE, "name": "ESPN Duke"},
        }
        svc._team_stats_cache_ts = 9999999999.0
        svc._real_stats_cache = {
            100: {"season_pace": 75.5, "name": "Real Duke"},
        }

        best = svc._get_best_team_stats()
        # Real stats should override ESPN for team 100
        assert best[100]["season_pace"] == 75.5

    @pytest.mark.asyncio
    async def test_warm_stats_for_slate(self):
        """warm_stats_for_slate populates real stats cache."""
        from app.services.cbb_game_service import CBBGameService
        from app.services.cbb_stats_service import CBBStatsService

        stats_svc = CBBStatsService()
        game_svc = CBBGameService(stats_service=stats_svc)

        # Mock the scoreboard to return one game
        mock_games = [{
            "id": "1",
            "date": "2026-02-15",
            "status": "STATUS_SCHEDULED",
            "home": {"id": 150, "name": "Duke", "abbreviation": "DUKE",
                     "score": 0, "records": []},
            "away": {"id": 153, "name": "UNC", "abbreviation": "UNC",
                     "score": 0, "records": []},
        }]

        mock_box = _make_n_box_scores(10)

        with patch.object(game_svc, "_fetch_scoreboard", return_value=mock_games), \
             patch.object(stats_svc, "_fetch_team_box_scores", return_value=mock_box), \
             patch.object(game_svc, "_get_espn_team_stats_cache", return_value={}):

            count = await game_svc.warm_stats_for_slate("2026-02-15")

        assert count == 2  # Duke + UNC
        assert 150 in game_svc._real_stats_cache
        assert 153 in game_svc._real_stats_cache

    def test_dvp_clamp_range(self):
        """DvP factors clamped to [0.80, 1.20]."""
        from app.services.cbb_game_service import CBBGameService

        # Extreme values that would produce factors outside range
        stats = {
            "opp_pts_pg": 200.0,  # Would be 200/73 = 2.74
            "opp_reb_pg": 5.0,    # Would be 5/34 = 0.15
            "opp_ast_pg": 14.5,
            "opp_stl_pg": 6.5,
            "opp_blk_pg": 3.2,
            "opp_tov_pg": 12.5,
            "opp_fg3m_pg": 7.5,
        }
        factors = CBBGameService._compute_dvp_from_stats(stats)
        assert factors["pts"] == 1.20   # Clamped to max
        assert factors["reb"] == 0.80   # Clamped to min
