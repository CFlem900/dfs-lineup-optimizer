"""Strict Pydantic schemas for BALLDONTLIE (BDL) API responses.

These schemas validate the shape of responses from the BDL V1/V2 REST API
(https://api.balldontlie.io).  They are used for type-checking at ingestion
time — the service layer still returns plain dicts, but runs each raw entry
through ``model_validate()`` to catch unexpected API changes early.

All non-critical fields default to ``None`` so that partial / evolving
responses degrade gracefully rather than raising hard validation errors.

Usage
-----
::

    from app.models.balldontlie_schemas import BDLOddsEntry

    for raw in raw_odds_list:
        BDLOddsEntry.model_validate(raw)   # raises on schema mismatch
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# ── Team ──────────────────────────────────────────────────────────────

class BDLTeam(BaseModel):
    """Team object nested inside BDL game / player responses."""

    id: int
    abbreviation: str
    city: Optional[str] = None
    full_name: Optional[str] = None
    name: Optional[str] = None
    conference: Optional[str] = None
    division: Optional[str] = None


# ── Player ────────────────────────────────────────────────────────────

class BDLPlayer(BaseModel):
    """A single player from ``/v1/players``."""

    id: int
    first_name: str
    last_name: str
    position: Optional[str] = None
    height: Optional[str] = None
    weight: Optional[int] = None
    jersey_number: Optional[str] = None
    college: Optional[str] = None
    country: Optional[str] = None
    draft_year: Optional[int] = None
    draft_round: Optional[int] = None
    draft_number: Optional[int] = None
    team: Optional[BDLTeam] = None

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()


class BDLPlayersResponse(BaseModel):
    """Top-level response envelope from ``/v1/players``."""

    data: List[BDLPlayer] = Field(default_factory=list)
    meta: Optional[Dict[str, Any]] = None


class BDLTeamsResponse(BaseModel):
    """Top-level response envelope from ``/v1/teams`` (MCP get_teams)."""

    data: List[BDLTeam] = Field(default_factory=list)
    meta: Optional[Dict[str, Any]] = None


# ── Game ──────────────────────────────────────────────────────────────

class BDLGame(BaseModel):
    """A single game from ``/v1/games`` or ``/v2/games``."""

    id: int
    home_team: BDLTeam
    visitor_team: BDLTeam
    status: Optional[str] = None
    period: Optional[int] = None
    time: Optional[str] = None
    home_team_score: Optional[int] = None
    visitor_team_score: Optional[int] = None
    date: Optional[str] = None
    season: Optional[int] = None
    postseason: Optional[bool] = None


class BDLGamesResponse(BaseModel):
    """Top-level response envelope from ``/v1/games`` or ``/v2/games``."""

    data: List[BDLGame] = Field(default_factory=list)
    meta: Optional[Dict[str, Any]] = None


# ── Odds ──────────────────────────────────────────────────────────────

class BDLOddsEntry(BaseModel):
    """A single odds entry from ``/v2/odds``.

    Fields mirror the BDL V2 odds payload.  The ``spread_home_value`` uses
    the standard convention: **negative = home team favoured**.

    Example
    -------
    ::

        {
            "game_id": 9001,
            "vendor": "DraftKings",
            "spread_home_value": -7.5,
            "spread_away_value": 7.5,
            "total_value": 224.5,
            "moneyline_home_odds": -320,
            "moneyline_away_odds": 260,
            "updated_at": "2026-02-24T18:30:00Z"
        }
    """

    game_id: int
    vendor: Optional[str] = None
    spread_home_value: Optional[float] = None
    spread_away_value: Optional[float] = None
    total_value: Optional[float] = None
    moneyline_home_odds: Optional[int] = None
    moneyline_away_odds: Optional[int] = None
    updated_at: Optional[str] = None


class BDLOddsResponse(BaseModel):
    """Top-level response envelope from ``/v2/odds``."""

    data: List[BDLOddsEntry] = Field(default_factory=list)
    meta: Optional[Dict[str, Any]] = None
