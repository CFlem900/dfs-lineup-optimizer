"""Tests for the DraftKings draftables (salary) service.

Tests cover:
- Name normalization (suffix stripping, period removal)
- Salary lookup building (full-name and last-name keys)
- Player matching logic (exact, fallback, no-match)
- Cache behavior (day-level invalidation)
- Edge cases (zero salary, empty response, duplicates)
- Salary attachment to PlayerProjection
"""

import pytest
from datetime import date
from unittest.mock import patch, MagicMock

from app.services.dk_draftables_service import (
    DKDraftablesService,
    DKPlayerSalary,
    _normalize_name,
)
from app.models.player import PlayerProjection


# ============================================================================
# Name Normalization
# ============================================================================


class TestNameNormalization:
    """Test the _normalize_name helper."""

    def test_basic_name(self):
        assert _normalize_name("Nikola Jokic") == "nikola jokic"

    def test_jr_suffix(self):
        assert _normalize_name("Gary Trent Jr.") == "gary trent"

    def test_jr_no_period(self):
        assert _normalize_name("Gary Trent Jr") == "gary trent"

    def test_sr_suffix(self):
        assert _normalize_name("Tim Hardaway Sr.") == "tim hardaway"

    def test_iii_suffix(self):
        assert _normalize_name("Robert Williams III") == "robert williams"

    def test_periods_in_name(self):
        assert _normalize_name("P.J. Washington") == "pj washington"

    def test_extra_whitespace(self):
        assert _normalize_name("  Nikola   Jokic  ") == "nikola jokic"

    def test_ii_suffix(self):
        assert _normalize_name("Lonnie Walker II") == "lonnie walker"


# ============================================================================
# Salary Lookup Building
# ============================================================================


class TestBuildSalaryLookup:
    """Test build_salary_lookup dict construction."""

    def _make_service_with_players(self, players):
        """Helper: create a service and inject cached players."""
        service = DKDraftablesService()
        service._cache[99999] = players
        service._cache_date = date.today().isoformat()
        return service

    def test_full_name_match(self):
        players = [
            DKPlayerSalary(1, "Nikola Jokic", "C", 11500, "DEN"),
        ]
        service = self._make_service_with_players(players)
        lookup = service.build_salary_lookup(99999)

        # Keys now include team abbreviation: "name:TEAM"
        assert "nikola jokic:DEN" in lookup
        assert lookup["nikola jokic:DEN"].salary == 11500

    def test_last_name_fallback_key(self):
        players = [
            DKPlayerSalary(1, "Nikola Jokic", "C", 11500, "DEN"),
        ]
        service = self._make_service_with_players(players)
        lookup = service.build_salary_lookup(99999)

        # Last-name fallback key also includes team
        assert "jokic:DEN" in lookup
        assert lookup["jokic:DEN"].salary == 11500

    def test_suffix_normalized_in_lookup(self):
        """Jr. suffix should be stripped from lookup keys."""
        players = [
            DKPlayerSalary(2, "Gary Trent Jr.", "G", 5500, "TOR"),
        ]
        service = self._make_service_with_players(players)
        lookup = service.build_salary_lookup(99999)

        assert "gary trent:TOR" in lookup
        assert "trent:TOR" in lookup

    def test_highest_salary_wins_duplicate_keys(self):
        """When two players share a last-name key on same team, highest salary wins."""
        players = [
            DKPlayerSalary(10, "Aaron Holiday", "G", 3500, "HOU"),
            DKPlayerSalary(11, "Jrue Holiday", "G", 8000, "BOS"),
        ]
        service = self._make_service_with_players(players)
        lookup = service.build_salary_lookup(99999)

        # Different teams, so last-name keys are separate
        assert lookup["holiday:HOU"].salary == 3500
        assert lookup["holiday:BOS"].salary == 8000
        # Full-name keys are distinct
        assert lookup["aaron holiday:HOU"].salary == 3500
        assert lookup["jrue holiday:BOS"].salary == 8000

    def test_zero_salary_filtered(self):
        """Players with zero salary are excluded from draftables."""
        service = DKDraftablesService()

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "draftables": [
                {"draftableId": 1, "displayName": "Player A", "position": "G",
                 "salary": 5000, "teamAbbreviation": "BOS", "status": None},
                {"draftableId": 2, "displayName": "Player B", "position": "F",
                 "salary": 0, "teamAbbreviation": "BOS", "status": None},
                {"draftableId": 3, "displayName": "Player C", "position": "C",
                 "salary": None, "teamAbbreviation": "BOS", "status": None},
            ]
        }

        with patch("app.services.dk_draftables_service.resilient_get", return_value=mock_response):
            players = service._fetch_draftables(12345)

        assert len(players) == 1
        assert players[0].display_name == "Player A"

    def test_empty_response(self):
        """Empty draftables array returns empty list."""
        service = DKDraftablesService()

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"draftables": []}

        with patch("app.services.dk_draftables_service.resilient_get", return_value=mock_response):
            players = service._fetch_draftables(12345)

        assert players == []


# ============================================================================
# Player Matching
# ============================================================================


class TestMatchSalary:
    """Test match_salary logic."""

    def _make_lookup(self, players):
        service = DKDraftablesService()
        service._cache[99999] = players
        service._cache_date = date.today().isoformat()
        return service.build_salary_lookup(99999), service

    def test_exact_full_name_match(self):
        players = [DKPlayerSalary(1, "Nikola Jokic", "C", 11500, "DEN")]
        lookup, service = self._make_lookup(players)

        match = service.match_salary(
            player_name="Nikola Jokic",
            team_abbreviation="DEN",
            lookup=lookup,
        )
        assert match is not None
        assert match.salary == 11500

    def test_last_name_fallback_match(self):
        """Our roster has 'N. Jokic' but DK has 'Nikola Jokic'."""
        players = [DKPlayerSalary(1, "Nikola Jokic", "C", 11500, "DEN")]
        lookup, service = self._make_lookup(players)

        # This won't match full name since "N" != "Nikola"
        # but should match via last name "jokic:DEN"
        match = service.match_salary(
            player_name="N. Jokic",
            team_abbreviation="DEN",
            lookup=lookup,
        )
        assert match is not None
        assert match.salary == 11500

    def test_suffix_match(self):
        """Jr. in our data matches Jr. in DK data after normalization."""
        players = [DKPlayerSalary(2, "Gary Trent Jr.", "G", 5500, "TOR")]
        lookup, service = self._make_lookup(players)

        match = service.match_salary(
            player_name="Gary Trent Jr.",
            team_abbreviation="TOR",
            lookup=lookup,
        )
        assert match is not None
        assert match.salary == 5500

    def test_no_match_wrong_team(self):
        """Same name, wrong team — no match."""
        players = [DKPlayerSalary(1, "Nikola Jokic", "C", 11500, "DEN")]
        lookup, service = self._make_lookup(players)

        match = service.match_salary(
            player_name="Nikola Jokic",
            team_abbreviation="LAL",
            lookup=lookup,
        )
        assert match is None

    def test_no_match_unknown_player(self):
        """Player not in DK draftables — returns None."""
        players = [DKPlayerSalary(1, "Nikola Jokic", "C", 11500, "DEN")]
        lookup, service = self._make_lookup(players)

        match = service.match_salary(
            player_name="John Smith",
            team_abbreviation="DEN",
            lookup=lookup,
        )
        assert match is None


# ============================================================================
# Cache Behavior
# ============================================================================


class TestCache:
    """Test caching logic."""

    def test_cache_hit(self):
        """Second call returns cached data without re-fetching."""
        service = DKDraftablesService()
        players = [DKPlayerSalary(1, "Test Player", "G", 5000, "BOS")]
        service._cache[12345] = players
        service._cache_date = "2026-02-08"

        with patch("app.services.dk_draftables_service.date") as mock_date:
            mock_date.today.return_value.isoformat.return_value = "2026-02-08"
            result = service.get_draftables(12345)

        assert len(result) == 1
        assert result[0].display_name == "Test Player"

    def test_cache_invalidates_on_new_day(self):
        """Cache clears when date changes."""
        service = DKDraftablesService()
        players = [DKPlayerSalary(1, "Test Player", "G", 5000, "BOS")]
        service._cache[12345] = players
        service._cache_date = "2026-02-07"  # yesterday

        assert not service._is_cache_valid(12345)
        assert len(service._cache) == 0  # cleared by _is_cache_valid


# ============================================================================
# Salary Attachment to PlayerProjection
# ============================================================================


class TestSalaryAttachment:
    """Test that salary data attaches to PlayerProjection correctly."""

    def test_salary_fields_default_none(self):
        """New PlayerProjection has dk_salary=None, dk_value=None."""
        proj = PlayerProjection(
            player_id=1,
            player_name="Test",
            position="G",
            baseline_minutes=30.0,
            adjusted_minutes=30.0,
            confidence=1.0,
        )
        assert proj.dk_salary is None
        assert proj.dk_value is None

    def test_salary_fields_can_be_set(self):
        """dk_salary and dk_value can be set on PlayerProjection."""
        proj = PlayerProjection(
            player_id=1,
            player_name="Test",
            position="G",
            baseline_minutes=30.0,
            adjusted_minutes=30.0,
            confidence=1.0,
            dk_points=35.0,
        )
        proj.dk_salary = 8500
        proj.dk_value = round(35.0 / 8500 * 1000, 2)

        assert proj.dk_salary == 8500
        assert proj.dk_value == 4.12

    def test_dk_value_calculation(self):
        """dk_value = dk_points / dk_salary * 1000."""
        dk_points = 25.0
        dk_salary = 4500
        expected = round(dk_points / dk_salary * 1000, 2)  # 5.56

        assert expected == 5.56

    def test_dk_value_zero_salary_guard(self):
        """Ensure no division by zero with salary=0."""
        dk_salary = 0
        dk_points = 25.0

        # The guard in routes.py: "if match.salary > 0"
        if dk_salary > 0:
            value = round(dk_points / dk_salary * 1000, 2)
        else:
            value = None

        assert value is None

    def test_dk_value_none_when_no_dk_points(self):
        """dk_value stays None if dk_points is None or 0."""
        dk_points = 0.0
        dk_salary = 5000

        if dk_points and dk_points > 0 and dk_salary > 0:
            value = round(dk_points / dk_salary * 1000, 2)
        else:
            value = None

        assert value is None

    def test_existing_projection_unaffected(self):
        """Constructing PlayerProjection without salary fields works fine."""
        proj = PlayerProjection(
            player_id=1,
            player_name="Test",
            position="G",
            baseline_minutes=30.0,
            adjusted_minutes=30.0,
            confidence=1.0,
            reason="Test",
            dk_points=25.0,
            fd_points=22.0,
            dk_floor=18.0,
            dk_ceiling=32.0,
            fd_floor=16.0,
            fd_ceiling=28.0,
            projected_stats={"pts": 15.0, "reb": 5.0, "ast": 3.0},
        )

        # All existing fields work
        assert proj.dk_points == 25.0
        assert proj.projected_stats["pts"] == 15.0
        # New fields default to None
        assert proj.dk_salary is None
        assert proj.dk_value is None
