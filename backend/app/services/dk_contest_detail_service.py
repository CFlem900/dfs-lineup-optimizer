"""Service for fetching detailed DraftKings contest metadata.

Endpoint:
    GET https://api.draftkings.com/contests/v1/contests/{id}?format=json

Returns prize structure, entry counts, max entries per user, guaranteed
prize pool, and other metadata that enables smarter contest selection
and strategy auto-detection.

Caching: 30-minute in-memory TTL (contest metadata is stable once published).
"""

import logging
import time
from typing import Dict, List, Optional

import httpx

from app.services.http_resilience import resilient_get, APIGroup

logger = logging.getLogger(__name__)

DK_CONTEST_DETAIL_URL = (
    "https://api.draftkings.com/contests/v1/contests/{contest_id}?format=json"
)

# HTTP settings (match existing DK services)
REQUEST_TIMEOUT = 15
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


class ContestPrizeLevel:
    """A single level in a contest prize structure."""

    __slots__ = ("min_rank", "max_rank", "prize")

    def __init__(self, min_rank: int, max_rank: int, prize: float):
        self.min_rank = min_rank
        self.max_rank = max_rank
        self.prize = prize


class ContestDetail:
    """Full contest metadata from DK API."""

    __slots__ = (
        "contest_id",
        "contest_name",
        "draft_group_id",
        "entry_fee",
        "total_prizes",
        "max_entries",
        "max_entries_per_user",
        "current_entries",
        "contest_type",
        "is_guaranteed",
        "prize_structure",
        "overlay_risk",
        "top_heavy_pct",
        "roi_breakeven_percentile",
    )

    def __init__(
        self,
        contest_id: str,
        contest_name: str,
        draft_group_id: int,
        entry_fee: float,
        total_prizes: float,
        max_entries: int,
        max_entries_per_user: int,
        current_entries: int,
        contest_type: str,
        is_guaranteed: bool,
        prize_structure: List[ContestPrizeLevel],
    ):
        self.contest_id = contest_id
        self.contest_name = contest_name
        self.draft_group_id = draft_group_id
        self.entry_fee = entry_fee
        self.total_prizes = total_prizes
        self.max_entries = max_entries
        self.max_entries_per_user = max_entries_per_user
        self.current_entries = current_entries
        self.contest_type = contest_type
        self.is_guaranteed = is_guaranteed
        self.prize_structure = prize_structure

        # Compute derived metrics
        self.overlay_risk = self._calc_overlay_risk()
        self.top_heavy_pct = self._calc_top_heavy_pct()
        self.roi_breakeven_percentile = self._calc_breakeven_percentile()

    def _calc_overlay_risk(self) -> float:
        """Percent of entries unfilled vs max capacity."""
        if self.max_entries <= 0:
            return 0.0
        return max(0.0, 1.0 - self.current_entries / self.max_entries) * 100

    def _calc_top_heavy_pct(self) -> float:
        """Percent of total prizes concentrated in top 1% of positions."""
        if not self.prize_structure or self.total_prizes <= 0:
            return 0.0
        top_1pct_cutoff = max(1, int(self.max_entries * 0.01))
        top_prize_sum = sum(
            level.prize * (level.max_rank - level.min_rank + 1)
            for level in self.prize_structure
            if level.min_rank <= top_1pct_cutoff
        )
        return min(100.0, top_prize_sum / self.total_prizes * 100)

    def _calc_breakeven_percentile(self) -> float:
        """What percentile you need to reach to break even on entry fee."""
        if not self.prize_structure or self.entry_fee <= 0 or self.max_entries <= 0:
            return 0.0
        # Find the rank where prize >= entry_fee
        breakeven_rank = self.max_entries
        for level in self.prize_structure:
            if level.prize >= self.entry_fee:
                breakeven_rank = level.max_rank
        return (breakeven_rank / self.max_entries) * 100

    def inferred_contest_type(self) -> str:
        """Infer DFS contest type for strategy selection.

        Returns 'gpp', 'cash', or 'single_entry' based on contest
        structure.
        """
        name_lower = self.contest_name.lower()

        if self.max_entries_per_user == 1:
            return "single_entry"

        # Cash game indicators
        if any(kw in name_lower for kw in ["double up", "50/50", "head-to-head", "h2h"]):
            return "cash"

        # GPP indicators
        if any(kw in name_lower for kw in ["tournament", "milly", "millionaire", "gpp"]):
            return "gpp"

        # Top-heavy prize structure = GPP
        if self.top_heavy_pct > 30:
            return "gpp"

        # Flat payout = cash
        if self.top_heavy_pct < 10:
            return "cash"

        return "gpp"  # default


class DKContestDetailService:
    """Fetches and caches DraftKings contest metadata."""

    def __init__(self):
        # Cache keyed by contest_id
        self._cache: Dict[str, ContestDetail] = {}
        self._cache_timestamps: Dict[str, float] = {}
        self._cache_ttl = 1800  # 30 minutes

    def _is_cache_valid(self, contest_id: str) -> bool:
        if contest_id not in self._cache:
            return False
        ts = self._cache_timestamps.get(contest_id, 0)
        return (time.time() - ts) < self._cache_ttl

    def get_contest_detail(self, contest_id: str) -> Optional[ContestDetail]:
        """Get detailed metadata for a specific DK contest.

        Cached for 30 minutes.
        """
        if self._is_cache_valid(contest_id):
            return self._cache[contest_id]

        try:
            detail = self._fetch_contest_detail(contest_id)
            if detail:
                self._cache[contest_id] = detail
                self._cache_timestamps[contest_id] = time.time()
                logger.info(
                    f"[Contest] Fetched detail for contest {contest_id}: "
                    f"{detail.contest_name} ({detail.current_entries}/{detail.max_entries} entries)"
                )
            return detail
        except Exception as e:
            logger.error(f"[Contest] Fetch failed for {contest_id}: {e}")
            return self._cache.get(contest_id)

    def get_contest_type(self, contest_id: str) -> Optional[str]:
        """Get the inferred DFS contest type ('gpp', 'cash', 'single_entry')."""
        detail = self.get_contest_detail(contest_id)
        if detail:
            return detail.inferred_contest_type()
        return None

    def _fetch_contest_detail(self, contest_id: str) -> Optional[ContestDetail]:
        """Fetch contest detail from DK API."""
        url = DK_CONTEST_DETAIL_URL.format(contest_id=contest_id)

        resp = resilient_get(
            url,
            group=APIGroup.DRAFTKINGS,
            headers={"User-Agent": USER_AGENT},
        )
        data = resp.json()

        return self._parse_contest(data, contest_id)

    def _parse_contest(
        self, data: dict, contest_id: str
    ) -> Optional[ContestDetail]:
        """Parse contest detail API response."""
        # DK returns contest data in various structures
        contest = data.get("contestDetail") or data.get("contest") or data
        if not contest:
            return None

        # Extract prize structure
        prize_data = contest.get("payoutStructure") or contest.get("prizes") or []
        prize_structure = self._parse_prize_structure(prize_data)

        # Contest type from DK
        contest_type = (
            contest.get("contestTypeName")
            or contest.get("gameType")
            or "Tournament"
        )

        return ContestDetail(
            contest_id=str(contest_id),
            contest_name=contest.get("name") or contest.get("contestName") or "",
            draft_group_id=contest.get("draftGroupId") or contest.get("dg") or 0,
            entry_fee=float(contest.get("entryFee") or contest.get("a") or 0),
            total_prizes=float(
                contest.get("totalPrizes")
                or contest.get("totalPayouts")
                or contest.get("tp")
                or 0
            ),
            max_entries=int(
                contest.get("maxEntries")
                or contest.get("maximumEntries")
                or contest.get("m")
                or 0
            ),
            max_entries_per_user=int(
                contest.get("maxEntriesPerUser")
                or contest.get("maximumEntriesPerUser")
                or contest.get("mec")
                or 1
            ),
            current_entries=int(
                contest.get("currentEntries")
                or contest.get("entries")
                or contest.get("ec")
                or 0
            ),
            contest_type=str(contest_type),
            is_guaranteed=bool(
                contest.get("isGuaranteed")
                or contest.get("guaranteed")
                or False
            ),
            prize_structure=prize_structure,
        )

    def _parse_prize_structure(
        self, prize_data
    ) -> List[ContestPrizeLevel]:
        """Parse prize payout structure."""
        levels = []
        if not isinstance(prize_data, list):
            return levels

        for entry in prize_data:
            try:
                min_rank = int(entry.get("minPosition") or entry.get("min") or 0)
                max_rank = int(entry.get("maxPosition") or entry.get("max") or 0)
                prize = float(entry.get("prize") or entry.get("amount") or 0)

                if min_rank > 0 and prize > 0:
                    levels.append(ContestPrizeLevel(
                        min_rank=min_rank,
                        max_rank=max_rank or min_rank,
                        prize=prize,
                    ))
            except (ValueError, TypeError):
                continue

        return sorted(levels, key=lambda l: l.min_rank)
