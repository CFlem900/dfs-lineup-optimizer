"""Service for scraping NBA news from ESPN, NBA.com, and RotoWire.

Features:
    - 10-minute in-memory cache with full invalidation
    - Deduplication by headline hash
    - Relevance tagging (injury, trade, lineup, general)
    - Team/player entity extraction
    - Graceful degradation per source (one failing doesn't block others)
"""

import hashlib
import logging
import re
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set, Tuple

import httpx

from app.models.news import NewsItem

logger = logging.getLogger(__name__)

# ── HTTP settings ──────────────────────────────────────────────────
REQUEST_TIMEOUT = 12
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
HEADERS = {"User-Agent": USER_AGENT, "Accept": "application/json, text/html"}

# ── Cache TTL ──────────────────────────────────────────────────────
CACHE_TTL_SECONDS = 600  # 10 minutes

# ── NBA team abbreviation to team_id mapping ───────────────────────
# Using nba_api static team IDs
NBA_TEAM_LOOKUP: Dict[str, int] = {
    "ATL": 1610612737, "BOS": 1610612738, "BKN": 1610612751,
    "CHA": 1610612766, "CHI": 1610612741, "CLE": 1610612739,
    "DAL": 1610612742, "DEN": 1610612743, "DET": 1610612765,
    "GSW": 1610612744, "HOU": 1610612745, "IND": 1610612754,
    "LAC": 1610612746, "LAL": 1610612747, "MEM": 1610612763,
    "MIA": 1610612748, "MIL": 1610612749, "MIN": 1610612750,
    "NOP": 1610612740, "NYK": 1610612752, "OKC": 1610612760,
    "ORL": 1610612753, "PHI": 1610612755, "PHX": 1610612756,
    "POR": 1610612757, "SAC": 1610612758, "SAS": 1610612759,
    "TOR": 1610612761, "UTA": 1610612762, "WAS": 1610612764,
}

# Full name → abbr for entity extraction
NBA_TEAM_NAMES: Dict[str, str] = {
    "hawks": "ATL", "celtics": "BOS", "nets": "BKN",
    "hornets": "CHA", "bulls": "CHI", "cavaliers": "CLE", "cavs": "CLE",
    "mavericks": "DAL", "mavs": "DAL", "nuggets": "DEN", "pistons": "DET",
    "warriors": "GSW", "rockets": "HOU", "pacers": "IND",
    "clippers": "LAC", "lakers": "LAL", "grizzlies": "MEM",
    "heat": "MIA", "bucks": "MIL", "timberwolves": "MIN", "wolves": "MIN",
    "pelicans": "NOP", "knicks": "NYK", "thunder": "OKC",
    "magic": "ORL", "76ers": "PHI", "sixers": "PHI",
    "suns": "PHX", "blazers": "POR", "trail blazers": "POR",
    "kings": "SAC", "spurs": "SAS", "raptors": "TOR",
    "jazz": "UTA", "wizards": "WAS",
    # City names
    "atlanta": "ATL", "boston": "BOS", "brooklyn": "BKN",
    "charlotte": "CHA", "chicago": "CHI", "cleveland": "CLE",
    "dallas": "DAL", "denver": "DEN", "detroit": "DET",
    "golden state": "GSW", "houston": "HOU", "indiana": "IND",
    "los angeles clippers": "LAC", "los angeles lakers": "LAL",
    "la clippers": "LAC", "la lakers": "LAL",
    "memphis": "MEM", "miami": "MIA", "milwaukee": "MIL",
    "minnesota": "MIN", "new orleans": "NOP", "new york": "NYK",
    "oklahoma city": "OKC", "orlando": "ORL", "philadelphia": "PHI",
    "phoenix": "PHX", "portland": "POR", "sacramento": "SAC",
    "san antonio": "SAS", "toronto": "TOR", "utah": "UTA",
    "washington": "WAS",
}

# ── Relevance keywords ─────────────────────────────────────────────
INJURY_KEYWORDS = [
    "injury", "injured", "out ", "questionable", "doubtful", "day-to-day",
    "gtd", "ruled out", "sidelined", "sprain", "strain", "fracture",
    "concussion", "surgery", "achilles", "knee", "ankle", "hamstring",
    "shoulder", "back", "hip", "calf", "foot", "wrist", "illness",
    "rest", "load management", "dnp", "inactive", "miss",
]
TRADE_KEYWORDS = [
    "trade", "traded", "deal", "acquire", "sign", "signed", "waive",
    "waived", "release", "released", "free agent", "buyout", "extension",
    "contract", "two-way", "10-day", "deadline",
]
LINEUP_KEYWORDS = [
    "starting", "starter", "lineup", "bench", "rotation", "minutes",
    "role", "will start", "moved to bench", "promoted", "return",
    "returning", "back in", "expected to play", "available",
]


def _make_id(headline: str, source: str) -> str:
    """Create a deterministic ID for deduplication."""
    raw = f"{headline.strip().lower()}|{source}"
    return hashlib.md5(raw.encode()).hexdigest()[:12]


def _classify_relevance(text: str) -> str:
    """Tag a news item by its most specific relevance category."""
    lower = text.lower()
    # Check most specific first
    for kw in INJURY_KEYWORDS:
        if kw in lower:
            return "injury"
    for kw in TRADE_KEYWORDS:
        if kw in lower:
            return "trade"
    for kw in LINEUP_KEYWORDS:
        if kw in lower:
            return "lineup"
    return "general"


def _extract_teams(text: str) -> List[str]:
    """Extract NBA team abbreviations mentioned in text."""
    lower = text.lower()
    found: Set[str] = set()

    # Check abbreviations directly (case-insensitive, word-boundary)
    for abbr in NBA_TEAM_LOOKUP:
        if re.search(rf"\b{abbr}\b", text, re.IGNORECASE):
            found.add(abbr)

    # Check full names / nicknames
    for name, abbr in NBA_TEAM_NAMES.items():
        if name in lower:
            found.add(abbr)

    return sorted(found)


def _team_ids_from_abbrs(abbrs: List[str]) -> List[int]:
    """Convert team abbreviations to NBA API team IDs."""
    return [NBA_TEAM_LOOKUP[a] for a in abbrs if a in NBA_TEAM_LOOKUP]


class NewsService:
    """Fetches and aggregates NBA news from multiple sources."""

    def __init__(self):
        self._cache: List[NewsItem] = []
        self._cache_timestamp: Optional[float] = None
        self._seen_ids: Set[str] = set()

    def _is_cache_valid(self) -> bool:
        if self._cache_timestamp is None:
            return False
        return (time.time() - self._cache_timestamp) < CACHE_TTL_SECONDS

    def get_news(
        self,
        team_ids: Optional[List[int]] = None,
        player_ids: Optional[List[int]] = None,
        limit: int = 50,
    ) -> Tuple[List[NewsItem], bool]:
        """Return news items, optionally filtered by team/player IDs.

        Returns:
            (items, cached) — the list and whether it came from cache.
        """
        if not self._is_cache_valid():
            self._refresh()
            cached = False
        else:
            cached = True

        items = self._cache

        # Filter by team IDs
        if team_ids:
            tid_set = set(team_ids)
            items = [n for n in items if tid_set & set(n.team_ids)]

        # Filter by player IDs
        if player_ids:
            pid_set = set(player_ids)
            items = [n for n in items if pid_set & set(n.player_ids)]

        # Sort by published_at descending (most recent first)
        items = sorted(items, key=lambda n: n.published_at, reverse=True)

        return items[:limit], cached

    def _refresh(self):
        """Fetch from all sources, deduplicate, and update cache.

        Does NOT cache empty results — if all sources fail, the next
        request will immediately retry rather than serving stale empties.
        """
        all_items: List[NewsItem] = []
        self._seen_ids = set()
        source_status: Dict[str, str] = {}

        # Fetch from each source independently (graceful degradation)
        for fetcher, label in [
            (self._fetch_espn, "ESPN"),
            (self._fetch_nba_com, "NBA.com"),
            (self._fetch_rotowire, "RotoWire"),
        ]:
            try:
                items = fetcher()
                # Deduplicate
                count = 0
                for item in items:
                    if item.id not in self._seen_ids:
                        self._seen_ids.add(item.id)
                        all_items.append(item)
                        count += 1
                source_status[label] = f"{count} items"
                logger.info(f"[News] {label}: fetched {count} items")
            except Exception as e:
                source_status[label] = f"FAILED: {e}"
                logger.warning(f"[News] {label} failed: {e}")

        # Only cache if we got at least some results
        if all_items:
            self._cache = all_items
            self._cache_timestamp = time.time()
        else:
            # Don't cache empty results — next request will retry immediately
            logger.warning(
                f"[News] All sources returned 0 items — NOT caching. "
                f"Status: {source_status}"
            )
            self._cache = []
            self._cache_timestamp = None  # Forces re-fetch on next request

        logger.info(f"[News] Refresh complete: {len(all_items)} total items — {source_status}")

    # ── ESPN ────────────────────────────────────────────────────────

    def _fetch_espn(self) -> List[NewsItem]:
        """Fetch NBA news from ESPN's public API."""
        url = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/news"
        resp = httpx.get(url, timeout=REQUEST_TIMEOUT, headers=HEADERS, follow_redirects=True)
        resp.raise_for_status()
        data = resp.json()

        items = []
        for article in data.get("articles", []):
            headline = article.get("headline", "").strip()
            if not headline:
                continue

            summary = article.get("description", "")
            pub_str = article.get("published", "")
            try:
                pub_dt = datetime.fromisoformat(pub_str.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                pub_dt = datetime.now(timezone.utc)

            link = ""
            try:
                links_obj = article.get("links", {})
                if isinstance(links_obj, dict):
                    web_obj = links_obj.get("web", {})
                    if isinstance(web_obj, dict):
                        link = web_obj.get("href", "")
                    if not link:
                        api_obj = links_obj.get("api", {})
                        if isinstance(api_obj, dict):
                            news_obj = api_obj.get("news", {})
                            if isinstance(news_obj, dict):
                                link = news_obj.get("href", "")
            except (AttributeError, TypeError):
                link = ""

            combined_text = f"{headline} {summary}"
            teams = _extract_teams(combined_text)
            relevance = _classify_relevance(combined_text)

            item = NewsItem(
                id=_make_id(headline, "ESPN"),
                headline=headline,
                summary=summary[:300] if summary else None,
                url=link or None,
                source="ESPN",
                source_icon="ESPN",
                published_at=pub_dt,
                relevance=relevance,
                team_ids=_team_ids_from_abbrs(teams),
                team_names=teams,
            )
            items.append(item)

        return items

    # ── NBA.com ─────────────────────────────────────────────────────

    def _fetch_nba_com(self) -> List[NewsItem]:
        """Fetch NBA news from NBA.com content delivery API.

        Uses the CDN content API which returns JSON without needing
        JavaScript rendering.  Falls back to the older stats endpoint
        if the CDN is unavailable.
        """
        items: List[NewsItem] = []

        # Try the CDN content feed first (more reliable)
        for url in [
            "https://cdn.nba.com/static/json/staticData/content/AllGroups.json",
            "https://stats.nba.com/js/data/cms/today_headlines.json",
        ]:
            try:
                headers = {
                    **HEADERS,
                    "Referer": "https://www.nba.com/",
                    "Origin": "https://www.nba.com",
                }
                resp = httpx.get(
                    url, timeout=REQUEST_TIMEOUT, headers=headers,
                    follow_redirects=True,
                )
                if resp.status_code != 200:
                    logger.debug(f"[News] NBA.com {url}: HTTP {resp.status_code}")
                    continue
                data = resp.json()
            except Exception as e:
                logger.debug(f"[News] NBA.com {url} failed: {e}")
                continue

            # Parse CDN format: { "titles": [...] } or { "content": { "items": [...] } }
            articles = []
            if "titles" in data:
                articles = data["titles"]
            elif "content" in data and isinstance(data["content"], dict):
                articles = data["content"].get("items", [])
            elif "sports_content" in data:
                sc = data["sports_content"]
                hl = sc.get("headlines", {})
                articles = hl.get("headline", []) if isinstance(hl, dict) else hl
            elif "articles" in data:
                articles = data["articles"]

            if not isinstance(articles, list):
                articles = [articles] if isinstance(articles, dict) else []

            for entry in articles[:25]:
                if not isinstance(entry, dict):
                    continue
                headline = (
                    entry.get("title", "")
                    or entry.get("headline", "")
                    or entry.get("name", "")
                ).strip()
                if not headline or len(headline) < 10:
                    continue

                summary = entry.get("excerpt", entry.get("description", ""))
                pub_str = entry.get("date", entry.get("published", ""))
                try:
                    pub_dt = datetime.fromisoformat(pub_str.replace("Z", "+00:00"))
                except (ValueError, AttributeError):
                    pub_dt = datetime.now(timezone.utc)

                link = entry.get("url", entry.get("link", ""))
                if link and not link.startswith("http"):
                    link = f"https://www.nba.com{link}"

                combined_text = f"{headline} {summary}"
                teams = _extract_teams(combined_text)
                relevance = _classify_relevance(combined_text)

                items.append(NewsItem(
                    id=_make_id(headline, "NBA.com"),
                    headline=headline,
                    summary=summary[:300] if summary else None,
                    url=link or None,
                    source="NBA.com",
                    source_icon="NBA",
                    published_at=pub_dt,
                    relevance=relevance,
                    team_ids=_team_ids_from_abbrs(teams),
                    team_names=teams,
                ))

            if items:
                break  # Got data from one URL, don't try the next

        return items

    # ── RotoWire ────────────────────────────────────────────────────

    def _fetch_rotowire(self) -> List[NewsItem]:
        """Fetch NBA player news from RotoWire's RSS feed.

        Uses the RSS/XML feed which returns structured data without
        needing JavaScript rendering (the HTML page is a React SPA
        that doesn't work with simple HTTP requests).
        """
        import xml.etree.ElementTree as ET

        items: List[NewsItem] = []

        # Try RSS feed first (most reliable), then HTML scraping as fallback
        rss_urls = [
            "https://www.rotowire.com/rss/news.php?sport=NBA",
            "https://www.rotowire.com/basketball/nba-news-feed.php",
        ]

        for rss_url in rss_urls:
            try:
                resp = httpx.get(
                    rss_url,
                    timeout=REQUEST_TIMEOUT,
                    headers={
                        **HEADERS,
                        "Accept": "application/rss+xml, application/xml, text/xml",
                    },
                    follow_redirects=True,
                )
                if resp.status_code != 200:
                    logger.debug(f"[News] RotoWire RSS {rss_url}: HTTP {resp.status_code}")
                    continue

                root = ET.fromstring(resp.text)

                # Standard RSS structure: rss > channel > item
                for rss_item in root.findall(".//item")[:30]:
                    title = (rss_item.findtext("title") or "").strip()
                    if not title or len(title) < 10:
                        continue

                    description = (rss_item.findtext("description") or "").strip()
                    # Strip HTML tags from description
                    description = re.sub(r"<[^>]+>", " ", description).strip()
                    description = re.sub(r"\s+", " ", description)

                    link = (rss_item.findtext("link") or "").strip()
                    pub_date = (rss_item.findtext("pubDate") or "").strip()

                    pub_dt = datetime.now(timezone.utc)
                    if pub_date:
                        pub_dt = self._parse_rss_date(pub_date)

                    combined_text = f"{title} {description}"
                    teams = _extract_teams(combined_text)
                    relevance = _classify_relevance(combined_text)

                    # Try to extract player name from title pattern: "Player Name - headline"
                    player_names = []
                    dash_match = re.match(r"^(.+?)\s*[-–—:]\s+", title)
                    if dash_match:
                        candidate = dash_match.group(1).strip()
                        # Basic check: 2-4 words, starts with capital
                        if 1 < len(candidate.split()) <= 4 and candidate[0].isupper():
                            player_names = [candidate]

                    items.append(NewsItem(
                        id=_make_id(title, "RotoWire"),
                        headline=title,
                        summary=description[:300] if description else None,
                        url=link or "https://www.rotowire.com/basketball/nba/news",
                        source="RotoWire",
                        source_icon="RW",
                        published_at=pub_dt,
                        relevance=relevance,
                        team_ids=_team_ids_from_abbrs(teams),
                        team_names=teams,
                        player_names=player_names,
                    ))

                if items:
                    break  # Got data, don't try other URLs

            except ET.ParseError as e:
                logger.debug(f"[News] RotoWire RSS parse error for {rss_url}: {e}")
                continue
            except Exception as e:
                logger.warning(f"[News] RotoWire RSS fetch failed for {rss_url}: {e}")
                continue

        # Fallback: if no RSS items, try HTML scraping (may fail if JS-rendered)
        if not items:
            items = self._fetch_rotowire_html()

        return items

    def _fetch_rotowire_html(self) -> List[NewsItem]:
        """Fallback: try HTML scraping of RotoWire news page."""
        try:
            url = "https://www.rotowire.com/basketball/nba/news"
            resp = httpx.get(url, timeout=REQUEST_TIMEOUT, headers=HEADERS, follow_redirects=True)
            resp.raise_for_status()
            html = resp.text

            items = []
            news_blocks = re.findall(
                r'<div[^>]*class="[^"]*news-update[^"]*"[^>]*>(.*?)</div>\s*</div>',
                html, re.DOTALL,
            )
            if not news_blocks:
                news_blocks = re.findall(
                    r'<div[^>]*class="[^"]*news-update__headline[^"]*"[^>]*>(.*?)</div>',
                    html, re.DOTALL,
                )

            for block in news_blocks[:30]:
                hl_match = re.search(
                    r'class="[^"]*news-update__headline[^"]*"[^>]*>(.*?)<',
                    block, re.DOTALL,
                )
                if not hl_match:
                    hl_match = re.search(r'<a[^>]*>(.*?)</a>', block)
                if not hl_match:
                    continue

                headline = re.sub(r'<[^>]+>', '', hl_match.group(1)).strip()
                if not headline or len(headline) < 10:
                    continue

                teams = _extract_teams(headline)
                relevance = _classify_relevance(headline)

                items.append(NewsItem(
                    id=_make_id(headline, "RotoWire"),
                    headline=headline,
                    summary=None,
                    url="https://www.rotowire.com/basketball/nba/news",
                    source="RotoWire",
                    source_icon="RW",
                    published_at=datetime.now(timezone.utc),
                    relevance=relevance,
                    team_ids=_team_ids_from_abbrs(teams),
                    team_names=teams,
                ))

            return items
        except Exception as e:
            logger.warning(f"[News] RotoWire HTML scraping failed: {e}")
            return []

    @staticmethod
    def _parse_rss_date(date_str: str) -> datetime:
        """Parse various RSS date formats (RFC 822, ISO 8601, etc.)."""
        from datetime import timedelta

        # RFC 822: "Mon, 10 Feb 2025 14:30:00 GMT"
        for fmt in [
            "%a, %d %b %Y %H:%M:%S %Z",
            "%a, %d %b %Y %H:%M:%S %z",
            "%Y-%m-%dT%H:%M:%S%z",
            "%Y-%m-%dT%H:%M:%SZ",
            "%Y-%m-%d %H:%M:%S",
        ]:
            try:
                dt = datetime.strptime(date_str.strip(), fmt)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt
            except ValueError:
                continue

        # Fallback: try ISO format
        try:
            return datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            pass

        return datetime.now(timezone.utc)
