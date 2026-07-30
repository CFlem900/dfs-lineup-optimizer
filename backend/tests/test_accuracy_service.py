"""Tests for AccuracyService — projection accuracy metrics."""

import math
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.accuracy_service import AccuracyService, _safe_mae, _safe_rmse, _safe_bias


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_row(
    *,
    player_id=1,
    player_name="Test Player",
    team_id="BOS",
    position="PG",
    dk_salary=7000,
    projected_minutes=30.0,
    actual_minutes=28.0,
    dk_projected_fp=40.0,
    dk_actual_fp=38.0,
    projected_pts=20.0,
    actual_pts=18.0,
    projected_reb=5.0,
    actual_reb=4.0,
    projected_ast=6.0,
    actual_ast=5.0,
    projected_stl=1.0,
    actual_stl=1.0,
    projected_blk=0.5,
    actual_blk=0.5,
    projected_tov=2.0,
    actual_tov=2.0,
    projected_fg3m=2.0,
    actual_fg3m=2.0,
    game_date=None,
):
    if game_date is None:
        game_date = datetime(2026, 2, 1, tzinfo=timezone.utc)
    return SimpleNamespace(
        player_id=player_id,
        player_name=player_name,
        team_id=team_id,
        position=position,
        dk_salary=dk_salary,
        projected_minutes=projected_minutes,
        actual_minutes=actual_minutes,
        dk_projected_fp=dk_projected_fp,
        dk_actual_fp=dk_actual_fp,
        projected_pts=projected_pts,
        actual_pts=actual_pts,
        projected_reb=projected_reb,
        actual_reb=actual_reb,
        projected_ast=projected_ast,
        actual_ast=actual_ast,
        projected_stl=projected_stl,
        actual_stl=actual_stl,
        projected_blk=projected_blk,
        actual_blk=actual_blk,
        projected_tov=projected_tov,
        actual_tov=actual_tov,
        projected_fg3m=projected_fg3m,
        actual_fg3m=actual_fg3m,
        game_date=game_date,
    )


def _mock_session(rows):
    """Return a context-manager mock whose .execute().scalars().all() yields *rows*."""
    scalars_mock = MagicMock()
    scalars_mock.all.return_value = rows

    result_mock = MagicMock()
    result_mock.scalars.return_value = scalars_mock

    session = AsyncMock()
    session.execute = AsyncMock(return_value=result_mock)

    # Make it work as ``async with get_session() as session:``
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


# ---------------------------------------------------------------------------
# Pure-function tests
# ---------------------------------------------------------------------------


class TestSafeMae:
    def test_safe_mae_with_known_errors(self):
        # MAE = mean of absolute values = (3 + 1 + 2) / 3 = 2.0
        assert _safe_mae([3, -1, 2]) == 2.0

    def test_safe_mae_empty(self):
        assert _safe_mae([]) == 0.0


class TestSafeRmse:
    def test_safe_rmse_with_known_errors(self):
        # RMSE = sqrt((9 + 16) / 2) = sqrt(12.5)
        expected = round(math.sqrt(12.5), 2)
        assert _safe_rmse([3, 4]) == expected

    def test_safe_rmse_empty(self):
        assert _safe_rmse([]) == 0.0


class TestSafeBias:
    def test_safe_bias_positive(self):
        # bias = (2 + 4 + 6) / 3 = 4.0  (over-projecting)
        assert _safe_bias([2, 4, 6]) == 4.0


# ---------------------------------------------------------------------------
# Async method tests  (mock DB layer)
# ---------------------------------------------------------------------------

DB_MODULE = "app.db.database"


class TestGetAccuracySummary:
    """Tests for get_accuracy_summary."""

    @patch(f"{DB_MODULE}.is_db_available", return_value=False)
    async def test_summary_db_unavailable(self, _mock_db):
        svc = AccuracyService()
        result = await svc.get_accuracy_summary()
        assert result == {"error": "Database not available", "records": 0}

    @patch(f"{DB_MODULE}.get_session")
    @patch(f"{DB_MODULE}.is_db_available", return_value=True)
    async def test_summary_empty_data(self, _mock_db, mock_get_session):
        mock_get_session.return_value = _mock_session([])
        svc = AccuracyService()
        result = await svc.get_accuracy_summary()
        assert result["records"] == 0
        assert "message" in result

    @patch(f"{DB_MODULE}.get_session")
    @patch(f"{DB_MODULE}.is_db_available", return_value=True)
    async def test_summary_with_data(self, _mock_db, mock_get_session):
        rows = [
            _make_row(projected_minutes=30, actual_minutes=28),  # err = +2
            _make_row(projected_minutes=25, actual_minutes=27),  # err = -2
            _make_row(projected_minutes=32, actual_minutes=30),  # err = +2
        ]
        mock_get_session.return_value = _mock_session(rows)

        svc = AccuracyService()
        result = await svc.get_accuracy_summary()

        assert result["records"] == 3
        # Minutes errors: [2, -2, 2]
        assert result["minutes"]["mae"] == 2.0          # (2+2+2)/3
        assert result["minutes"]["bias"] == round((2 + -2 + 2) / 3, 2)  # 0.67
        # RMSE = sqrt((4+4+4)/3)
        expected_rmse = round(math.sqrt(12 / 3), 2)
        assert result["minutes"]["rmse"] == expected_rmse


class TestGetAccuracyByPosition:
    """Tests for get_accuracy_by_position — grouping by position."""

    @patch(f"{DB_MODULE}.get_session")
    @patch(f"{DB_MODULE}.is_db_available", return_value=True)
    async def test_by_position_grouping(self, _mock_db, mock_get_session):
        rows = [
            _make_row(position="PG", projected_minutes=30, actual_minutes=28),
            _make_row(position="PG", projected_minutes=26, actual_minutes=24),
            _make_row(position="C", projected_minutes=20, actual_minutes=22),
        ]
        mock_get_session.return_value = _mock_session(rows)

        svc = AccuracyService()
        result = await svc.get_accuracy_by_position()

        positions = result["positions"]
        assert positions["PG"]["samples"] == 2
        assert positions["C"]["samples"] == 1
        # SF, SG, PF should have 0 samples
        assert positions["SF"]["samples"] == 0

        # PG errors: [+2, +2]  -> MAE = 2.0
        assert positions["PG"]["minutes_mae"] == 2.0
        # C errors: [-2]  -> MAE = 2.0
        assert positions["C"]["minutes_mae"] == 2.0


class TestGetAccuracyBySalaryTier:
    """Tests for get_accuracy_by_salary_tier — boundary logic."""

    @patch(f"{DB_MODULE}.get_session")
    @patch(f"{DB_MODULE}.is_db_available", return_value=True)
    async def test_by_salary_tier_boundaries(self, _mock_db, mock_get_session):
        rows = [
            _make_row(dk_salary=9000, projected_minutes=30, actual_minutes=28),  # high (>= 8000)
            _make_row(dk_salary=6000, projected_minutes=25, actual_minutes=23),  # mid (>= 5000)
            _make_row(dk_salary=3000, projected_minutes=20, actual_minutes=18),  # value (< 5000)
        ]
        mock_get_session.return_value = _mock_session(rows)

        svc = AccuracyService()
        result = await svc.get_accuracy_by_salary_tier()

        tiers = result["salary_tiers"]
        assert tiers["high"]["samples"] == 1
        assert tiers["mid"]["samples"] == 1
        assert tiers["value"]["samples"] == 1

        # All three rows have projected - actual = +2, so MAE should be 2.0
        assert tiers["high"]["minutes_mae"] == 2.0
        assert tiers["mid"]["minutes_mae"] == 2.0
        assert tiers["value"]["minutes_mae"] == 2.0


class TestGetAccuracyTimeline:
    """Tests for get_accuracy_timeline — date grouping."""

    @patch(f"{DB_MODULE}.get_session")
    @patch(f"{DB_MODULE}.is_db_available", return_value=True)
    async def test_timeline_date_grouping(self, _mock_db, mock_get_session):
        date1 = datetime(2026, 1, 10, tzinfo=timezone.utc)
        date2 = datetime(2026, 1, 11, tzinfo=timezone.utc)
        rows = [
            _make_row(game_date=date1, projected_minutes=30, actual_minutes=28),
            _make_row(game_date=date1, projected_minutes=26, actual_minutes=24),
            _make_row(game_date=date2, projected_minutes=20, actual_minutes=22),
        ]
        mock_get_session.return_value = _mock_session(rows)

        svc = AccuracyService()
        result = await svc.get_accuracy_timeline()

        timeline = result["timeline"]
        assert len(timeline) == 2

        # First date has 2 rows, second has 1
        day1 = next(t for t in timeline if t["date"] == "2026-01-10")
        day2 = next(t for t in timeline if t["date"] == "2026-01-11")
        assert day1["samples"] == 2
        assert day2["samples"] == 1

        # Day 1 abs errors: |2|, |2| -> MAE = 2.0
        assert day1["minutes_mae"] == 2.0
        # Day 2 abs errors: |−2| -> MAE = 2.0
        assert day2["minutes_mae"] == 2.0


class TestGetPlayerAccuracy:
    """Tests for get_player_accuracy."""

    @patch(f"{DB_MODULE}.get_session")
    @patch(f"{DB_MODULE}.is_db_available", return_value=True)
    async def test_player_accuracy_no_records(self, _mock_db, mock_get_session):
        mock_get_session.return_value = _mock_session([])

        svc = AccuracyService()
        result = await svc.get_player_accuracy(player_id=123)
        assert result == {"player_id": 123, "records": 0}


class TestCaching:
    """Verify that repeated calls return the cached result without re-querying."""

    @patch(f"{DB_MODULE}.get_session")
    @patch(f"{DB_MODULE}.is_db_available", return_value=True)
    async def test_caching_returns_cached_result(self, _mock_db, mock_get_session):
        rows = [
            _make_row(projected_minutes=30, actual_minutes=28),
        ]
        mock_get_session.return_value = _mock_session(rows)

        svc = AccuracyService()

        first = await svc.get_accuracy_summary(days=30)
        assert first["records"] == 1

        # Reset the mock so a second real call would return different data.
        mock_get_session.return_value = _mock_session([])

        second = await svc.get_accuracy_summary(days=30)

        # Should be identical (served from cache) — records still 1, not 0.
        assert second["records"] == 1
        assert second is first
