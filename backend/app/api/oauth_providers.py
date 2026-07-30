"""OAuth provider configuration using authlib.

Each provider is independently enabled by having both client_id and
client_secret set.  Call ``register_providers()`` once at startup.
"""

import logging

from authlib.integrations.starlette_client import OAuth

from app.config import get_settings

logger = logging.getLogger(__name__)

oauth = OAuth()

# Provider definitions: (name, config_builder)
_PROVIDER_DEFS = {
    "google": lambda s: dict(
        client_id=s.google_client_id,
        client_secret=s.google_client_secret,
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    ),
    "github": lambda s: dict(
        client_id=s.github_client_id,
        client_secret=s.github_client_secret,
        authorize_url="https://github.com/login/oauth/authorize",
        access_token_url="https://github.com/login/oauth/access_token",
        api_base_url="https://api.github.com/",
        client_kwargs={"scope": "read:user user:email"},
    ),
    "discord": lambda s: dict(
        client_id=s.discord_client_id,
        client_secret=s.discord_client_secret,
        authorize_url="https://discord.com/api/oauth2/authorize",
        access_token_url="https://discord.com/api/oauth2/token",
        api_base_url="https://discord.com/api/",
        client_kwargs={"scope": "identify email"},
    ),
    "microsoft": lambda s: dict(
        client_id=s.microsoft_client_id,
        client_secret=s.microsoft_client_secret,
        server_metadata_url="https://login.microsoftonline.com/common/v2.0/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    ),
    "twitter": lambda s: dict(
        client_id=s.twitter_client_id,
        client_secret=s.twitter_client_secret,
        authorize_url="https://twitter.com/i/oauth2/authorize",
        access_token_url="https://api.twitter.com/2/oauth2/token",
        api_base_url="https://api.twitter.com/2/",
        client_kwargs={
            "scope": "users.read tweet.read",
            "code_challenge_method": "S256",
        },
    ),
    "facebook": lambda s: dict(
        client_id=s.facebook_client_id,
        client_secret=s.facebook_client_secret,
        authorize_url="https://www.facebook.com/v19.0/dialog/oauth",
        access_token_url="https://graph.facebook.com/v19.0/oauth/access_token",
        api_base_url="https://graph.facebook.com/v19.0/",
        client_kwargs={"scope": "email public_profile"},
    ),
    "instagram": lambda s: dict(
        client_id=s.instagram_client_id,
        client_secret=s.instagram_client_secret,
        authorize_url="https://www.facebook.com/v19.0/dialog/oauth",
        access_token_url="https://graph.facebook.com/v19.0/oauth/access_token",
        api_base_url="https://graph.facebook.com/v19.0/",
        client_kwargs={"scope": "email public_profile instagram_basic"},
    ),
}

# Maps provider name -> (client_id_attr, client_secret_attr)
_PROVIDER_CREDS = {
    "google": ("google_client_id", "google_client_secret"),
    "github": ("github_client_id", "github_client_secret"),
    "discord": ("discord_client_id", "discord_client_secret"),
    "microsoft": ("microsoft_client_id", "microsoft_client_secret"),
    "twitter": ("twitter_client_id", "twitter_client_secret"),
    "facebook": ("facebook_client_id", "facebook_client_secret"),
    "instagram": ("instagram_client_id", "instagram_client_secret"),
}


def _is_configured(settings, provider: str) -> bool:
    id_attr, secret_attr = _PROVIDER_CREDS[provider]
    return bool(getattr(settings, id_attr)) and bool(getattr(settings, secret_attr))


def register_providers() -> None:
    """Register all configured OAuth providers. Call once at startup."""
    settings = get_settings()
    for name, builder in _PROVIDER_DEFS.items():
        if _is_configured(settings, name):
            oauth.register(name=name, **builder(settings))
            logger.info("OAuth provider registered: %s", name)


def get_available_providers() -> list[str]:
    """Return names of providers that have credentials configured."""
    settings = get_settings()
    return [name for name in _PROVIDER_DEFS if _is_configured(settings, name)]
