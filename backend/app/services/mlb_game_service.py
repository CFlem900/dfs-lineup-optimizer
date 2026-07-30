"""MLB game / schedule service.

ESPN endpoint: site.api.espn.com/apis/site/v2/sports/baseball/mlb/scoreboard
              ?dates=YYYYMMDD

Captures venue name from each event for future park-factor integration.
Reuses NFL's UTC→ET conversion helper to keep ``game_time_et`` honest
(MLB's lineup-lock semantics aren't tied to ET like NFL's, but
consistency across sports is cheap and saves comparison surprises).
"""

from __future__ import annotations

import logging
from datetime import date as _date_cls
from typing import Any, Dict, List, Optional

from app.models.game import DFSSlate, GameInfo, Schedule, TeamGameStats
from app.services.http_resilience import APIGroup, resilient_get
from app.services.nfl_game_service import _utc_iso_to_et_iso  # shared helper

logger = logging.getLogger(__name__)


_ESPN_MLB_SCOREBOARD = (
    "https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/scoreboard"
)
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


def _espn_state_to_status(state: str) -> str:
    """ESPN status state → our normalized labels (same mapping as NFL)."""
    s = (state or "").lower()
    if s == "pre":
        return "Scheduled"
    if s == "in":
        return "In Progress"
    if s == "post":
        return "Final"
    return state or "Unknown"


def _build_dk_slates(
    games: List[GameInfo],
    target_date: str,
    sport: str,
) -> List[DFSSlate]:
    """Build the slate list for a non-NBA sport (MLB/NFL).

    Why this exists (Prompt 7.9): NBA / CBB game services ship a
    populated ``Schedule.slates`` list with each entry carrying a real
    DK ``draft_group_id``. The frontend uses that DG ID as the input
    to /api/player-pool, /api/generate-lineups, and /api/dk-upload.

    MLB and NFL game services historically returned ``slates=[]``,
    so the frontend's slate-page widget could never resolve a DG ID
    and the universal player-pool fetch hit its "No DK draft group"
    pre-flight guard. That's why MLB users saw an empty player pool.

    Resolution
    ----------
    1. Try DK lobby via :class:`DKSlateService` for the requested
       sport — returns one ``DKSlateInfo`` per Classic DraftGroup,
       each with a real ``draft_group_id``.
    2. For each DK slate, attach all of *games* to it (we don't
       attempt per-game DK ↔ ESPN matching for MLB/NFL yet — DK
       and ESPN team abbreviations diverge frequently and a wrong
       match silently drops a game from the slate). Every slate
       carries the same game list but a distinct DG ID.
    3. If DK lobby is unreachable (or the sport has zero Classic
       slates today), fall back to a single "All Games" slate with
       no DG ID — preserves the previous behaviour but at least
       the frontend now has slate metadata to chew on.
    """
    if not games:
        return []

    sorted_games = sorted(games, key=lambda g: g.game_sequence)

    # Lazy import — DKSlateService transitively pulls in app.config /
    # registry, and importing at module level would risk circular
    # import with app.services.game_service.
    try:
        from app.services.dk_slate_service import DKSlateService
        dk_slates = DKSlateService().get_slates(target_date, sport=sport)
    except Exception as exc:
        logger.debug(
            "[Slates/%s] DK lobby fetch failed for %s: %s — "
            "falling back to single-slate",
            sport.upper(), target_date, exc,
        )
        dk_slates = []

    if not dk_slates:
        # No DK lobby data for this sport+date — log loudly so an
        # operator tailing the server can tell whether the empty pool
        # is a code path issue or a real DK-side gap. The frontend's
        # pre-flight guard will surface this state to the user.
        logger.warning(
            "[Slates/%s] DK lobby returned 0 Classic DraftGroups for %s "
            "(found %d ESPN games on the schedule). Frontend will "
            "render a fallback single-slate with draft_group_id=None. "
            "Possible causes: contests not yet published, DK lobby "
            "outage, or sticky-cache miss after a server restart.",
            sport.upper(), target_date, len(sorted_games),
        )
        return [DFSSlate(
            name="Main",
            label=f"All Games ({len(sorted_games)})",
            game_count=len(sorted_games),
            games=sorted_games,
            draft_group_id=None,
        )]

    out: List[DFSSlate] = []
    for dk in dk_slates:
        out.append(DFSSlate(
            name=dk.name,
            label=dk.label,
            game_count=len(sorted_games),
            games=sorted_games,
            draft_group_id=dk.draft_group_id,
        ))
    return out


def _stub_team_stats(team_id: int, abbr: str, name: str) -> TeamGameStats:
    return TeamGameStats(
        team_id=team_id,
        team_name=name,
        team_abbreviation=abbr,
        season_pace=0.0,
        season_off_rating=0.0,
        season_def_rating=0.0,
        season_ppg=0.0,
        season_opp_ppg=0.0,
        last_5_ppg=0.0,
    )


class MLBGameService:
    """Live MLB schedule sourced from ESPN's hidden scoreboard endpoint."""

    def __init__(self, data_service=None):
        self._data_service = data_service
        # Mirror GameService instance attrs
        self._team_stats_cache: Dict[int, Any] = {}
        self._team_stats_cache_date: Optional[str] = None
        self._db_cache = None

    # ── Public API ────────────────────────────────────────────────────

    def get_games(self, game_date: Optional[str] = None) -> Schedule:
        gd = game_date or _date_cls.today().isoformat()
        url = f"{_ESPN_MLB_SCOREBOARD}?dates={gd.replace('-', '')}"

        try:
            resp = resilient_get(
                url,
                group=APIGroup.ESPN_MLB,
                headers={"User-Agent": _USER_AGENT},
            )
            data = resp.json()
        except Exception as exc:
            logger.warning(
                "[MLBGameService] ESPN scoreboard fetch failed for %s: %s", gd, exc,
            )
            return Schedule(date=gd, game_count=0, games=[], slates=[])

        events = data.get("events") or []
        games: List[GameInfo] = []
        for ev in events:
            try:
                g = self._parse_event(ev, gd)
                if g is not None:
                    games.append(g)
            except Exception as exc:
                logger.debug(
                    "[MLBGameService] Skipped malformed event %s: %s",
                    ev.get("id", "?"), exc,
                )

        # Best-effort live-weather enrichment (Prompt 4.3). Outdoor games
        # get a fresh Open-Meteo forecast snapped to first pitch; closed-
        # roof parks short-circuit to synthetic dome defaults; any
        # per-game error leaves ``weather=None`` and the rest of the
        # slate keeps loading. The fan-out uses a thread pool so one
        # slow ballpark fetch can't block the whole scoreboard.
        try:
            from app.services.mlb_weather_service import enrich_games_with_weather
            enrich_games_with_weather(games)
        except Exception as exc:
            logger.warning(
                "[MLBGameService] Weather enrichment failed for %s: %s",
                gd, exc,
            )

        # Build the slate list (Prompt 7.9). Without this MLB ships
        # ``slates=[]`` and the frontend can't resolve a draft_group_id
        # to drive the universal player-pool fetch — users see "no DK
        # draft group resolved for this slate yet" forever.
        slates = _build_dk_slates(games, gd, sport="mlb")

        logger.info(
            "[MLBGameService] %s — parsed %d events into %d games "
            "across %d slate(s)",
            gd, len(events), len(games), len(slates),
        )
        return Schedule(
            date=gd, game_count=len(games), games=games, slates=slates,
        )

    def get_scoreboard(self, dates: Optional[str] = None) -> Schedule:
        """Public alias mirroring the NFL service. Accepts YYYY-MM-DD or YYYYMMDD."""
        if dates and len(dates) == 8 and dates.isdigit():
            dates = f"{dates[0:4]}-{dates[4:6]}-{dates[6:8]}"
        return self.get_games(dates)

    def get_dvp_matchup_factors(self, opponent_team_id: int) -> Dict[str, float]:
        return {}

    def has_game_on_date(self, team_id: int, game_date: str) -> bool:
        try:
            schedule = self.get_games(game_date)
        except Exception:
            return False
        return any(
            g.home_team.team_id == team_id or g.away_team.team_id == team_id
            for g in schedule.games
        )

    def get_team_game(
        self, team_id: int, game_date: Optional[str] = None,
    ) -> Optional[GameInfo]:
        try:
            schedule = self.get_games(game_date)
        except Exception:
            return None
        for g in schedule.games:
            if g.home_team.team_id == team_id or g.away_team.team_id == team_id:
                return g
        return None

    # ── Internal: ESPN response → our schema ─────────────────────────

    def _parse_event(self, ev: Dict[str, Any], gd: str) -> Optional[GameInfo]:
        comp = (ev.get("competitions") or [{}])[0]
        competitors = comp.get("competitors") or []
        if len(competitors) < 2:
            return None

        home = next((c for c in competitors if c.get("homeAway") == "home"), None)
        away = next((c for c in competitors if c.get("homeAway") == "away"), None)
        if not home or not away:
            return None

        home_team = self._resolve_team(home)
        away_team = self._resolve_team(away)
        if not home_team or not away_team:
            return None

        # ESPN stamps the venue on the competition (not the event). It can
        # be missing on older or postponed games — pass through as None.
        venue_name: Optional[str] = None
        venue_blob = comp.get("venue") or {}
        if isinstance(venue_blob, dict):
            venue_name = (venue_blob.get("fullName") or venue_blob.get("name") or None)
        # Fall back to the home team's home_park when ESPN omits the venue
        # (almost always identical anyway since MLB rarely plays neutral-site).
        if not venue_name and self._data_service is not None:
            home_record = self._data_service.get_team_by_id(home_team.team_id)
            if home_record:
                venue_name = home_record.get("home_park")

        status_state = ev.get("status", {}).get("type", {}).get("state", "")
        status_label = _espn_state_to_status(status_state)
        game_time_et = _utc_iso_to_et_iso(ev.get("date"))

        return GameInfo(
            game_id=str(ev.get("id") or f"mlb-{gd}-{home_team.team_abbreviation}"),
            game_date=gd,
            game_time_et=game_time_et,
            game_sequence=0,
            game_status=status_label,
            home_team=home_team,
            away_team=away_team,
            projected_total=0.0,
            projected_home_score=0.0,
            projected_away_score=0.0,
            projected_spread=0.0,
            projected_pace=0.0,
            pace_label="Average",
            venue=venue_name,
        )

    def _resolve_team(self, competitor: Dict[str, Any]) -> Optional[TeamGameStats]:
        team_blob = competitor.get("team") or {}
        try:
            espn_id = int(team_blob.get("id")) if team_blob.get("id") is not None else None
        except (TypeError, ValueError):
            espn_id = None

        rec = None
        if self._data_service is not None and espn_id is not None:
            rec = self._data_service.get_team_by_espn_id(espn_id)
        if rec is None and team_blob.get("abbreviation"):
            if self._data_service is not None:
                rec = self._data_service.get_team_by_abbreviation(team_blob["abbreviation"])

        if rec is None:
            logger.warning(
                "[MLBGameService] Unknown ESPN team id=%s abbr=%s — synthesizing",
                espn_id, team_blob.get("abbreviation"),
            )
            return _stub_team_stats(
                team_id=espn_id or 0,
                abbr=team_blob.get("abbreviation", "???"),
                name=team_blob.get("displayName") or "Unknown",
            )

        return _stub_team_stats(
            team_id=rec["id"], abbr=rec["abbreviation"], name=rec["full_name"],
        )
