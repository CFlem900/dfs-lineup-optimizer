"""Tournament Results CSV Import Service.

Parses DraftKings contest export CSVs and stores results in the
tournament_contest / tournament_entry tables.

Handles two DK CSV formats:
  1. **Compact**: Lineup column contains the full lineup string
     ``PG Jalen Brunson ($8,200) SG Jaylen Brown ($7,400) ...``
  2. **Expanded**: Lineup column is empty; each entry is followed by
     continuation rows with ``Player`` and ``Roster Position`` columns.

Also handles UTF-8 BOM prefix in CSV header.

Designed to be triggered via POST /api/tournament/import.
"""

import csv
import io
import logging
import math
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Regex to parse DK lineup column: "PG Player Name ($12,300)"
_DK_PLAYER_PATTERN = re.compile(
    r"(PG|SG|SF|PF|C|G|F|UTIL)\s+"  # roster slot
    r"(.+?)\s+"  # player name (non-greedy)
    r"\(\$(\d[\d,]*)\)"  # salary in parens
)


def _normalize_fieldnames(fieldnames: List[str]) -> Dict[str, str]:
    """Build a map from normalised column name to original name.

    Strips BOM (\ufeff), leading/trailing whitespace, and lowercases
    so lookups are resilient to minor CSV formatting differences.
    """
    mapping: Dict[str, str] = {}
    for fn in fieldnames:
        clean = fn.strip().lstrip("\ufeff").strip()
        mapping[clean.lower()] = fn
    return mapping


def _get(row: Dict, col_map: Dict[str, str], key: str, default=""):
    """Case- and BOM-insensitive column getter."""
    original = col_map.get(key.lower())
    if original is None:
        return default
    return row.get(original, default)


class TournamentImportService:
    """Imports DraftKings contest export CSVs into the database."""

    async def import_csv(
        self,
        csv_content: str,
        contest_date: str,
        contest_name: Optional[str] = None,
        contest_type: str = "gpp",
    ) -> Dict:
        """Parse a DK contest export CSV and store results.

        Parameters
        ----------
        csv_content : str
            Raw CSV text content from the uploaded file.
        contest_date : str
            Date of the contest (YYYY-MM-DD).
        contest_name : str, optional
            Human-readable contest name.
        contest_type : str
            "gpp", "cash", or "single_entry".

        Returns
        -------
        dict with keys: contest_id, entries_imported, top_1pct_count
        """
        from app.db.database import is_db_available, get_session
        from app.db.models import TournamentContest, TournamentEntry

        if not is_db_available():
            raise RuntimeError("Database not available")

        # Strip UTF-8 BOM if present
        csv_content = csv_content.lstrip("\ufeff")

        reader = csv.DictReader(io.StringIO(csv_content))
        fieldnames = list(reader.fieldnames or [])
        col_map = _normalize_fieldnames(fieldnames)
        rows = list(reader)

        if not rows:
            raise ValueError("CSV is empty or has no data rows")

        # Detect format: does the CSV have Player/Roster Position columns?
        has_player_col = "player" in col_map
        has_lineup_col = "lineup" in col_map

        # Group rows into entries.
        # In "expanded" format, an entry row has Rank populated; continuation
        # rows (with player data) have Rank empty.
        entries = self._group_entries(rows, col_map, has_player_col, has_lineup_col)

        total_entries = len(entries)
        if total_entries == 0:
            raise ValueError("No valid entries found in CSV")

        top_1pct_cutoff = max(1, int(total_entries * 0.01))

        async with get_session() as session:
            contest = TournamentContest(
                contest_name=contest_name or f"DK Contest {contest_date}",
                contest_date=datetime.fromisoformat(contest_date).replace(
                    tzinfo=timezone.utc
                ),
                platform="dk",
                entry_count=total_entries,
                contest_type=contest_type,
                source="csv",
                import_metadata={
                    "columns": fieldnames,
                    "row_count": len(rows),
                    "entry_count": total_entries,
                    "format": "expanded" if has_player_col else "compact",
                },
            )
            session.add(contest)
            await session.flush()  # get contest.id

            entries_imported = 0
            for entry_data in entries:
                rank = entry_data["rank"]
                entry = TournamentEntry(
                    contest_id_fk=contest.id,
                    rank=rank,
                    entry_id=entry_data.get("entry_id", ""),
                    entry_name=entry_data.get("entry_name", ""),
                    points=entry_data["points"],
                    lineup_data=entry_data["players"],
                    total_salary=entry_data["total_salary"],
                    is_winner=1 if rank <= top_1pct_cutoff else 0,
                )
                session.add(entry)
                entries_imported += 1

            await session.commit()

        logger.info(
            f"[TournamentImport] Imported {entries_imported} entries "
            f"({total_entries} total) for {contest_date}, "
            f"top 1%% cutoff: rank <= {top_1pct_cutoff}, "
            f"format={'expanded' if has_player_col else 'compact'}"
        )
        return {
            "contest_id": contest.id,
            "entries_imported": entries_imported,
            "top_1pct_count": top_1pct_cutoff,
        }

    def _group_entries(
        self,
        rows: List[Dict],
        col_map: Dict[str, str],
        has_player_col: bool,
        has_lineup_col: bool,
    ) -> List[Dict]:
        """Group CSV rows into entry dicts with parsed lineup data.

        Handles both compact (all-in-one Lineup column) and expanded
        (Player/Roster Position sub-rows) DK CSV formats.
        """
        entries: List[Dict] = []
        current_entry: Optional[Dict] = None

        for row in rows:
            rank_str = (_get(row, col_map, "Rank") or "").strip()
            points_str = (_get(row, col_map, "Points") or "").strip()

            # Detect if this is an entry header row (has a Rank value)
            is_entry_row = bool(rank_str and rank_str != "0" and rank_str.isdigit())

            # Fallback: also treat row as entry row if it has an EntryId
            if not is_entry_row:
                entry_id = (_get(row, col_map, "EntryId") or "").strip()
                if entry_id and rank_str.isdigit():
                    is_entry_row = True

            if is_entry_row:
                # Save previous entry
                if current_entry:
                    entries.append(current_entry)

                try:
                    rank = int(rank_str)
                    points = float(points_str) if points_str else 0.0
                except (ValueError, TypeError):
                    continue

                lineup_raw = _get(row, col_map, "Lineup") or ""
                parsed_from_lineup = self._parse_dk_lineup(lineup_raw)

                current_entry = {
                    "rank": rank,
                    "entry_id": _get(row, col_map, "EntryId"),
                    "entry_name": _get(row, col_map, "EntryName"),
                    "points": points,
                    "players": parsed_from_lineup,
                    "total_salary": sum(p["salary"] for p in parsed_from_lineup),
                }

                # If this row also has a Player column value, add it
                if has_player_col:
                    player_name = (_get(row, col_map, "Player") or "").strip()
                    roster_pos = (_get(row, col_map, "Roster Position") or "").strip()
                    fpts_str = (_get(row, col_map, "FPTS") or "").strip()
                    if player_name and not parsed_from_lineup:
                        # Expanded format: collect players from sub-rows
                        fpts = 0.0
                        try:
                            fpts = float(fpts_str) if fpts_str else 0.0
                        except (ValueError, TypeError):
                            pass
                        current_entry["players"].append({
                            "roster_slot": roster_pos or "UTIL",
                            "player_name": player_name,
                            "salary": 0,
                            "fpts": fpts,
                        })

            elif current_entry and has_player_col:
                # Continuation row: Player data for the current entry
                player_name = (_get(row, col_map, "Player") or "").strip()
                roster_pos = (_get(row, col_map, "Roster Position") or "").strip()
                fpts_str = (_get(row, col_map, "FPTS") or "").strip()
                if player_name:
                    fpts = 0.0
                    try:
                        fpts = float(fpts_str) if fpts_str else 0.0
                    except (ValueError, TypeError):
                        pass
                    current_entry["players"].append({
                        "roster_slot": roster_pos or "UTIL",
                        "player_name": player_name,
                        "salary": 0,
                        "fpts": fpts,
                    })

        # Don't forget the last entry
        if current_entry:
            entries.append(current_entry)

        # Recalculate total_salary for expanded format entries
        for entry in entries:
            if entry["total_salary"] == 0 and entry["players"]:
                entry["total_salary"] = sum(
                    p.get("salary", 0) for p in entry["players"]
                )

        return entries

    @staticmethod
    def _parse_dk_lineup(lineup_str: str) -> List[Dict]:
        """Parse a DK lineup string into structured player data.

        DK format: "PG LeBron James ($10200) SG Jaylen Brown ($7800) ..."
        """
        if not lineup_str or not lineup_str.strip():
            return []
        players = []
        for match in _DK_PLAYER_PATTERN.finditer(lineup_str):
            slot = match.group(1)
            name = match.group(2).strip()
            salary = int(match.group(3).replace(",", ""))
            players.append({
                "roster_slot": slot,
                "player_name": name,
                "salary": salary,
            })
        return players

    # ------------------------------------------------------------------
    # Ownership accuracy tracking
    # ------------------------------------------------------------------

    async def compute_actual_ownership(
        self, contest_id: int
    ) -> Dict[str, float]:
        """Derive actual field ownership % for each player from imported entries.

        Returns dict mapping normalised player_name → ownership percentage (0-100).
        """
        from app.db.database import is_db_available, get_session
        from app.db.models import TournamentEntry

        if not is_db_available():
            return {}

        from sqlalchemy import select

        async with get_session() as session:
            result = await session.execute(
                select(TournamentEntry.lineup_data).where(
                    TournamentEntry.contest_id_fk == contest_id
                )
            )
            rows = result.scalars().all()

        if not rows:
            return {}

        player_counts: Counter = Counter()
        total_entries = len(rows)

        for lineup_data in rows:
            if not lineup_data:
                continue
            seen_names = set()
            for player in lineup_data:
                name = (player.get("player_name") or "").strip().lower()
                if name and name not in seen_names:
                    player_counts[name] += 1
                    seen_names.add(name)

        # Convert counts → ownership %
        actual_ownership = {
            name: round(count / total_entries * 100, 2)
            for name, count in player_counts.items()
        }
        return actual_ownership

    async def compute_ownership_accuracy(
        self,
        contest_id: int,
        projected_ownership: Dict[str, float],
    ) -> Optional[Dict]:
        """Compare projected ownership vs actual field ownership.

        Parameters
        ----------
        contest_id : int
            The contest to compute actuals for.
        projected_ownership : dict
            Mapping of normalised player_name → projected ownership % (0-100).

        Returns
        -------
        dict with accuracy metrics:
            - mae: mean absolute error across all matched players
            - rmse: root mean squared error
            - correlation: Pearson correlation coefficient
            - n_matched: number of players with both projected and actual
            - worst_misses: top 10 largest absolute errors
            - by_tier: accuracy split by ownership tier (chalk/mid/low)
        """
        actual = await self.compute_actual_ownership(contest_id)
        if not actual or not projected_ownership:
            return None

        # Normalise projected keys to lowercase
        proj_norm = {k.lower(): v for k, v in projected_ownership.items()}

        errors = []
        details = []
        for name in set(actual.keys()) & set(proj_norm.keys()):
            p = proj_norm[name]
            a = actual[name]
            err = a - p  # positive = we under-projected
            errors.append(err)
            details.append({
                "player": name,
                "projected": p,
                "actual": a,
                "error": round(err, 2),
            })

        if not errors:
            return None

        n = len(errors)
        mae = sum(abs(e) for e in errors) / n
        rmse = math.sqrt(sum(e * e for e in errors) / n)

        # Pearson correlation
        proj_vals = [d["projected"] for d in details]
        act_vals = [d["actual"] for d in details]
        mean_p = sum(proj_vals) / n
        mean_a = sum(act_vals) / n
        cov = sum((p - mean_p) * (a - mean_a) for p, a in zip(proj_vals, act_vals))
        var_p = sum((p - mean_p) ** 2 for p in proj_vals)
        var_a = sum((a - mean_a) ** 2 for a in act_vals)
        denom = math.sqrt(var_p * var_a)
        correlation = round(cov / denom, 3) if denom > 0 else 0.0

        # Worst misses (sorted by absolute error)
        details.sort(key=lambda d: abs(d["error"]), reverse=True)

        # Tier breakdown
        tiers = {"chalk": [], "mid": [], "low": []}
        for d in details:
            if d["actual"] >= 20:
                tiers["chalk"].append(d["error"])
            elif d["actual"] >= 8:
                tiers["mid"].append(d["error"])
            else:
                tiers["low"].append(d["error"])

        by_tier = {}
        for tier_name, tier_errors in tiers.items():
            if tier_errors:
                by_tier[tier_name] = {
                    "mae": round(sum(abs(e) for e in tier_errors) / len(tier_errors), 2),
                    "bias": round(sum(tier_errors) / len(tier_errors), 2),
                    "n": len(tier_errors),
                }

        return {
            "mae": round(mae, 2),
            "rmse": round(rmse, 2),
            "correlation": correlation,
            "n_matched": n,
            "worst_misses": details[:10],
            "by_tier": by_tier,
        }
