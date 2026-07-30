"""Parse V5 generation results."""
import json
import sys
import os
os.environ["PYTHONIOENCODING"] = "utf-8"
sys.stdout.reconfigure(encoding="utf-8")

fpath = sys.argv[1] if len(sys.argv) > 1 else r"C:\Users\CFlem\AppData\Local\Temp\v5_test_150.json"

with open(fpath, encoding="utf-8") as f:
    data = json.load(f)

print("=" * 60)
print("V5 GENERATION RESULTS")
print("=" * 60)
print(f"Lineups generated: {data.get('num_generated', 0)}")
print(f"Generation time:   {data.get('generation_time_ms', 0) / 1000:.1f}s")
print(f"Pool size:         {data.get('pool_size', '?')}")
print()

lineups = data.get("lineups", [])

# Summary stats
fps = []
sals = []
for lu in lineups:
    total_fp = sum(p.get("projected_fp", 0) for p in lu.get("players", []))
    total_sal = sum(p.get("salary", 0) for p in lu.get("players", []))
    fps.append(total_fp)
    sals.append(total_sal)

if fps:
    print(f"FP range:    {min(fps):.1f} - {max(fps):.1f} (avg {sum(fps)/len(fps):.1f})")
    print(f"Salary range: ${min(sals):,} - ${max(sals):,} (avg ${sum(sals)/len(sals):,.0f})")
    print()

# Show first 5 and last 5 lineups
for i in list(range(min(5, len(lineups)))) + (list(range(max(5, len(lineups)-5), len(lineups))) if len(lineups) > 10 else []):
    lu = lineups[i]
    total_fp = sum(p.get("projected_fp", 0) for p in lu.get("players", []))
    total_sal = sum(p.get("salary", 0) for p in lu.get("players", []))
    print(f"Lineup {i+1}: {total_fp:.1f} FP | ${total_sal:,} salary")
    for p in lu.get("players", []):
        pos = p.get("position", "?")
        name = p.get("player_name", p.get("display_name", "?"))
        team = p.get("team_abbreviation", "?")
        sal = p.get("salary", 0)
        fp = p.get("projected_fp", 0)
        print(f"  {pos:5s} {name:25s} {team:4s} ${sal:>6,} {fp:>6.1f}FP")
    print()
    if i == 4 and len(lineups) > 10:
        print(f"  ... ({len(lineups) - 10} lineups omitted) ...\n")

# Unique players
from collections import Counter

all_players = set()
all_teams = set()
for lu in lineups:
    for p in lu.get("players", []):
        all_players.add(p.get("player_name", p.get("display_name", "?")))
        all_teams.add(p.get("team_abbreviation", "?"))

print(f"Unique players across {len(lineups)} lineups: {len(all_players)}")
print(f"Teams represented: {sorted(all_teams)} ({len(all_teams)} teams)")
print()

# Player frequency - top 20 and bottom 20
freq = Counter()
for lu in lineups:
    for p in lu.get("players", []):
        freq[p.get("player_name", "?")] += 1

print(f"Top 20 most frequent players:")
for name, count in freq.most_common(20):
    pct = count / len(lineups) * 100
    print(f"  {count:>3}/{len(lineups)} ({pct:>5.1f}%) {name}")

print(f"\nBottom 20 least frequent players:")
for name, count in freq.most_common()[:-21:-1]:
    pct = count / len(lineups) * 100
    print(f"  {count:>3}/{len(lineups)} ({pct:>5.1f}%) {name}")

# Exposure distribution
exposure_buckets = {
    "1 lineup (unique)": 0,
    "2-5 lineups": 0,
    "6-15 lineups": 0,
    "16-30 lineups": 0,
    "31-75 lineups": 0,
    "76+ lineups": 0,
}
for name, count in freq.items():
    if count == 1:
        exposure_buckets["1 lineup (unique)"] += 1
    elif count <= 5:
        exposure_buckets["2-5 lineups"] += 1
    elif count <= 15:
        exposure_buckets["6-15 lineups"] += 1
    elif count <= 30:
        exposure_buckets["16-30 lineups"] += 1
    elif count <= 75:
        exposure_buckets["31-75 lineups"] += 1
    else:
        exposure_buckets["76+ lineups"] += 1

print(f"\nExposure distribution ({len(freq)} total players):")
for bucket, count in exposure_buckets.items():
    print(f"  {bucket:20s}: {count:>3} players")
