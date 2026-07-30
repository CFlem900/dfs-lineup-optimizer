"""
Investigate why 4 players from DK sims are missing from our pool.
Also check projection sources and scores for key value plays.
"""
import sys, io, json, requests
from collections import Counter

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

BASE = "http://localhost:8000/api"
DG = 143272

MISSING_PLAYERS = ["Javon Small", "Jahmai Mashack", "Kyle Filipowski", "John Konchar"]
VALUE_PLAYS = ["Walter Clayton Jr.", "Cam Spencer", "Quentin Grimes", "Trendon Watford",
               "Donovan Clingan", "Olivier-Maxence Prosper", "Taylor Hendricks"]
OVER_EXPOSED = ["Jalen Brunson", "VJ Edgecombe", "Jaren Jackson Jr.", "Jaylin Williams",
                "Walker Kessler", "Damian Lillard"]

def main():
    # Get pool
    print("Fetching pool...")
    r = requests.get(f"{BASE}/player-pool", params={
        "draft_group_id": DG, "platform": "dk", "sport": "nba"
    }, timeout=300)
    if r.status_code != 200:
        print(f"ERROR: {r.status_code}")
        return
    data = r.json()
    pool = data.get("players", [])
    excluded = data.get("excluded_players", [])

    print(f"Pool: {len(pool)} players")
    print(f"Excluded: {len(excluded)} players")

    # Build lookup by name (case-insensitive)
    pool_by_name = {}
    for p in pool:
        name = p.get("player_name", "").strip()
        pool_by_name[name.lower()] = p

    excluded_by_name = {}
    for p in excluded:
        name = p.get("player_name", p.get("name", "")).strip()
        excluded_by_name[name.lower()] = p

    # 1. Check missing players
    print(f"\n{'='*80}")
    print(f"  MISSING PLAYERS INVESTIGATION")
    print(f"{'='*80}")
    for name in MISSING_PLAYERS:
        nl = name.lower()
        if nl in pool_by_name:
            p = pool_by_name[nl]
            print(f"\n  {name}: FOUND in pool")
            print(f"    Salary: ${p.get('salary', 0):,}")
            print(f"    Projected FP: {p.get('projected_fp', 0)}")
            print(f"    Source: {p.get('projection_source', 'unknown')}")
        elif nl in excluded_by_name:
            p = excluded_by_name[nl]
            print(f"\n  {name}: FOUND in EXCLUDED list")
            print(f"    Reason: {p.get('exclusion_reason', p.get('reason', 'unknown'))}")
            print(f"    Data: {json.dumps(p, default=str)[:300]}")
        else:
            print(f"\n  {name}: NOT FOUND in pool or excluded list")
            # Try fuzzy match
            matches = [n for n in list(pool_by_name.keys()) + list(excluded_by_name.keys())
                      if name.split()[-1].lower() in n]
            if matches:
                print(f"    Possible matches: {matches}")

    # 2. Check value plays detail
    print(f"\n{'='*80}")
    print(f"  VALUE PLAYS DETAIL (projection sources, scores)")
    print(f"{'='*80}")
    print(f"  {'Player':<28} {'Salary':>7} {'Proj':>6} {'Floor':>6} {'Ceil':>6} {'FPPG':>6} {'Own%':>6} {'Source':<20}")
    print(f"  {'-'*100}")
    for name in VALUE_PLAYS:
        nl = name.lower()
        if nl in pool_by_name:
            p = pool_by_name[nl]
            sal = p.get("salary", 0)
            proj = p.get("projected_fp", 0)
            floor = p.get("floor_fp", 0)
            ceil = p.get("ceiling_fp", 0)
            fppg = p.get("dk_fppg") or p.get("fppg", 0)
            own = p.get("estimated_ownership", 0)
            src = p.get("projection_source") or "unknown"
            print(f"  {name:<28} ${sal:>6,} {proj:>6.1f} {floor:>6.1f} {ceil:>6.1f} {fppg:>6.1f} {own:>5.1f}% {src:<20}")
        else:
            print(f"  {name:<28} NOT IN POOL")

    # 3. Over-exposed players detail
    print(f"\n{'='*80}")
    print(f"  OVER-EXPOSED PLAYERS DETAIL (not in DK sims)")
    print(f"{'='*80}")
    print(f"  {'Player':<28} {'Salary':>7} {'Proj':>6} {'Floor':>6} {'Ceil':>6} {'FPPG':>6} {'Own%':>6} {'Source':<20}")
    print(f"  {'-'*100}")
    for name in OVER_EXPOSED:
        nl = name.lower()
        if nl in pool_by_name:
            p = pool_by_name[nl]
            sal = p.get("salary", 0)
            proj = p.get("projected_fp", 0) or 0
            floor = p.get("floor_fp", 0) or 0
            ceil = p.get("ceiling_fp", 0) or 0
            fppg = p.get("dk_fppg") or p.get("fppg") or 0
            own = p.get("estimated_ownership", 0) or 0
            src = p.get("projection_source") or "unknown"
            print(f"  {name:<28} ${sal:>6,} {proj:>6.1f} {floor:>6.1f} {ceil:>6.1f} {fppg:>6.1f} {own:>5.1f}% {src:<20}")

    # 4. Projection source breakdown
    print(f"\n{'='*80}")
    print(f"  PROJECTION SOURCE BREAKDOWN")
    print(f"{'='*80}")
    sources = Counter()
    for p in pool:
        sources[p.get("projection_source", "unknown")] += 1
    for src, count in sources.most_common():
        print(f"  {src:<30} {count:>4} players")

    # 5. Show all excluded players
    print(f"\n{'='*80}")
    print(f"  ALL EXCLUDED PLAYERS ({len(excluded)})")
    print(f"{'='*80}")
    for p in excluded:
        name = p.get("player_name", p.get("name", "???"))
        reason = p.get("exclusion_reason", p.get("reason", "unknown"))
        print(f"  {name:<30} Reason: {reason}")

    # 6. Value ratio analysis for all pool players
    print(f"\n{'='*80}")
    print(f"  TOP-20 VALUE RATIOS (FP per $1K salary)")
    print(f"{'='*80}")
    ratios = []
    for p in pool:
        sal = p.get("salary", 0) or 0
        proj = p.get("projected_fp", 0) or 0
        if sal > 0 and proj > 0:
            ratio = proj / (sal / 1000)
            ratios.append((p.get("player_name", "???"), sal, proj, ratio, p.get("projection_source") or "?"))
    ratios.sort(key=lambda x: -x[3])
    print(f"  {'Player':<28} {'Salary':>7} {'Proj':>6} {'Val Ratio':>10} {'Source':<15}")
    print(f"  {'-'*70}")
    for name, sal, proj, ratio, src in ratios[:20]:
        print(f"  {name:<28} ${sal:>6,} {proj:>6.1f} {ratio:>9.2f}x {src:<15}")

    print(f"\n{'='*80}")


if __name__ == "__main__":
    main()
