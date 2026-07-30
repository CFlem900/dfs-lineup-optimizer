"""Live rotation monitor — tracks minutes vs projections + foul trouble.

Polls the BallDontLie REST API (``/stats?game_ids[]=...``) for live box
scores and compares actual minutes played against RotationEngine projections.

**Data source clarification:**

    Live box scores come from ``BallDontLieService.get_live_box_scores()``
    — a **REST API** endpoint, NOT an MCP tool.  The BDL MCP only supports
    ``get_teams``, ``get_players``, ``get_games``, ``get_game``.  Attempting
    ``BDLMCPClient.get_live_box_scores()`` raises ``BDLMethodNotAvailable``.

Output is a JSON-serializable dict designed for consumption by a C#
backend (or any HTTP consumer):

.. code-block:: json

    {
        "snapshot_utc": "2026-02-25T20:15:00Z",
        "game_date": "2026-02-25",
        "period_of_game": 2,
        "elapsed_game_minutes": 24.0,
        "players": [
            {
                "player_name": "Luka Doncic",
                "team": "DAL",
                "minutes_played": 18.5,
                "projected_total_minutes": 36.0,
                "expected_minutes_at_this_point": 18.0,
                "minutes_delta": 0.5,
                "rotation_status": "on_track",
                "personal_fouls": 3,
                "foul_trouble": true,
                "pts": 15, "reb": 4, "ast": 3, "pra": 22
            }
        ],
        "flags": {
            "foul_trouble": ["Luka Doncic"],
            "over_minutes": [],
            "under_minutes": ["Jayson Tatum"]
        },
        "summary": {
            "total_tracked": 8,
            "on_track_count": 5,
            "over_count": 1,
            "under_count": 2,
            "foul_trouble_count": 1
        }
    }

Usage
-----
::

    from app.services.rotation_monitor import RotationMonitor

    monitor = RotationMonitor(bdl_service, live_game_state_service)

    projections = {
        "Luka Doncic":   {"projected_minutes": 36.0, "team": "DAL"},
        "Jayson Tatum":  {"projected_minutes": 34.5, "team": "BOS"},
    }

    snapshot = await monitor.poll_snapshot("2026-02-25", projections)
    json_str = snapshot.model_dump_json(indent=2)
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────

NBA_REGULATION_MINUTES = 48.0
NBA_QUARTER_MINUTES = 12.0

# Rotation deviation thresholds (fraction of expected minutes)
OVER_MINUTES_THRESHOLD = 1.15   # 15% more minutes than expected
UNDER_MINUTES_THRESHOLD = 0.80  # 20% fewer minutes than expected

# Foul trouble
FOUL_TROUBLE_FIRST_HALF = 3     # 3+ fouls in first half
FOUL_TROUBLE_THIRD_Q = 4        # 4+ fouls by end of Q3
FOUL_TROUBLE_FOURTH_Q = 5       # 5+ fouls in Q4 (standard foul-out is 6)


# ── Pydantic models (JSON-serializable for C# consumption) ───────────────


class PlayerRotationStatus(BaseModel):
    """Live rotation snapshot for one player."""

    player_name: str
    team: str
    minutes_played: float = 0.0
    projected_total_minutes: float = 0.0
    expected_minutes_at_this_point: float = 0.0
    minutes_delta: float = 0.0
    rotation_status: str = "not_started"   # on_track, over, under, not_started
    personal_fouls: int = 0
    foul_trouble: bool = False
    pts: int = 0
    reb: int = 0
    ast: int = 0
    pra: int = 0


class RotationFlags(BaseModel):
    """Flagged player lists for quick filtering."""

    foul_trouble: List[str] = Field(default_factory=list)
    over_minutes: List[str] = Field(default_factory=list)
    under_minutes: List[str] = Field(default_factory=list)


class RotationSummary(BaseModel):
    """Aggregate counts."""

    total_tracked: int = 0
    on_track_count: int = 0
    over_count: int = 0
    under_count: int = 0
    foul_trouble_count: int = 0


class RotationSnapshot(BaseModel):
    """Full JSON payload for the C# backend."""

    snapshot_utc: str
    game_date: str
    period_of_game: int = 0
    elapsed_game_minutes: float = 0.0
    players: List[PlayerRotationStatus] = Field(default_factory=list)
    flags: RotationFlags = Field(default_factory=RotationFlags)
    summary: RotationSummary = Field(default_factory=RotationSummary)


# ── Pure functions ────────────────────────────────────────────────────────


def estimate_elapsed_minutes(period: int, is_final: bool) -> float:
    """Estimate elapsed game minutes from BDL period number.

    BDL provides period (1-4 for regulation, 5+ for OT) but no game clock.
    We use the midpoint of the current quarter as a best estimate.
    """
    if is_final:
        if period <= 4:
            return NBA_REGULATION_MINUTES
        return NBA_REGULATION_MINUTES + (period - 4) * 5.0  # 5 min OT

    if period <= 0:
        return 0.0

    if period <= 4:
        completed = period - 1
        return (completed * NBA_QUARTER_MINUTES) + (NBA_QUARTER_MINUTES / 2)

    # In OT
    completed_ot = period - 5
    return NBA_REGULATION_MINUTES + completed_ot * 5.0 + 2.5


def expected_minutes_at(
    projected_total: float,
    elapsed_game_minutes: float,
) -> float:
    """How many player minutes we'd expect at this point in the game.

    Assumes linear distribution of minutes across the game.
    """
    if NBA_REGULATION_MINUTES <= 0 or projected_total <= 0:
        return 0.0
    fraction = min(1.0, elapsed_game_minutes / NBA_REGULATION_MINUTES)
    return projected_total * fraction


def classify_rotation(
    actual: float,
    expected: float,
    elapsed_game_minutes: float,
) -> str:
    """Classify a player's rotation status.

    Returns 'on_track', 'over', 'under', or 'not_started'.
    """
    if elapsed_game_minutes <= 0:
        return "not_started"
    if expected <= 0:
        return "on_track"

    ratio = actual / expected
    if ratio >= OVER_MINUTES_THRESHOLD:
        return "over"
    if ratio <= UNDER_MINUTES_THRESHOLD:
        return "under"
    return "on_track"


def is_foul_trouble(fouls: int, period: int) -> bool:
    """Determine if a player is in foul trouble given the current period."""
    if period <= 2:
        return fouls >= FOUL_TROUBLE_FIRST_HALF
    if period == 3:
        return fouls >= FOUL_TROUBLE_THIRD_Q
    return fouls >= FOUL_TROUBLE_FOURTH_Q


def parse_bdl_minutes(raw: str) -> float:
    """Parse BDL minutes string ('32:15' or '32' or '') to float."""
    if not raw:
        return 0.0
    try:
        if ":" in str(raw):
            parts = str(raw).split(":")
            return float(parts[0]) + float(parts[1]) / 60.0
        return float(raw)
    except (ValueError, IndexError):
        return 0.0


# ── Core service ──────────────────────────────────────────────────────────


class RotationMonitor:
    """Live rotation tracking via BDL REST API (not MCP).

    Parameters
    ----------
    bdl_service : BallDontLieService
        The REST client with ``get_live_box_scores(game_ids)``.
    live_game_state_service : LiveGameStateService
        Async service for fetching current game periods.
    """

    def __init__(self, bdl_service, live_game_state_service):
        self._bdl = bdl_service
        self._live_game = live_game_state_service

    async def poll_snapshot(
        self,
        game_date: str,
        projections: Dict[str, Dict[str, Any]],
    ) -> RotationSnapshot:
        """Build a single rotation snapshot for all tracked players.

        This is the method you call every 2 minutes from your polling loop.

        Args:
            game_date: ``"YYYY-MM-DD"`` for tonight's slate.
            projections: Keyed by player name. Each value is a dict with:
                - ``projected_minutes`` (float): RotationEngine projection.
                - ``team`` (str): Team abbreviation.

        Returns:
            ``RotationSnapshot`` — call ``.model_dump()`` for JSON dict
            or ``.model_dump_json()`` for JSON string.
        """
        now_utc = datetime.now(timezone.utc).isoformat()

        # 1. Fetch game states to find in-progress game IDs + periods
        game_states = await self._fetch_game_states(game_date)
        if not game_states:
            return RotationSnapshot(
                snapshot_utc=now_utc,
                game_date=game_date,
            )

        # 2. Collect in-progress BDL game IDs
        active_game_ids: List[int] = []
        seen_game_ids: Set[int] = set()
        max_period = 0

        for gs in game_states.values():
            if gs.bdl_game_id not in seen_game_ids:
                seen_game_ids.add(gs.bdl_game_id)
                if gs.is_in_progress or gs.is_final:
                    active_game_ids.append(gs.bdl_game_id)
                if gs.period > max_period:
                    max_period = gs.period

        if not active_game_ids:
            return RotationSnapshot(
                snapshot_utc=now_utc,
                game_date=game_date,
            )

        # Determine if any game is final (for elapsed calc)
        any_final = any(
            gs.is_final for gs in game_states.values()
        )
        elapsed = estimate_elapsed_minutes(max_period, any_final)

        # 3. Fetch live box scores via BDL REST API
        box_scores = self._fetch_box_scores(active_game_ids)

        # 4. Index box scores by player name
        stats_by_name: Dict[str, dict] = {}
        for row in box_scores:
            player = row.get("player") or {}
            first = player.get("first_name", "")
            last = player.get("last_name", "")
            name = f"{first} {last}".strip()
            if name:
                stats_by_name[name] = row

        # 5. Build player rotation statuses
        players: List[PlayerRotationStatus] = []
        flags = RotationFlags()
        on_track = over = under = foul_ct = 0

        for player_name, proj in projections.items():
            projected_mins = proj.get("projected_minutes", 0.0)
            team = proj.get("team", "")

            # Find this player's game state for their specific period
            player_gs = game_states.get(team.upper())
            player_period = player_gs.period if player_gs else max_period
            player_final = player_gs.is_final if player_gs else any_final
            player_elapsed = estimate_elapsed_minutes(
                player_period, player_final
            )

            # Look up live stats
            row = stats_by_name.get(player_name, {})
            mins_played = parse_bdl_minutes(row.get("min", ""))
            fouls = int(row.get("pf", 0) or 0)
            pts = int(row.get("pts", 0) or 0)
            reb = int(row.get("reb", 0) or 0)
            ast = int(row.get("ast", 0) or 0)

            expected = expected_minutes_at(projected_mins, player_elapsed)
            delta = round(mins_played - expected, 1)
            status = classify_rotation(mins_played, expected, player_elapsed)
            in_foul_trouble = is_foul_trouble(fouls, player_period)

            # Count statuses
            if status == "over":
                over += 1
                flags.over_minutes.append(player_name)
            elif status == "under":
                under += 1
                flags.under_minutes.append(player_name)
            elif status == "on_track":
                on_track += 1

            if in_foul_trouble:
                foul_ct += 1
                flags.foul_trouble.append(player_name)

            players.append(PlayerRotationStatus(
                player_name=player_name,
                team=team,
                minutes_played=round(mins_played, 1),
                projected_total_minutes=round(projected_mins, 1),
                expected_minutes_at_this_point=round(expected, 1),
                minutes_delta=delta,
                rotation_status=status,
                personal_fouls=fouls,
                foul_trouble=in_foul_trouble,
                pts=pts,
                reb=reb,
                ast=ast,
                pra=pts + reb + ast,
            ))

        # Sort by largest absolute minutes deviation
        players.sort(key=lambda p: abs(p.minutes_delta), reverse=True)

        return RotationSnapshot(
            snapshot_utc=now_utc,
            game_date=game_date,
            period_of_game=max_period,
            elapsed_game_minutes=round(elapsed, 1),
            players=players,
            flags=flags,
            summary=RotationSummary(
                total_tracked=len(players),
                on_track_count=on_track,
                over_count=over,
                under_count=under,
                foul_trouble_count=foul_ct,
            ),
        )

    # ── Internal helpers ──────────────────────────────────────────────

    async def _fetch_game_states(self, game_date: str) -> dict:
        """Fetch game states via LiveGameStateService."""
        try:
            return await self._live_game.fetch_game_states(game_date)
        except Exception as e:
            logger.error(f"[RotationMonitor] Game state fetch failed: {e}")
            return {}

    def _fetch_box_scores(self, game_ids: List[int]) -> List[dict]:
        """Fetch live box scores via BDL REST API (not MCP)."""
        try:
            return self._bdl.get_live_box_scores(game_ids)
        except Exception as e:
            logger.error(
                f"[RotationMonitor] BDL box score fetch failed: {e}"
            )
            return []
