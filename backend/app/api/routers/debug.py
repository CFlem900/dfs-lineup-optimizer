"""Debug / dev-only endpoints.

These routes only mount when the ``DEBUG_MODE`` env var is truthy. They
let us exercise data paths (NFL/MLB ingestion, in particular) without a
live DraftKings connection — critical during the offseason when no
real slates exist for the sport you're trying to test.

Mounted from :mod:`app.main`::

    if os.environ.get("DEBUG_MODE", "").lower() in ("1", "true", "yes"):
        from app.api.routers.debug import router as debug_router
        app.include_router(debug_router, prefix="/api")

Endpoints:

  POST /api/debug/mock-ingest
    Body: ``{"draft_group_id": int, "sport": str, "payload": {...}}``
    Parses ``payload`` (a real DK draftables JSON shape) through the
    sport-aware parser and writes the result into the live
    ``DKDraftablesService`` cache. Subsequent ``get_draftables(dg)``
    calls hit the mock data, allowing the lineup builder, scoreboard,
    and player-pool endpoints to exercise the full pipeline.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.api.auth import require_api_key
from app.api.dependencies import get_services

logger = logging.getLogger(__name__)
router = APIRouter()


# ─────────────────────────────────────────────────────────────────────
# /api/debug/mock-ingest
# ─────────────────────────────────────────────────────────────────────


class MockIngestRequest(BaseModel):
    """Body for POST /api/debug/mock-ingest."""

    draft_group_id: int = Field(
        ...,
        description=(
            "DraftGroup ID to associate the payload with. The mock data "
            "is keyed by this ID in the in-memory draftables cache so "
            "subsequent reads with the same dg_id return the mock."
        ),
    )
    sport: str = Field(
        default="nba",
        description="Sport code (nba/cbb/nfl/mlb). Used for pos_to_class lookup.",
    )
    payload: Dict[str, Any] = Field(
        ...,
        description=(
            "Raw DK draftables JSON payload — the same shape returned "
            "by https://api.draftkings.com/draftgroups/v1/draftgroups/"
            "<dg>/draftables. Must contain a top-level 'draftables' "
            "array with entries that have at minimum 'displayName', "
            "'position', 'salary', 'teamAbbreviation', and 'draftableId'."
        ),
    )


class MockIngestResponse(BaseModel):
    draft_group_id: int
    sport: str
    parsed_count: int
    by_position: Dict[str, int]
    by_scoring_class: Dict[str, int]
    sample: list


@router.post("/debug/mock-ingest", response_model=MockIngestResponse)
async def mock_ingest_draftables(
    body: MockIngestRequest,
    _auth=Depends(require_api_key),
):
    """Inject a static DK draftables payload into the per-DG cache.

    Lets the offseason / no-slate workflow validate parsing, scoring
    routing, and downstream consumers (lineup builder, player pool)
    without depending on a live DraftKings response.

    Verifies the parse by returning a position breakdown and a
    scoring-class breakdown — the latter is non-empty only for MLB
    (where ``pos_to_class`` is populated).
    """
    svc = get_services()
    try:
        players = svc.dk_draftables_service.inject_mock_payload(
            draft_group_id=body.draft_group_id,
            data=body.payload,
            sport=body.sport,
        )
    except Exception as exc:
        logger.error(f"[MockIngest] Failed to parse payload: {exc}", exc_info=True)
        raise HTTPException(status_code=400, detail=f"Parse failed: {exc}")

    by_pos: Dict[str, int] = {}
    by_class: Dict[str, int] = {}
    for p in players:
        by_pos[p.position] = by_pos.get(p.position, 0) + 1
        cls = p.scoring_class or "(none)"
        by_class[cls] = by_class.get(cls, 0) + 1

    sample = [
        {
            "dk_player_id": p.dk_player_id,
            "name": p.display_name,
            "position": p.position,
            "team": p.team_abbreviation,
            "salary": p.salary,
            "scoring_class": p.scoring_class,
        }
        for p in players[:5]
    ]

    return MockIngestResponse(
        draft_group_id=body.draft_group_id,
        sport=body.sport,
        parsed_count=len(players),
        by_position=by_pos,
        by_scoring_class=by_class,
        sample=sample,
    )


@router.get("/debug/mock-ingest/status/{draft_group_id}")
async def mock_ingest_status(
    draft_group_id: int,
    _auth=Depends(require_api_key),
):
    """Report what mock data (if any) is currently cached for a dg_id."""
    svc = get_services()
    cached = svc.dk_draftables_service._cache.get(draft_group_id, [])
    return {
        "draft_group_id": draft_group_id,
        "cached_count": len(cached),
        "by_position": {
            p.position: sum(1 for x in cached if x.position == p.position)
            for p in cached
        },
    }


@router.delete("/debug/mock-ingest/{draft_group_id}")
async def mock_ingest_clear(
    draft_group_id: int,
    _auth=Depends(require_api_key),
):
    """Drop a mock payload so the next read re-fetches from DK live."""
    svc = get_services()
    removed = svc.dk_draftables_service._cache.pop(draft_group_id, None)
    return {
        "draft_group_id": draft_group_id,
        "removed_count": len(removed) if removed else 0,
    }
