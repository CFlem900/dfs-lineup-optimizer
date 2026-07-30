"""Service for fetching DraftKings player salary (draftables) data.

Endpoint:
    GET https://api.draftkings.com/draftgroups/v1/draftables?draftGroupId={id}

Returns player salaries, positions, and statuses for a specific DraftGroup.
Salaries are published once per slate and never change, so we cache them
for the entire day keyed by draft_group_id.
"""

import logging
import time
from datetime import date
from typing import Dict, List, Optional

import httpx

from app.services.http_resilience import resilient_get, APIGroup
from app.utils.helpers import normalize_player_name

logger = logging.getLogger(__name__)

DK_DRAFTABLES_URL = "https://api.draftkings.com/draftgroups/v1/draftgroups/{dg_id}/draftables"

# Reuse HTTP settings from dk_slate_service
REQUEST_TIMEOUT = 15
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


class DKPlayerSalary:
    """Parsed salary data for a single DK draftable player.

    ``scoring_class`` is populated for sports with polymorphic scoring
    (currently MLB: ``"hitter"`` or ``"pitcher"``). For NBA/CBB/NFL it's
    ``None`` because every player on those slates uses the same scoring
    formula. The lineup builder reads this when calling
    ``DFSService._calculate_score`` so a pitcher entered at slot ``P``
    is scored on pitcher stats only — see Prompt 2.1.
    """

    __slots__ = (
        "dk_player_id",
        "display_name",
        "position",
        "salary",
        "team_abbreviation",
        "status",
        "scoring_class",
    )

    def __init__(
        self,
        dk_player_id: int,
        display_name: str,
        position: str,
        salary: int,
        team_abbreviation: str,
        status: Optional[str] = None,
        scoring_class: Optional[str] = None,
    ):
        self.dk_player_id = dk_player_id
        self.display_name = display_name
        self.position = position
        self.salary = salary
        self.team_abbreviation = team_abbreviation
        self.status = status
        self.scoring_class = scoring_class


# Use the shared normalize_player_name from utils.helpers.
# Keep this alias for backward compatibility within this module.
_normalize_name = normalize_player_name


class DKDraftablesService:
    """Fetches and caches DraftKings player salaries for a DraftGroup."""

    def __init__(self):
        # Cache keyed by draft_group_id
        self._cache: Dict[int, List[DKPlayerSalary]] = {}
        self._cache_date: Optional[str] = None

    def _is_cache_valid(self, draft_group_id: int) -> bool:
        today = date.today().isoformat()
        if self._cache_date != today:
            self._cache.clear()
            self._cache_date = today
            return False
        return draft_group_id in self._cache

    def get_draftables(
        self,
        draft_group_id: int,
        force_refresh: bool = False,
        sport: str = "nba",
    ) -> List[DKPlayerSalary]:
        """Get player salaries for a DraftGroup.

        Cached for the entire day since DK salaries don't change
        once published.  Use ``force_refresh=True`` to re-fetch from
        the API (useful for getting updated injury statuses which DO
        change throughout the day).

        The ``sport`` parameter routes the parser to the correct
        ``pos_to_class`` map so MLB pitchers get tagged with
        ``scoring_class="pitcher"`` etc. NBA/CBB/NFL are sport-agnostic
        from the parser's perspective (no polymorphic scoring).
        """
        if not force_refresh and self._is_cache_valid(draft_group_id):
            return self._cache[draft_group_id]

        try:
            players = self._fetch_draftables(draft_group_id, sport=sport)
            self._cache[draft_group_id] = players
            self._cache_date = date.today().isoformat()
            if force_refresh:
                logger.info(
                    f"Force-refreshed {len(players)} draftable players "
                    f"for DG {draft_group_id} (status update)"
                )
            else:
                logger.info(
                    f"Fetched {len(players)} draftable players for DG {draft_group_id}"
                )
            return players
        except Exception as e:
            logger.error(
                f"DK draftables fetch failed for DG {draft_group_id}: {e}"
            )
            return self._cache.get(draft_group_id, [])

    def _fetch_draftables(
        self, draft_group_id: int, sport: str = "nba",
    ) -> List[DKPlayerSalary]:
        """Fetch draftables from DK API and parse via :meth:`parse_draftables_payload`."""
        url = DK_DRAFTABLES_URL.format(dg_id=draft_group_id)
        resp = resilient_get(
            url,
            group=APIGroup.DRAFTKINGS,
            headers={"User-Agent": USER_AGENT},
        )
        data = resp.json()
        return self.parse_draftables_payload(data, sport=sport)

    @classmethod
    def parse_draftables_payload(
        cls, data: Dict, sport: str = "nba",
    ) -> List[DKPlayerSalary]:
        """Parse a raw DK draftables JSON payload into ``DKPlayerSalary``.

        Public surface so tooling (the ``debug.mock-ingest`` endpoint,
        unit tests, replay scripts) can feed the parser a saved payload
        and verify the parse without a live network call.

        Sport-aware behaviour: when the registered ``SportConfig`` has a
        ``pos_to_class`` map (currently MLB only), each player's position
        is mapped to a ``scoring_class`` (``"hitter"`` / ``"pitcher"``)
        and stored on ``DKPlayerSalary.scoring_class``. Other sports
        leave it ``None``.
        """
        from app.sports import get_config as _get_sport_cfg
        try:
            cfg = _get_sport_cfg(sport)
            pos_to_class = cfg.pos_to_class or {}
        except ValueError:
            pos_to_class = {}

        # NFL DST records are team-level entities (Cowboys, 49ers, etc.)
        # not individual players. DK gives them inconsistent display names
        # ("Cowboys", "Dallas Cowboys", " Cowboys", or even nothing with
        # the team in shortName) and a CSV uploader may use a different
        # spelling. Normalize them via the NFL team table so the import
        # alias system doesn't have to fight DK's formatting.
        nfl_team_lookup: Optional[Dict[str, Dict]] = None
        if sport == "nfl":
            try:
                from app.services.nfl_data_service import NFLDataService
                _nfl = NFLDataService()
                nfl_team_lookup = {
                    t["abbreviation"].upper(): t for t in _nfl.get_all_teams()
                }
            except Exception:
                nfl_team_lookup = None

        draftables = data.get("draftables", [])
        players: List[DKPlayerSalary] = []

        for d in draftables:
            salary = d.get("salary")
            if salary is None or salary <= 0:
                continue

            display_name = d.get("displayName") or d.get("shortName") or ""
            if not display_name:
                continue

            position = d.get("position", "") or ""
            scoring_cls: Optional[str] = None
            if pos_to_class:
                # Position can be a slash-joined dual-eligibility string
                # (e.g. "1B/OF" in MLB or "G/F" in CBB). Use the first
                # token's class — DK lists primary position first.
                primary = position.split("/")[0].strip().upper()
                scoring_cls = pos_to_class.get(primary)

            team_abbr = (d.get("teamAbbreviation") or "").strip()

            # ── NFL DST canonicalization (verified for 2026 DK) ──
            # DK ships defense entries in two ways:
            #   1. position="DST" with displayName="Cowboys" or "Dallas Cowboys"
            #   2. position blank (or odd value) with displayName ending in
            #      " DST" — e.g. "Cowboys DST" / "Dallas Cowboys DST".
            # We detect both forms (suffix OR explicit position) and
            # normalize to "<full team name> DST" so projection-CSV
            # matching is consistent regardless of which DK variant the
            # row arrived as.
            display_lower = display_name.strip().lower()
            looks_like_dst = (
                position.upper() == "DST"
                or display_lower.endswith(" dst")
                or display_lower.endswith(" defense")
            )
            if (
                sport == "nfl"
                and looks_like_dst
                and nfl_team_lookup
                and team_abbr.upper() in nfl_team_lookup
            ):
                # Canonical form preserves the " DST" suffix DK uses.
                # If the row's position field was blank, also write back
                # "DST" so downstream slot-eligibility and scoring
                # ("dst_*" coefficients) route correctly.
                display_name = (
                    f"{nfl_team_lookup[team_abbr.upper()]['full_name']} DST"
                )
                if position.upper() != "DST":
                    position = "DST"

            players.append(
                DKPlayerSalary(
                    dk_player_id=d.get("draftableId", 0),
                    display_name=display_name.strip(),
                    position=position,
                    salary=salary,
                    team_abbreviation=team_abbr,
                    status=d.get("status"),
                    scoring_class=scoring_cls,
                )
            )

        return players

    def inject_mock_payload(
        self, draft_group_id: int, data: Dict, sport: str = "nba",
    ) -> List[DKPlayerSalary]:
        """Parse a payload and write it directly into the per-DG cache.

        Used by the ``/api/debug/mock-ingest`` endpoint so test/dev
        flows can populate the in-memory pool without a live DK fetch.
        Returns the parsed list so the caller can inspect what was
        ingested (counts by position, scoring-class breakdown, etc.).
        """
        players = self.parse_draftables_payload(data, sport=sport)
        self._cache[draft_group_id] = players
        self._cache_date = date.today().isoformat()
        logger.info(
            "[MockIngest] Injected %d draftables for DG %s (sport=%s)",
            len(players), draft_group_id, sport,
        )
        return players

    def build_salary_lookup(
        self, draft_group_id: int
    ) -> Dict[str, "DKPlayerSalary"]:
        """Build a lookup dict for matching against our roster.

        Keys are normalized ``"firstname lastname:TEAM"`` and
        ``"lastname:TEAM"`` strings.  When duplicates exist the
        highest-salaried entry wins.
        """
        draftables = self.get_draftables(draft_group_id)
        lookup: Dict[str, DKPlayerSalary] = {}

        for p in draftables:
            team = p.team_abbreviation.upper()
            normalized = _normalize_name(p.display_name)

            # Key 1: full normalized name + team
            full_key = f"{normalized}:{team}"
            if full_key not in lookup or p.salary > lookup[full_key].salary:
                lookup[full_key] = p

            # Key 2: last name + team (fallback for partial name matches)
            parts = normalized.split()
            if parts:
                last_key = f"{parts[-1]}:{team}"
                # Only use last-name key if no conflict yet
                if last_key not in lookup or p.salary > lookup[last_key].salary:
                    lookup[last_key] = p

        return lookup

    def match_salary(
        self,
        player_name: str,
        team_abbreviation: str,
        lookup: Dict[str, "DKPlayerSalary"],
    ) -> Optional["DKPlayerSalary"]:
        """Match a player from our roster to a DK draftable.

        Tries full-name match first, then last-name fallback.
        """
        team = team_abbreviation.upper()
        normalized = _normalize_name(player_name)

        # Try full name match
        full_key = f"{normalized}:{team}"
        match = lookup.get(full_key)
        if match:
            return match

        # Fallback: last name + team
        parts = normalized.split()
        if parts:
            last_key = f"{parts[-1]}:{team}"
            return lookup.get(last_key)

        return None

    # ------------------------------------------------------------------
    # Pre-lock status refresh & roster change detection
    # ------------------------------------------------------------------

    def refresh_draftables(self, draft_group_id: int) -> List[DKPlayerSalary]:
        """Force a fresh fetch of DK draftables, bypassing the daily cache.

        Used near game lock to pick up status changes (e.g. "O" for
        newly ruled-out or suspended players, newly added replacements).
        Salaries don't change, but the **status** field and the set of
        listed players can change up to minutes before lock.
        """
        try:
            players = self._fetch_draftables(draft_group_id)
            self._cache[draft_group_id] = players
            self._cache_date = date.today().isoformat()
            logger.info(
                f"Force-refreshed {len(players)} draftable players "
                f"for DG {draft_group_id}"
            )
            return players
        except Exception as e:
            logger.error(
                f"DK draftables force-refresh failed for "
                f"DG {draft_group_id}: {e}"
            )
            return self._cache.get(draft_group_id, [])

    def detect_status_changes(
        self,
        draft_group_id: int,
        previous_players: List[DKPlayerSalary],
    ) -> Dict[str, list]:
        """Compare fresh draftables against a previous snapshot.

        Returns a dict with three keys:
          - ``newly_out``:   Players whose status changed TO "O"/"OUT"
          - ``newly_added``: Players in the new fetch but NOT in previous
          - ``newly_removed``: Players in previous but NOT in the new fetch

        This enables the late-swap monitor to detect:
          - Last-minute ruled-out / suspended players
          - Emergency roster additions (G-League call-ups, signings)
          - Players removed from the DK pool (ineligible, waived)
        """
        fresh = self.refresh_draftables(draft_group_id)

        prev_by_id = {p.dk_player_id: p for p in previous_players}
        fresh_by_id = {p.dk_player_id: p for p in fresh}

        newly_out = []
        for pid, fp in fresh_by_id.items():
            old = prev_by_id.get(pid)
            fresh_status = (fp.status or "").strip().upper()
            old_status = (old.status or "").strip().upper() if old else ""
            # Detect transition to Out (wasn't Out before)
            if fresh_status in ("O", "OUT") and old_status not in ("O", "OUT"):
                newly_out.append({
                    "player": fp.display_name,
                    "team": fp.team_abbreviation,
                    "salary": fp.salary,
                    "old_status": old_status or "None",
                    "new_status": fresh_status,
                })

        prev_ids = set(prev_by_id.keys())
        fresh_ids = set(fresh_by_id.keys())

        newly_added = [
            {
                "player": fresh_by_id[pid].display_name,
                "team": fresh_by_id[pid].team_abbreviation,
                "salary": fresh_by_id[pid].salary,
                "status": fresh_by_id[pid].status,
            }
            for pid in (fresh_ids - prev_ids)
        ]

        newly_removed = [
            {
                "player": prev_by_id[pid].display_name,
                "team": prev_by_id[pid].team_abbreviation,
                "salary": prev_by_id[pid].salary,
            }
            for pid in (prev_ids - fresh_ids)
        ]

        if newly_out or newly_added or newly_removed:
            logger.info(
                f"DK roster changes detected for DG {draft_group_id}: "
                f"{len(newly_out)} newly out, "
                f"{len(newly_added)} added, "
                f"{len(newly_removed)} removed"
            )

        return {
            "newly_out": newly_out,
            "newly_added": newly_added,
            "newly_removed": newly_removed,
        }
