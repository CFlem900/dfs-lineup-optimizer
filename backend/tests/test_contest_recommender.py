"""Tests for ContestRecommenderService scoring logic.

Covers:
  - Overlay score: guaranteed vs non-guaranteed, scaling
  - Structure score: GPP top-heavy vs cash flat payout
  - Field strength: fill rate, multi-entry, entry fee
  - Calibration fit: no calibrations, some, many
  - Entry count recommendations: thresholds and caps
  - score_contest(): full scoring pipeline via mock ContestDetail
  - _score_from_lobby_data(): fallback scoring from lobby dicts
  - recommend_contests(): end-to-end with mocked lobby fetch
"""

import pytest
from unittest.mock import MagicMock, patch

from app.models.ai import ContestScore, ContestRecommendation
from app.services.contest_recommender_service import (
    ContestRecommenderService,
    _W_OVERLAY,
    _W_STRUCTURE,
    _W_FIELD_STRENGTH,
    _W_CALIBRATION,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class MockContestDetail:
    """Mimics dk_contest_detail_service.ContestDetail."""

    def __init__(
        self,
        contest_id: str = "12345",
        contest_name: str = "Test GPP",
        entry_fee: float = 20.0,
        total_prizes: float = 50000.0,
        max_entries: int = 1000,
        current_entries: int = 500,
        max_entries_per_user: int = 150,
        contest_type: str = "Tournament",
        is_guaranteed: bool = True,
        overlay_risk: float = 10.0,
        top_heavy_pct: float = 40.0,
        roi_breakeven_percentile: float = 20.0,
    ):
        self.contest_id = contest_id
        self.contest_name = contest_name
        self.entry_fee = entry_fee
        self.total_prizes = total_prizes
        self.max_entries = max_entries
        self.current_entries = current_entries
        self.max_entries_per_user = max_entries_per_user
        self.contest_type = contest_type
        self.is_guaranteed = is_guaranteed
        self.overlay_risk = overlay_risk
        self.top_heavy_pct = top_heavy_pct
        self.roi_breakeven_percentile = roi_breakeven_percentile

    def inferred_contest_type(self) -> str:
        name_lower = self.contest_name.lower()
        if self.max_entries_per_user == 1:
            return "single_entry"
        if any(kw in name_lower for kw in ["double up", "50/50", "h2h", "head-to-head"]):
            return "cash"
        if self.top_heavy_pct > 30:
            return "gpp"
        if self.top_heavy_pct < 10:
            return "cash"
        return "gpp"


def _make_service(calibrations=None) -> ContestRecommenderService:
    """Create service with mocked dependencies."""
    dk_contests = MagicMock()
    dk_contests.get_contest_detail.return_value = None

    cal_service = MagicMock()
    if calibrations is None:
        cal_service.get_all.return_value = {}
    else:
        cal_service.get_all.return_value = calibrations

    return ContestRecommenderService(
        dk_contest_detail_service=dk_contests,
        calibration_service=cal_service,
    )


# ---------------------------------------------------------------------------
# Overlay Score
# ---------------------------------------------------------------------------

class TestOverlayScore:
    def test_zero_overlay_scores_zero(self):
        score = ContestRecommenderService._calc_overlay_score(0.0, True)
        assert score == 0.0

    def test_fifty_pct_overlay_scores_max(self):
        score = ContestRecommenderService._calc_overlay_score(50.0, True)
        assert score == 100.0

    def test_overlay_scales_linearly(self):
        score = ContestRecommenderService._calc_overlay_score(25.0, True)
        assert score == 50.0

    def test_non_guaranteed_capped_at_sixty(self):
        score = ContestRecommenderService._calc_overlay_score(50.0, False)
        assert score == 60.0

    def test_non_guaranteed_below_cap_unchanged(self):
        # 10% overlay * 2.0 = 20, well below cap of 60
        score = ContestRecommenderService._calc_overlay_score(10.0, False)
        assert score == 20.0


# ---------------------------------------------------------------------------
# Structure Score
# ---------------------------------------------------------------------------

class TestStructureScore:
    def test_gpp_high_top_heavy_scores_high(self):
        score = ContestRecommenderService._calc_structure_score(60.0, 25.0, True)
        # 60 * 1.5 = 90, breakeven 25 < 30 → +20 → capped at 100
        assert score == 100.0

    def test_gpp_low_top_heavy_scores_low(self):
        score = ContestRecommenderService._calc_structure_score(10.0, 60.0, True)
        # 10 * 1.5 = 15, no breakeven bonus
        assert score == 15.0

    def test_gpp_breakeven_below_30_gets_bonus(self):
        score = ContestRecommenderService._calc_structure_score(30.0, 25.0, True)
        # 30 * 1.5 = 45 + 20 = 65
        assert score == 65.0

    def test_gpp_breakeven_between_30_50_gets_smaller_bonus(self):
        score = ContestRecommenderService._calc_structure_score(30.0, 40.0, True)
        # 30 * 1.5 = 45 + 10 = 55
        assert score == 55.0

    def test_cash_low_top_heavy_scores_high(self):
        score = ContestRecommenderService._calc_structure_score(5.0, 50.0, False)
        # 100 - 5*2 = 90
        assert score == 90.0

    def test_cash_high_top_heavy_scores_low(self):
        score = ContestRecommenderService._calc_structure_score(60.0, 50.0, False)
        # 100 - 60*2 = -20 → clamped to 0
        assert score == 0.0

    def test_cash_bad_breakeven_penalty(self):
        score = ContestRecommenderService._calc_structure_score(5.0, 65.0, False)
        # 100 - 10 = 90 - 15 = 75
        assert score == 75.0


# ---------------------------------------------------------------------------
# Field Strength Score
# ---------------------------------------------------------------------------

class TestFieldStrengthScore:
    def test_empty_field_high_score(self):
        score = ContestRecommenderService._calc_field_strength_score(0.0, 1, 5.0)
        # base=100, max_per_user=1 (no discount), entry_fee=5 → +15 → 115 capped 100
        assert score == 100.0

    def test_full_field_low_score(self):
        score = ContestRecommenderService._calc_field_strength_score(1.0, 1, 20.0)
        # base=0, no adjustments
        assert score == 0.0

    def test_half_filled(self):
        score = ContestRecommenderService._calc_field_strength_score(0.5, 1, 20.0)
        # base=50, no discounts, 20 fee no adj
        assert score == 50.0

    def test_multi_entry_sharps_discount(self):
        # max_entries_per_user > 20 → 0.80 multiplier
        score_multi = ContestRecommenderService._calc_field_strength_score(0.5, 150, 20.0)
        score_single = ContestRecommenderService._calc_field_strength_score(0.5, 1, 20.0)
        assert score_multi < score_single

    def test_low_entry_fee_bonus(self):
        score_cheap = ContestRecommenderService._calc_field_strength_score(0.5, 1, 3.0)
        score_normal = ContestRecommenderService._calc_field_strength_score(0.5, 1, 20.0)
        assert score_cheap > score_normal

    def test_high_entry_fee_penalty(self):
        score_expensive = ContestRecommenderService._calc_field_strength_score(0.5, 1, 50.0)
        score_normal = ContestRecommenderService._calc_field_strength_score(0.5, 1, 20.0)
        assert score_expensive < score_normal


# ---------------------------------------------------------------------------
# Calibration Fit Score
# ---------------------------------------------------------------------------

class TestCalibrationFitScore:
    def test_no_calibration_service(self):
        svc = ContestRecommenderService(MagicMock(), calibration_service=None)
        score = svc._calc_calibration_fit_score()
        assert score == 40.0

    def test_empty_calibrations(self):
        svc = _make_service(calibrations={})
        score = svc._calc_calibration_fit_score()
        assert score == 30.0

    def test_some_calibrations(self):
        cals = {"position_PG": 1.05, "position_SG": 1.10, "b2b_penalty": 0.92}
        svc = _make_service(calibrations=cals)
        score = svc._calc_calibration_fit_score()
        # 3 active cals (all differ from 1.0) → 30 + 3*10 = 60
        assert score == 60.0

    def test_many_calibrations_capped(self):
        cals = {f"cal_{i}": 1.05 + i * 0.01 for i in range(10)}
        svc = _make_service(calibrations=cals)
        score = svc._calc_calibration_fit_score()
        # 10 * 10 + 30 = 130 → capped at 100
        assert score == 100.0

    def test_calibrations_near_1_ignored(self):
        cals = {"position_PG": 1.005, "position_SG": 1.0}
        svc = _make_service(calibrations=cals)
        score = svc._calc_calibration_fit_score()
        # Both within 0.01 of 1.0 → 0 active → 30 + 0 = 30
        assert score == 30.0


# ---------------------------------------------------------------------------
# Entry Recommendations
# ---------------------------------------------------------------------------

class TestEntryRecommendations:
    def test_low_score_skips(self):
        entries = ContestRecommenderService._recommend_entries(40, "gpp", 150)
        assert entries == 0

    def test_cash_always_one(self):
        entries = ContestRecommenderService._recommend_entries(90, "cash", 150)
        assert entries == 1

    def test_score_85_plus_up_to_20(self):
        entries = ContestRecommenderService._recommend_entries(90, "gpp", 150)
        assert entries == 20

    def test_score_75_to_84_up_to_10(self):
        entries = ContestRecommenderService._recommend_entries(80, "gpp", 150)
        assert entries == 10

    def test_score_65_to_74_up_to_3(self):
        entries = ContestRecommenderService._recommend_entries(70, "gpp", 150)
        assert entries == 3

    def test_score_50_to_64_returns_one(self):
        entries = ContestRecommenderService._recommend_entries(55, "gpp", 150)
        assert entries == 1

    def test_capped_by_max_entries_per_user(self):
        entries = ContestRecommenderService._recommend_entries(90, "gpp", 5)
        assert entries == 5

    def test_single_entry_gpp(self):
        entries = ContestRecommenderService._recommend_entries(90, "single_entry", 1)
        assert entries == 1


# ---------------------------------------------------------------------------
# score_contest() — Full pipeline via ContestDetail
# ---------------------------------------------------------------------------

class TestScoreContest:
    def test_gpp_contest_returns_valid_score(self):
        svc = _make_service()
        detail = MockContestDetail(
            overlay_risk=20.0,
            top_heavy_pct=45.0,
            roi_breakeven_percentile=22.0,
            max_entries=5000,
            current_entries=3000,
            max_entries_per_user=150,
            entry_fee=20.0,
            is_guaranteed=True,
        )
        result = svc.score_contest(detail)

        assert isinstance(result, ContestScore)
        assert result.contest_id == "12345"
        assert 0 <= result.overall_score <= 100
        assert 0 <= result.confidence <= 1.0
        assert result.overlay_score == 40.0  # 20 * 2.0
        assert result.recommended_entries >= 0

    def test_cash_contest_no_stacking_low_leverage(self):
        svc = _make_service()
        detail = MockContestDetail(
            contest_name="$5 Double Up",
            top_heavy_pct=5.0,
            roi_breakeven_percentile=50.0,
            max_entries=200,
            current_entries=150,
            max_entries_per_user=10,
            entry_fee=5.0,
        )
        result = svc.score_contest(detail)

        assert result.contest_type == "cash"

    def test_high_overlay_guaranteed_high_score(self):
        svc = _make_service()
        detail = MockContestDetail(
            overlay_risk=60.0,
            top_heavy_pct=50.0,
            roi_breakeven_percentile=15.0,
            max_entries=10000,
            current_entries=2000,
            is_guaranteed=True,
            entry_fee=5.0,
            max_entries_per_user=150,
        )
        result = svc.score_contest(detail)

        assert result.overlay_score == 100.0
        assert result.overall_score > 70

    def test_nearly_full_reduces_confidence(self):
        svc = _make_service()
        detail = MockContestDetail(
            overlay_risk=2.0,
            max_entries=1000,
            current_entries=970,
        )
        result = svc.score_contest(detail)

        # fill_rate = 0.97 > 0.95 → confidence *= 0.85
        base_conf = result.overall_score / 100.0
        expected_conf = base_conf * 0.85
        # Calibration < 30 → an additional 0.70 multiplier
        if result.calibration_fit_score < 30:
            expected_conf *= 0.70
        assert abs(result.confidence - round(expected_conf, 3)) < 0.01

    def test_reasoning_populated(self):
        svc = _make_service()
        detail = MockContestDetail(
            overlay_risk=50.0,
            is_guaranteed=True,
            top_heavy_pct=50.0,
        )
        result = svc.score_contest(detail)

        assert len(result.reasoning) > 0
        assert any("overlay" in r.lower() for r in result.reasoning)

    def test_warnings_for_high_entry_fee(self):
        svc = _make_service()
        detail = MockContestDetail(entry_fee=150.0)
        result = svc.score_contest(detail)

        assert any("bankroll" in w.lower() for w in result.warnings)

    def test_warnings_for_non_guaranteed(self):
        svc = _make_service()
        detail = MockContestDetail(
            is_guaranteed=False,
            overlay_risk=30.0,
        )
        result = svc.score_contest(detail)

        assert any("non-guaranteed" in w.lower() for w in result.warnings)


# ---------------------------------------------------------------------------
# _score_from_lobby_data() — Fallback scoring from lobby dicts
# ---------------------------------------------------------------------------

class TestScoreFromLobbyData:
    def test_basic_gpp_lobby_data(self):
        svc = _make_service()
        raw = {
            "id": "999",
            "name": "$5 GPP Tournament",
            "entry_fee": 5.0,
            "max_entries": 2000,
            "current_entries": 1000,
            "total_prizes": 9000.0,
            "max_entries_per_user": 150,
            "is_guaranteed": True,
        }
        result = svc._score_from_lobby_data(raw)

        assert result is not None
        assert result.contest_id == "999"
        assert result.contest_type == "gpp"
        assert 0 <= result.overall_score <= 100

    def test_cash_contest_detected(self):
        svc = _make_service()
        raw = {
            "id": "100",
            "name": "$3 Double Up",
            "entry_fee": 3.0,
            "max_entries": 200,
            "current_entries": 100,
            "total_prizes": 360.0,
            "max_entries_per_user": 1,
            "is_guaranteed": True,
        }
        result = svc._score_from_lobby_data(raw)

        assert result.contest_type == "cash"

    def test_single_entry_detected(self):
        svc = _make_service()
        raw = {
            "id": "200",
            "name": "$10 Tournament",
            "entry_fee": 10.0,
            "max_entries": 5000,
            "current_entries": 3000,
            "total_prizes": 45000.0,
            "max_entries_per_user": 1,
            "is_guaranteed": True,
        }
        result = svc._score_from_lobby_data(raw)

        assert result.contest_type == "single_entry"

    def test_zero_max_entries_returns_none(self):
        svc = _make_service()
        raw = {"id": "300", "max_entries": 0}
        result = svc._score_from_lobby_data(raw)
        assert result is None

    def test_lobby_data_includes_reasoning(self):
        svc = _make_service()
        raw = {
            "id": "400",
            "name": "Test",
            "entry_fee": 5.0,
            "max_entries": 1000,
            "current_entries": 500,
            "total_prizes": 4500.0,
            "max_entries_per_user": 150,
            "is_guaranteed": True,
        }
        result = svc._score_from_lobby_data(raw)

        assert any("lobby data" in r.lower() for r in result.reasoning)


# ---------------------------------------------------------------------------
# recommend_contests() — End-to-end with mocked lobby
# ---------------------------------------------------------------------------

class TestRecommendContests:
    def test_empty_lobby_returns_empty(self):
        svc = _make_service()
        svc._fetch_lobby_contests = MagicMock(return_value=[])

        result = svc.recommend_contests(sport="nba")

        assert isinstance(result, ContestRecommendation)
        assert result.total_contests_scanned == 0
        assert len(result.contests) == 0

    def test_scored_contests_sorted_descending(self):
        svc = _make_service()
        svc._fetch_lobby_contests = MagicMock(return_value=[
            {
                "id": "1", "name": "Bad Contest",
                "entry_fee": 100.0, "max_entries": 100,
                "current_entries": 99, "total_prizes": 9000.0,
                "max_entries_per_user": 150, "is_guaranteed": False,
            },
            {
                "id": "2", "name": "Good Contest",
                "entry_fee": 5.0, "max_entries": 5000,
                "current_entries": 1000, "total_prizes": 22500.0,
                "max_entries_per_user": 150, "is_guaranteed": True,
            },
        ])

        result = svc.recommend_contests(sport="nba")

        assert len(result.contests) == 2
        assert result.contests[0].overall_score >= result.contests[1].overall_score

    def test_draft_group_filter(self):
        svc = _make_service()
        svc._fetch_lobby_contests = MagicMock(return_value=[
            {"id": "1", "name": "Slate A", "draft_group_id": 100,
             "entry_fee": 5.0, "max_entries": 1000, "current_entries": 500,
             "total_prizes": 4500.0, "max_entries_per_user": 150, "is_guaranteed": True},
            {"id": "2", "name": "Slate B", "draft_group_id": 200,
             "entry_fee": 5.0, "max_entries": 1000, "current_entries": 500,
             "total_prizes": 4500.0, "max_entries_per_user": 150, "is_guaranteed": True},
        ])

        result = svc.recommend_contests(sport="nba", draft_group_id=100)

        assert len(result.contests) == 1
        assert result.draft_group_id == 100

    def test_total_entries_recommended(self):
        svc = _make_service()
        svc._fetch_lobby_contests = MagicMock(return_value=[
            {
                "id": "1", "name": "$5 GPP",
                "entry_fee": 5.0, "max_entries": 5000,
                "current_entries": 1000, "total_prizes": 22500.0,
                "max_entries_per_user": 150, "is_guaranteed": True,
            },
        ])

        result = svc.recommend_contests(sport="nba")

        assert result.total_entries_recommended == sum(
            c.recommended_entries for c in result.contests
        )

    def test_generation_time_recorded(self):
        svc = _make_service()
        svc._fetch_lobby_contests = MagicMock(return_value=[])

        result = svc.recommend_contests(sport="nba")

        assert result.generation_time_ms >= 0


# ---------------------------------------------------------------------------
# score_contest_by_id() — delegates to DKContestDetailService
# ---------------------------------------------------------------------------

class TestScoreContestById:
    def test_returns_none_when_detail_missing(self):
        svc = _make_service()
        svc._dk_contests.get_contest_detail.return_value = None

        result = svc.score_contest_by_id("99999")
        assert result is None

    def test_returns_score_when_detail_found(self):
        svc = _make_service()
        detail = MockContestDetail(contest_id="55555")
        svc._dk_contests.get_contest_detail.return_value = detail

        result = svc.score_contest_by_id("55555")

        assert result is not None
        assert result.contest_id == "55555"
        assert isinstance(result, ContestScore)


# ---------------------------------------------------------------------------
# Lobby parsing
# ---------------------------------------------------------------------------

class TestParseLobbyContests:
    def test_parses_short_field_names(self):
        raw = [
            {
                "id": "1",
                "n": "Test Contest",
                "a": 5.0,
                "m": 1000,
                "ec": 500,
                "fpp": 4500.0,
                "nt": 150,
                "dg": 12345,
                "IsGuaranteed": True,
            }
        ]
        parsed = ContestRecommenderService._parse_lobby_contests(raw)

        assert len(parsed) == 1
        assert parsed[0]["id"] == "1"
        assert parsed[0]["name"] == "Test Contest"
        assert parsed[0]["entry_fee"] == 5.0
        assert parsed[0]["max_entries"] == 1000
        assert parsed[0]["current_entries"] == 500
        assert parsed[0]["total_prizes"] == 4500.0
        assert parsed[0]["max_entries_per_user"] == 150
        assert parsed[0]["draft_group_id"] == 12345

    def test_parses_verbose_field_names(self):
        raw = [
            {
                "ContestId": "2",
                "ContestName": "Verbose Contest",
                "EntryFee": 10.0,
                "MaxEntries": 2000,
                "Entries": 800,
                "TotalPrizes": 18000.0,
                "MaxEntriesPerUser": 20,
                "DraftGroupId": 99,
            }
        ]
        parsed = ContestRecommenderService._parse_lobby_contests(raw)

        assert len(parsed) == 1
        assert parsed[0]["id"] == "2"
        assert parsed[0]["name"] == "Verbose Contest"
        assert parsed[0]["entry_fee"] == 10.0

    def test_skips_entries_without_id(self):
        raw = [{"n": "No ID Contest", "a": 5.0}]
        parsed = ContestRecommenderService._parse_lobby_contests(raw)
        assert len(parsed) == 0

    def test_handles_empty_list(self):
        parsed = ContestRecommenderService._parse_lobby_contests([])
        assert parsed == []
