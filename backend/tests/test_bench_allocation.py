"""Tests for the concentrated bench allocation logic in top_down_minutes.

Covers the Phase 3 rewrite: geometric-decay shares, raised 6th/7th man
floors, hard guillotine (no 10th+ man minutes), and the two-pass
clamping/redistribution algorithm.

Also validates the helper functions _compute_bench_shares() and
_bench_floor_for_rank().
"""
import pytest
from typing import Dict, List, Optional

from app.models.player import PlayerMinutes, PlayerStatus
from app.services.top_down_minutes import (
    allocate_team_minutes,
    _compute_bench_shares,
    _bench_floor_for_rank,
    BENCH_6TH_FLOOR,
    BENCH_7TH_FLOOR,
    BENCH_DEEP_FLOOR,
    BENCH_CAP,
    BENCH_SCRUB_CAP,
    BENCH_SCRUB_OWNERSHIP_THRESHOLD,
    DK_MIN_SALARY_AUTOOUT,
    STARTER_FLOOR,
    STARTER_CAP,
    TOTAL_TEAM_MINUTES,
)


# ── Helpers ──────────────────────────────────────────────────────────────


def _make_player(
    pid: int,
    name: str,
    pos: str,
    season_avg: float,
    last_5: Optional[List[float]] = None,
    dk_salary: Optional[int] = None,
    team_id: int = 1,
    dnp_streak: int = 0,
    market_ownership: Optional[float] = None,
    is_confirmed_starter: Optional[bool] = None,
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
        recent_dnp_streak=dnp_streak,
        market_ownership=market_ownership,
        is_confirmed_starter=is_confirmed_starter,
    )


def _make_team(
    n_starters: int = 5,
    n_bench: int = 4,
    n_deep: int = 3,
    starter_avg: float = 32.0,
    bench_avg: float = 18.0,
    deep_avg: float = 8.0,
) -> List[PlayerMinutes]:
    """Build a realistic team roster with starters, bench, and deep bench."""
    positions = ["PG", "SG", "SF", "PF", "C"]
    bench_positions = ["PG", "SG", "SF", "PF"]
    deep_positions = ["PG", "SF", "C"]
    players = []
    pid = 1

    # Starters
    for i in range(n_starters):
        avg = starter_avg - i * 1.0  # slight taper: 32, 31, 30, 29, 28
        players.append(_make_player(
            pid, f"Starter{i+1}", positions[i % 5], avg,
            dk_salary=8000 - i * 500,
        ))
        pid += 1

    # Bench rotation
    for i in range(n_bench):
        avg = bench_avg - i * 2.0  # 18, 16, 14, 12
        players.append(_make_player(
            pid, f"Bench{i+1}", bench_positions[i % 4], avg,
            dk_salary=5000 - i * 500,
        ))
        pid += 1

    # Deep bench (should get 0 with the new system)
    for i in range(n_deep):
        avg = deep_avg - i * 2.0  # 8, 6, 4
        players.append(_make_player(
            pid, f"Deep{i+1}", deep_positions[i % 3], avg,
            dk_salary=3000,
        ))
        pid += 1

    return players


# ── Helper function tests ────────────────────────────────────────────────


class TestComputeBenchShares:
    """Tests for _compute_bench_shares()."""

    @pytest.mark.parametrize("bench_count", [1, 2, 3, 4, 5, 6, 7])
    def test_shares_sum_to_one(self, bench_count):
        shares = _compute_bench_shares(bench_count)
        assert len(shares) == bench_count
        assert abs(sum(shares) - 1.0) < 1e-10, \
            f"Shares sum to {sum(shares)}, expected 1.0"

    def test_shares_monotonically_decreasing(self):
        for bench_count in range(2, 8):
            shares = _compute_bench_shares(bench_count)
            for i in range(1, len(shares)):
                assert shares[i] < shares[i - 1], \
                    f"bench_count={bench_count}: share[{i}]={shares[i]} >= share[{i-1}]={shares[i-1]}"

    def test_empty_bench(self):
        assert _compute_bench_shares(0) == []

    def test_single_bench_player_gets_100_percent(self):
        shares = _compute_bench_shares(1)
        assert shares == [1.0]

    def test_typical_4_bench(self):
        """With 4 bench players, 6th man should get ~38% of remaining."""
        shares = _compute_bench_shares(4)
        assert shares[0] > 0.35  # 6th man gets the biggest share
        assert shares[3] > 0.10  # 9th man still gets meaningful share


class TestBenchFloorForRank:
    """Tests for _bench_floor_for_rank()."""

    def test_6th_man_floor(self):
        assert _bench_floor_for_rank(0) == BENCH_6TH_FLOOR  # 18.0

    def test_7th_man_floor(self):
        assert _bench_floor_for_rank(1) == BENCH_7TH_FLOOR  # 14.0

    def test_8th_man_floor(self):
        assert _bench_floor_for_rank(2) == BENCH_DEEP_FLOOR  # 4.0

    def test_9th_man_floor(self):
        assert _bench_floor_for_rank(3) == BENCH_DEEP_FLOOR  # 4.0

    def test_deep_bench_floor(self):
        for rank in range(4, 10):
            assert _bench_floor_for_rank(rank) == BENCH_DEEP_FLOOR


# ── Integration tests (full allocate_team_minutes) ───────────────────────


class TestBenchAllocation:
    """Integration tests for the concentrated bench allocation."""

    def test_total_always_240(self):
        """The 240-minute invariant must hold with the new bench logic."""
        team = _make_team(n_starters=5, n_bench=4, n_deep=3)
        result = allocate_team_minutes(
            rotation=team, injuries=[], rotation_depth=9,
        )
        total = sum(result.values())
        assert abs(total - TOTAL_TEAM_MINUTES) < 0.2, \
            f"Total minutes = {total}, expected {TOTAL_TEAM_MINUTES}"

    def test_6th_man_at_least_18_min(self):
        """6th man should get at least BENCH_6TH_FLOOR (18 min)."""
        team = _make_team(n_starters=5, n_bench=4, n_deep=3)
        result = allocate_team_minutes(
            rotation=team, injuries=[], rotation_depth=9,
        )
        # The 6th player (index 5) is the top bench player
        bench_mins = sorted(
            [result[p.player_id] for p in team if result[p.player_id] > 0
             and result[p.player_id] < STARTER_FLOOR],
            reverse=True,
        )
        assert len(bench_mins) >= 1, "No bench players got minutes"
        assert bench_mins[0] >= BENCH_6TH_FLOOR, \
            f"6th man got {bench_mins[0]} min, expected >= {BENCH_6TH_FLOOR}"

    def test_7th_man_at_least_14_min(self):
        """7th man should get at least BENCH_7TH_FLOOR (14 min)."""
        team = _make_team(n_starters=5, n_bench=4, n_deep=3)
        result = allocate_team_minutes(
            rotation=team, injuries=[], rotation_depth=9,
        )
        bench_mins = sorted(
            [result[p.player_id] for p in team if result[p.player_id] > 0
             and result[p.player_id] < STARTER_FLOOR],
            reverse=True,
        )
        assert len(bench_mins) >= 2, "Fewer than 2 bench players got minutes"
        assert bench_mins[1] >= BENCH_7TH_FLOOR, \
            f"7th man got {bench_mins[1]} min, expected >= {BENCH_7TH_FLOOR}"

    def test_no_10th_man_depth_9(self):
        """With rotation_depth=9, the 10th+ man must get 0.0 minutes."""
        team = _make_team(n_starters=5, n_bench=4, n_deep=3)
        result = allocate_team_minutes(
            rotation=team, injuries=[], rotation_depth=9,
        )
        active_count = sum(1 for m in result.values() if m > 0)
        assert active_count <= 9, \
            f"{active_count} active players, expected <= 9"
        # Deep bench should all be 0
        for p in team[-3:]:  # last 3 are deep bench
            assert result[p.player_id] == 0.0, \
                f"Deep bench {p.player_name} got {result[p.player_id]} min"

    def test_bench_cap_never_exceeded(self):
        """No bench player should ever exceed BENCH_CAP."""
        team = _make_team(n_starters=5, n_bench=4, n_deep=3)
        result = allocate_team_minutes(
            rotation=team, injuries=[], rotation_depth=9,
        )
        starters = set(p.player_id for p in team[:5])
        for p in team:
            if p.player_id not in starters and result[p.player_id] > 0:
                assert result[p.player_id] <= BENCH_CAP + 0.2, \
                    f"Bench player {p.player_name} got {result[p.player_id]} min, cap={BENCH_CAP}"

    def test_depth_7_two_bench_total_240(self):
        """With depth=7, only 2 bench players get minutes, total = 240."""
        team = _make_team(n_starters=5, n_bench=4, n_deep=3)
        result = allocate_team_minutes(
            rotation=team, injuries=[], rotation_depth=7,
        )
        total = sum(result.values())
        assert abs(total - TOTAL_TEAM_MINUTES) < 0.2, \
            f"Total = {total}, expected {TOTAL_TEAM_MINUTES}"
        active = sum(1 for m in result.values() if m > 0)
        assert active <= 7, f"{active} active, expected <= 7"

    def test_depth_10_capped_to_9(self):
        """With depth=10 requested, hard cap enforces 9-man rotation (4 bench max)."""
        team = _make_team(n_starters=5, n_bench=5, n_deep=2)
        result = allocate_team_minutes(
            rotation=team, injuries=[], rotation_depth=10,
        )
        total = sum(result.values())
        assert abs(total - TOTAL_TEAM_MINUTES) < 0.2
        active = sum(1 for m in result.values() if m > 0)
        assert active <= 9, f"Hard 9-man cap should limit active players, got {active}"
        # Only 4 bench players should get minutes (5 starters + 4 bench = 9)
        bench_mins = sorted(
            [result[p.player_id] for p in team if result[p.player_id] > 0
             and result[p.player_id] < STARTER_FLOOR],
            reverse=True,
        )
        assert len(bench_mins) == 4, f"Expected 4 bench (9-man cap), got {len(bench_mins)}"
        # 10th man (5th bench) should get 0 — zeroed by depth cap
        zeroed = sum(1 for m in result.values() if m == 0)
        assert zeroed >= 3, "Deep bench players should be zeroed"

    @pytest.mark.parametrize("depth", [7, 8, 9, 10, 11, 12])
    def test_total_always_240_parametrized(self, depth):
        """Across all rotation depths (7-12), total must equal 240."""
        n_bench = max(depth - 5, 0)
        team = _make_team(n_starters=5, n_bench=n_bench, n_deep=3)
        result = allocate_team_minutes(
            rotation=team, injuries=[], rotation_depth=depth,
        )
        total = sum(result.values())
        assert abs(total - TOTAL_TEAM_MINUTES) < 0.2, \
            f"depth={depth}: total={total}, expected {TOTAL_TEAM_MINUTES}"

    def test_bench_minutes_concentrated_not_diffuse(self):
        """The 6th man should get more minutes than under the old system.

        Old system: 6th man ≈ 22 min with remaining=75
        New system: 6th man ≈ 26 min (cap) with remaining=75
        """
        team = _make_team(n_starters=5, n_bench=4, n_deep=3)
        result = allocate_team_minutes(
            rotation=team, injuries=[], rotation_depth=9,
        )
        # Get bench allocations sorted descending
        starter_ids = set(p.player_id for p in team[:5])
        bench_mins = sorted(
            [result[pid] for pid in result if pid not in starter_ids and result[pid] > 0],
            reverse=True,
        )
        if bench_mins:
            # 6th man should get at least 22 min (better than old system's ~22)
            assert bench_mins[0] >= 22.0, \
                f"6th man only got {bench_mins[0]} min — bench bloat not fixed"


# ── Scrub Cap tests ─────────────────────────────────────────────────────


def _make_team_with_scrub(
    scrub_salary: Optional[int] = 3000,
    scrub_ownership: Optional[float] = None,
    scrub_confirmed: Optional[bool] = None,
    scrub_bench_rank: int = 3,  # 0-indexed in bench array (0=6th man, 3=9th man)
) -> List[PlayerMinutes]:
    """Build a 9-man team with one scrub at the specified bench rank.

    Default: scrub is the 9th man (bench index 3) with $3000 salary, no ownership.
    """
    positions = ["PG", "SG", "SF", "PF", "C"]
    bench_positions = ["PG", "SG", "SF", "PF"]
    players = []
    pid = 1

    # 5 starters
    for i in range(5):
        avg = 32.0 - i * 1.0
        players.append(_make_player(
            pid, f"Starter{i+1}", positions[i], avg,
            dk_salary=8000 - i * 500,
        ))
        pid += 1

    # 4 bench players (one is the scrub)
    for i in range(4):
        if i == scrub_bench_rank:
            # The scrub
            avg = 12.0  # decent season avg (scrub forced in by injury)
            players.append(_make_player(
                pid, "Scrub", bench_positions[i], avg,
                dk_salary=scrub_salary,
                market_ownership=scrub_ownership,
                is_confirmed_starter=scrub_confirmed,
            ))
        else:
            avg = 18.0 - i * 2.0
            players.append(_make_player(
                pid, f"Bench{i+1}", bench_positions[i], avg,
                dk_salary=5000 - i * 500,
            ))
        pid += 1

    return players


class TestScrubCap:
    """Tests for the minimum-salary scrub cap in Phase 3 bench allocation."""

    def test_scrub_capped_at_8_min(self):
        """$3000 salary, no ownership → capped at BENCH_SCRUB_CAP."""
        team = _make_team_with_scrub(scrub_salary=3000, scrub_ownership=None)
        result = allocate_team_minutes(
            rotation=team, injuries=[], rotation_depth=9,
        )
        scrub = [p for p in team if p.player_name == "Scrub"][0]
        assert result[scrub.player_id] <= BENCH_SCRUB_CAP, \
            f"Scrub got {result[scrub.player_id]} min, expected <= {BENCH_SCRUB_CAP}"

    def test_scrub_cap_zero_ownership(self):
        """$3200 salary, 0.0% ownership → capped."""
        team = _make_team_with_scrub(scrub_salary=3200, scrub_ownership=0.0)
        result = allocate_team_minutes(
            rotation=team, injuries=[], rotation_depth=9,
        )
        scrub = [p for p in team if p.player_name == "Scrub"][0]
        assert result[scrub.player_id] <= BENCH_SCRUB_CAP, \
            f"Scrub got {result[scrub.player_id]} min, expected <= {BENCH_SCRUB_CAP}"

    def test_scrub_cap_low_ownership(self):
        """$3100 salary, 1.5% ownership (below threshold) → capped."""
        team = _make_team_with_scrub(scrub_salary=3100, scrub_ownership=1.5)
        result = allocate_team_minutes(
            rotation=team, injuries=[], rotation_depth=9,
        )
        scrub = [p for p in team if p.player_name == "Scrub"][0]
        assert result[scrub.player_id] <= BENCH_SCRUB_CAP, \
            f"Scrub got {result[scrub.player_id]} min, expected <= {BENCH_SCRUB_CAP}"

    def test_no_cap_above_salary(self):
        """$3300 salary (above DK_MIN_SALARY_AUTOOUT) → no scrub cap."""
        team = _make_team_with_scrub(scrub_salary=3300, scrub_ownership=None)
        result = allocate_team_minutes(
            rotation=team, injuries=[], rotation_depth=9,
        )
        scrub = [p for p in team if p.player_name == "Scrub"][0]
        assert result[scrub.player_id] > BENCH_SCRUB_CAP, \
            f"Player at $3300 got {result[scrub.player_id]} min, should exceed scrub cap"

    def test_no_cap_above_ownership(self):
        """$3000 salary but 5.0% ownership (market signal) → no scrub cap."""
        team = _make_team_with_scrub(scrub_salary=3000, scrub_ownership=5.0)
        result = allocate_team_minutes(
            rotation=team, injuries=[], rotation_depth=9,
        )
        scrub = [p for p in team if p.player_name == "Scrub"][0]
        assert result[scrub.player_id] > BENCH_SCRUB_CAP, \
            f"Player with 5% ownership got {result[scrub.player_id]} min, should exceed scrub cap"

    def test_no_cap_at_threshold(self):
        """$3000 salary, exactly 2.0% ownership (at threshold, strict <) → no cap."""
        team = _make_team_with_scrub(scrub_salary=3000, scrub_ownership=2.0)
        result = allocate_team_minutes(
            rotation=team, injuries=[], rotation_depth=9,
        )
        scrub = [p for p in team if p.player_name == "Scrub"][0]
        assert result[scrub.player_id] > BENCH_SCRUB_CAP, \
            f"Player at exactly 2.0% ownership got {result[scrub.player_id]} min, should exceed scrub cap"

    def test_no_cap_salary_none(self):
        """Salary=None (unknown) → no scrub cap applied."""
        team = _make_team_with_scrub(scrub_salary=None, scrub_ownership=None)
        result = allocate_team_minutes(
            rotation=team, injuries=[], rotation_depth=9,
        )
        scrub = [p for p in team if p.player_name == "Scrub"][0]
        assert result[scrub.player_id] > BENCH_SCRUB_CAP, \
            f"Player with no salary got {result[scrub.player_id]} min, should exceed scrub cap"

    def test_confirmed_starter_exempt(self):
        """$3000 salary + confirmed starter → exempt from scrub cap."""
        team = _make_team_with_scrub(
            scrub_salary=3000, scrub_ownership=None, scrub_confirmed=True,
        )
        result = allocate_team_minutes(
            rotation=team, injuries=[], rotation_depth=9,
        )
        scrub = [p for p in team if p.player_name == "Scrub"][0]
        assert result[scrub.player_id] > BENCH_SCRUB_CAP, \
            f"Confirmed starter got {result[scrub.player_id]} min, should exceed scrub cap"

    def test_surplus_redistributed(self):
        """When a scrub is capped, other bench players get more minutes."""
        # Build two teams: one with scrub, one without (normal $5000 player)
        team_scrub = _make_team_with_scrub(scrub_salary=3000, scrub_ownership=None)
        team_normal = _make_team_with_scrub(scrub_salary=5000, scrub_ownership=None)

        result_scrub = allocate_team_minutes(
            rotation=team_scrub, injuries=[], rotation_depth=9,
        )
        result_normal = allocate_team_minutes(
            rotation=team_normal, injuries=[], rotation_depth=9,
        )

        # The non-scrub bench players should get MORE minutes when scrub is capped
        scrub_bench_ids = set(
            p.player_id for p in team_scrub
            if p.player_name != "Scrub" and p.player_name.startswith("Bench")
        )
        normal_bench_ids = set(
            p.player_id for p in team_normal
            if p.player_name != "Scrub" and p.player_name.startswith("Bench")
        )
        scrub_bench_total = sum(result_scrub[pid] for pid in scrub_bench_ids)
        normal_bench_total = sum(result_normal[pid] for pid in normal_bench_ids)

        assert scrub_bench_total > normal_bench_total, \
            f"Other bench got {scrub_bench_total} with scrub cap vs {normal_bench_total} without"

    def test_total_240(self):
        """Total minutes must be 240 even with a scrub-capped player."""
        team = _make_team_with_scrub(scrub_salary=3000, scrub_ownership=None)
        result = allocate_team_minutes(
            rotation=team, injuries=[], rotation_depth=9,
        )
        total = sum(result.values())
        assert abs(total - TOTAL_TEAM_MINUTES) < 0.2, \
            f"Total = {total}, expected {TOTAL_TEAM_MINUTES}"

    def test_overrides_6th_man_floor(self):
        """Scrub as 6th man: scrub cap (8.0) must override 6th man floor (18.0)."""
        team = _make_team_with_scrub(
            scrub_salary=3000, scrub_ownership=None, scrub_bench_rank=0,
        )
        result = allocate_team_minutes(
            rotation=team, injuries=[], rotation_depth=9,
        )
        scrub = [p for p in team if p.player_name == "Scrub"][0]
        assert result[scrub.player_id] <= BENCH_SCRUB_CAP, \
            f"Scrub as 6th man got {result[scrub.player_id]} min, expected <= {BENCH_SCRUB_CAP} (floor should be overridden)"
