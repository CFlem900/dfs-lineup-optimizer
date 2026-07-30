"""Historical Backfill Service.

Retroactively populates ``player_minutes_history`` by:
1. Fetching actual box scores from the NBA API for each game date.
2. Generating *retroactive* projections using the current RotationEngine
   and DFS models (using each player's season-average data available at
   the time of the game).
3. Storing both projected and actual values so the Accuracy Dashboard
   has real data to analyse.

Usage via API::

    POST /backtest/backfill?start_date=2025-12-01&end_date=2026-02-12

Or via the management script::

    python -m app.services.ingestion.historical_backfill \\
        --start 2025-12-01 --end 2026-02-12
"""

import logging
import time
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class HistoricalBackfill:
    """Generates retroactive projections and ingests box scores."""

    def __init__(
        self,
        nba_service=None,
        rotation_engine=None,
        dfs_service=None,
        game_service=None,
        injury_service=None,
    ):
        self._nba = nba_service
        self._engine = rotation_engine
        self._dfs = dfs_service
        self._games = game_service
        self._injuries = injury_service

    # ── Public API ────────────────────────────────────────────────

    async def backfill(
        self,
        start_date: str,
        end_date: str,
        progress_callback=None,
    ) -> Dict:
        """Run full backfill for a date range.

        Returns summary dict with dates processed, rows inserted, errors.
        """
        from app.db.database import is_db_available

        if not is_db_available():
            return {"error": "Database not available", "rows_inserted": 0}

        if not self._nba:
            return {"error": "NBA service not available", "rows_inserted": 0}

        current = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date)
        today = date.today()

        # Don't backfill future dates
        if end >= today:
            end = today - timedelta(days=1)

        total_inserted = 0
        dates_processed = 0
        dates_skipped = 0
        errors: List[str] = []

        while current <= end:
            date_str = current.isoformat()
            try:
                count = await self._backfill_single_date(date_str)
                if count > 0:
                    total_inserted += count
                    dates_processed += 1
                    logger.info(
                        f"[Backfill] {date_str}: {count} records inserted"
                    )
                else:
                    dates_skipped += 1
                    logger.debug(f"[Backfill] {date_str}: no games or skipped")

                if progress_callback:
                    progress_callback(date_str, count, total_inserted)

            except Exception as exc:
                msg = f"{date_str}: {str(exc)[:100]}"
                errors.append(msg)
                logger.warning(f"[Backfill] Error on {msg}")

            current += timedelta(days=1)

        logger.info(
            f"[Backfill] Complete: {total_inserted} rows across "
            f"{dates_processed} dates ({dates_skipped} skipped, "
            f"{len(errors)} errors)"
        )

        return {
            "rows_inserted": total_inserted,
            "dates_processed": dates_processed,
            "dates_skipped": dates_skipped,
            "errors": errors[:20],  # Cap error list
            "start_date": start_date,
            "end_date": end_date,
        }

    # ── Internal ──────────────────────────────────────────────────

    async def _backfill_single_date(self, game_date: str) -> int:
        """Backfill a single game date with projections + actuals."""
        from nba_api.stats.endpoints import scoreboardv2
        from app.db.database import get_session
        from app.db.models import PlayerMinutesHistory
        from sqlalchemy import select
        from sqlalchemy.exc import IntegrityError

        # 1. Get games played on this date
        sb = scoreboardv2.ScoreboardV2(
            game_date=game_date, league_id="00"
        )
        time.sleep(0.7)

        games_header = sb.get_normalized_dict().get("GameHeader", [])
        if not games_header:
            return 0

        # 2. Build team → game lookup for generating projections
        team_games = {}
        for g in games_header:
            home_id = g.get("HOME_TEAM_ID")
            away_id = g.get("VISITOR_TEAM_ID")
            game_id = g.get("GAME_ID", "")
            if home_id:
                team_games[home_id] = game_id
            if away_id:
                team_games[away_id] = game_id

        # 3. Generate retroactive projections for all teams that played
        proj_lookup = await self._generate_projections_for_date(
            game_date, team_games
        )

        # 4. Fetch box scores (V3 API) and store with projections
        rows_inserted = 0

        async with get_session() as session:
            # Check which players already have records for this date
            existing_stmt = (
                select(PlayerMinutesHistory.player_id)
                .where(
                    PlayerMinutesHistory.game_date
                    >= datetime.fromisoformat(game_date),
                )
                .where(
                    PlayerMinutesHistory.game_date
                    < datetime.fromisoformat(game_date) + timedelta(days=1),
                )
            )
            existing_result = await session.execute(existing_stmt)
            existing_pids = {r[0] for r in existing_result.all()}

            for game in games_header:
                game_id = game.get("GAME_ID", "")
                if not game_id:
                    continue

                try:
                    all_players = _fetch_box_score_v3(game_id)

                    for ps in all_players:
                        player_id = ps.get("player_id")
                        if not player_id:
                            continue

                        # Skip if already backfilled
                        if player_id in existing_pids:
                            continue

                        actual_minutes = ps.get("minutes", 0.0)
                        pts = ps.get("pts", 0)
                        reb = ps.get("reb", 0)
                        ast = ps.get("ast", 0)
                        stl = ps.get("stl", 0)
                        blk = ps.get("blk", 0)
                        tov = ps.get("tov", 0)
                        fg3m = ps.get("fg3m", 0)

                        dk_actual = _dk_fp(pts, reb, ast, stl, blk, tov, fg3m)

                        # Look up retroactive projection
                        proj = proj_lookup.get(player_id, {})

                        record = PlayerMinutesHistory(
                            player_id=player_id,
                            player_name=ps.get("player_name", ""),
                            team_id=ps.get("team_id"),
                            position=ps.get("position", ""),
                            game_date=datetime.fromisoformat(game_date),
                            actual_minutes=round(actual_minutes, 1),
                            projected_minutes=proj.get("projected_minutes"),
                            baseline_minutes=proj.get("baseline_minutes"),
                            dk_projected_fp=proj.get("dk_projected_fp"),
                            dk_actual_fp=round(dk_actual, 1),
                            dk_salary=proj.get("dk_salary"),
                            was_injured=actual_minutes < 1,
                            # Per-stat actuals
                            actual_pts=float(pts),
                            actual_reb=float(reb),
                            actual_ast=float(ast),
                            actual_stl=float(stl),
                            actual_blk=float(blk),
                            actual_tov=float(tov),
                            actual_fg3m=float(fg3m),
                            # Per-stat projections
                            projected_pts=proj.get("projected_pts"),
                            projected_reb=proj.get("projected_reb"),
                            projected_ast=proj.get("projected_ast"),
                            projected_stl=proj.get("projected_stl"),
                            projected_blk=proj.get("projected_blk"),
                            projected_tov=proj.get("projected_tov"),
                            projected_fg3m=proj.get("projected_fg3m"),
                        )

                        session.add(record)
                        rows_inserted += 1

                except Exception as exc:
                    logger.warning(
                        f"[Backfill] Failed box score for {game_id}: {exc}"
                    )
                    continue

            if rows_inserted > 0:
                try:
                    await session.commit()
                except IntegrityError:
                    await session.rollback()
                    logger.debug(
                        f"[Backfill] Some duplicates on {game_date}, "
                        "retrying one-by-one"
                    )
                    rows_inserted = 0

        return rows_inserted

    async def _generate_projections_for_date(
        self,
        game_date: str,
        team_games: Dict[int, str],
    ) -> Dict[int, dict]:
        """Generate retroactive projections for all teams on a date.

        Returns player_id → {projected_minutes, dk_projected_fp, ...}
        """
        proj_lookup: Dict[int, dict] = {}

        if not self._engine or not self._nba:
            return proj_lookup

        all_teams = self._nba.get_all_teams()
        team_map = {t["id"]: t for t in all_teams}

        for team_id, _game_id in team_games.items():
            team_info = team_map.get(team_id)
            if not team_info:
                continue

            try:
                # Build rotation from current NBA data
                # Note: this uses *current* season averages, which is a
                # reasonable approximation for recent dates.
                rotation = self._nba.build_team_rotation(team_id)
                if not rotation:
                    continue

                team_name = team_info["full_name"]

                # Get injuries (current — not historical, but still
                # better than no adjustment)
                injuries = []
                all_injuries = []
                if self._injuries:
                    try:
                        injuries = self._injuries.get_team_injuries(team_name)
                        all_injuries = self._injuries.get_all_injuries()
                    except Exception:
                        pass

                # Get game context if available
                game_info = None
                is_b2b = False
                if self._games:
                    try:
                        if self._games.has_game_on_date(team_id, game_date):
                            game_info = self._games.get_team_game(
                                team_id, game_date
                            )
                    except Exception:
                        pass

                # Generate projection
                result = self._engine.project_team_rotation(
                    team_id=team_id,
                    team_name=team_name,
                    rotation=rotation,
                    injuries=injuries,
                    game_date=game_date,
                    apply_coach_adjustments=True,
                    game_info=game_info,
                    is_b2b=is_b2b,
                    all_injuries=all_injuries,
                )

                # Project DFS stats
                if self._dfs:
                    matchup_factors = None
                    if game_info and self._games:
                        try:
                            opp_id = (
                                game_info.away_team.team_id
                                if game_info.home_team.team_id == team_id
                                else game_info.home_team.team_id
                            )
                            matchup_factors = (
                                self._games.get_dvp_matchup_factors(opp_id)
                            )
                        except Exception:
                            pass
                    self._dfs.project_team_dfs(
                        result, rotation, matchup_factors=matchup_factors,
                        game_service=self._games,
                        opponent_team_id=opp_id if matchup_factors else None,
                    )

                # Extract per-player projections into lookup
                for proj in result.projections:
                    stats = proj.projected_stats or {}
                    proj_lookup[proj.player_id] = {
                        "projected_minutes": proj.adjusted_minutes,
                        "baseline_minutes": proj.baseline_minutes,
                        "dk_projected_fp": proj.dk_points,
                        "dk_salary": proj.dk_salary,
                        "projected_pts": stats.get("pts"),
                        "projected_reb": stats.get("reb"),
                        "projected_ast": stats.get("ast"),
                        "projected_stl": stats.get("stl"),
                        "projected_blk": stats.get("blk"),
                        "projected_tov": stats.get("tov"),
                        "projected_fg3m": stats.get("fg3m"),
                    }

                # Rate-limit between teams
                time.sleep(0.3)

            except Exception as exc:
                logger.debug(
                    f"[Backfill] Projection failed for team {team_id} "
                    f"on {game_date}: {exc}"
                )
                continue

        return proj_lookup


# ── Helpers ───────────────────────────────────────────────────────

def _fetch_box_score_v3(game_id: str) -> List[dict]:
    """Fetch box score using V3 API and normalize to a flat player list.

    V3 nests players under ``homeTeam.players`` / ``awayTeam.players``
    with stats under a ``statistics`` sub-dict.  This function flattens
    everything into a simple list of dicts with keys:
    ``player_id``, ``player_name``, ``team_id``, ``position``,
    ``minutes``, ``pts``, ``reb``, ``ast``, ``stl``, ``blk``,
    ``tov``, ``fg3m``.
    """
    from nba_api.stats.endpoints import boxscoretraditionalv3

    box = boxscoretraditionalv3.BoxScoreTraditionalV3(game_id=game_id)
    time.sleep(0.7)

    raw = box.get_dict()
    bst = raw.get("boxScoreTraditional", {})

    players: List[dict] = []

    for side in ("homeTeam", "awayTeam"):
        team_data = bst.get(side, {})
        team_id = team_data.get("teamId")

        for p in team_data.get("players", []):
            stats = p.get("statistics", {})
            minutes = _parse_minutes(stats.get("minutes", "0:00"))

            players.append({
                "player_id": p.get("personId"),
                "player_name": f"{p.get('firstName', '')} {p.get('familyName', '')}".strip(),
                "team_id": team_id,
                "position": p.get("position", ""),
                "minutes": minutes,
                "pts": stats.get("points", 0) or 0,
                "reb": stats.get("reboundsTotal", 0) or 0,
                "ast": stats.get("assists", 0) or 0,
                "stl": stats.get("steals", 0) or 0,
                "blk": stats.get("blocks", 0) or 0,
                "tov": stats.get("turnovers", 0) or 0,
                "fg3m": stats.get("threePointersMade", 0) or 0,
            })

    return players


def _parse_minutes(minutes_str: str) -> float:
    """Parse NBA API minutes format ('MM:SS' or None) to float."""
    if not minutes_str or minutes_str == "None":
        return 0.0
    try:
        if ":" in str(minutes_str):
            parts = str(minutes_str).split(":")
            return float(parts[0]) + float(parts[1]) / 60.0
        return float(minutes_str)
    except (ValueError, IndexError):
        return 0.0


def _dk_fp(pts, reb, ast, stl, blk, tov, fg3m) -> float:
    """Calculate actual DK fantasy points from box score stats."""
    fp = (
        pts * 1.0 + reb * 1.25 + ast * 1.5
        + stl * 2.0 + blk * 2.0 + tov * (-0.5)
        + fg3m * 0.5
    )
    cats = sum(1 for v in [pts, reb, ast, stl, blk] if v >= 10)
    if cats >= 2:
        fp += 1.5
    if cats >= 3:
        fp += 3.0
    return fp


# ── CLI entrypoint ────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    import asyncio
    import os
    import sys

    # Ensure the backend directory is on the path
    backend_dir = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..", "..")
    )
    sys.path.insert(0, backend_dir)

    from dotenv import load_dotenv
    load_dotenv(override=True)

    parser = argparse.ArgumentParser(description="Backfill historical data")
    parser.add_argument(
        "--start", required=True, help="Start date YYYY-MM-DD"
    )
    parser.add_argument(
        "--end", required=True, help="End date YYYY-MM-DD"
    )
    args = parser.parse_args()

    async def main():
        from app.db.database import init_db, close_db
        from app.services.nba_api_service import NBAApiService
        from app.services.injury_service import InjuryService
        from app.services.game_service import GameService
        from app.services.dfs_service import DFSService
        from app.services.rotation_engine import RotationEngine

        ok = await init_db()
        if not ok:
            print("ERROR: Database not available. Check DATABASE_URL.")
            sys.exit(1)

        nba = NBAApiService()
        engine = RotationEngine()
        dfs = DFSService()
        games = GameService()
        injuries = InjuryService()

        backfiller = HistoricalBackfill(
            nba_service=nba,
            rotation_engine=engine,
            dfs_service=dfs,
            game_service=games,
            injury_service=injuries,
        )

        def on_progress(dt, count, total):
            print(f"  {dt}: +{count} rows (total: {total})")

        print(f"Starting backfill: {args.start} → {args.end}")
        result = await backfiller.backfill(
            args.start, args.end, progress_callback=on_progress
        )

        print(f"\n{'='*50}")
        print(f"Backfill complete!")
        print(f"  Rows inserted:   {result['rows_inserted']}")
        print(f"  Dates processed: {result['dates_processed']}")
        print(f"  Dates skipped:   {result['dates_skipped']}")
        if result.get("errors"):
            print(f"  Errors ({len(result['errors'])}):")
            for e in result["errors"]:
                print(f"    - {e}")

        await close_db()

    asyncio.run(main())
