"""Tests for LivePropTrackerService — real-time prop tracking."""

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Optional

import pytest

from app.services.live_prop_tracker_service import (
    LivePlayerStats,
    LivePropTrackerService,
    PlayerPropTracker,
    PropTrackerSnapshot,
    _parse_bdl_minutes,
    calculate_pace_status,
    estimate_elapsed_game_minutes,
    update_hit_probability,
    NBA_REGULATION_MINUTES,
    NBA_QUARTER_MINUTES,
    NBA_OT_MINUTES,
)


# ── Mock services ─────────────────────────────────────────────────────────


@dataclass
class MockGameState:
    """Minimal mock of live_game_state_service.GameState."""

    bdl_game_id: int
    home_team: str
    visitor_team: str
    has_started: bool
    period: int
    status_detail: str
    home_score: int = 0
    visitor_score: int = 0
    is_final: bool = False

    @property
    def is_in_progress(self) -> bool:
        return self.has_started and not self.is_final


class MockLiveGameStateService:
    """Mock returning configurable game states."""

    def __init__(self, game_states: Dict[str, MockGameState] = None):
        self._states = game_states or {}

    async def fetch_game_states(self, game_date: str):
        return self._states


class MockDKPropsService:
    """Mock returning configurable DK props."""

    def __init__(self, props: dict = None):
        self._props = props or {}

    def get_player_props(self, game_date: str):
        return self._props


class MockPlayerProps:
    """Minimal mock of models.props.PlayerProps."""

    def __init__(self, name, team, props_list):
        self.player_name = name
        self.team_abbreviation = team
        self.props = props_list


class MockPropLine:
    """Minimal mock of models.props.PlayerPropLine."""

    def __init__(self, stat, line, over_odds=-110, implied_over=0.5238):
        self.stat = stat
        self.line = line
        self.over_odds = over_odds
        self.implied_over_prob = implied_over


class MockCacheService:
    """In-memory async cache mock."""

    def __init__(self):
        self._store: dict = {}

    async def get(self, key):
        return self._store.get(key)

    async def set(self, key, value, ttl=None):
        self._store[key] = value

    async def delete(self, key):
        self._store.pop(key, None)

    async def clear_pattern(self, pattern):
        import fnmatch
        keys = [k for k in self._store if fnmatch.fnmatch(k, pattern)]
        for k in keys:
            del self._store[k]


class MockBDLService:
    """Mock BDL service for live box score auto-fetch tests."""

    def __init__(self, box_scores=None, games=None):
        self._box_scores = box_scores or []
        self._games = games or []

    def get_live_box_scores(self, game_ids):
        return self._box_scores

    def get_games(self, date_str):
        return self._games


def _make_service(
    game_states=None,
    dk_props=None,
    cache=None,
    bdl_service=None,
):
    return LivePropTrackerService(
        dk_props_service=MockDKPropsService(dk_props),
        live_game_state_service=MockLiveGameStateService(game_states),
        cache_service=cache,
        bdl_service=bdl_service,
    )


# ── Test elapsed game minutes ─────────────────────────────────────────────


class TestElapsedGameMinutes:
    def test_not_started(self):
        assert estimate_elapsed_game_minutes(0, False) == 0.0

    def test_quarter_1_midpoint(self):
        result = estimate_elapsed_game_minutes(1, False)
        assert result == NBA_QUARTER_MINUTES / 2  # 6.0

    def test_quarter_2_midpoint(self):
        result = estimate_elapsed_game_minutes(2, False)
        expected = NBA_QUARTER_MINUTES + (NBA_QUARTER_MINUTES / 2)  # 18.0
        assert result == expected

    def test_quarter_3_midpoint(self):
        result = estimate_elapsed_game_minutes(3, False)
        expected = 2 * NBA_QUARTER_MINUTES + (NBA_QUARTER_MINUTES / 2)  # 30.0
        assert result == expected

    def test_quarter_4_midpoint(self):
        result = estimate_elapsed_game_minutes(4, False)
        expected = 3 * NBA_QUARTER_MINUTES + (NBA_QUARTER_MINUTES / 2)  # 42.0
        assert result == expected

    def test_final_regulation(self):
        assert estimate_elapsed_game_minutes(4, True) == NBA_REGULATION_MINUTES

    def test_final_overtime(self):
        # Final in OT1 (period 5)
        result = estimate_elapsed_game_minutes(5, True)
        assert result == NBA_REGULATION_MINUTES + NBA_OT_MINUTES  # 53.0

    def test_in_ot_period(self):
        # Currently in OT1 (period 5), not final
        result = estimate_elapsed_game_minutes(5, False)
        expected = NBA_REGULATION_MINUTES + (NBA_OT_MINUTES / 2)  # 50.5
        assert result == expected

    def test_negative_period(self):
        assert estimate_elapsed_game_minutes(-1, False) == 0.0


# ── Test pace status ──────────────────────────────────────────────────────


class TestPaceStatus:
    def test_not_started(self):
        assert calculate_pace_status(0, 42.5, 0.0, "not_started") == "not_started"

    def test_final_hit(self):
        assert calculate_pace_status(45.0, 42.5, 1.0, "final") == "hit"

    def test_final_miss(self):
        assert calculate_pace_status(40.0, 42.5, 1.0, "final") == "miss"

    def test_way_ahead(self):
        # 75% of line with 50% of minutes → ratio = 1.50
        current = 42.5 * 0.75  # 31.875
        status = calculate_pace_status(current, 42.5, 0.50, "in_progress")
        assert status == "way_ahead"

    def test_ahead(self):
        # 60% of line with 50% of minutes → ratio = 1.20
        current = 42.5 * 0.60
        status = calculate_pace_status(current, 42.5, 0.50, "in_progress")
        assert status == "ahead"

    def test_on_pace(self):
        # 50% of line with 50% of minutes → ratio = 1.0
        current = 42.5 * 0.50
        status = calculate_pace_status(current, 42.5, 0.50, "in_progress")
        assert status == "on_pace"

    def test_behind(self):
        # 40% of line with 50% of minutes → ratio = 0.80
        current = 42.5 * 0.40
        status = calculate_pace_status(current, 42.5, 0.50, "in_progress")
        assert status == "behind"

    def test_way_behind(self):
        # 25% of line with 50% of minutes → ratio = 0.50
        current = 42.5 * 0.25
        status = calculate_pace_status(current, 42.5, 0.50, "in_progress")
        assert status == "way_behind"

    def test_early_game_defaults_on_pace(self):
        # Very early in game (5% elapsed) → defaults to on_pace
        status = calculate_pace_status(2.0, 42.5, 0.05, "in_progress")
        assert status == "on_pace"


# ── Test hit probability ──────────────────────────────────────────────────


class TestHitProbability:
    def test_pre_game_returns_none(self):
        p = update_hit_probability(0, 42.5, 32.0, 0.88, 8.0, 0.0)
        assert p is None

    def test_already_hit_returns_1(self):
        p = update_hit_probability(45.0, 0.0, 10.0, 0.88, 8.0, 0.7)
        assert p == 1.0

    def test_no_time_left_returns_0(self):
        p = update_hit_probability(30.0, 12.5, 0.0, 0.88, 8.0, 1.0)
        assert p == 0.0

    def test_ahead_increases_probability(self):
        # Player clearly ahead at halftime — high rate covers remaining easily
        p = update_hit_probability(
            current_value=30.0,
            remaining_requirement=12.5,  # need 12.5 more for 42.5 line
            remaining_minutes=16.0,
            per_min_rate=0.88,  # rate produces ~14.1 in 16 min → covers 12.5
            pre_game_std=8.0,
            elapsed_fraction=0.5,
        )
        assert p is not None
        assert p > 0.5  # Projected to comfortably clear the line

    def test_behind_decreases_probability(self):
        # Player behind at halftime
        p = update_hit_probability(
            current_value=10.0,
            remaining_requirement=32.5,  # need 32.5 more for 42.5 line
            remaining_minutes=16.0,
            per_min_rate=0.88,  # rate would produce ~14.1 in 16 min
            pre_game_std=8.0,
            elapsed_fraction=0.5,
        )
        assert p is not None
        assert p < 0.3  # Should be low

    def test_probability_bounded_0_1(self):
        p = update_hit_probability(20.0, 22.5, 15.0, 0.88, 8.0, 0.5)
        assert p is not None
        assert 0.0 <= p <= 1.0


# ── Test live stats ingestion ─────────────────────────────────────────────


class TestLiveStatsIngestion:
    @pytest.mark.asyncio
    async def test_ingest_stores_stats(self):
        svc = _make_service()
        stats = [
            LivePlayerStats(
                player_name="Luka Doncic",
                team_abbreviation="DAL",
                pts=22,
                reb=6,
                ast=4,
                minutes_played=18.5,
                source="scraper",
            ),
        ]
        count = await svc.ingest_live_stats("2026-02-25", stats)
        assert count == 1

        # Check in-memory store
        key = svc._stats_key("Luka Doncic", "DAL")
        assert key in svc._mem_stats
        assert svc._mem_stats[key]["pts"] == 22

    @pytest.mark.asyncio
    async def test_ingest_multiple_players(self):
        svc = _make_service()
        stats = [
            LivePlayerStats(player_name="Player A", team_abbreviation="BOS", pts=15),
            LivePlayerStats(player_name="Player B", team_abbreviation="MIL", pts=12),
        ]
        count = await svc.ingest_live_stats("2026-02-25", stats)
        assert count == 2

    @pytest.mark.asyncio
    async def test_ingest_with_redis_cache(self):
        cache = MockCacheService()
        svc = _make_service(cache=cache)
        stats = [
            LivePlayerStats(
                player_name="Test Player",
                team_abbreviation="LAL",
                pts=10,
            ),
        ]
        count = await svc.ingest_live_stats("2026-02-25", stats)
        assert count == 1
        # Should be in the Redis mock
        key = svc._stats_key("Test Player", "LAL")
        assert key in cache._store

    @pytest.mark.asyncio
    async def test_clear_live_stats(self):
        svc = _make_service()
        await svc.ingest_live_stats(
            "2026-02-25",
            [LivePlayerStats(player_name="X", team_abbreviation="DAL", pts=5)],
        )
        assert len(svc._mem_stats) == 1
        await svc.clear_live_stats()
        assert len(svc._mem_stats) == 0


# ── Test tracker snapshot (end-to-end) ────────────────────────────────────


class TestTrackerSnapshot:
    @pytest.mark.asyncio
    async def test_empty_when_no_props(self):
        svc = _make_service(game_states={}, dk_props={})
        snapshot = await svc.get_tracker_snapshot("2026-02-25")
        assert isinstance(snapshot, PropTrackerSnapshot)
        assert len(snapshot.players) == 0

    @pytest.mark.asyncio
    async def test_pre_game_snapshot(self):
        """Before tipoff — all players should be 'not_started'."""
        gs = MockGameState(
            bdl_game_id=1001,
            home_team="DAL",
            visitor_team="LAL",
            has_started=False,
            period=0,
            status_detail="2026-02-25T19:00:00Z",
        )
        props = {
            "luka doncic:DAL": MockPlayerProps(
                "Luka Doncic", "DAL",
                [MockPropLine("pra", 48.5)],
            ),
        }
        svc = _make_service(
            game_states={"DAL": gs, "LAL": gs},
            dk_props=props,
        )
        snapshot = await svc.get_tracker_snapshot("2026-02-25")

        assert len(snapshot.players) == 1
        t = snapshot.players[0]
        assert t.player_name == "Luka Doncic"
        assert t.stat == "pra"
        assert t.pace_status == "not_started"
        assert t.current_value == 0.0
        assert t.remaining_requirement == 48.5

    @pytest.mark.asyncio
    async def test_in_progress_estimated_mode(self):
        """Mid-game without live stats — should use pace estimation."""
        gs = MockGameState(
            bdl_game_id=1001,
            home_team="DAL",
            visitor_team="LAL",
            has_started=True,
            period=2,  # 2nd quarter
            status_detail="2nd Qtr",
            home_score=55,
            visitor_score=50,
        )
        props = {
            "luka doncic:DAL": MockPlayerProps(
                "Luka Doncic", "DAL",
                [MockPropLine("pts", 28.5)],
            ),
        }
        svc = _make_service(
            game_states={"DAL": gs, "LAL": gs},
            dk_props=props,
        )
        snapshot = await svc.get_tracker_snapshot("2026-02-25")

        assert len(snapshot.players) == 1
        t = snapshot.players[0]
        assert t.source == "estimated"
        assert t.game_period == 2
        assert t.game_status == "in_progress"
        assert t.elapsed_game_minutes == 18.0
        assert t.current_value > 0  # Estimated from per-min rate
        assert snapshot.mode == "estimated"

    @pytest.mark.asyncio
    async def test_in_progress_with_live_stats(self):
        """Mid-game with ingested live stats — should use live data."""
        gs = MockGameState(
            bdl_game_id=1001,
            home_team="DAL",
            visitor_team="LAL",
            has_started=True,
            period=3,
            status_detail="3rd Qtr",
            home_score=78,
            visitor_score=72,
        )
        props = {
            "luka doncic:DAL": MockPlayerProps(
                "Luka Doncic", "DAL",
                [MockPropLine("pra", 48.5)],
            ),
        }
        svc = _make_service(
            game_states={"DAL": gs, "LAL": gs},
            dk_props=props,
        )

        # Ingest live stats
        # Give Luka huge stats at halftime → clearly way ahead
        await svc.ingest_live_stats("2026-02-25", [
            LivePlayerStats(
                player_name="Luka Doncic",
                team_abbreviation="DAL",
                pts=28,
                reb=8,
                ast=7,
                minutes_played=18.0,  # only 18 min played (56% of 32)
            ),
        ])

        snapshot = await svc.get_tracker_snapshot("2026-02-25")

        assert len(snapshot.players) == 1
        t = snapshot.players[0]
        assert t.source == "live"
        assert t.current_value == 43.0  # 28 + 8 + 7
        assert t.remaining_requirement == 5.5  # 48.5 - 43.0
        # At 56% of minutes with 89% of line → ratio ~1.58 → way_ahead
        assert t.pace_status in ("ahead", "way_ahead")
        assert snapshot.mode == "live"

    @pytest.mark.asyncio
    async def test_final_game_hit_miss(self):
        """After game ends — should report hit or miss."""
        gs = MockGameState(
            bdl_game_id=1001,
            home_team="DAL",
            visitor_team="LAL",
            has_started=True,
            period=4,
            status_detail="Final",
            home_score=115,
            visitor_score=108,
            is_final=True,
        )
        props = {
            "luka doncic:DAL": MockPlayerProps(
                "Luka Doncic", "DAL",
                [MockPropLine("pra", 48.5)],
            ),
        }
        svc = _make_service(
            game_states={"DAL": gs, "LAL": gs},
            dk_props=props,
        )

        await svc.ingest_live_stats("2026-02-25", [
            LivePlayerStats(
                player_name="Luka Doncic",
                team_abbreviation="DAL",
                pts=30,
                reb=10,
                ast=12,
                minutes_played=36.0,
            ),
        ])

        snapshot = await svc.get_tracker_snapshot("2026-02-25")
        t = snapshot.players[0]
        assert t.pace_status == "hit"  # 52 > 48.5
        assert t.hit_probability == 1.0

    @pytest.mark.asyncio
    async def test_summary_counts(self):
        """Summary should aggregate pace status counts correctly."""
        gs_live = MockGameState(
            bdl_game_id=1, home_team="A", visitor_team="B",
            has_started=True, period=3, status_detail="3Q",
        )
        gs_pre = MockGameState(
            bdl_game_id=2, home_team="C", visitor_team="D",
            has_started=False, period=0, status_detail="7:00PM",
        )
        props = {
            "player1:A": MockPlayerProps("Player1", "A", [MockPropLine("pts", 25.0)]),
            "player2:C": MockPlayerProps("Player2", "C", [MockPropLine("pts", 20.0)]),
        }
        svc = _make_service(
            game_states={
                "A": gs_live, "B": gs_live,
                "C": gs_pre, "D": gs_pre,
            },
            dk_props=props,
        )
        snapshot = await svc.get_tracker_snapshot("2026-02-25")

        assert snapshot.summary.total_tracked == 2
        assert snapshot.summary.games_in_progress == 1
        assert snapshot.summary.games_not_started == 1

    @pytest.mark.asyncio
    async def test_mixed_mode(self):
        """When some players have live stats and others don't → mixed mode."""
        gs = MockGameState(
            bdl_game_id=1, home_team="DAL", visitor_team="LAL",
            has_started=True, period=2, status_detail="2Q",
        )
        props = {
            "player1:DAL": MockPlayerProps("Player1", "DAL", [MockPropLine("pts", 25.0)]),
            "player2:LAL": MockPlayerProps("Player2", "LAL", [MockPropLine("pts", 20.0)]),
        }
        svc = _make_service(
            game_states={"DAL": gs, "LAL": gs},
            dk_props=props,
        )
        # Only ingest stats for Player1
        await svc.ingest_live_stats("2026-02-25", [
            LivePlayerStats(player_name="Player1", team_abbreviation="DAL", pts=15),
        ])

        snapshot = await svc.get_tracker_snapshot("2026-02-25")
        assert snapshot.mode == "mixed"
        sources = {t.source for t in snapshot.players}
        assert "live" in sources
        assert "estimated" in sources


# ── Test BDL minutes parsing ─────────────────────────────────────────────


class TestParseBDLMinutes:
    def test_colon_format(self):
        assert _parse_bdl_minutes("32:15") == pytest.approx(32.25)

    def test_integer_string(self):
        assert _parse_bdl_minutes("28") == 28.0

    def test_empty_string(self):
        assert _parse_bdl_minutes("") == 0.0

    def test_none_coerced(self):
        assert _parse_bdl_minutes("") == 0.0

    def test_zero_seconds(self):
        assert _parse_bdl_minutes("12:00") == 12.0

    def test_invalid_string(self):
        assert _parse_bdl_minutes("N/A") == 0.0


# ── Test BDL auto-fetch ──────────────────────────────────────────────────


class TestBDLAutoFetch:
    @pytest.mark.asyncio
    async def test_fetch_ingests_live_stats(self):
        """BDL box scores should be converted and stored as live stats."""
        gs = MockGameState(
            bdl_game_id=5001,
            home_team="DAL",
            visitor_team="LAL",
            has_started=True,
            period=3,
            status_detail="3rd Qtr",
            home_score=85,
            visitor_score=78,
        )
        bdl = MockBDLService(box_scores=[
            {
                "player": {"first_name": "Luka", "last_name": "Doncic"},
                "team": {"abbreviation": "DAL"},
                "game": {"id": 5001},
                "min": "24:30",
                "pts": 22,
                "reb": 7,
                "ast": 5,
                "stl": 1,
                "blk": 0,
                "turnover": 3,
                "fg3m": 4,
            },
            {
                "player": {"first_name": "LeBron", "last_name": "James"},
                "team": {"abbreviation": "LAL"},
                "game": {"id": 5001},
                "min": "26:00",
                "pts": 18,
                "reb": 8,
                "ast": 6,
                "stl": 0,
                "blk": 1,
                "turnover": 2,
                "fg3m": 2,
            },
        ])
        svc = _make_service(
            game_states={"DAL": gs, "LAL": gs},
            bdl_service=bdl,
        )

        count = await svc.fetch_bdl_live_stats("2026-02-25")
        assert count == 2

        # Verify stats are in memory
        stats = await svc._get_live_stats()
        assert len(stats) >= 2

    @pytest.mark.asyncio
    async def test_fetch_skips_without_bdl(self):
        """No BDL service → returns 0 without errors."""
        svc = _make_service()
        count = await svc.fetch_bdl_live_stats("2026-02-25")
        assert count == 0

    @pytest.mark.asyncio
    async def test_fetch_skips_no_active_games(self):
        """Pre-game (no active games) → returns 0."""
        gs = MockGameState(
            bdl_game_id=5001,
            home_team="DAL",
            visitor_team="LAL",
            has_started=False,
            period=0,
            status_detail="7:00 PM",
        )
        bdl = MockBDLService()
        svc = _make_service(
            game_states={"DAL": gs, "LAL": gs},
            bdl_service=bdl,
        )
        count = await svc.fetch_bdl_live_stats("2026-02-25")
        assert count == 0

    @pytest.mark.asyncio
    async def test_auto_fetch_on_snapshot(self):
        """Snapshot should auto-fetch BDL stats when cache is empty."""
        gs = MockGameState(
            bdl_game_id=5001,
            home_team="DAL",
            visitor_team="LAL",
            has_started=True,
            period=2,
            status_detail="2nd Qtr",
            home_score=52,
            visitor_score=48,
        )
        bdl = MockBDLService(box_scores=[
            {
                "player": {"first_name": "Luka", "last_name": "Doncic"},
                "team": {"abbreviation": "DAL"},
                "game": {"id": 5001},
                "min": "15:00",
                "pts": 18,
                "reb": 5,
                "ast": 4,
                "stl": 1, "blk": 0, "turnover": 2, "fg3m": 3,
            },
        ])
        props = {
            "luka doncic:DAL": MockPlayerProps(
                "Luka Doncic", "DAL",
                [MockPropLine("pra", 48.5)],
            ),
        }
        svc = _make_service(
            game_states={"DAL": gs, "LAL": gs},
            dk_props=props,
            bdl_service=bdl,
        )

        # No manual ingestion — snapshot should auto-fetch from BDL
        snapshot = await svc.get_tracker_snapshot("2026-02-25")
        assert len(snapshot.players) == 1
        t = snapshot.players[0]
        assert t.source == "live"
        assert t.current_value == 27.0  # 18 + 5 + 4
        assert t.remaining_requirement == 21.5  # 48.5 - 27.0
