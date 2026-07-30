"""Blowout‑risk analyzer — adjusts prop edges based on matchup context.

Uses BallDontLie game data (spreads, recent scores, team records) to
identify likely blowouts and discount the projected minutes/PRA before
recalculating over‑edges.  This is a **pre‑bet filter** that layers on
top of the existing RotationEngine blowout logic (which operates at the
simulation level).

Blowout risk is derived from three independent signals:

  1. **Spread signal** — BDL V2 odds (|spread| ≥ threshold)
  2. **Record signal** — season win‑rate differential between the two
     teams (top seed vs bottom seed)
  3. **Recent‑form signal** — average scoring margin over each team's
     last *N* games (hot team vs cold team)

These signals are combined into a single ``risk_score`` (0.0–1.0) that
determines the minutes‑adjustment factor.

Data flow
---------
::

    PropEdgeAnalyzer.find_top_over_edges()   ← raw edge DataFrame
        ↓
    BlowoutRiskAnalyzer.adjust_edges(df)     ← this module
        ↓
    Adjusted DataFrame with new columns:
        blowout_risk, minutes_factor, adj_sim_mean, adj_p_over, adj_edge_pct
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from app.services.prop_edge_analyzer import (
    american_odds_to_implied_prob,
    kelly_fraction,
    prob_over,
)

logger = logging.getLogger(__name__)

# ── Tuning constants ──────────────────────────────────────────────────────

# Spread signal
SPREAD_BLOWOUT_THRESHOLD: float = 7.0  # |spread| above this → risk
SPREAD_SIGNAL_WEIGHT: float = 0.50     # weight in composite score

# Record signal (win‑rate differential)
RECORD_DIFF_THRESHOLD: float = 0.20    # win% gap to start flagging
RECORD_SIGNAL_WEIGHT: float = 0.25

# Recent‑form signal (avg margin over last N games)
RECENT_GAMES_WINDOW: int = 10          # games to look back
MARGIN_DIFF_THRESHOLD: float = 8.0     # combined margin gap to flag
RECENT_SIGNAL_WEIGHT: float = 0.25

# Minutes adjustment
DEFAULT_BLOWOUT_MINUTES_FACTOR: float = 0.90  # 10 % reduction
RISK_THRESHOLD: float = 0.40                   # composite score to trigger


# ── Data classes ──────────────────────────────────────────────────────────


@dataclass
class TeamForm:
    """Recent performance snapshot for one team."""

    team_abbr: str
    wins: int = 0
    losses: int = 0
    recent_avg_margin: float = 0.0  # positive = winning by this much
    recent_record: Tuple[int, int] = (0, 0)  # (W, L) in window

    @property
    def win_pct(self) -> float:
        total = self.wins + self.losses
        return self.wins / total if total > 0 else 0.5

    @property
    def recent_win_pct(self) -> float:
        w, l = self.recent_record
        total = w + l
        return w / total if total > 0 else 0.5


@dataclass
class BlowoutAssessment:
    """Blowout‑risk assessment for a single game."""

    game_id: Optional[int] = None
    home_team: str = ""
    away_team: str = ""

    # Raw signals (0.0–1.0 each)
    spread_signal: float = 0.0
    record_signal: float = 0.0
    recent_signal: float = 0.0

    # Composite
    risk_score: float = 0.0  # weighted average of signals
    is_blowout_risk: bool = False

    # Adjustment
    minutes_factor: float = 1.0  # applied to mean PRA

    # Context
    spread: Optional[float] = None
    home_form: Optional[TeamForm] = None
    away_form: Optional[TeamForm] = None
    favored_team: str = ""


# ── Core analyzer ─────────────────────────────────────────────────────────


class BlowoutRiskAnalyzer:
    """Assess blowout risk per game and adjust prop‑edge projections.

    Parameters
    ----------
    bdl_service : BallDontLieService
        Used to fetch game odds, recent scores, and team records.
    minutes_factor : float
        Multiplier applied to mean PRA when blowout risk is detected.
        Default 0.90 (10 % reduction per the user spec).
    risk_threshold : float
        Composite risk score (0–1) above which a game is flagged.
    """

    def __init__(
        self,
        bdl_service,
        minutes_factor: float = DEFAULT_BLOWOUT_MINUTES_FACTOR,
        risk_threshold: float = RISK_THRESHOLD,
    ):
        self._bdl = bdl_service
        self._minutes_factor = minutes_factor
        self._risk_threshold = risk_threshold

    # ── Public API ────────────────────────────────────────────────────

    def assess_games(
        self, game_date: str
    ) -> Dict[str, BlowoutAssessment]:
        """Assess blowout risk for every game on ``game_date``.

        Returns a dict keyed by **team abbreviation** (both home and away
        entries point to the same ``BlowoutAssessment``).

        Args:
            game_date: ``"YYYY-MM-DD"`` string.

        Returns:
            ``{"DAL": BlowoutAssessment(...), "LAL": BlowoutAssessment(...), ...}``
        """
        # 1. Fetch tonight's games and odds
        games = self._fetch_games(game_date)
        odds_by_game = self._fetch_odds(game_date)

        # 2. Fetch recent form for all teams playing tonight
        teams_playing = set()
        for g in games:
            h = (g.get("home_team") or {}).get("abbreviation", "").upper()
            v = (g.get("visitor_team") or {}).get("abbreviation", "").upper()
            if h:
                teams_playing.add(h)
            if v:
                teams_playing.add(v)

        team_form = self._build_team_form(teams_playing, game_date)

        # 3. Score each game
        result: Dict[str, BlowoutAssessment] = {}
        for g in games:
            home = (g.get("home_team") or {}).get("abbreviation", "").upper()
            away = (g.get("visitor_team") or {}).get("abbreviation", "").upper()
            if not home or not away:
                continue

            gid = g.get("id")
            odds = odds_by_game.get(gid, {})
            spread = odds.get("spread_home")  # negative = home favored

            assessment = self._score_game(
                gid, home, away, spread,
                team_form.get(home), team_form.get(away),
            )

            result[home] = assessment
            result[away] = assessment

        logger.info(
            f"[BlowoutRisk] Assessed {len(games)} games on {game_date}: "
            f"{sum(1 for a in result.values() if a.is_blowout_risk) // 2} "
            f"flagged as blowout risk"
        )
        return result

    def adjust_edges(
        self,
        edge_df: pd.DataFrame,
        assessments: Dict[str, BlowoutAssessment],
    ) -> pd.DataFrame:
        """Apply blowout‑risk adjustments to a prop‑edge DataFrame.

        Takes the output of ``PropEdgeAnalyzer.find_top_over_edges()`` and
        adds adjusted columns.  Players on non‑blowout games are unchanged.

        New columns added:

        ================== ===============================================
        Column             Description
        ================== ===============================================
        blowout_risk       Composite risk score (0.0–1.0)
        minutes_factor     Multiplier applied (1.0 = no change)
        adj_sim_mean       ``sim_mean × minutes_factor``
        adj_sim_std        ``sim_std × minutes_factor``
        adj_p_over         Recalculated P(Over) with adjusted mean/std
        adj_edge_pct       Recalculated edge
        adj_edge_display   Formatted adjusted edge
        adj_kelly_frac     Recalculated Kelly fraction
        adj_confidence     Recalculated confidence tier
        favored_team       Which team is favored
        ================== ===============================================

        Args:
            edge_df:     Output of ``PropEdgeAnalyzer.find_top_over_edges()``.
            assessments: Output of ``self.assess_games()``.

        Returns:
            A copy of the DataFrame with adjustment columns appended,
            re‑sorted by ``adj_edge_pct`` descending.
        """
        if edge_df.empty:
            return edge_df.copy()

        df = edge_df.copy()

        # Vectorized lookup and adjustment
        risks = []
        factors = []
        adj_means = []
        adj_stds = []
        adj_p_overs = []
        adj_edges = []
        adj_displays = []
        adj_kellys = []
        adj_confs = []
        favored = []

        for _, row in df.iterrows():
            team = row.get("team", "")
            assessment = assessments.get(team.upper()) if team else None

            if assessment and assessment.is_blowout_risk:
                risk = assessment.risk_score
                factor = assessment.minutes_factor
                fav = assessment.favored_team
            else:
                risk = assessment.risk_score if assessment else 0.0
                factor = 1.0
                fav = assessment.favored_team if assessment else ""

            # Scale mean and std proportionally (PRA ∝ minutes)
            adj_mean = row["sim_mean"] * factor
            adj_std = row["sim_std"] * factor
            line = row["book_line"]
            odds = row["over_odds"]

            p = prob_over(adj_mean, adj_std, line)
            implied = american_odds_to_implied_prob(odds)
            edge = p - implied

            risks.append(round(risk, 3))
            factors.append(round(factor, 3))
            adj_means.append(round(adj_mean, 1))
            adj_stds.append(round(adj_std, 1))
            adj_p_overs.append(round(p, 4))
            adj_edges.append(round(edge, 4))
            adj_displays.append(f"{'+' if edge >= 0 else ''}{edge * 100:.1f}%")
            adj_kellys.append(round(kelly_fraction(edge, odds), 4))
            adj_confs.append(self._confidence_label(edge, adj_std, adj_mean, line))
            favored.append(fav)

        df["blowout_risk"] = risks
        df["minutes_factor"] = factors
        df["adj_sim_mean"] = adj_means
        df["adj_sim_std"] = adj_stds
        df["adj_p_over"] = adj_p_overs
        df["adj_edge_pct"] = adj_edges
        df["adj_edge_display"] = adj_displays
        df["adj_kelly_frac"] = adj_kellys
        df["adj_confidence"] = adj_confs
        df["favored_team"] = favored

        return df.sort_values("adj_edge_pct", ascending=False).reset_index(
            drop=True
        )

    # ── Risk scoring ──────────────────────────────────────────────────

    def _score_game(
        self,
        game_id: Optional[int],
        home: str,
        away: str,
        spread: Optional[float],
        home_form: Optional[TeamForm],
        away_form: Optional[TeamForm],
    ) -> BlowoutAssessment:
        """Compute composite blowout risk for a single game."""

        # Signal 1: Spread
        spread_sig = 0.0
        if spread is not None:
            abs_spread = abs(spread)
            if abs_spread >= SPREAD_BLOWOUT_THRESHOLD:
                # Linear ramp: 7 → 0.0, 15 → 1.0
                spread_sig = min(
                    1.0,
                    (abs_spread - SPREAD_BLOWOUT_THRESHOLD)
                    / (15.0 - SPREAD_BLOWOUT_THRESHOLD),
                )

        # Signal 2: Season record differential
        record_sig = 0.0
        if home_form and away_form:
            diff = abs(home_form.win_pct - away_form.win_pct)
            if diff >= RECORD_DIFF_THRESHOLD:
                record_sig = min(1.0, (diff - RECORD_DIFF_THRESHOLD) / 0.30)

        # Signal 3: Recent form (average margin differential)
        recent_sig = 0.0
        if home_form and away_form:
            margin_gap = abs(
                home_form.recent_avg_margin - away_form.recent_avg_margin
            )
            if margin_gap >= MARGIN_DIFF_THRESHOLD:
                recent_sig = min(
                    1.0,
                    (margin_gap - MARGIN_DIFF_THRESHOLD)
                    / (20.0 - MARGIN_DIFF_THRESHOLD),
                )

        # Composite (weighted)
        # If no spread available, redistribute its weight equally
        if spread is not None:
            risk = (
                spread_sig * SPREAD_SIGNAL_WEIGHT
                + record_sig * RECORD_SIGNAL_WEIGHT
                + recent_sig * RECENT_SIGNAL_WEIGHT
            )
        else:
            risk = (
                record_sig * (RECORD_SIGNAL_WEIGHT + SPREAD_SIGNAL_WEIGHT / 2)
                + recent_sig * (RECENT_SIGNAL_WEIGHT + SPREAD_SIGNAL_WEIGHT / 2)
            )

        is_risk = risk >= self._risk_threshold

        # Minutes factor: linear interpolation between 1.0 and configured floor
        if is_risk:
            # Scale factor from 1.0 → self._minutes_factor as risk → 1.0
            factor = 1.0 - (1.0 - self._minutes_factor) * min(risk / 1.0, 1.0)
        else:
            factor = 1.0

        # Determine favored team
        fav = ""
        if spread is not None:
            fav = home if spread < 0 else away
        elif home_form and away_form:
            fav = home if home_form.win_pct > away_form.win_pct else away

        return BlowoutAssessment(
            game_id=game_id,
            home_team=home,
            away_team=away,
            spread_signal=round(spread_sig, 3),
            record_signal=round(record_sig, 3),
            recent_signal=round(recent_sig, 3),
            risk_score=round(risk, 3),
            is_blowout_risk=is_risk,
            minutes_factor=round(factor, 3),
            spread=spread,
            home_form=home_form,
            away_form=away_form,
            favored_team=fav,
        )

    # ── BDL data fetching ─────────────────────────────────────────────

    def _fetch_games(self, game_date: str) -> List[dict]:
        """Fetch tonight's games from BDL."""
        try:
            return self._bdl.get_games(game_date)
        except Exception as e:
            logger.error(f"[BlowoutRisk] Failed to fetch games: {e}")
            return []

    def _fetch_odds(self, game_date: str) -> Dict[int, dict]:
        """Fetch odds for tonight's games from BDL V2."""
        try:
            return self._bdl.get_odds_by_game(game_date)
        except Exception as e:
            logger.error(f"[BlowoutRisk] Failed to fetch odds: {e}")
            return {}

    def _build_team_form(
        self, teams: set, game_date: str
    ) -> Dict[str, TeamForm]:
        """Build recent-form snapshots for a set of teams.

        Fetches the last ``RECENT_GAMES_WINDOW`` finished games for each
        team and computes win %, average margin, and recent record.
        """
        result: Dict[str, TeamForm] = {}
        if not teams:
            return result

        try:
            end = date.fromisoformat(game_date)
        except ValueError:
            return result

        start = end - timedelta(days=30)  # ~15 games in 30 days

        # Batch all dates into a single BDL request (dates[] accepts arrays)
        all_games: List[dict] = []
        dates_param = "&".join(
            f"dates[]={d.isoformat()}"
            for d in (start + timedelta(days=i) for i in range((end - start).days))
        )
        try:
            raw = self._bdl._get(f"/games?{dates_param}&per_page=100")
            all_games = raw.get("data", [])
            # Handle pagination if needed (30 days can exceed 100 games)
            meta = raw.get("meta", {})
            next_cursor = meta.get("next_cursor")
            while next_cursor:
                page = self._bdl._get(
                    f"/games?{dates_param}&per_page=100&cursor={next_cursor}"
                )
                all_games.extend(page.get("data", []))
                next_cursor = page.get("meta", {}).get("next_cursor")
        except Exception as e:
            logger.warning(f"[BlowoutRisk] Bulk game fetch failed: {e}")
            return result

        # Index games by team
        team_games: Dict[str, List[dict]] = {t: [] for t in teams}
        for g in all_games:
            if g.get("status") != "Final":
                continue
            h = (g.get("home_team") or {}).get("abbreviation", "").upper()
            v = (g.get("visitor_team") or {}).get("abbreviation", "").upper()
            h_score = g.get("home_team_score", 0) or 0
            v_score = g.get("visitor_team_score", 0) or 0

            if h in team_games:
                team_games[h].append({"margin": h_score - v_score, "date": g.get("date", "")})
            if v in team_games:
                team_games[v].append({"margin": v_score - h_score, "date": g.get("date", "")})

        # Compute form for each team
        for team_abbr in teams:
            games = team_games.get(team_abbr, [])
            # Sort by date descending, take recent window
            games.sort(key=lambda x: x.get("date", ""), reverse=True)
            recent = games[:RECENT_GAMES_WINDOW]

            if not recent:
                result[team_abbr] = TeamForm(team_abbr=team_abbr)
                continue

            margins = [g["margin"] for g in recent]
            wins = sum(1 for m in margins if m > 0)
            losses = len(margins) - wins
            avg_margin = float(np.mean(margins)) if margins else 0.0

            # Season record from all fetched games (approximation)
            all_margins = [g["margin"] for g in games]
            season_wins = sum(1 for m in all_margins if m > 0)
            season_losses = len(all_margins) - season_wins

            result[team_abbr] = TeamForm(
                team_abbr=team_abbr,
                wins=season_wins,
                losses=season_losses,
                recent_avg_margin=round(avg_margin, 1),
                recent_record=(wins, losses),
            )

        return result

    # ── Helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _confidence_label(edge: float, std: float, mean: float, line: float) -> str:
        """Same logic as PropEdgeAnalyzer for consistency."""
        diff = mean - line
        z = diff / std if std > 0 else (10.0 if diff > 0 else -10.0)
        if edge >= 0.08 and z >= 0.5:
            return "High"
        elif edge >= 0.04 and z >= 0.25:
            return "Medium"
        return "Low"
