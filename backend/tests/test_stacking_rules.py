"""Tests for stacking_rules.py — Game Stacking Rules Engine."""

import pytest
from types import SimpleNamespace

from app.services.stacking_rules import (
    StackingConfig,
    StackingRule,
    StackingRulesEngine,
)


def _make_lineup_players(specs):
    """Build lineup player dicts from (team, game_id, position) tuples."""
    return [
        {
            "player_id": 1000 + i,
            "player_name": f"Player_{i}",
            "team": team,
            "game_id": gid,
            "position": pos,
            "salary": 5000,
            "projected_fp": 25.0,
        }
        for i, (team, gid, pos) in enumerate(specs)
    ]


class TestValidateLineup:
    def test_disabled_config(self):
        engine = StackingRulesEngine()
        config = StackingConfig(rules=[], enabled=False)
        valid, violations = engine.validate_lineup([], [], config)
        assert valid is True
        assert violations == []

    def test_empty_rules(self):
        engine = StackingRulesEngine()
        config = StackingConfig(rules=[], enabled=True)
        valid, violations = engine.validate_lineup([], [], config)
        assert valid is True

    def test_min_stack_satisfied(self):
        engine = StackingRulesEngine()
        config = StackingConfig(rules=[
            StackingRule(rule_type="min_stack", min_players=2),
        ])
        # 3 players from same game
        lineup = _make_lineup_players([
            ("LAL", "G1", "PG"), ("LAL", "G1", "SG"), ("BOS", "G1", "SF"),
            ("MIA", "G2", "PF"), ("NYK", "G3", "C"),
        ])
        valid, violations = engine.validate_lineup(lineup, [], config)
        assert valid is True

    def test_min_stack_violated(self):
        engine = StackingRulesEngine()
        config = StackingConfig(rules=[
            StackingRule(rule_type="min_stack", min_players=3),
        ])
        # Each game has only 1 player
        lineup = _make_lineup_players([
            ("LAL", "G1", "PG"), ("BOS", "G2", "SG"), ("MIA", "G3", "SF"),
        ])
        valid, violations = engine.validate_lineup(lineup, [], config)
        assert valid is False
        assert len(violations) == 1
        assert "No game has 3+" in violations[0]

    def test_max_per_team_satisfied(self):
        engine = StackingRulesEngine()
        config = StackingConfig(rules=[
            StackingRule(rule_type="max_per_team", max_players=3),
        ])
        lineup = _make_lineup_players([
            ("LAL", "G1", "PG"), ("LAL", "G1", "SG"), ("LAL", "G1", "SF"),
            ("BOS", "G2", "PF"), ("BOS", "G2", "C"),
        ])
        valid, violations = engine.validate_lineup(lineup, [], config)
        assert valid is True

    def test_max_per_team_violated(self):
        engine = StackingRulesEngine()
        config = StackingConfig(rules=[
            StackingRule(rule_type="max_per_team", max_players=2),
        ])
        lineup = _make_lineup_players([
            ("LAL", "G1", "PG"), ("LAL", "G1", "SG"), ("LAL", "G1", "SF"),
        ])
        valid, violations = engine.validate_lineup(lineup, [], config)
        assert valid is False
        assert "LAL" in violations[0]

    def test_bring_back_satisfied(self):
        engine = StackingRulesEngine()
        config = StackingConfig(rules=[
            StackingRule(
                rule_type="bring_back",
                game_id="G1",
                team_abbr="LAL",
            ),
        ])
        lineup = _make_lineup_players([
            ("LAL", "G1", "PG"), ("LAL", "G1", "SG"), ("BOS", "G1", "SF"),
        ])
        valid, violations = engine.validate_lineup(lineup, [], config)
        assert valid is True

    def test_bring_back_violated(self):
        engine = StackingRulesEngine()
        config = StackingConfig(rules=[
            StackingRule(
                rule_type="bring_back",
                game_id="G1",
                team_abbr="LAL",
            ),
        ])
        # All from same team, no bring-back
        lineup = _make_lineup_players([
            ("LAL", "G1", "PG"), ("LAL", "G1", "SG"),
        ])
        valid, violations = engine.validate_lineup(lineup, [], config)
        assert valid is False
        assert "no bring-back" in violations[0]


class TestFilterCandidates:
    def test_disabled_returns_all(self):
        engine = StackingRulesEngine()
        config = StackingConfig(enabled=False)
        candidates = [SimpleNamespace(player_id=1, team_abbreviation="LAL")]
        result = engine.filter_candidates("PG", candidates, [], [], config)
        assert len(result) == 1

    def test_max_per_team_filters(self):
        engine = StackingRulesEngine()
        config = StackingConfig(rules=[
            StackingRule(rule_type="max_per_team", max_players=2),
        ])
        current = [{"team": "LAL"}, {"team": "LAL"}]  # 2 LAL already
        candidates = [
            SimpleNamespace(player_id=1, team_abbreviation="LAL"),  # blocked
            SimpleNamespace(player_id=2, team_abbreviation="BOS"),  # allowed
        ]
        result = engine.filter_candidates("PG", candidates, current, [], config)
        assert len(result) == 1
        assert result[0].player_id == 2

    def test_no_empty_result(self):
        """If filtering removes everyone, return original candidates."""
        engine = StackingRulesEngine()
        config = StackingConfig(rules=[
            StackingRule(rule_type="max_per_team", max_players=1),
        ])
        current = [{"team": "LAL"}]
        candidates = [
            SimpleNamespace(player_id=1, team_abbreviation="LAL"),
        ]
        result = engine.filter_candidates("PG", candidates, current, [], config)
        # Should return original since filtering would make it empty
        assert len(result) == 1


class TestScoreStackingBonus:
    def test_no_rules(self):
        engine = StackingRulesEngine()
        config = StackingConfig(rules=[], enabled=True)
        bonus = engine.score_stacking_bonus([], [], config)
        assert bonus == 0.0

    def test_min_stack_bonus(self):
        engine = StackingRulesEngine()
        config = StackingConfig(rules=[
            StackingRule(rule_type="min_stack", min_players=2, weight=1.0),
        ])
        lineup = _make_lineup_players([
            ("LAL", "G1", "PG"), ("BOS", "G1", "SG"), ("MIA", "G1", "SF"),
        ])
        bonus = engine.score_stacking_bonus(lineup, [], config)
        # 3 players in G1, meets min_players=2, bonus = 1.0 * (3-1) * 0.5 = 1.0
        assert bonus == 1.0

    def test_bring_back_bonus(self):
        engine = StackingRulesEngine()
        config = StackingConfig(rules=[
            StackingRule(
                rule_type="bring_back",
                game_id="G1",
                team_abbr="LAL",
                weight=2.0,
            ),
        ])
        lineup = _make_lineup_players([
            ("LAL", "G1", "PG"), ("LAL", "G1", "SG"), ("BOS", "G1", "SF"),
        ])
        bonus = engine.score_stacking_bonus(lineup, [], config)
        # 2 primary + 1 opp = bring-back bonus: 2.0 * 1.5 = 3.0
        assert bonus == 3.0


class TestDefaultConfig:
    def test_basic_config(self):
        config = StackingRulesEngine.create_default_config(
            min_stack_size=3,
            max_per_team=4,
        )
        assert config.enabled is True
        assert len(config.rules) == 2
        assert config.rules[0].rule_type == "min_stack"
        assert config.rules[0].min_players == 3
        assert config.rules[1].rule_type == "max_per_team"
        assert config.rules[1].max_players == 4

    def test_with_bring_back(self):
        config = StackingRulesEngine.create_default_config(
            min_stack_size=2,
            max_per_team=3,
            enable_bring_back=True,
            target_game_id="G1",
            primary_team="LAL",
        )
        assert len(config.rules) == 3
        assert config.rules[2].rule_type == "bring_back"
        assert config.rules[2].game_id == "G1"
        assert config.rules[2].team_abbr == "LAL"


# ---------------------------------------------------------------------------
# Correlation Stacking Tests
# ---------------------------------------------------------------------------

class TestCorrelationStacking:
    """Tests for correlation-weighted stacking and lineup scoring."""

    def test_correlation_weighted_selection(self):
        """When correlation_weights are provided, correlated players should
        be selected more often than uncorrelated ones."""
        import random
        from app.models.lineup import PlayerPoolEntry
        from app.services.lineup_optimizer_service import (
            LineupOptimizerService,
            DK_SLOT_ELIGIBILITY,
        )

        # Build a pool of 6 players in the same game, split across two teams
        pool = []
        for i, (name, team, fp) in enumerate([
            ("PG1", "LAL", 30.0),
            ("SG1", "LAL", 28.0),
            ("SF1", "LAL", 26.0),
            ("PG2", "BOS", 22.0),
            ("SG2", "BOS", 20.0),
            ("SF2", "BOS", 18.0),
        ]):
            pos = name[:2]
            eligible = [
                slot for slot, positions in DK_SLOT_ELIGIBILITY.items()
                if pos in positions
            ]
            pool.append(PlayerPoolEntry(
                player_id=i + 1,
                player_name=name,
                position=pos,
                team_abbreviation=team,
                salary=5000,
                projected_fp=fp,
                floor_fp=fp * 0.8,
                ceiling_fp=fp * 1.2,
                projected_minutes=25.0,
                eligible_slots=eligible,
                game_id="G1",
            ))

        target_game = {
            "game_id": "G1",
            "team_a": "LAL",
            "team_b": "BOS",
            "game_total": 230.0,
        }

        # Correlation weights: PG1-SG1 are highly correlated
        correlation_weights = {
            (1, 2): 0.85,  # PG1 <-> SG1 very high
            (1, 3): 0.10,  # PG1 <-> SF1 low
            (2, 3): 0.10,  # SG1 <-> SF1 low
        }

        # Run selection many times; count how often PG1 + SG1 appear together
        corr_together = 0
        no_corr_together = 0
        trials = 300

        for seed in range(trials):
            rng = random.Random(seed)
            ids_corr = LineupOptimizerService._select_stack_players(
                pool, target_game, rng, stack_size=3, bring_back=True,
                correlation_weights=correlation_weights,
            )
            if 1 in ids_corr and 2 in ids_corr:
                corr_together += 1

        for seed in range(trials):
            rng = random.Random(seed)
            ids_no = LineupOptimizerService._select_stack_players(
                pool, target_game, rng, stack_size=3, bring_back=True,
                correlation_weights=None,
            )
            if 1 in ids_no and 2 in ids_no:
                no_corr_together += 1

        # With correlation weights, PG1+SG1 should appear together more often
        assert corr_together > no_corr_together, (
            f"Correlated pair co-selected {corr_together} vs "
            f"{no_corr_together} times without weights"
        )

    def test_correlation_lineup_scoring_bonus(self):
        """Lineup score should increase when _cached_correlations has
        high correlation values for same-game players."""
        from app.models.lineup import (
            PlayerPoolEntry,
            OptimizedLineup,
            LineupPlayer,
        )
        from app.services.lineup_optimizer_service import (
            LineupOptimizerService,
            DK_SALARY_CAP,
            DK_SLOT_ELIGIBILITY,
            DK_ROSTER_SLOTS,
        )

        # Create a pool of 8 players, 3 from same game
        positions = ["PG", "SG", "SF", "PF", "C", "PG", "SF", "SG"]
        teams = ["LAL", "LAL", "BOS", "MIL", "MIN", "NYK", "DEN", "CHI"]
        game_ids = ["G1", "G1", "G1", "G2", "G3", "G4", "G5", "G6"]
        slots = ["PG", "SG", "SF", "PF", "C", "G", "F", "UTIL"]

        pool = []
        lineup_players = []
        for i in range(8):
            fp = 25.0 + i * 2.0
            eligible = [
                slot for slot, pos_set in DK_SLOT_ELIGIBILITY.items()
                if positions[i] in pos_set
            ]
            pool.append(PlayerPoolEntry(
                player_id=i + 1,
                player_name=f"Player_{i}",
                position=positions[i],
                team_abbreviation=teams[i],
                salary=5500,
                projected_fp=fp,
                floor_fp=fp * 0.8,
                ceiling_fp=fp * 1.2,
                projected_minutes=28.0,
                eligible_slots=eligible,
                game_id=game_ids[i],
            ))
            lineup_players.append(LineupPlayer(
                player_id=i + 1,
                player_name=f"Player_{i}",
                position=positions[i],
                roster_slot=slots[i],
                team_abbreviation=teams[i],
                salary=5500,
                projected_fp=fp,
                floor_fp=fp * 0.8,
                ceiling_fp=fp * 1.2,
                projected_minutes=28.0,
            ))

        lineup = OptimizedLineup(
            platform="dk",
            players=lineup_players,
            total_salary=44000,
            salary_remaining=6000,
            total_projected_fp=sum(25.0 + i * 2.0 for i in range(8)),
            total_floor_fp=sum((25.0 + i * 2.0) * 0.8 for i in range(8)),
            total_ceiling_fp=sum((25.0 + i * 2.0) * 1.2 for i in range(8)),
            salary_cap=DK_SALARY_CAP,
            roster_slots=DK_ROSTER_SLOTS,
        )

        # Optimizer without correlations
        opt_no_corr = LineupOptimizerService(
            dfs_service=None, dk_draftables_service=None,
            nba_service=None, injury_service=None, rotation_engine=None,
        )
        opt_no_corr._cached_correlations = None

        score_no_corr = opt_no_corr._score_lineup(
            lineup, pool, "ceiling", "gpp", DK_SALARY_CAP,
        )

        # Optimizer with high correlations among G1 players (ids 1, 2, 3)
        opt_corr = LineupOptimizerService(
            dfs_service=None, dk_draftables_service=None,
            nba_service=None, injury_service=None, rotation_engine=None,
        )
        opt_corr._cached_correlations = {
            (1, 2): 0.65,
            (1, 3): 0.55,
            (2, 3): 0.50,
        }

        score_corr = opt_corr._score_lineup(
            lineup, pool, "ceiling", "gpp", DK_SALARY_CAP,
        )

        # With high correlations, the score should be boosted
        assert score_corr > score_no_corr, (
            f"Correlated score ({score_corr:.2f}) should exceed "
            f"uncorrelated ({score_no_corr:.2f})"
        )
