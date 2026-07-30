"""Web scraper for non-Twitter expert sources (RotoWire, FantasyLabs, RSS feeds).

Extracts player-specific blurbs and notes from web sources.
Uses httpx + regex/XML parsing. Each source fails independently.
Cache TTL: 15 minutes.

Sources:
    - RotoWire RSS: Player news blurbs via XML feed (no JS needed)
    - FantasyLabs API: Player notes (often gated, graceful failure)
    - ESPN NBA RSS: General NBA news with player mentions
    - NBC Sports Edge: DFS-focused analysis via RSS
"""

import hashlib
import logging
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import httpx

from app.config.expert_sources import ExpertTier
from app.models.expert_signal import ExpertSignal

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 8  # Hard per-request timeout (was 12 — DNS hangs caused 1156s blocks)
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
)
CACHE_TTL = 900  # 15 minutes

# RSS feed URLs for NBA / DFS expert content
RSS_FEEDS: List[Dict] = [
    {
        "url": "https://www.rotowire.com/rss/news.php?sport=NBA",
        "name": "RotoWire",
        "tier": ExpertTier.TIER_2,
        "specialty": "rotations",
    },
    {
        "url": "https://www.espn.com/espn/rss/nba/news",
        "name": "ESPN",
        "tier": ExpertTier.TIER_1,
        "specialty": "breaking_news",
    },
    {
        "url": "https://feeds.nbcsports.com/nbcsports/rss/NBA",
        "name": "NBC Sports",
        "tier": ExpertTier.TIER_2,
        "specialty": "fantasy",
    },
    {
        "url": "https://basketballmonster.com/rss.aspx",
        "name": "BasketballMonster",
        "tier": ExpertTier.TIER_3,
        "specialty": "fantasy",
    },
]


def _make_signal_id(source: str, content: str) -> str:
    raw = f"{source}|{content.strip().lower()[:100]}"
    return hashlib.md5(raw.encode()).hexdigest()[:12]


def _parse_rss_date(date_str: str) -> datetime:
    """Parse various RSS date formats."""
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
    try:
        return datetime.fromisoformat(date_str.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return datetime.now(timezone.utc)


class WebScraper:
    """Scrapes expert signals from web-based NBA sources."""

    def __init__(self):
        self._rw_cache: Optional[Tuple[List[ExpertSignal], float]] = None
        self._fl_cache: Optional[Tuple[List[ExpertSignal], float]] = None
        self._rss_cache: Optional[Tuple[List[ExpertSignal], float]] = None

    # ------------------------------------------------------------------
    # RSS-based scraping (primary, most reliable)
    # ------------------------------------------------------------------

    def scrape_rss_feeds(
        self,
        player_names: Optional[List[str]] = None,
        team_abbrs: Optional[List[str]] = None,
    ) -> List[ExpertSignal]:
        """Scrape all configured RSS feeds for NBA/DFS expert signals.

        This is the primary signal source — RSS feeds return structured
        XML data without JavaScript rendering, making them far more
        reliable than HTML scraping.
        """
        if self._rss_cache and (time.time() - self._rss_cache[1]) < CACHE_TTL:
            signals = self._rss_cache[0]
        else:
            signals = self._fetch_all_rss()
            # Only cache if we got results
            if signals:
                self._rss_cache = (signals, time.time())

        return self._filter_signals(signals, player_names, team_abbrs)

    def _fetch_all_rss(self) -> List[ExpertSignal]:
        """Fetch from all configured RSS feeds."""
        all_signals: List[ExpertSignal] = []

        for feed in RSS_FEEDS:
            try:
                signals = self._fetch_rss_feed(
                    url=feed["url"],
                    source_name=feed["name"],
                    tier=feed["tier"],
                    specialty=feed["specialty"],
                )
                all_signals.extend(signals)
                logger.info(
                    f"[WebScraper] RSS {feed['name']}: {len(signals)} signals"
                )
            except Exception as e:
                logger.warning(
                    f"[WebScraper] RSS {feed['name']} failed: {e}"
                )

        return all_signals

    def _fetch_rss_feed(
        self,
        url: str,
        source_name: str,
        tier: ExpertTier,
        specialty: str,
    ) -> List[ExpertSignal]:
        """Fetch and parse a single RSS feed into ExpertSignal objects."""
        resp = httpx.get(
            url,
            timeout=REQUEST_TIMEOUT,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/rss+xml, application/xml, text/xml, */*",
            },
            follow_redirects=True,
        )
        if resp.status_code != 200:
            logger.debug(f"[WebScraper] RSS {source_name}: HTTP {resp.status_code}")
            return []

        signals = []
        try:
            root = ET.fromstring(resp.text)
        except ET.ParseError as e:
            logger.warning(f"[WebScraper] RSS {source_name}: XML parse error: {e}")
            return []

        for item in root.findall(".//item")[:25]:
            title = (item.findtext("title") or "").strip()
            if not title or len(title) < 10:
                continue

            description = (item.findtext("description") or "").strip()
            # Strip HTML tags
            description = re.sub(r"<[^>]+>", " ", description).strip()
            description = re.sub(r"\s+", " ", description)

            link = (item.findtext("link") or "").strip()
            pub_date = (item.findtext("pubDate") or "").strip()

            pub_dt = _parse_rss_date(pub_date) if pub_date else datetime.now(timezone.utc)

            # Build content from title + description
            content = title
            if description and description != title:
                content = f"{title} — {description[:200]}"

            signal_type = self._classify_signal_type(content)
            sentiment = self._classify_sentiment(content)

            # Extract player names from content
            mentioned = self._extract_player_names(content)

            signals.append(ExpertSignal(
                id=_make_signal_id(source_name.lower(), content),
                expert_name=source_name,
                expert_handle=source_name.lower().replace(" ", ""),
                expert_tier=tier,
                expert_specialty=specialty,
                content=content[:500],
                timestamp=pub_dt,
                source_platform="web",
                source_url=link or url,
                signal_type=signal_type,
                sentiment=sentiment,
                mentioned_players=mentioned,
            ))

        return signals

    @staticmethod
    def _extract_player_names(text: str) -> List[str]:
        """Try to extract player names from text.

        Looks for patterns like "Player Name -", "Player Name:", or
        capitalized multi-word names that look like person names.
        """
        names = []

        # Pattern: "First Last - ..." or "First Last: ..."
        dash_match = re.match(r"^([A-Z][a-z]+(?:\s+[A-Z][a-z']+){1,2})\s*[-–—:]", text)
        if dash_match:
            names.append(dash_match.group(1).strip())

        return names

    # ------------------------------------------------------------------
    # Legacy scraping methods (RotoWire HTML + FantasyLabs API)
    # ------------------------------------------------------------------

    def scrape_rotowire_blurbs(
        self,
        player_names: Optional[List[str]] = None,
        team_abbrs: Optional[List[str]] = None,
    ) -> List[ExpertSignal]:
        """Scrape RotoWire NBA player news blurbs via RSS first, HTML fallback."""
        if self._rw_cache and (time.time() - self._rw_cache[1]) < CACHE_TTL:
            signals = self._rw_cache[0]
        else:
            # Try RSS first (reliable), then HTML (fragile)
            signals = self._fetch_rotowire_rss()
            if not signals:
                signals = self._fetch_rotowire_html()
            # Only cache non-empty results
            if signals:
                self._rw_cache = (signals, time.time())

        return self._filter_signals(signals, player_names, team_abbrs)

    def _fetch_rotowire_rss(self) -> List[ExpertSignal]:
        """Fetch RotoWire via RSS feed (primary strategy)."""
        try:
            return self._fetch_rss_feed(
                url="https://www.rotowire.com/rss/news.php?sport=NBA",
                source_name="RotoWire",
                tier=ExpertTier.TIER_2,
                specialty="rotations",
            )
        except Exception as e:
            logger.warning(f"[WebScraper] RotoWire RSS failed: {e}")
            return []

    def _fetch_rotowire_html(self) -> List[ExpertSignal]:
        """Fallback: scrape RotoWire HTML (may fail if JS-rendered)."""
        try:
            url = "https://www.rotowire.com/basketball/nba/news"
            resp = httpx.get(
                url,
                timeout=REQUEST_TIMEOUT,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml",
                },
                follow_redirects=True,
            )
            resp.raise_for_status()
            html = resp.text
        except Exception as e:
            logger.warning(f"[WebScraper] RotoWire HTML fetch failed: {e}")
            return []

        signals = []
        blocks = re.findall(
            r'<div[^>]*class="[^"]*news-update[^"]*"[^>]*>(.*?)</div>\s*</div>',
            html,
            re.DOTALL,
        )
        if not blocks:
            blocks = re.findall(
                r'<div[^>]*class="[^"]*news-update__headline[^"]*"[^>]*>(.*?)</div>',
                html,
                re.DOTALL,
            )

        if not blocks:
            logger.warning(
                "[WebScraper] RotoWire HTML: 0 blocks found — "
                "page likely requires JS rendering."
            )
            return []

        for block in blocks[:25]:
            hl_match = re.search(
                r'class="[^"]*news-update__headline[^"]*"[^>]*>(.*?)<',
                block,
                re.DOTALL,
            )
            if not hl_match:
                hl_match = re.search(r"<a[^>]*>(.*?)</a>", block)
            if not hl_match:
                continue

            headline = re.sub(r"<[^>]+>", "", hl_match.group(1)).strip()
            if not headline or len(headline) < 10:
                continue

            player_match = re.search(
                r'class="[^"]*news-update__player-name[^"]*"[^>]*>(.*?)<', block
            )
            player_name = ""
            if player_match:
                player_name = re.sub(r"<[^>]+>", "", player_match.group(1)).strip()

            signal_type = self._classify_signal_type(headline)
            sentiment = self._classify_sentiment(headline)

            signals.append(ExpertSignal(
                id=_make_signal_id("rotowire", headline),
                expert_name="RotoWire",
                expert_handle="rotowire",
                expert_tier=ExpertTier.TIER_2,
                expert_specialty="rotations",
                content=headline,
                timestamp=datetime.now(timezone.utc),
                source_platform="web",
                source_url="https://www.rotowire.com/basketball/nba/news",
                signal_type=signal_type,
                sentiment=sentiment,
                mentioned_players=[player_name] if player_name else [],
            ))

        logger.info(f"[WebScraper] RotoWire HTML: {len(signals)} blurbs parsed")
        return signals

    def scrape_fantasylabs_notes(
        self,
        player_names: Optional[List[str]] = None,
        team_abbrs: Optional[List[str]] = None,
    ) -> List[ExpertSignal]:
        """Scrape FantasyLabs NBA projection notes."""
        if self._fl_cache and (time.time() - self._fl_cache[1]) < CACHE_TTL:
            signals = self._fl_cache[0]
        else:
            signals = self._fetch_fantasylabs()
            if signals:
                self._fl_cache = (signals, time.time())

        return self._filter_signals(signals, player_names, team_abbrs)

    def _fetch_fantasylabs(self) -> List[ExpertSignal]:
        """Fetch FantasyLabs NBA notes. Returns empty on failure (site is often gated)."""
        try:
            url = "https://www.fantasylabs.com/api/playermodel/1/NBA"
            resp = httpx.get(
                url,
                timeout=REQUEST_TIMEOUT,
                headers={"User-Agent": USER_AGENT},
                follow_redirects=True,
            )
            if resp.status_code == 403:
                logger.debug(
                    "[WebScraper] FantasyLabs: 403 Forbidden (paywalled)"
                )
                return []
            if resp.status_code != 200:
                logger.debug(f"[WebScraper] FantasyLabs: HTTP {resp.status_code}")
                return []

            data = resp.json()
            if not isinstance(data, list):
                return []

            signals = []
            for entry in data[:30]:
                name = entry.get("Player_Name", "")
                notes = entry.get("Notes", "")
                if not notes or not name:
                    continue

                signal_type = self._classify_signal_type(notes)
                sentiment = self._classify_sentiment(notes)

                signals.append(ExpertSignal(
                    id=_make_signal_id("fantasylabs", f"{name}|{notes}"),
                    expert_name="FantasyLabs",
                    expert_handle="fantasylabs",
                    expert_tier=ExpertTier.TIER_2,
                    expert_specialty="fantasy",
                    content=f"{name}: {notes}"[:400],
                    timestamp=datetime.now(timezone.utc),
                    source_platform="web",
                    source_url="https://www.fantasylabs.com/nba/projections/",
                    signal_type=signal_type,
                    sentiment=sentiment,
                    mentioned_players=[name],
                ))

            logger.info(f"[WebScraper] FantasyLabs: {len(signals)} notes")
            return signals
        except Exception as e:
            logger.warning(f"[WebScraper] FantasyLabs failed: {e}")
            return []

    # ------------------------------------------------------------------
    # Classification helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _classify_signal_type(text: str) -> str:
        lower = text.lower()
        if any(kw in lower for kw in ["injur", "out ", "ruled out", "questionable", "doubtful", "sidelined"]):
            return "injury_update"
        if any(kw in lower for kw in ["trade", "traded", "deal", "sign", "waiv", "buyout"]):
            return "trade_rumor"
        if any(kw in lower for kw in ["minutes", "min ", "usage", "workload"]):
            return "minutes_projection"
        if any(kw in lower for kw in ["start", "bench", "lineup", "rotation"]):
            return "rotation_change"
        if any(kw in lower for kw in ["return", "available", "will play", "active"]):
            return "lineup_note"
        if any(kw in lower for kw in ["dfs", "fantasy", "value", "ownership", "chalk", "sleeper", "gpp"]):
            return "general_take"
        return "general_take"

    @staticmethod
    def _classify_sentiment(text: str) -> str:
        lower = text.lower()
        bullish = ["return", "available", "will play", "start", "increased",
                    "more minutes", "promoted", "trending up", "expected to play",
                    "smash spot", "value play", "top pick", "must-start"]
        bearish = ["out ", "ruled out", "miss", "sidelined", "limited",
                    "fewer minutes", "questionable", "doubtful", "rest", "dnp",
                    "avoid", "overpriced", "fade"]
        b_count = sum(1 for kw in bullish if kw in lower)
        bear_count = sum(1 for kw in bearish if kw in lower)
        if b_count > bear_count:
            return "bullish"
        if bear_count > b_count:
            return "bearish"
        return "neutral"

    @staticmethod
    def _filter_signals(
        signals: List[ExpertSignal],
        player_names: Optional[List[str]],
        team_abbrs: Optional[List[str]],
    ) -> List[ExpertSignal]:
        if not player_names and not team_abbrs:
            return signals

        filtered = []
        pn_lower = [n.lower() for n in (player_names or [])]
        ta_lower = [a.lower() for a in (team_abbrs or [])]

        for s in signals:
            content_lower = s.content.lower()
            player_match = any(p in content_lower for p in pn_lower)
            mentioned_match = any(
                p in mp.lower()
                for p in pn_lower
                for mp in s.mentioned_players
            )
            team_match = any(a in content_lower for a in ta_lower)

            if player_match or mentioned_match or team_match:
                filtered.append(s)

        return filtered
