"""DraftKings entry automation endpoints.

Provides endpoints for:
- Checking DK authentication status
- Downloading DK entries templates
- Filling templates with optimised lineups
- Uploading filled entries back to DK
- Full automated download → fill → upload flow
"""

import json
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request

from app.api.sse_helpers import sse_stream, format_named_event
from app.models.dk_entry import (
    DKAutoRequest,
    DKFillRequest,
    DKFillResult,
    DKFullResult,
    DKUploadResult,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/dk-entries")


# ------------------------------------------------------------------
# GET /dk-entries/auth-check — Verify DK authentication
# ------------------------------------------------------------------

@router.get("/auth-check")
async def auth_check():
    """Check if DraftKings session cookies are available and valid.

    Returns ``{authenticated: true/false, cookies_found: [...], error: ...}``
    """
    from app.services.dk_auth import verify_dk_auth
    try:
        result = await verify_dk_auth()
        return result
    except Exception as e:
        logger.error(f"DK auth check failed unexpectedly: {e}", exc_info=True)
        return {
            "authenticated": False,
            "cookies_found": [],
            "error": f"Auth check error: {e}",
        }


# ------------------------------------------------------------------
# POST /dk-entries/login — Interactive DK login via Playwright popup
# ------------------------------------------------------------------

@router.post("/login")
async def dk_login():
    """Open a browser popup for DraftKings login.

    Uses Playwright to open a visible Chromium window at draftkings.com.
    The user must log in manually.  Once authenticated, cookies are
    captured and cached for future use.

    This is needed when Chrome uses v20 App-Bound Encryption (Chrome 127+)
    which prevents automated cookie extraction from the Chrome database.

    Returns ``{authenticated: bool, cookies_found: [...], error: ...}``
    """
    from app.services.dk_auth import login_dk_interactive
    try:
        result = await login_dk_interactive()
        return result
    except Exception as e:
        logger.error(f"DK interactive login failed: {e}", exc_info=True)
        return {
            "authenticated": False,
            "cookies_found": [],
            "error": f"Login error: {e}",
        }


# ------------------------------------------------------------------
# POST /dk-entries/clear-cache — Clear cached DK cookies
# ------------------------------------------------------------------

@router.post("/clear-cache")
async def dk_clear_cache():
    """Clear cached DraftKings cookies, forcing re-extraction on next request."""
    from app.services.dk_auth import clear_cookie_cache
    clear_cookie_cache()
    return {"status": "ok", "message": "DK cookie cache cleared"}


# ------------------------------------------------------------------
# GET /dk-entries/download-template — Download entries CSV (SSE)
# ------------------------------------------------------------------

@router.get("/download-template")
async def download_template(
    request: Request,
    draft_group_id: int = Query(..., description="DK DraftGroup ID"),
    sport: str = Query("cbb", description="Sport: nba or cbb"),
):
    """Download the DK entries template for a given draft group.

    Returns an SSE stream with progress events, then a final ``done`` event
    containing the parsed ``DKEntriesTemplate``.
    """
    from app.services.dk_entry_service import DKEntryService

    svc = DKEntryService()

    def worker(put, cancelled):
        def progress(step, detail):
            if cancelled.is_set():
                return
            put({
                "event": "progress",
                "data": {"step": step, "detail": detail},
            })

        try:
            template = svc.download_entries_template(
                draft_group_id=draft_group_id,
                sport=sport,
                progress=progress,
            )
            put({
                "event": "done",
                "data": json.loads(template.model_dump_json()),
            })
        except Exception as e:
            logger.error(f"DK template download failed: {e}", exc_info=True)
            put({
                "event": "error",
                "data": {"error": str(e)},
            })

    return await sse_stream(request, worker, format_event=format_named_event, timeout_s=120)


# ------------------------------------------------------------------
# POST /dk-entries/fill — Fill template with lineup IDs
# ------------------------------------------------------------------

@router.post("/fill")
async def fill_entries(body: DKFillRequest):
    """Fill a DK entries template with optimised lineup player IDs.

    Accepts a template (from ``/download-template``) and a list of lineups
    (each a list of ``dk_player_id``s in slot order).

    Returns the filled CSV content and metadata.
    """
    from app.services.dk_entry_service import DKEntryService

    svc = DKEntryService()
    try:
        result = svc.fill_entries(
            body.template,
            body.lineup_player_ids,
            lineup_meta=body.lineup_meta,
            selection=body.selection,
        )
        return result.model_dump()
    except Exception as e:
        logger.error(f"DK entry fill failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ------------------------------------------------------------------
# POST /dk-entries/upload — Upload filled CSV to DK (SSE)
# ------------------------------------------------------------------

@router.post("/upload")
async def upload_entries(request: Request):
    """Upload a filled entries CSV to DraftKings.

    Accepts the filled CSV content as the request body text.
    Returns an SSE stream with progress events.
    """
    from app.services.dk_entry_service import DKEntryService

    body = await request.body()
    csv_content = body.decode("utf-8")

    if not csv_content.strip():
        raise HTTPException(status_code=400, detail="Empty CSV content")

    svc = DKEntryService()

    def worker(put, cancelled):
        def progress(step, detail):
            if cancelled.is_set():
                return
            put({
                "event": "progress",
                "data": {"step": step, "detail": detail},
            })

        try:
            result = svc.upload_entries(csv_content, progress=progress)
            put({
                "event": "done",
                "data": json.loads(result.model_dump_json()),
            })
        except Exception as e:
            logger.error(f"DK entry upload failed: {e}", exc_info=True)
            put({
                "event": "error",
                "data": {"error": str(e)},
            })

    return await sse_stream(request, worker, format_event=format_named_event, timeout_s=120)


# ------------------------------------------------------------------
# POST /dk-entries/auto — Full flow: download → fill → upload (SSE)
# ------------------------------------------------------------------

@router.post("/auto")
async def auto_upload(request: Request):
    """Full automated flow: download template → fill with lineups → upload.

    Accepts JSON body with ``draft_group_id``, ``sport``, and
    ``lineup_player_ids`` (list of lineup ID lists in slot order).

    Returns an SSE stream with progress events for each phase.
    """
    from app.services.dk_entry_service import DKEntryService

    body_bytes = await request.body()
    try:
        body = DKAutoRequest.model_validate_json(body_bytes)
    except Exception as e:
        raise HTTPException(status_code=422, detail=str(e))

    svc = DKEntryService()

    def worker(put, cancelled):
        def progress(step, detail):
            if cancelled.is_set():
                return
            put({
                "event": "progress",
                "data": {"step": step, "detail": detail},
            })

        try:
            result = svc.download_fill_upload(
                draft_group_id=body.draft_group_id,
                sport=body.sport,
                lineup_player_ids=body.lineup_player_ids,
                progress=progress,
            )
            put({
                "event": "done",
                "data": json.loads(result.model_dump_json()),
            })
        except Exception as e:
            logger.error(f"DK auto upload failed: {e}", exc_info=True)
            put({
                "event": "error",
                "data": {"error": str(e)},
            })

    return await sse_stream(request, worker, format_event=format_named_event, timeout_s=180)
