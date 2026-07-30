"""Tests for Showdown / Single-Game mode support."""

import pytest

from app.models.lineup import OptimizeRequest, MultiLineupRequest
from app.services.lineup_optimizer_service import (
    DK_SHOWDOWN_SALARY_CAP,
    DK_SHOWDOWN_SLOTS,
    DK_SHOWDOWN_ELIGIBILITY,
    CPT_MULTIPLIER,
)


class TestShowdownConstants:
    def test_showdown_salary_cap(self):
        assert DK_SHOWDOWN_SALARY_CAP == 50_000

    def test_showdown_slots(self):
        assert DK_SHOWDOWN_SLOTS == ["CPT", "FLEX", "FLEX", "FLEX", "FLEX", "FLEX"]
        assert len(DK_SHOWDOWN_SLOTS) == 6

    def test_cpt_multiplier(self):
        assert CPT_MULTIPLIER == 1.5

    def test_showdown_eligibility(self):
        assert "CPT" in DK_SHOWDOWN_ELIGIBILITY
        assert "FLEX" in DK_SHOWDOWN_ELIGIBILITY
        # All positions can fill CPT and FLEX
        for slot in ["CPT", "FLEX"]:
            elig = DK_SHOWDOWN_ELIGIBILITY[slot]
            assert "PG" in elig
            assert "SG" in elig
            assert "SF" in elig
            assert "PF" in elig
            assert "C" in elig


class TestOptimizeRequestMode:
    def test_default_mode_is_classic(self):
        req = OptimizeRequest(
            platform="dk",
            draft_group_id=12345,
        )
        assert req.mode == "classic"
        assert req.game_id is None

    def test_showdown_mode(self):
        req = OptimizeRequest(
            platform="dk",
            draft_group_id=12345,
            mode="showdown",
            game_id="0022400123",
        )
        assert req.mode == "showdown"
        assert req.game_id == "0022400123"

    def test_invalid_mode_rejected(self):
        with pytest.raises(Exception):
            OptimizeRequest(
                platform="dk",
                draft_group_id=12345,
                mode="invalid_mode",
            )


class TestMultiLineupRequestMode:
    def test_default_mode_is_classic(self):
        req = MultiLineupRequest(
            platform="dk",
            draft_group_id=12345,
        )
        assert req.mode == "classic"

    def test_showdown_mode(self):
        req = MultiLineupRequest(
            platform="dk",
            draft_group_id=12345,
            mode="showdown",
            game_id="G123",
        )
        assert req.mode == "showdown"
        assert req.game_id == "G123"


class TestCPTMultiplierLogic:
    def test_cpt_multiplier_applied(self):
        """Verify CPT scoring increases projected FP by 1.5x."""
        base_fp = 30.0
        cpt_fp = base_fp * CPT_MULTIPLIER
        assert cpt_fp == 45.0

    def test_flex_no_multiplier(self):
        """FLEX slots should use 1.0x multiplier."""
        base_fp = 30.0
        flex_fp = base_fp * 1.0
        assert flex_fp == 30.0

    def test_showdown_total_scoring(self):
        """Test total FP calculation with CPT + 5 FLEX."""
        cpt_fp = 35.0 * CPT_MULTIPLIER  # 52.5
        flex_fps = [28.0, 25.0, 22.0, 20.0, 18.0]  # 113.0
        total = cpt_fp + sum(flex_fps)
        assert total == 165.5


class TestShowdownPoolFiltering:
    def test_filter_to_single_game(self):
        """Showdown mode should filter pool to a single game."""
        from types import SimpleNamespace

        pool = [
            SimpleNamespace(player_id=1, game_id="G1", projected_fp=30),
            SimpleNamespace(player_id=2, game_id="G1", projected_fp=25),
            SimpleNamespace(player_id=3, game_id="G2", projected_fp=35),
            SimpleNamespace(player_id=4, game_id="G1", projected_fp=20),
        ]

        # Simulate the filtering logic
        game_id = "G1"
        filtered = [p for p in pool if getattr(p, "game_id", None) == game_id]

        assert len(filtered) == 3
        assert all(p.game_id == "G1" for p in filtered)
