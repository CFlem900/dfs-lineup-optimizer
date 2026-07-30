"""Discord REST API fetcher for late-breaking NBA news alerts.

Fetches the latest messages from a configured Discord channel using the
REST API (no async event loop required).  Messages are passed to the
NewsParserService NLP scanner to detect confirmed starters.

Configuration
-------------
Environment variables:
    DISCORD_BOT_TOKEN       Bot token for authentication (required)
    DISCORD_NEWS_CHANNEL_ID Channel ID to poll (required for auto-fetch)

The channel ID can also be set at runtime via the constructor or by
updating ``channel_id`` on the instance.

Usage
-----
    svc = DiscordNewsService()
    alerts = svc.fetch_latest_alerts(limit=5)
    # alerts = ["[Underdog NBA] Bez Mbeng will start...", ...]
"""

import logging
import os
import time
from typing import List, Optional

import httpx

logger = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────────────
DISCORD_API_BASE = "https://discord.com/api/v10"
REQUEST_TIMEOUT = 8.0  # seconds

# Minimum interval between Discord API calls (rate-limit protection).
# Discord's per-channel rate limit is ~5 req/5s; we stay well under.
_MIN_POLL_INTERVAL_SECONDS = 30


class DiscordNewsService:
    """Fetches latest messages from a Discord channel via REST API.

    Lightweight, synchronous, and stateless — designed to be called once
    at the top of the lineup generation pipeline before TopDownMinutes.
    """

    def __init__(
        self,
        bot_token: Optional[str] = None,
        channel_id: Optional[str] = None,
    ):
        self._bot_token = bot_token or os.getenv("DISCORD_BOT_TOKEN", "")
        self.channel_id = channel_id or os.getenv(
            "DISCORD_NEWS_CHANNEL_ID", ""
        )
        self._last_poll_ts: float = 0.0
        self._cached_alerts: List[str] = []

    @property
    def is_available(self) -> bool:
        """True if both bot token and channel ID are configured."""
        return bool(self._bot_token) and bool(self.channel_id)

    def fetch_latest_alerts(
        self,
        limit: int = 5,
        channel_id: Optional[str] = None,
    ) -> List[str]:
        """Fetch the latest messages from the Discord news channel.

        Parameters
        ----------
        limit : int
            Number of messages to fetch (1-100, default 5).
        channel_id : str, optional
            Override the default channel ID for this call.

        Returns
        -------
        list[str]
            Raw message text content, newest first.
            Empty list on any error (graceful degradation).
        """
        _cid = channel_id or self.channel_id
        _token = self._bot_token

        if not _token:
            logger.debug("[Discord] No bot token configured — skipping fetch")
            return []
        if not _cid:
            logger.debug("[Discord] No channel ID configured — skipping fetch")
            return []

        # Rate-limit guard: reuse cached result if polled recently
        now = time.monotonic()
        if (now - self._last_poll_ts) < _MIN_POLL_INTERVAL_SECONDS and self._cached_alerts:
            logger.debug(
                "[Discord] Using cached alerts (%.0fs since last poll, min=%ds)",
                now - self._last_poll_ts, _MIN_POLL_INTERVAL_SECONDS,
            )
            return self._cached_alerts

        url = f"{DISCORD_API_BASE}/channels/{_cid}/messages"
        headers = {
            "Authorization": f"Bot {_token}",
            "Content-Type": "application/json",
        }
        params = {"limit": min(max(limit, 1), 100)}

        try:
            resp = httpx.get(
                url,
                headers=headers,
                params=params,
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            messages = resp.json()

            if not isinstance(messages, list):
                logger.warning(
                    "[Discord] Unexpected response type: %s", type(messages)
                )
                return []

            # Extract raw text content, skip empty messages and embeds-only
            alerts = []
            for msg in messages:
                content = (msg.get("content") or "").strip()
                if content:
                    alerts.append(content)

                # Also check embed descriptions (Underdog bot uses embeds)
                for embed in msg.get("embeds", []):
                    desc = (embed.get("description") or "").strip()
                    title = (embed.get("title") or "").strip()
                    if desc:
                        alerts.append(desc)
                    elif title:
                        alerts.append(title)

            self._last_poll_ts = now
            self._cached_alerts = alerts

            logger.info(
                "[Discord] Fetched %d alert(s) from channel %s "
                "(%d raw messages)",
                len(alerts), _cid, len(messages),
            )
            return alerts

        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            if status == 401:
                logger.error(
                    "[Discord] Authentication failed (401) — check DISCORD_BOT_TOKEN"
                )
            elif status == 403:
                logger.error(
                    "[Discord] Access denied (403) — bot lacks permission "
                    "to read channel %s", _cid,
                )
            elif status == 404:
                logger.error(
                    "[Discord] Channel not found (404) — check "
                    "DISCORD_NEWS_CHANNEL_ID=%s", _cid,
                )
            elif status == 429:
                retry_after = e.response.headers.get("Retry-After", "?")
                logger.warning(
                    "[Discord] Rate limited (429) — retry after %ss", retry_after,
                )
            else:
                logger.error("[Discord] HTTP %d: %s", status, e)
            return []

        except httpx.TimeoutException:
            logger.warning(
                "[Discord] Request timed out after %.1fs", REQUEST_TIMEOUT
            )
            return []

        except Exception as e:
            logger.error("[Discord] Unexpected error: %s", e)
            return []

    def clear_cache(self):
        """Force next fetch to hit the API (bypasses rate-limit cache)."""
        self._last_poll_ts = 0.0
        self._cached_alerts = []
