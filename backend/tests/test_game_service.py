"""Tests for GameService — slate game matching with DK abbreviation normalization."""

import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

from app.config.constants import DK_TO_NBA_ABBR_ALIASES
from app.models.game import DFSSlate, GameInfo, TeamGameStats
from app.services.dk_slate_service import DK_TEAM_ID_TO_NBA_ABBR, DKSlateInfo
from app.services.game_service import GameService


# ---------------------------------------------------------------------------
# Helpers — build mock objects for tests
# ---------------------------------------------------------------------------

def _make_team_stats(abbr: str, team_id: int = 1) -> TeamGameStats:
    """Create a minimal TeamGameStats for testing."""
    return TeamGameStats(
        team_id=team_id,
        team_name=abbr,
        team_abbreviation=abbr,
        season_pace=100.0,
        season_off_rating=110.0,
        season_def_rating=110.0,
        season_ppg=110.0,
        season_opp_ppg=110.0,
        last_5_ppg=110.0,
    )


def _make_game(game_id: str, away_abbr: str, home_abbr: str, seq: int = 1) -> GameInfo:
    """Create a minimal GameInfo using NBA-standard abbreviations."""
    return GameInfo(
        game_id=game_id,
        game_date="2026-02-20",
        game_sequence=seq,
        game_status="Scheduled",
        home_team=_make_team_stats(home_abbr, team_id=seq * 10),
        away_team=_make_team_stats(away_abbr, team_id=seq * 10 + 1),
        projected_total=220.0,
        projected_home_score=112.0,
        projected_away_score=108.0,
        projected_spread=-4.0,
        projected_pace=100.0,
        pace_label="Average",
    )


def _make_dk_slate(
    games: List[Dict],
    name: str = "Main",
    draft_group_id: int = 99999,
) -> DKSlateInfo:
    """Create a DKSlateInfo with the given game dicts."""
    now = datetime.now(timezone.utc)
    return DKSlateInfo(
        draft_group_id=draft_group_id,
        name=name,
        label=f"{name} (7:00 PM ET+)",
        game_count=len(games),
        min_start_utc=now,
        max_start_utc=now,
        games=games,
    )


def _dk_game_dict(
    away_abbr: str,
    home_abbr: str,
    away_team_dk_id: Optional[int] = None,
    home_team_dk_id: Optional[int] = None,
) -> Dict:
    """Build a DK-style game dict as produced by DKSlateService._parse_draftgroup."""
    return {
        "dk_game_id": 12345,
        "away_abbr": away_abbr,
        "home_abbr": home_abbr,
        "away_team_dk_id": away_team_dk_id,
        "home_team_dk_id": home_team_dk_id,
        "start_utc": datetime.now(timezone.utc),
        "description": f"{away_abbr} @ {home_abbr}",
        "location": "",
        "status": "",
    }


# ---------------------------------------------------------------------------
# Tests for _build_slates() abbreviation normalization
# ---------------------------------------------------------------------------

class TestBuildSlatesAbbreviationNormalization:
    """Verify that _build_slates() normalizes DK abbreviations to NBA API standard."""

    def _build_service(self, dk_slates: List[DKSlateInfo]) -> GameService:
        """Create a GameService with mocked DK slate service."""
        svc = GameService.__new__(GameService)
        svc._dk_slate_service = MagicMock()
        svc._dk_slate_service.get_slates.return_value = dk_slates
        return svc

    def test_dk_abbreviation_normalization_sa_pho(self):
        """SA @ PHO should match SAS @ PHX game from NBA API."""
        dk_game = _dk_game_dict("SA", "PHO")
        dk_slate = _make_dk_slate([dk_game])

        nba_games = [_make_game("0022600001", "SAS", "PHX", seq=1)]
        svc = self._build_service([dk_slate])

        slates = svc._build_slates(nba_games)
        assert len(slates) == 1
        assert slates[0].game_count == 1
        assert slates[0].games[0].away_team.team_abbreviation == "SAS"
        assert slates[0].games[0].home_team.team_abbreviation == "PHX"

    def test_dk_abbreviation_normalization_gs_ny(self):
        """GS @ NY should match GSW @ NYK game from NBA API."""
        dk_game = _dk_game_dict("GS", "NY")
        dk_slate = _make_dk_slate([dk_game])

        nba_games = [_make_game("0022600002", "GSW", "NYK", seq=1)]
        svc = self._build_service([dk_slate])

        slates = svc._build_slates(nba_games)
        assert len(slates) == 1
        assert slates[0].game_count == 1
        assert slates[0].games[0].away_team.team_abbreviation == "GSW"

    def test_standard_abbreviations_unchanged(self):
        """ATL @ BOS (same in DK and NBA) should still match correctly."""
        dk_game = _dk_game_dict("ATL", "BOS")
        dk_slate = _make_dk_slate([dk_game])

        nba_games = [_make_game("0022600003", "ATL", "BOS", seq=1)]
        svc = self._build_service([dk_slate])

        slates = svc._build_slates(nba_games)
        assert len(slates) == 1
        assert slates[0].game_count == 1

    def test_dk_team_id_fallback(self):
        """When abbreviations are '???', DK team ID should resolve them."""
        dk_game = _dk_game_dict(
            "???", "???",
            away_team_dk_id=24,   # SAS in DK_TEAM_ID_TO_NBA_ABBR
            home_team_dk_id=21,   # PHX in DK_TEAM_ID_TO_NBA_ABBR
        )
        dk_slate = _make_dk_slate([dk_game])

        nba_games = [_make_game("0022600004", "SAS", "PHX", seq=1)]
        svc = self._build_service([dk_slate])

        slates = svc._build_slates(nba_games)
        assert len(slates) == 1
        assert slates[0].game_count == 1

    def test_unmatched_game_logged(self, caplog):
        """Completely unknown abbreviation should be dropped with a warning."""
        dk_game = _dk_game_dict("XYZ", "QQQ")
        dk_slate = _make_dk_slate([dk_game])

        nba_games = [_make_game("0022600005", "ATL", "BOS", seq=1)]
        svc = self._build_service([dk_slate])

        with caplog.at_level(logging.WARNING):
            slates = svc._build_slates(nba_games)

        # No games matched → should fallback to single slate
        assert any("No NBA game match" in msg for msg in caplog.messages)

    def test_multi_game_slate_mixed_abbreviations(self):
        """Slate with a mix of standard and DK-only abbreviations."""
        dk_games = [
            _dk_game_dict("ATL", "BOS"),     # Standard
            _dk_game_dict("SA", "PHO"),       # DK-only → SAS @ PHX
            _dk_game_dict("GS", "NY"),        # DK-only → GSW @ NYK
        ]
        dk_slate = _make_dk_slate(dk_games)

        nba_games = [
            _make_game("001", "ATL", "BOS", seq=1),
            _make_game("002", "SAS", "PHX", seq=2),
            _make_game("003", "GSW", "NYK", seq=3),
        ]
        svc = self._build_service([dk_slate])

        slates = svc._build_slates(nba_games)
        assert len(slates) == 1
        assert slates[0].game_count == 3


# ---------------------------------------------------------------------------
# Tests for _find_matching_game (static method — no normalization here)
# ---------------------------------------------------------------------------

class TestFindMatchingGame:
    """_find_matching_game uses exact matching (normalization is caller's job)."""

    def test_exact_match(self):
        games = [_make_game("001", "ATL", "BOS")]
        match = GameService._find_matching_game("ATL", "BOS", games, set())
        assert match is not None
        assert match.game_id == "001"

    def test_no_match_returns_none(self):
        games = [_make_game("001", "ATL", "BOS")]
        match = GameService._find_matching_game("SA", "PHO", games, set())
        assert match is None

    def test_excludes_already_matched(self):
        games = [_make_game("001", "ATL", "BOS")]
        match = GameService._find_matching_game("ATL", "BOS", games, {"001"})
        assert match is None


# ---------------------------------------------------------------------------
# Tests for DK_TO_NBA_ABBR_ALIASES completeness
# ---------------------------------------------------------------------------

class TestDKAbbreviationAliases:
    """Verify the shared alias map covers all known DK divergences."""

    def test_all_aliases_map_to_known_nba_abbreviations(self):
        """Every alias target should be a valid NBA team abbreviation."""
        from nba_api.stats.static import teams as nba_teams
        valid_abbrs = {t["abbreviation"] for t in nba_teams.get_teams()}
        for dk_abbr, nba_abbr in DK_TO_NBA_ABBR_ALIASES.items():
            assert nba_abbr in valid_abbrs, (
                f"DK alias {dk_abbr!r} → {nba_abbr!r} is not a valid NBA abbreviation"
            )

    def test_no_alias_key_is_already_standard(self):
        """Alias keys should NOT be standard NBA abbreviations (that would be redundant)."""
        from nba_api.stats.static import teams as nba_teams
        valid_abbrs = {t["abbreviation"] for t in nba_teams.get_teams()}
        for dk_abbr in DK_TO_NBA_ABBR_ALIASES:
            assert dk_abbr not in valid_abbrs, (
                f"DK alias key {dk_abbr!r} is already a standard NBA abbreviation — redundant"
            )

    def test_dk_team_id_map_agrees_with_alias_targets(self):
        """DK team IDs that map to alias targets should be consistent."""
        # Example: DK ID 21 → PHX, and "PHO" alias → PHX — both should agree
        assert DK_TEAM_ID_TO_NBA_ABBR.get(21) == DK_TO_NBA_ABBR_ALIASES.get("PHO")  # PHX
        assert DK_TEAM_ID_TO_NBA_ABBR.get(24) == DK_TO_NBA_ABBR_ALIASES.get("SA")   # SAS
        assert DK_TEAM_ID_TO_NBA_ABBR.get(9) == DK_TO_NBA_ABBR_ALIASES.get("GS")    # GSW
        assert DK_TEAM_ID_TO_NBA_ABBR.get(18) == DK_TO_NBA_ABBR_ALIASES.get("NY")   # NYK


# ---------------------------------------------------------------------------
# BDL integration — data_service wiring
# ---------------------------------------------------------------------------


def _make_game_service_with_data_service(mock_data_service):
    """Create a GameService with mocked _data_service injected."""
    from datetime import date

    svc = GameService.__new__(GameService)
    svc._last_request_time = 0.0
    svc._min_request_interval = 0.0  # No rate limit in tests
    svc._team_stats_cache = None
    svc._team_stats_cache_date = None
    svc._schedule_cache = {}
    svc._schedule_cache_ts = {}
    svc._scoreboard_header_cache = {}
    svc._dk_slate_service = MagicMock()
    svc._dk_slate_service.get_slates.return_value = []
    svc._db_cache = None
    svc._data_service = mock_data_service
    svc._bdl = None            # BDL odds fallback disabled in tests
    svc._odds_service = None   # The Odds API fallback disabled in tests
    svc._odds_fetcher = None   # unified OddsFetcherService not wired → legacy odds chain
    svc._last_5_cache = {}
    return svc


class TestGameServiceBDLIntegration:
    """Verify GameService uses _data_service (BDL) for scoreboard."""

    CHI_ID = 1610612741
    ATL_ID = 1610612737

    def _bdl_game_header(self, game_id="9001", home_id=1610612741, away_id=1610612737):
        return {
            "GAME_ID": game_id,
            "HOME_TEAM_ID": home_id,
            "VISITOR_TEAM_ID": away_id,
            "GAME_STATUS_TEXT": "7:00 PM ET",
            "GAME_STATUS_ID": 1,
            "GAME_SEQUENCE": 1,
            "_source": "balldontlie",
        }

    def test_has_game_on_date_uses_data_service(self):
        """has_game_on_date() tries _data_service before ScoreboardV2."""
        mock_ds = MagicMock()
        mock_ds.get_scoreboard_for_date.return_value = [
            self._bdl_game_header(),
        ]
        svc = _make_game_service_with_data_service(mock_ds)

        assert svc.has_game_on_date(self.CHI_ID, "2026-02-24") is True
        assert svc.has_game_on_date(self.ATL_ID, "2026-02-24") is True
        # Should use cached headers on second call
        assert mock_ds.get_scoreboard_for_date.call_count == 1

    def test_has_game_on_date_team_not_playing(self):
        """has_game_on_date() returns False for team not in any game."""
        mock_ds = MagicMock()
        mock_ds.get_scoreboard_for_date.return_value = [
            self._bdl_game_header(),
        ]
        svc = _make_game_service_with_data_service(mock_ds)

        BOS_ID = 1610612738
        assert svc.has_game_on_date(BOS_ID, "2026-02-24") is False

    def test_has_game_on_date_data_service_failure_falls_back(self):
        """ScoreboardV2 fallback when data_service fails is gated by
        settings.skip_nba_api_live (default True → fail closed, no
        stats.nba.com call; False → fallback engages)."""
        mock_ds = MagicMock()
        mock_ds.get_scoreboard_for_date.side_effect = Exception("BDL down")

        with patch(
            "app.services.game_service.scoreboardv2.ScoreboardV2"
        ) as mock_sb:
            mock_sb.return_value.get_normalized_dict.return_value = {
                "GameHeader": [
                    {"HOME_TEAM_ID": self.CHI_ID, "VISITOR_TEAM_ID": self.ATL_ID}
                ]
            }

            # Default config (skip_nba_api_live=True): stats.nba.com is
            # never called from a user-facing path — check fails closed.
            svc = _make_game_service_with_data_service(mock_ds)
            assert svc.has_game_on_date(self.CHI_ID, "2026-02-24") is False
            mock_sb.assert_not_called()

            # With stats.nba.com enabled, ScoreboardV2 fallback engages.
            svc2 = _make_game_service_with_data_service(mock_ds)
            with patch(
                "app.services.game_service.settings.skip_nba_api_live", False
            ):
                assert svc2.has_game_on_date(self.CHI_ID, "2026-02-24") is True

    def test_has_game_on_date_no_data_service(self):
        """Without _data_service, ScoreboardV2 is used only when
        stats.nba.com is enabled (skip_nba_api_live=False); the default
        config fails closed without calling it."""
        with patch(
            "app.services.game_service.scoreboardv2.ScoreboardV2"
        ) as mock_sb:
            mock_sb.return_value.get_normalized_dict.return_value = {
                "GameHeader": [
                    {"HOME_TEAM_ID": self.CHI_ID, "VISITOR_TEAM_ID": self.ATL_ID}
                ]
            }

            # Default config: no data service AND no stats.nba.com → False.
            svc = _make_game_service_with_data_service(None)
            assert svc.has_game_on_date(self.CHI_ID, "2026-02-24") is False
            mock_sb.assert_not_called()

            # stats.nba.com enabled → goes directly to ScoreboardV2.
            svc2 = _make_game_service_with_data_service(None)
            with patch(
                "app.services.game_service.settings.skip_nba_api_live", False
            ):
                assert svc2.has_game_on_date(self.CHI_ID, "2026-02-24") is True

    def test_all_star_filter_bdl_source(self):
        """BDL-sourced games with non-franchise team IDs are filtered."""
        from datetime import date

        mock_ds = MagicMock()
        mock_ds.get_scoreboard_for_date.return_value = [
            # Valid franchise game
            self._bdl_game_header(game_id="9001", home_id=self.CHI_ID, away_id=self.ATL_ID),
            # All-Star game with non-franchise team IDs
            {
                "GAME_ID": "9002",
                "HOME_TEAM_ID": 999999,
                "VISITOR_TEAM_ID": 999998,
                "GAME_STATUS_TEXT": "8:00 PM ET",
                "GAME_STATUS_ID": 1,
                "GAME_SEQUENCE": 2,
                "_source": "balldontlie",
            },
        ]
        svc = _make_game_service_with_data_service(mock_ds)

        # Mock team stats and odds to avoid real API calls
        svc._get_all_team_stats = MagicMock(return_value={
            self.CHI_ID: {
                "team_name": "Chicago Bulls", "ppg": 110.0, "pace": 100.0,
                "off_rating": 112.0, "def_rating": 110.0, "w": 30, "l": 20,
                "opp_pts_pg": 110.0, "opp_reb_pg": 43.0, "opp_ast_pg": 25.0,
                "opp_stl_pg": 7.5, "opp_blk_pg": 5.0, "opp_tov_pg": 14.0,
                "opp_fg3m_pg": 12.0,
            },
            self.ATL_ID: {
                "team_name": "Atlanta Hawks", "ppg": 108.0, "pace": 99.0,
                "off_rating": 110.0, "def_rating": 112.0, "w": 25, "l": 25,
                "opp_pts_pg": 112.0, "opp_reb_pg": 44.0, "opp_ast_pg": 26.0,
                "opp_stl_pg": 8.0, "opp_blk_pg": 4.5, "opp_tov_pg": 13.0,
                "opp_fg3m_pg": 13.0,
            },
        })
        svc._fetch_odds = MagicMock(return_value={})
        svc._get_team_last_5 = MagicMock(return_value=110.0)

        schedule = svc.get_games(date.today().isoformat())
        # Only the valid franchise game should pass the filter
        assert schedule.game_count == 1
        assert schedule.games[0].game_id == "9001"

    def test_bdl_source_skips_nba_api_last5(self):
        """When BDL provides scoreboard, _get_team_last_5 is called with skip_api=True.

        This prevents 20+ slow per-team NBA API calls when stats.nba.com is
        unreachable.  Season PPG is used instead of last-5 (negligible accuracy
        difference, but avoids minutes-long page loads).
        """
        from datetime import date
        from unittest.mock import call

        mock_ds = MagicMock()
        mock_ds.get_scoreboard_for_date.return_value = [
            self._bdl_game_header(game_id="9001", home_id=self.CHI_ID, away_id=self.ATL_ID),
        ]
        svc = _make_game_service_with_data_service(mock_ds)

        svc._get_all_team_stats = MagicMock(return_value={
            self.CHI_ID: {
                "team_name": "Chicago Bulls", "ppg": 110.0, "pace": 100.0,
                "off_rating": 112.0, "def_rating": 110.0, "w": 30, "l": 20,
                "opp_pts_pg": 110.0, "opp_reb_pg": 43.0, "opp_ast_pg": 25.0,
                "opp_stl_pg": 7.5, "opp_blk_pg": 5.0, "opp_tov_pg": 14.0,
                "opp_fg3m_pg": 12.0,
            },
            self.ATL_ID: {
                "team_name": "Atlanta Hawks", "ppg": 108.0, "pace": 99.0,
                "off_rating": 110.0, "def_rating": 112.0, "w": 25, "l": 25,
                "opp_pts_pg": 112.0, "opp_reb_pg": 44.0, "opp_ast_pg": 26.0,
                "opp_stl_pg": 8.0, "opp_blk_pg": 4.5, "opp_tov_pg": 13.0,
                "opp_fg3m_pg": 13.0,
            },
        })
        svc._fetch_odds = MagicMock(return_value={})

        # Track _get_team_last_5 calls to verify skip_api=True
        original_last_5 = svc._get_team_last_5
        last_5_calls = []

        def _track_last_5(team_id, season=None, *, skip_api=False):
            last_5_calls.append({"team_id": team_id, "skip_api": skip_api})
            return 0.0  # skip_api=True returns 0.0

        svc._get_team_last_5 = _track_last_5

        schedule = svc.get_games(date.today().isoformat())
        assert schedule.game_count == 1

        # Both teams should have been called with skip_api=True
        assert len(last_5_calls) == 2
        assert all(c["skip_api"] is True for c in last_5_calls)

        # Since last-5 returns 0.0, season PPG should be used as fallback
        assert schedule.games[0].home_team.last_5_ppg == 110.0  # season ppg
        assert schedule.games[0].away_team.last_5_ppg == 108.0  # season ppg
