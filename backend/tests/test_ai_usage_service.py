"""Unit tests for AIUsageService and estimate_cost."""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.ai_usage_service import AIUsageService, estimate_cost


# ============================================================================
# estimate_cost — pure function tests
# ============================================================================


class TestEstimateCost:
    def test_estimate_cost_haiku(self):
        """Haiku: 1000 input + 500 output tokens."""
        result = estimate_cost("claude-3-5-haiku-20241022", 1000, 500)
        # 1000 * 1.0 / 1_000_000 + 500 * 5.0 / 1_000_000 = 0.001 + 0.0025
        assert result == pytest.approx(0.0035)

    def test_estimate_cost_gpt4o_mini(self):
        """GPT-4o-mini: 10 000 input + 2 000 output tokens."""
        result = estimate_cost("gpt-4o-mini", 10_000, 2_000)
        # 10000 * 0.15 / 1_000_000 + 2000 * 0.60 / 1_000_000 = 0.0015 + 0.0012
        assert result == pytest.approx(0.0027)

    def test_estimate_cost_unknown_model(self):
        """Unknown model falls back to default rates (input=1.0, output=5.0)."""
        result = estimate_cost("some-unknown-model-v42", 1000, 500)
        # Same math as haiku since defaults match haiku rates
        expected = 1000 * 1.0 / 1_000_000 + 500 * 5.0 / 1_000_000
        assert result == pytest.approx(expected)

    def test_estimate_cost_zero_tokens(self):
        """Zero input and output tokens → zero cost."""
        result = estimate_cost("gpt-4o", 0, 0)
        assert result == 0.0


# ============================================================================
# AIUsageService — async / DB-dependent tests
# ============================================================================


class TestAIUsageService:
    def test_service_init(self):
        """AIUsageService() creates empty caches."""
        svc = AIUsageService()
        assert svc._cache == {}
        assert svc._cache_ts == {}
        assert svc._CACHE_TTL == 300

    def test_log_usage_db_unavailable(self):
        """log_usage returns silently when the database is not available."""
        svc = AIUsageService()
        with patch(
            "app.services.ai_usage_service._ensure_sync_pool", return_value=False
        ):
            # Should not raise — sync call, no DB available
            svc.log_usage(
                agent_name="test_agent",
                model_used="gpt-4o-mini",
                provider="openai",
                input_tokens=100,
                output_tokens=50,
                latency_ms=200,
                cached=False,
            )
        # No assertion needed — just verifying no exception is raised

    @pytest.mark.asyncio
    async def test_get_usage_summary_db_unavailable(self):
        """get_usage_summary returns zeroed dict when DB is unavailable."""
        svc = AIUsageService()
        with patch(
            "app.db.database.is_db_available", return_value=False
        ):
            result = await svc.get_usage_summary(days=7)

        assert result["total_cost"] == 0
        assert result["total_calls"] == 0
        assert result["by_agent"] == {}

    @pytest.mark.asyncio
    async def test_caching_works(self):
        """Second call within TTL returns cached result without hitting DB."""
        svc = AIUsageService()

        # Build a fake DB result to return on the first call
        fake_totals_row = MagicMock()
        fake_totals_row.calls = 5
        fake_totals_row.input_tokens = 1000
        fake_totals_row.output_tokens = 500
        fake_totals_row.cost = 0.005

        fake_totals_result = MagicMock()
        fake_totals_result.one.return_value = fake_totals_row

        fake_by_agent_result = MagicMock()
        fake_by_agent_result.all.return_value = []

        mock_session = AsyncMock()
        # First execute → totals; second execute → by_agent
        mock_session.execute = AsyncMock(
            side_effect=[fake_totals_result, fake_by_agent_result]
        )

        # Create an async context manager for get_session
        mock_session_cm = AsyncMock()
        mock_session_cm.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_cm.__aexit__ = AsyncMock(return_value=False)

        with (
            patch(
                "app.db.database.is_db_available", return_value=True
            ),
            patch(
                "app.db.database.get_session",
                return_value=mock_session_cm,
            ),
        ):
            # First call — should query DB
            result1 = await svc.get_usage_summary(days=7)
            assert result1["total_calls"] == 5

            # Second call — should return cached (no additional DB queries)
            result2 = await svc.get_usage_summary(days=7)
            assert result2["total_calls"] == 5

        # session.execute should have been called exactly twice (totals + by_agent)
        # from the first call only; the second call used cache
        assert mock_session.execute.call_count == 2
