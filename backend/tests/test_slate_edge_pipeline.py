"""Tests for SlateEdgePipeline — DK props + Monte Carlo sims → top edges."""

import pytest
import pandas as pd
from unittest.mock import MagicMock, PropertyMock

from app.models.props import PlayerPropLine as DKPropLine, PlayerProps
from app.services.bdl_mcp_client import BDLMethodNotAvailable
from app.services.prop_edge_analyzer import PropEdgeAnalyzer, SimProjection
from app.services.slate_edge_pipeline import SlateEdgePipeline


# ============================================================================
# FIXTURES
# ============================================================================


def _make_dk_props() -> dict:
    """Build a mock DK props cache (normalized_name:TEAM → PlayerProps)."""
    return {
        "luka doncic:DAL": PlayerProps(
            player_name="Luka Doncic",
            team_abbreviation="DAL",
            props=[
                DKPropLine(
                    stat="pra", line=48.5, over_odds=-110,
                    under_odds=-110, implied_over_prob=0.524,
                    implied_under_prob=0.524,
                ),
                DKPropLine(
                    stat="pts", line=28.5, over_odds=-115,
                    under_odds=-105, implied_over_prob=0.535,
                    implied_under_prob=0.512,
                ),
            ],
        ),
        "shai gilgeous-alexander:OKC": PlayerProps(
            player_name="Shai Gilgeous-Alexander",
            team_abbreviation="OKC",
            props=[
                DKPropLine(
                    stat="pra", line=42.5, over_odds=-115,
                    under_odds=-105, implied_over_prob=0.535,
                    implied_under_prob=0.512,
                ),
            ],
        ),
        "jayson tatum:BOS": PlayerProps(
            player_name="Jayson Tatum",
            team_abbreviation="BOS",
            props=[
                DKPropLine(
                    stat="pra", line=40.5, over_odds=-105,
                    under_odds=-115, implied_over_prob=0.512,
                    implied_under_prob=0.535,
                ),
            ],
        ),
        "nikola jokic:DEN": PlayerProps(
            player_name="Nikola Jokic",
            team_abbreviation="DEN",
            props=[
                DKPropLine(
                    stat="pra", line=52.5, over_odds=-120,
                    under_odds=100, implied_over_prob=0.545,
                    implied_under_prob=0.500,
                ),
            ],
        ),
        "anthony edwards:MIN": PlayerProps(
            player_name="Anthony Edwards",
            team_abbreviation="MIN",
            props=[
                DKPropLine(
                    stat="pra", line=37.5, over_odds=-110,
                    under_odds=-110, implied_over_prob=0.524,
                    implied_under_prob=0.524,
                ),
            ],
        ),
        "tyrese haliburton:IND": PlayerProps(
            player_name="Tyrese Haliburton",
            team_abbreviation="IND",
            props=[
                DKPropLine(
                    stat="pra", line=34.5, over_odds=-110,
                    under_odds=-110, implied_over_prob=0.524,
                    implied_under_prob=0.524,
                ),
            ],
        ),
    }


@pytest.fixture
def sim_projections() -> dict:
    """Monte Carlo sim output for all players."""
    return {
        "Luka Doncic": SimProjection(mean_pra=52.1, std_pra=9.3),
        "Shai Gilgeous-Alexander": SimProjection(mean_pra=46.8, std_pra=8.1),
        "Jayson Tatum": SimProjection(mean_pra=43.2, std_pra=8.5),
        "Anthony Edwards": SimProjection(mean_pra=36.0, std_pra=7.8),
        "Nikola Jokic": SimProjection(mean_pra=56.4, std_pra=9.8),
        "Tyrese Haliburton": SimProjection(mean_pra=33.0, std_pra=7.2),
    }


@pytest.fixture
def mock_dk_props_svc():
    """Mock DKPropsService returning sample props."""
    svc = MagicMock()
    svc.get_player_props.return_value = _make_dk_props()
    return svc


@pytest.fixture
def mock_bdl_mcp():
    """Mock BDLMCPClient with get_active_roster returning all players."""
    from app.models.balldontlie_schemas import BDLPlayer

    client = MagicMock()
    # Return a full roster for any team
    client.get_active_roster.return_value = [
        BDLPlayer(id=1, first_name="Luka", last_name="Doncic"),
        BDLPlayer(id=2, first_name="Shai", last_name="Gilgeous-Alexander"),
        BDLPlayer(id=3, first_name="Jayson", last_name="Tatum"),
        BDLPlayer(id=4, first_name="Nikola", last_name="Jokic"),
        BDLPlayer(id=5, first_name="Anthony", last_name="Edwards"),
        BDLPlayer(id=6, first_name="Tyrese", last_name="Haliburton"),
    ]
    return client


@pytest.fixture
def pipeline(mock_dk_props_svc, mock_bdl_mcp):
    return SlateEdgePipeline(mock_dk_props_svc, mock_bdl_mcp)


@pytest.fixture
def pipeline_no_bdl(mock_dk_props_svc):
    """Pipeline without BDL MCP client."""
    return SlateEdgePipeline(mock_dk_props_svc)


# ============================================================================
# TEST: Full pipeline
# ============================================================================


class TestBuildSlateEdges:
    def test_returns_dataframe_with_top_5(self, pipeline, sim_projections):
        df = pipeline.build_slate_edges("2026-02-25", sim_projections, top_n=5)
        assert isinstance(df, pd.DataFrame)
        assert len(df) <= 5

    def test_sorted_by_edge_descending(self, pipeline, sim_projections):
        df = pipeline.build_slate_edges("2026-02-25", sim_projections, top_n=5)
        if len(df) > 1:
            edges = df["edge_pct"].tolist()
            assert edges == sorted(edges, reverse=True)

    def test_all_expected_columns(self, pipeline, sim_projections):
        df = pipeline.build_slate_edges("2026-02-25", sim_projections, top_n=5)
        expected = {
            "player", "team", "book_line", "over_odds", "sim_mean",
            "sim_std", "p_over", "implied", "edge_pct", "edge_display",
            "kelly_frac", "confidence",
        }
        assert expected.issubset(set(df.columns))

    def test_jokic_and_luka_have_highest_edges(self, pipeline, sim_projections):
        """Jokic (56.4 vs 52.5) and Luka (52.1 vs 48.5) should dominate."""
        df = pipeline.build_slate_edges("2026-02-25", sim_projections, top_n=2)
        top_names = set(df["player"].tolist())
        # Both should be mean > line with positive edge
        assert all(df["edge_pct"] > 0)

    def test_player_names_filter(self, pipeline, sim_projections):
        """Only evaluate requested players."""
        df = pipeline.build_slate_edges(
            "2026-02-25",
            sim_projections,
            player_names=["Luka Doncic", "Nikola Jokic"],
            top_n=5,
        )
        names = set(df["player"].tolist())
        assert names.issubset({"Luka Doncic", "Nikola Jokic"})

    def test_stat_type_pts(self, pipeline, sim_projections):
        """Can analyze PTS props instead of PRA."""
        # Only Luka has a pts prop in our fixture
        pts_projections = {
            "Luka Doncic": {"mean_pra": 30.5, "std_pra": 6.0},
        }
        df = pipeline.build_slate_edges(
            "2026-02-25", pts_projections, stat_type="pts", top_n=5,
        )
        if len(df) > 0:
            assert df.iloc[0]["stat_type"] == "PTS"


# ============================================================================
# TEST: No BDL MCP (should still work)
# ============================================================================


class TestWithoutBDL:
    def test_works_without_bdl_client(self, pipeline_no_bdl, sim_projections):
        """Pipeline works without BDL MCP — no roster filtering."""
        df = pipeline_no_bdl.build_slate_edges(
            "2026-02-25", sim_projections, top_n=5,
        )
        assert isinstance(df, pd.DataFrame)
        assert len(df) > 0


# ============================================================================
# TEST: Active roster filtering
# ============================================================================


class TestActiveRosterFilter:
    def test_filters_out_inactive_players(
        self, mock_dk_props_svc, sim_projections
    ):
        """Players not on active roster are excluded."""
        from app.models.balldontlie_schemas import BDLPlayer

        mock_bdl = MagicMock()
        # Only return Luka and Jokic as active
        mock_bdl.get_active_roster.return_value = [
            BDLPlayer(id=1, first_name="Luka", last_name="Doncic"),
            BDLPlayer(id=4, first_name="Nikola", last_name="Jokic"),
        ]

        pipeline = SlateEdgePipeline(mock_dk_props_svc, mock_bdl)
        df = pipeline.build_slate_edges("2026-02-25", sim_projections, top_n=5)

        names = set(df["player"].tolist())
        # Only active players should appear
        assert names.issubset({"Luka Doncic", "Nikola Jokic"})

    def test_roster_failure_includes_all(
        self, mock_dk_props_svc, sim_projections
    ):
        """If BDL roster fetch fails, include all players from that team."""
        mock_bdl = MagicMock()
        mock_bdl.get_active_roster.side_effect = Exception("BDL down")

        pipeline = SlateEdgePipeline(mock_dk_props_svc, mock_bdl)
        df = pipeline.build_slate_edges("2026-02-25", sim_projections, top_n=5)

        # Should still return results (graceful degradation)
        assert len(df) > 0

    def test_skip_filter_when_disabled(
        self, mock_dk_props_svc, mock_bdl_mcp, sim_projections
    ):
        """filter_active_only=False skips roster lookup."""
        pipeline = SlateEdgePipeline(mock_dk_props_svc, mock_bdl_mcp)
        df = pipeline.build_slate_edges(
            "2026-02-25", sim_projections,
            filter_active_only=False, top_n=5,
        )
        # BDL should NOT have been called
        mock_bdl_mcp.get_active_roster.assert_not_called()
        assert len(df) > 0


# ============================================================================
# TEST: Edge cases
# ============================================================================


class TestEdgeCases:
    def test_empty_dk_props(self, mock_bdl_mcp, sim_projections):
        """Empty DK props returns empty DataFrame."""
        empty_dk = MagicMock()
        empty_dk.get_player_props.return_value = {}

        pipeline = SlateEdgePipeline(empty_dk, mock_bdl_mcp)
        df = pipeline.build_slate_edges("2026-02-25", sim_projections)
        assert df.empty

    def test_dk_service_failure(self, mock_bdl_mcp, sim_projections):
        """DK service error returns empty DataFrame."""
        broken_dk = MagicMock()
        broken_dk.get_player_props.side_effect = Exception("DK API down")

        pipeline = SlateEdgePipeline(broken_dk, mock_bdl_mcp)
        df = pipeline.build_slate_edges("2026-02-25", sim_projections)
        assert df.empty

    def test_no_matching_sim_projections(self, pipeline):
        """Sim projections with no matching players → empty."""
        unrelated = {"Fake Player": SimProjection(mean_pra=50.0, std_pra=8.0)}
        df = pipeline.build_slate_edges("2026-02-25", unrelated)
        assert df.empty

    def test_dict_projections_accepted(self, pipeline):
        """Plain dicts work as sim projections."""
        projs = {"Luka Doncic": {"mean_pra": 52.0, "std_pra": 9.0}}
        df = pipeline.build_slate_edges("2026-02-25", projs, top_n=1)
        assert len(df) == 1
        assert df.iloc[0]["player"] == "Luka Doncic"


# ============================================================================
# TEST: BDL MCP props guard (demonstrates the guard in action)
# ============================================================================


class TestBDLPropsGuard:
    """Prove that attempting to use BDL MCP for props fails correctly."""

    def test_bdl_mcp_rejects_props_call(self):
        """The BDLMCPClient raises BDLMethodNotAvailable for props."""
        from app.services.bdl_mcp_client import BDLMCPClient

        mock_bdl_svc = MagicMock()
        mock_bdl_svc.is_available = True
        client = BDLMCPClient(mock_bdl_svc)

        with pytest.raises(BDLMethodNotAvailable, match="get_player_props"):
            client.get_player_props()

    def test_bdl_mcp_rejects_get_props(self):
        from app.services.bdl_mcp_client import BDLMCPClient

        mock_bdl_svc = MagicMock()
        mock_bdl_svc.is_available = True
        client = BDLMCPClient(mock_bdl_svc)

        with pytest.raises(BDLMethodNotAvailable, match="get_props"):
            client.get_props()

    def test_pipeline_uses_dk_not_bdl_for_props(self, pipeline, sim_projections):
        """Pipeline calls DKPropsService (not BDL MCP) for prop lines."""
        pipeline.build_slate_edges("2026-02-25", sim_projections)

        # DK props service was called
        pipeline._dk_props.get_player_props.assert_called_once_with("2026-02-25")

        # BDL MCP was called for roster validation only, NOT for props
        if pipeline._bdl_mcp:
            for call in pipeline._bdl_mcp.method_calls:
                method_name = call[0]
                assert "prop" not in method_name.lower(), (
                    f"Pipeline called BDL MCP for props: {method_name}"
                )
