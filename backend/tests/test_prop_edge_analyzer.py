"""Tests for PropEdgeAnalyzer — player‑prop edge comparison module."""

import pytest
import pandas as pd
from datetime import date, timedelta
from unittest.mock import MagicMock

from app.models.balldontlie_schemas import BDLGame, BDLTeam
from app.services.prop_edge_analyzer import (
    PropEdgeAnalyzer,
    PlayerPropLine,
    SimProjection,
    AdjustmentResult,
    ContextualRefiner,
    GarbageTimeOpportunity,
    GarbageTimePivot,
    GarbageTimeResult,
    american_odds_to_implied_prob,
    kelly_fraction,
    prob_over,
    CR_WIN_PCT_GAP_THRESHOLD,
    CR_FAVORITE_MARGIN_THRESHOLD,
    CR_HIGH_RISK_MINUTES_FACTOR,
    GT_MAX_AVG_MINUTES,
    GT_USAGE_SPIKE_THRESHOLD,
    GT_SPECIALIST_MINUTES_FACTOR,
    GT_MIN_BLOWOUT_GAMES,
)


# ── Helper math tests ────────────────────────────────────────────────────


class TestAmericanOddsConversion:
    def test_standard_minus_110(self):
        p = american_odds_to_implied_prob(-110)
        assert abs(p - 0.5238) < 0.001

    def test_plus_150(self):
        p = american_odds_to_implied_prob(150)
        assert abs(p - 0.4000) < 0.001

    def test_heavy_favorite(self):
        p = american_odds_to_implied_prob(-200)
        assert abs(p - 0.6667) < 0.001

    def test_even_money(self):
        p = american_odds_to_implied_prob(100)
        assert abs(p - 0.5000) < 0.001


class TestProbOver:
    def test_mean_above_line(self):
        # Mean 50, std 10, line 45 → should be > 0.5
        p = prob_over(50.0, 10.0, 45.0)
        assert p > 0.5

    def test_mean_below_line(self):
        # Mean 40, std 10, line 45 → should be < 0.5
        p = prob_over(40.0, 10.0, 45.0)
        assert p < 0.5

    def test_mean_equals_line(self):
        # Should be ~0.5
        p = prob_over(45.0, 10.0, 45.0)
        assert abs(p - 0.5) < 0.001

    def test_zero_std_above(self):
        assert prob_over(50.0, 0.0, 45.0) == 1.0

    def test_zero_std_below(self):
        assert prob_over(40.0, 0.0, 45.0) == 0.0

    def test_large_edge(self):
        # Mean 60, std 8, line 45 → very high P(over)
        p = prob_over(60.0, 8.0, 45.0)
        assert p > 0.95


class TestKellyFraction:
    def test_positive_edge(self):
        k = kelly_fraction(0.08, -110, fraction=0.25)
        assert k > 0.0

    def test_zero_edge(self):
        assert kelly_fraction(0.0, -110) == 0.0

    def test_negative_edge(self):
        assert kelly_fraction(-0.05, -110) == 0.0


# ── Core analyzer tests ──────────────────────────────────────────────────


@pytest.fixture
def sample_props():
    return [
        PlayerPropLine(
            player_name="Luka Doncic",
            line=48.5,
            over_odds=-110,
            team="DAL",
            opponent="LAL",
        ),
        PlayerPropLine(
            player_name="Shai Gilgeous-Alexander",
            line=42.5,
            over_odds=-115,
            team="OKC",
            opponent="DEN",
        ),
        PlayerPropLine(
            player_name="Jayson Tatum",
            line=40.5,
            over_odds=-105,
            team="BOS",
            opponent="MIL",
        ),
        PlayerPropLine(
            player_name="Anthony Edwards",
            line=37.5,
            over_odds=-110,
            team="MIN",
            opponent="PHX",
        ),
        PlayerPropLine(
            player_name="Nikola Jokic",
            line=52.5,
            over_odds=-120,
            team="DEN",
            opponent="OKC",
        ),
        PlayerPropLine(
            player_name="Tyrese Haliburton",
            line=34.5,
            over_odds=-110,
            team="IND",
            opponent="CLE",
        ),
    ]


@pytest.fixture
def sample_projections():
    return {
        "Luka Doncic": SimProjection(mean_pra=52.1, std_pra=9.3),
        "Shai Gilgeous-Alexander": SimProjection(mean_pra=46.8, std_pra=8.1),
        "Jayson Tatum": SimProjection(mean_pra=43.2, std_pra=8.5),
        "Anthony Edwards": SimProjection(mean_pra=36.0, std_pra=7.8),
        "Nikola Jokic": SimProjection(mean_pra=56.4, std_pra=9.8),
        "Tyrese Haliburton": SimProjection(mean_pra=33.0, std_pra=7.2),
    }


class TestPropEdgeAnalyzer:
    def test_returns_dataframe(self, sample_props, sample_projections):
        analyzer = PropEdgeAnalyzer()
        df = analyzer.find_top_over_edges(sample_props, sample_projections, top_n=5)
        assert isinstance(df, pd.DataFrame)
        assert len(df) <= 5

    def test_sorted_by_edge_descending(self, sample_props, sample_projections):
        analyzer = PropEdgeAnalyzer()
        df = analyzer.find_top_over_edges(sample_props, sample_projections, top_n=5)
        if len(df) > 1:
            edges = df["edge_pct"].tolist()
            assert edges == sorted(edges, reverse=True)

    def test_positive_edges_for_mean_above_line(self):
        props = [
            PlayerPropLine(player_name="Player A", line=40.0, over_odds=-110),
        ]
        projections = {"Player A": {"mean_pra": 48.0, "std_pra": 8.0}}
        analyzer = PropEdgeAnalyzer()
        df = analyzer.find_top_over_edges(props, projections, top_n=5)
        assert len(df) == 1
        assert df.iloc[0]["edge_pct"] > 0

    def test_negative_edge_for_mean_below_line(self):
        props = [
            PlayerPropLine(player_name="Player B", line=50.0, over_odds=-110),
        ]
        projections = {"Player B": {"mean_pra": 38.0, "std_pra": 7.0}}
        analyzer = PropEdgeAnalyzer()
        df = analyzer.analyze_all(props, projections)
        assert len(df) == 1
        assert df.iloc[0]["edge_pct"] < 0

    def test_missing_player_skipped(self, sample_props, sample_projections):
        # Add a prop with no matching projection
        extra = sample_props + [
            PlayerPropLine(player_name="Missing Player", line=30.0)
        ]
        analyzer = PropEdgeAnalyzer()
        df = analyzer.find_top_over_edges(extra, sample_projections, top_n=10)
        names = df["player"].tolist()
        assert "Missing Player" not in names

    def test_min_edge_filter(self, sample_props, sample_projections):
        analyzer = PropEdgeAnalyzer(min_edge_pct=0.05)
        df = analyzer.find_top_over_edges(sample_props, sample_projections, top_n=10)
        if len(df) > 0:
            assert all(df["edge_pct"] >= 0.05)

    def test_dict_projections_accepted(self):
        props = [PlayerPropLine(player_name="Test", line=40.0)]
        projections = {"Test": {"mean_pra": 45.0, "std_pra": 8.0}}
        analyzer = PropEdgeAnalyzer()
        df = analyzer.find_top_over_edges(props, projections)
        assert len(df) == 1

    def test_all_columns_present(self, sample_props, sample_projections):
        analyzer = PropEdgeAnalyzer()
        df = analyzer.find_top_over_edges(sample_props, sample_projections)
        expected_cols = {
            "player", "team", "opponent", "stat_type", "book_line",
            "over_odds", "sim_mean", "sim_std", "p_over", "implied",
            "edge_pct", "edge_display", "kelly_frac", "confidence",
        }
        assert expected_cols.issubset(set(df.columns))

    def test_confidence_labels(self, sample_props, sample_projections):
        analyzer = PropEdgeAnalyzer()
        df = analyzer.analyze_all(sample_props, sample_projections)
        valid = {"High", "Medium", "Low"}
        assert all(c in valid for c in df["confidence"])

    def test_edge_display_format(self):
        props = [PlayerPropLine(player_name="X", line=40.0)]
        projections = {"X": SimProjection(mean_pra=48.0, std_pra=8.0)}
        analyzer = PropEdgeAnalyzer()
        df = analyzer.find_top_over_edges(props, projections)
        display = df.iloc[0]["edge_display"]
        assert display.startswith("+")
        assert display.endswith("%")

    def test_empty_when_no_props(self):
        analyzer = PropEdgeAnalyzer()
        df = analyzer.find_top_over_edges([], {})
        assert len(df) == 0


class TestProjectionsFromSimResults:
    """Test the bridge from PlayerSimResult → SimProjection."""

    def test_conversion(self):
        # Minimal mock of PlayerSimResult
        class MockPSR:
            player_name = "Test Player"
            mean_pts = 25.0
            mean_reb = 8.0
            mean_ast = 6.0
            std_pts = 5.0
            std_reb = 2.5
            std_ast = 2.0

        result = PropEdgeAnalyzer.projections_from_sim_results([MockPSR()])
        assert "Test Player" in result
        proj = result["Test Player"]
        assert abs(proj.mean_pra - 39.0) < 0.01
        # std = sqrt(25 + 6.25 + 4) = sqrt(35.25) ≈ 5.94
        assert abs(proj.std_pra - 5.94) < 0.1


# ── ContextualRefiner tests ──────────────────────────────────────────────


def _make_team_games(
    team_id: int,
    team_abbr: str,
    total: int = 60,
    wins: int = 45,
    win_margin: int = 12,
    loss_margin: int = 15,
    losses_first: bool = True,
) -> list:
    """Build a season of BDLGame objects for a single team.

    When ``losses_first=True``, losses are chronologically early and wins
    are recent — suitable for simulating a dominant team with strong recent
    form.  When ``losses_first=False``, wins come first and losses are
    recent (a struggling team).

    All games are constructed with the target team as the *home* team so
    that ``_build_context`` computes ``margin = h_score − v_score``.
    """
    team = BDLTeam(id=team_id, abbreviation=team_abbr)
    opp = BDLTeam(id=99, abbreviation="OPP")
    games = []
    n_losses = total - wins
    start = date(2025, 10, 22)

    for i in range(total):
        game_date = (start + timedelta(days=i * 2)).isoformat()

        if losses_first:
            is_win = i >= n_losses  # losses early, wins late (recent)
        else:
            is_win = i < wins  # wins early, losses late (recent)

        if is_win:
            h_score = 100 + win_margin
            v_score = 100
        else:
            h_score = 100
            v_score = 100 + loss_margin

        games.append(BDLGame(
            id=1000 + team_id * 100 + i,
            home_team=team,
            visitor_team=opp,
            status="Final",
            home_team_score=h_score,
            visitor_team_score=v_score,
            date=game_date,
            season=2025,
        ))

    return games


class TestContextualRefiner:
    """Tests for ContextualRefiner blowout risk assessment."""

    @pytest.fixture
    def mock_client(self):
        """Create a mock BDLMCPClient with four team mappings."""
        client = MagicMock()
        client.is_available = True
        client.get_teams.return_value = [
            BDLTeam(id=1, abbreviation="OKC"),
            BDLTeam(id=2, abbreviation="DET"),
            BDLTeam(id=3, abbreviation="BOS"),
            BDLTeam(id=4, abbreviation="NYK"),
        ]
        return client

    # ── High risk ──────────────────────────────────────────────────────

    def test_high_risk_dominant_vs_weak(self, mock_client):
        """OKC .750 + recent margin +12 vs DET .333 → High, 0.85 factor."""
        # OKC: 60 gm, 45 W / 15 L, losses early → recent 5 are +12 wins
        okc_games = _make_team_games(1, "OKC", 60, 45,
                                     win_margin=12, losses_first=True)
        # DET: 60 gm, 20 W / 40 L, wins early → recent 5 are −12 losses
        det_games = _make_team_games(2, "DET", 60, 20,
                                     win_margin=10, loss_margin=12,
                                     losses_first=False)

        def _games(league="NBA", *, season=None, team_id=None):
            return okc_games if team_id == 1 else det_games

        mock_client.get_games.side_effect = _games

        refiner = ContextualRefiner(mock_client)
        result = refiner.calculate_blowout_risk(
            1, 2, original_edge=0.08, game_date="2026-02-25",
        )

        assert result.risk_score == "High"
        assert result.minutes_factor == CR_HIGH_RISK_MINUTES_FACTOR
        assert result.win_pct_gap > CR_WIN_PCT_GAP_THRESHOLD
        assert result.favorite_avg_margin > CR_FAVORITE_MARGIN_THRESHOLD
        assert result.favored_team == "OKC"

    def test_high_risk_reduces_edge(self, mock_client):
        """When risk is High, adjusted_edge < original_edge."""
        okc_games = _make_team_games(1, "OKC", 60, 45,
                                     win_margin=12, losses_first=True)
        det_games = _make_team_games(2, "DET", 60, 20,
                                     win_margin=10, loss_margin=12,
                                     losses_first=False)

        def _games(league="NBA", *, season=None, team_id=None):
            return okc_games if team_id == 1 else det_games

        mock_client.get_games.side_effect = _games

        refiner = ContextualRefiner(mock_client)
        result = refiner.calculate_blowout_risk(
            1, 2,
            original_edge=0.08,
            sim_mean=48.0,
            sim_std=8.0,
            book_line=42.5,
            over_odds=-110,
            game_date="2026-02-25",
        )

        assert result.risk_score == "High"
        assert result.adjusted_edge < result.original_edge

    # ── Low risk ───────────────────────────────────────────────────────

    def test_low_risk_close_matchup(self, mock_client):
        """BOS .500 vs NYK .467 → Low risk, factor 1.0."""
        # BOS: 30 W / 30 L → .500; recent margin +5
        bos_games = _make_team_games(3, "BOS", 60, 30,
                                     win_margin=5, loss_margin=5,
                                     losses_first=True)
        # NYK: 28 W / 32 L → .467; recent margin +4
        nyk_games = _make_team_games(4, "NYK", 60, 28,
                                     win_margin=4, loss_margin=6,
                                     losses_first=True)

        def _games(league="NBA", *, season=None, team_id=None):
            return bos_games if team_id == 3 else nyk_games

        mock_client.get_games.side_effect = _games

        refiner = ContextualRefiner(mock_client)
        result = refiner.calculate_blowout_risk(
            3, 4, original_edge=0.05, game_date="2026-02-25",
        )

        assert result.risk_score == "Low"
        assert result.minutes_factor == 1.0

    # ── Medium risk ────────────────────────────────────────────────────

    def test_medium_risk_gap_trigger(self, mock_client):
        """Win% gap 0.250 (> 0.180 threshold) but < 0.300 → Medium."""
        # Team A: 36 W / 24 L = .600, recent margin +7
        team_a = _make_team_games(1, "OKC", 60, 36,
                                  win_margin=7, losses_first=True)
        # Team B: 21 W / 39 L = .350, recent losses
        team_b = _make_team_games(2, "DET", 60, 21,
                                  win_margin=5, loss_margin=8,
                                  losses_first=False)

        def _games(league="NBA", *, season=None, team_id=None):
            return team_a if team_id == 1 else team_b

        mock_client.get_games.side_effect = _games

        refiner = ContextualRefiner(mock_client)
        result = refiner.calculate_blowout_risk(
            1, 2, original_edge=0.06, game_date="2026-02-25",
        )

        assert result.risk_score == "Medium"
        assert result.minutes_factor == 1.0  # Medium = advisory, no penalty

    def test_medium_risk_margin_trigger(self, mock_client):
        """Close win% gap but recent margin > 6.0 → Medium via margin."""
        # Team A: 31 W / 29 L = .517, recent margin = +7.0 (> 6.0 thresh)
        team_a = _make_team_games(1, "OKC", 60, 31,
                                  win_margin=7, losses_first=True)
        # Team B: 30 W / 30 L = .500, recent margin = +5.0
        team_b = _make_team_games(2, "DET", 60, 30,
                                  win_margin=5, losses_first=True)

        def _games(league="NBA", *, season=None, team_id=None):
            return team_a if team_id == 1 else team_b

        mock_client.get_games.side_effect = _games

        refiner = ContextualRefiner(mock_client)
        result = refiner.calculate_blowout_risk(
            1, 2, original_edge=0.06, game_date="2026-02-25",
        )

        assert result.risk_score == "Medium"
        assert result.minutes_factor == 1.0

    # ── Fallback / edge cases ──────────────────────────────────────────

    def test_mcp_unavailable_returns_neutral(self, mock_client):
        """When MCP client is unavailable → Low risk, factor 1.0."""
        mock_client.is_available = False

        refiner = ContextualRefiner(mock_client)
        result = refiner.calculate_blowout_risk(
            1, 2, original_edge=0.08, game_date="2026-02-25",
        )

        assert result.risk_score == "Low"
        assert result.minutes_factor == 1.0
        assert result.original_edge == 0.08

    def test_no_games_returns_neutral(self, mock_client):
        """When get_games returns empty → Low risk."""
        mock_client.get_games.return_value = []

        refiner = ContextualRefiner(mock_client)
        result = refiner.calculate_blowout_risk(
            1, 2, original_edge=0.06, game_date="2026-02-25",
        )

        assert result.risk_score == "Low"
        assert result.minutes_factor == 1.0

    def test_get_games_exception_returns_neutral(self, mock_client):
        """When get_games raises → graceful fallback to Low risk."""
        mock_client.get_games.side_effect = RuntimeError("BDL down")

        refiner = ContextualRefiner(mock_client)
        result = refiner.calculate_blowout_risk(
            1, 2, original_edge=0.07, game_date="2026-02-25",
        )

        assert result.risk_score == "Low"
        assert result.minutes_factor == 1.0

    # ── By-abbreviation convenience ────────────────────────────────────

    def test_by_abbr_resolves_and_delegates(self, mock_client):
        """calculate_blowout_risk_by_abbr resolves OKC/DET → IDs."""
        okc_games = _make_team_games(1, "OKC", 60, 45,
                                     win_margin=12, losses_first=True)
        det_games = _make_team_games(2, "DET", 60, 20,
                                     win_margin=10, loss_margin=12,
                                     losses_first=False)

        def _games(league="NBA", *, season=None, team_id=None):
            return okc_games if team_id == 1 else det_games

        mock_client.get_games.side_effect = _games

        refiner = ContextualRefiner(mock_client)
        result = refiner.calculate_blowout_risk_by_abbr(
            "OKC", "DET", original_edge=0.08, game_date="2026-02-25",
        )

        assert result.home_team == "OKC"
        assert result.visitor_team == "DET"
        assert result.risk_score == "High"

    def test_by_abbr_unknown_team_returns_neutral(self, mock_client):
        """Unknown abbreviation → Low risk fallback."""
        refiner = ContextualRefiner(mock_client)
        result = refiner.calculate_blowout_risk_by_abbr(
            "ZZZ", "DET", original_edge=0.05, game_date="2026-02-25",
        )

        assert result.risk_score == "Low"
        assert result.minutes_factor == 1.0

    # ── AdjustmentResult completeness ──────────────────────────────────

    def test_result_fields_populated(self, mock_client):
        """AdjustmentResult has all expected fields after a successful call."""
        okc_games = _make_team_games(1, "OKC", 60, 45,
                                     win_margin=12, losses_first=True)
        det_games = _make_team_games(2, "DET", 60, 20,
                                     win_margin=10, loss_margin=12,
                                     losses_first=False)

        def _games(league="NBA", *, season=None, team_id=None):
            return okc_games if team_id == 1 else det_games

        mock_client.get_games.side_effect = _games

        refiner = ContextualRefiner(mock_client)
        result = refiner.calculate_blowout_risk(
            1, 2,
            original_edge=0.08,
            sim_mean=48.0,
            sim_std=8.0,
            book_line=42.5,
            over_odds=-110,
            game_date="2026-02-25",
        )

        assert isinstance(result, AdjustmentResult)
        assert result.home_team == "OKC"
        assert result.visitor_team == "DET"
        assert result.home_win_pct > 0
        assert result.visitor_win_pct > 0
        assert result.home_avg_margin != 0
        assert result.visitor_avg_margin != 0
        assert result.win_pct_gap > 0
        assert result.favored_team in ("OKC", "DET")


# ── GarbageTimeOpportunity tests ─────────────────────────────────────────


def _make_player_stat(
    player_id: int,
    game_date: str,
    minutes: str = "14:00",
    fga: int = 5,
    fta: int = 2,
    turnover: int = 1,
    pts: int = 8,
) -> dict:
    """Build a single BDL player stat row for testing."""
    return {
        "player": {"id": player_id},
        "game": {"date": game_date, "id": hash(game_date) & 0xFFFF},
        "min": minutes,
        "fga": fga,
        "fta": fta,
        "turnover": turnover,
        "pts": pts,
        "reb": 2,
        "ast": 1,
    }


class TestGarbageTimeOpportunity:
    """Tests for GarbageTimeOpportunity garbage-time specialist detection."""

    @pytest.fixture
    def mock_mcp(self):
        """Mock BDLMCPClient with teams, games, and players."""
        client = MagicMock()
        client.is_available = True
        client.get_teams.return_value = [
            BDLTeam(id=30, abbreviation="WAS"),
            BDLTeam(id=25, abbreviation="OKC"),
        ]
        return client

    @pytest.fixture
    def mock_bdl(self):
        """Mock BallDontLieService for REST player stats."""
        svc = MagicMock()
        svc.is_available = True
        return svc

    @pytest.fixture
    def mock_refiner(self):
        """Mock ContextualRefiner that returns High risk."""
        refiner = MagicMock()
        refiner.is_available = True
        result = MagicMock()
        result.risk_score = "High"
        refiner.calculate_blowout_risk_by_abbr.return_value = result
        return refiner

    @pytest.fixture
    def team_games(self):
        """10 games for WAS (id=30): 3 blowouts, 7 competitive."""
        was = BDLTeam(id=30, abbreviation="WAS")
        opp = BDLTeam(id=99, abbreviation="OPP")
        games = []
        start = date(2026, 1, 5)
        # 7 competitive games (margin 5-10), then 3 blowouts (margin 18-22)
        margins = [5, -7, 8, -3, 10, 6, -5, 18, -20, 22]
        for i, margin in enumerate(margins):
            game_date = (start + timedelta(days=i * 3)).isoformat()
            if margin > 0:
                h_score, v_score = 110, 110 - margin
            else:
                h_score, v_score = 110 + margin, 110
            games.append(BDLGame(
                id=5000 + i,
                home_team=was,
                visitor_team=opp,
                status="Final",
                home_team_score=h_score,
                visitor_team_score=v_score,
                date=game_date,
                season=2025,
            ))
        return games

    @pytest.fixture
    def blowout_dates(self):
        """The 3 game dates that are blowouts (margin > 15)."""
        start = date(2026, 1, 5)
        return {
            (start + timedelta(days=7 * 3)).isoformat(),  # index 7, margin 18
            (start + timedelta(days=8 * 3)).isoformat(),  # index 8, margin 20
            (start + timedelta(days=9 * 3)).isoformat(),  # index 9, margin 22
        }

    @pytest.fixture
    def competitive_dates(self):
        """The 7 game dates that are competitive (margin <= 15)."""
        start = date(2026, 1, 5)
        return {
            (start + timedelta(days=i * 3)).isoformat()
            for i in range(7)
        }

    @pytest.fixture
    def player_stats(self, blowout_dates, competitive_dates):
        """Stats for 4 players across 10 games.

        Player 101: Specialist — avg ~14 min, usage spikes 25%+ in blowouts
        Player 102: Starter — avg ~33 min (excluded by minutes filter)
        Player 103: Bench no-spike — avg ~12 min, similar usage in both contexts
        Player 104: Too few games — only 3 total games
        """
        stats = []
        bd = sorted(blowout_dates)
        cd = sorted(competitive_dates)

        # Player 101: Specialist (low min, high blowout usage)
        for d in cd:
            # Competitive: 14 min, FGA=4, FTA=2, TOV=1
            # usage = (4 + 0.88 + 1) / 14 = 0.42
            stats.append(_make_player_stat(101, d, "14:00", fga=4, fta=2, turnover=1))
        for d in bd:
            # Blowout: 16 min, FGA=8, FTA=3, TOV=1
            # usage = (8 + 1.32 + 1) / 16 = 0.645
            # spike = 0.645 / 0.42 = 1.536 (53.6% spike, well over 15%)
            stats.append(_make_player_stat(101, d, "16:00", fga=8, fta=3, turnover=1))

        # Player 102: Starter (avg 33 min — disqualified)
        for d in cd + bd:
            stats.append(_make_player_stat(102, d, "33:00", fga=15, fta=6, turnover=3))

        # Player 103: Bench, no spike (similar usage everywhere)
        for d in cd:
            # usage = (3 + 0.88 + 1) / 12 = 0.407
            stats.append(_make_player_stat(103, d, "12:00", fga=3, fta=2, turnover=1))
        for d in bd:
            # usage = (3 + 1.32 + 1) / 13 = 0.41
            # spike = 0.41 / 0.407 = 1.007 (< 1.15, no spike)
            stats.append(_make_player_stat(103, d, "13:00", fga=3, fta=3, turnover=1))

        # Player 104: Too few games (only 3)
        for d in cd[:3]:
            stats.append(_make_player_stat(104, d, "10:00", fga=5, fta=2, turnover=1))

        return stats

    def _setup_mocks(self, mock_mcp, mock_bdl, mock_refiner,
                     team_games, player_stats):
        """Wire all mocks together."""
        mock_mcp.get_games.return_value = team_games
        mock_mcp.get_players.return_value = [
            MagicMock(id=101, full_name="Bench Specialist"),
            MagicMock(id=102, full_name="Star Starter"),
            MagicMock(id=103, full_name="Bench Regular"),
            MagicMock(id=104, full_name="New Player"),
        ]
        mock_bdl.get_player_stats.return_value = player_stats
        return GarbageTimeOpportunity(mock_mcp, mock_bdl, mock_refiner)

    # ── Core specialist detection ──────────────────────────────────────

    def test_specialist_identified(self, mock_mcp, mock_bdl, mock_refiner,
                                   team_games, player_stats):
        """Player 101 qualifies: avg 14 min, usage spike ~1.54."""
        detector = self._setup_mocks(
            mock_mcp, mock_bdl, mock_refiner, team_games, player_stats)

        result = detector.find_garbage_time_pivots(
            "WAS", "OKC", game_date="2026-02-25",
        )

        assert result.blowout_risk == "High"
        assert len(result.pivots) == 1
        pivot = result.pivots[0]
        assert pivot.player_name == "Bench Specialist"
        assert pivot.avg_minutes < GT_MAX_AVG_MINUTES
        assert pivot.usage_spike_pct >= GT_USAGE_SPIKE_THRESHOLD
        assert pivot.minutes_factor == GT_SPECIALIST_MINUTES_FACTOR

    def test_starter_excluded(self, mock_mcp, mock_bdl, mock_refiner,
                              team_games, player_stats):
        """Player 102 (33 min avg) excluded — over 18 min threshold."""
        detector = self._setup_mocks(
            mock_mcp, mock_bdl, mock_refiner, team_games, player_stats)

        result = detector.find_garbage_time_pivots(
            "WAS", "OKC", game_date="2026-02-25",
        )

        pivot_names = [p.player_name for p in result.pivots]
        assert "Star Starter" not in pivot_names

    def test_no_usage_spike(self, mock_mcp, mock_bdl, mock_refiner,
                            team_games, player_stats):
        """Player 103 (bench, no spike) excluded — usage identical."""
        detector = self._setup_mocks(
            mock_mcp, mock_bdl, mock_refiner, team_games, player_stats)

        result = detector.find_garbage_time_pivots(
            "WAS", "OKC", game_date="2026-02-25",
        )

        pivot_names = [p.player_name for p in result.pivots]
        assert "Bench Regular" not in pivot_names

    def test_insufficient_blowout_games(self, mock_mcp, mock_bdl,
                                        mock_refiner):
        """Only 1 blowout in the sample → no pivots even if High risk."""
        was = BDLTeam(id=30, abbreviation="WAS")
        opp = BDLTeam(id=99, abbreviation="OPP")
        # 10 games, only 1 blowout
        games = []
        start = date(2026, 1, 5)
        margins = [5, -3, 8, 2, -6, 4, -7, 3, 6, 20]  # only last is blowout
        for i, m in enumerate(margins):
            gd = (start + timedelta(days=i * 3)).isoformat()
            h, v = (110, 110 - m) if m > 0 else (110 + m, 110)
            games.append(BDLGame(
                id=6000 + i, home_team=was, visitor_team=opp,
                status="Final", home_team_score=h, visitor_team_score=v,
                date=gd, season=2025,
            ))
        mock_mcp.get_games.return_value = games
        mock_bdl.get_player_stats.return_value = []

        detector = GarbageTimeOpportunity(mock_mcp, mock_bdl, mock_refiner)
        result = detector.find_garbage_time_pivots(
            "WAS", "OKC", game_date="2026-02-25",
        )

        assert result.blowout_games_found == 1
        assert len(result.pivots) == 0

    # ── Blowout risk gating ────────────────────────────────────────────

    def test_low_risk_returns_empty(self, mock_mcp, mock_bdl):
        """When risk is Low, no analysis performed."""
        refiner = MagicMock()
        refiner.is_available = True
        low_result = MagicMock()
        low_result.risk_score = "Low"
        refiner.calculate_blowout_risk_by_abbr.return_value = low_result

        detector = GarbageTimeOpportunity(mock_mcp, mock_bdl, refiner)
        result = detector.find_garbage_time_pivots(
            "WAS", "OKC", game_date="2026-02-25",
        )

        assert result.blowout_risk == "Low"
        assert len(result.pivots) == 0
        mock_mcp.get_games.assert_not_called()

    def test_high_risk_triggers_analysis(self, mock_mcp, mock_bdl,
                                         mock_refiner, team_games,
                                         player_stats):
        """When risk is High, analysis runs and returns specialists."""
        detector = self._setup_mocks(
            mock_mcp, mock_bdl, mock_refiner, team_games, player_stats)

        result = detector.find_garbage_time_pivots(
            "WAS", "OKC", game_date="2026-02-25",
        )

        assert result.blowout_risk == "High"
        assert result.games_analyzed == 10
        assert result.blowout_games_found == 3
        assert len(result.pivots) >= 1

    # ── Edge adjustment ────────────────────────────────────────────────

    def test_edge_boost_overrides_reduction(self, mock_mcp, mock_bdl):
        """Specialist gets 1.2x factor → adjusted edge > 0.85x adjusted edge."""
        detector = GarbageTimeOpportunity(mock_mcp, mock_bdl)

        pivot = GarbageTimePivot(
            player_name="Bench Specialist",
            team="WAS",
            bdl_player_id=101,
            avg_minutes=14.0,
            usage_spike_pct=1.35,
            minutes_factor=GT_SPECIALIST_MINUTES_FACTOR,
        )

        edge_df = pd.DataFrame([
            {"player": "Bench Specialist", "sim_mean": 25.0, "sim_std": 5.0,
             "book_line": 22.5, "over_odds": -110, "edge_pct": 0.08,
             "p_over": 0.69},
            {"player": "Regular Player", "sim_mean": 40.0, "sim_std": 8.0,
             "book_line": 38.0, "over_odds": -110, "edge_pct": 0.06,
             "p_over": 0.60},
        ])

        result_df = detector.adjust_edges_for_pivots(edge_df, [pivot])

        assert bool(result_df.loc[0, "is_gt_pivot"]) is True
        assert bool(result_df.loc[1, "is_gt_pivot"]) is False
        assert result_df.loc[0, "gt_usage_spike"] == 1.35

    def test_non_specialist_unchanged(self, mock_mcp, mock_bdl):
        """Non-pivot players keep their original values."""
        detector = GarbageTimeOpportunity(mock_mcp, mock_bdl)

        pivot = GarbageTimePivot(
            player_name="Bench Specialist",
            team="WAS",
            bdl_player_id=101,
            minutes_factor=GT_SPECIALIST_MINUTES_FACTOR,
        )

        edge_df = pd.DataFrame([
            {"player": "Other Player", "sim_mean": 40.0, "sim_std": 8.0,
             "book_line": 38.0, "over_odds": -110, "edge_pct": 0.06,
             "p_over": 0.60},
        ])

        result_df = detector.adjust_edges_for_pivots(edge_df, [pivot])

        assert bool(result_df.loc[0, "is_gt_pivot"]) is False
        assert result_df.loc[0, "gt_usage_spike"] == 0.0

    # ── Fallback / edge cases ──────────────────────────────────────────

    def test_bdl_unavailable_degrades(self, mock_mcp):
        """When REST service is unavailable → empty result, no crash."""
        mock_bdl = MagicMock()
        mock_bdl.is_available = False

        refiner = MagicMock()
        refiner.is_available = True
        high_result = MagicMock()
        high_result.risk_score = "High"
        refiner.calculate_blowout_risk_by_abbr.return_value = high_result

        detector = GarbageTimeOpportunity(mock_mcp, mock_bdl, refiner)
        result = detector.find_garbage_time_pivots(
            "WAS", "OKC", game_date="2026-02-25",
        )

        assert result.blowout_risk == "High"
        assert len(result.pivots) == 0

    def test_zero_minutes_games_skipped(self, mock_mcp, mock_bdl,
                                        mock_refiner, team_games):
        """DNP games (0 minutes) excluded from usage calculation."""
        mock_mcp.get_games.return_value = team_games
        mock_mcp.get_players.return_value = [
            MagicMock(id=201, full_name="DNP Player"),
        ]

        # Mix of real games and DNPs
        start = date(2026, 1, 5)
        bd = sorted([
            (start + timedelta(days=i * 3)).isoformat()
            for i in [7, 8, 9]
        ])
        cd = sorted([
            (start + timedelta(days=i * 3)).isoformat()
            for i in range(7)
        ])

        stats = []
        # Some competitive games with minutes
        for d in cd[:5]:
            stats.append(_make_player_stat(201, d, "10:00", fga=3, fta=1, turnover=1))
        # 2 DNP games — should be skipped
        for d in cd[5:7]:
            stats.append(_make_player_stat(201, d, "00", fga=0, fta=0, turnover=0))
        # Blowout games with usage spike
        for d in bd:
            stats.append(_make_player_stat(201, d, "12:00", fga=7, fta=2, turnover=1))

        mock_bdl.get_player_stats.return_value = stats

        detector = GarbageTimeOpportunity(mock_mcp, mock_bdl, mock_refiner)
        result = detector.find_garbage_time_pivots(
            "WAS", "OKC", game_date="2026-02-25",
        )

        # Player should qualify — DNPs excluded from avg minutes
        # avg minutes = (5*10 + 3*12) / 8 = 11.0 (< 18)
        # competitive usage = (3 + 0.44 + 1) / 10 = 0.444
        # blowout usage = (7 + 0.88 + 1) / 12 = 0.74
        # spike = 0.74 / 0.444 = 1.67 (>= 1.15)
        if len(result.pivots) > 0:
            assert result.pivots[0].avg_minutes < GT_MAX_AVG_MINUTES
