"""DFS fantasy point projection service.

Computes DraftKings and FanDuel fantasy point projections by
multiplying projected minutes by per-minute stat production rates,
then applying platform-specific scoring weights.

Enhancements over a naive ``minutes × rate`` model:

- **Matchup (DvP) factors**: Each stat category is scaled by the
  opponent's defensive strength in that category relative to the
  league average.
- **Stat-specific pace sensitivity**: Points and assists scale
  linearly with pace, while steals and blocks are much less
  pace-dependent.
- **Floor / ceiling stat-rate variance**: The floor represents a
  cold shooting night (lower per-minute rates AND slightly fewer
  minutes); the ceiling represents a hot night (higher rates +
  slightly more minutes).
- **Probability-weighted DD/TD bonuses**: Instead of a hard
  threshold at exactly 10.0, the expected value of the bonus is
  calculated using a normal-distribution approximation of each
  stat category's likelihood of hitting 10+.

DraftKings NBA scoring:
    PTS ×1.0  |  REB ×1.25  |  AST ×1.5  |  STL ×2.0  |  BLK ×2.0
    TO ×-0.5  |  3PM ×0.5   |  DD bonus +1.5  |  TD bonus +3.0

FanDuel NBA scoring:
    PTS ×1.0  |  REB ×1.2   |  AST ×1.5  |  STL ×3.0  |  BLK ×3.0
    TO ×-1.0
"""

import functools
import logging
import math
import numpy as np
from typing import Dict, List, Optional, Tuple

from app.models.player import PlayerMinutes, PlayerProjection
from app.models.rotation import TeamRotation
from app.models.dfs import DFSProjection, TeamDFSProjection
from app.config.constants import (
    CBB_PACE_SENSITIVITY,
    FLOOR_MINUTES_MULT,
    CEILING_MINUTES_MULT,
    FLOOR_RATE_MULT,
    CEILING_RATE_MULT,
    PACE_SENSITIVITY,
    DD_STAT_CV,
    DD_STAT_VARIANCE,
    USAGE_BOOST_STAT_WEIGHTS,
    DVP_FLOOR_SENSITIVITY,
    DVP_CEILING_SENSITIVITY,
    PROPS_PROJECTION_BLEND_WEIGHT,
    PROPS_DIVERGENCE_THRESHOLD,
    PROPS_DD_BLEND_WEIGHT,
    PLAYER_CV_BASELINE,
    PLAYER_CV_FLOOR_SCALE,
    PLAYER_CV_CEILING_SCALE,
    PLAYER_CV_FALLBACK,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normal_cdf(z: float) -> float:
    """Standard normal CDF using the error function (no scipy needed)."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _prob_over(projected: float, threshold: float = 10.0, stat: str = None) -> float:
    """Probability that a stat exceeds ``threshold`` in a single game.

    Uses a normal approximation with stat-specific CV for the sigma.
    Falls back to the global DD_STAT_VARIANCE if stat is not specified.
    """
    if projected <= 0:
        return 0.0
    cv = DD_STAT_CV.get(stat, DD_STAT_VARIANCE) if stat else DD_STAT_VARIANCE
    sigma = projected * cv
    if sigma < 0.01:
        return 1.0 if projected >= threshold else 0.0
    z = (threshold - projected) / sigma
    return 1.0 - _normal_cdf(z)


class DFSService:
    """Computes DraftKings and FanDuel fantasy point projections."""

    def __init__(self, calibration_service=None, props_service=None):
        self._calibration = calibration_service
        self._props_service = props_service

    def _project_stats(
        self,
        projected_minutes: float,
        player: PlayerMinutes,
        pace_factor: float = 1.0,
        matchup_factors: Optional[Dict[str, float]] = None,
        rate_multiplier: float = 1.0,
        sport: str = "nba",
    ) -> dict:
        """Project counting stats from minutes, per-minute rates, pace,
        opponent matchup, and an optional rate multiplier (for floor/ceiling).

        The formula per stat is::

            stat = minutes
                 × per_min_rate
                 × rate_multiplier
                 × pace_adjustment
                 × matchup_factor
                 × stat_rate_calibration

        Where:
            pace_adjustment = pace_factor ** (sensitivity[stat] * pace_cal)
            matchup_factor  = 1 + (raw_dvp - 1) × dvp_sensitivity_cal
        """
        cal = self._calibration
        stats = {}
        for field, attr in [
            ("pts", "pts_per_min"), ("reb", "reb_per_min"),
            ("ast", "ast_per_min"), ("stl", "stl_per_min"),
            ("blk", "blk_per_min"), ("tov", "tov_per_min"),
            ("fg3m", "fg3m_per_min"),
        ]:
            rate = getattr(player, attr, 0.0)

            # Pace adjustment via power-law (with learned sensitivity calibration).
            pace_map = CBB_PACE_SENSITIVITY if sport == "cbb" else PACE_SENSITIVITY
            sens = pace_map.get(field, 0.5)
            if cal:
                sens *= cal.get_pace_sensitivity_adjustment(field)
            # Power-law avoids double-counting: per_min_rate already reflects
            # the player's historical pace environment.  pace_factor ** sens
            # adjusts the rate for this game's projected pace without compounding.
            pace_adj = pace_factor ** sens if pace_factor > 0 and sens > 0 else 1.0

            # DvP matchup factor (with learned per-stat sensitivity calibration)
            raw_dvp = matchup_factors.get(field, 1.0) if matchup_factors else 1.0
            if cal and raw_dvp != 1.0:
                dvp_sens = cal.get_dvp_sensitivity_for_stat(field)
                dvp = 1.0 + (raw_dvp - 1.0) * dvp_sens
            else:
                dvp = raw_dvp

            # Apply usage boost with per-stat sensitivity.  When a teammate
            # is injured, scoring/assists increase much more than rebounds
            # or defensive stats.  stat_weight=1.0 gives full boost,
            # 0.0 gives none.  When rate_multiplier is 1.0 (no boost),
            # effective_boost is always 1.0 regardless of weights.
            stat_weight = USAGE_BOOST_STAT_WEIGHTS.get(field, 0.5)
            effective_boost = 1.0 + (rate_multiplier - 1.0) * stat_weight

            value = projected_minutes * rate * effective_boost * pace_adj * dvp

            # Apply learned per-stat rate calibration
            if cal:
                value *= cal.get_stat_rate_adjustment(field)

            stats[field] = round(value, 1)
        return stats

    def _project_stats_decomposed(
        self,
        projected_minutes: float,
        player: PlayerMinutes,
        pace_factor: float = 1.0,
        matchup_factors: Optional[Dict[str, float]] = None,
        rate_multiplier: float = 1.0,
        sport: str = "nba",
    ) -> dict:
        """Project stats with shot-type decomposition for PTS and FG3M.

        When a player's shooting profile is available, PTS are decomposed
        into ``fg3m × 3 + fg2m × 2 + ftm × 1`` instead of using the
        aggregate ``pts_per_min`` rate.  This makes the DK 3PM bonus
        (+0.5 per FG3M) naturally correlated with scoring volume for
        shooters — a guard scoring 20 via threes is worth more in DK
        scoring than a center scoring 20 via twos.

        Falls back to ``_project_stats()`` when the shooting profile is
        missing or incomplete.
        """
        if not getattr(player, "has_shooting_profile", False):
            return self._project_stats(
                projected_minutes, player, pace_factor, matchup_factors,
                rate_multiplier, sport,
            )

        from app.config.constants import (
            SHOT_DECOMP_LEAGUE_AVG_FG3_PCT,
            SHOT_DECOMP_LEAGUE_AVG_FG2_PCT,
            SHOT_DECOMP_LEAGUE_AVG_FT_PCT,
        )

        # First project all non-scoring stats using the base method
        stats = self._project_stats(
            projected_minutes, player, pace_factor, matchup_factors,
            rate_multiplier, sport,
        )

        # Decompose PTS and FG3M from shooting profile
        cal = self._calibration
        pace_map = CBB_PACE_SENSITIVITY if sport == "cbb" else PACE_SENSITIVITY
        pts_sens = pace_map.get("pts", 0.5)
        if cal:
            pts_sens *= cal.get_pace_sensitivity_adjustment("pts")
        # Power-law avoids double-counting (see _project_stats docstring)
        pace_adj = pace_factor ** pts_sens if pace_factor > 0 and pts_sens > 0 else 1.0

        dvp_pts = matchup_factors.get("pts", 1.0) if matchup_factors else 1.0
        if cal and dvp_pts != 1.0:
            dvp_pts = 1.0 + (dvp_pts - 1.0) * cal.get_dvp_sensitivity_for_stat("pts")

        stat_weight = USAGE_BOOST_STAT_WEIGHTS.get("pts", 0.5)
        effective_boost = 1.0 + (rate_multiplier - 1.0) * stat_weight

        # Shooting rates with league-average fallbacks for individual %
        fg3_pct = player.fg3_pct if player.fg3_pct is not None else SHOT_DECOMP_LEAGUE_AVG_FG3_PCT
        fg2_pct = player.fg2_pct if player.fg2_pct is not None else SHOT_DECOMP_LEAGUE_AVG_FG2_PCT
        ft_pct = player.ft_pct if player.ft_pct is not None else SHOT_DECOMP_LEAGUE_AVG_FT_PCT

        fg3a_rate = player.fg3a_rate or 0.0
        fga_rate = player.fga_rate or 0.0
        fta_rate = player.fta_rate or 0.0

        # Apply learned shot rate calibration adjustments
        if cal:
            fg3a_rate *= cal.get_shot_rate_adjustment("fg3a")
            fga_rate *= cal.get_shot_rate_adjustment("fga")
            fta_rate *= cal.get_shot_rate_adjustment("fta")

        # Decompose: fg2a = fga - fg3a
        fg2a_rate = max(0.0, fga_rate - fg3a_rate)

        # Project made shots from minutes × attempt rate × percentage
        base_mult = projected_minutes * effective_boost * pace_adj * dvp_pts
        fg3m = base_mult * fg3a_rate * fg3_pct
        fg2m = base_mult * fg2a_rate * fg2_pct
        ftm = base_mult * fta_rate * ft_pct

        # Apply stat rate calibration
        if cal:
            pts_cal = cal.get_stat_rate_adjustment("pts")
            fg3m_cal = cal.get_stat_rate_adjustment("fg3m")
            fg3m *= fg3m_cal
            fg2m *= pts_cal  # 2PM uses PTS calibration
            ftm *= pts_cal   # FTM uses PTS calibration

        # Compute decomposed PTS and FG3M
        decomposed_pts = fg3m * 3.0 + fg2m * 2.0 + ftm * 1.0
        decomposed_fg3m = fg3m

        # Replace PTS and FG3M with decomposed values
        stats["pts"] = round(decomposed_pts, 1)
        stats["fg3m"] = round(decomposed_fg3m, 1)

        return stats

    # ---- Correlated DD/TD probability model ----
    # 5×5 correlation sub-matrix for DD/TD stats (pts, reb, ast, stl, blk),
    # extracted from simulation_engine.STAT_CORRELATION.
    _DD_STATS = ["pts", "reb", "ast", "stl", "blk"]
    _DD_CORR = np.array([
        # pts   reb   ast   stl   blk
        [1.00, 0.15, 0.40, 0.05, 0.02],  # pts
        [0.15, 1.00, 0.10, 0.05, 0.20],  # reb
        [0.40, 0.10, 1.00, 0.10, 0.00],  # ast
        [0.05, 0.05, 0.10, 1.00, 0.15],  # stl
        [0.02, 0.20, 0.00, 0.15, 1.00],  # blk
    ])

    @classmethod
    def _correlated_dd_td_probs(cls, stats: dict) -> Tuple[float, float]:
        """Compute P(DD) and P(TD) using correlated stat draws.

        Wraps the cached implementation with a hashable key derived from
        rounded stat values.  Since the RNG seed is fixed (42), identical
        inputs always produce identical outputs, making LRU caching safe.
        """
        # Build hashable key from rounded stat values (1 decimal place)
        key = tuple(round(stats.get(s, 0.0), 1) for s in cls._DD_STATS)
        return cls._cached_dd_td_probs(key)

    @staticmethod
    @functools.lru_cache(maxsize=512)
    def _cached_dd_td_probs(stats_key: tuple) -> Tuple[float, float]:
        """Cached Monte Carlo DD/TD probability computation.

        Uses N=1000 samples with a fixed seed — statistically sufficient
        for DD probabilities in the 5-50% range (SE ~1.5% vs ~0.7% at
        N=5000, well within overall model uncertainty).
        """
        n_sims = 1000
        dd_stats = DFSService._DD_STATS
        n_cats = len(dd_stats)

        means = np.array(stats_key)
        cvs = np.array([DD_STAT_CV.get(s, DD_STAT_VARIANCE) for s in dd_stats])
        sigmas = means * cvs

        # Early exit: if fewer than 2 stats have any chance of reaching 10
        viable = np.sum(means > 3.0)
        if viable < 2:
            return 0.0, 0.0

        # Build covariance from correlation + sigmas
        sigma_outer = np.outer(sigmas, sigmas)
        cov_matrix = DFSService._DD_CORR * sigma_outer

        # Ensure positive semi-definite
        cov_matrix = (cov_matrix + cov_matrix.T) / 2
        eigvals = np.linalg.eigvalsh(cov_matrix)
        if eigvals.min() < 0:
            cov_matrix += np.eye(n_cats) * (abs(eigvals.min()) + 1e-6)

        rng = np.random.default_rng(seed=42)
        samples = rng.multivariate_normal(means, cov_matrix, size=n_sims)
        hits = (samples >= 10.0).sum(axis=1)

        p_dd = float((hits >= 2).mean())
        p_td = float((hits >= 3).mean())
        return p_dd, p_td

    def _apply_props_calibration(
        self,
        stats: dict,
        player_name: str,
        team: str,
        game_date: Optional[str] = None,
    ) -> dict:
        """Blend stat projections toward DK Sportsbook market lines.

        When our projection diverges from the market line by more than
        PROPS_DIVERGENCE_THRESHOLD (15%), blends toward the market:
            adjusted = ours × 0.70 + market × 0.30

        This preserves our model's signal while anchoring extreme
        divergences toward the market-implied line.

        Returns the same stats dict (modified in place if blending applied).
        """
        if not self._props_service:
            return stats

        try:
            props = self._props_service.get_player_prop(
                player_name, team, game_date
            )
        except Exception:
            return stats

        if not props:
            return stats

        blend_w = PROPS_PROJECTION_BLEND_WEIGHT  # 0.70
        market_w = 1.0 - blend_w                 # 0.30

        for stat in ("pts", "reb", "ast"):
            line = props.get_line(stat)
            if not line or line.line <= 0:
                continue
            our_val = stats.get(stat, 0.0)
            if our_val <= 0:
                continue

            delta_pct = abs(our_val - line.line) / line.line
            if delta_pct > PROPS_DIVERGENCE_THRESHOLD:
                adjusted = our_val * blend_w + line.line * market_w
                stats[stat] = round(adjusted, 1)
                logger.debug(
                    f"[Props] {player_name} {stat}: {our_val:.1f} → "
                    f"{adjusted:.1f} (market {line.line})"
                )

        return stats

    def _blend_dd_td_with_props(
        self,
        p_dd: float,
        p_td: float,
        player_name: str,
        team: str,
        game_date: Optional[str] = None,
    ) -> Tuple[float, float]:
        """Blend our Monte Carlo DD/TD probabilities with DK market implied probs.

        When DD/TD prop lines are available, uses a weighted blend:
            blended = ours × 0.60 + market_implied × 0.40

        Returns (blended_p_dd, blended_p_td).
        """
        if not self._props_service:
            return p_dd, p_td

        try:
            props = self._props_service.get_player_prop(
                player_name, team, game_date
            )
        except Exception:
            return p_dd, p_td

        if not props:
            return p_dd, p_td

        our_w = PROPS_DD_BLEND_WEIGHT      # 0.60
        market_w = 1.0 - our_w             # 0.40

        blended_dd = p_dd
        blended_td = p_td

        dd_line = props.get_line("dd")
        if dd_line and dd_line.implied_over_prob > 0:
            blended_dd = p_dd * our_w + dd_line.implied_over_prob * market_w

        td_line = props.get_line("td")
        if td_line and td_line.implied_over_prob > 0:
            blended_td = p_td * our_w + td_line.implied_over_prob * market_w

        return blended_dd, blended_td

    # ──────────────────────────────────────────────────────────────────
    # Sport-aware scoring entry point (added in Prompt 2.1)
    # ──────────────────────────────────────────────────────────────────

    @classmethod
    def _calculate_score(
        cls,
        stats: dict,
        sport: str = "nba",
        position: str = "",
        platform: str = "dk",
    ) -> float:
        """Sport-aware fantasy-point calculation.

        Dispatch order:
          1. **Polymorphic** — when ``cfg.scoring_map`` is non-empty
             (currently MLB only), look up the player's class in
             ``cfg.pos_to_class`` and apply only that class's
             coefficients. This is the rule that makes Shohei Ohtani's
             pitcher entry score *only* on pitcher stats, ignoring any
             batting stats in the same row.
          2. **Flat coefficients** — when ``cfg.dk_scoring`` is non-empty
             (currently NFL), apply the dot product of ``cfg.dk_scoring``
             and ``stats``, then add any threshold bonuses from
             ``cfg.dk_scoring_bonuses``.
          3. **Legacy** — fall back to ``_dk_score`` / ``_fd_score`` for
             NBA / CBB. Existing behaviour is unchanged for those sports.

        Parameters
        ----------
        stats : dict
            Lower-snake-case stat name -> value (e.g. ``{'k': 7, 'ip': 6.0}``
            for a pitcher; ``{'pts': 25, 'reb': 8, 'ast': 5}`` for an NBA
            player). Missing keys are treated as zero.
        sport : str
            Lower-case sport code passed through ``app.sports.get_config``.
        position : str
            DK draftables-style position string (``"P"``, ``"OF"``,
            ``"QB"``, ``"PG"``…). Used by the polymorphic dispatch to
            decide which sub-table in ``scoring_map`` to apply. Ignored
            for flat-coefficient sports.
        platform : str
            ``"dk"`` (default) or ``"fd"``. Currently only DK has a
            registry-backed scoring path; FD continues through the
            legacy ``_fd_score``.
        """
        from app.sports import get_config
        try:
            cfg = get_config(sport)
        except ValueError:
            # Unknown sport — fall through to NBA legacy. Preserves
            # behavior for any caller that passes an unrecognised sport.
            cfg = None

        # ── Polymorphic dispatch (MLB) ────────────────────────────────
        if cfg and cfg.scoring_map:
            cls_key = cfg.pos_to_class.get(position.upper() if position else "")
            if not cls_key:
                # Default to the first registered class so unknown positions
                # at least produce a number rather than crashing.
                cls_key = next(iter(cfg.scoring_map.keys()))
            coefs = cfg.scoring_map.get(cls_key, {})
            fp = 0.0
            for stat, coef in coefs.items():
                fp += stats.get(stat, 0) * coef
            return round(fp, 2)

        # ── Flat coefficients (NFL) ──────────────────────────────────
        if cfg and cfg.dk_scoring and platform == "dk":
            fp = 0.0
            for stat, coef in cfg.dk_scoring.items():
                fp += stats.get(stat, 0) * coef
            # Threshold bonuses (e.g. NFL 300+ pass yards = +3)
            for bonus in cfg.dk_scoring_bonuses or []:
                stat_val = stats.get(bonus.get("stat"), 0)
                if stat_val >= bonus.get("threshold", 0):
                    fp += bonus.get("bonus", 0)
            return round(fp, 2)

        # ── Legacy formulas (NBA / CBB) ──────────────────────────────
        if platform == "fd":
            return cls._fd_score(stats)
        return cls._dk_score(stats)

    @classmethod
    def _dk_score(cls, stats: dict) -> float:
        """Calculate DraftKings fantasy points from projected stats.

        Uses correlated probability-weighted DD/TD bonuses that account
        for cross-stat dependencies (e.g. high-scoring games also
        produce more assists).
        """
        fp = (
            stats["pts"] * 1.0
            + stats["reb"] * 1.25
            + stats["ast"] * 1.5
            + stats["stl"] * 2.0
            + stats["blk"] * 2.0
            + stats["tov"] * -0.5
            + stats["fg3m"] * 0.5
        )

        # Correlated DD/TD bonus using Monte Carlo with stat correlations
        p_dd, p_td = cls._correlated_dd_td_probs(stats)
        fp += p_dd * 1.5
        fp += p_td * 3.0

        return round(fp, 1)

    def _dk_score_with_props(
        self,
        stats: dict,
        player_name: str = "",
        team: str = "",
        game_date: Optional[str] = None,
        sport: str = "nba",
    ) -> float:
        """Calculate DK fantasy points with props-blended DD/TD bonuses.

        Uses the standard DK scoring formula but blends our Monte Carlo
        DD/TD probabilities with DK Sportsbook implied probabilities
        when available.
        """
        fp = (
            stats["pts"] * 1.0
            + stats["reb"] * 1.25
            + stats["ast"] * 1.5
            + stats["stl"] * 2.0
            + stats["blk"] * 2.0
            + stats["tov"] * -0.5
            + stats["fg3m"] * 0.5
        )

        # Correlated DD/TD bonus using Monte Carlo with stat correlations
        p_dd, p_td = self._correlated_dd_td_probs(stats)

        # CBB: 40-minute games produce far fewer DDs/TDs than 48-min NBA
        if sport == "cbb":
            p_dd *= 0.4
            p_td *= 0.1

        # Blend with DK market-implied probabilities when available
        # Skip for CBB — DK props API uses NBA-specific event group IDs.
        if sport != "cbb":
            p_dd, p_td = self._blend_dd_td_with_props(
                p_dd, p_td, player_name, team, game_date
            )

        fp += p_dd * 1.5
        fp += p_td * 3.0

        return round(fp, 1)

    @staticmethod
    def _fd_score(stats: dict) -> float:
        """Calculate FanDuel fantasy points from projected stats."""
        fp = (
            stats["pts"] * 1.0
            + stats["reb"] * 1.2
            + stats["ast"] * 1.5
            + stats["stl"] * 3.0
            + stats["blk"] * 3.0
            + stats["tov"] * -1.0
        )
        return round(fp, 1)

    def project_player_dfs(
        self,
        projection: PlayerProjection,
        player_minutes: PlayerMinutes,
        pace_factor: float = 1.0,
        matchup_factors: Optional[Dict[str, float]] = None,
        sport: str = "nba",
        skip_props: bool = False,
    ) -> DFSProjection:
        """Generate DFS projection for a single player.

        Args:
            matchup_factors: DvP ratios from ``GameService.get_dvp_matchup_factors()``.
                             Scales each stat by the opponent's defensive
                             strength in that category.
        """
        minutes = projection.adjusted_minutes

        # Usage boost from teammate injuries (per-minute rate increase)
        usage_boost = getattr(projection, "usage_boost", 1.0) or 1.0

        # Opponent-adjusted floor/ceiling rate multipliers.
        # A player facing a bottom-tier defense should have a higher floor;
        # against a top defense, both floor and ceiling compress.
        floor_rate = FLOOR_RATE_MULT
        ceiling_rate = CEILING_RATE_MULT
        if matchup_factors:
            dvp_mean = sum(matchup_factors.values()) / len(matchup_factors)
            floor_rate = max(0.50, FLOOR_RATE_MULT + (dvp_mean - 1.0) * DVP_FLOOR_SENSITIVITY)
            ceiling_rate = min(1.60, CEILING_RATE_MULT + (dvp_mean - 1.0) * DVP_CEILING_SENSITIVITY)

        # Player-specific volatility adjustment: widen floor/ceiling spread
        # for high-variance players, tighten for consistent players.
        cv = getattr(player_minutes, "minutes_cv", PLAYER_CV_FALLBACK)
        cv_delta = cv - PLAYER_CV_BASELINE
        if abs(cv_delta) > 0.01:
            # Floor LOWERS for high-CV (wider downside), rises for low-CV
            floor_rate = max(0.45, floor_rate * (1.0 - cv_delta * PLAYER_CV_FLOOR_SCALE))
            # Ceiling RAISES for high-CV (wider upside), drops for low-CV
            ceiling_rate = min(1.70, ceiling_rate * (1.0 + cv_delta * PLAYER_CV_CEILING_SCALE))

        # Project stats at baseline, floor, and ceiling.
        # Use shot-type decomposition when the player has a shooting profile,
        # which makes the FG3M bonus naturally correlated with scoring volume.
        stats = self._project_stats_decomposed(
            minutes, player_minutes, pace_factor, matchup_factors,
            rate_multiplier=usage_boost, sport=sport,
        )

        # Blend stat projections toward DK Sportsbook market lines when
        # divergence exceeds the threshold.  Only applies to baseline stats
        # (floor/ceiling are derived from rate multipliers, not re-blended).
        # Skip for CBB and when skip_props is set (DK fallback mode).
        if sport != "cbb" and not skip_props:
            stats = self._apply_props_calibration(
                stats, projection.player_name,
                getattr(projection, "team_abbreviation", ""),
            )

        floor_stats = self._project_stats_decomposed(
            minutes * FLOOR_MINUTES_MULT, player_minutes, pace_factor,
            matchup_factors, rate_multiplier=floor_rate * usage_boost,
            sport=sport,
        )
        ceiling_stats = self._project_stats_decomposed(
            minutes * CEILING_MINUTES_MULT, player_minutes, pace_factor,
            matchup_factors, rate_multiplier=ceiling_rate * usage_boost,
            sport=sport,
        )

        # DK scoring with props-blended DD/TD probabilities
        if skip_props:
            dk = self._dk_score(stats)
        else:
            dk = self._dk_score_with_props(
                stats, projection.player_name,
                getattr(projection, "team_abbreviation", ""),
                sport=sport,
            )
        fd = self._fd_score(stats)

        return DFSProjection(
            player_id=projection.player_id,
            player_name=projection.player_name,
            position=projection.position,
            projected_minutes=minutes,
            pts=stats["pts"],
            reb=stats["reb"],
            ast=stats["ast"],
            stl=stats["stl"],
            blk=stats["blk"],
            tov=stats["tov"],
            fg3m=stats["fg3m"],
            dk_points=dk,
            fd_points=fd,
            dk_floor=self._dk_score(floor_stats),
            dk_ceiling=self._dk_score(ceiling_stats),
            fd_floor=self._fd_score(floor_stats),
            fd_ceiling=self._fd_score(ceiling_stats),
        )

    def project_team_dfs(
        self,
        team_rotation: TeamRotation,
        rotation: List[PlayerMinutes],
        matchup_factors: Optional[Dict[str, float]] = None,
        game_service=None,
        opponent_team_id: Optional[int] = None,
        sport: str = "nba",
        skip_props: bool = False,
    ) -> TeamDFSProjection:
        """Generate DFS projections for the full team rotation.

        Also attaches DFS fields directly onto each PlayerProjection
        in ``team_rotation.projections`` so the existing API response
        includes DFS data without a separate endpoint.

        Args:
            matchup_factors: Team-level DvP ratios (fallback).
            game_service: If provided with opponent_team_id, uses
                position-specific DvP for sharper matchup adjustments.
            opponent_team_id: Opponent team ID for position DvP lookup.
        """
        # Build a lookup of PlayerMinutes by player_id
        minutes_lookup: Dict[int, PlayerMinutes] = {
            p.player_id: p for p in rotation
        }

        dfs_projections: List[DFSProjection] = []

        for proj in team_rotation.projections:
            player_minutes = minutes_lookup.get(proj.player_id)
            if not player_minutes or proj.adjusted_minutes <= 0:
                # Injured / DNP — zero out DFS fields
                proj.dk_points = 0.0
                proj.fd_points = 0.0
                proj.dk_floor = 0.0
                proj.dk_ceiling = 0.0
                proj.fd_floor = 0.0
                proj.fd_ceiling = 0.0
                proj.projected_stats = {
                    "pts": 0, "reb": 0, "ast": 0,
                    "stl": 0, "blk": 0, "tov": 0, "fg3m": 0,
                }
                continue

            # Use position-specific DvP when available, else team-level
            player_matchup = matchup_factors
            if game_service and opponent_team_id:
                try:
                    player_matchup = game_service.get_position_dvp_factors(
                        opponent_team_id, proj.position,
                    )
                except Exception:
                    pass  # Fall back to team-level

            dfs = self.project_player_dfs(
                proj, player_minutes,
                pace_factor=proj.pace_factor,
                matchup_factors=player_matchup,
                sport=sport,
                skip_props=skip_props,
            )
            dfs_projections.append(dfs)

            # Attach DFS data to the existing PlayerProjection
            proj.dk_points = dfs.dk_points
            proj.fd_points = dfs.fd_points
            proj.dk_floor = dfs.dk_floor
            proj.dk_ceiling = dfs.dk_ceiling
            proj.fd_floor = dfs.fd_floor
            proj.fd_ceiling = dfs.fd_ceiling
            proj.projected_stats = {
                "pts": dfs.pts,
                "reb": dfs.reb,
                "ast": dfs.ast,
                "stl": dfs.stl,
                "blk": dfs.blk,
                "tov": dfs.tov,
                "fg3m": dfs.fg3m,
            }

        team_dk = round(sum(d.dk_points for d in dfs_projections), 1)
        team_fd = round(sum(d.fd_points for d in dfs_projections), 1)

        # Attach team totals to the TeamRotation object
        team_rotation.team_dk_total = team_dk
        team_rotation.team_fd_total = team_fd

        return TeamDFSProjection(
            team_id=team_rotation.team_id,
            team_name=team_rotation.team_name,
            game_date=team_rotation.game_date,
            projections=dfs_projections,
            team_dk_total=team_dk,
            team_fd_total=team_fd,
        )
