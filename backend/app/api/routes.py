"""API route aggregation.

All service instantiation lives in ``app.api.dependencies``.
Domain-specific endpoints live in ``app.api.routers.*``.
This module creates two routers:
- ``auth_router`` — public (login/callback/logout/me/providers)
- ``router`` — protected by ``require_auth`` (all domain endpoints)

Both are mounted under ``/api`` prefix by main.py.
"""

from fastapi import APIRouter, Depends

from app.api.auth import require_auth
from app.api.routers import (
    auth,
    teams,
    simulation,
    signals,
    lineups,
    chat,
    accuracy,
    tournament,
    admin,
    admin_pipeline,
    correlations,
    props,
    underdog,
    dk_entries,
    entries,
    contests,
    pre_lock,
    live_props,
)

# Public router — no auth dependency (OAuth endpoints)
auth_router = APIRouter()
auth_router.include_router(auth.router, tags=["auth"])

# Protected router — requires valid OAuth session (or bypassed when oauth_enabled=False)
router = APIRouter(dependencies=[Depends(require_auth)])
router.include_router(teams.router, tags=["teams"])
router.include_router(simulation.router, tags=["simulation"])
router.include_router(signals.router, tags=["signals"])
router.include_router(lineups.router, tags=["lineups"])
router.include_router(chat.router, tags=["chat"])
router.include_router(accuracy.router, tags=["accuracy"])
router.include_router(tournament.router, tags=["tournament"])
router.include_router(admin.router, tags=["admin"])
router.include_router(admin_pipeline.router, tags=["admin"])
router.include_router(correlations.router, tags=["correlations"])
router.include_router(props.router, tags=["props"])
router.include_router(underdog.router, tags=["underdog"])
router.include_router(dk_entries.router, tags=["dk-entries"])
router.include_router(entries.router, tags=["entries"])
router.include_router(contests.router, tags=["contests"])
router.include_router(pre_lock.router, tags=["admin"])
router.include_router(live_props.router, tags=["live-props"])
