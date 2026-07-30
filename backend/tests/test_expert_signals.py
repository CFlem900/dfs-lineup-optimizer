"""Tests for expert signal pipeline — config, models, scoring, mock data."""

import time
from datetime import datetime, timedelta, timezone

import pytest

from app.config.expert_sources import (
    EXPERT_REGISTRY,
    ExpertSource,
    ExpertTier,
    get_twitter_experts,
    get_web_experts,
)
from app.models.expert_signal import ExpertSignal, ExpertSignalsResponse, RawTweet
from app.services.expert_signal_service import ExpertSignalService
from app.services.mock_expert_data import generate_mock_signals
from app.services.web_scraper import WebScraper


# ── Expert Registry Tests ──────────────────────────────────────────


class TestExpertRegistry:
    def test_registry_not_empty(self):
        assert len(EXPERT_REGISTRY) > 0

    def test_all_entries_valid(self):
        for expert in EXPERT_REGISTRY:
            assert expert.name
            assert expert.handle
            assert expert.platform in ("twitter", "web")
            assert expert.tier in (ExpertTier.TIER_1, ExpertTier.TIER_2, ExpertTier.TIER_3)

    def test_has_tier_1_experts(self):
        tier_1 = [e for e in EXPERT_REGISTRY if e.tier == ExpertTier.TIER_1]
        assert len(tier_1) >= 3

    def test_has_web_sources(self):
        web = get_web_experts()
        assert len(web) >= 1

    def test_twitter_filter_by_tier(self):
        tier_1_only = get_twitter_experts(ExpertTier.TIER_1)
        all_tiers = get_twitter_experts(ExpertTier.TIER_3)
        assert len(tier_1_only) <= len(all_tiers)
        for e in tier_1_only:
            assert e.tier == ExpertTier.TIER_1

    def test_web_experts_have_urls(self):
        for expert in get_web_experts():
            assert expert.url is not None
            assert expert.url.startswith("http")


# ── Model Tests ────────────────────────────────────────────────────


class TestRawTweetModel:
    def test_minimal(self):
        tweet = RawTweet(
            tweet_id="123",
            handle="testuser",
            display_name="Test User",
            text="This is a test tweet about NBA rotation",
            timestamp=datetime.now(timezone.utc),
            url="https://twitter.com/testuser/status/123",
        )
        assert tweet.likes == 0
        assert tweet.is_reply is False

    def test_full(self):
        tweet = RawTweet(
            tweet_id="456",
            handle="wojespn",
            display_name="Woj",
            text="Breaking: Player X traded",
            timestamp=datetime.now(timezone.utc),
            likes=5000,
            retweets=2000,
            url="https://twitter.com/wojespn/status/456",
            is_reply=False,
            is_retweet=False,
        )
        assert tweet.likes == 5000


class TestExpertSignalModel:
    def test_minimal(self):
        signal = ExpertSignal(
            id="abc",
            expert_name="Woj",
            expert_handle="wojespn",
            expert_tier=ExpertTier.TIER_1,
            expert_specialty="breaking_news",
            content="Test content",
            timestamp=datetime.now(timezone.utc),
            source_platform="twitter",
            source_url="https://twitter.com/wojespn/status/1",
            signal_type="general_take",
            sentiment="neutral",
        )
        assert signal.relevance_score == 0.0
        assert signal.mentioned_players == []

    def test_response_model(self):
        resp = ExpertSignalsResponse(
            signals=[],
            total=0,
            experts_tracked=0,
            cached=False,
            sentiment_summary={"bullish": 0.5, "bearish": 0.3, "neutral": 0.2},
        )
        assert resp.sentiment_summary["bullish"] == 0.5


# ── Scoring Tests ──────────────────────────────────────────────────


class TestSignalScoring:
    def _make_signal(self, tier=ExpertTier.TIER_1, hours_ago=0.5,
                     likes=100, retweets=50, text="Player X rotation change minutes"):
        return ExpertSignal(
            id="test",
            expert_name="Test",
            expert_handle="test",
            expert_tier=tier,
            expert_specialty="analytics",
            content=text,
            timestamp=datetime.now(timezone.utc) - timedelta(hours=hours_ago),
            source_platform="twitter",
            source_url="https://twitter.com/test/status/1",
            signal_type="rotation_change",
            sentiment="bullish",
            engagement={"likes": likes, "retweets": retweets},
        )

    def test_tier_1_higher_than_tier_3(self):
        svc = ExpertSignalService
        t1 = self._make_signal(tier=ExpertTier.TIER_1)
        t3 = self._make_signal(tier=ExpertTier.TIER_3)
        s1 = svc._calculate_signal_score(t1)
        s3 = svc._calculate_signal_score(t3)
        assert s1 > s3

    def test_recency_bonus_recent(self):
        svc = ExpertSignalService
        recent = self._make_signal(hours_ago=0.5)
        old = self._make_signal(hours_ago=12)
        assert svc._calculate_signal_score(recent) > svc._calculate_signal_score(old)

    def test_engagement_bonus(self):
        svc = ExpertSignalService
        viral = self._make_signal(likes=10000, retweets=5000)
        quiet = self._make_signal(likes=1, retweets=0)
        assert svc._calculate_signal_score(viral) > svc._calculate_signal_score(quiet)

    def test_keyword_bonus(self):
        svc = ExpertSignalService
        with_kw = self._make_signal(text="Minutes update: Player X starting tonight")
        without_kw = self._make_signal(text="Great game last night by Player X")
        assert svc._calculate_signal_score(with_kw) > svc._calculate_signal_score(without_kw)

    def test_player_mention_bonus(self):
        svc = ExpertSignalService
        signal = self._make_signal(text="Jokic expected to play 35 minutes tonight")
        with_player = svc._calculate_signal_score(signal, player_names=["Jokic"])
        without_player = svc._calculate_signal_score(signal, player_names=["LeBron"])
        assert with_player > without_player

    def test_score_capped_at_2(self):
        svc = ExpertSignalService
        signal = self._make_signal(
            tier=ExpertTier.TIER_1,
            hours_ago=0.1,
            likes=100000,
            retweets=50000,
            text="minutes rotation starter Jokic",
        )
        score = svc._calculate_signal_score(signal, player_names=["Jokic"])
        assert score <= 2.0


# ── Context Matching Tests ─────────────────────────────────────────


class TestContextMatching:
    def test_team_match(self):
        result = ExpertSignalService._match_to_context(
            "DEN looking to shake up rotation", None, "DEN"
        )
        assert result["has_match"] is True
        assert result["team"] == "DEN"

    def test_player_match_full_name(self):
        result = ExpertSignalService._match_to_context(
            "Nikola Jokic expected to play", ["Nikola Jokic"], None
        )
        assert result["has_match"] is True
        assert "Nikola Jokic" in result["players"]

    def test_player_match_last_name(self):
        result = ExpertSignalService._match_to_context(
            "Jokic will start tonight", ["Nikola Jokic"], None
        )
        assert result["has_match"] is True

    def test_no_match(self):
        result = ExpertSignalService._match_to_context(
            "Great weather today!", ["Nikola Jokic"], "DEN"
        )
        assert result["has_match"] is False

    def test_empty_context(self):
        result = ExpertSignalService._match_to_context(
            "Something about NBA", None, None
        )
        assert result["has_match"] is False


# ── Mock Data Tests ────────────────────────────────────────────────


class TestMockData:
    def test_generates_correct_count(self):
        signals = generate_mock_signals(count=15)
        assert len(signals) == 15

    def test_signals_have_required_fields(self):
        signals = generate_mock_signals(count=5)
        for s in signals:
            assert s.id
            assert s.expert_name
            assert s.content
            assert s.signal_type in (
                "injury_update", "rotation_change", "minutes_projection",
                "trade_rumor", "general_take",
            )
            assert s.sentiment in ("bullish", "bearish", "neutral")

    def test_team_filter(self):
        signals = generate_mock_signals(team_abbr="DEN", count=10)
        # Most signals should mention DEN players
        den_mentions = sum(
            1 for s in signals
            if s.mentioned_team == "DEN" or "DEN" in s.content
        )
        assert den_mentions > 0

    def test_sorted_by_relevance(self):
        signals = generate_mock_signals(count=20)
        for i in range(len(signals) - 1):
            assert signals[i].relevance_score >= signals[i + 1].relevance_score

    def test_engagement_present(self):
        signals = generate_mock_signals(count=10)
        has_engagement = sum(1 for s in signals if s.engagement)
        assert has_engagement == len(signals)


# ── Web Scraper Classification Tests ──────────────────────────────


class TestWebScraperClassification:
    def test_injury_classification(self):
        assert WebScraper._classify_signal_type("Player ruled out with knee injury") == "injury_update"

    def test_trade_classification(self):
        assert WebScraper._classify_signal_type("Team exploring trade options") == "trade_rumor"

    def test_rotation_classification(self):
        assert WebScraper._classify_signal_type("Moving to bench, new rotation") == "rotation_change"

    def test_minutes_classification(self):
        assert WebScraper._classify_signal_type("Expected 35 minutes tonight") == "minutes_projection"

    def test_general_fallback(self):
        assert WebScraper._classify_signal_type("NBA All-Star voting results") == "general_take"

    def test_bullish_sentiment(self):
        assert WebScraper._classify_sentiment("Player will start tonight, available") == "bullish"

    def test_bearish_sentiment(self):
        assert WebScraper._classify_sentiment("Player ruled out, sidelined") == "bearish"

    def test_neutral_sentiment(self):
        assert WebScraper._classify_sentiment("NBA schedule announcement") == "neutral"


# ── Service Integration Tests (with mock mode) ────────────────────


class TestExpertSignalServiceMock:
    def test_mock_mode_returns_signals(self):
        import os
        os.environ["SCRAPING_ENABLED"] = "false"
        svc = ExpertSignalService()
        svc._scraping_enabled = False  # Force mock mode
        result = svc.get_signals(team_abbr="DEN", limit=10)
        assert result.total > 0
        assert len(result.signals) <= 10

    def test_sentiment_summary_valid(self):
        import os
        os.environ["SCRAPING_ENABLED"] = "false"
        svc = ExpertSignalService()
        svc._scraping_enabled = False
        result = svc.get_signals(limit=20)
        s = result.sentiment_summary
        assert "bullish" in s
        assert "bearish" in s
        assert "neutral" in s
        total = s["bullish"] + s["bearish"] + s["neutral"]
        assert abs(total - 1.0) < 0.05  # Should sum to ~1.0

    def test_cache_behavior(self):
        svc = ExpertSignalService()
        svc._scraping_enabled = False
        r1 = svc.get_signals(team_abbr="LAL", limit=5)
        r2 = svc.get_signals(team_abbr="LAL", limit=5)
        assert r2.cached is True

    def test_experts_tracked_count(self):
        svc = ExpertSignalService()
        svc._scraping_enabled = False
        result = svc.get_signals(limit=30)
        assert result.experts_tracked > 0
