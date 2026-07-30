"""Expert signal aggregation service.

Orchestrates Twitter scraping, web scraping, and mock data to produce
a ranked list of expert signals for a given team/player context.

Features:
    - Multi-source aggregation (Twitter + web)
    - Relevance scoring (tier, recency, engagement, keyword, player mention)
    - Deduplication by content hash
    - 15-minute cache with team-keyed invalidation
    - Graceful fallback to mock data when SCRAPING_ENABLED=false
    - Sentiment summary computation
"""

import hashlib
import logging
import math
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Set, Tuple

from app.config.expert_sources import (
    EXPERT_REGISTRY,
    ExpertTier,
    get_twitter_experts,
    get_web_experts,
)
from app.models.expert_signal import ExpertSignal, ExpertSignalsResponse
from app.services.twitter_scraper import TwitterScraper
from app.services.web_scraper import WebScraper

logger = logging.getLogger(__name__)

CACHE_TTL = 900  # 15 minutes

# Keywords that boost relevance score
ROTATION_KEYWORDS = [
    "minutes", "starter", "bench", "dnp", "rest", "rotation",
    "starting", "role", "usage", "workload", "touches",
]


class ExpertSignalService:
    """Aggregates, scores, and ranks expert signals."""

    def __init__(self, signal_analysis_agent=None, expert_quality_agent=None):
        self._twitter_scraper = TwitterScraper()
        self._web_scraper = WebScraper()
        self._cache: Dict[str, Tuple[List[ExpertSignal], float]] = {}
        self._scraping_enabled = os.environ.get("SCRAPING_ENABLED", "true").lower() != "false"
        self._signal_agent = signal_analysis_agent    # Agent 1: NLP signal analysis
        self._quality_agent = expert_quality_agent    # Agent 10: Expert quality weighting

    def get_signals(
        self,
        team_abbr: Optional[str] = None,
        player_names: Optional[List[str]] = None,
        limit: int = 30,
    ) -> ExpertSignalsResponse:
        """Get ranked expert signals, optionally filtered by team/players.

        Returns cached results if still valid. Falls back to mock data
        if scraping is disabled.
        """
        cache_key = f"{team_abbr or 'all'}:{','.join(sorted(player_names or []))}"

        # Check cache
        cached = self._cache.get(cache_key)
        if cached and (time.time() - cached[1]) < CACHE_TTL:
            signals = cached[0]
            return self._build_response(signals, limit, cached=True)

        # Fetch fresh data
        if not self._scraping_enabled:
            from app.services.mock_expert_data import generate_mock_signals
            signals = generate_mock_signals(
                team_abbr=team_abbr,
                player_names=player_names,
                count=limit,
            )
            logger.info(f"[ExpertSignals] Mock mode: generated {len(signals)} signals")
        else:
            signals = self._aggregate(team_abbr, player_names)

        # Score and rank
        for signal in signals:
            signal.relevance_score = round(
                self._calculate_signal_score(signal, player_names, team_abbr), 2
            )

        # ── AI Enhancement (Agent 1) ──────────────────────────────
        # Replace keyword-based classification with LLM analysis when
        # available.  Falls back to existing scores silently.
        if self._signal_agent and self._signal_agent.is_available:
            try:
                batch = [
                    {"content": s.content, "context": {"team_abbr": team_abbr or ""}}
                    for s in signals[:20]
                ]
                analyses = self._signal_agent.batch_analyze(batch)
                for i, analysis in enumerate(analyses):
                    if analysis is not None and i < len(signals):
                        signals[i].sentiment = analysis.sentiment
                        signals[i].signal_type = analysis.signal_type
                        if analysis.mentioned_players:
                            signals[i].mentioned_players = analysis.mentioned_players
                        # Boost relevance for high-confidence AI classifications
                        if analysis.confidence > 0.8:
                            signals[i].relevance_score = min(
                                2.0, signals[i].relevance_score + 0.2
                            )
                logger.info(f"[Expert] AI enhanced {len(analyses)} signals")
            except Exception as exc:
                logger.warning(f"[Expert] AI enhancement failed, using fallback: {exc}")

        # ── AI Quality Weighting (Agent 10) ─────────────────────
        # Adjust relevance scores by expert accuracy.  High-accuracy
        # experts get boosted; unreliable ones get dampened.
        # When signal-type-specific specialty scores are available,
        # those are preferred over the generic weight modifier.
        if self._quality_agent:
            try:
                for signal in signals:
                    handle = getattr(signal, "expert_handle", "")
                    if not handle:
                        continue
                    signal_type = getattr(signal, "signal_type", "")
                    # Try signal-type-specific weight first
                    specialty_weight = (
                        self._quality_agent.get_specialty_weight(
                            handle, signal_type
                        )
                        if signal_type
                        else None
                    )
                    if specialty_weight is not None:
                        weight = specialty_weight
                    else:
                        weight = self._quality_agent.get_quality_adjusted_weight(
                            handle
                        )
                    signal.relevance_score = round(
                        signal.relevance_score * weight, 2
                    )
            except Exception as exc:
                logger.debug(f"[Expert] Quality weighting failed: {exc}")

        # Sort by relevance descending
        signals.sort(key=lambda s: s.relevance_score, reverse=True)

        # Only cache non-empty results so next request retries on failure
        if signals:
            self._cache[cache_key] = (signals, time.time())
        else:
            logger.warning(
                "[ExpertSignals] All sources returned 0 signals — "
                "NOT caching, will retry on next request"
            )

        return self._build_response(signals, limit, cached=False)

    # Maximum wall-clock time for the entire aggregation pipeline.
    # Prevents DNS failures from blocking the lineup generation thread
    # for minutes (the old code allowed 1156s / 19 minutes).
    _AGGREGATE_DEADLINE_S = 30.0

    # If this many consecutive sources fail with DNS errors, skip the rest.
    _DNS_FAIL_THRESHOLD = 3

    @staticmethod
    def _is_dns_error(exc: Exception) -> bool:
        """Check whether an exception chain contains a DNS resolution failure."""
        # Walk the chain: httpx wraps socket.gaierror inside ConnectError
        current: Optional[BaseException] = exc
        while current is not None:
            msg = str(current).lower()
            if "getaddrinfo failed" in msg or "name resolution" in msg:
                return True
            current = current.__cause__ or current.__context__
        return False

    def _aggregate(
        self,
        team_abbr: Optional[str],
        player_names: Optional[List[str]],
    ) -> List[ExpertSignal]:
        """Fetch from all sources and merge.

        Source priority:
            1. RSS feeds (most reliable — structured XML, no JS needed)
            2. Twitter API v2 (requires valid bearer token + Basic tier)
            3. RotoWire blurbs (RSS primary, HTML fallback)
            4. FantasyLabs (often 403 — graceful failure)

        Enforces a hard 30-second deadline and a DNS fast-fail circuit
        breaker: if 3+ consecutive sources fail with ``getaddrinfo failed``,
        all remaining sources are skipped immediately.
        """
        start_time = time.time()
        all_signals: List[ExpertSignal] = []
        seen_ids: Set[str] = set()
        dns_failures = 0  # consecutive DNS errors

        def _deadline_exceeded() -> bool:
            return (time.time() - start_time) >= self._AGGREGATE_DEADLINE_S

        def _dns_tripped() -> bool:
            return dns_failures >= self._DNS_FAIL_THRESHOLD

        def _add(signals: List[ExpertSignal], label: str) -> int:
            count = 0
            for s in signals:
                if s.id not in seen_ids:
                    seen_ids.add(s.id)
                    all_signals.append(s)
                    count += 1
            logger.info(f"[ExpertSignals] {label}: {count} new signals")
            return count

        def _record_success():
            nonlocal dns_failures
            dns_failures = 0  # reset consecutive counter on any success

        # 1. RSS feeds (primary — most reliable source for DFS content)
        if not _deadline_exceeded() and not _dns_tripped():
            try:
                rss_signals = self._web_scraper.scrape_rss_feeds(
                    player_names=player_names,
                    team_abbrs=[team_abbr] if team_abbr else None,
                )
                _add(rss_signals, "RSS Feeds")
                _record_success()
            except Exception as e:
                logger.warning(f"[ExpertSignals] RSS feeds failed: {e}")
                if self._is_dns_error(e):
                    dns_failures += 1

        # 2. Twitter signals (if bearer token configured)
        if not _deadline_exceeded() and not _dns_tripped():
            try:
                twitter_experts = get_twitter_experts()
                handles = [e.handle for e in twitter_experts]
                raw_tweets = self._twitter_scraper.fetch_expert_tweets(
                    handles=handles,
                    keywords=ROTATION_KEYWORDS if team_abbr else None,
                    hours_back=24,
                    max_per_handle=8,
                )

                twitter_signals = []
                expert_map = {e.handle.lower(): e for e in twitter_experts}
                for tweet in raw_tweets:
                    if tweet.is_retweet:
                        continue
                    expert = expert_map.get(tweet.handle.lower())
                    if not expert:
                        continue

                    signal = self._tweet_to_signal(tweet, expert)

                    if team_abbr or player_names:
                        matched = self._match_to_context(
                            signal.content, player_names, team_abbr
                        )
                        if not matched["has_match"]:
                            continue
                        signal.mentioned_players = matched["players"]
                        signal.mentioned_team = matched.get("team")

                    twitter_signals.append(signal)

                _add(twitter_signals, "Twitter")
                _record_success()
            except Exception as e:
                logger.warning(f"[ExpertSignals] Twitter aggregation failed: {e}")
                if self._is_dns_error(e):
                    dns_failures += 1

        # 3. Web signals (RotoWire blurbs — RSS primary, HTML fallback)
        if not _deadline_exceeded() and not _dns_tripped():
            try:
                rw_signals = self._web_scraper.scrape_rotowire_blurbs(
                    player_names=player_names,
                    team_abbrs=[team_abbr] if team_abbr else None,
                )
                _add(rw_signals, "RotoWire")
                _record_success()
            except Exception as e:
                logger.warning(f"[ExpertSignals] RotoWire failed: {e}")
                if self._is_dns_error(e):
                    dns_failures += 1

        # 4. Web signals (FantasyLabs — often gated)
        if not _deadline_exceeded() and not _dns_tripped():
            try:
                fl_signals = self._web_scraper.scrape_fantasylabs_notes(
                    player_names=player_names,
                    team_abbrs=[team_abbr] if team_abbr else None,
                )
                _add(fl_signals, "FantasyLabs")
                _record_success()
            except Exception as e:
                logger.warning(f"[ExpertSignals] FantasyLabs failed: {e}")
                if self._is_dns_error(e):
                    dns_failures += 1

        elapsed = time.time() - start_time

        if _dns_tripped():
            logger.warning(
                f"[ExpertSignals] DNS circuit breaker tripped after "
                f"{dns_failures} consecutive failures — skipped remaining "
                f"sources ({elapsed:.1f}s elapsed, "
                f"{len(all_signals)} signals collected)"
            )
        elif _deadline_exceeded():
            logger.warning(
                f"[ExpertSignals] Deadline exceeded ({elapsed:.1f}s > "
                f"{self._AGGREGATE_DEADLINE_S}s) — skipped remaining sources "
                f"({len(all_signals)} signals collected)"
            )
        else:
            logger.info(
                f"[ExpertSignals] Aggregation complete: "
                f"{len(all_signals)} signals in {elapsed:.1f}s"
            )
        return all_signals

    def _tweet_to_signal(self, tweet, expert) -> ExpertSignal:
        """Convert a RawTweet + ExpertSource into an ExpertSignal."""
        from app.services.web_scraper import WebScraper

        signal_type = WebScraper._classify_signal_type(tweet.text)
        sentiment = WebScraper._classify_sentiment(tweet.text)

        sig_id = hashlib.md5(
            f"{tweet.handle}|{tweet.text[:100]}".encode()
        ).hexdigest()[:12]

        return ExpertSignal(
            id=sig_id,
            expert_name=expert.name,
            expert_handle=expert.handle,
            expert_tier=expert.tier,
            expert_specialty=expert.specialty,
            avatar_url=expert.avatar_url,
            content=tweet.text[:500],
            timestamp=tweet.timestamp,
            source_platform="twitter",
            source_url=tweet.url,
            signal_type=signal_type,
            sentiment=sentiment,
            engagement={"likes": tweet.likes, "retweets": tweet.retweets},
        )

    @staticmethod
    def _calculate_signal_score(
        signal: ExpertSignal,
        player_names: Optional[List[str]] = None,
        team_abbr: Optional[str] = None,
    ) -> float:
        """Score a signal for relevance ranking (0.0 - 2.0).

        Formula:
            - Tier 1: base 1.0 | Tier 2: 0.7 | Tier 3: 0.4
            - Recency: +0.3 (<1hr), +0.2 (<3hr), +0.1 (<6hr)
            - Engagement: log(likes + retweets + 1) * 0.05
            - Keyword match: +0.2 if rotation keywords present
            - Direct player mention: +0.3
        """
        # Base tier score
        tier_scores = {
            ExpertTier.TIER_1: 1.0,
            ExpertTier.TIER_2: 0.7,
            ExpertTier.TIER_3: 0.4,
        }
        score = tier_scores.get(signal.expert_tier, 0.5)

        # Recency bonus
        now = datetime.now(timezone.utc)
        age = now - signal.timestamp
        if age < timedelta(hours=1):
            score += 0.3
        elif age < timedelta(hours=3):
            score += 0.2
        elif age < timedelta(hours=6):
            score += 0.1

        # Engagement bonus
        if signal.engagement:
            total = signal.engagement.get("likes", 0) + signal.engagement.get("retweets", 0)
            score += math.log(total + 1) * 0.05

        # Keyword bonus
        content_lower = signal.content.lower()
        if any(kw in content_lower for kw in ROTATION_KEYWORDS):
            score += 0.2

        # Direct player mention bonus
        if player_names:
            for pn in player_names:
                if pn.lower() in content_lower:
                    score += 0.3
                    break

        return min(score, 2.0)

    @staticmethod
    def _match_to_context(
        text: str,
        player_names: Optional[List[str]],
        team_abbr: Optional[str],
    ) -> dict:
        """Check if text mentions any of the target players/team."""
        lower = text.lower()
        result = {"has_match": False, "players": [], "team": None}

        if team_abbr and team_abbr.lower() in lower:
            result["has_match"] = True
            result["team"] = team_abbr

        if player_names:
            for name in player_names:
                # Check full name or last name
                if name.lower() in lower:
                    result["has_match"] = True
                    result["players"].append(name)
                else:
                    parts = name.split()
                    if len(parts) > 1 and parts[-1].lower() in lower:
                        result["has_match"] = True
                        result["players"].append(name)

        return result

    @staticmethod
    def _build_response(
        signals: List[ExpertSignal],
        limit: int,
        cached: bool,
    ) -> ExpertSignalsResponse:
        """Build the API response with sentiment summary."""
        limited = signals[:limit]

        # Compute sentiment summary
        total = len(limited) or 1
        bullish = sum(1 for s in limited if s.sentiment == "bullish")
        bearish = sum(1 for s in limited if s.sentiment == "bearish")
        neutral = sum(1 for s in limited if s.sentiment == "neutral")

        # Count unique experts
        experts_tracked = len(set(s.expert_handle for s in limited))

        return ExpertSignalsResponse(
            signals=limited,
            total=len(limited),
            experts_tracked=experts_tracked,
            cached=cached,
            sentiment_summary={
                "bullish": round(bullish / total, 2),
                "bearish": round(bearish / total, 2),
                "neutral": round(neutral / total, 2),
            },
        )
