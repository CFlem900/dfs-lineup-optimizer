"""Agent 6 — Coach Adjustment Learning.

Analyses recent game data to detect when a coach's rotation
patterns have shifted: rotation depth changes, pace adjustments,
B2B specific patterns, post-trade rotation shifts.

Additionally provides an **active-depth filter** that zeros out fringe
players (10th–13th man) beyond the coach's historical rotation depth.
This prevents 240-minute normalization from diluting starter/bench
projections with non-zero fringe minutes.

Uses **reasoning tier** — runs as daily background job, cached in Redis.
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ai import AIRequest, CoachPatternUpdate
from app.models.player import PlayerProjection
from app.services.agents.sport_context import get_sport_preamble
from app.services.ai_service import AIService

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are an NBA coaching pattern analyst.

Given recent game data for a coach, analyse whether their rotation patterns
have shifted from their known baseline. Look for:

1. **Rotation depth**: More or fewer players used?
2. **Starter minutes**: Up or down from baseline?
3. **Bench usage**: More or fewer minutes?
4. **Pace adjustments**: Team pace changed?
5. **B2B patterns**: How does the coach handle back-to-backs?
6. **Star treatment**: Star player minutes trending?

Return JSON:
{
  "coach_name": "Name",
  "team_id": int,
  "starter_multiplier_adj": float (delta, e.g. +0.02 = starters get 2% more),
  "bench_multiplier_adj": float (delta),
  "star_multiplier_adj": float (delta),
  "rotation_size_adj": int (+ or - players in rotation),
  "b2b_penalty_adj": float (delta to B2B penalty),
  "pace_factor_adj": float (delta to pace factor),
  "reasoning": "2-3 sentence explanation",
  "based_on_games": int,
  "confidence": float 0.0-1.0
}

Be conservative. confidence below 0.5 means "not enough evidence".
"""

CACHE_KEY_PREFIX = "ai:coach_pattern:"
CACHE_TTL = 86400  # 24 hours

# Separate cache prefix and TTL for rotation depth data
DEPTH_CACHE_PREFIX = "coach:rotation_depth:"
DEPTH_CACHE_TTL = 3600  # 1 hour

# Active-depth filter constants
MIN_ACTUAL_MINUTES_THRESHOLD = 4.0  # Players below this aren't in the rotation
MIN_GAMES_FOR_DEPTH = 3             # Need at least 3 game dates for a stable average
RECENT_GAME_LOOKBACK_DAYS = 21      # Look back 21 days for recent games
MAX_GAME_DATES = 5                  # Use the 5 most recent game dates
SAFETY_FLOOR_DEPTH = 7              # Never set expected depth below 7
INJURY_EXPANSION_THRESHOLD = 3      # Expand depth by 1 if this many+ players are out


class CoachLearningAgent:
    """Learns evolving coaching patterns from recent game data."""

    def __init__(self, ai_service: AIService, cache_service=None):
        self._ai = ai_service
        self._cache = cache_service

    @property
    def is_available(self) -> bool:
        return self._ai.is_available

    # ------------------------------------------------------------------
    # AI-driven coach pattern analysis
    # ------------------------------------------------------------------

    def analyze_coach_patterns(
        self, coach_name: str, team_id: int, recent_games: List[Dict],
        sport: str = "nba",
    ) -> Optional[CoachPatternUpdate]:
        """Analyse coach patterns from recent game data."""
        if not self._ai.is_available or not recent_games:
            return None

        cache_key = f"{CACHE_KEY_PREFIX}{team_id}"
        cached = self._cache_get(cache_key)
        if cached:
            return cached

        game_lines = []
        for g in recent_games[:10]:
            players_str = ", ".join(
                f"{p.get('name', '?')} ({p.get('minutes', 0):.0f}m"
                f"{' S' if p.get('starter') else ''})"
                for p in g.get("players", [])[:10]
            )
            game_lines.append(
                f"  {g.get('date', '?')} vs {g.get('opponent', '?')} "
                f"(Score: {g.get('score', '?')}, Pace: {g.get('pace', '?')}): "
                f"{players_str}"
            )

        user_prompt = (
            f"**Coach**: {coach_name} (Team ID: {team_id})\n"
            f"**Last {len(recent_games)} games**:\n"
            + "\n".join(game_lines) + "\n\n"
            f"Analyse rotation pattern changes from these games."
        )

        request = AIRequest(
            system_prompt=get_sport_preamble(sport) + SYSTEM_PROMPT,
            user_prompt=user_prompt,
            model_tier="reasoning",
            max_tokens=1024,
            temperature=0.3,
            response_format="json",
            agent_name="coach_learning",
        )

        data = self._ai.complete_json(request)
        if data is None:
            return None

        try:
            result = CoachPatternUpdate(**data)
            self._cache_set(cache_key, result)
            logger.info(
                f"[CoachAgent] {coach_name}: adj starter={result.starter_multiplier_adj:+.2f}, "
                f"bench={result.bench_multiplier_adj:+.2f}, "
                f"confidence={result.confidence:.2f}"
            )
            return result
        except Exception as exc:
            logger.warning(f"[CoachAgent] Parse failed: {exc}")
            return None

    # ------------------------------------------------------------------
    # Adjusted profile retrieval (consumed by rotation engine Step 3.5)
    # ------------------------------------------------------------------

    def get_adjusted_profile(self, team_id: int) -> Optional[CoachPatternUpdate]:
        """Retrieve cached AI-learned coach pattern adjustments.

        Called by ``RotationEngine.project_team_rotation()`` at Step 3.5.
        Returns ``None`` when no cached data is available (the engine
        falls back to static ``CoachProfile`` values).
        """
        cache_key = f"{CACHE_KEY_PREFIX}{team_id}"
        return self._cache_get(cache_key)

    # ------------------------------------------------------------------
    # Active-depth filter (Step 3.75 — before 240-min normalization)
    # ------------------------------------------------------------------

    def get_expected_rotation_size(
        self, team_id: int, sport: str = "nba",
    ) -> Optional[int]:
        """Return cached rotation depth (cache-only, no DB query).

        The DB query is performed ahead of time by
        ``prefetch_rotation_depth()`` on the main FastAPI event loop,
        which populates the cache.  This method is safe to call from
        sync ThreadPoolExecutor threads.

        Returns
        -------
        int or None
            Cached rotation size, or ``None`` if not yet prefetched.
        """
        depth = self._cache_get_raw(f"{DEPTH_CACHE_PREFIX}{team_id}")
        if depth is not None:
            try:
                return int(depth)
            except (TypeError, ValueError):
                pass
        return None

    async def prefetch_rotation_depth(
        self, team_id: int, session: AsyncSession, sport: str = "nba",
    ) -> Optional[int]:
        """Pre-fetch rotation depth on the main event loop.

        Must be ``await``-ed from an async router **before** dispatching
        sync work to ``run_in_executor``.  Uses the request-scoped
        ``AsyncSession`` so there is no independent pool and no orphaned
        connections.

        Stores the result in cache so that the subsequent sync call to
        ``get_expected_rotation_size()`` returns instantly.
        """
        # Return from cache if already warm
        cached = self._cache_get_raw(f"{DEPTH_CACHE_PREFIX}{team_id}")
        if cached is not None:
            try:
                return int(cached)
            except (TypeError, ValueError):
                pass

        result = await self._query_rotation_depth(team_id, sport, session)
        if result is not None:
            self._cache_set_raw(
                f"{DEPTH_CACHE_PREFIX}{team_id}",
                str(result),
                ttl=DEPTH_CACHE_TTL,
            )
        return result

    async def _query_rotation_depth(
        self, team_id: int, sport: str, session: AsyncSession,
    ) -> Optional[int]:
        """Async DB query: average active-player count over recent games.

        Queries ``player_minutes_history`` for the last 5 distinct game
        dates for this team (within 21 days), then counts players with
        > 4.0 actual minutes per date and returns the rounded average.

        Uses the caller-provided ``AsyncSession`` (request-scoped) so
        the query runs on the main FastAPI event loop with proper
        connection lifecycle.
        """
        from app.db.models import PlayerMinutesHistory
        from sqlalchemy import cast, Date, func, select

        cutoff = datetime.now(timezone.utc) - timedelta(days=RECENT_GAME_LOOKBACK_DAYS)

        try:
            # Subquery: 5 most recent distinct game dates for this team
            date_subq = (
                select(
                    cast(PlayerMinutesHistory.game_date, Date).label("gd")
                )
                .where(
                    PlayerMinutesHistory.team_id == team_id,
                    PlayerMinutesHistory.sport == sport,
                    PlayerMinutesHistory.game_date >= cutoff,
                )
                .distinct()
                .order_by(
                    cast(PlayerMinutesHistory.game_date, Date).desc()
                )
                .limit(MAX_GAME_DATES)
                .subquery()
            )

            # Count players with > 4.0 actual_minutes per game date
            stmt = (
                select(
                    cast(PlayerMinutesHistory.game_date, Date).label("gd"),
                    func.count().label("active_count"),
                )
                .where(
                    PlayerMinutesHistory.team_id == team_id,
                    PlayerMinutesHistory.sport == sport,
                    PlayerMinutesHistory.actual_minutes
                    > MIN_ACTUAL_MINUTES_THRESHOLD,
                    cast(PlayerMinutesHistory.game_date, Date).in_(
                        select(date_subq.c.gd)
                    ),
                )
                .group_by(cast(PlayerMinutesHistory.game_date, Date))
            )
            result = await session.execute(stmt)
            rows = result.all()

            if len(rows) < MIN_GAMES_FOR_DEPTH:
                return None

            # Recency-weighted average: recent games count more than older
            # games.  EMA-style weights: most recent game = weight 1.0,
            # each older game decays by 0.80.
            # This captures coaches tightening rotations late in the season
            # and reacting to roster changes (trades, injuries).
            _RECENCY_DECAY = 0.80
            rows_sorted = sorted(rows, key=lambda r: r.gd, reverse=True)
            weighted_sum = 0.0
            weight_sum = 0.0
            for i, r in enumerate(rows_sorted):
                w = _RECENCY_DECAY ** i
                weighted_sum += r.active_count * w
                weight_sum += w

            avg_rotation = weighted_sum / weight_sum if weight_sum > 0 else 9.0

            # Season progression adjustment: after All-Star break (mid-Feb),
            # coaches tighten rotations by ~0.5 players on average.
            now = datetime.now(timezone.utc)
            if now.month >= 2 and now.day >= 15 or now.month >= 3:
                avg_rotation = max(7, avg_rotation - 0.5)

            return round(avg_rotation)

        except Exception as exc:
            logger.warning(f"[CoachAgent] Rotation depth query failed: {exc}")
            return None

    def calculate_active_depth_and_filter(
        self,
        team_id: int,
        projections: Dict[int, PlayerProjection],
        sport: str = "nba",
    ) -> Dict[int, PlayerProjection]:
        """Zero out fringe players beyond the coach's expected rotation depth.

        This method runs as **Step 3.75** in the rotation engine pipeline,
        after coach multipliers (Step 3.5) and before 240-minute
        normalization (Step 4).  It prevents the normalizer from
        distributing minutes to deep bench players (10th–13th man) who
        historically don't see the floor, thereby protecting starter and
        core bench projections.

        Parameters
        ----------
        team_id : int
            NBA team ID.
        projections : dict
            ``player_id → PlayerProjection`` (post-coach-multipliers,
            pre-normalization).
        sport : str
            ``"nba"`` or ``"cbb"``.  Filter is NBA-only; CBB rosters
            have different rotation dynamics.

        Returns
        -------
        dict
            Modified projections dict with fringe players zeroed out.
            Returns input unchanged when:
            - sport is not ``"nba"``
            - DB data unavailable (graceful degradation)
            - expected depth < 7 (safety floor)
            - active roster ≤ expected depth (no filtering needed)
        """
        if sport != "nba":
            return projections

        if not projections:
            return projections

        expected_depth = self.get_expected_rotation_size(team_id, sport)

        if expected_depth is None:
            return projections

        # Safety floor: never cut below 7-man rotation
        if expected_depth < SAFETY_FLOOR_DEPTH:
            return projections

        # Separate active players from already-zeroed (injured/DNP)
        active: List[tuple] = []  # (player_id, projection)
        injured_count = 0

        for pid, proj in projections.items():
            if proj.adjusted_minutes > 0:
                active.append((pid, proj))
            elif proj.adjusted_minutes == 0.0 and proj.confidence == 0.0:
                injured_count += 1

        # Injury expansion safety valve: if 3+ players are out,
        # the coach is likely going deeper into the bench
        effective_depth = expected_depth
        if injured_count >= INJURY_EXPANSION_THRESHOLD:
            effective_depth += 1

        # No filtering needed if active roster fits within depth
        if len(active) <= effective_depth:
            return projections

        # Rank active players by composite score:
        #   baseline_minutes (60%) + dk_salary / 1000 (40%)
        # Higher composite = higher rotation priority
        def _composite(pid_proj: tuple) -> float:
            _, proj = pid_proj
            salary_component = (proj.dk_salary / 1000.0) if proj.dk_salary else 0.0
            return proj.baseline_minutes * 0.6 + salary_component * 0.4

        active.sort(key=_composite, reverse=True)

        # Zero out players beyond the effective depth
        to_zero = active[effective_depth:]
        for pid, proj in to_zero:
            proj.adjusted_minutes = 0.0
            proj.coach_decision_dnp = True
            proj.confidence = 0.0
            depth_note = (
                f" | Coach depth filter: outside {effective_depth}-man rotation"
            )
            proj.reason = (proj.reason or "") + depth_note
            projections[pid] = proj

        if to_zero:
            zeroed_names = [p.player_name for _, p in to_zero]
            logger.info(
                f"[DepthFilter] team={team_id}: zeroed {len(to_zero)} "
                f"fringe players (depth={effective_depth}): "
                f"{', '.join(zeroed_names)}"
            )

        return projections

    # ------------------------------------------------------------------
    # Cache helpers
    # ------------------------------------------------------------------

    def _cache_get(self, key: str) -> Optional[CoachPatternUpdate]:
        if not self._cache:
            return None
        try:
            data = self._cache.get_sync(key)
            if data:
                parsed = json.loads(data) if isinstance(data, str) else data
                return CoachPatternUpdate(**parsed)
        except Exception:
            pass
        return None

    def _cache_set(self, key: str, update: CoachPatternUpdate):
        if not self._cache:
            return
        try:
            self._cache.set_sync(key, update.model_dump_json(), ttl=CACHE_TTL)
        except Exception:
            pass

    def _cache_get_raw(self, key: str) -> Optional[str]:
        """Get a raw string value from cache (for simple int/string data)."""
        if not self._cache:
            return None
        try:
            data = self._cache.get_sync(key)
            if data is not None:
                return str(data)
        except Exception:
            pass
        return None

    def _cache_set_raw(self, key: str, value: str, ttl: int = DEPTH_CACHE_TTL):
        """Set a raw string value in cache."""
        if not self._cache:
            return
        try:
            self._cache.set_sync(key, value, ttl=ttl)
        except Exception:
            pass

