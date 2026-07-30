"""Production middleware for the RotationEngine API.

Provides:
- ``RequestTimeoutMiddleware`` — kills requests that exceed a configurable
  time limit (default 60 s) and returns 504 Gateway Timeout.
- ``RequestIDMiddleware`` — attaches a unique ``X-Request-ID`` header to
  every request/response for tracing.
- ``JSONFormatter`` — structured JSON log format for production
  environments (human-readable format in development).
"""

import asyncio
import json
import logging
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response


# ── Default timeout in seconds ────────────────────────────────────────
# Can be overridden per-environment via the REQUEST_TIMEOUT_S env var.
DEFAULT_REQUEST_TIMEOUT_S = 60


class RequestTimeoutMiddleware(BaseHTTPMiddleware):
    """Return a 504 if the response headers aren't produced within
    ``timeout_s`` seconds.

    **Limitation:** Due to how Starlette's ``BaseHTTPMiddleware`` works,
    ``asyncio.wait_for`` cancels the response *coroutine* but the
    underlying handler may continue running in its thread/task.
    The client gets a clean 504 immediately, which prevents hung
    connections, but CPU/network work in the handler is not forcibly
    stopped.  The partial-enrichment time budget in
    ``lineup_optimizer_service`` provides the deeper defence.

    Health-check and admin endpoints are exempt.
    """

    # Paths that are allowed to run longer (background jobs, cache refresh,
    # SSE streaming endpoints that send progressive updates).
    _EXEMPT_PREFIXES = (
        "/health",
        "/api/admin/",
        "/api/player-pool/stream",
        "/api/chat/stream",
        "/api/analyze-lineups/narrative",
        "/api/scoreboard",
        "/api/teams/",             # rotation calls hit NBA API (~10s/team on cold load)
        "/api/optimize-lineup",    # single lineup: pool build + ILP can exceed 60s
        "/api/generate-lineups",   # large lineup counts (150) need >60s
        "/api/dk-entries",         # Playwright SSE streams need >60s
    )

    def __init__(self, app, timeout_s: float = DEFAULT_REQUEST_TIMEOUT_S):
        super().__init__(app)
        self.timeout_s = timeout_s

    async def dispatch(self, request: Request, call_next) -> Response:
        # Skip timeout for health checks and admin endpoints
        path = request.url.path
        if any(path.startswith(p) for p in self._EXEMPT_PREFIXES):
            return await call_next(request)

        try:
            return await asyncio.wait_for(
                call_next(request), timeout=self.timeout_s
            )
        except asyncio.TimeoutError:
            logger = logging.getLogger("app.middleware")
            logger.error(
                f"Request timed out after {self.timeout_s}s: "
                f"{request.method} {path}"
            )
            return JSONResponse(
                status_code=504,
                content={
                    "error": "GatewayTimeout",
                    "detail": (
                        f"Request exceeded {int(self.timeout_s)}s time limit. "
                        "Try a smaller slate or retry later."
                    ),
                    "status_code": 504,
                },
            )


class RequestIDMiddleware(BaseHTTPMiddleware):
    """Generate/propagate an ``X-Request-ID`` on every request.

    If the incoming request already has an ``X-Request-ID`` header
    (e.g. from an upstream proxy), it is reused.  Otherwise a new UUID
    is generated.  The ID is stored on ``request.state.request_id`` and
    echoed back in the response header.

    Also logs basic request timing.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        # Reuse incoming request ID or generate a new one
        request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
        request.state.request_id = request_id

        start = time.perf_counter()
        response = await call_next(request)
        elapsed_ms = round((time.perf_counter() - start) * 1000, 1)

        response.headers["X-Request-ID"] = request_id

        logger = logging.getLogger("app.middleware")
        logger.info(
            f"{request.method} {request.url.path} → {response.status_code} "
            f"({elapsed_ms}ms) [rid={request_id[:8]}]"
        )

        return response


class JSONFormatter(logging.Formatter):
    """Structured JSON log formatter for production environments.

    Emits one JSON object per log line with keys:
    ``timestamp``, ``level``, ``logger``, ``message``, ``request_id``
    (if available in the log record).
    """

    def format(self, record: logging.LogRecord) -> str:
        log_data = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        # Include request_id if thread-local or record has it
        request_id = getattr(record, "request_id", None)
        if request_id:
            log_data["request_id"] = request_id

        # Include exception info if present
        if record.exc_info and record.exc_info[0] is not None:
            log_data["exception"] = self.formatException(record.exc_info)

        return json.dumps(log_data)
