import logging
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv

load_dotenv(override=True)  # Load .env and override empty system env vars

# ── CBBpy throttle (must be imported before cbbpy/joblib) ─────────────
# Sets LOKY_MAX_CPU_COUNT env var to limit joblib sub-processes.
import app.services.cbbpy_throttle  # noqa: F401  early import for env setup

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi.errors import RateLimitExceeded

from app.config import get_settings
from app.services.cache_service import CacheService
from app.db.database import init_db, close_db, is_db_available, seed_teams, seed_cbb_teams
from app.api.routes import router, auth_router
from app.api.rate_limiter import limiter
from app.api.error_handlers import register_error_handlers
from app.api.middleware import RequestIDMiddleware, RequestTimeoutMiddleware, JSONFormatter
from app.models.responses import HealthResponse

# ── Scheduler (module-level so admin endpoints can inspect it) ─────────
_scheduler = None

settings = get_settings()

# ── Logging ────────────────────────────────────────────────────────────
_log_level = getattr(logging, settings.log_level)
if settings.environment == "production":
    # Structured JSON logging for production
    handler = logging.StreamHandler()
    handler.setFormatter(JSONFormatter())
    logging.basicConfig(level=_log_level, handlers=[handler])
else:
    # Human-readable logs for development
    logging.basicConfig(
        level=_log_level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
logger = logging.getLogger(__name__)

cache_service = CacheService()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown events."""
    logger.info("Starting RotationEngine API...")

    # Increase the default asyncio thread pool so that background CBBpy
    # scraping (which uses asyncio.to_thread) doesn't exhaust the pool
    # and starve request-handling middleware/routes.
    import asyncio, concurrent.futures
    _loop = asyncio.get_running_loop()
    _loop.set_default_executor(
        concurrent.futures.ThreadPoolExecutor(max_workers=40, thread_name_prefix="asyncio")
    )

    # Store the main event loop reference so that sync wrappers
    # (called from ThreadPoolExecutor threads) can schedule DB
    # coroutines on the correct loop via run_coroutine_threadsafe.
    from app.services.nba_data_cache_service import NBADataCacheService
    NBADataCacheService.set_main_loop(_loop)
    from app.services.cbb_data_cache_service import CBBDataCacheService
    CBBDataCacheService.set_main_loop(_loop)

    # Ensure cache directory exists for file-based pool caching
    cache_dir = os.path.join(os.path.dirname(__file__), "..", "cache")
    os.makedirs(cache_dir, exist_ok=True)
    logger.info(f"Cache directory ready: {os.path.abspath(cache_dir)}")

    # Connect Redis cache
    await cache_service.connect()
    app.state.cache = cache_service
    logger.info(f"Cache connected: {cache_service.is_connected}")

    # Initialise the service container and attach cache to AI service
    from app.api.dependencies import get_services
    try:
        svc = get_services()
        svc.ai_service.set_cache(cache_service)
        # Wire Redis cache into InjuryService for cache-bust
        svc.injury_service._cache_service = cache_service
        svc.pre_lock_polling_service._cache = cache_service
        if svc.player_id_mapper:
            svc.player_id_mapper._cache = cache_service
        logger.info(f"AI service available: {svc.ai_service.is_available}")
    except Exception as exc:
        svc = None
        logger.warning(f"AI service setup skipped: {exc}")

    # Quick probe of stats.nba.com — pre-trip the circuit breaker if down
    # so rotation endpoints fall to DK fallback instantly instead of
    # waiting 30+ seconds for timeouts.
    # Skip when skip_nba_api_live=True — stats.nba.com is not used in the
    # live path, so probing it only causes a false OPEN circuit breaker
    # that blocks the DB cache fallback path unnecessarily.
    if not settings.skip_nba_api_live:
        try:
            from app.services.nba_api_service import probe_nba_api
            nba_ok = await asyncio.to_thread(probe_nba_api, 3.0)
            logger.info(f"NBA API reachable: {nba_ok}")
        except Exception:
            logger.warning("NBA API probe failed during startup")
    else:
        logger.info("NBA API probe skipped (skip_nba_api_live=True)")

    # Initialise PostgreSQL (non-fatal if unavailable)
    db_ok = await init_db()
    app.state.db_available = db_ok
    logger.info(f"Database available: {db_ok}")

    # Seed reference data
    if db_ok:
        await seed_teams()
        await seed_cbb_teams()

    # Pre-load learned calibrations into memory
    if db_ok and svc:
        try:
            cals = await svc.calibration_service.load_calibrations()
            logger.info(f"Calibrations loaded: {len(cals)} active adjustments")
        except Exception as exc:
            logger.warning(f"Calibration pre-load skipped: {exc}")

    # Pre-load Agent 9 team-specific injury offsets
    if db_ok and svc:
        try:
            offsets = await svc.calibration_service.compute_team_injury_offsets()
            logger.info(
                f"Team-injury offsets loaded: {len(offsets)} (team, status) pairs"
            )
        except Exception as exc:
            logger.warning(f"Team-injury offset pre-load skipped: {exc}")

    # Pre-load learned coach profile adjustments
    if db_ok and svc:
        try:
            count = await svc.coach_profile_service.load_learned_adjustments()
            logger.info(f"Coach profile adjustments loaded: {count} teams")
        except Exception as exc:
            logger.warning(f"Coach profile pre-load skipped: {exc}")

    # ── Register OAuth providers ────────────────────────────────────
    if settings.oauth_enabled:
        from app.api.oauth_providers import register_providers
        register_providers()
        logger.info("OAuth authentication enabled")

    # ── Start APScheduler for nightly feedback pipeline ─────────────
    # With uvicorn --workers N, each worker forks a separate process
    # that independently runs this lifespan.  To prevent N duplicate
    # cron jobs, only start the scheduler in one worker.
    #
    # Detection: uvicorn doesn't expose a worker index, but the first
    # worker is always the first to run lifespan, and we can use a
    # lightweight Redis or DB advisory lock.  Simpler: use an env-var
    # flag — the Dockerfile sets it, or we default to "always run" for
    # single-worker dev mode.
    _is_scheduler_worker = os.environ.get("SCHEDULER_ENABLED", "true").lower() in (
        "true", "1", "yes",
    )
    global _scheduler
    if not _is_scheduler_worker:
        logger.info("Scheduler disabled for this worker (SCHEDULER_ENABLED != true)")
    else:
        try:
            from apscheduler.schedulers.asyncio import AsyncIOScheduler
            from apscheduler.triggers.cron import CronTrigger

            _scheduler = AsyncIOScheduler()

            # Nightly pipeline: 3:00 AM Eastern Time
            # Ingests yesterday's box scores, runs projection analysis,
            # saves calibrations.
            async def _nightly_pipeline_job():
                from app.api.routers.admin_pipeline import (
                    run_nightly_pipeline, _pipeline_state,
                )
                # run_nightly_pipeline acquires its own lock internally,
                # so we only do a non-blocking status check here to avoid
                # deadlock (asyncio.Lock is non-reentrant).
                if _pipeline_state.get("last_status") == "running":
                    logger.info("[Scheduler] Pipeline already running — skipping")
                    return
                try:
                    await run_nightly_pipeline()
                except Exception as exc:
                    logger.error(f"[Scheduler] Nightly pipeline failed: {exc}")

            _scheduler.add_job(
                _nightly_pipeline_job,
                trigger=CronTrigger(hour=3, minute=0, timezone="US/Eastern"),
                id="nightly_pipeline",
                name="Nightly Feedback Pipeline",
                replace_existing=True,
            )

            # NBA data cache refresh: 4:00 AM Eastern Time
            async def _nba_cache_refresh_job():
                try:
                    from app.services.nba_data_cache_service import NBADataCacheService
                    from app.api.dependencies import get_services
                    cache_svc = NBADataCacheService()
                    # Pass BDL service so refresh uses it as primary source
                    svc = get_services()
                    bdl = getattr(svc, "bdl_service", None)
                    result = await cache_svc.refresh_all(balldontlie=bdl)
                    logger.info(
                        f"[Scheduler] NBA cache refresh complete: "
                        f"{result.get('players_updated', 0)} players, "
                        f"{result.get('games_inserted', 0)} games in "
                        f"{result.get('elapsed_s', 0)}s"
                    )
                except Exception as exc:
                    logger.error(f"[Scheduler] NBA cache refresh failed: {exc}")

            _scheduler.add_job(
                _nba_cache_refresh_job,
                trigger=CronTrigger(hour=4, minute=0, timezone="US/Eastern"),
                id="nba_cache_refresh",
                name="NBA Data Cache Refresh",
                replace_existing=True,
            )

            # G-League stats cache refresh: 4:15 AM Eastern Time
            # Fetches G-League player stats (LeagueID="20") for FPPM
            # translation.  Two-way players with zero NBA logs use
            # G-League data × 0.75 translation tax.
            async def _gleague_cache_refresh_job():
                try:
                    from app.api.dependencies import get_services
                    svc = get_services()
                    gl_svc = getattr(svc, "gleague_service", None)
                    if gl_svc:
                        result = await gl_svc.refresh_gleague_cache()
                        logger.info(
                            f"[Scheduler] G-League cache refresh complete: "
                            f"{result.get('count', 0)} players in "
                            f"{result.get('elapsed_s', 0)}s"
                        )
                    else:
                        logger.warning("[Scheduler] G-League service not available")
                except Exception as exc:
                    logger.error(f"[Scheduler] G-League cache refresh failed: {exc}")

            _scheduler.add_job(
                _gleague_cache_refresh_job,
                trigger=CronTrigger(
                    hour=4, minute=15, timezone="US/Eastern"
                ),
                id="gleague_cache_refresh",
                name="G-League Stats Cache Refresh",
                replace_existing=True,
            )

            # CBB data cache refresh: 4:30 AM Eastern Time
            # Runs after NBA cache, scrapes CBBpy box scores for all D1
            # teams and upserts into cbb_player_game_log.  Eliminates
            # the ~110s live-scrape during CBB lineup generation.
            async def _cbb_cache_refresh_job():
                try:
                    from app.services.cbb_data_cache_service import (
                        CBBDataCacheService,
                    )
                    cache_svc = CBBDataCacheService()
                    result = await cache_svc.refresh_all()
                    logger.info(
                        f"[Scheduler] CBB cache refresh complete: "
                        f"{result.get('teams_updated', 0)} teams, "
                        f"{result.get('games_inserted', 0)} games in "
                        f"{result.get('elapsed_s', 0)}s"
                    )
                except Exception as exc:
                    logger.error(
                        f"[Scheduler] CBB cache refresh failed: {exc}"
                    )

            _scheduler.add_job(
                _cbb_cache_refresh_job,
                trigger=CronTrigger(
                    hour=4, minute=30, timezone="US/Eastern"
                ),
                id="cbb_cache_refresh",
                name="CBB Data Cache Refresh",
                replace_existing=True,
            )

            # ── Injury sync: every 15 minutes ──────────────────────
            # Fetches injuries from BALLDONTLIE API and upserts into
            # the local nba_injuries DB table.  The live InjuryService
            # reads ONLY from this table — never makes HTTP calls.
            from apscheduler.triggers.interval import IntervalTrigger

            async def _sync_nba_injuries_job():
                """Background injury sync — BDL API → nba_injuries table.

                Handles 429 rate-limit gracefully: logs a warning and
                retries on the next 15-minute cycle.  The local DB
                retains the previous sync's data so lineups are never
                built with an empty injury list.
                """
                try:
                    import asyncio

                    from app.db.database import async_session_factory
                    from app.services.injury_service import InjuryService

                    # async_session_factory is None exactly when
                    # DATABASE_URL is unset — sync_injuries() itself uses
                    # its own psycopg2 connection, not this factory.
                    if async_session_factory is None:
                        logger.warning(
                            "[Scheduler] Injury sync skipped — "
                            "DB not initialised"
                        )
                        return

                    try:
                        from app.api.dependencies import get_services
                        svc = get_services()
                        cache_svc = getattr(svc, "cache_service", None)
                    except Exception:
                        cache_svc = None
                    sync_svc = InjuryService(cache_service=cache_svc)

                    # sync_injuries is synchronous (psycopg2) — run it in
                    # a worker thread, mirroring the proven pattern in
                    # pre_lock_polling_service._poll_once.
                    result = await asyncio.to_thread(sync_svc.sync_injuries)

                    upserted = result.get("upserted", 0)
                    changed = result.get("hash_changed", False)
                    logger.info(
                        "[Scheduler] Injury sync complete: "
                        f"{upserted} upserted, "
                        f"hash_changed={changed}"
                    )
                except Exception as exc:
                    # Catch-all: 429, network errors, DB errors — log
                    # and retry on the next cycle.  The local DB retains
                    # stale-but-valid data from the last successful sync.
                    logger.error(
                        f"[Scheduler] Injury sync failed "
                        f"(will retry in 15 min): {exc}"
                    )

            _scheduler.add_job(
                _sync_nba_injuries_job,
                trigger=IntervalTrigger(minutes=15),
                id="sync_nba_injuries",
                name="NBA Injury Sync (BDL → DB)",
                replace_existing=True,
            )

            # Pre-Lock Simulation Scheduler: 9:00 AM ET daily
            # Queries today's slates, then dynamically schedules one-shot
            # simulation jobs 60 min before each slate locks.
            async def _schedule_pre_lock_sims_job():
                try:
                    from app.services.pre_lock_simulation_service import (
                        schedule_slate_simulations,
                    )
                    count = await schedule_slate_simulations(_scheduler)
                    logger.info(
                        "[PreLockSim] Daily scheduler ran — %d jobs queued", count
                    )
                except Exception as e:
                    logger.error("[PreLockSim] Daily scheduler failed: %s", e)

            _scheduler.add_job(
                _schedule_pre_lock_sims_job,
                trigger=CronTrigger(hour=9, minute=0, timezone="US/Eastern"),
                id="pre_lock_sim_scheduler",
                name="Pre-Lock Simulation Scheduler (9:00 AM ET)",
                replace_existing=True,
            )

            _scheduler.start()
            logger.info(
                "APScheduler started — nightly pipeline at 3:00 AM ET, "
                "NBA cache refresh at 4:00 AM ET, "
                "CBB cache refresh at 4:30 AM ET, "
                "injury sync every 15 min, "
                "pre-lock sim scheduler at 9:00 AM ET"
            )
        except ImportError:
            logger.info("APScheduler not installed — nightly pipeline disabled")
        except Exception as exc:
            logger.warning(f"Scheduler setup failed: {exc}")

    # ── Background pool pre-warm ────────────────────────────────────
    # Fire-and-forget: build the player pool for today's main NBA and
    # CBB slates so the first user request hits a warm cache instead
    # of waiting 30-90s for a cold build.
    if svc and _is_scheduler_worker:
        import threading

        def _prewarm_pool():
            from datetime import date as _date
            import time as _time
            from app.services.dk_slate_service import DKSlateService

            # Small delay so the server is fully ready to handle
            # requests before we start the heavy CBBpy scraping.
            _time.sleep(3)

            gd = _date.today().isoformat()
            slate_svc = DKSlateService()

            # Timeout caps per sport — CBBpy can hang indefinitely
            _SPORT_TIMEOUTS = {"nba": 120, "cbb": 600}

            for sport in ("nba", "cbb"):
                try:
                    slates = slate_svc.get_slates(gd, sport=sport)
                    if not slates:
                        logger.info(f"[PreWarm] No {sport.upper()} slates for today — skipping")
                        continue

                    # Pick the main slate (most games)
                    main_slate = max(slates, key=lambda s: getattr(s, "game_count", 0))
                    dg_id = main_slate.draft_group_id
                    logger.info(
                        f"[PreWarm] Building {sport.upper()} pool for DG {dg_id} "
                        f"({getattr(main_slate, 'game_count', '?')} games)..."
                    )
                    t0 = _time.time()

                    # Run the build in a sub-thread with a hard timeout
                    # so a hanging CBBpy call doesn't block forever.
                    _timeout = _SPORT_TIMEOUTS.get(sport, 300)
                    _result_box = [None]

                    def _build_fn():
                        _result_box[0] = svc.lineup_optimizer_service.build_player_pool(
                            platform="dk",
                            draft_group_id=dg_id,
                            game_date=gd,
                            sport=sport,
                            data_service=svc.get_data_service(sport),
                            game_service_override=svc.get_game_service(sport),
                            injury_service_override=svc.get_injury_service(sport),
                        )

                    build_t = threading.Thread(
                        target=_build_fn, daemon=True,
                        name=f"prewarm-{sport}",
                    )
                    build_t.start()
                    build_t.join(timeout=_timeout)

                    if build_t.is_alive():
                        logger.warning(
                            f"[PreWarm] {sport.upper()} build timed out "
                            f"after {_timeout}s — abandoning"
                        )
                        continue

                    pool = _result_box[0] or []
                    logger.info(
                        f"[PreWarm] {sport.upper()} pool ready: {len(pool)} players "
                        f"in {_time.time() - t0:.1f}s"
                    )
                except Exception as exc:
                    logger.info(f"[PreWarm] {sport.upper()} skipped — {exc}")

        t = threading.Thread(target=_prewarm_pool, daemon=True, name="pool-prewarm")
        t.start()
        logger.info("[PreWarm] Background pool warm-up started (NBA + CBB)")

    # ── Start Redis Pub/Sub pre-warm subscriber ──────────────────────
    # Listens for star-player injury changes published by
    # InjuryService and proactively rebuilds the affected slate's
    # player pool before any user requests.
    if svc and cache_service.is_connected and _is_scheduler_worker:
        try:
            from app.services.prewarm_subscriber import start_prewarm_listener
            _prewarm_thread = start_prewarm_listener()
            if _prewarm_thread is not None:
                logger.info(
                    "[PreWarm-Sub] Background subscriber daemon started"
                )
        except Exception as exc:
            logger.warning(f"[PreWarm-Sub] Subscriber startup skipped: {exc}")

    # ── Start Pre-Lock Polling Service ────────────────────────────
    # Background asyncio task that polls injury + news pipelines at
    # high frequency when within 60 min of a DK slate lock.
    if svc and cache_service.is_connected and _is_scheduler_worker:
        try:
            svc.pre_lock_polling_service.start()
            logger.info("[PreLock] Pre-lock polling service started")
        except Exception as exc:
            logger.warning(f"[PreLock] Polling service startup failed: {exc}")

    yield

    logger.info("Shutting down RotationEngine API...")
    if svc:
        try:
            svc.pre_lock_polling_service.stop()
        except Exception:
            pass
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("APScheduler shut down")
    await close_db()
    await cache_service.disconnect()


app = FastAPI(
    title="RotationEngine",
    description="NBA Minutes Projection Engine",
    version="1.0.0",
    lifespan=lifespan,
)

# ── Rate Limiter ────────────────────────────────────────────────────
app.state.limiter = limiter


@app.exception_handler(RateLimitExceeded)
async def _rate_limit_handler(request: Request, exc: RateLimitExceeded):
    return JSONResponse(
        status_code=429,
        content={
            "error": "RateLimitExceeded",
            "detail": f"Rate limit exceeded: {exc.detail}",
            "status_code": 429,
        },
    )


# ── Request Timeout Middleware (10-min cap — returns 504 on timeout) ──
app.add_middleware(RequestTimeoutMiddleware, timeout_s=600)

# ── Request ID Middleware ───────────────────────────────────────────
app.add_middleware(RequestIDMiddleware)

# ── CORS ────────────────────────────────────────────────────────────
cors_origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "X-API-Key", "X-Request-ID", "Cookie"],
)

# ── Session Middleware (for authlib OAuth state during redirects) ──
if settings.oauth_enabled and settings.session_secret_key:
    from starlette.middleware.sessions import SessionMiddleware
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret_key,
        session_cookie="oauth_state",
        max_age=600,
        same_site="lax",
        https_only=settings.environment == "production",
    )

app.include_router(auth_router, prefix="/api")
app.include_router(router, prefix="/api")

# ── Debug router (mounts only when DEBUG_MODE is truthy) ────────────
# Hosts /api/debug/* endpoints — currently mock-ingest for offseason
# pipeline validation. Disabled by default so prod never exposes them.
if os.environ.get("DEBUG_MODE", "").lower() in ("1", "true", "yes"):
    from app.api.routers.debug import router as debug_router
    app.include_router(debug_router, prefix="/api")
    logger.info("[Debug] DEBUG_MODE=True — /api/debug/* endpoints registered")

register_error_handlers(app)


@app.get("/health", response_model=HealthResponse)
async def health_check():
    ai_status = False
    try:
        from app.api.dependencies import get_services as _get_health_svc
        ai_status = _get_health_svc().ai_service.is_available
    except Exception:
        pass

    # Check Alembic migration status (non-fatal)
    migration_current = None
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory
        from alembic.runtime.migration import MigrationContext
        from app.db.database import engine as _db_engine

        if _db_engine is not None:
            from sqlalchemy import text

            async def _check_migration():
                async with _db_engine.connect() as conn:
                    current_rev = await conn.scalar(
                        text("SELECT version_num FROM alembic_version LIMIT 1")
                    )
                    return current_rev

            try:
                current = await _check_migration()
                alembic_cfg = Config(
                    os.path.join(os.path.dirname(__file__), "..", "alembic.ini")
                )
                script = ScriptDirectory.from_config(alembic_cfg)
                head = script.get_current_head()
                migration_current = (current == head)
            except Exception:
                migration_current = None
    except Exception:
        pass

    # NBA API circuit breaker status
    nba_api_circuit = "unknown"
    try:
        from app.services.nba_api_service import _circuit_breaker
        nba_api_circuit = _circuit_breaker.state
    except Exception:
        pass

    # BallDontLie API status
    bdl_available = False
    bdl_circuit = "unknown"
    try:
        from app.api.dependencies import get_services as _get_bdl_svc
        _bdl_svc = _get_bdl_svc()
        source_status = _bdl_svc.nba_service.get_source_status()
        bdl_available = source_status.get("balldontlie_available", False)

        from app.services.http_resilience import _breakers, APIGroup
        bdl_breaker = _breakers.get(APIGroup.BALLDONTLIE)
        if bdl_breaker:
            bdl_circuit = bdl_breaker.state
    except Exception:
        pass

    return {
        "status": "healthy",
        "cache_connected": cache_service.is_connected,
        "db_connected": is_db_available(),
        "ai_available": ai_status,
        "nba_api_circuit_breaker": nba_api_circuit,
        "balldontlie_available": bdl_available,
        "balldontlie_circuit_breaker": bdl_circuit,
        "environment": settings.environment,
        "migration_current": migration_current,
    }


def get_next_pipeline_run() -> str | None:
    """Return the next scheduled pipeline run time as ISO string, or None."""
    if _scheduler and _scheduler.running:
        job = _scheduler.get_job("nightly_pipeline")
        if job and job.next_run_time:
            return job.next_run_time.isoformat()
    return None
