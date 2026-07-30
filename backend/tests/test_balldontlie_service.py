"""Tests for the BallDontLie API client service."""

import pytest
from unittest.mock import patch, MagicMock
import httpx

from app.services.balldontlie_service import BallDontLieService


# ============================================================================
# FIXTURES
# ============================================================================


@pytest.fixture
def bdl_service():
    """BallDontLieService with test API key."""
    with patch("app.services.balldontlie_service.settings") as mock_settings:
        mock_settings.balldontlie_api_key = "test-api-key-123"
        mock_settings.balldontlie_rate_limit = 600  # High limit for tests
        svc = BallDontLieService()
        # Pre-load team mapping to avoid API call in tests
        svc._nba_to_bdl_team = {1610612741: 5}  # CHI
        svc._bdl_to_nba_team = {5: 1610612741}
        svc._bdl_team_abbrs = {5: "CHI"}
        svc._teams_loaded = True
        return svc


@pytest.fixture
def bdl_roster_response():
    """Sample BDL roster response for CHI."""
    return {
        "data": [
            {
                "id": 101,
                "first_name": "Zach",
                "last_name": "LaVine",
                "position": "G",
                "height": "6-5",
                "weight": "200",
                "jersey_number": "8",
                "college": "UCLA",
                "country": "USA",
                "draft_year": 2014,
                "draft_round": 1,
                "draft_number": 13,
                "team": {
                    "id": 5,
                    "abbreviation": "CHI",
                    "city": "Chicago",
                    "conference": "East",
                    "division": "Central",
                    "full_name": "Chicago Bulls",
                    "name": "Bulls",
                },
            },
            {
                "id": 102,
                "first_name": "Coby",
                "last_name": "White",
                "position": "G",
                "height": "6-4",
                "weight": "195",
                "jersey_number": "0",
                "college": "North Carolina",
                "country": "USA",
                "draft_year": 2019,
                "draft_round": 1,
                "draft_number": 7,
                "team": {
                    "id": 5,
                    "abbreviation": "CHI",
                    "city": "Chicago",
                    "conference": "East",
                    "division": "Central",
                    "full_name": "Chicago Bulls",
                    "name": "Bulls",
                },
            },
        ],
        "meta": {"next_cursor": None, "per_page": 100},
    }


@pytest.fixture
def bdl_stats_response():
    """Sample BDL stats response with 2 games for 1 player."""
    return {
        "data": [
            {
                "id": 5001,
                "min": "34:22",
                "fgm": 10,
                "fga": 18,
                "fg_pct": 0.556,
                "fg3m": 3,
                "fg3a": 7,
                "fg3_pct": 0.429,
                "ftm": 5,
                "fta": 6,
                "ft_pct": 0.833,
                "oreb": 1,
                "dreb": 4,
                "reb": 5,
                "ast": 4,
                "stl": 2,
                "blk": 0,
                "turnover": 3,
                "pf": 2,
                "pts": 28,
                "plus_minus": 12,
                "player": {"id": 101, "first_name": "Zach", "last_name": "LaVine"},
                "team": {"id": 5, "abbreviation": "CHI"},
                "game": {
                    "id": 9001,
                    "date": "2026-02-20",
                    "home_team": {"id": 5, "abbreviation": "CHI"},
                    "visitor_team": {"id": 1, "abbreviation": "ATL"},
                },
            },
            {
                "id": 5002,
                "min": "30:45",
                "fgm": 8,
                "fga": 16,
                "fg_pct": 0.500,
                "fg3m": 2,
                "fg3a": 5,
                "fg3_pct": 0.400,
                "ftm": 4,
                "fta": 4,
                "ft_pct": 1.0,
                "oreb": 0,
                "dreb": 3,
                "reb": 3,
                "ast": 6,
                "stl": 1,
                "blk": 1,
                "turnover": 2,
                "pf": 3,
                "pts": 22,
                "plus_minus": -5,
                "player": {"id": 101, "first_name": "Zach", "last_name": "LaVine"},
                "team": {"id": 5, "abbreviation": "CHI"},
                "game": {
                    "id": 9002,
                    "date": "2026-02-18",
                    "home_team": {"id": 20, "abbreviation": "NYK"},
                    "visitor_team": {"id": 5, "abbreviation": "CHI"},
                },
            },
        ],
        "meta": {"next_cursor": None, "per_page": 100},
    }


@pytest.fixture
def bdl_games_response():
    """Sample BDL games response."""
    return {
        "data": [
            {
                "id": 9001,
                "date": "2026-02-24",
                "season": 2025,
                "status": "Final",
                "period": 4,
                "time": "",
                "postseason": False,
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
            }
        ],
        "meta": {"next_cursor": None, "per_page": 25},
    }


# ============================================================================
# TEST: is_available
# ============================================================================


class TestIsAvailable:
    def test_available_with_key(self, bdl_service):
        assert bdl_service.is_available is True

    def test_not_available_without_key(self):
        with patch("app.services.balldontlie_service.settings") as mock_settings:
            mock_settings.balldontlie_api_key = ""
            mock_settings.balldontlie_rate_limit = 60
            svc = BallDontLieService()
            assert svc.is_available is False


# ============================================================================
# TEST: Format Translation
# ============================================================================


class TestBdlStatToNbaFormat:
    """Test BDL → NBA game log format translation."""

    def test_basic_stat_translation(self, bdl_service):
        """All stat fields map correctly from BDL lowercase to NBA uppercase."""
        # Add ATL to the team abbr lookup so MATCHUP resolves correctly
        bdl_service._bdl_team_abbrs[1] = "ATL"

        bdl_stat = {
            "min": "32:15",
            "pts": 25,
            "reb": 8,
            "ast": 5,
            "stl": 2,
            "blk": 1,
            "turnover": 3,
            "fg3m": 4,
            "plus_minus": 12,
            "fgm": 10,
            "fga": 18,
            "fg_pct": 0.556,
            "fg3a": 7,
            "fg3_pct": 0.571,
            "ftm": 1,
            "fta": 2,
            "ft_pct": 0.5,
            "oreb": 2,
            "dreb": 6,
            "player": {"id": 101},
            "team": {"id": 5, "abbreviation": "CHI"},
            "game": {
                "id": 9001,
                "date": "2026-02-20",
                "home_team_id": 5,
                "visitor_team_id": 1,
            },
        }

        result = bdl_service.bdl_stat_to_nba_format(bdl_stat, "CHI")

        assert result["MIN"] == 32.2  # 32 + 15/60 ≈ 32.25, rounded to 32.2
        assert result["PTS"] == 25
        assert result["REB"] == 8
        assert result["AST"] == 5
        assert result["STL"] == 2
        assert result["BLK"] == 1
        assert result["TOV"] == 3  # BDL "turnover" → NBA "TOV"
        assert result["FG3M"] == 4
        assert result["PLUS_MINUS"] == 12
        assert result["MATCHUP"] == "CHI vs. ATL"  # Home game
        assert result["GAME_DATE"] == "2026-02-20"

    def test_away_game_matchup(self, bdl_service):
        """Away game produces correct MATCHUP string."""
        bdl_service._bdl_team_abbrs[20] = "NYK"

        bdl_stat = {
            "min": "28:00",
            "pts": 20,
            "reb": 5,
            "ast": 3,
            "stl": 1,
            "blk": 0,
            "turnover": 2,
            "fg3m": 2,
            "plus_minus": -3,
            "player": {"id": 101},
            "team": {"id": 5, "abbreviation": "CHI"},
            "game": {
                "id": 9002,
                "date": "2026-02-18",
                "home_team_id": 20,
                "visitor_team_id": 5,
            },
        }

        result = bdl_service.bdl_stat_to_nba_format(bdl_stat, "CHI")
        assert result["MATCHUP"] == "CHI @ NYK"

    def test_minutes_string_parsing(self, bdl_service):
        """Various minutes formats parse correctly."""
        # MM:SS format
        stat = {"min": "25:30", "game": {}, "team": {}, "player": {}}
        result = bdl_service.bdl_stat_to_nba_format(stat)
        assert result["MIN"] == 25.5

        # Zero minutes
        stat["min"] = "00:00"
        result = bdl_service.bdl_stat_to_nba_format(stat)
        assert result["MIN"] == 0.0

        # None
        stat["min"] = None
        result = bdl_service.bdl_stat_to_nba_format(stat)
        assert result["MIN"] == 0.0

        # Plain number
        stat["min"] = "32"
        result = bdl_service.bdl_stat_to_nba_format(stat)
        assert result["MIN"] == 32.0

    def test_null_stat_fields_default_to_zero(self, bdl_service):
        """None/null stat values default to 0."""
        bdl_stat = {
            "min": "20:00",
            "pts": None,
            "reb": None,
            "ast": 0,
            "stl": None,
            "blk": None,
            "turnover": None,
            "fg3m": None,
            "plus_minus": None,
            "player": {"id": 101},
            "team": {"id": 5, "abbreviation": "CHI"},
            "game": {"id": 9001, "date": "2026-02-20"},
        }

        result = bdl_service.bdl_stat_to_nba_format(bdl_stat)
        assert result["PTS"] == 0
        assert result["REB"] == 0
        assert result["STL"] == 0
        assert result["TOV"] == 0
        assert result["PLUS_MINUS"] == 0


# ============================================================================
# TEST: Season format conversion
# ============================================================================


class TestSeasonConversion:
    def test_standard_season(self):
        assert BallDontLieService.nba_season_to_bdl("2025-26") == 2025

    def test_older_season(self):
        assert BallDontLieService.nba_season_to_bdl("2023-24") == 2023


# ============================================================================
# TEST: Team mapping
# ============================================================================


class TestTeamMapping:
    def test_nba_to_bdl(self, bdl_service):
        """NBA.com team ID maps to BDL team ID."""
        assert bdl_service.get_bdl_team_id(1610612741) == 5  # CHI

    def test_bdl_to_nba(self, bdl_service):
        """BDL team ID maps back to NBA.com team ID."""
        assert bdl_service.get_nba_team_id(5) == 1610612741  # CHI

    def test_unknown_team_returns_none(self, bdl_service):
        """Unknown team ID returns None."""
        assert bdl_service.get_bdl_team_id(99999) is None
        assert bdl_service.get_nba_team_id(99999) is None


# ============================================================================
# TEST: get_team_players
# ============================================================================


class TestGetTeamPlayers:
    @patch("app.services.balldontlie_service.resilient_get")
    def test_fetches_roster(self, mock_get, bdl_service, bdl_roster_response):
        """Fetches team roster via BDL API."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = bdl_roster_response
        mock_get.return_value = mock_resp

        players = bdl_service.get_team_players(1610612741)

        assert len(players) == 2
        assert players[0]["first_name"] == "Zach"
        assert players[0]["last_name"] == "LaVine"
        assert players[1]["first_name"] == "Coby"
        assert players[1]["last_name"] == "White"

        # Verify correct URL was called
        call_url = mock_get.call_args[0][0]
        assert "/players?" in call_url
        assert "team_ids[]=5" in call_url

    @patch("app.services.balldontlie_service.resilient_get")
    def test_unknown_team_returns_empty(self, mock_get, bdl_service):
        """Unknown NBA team ID returns empty list without API call."""
        players = bdl_service.get_team_players(99999)
        assert players == []
        mock_get.assert_not_called()


# ============================================================================
# TEST: get_player_stats
# ============================================================================


class TestGetPlayerStats:
    @patch("app.services.balldontlie_service.resilient_get")
    def test_bulk_stats_fetch(self, mock_get, bdl_service, bdl_stats_response):
        """Fetches bulk stats for multiple players."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = bdl_stats_response
        mock_get.return_value = mock_resp

        stats = bdl_service.get_player_stats([101, 102], season=2025)

        assert len(stats) == 2
        assert stats[0]["pts"] == 28
        assert stats[1]["pts"] == 22

        # Verify URL has both player IDs
        call_url = mock_get.call_args[0][0]
        assert "player_ids[]=101" in call_url
        assert "player_ids[]=102" in call_url
        assert "seasons[]=2025" in call_url

    def test_empty_ids_returns_empty(self, bdl_service):
        """Empty player IDs list returns empty without API call."""
        stats = bdl_service.get_player_stats([], season=2025)
        assert stats == []


# ============================================================================
# TEST: get_games
# ============================================================================


class TestGetGames:
    @patch("app.services.balldontlie_service.resilient_get")
    def test_fetches_games_by_date(self, mock_get, bdl_service, bdl_games_response):
        """Fetches games for a specific date."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = bdl_games_response
        mock_get.return_value = mock_resp

        games = bdl_service.get_games("2026-02-24")

        assert len(games) == 1
        assert games[0]["home_team"]["abbreviation"] == "CHI"
        assert games[0]["visitor_team"]["abbreviation"] == "ATL"
        assert games[0]["home_team_score"] == 112

        call_url = mock_get.call_args[0][0]
        assert "dates[]=2026-02-24" in call_url


# ============================================================================
# TEST: get_team_game_logs_bulk
# ============================================================================


class TestGetTeamGameLogsBulk:
    @patch("app.services.balldontlie_service.resilient_get")
    def test_bulk_fetch_returns_roster_and_logs(
        self, mock_get, bdl_service, bdl_roster_response, bdl_stats_response
    ):
        """Bulk fetch returns roster + NBA-format game logs grouped by name."""
        # First call: roster, Second call: stats
        mock_resp_roster = MagicMock()
        mock_resp_roster.json.return_value = bdl_roster_response
        mock_resp_stats = MagicMock()
        mock_resp_stats.json.return_value = bdl_stats_response

        mock_get.side_effect = [mock_resp_roster, mock_resp_stats]

        roster, logs = bdl_service.get_team_game_logs_bulk(1610612741, "2025-26")

        # Roster is filtered to only players with season stats.
        # Only LaVine (ID 101) has stats; Coby White (ID 102) does not.
        assert len(roster) == 1
        assert roster[0]["first_name"] == "Zach"
        assert "Zach LaVine" in logs
        assert len(logs["Zach LaVine"]) == 2

        # Verify NBA format
        game = logs["Zach LaVine"][0]
        assert "MIN" in game
        assert "PTS" in game
        assert "MATCHUP" in game
        assert game["PTS"] == 28  # First game (most recent)

    @patch("app.services.balldontlie_service.resilient_get")
    def test_logs_sorted_reverse_chronological(
        self, mock_get, bdl_service, bdl_roster_response, bdl_stats_response
    ):
        """Game logs are sorted most-recent-first."""
        mock_resp_roster = MagicMock()
        mock_resp_roster.json.return_value = bdl_roster_response
        mock_resp_stats = MagicMock()
        mock_resp_stats.json.return_value = bdl_stats_response

        mock_get.side_effect = [mock_resp_roster, mock_resp_stats]

        _, logs = bdl_service.get_team_game_logs_bulk(1610612741, "2025-26")

        games = logs["Zach LaVine"]
        # Most recent first
        assert games[0]["GAME_DATE"] == "2026-02-20"
        assert games[1]["GAME_DATE"] == "2026-02-18"

    @patch("app.services.balldontlie_service.resilient_get")
    def test_empty_roster_returns_empty(self, mock_get, bdl_service):
        """Empty roster returns empty results."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"data": [], "meta": {}}
        mock_get.return_value = mock_resp

        roster, logs = bdl_service.get_team_game_logs_bulk(1610612741, "2025-26")
        assert roster == []
        assert logs == {}


# ============================================================================
# TEST: Pagination
# ============================================================================


class TestPagination:
    @patch("app.services.balldontlie_service.resilient_get")
    def test_follows_cursor_pagination(self, mock_get, bdl_service):
        """Follows next_cursor through multiple pages."""
        page1 = MagicMock()
        page1.json.return_value = {
            "data": [{"id": 1, "pts": 20}],
            "meta": {"next_cursor": 42, "per_page": 100},
        }
        page2 = MagicMock()
        page2.json.return_value = {
            "data": [{"id": 2, "pts": 25}],
            "meta": {"next_cursor": None, "per_page": 100},
        }
        mock_get.side_effect = [page1, page2]

        results = bdl_service._get_all_pages("/stats?seasons[]=2025&per_page=100")

        assert len(results) == 2
        assert results[0]["pts"] == 20
        assert results[1]["pts"] == 25
        assert mock_get.call_count == 2

        # Second call should include cursor
        second_url = mock_get.call_args_list[1][0][0]
        assert "cursor=42" in second_url

    @patch("app.services.balldontlie_service.resilient_get")
    def test_stops_at_max_pages(self, mock_get, bdl_service):
        """Stops paginating after max_pages even if more data exists."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "data": [{"id": 1}],
            "meta": {"next_cursor": 99, "per_page": 100},
        }
        mock_get.return_value = mock_resp

        results = bdl_service._get_all_pages("/stats?per_page=100", max_pages=3)

        assert len(results) == 3  # 1 item per page × 3 pages
        assert mock_get.call_count == 3


# ============================================================================
# TEST: Rate limiting
# ============================================================================


class TestRateLimiting:
    def test_rate_limit_updates_timestamp(self, bdl_service):
        """Rate limiter updates last request time."""
        bdl_service._rate_limit()
        assert bdl_service._last_request_time > 0


# ============================================================================
# TEST: Hardcoded team mapping fallback
# ============================================================================


class TestHardcodedMapping:
    def test_loads_30_teams(self):
        """Hardcoded mapping loads all 30 NBA teams."""
        with patch("app.services.balldontlie_service.settings") as mock_settings:
            mock_settings.balldontlie_api_key = "test-key"
            mock_settings.balldontlie_rate_limit = 60
            svc = BallDontLieService()
            svc._load_hardcoded_mapping()
            assert len(svc._nba_to_bdl_team) == 30
            assert len(svc._bdl_to_nba_team) == 30

    def test_chi_mapping_correct(self):
        """CHI maps to BDL ID 5."""
        with patch("app.services.balldontlie_service.settings") as mock_settings:
            mock_settings.balldontlie_api_key = "test-key"
            mock_settings.balldontlie_rate_limit = 60
            svc = BallDontLieService()
            svc._load_hardcoded_mapping()
            # CHI NBA ID is 1610612741
            assert svc._nba_to_bdl_team.get(1610612741) == 5


# ============================================================================
# TEST: get_bdl_position
# ============================================================================


class TestGetBdlPosition:
    def test_returns_position(self, bdl_service):
        assert bdl_service.get_bdl_position({"position": "G"}) == "G"
        assert bdl_service.get_bdl_position({"position": "F-C"}) == "F-C"

    def test_defaults_to_f(self, bdl_service):
        assert bdl_service.get_bdl_position({}) == "F"
        assert bdl_service.get_bdl_position({"position": ""}) == "F"
        assert bdl_service.get_bdl_position({"position": None}) == "F"


# ============================================================================
# TEST: V2 Odds endpoint
# ============================================================================


class TestGetOdds:
    """Tests for BDL V2 odds methods."""

    @patch("app.services.balldontlie_service.resilient_get")
    def test_fetches_odds_v2(self, mock_get, bdl_service):
        """get_odds uses V2 URL and parses response correctly."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "data": [
                {
                    "id": 1,
                    "game_id": 9001,
                    "vendor": "DraftKings",
                    "spread_home": -5.5,
                    "spread_away": 5.5,
                    "total": 224.0,
                    "over": -110,
                    "under": -110,
                    "moneyline_home": -200,
                    "moneyline_away": 170,
                    "updated_at": "2026-02-24T18:00:00Z",
                },
            ],
            "meta": {"next_cursor": None, "per_page": 100},
        }
        mock_get.return_value = mock_resp

        odds = bdl_service.get_odds("2026-02-24")

        assert len(odds) == 1
        assert odds[0]["game_id"] == 9001
        assert odds[0]["vendor"] == "DraftKings"
        assert odds[0]["spread_home"] == -5.5

        # Verify V2 URL
        call_url = mock_get.call_args[0][0]
        assert "/v2/odds" in call_url
        assert "dates[]=2026-02-24" in call_url

    @patch("app.services.balldontlie_service.resilient_get")
    def test_get_odds_by_game(self, mock_get, bdl_service):
        """get_odds_by_game groups by game_id and prefers DraftKings."""
        mock_resp = MagicMock()
        # Raw BDL V2 response uses *_value suffix for numeric fields
        mock_resp.json.return_value = {
            "data": [
                {
                    "id": 1,
                    "game_id": 9001,
                    "vendor": "FanDuel",
                    "spread_home_value": -6.0,
                    "spread_away_value": 6.0,
                    "total_value": 225.0,
                    "updated_at": "2026-02-24T17:00:00Z",
                },
                {
                    "id": 2,
                    "game_id": 9001,
                    "vendor": "DraftKings",
                    "spread_home_value": -5.5,
                    "spread_away_value": 5.5,
                    "total_value": 224.0,
                    "updated_at": "2026-02-24T18:00:00Z",
                },
                {
                    "id": 3,
                    "game_id": 9002,
                    "vendor": "FanDuel",
                    "spread_home_value": -2.0,
                    "spread_away_value": 2.0,
                    "total_value": 215.0,
                    "updated_at": "2026-02-24T17:30:00Z",
                },
            ],
            "meta": {"next_cursor": None, "per_page": 100},
        }
        mock_get.return_value = mock_resp

        result = bdl_service.get_odds_by_game("2026-02-24")

        # Two games
        assert len(result) == 2
        assert 9001 in result
        assert 9002 in result

        # Game 9001 should prefer DraftKings
        g1 = result[9001]
        assert g1["vendor"] == "DraftKings"
        assert g1["spread_home"] == -5.5
        assert g1["total"] == 224.0

        # Game 9002 only has FanDuel
        g2 = result[9002]
        assert g2["vendor"] == "FanDuel"
        assert g2["spread_home"] == -2.0

    @patch("app.services.balldontlie_service.resilient_get")
    def test_get_odds_empty_date(self, mock_get, bdl_service):
        """No odds for date returns empty dict."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "data": [],
            "meta": {"next_cursor": None, "per_page": 100},
        }
        mock_get.return_value = mock_resp

        result = bdl_service.get_odds_by_game("2026-07-04")
        assert result == {}


class TestV2Pagination:
    """Tests for V2 pagination helper."""

    @patch("app.services.balldontlie_service.resilient_get")
    def test_v2_follows_cursor(self, mock_get, bdl_service):
        """_get_all_pages_v2 follows cursor-based pagination."""
        page1 = MagicMock()
        page1.json.return_value = {
            "data": [{"id": 1, "game_id": 9001}],
            "meta": {"next_cursor": 50, "per_page": 100},
        }
        page2 = MagicMock()
        page2.json.return_value = {
            "data": [{"id": 2, "game_id": 9002}],
            "meta": {"next_cursor": None, "per_page": 100},
        }
        mock_get.side_effect = [page1, page2]

        results = bdl_service._get_all_pages_v2("/odds?per_page=100")

        assert len(results) == 2
        assert mock_get.call_count == 2

        # Second call should use V2 URL and have cursor
        second_url = mock_get.call_args_list[1][0][0]
        assert "/v2/" in second_url
        assert "cursor=50" in second_url
