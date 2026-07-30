"""Multi-strategy Twitter/X scraper for expert NBA tweets.

Strategies (tried in order):
    A) Twitter API v2 — primary, requires TWITTER_BEARER_TOKEN in .env
    B) Nitter RSS feeds — optional fallback (most instances are defunct)

Each strategy is self-contained and fails gracefully. If all strategies
fail for a handle, the handle is skipped without blocking other handles.

Note: This module uses synchronous httpx + time.sleep() for rate limiting.
It is designed to be called from a plain ``def`` FastAPI route handler
(which FastAPI runs in a threadpool) rather than from ``async def``.
"""

import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import httpx

from app.models.expert_signal import RawTweet

logger = logging.getLogger(__name__)

# -- HTTP settings ----------------------------------------------------------
REQUEST_TIMEOUT = 10
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/119.0.0.0 Safari/537.36",
]

# Nitter instances — most are defunct as of late 2024.
# Add working instances here if any become available; otherwise the
# Nitter strategy is silently skipped.
DEFAULT_NITTER_INSTANCES: List[str] = []

# Cache: handle -> (tweets, timestamp)
_tweet_cache: dict[str, tuple[list[RawTweet], float]] = {}
CACHE_TTL = 900  # 15 minutes


class TwitterScraper:
    """Fetches recent tweets from expert handles via multiple strategies."""

    def __init__(
        self,
        nitter_instances: Optional[List[str]] = None,
        bearer_token: Optional[str] = None,
    ):
        self._nitter_instances = nitter_instances if nitter_instances is not None else DEFAULT_NITTER_INSTANCES
        self._bearer_token = bearer_token or os.environ.get("TWITTER_BEARER_TOKEN")
        self._ua_index = 0
        self._last_request_time = 0.0

        if self._bearer_token:
            logger.info("[Twitter] Bearer token configured — API v2 strategy enabled")
        else:
            logger.warning(
                "[Twitter] No TWITTER_BEARER_TOKEN set. "
                "Twitter signals will be unavailable unless Nitter instances are configured."
            )

    def _get_ua(self) -> str:
        ua = USER_AGENTS[self._ua_index % len(USER_AGENTS)]
        self._ua_index += 1
        return ua

    def _rate_limit(self, min_interval: float = 0.5):
        """Sleep to enforce minimum interval between HTTP requests.

        This uses blocking ``time.sleep()`` — safe because the caller
        runs in a threadpool (FastAPI ``def`` handler), not on the
        asyncio event loop.
        """
        elapsed = time.time() - self._last_request_time
        if elapsed < min_interval:
            time.sleep(min_interval - elapsed)
        self._last_request_time = time.time()

    # If this many consecutive handles fail with DNS errors, skip the rest.
    _DNS_FAIL_THRESHOLD = 2
    # Hard wall-clock deadline for the entire fetch loop.
    _FETCH_DEADLINE_S = 20.0

    def fetch_expert_tweets(
        self,
        handles: List[str],
        keywords: Optional[List[str]] = None,
        hours_back: int = 24,
        max_per_handle: int = 10,
    ) -> List[RawTweet]:
        """Fetch recent tweets from given handles with optional keyword filter.

        Enforces a 20-second deadline and DNS fast-fail circuit breaker:
        if 2+ consecutive handles fail with ``getaddrinfo failed``, all
        remaining handles are skipped immediately.
        """
        all_tweets: List[RawTweet] = []
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_back)
        dns_failures = 0
        t0 = time.time()

        for handle in handles:
            # Deadline check
            if (time.time() - t0) >= self._FETCH_DEADLINE_S:
                logger.warning(
                    f"[Twitter] Deadline exceeded ({self._FETCH_DEADLINE_S}s) "
                    f"— skipping remaining {len(handles) - handles.index(handle)} handles"
                )
                break

            # DNS circuit breaker
            if dns_failures >= self._DNS_FAIL_THRESHOLD:
                logger.warning(
                    f"[Twitter] DNS circuit breaker tripped after "
                    f"{dns_failures} consecutive failures — skipping "
                    f"remaining handles"
                )
                break

            # Check cache first
            cached = _tweet_cache.get(handle)
            if cached and (time.time() - cached[1]) < CACHE_TTL:
                tweets = cached[0]
            else:
                try:
                    tweets = self._fetch_handle(handle)
                    _tweet_cache[handle] = (tweets, time.time())
                    dns_failures = 0  # reset on success
                except Exception as e:
                    err_msg = str(e).lower()
                    if "getaddrinfo failed" in err_msg or "name resolution" in err_msg:
                        dns_failures += 1
                    tweets = []

            # Filter by recency
            tweets = [t for t in tweets if t.timestamp >= cutoff]

            # Filter by keywords if specified
            if keywords:
                kw_lower = [k.lower() for k in keywords]
                tweets = [
                    t for t in tweets
                    if any(kw in t.text.lower() for kw in kw_lower)
                ]

            # Limit per handle
            all_tweets.extend(tweets[:max_per_handle])

        # Sort by timestamp descending
        all_tweets.sort(key=lambda t: t.timestamp, reverse=True)
        return all_tweets

    def _fetch_handle(self, handle: str) -> List[RawTweet]:
        """Try strategies in order: API v2 (primary), Nitter RSS (fallback)."""

        # Strategy A: Twitter API v2 (primary)
        if self._bearer_token:
            try:
                result = self._fetch_via_api(handle)
                if result:
                    logger.info(f"[Twitter] API OK for @{handle}: {len(result)} tweets")
                    return result
            except Exception as e:
                logger.warning(f"[Twitter] API failed for @{handle}: {e}")

        # Strategy B: Nitter RSS (fallback, if instances configured)
        for instance in self._nitter_instances:
            try:
                result = self._fetch_via_nitter(handle, instance)
                if result:
                    logger.info(f"[Twitter] Nitter OK for @{handle} via {instance}: {len(result)} tweets")
                    return result
            except Exception as e:
                logger.debug(f"[Twitter] Nitter {instance} failed for @{handle}: {e}")
                continue

        logger.warning(f"[Twitter] All strategies failed for @{handle}")
        return []

    def _fetch_via_api(self, handle: str) -> List[RawTweet]:
        """Use Twitter API v2 search/recent endpoint.

        Essential tier ($100/mo): 450 requests per 15-minute window.
        We use a 2-second interval between calls for safe headroom.
        """
        if not self._bearer_token:
            return []

        self._rate_limit(2.0)
        url = "https://api.twitter.com/2/tweets/search/recent"
        params = {
            "query": f"from:{handle} -is:retweet",
            "max_results": 10,
            "tweet.fields": "created_at,public_metrics",
        }
        resp = httpx.get(
            url,
            params=params,
            timeout=REQUEST_TIMEOUT,
            headers={
                "Authorization": f"Bearer {self._bearer_token}",
                "User-Agent": self._get_ua(),
            },
        )

        # Handle rate limiting gracefully
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("retry-after", 60))
            logger.warning(
                f"[Twitter] Rate limited for @{handle}. "
                f"Retry-After: {retry_after}s — skipping handle."
            )
            # Don't block for the full retry-after; just skip this handle.
            # The 15-min cache TTL means we'll retry naturally.
            return []

        resp.raise_for_status()
        data = resp.json()

        tweets = []
        for tweet in data.get("data", []):
            ts = datetime.fromisoformat(tweet["created_at"].replace("Z", "+00:00"))
            metrics = tweet.get("public_metrics", {})
            tweets.append(RawTweet(
                tweet_id=tweet["id"],
                handle=handle,
                display_name=handle,
                text=tweet["text"][:500],
                timestamp=ts,
                likes=metrics.get("like_count", 0),
                retweets=metrics.get("retweet_count", 0),
                url=f"https://twitter.com/{handle}/status/{tweet['id']}",
            ))
        return tweets

    def _fetch_via_nitter(self, handle: str, instance: str) -> List[RawTweet]:
        """Parse Nitter RSS feed for a handle (fallback strategy)."""
        self._rate_limit(0.3)
        url = f"https://{instance}/{handle}/rss"
        resp = httpx.get(
            url,
            timeout=REQUEST_TIMEOUT,
            headers={"User-Agent": self._get_ua()},
            follow_redirects=True,
        )
        resp.raise_for_status()

        tweets = []
        root = ET.fromstring(resp.text)
        # RSS structure: rss > channel > item
        for item in root.findall(".//item"):
            title = item.findtext("title", "").strip()
            link = item.findtext("link", "").strip()
            pub_date = item.findtext("pubDate", "")
            description = item.findtext("description", "")

            if not title:
                continue

            # Parse pubDate (RFC 822 format)
            try:
                ts = datetime.strptime(pub_date, "%a, %d %b %Y %H:%M:%S %Z")
                ts = ts.replace(tzinfo=timezone.utc)
            except (ValueError, AttributeError):
                ts = datetime.now(timezone.utc)

            # Extract tweet ID from link
            tweet_id = link.rsplit("/", 1)[-1] if "/" in link else link

            # Detect retweets and replies
            is_rt = title.startswith("RT by") or title.startswith("R to")
            is_reply = bool(re.match(r"^R to @\w+:", title))

            # Clean HTML from description for content
            text = re.sub(r"<[^>]+>", " ", description).strip()
            text = re.sub(r"\s+", " ", text)
            if not text:
                text = title

            tweets.append(RawTweet(
                tweet_id=tweet_id,
                handle=handle,
                display_name=handle,
                text=text[:500],
                timestamp=ts,
                url=link,
                is_reply=is_reply,
                is_retweet=is_rt,
            ))

        return tweets
