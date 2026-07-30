"""Extended tests for DKDraftablesService — pool building, invalid player filtering, status detection.

Covers:
  - _fetch_draftables() with mocked HTTP responses (no real network calls)
  - Pool building: filtering zero/null salary, missing names, valid players
  - Name normalization: accented characters, edge cases
  - build_salary_lookup() with duplicates, same-team same-lastname
  - match_salary() cross-team isolation
  - detect_status_changes(): newly out, newly added, newly removed
  - refresh_draftables() force-fetch behavior
  - Cache invalidation edge cases
  - Error handling: HTTP errors, malformed JSON

All network calls are mocked via unittest.mock.
"""

import pytest
from datetime import date
from unittest.mock import patch, MagicMock

import httpx

from app.services.dk_draftables_service import (
    DKDraftablesService,
    DKPlayerSalary,
    _normalize_name,
    DK_DRAFTABLES_URL,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_response(draftables: list, status_code: int = 200):
    """Create a mock httpx response with the given draftables."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = {"draftables": draftables}
    resp.raise_for_status = MagicMock()
    if status_code >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "error", request=MagicMock(), response=resp
        )
    return resp


def _dk_entry(
    player_id: int = 1,
    name: str = "Player",
    position: str = "G",
    salary: int = 5000,
    team: str = "BOS",
    status: str = None,
):
    """Build a single DK draftable API entry dict."""
    return {
        "draftableId": player_id,
        "displayName": name,
        "position": position,
        "salary": salary,
        "teamAbbreviation": team,
        "status": status,
    }


# ---------------------------------------------------------------------------
# _fetch_draftables() — HTTP mocking
# ---------------------------------------------------------------------------

class TestFetchDraftables:
    @patch("app.services.dk_draftables_service.resilient_get")
    def test_valid_response_parsed(self, mock_get):
        mock_get.return_value = _mock_response([
            _dk_entry(1, "LeBron James", "SF", 10500, "LAL"),
            _dk_entry(2, "Anthony Davis", "PF/C", 9800, "LAL"),
        ])
        svc = DKDraftablesService()
        players = svc._fetch_draftables(12345)
        assert len(players) == 2
        assert players[0].display_name == "LeBron James"
        assert players[0].salary == 10500
        assert players[1].position == "PF/C"

    @patch("app.services.dk_draftables_service.resilient_get")
    def test_zero_salary_filtered(self, mock_get):
        mock_get.return_value = _mock_response([
            _dk_entry(1, "Active Player", "G", 5000, "BOS"),
            _dk_entry(2, "Zero Salary", "F", 0, "BOS"),
        ])
        svc = DKDraftablesService()
        players = svc._fetch_draftables(100)
        assert len(players) == 1
        assert players[0].display_name == "Active Player"

    @patch("app.services.dk_draftables_service.resilient_get")
    def test_null_salary_filtered(self, mock_get):
        mock_get.return_value = _mock_response([
            _dk_entry(1, "Active", "G", 5000, "BOS"),
            {"draftableId": 2, "displayName": "Null Sal", "position": "F",
             "salary": None, "teamAbbreviation": "BOS"},
        ])
        svc = DKDraftablesService()
        players = svc._fetch_draftables(100)
        assert len(players) == 1

    @patch("app.services.dk_draftables_service.resilient_get")
    def test_negative_salary_filtered(self, mock_get):
        mock_get.return_value = _mock_response([
            _dk_entry(1, "Good", "G", 5000, "BOS"),
            _dk_entry(2, "Bad", "G", -1000, "BOS"),
        ])
        svc = DKDraftablesService()
        players = svc._fetch_draftables(100)
        assert len(players) == 1

    @patch("app.services.dk_draftables_service.resilient_get")
    def test_empty_name_filtered(self, mock_get):
        mock_get.return_value = _mock_response([
            _dk_entry(1, "Valid Player", "G", 5000, "BOS"),
            {"draftableId": 2, "displayName": "", "position": "F",
             "salary": 4000, "teamAbbreviation": "BOS"},
            {"draftableId": 3, "displayName": None, "position": "C",
             "salary": 4000, "teamAbbreviation": "BOS"},
        ])
        svc = DKDraftablesService()
        players = svc._fetch_draftables(100)
        assert len(players) == 1

    @patch("app.services.dk_draftables_service.resilient_get")
    def test_fallback_to_shortName(self, mock_get):
        """If displayName is missing, shortName is used."""
        mock_get.return_value = _mock_response([
            {"draftableId": 1, "shortName": "J. Doe", "position": "G",
             "salary": 5000, "teamAbbreviation": "BOS"},
        ])
        svc = DKDraftablesService()
        players = svc._fetch_draftables(100)
        assert len(players) == 1
        assert players[0].display_name == "J. Doe"

    @patch("app.services.dk_draftables_service.resilient_get")
    def test_empty_draftables_array(self, mock_get):
        mock_get.return_value = _mock_response([])
        svc = DKDraftablesService()
        players = svc._fetch_draftables(100)
        assert players == []

    @patch("app.services.dk_draftables_service.resilient_get")
    def test_http_error_raises(self, mock_get):
        mock_get.side_effect = httpx.HTTPStatusError(
            "500 Server Error", request=MagicMock(), response=MagicMock(status_code=500)
        )
        svc = DKDraftablesService()
        with pytest.raises(httpx.HTTPStatusError):
            svc._fetch_draftables(100)

    @patch("app.services.dk_draftables_service.resilient_get")
    def test_network_timeout_raises(self, mock_get):
        mock_get.side_effect = httpx.TimeoutException("timeout")
        svc = DKDraftablesService()
        with pytest.raises(httpx.TimeoutException):
            svc._fetch_draftables(100)

    @patch("app.services.dk_draftables_service.resilient_get")
    def test_status_field_preserved(self, mock_get):
        mock_get.return_value = _mock_response([
            _dk_entry(1, "Active Guy", "G", 5000, "BOS", status=None),
            _dk_entry(2, "Injured Guy", "F", 6000, "BOS", status="O"),
            _dk_entry(3, "Q Guy", "C", 7000, "BOS", status="Q"),
        ])
        svc = DKDraftablesService()
        players = svc._fetch_draftables(100)
        assert players[0].status is None
        assert players[1].status == "O"
        assert players[2].status == "Q"

    @patch("app.services.dk_draftables_service.resilient_get")
    def test_url_format(self, mock_get):
        """Ensure the URL is formatted with the draft group ID."""
        mock_get.return_value = _mock_response([])
        svc = DKDraftablesService()
        svc._fetch_draftables(67890)
        called_url = mock_get.call_args[0][0]
        assert "67890" in called_url


# ---------------------------------------------------------------------------
# Name normalization — extended edge cases
# ---------------------------------------------------------------------------

class TestNormalizeNameExtended:
    def test_accented_characters(self):
        assert _normalize_name("Nikola Jokić") == "nikola jokic"

    def test_doncic(self):
        assert _normalize_name("Luka Dončić") == "luka doncic"

    def test_iv_suffix(self):
        assert _normalize_name("Marcus Morris IV") == "marcus morris"

    def test_single_name(self):
        assert _normalize_name("Nene") == "nene"

    def test_hyphenated_name(self):
        # Hyphens are normalized to spaces for cross-source matching:
        # DK may list "Gilgeous-Alexander" while BDL stores "Gilgeous Alexander"
        result = _normalize_name("Shai Gilgeous-Alexander")
        assert result == "shai gilgeous alexander"

    def test_mixed_case(self):
        assert _normalize_name("LEBRON JAMES") == "lebron james"

    def test_trailing_period(self):
        assert _normalize_name("O.G. Anunoby") == "og anunoby"

    def test_empty_string(self):
        assert _normalize_name("") == ""

    def test_whitespace_only(self):
        assert _normalize_name("   ") == ""


# ---------------------------------------------------------------------------
# build_salary_lookup() — edge cases
# ---------------------------------------------------------------------------

class TestBuildSalaryLookupExtended:
    def _make_service(self, players, dg_id=99999):
        svc = DKDraftablesService()
        svc._cache[dg_id] = players
        svc._cache_date = date.today().isoformat()
        return svc

    def test_same_team_same_last_name_highest_salary_wins(self):
        """Two players on the same team with the same last name."""
        players = [
            DKPlayerSalary(1, "Jrue Holiday", "G", 8000, "BOS"),
            DKPlayerSalary(2, "Aaron Holiday", "G", 3500, "BOS"),
        ]
        svc = self._make_service(players)
        lookup = svc.build_salary_lookup(99999)

        # Last-name key: highest salary wins
        assert lookup["holiday:BOS"].salary == 8000
        # Full-name keys are distinct
        assert lookup["jrue holiday:BOS"].salary == 8000
        assert lookup["aaron holiday:BOS"].salary == 3500

    def test_multi_team_same_name(self):
        """Same name on different teams creates separate keys."""
        players = [
            DKPlayerSalary(1, "John Smith", "G", 5000, "BOS"),
            DKPlayerSalary(2, "John Smith", "G", 6000, "LAL"),
        ]
        svc = self._make_service(players)
        lookup = svc.build_salary_lookup(99999)

        assert lookup["john smith:BOS"].salary == 5000
        assert lookup["john smith:LAL"].salary == 6000
        assert lookup["smith:BOS"].salary == 5000
        assert lookup["smith:LAL"].salary == 6000

    def test_accented_name_lookup_key(self):
        players = [DKPlayerSalary(1, "Nikola Jokić", "C", 11500, "DEN")]
        svc = self._make_service(players)
        lookup = svc.build_salary_lookup(99999)
        assert "nikola jokic:DEN" in lookup

    def test_suffix_stripped_in_key(self):
        players = [DKPlayerSalary(1, "Robert Williams III", "C", 5000, "POR")]
        svc = self._make_service(players)
        lookup = svc.build_salary_lookup(99999)
        assert "robert williams:POR" in lookup
        assert "williams:POR" in lookup

    def test_empty_draftables_empty_lookup(self):
        svc = self._make_service([])
        lookup = svc.build_salary_lookup(99999)
        assert lookup == {}

    def test_large_pool_all_valid(self):
        """15 players all with valid data should all appear in lookup."""
        players = [
            DKPlayerSalary(i, f"Player{i} Last{i}", "G", 3000 + i * 500, "BOS")
            for i in range(15)
        ]
        svc = self._make_service(players)
        lookup = svc.build_salary_lookup(99999)
        # Each player should have at least a full-name key
        for i in range(15):
            assert f"player{i} last{i}:BOS" in lookup


# ---------------------------------------------------------------------------
# match_salary() — cross-team isolation
# ---------------------------------------------------------------------------

class TestMatchSalaryExtended:
    def _make_lookup_and_svc(self, players):
        svc = DKDraftablesService()
        svc._cache[99999] = players
        svc._cache_date = date.today().isoformat()
        lookup = svc.build_salary_lookup(99999)
        return lookup, svc

    def test_case_insensitive_team(self):
        """Team abbreviation matching should be case-insensitive."""
        players = [DKPlayerSalary(1, "Test Player", "G", 5000, "BOS")]
        lookup, svc = self._make_lookup_and_svc(players)

        match = svc.match_salary("Test Player", "bos", lookup)
        assert match is not None
        assert match.salary == 5000

    def test_accented_lookup(self):
        """Accented names in our roster match DK's accented names."""
        players = [DKPlayerSalary(1, "Nikola Jokić", "C", 11500, "DEN")]
        lookup, svc = self._make_lookup_and_svc(players)

        # Our roster might not have the accent
        match = svc.match_salary("Nikola Jokic", "DEN", lookup)
        assert match is not None
        assert match.salary == 11500

    def test_suffix_mismatch_still_matches(self):
        """Our roster says 'Gary Trent' but DK says 'Gary Trent Jr.'."""
        players = [DKPlayerSalary(1, "Gary Trent Jr.", "G", 5500, "TOR")]
        lookup, svc = self._make_lookup_and_svc(players)

        match = svc.match_salary("Gary Trent", "TOR", lookup)
        assert match is not None
        assert match.salary == 5500


# ---------------------------------------------------------------------------
# get_draftables() — cache integration
# ---------------------------------------------------------------------------

class TestGetDraftablesCacheIntegration:
    @patch("app.services.dk_draftables_service.resilient_get")
    def test_cache_hit_no_network_call(self, mock_get):
        """Cache hit should not trigger a network call."""
        svc = DKDraftablesService()
        players = [DKPlayerSalary(1, "Cached", "G", 5000, "BOS")]
        svc._cache[100] = players
        svc._cache_date = date.today().isoformat()

        result = svc.get_draftables(100)
        assert len(result) == 1
        assert result[0].display_name == "Cached"
        mock_get.assert_not_called()

    @patch("app.services.dk_draftables_service.resilient_get")
    def test_cache_miss_triggers_fetch(self, mock_get):
        mock_get.return_value = _mock_response([
            _dk_entry(1, "Fresh Player", "G", 7000, "LAL"),
        ])
        svc = DKDraftablesService()
        result = svc.get_draftables(200)
        assert len(result) == 1
        assert result[0].display_name == "Fresh Player"
        mock_get.assert_called_once()

    @patch("app.services.dk_draftables_service.resilient_get")
    def test_different_dg_id_triggers_fetch(self, mock_get):
        """Different draft group IDs should each trigger their own fetch."""
        svc = DKDraftablesService()
        svc._cache[100] = [DKPlayerSalary(1, "DG100", "G", 5000, "BOS")]
        svc._cache_date = date.today().isoformat()

        mock_get.return_value = _mock_response([
            _dk_entry(2, "DG200", "F", 6000, "LAL"),
        ])
        result = svc.get_draftables(200)
        assert result[0].display_name == "DG200"
        mock_get.assert_called_once()

    @patch("app.services.dk_draftables_service.resilient_get")
    def test_error_returns_empty_when_no_cache(self, mock_get):
        """On fetch error with no same-day cache, returns empty list."""
        svc = DKDraftablesService()
        # _is_cache_valid clears the dict on date change, so stale-day
        # entries are gone before the fallback path runs.
        svc._cache_date = "2026-01-01"

        mock_get.side_effect = Exception("network down")
        result = svc.get_draftables(100)
        assert result == []

    @patch("app.services.dk_draftables_service.resilient_get")
    def test_error_returns_same_day_cache(self, mock_get):
        """On fetch error for a NEW DG, same-day cache for another DG is unaffected."""
        svc = DKDraftablesService()
        svc._cache[100] = [DKPlayerSalary(1, "Cached", "G", 5000, "BOS")]
        svc._cache_date = date.today().isoformat()

        mock_get.side_effect = Exception("network down")
        # Fetching a DIFFERENT DG fails — cache for DG 100 is preserved
        result_200 = svc.get_draftables(200)
        assert result_200 == []  # No cache for DG 200

        result_100 = svc.get_draftables(100)
        assert len(result_100) == 1  # DG 100 cache intact
        assert result_100[0].display_name == "Cached"


# ---------------------------------------------------------------------------
# detect_status_changes()
# ---------------------------------------------------------------------------

class TestDetectStatusChanges:
    @patch.object(DKDraftablesService, "refresh_draftables")
    def test_newly_out_detected(self, mock_refresh):
        """Detect player transitioning to Out status."""
        previous = [
            DKPlayerSalary(1, "Player A", "G", 5000, "BOS", status=None),
            DKPlayerSalary(2, "Player B", "F", 6000, "BOS", status="Q"),
        ]
        fresh = [
            DKPlayerSalary(1, "Player A", "G", 5000, "BOS", status="O"),  # Now out!
            DKPlayerSalary(2, "Player B", "F", 6000, "BOS", status="Q"),  # Unchanged
        ]
        mock_refresh.return_value = fresh

        svc = DKDraftablesService()
        changes = svc.detect_status_changes(100, previous)

        assert len(changes["newly_out"]) == 1
        assert changes["newly_out"][0]["player"] == "Player A"
        assert changes["newly_out"][0]["old_status"] == "None"
        assert changes["newly_out"][0]["new_status"] == "O"
        assert len(changes["newly_added"]) == 0
        assert len(changes["newly_removed"]) == 0

    @patch.object(DKDraftablesService, "refresh_draftables")
    def test_newly_added_detected(self, mock_refresh):
        """Detect player added to the pool (emergency call-up)."""
        previous = [
            DKPlayerSalary(1, "Player A", "G", 5000, "BOS"),
        ]
        fresh = [
            DKPlayerSalary(1, "Player A", "G", 5000, "BOS"),
            DKPlayerSalary(3, "New Call-Up", "F", 3500, "BOS"),
        ]
        mock_refresh.return_value = fresh

        svc = DKDraftablesService()
        changes = svc.detect_status_changes(100, previous)

        assert len(changes["newly_added"]) == 1
        assert changes["newly_added"][0]["player"] == "New Call-Up"

    @patch.object(DKDraftablesService, "refresh_draftables")
    def test_newly_removed_detected(self, mock_refresh):
        """Detect player removed from the pool (waived, ineligible)."""
        previous = [
            DKPlayerSalary(1, "Player A", "G", 5000, "BOS"),
            DKPlayerSalary(2, "Waived Guy", "F", 4000, "BOS"),
        ]
        fresh = [
            DKPlayerSalary(1, "Player A", "G", 5000, "BOS"),
        ]
        mock_refresh.return_value = fresh

        svc = DKDraftablesService()
        changes = svc.detect_status_changes(100, previous)

        assert len(changes["newly_removed"]) == 1
        assert changes["newly_removed"][0]["player"] == "Waived Guy"

    @patch.object(DKDraftablesService, "refresh_draftables")
    def test_no_changes(self, mock_refresh):
        """No changes in roster → all lists empty."""
        players = [
            DKPlayerSalary(1, "Player A", "G", 5000, "BOS"),
            DKPlayerSalary(2, "Player B", "F", 6000, "BOS"),
        ]
        mock_refresh.return_value = players

        svc = DKDraftablesService()
        changes = svc.detect_status_changes(100, players)

        assert changes["newly_out"] == []
        assert changes["newly_added"] == []
        assert changes["newly_removed"] == []

    @patch.object(DKDraftablesService, "refresh_draftables")
    def test_out_to_out_not_flagged(self, mock_refresh):
        """Player who was already Out should NOT be flagged as newly out."""
        previous = [DKPlayerSalary(1, "Already Out", "G", 5000, "BOS", status="O")]
        fresh = [DKPlayerSalary(1, "Already Out", "G", 5000, "BOS", status="OUT")]
        mock_refresh.return_value = fresh

        svc = DKDraftablesService()
        changes = svc.detect_status_changes(100, previous)
        assert len(changes["newly_out"]) == 0

    @patch.object(DKDraftablesService, "refresh_draftables")
    def test_combined_changes(self, mock_refresh):
        """Detect multiple types of changes simultaneously."""
        previous = [
            DKPlayerSalary(1, "Goes Out", "G", 5000, "BOS", status=None),
            DKPlayerSalary(2, "Gets Removed", "F", 4000, "BOS"),
            DKPlayerSalary(3, "Stays", "C", 7000, "BOS"),
        ]
        fresh = [
            DKPlayerSalary(1, "Goes Out", "G", 5000, "BOS", status="O"),
            DKPlayerSalary(3, "Stays", "C", 7000, "BOS"),
            DKPlayerSalary(4, "New Addition", "G", 3500, "BOS"),
        ]
        mock_refresh.return_value = fresh

        svc = DKDraftablesService()
        changes = svc.detect_status_changes(100, previous)

        assert len(changes["newly_out"]) == 1
        assert len(changes["newly_removed"]) == 1
        assert len(changes["newly_added"]) == 1


# ---------------------------------------------------------------------------
# refresh_draftables() — force-fetch behavior
# ---------------------------------------------------------------------------

class TestRefreshDraftables:
    @patch("app.services.dk_draftables_service.resilient_get")
    def test_force_refresh_bypasses_cache(self, mock_get):
        svc = DKDraftablesService()
        svc._cache[100] = [DKPlayerSalary(1, "Old", "G", 5000, "BOS")]
        svc._cache_date = date.today().isoformat()

        mock_get.return_value = _mock_response([
            _dk_entry(1, "Updated", "G", 5000, "BOS"),
        ])

        result = svc.refresh_draftables(100)
        assert result[0].display_name == "Updated"
        mock_get.assert_called_once()

    @patch("app.services.dk_draftables_service.resilient_get")
    def test_force_refresh_error_returns_cache(self, mock_get):
        """On error, force-refresh returns cached data."""
        svc = DKDraftablesService()
        svc._cache[100] = [DKPlayerSalary(1, "Cached", "G", 5000, "BOS")]

        mock_get.side_effect = Exception("timeout")
        result = svc.refresh_draftables(100)
        assert len(result) == 1
        assert result[0].display_name == "Cached"

    @patch("app.services.dk_draftables_service.resilient_get")
    def test_force_refresh_updates_cache(self, mock_get):
        """After force-refresh, cache should contain updated data."""
        svc = DKDraftablesService()
        mock_get.return_value = _mock_response([
            _dk_entry(1, "Fresh", "G", 5500, "BOS"),
        ])

        svc.refresh_draftables(100)

        # Now a normal get_draftables should return the refreshed data
        svc._cache_date = date.today().isoformat()
        result = svc.get_draftables(100)
        assert result[0].display_name == "Fresh"


# ---------------------------------------------------------------------------
# Pool building integration: full pipeline
# ---------------------------------------------------------------------------

class TestPoolBuildingPipeline:
    """Test the full DK pool building pipeline: fetch → filter → lookup → match."""

    @patch("app.services.dk_draftables_service.resilient_get")
    def test_realistic_slate(self, mock_get):
        """Simulate a realistic 8-game slate with various edge cases."""
        draftables = [
            # Valid players
            _dk_entry(1, "LeBron James", "SF/PF", 10500, "LAL"),
            _dk_entry(2, "Nikola Jokić", "C", 11500, "DEN"),
            _dk_entry(3, "Gary Trent Jr.", "SG", 5500, "TOR"),
            _dk_entry(4, "P.J. Washington", "PF", 6200, "DAL"),
            _dk_entry(5, "Robert Williams III", "C", 4800, "POR"),
            # Invalid: zero salary (end-of-bench)
            _dk_entry(6, "Bench Warmer", "G", 0, "BOS"),
            # Invalid: no name
            {"draftableId": 7, "displayName": "", "position": "F",
             "salary": 3500, "teamAbbreviation": "MIA"},
            # Valid: with status
            _dk_entry(8, "Questionable Guy", "G", 5000, "BOS", status="Q"),
            _dk_entry(9, "Out Player", "F", 6000, "BOS", status="O"),
        ]
        mock_get.return_value = _mock_response(draftables)

        svc = DKDraftablesService()
        players = svc.get_draftables(12345)

        # 7 valid (5 normal + 2 with status), 2 filtered
        assert len(players) == 7
        names = [p.display_name for p in players]
        assert "LeBron James" in names
        assert "Nikola Jokić" in names
        assert "Gary Trent Jr." in names
        assert "Bench Warmer" not in names  # Filtered (zero salary)

        # Build lookup and verify matching
        lookup = svc.build_salary_lookup(12345)

        # Full name match
        match = svc.match_salary("LeBron James", "LAL", lookup)
        assert match is not None
        assert match.salary == 10500

        # Accented name match
        match = svc.match_salary("Nikola Jokic", "DEN", lookup)
        assert match is not None
        assert match.salary == 11500

        # Suffix-stripped match
        match = svc.match_salary("Gary Trent", "TOR", lookup)
        assert match is not None

        # Status-bearing player still in pool
        match = svc.match_salary("Out Player", "BOS", lookup)
        assert match is not None
        assert match.status == "O"

    @patch("app.services.dk_draftables_service.resilient_get")
    def test_all_invalid_players(self, mock_get):
        """Slate with only invalid players returns empty pool."""
        draftables = [
            _dk_entry(1, "Zero Sal", "G", 0, "BOS"),
            _dk_entry(2, "", "F", 5000, "BOS"),
            {"draftableId": 3, "position": "C", "salary": None,
             "teamAbbreviation": "BOS"},
        ]
        mock_get.return_value = _mock_response(draftables)

        svc = DKDraftablesService()
        players = svc.get_draftables(12345)
        assert players == []

    @patch("app.services.dk_draftables_service.resilient_get")
    def test_duplicate_players_different_positions(self, mock_get):
        """Some DK slates list the same player in multiple positions."""
        draftables = [
            _dk_entry(1, "Versatile Player", "PG", 8000, "BOS"),
            _dk_entry(1, "Versatile Player", "SG", 8000, "BOS"),
        ]
        mock_get.return_value = _mock_response(draftables)

        svc = DKDraftablesService()
        players = svc.get_draftables(12345)
        # Both entries are valid (DK does this for multi-position eligibility)
        assert len(players) == 2
