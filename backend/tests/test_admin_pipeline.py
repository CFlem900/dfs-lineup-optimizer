"""Tests for Phase 3: Automated Nightly Feedback Pipeline.

Tests cover:
1. Pipeline state management
2. Pipeline execution flow (with mocked services)
3. Admin endpoint access
4. Scheduler registration
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone


# ============================================================================
# PIPELINE STATE TESTS
# ============================================================================


class TestPipelineState:
    """Tests for pipeline state management."""

    def test_initial_state_is_idle(self):
        """Pipeline should start in idle state."""
        from app.api.routers.admin_pipeline import get_pipeline_state
        state = get_pipeline_state()
        assert state["last_status"] == "idle"
        assert state["last_run_at"] is None
        assert state["last_result"] is None
        assert state["total_runs"] == 0

    def test_state_dict_has_expected_keys(self):
        """Pipeline state should contain all expected keys."""
        from app.api.routers.admin_pipeline import get_pipeline_state
        state = get_pipeline_state()
        expected_keys = {
            "last_run_at", "last_status", "last_result",
            "last_error", "total_runs",
        }
        assert expected_keys == set(state.keys())


# ============================================================================
# PIPELINE EXECUTION TESTS
# ============================================================================


class TestPipelineExecution:
    """Tests for run_nightly_pipeline function."""

    @pytest.fixture(autouse=True)
    def reset_state(self):
        """Reset pipeline state before each test."""
        from app.api.routers import admin_pipeline
        admin_pipeline._pipeline_state.update({
            "last_run_at": None,
            "last_status": "idle",
            "last_result": None,
            "last_error": None,
            "total_runs": 0,
        })
        yield
        # Reset again after test
        admin_pipeline._pipeline_state.update({
            "last_run_at": None,
            "last_status": "idle",
            "last_result": None,
            "last_error": None,
            "total_runs": 0,
        })

    @pytest.mark.asyncio
    async def test_pipeline_runs_ingestion(self):
        """Pipeline should call box_score_ingester.ingest_game_date."""
        mock_svc = MagicMock()
        mock_svc.box_score_ingester.ingest_game_date = AsyncMock(return_value=15)

        with patch("app.api.dependencies.get_services", return_value=mock_svc), \
             patch("app.api.routers.admin_pipeline.is_db_available", return_value=True), \
             patch("app.api.dependencies.run_projection_analysis", new_callable=AsyncMock, return_value=None):

            from app.api.routers.admin_pipeline import run_nightly_pipeline
            result = await run_nightly_pipeline(game_date="2026-02-14")

            mock_svc.box_score_ingester.ingest_game_date.assert_called_once_with("2026-02-14")
            assert result["rows_ingested"] == 15
            assert result["game_date"] == "2026-02-14"

    @pytest.mark.asyncio
    async def test_pipeline_runs_analysis(self):
        """Pipeline should call run_projection_analysis."""
        mock_svc = MagicMock()
        mock_svc.box_score_ingester.ingest_game_date = AsyncMock(return_value=10)

        mock_analysis = {
            "analysis": {"recommendations": ["test"]},
            "calibrations_saved": 3,
        }

        with patch("app.api.dependencies.get_services", return_value=mock_svc), \
             patch("app.api.routers.admin_pipeline.is_db_available", return_value=True), \
             patch("app.api.dependencies.run_projection_analysis",
                   new_callable=AsyncMock, return_value=mock_analysis):

            from app.api.routers.admin_pipeline import run_nightly_pipeline
            result = await run_nightly_pipeline(game_date="2026-02-14")

            assert result["calibrations_saved"] == 3
            assert result["analysis"] is not None

    @pytest.mark.asyncio
    async def test_pipeline_updates_running_state(self):
        """Pipeline should set state to 'running' during execution."""
        from app.api.routers.admin_pipeline import (
            run_nightly_pipeline, _pipeline_state,
        )

        captured_status = []

        async def mock_ingest(date):
            captured_status.append(_pipeline_state["last_status"])
            return 0

        mock_svc = MagicMock()
        mock_svc.box_score_ingester.ingest_game_date = mock_ingest

        with patch("app.api.dependencies.get_services", return_value=mock_svc), \
             patch("app.api.routers.admin_pipeline.is_db_available", return_value=True), \
             patch("app.api.dependencies.run_projection_analysis",
                   new_callable=AsyncMock, return_value=None):

            await run_nightly_pipeline(game_date="2026-02-14")

        assert "running" in captured_status

    @pytest.mark.asyncio
    async def test_pipeline_success_status(self):
        """After successful run, state should be 'success'."""
        mock_svc = MagicMock()
        mock_svc.box_score_ingester.ingest_game_date = AsyncMock(return_value=0)

        with patch("app.api.dependencies.get_services", return_value=mock_svc), \
             patch("app.api.routers.admin_pipeline.is_db_available", return_value=True), \
             patch("app.api.dependencies.run_projection_analysis",
                   new_callable=AsyncMock, return_value=None):

            from app.api.routers.admin_pipeline import run_nightly_pipeline, get_pipeline_state
            await run_nightly_pipeline(game_date="2026-02-14")

            state = get_pipeline_state()
            assert state["last_status"] == "success"
            assert state["total_runs"] == 1
            assert state["last_run_at"] is not None

    @pytest.mark.asyncio
    async def test_pipeline_defaults_to_yesterday(self):
        """Without game_date, pipeline should use yesterday's date."""
        mock_svc = MagicMock()
        mock_svc.box_score_ingester.ingest_game_date = AsyncMock(return_value=0)

        with patch("app.api.dependencies.get_services", return_value=mock_svc), \
             patch("app.api.routers.admin_pipeline.is_db_available", return_value=True), \
             patch("app.api.dependencies.run_projection_analysis",
                   new_callable=AsyncMock, return_value=None):

            from app.api.routers.admin_pipeline import run_nightly_pipeline
            result = await run_nightly_pipeline()  # No date

            # Should have computed yesterday
            assert result["game_date"] is not None
            assert len(result["game_date"]) == 10  # YYYY-MM-DD format

    @pytest.mark.asyncio
    async def test_pipeline_handles_db_unavailable(self):
        """Pipeline should not crash when database is unavailable."""
        mock_svc = MagicMock()

        with patch("app.api.dependencies.get_services", return_value=mock_svc), \
             patch("app.api.routers.admin_pipeline.is_db_available", return_value=False), \
             patch("app.api.dependencies.run_projection_analysis",
                   new_callable=AsyncMock, return_value=None):

            from app.api.routers.admin_pipeline import run_nightly_pipeline
            result = await run_nightly_pipeline(game_date="2026-02-14")

            assert result["rows_ingested"] == 0
            assert result["calibrations_saved"] == 0

    @pytest.mark.asyncio
    async def test_pipeline_handles_ingestion_error(self):
        """Pipeline should continue if ingestion fails."""
        mock_svc = MagicMock()
        mock_svc.box_score_ingester.ingest_game_date = AsyncMock(
            side_effect=RuntimeError("NBA API down")
        )

        with patch("app.api.dependencies.get_services", return_value=mock_svc), \
             patch("app.api.routers.admin_pipeline.is_db_available", return_value=True), \
             patch("app.api.dependencies.run_projection_analysis",
                   new_callable=AsyncMock, return_value=None):

            from app.api.routers.admin_pipeline import run_nightly_pipeline, get_pipeline_state
            result = await run_nightly_pipeline(game_date="2026-02-14")

            # Should still succeed overall (ingestion error is non-fatal)
            state = get_pipeline_state()
            assert state["last_status"] == "success"
            assert "ingestion_error" in result


# ============================================================================
# SCHEDULER TESTS
# ============================================================================


class TestSchedulerSetup:
    """Tests for APScheduler integration."""

    def test_apscheduler_importable(self):
        """APScheduler should be installed and importable."""
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from apscheduler.triggers.cron import CronTrigger
        assert AsyncIOScheduler is not None
        assert CronTrigger is not None

    def test_get_next_pipeline_run_without_scheduler(self):
        """get_next_pipeline_run returns None when no scheduler."""
        import app.main as main_module
        original = main_module._scheduler
        main_module._scheduler = None
        try:
            assert main_module.get_next_pipeline_run() is None
        finally:
            main_module._scheduler = original

    def test_cron_trigger_config(self):
        """CronTrigger should accept our config without error."""
        from apscheduler.triggers.cron import CronTrigger
        trigger = CronTrigger(hour=3, minute=0, timezone="US/Eastern")
        assert trigger is not None


# ============================================================================
# ENDPOINT ROUTING TESTS
# ============================================================================


class TestEndpointRouting:
    """Tests for admin pipeline endpoint registration."""

    def test_router_has_trigger_endpoint(self):
        """admin_pipeline router should have POST /admin/pipeline/trigger."""
        from app.api.routers.admin_pipeline import router
        paths = [r.path for r in router.routes]
        assert "/admin/pipeline/trigger" in paths

    def test_router_has_status_endpoint(self):
        """admin_pipeline router should have GET /admin/pipeline/status."""
        from app.api.routers.admin_pipeline import router
        paths = [r.path for r in router.routes]
        assert "/admin/pipeline/status" in paths

    def test_admin_pipeline_included_in_main_router(self):
        """admin_pipeline should be registered in the main API router."""
        from app.api.routes import router as main_router
        # Check that admin_pipeline routes are included
        all_paths = []
        for route in main_router.routes:
            if hasattr(route, "path"):
                all_paths.append(route.path)
            elif hasattr(route, "routes"):
                # Sub-router
                for sub_route in route.routes:
                    if hasattr(sub_route, "path"):
                        all_paths.append(sub_route.path)
        assert "/admin/pipeline/trigger" in all_paths or any(
            "pipeline" in p for p in all_paths
        )
