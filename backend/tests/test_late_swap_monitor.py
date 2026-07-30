"""Unit tests for the LateSwapMonitor service."""

import pytest
from unittest.mock import MagicMock

from app.services.late_swap_monitor import LateSwapMonitor


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_lineup(players: list[dict]) -> MagicMock:
    """Return a mock OptimizedLineup with the given players list."""
    lineup = MagicMock()
    lineup.players = players
    return lineup


def _make_player(
    name: str,
    team: str = "LAL",
    position: str = "PG",
    slot: str = "PG",
    salary: int = 7000,
    projected_fp: float = 35.0,
) -> dict:
    return {
        "player_name": name,
        "team": team,
        "position": position,
        "slot": slot,
        "salary": salary,
        "projected_fp": projected_fp,
    }


def _build_monitor(
    injuries=None,
    news_resp=None,
    injury_raises=False,
    optimizer=None,
):
    """Construct a LateSwapMonitor with mocked services.

    Parameters
    ----------
    injuries : dict | list | None
        Return value of injury_service.get_all_injuries().
    news_resp : object | None
        Return value of news_service.get_news().
    injury_raises : bool
        If True, get_all_injuries() will raise an Exception.
    optimizer : MagicMock | None
        Optional lineup_optimizer_service mock.
    """
    injury_svc = MagicMock()
    if injury_raises:
        injury_svc.get_all_injuries.side_effect = Exception("Injury API unavailable")
    else:
        injury_svc.get_all_injuries.return_value = injuries or {}

    game_svc = MagicMock()

    news_svc = MagicMock()
    news_svc.get_news.return_value = news_resp

    return LateSwapMonitor(
        injury_service=injury_svc,
        game_service=game_svc,
        news_service=news_svc,
        lineup_optimizer_service=optimizer,
    )


# ---------------------------------------------------------------------------
# 1. check_for_updates returns the expected keys
# ---------------------------------------------------------------------------

class TestCheckForUpdatesStructure:
    def test_check_for_updates_structure(self):
        monitor = _build_monitor()
        result = monitor.check_for_updates()

        assert "injury_updates" in result
        assert "news_updates" in result
        assert "checked_at" in result
        assert "has_critical_updates" in result
        assert isinstance(result["injury_updates"], list)
        assert isinstance(result["news_updates"], list)
        assert isinstance(result["has_critical_updates"], bool)
        # checked_at should be an ISO timestamp string
        assert isinstance(result["checked_at"], str)
        assert "T" in result["checked_at"]


# ---------------------------------------------------------------------------
# 2. Injuries in dict format (team -> list of players)
# ---------------------------------------------------------------------------

class TestInjuriesDictFormat:
    def test_injuries_dict_format(self):
        injuries = {
            "LAL": [
                {"player_name": "LeBron James", "status": "Out", "comment": "knee"},
            ],
        }
        monitor = _build_monitor(injuries=injuries)
        result = monitor.check_for_updates()

        assert len(result["injury_updates"]) == 1
        update = result["injury_updates"][0]
        assert update["player_name"] == "LeBron James"
        assert update["status"] == "Out"
        assert update["team"] == "LAL"
        assert update["is_critical"] is True
        assert result["has_critical_updates"] is True


# ---------------------------------------------------------------------------
# 3. Injuries in list format
# ---------------------------------------------------------------------------

class TestInjuriesListFormat:
    def test_injuries_list_format(self):
        """get_at_risk_players handles injuries returned as a flat list."""
        injuries = [
            {"player_name": "Anthony Davis", "status": "Questionable", "comment": "ankle"},
        ]
        lineup = _make_lineup([_make_player("Anthony Davis", team="LAL")])
        monitor = _build_monitor(injuries=injuries)

        at_risk = monitor.get_at_risk_players([lineup])

        assert len(at_risk) == 1
        assert at_risk[0]["player_name"] == "Anthony Davis"
        assert at_risk[0]["injury_status"] == "Questionable"
        assert at_risk[0]["risk_level"] == "medium"


# ---------------------------------------------------------------------------
# 4. Player with "Out" status -> high risk
# ---------------------------------------------------------------------------

class TestAtRiskOutStatus:
    def test_at_risk_out_status(self):
        injuries = {
            "MIL": [
                {"player_name": "Giannis Antetokounmpo", "status": "Out", "comment": "knee soreness"},
            ],
        }
        lineup = _make_lineup([_make_player("Giannis Antetokounmpo", team="MIL", position="PF")])
        monitor = _build_monitor(injuries=injuries)

        at_risk = monitor.get_at_risk_players([lineup])

        assert len(at_risk) == 1
        entry = at_risk[0]
        assert entry["risk_level"] == "high"
        assert entry["injury_status"] == "Out"
        assert entry["injury_detail"] == "knee soreness"
        assert entry["lineup_index"] == 0


# ---------------------------------------------------------------------------
# 5. Player with "Questionable" status -> medium risk
# ---------------------------------------------------------------------------

class TestAtRiskQuestionableStatus:
    def test_at_risk_questionable_status(self):
        injuries = {
            "BOS": [
                {"player_name": "Jaylen Brown", "status": "Questionable", "comment": "hamstring"},
            ],
        }
        lineup = _make_lineup([_make_player("Jaylen Brown", team="BOS", position="SG")])
        monitor = _build_monitor(injuries=injuries)

        at_risk = monitor.get_at_risk_players([lineup])

        assert len(at_risk) == 1
        assert at_risk[0]["risk_level"] == "medium"
        assert at_risk[0]["injury_status"] == "Questionable"


# ---------------------------------------------------------------------------
# 6. Injury fetch failure -> graceful degradation, empty injuries
# ---------------------------------------------------------------------------

class TestInjuryFetchFailure:
    def test_injury_fetch_failure(self):
        """When get_all_injuries raises, get_at_risk_players returns empty list."""
        lineup = _make_lineup([_make_player("Stephen Curry", team="GSW")])
        monitor = _build_monitor(injury_raises=True)

        at_risk = monitor.get_at_risk_players([lineup])
        assert at_risk == []

    def test_injury_fetch_failure_check_for_updates(self):
        """check_for_updates also degrades gracefully on injury error."""
        monitor = _build_monitor(injury_raises=True)
        result = monitor.check_for_updates()
        assert result["injury_updates"] == []
        assert result["has_critical_updates"] is False


# ---------------------------------------------------------------------------
# 7. News keyword matching — "ruled out" appears in news_updates
# ---------------------------------------------------------------------------

class TestNewsKeywordMatching:
    def test_news_keyword_matching(self):
        article = MagicMock()
        article.title = "Star player ruled out for tonight's game"
        article.source = "ESPN"
        article.published_at = "2026-02-13T18:00:00Z"

        news_resp = MagicMock()
        news_resp.articles = [article]

        monitor = _build_monitor(news_resp=news_resp)
        result = monitor.check_for_updates()

        assert len(result["news_updates"]) == 1
        news_item = result["news_updates"][0]
        assert news_item["title"] == "Star player ruled out for tonight's game"
        assert news_item["source"] == "ESPN"
        assert news_item["is_positive"] is False

    def test_positive_news_keyword(self):
        """A 'will play' headline should be flagged as is_positive=True."""
        article = MagicMock()
        article.title = "Guard upgraded and will play tonight"
        article.source = "Bleacher Report"
        article.published_at = "2026-02-13T19:00:00Z"

        news_resp = MagicMock()
        news_resp.articles = [article]

        monitor = _build_monitor(news_resp=news_resp)
        result = monitor.check_for_updates()

        assert len(result["news_updates"]) == 1
        assert result["news_updates"][0]["is_positive"] is True


# ---------------------------------------------------------------------------
# 8. Empty lineups -> empty at-risk list
# ---------------------------------------------------------------------------

class TestEmptyLineups:
    def test_empty_lineups(self):
        injuries = {
            "LAL": [
                {"player_name": "LeBron James", "status": "Out", "comment": "rest"},
            ],
        }
        monitor = _build_monitor(injuries=injuries)
        at_risk = monitor.get_at_risk_players([])
        assert at_risk == []


# ---------------------------------------------------------------------------
# 9. suggest_swaps with no optimizer -> error message
# ---------------------------------------------------------------------------

class TestSuggestSwapsNoOptimizer:
    def test_suggest_swaps_no_optimizer(self):
        monitor = _build_monitor(optimizer=None)
        lineup = _make_lineup([_make_player("Player A")])

        result = monitor.suggest_swaps([lineup], pool=[])

        assert "error" in result
        assert result["error"] == "Lineup optimizer not available"
        assert result["swaps"] == []
        assert result["total_swaps"] == 0


# ---------------------------------------------------------------------------
# 10. Risk-level sorting: high before medium, then by projected_fp desc
# ---------------------------------------------------------------------------

class TestRiskLevelSorting:
    def test_risk_level_sorting(self):
        injuries = {
            "LAL": [
                {"player_name": "Player A", "status": "Questionable", "comment": ""},
            ],
            "BOS": [
                {"player_name": "Player B", "status": "Out", "comment": ""},
            ],
            "MIA": [
                {"player_name": "Player C", "status": "Doubtful", "comment": ""},
            ],
            "GSW": [
                {"player_name": "Player D", "status": "Questionable", "comment": ""},
            ],
        }
        lineup = _make_lineup([
            _make_player("Player A", team="LAL", projected_fp=40.0),   # medium, 40
            _make_player("Player B", team="BOS", projected_fp=30.0),   # high (Out), 30
            _make_player("Player C", team="MIA", projected_fp=25.0),   # high (Doubtful), 25
            _make_player("Player D", team="GSW", projected_fp=45.0),   # medium, 45
        ])
        monitor = _build_monitor(injuries=injuries)

        at_risk = monitor.get_at_risk_players([lineup])

        assert len(at_risk) == 4

        # First two should be high risk, sorted by projected_fp desc
        assert at_risk[0]["risk_level"] == "high"
        assert at_risk[0]["player_name"] == "Player B"
        assert at_risk[0]["projected_fp"] == 30.0

        assert at_risk[1]["risk_level"] == "high"
        assert at_risk[1]["player_name"] == "Player C"
        assert at_risk[1]["projected_fp"] == 25.0

        # Next two should be medium risk, sorted by projected_fp desc
        assert at_risk[2]["risk_level"] == "medium"
        assert at_risk[2]["player_name"] == "Player D"
        assert at_risk[2]["projected_fp"] == 45.0

        assert at_risk[3]["risk_level"] == "medium"
        assert at_risk[3]["player_name"] == "Player A"
        assert at_risk[3]["projected_fp"] == 40.0
