"""Contrarian / Fade List Service.

Identifies players to fade (high ownership + limited ceiling) and
leverage plays (low ownership + high ceiling) for GPP tournament
lineup construction.

Fade score = ownership_pct * (1 - ceiling_percentile)
Leverage score = (1 - ownership_pct/100) * ceiling_percentile
"""

import logging
import statistics
import time as _time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class FadeService:
    """Generates contrarian fade and leverage lists from pool data."""

    _CACHE_TTL = 120  # 2 minutes

    def __init__(self):
        self._cache: Dict[str, Any] = {}
        self._cache_ts: Dict[str, float] = {}

    def _cache_get(self, key: str) -> Optional[Any]:
        if key in self._cache and (_time.time() - self._cache_ts.get(key, 0)) < self._CACHE_TTL:
            return self._cache[key]
        return None

    def _cache_set(self, key: str, value: Any) -> None:
        self._cache[key] = value
        self._cache_ts[key] = _time.time()

    @staticmethod
    def _pool_hash(pool: List) -> str:
        """Quick hash of a pool for cache keying."""
        size = len(pool)
        sample_ids = [
            str(getattr(p, "player_id", 0)) for p in pool[:5]
        ]
        return f"{size}:{'_'.join(sample_ids)}"

    def generate_fade_list(
        self,
        pool: List,  # List[PlayerPoolEntry]
        ownership_threshold_fade: float = 25.0,
        ownership_threshold_leverage: float = 8.0,
        ceiling_percentile_fade: float = 0.50,
        ceiling_percentile_leverage: float = 0.75,
        limit: int = 20,
    ) -> Dict[str, Any]:
        """Analyze player pool for fade and leverage candidates.

        Parameters
        ----------
        pool : list of PlayerPoolEntry
            The full player pool with projections and ownership.
        ownership_threshold_fade : float
            Minimum ownership % to be considered a fade candidate.
        ownership_threshold_leverage : float
            Maximum ownership % to be considered a leverage play.
        ceiling_percentile_fade : float
            Players below this ceiling percentile are fade candidates.
        ceiling_percentile_leverage : float
            Players above this ceiling percentile are leverage candidates.
        limit : int
            Max number of results per category.

        Returns
        -------
        dict with "fades", "leverage_plays", and summary stats.
        """
        cache_key = f"fade_{self._pool_hash(pool)}" if pool else "empty"
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached

        if not pool:
            return {
                "fades": [],
                "leverage_plays": [],
                "pool_size": 0,
                "message": "Empty player pool",
            }

        # Extract ceiling values for percentile computation
        ceilings = [
            getattr(p, "ceiling_fp", 0) or 0
            for p in pool
            if (getattr(p, "ceiling_fp", 0) or 0) > 0
        ]

        if not ceilings:
            return {
                "fades": [],
                "leverage_plays": [],
                "pool_size": len(pool),
                "message": "No ceiling data available",
            }

        ceilings_sorted = sorted(ceilings)
        median_ceiling = statistics.median(ceilings_sorted)

        # Compute percentile thresholds
        def _percentile(data: List[float], pct: float) -> float:
            idx = int(len(data) * pct)
            idx = min(idx, len(data) - 1)
            return data[idx]

        fade_ceiling_threshold = _percentile(ceilings_sorted, ceiling_percentile_fade)
        leverage_ceiling_threshold = _percentile(ceilings_sorted, ceiling_percentile_leverage)

        fades = []
        leverage_plays = []

        for p in pool:
            ownership = getattr(p, "ownership_projection", None)
            ceiling = getattr(p, "ceiling_fp", 0) or 0
            projected_fp = getattr(p, "projected_fp", 0) or 0
            salary = getattr(p, "salary", 0) or 0
            player_name = getattr(p, "player_name", "Unknown")

            if ownership is None:
                continue

            # Compute ceiling percentile for this player
            ceil_pctl = sum(1 for c in ceilings_sorted if c <= ceiling) / len(ceilings_sorted)

            player_data = {
                "player_id": getattr(p, "player_id", 0),
                "player_name": player_name,
                "position": getattr(p, "position", ""),
                "team": getattr(p, "team_abbreviation", ""),
                "salary": salary,
                "projected_fp": round(projected_fp, 1),
                "ceiling_fp": round(ceiling, 1),
                "floor_fp": round(getattr(p, "floor_fp", 0) or 0, 1),
                "ownership_pct": round(ownership, 1),
                "ceiling_percentile": round(ceil_pctl, 2),
                "dk_value": round(getattr(p, "dk_value", 0) or 0, 2),
            }

            # Fade candidates: high ownership + below-median ceiling
            if ownership >= ownership_threshold_fade and ceiling < fade_ceiling_threshold:
                fade_score = (ownership / 100.0) * (1.0 - ceil_pctl)
                player_data["fade_score"] = round(fade_score, 3)
                player_data["reason"] = (
                    f"{ownership:.0f}% owned but ceiling ({ceiling:.1f}) "
                    f"is below {ceiling_percentile_fade*100:.0f}th percentile ({fade_ceiling_threshold:.1f})"
                )
                fades.append(player_data)

            # Leverage candidates: low ownership + high ceiling
            elif ownership <= ownership_threshold_leverage and ceiling >= leverage_ceiling_threshold:
                leverage_score = (1.0 - ownership / 100.0) * ceil_pctl
                player_data["leverage_score"] = round(leverage_score, 3)
                player_data["reason"] = (
                    f"Only {ownership:.0f}% owned with ceiling ({ceiling:.1f}) "
                    f"above {ceiling_percentile_leverage*100:.0f}th percentile ({leverage_ceiling_threshold:.1f})"
                )
                leverage_plays.append(player_data)

        # Sort by score descending
        fades.sort(key=lambda x: x.get("fade_score", 0), reverse=True)
        leverage_plays.sort(key=lambda x: x.get("leverage_score", 0), reverse=True)

        # Count ownership-available players
        with_ownership = sum(
            1 for p in pool if getattr(p, "ownership_projection", None) is not None
        )

        result = {
            "fades": fades[:limit],
            "leverage_plays": leverage_plays[:limit],
            "pool_size": len(pool),
            "players_with_ownership": with_ownership,
            "median_ceiling": round(median_ceiling, 1),
            "fade_ceiling_threshold": round(fade_ceiling_threshold, 1),
            "leverage_ceiling_threshold": round(leverage_ceiling_threshold, 1),
            "thresholds": {
                "fade_ownership_min": ownership_threshold_fade,
                "leverage_ownership_max": ownership_threshold_leverage,
                "fade_ceiling_percentile": ceiling_percentile_fade,
                "leverage_ceiling_percentile": ceiling_percentile_leverage,
            },
        }
        self._cache_set(cache_key, result)
        return result

    def get_player_scores(self, pool: List) -> Dict[int, Dict[str, float]]:
        """Return per-player fade/leverage scores for optimizer integration.

        Returns
        -------
        dict mapping player_id → {"fade_score": float} or {"leverage_score": float}.
        Players that are neither fade nor leverage candidates are omitted.
        """
        data = self.generate_fade_list(pool)
        scores: Dict[int, Dict[str, float]] = {}
        for entry in data.get("fades", []):
            pid = entry.get("player_id", 0)
            if pid:
                scores[pid] = {"fade_score": entry.get("fade_score", 0.0)}
        for entry in data.get("leverage_plays", []):
            pid = entry.get("player_id", 0)
            if pid:
                scores[pid] = {"leverage_score": entry.get("leverage_score", 0.0)}
        return scores
