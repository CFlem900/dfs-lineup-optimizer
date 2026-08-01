"""Tests for the shared HTTP resilience layer (circuit breaker + retry).

Covers:
  - CircuitBreaker state machine (CLOSED → OPEN → HALF_OPEN → CLOSED)
  - CircuitBreaker diagnostics and reset
  - resilient_get(): success path, retry on timeout/5xx/429
  - resilient_get(): no retry on 4xx (404, 403)
  - resilient_get(): exhausted retries
  - resilient_get(): circuit breaker blocks requests
  - resilient_get(): failure recording after exhaustion
  - Per-group circuit breaker isolation
"""

import time
import pytest
from unittest.mock import patch, MagicMock

import httpx

from app.services.http_resilience import (
    CircuitBreaker,
    CircuitBreakerOpen,
    APIGroup,
    resilient_get,
    get_all_diagnostics,
    reset_breaker,
    _breakers,
    _is_retryable,
)


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def reset_all_breakers():
    """Reset all circuit breakers before and after each test."""
    for breaker in _breakers.values():
        breaker.reset()
    yield
    for breaker in _breakers.values():
        breaker.reset()


def _ok_response(data: dict = None):
    """Create a mock successful httpx.Response."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.json.return_value = data or {}
    resp.raise_for_status = MagicMock()
    return resp


def _error_response(status_code: int):
    """Create a mock error httpx.Response that raises on raise_for_status."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.raise_for_status.side_effect = httpx.HTTPStatusError(
        f"{status_code} Error", request=MagicMock(), response=resp
    )
    return resp


# ── CircuitBreaker state machine ──────────────────────────────────────


class TestCircuitBreakerStates:
    def test_initial_state_closed(self):
        cb = CircuitBreaker(failure_threshold=3, recovery_s=10.0)
        assert cb.state == CircuitBreaker.CLOSED
        assert cb.allow_request() is True

    def test_failures_below_threshold_stay_closed(self):
        cb = CircuitBreaker(failure_threshold=3, recovery_s=10.0)
        cb.record_failure()
        cb.record_failure()
        assert cb.state == CircuitBreaker.CLOSED
        assert cb.allow_request() is True

    def test_failures_at_threshold_opens(self):
        cb = CircuitBreaker(failure_threshold=3, recovery_s=10.0)
        for _ in range(3):
            cb.record_failure()
        assert cb.state == CircuitBreaker.OPEN
        assert cb.allow_request() is False

    def test_open_transitions_to_half_open_after_recovery(self):
        cb = CircuitBreaker(failure_threshold=2, recovery_s=0.1)
        cb.record_failure()
        cb.record_failure()
        assert cb.state == CircuitBreaker.OPEN

        time.sleep(0.15)
        assert cb.state == CircuitBreaker.HALF_OPEN
        assert cb.allow_request() is True

    def test_half_open_success_closes(self):
        cb = CircuitBreaker(failure_threshold=2, recovery_s=0.1)
        cb.record_failure()
        cb.record_failure()
        time.sleep(0.15)
        assert cb.state == CircuitBreaker.HALF_OPEN

        cb.record_success()
        assert cb.state == CircuitBreaker.CLOSED
        assert cb.allow_request() is True

    def test_half_open_failure_reopens(self):
        cb = CircuitBreaker(failure_threshold=2, recovery_s=0.1)
        cb.record_failure()
        cb.record_failure()
        time.sleep(0.15)
        assert cb.state == CircuitBreaker.HALF_OPEN

        # Two more failures to meet threshold again
        cb.record_failure()
        cb.record_failure()
        assert cb.state == CircuitBreaker.OPEN

    def test_success_resets_failure_count(self):
        cb = CircuitBreaker(failure_threshold=3, recovery_s=10.0)
        cb.record_failure()
        cb.record_failure()
        cb.record_success()  # Resets counter
        cb.record_failure()
        assert cb.state == CircuitBreaker.CLOSED  # Only 1 failure since reset


class TestCircuitBreakerDiagnostics:
    def test_diagnostics_shape(self):
        cb = CircuitBreaker(failure_threshold=5, recovery_s=60.0)
        diag = cb.get_diagnostics()
        assert diag["state"] == "CLOSED"
        assert diag["consecutive_failures"] == 0
        assert diag["failure_threshold"] == 5
        assert diag["recovery_seconds"] == 60.0
        assert diag["opened_at"] is None
        assert diag["seconds_since_open"] is None

    def test_diagnostics_when_open(self):
        cb = CircuitBreaker(failure_threshold=2, recovery_s=60.0)
        cb.record_failure()
        cb.record_failure()
        diag = cb.get_diagnostics()
        assert diag["state"] == "OPEN"
        assert diag["consecutive_failures"] == 2
        assert diag["opened_at"] > 0
        assert diag["seconds_since_open"] is not None


class TestCircuitBreakerReset:
    def test_reset_from_open(self):
        cb = CircuitBreaker(failure_threshold=2, recovery_s=60.0)
        cb.record_failure()
        cb.record_failure()
        assert cb.state == CircuitBreaker.OPEN

        cb.reset()
        assert cb.state == CircuitBreaker.CLOSED
        assert cb.allow_request() is True


# ── Retry predicate ──────────────────────────────────────────────────


class TestRetryPredicate:
    def test_timeout_is_retryable(self):
        assert _is_retryable(httpx.TimeoutException("timeout")) is True

    def test_connect_error_is_retryable(self):
        assert _is_retryable(httpx.ConnectError("refused")) is True

    def test_5xx_is_retryable(self):
        resp = MagicMock()
        resp.status_code = 503
        exc = httpx.HTTPStatusError("503", request=MagicMock(), response=resp)
        assert _is_retryable(exc) is True

    def test_429_is_retryable(self):
        resp = MagicMock()
        resp.status_code = 429
        exc = httpx.HTTPStatusError("429", request=MagicMock(), response=resp)
        assert _is_retryable(exc) is True

    def test_404_not_retryable(self):
        resp = MagicMock()
        resp.status_code = 404
        exc = httpx.HTTPStatusError("404", request=MagicMock(), response=resp)
        assert _is_retryable(exc) is False

    def test_403_not_retryable(self):
        resp = MagicMock()
        resp.status_code = 403
        exc = httpx.HTTPStatusError("403", request=MagicMock(), response=resp)
        assert _is_retryable(exc) is False

    def test_generic_exception_not_retryable(self):
        assert _is_retryable(ValueError("bad")) is False


# ── resilient_get() ──────────────────────────────────────────────────


class TestResilientGet:
    @patch("app.services.http_resilience.httpx.get")
    def test_success_single_attempt(self, mock_get):
        """Successful request on first attempt."""
        mock_get.return_value = _ok_response({"ok": True})
        resp = resilient_get("https://example.com", group=APIGroup.DRAFTKINGS)
        assert resp.json() == {"ok": True}
        assert mock_get.call_count == 1

    @patch("app.services.http_resilience.httpx.get")
    def test_retries_on_timeout_then_succeeds(self, mock_get):
        """Retries on timeout, succeeds on 3rd attempt."""
        mock_get.side_effect = [
            httpx.TimeoutException("t1"),
            httpx.TimeoutException("t2"),
            _ok_response({"ok": True}),
        ]
        resp = resilient_get("https://example.com", group=APIGroup.DRAFTKINGS)
        assert resp.json() == {"ok": True}
        assert mock_get.call_count == 3

    @patch("app.services.http_resilience.httpx.get")
    def test_retries_on_5xx_then_succeeds(self, mock_get):
        """Retries on 503, succeeds on 2nd attempt."""
        mock_get.side_effect = [
            _error_response(503),
            _ok_response({"ok": True}),
        ]
        resp = resilient_get("https://example.com", group=APIGroup.ESPN_CBB)
        assert resp.json() == {"ok": True}
        assert mock_get.call_count == 2

    @patch("app.services.http_resilience.httpx.get")
    def test_retries_on_429_then_succeeds(self, mock_get):
        """Retries on 429 rate limit, succeeds on 2nd attempt."""
        mock_get.side_effect = [
            _error_response(429),
            _ok_response({"ok": True}),
        ]
        resp = resilient_get("https://example.com", group=APIGroup.UNDERDOG)
        assert resp.json() == {"ok": True}
        assert mock_get.call_count == 2

    @patch("app.services.http_resilience.httpx.get")
    def test_does_not_retry_404(self, mock_get):
        """404 is not retried — raises immediately after 1 call."""
        mock_get.return_value = _error_response(404)
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            resilient_get("https://example.com", group=APIGroup.DRAFTKINGS)
        assert exc_info.value.response.status_code == 404
        assert mock_get.call_count == 1

    @patch("app.services.http_resilience.httpx.get")
    def test_does_not_retry_403(self, mock_get):
        """403 is not retried — raises immediately after 1 call."""
        mock_get.return_value = _error_response(403)
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            resilient_get("https://example.com", group=APIGroup.DRAFTKINGS)
        assert exc_info.value.response.status_code == 403
        assert mock_get.call_count == 1

    @patch("app.services.http_resilience.httpx.get")
    def test_exhausts_retries_on_persistent_timeout(self, mock_get):
        """After exhausting all retry attempts on timeouts, raises TimeoutException."""
        mock_get.side_effect = httpx.TimeoutException("always timeout")
        with pytest.raises(httpx.TimeoutException):
            resilient_get("https://example.com", group=APIGroup.DRAFTKINGS)
        # DraftKings max_retries was raised 3 -> 4 for transient errors
        # (see _API_CONFIG in app/services/http_resilience.py).
        assert mock_get.call_count == 4  # max_retries=4

    @patch("app.services.http_resilience.httpx.get")
    def test_circuit_breaker_blocks_when_open(self, mock_get):
        """When breaker is OPEN, raises CircuitBreakerOpen without HTTP call."""
        breaker = _breakers[APIGroup.DRAFTKINGS]
        for _ in range(breaker.failure_threshold):
            breaker.record_failure()
        assert breaker.state == CircuitBreaker.OPEN

        with pytest.raises(CircuitBreakerOpen):
            resilient_get("https://example.com", group=APIGroup.DRAFTKINGS)
        mock_get.assert_not_called()

    @patch("app.services.http_resilience.httpx.get")
    def test_records_failure_on_exhaustion(self, mock_get):
        """After exhausting retries, the circuit breaker records a failure."""
        breaker = _breakers[APIGroup.ESPN_CBB]
        initial_failures = breaker.get_diagnostics()["consecutive_failures"]

        mock_get.side_effect = httpx.TimeoutException("always timeout")
        with pytest.raises(httpx.TimeoutException):
            resilient_get("https://example.com", group=APIGroup.ESPN_CBB)

        new_failures = breaker.get_diagnostics()["consecutive_failures"]
        assert new_failures == initial_failures + 1

    @patch("app.services.http_resilience.httpx.get")
    def test_records_success_on_success(self, mock_get):
        """On success, the circuit breaker records success (resets failures)."""
        breaker = _breakers[APIGroup.UNDERDOG]
        breaker.record_failure()
        breaker.record_failure()
        assert breaker.get_diagnostics()["consecutive_failures"] == 2

        mock_get.return_value = _ok_response()
        resilient_get("https://example.com", group=APIGroup.UNDERDOG)
        assert breaker.get_diagnostics()["consecutive_failures"] == 0


# ── Per-group isolation ──────────────────────────────────────────────


class TestPerGroupIsolation:
    @patch("app.services.http_resilience.httpx.get")
    def test_dk_breaker_open_doesnt_affect_espn(self, mock_get):
        """DraftKings breaker open doesn't block ESPN_CBB requests."""
        dk_breaker = _breakers[APIGroup.DRAFTKINGS]
        for _ in range(dk_breaker.failure_threshold):
            dk_breaker.record_failure()
        assert dk_breaker.state == CircuitBreaker.OPEN

        # ESPN should still work
        mock_get.return_value = _ok_response({"espn": True})
        resp = resilient_get("https://espn.com", group=APIGroup.ESPN_CBB)
        assert resp.json() == {"espn": True}

    def test_each_group_has_own_breaker(self):
        """Each API group has a distinct circuit breaker instance."""
        assert _breakers[APIGroup.DRAFTKINGS] is not _breakers[APIGroup.ESPN_CBB]
        assert _breakers[APIGroup.ESPN_CBB] is not _breakers[APIGroup.UNDERDOG]


# ── Diagnostics / reset helpers ──────────────────────────────────────


class TestDiagnosticsAndReset:
    def test_get_all_diagnostics(self):
        result = get_all_diagnostics()
        assert "DraftKings" in result
        assert "ESPN_CBB" in result
        assert "Underdog" in result
        for diag in result.values():
            assert "state" in diag
            assert "consecutive_failures" in diag

    def test_reset_breaker(self):
        breaker = _breakers[APIGroup.DRAFTKINGS]
        for _ in range(breaker.failure_threshold):
            breaker.record_failure()
        assert breaker.state == CircuitBreaker.OPEN

        reset_breaker(APIGroup.DRAFTKINGS)
        assert breaker.state == CircuitBreaker.CLOSED
