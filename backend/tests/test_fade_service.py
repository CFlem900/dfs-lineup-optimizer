"""Unit tests for FadeService."""

import pytest

from app.services.fade_service import FadeService


class MockPlayer:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


def _make_player(
    player_id=1,
    player_name="Player",
    position="PG",
    team_abbreviation="BOS",
    salary=5000,
    projected_fp=30.0,
    ceiling_fp=40.0,
    floor_fp=20.0,
    ownership_projection=10.0,
    dk_value=6.0,
):
    return MockPlayer(
        player_id=player_id,
        player_name=player_name,
        position=position,
        team_abbreviation=team_abbreviation,
        salary=salary,
        projected_fp=projected_fp,
        ceiling_fp=ceiling_fp,
        floor_fp=floor_fp,
        ownership_projection=ownership_projection,
        dk_value=dk_value,
    )


class TestFadeService:
    """Tests for FadeService.generate_fade_list."""

    def test_empty_pool(self):
        """An empty pool returns a minimal result with an 'Empty player pool' message."""
        svc = FadeService()
        result = svc.generate_fade_list([])
        assert result == {
            "fades": [],
            "leverage_plays": [],
            "pool_size": 0,
            "message": "Empty player pool",
        }

    def test_all_none_ownership(self):
        """When every player has ownership_projection=None, fades and leverage are empty."""
        svc = FadeService()
        pool = [
            _make_player(player_id=i, ceiling_fp=30.0 + i, ownership_projection=None)
            for i in range(5)
        ]
        result = svc.generate_fade_list(pool)
        assert result["fades"] == []
        assert result["leverage_plays"] == []
        assert result["pool_size"] == 5

    def test_single_player_pool(self):
        """A pool with one player should not crash and should return pool_size=1."""
        svc = FadeService()
        pool = [_make_player(player_id=1, ceiling_fp=35.0, ownership_projection=15.0)]
        result = svc.generate_fade_list(pool)
        assert result["pool_size"] == 1
        # A single player cannot be below its own percentile, so no fade expected
        assert isinstance(result["fades"], list)
        assert isinstance(result["leverage_plays"], list)

    def test_high_ownership_low_ceiling_fade(self):
        """A player with high ownership and a ceiling below the 50th percentile should appear in fades."""
        svc = FadeService()
        # Build a pool where one player has high ownership but a low ceiling.
        # We need enough players so that the ceiling percentile threshold is meaningful.
        pool = []
        # 9 players with low ownership and ascending ceilings (20-60)
        for i in range(9):
            pool.append(
                _make_player(
                    player_id=i + 1,
                    player_name=f"Filler{i}",
                    ceiling_fp=20.0 + (i * 5),  # 20, 25, 30, 35, 40, 45, 50, 55, 60
                    ownership_projection=5.0,
                )
            )
        # The fade candidate: high ownership, very low ceiling
        fade_candidate = _make_player(
            player_id=100,
            player_name="FadeMe",
            ceiling_fp=22.0,
            ownership_projection=30.0,
        )
        pool.append(fade_candidate)

        result = svc.generate_fade_list(pool)
        fade_names = [f["player_name"] for f in result["fades"]]
        assert "FadeMe" in fade_names

    def test_low_ownership_high_ceiling_leverage(self):
        """A player with low ownership and a ceiling above the 75th percentile should appear in leverage_plays."""
        svc = FadeService()
        pool = []
        # 9 players with moderate ownership and ascending ceilings
        for i in range(9):
            pool.append(
                _make_player(
                    player_id=i + 1,
                    player_name=f"Filler{i}",
                    ceiling_fp=20.0 + (i * 5),  # 20..60
                    ownership_projection=15.0,
                )
            )
        # Leverage candidate: low ownership, very high ceiling
        leverage_candidate = _make_player(
            player_id=200,
            player_name="LeverageMe",
            ceiling_fp=70.0,
            ownership_projection=3.0,
        )
        pool.append(leverage_candidate)

        result = svc.generate_fade_list(pool)
        leverage_names = [lp["player_name"] for lp in result["leverage_plays"]]
        assert "LeverageMe" in leverage_names

    def test_identical_ceilings(self):
        """All players with the same ceiling should not cause division errors or crashes."""
        svc = FadeService()
        pool = [
            _make_player(
                player_id=i,
                player_name=f"Player{i}",
                ceiling_fp=40.0,
                ownership_projection=10.0 + i,
            )
            for i in range(10)
        ]
        result = svc.generate_fade_list(pool)
        # Should execute without error and return a valid dict
        assert result["pool_size"] == 10
        assert isinstance(result["fades"], list)
        assert isinstance(result["leverage_plays"], list)

    def test_custom_thresholds(self):
        """Passing custom thresholds changes which players qualify as fades or leverage plays."""
        svc = FadeService()
        pool = []
        for i in range(10):
            pool.append(
                _make_player(
                    player_id=i + 1,
                    player_name=f"Player{i}",
                    ceiling_fp=20.0 + (i * 5),  # 20..65
                    ownership_projection=12.0,
                )
            )

        # With default thresholds (fade>=25%, leverage<=8%), 12% ownership won't fade or leverage
        result_default = svc.generate_fade_list(pool)
        assert len(result_default["fades"]) == 0
        assert len(result_default["leverage_plays"]) == 0

        # Lower fade threshold so 12% ownership qualifies as fade candidate
        svc2 = FadeService()  # new instance to avoid cache
        result_custom = svc2.generate_fade_list(
            pool,
            ownership_threshold_fade=10.0,
            ownership_threshold_leverage=15.0,
        )
        # Now players with 12% ownership can qualify as fade OR leverage depending on ceiling
        total_candidates = len(result_custom["fades"]) + len(result_custom["leverage_plays"])
        assert total_candidates > 0

    def test_limit_parameter(self):
        """The limit parameter caps the number of returned fades and leverage_plays."""
        svc = FadeService()
        pool = []
        # Create many fade candidates: high ownership, low ceiling
        for i in range(15):
            pool.append(
                _make_player(
                    player_id=i + 1,
                    player_name=f"Fade{i}",
                    ceiling_fp=10.0 + i,  # low ceilings: 10-24
                    ownership_projection=30.0 + i,
                )
            )
        # Create many leverage candidates: low ownership, high ceiling
        for i in range(15):
            pool.append(
                _make_player(
                    player_id=100 + i,
                    player_name=f"Leverage{i}",
                    ceiling_fp=80.0 + i,  # high ceilings: 80-94
                    ownership_projection=2.0,
                )
            )

        result = svc.generate_fade_list(pool, limit=3)
        assert len(result["fades"]) <= 3
        assert len(result["leverage_plays"]) <= 3

    def test_fade_score_sorting(self):
        """Fades should be sorted by fade_score in descending order."""
        svc = FadeService()
        pool = []
        # Background players to establish a ceiling range
        for i in range(10):
            pool.append(
                _make_player(
                    player_id=i + 1,
                    player_name=f"Filler{i}",
                    ceiling_fp=20.0 + (i * 8),  # 20..92
                    ownership_projection=5.0,
                )
            )
        # Multiple fade candidates with varying ownership and low ceilings
        pool.append(_make_player(player_id=50, player_name="FadeA", ceiling_fp=22.0, ownership_projection=30.0))
        pool.append(_make_player(player_id=51, player_name="FadeB", ceiling_fp=21.0, ownership_projection=40.0))
        pool.append(_make_player(player_id=52, player_name="FadeC", ceiling_fp=23.0, ownership_projection=35.0))

        result = svc.generate_fade_list(pool)
        fades = result["fades"]
        if len(fades) >= 2:
            for i in range(len(fades) - 1):
                assert fades[i]["fade_score"] >= fades[i + 1]["fade_score"], (
                    f"Fades not sorted descending at index {i}: "
                    f"{fades[i]['fade_score']} < {fades[i+1]['fade_score']}"
                )

    def test_leverage_score_sorting(self):
        """Leverage plays should be sorted by leverage_score in descending order."""
        svc = FadeService()
        pool = []
        # Background players to establish a ceiling range
        for i in range(10):
            pool.append(
                _make_player(
                    player_id=i + 1,
                    player_name=f"Filler{i}",
                    ceiling_fp=20.0 + (i * 8),  # 20..92
                    ownership_projection=15.0,
                )
            )
        # Multiple leverage candidates with varying ownership and high ceilings
        pool.append(_make_player(player_id=60, player_name="LevA", ceiling_fp=85.0, ownership_projection=3.0))
        pool.append(_make_player(player_id=61, player_name="LevB", ceiling_fp=90.0, ownership_projection=5.0))
        pool.append(_make_player(player_id=62, player_name="LevC", ceiling_fp=88.0, ownership_projection=2.0))

        result = svc.generate_fade_list(pool)
        leverages = result["leverage_plays"]
        if len(leverages) >= 2:
            for i in range(len(leverages) - 1):
                assert leverages[i]["leverage_score"] >= leverages[i + 1]["leverage_score"], (
                    f"Leverage plays not sorted descending at index {i}: "
                    f"{leverages[i]['leverage_score']} < {leverages[i+1]['leverage_score']}"
                )
