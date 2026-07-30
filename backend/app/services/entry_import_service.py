"""Live DraftKings entry import service.

Parses a DraftKings ``edit_entries.csv`` file, resolves each player to
their DK draftable identity (dk_player_id, salary, team), and stores
the entries in the ``active_entries`` table for late-swap usage.

Supports two CSV flavors:
    1. **Template format** — positional columns contain bare numeric
       dk_player_ids (e.g. ``12345678``), with a reference table at the
       bottom mapping IDs to display names.
    2. **Edit-entries format** — positional columns contain
       ``"PlayerName (dk_player_id)"`` (e.g. ``"Luka Doncic (12345678)"``).

Re-uploading the same CSV upserts entries (matched on ``entry_id``).
"""

import csv
import io
import logging
import re
from datetime import datetime, timezone
from typing import Dict, List, Optional

from app.utils.helpers import normalize_player_name

logger = logging.getLogger(__name__)

# Regex to extract player name and DK ID from "Player Name (12345678)"
_PLAYER_CELL_PATTERN = re.compile(r"^(.+?)\s*\((\d+)\)\s*$")

# Known DK roster slot column names (NBA Classic + CBB Classic)
_KNOWN_ROSTER_SLOTS = {
    "PG", "SG", "SF", "PF", "C", "G", "F", "UTIL",
}


# ------------------------------------------------------------------
# Standalone name-matching (mirrors LineupOptimizerService._names_match)
# ------------------------------------------------------------------

def _names_match(name_a: str, name_b: str) -> bool:
    """Check if two player names refer to the same person.

    Handles suffix differences (Jr./Sr./III), period removal (P.J.→PJ),
    accent transliteration (Jokić→Jokic), hyphen/apostrophe variants,
    short first names (PJ), and name-order swaps.
    """
    a = normalize_player_name(name_a).replace("-", "").replace("'", "")
    b = normalize_player_name(name_b).replace("-", "").replace("'", "")

    if a == b:
        return True

    # Fallback 1: last name exact + first-name prefix match
    parts_a = a.split()
    parts_b = b.split()
    if parts_a and parts_b and len(parts_a) >= 2 and len(parts_b) >= 2:
        if parts_a[-1] == parts_b[-1]:
            fa, fb = parts_a[0], parts_b[0]
            min_len = min(len(fa), len(fb))
            if min_len >= 3:
                if fa[:3] == fb[:3]:
                    return True
            elif min_len >= 2:
                if fa == fb:
                    return True

    # Fallback 2: sorted-token comparison (catches name-order swaps)
    if sorted(a.split()) == sorted(b.split()):
        return True

    return False


def _parse_fee(fee_str: str) -> float:
    """Parse '$5.00' or '5.00' or '5' to float."""
    cleaned = fee_str.replace("$", "").replace(",", "").strip()
    try:
        return float(cleaned)
    except (ValueError, TypeError):
        return 0.0


def _normalize_header(h: str) -> str:
    """Lowercase, strip whitespace from a CSV header."""
    return h.strip().lower()


# ------------------------------------------------------------------
# Service class
# ------------------------------------------------------------------

class EntryImportService:
    """Parse and store DraftKings contest entries for late-swap."""

    # ── Header / Column Detection ──────────────────────────────────

    @staticmethod
    def _find_roster_column_indices(
        header: List[str],
    ) -> tuple[Dict[str, int], List[str], List[int]]:
        """Scan a DK CSV header for standard columns and roster slot positions.

        Parameters
        ----------
        header : list of str
            Raw header row from ``csv.reader``.

        Returns
        -------
        (col_idx, roster_slots, roster_indices)
            col_idx : dict mapping canonical names
                      (``entry_id``, ``contest_name``, ``contest_id``, ``fee``)
                      to their column index.
            roster_slots : list of uppercase slot names (e.g. ``['PG', 'SG', ...]``).
            roster_indices : list of column indices corresponding to each slot.

        Raises
        ------
        ValueError
            If the ``Entry ID`` column is missing or fewer than 6 roster
            slot columns are detected.
        """
        header_lower = [_normalize_header(h) for h in header]

        col_idx: Dict[str, int] = {}
        for i, h in enumerate(header_lower):
            if h in ("entry id", "entryid"):
                col_idx["entry_id"] = i
            elif h in ("contest name", "contestname"):
                col_idx["contest_name"] = i
            elif h in ("contest id", "contestid"):
                col_idx["contest_id"] = i
            elif h in ("entry fee", "fee", "entryfee"):
                col_idx["fee"] = i

        if "entry_id" not in col_idx:
            raise ValueError(
                "CSV missing required 'Entry ID' column. "
                f"Found headers: {header}"
            )

        # Detect roster slot columns (between last known column and
        # empty/Instructions column).
        fee_col = col_idx.get("fee", col_idx.get("contest_id", 3))
        roster_start = fee_col + 1
        roster_slots: List[str] = []
        roster_indices: List[int] = []

        for i in range(roster_start, len(header)):
            h = header[i].strip()
            if not h or h.lower() == "instructions":
                break
            if h.upper() in _KNOWN_ROSTER_SLOTS or (
                len(h) <= 4 and h == h.upper()
            ):
                roster_slots.append(h.upper())
                roster_indices.append(i)

        if len(roster_slots) < 6:
            raise ValueError(
                f"CSV missing positional columns. Found {len(roster_slots)}/8 "
                f"roster slots: {roster_slots}. Expected columns like "
                f"PG, SG, SF, PF, C, G, F, UTIL."
            )

        return col_idx, roster_slots, roster_indices

    # ── CSV Parsing ────────────────────────────────────────────────

    def _parse_csv(self, csv_text: str) -> tuple[List[Dict], List[str]]:
        """Parse edit_entries.csv into a list of raw entry dicts.

        Each dict contains: entry_id, contest_id, contest_name, fee,
        and players (list of {slot, raw_name, dk_player_id}).

        Returns
        -------
        (entries, roster_slots) — a list of entry dicts and the
        detected roster slot column names (e.g. ['PG','SG',...]).
        """
        # Sanitize
        csv_text = csv_text.lstrip("\ufeff")       # Strip UTF-8 BOM
        csv_text = csv_text.replace("\x00", "")    # Remove null bytes

        reader = csv.reader(io.StringIO(csv_text))
        rows = list(reader)
        if not rows:
            raise ValueError("CSV file is empty — no header row found.")

        header = rows[0]
        col_idx, roster_slots, roster_indices = self._find_roster_column_indices(header)

        # Parse entry rows
        entries: List[Dict] = []
        for row_idx in range(1, len(rows)):
            row = rows[row_idx]
            if len(row) < 4:
                break  # Blank or reference section

            # Entry ID must be present
            raw_entry_id = row[col_idx["entry_id"]].strip() if col_idx["entry_id"] < len(row) else ""
            if not raw_entry_id:
                continue
            # Skip non-numeric entry IDs (reference table rows)
            try:
                int(raw_entry_id)
            except ValueError:
                continue

            contest_id = ""
            if "contest_id" in col_idx and col_idx["contest_id"] < len(row):
                contest_id = row[col_idx["contest_id"]].strip()

            contest_name = ""
            if "contest_name" in col_idx and col_idx["contest_name"] < len(row):
                contest_name = row[col_idx["contest_name"]].strip()

            fee_raw = ""
            if "fee" in col_idx and col_idx["fee"] < len(row):
                fee_raw = row[col_idx["fee"]].strip()
            fee = _parse_fee(fee_raw)

            # Parse positional columns
            players: List[Dict] = []
            for slot, ci in zip(roster_slots, roster_indices):
                cell = row[ci].strip() if ci < len(row) else ""
                if not cell:
                    continue

                # Strategy 1: "PlayerName (dk_player_id)" format
                m = _PLAYER_CELL_PATTERN.match(cell)
                if m:
                    players.append({
                        "slot": slot,
                        "raw_name": m.group(1).strip(),
                        "dk_player_id": int(m.group(2)),
                    })
                    continue

                # Strategy 2: bare numeric dk_player_id (template format)
                if cell.isdigit() and len(cell) >= 5:
                    players.append({
                        "slot": slot,
                        "raw_name": None,
                        "dk_player_id": int(cell),
                    })
                    continue

                # Strategy 3: plain name (no ID available)
                players.append({
                    "slot": slot,
                    "raw_name": cell,
                    "dk_player_id": None,
                })

            entries.append({
                "entry_id": raw_entry_id,
                "contest_id": contest_id,
                "contest_name": contest_name,
                "fee": fee,
                "players": players,
            })

        return entries, roster_slots

    # ── Standalone CSV Parse (no DB, no player resolution) ─────────

    def parse_dk_entries_csv(self, file_bytes: bytes) -> "DKParsedEntriesResponse":
        """Parse a DraftKings entries CSV and return contest-grouped data.

        This is a **lightweight** parsing method that does NOT require a
        ``draft_group_id``, does NOT resolve player IDs against draftables,
        and does NOT persist anything to the database.  It simply extracts
        the user's contest entries from the CSV and groups them by contest.

        Parameters
        ----------
        file_bytes : bytes
            Raw bytes of the uploaded CSV file.

        Returns
        -------
        DKParsedEntriesResponse
            Contests grouped by ID, each with its entries and current
            roster slot → player-name mapping.

        Raises
        ------
        InvalidDKFormatError
            If the file cannot be decoded or is missing required DK
            headers (Entry ID, Contest ID, roster slot columns).
        """
        from app.models.dk_entry import (
            DKParsedContest,
            DKParsedEntriesResponse,
            DKParsedEntry,
        )
        from app.utils.exceptions import InvalidDKFormatError

        # ── Decode bytes → text ────────────────────────────────────
        try:
            csv_text = file_bytes.decode("utf-8-sig")
        except UnicodeDecodeError:
            try:
                csv_text = file_bytes.decode("latin-1")
            except Exception:
                raise InvalidDKFormatError(
                    "File could not be decoded — expected a UTF-8 or "
                    "Latin-1 encoded CSV file."
                )

        if not csv_text.strip():
            raise InvalidDKFormatError("Uploaded file is empty.")

        # ── Parse using the existing CSV engine ────────────────────
        try:
            raw_entries, roster_slots = self._parse_csv(csv_text)
        except ValueError as exc:
            # _parse_csv raises ValueError for missing headers / slots
            raise InvalidDKFormatError(str(exc)) from exc

        if not raw_entries:
            raise InvalidDKFormatError(
                "No valid entry rows found in the CSV. Make sure this "
                "is a DraftKings entries file (not the player pool)."
            )

        # ── Group entries by contest ───────────────────────────────
        from collections import OrderedDict

        contest_map: OrderedDict[str, dict] = OrderedDict()

        for entry in raw_entries:
            cid = entry["contest_id"] or "unknown"
            cname = entry["contest_name"] or ""
            fee_raw = entry.get("fee", 0)
            # Format fee as dollar string for display
            fee_str = f"${fee_raw:.2f}" if isinstance(fee_raw, (int, float)) and fee_raw > 0 else str(fee_raw)

            # Build current_roster: slot → display name
            current_roster: Dict[str, str] = {s: "" for s in roster_slots}
            for p in entry.get("players", []):
                slot = p["slot"]
                name = p.get("raw_name") or ""
                # If we only have a dk_player_id but no name, show the ID
                if not name and p.get("dk_player_id"):
                    name = str(p["dk_player_id"])
                current_roster[slot] = name

            parsed_entry = DKParsedEntry(
                entry_id=str(entry["entry_id"]),
                current_roster=current_roster,
            )

            if cid not in contest_map:
                contest_map[cid] = {
                    "contest_id": str(cid),
                    "contest_name": cname,
                    "entry_fee": fee_str,
                    "entries": [],
                }
            contest_map[cid]["entries"].append(parsed_entry)

        contests = [
            DKParsedContest(**cdata) for cdata in contest_map.values()
        ]

        logger.info(
            "[EntryImport] Parsed %d entries across %d contests "
            "(slots: %s)",
            len(raw_entries), len(contests),
            ", ".join(roster_slots),
        )

        return DKParsedEntriesResponse(
            contests=contests,
            roster_slots=roster_slots,
            total_entries=len(raw_entries),
            total_contests=len(contests),
        )

    # ── Player Cell Format Detection ─────────────────────────────

    @staticmethod
    def _detect_player_cell_format(
        rows: List[List[str]],
        roster_indices: List[int],
    ) -> str:
        """Detect whether the original CSV uses ``Name (ID)`` or bare ID format.

        Scans entry rows (rows after the header) for the first non-empty
        player cell in a roster slot column.

        Returns
        -------
        str
            ``'name_id'`` if cells use ``"PlayerName (dk_player_id)"`` format,
            ``'bare_id'`` if cells contain only a numeric dk_player_id.
        """
        for row in rows[1:]:
            if len(row) < 4:
                break
            for ci in roster_indices:
                if ci >= len(row):
                    continue
                cell = row[ci].strip()
                if not cell:
                    continue
                # Check "Name (ID)" format first
                if _PLAYER_CELL_PATTERN.match(cell):
                    return "name_id"
                # Check bare numeric ID
                if cell.isdigit() and len(cell) >= 5:
                    return "bare_id"
        # Default to bare_id — safest for DK upload
        return "bare_id"

    # ── Auto-Fill: Lineup → Entry Assignment ───────────────────────

    def assign_lineups_to_entries(
        self,
        entry_ids: List[str],
        lineups: List[List[Dict]],
        allow_duplicates: bool = False,
    ) -> tuple[Dict[str, List[Dict]], List[str]]:
        """Map generated lineups to entry IDs via round-robin.

        Parameters
        ----------
        entry_ids : list of str
            Entry IDs from the parsed CSV, in original order.
        lineups : list of list of dict
            Each lineup is a list of player dicts with at minimum
            ``dk_player_id`` and ``display_name``, in roster slot order.
        allow_duplicates : bool
            If ``False``, a warning is emitted when lineups must be cycled
            (more entries than unique lineups).  If ``True``, cycling is
            expected behaviour (e.g. user wants their top lineup in all
            entries).

        Returns
        -------
        (assignment_map, warnings)
            assignment_map : dict of ``entry_id`` → lineup player list.
            warnings : list of warning strings.
        """
        warnings: List[str] = []
        num_entries = len(entry_ids)
        num_lineups = len(lineups)

        if num_lineups == 0:
            return {}, ["No lineups provided"]

        cycling = num_lineups < num_entries
        if cycling and not allow_duplicates:
            warnings.append(
                f"Only {num_lineups} unique lineup(s) for {num_entries} "
                f"entries — cycling lineups to fill all entries"
            )
            logger.warning(
                "[AutoFill] %d lineups for %d entries — cycling",
                num_lineups, num_entries,
            )

        assignment: Dict[str, List[Dict]] = {}
        for i, eid in enumerate(entry_ids):
            assignment[eid] = lineups[i % num_lineups]

        logger.info(
            "[AutoFill] Assigned %d lineups to %d entries "
            "(cycling=%s, allow_duplicates=%s)",
            min(num_lineups, num_entries), num_entries,
            cycling, allow_duplicates,
        )

        return assignment, warnings

    # ── Auto-Fill: CSV Reconstruction ──────────────────────────────

    def export_filled_dk_csv(
        self,
        original_csv_bytes: bytes,
        assignment: Dict[str, List[Dict]],
        player_cell_format: str = "bare_id",
    ) -> tuple[str, int, Dict[str, int]]:
        """Reconstruct the original DK CSV with assigned lineups.

        Only **overwrites roster slot columns** for entries present in
        ``assignment``.  All other columns — meta columns on the left
        (Entry ID, Contest Name, Contest ID, Entry Fee) and instruction /
        player-pool columns on the right — are preserved exactly as they
        appear in the original file.  Trailing rows (player reference
        table, blank rows) are also passed through untouched.

        Parameters
        ----------
        original_csv_bytes : bytes
            Raw bytes of the original uploaded DK entries CSV.
        assignment : dict
            ``entry_id`` → list of player dicts (each with ``dk_player_id``
            and ``display_name``), in roster slot order.
        player_cell_format : str
            ``'name_id'`` to write ``"Name (ID)"`` cells, or ``'bare_id'``
            to write bare ``dk_player_id`` values.

        Returns
        -------
        (filled_csv_str, entries_filled, contest_summary)
            filled_csv_str : the reconstructed CSV as a string.
            entries_filled : count of entry rows that were overwritten.
            contest_summary : ``{contest_name: count}`` dict.
        """
        # ── Decode ────────────────────────────────────────────────
        try:
            csv_text = original_csv_bytes.decode("utf-8-sig")
        except UnicodeDecodeError:
            csv_text = original_csv_bytes.decode("latin-1")

        csv_text = csv_text.lstrip("\ufeff").replace("\x00", "")

        reader = csv.reader(io.StringIO(csv_text))
        all_rows = list(reader)
        if not all_rows:
            return "", 0, {}

        header = all_rows[0]
        col_idx, _roster_slots, roster_indices = self._find_roster_column_indices(header)
        entry_id_col = col_idx["entry_id"]
        contest_name_col = col_idx.get("contest_name")

        # ── Overwrite roster slot columns ─────────────────────────
        entries_filled = 0
        contest_summary: Dict[str, int] = {}

        for row in all_rows[1:]:
            if len(row) < 4:
                continue  # blank / reference row — pass through

            raw_eid = row[entry_id_col].strip() if entry_id_col < len(row) else ""
            if not raw_eid:
                continue

            # Only overwrite rows whose entry_id is in the assignment
            if raw_eid not in assignment:
                continue

            lineup = assignment[raw_eid]
            for slot_idx, col_i in enumerate(roster_indices):
                if slot_idx < len(lineup):
                    player = lineup[slot_idx]
                    pid = player.get("dk_player_id", "")
                    name = player.get("display_name", "")
                    if player_cell_format == "name_id" and name and pid:
                        cell = f"{name} ({pid})"
                    else:
                        cell = str(pid) if pid else ""
                    # Ensure row is long enough
                    while len(row) <= col_i:
                        row.append("")
                    row[col_i] = cell
                else:
                    # More roster slots than lineup players — clear cell
                    while len(row) <= col_i:
                        row.append("")
                    row[col_i] = ""

            entries_filled += 1

            # Track contest summary
            cname = ""
            if contest_name_col is not None and contest_name_col < len(row):
                cname = row[contest_name_col].strip()
            contest_summary[cname] = contest_summary.get(cname, 0) + 1

        # ── Write all rows back ───────────────────────────────────
        output = io.StringIO()
        writer = csv.writer(output, lineterminator="\n")
        for row in all_rows:
            writer.writerow(row)
        filled_csv = output.getvalue()

        logger.info(
            "[AutoFill] Filled %d entries in CSV (%d contests)",
            entries_filled, len(contest_summary),
        )

        return filled_csv, entries_filled, contest_summary

    # ── Player Resolution ─────────────────────────────────────────

    def _resolve_players(
        self,
        raw_entries: List[Dict],
        draftables,
    ) -> tuple:
        """Resolve CSV players to dk_player_id, salary, and team.

        Returns (resolved_entries, unresolved_list) where unresolved_list
        contains dicts of players that could not be matched.
        """
        # Build lookup maps
        dk_by_id: Dict[int, object] = {}
        dk_by_name: Dict[str, object] = {}
        for p in draftables:
            dk_by_id[p.dk_player_id] = p
            norm = normalize_player_name(p.display_name)
            # Keep the first (highest-salary) entry if duplicate names
            if norm not in dk_by_name:
                dk_by_name[norm] = p

        unresolved: List[Dict] = []
        resolved_entries: List[Dict] = []

        for entry in raw_entries:
            lineup: List[Dict] = []
            for p in entry["players"]:
                dk_id = p["dk_player_id"]
                raw_name = p["raw_name"]
                slot = p["slot"]

                dk_player = None

                # Tier 1: dk_player_id direct lookup
                if dk_id is not None:
                    dk_player = dk_by_id.get(dk_id)

                # Tier 2: normalized name exact match
                if dk_player is None and raw_name:
                    norm = normalize_player_name(raw_name)
                    dk_player = dk_by_name.get(norm)

                # Tier 3: fuzzy name match (handles suffixes, accents)
                if dk_player is None and raw_name:
                    for dp in draftables:
                        if _names_match(raw_name, dp.display_name):
                            dk_player = dp
                            break

                if dk_player is not None:
                    lineup.append({
                        "slot": slot,
                        "dk_player_id": dk_player.dk_player_id,
                        "player_name": dk_player.display_name,
                        "salary": dk_player.salary,
                        "team": dk_player.team_abbreviation,
                    })
                else:
                    # Unresolved — store with null fields
                    display = raw_name or f"dk_id={dk_id}"
                    lineup.append({
                        "slot": slot,
                        "dk_player_id": dk_id,
                        "player_name": raw_name or "",
                        "salary": 0,
                        "team": None,
                    })
                    unresolved.append({
                        "entry_id": entry["entry_id"],
                        "slot": slot,
                        "raw_name": raw_name,
                        "dk_player_id": dk_id,
                    })
                    logger.warning(
                        "[EntryImport] Unresolved player: '%s' "
                        "(dk_id=%s) in entry %s",
                        display, dk_id, entry["entry_id"],
                    )

            entry["lineup"] = lineup
            entry["total_salary"] = sum(p["salary"] for p in lineup)
            resolved_entries.append(entry)

        if unresolved:
            logger.warning(
                "[EntryImport] %d player(s) could not be resolved "
                "against draftables", len(unresolved),
            )

        return resolved_entries, unresolved

    # ── Main Import Method ────────────────────────────────────────

    async def import_entries(
        self,
        csv_text: str,
        draft_group_id: int,
        dk_draftables_service,
        sport: str = "nba",
    ) -> Dict:
        """Parse CSV, resolve players, upsert to DB.

        Parameters
        ----------
        csv_text : str
            Raw CSV content from the uploaded file.
        draft_group_id : int
            The DraftKings DraftGroup ID for the target slate.
        dk_draftables_service : DKDraftablesService
            Service instance for fetching draftable player data.
        sport : str
            Sport key: ``"nba"`` or ``"cbb"``.

        Returns
        -------
        dict
            Summary with total_imported, total_fees, entries, etc.
        """
        from app.db.database import is_db_available, get_session
        from app.db.models import ActiveEntry

        # Step 1: Parse the CSV
        raw_entries, _slots = self._parse_csv(csv_text)
        if not raw_entries:
            raise ValueError("No valid entries found in CSV")

        logger.info(
            "[EntryImport] Parsed %d entries from CSV for DG=%d",
            len(raw_entries), draft_group_id,
        )

        # Step 2: Fetch draftables for player resolution
        draftables = dk_draftables_service.get_draftables(draft_group_id)
        if not draftables:
            raise ValueError(
                f"No draftables found for draft_group_id={draft_group_id}. "
                f"Is this a valid DraftKings slate?"
            )

        # Step 3: Resolve players
        resolved, unresolved = self._resolve_players(raw_entries, draftables)

        # Step 4: Upsert to database
        persisted = False
        if is_db_available():
            try:
                from sqlalchemy.dialects.postgresql import insert as pg_insert

                async with get_session() as session:
                    for entry in resolved:
                        stmt = pg_insert(ActiveEntry).values(
                            entry_id=str(entry["entry_id"]),
                            contest_id=str(entry["contest_id"]),
                            contest_name=entry["contest_name"],
                            draft_group_id=draft_group_id,
                            fee=entry["fee"],
                            lineup=entry["lineup"],
                            total_salary=entry["total_salary"],
                            sport=sport,
                        ).on_conflict_do_update(
                            index_elements=["entry_id"],
                            set_={
                                "contest_name": entry["contest_name"],
                                "lineup": entry["lineup"],
                                "total_salary": entry["total_salary"],
                                "fee": entry["fee"],
                                "updated_at": datetime.now(timezone.utc),
                            },
                        )
                        await session.execute(stmt)

                    await session.commit()
                    persisted = True

                logger.info(
                    "[EntryImport] Upserted %d entries to DB (DG=%d)",
                    len(resolved), draft_group_id,
                )
            except Exception as e:
                logger.error(
                    "[EntryImport] DB upsert failed: %s", e, exc_info=True,
                )
        else:
            logger.warning("[EntryImport] DB unavailable — entries not persisted")

        # Step 5: Build response
        return self._build_response(resolved, unresolved, persisted)

    # ── Response Builder ──────────────────────────────────────────

    @staticmethod
    def _build_response(
        entries: List[Dict],
        unresolved: List[Dict],
        persisted: bool,
    ) -> Dict:
        """Build the JSON response payload."""
        total_fees = sum(e.get("fee", 0) for e in entries)
        contest_ids = set()
        contest_map: Dict[str, Dict] = {}
        for e in entries:
            cid = e["contest_id"]
            contest_ids.add(cid)
            if cid not in contest_map:
                contest_map[cid] = {
                    "contest_id": cid,
                    "contest_name": e["contest_name"],
                    "entry_count": 0,
                }
            contest_map[cid]["entry_count"] += 1

        return {
            "total_imported": len(entries),
            "total_fees": round(total_fees, 2),
            "contest_count": len(contest_ids),
            "contests": list(contest_map.values()),
            "unresolved_players": unresolved,
            "entries": [
                {
                    "entry_id": e["entry_id"],
                    "contest_id": e["contest_id"],
                    "contest_name": e["contest_name"],
                    "fee": e["fee"],
                    "total_salary": e["total_salary"],
                    "players": e["lineup"],
                }
                for e in entries
            ],
            "persisted": persisted,
        }

    # ── Retrieval ─────────────────────────────────────────────────

    async def get_entries_for_slate(
        self,
        draft_group_id: int,
        sport: str = "nba",
    ) -> List[Dict]:
        """Load all active entries for a draft group from DB."""
        from app.db.database import is_db_available, get_session
        from app.db.models import ActiveEntry
        from sqlalchemy import select

        if not is_db_available():
            return []

        async with get_session() as session:
            stmt = (
                select(ActiveEntry)
                .where(ActiveEntry.draft_group_id == draft_group_id)
                .where(ActiveEntry.sport == sport)
                .order_by(ActiveEntry.id)
            )
            result = await session.execute(stmt)
            rows = result.scalars().all()

        return [
            {
                "entry_id": r.entry_id,
                "contest_id": r.contest_id,
                "contest_name": r.contest_name,
                "fee": r.fee,
                "total_salary": r.total_salary,
                "lineup": r.lineup,
                "imported_at": r.imported_at.isoformat() if r.imported_at else None,
                "updated_at": r.updated_at.isoformat() if r.updated_at else None,
            }
            for r in rows
        ]

    # ── Deletion ──────────────────────────────────────────────────

    async def delete_entries_for_slate(
        self,
        draft_group_id: int,
        sport: str = "nba",
    ) -> int:
        """Delete all active entries for a draft group.  Returns count."""
        from app.db.database import is_db_available, get_session
        from app.db.models import ActiveEntry
        from sqlalchemy import delete

        if not is_db_available():
            return 0

        async with get_session() as session:
            stmt = (
                delete(ActiveEntry)
                .where(ActiveEntry.draft_group_id == draft_group_id)
                .where(ActiveEntry.sport == sport)
            )
            result = await session.execute(stmt)
            await session.commit()
            return result.rowcount
