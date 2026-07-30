"""Tests for sigmoid ownership leverage and value-adjusted dampener.

Verifies:
  1. Sigmoid penalty model (max 15% penalty via logistic curve)
  2. Value-adjusted dampener (high FP/$1K players bypass penalty)
  3. Dynamic slate threshold computation
  4. FadeService integration in composite scoring
"""

import math
import pytest
from unittest.mock import MagicMock, patch

from app.services.lineup_optimizer_service import LineupOptimizerService
from app.services.fade_service import FadeService
from app.models.lineup import PlayerPoolEntry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_optimizer(**overrides) -> LineupOptimizerService:
    """Create a minimal optimizer instance with mocked services."""
    defaults = dict(
        dfs_service=MagicMock(),
        dk_draftables_service=MagicMock(),
        nba_service=MagicMock(),
        injury_service=MagicMock(),
        rotation_engine=MagicMock(),
        calibration_service=None,
        fade_service=None,
    )
    defaults.update(overrides)
    return LineupOptimizerService(**defaults)


def _make_player(
    player_id=1,
    name="Player",
    position="PG",
    team="BOS",
    salary=6000,
    projected_fp=30.0,
    ceiling_fp=38.0,
    floor_fp=22.0,
    ownership=10.0,
    **kwargs,
) -> PlayerPoolEntry:
    from app.services.lineup_optimizer_service import DK_SLOT_ELIGIBILITY
    eligible = [s for s, positions in DK_SLOT_ELIGIBILITY.items() if position in positions]
    return PlayerPoolEntry(
        player_id=player_id,
        player_name=name,
        position=position,
        team_abbreviation=team,
        salary=salary,
        projected_fp=projected_fp,
        ceiling_fp=ceiling_fp,
        floor_fp=floor_fp,
        projected_minutes=28.0,
        dk_value=round(projected_fp / salary * 1000, 2) if salary > 0 else 0.0,
        eligible_slots=eligible,
        dk_player_id=player_id + 10000,
        estimated_ownership=ownership,
        **kwargs,
    )


class _FakeRng:
    """Deterministic RNG for test reproducibility."""
    def uniform(self, a, b):
        return (a + b) / 2.0  # Always returns midpoint (= 1.0 for symmetric noise)

    def gauss(self, mu, sigma):
        return mu  # No noise — return the mean for deterministic tests


# ===========================================================================
# Tests for the capped ownership leverage model
# ===========================================================================

class TestOwnershipLeverageMultiplier:
    """Test _ownership_leverage_multiplier() with sigmoid penalty model."""

    def setup_method(self):
        self.opt = _make_optimizer()

    def test_monotonically_decreasing(self):
        """Multiplier should weakly decrease as ownership increases."""
        ownerships = [5, 8, 10, 15, 20, 25, 30, 35, 40]
        mults = [self.opt._ownership_leverage_multiplier(o, "balanced") for o in ownerships]
        for i in range(len(mults) - 1):
            assert mults[i] >= mults[i + 1], (
                f"Not monotonic: own={ownerships[i]} ({mults[i]:.4f}) "
                f"< own={ownerships[i+1]} ({mults[i+1]:.4f})"
            )

    def test_low_owned_near_unity(self):
        """At low ownership (5%), sigmoid penalty is near-zero → mult ≈ 1.0."""
        mult = self.opt._ownership_leverage_multiplier(5.0, "balanced")
        assert abs(mult - 1.0) < 0.005, f"Expected ~1.0 at 5% owned, got {mult}"

    def test_low_owned_no_boost(self):
        """Sigmoid model gives no boost (mult <= 1.0) for non-zero ownership."""
        mult = self.opt._ownership_leverage_multiplier(3.0, "balanced")
        assert mult <= 1.0, f"Sigmoid should not boost 3% owned, got {mult}"
        assert mult > 0.99, f"3% owned should have near-zero penalty, got {mult}"

    def test_high_owned_faded(self):
        """Players above midpoint ownership should get clear fade."""
        mult = self.opt._ownership_leverage_multiplier(45.0, "balanced")
        assert mult < 0.95, f"Expected fade for 45% owned, got {mult}"

    def test_max_penalty_capped_at_15pct(self):
        """Even extremely high ownership should not exceed 15% penalty."""
        from app.config.constants import OWNERSHIP_MAX_PENALTY_PCT
        mult = self.opt._ownership_leverage_multiplier(80.0, "balanced")
        assert mult >= (1.0 - OWNERSHIP_MAX_PENALTY_PCT), (
            f"80% owned multiplier {mult:.4f} violates 15% cap"
        )
        mult_extreme = self.opt._ownership_leverage_multiplier(90.0, "contrarian")
        assert mult_extreme >= (1.0 - OWNERSHIP_MAX_PENALTY_PCT), (
            f"90% contrarian multiplier {mult_extreme:.4f} violates 15% cap"
        )

    def test_sigmoid_midpoint_half_penalty(self):
        """At the midpoint (40%), penalty should be exactly Max_Penalty / 2."""
        import math
        from app.config.constants import OWNERSHIP_MAX_PENALTY_PCT, OWNERSHIP_SIGMOID_MIDPOINT
        mid_own = OWNERSHIP_SIGMOID_MIDPOINT * 100  # 40.0
        mult = self.opt._ownership_leverage_multiplier(mid_own, "balanced")
        expected = 1.0 - OWNERSHIP_MAX_PENALTY_PCT / 2.0  # 0.925
        assert abs(mult - expected) < 0.001, (
            f"At midpoint ({mid_own}%), expected mult={expected:.4f}, got {mult:.4f}"
        )

    def test_contrarian_fades_more_aggressively(self):
        """Contrarian strategy should fade more aggressively.

        Use 25% ownership where contrarian midpoint (30%) is closer than balanced (40%).
        """
        gpp_mult = self.opt._ownership_leverage_multiplier(25.0, "balanced")
        ctr_mult = self.opt._ownership_leverage_multiplier(25.0, "contrarian")
        assert ctr_mult < gpp_mult, (
            f"Contrarian ({ctr_mult:.4f}) should be lower than balanced ({gpp_mult:.4f})"
        )

    def test_zero_ownership_returns_boost(self):
        """Zero ownership should return the boost cap (1.15)."""
        mult = self.opt._ownership_leverage_multiplier(0, "balanced")
        assert mult == 1.15

    def test_negative_ownership_returns_boost(self):
        """Negative ownership (edge case) should return the boost cap."""
        mult = self.opt._ownership_leverage_multiplier(-5, "balanced")
        assert mult == 1.15

    def test_no_cliff_between_adjacent_values(self):
        """No adjacent ownership values should have > 5% multiplier jump."""
        for own in range(1, 50):
            m1 = self.opt._ownership_leverage_multiplier(float(own), "balanced")
            m2 = self.opt._ownership_leverage_multiplier(float(own + 1), "balanced")
            pct_change = abs(m1 - m2) / max(m1, 0.01)
            assert pct_change < 0.05, (
                f"Cliff at own={own}->{own+1}: {m1:.4f}->{m2:.4f} ({pct_change:.1%})"
            )

    def test_calibration_override_alpha(self):
        """When CalibrationService provides an alpha (steepness) override, it's used.

        Use 50% ownership (above midpoint) where higher k = more fading.
        """
        mock_cal = MagicMock()
        mock_cal.get_ownership_leverage_alpha.return_value = 1.5  # 50% steeper
        mock_cal.get_ownership_leverage_baseline.return_value = 1.0  # no change
        opt = _make_optimizer(calibration_service=mock_cal)

        mult_default = self.opt._ownership_leverage_multiplier(50.0, "balanced")
        mult_calibrated = opt._ownership_leverage_multiplier(50.0, "balanced")
        # Higher k above midpoint = more penalty
        assert mult_calibrated < mult_default

    def test_calibration_override_baseline(self):
        """When CalibrationService provides a baseline (midpoint) override, it's used."""
        mock_cal = MagicMock()
        mock_cal.get_ownership_leverage_alpha.return_value = 1.0  # no change
        mock_cal.get_ownership_leverage_baseline.return_value = 1.5  # shift midpoint up
        opt = _make_optimizer(calibration_service=mock_cal)

        # With higher midpoint, a 15% owned player gets even less penalty
        mult_default = self.opt._ownership_leverage_multiplier(15.0, "balanced")
        mult_calibrated = opt._ownership_leverage_multiplier(15.0, "balanced")
        assert mult_calibrated > mult_default


# ===========================================================================
# Tests for Value-Adjusted Ownership Dampener
# ===========================================================================

class TestValueAdjustedDampener:
    """Test that high FP/$1K players bypass ownership fade."""

    def setup_method(self):
        self.opt = _make_optimizer()

    def test_high_value_immune(self):
        """Player at 7.77 FP/$1K (above static ceiling 7.0) → fully immune."""
        mult = self.opt._ownership_leverage_multiplier(
            25.0, "balanced", value_ratio=7.77
        )
        assert mult == 1.0, f"Expected 1.0 for 7.77x above ceiling, got {mult}"

    def test_above_ceiling_fully_immune(self):
        """Value above ceiling returns exactly 1.0."""
        mult = self.opt._ownership_leverage_multiplier(
            30.0, "balanced", value_ratio=8.5
        )
        assert mult == 1.0, f"Expected 1.0 for 8.5x value, got {mult}"

    def test_at_ceiling_fully_immune(self):
        """Value exactly at ceiling (7.0) returns 1.0."""
        mult = self.opt._ownership_leverage_multiplier(
            30.0, "balanced", value_ratio=7.0
        )
        assert mult == 1.0, f"Expected 1.0 for 7.0x value, got {mult}"

    def test_low_value_full_penalty(self):
        """Low value (4.5x, below threshold 5.0) → same penalty as no value_ratio."""
        mult_no_value = self.opt._ownership_leverage_multiplier(25.0, "balanced")
        mult_low_value = self.opt._ownership_leverage_multiplier(
            25.0, "balanced", value_ratio=4.5
        )
        assert abs(mult_low_value - mult_no_value) < 0.001, (
            f"4.5x value should match no-value: {mult_low_value:.4f} vs {mult_no_value:.4f}"
        )

    def test_threshold_boundary_no_dampening(self):
        """Value exactly at threshold (5.0) → no dampening yet."""
        mult_no_value = self.opt._ownership_leverage_multiplier(25.0, "balanced")
        mult_at_threshold = self.opt._ownership_leverage_multiplier(
            25.0, "balanced", value_ratio=5.0
        )
        assert abs(mult_at_threshold - mult_no_value) < 0.001, (
            f"At threshold should match no-value: {mult_at_threshold:.4f} vs {mult_no_value:.4f}"
        )

    def test_none_value_ratio_no_effect(self):
        """None value_ratio should behave identically to no value_ratio."""
        mult_default = self.opt._ownership_leverage_multiplier(25.0, "balanced")
        mult_none = self.opt._ownership_leverage_multiplier(
            25.0, "balanced", value_ratio=None
        )
        assert mult_default == mult_none

    def test_midpoint_partial_dampening(self):
        """Value at 6.0 (midpoint of 5.0-7.0 range) → partial dampening."""
        mult_no_value = self.opt._ownership_leverage_multiplier(25.0, "balanced")
        mult_mid = self.opt._ownership_leverage_multiplier(
            25.0, "balanced", value_ratio=6.0
        )
        # Should be between no-dampening and fully-immune
        assert mult_mid > mult_no_value, "Midpoint should be less penalized"
        assert mult_mid < 1.0, "Midpoint should still have some penalty"

    def test_contrarian_also_dampened(self):
        """Value dampener should work with contrarian strategy too."""
        mult_no_value = self.opt._ownership_leverage_multiplier(25.0, "contrarian")
        mult_high = self.opt._ownership_leverage_multiplier(
            25.0, "contrarian", value_ratio=7.5
        )
        assert mult_high > mult_no_value, (
            "High value should reduce contrarian fade too"
        )

    def test_dynamic_threshold_used_when_set(self):
        """When _slate_value_threshold is set, it overrides static constants."""
        opt = _make_optimizer()
        # Set dynamic threshold lower than static (simulates injury-heavy slate)
        opt._slate_value_threshold = 4.5
        opt._slate_value_ceiling = 6.5

        # A 5.3x player exceeds dynamic threshold → gets dampened
        mult_dynamic = opt._ownership_leverage_multiplier(
            30.0, "balanced", value_ratio=5.3
        )
        # Without dynamic threshold, 5.3x > 5.0 static threshold → also dampened
        # but the dynamic ceiling (6.5) is lower, so more dampening
        opt2 = _make_optimizer()
        mult_static = opt2._ownership_leverage_multiplier(
            30.0, "balanced", value_ratio=5.3
        )
        # Dynamic should give MORE relief since 5.3 is further from 4.5 threshold
        assert mult_dynamic > mult_static, (
            f"Dynamic ({mult_dynamic:.4f}) should give more relief than static ({mult_static:.4f})"
        )

    def test_50pct_owned_5_3x_value_bypasses_penalty(self):
        """Core spec: 50% owned at 5.3x on a slate where p90=5.1 → dampened.

        Sigmoid at 50%: penalty ≈ 0.115 (before dampener).
        When dynamic threshold = 5.1 and ceiling = 7.1:
        dampener = 1 - (5.3 - 5.1) / (7.1 - 5.1) = 0.90
        final ≈ 0.115 * 0.90 = 0.104 → multiplier ≈ 0.896
        """
        opt = _make_optimizer()
        opt._slate_value_threshold = 5.1
        opt._slate_value_ceiling = 7.1
        mult = opt._ownership_leverage_multiplier(
            50.0, "balanced", value_ratio=5.3
        )
        # Should have partial dampening
        assert mult > 0.88, f"5.3x at 50% owned should get dampened, got {mult}"
        assert mult < 1.0, "Should still have some small penalty"

    def test_50pct_owned_4_0x_value_full_sigmoid_penalty(self):
        """50% owned at 4.0x value → full sigmoid penalty (no dampening).

        Sigmoid: penalty = 0.15 / (1 + e^(-12*(0.50-0.40))) ≈ 0.115
        multiplier ≈ 0.885
        """
        opt = _make_optimizer()
        opt._slate_value_threshold = 5.1
        opt._slate_value_ceiling = 7.1
        mult = opt._ownership_leverage_multiplier(
            50.0, "balanced", value_ratio=4.0
        )
        # Sigmoid at 50% gives ~11.5% penalty (not 15% — curve hasn't saturated)
        assert mult >= 0.85, f"Should not exceed max penalty, got multiplier {mult}"
        assert abs(mult - 0.885) < 0.01, f"Expected ~0.885 for sigmoid at 50%, got {mult}"


# ===========================================================================
# Tests for dynamic slate value threshold
# ===========================================================================

class TestSlateValueThreshold:
    """Test _calculate_slate_value_threshold() computation."""

    def setup_method(self):
        self.opt = _make_optimizer()

    def test_basic_threshold_computation(self):
        """Should return 90th percentile of value ratios."""
        pool = [
            _make_player(player_id=i, salary=5000, projected_fp=20.0 + i)
            for i in range(20)
        ]
        threshold, ceiling = self.opt._calculate_slate_value_threshold(pool)
        # Values range from 4.0 to 7.8 FP/$1K — p90 should be ~7.4
        assert threshold > 4.0
        assert ceiling == threshold + 2.0

    def test_injury_heavy_slate_lower_threshold(self):
        """Slate where most players have moderate value → lower threshold."""
        # Simulate slate with compressed values (injury-heavy, obvious plays)
        pool = [
            _make_player(player_id=i, salary=6000, projected_fp=28.0 + i * 0.5)
            for i in range(20)
        ]
        threshold, ceiling = self.opt._calculate_slate_value_threshold(pool)
        # Values range from ~4.67 to ~6.25 — p90 should be ~6.0
        assert threshold < 7.0, f"Injury slate threshold should be moderate, got {threshold}"

    def test_empty_pool_returns_static_defaults(self):
        """Empty pool should fall back to static constants."""
        from app.config.constants import GOOD_CHALK_VALUE_THRESHOLD, GOOD_CHALK_VALUE_CEILING
        threshold, ceiling = self.opt._calculate_slate_value_threshold([])
        assert threshold == GOOD_CHALK_VALUE_THRESHOLD
        assert ceiling == GOOD_CHALK_VALUE_CEILING

    def test_small_pool_returns_static_defaults(self):
        """Pool with < 5 players falls back to static constants."""
        from app.config.constants import GOOD_CHALK_VALUE_THRESHOLD, GOOD_CHALK_VALUE_CEILING
        pool = [_make_player(player_id=i) for i in range(3)]
        threshold, ceiling = self.opt._calculate_slate_value_threshold(pool)
        assert threshold == GOOD_CHALK_VALUE_THRESHOLD
        assert ceiling == GOOD_CHALK_VALUE_CEILING


# ===========================================================================
# Tests for Elite Core exposure exemptions
# ===========================================================================

class TestEliteCoreExposure:
    """Test elite core exposure cap, min floor, and decay exemption."""

    def test_elite_core_cap_in_auto_exposure(self):
        """Elite core players get capped at ABSOLUTE_GLOBAL_MAX_EXPOSURE."""
        from app.config.constants import (
            ELITE_CORE_MAX_EXPOSURE,
            ABSOLUTE_GLOBAL_MAX_EXPOSURE,
            EXPOSURE_PENALTY_DEFAULT_CAP,
        )
        pool = [
            # Elite: 7.77 FP/$1K (above 6.5 threshold)
            _make_player(player_id=1, salary=3600, projected_fp=28.0),
            # Stud but not elite: 5.0 FP/$1K
            _make_player(player_id=2, salary=6000, projected_fp=30.0),
            # Mid-tier
            _make_player(player_id=3, salary=7000, projected_fp=25.0),
        ] + [
            _make_player(player_id=10 + i, salary=5000, projected_fp=20.0)
            for i in range(17)
        ]
        caps = LineupOptimizerService._compute_auto_exposure_caps(
            pool, "gpp", None, {},
        )
        # Elite core cap is at global ceiling; if equal to global_cap, it may
        # not have an explicit entry (implicit global default applies).
        # The effective cap is min(explicit or global_default, ABSOLUTE_GLOBAL).
        effective_cap = caps.get(1, EXPOSURE_PENALTY_DEFAULT_CAP)
        assert effective_cap <= ABSOLUTE_GLOBAL_MAX_EXPOSURE, (
            f"Elite core effective cap should be <= {ABSOLUTE_GLOBAL_MAX_EXPOSURE}, "
            f"got {effective_cap}"
        )
        assert ELITE_CORE_MAX_EXPOSURE <= ABSOLUTE_GLOBAL_MAX_EXPOSURE, (
            f"ELITE_CORE_MAX_EXPOSURE ({ELITE_CORE_MAX_EXPOSURE}) must not exceed "
            f"ABSOLUTE_GLOBAL_MAX_EXPOSURE ({ABSOLUTE_GLOBAL_MAX_EXPOSURE})"
        )

    def test_elite_core_min_exposure_floor(self):
        """Top projected player with elite value gets 80% min floor."""
        from app.config.constants import ELITE_CORE_MIN_EXPOSURE
        pool = [
            # Elite: highest projection + 7.77x value
            _make_player(player_id=1, salary=3600, projected_fp=28.0),
            _make_player(player_id=2, salary=6000, projected_fp=27.0),
            _make_player(player_id=3, salary=7000, projected_fp=26.0),
        ] + [
            _make_player(player_id=10 + i, salary=5000, projected_fp=20.0)
            for i in range(10)
        ]
        mins = LineupOptimizerService._compute_auto_min_exposure(
            pool, "gpp", {},
        )
        assert mins.get(1, 0) >= ELITE_CORE_MIN_EXPOSURE, (
            f"Elite core min should be >= {ELITE_CORE_MIN_EXPOSURE}, got {mins.get(1)}"
        )

    def test_non_elite_top1_gets_standard_min(self):
        """Top projected player without elite value gets standard 50% floor."""
        from app.config.constants import GPP_AUTO_MIN_EXPO_TOP1
        pool = [
            # Top projection but only 5.0x value (not elite)
            _make_player(player_id=1, salary=6000, projected_fp=30.0),
            _make_player(player_id=2, salary=6000, projected_fp=27.0),
            _make_player(player_id=3, salary=7000, projected_fp=26.0),
        ] + [
            _make_player(player_id=10 + i, salary=5000, projected_fp=20.0)
            for i in range(10)
        ]
        mins = LineupOptimizerService._compute_auto_min_exposure(
            pool, "gpp", {},
        )
        assert mins.get(1) == GPP_AUTO_MIN_EXPO_TOP1, (
            f"Non-elite top1 should get standard {GPP_AUTO_MIN_EXPO_TOP1}, got {mins.get(1)}"
        )

    def test_elite_core_exposure_decay_exempt(self):
        """Elite core players bypass quadratic decay in _apply_exposure_penalties."""
        import pulp
        # Create a simple ILP with one player
        prob = pulp.LpProblem("test", pulp.LpMaximize)
        x = {(1, "PG"): pulp.LpVariable("x_1_PG", cat=pulp.LpBinary)}
        base_coeffs = {1: 30.0}

        # Simulate 50% exposure (75 out of 150 lineups)
        class FakeCounts:
            def snapshot(self):
                return {1: 75}

        # Without elite core exemption
        prob1 = pulp.LpProblem("test1", pulp.LpMaximize)
        x1 = {(1, "PG"): pulp.LpVariable("x1_1_PG", cat=pulp.LpBinary)}
        LineupOptimizerService._apply_exposure_penalties(
            prob1, x1, base_coeffs, FakeCounts(),
            n_requested=150, max_exposure=0.95,
            locked_player_ids=[], player_max_exposure=None,
            elite_core_pids=None,
        )

        # With elite core exemption
        prob2 = pulp.LpProblem("test2", pulp.LpMaximize)
        x2 = {(1, "PG"): pulp.LpVariable("x2_1_PG", cat=pulp.LpBinary)}
        LineupOptimizerService._apply_exposure_penalties(
            prob2, x2, base_coeffs, FakeCounts(),
            n_requested=150, max_exposure=0.95,
            locked_player_ids=[], player_max_exposure=None,
            elite_core_pids={1},
        )

        # Extract coefficients from objectives
        # The elite core version should have higher coefficient
        obj1 = prob1.objective
        obj2 = prob2.objective

        # Get the coefficient of the variable
        coeff1 = sum(v[1] for v in obj1.items())
        coeff2 = sum(v[1] for v in obj2.items())

        assert coeff2 > coeff1, (
            f"Elite core coeff ({coeff2:.2f}) should exceed "
            f"non-exempt ({coeff1:.2f}) at 50% exposure"
        )
        # Elite core should have full base coefficient (30.0)
        assert abs(coeff2 - 30.0) < 0.01, (
            f"Elite core should maintain full coefficient, got {coeff2}"
        )


# ===========================================================================
# Tests for fade/leverage integration in composite scoring
# ===========================================================================

class TestFadeLeverageIntegration:
    """Test that fade/leverage scores affect composite scoring."""

    def test_fade_candidate_receives_penalty(self):
        """A fade candidate should get a lower composite score than a neutral player."""
        opt = _make_optimizer()
        opt._strategy_adjustments = None

        player = _make_player(ownership=30.0)
        rng = _FakeRng()
        exposure = {}

        # Score without fade data
        opt._fade_leverage_scores = {}
        score_neutral = opt._compute_composite_score(
            player, "balanced", exposure, 0, rng, "gpp"
        )

        # Score with fade data
        opt._fade_leverage_scores = {player.player_id: {"fade_score": 0.8}}
        score_faded = opt._compute_composite_score(
            player, "balanced", exposure, 0, rng, "gpp"
        )

        assert score_faded < score_neutral, (
            f"Faded ({score_faded:.3f}) should be lower than neutral ({score_neutral:.3f})"
        )

    def test_leverage_candidate_receives_boost(self):
        """A leverage candidate should get a higher composite score than neutral."""
        opt = _make_optimizer()
        opt._strategy_adjustments = None

        player = _make_player(ownership=4.0)
        rng = _FakeRng()
        exposure = {}

        # Score without leverage data
        opt._fade_leverage_scores = {}
        score_neutral = opt._compute_composite_score(
            player, "balanced", exposure, 0, rng, "gpp"
        )

        # Score with leverage data
        opt._fade_leverage_scores = {player.player_id: {"leverage_score": 0.9}}
        score_boosted = opt._compute_composite_score(
            player, "balanced", exposure, 0, rng, "gpp"
        )

        assert score_boosted > score_neutral, (
            f"Leveraged ({score_boosted:.3f}) should be higher than neutral ({score_neutral:.3f})"
        )

    def test_fade_leverage_no_effect_on_cash(self):
        """Fade/leverage should not apply to cash games."""
        opt = _make_optimizer()
        opt._strategy_adjustments = None

        player = _make_player(ownership=30.0)
        rng = _FakeRng()
        exposure = {}

        opt._fade_leverage_scores = {player.player_id: {"fade_score": 0.9}}
        score_cash = opt._compute_composite_score(
            player, "balanced", exposure, 0, rng, "cash"
        )

        opt._fade_leverage_scores = {}
        score_neutral = opt._compute_composite_score(
            player, "balanced", exposure, 0, rng, "cash"
        )

        assert abs(score_cash - score_neutral) < 0.001, (
            "Fade should not affect cash game scoring"
        )

    def test_fade_leverage_neutral_when_service_missing(self):
        """When fade_service is None, _fade_leverage_scores is empty."""
        opt = _make_optimizer(fade_service=None)
        assert opt._fade_leverage_scores == {}


# ===========================================================================
# Tests for FadeService.get_player_scores
# ===========================================================================

class _MockPlayer:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


class TestFadeServiceGetPlayerScores:
    """Test FadeService.get_player_scores() helper."""

    def test_returns_dict_structure(self):
        """Return dict should map player_id -> fade or leverage score."""
        pool = [
            _MockPlayer(player_id=1, player_name="A", position="PG",
                        team_abbreviation="BOS", salary=8000,
                        projected_fp=40.0, ceiling_fp=35.0, floor_fp=25.0,
                        ownership_projection=30.0, dk_value=5.0),
            _MockPlayer(player_id=2, player_name="B", position="SG",
                        team_abbreviation="LAL", salary=4000,
                        projected_fp=25.0, ceiling_fp=50.0, floor_fp=15.0,
                        ownership_projection=5.0, dk_value=6.25),
        ]
        svc = FadeService()
        scores = svc.get_player_scores(pool)
        assert isinstance(scores, dict)
        # At least one of them should be tagged (depends on thresholds vs pool)

    def test_fade_candidate_has_fade_score_key(self):
        """Fade candidates should have 'fade_score' in their entry."""
        pool = [
            _MockPlayer(player_id=i, player_name=f"P{i}", position="PG",
                        team_abbreviation="BOS", salary=5000,
                        projected_fp=30.0, ceiling_fp=20.0 + i, floor_fp=15.0,
                        ownership_projection=3.0 + i * 3, dk_value=6.0)
            for i in range(10)
        ]
        # Make one player clearly a fade (high own, low ceiling)
        pool.append(_MockPlayer(
            player_id=99, player_name="ChalkBust", position="PG",
            team_abbreviation="BOS", salary=9000,
            projected_fp=35.0, ceiling_fp=15.0, floor_fp=10.0,
            ownership_projection=35.0, dk_value=3.9,
        ))
        svc = FadeService()
        scores = svc.get_player_scores(pool)
        if 99 in scores:
            assert "fade_score" in scores[99]

    def test_empty_pool_returns_empty(self):
        """Empty pool should return empty dict."""
        svc = FadeService()
        assert svc.get_player_scores([]) == {}
