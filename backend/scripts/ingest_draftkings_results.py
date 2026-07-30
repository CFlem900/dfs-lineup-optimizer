#!/usr/bin/env python3
"""DraftKings Contest Results → lineup_results backfill.

Parses a DK Contest History CSV, resolves player names to DK player IDs
using a companion Draftables CSV, reconstructs lineup hashes, and updates
the ``lineup_results`` table with actual scores and P&L data.

Matching strategy:
  1. Parse each CSV lineup string into 8 player (name, salary) pairs
  2. Resolve each player name to a DK player ID via the draftables CSV
     (exact → alias → last-name → fuzzy SequenceMatcher ≥ 0.82)
  3. Sort the 8 IDs and compute SHA-256 — same hash algorithm as
     ``SolverTrackingService._hash_lineup()``
  4. Match the hash against ``lineup_results.lineup_hash`` in the DB
  5. Update ``actual_score``, ``entry_fee_total``, ``winnings_total``,
     ``net_profit`` on the matched row(s)

When the same lineup was entered in multiple DK contests, entry fees
and winnings are aggregated (summed) before updating the DB record.

Usage
-----
::

    cd backend

    # Standard run:
    python scripts/ingest_draftkings_results.py contest-history.csv draftables.csv

    # Preview without DB writes:
    python scripts/ingest_draftkings_results.py contest-history.csv draftables.csv --dry-run

Required files
--------------
contest_csv
    DK "Contest History" CSV — download from My Contests → History.
    Must contain a ``Lineup`` column in compact DK format
    (``PG Name ($Salary) SG Name ($Salary) ...``).

draftables_csv
    DK "Draftables" CSV for the **same slate** — download from the
    DK lobby before the slate locks.  Must contain ``ID`` and ``Name``
    columns (various DK column-name variants are auto-detected).
    Provides the name → DK-player-ID mapping needed to reconstruct
    the lineup hash.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import io
import logging
import os
import re
import sys
import unicodedata
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ── Path setup ───────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).resolve().parent
BACKEND_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(BACKEND_DIR))

# Optional pandas — used if available, falls back to stdlib csv
try:
    import pandas as pd  # type: ignore[import-untyped]

    HAS_PANDAS = True
except ImportError:
    HAS_PANDAS = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────

FUZZY_THRESHOLD = 0.82

# DK compact lineup regex: "PG Jalen Brunson ($8,200) SG ..."
_DK_PLAYER_RE = re.compile(
    r"(PG|SG|SF|PF|C|G|F|UTIL)\s+"  # roster slot
    r"(.+?)\s+"                      # player name (non-greedy)
    r"\(\$(\d[\d,]*)\)"             # salary in parens
)

# Known DK ↔ canonical name aliases that fuzzy matching often misses.
KNOWN_ALIASES: Dict[str, str] = {
    "nic claxton":                "nicolas claxton",
    "nick claxton":               "nicolas claxton",
    "herb jones":                 "herbert jones",
    "cam thomas":                 "cameron thomas",
    "cam johnson":                "cameron johnson",
    "kenyon martin":              "kenyon martin jr",
    "dereck lively":              "dereck lively ii",
    "jabari smith":               "jabari smith jr",
    "jaren jackson":              "jaren jackson jr",
    "gary trent":                 "gary trent jr",
    "larry nance":                "larry nance jr",
    "marcus morris":              "marcus morris sr",
    "michael porter":             "michael porter jr",
    "tim hardaway":               "tim hardaway jr",
    "wendell carter":             "wendell carter jr",
    "kelly oubre":                "kelly oubre jr",
    "shai gilgeous alexander":    "shai gilgeous-alexander",
    "pj washington":              "pj washington",
    "tj mcconnell":               "tj mcconnell",
    "tj warren":                  "tj warren",
    "cj mccollum":                "cj mccollum",
    "rj barrett":                 "rj barrett",
    "aj griffin":                  "aj griffin",
    "kevin porter":               "kevin porter jr",
    "otto porter":                "otto porter jr",
    "al horford":                 "al horford",
    "naz reid":                   "naz reid",
}


# ── Name normalization ───────────────────────────────────────────────

def normalize_name(name: str) -> str:
    """Normalize a player name for matching.

    Identical to ``player_id_mapper.normalize_name()``:
    lowercase → NFKD → strip combining marks → remove suffixes
    (Jr./Sr./III/IV/II) → remove periods → collapse whitespace.
    """
    n = name.strip().lower()
    n = unicodedata.normalize("NFKD", n)
    n = "".join(c for c in n if not unicodedata.combining(c))
    n = re.sub(r"\b(jr\.?|sr\.?|iii|iv|ii)\s*$", "", n).strip()
    n = n.replace(".", "")
    n = re.sub(r"\s+", " ", n).strip()
    return n


# ── Parse DK lineup string ──────────────────────────────────────────

def parse_dk_lineup(lineup_str: str) -> List[Dict[str, Any]]:
    """Parse a compact DK lineup string into structured player data.

    Input:  ``"PG Jalen Brunson ($8,200) SG Jaylen Brown ($7,400) ..."``
    Output: ``[{"slot": "PG", "name": "Jalen Brunson", "salary": 8200}, ...]``
    """
    if not lineup_str or not lineup_str.strip():
        return []

    players: List[Dict[str, Any]] = []
    for m in _DK_PLAYER_RE.finditer(lineup_str):
        players.append({
            "slot": m.group(1),
            "name": m.group(2).strip(),
            "salary": int(m.group(3).replace(",", "")),
        })
    return players


# ── Load DK draftables CSV → name-to-ID mapping ─────────────────────

def load_draftables(csv_path: str) -> Dict[str, int]:
    """Load a DK Draftables CSV and return ``normalized_name → dk_player_id``.

    Handles several DK column-name variants:
      * ID / PlayerId / Player_ID / DraftableId
      * Name / PlayerName / Player_Name / Display Name / Name + ID
    """
    mapping: Dict[str, int] = {}

    with open(csv_path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fields_raw = reader.fieldnames or []

        # Normalise column names for flexible matching
        fields: Dict[str, str] = {}
        for fn in fields_raw:
            clean = (
                fn.strip()
                .lower()
                .replace(" ", "_")
                .replace("+", "_")
            )
            fields[clean] = fn

        # Locate the ID column
        id_col: Optional[str] = None
        for candidate in (
            "id", "playerid", "player_id",
            "draftableid", "draftable_id",
        ):
            if candidate in fields:
                id_col = fields[candidate]
                break

        # Locate the Name column
        name_col: Optional[str] = None
        for candidate in (
            "name", "playername", "player_name",
            "display_name", "name___id",
        ):
            if candidate in fields:
                name_col = fields[candidate]
                break

        if not id_col or not name_col:
            raise ValueError(
                f"Draftables CSV missing required columns.\n"
                f"  Found columns: {fields_raw}\n"
                f"  Need: an ID column (e.g. 'ID') + a Name column (e.g. 'Name')"
            )

        for row in reader:
            try:
                raw_id = (row.get(id_col, "") or "").strip()
                raw_name = (row.get(name_col, "") or "").strip()

                # Handle DK "Name + ID" format: "Jalen Brunson (19663046)"
                if "(" in raw_name and ")" in raw_name:
                    name_part = raw_name.split("(")[0].strip()
                else:
                    name_part = raw_name

                dk_id = int(raw_id)
                if name_part and dk_id > 0:
                    mapping[normalize_name(name_part)] = dk_id
            except (ValueError, KeyError, TypeError):
                continue

    logger.info(f"Loaded {len(mapping)} players from draftables: {csv_path}")
    return mapping


# ── Fuzzy name resolution ────────────────────────────────────────────

def resolve_name_to_id(
    name: str,
    draftables: Dict[str, int],
    threshold: float = FUZZY_THRESHOLD,
) -> Tuple[Optional[int], str]:
    """Resolve a DK player name to a DK player ID.

    Tries (in order): exact → alias → last-name → fuzzy.
    Returns ``(dk_player_id, match_method)`` or ``(None, "unresolved")``.
    """
    norm = normalize_name(name)

    # 1. Exact normalised match
    if norm in draftables:
        return draftables[norm], "exact"

    # 2. Known alias
    alias = KNOWN_ALIASES.get(norm)
    if alias:
        alias_norm = normalize_name(alias)
        if alias_norm in draftables:
            return draftables[alias_norm], "alias"

    # 3. Last-name match (only if unique within the draftables)
    parts = norm.split()
    last = parts[-1] if parts else norm
    last_matches = [
        (dn, did) for dn, did in draftables.items()
        if dn.split()[-1] == last
    ]
    if len(last_matches) == 1:
        return last_matches[0][1], "last_name"

    # 4. Fuzzy SequenceMatcher
    best_ratio = 0.0
    best_id: Optional[int] = None
    for dn, did in draftables.items():
        ratio = SequenceMatcher(None, norm, dn).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_id = did

    if best_ratio >= threshold and best_id is not None:
        logger.debug(
            f"Fuzzy matched '{name}' → ID {best_id} (ratio={best_ratio:.3f})"
        )
        return best_id, "fuzzy"

    return None, "unresolved"


# ── Compute lineup hash ─────────────────────────────────────────────

def compute_lineup_hash(player_ids: List[int]) -> str:
    """SHA-256 of sorted, comma-joined player IDs.

    Identical to ``SolverTrackingService._hash_lineup()``.
    """
    sorted_ids = sorted(player_ids)
    return hashlib.sha256(
        ",".join(str(pid) for pid in sorted_ids).encode()
    ).hexdigest()


# ── Numeric parsing helpers ──────────────────────────────────────────

def _safe_float(val: Any) -> float:
    """Parse a numeric value, tolerating NaN / empty / None."""
    if val is None:
        return 0.0
    s = str(val).strip()
    if not s or s.lower() == "nan":
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def _safe_money(val: Any) -> float:
    """Parse a money value, stripping ``$`` and commas."""
    if val is None:
        return 0.0
    s = str(val).strip().replace("$", "").replace(",", "")
    if not s or s.lower() == "nan":
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


# ── Parse DK Contest History CSV ─────────────────────────────────────

# Flexible column-name mapping for the contest CSV.
_CONTEST_COL_CANDIDATES: Dict[str, List[str]] = {
    "points":       ["points", "fpts", "fantasy_points"],
    "winnings":     ["winnings", "winnings($)", "prize", "payout"],
    "entry_fee":    ["entry_fee", "buy_in", "entryfee", "fee"],
    "lineup":       ["lineup"],
    "contest_name": ["contest_name", "contestname", "name", "title"],
    "contest_date": ["contest_date_est", "contest_date", "date"],
    "entry_id":     ["entry_id", "entryid"],
    "sport":        ["sport"],
    "place":        ["place", "rank", "position"],
}


def parse_contest_csv(csv_path: str) -> List[Dict[str, Any]]:
    """Parse a DK Contest History CSV into a list of entry dicts.

    Uses pandas if available for robust encoding/type handling,
    otherwise falls back to the stdlib ``csv`` module.
    """
    if HAS_PANDAS:
        return _parse_contest_csv_pandas(csv_path)
    return _parse_contest_csv_stdlib(csv_path)


def _build_col_map_from_list(
    columns: List[str],
) -> Dict[str, str]:
    """Build standard-name → actual-name mapping from column list."""
    norm_to_orig: Dict[str, str] = {}
    for col in columns:
        clean = (
            col.strip()
            .lower()
            .replace(" ", "_")
            .replace("-", "_")
        )
        norm_to_orig[clean] = col

    col_map: Dict[str, str] = {}
    for key, candidates in _CONTEST_COL_CANDIDATES.items():
        for c in candidates:
            if c in norm_to_orig:
                col_map[key] = norm_to_orig[c]
                break

    if "lineup" not in col_map:
        raise ValueError(
            f"Contest CSV missing 'Lineup' column.\n"
            f"  Found columns: {columns}"
        )

    return col_map


def _row_to_entry(
    row: Dict[str, Any],
    col_map: Dict[str, str],
) -> Optional[Dict[str, Any]]:
    """Convert a single CSV/DF row into a normalised entry dict."""
    lineup_str = str(row.get(col_map.get("lineup", ""), "") or "").strip()
    if not lineup_str or lineup_str.lower() == "nan":
        return None

    players = parse_dk_lineup(lineup_str)
    if not players:
        return None

    points = _safe_float(row.get(col_map.get("points", ""), 0))
    entry_fee = _safe_money(row.get(col_map.get("entry_fee", ""), 0))
    winnings = _safe_money(row.get(col_map.get("winnings", ""), 0))

    return {
        "points": points,
        "entry_fee": entry_fee,
        "winnings": winnings,
        "contest_name": str(
            row.get(col_map.get("contest_name", ""), "")
        ).strip(),
        "contest_date": str(
            row.get(col_map.get("contest_date", ""), "")
        ).strip(),
        "entry_id": str(
            row.get(col_map.get("entry_id", ""), "")
        ).strip(),
        "sport": str(
            row.get(col_map.get("sport", ""), "")
        ).strip().lower(),
        "place": str(
            row.get(col_map.get("place", ""), "")
        ).strip(),
        "players": players,
        "total_salary": sum(p["salary"] for p in players),
    }


def _parse_contest_csv_pandas(csv_path: str) -> List[Dict[str, Any]]:
    """Parse using pandas for robust CSV handling."""
    df = pd.read_csv(csv_path, encoding="utf-8-sig", dtype=str)
    df.fillna("", inplace=True)

    col_map = _build_col_map_from_list(list(df.columns))

    entries: List[Dict[str, Any]] = []
    for _, row in df.iterrows():
        entry = _row_to_entry(row.to_dict(), col_map)
        if entry is not None:
            entries.append(entry)

    logger.info(f"Parsed {len(entries)} entries from contest CSV (pandas)")
    return entries


def _parse_contest_csv_stdlib(csv_path: str) -> List[Dict[str, Any]]:
    """Parse using stdlib csv module as fallback."""
    entries: List[Dict[str, Any]] = []

    with open(csv_path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        col_map = _build_col_map_from_list(list(reader.fieldnames or []))

        for row in reader:
            entry = _row_to_entry(row, col_map)
            if entry is not None:
                entries.append(entry)

    logger.info(f"Parsed {len(entries)} entries from contest CSV (stdlib)")
    return entries


# ── Main async logic ─────────────────────────────────────────────────

async def run(args: argparse.Namespace) -> None:
    """Main ingestion pipeline."""

    # ── 1. Load draftables name → ID mapping ─────────────────────────
    logger.info(f"Loading draftables from: {args.draftables_csv}")
    draftables = load_draftables(args.draftables_csv)
    if not draftables:
        logger.error("No players loaded from draftables CSV — aborting")
        sys.exit(1)

    # ── 2. Parse contest history CSV ─────────────────────────────────
    logger.info(f"Parsing contest history from: {args.contest_csv}")
    entries = parse_contest_csv(args.contest_csv)
    if not entries:
        logger.error("No entries parsed from contest CSV — aborting")
        sys.exit(1)

    # ── 3. Resolve names → IDs and compute lineup hashes ─────────────
    logger.info("Resolving player names to DK player IDs...")

    resolved_entries: List[Dict[str, Any]] = []
    partial_entries: List[Dict[str, Any]] = []
    unresolved_names: Dict[str, int] = defaultdict(int)
    resolution_stats: Dict[str, int] = defaultdict(int)

    for entry in entries:
        player_ids: List[int] = []
        all_resolved = True

        for p in entry["players"]:
            dk_id, method = resolve_name_to_id(p["name"], draftables)
            if dk_id is not None:
                player_ids.append(dk_id)
                resolution_stats[method] += 1
            else:
                all_resolved = False
                unresolved_names[p["name"]] += 1

        if all_resolved and len(player_ids) == len(entry["players"]):
            entry["player_ids"] = player_ids
            entry["lineup_hash"] = compute_lineup_hash(player_ids)
            resolved_entries.append(entry)
        else:
            entry["player_ids"] = player_ids
            entry["lineup_hash"] = None
            partial_entries.append(entry)

    # Report resolution stats
    total_players = sum(len(e["players"]) for e in entries)
    total_resolved = sum(resolution_stats.values())
    logger.info(
        f"Name resolution: {total_resolved}/{total_players} players "
        f"(exact={resolution_stats.get('exact', 0)}, "
        f"alias={resolution_stats.get('alias', 0)}, "
        f"last_name={resolution_stats.get('last_name', 0)}, "
        f"fuzzy={resolution_stats.get('fuzzy', 0)})"
    )

    if unresolved_names:
        sorted_unresolved = sorted(
            unresolved_names.items(), key=lambda x: -x[1]
        )
        top = sorted_unresolved[:10]
        logger.warning(
            f"Could not resolve {len(unresolved_names)} unique player names: "
            + ", ".join(f"'{n}' (x{c})" for n, c in top)
            + (
                f" ... +{len(unresolved_names) - 10} more"
                if len(unresolved_names) > 10
                else ""
            )
        )

    logger.info(
        f"Fully hashed entries: {len(resolved_entries)}/{len(entries)} "
        f"({len(partial_entries)} partially resolved)"
    )

    if not resolved_entries:
        logger.error(
            "No entries could be fully resolved. Check draftables CSV."
        )
        sys.exit(1)

    # ── 4. Group by lineup_hash → aggregate P&L across contests ──────
    #    Same lineup entered in N different contests → sum fees & winnings
    hash_groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for entry in resolved_entries:
        hash_groups[entry["lineup_hash"]].append(entry)

    logger.info(
        f"Unique lineup hashes: {len(hash_groups)} "
        f"(from {len(resolved_entries)} contest entries)"
    )

    # ── 5. Initialise database ───────────────────────────────────────
    logger.info("Connecting to database...")

    from app.db.database import init_db, get_session, close_db
    from app.db.models import LineupResult

    db_ok = await init_db()
    if not db_ok:
        logger.error(
            "Database init failed. "
            "Ensure DATABASE_URL env var is set and the DB is reachable."
        )
        sys.exit(1)

    # ── 6. Match lineup hashes and update records ────────────────────
    from sqlalchemy import select

    matched_hashes = 0
    matched_rows = 0
    orphan_entries: List[Dict[str, Any]] = []

    try:
        async with get_session() as session:
            for lineup_hash, contest_entries in hash_groups.items():
                # Aggregate P&L across all contests for this lineup
                total_fee = sum(e["entry_fee"] for e in contest_entries)
                total_win = sum(e["winnings"] for e in contest_entries)
                # All entries for the same lineup have the same actual score
                actual_pts = contest_entries[0]["points"]
                net_profit = total_win - total_fee

                # Find matching lineup_result row(s)
                stmt = select(LineupResult).where(
                    LineupResult.lineup_hash == lineup_hash
                )
                result = await session.execute(stmt)
                rows = result.scalars().all()

                if not rows:
                    orphan_entries.extend(contest_entries)
                    continue

                matched_hashes += 1

                # Update each matching row with actual results
                for row in rows:
                    if not args.dry_run:
                        row.actual_score = actual_pts
                        row.entry_fee_total = total_fee
                        row.winnings_total = total_win
                        row.net_profit = net_profit
                    matched_rows += 1

            if not args.dry_run:
                await session.commit()
                logger.info(
                    f"Committed updates: {matched_rows} row(s) across "
                    f"{matched_hashes} unique lineups"
                )
            else:
                logger.info(
                    f"DRY RUN — would update {matched_rows} row(s) across "
                    f"{matched_hashes} unique lineups"
                )

    except Exception as e:
        logger.error(f"Database operation failed: {e}")
        raise
    finally:
        await close_db()

    # ── 7. Print summary ─────────────────────────────────────────────
    _print_summary(
        entries=entries,
        resolved=resolved_entries,
        partial=partial_entries,
        hash_groups=hash_groups,
        matched_hashes=matched_hashes,
        matched_rows=matched_rows,
        orphan_entries=orphan_entries,
        unresolved_names=unresolved_names,
        resolution_stats=resolution_stats,
        dry_run=args.dry_run,
    )


# ── Summary printer ─────────────────────────────────────────────────

def _print_summary(
    entries: List[Dict],
    resolved: List[Dict],
    partial: List[Dict],
    hash_groups: Dict[str, List[Dict]],
    matched_hashes: int,
    matched_rows: int,
    orphan_entries: List[Dict],
    unresolved_names: Dict[str, int],
    resolution_stats: Dict[str, int],
    dry_run: bool,
) -> None:
    """Print a human-readable ingestion summary."""
    print()
    print("=" * 64)
    print("  DraftKings Results Ingestion Summary")
    print("=" * 64)
    print()

    # ── Parsing stats
    print("  Parsing")
    print(f"    CSV entries read:          {len(entries)}")
    print(f"    Entries with lineup:       {len(entries)}")
    print(f"    Fully resolved (hashed):   {len(resolved)}")
    print(f"    Partially resolved:        {len(partial)}")
    print()

    # ── Name resolution
    total_players = sum(len(e["players"]) for e in entries)
    total_resolved = sum(resolution_stats.values())
    print("  Name Resolution")
    print(f"    Players in CSV:            {total_players}")
    print(f"    Resolved to DK IDs:        {total_resolved}")
    print(f"      - exact:                 {resolution_stats.get('exact', 0)}")
    print(f"      - alias:                 {resolution_stats.get('alias', 0)}")
    print(f"      - last_name:             {resolution_stats.get('last_name', 0)}")
    print(f"      - fuzzy:                 {resolution_stats.get('fuzzy', 0)}")
    print(f"    Unresolved:                {len(unresolved_names)}")
    print()

    # ── Database matching
    print("  Database Matching")
    print(f"    Unique lineup hashes:      {len(hash_groups)}")
    print(f"    Matched in DB:             {matched_hashes} lineups")
    print(f"    DB rows updated:           {matched_rows}")
    print(f"    Orphans (no DB match):     {len(orphan_entries)}")
    if dry_run:
        print(f"    Mode:                      DRY RUN (no writes)")
    print()

    # ── P&L summary (all resolved entries, regardless of DB match)
    total_fees = sum(e["entry_fee"] for e in resolved)
    total_winnings = sum(e["winnings"] for e in resolved)
    total_profit = total_winnings - total_fees

    print("  P&L (all resolved entries)")
    print(f"    Total entry fees:          ${total_fees:>10,.2f}")
    print(f"    Total winnings:            ${total_winnings:>10,.2f}")
    print(f"    Net profit:                ${total_profit:>10,.2f}")
    if total_fees > 0:
        roi = (total_profit / total_fees) * 100
        print(f"    ROI:                       {roi:>10.1f}%")
    print()

    # ── Orphan details
    if orphan_entries:
        # Deduplicate by lineup_hash
        seen_hashes: set = set()
        unique_orphans: List[Dict] = []
        for o in orphan_entries:
            h = o.get("lineup_hash") or "unknown"
            if h not in seen_hashes:
                seen_hashes.add(h)
                unique_orphans.append(o)

        print(
            f"  Orphan Lineups ({len(unique_orphans)} unique — "
            f"generated by you but not found in lineup_results):"
        )
        for i, o in enumerate(unique_orphans[:10]):
            names = ", ".join(p["name"] for p in o["players"][:3])
            print(
                f"    {i+1:>2}. {o.get('contest_name', '')[:38]:38s}  "
                f"{o['points']:6.1f}pts  "
                f"${o['entry_fee']:>6.0f} -> ${o['winnings']:>6.0f}  "
                f"({names}...)"
            )
        remaining = len(unique_orphans) - 10
        if remaining > 0:
            print(f"        ... and {remaining} more")
        print()

    print("=" * 64)
    print()


# ── CLI entry point ──────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Ingest DraftKings contest results into the lineup_results "
            "table. Matches DK lineups to optimizer-generated lineups by "
            "reconstructing the same SHA-256 lineup hash, then updates "
            "actual_score, entry_fee_total, winnings_total, and net_profit."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python scripts/ingest_draftkings_results.py contest-history.csv DKSalaries.csv
  python scripts/ingest_draftkings_results.py contest-history.csv DKSalaries.csv --dry-run

Required files:
  contest_csv     DK "Contest History" CSV (My Contests > History > Download)
  draftables_csv  DK Draftables/Salaries CSV for the same slate
                  (provides the player Name -> ID mapping)
""",
    )

    parser.add_argument(
        "contest_csv",
        help="Path to DraftKings Contest History CSV",
    )
    parser.add_argument(
        "draftables_csv",
        help="Path to DK Draftables/Salaries CSV (name -> player ID mapping)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview matching without writing to the database",
    )

    args = parser.parse_args()

    # Validate files exist
    for attr in ("contest_csv", "draftables_csv"):
        fpath = Path(getattr(args, attr))
        if not fpath.exists():
            print(f"ERROR: File not found: {fpath}")
            sys.exit(1)

    asyncio.run(run(args))


if __name__ == "__main__":
    main()
