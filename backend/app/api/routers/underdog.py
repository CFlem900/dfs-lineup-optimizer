"""Underdog Fantasy API endpoints.

Provides pick'em line data and edge analysis for Underdog Fantasy contests.
"""

import logging
from typing import Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.api.dependencies import get_services
from app.models.underdog import (
    PickemComparison,
    PickemEntry,
    UnderdogPickemResponse,
    UnderdogSlate,
)

logger = logging.getLogger(__name__)
router = APIRouter()


# ── Request models ─────────────────────────────────────────────────────


class BuildEntryRequest(BaseModel):
    """Request body for building an optimal pick'em entry."""

    num_picks: int = Field(default=5, ge=2, le=6)
    strategy: str = Field(
        default="max_edge",
        description="Strategy: max_edge, diversified, or correlated",
    )
    entry_type: str = Field(
        default="flex",
        description="Entry type: flex or power_play",
    )
    game_date: Optional[str] = None
    sport: str = "nba"


# ── Helper: build projections dict from cached player pool ────────────


def _build_projections_dict(
    sport: str = "nba",
) -> Dict[str, Dict[str, float]]:
    """Build normalized-name:TEAM -> stat projections from cached pool.

    Draws from the lineup optimizer's cached player pool, which contains
    per-player DFS projections with individual stat breakdowns.
    """
    svc = get_services()

    # Try to get cached pool first
    pool = svc.lineup_optimizer_service.get_cached_pool(sport=sport)
    if not pool:
        return {}

    projections: Dict[str, Dict[str, float]] = {}
    for entry in pool:
        # Normalize the player name for matching
        from app.services.underdog_api_service import _normalize_name

        norm_name = _normalize_name(entry.player_name)
        team = (entry.team_abbreviation or "").upper()
        key = f"{norm_name}:{team}"

        # Extract projected stats from the pool entry
        stats: Dict[str, float] = {}

        # If projected_stats dict is available (from DFS pipeline)
        if hasattr(entry, "projected_stats") and entry.projected_stats:
            ps = entry.projected_stats
            stats["pts"] = ps.get("pts", 0) or ps.get("projected_pts", 0)
            stats["reb"] = ps.get("reb", 0) or ps.get("projected_reb", 0)
            stats["ast"] = ps.get("ast", 0) or ps.get("projected_ast", 0)
            stats["stl"] = ps.get("stl", 0) or ps.get("projected_stl", 0)
            stats["blk"] = ps.get("blk", 0) or ps.get("projected_blk", 0)
            stats["tov"] = ps.get("tov", 0) or ps.get("projected_tov", 0)
            stats["fg3m"] = ps.get("fg3m", 0) or ps.get("projected_fg3m", 0)

        # FD points = Underdog Fantasy Points (identical scoring)
        if hasattr(entry, "fd_points") and entry.fd_points:
            stats["fd_points"] = entry.fd_points
        elif hasattr(entry, "projected_fp") and entry.projected_fp:
            # Fallback: use projected_fp (may be DK scoring)
            stats["fd_points"] = entry.projected_fp

        # Composite: PRA
        if stats.get("pts") and stats.get("reb") and stats.get("ast"):
            stats["pra"] = stats["pts"] + stats["reb"] + stats["ast"]

        # Composite: steals + blocks
        if stats.get("stl") or stats.get("blk"):
            stats["stl_blk"] = (stats.get("stl", 0)) + (stats.get("blk", 0))

        # Fantasy points alias
        if stats.get("fd_points"):
            stats["fantasy_points"] = stats["fd_points"]

        if stats:
            projections[key] = stats

    return projections


# ── Debug: Raw API response ────────────────────────────────────────────


@router.get("/underdog/pickem/debug")
def debug_pickem_raw(
    game_date: Optional[str] = Query(None, description="Date (YYYY-MM-DD)"),
) -> dict:
    """Return raw Underdog API response structure for debugging.

    Use this to inspect the actual response structure when the parser
    produces unexpected results (???, UUIDs, missing data).
    """
    import httpx

    from app.services.underdog_api_service import (
        BASE_URL,
        REQUEST_TIMEOUT,
        USER_AGENT,
    )

    try:
        resp = httpx.get(
            BASE_URL,
            timeout=REQUEST_TIMEOUT,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            follow_redirects=True,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        return {"error": str(e)}

    # Return structure summary + samples (not the full payload)
    summary: dict = {"top_level_keys": list(data.keys())}
    for key in data:
        val = data[key]
        if isinstance(val, list):
            summary[f"{key}_count"] = len(val)
            if val:
                summary[f"{key}_sample_keys"] = list(val[0].keys())
                summary[f"{key}_sample"] = val[0]
        elif isinstance(val, dict):
            summary[f"{key}_keys"] = list(val.keys())
            summary[f"{key}_sample"] = {
                k: str(v)[:80] for k, v in list(val.items())[:10]
            }

    return summary


# ── Pick'em Lines ──────────────────────────────────────────────────────


@router.get("/underdog/pickem/lines")
def get_pickem_lines(
    game_date: Optional[str] = Query(None, description="Date (YYYY-MM-DD)"),
    sport: str = Query("nba", description="Sport: nba or cbb"),
) -> dict:
    """Get raw Underdog pick'em lines for a date."""
    svc = get_services()
    slate = svc.underdog_api_service.get_pickem_lines(game_date)
    return slate.model_dump()


# ── Pick'em Edges ──────────────────────────────────────────────────────


@router.get("/underdog/pickem/edges")
def get_pickem_edges(
    game_date: Optional[str] = Query(None, description="Date (YYYY-MM-DD)"),
    sport: str = Query("nba", description="Sport: nba or cbb"),
    stat: Optional[str] = Query(None, description="Filter by stat (pts, reb, ast, etc.)"),
    min_edge: Optional[float] = Query(None, description="Minimum |delta_pct| to include"),
) -> dict:
    """Compare our projections to UD lines and return edge analysis.

    Requires a cached player pool (build one first via the Lineup tab).
    """
    svc = get_services()

    # Build projections from cached pool
    projections = _build_projections_dict(sport=sport)
    if not projections:
        return UnderdogPickemResponse(
            game_date=game_date or "",
            lines=[],
            total_lines=0,
        ).model_dump()

    # Get edges
    response = svc.underdog_pickem_service.get_all_edges(
        game_date=game_date or "",
        projections=projections,
    )

    # Apply optional filters
    if stat:
        response.lines = [l for l in response.lines if l.stat == stat]
        response.total_lines = len(response.lines)

    if min_edge is not None:
        response.lines = [l for l in response.lines if abs(l.delta_pct) >= min_edge]
        response.total_lines = len(response.lines)

    return response.model_dump()


# ── Single Player Edge ─────────────────────────────────────────────────


@router.get("/underdog/pickem/player/{player_name}")
def get_player_edges(
    player_name: str,
    game_date: Optional[str] = Query(None),
    sport: str = Query("nba"),
) -> dict:
    """Get all pick'em edges for a single player."""
    svc = get_services()
    lines = svc.underdog_api_service.get_player_lines(player_name, game_date)

    if not lines:
        return {"player_name": player_name, "lines": []}

    # Try to get our projection for this player
    projections = _build_projections_dict(sport=sport)
    edges = []
    for line in lines:
        comparison = svc.underdog_pickem_service._compare_single(line, projections)
        if comparison:
            edges.append(comparison.model_dump())

    return {
        "player_name": player_name,
        "lines": edges,
        "total_lines": len(edges),
    }


# ── Build Entry ────────────────────────────────────────────────────────


@router.post("/underdog/pickem/build-entry")
def build_pickem_entry(request: BuildEntryRequest) -> dict:
    """Build an optimal pick'em entry from available edges."""
    svc = get_services()

    # Get all edges
    projections = _build_projections_dict(sport=request.sport)
    if not projections:
        raise HTTPException(
            status_code=400,
            detail="No cached player pool. Build a player pool first via the Lineup tab.",
        )

    response = svc.underdog_pickem_service.get_all_edges(
        game_date=request.game_date or "",
        projections=projections,
    )

    entry = svc.underdog_pickem_service.build_optimal_entry(
        edges=response.lines,
        num_picks=request.num_picks,
        strategy=request.strategy,
        entry_type=request.entry_type,
    )

    return entry.model_dump()


# ── Correlations ───────────────────────────────────────────────────────


@router.get("/underdog/pickem/correlations")
def get_pickem_correlations(
    game_date: Optional[str] = Query(None),
    sport: str = Query("nba"),
) -> dict:
    """Get correlation matrix for pick'em selections.

    Shows which player/stat combinations tend to co-move, useful for
    building correlated or diversified entries.
    """
    svc = get_services()

    projections = _build_projections_dict(sport=sport)
    response = svc.underdog_pickem_service.get_all_edges(
        game_date=game_date or "",
        projections=projections,
    )

    # Build simple team-based correlation info
    team_groups: Dict[str, List[dict]] = {}
    for edge in response.lines:
        team = edge.team_abbreviation
        team_groups.setdefault(team, []).append({
            "player_name": edge.player_name,
            "stat": edge.stat,
            "recommendation": edge.recommendation,
            "delta_pct": edge.delta_pct,
        })

    return {
        "game_date": game_date or "",
        "team_correlations": team_groups,
        "total_teams": len(team_groups),
        "tip": "Players on the same team are positively correlated. "
        "For flex entries, diversify across teams. "
        "For power plays, consider stacking same-team picks.",
    }
