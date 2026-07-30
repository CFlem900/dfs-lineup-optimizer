"""Tests for NewsService — model, dedup, relevance, team extraction, caching."""

import time
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

import pytest

from app.models.news import NewsItem, NewsResponse
from app.services.news_service import (
    NewsService,
    _make_id,
    _classify_relevance,
    _extract_teams,
    _team_ids_from_abbrs,
    NBA_TEAM_LOOKUP,
)


# ── Model tests ────────────────────────────────────────────────────


class TestNewsItemModel:
    def test_minimal_news_item(self):
        item = NewsItem(
            id="abc123",
            headline="LeBron scores 40",
            source="ESPN",
            published_at=datetime.now(timezone.utc),
        )
        assert item.headline == "LeBron scores 40"
        assert item.relevance == "general"
        assert item.team_ids == []
        assert item.player_names == []

    def test_full_news_item(self):
        item = NewsItem(
            id="xyz789",
            headline="Jokic out with knee injury",
            summary="Denver Nuggets star Nikola Jokic will miss...",
            url="https://espn.com/article",
            source="ESPN",
            source_icon="ESPN",
            published_at=datetime(2025, 1, 15, tzinfo=timezone.utc),
            relevance="injury",
            team_ids=[1610612743],
            team_names=["DEN"],
            player_names=["Nikola Jokic"],
        )
        assert item.relevance == "injury"
        assert 1610612743 in item.team_ids
        assert "DEN" in item.team_names

    def test_news_response_model(self):
        resp = NewsResponse(
            items=[],
            total=0,
            cached=True,
            last_fetched=datetime.now(timezone.utc),
        )
        assert resp.total == 0
        assert resp.cached is True


# ── Dedup ID tests ─────────────────────────────────────────────────


class TestMakeId:
    def test_deterministic(self):
        id1 = _make_id("LeBron scores 40", "ESPN")
        id2 = _make_id("LeBron scores 40", "ESPN")
        assert id1 == id2

    def test_different_sources_differ(self):
        id1 = _make_id("LeBron scores 40", "ESPN")
        id2 = _make_id("LeBron scores 40", "NBA.com")
        assert id1 != id2

    def test_case_insensitive(self):
        id1 = _make_id("LeBron Scores 40", "ESPN")
        id2 = _make_id("lebron scores 40", "ESPN")
        assert id1 == id2

    def test_length(self):
        result = _make_id("any headline", "ESPN")
        assert len(result) == 12


# ── Relevance classification tests ─────────────────────────────────


class TestClassifyRelevance:
    def test_injury_keyword(self):
        assert _classify_relevance("Jokic ruled out with knee injury") == "injury"

    def test_trade_keyword(self):
        assert _classify_relevance("Lakers trade for star center") == "trade"

    def test_lineup_keyword(self):
        assert _classify_relevance("Murray will start tonight") == "lineup"

    def test_general_fallback(self):
        assert _classify_relevance("NBA All-Star voting results") == "general"

    def test_injury_takes_priority(self):
        # Injury should win over lineup when both present
        assert _classify_relevance("Player returning from injury, will start") == "injury"

    def test_questionable_status(self):
        assert _classify_relevance("Curry questionable for tonight") == "injury"

    def test_free_agent(self):
        assert _classify_relevance("Team signs free agent guard") == "trade"

    def test_rotation_keyword(self):
        assert _classify_relevance("Coach adjusts rotation for playoffs") == "lineup"


# ── Team extraction tests ──────────────────────────────────────────


class TestExtractTeams:
    def test_abbreviation(self):
        teams = _extract_teams("DEN vs LAL tonight")
        assert "DEN" in teams
        assert "LAL" in teams

    def test_team_nickname(self):
        teams = _extract_teams("The Nuggets beat the Lakers")
        assert "DEN" in teams
        assert "LAL" in teams

    def test_city_name(self):
        teams = _extract_teams("Denver wins in overtime against Boston")
        assert "DEN" in teams
        assert "BOS" in teams

    def test_no_teams(self):
        teams = _extract_teams("NBA announces new schedule format")
        assert teams == []

    def test_multiple_teams(self):
        teams = _extract_teams("Trade between Celtics, Heat, and Thunder")
        assert "BOS" in teams
        assert "MIA" in teams
        assert "OKC" in teams

    def test_sixers_alias(self):
        teams = _extract_teams("Sixers dominate the Knicks")
        assert "PHI" in teams
        assert "NYK" in teams


class TestTeamIdsFromAbbrs:
    def test_known_teams(self):
        ids = _team_ids_from_abbrs(["DEN", "LAL"])
        assert 1610612743 in ids  # DEN
        assert 1610612747 in ids  # LAL

    def test_unknown_abbr_skipped(self):
        ids = _team_ids_from_abbrs(["DEN", "XYZ"])
        assert len(ids) == 1

    def test_empty_list(self):
        assert _team_ids_from_abbrs([]) == []


# ── Cache behavior tests ───────────────────────────────────────────


class TestNewsServiceCache:
    def test_cache_initially_invalid(self):
        svc = NewsService()
        assert svc._is_cache_valid() is False

    def test_cache_valid_after_refresh(self):
        svc = NewsService()
        svc._cache_timestamp = time.time()
        svc._cache = []
        assert svc._is_cache_valid() is True

    def test_cache_expires(self):
        svc = NewsService()
        svc._cache_timestamp = time.time() - 700  # > 600s TTL
        assert svc._is_cache_valid() is False


# ── Dedup behavior tests ──────────────────────────────────────────


class TestDeduplication:
    def test_same_headline_same_source_deduped(self):
        svc = NewsService()
        item1 = NewsItem(
            id=_make_id("Breaking: Injury update", "ESPN"),
            headline="Breaking: Injury update",
            source="ESPN",
            published_at=datetime.now(timezone.utc),
        )
        item2 = NewsItem(
            id=_make_id("Breaking: Injury update", "ESPN"),
            headline="Breaking: Injury update",
            source="ESPN",
            published_at=datetime.now(timezone.utc),
        )
        # Simulate refresh behavior
        svc._seen_ids = set()
        results = []
        for item in [item1, item2]:
            if item.id not in svc._seen_ids:
                svc._seen_ids.add(item.id)
                results.append(item)
        assert len(results) == 1

    def test_same_headline_different_source_both_kept(self):
        svc = NewsService()
        id1 = _make_id("Breaking: Injury update", "ESPN")
        id2 = _make_id("Breaking: Injury update", "NBA.com")
        svc._seen_ids = set()
        results = []
        for item_id in [id1, id2]:
            if item_id not in svc._seen_ids:
                svc._seen_ids.add(item_id)
                results.append(item_id)
        assert len(results) == 2


# ── Filter behavior tests ─────────────────────────────────────────


class TestNewsFiltering:
    def _make_items(self):
        return [
            NewsItem(
                id="a",
                headline="Denver news",
                source="ESPN",
                published_at=datetime(2025, 6, 1, 12, 0, tzinfo=timezone.utc),
                team_ids=[1610612743],  # DEN
                team_names=["DEN"],
            ),
            NewsItem(
                id="b",
                headline="Lakers news",
                source="ESPN",
                published_at=datetime(2025, 6, 1, 11, 0, tzinfo=timezone.utc),
                team_ids=[1610612747],  # LAL
                team_names=["LAL"],
            ),
            NewsItem(
                id="c",
                headline="General NBA news",
                source="ESPN",
                published_at=datetime(2025, 6, 1, 10, 0, tzinfo=timezone.utc),
                team_ids=[],
                team_names=[],
            ),
        ]

    def test_filter_by_team_id(self):
        svc = NewsService()
        svc._cache = self._make_items()
        svc._cache_timestamp = time.time()

        items, cached = svc.get_news(team_ids=[1610612743])
        assert cached is True
        assert len(items) == 1
        assert items[0].id == "a"

    def test_filter_by_multiple_team_ids(self):
        svc = NewsService()
        svc._cache = self._make_items()
        svc._cache_timestamp = time.time()

        items, _ = svc.get_news(team_ids=[1610612743, 1610612747])
        assert len(items) == 2

    def test_no_filter_returns_all(self):
        svc = NewsService()
        svc._cache = self._make_items()
        svc._cache_timestamp = time.time()

        items, _ = svc.get_news()
        assert len(items) == 3

    def test_sorted_by_published_at_descending(self):
        svc = NewsService()
        svc._cache = self._make_items()
        svc._cache_timestamp = time.time()

        items, _ = svc.get_news()
        assert items[0].id == "a"  # most recent
        assert items[-1].id == "c"  # oldest

    def test_limit_respected(self):
        svc = NewsService()
        svc._cache = self._make_items()
        svc._cache_timestamp = time.time()

        items, _ = svc.get_news(limit=2)
        assert len(items) == 2


# ── RSS date parsing ──────────────────────────────────────────────


class TestRSSDateParsing:
    """Test the _parse_rss_date static method that replaced _parse_relative_time."""

    def test_rfc822_format(self):
        result = NewsService._parse_rss_date("Mon, 10 Feb 2025 14:30:00 GMT")
        assert result.month == 2
        assert result.day == 10
        assert result.hour == 14

    def test_iso8601_with_tz(self):
        result = NewsService._parse_rss_date("2025-01-15T10:00:00+00:00")
        assert result.month == 1
        assert result.day == 15
        assert result.hour == 10

    def test_iso8601_with_z(self):
        result = NewsService._parse_rss_date("2025-06-01T08:30:00Z")
        assert result.month == 6
        assert result.day == 1

    def test_date_time_no_tz(self):
        result = NewsService._parse_rss_date("2025-03-20 18:45:00")
        assert result.month == 3
        assert result.day == 20
        assert result.tzinfo is not None  # should get UTC assigned

    def test_unknown_format_returns_now(self):
        result = NewsService._parse_rss_date("some random text")
        expected = datetime.now(timezone.utc)
        assert abs((result - expected).total_seconds()) < 5
