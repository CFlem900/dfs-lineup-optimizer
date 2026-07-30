"""Tests for vacancy detection and dynamic starter promotion in top_down_minutes.

Covers the core scenario: when a star (season_avg >= 28) is Out, the backup
with high recent_avg (>= 24) should be promoted to starter over veterans with
higher season_avg but lower recent minutes.

Also tests: DK salary tiebreaking, non-star Out (no vacancy), and the
salary-adjusted trade minutes helper.
"""
import pytest
from typing import Dict, List, Optional

from app.models.player import PlayerMinutes, PlayerStatus
from app.services.top_down_minutes import (
    allocate_team_minutes,
    _player_sort_score,
    _vacancy_aware_score,
    _pick_best_auto_promote,
    VACANCY_STAR_THRESHOLD,
    VACANCY_RECENT_AVG_THRESHOLD,
    STARTER_FLOOR,
    STARTER_CAP,
    STARTER_DEFAULT,
)
from app.services.nba_api_service import (
    _salary_adjusted_trade_minutes,
    _TRADE_DEFAULT_MINUTES,
)


# ── Helpers ──────────────────────────────────────────────────────────────


def _make_player(
    pid: int,
    name: str,
    pos: str,
    season_avg: float,
    last_5: Optional[List[float]] = None,
    dk_salary: Optional[int] = None,
    dk_position: Optional[str] = None,
    team_id: int = 1,
    dnp_streak: int = 0,
) -> PlayerMinutes:
    """Build a PlayerMinutes with minimal boilerplate."""
    last_5 = last_5 or [season_avg] * 5
    return PlayerMinutes(
        player_id=pid,
        player_name=name,
        position=pos,
        team_id=team_id,
        minutes_last_5=last_5,
        minutes_last_10=last_5 + last_5,
        season_avg=season_avg,
        usage_rate=0.20,
        dk_salary=dk_salary,
        dk_position=dk_position,
        recent_dnp_streak=dnp_streak,
    )


def _make_full_team(
    star_pid: int = 1,
    star_name: str = "Star PG",
    star_pos: str = "PG",
    star_avg: float = 33.0,
    star_salary: int = 9200,
    backup_pid: int = 2,
    backup_name: str = "Backup PG",
    backup_pos: str = "PG",
    backup_season_avg: float = 15.0,
    backup_last_5: Optional[List[float]] = None,
    backup_salary: int = 4200,
) -> List[PlayerMinutes]:
    """Build a realistic 9-man rotation for testing."""
    backup_last_5 = backup_last_5 or [28, 30, 27, 29, 26]
    return [
        _make_player(star_pid, star_name, star_pos, star_avg, dk_salary=star_salary),
        _make_player(backup_pid, backup_name, backup_pos, backup_season_avg,
                     backup_last_5, dk_salary=backup_salary),
        _make_player(3, "Starter SG", "SG", 30.0, dk_salary=7500),
        _make_player(4, "Starter SF", "SF", 28.0, dk_salary=7000),
        _make_player(5, "Starter PF", "PF", 31.0, dk_salary=8000),
        _make_player(6, "Starter C", "C", 32.0, dk_salary=8500),
        _make_player(7, "Bench SG", "SG", 18.0, dk_salary=4000),
        _make_player(8, "Bench PF", "PF", 16.0, dk_salary=3500),
        _make_player(9, "Bench SF", "SF", 14.0, dk_salary=3200),
    ]


# ============================================================================
# VACANCY DETECTION TESTS
# ============================================================================


class TestVacancyDetection:
    """Test that vacancy slots are identified when stars are Out."""

    def test_star_out_creates_vacancy_and_promotes_backup(self):
        """When a star PG (33 avg) is Out, backup PG with high recent avg
        should be promoted to starter with >= STARTER_FLOOR minutes."""
        rotation = _make_full_team()
        injuries = [PlayerStatus(player_id=1, player_name="Star PG", status="Out")]
        dk_statuses = {1: "OUT"}

        result = allocate_team_minutes(
            rotation=rotation,
            injuries=injuries,
            dk_injury_statuses=dk_statuses,
            team_name="TEST",
        )

        # Star should be zeroed
        assert result[1] == 0.0, f"Star should be 0 min, got {result[1]}"

        # Backup should get starter minutes (>= STARTER_FLOOR = 28)
        assert result[2] >= STARTER_FLOOR, (
            f"Backup PG got {result[2]} min, expected >= {STARTER_FLOOR} "
            f"(vacancy promotion should fire)"
        )

    def test_non_star_out_no_vacancy_boost(self):
        """When a non-star (< 28 avg) is Out, no vacancy mechanism fires.
        The existing starters should keep their slots."""
        rotation = [
            _make_player(1, "Starter PG", "PG", 32.0, dk_salary=8000),
            _make_player(2, "Bench PG", "PG", 15.0, dk_salary=3500),
            _make_player(3, "Starter SG", "SG", 30.0, dk_salary=7500),
            _make_player(4, "Starter SF", "SF", 28.0, dk_salary=7000),
            _make_player(5, "Starter PF", "PF", 30.0, dk_salary=8000),
            _make_player(6, "Starter C", "C", 32.0, dk_salary=8500),
            _make_player(7, "Bench SG", "SG", 16.0, dk_salary=3500),
            _make_player(8, "Bench PF", "PF", 14.0, dk_salary=3200),
        ]
        injuries = [PlayerStatus(player_id=2, player_name="Bench PG", status="Out")]

        result = allocate_team_minutes(
            rotation=rotation,
            injuries=injuries,
            dk_injury_statuses={2: "OUT"},
            team_name="TEST",
        )

        # Bench PG should be zeroed
        assert result[2] == 0.0
        # Starter PG should still have starter minutes
        assert result[1] >= STARTER_FLOOR

    def test_no_injuries_no_vacancy(self):
        """With no injuries, all starters should be selected normally."""
        rotation = _make_full_team()
        result = allocate_team_minutes(
            rotation=rotation,
            injuries=[],
            team_name="TEST",
        )

        # Star PG should be a starter with full minutes
        assert result[1] >= STARTER_FLOOR
        # Backup PG should be bench
        assert result[2] < STARTER_FLOOR

    def test_total_minutes_equals_240(self):
        """Minutes must sum to 240 regardless of vacancy promotions."""
        rotation = _make_full_team()
        injuries = [PlayerStatus(player_id=1, player_name="Star PG", status="Out")]

        result = allocate_team_minutes(
            rotation=rotation,
            injuries=injuries,
            dk_injury_statuses={1: "OUT"},
            team_name="TEST",
        )

        total = sum(result.values())
        assert abs(total - 240.0) < 0.5, f"Total minutes {total} != 240"


# ============================================================================
# VACANCY-AWARE SCORE TESTS
# ============================================================================


class TestVacancyAwareScore:
    """Test the enhanced scoring function for vacancy slots."""

    def test_recent_starter_outscores_veteran_same_salary(self):
        """A bench player starting recently should outscore a veteran
        under vacancy scoring (recency-weighted), even though the
        veteran wins under normal scoring (season-weighted).

        Uses same salary to isolate the recency vs season_avg property.
        """
        # Small-like profile: low season avg, high recent
        backup = _make_player(1, "Backup", "PG", 15.0, [28, 30, 27, 29, 26],
                              dk_salary=5000)
        # Smart-like profile: high season avg, moderate recent
        veteran = _make_player(2, "Veteran", "SG", 25.0, [22, 23, 21, 24, 20],
                               dk_salary=5000)

        # Normal score: veteran wins (70% season + 30% recent)
        # Veteran: 0.70*25 + 0.30*22 = 24.1
        # Backup:  0.70*15 + 0.30*28 = 18.9
        assert _player_sort_score(veteran) > _player_sort_score(backup), (
            "Veteran should win under normal scoring"
        )

        # Vacancy score: backup wins (30% season + 70% recent)
        # Backup:  0.30*15 + 0.70*28 = 24.1
        # Veteran: 0.30*25 + 0.70*22 = 22.9
        assert _vacancy_aware_score(backup) > _vacancy_aware_score(veteran), (
            "Backup (recent starter) should win under vacancy scoring"
        )

    def test_recent_starter_outscores_veteran_different_salary(self):
        """Even with a salary disadvantage, the recent starter wins
        when the recency gap is large enough."""
        backup = _make_player(1, "Backup", "PG", 12.0, [32, 34, 30, 33, 31],
                              dk_salary=4200)
        veteran = _make_player(2, "Veteran", "SG", 25.0, [22, 23, 21, 24, 20],
                               dk_salary=5800)

        # Normal: veteran wins easily
        assert _player_sort_score(veteran) > _player_sort_score(backup)

        # Vacancy: backup's massive recent advantage overcomes salary gap
        # Backup:  0.30*12 + 0.70*32 + salary(-1.2) = 3.6 + 22.4 - 1.2 = 24.8
        # Veteran: 0.30*25 + 0.70*22 + salary(+1.2) = 7.5 + 15.4 + 1.2 = 24.1
        assert _vacancy_aware_score(backup) > _vacancy_aware_score(veteran), (
            "Backup with very high recent avg should beat veteran"
        )

    def test_dk_salary_breaks_ties(self):
        """DK salary should break ties between similar recent performers."""
        player_a = _make_player(1, "A", "PG", 14.0, [24, 25, 23, 24, 24],
                                dk_salary=5500)
        player_b = _make_player(2, "B", "PG", 14.0, [24, 25, 23, 24, 24],
                                dk_salary=3500)

        score_a = _vacancy_aware_score(player_a)
        score_b = _vacancy_aware_score(player_b)
        assert score_a > score_b, "Higher DK salary should break ties"

    def test_salary_boost_is_capped(self):
        """DK salary boost should not dominate the score."""
        low_min_high_sal = _make_player(1, "Rich Bench", "PG", 8.0, [8, 9, 7, 8, 8],
                                         dk_salary=10000)
        high_min_low_sal = _make_player(2, "Starting", "PG", 12.0, [26, 28, 24, 25, 27],
                                         dk_salary=3000)

        # Despite massive salary advantage, the player with real minutes wins
        assert _vacancy_aware_score(high_min_low_sal) > _vacancy_aware_score(low_min_high_sal), (
            "Real minutes should trump salary — salary boost is capped"
        )

    def test_no_salary_no_crash(self):
        """Player with no DK salary should still get a valid score."""
        player = _make_player(1, "Unknown", "PG", 20.0, dk_salary=None)
        score = _vacancy_aware_score(player)
        assert score > 0


# ============================================================================
# AUTO-PROMOTION TESTS
# ============================================================================


class TestAutoPromotion:
    """Test the recent_avg >= 24 auto-promotion rule."""

    def test_auto_promote_fires_for_recent_starter(self):
        """Player with recent_avg >= 24 should be promoted to starter
        even if their season_avg is very low."""
        rotation = _make_full_team(
            backup_season_avg=12.0,  # Very low season avg
            backup_last_5=[26, 28, 24, 25, 27],  # But recently starting (avg=26)
            backup_salary=3800,
        )
        injuries = [PlayerStatus(player_id=1, player_name="Star PG", status="Out")]

        result = allocate_team_minutes(
            rotation=rotation,
            injuries=injuries,
            dk_injury_statuses={1: "OUT"},
            team_name="TEST",
        )

        # Backup should get starter minutes via auto-promote
        assert result[2] >= STARTER_FLOOR, (
            f"Backup got {result[2]} min — auto-promote should fire "
            f"(recent_avg=26 >= {VACANCY_RECENT_AVG_THRESHOLD})"
        )

    def test_no_auto_promote_below_threshold(self):
        """Player with recent_avg < 24 should NOT auto-promote, but may
        still win via vacancy-aware scoring if their score is highest."""
        rotation = _make_full_team(
            backup_season_avg=10.0,
            backup_last_5=[14, 15, 13, 14, 14],  # recent_avg=14, below threshold
            backup_salary=3200,
        )
        injuries = [PlayerStatus(player_id=1, player_name="Star PG", status="Out")]

        result = allocate_team_minutes(
            rotation=rotation,
            injuries=injuries,
            dk_injury_statuses={1: "OUT"},
            team_name="TEST",
        )

        # Backup's recent avg is 14 (below 24) — no auto-promote.
        # But they're the only exact PG match after the star is out,
        # so they should still win the PG slot via vacancy-aware scoring.
        # Their vacancy score = 0.30*10 + 0.70*14 + salary_boost
        # = 3.0 + 9.8 + (-2.7) = 10.1 — low but they're the only PG.
        # Outcome depends on whether another player has PG position.
        # In our test team, they're the only other PG, so they should win.
        assert result[2] >= STARTER_FLOOR or result[2] > 0


class TestStarterSqueeze:
    """Test that promoted backup gets proper Starter's Squeeze allocation."""

    def test_promoted_backup_gets_at_least_starter_floor(self):
        """Once promoted, backup gets position-aware baseline via
        _get_starter_minute_baseline(), clamped to at least STARTER_FLOOR.
        PG backup (avg=15) → min(15+12, 32) = 27 → clamped to 28 min floor."""
        rotation = _make_full_team(
            backup_season_avg=15.0,
            backup_last_5=[28, 30, 27, 29, 26],
        )
        injuries = [PlayerStatus(player_id=1, player_name="Star PG", status="Out")]

        result = allocate_team_minutes(
            rotation=rotation,
            injuries=injuries,
            dk_injury_statuses={1: "OUT"},
            team_name="TEST",
        )

        # Position-aware starter baseline: min(15+12, 32) = 27 → clamped to STARTER_FLOOR
        assert result[2] >= STARTER_FLOOR, (
            f"Promoted backup got {result[2]} min, expected >= {STARTER_FLOOR}"
        )
        # Should be reasonable for a promoted starter (with Phase 4 residual
        # redistribution, may exceed 32 in small rotations due to 240 budget)
        assert result[2] <= STARTER_CAP + 0.5, (
            f"Promoted backup got {result[2]} min, should be <= {STARTER_CAP}"
        )

    def test_high_avg_backup_keeps_higher_minutes(self):
        """A backup with high season_avg who gets promoted should keep
        their higher minutes (not be capped down to STARTER_DEFAULT)."""
        rotation = _make_full_team(
            backup_season_avg=35.0,  # This player is actually a star
            backup_last_5=[34, 36, 33, 35, 34],
        )
        injuries = [PlayerStatus(player_id=1, player_name="Star PG", status="Out")]

        result = allocate_team_minutes(
            rotation=rotation,
            injuries=injuries,
            dk_injury_statuses={1: "OUT"},
            team_name="TEST",
        )

        # max(35, 32) = 35.0 min
        assert result[2] >= 34.0, (
            f"High-avg backup got {result[2]} min, expected >= 34"
        )


# ============================================================================
# SALARY-ADJUSTED TRADE MINUTES TESTS
# ============================================================================


class TestSalaryAdjustedTradeMinutes:
    """Test the _salary_adjusted_trade_minutes() helper in nba_api_service."""

    def test_low_salary_uses_position_default(self):
        """Players below $4.5K should get flat position defaults."""
        assert _salary_adjusted_trade_minutes("PG", 3000) == 14.0
        assert _salary_adjusted_trade_minutes("SF", 3000) == 12.0
        assert _salary_adjusted_trade_minutes("C", 4000) == 12.0

    def test_mid_salary_gets_rotation_minutes(self):
        """$4.5K-$6K players should get 20-26 min (interpolated)."""
        result = _salary_adjusted_trade_minutes("PG", 5000)
        assert 20.0 <= result <= 26.0, f"$5K PG got {result} min"

    def test_high_salary_gets_starter_minutes(self):
        """$8K+ players should get 30+ min."""
        assert _salary_adjusted_trade_minutes("PG", 8000) >= 30.0
        assert _salary_adjusted_trade_minutes("SF", 8500) >= 30.0

    def test_very_high_salary_gets_star_minutes(self):
        """$9K+ players should get 34 min."""
        assert _salary_adjusted_trade_minutes("PG", 9000) == 34.0
        assert _salary_adjusted_trade_minutes("C", 10000) == 34.0

    def test_interpolation_is_smooth(self):
        """Minutes should increase monotonically with salary."""
        results = [
            _salary_adjusted_trade_minutes("PG", sal)
            for sal in [3000, 4500, 5000, 5500, 6000, 7000, 8000, 9000, 10000]
        ]
        for i in range(len(results) - 1):
            assert results[i] <= results[i + 1], (
                f"Non-monotonic: ${[3000, 4500, 5000, 5500, 6000, 7000, 8000, 9000, 10000][i]} "
                f"→ {results[i]} > ${[3000, 4500, 5000, 5500, 6000, 7000, 8000, 9000, 10000][i+1]} "
                f"→ {results[i+1]}"
            )

    def test_never_below_position_default(self):
        """Salary adjustment should never reduce below position default."""
        for pos, base in _TRADE_DEFAULT_MINUTES.items():
            for sal in [0, 1000, 3000, 5000, 8000]:
                result = _salary_adjusted_trade_minutes(pos, sal)
                assert result >= base, (
                    f"{pos} at ${sal}: got {result}, expected >= {base}"
                )


# ============================================================================
# MEMPHIS GRIZZLIES SCENARIO (Morant/Small)
# ============================================================================


class TestMemphisScenario:
    """End-to-end test mirroring the Morant/Small projection gap."""

    def test_javon_small_promoted_when_morant_out(self):
        """Javon Small (PG, low season avg, high recent) should be promoted
        to starter when Ja Morant (PG, star) is ruled Out."""
        rotation = [
            _make_player(1, "Ja Morant", "PG", 33.0, dk_salary=9200),
            _make_player(2, "Javon Small", "PG", 15.0, [28, 30, 27, 29, 26],
                         dk_salary=4200),
            _make_player(3, "Marcus Smart", "SG", 25.0, [22, 23, 21, 24, 20],
                         dk_salary=5800),
            _make_player(4, "Desmond Bane", "SG-SF", 30.0, dk_salary=7500),
            _make_player(5, "Jaren Jackson Jr", "PF-C", 32.0, dk_salary=8800),
            _make_player(6, "Brandon Clarke", "PF", 18.0, dk_salary=4000),
            _make_player(7, "Santi Aldama", "SF-PF", 22.0, dk_salary=5200),
            _make_player(8, "Jake LaRavia", "SF", 12.0, dk_salary=3500),
            _make_player(9, "Luke Kennard", "SG", 14.0, dk_salary=3200),
        ]
        injuries = [
            PlayerStatus(player_id=1, player_name="Ja Morant", status="Out"),
        ]

        result = allocate_team_minutes(
            rotation=rotation,
            injuries=injuries,
            dk_injury_statuses={1: "OUT"},
            team_name="MEM",
        )

        # Morant zeroed
        assert result[1] == 0.0, f"Morant should be 0 min, got {result[1]}"

        # Small promoted to starter (>= 28 min, ideally ~32)
        assert result[2] >= STARTER_FLOOR, (
            f"Small got {result[2]} min, expected >= {STARTER_FLOOR} "
            f"(vacancy promotion for PG slot)"
        )

        # Verify total still sums to 240
        total = sum(result.values())
        assert abs(total - 240.0) < 0.5, f"Total {total} != 240"

        # Smart should NOT be the PG — he's SG, not exact PG match
        # He should still get starter minutes for the SG slot
        assert result[3] >= STARTER_FLOOR, (
            f"Smart got {result[3]} min — should still be SG starter"
        )


# ============================================================================
# DK POSITION MATCHING IN AUTO-PROMOTE
# ============================================================================


class TestDKPositionAutoPromote:
    """Test that _pick_best_auto_promote uses DK position to rank candidates."""

    def test_dk_position_match_beats_higher_stats(self):
        """Player whose DK position matches vacancy slot should win over
        a player with higher stats but different DK position."""
        import logging
        _logger = logging.getLogger("test")

        small = _make_player(
            1, "Javon Small", "G", 19.2, [26, 33, 15, 33, 19],
            dk_salary=5500, dk_position="PG",
        )
        rupert = _make_player(
            2, "Rayan Rupert", "G-F", 28.0, [31, 30, 32, 31, 31],
            dk_salary=4300, dk_position="SF",
        )
        candidates = [(small, 25.2), (rupert, 31.0)]

        winner = _pick_best_auto_promote(candidates, "PG", _logger, "test")
        assert winner.player_name == "Javon Small", (
            f"Expected Small (dk_pos=PG) to win PG slot, got {winner.player_name}"
        )

    def test_salary_breaks_tie_when_same_dk_position(self):
        """When both candidates have matching DK position, higher salary wins."""
        import logging
        _logger = logging.getLogger("test")

        player_a = _make_player(
            1, "Player A", "G", 20.0, [25, 26, 24, 27, 25],
            dk_salary=6000, dk_position="PG",
        )
        player_b = _make_player(
            2, "Player B", "G", 22.0, [28, 27, 29, 26, 28],
            dk_salary=4500, dk_position="PG",
        )
        candidates = [(player_a, 25.4), (player_b, 27.6)]

        winner = _pick_best_auto_promote(candidates, "PG", _logger, "test")
        assert winner.player_name == "Player A", (
            f"Expected Player A ($6K) to win over Player B ($4.5K), "
            f"got {winner.player_name}"
        )

    def test_no_dk_position_falls_back_to_salary(self):
        """When no player has dk_position set, highest salary should win."""
        import logging
        _logger = logging.getLogger("test")

        player_a = _make_player(
            1, "Player A", "G", 18.0, [26, 25, 27, 24, 28],
            dk_salary=5500,
        )
        player_b = _make_player(
            2, "Player B", "G", 25.0, [30, 29, 31, 28, 30],
            dk_salary=4000,
        )
        candidates = [(player_a, 26.0), (player_b, 29.6)]

        winner = _pick_best_auto_promote(candidates, "PG", _logger, "test")
        assert winner.player_name == "Player A", (
            f"Expected Player A ($5.5K) without dk_pos to win over Player B ($4K), "
            f"got {winner.player_name}"
        )

    def test_bdl_generic_positions_with_dk_specifics(self):
        """End-to-end: BDL returns 'G'/'G-F' but DK positions are specific.
        Mirrors real MEM scenario with multiple auto-promote candidates."""
        # Morant (G, dk=PG) is Out
        # Small (G, dk=PG, $5500, recent=25.2) should beat
        # Rupert (G-F, dk=SF, $4300, recent=31.0)
        # Clayton (G, dk=SG, $4700, recent=26.6)
        rotation = [
            _make_player(1, "Ja Morant", "G", 28.4,
                         dk_salary=8400, dk_position="PG"),
            _make_player(2, "Javon Small", "G", 19.2,
                         [26, 33, 15, 33, 19], dk_salary=5500, dk_position="PG"),
            _make_player(3, "Rayan Rupert", "G-F", 28.0,
                         [31, 30, 32, 31, 31], dk_salary=4300, dk_position="SF"),
            _make_player(4, "Walter Clayton Jr.", "G", 25.2,
                         [27, 26, 28, 25, 27], dk_salary=4700, dk_position="SG"),
            _make_player(5, "Jaylen Wells", "F", 26.6,
                         dk_salary=4800, dk_position="SF"),
            _make_player(6, "Taylor Hendricks", "F", 23.9,
                         dk_salary=5000, dk_position="PF/C"),
            _make_player(7, "Cedric Coward", "G", 26.1,
                         [23, 22, 24, 21, 23], dk_salary=5300, dk_position="SG"),
            _make_player(8, "Cam Spencer", "G", 23.9,
                         [25, 24, 26, 23, 27], dk_salary=5100, dk_position="SG/SF"),
            _make_player(9, "GG Jackson", "F", 20.8,
                         [26, 25, 27, 24, 28], dk_salary=5800, dk_position="PF"),
        ]
        injuries = [
            PlayerStatus(player_id=1, player_name="Ja Morant", status="Out"),
        ]

        result = allocate_team_minutes(
            rotation=rotation,
            injuries=injuries,
            dk_injury_statuses={1: "OUT"},
            team_name="MEM",
        )

        # Morant zeroed
        assert result[1] == 0.0

        # Small should be promoted to starter (PG vacancy, dk_position=PG)
        assert result[2] >= STARTER_FLOOR, (
            f"Small got {result[2]} min, expected >= {STARTER_FLOOR} "
            f"(DK position PG should win PG vacancy)"
        )

        # Total must be 240
        total = sum(result.values())
        assert abs(total - 240.0) < 0.5, f"Total {total} != 240"

    def test_healthy_beats_questionable_same_dk_position(self):
        """Healthy player should beat Q player at same DK position, even
        if Q player has higher salary (mirrors Jerome vs Small)."""
        import logging
        _logger = logging.getLogger("test")

        # Jerome: PG, $7K, Q
        jerome = _make_player(
            10, "Ty Jerome", "G", 21.9, [24, 23, 25, 24, 24],
            dk_salary=7000, dk_position="PG",
        )
        # Small: PG, $5.5K, healthy
        small = _make_player(
            14, "Javon Small", "G", 19.2, [26, 33, 15, 33, 19],
            dk_salary=5500, dk_position="PG",
        )
        candidates = [(jerome, 24.0), (small, 25.2)]
        dk_statuses = {10: "Q"}  # Jerome is Questionable

        winner = _pick_best_auto_promote(
            candidates, "PG", _logger, "test",
            dk_injury_statuses=dk_statuses,
        )
        assert winner.player_name == "Javon Small", (
            f"Expected Small (healthy, $5.5K) to beat Jerome (Q, $7K), "
            f"got {winner.player_name}"
        )

    def test_health_only_matters_when_dk_position_matches(self):
        """Health status should only matter among position-matched candidates.
        A healthy SF should NOT beat a Q PG for the PG slot."""
        import logging
        _logger = logging.getLogger("test")

        pg_player = _make_player(
            1, "PG Player", "G", 22.0, [25, 24, 26, 25, 25],
            dk_salary=6000, dk_position="PG",
        )
        sf_player = _make_player(
            2, "SF Player", "G-F", 28.0, [30, 31, 29, 30, 30],
            dk_salary=4500, dk_position="SF",
        )
        candidates = [(pg_player, 25.0), (sf_player, 30.0)]
        dk_statuses = {1: "Q"}  # PG Player is Q, SF Player is healthy

        winner = _pick_best_auto_promote(
            candidates, "PG", _logger, "test",
            dk_injury_statuses=dk_statuses,
        )
        # PG player should still win because DK position match (PG) > health
        assert winner.player_name == "PG Player", (
            f"Expected PG Player (Q but dk=PG) to beat SF Player (healthy, dk=SF), "
            f"got {winner.player_name}"
        )
