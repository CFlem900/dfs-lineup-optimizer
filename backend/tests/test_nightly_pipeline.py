"""Tests for the nightly backtesting pipeline components.

Covers:
- compute_deterministic_calibrations() — position, salary tier, stat, B2B biases
- run_projection_analysis() — AI path and deterministic fallback
- run_nightly_pipeline() — full pipeline with error handling
"""

import pytest
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.agents.backtesting_agent import (
    compute_accuracy_stats,
    compute_deterministic_calibrations,
)


# ── Helpers ──────────────────────────────────────────────────────────


def _make_row(
    position="PG",
    projected_fp=30.0,
    actual_fp=32.0,
    dk_salary=6000,
    was_b2b=False,
    projected_pts=15.0,
    actual_pts=16.5,
    projected_reb=4.0,
    actual_reb=4.0,
    projected_ast=6.0,
    actual_ast=6.0,
    projected_stl=1.0,
    actual_stl=1.0,
    projected_blk=0.3,
    actual_blk=0.3,
    projected_tov=2.0,
    actual_tov=2.0,
    projected_fg3m=2.0,
    actual_fg3m=2.0,
    projected_minutes=32.0,
    actual_minutes=33.0,
):
    return {
        "player_name": "Test Player",
        "position": position,
        "team_id": 1,
        "dk_salary": dk_salary,
        "projected_minutes": projected_minutes,
        "actual_minutes": actual_minutes,
        "projected_fp": projected_fp,
        "actual_fp": actual_fp,
        "projected_pts": projected_pts,
        "actual_pts": actual_pts,
        "projected_reb": projected_reb,
        "actual_reb": actual_reb,
        "projected_ast": projected_ast,
        "actual_ast": actual_ast,
        "projected_stl": projected_stl,
        "actual_stl": actual_stl,
        "projected_blk": projected_blk,
        "actual_blk": actual_blk,
        "projected_tov": projected_tov,
        "actual_tov": actual_tov,
        "projected_fg3m": projected_fg3m,
        "actual_fg3m": actual_fg3m,
        "was_b2b": was_b2b,
        "game_date": "2026-02-20",
    }


# ── compute_deterministic_calibrations — Position Bias ────────────


def test_deterministic_position_bias():
    """Position with bias ≥ threshold produces a calibration adjustment."""
    # 25 rows all PG, each under-projected by 2.0 FP (actual=32, proj=30)
    data = [_make_row(position="PG", projected_fp=30, actual_fp=32) for _ in range(25)]
    result = compute_deterministic_calibrations(data, min_sample=20, bias_threshold=1.0)

    assert "position_PG_bias" in result
    # Bias = +2.0, mean proj = 30.0, multiplier = 1 + 2/30 ≈ 1.067
    assert result["position_PG_bias"] > 1.0
    assert result["position_PG_bias"] <= 1.15


def test_deterministic_no_position_bias_when_small():
    """Position with bias < threshold produces no adjustment."""
    # Bias = 0.3 FP (below 1.0 threshold)
    data = [_make_row(projected_fp=30, actual_fp=30.3) for _ in range(25)]
    result = compute_deterministic_calibrations(data, min_sample=20, bias_threshold=1.0)

    assert "position_PG_bias" not in result


# ── compute_deterministic_calibrations — Salary Tier Bias ─────────


def test_deterministic_salary_tier_bias():
    """Salary tier with sufficient bias produces adjustment."""
    # HIGH tier ($8000+), over-projected by 3 FP (actual=27, proj=30)
    data = [
        _make_row(dk_salary=9000, projected_fp=30, actual_fp=27)
        for _ in range(25)
    ]
    result = compute_deterministic_calibrations(data, min_sample=20, bias_threshold=1.0)

    assert "salary_tier_high_projection" in result
    # Bias = -3.0 (over-project), multiplier < 1.0
    assert result["salary_tier_high_projection"] < 1.0
    assert result["salary_tier_high_projection"] >= 0.85


# ── compute_deterministic_calibrations — Stat Rate Bias ───────────


def test_deterministic_stat_rate_bias():
    """Per-stat bias ≥ 5% relative produces a stat_rate adjustment."""
    # Under-project rebounds by 1.0 with mean proj of 5.0 → 20% relative
    data = [
        _make_row(projected_reb=5.0, actual_reb=6.0)
        for _ in range(25)
    ]
    result = compute_deterministic_calibrations(data, min_sample=20)

    assert "stat_rate_reb" in result
    # Bias = +1.0, mean proj = 5.0 → multiplier = 1.2 → clamped to 1.15
    assert result["stat_rate_reb"] > 1.0


def test_deterministic_stat_rate_no_bias_when_small():
    """Per-stat bias < 5% relative produces no stat_rate adjustment."""
    # Bias = 0.1 with mean proj = 15.0 → 0.67% relative (below 5%)
    data = [
        _make_row(projected_pts=15.0, actual_pts=15.1)
        for _ in range(25)
    ]
    result = compute_deterministic_calibrations(data, min_sample=20)

    assert "stat_rate_pts" not in result


# ── compute_deterministic_calibrations — B2B Bias ─────────────────


def test_deterministic_b2b_bias():
    """B2B games with significant bias produce game_context_b2b adjustment."""
    data = [
        _make_row(was_b2b=True, projected_fp=30, actual_fp=26)
        for _ in range(25)
    ]
    result = compute_deterministic_calibrations(data, min_sample=20, bias_threshold=1.0)

    assert "game_context_b2b" in result
    # Bias = -4.0, over-projected → multiplier < 1.0
    assert result["game_context_b2b"] < 1.0


# ── compute_deterministic_calibrations — Below Threshold ──────────


def test_deterministic_below_threshold_empty():
    """Small biases across all categories produce empty dict."""
    # Tiny biases (0.1 FP) → nothing crosses threshold
    data = [_make_row(projected_fp=30, actual_fp=30.1) for _ in range(25)]
    result = compute_deterministic_calibrations(data, min_sample=20, bias_threshold=1.0)

    # No FP-level keys (position, salary, b2b)
    assert not any(k.startswith("position_") for k in result)
    assert not any(k.startswith("salary_tier_") for k in result)
    assert "game_context_b2b" not in result


# ── compute_deterministic_calibrations — Small Sample ─────────────


def test_deterministic_small_sample_empty():
    """Insufficient data (n < min_sample) returns empty dict."""
    data = [_make_row(projected_fp=30, actual_fp=35) for _ in range(5)]
    result = compute_deterministic_calibrations(data, min_sample=20, bias_threshold=1.0)

    assert result == {}


# ── compute_deterministic_calibrations — Clamping ─────────────────


def test_deterministic_clamped_at_bounds():
    """Extreme biases are clamped to [0.85, 1.15]."""
    # Massive over-projection: proj=30, actual=10 → bias=-20 → would be 0.33
    data = [_make_row(projected_fp=30, actual_fp=10) for _ in range(25)]
    result = compute_deterministic_calibrations(data, min_sample=20, bias_threshold=1.0)

    assert "position_PG_bias" in result
    assert result["position_PG_bias"] == 0.85  # Clamped at lower bound


def test_deterministic_clamped_upper():
    """Massive under-projection is clamped to 1.15."""
    # proj=10, actual=30 → bias=+20 → would be 3.0
    data = [_make_row(projected_fp=10, actual_fp=30) for _ in range(25)]
    result = compute_deterministic_calibrations(data, min_sample=20, bias_threshold=1.0)

    assert "position_PG_bias" in result
    assert result["position_PG_bias"] == 1.15  # Clamped at upper bound


# ── compute_deterministic_calibrations — Empty Input ──────────────


def test_deterministic_empty_data():
    """Empty accuracy data returns empty dict."""
    assert compute_deterministic_calibrations([]) == {}


# ── run_projection_analysis — AI Success ──────────────────────────


@pytest.mark.asyncio
async def test_run_projection_analysis_ai_success():
    """When Agent 9 returns analysis, AI calibrations are saved."""
    from app.api.dependencies import run_projection_analysis

    mock_analysis = MagicMock()
    mock_analysis.calibration_adjustments = {"position_PG_bias": 1.03}
    mock_analysis.recommendations = ["Reduce PG projections"]
    mock_analysis.model_dump.return_value = {
        "biases": [],
        "calibration_adjustments": {"position_PG_bias": 1.03},
    }

    mock_rows = []
    for i in range(10):
        r = MagicMock()
        r.player_name = f"Player {i}"
        r.position = "PG"
        r.team_id = 1
        r.dk_salary = 6000
        r.projected_minutes = 30
        r.actual_minutes = 31
        r.dk_projected_fp = 30
        r.dk_actual_fp = 32
        r.projected_pts = 15
        r.actual_pts = 16
        r.projected_reb = 4
        r.actual_reb = 4
        r.projected_ast = 6
        r.actual_ast = 6
        r.projected_stl = 1
        r.actual_stl = 1
        r.projected_blk = 0.3
        r.actual_blk = 0.3
        r.projected_tov = 2
        r.actual_tov = 2
        r.projected_fg3m = 2
        r.actual_fg3m = 2
        r.game_date = MagicMock()
        r.game_date.date.return_value = date(2026, 2, 20)
        r.game_date.isoformat.return_value = "2026-02-20"
        mock_rows.append(r)

    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = mock_rows
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    mock_svc = MagicMock()
    mock_svc.backtesting_agent.analyze_projection_accuracy.return_value = mock_analysis
    mock_svc.calibration_service.save_backtest_calibrations = AsyncMock(return_value=1)
    mock_svc.calibration_service.load_calibrations = AsyncMock()

    with (
        patch("app.db.database.is_db_available", return_value=True),
        patch("app.db.database.get_session", return_value=mock_session),
        patch("app.api.dependencies.get_services", return_value=mock_svc),
    ):
        result = await run_projection_analysis(days=30)

    assert result is not None
    assert result["calibrations_saved"] == 1
    assert "analysis" in result
    mock_svc.calibration_service.save_backtest_calibrations.assert_called_once()


# ── run_projection_analysis — AI Failure + Deterministic Fallback ─


@pytest.mark.asyncio
async def test_run_projection_analysis_deterministic_fallback():
    """When Agent 9 fails, deterministic calibrations are used as fallback."""
    from app.api.dependencies import run_projection_analysis

    mock_rows = []
    for i in range(25):
        r = MagicMock()
        r.player_name = f"Player {i}"
        r.position = "PG"
        r.team_id = 1
        r.dk_salary = 6000
        r.projected_minutes = 30
        r.actual_minutes = 33
        r.dk_projected_fp = 30.0
        r.dk_actual_fp = 33.0  # +3.0 bias
        r.projected_pts = 15.0
        r.actual_pts = 16.5
        r.projected_reb = 4.0
        r.actual_reb = 5.0
        r.projected_ast = 6.0
        r.actual_ast = 6.0
        r.projected_stl = 1.0
        r.actual_stl = 1.0
        r.projected_blk = 0.3
        r.actual_blk = 0.3
        r.projected_tov = 2.0
        r.actual_tov = 2.0
        r.projected_fg3m = 2.0
        r.actual_fg3m = 2.0
        r.game_date = MagicMock()
        r.game_date.date.return_value = date(2026, 2, 20)
        r.game_date.isoformat.return_value = "2026-02-20"
        mock_rows.append(r)

    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = mock_rows
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    mock_svc = MagicMock()
    # Agent 9 returns None (AI unavailable)
    mock_svc.backtesting_agent.analyze_projection_accuracy.return_value = None
    mock_svc.backtesting_agent.analyze_accuracy.return_value = None
    mock_svc.calibration_service.save_backtest_calibrations = AsyncMock(return_value=3)
    mock_svc.calibration_service.load_calibrations = AsyncMock()

    with (
        patch("app.db.database.is_db_available", return_value=True),
        patch("app.db.database.get_session", return_value=mock_session),
        patch("app.api.dependencies.get_services", return_value=mock_svc),
    ):
        result = await run_projection_analysis(days=30)

    assert result is not None
    assert result["calibrations_saved"] == 3
    # Verify it's the deterministic path
    analysis = result["analysis"]
    assert isinstance(analysis, dict)
    assert analysis.get("source") == "deterministic"
    assert "stats" in analysis
    # Deterministic fallback used save_backtest_calibrations
    mock_svc.calibration_service.save_backtest_calibrations.assert_called_once()
    call_kwargs = mock_svc.calibration_service.save_backtest_calibrations.call_args
    metadata = call_kwargs[1].get("metadata") or call_kwargs[0][1]
    assert "deterministic_fallback" in str(metadata)


# ── run_nightly_pipeline — Full Success ───────────────────────────


@pytest.mark.asyncio
async def test_run_nightly_pipeline_full_success():
    """All pipeline steps succeed and return results."""
    from app.api.routers.admin_pipeline import run_nightly_pipeline

    mock_svc = MagicMock()
    mock_svc.box_score_ingester.ingest_game_date = AsyncMock(return_value=50)
    mock_svc.simulation_tuning_agent = MagicMock()
    mock_svc.simulation_tuning_agent.compute_nightly_calibrations.return_value = {
        "noise_sigma_pts": 1.05,
    }
    mock_svc.calibration_service.save_backtest_calibrations = AsyncMock(return_value=1)
    mock_svc.calibration_service.load_calibrations = AsyncMock()
    mock_svc.calibration_service.compute_team_injury_offsets = AsyncMock(return_value={
        (1, "GTD"): 1.05,
        (2, "OUT"): 0.90,
    })

    mock_analysis = {
        "analysis": {"biases": [], "source": "ai"},
        "calibrations_saved": 5,
        "stats": {"overall_fp_bias": 1.2, "overall_min_bias": -0.5, "sample_size": 100},
    }

    with (
        patch("app.api.dependencies.get_services", return_value=mock_svc),
        patch("app.api.dependencies.run_projection_analysis", AsyncMock(return_value=mock_analysis)),
        patch("app.api.routers.admin_pipeline.is_db_available", return_value=True),
        patch("app.api.routers.admin_pipeline._build_sim_tuning_pool", return_value=[{"player_id": 1}]),
    ):
        result = await run_nightly_pipeline(game_date="2026-02-20")

    assert result["rows_ingested"] == 50
    assert result["calibrations_saved"] == 5
    assert result["injury_offsets_loaded"] == 2
    assert "bias_summary" in result


# ── run_nightly_pipeline — Agent 9 Failure ────────────────────────


@pytest.mark.asyncio
async def test_run_nightly_pipeline_agent9_fails():
    """Pipeline continues when projection analysis fails."""
    from app.api.routers.admin_pipeline import run_nightly_pipeline

    mock_svc = MagicMock()
    mock_svc.box_score_ingester.ingest_game_date = AsyncMock(return_value=10)
    mock_svc.simulation_tuning_agent = None  # Not available
    mock_svc.calibration_service.compute_team_injury_offsets = AsyncMock(return_value={})

    with (
        patch("app.api.dependencies.get_services", return_value=mock_svc),
        patch(
            "app.api.dependencies.run_projection_analysis",
            AsyncMock(side_effect=Exception("Agent 9 timeout")),
        ),
        patch("app.api.routers.admin_pipeline.is_db_available", return_value=True),
    ):
        result = await run_nightly_pipeline(game_date="2026-02-20")

    # Pipeline didn't crash
    assert result["rows_ingested"] == 10
    assert "analysis_error" in result
    assert result["analysis"] is None


# ── run_nightly_pipeline — Sim Tuning Failure ─────────────────────


@pytest.mark.asyncio
async def test_run_nightly_pipeline_sim_tuning_fails():
    """Pipeline continues when simulation tuning fails."""
    from app.api.routers.admin_pipeline import run_nightly_pipeline

    mock_svc = MagicMock()
    mock_svc.box_score_ingester.ingest_game_date = AsyncMock(return_value=30)
    mock_svc.simulation_tuning_agent = MagicMock()
    mock_svc.simulation_tuning_agent.compute_nightly_calibrations.side_effect = RuntimeError("boom")
    mock_svc.calibration_service.compute_team_injury_offsets = AsyncMock(return_value={})

    with (
        patch("app.api.dependencies.get_services", return_value=mock_svc),
        patch("app.api.dependencies.run_projection_analysis", AsyncMock(return_value=None)),
        patch("app.api.routers.admin_pipeline.is_db_available", return_value=True),
        patch("app.api.routers.admin_pipeline._build_sim_tuning_pool", return_value=[{"player_id": 1}]),
    ):
        result = await run_nightly_pipeline(game_date="2026-02-20")

    # Pipeline didn't crash, sim tuning error recorded
    assert result["rows_ingested"] == 30
    assert "sim_tuning_error" in result
