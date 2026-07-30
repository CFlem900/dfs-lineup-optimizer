"""Live Prop Tracker endpoints — real-time prop monitoring during games.

Provides:
  - ``GET /live-props/tracker``     — full snapshot of all tracked props
  - ``POST /live-props/ingest``     — inject live player stats from external source
  - ``GET /live-props/stream``      — SSE stream for dashboard auto-refresh
  - ``GET /live-props/player/{name}`` — single-player tracker detail
"""

import asyncio
import logging
import time
from datetime import date, datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Query, Request

from app.api.dependencies import get_services
from app.api.sse_helpers import format_named_event, sse_stream
from app.services.live_prop_tracker_service import (
    LiveStatsIngestRequest,
    PlayerPropTracker,
    PropTrackerSnapshot,
)

logger = logging.getLogger(__name__)
router = APIRouter()


# ── GET /live-props/tracker ───────────────────────────────────────────────


@router.get("/live-props/tracker", response_model=PropTrackerSnapshot)
async def get_prop_tracker(
    game_date: Optional[str] = Query(
        None, description="Date in YYYY-MM-DD format. Defaults to today."
    ),
):
    """Get a full snapshot of all tracked player props.

    Combines DraftKings prop lines, live game states from BDL, and
    any ingested live player stats to produce pace tracking for every
    player with a prop line tonight.
    """
    svc = get_services()
    target = game_date or date.today().isoformat()
    return await svc.live_prop_tracker_service.get_tracker_snapshot(target)


# ── POST /live-props/ingest ───────────────────────────────────────────────


@router.post("/live-props/ingest")
async def ingest_live_stats(body: LiveStatsIngestRequest):
    """Inject live player box-score stats from an external source.

    Stats are cached with a 5-minute TTL and used by the tracker
    snapshot endpoint for precise pace calculations.

    Accepts a list of player stat snapshots (pts, reb, ast, minutes, etc.).
    """
    svc = get_services()
    count = await svc.live_prop_tracker_service.ingest_live_stats(
        body.game_date, body.players
    )
    return {
        "ingested": count,
        "game_date": body.game_date,
        "source": body.source,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ── POST /live-props/refresh ──────────────────────────────────────────────


@router.post("/live-props/refresh")
async def refresh_bdl_stats(
    game_date: Optional[str] = Query(
        None, description="Date in YYYY-MM-DD format. Defaults to today."
    ),
):
    """Pull live box-score stats from BallDontLie for all active games.

    Fetches player-level stats from BDL for every in-progress or final
    game and stores them in the tracker cache.  Call this to force-refresh
    before checking the tracker snapshot.
    """
    svc = get_services()
    target = game_date or date.today().isoformat()
    count = await svc.live_prop_tracker_service.fetch_bdl_live_stats(target)
    return {
        "fetched": count,
        "game_date": target,
        "source": "bdl",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ── GET /live-props/stream ────────────────────────────────────────────────


@router.get("/live-props/stream")
async def stream_prop_tracker(
    request: Request,
    game_date: Optional[str] = Query(
        None, description="Date in YYYY-MM-DD format."
    ),
    poll_interval: int = Query(
        30,
        ge=10,
        le=120,
        description="Seconds between update polls.",
    ),
):
    """Stream live prop tracker updates via Server-Sent Events.

    The stream sends ``event: update`` messages every ``poll_interval``
    seconds with the full ``PropTrackerSnapshot``.  Sends ``event: done``
    when all games are final, then closes the stream.

    Connect from a browser with ``EventSource('/api/live-props/stream')``.
    """
    svc = get_services()
    target = game_date or date.today().isoformat()

    def _poll_worker(put, cancelled):
        """Sync worker that polls the tracker on an interval."""
        while not cancelled.is_set():
            try:
                snapshot = asyncio.run(
                    svc.live_prop_tracker_service.get_tracker_snapshot(target)
                )
                put({
                    "event": "update",
                    "data": snapshot.model_dump(mode="json"),
                })

                # Check if all games are final
                if snapshot.games and all(
                    g.status == "final" for g in snapshot.games
                ):
                    put({
                        "event": "done",
                        "data": {"message": "All games final"},
                    })
                    return

            except Exception as e:
                logger.error(f"[LiveProps SSE] Poll error: {e}")
                put({
                    "event": "error",
                    "data": {"detail": str(e)},
                })
                return

            # Sleep in 1-second increments so cancellation is responsive
            for _ in range(poll_interval):
                if cancelled.is_set():
                    return
                time.sleep(1)

    return await sse_stream(
        request,
        _poll_worker,
        timeout_s=14400.0,  # 4 hours — covers a full NBA slate
        format_event=format_named_event,
    )


# ── GET /live-props/player/{player_name} ──────────────────────────────────


@router.get(
    "/live-props/player/{player_name}",
    response_model=List[PlayerPropTracker],
)
async def get_player_prop_tracker(
    player_name: str,
    game_date: Optional[str] = Query(
        None, description="Date in YYYY-MM-DD format."
    ),
    team: Optional[str] = Query(
        None, description="Team abbreviation filter (e.g., 'DAL')."
    ),
):
    """Get tracking detail for a single player across all their props.

    Returns a list of ``PlayerPropTracker`` objects — one per tracked
    stat category (pts, reb, ast, pra).
    """
    svc = get_services()
    target = game_date or date.today().isoformat()

    snapshot = await svc.live_prop_tracker_service.get_tracker_snapshot(target)

    # Filter to the requested player
    results = []
    search = player_name.lower()
    for t in snapshot.players:
        if search in t.player_name.lower():
            if team and t.team_abbreviation.upper() != team.upper():
                continue
            results.append(t)

    return results
