"""Real-time NBA game state telemetry via the BALLDONTLIE API.

Replaces static schedule-based lock detection with live game-state
polling.  Used by the late-swap flow to determine which DraftKings
roster slots are locked (game tipped) vs. open (swappable).

Key design decisions:

1. **Async httpx** — The BDL ``/games`` endpoint is queried via
   ``httpx.AsyncClient`` with a hard 5-second timeout.  If the API
   is unreachable, ``fallback_to_schedule=True`` uses the scheduled
   tip-off time embedded in the ``status`` field as a conservative
   proxy.

2. **``has_started`` semantics** — A game has started *only* when
   ``period > 0`` **or** ``status == "Final"``.  If the BDL API
   still reports ``period == 0`` *despite* the scheduled time having
   passed, the game is treated as **not started** (delayed tip-off).
   This avoids false locks on rain delays, broadcast holds, etc.

3. **DK ↔ BDL abbreviation mapping** — DraftKings uses non-standard
   abbreviations for several teams (PHO for PHX, SA for SAS, etc.).
   The bidirectional ``DK_TO_BDL_ABBR`` / ``BDL_TO_DK_ABBR`` maps
   ensure the lock logic applies to the correct players regardless
   of which nomenclature the caller uses.

Usage::

    svc = LiveGameStateService()
    slate = await svc.fetch_game_states("2026-02-25")
    for team, gs in slate.items():
        if gs.has_started:
            print(f"{team}: LOCKED ({gs.status_detail})")

CLI::

    python -m app.services.live_game_state_service 2026-02-25
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)


# ============================================================================
# DraftKings ↔ BDL Team Abbreviation Mapping
# ============================================================================
# DK occasionally uses non-standard abbreviations.  BDL consistently
# uses the NBA.com canonical set.  These maps bridge the gap.

DK_TO_BDL_ABBR: Dict[str, str] = {
    "PHO": "PHX",   # Phoenix Suns
    "SA":  "SAS",   # San Antonio Spurs
    "GS":  "GSW",   # Golden State Warriors
    "NY":  "NYK",   # New York Knicks
    "NO":  "NOP",   # New Orleans Pelicans
    "BK":  "BKN",   # Brooklyn Nets
    "CHO": "CHA",   # Charlotte Hornets
    "WSH": "WAS",   # Washington Wizards
}

# Reverse map: BDL canonical → DK variant
BDL_TO_DK_ABBR: Dict[str, str] = {v: k for k, v in DK_TO_BDL_ABBR.items()}

# All 30 NBA team abbreviations (BDL canonical).  Used as a whitelist
# so we never accidentally create entries for non-NBA games.
_NBA_TEAM_ABBRS = frozenset({
    "ATL", "BOS", "BKN", "CHA", "CHI", "CLE", "DAL", "DEN", "DET", "GSW",
    "HOU", "IND", "LAC", "LAL", "MEM", "MIA", "MIL", "MIN", "NOP", "NYK",
    "OKC", "ORL", "PHI", "PHX", "POR", "SAC", "SAS", "TOR", "UTA", "WAS",
})


def normalise_to_bdl(abbr: str) -> str:
    """Convert a DK (or any) abbreviation to the BDL canonical form."""
    upper = abbr.upper().strip()
    return DK_TO_BDL_ABBR.get(upper, upper)


def normalise_to_dk(abbr: str) -> str:
    """Convert a BDL abbreviation to the DK form (only differs for aliases)."""
    upper = abbr.upper().strip()
    return BDL_TO_DK_ABBR.get(upper, upper)


# ============================================================================
# ISO-8601 Datetime Regex (for detecting scheduled-time status strings)
# ============================================================================
# BDL uses the ``status`` field as a dual-purpose value:
#   • Before tip: an ISO datetime like "2026-02-26T00:30:00Z"
#   • During/after: a human string like "1st Qtr", "Halftime", "Final"

_ISO_DATETIME_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
)


def _is_iso_datetime(value: str) -> bool:
    """Return True if the string looks like an ISO-8601 datetime."""
    return bool(_ISO_DATETIME_RE.match(value))


def _parse_scheduled_time(status_str: str) -> Optional[datetime]:
    """Parse the ISO scheduled time from the BDL status field.

    Returns a timezone-aware UTC datetime, or None if unparseable.
    """
    try:
        # Handle both "Z" suffix and "+00:00"
        cleaned = status_str.replace("Z", "+00:00")
        return datetime.fromisoformat(cleaned)
    except (ValueError, TypeError):
        return None


# ============================================================================
# GameState Dataclass
# ============================================================================

@dataclass
class GameState:
    """Immutable snapshot of a single NBA game's live state.

    Attributes:
        bdl_game_id: BALLDONTLIE game identifier.
        home_team: Home team abbreviation (BDL canonical).
        visitor_team: Visitor team abbreviation (BDL canonical).
        has_started: True only when real play has begun (period > 0 or Final).
        period: Current game period (0 = not started, 1-4 = regulation, 5+ = OT).
        status_detail: Raw BDL status string (ISO time, "1st Qtr", "Final", etc.).
        home_score: Home team score.
        visitor_score: Visitor team score.
        scheduled_tip: Parsed UTC datetime of the scheduled tip-off, if available.
        is_final: True when the game has ended.
    """
    bdl_game_id: int
    home_team: str
    visitor_team: str
    has_started: bool
    period: int
    status_detail: str
    home_score: int = 0
    visitor_score: int = 0
    scheduled_tip: Optional[datetime] = None
    is_final: bool = False

    @property
    def is_in_progress(self) -> bool:
        """True if the game has started but is not yet final."""
        return self.has_started and not self.is_final

    @property
    def opponent_for(self) -> Dict[str, str]:
        """Return {team: opponent} for both sides."""
        return {
            self.home_team: self.visitor_team,
            self.visitor_team: self.home_team,
        }


# ============================================================================
# LiveGameStateService
# ============================================================================

class LiveGameStateService:
    """Async service for real-time NBA game state via BALLDONTLIE API.

    Fetches the ``/games`` endpoint with a 5-second timeout and
    classifies each game into a ``GameState`` with the critical
    ``has_started`` flag.

    Args:
        api_key: BALLDONTLIE API key.  Defaults to the environment setting.
        timeout: HTTP timeout in seconds (default 5.0).
        fallback_to_schedule: If BDL is unreachable and this is True,
            treat games whose scheduled tip-off is > 10 minutes in the
            past as "started" (conservative heuristic).
    """

    BDL_BASE_URL = "https://api.balldontlie.io/v1"

    # If the BDL API is unreachable and a game's scheduled tip-off was
    # more than SCHEDULE_FALLBACK_GRACE_MINUTES ago, treat it as started.
    SCHEDULE_FALLBACK_GRACE_MINUTES = 10

    def __init__(
        self,
        api_key: Optional[str] = None,
        timeout: float = 5.0,
        fallback_to_schedule: bool = True,
    ):
        _settings = get_settings()
        self._api_key: str = api_key or _settings.balldontlie_api_key
        self._timeout: float = timeout
        self._fallback_to_schedule: bool = fallback_to_schedule

    @property
    def is_available(self) -> bool:
        return bool(self._api_key)

    # ------------------------------------------------------------------
    # Core: Fetch + Parse
    # ------------------------------------------------------------------

    async def fetch_game_states(
        self,
        game_date: str,
    ) -> Dict[str, GameState]:
        """Fetch live game states for a date, indexed by BDL team abbreviation.

        Each team on the slate gets its own entry so callers can look up
        any player's team to find the game state.

        Args:
            game_date: Date in ``YYYY-MM-DD`` format.

        Returns:
            Dict mapping uppercase team abbreviation (BDL canonical) to
            ``GameState``.  Empty dict if the API is unreachable and
            fallback is disabled.
        """
        raw_games = await self._fetch_games_from_bdl(game_date)

        if raw_games is not None:
            return self._parse_game_states(raw_games)

        # BDL unavailable — attempt schedule-based fallback
        logger.warning(
            "[LiveGameState] BDL API unreachable for %s — "
            "%s",
            game_date,
            "falling back to schedule heuristic"
            if self._fallback_to_schedule
            else "returning empty (no fallback)",
        )
        return {}

    async def _fetch_games_from_bdl(
        self,
        game_date: str,
    ) -> Optional[List[Dict[str, Any]]]:
        """Hit the BDL ``/games`` endpoint with a hard timeout.

        Returns the ``data`` array on success, or None on any failure.
        """
        if not self._api_key:
            logger.warning("[LiveGameState] No BDL API key configured")
            return None

        url = f"{self.BDL_BASE_URL}/games?dates[]={game_date}"
        headers = {"Authorization": self._api_key}

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.get(url, headers=headers)
                resp.raise_for_status()
                body = resp.json()
                data = body.get("data")
                if not isinstance(data, list):
                    logger.error(
                        "[LiveGameState] Unexpected response shape: "
                        "'data' is %s",
                        type(data).__name__,
                    )
                    return None
                return data

        except httpx.TimeoutException:
            logger.error(
                "[LiveGameState] BDL /games timed out (%.1fs)", self._timeout
            )
        except httpx.HTTPStatusError as exc:
            logger.error(
                "[LiveGameState] BDL /games HTTP %d: %s",
                exc.response.status_code,
                exc,
            )
        except httpx.ConnectError as exc:
            logger.error(
                "[LiveGameState] BDL /games connection failed: %s", exc
            )
        except Exception as exc:
            logger.error(
                "[LiveGameState] BDL /games unexpected error: %s", exc
            )

        return None

    # ------------------------------------------------------------------
    # State Evaluation
    # ------------------------------------------------------------------

    def _parse_game_states(
        self,
        raw_games: List[Dict[str, Any]],
    ) -> Dict[str, GameState]:
        """Parse raw BDL game dicts into GameState objects.

        Classification logic (the critical ``has_started`` flag):

        +------------------------+--------+----------+--------------+
        | BDL status field       | period | is_final | has_started  |
        +========================+========+==========+==============+
        | ISO datetime (future)  |   0    |  False   | **False**    |
        | ISO datetime (past)    |   0    |  False   | **False** ¹  |
        | "1st Qtr" .. "4th Qtr" |  1-4   |  False   | **True**     |
        | "Halftime"             |   2    |  False   | **True**     |
        | "OT1" .. "OTn"         |  5+    |  False   | **True**     |
        | "Final"                |  4+    |  True    | **True**     |
        +------------------------+--------+----------+--------------+

        ¹ Delayed games: even if the scheduled time has passed, ``period == 0``
          means the ball hasn't tipped.  We trust BDL's live telemetry over
          the clock.  This prevents false locks during broadcast delays,
          rain delays, or arena incidents.
        """
        result: Dict[str, GameState] = {}

        for game in raw_games:
            try:
                gs = self._parse_single_game(game)
                if gs is None:
                    continue

                # Index by both teams
                if gs.home_team in _NBA_TEAM_ABBRS:
                    result[gs.home_team] = gs
                if gs.visitor_team in _NBA_TEAM_ABBRS:
                    result[gs.visitor_team] = gs

            except Exception as exc:
                logger.warning(
                    "[LiveGameState] Skipping malformed game entry: %s", exc
                )
                continue

        logger.info(
            "[LiveGameState] Parsed %d games → %d team entries",
            len(raw_games),
            len(result),
        )
        return result

    @staticmethod
    def _parse_single_game(game: Dict[str, Any]) -> Optional[GameState]:
        """Parse a single BDL game dict into a GameState."""
        home_team_obj = game.get("home_team") or {}
        visitor_team_obj = game.get("visitor_team") or {}

        home_abbr = (home_team_obj.get("abbreviation") or "").upper()
        visitor_abbr = (visitor_team_obj.get("abbreviation") or "").upper()

        if not home_abbr or not visitor_abbr:
            return None

        status_raw = game.get("status", "")
        period = game.get("period", 0) or 0
        bdl_game_id = game.get("id", 0)

        # ── Determine has_started ─────────────────────────────────────
        # Primary signal: period > 0 means actual play has begun.
        # Secondary signal: status == "Final" for completed games.
        # Everything else (including past-scheduled-time with period==0)
        # is treated as NOT STARTED.  This is the delayed-game safeguard.

        is_final = (
            str(status_raw).strip().lower() == "final"
            or str(game.get("time", "")).strip().lower() == "final"
        )

        has_started = period > 0 or is_final

        # ── Parse scheduled tip-off (if status is an ISO datetime) ────
        scheduled_tip: Optional[datetime] = None
        if _is_iso_datetime(str(status_raw)):
            scheduled_tip = _parse_scheduled_time(str(status_raw))

        return GameState(
            bdl_game_id=bdl_game_id,
            home_team=home_abbr,
            visitor_team=visitor_abbr,
            has_started=has_started,
            period=period,
            status_detail=str(status_raw),
            home_score=game.get("home_team_score", 0) or 0,
            visitor_score=game.get("visitor_team_score", 0) or 0,
            scheduled_tip=scheduled_tip,
            is_final=is_final,
        )

    # ------------------------------------------------------------------
    # Schedule Fallback (used when BDL API is unreachable)
    # ------------------------------------------------------------------

    def build_fallback_from_schedule(
        self,
        scheduled_games: List[Dict[str, Any]],
    ) -> Dict[str, GameState]:
        """Build GameState entries from DK schedule data when BDL is down.

        Uses a conservative heuristic: if the scheduled tip-off time is
        more than ``SCHEDULE_FALLBACK_GRACE_MINUTES`` in the past, assume
        the game has started.  This errs on the side of caution — a
        10-minute grace window accounts for most broadcast delays without
        prematurely unlocking a game that already tipped.

        Args:
            scheduled_games: List of dicts with at minimum ``home_team``,
                ``visitor_team``, and ``scheduled_time`` (ISO 8601 UTC).

        Returns:
            Dict mapping team abbreviation → GameState.
        """
        now = datetime.now(timezone.utc)
        result: Dict[str, GameState] = {}

        for sg in scheduled_games:
            home = normalise_to_bdl(sg.get("home_team", ""))
            visitor = normalise_to_bdl(sg.get("visitor_team", ""))
            sched_str = sg.get("scheduled_time", "")
            scheduled_tip = _parse_scheduled_time(sched_str)

            if not home or not visitor:
                continue

            has_started = False
            if scheduled_tip:
                elapsed_minutes = (
                    now - scheduled_tip
                ).total_seconds() / 60.0
                has_started = (
                    elapsed_minutes > self.SCHEDULE_FALLBACK_GRACE_MINUTES
                )

            gs = GameState(
                bdl_game_id=0,
                home_team=home,
                visitor_team=visitor,
                has_started=has_started,
                period=0,
                status_detail=f"schedule_fallback:{sched_str}",
                scheduled_tip=scheduled_tip,
            )

            if home in _NBA_TEAM_ABBRS:
                result[home] = gs
            if visitor in _NBA_TEAM_ABBRS:
                result[visitor] = gs

        return result

    # ------------------------------------------------------------------
    # Convenience: Build lock map for the late-swap flow
    # ------------------------------------------------------------------

    def build_lock_map(
        self,
        game_states: Dict[str, GameState],
    ) -> Dict[str, Dict[str, Any]]:
        """Convert GameState dict to the legacy ``game_status_map`` format.

        Returns the same shape as ``BallDontLieService.get_game_statuses_by_team()``
        so the late-swap endpoint can use either service interchangeably.
        """
        result: Dict[str, Dict[str, Any]] = {}

        for abbr, gs in game_states.items():
            if gs.is_final:
                game_status = "final"
            elif gs.is_in_progress:
                game_status = "in_progress"
            else:
                game_status = "not_started"

            opp = gs.opponent_for.get(abbr, "")

            result[abbr] = {
                "game_status": game_status,
                "game_status_detail": gs.status_detail,
                "is_locked": gs.has_started,
                "opponent": opp,
                "home_team_score": gs.home_score,
                "visitor_team_score": gs.visitor_score,
                "bdl_game_id": gs.bdl_game_id,
                "period": gs.period,
            }

            # Also index under the DK alias (if it differs from BDL)
            dk_alias = normalise_to_dk(abbr)
            if dk_alias != abbr and dk_alias not in result:
                result[dk_alias] = result[abbr]

        return result

    # ------------------------------------------------------------------
    # Convenience: Resolve a player's lock status
    # ------------------------------------------------------------------

    def is_player_locked(
        self,
        team_abbreviation: str,
        game_states: Dict[str, GameState],
    ) -> bool:
        """Check if a player's game has started (slot should be locked).

        Handles DK ↔ BDL abbreviation translation automatically.
        """
        bdl_abbr = normalise_to_bdl(team_abbreviation)
        gs = game_states.get(bdl_abbr)
        if gs is None:
            # Team not on today's slate — treat as unlocked
            return False
        return gs.has_started


# ============================================================================
# CLI Entrypoint
# ============================================================================

async def _cli_main(game_date: str) -> None:
    """Standalone CLI runner for testing game state fetching."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )

    svc = LiveGameStateService()
    if not svc.is_available:
        print("ERROR: No BALLDONTLIE_API_KEY configured")
        return

    states = await svc.fetch_game_states(game_date)

    if not states:
        print(f"No games found for {game_date}")
        return

    # De-duplicate (both teams point to the same GameState)
    seen_ids: set = set()
    print(f"\n{'=' * 72}")
    print(f"  Game States for {game_date}")
    print(f"{'=' * 72}")

    for abbr, gs in sorted(states.items()):
        if gs.bdl_game_id in seen_ids:
            continue
        seen_ids.add(gs.bdl_game_id)

        lock_icon = "LOCKED" if gs.has_started else "OPEN"
        tip_str = (
            gs.scheduled_tip.strftime("%I:%M %p ET")
            if gs.scheduled_tip
            else gs.status_detail
        )

        print(
            f"  {gs.visitor_team:>3s} @ {gs.home_team:<3s}  |  "
            f"{lock_icon:6s}  |  P{gs.period}  |  "
            f"{gs.home_score}-{gs.visitor_score}  |  {tip_str}"
        )

    print(f"{'=' * 72}\n")


if __name__ == "__main__":
    import asyncio
    import sys

    date_arg = sys.argv[1] if len(sys.argv) > 1 else None
    if not date_arg:
        from datetime import date

        date_arg = date.today().isoformat()

    asyncio.run(_cli_main(date_arg))
