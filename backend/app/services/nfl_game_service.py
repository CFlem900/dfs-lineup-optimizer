"""NFL game / schedule service.

Fetches the NFL schedule from ESPN's public-facing scoreboard API and
returns it in the same :class:`Schedule` shape the existing routers
expect, so ``/api/scoreboard?sport=nfl`` works without any router-level
changes.

ESPN endpoint:
  GET https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard
      ?dates=YYYYMMDD

The shape we care about::

  {
    "events": [
      {
        "id": "401547601",
        "date": "2024-09-08T17:00Z",
        "status": {"type": {"state": "pre"|"in"|"post", ...}},
        "competitions": [{
          "competitors": [
            {"team": {"id": "12", "abbreviation": "KC"}, "homeAway": "home", ...},
            {"team": {"id": "30", "abbreviation": "JAX"}, "homeAway": "away", ...}
          ]
        }]
      },
      ...
    ]
  }

The :class:`GameInfo` model was designed for NBA and carries
basketball-specific fields (pace, opp_*_pg, last_5_ppg). Until we build
a real NFL team-stats source those fields are stubbed at 0.0 — the
frontend's slate page only reads the matchup metadata (teams, time,
status) so the zero stats are invisible to the user.
"""

from __future__ import annotations

import logging
from datetime import date as _date_cls, datetime, timezone
from typing import Any, Dict, List, Optional

try:
    # zoneinfo ships with Python 3.9+; preferred over pytz.
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover — fallback for stripped-down envs
    _ET = timezone.utc  # last-ditch fallback; ET conversion becomes no-op

from app.models.game import GameInfo, Schedule, TeamGameStats
from app.services.http_resilience import APIGroup, resilient_get

logger = logging.getLogger(__name__)


def _utc_iso_to_et_iso(utc_iso: Optional[str]) -> Optional[str]:
    """Convert ESPN's UTC ISO string to a timezone-aware ET ISO string.

    ESPN returns times like ``"2024-09-08T17:00Z"`` (always UTC). Lineup
    locks happen in ET (DK's published kickoff time), so storing the
    raw UTC string under a field literally named ``game_time_et`` would
    cause "lock" comparisons that look correct but actually fire late.
    Convert here, output ISO 8601 with the right offset
    (``"2024-09-08T13:00:00-04:00"`` during EDT or ``-05:00`` during EST).

    Returns None for empty input or unparseable strings rather than
    raising — a missing kickoff time should not abort schedule parsing.
    """
    if not utc_iso:
        return None
    raw = utc_iso.strip()
    # ESPN uses 'Z' suffix; Python ≤3.10 fromisoformat doesn't accept Z
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt_utc = datetime.fromisoformat(raw)
    except ValueError:
        return utc_iso  # pass through; better than dropping the field
    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=timezone.utc)
    return dt_utc.astimezone(_ET).isoformat()


_ESPN_NFL_SCOREBOARD = (
    "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
)
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


def _espn_state_to_status(state: str) -> str:
    """Map ESPN's status state code to our normalized labels."""
    s = (state or "").lower()
    if s == "pre":
        return "Scheduled"
    if s == "in":
        return "In Progress"
    if s == "post":
        return "Final"
    return state or "Unknown"


def _stub_team_stats(
    team_id: int, abbr: str, name: str,
) -> TeamGameStats:
    """Build a TeamGameStats with zeros for NFL (no team-stats engine yet).

    The basketball-specific fields (pace, opp_*_pg, last_5_ppg) don't
    apply to NFL — zeros are correct placeholders. The frontend slate
    cards only render id/abbr/name; numeric fields aren't shown for NFL.
    """
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


class NFLGameService:
    """Real NFL schedule, sourced from ESPN's hidden scoreboard API."""

    def __init__(self, data_service=None):
        # ``data_service`` is a NFLDataService used to translate ESPN
        # team IDs to our internal numbering. Passed in by the
        # ServiceContainer so we don't double-instantiate the team table.
        self._data_service = data_service
        # Mirror NBA GameService's instance attrs so the lineup builder's
        # defensive ``getattr(svc, '_db_cache', None)`` doesn't crash.
        self._team_stats_cache: Dict[int, Any] = {}
        self._team_stats_cache_date: Optional[str] = None
        self._db_cache = None

    # ── Public API ────────────────────────────────────────────────────

    def get_games(self, game_date: Optional[str] = None) -> Schedule:
        """Return the NFL schedule for a date.

        ``game_date`` is YYYY-MM-DD; defaults to today. The response is
        in the same Schedule shape the NBA service returns so the
        existing /api/scoreboard handler renders it without changes.

        Empty schedule (date with no games) returns a valid Schedule
        with ``game_count=0`` rather than 500-ing.
        """
        gd = game_date or _date_cls.today().isoformat()
        # ESPN expects YYYYMMDD (no dashes) on the ?dates= query string.
        url = f"{_ESPN_NFL_SCOREBOARD}?dates={gd.replace('-', '')}"

        try:
            resp = resilient_get(
                url,
                group=APIGroup.ESPN_NFL,
                headers={"User-Agent": _USER_AGENT},
            )
            data = resp.json()
        except Exception as exc:
            logger.warning(
                "[NFLGameService] ESPN scoreboard fetch failed for %s: %s", gd, exc,
            )
            return Schedule(date=gd, game_count=0, games=[], slates=[])

        events = data.get("events") or []
        games: List[GameInfo] = []
        for ev in events:
            try:
                game = self._parse_event(ev, gd)
                if game is not None:
                    games.append(game)
            except Exception as exc:
                logger.debug(
                    "[NFLGameService] Skipped malformed event %s: %s",
                    ev.get("id", "?"), exc,
                )

        # Best-effort live-weather enrichment (Prompt 7.5). Outdoor
        # games get a fresh Open-Meteo forecast snapped to kickoff;
        # closed-roof venues short-circuit to the dome sentinel; any
        # per-game error leaves ``weather=None`` and the rest of the
        # slate keeps loading. Wind matters more in NFL than people
        # realise — kickers in 20mph wind lose ~10 yards of FG range.
        try:
            from app.services.nfl_weather_service import (
                enrich_games_with_weather,
            )
            enrich_games_with_weather(games)
        except Exception as exc:
            logger.warning(
                "[NFLGameService] Weather enrichment failed for %s: %s",
                gd, exc,
            )

        # Slate list (Prompt 7.9) — same rationale as MLB: shipping
        # ``slates=[]`` blocked the frontend from resolving a
        # ``draft_group_id``, which broke the universal player-pool
        # fetch added in Prompt 7.7.
        from app.services.mlb_game_service import _build_dk_slates
        slates = _build_dk_slates(games, gd, sport="nfl")

        logger.info(
            "[NFLGameService] %s — parsed %d events into %d games "
            "across %d slate(s)",
            gd, len(events), len(games), len(slates),
        )
        return Schedule(
            date=gd, game_count=len(games), games=games, slates=slates,
        )

    def get_scoreboard(self, dates: Optional[str] = None) -> Schedule:
        """Public alias for :meth:`get_games` matching the ESPN endpoint
        naming. Accepts either YYYY-MM-DD or ESPN's compact YYYYMMDD."""
        if dates and len(dates) == 8 and dates.isdigit():
            # Convert ESPN-style YYYYMMDD → YYYY-MM-DD for our parser
            dates = f"{dates[0:4]}-{dates[4:6]}-{dates[6:8]}"
        return self.get_games(dates)

    def get_dvp_matchup_factors(self, opponent_team_id: int) -> Dict[str, float]:
        """No NFL DvP engine yet — return empty dict."""
        return {}

    def has_game_on_date(self, team_id: int, game_date: str) -> bool:
        """Defensive check — fetch the date's schedule and search."""
        try:
            schedule = self.get_games(game_date)
        except Exception:
            return False
        for g in schedule.games:
            if g.home_team.team_id == team_id or g.away_team.team_id == team_id:
                return True
        return False

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
        """Translate one ESPN event into a GameInfo.

        Returns None when the event is unparseable (missing competitors,
        bad team IDs, etc.) so the caller can skip it without aborting
        the whole schedule.
        """
        comp = (ev.get("competitions") or [{}])[0]
        comps = comp.get("competitors") or []
        if len(comps) < 2:
            return None

        # competitors come as a 2-element list; sort home → away by tag
        home = next((c for c in comps if c.get("homeAway") == "home"), None)
        away = next((c for c in comps if c.get("homeAway") == "away"), None)
        if not home or not away:
            return None

        home_team = self._resolve_team(home)
        away_team = self._resolve_team(away)
        if not home_team or not away_team:
            return None

        # ESPN stamps the venue on the competition (not the event). Used
        # downstream by ``nfl_weather_service`` to pick lat/lon and decide
        # dome vs outdoor — the weather pipeline silently no-ops when
        # venue is None, so a missing field here just means no weather.
        venue_name: Optional[str] = None
        venue_blob = comp.get("venue") or {}
        if isinstance(venue_blob, dict):
            venue_name = (
                venue_blob.get("fullName") or venue_blob.get("name") or None
            )

        # ESPN status block: {"type": {"state": "pre", "name": "...", "completed": false}}
        status_state = (
            ev.get("status", {}).get("type", {}).get("state", "")
        )
        status_label = _espn_state_to_status(status_state)

        # ESPN returns kickoff in UTC (e.g. "2024-09-08T17:00Z"). Convert
        # to America/New_York so ``game_time_et`` actually holds ET — DK's
        # lineup-lock cutoff is published in ET, and downstream
        # comparisons assume the field name is honest.
        game_time = _utc_iso_to_et_iso(ev.get("date"))

        return GameInfo(
            game_id=str(ev.get("id") or f"nfl-{gd}-{home_team.team_abbreviation}"),
            game_date=gd,
            game_time_et=game_time,
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
        """Map an ESPN competitor.team to our internal team record."""
        team_blob = competitor.get("team") or {}
        espn_id_str = team_blob.get("id")
        try:
            espn_id = int(espn_id_str) if espn_id_str is not None else None
        except (TypeError, ValueError):
            espn_id = None

        # Prefer ID translation via the data service; fall back to abbrev.
        rec = None
        if self._data_service is not None and espn_id is not None:
            rec = self._data_service.get_team_by_espn_id(espn_id)
        if rec is None and team_blob.get("abbreviation"):
            if self._data_service is not None:
                rec = self._data_service.get_team_by_abbreviation(team_blob["abbreviation"])

        if rec is None:
            # ESPN gave us a team we don't know about — synthesize from
            # the feed so the schedule still parses, but log so we can
            # patch the team table.
            logger.warning(
                "[NFLGameService] Unknown ESPN team id=%s abbr=%s — synthesizing",
                espn_id, team_blob.get("abbreviation"),
            )
            return _stub_team_stats(
                team_id=espn_id or 0,
                abbr=team_blob.get("abbreviation", "???"),
                name=team_blob.get("displayName") or team_blob.get("shortDisplayName") or "Unknown",
            )

        return _stub_team_stats(
            team_id=rec["id"],
            abbr=rec["abbreviation"],
            name=rec["full_name"],
        )
