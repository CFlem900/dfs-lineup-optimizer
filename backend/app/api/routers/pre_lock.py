"""Pre-lock polling service monitoring endpoints."""

import logging

from fastapi import APIRouter, Depends, Request

from app.api.auth import require_admin_key
from app.api.dependencies import get_services
from app.api.rate_limiter import limiter

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/admin/pre-lock/status")
async def pre_lock_status(_auth=Depends(require_admin_key)):
    """Get the current pre-lock polling service status.

    Returns running state, active window, slate schedule with lock
    times, last poll results, and recent errors.
    """
    svc = get_services()
    return svc.pre_lock_polling_service.get_status()


@router.post("/admin/pre-lock/force-poll")
@limiter.limit("5/minute")
async def force_poll(request: Request, _auth=Depends(require_admin_key)):
    """Force an immediate poll cycle regardless of active window.

    Triggers injury sync + news refresh and returns the updated status.
    """
    svc = get_services()
    await svc.pre_lock_polling_service._poll_cycle()
    return {
        "status": "ok",
        "result": svc.pre_lock_polling_service.get_status(),
    }
