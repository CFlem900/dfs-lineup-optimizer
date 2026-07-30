"""Tests for API response models, pagination helpers, and endpoint response_model integration."""

import pytest
from datetime import datetime, timezone

from app.models.pagination import (
    PaginationMeta,
    CursorPage,
    encode_cursor,
    decode_cursor,
)
from app.models.responses import (
    AccuracyPaginatedResponse,
    AccuracyRecord,
    AccuracySummaryResponse,
    AccuracyMetrics,
    AccuracyByPositionResponse,
    PositionAccuracy,
    AccuracyBySalaryTierResponse,
    SalaryTierAccuracy,
    AccuracyTimelineResponse,
    AccuracyTimelineEntry,
    PlayerAccuracyResponse,
    PlayerAccuracyGame,
    CalibrationHistoryResponse,
    CalibrationHistoryEntry,
    PlayerPoolResponse,
    TeamsResponse,
    ConferencesResponse,
    HealthResponse,
    PipelineStatusResponse,
)
from app.models.lineup import PlayerPoolEntry


# ══════════════════════════════════════════════════════════════════
# Cursor encode / decode
# ══════════════════════════════════════════════════════════════════


class TestCursorHelpers:
    def test_encode_decode_roundtrip(self):
        """Encode a cursor and decode it; verify fields match."""
        original = {"game_date": "2026-02-20", "id": 42}
        encoded = encode_cursor(**original)
        decoded = decode_cursor(encoded)
        assert decoded["game_date"] == "2026-02-20"
        assert decoded["id"] == 42

    def test_encode_decode_with_offset(self):
        """Offset-based cursor for in-memory lists."""
        encoded = encode_cursor(offset=100)
        decoded = decode_cursor(encoded)
        assert decoded["offset"] == 100

    def test_encode_decode_datetime(self):
        """datetime objects are serialised via default=str."""
        dt = datetime(2026, 2, 20, 15, 30, 0, tzinfo=timezone.utc)
        encoded = encode_cursor(game_date=dt, id=7)
        decoded = decode_cursor(encoded)
        assert "2026-02-20" in decoded["game_date"]
        assert decoded["id"] == 7

    def test_decode_invalid_cursor_raises(self):
        """Invalid base64 / JSON should raise."""
        with pytest.raises(Exception):
            decode_cursor("not-valid-base64!!!")


# ══════════════════════════════════════════════════════════════════
# PaginationMeta model
# ══════════════════════════════════════════════════════════════════


class TestPaginationMeta:
    def test_has_more_true(self):
        meta = PaginationMeta(limit=50, has_more=True, next_cursor="abc123")
        assert meta.has_more is True
        assert meta.next_cursor == "abc123"

    def test_has_more_false(self):
        meta = PaginationMeta(limit=50, has_more=False)
        assert meta.has_more is False
        assert meta.next_cursor is None


# ══════════════════════════════════════════════════════════════════
# Accuracy response models
# ══════════════════════════════════════════════════════════════════


class TestAccuracyResponses:
    def test_accuracy_paginated_response(self):
        """Build an AccuracyPaginatedResponse and verify serialization."""
        records = [
            AccuracyRecord(
                player_id=1,
                player_name="LeBron James",
                team_id=1610612747,
                game_date="2026-02-20",
                projected_minutes=34.5,
                actual_minutes=36.0,
                error=1.5,
                position="SF",
                was_injured=False,
            ),
        ]
        resp = AccuracyPaginatedResponse(
            total_records=1,
            mean_absolute_error=1.5,
            root_mean_squared_error=1.5,
            records=records,
            pagination=PaginationMeta(limit=50, has_more=False),
        )
        d = resp.model_dump()
        assert d["total_records"] == 1
        assert d["records"][0]["player_name"] == "LeBron James"
        assert d["pagination"]["has_more"] is False

    def test_accuracy_paginated_response_backward_compatible_keys(self):
        """JSON output has the same top-level keys as the old dict response."""
        resp = AccuracyPaginatedResponse(
            total_records=0,
            mean_absolute_error=0.0,
            root_mean_squared_error=0.0,
            records=[],
            pagination=PaginationMeta(limit=50, has_more=False),
        )
        d = resp.model_dump()
        # Old keys that must still be present
        assert "total_records" in d
        assert "mean_absolute_error" in d
        assert "root_mean_squared_error" in d
        assert "records" in d
        # New key
        assert "pagination" in d

    def test_accuracy_summary_response(self):
        resp = AccuracySummaryResponse(
            records=100,
            period_days=30,
            minutes=AccuracyMetrics(mae=2.5, rmse=3.1, bias=-0.3),
            fantasy_points=AccuracyMetrics(mae=4.0, rmse=5.2, bias=0.5, samples=95),
            per_stat={
                "pts": AccuracyMetrics(mae=3.0, rmse=4.0, bias=0.1, samples=100),
            },
        )
        d = resp.model_dump()
        assert d["period_days"] == 30
        assert d["minutes"]["mae"] == 2.5
        assert d["fantasy_points"]["samples"] == 95

    def test_accuracy_by_position_response(self):
        resp = AccuracyByPositionResponse(
            period_days=30,
            positions={
                "PG": PositionAccuracy(
                    minutes_mae=2.0, minutes_bias=-0.5,
                    fp_mae=3.5, fp_bias=0.2, samples=20,
                ),
            },
        )
        d = resp.model_dump()
        assert "PG" in d["positions"]
        assert d["positions"]["PG"]["samples"] == 20

    def test_accuracy_by_salary_tier_response(self):
        resp = AccuracyBySalaryTierResponse(
            period_days=30,
            salary_tiers={
                "high": SalaryTierAccuracy(
                    minutes_mae=1.8, minutes_bias=0.3,
                    fp_mae=3.0, fp_bias=0.1, samples=15,
                ),
            },
        )
        d = resp.model_dump()
        assert d["salary_tiers"]["high"]["minutes_mae"] == 1.8

    def test_accuracy_timeline_response(self):
        resp = AccuracyTimelineResponse(
            period_days=90,
            timeline=[
                AccuracyTimelineEntry(
                    date="2026-02-20", minutes_mae=2.1, fp_mae=3.5, samples=10,
                ),
            ],
        )
        d = resp.model_dump()
        assert len(d["timeline"]) == 1
        assert d["timeline"][0]["date"] == "2026-02-20"

    def test_player_accuracy_response(self):
        resp = PlayerAccuracyResponse(
            player_id=201566,
            player_name="LeBron James",
            records=10,
            minutes_mae=2.5,
            minutes_bias=-0.3,
            fp_mae=4.0,
            fp_bias=0.5,
            games=[
                PlayerAccuracyGame(
                    game_date="2026-02-20",
                    projected_minutes=34.5,
                    actual_minutes=36.0,
                    projected_fp=45.0,
                    actual_fp=48.0,
                    position="SF",
                    dk_salary=10000,
                ),
            ],
        )
        d = resp.model_dump()
        assert d["player_id"] == 201566
        assert len(d["games"]) == 1
        assert d["games"][0]["dk_salary"] == 10000

    def test_player_accuracy_empty(self):
        """Player with no records — mirrors the service's empty case."""
        resp = PlayerAccuracyResponse(player_id=999, records=0)
        d = resp.model_dump()
        assert d["records"] == 0
        assert d["games"] == []


# ══════════════════════════════════════════════════════════════════
# Calibration history
# ══════════════════════════════════════════════════════════════════


class TestCalibrationHistoryResponse:
    def test_calibration_history_with_pagination(self):
        entries = [
            CalibrationHistoryEntry(
                calibration_key="position_PG_bias",
                category="position",
                adjustment_value=1.05,
                source="tournament",
            ),
        ]
        resp = CalibrationHistoryResponse(
            history=entries,
            count=50,
            pagination=PaginationMeta(
                limit=1, has_more=True,
                next_cursor=encode_cursor(offset=1),
            ),
        )
        d = resp.model_dump()
        assert d["count"] == 50
        assert d["pagination"]["has_more"] is True
        assert d["pagination"]["next_cursor"] is not None
        assert len(d["history"]) == 1

    def test_calibration_history_last_page(self):
        resp = CalibrationHistoryResponse(
            history=[],
            count=0,
            pagination=PaginationMeta(limit=50, has_more=False),
        )
        d = resp.model_dump()
        assert d["pagination"]["has_more"] is False
        assert d["pagination"]["next_cursor"] is None


# ══════════════════════════════════════════════════════════════════
# Player pool response
# ══════════════════════════════════════════════════════════════════


class TestPlayerPoolResponse:
    def test_player_pool_response_serializes(self):
        entry = PlayerPoolEntry(
            player_id=1,
            player_name="Test Player",
            position="PG",
            team_abbreviation="LAL",
            salary=8000,
            projected_fp=35.0,
            floor_fp=25.0,
            ceiling_fp=50.0,
            projected_minutes=32.0,
            eligible_slots=["PG", "G", "UTIL"],
        )
        resp = PlayerPoolResponse(players=[entry], count=1)
        d = resp.model_dump()
        assert d["count"] == 1
        assert d["players"][0]["player_name"] == "Test Player"
        assert d["players"][0]["salary"] == 8000

    def test_player_pool_response_empty(self):
        resp = PlayerPoolResponse(players=[], count=0)
        d = resp.model_dump()
        assert d["count"] == 0
        assert d["players"] == []


# ══════════════════════════════════════════════════════════════════
# Teams responses
# ══════════════════════════════════════════════════════════════════


class TestTeamsResponses:
    def test_teams_response(self):
        resp = TeamsResponse(
            teams=[{"id": 1, "full_name": "Lakers", "abbreviation": "LAL"}],
            sport="nba",
        )
        d = resp.model_dump()
        assert d["sport"] == "nba"
        assert len(d["teams"]) == 1

    def test_conferences_response(self):
        resp = ConferencesResponse(
            conferences={"Big 12": [{"id": 100, "full_name": "Kansas"}]},
            sport="cbb",
        )
        d = resp.model_dump()
        assert "Big 12" in d["conferences"]


# ══════════════════════════════════════════════════════════════════
# Health & pipeline
# ══════════════════════════════════════════════════════════════════


class TestHealthAndPipelineResponses:
    def test_health_response(self):
        """HealthResponse accepts the same dict the /health endpoint returns."""
        data = {
            "status": "healthy",
            "cache_connected": True,
            "db_connected": True,
            "ai_available": False,
            "nba_api_circuit_breaker": "closed",
            "environment": "development",
            "migration_current": True,
        }
        resp = HealthResponse(**data)
        d = resp.model_dump()
        assert d["status"] == "healthy"
        assert d["migration_current"] is True

    def test_health_response_defaults(self):
        """HealthResponse fills in defaults for optional fields."""
        resp = HealthResponse(
            status="healthy",
            cache_connected=False,
            db_connected=False,
            ai_available=False,
            environment="test",
        )
        assert resp.nba_api_circuit_breaker == "unknown"
        assert resp.migration_current is None

    def test_pipeline_status_response(self):
        data = {
            "last_run_at": "2026-02-20T03:00:00+00:00",
            "last_status": "success",
            "last_result": {"rows_ingested": 150},
            "last_error": None,
            "total_runs": 5,
            "next_scheduled_run": "2026-02-21T03:00:00-05:00",
        }
        resp = PipelineStatusResponse(**data)
        d = resp.model_dump()
        assert d["total_runs"] == 5
        assert d["last_result"]["rows_ingested"] == 150

    def test_pipeline_status_response_defaults(self):
        resp = PipelineStatusResponse()
        assert resp.last_status == "idle"
        assert resp.total_runs == 0


# ══════════════════════════════════════════════════════════════════
# CursorPage generic
# ══════════════════════════════════════════════════════════════════


class TestCursorPage:
    def test_cursor_page_with_string_items(self):
        page = CursorPage[str](
            items=["a", "b", "c"],
            pagination=PaginationMeta(limit=10, has_more=False),
        )
        d = page.model_dump()
        assert d["items"] == ["a", "b", "c"]
        assert d["pagination"]["has_more"] is False
