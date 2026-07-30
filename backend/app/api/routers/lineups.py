"""Player pool, lineup optimization, analysis, late swap, fade, and ownership simulation endpoints."""

import asyncio
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.api.rate_limiter import limiter
from app.api.auth import require_api_key
from app.api.dependencies import get_services
from app.utils.exceptions import LineupGenerationError
from app.api.sse_helpers import sse_stream, format_named_event
from app.models.lineup import (
    AnalyzeLineupsRequest,
    AnalyzeLineupsResponse,
    GameSlotStatus,
    LateSwapMonitorRequest,
    LateSwapRequest,
    LateSwapResponse,
    LateSwapSlotInfo,
    LateSwapSuggestion,
    MultiLineupRequest,
    MultiLineupResponse,
    OptimizeRequest,
    OptimizedLineup,
    RefineLineupsRequest,
    RefineLineupsResponse,
    SimFilterRequest,
    SimFilterResponse,
)
from app.services.lineup_optimizer_service import (
    _index_slots,
    _base_slot,
    DK_SALARY_CAP,
    FD_SALARY_CAP,
    DK_ROSTER_SLOTS,
    FD_ROSTER_SLOTS,
    DK_CBB_ROSTER_SLOTS,
)
from app.models.responses import PlayerPoolResponse

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/player-pool", response_model=PlayerPoolResponse)
def get_player_pool(
    platform: str = Query("dk", description="Platform: dk or fd"),
    draft_group_id: int = Query(..., description="DK DraftGroup ID"),
    game_date: Optional[str] = Query(None, description="Game date YYYY-MM-DD"),
    sport: str = Query("nba", description="Sport: nba or cbb"),
):
    """Get the full player pool with projections and salaries for a slate.

    Returns all players available for lineup construction, with salary,
    projected fantasy points, floor/ceiling, and eligible roster slots
    pre-computed for the specified platform.

    For CBB, if no cache is available this returns a 202 status with a
    message to use the ``/player-pool/stream`` SSE endpoint instead,
    since a cold CBB build takes 3-5 minutes and would exceed the
    request timeout.
    """
    from app.services.lineup_optimizer_service import (
        _pool_cache, _pool_lock, _POOL_CACHE_TTL,
        _cache_key, _load_pool_from_file,
        _apply_imported_projection_overrides,
        _apply_rotation_role,
    )

    try:
        svc = get_services()

        # For CBB, check cache first — cold builds take 3-5 minutes
        # and will exceed the 60 s request timeout.
        if sport == "cbb":
            gd = game_date or __import__("datetime").date.today().isoformat()
            cache_key = _cache_key(f"{sport}:{platform}", draft_group_id, gd)
            now = time.time()

            # Check in-memory cache
            with _pool_lock:
                entry = _pool_cache.get(cache_key)
                if entry is not None:
                    cached_at = entry[0]
                    cached_pool = entry[1]
                    if now - cached_at < _POOL_CACHE_TTL and isinstance(cached_pool, list):
                        _apply_imported_projection_overrides(cached_pool)
                        # Derive Starter/Bench/Out classification from
                        # the (possibly user-overridden) minutes. Done
                        # after the override pass so role reflects what
                        # the consumer actually sees. (Prompt 7.8)
                        _apply_rotation_role(cached_pool, sport)
                        return {"players": cached_pool, "count": len(cached_pool)}

            # Check file cache
            _file_result = _load_pool_from_file(cache_key)
            if _file_result is not None:
                file_pool, _file_expected = _file_result
                with _pool_lock:
                    _pool_cache[cache_key] = (now, file_pool, _file_expected)
                _apply_imported_projection_overrides(file_pool)
                _apply_rotation_role(file_pool, sport)
                return {"players": file_pool, "count": len(file_pool)}

            # No cache — tell the client to use the stream endpoint
            from fastapi.responses import JSONResponse
            return JSONResponse(
                status_code=202,
                content={
                    "players": [],
                    "count": 0,
                    "building": True,
                    "message": (
                        "CBB pool is being built in the background. "
                        "Use /player-pool/stream for real-time progress."
                    ),
                },
            )

        pool, excluded_players = svc.lineup_optimizer_service.build_player_pool(
            platform=platform,
            draft_group_id=draft_group_id,
            game_date=game_date,
            excluded_player_ids=[],
            sport=sport,
            data_service=svc.get_data_service(sport),
            game_service_override=svc.get_game_service(sport),
            injury_service_override=svc.get_injury_service(sport),
            return_excluded=True,
        )
        _apply_imported_projection_overrides(pool)
        # Derive sport-aware Starter/Bench/Out classification — after
        # projections + overrides are finalized so role reflects the
        # final minutes value. (Prompt 7.8)
        _apply_rotation_role(pool, sport)
        return {
            "players": pool,
            "count": len(pool),
            "excluded_players": excluded_players,
            "excluded_count": len(excluded_players),
        }
    except Exception as e:
        logger.error(f"Player pool fetch failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/player-pool/injury-hash")
def injury_hash_check(
    services=Depends(get_services),
):
    """Lightweight endpoint for frontend staleness detection.

    Returns the current injury hash. Frontend polls this every 30s
    and triggers a pool refresh when the hash changes vs. what it
    received when the pool was last loaded.

    No auth required — hash alone reveals nothing sensitive.
    """
    try:
        injury_svc = services.injury_service
        current_hash = injury_svc.get_injury_hash()
        return {
            "injury_hash": current_hash,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        logger.error(f"Injury hash check failed: {e}")
        return {"injury_hash": "", "timestamp": ""}


@router.get("/player-pool/diagnostics")
def pool_diagnostics(
    _auth=Depends(require_api_key),
    draft_group_id: int = Query(..., description="DK DraftGroup ID"),
    platform: str = Query("dk", description="Platform: dk or fd"),
    game_date: Optional[str] = Query(None, description="Game date YYYY-MM-DD"),
    sport: str = Query("nba", description="Sport: nba or cbb"),
):
    """Lightweight diagnostics for a player pool — does NOT trigger a build.

    Returns DraftGroup metadata, circuit breaker state, cache status,
    and draftable counts to help debug small-pool issues.
    """
    import os
    from datetime import date as _date

    import httpx

    from app.services.lineup_optimizer_service import (
        _pool_cache, _pool_lock, _POOL_CACHE_TTL,
        _cache_key, _file_cache_path,
    )

    svc = get_services()
    gd = game_date or _date.today().isoformat()
    now = time.time()
    result: Dict = {
        "draft_group_id": draft_group_id,
        "platform": platform,
        "sport": sport,
        "game_date": gd,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    # ── Section 1: DK Draftables ──
    try:
        draftables = svc.dk_draftables_service.get_draftables(draft_group_id)
        _teams: Dict[str, int] = {}
        for d in draftables:
            t = d.team_abbreviation.upper()
            _teams[t] = _teams.get(t, 0) + 1
        _warnings = []
        if len(_teams) <= 2:
            _warnings.append(
                f"Only {len(_teams)} teams — likely SHOWDOWN, not Classic"
            )
        result["draftables"] = {
            "total": len(draftables),
            "teams": dict(sorted(_teams.items())),
            "team_count": len(_teams),
            "warnings": _warnings or None,
        }
    except Exception as e:
        result["draftables"] = {"error": str(e)}

    # ── Section 2: DraftGroup Metadata (DK API) ──
    _GAME_TYPE_LABELS = {70: "Classic", 96: "Showdown", 98: "CBB Classic"}
    try:
        _dg_url = f"https://api.draftkings.com/draftgroups/v1/{draft_group_id}"
        _dg_resp = httpx.get(_dg_url, timeout=10, follow_redirects=True)
        _dg_resp.raise_for_status()
        _dg_data = _dg_resp.json().get("draftGroup", {})
        _game_type_id = _dg_data.get("gameType")
        _gt_label = _GAME_TYPE_LABELS.get(
            _game_type_id, f"Unknown({_game_type_id})"
        )
        _dg_games = _dg_data.get("games", [])
        _dg_warnings = []
        if sport == "nba" and _game_type_id != 70:
            _dg_warnings.append(
                f"gameType={_game_type_id} ({_gt_label}) is NOT Classic (70) "
                f"— this explains a small player pool"
            )
        result["draft_group_metadata"] = {
            "game_type_id": _game_type_id,
            "game_type_label": _gt_label,
            "sport_id": _dg_data.get("sportId"),
            "game_count": len(_dg_games),
            "warnings": _dg_warnings or None,
        }
    except Exception as e:
        result["draft_group_metadata"] = {"error": str(e)}

    # ── Section 3: Circuit Breaker ──
    try:
        from app.services.nba_api_service import get_circuit_breaker_diagnostics
        result["circuit_breaker"] = get_circuit_breaker_diagnostics()
    except Exception as e:
        result["circuit_breaker"] = {"error": str(e)}

    # ── Section 4: Rotation Cache ──
    try:
        from app.services.nba_api_service import _rotation_cache
        _entries = []
        for tid, (cached_at, rot) in list(_rotation_cache.items()):
            _entries.append({
                "team_id": tid,
                "players": len(rot),
                "age_seconds": round(now - cached_at, 1),
                "expired": (now - cached_at) > 3600,
            })
        result["rotation_cache"] = {
            "entries": len(_entries),
            "teams": sorted(_entries, key=lambda x: x["team_id"]),
        }
    except Exception as e:
        result["rotation_cache"] = {"error": str(e)}

    # ── Section 5: Pool Cache ──
    try:
        _cache_key_str = _cache_key(f"{sport}:{platform}", draft_group_id, gd)
        _mem_entries = []
        with _pool_lock:
            for k, v in list(_pool_cache.items()):
                if k.endswith(":inj"):
                    continue
                cached_at = v[0]
                cached_pool = v[1]
                if isinstance(cached_pool, list):
                    _mem_entries.append({
                        "key": k,
                        "players": len(cached_pool),
                        "age_seconds": round(now - cached_at, 1),
                        "expired": (now - cached_at) > _POOL_CACHE_TTL,
                        "is_current": k == _cache_key_str,
                    })

        # File cache entries
        _cache_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            "cache",
        )
        _file_entries = []
        if os.path.isdir(_cache_dir):
            for fname in sorted(os.listdir(_cache_dir)):
                if fname.startswith("pool_") and fname.endswith(".json"):
                    fpath = os.path.join(_cache_dir, fname)
                    _stat = os.stat(fpath)
                    _file_entries.append({
                        "filename": fname,
                        "size_bytes": _stat.st_size,
                        "age_seconds": round(now - _stat.st_mtime, 1),
                        "expired": (now - _stat.st_mtime) > 7200,
                    })

        result["pool_cache"] = {
            "memory_entries": _mem_entries,
            "file_entries": _file_entries,
            "current_cache_key": _cache_key_str,
        }
    except Exception as e:
        result["pool_cache"] = {"error": str(e)}

    # ── Section 6: Draftables Service Cache ──
    try:
        _dk_cache = svc.dk_draftables_service._cache
        _dk_date = getattr(svc.dk_draftables_service, "_cache_date", None)
        result["draftables_cache"] = {
            "cached_draft_groups": list(_dk_cache.keys()),
            "players_per_dg": {
                dg: len(players) for dg, players in _dk_cache.items()
            },
            "cache_date": str(_dk_date) if _dk_date else None,
        }
    except Exception as e:
        result["draftables_cache"] = {"error": str(e)}

    return result


@router.post("/player-pool/clear-cache")
def clear_pool_cache(
    _auth=Depends(require_api_key),
    platform: str = Query("dk", description="Platform: dk or fd"),
    draft_group_id: int = Query(0, description="DK DraftGroup ID (0 = all)"),
    game_date: Optional[str] = Query(None, description="Game date YYYY-MM-DD"),
    sport: str = Query("nba", description="Sport: nba or cbb"),
    all: bool = Query(False, description="Clear ALL caches (rotation, draftables, circuit breaker)"),
):
    """Clear server-side player pool and enrichment caches.

    If all parameters are provided, clears caches for that specific slate.
    If draft_group_id is 0, clears ALL cached data.
    If all=true, also clears rotation cache, draftables cache, and resets
    the NBA API circuit breaker.
    """
    from app.services.lineup_optimizer_service import clear_optimizer_cache

    if draft_group_id > 0 and game_date:
        cleared = clear_optimizer_cache(f"{sport}:{platform}", draft_group_id, game_date)
    else:
        cleared = clear_optimizer_cache()

    details = {"pool_cache": cleared}

    if all:
        # Clear rotation cache
        try:
            from app.services.nba_api_service import clear_rotation_cache
            rot_cleared = clear_rotation_cache()
            details["rotation_cache"] = rot_cleared
        except Exception as e:
            details["rotation_cache_error"] = str(e)

        # Clear draftables cache
        try:
            svc = get_services()
            dk_count = len(svc.dk_draftables_service._cache)
            svc.dk_draftables_service._cache.clear()
            if hasattr(svc.dk_draftables_service, "_cache_date"):
                svc.dk_draftables_service._cache_date = None
            details["draftables_cache"] = dk_count
        except Exception as e:
            details["draftables_cache_error"] = str(e)

        # Reset circuit breaker
        try:
            from app.services.nba_api_service import reset_circuit_breaker
            reset_circuit_breaker()
            details["circuit_breaker"] = "reset"
        except Exception as e:
            details["circuit_breaker_error"] = str(e)

    total_cleared = sum(
        v for v in details.values() if isinstance(v, int)
    )
    return {
        "cleared": total_cleared,
        "details": details,
        "message": f"Cleared {total_cleared} cache entries"
        + (" + rotation + draftables + circuit breaker" if all else ""),
    }


@router.get("/player-pool/stream")
@limiter.limit("10/minute")
async def stream_player_pool(
    request: Request,
    _auth=Depends(require_api_key),
    platform: str = Query("dk", description="Platform: dk or fd"),
    draft_group_id: int = Query(..., description="DK DraftGroup ID"),
    game_date: Optional[str] = Query(None, description="Game date YYYY-MM-DD"),
    sport: str = Query("nba", description="Sport: nba or cbb"),
):
    """Stream player pool build progress via Server-Sent Events.

    Sends progress events as teams are processed, then a final 'done'
    event with the complete pool.  The frontend can use this for
    real-time progress bars instead of simulated timers.

    Event types:
      - ``progress``: ``{ step, completed, total }``
      - ``done``: ``{ players: [...], count: N }``
      - ``error``: ``{ detail: "..." }``
    """
    svc = get_services()

    def _build_pool(put, cancelled):
        def _on_progress(step: str, completed: int, total: int):
            if cancelled.is_set():
                return
            put({
                "event": "progress",
                "data": {"step": step, "completed": completed, "total": total},
            })

        try:
            result = svc.lineup_optimizer_service.build_player_pool(
                platform=platform,
                draft_group_id=draft_group_id,
                game_date=game_date,
                excluded_player_ids=[],
                on_progress=_on_progress,
                sport=sport,
                data_service=svc.get_data_service(sport),
                game_service_override=svc.get_game_service(sport),
                injury_service_override=svc.get_injury_service(sport),
                return_excluded=True,
                cancelled=cancelled,
            )
            pool, excluded_players = result
            if not cancelled.is_set():
                put({
                    "event": "done",
                    "data": {
                        "players": [p.model_dump() for p in pool],
                        "count": len(pool),
                        "excluded_players": [e.model_dump() for e in excluded_players],
                        "excluded_count": len(excluded_players),
                    },
                })
        except Exception as e:
            logger.error(f"Stream player pool failed: {e}")
            put({"event": "error", "data": {"detail": str(e)}})

    # CBB pools take longer due to sequential CBBpy scraping per team
    _timeout = 420.0 if sport == "cbb" else 240.0
    return await sse_stream(
        request,
        _build_pool,
        timeout_s=_timeout,
        format_event=format_named_event,
    )


@router.post("/preload-pool")
@limiter.limit("10/minute")
def preload_pool(
    request: Request,
    _auth=Depends(require_api_key),
    platform: str = Query("draftkings"),
    draft_group_id: int = Query(...),
    game_date: Optional[str] = Query(None),
    sport: str = Query("nba", description="Sport: nba or cbb"),
):
    """Pre-warm the player pool cache for a given slate.

    Call this as soon as the user selects a slate/draft-group so the pool
    is already cached when they later hit Generate Lineups.  Returns pool
    size and cache status so the frontend can show readiness.

    This is a **synchronous** call -- it blocks until the pool is built
    (30-90s on cold cache) or returns immediately if cached (~50ms).
    """
    from datetime import date as _date

    svc = get_services()
    gd = game_date or _date.today().isoformat()
    t0 = time.time()

    # Check if pool is already cached (match the EXACT draft group)
    cached_pool = svc.lineup_optimizer_service.get_cached_pool(
        sport=sport, draft_group_id=draft_group_id,
    )
    if cached_pool:
        return {
            "status": "cached",
            "player_count": len(cached_pool),
            "elapsed_seconds": round(time.time() - t0, 2),
            "message": "Pool already warm",
        }

    try:
        pool = svc.lineup_optimizer_service.build_player_pool(
            platform=platform,
            draft_group_id=draft_group_id,
            game_date=gd,
            sport=sport,
            data_service=svc.get_data_service(sport),
            game_service_override=svc.get_game_service(sport),
            injury_service_override=svc.get_injury_service(sport),
        )
        elapsed = round(time.time() - t0, 2)
        return {
            "status": "built",
            "player_count": len(pool),
            "elapsed_seconds": elapsed,
            "message": f"Pool built and cached in {elapsed}s",
        }
    except Exception as e:
        logger.error(f"Pool preload failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/optimize-lineup", response_model=OptimizedLineup)
@limiter.limit("20/minute")
def optimize_lineup(request: Request, _auth=Depends(require_api_key), opt_request: OptimizeRequest = ...):
    """Generate an optimal DFS lineup for the given slate and platform.

    Accepts locked/excluded player IDs to constrain the optimizer.
    Returns a salary-cap-legal, position-valid lineup maximizing
    projected fantasy points.
    """
    if getattr(opt_request, 'mode', 'classic') == 'showdown' and getattr(opt_request, 'sport', 'nba') == 'cbb':
        raise HTTPException(
            status_code=400,
            detail="Showdown mode is not available for college basketball",
        )
    try:
        svc = get_services()
        result = svc.lineup_optimizer_service.optimize(opt_request)
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Lineup optimization failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/generate-lineups", response_model=MultiLineupResponse)
@limiter.limit("20/minute")
def generate_lineups(request: Request, _auth=Depends(require_api_key), gen_request: MultiLineupRequest = ...):
    """Generate N diverse DFS lineups for the given slate and platform.

    Supports strategy selection (max_projection, balanced, ceiling) and
    diversity enforcement via max_overlap constraint.  Enriches the
    player pool with simulation, expert-signal, and game-context data
    before optimizing.
    """
    if getattr(gen_request, 'mode', 'classic') == 'showdown' and getattr(gen_request, 'sport', 'nba') == 'cbb':
        raise HTTPException(
            status_code=400,
            detail="Showdown mode is not available for college basketball",
        )
    try:
        svc = get_services()
        result = svc.lineup_optimizer_service.generate_lineups(gen_request)

        # Fire-and-forget: log config + lineups to DB for ROI tracking
        tracker = svc.solver_tracking_service
        if tracker is not None and result.num_generated > 0:
            def _track_run():
                try:
                    asyncio.run(tracker.log_generation(gen_request, result))
                except Exception as _te:
                    logger.debug(f"[SolverTracking] background log failed: {_te}")
            threading.Thread(target=_track_run, daemon=True, name="solver-track").start()

        # Fire-and-forget: SMS alert on lineup generation completion
        if result.num_generated > 0:
            def _sms_notify():
                try:
                    from app.services.notification_service import NotificationService
                    notifier = NotificationService()
                    if notifier.sms_available:
                        _sport = getattr(gen_request, "sport", "nba").upper()
                        _strat = getattr(gen_request, "strategy", "balanced")
                        _elapsed = f"{result.generation_time_ms / 1000:.1f}s"
                        # Top projected lineup score
                        _top_fp = 0.0
                        if result.lineups:
                            _top_fp = max(
                                lu.total_projected_fp for lu in result.lineups
                            )
                        notifier.send_sms_alert(
                            f"[DFS] {_sport} {result.num_generated} lineups built "
                            f"({_strat}, {_elapsed}). "
                            f"Top proj: {_top_fp:.1f} FP. "
                            f"Pool: {result.pool_size} players."
                        )
                except Exception:
                    pass  # Never block the response for SMS
            threading.Thread(target=_sms_notify, daemon=True, name="sms-gen").start()

        return result
    except LineupGenerationError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Multi-lineup generation failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/sim-filter-lineups", response_model=SimFilterResponse)
@limiter.limit("10/minute")
def sim_filter_lineups(request: Request, _auth=Depends(require_api_key), body: SimFilterRequest = ...):
    """Generate lineups via the simulate-and-filter pipeline.

    Runs N Monte Carlo simulations per game, finds the optimal lineup
    for each simulated outcome, and returns the most frequently
    appearing lineups sorted by occurrence count.  This is an
    alternative to the standard optimizer for A/B testing.
    """
    try:
        svc = get_services()
        result = svc.sim_filter_service.generate(body)
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Sim-filter generation failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/analyze-lineups", response_model=AnalyzeLineupsResponse)
def analyze_lineups(_auth=Depends(require_api_key), request: AnalyzeLineupsRequest = ...):
    """Analyze generated lineups and provide insights, risks, and swap
    suggestions.

    Evaluates each lineup across multiple dimensions (projection quality,
    salary efficiency, team stacking, expert consensus, risk factors)
    and generates actionable swap recommendations.
    """
    try:
        svc = get_services()
        result = svc.lineup_analysis_service.analyze_lineups(request)
        return result
    except Exception as e:
        logger.error(f"Lineup analysis failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/refine-lineups", response_model=RefineLineupsResponse)
def refine_lineups(_auth=Depends(require_api_key), request: RefineLineupsRequest = ...):
    """Refine existing lineups by iteratively applying swap suggestions.

    Takes analyzed lineups and performs swap iterations to improve their
    overall grades.  Each lineup is independently refined: analyze ->
    pick best swap -> apply -> re-analyze -> repeat until grade target
    reached or no improving swap exists.
    """
    try:
        svc = get_services()
        result = svc.lineup_analysis_service.refine_lineups(request)
        return result
    except Exception as e:
        logger.error(f"Lineup refinement failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/analyze-lineups/narrative")
async def stream_lineup_narrative(
    request: Request,
    _auth=Depends(require_api_key),
    body: AnalyzeLineupsRequest = ...,
):
    """Stream AI-generated narrative analysis via SSE.

    First runs the structured analysis, then streams a prose narrative
    for the first lineup (or the lineup at index 0).
    """
    svc = get_services()

    def _run_narrative(put, cancelled):
        try:
            # Run structured analysis first
            analysis_result = svc.lineup_analysis_service.analyze_lineups(body)
            if not analysis_result.analyses:
                put({"event": "error", "data": {"detail": "No analysis results"}})
                return

            if cancelled.is_set():
                return

            analysis_dict = analysis_result.analyses[0].model_dump()
            lineup_dict = body.lineups[0].model_dump() if body.lineups else {}

            for chunk in svc.narrative_agent.stream_narrative(
                analysis_dict, lineup_dict, body.platform,
                sport=body.sport,
            ):
                if cancelled.is_set():
                    break
                put({"event": "chunk", "data": {"text": chunk}})

            if not cancelled.is_set():
                put({"event": "done", "data": {}})
        except Exception as e:
            logger.error(f"Narrative stream failed: {e}")
            put({"event": "error", "data": {"detail": str(e)}})

    return await sse_stream(
        request,
        _run_narrative,
        timeout_s=120.0,
        format_event=format_named_event,
    )


@router.post("/late-swap", response_model=LateSwapResponse)
def late_swap(
    _auth=Depends(require_api_key),
    request: LateSwapRequest = ...,
):
    """Detect ruled-out players and re-optimize open slots via ILP.

    Queries the BallDontLie ``/games`` endpoint via the async
    ``LiveGameStateService`` for real-time game telemetry.  Each
    roster slot is classified as **locked** (``has_started == True``)
    or **open** (game not yet tipped).

    Critically, a delayed game whose scheduled tip-off has passed but
    whose ``period`` is still 0 is treated as **not started** — this
    prevents false locks during broadcast holds or arena delays.

    Locked slots are preserved; open slots are re-optimized via the
    CBC ILP solver using updated projections from InjurySyncService.

    Falls back to legacy greedy 1-for-1 replacement when BDL is
    unavailable, ``use_ilp=False``, or the ILP solver fails.
    """
    sport = request.sport

    try:
        import asyncio
        from datetime import date as _date

        from app.services.live_game_state_service import (
            LiveGameStateService,
            normalise_to_bdl,
        )

        svc = get_services()
        gd = request.game_date or _date.today().isoformat()

        # ── Step 0: Fetch real-time game states via LiveGameStateService ──
        game_states: Dict[str, object] = {}      # team → GameState
        game_status_map: Dict[str, dict] = {}     # legacy format for slot info
        bdl_available = False

        if sport == "nba":
            live_svc = LiveGameStateService(timeout=5.0)
            if live_svc.is_available:
                try:
                    # Run the async fetch in a sync context
                    try:
                        loop = asyncio.get_running_loop()
                    except RuntimeError:
                        loop = None

                    if loop and loop.is_running():
                        import concurrent.futures
                        with concurrent.futures.ThreadPoolExecutor() as tex:
                            game_states = tex.submit(
                                asyncio.run,
                                live_svc.fetch_game_states(gd),
                            ).result(timeout=8.0)
                    else:
                        game_states = asyncio.run(
                            live_svc.fetch_game_states(gd)
                        )

                    # Build legacy format for backward-compatible slot info
                    game_status_map = live_svc.build_lock_map(game_states)
                    bdl_available = bool(game_states)
                except Exception as exc:
                    logger.warning(
                        "[LateSwap] LiveGameState fetch failed, "
                        "trying legacy BDL: %s",
                        exc,
                    )
                    # Fallback to legacy sync BDL service
                    bdl = getattr(svc, "bdl_service", None)
                    if bdl and bdl.is_available:
                        try:
                            game_status_map = (
                                bdl.get_game_statuses_by_team(gd)
                            )
                            bdl_available = True
                        except Exception as exc2:
                            logger.warning(
                                "[LateSwap] Legacy BDL also failed: %s",
                                exc2,
                            )

        # ── Step 1: Build and enrich player pool ────────────────────
        pool = svc.lineup_optimizer_service.build_player_pool(
            platform=request.platform,
            draft_group_id=request.draft_group_id,
            game_date=gd,
            sport=sport,
            data_service=svc.get_data_service(sport),
            game_service_override=svc.get_game_service(sport),
            injury_service_override=svc.get_injury_service(sport),
        )
        pool = svc.lineup_optimizer_service._enrich_pool(
            pool, request.platform, gd,
        )

        # ── Step 2: Determine platform constants ────────────────────
        salary_cap = DK_SALARY_CAP if request.platform == "dk" else FD_SALARY_CAP
        if request.platform == "dk" and sport == "cbb":
            roster_slots = list(DK_CBB_ROSTER_SLOTS)
        elif request.platform == "dk":
            roster_slots = list(DK_ROSTER_SLOTS)
        else:
            roster_slots = list(FD_ROSTER_SLOTS)

        indexed_roster = _index_slots(roster_slots)

        # ── Step 3: Process each lineup ─────────────────────────────
        all_swaps: list = []
        result_lineups: list = []
        all_slot_statuses: list = []
        all_locked_salary: list = []
        all_open_counts: list = []

        for lineup in request.lineups:
            # 3a — Classify slots via has_started telemetry
            locked_slots: Dict[str, "LineupPlayer"] = {}
            open_slot_keys: list = []
            slot_info_list: list = []

            for lp, isl in zip(lineup.players, indexed_roster):
                team = (lp.team_abbreviation or "").upper()
                bdl_team = normalise_to_bdl(team)

                # Use GameState.has_started for real-time lock detection
                gs_obj = game_states.get(bdl_team) if game_states else None
                gs_map = game_status_map.get(team) or game_status_map.get(bdl_team)

                if gs_obj is not None:
                    is_locked = gs_obj.has_started
                elif gs_map is not None:
                    is_locked = bool(gs_map.get("is_locked"))
                else:
                    is_locked = False

                lock_reason = None
                if is_locked and gs_map:
                    lock_reason = f"game_{gs_map['game_status']}"
                elif is_locked and gs_obj is not None:
                    lock_reason = (
                        "game_final" if gs_obj.is_final
                        else f"game_in_progress_P{gs_obj.period}"
                    )

                if is_locked:
                    locked_slots[isl] = lp
                else:
                    open_slot_keys.append(isl)

                slot_info_list.append(
                    LateSwapSlotInfo(
                        roster_slot=_base_slot(isl),
                        player_name=lp.player_name,
                        player_id=lp.player_id,
                        team_abbreviation=lp.team_abbreviation,
                        is_locked=is_locked,
                        lock_reason=lock_reason,
                        game_status=GameSlotStatus(
                            team_abbreviation=team,
                            game_status=gs_map["game_status"],
                            game_status_detail=gs_map.get(
                                "game_status_detail", ""
                            ),
                            is_locked=gs_map["is_locked"],
                            opponent=gs_map.get("opponent"),
                            home_team_score=gs_map.get("home_team_score"),
                            visitor_team_score=gs_map.get(
                                "visitor_team_score"
                            ),
                        ) if gs_map else None,
                    )
                )

            all_slot_statuses.append(slot_info_list)
            lu_locked_salary = sum(p.salary for p in locked_slots.values())
            all_locked_salary.append(lu_locked_salary)
            all_open_counts.append(len(open_slot_keys))

            # 3b — Detect injured players (swap suggestions)
            swap_suggestions = svc.lineup_optimizer_service.detect_late_swaps(
                lineup, pool, sport=sport,
            )
            swap_models = [
                LateSwapSuggestion(**s) for s in swap_suggestions
            ]
            all_swaps.append(swap_models)

            # 3c — Re-optimize if auto_apply
            if request.auto_apply and open_slot_keys:
                if request.use_ilp:
                    optimized = svc.lineup_optimizer_service.optimize_late_swap(
                        lineup=lineup,
                        pool=pool,
                        locked_slots=locked_slots,
                        open_slots=open_slot_keys,
                        platform=request.platform,
                        salary_cap=salary_cap,
                        sport=sport,
                    )
                    if optimized:
                        result_lineups.append(optimized)
                    else:
                        # ILP failed — fall back to greedy
                        updated = svc.lineup_optimizer_service.apply_late_swaps(
                            lineup, pool,
                        )
                        result_lineups.append(updated)
                else:
                    updated = svc.lineup_optimizer_service.apply_late_swaps(
                        lineup, pool,
                    )
                    result_lineups.append(updated)
            elif request.auto_apply and not open_slot_keys:
                # All slots locked — nothing to change
                result_lineups.append(lineup)
            else:
                result_lineups.append(lineup)

        # ── Step 4: Build response ──────────────────────────────────
        total = sum(len(s) for s in all_swaps)
        warnings: list = []
        if total > 0:
            warnings.append(
                f"Found {total} player(s) ruled Out/Doubtful across "
                f"{len(request.lineups)} lineup(s)"
            )
        if not bdl_available and sport == "nba":
            warnings.append(
                "BDL API unavailable — all slots treated as open (legacy mode)"
            )

        response_game_statuses: Optional[Dict[str, GameSlotStatus]] = None
        if game_status_map:
            response_game_statuses = {}
            for team_abbr, gs in game_status_map.items():
                response_game_statuses[team_abbr] = GameSlotStatus(
                    team_abbreviation=team_abbr,
                    game_status=gs["game_status"],
                    game_status_detail=gs.get("game_status_detail", ""),
                    is_locked=gs["is_locked"],
                    opponent=gs.get("opponent"),
                    home_team_score=gs.get("home_team_score"),
                    visitor_team_score=gs.get("visitor_team_score"),
                )

        return LateSwapResponse(
            lineups=result_lineups,
            swaps=all_swaps,
            total_swaps=total,
            warnings=warnings,
            slot_statuses=all_slot_statuses,
            locked_salary=all_locked_salary,
            open_slots_count=all_open_counts,
            game_statuses=response_game_statuses,
            bdl_available=bdl_available,
        )
    except Exception as e:
        logger.error(f"Late swap detection failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ── DK CSV Export ─────────────────────────────────────────────────────
# Instant DK-ready CSV from generated lineups.
# Two variants: file download and plain-text clipboard.

_DK_SLOT_ORDER_NBA = ["PG", "SG", "SF", "PF", "C", "G", "F", "UTIL"]
_DK_SLOT_ORDER_CBB = ["G", "G", "G", "F", "F", "F", "UTIL", "UTIL"]


def _lineups_to_dk_csv(
    lineups: list,
    sport: str = "nba",
) -> str:
    """Convert OptimizedLineup list to DK bulk-import CSV string.

    Format accepted by DK's "Import Lineups" feature:
        PG,SG,SF,PF,C,G,F,UTIL
        12345678,23456789,...
    """
    slot_order = _DK_SLOT_ORDER_NBA if sport == "nba" else _DK_SLOT_ORDER_CBB
    header = ",".join(slot_order)
    rows = []
    for lu in lineups:
        # Build slot → dk_player_id map
        slot_map: Dict[str, int] = {}
        for p in lu.players:
            dk_id = getattr(p, "dk_player_id", None) or p.player_id
            slot_map[p.roster_slot] = dk_id
        row = ",".join(str(slot_map.get(s, "")) for s in slot_order)
        rows.append(row)
    return header + "\n" + "\n".join(rows)


@router.post("/export-dk-csv")
def export_dk_csv(
    _auth=Depends(require_api_key),
    request: Request = None,
    sport: str = Query("nba", description="Sport (nba or cbb)"),
    lineups: list = None,
    services=Depends(get_services),
):
    """Export lineups as a DraftKings bulk-import CSV file.

    Accepts lineups as JSON body (list of OptimizedLineup dicts).
    Returns a downloadable CSV with dk_player_ids per roster slot.
    """
    from fastapi.responses import StreamingResponse
    import io

    if not lineups:
        # Try to get from request body
        try:
            import json
            body = request._body if hasattr(request, "_body") else None
            if body:
                lineups = json.loads(body)
        except Exception:
            pass

    if not lineups:
        raise HTTPException(
            status_code=400,
            detail="No lineups provided. Pass lineups as JSON body.",
        )

    # Accept both raw dicts and OptimizedLineup objects
    from app.models.lineup import OptimizedLineup as _OL
    parsed = []
    for lu in lineups:
        if isinstance(lu, dict):
            try:
                parsed.append(_OL(**lu))
            except Exception as e:
                logger.warning(f"[DK Export] Skipping invalid lineup: {e}")
                continue
        else:
            parsed.append(lu)

    if not parsed:
        raise HTTPException(status_code=400, detail="No valid lineups to export.")

    csv_text = _lineups_to_dk_csv(parsed, sport)

    # Save to disk for browser upload workflow
    import os
    from datetime import datetime
    export_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
        "exports",
    )
    os.makedirs(export_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    export_path = os.path.join(export_dir, f"dk_lineups_{ts}.csv")
    with open(export_path, "w", encoding="utf-8") as f:
        f.write(csv_text)

    logger.info(
        "[DK Export] Exported %d lineups to %s (%d bytes)",
        len(parsed), export_path, len(csv_text),
    )

    buf = io.StringIO(csv_text)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="dk_lineups_{ts}.csv"',
            "X-Lineups-Count": str(len(parsed)),
            "X-Export-Path": export_path,
        },
    )


@router.post("/export-dk-csv/clipboard")
def export_dk_csv_clipboard(
    _auth=Depends(require_api_key),
    sport: str = Query("nba", description="Sport (nba or cbb)"),
    lineups: list = None,
):
    """Export lineups as plain-text DK player IDs for clipboard paste.

    Returns plain text: one line per lineup, comma-separated dk_player_ids.
    """
    if not lineups:
        raise HTTPException(status_code=400, detail="No lineups provided.")

    from app.models.lineup import OptimizedLineup as _OL
    parsed = []
    for lu in lineups:
        if isinstance(lu, dict):
            try:
                parsed.append(_OL(**lu))
            except Exception:
                continue
        else:
            parsed.append(lu)

    if not parsed:
        raise HTTPException(status_code=400, detail="No valid lineups.")

    csv_text = _lineups_to_dk_csv(parsed, sport)
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(csv_text)


@router.post("/late-swap/fast-patch")
def fast_patch(
    _auth=Depends(require_api_key),
    scratched_player_id: int = Query(..., description="Player ID of the scratched player"),
    request: Request = None,
    services=Depends(get_services),
):
    """Delta-patch saved lineups when a player is scratched before lock.

    Greedy single-player replacement — no ILP re-solve.
    Processes 150 lineups in <50ms.

    Requires a current player pool (will use cached pool if available).
    Patches lineups in-place and returns the patch report.
    """
    from app.services.late_swap_service import fast_patch_lineups

    try:
        # Get saved lineups from the optimizer service
        optimizer = services.lineup_optimizer_service
        saved = getattr(optimizer, "_last_generated_lineups", None)
        if not saved:
            raise HTTPException(
                status_code=404,
                detail="No saved lineups found. Generate lineups first.",
            )

        # Get the current player pool (cached)
        pool_result = optimizer.build_player_pool(
            platform="dk", draft_group_id=0, game_date="",
            excluded=set(), sport="nba",
        )
        pool = pool_result[0] if isinstance(pool_result, tuple) else pool_result

        report = fast_patch_lineups(
            scratched_player_id=scratched_player_id,
            player_pool=pool,
            lineups=saved,
        )

        return {
            "scratched_player": report.scratched_player_name,
            "lineups_scanned": report.lineups_scanned,
            "lineups_affected": report.lineups_affected,
            "lineups_patched": report.lineups_patched,
            "lineups_failed": report.lineups_failed,
            "elapsed_ms": report.elapsed_ms,
            "patches": [
                {
                    "lineup_index": p.lineup_index,
                    "swapped_out": p.swapped_out,
                    "swapped_in": p.swapped_in,
                    "salary_delta": p.salary_delta,
                    "fp_delta": p.fp_delta,
                    "lineup_total_salary": p.lineup_total_salary,
                    "lineup_total_fp": p.lineup_total_fp,
                }
                for p in report.patches
            ],
            "failed_indices": report.failed_indices,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Fast patch failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/late-swap/monitor")
async def get_late_swap_monitor(
    _auth=Depends(require_api_key),
    game_date: Optional[str] = Query(None, description="Game date YYYY-MM-DD"),
    sport: str = Query("nba", description="Sport: nba or cbb"),
):
    """Check for at-risk players and recent injury/news updates.

    Returns current injury/status alerts, recent news updates, and
    whether any critical changes need attention.  Designed to be
    polled every 5 minutes when games are approaching tip-off.
    """
    # CBB late-swap monitoring not yet supported — return empty gracefully
    if sport == "cbb":
        return {
            "injury_updates": [],
            "news_updates": [],
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "has_critical_updates": False,
        }

    try:
        svc = get_services()
        updates = svc.late_swap_monitor.check_for_updates(game_date=game_date)
        return updates
    except Exception as e:
        logger.error(f"Late swap monitor failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/late-swap/monitor/full")
def late_swap_monitor_full(
    _auth=Depends(require_api_key),
    request: LateSwapMonitorRequest = ...,
):
    """Full late-swap monitoring: at-risk players, updates, and swap suggestions.

    Accepts lineups and builds the player pool to provide comprehensive
    monitoring including at-risk player detection, recent injury/news
    updates, and automated swap suggestions for affected players.
    """
    # Sport comes from the request body (LateSwapMonitorRequest.sport),
    # NOT a query param — the frontend sends it in the JSON body.
    sport = request.sport

    # CBB late-swap monitoring not yet supported — return empty gracefully
    if sport == "cbb":
        return {
            "recent_updates": {
                "injury_updates": [],
                "news_updates": [],
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "has_critical_updates": False,
            },
            "at_risk_players": [],
            "swap_suggestions": None,
            "needs_attention": False,
        }

    try:
        from datetime import date as _date

        svc = get_services()
        gd = request.game_date or _date.today().isoformat()

        # Build and enrich pool for swap suggestions
        pool = svc.lineup_optimizer_service.build_player_pool(
            platform=request.platform,
            draft_group_id=request.draft_group_id,
            game_date=gd,
            sport=sport,
            data_service=svc.get_data_service(sport),
            game_service_override=svc.get_game_service(sport),
            injury_service_override=svc.get_injury_service(sport),
        )
        pool = svc.lineup_optimizer_service._enrich_pool(
            pool, request.platform, gd,
        )

        result = svc.late_swap_monitor.get_monitor_status(
            lineups=request.lineups,
            pool=pool,
            game_date=gd,
        )
        return result
    except Exception as e:
        logger.error(f"Late swap full monitor failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/fade-list")
async def get_fade_list(
    platform: str = Query("dk", description="dk or fd"),
    draft_group_id: Optional[int] = Query(None),
    game_date: Optional[str] = Query(None),
    sport: str = Query("nba", description="Sport: nba or cbb"),
):
    """Generate contrarian fade and leverage play lists.

    Identifies high-ownership players with limited ceiling (fades)
    and low-ownership players with high ceiling (leverage plays).
    Requires a previously built player pool in the optimizer cache.
    """
    try:
        svc = get_services()
        # Try to get pool from optimizer cache
        pool = svc.lineup_optimizer_service.get_cached_pool(sport=sport)

        if not pool:
            return {
                "fades": [],
                "leverage_plays": [],
                "message": (
                    "No player pool in cache. Build a pool first via "
                    "/player-pool endpoint."
                ),
            }

        result = svc.fade_service.generate_fade_list(pool)
        return result
    except Exception as e:
        logger.error(f"Fade list generation failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/ownership/simulate")
@limiter.limit("10/minute")
async def simulate_ownership(
    request: Request,
    _auth=Depends(require_api_key),
    sport: str = Query("nba", description="Sport: nba or cbb"),
):
    """Run Monte Carlo ownership simulation for a user lineup.

    Estimates win rate, min-cash rate, and expected ROI by simulating
    opponent lineups constructed from ownership projections.

    Body JSON: {lineup, field_size?, num_simulations?}
    - lineup: list of {player_id, player_name, projected_fp, ownership_pct, ceiling_fp, floor_fp}
    """
    try:
        svc = get_services()

        body = await request.json()

        user_lineup = body.get("lineup", [])
        field_size = body.get("field_size", 1000)
        num_simulations = body.get("num_simulations", 500)

        if not user_lineup:
            raise HTTPException(status_code=400, detail="No lineup provided")

        # Get pool from optimizer cache
        pool = svc.lineup_optimizer_service.get_cached_pool(sport=sport)
        if not pool:
            return {
                "error": "No player pool in cache. Build a pool first.",
                "result": None,
            }

        from app.services.ownership_simulator import SimulationResult
        result = svc.ownership_simulator.simulate(
            user_lineup=user_lineup,
            pool=pool,
            field_size=field_size,
            num_simulations=num_simulations,
            sport=sport,
        )

        return {
            "result": {
                "avg_percentile": result.avg_percentile,
                "win_rate": result.win_rate,
                "top_1pct_rate": result.top_1pct_rate,
                "top_10pct_rate": result.top_10pct_rate,
                "min_cash_rate": result.min_cash_rate,
                "expected_roi": result.expected_roi,
                "avg_score": result.avg_score,
                "score_std": result.score_std,
                "field_avg_score": result.field_avg_score,
                "simulations_run": result.simulations_run,
                "chalk_comparison": result.chalk_comparison,
                "contrarian_comparison": result.contrarian_comparison,
            },
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Ownership simulation failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ──────────────────────────────────────────────────────────────────
# Solver Tracking & ROI Analytics
# ──────────────────────────────────────────────────────────────────


@router.get("/solver/roi-report")
@limiter.limit("30/minute")
async def solver_roi_report(
    request: Request,
    _auth=Depends(require_api_key),
    sport: Optional[str] = Query(None, description="Filter by sport (nba/cbb)"),
    platform: Optional[str] = Query(None, description="Filter by platform (dk/fd)"),
    min_batches: int = Query(1, ge=1, description="Minimum batch runs to include"),
):
    """Return per-solver-configuration ROI report.

    Joins solver_configurations, lineup_batches, and lineup_results
    to compute total net profit, average scores, and ROI percentage
    for each distinct solver configuration.  Ordered by highest net
    profit first.
    """
    svc = get_services()
    tracker = svc.solver_tracking_service
    if tracker is None:
        raise HTTPException(status_code=503, detail="Solver tracking not available")

    report = await tracker.get_strategy_roi_report(
        sport=sport, platform=platform, min_batches=min_batches,
    )
    return {"report": report, "count": len(report)}


@router.get("/solver/batch/{batch_id}")
@limiter.limit("30/minute")
async def solver_batch_detail(
    request: Request,
    batch_id: int,
    _auth=Depends(require_api_key),
):
    """Return details for a specific solver batch (config + lineup results)."""
    svc = get_services()
    tracker = svc.solver_tracking_service
    if tracker is None:
        raise HTTPException(status_code=503, detail="Solver tracking not available")

    detail = await tracker.get_batch_detail(batch_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"Batch {batch_id} not found")
    return detail


@router.post("/solver/batch/{batch_id}/backfill")
@limiter.limit("10/minute")
async def solver_backfill_results(
    request: Request,
    batch_id: int,
    _auth=Depends(require_api_key),
    body: dict = ...,
):
    """Back-fill actual scores and P&L for a settled batch.

    Body should contain ``results``: a list of dicts, each with:
      - lineup_hash (str)
      - actual_score (float)
      - entry_fee_total (float, optional)
      - winnings_total (float, optional)
    """
    svc = get_services()
    tracker = svc.solver_tracking_service
    if tracker is None:
        raise HTTPException(status_code=503, detail="Solver tracking not available")

    results = body.get("results", [])
    if not results:
        raise HTTPException(status_code=400, detail="No results provided")

    updated = await tracker.backfill_results(batch_id, results)
    return {"batch_id": batch_id, "rows_updated": updated}


# ── Improvement #4: External Ownership Import ────────────────────────


@router.post("/player-pool/import-ownership")
@limiter.limit("10/minute")
async def import_ownership(
    request: Request,
    _auth=Depends(require_api_key),
):
    """Import external ownership projections from CSV upload.

    Accepts a CSV file (form-data) with columns:
      - ``player_name`` (required)
      - ``ownership_pct`` or ``Ownership`` or ``Own%`` (required)

    Imported values override the model-based ownership for matching
    players until the next server restart or a new CSV upload.
    """
    import csv
    import io
    from fastapi import UploadFile, File

    form = await request.form()
    file = form.get("file")
    if file is None:
        raise HTTPException(status_code=400, detail="No file uploaded")

    try:
        content = await file.read()
        text = content.decode("utf-8-sig")
    except Exception as e:
        raise HTTPException(
            status_code=400, detail=f"Could not read file: {e}"
        )

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise HTTPException(
            status_code=400, detail="CSV has no headers"
        )

    # Detect ownership column
    own_col = None
    for candidate in ("ownership_pct", "Ownership", "Ownership %", "Own%", "own%", "ownership"):
        if candidate in reader.fieldnames:
            own_col = candidate
            break
    if own_col is None:
        raise HTTPException(
            status_code=400,
            detail=(
                f"No ownership column found. Expected one of: "
                f"ownership_pct, Ownership, Own%. "
                f"Got: {reader.fieldnames}"
            ),
        )

    # Detect player name column
    name_col = None
    for candidate in ("player_name", "Player", "Name", "player", "name"):
        if candidate in reader.fieldnames:
            name_col = candidate
            break
    if name_col is None:
        raise HTTPException(
            status_code=400,
            detail=(
                f"No player name column found. Expected one of: "
                f"player_name, Player, Name. "
                f"Got: {reader.fieldnames}"
            ),
        )

    # Detect optional Starting column (Data Hub CSV includes this)
    start_col = None
    for candidate in (
        "Starting", "starting", "Starter", "starter",
        "Start", "Is_Starting", "is_starting",
    ):
        if candidate in reader.fieldnames:
            start_col = candidate
            break

    from app.services.dk_draftables_service import _normalize_name
    from app.services.lineup_optimizer_service import (
        _imported_ownership,
        _imported_ownership_lock,
        _imported_starters,
        _imported_starters_lock,
    )

    imported = {}
    starters_imported = {}
    errors = []
    for i, row in enumerate(reader, start=2):
        name_raw = row.get(name_col, "").strip()
        own_raw = row.get(own_col, "").strip().rstrip("%")
        if not name_raw or not own_raw:
            continue
        try:
            own_val = float(own_raw)
            if own_val > 1.0 and own_val <= 100.0:
                pass  # already percentage
            elif 0.0 < own_val <= 1.0:
                own_val *= 100.0  # convert decimal to percentage
            else:
                errors.append(f"Row {i}: invalid ownership {own_raw}")
                continue
            norm_name = _normalize_name(name_raw)
            imported[norm_name] = round(own_val, 1)

            # Parse Starting flag (if column exists in CSV)
            if start_col:
                start_raw = row.get(start_col, "").strip().lower()
                if start_raw in ("true", "yes", "1", "y"):
                    starters_imported[norm_name] = True
        except ValueError:
            errors.append(f"Row {i}: could not parse '{own_raw}' as number")

    if not imported:
        raise HTTPException(
            status_code=400,
            detail=f"No valid ownership rows found. Errors: {errors[:10]}",
        )

    with _imported_ownership_lock:
        _imported_ownership.clear()
        _imported_ownership.update(imported)

    # Also update confirmed starters if Starting column was found
    if starters_imported:
        with _imported_starters_lock:
            _imported_starters.clear()
            _imported_starters.update(starters_imported)

    return {
        "imported": len(imported),
        "starters": len(starters_imported),
        "errors": errors[:10] if errors else [],
        "message": (
            f"Imported ownership for {len(imported)} players"
            + (f", {len(starters_imported)} confirmed starters" if starters_imported else "")
            + ". These will override model data in the next generation."
        ),
    }


@router.delete("/player-pool/import-ownership")
@limiter.limit("10/minute")
async def clear_imported_ownership(
    request: Request,
    _auth=Depends(require_api_key),
):
    """Clear all imported external ownership and starter data."""
    from app.services.lineup_optimizer_service import (
        _imported_ownership,
        _imported_ownership_lock,
        _imported_starters,
        _imported_starters_lock,
    )

    with _imported_ownership_lock:
        count = len(_imported_ownership)
        _imported_ownership.clear()
    with _imported_starters_lock:
        starter_count = len(_imported_starters)
        _imported_starters.clear()

    return {
        "cleared": count,
        "starters_cleared": starter_count,
        "message": f"Cleared {count} ownership + {starter_count} starter entries",
    }


@router.post("/player-pool/import-projections")
@limiter.limit("10/minute")
async def import_projections(
    request: Request,
    sport: str = Query("nba", description="Sport: nba, cbb, nfl, or mlb"),
    _auth=Depends(require_api_key),
):
    """Import external consensus projections from CSV upload.

    Accepts a CSV file (form-data) with columns:
      - ``Player`` or ``player_name`` or ``Name`` (required)
      - ``Projection`` or ``FPTS`` or ``projected_fp`` (required)
      - ``Floor`` or ``floor_fp`` (optional)
      - ``Ceiling`` or ``ceiling_fp`` (optional)

    The ``sport`` query parameter (or ``sport`` form field) routes the
    import to the correct sport's player pool and the correct slice
    of the alias map. An NFL upload will not be applied to MLB
    projections and vice versa. Defaults to ``nba`` for backward compat.

    Imported values override rotation-based projections for matching
    players until the next server restart or a new CSV upload.
    """
    import csv
    import io

    form = await request.form()
    file = form.get("file")
    if file is None:
        raise HTTPException(status_code=400, detail="No file uploaded")

    # Form-data ``sport`` overrides the query param when both are present —
    # the multipart frontend uses form data, the JSON test path uses query.
    form_sport = form.get("sport")
    if form_sport:
        sport = str(form_sport).strip().lower() or sport
    sport = (sport or "nba").lower()

    try:
        content = await file.read()
        text = content.decode("utf-8-sig")
    except Exception as e:
        raise HTTPException(
            status_code=400, detail=f"Could not read file: {e}"
        )

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise HTTPException(
            status_code=400, detail="CSV has no headers"
        )

    # Detect projection column
    proj_col = None
    for candidate in (
        "Projection", "projected_fp", "FPTS", "fpts",
        "projection", "Proj", "proj",
    ):
        if candidate in reader.fieldnames:
            proj_col = candidate
            break
    if proj_col is None:
        raise HTTPException(
            status_code=400,
            detail=(
                f"No projection column found. Expected one of: "
                f"Projection, projected_fp, FPTS. "
                f"Got: {reader.fieldnames}"
            ),
        )

    # Detect player name column
    name_col = None
    for candidate in ("Player", "player_name", "Name", "player", "name"):
        if candidate in reader.fieldnames:
            name_col = candidate
            break
    if name_col is None:
        raise HTTPException(
            status_code=400,
            detail=(
                f"No player name column found. Expected one of: "
                f"Player, player_name, Name. "
                f"Got: {reader.fieldnames}"
            ),
        )

    # Detect optional floor/ceiling columns
    floor_col = None
    for candidate in ("Floor", "floor_fp", "floor"):
        if candidate in reader.fieldnames:
            floor_col = candidate
            break
    ceiling_col = None
    for candidate in ("Ceiling", "ceiling_fp", "ceiling"):
        if candidate in reader.fieldnames:
            ceiling_col = candidate
            break

    from app.services.dk_draftables_service import _normalize_name
    from app.services.lineup_optimizer_service import (
        _imported_projections,
        _imported_projections_lock,
        _enriched_cache,
        _pool_cache,
        _pool_lock,
    )
    from app.services import projection_alias_service

    # Sport-partitioned alias lookup — an MLB upload doesn't see NFL
    # aliases, preventing cross-sport name-collision bugs.
    aliases = projection_alias_service.list_aliases(sport=sport)

    # ── NFL DST preprocessing ───────────────────────────────────────
    # Build a lookup of team mascots, full names, and abbreviations for
    # auto-formatting CSV "Cowboys" / "DAL" / "Dallas Cowboys" entries
    # to the canonical DK form "<full team name> DST". Saves the user
    # from manually aliasing every defense, every week.
    nfl_dst_lookup: Dict[str, str] = {}  # any mascot/abbr/full-name → "Full Name DST"
    if sport == "nfl":
        try:
            from app.services.nfl_data_service import NFLDataService
            for t in NFLDataService().get_all_teams():
                full = t["full_name"]
                canonical = f"{full} DST"
                # Mascot (last word of full name): "Cowboys" → "Dallas Cowboys DST"
                mascot = full.rsplit(" ", 1)[-1] if " " in full else full
                nfl_dst_lookup[_normalize_name(mascot)] = canonical
                # Abbreviation: "DAL" → "Dallas Cowboys DST"
                nfl_dst_lookup[_normalize_name(t["abbreviation"])] = canonical
                # Full name (no DST suffix): "Dallas Cowboys" → canonical
                nfl_dst_lookup[_normalize_name(full)] = canonical
                # Already-suffixed forms also resolve to themselves
                nfl_dst_lookup[_normalize_name(canonical)] = canonical
                nfl_dst_lookup[_normalize_name(f"{mascot} DST")] = canonical
                nfl_dst_lookup[_normalize_name(f"{t['abbreviation']} DST")] = canonical
        except Exception as exc:
            logger.warning("[ImportProj] NFL DST preprocessor setup failed: %s", exc)

    def _maybe_resolve_dst(raw_csv_name: str) -> Optional[str]:
        """Return the canonical "<Full> DST" name when the CSV row
        is recognizably an NFL defense; None otherwise (regular skill
        players pass through unchanged)."""
        if sport != "nfl" or not nfl_dst_lookup:
            return None
        norm = _normalize_name(raw_csv_name)
        # Direct hit in the lookup (mascot, full, abbr, suffix variants)
        if norm in nfl_dst_lookup:
            return nfl_dst_lookup[norm]
        # Strip trailing " dst" / " defense" / " d/st" and retry
        for suffix in (" dst", " defense", " d/st"):
            if norm.endswith(suffix):
                stripped = norm[: -len(suffix)].strip()
                if stripped in nfl_dst_lookup:
                    return nfl_dst_lookup[stripped]
        return None

    # Two parallel maps:
    #   imported           — keyed by canonical normalized name (alias-applied)
    #   raw_csv_rows       — keyed by raw normalized CSV name, used to compute
    #                        unmatched diagnostic against the pool below
    imported: Dict[str, Dict[str, float]] = {}
    raw_csv_rows: Dict[str, Dict[str, float]] = {}
    raw_to_csv_display: Dict[str, str] = {}  # norm -> original CSV display
    errors = []
    aliases_applied = 0
    dst_auto_resolved = 0
    zeroed_count = 0
    for i, row in enumerate(reader, start=2):
        name_raw = row.get(name_col, "").strip()
        proj_raw = row.get(proj_col, "").strip()
        if not name_raw or not proj_raw:
            continue
        try:
            proj_val = float(proj_raw)
            # Zero is a legitimate signal — "this player is OUT, project at 0".
            # Reject only negatives, which are clearly malformed data.
            if proj_val < 0:
                errors.append(f"Row {i}: projection must be >= 0, got {proj_raw}")
                continue
            entry: Dict[str, float] = {"projected_fp": round(proj_val, 1)}
            if floor_col:
                floor_raw = row.get(floor_col, "").strip()
                if floor_raw:
                    entry["floor_fp"] = round(float(floor_raw), 1)
            if ceiling_col:
                ceil_raw = row.get(ceiling_col, "").strip()
                if ceil_raw:
                    entry["ceiling_fp"] = round(float(ceil_raw), 1)
            # When the user imports a 0-projection (OUT player), force
            # floor and ceiling to 0 too unless they explicitly overrode
            # them. Otherwise the rotation engine's existing floor/ceiling
            # would survive the import and the player still looks playable
            # to the lineup builder (e.g., 0 proj but 30 ceiling).
            if proj_val == 0:
                entry.setdefault("floor_fp", 0.0)
                entry.setdefault("ceiling_fp", 0.0)
                zeroed_count += 1

            # NFL DST auto-resolution: rewrite the CSV name if it's
            # recognizably a defense before alias lookup runs. This is
            # an in-memory transform — the saved alias still keys off
            # whatever raw form the user typed.
            csv_norm = _normalize_name(name_raw)
            dst_canonical = _maybe_resolve_dst(name_raw)
            if dst_canonical:
                csv_norm = _normalize_name(dst_canonical)
                dst_auto_resolved += 1

            raw_csv_rows[csv_norm] = entry
            raw_to_csv_display[csv_norm] = dst_canonical or name_raw

            # Apply alias map: if user previously matched "csv_norm" to a
            # different canonical name (within the same sport), store the
            # projection under that canonical key so the pool walk finds it.
            alias_target = aliases.get(csv_norm)
            if alias_target and alias_target.get("canonical_normalized"):
                store_key = alias_target["canonical_normalized"]
                aliases_applied += 1
            else:
                store_key = csv_norm
            imported[store_key] = entry
        except ValueError:
            errors.append(f"Row {i}: could not parse '{proj_raw}' as number")

    if not imported:
        raise HTTPException(
            status_code=400,
            detail=f"No valid projection rows found. Errors: {errors[:10]}",
        )

    with _imported_projections_lock:
        _imported_projections.clear()
        _imported_projections.update(imported)

    # Invalidate caches so the next pool read / generation applies the
    # new overrides to freshly-built entries (avoids mutating cached
    # pool entries with stale values on subsequent imports).
    _enriched_cache.clear()
    with _pool_lock:
        cached_pools = [entry for entry in _pool_cache.values() if isinstance(entry, tuple)]
        _pool_cache.clear()

    # ── Compute unmatched diagnostic ─────────────────────────────────
    # An unmatched CSV row is one whose normalized name (after alias
    # resolution) doesn't correspond to any player in the most recent
    # cached pool we have on hand. We use the freshest cached pool — if
    # there is none, the diagnostic is "unknown" and gets returned empty.
    unmatched: list = []
    if cached_pools:
        # Each entry is (timestamp, pool_list, expected_team_count) — pick
        # the freshest by timestamp.
        latest = max(cached_pools, key=lambda t: t[0] if t and len(t) > 0 else 0)
        if isinstance(latest, tuple) and len(latest) >= 2 and isinstance(latest[1], list):
            pool_list = latest[1]
            pool_norm_names = {
                _normalize_name(p.player_name): p
                for p in pool_list
                if getattr(p, "player_name", None)
            }
            # Walk every raw CSV row; if (after alias) it's not a pool key,
            # it's unmatched. Also report fuzzy suggestions.
            import difflib
            pool_name_list = list(pool_norm_names.keys())
            for csv_norm, entry in raw_csv_rows.items():
                effective = csv_norm
                alias_target = aliases.get(csv_norm)
                if alias_target and alias_target.get("canonical_normalized"):
                    effective = alias_target["canonical_normalized"]
                if effective in pool_norm_names:
                    continue
                # Suggest top-3 closest pool names (ratio-cutoff 0.6)
                suggestions = []
                close = difflib.get_close_matches(
                    csv_norm, pool_name_list, n=3, cutoff=0.6,
                )
                for cn in close:
                    p = pool_norm_names[cn]
                    suggestions.append({
                        "player_id": getattr(p, "player_id", None),
                        "name": p.player_name,
                        "team": getattr(p, "team_abbreviation", None),
                        "position": getattr(p, "position", None),
                        "salary": getattr(p, "salary", None),
                    })
                unmatched.append({
                    "csv_name": raw_to_csv_display.get(csv_norm, csv_norm),
                    "csv_normalized": csv_norm,
                    "projection": entry.get("projected_fp"),
                    "floor": entry.get("floor_fp"),
                    "ceiling": entry.get("ceiling_fp"),
                    "suggestions": suggestions,
                })

    return {
        "sport": sport,
        "imported": len(imported),
        "aliases_applied": aliases_applied,
        "dst_auto_resolved": dst_auto_resolved,
        "zeroed_count": zeroed_count,
        "unmatched": unmatched,
        "unmatched_count": len(unmatched),
        "pool_diagnosed": bool(cached_pools),
        "errors": errors[:10] if errors else [],
        "columns": {
            "name": name_col,
            "projection": proj_col,
            "floor": floor_col,
            "ceiling": ceiling_col,
        },
        "message": (
            f"[{sport.upper()}] Imported projections for {len(imported)} players "
            f"({aliases_applied} via saved aliases"
            + (f", {dst_auto_resolved} DST auto-resolved" if dst_auto_resolved else "")
            + (f", {zeroed_count} zeroed-out OUT players" if zeroed_count else "")
            + "). "
            + (
                f"{len(unmatched)} CSV row(s) didn't match any pool player — "
                f"open the unmatched-names dialog to map them."
                if unmatched else ""
            )
        ),
    }


# ------------------------------------------------------------------
# Projection alias endpoints
# ------------------------------------------------------------------

@router.get("/player-pool/projection-aliases")
async def list_projection_aliases(
    sport: Optional[str] = Query(None, description="Filter to one sport (nba/cbb/nfl/mlb)"),
    _auth=Depends(require_api_key),
):
    """List saved CSV-name → pool-name aliases.

    With ``?sport=`` filters to that sport's bucket; otherwise returns
    the full nested ``{sport: {csv_norm: entry}}`` map.
    """
    from app.services import projection_alias_service
    return {
        "sport": sport,
        "aliases": projection_alias_service.list_aliases(sport),
    }


@router.post("/player-pool/projection-aliases")
async def add_projection_alias(
    request: Request,
    sport: str = Query("nba", description="Sport: nba, cbb, nfl, or mlb"),
    _auth=Depends(require_api_key),
):
    """Persist a manual CSV-name → pool-player match.

    Request body (JSON)::

        {
          "csv_name":     "Quenton Jackson",       // raw CSV name (any case/diacritics)
          "player_id":    1641705,                 // pool player_id (preferred)
          "canonical_name": "Quentin Jackson"      // optional; resolved from player_id if omitted
        }

    Either ``player_id`` or ``canonical_name`` must be provided. When
    ``player_id`` is given, the canonical name is looked up from the most
    recent cached pool. The mapping is also written back into the live
    ``_imported_projections`` dict so the current import takes effect on
    the next pool fetch — no re-upload required.
    """
    from app.services.dk_draftables_service import _normalize_name
    from app.services import projection_alias_service
    from app.services.lineup_optimizer_service import (
        _imported_projections,
        _imported_projections_lock,
        _enriched_cache,
        _pool_cache,
        _pool_lock,
    )

    body = await request.json()
    csv_name = (body.get("csv_name") or "").strip()
    player_id = body.get("player_id")
    canonical_name = (body.get("canonical_name") or "").strip()
    # Body sport overrides the query param when both are present.
    body_sport = (body.get("sport") or "").strip().lower()
    if body_sport:
        sport = body_sport
    sport = (sport or "nba").lower()

    if not csv_name:
        raise HTTPException(status_code=400, detail="csv_name is required")
    if player_id is None and not canonical_name:
        raise HTTPException(
            status_code=400,
            detail="Provide either player_id or canonical_name",
        )

    csv_norm = _normalize_name(csv_name)

    # Resolve canonical name from player_id via the latest cached pool
    team = None
    with _pool_lock:
        cached = [e for e in _pool_cache.values() if isinstance(e, tuple)]
    if cached:
        latest = max(cached, key=lambda t: t[0] if t and len(t) > 0 else 0)
        if isinstance(latest, tuple) and len(latest) >= 2 and isinstance(latest[1], list):
            for p in latest[1]:
                if player_id is not None and getattr(p, "player_id", None) == int(player_id):
                    canonical_name = canonical_name or p.player_name
                    team = getattr(p, "team_abbreviation", None)
                    break
                if canonical_name and getattr(p, "player_name", "") == canonical_name:
                    player_id = player_id or getattr(p, "player_id", None)
                    team = getattr(p, "team_abbreviation", None)
                    break

    if not canonical_name:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Could not resolve canonical name from player_id={player_id}. "
                "Pool may not be cached — load the slate first, then retry."
            ),
        )

    canonical_norm = _normalize_name(canonical_name)

    entry = projection_alias_service.add_alias(
        csv_normalized_name=csv_norm,
        canonical_name=canonical_name,
        canonical_normalized=canonical_norm,
        sport=sport,
        player_id=int(player_id) if player_id is not None else None,
        team=team,
    )

    # Re-key the live imported_projections so the current import benefits
    # without requiring a re-upload.
    rekeyed = False
    with _imported_projections_lock:
        if csv_norm in _imported_projections:
            _imported_projections[canonical_norm] = _imported_projections.pop(csv_norm)
            rekeyed = True

    if rekeyed:
        # Pool cache must rebuild so the override actually applies on read.
        _enriched_cache.clear()
        with _pool_lock:
            _pool_cache.clear()

    return {
        "sport": sport,
        "alias": {csv_norm: entry},
        "applied_to_current_import": rekeyed,
    }


@router.delete("/player-pool/projection-aliases/{csv_name}")
async def delete_projection_alias(
    csv_name: str,
    sport: str = Query("nba", description="Sport: nba, cbb, nfl, or mlb"),
    _auth=Depends(require_api_key),
):
    """Remove a saved alias by its raw CSV name (any case) within ``sport``."""
    from app.services.dk_draftables_service import _normalize_name
    from app.services import projection_alias_service
    csv_norm = _normalize_name(csv_name)
    removed = projection_alias_service.remove_alias(csv_norm, sport=sport)
    if not removed:
        raise HTTPException(
            status_code=404, detail=f"No alias for {csv_name!r} under sport={sport!r}",
        )
    return {"removed": csv_norm, "sport": sport}


@router.delete("/player-pool/import-projections")
@limiter.limit("10/minute")
async def clear_imported_projections(
    request: Request,
    _auth=Depends(require_api_key),
):
    """Clear all imported external projection data."""
    from app.services.lineup_optimizer_service import (
        _imported_projections,
        _imported_projections_lock,
        _enriched_cache,
        _pool_cache,
        _pool_lock,
    )

    with _imported_projections_lock:
        count = len(_imported_projections)
        _imported_projections.clear()

    # Invalidate caches so stale post-override values don't leak through
    # after the user removes the import.
    _enriched_cache.clear()
    with _pool_lock:
        _pool_cache.clear()

    return {
        "cleared": count,
        "message": f"Cleared {count} imported projection entries",
    }


# ── Analytics Export ──────────────────────────────────────────────────
@router.post("/export-analytics")
@limiter.limit("5/minute")
def export_analytics(
    request: Request,
    _auth=Depends(require_api_key),
    platform: str = Query("dk", description="Platform: dk or fd"),
    draft_group_id: int = Query(..., description="DK DraftGroup ID"),
    game_date: Optional[str] = Query(None, description="Game date YYYY-MM-DD"),
    sport: str = Query("nba", description="Sport: nba or cbb"),
):
    """Export player metrics and game environments to CSV/XLSX for auditing.

    Builds the player pool (or uses cache), then exports two reports:
    - player_metrics_report.csv/.xlsx — sorted by Leverage descending
    - game_environments_report.csv/.xlsx — sorted by Vegas Total descending

    Returns the file paths of the exported files.
    """
    from app.services.data_export_service import DataExportService
    from app.services.lineup_optimizer_service import (
        _pool_cache, _pool_lock, _POOL_CACHE_TTL,
        _cache_key, _load_pool_from_file,
    )

    try:
        svc = get_services()
        gd = game_date or __import__("datetime").date.today().isoformat()

        # Try to get pool from cache first
        cache_key = _cache_key(f"{sport}:{platform}", draft_group_id, gd)
        pool = None
        now = time.time()
        with _pool_lock:
            entry = _pool_cache.get(cache_key)
            if entry and now - entry[0] < _POOL_CACHE_TTL:
                pool = entry[1]

        if pool is None:
            _file_result = _load_pool_from_file(cache_key)
            if _file_result:
                pool = _file_result[0]

        if not pool:
            # Build fresh pool
            pool_resp = svc.lineup_optimizer_service.build_player_pool(
                platform=platform,
                draft_group_id=draft_group_id,
                game_date=gd,
                sport=sport,
            )
            pool = pool_resp.get("players", []) if isinstance(pool_resp, dict) else pool_resp

        # Get game environments if available
        games = []
        try:
            if svc.game_service and sport == "nba":
                games = svc.game_service.get_games_for_date(gd) or []
        except Exception as _ge:
            logger.debug(f"[Export] Could not fetch games: {_ge}")

        exporter = DataExportService()
        result = exporter.export_all(pool, games if games else None)

        # Auto-email reports if SMTP is configured
        email_sent = False
        try:
            from app.services.notification_service import NotificationService
            notifier = NotificationService()
            if notifier.is_available:
                # Collect all generated file paths
                all_files = []
                for category in result.values():
                    if isinstance(category, dict):
                        all_files.extend(category.values())

                if all_files:
                    from datetime import date as _date
                    _today = game_date or _date.today().isoformat()
                    email_sent = notifier.send_slate_reports(
                        subject=f"DFS Analytics Report — {_today} {sport.upper()} {platform.upper()}",
                        body=(
                            f"Attached: Player metrics and game environment reports\n"
                            f"Slate: {sport.upper()} {platform.upper()} (DG {draft_group_id})\n"
                            f"Pool size: {len(pool) if pool else 0} players\n"
                            f"Games: {len(games)}\n\n"
                            f"Reports sorted by Leverage (best tournament plays first)."
                        ),
                        file_paths=all_files,
                    )
        except Exception as _email_err:
            logger.debug(f"[Export] Email notification failed: {_email_err}")

        return {
            "status": "success",
            "pool_size": len(pool) if pool else 0,
            "games_count": len(games),
            "files": result,
            "email_sent": email_sent,
        }
    except Exception as e:
        logger.error(f"Analytics export failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
