"""Tests for OddsService — The Odds API integration.

All tests use synthetic data (no network calls).
"""

import pytest
import time
from unittest.mock import patch, MagicMock

from app.services.odds_service import OddsService, _find_preferred_bookmaker


# ============================================================================
# FIXTURES
# ============================================================================


def _make_odds_event(
    home_team="Duke Blue Devils",
    away_team="North Carolina Tar Heels",
    over_under=145.5,
    spread=-3.5,
    bookmaker_key="draftkings",
):
    """Create a mock Odds API event dict."""
    return {
        "id": "test123",
        "sport_key": "basketball_ncaab",
        "home_team": home_team,
        "away_team": away_team,
        "commence_time": "2026-02-15T23:00:00Z",
        "bookmakers": [
            {
                "key": bookmaker_key,
                "title": bookmaker_key.title(),
                "markets": [
                    {
                        "key": "totals",
                        "outcomes": [
                            {"name": "Over", "price": -110, "point": over_under},
                            {"name": "Under", "price": -110, "point": over_under},
                        ],
                    },
                    {
                        "key": "spreads",
                        "outcomes": [
                            {"name": home_team, "price": -110, "point": spread},
                            {"name": away_team, "price": -110, "point": -spread},
                        ],
                    },
                ],
            }
        ],
    }


# ============================================================================
# BASIC SERVICE BEHAVIOR
# ============================================================================


class TestOddsServiceBasic:
    """Test service initialization and availability."""

    def test_missing_api_key_returns_empty(self):
        """No API key → get_odds returns empty dict."""
        svc = OddsService(api_key="")
        result = svc.get_odds("cbb")
        assert result == {}

    def test_is_available_with_key(self):
        """API key set → is_available returns True."""
        svc = OddsService(api_key="test_key_123")
        assert svc.is_available is True

    def test_is_available_without_key(self):
        """No API key → is_available returns False."""
        svc = OddsService(api_key="")
        assert svc.is_available is False


# ============================================================================
# RESPONSE PARSING
# ============================================================================


class TestParseOddsResponse:
    """Test parsing The Odds API response format."""

    def test_parse_single_event(self):
        """Parse a single event with spreads and totals."""
        events = [_make_odds_event()]
        result = OddsService._parse_odds_response(events)

        assert len(result) == 1
        key = "Duke Blue Devils vs North Carolina Tar Heels"
        assert key in result
        assert result[key]["over_under"] == 145.5
        assert result[key]["spread"] == -3.5

    def test_parse_multiple_events(self):
        """Parse multiple events."""
        events = [
            _make_odds_event("Duke Blue Devils", "UNC Tar Heels", 145.5, -3.5),
            _make_odds_event("Kentucky Wildcats", "Tennessee Volunteers", 138.0, 2.0),
        ]
        result = OddsService._parse_odds_response(events)
        assert len(result) == 2

    def test_parse_missing_bookmakers(self):
        """Event with no bookmakers → skipped."""
        events = [{
            "home_team": "Duke",
            "away_team": "UNC",
            "bookmakers": [],
        }]
        result = OddsService._parse_odds_response(events)
        assert result == {}

    def test_parse_missing_teams(self):
        """Event missing team names → skipped."""
        events = [{
            "home_team": "",
            "away_team": "UNC",
            "bookmakers": [{"key": "dk", "markets": []}],
        }]
        result = OddsService._parse_odds_response(events)
        assert result == {}

    def test_parse_only_totals_no_spread(self):
        """Event with only totals market → over_under present, spread missing."""
        events = [{
            "home_team": "Duke",
            "away_team": "UNC",
            "bookmakers": [{
                "key": "draftkings",
                "markets": [{
                    "key": "totals",
                    "outcomes": [
                        {"name": "Over", "price": -110, "point": 150.0},
                        {"name": "Under", "price": -110, "point": 150.0},
                    ],
                }],
            }],
        }]
        result = OddsService._parse_odds_response(events)
        assert len(result) == 1
        key = "Duke vs UNC"
        assert result[key]["over_under"] == 150.0
        assert "spread" not in result[key]

    def test_parse_only_spreads_no_total(self):
        """Event with only spreads market → spread present, total missing."""
        events = [{
            "home_team": "Duke",
            "away_team": "UNC",
            "bookmakers": [{
                "key": "draftkings",
                "markets": [{
                    "key": "spreads",
                    "outcomes": [
                        {"name": "Duke", "price": -110, "point": -5.0},
                        {"name": "UNC", "price": -110, "point": 5.0},
                    ],
                }],
            }],
        }]
        result = OddsService._parse_odds_response(events)
        assert len(result) == 1
        key = "Duke vs UNC"
        assert result[key]["spread"] == -5.0
        assert "over_under" not in result[key]


# ============================================================================
# BOOKMAKER PREFERENCE
# ============================================================================


class TestBookmakerPreference:
    """Test preferred bookmaker selection."""

    def test_prefers_draftkings(self):
        """DraftKings selected when available."""
        bookmakers = [
            {"key": "betmgm", "markets": []},
            {"key": "draftkings", "markets": []},
            {"key": "fanduel", "markets": []},
        ]
        bm = _find_preferred_bookmaker(bookmakers)
        assert bm["key"] == "draftkings"

    def test_falls_back_to_fanduel(self):
        """FanDuel selected when DraftKings absent."""
        bookmakers = [
            {"key": "betmgm", "markets": []},
            {"key": "fanduel", "markets": []},
        ]
        bm = _find_preferred_bookmaker(bookmakers)
        assert bm["key"] == "fanduel"

    def test_falls_back_to_first(self):
        """First bookmaker when neither DK nor FD available."""
        bookmakers = [
            {"key": "betmgm", "markets": []},
            {"key": "pointsbet", "markets": []},
        ]
        bm = _find_preferred_bookmaker(bookmakers)
        assert bm["key"] == "betmgm"

    def test_empty_bookmakers(self):
        """Empty list → None."""
        assert _find_preferred_bookmaker([]) is None


# ============================================================================
# TEAM NAME MATCHING
# ============================================================================


class TestTeamNameMatching:
    """Test fuzzy team name matching between ESPN and Odds API."""

    def test_exact_match(self):
        """Exact team names → match found."""
        svc = OddsService(api_key="test")
        svc._cache = {
            "cbb:today": (time.time(), {
                "Duke Blue Devils vs North Carolina Tar Heels": {
                    "over_under": 145.5, "spread": -3.5,
                },
            }),
        }
        result = svc.get_game_odds(
            "cbb", "Duke Blue Devils", "North Carolina Tar Heels"
        )
        assert result is not None
        assert result["over_under"] == 145.5

    def test_partial_match(self):
        """Partial team names → match found via word matching."""
        svc = OddsService(api_key="test")
        svc._cache = {
            "cbb:2026-02-15": (time.time(), {
                "Duke Blue Devils vs North Carolina Tar Heels": {
                    "over_under": 145.5, "spread": -3.5,
                },
            }),
        }
        # ESPN might use "Duke" and "North Carolina"
        result = svc.get_game_odds(
            "cbb", "Duke", "North Carolina", "2026-02-15"
        )
        assert result is not None
        assert result["over_under"] == 145.5

    def test_no_match(self):
        """Completely different team names → None."""
        svc = OddsService(api_key="test")
        svc._cache = {
            "cbb:today": (time.time(), {
                "Duke vs UNC": {"over_under": 145.5},
            }),
        }
        result = svc.get_game_odds("cbb", "Kentucky", "Tennessee")
        assert result is None

    def test_no_odds_available(self):
        """No API key → get_game_odds returns None."""
        svc = OddsService(api_key="")
        result = svc.get_game_odds("cbb", "Duke", "UNC")
        assert result is None


# ============================================================================
# CACHING
# ============================================================================


class TestOddsCaching:
    """Test odds caching behavior."""

    def test_cache_hit(self):
        """Second call within TTL returns cached data."""
        svc = OddsService(api_key="test")
        mock_events = [_make_odds_event()]

        call_count = 0

        def mock_fetch(sport):
            nonlocal call_count
            call_count += 1
            return OddsService._parse_odds_response(mock_events)

        with patch.object(svc, "_fetch_odds", side_effect=mock_fetch):
            result1 = svc.get_odds("cbb", "2026-02-15")
            result2 = svc.get_odds("cbb", "2026-02-15")

        assert call_count == 1  # Only fetched once
        assert result1 == result2

    def test_clear_cache(self):
        """clear_cache forces refetch."""
        svc = OddsService(api_key="test")
        mock_events = [_make_odds_event()]

        call_count = 0

        def mock_fetch(sport):
            nonlocal call_count
            call_count += 1
            return OddsService._parse_odds_response(mock_events)

        with patch.object(svc, "_fetch_odds", side_effect=mock_fetch):
            svc.get_odds("cbb", "2026-02-15")
            svc.clear_cache()
            svc.get_odds("cbb", "2026-02-15")

        assert call_count == 2


# ============================================================================
# GRACEFUL DEGRADATION
# ============================================================================


class TestGracefulDegradation:
    """Test graceful degradation on API failures."""

    def test_api_failure_returns_empty(self):
        """HTTP error → empty dict, no crash."""
        svc = OddsService(api_key="test")

        with patch.object(svc, "_fetch_odds", side_effect=Exception("API down")):
            result = svc.get_odds("cbb")

        assert result == {}

    def test_game_odds_with_empty_cache(self):
        """get_game_odds with no cached data → None."""
        svc = OddsService(api_key="test")
        svc._cache = {}

        with patch.object(svc, "_fetch_odds", side_effect=Exception("fail")):
            result = svc.get_game_odds("cbb", "Duke", "UNC")

        assert result is None


# ============================================================================
# VEGAS BLENDING IN CBBGameService
# ============================================================================


class TestVegasBlendingInGameService:
    """Test that CBBGameService correctly blends Vegas odds."""

    def test_projection_with_vegas_total(self):
        """When Vegas total is available, projected total is blended."""
        from app.services.cbb_game_service import CBBGameService

        svc = CBBGameService()
        raw_game = {
            "id": "1",
            "date": "2026-02-15T23:00:00Z",
            "status": "STATUS_SCHEDULED",
            "home": {"id": 150, "name": "Duke", "abbreviation": "DUKE",
                     "score": 0, "records": []},
            "away": {"id": 153, "name": "UNC", "abbreviation": "UNC",
                     "score": 0, "records": []},
        }
        team_stats = {
            150: {
                "season_pace": 72.0, "season_ppg": 80.0, "season_opp_ppg": 68.0,
                "wins": 20, "losses": 5,
                "season_off_rating": 110.0, "season_def_rating": 95.0,
            },
            153: {
                "season_pace": 70.0, "season_ppg": 75.0, "season_opp_ppg": 70.0,
                "wins": 18, "losses": 7,
                "season_off_rating": 108.0, "season_def_rating": 98.0,
            },
        }

        # Without Vegas
        game_no_vegas = svc._project_game(raw_game, team_stats)
        assert game_no_vegas.over_under is None
        model_total = game_no_vegas.projected_total

        # With Vegas O/U = 155 (different from model)
        vegas = {"over_under": 155.0, "spread": -5.0}
        game_with_vegas = svc._project_game(raw_game, team_stats, vegas)
        assert game_with_vegas.over_under == 155.0
        assert game_with_vegas.vegas_spread == -5.0

        # Blended total should be between model and Vegas
        blended_total = game_with_vegas.projected_total
        assert min(model_total, 155.0) <= blended_total <= max(model_total, 155.0)

    def test_projection_without_vegas(self):
        """No Vegas odds → projection uses pure model."""
        from app.services.cbb_game_service import CBBGameService

        svc = CBBGameService()
        raw_game = {
            "id": "2",
            "date": "2026-02-15",
            "status": "STATUS_SCHEDULED",
            "home": {"id": 150, "name": "Duke", "abbreviation": "DUKE",
                     "score": 0, "records": []},
            "away": {"id": 153, "name": "UNC", "abbreviation": "UNC",
                     "score": 0, "records": []},
        }
        team_stats = {}

        game = svc._project_game(raw_game, team_stats)
        assert game.over_under is None
        assert game.vegas_spread is None
        # Should still produce valid projections from league averages
        assert game.projected_total > 0

    def test_ou_edge_calculation(self):
        """over_under_edge = projected_total - vegas_ou."""
        from app.services.cbb_game_service import CBBGameService

        svc = CBBGameService()
        raw_game = {
            "id": "3",
            "date": "2026-02-15",
            "status": "STATUS_SCHEDULED",
            "home": {"id": 150, "name": "Duke", "abbreviation": "DUKE",
                     "score": 0, "records": []},
            "away": {"id": 153, "name": "UNC", "abbreviation": "UNC",
                     "score": 0, "records": []},
        }
        team_stats = {}
        vegas = {"over_under": 140.0}

        game = svc._project_game(raw_game, team_stats, vegas)
        assert game.over_under == 140.0
        assert game.over_under_edge is not None
        # Edge = blended_total - vegas_ou
        expected_edge = round(game.projected_total - 140.0, 1)
        assert game.over_under_edge == expected_edge
