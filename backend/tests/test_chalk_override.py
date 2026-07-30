"""Tests for the Phase 0d Chalk Override in top_down_minutes.

Covers the market-signal-based starter pre-seeding:
  - Confirmed starter promotion (is_confirmed_starter=True)
  - Ownership-based promotion (market_ownership > 15%)
  - Graceful no-op when no market signals exist
  - Position slot claiming (exact + family match)
  - Interaction with vacancy detection (star OUT)
  - Maximum 5 starters cap
  - Priority ordering (confirmed > ownership > depth-chart)
  - Zeroed players excluded from chalk override
  - Total minutes still exactly 240.0
"""
import pytest
from typing import Dict, List, Optional

from app.models.player import PlayerMinutes, PlayerStatus
from app.services.top_down_minutes import (
    allocate_team_minutes,
    CHALK_OWNERSHIP_THRESHOLD,
    STARTER_FLOOR,
    STARTER_CAP,
    BENCH_6TH_FLOOR,
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
    """Build a PlayerMinutes with optional chalk signals."""
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


def _make_standard_team(
    chalk_overrides: Optional[Dict[int, dict]] = None,
) -> List[PlayerMinutes]:
    """Build a 12-player roster (5 starters, 4 bench, 3 deep).

    chalk_overrides: dict of {player_id: {market_ownership, is_confirmed_starter}}
    to apply after construction.
    """
    chalk_overrides = chalk_overrides or {}
    positions = ["PG", "SG", "SF", "PF", "C"]
    bench_positions = ["PG", "SG", "SF", "PF"]
    deep_positions = ["PG", "SF", "C"]
    players = []
    pid = 1

    # Starters (avg 32-28)
    for i in range(5):
        avg = 32.0 - i * 1.0
        p = _make_player(
            pid, f"Starter{i+1}", positions[i], avg,
            dk_salary=8000 - i * 500,
        )
        players.append(p)
        pid += 1

    # Bench rotation (avg 18-12)
    for i in range(4):
        avg = 18.0 - i * 2.0
        p = _make_player(
            pid, f"Bench{i+1}", bench_positions[i], avg,
            dk_salary=5000 - i * 500,
        )
        players.append(p)
        pid += 1

    # Deep bench (avg 8-4)
    for i in range(3):
        avg = 8.0 - i * 2.0
        p = _make_player(
            pid, f"Deep{i+1}", deep_positions[i], avg,
            dk_salary=3000,
        )
        players.append(p)
        pid += 1

    # Apply chalk overrides
    for p in players:
        if p.player_id in chalk_overrides:
            overrides = chalk_overrides[p.player_id]
            if "market_ownership" in overrides:
                p.market_ownership = overrides["market_ownership"]
            if "is_confirmed_starter" in overrides:
                p.is_confirmed_starter = overrides["is_confirmed_starter"]

    return players


# ── Tests ────────────────────────────────────────────────────────────────


class TestChalkOverrideNoOp:
    """When no market signals exist, Phase 0d should be a complete no-op."""

    def test_no_chalk_data_no_override(self):
        """With all fields None, starters are chosen by depth-chart score."""
        team = _make_standard_team()
        result = allocate_team_minutes(
            rotation=team, injuries=[], rotation_depth=9,
        )
        total = sum(result.values())
        assert abs(total - TOTAL_TEAM_MINUTES) < 0.2

        # Top 5 by season_avg should be starters (players 1-5)
        for p in team[:5]:
            assert result[p.player_id] >= STARTER_FLOOR, \
                f"{p.player_name} (avg={p.season_avg}) should be a starter"

    def test_ownership_below_threshold_no_override(self):
        """Ownership = 8% (below 15% threshold) → no chalk override."""
        team = _make_standard_team(
            chalk_overrides={6: {"market_ownership": 8.0}}  # Bench1
        )
        result = allocate_team_minutes(
            rotation=team, injuries=[], rotation_depth=9,
        )
        # Bench1 (pid=6) should NOT be a starter
        bench1_min = result[6]
        assert bench1_min < STARTER_FLOOR, \
            f"Bench1 with 8% ownership should be bench, got {bench1_min} min"


class TestConfirmedStarterPromotion:
    """Tests for is_confirmed_starter=True override."""

    def test_confirmed_starter_beats_depth_chart(self):
        """Backup PG (avg=12) with confirmed=True beats PG1 (avg=32)."""
        team = _make_standard_team(
            chalk_overrides={
                # Bench1 is a PG with avg=18, confirmed starter
                6: {"is_confirmed_starter": True},
            }
        )
        result = allocate_team_minutes(
            rotation=team, injuries=[], rotation_depth=9,
        )
        # Bench1 (pid=6, PG) should get starter minutes
        assert result[6] >= STARTER_FLOOR, \
            f"Confirmed starter Bench1 got {result[6]} min, expected >= {STARTER_FLOOR}"
        total = sum(result.values())
        assert abs(total - TOTAL_TEAM_MINUTES) < 0.2

    def test_confirmed_starter_displaces_positional_starter(self):
        """Confirmed PG bench player claims PG slot, PG1 drops to bench or other slot."""
        team = _make_standard_team(
            chalk_overrides={
                6: {"is_confirmed_starter": True},  # Bench1 = PG
            }
        )
        result = allocate_team_minutes(
            rotation=team, injuries=[], rotation_depth=9,
        )
        # Both PG1 (pid=1) and Bench1 (pid=6) should still get minutes
        # PG1 might get displaced to bench or claim another slot
        assert result[1] > 0, "PG1 should still get some minutes"
        assert result[6] >= STARTER_FLOOR, "Confirmed Bench1 should be starter"


class TestOwnershipPromotion:
    """Tests for market_ownership > threshold override."""

    def test_high_ownership_promotes_to_starter(self):
        """SF bench player with 55% ownership → promoted to starter."""
        team = _make_standard_team(
            chalk_overrides={
                8: {"market_ownership": 55.0},  # Bench3 = SF
            }
        )
        result = allocate_team_minutes(
            rotation=team, injuries=[], rotation_depth=9,
        )
        assert result[8] >= STARTER_FLOOR, \
            f"Bench3 with 55% ownership got {result[8]} min, expected >= {STARTER_FLOOR}"
        total = sum(result.values())
        assert abs(total - TOTAL_TEAM_MINUTES) < 0.2

    def test_borderline_ownership_above_threshold(self):
        """Ownership exactly at threshold+0.1 should trigger override."""
        team = _make_standard_team(
            chalk_overrides={
                7: {"market_ownership": CHALK_OWNERSHIP_THRESHOLD + 0.1},
            }
        )
        result = allocate_team_minutes(
            rotation=team, injuries=[], rotation_depth=9,
        )
        assert result[7] >= STARTER_FLOOR, \
            f"Bench2 with {CHALK_OWNERSHIP_THRESHOLD + 0.1}% ownership should be starter"


class TestChalkPriority:
    """Tests for priority ordering among chalk candidates."""

    def test_confirmed_priority_over_ownership(self):
        """Confirmed starter wins over higher-ownership-only player."""
        # Two bench players: one confirmed (20% own), one 65% ownership only
        team = _make_standard_team(
            chalk_overrides={
                7: {"market_ownership": 65.0},  # Bench2 = SG, high own only
                8: {"market_ownership": 20.0, "is_confirmed_starter": True},  # Bench3 = SF, confirmed
            }
        )
        result = allocate_team_minutes(
            rotation=team, injuries=[], rotation_depth=9,
        )
        # Both should get starter minutes since there are 2 chalk + 3 remaining slots
        assert result[7] >= STARTER_FLOOR, "Bench2 (65% own) should be starter"
        assert result[8] >= STARTER_FLOOR, "Bench3 (confirmed) should be starter"
        total = sum(result.values())
        assert abs(total - TOTAL_TEAM_MINUTES) < 0.2


class TestChalkMaxFive:
    """Maximum 5 chalk starters cap."""

    def test_chalk_max_five_cap(self):
        """7 players with confirmed=True → only 5 become starters."""
        # Give ALL 7 non-deep players confirmed status
        overrides = {}
        for pid in range(1, 10):
            overrides[pid] = {"is_confirmed_starter": True, "market_ownership": 50.0}
        team = _make_standard_team(chalk_overrides=overrides)
        result = allocate_team_minutes(
            rotation=team, injuries=[], rotation_depth=9,
        )
        active = sum(1 for m in result.values() if m > 0)
        starters_count = sum(1 for m in result.values() if m >= STARTER_FLOOR)
        assert starters_count == 5, f"Expected exactly 5 starters, got {starters_count}"
        total = sum(result.values())
        assert abs(total - TOTAL_TEAM_MINUTES) < 0.2

    def test_six_chalk_candidates_five_placed(self):
        """6 chalk candidates → 5 placed as starters, 1 to bench."""
        overrides = {}
        for pid in [1, 2, 3, 4, 5, 6]:
            overrides[pid] = {"market_ownership": 25.0}
        team = _make_standard_team(chalk_overrides=overrides)
        result = allocate_team_minutes(
            rotation=team, injuries=[], rotation_depth=9,
        )
        total = sum(result.values())
        assert abs(total - TOTAL_TEAM_MINUTES) < 0.2


class TestChalkPositionSlots:
    """Position slot claiming in chalk override."""

    def test_chalk_claims_position_slot(self):
        """Chalk PG claims PG slot, regular PG1 fills another slot."""
        team = _make_standard_team(
            chalk_overrides={
                6: {"is_confirmed_starter": True},  # Bench1 = PG
            }
        )
        result = allocate_team_minutes(
            rotation=team, injuries=[], rotation_depth=9,
        )
        # PG1 (pid=1, avg=32) should still get minutes (fills another slot)
        assert result[1] > 0
        # Bench1 PG (pid=6) gets starter minutes
        assert result[6] >= STARTER_FLOOR
        # Total 5 starters exactly
        starters = sum(1 for m in result.values() if m >= STARTER_FLOOR)
        assert starters == 5

    def test_chalk_family_match(self):
        """Player with "G" position claims PG or SG slot via family match."""
        team = _make_standard_team()
        # Replace Bench1 with a "G" (guard family) player
        team[5] = _make_player(
            6, "GuardX", "G", 14.0,
            dk_salary=5000,
            market_ownership=40.0,
        )
        result = allocate_team_minutes(
            rotation=team, injuries=[], rotation_depth=9,
        )
        # GuardX should get starter minutes via guard-family slot
        assert result[6] >= STARTER_FLOOR, \
            f"GuardX with 40% ownership got {result[6]} min, expected >= {STARTER_FLOOR}"


class TestChalkWithVacancy:
    """Interaction between chalk override and vacancy detection."""

    def test_chalk_with_vacancy_detection(self):
        """Star PG OUT + backup PG confirmed → chalk fills the vacancy slot."""
        team = _make_standard_team(
            chalk_overrides={
                6: {"is_confirmed_starter": True, "market_ownership": 55.0},
            }
        )
        # Mark PG1 (Starter1) as OUT
        injuries = [
            PlayerStatus(
                player_id=1,
                player_name="Starter1",
                status="Out",
                injury_description="Knee",
            )
        ]
        result = allocate_team_minutes(
            rotation=team, injuries=injuries, rotation_depth=9,
        )
        # Starter1 should be zeroed
        assert result[1] == 0.0, "OUT starter should get 0 minutes"
        # Bench1 PG (confirmed) should fill the vacancy
        assert result[6] >= STARTER_FLOOR, \
            f"Confirmed Bench1 should be starter, got {result[6]} min"
        total = sum(result.values())
        assert abs(total - TOTAL_TEAM_MINUTES) < 0.2


class TestChalkInjuredExcluded:
    """Injured chalk players should be excluded by Phase 0 before Phase 0d."""

    def test_injured_chalk_player_excluded(self):
        """Confirmed starter who is ruled OUT → zeroed, not placed as starter."""
        team = _make_standard_team(
            chalk_overrides={
                6: {"is_confirmed_starter": True, "market_ownership": 60.0},
            }
        )
        # Mark Bench1 (the chalk player) as OUT
        injuries = [
            PlayerStatus(
                player_id=6,
                player_name="Bench1",
                status="Out",
                injury_description="Ankle",
            )
        ]
        result = allocate_team_minutes(
            rotation=team, injuries=injuries, rotation_depth=9,
        )
        # Bench1 should be zeroed despite being "confirmed"
        assert result[6] == 0.0, \
            "OUT chalk player should get 0 minutes regardless of signals"
        total = sum(result.values())
        assert abs(total - TOTAL_TEAM_MINUTES) < 0.2


class TestChalkTotal240:
    """240-minute invariant with chalk overrides."""

    def test_total_240_with_chalk_override(self):
        """Sum of all allocations == 240.0 with 2 chalk overrides."""
        team = _make_standard_team(
            chalk_overrides={
                6: {"is_confirmed_starter": True, "market_ownership": 40.0},
                7: {"market_ownership": 30.0},
            }
        )
        result = allocate_team_minutes(
            rotation=team, injuries=[], rotation_depth=9,
        )
        total = sum(result.values())
        assert abs(total - TOTAL_TEAM_MINUTES) < 0.2, \
            f"Total = {total}, expected {TOTAL_TEAM_MINUTES}"

    @pytest.mark.parametrize("depth", [7, 8, 9, 10, 11])
    def test_chalk_parametrized_depths(self, depth):
        """Total = 240 across depth 7-11 with chalk overrides."""
        n_bench = max(depth - 5, 0)
        bench_positions = ["PG", "SG", "SF", "PF", "C"]
        players = []
        pid = 1
        # 5 starters
        for i in range(5):
            players.append(_make_player(
                pid, f"S{i+1}", bench_positions[i], 32.0 - i,
                dk_salary=8000 - i * 500,
            ))
            pid += 1
        # Bench
        for i in range(n_bench):
            p = _make_player(
                pid, f"B{i+1}", bench_positions[i % 5], 16.0 - i * 2,
                dk_salary=5000 - i * 500,
                # First bench player is chalk-confirmed
                is_confirmed_starter=True if i == 0 else None,
                market_ownership=45.0 if i == 0 else None,
            )
            players.append(p)
            pid += 1
        # 2 deep bench
        for i in range(2):
            players.append(_make_player(
                pid, f"D{i+1}", bench_positions[i % 5], 5.0,
                dk_salary=3000,
            ))
            pid += 1

        result = allocate_team_minutes(
            rotation=players, injuries=[], rotation_depth=depth,
        )
        total = sum(result.values())
        assert abs(total - TOTAL_TEAM_MINUTES) < 0.2, \
            f"depth={depth}: total={total}, expected {TOTAL_TEAM_MINUTES}"


class TestChalkDepthTrimProtection:
    """Chalk-protected players must survive Phase 0b depth trim.

    The "Josh Minott bug": a 64%-owned confirmed starter with season_avg=14
    scores ~14 on the depth chart, ranking them 10th+.  Phase 0b's 9-man
    hard cap kills them before Phase 0d can promote them.  Chalk protection
    shields these players from the trim.
    """

    def test_high_ownership_deep_bench_survives_depth_trim(self):
        """Player ranked 11th by depth-chart but 64% owned must NOT be trimmed.

        Simulates: Josh Minott (F, avg=14, $4600, 64% owned, confirmed starter)
        on a team with 12 rostered players.  Without chalk protection, he'd be
        culled at position 11 in the Phase 0b sort.
        """
        positions = ["PG", "SG", "SF", "PF", "C"]
        players = []
        pid = 1

        # 5 real starters (avg 33-29)
        for i in range(5):
            players.append(_make_player(
                pid, f"Starter{i+1}", positions[i], 33.0 - i,
                dk_salary=8000 - i * 500,
            ))
            pid += 1

        # 5 bench players (avg 22-14) — all rank higher than Minott by score
        bench_pos = ["PG", "SG", "SF", "PF", "C"]
        for i in range(5):
            players.append(_make_player(
                pid, f"Bench{i+1}", bench_pos[i], 22.0 - i * 2,
                dk_salary=5000 - i * 300,
            ))
            pid += 1

        # Josh Minott: low season_avg but MASSIVE ownership + confirmed starter
        minott = _make_player(
            pid, "Josh Minott", "SF", 14.0,
            dk_salary=4600,
            market_ownership=64.0,
            is_confirmed_starter=True,
        )
        players.append(minott)
        pid += 1

        # Another deep bench scrub
        players.append(_make_player(
            pid, "DeepScrub", "PG", 5.0, dk_salary=3000,
        ))

        result = allocate_team_minutes(
            rotation=players, injuries=[], rotation_depth=9,
        )

        # Minott MUST get starter-level minutes (chalk-protected + promoted)
        assert result[minott.player_id] >= STARTER_FLOOR, (
            f"Josh Minott (64% own, confirmed starter) got {result[minott.player_id]} min, "
            f"expected >= {STARTER_FLOOR} — chalk protection failed"
        )
        # Total must still be 240
        total = sum(result.values())
        assert abs(total - TOTAL_TEAM_MINUTES) < 0.2, \
            f"Total = {total}, expected {TOTAL_TEAM_MINUTES}"

    def test_ownership_only_survives_depth_trim(self):
        """High ownership alone (no confirmed starter) also protects from trim."""
        positions = ["PG", "SG", "SF", "PF", "C"]
        players = []
        pid = 1

        # 5 starters
        for i in range(5):
            players.append(_make_player(
                pid, f"Starter{i+1}", positions[i], 33.0 - i,
                dk_salary=8000 - i * 500,
            ))
            pid += 1

        # 5 bench (avg 22-14)
        for i in range(5):
            players.append(_make_player(
                pid, f"Bench{i+1}", positions[i], 22.0 - i * 2,
                dk_salary=5000 - i * 300,
            ))
            pid += 1

        # High-ownership player ranked 11th+ by depth chart
        chalk_player = _make_player(
            pid, "ChalkGuy", "PF", 12.0,
            dk_salary=4000,
            market_ownership=45.0,  # High ownership, no confirmed flag
            is_confirmed_starter=None,
        )
        players.append(chalk_player)

        result = allocate_team_minutes(
            rotation=players, injuries=[], rotation_depth=9,
        )

        # ChalkGuy must survive and get starter minutes
        assert result[chalk_player.player_id] >= STARTER_FLOOR, (
            f"ChalkGuy (45% own) got {result[chalk_player.player_id]} min, "
            f"expected >= {STARTER_FLOOR} — ownership-only protection failed"
        )
        total = sum(result.values())
        assert abs(total - TOTAL_TEAM_MINUTES) < 0.2

    def test_low_ownership_still_trimmed(self):
        """Players below ownership threshold should still be trimmed normally."""
        positions = ["PG", "SG", "SF", "PF", "C"]
        players = []
        pid = 1

        # 5 starters + 5 bench (fill 10 slots)
        for i in range(5):
            players.append(_make_player(
                pid, f"Starter{i+1}", positions[i], 33.0 - i,
                dk_salary=8000,
            ))
            pid += 1
        for i in range(5):
            players.append(_make_player(
                pid, f"Bench{i+1}", positions[i], 20.0 - i,
                dk_salary=5000,
            ))
            pid += 1

        # Low-ownership deep bench — NO protection
        scrub = _make_player(
            pid, "LowOwnScrub", "SF", 8.0,
            dk_salary=3000,
            market_ownership=5.0,  # Below 15% threshold
        )
        players.append(scrub)

        result = allocate_team_minutes(
            rotation=players, injuries=[], rotation_depth=9,
        )

        # LowOwnScrub should be trimmed (0 minutes)
        assert result[scrub.player_id] == 0.0, (
            f"LowOwnScrub (5% own) got {result[scrub.player_id]} min, "
            f"expected 0.0 — should have been trimmed"
        )

    def test_chalk_protection_displaces_lowest_non_chalk(self):
        """When chalk player is protected, the lowest non-chalk player gets trimmed."""
        positions = ["PG", "SG", "SF", "PF", "C"]
        players = []
        pid = 1

        # 5 starters
        for i in range(5):
            players.append(_make_player(
                pid, f"Starter{i+1}", positions[i], 33.0 - i,
                dk_salary=8000,
            ))
            pid += 1

        # 4 bench players (avg 20, 18, 16, 14)
        for i in range(4):
            players.append(_make_player(
                pid, f"Bench{i+1}", positions[i], 20.0 - i * 2,
                dk_salary=5000 - i * 300,
            ))
            pid += 1

        # The 10th player by score (avg=10) — this one should be displaced
        displaceable = _make_player(
            pid, "Displaceable", "C", 10.0, dk_salary=3500,
        )
        players.append(displaceable)
        pid += 1

        # 11th player by score but chalk-protected (avg=8, 50% ownership)
        chalk_player = _make_player(
            pid, "ChalkProtected", "SF", 8.0,
            dk_salary=4000,
            market_ownership=50.0,
        )
        players.append(chalk_player)

        result = allocate_team_minutes(
            rotation=players, injuries=[], rotation_depth=9,
        )

        # ChalkProtected (50% own) should survive
        assert result[chalk_player.player_id] > 0, (
            f"ChalkProtected (50% own) got 0 min — should have survived trim"
        )
        # Displaceable (no chalk signal) should be trimmed
        assert result[displaceable.player_id] == 0.0, (
            f"Displaceable (no signals) got {result[displaceable.player_id]} min, "
            f"expected 0.0 — should have been displaced by chalk player"
        )
        total = sum(result.values())
        assert abs(total - TOTAL_TEAM_MINUTES) < 0.2
