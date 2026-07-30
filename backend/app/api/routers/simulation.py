"""Game simulation endpoints."""

import logging
from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request

from app.api.rate_limiter import limiter
from app.api.dependencies import get_services
from app.models.simulation import GameSimResult, SimulationConfig
from app.services.simulation_engine import SimulationEngine

logger = logging.getLogger(__name__)
router = APIRouter()


def _check_b2b(team_id: int, game_date: str, sport: str = "nba") -> bool:
    """Check if a team is on the second night of a back-to-back."""
    if sport == "cbb":
        return False  # No back-to-backs in college basketball
    try:
        svc = get_services()
        game_svc = svc.get_game_service(sport)
        gd = date.fromisoformat(game_date)
        yesterday = (gd - timedelta(days=1)).isoformat()
        return game_svc.has_game_on_date(team_id, yesterday)
    except Exception as e:
        logger.warning(f"B2B check failed for team {team_id}: {e}")
        return False


@router.get("/games/{game_id}/simulate", response_model=GameSimResult)
@limiter.limit("10/minute")
async def simulate_game(
    request: Request,
    game_id: str,
    game_date: Optional[str] = Query(None, description="Game date YYYY-MM-DD (default: today)"),
    num_simulations: int = Query(10_000, ge=100, le=100_000, description="Monte Carlo iterations"),
    minutes_variance: float = Query(0.20, ge=0.0, le=0.50, description="Player minutes noise (sigma fraction)"),
    pace_variance: float = Query(0.04, ge=0.0, le=0.15, description="Game pace noise (sigma fraction)"),
    scoring_variance: float = Query(0.15, ge=0.0, le=0.40, description="Stat scoring noise (sigma fraction)"),
    over_under_line: Optional[float] = Query(None, description="Vegas total for O/U analysis"),
    sport: str = Query("nba", description="Sport: nba or cbb"),
):
    """Run Monte Carlo simulation for a game.

    Produces probability distributions for team scores, win probability,
    over/under odds, and individual player stat lines / DFS projections.
    """
    try:
        svc = get_services()

        # Sport gate (Prompt 7.11): the Monte Carlo engine is built
        # around minutes-distribution + per-player scoring rates,
        # which is structurally an NBA/CBB abstraction. MLB needs a
        # hitter-vs-pitcher matchup model and NFL needs play-by-play
        # distributions — different problems entirely. Reject early
        # with a clear message instead of falling through to the
        # generic "rotation data unavailable" error that misleads
        # users into thinking it's a transient data issue.
        if sport not in ("nba", "cbb"):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Game simulation isn't available for {sport.upper()} yet. "
                    f"The Monte Carlo engine currently models basketball "
                    f"only (minutes / pace / scoring rates). "
                    f"For {sport.upper()}, use the Lineup tab and Import "
                    f"Proj to project per-player FP from a projections CSV."
                ),
            )

        data_svc = svc.get_data_service(sport)
        game_svc = svc.get_game_service(sport)
        injury_svc = svc.get_injury_service(sport)

        # Pre-warm CBB stats before sync game service calls
        if sport == "cbb" and hasattr(game_svc, "warm_stats_for_slate"):
            await game_svc.warm_stats_for_slate(game_date)

        # 1. Find the game from the schedule for the target date
        schedule = game_svc.get_games(game_date)
        game_info = next(
            (g for g in schedule.games if g.game_id == game_id), None
        )
        if not game_info:
            raise HTTPException(
                status_code=404,
                detail=f"Game {game_id} not found in schedule",
            )

        # 2. Build rotations for both teams
        home_id = game_info.home_team.team_id
        away_id = game_info.away_team.team_id

        _nba_cache = getattr(data_svc, '_db_cache', None)
        home_rotation_raw = data_svc.build_team_rotation(home_id, cache_service=_nba_cache)
        away_rotation_raw = data_svc.build_team_rotation(away_id, cache_service=_nba_cache)

        if not home_rotation_raw or not away_rotation_raw:
            raise HTTPException(
                status_code=404,
                detail="Rotation data unavailable for one or both teams",
            )

        # 3. Get injuries and project rotations
        home_injuries = injury_svc.get_team_injuries(
            game_info.home_team.team_name
        )
        away_injuries = injury_svc.get_team_injuries(
            game_info.away_team.team_name
        )
        all_injuries = injury_svc.get_all_injuries()

        gd = game_date or date.today().isoformat()

        home_b2b = _check_b2b(home_id, gd, sport)
        away_b2b = _check_b2b(away_id, gd, sport)

        # Pre-fetch CoachAgent rotation depth on the main event loop
        if svc.coach_learning_agent and sport == "nba":
            try:
                from app.db.database import get_session as _get_db_session
                async with _get_db_session() as _coach_session:
                    await svc.coach_learning_agent.prefetch_rotation_depth(
                        home_id, _coach_session, sport,
                    )
                    await svc.coach_learning_agent.prefetch_rotation_depth(
                        away_id, _coach_session, sport,
                    )
            except Exception as _coach_exc:
                logger.debug(f"[CoachAgent] Rotation depth prefetch skipped: {_coach_exc}")

        home_projected = svc.engine.project_team_rotation(
            team_id=home_id,
            team_name=game_info.home_team.team_name,
            rotation=home_rotation_raw,
            injuries=home_injuries,
            game_date=gd,
            game_info=game_info,
            is_b2b=home_b2b,
            all_injuries=all_injuries,
            sport=sport,
        )
        away_projected = svc.engine.project_team_rotation(
            team_id=away_id,
            team_name=game_info.away_team.team_name,
            rotation=away_rotation_raw,
            injuries=away_injuries,
            game_date=gd,
            game_info=game_info,
            is_b2b=away_b2b,
            all_injuries=all_injuries,
            sport=sport,
        )

        # 4. Attach DFS projections with DvP matchup factors
        home_dvp = game_svc.get_dvp_matchup_factors(away_id)
        away_dvp = game_svc.get_dvp_matchup_factors(home_id)
        svc.dfs_service.project_team_dfs(
            home_projected, home_rotation_raw, matchup_factors=home_dvp,
            game_service=game_svc, opponent_team_id=away_id,
            sport=sport,
        )
        svc.dfs_service.project_team_dfs(
            away_projected, away_rotation_raw, matchup_factors=away_dvp,
            game_service=game_svc, opponent_team_id=home_id,
            sport=sport,
        )

        # 5. Run simulation
        config = SimulationConfig(
            num_simulations=num_simulations,
            minutes_variance=minutes_variance,
            pace_variance=pace_variance,
            scoring_variance=scoring_variance,
        )
        sim_engine = SimulationEngine(config)

        result = sim_engine.simulate_game(
            game_info=game_info,
            home_rotation=home_projected,
            home_players=home_rotation_raw,
            away_rotation=away_projected,
            away_players=away_rotation_raw,
            over_under_line=over_under_line,
            home_matchup_factors=home_dvp,
            away_matchup_factors=away_dvp,
            sport=sport,
        )

        return result

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Simulation failed for game {game_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))
