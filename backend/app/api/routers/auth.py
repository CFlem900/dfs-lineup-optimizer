"""OAuth authentication router.

Endpoints:
    GET  /auth/providers           — List available OAuth providers
    GET  /auth/login/{provider}    — Redirect to provider's login page
    GET  /auth/callback/{provider} — Handle provider callback, create session
    POST /auth/logout              — Revoke current session
    GET  /auth/me                  — Return current user info
"""

import logging
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from itsdangerous import URLSafeSerializer
from sqlalchemy import select, update

from app.config import get_settings
from app.api.oauth_providers import oauth, get_available_providers
from app.db.database import get_session as get_db_session, is_db_available
from app.db.models import User, UserSession

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])


# ── Session Token Helpers ────────────────────────────────────────


def _get_serializer() -> URLSafeSerializer:
    settings = get_settings()
    return URLSafeSerializer(settings.session_secret_key, salt="session")


def _generate_session_token() -> str:
    raw = secrets.token_urlsafe(32)
    return _get_serializer().dumps(raw)


def _validate_session_token(token: str) -> bool:
    try:
        _get_serializer().loads(token)
        return True
    except Exception:
        return False


# ── Endpoints ────────────────────────────────────────────────────


@router.get("/providers")
async def list_providers():
    """Return available OAuth providers."""
    return {"providers": get_available_providers()}


@router.get("/login/{provider}")
async def oauth_login(provider: str, request: Request):
    """Initiate OAuth login flow — redirects browser to provider."""
    available = get_available_providers()
    if provider not in available:
        raise HTTPException(status_code=400, detail=f"Provider '{provider}' is not configured")

    settings = get_settings()
    redirect_uri = f"{settings.oauth_redirect_base_url}/api/auth/callback/{provider}"

    client = oauth.create_client(provider)
    return await client.authorize_redirect(request, redirect_uri)


@router.get("/callback/{provider}")
async def oauth_callback(provider: str, request: Request):
    """Handle the OAuth provider callback.

    1. Exchange code for token
    2. Fetch user info from provider
    3. Upsert User row
    4. Create UserSession row
    5. Set HTTP-only session cookie
    6. Redirect to frontend
    """
    available = get_available_providers()
    if provider not in available:
        raise HTTPException(status_code=400, detail=f"Provider '{provider}' is not configured")

    settings = get_settings()
    client = oauth.create_client(provider)

    try:
        token = await client.authorize_access_token(request)
    except Exception as exc:
        logger.error("OAuth token exchange failed for %s: %s", provider, exc)
        return RedirectResponse(f"{settings.frontend_url}?auth_error=token_exchange_failed")

    # Extract user info based on provider
    email, display_name, avatar_url, provider_id = await _extract_user_info(
        client, provider, token
    )

    if not email:
        logger.warning("OAuth callback for %s: no email returned", provider)
        return RedirectResponse(f"{settings.frontend_url}?auth_error=no_email")

    if not is_db_available():
        raise HTTPException(status_code=503, detail="Database unavailable")

    # Upsert user + create session
    async with get_db_session() as db:
        stmt = select(User).where(User.email == email)
        result = await db.execute(stmt)
        user = result.scalar_one_or_none()

        now = datetime.now(timezone.utc)
        if user is None:
            user = User(
                email=email,
                display_name=display_name,
                avatar_url=avatar_url,
                oauth_provider=provider,
                oauth_provider_id=str(provider_id),
                last_login_at=now,
            )
            db.add(user)
        else:
            user.display_name = display_name or user.display_name
            user.avatar_url = avatar_url or user.avatar_url
            user.oauth_provider = provider
            user.oauth_provider_id = str(provider_id)
            user.last_login_at = now

        await db.flush()

        session_token = _generate_session_token()
        expires_at = now + timedelta(seconds=settings.session_max_age_seconds)
        db_session = UserSession(
            session_token=session_token,
            user_id=user.id,
            expires_at=expires_at,
        )
        db.add(db_session)
        await db.commit()

    # Set cookie and redirect to frontend
    response = RedirectResponse(settings.frontend_url, status_code=302)
    response.set_cookie(
        key="session",
        value=session_token,
        max_age=settings.session_max_age_seconds,
        httponly=True,
        samesite="lax",
        secure=settings.environment == "production",
        path="/",
    )
    return response


@router.post("/logout")
async def logout(request: Request):
    """Revoke the current session and clear the cookie."""
    session_token = request.cookies.get("session")
    if session_token and is_db_available():
        async with get_db_session() as db:
            stmt = (
                update(UserSession)
                .where(UserSession.session_token == session_token)
                .values(is_revoked=True)
            )
            await db.execute(stmt)
            await db.commit()

    response = Response(content='{"ok": true}', media_type="application/json")
    response.delete_cookie("session", path="/")
    return response


@router.get("/me")
async def get_current_user(request: Request):
    """Return the authenticated user's profile, or 401."""
    user = await _get_user_from_request(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return {
        "id": user.id,
        "email": user.email,
        "display_name": user.display_name,
        "avatar_url": user.avatar_url,
        "provider": user.oauth_provider,
    }


# ── Internal Helpers ─────────────────────────────────────────────


async def _extract_user_info(client, provider: str, token: dict):
    """Extract (email, display_name, avatar_url, provider_id) from OAuth token."""
    if provider == "google":
        user_info = token.get("userinfo", {})
        if not user_info:
            user_info = await client.userinfo()
        return (
            user_info.get("email"),
            user_info.get("name"),
            user_info.get("picture"),
            user_info.get("sub"),
        )

    elif provider == "github":
        resp = await client.get("user", token=token)
        profile = resp.json()
        email = profile.get("email")
        if not email:
            email_resp = await client.get("user/emails", token=token)
            emails = email_resp.json()
            primary = next((e for e in emails if e.get("primary")), None)
            email = primary["email"] if primary else None
        return (
            email,
            profile.get("name") or profile.get("login"),
            profile.get("avatar_url"),
            profile.get("id"),
        )

    elif provider == "discord":
        resp = await client.get("users/@me", token=token)
        profile = resp.json()
        avatar_hash = profile.get("avatar")
        avatar_url = None
        if avatar_hash:
            avatar_url = f"https://cdn.discordapp.com/avatars/{profile['id']}/{avatar_hash}.png"
        return (
            profile.get("email"),
            profile.get("global_name") or profile.get("username"),
            avatar_url,
            profile.get("id"),
        )

    elif provider == "microsoft":
        user_info = token.get("userinfo", {})
        if not user_info:
            user_info = await client.userinfo()
        return (
            user_info.get("email"),
            user_info.get("name"),
            None,
            user_info.get("sub"),
        )

    elif provider == "twitter":
        resp = await client.get("users/me", token=token,
                                params={"user.fields": "profile_image_url"})
        data = resp.json().get("data", {})
        # Twitter v2 does not reliably return email; use username as fallback
        return (
            data.get("email"),
            data.get("name"),
            data.get("profile_image_url"),
            data.get("id"),
        )

    elif provider == "facebook":
        resp = await client.get("me", token=token,
                                params={"fields": "id,name,email,picture.type(large)"})
        profile = resp.json()
        picture_url = None
        pic_data = profile.get("picture", {}).get("data", {})
        if pic_data:
            picture_url = pic_data.get("url")
        return (
            profile.get("email"),
            profile.get("name"),
            picture_url,
            profile.get("id"),
        )

    elif provider == "instagram":
        # Instagram auth goes through Facebook OAuth; fetch user via Graph API
        resp = await client.get("me", token=token,
                                params={"fields": "id,name,email"})
        profile = resp.json()
        return (
            profile.get("email"),
            profile.get("name"),
            None,
            profile.get("id"),
        )

    return (None, None, None, None)


async def _get_user_from_request(request: Request):
    """Look up user from session cookie. Returns User or None."""
    session_token = request.cookies.get("session")
    if not session_token:
        return None
    if not _validate_session_token(session_token):
        return None
    if not is_db_available():
        return None

    async with get_db_session() as db:
        stmt = (
            select(User)
            .join(UserSession, UserSession.user_id == User.id)
            .where(UserSession.session_token == session_token)
            .where(UserSession.is_revoked == False)  # noqa: E712
            .where(UserSession.expires_at > datetime.now(timezone.utc))
        )
        result = await db.execute(stmt)
        return result.scalar_one_or_none()
