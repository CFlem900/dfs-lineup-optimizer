"""
Compare App-Generated Lineups (v3 — after 5 fixes) vs DK Pre-Contest Sims.
Clears pool cache, generates 150 GPP lineups for DG=143272, and compares
exposure rates against the DK sims CSV.
"""
import sys
import io
import csv
import re
import requests
import time
from collections import Counter, defaultdict

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

BASE = "http://localhost:8000/api"
DG = 143272
NUM_LINEUPS = 150
CSV_PATH = r"C:\Users\CFlem\Downloads\DK_NBA_Main_Pre_Contest_Sims_Lineups (9).csv"

POSITIONS = ["PG", "SG", "SF", "PF", "C", "G", "F", "UTIL"]


def strip_dk_id(raw: str) -> str:
    return re.sub(r"\s*\(\d+\)", "", raw).strip()


def load_dk_sims(path: str):
    lineups = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            lineup = []
            for pos in POSITIONS:
                name = strip_dk_id(row[pos].strip())
                lineup.append(name)
            lineups.append(lineup)
    return lineups


def get_exposure(lineups, total):
    ctr = Counter()
    for lu in lineups:
        for p in lu:
            ctr[p] += 1
    return {p: (cnt, cnt / total * 100) for p, cnt in ctr.most_common()}


def main():
    # Step 1: Clear pool cache
    print("Clearing pool cache...")
    r = requests.post(f"{BASE}/player-pool/clear-cache")
    print(f"  Cache clear: {r.status_code}")

    # Step 2: Build fresh pool
    print(f"\nBuilding fresh pool for DG={DG}...")
    r = requests.get(f"{BASE}/player-pool", params={
        "draft_group_id": DG,
        "platform": "dk",
        "sport": "nba",
    })
    if r.status_code != 200:
        print(f"  ERROR building pool: {r.status_code} {r.text[:500]}")
        return
    pool_resp = r.json()
    pool = pool_resp.get("players", pool_resp) if isinstance(pool_resp, dict) else pool_resp
    if isinstance(pool, list):
        print(f"  Pool size: {len(pool)} players")
        sources = Counter()
        for p in pool:
            sources[p.get("projection_source", "unknown")] += 1
        print(f"  Projection sources: {dict(sources)}")
    else:
        print(f"  Pool response keys: {list(pool_resp.keys()) if isinstance(pool_resp, dict) else 'N/A'}")

    # Step 3: Generate 150 lineups
    print(f"\nGenerating {NUM_LINEUPS} GPP lineups...")
    t0 = time.time()
    r = requests.post(f"{BASE}/generate-lineups", json={
        "platform": "dk",
        "draft_group_id": DG,
        "num_lineups": NUM_LINEUPS,
        "contest_type": "gpp",
        "sport": "nba",
        "strategy": "balanced",
    })
    elapsed = time.time() - t0
    if r.status_code != 200:
        print(f"  ERROR generating lineups: {r.status_code} {r.text[:500]}")
        return

    result = r.json()
    app_lineups_raw = result.get("lineups", [])
    print(f"  Generated: {len(app_lineups_raw)} lineups in {elapsed:.1f}s")

    # Extract player names from app lineups
    app_lineups = []
    for lu in app_lineups_raw:
        players = lu.get("players", [])
        names = [p.get("player_name", p.get("name", "???")) for p in players]
        app_lineups.append(names)

    # Step 4: Load DK sims
    dk_lineups = load_dk_sims(CSV_PATH)
    print(f"  DK sims: {len(dk_lineups)} lineups")

    # Step 5: Compare
    app_exp = get_exposure(app_lineups, len(app_lineups))
    dk_exp = get_exposure(dk_lineups, len(dk_lineups))

    # All unique players across both
    all_players = sorted(set(list(app_exp.keys()) + list(dk_exp.keys())),
                         key=lambda p: max(app_exp.get(p, (0, 0))[1], dk_exp.get(p, (0, 0))[1]),
                         reverse=True)

    print(f"\n{'=' * 90}")
    print(f"  EXPOSURE COMPARISON: App (v3) vs DK Pre-Contest Sims")
    print(f"  App Lineups: {len(app_lineups)}  |  DK Sims: {len(dk_lineups)}")
    print(f"  App Unique Players: {len(app_exp)}  |  DK Unique Players: {len(dk_exp)}")
    print(f"{'=' * 90}")

    print(f"\n  {'Player':<30} {'App%':>7} {'DK%':>7} {'Delta':>7}  {'Match':<10}")
    print(f"  {'-' * 65}")

    close_matches = 0
    total_delta = 0
    for player in all_players:
        app_pct = app_exp.get(player, (0, 0))[1]
        dk_pct = dk_exp.get(player, (0, 0))[1]
        delta = app_pct - dk_pct

        if abs(delta) < 10:
            match = "CLOSE"
            close_matches += 1
        elif delta > 0:
            match = "OVER"
        else:
            match = "UNDER"

        total_delta += abs(delta)
        print(f"  {player:<30} {app_pct:>6.1f}% {dk_pct:>6.1f}% {delta:>+6.1f}%  {match:<10}")

    # Summary metrics
    avg_delta = total_delta / len(all_players) if all_players else 0
    print(f"\n{'-' * 90}")
    print(f"  SUMMARY METRICS")
    print(f"{'-' * 90}")
    print(f"  Close matches (within 10%): {close_matches}/{len(all_players)} ({close_matches/len(all_players)*100:.0f}%)")
    print(f"  Average absolute delta: {avg_delta:.1f}%")
    print(f"  App unique players: {len(app_exp)}")
    print(f"  DK unique players: {len(dk_exp)}")

    # Top-10 comparison
    app_top10 = sorted(app_exp.items(), key=lambda x: -x[1][1])[:10]
    dk_top10 = sorted(dk_exp.items(), key=lambda x: -x[1][1])[:10]

    print(f"\n  TOP-10 MOST EXPOSED:")
    print(f"  {'Rank':<6} {'App Player':<30} {'App%':>6}  {'DK Player':<30} {'DK%':>6}")
    print(f"  {'-' * 82}")
    for i in range(10):
        ap, (_, apct) = app_top10[i] if i < len(app_top10) else ("", (0, 0))
        dp, (_, dpct) = dk_top10[i] if i < len(dk_top10) else ("", (0, 0))
        print(f"  {i+1:<6} {ap:<30} {apct:>5.1f}%  {dp:<30} {dpct:>5.1f}%")

    # Value play comparison (players with salary <= $4500)
    KNOWN_VALUE = {
        "Cam Spencer", "Javon Small", "Walter Clayton Jr.", "Jahmai Mashack",
        "Trendon Watford", "Taylor Hendricks", "Olivier-Maxence Prosper",
        "Dominick Barlow", "John Konchar", "Ace Bailey", "Brice Sensabaugh",
        "Toumani Camara", "Kyle Filipowski", "Neemias Queta",
        "Scoot Henderson", "Donovan Clingan", "Quentin Grimes",
    }
    print(f"\n  VALUE PLAYS (salary <= $4,500):")
    print(f"  {'Player':<30} {'App%':>7} {'DK%':>7} {'Delta':>7}")
    print(f"  {'-' * 55}")
    for player in sorted(KNOWN_VALUE):
        app_pct = app_exp.get(player, (0, 0))[1]
        dk_pct = dk_exp.get(player, (0, 0))[1]
        if app_pct > 0 or dk_pct > 0:
            delta = app_pct - dk_pct
            print(f"  {player:<30} {app_pct:>6.1f}% {dk_pct:>6.1f}% {delta:>+6.1f}%")

    # Star players comparison
    STARS = {
        "Shai Gilgeous-Alexander", "Karl-Anthony Towns", "Tyrese Maxey",
        "Jaylen Brown", "Jalen Brunson", "LaMelo Ball", "Chet Holmgren",
    }
    print(f"\n  STAR PLAYERS:")
    print(f"  {'Player':<30} {'App%':>7} {'DK%':>7} {'Delta':>7}")
    print(f"  {'-' * 55}")
    for player in sorted(STARS):
        app_pct = app_exp.get(player, (0, 0))[1]
        dk_pct = dk_exp.get(player, (0, 0))[1]
        delta = app_pct - dk_pct
        print(f"  {player:<30} {app_pct:>6.1f}% {dk_pct:>6.1f}% {delta:>+6.1f}%")

    print(f"\n{'=' * 90}")
    print(f"  END OF COMPARISON")
    print(f"{'=' * 90}")


if __name__ == "__main__":
    main()
