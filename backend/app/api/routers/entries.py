"""Live contest entry import, retrieval, late-swap, and CSV export endpoints.

Allows users to upload their DraftKings ``edit_entries.csv`` file so the
system can identify their current contest entries and enable late-swap
lineup optimisation.  After optimisation, entries can be exported back
to DK-compatible CSV format for re-upload.

Endpoints:
    POST  /slates/parse-entries-csv                             -- Lightweight CSV parse
    POST  /slates/fill-entries-csv                              -- Auto-fill entries with lineups
    POST  /slates/{draft_group_id}/import-entries               -- Upload & parse CSV
    GET   /slates/{draft_group_id}/entries                      -- Retrieve stored entries
    DELETE /slates/{draft_group_id}/entries                      -- Clear entries
    POST  /slates/{draft_group_id}/entries/{entry_id}/late-swap  -- Late-swap one entry
    POST  /slates/{draft_group_id}/entries/late-swap-all         -- Late-swap all entries
    GET   /slates/{draft_group_id}/entries/export-csv            -- Export entries as DK CSV
"""

import csv
import io
import json
import logging
from datetime import date
from typing import Dict, List, Optional

from fastapi import APIRouter, Form, HTTPException, Query, Request, UploadFile, File
from fastapi.responses import StreamingResponse

from app.api.rate_limiter import limiter
from app.api.dependencies import get_services
from app.config import get_settings as _get_settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/slates")


# Allowed MIME types for CSV uploads (matches tournament.py)
_ALLOWED_MIME_TYPES = {
    "text/csv",
    "application/vnd.ms-excel",
    "application/octet-stream",
}

# DraftKings CSV roster slot order (NBA Classic).
# Matches DK_ROSTER_SLOTS in lineup_optimizer_service.py:322.
_DK_NBA_SLOT_ORDER = ["PG", "SG", "SF", "PF", "C", "G", "F", "UTIL"]

# DraftKings CSV header columns (before roster slots)
_DK_CSV_META_HEADERS = ["Entry ID", "Contest Name", "Contest ID", "Entry Fee"]


def _format_dk_player_cell(player: Dict) -> str:
    """Convert a lineup JSONB player dict to DK CSV cell format.

    Input:  {"player_name": "Luka Doncic", "dk_player_id": 12345678, ...}
    Output: "Luka Doncic (12345678)"

    Falls back to player name only if dk_player_id is missing.
    """
    name = player.get("player_name", "")
    pid = player.get("dk_player_id")
    if pid:
        return f"{name} ({pid})"
    return name


def _build_slot_lookup(lineup: List[Dict]) -> Dict[str, Dict]:
    """Build a slot -> player dict lookup from a lineup JSONB array.

    For NBA Classic (all unique slots), this is a simple 1:1 mapping.
    For CBB (duplicate slots like G, G, G), the first occurrence wins
    and subsequent duplicates are accessed by list index instead.
    """
    lookup: Dict[str, Dict] = {}
    for player in lineup:
        slot = player.get("slot", "")
        if slot not in lookup:
            lookup[slot] = player
    return lookup


# ------------------------------------------------------------------
# POST /slates/parse-entries-csv
# ------------------------------------------------------------------

@router.post("/parse-entries-csv")
@limiter.limit("20/minute")
async def parse_entries_csv(
    request: Request,
    file: UploadFile = File(...),
):
    """Parse a DraftKings entries CSV and return contest-grouped data.

    This is a **read-only** endpoint — it does not resolve player IDs,
    does not require a draft_group_id, and does not persist anything
    to the database.  It simply extracts the contest entries from the
    uploaded CSV and returns them grouped by contest.

    Returns a ``DKParsedEntriesResponse`` with contests, roster slots,
    and per-entry current rosters.
    """
    from app.utils.exceptions import InvalidDKFormatError

    try:
        svc = get_services()

        # ── File validation ────────────────────────────────────────
        if file.content_type and file.content_type not in _ALLOWED_MIME_TYPES:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid file type '{file.content_type}'. "
                       f"Only CSV files are accepted.",
            )
        if file.filename and not file.filename.lower().endswith(".csv"):
            raise HTTPException(
                status_code=400,
                detail=f"Invalid filename '{file.filename}'. "
                       f"Only .csv files are accepted.",
            )

        content = await file.read()
        settings = _get_settings()
        max_bytes = settings.max_upload_size_mb * 1024 * 1024
        if len(content) > max_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"File too large ({len(content)} bytes). "
                       f"Max allowed: {settings.max_upload_size_mb} MB.",
            )

        # ── Parse CSV ──────────────────────────────────────────────
        result = svc.entry_import_service.parse_dk_entries_csv(content)
        return result.model_dump()

    except InvalidDKFormatError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Parse entries CSV failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ------------------------------------------------------------------
# POST /slates/fill-entries-csv
# ------------------------------------------------------------------

@router.post("/fill-entries-csv")
@limiter.limit("20/minute")
async def fill_entries_csv(
    request: Request,
    file: UploadFile = File(..., description="Original DK entries CSV"),
    lineups: str = Form(..., description="JSON array of lineups"),
    allow_duplicates: bool = Form(False, description="Allow same lineup across entries"),
):
    """Fill a DraftKings entries CSV with generated lineups and return it.

    Accepts the original CSV file and a JSON-stringified array of lineups.
    Each lineup is an array of ``{dk_player_id, display_name}`` objects
    in roster slot order.  The endpoint maps lineups to entries via
    round-robin, overwrites **only** the roster slot columns in the
    original CSV (preserving all other columns), and returns the filled
    CSV as a file download.

    Metadata is returned in response headers:

    - ``X-Entries-Filled``: number of entries filled
    - ``X-Lineups-Used``: number of unique lineups used
    - ``X-Warnings``: JSON array of warning strings
    - ``X-Contest-Summary``: JSON object ``{contest_name: count}``
    """
    from app.utils.exceptions import InvalidDKFormatError
    from app.services.entry_import_service import EntryImportService

    try:
        svc = get_services()

        # ── File validation ────────────────────────────────────────
        if file.content_type and file.content_type not in _ALLOWED_MIME_TYPES:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid file type '{file.content_type}'. "
                       f"Only CSV files are accepted.",
            )
        if file.filename and not file.filename.lower().endswith(".csv"):
            raise HTTPException(
                status_code=400,
                detail=f"Invalid filename '{file.filename}'. "
                       f"Only .csv files are accepted.",
            )

        content = await file.read()
        settings = _get_settings()
        max_bytes = settings.max_upload_size_mb * 1024 * 1024
        if len(content) > max_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"File too large ({len(content)} bytes). "
                       f"Max allowed: {settings.max_upload_size_mb} MB.",
            )

        # ── Parse lineups JSON ─────────────────────────────────────
        try:
            lineups_data = json.loads(lineups)
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid lineups JSON: {exc}",
            )

        if not isinstance(lineups_data, list) or not lineups_data:
            raise HTTPException(
                status_code=400,
                detail="lineups must be a non-empty JSON array of lineups",
            )
        for i, lu in enumerate(lineups_data):
            if not isinstance(lu, list):
                raise HTTPException(
                    status_code=400,
                    detail=f"Lineup {i + 1} must be an array of player objects",
                )
            for j, p in enumerate(lu):
                if not isinstance(p, dict) or "dk_player_id" not in p:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Lineup {i + 1}, player {j + 1}: "
                               f"missing required 'dk_player_id' field",
                    )

        # ── Parse CSV to extract entry IDs ─────────────────────────
        parsed = svc.entry_import_service.parse_dk_entries_csv(content)
        entry_ids: List[str] = []
        for contest in parsed.contests:
            for entry in contest.entries:
                entry_ids.append(entry.entry_id)

        if not entry_ids:
            raise HTTPException(
                status_code=422,
                detail="No entries found in the CSV to fill.",
            )

        # ── Detect original player cell format ─────────────────────
        try:
            csv_text = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            csv_text = content.decode("latin-1")
        csv_text_clean = csv_text.lstrip("\ufeff").replace("\x00", "")
        all_rows = list(csv.reader(io.StringIO(csv_text_clean)))
        _col_idx, _slots, roster_indices = (
            EntryImportService._find_roster_column_indices(all_rows[0])
        )
        detected_format = EntryImportService._detect_player_cell_format(
            all_rows, roster_indices,
        )

        # ── Assign lineups to entries ──────────────────────────────
        assignment, warnings = svc.entry_import_service.assign_lineups_to_entries(
            entry_ids=entry_ids,
            lineups=lineups_data,
            allow_duplicates=allow_duplicates,
        )

        # ── Export filled CSV ──────────────────────────────────────
        filled_csv, entries_filled, contest_summary = (
            svc.entry_import_service.export_filled_dk_csv(
                original_csv_bytes=content,
                assignment=assignment,
                player_cell_format=detected_format,
            )
        )

        # ── Return CSV as file download ────────────────────────────
        headers = {
            "Content-Disposition": 'attachment; filename="DKEntries_filled.csv"',
            "X-Entries-Filled": str(entries_filled),
            "X-Lineups-Used": str(min(len(lineups_data), len(entry_ids))),
            "X-Warnings": json.dumps(warnings),
            "X-Contest-Summary": json.dumps(contest_summary),
            # Allow frontend JS to read custom headers
            "Access-Control-Expose-Headers": (
                "X-Entries-Filled, X-Lineups-Used, "
                "X-Warnings, X-Contest-Summary"
            ),
        }
        return StreamingResponse(
            iter([filled_csv]),
            media_type="text/csv",
            headers=headers,
        )

    except InvalidDKFormatError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Fill entries CSV failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ------------------------------------------------------------------
# POST /slates/{draft_group_id}/import-entries
# ------------------------------------------------------------------

@router.post("/{draft_group_id}/import-entries")
@limiter.limit("10/minute")
async def import_entries(
    request: Request,
    draft_group_id: int,
    file: UploadFile = File(...),
    sport: str = Query("nba", description="Sport: nba or cbb"),
):
    """Upload a DraftKings edit_entries.csv for late-swap.

    Parses the CSV, resolves all players against the draftables for
    this draft group, and upserts entries into the ``active_entries``
    table.  Re-uploading the same CSV updates existing entries (no
    duplicates).

    Returns a summary with imported count, fees, and per-entry details.
    """
    try:
        svc = get_services()

        # ── File validation (matches tournament.py pattern) ───────
        if file.content_type and file.content_type not in _ALLOWED_MIME_TYPES:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid file type '{file.content_type}'. "
                       f"Only CSV files are accepted.",
            )
        if file.filename and not file.filename.lower().endswith(".csv"):
            raise HTTPException(
                status_code=400,
                detail=f"Invalid filename '{file.filename}'. "
                       f"Only .csv files are accepted.",
            )

        content = await file.read()
        settings = _get_settings()
        max_bytes = settings.max_upload_size_mb * 1024 * 1024
        if len(content) > max_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"File too large ({len(content)} bytes). "
                       f"Max allowed: {settings.max_upload_size_mb} MB.",
            )

        csv_text = content.decode("utf-8")

        # ── Import entries via service ────────────────────────────
        result = await svc.entry_import_service.import_entries(
            csv_text=csv_text,
            draft_group_id=draft_group_id,
            dk_draftables_service=svc.dk_draftables_service,
            sport=sport,
        )
        return result

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            "Entry import failed for DG=%d: %s",
            draft_group_id, e, exc_info=True,
        )
        raise HTTPException(status_code=500, detail=str(e))


# ------------------------------------------------------------------
# GET /slates/{draft_group_id}/entries
# ------------------------------------------------------------------

@router.get("/{draft_group_id}/entries")
async def get_entries(
    draft_group_id: int,
    sport: str = Query("nba", description="Sport: nba or cbb"),
):
    """Get all imported entries for a draft group.

    Returns the full list of entries with lineup details, ordered by
    database ID (insertion order).
    """
    try:
        svc = get_services()
        entries = await svc.entry_import_service.get_entries_for_slate(
            draft_group_id=draft_group_id,
            sport=sport,
        )
        total_fees = sum(e.get("fee", 0) or 0 for e in entries)
        return {
            "draft_group_id": draft_group_id,
            "total_entries": len(entries),
            "total_fees": round(total_fees, 2),
            "entries": entries,
        }
    except Exception as e:
        logger.error(
            "Get entries failed for DG=%d: %s",
            draft_group_id, e, exc_info=True,
        )
        raise HTTPException(status_code=500, detail=str(e))


# ------------------------------------------------------------------
# DELETE /slates/{draft_group_id}/entries
# ------------------------------------------------------------------

@router.delete("/{draft_group_id}/entries")
async def delete_entries(
    draft_group_id: int,
    sport: str = Query("nba", description="Sport: nba or cbb"),
):
    """Delete all imported entries for a draft group.

    Returns the count of deleted entries.
    """
    try:
        svc = get_services()
        count = await svc.entry_import_service.delete_entries_for_slate(
            draft_group_id=draft_group_id,
            sport=sport,
        )
        return {
            "deleted": count,
            "draft_group_id": draft_group_id,
        }
    except Exception as e:
        logger.error(
            "Delete entries failed for DG=%d: %s",
            draft_group_id, e, exc_info=True,
        )
        raise HTTPException(status_code=500, detail=str(e))


# ------------------------------------------------------------------
# POST /slates/{draft_group_id}/entries/{entry_id}/late-swap
# ------------------------------------------------------------------

@router.post("/{draft_group_id}/entries/{entry_id}/late-swap")
async def late_swap_entry(
    draft_group_id: int,
    entry_id: str,
    sport: str = Query("nba", description="Sport: nba or cbb"),
    game_date: Optional[str] = Query(
        None, description="Game date YYYY-MM-DD (default: today)",
    ),
):
    """Late-swap optimise a single entry via ILP.

    Evaluates which players are locked (game started) vs. unlocked,
    then re-optimises the open slots to maximise projected fantasy
    points under the reduced salary budget.

    Returns the updated lineup, swap details, and diagnostics.
    """
    try:
        svc = get_services()

        # Resolve game_date
        gd = game_date or date.today().isoformat()

        # Build player pool from the lineup optimizer
        pool = svc.lineup_optimizer_service.build_player_pool(
            platform="dk",
            draft_group_id=draft_group_id,
            sport=sport,
        )
        if not pool:
            raise HTTPException(
                status_code=404,
                detail=f"No player pool available for DG={draft_group_id}",
            )

        # Fetch game states
        game_states = await svc.live_game_state_service.fetch_game_states(gd)

        # Load the specific entry
        entries = await svc.entry_import_service.get_entries_for_slate(
            draft_group_id=draft_group_id, sport=sport,
        )
        target = None
        for e in entries:
            if str(e.get("entry_id")) == str(entry_id):
                target = e
                break

        if target is None:
            raise HTTPException(
                status_code=404,
                detail=f"Entry {entry_id} not found for DG={draft_group_id}",
            )

        # Optimise
        result = svc.late_swap_service.optimize_entry(
            entry_data=target,
            player_pool=pool,
            game_states=game_states,
            sport=sport,
        )

        # Update DB if swaps were made
        if result.success and result.swaps:
            from app.services.late_swap_service import LateSwapService
            await LateSwapService._update_entry_in_db(
                entry_id=entry_id,
                new_lineup=result.lineup,
                total_salary=result.total_salary,
            )

        return {
            "success": result.success,
            "entry_id": result.entry_id,
            "lineup": result.lineup,
            "total_salary": result.total_salary,
            "total_projected_fp": result.total_projected_fp,
            "swaps": [
                {
                    "slot": s.slot,
                    "old_player": s.old_player,
                    "new_player": s.new_player,
                    "old_salary": s.old_salary,
                    "new_salary": s.new_salary,
                    "projected_fp_gain": s.projected_fp_gain,
                }
                for s in result.swaps
            ],
            "locked_count": result.locked_count,
            "open_count": result.open_count,
            "remaining_salary_used": result.remaining_salary_used,
            "warnings": result.warnings,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            "Late-swap failed for entry %s DG=%d: %s",
            entry_id, draft_group_id, e, exc_info=True,
        )
        raise HTTPException(status_code=500, detail=str(e))


# ------------------------------------------------------------------
# POST /slates/{draft_group_id}/entries/late-swap-all
# ------------------------------------------------------------------

@router.post("/{draft_group_id}/entries/late-swap-all")
async def late_swap_all_entries(
    draft_group_id: int,
    sport: str = Query("nba", description="Sport: nba or cbb"),
    game_date: Optional[str] = Query(
        None, description="Game date YYYY-MM-DD (default: today)",
    ),
):
    """Late-swap optimise all entries for a draft group.

    Processes each entry in sequence, evaluating locks and re-optimising
    open slots.  Returns a summary with per-entry results.
    """
    try:
        svc = get_services()

        gd = game_date or date.today().isoformat()

        # Build player pool once for all entries
        pool = svc.lineup_optimizer_service.build_player_pool(
            platform="dk",
            draft_group_id=draft_group_id,
            sport=sport,
        )
        if not pool:
            raise HTTPException(
                status_code=404,
                detail=f"No player pool available for DG={draft_group_id}",
            )

        # Run batch optimisation
        results = await svc.late_swap_service.optimize_all_entries(
            draft_group_id=draft_group_id,
            player_pool=pool,
            game_date=gd,
            sport=sport,
        )

        total_swaps = sum(len(r.swaps) for r in results)
        total_fp_gain = sum(
            sum(s.projected_fp_gain for s in r.swaps)
            for r in results
        )

        return {
            "draft_group_id": draft_group_id,
            "total_entries": len(results),
            "total_swaps": total_swaps,
            "total_fp_gain": round(total_fp_gain, 2),
            "entries": [
                {
                    "success": r.success,
                    "entry_id": r.entry_id,
                    "lineup": r.lineup,
                    "total_salary": r.total_salary,
                    "total_projected_fp": r.total_projected_fp,
                    "swaps": [
                        {
                            "slot": s.slot,
                            "old_player": s.old_player,
                            "new_player": s.new_player,
                            "old_salary": s.old_salary,
                            "new_salary": s.new_salary,
                            "projected_fp_gain": s.projected_fp_gain,
                        }
                        for s in r.swaps
                    ],
                    "locked_count": r.locked_count,
                    "open_count": r.open_count,
                    "warnings": r.warnings,
                }
                for r in results
            ],
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            "Late-swap-all failed for DG=%d: %s",
            draft_group_id, e, exc_info=True,
        )
        raise HTTPException(status_code=500, detail=str(e))


# ------------------------------------------------------------------
# GET /slates/{draft_group_id}/entries/export-csv
# ------------------------------------------------------------------

@router.get("/{draft_group_id}/entries/export-csv")
async def export_entries_csv(
    draft_group_id: int,
    sport: str = Query("nba", description="Sport: nba or cbb"),
):
    """Export all entries for a draft group as a DraftKings-compatible CSV.

    Produces a CSV file matching the DK ``edit_entries.csv`` format so the
    user can re-upload optimised lineups directly to DraftKings.

    Header row:
        Entry ID, Contest Name, Contest ID, Entry Fee, PG, SG, SF, PF, C, G, F, UTIL

    Player cells use the DK format: ``"PlayerName (dk_player_id)"``

    Returns a ``text/csv`` streaming response with Content-Disposition
    attachment header for browser download.
    """
    try:
        svc = get_services()

        entries = await svc.entry_import_service.get_entries_for_slate(
            draft_group_id=draft_group_id,
            sport=sport,
        )
        if not entries:
            raise HTTPException(
                status_code=404,
                detail=f"No entries found for DG={draft_group_id}",
            )

        # Determine slot order from the first entry's lineup.
        # For NBA Classic, all 8 slots are unique so we use the
        # canonical DK order.  For CBB (duplicate G/F/UTIL), we
        # preserve the lineup's insertion order instead.
        first_lineup = entries[0].get("lineup", [])
        first_slots = [p.get("slot", "") for p in first_lineup]

        if set(first_slots) == set(_DK_NBA_SLOT_ORDER):
            slot_order = _DK_NBA_SLOT_ORDER
        else:
            # CBB or non-standard: use lineup order as-is
            slot_order = first_slots

        # ── Build CSV in memory ──────────────────────────────────
        output = io.StringIO()
        writer = csv.writer(output, lineterminator="\n")

        # Header row
        writer.writerow(_DK_CSV_META_HEADERS + slot_order)

        # Entry rows
        for entry in entries:
            lineup = entry.get("lineup", [])

            # For NBA (unique slots) use lookup by slot name.
            # For CBB (duplicate slots) use positional index.
            has_duplicate_slots = len(slot_order) != len(set(slot_order))

            if has_duplicate_slots:
                # Positional: lineup list index maps to slot_order index
                player_cells = []
                for idx, slot in enumerate(slot_order):
                    if idx < len(lineup):
                        player_cells.append(
                            _format_dk_player_cell(lineup[idx])
                        )
                    else:
                        logger.warning(
                            "Entry %s missing player at index %d (slot %s)",
                            entry.get("entry_id"), idx, slot,
                        )
                        player_cells.append("")
            else:
                # Lookup by slot name (NBA Classic -- all unique)
                slot_lookup = _build_slot_lookup(lineup)
                player_cells = []
                for slot in slot_order:
                    player = slot_lookup.get(slot)
                    if player:
                        player_cells.append(
                            _format_dk_player_cell(player)
                        )
                    else:
                        logger.warning(
                            "Entry %s missing player for slot %s",
                            entry.get("entry_id"), slot,
                        )
                        player_cells.append("")

            # Fee formatting: show as decimal (e.g. 5.00)
            fee = entry.get("fee")
            fee_str = f"{fee:.2f}" if fee is not None else ""

            writer.writerow([
                entry.get("entry_id", ""),
                entry.get("contest_name", ""),
                entry.get("contest_id", ""),
                fee_str,
            ] + player_cells)

        csv_content = output.getvalue()
        output.close()

        # ── Return as streaming CSV download ─────────────────────
        filename = f"dk-entries-{draft_group_id}.csv"
        return StreamingResponse(
            iter([csv_content]),
            media_type="text/csv",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
            },
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            "Export entries CSV failed for DG=%d: %s",
            draft_group_id, e, exc_info=True,
        )
        raise HTTPException(status_code=500, detail=str(e))
