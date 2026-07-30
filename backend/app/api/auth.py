"""Authentication for the RotationEngine API.

Supports two modes:
1. **API key** — legacy X-API-Key header (user + admin tiers)
2. **OAuth session** — HTTP-only ``session`` cookie set after OAuth login

When ``oauth_enabled=True``, the ``require_auth`` dependency validates
the session cookie against the ``user_sessions`` database table.  When
``oauth_enabled=False`` (default), ``require_auth`` passes through,
keeping local development frictionless.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import Request, Security, HTTPException, status
from fastapi.security import APIKeyHeader
from sqlalchemy import select

from app.config import get_settings

logger = logging.getLogger(__name__)

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def require_api_key(
    api_key: Optional[str] = Security(api_key_header),
) -> None:
    """Dependency: require a valid user-level API key.

    If ``api_key`` is not configured (empty string), authentication is
    skipped — this keeps local development frictionless.
    """
    settings = get_settings()
    if not settings.api_key:
        return  # Auth disabled when no key configured
    if not api_key or api_key != settings.api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
        )


async def require_admin_key(
    api_key: Optional[str] = Security(api_key_header),
) -> None:
    """Dependency: require a valid admin-level API key.

    Falls back to checking the user-level key if no separate admin key
    is configured, so a single key can protect everything.
    """
    settings = get_settings()
    expected = settings.admin_api_key or settings.api_key
    if not expected:
        return  # Auth disabled when no key configured
    if not api_key or api_key != expected:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )


async def require_auth(request: Request) -> None:
    """Dependency: require a valid OAuth session cookie.

    When ``oauth_enabled`` is False in settings, authentication is
    skipped and the request passes through (backwards compatible
    for local development).
    """
    settings = get_settings()
    if not settings.oauth_enabled:
        return  # Auth disabled — all requests pass through

    session_token = request.cookies.get("session")
    if not session_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )

    from app.db.database import get_session as get_db_session, is_db_available
    from app.db.models import User, UserSession

    if not is_db_available():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database unavailable",
        )

    async with get_db_session() as db:
        stmt = (
            select(User)
            .join(UserSession, UserSession.user_id == User.id)
            .where(UserSession.session_token == session_token)
            .where(UserSession.is_revoked == False)  # noqa: E712
            .where(UserSession.expires_at > datetime.now(timezone.utc))
        )
        result = await db.execute(stmt)
        user = result.scalar_one_or_none()

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session expired or invalid",
        )
