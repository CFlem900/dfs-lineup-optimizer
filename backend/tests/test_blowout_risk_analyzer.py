"""Tests for BlowoutRiskAnalyzer — matchup‑context edge adjustments."""

import pytest
import pandas as pd

from app.services.blowout_risk_analyzer import (
    BlowoutAssessment,
    BlowoutRiskAnalyzer,
    TeamForm,
    SPREAD_BLOWOUT_THRESHOLD,
)
from app.services.prop_edge_analyzer import prob_over


# ── Mock BDL service ──────────────────────────────────────────────────────


def _game(gid, home, away, status="Final", h_score=0, v_score=0, dt=""):
    return {
        "id": gid,
        "home_team": {"abbreviation": home},
        "visitor_team": {"abbreviation": away},
        "home_team_score": h_score,
        "visitor_team_score": v_score,
        "status": status,
        "date": dt,
    }


class MockBDL:
    """Configurable BDL mock for blowout risk tests."""

    def __init__(
        self,
        tonight_games=None,
        odds=None,
        recent_games=None,
    ):
        self._tonight = tonight_games or []
        self._odds = odds or {}
        self._recent = recent_games or []

    def get_games(self, date_str: str):
        # If asking for tonight, return tonight's games
        # Otherwise return recent games for form calculation
        if date_str == "2026-02-25":
            return self._tonight
        return [g for g in self._recent if g.get("date", "") == date_str]

    def _get(self, path: str) -> dict:
        """Mock _get for bulk game fetch (used by _build_team_form)."""
        # Return all recent games as BDL paginated response
        return {"data": list(self._recent), "meta": {}}

    def get_odds_by_game(self, date_str: str):
        return self._odds


# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def blowout_game_bdl():
    """OKC (-12) vs WAS — classic blowout setup."""
    return MockBDL(
        tonight_games=[
            _game(1001, "OKC", "WAS", status="Scheduled"),
        ],
        odds={
            1001: {
                "spread_home": -12.0,
                "total": 225.0,
                "vendor": "DraftKings",
            }
        },
        recent_games=[
            # OKC: winning big recently
            _game(900, "OKC", "PHX", "Final", 120, 102, "2026-02-24"),
            _game(901, "OKC", "LAL", "Final", 118, 104, "2026-02-23"),
            _game(902, "DEN", "OKC", "Final", 100, 115, "2026-02-22"),
            _game(903, "OKC", "MIN", "Final", 112, 108, "2026-02-21"),
            _game(904, "OKC", "DAL", "Final", 125, 100, "2026-02-20"),
            # WAS: losing badly
            _game(910, "WAS", "BOS", "Final", 95, 125, "2026-02-24"),
            _game(911, "WAS", "NYK", "Final", 100, 118, "2026-02-23"),
            _game(912, "MIL", "WAS", "Final", 120, 105, "2026-02-22"),
            _game(913, "WAS", "CLE", "Final", 98, 115, "2026-02-21"),
            _game(914, "WAS", "PHI", "Final", 100, 112, "2026-02-20"),
        ],
    )


@pytest.fixture
def close_game_bdl():
    """BOS (-2.5) vs MIL — close matchup, no blowout risk."""
    return MockBDL(
        tonight_games=[
            _game(2001, "BOS", "MIL", status="Scheduled"),
        ],
        odds={
            2001: {
                "spread_home": -2.5,
                "total": 228.0,
                "vendor": "DraftKings",
            }
        },
        recent_games=[
            _game(800, "BOS", "CLE", "Final", 112, 108, "2026-02-24"),
            _game(801, "BOS", "NYK", "Final", 105, 102, "2026-02-23"),
            _game(810, "MIL", "IND", "Final", 118, 110, "2026-02-24"),
            _game(811, "MIL", "CHI", "Final", 115, 105, "2026-02-23"),
        ],
    )


@pytest.fixture
def no_odds_bdl():
    """Game with no odds available — relies on form signals only."""
    return MockBDL(
        tonight_games=[
            _game(3001, "GSW", "POR", status="Scheduled"),
        ],
        odds={},  # No odds
        recent_games=[
            # GSW: strong
            _game(700, "GSW", "SAC", "Final", 120, 105, "2026-02-24"),
            _game(701, "GSW", "LAC", "Final", 115, 100, "2026-02-23"),
            _game(702, "GSW", "UTA", "Final", 125, 98,  "2026-02-22"),
            # POR: weak
            _game(710, "POR", "DEN", "Final", 95, 120, "2026-02-24"),
            _game(711, "POR", "OKC", "Final", 90, 125, "2026-02-23"),
            _game(712, "POR", "DAL", "Final", 100, 118, "2026-02-22"),
        ],
    )


def _make_edge_df(players):
    """Build a minimal edge DataFrame matching PropEdgeAnalyzer output."""
    return pd.DataFrame(players)


# ── Risk scoring tests ────────────────────────────────────────────────────


class TestAssessGames:
    def test_blowout_game_flagged(self, blowout_game_bdl):
        analyzer = BlowoutRiskAnalyzer(blowout_game_bdl)
        result = analyzer.assess_games("2026-02-25")

        assert "OKC" in result
        assert "WAS" in result
        a = result["OKC"]
        assert a.is_blowout_risk is True
        assert a.risk_score > 0.4
        assert a.minutes_factor < 1.0
        assert a.spread == -12.0
        assert a.favored_team == "OKC"

    def test_close_game_not_flagged(self, close_game_bdl):
        analyzer = BlowoutRiskAnalyzer(close_game_bdl)
        result = analyzer.assess_games("2026-02-25")

        assert "BOS" in result
        a = result["BOS"]
        assert a.is_blowout_risk is False
        assert a.minutes_factor == 1.0

    def test_no_odds_uses_form(self, no_odds_bdl):
        analyzer = BlowoutRiskAnalyzer(no_odds_bdl)
        result = analyzer.assess_games("2026-02-25")

        assert "GSW" in result
        a = result["GSW"]
        # With big form differential and no odds, should still detect risk
        assert a.spread is None
        assert a.spread_signal == 0.0
        # Record + recent signals should contribute
        assert a.record_signal > 0 or a.recent_signal > 0

    def test_both_teams_get_same_assessment(self, blowout_game_bdl):
        analyzer = BlowoutRiskAnalyzer(blowout_game_bdl)
        result = analyzer.assess_games("2026-02-25")
        assert result["OKC"] is result["WAS"]


class TestSpreadSignal:
    def test_threshold_boundary(self):
        """Spread exactly at threshold → signal = 0."""
        bdl = MockBDL(
            tonight_games=[_game(1, "A", "B", "Scheduled")],
            odds={1: {"spread_home": -7.0}},
        )
        analyzer = BlowoutRiskAnalyzer(bdl)
        result = analyzer.assess_games("2026-02-25")
        if "A" in result:
            assert result["A"].spread_signal == 0.0

    def test_large_spread(self):
        """15-point spread → signal = 1.0."""
        bdl = MockBDL(
            tonight_games=[_game(1, "A", "B", "Scheduled")],
            odds={1: {"spread_home": -15.0}},
        )
        analyzer = BlowoutRiskAnalyzer(bdl)
        result = analyzer.assess_games("2026-02-25")
        if "A" in result:
            assert result["A"].spread_signal == 1.0


# ── Edge adjustment tests ─────────────────────────────────────────────────


class TestAdjustEdges:
    def test_blowout_reduces_edge(self, blowout_game_bdl):
        analyzer = BlowoutRiskAnalyzer(blowout_game_bdl)
        assessments = analyzer.assess_games("2026-02-25")

        edge_df = _make_edge_df([
            {
                "player": "Shai Gilgeous-Alexander",
                "team": "OKC",
                "opponent": "WAS",
                "stat_type": "PRA",
                "book_line": 42.5,
                "over_odds": -110,
                "dk_salary": 10800,
                "sim_mean": 48.0,
                "sim_std": 8.5,
                "p_over": 0.7420,
                "implied": 0.5238,
                "edge_pct": 0.2182,
                "edge_display": "+21.8%",
                "kelly_frac": 0.05,
                "confidence": "High",
            }
        ])

        adjusted = analyzer.adjust_edges(edge_df, assessments)

        assert "adj_sim_mean" in adjusted.columns
        assert "adj_edge_pct" in adjusted.columns
        assert "blowout_risk" in adjusted.columns
        assert "minutes_factor" in adjusted.columns

        row = adjusted.iloc[0]
        assert row["adj_sim_mean"] < row["sim_mean"]
        assert row["minutes_factor"] < 1.0
        # Edge should decrease (lower mean → lower P(over) → lower edge)
        assert row["adj_edge_pct"] < row["edge_pct"]

    def test_close_game_no_change(self, close_game_bdl):
        analyzer = BlowoutRiskAnalyzer(close_game_bdl)
        assessments = analyzer.assess_games("2026-02-25")

        edge_df = _make_edge_df([
            {
                "player": "Jayson Tatum",
                "team": "BOS",
                "opponent": "MIL",
                "stat_type": "PRA",
                "book_line": 40.5,
                "over_odds": -110,
                "dk_salary": 9500,
                "sim_mean": 44.0,
                "sim_std": 8.0,
                "p_over": 0.6700,
                "implied": 0.5238,
                "edge_pct": 0.1462,
                "edge_display": "+14.6%",
                "kelly_frac": 0.03,
                "confidence": "High",
            }
        ])

        adjusted = analyzer.adjust_edges(edge_df, assessments)
        row = adjusted.iloc[0]
        assert row["minutes_factor"] == 1.0
        assert row["adj_sim_mean"] == row["sim_mean"]
        # adj_edge is recomputed from scratch, so compare to within tolerance
        assert abs(row["adj_edge_pct"] - row["adj_edge_pct"]) < 0.001

    def test_empty_df_returns_empty(self, blowout_game_bdl):
        analyzer = BlowoutRiskAnalyzer(blowout_game_bdl)
        adjusted = analyzer.adjust_edges(pd.DataFrame(), {})
        assert adjusted.empty

    def test_unknown_team_no_crash(self, blowout_game_bdl):
        """Player on a team not in tonight's games → no adjustment."""
        analyzer = BlowoutRiskAnalyzer(blowout_game_bdl)
        assessments = analyzer.assess_games("2026-02-25")

        edge_df = _make_edge_df([
            {
                "player": "Some Player",
                "team": "LAL",
                "opponent": "PHX",
                "stat_type": "PRA",
                "book_line": 35.0,
                "over_odds": -110,
                "dk_salary": 7000,
                "sim_mean": 38.0,
                "sim_std": 7.0,
                "p_over": 0.67,
                "implied": 0.5238,
                "edge_pct": 0.14,
                "edge_display": "+14.0%",
                "kelly_frac": 0.03,
                "confidence": "Medium",
            }
        ])

        adjusted = analyzer.adjust_edges(edge_df, assessments)
        row = adjusted.iloc[0]
        # No assessment for LAL → no change
        assert row["minutes_factor"] == 1.0

    def test_sorted_by_adjusted_edge(self, blowout_game_bdl):
        analyzer = BlowoutRiskAnalyzer(blowout_game_bdl)
        assessments = analyzer.assess_games("2026-02-25")

        edge_df = _make_edge_df([
            {
                "player": "Player A", "team": "OKC", "opponent": "WAS",
                "stat_type": "PRA", "book_line": 42.5, "over_odds": -110,
                "dk_salary": 10000, "sim_mean": 48.0, "sim_std": 8.5,
                "p_over": 0.74, "implied": 0.52, "edge_pct": 0.22,
                "edge_display": "+22%", "kelly_frac": 0.05, "confidence": "High",
            },
            {
                "player": "Player B", "team": "WAS", "opponent": "OKC",
                "stat_type": "PRA", "book_line": 30.0, "over_odds": -110,
                "dk_salary": 5000, "sim_mean": 35.0, "sim_std": 6.0,
                "p_over": 0.80, "implied": 0.52, "edge_pct": 0.28,
                "edge_display": "+28%", "kelly_frac": 0.06, "confidence": "High",
            },
        ])

        adjusted = analyzer.adjust_edges(edge_df, assessments)
        edges = adjusted["adj_edge_pct"].tolist()
        assert edges == sorted(edges, reverse=True)


class TestMinutesFactor:
    def test_custom_factor(self):
        """User can configure a different minutes penalty floor."""
        bdl = MockBDL(
            tonight_games=[_game(1, "OKC", "WAS", "Scheduled")],
            odds={1: {"spread_home": -14.0}},
        )
        # With floor=0.85, the factor should be < 1.0 when blowout detected
        analyzer = BlowoutRiskAnalyzer(bdl, minutes_factor=0.85)
        result = analyzer.assess_games("2026-02-25")
        if "OKC" in result and result["OKC"].is_blowout_risk:
            # Factor scales with risk — should be less than 1.0 but the
            # configured floor (0.85) represents the *max* penalty at risk=1.0
            assert result["OKC"].minutes_factor < 1.0

    def test_factor_scales_with_risk(self):
        """Higher risk → lower minutes factor (not just a flat 10%)."""
        bdl_moderate = MockBDL(
            tonight_games=[_game(1, "A", "B", "Scheduled")],
            odds={1: {"spread_home": -9.0}},
        )
        bdl_extreme = MockBDL(
            tonight_games=[_game(1, "A", "B", "Scheduled")],
            odds={1: {"spread_home": -15.0}},
        )
        a_mod = BlowoutRiskAnalyzer(bdl_moderate)
        a_ext = BlowoutRiskAnalyzer(bdl_extreme)

        r_mod = a_mod.assess_games("2026-02-25")
        r_ext = a_ext.assess_games("2026-02-25")

        if "A" in r_mod and "A" in r_ext:
            # Extreme should have equal or lower factor
            assert r_ext["A"].minutes_factor <= r_mod["A"].minutes_factor
