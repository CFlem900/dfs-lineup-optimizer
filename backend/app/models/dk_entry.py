"""Pydantic models for the DraftKings entry automation service.

Defines the data structures for downloading, filling, and uploading
DraftKings entries CSV templates.
"""

from pydantic import BaseModel, Field
from typing import Dict, List, Optional


class DKEntryRow(BaseModel):
    """A single entry (row) in the DK entries template."""

    entry_id: int
    contest_name: str
    contest_id: int
    entry_fee: str
    player_ids: List[Optional[int]] = Field(
        default_factory=list,
        description="Current lineup slot IDs (None = empty/reservation)",
    )


class DKEntriesTemplate(BaseModel):
    """Parsed DK entries template downloaded from draftkings.com/lineups."""

    draft_group_id: Optional[int] = None
    sport: str = "cbb"
    roster_slots: List[str] = Field(
        default_factory=list,
        description="Position headers, e.g. ['G','G','G','F','F','F','UTIL','UTIL']",
    )
    entries: List[DKEntryRow] = Field(default_factory=list)
    player_reference: Dict[int, str] = Field(
        default_factory=dict,
        description="dk_player_id -> display_name from the reference table",
    )
    raw_csv: str = Field(
        default="",
        description="Original CSV content as downloaded from DK",
    )
    contest_summary: Dict[str, int] = Field(
        default_factory=dict,
        description="Contest name -> entry count summary",
    )


class LineupMeta(BaseModel):
    """Optional scoring metadata for a single lineup, parallel to
    ``lineup_player_ids``. Lets the fill selector pick the right lineup for
    each contest type (high-floor for cash, high-ceiling for GPP)."""

    projection: float = 0.0
    ceiling: float = 0.0
    floor: float = 0.0


class FillSelectionOptions(BaseModel):
    """Knobs for the DK Fill selector."""

    mode: str = Field(
        default="auto",
        description=(
            "'auto' = contest-tier matching with dedup + exposure cap. "
            "'round_robin' = legacy idx % num_lineups behavior."
        ),
    )
    max_exposure_pct: Optional[float] = Field(
        default=None,
        description=(
            "Cap on how often a single lineup can be used, expressed as a "
            "fraction of total entries (e.g. 0.25 = at most 25% of entries). "
            "None = uncapped."
        ),
        ge=0.0,
        le=1.0,
    )
    dedupe_per_contest: bool = Field(
        default=True,
        description=(
            "If True, never assign the same lineup twice to the same contest_id "
            "(DK rejects duplicate entries within a single contest)."
        ),
    )


class DKFillRequest(BaseModel):
    """Request body for filling entries with lineup IDs."""

    template: DKEntriesTemplate
    lineup_player_ids: List[List[int]] = Field(
        description="List of lineups, each is a list of dk_player_ids in slot order",
    )
    lineup_meta: Optional[List[LineupMeta]] = Field(
        default=None,
        description=(
            "Optional per-lineup scoring metadata, same length and order as "
            "``lineup_player_ids``. Required for tiered selection in 'auto' mode."
        ),
    )
    selection: FillSelectionOptions = Field(
        default_factory=FillSelectionOptions,
        description="Selection algorithm options.",
    )


class DKFillResult(BaseModel):
    """Result of filling a DK entries template with lineup player IDs."""

    entries_filled: int
    lineups_used: int
    lineup_cycling: bool = Field(
        default=False,
        description="True if lineups were cycled to fill more entries than lineups",
    )
    warnings: List[str] = Field(default_factory=list)
    filled_csv: str = Field(
        default="",
        description="Ready-to-upload CSV content",
    )
    contest_summary: Dict[str, int] = Field(
        default_factory=dict,
        description="Contest name -> entries filled",
    )
    selection_summary: Dict[str, object] = Field(
        default_factory=dict,
        description=(
            "Diagnostics from the smart selector: mode, lineup_usage "
            "(idx -> count), cash_entries, gpp_entries, dedup_violations."
        ),
    )


class DKUploadResult(BaseModel):
    """Result of uploading filled entries to DraftKings."""

    success: bool
    entries_updated: int = 0
    errors: List[str] = Field(default_factory=list)
    message: str = ""


class DKAutoRequest(BaseModel):
    """Request body for the full auto flow: download -> fill -> upload."""

    draft_group_id: int
    sport: str = "cbb"
    lineup_player_ids: List[List[int]] = Field(
        description="List of lineups, each is a list of dk_player_ids in slot order",
    )


class DKFullResult(BaseModel):
    """Result of the full automated download -> fill -> upload flow."""

    download_ok: bool = False
    fill_ok: bool = False
    upload_ok: bool = False
    template: Optional[DKEntriesTemplate] = None
    fill_result: Optional[DKFillResult] = None
    upload_result: Optional[DKUploadResult] = None
    error: Optional[str] = None


# ── Parsed DK Entries CSV (user upload) ──────────────────────────


class DKParsedEntry(BaseModel):
    """A single parsed contest entry with its current roster."""

    entry_id: str
    current_roster: Dict[str, str] = Field(
        default_factory=dict,
        description="Roster slot -> player display name (empty string if unfilled)",
    )


class DKParsedContest(BaseModel):
    """A contest with all its entries grouped together."""

    contest_id: str
    contest_name: str
    entry_fee: str
    entries: List[DKParsedEntry] = Field(default_factory=list)


class DKParsedEntriesResponse(BaseModel):
    """Top-level response from parse_dk_entries_csv."""

    contests: List[DKParsedContest] = Field(default_factory=list)
    roster_slots: List[str] = Field(
        default_factory=list,
        description="Detected roster slot columns, e.g. ['PG','SG','SF','PF','C','G','F','UTIL']",
    )
    total_entries: int = 0
    total_contests: int = 0


class AutoFillLineupPlayer(BaseModel):
    """A single player in a lineup for auto-fill purposes.

    Sent from the frontend as part of a JSON-encoded lineups array.
    Each player maps to one roster slot position in slot order.
    """

    dk_player_id: int
    display_name: str = ""
