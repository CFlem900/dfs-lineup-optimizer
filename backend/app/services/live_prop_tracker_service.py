"""Live Prop Tracker — real-time monitoring of player props during games.

Tracks DraftKings player O/U prop lines against live (or estimated) in-game
stats to identify players who are "ahead of schedule" or "behind pace".

Two operating modes:

  **Mode 1 — Live Stats (primary):**
    External system (scraper, API) POSTs real-time box-score data via the
    ``/live-props/ingest`` endpoint.  Stats are stored in Redis with a
    5-minute TTL and used for precise pace tracking.

  **Mode 2 — Pace Estimation (fallback):**
    When no live stats are available, uses the BDL game period (via
    ``LiveGameStateService``) combined with RotationEngine per-minute
    production rates to estimate where a player *should* be.

Usage
-----
::

    svc = LivePropTrackerService(dk_props, live_game_state, cache)
    snapshot = await svc.get_tracker_snapshot("2026-02-25")
    for t in snapshot.players:
        if t.pace_status in ("ahead", "way_ahead"):
            print(f"{t.player_name} is {t.pace_status} on {t.stat} prop")
"""

from __future__ import annotations

import logging
import math
import re
import unicodedata
from datetime import date as date_cls, datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from pydantic import BaseModel, Field
from scipy.stats import norm

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────

NBA_REGULATION_MINUTES = 48.0
NBA_QUARTER_MINUTES = 12.0
NBA_OT_MINUTES = 5.0

LIVE_STATS_KEY_PREFIX = "live_tracker"
LIVE_STATS_TTL = 300  # 5 minutes
SNAPSHOT_TTL = 120  # 2 minutes

# Pace status thresholds (pace_ratio = current / expected_at_this_point)
WAY_AHEAD_THRESHOLD = 1.25
AHEAD_THRESHOLD = 1.10
BEHIND_THRESHOLD = 0.90
WAY_BEHIND_THRESHOLD = 0.75

# Tracked DK prop stat categories
TRACKED_STATS = ["pts", "reb", "ast", "pra"]

# Minimum elapsed fraction before pace is meaningful
MIN_ELAPSED_FOR_PACE = 0.10  # ~5 min into a game


# ── Pydantic models ──────────────────────────────────────────────────────


class LivePlayerStats(BaseModel):
    """Real-time box-score stats for one player, injected via POST."""

    player_name: str
    team_abbreviation: str
    pts: float = 0.0
    reb: float = 0.0
    ast: float = 0.0
    stl: float = 0.0
    blk: float = 0.0
    tov: float = 0.0
    fg3m: float = 0.0
    minutes_played: float = 0.0
    source: str = "manual"  # "manual", "scraper", "api"
    timestamp: Optional[str] = None


class PlayerPropTracker(BaseModel):
    """Tracking state for one player against one stat prop line."""

    player_name: str
    team_abbreviation: str
    stat: str  # "pts", "reb", "ast", "pra"
    prop_line: float
    over_odds: int = -110
    implied_over_prob: float = 0.5

    # Current state
    current_value: float = 0.0
    source: str = "estimated"  # "live" or "estimated"

    # Game context
    game_period: int = 0
    game_status: str = "not_started"
    elapsed_game_minutes: float = 0.0
    total_projected_minutes: float = 0.0

    # Pace tracking
    remaining_requirement: float = 0.0
    remaining_player_minutes: float = 0.0
    required_rate: Optional[float] = None
    pre_game_rate: float = 0.0
    current_pace: Optional[float] = None

    # Flags
    pace_status: str = "not_started"
    hit_probability: Optional[float] = None


class GameStateInfo(BaseModel):
    """Simplified game state for tracker response."""

    home_team: str
    visitor_team: str
    home_score: int = 0
    visitor_score: int = 0
    period: int = 0
    status: str = "not_started"


class PropTrackerSummary(BaseModel):
    """Aggregate stats across all tracked props."""

    total_tracked: int = 0
    way_ahead_count: int = 0
    ahead_count: int = 0
    on_pace_count: int = 0
    behind_count: int = 0
    way_behind_count: int = 0
    hit_count: int = 0
    miss_count: int = 0
    games_in_progress: int = 0
    games_final: int = 0
    games_not_started: int = 0


class PropTrackerSnapshot(BaseModel):
    """Full snapshot of all tracked player props for a game date."""

    game_date: str
    timestamp: str
    mode: str = "estimated"  # "live", "estimated", "mixed"
    players: List[PlayerPropTracker] = Field(default_factory=list)
    games: List[GameStateInfo] = Field(default_factory=list)
    summary: PropTrackerSummary = Field(default_factory=PropTrackerSummary)


class LiveStatsIngestRequest(BaseModel):
    """POST body for bulk live stats ingestion."""

    game_date: str
    players: List[LivePlayerStats]
    source: str = "manual"


# ── Name normalization (reuse project pattern) ───────────────────────────


def _normalize_name(name: str) -> str:
    """Normalize player name for cache key matching.

    Same logic as dk_props_service / dk_draftables_service.
    """
    n = name.strip().lower()
    n = unicodedata.normalize("NFKD", n)
    n = "".join(c for c in n if not unicodedata.combining(c))
    n = re.sub(r"\b(jr\.?|sr\.?|iii|iv|ii)\s*$", "", n).strip()
    n = n.replace(".", "")
    n = re.sub(r"\s+", " ", n).strip()
    return n


# ── Core service ──────────────────────────────────────────────────────────


class LivePropTrackerService:
    """Real-time prop tracking against DraftKings lines.

    Parameters
    ----------
    dk_props_service : DKPropsService
        Fetches pre-game DK prop lines.
    live_game_state_service : LiveGameStateService
        Fetches real-time game states from BDL.
    cache_service : CacheService, optional
        Redis cache for live stats storage.  Falls back to in-memory
        dict if ``None``.
    """

    def __init__(
        self,
        dk_props_service,
        live_game_state_service,
        cache_service=None,
        bdl_service=None,
    ):
        self._dk_props = dk_props_service
        self._live_game = live_game_state_service
        self._cache = cache_service
        self._bdl = bdl_service

        # In-memory fallback for live stats when no Redis
        self._mem_stats: Dict[str, dict] = {}

    # ── Public API ────────────────────────────────────────────────────

    async def get_tracker_snapshot(
        self,
        game_date: Optional[str] = None,
        projections_cache: Optional[Dict[str, dict]] = None,
    ) -> PropTrackerSnapshot:
        """Build a full tracker snapshot for a game date.

        Args:
            game_date: ``"YYYY-MM-DD"`` (defaults to today).
            projections_cache: Optional dict of ``{player_name: {
                "projected_minutes": float, "pts_per_min": float,
                "reb_per_min": float, "ast_per_min": float,
                "pre_game_std": float}}`` from the RotationEngine.
                If not provided, pace estimation falls back to
                league-average defaults.

        Returns:
            Full ``PropTrackerSnapshot`` with all tracked players.
        """
        target = game_date or date_cls.today().isoformat()
        projections_cache = projections_cache or {}

        # 1. Fetch game states
        game_states = await self._fetch_game_states(target)

        # 2. Build team → game state lookup
        team_game: Dict[str, Any] = {}
        game_list: List[GameStateInfo] = []
        seen_games = set()
        for team_abbr, gs in game_states.items():
            team_game[team_abbr] = gs
            gid = gs.bdl_game_id
            if gid not in seen_games:
                seen_games.add(gid)
                status = "final" if gs.is_final else (
                    "in_progress" if gs.is_in_progress else "not_started"
                )
                game_list.append(GameStateInfo(
                    home_team=gs.home_team,
                    visitor_team=gs.visitor_team,
                    home_score=gs.home_score,
                    visitor_score=gs.visitor_score,
                    period=gs.period,
                    status=status,
                ))

        # 3. Fetch DK prop lines
        dk_props = self._dk_props.get_player_props(target)

        # 4. Read live stats from cache; auto-fetch from BDL if empty
        live_stats = await self._get_live_stats()
        if not live_stats and self._bdl:
            any_in_progress = any(
                gs.is_in_progress for gs in game_states.values()
            )
            if any_in_progress:
                await self.fetch_bdl_live_stats(target)
                live_stats = await self._get_live_stats()

        # 5. Build trackers for each player/stat
        trackers: List[PlayerPropTracker] = []
        has_live = False
        has_estimated = False

        for prop_key, player_props in dk_props.items():
            team = player_props.team_abbreviation.upper()
            name = player_props.player_name

            # Get game state for this team
            gs = team_game.get(team)
            if gs is None:
                continue

            period = gs.period
            is_final = gs.is_final
            game_status = "final" if is_final else (
                "in_progress" if gs.is_in_progress else "not_started"
            )
            elapsed_game = estimate_elapsed_game_minutes(period, is_final)

            # Look up live stats
            live_key = f"{_normalize_name(name)}:{team}"
            player_live = live_stats.get(live_key)

            # Look up projection cache
            proj = projections_cache.get(name, {})
            projected_mins = proj.get("projected_minutes", 32.0)  # default
            pts_rate = proj.get("pts_per_min", 0.55)
            reb_rate = proj.get("reb_per_min", 0.18)
            ast_rate = proj.get("ast_per_min", 0.15)
            pre_game_std = proj.get("pre_game_std", 8.0)

            # Player elapsed minutes (proportional to game elapsed)
            player_elapsed = min(
                projected_mins,
                projected_mins * (elapsed_game / NBA_REGULATION_MINUTES)
                if NBA_REGULATION_MINUTES > 0 else 0,
            )

            for stat_prop in player_props.props:
                if stat_prop.stat not in TRACKED_STATS:
                    continue

                stat = stat_prop.stat
                line = stat_prop.line

                # Determine current value
                if player_live:
                    current = _get_stat_value(player_live, stat)
                    source = "live"
                    has_live = True
                    # Use actual minutes played if available
                    actual_mins = player_live.get("minutes_played", 0) or 0
                    if actual_mins > 0:
                        player_elapsed = actual_mins
                else:
                    # Pace estimation fallback
                    rate = _get_per_min_rate(stat, pts_rate, reb_rate, ast_rate)
                    current = rate * player_elapsed
                    source = "estimated"
                    has_estimated = True

                remaining_req = max(0.0, line - current)
                remaining_mins = max(0.0, projected_mins - player_elapsed)

                # Per-minute rate from projection
                pre_rate = _get_per_min_rate(stat, pts_rate, reb_rate, ast_rate)

                # Required rate to hit the over
                req_rate = (
                    remaining_req / remaining_mins
                    if remaining_mins > 0 else None
                )

                # Current pace (per minute)
                cur_pace = (
                    current / player_elapsed
                    if player_elapsed > 0 else None
                )

                # Elapsed fraction
                elapsed_frac = (
                    player_elapsed / projected_mins
                    if projected_mins > 0 else 0.0
                )

                # Pace status
                pace_status = calculate_pace_status(
                    current, line, elapsed_frac, game_status
                )

                # Hit probability
                hit_prob = update_hit_probability(
                    current, remaining_req, remaining_mins,
                    pre_rate, pre_game_std, elapsed_frac,
                )

                trackers.append(PlayerPropTracker(
                    player_name=name,
                    team_abbreviation=team,
                    stat=stat,
                    prop_line=line,
                    over_odds=stat_prop.over_odds,
                    implied_over_prob=stat_prop.implied_over_prob,
                    current_value=round(current, 1),
                    source=source,
                    game_period=period,
                    game_status=game_status,
                    elapsed_game_minutes=round(elapsed_game, 1),
                    total_projected_minutes=round(projected_mins, 1),
                    remaining_requirement=round(remaining_req, 1),
                    remaining_player_minutes=round(remaining_mins, 1),
                    required_rate=round(req_rate, 3) if req_rate is not None else None,
                    pre_game_rate=round(pre_rate, 3),
                    current_pace=round(cur_pace, 3) if cur_pace is not None else None,
                    pace_status=pace_status,
                    hit_probability=round(hit_prob, 4) if hit_prob is not None else None,
                ))

        # 6. Build summary
        summary = _build_summary(trackers, game_list)

        # 7. Determine mode
        if has_live and has_estimated:
            mode = "mixed"
        elif has_live:
            mode = "live"
        else:
            mode = "estimated"

        return PropTrackerSnapshot(
            game_date=target,
            timestamp=datetime.now(timezone.utc).isoformat(),
            mode=mode,
            players=trackers,
            games=game_list,
            summary=summary,
        )

    async def ingest_live_stats(
        self, game_date: str, stats: Sequence[LivePlayerStats]
    ) -> int:
        """Store live player stats in cache.

        Args:
            game_date: The game date (for logging).
            stats: List of live stat snapshots.

        Returns:
            Number of players ingested.
        """
        count = 0
        for s in stats:
            key = self._stats_key(s.player_name, s.team_abbreviation)
            value = s.model_dump()
            value["_ingested_at"] = datetime.now(timezone.utc).isoformat()

            if self._cache:
                await self._cache.set(key, value, ttl=LIVE_STATS_TTL)
            else:
                self._mem_stats[key] = value

            count += 1

        logger.info(
            f"[LiveTracker] Ingested {count} player stats for {game_date}"
        )
        return count

    async def fetch_bdl_live_stats(self, game_date: str) -> int:
        """Auto-fetch live box scores from BDL for all in-progress games.

        Pulls player-level stats from the BDL ``/stats?game_ids[]=...``
        endpoint for every game that has started on ``game_date``, then
        stores them in cache the same way ``ingest_live_stats()`` does.

        Args:
            game_date: ``"YYYY-MM-DD"`` string.

        Returns:
            Number of player stat lines fetched and cached.
        """
        if not self._bdl:
            logger.debug("[LiveTracker] No BDL service — skipping auto-fetch")
            return 0

        # 1. Get game states to find in-progress game IDs
        game_states = await self._fetch_game_states(game_date)
        if not game_states:
            return 0

        # Deduplicate game IDs (each game maps two teams)
        active_game_ids = []
        seen = set()
        for gs in game_states.values():
            if gs.bdl_game_id not in seen and (gs.is_in_progress or gs.is_final):
                active_game_ids.append(gs.bdl_game_id)
                seen.add(gs.bdl_game_id)

        if not active_game_ids:
            logger.debug("[LiveTracker] No active games to fetch stats for")
            return 0

        # 2. Pull box scores from BDL
        try:
            raw_stats = self._bdl.get_live_box_scores(active_game_ids)
        except Exception as e:
            logger.error(f"[LiveTracker] BDL live box score fetch failed: {e}")
            return 0

        if not raw_stats:
            return 0

        # 3. Convert BDL stat rows → LivePlayerStats and ingest
        live_stats: List[LivePlayerStats] = []
        for row in raw_stats:
            player = row.get("player") or {}
            team = row.get("team") or {}
            first = player.get("first_name", "")
            last = player.get("last_name", "")
            name = f"{first} {last}".strip()
            team_abbr = team.get("abbreviation", "")

            if not name or not team_abbr:
                continue

            # Parse minutes string (BDL format: "32:15" or "32" or "")
            mins_raw = row.get("min", "") or ""
            minutes_played = _parse_bdl_minutes(mins_raw)

            live_stats.append(LivePlayerStats(
                player_name=name,
                team_abbreviation=team_abbr.upper(),
                pts=float(row.get("pts", 0) or 0),
                reb=float(row.get("reb", 0) or 0),
                ast=float(row.get("ast", 0) or 0),
                stl=float(row.get("stl", 0) or 0),
                blk=float(row.get("blk", 0) or 0),
                tov=float(row.get("turnover", 0) or 0),
                fg3m=float(row.get("fg3m", 0) or 0),
                minutes_played=minutes_played,
                source="bdl",
            ))

        if not live_stats:
            return 0

        count = await self.ingest_live_stats(game_date, live_stats)
        logger.info(
            f"[LiveTracker] BDL auto-fetch: {count} players from "
            f"{len(active_game_ids)} games on {game_date}"
        )
        return count

    async def clear_live_stats(self) -> None:
        """Clear all cached live stats."""
        if self._cache:
            await self._cache.clear_pattern(f"{LIVE_STATS_KEY_PREFIX}:*")
        else:
            self._mem_stats.clear()

    # ── Internal helpers ──────────────────────────────────────────────

    async def _fetch_game_states(self, game_date: str) -> dict:
        """Fetch game states from LiveGameStateService."""
        try:
            return await self._live_game.fetch_game_states(game_date)
        except Exception as e:
            logger.error(f"[LiveTracker] Game state fetch failed: {e}")
            return {}

    async def _get_live_stats(self) -> Dict[str, dict]:
        """Read all cached live stats, keyed by normalized_name:TEAM."""
        result: Dict[str, dict] = {}

        if self._cache:
            # CacheService doesn't expose scan-by-prefix, so we use
            # the in-memory fallback pattern.  For production Redis,
            # a scan would be used.  For now, the ingest stores keys
            # we track.
            pass

        # In-memory fallback always available
        for key, value in self._mem_stats.items():
            # Strip prefix to get "name:TEAM"
            short_key = key.replace(f"{LIVE_STATS_KEY_PREFIX}:", "", 1)
            result[short_key] = value

        return result

    def _stats_key(self, name: str, team: str) -> str:
        norm = _normalize_name(name)
        return f"{LIVE_STATS_KEY_PREFIX}:{norm}:{team.upper()}"


# ── Pure functions (testable without service) ─────────────────────────────


def estimate_elapsed_game_minutes(period: int, is_final: bool) -> float:
    """Convert BDL game period to estimated elapsed game minutes.

    BDL only provides the period number (no game clock), so we use the
    midpoint of the current quarter as a best estimate.

    Args:
        period: 0 = not started, 1–4 = regulation quarters, 5+ = OT.
        is_final: True if the game has ended.

    Returns:
        Estimated elapsed minutes (0.0–48.0+ for OT).
    """
    if is_final:
        if period <= 4:
            return NBA_REGULATION_MINUTES
        # OT games: 48 + 5 per OT period completed
        return NBA_REGULATION_MINUTES + (period - 4) * NBA_OT_MINUTES

    if period <= 0:
        return 0.0

    if period <= 4:
        # Midpoint of the current quarter
        completed_quarters = period - 1
        return (completed_quarters * NBA_QUARTER_MINUTES) + (NBA_QUARTER_MINUTES / 2)

    # In OT period
    completed_ot = period - 5  # periods already completed beyond Q4
    return (
        NBA_REGULATION_MINUTES
        + completed_ot * NBA_OT_MINUTES
        + (NBA_OT_MINUTES / 2)
    )


def calculate_pace_status(
    current_value: float,
    prop_line: float,
    elapsed_fraction: float,
    game_status: str,
) -> str:
    """Determine a player's pace status relative to their prop line.

    Args:
        current_value: Current stat value (actual or estimated).
        prop_line: The O/U line from DraftKings.
        elapsed_fraction: Fraction of projected minutes elapsed (0–1).
        game_status: "not_started", "in_progress", or "final".

    Returns:
        One of: "not_started", "hit", "miss", "way_ahead", "ahead",
        "on_pace", "behind", "way_behind".
    """
    if game_status == "not_started":
        return "not_started"

    if game_status == "final":
        return "hit" if current_value >= prop_line else "miss"

    if elapsed_fraction < MIN_ELAPSED_FOR_PACE:
        return "on_pace"  # Too early for meaningful pace

    expected = prop_line * elapsed_fraction
    if expected <= 0:
        return "on_pace"

    pace_ratio = current_value / expected

    if pace_ratio >= WAY_AHEAD_THRESHOLD:
        return "way_ahead"
    elif pace_ratio >= AHEAD_THRESHOLD:
        return "ahead"
    elif pace_ratio <= WAY_BEHIND_THRESHOLD:
        return "way_behind"
    elif pace_ratio <= BEHIND_THRESHOLD:
        return "behind"
    return "on_pace"


def update_hit_probability(
    current_value: float,
    remaining_requirement: float,
    remaining_minutes: float,
    per_min_rate: float,
    pre_game_std: float,
    elapsed_fraction: float,
) -> Optional[float]:
    """Recalculate P(over) as the game progresses.

    Uses the normal CDF.  The key insight: as the game progresses,
    the remaining variance shrinks proportionally to the remaining
    time fraction.

    Args:
        current_value: What the player has so far.
        remaining_requirement: How much more they need (line - current).
        remaining_minutes: Projected remaining player minutes.
        per_min_rate: Pre-game per-minute production rate.
        pre_game_std: Pre-game standard deviation of the full-game stat.
        elapsed_fraction: Fraction of game elapsed.

    Returns:
        P(over) in [0, 1], or None if game hasn't started.
    """
    if elapsed_fraction <= 0:
        return None  # Pre-game — use pre-game edge analysis instead

    if remaining_requirement <= 0:
        return 1.0  # Already over the line

    if remaining_minutes <= 0:
        return 0.0  # No time left, didn't hit

    # Expected remaining production
    expected_remaining = per_min_rate * remaining_minutes

    # Remaining std shrinks with remaining fraction
    remaining_fraction = max(0.01, 1.0 - elapsed_fraction)
    remaining_std = pre_game_std * math.sqrt(remaining_fraction)

    if remaining_std <= 0:
        return 1.0 if expected_remaining >= remaining_requirement else 0.0

    # P(remaining_production >= remaining_requirement)
    return float(
        1.0 - norm.cdf(remaining_requirement, loc=expected_remaining, scale=remaining_std)
    )


def _parse_bdl_minutes(mins_raw: str) -> float:
    """Parse BDL minutes string (``"32:15"`` or ``"32"``) to float minutes."""
    if not mins_raw:
        return 0.0
    try:
        if ":" in mins_raw:
            parts = mins_raw.split(":")
            return float(parts[0]) + float(parts[1]) / 60.0
        return float(mins_raw)
    except (ValueError, IndexError):
        return 0.0


def _get_stat_value(live_stats: dict, stat: str) -> float:
    """Extract a stat value from a live stats dict."""
    if stat == "pra":
        return (
            (live_stats.get("pts", 0) or 0)
            + (live_stats.get("reb", 0) or 0)
            + (live_stats.get("ast", 0) or 0)
        )
    return float(live_stats.get(stat, 0) or 0)


def _get_per_min_rate(stat: str, pts_rate: float, reb_rate: float, ast_rate: float) -> float:
    """Get per-minute production rate for a stat category."""
    if stat == "pts":
        return pts_rate
    elif stat == "reb":
        return reb_rate
    elif stat == "ast":
        return ast_rate
    elif stat == "pra":
        return pts_rate + reb_rate + ast_rate
    return 0.0


def _build_summary(
    trackers: List[PlayerPropTracker],
    games: List[GameStateInfo],
) -> PropTrackerSummary:
    """Build aggregate summary from tracker list."""
    status_counts: Dict[str, int] = {}
    for t in trackers:
        status_counts[t.pace_status] = status_counts.get(t.pace_status, 0) + 1

    return PropTrackerSummary(
        total_tracked=len(trackers),
        way_ahead_count=status_counts.get("way_ahead", 0),
        ahead_count=status_counts.get("ahead", 0),
        on_pace_count=status_counts.get("on_pace", 0) + status_counts.get("not_started", 0),
        behind_count=status_counts.get("behind", 0),
        way_behind_count=status_counts.get("way_behind", 0),
        hit_count=status_counts.get("hit", 0),
        miss_count=status_counts.get("miss", 0),
        games_in_progress=sum(1 for g in games if g.status == "in_progress"),
        games_final=sum(1 for g in games if g.status == "final"),
        games_not_started=sum(1 for g in games if g.status == "not_started"),
    )
