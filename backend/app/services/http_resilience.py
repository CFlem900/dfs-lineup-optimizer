"""Shared HTTP resilience layer: retry, timeout, and circuit breaker.

Provides ``resilient_get()`` — a drop-in replacement for ``httpx.get()`` that
adds per-API-group circuit breakers and tenacity-powered exponential backoff.

Usage::

    from app.services.http_resilience import resilient_get, APIGroup

    resp = resilient_get(url, group=APIGroup.DRAFTKINGS, headers={...})
    resp.raise_for_status()
    data = resp.json()
"""

from __future__ import annotations

import logging
import threading
import time
from enum import Enum
from typing import Dict, Optional

import httpx
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
    before_sleep_log,
    RetryError,
)

logger = logging.getLogger(__name__)


# ── API Groups ────────────────────────────────────────────────────────


class APIGroup(str, Enum):
    DRAFTKINGS = "DraftKings"
    ESPN_CBB = "ESPN_CBB"
    ESPN_NFL = "ESPN_NFL"
    ESPN_MLB = "ESPN_MLB"
    UNDERDOG = "Underdog"
    BALLDONTLIE = "BallDontLie"
    OPEN_METEO = "OpenMeteo"


_API_CONFIG: Dict[APIGroup, dict] = {
    APIGroup.DRAFTKINGS: {
        "timeout": 12.0,
        "max_retries": 4,         # was 3 — give DK one more retry for transient errors
        "backoff_base": 1.5,      # was 1.0 — longer backoff (1.5s, 3s, 6s, 12s)
        "failure_threshold": 10,   # was 5 — DK props failures should not block FPPG fetch
        "recovery_s": 30.0,       # was 60 — recover faster after transient issues
    },
    APIGroup.ESPN_CBB: {
        "timeout": 15.0,
        "max_retries": 3,
        "backoff_base": 1.5,
        "failure_threshold": 5,
        "recovery_s": 60.0,
    },
    APIGroup.ESPN_NFL: {
        # ESPN's hidden site API for NFL schedules. Same shape and
        # quotas as ESPN_CBB — separate breaker so an outage on one
        # sport's feed doesn't trip the other.
        "timeout": 12.0,
        "max_retries": 3,
        "backoff_base": 1.5,
        "failure_threshold": 5,
        "recovery_s": 60.0,
    },
    APIGroup.ESPN_MLB: {
        "timeout": 12.0,
        "max_retries": 3,
        "backoff_base": 1.5,
        "failure_threshold": 5,
        "recovery_s": 60.0,
    },
    APIGroup.UNDERDOG: {
        "timeout": 15.0,
        "max_retries": 3,
        "backoff_base": 1.0,
        "failure_threshold": 5,
        "recovery_s": 60.0,
    },
    APIGroup.BALLDONTLIE: {
        "timeout": 12.0,
        "max_retries": 5,        # was 3 — 429s need more retries to ride out rate limit windows
        "backoff_base": 1.5,     # was 1.0 — longer exponential backoff (1.5s, 3s, 6s, 12s)
        "failure_threshold": 15,  # was 5 — 429s are transient, don't trip breaker too easily
        "recovery_s": 15.0,      # was 30 — recover faster after transient 429 bursts
    },
    APIGroup.OPEN_METEO: {
        # Open-Meteo (free, no API key) — used for MLB game-time weather
        # forecasts. The free tier is generous (~10k req/day) and the
        # service is generally fast (<500 ms), but we keep the timeout
        # tight so a slow weather call can never bottleneck the
        # scoreboard endpoint. Weather is best-effort: any failure here
        # falls back to ``weather=None`` on the affected GameInfo and
        # the rest of the slate keeps loading.
        "timeout": 5.0,
        "max_retries": 2,
        "backoff_base": 1.0,
        "failure_threshold": 8,
        "recovery_s": 30.0,
    },
}


# ── Circuit Breaker ───────────────────────────────────────────────────


class CircuitBreakerOpen(Exception):
    """Raised when the circuit breaker is open for an API group."""

    def __init__(self, group: APIGroup):
        self.group = group
        super().__init__(f"Circuit breaker OPEN for {group.value}")


class CircuitBreaker:
    """Thread-safe circuit breaker (CLOSED → OPEN → HALF_OPEN state machine).

    Extracted from nba_api_service._CircuitBreaker for reuse across API groups.
    """

    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"

    def __init__(self, failure_threshold: int = 5, recovery_s: float = 60.0):
        self.failure_threshold = failure_threshold
        self.recovery_s = recovery_s
        self._state = self.CLOSED
        self._consecutive_failures = 0
        self._opened_at: float = 0.0
        self._lock = threading.Lock()

    @property
    def state(self) -> str:
        with self._lock:
            if self._state == self.OPEN:
                if time.time() - self._opened_at >= self.recovery_s:
                    self._state = self.HALF_OPEN
            return self._state

    def record_success(self):
        with self._lock:
            self._consecutive_failures = 0
            self._state = self.CLOSED

    def record_failure(self):
        with self._lock:
            self._consecutive_failures += 1
            if self._consecutive_failures >= self.failure_threshold:
                if self._state != self.OPEN:
                    logger.warning(
                        "[CircuitBreaker] OPEN — %d consecutive failures. "
                        "Failing fast for %.0fs.",
                        self._consecutive_failures,
                        self.recovery_s,
                    )
                self._state = self.OPEN
                self._opened_at = time.time()

    def allow_request(self) -> bool:
        return self.state != self.OPEN

    def get_diagnostics(self) -> dict:
        with self._lock:
            if self._state == self.OPEN:
                if time.time() - self._opened_at >= self.recovery_s:
                    self._state = self.HALF_OPEN
            _current = self._state
            return {
                "state": _current,
                "consecutive_failures": self._consecutive_failures,
                "failure_threshold": self.failure_threshold,
                "recovery_seconds": self.recovery_s,
                "opened_at": self._opened_at if _current != self.CLOSED else None,
                "seconds_since_open": (
                    round(time.time() - self._opened_at, 1)
                    if self._opened_at and _current != self.CLOSED
                    else None
                ),
            }

    def reset(self):
        with self._lock:
            self._state = self.CLOSED
            self._consecutive_failures = 0
            self._opened_at = 0.0


# Module-level singletons — one breaker per API group
_breakers: Dict[APIGroup, CircuitBreaker] = {
    group: CircuitBreaker(
        failure_threshold=cfg["failure_threshold"],
        recovery_s=cfg["recovery_s"],
    )
    for group, cfg in _API_CONFIG.items()
}


# ── Retry Predicate ──────────────────────────────────────────────────


def _is_retryable(exc: BaseException) -> bool:
    """Return True for transient errors that should be retried."""
    # Network / timeout errors
    if isinstance(exc, (httpx.TimeoutException, httpx.ConnectError)):
        return True
    # 5xx server errors and 429 rate limiting
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500 or exc.response.status_code == 429
    return False


# ── Main Entry Point ─────────────────────────────────────────────────


def resilient_get(
    url: str,
    *,
    group: APIGroup,
    timeout: Optional[float] = None,
    headers: Optional[Dict] = None,
    follow_redirects: bool = True,
) -> httpx.Response:
    """Drop-in replacement for ``httpx.get()`` with retry + circuit breaker.

    Raises:
        CircuitBreakerOpen: if the group's breaker is OPEN.
        httpx.TimeoutException: after exhausting retries on timeouts.
        httpx.HTTPStatusError: on non-retryable HTTP errors (4xx except 429),
            or after exhausting retries on 5xx/429.
    """
    breaker = _breakers[group]
    config = _API_CONFIG[group]

    if not breaker.allow_request():
        raise CircuitBreakerOpen(group)

    effective_timeout = timeout if timeout is not None else config["timeout"]

    @retry(
        stop=stop_after_attempt(config["max_retries"]),
        wait=wait_exponential(multiplier=config["backoff_base"], min=1, max=10),
        retry=retry_if_exception(_is_retryable),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )
    def _do_get() -> httpx.Response:
        resp = httpx.get(
            url,
            timeout=effective_timeout,
            headers=headers,
            follow_redirects=follow_redirects,
        )
        resp.raise_for_status()
        return resp

    try:
        response = _do_get()
        breaker.record_success()
        return response
    except RetryError as e:
        breaker.record_failure()
        raise e.last_attempt.result() if e.last_attempt.result() is not None else e
    except Exception:
        breaker.record_failure()
        raise


# ── Diagnostics ──────────────────────────────────────────────────────


def get_all_diagnostics() -> Dict[str, dict]:
    """Return circuit breaker state for all API groups."""
    return {g.value: _breakers[g].get_diagnostics() for g in APIGroup}


def reset_breaker(group: APIGroup):
    """Force-reset a circuit breaker to CLOSED."""
    _breakers[group].reset()
    logger.info("[Resilient] Force-reset %s circuit breaker", group.value)
