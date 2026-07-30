"""Tests for CBB props service and sport-aware props routing."""

import os
from unittest.mock import MagicMock, patch

import pytest

from app.services.dk_props_service import (
    CBB_EVENT_GROUP_ID,
    DKPropsService,
    NBA_EVENT_GROUP_ID,
    KNOWN_CATEGORIES,
    _normalize_name,
)
from app.services.cbb_props_service import CBBPropsService
from app.models.props import PlayerPropLine, PlayerProps


# ── CBBPropsService basic tests ──────────────────────────────────────────


class TestCBBPropsServiceInit:
    """CBBPropsService uses the correct event group ID."""

    def test_uses_cbb_event_group_id(self):
        svc = CBBPropsService()
        assert svc._event_group_id == CBB_EVENT_GROUP_ID
        assert svc._event_group_id != NBA_EVENT_GROUP_ID

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("DK_CBB_EVENT_GROUP_ID", "99999")
        svc = CBBPropsService()
        assert svc._event_group_id == 99999

    def test_inherits_dk_props_service(self):
        svc = CBBPropsService()
        assert isinstance(svc, DKPropsService)

    def test_has_all_parent_methods(self):
        svc = CBBPropsService()
        assert hasattr(svc, "get_player_props")
        assert hasattr(svc, "get_player_prop")
        assert hasattr(svc, "compare_projections")
        assert hasattr(svc, "get_props_summary")
        assert hasattr(svc, "_fetch_all_props")
        assert hasattr(svc, "_parse_category_response")


class TestDKPropsServiceParameterized:
    """DKPropsService accepts custom event_group_id."""

    def test_default_is_nba(self):
        svc = DKPropsService()
        assert svc._event_group_id == NBA_EVENT_GROUP_ID

    def test_custom_event_group_id(self):
        svc = DKPropsService(event_group_id=12345)
        assert svc._event_group_id == 12345

    def test_cbb_event_group_via_param(self):
        svc = DKPropsService(event_group_id=CBB_EVENT_GROUP_ID)
        assert svc._event_group_id == CBB_EVENT_GROUP_ID


class TestCBBPropsURLConstruction:
    """Verify _fetch_category_props uses the correct event group in URL."""

    @patch("app.services.dk_props_service.resilient_get")
    def test_cbb_url_uses_cbb_event_group(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"eventGroup": {"offerCategories": []}}
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        svc = CBBPropsService()
        svc._fetch_category_props("pts", 1215)

        # Verify the URL used the CBB event group ID
        call_args = mock_get.call_args
        url = call_args[0][0] if call_args[0] else call_args[1].get("url", "")
        assert str(CBB_EVENT_GROUP_ID) in url
        assert str(NBA_EVENT_GROUP_ID) not in url

    @patch("app.services.dk_props_service.resilient_get")
    def test_nba_url_uses_nba_event_group(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"eventGroup": {"offerCategories": []}}
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        svc = DKPropsService()  # default = NBA
        svc._fetch_category_props("pts", 1215)

        call_args = mock_get.call_args
        url = call_args[0][0] if call_args[0] else call_args[1].get("url", "")
        assert str(NBA_EVENT_GROUP_ID) in url


class TestCBBPropsParsing:
    """CBBPropsService correctly parses DK response (same format as NBA)."""

    def _make_dk_response(self, player_name, team, stat, line_val, over_odds, under_odds):
        """Build a minimal DK Sportsbook API response."""
        return {
            "eventGroup": {
                "offerCategories": [{
                    "offerSubcategoryDescriptors": [{
                        "offerSubcategory": {
                            "offers": [[{
                                "label": f"{player_name} - {stat.upper()}",
                                "sourceText": f"{team}",
                                "criterion": {"eventLabel": "AWAY @ HOME"},
                                "outcomes": [
                                    {
                                        "label": "Over",
                                        "participant": player_name,
                                        "line": line_val,
                                        "oddsAmerican": over_odds,
                                        "participantMetadata": {
                                            "teamAbbreviation": team,
                                        },
                                    },
                                    {
                                        "label": "Under",
                                        "participant": player_name,
                                        "line": line_val,
                                        "oddsAmerican": under_odds,
                                        "participantMetadata": {
                                            "teamAbbreviation": team,
                                        },
                                    },
                                ],
                            }]]
                        }
                    }]
                }]
            }
        }

    def test_parse_cbb_player_props(self):
        svc = CBBPropsService()
        data = self._make_dk_response(
            "Zach Edey", "PUR", "pts", 18.5, "-110", "-110"
        )
        result = svc._parse_category_response(data, "pts")
        assert len(result) == 1
        key = list(result.keys())[0]
        player = result[key]
        assert player.player_name == "Zach Edey"
        assert player.team_abbreviation == "PUR"
        assert len(player.props) == 1
        assert player.props[0].stat == "pts"
        assert player.props[0].line == 18.5

    def test_parse_multiple_cbb_players(self):
        svc = CBBPropsService()
        # Build response with two players in the offers grid
        data = {
            "eventGroup": {
                "offerCategories": [{
                    "offerSubcategoryDescriptors": [{
                        "offerSubcategory": {
                            "offers": [
                                [{
                                    "label": "Cooper Flagg - PTS",
                                    "sourceText": "DUKE",
                                    "criterion": {},
                                    "outcomes": [
                                        {"label": "Over", "participant": "Cooper Flagg",
                                         "line": 21.5, "oddsAmerican": "-120",
                                         "participantMetadata": {"teamAbbreviation": "DUKE"}},
                                        {"label": "Under", "participant": "Cooper Flagg",
                                         "line": 21.5, "oddsAmerican": "+100",
                                         "participantMetadata": {"teamAbbreviation": "DUKE"}},
                                    ],
                                }],
                                [{
                                    "label": "Johni Broome - PTS",
                                    "sourceText": "AUB",
                                    "criterion": {},
                                    "outcomes": [
                                        {"label": "Over", "participant": "Johni Broome",
                                         "line": 16.5, "oddsAmerican": "-105",
                                         "participantMetadata": {"teamAbbreviation": "AUB"}},
                                        {"label": "Under", "participant": "Johni Broome",
                                         "line": 16.5, "oddsAmerican": "-115",
                                         "participantMetadata": {"teamAbbreviation": "AUB"}},
                                    ],
                                }],
                            ]
                        }
                    }]
                }]
            }
        }
        result = svc._parse_category_response(data, "pts")
        assert len(result) == 2

    def test_implied_probabilities_sum_to_one(self):
        svc = CBBPropsService()
        data = self._make_dk_response(
            "RJ Davis", "UNC", "ast", 5.5, "-130", "+110"
        )
        result = svc._parse_category_response(data, "ast")
        player = list(result.values())[0]
        prop = player.props[0]
        assert abs(prop.implied_over_prob + prop.implied_under_prob - 1.0) < 0.001


class TestCBBPropsComparison:
    """compare_projections works with CBB service."""

    def test_compare_with_cbb_props(self):
        svc = CBBPropsService()
        # Manually populate cache
        player_props = PlayerProps(
            player_name="Cooper Flagg",
            team_abbreviation="DUKE",
            props=[
                PlayerPropLine(
                    stat="pts", line=21.5, over_odds=-110, under_odds=-110,
                    implied_over_prob=0.5, implied_under_prob=0.5,
                ),
                PlayerPropLine(
                    stat="reb", line=8.5, over_odds=-115, under_odds=-105,
                    implied_over_prob=0.535, implied_under_prob=0.465,
                ),
            ],
        )
        norm_name = _normalize_name("Cooper Flagg")
        svc._cache = {f"{norm_name}:DUKE": player_props}
        svc._cache_date = "2026-02-15"
        svc._cache_timestamp = float("inf")  # never expires

        comparisons = svc.compare_projections(
            player_name="Cooper Flagg",
            team="DUKE",
            projected_stats={"pts": 23.0, "reb": 9.0, "ast": 3.0},
            game_date="2026-02-15",
        )

        # Should get comparisons for pts and reb (ast has no prop line)
        assert len(comparisons) == 2
        pts_comp = next(c for c in comparisons if c.stat == "pts")
        assert pts_comp.our_projection == 23.0
        assert pts_comp.market_line == 21.5
        assert pts_comp.delta == 1.5
        assert pts_comp.signal == "bullish"  # 1.5 / 21.5 = 6.98% > 5%

    def test_no_props_returns_empty(self):
        svc = CBBPropsService()
        svc._cache = {}
        svc._cache_date = "2026-02-15"
        svc._cache_timestamp = float("inf")

        comparisons = svc.compare_projections(
            player_name="Unknown Player",
            team="XXX",
            projected_stats={"pts": 15.0},
            game_date="2026-02-15",
        )
        assert comparisons == []


class TestCBBPropsCaching:
    """Verify independent caching for NBA and CBB services."""

    def test_separate_cache_instances(self):
        nba_svc = DKPropsService()
        cbb_svc = CBBPropsService()

        # Populate NBA cache
        nba_svc._cache = {"player1:LAL": MagicMock()}
        nba_svc._cache_date = "2026-02-15"

        # CBB cache should be empty
        assert len(cbb_svc._cache) == 0
        assert cbb_svc._cache_date is None

    def test_cache_invalidation_on_date_change(self):
        svc = CBBPropsService()
        svc._cache = {"p:DUKE": MagicMock()}
        svc._cache_date = "2026-02-14"
        svc._cache_timestamp = float("inf")

        # Different date should invalidate
        assert not svc._is_cache_valid("2026-02-15")

        # Same date should be valid
        assert svc._is_cache_valid("2026-02-14")


class TestServiceContainerPropsRouting:
    """get_props_service returns correct service by sport."""

    def test_nba_returns_dk_props(self):
        from app.api.dependencies import ServiceContainer

        container = ServiceContainer()
        container._initialised = True
        container.dk_props_service = DKPropsService()
        container.cbb_props_service = CBBPropsService()

        nba_svc = container.get_props_service("nba")
        assert isinstance(nba_svc, DKPropsService)
        assert nba_svc._event_group_id == NBA_EVENT_GROUP_ID

    def test_cbb_returns_cbb_props(self):
        from app.api.dependencies import ServiceContainer

        container = ServiceContainer()
        container._initialised = True
        container.dk_props_service = DKPropsService()
        container.cbb_props_service = CBBPropsService()

        cbb_svc = container.get_props_service("cbb")
        assert isinstance(cbb_svc, CBBPropsService)
        assert cbb_svc._event_group_id == CBB_EVENT_GROUP_ID

    def test_default_returns_nba(self):
        from app.api.dependencies import ServiceContainer

        container = ServiceContainer()
        container._initialised = True
        container.dk_props_service = DKPropsService()
        container.cbb_props_service = CBBPropsService()

        default_svc = container.get_props_service()
        assert default_svc._event_group_id == NBA_EVENT_GROUP_ID
