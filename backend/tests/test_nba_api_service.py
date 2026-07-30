"""Tests for NBAApiService — circuit breaker, rate limiting, retry logic, and API mocking.

Covers:
  - _CircuitBreaker state machine (CLOSED → OPEN → HALF_OPEN → CLOSED)
  - _CircuitBreaker thread safety and diagnostics
  - _retry_request() integration with circuit breaker
  - Rate limiter behavior
  - API endpoint methods with mocked responses
  - _parse_minutes() edge cases
  - find_rotation_cutoff() logic
  - build_player_minutes() with mocked game logs

All network calls are mocked — no real NBA API requests are made.
"""

import threading
import time
from datetime import date
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from app.services.nba_api_service import (
    NBAApiService,
    _CircuitBreaker,
    _circuit_breaker,
    clear_rotation_cache,
    get_circuit_breaker_diagnostics,
    probe_nba_api,
    reset_circuit_breaker,
)
from app.models.player import PlayerMinutes


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_module_circuit_breaker():
    """Reset the module-level circuit breaker before each test."""
    _circuit_breaker.reset()
    yield
    _circuit_breaker.reset()


@pytest.fixture
def cb():
    """Fresh circuit breaker with low threshold for fast testing."""
    return _CircuitBreaker(failure_threshold=3, recovery_s=0.1)


@pytest.fixture
def service():
    """NBAApiService with cleared rotation cache."""
    from app.services import nba_api_service
    nba_api_service._rotation_cache.clear()
    return NBAApiService()


# ---------------------------------------------------------------------------
# _CircuitBreaker — state machine
# ---------------------------------------------------------------------------

class TestCircuitBreakerStates:
    def test_initial_state_is_closed(self, cb):
        assert cb.state == _CircuitBreaker.CLOSED

    def test_stays_closed_below_threshold(self, cb):
        cb.record_failure()
        cb.record_failure()
        assert cb.state == _CircuitBreaker.CLOSED
        assert cb.allow_request() is True

    def test_opens_at_threshold(self, cb):
        for _ in range(3):
            cb.record_failure()
        assert cb.state == _CircuitBreaker.OPEN
        assert cb.allow_request() is False

    def test_transitions_to_half_open_after_recovery(self, cb):
        for _ in range(3):
            cb.record_failure()
        assert cb.state == _CircuitBreaker.OPEN

        # Wait for recovery window
        time.sleep(0.15)
        assert cb.state == _CircuitBreaker.HALF_OPEN
        assert cb.allow_request() is True

    def test_half_open_success_closes(self, cb):
        for _ in range(3):
            cb.record_failure()
        time.sleep(0.15)
        assert cb.state == _CircuitBreaker.HALF_OPEN

        cb.record_success()
        assert cb.state == _CircuitBreaker.CLOSED
        assert cb.allow_request() is True

    def test_half_open_failure_reopens(self, cb):
        for _ in range(3):
            cb.record_failure()
        time.sleep(0.15)
        assert cb.state == _CircuitBreaker.HALF_OPEN

        # Record enough failures to re-trip
        for _ in range(3):
            cb.record_failure()
        assert cb.state == _CircuitBreaker.OPEN

    def test_success_resets_failure_count(self, cb):
        cb.record_failure()
        cb.record_failure()
        cb.record_success()
        # After success, failure count is 0 — need 3 more to trip
        cb.record_failure()
        cb.record_failure()
        assert cb.state == _CircuitBreaker.CLOSED

    def test_reset_forces_closed(self, cb):
        for _ in range(3):
            cb.record_failure()
        assert cb.state == _CircuitBreaker.OPEN

        cb.reset()
        assert cb.state == _CircuitBreaker.CLOSED
        assert cb.allow_request() is True


class TestCircuitBreakerDiagnostics:
    def test_closed_diagnostics(self, cb):
        diag = cb.get_diagnostics()
        assert diag["state"] == "CLOSED"
        assert diag["consecutive_failures"] == 0
        assert diag["failure_threshold"] == 3
        assert diag["opened_at"] is None

    def test_open_diagnostics(self, cb):
        for _ in range(3):
            cb.record_failure()
        diag = cb.get_diagnostics()
        assert diag["state"] == "OPEN"
        assert diag["consecutive_failures"] == 3
        assert diag["opened_at"] is not None
        assert diag["seconds_since_open"] is not None

    def test_half_open_diagnostics(self, cb):
        for _ in range(3):
            cb.record_failure()
        time.sleep(0.15)
        diag = cb.get_diagnostics()
        assert diag["state"] == "HALF_OPEN"


class TestCircuitBreakerThreadSafety:
    def test_concurrent_failures_trip_correctly(self):
        """Multiple threads recording failures should still trip at threshold."""
        cb = _CircuitBreaker(failure_threshold=10, recovery_s=5.0)
        barrier = threading.Barrier(5)

        def record_failures():
            barrier.wait()
            for _ in range(3):
                cb.record_failure()

        threads = [threading.Thread(target=record_failures) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 5 threads × 3 failures = 15 total, well above threshold of 10
        assert cb.state == _CircuitBreaker.OPEN


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

class TestModuleLevelHelpers:
    def test_get_circuit_breaker_diagnostics(self):
        diag = get_circuit_breaker_diagnostics()
        assert diag["state"] == "CLOSED"

    def test_reset_circuit_breaker(self):
        for _ in range(5):
            _circuit_breaker.record_failure()
        assert _circuit_breaker.state == _CircuitBreaker.OPEN
        reset_circuit_breaker()
        assert _circuit_breaker.state == _CircuitBreaker.CLOSED

    def test_clear_rotation_cache(self):
        from app.services import nba_api_service
        nba_api_service._rotation_cache[123] = (time.time(), [])
        count = clear_rotation_cache()
        assert count == 1
        assert len(nba_api_service._rotation_cache) == 0

    @patch("app.services.nba_api_service.httpx.head")
    def test_probe_nba_api_success(self, mock_head):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_head.return_value = mock_resp
        assert probe_nba_api(timeout_s=1.0) is True

    @patch("app.services.nba_api_service.httpx.head", side_effect=Exception("timeout"))
    def test_probe_nba_api_failure_trips_breaker(self, mock_head):
        result = probe_nba_api(timeout_s=1.0)
        assert result is False
        assert _circuit_breaker.state == _CircuitBreaker.OPEN

    @patch("app.services.nba_api_service.httpx.head")
    def test_probe_nba_api_server_error_trips(self, mock_head):
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_head.return_value = mock_resp
        # status >= 500 triggers the failure path
        result = probe_nba_api(timeout_s=1.0)
        assert result is False


# ---------------------------------------------------------------------------
# _retry_request() — integration with circuit breaker
# ---------------------------------------------------------------------------

class TestRetryRequest:
    def test_successful_call(self, service):
        func = MagicMock(return_value=["data"])
        result = service._retry_request(func)
        assert result == ["data"]
        func.assert_called_once()

    def test_retries_on_failure_then_succeeds(self, service):
        func = MagicMock(side_effect=[Exception("fail"), ["data"]])
        with patch("app.services.nba_api_service.settings") as mock_settings:
            mock_settings.nba_api_max_retries = 2
            mock_settings.nba_api_retry_delay = 0.01
            mock_settings.nba_api_timeout = 5
            result = service._retry_request(func)
        assert result == ["data"]
        assert func.call_count == 2

    def test_raises_after_all_retries_exhausted(self, service):
        func = MagicMock(side_effect=Exception("persistent failure"))
        with patch("app.services.nba_api_service.settings") as mock_settings:
            mock_settings.nba_api_max_retries = 2
            mock_settings.nba_api_retry_delay = 0.01
            mock_settings.nba_api_timeout = 5
            with pytest.raises(Exception, match="persistent failure"):
                service._retry_request(func)
        assert func.call_count == 2

    def test_fails_fast_when_breaker_open(self, service):
        # Trip the module-level breaker
        for _ in range(5):
            _circuit_breaker.record_failure()
        assert _circuit_breaker.state == _CircuitBreaker.OPEN

        func = MagicMock()
        with pytest.raises(RuntimeError, match="circuit breaker is OPEN"):
            service._retry_request(func)
        func.assert_not_called()

    def test_records_success_on_breaker(self, service):
        func = MagicMock(return_value="ok")
        _circuit_breaker.record_failure()  # 1 failure, still closed
        service._retry_request(func)
        diag = _circuit_breaker.get_diagnostics()
        assert diag["consecutive_failures"] == 0

    def test_records_failure_on_breaker(self, service):
        func = MagicMock(side_effect=Exception("fail"))
        with patch("app.services.nba_api_service.settings") as mock_settings:
            mock_settings.nba_api_max_retries = 1
            mock_settings.nba_api_retry_delay = 0.01
            mock_settings.nba_api_timeout = 5
            with pytest.raises(Exception):
                service._retry_request(func)
        diag = _circuit_breaker.get_diagnostics()
        assert diag["consecutive_failures"] >= 1


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------

class TestRateLimiter:
    def test_first_call_no_delay(self, service):
        """First request should proceed without significant delay."""
        service._last_request_time = 0.0
        t0 = time.time()
        service._rate_limit()
        elapsed = time.time() - t0
        assert elapsed < 0.5

    def test_enforces_minimum_interval(self, service):
        """Rapid consecutive calls should be throttled."""
        service._min_request_interval = 0.1
        service._rate_limit()  # first call
        t0 = time.time()
        service._rate_limit()  # should wait ~0.1s
        elapsed = time.time() - t0
        # Allow some tolerance but should have waited
        assert elapsed >= 0.05


# ---------------------------------------------------------------------------
# _parse_minutes()
# ---------------------------------------------------------------------------

class TestParseMinutes:
    def test_none_returns_zero(self):
        assert NBAApiService._parse_minutes(None) == 0.0

    def test_integer(self):
        assert NBAApiService._parse_minutes(32) == 32.0

    def test_float(self):
        assert NBAApiService._parse_minutes(28.5) == 28.5

    def test_colon_format(self):
        result = NBAApiService._parse_minutes("32:30")
        assert abs(result - 32.5) < 0.01

    def test_colon_format_zero_seconds(self):
        assert NBAApiService._parse_minutes("25:00") == 25.0

    def test_string_number(self):
        assert NBAApiService._parse_minutes("36") == 36.0

    def test_empty_string(self):
        assert NBAApiService._parse_minutes("") == 0.0

    def test_invalid_string(self):
        assert NBAApiService._parse_minutes("N/A") == 0.0

    def test_invalid_colon_format(self):
        assert NBAApiService._parse_minutes("abc:def") == 0.0

    def test_zero(self):
        assert NBAApiService._parse_minutes(0) == 0.0


# ---------------------------------------------------------------------------
# find_rotation_cutoff()
# ---------------------------------------------------------------------------

def _make_pm(name: str, season_avg: float) -> PlayerMinutes:
    """Helper to create a PlayerMinutes with the given season_avg."""
    return PlayerMinutes(
        player_id=hash(name) % 100000,
        player_name=name,
        position="G",
        team_id=1,
        minutes_last_5=[season_avg] * 5,
        minutes_last_10=[season_avg] * 10,
        season_avg=season_avg,
        usage_rate=0.20,
    )


class TestFindRotationCutoff:
    def test_fewer_than_min_rotation(self):
        """If fewer players than min_rotation, return all."""
        players = [_make_pm(f"P{i}", 30 - i) for i in range(5)]
        cut, gap, ratio = NBAApiService.find_rotation_cutoff(players)
        assert cut == 5
        assert gap == 0.0

    def test_clear_gap_at_position_9(self):
        """A big drop between player 9 and 10 should cut at 9."""
        avgs = [34, 32, 30, 28, 27, 26, 25, 24, 22, 8, 6, 5, 4]
        players = [_make_pm(f"P{i}", avgs[i]) for i in range(len(avgs))]
        cut, gap, ratio = NBAApiService.find_rotation_cutoff(players)
        # The gap between 22 and 8 is 14 min (64% ratio) — massive cliff
        assert cut == 9
        assert gap > 10

    def test_no_meaningful_gap_defaults_to_10(self):
        """Gradual decline (1 min per player) → no meaningful gap → default 10."""
        avgs = [30, 29, 28, 27, 26, 25, 24, 23, 22, 21, 20, 19, 18]
        players = [_make_pm(f"P{i}", avgs[i]) for i in range(len(avgs))]
        cut, gap, ratio = NBAApiService.find_rotation_cutoff(players)
        assert cut == 10  # Default when no clear gap
        assert gap == 0.0  # Reset because gap didn't meet thresholds

    def test_custom_max_rotation(self):
        """max_rotation caps how far we scan."""
        avgs = [34, 32, 30, 28, 27, 26, 25, 24, 22, 8, 6, 5, 4]
        players = [_make_pm(f"P{i}", avgs[i]) for i in range(len(avgs))]
        # max_rotation=8 means we only scan positions 8-8, missing the cliff at 9
        cut, gap, ratio = NBAApiService.find_rotation_cutoff(
            players, max_rotation=8
        )
        assert cut <= 10  # Won't find the gap at position 9

    def test_empty_players(self):
        cut, gap, ratio = NBAApiService.find_rotation_cutoff([])
        assert cut == 0

    def test_exactly_min_rotation_players(self):
        """Exactly 8 players (min_rotation) → return all."""
        players = [_make_pm(f"P{i}", 30 - i * 2) for i in range(8)]
        cut, gap, ratio = NBAApiService.find_rotation_cutoff(players)
        assert cut == 8


# ---------------------------------------------------------------------------
# API endpoint methods with mocked responses
# ---------------------------------------------------------------------------

class TestMockedAPICalls:
    @patch("app.services.nba_api_service.scoreboardv2.ScoreboardV2")
    def test_get_today_scoreboard(self, mock_sb_class, service):
        mock_sb = MagicMock()
        mock_sb.get_normalized_dict.return_value = {
            "GameHeader": [
                {"GAME_ID": "0022400001", "HOME_TEAM_ID": 1, "VISITOR_TEAM_ID": 2}
            ]
        }
        mock_sb_class.return_value = mock_sb

        result = service.get_today_scoreboard()
        assert len(result) == 1
        assert result[0]["GAME_ID"] == "0022400001"

    @patch("app.services.nba_api_service.commonteamroster.CommonTeamRoster")
    def test_get_team_roster(self, mock_roster_class, service):
        mock_roster = MagicMock()
        mock_roster.get_normalized_dict.return_value = {
            "CommonTeamRoster": [
                {"PLAYER_ID": 101, "PLAYER": "LeBron James", "POSITION": "F"},
                {"PLAYER_ID": 102, "PLAYER": "Anthony Davis", "POSITION": "C"},
            ]
        }
        mock_roster_class.return_value = mock_roster

        result = service.get_team_roster(team_id=1610612747)
        assert len(result) == 2
        assert result[0]["PLAYER"] == "LeBron James"

    @patch("app.services.nba_api_service.playergamelog.PlayerGameLog")
    def test_get_player_game_log(self, mock_log_class, service):
        mock_log = MagicMock()
        mock_log.get_normalized_dict.return_value = {
            "PlayerGameLog": [
                {"GAME_DATE": "2026-02-01", "MIN": 34, "PTS": 28},
                {"GAME_DATE": "2026-01-30", "MIN": 31, "PTS": 22},
            ]
        }
        mock_log_class.return_value = mock_log

        result = service.get_player_game_log(player_id=2544, last_n=2)
        assert len(result) == 2
        assert result[0]["PTS"] == 28

    @patch("app.services.nba_api_service.playergamelog.PlayerGameLog")
    def test_get_player_game_log_respects_last_n(self, mock_log_class, service):
        games = [{"GAME_DATE": f"2026-02-{i:02d}", "MIN": 30} for i in range(1, 21)]
        mock_log = MagicMock()
        mock_log.get_normalized_dict.return_value = {"PlayerGameLog": games}
        mock_log_class.return_value = mock_log

        result = service.get_player_game_log(player_id=101, last_n=5)
        assert len(result) == 5


# ---------------------------------------------------------------------------
# build_player_minutes with mocked game log
# ---------------------------------------------------------------------------

class TestBuildPlayerMinutes:
    def _make_game(self, minutes, pts=15, reb=5, ast=3, matchup="DEN vs. LAL",
                   plus_minus=5, stl=1, blk=0, tov=2, fg3m=2, fga=15, fta=5):
        return {
            "MIN": minutes, "PTS": pts, "REB": reb, "AST": ast,
            "STL": stl, "BLK": blk, "TOV": tov, "FG3M": fg3m,
            "FGA": fga, "FTA": fta,
            "MATCHUP": matchup, "PLUS_MINUS": plus_minus,
        }

    @patch.object(NBAApiService, "_team_id_to_abbreviation", return_value="DEN")
    def test_basic_build(self, mock_abbr, service):
        games = [self._make_game(32 + i) for i in range(15)]
        pm = service._build_player_minutes_from_gamelog(
            player_id=101, player_name="Nikola Jokic",
            position="C", team_id=1610612743,
            birth_date=None, game_log=games,
        )
        assert pm is not None
        assert pm.player_name == "Nikola Jokic"
        assert pm.position == "C"
        assert pm.season_avg > 30
        # USG% computed from game-log FGA/FTA/TOV (no longer mocked)
        assert 0.15 < pm.usage_rate < 0.35

    @patch.object(NBAApiService, "_get_usage_rates", return_value={})
    @patch.object(NBAApiService, "_team_id_to_abbreviation", return_value="DEN")
    def test_empty_game_log_returns_none(self, mock_abbr, mock_usg, service):
        pm = service._build_player_minutes_from_gamelog(
            player_id=101, player_name="Ghost",
            position="G", team_id=1, birth_date=None, game_log=[],
        )
        assert pm is None

    @patch.object(NBAApiService, "_get_usage_rates", return_value={})
    @patch.object(NBAApiService, "_team_id_to_abbreviation", return_value="DEN")
    def test_dnp_games_excluded_from_average(self, mock_abbr, mock_usg, service):
        """Zero-minute games should not drag down the season average."""
        games = [
            self._make_game(0),   # DNP (most recent)
            self._make_game(0),   # DNP
            self._make_game(30),
            self._make_game(28),
            self._make_game(32),
        ]
        pm = service._build_player_minutes_from_gamelog(
            player_id=101, player_name="Test Player",
            position="G", team_id=1, birth_date=None, game_log=games,
        )
        assert pm is not None
        assert pm.season_avg == 30.0  # avg of [30, 28, 32]
        assert pm.recent_dnp_streak == 2  # 2 consecutive recent DNPs

    @patch.object(NBAApiService, "_get_usage_rates", return_value={})
    @patch.object(NBAApiService, "_team_id_to_abbreviation", return_value="DEN")
    def test_trade_filter_drops_old_team_games(self, mock_abbr, mock_usg, service):
        """Games from previous team should be dropped after a trade."""
        games = [
            self._make_game(30, matchup="DEN vs. LAL"),  # Current team
            self._make_game(28, matchup="DEN @ BOS"),     # Current team
            self._make_game(25, matchup="PHI vs. MIA"),   # Old team
            self._make_game(22, matchup="PHI @ CLE"),     # Old team
        ]
        pm = service._build_player_minutes_from_gamelog(
            player_id=101, player_name="Traded Player",
            position="F", team_id=1610612743, birth_date=None, game_log=games,
        )
        assert pm is not None
        # Should only use DEN games (30 and 28), not PHI games
        assert pm.season_avg == 29.0

    @patch.object(NBAApiService, "_get_usage_rates", return_value={})
    @patch.object(NBAApiService, "_team_id_to_abbreviation", return_value="DEN")
    def test_stat_rates_computed(self, mock_abbr, mock_usg, service):
        """Per-minute stat rates should be computed from game log."""
        games = [
            self._make_game(30, pts=20, reb=10, ast=5),
            self._make_game(30, pts=20, reb=10, ast=5),
        ]
        pm = service._build_player_minutes_from_gamelog(
            player_id=101, player_name="Stat Guy",
            position="C", team_id=1, birth_date=None, game_log=games,
        )
        assert pm is not None
        # 40 pts / 60 min = 0.6667 per min (with small-sample regression to prior)
        assert pm.pts_per_min > 0
        assert pm.reb_per_min > 0
        assert pm.ast_per_min > 0

    @patch.object(NBAApiService, "_get_usage_rates", return_value={})
    @patch.object(NBAApiService, "_team_id_to_abbreviation", return_value="DEN")
    def test_small_sample_bayesian_regression(self, mock_abbr, mock_usg, service):
        """Players with < 15 games should be regressed toward position priors."""
        # Only 5 games — well below REGRESSION_THRESHOLD of 15
        games = [self._make_game(30, pts=0, reb=0, ast=0) for _ in range(5)]
        pm = service._build_player_minutes_from_gamelog(
            player_id=101, player_name="Rookie",
            position="PG", team_id=1, birth_date=None, game_log=games,
        )
        assert pm is not None
        # With 0 stats across 5 games, regression to PG priors should
        # pull pts_per_min above 0 (PG prior is ~0.55)
        assert pm.pts_per_min > 0

    @patch.object(NBAApiService, "_get_usage_rates", return_value={})
    @patch.object(NBAApiService, "_team_id_to_abbreviation", return_value="DEN")
    def test_age_parsed_from_birth_date(self, mock_abbr, mock_usg, service):
        games = [self._make_game(30) for _ in range(5)]
        pm = service._build_player_minutes_from_gamelog(
            player_id=101, player_name="Vet",
            position="G", team_id=1,
            birth_date="1990-01-15",
            game_log=games,
        )
        assert pm is not None
        assert pm.age is not None
        assert pm.age >= 35  # Born in 1990, it's 2026

    @patch.object(NBAApiService, "_get_usage_rates", return_value={})
    @patch.object(NBAApiService, "_team_id_to_abbreviation", return_value="DEN")
    def test_age_none_on_bad_birth_date(self, mock_abbr, mock_usg, service):
        games = [self._make_game(30) for _ in range(5)]
        pm = service._build_player_minutes_from_gamelog(
            player_id=101, player_name="Mystery",
            position="G", team_id=1,
            birth_date="not-a-date",
            game_log=games,
        )
        assert pm is not None
        assert pm.age is None


# ---------------------------------------------------------------------------
# Circuit breaker integration: cascading failures
# ---------------------------------------------------------------------------

class TestCircuitBreakerCascade:
    """Test that the circuit breaker protects against cascading API failures."""

    def test_multiple_methods_fail_fast_when_open(self, service):
        """Once breaker trips, ALL API methods should fail fast."""
        for _ in range(5):
            _circuit_breaker.record_failure()

        with pytest.raises(RuntimeError, match="circuit breaker"):
            service.get_today_scoreboard()

        with pytest.raises(RuntimeError, match="circuit breaker"):
            service.get_team_roster(1)

        with pytest.raises(RuntimeError, match="circuit breaker"):
            service.get_player_game_log(1)

    def test_breaker_trips_after_consecutive_api_errors(self, service):
        """Simulate real consecutive API failures tripping the breaker."""
        fail_func = MagicMock(side_effect=Exception("NBA API down"))

        with patch("app.services.nba_api_service.settings") as mock_settings:
            mock_settings.nba_api_max_retries = 1
            mock_settings.nba_api_retry_delay = 0.01
            mock_settings.nba_api_timeout = 5

            # Each call adds 1 failure (max_retries=1 means only 1 attempt)
            for _ in range(5):
                try:
                    service._retry_request(fail_func)
                except Exception:
                    pass

        assert _circuit_breaker.state == _CircuitBreaker.OPEN

        # Next call should fail fast without calling the function
        clean_func = MagicMock(return_value="should not be called")
        with pytest.raises(RuntimeError, match="circuit breaker"):
            service._retry_request(clean_func)
        clean_func.assert_not_called()


# ---------------------------------------------------------------------------
# Trade detection — build_team_rotation with missing DK players
# ---------------------------------------------------------------------------

class TestTradeDetection:
    """Tests for the trade detection feature in build_team_rotation.

    When a DK draftable player isn't on the team's NBA API roster (recently
    traded), the system should:
    1. Detect the missing player by comparing DK names to rotation names
    2. Look up the player's NBA ID via nba_api static data
    3. Fetch their game log and add them to the rotation
    4. Set roster_change_detected=True on the PlayerMinutes
    """

    @pytest.fixture(autouse=True)
    def _enable_nba_api_live(self):
        """Temporarily enable live NBA API path for trade detection tests."""
        import app.services.nba_api_service as _mod
        original = _mod.settings.skip_nba_api_live
        _mod.settings.skip_nba_api_live = False
        yield
        _mod.settings.skip_nba_api_live = original

    def _make_game(self, minutes, matchup="CHI vs. MIA", pts=15, reb=5,
                   ast=3, plus_minus=5, stl=1, blk=0, tov=2, fg3m=2):
        return {
            "MIN": minutes, "PTS": pts, "REB": reb, "AST": ast,
            "STL": stl, "BLK": blk, "TOV": tov, "FG3M": fg3m,
            "MATCHUP": matchup, "PLUS_MINUS": plus_minus,
        }

    @patch.object(NBAApiService, "_get_usage_rates", return_value={})
    @patch.object(NBAApiService, "_team_id_to_abbreviation", return_value="CHI")
    @patch.object(NBAApiService, "get_player_game_log")
    @patch("app.services.nba_api_service.nba_players_static", create=True)
    @patch.object(NBAApiService, "get_team_roster")
    def test_missing_dk_player_detected_and_added(
        self, mock_roster, mock_static, mock_game_log,
        mock_abbr, mock_usg, service
    ):
        """A DK player not on the roster should be fetched via static lookup."""
        # Roster has 2 players (no Dillingham)
        mock_roster.return_value = [
            {"PLAYER_ID": 1, "PLAYER": "Zach LaVine", "POSITION": "SG", "BIRTH_DATE": None},
            {"PLAYER_ID": 2, "PLAYER": "Nikola Vucevic", "POSITION": "C", "BIRTH_DATE": None},
        ]

        # Game logs for roster players
        lavine_games = [self._make_game(34) for _ in range(10)]
        vuc_games = [self._make_game(30) for _ in range(10)]
        dillingham_games = [self._make_game(20, matchup="MIN vs. OKC") for _ in range(10)]

        def side_effect_game_log(player_id, season=None, last_n=82):
            if player_id == 1:
                return lavine_games
            elif player_id == 2:
                return vuc_games
            elif player_id == 1642265:  # Dillingham NBA ID
                return dillingham_games
            return []

        mock_game_log.side_effect = side_effect_game_log

        # Static player lookup finds Dillingham
        with patch("nba_api.stats.static.players.find_players_by_full_name") as mock_find:
            mock_find.return_value = [
                {"id": 1642265, "full_name": "Rob Dillingham",
                 "first_name": "Rob", "last_name": "Dillingham", "is_active": True}
            ]

            rotation = service.build_team_rotation(
                team_id=1610612741,  # CHI
                draftable_names={"Zach LaVine", "Nikola Vucevic", "Rob Dillingham"},
                draftable_positions={"Rob Dillingham": "PG", "Zach LaVine": "SG", "Nikola Vucevic": "C"},
            )

        # Should have 3 players: LaVine, Vucevic, Dillingham
        assert len(rotation) == 3

        # Find Dillingham in the rotation
        dillingham = next(
            (p for p in rotation if "Dillingham" in p.player_name), None
        )
        assert dillingham is not None, (
            f"Dillingham should be in rotation. Got: {[p.player_name for p in rotation]}"
        )
        assert dillingham.roster_change_detected is True
        assert dillingham.position == "PG"

    @patch.object(NBAApiService, "_get_usage_rates", return_value={})
    @patch.object(NBAApiService, "_team_id_to_abbreviation", return_value="CHI")
    @patch.object(NBAApiService, "get_player_game_log")
    @patch.object(NBAApiService, "get_team_roster")
    def test_no_trade_detection_when_all_dk_on_roster(
        self, mock_roster, mock_game_log, mock_abbr, mock_usg, service
    ):
        """No trade detection should fire when all DK players are on the roster."""
        mock_roster.return_value = [
            {"PLAYER_ID": 1, "PLAYER": "Zach LaVine", "POSITION": "SG", "BIRTH_DATE": None},
            {"PLAYER_ID": 2, "PLAYER": "Nikola Vucevic", "POSITION": "C", "BIRTH_DATE": None},
        ]

        games = [self._make_game(30) for _ in range(10)]
        mock_game_log.return_value = games

        rotation = service.build_team_rotation(
            team_id=1610612741,
            draftable_names={"Zach LaVine", "Nikola Vucevic"},
        )

        assert len(rotation) == 2
        for p in rotation:
            assert p.roster_change_detected is False

    @patch.object(NBAApiService, "_get_usage_rates", return_value={})
    @patch.object(NBAApiService, "_team_id_to_abbreviation", return_value="CHI")
    @patch.object(NBAApiService, "get_player_game_log")
    @patch.object(NBAApiService, "get_team_roster")
    def test_trade_detection_graceful_when_static_lookup_fails(
        self, mock_roster, mock_game_log, mock_abbr, mock_usg, service
    ):
        """If static player lookup fails, the existing rotation should be returned."""
        mock_roster.return_value = [
            {"PLAYER_ID": 1, "PLAYER": "Zach LaVine", "POSITION": "SG", "BIRTH_DATE": None},
        ]

        games = [self._make_game(34) for _ in range(10)]
        mock_game_log.return_value = games

        # Static lookup finds nothing
        with patch("nba_api.stats.static.players.find_players_by_full_name") as mock_find:
            mock_find.return_value = []
            with patch("nba_api.stats.static.players.find_players_by_last_name") as mock_last:
                mock_last.return_value = []

                rotation = service.build_team_rotation(
                    team_id=1610612741,
                    draftable_names={"Zach LaVine", "Unknown Player"},
                )

        # Should still have LaVine
        assert len(rotation) == 1
        assert rotation[0].player_name == "Zach LaVine"
        assert rotation[0].roster_change_detected is False


# ---------------------------------------------------------------------------
# Trade filter fallback — recently-traded players with 0 new-team games
# ---------------------------------------------------------------------------

class TestTradeFilterRecentlyTraded:
    """Tests for the recently-traded player fallback in _build_player_minutes_from_gamelog.

    When a player has been traded and has ALL games from the old team
    (0 games on the new team), the system should:
    1. NOT use old-team minutes (misleading — e.g. 4 min bench role)
    2. Use position-based default minutes instead
    3. Preserve per-minute stat rates from old-team data
    4. Set roster_change_detected=True
    """

    def _make_game(self, minutes, matchup="MIN vs. OKC", pts=8, reb=2, ast=3,
                   plus_minus=0, stl=1, blk=0, tov=1, fg3m=1):
        return {
            "MIN": minutes, "PTS": pts, "REB": reb, "AST": ast,
            "STL": stl, "BLK": blk, "TOV": tov, "FG3M": fg3m,
            "MATCHUP": matchup, "PLUS_MINUS": plus_minus,
        }

    @patch.object(NBAApiService, "_get_usage_rates", return_value={999: 0.18})
    @patch.object(NBAApiService, "_team_id_to_abbreviation", return_value="SAS")
    def test_all_pretrade_uses_position_default(self, mock_abbr, mock_usg, service):
        """Player with ALL games from old team should get position-default minutes."""
        from app.services.nba_api_service import _TRADE_DEFAULT_MINUTES

        default_pg = _TRADE_DEFAULT_MINUTES.get("PG", 14.0)
        # 10 MIN games at ~4 min each (bench role)
        games = [self._make_game(4, matchup="MIN vs. OKC") for _ in range(10)]
        pm = service._build_player_minutes_from_gamelog(
            player_id=999, player_name="Rob Dillingham",
            position="PG", team_id=1610612759,  # SAS
            birth_date="2003-01-02", game_log=games,
        )
        assert pm is not None
        # Should use PG position default, NOT old MIN avg (4.0)
        assert pm.season_avg == default_pg
        assert pm.minutes_last_5 == [default_pg] * 5
        assert pm.minutes_last_10 == [default_pg] * 10

    @patch.object(NBAApiService, "_get_usage_rates", return_value={999: 0.18})
    @patch.object(NBAApiService, "_team_id_to_abbreviation", return_value="SAS")
    def test_all_pretrade_preserves_stat_rates(self, mock_abbr, mock_usg, service):
        """Per-minute stat rates should still be computed from old-team data."""
        # Each game: 4 min, 8 pts → 2.0 pts/min (before regression)
        games = [self._make_game(4, matchup="MIN vs. OKC", pts=8, reb=2, ast=3)
                 for _ in range(10)]
        pm = service._build_player_minutes_from_gamelog(
            player_id=999, player_name="Rob Dillingham",
            position="PG", team_id=1610612759,
            birth_date=None, game_log=games,
        )
        assert pm is not None
        # Per-minute rates should be > 0 (computed from old MIN data)
        assert pm.pts_per_min > 0
        assert pm.reb_per_min > 0
        assert pm.ast_per_min > 0

    @patch.object(NBAApiService, "_get_usage_rates", return_value={999: 0.18})
    @patch.object(NBAApiService, "_team_id_to_abbreviation", return_value="SAS")
    def test_all_pretrade_sets_roster_change(self, mock_abbr, mock_usg, service):
        """roster_change_detected should be True for recently-traded players."""
        games = [self._make_game(4, matchup="MIN vs. OKC") for _ in range(10)]
        pm = service._build_player_minutes_from_gamelog(
            player_id=999, player_name="Rob Dillingham",
            position="PG", team_id=1610612759,
            birth_date=None, game_log=games,
        )
        assert pm is not None
        assert pm.roster_change_detected is True
        assert pm.recent_dnp_streak == 0  # Fresh start on new team

    @patch.object(NBAApiService, "_get_usage_rates", return_value={101: 0.22})
    @patch.object(NBAApiService, "_team_id_to_abbreviation", return_value="DEN")
    def test_partial_trade_keeps_new_team_only(self, mock_abbr, mock_usg, service):
        """Existing behavior: player with SOME new-team games keeps only those."""
        games = [
            self._make_game(30, matchup="DEN vs. LAL"),  # New team
            self._make_game(28, matchup="DEN @ BOS"),     # New team
            self._make_game(4, matchup="MIN vs. OKC"),    # Old team
            self._make_game(4, matchup="MIN @ ATL"),      # Old team
        ]
        pm = service._build_player_minutes_from_gamelog(
            player_id=101, player_name="Traded Player",
            position="F", team_id=1610612743,
            birth_date=None, game_log=games,
        )
        assert pm is not None
        # Should use only DEN games (30, 28), not MIN games (4, 4)
        assert pm.season_avg == 29.0
        assert pm.roster_change_detected is False  # Normal trade filter, not fallback

    @patch.object(NBAApiService, "_get_usage_rates", return_value={101: 0.22})
    @patch.object(NBAApiService, "_team_id_to_abbreviation", return_value="DEN")
    def test_no_trade_all_current_team(self, mock_abbr, mock_usg, service):
        """Player with all games on current team — no trade filtering needed."""
        games = [self._make_game(30, matchup="DEN vs. LAL") for _ in range(10)]
        pm = service._build_player_minutes_from_gamelog(
            player_id=101, player_name="Homegrown Star",
            position="SG", team_id=1610612743,
            birth_date=None, game_log=games,
        )
        assert pm is not None
        assert pm.season_avg == 30.0
        assert pm.roster_change_detected is False

    @patch.object(NBAApiService, "_get_usage_rates", return_value={999: 0.18})
    @patch.object(NBAApiService, "_team_id_to_abbreviation", return_value="SAS")
    def test_all_pretrade_center_gets_center_default(self, mock_abbr, mock_usg, service):
        """Center position should get C default, not PG default."""
        from app.services.nba_api_service import _TRADE_DEFAULT_MINUTES

        default_c = _TRADE_DEFAULT_MINUTES.get("C", 12.0)
        games = [self._make_game(8, matchup="MIN vs. OKC") for _ in range(10)]
        pm = service._build_player_minutes_from_gamelog(
            player_id=999, player_name="Traded Center",
            position="C", team_id=1610612759,
            birth_date=None, game_log=games,
        )
        assert pm is not None
        assert pm.season_avg == default_c

    @patch.object(NBAApiService, "_get_usage_rates", return_value={999: 0.18})
    @patch.object(NBAApiService, "_team_id_to_abbreviation", return_value="SAS")
    def test_all_pretrade_with_dnp_games(self, mock_abbr, mock_usg, service):
        """Recently-traded player with DNP games should still get default minutes."""
        from app.services.nba_api_service import _TRADE_DEFAULT_MINUTES

        default_pg = _TRADE_DEFAULT_MINUTES.get("PG", 14.0)
        games = [
            self._make_game(0, matchup="MIN vs. OKC"),   # DNP
            self._make_game(4, matchup="MIN @ ATL"),
            self._make_game(0, matchup="MIN vs. LAL"),   # DNP
            self._make_game(4, matchup="MIN @ BOS"),
        ]
        pm = service._build_player_minutes_from_gamelog(
            player_id=999, player_name="Bench Warmer Traded",
            position="PG", team_id=1610612759,
            birth_date=None, game_log=games,
        )
        assert pm is not None
        assert pm.season_avg == default_pg  # PG default, not old-team avg
        assert pm.roster_change_detected is True
