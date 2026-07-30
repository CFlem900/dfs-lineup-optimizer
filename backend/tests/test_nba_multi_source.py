"""Tests for the NBAMultiSourceService facade."""

import pytest
from unittest.mock import MagicMock, patch, PropertyMock

from app.models.player import PlayerMinutes
from app.services.nba_multi_source import NBAMultiSourceService, _bdl_team_to_nba_id


# ============================================================================
# FIXTURES
# ============================================================================


@pytest.fixture
def mock_nba_api():
    """Mock NBAApiService."""
    nba = MagicMock()
    nba.get_all_teams.return_value = [
        {"id": 1610612741, "abbreviation": "CHI", "full_name": "Chicago Bulls",
         "nickname": "Bulls", "city": "Chicago"},
    ]
    nba.find_team_by_name.return_value = {
        "id": 1610612741, "abbreviation": "CHI", "full_name": "Chicago Bulls",
    }
    nba.get_today_scoreboard.return_value = [
        {"GAME_ID": "0022500123", "HOME_TEAM_ID": 1610612741}
    ]
    nba.build_team_rotation.return_value = [
        PlayerMinutes(
            player_id=201, player_name="Zach LaVine", position="G",
            team_id=1610612741, season_avg=32.0, usage_rate=0.25,
            minutes_last_5=[30, 33, 35, 32, 31],
            minutes_last_10=[30, 33, 35, 32, 31, 28, 34, 30, 33, 29],
        ),
    ]
    nba._db_cache = None
    return nba


@pytest.fixture
def mock_bdl():
    """Mock BallDontLieService."""
    bdl = MagicMock()
    type(bdl).is_available = PropertyMock(return_value=True)
    return bdl


@pytest.fixture
def mock_db_cache():
    """Mock NBADataCacheService."""
    return MagicMock()


@pytest.fixture
def multi_source(mock_nba_api, mock_bdl, mock_db_cache):
    """NBAMultiSourceService with all mocked dependencies."""
    return NBAMultiSourceService(
        nba_api=mock_nba_api,
        balldontlie=mock_bdl,
        db_cache=mock_db_cache,
    )


@pytest.fixture
def multi_source_no_bdl(mock_nba_api, mock_db_cache):
    """NBAMultiSourceService without BDL (BDL unavailable)."""
    return NBAMultiSourceService(
        nba_api=mock_nba_api,
        balldontlie=None,
        db_cache=mock_db_cache,
    )


@pytest.fixture
def sample_bdl_roster():
    """Sample BDL roster response (already parsed)."""
    return [
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
    ]


@pytest.fixture
def sample_bdl_logs():
    """Sample BDL game logs by name (already in NBA format)."""
    return {
        "Zach LaVine": [
            {"MIN": 34.0, "PTS": 28, "REB": 5, "AST": 4, "STL": 2,
             "BLK": 0, "TOV": 3, "FG3M": 3, "PLUS_MINUS": 12,
             "MATCHUP": "CHI vs. ATL", "GAME_DATE": "2026-02-20"},
            {"MIN": 30.0, "PTS": 22, "REB": 3, "AST": 6, "STL": 1,
             "BLK": 1, "TOV": 2, "FG3M": 2, "PLUS_MINUS": -5,
             "MATCHUP": "CHI @ NYK", "GAME_DATE": "2026-02-18"},
        ],
        "Coby White": [
            {"MIN": 28.0, "PTS": 18, "REB": 4, "AST": 5, "STL": 1,
             "BLK": 0, "TOV": 2, "FG3M": 2, "PLUS_MINUS": 8,
             "MATCHUP": "CHI vs. ATL", "GAME_DATE": "2026-02-20"},
        ],
    }


# ============================================================================
# TEST: get_all_teams / find_team_by_name
# ============================================================================


class TestStaticMethods:
    """These just delegate to NBAApiService (offline, no HTTP)."""

    def test_get_all_teams(self, multi_source, mock_nba_api):
        teams = multi_source.get_all_teams()
        assert len(teams) == 1
        assert teams[0]["abbreviation"] == "CHI"
        mock_nba_api.get_all_teams.assert_called_once()

    def test_find_team_by_name(self, multi_source, mock_nba_api):
        team = multi_source.find_team_by_name("Bulls")
        assert team["id"] == 1610612741
        mock_nba_api.find_team_by_name.assert_called_once_with("Bulls")


# ============================================================================
# TEST: get_today_scoreboard
# ============================================================================


class TestScoreboard:
    def test_tries_bdl_first(self, multi_source, mock_bdl, mock_nba_api):
        """BDL scoreboard is tried first."""
        mock_bdl.get_games.return_value = [
            {
                "id": 9001,
                "date": "2026-02-24",
                "status": "Final",
                "period": 4,
                "time": "",
                "home_team_score": 112,
                "visitor_team_score": 105,
                "home_team": {"id": 5, "abbreviation": "CHI", "full_name": "Chicago Bulls"},
                "visitor_team": {"id": 1, "abbreviation": "ATL", "full_name": "Atlanta Hawks"},
            }
        ]

        games = multi_source.get_today_scoreboard()
        assert len(games) == 1
        assert games[0]["HOME_TEAM_ABBREVIATION"] == "CHI"
        mock_bdl.get_games.assert_called_once()
        mock_nba_api.get_today_scoreboard.assert_not_called()

    def test_returns_empty_on_bdl_failure(self, multi_source, mock_bdl, mock_nba_api):
        """Returns empty when BDL fails (no NBA API fallback)."""
        mock_bdl.get_games.side_effect = Exception("BDL down")

        games = multi_source.get_today_scoreboard()
        assert games == []
        mock_nba_api.get_today_scoreboard.assert_not_called()

    def test_returns_empty_when_bdl_unavailable(self, multi_source_no_bdl, mock_nba_api):
        """Returns empty when BDL is not configured (no NBA API fallback)."""
        games = multi_source_no_bdl.get_today_scoreboard()
        assert games == []
        mock_nba_api.get_today_scoreboard.assert_not_called()

    def test_returns_empty_when_bdl_returns_empty(self, multi_source, mock_bdl, mock_nba_api):
        """Returns empty when BDL returns no games (no NBA API fallback)."""
        mock_bdl.get_games.return_value = []

        games = multi_source.get_today_scoreboard()
        assert games == []
        mock_nba_api.get_today_scoreboard.assert_not_called()


# ============================================================================
# TEST: build_team_rotation with BDL
# ============================================================================


class TestBuildTeamRotation:
    def test_bdl_data_passed_as_prefetch(
        self, multi_source, mock_bdl, mock_nba_api,
        sample_bdl_roster, sample_bdl_logs
    ):
        """When BDL succeeds, data is passed as prefetched to NBAApiService.

        With draftable_names provided, the DK-driven path is used:
        get_dk_driven_game_logs() instead of get_team_game_logs_bulk().
        """
        mock_bdl.get_dk_driven_game_logs.return_value = (
            sample_bdl_roster, sample_bdl_logs
        )
        mock_bdl.get_bdl_position.side_effect = lambda p: p.get("position", "F")

        # Mock the NBA static player lookup
        with patch("app.services.nba_multi_source.nba_players_static") as mock_static:
            mock_static.get_players.return_value = [
                {"id": 201, "full_name": "Zach LaVine", "is_active": True},
                {"id": 202, "full_name": "Coby White", "is_active": True},
            ]

            multi_source.build_team_rotation(
                team_id=1610612741,
                season="2025-26",
                draftable_names={"Zach LaVine", "Coby White"},
            )

        # NBAApiService should be called with prefetched data
        call_kwargs = mock_nba_api.build_team_rotation.call_args[1]
        assert call_kwargs["prefetched_roster"] is not None
        assert call_kwargs["prefetched_game_logs"] is not None
        # DB cache should NOT be passed when BDL succeeds
        assert "cache_service" not in call_kwargs

    def test_falls_back_to_db_cache_on_bdl_failure(
        self, multi_source, mock_bdl, mock_nba_api, mock_db_cache
    ):
        """Falls back to DB cache when BDL fails."""
        mock_bdl.get_dk_driven_game_logs.side_effect = Exception("BDL error")

        multi_source.build_team_rotation(
            team_id=1610612741,
            draftable_names={"Zach LaVine"},
        )

        # Should fall back with DB cache (no prefetched data)
        call_kwargs = mock_nba_api.build_team_rotation.call_args[1]
        assert "prefetched_roster" not in call_kwargs
        assert "prefetched_game_logs" not in call_kwargs
        assert call_kwargs["cache_service"] == mock_db_cache

    def test_falls_back_when_bdl_not_configured(
        self, multi_source_no_bdl, mock_nba_api, mock_db_cache
    ):
        """Without BDL, goes straight to NBAApiService with DB cache."""
        multi_source_no_bdl.build_team_rotation(
            team_id=1610612741,
        )

        call_kwargs = mock_nba_api.build_team_rotation.call_args[1]
        assert call_kwargs["cache_service"] == mock_db_cache

    def test_prefetched_data_passes_through(
        self, multi_source, mock_nba_api
    ):
        """Caller-provided prefetched data is passed through directly."""
        pre_roster = [{"PLAYER_ID": 201, "PLAYER": "Zach LaVine"}]
        pre_logs = {201: [{"MIN": 34, "PTS": 28}]}

        multi_source.build_team_rotation(
            team_id=1610612741,
            prefetched_roster=pre_roster,
            prefetched_game_logs=pre_logs,
        )

        call_kwargs = mock_nba_api.build_team_rotation.call_args[1]
        assert call_kwargs["prefetched_roster"] == pre_roster
        assert call_kwargs["prefetched_game_logs"] == pre_logs

    def test_draftable_args_forwarded(
        self, multi_source_no_bdl, mock_nba_api
    ):
        """Draftable names and positions are forwarded to NBAApiService."""
        multi_source_no_bdl.build_team_rotation(
            team_id=1610612741,
            draftable_names={"Zach LaVine"},
            draftable_positions={"Zach LaVine": "SG"},
            max_players=10,
        )

        call_kwargs = mock_nba_api.build_team_rotation.call_args[1]
        assert call_kwargs["draftable_names"] == {"Zach LaVine"}
        assert call_kwargs["draftable_positions"] == {"Zach LaVine": "SG"}
        assert call_kwargs["max_players"] == 10


# ============================================================================
# TEST: BDL → NBA prefetch conversion
# ============================================================================


class TestBdlPrefetchConversion:
    def test_converts_roster_format(
        self, multi_source, mock_bdl,
        sample_bdl_roster, sample_bdl_logs
    ):
        """BDL roster is converted to NBA API format."""
        mock_bdl.get_team_game_logs_bulk.return_value = (
            sample_bdl_roster, sample_bdl_logs
        )
        mock_bdl.get_bdl_position.side_effect = lambda p: p.get("position", "F")

        with patch("app.services.nba_multi_source.nba_players_static") as mock_static:
            mock_static.get_players.return_value = [
                {"id": 201, "full_name": "Zach LaVine", "is_active": True},
                {"id": 202, "full_name": "Coby White", "is_active": True},
            ]

            roster, logs = multi_source._fetch_bdl_prefetch(
                1610612741, "2025-26"
            )

        assert len(roster) == 2
        # Check roster format
        assert roster[0]["PLAYER_ID"] == 201
        assert roster[0]["PLAYER"] == "Zach LaVine"
        assert roster[0]["POSITION"] == "G"

        # Check game logs keyed by NBA ID
        assert 201 in logs
        assert len(logs[201]) == 2
        assert logs[201][0]["PTS"] == 28

    def test_returns_none_for_empty_roster(self, multi_source, mock_bdl):
        """Returns (None, None) when BDL returns empty roster."""
        mock_bdl.get_team_game_logs_bulk.return_value = ([], {})

        roster, logs = multi_source._fetch_bdl_prefetch(1610612741, "2025-26")
        assert roster is None
        assert logs is None

    def test_handles_unmatched_players(
        self, multi_source, mock_bdl, sample_bdl_roster, sample_bdl_logs
    ):
        """Unmatched BDL players are skipped without error."""
        mock_bdl.get_team_game_logs_bulk.return_value = (
            sample_bdl_roster, sample_bdl_logs
        )
        mock_bdl.get_bdl_position.side_effect = lambda p: p.get("position", "F")

        with patch("app.services.nba_multi_source.nba_players_static") as mock_static:
            # Only Zach LaVine matches — Coby White is "missing"
            mock_static.get_players.return_value = [
                {"id": 201, "full_name": "Zach LaVine", "is_active": True},
            ]

            roster, logs = multi_source._fetch_bdl_prefetch(
                1610612741, "2025-26"
            )

        assert len(roster) == 1
        assert roster[0]["PLAYER"] == "Zach LaVine"
        assert 201 in logs


# ============================================================================
# TEST: Scoreboard conversion
# ============================================================================


class TestScoreboardConversion:
    def test_final_game_conversion(self):
        """Final game is converted to NBA API format."""
        bdl_games = [{
            "id": 9001,
            "date": "2026-02-24",
            "status": "Final",
            "period": 4,
            "time": "",
            "home_team_score": 112,
            "visitor_team_score": 105,
            "home_team": {"id": 5, "abbreviation": "CHI", "full_name": "Chicago Bulls"},
            "visitor_team": {"id": 1, "abbreviation": "ATL", "full_name": "Atlanta Hawks"},
        }]

        result = NBAMultiSourceService._convert_bdl_scoreboard(bdl_games)

        assert len(result) == 1
        assert result[0]["GAME_STATUS_TEXT"] == "Final"
        assert result[0]["HOME_TEAM_SCORE"] == 112
        assert result[0]["VISITOR_TEAM_SCORE"] == 105
        assert result[0]["HOME_TEAM_ABBREVIATION"] == "CHI"
        assert result[0]["VISITOR_TEAM_ABBREVIATION"] == "ATL"

    def test_live_game_conversion(self):
        """Live game shows quarter + time."""
        bdl_games = [{
            "id": 9002,
            "date": "2026-02-24",
            "status": "In Progress",
            "period": 3,
            "time": "5:42",
            "home_team_score": 78,
            "visitor_team_score": 72,
            "home_team": {"id": 5, "abbreviation": "CHI", "full_name": "Chicago Bulls"},
            "visitor_team": {"id": 1, "abbreviation": "ATL", "full_name": "Atlanta Hawks"},
        }]

        result = NBAMultiSourceService._convert_bdl_scoreboard(bdl_games)
        assert result[0]["GAME_STATUS_TEXT"] == "Q3 5:42"

    def test_status_id_final(self):
        """Final game gets GAME_STATUS_ID=3."""
        bdl_games = [{
            "id": 9001, "date": "2026-02-24", "status": "Final",
            "period": 4, "time": "",
            "home_team_score": 112, "visitor_team_score": 105,
            "home_team": {"id": 5, "abbreviation": "CHI", "full_name": "Chicago Bulls"},
            "visitor_team": {"id": 1, "abbreviation": "ATL", "full_name": "Atlanta Hawks"},
        }]
        result = NBAMultiSourceService._convert_bdl_scoreboard(bdl_games)
        assert result[0]["GAME_STATUS_ID"] == 3

    def test_status_id_in_progress(self):
        """In-progress game gets GAME_STATUS_ID=2."""
        bdl_games = [{
            "id": 9002, "date": "2026-02-24", "status": "In Progress",
            "period": 3, "time": "5:42",
            "home_team_score": 78, "visitor_team_score": 72,
            "home_team": {"id": 5, "abbreviation": "CHI", "full_name": "Chicago Bulls"},
            "visitor_team": {"id": 1, "abbreviation": "ATL", "full_name": "Atlanta Hawks"},
        }]
        result = NBAMultiSourceService._convert_bdl_scoreboard(bdl_games)
        assert result[0]["GAME_STATUS_ID"] == 2

    def test_status_id_scheduled(self):
        """Scheduled game gets GAME_STATUS_ID=1."""
        bdl_games = [{
            "id": 9003, "date": "2026-02-24", "status": "",
            "period": 0, "time": "",
            "home_team": {"id": 5, "abbreviation": "CHI", "full_name": "Chicago Bulls"},
            "visitor_team": {"id": 1, "abbreviation": "ATL", "full_name": "Atlanta Hawks"},
        }]
        result = NBAMultiSourceService._convert_bdl_scoreboard(bdl_games)
        assert result[0]["GAME_STATUS_ID"] == 1

    def test_game_sequence_ordering(self):
        """Multiple games get sequential GAME_SEQUENCE numbers."""
        bdl_games = [
            {"id": 9001, "date": "2026-02-24", "status": "", "period": 0, "time": "",
             "home_team": {"id": 5, "abbreviation": "CHI", "full_name": "Bulls"},
             "visitor_team": {"id": 1, "abbreviation": "ATL", "full_name": "Hawks"}},
            {"id": 9002, "date": "2026-02-24", "status": "", "period": 0, "time": "",
             "home_team": {"id": 10, "abbreviation": "GSW", "full_name": "Warriors"},
             "visitor_team": {"id": 2, "abbreviation": "BOS", "full_name": "Celtics"}},
        ]
        result = NBAMultiSourceService._convert_bdl_scoreboard(bdl_games)
        assert result[0]["GAME_SEQUENCE"] == 1
        assert result[1]["GAME_SEQUENCE"] == 2

    def test_source_tag(self):
        """Converted games have _source='balldontlie' tag."""
        bdl_games = [{
            "id": 9001, "date": "2026-02-24", "status": "Final",
            "period": 4, "time": "",
            "home_team_score": 112, "visitor_team_score": 105,
            "home_team": {"id": 5, "abbreviation": "CHI", "full_name": "Chicago Bulls"},
            "visitor_team": {"id": 1, "abbreviation": "ATL", "full_name": "Atlanta Hawks"},
        }]
        result = NBAMultiSourceService._convert_bdl_scoreboard(bdl_games)
        assert result[0]["_source"] == "balldontlie"


# ============================================================================
# TEST: Scoreboard for specific date
# ============================================================================


class TestScoreboardForDate:
    def test_bdl_first_for_date(self, multi_source, mock_bdl):
        """get_scoreboard_for_date tries BDL first."""
        mock_bdl.get_games.return_value = [
            {"id": 9001, "date": "2026-02-24", "status": "Final", "period": 4,
             "time": "", "home_team_score": 112, "visitor_team_score": 105,
             "home_team": {"id": 5, "abbreviation": "CHI", "full_name": "Chicago Bulls"},
             "visitor_team": {"id": 1, "abbreviation": "ATL", "full_name": "Atlanta Hawks"}},
        ]

        result = multi_source.get_scoreboard_for_date("2026-02-24")
        assert len(result) == 1
        assert result[0]["GAME_ID"] == "9001"
        mock_bdl.get_games.assert_called_once_with("2026-02-24")

    def test_bdl_failure_returns_empty(self, multi_source, mock_bdl):
        """Returns empty when BDL fails (no NBA API fallback)."""
        mock_bdl.get_games.side_effect = Exception("BDL down")

        result = multi_source.get_scoreboard_for_date("2026-02-20")
        assert result == []

    def test_bdl_empty_returns_empty(self, multi_source, mock_bdl):
        """Returns empty when BDL returns empty list (no NBA API fallback)."""
        mock_bdl.get_games.return_value = []

        result = multi_source.get_scoreboard_for_date("2026-02-20")
        assert result == []

    def test_no_bdl_returns_empty(self, multi_source_no_bdl):
        """Without BDL, returns empty (no NBA API fallback)."""
        result = multi_source_no_bdl.get_scoreboard_for_date("2026-02-20")
        assert result == []

    def test_all_sources_fail_returns_empty(self, multi_source, mock_bdl):
        """Returns empty list when BDL fails."""
        mock_bdl.get_games.side_effect = Exception("BDL down")

        result = multi_source.get_scoreboard_for_date("2026-02-20")
        assert result == []


# ============================================================================
# TEST: BDL team → NBA team ID helper
# ============================================================================


class TestBdlTeamToNbaId:
    def test_known_teams(self):
        assert _bdl_team_to_nba_id(5) == 1610612741   # CHI
        assert _bdl_team_to_nba_id(14) == 1610612747   # LAL
        assert _bdl_team_to_nba_id(2) == 1610612738    # BOS

    def test_unknown_team(self):
        assert _bdl_team_to_nba_id(999) is None

    def test_none_input(self):
        assert _bdl_team_to_nba_id(None) is None


# ============================================================================
# TEST: Proxy methods
# ============================================================================


class TestProxyMethods:
    def test_get_team_roster(self, multi_source, mock_nba_api):
        multi_source.get_team_roster(1610612741, "2025-26")
        mock_nba_api.get_team_roster.assert_called_once_with(1610612741, "2025-26")

    def test_build_player_minutes(self, multi_source, mock_nba_api):
        multi_source.build_player_minutes(201, "Zach LaVine", "G", 1610612741)
        mock_nba_api.build_player_minutes.assert_called_once()


# ============================================================================
# TEST: Diagnostics
# ============================================================================


class TestDiagnostics:
    def test_source_status_all_available(self, multi_source):
        status = multi_source.get_source_status()
        assert status["balldontlie_available"] is True
        assert status["db_cache_available"] is True
        assert status["nba_api_live_disabled"] is True

    def test_source_status_no_bdl(self, multi_source_no_bdl):
        status = multi_source_no_bdl.get_source_status()
        assert status["balldontlie_available"] is False
        assert status["db_cache_available"] is True
        assert status["nba_api_live_disabled"] is True
