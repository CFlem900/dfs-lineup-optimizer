"""Service for fetching DraftKings slate (DraftGroup) data.

Uses the public DraftKings API to determine which games belong to
which DFS slates (Early, Main, Night, etc.) instead of guessing
from NBA API data.

Endpoints used:
    - GET https://www.draftkings.com/lobby/getcontests?sport=NBA
        Returns all current contests; we extract unique Classic
        (gameTypeId=70) DraftGroup IDs from the Contests array.

    - GET https://api.draftkings.com/draftgroups/v1/{draftGroupId}
        Returns a DraftGroup's metadata including the actual games
        with UTC start times, team IDs, and descriptions.
"""

import json
import logging
import os
import time
import re
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import httpx

from app.services.http_resilience import resilient_get, APIGroup
from app.sports import get_config

logger = logging.getLogger(__name__)

# DK API endpoints
# Legacy module-level constants — preserved for backward compat with tests
# and any external imports. The values mirror the registry; future sports
# (NFL, MLB) should not add new constants here, just register a SportConfig.
DK_LOBBY_URL = get_config("nba").dk_lobby_url
DK_CBB_LOBBY_URL = get_config("cbb").dk_lobby_url
DK_DRAFTGROUP_URL = "https://api.draftkings.com/draftgroups/v1/{dg_id}"

def _get_lobby_url(sport: str = "nba") -> str:
    """Return the DK lobby URL for the given sport."""
    return get_config(sport).dk_lobby_url

# Classic DFS game type IDs — differ by sport.
# Legacy aliases retained for any caller that still imports them directly;
# new code should call ``_get_classic_game_type_id(sport)`` or read the
# tuple from ``get_config(sport).dk_classic_game_type_ids``.
CLASSIC_GAME_TYPE_ID = get_config("nba").dk_classic_game_type_ids[0]
CBB_CLASSIC_GAME_TYPE_ID = get_config("cbb").dk_classic_game_type_ids[-1]

def _get_classic_game_type_id(sport: str = "nba") -> int:
    """Return the *primary* Classic DraftGroup gameTypeId for the given sport.

    For sports that have multiple legal Classic IDs (CBB has both 70 and 98),
    this returns the canonical/primary one — the tuple-based check belongs in
    the validation path, not here.
    """
    ids = get_config(sport).dk_classic_game_type_ids
    if not ids:
        raise ValueError(f"Sport {sport!r} has no Classic game type IDs configured")
    # CBB's primary (canonical) Classic ID is 98 even though 70 also appears
    # historically; preserve that by returning the LAST entry.
    return ids[-1] if sport == "cbb" else ids[0]

# HTTP settings
REQUEST_TIMEOUT = 15
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# DK team ID → NBA team abbreviation mapping.
# Built from observed DraftKings API responses.
DK_TEAM_ID_TO_NBA_ABBR: Dict[int, str] = {
    1: "ATL",
    2: "BOS",
    3: "NOP",
    4: "CHI",
    5: "CLE",
    6: "DAL",
    7: "DEN",
    8: "DET",
    9: "GSW",
    10: "HOU",
    11: "IND",
    12: "LAC",
    13: "LAL",
    14: "MIA",
    15: "MIL",
    16: "MIN",
    17: "BKN",
    18: "NYK",
    19: "ORL",
    20: "PHI",
    21: "PHX",
    22: "POR",
    23: "SAC",
    24: "SAS",
    25: "OKC",
    26: "UTA",
    27: "WAS",
    28: "TOR",
    29: "MEM",
    30: "CHA",
    5312: "CHA",  # alternate CHA id seen in DK data
}

# Reverse mapping: NBA abbreviation → DK team ID
NBA_ABBR_TO_DK_TEAM_ID: Dict[str, int] = {
    v: k for k, v in DK_TEAM_ID_TO_NBA_ABBR.items() if k != 5312
}


class DKSlateInfo:
    """Parsed DraftKings slate (DraftGroup) data."""

    def __init__(
        self,
        draft_group_id: int,
        name: str,
        label: str,
        game_count: int,
        min_start_utc: datetime,
        max_start_utc: datetime,
        games: List[Dict],
    ):
        self.draft_group_id = draft_group_id
        self.name = name
        self.label = label
        self.game_count = game_count
        self.min_start_utc = min_start_utc
        self.max_start_utc = max_start_utc
        self.games = games  # list of {away_abbr, home_abbr, start_utc, description}


class DKSlateService:
    """Fetches real DraftKings slate groupings for today's NBA games."""

    # How long past the last game start a slate remains visible for late-swap.
    LATE_SWAP_WINDOW_HOURS = 3

    # File path for persisting sticky cache across server restarts.
    _STICKY_CACHE_DIR = Path(__file__).resolve().parent.parent.parent / "cache"
    _STICKY_CACHE_FILE = _STICKY_CACHE_DIR / "dk_slate_sticky.json"

    def __init__(self):
        # Cache keyed by "{sport}:{date}" so NBA and CBB don't collide.
        self._cache: Dict[str, List[DKSlateInfo]] = {}
        self._cache_timestamps: Dict[str, float] = {}
        self._cache_ttl = 300  # 5 min cache

        # ── Sticky cache ─────────────────────────────────────────────
        # DK removes DraftGroups from the lobby once they lock (~15 min
        # before tip-off).  The sticky cache preserves every DraftGroup
        # we've ever seen for a given date so that in-progress slates
        # remain visible for late-swap.  Entries expire when
        # max_start_utc + LATE_SWAP_WINDOW_HOURS passes.
        # Keyed by "{sport}:{date}" → {draft_group_id: DKSlateInfo}
        self._sticky_cache: Dict[str, Dict[int, DKSlateInfo]] = {}

        # Load persisted sticky cache from disk (survives server restarts)
        self._load_sticky_cache()

    # ── Sticky cache file persistence ─────────────────────────────────

    def _load_sticky_cache(self):
        """Load sticky cache from disk on startup."""
        try:
            if not self._STICKY_CACHE_FILE.exists():
                return
            raw = json.loads(self._STICKY_CACHE_FILE.read_text(encoding="utf-8"))
            now_utc = datetime.now(timezone.utc)
            cutoff = now_utc - timedelta(hours=self.LATE_SWAP_WINDOW_HOURS)
            loaded = 0
            for key, entries in raw.items():
                self._sticky_cache[key] = {}
                for dg_id_str, info in entries.items():
                    dg_id = int(dg_id_str)
                    max_start = self._parse_dk_datetime(info.get("max_start_utc", ""))
                    if max_start and max_start < cutoff:
                        continue  # already expired
                    min_start = self._parse_dk_datetime(info.get("min_start_utc", ""))
                    games = []
                    for g in info.get("games", []):
                        g_copy = dict(g)
                        if "start_utc" in g_copy and g_copy["start_utc"]:
                            g_copy["start_utc"] = self._parse_dk_datetime(g_copy["start_utc"])
                        games.append(g_copy)
                    slate = DKSlateInfo(
                        draft_group_id=dg_id,
                        name=info.get("name", ""),
                        label=info.get("label", ""),
                        game_count=info.get("game_count", len(games)),
                        min_start_utc=min_start or now_utc,
                        max_start_utc=max_start or now_utc,
                        games=games,
                    )
                    self._sticky_cache[key][dg_id] = slate
                    loaded += 1
                # Remove empty keys
                if not self._sticky_cache[key]:
                    del self._sticky_cache[key]
            if loaded:
                logger.info(f"[DKSlate] Loaded {loaded} slates from sticky cache file")
        except Exception as e:
            logger.warning(f"[DKSlate] Failed to load sticky cache: {e}")

    def _save_sticky_cache(self):
        """Persist sticky cache to disk so it survives server restarts."""
        try:
            self._STICKY_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            data: Dict[str, Dict[str, dict]] = {}
            for key, entries in self._sticky_cache.items():
                data[key] = {}
                for dg_id, sl in entries.items():
                    games_out = []
                    for g in sl.games:
                        g_copy = dict(g)
                        if "start_utc" in g_copy and g_copy["start_utc"]:
                            g_copy["start_utc"] = g_copy["start_utc"].isoformat()
                        games_out.append(g_copy)
                    data[key][str(dg_id)] = {
                        "name": sl.name,
                        "label": sl.label,
                        "game_count": sl.game_count,
                        "min_start_utc": sl.min_start_utc.isoformat() if sl.min_start_utc else "",
                        "max_start_utc": sl.max_start_utc.isoformat() if sl.max_start_utc else "",
                        "games": games_out,
                    }
            self._STICKY_CACHE_FILE.write_text(
                json.dumps(data, indent=2, default=str),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"[DKSlate] Failed to save sticky cache: {e}")

    def _cache_key(self, target_date: str, sport: str) -> str:
        return f"{sport}:{target_date}"

    def _is_cache_valid(self, target_date: str, sport: str = "nba") -> bool:
        key = self._cache_key(target_date, sport)
        if key not in self._cache_timestamps:
            return False
        return (time.time() - self._cache_timestamps[key]) < self._cache_ttl

    def get_slates(self, target_date: str = None, sport: str = "nba") -> List[DKSlateInfo]:
        """Get DK Classic slates for a given date (Early, Main, Night, etc.).

        Args:
            target_date: Date string YYYY-MM-DD. Defaults to today.
            sport: "nba" or "cbb" — determines which DK lobby to query.

        Returns a list of DKSlateInfo objects, one per Classic DraftGroup.
        Falls back to empty list if DK API is unreachable.
        """
        target = target_date or date.today().isoformat()
        key = self._cache_key(target, sport)

        if self._is_cache_valid(target, sport) and key in self._cache:
            return self._cache[key]

        try:
            slates = self._fetch_slates(target, sport=sport)
            self._cache[key] = slates
            self._cache_timestamps[key] = time.time()
            return slates
        except Exception as e:
            logger.error(f"DK slate fetch failed: {e}")
            return self._cache.get(key, [])

    def _fetch_slates(self, target_date: str, sport: str = "nba") -> List[DKSlateInfo]:
        """Fetch Classic DraftGroups from DK lobby and resolve their games.

        Merges freshly-fetched slates with the sticky cache so that
        DraftGroups which DK has removed (because they locked) are
        still returned if their late-swap window hasn't expired.
        The sticky cache is persisted to disk so it survives restarts.
        """
        now_utc = datetime.now(timezone.utc)
        key = self._cache_key(target_date, sport)

        # Step 1: Get unique Classic DraftGroup IDs from lobby
        classic_dg_ids = self._get_classic_draft_group_ids(sport=sport)
        if classic_dg_ids:
            logger.info(f"Found {len(classic_dg_ids)} Classic DraftGroups: {classic_dg_ids}")

        # Step 2: Fetch detail for each DraftGroup
        fresh_slates: List[DKSlateInfo] = []
        for dg_id, sd_string in (classic_dg_ids or []):
            try:
                slate = self._fetch_draft_group_detail(dg_id, sd_string)
                if slate:
                    fresh_slates.append(slate)
            except Exception as e:
                logger.warning(f"Failed to fetch DraftGroup {dg_id}: {e}")

        # Step 2b: Drop single-game DraftGroups — these are occasionally
        # tagged as Classic (gameTypeId=70) but are really Showdown
        # variants.  The user strictly plays multi-game Classic slates.
        if fresh_slates:
            before_count = len(fresh_slates)
            fresh_slates = [s for s in fresh_slates if s.game_count >= 2]
            dropped = before_count - len(fresh_slates)
            if dropped:
                logger.info(
                    f"[DKSlate] Dropped {dropped} single-game Classic DG(s) "
                    f"(likely Showdown mislabel)"
                )

        # Step 3: Filter to the target date using actual game start times
        matching_fresh = self._filter_slates_for_date(fresh_slates, target_date)

        # Step 4: Merge into sticky cache — add any newly-seen DraftGroups
        if key not in self._sticky_cache:
            self._sticky_cache[key] = {}
        dirty = False
        for sl in matching_fresh:
            if sl.draft_group_id not in self._sticky_cache[key]:
                dirty = True
            self._sticky_cache[key][sl.draft_group_id] = sl

        # Step 4b: If lobby returned 0 matching slates for the target date,
        # try to re-fetch known DG IDs from the sticky cache (they may
        # still be accessible via the DK DraftGroup API even after
        # disappearing from the lobby).
        if not matching_fresh and key in self._sticky_cache and self._sticky_cache[key]:
            stale_ids = list(self._sticky_cache[key].keys())
            logger.info(
                f"[DKSlate] Lobby empty for {target_date} — re-fetching "
                f"{len(stale_ids)} sticky DG IDs: {stale_ids}"
            )
            for dg_id in stale_ids:
                try:
                    refreshed = self._fetch_draft_group_detail(dg_id, "")
                    if refreshed:
                        self._sticky_cache[key][dg_id] = refreshed
                except Exception as e:
                    logger.debug(f"[DKSlate] Re-fetch DG {dg_id} failed: {e}")

        # Step 5: Build final list from sticky cache, pruning expired entries
        cutoff = now_utc - timedelta(hours=self.LATE_SWAP_WINDOW_HOURS)
        merged: List[DKSlateInfo] = []
        expired_ids = []
        for dg_id, sl in self._sticky_cache.get(key, {}).items():
            if sl.max_start_utc and sl.max_start_utc < cutoff:
                expired_ids.append(dg_id)
                continue
            merged.append(sl)
        for dg_id in expired_ids:
            del self._sticky_cache[key][dg_id]
            dirty = True

        if expired_ids:
            logger.info(
                f"[DKSlate] Pruned {len(expired_ids)} expired slates from "
                f"sticky cache: {expired_ids}"
            )

        # Persist to disk when anything changed
        if dirty or expired_ids:
            self._save_sticky_cache()

        fresh_dg_ids = {sl.draft_group_id for sl in matching_fresh}
        sticky_only = [sl for sl in merged if sl.draft_group_id not in fresh_dg_ids]
        if sticky_only:
            logger.info(
                f"[DKSlate] Sticky cache preserved {len(sticky_only)} "
                f"locked/live slates: {[sl.draft_group_id for sl in sticky_only]}"
            )

        if not merged:
            if not classic_dg_ids:
                logger.info("No Classic DraftGroups found in DK lobby (and sticky cache empty)")
            return []

        # Step 6: Sort by earliest game time and assign names
        merged.sort(key=lambda s: s.min_start_utc)
        self._assign_slate_names(merged)

        return merged

    def _filter_slates_for_date(
        self, slates: List[DKSlateInfo], target_date: str
    ) -> List[DKSlateInfo]:
        """Filter slates to only include those with games on the target date.

        NBA games that start in the evening ET are actually in the next
        UTC day. For example, a 7:00 PM ET game on Feb 10 is Feb 11 at
        00:00 UTC. So we check if the game's ET date matches the target.

        Uses proper US/Eastern timezone (handles EST/EDT transitions).
        """
        target = date.fromisoformat(target_date)

        try:
            import zoneinfo
            eastern = zoneinfo.ZoneInfo("America/New_York")
        except Exception:
            eastern = None

        result = []
        for slate in slates:
            has_matching_game = False
            for game in slate.games:
                start_utc = game.get("start_utc")
                if start_utc:
                    if eastern is not None:
                        game_date_et = start_utc.astimezone(eastern).date()
                    else:
                        # Fallback: hardcoded EST (UTC-5)
                        game_date_et = (start_utc + timedelta(hours=-5)).date()
                    if game_date_et == target:
                        has_matching_game = True
                        break

            if has_matching_game:
                result.append(slate)
            else:
                logger.info(
                    f"[DKSlate] Filtered out DG {slate.draft_group_id} — "
                    f"no games on target date {target} "
                    f"(min_start={slate.min_start_utc})"
                )

        if not result and slates:
            logger.info(
                f"No slates match target date ({target}). "
                f"Slate dates: {[s.min_start_utc.date() for s in slates]}"
            )

        return result

    def _get_classic_draft_group_ids(self, sport: str = "nba") -> List[Tuple[int, str]]:
        """Extract unique Classic DraftGroup IDs from lobby.

        Filters by the full tuple of legal Classic gameTypeIds registered
        for the sport (``cfg.dk_classic_game_type_ids``). NBA = (70,),
        CBB = (70, 98), NFL = (1,), MLB = (1,). Some sports historically
        have used multiple IDs (CBB has both 70 and 98 at different
        times) — accepting any of them keeps the slate fetch resilient.

        Returns list of (draftGroupId, sdstring) tuples.
        """
        from app.sports import get_config as _get_sport_cfg
        legal_gt: Tuple[int, ...] = _get_sport_cfg(sport).dk_classic_game_type_ids
        legal_set = set(legal_gt)
        lobby_url = _get_lobby_url(sport)
        try:
            resp = resilient_get(
                lobby_url,
                group=APIGroup.DRAFTKINGS,
                headers={"User-Agent": USER_AGENT},
            )
            data = resp.json()
        except Exception as e:
            logger.error(f"DK lobby request failed: {e}")
            return []

        contests = data.get("Contests", [])
        if not contests:
            # Try alternate key casing
            contests = data.get("contests", [])

        # Also check for DraftGroups array at root level
        draft_groups_root = data.get("DraftGroups", [])

        seen: Dict[int, str] = {}

        # Extract from Contests array
        for contest in contests:
            game_type_id = contest.get("gameTypeId") or contest.get("GameTypeId")
            dg_id = contest.get("dg") or contest.get("DraftGroupId")
            sd_string = contest.get("sdstring") or contest.get("StartDateString", "")

            if game_type_id in legal_set and dg_id and dg_id not in seen:
                seen[dg_id] = sd_string

        # Also extract from DraftGroups array if present
        for dg in draft_groups_root:
            game_type_id = dg.get("GameTypeId") or dg.get("gameTypeId")
            dg_id = dg.get("DraftGroupId") or dg.get("draftGroupId")
            sd_string = dg.get("ContestStartTimeSuffix", "")

            if game_type_id in legal_set and dg_id and dg_id not in seen:
                seen[dg_id] = sd_string

        logger.info(
            f"[{sport.upper()}] Found {len(seen)} Classic (gameType in {sorted(legal_set)}) "
            f"DraftGroups: {list(seen.keys())}"
        )

        # Return all Classic DraftGroups — date filtering happens later
        # in _filter_slates_for_date() using actual game start times.
        return list(seen.items())

    def _fetch_draft_group_detail(
        self, dg_id: int, sd_string: str
    ) -> Optional[DKSlateInfo]:
        """Fetch a single DraftGroup's games and metadata."""
        url = DK_DRAFTGROUP_URL.format(dg_id=dg_id)
        try:
            resp = resilient_get(
                url,
                group=APIGroup.DRAFTKINGS,
                headers={"User-Agent": USER_AGENT},
            )
            data = resp.json()
        except Exception as e:
            logger.error(f"DK DraftGroup {dg_id} request failed: {e}")
            return None

        dg = data.get("draftGroup", {})
        if not dg:
            return None

        raw_games = dg.get("games", [])
        min_start_str = dg.get("minStartTime", "")
        max_start_str = dg.get("maxStartTime", "")

        min_start = self._parse_dk_datetime(min_start_str)
        max_start = self._parse_dk_datetime(max_start_str)

        if not min_start or not max_start:
            logger.warning(f"DG {dg_id}: could not parse start times")
            return None

        games = []
        for game in raw_games:
            description = game.get("description", "")
            away_abbr, home_abbr = self._parse_matchup(description)
            start_str = game.get("startDate", "")
            start_utc = self._parse_dk_datetime(start_str)

            games.append({
                "dk_game_id": game.get("gameId"),
                "away_team_dk_id": game.get("awayTeamId"),
                "home_team_dk_id": game.get("homeTeamId"),
                "away_abbr": away_abbr,
                "home_abbr": home_abbr,
                "start_utc": start_utc,
                "description": description,
                "location": game.get("location", ""),
                "status": game.get("status", ""),
            })

        return DKSlateInfo(
            draft_group_id=dg_id,
            name="",  # will be assigned in _assign_slate_names
            label=sd_string,
            game_count=len(games),
            min_start_utc=min_start,
            max_start_utc=max_start,
            games=games,
        )

    def _assign_slate_names(self, slates: List[DKSlateInfo]):
        """Assign human-readable names to slates based on their position and times.

        DK naming conventions for Classic slates:
        - If only 1 Classic slate → "Main"
        - If 2 slates → "Main" (earlier/bigger), "Night" (later/smaller)
        - If 3+ slates → "Early" (first), "Main" (biggest/middle), "Night" (last)

        We also use ET start times in labels.
        """
        if not slates:
            return

        try:
            import zoneinfo
            eastern = zoneinfo.ZoneInfo("America/New_York")
        except Exception:
            eastern = None

        def _to_et(utc_dt):
            if eastern is not None:
                return utc_dt.astimezone(eastern)
            return utc_dt + timedelta(hours=-5)

        if len(slates) == 1:
            s = slates[0]
            start_et = _to_et(s.min_start_utc)
            s.name = "Main"
            s.label = f"Main ({self._format_time_et(start_et)}) \u2014 {s.game_count} games"
            return

        if len(slates) == 2:
            # Earlier one is Main, later is Night (unless the earlier one
            # has fewer games, in which case it's Early and the later is Main)
            s0, s1 = slates[0], slates[1]
            if s0.game_count < s1.game_count:
                s0.name = "Early"
                s1.name = "Main"
            else:
                s0.name = "Main"
                s1.name = "Night"

            for s in slates:
                start_et = _to_et(s.min_start_utc)
                s.label = f"{s.name} ({self._format_time_et(start_et)}) \u2014 {s.game_count} games"
            return

        # 3+ slates
        # Find the one with the most games — that's Main
        max_count = max(s.game_count for s in slates)
        main_idx = next(i for i, s in enumerate(slates) if s.game_count == max_count)

        for i, s in enumerate(slates):
            if i < main_idx:
                s.name = "Early" if i == 0 else f"Early {i + 1}"
            elif i == main_idx:
                s.name = "Main"
            else:
                s.name = "Night" if i == len(slates) - 1 else f"Late {i - main_idx}"

            start_et = _to_et(s.min_start_utc)
            s.label = f"{s.name} ({self._format_time_et(start_et)}) \u2014 {s.game_count} games"

    @staticmethod
    def _format_time_et(dt: datetime) -> str:
        """Format a datetime to '7:00 PM ET' style, cross-platform."""
        hour = dt.hour % 12 or 12
        minute = dt.strftime("%M")
        ampm = "AM" if dt.hour < 12 else "PM"
        return f"{hour}:{minute} {ampm} ET"

    @staticmethod
    def _parse_dk_datetime(dt_str: str) -> Optional[datetime]:
        """Parse DK ISO datetime like '2026-02-12T00:30:00.0000000Z' to datetime."""
        if not dt_str:
            return None
        try:
            # Strip trailing Z and fractional seconds precision beyond 6 digits
            clean = re.sub(r"\.(\d{6})\d*Z$", r".\1+00:00", dt_str)
            if clean.endswith("Z"):
                clean = clean[:-1] + "+00:00"
            return datetime.fromisoformat(clean)
        except (ValueError, TypeError):
            try:
                # Fallback: strip everything after seconds
                clean = dt_str[:19]
                return datetime.fromisoformat(clean).replace(tzinfo=timezone.utc)
            except Exception:
                return None

    @staticmethod
    def _parse_matchup(description: str) -> Tuple[str, str]:
        """Parse 'CHI @ BOS' into ('CHI', 'BOS')."""
        if not description:
            return ("???", "???")
        parts = description.split("@")
        if len(parts) == 2:
            return (parts[0].strip(), parts[1].strip())
        return ("???", "???")

    def get_game_team_abbrs_in_slate(
        self, slate: DKSlateInfo
    ) -> List[Tuple[str, str]]:
        """Return list of (away_abbr, home_abbr) for each game in a slate."""
        result = []
        for game in slate.games:
            away = game.get("away_abbr", "???")
            home = game.get("home_abbr", "???")
            # Also try DK team ID mapping if abbreviation parsing failed
            if away == "???":
                dk_away_id = game.get("away_team_dk_id")
                away = DK_TEAM_ID_TO_NBA_ABBR.get(dk_away_id, "???")
            if home == "???":
                dk_home_id = game.get("home_team_dk_id")
                home = DK_TEAM_ID_TO_NBA_ABBR.get(dk_home_id, "???")
            result.append((away, home))
        return result
