"""Service for scraping Underdog Fantasy pick'em lines.

Fetches Over/Under lines from Underdog's internal API for NBA player
stats.  Unlike DraftKings, Underdog has no official public API — this
service reverse-engineers the endpoints used by the web app.

The scraper is modelled after :class:`DKPropsService`: HTTP fetch with
``httpx``, in-memory cache with configurable TTL, and graceful fallback
on errors.

Reference:
    Community scraper structure — GitHub:aidanhall21/underdog-fantasy-pickem-scraper

API response structure (observed)::

    {
        "players":          [{ "id", "first_name", "last_name", "team_id", "position_id", ... }],
        "appearances":      [{ "id": appearance_id, "player_id", "team_id", "status", ... }],
        "over_under_lines": [{ "options": [...], "over_under": { "appearance_stat": { "appearance_id", "stat" } }, ... }],
        "games":            [{ "id", "home_team_id", "away_team_id", "scheduled_at", ... }]
    }
"""

import logging
import os
import re
import time
import unicodedata
from datetime import date as date_cls, datetime, timezone
from typing import Dict, List, Optional

import httpx

from app.models.underdog import UnderdogGame, UnderdogPickemLine, UnderdogSlate
from app.services.http_resilience import resilient_get, APIGroup

logger = logging.getLogger(__name__)

# ── Configuration ──────────────────────────────────────────────────────

BASE_URL = os.getenv(
    "UNDERDOG_API_BASE_URL",
    "https://api.underdogfantasy.com/beta/v5/over_under_lines",
)

REQUEST_TIMEOUT = 20  # seconds
CACHE_TTL = 600  # 10 minutes — UD lines change less frequently than DK odds

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/121.0.0.0 Safari/537.36"
)

# ── Stat-name mapping ──────────────────────────────────────────────────
# Maps Underdog stat identifiers to our internal keys.
# The UD API uses various formats across versions; handle all known variants.

UD_STAT_MAP: Dict[str, str] = {
    # Primary names (from API response "stat" field)
    "points": "pts",
    "rebounds": "reb",
    "assists": "ast",
    "pts_rebs_asts": "pra",
    "three_pointers_made": "fg3m",
    "fantasy_points": "fantasy_points",
    "steals": "stl_blk",  # UD sometimes groups steals + blocks
    "blks_stls": "stl_blk",
    "blocks_steals": "stl_blk",
    "turnovers": "tov",
    "double_doubles": "dd",
    "triple_doubles": "td",
    # Additional aliases that may appear
    "pts": "pts",
    "reb": "reb",
    "ast": "ast",
    "pra": "pra",
    "fg3m": "fg3m",
    "3pm": "fg3m",
    "stl": "stl_blk",
    "blk": "stl_blk",
}

# Stats we expose for pick'em edge comparison
PICKEM_STATS = {"pts", "reb", "ast", "pra", "fg3m", "stl_blk", "fantasy_points"}


def _normalize_name(name: str) -> str:
    """Normalize a player name for matching.

    Identical logic to dk_props_service._normalize_name to ensure
    consistent cross-service name matching.
    """
    n = name.strip().lower()
    n = unicodedata.normalize("NFKD", n)
    n = "".join(c for c in n if not unicodedata.combining(c))
    n = re.sub(r"\b(jr\.?|sr\.?|iii|iv|ii)\s*$", "", n).strip()
    n = n.replace(".", "")
    n = re.sub(r"\s+", " ", n).strip()
    return n


_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def _looks_like_uuid(value: str) -> bool:
    """Return True if *value* looks like a UUID (common in UD internal IDs)."""
    return bool(_UUID_RE.match(value.strip()))


def _normalize_stat(stat_raw: str) -> str:
    """Map an Underdog stat name to our internal stat key."""
    key = stat_raw.strip().lower().replace(" ", "_").replace("-", "_")
    return UD_STAT_MAP.get(key, key)


class UnderdogApiService:
    """Scrapes Underdog Fantasy pick'em lines.

    Usage::

        service = UnderdogApiService()
        slate = service.get_pickem_lines("2026-02-19")
        for line in slate.lines:
            print(f"{line.player_name} {line.stat} {line.line}")
    """

    def __init__(self, base_url: Optional[str] = None, cache_ttl: int = CACHE_TTL):
        self._base_url = base_url or BASE_URL
        self._cache_ttl = cache_ttl
        # Cache: date-string -> UnderdogSlate
        self._cache: Dict[str, UnderdogSlate] = {}
        self._cache_timestamps: Dict[str, float] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_pickem_lines(
        self, game_date: Optional[str] = None
    ) -> UnderdogSlate:
        """Return all pick'em lines for the given date.

        Returns an :class:`UnderdogSlate` with ``lines`` populated.
        On failure, returns the cached data (if any) or an empty slate.
        """
        target = game_date or date_cls.today().isoformat()

        if self._is_cache_valid(target):
            return self._cache[target]

        try:
            slate = self._fetch_lines(target)
            if slate and slate.lines:
                self._cache[target] = slate
                self._cache_timestamps[target] = time.time()
                logger.info(
                    f"[UnderdogAPI] Fetched {len(slate.lines)} pick'em lines "
                    f"for {slate.player_count} players on {target}"
                )
            return slate
        except Exception as e:
            logger.error(f"[UnderdogAPI] Fetch failed for {target}: {e}")
            return self._cache.get(target, UnderdogSlate(game_date=target))

    def get_player_line(
        self,
        player_name: str,
        stat: str,
        game_date: Optional[str] = None,
    ) -> Optional[UnderdogPickemLine]:
        """Get a specific player's pick'em line for one stat."""
        slate = self.get_pickem_lines(game_date)
        norm = _normalize_name(player_name)
        stat_key = _normalize_stat(stat) if stat not in PICKEM_STATS else stat

        for line in slate.lines:
            if _normalize_name(line.player_name) == norm and line.stat == stat_key:
                return line
        return None

    def get_player_lines(
        self,
        player_name: str,
        game_date: Optional[str] = None,
    ) -> List[UnderdogPickemLine]:
        """Get all pick'em lines for a single player."""
        slate = self.get_pickem_lines(game_date)
        norm = _normalize_name(player_name)
        return [l for l in slate.lines if _normalize_name(l.player_name) == norm]

    # ------------------------------------------------------------------
    # Cache management
    # ------------------------------------------------------------------

    def _is_cache_valid(self, game_date: str) -> bool:
        if game_date not in self._cache:
            return False
        ts = self._cache_timestamps.get(game_date, 0.0)
        return (time.time() - ts) < self._cache_ttl

    def invalidate_cache(self, game_date: Optional[str] = None) -> None:
        """Clear cached data.  If *game_date* is ``None``, clear all."""
        if game_date:
            self._cache.pop(game_date, None)
            self._cache_timestamps.pop(game_date, None)
        else:
            self._cache.clear()
            self._cache_timestamps.clear()

    # ------------------------------------------------------------------
    # HTTP fetching
    # ------------------------------------------------------------------

    def _fetch_lines(self, game_date: str) -> UnderdogSlate:
        """Fetch pick'em lines from the Underdog API."""
        try:
            resp = resilient_get(
                self._base_url,
                group=APIGroup.UNDERDOG,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "application/json",
                },
            )
            data = resp.json()
        except httpx.HTTPStatusError as e:
            logger.warning(
                f"[UnderdogAPI] HTTP {e.response.status_code} from Underdog API"
            )
            return UnderdogSlate(game_date=game_date)
        except httpx.TimeoutException:
            logger.warning("[UnderdogAPI] Request timed out")
            return UnderdogSlate(game_date=game_date)
        except Exception as e:
            logger.warning(f"[UnderdogAPI] Request failed: {e}")
            return UnderdogSlate(game_date=game_date)

        return self._parse_response(data, game_date)

    # ------------------------------------------------------------------
    # Response parsing
    # ------------------------------------------------------------------

    def _parse_response(self, data: dict, game_date: str) -> UnderdogSlate:
        """Parse the Underdog API JSON into an :class:`UnderdogSlate`.

        The response has four top-level arrays: ``players``,
        ``appearances``, ``over_under_lines``, and ``games``.
        We join them together to build structured pick'em lines.
        """
        # ── Build lookup tables ────────────────────────────────────────
        players_raw: List[dict] = data.get("players", [])
        appearances_raw: List[dict] = data.get("appearances", [])
        lines_raw: List[dict] = data.get("over_under_lines", [])
        games_raw: List[dict] = data.get("games", [])

        # ── Diagnostic logging when arrays are missing ────────────────
        if not players_raw:
            logger.warning(
                f"[UnderdogAPI] No 'players' array in response. "
                f"Top-level keys: {list(data.keys())}"
            )
        if not lines_raw:
            logger.warning(
                f"[UnderdogAPI] No 'over_under_lines' array in response. "
                f"Top-level keys: {list(data.keys())}"
            )

        # player_id -> player info
        player_lookup: Dict[str, dict] = {}
        for p in players_raw:
            pid = str(p.get("id", ""))
            if pid:
                player_lookup[pid] = p

        # appearance_id -> appearance info (includes player_id, team_id)
        appearance_lookup: Dict[str, dict] = {}
        for a in appearances_raw:
            aid = str(a.get("id", ""))
            if aid:
                appearance_lookup[aid] = a

        # team_id -> team abbreviation (from multiple sources)
        team_lookup: Dict[str, str] = self._build_team_lookup(
            data, games_raw, appearances_raw, players_raw
        )

        # ── Parse over/under lines ─────────────────────────────────────
        parsed_lines: List[UnderdogPickemLine] = []
        seen_keys: set = set()  # deduplicate

        for ou_line in lines_raw:
            try:
                parsed = self._parse_single_line(
                    ou_line, player_lookup, appearance_lookup, team_lookup
                )
                if parsed is None:
                    continue
                # Deduplicate by player + stat
                dedup_key = f"{_normalize_name(parsed.player_name)}:{parsed.stat}"
                if dedup_key in seen_keys:
                    continue
                seen_keys.add(dedup_key)
                # Only keep stats we track
                if parsed.stat in PICKEM_STATS:
                    parsed_lines.append(parsed)
            except Exception as e:
                logger.debug(f"[UnderdogAPI] Skipping malformed line: {e}")
                continue

        # ── Log parse rate for debugging ─────────────────────────────
        total_attempted = len(lines_raw)
        total_parsed = len(parsed_lines)
        if total_attempted > 0 and total_parsed == 0:
            logger.error(
                f"[UnderdogAPI] Parsed 0/{total_attempted} lines. "
                f"API response structure may have changed. "
                f"Line keys: {list(lines_raw[0].keys()) if lines_raw else []}, "
                f"Player keys: {list(players_raw[0].keys()) if players_raw else []}"
            )
        elif total_attempted > 0 and total_parsed < total_attempted * 0.3:
            logger.warning(
                f"[UnderdogAPI] Low parse rate: {total_parsed}/{total_attempted}. "
                f"Check for API structure changes."
            )

        # ── Parse games ────────────────────────────────────────────────
        games: List[UnderdogGame] = []
        for g in games_raw:
            home = team_lookup.get(str(g.get("home_team_id", "")), "???")
            away = team_lookup.get(str(g.get("away_team_id", "")), "???")
            sched = g.get("scheduled_at", "")
            games.append(UnderdogGame(
                home_team=home,
                away_team=away,
                game_time=sched,
            ))

        # Count unique players
        unique_players = {_normalize_name(l.player_name) for l in parsed_lines}

        return UnderdogSlate(
            game_date=game_date,
            lines=parsed_lines,
            player_count=len(unique_players),
            games=games,
            fetched_at=datetime.now(timezone.utc).isoformat(),
        )

    def _parse_single_line(
        self,
        ou_line: dict,
        player_lookup: Dict[str, dict],
        appearance_lookup: Dict[str, dict],
        team_lookup: Dict[str, str],
    ) -> Optional[UnderdogPickemLine]:
        """Parse a single ``over_under_lines`` entry.

        Tries multiple structural patterns since the Underdog API is
        undocumented and has changed field names / nesting across versions.
        """
        # ── Extract appearance_id + stat (try multiple nesting patterns) ──
        over_under = ou_line.get("over_under", {})
        appearance_stat = over_under.get("appearance_stat", {})

        appearance_id = str(appearance_stat.get("appearance_id", ""))
        stat_raw = appearance_stat.get("stat", "")

        # Fallback: flat structure (some API versions)
        if not appearance_id:
            appearance_id = str(ou_line.get("appearance_id", ""))
        if not stat_raw:
            stat_raw = ou_line.get("stat", "") or ou_line.get("stat_type", "")

        # Fallback: appearance_stat directly on ou_line
        if not appearance_id:
            app_stat = ou_line.get("appearance_stat", {})
            appearance_id = str(app_stat.get("appearance_id", ""))
            stat_raw = stat_raw or app_stat.get("stat", "")

        if not appearance_id or not stat_raw:
            return None

        # ── Resolve appearance → player ──────────────────────────────────
        appearance = appearance_lookup.get(appearance_id, {})
        player_id = str(appearance.get("player_id", ""))
        player = player_lookup.get(player_id, {})

        if not player:
            return None

        # ── Extract player name (resilient to field name changes) ────────
        player_name = self._extract_player_name(player)

        if not player_name or _looks_like_uuid(player_name):
            logger.debug(
                f"[UnderdogAPI] Skipping line: invalid player name "
                f"{player_name!r}, keys: {list(player.keys())}"
            )
            return None

        # ── Resolve team ─────────────────────────────────────────────────
        team_id = str(
            appearance.get("team_id", "") or player.get("team_id", "")
        )
        team_abbr = team_lookup.get(team_id, "???")

        # ── Parse the line value ─────────────────────────────────────────
        line_value = self._extract_line_value(ou_line)
        if line_value is None:
            return None

        # ── Normalize stat ───────────────────────────────────────────────
        stat_key = _normalize_stat(stat_raw)

        # ── Resolve opponent (from game data if available) ───────────────
        opponent = ""  # Would need game-level join; simplified for now

        # ── Extract position (try multiple fields) ───────────────────────
        position = (
            player.get("position_id", "")
            or player.get("position", "")
            or player.get("sport_position", "")
        )
        # Don't show UUID-like position values
        if _looks_like_uuid(str(position)):
            position = ""

        return UnderdogPickemLine(
            player_name=player_name,
            team_abbreviation=team_abbr,
            stat=stat_key,
            line=line_value,
            appearance_id=appearance_id,
            player_id=player_id,
            opponent=opponent,
            position=str(position),
            image_url=player.get("image_url"),
        )

    def _extract_player_name(self, player: dict) -> str:
        """Extract player name from a player dict, trying multiple patterns.

        Underdog's undocumented API has changed field names across versions.
        We try all known patterns to be resilient to future changes.
        """
        # Pattern 1: first_name + last_name (original observed format)
        first = player.get("first_name", "") or ""
        last = player.get("last_name", "") or ""
        if first and last and not _looks_like_uuid(first):
            return f"{first} {last}".strip()

        # Pattern 2: camelCase variants
        first = player.get("firstName", "") or ""
        last = player.get("lastName", "") or ""
        if first and last and not _looks_like_uuid(first):
            return f"{first} {last}".strip()

        # Pattern 3: Single display_name / full_name / name field
        for field in ("display_name", "displayName", "full_name",
                       "fullName", "name"):
            val = player.get(field, "")
            if val and len(val) > 2 and not _looks_like_uuid(val):
                return val.strip()

        # Pattern 4: Partial — only first OR last name available
        if first and not _looks_like_uuid(first):
            return first.strip()
        if last and not _looks_like_uuid(last):
            return last.strip()

        # Nothing worked — log for debugging
        logger.warning(
            f"[UnderdogAPI] Could not extract player name. "
            f"Keys: {list(player.keys())}, "
            f"Sample: {{{', '.join(f'{k}: {str(v)[:30]!r}' for k, v in list(player.items())[:5])}}}"
        )
        return ""

    def _extract_line_value(self, ou_line: dict) -> Optional[float]:
        """Extract the numerical O/U line from an over_under_lines entry.

        The line can appear in:
        - ``ou_line["stat_value"]``
        - ``ou_line["over_under"]["appearance_stat"]["stat_value"]``
        - ``ou_line["options"][0]["title"]``  (as a string like "25.5")
        """
        # Try stat_value at top level
        sv = ou_line.get("stat_value")
        if sv is not None:
            try:
                return float(sv)
            except (ValueError, TypeError):
                pass

        # Try nested under over_under
        over_under = ou_line.get("over_under", {})
        sv2 = over_under.get("appearance_stat", {}).get("stat_value")
        if sv2 is not None:
            try:
                return float(sv2)
            except (ValueError, TypeError):
                pass

        # Try from options title
        options = ou_line.get("options", [])
        for opt in options:
            title = str(opt.get("title", ""))
            # Title might be "Higher 25.5" or just "25.5"
            match = re.search(r"(\d+\.?\d*)", title)
            if match:
                try:
                    return float(match.group(1))
                except ValueError:
                    pass

        return None

    def _build_team_lookup(
        self,
        data: dict,
        games_raw: List[dict],
        appearances_raw: List[dict],
        players_raw: List[dict],
    ) -> Dict[str, str]:
        """Build team_id → abbreviation mapping from available data.

        Underdog API may include team info in different places depending
        on the endpoint version.  We try multiple sources.
        """
        lookup: Dict[str, str] = {}

        # ── Source 1: top-level "teams" array (newer API versions) ────────
        teams_raw: List[dict] = data.get("teams", [])
        for t in teams_raw:
            tid = str(t.get("id", ""))
            abbr = (
                t.get("abbrev", "")
                or t.get("abbreviation", "")
                or t.get("abbr", "")
                or t.get("short_name", "")
                or t.get("code", "")
            )
            if tid and abbr:
                lookup[tid] = abbr.upper()

        # ── Source 2: from games (nested or flat team objects) ────────────
        for g in games_raw:
            for key in ("home_team", "away_team"):
                team = g.get(key, {})
                if isinstance(team, dict):
                    tid = str(team.get("id", ""))
                    abbr = (
                        team.get("abbrev", "")
                        or team.get("abbreviation", "")
                        or team.get("abbr", "")
                        or team.get("short_name", "")
                    )
                    if tid and abbr:
                        lookup.setdefault(tid, abbr.upper())
            # Also try flat keys (home_team_id, home_team_abbrev, etc.)
            for prefix in ("home_team_", "away_team_"):
                tid = str(g.get(f"{prefix}id", ""))
                abbr = (
                    g.get(f"{prefix}abbrev", "")
                    or g.get(f"{prefix}abbreviation", "")
                    or g.get(f"{prefix}abbr", "")
                )
                if tid and abbr:
                    lookup.setdefault(tid, abbr.upper())

        # ── Source 3: from players ────────────────────────────────────────
        for p in players_raw:
            tid = str(p.get("team_id", ""))
            abbr = (
                p.get("team_abbreviation", "")
                or p.get("team_abbrev", "")
                or p.get("teamAbbreviation", "")
                or p.get("team_abbr", "")
            )
            if tid and abbr and tid not in lookup:
                lookup[tid] = abbr.upper()

        # ── Source 4: from appearances ────────────────────────────────────
        for a in appearances_raw:
            tid = str(a.get("team_id", ""))
            abbr = (
                a.get("team_abbreviation", "")
                or a.get("team_abbrev", "")
                or a.get("teamAbbreviation", "")
            )
            if tid and abbr and tid not in lookup:
                lookup[tid] = abbr.upper()

        if not lookup and (games_raw or players_raw):
            logger.warning(
                f"[UnderdogAPI] Could not build team lookup. "
                f"Game keys: {list(games_raw[0].keys()) if games_raw else []}, "
                f"Player keys: {list(players_raw[0].keys()) if players_raw else []}, "
                f"Top-level keys: {list(data.keys())}"
            )

        return lookup
