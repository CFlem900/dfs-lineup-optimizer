"""Player‑prop edge analyzer — compares Monte Carlo projections to book lines.

Given a list of player PRA (Points + Rebounds + Assists) over/under lines and
the corresponding simulation outputs (mean, std), this module calculates the
probability of going over each line and the resulting edge vs the book.

The edge is computed as:

    edge_pct = P(over) − implied_book_probability

where P(over) comes from the normal CDF of the simulation distribution and the
implied book probability is derived from the American odds (−110 ⇒ 52.38 %).

Usage
-----
::

    from app.services.prop_edge_analyzer import PropEdgeAnalyzer, PlayerPropLine

    lines = [
        PlayerPropLine(player_name="Luka Doncic",   line=48.5, over_odds=-110),
        PlayerPropLine(player_name="Shai Gilgeous-Alexander", line=42.5, over_odds=-115),
    ]

    sim_projections = {
        "Luka Doncic":                    {"mean_pra": 52.1, "std_pra": 9.3},
        "Shai Gilgeous-Alexander":        {"mean_pra": 46.8, "std_pra": 8.1},
    }

    analyzer = PropEdgeAnalyzer()
    df = analyzer.find_top_over_edges(lines, sim_projections, top_n=5)
    print(df.to_string(index=False))
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd
from scipy.stats import norm

logger = logging.getLogger(__name__)


# ── Data classes ──────────────────────────────────────────────────────────


@dataclass
class PlayerPropLine:
    """A single player O/U prop from the sportsbook.

    Attributes:
        player_name: Must match the name used in your simulation output.
        line:        The over/under number (e.g. 42.5 for PRA).
        over_odds:   American odds for the Over (e.g. -110).  Defaults to
                     -110 (standard juice).
        under_odds:  American odds for the Under.  Optional — only used if
                     you want to evaluate under edges too.
        stat_type:   Label for the prop market (default ``"PRA"``).
        dk_salary:   Optional DraftKings salary for additional context.
        team:        Optional team abbreviation.
        opponent:    Optional opponent abbreviation.
    """

    player_name: str
    line: float
    over_odds: int = -110
    under_odds: int = -110
    stat_type: str = "PRA"
    dk_salary: Optional[int] = None
    team: Optional[str] = None
    opponent: Optional[str] = None


@dataclass
class SimProjection:
    """Simulator output for a single player's PRA distribution.

    Can be built manually or extracted from ``PlayerSimResult``.
    """

    mean_pra: float
    std_pra: float
    mean_pts: float = 0.0
    mean_reb: float = 0.0
    mean_ast: float = 0.0
    num_sims: int = 10_000


# ── Helpers ───────────────────────────────────────────────────────────────


def american_odds_to_implied_prob(odds: int) -> float:
    """Convert American odds to implied probability (0‑1).

    Examples:
        -110 → 0.5238
        +150 → 0.4000
        -200 → 0.6667
    """
    if odds < 0:
        return abs(odds) / (abs(odds) + 100)
    else:
        return 100 / (odds + 100)


def prob_over(mean: float, std: float, line: float) -> float:
    """P(X > line) assuming X ~ N(mean, std²).

    Returns a value in [0, 1].  If std ≤ 0 the result is binary (0 or 1).
    """
    if std <= 0:
        return 1.0 if mean > line else 0.0
    return 1.0 - norm.cdf(line, loc=mean, scale=std)


def kelly_fraction(edge: float, odds: int, fraction: float = 0.25) -> float:
    """Quarter‑Kelly sizing for bankroll management.

    Args:
        edge:     Decimal edge (e.g. 0.08 for 8 %).
        odds:     American odds for the bet.
        fraction: Kelly fraction (default 0.25 = quarter‑Kelly).

    Returns:
        Recommended bet size as a fraction of bankroll (≥ 0).
    """
    if edge <= 0:
        return 0.0
    if odds < 0:
        decimal_odds = 1.0 + (100 / abs(odds))
    else:
        decimal_odds = 1.0 + (odds / 100)
    b = decimal_odds - 1.0  # net payout per unit
    p = american_odds_to_implied_prob(odds) + edge
    q = 1.0 - p
    if b <= 0:
        return 0.0
    kelly = (b * p - q) / b
    return max(0.0, kelly * fraction)


# ── Core analyzer ─────────────────────────────────────────────────────────


class PropEdgeAnalyzer:
    """Compare Monte Carlo simulation projections against sportsbook lines."""

    def __init__(self, min_edge_pct: float = 0.0, min_over_prob: float = 0.0):
        """
        Args:
            min_edge_pct:  Minimum edge % to include in results (0.0 = show all).
            min_over_prob: Minimum P(Over) to include (0.0 = show all).
        """
        self.min_edge_pct = min_edge_pct
        self.min_over_prob = min_over_prob

    # ── Public API ────────────────────────────────────────────────────

    def find_top_over_edges(
        self,
        prop_lines: Sequence[PlayerPropLine],
        sim_projections: Dict[str, SimProjection | dict],
        *,
        top_n: int = 5,
    ) -> pd.DataFrame:
        """Calculate edge for each prop and return the top *N* overs.

        Args:
            prop_lines:       List of book lines (one per player/prop).
            sim_projections:  Keyed by ``player_name``.  Values can be
                              ``SimProjection`` instances *or* plain dicts with
                              keys ``mean_pra`` and ``std_pra``.
            top_n:            Number of results to return.

        Returns:
            A ``DataFrame`` sorted by descending edge with columns:

            ========== ==============================================
            Column     Description
            ========== ==============================================
            player     Player name
            team       Team abbreviation (if provided)
            opponent   Opponent (if provided)
            stat_type  Market label (e.g. "PRA")
            book_line  The sportsbook O/U number
            over_odds  American odds on the over
            sim_mean   Simulator's projected mean
            sim_std    Simulator's projected std dev
            p_over     P(Over) from the normal CDF
            implied    Book's implied probability
            edge_pct   ``p_over − implied`` (decimal, e.g. 0.08)
            edge_display  Edge formatted as ``"+8.0%"``
            kelly_frac Quarter‑Kelly bet sizing
            confidence "High" / "Medium" / "Low"
            ========== ==============================================
        """
        rows: List[dict] = []

        for prop in prop_lines:
            proj = sim_projections.get(prop.player_name)
            if proj is None:
                logger.warning(
                    f"[PropEdge] No simulation data for {prop.player_name!r} — skipping"
                )
                continue

            mean, std = self._extract_mean_std(proj)
            if mean is None:
                continue

            p = prob_over(mean, std, prop.line)
            implied = american_odds_to_implied_prob(prop.over_odds)
            edge = p - implied

            if edge < self.min_edge_pct:
                continue
            if p < self.min_over_prob:
                continue

            rows.append(
                {
                    "player": prop.player_name,
                    "team": prop.team or "",
                    "opponent": prop.opponent or "",
                    "stat_type": prop.stat_type,
                    "book_line": prop.line,
                    "over_odds": prop.over_odds,
                    "dk_salary": prop.dk_salary,
                    "sim_mean": round(mean, 1),
                    "sim_std": round(std, 1),
                    "p_over": round(p, 4),
                    "implied": round(implied, 4),
                    "edge_pct": round(edge, 4),
                    "edge_display": f"{'+' if edge >= 0 else ''}{edge * 100:.1f}%",
                    "kelly_frac": round(kelly_fraction(edge, prop.over_odds), 4),
                    "confidence": self._confidence_label(edge, std, mean, prop.line),
                }
            )

        if not rows:
            logger.info("[PropEdge] No props matched the filter criteria.")
            return pd.DataFrame()

        df = pd.DataFrame(rows).sort_values("edge_pct", ascending=False)
        return df.head(top_n).reset_index(drop=True)

    def analyze_all(
        self,
        prop_lines: Sequence[PlayerPropLine],
        sim_projections: Dict[str, SimProjection | dict],
    ) -> pd.DataFrame:
        """Return edge analysis for *all* props (no top‑N or edge filter)."""
        # Temporarily remove filters to show everything
        saved_edge, saved_prob = self.min_edge_pct, self.min_over_prob
        self.min_edge_pct = -float("inf")
        self.min_over_prob = 0.0
        try:
            return self.find_top_over_edges(
                prop_lines, sim_projections, top_n=len(prop_lines)
            )
        finally:
            self.min_edge_pct, self.min_over_prob = saved_edge, saved_prob

    def find_edges_with_blowout_adjustment(
        self,
        prop_lines: Sequence[PlayerPropLine],
        sim_projections: Dict[str, SimProjection | dict],
        blowout_analyzer,
        game_date: str,
        *,
        top_n: int = 10,
    ) -> pd.DataFrame:
        """End-to-end pipeline: compute edges, then adjust for blowout risk.

        Chains ``find_top_over_edges()`` → ``BlowoutRiskAnalyzer.adjust_edges()``
        so the caller gets a single DataFrame with both raw and adjusted columns.

        Args:
            prop_lines:       DK player prop lines.
            sim_projections:  Simulation output (keyed by player name).
            blowout_analyzer: ``BlowoutRiskAnalyzer`` instance.
            game_date:        ``"YYYY-MM-DD"`` for tonight's games.
            top_n:            How many raw edges to evaluate (before adjustment).

        Returns:
            DataFrame with original edge columns **plus** adjusted columns:
            ``blowout_risk``, ``minutes_factor``, ``adj_sim_mean``,
            ``adj_p_over``, ``adj_edge_pct``, ``adj_kelly_frac``, etc.
            Sorted by ``adj_edge_pct`` descending.
        """
        # Step 1: Raw edge analysis
        raw_df = self.find_top_over_edges(prop_lines, sim_projections, top_n=top_n)
        if raw_df.empty:
            return raw_df

        # Step 2: Assess blowout risk for all games tonight
        assessments = blowout_analyzer.assess_games(game_date)

        # Step 3: Apply minutes deduction + recalculate edges
        return blowout_analyzer.adjust_edges(raw_df, assessments)

    # ── Build from SimulationEngine output ────────────────────────────

    @staticmethod
    def projections_from_sim_results(
        player_sim_results: Sequence,
    ) -> Dict[str, SimProjection]:
        """Convert a list of ``PlayerSimResult`` objects into a projection dict.

        This bridges the gap between your SimulationEngine output and the
        analyzer's input format.  PRA = PTS + REB + AST; the standard
        deviation is approximated via quadrature (√(σ² + σ² + σ²)) assuming
        independence (conservative; real correlation is slightly positive).

        Args:
            player_sim_results: Iterable of ``PlayerSimResult`` (from
                ``simulation.py``).

        Returns:
            Dict mapping player name → ``SimProjection``.
        """
        projections: Dict[str, SimProjection] = {}
        for psr in player_sim_results:
            mean_pra = psr.mean_pts + psr.mean_reb + psr.mean_ast
            # Approximate PRA std via quadrature (assumes independence)
            std_pra = float(
                np.sqrt(psr.std_pts**2 + psr.std_reb**2 + psr.std_ast**2)
            )
            projections[psr.player_name] = SimProjection(
                mean_pra=round(mean_pra, 2),
                std_pra=round(std_pra, 2),
                mean_pts=psr.mean_pts,
                mean_reb=psr.mean_reb,
                mean_ast=psr.mean_ast,
            )
        return projections

    # ── Internals ─────────────────────────────────────────────────────

    @staticmethod
    def _extract_mean_std(proj) -> tuple:
        """Extract (mean, std) from a SimProjection or plain dict."""
        if isinstance(proj, SimProjection):
            return proj.mean_pra, proj.std_pra
        if isinstance(proj, dict):
            mean = proj.get("mean_pra")
            std = proj.get("std_pra")
            if mean is not None and std is not None:
                return float(mean), float(std)
        return None, None

    async def enrich_with_bdl_ids(
        self,
        prop_lines: Sequence[PlayerPropLine],
        player_id_mapper,
    ) -> Dict[str, int]:
        """Resolve BDL player IDs for a list of DK prop lines.

        Returns a dict mapping ``player_name`` → ``bdl_id`` for every player
        that could be matched.  Unmatched players are omitted (logged as
        warnings by the mapper).

        Usage::

            from app.api.dependencies import get_services
            svc = get_services()
            bdl_ids = await analyzer.enrich_with_bdl_ids(
                prop_lines, svc.player_id_mapper,
            )
            # bdl_ids = {"Luka Doncic": 666, "Shai Gilgeous-Alexander": 300, ...}
        """
        from app.services.player_id_mapper import DKPlayerInput

        dk_inputs = [
            DKPlayerInput(name=p.player_name, team=p.team or "")
            for p in prop_lines
            if p.team
        ]
        if not dk_inputs:
            logger.warning("[PropEdge] No prop lines have team info — cannot map to BDL IDs")
            return {}

        mapping = await player_id_mapper.build_mapping(dk_inputs)
        return {name: pm.bdl_id for name, pm in mapping.items()}

    @staticmethod
    def _confidence_label(
        edge: float, std: float, mean: float, line: float
    ) -> str:
        """Heuristic confidence tier based on edge size and projection gap."""
        diff = mean - line
        if std > 0:
            z_score = diff / std
        else:
            z_score = 10.0 if diff > 0 else -10.0

        if edge >= 0.08 and z_score >= 0.5:
            return "High"
        elif edge >= 0.04 and z_score >= 0.25:
            return "Medium"
        else:
            return "Low"


# ── ContextualRefiner ────────────────────────────────────────────────────
#
# Derives blowout risk from the BallDontLie MCP game data (win %,
# average margin) and applies a minutes multiplier to the RotationEngine
# projection pipeline.
#
# The BDL MCP has NO standings or team‑stats endpoint — only:
#   get_teams, get_players, get_games, get_game.
#
# Win percentage and average margin are therefore computed directly from
# completed‑game results (same approach as BlowoutRiskAnalyzer._build_team_form).
# "Net Rating" is approximated by average point differential per game over
# the same window, which tracks net rating closely at the team level.
# ─────────────────────────────────────────────────────────────────────────

# Tuning constants (ContextualRefiner)
CR_WIN_PCT_GAP_THRESHOLD: float = 0.300    # ΔWin% required for "High" risk
CR_FAVORITE_MARGIN_THRESHOLD: float = 8.0  # Avg margin for favorite must be > +8.0
CR_HIGH_RISK_MINUTES_FACTOR: float = 0.85  # 15 % reduction when risk is "High"
CR_RECENT_GAMES_WINDOW: int = 5            # Last 5 games for average margin
CR_SEASON_LOOKBACK_DAYS: int = 60          # ~30 games — proxy for "season" record

# Tuning constants (GarbageTimeOpportunity)
GT_LOOKBACK_GAMES: int = 10                    # Last N team games to analyse
GT_BLOWOUT_MARGIN: float = 15.0                # |margin| > this = blowout game
GT_MAX_AVG_MINUTES: float = 18.0               # Bench threshold: avg < 18 min
GT_USAGE_SPIKE_THRESHOLD: float = 1.15         # ≥ 15 % usage increase required
GT_SPECIALIST_MINUTES_FACTOR: float = 1.20     # 20 % boost (replaces 0.85×)
GT_MIN_BLOWOUT_GAMES: int = 2                  # Minimum blowout games to qualify
GT_MIN_GAMES_PLAYED: int = 5                   # Minimum total games with data


@dataclass
class AdjustmentResult:
    """Result of a ``ContextualRefiner.calculate_blowout_risk()`` call.

    Captures the original edge, the blowout‑risk assessment, and the
    new edge after applying the minutes multiplier.

    Attributes:
        home_team:         Home team abbreviation.
        visitor_team:      Visitor team abbreviation.
        original_edge:     The raw edge (decimal, e.g. 0.08 for 8 %).
        risk_score:        ``"High"``, ``"Medium"``, or ``"Low"``.
        win_pct_gap:       Absolute difference in season win percentages.
        favorite_avg_margin: The favored team's recent average margin.
        minutes_factor:    Multiplier applied (0.85 when High, 1.0 otherwise).
        adjusted_edge:     The edge after scaling the sim projection by
                           ``minutes_factor`` and recalculating P(Over).
        favored_team:      Abbreviation of the favored team.
        home_win_pct:      Season win percentage for home team.
        visitor_win_pct:   Season win percentage for visitor team.
        home_avg_margin:   Home team's recent avg margin (approx net rating).
        visitor_avg_margin: Visitor team's recent avg margin.
    """

    home_team: str = ""
    visitor_team: str = ""
    original_edge: float = 0.0
    risk_score: str = "Low"           # "High", "Medium", "Low"
    win_pct_gap: float = 0.0
    favorite_avg_margin: float = 0.0
    minutes_factor: float = 1.0
    adjusted_edge: float = 0.0
    favored_team: str = ""
    home_win_pct: float = 0.0
    visitor_win_pct: float = 0.0
    home_avg_margin: float = 0.0
    visitor_avg_margin: float = 0.0


@dataclass
class _TeamContext:
    """Internal snapshot used by ContextualRefiner."""

    team_abbr: str = ""
    bdl_team_id: int = 0
    wins: int = 0
    losses: int = 0
    recent_avg_margin: float = 0.0   # avg point differential (last N games)

    @property
    def win_pct(self) -> float:
        total = self.wins + self.losses
        return self.wins / total if total > 0 else 0.5


class ContextualRefiner:
    """Derives blowout risk from BDL MCP game data and adjusts edges.

    Uses the MCP ``get_games`` tool (the only endpoint that returns
    scores) to compute season win percentage and recent average margin
    for both teams.  A ``"High"`` blowout risk is detected when:

        1. Win % gap > 0.300  **AND**
        2. The favorite's recent avg margin > +8.0

    When the risk is ``"High"``, a ``0.85×`` multiplier is applied to
    the projected minutes in the RotationEngine (via ``minutes_factor``
    on the returned ``AdjustmentResult``).

    The class is designed for integration with ``PropEdgeAnalyzer``:
    call ``calculate_blowout_risk()`` per‑game, then use the returned
    ``AdjustmentResult.minutes_factor`` to scale sim projections before
    (or after) computing over‑edges.

    Parameters
    ----------
    bdl_mcp_client : BDLMCPClient
        Typed wrapper around the BDL MCP tools.  Used exclusively for
        ``get_teams()`` and ``get_games()`` — no REST‑only endpoints.
    win_pct_gap_threshold : float
        Minimum ΔWin% to qualify for "High" risk (default 0.300).
    favorite_margin_threshold : float
        Minimum average margin for the favorite (default +8.0).
    high_risk_minutes_factor : float
        Minutes multiplier applied when risk is "High" (default 0.85).
    """

    def __init__(
        self,
        bdl_mcp_client,
        *,
        win_pct_gap_threshold: float = CR_WIN_PCT_GAP_THRESHOLD,
        favorite_margin_threshold: float = CR_FAVORITE_MARGIN_THRESHOLD,
        high_risk_minutes_factor: float = CR_HIGH_RISK_MINUTES_FACTOR,
    ):
        self._client = bdl_mcp_client
        self._win_pct_gap = win_pct_gap_threshold
        self._margin_thresh = favorite_margin_threshold
        self._high_factor = high_risk_minutes_factor

        # Lazy‑loaded BDL team mapping: abbreviation → bdl_team_id
        self._team_map: Dict[str, int] = {}

    @property
    def is_available(self) -> bool:
        """True if the underlying MCP client can serve requests."""
        return bool(self._client and getattr(self._client, "is_available", False))

    # ── Public API ────────────────────────────────────────────────────

    def calculate_blowout_risk(
        self,
        home_team_id: int,
        visitor_team_id: int,
        *,
        original_edge: float = 0.0,
        sim_mean: float = 0.0,
        sim_std: float = 0.0,
        book_line: float = 0.0,
        over_odds: int = -110,
        game_date: Optional[str] = None,
    ) -> AdjustmentResult:
        """Assess blowout risk for a game and return an adjusted edge.

        Uses the BDL MCP ``get_games`` tool to derive win percentage and
        recent average margin for both teams, then applies the risk logic.

        Parameters
        ----------
        home_team_id : int
            BDL team ID for the home team.
        visitor_team_id : int
            BDL team ID for the visitor team.
        original_edge : float
            Raw edge from ``PropEdgeAnalyzer`` (decimal, e.g. 0.08).
        sim_mean : float
            Simulation mean PRA (used for edge recalculation).
        sim_std : float
            Simulation std PRA.
        book_line : float
            The sportsbook O/U line.
        over_odds : int
            American odds on the over (default -110).
        game_date : str, optional
            ``"YYYY-MM-DD"`` — defaults to today.

        Returns
        -------
        AdjustmentResult
            Contains the original edge, risk assessment, minutes factor,
            and the recalculated edge after scaling.
        """
        gd = game_date or date.today().isoformat()

        # ── Step 1: Build team context from game data ────────────────
        home_ctx = self._build_context(home_team_id, gd)
        visitor_ctx = self._build_context(visitor_team_id, gd)

        # ── Step 2: Compute signals ──────────────────────────────────
        win_gap = abs(home_ctx.win_pct - visitor_ctx.win_pct)

        # Identify the favorite (higher win %)
        if home_ctx.win_pct >= visitor_ctx.win_pct:
            fav_ctx, fav_abbr = home_ctx, home_ctx.team_abbr
        else:
            fav_ctx, fav_abbr = visitor_ctx, visitor_ctx.team_abbr

        fav_margin = fav_ctx.recent_avg_margin

        # ── Step 3: Risk scoring ─────────────────────────────────────
        if win_gap > self._win_pct_gap and fav_margin > self._margin_thresh:
            risk_label = "High"
            minutes_factor = self._high_factor
        elif win_gap > self._win_pct_gap * 0.6 or fav_margin > self._margin_thresh * 0.75:
            risk_label = "Medium"
            minutes_factor = 1.0  # no penalty, advisory only
        else:
            risk_label = "Low"
            minutes_factor = 1.0

        # ── Step 4: Recalculate edge with adjusted projection ────────
        adj_mean = sim_mean * minutes_factor
        adj_std = sim_std * minutes_factor

        if adj_mean > 0 and adj_std > 0 and book_line > 0:
            p = prob_over(adj_mean, adj_std, book_line)
            implied = american_odds_to_implied_prob(over_odds)
            adjusted_edge = round(p - implied, 4)
        else:
            # No projection data — return original edge scaled by factor
            adjusted_edge = round(original_edge * minutes_factor, 4)

        return AdjustmentResult(
            home_team=home_ctx.team_abbr,
            visitor_team=visitor_ctx.team_abbr,
            original_edge=round(original_edge, 4),
            risk_score=risk_label,
            win_pct_gap=round(win_gap, 4),
            favorite_avg_margin=round(fav_margin, 1),
            minutes_factor=minutes_factor,
            adjusted_edge=adjusted_edge,
            favored_team=fav_abbr,
            home_win_pct=round(home_ctx.win_pct, 4),
            visitor_win_pct=round(visitor_ctx.win_pct, 4),
            home_avg_margin=round(home_ctx.recent_avg_margin, 1),
            visitor_avg_margin=round(visitor_ctx.recent_avg_margin, 1),
        )

    def calculate_blowout_risk_by_abbr(
        self,
        home_abbr: str,
        visitor_abbr: str,
        **kwargs,
    ) -> AdjustmentResult:
        """Convenience wrapper accepting team abbreviations instead of IDs.

        Resolves abbreviations to BDL team IDs via the MCP ``get_teams``
        tool, then delegates to ``calculate_blowout_risk()``.

        Args:
            home_abbr:    Home team abbreviation (e.g. ``"OKC"``).
            visitor_abbr: Visitor team abbreviation (e.g. ``"DET"``).
            **kwargs:     Forwarded to ``calculate_blowout_risk()``.

        Returns:
            AdjustmentResult — same as ``calculate_blowout_risk()``.
        """
        self._ensure_team_map()
        home_id = self._team_map.get(home_abbr.upper(), 0)
        visitor_id = self._team_map.get(visitor_abbr.upper(), 0)

        if not home_id or not visitor_id:
            logger.warning(
                f"[ContextualRefiner] Unknown team abbreviation: "
                f"home={home_abbr!r}→{home_id}, visitor={visitor_abbr!r}→{visitor_id}"
            )
            return AdjustmentResult(
                home_team=home_abbr.upper(),
                visitor_team=visitor_abbr.upper(),
                original_edge=kwargs.get("original_edge", 0.0),
                risk_score="Low",
                minutes_factor=1.0,
                adjusted_edge=kwargs.get("original_edge", 0.0),
            )

        return self.calculate_blowout_risk(
            home_team_id=home_id,
            visitor_team_id=visitor_id,
            **kwargs,
        )

    # ── Internals ─────────────────────────────────────────────────────

    def _ensure_team_map(self) -> None:
        """Populate abbreviation → BDL team ID mapping from MCP get_teams."""
        if self._team_map:
            return
        try:
            teams = self._client.get_teams(league="NBA")
            for t in teams:
                if t.abbreviation:
                    self._team_map[t.abbreviation.upper()] = t.id
            logger.info(
                f"[ContextualRefiner] Loaded {len(self._team_map)} team mappings"
            )
        except Exception as e:
            logger.error(f"[ContextualRefiner] Failed to load team map: {e}")

    def _build_context(
        self, bdl_team_id: int, game_date: str
    ) -> _TeamContext:
        """Build win % and recent avg margin from MCP game data.

        Fetches completed games for the team across the current season
        using ``BDLMCPClient.get_games(season=…, team_id=…)``.

        The BDL MCP has no standings or stats endpoint, so win % is
        derived from game-by-game results and average margin (a close
        proxy for net rating) is the mean point differential over the
        last ``CR_RECENT_GAMES_WINDOW`` games.

        Falls back to neutral defaults (0.500 win %, 0.0 margin) if
        the data is unavailable to prevent the refiner from breaking
        the pipeline.
        """
        # Resolve team abbreviation for the result
        abbr = self._resolve_abbr(bdl_team_id)

        default = _TeamContext(team_abbr=abbr, bdl_team_id=bdl_team_id)

        if not self.is_available:
            logger.debug(
                "[ContextualRefiner] MCP not available — returning default context"
            )
            return default

        try:
            # Determine season year from game_date.
            # BDL uses the starting year: 2025-26 season → season=2025.
            gd = date.fromisoformat(game_date)
            season_year = gd.year if gd.month >= 10 else gd.year - 1

            games = self._client.get_games(
                league="NBA",
                season=season_year,
                team_id=bdl_team_id,
            )
        except Exception as e:
            logger.warning(
                f"[ContextualRefiner] Failed to fetch games for team "
                f"{bdl_team_id}: {e}"
            )
            return default

        if not games:
            return default

        # Filter to completed games only, build margin list
        margins: List[Tuple[str, float]] = []  # (date, margin)
        for g in games:
            if g.status != "Final":
                continue
            h_score = g.home_team_score or 0
            v_score = g.visitor_team_score or 0
            if h_score == 0 and v_score == 0:
                continue  # No score data

            # Margin is from this team's perspective
            if g.home_team and g.home_team.id == bdl_team_id:
                margin = h_score - v_score
            elif g.visitor_team and g.visitor_team.id == bdl_team_id:
                margin = v_score - h_score
            else:
                continue

            game_date_str = g.date or ""
            margins.append((game_date_str, margin))

        if not margins:
            return default

        # Sort by date descending (most recent first)
        margins.sort(key=lambda x: x[0], reverse=True)

        # Season record from all games
        season_wins = sum(1 for _, m in margins if m > 0)
        season_losses = len(margins) - season_wins

        # Recent avg margin (last N games)
        recent = margins[:CR_RECENT_GAMES_WINDOW]
        recent_margins = [m for _, m in recent]
        avg_margin = float(np.mean(recent_margins)) if recent_margins else 0.0

        return _TeamContext(
            team_abbr=abbr,
            bdl_team_id=bdl_team_id,
            wins=season_wins,
            losses=season_losses,
            recent_avg_margin=round(avg_margin, 1),
        )

    def _resolve_abbr(self, bdl_team_id: int) -> str:
        """Resolve a BDL team ID to its abbreviation."""
        self._ensure_team_map()
        for abbr, tid in self._team_map.items():
            if tid == bdl_team_id:
                return abbr
        return f"T{bdl_team_id}"


# ── GarbageTimeOpportunity ───────────────────────────────────────────────
#
# Detects bench players whose per‑minute usage spikes in blowout games
# ("garbage time specialists").  When the ContextualRefiner flags a game
# as "High" blowout risk, these players get a 1.20× minutes boost
# (replacing the standard 0.85× reduction), making their low‑volume
# prop overs significantly more valuable.
#
# Data sources:
#   • BDL MCP  get_games()   → identify blowout games by score margin
#   • BDL MCP  get_players() → fetch team rosters (BDL IDs)
#   • BDL REST get_player_stats() → per‑game box scores (MIN, FGA, FTA, TOV)
#
# Usage‑rate proxy (BDL has no per‑game USG_PCT):
#   usage_proxy = (FGA + 0.44 × FTA + TOV) / MIN
# ─────────────────────────────────────────────────────────────────────────


@dataclass
class GarbageTimePivot:
    """A player identified as a garbage‑time specialist.

    These players average fewer than ``GT_MAX_AVG_MINUTES`` (18) and show
    a usage spike of ≥ 15 % when the team's point differential exceeds 15.
    In "High" blowout‑risk games they receive a 1.20× minutes boost
    instead of the standard 0.85× reduction.
    """

    player_name: str = ""
    team: str = ""
    bdl_player_id: int = 0
    avg_minutes: float = 0.0
    normal_usage_proxy: float = 0.0
    blowout_usage_proxy: float = 0.0
    usage_spike_pct: float = 0.0        # blowout / normal ratio
    blowout_games_count: int = 0
    normal_games_count: int = 0
    minutes_factor: float = GT_SPECIALIST_MINUTES_FACTOR
    confidence: str = "Medium"           # "High" if ≥ 3 blowout games


@dataclass
class GarbageTimeResult:
    """Analysis result for garbage‑time specialist detection on one team."""

    team: str = ""
    opponent: str = ""
    blowout_risk: str = "Low"
    pivots: List[GarbageTimePivot] = field(default_factory=list)
    games_analyzed: int = 0
    blowout_games_found: int = 0
    analysis_date: str = ""


class GarbageTimeOpportunity:
    """Detects garbage‑time specialists and boosts their projections.

    Scans the last ``GT_LOOKBACK_GAMES`` team games via the BDL MCP to
    classify each as a blowout (|margin| > 15) or competitive.  Then
    fetches per‑game box scores via the BDL REST API to compute a
    usage proxy in each context.  Players who average < 18 minutes and
    show a ≥ 15 % usage spike in blowouts are flagged as specialists.

    When the ``ContextualRefiner`` rates a matchup as ``"High"`` blowout
    risk, specialists receive a 1.20× minutes factor (replacing the
    default 0.85×).  The ``adjust_edges_for_pivots`` method patches the
    edge DataFrame from ``PropEdgeAnalyzer`` accordingly.

    Parameters
    ----------
    bdl_mcp_client : BDLMCPClient
        Used for ``get_teams()``, ``get_players()``, ``get_games()``.
    bdl_service : BallDontLieService
        Used for ``get_player_stats()`` (REST — not available via MCP).
    contextual_refiner : ContextualRefiner, optional
        Provides the blowout‑risk flag for tonight's matchups.
    """

    def __init__(
        self,
        bdl_mcp_client,
        bdl_service,
        contextual_refiner=None,
    ):
        self._mcp = bdl_mcp_client
        self._bdl = bdl_service
        self._refiner = contextual_refiner
        self._team_map: Dict[str, int] = {}

    @property
    def is_available(self) -> bool:
        """True if both MCP and REST BDL services can serve requests."""
        mcp_ok = bool(self._mcp and getattr(self._mcp, "is_available", False))
        bdl_ok = bool(self._bdl and getattr(self._bdl, "is_available", False))
        return mcp_ok and bdl_ok

    # ── Public API ────────────────────────────────────────────────────

    def find_garbage_time_pivots(
        self,
        team_abbr: str,
        opponent_abbr: str,
        *,
        game_date: Optional[str] = None,
        blowout_risk_override: Optional[str] = None,
    ) -> GarbageTimeResult:
        """Identify garbage‑time specialists for a team in a matchup.

        Parameters
        ----------
        team_abbr : str
            Abbreviation of the team to analyse (e.g. ``"WAS"``).
        opponent_abbr : str
            Abbreviation of the opponent (e.g. ``"OKC"``).
        game_date : str, optional
            ``"YYYY-MM-DD"`` — defaults to today.
        blowout_risk_override : str, optional
            Skip the ContextualRefiner and use this risk level directly.

        Returns
        -------
        GarbageTimeResult
        """
        gd = game_date or date.today().isoformat()
        team_upper = team_abbr.upper()
        opp_upper = opponent_abbr.upper()

        # ── Step 1: Determine blowout risk ─────────────────────────
        risk = self._resolve_blowout_risk(team_upper, opp_upper, gd,
                                          blowout_risk_override)

        empty = GarbageTimeResult(
            team=team_upper,
            opponent=opp_upper,
            blowout_risk=risk,
            analysis_date=gd,
        )

        if risk != "High":
            return empty

        if not self.is_available:
            logger.debug(
                "[GarbageTime] Services not available — skipping analysis"
            )
            return empty

        # ── Step 2: Identify blowout games ─────────────────────────
        self._ensure_team_map()
        bdl_team_id = self._team_map.get(team_upper, 0)
        if not bdl_team_id:
            logger.warning(
                f"[GarbageTime] Unknown team abbreviation: {team_upper!r}"
            )
            return empty

        blowout_dates, competitive_dates, total = self._identify_blowout_games(
            bdl_team_id, gd,
        )

        if len(blowout_dates) < GT_MIN_BLOWOUT_GAMES:
            logger.debug(
                f"[GarbageTime] Only {len(blowout_dates)} blowout games "
                f"for {team_upper} (need {GT_MIN_BLOWOUT_GAMES}) — skipping"
            )
            empty.games_analyzed = total
            empty.blowout_games_found = len(blowout_dates)
            return empty

        # ── Step 3: Fetch roster + per‑game stats ──────────────────
        pivots = self._compute_player_usage_splits(
            team_abbr=team_upper,
            bdl_team_id=bdl_team_id,
            gd=gd,
            blowout_dates=blowout_dates,
            competitive_dates=competitive_dates,
        )

        logger.info(
            f"[GarbageTime] {team_upper}: {len(pivots)} specialist(s) from "
            f"{total} games ({len(blowout_dates)} blowouts)"
        )

        return GarbageTimeResult(
            team=team_upper,
            opponent=opp_upper,
            blowout_risk=risk,
            pivots=pivots,
            games_analyzed=total,
            blowout_games_found=len(blowout_dates),
            analysis_date=gd,
        )

    def adjust_edges_for_pivots(
        self,
        edge_df: pd.DataFrame,
        pivots: List[GarbageTimePivot],
    ) -> pd.DataFrame:
        """Apply 1.20× minutes boost to pivot players in an edge DataFrame.

        Non‑pivot players are left unchanged.  New columns added:
        ``is_gt_pivot`` (bool) and ``gt_usage_spike`` (float).

        Parameters
        ----------
        edge_df : pd.DataFrame
            Edge table from ``PropEdgeAnalyzer`` (must have ``player``,
            ``sim_mean``, ``sim_std``, ``book_line``, ``over_odds``).
        pivots : list[GarbageTimePivot]
            Specialists identified by ``find_garbage_time_pivots()``.

        Returns
        -------
        pd.DataFrame
            Copy of *edge_df* with adjusted columns for pivot players.
        """
        if edge_df.empty or not pivots:
            df = edge_df.copy()
            df["is_gt_pivot"] = False
            df["gt_usage_spike"] = 0.0
            return df

        pivot_map = {p.player_name.lower(): p for p in pivots}
        df = edge_df.copy()
        df["is_gt_pivot"] = False
        df["gt_usage_spike"] = 0.0

        for idx, row in df.iterrows():
            player_key = str(row.get("player", "")).lower()
            pivot = pivot_map.get(player_key)
            if pivot is None:
                continue

            df.at[idx, "is_gt_pivot"] = True
            df.at[idx, "gt_usage_spike"] = round(pivot.usage_spike_pct, 3)

            # Override minutes factor to 1.2× (was 0.85× for blowout)
            factor = pivot.minutes_factor
            sim_mean = float(row.get("sim_mean", 0))
            sim_std = float(row.get("sim_std", 0))
            book_line = float(row.get("book_line", 0))
            over_odds = int(row.get("over_odds", -110))

            adj_mean = sim_mean * factor
            adj_std = sim_std * factor

            if adj_mean > 0 and adj_std > 0 and book_line > 0:
                p_over = prob_over(adj_mean, adj_std, book_line)
                implied = american_odds_to_implied_prob(over_odds)
                adj_edge = round(p_over - implied, 4)
            else:
                adj_edge = round(
                    float(row.get("edge_pct", 0)) * factor, 4
                )
                p_over = float(row.get("p_over", 0))

            if "minutes_factor" in df.columns:
                df.at[idx, "minutes_factor"] = factor
            if "adj_sim_mean" in df.columns:
                df.at[idx, "adj_sim_mean"] = round(adj_mean, 2)
            if "adj_sim_std" in df.columns:
                df.at[idx, "adj_sim_std"] = round(adj_std, 2)
            if "adj_p_over" in df.columns:
                df.at[idx, "adj_p_over"] = round(p_over, 4)
            if "adj_edge_pct" in df.columns:
                df.at[idx, "adj_edge_pct"] = adj_edge

        return df

    def find_slate_pivots(
        self,
        game_date: str,
        matchups: List[Tuple[str, str]],
    ) -> Tuple[List[GarbageTimeResult], List[GarbageTimePivot]]:
        """Scan all slate matchups and return every pivot player.

        Parameters
        ----------
        game_date : str
            ``"YYYY-MM-DD"`` for the slate.
        matchups : list[tuple[str, str]]
            ``[(home_abbr, away_abbr), ...]`` for each game.

        Returns
        -------
        (all_results, all_pivots)
        """
        all_results: List[GarbageTimeResult] = []
        all_pivots: List[GarbageTimePivot] = []

        for home, away in matchups:
            for team, opp in [(home, away), (away, home)]:
                result = self.find_garbage_time_pivots(
                    team, opp, game_date=game_date,
                )
                all_results.append(result)
                all_pivots.extend(result.pivots)

        logger.info(
            f"[GarbageTime] Slate scan: {len(all_pivots)} total pivot(s) "
            f"across {len(matchups)} matchups"
        )
        return all_results, all_pivots

    # ── Internals ─────────────────────────────────────────────────────

    def _resolve_blowout_risk(
        self,
        team_abbr: str,
        opponent_abbr: str,
        game_date: str,
        override: Optional[str],
    ) -> str:
        """Get blowout risk from override or ContextualRefiner."""
        if override:
            return override

        if self._refiner and self._refiner.is_available:
            try:
                result = self._refiner.calculate_blowout_risk_by_abbr(
                    opponent_abbr, team_abbr, game_date=game_date,
                )
                return result.risk_score
            except Exception as e:
                logger.warning(
                    f"[GarbageTime] ContextualRefiner failed: {e}"
                )

        return "Low"

    def _ensure_team_map(self) -> None:
        """Cache abbreviation → BDL team ID mapping."""
        if self._team_map:
            return
        try:
            teams = self._mcp.get_teams(league="NBA")
            for t in teams:
                if t.abbreviation:
                    self._team_map[t.abbreviation.upper()] = t.id
        except Exception as e:
            logger.error(f"[GarbageTime] Failed to load team map: {e}")

    def _identify_blowout_games(
        self,
        bdl_team_id: int,
        game_date: str,
    ) -> Tuple[set, set, int]:
        """Classify recent team games as blowout vs competitive.

        Returns
        -------
        (blowout_dates, competitive_dates, total_games_analyzed)
            Sets of ``"YYYY-MM-DD"`` date strings.
        """
        gd = date.fromisoformat(game_date)
        season_year = gd.year if gd.month >= 10 else gd.year - 1

        try:
            games = self._mcp.get_games(
                league="NBA", season=season_year, team_id=bdl_team_id,
            )
        except Exception as e:
            logger.warning(
                f"[GarbageTime] get_games failed for team {bdl_team_id}: {e}"
            )
            return set(), set(), 0

        # Filter to completed games, sort by date desc, take last N
        completed = []
        for g in games:
            if g.status != "Final":
                continue
            h = g.home_team_score or 0
            v = g.visitor_team_score or 0
            if h == 0 and v == 0:
                continue
            if g.home_team and g.home_team.id == bdl_team_id:
                margin = h - v
            elif g.visitor_team and g.visitor_team.id == bdl_team_id:
                margin = v - h
            else:
                continue
            completed.append((g.date or "", margin))

        completed.sort(key=lambda x: x[0], reverse=True)
        recent = completed[:GT_LOOKBACK_GAMES]

        blowout_dates: set = set()
        competitive_dates: set = set()
        for dt, margin in recent:
            if abs(margin) > GT_BLOWOUT_MARGIN:
                blowout_dates.add(dt[:10])
            else:
                competitive_dates.add(dt[:10])

        return blowout_dates, competitive_dates, len(recent)

    def _compute_player_usage_splits(
        self,
        team_abbr: str,
        bdl_team_id: int,
        gd: str,
        blowout_dates: set,
        competitive_dates: set,
    ) -> List[GarbageTimePivot]:
        """Fetch per‑game stats and flag garbage‑time specialists."""
        # Get roster via MCP
        try:
            roster = self._mcp.get_players(league="NBA", team_abbr=team_abbr)
        except Exception as e:
            logger.warning(f"[GarbageTime] get_players failed: {e}")
            return []

        if not roster:
            return []

        bdl_ids = [p.id for p in roster]

        # Fetch per-game stats via BDL REST
        gd_parsed = date.fromisoformat(gd)
        season_year = gd_parsed.year if gd_parsed.month >= 10 else gd_parsed.year - 1
        try:
            all_stats = self._bdl.get_player_stats(bdl_ids, season_year)
        except Exception as e:
            logger.warning(f"[GarbageTime] get_player_stats failed: {e}")
            return []

        # Index stats by BDL player ID
        stats_by_player: Dict[int, List[dict]] = {}
        for s in all_stats:
            pid = s.get("player", {}).get("id")
            if pid:
                stats_by_player.setdefault(pid, []).append(s)

        # Build a name map from roster
        name_map: Dict[int, str] = {}
        for p in roster:
            name_map[p.id] = p.full_name

        pivots: List[GarbageTimePivot] = []

        for pid, stats_list in stats_by_player.items():
            blowout_usages: List[float] = []
            competitive_usages: List[float] = []
            all_minutes: List[float] = []

            for s in stats_list:
                game_date_str = s.get("game", {}).get("date", "")[:10]
                minutes = self._parse_bdl_minutes(s.get("min", "0"))

                if minutes <= 0:
                    continue

                all_minutes.append(minutes)
                usage = self._compute_usage_proxy(s, minutes)

                if game_date_str in blowout_dates:
                    blowout_usages.append(usage)
                elif game_date_str in competitive_dates:
                    competitive_usages.append(usage)

            # ── Apply qualification filters ────────────────────────
            total_games = len(all_minutes)
            if total_games < GT_MIN_GAMES_PLAYED:
                continue

            avg_min = float(np.mean(all_minutes)) if all_minutes else 0
            if avg_min >= GT_MAX_AVG_MINUTES:
                continue

            if len(blowout_usages) < GT_MIN_BLOWOUT_GAMES:
                continue

            if not competitive_usages:
                continue  # No baseline to compare against

            blowout_avg = float(np.mean(blowout_usages))
            competitive_avg = float(np.mean(competitive_usages))

            if competitive_avg <= 0:
                continue

            spike = blowout_avg / competitive_avg
            if spike < GT_USAGE_SPIKE_THRESHOLD:
                continue

            # ── Qualified specialist ───────────────────────────────
            pivots.append(GarbageTimePivot(
                player_name=name_map.get(pid, f"Player_{pid}"),
                team=team_abbr,
                bdl_player_id=pid,
                avg_minutes=round(avg_min, 1),
                normal_usage_proxy=round(competitive_avg, 4),
                blowout_usage_proxy=round(blowout_avg, 4),
                usage_spike_pct=round(spike, 3),
                blowout_games_count=len(blowout_usages),
                normal_games_count=len(competitive_usages),
                minutes_factor=GT_SPECIALIST_MINUTES_FACTOR,
                confidence="High" if len(blowout_usages) >= 3 else "Medium",
            ))

        return pivots

    # ── Helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _parse_bdl_minutes(raw_min) -> float:
        """Parse BDL minutes string (``"32:15"``, ``"00"``, etc.) to float."""
        if isinstance(raw_min, str) and ":" in raw_min:
            parts = raw_min.split(":")
            try:
                return int(parts[0]) + int(parts[1]) / 60
            except (ValueError, IndexError):
                return 0.0
        if raw_min:
            try:
                return float(raw_min)
            except (ValueError, TypeError):
                return 0.0
        return 0.0

    @staticmethod
    def _compute_usage_proxy(stat: dict, minutes: float) -> float:
        """Compute possessions‑used proxy: ``(FGA + 0.44×FTA + TOV) / MIN``.

        This is the standard NBA usage formula (pre‑team‑normalization).
        """
        fga = stat.get("fga", 0) or 0
        fta = stat.get("fta", 0) or 0
        tov = stat.get("turnover", 0) or 0
        return (fga + 0.44 * fta + tov) / minutes if minutes > 0 else 0.0
