"""Slate edge pipeline — end-to-end comparison of Monte Carlo sims vs DK props.

Bridges the gap between the DraftKings prop lines (``DKPropsService``) and the
Monte Carlo simulation output (``PropEdgeAnalyzer``) for tonight's slate.

**Why not the BDL MCP?**

    The BallDontLie MCP exposes exactly four tools: ``get_teams``,
    ``get_players``, ``get_games``, ``get_game``.  It has **no** props
    endpoint.  Attempting ``BDLMCPClient.get_player_props()`` will raise
    ``BDLMethodNotAvailable``.  Player prop lines come from DraftKings
    Sportsbook (``DKPropsService``), not from BDL.

Pipeline
--------
::

    DKPropsService.get_player_props(date)      ← fetch DK PRA lines
        ↓
    BDLMCPClient.get_active_roster(team)       ← filter to active players
        ↓
    PropEdgeAnalyzer.find_top_over_edges()     ← compute P(over), edge, Kelly
        ↓
    pd.DataFrame  (top 5 highest-value Overs)

Usage
-----
::

    from app.services.slate_edge_pipeline import SlateEdgePipeline

    pipeline = SlateEdgePipeline(dk_props_svc, bdl_mcp_client, edge_analyzer)

    sim_projections = {
        "Luka Doncic":   {"mean_pra": 52.1, "std_pra": 9.3},
        "Shai Gilgeous-Alexander": {"mean_pra": 46.8, "std_pra": 8.1},
    }

    df = pipeline.build_slate_edges(
        game_date="2026-02-25",
        sim_projections=sim_projections,
        top_n=5,
    )
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional, Sequence, Set

import pandas as pd

from app.services.prop_edge_analyzer import (
    PlayerPropLine as EdgePropLine,
    PropEdgeAnalyzer,
    SimProjection,
)

logger = logging.getLogger(__name__)


class SlateEdgePipeline:
    """End-to-end pipeline: DK props + Monte Carlo sims → top-N over edges.

    Parameters
    ----------
    dk_props_service : DKPropsService
        Fetches live O/U lines from DraftKings Sportsbook.
    bdl_mcp_client : BDLMCPClient, optional
        Used to validate that players are active (not Out/DNP).
        If ``None``, no roster filtering is applied.
    edge_analyzer : PropEdgeAnalyzer, optional
        Computes P(over), edge, Kelly.  Defaults to a fresh instance.
    """

    def __init__(
        self,
        dk_props_service,
        bdl_mcp_client=None,
        edge_analyzer: Optional[PropEdgeAnalyzer] = None,
    ):
        self._dk_props = dk_props_service
        self._bdl_mcp = bdl_mcp_client
        self._analyzer = edge_analyzer or PropEdgeAnalyzer()

    def build_slate_edges(
        self,
        game_date: str,
        sim_projections: Dict[str, SimProjection | dict],
        *,
        player_names: Optional[Sequence[str]] = None,
        top_n: int = 5,
        stat_type: str = "pra",
        filter_active_only: bool = True,
    ) -> pd.DataFrame:
        """Full pipeline: fetch DK props → validate roster → compute edges.

        Args:
            game_date: ``"YYYY-MM-DD"`` for tonight's slate.
            sim_projections: Keyed by player name.  Values can be
                ``SimProjection`` instances or dicts with ``mean_pra``
                and ``std_pra``.
            player_names: If provided, only evaluate these players.
                Otherwise evaluate all players that appear in both
                the DK props and the sim projections.
            top_n: Number of top overs to return.
            stat_type: DK stat category to compare (default ``"pra"``).
            filter_active_only: If True and a BDLMCPClient is available,
                exclude players whose injury status is "Out" or DNP.

        Returns:
            DataFrame with columns: player, team, opponent, book_line,
            over_odds, sim_mean, sim_std, p_over, implied, edge_pct,
            edge_display, kelly_frac, confidence.  Sorted by edge_pct
            descending, limited to ``top_n``.
        """
        # Step 1: Fetch DK prop lines
        dk_props = self._fetch_dk_props(game_date)
        if not dk_props:
            logger.warning(
                f"[SlateEdge] No DK props available for {game_date}"
            )
            return pd.DataFrame()

        # Step 2: Build active player set (optional roster filter)
        active_names: Optional[Set[str]] = None
        if filter_active_only and self._bdl_mcp:
            active_names = self._get_active_player_names(dk_props)

        # Step 3: Convert DK PlayerProps → EdgePropLine for each target player
        edge_lines = self._convert_dk_props(
            dk_props,
            stat_type=stat_type,
            player_filter=set(player_names) if player_names else None,
            active_filter=active_names,
        )

        if not edge_lines:
            logger.info(
                f"[SlateEdge] No {stat_type.upper()} props matched filters "
                f"for {game_date}"
            )
            return pd.DataFrame()

        # Step 4: Run edge analysis
        df = self._analyzer.find_top_over_edges(
            edge_lines, sim_projections, top_n=top_n,
        )

        logger.info(
            f"[SlateEdge] {game_date}: {len(edge_lines)} props evaluated, "
            f"{len(df)} edges returned (top {top_n})"
        )
        return df

    # ── Internal helpers ──────────────────────────────────────────────

    def _fetch_dk_props(self, game_date: str) -> dict:
        """Fetch DK player props, returning the raw cache dict."""
        try:
            return self._dk_props.get_player_props(game_date)
        except Exception as e:
            logger.error(f"[SlateEdge] DK props fetch failed: {e}")
            return {}

    def _get_active_player_names(self, dk_props: dict) -> Set[str]:
        """Use BDLMCPClient + InjuryService to get active player names.

        Collects all teams from the DK props, fetches active rosters
        from BDL (filtered by injuries), and returns the set of
        lowercase full names.
        """
        active: Set[str] = set()
        teams_seen: Set[str] = set()

        for player_props in dk_props.values():
            team = getattr(player_props, "team_abbreviation", "")
            if team and team.upper() not in teams_seen:
                teams_seen.add(team.upper())
                try:
                    roster = self._bdl_mcp.get_active_roster(team.upper())
                    for p in roster:
                        active.add(p.full_name.lower())
                except Exception as e:
                    logger.debug(
                        f"[SlateEdge] Active roster fetch failed for "
                        f"{team}: {e} — including all {team} players"
                    )
                    # On failure, include all players from this team
                    for pp in dk_props.values():
                        if getattr(pp, "team_abbreviation", "").upper() == team.upper():
                            active.add(getattr(pp, "player_name", "").lower())

        return active

    def _convert_dk_props(
        self,
        dk_props: dict,
        *,
        stat_type: str,
        player_filter: Optional[Set[str]],
        active_filter: Optional[Set[str]],
    ) -> List[EdgePropLine]:
        """Convert DK PlayerProps to PropEdgeAnalyzer's EdgePropLine format.

        Only includes players that:
        - Have a prop line for ``stat_type``
        - Are in ``player_filter`` (if provided)
        - Are in ``active_filter`` (if provided)
        """
        lines: List[EdgePropLine] = []

        for player_props in dk_props.values():
            name = getattr(player_props, "player_name", "")
            team = getattr(player_props, "team_abbreviation", "")

            # Player name filter
            if player_filter and name not in player_filter:
                continue

            # Active roster filter
            if active_filter and name.lower() not in active_filter:
                logger.debug(
                    f"[SlateEdge] Skipping {name} — not on active roster"
                )
                continue

            # Find the specific stat prop
            prop_line = player_props.get_line(stat_type)
            if not prop_line:
                continue

            lines.append(EdgePropLine(
                player_name=name,
                line=prop_line.line,
                over_odds=prop_line.over_odds,
                stat_type=stat_type.upper(),
                team=team,
            ))

        return lines
