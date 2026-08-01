"""Tests for PreLockPollingService.

Covers:
  - _minutes_to_nearest_lock(): future/past/no slates
  - _compute_news_hash(): stability and change detection
  - _should_refresh_schedule(): stale/fresh/empty
  - _poll_cycle(): with mocked sync_injuries() and news service
  - get_status(): returns expected shape
  - start/stop lifecycle
"""

import asyncio
import hashlib
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.pre_lock_polling_service import PreLockPollingService
from app.config.constants import (
    PRE_LOCK_WINDOW_MINUTES,
    ACTIVE_POLL_INTERVAL_S,
    DORMANT_POLL_INTERVAL_S,
    SLATE_SCHEDULE_REFRESH_MINUTES,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_service(**kwargs) -> PreLockPollingService:
    """Create service with mocked dependencies."""
    injury_sync = MagicMock()
    injury_sync.sync_injuries.return_value = {
        "hash_changed": False,
        "star_changes": [],
        "optimizer_caches_cleared": 0,
        "prewarm_published": 0,
    }

    news = MagicMock()
    news._cache_timestamp = None
    news.get_news.return_value = ([], False)

    cache = AsyncMock()
    cache._redis = AsyncMock()
    cache.is_connected = True

    return PreLockPollingService(
        injury_service=injury_sync,
        news_service=news,
        cache_service=kwargs.get("cache_service", cache),
    )


class MockNewsItem:
    def __init__(self, item_id: str, relevance: str = "general"):
        self.id = item_id
        self.relevance = relevance


# ---------------------------------------------------------------------------
# Constants validation
# ---------------------------------------------------------------------------

class TestConstants:
    def test_pre_lock_window_is_60(self):
        assert PRE_LOCK_WINDOW_MINUTES == 60

    def test_active_interval_is_120(self):
        assert ACTIVE_POLL_INTERVAL_S == 120

    def test_dormant_interval_is_600(self):
        assert DORMANT_POLL_INTERVAL_S == 600

    def test_schedule_refresh_is_60(self):
        assert SLATE_SCHEDULE_REFRESH_MINUTES == 60


# ---------------------------------------------------------------------------
# _minutes_to_nearest_lock()
# ---------------------------------------------------------------------------

class TestMinutesToNearestLock:
    def test_no_slates_returns_none(self):
        svc = _make_service()
        svc._slate_schedule = []
        result = svc._minutes_to_nearest_lock(datetime.now(timezone.utc))
        assert result is None

    def test_all_slates_in_past_returns_none(self):
        svc = _make_service()
        now = datetime.now(timezone.utc)
        svc._slate_schedule = [
            {"lock_time_utc": now - timedelta(hours=2), "slate_name": "Early"},
            {"lock_time_utc": now - timedelta(hours=1), "slate_name": "Main"},
        ]
        result = svc._minutes_to_nearest_lock(now)
        assert result is None

    def test_one_future_slate(self):
        svc = _make_service()
        now = datetime.now(timezone.utc)
        svc._slate_schedule = [
            {"lock_time_utc": now + timedelta(minutes=30), "slate_name": "Main"},
        ]
        result = svc._minutes_to_nearest_lock(now)
        assert result is not None
        assert abs(result - 30.0) < 0.1

    def test_multiple_slates_returns_nearest(self):
        svc = _make_service()
        now = datetime.now(timezone.utc)
        svc._slate_schedule = [
            {"lock_time_utc": now + timedelta(minutes=120), "slate_name": "Night"},
            {"lock_time_utc": now + timedelta(minutes=45), "slate_name": "Main"},
            {"lock_time_utc": now - timedelta(minutes=30), "slate_name": "Early"},
        ]
        result = svc._minutes_to_nearest_lock(now)
        assert result is not None
        assert abs(result - 45.0) < 0.1

    def test_mix_of_past_and_future(self):
        svc = _make_service()
        now = datetime.now(timezone.utc)
        svc._slate_schedule = [
            {"lock_time_utc": now - timedelta(hours=3), "slate_name": "Early"},
            {"lock_time_utc": now + timedelta(minutes=10), "slate_name": "Main"},
        ]
        result = svc._minutes_to_nearest_lock(now)
        assert abs(result - 10.0) < 0.1


# ---------------------------------------------------------------------------
# _compute_news_hash()
# ---------------------------------------------------------------------------

class TestComputeNewsHash:
    def test_empty_list_stable(self):
        h1 = PreLockPollingService._compute_news_hash([])
        h2 = PreLockPollingService._compute_news_hash([])
        assert h1 == h2
        assert len(h1) == 32  # MD5 hex length

    def test_same_items_same_hash(self):
        items = [MockNewsItem("abc"), MockNewsItem("def")]
        h1 = PreLockPollingService._compute_news_hash(items)
        h2 = PreLockPollingService._compute_news_hash(items)
        assert h1 == h2

    def test_order_independent(self):
        items_a = [MockNewsItem("abc"), MockNewsItem("def")]
        items_b = [MockNewsItem("def"), MockNewsItem("abc")]
        # Hash is of sorted IDs, so order shouldn't matter
        h1 = PreLockPollingService._compute_news_hash(items_a)
        h2 = PreLockPollingService._compute_news_hash(items_b)
        assert h1 == h2

    def test_different_items_different_hash(self):
        items_a = [MockNewsItem("abc")]
        items_b = [MockNewsItem("xyz")]
        h1 = PreLockPollingService._compute_news_hash(items_a)
        h2 = PreLockPollingService._compute_news_hash(items_b)
        assert h1 != h2

    def test_added_item_changes_hash(self):
        items_a = [MockNewsItem("abc")]
        items_b = [MockNewsItem("abc"), MockNewsItem("def")]
        h1 = PreLockPollingService._compute_news_hash(items_a)
        h2 = PreLockPollingService._compute_news_hash(items_b)
        assert h1 != h2


# ---------------------------------------------------------------------------
# _should_refresh_schedule()
# ---------------------------------------------------------------------------

class TestShouldRefreshSchedule:
    def test_empty_schedule_needs_refresh(self):
        svc = _make_service()
        svc._slate_schedule = []
        assert svc._should_refresh_schedule() is True

    def test_no_fetch_time_needs_refresh(self):
        svc = _make_service()
        svc._slate_schedule = [{"slate_name": "Main"}]
        svc._schedule_fetched_at = None
        assert svc._should_refresh_schedule() is True

    def test_fresh_schedule_no_refresh(self):
        svc = _make_service()
        svc._slate_schedule = [{"slate_name": "Main"}]
        svc._schedule_fetched_at = datetime.now(timezone.utc) - timedelta(minutes=5)
        assert svc._should_refresh_schedule() is False

    def test_stale_schedule_needs_refresh(self):
        svc = _make_service()
        svc._slate_schedule = [{"slate_name": "Main"}]
        svc._schedule_fetched_at = datetime.now(timezone.utc) - timedelta(
            minutes=SLATE_SCHEDULE_REFRESH_MINUTES + 1
        )
        assert svc._should_refresh_schedule() is True


# ---------------------------------------------------------------------------
# _poll_cycle() — mocked end-to-end
# ---------------------------------------------------------------------------

class TestPollCycle:
    @pytest.mark.asyncio
    async def test_poll_increments_count(self):
        svc = _make_service()
        assert svc._poll_count == 0
        await svc._poll_cycle()
        assert svc._poll_count == 1

    @pytest.mark.asyncio
    async def test_poll_sets_last_poll_at(self):
        svc = _make_service()
        assert svc._last_poll_at is None
        await svc._poll_cycle()
        assert svc._last_poll_at is not None

    @pytest.mark.asyncio
    async def test_poll_calls_sync_injuries(self):
        svc = _make_service()
        await svc._poll_cycle()
        svc._injury_sync.sync_injuries.assert_called_once()

    @pytest.mark.asyncio
    async def test_poll_forces_news_refresh(self):
        svc = _make_service()
        svc._news._cache_timestamp = 12345.0
        await svc._poll_cycle()
        # _cache_timestamp should have been cleared to force refresh
        svc._news.get_news.assert_called_once()

    @pytest.mark.asyncio
    async def test_injury_hash_changed_tracked(self):
        svc = _make_service()
        svc._injury_sync.sync_injuries.return_value = {
            "hash_changed": True,
            "star_changes": [{"player": "Test"}],
            "optimizer_caches_cleared": 3,
            "prewarm_published": 1,
        }
        await svc._poll_cycle()
        assert svc._last_injury_result["hash_changed"] is True

    @pytest.mark.asyncio
    async def test_news_change_detected(self):
        svc = _make_service()
        # Set a previous hash so we can detect a change
        svc._last_news_hash = "old_hash"
        svc._news.get_news.return_value = (
            [MockNewsItem("new1"), MockNewsItem("new2")],
            False,
        )
        await svc._poll_cycle()
        assert svc._last_news_changed is True

    @pytest.mark.asyncio
    async def test_news_no_change_on_first_run(self):
        svc = _make_service()
        # First run: _last_news_hash is "" so change detection is skipped
        svc._news.get_news.return_value = (
            [MockNewsItem("item1")],
            False,
        )
        await svc._poll_cycle()
        assert svc._last_news_changed is False

    @pytest.mark.asyncio
    async def test_news_change_publishes_to_redis(self):
        svc = _make_service()
        svc._last_news_hash = "old_hash"
        svc._news.get_news.return_value = (
            [MockNewsItem("new1", relevance="injury")],
            False,
        )
        await svc._poll_cycle()
        svc._cache._redis.publish.assert_called_once()
        call_args = svc._cache._redis.publish.call_args
        assert call_args[0][0] == "rotation_engine_updates"

    @pytest.mark.asyncio
    async def test_news_change_busts_pool_cache(self):
        svc = _make_service()
        svc._last_news_hash = "old_hash"
        svc._news.get_news.return_value = (
            [MockNewsItem("new1")],
            False,
        )
        await svc._poll_cycle()
        svc._cache.clear_pattern.assert_called_with("pool:*")

    @pytest.mark.asyncio
    async def test_poll_handles_sync_injuries_error(self):
        svc = _make_service()
        svc._injury_sync.sync_injuries.side_effect = Exception("DB down")
        # Should not raise
        await svc._poll_cycle()
        assert len(svc._errors) == 1
        assert "injury_sync" in svc._errors[0]

    @pytest.mark.asyncio
    async def test_poll_handles_news_error(self):
        svc = _make_service()
        svc._news.get_news.side_effect = Exception("Network error")
        await svc._poll_cycle()
        assert len(svc._errors) == 1
        assert "news" in svc._errors[0]


# ---------------------------------------------------------------------------
# get_status()
# ---------------------------------------------------------------------------

class TestGetStatus:
    def test_status_has_expected_keys(self):
        svc = _make_service()
        status = svc.get_status()

        expected_keys = {
            "running", "active_window", "poll_count", "last_poll_at",
            "minutes_to_nearest_lock", "slate_schedule",
            "last_injury_hash_changed", "last_news_changed", "recent_errors",
            "auto_swap",
        }
        assert set(status.keys()) == expected_keys

    def test_status_defaults(self):
        svc = _make_service()
        status = svc.get_status()

        assert status["running"] is False
        assert status["active_window"] is False
        assert status["poll_count"] == 0
        assert status["last_poll_at"] is None
        assert status["minutes_to_nearest_lock"] is None
        assert status["slate_schedule"] == []
        assert status["last_injury_hash_changed"] is False
        assert status["last_news_changed"] is False
        assert status["recent_errors"] == []

    def test_status_with_slate_schedule(self):
        svc = _make_service()
        now = datetime.now(timezone.utc)
        svc._slate_schedule = [
            {
                "slate_name": "Main",
                "lock_time_utc": now + timedelta(hours=2),
                "draft_group_id": 12345,
                "game_count": 8,
            },
        ]
        status = svc.get_status()

        assert len(status["slate_schedule"]) == 1
        assert status["slate_schedule"][0]["name"] == "Main"
        assert status["slate_schedule"][0]["draft_group_id"] == 12345
        assert status["slate_schedule"][0]["locked"] is False
        assert status["minutes_to_nearest_lock"] is not None

    def test_status_shows_locked_slate(self):
        svc = _make_service()
        now = datetime.now(timezone.utc)
        svc._slate_schedule = [
            {
                "slate_name": "Early",
                "lock_time_utc": now - timedelta(hours=1),
                "draft_group_id": 111,
                "game_count": 3,
            },
        ]
        status = svc.get_status()

        assert status["slate_schedule"][0]["locked"] is True
        assert status["minutes_to_nearest_lock"] is None

    @pytest.mark.asyncio
    async def test_status_after_poll(self):
        svc = _make_service()
        await svc._poll_cycle()
        status = svc.get_status()

        assert status["poll_count"] == 1
        assert status["last_poll_at"] is not None


# ---------------------------------------------------------------------------
# Start / Stop lifecycle
# ---------------------------------------------------------------------------

class TestLifecycle:
    def test_initial_state(self):
        svc = _make_service()
        assert svc._running is False
        assert svc._task is None

    def test_stop_when_not_started(self):
        svc = _make_service()
        # Should not raise
        svc.stop()
        assert svc._running is False

    @pytest.mark.asyncio
    async def test_start_creates_task(self):
        svc = _make_service()
        # Mock the _run_loop to prevent actual execution
        svc._run_loop = AsyncMock()
        svc.start()
        assert svc._running is True
        assert svc._task is not None
        svc.stop()
        # Give the event loop a chance to process cancellation
        await asyncio.sleep(0.01)


# ---------------------------------------------------------------------------
# Error tracking
# ---------------------------------------------------------------------------

class TestErrorTracking:
    @pytest.mark.asyncio
    async def test_errors_capped_at_10(self):
        svc = _make_service()
        svc._injury_sync.sync_injuries.side_effect = Exception("fail")
        for _ in range(15):
            await svc._poll_cycle()
        assert len(svc._errors) == 10

    @pytest.mark.asyncio
    async def test_status_shows_last_5_errors(self):
        svc = _make_service()
        svc._errors = [f"error_{i}" for i in range(8)]
        status = svc.get_status()
        assert len(status["recent_errors"]) == 5
