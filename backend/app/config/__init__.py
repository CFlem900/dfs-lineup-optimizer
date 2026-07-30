from pydantic_settings import BaseSettings
from functools import lru_cache
import os

# Ensure .env values override empty system env vars before Settings loads.
# This is needed because pydantic-settings prioritises os.environ over
# .env files, and an empty ANTHROPIC_API_KEY="" in the system environment
# silently shadows the real key in .env.
try:
    from dotenv import load_dotenv as _load_dotenv
    _env_file = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        ".env",
    )
    if os.path.exists(_env_file):
        _load_dotenv(_env_file, override=True)
except Exception:
    pass


class Settings(BaseSettings):
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_db: int = 0
    redis_password: str = ""

    nba_api_timeout: int = 15   # 15s timeout — fail fast on throttled requests
    nba_api_max_retries: int = 2  # 2 retries (down from 3) to avoid long stalls
    nba_api_retry_delay: int = 3  # 3s base delay with exponential backoff

    cache_ttl_baseline: int = 86400
    cache_ttl_rotation: int = 300
    cache_ttl_injuries: int = 600

    environment: str = "development"
    log_level: str = "INFO"

    # ── AI Provider Settings ────────────────────────────────────
    ai_provider: str = "anthropic"          # "openai" | "anthropic"
    ai_enabled: bool = True                 # Global kill switch
    openai_api_key: str = ""
    openai_model_default: str = "gpt-4o-mini"
    openai_model_reasoning: str = "gpt-4o"
    anthropic_api_key: str = ""
    anthropic_model_default: str = "claude-haiku-4-5-20251001"
    anthropic_model_reasoning: str = "claude-sonnet-4-20250514"
    ai_max_retries: int = 3
    ai_timeout: int = 30
    ai_cache_ttl: int = 900                 # 15 min cache for AI responses

    # ── API Settings ─────────────────────────────────────────────────
    cors_origins: str = "http://localhost:5173,http://localhost:3000"
    max_upload_size_mb: int = 10

    # ── Authentication ─────────────────────────────────────────────
    api_key: str = ""              # User-level API key (empty = auth disabled)
    admin_api_key: str = ""        # Admin-level API key (empty = falls back to api_key)

    # ── OAuth ────────────────────────────────────────────────────
    oauth_enabled: bool = False                # Set True to enforce login wall
    session_secret_key: str = ""               # Signs session cookies (required when oauth_enabled)
    session_max_age_seconds: int = 604800      # 7 days

    google_client_id: str = ""
    google_client_secret: str = ""
    github_client_id: str = ""
    github_client_secret: str = ""
    discord_client_id: str = ""
    discord_client_secret: str = ""
    microsoft_client_id: str = ""
    microsoft_client_secret: str = ""
    twitter_client_id: str = ""
    twitter_client_secret: str = ""
    facebook_client_id: str = ""
    facebook_client_secret: str = ""
    instagram_client_id: str = ""
    instagram_client_secret: str = ""

    oauth_redirect_base_url: str = "http://localhost:8000"
    frontend_url: str = "http://localhost:5173"

    # ── Database Pool ──────────────────────────────────────────────
    # Per-worker pool.  With N uvicorn workers, total connections =
    # N × (pool_size + max_overflow).  PostgreSQL default max_connections=100.
    # 2 workers × (5 + 10) = 30 connections — safe headroom.
    db_pool_size: int = 5          # Persistent connections per worker
    db_max_overflow: int = 10      # Additional connections under load
    db_pool_pre_ping: bool = True  # Validate connections before checkout

    # ── External Data APIs ─────────────────────────────────────────
    odds_api_key: str = ""           # The Odds API key (empty = odds disabled)
    balldontlie_api_key: str = ""    # BALLDONTLIE API key (empty = BDL disabled)
    balldontlie_rate_limit: int = 30 # Requests per minute (free=30, ALL-STAR=600)

    # ── Data Retention ──────────────────────────────────────────────
    archive_history_days: int = 730  # 2 years of historical data

    # ── NBA Data Source ──────────────────────────────────────────────
    # When True, never call stats.nba.com from user-facing requests.
    # BallDontLie becomes the sole live source; stats.nba.com is only
    # used in the 4 AM background cache refresh (if at all).
    skip_nba_api_live: bool = True

    # ── Feature Flags ────────────────────────────────────────────────
    # Set any to false in .env to disable that feature at startup.
    # Agent flags gate construction; disabled agents become None and
    # all downstream guards (if agent: / if agent.is_available) skip.
    feature_agent_line_movement: bool = True
    feature_agent_signal_analysis: bool = True
    feature_agent_injury_impact: bool = True
    feature_agent_news_projection: bool = True
    feature_agent_ownership: bool = True
    feature_agent_lineup_strategy: bool = True
    feature_agent_narrative: bool = True
    feature_agent_simulation_tuning: bool = True
    feature_agent_coach_learning: bool = True
    feature_agent_expert_quality: bool = True
    feature_agent_backtesting: bool = True
    feature_agent_tournament_analysis: bool = True
    feature_agent_chat: bool = True
    feature_calibration: bool = True
    feature_nightly_pipeline: bool = True

    class Config:
        env_file = ".env"
        extra = "ignore"


@lru_cache()
def get_settings() -> Settings:
    return Settings()
