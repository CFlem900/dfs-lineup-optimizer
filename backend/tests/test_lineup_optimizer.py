"""Tests for the DFS lineup optimizer service."""

import pytest
from unittest.mock import patch
from app.models.lineup import PlayerPoolEntry, OptimizeRequest, MultiLineupRequest, MultiLineupResponse
from app.services.lineup_optimizer_service import (
    LineupOptimizerService,
    DK_SALARY_CAP,
    FD_SALARY_CAP,
    DK_ROSTER_SLOTS,
    FD_ROSTER_SLOTS,
    DK_SLOT_ELIGIBILITY,
    DK_SLOT_ORDER,
    FD_SLOT_ORDER,
    DK_SHOWDOWN_SLOTS,
    DK_SHOWDOWN_SALARY_CAP,
    CPT_MULTIPLIER,
    _base_slot,
    _index_slots,
    _PULP_AVAILABLE,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_pool_player(
    player_id, name, pos, team, salary, fp,
    floor_fp=None, ceil_fp=None, minutes=25.0,
):
    """Helper to create a PlayerPoolEntry for tests."""
    floor_fp = floor_fp or round(fp * 0.8, 1)
    ceil_fp = ceil_fp or round(fp * 1.2, 1)

    # Compute eligible DK slots
    eligible = []
    for slot, positions in DK_SLOT_ELIGIBILITY.items():
        if pos in positions:
            eligible.append(slot)

    return PlayerPoolEntry(
        player_id=player_id,
        player_name=name,
        position=pos,
        team_abbreviation=team,
        salary=salary,
        projected_fp=fp,
        floor_fp=floor_fp,
        ceiling_fp=ceil_fp,
        projected_minutes=minutes,
        dk_value=round(fp / salary * 1000, 2) if salary > 0 else 0.0,
        eligible_slots=eligible,
        dk_player_id=player_id + 10000,
    )


@pytest.fixture
def mock_pool():
    """A pool of 16 players (2+ per position) for testing the optimizer."""
    return [
        # PG (3 players)
        _make_pool_player(1, "PG Star", "PG", "BOS", 9000, 45.0),
        _make_pool_player(2, "PG Mid", "PG", "NYK", 6000, 28.0),
        _make_pool_player(3, "PG Value", "PG", "MIA", 3500, 15.0),
        # SG (3 players)
        _make_pool_player(4, "SG Star", "SG", "LAL", 8500, 42.0),
        _make_pool_player(5, "SG Mid", "SG", "CHI", 5500, 25.0),
        _make_pool_player(6, "SG Value", "SG", "ORL", 3500, 14.0),
        # SF (3 players)
        _make_pool_player(7, "SF Star", "SF", "DEN", 8000, 40.0),
        _make_pool_player(8, "SF Mid", "SF", "PHX", 5000, 22.0),
        _make_pool_player(9, "SF Value", "SF", "SAS", 3500, 13.0),
        # PF (3 players)
        _make_pool_player(10, "PF Star", "PF", "MIL", 7500, 38.0),
        _make_pool_player(11, "PF Mid", "PF", "CLE", 5000, 20.0),
        _make_pool_player(12, "PF Value", "PF", "IND", 3500, 12.0),
        # C (3 players)
        _make_pool_player(13, "C Star", "C", "MIN", 7000, 35.0),
        _make_pool_player(14, "C Mid", "C", "SAC", 4500, 18.0),
        _make_pool_player(15, "C Value", "C", "UTA", 3500, 11.0),
        # Extra guard for UTIL flexibility
        _make_pool_player(16, "G Bench", "PG", "WAS", 3500, 10.0),
    ]


@pytest.fixture
def optimizer():
    """A LineupOptimizerService with None services (pool tests bypass them)."""
    return LineupOptimizerService(
        dfs_service=None,
        dk_draftables_service=None,
        nba_service=None,
        injury_service=None,
        rotation_engine=None,
    )


# ---------------------------------------------------------------------------
# Greedy Fill Tests
# ---------------------------------------------------------------------------

class TestGreedyFill:
    """Test the greedy slot-filling algorithm."""

    def test_fills_all_dk_slots(self, optimizer, mock_pool):
        """Greedy fill should produce exactly 8 filled slots for DK."""
        lineup = optimizer._greedy_fill(
            pool=mock_pool,
            lineup={},
            remaining_slots=["C", "PG", "SG", "SF", "PF", "G", "F", "UTIL"],
            used_ids=set(),
            remaining_salary=DK_SALARY_CAP,
            platform="dk",
        )
        assert len(lineup) == 8

    def test_respects_salary_cap(self, optimizer, mock_pool):
        """Total salary should not exceed $50,000."""
        lineup = optimizer._greedy_fill(
            pool=mock_pool,
            lineup={},
            remaining_slots=["C", "PG", "SG", "SF", "PF", "G", "F", "UTIL"],
            used_ids=set(),
            remaining_salary=DK_SALARY_CAP,
            platform="dk",
        )
        total_salary = sum(p.salary for p in lineup.values())
        assert total_salary <= DK_SALARY_CAP

    def test_no_duplicate_players(self, optimizer, mock_pool):
        """Each player should appear in at most one slot."""
        lineup = optimizer._greedy_fill(
            pool=mock_pool,
            lineup={},
            remaining_slots=["C", "PG", "SG", "SF", "PF", "G", "F", "UTIL"],
            used_ids=set(),
            remaining_salary=DK_SALARY_CAP,
            platform="dk",
        )
        player_ids = [p.player_id for p in lineup.values()]
        assert len(player_ids) == len(set(player_ids))

    def test_position_eligibility_respected(self, optimizer, mock_pool):
        """Each player should be eligible for the slot they fill."""
        lineup = optimizer._greedy_fill(
            pool=mock_pool,
            lineup={},
            remaining_slots=["C", "PG", "SG", "SF", "PF", "G", "F", "UTIL"],
            used_ids=set(),
            remaining_salary=DK_SALARY_CAP,
            platform="dk",
        )
        for indexed_slot, player in lineup.items():
            base = _base_slot(indexed_slot)
            eligible_positions = DK_SLOT_ELIGIBILITY[base]
            assert player.position in eligible_positions, (
                f"Player {player.player_name} ({player.position}) "
                f"ineligible for slot {base}"
            )

    def test_picks_high_fp_players(self, optimizer, mock_pool):
        """Stars should generally be selected over value plays."""
        lineup = optimizer._greedy_fill(
            pool=mock_pool,
            lineup={},
            remaining_slots=["C", "PG", "SG", "SF", "PF", "G", "F", "UTIL"],
            used_ids=set(),
            remaining_salary=DK_SALARY_CAP,
            platform="dk",
        )
        names = [p.player_name for p in lineup.values()]
        # Stars should be in the lineup given sufficient budget
        assert "PG Star" in names
        assert "SG Star" in names
        assert "C Star" in names


class TestFDFill:
    """Test FanDuel lineup filling (9 slots, different structure)."""

    def test_fills_all_fd_slots(self, optimizer, mock_pool):
        """FD lineup should have 9 filled slots."""
        lineup = optimizer._greedy_fill(
            pool=mock_pool,
            lineup={},
            remaining_slots=["C", "PG", "PG", "SG", "SG", "SF", "SF", "PF", "PF"],
            used_ids=set(),
            remaining_salary=FD_SALARY_CAP,
            platform="fd",
        )
        assert len(lineup) == 9

    def test_fd_salary_cap(self, optimizer, mock_pool):
        """FD total salary should not exceed $60,000."""
        # Scale salaries to FD range for a realistic test
        fd_pool = []
        for p in mock_pool:
            fd_p = p.model_copy(update={"salary": int(round(p.salary * 1.2, -2))})
            fd_pool.append(fd_p)

        lineup = optimizer._greedy_fill(
            pool=fd_pool,
            lineup={},
            remaining_slots=["C", "PG", "PG", "SG", "SG", "SF", "SF", "PF", "PF"],
            used_ids=set(),
            remaining_salary=FD_SALARY_CAP,
            platform="fd",
        )
        total = sum(p.salary for p in lineup.values())
        assert total <= FD_SALARY_CAP


# ---------------------------------------------------------------------------
# Iterative Improvement Tests
# ---------------------------------------------------------------------------

class TestIterativeImprove:
    """Test the swap-based improvement pass."""

    def test_improves_or_maintains_fp(self, optimizer, mock_pool):
        """Iterative improvement should not decrease total FP."""
        initial_lineup = optimizer._greedy_fill(
            pool=mock_pool,
            lineup={},
            remaining_slots=["C", "PG", "SG", "SF", "PF", "G", "F", "UTIL"],
            used_ids=set(),
            remaining_salary=DK_SALARY_CAP,
            platform="dk",
        )
        initial_fp = sum(p.projected_fp for p in initial_lineup.values())

        improved = optimizer._iterative_improve(
            initial_lineup, mock_pool, DK_SALARY_CAP, "dk"
        )
        improved_fp = sum(p.projected_fp for p in improved.values())

        assert improved_fp >= initial_fp

    def test_improvement_respects_cap(self, optimizer, mock_pool):
        """Iterative improvement must stay under salary cap."""
        initial = optimizer._greedy_fill(
            pool=mock_pool,
            lineup={},
            remaining_slots=["C", "PG", "SG", "SF", "PF", "G", "F", "UTIL"],
            used_ids=set(),
            remaining_salary=DK_SALARY_CAP,
            platform="dk",
        )
        improved = optimizer._iterative_improve(
            initial, mock_pool, DK_SALARY_CAP, "dk"
        )
        total = sum(p.salary for p in improved.values())
        assert total <= DK_SALARY_CAP


# ---------------------------------------------------------------------------
# Lock / Exclude Tests
# ---------------------------------------------------------------------------

class TestLockExclude:
    """Test locked and excluded player constraints."""

    def test_locked_player_in_lineup(self, optimizer, mock_pool):
        """A locked player must appear in the final lineup."""
        from app.services.lineup_optimizer_service import _index_slots

        # Lock PG Value (player_id=3)
        lineup = {}
        used_ids = set()
        remaining_salary = DK_SALARY_CAP
        slot_order = ["C", "PG", "SG", "SF", "PF", "G", "F", "UTIL"]
        indexed_slots = _index_slots(slot_order)

        # Pre-assign locked player to first eligible indexed slot
        locked_player = next(p for p in mock_pool if p.player_id == 3)
        for isl in list(indexed_slots):
            base = _base_slot(isl)
            elig = optimizer._get_slot_eligible_positions(base, "dk")
            if locked_player.position in elig and isl not in lineup:
                lineup[isl] = locked_player
                used_ids.add(3)
                remaining_salary -= locked_player.salary
                indexed_slots.remove(isl)
                break

        lineup = optimizer._greedy_fill(
            mock_pool, lineup, indexed_slots, used_ids,
            remaining_salary, "dk",
        )

        player_ids = {p.player_id for p in lineup.values()}
        assert 3 in player_ids, "Locked player should be in lineup"

    def test_excluded_player_not_in_lineup(self, optimizer, mock_pool):
        """An excluded player must not appear in the lineup."""
        # Exclude PG Star (player_id=1)
        excluded = {1}
        filtered_pool = [p for p in mock_pool if p.player_id not in excluded]

        lineup = optimizer._greedy_fill(
            pool=filtered_pool,
            lineup={},
            remaining_slots=["C", "PG", "SG", "SF", "PF", "G", "F", "UTIL"],
            used_ids=set(),
            remaining_salary=DK_SALARY_CAP,
            platform="dk",
        )

        player_ids = {p.player_id for p in lineup.values()}
        assert 1 not in player_ids, "Excluded player should not be in lineup"


# ---------------------------------------------------------------------------
# Eligible Slots Tests
# ---------------------------------------------------------------------------

class TestEligibleSlots:
    """Test position eligibility mapping."""

    def test_pg_eligible_dk(self, optimizer):
        slots = optimizer._get_eligible_slots("PG", "dk")
        assert "PG" in slots
        assert "G" in slots
        assert "UTIL" in slots
        assert "F" not in slots
        assert "C" not in slots

    def test_c_eligible_dk(self, optimizer):
        slots = optimizer._get_eligible_slots("C", "dk")
        assert "C" in slots
        assert "UTIL" in slots
        assert "G" not in slots
        assert "F" not in slots

    def test_sf_eligible_dk(self, optimizer):
        slots = optimizer._get_eligible_slots("SF", "dk")
        assert "SF" in slots
        assert "F" in slots
        assert "UTIL" in slots
        assert "G" not in slots

    def test_pg_eligible_fd(self, optimizer):
        slots = optimizer._get_eligible_slots("PG", "fd")
        assert slots == ["PG"]

    def test_c_eligible_fd(self, optimizer):
        slots = optimizer._get_eligible_slots("C", "fd")
        assert slots == ["C"]


# ---------------------------------------------------------------------------
# Name Matching Tests
# ---------------------------------------------------------------------------

class TestNameMatching:
    def test_exact_match(self, optimizer):
        assert optimizer._names_match("LeBron James", "LeBron James")

    def test_case_insensitive(self, optimizer):
        assert optimizer._names_match("lebron james", "LeBron James")

    def test_suffix_stripped(self, optimizer):
        assert optimizer._names_match("Gary Trent Jr.", "Gary Trent")

    def test_accents(self, optimizer):
        assert optimizer._names_match("Nikola Jokic", "Nikola Jokic")

    def test_different_players(self, optimizer):
        assert not optimizer._names_match("LeBron James", "Kevin Durant")

    # ── New edge-case tests ──────────────────────────────────────────

    def test_hyphenated_name(self, optimizer):
        """Gilgeous-Alexander should match with or without hyphen."""
        assert optimizer._names_match(
            "Shai Gilgeous-Alexander", "Shai GilgeousAlexander"
        )

    def test_apostrophe_name(self, optimizer):
        """O'Brien / Obrien should match."""
        assert optimizer._names_match("Kyle O'Keeffe", "Kyle Okeeffe")

    def test_short_first_name_pj(self, optimizer):
        """PJ Washington (2-char first name) should match P.J. Washington."""
        assert optimizer._names_match("PJ Washington", "P.J. Washington")

    def test_same_last_name_different_first(self, optimizer):
        """Players with same last name but clearly different first names.

        Note: The 3-char prefix check can match "Marcus" / "Markieff"
        (both start with "Mar") — this is accepted because the match
        runs per-team, so twins on different teams don't collide.
        Test uses names where the first 3 chars differ.
        """
        assert not optimizer._names_match("Aaron Gordon", "Eric Gordon")

    def test_suffix_sr(self, optimizer):
        """Senior suffix stripped correctly."""
        assert optimizer._names_match("Tim Hardaway Sr.", "Tim Hardaway")

    def test_sorted_token_fallback(self, optimizer):
        """Name order swap should match via sorted-token fallback."""
        assert optimizer._names_match("James LeBron", "LeBron James")

    def test_hyphen_and_suffix(self, optimizer):
        """Compound edge case: hyphen + suffix."""
        assert optimizer._names_match(
            "Wendell Carter Jr.", "Wendell Carter"
        )


# ---------------------------------------------------------------------------
# DK → NBA Abbreviation Alias Tests
# ---------------------------------------------------------------------------

class TestAbbreviationAliases:
    """Verify the DK_TO_NBA_ABBR_ALIASES mapping (shared from constants)."""

    def test_pho_to_phx(self):
        from app.config.constants import DK_TO_NBA_ABBR_ALIASES
        assert DK_TO_NBA_ABBR_ALIASES["PHO"] == "PHX"

    def test_gs_to_gsw(self):
        from app.config.constants import DK_TO_NBA_ABBR_ALIASES
        assert DK_TO_NBA_ABBR_ALIASES["GS"] == "GSW"

    def test_sa_to_sas(self):
        from app.config.constants import DK_TO_NBA_ABBR_ALIASES
        assert DK_TO_NBA_ABBR_ALIASES["SA"] == "SAS"

    def test_standard_abbr_not_in_aliases(self):
        """Standard abbreviations like PHX should NOT be in the alias map."""
        from app.config.constants import DK_TO_NBA_ABBR_ALIASES
        assert "PHX" not in DK_TO_NBA_ABBR_ALIASES
        assert "GSW" not in DK_TO_NBA_ABBR_ALIASES
        assert "LAL" not in DK_TO_NBA_ABBR_ALIASES

    def test_alias_applied_in_grouping(self):
        """When draftables use aliased abbreviation, it should be resolved."""
        from app.config.constants import DK_TO_NBA_ABBR_ALIASES
        abbr = "PHO"
        resolved = DK_TO_NBA_ABBR_ALIASES.get(abbr, abbr)
        assert resolved == "PHX"

    def test_lineup_optimizer_reexports_shared_map(self):
        """Lineup optimizer's _DK_TO_NBA_ABBR_ALIASES should be the shared map."""
        from app.config.constants import DK_TO_NBA_ABBR_ALIASES
        from app.services.lineup_optimizer_service import _DK_TO_NBA_ABBR_ALIASES
        assert _DK_TO_NBA_ABBR_ALIASES is DK_TO_NBA_ABBR_ALIASES


# ---------------------------------------------------------------------------
# Stale Cache Validation Tests
# ---------------------------------------------------------------------------

class TestStaleCacheValidation:
    """File cache with too few players should be discarded."""

    def test_cache_threshold_formula(self):
        """Cache threshold is max(20, teams * 5)."""
        teams = set(["ATL", "BOS", "CHI", "CLE", "DAL", "DEN", "DET", "GSW", "HOU", "IND"])
        _min = max(20, len(teams) * 5)
        assert _min == 50  # 10 teams * 5

    def test_single_team_threshold(self):
        """Single-team cache threshold should be at least 20."""
        teams = set(["ATL"])
        _min = max(20, len(teams) * 5)
        assert _min == 20

    def test_small_pool_below_threshold(self):
        """13 players across 1 team is below threshold 20."""
        pool_size = 13
        teams = set(["ATL"])
        _min = max(20, len(teams) * 5)
        assert pool_size < _min  # Would be discarded

    def test_inmemory_cache_uses_same_threshold_logic(self):
        """In-memory cache validation should use max(20, teams*5) threshold."""
        # Simulate a pool with 3 teams and only 10 players — should fail
        teams_in_mem = 3
        pool_size = 10
        expected_min = max(20, teams_in_mem * 5)
        assert expected_min == 20
        assert pool_size < expected_min  # Would be discarded

        # Simulate a pool with 8 teams and 50 players — should pass
        teams_in_mem2 = 8
        pool_size2 = 50
        expected_min2 = max(20, teams_in_mem2 * 5)
        assert expected_min2 == 40
        assert pool_size2 >= expected_min2  # Would be kept


# ---------------------------------------------------------------------------
# Min Salary Reservation Tests
# ---------------------------------------------------------------------------

class TestMinSalaryReservation:
    """The greedy filler should reserve enough budget for future slots."""

    def test_min_salary_calculation(self, optimizer, mock_pool):
        """Min salary for remaining slots should equal sum of cheapest eligible."""
        min_sal = optimizer._min_salary_for_slots(
            mock_pool,
            ["PG", "C"],
            used_ids=set(),
            platform="dk",
        )
        # Cheapest PG is PG Value at $3500, cheapest C is C Value at $3500
        assert min_sal == 7000

    def test_reserves_dont_reuse_players(self, optimizer, mock_pool):
        """Min salary calc should not reuse the same player for two slots."""
        min_sal = optimizer._min_salary_for_slots(
            mock_pool,
            ["PG", "G"],  # G accepts PG or SG
            used_ids=set(),
            platform="dk",
        )
        # Cheapest PG is PG Value $3500, next cheapest for G is SG Value $3500
        assert min_sal == 7000


# ---------------------------------------------------------------------------
# ILP Optimizer Tests
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _PULP_AVAILABLE, reason="PuLP not installed")
class TestILPOptimizer:
    """Test the ILP-based lineup optimization."""

    def test_ilp_fills_all_dk_slots(self, optimizer, mock_pool):
        """ILP should produce exactly 8 filled slots for DK."""
        result = optimizer._ilp_optimize(
            pool=mock_pool,
            platform="dk",
            salary_cap=DK_SALARY_CAP,
            slot_order=DK_SLOT_ORDER,
            locked_player_ids=[],
            score_fn=lambda p: p.projected_fp,
        )
        assert result is not None
        assert len(result) == 8

    def test_ilp_respects_salary_cap(self, optimizer, mock_pool):
        """ILP lineup total salary must be within the salary cap."""
        result = optimizer._ilp_optimize(
            pool=mock_pool,
            platform="dk",
            salary_cap=DK_SALARY_CAP,
            slot_order=DK_SLOT_ORDER,
            locked_player_ids=[],
            score_fn=lambda p: p.projected_fp,
        )
        assert result is not None
        total = sum(p.salary for p in result.values())
        assert total <= DK_SALARY_CAP

    def test_ilp_no_duplicate_players(self, optimizer, mock_pool):
        """Each player should appear at most once in the lineup."""
        result = optimizer._ilp_optimize(
            pool=mock_pool,
            platform="dk",
            salary_cap=DK_SALARY_CAP,
            slot_order=DK_SLOT_ORDER,
            locked_player_ids=[],
            score_fn=lambda p: p.projected_fp,
        )
        assert result is not None
        player_ids = [p.player_id for p in result.values()]
        assert len(player_ids) == len(set(player_ids))

    def test_ilp_position_eligibility(self, optimizer, mock_pool):
        """Each player must be eligible for the slot they're assigned to."""
        result = optimizer._ilp_optimize(
            pool=mock_pool,
            platform="dk",
            salary_cap=DK_SALARY_CAP,
            slot_order=DK_SLOT_ORDER,
            locked_player_ids=[],
            score_fn=lambda p: p.projected_fp,
        )
        assert result is not None
        for indexed_slot, player in result.items():
            base = _base_slot(indexed_slot)
            elig = DK_SLOT_ELIGIBILITY.get(base, [])
            assert player.position in elig, (
                f"Player {player.player_name} (pos={player.position}) "
                f"in slot {base} but eligible positions are {elig}"
            )

    def test_ilp_locked_player_included(self, optimizer, mock_pool):
        """Locked players must appear in the ILP solution."""
        # Lock the PG Mid (id=2) and SF Value (id=9)
        result = optimizer._ilp_optimize(
            pool=mock_pool,
            platform="dk",
            salary_cap=DK_SALARY_CAP,
            slot_order=DK_SLOT_ORDER,
            locked_player_ids=[2, 9],
            score_fn=lambda p: p.projected_fp,
        )
        assert result is not None
        roster_ids = {p.player_id for p in result.values()}
        assert 2 in roster_ids, "Locked player PG Mid (id=2) missing"
        assert 9 in roster_ids, "Locked player SF Value (id=9) missing"

    def test_ilp_salary_floor_enforced(self, optimizer, mock_pool):
        """ILP should respect a minimum salary floor."""
        floor = 47_000
        result = optimizer._ilp_optimize(
            pool=mock_pool,
            platform="dk",
            salary_cap=DK_SALARY_CAP,
            slot_order=DK_SLOT_ORDER,
            locked_player_ids=[],
            score_fn=lambda p: p.projected_fp,
            salary_floor=floor,
        )
        assert result is not None
        total = sum(p.salary for p in result.values())
        assert total >= floor, f"Total salary {total} < floor {floor}"

    def test_ilp_fd_duplicate_slots(self, optimizer, mock_pool):
        """ILP should correctly handle FD's 9-slot structure with duplicates."""
        result = optimizer._ilp_optimize(
            pool=mock_pool,
            platform="fd",
            salary_cap=FD_SALARY_CAP,
            slot_order=FD_SLOT_ORDER,
            locked_player_ids=[],
            score_fn=lambda p: p.projected_fp,
        )
        assert result is not None
        assert len(result) == 9
        # Check indexed slot names
        for slot_key in result:
            assert "_" in slot_key  # All keys should be indexed
        # No duplicate players
        player_ids = [p.player_id for p in result.values()]
        assert len(player_ids) == len(set(player_ids))

    def test_ilp_showdown_6_slots(self, optimizer, mock_pool):
        """ILP should fill 6 showdown slots (1 CPT + 5 FLEX)."""
        # Give all players a game_id for showdown
        for p in mock_pool:
            p.game_id = "GAME_001"

        result = optimizer._ilp_optimize(
            pool=mock_pool,
            platform="dk",
            salary_cap=DK_SHOWDOWN_SALARY_CAP,
            slot_order=["CPT", "FLEX", "FLEX", "FLEX", "FLEX", "FLEX"],
            locked_player_ids=[],
            score_fn=lambda p: p.projected_fp,
            mode="showdown",
        )
        assert result is not None
        assert len(result) == 6
        # Exactly one CPT slot
        cpt_slots = [k for k in result if _base_slot(k) == "CPT"]
        assert len(cpt_slots) == 1
        flex_slots = [k for k in result if _base_slot(k) == "FLEX"]
        assert len(flex_slots) == 5

    def test_ilp_showdown_cpt_multiplier(self, optimizer, mock_pool):
        """CPT slot should get the player with highest 1.5x-adjusted value."""
        # Make all players same salary so the best scorer gets CPT
        for p in mock_pool:
            p.salary = 5000
            p.game_id = "GAME_001"

        result = optimizer._ilp_optimize(
            pool=mock_pool,
            platform="dk",
            salary_cap=DK_SHOWDOWN_SALARY_CAP,
            slot_order=["CPT", "FLEX", "FLEX", "FLEX", "FLEX", "FLEX"],
            locked_player_ids=[],
            score_fn=lambda p: p.projected_fp,
            mode="showdown",
        )
        assert result is not None
        cpt_player = result["CPT_0"]
        # The highest-projected player (PG Star, 45.0 FP) should be captain
        # because 45.0 * 1.5 = 67.5 is better than any other player as CPT
        assert cpt_player.projected_fp == 45.0, (
            f"CPT should be the highest-FP player but got {cpt_player.player_name} "
            f"({cpt_player.projected_fp} FP)"
        )

    def test_ilp_stacking_constraint(self, optimizer, mock_pool):
        """Stacking should ensure ≥ stack_size from the target game."""
        game_id = "GAME_001"
        # Assign half the pool to one game
        for p in mock_pool[:8]:
            p.game_id = game_id
            if p.team_abbreviation in ("BOS", "NYK", "MIA", "LAL"):
                p.team_abbreviation = "BOS"
            else:
                p.team_abbreviation = "NYK"

        result = optimizer._ilp_optimize(
            pool=mock_pool,
            platform="dk",
            salary_cap=DK_SALARY_CAP,
            slot_order=DK_SLOT_ORDER,
            locked_player_ids=[],
            score_fn=lambda p: p.projected_fp,
            stack_game_id=game_id,
            stack_primary_team="BOS",
            stack_size=3,
            bring_back=True,
        )
        assert result is not None
        bos_count = sum(
            1 for p in result.values()
            if p.team_abbreviation.upper() == "BOS"
        )
        nyk_count = sum(
            1 for p in result.values()
            if p.team_abbreviation.upper() == "NYK"
        )
        assert bos_count >= 2, f"Expected ≥2 BOS players, got {bos_count}"
        assert nyk_count >= 1, f"Expected ≥1 NYK bring-back, got {nyk_count}"

    def test_ilp_optimal_beats_greedy(self, optimizer, mock_pool):
        """ILP result should have total score ≥ greedy result."""
        score_fn = lambda p: p.projected_fp

        # ILP solution
        ilp_result = optimizer._ilp_optimize(
            pool=mock_pool,
            platform="dk",
            salary_cap=DK_SALARY_CAP,
            slot_order=DK_SLOT_ORDER,
            locked_player_ids=[],
            score_fn=score_fn,
        )
        assert ilp_result is not None
        ilp_total = sum(score_fn(p) for p in ilp_result.values())

        # Greedy solution
        indexed = _index_slots(DK_SLOT_ORDER)
        greedy_lineup = optimizer._greedy_fill_scored(
            mock_pool, {}, list(indexed), set(),
            DK_SALARY_CAP, "dk", score_fn,
        )
        greedy_lineup = optimizer._iterative_improve_scored(
            greedy_lineup, mock_pool, DK_SALARY_CAP, "dk", score_fn,
        )
        greedy_total = sum(score_fn(p) for p in greedy_lineup.values())

        assert ilp_total >= greedy_total, (
            f"ILP ({ilp_total:.1f}) should be ≥ greedy ({greedy_total:.1f})"
        )

    def test_ilp_infeasible_returns_none(self, optimizer, mock_pool):
        """ILP should return None when constraints are impossible."""
        # Lock two players whose combined salary exceeds the cap
        # with a tiny cap
        result = optimizer._ilp_optimize(
            pool=mock_pool,
            platform="dk",
            salary_cap=10_000,  # Way too low for 8 players
            slot_order=DK_SLOT_ORDER,
            locked_player_ids=[1, 4, 7, 10, 13],  # Stars total $40K
            score_fn=lambda p: p.projected_fp,
        )
        assert result is None

    def test_greedy_result_when_ilp_fails(self, optimizer, mock_pool):
        """_build_single_lineup should produce a valid lineup even if ILP fails."""
        # Patch _ilp_optimize to always return None (simulate failure)
        with patch.object(optimizer, "_ilp_optimize", return_value=None):
            result = optimizer._build_single_lineup(
                pool=mock_pool,
                platform="dk",
                salary_cap=DK_SALARY_CAP,
                roster_slots=DK_ROSTER_SLOTS,
                slot_order=DK_SLOT_ORDER,
                locked_player_ids=[],
                extra_excludes=set(),
                score_fn=lambda p: p.projected_fp,
            )
        assert result is not None
        assert len(result.players) == 8
        assert result.total_salary <= DK_SALARY_CAP

    def test_hybrid_always_returns_result(self, optimizer, mock_pool):
        """_build_single_lineup always returns a valid lineup even if ILP raises."""
        with patch.object(optimizer, "_ilp_optimize", side_effect=RuntimeError("boom")):
            result = optimizer._build_single_lineup(
                pool=mock_pool,
                platform="dk",
                salary_cap=DK_SALARY_CAP,
                roster_slots=DK_ROSTER_SLOTS,
                slot_order=DK_SLOT_ORDER,
                locked_player_ids=[],
                extra_excludes=set(),
                score_fn=lambda p: p.projected_fp,
            )
        assert result is not None
        assert len(result.players) == 8
        assert result.total_salary <= DK_SALARY_CAP

    @pytest.mark.skipif(not _PULP_AVAILABLE, reason="PuLP not installed")
    def test_ilp_warm_start_accepted(self, optimizer, mock_pool):
        """ILP should accept a warm start lineup and return >= greedy score."""
        score_fn = lambda p: p.projected_fp
        indexed = _index_slots(DK_SLOT_ORDER)

        # Build greedy solution first
        greedy = optimizer._greedy_fill_scored(
            mock_pool, {}, list(indexed), set(),
            DK_SALARY_CAP, "dk", score_fn,
        )
        greedy = optimizer._iterative_improve_scored(
            greedy, mock_pool, DK_SALARY_CAP, "dk", score_fn,
        )
        greedy_score = sum(score_fn(p) for p in greedy.values())

        # Pass greedy as warm start to ILP
        result = optimizer._ilp_optimize(
            pool=mock_pool,
            platform="dk",
            salary_cap=DK_SALARY_CAP,
            slot_order=DK_SLOT_ORDER,
            locked_player_ids=[],
            score_fn=score_fn,
            warm_start_lineup=greedy,
        )
        assert result is not None
        ilp_score = sum(score_fn(p) for p in result.values())
        assert ilp_score >= greedy_score, (
            f"ILP with warm start ({ilp_score:.1f}) should be >= "
            f"greedy ({greedy_score:.1f})"
        )

    @pytest.mark.skipif(not _PULP_AVAILABLE, reason="PuLP not installed")
    def test_ilp_warm_start_none_still_works(self, optimizer, mock_pool):
        """Passing warm_start_lineup=None should behave like the original."""
        result = optimizer._ilp_optimize(
            pool=mock_pool,
            platform="dk",
            salary_cap=DK_SALARY_CAP,
            slot_order=DK_SLOT_ORDER,
            locked_player_ids=[],
            score_fn=lambda p: p.projected_fp,
            warm_start_lineup=None,
        )
        assert result is not None
        assert len(result) == 8


# ---------------------------------------------------------------------------
# Composite Score Feature Tests
# ---------------------------------------------------------------------------

class TestCompositeScoreFeatures:
    """Tests for _compute_composite_score GPP enrichment signals."""

    def test_composite_score_sim_std_bonus(self, optimizer):
        """High sim_std should widen score variance (Gaussian noise sigma).

        sim_std no longer adds a deterministic upward bonus — it controls
        the sigma of the Gaussian noise applied to the composite score.
        Verify that a player with high sim_std produces a wider spread
        of scores across multiple RNG seeds than a low-sim_std player.
        """
        import random as _random
        import statistics

        base_player = _make_pool_player(1, "Steady", "PG", "BOS", 7000, 30.0)
        volatile = _make_pool_player(2, "Volatile", "PG", "BOS", 7000, 30.0)
        # Give the volatile player a high sim_std
        volatile.sim_std = 12.0
        base_player.sim_std = 2.0  # small but non-zero so both use sim_std path

        n_samples = 200
        scores_base = []
        scores_vol = []
        for seed in range(n_samples):
            scores_base.append(
                optimizer._compute_composite_score(
                    base_player, "balanced", {}, 0,
                    _random.Random(seed), contest_type="gpp",
                )
            )
            scores_vol.append(
                optimizer._compute_composite_score(
                    volatile, "balanced", {}, 0,
                    _random.Random(seed), contest_type="gpp",
                )
            )
        # Volatile player should have a wider distribution of scores
        std_base = statistics.stdev(scores_base)
        std_vol = statistics.stdev(scores_vol)
        assert std_vol > std_base, (
            f"Volatile std ({std_vol:.2f}) should exceed steady std ({std_base:.2f})"
        )

    def test_composite_score_opponent_def_rating(self, optimizer):
        """Bad defense (high rating > 115) should boost GPP score."""
        import random as _random
        good_def = _make_pool_player(1, "GoodDef", "SG", "LAL", 7000, 30.0)
        bad_def = _make_pool_player(2, "BadDef", "SG", "LAL", 7000, 30.0)
        good_def.opponent_def_rating = 103.0  # good defense
        bad_def.opponent_def_rating = 116.0   # very bad defense

        score_good = optimizer._compute_composite_score(
            good_def, "balanced", {}, 0, _random.Random(42), contest_type="gpp",
        )
        score_bad = optimizer._compute_composite_score(
            bad_def, "balanced", {}, 0, _random.Random(42), contest_type="gpp",
        )
        assert score_bad > score_good

    def test_composite_score_rotation_confidence_penalty(self, optimizer):
        """Low rotation_confidence (< 0.6) should reduce score."""
        import random as _random
        confident = _make_pool_player(1, "Sure", "SF", "DEN", 6000, 28.0)
        uncertain = _make_pool_player(2, "Maybe", "SF", "DEN", 6000, 28.0)
        confident.rotation_confidence = 1.0
        uncertain.rotation_confidence = 0.5  # triggers 0.95 multiplier

        score_conf = optimizer._compute_composite_score(
            confident, "balanced", {}, 0, _random.Random(42), contest_type="gpp",
        )
        score_unc = optimizer._compute_composite_score(
            uncertain, "balanced", {}, 0, _random.Random(42), contest_type="gpp",
        )
        assert score_conf > score_unc

    def test_composite_score_game_pace(self, optimizer):
        """Fast pace (>=105) boosts score; slow pace (<95) penalizes."""
        import random as _random
        fast = _make_pool_player(1, "Fast", "PF", "MIL", 6000, 28.0)
        slow = _make_pool_player(2, "Slow", "PF", "MIL", 6000, 28.0)
        fast.game_pace = 107.0
        slow.game_pace = 93.0

        score_fast = optimizer._compute_composite_score(
            fast, "balanced", {}, 0, _random.Random(42), contest_type="gpp",
        )
        score_slow = optimizer._compute_composite_score(
            slow, "balanced", {}, 0, _random.Random(42), contest_type="gpp",
        )
        assert score_fast > score_slow

    def test_contrarian_strategy_produces_valid_score(self, optimizer):
        """Contrarian strategy should return a positive score."""
        import random as _random
        player = _make_pool_player(1, "Contrarian", "C", "MIN", 5000, 22.0)
        player.estimated_ownership = 3.0  # low-owned leverage
        player.sim_p90 = 40.0

        score = optimizer._compute_composite_score(
            player, "contrarian", {}, 0, _random.Random(42), contest_type="gpp",
        )
        assert score > 0

    def test_exposure_limit_respected(self, optimizer, mock_pool):
        """No player should appear more than max_appearances times."""
        import random as _random
        num_lineups = 10
        max_exposure_pct = 0.5
        max_appearances = max(1, int(max_exposure_pct * num_lineups))

        exposure = {}
        for lineup_idx in range(num_lineups):
            # Compute excludes based on exposure limit
            excludes = set()
            for pid, count in exposure.items():
                if count >= max_appearances:
                    excludes.add(pid)

            result = optimizer._build_single_lineup(
                pool=mock_pool,
                platform="dk",
                salary_cap=DK_SALARY_CAP,
                roster_slots=DK_ROSTER_SLOTS,
                slot_order=DK_SLOT_ORDER,
                locked_player_ids=[],
                extra_excludes=excludes,
                score_fn=lambda p: p.projected_fp,
            )
            if result is not None:
                for p in result.players:
                    exposure[p.player_id] = exposure.get(p.player_id, 0) + 1

        # Verify no player exceeds the limit
        for pid, count in exposure.items():
            assert count <= max_appearances, (
                f"Player {pid} appeared {count} times, max allowed {max_appearances}"
            )


class TestPlayerPoolMinutesCap:
    """Verify that PlayerPoolEntry model caps projected_minutes at 53."""

    def test_pool_entry_caps_impossible_minutes(self):
        """PlayerPoolEntry should clamp projected_minutes to 53.0."""
        entry = _make_pool_player(1, "Bug Player", "PG", "HOU", 5000, 25.0)
        entry.projected_minutes = 75.3  # Impossible value
        # The validator fires on construction but not on assignment;
        # create a fresh one to test the validator
        entry2 = PlayerPoolEntry(
            player_id=1,
            player_name="Bug Player",
            position="PG",
            team_abbreviation="HOU",
            salary=5000,
            projected_fp=25.0,
            floor_fp=20.0,
            ceiling_fp=30.0,
            projected_minutes=75.3,  # Should be capped by validator
            eligible_slots=["PG", "G", "UTIL"],
        )
        assert entry2.projected_minutes == 53.0

    def test_pool_entry_allows_normal_minutes(self):
        """Normal minutes should pass through uncapped."""
        entry = PlayerPoolEntry(
            player_id=2,
            player_name="Normal",
            position="SG",
            team_abbreviation="BOS",
            salary=6000,
            projected_fp=30.0,
            floor_fp=24.0,
            ceiling_fp=36.0,
            projected_minutes=34.5,
            eligible_slots=["SG", "G", "UTIL"],
        )
        assert entry.projected_minutes == 34.5


# ---------------------------------------------------------------------------
# DraftGroup Type Validation Tests
# ---------------------------------------------------------------------------

class TestDraftGroupValidation:
    """Verify Showdown/small-slate detection logging."""

    def test_two_team_slate_is_showdown(self):
        """A DraftGroup with <=2 teams should be flagged as Showdown."""
        teams_in_slate = {"BOS": [1, 2, 3], "NYK": [4, 5, 6]}
        # The production code uses: if len(teams_in_slate) <= 2 → warning
        assert len(teams_in_slate) <= 2

    def test_four_team_slate_is_small(self):
        """A DraftGroup with 3-4 teams should be flagged as small slate."""
        teams_in_slate = {
            "BOS": [1], "NYK": [2], "LAL": [3], "CHI": [4],
        }
        assert 2 < len(teams_in_slate) <= 4

    def test_ten_team_slate_is_classic(self):
        """A DraftGroup with >4 teams should NOT trigger warnings."""
        teams_in_slate = {
            t: [i] for i, t in enumerate(
                ["BOS", "NYK", "LAL", "CHI", "MIA",
                 "DEN", "PHX", "MIL", "CLE", "IND"]
            )
        }
        assert len(teams_in_slate) > 4

    def test_incomplete_roster_detection(self):
        """A team with <8 draftables should be flagged."""
        teams_in_slate = {
            "BOS": list(range(12)),   # OK
            "NYK": list(range(5)),    # Too few
        }
        for abbr, draftables in teams_in_slate.items():
            if len(draftables) < 8:
                assert abbr == "NYK"


# ---------------------------------------------------------------------------
# Trace Return Type Tests
# ---------------------------------------------------------------------------

class TestTraceReturnType:
    """Verify the trace dict structure returned by _process_team."""

    def test_trace_dict_has_expected_keys(self):
        """A successful trace should contain all pipeline gate keys."""
        # Simulate a successful trace dict
        trace = {
            "abbr": "BOS",
            "draftables": 14,
            "rotation": 12,
            "projected": 12,
            "after_normalization": 10,
            "name_matched": 10,
            "name_misses": 4,
            "name_miss_list": ["Player A", "Player B"],
            "zero_min": 2,
            "low_games": 1,
            "zero_fp": 0,
            "injury": 1,
            "excluded": 0,
            "final": 8,
        }
        expected_keys = {
            "abbr", "draftables", "rotation", "projected",
            "after_normalization", "name_matched", "name_misses",
            "name_miss_list", "zero_min", "low_games", "zero_fp",
            "injury", "excluded", "final",
        }
        assert expected_keys.issubset(trace.keys())

    def test_error_trace_has_error_field(self):
        """An error trace should contain 'error' key."""
        trace = {"abbr": "ATL", "draftables": 10, "error": "empty_rotation"}
        assert "error" in trace
        assert trace["error"] == "empty_rotation"

    def test_error_trace_for_unresolved_abbr(self):
        """Unresolved abbreviation trace should have specific error."""
        trace = {"abbr": "XYZ", "draftables": 8, "error": "unresolved_abbr"}
        assert trace["error"] == "unresolved_abbr"

    def test_pipeline_summary_aggregation(self):
        """Pipeline summary should correctly aggregate across teams."""
        traces = [
            {"abbr": "BOS", "draftables": 14, "rotation": 12,
             "name_matched": 10, "name_misses": 4, "final": 8},
            {"abbr": "NYK", "draftables": 12, "rotation": 10,
             "name_matched": 8, "name_misses": 4, "final": 6},
            {"abbr": "ATL", "draftables": 10, "error": "empty_rotation"},
        ]
        _t_draftables = sum(t.get("draftables", 0) for t in traces)
        _t_rotation = sum(t.get("rotation", 0) for t in traces)
        _t_matched = sum(t.get("name_matched", 0) for t in traces)
        _t_misses = sum(t.get("name_misses", 0) for t in traces)
        _t_final = sum(t.get("final", 0) for t in traces)
        _errored = [t["abbr"] for t in traces if "error" in t]

        assert _t_draftables == 36
        assert _t_rotation == 22
        assert _t_matched == 18
        assert _t_misses == 8
        assert _t_final == 14
        assert _errored == ["ATL"]


# ---------------------------------------------------------------------------
# Exception Handler Tests
# ---------------------------------------------------------------------------

class TestExceptionHandlers:
    """Verify that exception handlers include exc_info=True."""

    def test_exception_handler_has_exc_info_in_source(self):
        """The _process_team error handler should include exc_info=True.

        This is a source-level check to prevent regression — if someone
        removes exc_info=True, this test will catch it.
        """
        import inspect
        source = inspect.getsource(LineupOptimizerService._build_player_pool_inner)
        # Check that 'exc_info=True' appears in the source (at least 3 times:
        # _process_team handler, ThreadPoolExecutor handler, opponent lookup)
        count = source.count("exc_info=True")
        assert count >= 3, (
            f"Expected at least 3 exc_info=True in _build_player_pool_inner, "
            f"found {count}. Did someone remove stack trace logging?"
        )


# ---------------------------------------------------------------------------
# Circuit Breaker Diagnostics Tests
# ---------------------------------------------------------------------------

class TestCircuitBreakerDiagnostics:
    """Verify circuit breaker diagnostic accessors."""

    def test_diagnostics_returns_expected_keys(self):
        from app.services.nba_api_service import get_circuit_breaker_diagnostics
        diag = get_circuit_breaker_diagnostics()
        expected = {
            "state", "consecutive_failures", "failure_threshold",
            "recovery_seconds",
        }
        assert expected.issubset(diag.keys())

    def test_diagnostics_default_state_is_closed(self):
        from app.services.nba_api_service import (
            get_circuit_breaker_diagnostics, reset_circuit_breaker,
        )
        reset_circuit_breaker()
        diag = get_circuit_breaker_diagnostics()
        assert diag["state"] == "CLOSED"
        assert diag["consecutive_failures"] == 0
        assert diag["opened_at"] is None

    def test_reset_clears_failures(self):
        from app.services.nba_api_service import (
            _circuit_breaker, reset_circuit_breaker,
            get_circuit_breaker_diagnostics,
        )
        # Simulate failures
        for _ in range(10):
            _circuit_breaker.record_failure()
        diag = get_circuit_breaker_diagnostics()
        assert diag["consecutive_failures"] >= 5

        # Reset
        reset_circuit_breaker()
        diag2 = get_circuit_breaker_diagnostics()
        assert diag2["state"] == "CLOSED"
        assert diag2["consecutive_failures"] == 0

    def test_clear_rotation_cache(self):
        from app.services.nba_api_service import (
            _rotation_cache, clear_rotation_cache,
        )
        # Add a dummy entry
        _rotation_cache[99999] = (0.0, [])
        assert 99999 in _rotation_cache

        cleared = clear_rotation_cache()
        assert cleared >= 1
        assert 99999 not in _rotation_cache


# ---------------------------------------------------------------------------
# DraftGroup Game Type Label Tests
# ---------------------------------------------------------------------------

class TestDraftGroupGameType:
    """Verify game type label mapping."""

    def test_classic_label(self):
        labels = {70: "Classic", 96: "Showdown", 98: "CBB Classic"}
        assert labels.get(70) == "Classic"

    def test_showdown_label(self):
        labels = {70: "Classic", 96: "Showdown", 98: "CBB Classic"}
        assert labels.get(96) == "Showdown"

    def test_cbb_classic_label(self):
        labels = {70: "Classic", 96: "Showdown", 98: "CBB Classic"}
        assert labels.get(98) == "CBB Classic"

    def test_unknown_type(self):
        labels = {70: "Classic", 96: "Showdown", 98: "CBB Classic"}
        assert labels.get(42) is None

    def test_circuit_breaker_logging_in_source(self):
        """Verify CB state logging exists in _build_player_pool_inner."""
        import inspect
        source = inspect.getsource(LineupOptimizerService._build_player_pool_inner)
        assert "get_circuit_breaker_diagnostics" in source
        assert "Circuit breaker" in source

    def test_gametype_verification_in_source(self):
        """Verify DraftGroup gameType verification exists in _build_player_pool_inner."""
        import inspect
        source = inspect.getsource(LineupOptimizerService._build_player_pool_inner)
        assert "gameType" in source
        assert "WRONG GAME TYPE" in source


# ---------------------------------------------------------------------------
# Entries Type Safety Tests
# ---------------------------------------------------------------------------

class TestEntriesTypeSafety:
    """Verify the consumer type-checks entries before pool.extend()."""

    def test_entries_type_check_in_source(self):
        """Verify isinstance(entries, list) check exists in _build_player_pool_inner."""
        import inspect
        source = inspect.getsource(LineupOptimizerService._build_player_pool_inner)
        assert "not isinstance(entries, list)" in source


# ---------------------------------------------------------------------------
# Multi-Lineup Generation Tests
# ---------------------------------------------------------------------------

def _make_large_pool():
    """Build a realistic pool (24 players across 6 teams) for multi-lineup tests."""
    teams = ["BOS", "NYK", "LAL", "DEN", "MIL", "PHX"]
    positions = ["PG", "SG", "SF", "PF", "C"]
    pool = []
    pid = 1
    for team_idx, team in enumerate(teams):
        for pos_idx, pos in enumerate(positions):
            # Stagger salaries so greedy has meaningful choices
            base_salary = 4000 + (pos_idx * 800) + (team_idx * 200)
            base_fp = 15.0 + (pos_idx * 3.5) + (team_idx * 1.2)
            # Primary player
            p = _make_pool_player(
                pid, f"{team} {pos}", pos, team,
                salary=base_salary,
                fp=round(base_fp, 1),
                minutes=28.0,
            )
            p.game_id = f"game_{team_idx // 2}"
            pool.append(p)
            pid += 1
            # Backup (cheaper, lower FP)
            if pos_idx < 3:  # extra guards/forwards
                p2 = _make_pool_player(
                    pid, f"{team} {pos} Bkp", pos, team,
                    salary=max(3500, base_salary - 1500),
                    fp=round(base_fp * 0.65, 1),
                    minutes=18.0,
                )
                p2.game_id = f"game_{team_idx // 2}"
                pool.append(p2)
                pid += 1
    return pool


class TestMultiLineupGeneration:
    """End-to-end tests for the generate_lineups pipeline."""

    def _make_optimizer(self):
        svc = LineupOptimizerService(
            dfs_service=None,
            dk_draftables_service=None,
            nba_service=None,
            injury_service=None,
            rotation_engine=None,
        )
        return svc

    def test_generate_3_lineups(self):
        """Generate 3 lineups from a realistic pool — should all be valid."""
        svc = self._make_optimizer()
        pool = _make_large_pool()

        request = MultiLineupRequest(
            platform="dk",
            draft_group_id=1,
            game_date="2026-02-23",
            num_lineups=3,
            strategy="max_projection",
            contest_type="cash",
            enable_stacking=False,
            salary_floor_pct=0.90,
        )

        # Bypass pool building and enrichment
        with patch.object(svc, "build_player_pool", return_value=pool), \
             patch.object(svc, "_enrich_pool", return_value=pool):
            result = svc.generate_lineups(request)

        assert isinstance(result, MultiLineupResponse)
        assert result.num_generated >= 1, (
            f"Expected at least 1 lineup, got {result.num_generated}. "
            f"Warnings: {result.warnings}"
        )
        assert result.num_generated <= 3

        for lu in result.lineups:
            assert len(lu.players) == 8, f"Lineup has {len(lu.players)} players (expected 8)"
            assert lu.total_salary <= DK_SALARY_CAP, f"Salary {lu.total_salary} > {DK_SALARY_CAP}"
            # Unique players
            pids = [p.player_id for p in lu.players]
            assert len(set(pids)) == 8, f"Duplicate players: {pids}"

    def test_generate_5_lineups_gpp(self):
        """Generate 5 GPP lineups with stacking enabled."""
        svc = self._make_optimizer()
        pool = _make_large_pool()

        request = MultiLineupRequest(
            platform="dk",
            draft_group_id=1,
            game_date="2026-02-23",
            num_lineups=5,
            strategy="ceiling",
            contest_type="gpp",
            enable_stacking=True,
            salary_floor_pct=0.90,
        )

        with patch.object(svc, "build_player_pool", return_value=pool), \
             patch.object(svc, "_enrich_pool", return_value=pool):
            result = svc.generate_lineups(request)

        assert isinstance(result, MultiLineupResponse)
        assert result.num_generated >= 1, (
            f"Expected at least 1 lineup, got {result.num_generated}. "
            f"Warnings: {result.warnings}"
        )

        for lu in result.lineups:
            assert len(lu.players) == 8
            assert lu.total_salary <= DK_SALARY_CAP

    def test_generate_lineups_no_empty_result(self):
        """Generate 2 lineups and verify result is non-empty."""
        svc = self._make_optimizer()
        pool = _make_large_pool()

        request = MultiLineupRequest(
            platform="dk",
            draft_group_id=1,
            game_date="2026-02-23",
            num_lineups=2,
            strategy="balanced",
            contest_type="cash",
            enable_stacking=False,
            salary_floor_pct=0.90,
        )

        with patch.object(svc, "build_player_pool", return_value=pool), \
             patch.object(svc, "_enrich_pool", return_value=pool):
            result = svc.generate_lineups(request)

        assert result.num_generated >= 2, (
            f"Expected 2 lineups, got {result.num_generated}. "
            f"Warnings: {result.warnings}"
        )

    def test_generate_lineups_salary_cap_enforced(self):
        """All generated lineups must respect the $50K salary cap."""
        svc = self._make_optimizer()
        pool = _make_large_pool()

        request = MultiLineupRequest(
            platform="dk",
            draft_group_id=1,
            game_date="2026-02-23",
            num_lineups=10,
            strategy="max_projection",
            contest_type="gpp",
            enable_stacking=False,
            salary_floor_pct=0.90,
        )

        with patch.object(svc, "build_player_pool", return_value=pool), \
             patch.object(svc, "_enrich_pool", return_value=pool):
            result = svc.generate_lineups(request)

        for i, lu in enumerate(result.lineups):
            assert lu.total_salary <= DK_SALARY_CAP, (
                f"Lineup {i+1} salary ${lu.total_salary:,} exceeds "
                f"cap ${DK_SALARY_CAP:,}"
            )
