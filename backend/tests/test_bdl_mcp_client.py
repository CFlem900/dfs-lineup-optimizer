"""Tests for the BDLMCPClient — typed wrapper over BDL MCP tools."""

import pytest
from unittest.mock import MagicMock, patch

from app.models.balldontlie_schemas import BDLGame, BDLPlayer, BDLTeam
from app.services.bdl_mcp_client import (
    BDLMCPClient,
    BDLMethodNotAvailable,
    _UNSUPPORTED_METHODS,
)


# ============================================================================
# FIXTURES
# ============================================================================


@pytest.fixture
def mock_bdl_service():
    """Mock BallDontLieService with pre-loaded team mapping."""
    svc = MagicMock()
    svc.is_available = True
    svc._bdl_team_abbrs = {
        1: "ATL", 2: "BOS", 5: "CHI", 7: "DAL", 14: "LAL",
    }
    svc._teams_loaded = True
    return svc


@pytest.fixture
def mock_injury_service():
    """Mock InjuryService returning sample injuries."""
    svc = MagicMock()
    svc.get_active_injuries.return_value = [
        {
            "player_name": "Zach LaVine",
            "status": "Out",
            "injury_description": "Left knee",
            "official_factor": 0.0,
            "p_plays": 0.0,
            "e_minutes_if_active": 0.0,
            "team_name": "Chicago Bulls",
        },
        {
            "player_name": "Lonzo Ball",
            "status": "Out",
            "injury_description": "Left knee surgery",
            "official_factor": 0.0,
            "p_plays": 0.0,
            "e_minutes_if_active": 0.0,
            "team_name": "Chicago Bulls",
        },
        {
            "player_name": "Coby White",
            "status": "Questionable",
            "injury_description": "Ankle soreness",
            "official_factor": 0.81,
            "p_plays": 0.85,
            "e_minutes_if_active": 0.95,
            "team_name": "Chicago Bulls",
        },
    ]
    return svc


@pytest.fixture
def bdl_client(mock_bdl_service, mock_injury_service):
    """BDLMCPClient wired with mocked dependencies."""
    return BDLMCPClient(mock_bdl_service, mock_injury_service)


@pytest.fixture
def bdl_client_no_injury(mock_bdl_service):
    """BDLMCPClient without InjuryService."""
    return BDLMCPClient(mock_bdl_service)


@pytest.fixture
def raw_players_chi():
    """Raw BDL player dicts for CHI roster."""
    return {
        "data": [
            {
                "id": 101,
                "first_name": "Zach",
                "last_name": "LaVine",
                "position": "G",
                "team": {"id": 5, "abbreviation": "CHI"},
            },
            {
                "id": 102,
                "first_name": "Coby",
                "last_name": "White",
                "position": "G",
                "team": {"id": 5, "abbreviation": "CHI"},
            },
            {
                "id": 103,
                "first_name": "Nikola",
                "last_name": "Vucevic",
                "position": "C",
                "team": {"id": 5, "abbreviation": "CHI"},
            },
            {
                "id": 104,
                "first_name": "Lonzo",
                "last_name": "Ball",
                "position": "G",
                "team": {"id": 5, "abbreviation": "CHI"},
            },
        ],
        "meta": {"next_cursor": None, "per_page": 100},
    }


@pytest.fixture
def raw_games_response():
    """Raw BDL games response."""
    return {
        "data": [
            {
                "id": 9001,
                "date": "2026-02-25",
                "season": 2025,
                "status": "Final",
                "period": 4,
                "home_team_score": 112,
                "visitor_team_score": 105,
                "home_team": {
                    "id": 5,
                    "abbreviation": "CHI",
                    "full_name": "Chicago Bulls",
                },
                "visitor_team": {
                    "id": 1,
                    "abbreviation": "ATL",
                    "full_name": "Atlanta Hawks",
                },
            },
        ],
        "meta": {"next_cursor": None, "per_page": 100},
    }


# ============================================================================
# TEST: is_available
# ============================================================================


class TestIsAvailable:
    def test_available_when_bdl_has_key(self, bdl_client):
        assert bdl_client.is_available is True

    def test_not_available_when_no_bdl_service(self):
        client = BDLMCPClient(None)
        assert client.is_available is False

    def test_not_available_when_bdl_says_no(self):
        mock_bdl = MagicMock()
        mock_bdl.is_available = False
        client = BDLMCPClient(mock_bdl)
        assert client.is_available is False


# ============================================================================
# TEST: get_teams
# ============================================================================


class TestGetTeams:
    def test_returns_bdl_team_models(self, bdl_client):
        teams = bdl_client.get_teams()
        assert len(teams) == 5
        assert all(isinstance(t, BDLTeam) for t in teams)
        abbrs = {t.abbreviation for t in teams}
        assert "CHI" in abbrs
        assert "DAL" in abbrs

    def test_raises_when_unavailable(self):
        client = BDLMCPClient(None)
        with pytest.raises(RuntimeError, match="not available"):
            client.get_teams()


# ============================================================================
# TEST: get_players
# ============================================================================


class TestGetPlayers:
    def test_by_team_abbr(self, bdl_client, raw_players_chi):
        bdl_client._bdl._get.return_value = raw_players_chi
        players = bdl_client.get_players(team_abbr="CHI")
        assert len(players) == 4
        assert all(isinstance(p, BDLPlayer) for p in players)
        assert players[0].first_name == "Zach"

    def test_by_name_search(self, bdl_client):
        bdl_client._bdl._get.return_value = {
            "data": [
                {"id": 101, "first_name": "Zach", "last_name": "LaVine"},
            ],
            "meta": {},
        }
        players = bdl_client.get_players(last_name="LaVine")
        assert len(players) == 1
        assert players[0].last_name == "LaVine"

    def test_no_filters_returns_empty(self, bdl_client):
        players = bdl_client.get_players()
        assert players == []

    def test_skips_invalid_player_data(self, bdl_client):
        bdl_client._bdl._get.return_value = {
            "data": [
                {"id": 101, "first_name": "Zach", "last_name": "LaVine"},
                {"bad_key": "no id field"},  # Will fail validation
            ],
            "meta": {},
        }
        players = bdl_client.get_players(team_abbr="CHI")
        # Should still get the valid player, skipping the bad one
        assert len(players) == 1


# ============================================================================
# TEST: get_games
# ============================================================================


class TestGetGames:
    def test_fetches_by_date(self, bdl_client, raw_games_response):
        bdl_client._bdl._get.return_value = raw_games_response
        games = bdl_client.get_games(date="2026-02-25")
        assert len(games) == 1
        assert isinstance(games[0], BDLGame)
        assert games[0].id == 9001
        assert games[0].home_team.abbreviation == "CHI"

        # Verify correct path was used
        call_path = bdl_client._bdl._get.call_args[0][0]
        assert "dates[]=2026-02-25" in call_path

    def test_fetches_by_season(self, bdl_client, raw_games_response):
        bdl_client._bdl._get.return_value = raw_games_response
        bdl_client.get_games(season=2025)
        call_path = bdl_client._bdl._get.call_args[0][0]
        assert "seasons[]=2025" in call_path

    def test_fetches_by_team_id(self, bdl_client, raw_games_response):
        bdl_client._bdl._get.return_value = raw_games_response
        bdl_client.get_games(team_id=5)
        call_path = bdl_client._bdl._get.call_args[0][0]
        assert "team_ids[]=5" in call_path


# ============================================================================
# TEST: get_game
# ============================================================================


class TestGetGame:
    def test_returns_single_game(self, bdl_client):
        bdl_client._bdl._get.return_value = {
            "id": 9001,
            "date": "2026-02-25",
            "status": "Final",
            "period": 4,
            "home_team_score": 112,
            "visitor_team_score": 105,
            "home_team": {"id": 5, "abbreviation": "CHI"},
            "visitor_team": {"id": 1, "abbreviation": "ATL"},
        }
        game = bdl_client.get_game(9001)
        assert isinstance(game, BDLGame)
        assert game.id == 9001

    def test_returns_none_on_error(self, bdl_client):
        bdl_client._bdl._get.side_effect = Exception("API error")
        game = bdl_client.get_game(99999)
        assert game is None


# ============================================================================
# TEST: get_active_roster (InjuryService integration)
# ============================================================================


class TestGetActiveRoster:
    def test_filters_out_injured_players(self, bdl_client, raw_players_chi):
        """Out players (LaVine, Ball) are removed; Questionable (White) stays."""
        bdl_client._bdl._get.return_value = raw_players_chi

        active = bdl_client.get_active_roster("CHI")

        names = [p.full_name for p in active]
        assert "Zach LaVine" not in names  # Out
        assert "Lonzo Ball" not in names   # Out
        assert "Coby White" in names       # Questionable → stays
        assert "Nikola Vucevic" in names   # Healthy → stays
        assert len(active) == 2

    def test_no_injury_service_returns_full_roster(
        self, bdl_client_no_injury, raw_players_chi
    ):
        """Without InjuryService, returns all players."""
        bdl_client_no_injury._bdl._get.return_value = raw_players_chi
        active = bdl_client_no_injury.get_active_roster("CHI")
        assert len(active) == 4

    def test_injury_service_error_returns_full_roster(
        self, bdl_client, raw_players_chi
    ):
        """If InjuryService raises, fall back to full roster."""
        bdl_client._bdl._get.return_value = raw_players_chi
        bdl_client._injury.get_active_injuries.side_effect = Exception("DB down")

        active = bdl_client.get_active_roster("CHI")
        assert len(active) == 4

    def test_no_injuries_returns_full_roster(self, bdl_client, raw_players_chi):
        """If no injuries reported, returns full roster."""
        bdl_client._bdl._get.return_value = raw_players_chi
        bdl_client._injury.get_active_injuries.return_value = []

        active = bdl_client.get_active_roster("CHI")
        assert len(active) == 4


# ============================================================================
# TEST: Props guard (__getattr__)
# ============================================================================


class TestPropsGuard:
    def test_get_player_props_raises(self, bdl_client):
        with pytest.raises(BDLMethodNotAvailable, match="get_player_props"):
            bdl_client.get_player_props()

    def test_get_props_raises(self, bdl_client):
        with pytest.raises(BDLMethodNotAvailable, match="get_props"):
            bdl_client.get_props()

    def test_get_odds_raises(self, bdl_client):
        with pytest.raises(BDLMethodNotAvailable, match="get_odds"):
            bdl_client.get_odds()

    def test_get_betting_lines_raises(self, bdl_client):
        with pytest.raises(BDLMethodNotAvailable, match="get_betting_lines"):
            bdl_client.get_betting_lines()

    def test_get_player_stats_raises(self, bdl_client):
        with pytest.raises(BDLMethodNotAvailable, match="get_player_stats"):
            bdl_client.get_player_stats()

    def test_get_season_averages_raises(self, bdl_client):
        with pytest.raises(BDLMethodNotAvailable, match="get_season_averages"):
            bdl_client.get_season_averages()

    def test_all_unsupported_methods_blocked(self, bdl_client):
        """Every method in _UNSUPPORTED_METHODS raises BDLMethodNotAvailable."""
        for method_name in _UNSUPPORTED_METHODS:
            with pytest.raises(BDLMethodNotAvailable):
                getattr(bdl_client, method_name)

    def test_unknown_attr_raises_attribute_error(self, bdl_client):
        """Non-blocklisted unknown attrs raise normal AttributeError."""
        with pytest.raises(AttributeError, match="has no attribute"):
            bdl_client.totally_fake_method()

    def test_props_guard_logs_error(self, bdl_client, caplog):
        """Calling a blocked method logs an ERROR with the method name."""
        import logging

        with caplog.at_level(logging.ERROR, logger="app.services.bdl_mcp_client"):
            with pytest.raises(BDLMethodNotAvailable):
                bdl_client.get_player_props()

        assert "get_player_props" in caplog.text
        assert "NOT available" in caplog.text


# ============================================================================
# TEST: _require_available
# ============================================================================


class TestRequireAvailable:
    def test_raises_when_no_service(self):
        client = BDLMCPClient(None)
        with pytest.raises(RuntimeError, match="not available"):
            client._require_available()

    def test_passes_when_available(self, bdl_client):
        bdl_client._require_available()  # Should not raise


# ============================================================================
# TEST: _fetch_players_by_team
# ============================================================================


class TestFetchPlayersByTeam:
    def test_unknown_team_returns_empty(self, bdl_client):
        result = bdl_client._fetch_players_by_team("ZZZ")
        assert result == []

    def test_calls_correct_path(self, bdl_client, raw_players_chi):
        bdl_client._bdl._get.return_value = raw_players_chi
        result = bdl_client._fetch_players_by_team("CHI")
        assert len(result) == 4
        call_path = bdl_client._bdl._get.call_args[0][0]
        assert "team_ids[]=5" in call_path
