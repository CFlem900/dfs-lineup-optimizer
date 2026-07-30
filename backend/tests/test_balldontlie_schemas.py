"""Tests for BDL API Pydantic schemas.

Validates that ``balldontlie_schemas.py`` correctly parses BDL responses
and gracefully handles missing / malformed fields.
"""

import pytest
from pydantic import ValidationError

from app.models.balldontlie_schemas import (
    BDLGame,
    BDLGamesResponse,
    BDLOddsEntry,
    BDLOddsResponse,
    BDLTeam,
)


# ============================================================================
# BDLTeam
# ============================================================================


class TestBDLTeam:
    """Tests for the BDLTeam schema."""

    def test_valid_team(self):
        team = BDLTeam(id=1, abbreviation="BOS", full_name="Boston Celtics")
        assert team.id == 1
        assert team.abbreviation == "BOS"
        assert team.full_name == "Boston Celtics"

    def test_minimal_team(self):
        """Only id and abbreviation are required."""
        team = BDLTeam(id=2, abbreviation="LAL")
        assert team.city is None
        assert team.full_name is None
        assert team.name is None

    def test_missing_id_rejected(self):
        with pytest.raises(ValidationError):
            BDLTeam(abbreviation="BOS")

    def test_missing_abbreviation_rejected(self):
        with pytest.raises(ValidationError):
            BDLTeam(id=1)


# ============================================================================
# BDLGame
# ============================================================================


class TestBDLGame:
    """Tests for the BDLGame schema."""

    def test_valid_game(self):
        game = BDLGame(
            id=9001,
            home_team=BDLTeam(id=1, abbreviation="BOS"),
            visitor_team=BDLTeam(id=2, abbreviation="LAL"),
            status="Final",
            date="2026-02-24",
        )
        assert game.id == 9001
        assert game.home_team.abbreviation == "BOS"
        assert game.visitor_team.abbreviation == "LAL"

    def test_optional_fields_default_none(self):
        game = BDLGame(
            id=9001,
            home_team=BDLTeam(id=1, abbreviation="BOS"),
            visitor_team=BDLTeam(id=2, abbreviation="LAL"),
        )
        assert game.status is None
        assert game.period is None
        assert game.home_team_score is None
        assert game.season is None

    def test_missing_game_id_rejected(self):
        with pytest.raises(ValidationError):
            BDLGame(
                home_team=BDLTeam(id=1, abbreviation="BOS"),
                visitor_team=BDLTeam(id=2, abbreviation="LAL"),
            )

    def test_from_raw_dict(self):
        """Should parse a dict that matches the BDL API response shape."""
        raw = {
            "id": 9001,
            "home_team": {"id": 1, "abbreviation": "BOS"},
            "visitor_team": {"id": 2, "abbreviation": "LAL"},
            "status": "Scheduled",
            "date": "2026-02-24",
        }
        game = BDLGame.model_validate(raw)
        assert game.home_team.abbreviation == "BOS"


# ============================================================================
# BDLOddsEntry
# ============================================================================


class TestBDLOddsEntry:
    """Tests for the BDLOddsEntry schema."""

    def test_valid_odds_entry(self):
        entry = BDLOddsEntry(
            game_id=9001,
            vendor="DraftKings",
            spread_home_value=-7.5,
            spread_away_value=7.5,
            total_value=224.5,
            moneyline_home_odds=-320,
            moneyline_away_odds=260,
        )
        assert entry.game_id == 9001
        assert entry.spread_home_value == -7.5
        assert entry.total_value == 224.5
        assert entry.vendor == "DraftKings"

    def test_missing_optional_fields(self):
        """Optional fields should default to None."""
        entry = BDLOddsEntry(game_id=9001)
        assert entry.spread_home_value is None
        assert entry.spread_away_value is None
        assert entry.total_value is None
        assert entry.vendor is None
        assert entry.moneyline_home_odds is None

    def test_missing_game_id_rejected(self):
        """game_id is required."""
        with pytest.raises(ValidationError):
            BDLOddsEntry(vendor="DraftKings", total_value=224.5)

    def test_from_raw_dict(self):
        """Should parse a dict matching BDL /v2/odds response."""
        raw = {
            "game_id": 9001,
            "vendor": "FanDuel",
            "spread_home_value": -3.0,
            "total_value": 218.0,
        }
        entry = BDLOddsEntry.model_validate(raw)
        assert entry.spread_home_value == -3.0


# ============================================================================
# Response envelopes
# ============================================================================


class TestBDLResponses:
    """Tests for top-level response envelope schemas."""

    def test_odds_response_empty(self):
        resp = BDLOddsResponse()
        assert resp.data == []
        assert resp.meta is None

    def test_odds_response_with_data(self):
        resp = BDLOddsResponse(
            data=[{"game_id": 9001, "total_value": 224.5}],
            meta={"next_cursor": 123},
        )
        assert len(resp.data) == 1
        assert resp.meta["next_cursor"] == 123

    def test_games_response_empty(self):
        resp = BDLGamesResponse()
        assert resp.data == []

    def test_games_response_with_data(self):
        raw = {
            "data": [
                {
                    "id": 9001,
                    "home_team": {"id": 1, "abbreviation": "BOS"},
                    "visitor_team": {"id": 2, "abbreviation": "LAL"},
                }
            ],
            "meta": {"total_count": 1},
        }
        resp = BDLGamesResponse.model_validate(raw)
        assert len(resp.data) == 1
        assert resp.data[0].home_team.abbreviation == "BOS"
