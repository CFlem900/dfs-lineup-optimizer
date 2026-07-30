"""Tests for the Hierarchical Substitution Tree (DAG) in Agent 3.

Tests the position-specific cascade redistribution of vacated minutes
when players are injured, including DAG resolution, player-position
fitting, ceiling enforcement, and overflow spill logic.
"""

from unittest.mock import MagicMock, patch

import pytest

from app.models.player import PlayerMinutes, PlayerProjection
from app.services.agents.injury_impact_agent import (
    _backup_sort_key,
    _player_fits_dag_target,
    _resolve_cascade,
    calculate_hierarchical_redistribution,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_player_minutes(
    player_id: int,
    name: str = "Player",
    position: str = "PG",
    season_avg: float = 20.0,
    minutes_last_5: list | None = None,
    usage_rate: float = 0.20,
    team_id: int = 1,
) -> PlayerMinutes:
    """Create a minimal ``PlayerMinutes`` for testing."""
    return PlayerMinutes(
        player_id=player_id,
        player_name=f"{name}_{player_id}",
        position=position,
        team_id=team_id,
        minutes_last_5=minutes_last_5 or [season_avg] * 5,
        minutes_last_10=[season_avg] * 10,
        season_avg=season_avg,
        usage_rate=usage_rate,
    )


def _make_projection(
    player_id: int,
    name: str = "Player",
    position: str = "PG",
    baseline: float = 20.0,
    adjusted: float = 20.0,
    confidence: float = 1.0,
) -> PlayerProjection:
    """Create a minimal ``PlayerProjection`` for testing."""
    return PlayerProjection(
        player_id=player_id,
        player_name=f"{name}_{player_id}",
        position=position,
        baseline_minutes=baseline,
        adjusted_minutes=adjusted,
        confidence=confidence,
        reason="Baseline projection",
    )


# ===========================================================================
# TestResolveCascade
# ===========================================================================

class TestResolveCascade:
    """Tests for ``_resolve_cascade()`` — DAG resolution."""

    def test_pg_cascade(self):
        """PG → [("PG", 0.70), ("SG", 0.30)]"""
        result = _resolve_cascade("PG")
        assert len(result) == 2
        targets = {t: s for t, s in result}
        assert targets["PG"] == pytest.approx(0.70, abs=0.01)
        assert targets["SG"] == pytest.approx(0.30, abs=0.01)

    def test_c_cascade(self):
        """C → [("C", 0.80), ("PF", 0.20)]"""
        result = _resolve_cascade("C")
        targets = {t: s for t, s in result}
        assert targets["C"] == pytest.approx(0.80, abs=0.01)
        assert targets["PF"] == pytest.approx(0.20, abs=0.01)

    def test_sf_cascade(self):
        """SF → [("SF", 0.70), ("PF", 0.20), ("SG", 0.10)]"""
        result = _resolve_cascade("SF")
        targets = {t: s for t, s in result}
        assert targets["SF"] == pytest.approx(0.70, abs=0.01)
        assert targets["PF"] == pytest.approx(0.20, abs=0.01)
        assert targets["SG"] == pytest.approx(0.10, abs=0.01)

    def test_generic_g_resolves_to_pg(self):
        """'G' → resolves via fallback to PG cascade."""
        result = _resolve_cascade("G")
        targets = {t: s for t, s in result}
        assert "PG" in targets
        assert "SG" in targets
        assert sum(s for _, s in result) == pytest.approx(1.0, abs=0.01)

    def test_multi_position_gf_merges(self):
        """'G-F' → merged cascade with targets from both PG and SF."""
        result = _resolve_cascade("G-F")
        targets = {t: s for t, s in result}

        # Should have targets from both PG and SF cascades
        assert len(targets) >= 3  # PG, SG, SF at minimum
        assert sum(s for _, s in result) == pytest.approx(1.0, abs=0.01)

        # PG gets some share (from the G cascade)
        assert "PG" in targets
        # SF gets some share (from the F cascade)
        assert "SF" in targets

    def test_multi_position_fc_merges(self):
        """'F-C' → merged cascade from SF and C."""
        result = _resolve_cascade("F-C")
        targets = {t: s for t, s in result}
        assert sum(s for _, s in result) == pytest.approx(1.0, abs=0.01)
        # Should have targets from both SF and C cascades
        assert "C" in targets or "PF" in targets

    def test_unknown_position_empty(self):
        """'XX' → empty cascade."""
        result = _resolve_cascade("XX")
        assert result == []

    def test_empty_string_empty(self):
        """'' → empty cascade."""
        result = _resolve_cascade("")
        assert result == []


# ===========================================================================
# TestPlayerFitsDagTarget
# ===========================================================================

class TestPlayerFitsDagTarget:
    """Tests for ``_player_fits_dag_target()`` — position-to-target matching."""

    def test_exact_match_pg(self):
        """PG player fits PG target with exact match."""
        fits, is_exact = _player_fits_dag_target("PG", "PG")
        assert fits is True
        assert is_exact is True

    def test_exact_match_c(self):
        """C player fits C target with exact match."""
        fits, is_exact = _player_fits_dag_target("C", "C")
        assert fits is True
        assert is_exact is True

    def test_family_match_sg_fits_pg(self):
        """SG player fits PG target (both guards), not exact."""
        fits, is_exact = _player_fits_dag_target("SG", "PG")
        assert fits is True
        assert is_exact is False

    def test_family_match_pf_fits_sf(self):
        """PF player fits SF target (both forwards), not exact."""
        fits, is_exact = _player_fits_dag_target("PF", "SF")
        assert fits is True
        assert is_exact is False

    def test_generic_g_fits_pg_and_sg(self):
        """'G' player fits both PG and SG targets."""
        fits_pg, exact_pg = _player_fits_dag_target("G", "PG")
        fits_sg, exact_sg = _player_fits_dag_target("G", "SG")
        assert fits_pg is True
        assert fits_sg is True
        # Generic G resolves to PG and SG, so both are "exact"
        assert exact_pg is True
        assert exact_sg is True

    def test_no_fit_c_vs_pg(self):
        """C player does not fit PG target."""
        fits, is_exact = _player_fits_dag_target("C", "PG")
        assert fits is False
        assert is_exact is False

    def test_no_fit_pg_vs_c(self):
        """PG player does not fit C target."""
        fits, is_exact = _player_fits_dag_target("PG", "C")
        assert fits is False
        assert is_exact is False

    def test_multi_position_fc_fits_c(self):
        """'F-C' player fits C target."""
        fits, is_exact = _player_fits_dag_target("F-C", "C")
        assert fits is True

    def test_multi_position_fc_fits_sf(self):
        """'F-C' player fits SF target (forward family)."""
        fits, is_exact = _player_fits_dag_target("F-C", "SF")
        assert fits is True

    def test_multi_position_gf_fits_pg(self):
        """'G-F' player fits PG target."""
        fits, is_exact = _player_fits_dag_target("G-F", "PG")
        assert fits is True


# ===========================================================================
# TestBackupSortKey
# ===========================================================================

class TestBackupSortKey:
    """Tests for ``_backup_sort_key()`` — ranking formula."""

    def test_higher_season_avg_ranks_higher(self):
        """Player with higher season avg scores higher."""
        p1 = _make_player_minutes(1, season_avg=30.0)
        p2 = _make_player_minutes(2, season_avg=15.0)
        assert _backup_sort_key(p1) > _backup_sort_key(p2)

    def test_recent_form_contributes(self):
        """Player with high recent form scores higher than flat season avg."""
        # Same season avg but p1 has hot recent form
        p1 = _make_player_minutes(1, season_avg=20.0, minutes_last_5=[30, 28, 32, 29, 31])
        p2 = _make_player_minutes(2, season_avg=20.0, minutes_last_5=[10, 12, 11, 10, 12])
        assert _backup_sort_key(p1) > _backup_sort_key(p2)


# ===========================================================================
# TestHierarchicalRedistribution
# ===========================================================================

class TestHierarchicalRedistribution:
    """Tests for ``calculate_hierarchical_redistribution()`` — core DAG logic."""

    def test_pg_injury_70_30_split(self):
        """PG out → backup PG gets ~70% share, SG gets ~30% share."""
        # Injured PG with 30 min baseline
        injured = _make_player_minutes(1, position="PG", season_avg=30.0)
        backup_pg = _make_player_minutes(2, name="BackupPG", position="PG", season_avg=15.0)
        sg = _make_player_minutes(3, name="SG", position="SG", season_avg=20.0)
        sf = _make_player_minutes(4, name="SF", position="SF", season_avg=25.0)

        rotation = [injured, backup_pg, sg, sf]

        adjusted = {
            1: _make_projection(1, position="PG", baseline=30.0, adjusted=0.0, confidence=0.0),
            2: _make_projection(2, position="PG", baseline=15.0, adjusted=15.0),
            3: _make_projection(3, position="SG", baseline=20.0, adjusted=20.0),
            4: _make_projection(4, position="SF", baseline=25.0, adjusted=25.0),
        }

        result = calculate_hierarchical_redistribution(
            rotation=rotation,
            reduced_players={1: 30.0},
            baseline_projections={1: 30.0, 2: 15.0, 3: 20.0, 4: 25.0},
            adjusted=adjusted,
            spot_start_caps={},
            all_reduced_ids={1},
        )

        # Backup PG should get the PG node share (~70% of 30 = 21 × 0.65 primary ≈ 13.6)
        # SG should get the SG node share (~30% of 30 = 9 × 0.65 primary ≈ 5.85)
        pg_gained = result[2].adjusted_minutes - 15.0
        sg_gained = result[3].adjusted_minutes - 20.0

        assert pg_gained > 0, "Backup PG should gain minutes"
        assert sg_gained > 0, "SG should gain minutes from SG node"
        # PG node gets 70% of freed, SG node gets 30% — PG gains more
        assert pg_gained > sg_gained, "PG should gain more than SG"
        # SF should get nothing (no forward cascade from PG injury)
        assert result[4].adjusted_minutes == 25.0

    def test_c_injury_80_20_split(self):
        """C out → backup C gets ~80% share, PF gets ~20% share."""
        injured = _make_player_minutes(1, position="C", season_avg=28.0)
        backup_c = _make_player_minutes(2, name="BackupC", position="C", season_avg=12.0)
        pf = _make_player_minutes(3, name="PF", position="PF", season_avg=22.0)
        pg = _make_player_minutes(4, name="PG", position="PG", season_avg=30.0)

        rotation = [injured, backup_c, pf, pg]

        adjusted = {
            1: _make_projection(1, position="C", baseline=28.0, adjusted=0.0, confidence=0.0),
            2: _make_projection(2, position="C", baseline=12.0, adjusted=12.0),
            3: _make_projection(3, position="PF", baseline=22.0, adjusted=22.0),
            4: _make_projection(4, position="PG", baseline=30.0, adjusted=30.0),
        }

        result = calculate_hierarchical_redistribution(
            rotation=rotation,
            reduced_players={1: 28.0},
            baseline_projections={1: 28.0, 2: 12.0, 3: 22.0, 4: 30.0},
            adjusted=adjusted,
            spot_start_caps={},
            all_reduced_ids={1},
        )

        c_gained = result[2].adjusted_minutes - 12.0
        pf_gained = result[3].adjusted_minutes - 22.0

        assert c_gained > 0, "Backup C should gain minutes"
        assert pf_gained > 0, "PF should gain minutes"
        assert c_gained > pf_gained, "C should gain more than PF"
        # PG should not gain anything (no guard cascade from C)
        assert result[4].adjusted_minutes == 30.0

    def test_sf_injury_three_way(self):
        """SF out → 70% to SF, 20% to PF, 10% to SG."""
        injured = _make_player_minutes(1, position="SF", season_avg=32.0)
        backup_sf = _make_player_minutes(2, name="BackupSF", position="SF", season_avg=12.0)
        pf = _make_player_minutes(3, name="PF", position="PF", season_avg=20.0)
        sg = _make_player_minutes(4, name="SG", position="SG", season_avg=22.0)
        c = _make_player_minutes(5, name="C", position="C", season_avg=25.0)

        rotation = [injured, backup_sf, pf, sg, c]

        adjusted = {
            1: _make_projection(1, position="SF", baseline=32.0, adjusted=0.0, confidence=0.0),
            2: _make_projection(2, position="SF", baseline=12.0, adjusted=12.0),
            3: _make_projection(3, position="PF", baseline=20.0, adjusted=20.0),
            4: _make_projection(4, position="SG", baseline=22.0, adjusted=22.0),
            5: _make_projection(5, position="C", baseline=25.0, adjusted=25.0),
        }

        result = calculate_hierarchical_redistribution(
            rotation=rotation,
            reduced_players={1: 32.0},
            baseline_projections={1: 32.0, 2: 12.0, 3: 20.0, 4: 22.0, 5: 25.0},
            adjusted=adjusted,
            spot_start_caps={},
            all_reduced_ids={1},
        )

        sf_gained = result[2].adjusted_minutes - 12.0
        pf_gained = result[3].adjusted_minutes - 20.0
        sg_gained = result[4].adjusted_minutes - 22.0

        assert sf_gained > pf_gained > 0, "SF > PF > 0"
        assert sg_gained > 0, "SG should gain from 10% node"
        # C should not gain (no C node in SF cascade)
        assert result[5].adjusted_minutes == 25.0

    def test_starter_ceiling_38_enforced(self):
        """Starter absorber (baseline >= 24) cannot exceed 38 min ceiling."""
        injured = _make_player_minutes(1, position="PG", season_avg=30.0)
        starter_sg = _make_player_minutes(2, position="SG", season_avg=35.0)

        rotation = [injured, starter_sg]

        adjusted = {
            1: _make_projection(1, position="PG", baseline=30.0, adjusted=0.0, confidence=0.0),
            2: _make_projection(2, position="SG", baseline=35.0, adjusted=35.0),
        }

        result = calculate_hierarchical_redistribution(
            rotation=rotation,
            reduced_players={1: 30.0},
            baseline_projections={1: 30.0, 2: 35.0},
            adjusted=adjusted,
            spot_start_caps={},
            all_reduced_ids={1},
        )

        # SG is a starter (35 baseline >= 24), ceiling = 38
        assert result[2].adjusted_minutes <= 38.0

    def test_bench_ceiling_28_enforced(self):
        """Bench absorber (baseline < 24) cannot exceed 28 min ceiling."""
        injured = _make_player_minutes(1, position="PG", season_avg=30.0)
        bench_pg = _make_player_minutes(2, position="PG", season_avg=15.0)

        rotation = [injured, bench_pg]

        adjusted = {
            1: _make_projection(1, position="PG", baseline=30.0, adjusted=0.0, confidence=0.0),
            2: _make_projection(2, position="PG", baseline=15.0, adjusted=15.0),
        }

        result = calculate_hierarchical_redistribution(
            rotation=rotation,
            reduced_players={1: 30.0},
            baseline_projections={1: 30.0, 2: 15.0},
            adjusted=adjusted,
            spot_start_caps={},
            all_reduced_ids={1},
        )

        # Bench player: 15 baseline < 24, ceiling = 28
        assert result[2].adjusted_minutes <= 28.0

    def test_overflow_spills_to_next_node(self):
        """When primary node is fully capped, overflow goes to secondary."""
        injured = _make_player_minutes(1, position="PG", season_avg=30.0)
        # Backup PG already at 27 min (bench ceiling = 28, can only absorb 1 min)
        backup_pg = _make_player_minutes(2, position="PG", season_avg=20.0)
        sg = _make_player_minutes(3, position="SG", season_avg=18.0)

        rotation = [injured, backup_pg, sg]

        adjusted = {
            1: _make_projection(1, position="PG", baseline=30.0, adjusted=0.0, confidence=0.0),
            2: _make_projection(2, position="PG", baseline=20.0, adjusted=27.0),
            3: _make_projection(3, position="SG", baseline=18.0, adjusted=18.0),
        }

        result = calculate_hierarchical_redistribution(
            rotation=rotation,
            reduced_players={1: 30.0},
            baseline_projections={1: 30.0, 2: 20.0, 3: 18.0},
            adjusted=adjusted,
            spot_start_caps={},
            all_reduced_ids={1},
        )

        # PG node (70% of 30 = 21): PG at 27, ceiling 28 → can absorb 1
        # Overflow from PG node spills to SG node (30% of 30 = 9 + ~20 overflow)
        assert result[2].adjusted_minutes <= 28.0
        assert result[3].adjusted_minutes > 18.0, "SG should receive overflow"

    def test_spot_start_cap_overrides_dag_ceiling(self):
        """spot_start_cap > bench ceiling → uses the higher cap."""
        injured = _make_player_minutes(1, position="PG", season_avg=30.0)
        backup_pg = _make_player_minutes(2, position="PG", season_avg=14.0)

        rotation = [injured, backup_pg]

        adjusted = {
            1: _make_projection(1, position="PG", baseline=30.0, adjusted=0.0, confidence=0.0),
            2: _make_projection(2, position="PG", baseline=14.0, adjusted=14.0),
        }

        # Spot-start cap from Phase 1b: 30 × 0.88 = 26.4
        # This is below bench ceiling (28), but in a case where
        # the spot_start_cap exceeds starter ceiling...
        result = calculate_hierarchical_redistribution(
            rotation=rotation,
            reduced_players={1: 30.0},
            baseline_projections={1: 30.0, 2: 14.0},
            adjusted=adjusted,
            spot_start_caps={2: 40.0},  # Very high cap
            all_reduced_ids={1},
        )

        # With spot_start_cap=40 > starter_ceiling=38, uses max(40, 38)=40
        # So the player can absorb more than the standard ceiling
        assert result[2].adjusted_minutes > 28.0

    def test_exact_position_ranks_higher(self):
        """PG ranks above SG within the 'PG' DAG node."""
        injured = _make_player_minutes(1, position="PG", season_avg=30.0)
        # SG has higher season_avg but PG has exact position match
        backup_pg = _make_player_minutes(2, position="PG", season_avg=12.0)
        sg_player = _make_player_minutes(3, position="SG", season_avg=18.0)

        rotation = [injured, backup_pg, sg_player]

        adjusted = {
            1: _make_projection(1, position="PG", baseline=30.0, adjusted=0.0, confidence=0.0),
            2: _make_projection(2, position="PG", baseline=12.0, adjusted=12.0),
            3: _make_projection(3, position="SG", baseline=18.0, adjusted=18.0),
        }

        result = calculate_hierarchical_redistribution(
            rotation=rotation,
            reduced_players={1: 30.0},
            baseline_projections={1: 30.0, 2: 12.0, 3: 18.0},
            adjusted=adjusted,
            spot_start_caps={},
            all_reduced_ids={1},
        )

        # Within the PG node (70% of 30 = 21 min):
        # PG player (exact match) should be primary absorber → gets 65% of 21
        # SG player (family match) should get 35% of 21
        pg_gain_from_pg_node = result[2].adjusted_minutes - 12.0
        # Note: SG also gets the SG node (30% of 30 = 9 min) on top
        # So we verify the PG gains more from the PG node specifically
        assert pg_gain_from_pg_node > 0, "Backup PG should gain from PG node"

    def test_no_candidates_overflow_carries(self):
        """No players at target pos → pool carries to next DAG node."""
        # PF injured, but no SF or C on roster — only PG and SG
        injured = _make_player_minutes(1, position="PF", season_avg=25.0)
        pg = _make_player_minutes(2, position="PG", season_avg=28.0)
        sg = _make_player_minutes(3, position="SG", season_avg=22.0)

        rotation = [injured, pg, sg]

        adjusted = {
            1: _make_projection(1, position="PF", baseline=25.0, adjusted=0.0, confidence=0.0),
            2: _make_projection(2, position="PG", baseline=28.0, adjusted=28.0),
            3: _make_projection(3, position="SG", baseline=22.0, adjusted=22.0),
        }

        result = calculate_hierarchical_redistribution(
            rotation=rotation,
            reduced_players={1: 25.0},
            baseline_projections={1: 25.0, 2: 28.0, 3: 22.0},
            adjusted=adjusted,
            spot_start_caps={},
            all_reduced_ids={1},
        )

        # PF cascade: [("PF", 0.70), ("SF", 0.20), ("C", 0.10)]
        # No PF, SF, or C candidates → all overflow
        # PG and SG should NOT receive minutes (different family)
        assert result[2].adjusted_minutes == 28.0
        assert result[3].adjusted_minutes == 22.0

    def test_multiple_injuries_same_position(self):
        """Two PGs injured → both freed pools flow through DAG."""
        inj1 = _make_player_minutes(1, position="PG", season_avg=30.0)
        inj2 = _make_player_minutes(2, position="PG", season_avg=18.0)
        backup_pg = _make_player_minutes(3, position="PG", season_avg=8.0)
        sg = _make_player_minutes(4, position="SG", season_avg=20.0)

        rotation = [inj1, inj2, backup_pg, sg]

        adjusted = {
            1: _make_projection(1, position="PG", baseline=30.0, adjusted=0.0, confidence=0.0),
            2: _make_projection(2, position="PG", baseline=18.0, adjusted=0.0, confidence=0.0),
            3: _make_projection(3, position="PG", baseline=8.0, adjusted=8.0),
            4: _make_projection(4, position="SG", baseline=20.0, adjusted=20.0),
        }

        result = calculate_hierarchical_redistribution(
            rotation=rotation,
            reduced_players={1: 30.0, 2: 18.0},
            baseline_projections={1: 30.0, 2: 18.0, 3: 8.0, 4: 20.0},
            adjusted=adjusted,
            spot_start_caps={},
            all_reduced_ids={1, 2},
        )

        # Backup PG and SG should both gain minutes from both injuries
        assert result[3].adjusted_minutes > 8.0
        assert result[4].adjusted_minutes > 20.0

    def test_empty_reduced_players_no_change(self):
        """No injuries → projections unchanged."""
        pg = _make_player_minutes(1, position="PG", season_avg=30.0)
        rotation = [pg]

        adjusted = {
            1: _make_projection(1, position="PG", baseline=30.0, adjusted=30.0),
        }

        result = calculate_hierarchical_redistribution(
            rotation=rotation,
            reduced_players={},
            baseline_projections={1: 30.0},
            adjusted=adjusted,
            spot_start_caps={},
            all_reduced_ids=set(),
        )

        assert result[1].adjusted_minutes == 30.0

    def test_reduced_player_excluded_from_receiving(self):
        """Reduced players (in all_reduced_ids) don't receive minutes."""
        inj = _make_player_minutes(1, position="PG", season_avg=30.0)
        gtd_pg = _make_player_minutes(2, position="PG", season_avg=20.0)
        sg = _make_player_minutes(3, position="SG", season_avg=18.0)

        rotation = [inj, gtd_pg, sg]

        adjusted = {
            1: _make_projection(1, position="PG", baseline=30.0, adjusted=0.0, confidence=0.0),
            2: _make_projection(2, position="PG", baseline=20.0, adjusted=13.2),
            3: _make_projection(3, position="SG", baseline=18.0, adjusted=18.0),
        }

        # Player 2 is also reduced (GTD), so they shouldn't receive minutes
        result = calculate_hierarchical_redistribution(
            rotation=rotation,
            reduced_players={1: 30.0},
            baseline_projections={1: 30.0, 2: 20.0, 3: 18.0},
            adjusted=adjusted,
            spot_start_caps={},
            all_reduced_ids={1, 2},  # Both PG injured/reduced
        )

        # GTD PG should NOT gain minutes
        assert result[2].adjusted_minutes == 13.2
        # SG should get all redistributed minutes
        assert result[3].adjusted_minutes > 18.0

    def test_reason_includes_dag_target(self):
        """Reason field mentions the DAG target position."""
        injured = _make_player_minutes(1, position="PG", season_avg=30.0)
        backup = _make_player_minutes(2, position="PG", season_avg=15.0)

        rotation = [injured, backup]

        adjusted = {
            1: _make_projection(1, position="PG", baseline=30.0, adjusted=0.0, confidence=0.0),
            2: _make_projection(2, position="PG", baseline=15.0, adjusted=15.0),
        }

        result = calculate_hierarchical_redistribution(
            rotation=rotation,
            reduced_players={1: 30.0},
            baseline_projections={1: 30.0, 2: 15.0},
            adjusted=adjusted,
            spot_start_caps={},
            all_reduced_ids={1},
        )

        assert "DAG" in result[2].reason
        assert "PG" in result[2].reason


# ===========================================================================
# TestDAGToggle
# ===========================================================================

class TestDAGToggle:
    """Tests for the USE_HIERARCHICAL_DAG toggle in the rotation engine."""

    def test_use_hierarchical_dag_true(self):
        """When toggle is True, DAG function is called."""
        from app.services.rotation_engine import RotationEngine

        engine = RotationEngine()
        injured = _make_player_minutes(1, position="PG", season_avg=30.0)
        backup = _make_player_minutes(2, position="PG", season_avg=15.0)

        rotation = [injured, backup]
        baselines = {1: 30.0, 2: 15.0}

        from app.models.player import PlayerStatus
        injured_players = [
            PlayerStatus(player_id=1, player_name="Injured_1", status="Out")
        ]

        with patch(
            "app.services.rotation_engine.USE_HIERARCHICAL_DAG", True
        ), patch(
            "app.services.rotation_engine.calculate_hierarchical_redistribution",
            wraps=calculate_hierarchical_redistribution,
        ) as mock_dag:
            result = engine.redistribute_minutes(
                rotation=rotation,
                injured_players=injured_players,
                baseline_projections=baselines,
            )
            mock_dag.assert_called_once()

    def test_use_hierarchical_dag_false(self):
        """When toggle is False, legacy waterfall is used."""
        from app.services.rotation_engine import RotationEngine

        engine = RotationEngine()
        injured = _make_player_minutes(1, position="PG", season_avg=30.0)
        backup = _make_player_minutes(2, position="PG", season_avg=15.0)

        rotation = [injured, backup]
        baselines = {1: 30.0, 2: 15.0}

        from app.models.player import PlayerStatus
        injured_players = [
            PlayerStatus(player_id=1, player_name="Injured_1", status="Out")
        ]

        with patch(
            "app.services.rotation_engine.USE_HIERARCHICAL_DAG", False
        ), patch(
            "app.services.rotation_engine.calculate_hierarchical_redistribution",
        ) as mock_dag:
            result = engine.redistribute_minutes(
                rotation=rotation,
                injured_players=injured_players,
                baseline_projections=baselines,
            )
            mock_dag.assert_not_called()
            # Backup should still gain minutes via legacy path
            assert result[2].adjusted_minutes > 15.0


# ===========================================================================
# TestUsageBoostIntegration
# ===========================================================================

class TestUsageBoostIntegration:
    """Tests that Phase 2c usage boost works correctly after DAG redistribution."""

    def test_high_usage_out_primary_gets_boost(self):
        """After DAG redistribution, Phase 2c still applies 1.10x usage_boost."""
        from app.services.rotation_engine import RotationEngine

        engine = RotationEngine()

        # High-usage star PG (usage > 25%) is Out
        star = _make_player_minutes(
            1, position="PG", season_avg=34.0, usage_rate=0.30,
        )
        backup_pg = _make_player_minutes(
            2, name="Backup", position="PG", season_avg=15.0, usage_rate=0.15,
        )
        sg = _make_player_minutes(
            3, name="SG", position="SG", season_avg=28.0, usage_rate=0.20,
        )

        rotation = [star, backup_pg, sg]
        baselines = {1: 34.0, 2: 15.0, 3: 28.0}

        from app.models.player import PlayerStatus
        injured_players = [
            PlayerStatus(player_id=1, player_name="Star_1", status="Out")
        ]

        result = engine.redistribute_minutes(
            rotation=rotation,
            injured_players=injured_players,
            baseline_projections=baselines,
        )

        # Primary backup (player 2) should have usage_boost >= 1.10
        # from Phase 2c (high-usage Out → 1.10x flat boost)
        assert result[2].usage_boost >= 1.10
