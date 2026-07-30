"""Correlation Groups Service.

Computes Pearson correlations between players on the same team based on
historical DK fantasy-point outcomes.  This enables smarter game stacking
— if two players are highly correlated, pairing them in a lineup captures
shared positive game scripts.

Also supports cross-team (negative) correlations for bring-back logic.
"""

import logging
import time as _time
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)


class CorrelationService:
    """Computes player-pair correlations from historical FP data."""

    _CACHE_TTL = 1800  # 30 minutes

    def __init__(self):
        self._cache: Dict[str, Any] = {}
        self._cache_ts: Dict[str, float] = {}

    # ── Cache helpers ─────────────────────────────────────────────

    def _cache_get(self, key: str) -> Optional[Any]:
        if key in self._cache and (_time.time() - self._cache_ts.get(key, 0)) < self._CACHE_TTL:
            return self._cache[key]
        return None

    def _cache_set(self, key: str, value: Any) -> None:
        self._cache[key] = value
        self._cache_ts[key] = _time.time()

    # ── Core correlation computation ──────────────────────────────

    @staticmethod
    def _pearson(x: List[float], y: List[float]) -> Optional[float]:
        """Compute Pearson correlation between two equal-length float lists.

        Returns None if fewer than 5 shared observations or if either
        series has zero variance.
        """
        if len(x) != len(y) or len(x) < 5:
            return None

        ax = np.array(x, dtype=np.float64)
        ay = np.array(y, dtype=np.float64)

        # Filter out NaN pairs
        mask = ~(np.isnan(ax) | np.isnan(ay))
        ax, ay = ax[mask], ay[mask]

        if len(ax) < 5:
            return None

        std_x, std_y = ax.std(), ay.std()
        if std_x == 0 or std_y == 0:
            return None

        corr = np.corrcoef(ax, ay)[0, 1]
        return round(float(corr), 4)

    @staticmethod
    def _build_game_fp_matrix(
        rows: List,
    ) -> Tuple[Dict[int, str], Dict[str, Dict[int, float]]]:
        """Transform DB rows into a player → {game_date → fp} mapping.

        Parameters
        ----------
        rows : list
            Objects with ``player_id``, ``player_name``, ``game_date``,
            ``dk_actual_fp``.

        Returns
        -------
        player_names : dict
            player_id → player_name
        matrix : dict
            game_date_str → {player_id → dk_actual_fp}
        """
        player_names: Dict[int, str] = {}
        matrix: Dict[str, Dict[int, float]] = {}

        for row in rows:
            pid = getattr(row, "player_id", None)
            name = getattr(row, "player_name", "Unknown")
            gdate = getattr(row, "game_date", None)
            fp = getattr(row, "dk_actual_fp", None)

            if pid is None or gdate is None or fp is None:
                continue

            player_names[pid] = name
            date_key = str(gdate)[:10]  # YYYY-MM-DD

            if date_key not in matrix:
                matrix[date_key] = {}
            matrix[date_key][pid] = float(fp)

        return player_names, matrix

    @staticmethod
    def _compute_pair_correlations(
        player_names: Dict[int, str],
        matrix: Dict[str, Dict[int, float]],
        min_games: int = 5,
    ) -> List[Dict[str, Any]]:
        """Compute pairwise correlations for all player pairs.

        Parameters
        ----------
        player_names : dict
            player_id → player_name
        matrix : dict
            game_date_str → {player_id → dk_actual_fp}
        min_games : int
            Minimum shared games required.

        Returns
        -------
        list of dicts with keys: player_a_id, player_a_name, player_b_id,
        player_b_name, correlation, shared_games
        """
        player_ids = sorted(player_names.keys())
        results: List[Dict[str, Any]] = []

        for i, pid_a in enumerate(player_ids):
            for pid_b in player_ids[i + 1:]:
                # Collect shared game FPs
                fps_a: List[float] = []
                fps_b: List[float] = []
                for _date, game_data in matrix.items():
                    if pid_a in game_data and pid_b in game_data:
                        fps_a.append(game_data[pid_a])
                        fps_b.append(game_data[pid_b])

                if len(fps_a) < min_games:
                    continue

                corr = CorrelationService._pearson(fps_a, fps_b)
                if corr is not None:
                    results.append({
                        "player_a_id": pid_a,
                        "player_a_name": player_names[pid_a],
                        "player_b_id": pid_b,
                        "player_b_name": player_names[pid_b],
                        "correlation": corr,
                        "shared_games": len(fps_a),
                    })

        return results

    # ── Public API ────────────────────────────────────────────────

    async def get_team_correlations(
        self,
        team_id: int,
        days: int = 60,
        sport: str = "nba",
    ) -> Dict[str, Any]:
        """Get all player-pair correlations for a team.

        Queries PlayerMinutesHistory for ``days`` of data, builds a game-FP
        matrix, and computes pairwise Pearson correlations.

        Returns dict with 'pairs', 'team_id', 'games_analyzed', 'players'.
        """
        cache_key = f"team_corr_{sport}_{team_id}_{days}"
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached

        from app.db.database import is_db_available, get_session

        if not is_db_available():
            return {"error": "Database not available", "pairs": []}

        from app.db.models import PlayerMinutesHistory
        from sqlalchemy import select
        from datetime import datetime, timedelta, timezone

        cutoff = datetime.now(timezone.utc) - timedelta(days=days)

        try:
            async with get_session() as session:
                stmt = (
                    select(PlayerMinutesHistory)
                    .where(
                        PlayerMinutesHistory.team_id == team_id,
                        PlayerMinutesHistory.game_date >= cutoff,
                        PlayerMinutesHistory.dk_actual_fp.isnot(None),
                    )
                    .order_by(PlayerMinutesHistory.game_date)
                )
                result = await session.execute(stmt)
                rows = result.scalars().all()
        except Exception as exc:
            logger.error(f"Correlation query failed for team {team_id}: {exc}")
            return {"error": str(exc), "pairs": []}

        if not rows:
            return {
                "team_id": team_id,
                "pairs": [],
                "games_analyzed": 0,
                "players": 0,
                "message": "No historical data found",
            }

        player_names, matrix = self._build_game_fp_matrix(rows)
        min_games = 3 if sport == "cbb" else 5
        pairs = self._compute_pair_correlations(player_names, matrix, min_games=min_games)

        # Sort by absolute correlation descending
        pairs.sort(key=lambda p: abs(p["correlation"]), reverse=True)

        result = {
            "team_id": team_id,
            "pairs": pairs,
            "games_analyzed": len(matrix),
            "players": len(player_names),
            "days": days,
        }
        self._cache_set(cache_key, result)
        return result

    async def get_correlated_pairs(
        self,
        team_id: int,
        min_correlation: float = 0.3,
        days: int = 60,
        sport: str = "nba",
    ) -> Dict[str, Any]:
        """Get only strongly correlated player pairs for a team.

        Wraps ``get_team_correlations`` and filters to pairs above the
        correlation threshold.
        """
        all_data = await self.get_team_correlations(team_id, days, sport=sport)
        if "error" in all_data:
            return all_data

        strong_pairs = [
            p for p in all_data.get("pairs", [])
            if abs(p["correlation"]) >= min_correlation
        ]

        return {
            "team_id": team_id,
            "pairs": strong_pairs,
            "total_pairs": len(all_data.get("pairs", [])),
            "strong_pairs_count": len(strong_pairs),
            "min_correlation": min_correlation,
            "games_analyzed": all_data.get("games_analyzed", 0),
            "players": all_data.get("players", 0),
            "days": days,
        }

    async def get_game_correlations(
        self,
        home_team_id: int,
        away_team_id: int,
        days: int = 60,
        sport: str = "nba",
    ) -> Dict[str, Any]:
        """Get intra-team and cross-team correlations for a game matchup.

        Computes correlations within each team, plus cross-team pairs
        (useful for bring-back / negative correlation logic).
        """
        cache_key = f"game_corr_{sport}_{home_team_id}_{away_team_id}_{days}"
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached

        from app.db.database import is_db_available, get_session

        if not is_db_available():
            return {"error": "Database not available"}

        from app.db.models import PlayerMinutesHistory
        from sqlalchemy import select, or_
        from datetime import datetime, timedelta, timezone

        cutoff = datetime.now(timezone.utc) - timedelta(days=days)

        try:
            async with get_session() as session:
                stmt = (
                    select(PlayerMinutesHistory)
                    .where(
                        or_(
                            PlayerMinutesHistory.team_id == home_team_id,
                            PlayerMinutesHistory.team_id == away_team_id,
                        ),
                        PlayerMinutesHistory.game_date >= cutoff,
                        PlayerMinutesHistory.dk_actual_fp.isnot(None),
                    )
                    .order_by(PlayerMinutesHistory.game_date)
                )
                result = await session.execute(stmt)
                rows = result.scalars().all()
        except Exception as exc:
            logger.error(f"Game correlation query failed: {exc}")
            return {"error": str(exc)}

        if not rows:
            return {
                "home_team_id": home_team_id,
                "away_team_id": away_team_id,
                "home_pairs": [],
                "away_pairs": [],
                "cross_team_pairs": [],
                "message": "No data found",
            }

        # Separate rows by team
        home_rows = [r for r in rows if r.team_id == home_team_id]
        away_rows = [r for r in rows if r.team_id == away_team_id]

        # Intra-team correlations
        home_names, home_matrix = self._build_game_fp_matrix(home_rows)
        away_names, away_matrix = self._build_game_fp_matrix(away_rows)

        min_games = 3 if sport == "cbb" else 5
        home_pairs = self._compute_pair_correlations(home_names, home_matrix, min_games=min_games)
        away_pairs = self._compute_pair_correlations(away_names, away_matrix, min_games=min_games)

        # Cross-team correlations (combine all rows, compute pairs across teams)
        all_names, all_matrix = self._build_game_fp_matrix(rows)
        all_pairs = self._compute_pair_correlations(all_names, all_matrix, min_games=min_games)

        home_ids = set(home_names.keys())
        away_ids = set(away_names.keys())
        cross_pairs = [
            p for p in all_pairs
            if (p["player_a_id"] in home_ids and p["player_b_id"] in away_ids)
            or (p["player_a_id"] in away_ids and p["player_b_id"] in home_ids)
        ]

        # Sort each by absolute correlation
        for pairs_list in (home_pairs, away_pairs, cross_pairs):
            pairs_list.sort(key=lambda p: abs(p["correlation"]), reverse=True)

        result = {
            "home_team_id": home_team_id,
            "away_team_id": away_team_id,
            "home_pairs": home_pairs,
            "away_pairs": away_pairs,
            "cross_team_pairs": cross_pairs,
            "games_analyzed": len(all_matrix),
            "days": days,
        }
        self._cache_set(cache_key, result)
        return result
