"""Game Stacking Rules Engine.

Enforces and scores game stacking constraints for DFS lineup construction.

Stacking rules specify:
- Minimum players from the same game
- Maximum players from a single team
- Bring-back (opposite-team correlations, e.g., QB + opposing WR in NFL)
- Position stacking preferences (e.g., guard-center combos)

For NBA DFS, common stacking patterns:
- 2-3 players from a high-total game
- Bring-back: a player from the opposing team in the same game
- Avoid too many players from one team (cap at 3-4)
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class StackingRule:
    """A single stacking constraint."""

    rule_type: str  # "min_stack", "max_per_team", "bring_back", "game_stack"
    game_id: Optional[str] = None
    team_abbr: Optional[str] = None
    min_players: int = 0
    max_players: int = 99
    positions: Optional[List[str]] = None
    bring_back_positions: Optional[List[str]] = None
    weight: float = 1.0  # scoring weight


@dataclass
class StackingConfig:
    """Collection of stacking rules."""

    rules: List[StackingRule] = field(default_factory=list)
    enabled: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "rules": [
                {
                    "rule_type": r.rule_type,
                    "game_id": r.game_id,
                    "team_abbr": r.team_abbr,
                    "min_players": r.min_players,
                    "max_players": r.max_players,
                    "positions": r.positions,
                    "bring_back_positions": r.bring_back_positions,
                    "weight": r.weight,
                }
                for r in self.rules
            ],
        }


class StackingRulesEngine:
    """Validates and scores lineups based on stacking rules."""

    def validate_lineup(
        self,
        lineup: List[Dict[str, Any]],
        pool: List,
        config: StackingConfig,
    ) -> Tuple[bool, List[str]]:
        """Check if a lineup satisfies all stacking rules.

        Parameters
        ----------
        lineup : list of dict
            Each dict has: player_id, player_name, team, game_id, position.
        pool : list
            Full player pool (for game/team lookups if needed).
        config : StackingConfig
            Active stacking rules.

        Returns
        -------
        (is_valid, list_of_violation_messages)
        """
        if not config.enabled or not config.rules:
            return True, []

        violations = []

        # Build lookup structures
        by_game: Dict[str, List[Dict]] = {}
        by_team: Dict[str, List[Dict]] = {}

        for p in lineup:
            gid = p.get("game_id", "")
            team = p.get("team", p.get("team_abbreviation", ""))
            if gid:
                by_game.setdefault(gid, []).append(p)
            if team:
                by_team.setdefault(team, []).append(p)

        for rule in config.rules:
            if rule.rule_type == "min_stack":
                # At least min_players from the same game
                if rule.game_id:
                    count = len(by_game.get(rule.game_id, []))
                    if count < rule.min_players:
                        violations.append(
                            f"Game {rule.game_id}: need {rule.min_players} "
                            f"players, have {count}"
                        )
                else:
                    # General: at least one game with min_players
                    max_in_any = max(
                        (len(v) for v in by_game.values()), default=0
                    )
                    if max_in_any < rule.min_players:
                        violations.append(
                            f"No game has {rule.min_players}+ players "
                            f"(max stack size: {max_in_any})"
                        )

            elif rule.rule_type == "max_per_team":
                for team, players in by_team.items():
                    if rule.team_abbr and team != rule.team_abbr:
                        continue
                    if len(players) > rule.max_players:
                        violations.append(
                            f"Team {team}: max {rule.max_players} players "
                            f"allowed, have {len(players)}"
                        )

            elif rule.rule_type == "bring_back":
                # Ensure at least 1 player from the opposing team in stacked game
                if rule.game_id and rule.team_abbr:
                    game_players = by_game.get(rule.game_id, [])
                    has_bring_back = any(
                        p.get("team", p.get("team_abbreviation", ""))
                        != rule.team_abbr
                        for p in game_players
                    )
                    if not has_bring_back and len(game_players) >= 2:
                        violations.append(
                            f"Game {rule.game_id}: no bring-back player "
                            f"from opposing team (primary: {rule.team_abbr})"
                        )

            elif rule.rule_type == "game_stack":
                # Enforce game stacking: min/max from a specific game
                if rule.game_id:
                    count = len(by_game.get(rule.game_id, []))
                    if count < rule.min_players:
                        violations.append(
                            f"Game {rule.game_id}: need {rule.min_players}+ "
                            f"players, have {count}"
                        )
                    if count > rule.max_players:
                        violations.append(
                            f"Game {rule.game_id}: max {rule.max_players} "
                            f"players allowed, have {count}"
                        )

        return len(violations) == 0, violations

    def filter_candidates(
        self,
        slot: str,
        candidates: List,
        current_lineup: List[Dict[str, Any]],
        pool: List,
        config: StackingConfig,
    ) -> List:
        """Filter candidate players for a slot based on stacking rules.

        Removes candidates that would violate max constraints, and optionally
        boosts candidates that would help satisfy min constraints.

        Parameters
        ----------
        slot : str
            Roster slot being filled.
        candidates : list
            Available players for this slot.
        current_lineup : list of dict
            Players already assigned.
        pool : list
            Full pool for context.
        config : StackingConfig

        Returns
        -------
        Filtered candidate list (never empty if input wasn't empty).
        """
        if not config.enabled or not config.rules or not candidates:
            return candidates

        # Build team counts from current lineup
        team_counts: Dict[str, int] = {}
        for p in current_lineup:
            team = p.get("team", p.get("team_abbreviation", ""))
            if team:
                team_counts[team] = team_counts.get(team, 0) + 1

        # Find max_per_team limits
        team_limits: Dict[str, int] = {}
        global_team_limit = 99
        for rule in config.rules:
            if rule.rule_type == "max_per_team":
                if rule.team_abbr:
                    team_limits[rule.team_abbr] = rule.max_players
                else:
                    global_team_limit = rule.max_players

        # Filter out candidates that would exceed team limits
        filtered = []
        for c in candidates:
            c_team = getattr(c, "team_abbreviation", "") or getattr(c, "team", "")
            limit = team_limits.get(c_team, global_team_limit)
            current = team_counts.get(c_team, 0)
            if current < limit:
                filtered.append(c)

        # If filtering removed everyone, return original candidates
        # (prefer a suboptimal lineup over an empty one)
        return filtered if filtered else candidates

    def score_stacking_bonus(
        self,
        lineup: List[Dict[str, Any]],
        pool: List,
        config: StackingConfig,
    ) -> float:
        """Compute a stacking bonus score for a lineup.

        Rewards lineups that leverage game stacking for correlated outcomes.
        Higher = better stacking.

        Returns
        -------
        float bonus points (can be added to lineup's total score).
        """
        if not config.enabled or not config.rules:
            return 0.0

        bonus = 0.0

        # Count by game and team
        by_game: Dict[str, List[Dict]] = {}
        by_team: Dict[str, List[Dict]] = {}

        for p in lineup:
            gid = p.get("game_id", "")
            team = p.get("team", p.get("team_abbreviation", ""))
            if gid:
                by_game.setdefault(gid, []).append(p)
            if team:
                by_team.setdefault(team, []).append(p)

        for rule in config.rules:
            if rule.rule_type == "min_stack" or rule.rule_type == "game_stack":
                # Reward meeting stack targets
                if rule.game_id:
                    count = len(by_game.get(rule.game_id, []))
                else:
                    count = max((len(v) for v in by_game.values()), default=0)

                if count >= rule.min_players:
                    # Bonus = weight * (count - 1) — more stacking = more bonus
                    bonus += rule.weight * (count - 1) * 0.5

            elif rule.rule_type == "bring_back":
                if rule.game_id and rule.team_abbr:
                    game_players = by_game.get(rule.game_id, [])
                    primary_count = sum(
                        1 for p in game_players
                        if p.get("team", p.get("team_abbreviation", "")) == rule.team_abbr
                    )
                    opp_count = len(game_players) - primary_count
                    if primary_count >= 2 and opp_count >= 1:
                        bonus += rule.weight * 1.5  # Strong bring-back bonus

        return round(bonus, 2)

    @staticmethod
    def create_default_config(
        min_stack_size: int = 2,
        max_per_team: int = 4,
        enable_bring_back: bool = False,
        target_game_id: Optional[str] = None,
        primary_team: Optional[str] = None,
        sport: str = "nba",
    ) -> StackingConfig:
        """Build a common stacking configuration."""
        # Sport-aware default: CBB has fewer high-usage players per team
        effective_max = max_per_team if max_per_team != 4 else (3 if sport == "cbb" else 4)
        rules = [
            StackingRule(
                rule_type="min_stack",
                game_id=target_game_id,
                min_players=min_stack_size,
                weight=1.0,
            ),
            StackingRule(
                rule_type="max_per_team",
                max_players=effective_max,
                weight=1.0,
            ),
        ]

        if enable_bring_back and target_game_id and primary_team:
            rules.append(
                StackingRule(
                    rule_type="bring_back",
                    game_id=target_game_id,
                    team_abbr=primary_team,
                    weight=1.5,
                )
            )

        return StackingConfig(rules=rules, enabled=True)
