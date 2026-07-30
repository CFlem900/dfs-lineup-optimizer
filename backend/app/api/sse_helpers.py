"""Shared async SSE streaming helper.

Replaces the per-endpoint ``threading.Thread`` + ``queue.Queue`` boilerplate
with a single reusable ``sse_stream()`` function that:

- Runs synchronous work in a thread via ``asyncio.to_thread``
- Uses ``queue.Queue`` for thread-safe message passing (wrapped with
  ``asyncio.to_thread(q.get)`` so it never blocks the event loop)
- Detects client disconnects via ``request.is_disconnected()``
- Signals cancellation to the worker via ``threading.Event``
- Enforces an overall idle timeout
"""

import asyncio
import json
import logging
import queue
import threading
import time
from typing import Any, Callable, Dict, Optional

from starlette.requests import Request
from starlette.responses import StreamingResponse

logger = logging.getLogger(__name__)

# Standard SSE response headers
SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


# ── Pre-built formatters ─────────────────────────────────────────────


def format_named_event(msg: Dict[str, Any]) -> str:
    """Format as named SSE event: ``event: <name>\\ndata: <json>\\n\\n``.

    Expects *msg* to have ``event`` and ``data`` keys, e.g.::

        {"event": "progress", "data": {"step": "Fetching", "completed": 1, "total": 5}}

    Produces::

        event: progress
        data: {"step": "Fetching", "completed": 1, "total": 5}

    """
    event = msg.get("event", "message")
    data = msg.get("data", {})
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def format_data_event(msg: Dict[str, Any]) -> str:
    """Format as unnamed SSE data line: ``data: <json>\\n\\n``.

    The entire *msg* dict is serialised as-is::

        data: {"chunk": "Hello"}

    """
    return f"data: {json.dumps(msg)}\n\n"


# ── Core helper ──────────────────────────────────────────────────────


async def sse_stream(
    request: Request,
    work_fn: Callable[[Callable[[Dict[str, Any]], None], threading.Event], None],
    *,
    timeout_s: float = 120.0,
    disconnect_poll_s: float = 1.0,
    format_event: Callable[[Dict[str, Any]], str] = format_data_event,
    is_terminal: Optional[Callable[[Dict[str, Any]], bool]] = None,
) -> StreamingResponse:
    """Run *work_fn* in a thread and stream its output as SSE.

    Parameters
    ----------
    request
        The Starlette request, used for ``request.is_disconnected()``.
    work_fn
        A **synchronous** callable with signature
        ``(put: Callable[[dict], None], cancelled: threading.Event) -> None``.
        It should call ``put(msg)`` to enqueue SSE messages and check
        ``cancelled.is_set()`` periodically to support early exit.
    timeout_s
        Maximum seconds of silence (no messages) before the stream is
        terminated with a timeout event.
    disconnect_poll_s
        How often (in seconds) to check for client disconnection between
        queue polls.  Lower values give faster disconnect detection but
        slightly more overhead.
    format_event
        Callable that converts a dict message into an SSE-formatted string.
        Defaults to :func:`format_data_event`.
    is_terminal
        Callable returning ``True`` if the message should be the last one
        yielded.  Defaults to checking for ``event in ("done", "error")``
        or ``msg.get("done") is True`` or ``msg.get("error")`` being truthy.
    """
    q: queue.Queue = queue.Queue()
    cancelled = threading.Event()

    if is_terminal is None:

        def is_terminal(msg: Dict[str, Any]) -> bool:
            return (
                msg.get("event") in ("done", "error")
                or msg.get("done") is True
                or (msg.get("error") is not None and msg.get("error") is not False)
            )

    def _put(msg: Dict[str, Any]) -> None:
        """Enqueue a message (called from the worker thread)."""
        if cancelled.is_set():
            return
        try:
            q.put_nowait(msg)
        except queue.Full:
            logger.warning("SSE queue full — dropping message")

    async def _run_in_thread():
        """Wrapper that runs the sync work_fn via asyncio.to_thread."""
        try:
            await asyncio.to_thread(work_fn, _put, cancelled)
        except Exception as exc:
            logger.error(f"SSE background work failed: {exc}", exc_info=True)
            try:
                q.put_nowait({"event": "error", "data": {"detail": str(exc)}})
            except queue.Full:
                pass

    async def _event_generator():
        task = asyncio.create_task(_run_in_thread())
        last_msg_time = time.monotonic()

        try:
            while True:
                # ── Check client disconnect ──
                if await request.is_disconnected():
                    logger.info("SSE client disconnected — cancelling work")
                    cancelled.set()
                    break

                # ── Poll queue (non-blocking to the event loop) ──
                try:
                    msg = await asyncio.to_thread(
                        q.get, timeout=disconnect_poll_s
                    )
                except queue.Empty:
                    # No message yet — check overall timeout
                    if time.monotonic() - last_msg_time > timeout_s:
                        logger.info(
                            f"SSE idle timeout ({timeout_s}s) — closing stream"
                        )
                        yield format_event(
                            {"event": "timeout", "data": {}}
                        )
                        cancelled.set()
                        break
                    # Check if background task finished without signalling
                    if task.done():
                        break
                    continue

                last_msg_time = time.monotonic()
                yield format_event(msg)

                if is_terminal(msg):
                    break
        except asyncio.CancelledError:
            cancelled.set()
        finally:
            # Ensure the worker is signalled to stop and we wait for it
            cancelled.set()
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                pass

    return StreamingResponse(
        _event_generator(),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )
