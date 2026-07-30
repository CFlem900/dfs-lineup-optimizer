"""DraftKings ↔ BallDontLie player-ID mapping with fuzzy matching.

Maps DraftKings player names (``"Nicolas Claxton"``) to BallDontLie
player IDs so that live BDL stats can be joined to DK prop lines and
DFS salaries.

Matching strategy (ordered by priority):
  1. **Exact normalized match** — ``name:TEAM`` key (fastest, most common)
  2. **Last-name + team** fallback — handles first-name abbreviations
  3. **Fuzzy match** — ``SequenceMatcher`` ratio ≥ 0.82 within the same
     team (catches Nick/Nicolas, PJ/P.J., etc.)

Results are cached in Redis (``player_map:{name}:{team}`` keys, 24h TTL)
so repeated lookups during a slate are essentially free.

Usage
-----
::

    from app.services.player_id_mapper import PlayerIdMapper

    mapper = PlayerIdMapper(bdl_service, cache_service)

    # Build from a DK prop / draftables list
    mapping = await mapper.build_mapping(dk_players)
    # → {"Luka Doncic": PlayerMapping(bdl_id=666, ...), ...}

    # Single lookup
    pm = await mapper.resolve("Nick Claxton", "BKN")
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────

CACHE_KEY_PREFIX = "player_map"
CACHE_TTL = 86_400  # 24 hours — mapping is stable within a day
FUZZY_THRESHOLD = 0.82  # SequenceMatcher ratio cutoff

# Common DK ↔ BDL name mismatches that fuzzy matching may miss.
# Keyed by normalized DK name → normalized BDL name.
KNOWN_ALIASES: Dict[str, str] = {
    # ── Nickname → Legal name (completely different first names) ──────
    # These CANNOT be caught by fuzzy matching because the names are too
    # different (ratio < 0.82).  Every dropped player in production should
    # be added here when diagnosed.
    "gg jackson": "gregory jackson",
    "bub carrington": "carlton carrington",
    "moe wagner": "moritz wagner",
    "bones hyland": "nahshon hyland",
    "kj martin": "kenyon martin",
    "alexandre sarr": "alex sarr",
    "alex sarr": "alexandre sarr",

    # ── Shortened / abbreviated first names ──────────────────────────
    "nic claxton": "nicolas claxton",
    "nick claxton": "nicolas claxton",
    "herb jones": "herbert jones",
    "cam thomas": "cameron thomas",
    "cam johnson": "cameron johnson",
    "cam whitmore": "cameron whitmore",
    "naz reid": "naz reid",

    # ── Initialism first names (DK uses initials) ────────────────────
    "pj washington": "pj washington",
    "tj mcconnell": "tj mcconnell",
    "tj warren": "tj warren",
    "cj mccollum": "cj mccollum",
    "rj barrett": "rj barrett",
    "aj griffin": "aj griffin",
    "aj green": "aj green",
    "ej liddell": "ej liddell",

    # ── Suffix mismatches (DK strips, BDL keeps, or vice versa) ──────
    # normalize_player_name strips Jr/Sr/II/III/IV, so these handle the
    # REVERSE case where BDL has the suffix-free name but DK has the
    # suffixed form (rare but happens with traded players).
    "kenyon martin": "kenyon martin",
    "dereck lively": "dereck lively",
    "jabari smith": "jabari smith",
    "jaren jackson": "jaren jackson",
    "gary trent": "gary trent",
    "larry nance": "larry nance",
    "marcus morris": "marcus morris",
    "michael porter": "michael porter",
    "kevin porter": "kevin porter",
    "otto porter": "otto porter",
    "tim hardaway": "tim hardaway",
    "wendell carter": "wendell carter",
    "kelly oubre": "kelly oubre",
    "al horford": "al horford",
    "scotty pippen": "scotty pippen",
    "craig porter": "craig porter",
    "patrick baldwin": "patrick baldwin",
    "jaime jaquez": "jaime jaquez",
    "dennis smith": "dennis smith",
    "gary payton": "gary payton",
    "walker kessler": "walker kessler",

    # ── Hyphenated / punctuated name variants ────────────────────────
    # After normalize_player_name, hyphens become spaces and apostrophes
    # are removed, so most of these now auto-match.  Kept for safety
    # in case of upstream normalization changes.
    "shai gilgeous alexander": "shai gilgeous alexander",
    "deaaron fox": "deaaron fox",
    "trayce jackson davis": "trayce jackson davis",
    "nickeil alexander walker": "nickeil alexander walker",

    # ── EJ Harkless special case ─────────────────────────────────────
    # BDL might list as "E.J. Harkless" or "Emmanuel Harkless"
    "ej harkless": "ej harkless",
    "emmanuel harkless": "ej harkless",

    # ── Two-way / G-League call-ups (name mismatches in BDL) ───────
    # These players are often missing from BDL entirely or listed
    # under legal names that differ from DK display names.
    "walter clayton": "walter clayton",    # MEM two-way — BDL may lack
    "kennedy chandler": "kennedy chandler",  # UTA — BDL may lack
    "jaylen wells": "jaylen wells",        # MEM rookie — BDL may lack

    # ── Additional common mismatches ───────────────────────────────
    "trey murphy": "trey murphy",         # NOP — sometimes "Trey Murphy III"
    "brandon clarke": "brandon clarke",    # MEM
    "jalen green": "jalen green",
    "keyonte george": "keyonte george",
    "collin sexton": "collin sexton",
    "zaccharie risacher": "zaccharie risacher",  # ATL rookie
}


# ── Data classes ──────────────────────────────────────────────────────────


@dataclass
class PlayerMapping:
    """A resolved DK → BDL player mapping."""

    dk_name: str
    bdl_id: int
    bdl_first_name: str
    bdl_last_name: str
    team: str
    match_method: str  # "exact", "last_name", "fuzzy", "alias", "cached"
    confidence: float  # 0.0–1.0


@dataclass
class DKPlayerInput:
    """Minimal DK player info needed for mapping.

    Can be constructed from ``DKPlayerSalary``, ``PlayerPropLine``,
    or a raw CSV/JSON row.
    """

    name: str
    team: str
    dk_player_id: Optional[int] = None
    position: Optional[str] = None


# ── Name normalization ────────────────────────────────────────────────────


def normalize_name(name: str) -> str:
    """Normalize a player name for matching.

    Delegates to the canonical ``normalize_player_name`` from helpers.py
    so all DK services (props, draftables, mapper) produce identical keys.

    Steps (see helpers.py for full details):
      1. Lowercase + strip
      2. Unicode NFKD decomposition (Jokić → jokic)
      3. Strip suffixes (Jr., Sr., III, IV, II)
      4. Remove periods, apostrophes, hyphens
      5. Collapse whitespace
    """
    from app.utils.helpers import normalize_player_name
    return normalize_player_name(name)


def _last_name(normalized: str) -> str:
    """Extract the last token from a normalized name."""
    parts = normalized.split()
    return parts[-1] if parts else normalized


def _make_cache_key(normalized_name: str, team: str) -> str:
    return f"{CACHE_KEY_PREFIX}:{normalized_name}:{team.upper()}"


# ── Core mapper ───────────────────────────────────────────────────────────


class PlayerIdMapper:
    """Maps DraftKings player names to BallDontLie player IDs.

    Parameters
    ----------
    bdl_service : BallDontLieService
        The existing BDL client (with roster fetching).
    cache_service : CacheService, optional
        Redis-backed cache.  If ``None``, caching is skipped.
    fuzzy_threshold : float
        Minimum ``SequenceMatcher`` ratio to accept a fuzzy match.
    """

    def __init__(
        self,
        bdl_service,
        cache_service=None,
        fuzzy_threshold: float = FUZZY_THRESHOLD,
    ):
        self._bdl = bdl_service
        self._cache = cache_service
        self._fuzzy_threshold = fuzzy_threshold

        # In-memory roster index built once per session.
        # Keys: team abbreviation (upper) → list of (normalized_full, bdl_player_dict)
        self._roster_index: Dict[str, List[Tuple[str, dict]]] = {}
        self._index_built = False

    # ── Public API ────────────────────────────────────────────────────

    async def build_mapping(
        self,
        dk_players: Sequence[DKPlayerInput],
    ) -> Dict[str, PlayerMapping]:
        """Resolve BDL IDs for a list of DK players.

        Args:
            dk_players: DK player list (from CSV, draftables, or props).

        Returns:
            Dict mapping ``dk_name`` → ``PlayerMapping``.  Players that
            could not be matched are omitted (logged as warnings).
        """
        result: Dict[str, PlayerMapping] = {}
        # Collect unique teams to pre-fetch rosters
        teams = {p.team.upper() for p in dk_players if p.team}
        await self._ensure_roster_index(teams)

        for dk in dk_players:
            pm = await self.resolve(dk.name, dk.team)
            if pm:
                result[dk.name] = pm
            else:
                logger.warning(
                    f"[PlayerMap] No BDL match for DK player "
                    f"{dk.name!r} ({dk.team})"
                )

        matched = len(result)
        total = len(dk_players)
        logger.info(
            f"[PlayerMap] Mapped {matched}/{total} DK players to BDL IDs "
            f"({matched / total * 100:.0f}% hit rate)"
            if total
            else "[PlayerMap] No DK players to map"
        )
        return result

    async def resolve(
        self, dk_name: str, team: str
    ) -> Optional[PlayerMapping]:
        """Resolve a single DK player name to a BDL player mapping.

        Tries (in order): Redis cache → exact → alias → last-name → fuzzy.
        """
        team = team.upper()
        norm = normalize_name(dk_name)
        cache_key = _make_cache_key(norm, team)

        # 1. Check Redis cache
        cached = await self._cache_get(cache_key)
        if cached:
            return PlayerMapping(
                dk_name=dk_name,
                bdl_id=cached["bdl_id"],
                bdl_first_name=cached["first"],
                bdl_last_name=cached["last"],
                team=team,
                match_method="cached",
                confidence=cached.get("conf", 1.0),
            )

        # 2. Ensure roster is loaded for this team
        await self._ensure_roster_index({team})

        roster = self._roster_index.get(team, [])
        if not roster:
            return None

        # 3. Exact normalized match
        pm = self._exact_match(dk_name, norm, team, roster)
        if pm:
            await self._cache_set(cache_key, pm)
            return pm

        # 4. Known-alias lookup
        alias_norm = KNOWN_ALIASES.get(norm)
        if alias_norm:
            pm = self._exact_match(dk_name, alias_norm, team, roster)
            if pm:
                pm.match_method = "alias"
                await self._cache_set(cache_key, pm)
                return pm

        # 5. Last-name + team fallback
        pm = self._last_name_match(dk_name, norm, team, roster)
        if pm:
            await self._cache_set(cache_key, pm)
            return pm

        # 6. Fuzzy match
        pm = self._fuzzy_match(dk_name, norm, team, roster)
        if pm:
            await self._cache_set(cache_key, pm)
            return pm

        return None

    async def invalidate(self, dk_name: str, team: str) -> None:
        """Remove a cached mapping (e.g., after a trade)."""
        norm = normalize_name(dk_name)
        key = _make_cache_key(norm, team.upper())
        if self._cache:
            await self._cache.delete(key)

    async def invalidate_all(self) -> None:
        """Clear all cached mappings."""
        if self._cache:
            await self._cache.clear_pattern(f"{CACHE_KEY_PREFIX}:*")
        self._roster_index.clear()
        self._index_built = False

    # ── Roster index management ───────────────────────────────────────

    async def _ensure_roster_index(self, teams: set) -> None:
        """Fetch BDL rosters for teams not yet in the index."""
        missing = {t for t in teams if t not in self._roster_index}
        if not missing:
            return

        for team_abbr in missing:
            players = self._fetch_team_roster(team_abbr)
            entries: List[Tuple[str, dict]] = []
            for p in players:
                full = f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
                entries.append((normalize_name(full), p))
            self._roster_index[team_abbr] = entries
            logger.debug(
                f"[PlayerMap] Indexed {len(entries)} BDL players for {team_abbr}"
            )

    def _fetch_team_roster(self, team_abbr: str) -> List[dict]:
        """Fetch roster from BDL by team abbreviation.

        Uses the BDL service's team mapping to look up by abbreviation.
        Falls back to an empty list if the team can't be resolved.
        """
        self._bdl._ensure_team_mapping()

        # Reverse-lookup: abbreviation → BDL team ID
        bdl_team_id = None
        for bdl_id, abbr in self._bdl._bdl_team_abbrs.items():
            if abbr.upper() == team_abbr.upper():
                bdl_team_id = bdl_id
                break

        if bdl_team_id is None:
            logger.warning(f"[PlayerMap] Unknown team abbreviation: {team_abbr}")
            return []

        # Fetch via the BDL v1 players endpoint filtered by team
        try:
            path = f"/players?team_ids[]={bdl_team_id}&per_page=100"
            result = self._bdl._get(path)
            return result.get("data", [])
        except Exception as e:
            logger.error(f"[PlayerMap] BDL roster fetch failed for {team_abbr}: {e}")
            return []

    # ── Matching strategies ───────────────────────────────────────────

    def _exact_match(
        self,
        dk_name: str,
        norm: str,
        team: str,
        roster: List[Tuple[str, dict]],
    ) -> Optional[PlayerMapping]:
        """Match by exact normalized name within the team roster."""
        for bdl_norm, bdl_player in roster:
            if bdl_norm == norm:
                return self._make_mapping(dk_name, bdl_player, team, "exact", 1.0)
        return None

    def _last_name_match(
        self,
        dk_name: str,
        norm: str,
        team: str,
        roster: List[Tuple[str, dict]],
    ) -> Optional[PlayerMapping]:
        """Match by last name when there's exactly one match on the team."""
        dk_last = _last_name(norm)
        candidates = [
            (bn, bp) for bn, bp in roster if _last_name(bn) == dk_last
        ]
        if len(candidates) == 1:
            return self._make_mapping(
                dk_name, candidates[0][1], team, "last_name", 0.90
            )
        return None

    def _fuzzy_match(
        self,
        dk_name: str,
        norm: str,
        team: str,
        roster: List[Tuple[str, dict]],
    ) -> Optional[PlayerMapping]:
        """Fuzzy-match using SequenceMatcher within the team roster."""
        best_ratio = 0.0
        best_player = None
        for bdl_norm, bdl_player in roster:
            ratio = SequenceMatcher(None, norm, bdl_norm).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_player = bdl_player

        if best_ratio >= self._fuzzy_threshold and best_player is not None:
            logger.info(
                f"[PlayerMap] Fuzzy matched {dk_name!r} → "
                f"{best_player.get('first_name')} {best_player.get('last_name')} "
                f"(ratio={best_ratio:.3f})"
            )
            return self._make_mapping(
                dk_name, best_player, team, "fuzzy", round(best_ratio, 3)
            )
        return None

    # ── Helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _make_mapping(
        dk_name: str,
        bdl_player: dict,
        team: str,
        method: str,
        confidence: float,
    ) -> PlayerMapping:
        return PlayerMapping(
            dk_name=dk_name,
            bdl_id=bdl_player["id"],
            bdl_first_name=bdl_player.get("first_name", ""),
            bdl_last_name=bdl_player.get("last_name", ""),
            team=team,
            match_method=method,
            confidence=confidence,
        )

    async def _cache_get(self, key: str) -> Optional[dict]:
        if not self._cache:
            return None
        return await self._cache.get(key)

    async def _cache_set(self, key: str, pm: PlayerMapping) -> None:
        if not self._cache:
            return
        await self._cache.set(
            key,
            {
                "bdl_id": pm.bdl_id,
                "first": pm.bdl_first_name,
                "last": pm.bdl_last_name,
                "conf": pm.confidence,
            },
            ttl=CACHE_TTL,
        )
