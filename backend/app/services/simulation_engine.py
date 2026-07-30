"""Monte Carlo game simulation engine.

Runs vectorized NumPy simulations of NBA games using the projected
rotations, per-minute stat rates, and game-level projections already
produced by the RotationEngine, DFSService, and GameService.

The simulation adds stochastic variance to:
    - Game pace (total possessions)
    - Player minutes (per-player allocation)
    - Per-minute stat production (noise multiplier per stat category)

and produces probability distributions for team scores, individual
player stat lines, DFS fantasy points, win probability, and
over/under analysis.

Usage:
    sim = SimulationEngine(SimulationConfig(seed=42))
    result = sim.simulate_game(
        game_info, home_rotation, home_players,
        away_rotation, away_players,
    )
"""

import logging
from typing import Dict, List, Optional, Tuple

import numpy as np

from app.config.constants import (
    CROSS_TEAM_CORRELATION_ENABLED,
    GAME_SCRIPT_SCENARIOS,
    GAME_SCRIPT_STARTER_MINUTES,
    DVP_VARIANCE_SENSITIVITY,
    OT_PROBABILITY_CLOSE_GAME,
    OT_EXTRA_TEAM_MINUTES,
    OT_MAX_PLAYER_MINUTES,
    OT_TOTAL_BOOST,
)
from app.models.game import GameInfo
from app.models.player import PlayerMinutes, PlayerProjection
from app.models.rotation import TeamRotation
from app.models.simulation import (
    GameSimResult,
    PlayerSimInput,
    PlayerSimResult,
    SimulationConfig,
    TeamSimResult,
)

logger = logging.getLogger(__name__)

# League averages for calibration (centralized in constants.py)
from app.config.constants import LEAGUE_AVG_PACE  # noqa: E402
REGULATION_MINUTES = 240.0  # 5 players × 48 min
MAX_PLAYER_MINUTES = 48.0

# Per-stat noise standard deviations.
# Higher σ for rare events (STL, BLK), lower for volume stats (PTS, REB).
STAT_NOISE_SIGMA: Dict[str, float] = {
    "pts": 0.15,
    "reb": 0.15,
    "ast": 0.25,
    "stl": 0.40,
    "blk": 0.40,
    "tov": 0.30,
    "fg3m": 0.30,
}

# Stat correlation matrix — rows/cols ordered as STAT_ORDER.
# High-scoring games produce more assists; rebounds and blocks co-vary;
# turnovers correlate with high-usage (points + assists); 3PM with points.
STAT_ORDER = ["pts", "reb", "ast", "stl", "blk", "tov", "fg3m"]
STAT_CORRELATION = np.array([
    # pts   reb   ast   stl   blk   tov   fg3m
    [1.00, 0.15, 0.40, 0.05, 0.02, 0.25, 0.55],  # pts
    [0.15, 1.00, 0.10, 0.05, 0.20, 0.05, 0.00],  # reb
    [0.40, 0.10, 1.00, 0.10, 0.00, 0.20, 0.15],  # ast
    [0.05, 0.05, 0.10, 1.00, 0.15, 0.10, 0.00],  # stl
    [0.02, 0.20, 0.00, 0.15, 1.00, 0.00, 0.00],  # blk
    [0.25, 0.05, 0.20, 0.10, 0.00, 1.00, 0.05],  # tov
    [0.55, 0.00, 0.15, 0.00, 0.00, 0.05, 1.00],  # fg3m
])

# ---------------------------------------------------------------------------
# Position-group correlation matrices
# ---------------------------------------------------------------------------
# Guards (PG, SG): high pts↔ast and pts↔fg3m correlations; minimal reb/blk.
GUARD_CORRELATION = np.array([
    # pts   reb   ast   stl   blk   tov   fg3m
    [1.00, 0.08, 0.55, 0.08, 0.00, 0.30, 0.60],  # pts
    [0.08, 1.00, 0.05, 0.05, 0.05, 0.05, 0.00],  # reb
    [0.55, 0.05, 1.00, 0.12, 0.00, 0.30, 0.10],  # ast
    [0.08, 0.05, 0.12, 1.00, 0.05, 0.08, 0.00],  # stl
    [0.00, 0.05, 0.00, 0.05, 1.00, 0.00, 0.00],  # blk
    [0.30, 0.05, 0.30, 0.08, 0.00, 1.00, 0.05],  # tov
    [0.60, 0.00, 0.10, 0.00, 0.00, 0.05, 1.00],  # fg3m
])

# Bigs (PF, C): high reb↔blk and pts↔reb correlations; lower ast involvement.
BIG_CORRELATION = np.array([
    # pts   reb   ast   stl   blk   tov   fg3m
    [1.00, 0.25, 0.15, 0.05, 0.10, 0.20, 0.35],  # pts
    [0.25, 1.00, 0.08, 0.05, 0.25, 0.05, 0.00],  # reb
    [0.15, 0.08, 1.00, 0.08, 0.00, 0.15, 0.08],  # ast
    [0.05, 0.05, 0.08, 1.00, 0.20, 0.08, 0.00],  # stl
    [0.10, 0.25, 0.00, 0.20, 1.00, 0.00, 0.00],  # blk
    [0.20, 0.05, 0.15, 0.08, 0.00, 1.00, 0.03],  # tov
    [0.35, 0.00, 0.08, 0.00, 0.00, 0.03, 1.00],  # fg3m
])

# Wings (SF): balanced between guard and big stat patterns.
WING_CORRELATION = np.array([
    # pts   reb   ast   stl   blk   tov   fg3m
    [1.00, 0.15, 0.30, 0.08, 0.05, 0.25, 0.50],  # pts
    [0.15, 1.00, 0.08, 0.08, 0.15, 0.05, 0.00],  # reb
    [0.30, 0.08, 1.00, 0.10, 0.00, 0.22, 0.12],  # ast
    [0.08, 0.08, 0.10, 1.00, 0.10, 0.08, 0.00],  # stl
    [0.05, 0.15, 0.00, 0.10, 1.00, 0.00, 0.00],  # blk
    [0.25, 0.05, 0.22, 0.08, 0.00, 1.00, 0.05],  # tov
    [0.50, 0.00, 0.12, 0.00, 0.00, 0.05, 1.00],  # fg3m
])

# Position → correlation matrix lookup.
# Integer labels used for efficient NumPy grouping in _simulate_team().
_POSITION_CORR_LABEL: Dict[str, int] = {
    "PG": 0, "SG": 0,  # guard
    "SF": 1,             # wing
    "PF": 2, "C": 2,    # big
}
_CORR_BY_LABEL = {
    0: GUARD_CORRELATION,
    1: WING_CORRELATION,
    2: BIG_CORRELATION,
    3: STAT_CORRELATION,  # fallback / generic
}

# Noise multiplier clamp range — prevents negative stats or absurd spikes
NOISE_MIN = 0.3
NOISE_MAX = 2.5

# Stat-specific pace sensitivity.
# How strongly each stat category scales with game pace.
#   1.0 = fully proportional to pace  |  0.0 = independent of pace
# Shared semantics with dfs_service.PACE_SENSITIVITY.
PACE_SENSITIVITY: Dict[str, float] = {
    "pts": 1.0,    # Points scale directly with possessions
    "ast": 1.0,    # Assists scale directly with possessions
    "fg3m": 0.9,   # 3PM closely tracks pace
    "tov": 0.8,    # Turnovers are mostly pace-driven
    "reb": 0.6,    # Rebounds are moderately pace-sensitive
    "stl": 0.3,    # Steals are mostly matchup-driven
    "blk": 0.2,    # Blocks are mostly matchup-driven
}

# Percentile keys for output distributions
PERCENTILE_KEYS = [10, 25, 50, 75, 90]


class SimulationEngine:
    """Monte Carlo game simulator using vectorized NumPy operations.

    All N simulations run simultaneously as array operations — no
    Python loops over individual simulations or possessions.
    """

    def __init__(self, config: SimulationConfig = None):
        self.config = config or SimulationConfig()
        self._rng = np.random.default_rng(self.config.seed)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def simulate_game(
        self,
        game_info: GameInfo,
        home_rotation: TeamRotation,
        home_players: List[PlayerMinutes],
        away_rotation: TeamRotation,
        away_players: List[PlayerMinutes],
        over_under_line: Optional[float] = None,
        player_noise_overrides: Optional[Dict[int, Dict[str, float]]] = None,
        home_matchup_factors: Optional[Dict[str, float]] = None,
        away_matchup_factors: Optional[Dict[str, float]] = None,
        sport: str = "nba",
    ) -> GameSimResult:
        """Run Monte Carlo simulation for a full game.

        Parameters
        ----------
        game_info : GameInfo
            Projected pace, scores, spread from GameService.
        home_rotation / away_rotation : TeamRotation
            Projected rotations from RotationEngine (with DFS attached).
        home_players / away_players : List[PlayerMinutes]
            Raw player data with per-minute stat rates.
        over_under_line : float, optional
            Vegas total line for over/under analysis.

        Returns
        -------
        GameSimResult
            Full probability distributions for both teams and all players.
        """
        N = self.config.num_simulations

        # Step 1: Sample game pace for all N simulations
        sim_pace = self._sample_pace(game_info.projected_pace, N)

        # Step 2: Prepare player inputs
        home_inputs = self.prepare_player_inputs(home_rotation, home_players)
        away_inputs = self.prepare_player_inputs(away_rotation, away_players)

        if not home_inputs or not away_inputs:
            logger.warning("Empty rotation(s), returning degenerate result")
            return self._degenerate_result(game_info, N)

        # Step 3: Simulate each team
        # Pass spread for game-script scenario modeling.  Use vegas_spread
        # when available, falling back to the model-projected spread.
        _spread = (
            game_info.vegas_spread
            if getattr(game_info, "vegas_spread", None) is not None
            else game_info.projected_spread
        )
        _over_under = getattr(game_info, "over_under", None)
        home_scores, home_sim_data = self._simulate_team(
            home_inputs, sim_pace, game_info.projected_home_score,
            player_noise_overrides=player_noise_overrides,
            matchup_factors=home_matchup_factors,
            spread=_spread,
            is_home=True,
            sport=sport,
            over_under=_over_under,
        )
        away_scores, away_sim_data = self._simulate_team(
            away_inputs, sim_pace, game_info.projected_away_score,
            player_noise_overrides=player_noise_overrides,
            matchup_factors=away_matchup_factors,
            spread=_spread,
            is_home=False,
            sport=sport,
            over_under=_over_under,
        )

        # Step 3.5: Apply cross-team correlation effects (NBA only)
        if sport == "nba" and CROSS_TEAM_CORRELATION_ENABLED:
            self._apply_cross_team_effects(home_sim_data, away_sim_data, N)
            home_scores = home_sim_data["stats"]["pts"].sum(axis=0)
            away_scores = away_sim_data["stats"]["pts"].sum(axis=0)

        # Step 4: Compute game-level aggregates
        home_wins = int(np.sum(home_scores > away_scores))
        away_wins = int(np.sum(away_scores > home_scores))

        total_scores = home_scores + away_scores
        spread = home_scores - away_scores  # positive = home winning

        # Over/under analysis
        over_pct = None
        under_pct = None
        if over_under_line is not None:
            over_pct = round(float(np.mean(total_scores > over_under_line)), 4)
            under_pct = round(float(np.mean(total_scores < over_under_line)), 4)

        # Step 5: Build results
        home_result = self._build_team_result(
            game_info.home_team.team_id,
            game_info.home_team.team_name,
            home_inputs,
            home_scores,
            home_sim_data,
        )
        away_result = self._build_team_result(
            game_info.away_team.team_id,
            game_info.away_team.team_name,
            away_inputs,
            away_scores,
            away_sim_data,
        )

        return GameSimResult(
            game_id=game_info.game_id,
            num_simulations=N,
            home_team=home_result,
            away_team=away_result,
            home_win_pct=round(home_wins / N, 4),
            away_win_pct=round(away_wins / N, 4),
            mean_total=round(float(total_scores.mean()), 1),
            std_total=round(float(total_scores.std()), 1),
            over_under_line=over_under_line,
            over_pct=over_pct,
            under_pct=under_pct,
            projected_spread=round(float(spread.mean()), 1),
            spread_std=round(float(spread.std()), 1),
        )

    def simulate_game_raw(
        self,
        game_info: GameInfo,
        home_rotation: TeamRotation,
        home_players: List[PlayerMinutes],
        away_rotation: TeamRotation,
        away_players: List[PlayerMinutes],
        over_under_line: Optional[float] = None,
        player_noise_overrides: Optional[Dict[int, Dict[str, float]]] = None,
        home_matchup_factors: Optional[Dict[str, float]] = None,
        away_matchup_factors: Optional[Dict[str, float]] = None,
        sport: str = "nba",
    ) -> Tuple[GameSimResult, Dict[int, Dict[str, "np.ndarray"]]]:
        """Run Monte Carlo simulation and return raw per-player iteration vectors.

        Identical to :meth:`simulate_game` but additionally returns the
        raw per-player fantasy-point arrays **before** percentile
        aggregation.  This is used by the Sim-Optimal lineup generator,
        which needs the exact FP outcome for every player in every
        simulation iteration.

        Returns
        -------
        result : GameSimResult
            Standard aggregated result (unchanged from simulate_game).
        raw_fps : dict
            ``{player_id: {"dk": ndarray(N,), "fd": ndarray(N,)}}``
            Maps each player to their DK and FD fantasy-point arrays
            across all N simulation iterations.
        """
        N = self.config.num_simulations

        # Step 1: Sample game pace
        sim_pace = self._sample_pace(game_info.projected_pace, N)

        # Step 2: Prepare player inputs
        home_inputs = self.prepare_player_inputs(home_rotation, home_players)
        away_inputs = self.prepare_player_inputs(away_rotation, away_players)

        if not home_inputs or not away_inputs:
            logger.warning("Empty rotation(s), returning degenerate result")
            return self._degenerate_result(game_info, N), {}

        # Step 3: Simulate each team
        _spread = (
            game_info.vegas_spread
            if getattr(game_info, "vegas_spread", None) is not None
            else game_info.projected_spread
        )
        _over_under = getattr(game_info, "over_under", None)
        home_scores, home_sim_data = self._simulate_team(
            home_inputs, sim_pace, game_info.projected_home_score,
            player_noise_overrides=player_noise_overrides,
            matchup_factors=home_matchup_factors,
            spread=_spread, is_home=True, sport=sport,
            over_under=_over_under,
        )
        away_scores, away_sim_data = self._simulate_team(
            away_inputs, sim_pace, game_info.projected_away_score,
            player_noise_overrides=player_noise_overrides,
            matchup_factors=away_matchup_factors,
            spread=_spread, is_home=False, sport=sport,
            over_under=_over_under,
        )

        # Step 3.5: Apply cross-team correlation effects (NBA only)
        if sport == "nba" and CROSS_TEAM_CORRELATION_ENABLED:
            self._apply_cross_team_effects(home_sim_data, away_sim_data, N)
            home_scores = home_sim_data["stats"]["pts"].sum(axis=0)
            away_scores = away_sim_data["stats"]["pts"].sum(axis=0)

        # ── Extract raw FP vectors before aggregation ──────────────
        raw_fps: Dict[int, Dict[str, np.ndarray]] = {}
        for team_inputs, team_sim_data in [
            (home_inputs, home_sim_data),
            (away_inputs, away_sim_data),
        ]:
            for idx, player_input in enumerate(team_inputs):
                raw_fps[player_input.player_id] = {
                    "dk": team_sim_data["dk_pts"][idx].copy(),
                    "fd": team_sim_data["fd_pts"][idx].copy(),
                }

        # Step 4: Game-level aggregates
        home_wins = int(np.sum(home_scores > away_scores))
        away_wins = int(np.sum(away_scores > home_scores))
        total_scores = home_scores + away_scores
        spread = home_scores - away_scores

        over_pct = None
        under_pct = None
        if over_under_line is not None:
            over_pct = round(float(np.mean(total_scores > over_under_line)), 4)
            under_pct = round(float(np.mean(total_scores < over_under_line)), 4)

        # Step 5: Build aggregated result
        home_result = self._build_team_result(
            game_info.home_team.team_id, game_info.home_team.team_name,
            home_inputs, home_scores, home_sim_data,
        )
        away_result = self._build_team_result(
            game_info.away_team.team_id, game_info.away_team.team_name,
            away_inputs, away_scores, away_sim_data,
        )

        result = GameSimResult(
            game_id=game_info.game_id,
            num_simulations=N,
            home_team=home_result,
            away_team=away_result,
            home_win_pct=round(home_wins / N, 4),
            away_win_pct=round(away_wins / N, 4),
            mean_total=round(float(total_scores.mean()), 1),
            std_total=round(float(total_scores.std()), 1),
            over_under_line=over_under_line,
            over_pct=over_pct,
            under_pct=under_pct,
            projected_spread=round(float(spread.mean()), 1),
            spread_std=round(float(spread.std()), 1),
        )
        return result, raw_fps

    # ------------------------------------------------------------------
    # Player input preparation
    # ------------------------------------------------------------------

    @staticmethod
    def prepare_player_inputs(
        team_rotation: TeamRotation,
        rotation: List[PlayerMinutes],
    ) -> List[PlayerSimInput]:
        """Build simulation input params from rotation data.

        Joins TeamRotation.projections with PlayerMinutes by player_id,
        filtering out injured/DNP players (adjusted_minutes <= 0).
        """
        minutes_lookup = {p.player_id: p for p in rotation}
        inputs: List[PlayerSimInput] = []

        for proj in team_rotation.projections:
            if proj.adjusted_minutes <= 0:
                continue
            pm = minutes_lookup.get(proj.player_id)
            if not pm:
                continue

            # Default usage_rate to a small positive value if missing
            usage = pm.usage_rate if pm.usage_rate > 0 else 0.10

            # ── Bimodal GMM parameters for bench players ─────────────
            # Bench players (low baseline minutes, low usage) have a
            # bimodal distribution: their "base" state is the normal
            # workload, and a "ceiling" state fires when starters are
            # pulled (blowout) or injured (in-game).
            #
            # Ceiling probability is derived from:
            #   - Blowout efficiency factor on the allocation (team health)
            #   - Player's baseline vs starter-level minutes gap
            #   - Spread magnitude (bigger spread = more blowout risk)
            _ceil_prob = 0.0
            _ceil_minutes = 0.0
            _ceil_std = 0.0

            is_bench = proj.adjusted_minutes < 22.0 and usage < 0.20
            if is_bench:
                # Base ceiling: 12% chance for any bench player
                _ceil_prob = 0.12

                # Boost ceiling probability if blowout risk is elevated
                # (via garbage_time_factor < 1.0 from rotation engine)
                _gt = getattr(proj, "garbage_time_factor", 1.0)
                if _gt < 1.0:
                    # garbage_time_factor=0.85 → extra 10% ceiling prob
                    _ceil_prob += (1.0 - _gt) * 0.65
                    _ceil_prob = min(_ceil_prob, 0.35)  # Cap at 35%

                # Ceiling minutes: starter-level workload (28-34 min)
                # proportional to their positional default
                _ceil_minutes = min(
                    proj.adjusted_minutes + 12.0,  # +12 min bump
                    34.0,  # Hard cap
                )
                _ceil_std = 3.5  # Wider spread in ceiling state

            # ── Foul trouble risk ─────────────────────────────────
            # Centers and aggressive defenders foul more.  Positional
            # priors combined with high-minute starters (who play
            # more aggressive minutes = more foul exposure).
            _foul_prob = 0.0
            _foul_min_pct = 0.70  # 70% of projected minutes if fouled

            pos_upper = proj.position.upper() if proj.position else ""
            pos_parts = {p.strip() for p in pos_upper.replace("-", "/").split("/")}

            if proj.adjusted_minutes >= 20.0:  # Only model for rotation players
                # Positional base foul rates (NBA averages):
                #   C: ~3.5 PF/game → ~18% foul trouble risk
                #   PF: ~3.0 PF/game → ~14% risk
                #   SF/SG: ~2.5 PF/game → ~10% risk
                #   PG: ~2.2 PF/game → ~8% risk
                if pos_parts & {"C"}:
                    _foul_prob = 0.18
                elif pos_parts & {"PF"}:
                    _foul_prob = 0.14
                elif pos_parts & {"SF", "SG"}:
                    _foul_prob = 0.10
                else:
                    _foul_prob = 0.08

                # High-usage players foul more (aggressive drives/post-ups)
                if usage >= 0.25:
                    _foul_prob *= 1.15  # +15%

                # Starters in competitive games foul less carefully
                # (coaches protect them).  But if spread is large,
                # the bench plays more and starters sit anyway.

            inputs.append(
                PlayerSimInput(
                    player_id=proj.player_id,
                    player_name=proj.player_name,
                    position=proj.position,
                    projected_minutes=proj.adjusted_minutes,
                    usage_rate=usage,
                    pts_per_min=pm.pts_per_min,
                    reb_per_min=pm.reb_per_min,
                    ast_per_min=pm.ast_per_min,
                    stl_per_min=pm.stl_per_min,
                    blk_per_min=pm.blk_per_min,
                    tov_per_min=pm.tov_per_min,
                    fg3m_per_min=pm.fg3m_per_min,
                    garbage_time_factor=getattr(proj, "garbage_time_factor", 1.0),
                    ceiling_probability=_ceil_prob,
                    ceiling_minutes=_ceil_minutes,
                    ceiling_std=_ceil_std,
                    foul_trouble_probability=_foul_prob,
                    foul_trouble_minutes_pct=_foul_min_pct,
                )
            )

        return inputs

    # ------------------------------------------------------------------
    # Core simulation
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Game Script Scenario Sampling
    # ------------------------------------------------------------------

    @staticmethod
    def _game_script_probabilities(
        spread: float,
        is_home: bool,
        over_under: Optional[float] = None,
    ) -> Dict[str, float]:
        """Derive per-scenario probabilities from the Vegas/model spread.

        Parameters
        ----------
        spread : float
            Point spread (negative = home favored).
        is_home : bool
            Whether this team is the home team.
        over_under : float, optional
            Vegas over/under total line.  When provided, shifts probability
            between shootout and grind scenarios (high O/U → more shootout).

        Returns a dict mapping scenario name → probability (sums to 1.0).

        Probability logic (team-relative spread, positive = this team favored):
        * Large favorite (>= 8):  heavy blowout-win chance
        * Moderate favorite (4-8): balanced, some blowout-win
        * Pick'em (-3 to 3):     mostly close-game / shootout
        * Moderate dog (4-8):    balanced, some blowout-loss
        * Large dog (>= 8):     heavy blowout-loss chance
        """
        # Convert to team-relative (positive = this team favored)
        team_spread = -spread if is_home else spread

        if team_spread >= 8:
            probs = {
                "blowout_win": 0.35, "blowout_loss": 0.03,
                "close_game": 0.18, "shootout": 0.22, "grind": 0.22,
            }
        elif team_spread >= 4:
            probs = {
                "blowout_win": 0.18, "blowout_loss": 0.05,
                "close_game": 0.25, "shootout": 0.27, "grind": 0.25,
            }
        elif team_spread >= -3:
            # Pick'em — close-game dominant
            probs = {
                "blowout_win": 0.08, "blowout_loss": 0.08,
                "close_game": 0.35, "shootout": 0.27, "grind": 0.22,
            }
        elif team_spread >= -8:
            probs = {
                "blowout_win": 0.05, "blowout_loss": 0.18,
                "close_game": 0.25, "shootout": 0.27, "grind": 0.25,
            }
        else:
            probs = {
                "blowout_win": 0.03, "blowout_loss": 0.35,
                "close_game": 0.18, "shootout": 0.22, "grind": 0.22,
            }

        # ── Over/Under adjustment ─────────────────────────────────
        # High O/U (>224 NBA avg) shifts probability from grind → shootout.
        # Low O/U shifts shootout → grind.
        if over_under is not None:
            _OU_NEUTRAL = 224.0       # NBA average O/U
            _OU_SHIFT_SCALE = 0.002   # 0.2% shift per O/U point
            ou_delta = over_under - _OU_NEUTRAL
            shift = max(-0.08, min(0.08, ou_delta * _OU_SHIFT_SCALE))

            # Transfer between shootout and grind
            probs["shootout"] = max(0.05, probs["shootout"] + shift)
            probs["grind"] = max(0.05, probs["grind"] - shift)

            # Renormalize to sum to 1.0
            total = sum(probs.values())
            if total > 0:
                probs = {k: v / total for k, v in probs.items()}

        return probs

    def _sample_game_scenarios(
        self,
        N: int,
        spread: float,
        is_home: bool,
        over_under: Optional[float] = None,
    ) -> np.ndarray:
        """Sample game-script scenario indices for N simulations.

        Returns an integer array of shape (N,) where each value is an
        index into the ``_SCENARIO_NAMES`` list.
        """
        probs = self._game_script_probabilities(spread, is_home, over_under)
        scenario_names = list(GAME_SCRIPT_SCENARIOS.keys())
        prob_array = np.array([probs.get(s, 0.0) for s in scenario_names])
        # Ensure probabilities sum to 1
        prob_array = prob_array / prob_array.sum()
        return self._rng.choice(len(scenario_names), size=N, p=prob_array)

    def _sample_pace(self, projected_pace: float, n: int) -> np.ndarray:
        """Sample game pace from a normal distribution, clipped to ±20%."""
        pace_std = projected_pace * self.config.pace_variance
        sim_pace = self._rng.normal(loc=projected_pace, scale=pace_std, size=n)
        return np.clip(sim_pace, projected_pace * 0.80, projected_pace * 1.20)

    def _simulate_team(
        self,
        players: List[PlayerSimInput],
        sim_pace: np.ndarray,
        projected_team_score: float,
        player_noise_overrides: Optional[Dict[int, Dict[str, float]]] = None,
        matchup_factors: Optional[Dict[str, float]] = None,
        spread: float = 0.0,
        is_home: bool = True,
        sport: str = "nba",
        over_under: Optional[float] = None,
    ) -> Tuple[np.ndarray, Dict]:
        """Simulate N games for one team.

        Parameters
        ----------
        player_noise_overrides : dict, optional
            Per-player stat noise sigma overrides from Agent 8.
            Format: ``{player_id: {"pts": sigma, "reb": sigma, ...}}``
            Falls back to global STAT_NOISE_SIGMA for missing entries.
        spread : float
            Game spread (negative = home favored).  Used by the multi-
            scenario game-script model.
        is_home : bool
            Whether this team is the home team.
        over_under : float, optional
            Vegas O/U total line.  Shifts game-script probabilities
            (high O/U → more shootout scenarios).

        Returns
        -------
        team_scores : ndarray, shape (N,)
            Total team points per simulation.
        sim_data : dict
            Raw simulation arrays for building player results.
            Keys: "minutes" (P,N), "stats" {name: (P,N)},
                  "dk_pts" (P,N), "fd_pts" (P,N)
        """
        N = len(sim_pace)
        P = len(players)

        # Sport-aware constant overrides (CBB: 200 team-min, 40 max, 68 pace)
        from app.config.constants import get_sport_constants
        _sc = get_sport_constants(sport)
        _REG_MINUTES = _sc.get("REGULATION_MINUTES", REGULATION_MINUTES)
        _MAX_MINUTES = _sc.get("ABSOLUTE_MAX_MINUTES", MAX_PLAYER_MINUTES)
        _AVG_PACE = _sc.get("LEAGUE_AVG_PACE", LEAGUE_AVG_PACE)
        _PACE_SENS = _sc.get("PACE_SENSITIVITY", PACE_SENSITIVITY)

        # ---- Step 1: Sample minutes (bimodal GMM for bench players) ----
        # Players with ceiling_probability > 0 use a Gaussian Mixture:
        #   Component 1 (base): N(projected_minutes, base_std) — normal workload
        #   Component 2 (ceiling): N(ceiling_minutes, ceiling_std) — blowout/injury
        # All other players use the standard single Gaussian.
        # Fully vectorized: no Python loops over players or simulations.
        proj_minutes = np.array([p.projected_minutes for p in players])  # (P,)
        minutes_std = proj_minutes * self.config.minutes_variance  # (P,)

        # Extract GMM parameters (0.0 defaults = single Gaussian)
        ceiling_prob = np.array([p.ceiling_probability for p in players])  # (P,)
        ceiling_mean = np.array([p.ceiling_minutes for p in players])     # (P,)
        ceiling_std_arr = np.array([p.ceiling_std for p in players])      # (P,)

        has_gmm = ceiling_prob.max() > 0  # Any player with bimodal?

        if has_gmm:
            # ── Vectorized GMM: sample both components, then merge ─────
            # Base component: N(projected_minutes, base_std) — shape (P, N)
            base_samples = self._rng.normal(
                loc=proj_minutes[:, np.newaxis],
                scale=np.maximum(minutes_std[:, np.newaxis], 0.01),
                size=(P, N),
            )

            # Ceiling component: N(ceiling_minutes, ceiling_std) — shape (P, N)
            ceiling_samples = self._rng.normal(
                loc=ceiling_mean[:, np.newaxis],
                scale=np.maximum(ceiling_std_arr[:, np.newaxis], 0.01),
                size=(P, N),
            )

            # Binomial mask: for each (player, sim), does the ceiling state fire?
            # ceiling_mask[p, n] = True with probability ceiling_prob[p]
            ceiling_mask = (
                self._rng.random(size=(P, N))
                < ceiling_prob[:, np.newaxis]
            )

            # Merge: use ceiling sample where mask is True, base otherwise
            sim_minutes = np.where(ceiling_mask, ceiling_samples, base_samples)
        else:
            # Standard single Gaussian (no bimodal players on this team)
            sim_minutes = self._rng.normal(
                loc=proj_minutes[:, np.newaxis],
                scale=np.maximum(minutes_std[:, np.newaxis], 0.01),
                size=(P, N),
            )

        sim_minutes = np.clip(sim_minutes, 0.0, _MAX_MINUTES)

        # ---- Step 1b: Foul trouble reduction ----
        # Players who hit foul trouble play only ~70% of their projected
        # minutes.  Apply a binomial mask per (player, sim) based on each
        # player's foul_trouble_probability.
        foul_prob = np.array([p.foul_trouble_probability for p in players])  # (P,)
        if foul_prob.max() > 0:
            foul_min_pct = np.array([p.foul_trouble_minutes_pct for p in players])  # (P,)
            foul_mask = (
                self._rng.random(size=(P, N))
                < foul_prob[:, np.newaxis]
            )  # (P, N) — True where foul trouble fires
            # Reduce minutes for foul-troubled sims
            foul_factor = np.where(
                foul_mask,
                foul_min_pct[:, np.newaxis],
                1.0,
            )  # (P, N)
            sim_minutes = sim_minutes * foul_factor

        # ---- Step 2: Normalize to team regulation minutes per simulation ----
        # Pre-sample game-script scenarios here so we can use them for OT
        # modeling.  The scenario_indices array is reused in Step 4b below.
        scenario_names = list(GAME_SCRIPT_SCENARIOS.keys())
        scenario_indices = self._sample_game_scenarios(N, spread, is_home, over_under)

        # ---- Step 2a: Overtime modeling ----
        # Close games have ~6.5% OT probability.  OT adds 25 team-minutes
        # (5 min × 5 players) and raises per-player max to 53 min.
        close_game_idx = scenario_names.index("close_game") if "close_game" in scenario_names else -1
        is_close_game = (scenario_indices == close_game_idx) if close_game_idx >= 0 else np.zeros(N, dtype=bool)
        ot_mask = np.zeros(N, dtype=bool)
        if np.any(is_close_game):
            ot_draws = self._rng.random(N) < OT_PROBABILITY_CLOSE_GAME
            ot_mask = is_close_game & ot_draws

        # Per-sim normalization target and player cap
        norm_target = np.full(N, _REG_MINUTES)
        max_cap = np.full(N, _MAX_MINUTES)
        if np.any(ot_mask):
            norm_target[ot_mask] = _REG_MINUTES + OT_EXTRA_TEAM_MINUTES
            max_cap[ot_mask] = OT_MAX_PLAYER_MINUTES

        # ── Step 2b: Zero-sum minute correlation ─────────────────────
        # NBA minutes are zero-sum: if Player A gets +3 min, the team's
        # other players must collectively lose 3 min.  The old approach
        # (proportional scale to 240) preserved relative shares but made
        # per-player distributions artificially narrow.
        #
        # New approach: decompose each sim into (team_target × shares).
        # 1. Compute raw shares from the sampled minutes
        # 2. Apply a Dirichlet-like perturbation to the shares
        #    (this creates zero-sum deviations: one player up = others down)
        # 3. Multiply perturbed shares by team target (240 or 265 for OT)
        #
        # The perturbation magnitude scales with minutes_variance so
        # starters (high projected) are more stable than bench players.
        col_totals = sim_minutes.sum(axis=0)  # (N,)
        col_totals = np.where(col_totals > 0, col_totals, 1.0)

        # Raw shares: fraction of team minutes each player consumes
        raw_shares = sim_minutes / col_totals[np.newaxis, :]  # (P, N)

        # Dirichlet-like perturbation of shares.
        # Concentration parameter alpha controls stability:
        #   high alpha (50+) = shares barely move (starters are predictable)
        #   low alpha (5-15) = shares move a lot (bench volatility)
        # We use player-specific alpha proportional to projected minutes
        # so starters' shares are more stable than bench players'.
        #
        # Vectorized: sample N Dirichlet draws using a single alpha vector
        # derived from the mean raw shares (same alpha for all sims).
        # This is a deliberate approximation — per-sim alpha would require
        # a Python loop over N which destroys vectorization performance.
        mean_shares = raw_shares.mean(axis=1)  # (P,) — average share per player
        alpha_concentration = 80.0  # Higher = tighter around mean shares
        alpha_vec = np.maximum(mean_shares * alpha_concentration, 0.5)  # (P,)

        # Batch Dirichlet: shape (N, P) → transpose to (P, N)
        perturbed_shares = self._rng.dirichlet(alpha_vec, size=N).T  # (P, N)

        # Multiply perturbed shares by team target → guaranteed zero-sum
        sim_minutes = perturbed_shares * norm_target[np.newaxis, :]

        # Re-clip after share perturbation — use per-sim max cap
        sim_minutes = np.minimum(sim_minutes, max_cap[np.newaxis, :])
        sim_minutes = np.maximum(sim_minutes, 0.0)

        # ---- Step 3: Compute player stats with noise ----
        rates = {
            "pts": np.array([p.pts_per_min for p in players]),
            "reb": np.array([p.reb_per_min for p in players]),
            "ast": np.array([p.ast_per_min for p in players]),
            "stl": np.array([p.stl_per_min for p in players]),
            "blk": np.array([p.blk_per_min for p in players]),
            "tov": np.array([p.tov_per_min for p in players]),
            "fg3m": np.array([p.fg3m_per_min for p in players]),
        }

        # ---- Usage-rate variance sampling ----
        # Usage rate varies +/-10% game-to-game. Sample per-sim deviation.
        usage_rates_arr = np.array([
            getattr(p, 'usage_rate', 0.0) or 0.0 for p in players
        ])  # (P,)
        _has_usage = usage_rates_arr.sum() > 0
        _usage_ratio = None
        if _has_usage:
            usage_std = usage_rates_arr * 0.10
            sim_usage = self._rng.normal(
                loc=usage_rates_arr[:, np.newaxis],
                scale=np.maximum(usage_std[:, np.newaxis], 0.001),
                size=(P, N),
            )
            sim_usage = np.clip(sim_usage, 0.05, 0.45)
            baseline_usage = np.maximum(usage_rates_arr[:, np.newaxis], 0.05)
            _usage_ratio = sim_usage / baseline_usage  # (P, N)

        # Apply DvP matchup factors to per-minute rates (same as DFSService)
        if matchup_factors:
            # Map stat field names (lowercase) to rate keys
            for stat_name in rates:
                dvp = matchup_factors.get(stat_name, 1.0)
                if dvp != 1.0:
                    rates[stat_name] = rates[stat_name] * dvp

        # Apply garbage-time quality discount to per-minute rates.
        # Bench players in blowout games have garbage_time_factor < 1.0,
        # reflecting that extra minutes from starters sitting produce at a
        # lower per-minute rate than competitive minutes.
        gt_factors = np.array([p.garbage_time_factor for p in players])  # (P,)
        if np.any(gt_factors < 1.0):
            for stat_name in rates:
                rates[stat_name] = rates[stat_name] * gt_factors

        player_stats: Dict[str, np.ndarray] = {}

        # --- Correlated multivariate stat noise ---
        # Build a per-stat sigma vector (P, 7) and derive the covariance
        # matrix from the correlation matrix so that noise across stat
        # categories co-varies realistically (e.g. high-pts sims also
        # tend to produce more assists and 3PM).
        n_stats = len(STAT_ORDER)

        # Resolve per-player, per-stat sigma values.  Shape: (P, 7)
        if player_noise_overrides:
            sigma_matrix = np.array([
                [
                    player_noise_overrides.get(p.player_id, {}).get(
                        stat_name,
                        STAT_NOISE_SIGMA.get(stat_name, self.config.scoring_variance),
                    )
                    for stat_name in STAT_ORDER
                ]
                for p in players
            ])  # (P, 7)
        else:
            sigma_matrix = np.tile(
                np.array([
                    STAT_NOISE_SIGMA.get(s, self.config.scoring_variance)
                    for s in STAT_ORDER
                ]),
                (P, 1),
            )  # (P, 7)

        # ---- Archetype-based noise adjustment ----
        # Stars (high usage) are more predictable; bench players more volatile.
        # Scale sigma by usage tier: stars -15%, role players baseline, bench +20%.
        usage_rates = np.array([
            getattr(p, 'usage_rate', 0.20) or 0.20 for p in players
        ])  # (P,)
        archetype_scale = np.where(
            usage_rates >= 0.28, 0.85,           # Stars: tighter sigma
            np.where(usage_rates <= 0.15, 1.20,  # Bench: wider sigma
                     1.0)                         # Role players: baseline
        )  # (P,)
        sigma_matrix = sigma_matrix * archetype_scale[:, np.newaxis]  # (P, 7) * (P, 1)

        # ---- Step 3b: Matchup-quality variance adjustment ----
        # Against a favorable matchup (dvp > 1.0), production is more
        # predictable → reduce sigma.  Against a tough matchup (dvp < 1.0),
        # outcomes become less predictable → increase sigma.
        # sigma_adjusted = sigma × (2.0 - dvp)  scaled by sensitivity
        if matchup_factors and DVP_VARIANCE_SENSITIVITY > 0:
            dvp_mean = sum(matchup_factors.values()) / max(len(matchup_factors), 1)
            # variance_scale: favorable (dvp=1.10) → 0.90; tough (dvp=0.90) → 1.10
            variance_scale = 1.0 + (1.0 - dvp_mean) * DVP_VARIANCE_SENSITIVITY
            variance_scale = np.clip(variance_scale, 0.70, 1.40)
            sigma_matrix = sigma_matrix * variance_scale

        # Identify unique (sigma_profile, position_group) combinations to
        # avoid recomputing covariance unnecessarily while selecting the
        # correct position-based correlation matrix for each player.
        pos_labels = np.array([
            _POSITION_CORR_LABEL.get(
                getattr(p, "position", "").upper(), 3  # 3 = generic fallback
            )
            for p in players
        ], dtype=np.float64)  # (P,)

        # Composite key: sigma row (7 cols) + position label (1 col)
        composite_key = np.column_stack([sigma_matrix, pos_labels.reshape(-1, 1)])
        unique_keys, inverse_idx = np.unique(
            composite_key, axis=0, return_inverse=True,
        )

        # Pre-allocate noise array: (P, N, 7)
        noise_all = np.empty((P, N, n_stats), dtype=np.float64)

        for ui in range(len(unique_keys)):
            sigmas = unique_keys[ui, :n_stats]
            label = int(unique_keys[ui, n_stats])
            corr_matrix = _CORR_BY_LABEL.get(label, STAT_CORRELATION)

            # Covariance = outer(sigmas, sigmas) * position-specific correlation
            cov = np.outer(sigmas, sigmas) * corr_matrix
            player_mask = inverse_idx == ui
            n_players_in_group = int(player_mask.sum())
            # Draw (n_players_in_group * N) samples of dimension 7,
            # then reshape to (n_players_in_group, N, 7).
            draws = self._rng.multivariate_normal(
                mean=np.ones(n_stats),
                cov=cov,
                size=(n_players_in_group, N),
            )  # (n_players_in_group, N, 7)
            noise_all[player_mask] = draws

        # Clamp each noise value to [NOISE_MIN, NOISE_MAX]
        noise_all = np.clip(noise_all, NOISE_MIN, NOISE_MAX)

        # Apply noise to each stat column
        for si, stat_name in enumerate(STAT_ORDER):
            rate_arr = rates[stat_name]                      # (P,)
            noise = noise_all[:, :, si]                      # (P, N)
            raw = rate_arr[:, np.newaxis] * sim_minutes * noise
            player_stats[stat_name] = np.maximum(raw, 0.0)


        # ---- Apply usage-rate variance to offensive stats ----
        if _usage_ratio is not None:
            _USAGE_STAT_SENSITIVITY = {
                "pts": 0.8, "ast": 0.6, "fg3m": 0.7, "tov": 0.5,
            }
            for stat_name, sens in _USAGE_STAT_SENSITIVITY.items():
                if stat_name in player_stats:
                    adj = 1.0 + (_usage_ratio - 1.0) * sens
                    player_stats[stat_name] = player_stats[stat_name] * adj

        # ---- Step 4: Pace-calibrate ALL stats ----
        pace_factor = sim_pace / _AVG_PACE  # (N,)

        # Apply stat-specific pace sensitivity to every stat category.
        # Formula: adjusted = raw × (1 + (pace_factor - 1) × sensitivity)
        # This means PTS/AST scale fully with pace, while STL/BLK barely move.
        for stat_name in player_stats:
            sens = _PACE_SENS.get(stat_name, 0.5)
            if sens > 0:
                pace_adj = 1.0 + (pace_factor - 1.0) * sens  # (N,)
                player_stats[stat_name] = player_stats[stat_name] * pace_adj[np.newaxis, :]

        # ---- Step 4b: Scenario-based game-script noise ----
        # Each simulation samples a game state (blowout-win, close-game,
        # shootout, grind, blowout-loss) with spread-derived probabilities.
        # Starters and bench players receive different multipliers per
        # scenario, plus a small random jitter for intra-scenario variance.
        # NOTE: scenario_names and scenario_indices were pre-sampled in
        # Step 2a for OT modeling — reused here to avoid double-sampling.

        # Build per-player, per-sim multiplier array (P, N)
        is_starter = np.array(
            [p.projected_minutes >= GAME_SCRIPT_STARTER_MINUTES for p in players],
            dtype=np.float64,
        )  # (P,)  1.0=starter, 0.0=bench

        # Pre-build scenario multiplier lookup arrays for efficiency
        starter_mults = np.array([
            GAME_SCRIPT_SCENARIOS[s]["starter_mult"] for s in scenario_names
        ])
        bench_mults = np.array([
            GAME_SCRIPT_SCENARIOS[s]["bench_mult"] for s in scenario_names
        ])
        pace_mults = np.array([
            GAME_SCRIPT_SCENARIOS[s]["pace_mult"] for s in scenario_names
        ])

        # Map scenario indices → multipliers for each sim  (N,)
        starter_script = starter_mults[scenario_indices]  # (N,)
        bench_script = bench_mults[scenario_indices]      # (N,)
        pace_script = pace_mults[scenario_indices]        # (N,)

        # Per-player script: blend starter/bench based on role  (P, N)
        game_script = (
            is_starter[:, np.newaxis] * starter_script[np.newaxis, :]
            + (1.0 - is_starter[:, np.newaxis]) * bench_script[np.newaxis, :]
        )
        # Add small intra-scenario jitter (±3%) for natural variance
        jitter = self._rng.normal(1.0, 0.03, size=(P, N))
        jitter = np.clip(jitter, 0.90, 1.10)
        game_script = game_script * jitter

        # Apply scenario pace nudge to the already-sampled pace
        sim_pace = sim_pace * pace_script

        for stat_name in player_stats:
            player_stats[stat_name] = player_stats[stat_name] * game_script

        # Additional PTS calibration: anchor team total to projected score
        raw_team_pts = player_stats["pts"].sum(axis=0)  # (N,)
        raw_mean = float(raw_team_pts.mean())

        if raw_mean > 0:
            # Target = projected score (already accounts for pace via above)
            target_mean = projected_team_score
            calibration = target_mean / raw_mean  # scalar
            player_stats["pts"] = player_stats["pts"] * calibration

        # ---- Step 4c: Overtime total boost ----
        # OT sims get additional scoring distributed proportionally among
        # players by their minutes share.  This raises team totals by
        # ~8 pts for games that go to overtime.
        if np.any(ot_mask):
            ot_pts_share = sim_minutes[:, ot_mask]  # (P, n_ot)
            ot_total = ot_pts_share.sum(axis=0)     # (n_ot,)
            ot_total = np.where(ot_total > 0, ot_total, 1.0)
            ot_boost = OT_TOTAL_BOOST * (ot_pts_share / ot_total[np.newaxis, :])
            player_stats["pts"][:, ot_mask] += ot_boost

        team_scores = player_stats["pts"].sum(axis=0)  # (N,)

        # ---- Step 5: Vectorized DFS scoring ----
        dk_pts = (
            player_stats["pts"] * 1.0
            + player_stats["reb"] * 1.25
            + player_stats["ast"] * 1.5
            + player_stats["stl"] * 2.0
            + player_stats["blk"] * 2.0
            + player_stats["tov"] * -0.5
            + player_stats["fg3m"] * 0.5
        )

        # DD/TD bonuses
        cats = np.stack(
            [
                player_stats["pts"],
                player_stats["reb"],
                player_stats["ast"],
                player_stats["stl"],
                player_stats["blk"],
            ],
            axis=0,
        )  # (5, P, N)
        cats_over_10 = (cats >= 10.0).sum(axis=0)  # (P, N)
        dk_pts = dk_pts + np.where(cats_over_10 >= 2, 1.5, 0.0)
        dk_pts = dk_pts + np.where(cats_over_10 >= 3, 3.0, 0.0)

        fd_pts = (
            player_stats["pts"] * 1.0
            + player_stats["reb"] * 1.2
            + player_stats["ast"] * 1.5
            + player_stats["stl"] * 3.0
            + player_stats["blk"] * 3.0
            + player_stats["tov"] * -1.0
        )

        return team_scores, {
            "minutes": sim_minutes,
            "stats": player_stats,
            "dk_pts": dk_pts,
            "fd_pts": fd_pts,
        }

    # ------------------------------------------------------------------
    # Cross-team correlation
    # ------------------------------------------------------------------

    @staticmethod
    def _recompute_dfs_scores(
        player_stats: Dict[str, np.ndarray],
    ) -> Tuple[np.ndarray, np.ndarray]:
        """Recompute DK and FD fantasy points from adjusted stat arrays.

        Used after cross-team adjustments modify stats in-place so that
        DFS scores reflect the updated stat lines.

        Parameters
        ----------
        player_stats : dict
            ``{stat_name: ndarray(P, N)}``

        Returns
        -------
        dk_pts, fd_pts : ndarray(P, N), ndarray(P, N)
        """
        dk_pts = (
            player_stats["pts"] * 1.0
            + player_stats["reb"] * 1.25
            + player_stats["ast"] * 1.5
            + player_stats["stl"] * 2.0
            + player_stats["blk"] * 2.0
            + player_stats["tov"] * -0.5
            + player_stats["fg3m"] * 0.5
        )
        # DD/TD bonuses
        cats = np.stack([
            player_stats["pts"], player_stats["reb"], player_stats["ast"],
            player_stats["stl"], player_stats["blk"],
        ], axis=0)  # (5, P, N)
        cats_over_10 = (cats >= 10.0).sum(axis=0)  # (P, N)
        dk_pts = dk_pts + np.where(cats_over_10 >= 2, 1.5, 0.0)
        dk_pts = dk_pts + np.where(cats_over_10 >= 3, 3.0, 0.0)

        fd_pts = (
            player_stats["pts"] * 1.0
            + player_stats["reb"] * 1.2
            + player_stats["ast"] * 1.5
            + player_stats["stl"] * 3.0
            + player_stats["blk"] * 3.0
            + player_stats["tov"] * -1.0
        )
        return dk_pts, fd_pts

    def _apply_cross_team_effects(
        self,
        home_sim_data: Dict,
        away_sim_data: Dict,
        N: int,
    ) -> None:
        """Apply cross-team negative correlation effects in-place.

        Three effects:
        1. Shared game environment — both teams' PTS/AST/3PM scaled by
           the same random factor (high-action vs grind games).
        2. Defensive depression — when Team A scores above median,
           Team B's STL/BLK are depressed (and vice versa).
        3. Rebound coupling — when Team A scores efficiently (high PTS),
           Team B's REB opportunities decrease.

        Modifies ``home_sim_data`` and ``away_sim_data`` stats in-place,
        then recomputes ``dk_pts`` and ``fd_pts``.
        """
        from app.config.constants import (
            CROSS_TEAM_GAME_ENV_SIGMA,
            CROSS_TEAM_GAME_ENV_STATS,
            CROSS_TEAM_DEF_SENSITIVITY,
            CROSS_TEAM_DEF_CLAMP_MIN,
            CROSS_TEAM_DEF_CLAMP_MAX,
            CROSS_TEAM_REB_SENSITIVITY,
            CROSS_TEAM_REB_CLAMP_MIN,
            CROSS_TEAM_REB_CLAMP_MAX,
        )

        home_stats = home_sim_data["stats"]
        away_stats = away_sim_data["stats"]

        # --- Effect 1: Shared Game Environment ---
        game_env = self._rng.normal(1.0, CROSS_TEAM_GAME_ENV_SIGMA, size=N)
        game_env = np.clip(game_env, 0.85, 1.15)

        for stat_name in CROSS_TEAM_GAME_ENV_STATS:
            if stat_name in home_stats:
                home_stats[stat_name] = home_stats[stat_name] * game_env[np.newaxis, :]
            if stat_name in away_stats:
                away_stats[stat_name] = away_stats[stat_name] * game_env[np.newaxis, :]

        # --- Effect 2: Defensive Depression ---
        home_team_pts = home_stats["pts"].sum(axis=0)  # (N,)
        away_team_pts = away_stats["pts"].sum(axis=0)  # (N,)

        home_pts_median = max(float(np.median(home_team_pts)), 1e-9)
        away_pts_median = max(float(np.median(away_team_pts)), 1e-9)

        # Deviation: fraction above/below median
        home_deviation = (home_team_pts - home_pts_median) / home_pts_median
        away_deviation = (away_team_pts - away_pts_median) / away_pts_median

        # When opponent scores above median → depress this team's DEF stats
        away_def_factor = np.clip(
            1.0 - home_deviation * CROSS_TEAM_DEF_SENSITIVITY,
            CROSS_TEAM_DEF_CLAMP_MIN,
            CROSS_TEAM_DEF_CLAMP_MAX,
        )
        home_def_factor = np.clip(
            1.0 - away_deviation * CROSS_TEAM_DEF_SENSITIVITY,
            CROSS_TEAM_DEF_CLAMP_MIN,
            CROSS_TEAM_DEF_CLAMP_MAX,
        )

        for stat_name in ("stl", "blk"):
            if stat_name in home_stats:
                home_stats[stat_name] = home_stats[stat_name] * home_def_factor[np.newaxis, :]
            if stat_name in away_stats:
                away_stats[stat_name] = away_stats[stat_name] * away_def_factor[np.newaxis, :]

        # --- Effect 3: Rebound Coupling ---
        away_reb_factor = np.clip(
            1.0 - home_deviation * CROSS_TEAM_REB_SENSITIVITY,
            CROSS_TEAM_REB_CLAMP_MIN,
            CROSS_TEAM_REB_CLAMP_MAX,
        )
        home_reb_factor = np.clip(
            1.0 - away_deviation * CROSS_TEAM_REB_SENSITIVITY,
            CROSS_TEAM_REB_CLAMP_MIN,
            CROSS_TEAM_REB_CLAMP_MAX,
        )

        if "reb" in home_stats:
            home_stats["reb"] = home_stats["reb"] * home_reb_factor[np.newaxis, :]
        if "reb" in away_stats:
            away_stats["reb"] = away_stats["reb"] * away_reb_factor[np.newaxis, :]

        # --- Recompute DFS scores ---
        home_sim_data["dk_pts"], home_sim_data["fd_pts"] = self._recompute_dfs_scores(home_stats)
        away_sim_data["dk_pts"], away_sim_data["fd_pts"] = self._recompute_dfs_scores(away_stats)

    # ------------------------------------------------------------------
    # Result building
    # ------------------------------------------------------------------

    def _build_team_result(
        self,
        team_id: int,
        team_name: str,
        players: List[PlayerSimInput],
        team_scores: np.ndarray,
        sim_data: Dict,
    ) -> TeamSimResult:
        """Aggregate simulation arrays into TeamSimResult."""
        player_results = [
            self._build_player_result(p, i, sim_data) for i, p in enumerate(players)
        ]

        score_pcts = np.percentile(team_scores, PERCENTILE_KEYS)

        return TeamSimResult(
            team_id=team_id,
            team_name=team_name,
            mean_score=round(float(team_scores.mean()), 1),
            std_score=round(float(team_scores.std()), 1),
            median_score=round(float(np.median(team_scores)), 1),
            score_percentiles={
                f"p{k}": round(float(v), 1)
                for k, v in zip(PERCENTILE_KEYS, score_pcts)
            },
            players=player_results,
        )

    @staticmethod
    def _build_player_result(
        player: PlayerSimInput,
        idx: int,
        sim_data: Dict,
    ) -> PlayerSimResult:
        """Build PlayerSimResult from raw simulation arrays."""

        def _pct_dict(arr: np.ndarray) -> Dict[str, float]:
            pcts = np.percentile(arr, PERCENTILE_KEYS)
            return {
                f"p{k}": round(float(v), 1)
                for k, v in zip(PERCENTILE_KEYS, pcts)
            }

        mins = sim_data["minutes"][idx]
        pts = sim_data["stats"]["pts"][idx]
        reb = sim_data["stats"]["reb"][idx]
        ast = sim_data["stats"]["ast"][idx]
        stl = sim_data["stats"]["stl"][idx]
        blk = sim_data["stats"]["blk"][idx]
        tov = sim_data["stats"]["tov"][idx]
        fg3m = sim_data["stats"]["fg3m"][idx]
        dk = sim_data["dk_pts"][idx]
        fd = sim_data["fd_pts"][idx]

        return PlayerSimResult(
            player_id=player.player_id,
            player_name=player.player_name,
            position=player.position,
            mean_minutes=round(float(mins.mean()), 1),
            mean_pts=round(float(pts.mean()), 1),
            mean_reb=round(float(reb.mean()), 1),
            mean_ast=round(float(ast.mean()), 1),
            mean_stl=round(float(stl.mean()), 1),
            mean_blk=round(float(blk.mean()), 1),
            mean_tov=round(float(tov.mean()), 1),
            mean_fg3m=round(float(fg3m.mean()), 1),
            mean_dk_points=round(float(dk.mean()), 1),
            mean_fd_points=round(float(fd.mean()), 1),
            std_pts=round(float(pts.std()), 1),
            std_reb=round(float(reb.std()), 1),
            std_ast=round(float(ast.std()), 1),
            std_dk_points=round(float(dk.std()), 1),
            std_fd_points=round(float(fd.std()), 1),
            dk_percentiles=_pct_dict(dk),
            fd_percentiles=_pct_dict(fd),
            pts_percentiles=_pct_dict(pts),
        )

    # ------------------------------------------------------------------
    # Edge case handling
    # ------------------------------------------------------------------

    @staticmethod
    def _degenerate_result(game_info: GameInfo, n: int) -> GameSimResult:
        """Return a zero-value result when rotation data is missing."""
        empty_team = TeamSimResult(
            team_id=0,
            team_name="Unknown",
            mean_score=0.0,
            std_score=0.0,
            median_score=0.0,
            score_percentiles={f"p{k}": 0.0 for k in PERCENTILE_KEYS},
            players=[],
        )
        return GameSimResult(
            game_id=game_info.game_id,
            num_simulations=n,
            home_team=empty_team,
            away_team=empty_team,
            home_win_pct=0.5,
            away_win_pct=0.5,
            mean_total=0.0,
            std_total=0.0,
            projected_spread=0.0,
            spread_std=0.0,
        )
