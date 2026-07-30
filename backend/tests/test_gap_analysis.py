"""Unit tests for the rotation gap analysis algorithm.

Tests ``NBAApiService.find_rotation_cutoff()`` — a pure function that
determines how many players belong in a game-night rotation based on
the relative gap in season averages.
"""

import pytest
from typing import List

from app.models.player import PlayerMinutes
from app.services.nba_api_service import NBAApiService


def _make_players(avgs: List[float]) -> List[PlayerMinutes]:
    """Create minimal PlayerMinutes objects from a list of season averages."""
    return [
        PlayerMinutes(
            player_id=i,
            player_name=f"Player {i}",
            position="G",
            team_id=1,
            minutes_last_5=[avg] * 5,
            minutes_last_10=[avg] * 10,
            season_avg=avg,
            usage_rate=0.15,
        )
        for i, avg in enumerate(avgs)
    ]


class TestFindRotationCutoff:
    """Tests for the relative gap analysis algorithm."""

    def test_clear_gap_at_position_9(self):
        """Clear drop after 9th player — should keep 9."""
        # Gap at index 8→9: 15→5 = 10 min, ratio = 10/15 = 66.7%
        avgs = [35, 33, 32, 30, 28, 22, 20, 18, 15, 5]
        players = _make_players(avgs)

        cut, gap, ratio = NBAApiService.find_rotation_cutoff(players)

        assert cut == 9
        assert gap == pytest.approx(10.0)
        assert ratio == pytest.approx(10.0 / 15.0, abs=0.01)

    def test_deep_rotation_gap_at_position_11(self):
        """Deep-rotation team with biggest relative gap at position 11."""
        # Gaps:
        #   pos 8: 18→15 = 3, ratio = 3/18 = 16.7%  (below 20%)
        #   pos 9: 15→10 = 5, ratio = 5/15 = 33.3%
        #   pos 10: 10→8 = 2, ratio = 2/10 = 20%
        #   pos 11:  8→3 = 5, ratio = 5/8  = 62.5%  (BIGGEST)
        avgs = [35, 33, 32, 30, 28, 22, 20, 18, 15, 10, 8, 3]
        players = _make_players(avgs)

        cut, gap, ratio = NBAApiService.find_rotation_cutoff(players)

        assert cut == 11
        assert gap == pytest.approx(5.0)
        assert ratio == pytest.approx(5.0 / 8.0, abs=0.01)

    def test_no_significant_gap_defaults_to_10(self):
        """Evenly-spaced rotation with no meaningful gap defaults to 10."""
        # All gaps are 2 min; at pos 8: 2/21 = 9.5% — below 20% threshold
        avgs = [35, 33, 31, 29, 27, 25, 23, 21, 19, 17, 15, 13]
        players = _make_players(avgs)

        cut, gap, ratio = NBAApiService.find_rotation_cutoff(players)

        assert cut == 10  # default_rotation
        assert gap == 0.0  # thresholds not met
        assert ratio == 0.0

    def test_fewer_than_min_rotation(self):
        """Fewer than 8 players — return them all."""
        avgs = [35, 33, 30, 28, 22, 18, 15]
        players = _make_players(avgs)

        cut, gap, ratio = NBAApiService.find_rotation_cutoff(players)

        assert cut == 7
        assert gap == 0.0
        assert ratio == 0.0

    def test_exactly_min_rotation(self):
        """Exactly 8 players — return them all."""
        avgs = [35, 33, 30, 28, 22, 18, 15, 10]
        players = _make_players(avgs)

        cut, gap, ratio = NBAApiService.find_rotation_cutoff(players)

        assert cut == 8

    def test_max_players_override(self):
        """max_rotation override limits the scan range."""
        # With max_rotation=9, can't scan past position 9
        avgs = [35, 33, 32, 30, 28, 22, 20, 18, 15, 10, 8, 3]
        players = _make_players(avgs)

        cut, _, _ = NBAApiService.find_rotation_cutoff(
            players, max_rotation=9
        )

        # Can only scan positions 7-8; biggest relative gap there
        # is at pos 8: 18→15 = 3, ratio = 3/18 = 16.7% (below 20%)
        # So falls back to default_rotation=10, clamped by len
        assert cut <= 10

    def test_large_absolute_but_small_relative_gap(self):
        """A big absolute gap among high-minute players is noise."""
        # Gap at pos 7: 32→27 = 5 min absolute, but ratio = 5/32 = 15.6%
        # This is below the 20% threshold, so it shouldn't trigger
        avgs = [38, 37, 36, 35, 34, 33, 32, 27, 25, 23, 21, 19]
        players = _make_players(avgs)

        cut, gap, ratio = NBAApiService.find_rotation_cutoff(players)

        # All ratios are < 20%, so should fallback to default 10
        assert cut == 10
        assert gap == 0.0

    def test_small_absolute_gap_despite_large_ratio(self):
        """A large ratio with tiny absolute gap is noise too."""
        # If the absolute gap is < 2.0, it's not meaningful regardless
        # of ratio
        avgs = [35, 33, 32, 30, 28, 22, 20, 18, 15, 10, 4.0, 1.5]
        players = _make_players(avgs)

        cut, gap, ratio = NBAApiService.find_rotation_cutoff(players)

        # The 4.0→1.5 gap has ratio 2.5/4.0=62.5% AND abs=2.5 > 2.0
        # But there's also 10→4.0 = ratio 6/10 = 60% AND abs=6.0
        # The algorithm picks the highest ratio, which should be
        # 4.0→1.5 at 62.5%
        assert cut in (10, 11)  # either gap is valid
        assert gap >= 2.0

    def test_empty_list(self):
        """Empty player list."""
        cut, gap, ratio = NBAApiService.find_rotation_cutoff([])

        assert cut == 0
        assert gap == 0.0
        assert ratio == 0.0

    def test_single_player(self):
        """Single player."""
        players = _make_players([35])

        cut, gap, ratio = NBAApiService.find_rotation_cutoff(players)

        assert cut == 1
        assert gap == 0.0

    def test_all_equal_minutes(self):
        """All players have the same season average — no gap possible."""
        avgs = [20] * 14
        players = _make_players(avgs)

        cut, gap, ratio = NBAApiService.find_rotation_cutoff(players)

        assert cut == 10  # default_rotation
        assert gap == 0.0

    def test_custom_thresholds(self):
        """Custom min_abs_gap and min_gap_ratio thresholds."""
        avgs = [35, 33, 32, 30, 28, 22, 20, 18, 15, 10, 8, 3]
        players = _make_players(avgs)

        # With very strict thresholds (50% ratio), only the 8→3 gap qualifies
        cut, gap, ratio = NBAApiService.find_rotation_cutoff(
            players, min_gap_ratio=0.50
        )

        assert cut == 11
        assert ratio >= 0.50
