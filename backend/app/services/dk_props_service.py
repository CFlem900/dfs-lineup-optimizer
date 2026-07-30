"""Service for fetching DraftKings Sportsbook player prop lines.

Retrieves Over/Under lines for NBA player stats (pts, reb, ast, PRA,
DD, TD) from the DK Sportsbook public API.  Props represent
Vegas-calibrated projections that can cross-validate our stat
projections, improve DD/TD probability estimates, and provide
market-anchored floor/ceiling ranges.

Endpoints used:
    - GET https://sportsbook-hamilton.draftkings.com/api/sportscontent/dkusnj/v1/sports/basketball/nba/events
        Returns NBA game events with player prop offerings.

    - GET https://sportsbook.draftkings.com/sites/US-SB/api/v5/eventgroup/{eventGroupId}/categories/{categoryId}
        Returns prop categories and subcategories for a specific event group.

Caching: 15-minute in-memory TTL (props move intraday).
"""

import logging
import re
import threading
import time
import unicodedata
from typing import Dict, List, Optional, Set, Tuple

import httpx

from app.models.props import PlayerPropLine, PlayerProps, PropsComparison
from app.services.http_resilience import resilient_get, APIGroup

logger = logging.getLogger(__name__)

# DK Sportsbook API endpoints
DK_SB_EVENT_GROUP_URL = (
    "https://sportsbook.draftkings.com/sites/US-SB/api/v5/"
    "eventgroup/{event_group_id}/categories/{category_id}"
)
DK_SB_SUBCATEGORY_URL = (
    "https://sportsbook.draftkings.com/sites/US-SB/api/v5/"
    "eventgroup/{event_group_id}/categories/{category_id}/"
    "subcategories/{subcategory_id}"
)

# Alternative API that may be more stable / provide more data
DK_SB_OFFERS_URL = (
    "https://sportsbook-nash.draftkings.com/sites/US-SB/api/v5/"
    "eventgroup/{event_group_id}/categories/{category_id}"
)

# DK Sportsbook event group IDs by sport
NBA_EVENT_GROUP_ID = 42648
CBB_EVENT_GROUP_ID = 92483  # NCAA Men's Basketball (may shift seasonally)

# Known DK Sportsbook category IDs for player props
# These may shift — the service discovers them dynamically and falls back to these
# NBA and CBB share the same category structure on DK Sportsbook
KNOWN_CATEGORIES: Dict[str, int] = {
    "pts": 1215,      # Points
    "reb": 1216,      # Rebounds
    "ast": 1217,      # Assists
    "fg3m": 1218,     # Three-Pointers Made
    "pra": 1219,      # Pts + Reb + Ast
    "pr": 1220,       # Pts + Reb
    "pa": 1221,       # Pts + Ast
    "ra": 1222,       # Reb + Ast
    "stl": 1223,      # Steals
    "blk": 1224,      # Blocks
    "tov": 1225,      # Turnovers
    "dd": 1226,       # Double-Double
    "td": 1227,       # Triple-Double
}

# Stats we care about for projection calibration
PROJECTION_STATS = ["pts", "reb", "ast"]
DD_TD_STATS = ["dd", "td"]
ALL_TRACKED_STATS = ["pts", "reb", "ast", "pra", "fg3m", "dd", "td"]

# HTTP settings (match existing DK services)
REQUEST_TIMEOUT = 15
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


def _normalize_name(name: str) -> str:
    """Normalize a player name for matching.

    Reuses the same logic as dk_draftables_service to ensure consistent
    name matching across all DK data sources.
    """
    n = name.strip().lower()
    n = unicodedata.normalize("NFKD", n)
    n = "".join(c for c in n if not unicodedata.combining(c))
    n = re.sub(r"\b(jr\.?|sr\.?|iii|iv|ii)\s*$", "", n).strip()
    n = n.replace(".", "")
    n = re.sub(r"\s+", " ", n).strip()
    return n


class DKPropsService:
    """Fetches and caches DraftKings Sportsbook player prop lines.

    Parameters
    ----------
    event_group_id : int, optional
        DK Sportsbook event group ID.  Defaults to NBA.
        Override with ``CBB_EVENT_GROUP_ID`` for college basketball.
    """

    def __init__(self, event_group_id: int = NBA_EVENT_GROUP_ID):
        self._event_group_id = event_group_id
        # Cache: normalized_name:TEAM -> PlayerProps
        self._cache: Dict[str, PlayerProps] = {}
        self._cache_date: Optional[str] = None
        self._cache_timestamp: float = 0.0
        self._cache_ttl = 900  # 15 minutes
        self._category_cache: Dict[str, int] = {}
        # Lock to prevent concurrent duplicate fetches from thread pool
        self._fetch_lock = threading.Lock()
        # Track categories that returned 404 (skip for rest of session)
        self._dead_categories: Set[str] = set()
        self._dead_categories_date: Optional[str] = None

    def _is_cache_valid(self, game_date: str) -> bool:
        if self._cache_date != game_date:
            return False
        return (time.time() - self._cache_timestamp) < self._cache_ttl

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_player_props(
        self, game_date: Optional[str] = None
    ) -> Dict[str, PlayerProps]:
        """Get all player props for today's NBA games.

        Returns a lookup dict keyed by ``"normalized_name:TEAM"``.
        Returns empty dict if sportsbook API is unreachable.

        Thread-safe: uses a lock to prevent concurrent duplicate fetches
        when multiple rotation projections run in the thread pool.
        """
        from datetime import date as date_cls
        target = game_date or date_cls.today().isoformat()

        # Fast path: cache hit (no lock needed for reads)
        if self._is_cache_valid(target):
            return self._cache

        # Slow path: acquire lock so only one thread fetches
        with self._fetch_lock:
            # Double-check after acquiring lock (another thread may have fetched)
            if self._is_cache_valid(target):
                return self._cache

            # Reset dead categories on date change
            if self._dead_categories_date != target:
                self._dead_categories.clear()
                self._dead_categories_date = target

            try:
                props = self._fetch_all_props()
                # Cache even if empty — prevents repeated fetches when
                # all categories return 404 (e.g. too early in the day).
                self._cache = props
                self._cache_date = target
                self._cache_timestamp = time.time()
                if props:
                    logger.info(
                        f"[Props] Fetched props for {len(props)} players"
                    )
                else:
                    logger.info(
                        "[Props] No props available (all categories empty/404)"
                    )
                return props
            except Exception as e:
                logger.error(f"[Props] Fetch failed: {e}")
                return self._cache if self._cache else {}

    def get_player_prop(
        self, player_name: str, team: str, game_date: Optional[str] = None
    ) -> Optional[PlayerProps]:
        """Get props for a specific player."""
        lookup = self.get_player_props(game_date)
        key = f"{_normalize_name(player_name)}:{team.upper()}"
        return lookup.get(key)

    def compare_projections(
        self,
        player_name: str,
        team: str,
        projected_stats: Dict[str, float],
        game_date: Optional[str] = None,
    ) -> List[PropsComparison]:
        """Compare our stat projections against market lines for a player.

        Returns a list of PropsComparison objects, one per stat where
        both our projection and a market line exist.
        """
        props = self.get_player_prop(player_name, team, game_date)
        if not props:
            return []

        comparisons = []
        for stat in PROJECTION_STATS:
            our_val = projected_stats.get(stat, 0.0)
            line = props.get_line(stat)
            if not line or our_val <= 0:
                continue

            delta = our_val - line.line
            delta_pct = (delta / line.line * 100) if line.line > 0 else 0.0

            if abs(delta_pct) <= 5.0:
                signal = "aligned"
            elif delta_pct > 5.0:
                signal = "bullish"
            else:
                signal = "bearish"

            comparisons.append(PropsComparison(
                player_name=player_name,
                team_abbreviation=team.upper(),
                stat=stat,
                our_projection=round(our_val, 1),
                market_line=line.line,
                delta=round(delta, 1),
                delta_pct=round(delta_pct, 1),
                implied_over_prob=line.implied_over_prob,
                signal=signal,
            ))

        return comparisons

    # ------------------------------------------------------------------
    # Fetching
    # ------------------------------------------------------------------

    def _fetch_all_props(self) -> Dict[str, PlayerProps]:
        """Fetch player props across all tracked stat categories.

        Iterates through known category IDs for pts, reb, ast, etc.,
        fetches the offerings, and merges them into a unified lookup.
        Skips categories previously marked as dead (404) for this date.
        """
        all_props: Dict[str, PlayerProps] = {}

        for stat, cat_id in KNOWN_CATEGORIES.items():
            if stat not in ALL_TRACKED_STATS:
                continue
            if stat in self._dead_categories:
                continue  # Skip categories that returned 404 today

            try:
                stat_props = self._fetch_category_props(stat, cat_id)
                # Merge into all_props
                for key, player_props in stat_props.items():
                    if key in all_props:
                        all_props[key].props.extend(player_props.props)
                    else:
                        all_props[key] = player_props
            except Exception as e:
                logger.warning(f"[Props] Failed to fetch {stat} (cat {cat_id}): {e}")
                continue

        return all_props

    def _fetch_category_props(
        self, stat: str, category_id: int
    ) -> Dict[str, PlayerProps]:
        """Fetch props for a single stat category from DK Sportsbook."""
        url = DK_SB_EVENT_GROUP_URL.format(
            event_group_id=self._event_group_id,
            category_id=category_id,
        )

        try:
            resp = resilient_get(
                url,
                group=APIGroup.DRAFTKINGS,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "application/json",
                },
            )
            data = resp.json()
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            if status in (403, 451):
                logger.warning(
                    f"[Props] Sportsbook API returned {status} "
                    f"for {stat} — may be geo-restricted"
                )
            elif status == 404:
                # Category not available — mark dead so we don't retry
                self._dead_categories.add(stat)
                logger.info(
                    f"[Props] Category '{stat}' (cat {category_id}) returned "
                    f"404 — marking dead for today"
                )
            else:
                logger.warning(f"[Props] HTTP error for {stat}: {e}")
            return {}
        except Exception as e:
            logger.warning(f"[Props] Request failed for {stat}: {e}")
            return {}

        return self._parse_category_response(data, stat)

    def _parse_category_response(
        self, data: dict, stat: str
    ) -> Dict[str, PlayerProps]:
        """Parse a DK Sportsbook category API response into PlayerProps.

        The response format varies but generally contains:
        - eventGroup.offerCategories[].offerSubcategoryDescriptors[].offerSubcategory.offers[][]
        - Each offer has outcomes with participant names, lines, and odds
        """
        result: Dict[str, PlayerProps] = {}

        # Navigate the nested response structure
        event_group = data.get("eventGroup", {})
        offer_categories = event_group.get("offerCategories", [])

        for category in offer_categories:
            subcategory_descriptors = category.get(
                "offerSubcategoryDescriptors", []
            )
            for descriptor in subcategory_descriptors:
                subcategory = descriptor.get("offerSubcategory", {})
                offers_grid = subcategory.get("offers", [])

                for offers_row in offers_grid:
                    for offer in offers_row:
                        self._parse_offer(offer, stat, result)

        return result

    def _parse_offer(
        self, offer: dict, stat: str, result: Dict[str, PlayerProps]
    ) -> None:
        """Parse a single offer (one player's O/U for one stat)."""
        outcomes = offer.get("outcomes", [])
        if len(outcomes) < 2:
            return

        # Find Over and Under outcomes
        over_outcome = None
        under_outcome = None
        for outcome in outcomes:
            label = (outcome.get("label") or "").lower()
            if label == "over":
                over_outcome = outcome
            elif label == "under":
                under_outcome = outcome

        if not over_outcome or not under_outcome:
            return

        # Extract line value
        line = over_outcome.get("line") or under_outcome.get("line")
        if line is None:
            return

        # Extract player name and team from the outcome or offer
        player_name = over_outcome.get("participant") or ""
        if not player_name:
            # Try the offer-level label
            offer_label = offer.get("label") or ""
            # DK format: "Player Name - Stat Category"
            if " - " in offer_label:
                player_name = offer_label.split(" - ")[0].strip()
            else:
                player_name = offer_label.strip()

        if not player_name:
            return

        # Extract team abbreviation from offer metadata
        team = ""
        source_text = offer.get("sourceText") or ""
        # DK sometimes includes team in sourceText like "LAL" or "Los Angeles Lakers"
        if source_text:
            parts = source_text.split()
            if parts and len(parts[0]) <= 3:
                team = parts[0].upper()

        # Try to get team from the event/criterion data
        if not team:
            criterion = offer.get("criterion", {})
            event_label = criterion.get("eventLabel") or ""
            # Event labels are like "CHI @ BOS"
            # We may not know which team the player is on from this alone
            # Fall back to team from participant metadata
            participant_data = over_outcome.get("participantMetadata", {})
            team = (participant_data.get("teamAbbreviation") or "").upper()

        # Extract American odds
        over_odds = self._parse_odds(over_outcome.get("oddsAmerican") or "")
        under_odds = self._parse_odds(under_outcome.get("oddsAmerican") or "")

        # Convert to implied probabilities (vig-removed)
        if over_odds != 0 and under_odds != 0:
            over_prob, under_prob = self._vig_removed_probs(over_odds, under_odds)
        else:
            over_prob = 0.5
            under_prob = 0.5

        prop_line = PlayerPropLine(
            stat=stat,
            line=float(line),
            over_odds=over_odds,
            under_odds=under_odds,
            implied_over_prob=round(over_prob, 4),
            implied_under_prob=round(under_prob, 4),
        )

        # Build lookup key
        normalized = _normalize_name(player_name)
        key = f"{normalized}:{team}" if team else normalized

        if key in result:
            result[key].props.append(prop_line)
        else:
            result[key] = PlayerProps(
                player_name=player_name,
                team_abbreviation=team,
                props=[prop_line],
            )

    # ------------------------------------------------------------------
    # Odds math
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_odds(odds_str: str) -> int:
        """Parse American odds string to integer.

        Handles formats like "-110", "+120", "EVEN".
        """
        if not odds_str:
            return 0
        s = odds_str.strip().upper()
        if s == "EVEN" or s == "EV":
            return 100
        try:
            return int(s.replace("+", ""))
        except (ValueError, TypeError):
            return 0

    @staticmethod
    def _american_to_raw_prob(odds: int) -> float:
        """Convert American odds to raw implied probability (with vig).

        Negative odds (favorite): prob = -odds / (-odds + 100)
        Positive odds (underdog): prob = 100 / (odds + 100)
        """
        if odds == 0:
            return 0.5
        if odds < 0:
            return abs(odds) / (abs(odds) + 100.0)
        return 100.0 / (odds + 100.0)

    @classmethod
    def _vig_removed_probs(
        cls, over_odds: int, under_odds: int
    ) -> Tuple[float, float]:
        """Convert a two-way Over/Under market to vig-removed probabilities.

        The raw probabilities sum to > 1.0 due to bookmaker's vig.
        We normalize them to sum to exactly 1.0 for fair probabilities.
        """
        raw_over = cls._american_to_raw_prob(over_odds)
        raw_under = cls._american_to_raw_prob(under_odds)
        total = raw_over + raw_under

        if total <= 0:
            return 0.5, 0.5

        return raw_over / total, raw_under / total

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    def get_props_summary(
        self, game_date: Optional[str] = None
    ) -> Dict[str, int]:
        """Return count of players with props by stat category."""
        props = self.get_player_props(game_date)
        summary: Dict[str, int] = {}
        for player_props in props.values():
            for prop in player_props.props:
                summary[prop.stat] = summary.get(prop.stat, 0) + 1
        return summary
