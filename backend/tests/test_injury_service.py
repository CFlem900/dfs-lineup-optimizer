"""Unit tests for the consolidated InjuryService (BDL-powered).

Tests cover:
- BDL API fetch (mocked httpx)
- Status normalisation
- Public interface backward compatibility (signatures, return types)
- In-memory cache behaviour
- Hash computation
- DNP decay model
- InjuryImpactAgent format (get_active_injuries)
- BDL team mapping
- Status text parsing
"""

import hashlib
import pytest
from unittest.mock import patch, MagicMock, AsyncMock

from app.services.injury_service import (
    InjuryService,
    OFFICIAL_FACTORS,
    dnp_decay_factor,
    dnp_label,
    DNP_WINDOW,
    DNP_AUTO_OUT_THRESHOLD,
)


# ── Helpers ──────────────────────────────────────────────────────────


def _make_bdl_response(injuries, next_cursor=None):
    """Build a minimal BDL /v1/player_injuries JSON response."""
    data = []
    for pname, team_id, status, desc in injuries:
        first, *rest = pname.split(" ", 1)
        last = rest[0] if rest else ""
        data.append({
            "player": {
                "first_name": first,
                "last_name": last,
                "team_id": team_id,
            },
            "status": status,
            "description": desc,
        })
    meta = {"next_cursor": next_cursor}
    return {"data": data, "meta": meta}


# ── BDL Fetch Tests ─────────────────────────────────────────────────


class TestBDLFetch:
    """Tests for _fetch_injuries_async() and _fetch_injuries()."""

    @patch("app.services.injury_service.httpx.AsyncClient")
    def test_successful_bdl_fetch(self, MockAsyncClient):
        """Valid BDL response parses correctly."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = _make_bdl_response([
            ("Anthony Davis", 14, "Questionable", "Knee - Soreness"),
            ("LeBron James", 14, "Out", "Back - Tightness"),
        ])
        mock_resp.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get.return_value = mock_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        MockAsyncClient.return_value = mock_client

        svc = InjuryService()
        svc._bdl_api_key = "test-key"
        # Pre-set team map to avoid nba_api dependency
        svc._bdl_team_map = {14: (1610612747, "Los Angeles Lakers", "LAL")}

        import asyncio
        result = asyncio.run(svc._fetch_injuries_async())

        assert len(result) == 2
        assert result[0]["player_name"] == "Anthony Davis"
        assert result[0]["team_name"] == "Los Angeles Lakers"
        assert result[0]["injury_status"] == "Questionable"
        assert result[1]["player_name"] == "LeBron James"
        assert result[1]["injury_status"] == "Out"

    def test_no_api_key_returns_empty(self):
        """Without BDL API key, returns empty list."""
        svc = InjuryService()
        svc._bdl_api_key = ""

        import asyncio
        result = asyncio.run(svc._fetch_injuries_async())
        assert result == []

    @patch("app.services.injury_service.httpx.AsyncClient")
    def test_bdl_pagination(self, MockAsyncClient):
        """Cursor-based pagination fetches all pages."""
        page1 = _make_bdl_response(
            [("Player A", 1, "Out", "Knee")],
            next_cursor=42,
        )
        page2 = _make_bdl_response(
            [("Player B", 2, "GTD", "Back")],
            next_cursor=None,
        )

        mock_client = AsyncMock()
        resp1 = MagicMock()
        resp1.json.return_value = page1
        resp1.raise_for_status = MagicMock()
        resp2 = MagicMock()
        resp2.json.return_value = page2
        resp2.raise_for_status = MagicMock()
        mock_client.get.side_effect = [resp1, resp2]
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        MockAsyncClient.return_value = mock_client

        svc = InjuryService()
        svc._bdl_api_key = "test-key"
        svc._bdl_team_map = {
            1: (0, "Team Alpha", "ALP"),
            2: (0, "Team Beta", "BET"),
        }

        import asyncio
        result = asyncio.run(svc._fetch_injuries_async())

        assert len(result) == 2
        assert result[0]["player_name"] == "Player A"
        assert result[1]["player_name"] == "Player B"
        assert mock_client.get.call_count == 2

    @patch("app.services.injury_service.httpx.AsyncClient")
    def test_bdl_http_error_returns_empty(self, MockAsyncClient):
        """HTTP error returns empty list, no crash."""
        import httpx as _httpx

        mock_client = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.raise_for_status.side_effect = _httpx.HTTPStatusError(
            "Unauthorized", request=MagicMock(), response=mock_resp,
        )
        mock_client.get.return_value = mock_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        MockAsyncClient.return_value = mock_client

        svc = InjuryService()
        svc._bdl_api_key = "bad-key"
        svc._bdl_team_map = {}

        import asyncio
        result = asyncio.run(svc._fetch_injuries_async())
        assert result == []

    @patch("app.services.injury_service.httpx.AsyncClient")
    def test_bdl_timeout_returns_empty(self, MockAsyncClient):
        """Timeout returns empty list."""
        import httpx as _httpx

        mock_client = AsyncMock()
        mock_client.get.side_effect = _httpx.TimeoutException("timed out")
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        MockAsyncClient.return_value = mock_client

        svc = InjuryService()
        svc._bdl_api_key = "test-key"
        svc._bdl_team_map = {}

        import asyncio
        result = asyncio.run(svc._fetch_injuries_async())
        assert result == []


# ── Public Interface Tests ───────────────────────────────────────────


class TestPublicInterfaceUnchanged:
    """Verify the public interface remains backward-compatible."""

    def test_get_injuries_returns_player_status_list(self):
        """get_injuries() returns List[PlayerStatus].

        Now mocks _read_injuries_from_db (DB-only path) instead of
        the old _fetch_injuries (live BDL HTTP) since the service no
        longer makes live HTTP calls during lineup generation.
        """
        svc = InjuryService()
        svc._read_injuries_from_db = MagicMock(return_value=[
            {"player": "LeBron James", "team": "Los Angeles Lakers",
             "status": "GTD", "description": "Knee soreness"},
        ])
        result = svc.get_injuries()
        assert len(result) == 1
        assert result[0].player_name == "LeBron James"
        assert result[0].status == "GTD"
        assert result[0].injury_description == "Knee soreness"

    def test_get_team_injuries_filters_correctly(self):
        """get_team_injuries() returns only matching team."""
        svc = InjuryService()
        svc._read_injuries_from_db = MagicMock(return_value=[
            {"player": "A", "team": "Los Angeles Lakers",
             "status": "Out", "description": ""},
            {"player": "B", "team": "Boston Celtics",
             "status": "GTD", "description": ""},
        ])
        result = svc.get_team_injuries("Lakers")
        assert len(result) == 1
        assert result[0].player_name == "A"

    def test_get_all_injuries_returns_everything(self):
        """get_all_injuries() returns unfiltered list."""
        svc = InjuryService()
        svc._read_injuries_from_db = MagicMock(return_value=[
            {"player": "A", "team": "T1",
             "status": "Out", "description": ""},
            {"player": "B", "team": "T2",
             "status": "GTD", "description": ""},
        ])
        result = svc.get_all_injuries()
        assert len(result) == 2

    def test_injury_hash_changes_on_status_change(self):
        """Hash changes when a player's status changes."""
        svc = InjuryService()
        data1 = [{"player": "X", "team": "Y", "description": "", "status": "GTD"}]
        data2 = [{"player": "X", "team": "Y", "description": "", "status": "Out"}]
        h1 = svc._compute_hash(data1)
        h2 = svc._compute_hash(data2)
        assert h1 != h2

    def test_injury_hash_stable_for_same_data(self):
        """Hash is stable when data is the same."""
        svc = InjuryService()
        data = [
            {"player": "A", "team": "T", "description": "", "status": "Out"},
            {"player": "B", "team": "T", "description": "", "status": "GTD"},
        ]
        assert svc._compute_hash(data) == svc._compute_hash(data)

    def test_cache_prevents_refetch(self):
        """Multiple calls within TTL use cached data (DB read, not HTTP)."""
        svc = InjuryService()
        svc._read_injuries_from_db = MagicMock(return_value=[
            {"player": "X", "team": "Y",
             "status": "Out", "description": ""},
        ])
        svc.get_injuries()
        svc.get_injuries()
        # Only one DB read despite two calls (in-memory cache)
        svc._read_injuries_from_db.assert_called_once()

    def test_get_official_factor_known_statuses(self):
        """All canonical statuses return expected factors."""
        assert InjuryService.get_official_factor("Out") == 0.00
        assert InjuryService.get_official_factor("Doubtful") == 0.15
        assert InjuryService.get_official_factor("GTD") == 0.54
        assert InjuryService.get_official_factor("Questionable") == 0.64
        assert InjuryService.get_official_factor("Available") == 1.00

    def test_get_official_factor_unknown_defaults_to_questionable(self):
        """Unknown status defaults to Questionable factor."""
        assert InjuryService.get_official_factor("SomeUnknown") == 0.64

    def test_get_all_factors(self):
        """get_all_factors() returns a copy of the table."""
        factors = InjuryService.get_all_factors()
        assert "Out" in factors
        assert "Available" in factors
        assert factors["Out"] == 0.00


# ── DNP Decay Model Tests ───────────────────────────────────────────


class TestDNPDecay:
    """Tests for the DNP decay model functions."""

    def test_zero_dnps(self):
        assert dnp_decay_factor(0) == 1.0

    def test_one_dnp(self):
        assert dnp_decay_factor(1) == 0.80

    def test_two_dnps(self):
        assert dnp_decay_factor(2) == 0.60

    def test_three_dnps(self):
        assert dnp_decay_factor(3) == 0.40

    def test_four_dnps(self):
        assert dnp_decay_factor(4) == 0.20

    def test_five_dnps_auto_out(self):
        assert dnp_decay_factor(5) == 0.0

    def test_six_dnps_still_zero(self):
        assert dnp_decay_factor(6) == 0.0

    def test_label_zero(self):
        assert dnp_label(0) == ""

    def test_label_mid(self):
        assert dnp_label(3) == "DNP-CD/3"

    def test_label_auto_out(self):
        assert dnp_label(5) == "DNP-CD/AUTO-OUT"

    def test_constants(self):
        assert DNP_WINDOW == 5
        assert DNP_AUTO_OUT_THRESHOLD == 5


# ── Status Normalisation Tests ──────────────────────────────────────


class TestNormaliseStatus:
    """Tests for _normalise_status() and _parse_status_text()."""

    def test_normalise_out(self):
        assert InjuryService._normalise_status("Out") == "Out"
        assert InjuryService._normalise_status("out") == "Out"

    def test_normalise_gtd_variants(self):
        assert InjuryService._normalise_status("GTD") == "GTD"
        assert InjuryService._normalise_status("Game Time Decision") == "GTD"
        assert InjuryService._normalise_status("Day-To-Day") == "GTD"
        assert InjuryService._normalise_status("day to day") == "GTD"

    def test_normalise_available(self):
        assert InjuryService._normalise_status("Available") == "Available"
        assert InjuryService._normalise_status("Probable") == "Available"

    def test_normalise_unknown_defaults_to_questionable(self):
        assert InjuryService._normalise_status("SomeRandomStatus") == "Questionable"

    def test_parse_expected_to_play(self):
        assert InjuryService._parse_status_text("Expected to play tonight") == "Available"

    def test_parse_probable(self):
        assert InjuryService._parse_status_text("Probable (Knee)") == "Available"

    def test_parse_day_to_day(self):
        assert InjuryService._parse_status_text("Day To Day (Back)") == "GTD"

    def test_parse_gtd(self):
        assert InjuryService._parse_status_text("GTD - Ankle") == "GTD"

    def test_parse_out_for_season(self):
        assert InjuryService._parse_status_text("Out For Season (ACL)") == "Out"

    def test_parse_ruled_out(self):
        assert InjuryService._parse_status_text("Ruled out for tonight") == "Out"

    def test_parse_suspended(self):
        assert InjuryService._parse_status_text("Suspended - 25 games") == "Out"

    def test_parse_inactive(self):
        assert InjuryService._parse_status_text("Inactive - Coach's decision") == "Out"

    def test_parse_questionable(self):
        assert InjuryService._parse_status_text("Questionable (Hamstring)") == "Questionable"

    def test_parse_doubtful(self):
        assert InjuryService._parse_status_text("Doubtful (Knee)") == "Doubtful"

    def test_parse_out_parenthetical(self):
        assert InjuryService._parse_status_text("Out (Knee) - Surgery") == "Out"

    def test_parse_default_questionable(self):
        assert InjuryService._parse_status_text("Some unknown description") == "Questionable"


# ── Get Active Injuries Tests ────────────────────────────────────────


class TestGetActiveInjuries:
    """Tests for get_active_injuries() Agent 3 format."""

    def test_excludes_available(self):
        """Available players are excluded from active injuries."""
        svc = InjuryService()
        svc._read_injuries_from_db = MagicMock(return_value=[
            {"player": "Healthy Guy", "team": "Team",
             "status": "Available", "description": ""},
            {"player": "Hurt Guy", "team": "Team",
             "status": "Out", "description": "Knee"},
        ])
        result = svc.get_active_injuries()
        assert len(result) == 1
        assert result[0]["player_name"] == "Hurt Guy"

    def test_sorted_by_severity(self):
        """Results sorted: Out -> Doubtful -> GTD -> Questionable."""
        svc = InjuryService()
        svc._read_injuries_from_db = MagicMock(return_value=[
            {"player": "Q", "team": "T",
             "status": "Questionable", "description": ""},
            {"player": "O", "team": "T",
             "status": "Out", "description": ""},
            {"player": "G", "team": "T",
             "status": "GTD", "description": ""},
        ])
        result = svc.get_active_injuries()
        assert result[0]["status"] == "Out"
        assert result[1]["status"] == "GTD"
        assert result[2]["status"] == "Questionable"

    def test_has_required_fields(self):
        """Each entry has all required fields."""
        svc = InjuryService()
        svc._read_injuries_from_db = MagicMock(return_value=[
            {"player": "Joel Embiid", "team": "Philadelphia 76ers",
             "status": "GTD", "description": "Knee soreness"},
        ])
        result = svc.get_active_injuries()
        assert len(result) == 1
        entry = result[0]
        assert "player_name" in entry
        assert "status" in entry
        assert "injury_description" in entry
        assert "official_factor" in entry
        assert "p_plays" in entry
        assert "e_minutes_if_active" in entry
        assert "team_name" in entry
        assert "last_updated" in entry

    def test_filters_by_team(self):
        """Team filter works correctly."""
        svc = InjuryService()
        svc._read_injuries_from_db = MagicMock(return_value=[
            {"player": "A", "team": "Los Angeles Lakers",
             "status": "Out", "description": ""},
            {"player": "B", "team": "Boston Celtics",
             "status": "Out", "description": ""},
        ])
        result = svc.get_active_injuries(team_name="Lakers")
        assert len(result) == 1
        assert result[0]["team_name"] == "Los Angeles Lakers"


# ── Official Factor Table Tests ─────────────────────────────────────


class TestOfficialFactors:
    """Tests for OFFICIAL_FACTORS dict consistency."""

    def test_out_is_zero(self):
        assert OFFICIAL_FACTORS["Out"] == 0.00

    def test_available_is_one(self):
        assert OFFICIAL_FACTORS["Available"] == 1.00

    def test_doubtful_is_015(self):
        assert OFFICIAL_FACTORS["Doubtful"] == 0.15

    def test_gtd_is_054(self):
        assert OFFICIAL_FACTORS["GTD"] == 0.54

    def test_questionable_is_064(self):
        assert OFFICIAL_FACTORS["Questionable"] == 0.64

    def test_game_time_decision_matches_gtd(self):
        assert OFFICIAL_FACTORS["Game Time Decision"] == OFFICIAL_FACTORS["GTD"]

    def test_severity_ordering(self):
        """More severe statuses have lower factors."""
        assert OFFICIAL_FACTORS["Out"] < OFFICIAL_FACTORS["Doubtful"]
        assert OFFICIAL_FACTORS["Doubtful"] < OFFICIAL_FACTORS["GTD"]
        assert OFFICIAL_FACTORS["GTD"] < OFFICIAL_FACTORS["Questionable"]
        assert OFFICIAL_FACTORS["Questionable"] < OFFICIAL_FACTORS["Available"]


# ── BDL Team Mapping Tests ──────────────────────────────────────────


class TestBDLTeamMapping:
    """Tests for _ensure_bdl_team_map()."""

    def test_team_map_cached(self):
        """Second call returns cached map."""
        svc = InjuryService()
        svc._bdl_team_map = {1: (0, "Team A", "TA")}
        result = svc._ensure_bdl_team_map()
        assert result == {1: (0, "Team A", "TA")}

    @patch("app.services.injury_service.InjuryService._ensure_bdl_team_map")
    def test_bdl_rows_to_internal(self, mock_map):
        """BDL rows are converted to internal format."""
        rows = [
            {"player_name": "Joel Embiid", "team_name": "Philadelphia 76ers",
             "injury_status": "Out", "reason": "Knee - surgery"},
            {"player_name": "Tyrese Maxey", "team_name": "Philadelphia 76ers",
             "injury_status": "Questionable", "reason": "Ankle"},
        ]
        result = InjuryService._bdl_rows_to_internal(rows)
        assert len(result) == 2
        assert result[0]["player"] == "Joel Embiid"
        assert result[0]["team"] == "Philadelphia 76ers"
        assert result[0]["status"] == "Out"
        assert result[1]["status"] == "Questionable"


# ── Star Change Detection Tests ─────────────────────────────────────


class TestStarChangeDetection:
    """Tests for _detect_star_changes()."""

    def test_new_injury_detected(self):
        """New injury for a star player is detected."""
        previous = {}  # No previous state
        current = [{
            "player_name": "Joel Embiid",
            "team_name": "Philadelphia 76ers",
            "team_id": 1610612755,
            "injury_status": "Out",
            "effective_factor": 0.00,
        }]
        baselines = {"Joel Embiid": 33.2}

        changes = InjuryService._detect_star_changes(previous, current, baselines)
        assert len(changes) == 1
        assert changes[0]["severity"] == "new_injury"
        assert changes[0]["new_status"] == "Out"

    def test_escalation_detected(self):
        """Status escalation (GTD → Out) is detected."""
        previous = {"Joel Embiid": ("GTD", 0.66)}
        current = [{
            "player_name": "Joel Embiid",
            "team_name": "Philadelphia 76ers",
            "team_id": 1610612755,
            "injury_status": "Out",
            "effective_factor": 0.00,
        }]
        baselines = {"Joel Embiid": 33.2}

        changes = InjuryService._detect_star_changes(previous, current, baselines)
        assert len(changes) == 1
        assert changes[0]["severity"] == "escalation"

    def test_non_star_ignored(self):
        """Players below STAR_ANCHOR_THRESHOLD are ignored."""
        previous = {}
        current = [{
            "player_name": "Bench Player",
            "team_name": "Team",
            "team_id": 1,
            "injury_status": "Out",
            "effective_factor": 0.00,
        }]
        baselines = {"Bench Player": 12.5}  # Below 26.0

        changes = InjuryService._detect_star_changes(previous, current, baselines)
        assert len(changes) == 0

    def test_no_change_ignored(self):
        """Same status and factor produces no change."""
        previous = {"Joel Embiid": ("Out", 0.00)}
        current = [{
            "player_name": "Joel Embiid",
            "team_name": "Philadelphia 76ers",
            "team_id": 1610612755,
            "injury_status": "Out",
            "effective_factor": 0.00,
        }]
        baselines = {"Joel Embiid": 33.2}

        changes = InjuryService._detect_star_changes(previous, current, baselines)
        assert len(changes) == 0


# ── Sync Pipeline Tests ─────────────────────────────────────────────


class TestSyncPipeline:
    """Tests for sync_injuries() pipeline logic."""

    def test_sync_no_db_returns_error(self):
        """Without database, returns error dict."""
        svc = InjuryService()
        with patch("app.services.injury_service._pg_connect", return_value=None):
            result = svc.sync_injuries()
        assert result["error"] == "Database unavailable"
        assert result["upserted"] == 0

    def test_sync_no_data_returns_zero(self):
        """No BDL data returns zero upserts."""
        svc = InjuryService()
        svc._fetch_injuries = MagicMock(return_value=[])

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        with patch("app.services.injury_service._redis_connect", return_value=None):
            result = svc.sync_injuries(conn=mock_conn)

        assert result["upserted"] == 0
        assert result["message"] == "No injury data from BDL API"
