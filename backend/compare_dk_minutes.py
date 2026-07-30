"""
Compare our rotation engine projected minutes vs DraftKings minutes projections.

Usage:
    ./venv/Scripts/python compare_dk_minutes.py [--dg DG_ID]

Reads the DK CSV and calls the local rotation API for each team on the
2026-03-20 main slate, then prints per-player diffs and summary stats.
"""

import argparse
import csv
import statistics
import sys
import unicodedata
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DK_CSV = Path(r"C:\Users\CFlem\Downloads\DK_NBA_Main_Data_Hub_Projections (22).csv")
API_BASE = "http://localhost:8000/api"
GAME_DATE = "2026-03-20"

# NBA official team IDs for tonight's 10-team slate
TEAM_IDS = {
    "ATL": 1610612737,
    "HOU": 1610612745,
    "DET": 1610612765,
    "GSW": 1610612744,
    "BOS": 1610612738,
    "MEM": 1610612763,
    "NYK": 1610612752,
    "BKN": 1610612751,
    "MIN": 1610612750,
    "POR": 1610612757,
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# Name aliases for matching DK names ↔ engine names
NAME_ALIASES = {
    "robert williams": "robert williams iii",
    "jimmy butler": "jimmy butler iii",
    "gg jackson": "gregory jackson",
    "gregory jackson": "gg jackson",
    "bones hyland": "nah'shon hyland",
    "nah'shon hyland": "bones hyland",
    "nicolas claxton": "nic claxton",
    "nic claxton": "nicolas claxton",
    "lj cryer": "l.j. cryer",
    "l.j. cryer": "lj cryer",
}


def normalize_name(name: str) -> str:
    """NFKD normalize + lowercase + strip for fuzzy matching."""
    nfkd = unicodedata.normalize("NFKD", name)
    ascii_name = nfkd.encode("ascii", "ignore").decode("ascii")
    return ascii_name.lower().strip()


def match_name(name_key: str, lookup: dict) -> str | None:
    """Try exact match first, then aliases, then suffix-stripped match."""
    if name_key in lookup:
        return name_key
    # Try alias
    alias = NAME_ALIASES.get(name_key)
    if alias and alias in lookup:
        return alias
    # Try stripping common suffixes (III, Jr., II, IV)
    for suffix in (" iii", " jr.", " jr", " ii", " iv"):
        stripped = name_key.removesuffix(suffix)
        if stripped != name_key and stripped in lookup:
            return stripped
    # Try adding common suffixes
    for suffix in (" iii", " jr."):
        augmented = name_key + suffix
        if augmented in lookup:
            return augmented
    return None


def load_dk_csv() -> dict[str, dict]:
    """Load DK CSV, return dict keyed by normalized player name."""
    players = {}
    with open(DK_CSV, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            team = row["Team"].strip()
            if team not in TEAM_IDS:
                continue
            name_key = normalize_name(row["Player"])
            try:
                dk_mins = float(row["Minutes"])
            except (ValueError, TypeError):
                dk_mins = 0.0
            players[name_key] = {
                "name": row["Player"].strip(),
                "team": team,
                "dk_minutes": dk_mins,
                "salary": row.get("Salary", ""),
                "position": row.get("Position", ""),
            }
    return players


def fetch_rotation(team_abbr: str, team_id: int, dg_id: str | None) -> list[dict]:
    """Call the rotation endpoint and return the projections list."""
    url = f"{API_BASE}/teams/{team_id}/rotation"
    params = {"game_date": GAME_DATE}
    if dg_id:
        params["draft_group_id"] = dg_id

    try:
        resp = requests.get(url, params=params, timeout=90)
        resp.raise_for_status()
        data = resp.json()
        return data.get("projections", [])
    except requests.RequestException as e:
        print(f"  [ERROR] {team_abbr} ({team_id}): {e}", file=sys.stderr)
        return []


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="Compare engine vs DK minutes")
    parser.add_argument("--dg", default=None, help="DraftKings draft_group_id")
    args = parser.parse_args()

    # 1. Load DK data
    if not DK_CSV.exists():
        print(f"DK CSV not found: {DK_CSV}", file=sys.stderr)
        sys.exit(1)
    dk_players = load_dk_csv()
    print(f"Loaded {len(dk_players)} DK players across {len(TEAM_IDS)} teams\n")

    # 2. Fetch rotation for each team
    engine_by_name: dict[str, dict] = {}
    for abbr, tid in sorted(TEAM_IDS.items()):
        print(f"Fetching {abbr} ({tid})...")
        projections = fetch_rotation(abbr, tid, args.dg)
        for p in projections:
            name_key = normalize_name(p["player_name"])
            engine_by_name[name_key] = {
                "name": p["player_name"],
                "adjusted_minutes": p.get("adjusted_minutes", 0.0),
                "baseline_minutes": p.get("baseline_minutes", 0.0),
                "confidence": p.get("confidence", 0.0),
                "team": abbr,
            }
        print(f"  -> {len(projections)} players")

    # 3. Compare
    comparisons = []
    dk_only = []

    matched_engine_keys = set()
    for name_key, dk_info in dk_players.items():
        eng_key = match_name(name_key, engine_by_name)
        if eng_key:
            eng = engine_by_name[eng_key]
            matched_engine_keys.add(eng_key)
            diff = eng["adjusted_minutes"] - dk_info["dk_minutes"]
            comparisons.append({
                "name": dk_info["name"],
                "team": dk_info["team"],
                "dk_mins": dk_info["dk_minutes"],
                "engine_mins": eng["adjusted_minutes"],
                "baseline_mins": eng["baseline_minutes"],
                "diff": diff,
                "abs_diff": abs(diff),
                "confidence": eng["confidence"],
                "salary": dk_info["salary"],
                "position": dk_info["position"],
            })
        else:
            dk_only.append(dk_info)

    engine_only = set(engine_by_name.keys()) - matched_engine_keys

    # Sort by absolute error descending
    comparisons.sort(key=lambda x: x["abs_diff"], reverse=True)

    # 4. Print per-player diffs
    print("\n" + "=" * 100)
    print(f"{'Player':<25} {'Team':<5} {'Pos':<5} {'Salary':<7} "
          f"{'DK Min':>7} {'Eng Min':>8} {'Base':>7} {'Diff':>7} {'Conf':>6}")
    print("-" * 100)
    for c in comparisons:
        sign = "+" if c["diff"] >= 0 else ""
        print(f"{c['name']:<25} {c['team']:<5} {c['position']:<5} {c['salary']:<7} "
              f"{c['dk_mins']:>7.1f} {c['engine_mins']:>8.1f} {c['baseline_mins']:>7.1f} "
              f"{sign}{c['diff']:>6.1f} {c['confidence']:>6.2f}")

    # 5. Summary stats (players with >= 10 DK minutes)
    significant = [c for c in comparisons if c["dk_mins"] >= 10.0]
    if significant:
        abs_diffs = [c["abs_diff"] for c in significant]
        diffs = [c["diff"] for c in significant]
        mean_abs = statistics.mean(abs_diffs)
        median_abs = statistics.median(abs_diffs)
        stdev_diff = statistics.stdev(diffs) if len(diffs) > 1 else 0.0
        mean_diff = statistics.mean(diffs)

        print("\n" + "=" * 100)
        print(f"SUMMARY (players with >= 10 DK minutes: {len(significant)} players)")
        print(f"  Mean absolute diff : {mean_abs:.2f} min")
        print(f"  Median absolute diff: {median_abs:.2f} min")
        print(f"  Std dev of diff    : {stdev_diff:.2f} min")
        print(f"  Mean signed diff   : {mean_diff:+.2f} min (+ = engine higher)")
        print(f"  Max over-project   : {max(diffs):+.1f} min")
        print(f"  Max under-project  : {min(diffs):+.1f} min")

        # Buckets
        under_2 = sum(1 for d in abs_diffs if d < 2)
        under_5 = sum(1 for d in abs_diffs if 2 <= d < 5)
        over_5 = sum(1 for d in abs_diffs if d >= 5)
        print(f"\n  Within 2 min: {under_2} ({100*under_2/len(abs_diffs):.0f}%)")
        print(f"  2-5 min off : {under_5} ({100*under_5/len(abs_diffs):.0f}%)")
        print(f"  5+ min off  : {over_5} ({100*over_5/len(abs_diffs):.0f}%)")
    else:
        print("\nNo players with >= 10 DK minutes found for summary.")

    # 6. Unmatched players
    if dk_only:
        print(f"\n--- DK players NOT in engine ({len(dk_only)}) ---")
        for p in dk_only:
            print(f"  {p['name']:<25} {p['team']:<5} DK={p['dk_minutes']:.1f} min")

    if engine_only:
        print(f"\n--- Engine players NOT in DK ({len(engine_only)}) ---")
        for name_key in sorted(engine_only):
            eng = engine_by_name[name_key]
            print(f"  {eng['name']:<25} {eng.get('team','?'):<5} "
                  f"Eng={eng['adjusted_minutes']:.1f} min")


if __name__ == "__main__":
    main()
