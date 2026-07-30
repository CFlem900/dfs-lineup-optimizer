"""Teams, rotation, injuries, coaching, and player projection endpoints."""

import asyncio
import functools
import logging
import threading
import time as _time_mod
from datetime import date, timedelta
from typing import Dict, List, Optional, Set, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.dependencies import get_services
from app.models.player import PlayerMinutes, PlayerProjection
from app.models.rotation import TeamRotation
from app.models.coach import COACH_PROFILES, get_coach_profile
from app.models.responses import TeamsResponse, ConferencesResponse
from app.config.constants import DK_TO_NBA_ABBR_ALIASES
from app.services.nba_api_service import _circuit_breaker as _nba_circuit_breaker

logger = logging.getLogger(__name__)
router = APIRouter()


# ── Rotation result cache + per-key build lock ────────────────────────
# The Slate tab fires 6 rotation requests in parallel for visible games.
# Each request's heavy path (build_team_rotation → BDL) takes 5–60s and
# is identical for the same (team, date, draft_group, sport) tuple — so
# we collapse duplicate inflight calls onto a single build with a per-key
# lock and serve hot results from a short TTL.  This prevents the
# slate-page fan-out from stalling the browser's HTTP connection pool
# (which then blocks the player-pool fetch on the Lineup tab).
#
# TTL is short (5 min) because rotations can shift on injury news; the
# nba_injuries sync job runs every 15 min and would invalidate downstream
# data, but the rotation engine reads injuries live each build anyway.
_ROTATION_CACHE_TTL = 300.0  # seconds
_rotation_cache: Dict[Tuple, Tuple[float, "TeamRotation"]] = {}
_rotation_locks: Dict[Tuple, threading.Lock] = {}
_rotation_locks_master = threading.Lock()


def _rotation_cache_key(team_id: int, game_date: Optional[str], draft_group_id: Optional[int],
                        apply_coach_adj: bool, sport: str) -> Tuple:
    return (team_id, game_date or "", draft_group_id or 0, bool(apply_coach_adj), sport)


def _get_rotation_lock(key: Tuple) -> threading.Lock:
    """One lock per cache key; created lazily."""
    with _rotation_locks_master:
        lock = _rotation_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _rotation_locks[key] = lock
        return lock


def _read_rotation_cache(key: Tuple):
    entry = _rotation_cache.get(key)
    if not entry:
        return None
    ts, value = entry
    if _time_mod.time() - ts > _ROTATION_CACHE_TTL:
        # Expired — drop it; the build path will refresh.
        _rotation_cache.pop(key, None)
        return None
    return value


def _write_rotation_cache(key: Tuple, value) -> None:
    _rotation_cache[key] = (_time_mod.time(), value)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _check_b2b(team_id: int, game_date: str, sport: str = "nba") -> bool:
    """Check if a team is on the second night of a back-to-back.

    Uses the lightweight ``has_game_on_date()`` which only fetches
    the scoreboard header (1 API call) instead of building full
    GameInfo objects with projections.

    Returns False for CBB (no back-to-backs in college basketball).
    """
    if sport == "cbb":
        return False
    try:
        svc = get_services()
        game_svc = svc.get_game_service(sport)
        gd = date.fromisoformat(game_date)
        yesterday = (gd - timedelta(days=1)).isoformat()
        return game_svc.has_game_on_date(team_id, yesterday)
    except Exception as e:
        logger.warning(f"B2B check failed for team {team_id}: {e}")
        return False


def _extract_dk_team_data(
    draft_group_id: int,
    team_abbreviation: str,
) -> Tuple[Optional[Set[str]], Optional[Dict[str, str]], Optional[Dict[str, int]]]:
    """Extract DK draftable names, positions, and salaries for one team.

    Returns (draftable_names, draftable_positions, draftable_salaries).
    All values are ``None`` when no draft group is available.
    """
    if not draft_group_id:
        return None, None, None
    try:
        svc = get_services()
        draftables = svc.dk_draftables_service.get_draftables(draft_group_id)
        if not draftables:
            return None, None, None

        target_abbr = team_abbreviation.upper()
        dk_reverse = {v: k for k, v in DK_TO_NBA_ABBR_ALIASES.items()}
        dk_alias = dk_reverse.get(target_abbr, target_abbr)

        names: Set[str] = set()
        positions: Dict[str, str] = {}
        salaries: Dict[str, int] = {}

        for p in draftables:
            p_abbr = p.team_abbreviation.upper()
            p_abbr_nba = DK_TO_NBA_ABBR_ALIASES.get(p_abbr, p_abbr)
            if p_abbr_nba != target_abbr and p_abbr != dk_alias:
                continue
            dn = p.display_name
            if dn in names:
                continue
            names.add(dn)
            positions[dn.lower()] = p.position or ""
            salaries[dn.lower()] = p.salary or 0

        if not names:
            return None, None, None
        return names, positions, salaries
    except Exception as e:
        logger.warning(f"[DK extract] Failed for {team_abbreviation} DG {draft_group_id}: {e}")
        return None, None, None


def _attach_dk_salaries(
    team_rotation: TeamRotation,
    draft_group_id: int,
    team_abbreviation: str,
) -> None:
    """Match DK draftable salaries to our roster projections.

    Sets ``dk_salary`` and ``dk_value`` (FP per $1K) on each
    PlayerProjection that matches a DK draftable.
    """
    try:
        svc = get_services()
        lookup = svc.dk_draftables_service.build_salary_lookup(draft_group_id)
        if not lookup:
            return

        matched = 0
        for proj in team_rotation.projections:
            match = svc.dk_draftables_service.match_salary(
                player_name=proj.player_name,
                team_abbreviation=team_abbreviation,
                lookup=lookup,
            )
            if match:
                proj.dk_salary = match.salary
                dk_fp = proj.dk_points
                if dk_fp is not None and dk_fp > 0 and match.salary > 0:
                    proj.dk_value = round(dk_fp / match.salary * 1000, 2)
                matched += 1

        value_count = sum(1 for p in team_rotation.projections if p.dk_value is not None)
        logger.info(
            f"Salary match: {matched}/{len(team_rotation.projections)} "
            f"players for {team_abbreviation} (DG {draft_group_id}), "
            f"{value_count} with dk_value"
        )
    except Exception as e:
        logger.warning(f"Failed to attach DK salaries: {e}")


def _build_dk_fallback_rotation(
    draft_group_id: int,
    team_id: int,
    team_abbreviation: str,
) -> List[PlayerMinutes]:
    """Build a minimal rotation from DK draftables when the NBA API is down.

    Estimates minutes and usage from DK salary tiers using league-average
    priors.  The rotation engine can then project from these baselines.
    """
    svc = get_services()
    draftables = svc.dk_draftables_service.get_draftables(draft_group_id)
    if not draftables:
        return []

    # Normalize team abbreviation for comparison
    target_abbr = team_abbreviation.upper()
    # Also accept DK aliases (e.g. "SA" → "SAS")
    dk_reverse = {v: k for k, v in DK_TO_NBA_ABBR_ALIASES.items()}
    dk_alias = dk_reverse.get(target_abbr, target_abbr)

    rotation = []
    seen_names = set()
    for p in draftables:
        p_abbr = p.team_abbreviation.upper()
        # Normalize DK abbreviation to NBA abbreviation for matching
        p_abbr_nba = DK_TO_NBA_ABBR_ALIASES.get(p_abbr, p_abbr)
        if p_abbr_nba != target_abbr and p_abbr != dk_alias:
            continue

        # DK lists each player multiple times for roster slot eligibility
        # (PG, PG/SG, UTIL, etc.) — each gets a unique dk_player_id.
        # Deduplicate by display name within the team.
        if p.display_name in seen_names:
            continue
        seen_names.add(p.display_name)

        # Skip players marked as Out
        if p.status and p.status.upper() == "O":
            continue

        # Estimate season_avg from DK salary tiers
        sal = p.salary or 3500
        if sal >= 10000:
            est_min, est_usg = 35.0, 0.28
        elif sal >= 8000:
            est_min, est_usg = 32.0, 0.24
        elif sal >= 6000:
            est_min, est_usg = 28.0, 0.20
        elif sal >= 5000:
            est_min, est_usg = 24.0, 0.17
        elif sal >= 4000:
            est_min, est_usg = 20.0, 0.15
        else:
            est_min, est_usg = 14.0, 0.12

        # Position-based per-minute stat rates (league averages)
        pos = (p.position or "SF").upper()
        if pos in ("PG", "SG", "G"):
            stat_rates = dict(
                pts_per_min=0.58, reb_per_min=0.12, ast_per_min=0.22,
                stl_per_min=0.04, blk_per_min=0.01, tov_per_min=0.08,
                fg3m_per_min=0.10,
            )
        elif pos in ("PF", "C", "F/C", "C/F"):
            stat_rates = dict(
                pts_per_min=0.52, reb_per_min=0.28, ast_per_min=0.10,
                stl_per_min=0.03, blk_per_min=0.05, tov_per_min=0.06,
                fg3m_per_min=0.05,
            )
        else:  # SF, F, UTIL, etc.
            stat_rates = dict(
                pts_per_min=0.55, reb_per_min=0.18, ast_per_min=0.14,
                stl_per_min=0.04, blk_per_min=0.02, tov_per_min=0.07,
                fg3m_per_min=0.08,
            )

        rotation.append(PlayerMinutes(
            player_id=p.dk_player_id,
            player_name=p.display_name,
            position=pos,
            team_id=team_id,
            minutes_last_5=[],
            minutes_last_10=[],
            season_avg=est_min,
            usage_rate=est_usg,
            **stat_rates,
        ))

    # Sort by estimated minutes descending (highest salary first)
    rotation.sort(key=lambda x: x.season_avg, reverse=True)
    logger.info(
        f"[DK Fallback] Built {len(rotation)} player rotation for "
        f"{team_abbreviation} (team {team_id}) from DG {draft_group_id}"
    )
    return rotation


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/teams", response_model=TeamsResponse)
async def get_teams(
    sport: str = Query("nba", description="Sport: nba or cbb"),
):
    """Get all teams (NBA or CBB)."""
    try:
        svc = get_services()
        data_svc = svc.get_data_service(sport)
        teams = data_svc.get_all_teams()
        return {"teams": teams, "sport": sport}
    except Exception as e:
        logger.error(f"Failed to fetch teams: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/teams/conferences", response_model=ConferencesResponse)
async def get_teams_by_conference(
    sport: str = Query("nba", description="Sport: nba or cbb"),
):
    """Get teams grouped by conference (CBB only).

    Returns conferences with their teams sorted alphabetically.
    For NBA, returns a single "NBA" group with all 30 teams.
    """
    try:
        svc = get_services()
        if sport == "cbb":
            from app.services.cbb_teams import CBBTeamRegistry
            registry = CBBTeamRegistry()
            by_conf = registry.get_teams_by_conference()
            # Sort conferences alphabetically, teams within each conference
            sorted_confs = {}
            for conf in sorted(by_conf.keys()):
                sorted_confs[conf] = sorted(
                    by_conf[conf], key=lambda t: t.get("full_name", "")
                )
            return {"conferences": sorted_confs, "sport": sport}
        else:
            # NBA: single group
            data_svc = svc.get_data_service(sport)
            teams = data_svc.get_all_teams()
            return {"conferences": {"NBA": teams}, "sport": sport}
    except Exception as e:
        logger.error(f"Failed to fetch conferences: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/teams/{team_id}/rotation", response_model=TeamRotation)
async def get_team_rotation(
    team_id: int,
    game_date: Optional[str] = Query(None, description="Game date YYYY-MM-DD"),
    apply_coach_adj: bool = Query(True, description="Apply coach adjustments"),
    draft_group_id: Optional[int] = Query(None, description="DK DraftGroup ID for salary data"),
    sport: str = Query("nba", description="Sport: nba or cbb"),
):
    """Get projected rotation for a team."""
    cache_key = _rotation_cache_key(team_id, game_date, draft_group_id, apply_coach_adj, sport)
    cached = _read_rotation_cache(cache_key)
    if cached is not None:
        return cached

    # Per-key lock collapses the Slate-page's parallel duplicate requests
    # (6 simultaneous identical calls) onto a single build path. The other
    # 5 wait briefly, then read the freshly-cached result.
    key_lock = _get_rotation_lock(cache_key)
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, key_lock.acquire)
    try:
        # Re-check cache after acquiring lock — another waiter may have
        # already populated it while we were queued.
        cached = _read_rotation_cache(cache_key)
        if cached is not None:
            return cached

        try:
            svc = get_services()
            data_svc = svc.get_data_service(sport)
            game_svc = svc.get_game_service(sport)
            injury_svc = svc.get_injury_service(sport)

            # Pre-warm CBB stats before sync game service calls
            if sport == "cbb" and hasattr(game_svc, "warm_stats_for_slate"):
                await game_svc.warm_stats_for_slate(game_date)

            team_info = None
            for t in data_svc.get_all_teams():
                if t["id"] == team_id:
                    team_info = t
                    break

            if not team_info:
                raise HTTPException(status_code=404, detail=f"Team {team_id} not found")

            team_name = team_info["full_name"]
            team_abbr = team_info.get("abbreviation", "")

            # Pre-fetch ALL DB cache data asynchronously (avoids event-loop
            # conflicts when sync code in the thread pool tries to call
            # _run_async/asyncio.run() which creates a new loop incompatible
            # with the asyncpg connection pool).
            # ALWAYS try DB cache — it's the fastest source and eliminates
            # the need for live BDL/NBA API calls entirely.
            _nba_cache = getattr(data_svc, '_db_cache', None)
            _prefetched_roster = None
            _prefetched_logs = None
            _nba_api_up = _nba_circuit_breaker.state == "CLOSED"
            if _nba_cache is not None and sport == "nba":
                try:
                    from app.utils.helpers import get_current_nba_season
                    _season = get_current_nba_season()
                    _prefetched_roster = await _nba_cache.get_team_roster(team_id, _season)
                    if _prefetched_roster:
                        _prefetched_logs = await _nba_cache.get_team_game_logs(team_id, _season)
                        # Guard: empty dict {} means roster cached but no
                        # game-log rows exist yet.  Null it out so the
                        # downstream multi-source service tries BDL instead
                        # of skipping it (empty dict is "not None" but falsy).
                        if not _prefetched_logs:
                            _prefetched_logs = None
                except Exception as e:
                    logger.warning(f"Async DB cache pre-fetch failed for team {team_id}: {e}")

                # Also pre-warm game_svc team stats cache so the thread pool
                # worker never calls _run_async for DB reads.
                try:
                    _today = date.today().isoformat()
                    if not game_svc._team_stats_cache or game_svc._team_stats_cache_date != _today:
                        db_stats = await _nba_cache.get_all_team_stats(_season)
                        if db_stats and len(db_stats) >= 20:
                            game_svc._team_stats_cache = db_stats
                            game_svc._team_stats_cache_date = _today
                except Exception:
                    pass  # Non-fatal — sync fallback will handle it

            # ── Pre-fetch CoachAgent rotation depth on the main event loop ──
            # Must happen BEFORE run_in_executor so the sync thread reads
            # from warm cache instead of spawning a new event loop for DB.
            if svc.coach_learning_agent and sport == "nba":
                try:
                    from app.db.database import get_session as _get_db_session
                    async with _get_db_session() as _coach_session:
                        await svc.coach_learning_agent.prefetch_rotation_depth(
                            team_id, _coach_session, sport,
                        )
                except Exception as _coach_exc:
                    logger.debug(f"[CoachAgent] Rotation depth prefetch skipped: {_coach_exc}")

            # ── Extract DK draftable data for this team (needed by
            # _resolve_position and build_synthetic_player inside the
            # rotation builder).  Done on the main thread since it only
            # reads from the in-memory DK cache.
            _dk_names, _dk_positions, _dk_salaries = _extract_dk_team_data(
                draft_group_id, team_abbr,
            ) if draft_group_id and sport == "nba" else (None, None, None)

            # ── Heavy sync work — run in thread pool to avoid blocking
            # the event loop while NBA API / rotation engine run. ──
            def _build_rotation_sync():
                """All sync-heavy work runs here, off the event loop."""
                import time as _t
                _t0 = _t.time()
                rotation = None
                is_dk_fallback = False

                # Always pass DB cache to multi-source service.  When DB
                # cache has prefetched data, BDL/NBA API are skipped entirely.
                # When DB cache is empty, BDL serves as primary live source.
                try:
                    rotation = data_svc.build_team_rotation(
                        team_id,
                        cache_service=_nba_cache,
                        prefetched_roster=_prefetched_roster,
                        prefetched_game_logs=_prefetched_logs,
                        draftable_names=_dk_names,
                        draftable_positions=_dk_positions,
                        draftable_salaries=_dk_salaries,
                    )
                except Exception as api_err:
                    logger.warning(
                        f"[Rotation] Data service failed for team {team_id}: {api_err}"
                    )

                # Fallback: build rotation from DK draftables when NBA API is down
                if not rotation and draft_group_id and sport == "nba":
                    rotation = _build_dk_fallback_rotation(
                        draft_group_id, team_id, team_abbr,
                    )
                    is_dk_fallback = bool(rotation)

                if not rotation:
                    return None

                # When using DK fallback, skip slow external calls:
                # - Injury waterfall (PDF + ESPN = ~40s cold cache)
                # - Game service (NBA API = ~30s timeout)
                # - Props blending (DK Sportsbook = ~10s fetch lock)
                injuries = []
                all_injuries = []
                if not is_dk_fallback:
                    try:
                        injuries = injury_svc.get_team_injuries(team_name)
                        all_injuries = injury_svc.get_all_injuries()
                    except Exception as inj_err:
                        logger.warning(f"[Rotation] Injury fetch failed for {team_name}: {inj_err}")

                gd = game_date or date.today().isoformat()

                game_info = None
                is_b2b = False
                if not is_dk_fallback and _nba_api_up:
                    try:
                        if game_svc.has_game_on_date(team_id, gd):
                            game_info = game_svc.get_team_game(team_id, gd)
                            is_b2b = _check_b2b(team_id, gd, sport)
                    except Exception:
                        pass  # Game context is optional

                result = svc.engine.project_team_rotation(
                    team_id=team_id,
                    team_name=team_name,
                    rotation=rotation,
                    injuries=injuries,
                    game_date=gd,
                    apply_coach_adjustments=apply_coach_adj,
                    game_info=game_info,
                    is_b2b=is_b2b,
                    all_injuries=all_injuries,
                )

                matchup_factors = None
                opp_id = None
                if game_info:
                    opp_id = (
                        game_info.away_team.team_id
                        if game_info.home_team.team_id == team_id
                        else game_info.home_team.team_id
                    )
                    matchup_factors = game_svc.get_dvp_matchup_factors(opp_id)

                svc.dfs_service.project_team_dfs(
                    result, rotation, matchup_factors=matchup_factors,
                    game_service=game_svc,
                    opponent_team_id=opp_id,
                    sport=sport,
                    skip_props=is_dk_fallback,
                )

                if draft_group_id:
                    _attach_dk_salaries(result, draft_group_id, team_abbr)

                logger.info(
                    f"[Rotation] {team_name} ({team_id}) completed in "
                    f"{_t.time()-_t0:.2f}s (dk_fallback={is_dk_fallback})"
                )

                # Populate the cache from the worker thread so that even if
                # the awaiting request timed out (504), a client retry hits
                # a warm cache instead of re-running the build.
                if result is not None:
                    try:
                        _write_rotation_cache(cache_key, result)
                    except Exception:
                        pass

                return result

            inner_loop = asyncio.get_running_loop()
            # Wall-clock timeout so a slow upstream (e.g. BallDontLie 429
            # back-off chain, ~30s+) can't pin a browser connection
            # indefinitely. The orphaned worker thread will keep running
            # in the executor and eventually populate the cache, so a
            # subsequent retry from the client gets served fast.
            try:
                result = await asyncio.wait_for(
                    inner_loop.run_in_executor(None, _build_rotation_sync),
                    timeout=25.0,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    f"[Rotation] team {team_id} build exceeded 25s — "
                    "returning 504 so client can retry; worker thread "
                    "continues in background and will populate the cache."
                )
                raise HTTPException(
                    status_code=504,
                    detail=(
                        "Rotation build is taking longer than usual "
                        "(upstream rate-limit). Retry in a moment — the "
                        "result will be cached when the build completes."
                    ),
                )

            if not result:
                raise HTTPException(
                    status_code=404, detail="No rotation data available"
                )

            # Cache the successful build for the next 5 minutes so the
            # Slate-page fan-out (and any quick reload) returns instantly
            # without re-hitting BDL.
            _write_rotation_cache(cache_key, result)
            return result
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Rotation projection failed for team {team_id}: {e}")
            raise HTTPException(status_code=500, detail=str(e))
    finally:
        key_lock.release()


@router.get("/teams/{team_id}/injuries")
async def get_team_injuries(
    team_id: int,
    sport: str = Query("nba", description="Sport: nba or cbb"),
):
    """Get current injuries for a team."""
    try:
        svc = get_services()
        data_svc = svc.get_data_service(sport)
        injury_svc = svc.get_injury_service(sport)

        team_info = None
        for t in data_svc.get_all_teams():
            if t["id"] == team_id:
                team_info = t
                break

        if not team_info:
            raise HTTPException(status_code=404, detail=f"Team {team_id} not found")

        injuries = injury_svc.get_team_injuries(team_info["full_name"])
        return {"team": team_info["full_name"], "injuries": injuries}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to fetch injuries for team {team_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/teams/{team_id}/game-today")
async def get_team_game_today(
    team_id: int,
    game_date: Optional[str] = Query(None, description="Date YYYY-MM-DD (default: today)"),
    sport: str = Query("nba", description="Sport: nba or cbb"),
):
    """Get game info for a team on a given date.

    Returns projected totals, pace, spread, and over/under context.
    """
    try:
        svc = get_services()
        game_svc = svc.get_game_service(sport)

        # Pre-warm CBB stats before sync game service calls
        if sport == "cbb" and hasattr(game_svc, "warm_stats_for_slate"):
            await game_svc.warm_stats_for_slate(game_date)

        game = game_svc.get_team_game(team_id, game_date)
        if not game:
            return {"playing_today": False, "game": None}
        return {"playing_today": True, "game": game}
    except Exception as e:
        logger.error(f"Failed to fetch game for team {team_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/coaches")
async def get_coaches():
    """Get all coach profiles."""
    return {"coaches": COACH_PROFILES}


@router.get("/coaches/{team_id}")
async def get_coach_for_team(team_id: int):
    """Get the coach profile for a specific team."""
    profile = get_coach_profile(team_id)
    return {"coach": profile}


@router.get("/coaches/{team_id}/learned")
async def get_learned_coach_adjustments(team_id: int):
    """View DB-stored learned adjustments for a team's coach profile."""
    svc = get_services()
    merged = svc.coach_profile_service.get_merged_profile(team_id)
    raw_adj = svc.coach_profile_service._adjustments.get(team_id, {})
    return {
        "team_id": team_id,
        "merged_profile": merged.model_dump(),
        "learned_adjustments": raw_adj,
    }


@router.get("/projection/{team_id}/player/{player_id}")
async def get_player_projection(
    team_id: int,
    player_id: int,
    sport: str = Query("nba", description="Sport: nba or cbb"),
):
    """Get minutes projection for a single player."""
    try:
        svc = get_services()
        data_svc = svc.get_data_service(sport)
        _nba_cache = getattr(data_svc, '_db_cache', None)
        rotation = data_svc.build_team_rotation(team_id, cache_service=_nba_cache)
        player = next((p for p in rotation if p.player_id == player_id), None)

        if not player:
            raise HTTPException(
                status_code=404, detail=f"Player {player_id} not found"
            )

        baseline = svc.engine.get_baseline_projection(player)
        return PlayerProjection(
            player_id=player.player_id,
            player_name=player.player_name,
            position=player.position,
            baseline_minutes=baseline,
            adjusted_minutes=baseline,
            confidence=1.0,
            reason="Baseline projection",
        ).model_dump()
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Player projection failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
