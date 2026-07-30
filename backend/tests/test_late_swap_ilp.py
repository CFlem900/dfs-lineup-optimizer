"""Tests for precision late-swap: BDL game-status lookup + ILP re-optimization."""

import pytest
from unittest.mock import patch, MagicMock

from app.models.lineup import (
    GameSlotStatus,
    LateSwapSlotInfo,
    LineupPlayer,
    OptimizedLineup,
    PlayerPoolEntry,
)
from app.services.balldontlie_service import BallDontLieService
from app.services.lineup_optimizer_service import (
    LineupOptimizerService,
    DK_SALARY_CAP,
    DK_ROSTER_SLOTS,
    DK_SLOT_ELIGIBILITY,
    _index_slots,
    _base_slot,
    _PULP_AVAILABLE,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_pool_player(
    player_id, name, pos, team, salary, fp,
    floor_fp=None, ceil_fp=None, minutes=25.0,
    injury_status=None,
):
    """Create a PlayerPoolEntry for tests."""
    floor_fp = floor_fp or round(fp * 0.8, 1)
    ceil_fp = ceil_fp or round(fp * 1.2, 1)
    eligible = [
        slot for slot, positions in DK_SLOT_ELIGIBILITY.items()
        if pos in positions
    ]
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
        injury_status=injury_status,
    )


def _make_lineup_player(
    player_id, name, pos, team, salary, fp,
    roster_slot=None, minutes=25.0,
):
    """Create a LineupPlayer for tests."""
    return LineupPlayer(
        player_id=player_id,
        player_name=name,
        position=pos,
        roster_slot=roster_slot or pos,
        team_abbreviation=team,
        salary=salary,
        projected_fp=fp,
        floor_fp=round(fp * 0.8, 1),
        ceiling_fp=round(fp * 1.2, 1),
        projected_minutes=minutes,
    )


def _make_optimized_lineup(players, platform="dk", sport="nba"):
    """Build an OptimizedLineup from a list of LineupPlayers."""
    total_salary = sum(p.salary for p in players)
    return OptimizedLineup(
        platform=platform,
        sport=sport,
        players=players,
        total_salary=total_salary,
        salary_remaining=DK_SALARY_CAP - total_salary,
        total_projected_fp=round(sum(p.projected_fp for p in players), 1),
        total_floor_fp=round(sum(p.floor_fp for p in players), 1),
        total_ceiling_fp=round(sum(p.ceiling_fp for p in players), 1),
        salary_cap=DK_SALARY_CAP,
        roster_slots=list(DK_ROSTER_SLOTS),
    )


@pytest.fixture
def optimizer():
    """A LineupOptimizerService with None services."""
    return LineupOptimizerService(
        dfs_service=None,
        dk_draftables_service=None,
        nba_service=None,
        injury_service=None,
        rotation_engine=None,
    )


@pytest.fixture
def mock_pool():
    """16-player pool across all positions."""
    return [
        _make_pool_player(1, "PG Star", "PG", "BOS", 9000, 45.0),
        _make_pool_player(2, "PG Mid", "PG", "NYK", 6000, 28.0),
        _make_pool_player(3, "PG Value", "PG", "MIA", 3500, 15.0),
        _make_pool_player(4, "SG Star", "SG", "LAL", 8500, 42.0),
        _make_pool_player(5, "SG Mid", "SG", "CHI", 5500, 25.0),
        _make_pool_player(6, "SG Value", "SG", "ORL", 3500, 14.0),
        _make_pool_player(7, "SF Star", "SF", "DEN", 8000, 40.0),
        _make_pool_player(8, "SF Mid", "SF", "PHX", 5000, 22.0),
        _make_pool_player(9, "SF Value", "SF", "SAS", 3500, 13.0),
        _make_pool_player(10, "PF Star", "PF", "MIL", 7500, 38.0),
        _make_pool_player(11, "PF Mid", "PF", "CLE", 5000, 20.0),
        _make_pool_player(12, "PF Value", "PF", "IND", 3500, 12.0),
        _make_pool_player(13, "C Star", "C", "MIN", 7000, 35.0),
        _make_pool_player(14, "C Mid", "C", "SAC", 4500, 18.0),
        _make_pool_player(15, "C Value", "C", "UTA", 3500, 11.0),
        _make_pool_player(16, "G Bench", "PG", "WAS", 3500, 10.0),
    ]


# ===========================================================================
# BallDontLieService.get_game_statuses_by_team
# ===========================================================================

class TestGetGameStatusesByTeam:
    """Tests for the BDL game-status team-keyed lookup."""

    @pytest.fixture
    def bdl_service(self):
        with patch("app.services.balldontlie_service.settings") as mock_s:
            mock_s.balldontlie_api_key = "test-key"
            mock_s.balldontlie_rate_limit = 600
            svc = BallDontLieService()
            svc._teams_loaded = True
            return svc

    def test_final_game_is_locked(self, bdl_service):
        """A game with status='Final' should be locked for both teams."""
        bdl_service._get = MagicMock(return_value={"data": [{
            "id": 100,
            "status": "Final",
            "period": 4,
            "home_team": {"abbreviation": "BOS"},
            "visitor_team": {"abbreviation": "NYK"},
            "home_team_score": 112,
            "visitor_team_score": 105,
        }]})
        result = bdl_service.get_game_statuses_by_team("2026-02-24")

        assert "BOS" in result
        assert "NYK" in result
        assert result["BOS"]["game_status"] == "final"
        assert result["BOS"]["is_locked"] is True
        assert result["NYK"]["is_locked"] is True
        assert result["BOS"]["opponent"] == "NYK"
        assert result["NYK"]["opponent"] == "BOS"

    def test_in_progress_game_is_locked(self, bdl_service):
        """A game with period > 0 should be locked."""
        bdl_service._get = MagicMock(return_value={"data": [{
            "id": 200,
            "status": "In Progress",
            "period": 2,
            "home_team": {"abbreviation": "CHI"},
            "visitor_team": {"abbreviation": "MIA"},
            "home_team_score": 55,
            "visitor_team_score": 48,
        }]})
        result = bdl_service.get_game_statuses_by_team("2026-02-24")

        assert result["CHI"]["game_status"] == "in_progress"
        assert result["CHI"]["is_locked"] is True
        assert result["MIA"]["is_locked"] is True

    def test_scheduled_game_is_open(self, bdl_service):
        """A game with period=0 and non-Final status should be open."""
        bdl_service._get = MagicMock(return_value={"data": [{
            "id": 300,
            "status": "7:00 PM ET",
            "period": 0,
            "home_team": {"abbreviation": "LAL"},
            "visitor_team": {"abbreviation": "DEN"},
            "home_team_score": 0,
            "visitor_team_score": 0,
        }]})
        result = bdl_service.get_game_statuses_by_team("2026-02-24")

        assert result["LAL"]["game_status"] == "not_started"
        assert result["LAL"]["is_locked"] is False
        assert result["DEN"]["is_locked"] is False

    def test_empty_games_returns_empty(self, bdl_service):
        """No games should return empty dict."""
        bdl_service._get = MagicMock(return_value={"data": []})
        result = bdl_service.get_game_statuses_by_team("2026-02-24")
        assert result == {}

    def test_mixed_slate(self, bdl_service):
        """Slate with one started and one not started game."""
        bdl_service._get = MagicMock(return_value={"data": [
            {
                "id": 100, "status": "Final", "period": 4,
                "home_team": {"abbreviation": "BOS"},
                "visitor_team": {"abbreviation": "NYK"},
                "home_team_score": 112, "visitor_team_score": 105,
            },
            {
                "id": 200, "status": "9:00 PM ET", "period": 0,
                "home_team": {"abbreviation": "LAL"},
                "visitor_team": {"abbreviation": "DEN"},
                "home_team_score": 0, "visitor_team_score": 0,
            },
        ]})
        result = bdl_service.get_game_statuses_by_team("2026-02-24")

        assert result["BOS"]["is_locked"] is True
        assert result["NYK"]["is_locked"] is True
        assert result["LAL"]["is_locked"] is False
        assert result["DEN"]["is_locked"] is False

    def test_null_period_treated_as_zero(self, bdl_service):
        """period=None should be treated as not started."""
        bdl_service._get = MagicMock(return_value={"data": [{
            "id": 400,
            "status": "8:00 PM ET",
            "period": None,
            "home_team": {"abbreviation": "PHX"},
            "visitor_team": {"abbreviation": "SAS"},
            "home_team_score": 0,
            "visitor_team_score": 0,
        }]})
        result = bdl_service.get_game_statuses_by_team("2026-02-24")

        assert result["PHX"]["is_locked"] is False
        assert result["SAS"]["is_locked"] is False


# ===========================================================================
# LineupOptimizerService.optimize_late_swap
# ===========================================================================

class TestOptimizeLateSwap:
    """Tests for the ILP late-swap re-optimizer."""

    def _build_lineup_and_pool(self, mock_pool):
        """Build an 8-player DK lineup + pool for late-swap tests.

        Lineup:
            PG=PG Star (BOS, $9000, 45fp)
            SG=SG Star (LAL, $8500, 42fp)
            SF=SF Star (DEN, $8000, 40fp)
            PF=PF Star (MIL, $7500, 38fp)
            C =C Star  (MIN, $7000, 35fp)
            G =PG Mid  (NYK, $6000, 28fp)
            F =SF Mid  (PHX, $5000, 22fp) -- SF in F slot (eligible)
            UTIL=SG Value (ORL, $3500, 14fp) -- SG in UTIL (eligible)
        Total: $54,500 > $50,000 ... let me recalculate for valid budget.
        """
        players = [
            _make_lineup_player(1, "PG Star", "PG", "BOS", 8000, 40.0, "PG"),
            _make_lineup_player(4, "SG Star", "SG", "LAL", 7500, 38.0, "SG"),
            _make_lineup_player(7, "SF Star", "SF", "DEN", 7000, 36.0, "SF"),
            _make_lineup_player(10, "PF Star", "PF", "MIL", 6500, 33.0, "PF"),
            _make_lineup_player(13, "C Star", "C", "MIN", 6000, 30.0, "C"),
            _make_lineup_player(2, "PG Mid", "PG", "NYK", 5500, 25.0, "G"),
            _make_lineup_player(8, "SF Mid", "SF", "PHX", 5000, 22.0, "F"),
            _make_lineup_player(6, "SG Value", "SG", "ORL", 4500, 18.0, "UTIL"),
        ]
        lineup = _make_optimized_lineup(players)

        # Rebuild pool with matching salaries
        pool = [
            _make_pool_player(1, "PG Star", "PG", "BOS", 8000, 40.0),
            _make_pool_player(2, "PG Mid", "PG", "NYK", 5500, 25.0),
            _make_pool_player(3, "PG Value", "PG", "MIA", 3500, 15.0),
            _make_pool_player(4, "SG Star", "SG", "LAL", 7500, 38.0),
            _make_pool_player(5, "SG Mid", "SG", "CHI", 5000, 24.0),
            _make_pool_player(6, "SG Value", "SG", "ORL", 4500, 18.0),
            _make_pool_player(7, "SF Star", "SF", "DEN", 7000, 36.0),
            _make_pool_player(8, "SF Mid", "SF", "PHX", 5000, 22.0),
            _make_pool_player(9, "SF Value", "SF", "SAS", 3500, 13.0),
            _make_pool_player(10, "PF Star", "PF", "MIL", 6500, 33.0),
            _make_pool_player(11, "PF Mid", "PF", "CLE", 4500, 19.0),
            _make_pool_player(12, "PF Value", "PF", "IND", 3500, 12.0),
            _make_pool_player(13, "C Star", "C", "MIN", 6000, 30.0),
            _make_pool_player(14, "C Mid", "C", "SAC", 4000, 17.0),
            _make_pool_player(15, "C Value", "C", "UTA", 3500, 11.0),
            _make_pool_player(16, "G Bench", "PG", "WAS", 3500, 10.0),
        ]
        return lineup, pool

    @pytest.mark.skipif(not _PULP_AVAILABLE, reason="PuLP not installed")
    def test_basic_ilp_reoptimize(self, optimizer):
        """ILP should fill open slots within remaining salary budget."""
        lineup, pool = self._build_lineup_and_pool(None)
        indexed = _index_slots(DK_ROSTER_SLOTS)

        # Lock first 5 slots (PG, SG, SF, PF, C games started)
        locked_slots = {
            indexed[i]: lineup.players[i] for i in range(5)
        }
        open_slots = indexed[5:]  # G, F, UTIL

        result = optimizer.optimize_late_swap(
            lineup=lineup,
            pool=pool,
            locked_slots=locked_slots,
            open_slots=open_slots,
            platform="dk",
            salary_cap=DK_SALARY_CAP,
        )

        assert result is not None
        assert len(result.players) == 8
        assert result.total_salary <= DK_SALARY_CAP

        # Verify locked players are preserved
        for i in range(5):
            assert result.players[i].player_id == lineup.players[i].player_id

    @pytest.mark.skipif(not _PULP_AVAILABLE, reason="PuLP not installed")
    def test_all_locked_returns_none(self, optimizer):
        """When all slots are locked, optimize_late_swap returns None."""
        lineup, pool = self._build_lineup_and_pool(None)
        indexed = _index_slots(DK_ROSTER_SLOTS)

        locked_slots = {
            indexed[i]: lineup.players[i] for i in range(8)
        }
        open_slots = []

        result = optimizer.optimize_late_swap(
            lineup=lineup,
            pool=pool,
            locked_slots=locked_slots,
            open_slots=open_slots,
            platform="dk",
            salary_cap=DK_SALARY_CAP,
        )
        assert result is None

    @pytest.mark.skipif(not _PULP_AVAILABLE, reason="PuLP not installed")
    def test_all_open_full_reoptimize(self, optimizer):
        """With no locked slots, the ILP should re-optimize entire lineup."""
        lineup, pool = self._build_lineup_and_pool(None)
        indexed = _index_slots(DK_ROSTER_SLOTS)

        locked_slots = {}
        open_slots = list(indexed)

        result = optimizer.optimize_late_swap(
            lineup=lineup,
            pool=pool,
            locked_slots=locked_slots,
            open_slots=open_slots,
            platform="dk",
            salary_cap=DK_SALARY_CAP,
        )

        assert result is not None
        assert len(result.players) == 8
        assert result.total_salary <= DK_SALARY_CAP
        # Should pick high-value players
        assert result.total_projected_fp > 0

    @pytest.mark.skipif(not _PULP_AVAILABLE, reason="PuLP not installed")
    def test_salary_constraint_respected(self, optimizer):
        """ILP must respect the reduced salary budget from locked players."""
        lineup, pool = self._build_lineup_and_pool(None)
        indexed = _index_slots(DK_ROSTER_SLOTS)

        # Lock 5 expensive players
        locked_slots = {
            indexed[i]: lineup.players[i] for i in range(5)
        }
        open_slots = indexed[5:]

        locked_salary = sum(p.salary for p in locked_slots.values())
        remaining = DK_SALARY_CAP - locked_salary

        result = optimizer.optimize_late_swap(
            lineup=lineup,
            pool=pool,
            locked_slots=locked_slots,
            open_slots=open_slots,
            platform="dk",
            salary_cap=DK_SALARY_CAP,
        )

        assert result is not None
        open_salary = sum(
            p.salary for i, p in enumerate(result.players) if i >= 5
        )
        assert open_salary <= remaining

    @pytest.mark.skipif(not _PULP_AVAILABLE, reason="PuLP not installed")
    def test_injured_players_excluded(self, optimizer):
        """Out/Doubtful players should be excluded from the ILP pool."""
        lineup, pool = self._build_lineup_and_pool(None)
        indexed = _index_slots(DK_ROSTER_SLOTS)

        # Mark some pool players as Out
        pool[2].injury_status = "Out"      # PG Value
        pool[4].injury_status = "Doubtful"  # SG Mid

        locked_slots = {indexed[i]: lineup.players[i] for i in range(5)}
        open_slots = indexed[5:]

        result = optimizer.optimize_late_swap(
            lineup=lineup,
            pool=pool,
            locked_slots=locked_slots,
            open_slots=open_slots,
            platform="dk",
            salary_cap=DK_SALARY_CAP,
        )

        if result is not None:
            result_ids = {p.player_id for p in result.players}
            assert 3 not in result_ids, "Out player should not appear"
            assert 5 not in result_ids, "Doubtful player should not appear"

    @pytest.mark.skipif(not _PULP_AVAILABLE, reason="PuLP not installed")
    def test_locked_players_not_reused(self, optimizer):
        """Locked players should not appear in open slot assignments."""
        lineup, pool = self._build_lineup_and_pool(None)
        indexed = _index_slots(DK_ROSTER_SLOTS)

        locked_slots = {indexed[i]: lineup.players[i] for i in range(5)}
        locked_ids = {p.player_id for p in locked_slots.values()}
        open_slots = indexed[5:]

        result = optimizer.optimize_late_swap(
            lineup=lineup,
            pool=pool,
            locked_slots=locked_slots,
            open_slots=open_slots,
            platform="dk",
            salary_cap=DK_SALARY_CAP,
        )

        assert result is not None
        # Open slots should not duplicate a locked player
        open_ids = {result.players[i].player_id for i in range(5, 8)}
        assert locked_ids.isdisjoint(open_ids)

    def test_returns_none_without_pulp(self, optimizer):
        """If PuLP is not available, should return None."""
        lineup, pool = self._build_lineup_and_pool(None)
        indexed = _index_slots(DK_ROSTER_SLOTS)

        with patch("app.services.lineup_optimizer_service._PULP_AVAILABLE", False):
            result = optimizer.optimize_late_swap(
                lineup=lineup,
                pool=pool,
                locked_slots={},
                open_slots=list(indexed),
                platform="dk",
                salary_cap=DK_SALARY_CAP,
            )
        assert result is None


# ===========================================================================
# Pydantic model tests
# ===========================================================================

class TestNewModels:
    """Verify the new Pydantic models can be instantiated."""

    def test_game_slot_status_construction(self):
        gs = GameSlotStatus(
            team_abbreviation="BOS",
            game_status="in_progress",
            game_status_detail="Q2 5:31",
            is_locked=True,
            opponent="NYK",
            home_team_score=55,
            visitor_team_score=48,
        )
        assert gs.is_locked is True
        assert gs.game_status == "in_progress"

    def test_late_swap_slot_info_construction(self):
        info = LateSwapSlotInfo(
            roster_slot="PG",
            player_name="Test Player",
            player_id=42,
            team_abbreviation="BOS",
            is_locked=True,
            lock_reason="game_in_progress",
        )
        assert info.is_locked is True
        assert info.lock_reason == "game_in_progress"

    def test_game_slot_status_not_started(self):
        gs = GameSlotStatus(
            team_abbreviation="LAL",
            game_status="not_started",
            game_status_detail="9:00 PM ET",
            is_locked=False,
        )
        assert gs.is_locked is False
        assert gs.opponent is None
