"""BALLDONTLIE API client for reliable NBA data.

Provides roster, game log, and schedule data as a resilient alternative
to ``stats.nba.com``.  Used by ``NBAMultiSourceService`` as the primary
live data source (before falling back to ``nba_api``).

Key advantages over stats.nba.com:
  - Stable uptime, no anti-scraping throttling
  - Bulk endpoints (2-3 calls vs 15+ per team)
  - Cursor-based pagination, clean JSON responses

API docs: https://docs.balldontlie.io
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Dict, List, Optional, Tuple

from app.config import get_settings
from app.services.http_resilience import APIGroup, resilient_get
from app.utils.helpers import normalize_player_name

logger = logging.getLogger(__name__)

settings = get_settings()


class BallDontLieService:
    """Client for the BALLDONTLIE NBA API.

    Features:
      - Rate limiter (configurable via ``balldontlie_rate_limit`` setting)
      - Circuit breaker via ``http_resilience.APIGroup.BALLDONTLIE``
      - Cursor-based pagination for bulk fetches
      - NBA.com ↔ BDL team ID mapping (lazy-loaded, cached forever)
      - Game-log format translation (BDL lowercase → NBA uppercase)
    """

    BASE_URL = "https://api.balldontlie.io/v1"
    BASE_URL_V2 = "https://api.balldontlie.io/v2"

    # Limit how many teams can fetch BDL data concurrently.
    # Without this, 18+ concurrent rotation builds overwhelm BDL's
    # 600 RPM rate limit and trigger cascading 429 → circuit breaker open.
    _team_fetch_semaphore = threading.Semaphore(3)

    def __init__(self):
        self._api_key: str = settings.balldontlie_api_key
        self._rate_limit_rpm: int = settings.balldontlie_rate_limit
        # Use 80% of the configured limit as headroom against 429s.
        effective_rpm = int(self._rate_limit_rpm * 0.80)
        self._min_interval: float = 60.0 / max(effective_rpm, 1)
        self._last_request_time: float = 0.0
        self._lock = threading.Lock()
        self._backoff_until: float = 0.0  # 429-aware global pause

        # Rate limiter observability
        self._request_count: int = 0
        self._minute_start: float = time.time()
        self._requests_this_minute: int = 0

        # Team mapping: NBA.com ID ↔ BDL ID (lazy-loaded on first use)
        self._nba_to_bdl_team: Dict[int, int] = {}
        self._bdl_to_nba_team: Dict[int, int] = {}
        self._bdl_team_abbrs: Dict[int, str] = {}  # BDL team ID → abbreviation
        self._teams_loaded: bool = False
        self._team_lock = threading.Lock()

        # In-memory BDL player ID cache: "normalized_name:TEAM" → BDL player dict.
        # Survives across requests within the same process.  Populated from
        # search_player() results and BDL roster fetches.
        self._player_name_cache: Dict[str, dict] = {}
        self._player_cache_lock = threading.Lock()

    # ── Properties ────────────────────────────────────────────────────

    @property
    def is_available(self) -> bool:
        """True if an API key is configured."""
        return bool(self._api_key)

    # ── Internal HTTP helpers ─────────────────────────────────────────

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": self._api_key}

    def _rate_limit(self):
        """Enforce rate limiting between API calls (thread-safe).

        Sleeps INSIDE the lock so only one thread proceeds at a time.
        This prevents the burst-on-wake bug where multiple threads
        compute overlapping wait times, release the lock, and all
        fire requests simultaneously after waking from sleep.

        Also respects a global ``_backoff_until`` timestamp set by
        ``notify_429()`` — when any thread receives a 429, ALL threads
        pause until the backoff expires.
        """
        with self._lock:
            now = time.time()

            # 429-aware global pause: if a 429 was received recently,
            # wait until the backoff period expires.
            if now < self._backoff_until:
                backoff_wait = self._backoff_until - now
                time.sleep(backoff_wait)
                now = time.time()

            elapsed = now - self._last_request_time
            if elapsed < self._min_interval:
                wait = self._min_interval - elapsed
                time.sleep(wait)

            self._last_request_time = time.time()

            # Track requests per minute for observability
            if now - self._minute_start >= 60:
                if self._requests_this_minute > 0:
                    logger.info(
                        f"[BDL] Rate: {self._requests_this_minute} requests "
                        f"in last minute (limit: {self._rate_limit_rpm})"
                    )
                self._minute_start = now
                self._requests_this_minute = 0
            self._requests_this_minute += 1
            self._request_count += 1

    def notify_429(self, retry_after: float = 5.0):
        """Signal that a 429 was received — pause ALL threads.

        Called from ``_get()`` when resilient_get raises after
        exhausting retries on a 429.  Sets a global backoff timestamp
        so subsequent ``_rate_limit()`` calls will wait.
        """
        with self._lock:
            new_backoff = time.time() + retry_after
            if new_backoff > self._backoff_until:
                self._backoff_until = new_backoff
                logger.warning(
                    "[BDL] 429 received - global pause for %.1fs",
                    retry_after,
                )

    def _get(self, path: str) -> dict:
        """Make an authenticated GET request with rate limiting + circuit breaker.

        ``path`` should include query params, e.g. ``/players?team_ids[]=5``.

        On 429 (after resilient_get exhausts its retries), signals a
        global pause via ``notify_429()`` so other threads back off,
        then re-raises so the caller can handle or propagate.
        """
        self._rate_limit()
        url = f"{self.BASE_URL}{path}"
        try:
            resp = resilient_get(
                url,
                group=APIGroup.BALLDONTLIE,
                headers=self._headers(),
            )
            return resp.json()
        except Exception as e:
            # If it was a 429, signal all threads to pause
            import httpx
            if isinstance(e, httpx.HTTPStatusError) and e.response.status_code == 429:
                # Parse Retry-After header if available
                retry_after = float(e.response.headers.get("Retry-After", "10"))
                self.notify_429(min(retry_after, 30.0))
            raise

    def _get_all_pages(
        self, path: str, *, max_pages: int = 10
    ) -> List[dict]:
        """Fetch all pages using cursor-based pagination.

        ``path`` should already include query params (with ``per_page=100``).
        Subsequent pages append ``&cursor=X``.
        """
        all_data: List[dict] = []
        cursor: Optional[int] = None

        for _ in range(max_pages):
            current_path = path
            if cursor is not None:
                sep = "&" if "?" in path else "?"
                current_path = f"{path}{sep}cursor={cursor}"

            result = self._get(current_path)
            data = result.get("data", [])
            all_data.extend(data)

            meta = result.get("meta", {})
            cursor = meta.get("next_cursor")
            if cursor is None:
                break

        return all_data

    # ── V2 Internal HTTP helpers ──────────────────────────────────────

    def _get_v2(self, path: str) -> dict:
        """Make an authenticated GET to the V2 API with rate limiting + circuit breaker."""
        self._rate_limit()
        url = f"{self.BASE_URL_V2}{path}"
        try:
            resp = resilient_get(
                url,
                group=APIGroup.BALLDONTLIE,
                headers=self._headers(),
            )
            return resp.json()
        except Exception as e:
            import httpx
            if isinstance(e, httpx.HTTPStatusError) and e.response.status_code == 429:
                retry_after = float(e.response.headers.get("Retry-After", "10"))
                self.notify_429(min(retry_after, 30.0))
            raise

    def _get_all_pages_v2(
        self, path: str, *, max_pages: int = 5
    ) -> List[dict]:
        """Fetch all pages from V2 endpoint using cursor-based pagination."""
        all_data: List[dict] = []
        cursor: Optional[int] = None

        for _ in range(max_pages):
            current_path = path
            if cursor is not None:
                sep = "&" if "?" in path else "?"
                current_path = f"{path}{sep}cursor={cursor}"

            result = self._get_v2(current_path)
            data = result.get("data", [])
            all_data.extend(data)

            meta = result.get("meta", {})
            cursor = meta.get("next_cursor")
            if cursor is None:
                break

        return all_data

    # ── Team ID Mapping ──────────────────────────────────────────────

    def _ensure_team_mapping(self):
        """Load BDL team IDs and build NBA ↔ BDL mapping (thread-safe)."""
        if self._teams_loaded:
            return
        with self._team_lock:
            if self._teams_loaded:
                return
            try:
                self._load_team_mapping()
            except Exception as e:
                logger.error(f"[BDL] Team mapping load failed: {e}")
                self._load_hardcoded_mapping()
            self._teams_loaded = True

    def _load_team_mapping(self):
        """Fetch all teams from BDL and match to nba_api static teams."""
        from nba_api.stats.static import teams as nba_teams_static

        nba_teams_list = nba_teams_static.get_teams()
        nba_by_abbr: Dict[str, int] = {
            t["abbreviation"].upper(): t["id"] for t in nba_teams_list
        }

        result = self._get("/teams")
        bdl_teams = result.get("data", [])

        for bt in bdl_teams:
            bdl_id = bt["id"]
            abbr = bt.get("abbreviation", "").upper()
            self._bdl_team_abbrs[bdl_id] = abbr

            # Skip defunct/historical teams (no conference = not a current
            # NBA franchise).  BDL includes teams like the 1940s Washington
            # Capitols (id=42, abbr="WAS") alongside the current Wizards
            # (id=30, abbr="WAS").  Without this filter, the historical
            # team OVERWRITES the current one in the mapping, causing the
            # Wizards to return zero data (querying a 1940s roster).
            conference = (bt.get("conference") or "").strip()
            if not conference:
                continue

            nba_id = nba_by_abbr.get(abbr)
            if nba_id:
                # Only overwrite if we don't already have a mapping for
                # this NBA team.  First match (lower BDL ID = current
                # franchise) takes priority.
                if nba_id not in self._nba_to_bdl_team:
                    self._nba_to_bdl_team[nba_id] = bdl_id
                    self._bdl_to_nba_team[bdl_id] = nba_id

        logger.info(
            f"[BDL] Team mapping loaded: {len(self._nba_to_bdl_team)}/30 teams"
        )

    def _load_hardcoded_mapping(self):
        """Fallback: hard-coded BDL team IDs by abbreviation.

        These are BDL's internal team IDs (1-30).  They rarely change.
        Last verified: Feb 2026.
        """
        from nba_api.stats.static import teams as nba_teams_static

        nba_teams_list = nba_teams_static.get_teams()
        nba_by_abbr: Dict[str, int] = {
            t["abbreviation"].upper(): t["id"] for t in nba_teams_list
        }

        # BDL team IDs mapped by abbreviation
        _BDL_TEAMS = {
            "ATL": 1, "BOS": 2, "BKN": 3, "CHA": 4, "CHI": 5,
            "CLE": 6, "DAL": 7, "DEN": 8, "DET": 9, "GSW": 10,
            "HOU": 11, "IND": 12, "LAC": 13, "LAL": 14, "MEM": 15,
            "MIA": 16, "MIL": 17, "MIN": 18, "NOP": 19, "NYK": 20,
            "OKC": 21, "ORL": 22, "PHI": 23, "PHX": 24, "POR": 25,
            "SAC": 26, "SAS": 27, "TOR": 28, "UTA": 29, "WAS": 30,
        }

        for abbr, bdl_id in _BDL_TEAMS.items():
            nba_id = nba_by_abbr.get(abbr)
            if nba_id:
                self._nba_to_bdl_team[nba_id] = bdl_id
                self._bdl_to_nba_team[bdl_id] = nba_id
                self._bdl_team_abbrs[bdl_id] = abbr

        logger.info(
            f"[BDL] Hardcoded team mapping loaded: "
            f"{len(self._nba_to_bdl_team)} teams"
        )

    def get_bdl_team_id(self, nba_team_id: int) -> Optional[int]:
        """Map an NBA.com team ID to a BALLDONTLIE team ID."""
        self._ensure_team_mapping()
        return self._nba_to_bdl_team.get(nba_team_id)

    def get_nba_team_id(self, bdl_team_id: int) -> Optional[int]:
        """Map a BALLDONTLIE team ID to an NBA.com team ID."""
        self._ensure_team_mapping()
        return self._bdl_to_nba_team.get(bdl_team_id)

    # ── Season format helpers ─────────────────────────────────────────

    @staticmethod
    def nba_season_to_bdl(nba_season: str) -> int:
        """Convert '2025-26' → 2025 (BDL uses the start year)."""
        return int(nba_season.split("-")[0])

    # ── Public API Methods ────────────────────────────────────────────

    def get_team_players(
        self, nba_team_id: int, max_pages: int = 5
    ) -> List[dict]:
        """Get players associated with a team.

        BDL's ``/players?team_ids[]`` endpoint returns ALL players who have
        ever been on the team, not just the current roster.  Results are
        paginated (100 per page).

        ``max_pages`` defaults to **5** (500 players) to ensure recently
        acquired players with high BDL IDs are not truncated.  The previous
        default of 2 caused mid-season acquisitions (trades) to be missed
        because BDL sorts by ascending ID and new players land on page 3+.

        Args:
            nba_team_id: NBA.com team ID (e.g. 1610612741 for CHI).
            max_pages: Maximum pages to fetch (default 5).

        Returns:
            List of BDL player dicts with keys: ``id``, ``first_name``,
            ``last_name``, ``position``, ``height``, ``weight``, ``team``.
        """
        self._ensure_team_mapping()
        bdl_id = self._nba_to_bdl_team.get(nba_team_id)
        if bdl_id is None:
            logger.warning(f"[BDL] No team mapping for NBA ID {nba_team_id}")
            return []

        path = f"/players?team_ids[]={bdl_id}&per_page=100"
        return self._get_all_pages(path, max_pages=max_pages)

    def get_live_box_scores(self, game_ids: List[int]) -> List[dict]:
        """Fetch player box-score stats for specific games (including live).

        Uses the ``/stats?game_ids[]=...`` endpoint which returns player-level
        stats for in-progress AND final games — the key to live prop tracking.

        Args:
            game_ids: BDL game IDs (from ``get_games()`` response).

        Returns:
            List of stat dicts with keys: ``id``, ``min``, ``pts``, ``reb``,
            ``ast``, ``stl``, ``blk``, ``turnover``, ``fg3m``, ``player``,
            ``team``, ``game``.
        """
        if not game_ids:
            return []

        ids_param = "&".join(f"game_ids[]={gid}" for gid in game_ids)
        path = f"/stats?{ids_param}&per_page=100"
        return self._get_all_pages(path, max_pages=10)

    def get_player_stats(
        self,
        bdl_player_ids: List[int],
        season: int,
    ) -> List[dict]:
        """Fetch game-level stats for multiple players in bulk.

        This is the key performance improvement over stats.nba.com:
        replaces 15+ individual ``PlayerGameLog`` calls with 1-2 bulk calls.

        Player IDs are batched into chunks of 25 to avoid URL length issues
        and BDL API limits on array query parameters.

        Args:
            bdl_player_ids: List of BALLDONTLIE player IDs.
            season: Season start year (e.g. 2025 for 2025-26).

        Returns:
            List of stat dicts (one per game per player).
        """
        if not bdl_player_ids:
            return []

        BATCH_SIZE = 25
        all_stats: List[dict] = []

        for i in range(0, len(bdl_player_ids), BATCH_SIZE):
            batch = bdl_player_ids[i : i + BATCH_SIZE]
            ids_param = "&".join(f"player_ids[]={pid}" for pid in batch)
            path = f"/stats?{ids_param}&seasons[]={season}&per_page=100"
            stats = self._get_all_pages(path, max_pages=20)
            all_stats.extend(stats)

        return all_stats

    def get_season_averages(
        self,
        bdl_player_ids: List[int],
        season: int,
    ) -> List[dict]:
        """Fetch season averages for multiple players.

        Args:
            bdl_player_ids: List of BALLDONTLIE player IDs.
            season: Season start year (e.g. 2025 for 2025-26).

        Returns:
            List of season-average dicts.
        """
        if not bdl_player_ids:
            return []

        ids_param = "&".join(f"player_ids[]={pid}" for pid in bdl_player_ids)
        path = f"/season_averages?{ids_param}&season={season}"
        result = self._get(path)
        return result.get("data", [])

    def search_player(self, last_name: str) -> List[dict]:
        """Search for players by name via BDL ``/players?search=`` endpoint.

        Args:
            last_name: Player last name (or partial name) to search for.

        Returns:
            List of BDL player dicts matching the search term.
        """
        from urllib.parse import quote
        path = f"/players?search={quote(last_name)}&per_page=25"
        result = self._get(path)
        return result.get("data", [])

    # ── Player Name Cache ─────────────────────────────────────────────

    def _cache_key(self, name: str, team_abbr: str) -> str:
        """Build a cache key from player name + team abbreviation."""
        return f"{normalize_player_name(name)}:{team_abbr.upper()}"

    def cache_player(self, dk_name: str, team_abbr: str, bdl_player: dict):
        """Store a BDL player dict in the in-memory cache."""
        key = self._cache_key(dk_name, team_abbr)
        with self._player_cache_lock:
            self._player_name_cache[key] = bdl_player

    def get_cached_player(self, dk_name: str, team_abbr: str) -> Optional[dict]:
        """Look up a BDL player dict from the in-memory cache."""
        key = self._cache_key(dk_name, team_abbr)
        with self._player_cache_lock:
            return self._player_name_cache.get(key)

    # ── DK-to-BDL Name Alias Map ─────────────────────────────────────
    # DraftKings sometimes uses nicknames that don't match BDL's legal
    # names.  This map normalizes DK names → BDL search names so the
    # last-name lookup can find them.  Keys are lowercase DK names.
    # DK name (lowercase) → BDL search name.
    # BDL search_player() does substring matching against its DB, so
    # the value here should be whatever string returns the correct player
    # from the BDL /players?search= endpoint.
    #
    # IMPORTANT: Both directions must be covered when the nickname is
    # completely different from the legal name.  DK might list "GG Jackson"
    # but BDL stores "Gregory Jackson".  We need DK→BDL AND BDL→DK.
    _DK_NAME_ALIASES: Dict[str, str] = {
        # ── Nickname → Legal (DK uses nickname, BDL stores legal) ────
        "gg jackson ii": "Gregory Jackson",
        "gg jackson": "Gregory Jackson",
        "bub carrington": "Carlton Carrington",
        "moe wagner": "Moritz Wagner",
        "bones hyland": "Nah'Shon Hyland",
        "kj martin": "Kenyon Martin",
        "kj martin jr.": "Kenyon Martin",

        # ── Abbreviated first names ──────────────────────────────────
        "cam thomas": "Cameron Thomas",
        "cam johnson": "Cameron Johnson",
        "cam whitmore": "Cameron Whitmore",
        "herb jones": "Herbert Jones",
        "nic claxton": "Nicolas Claxton",
        "nicolas claxton": "Nic Claxton",
        "alexandre sarr": "Alex Sarr",
        "alex sarr": "Alexandre Sarr",

        # ── Period / initial variants ────────────────────────────────
        "pj washington": "P.J. Washington",
        "aj green": "A.J. Green",
        "aj griffin": "A.J. Griffin",
        "ej liddell": "E.J. Liddell",
        "ej harkless": "E.J. Harkless",
        "cj mccollum": "C.J. McCollum",
        "rj barrett": "R.J. Barrett",
        "tj mcconnell": "T.J. McConnell",
        "tj warren": "T.J. Warren",

        # ── Misc ─────────────────────────────────────────────────────
        "kristaps porzingis": "Kristaps Porzingis",

        # ── Two-way / G-League call-ups ──────────────────────────────
        # These players are often missing from BDL or listed under
        # legal names.  Adding them defensively for search fallback.
        "walter clayton jr.": "Walter Clayton",
        "walter clayton jr": "Walter Clayton",
        "trey murphy iii": "Trey Murphy",
    }

    # ── DK-Driven Bulk Resolution ─────────────────────────────────────

    def resolve_dk_players_bulk(
        self,
        dk_players: List[Tuple[str, str]],
    ) -> Dict[str, dict]:
        """Resolve DK player names to BDL player dicts via search.

        Instead of fetching all-time team rosters (200+ players, 10+ API
        pages per team), this method searches by player last name (1 API
        call per unique last name).  Results are cached so subsequent
        calls for the same slate are essentially free.

        Args:
            dk_players: List of ``(display_name, team_abbreviation)`` tuples
                from DraftKings draftables.

        Returns:
            Dict mapping ``display_name`` → BDL player dict.
        """
        resolved: Dict[str, dict] = {}

        # 0. Apply DK→BDL name aliases before anything else.
        #    We keep the original dk_name as the key but search under
        #    the alias so BDL can find the player.
        aliased_players: List[Tuple[str, str, str]] = []  # (orig, search, team)
        for dk_name, team_abbr in dk_players:
            alias = self._DK_NAME_ALIASES.get(dk_name.lower().strip())
            aliased_players.append((dk_name, alias or dk_name, team_abbr))

        # 1. Check cache for each player (try both dk_name and alias)
        uncached: List[Tuple[str, str, str]] = []  # (dk_name, search_name, team)
        for dk_name, search_name, team_abbr in aliased_players:
            cached = self.get_cached_player(dk_name, team_abbr)
            if not cached and search_name != dk_name:
                cached = self.get_cached_player(search_name, team_abbr)
            if cached:
                resolved[dk_name] = cached
            else:
                uncached.append((dk_name, search_name, team_abbr))

        if not uncached:
            logger.info(
                f"[BDL] resolve_dk_players_bulk: all {len(dk_players)} "
                f"players found in cache (0 API calls)"
            )
            return resolved

        # 2. Group uncached players by unique last name to deduplicate
        #    search calls.  "LeBron James" and "Mike James" share last
        #    name "James" -> one search call returns both.
        #    Use search_name (the alias) for grouping so BDL can find them.
        import re as _re

        _SUFFIX_RE = _re.compile(
            r"\b(jr\.?|sr\.?|iii|iv|ii)\s*$", _re.IGNORECASE
        )

        last_name_groups: Dict[str, List[Tuple[str, str, str]]] = {}
        for dk_name, search_name, team_abbr in uncached:
            # Strip suffixes before extracting last name so that
            # "Jaime Jaquez Jr." -> last_name "jaquez" (not "jr.")
            clean = _SUFFIX_RE.sub("", search_name).strip()
            parts = clean.split()
            last_name = parts[-1] if len(parts) > 1 else clean
            last_name_lower = last_name.lower()
            last_name_groups.setdefault(last_name_lower, []).append(
                (dk_name, search_name, team_abbr)
            )

        logger.info(
            f"[BDL] resolve_dk_players_bulk: {len(resolved)} cached, "
            f"{len(uncached)} uncached -> {len(last_name_groups)} unique "
            f"last name searches needed"
        )

        # 3. Search each unique last name and match results
        _api_calls = 0
        for last_name, players_with_name in last_name_groups.items():
            try:
                search_results = self.search_player(last_name)
                _api_calls += 1
            except Exception as e:
                logger.warning(
                    f"[BDL] Search failed for '{last_name}': {e}"
                )
                continue

            if not search_results:
                continue

            # Match each DK player against search results
            for dk_name, search_name, team_abbr in players_with_name:
                # Use the alias (search_name) for matching against BDL
                dk_norm = normalize_player_name(search_name)
                dk_parts = dk_norm.split()
                dk_first = dk_parts[0] if dk_parts else ""
                dk_last_n = dk_parts[-1] if len(dk_parts) > 1 else ""
                best_match = None

                for bp in search_results:
                    bp_name = f"{bp['first_name']} {bp['last_name']}"
                    bp_norm = normalize_player_name(bp_name)
                    bp_team = (
                        bp.get("team", {}).get("abbreviation", "")
                    ).upper()
                    same_team = bp_team == team_abbr.upper()

                    # Exact name match on same team is best
                    if dk_norm == bp_norm and same_team:
                        best_match = bp
                        break
                    # Substring match on same team
                    if same_team and (
                        dk_norm in bp_norm or bp_norm in dk_norm
                    ):
                        best_match = bp
                        break
                    # Nickname match on same team: same last name,
                    # first name is a prefix (Cam/Cameron)
                    if same_team and dk_last_n:
                        bp_parts = bp_norm.split()
                        bp_last_n = bp_parts[-1] if len(bp_parts) > 1 else ""
                        bp_first = bp_parts[0] if bp_parts else ""
                        if dk_last_n == bp_last_n and (
                            dk_first.startswith(bp_first)
                            or bp_first.startswith(dk_first)
                        ):
                            best_match = bp
                            break

                # If no team match, try name-only match (traded players
                # may still be under old team in BDL)
                if not best_match:
                    for bp in search_results:
                        bp_name = f"{bp['first_name']} {bp['last_name']}"
                        bp_norm = normalize_player_name(bp_name)
                        if dk_norm == bp_norm or (
                            dk_norm in bp_norm or bp_norm in dk_norm
                        ):
                            best_match = bp
                            break

                if best_match:
                    resolved[dk_name] = best_match
                    self.cache_player(dk_name, team_abbr, best_match)

        # 4. Fallback: for players still unresolved (common last names
        #    like "Johnson" return 25+ results sorted by historical ID,
        #    so newer players are buried), search by first name instead.
        #    BDL has far fewer "Jalen"s than "Johnson"s, so first-name
        #    search usually surfaces the right player on page 1.
        still_missing = [
            (dn, sn, ta) for dn, sn, ta in uncached if dn not in resolved
        ]
        if still_missing:
            logger.info(
                f"[BDL] {len(still_missing)} players unresolved after "
                f"last-name search, trying first-name fallback"
            )
            for dk_name, search_name, team_abbr in still_missing:
                clean = _SUFFIX_RE.sub("", search_name).strip()
                parts = clean.split()
                first_name = parts[0] if parts else clean
                try:
                    results = self.search_player(first_name)
                    _api_calls += 1
                except Exception as e:
                    logger.warning(
                        f"[BDL] First-name search failed for "
                        f"'{first_name}': {e}"
                    )
                    continue
                if not results:
                    continue

                # Use alias (search_name) for matching against BDL results
                dk_norm = normalize_player_name(search_name)
                dk_parts = dk_norm.split()
                dk_last = dk_parts[-1] if len(dk_parts) > 1 else ""
                best_match = None

                for bp in results:
                    bp_name = f"{bp['first_name']} {bp['last_name']}"
                    bp_norm = normalize_player_name(bp_name)
                    bp_team = (
                        bp.get("team", {}).get("abbreviation", "")
                    ).upper()

                    # Exact normalized match + same team
                    if dk_norm == bp_norm and bp_team == team_abbr.upper():
                        best_match = bp
                        break
                    # Exact match, different team (traded)
                    if dk_norm == bp_norm:
                        best_match = bp
                        break
                    # Nickname match: same last name + first name is
                    # a prefix (Cam/Cameron, Herb/Herbert), same team
                    bp_parts = bp_norm.split()
                    bp_last = bp_parts[-1] if len(bp_parts) > 1 else ""
                    bp_first = bp_parts[0] if bp_parts else ""
                    dk_first = dk_parts[0] if dk_parts else ""
                    if (
                        dk_last
                        and dk_last == bp_last
                        and bp_team == team_abbr.upper()
                        and (
                            dk_first.startswith(bp_first)
                            or bp_first.startswith(dk_first)
                        )
                    ):
                        best_match = bp
                        break

                # Nickname match without team (traded + nickname)
                if not best_match:
                    for bp in results:
                        bp_name = f"{bp['first_name']} {bp['last_name']}"
                        bp_norm = normalize_player_name(bp_name)
                        bp_parts = bp_norm.split()
                        bp_last = bp_parts[-1] if len(bp_parts) > 1 else ""
                        bp_first = bp_parts[0] if bp_parts else ""
                        dk_first = dk_parts[0] if dk_parts else ""
                        if (
                            dk_last
                            and dk_last == bp_last
                            and (
                                dk_first.startswith(bp_first)
                                or bp_first.startswith(dk_first)
                            )
                        ):
                            best_match = bp
                            break

                if best_match:
                    resolved[dk_name] = best_match
                    self.cache_player(dk_name, team_abbr, best_match)

        # 5. Fuzzy fallback — last-resort SequenceMatcher for any players
        #    that survived all previous stages.  This catches near-misses
        #    like "Alexandre Sarr" / "Alex Sarr" (ratio=0.78 on normalized
        #    names) that are just below the substring/prefix thresholds.
        #    We use a conservative threshold (0.75) + same-team requirement
        #    to avoid false positives.
        from difflib import SequenceMatcher as _SM

        _FUZZY_THRESHOLD = 0.75
        still_missing_final = [
            (dn, sn, ta) for dn, sn, ta in uncached if dn not in resolved
        ]
        if still_missing_final:
            logger.warning(
                f"[BDL] {len(still_missing_final)} players STILL unresolved "
                f"after all stages, trying fuzzy fallback: "
                f"{[dn for dn, _, _ in still_missing_final]}"
            )
            for dk_name, search_name, team_abbr in still_missing_final:
                dk_norm = normalize_player_name(search_name)
                # Search by last name one more time (may have already
                # been done, but results aren't cached between stages)
                clean = _SUFFIX_RE.sub("", search_name).strip()
                parts = clean.split()
                last_name = parts[-1] if len(parts) > 1 else clean
                try:
                    results = self.search_player(last_name)
                    _api_calls += 1
                except Exception:
                    continue
                if not results:
                    continue

                best_match = None
                best_ratio = 0.0
                for bp in results:
                    bp_name = f"{bp['first_name']} {bp['last_name']}"
                    bp_norm = normalize_player_name(bp_name)
                    bp_team = (
                        bp.get("team", {}).get("abbreviation", "")
                    ).upper()
                    # Same-team requirement for fuzzy to prevent false positives
                    if bp_team != team_abbr.upper():
                        continue
                    ratio = _SM(None, dk_norm, bp_norm).ratio()
                    if ratio > best_ratio:
                        best_ratio = ratio
                        best_match = bp

                if best_match and best_ratio >= _FUZZY_THRESHOLD:
                    resolved[dk_name] = best_match
                    self.cache_player(dk_name, team_abbr, best_match)
                    logger.info(
                        f"[BDL] Fuzzy matched '{dk_name}' -> "
                        f"'{best_match['first_name']} {best_match['last_name']}' "
                        f"(ratio={best_ratio:.3f}, team={team_abbr})"
                    )
                else:
                    logger.warning(
                        f"[BDL] DROPPED: '{dk_name}' ({team_abbr}) — no match "
                        f"found after all stages (best_ratio="
                        f"{best_ratio:.3f if best_match else 0:.3f})"
                    )

        _unresolved = len(uncached) - (len(resolved) - (len(dk_players) - len(uncached)))
        logger.info(
            f"[BDL] resolve_dk_players_bulk complete: "
            f"{len(resolved)}/{len(dk_players)} resolved, "
            f"{_api_calls} API calls, "
            f"{_unresolved} unresolved"
        )

        return resolved

    def get_dk_driven_game_logs(
        self,
        dk_players: List[Tuple[str, str]],
        season_str: str,
    ) -> Tuple[List[dict], Dict[str, List[dict]]]:
        """Fetch roster + game logs for DK-listed players only.

        This is the DK-driven replacement for ``get_team_game_logs_bulk()``.
        Instead of fetching an all-time team roster (200+ players, 10+ API
        pages) and then querying stats for all of them, this method:

        1. Resolves DK player names → BDL player IDs (via search + cache)
        2. Batch-fetches stats for only those players (1-2 API calls)

        API call reduction: ~100-200 calls → ~20-80 (cold cache) or ~6-12
        (warm cache).

        Args:
            dk_players: ``(display_name, team_abbreviation)`` tuples from DK.
            season_str: NBA season string (e.g. ``"2025-26"``).

        Returns:
            Same format as ``get_team_game_logs_bulk()``:
            ``(roster, game_logs_by_name)``
        """
        season = self.nba_season_to_bdl(season_str)

        # 1. Resolve DK names → BDL player dicts
        resolved = self.resolve_dk_players_bulk(dk_players)
        if not resolved:
            logger.warning("[BDL] DK-driven lookup resolved 0 players")
            return [], {}

        # 2. Collect BDL IDs for stats fetch
        bdl_ids = [bp["id"] for bp in resolved.values()]

        # 3. Batch-fetch stats (only for resolved players)
        all_stats = self.get_player_stats(bdl_ids, season)

        # 4. Build roster + game_logs_by_name in same format as
        #    get_team_game_logs_bulk()
        roster: List[dict] = list(resolved.values())

        # Identify which players have season data
        active_ids: set = set()
        for stat in all_stats:
            pid = stat.get("player", {}).get("id")
            if pid:
                active_ids.add(pid)

        # Filter roster to players with stats
        active_roster = [p for p in roster if p["id"] in active_ids]

        # Build name → ID mapping
        player_id_to_name: Dict[int, str] = {}
        player_id_to_team: Dict[int, str] = {}
        for dk_name, bdl_player in resolved.items():
            bdl_id = bdl_player["id"]
            bdl_name = f"{bdl_player['first_name']} {bdl_player['last_name']}"
            player_id_to_name[bdl_id] = bdl_name
            player_id_to_team[bdl_id] = (
                bdl_player.get("team", {}).get("abbreviation", "")
            )

        # Group stats by player name, convert to NBA format
        logs_by_name: Dict[str, List[dict]] = {}
        for stat in all_stats:
            bdl_pid = stat.get("player", {}).get("id")
            name = player_id_to_name.get(bdl_pid, "Unknown")
            team_abbr = player_id_to_team.get(bdl_pid, "")
            nba_row = self.bdl_stat_to_nba_format(stat, team_abbr)
            logs_by_name.setdefault(name, []).append(nba_row)

        # Sort each player's games by date (most recent first)
        for name in logs_by_name:
            logs_by_name[name].sort(
                key=lambda g: g.get("GAME_DATE", ""), reverse=True
            )

        logger.info(
            f"[BDL] DK-driven fetch: {len(active_roster)} players with stats, "
            f"{len(all_stats)} game rows, {len(logs_by_name)} with logs"
        )

        return active_roster, logs_by_name

    def get_single_player_stats(
        self, bdl_player_id: int, season: int, team_abbr: str = ""
    ) -> List[dict]:
        """Fetch one player's game logs for a season, converted to NBA API format.

        Args:
            bdl_player_id: BDL player ID.
            season: Season start year (e.g. 2025 for 2025-26).
            team_abbr: Team abbreviation for MATCHUP construction.

        Returns:
            List of NBA-format game log dicts (same format as
            ``bdl_stat_to_nba_format`` output).
        """
        stats = self.get_player_stats([bdl_player_id], season)
        return [self.bdl_stat_to_nba_format(s, team_abbr) for s in stats]

    def get_games(self, date_str: str) -> List[dict]:
        """Fetch games for a specific date.

        Args:
            date_str: Date in ``YYYY-MM-DD`` format.

        Returns:
            List of game dicts with home/away teams and scores.
        """
        path = f"/games?dates[]={date_str}"
        result = self._get(path)
        return result.get("data", [])

    def get_game_statuses_by_team(self, date_str: str) -> Dict[str, dict]:
        """Fetch games for a date and return status indexed by team abbreviation.

        Classification:
          - ``status == "Final"``  → ``game_status="final"``, ``is_locked=True``
          - ``period > 0``         → ``game_status="in_progress"``, ``is_locked=True``
          - otherwise              → ``game_status="not_started"``, ``is_locked=False``

        Returns:
            Dict mapping uppercase team abbreviation to a dict with keys:
            ``game_status``, ``game_status_detail``, ``is_locked``,
            ``opponent``, ``home_team_score``, ``visitor_team_score``,
            ``bdl_game_id``, ``period``.
        """
        raw_games = self.get_games(date_str)
        result: Dict[str, dict] = {}

        for g in raw_games:
            home = g.get("home_team") or {}
            visitor = g.get("visitor_team") or {}
            home_abbr = (home.get("abbreviation") or "").upper()
            visitor_abbr = (visitor.get("abbreviation") or "").upper()

            status_raw = g.get("status", "")
            period = g.get("period", 0) or 0

            if status_raw == "Final":
                game_status = "final"
                is_locked = True
            elif period > 0:
                game_status = "in_progress"
                is_locked = True
            else:
                game_status = "not_started"
                is_locked = False

            shared = {
                "game_status": game_status,
                "game_status_detail": status_raw,
                "is_locked": is_locked,
                "home_team_score": g.get("home_team_score", 0),
                "visitor_team_score": g.get("visitor_team_score", 0),
                "bdl_game_id": g.get("id"),
                "period": period,
            }

            if home_abbr:
                result[home_abbr] = {**shared, "opponent": visitor_abbr}
            if visitor_abbr:
                result[visitor_abbr] = {**shared, "opponent": home_abbr}

        return result

    # ── V2 Odds ────────────────────────────────────────────────────────

    def get_odds(self, date_str: str) -> List[dict]:
        """Fetch odds for a specific date from the BDL V2 odds endpoint.

        Args:
            date_str: Date in ``YYYY-MM-DD`` format.

        Returns:
            List of raw odds dicts with ``game_id``, ``vendor``,
            ``spread_home_value``, ``total_value``, etc.
        """
        path = f"/odds?dates[]={date_str}&per_page=100"
        raw = self._get_all_pages_v2(path, max_pages=3)
        return self._validate_odds_response(raw)

    def _validate_odds_response(self, raw: List[dict]) -> List[dict]:
        """Validate odds entries via Pydantic schemas.

        Runs each entry through ``BDLOddsEntry.model_validate()`` for
        type-checking.  On validation failure a warning is logged and
        the original (unmodified) data is returned — this ensures
        graceful degradation when BDL changes its response shape.
        """
        try:
            from app.models.balldontlie_schemas import BDLOddsEntry
            for entry in raw:
                BDLOddsEntry.model_validate(entry)
        except Exception as e:
            logger.warning(f"[BDL] Odds response validation warning: {e}")
        return raw

    def get_odds_by_game(self, date_str: str) -> Dict[int, dict]:
        """Fetch odds for a date and return the best entry per game.

        Prefers DraftKings odds; falls back to the first available vendor.

        Returns:
            Dict mapping BDL ``game_id`` to a dict with keys:
            ``spread_home``, ``spread_away``, ``total``, ``vendor``,
            ``updated_at``, ``bdl_game_id``.
        """
        raw = self.get_odds(date_str)
        if not raw:
            return {}

        by_game: Dict[int, dict] = {}
        for entry in raw:
            gid = entry.get("game_id")
            if not gid:
                continue
            vendor = (entry.get("vendor") or "").lower()
            existing = by_game.get(gid)
            if existing is None or (
                vendor == "draftkings"
                and existing.get("vendor", "").lower() != "draftkings"
            ):
                by_game[gid] = {
                    "spread_home": entry.get("spread_home_value"),
                    "spread_away": entry.get("spread_away_value"),
                    "total": entry.get("total_value"),
                    "moneyline_home": entry.get("moneyline_home_odds"),
                    "moneyline_away": entry.get("moneyline_away_odds"),
                    "vendor": entry.get("vendor", ""),
                    "updated_at": entry.get("updated_at", ""),
                    "bdl_game_id": gid,
                }

        logger.info(
            f"[BDL] Odds fetched for {len(by_game)} games on {date_str}"
        )
        return by_game

    # ── Format Translation ────────────────────────────────────────────

    def bdl_stat_to_nba_format(
        self,
        stat: dict,
        team_abbr: str = "",
    ) -> dict:
        """Convert a BDL stats row to NBA API game-log format.

        This allows reuse of ``NBAApiService._build_player_minutes_from_gamelog``
        without modification.

        BDL keys (lowercase):  min, pts, reb, ast, stl, blk, turnover, fg3m, ...
        NBA keys (uppercase):  MIN, PTS, REB, AST, STL, BLK, TOV, FG3M, ...
        """
        # Parse BDL minutes string ("32:15", "00", None, etc.)
        raw_min = stat.get("min", "0")
        if isinstance(raw_min, str) and ":" in raw_min:
            parts = raw_min.split(":")
            try:
                minutes = int(parts[0]) + int(parts[1]) / 60
            except (ValueError, IndexError):
                minutes = 0
        elif raw_min:
            try:
                minutes = float(raw_min)
            except (ValueError, TypeError):
                minutes = 0
        else:
            minutes = 0

        # Build MATCHUP string from game object.
        # BDL /stats response has game.home_team_id / game.visitor_team_id
        # (integers) but NOT nested home_team/visitor_team objects.
        # Use the BDL team-ID-to-abbreviation lookup for accurate matchups.
        game = stat.get("game", {})
        player_team = stat.get("team", {})
        player_abbr = player_team.get("abbreviation", team_abbr)

        home_team_id = game.get("home_team_id")
        visitor_team_id = game.get("visitor_team_id")
        player_team_id = player_team.get("id")

        if player_team_id and home_team_id and player_team_id == home_team_id:
            opp_abbr = self._bdl_team_abbrs.get(visitor_team_id, "???")
            matchup = f"{player_abbr} vs. {opp_abbr}"
        elif home_team_id:
            home_abbr = self._bdl_team_abbrs.get(home_team_id, "???")
            matchup = f"{player_abbr} @ {home_abbr}"
        else:
            # Fallback: player team only (still passes trade filter)
            matchup = f"{player_abbr} vs. ???"

        # Derive a Game_ID so the 4 AM DB cache refresh can persist
        # BDL-sourced game logs (previously missing → silently skipped).
        game_id = str(game.get("id", ""))
        if not game_id:
            # Fallback: synthesise from date + team IDs so the row isn't
            # dropped by the ``if not game_id: continue`` guard.
            _gdate = game.get("date", "unknown")
            game_id = f"BDL-{home_team_id or 0}-{visitor_team_id or 0}-{_gdate}"

        return {
            "Game_ID": game_id,
            "GAME_ID": game_id,
            "MIN": round(minutes, 1),
            "PTS": stat.get("pts", 0) or 0,
            "REB": stat.get("reb", 0) or 0,
            "AST": stat.get("ast", 0) or 0,
            "STL": stat.get("stl", 0) or 0,
            "BLK": stat.get("blk", 0) or 0,
            "TOV": stat.get("turnover", 0) or 0,
            "FG3M": stat.get("fg3m", 0) or 0,
            "PLUS_MINUS": stat.get("plus_minus", 0) or 0,
            "FGM": stat.get("fgm", 0) or 0,
            "FGA": stat.get("fga", 0) or 0,
            "FG_PCT": stat.get("fg_pct", 0) or 0,
            "FG3A": stat.get("fg3a", 0) or 0,
            "FG3_PCT": stat.get("fg3_pct", 0) or 0,
            "FTM": stat.get("ftm", 0) or 0,
            "FTA": stat.get("fta", 0) or 0,
            "FT_PCT": stat.get("ft_pct", 0) or 0,
            "OREB": stat.get("oreb", 0) or 0,
            "DREB": stat.get("dreb", 0) or 0,
            "MATCHUP": matchup,
            "GAME_DATE": game.get("date", ""),
        }

    # ── High-Level Bulk Fetch ─────────────────────────────────────────

    def get_team_game_logs_bulk(
        self,
        nba_team_id: int,
        season_str: str,
    ) -> Tuple[List[dict], Dict[str, List[dict]]]:
        """Fetch roster + all game logs for a team via batched API calls.

        BDL's ``/players?team_ids[]`` returns ALL players ever associated
        with the team (200+), not just the current roster.  To avoid
        querying stats for 200+ mostly-historical players, we first fetch
        a small batch of stats to identify which player IDs actually have
        data for the requested season, then only query those players.

        Uses a class-level semaphore (``_team_fetch_semaphore``) to limit
        how many teams fetch concurrently.  Without this, 18 concurrent
        rotation builds each making 10-30 BDL API calls would overwhelm
        the 600 RPM rate limit and trigger cascading 429 errors.

        Args:
            nba_team_id: NBA.com team ID.
            season_str: NBA season string (e.g. ``"2025-26"``).

        Returns:
            ``(roster, game_logs_by_name)``
            - ``roster``: list of BDL player dicts (only those with season data)
            - ``game_logs_by_name``: ``{player_full_name: [NBA-format rows]}``
              Game logs are sorted most-recent-first (reverse chronological).
        """
        # Acquire semaphore — only N teams fetch BDL data at a time.
        # Other teams block here until a slot opens, which naturally
        # staggers the API calls and prevents 429 bursts.
        with self._team_fetch_semaphore:
            return self._get_team_game_logs_bulk_inner(nba_team_id, season_str)

    def _get_team_game_logs_bulk_inner(
        self,
        nba_team_id: int,
        season_str: str,
    ) -> Tuple[List[dict], Dict[str, List[dict]]]:
        """Inner implementation of get_team_game_logs_bulk (runs under semaphore)."""
        season = self.nba_season_to_bdl(season_str)

        # 1. Get full all-time roster (paginated — may be 200+ players)
        full_roster = self.get_team_players(nba_team_id)
        if not full_roster:
            return [], {}

        logger.info(
            f"[BDL] Full roster for team {nba_team_id}: {len(full_roster)} "
            f"all-time players (querying {season} stats in batches)"
        )

        # 2. Get stats in batches of 25 player IDs
        bdl_ids = [p["id"] for p in full_roster]
        all_stats = self.get_player_stats(bdl_ids, season)

        # 3. Identify which players actually have season data
        active_player_ids: set = set()
        for stat in all_stats:
            pid = stat.get("player", {}).get("id")
            if pid:
                active_player_ids.add(pid)

        # Filter roster to only active-season players
        roster = [p for p in full_roster if p["id"] in active_player_ids]
        logger.info(
            f"[BDL] {len(roster)}/{len(full_roster)} players have "
            f"{season} season stats ({len(all_stats)} total game rows)"
        )

        # 3. Group by player and convert to NBA format
        player_id_to_name: Dict[int, str] = {
            p["id"]: f"{p['first_name']} {p['last_name']}"
            for p in roster
        }

        # Determine team abbreviation for MATCHUP construction
        team_abbr = ""
        if roster and roster[0].get("team"):
            team_abbr = roster[0]["team"].get("abbreviation", "")

        logs_by_name: Dict[str, List[dict]] = {}
        for stat in all_stats:
            bdl_pid = stat.get("player", {}).get("id")
            name = player_id_to_name.get(bdl_pid, "Unknown")
            nba_row = self.bdl_stat_to_nba_format(stat, team_abbr)

            if name not in logs_by_name:
                logs_by_name[name] = []
            logs_by_name[name].append(nba_row)

        # 4. Sort each player's games by date (most recent first)
        for name in logs_by_name:
            logs_by_name[name].sort(
                key=lambda g: g.get("GAME_DATE", ""), reverse=True
            )

        logger.info(
            f"[BDL] Bulk fetch for team {nba_team_id}: "
            f"{len(roster)} players, {len(all_stats)} stat rows, "
            f"{len(logs_by_name)} players with game logs"
        )

        return roster, logs_by_name

    def get_bdl_position(self, bdl_player: dict) -> str:
        """Extract a position string from a BDL player dict.

        BDL uses full position names like ``"G"``, ``"F"``, ``"C"``,
        ``"G-F"``, ``"F-C"``, etc.  Returns the raw value or ``"F"``
        as a safe default.
        """
        return bdl_player.get("position", "F") or "F"
