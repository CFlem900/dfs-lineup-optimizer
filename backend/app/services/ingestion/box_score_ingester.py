"""Box Score Ingester — Post-game data ingestion.

Fetches actual box scores from the NBA API and stores them alongside
pre-game projections in the player_minutes_history table for
backtesting analysis.

Designed to run as a background task (e.g., cron at 2am ET or
triggered via POST /backtest/ingest).
"""

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


class BoxScoreIngester:
    """Post-game data ingestion for backtesting."""

    def __init__(self, nba_service=None, game_service=None,
                 on_complete: Optional[Callable] = None):
        self._nba = nba_service
        self._games = game_service
        self._on_complete = on_complete

    async def ingest_game_date(self, game_date: str, sport: str = "nba") -> int:
        """Ingest all box scores for a given date.

        Fetches actual box score data from the NBA API, matches
        against our pre-game projections, and stores comparison
        records in the database.

        Returns the number of rows inserted.
        """
        if sport == "cbb":
            logger.info("[Ingest] CBB box score ingestion not yet implemented")
            return 0

        from app.db.database import is_db_available, get_session

        if not is_db_available():
            logger.warning("[Ingest] Database not available — skipping")
            return 0

        if not self._nba:
            logger.warning("[Ingest] NBA service not configured — skipping")
            return 0

        rows_inserted = 0

        try:
            # Get games that were played on this date
            from nba_api.stats.endpoints import boxscoretraditionalv3
            from nba_api.stats.endpoints import scoreboardv2
            import time

            # Get the scoreboard to find game IDs
            sb = scoreboardv2.ScoreboardV2(
                game_date=game_date,
                league_id="00",
            )
            time.sleep(0.7)  # Rate limiting

            games_header = sb.get_normalized_dict().get("GameHeader", [])
            if not games_header:
                logger.info(f"[Ingest] No games found for {game_date}")
                return 0

            logger.info(f"[Ingest] Found {len(games_header)} games on {game_date}")

            from app.db.models import PlayerMinutesHistory, ProjectionLog
            from sqlalchemy import select

            async with get_session() as session:
                # Load pre-game projection logs for this date to
                # store projected per-stat values alongside actuals.
                proj_lookup: Dict[int, dict] = {}  # player_id -> projected_stats
                try:
                    proj_stmt = (
                        select(ProjectionLog)
                        .where(
                            ProjectionLog.game_date
                            >= datetime.fromisoformat(game_date),
                        )
                        .where(
                            ProjectionLog.game_date
                            < datetime.fromisoformat(game_date)
                            + timedelta(days=1),
                        )
                    )
                    proj_result = await session.execute(proj_stmt)
                    for log in proj_result.scalars().all():
                        data = log.projection_data or {}
                        for p in data.get("projections", []):
                            pid = p.get("player_id")
                            stats = p.get("projected_stats")
                            if pid and stats:
                                proj_lookup[pid] = {
                                    "projected_minutes": p.get(
                                        "adjusted_minutes", 0
                                    ),
                                    "dk_projected_fp": p.get("dk_points", 0),
                                    **{
                                        f"projected_{k}": v
                                        for k, v in stats.items()
                                    },
                                }
                    if proj_lookup:
                        logger.info(
                            f"[Ingest] Loaded projections for "
                            f"{len(proj_lookup)} players from ProjectionLog"
                        )
                except Exception as pe:
                    logger.debug(
                        f"[Ingest] Could not load projection logs: {pe}"
                    )

                for game in games_header:
                    game_id = game.get("GAME_ID", "")
                    if not game_id:
                        continue

                    try:
                        # Use V3 API (V2 deprecated for 2025-26 season)
                        all_players = self._fetch_box_score_v3(
                            boxscoretraditionalv3, game_id
                        )
                        time.sleep(0.7)

                        for ps in all_players:
                            player_id = ps.get("player_id")
                            if not player_id:
                                continue

                            actual_minutes = ps.get("minutes", 0.0)

                            pts = ps.get("pts", 0) or 0
                            reb = ps.get("reb", 0) or 0
                            ast = ps.get("ast", 0) or 0
                            stl = ps.get("stl", 0) or 0
                            blk = ps.get("blk", 0) or 0
                            tov = ps.get("tov", 0) or 0
                            fg3m = ps.get("fg3m", 0) or 0
                            fga = ps.get("fga", 0) or 0
                            fgm = ps.get("fgm", 0) or 0
                            fg3a = ps.get("fg3a", 0) or 0
                            fta = ps.get("fta", 0) or 0
                            ftm = ps.get("ftm", 0) or 0

                            # Shooting per-minute rates (≥3 min)
                            if actual_minutes >= 3.0:
                                _fg3a_rate = round(
                                    fg3a / actual_minutes, 4
                                )
                                _fga_rate = round(
                                    fga / actual_minutes, 4
                                )
                                _fta_rate = round(
                                    fta / actual_minutes, 4
                                )
                                _fg3_pct = (
                                    round(fg3m / fg3a, 3)
                                    if fg3a > 0
                                    else None
                                )
                                _fg2_pct = (
                                    round(
                                        (fgm - fg3m) / (fga - fg3a),
                                        3,
                                    )
                                    if (fga - fg3a) > 0
                                    else None
                                )
                                _ft_pct = (
                                    round(ftm / fta, 3)
                                    if fta > 0
                                    else None
                                )
                            else:
                                _fg3a_rate = None
                                _fga_rate = None
                                _fta_rate = None
                                _fg3_pct = None
                                _fg2_pct = None
                                _ft_pct = None

                            dk_fp = (
                                pts * 1.0 + reb * 1.25 + ast * 1.5
                                + stl * 2.0 + blk * 2.0 + tov * (-0.5)
                                + fg3m * 0.5
                            )
                            cats = sum(
                                1 for v in [pts, reb, ast, stl, blk]
                                if v >= 10
                            )
                            if cats >= 2:
                                dk_fp += 1.5
                            if cats >= 3:
                                dk_fp += 3.0

                            # Look up pre-game projections for this player
                            proj_data = proj_lookup.get(player_id, {})

                            record = PlayerMinutesHistory(
                                player_id=player_id,
                                player_name=ps.get("player_name", ""),
                                team_id=ps.get("team_id"),
                                position=ps.get("position", ""),
                                game_date=datetime.fromisoformat(
                                    game_date
                                ),
                                actual_minutes=round(actual_minutes, 1),
                                projected_minutes=proj_data.get(
                                    "projected_minutes"
                                ),
                                dk_projected_fp=proj_data.get(
                                    "dk_projected_fp"
                                ),
                                dk_actual_fp=round(dk_fp, 1),
                                was_injured=actual_minutes < 1,
                                # Per-stat actuals
                                actual_pts=float(pts),
                                actual_reb=float(reb),
                                actual_ast=float(ast),
                                actual_stl=float(stl),
                                actual_blk=float(blk),
                                actual_tov=float(tov),
                                actual_fg3m=float(fg3m),
                                # Per-stat projections (from ProjectionLog)
                                projected_pts=proj_data.get("projected_pts"),
                                projected_reb=proj_data.get("projected_reb"),
                                projected_ast=proj_data.get("projected_ast"),
                                projected_stl=proj_data.get("projected_stl"),
                                projected_blk=proj_data.get("projected_blk"),
                                projected_tov=proj_data.get("projected_tov"),
                                projected_fg3m=proj_data.get(
                                    "projected_fg3m"
                                ),
                                # Shooting profile actuals
                                actual_fg3a_rate=_fg3a_rate,
                                actual_fga_rate=_fga_rate,
                                actual_fta_rate=_fta_rate,
                                actual_fg3_pct=_fg3_pct,
                                actual_fg2_pct=_fg2_pct,
                                actual_ft_pct=_ft_pct,
                                # Opponent + projected rates
                                opponent_team_id=ps.get(
                                    "opponent_team_id"
                                ),
                                projected_fg3a_rate=proj_data.get(
                                    "projected_fg3a_rate"
                                ),
                                projected_fga_rate=proj_data.get(
                                    "projected_fga_rate"
                                ),
                                projected_fta_rate=proj_data.get(
                                    "projected_fta_rate"
                                ),
                            )

                            session.add(record)
                            rows_inserted += 1

                    except Exception as exc:
                        logger.warning(
                            f"[Ingest] Failed to process game {game_id}: {exc}"
                        )
                        continue

                await session.commit()

        except Exception as exc:
            logger.error(f"[Ingest] Ingestion failed for {game_date}: {exc}")
            return 0

        logger.info(
            f"[Ingest] Inserted {rows_inserted} records for {game_date}"
        )

        # Auto-trigger post-ingestion analysis callback
        if rows_inserted > 0 and self._on_complete:
            try:
                import asyncio
                if asyncio.iscoroutinefunction(self._on_complete):
                    await self._on_complete(game_date, rows_inserted)
                else:
                    self._on_complete(game_date, rows_inserted)
            except Exception as cb_exc:
                logger.warning(
                    f"[Ingest] Post-ingestion callback failed: {cb_exc}"
                )

        return rows_inserted

    async def backfill(self, start_date: str, end_date: str) -> int:
        """Bulk backfill historical data for a date range.

        Returns total rows inserted across all dates.
        """
        total = 0
        current = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date)

        while current <= end:
            count = await self.ingest_game_date(current.isoformat())
            total += count
            current += timedelta(days=1)

        logger.info(
            f"[Ingest] Backfill complete: {total} records "
            f"from {start_date} to {end_date}"
        )
        return total

    @staticmethod
    def _fetch_box_score_v3(v3_module, game_id: str) -> list:
        """Fetch box score via V3 API and flatten to a list of player dicts."""
        import time as _t
        box = v3_module.BoxScoreTraditionalV3(game_id=game_id)
        _t.sleep(0.7)
        raw = box.get_dict()
        bst = raw.get("boxScoreTraditional", {})
        players = []
        home_team_id = bst.get("homeTeam", {}).get("teamId")
        away_team_id = bst.get("awayTeam", {}).get("teamId")
        for side in ("homeTeam", "awayTeam"):
            team_data = bst.get(side, {})
            team_id = team_data.get("teamId")
            opp_id = away_team_id if side == "homeTeam" else home_team_id
            for p in team_data.get("players", []):
                stats = p.get("statistics", {})
                minutes_str = stats.get("minutes", "0:00")
                minutes = BoxScoreIngester._parse_minutes(minutes_str)
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
                    # Shooting profile fields (for advanced calibration)
                    "fga": stats.get("fieldGoalsAttempted", 0) or 0,
                    "fgm": stats.get("fieldGoalsMade", 0) or 0,
                    "fg3a": stats.get("threePointersAttempted", 0) or 0,
                    "fta": stats.get("freeThrowsAttempted", 0) or 0,
                    "ftm": stats.get("freeThrowsMade", 0) or 0,
                    "opponent_team_id": opp_id,
                })
        return players

    @staticmethod
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
