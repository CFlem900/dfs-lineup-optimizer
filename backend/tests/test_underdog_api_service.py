"""Tests for UnderdogApiService — pick'em line scraper."""

import pytest
from unittest.mock import patch, MagicMock

from app.models.underdog import UnderdogPickemLine, UnderdogSlate
from app.services.underdog_api_service import (
    UnderdogApiService,
    _looks_like_uuid,
    _normalize_name,
    _normalize_stat,
    PICKEM_STATS,
)


# ── Sample Underdog API response (mocked) ────────────────────────────

SAMPLE_UD_RESPONSE = {
    "players": [
        {
            "id": "p1001",
            "first_name": "LeBron",
            "last_name": "James",
            "team_id": "t100",
            "position_id": "SF",
            "image_url": "https://img.ud/lebron.png",
        },
        {
            "id": "p1002",
            "first_name": "Stephen",
            "last_name": "Curry",
            "team_id": "t101",
            "position_id": "PG",
            "image_url": "https://img.ud/curry.png",
        },
        {
            "id": "p1003",
            "first_name": "Nikola",
            "last_name": "Jokic",
            "team_id": "t102",
            "position_id": "C",
            "image_url": None,
        },
    ],
    "appearances": [
        {"id": "a2001", "player_id": "p1001", "team_id": "t100", "status": "active"},
        {"id": "a2002", "player_id": "p1002", "team_id": "t101", "status": "active"},
        {"id": "a2003", "player_id": "p1003", "team_id": "t102", "status": "active"},
    ],
    "over_under_lines": [
        {
            "stat_value": 25.5,
            "over_under": {
                "appearance_stat": {
                    "appearance_id": "a2001",
                    "stat": "points",
                }
            },
            "options": [
                {"title": "Higher", "choice": "higher"},
                {"title": "Lower", "choice": "lower"},
            ],
        },
        {
            "stat_value": 8.5,
            "over_under": {
                "appearance_stat": {
                    "appearance_id": "a2001",
                    "stat": "rebounds",
                }
            },
            "options": [],
        },
        {
            "stat_value": 7.5,
            "over_under": {
                "appearance_stat": {
                    "appearance_id": "a2001",
                    "stat": "assists",
                }
            },
            "options": [],
        },
        {
            "stat_value": 41.5,
            "over_under": {
                "appearance_stat": {
                    "appearance_id": "a2001",
                    "stat": "pts_rebs_asts",
                }
            },
            "options": [],
        },
        {
            "stat_value": 28.5,
            "over_under": {
                "appearance_stat": {
                    "appearance_id": "a2002",
                    "stat": "points",
                }
            },
            "options": [],
        },
        {
            "stat_value": 5.5,
            "over_under": {
                "appearance_stat": {
                    "appearance_id": "a2002",
                    "stat": "three_pointers_made",
                }
            },
            "options": [],
        },
        {
            "stat_value": 48.5,
            "over_under": {
                "appearance_stat": {
                    "appearance_id": "a2003",
                    "stat": "fantasy_points",
                }
            },
            "options": [],
        },
    ],
    "games": [
        {
            "id": "g3001",
            "home_team_id": "t100",
            "away_team_id": "t103",
            "scheduled_at": "2026-02-19T19:00:00Z",
        },
    ],
}


# ── Helper: patch httpx.get ───────────────────────────────────────────

def _mock_httpx_get(data: dict = None, status_code: int = 200, raise_exc=None):
    """Return a patcher for resilient_get that returns mock response.

    For error status codes (>= 400), the mock raises HTTPStatusError
    directly — matching real resilient_get behavior (it re-raises after
    exhausting retries or immediately for non-retryable 4xx).
    """
    if raise_exc:
        return patch("app.services.underdog_api_service.resilient_get", side_effect=raise_exc)
    if status_code >= 400:
        import httpx as _httpx
        mock_resp = MagicMock()
        mock_resp.status_code = status_code
        exc = _httpx.HTTPStatusError("error", request=MagicMock(), response=mock_resp)
        return patch("app.services.underdog_api_service.resilient_get", side_effect=exc)
    mock_resp = MagicMock()
    mock_resp.status_code = status_code
    mock_resp.json.return_value = data or SAMPLE_UD_RESPONSE
    return patch("app.services.underdog_api_service.resilient_get", return_value=mock_resp)


# ── Tests: Name normalisation ─────────────────────────────────────────

class TestNameNormalization:
    def test_basic(self):
        assert _normalize_name("LeBron James") == "lebron james"

    def test_suffix_removal(self):
        assert _normalize_name("Jaren Jackson Jr.") == "jaren jackson"
        assert _normalize_name("Gary Trent Jr") == "gary trent"
        assert _normalize_name("Robert Williams III") == "robert williams"

    def test_accented_characters(self):
        assert _normalize_name("Nikola Jokić") == "nikola jokic"
        assert _normalize_name("Luka Dončić") == "luka doncic"

    def test_extra_whitespace(self):
        assert _normalize_name("  Stephen   Curry  ") == "stephen curry"


# ── Tests: Stat normalisation ─────────────────────────────────────────

class TestStatNormalization:
    def test_known_stats(self):
        assert _normalize_stat("points") == "pts"
        assert _normalize_stat("rebounds") == "reb"
        assert _normalize_stat("assists") == "ast"
        assert _normalize_stat("pts_rebs_asts") == "pra"
        assert _normalize_stat("three_pointers_made") == "fg3m"
        assert _normalize_stat("fantasy_points") == "fantasy_points"
        assert _normalize_stat("blks_stls") == "stl_blk"

    def test_case_insensitive(self):
        assert _normalize_stat("Points") == "pts"
        assert _normalize_stat("REBOUNDS") == "reb"

    def test_unknown_passthrough(self):
        assert _normalize_stat("some_new_stat") == "some_new_stat"


# ── Tests: Response parsing ───────────────────────────────────────────

class TestResponseParsing:
    def setup_method(self):
        self.service = UnderdogApiService(cache_ttl=0)  # no cache for tests

    def test_parse_response_creates_lines(self):
        slate = self.service._parse_response(SAMPLE_UD_RESPONSE, "2026-02-19")
        assert isinstance(slate, UnderdogSlate)
        assert slate.game_date == "2026-02-19"
        assert len(slate.lines) > 0

    def test_parse_response_player_count(self):
        slate = self.service._parse_response(SAMPLE_UD_RESPONSE, "2026-02-19")
        # 3 players: LeBron, Curry, Jokic
        assert slate.player_count == 3

    def test_parse_response_line_values(self):
        slate = self.service._parse_response(SAMPLE_UD_RESPONSE, "2026-02-19")
        lebron_pts = [l for l in slate.lines if "lebron" in l.player_name.lower() and l.stat == "pts"]
        assert len(lebron_pts) == 1
        assert lebron_pts[0].line == 25.5

    def test_parse_response_stat_mapping(self):
        slate = self.service._parse_response(SAMPLE_UD_RESPONSE, "2026-02-19")
        stats = {l.stat for l in slate.lines}
        # Should have pts, reb, ast, pra, fg3m, fantasy_points (all in PICKEM_STATS)
        assert "pts" in stats
        assert "reb" in stats
        assert "ast" in stats
        assert "pra" in stats
        assert "fg3m" in stats
        assert "fantasy_points" in stats

    def test_parse_response_deduplication(self):
        """Same player + stat should not appear twice."""
        # Add a duplicate line for LeBron points
        dupe_response = {**SAMPLE_UD_RESPONSE}
        dupe_response["over_under_lines"] = list(SAMPLE_UD_RESPONSE["over_under_lines"]) + [
            {
                "stat_value": 26.5,  # slightly different value
                "over_under": {
                    "appearance_stat": {
                        "appearance_id": "a2001",
                        "stat": "points",
                    }
                },
                "options": [],
            },
        ]
        slate = self.service._parse_response(dupe_response, "2026-02-19")
        lebron_pts = [l for l in slate.lines if "lebron" in l.player_name.lower() and l.stat == "pts"]
        assert len(lebron_pts) == 1  # First one wins

    def test_parse_response_empty_data(self):
        slate = self.service._parse_response({}, "2026-02-19")
        assert slate.game_date == "2026-02-19"
        assert slate.lines == []
        assert slate.player_count == 0

    def test_parse_response_missing_player(self):
        """Lines referencing unknown player_id should be skipped."""
        bad_response = {
            "players": [],  # No players
            "appearances": [
                {"id": "a9999", "player_id": "p9999", "team_id": "t100"},
            ],
            "over_under_lines": [
                {
                    "stat_value": 20.0,
                    "over_under": {
                        "appearance_stat": {
                            "appearance_id": "a9999",
                            "stat": "points",
                        }
                    },
                    "options": [],
                },
            ],
            "games": [],
        }
        slate = self.service._parse_response(bad_response, "2026-02-19")
        assert len(slate.lines) == 0

    def test_parse_games(self):
        slate = self.service._parse_response(SAMPLE_UD_RESPONSE, "2026-02-19")
        assert len(slate.games) == 1
        assert slate.games[0].game_time == "2026-02-19T19:00:00Z"


# ── Tests: Public API methods (with mocked HTTP) ─────────────────────

class TestGetPickemLines:
    def setup_method(self):
        self.service = UnderdogApiService(cache_ttl=0)

    def test_get_pickem_lines_success(self):
        with _mock_httpx_get(SAMPLE_UD_RESPONSE):
            slate = self.service.get_pickem_lines("2026-02-19")
        assert isinstance(slate, UnderdogSlate)
        assert len(slate.lines) > 0

    def test_get_pickem_lines_default_date(self):
        with _mock_httpx_get(SAMPLE_UD_RESPONSE):
            slate = self.service.get_pickem_lines()
        assert slate.game_date  # Should have today's date

    def test_get_pickem_lines_http_error(self):
        with _mock_httpx_get(status_code=500):
            slate = self.service.get_pickem_lines("2026-02-19")
        assert isinstance(slate, UnderdogSlate)
        assert slate.lines == []

    def test_get_pickem_lines_timeout(self):
        import httpx as _httpx
        with _mock_httpx_get(raise_exc=_httpx.TimeoutException("timeout")):
            slate = self.service.get_pickem_lines("2026-02-19")
        assert isinstance(slate, UnderdogSlate)
        assert slate.lines == []

    def test_get_pickem_lines_connection_error(self):
        with _mock_httpx_get(raise_exc=httpx_conn_error()):
            slate = self.service.get_pickem_lines("2026-02-19")
        assert isinstance(slate, UnderdogSlate)
        assert slate.lines == []


class TestGetPlayerLine:
    def setup_method(self):
        self.service = UnderdogApiService(cache_ttl=0)

    def test_get_player_line_found(self):
        with _mock_httpx_get(SAMPLE_UD_RESPONSE):
            line = self.service.get_player_line("LeBron James", "pts", "2026-02-19")
        assert line is not None
        assert line.line == 25.5
        assert line.stat == "pts"

    def test_get_player_line_not_found(self):
        with _mock_httpx_get(SAMPLE_UD_RESPONSE):
            line = self.service.get_player_line("Nobody Real", "pts", "2026-02-19")
        assert line is None

    def test_get_player_line_case_insensitive(self):
        with _mock_httpx_get(SAMPLE_UD_RESPONSE):
            line = self.service.get_player_line("lebron james", "pts", "2026-02-19")
        assert line is not None

    def test_get_player_lines_all_stats(self):
        with _mock_httpx_get(SAMPLE_UD_RESPONSE):
            lines = self.service.get_player_lines("LeBron James", "2026-02-19")
        # LeBron should have pts, reb, ast, pra
        assert len(lines) == 4
        stats = {l.stat for l in lines}
        assert stats == {"pts", "reb", "ast", "pra"}


# ── Tests: Cache behaviour ────────────────────────────────────────────

class TestCaching:
    def test_cache_prevents_refetch(self):
        service = UnderdogApiService(cache_ttl=3600)  # 1 hour cache
        with _mock_httpx_get(SAMPLE_UD_RESPONSE) as mock_get:
            service.get_pickem_lines("2026-02-19")
            service.get_pickem_lines("2026-02-19")
        # Should only fetch once
        assert mock_get.call_count == 1

    def test_cache_different_dates(self):
        service = UnderdogApiService(cache_ttl=3600)
        with _mock_httpx_get(SAMPLE_UD_RESPONSE) as mock_get:
            service.get_pickem_lines("2026-02-19")
            service.get_pickem_lines("2026-02-20")
        # Should fetch twice (different dates)
        assert mock_get.call_count == 2

    def test_invalidate_cache(self):
        service = UnderdogApiService(cache_ttl=3600)
        with _mock_httpx_get(SAMPLE_UD_RESPONSE) as mock_get:
            service.get_pickem_lines("2026-02-19")
            service.invalidate_cache("2026-02-19")
            service.get_pickem_lines("2026-02-19")
        assert mock_get.call_count == 2

    def test_invalidate_all(self):
        service = UnderdogApiService(cache_ttl=3600)
        with _mock_httpx_get(SAMPLE_UD_RESPONSE):
            service.get_pickem_lines("2026-02-19")
            service.get_pickem_lines("2026-02-20")
        service.invalidate_cache()
        assert len(service._cache) == 0


# ── Tests: Line value extraction ──────────────────────────────────────

class TestLineValueExtraction:
    def setup_method(self):
        self.service = UnderdogApiService()

    def test_stat_value_top_level(self):
        val = self.service._extract_line_value({"stat_value": 25.5})
        assert val == 25.5

    def test_stat_value_nested(self):
        val = self.service._extract_line_value({
            "over_under": {
                "appearance_stat": {"stat_value": 30.0}
            }
        })
        assert val == 30.0

    def test_stat_value_from_options_title(self):
        val = self.service._extract_line_value({
            "options": [{"title": "Higher 22.5"}, {"title": "Lower 22.5"}]
        })
        assert val == 22.5

    def test_no_value_returns_none(self):
        val = self.service._extract_line_value({})
        assert val is None

    def test_stat_value_string(self):
        val = self.service._extract_line_value({"stat_value": "18.5"})
        assert val == 18.5


# ── Tests: Pydantic model validation ─────────────────────────────────

class TestModels:
    def test_pickem_line_creation(self):
        line = UnderdogPickemLine(
            player_name="LeBron James",
            team_abbreviation="LAL",
            stat="pts",
            line=25.5,
        )
        assert line.player_name == "LeBron James"
        assert line.line == 25.5

    def test_slate_defaults(self):
        slate = UnderdogSlate(game_date="2026-02-19")
        assert slate.lines == []
        assert slate.player_count == 0
        assert slate.games == []


# ── Tests: Resilient parsing (API structure changes) ─────────────────


class TestResilientParsing:
    """Verify the parser handles alternative API response structures."""

    def setup_method(self):
        self.service = UnderdogApiService(cache_ttl=0)

    def test_display_name_field(self):
        """Handle API returning display_name instead of first_name/last_name."""
        response = {
            "players": [
                {
                    "id": "p1001",
                    "display_name": "LeBron James",
                    "team_id": "t100",
                    "position_id": "SF",
                },
            ],
            "appearances": [
                {"id": "a2001", "player_id": "p1001", "team_id": "t100"},
            ],
            "over_under_lines": [
                {
                    "stat_value": 25.5,
                    "over_under": {
                        "appearance_stat": {
                            "appearance_id": "a2001",
                            "stat": "points",
                        }
                    },
                    "options": [],
                },
            ],
            "games": [],
            "teams": [{"id": "t100", "abbrev": "LAL"}],
        }
        slate = self.service._parse_response(response, "2026-02-19")
        assert len(slate.lines) == 1
        assert slate.lines[0].player_name == "LeBron James"
        assert slate.lines[0].team_abbreviation == "LAL"

    def test_camel_case_name_fields(self):
        """Handle camelCase firstName/lastName fields."""
        response = {
            "players": [
                {
                    "id": "p1001",
                    "firstName": "Stephen",
                    "lastName": "Curry",
                    "team_id": "t101",
                },
            ],
            "appearances": [
                {"id": "a2001", "player_id": "p1001", "team_id": "t101"},
            ],
            "over_under_lines": [
                {
                    "stat_value": 28.5,
                    "over_under": {
                        "appearance_stat": {
                            "appearance_id": "a2001",
                            "stat": "points",
                        }
                    },
                    "options": [],
                },
            ],
            "games": [],
        }
        slate = self.service._parse_response(response, "2026-02-19")
        assert len(slate.lines) == 1
        assert slate.lines[0].player_name == "Stephen Curry"

    def test_top_level_teams_array(self):
        """Handle team data from a 'teams' top-level array."""
        response = {
            "players": [
                {
                    "id": "p1001",
                    "first_name": "LeBron",
                    "last_name": "James",
                    "team_id": "t100",
                },
            ],
            "appearances": [
                {"id": "a2001", "player_id": "p1001", "team_id": "t100"},
            ],
            "over_under_lines": [
                {
                    "stat_value": 25.5,
                    "over_under": {
                        "appearance_stat": {
                            "appearance_id": "a2001",
                            "stat": "points",
                        }
                    },
                    "options": [],
                },
            ],
            "games": [],
            "teams": [{"id": "t100", "abbreviation": "LAL"}],
        }
        slate = self.service._parse_response(response, "2026-02-19")
        assert len(slate.lines) == 1
        assert slate.lines[0].team_abbreviation == "LAL"

    def test_uuid_player_name_skipped(self):
        """Lines where player name resolves to a UUID should be skipped."""
        response = {
            "players": [
                {
                    "id": "p1001",
                    "first_name": "b824645c-c5b8-4f7c-afe8-dc181c000001",
                    "last_name": "",
                    "team_id": "t100",
                },
            ],
            "appearances": [
                {"id": "a2001", "player_id": "p1001", "team_id": "t100"},
            ],
            "over_under_lines": [
                {
                    "stat_value": 25.5,
                    "over_under": {
                        "appearance_stat": {
                            "appearance_id": "a2001",
                            "stat": "points",
                        }
                    },
                    "options": [],
                },
            ],
            "games": [],
        }
        slate = self.service._parse_response(response, "2026-02-19")
        assert len(slate.lines) == 0  # UUID name should be skipped

    def test_flat_line_structure(self):
        """Handle over_under_lines with flat appearance_id/stat fields."""
        response = {
            "players": [
                {
                    "id": "p1001",
                    "first_name": "LeBron",
                    "last_name": "James",
                    "team_id": "t100",
                },
            ],
            "appearances": [
                {"id": "a2001", "player_id": "p1001", "team_id": "t100"},
            ],
            "over_under_lines": [
                {
                    "stat_value": 25.5,
                    "appearance_id": "a2001",
                    "stat": "points",
                    "options": [],
                },
            ],
            "games": [],
        }
        slate = self.service._parse_response(response, "2026-02-19")
        assert len(slate.lines) == 1
        assert slate.lines[0].player_name == "LeBron James"
        assert slate.lines[0].line == 25.5

    def test_team_abbreviation_from_appearances(self):
        """Handle team abbreviation from appearances array."""
        response = {
            "players": [
                {
                    "id": "p1001",
                    "first_name": "LeBron",
                    "last_name": "James",
                    "team_id": "t100",
                },
            ],
            "appearances": [
                {
                    "id": "a2001",
                    "player_id": "p1001",
                    "team_id": "t100",
                    "team_abbreviation": "LAL",
                },
            ],
            "over_under_lines": [
                {
                    "stat_value": 25.5,
                    "over_under": {
                        "appearance_stat": {
                            "appearance_id": "a2001",
                            "stat": "points",
                        }
                    },
                    "options": [],
                },
            ],
            "games": [],
        }
        slate = self.service._parse_response(response, "2026-02-19")
        assert len(slate.lines) == 1
        assert slate.lines[0].team_abbreviation == "LAL"

    def test_uuid_position_not_displayed(self):
        """UUID-like position values should be replaced with empty string."""
        response = {
            "players": [
                {
                    "id": "p1001",
                    "first_name": "LeBron",
                    "last_name": "James",
                    "team_id": "t100",
                    "position_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
                },
            ],
            "appearances": [
                {"id": "a2001", "player_id": "p1001", "team_id": "t100"},
            ],
            "over_under_lines": [
                {
                    "stat_value": 25.5,
                    "over_under": {
                        "appearance_stat": {
                            "appearance_id": "a2001",
                            "stat": "points",
                        }
                    },
                    "options": [],
                },
            ],
            "games": [],
        }
        slate = self.service._parse_response(response, "2026-02-19")
        assert len(slate.lines) == 1
        assert slate.lines[0].position == ""  # UUID should be filtered out


# ── Tests: UUID detection ────────────────────────────────────────────


class TestUUIDDetection:
    def test_valid_uuid(self):
        assert _looks_like_uuid("b824645c-c5b8-4f7c-afe8-dc181c000001") is True

    def test_non_uuid(self):
        assert _looks_like_uuid("LeBron James") is False
        assert _looks_like_uuid("SF") is False
        assert _looks_like_uuid("LAL") is False

    def test_empty_string(self):
        assert _looks_like_uuid("") is False

    def test_partial_uuid(self):
        # Only 8 hex chars, not a full UUID
        assert _looks_like_uuid("b824645c") is False


# ── Helper: connection error ──────────────────────────────────────────

def httpx_conn_error():
    import httpx as _httpx
    return _httpx.ConnectError("Connection refused")
