"""Tests for UnderdogPickemService — edge detection and entry building."""

import pytest
from unittest.mock import MagicMock

from app.models.underdog import (
    FLEX_PAYOUT,
    POWER_PLAY_PAYOUT,
    PickemComparison,
    PickemEntry,
    UnderdogPickemLine,
    UnderdogPickemResponse,
    UnderdogSlate,
)
from app.services.underdog_api_service import UnderdogApiService
from app.services.underdog_pickem_service import (
    EDGE_MODERATE_PCT,
    EDGE_SLIGHT_PCT,
    EDGE_STRONG_PCT,
    SKIP_BELOW_PCT,
    UnderdogPickemService,
)


# ── Fixtures ──────────────────────────────────────────────────────────


def _make_ud_line(player, team, stat, line):
    return UnderdogPickemLine(
        player_name=player,
        team_abbreviation=team,
        stat=stat,
        line=line,
        appearance_id=f"a_{player.lower().replace(' ', '_')}_{stat}",
    )


def _make_slate(lines):
    return UnderdogSlate(
        game_date="2026-02-19",
        lines=lines,
        player_count=len({l.player_name for l in lines}),
    )


SAMPLE_UD_LINES = [
    _make_ud_line("LeBron James", "LAL", "pts", 25.5),
    _make_ud_line("LeBron James", "LAL", "reb", 8.5),
    _make_ud_line("LeBron James", "LAL", "ast", 7.5),
    _make_ud_line("LeBron James", "LAL", "pra", 41.5),
    _make_ud_line("Stephen Curry", "GSW", "pts", 28.5),
    _make_ud_line("Stephen Curry", "GSW", "fg3m", 5.5),
    _make_ud_line("Stephen Curry", "GSW", "fantasy_points", 42.0),
    _make_ud_line("Nikola Jokic", "DEN", "pts", 26.5),
    _make_ud_line("Nikola Jokic", "DEN", "reb", 12.5),
    _make_ud_line("Nikola Jokic", "DEN", "ast", 9.5),
    _make_ud_line("Nikola Jokic", "DEN", "pra", 48.5),
    _make_ud_line("Nikola Jokic", "DEN", "fantasy_points", 55.0),
    _make_ud_line("Jayson Tatum", "BOS", "pts", 27.5),
    _make_ud_line("Jayson Tatum", "BOS", "reb", 8.5),
    _make_ud_line("Anthony Edwards", "MIN", "pts", 25.5),
    _make_ud_line("Anthony Edwards", "MIN", "stl_blk", 2.5),
]

SAMPLE_PROJECTIONS = {
    "lebron james:LAL": {
        "pts": 28.0,  # +2.5 over 25.5 = +9.8%
        "reb": 8.0,   # -0.5 under 8.5 = -5.9%
        "ast": 8.2,   # +0.7 over 7.5 = +9.3%
        "pra": 44.2,  # pts+reb+ast
        "fg3m": 1.5,
        "stl": 1.0,
        "blk": 0.5,
        "tov": 3.5,
        "fd_points": 50.0,
    },
    "stephen curry:GSW": {
        "pts": 30.0,  # +1.5 over 28.5 = +5.3%
        "reb": 5.0,
        "ast": 6.5,
        "fg3m": 6.5,  # +1.0 over 5.5 = +18.2%
        "stl": 1.0,
        "blk": 0.3,
        "tov": 3.0,
        "fd_points": 45.0,  # +3.0 over 42.0 = +7.1%
    },
    "nikola jokic:DEN": {
        "pts": 26.0,  # -0.5 under 26.5 = -1.9% (SKIP)
        "reb": 13.0,  # +0.5 over 12.5 = +4.0% (slight)
        "ast": 10.0,  # +0.5 over 9.5 = +5.3%
        "pra": 49.0,  # +0.5 over 48.5 = +1.0% (SKIP)
        "fd_points": 58.0,  # +3.0 over 55.0 = +5.5%
    },
    "jayson tatum:BOS": {
        "pts": 29.0,  # +1.5 over 27.5 = +5.5%
        "reb": 9.5,   # +1.0 over 8.5 = +11.8%
    },
    "anthony edwards:MIN": {
        "pts": 30.0,  # +4.5 over 25.5 = +17.6% (strong!)
        "stl": 1.5,
        "blk": 1.5,
        "stl_blk": 3.0,  # +0.5 over 2.5 = +20%
    },
}


def _create_service(lines=None):
    """Create a pickem service with a mocked UD API."""
    mock_api = MagicMock(spec=UnderdogApiService)
    mock_api.get_pickem_lines.return_value = _make_slate(lines or SAMPLE_UD_LINES)
    mock_api.get_player_line.side_effect = lambda name, stat, gd=None: next(
        (l for l in (lines or SAMPLE_UD_LINES) if l.player_name.lower() == name.lower() and l.stat == stat),
        None,
    )
    mock_api.get_player_lines.side_effect = lambda name, gd=None: [
        l for l in (lines or SAMPLE_UD_LINES) if l.player_name.lower() == name.lower()
    ]
    return UnderdogPickemService(underdog_api=mock_api)


# ── Tests: Edge classification ────────────────────────────────────────


class TestEdgeClassification:
    def test_strong_edge(self):
        svc = _create_service()
        result = svc._classify_edge(15.0)
        assert result == "strong"

    def test_moderate_edge(self):
        svc = _create_service()
        result = svc._classify_edge(9.0)
        assert result == "moderate"

    def test_slight_edge(self):
        svc = _create_service()
        result = svc._classify_edge(4.0)
        assert result == "slight"

    def test_no_edge(self):
        svc = _create_service()
        result = svc._classify_edge(1.5)
        assert result == "no_edge"

    def test_exact_threshold_strong(self):
        svc = _create_service()
        result = svc._classify_edge(EDGE_STRONG_PCT)
        assert result == "strong"

    def test_exact_threshold_moderate(self):
        svc = _create_service()
        result = svc._classify_edge(EDGE_MODERATE_PCT)
        assert result == "moderate"


# ── Tests: Recommendation logic ───────────────────────────────────────


class TestRecommendation:
    def test_over_recommendation(self):
        svc = _create_service()
        rec = svc._recommend(10.0, 0.7)
        assert rec == "OVER"

    def test_under_recommendation(self):
        svc = _create_service()
        rec = svc._recommend(-10.0, 0.7)
        assert rec == "UNDER"

    def test_skip_marginal(self):
        svc = _create_service()
        rec = svc._recommend(1.5, 0.3)
        assert rec == "SKIP"

    def test_skip_zero(self):
        svc = _create_service()
        rec = svc._recommend(0.0, 0.3)
        assert rec == "SKIP"


# ── Tests: Confidence scoring ─────────────────────────────────────────


class TestConfidence:
    def test_low_delta_low_confidence(self):
        svc = _create_service()
        conf = svc._compute_confidence(1.0)
        assert conf < 0.5

    def test_medium_delta_medium_confidence(self):
        svc = _create_service()
        conf = svc._compute_confidence(10.0)
        assert 0.5 <= conf <= 0.85

    def test_high_delta_high_confidence(self):
        svc = _create_service()
        conf = svc._compute_confidence(25.0)
        assert conf >= 0.75

    def test_confidence_capped(self):
        svc = _create_service()
        conf = svc._compute_confidence(100.0)
        assert conf <= 0.95

    def test_negative_delta_same_confidence(self):
        svc = _create_service()
        conf_pos = svc._compute_confidence(10.0)
        conf_neg = svc._compute_confidence(-10.0)
        assert conf_pos == conf_neg  # Absolute value used


# ── Tests: Stat projection extraction ─────────────────────────────────


class TestProjectedStatExtraction:
    def test_direct_stat(self):
        svc = _create_service()
        val = svc._get_projected_stat({"pts": 25.0}, "pts")
        assert val == 25.0

    def test_composite_pra(self):
        svc = _create_service()
        val = svc._get_projected_stat({"pts": 20, "reb": 10, "ast": 5}, "pra")
        assert val == 35.0

    def test_composite_stl_blk(self):
        svc = _create_service()
        val = svc._get_projected_stat({"stl": 1.5, "blk": 0.5}, "stl_blk")
        assert val == 2.0

    def test_fantasy_points(self):
        svc = _create_service()
        val = svc._get_projected_stat({"fd_points": 45.0}, "fantasy_points")
        assert val == 45.0

    def test_missing_stat_returns_none(self):
        svc = _create_service()
        val = svc._get_projected_stat({"pts": 25.0}, "reb")
        assert val is None

    def test_pra_partial_stats(self):
        svc = _create_service()
        val = svc._get_projected_stat({"pts": 20, "reb": 0, "ast": 0}, "pra")
        assert val == 20.0


# ── Tests: get_all_edges ──────────────────────────────────────────────


class TestGetAllEdges:
    def test_returns_response(self):
        svc = _create_service()
        result = svc.get_all_edges("2026-02-19", SAMPLE_PROJECTIONS)
        assert isinstance(result, UnderdogPickemResponse)
        assert result.game_date == "2026-02-19"
        assert result.total_lines > 0

    def test_sorted_by_abs_delta(self):
        svc = _create_service()
        result = svc.get_all_edges("2026-02-19", SAMPLE_PROJECTIONS)
        deltas = [abs(l.delta_pct) for l in result.lines]
        assert deltas == sorted(deltas, reverse=True)

    def test_edge_counts(self):
        svc = _create_service()
        result = svc.get_all_edges("2026-02-19", SAMPLE_PROJECTIONS)
        # Should have at least some edges
        assert result.strong_edges + result.moderate_edges + result.slight_edges >= 0

    def test_lebron_pts_is_over(self):
        """LeBron: our 28.0 vs UD 25.5 = +9.8% → OVER, moderate edge."""
        svc = _create_service()
        result = svc.get_all_edges("2026-02-19", SAMPLE_PROJECTIONS)
        lebron_pts = [l for l in result.lines if "LeBron" in l.player_name and l.stat == "pts"]
        assert len(lebron_pts) == 1
        assert lebron_pts[0].recommendation == "OVER"
        assert lebron_pts[0].delta > 0

    def test_edwards_pts_strong_edge(self):
        """Edwards: our 30.0 vs UD 25.5 = +17.6% → strong edge."""
        svc = _create_service()
        result = svc.get_all_edges("2026-02-19", SAMPLE_PROJECTIONS)
        edwards_pts = [l for l in result.lines if "Edwards" in l.player_name and l.stat == "pts"]
        assert len(edwards_pts) == 1
        assert edwards_pts[0].edge_strength == "strong"

    def test_jokic_pts_skip(self):
        """Jokic PTS: our 26.0 vs UD 26.5 = -1.9% → SKIP."""
        svc = _create_service()
        result = svc.get_all_edges("2026-02-19", SAMPLE_PROJECTIONS)
        jokic_pts = [l for l in result.lines if "Jokic" in l.player_name and l.stat == "pts"]
        assert len(jokic_pts) == 1
        assert jokic_pts[0].recommendation == "SKIP"

    def test_no_projections_returns_empty(self):
        svc = _create_service()
        result = svc.get_all_edges("2026-02-19", {})
        assert result.total_lines == 0

    def test_avg_abs_delta(self):
        svc = _create_service()
        result = svc.get_all_edges("2026-02-19", SAMPLE_PROJECTIONS)
        assert result.avg_abs_delta_pct >= 0


# ── Tests: get_player_edge ────────────────────────────────────────────


class TestGetPlayerEdge:
    def test_returns_comparison(self):
        svc = _create_service()
        edge = svc.get_player_edge("LeBron James", "pts", 28.0, "2026-02-19")
        assert isinstance(edge, PickemComparison)
        assert edge.our_projection == 28.0
        assert edge.ud_line == 25.5

    def test_not_found(self):
        svc = _create_service()
        edge = svc.get_player_edge("Nobody", "pts", 20.0, "2026-02-19")
        assert edge is None


# ── Tests: build_optimal_entry ────────────────────────────────────────


class TestBuildOptimalEntry:
    def _get_edges(self):
        svc = _create_service()
        result = svc.get_all_edges("2026-02-19", SAMPLE_PROJECTIONS)
        return svc, result.lines

    def test_max_edge_strategy(self):
        svc, edges = self._get_edges()
        entry = svc.build_optimal_entry(edges, num_picks=3, strategy="max_edge")
        assert isinstance(entry, PickemEntry)
        assert entry.num_picks == 3
        assert len(entry.picks) == 3

    def test_diversified_strategy(self):
        svc, edges = self._get_edges()
        entry = svc.build_optimal_entry(edges, num_picks=4, strategy="diversified")
        assert entry.num_picks == 4
        # Should have diverse stats
        stats = {p.stat for p in entry.picks}
        assert len(stats) >= 2  # At least 2 different stat categories

    def test_correlated_strategy(self):
        svc, edges = self._get_edges()
        entry = svc.build_optimal_entry(edges, num_picks=3, strategy="correlated")
        assert entry.num_picks == 3

    def test_payout_multiplier_flex(self):
        svc, edges = self._get_edges()
        entry = svc.build_optimal_entry(edges, num_picks=5, entry_type="flex")
        assert entry.payout_multiplier == FLEX_PAYOUT[5]

    def test_payout_multiplier_power_play(self):
        svc, edges = self._get_edges()
        entry = svc.build_optimal_entry(edges, num_picks=5, entry_type="power_play")
        assert entry.payout_multiplier == POWER_PLAY_PAYOUT[5]

    def test_min_picks_clamped(self):
        svc, edges = self._get_edges()
        entry = svc.build_optimal_entry(edges, num_picks=1)  # Below minimum
        assert entry.num_picks == 2

    def test_max_picks_clamped(self):
        svc, edges = self._get_edges()
        entry = svc.build_optimal_entry(edges, num_picks=10)  # Above maximum
        assert entry.num_picks <= 6

    def test_correlation_score(self):
        svc, edges = self._get_edges()
        entry = svc.build_optimal_entry(edges, num_picks=3)
        assert 0.0 <= entry.correlation_score <= 1.0

    def test_expected_hit_rate(self):
        svc, edges = self._get_edges()
        entry = svc.build_optimal_entry(edges, num_picks=3)
        assert 0.0 <= entry.expected_hit_rate <= 1.0

    def test_empty_edges(self):
        svc = _create_service()
        entry = svc.build_optimal_entry([], num_picks=3)
        assert entry.num_picks == 0
        assert entry.picks == []

    def test_diversified_no_duplicate_players(self):
        svc, edges = self._get_edges()
        entry = svc.build_optimal_entry(edges, num_picks=5, strategy="diversified")
        player_names = [p.player_name for p in entry.picks]
        # Check no duplicate players
        assert len(player_names) == len(set(player_names))


# ── Tests: Pydantic model payout tables ───────────────────────────────


class TestPayoutTables:
    def test_flex_payouts(self):
        assert FLEX_PAYOUT[2] == 3.0
        assert FLEX_PAYOUT[3] == 6.0
        assert FLEX_PAYOUT[5] == 20.0
        assert FLEX_PAYOUT[6] == 40.0

    def test_power_play_payouts(self):
        assert POWER_PLAY_PAYOUT[2] == 6.0
        assert POWER_PLAY_PAYOUT[5] == 50.0
        assert POWER_PLAY_PAYOUT[6] == 100.0

    def test_power_play_higher_than_flex(self):
        for n in range(2, 7):
            assert POWER_PLAY_PAYOUT[n] > FLEX_PAYOUT[n]
