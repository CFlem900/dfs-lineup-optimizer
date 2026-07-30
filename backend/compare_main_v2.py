"""Compare main v1 lineups against DK simulator lineups for 03/05/2026 main slate."""
import csv
import json
import re
from collections import Counter

# Load our lineups
with open("main_v2_lineups.json", encoding="utf-8") as f:
    ours = json.load(f)

# Load DK sim lineups — columns are PG,SG,SF,PF,C,G,F,UTIL
# Values are "Player Name (ID)"
dk_lineups = []
with open(r"C:\Users\CFlem\Downloads\DK_NBA_Main_Pre_Contest_Sims_Lineups (13).csv",
          encoding="utf-8-sig") as f:
    reader = csv.DictReader(f)
    for row in reader:
        players = []
        for slot in ["PG", "SG", "SF", "PF", "C", "G", "F", "UTIL"]:
            val = row.get(slot, "").strip()
            if val:
                # Extract name from "Player Name (12345)"
                name = re.sub(r'\s*\(\d+\)$', '', val).strip()
                players.append(name)
        if players:
            dk_lineups.append(players)

# Load DK projections
dk_proj = {}
with open(r"C:\Users\CFlem\Downloads\DK_NBA_Main_Data_Hub_Projections (1).csv",
          encoding="utf-8-sig") as f:
    reader = csv.DictReader(f)
    for row in reader:
        name = row.get("Player", "").strip()
        if name:
            dk_proj[name] = {
                "fp": float(row.get("Projection", 0) or 0),
                "salary": int(row.get("Salary", 0) or 0),
                "ownership": float(row.get("Ownership %", "0").replace("%", "") or 0),
                "minutes": float(row.get("Minutes", 0) or 0),
                "team": row.get("Team", "?").strip(),
            }

print(f"Our lineups: {ours['num_generated']}/{ours['num_requested']}")
print(f"DK sim lineups: {len(dk_lineups)}")
print(f"Our pool size: {ours['pool_size']}")
print()

# Our exposure
our_exposure = Counter()
our_players = {}
for lu in ours.get("lineups", []):
    for p in lu.get("players", []):
        name = p["player_name"]
        our_exposure[name] += 1
        if name not in our_players:
            our_players[name] = p

# DK exposure
dk_exposure = Counter()
for lu in dk_lineups:
    for name in lu:
        dk_exposure[name] += 1

# All players
all_players = set(our_exposure.keys()) | set(dk_exposure.keys())

n_ours = ours["num_generated"]
n_dk = len(dk_lineups)

print("=" * 115)
print(f"{'Player':<30} {'Team':>5} {'Sal':>6} {'Our%':>6} {'DK%':>6} {'Gap':>7} {'OurFP':>6} {'DKFP':>6} {'FP Err':>7} {'DKOwn':>6}")
print("=" * 115)

rows = []
for name in all_players:
    our_pct = (our_exposure[name] / n_ours * 100) if name in our_exposure else 0
    dk_pct = (dk_exposure[name] / n_dk * 100) if name in dk_exposure else 0
    gap = our_pct - dk_pct

    our_fp = our_players[name]["projected_fp"] if name in our_players else 0
    dk_fp = dk_proj.get(name, {}).get("fp", 0)
    fp_err = ((our_fp - dk_fp) / dk_fp * 100) if dk_fp > 0 else 0

    team = our_players[name]["team_abbreviation"] if name in our_players else dk_proj.get(name, {}).get("team", "?")
    salary = our_players[name]["salary"] if name in our_players else dk_proj.get(name, {}).get("salary", 0)
    dk_own = dk_proj.get(name, {}).get("ownership", 0)
    rows.append((name, team, salary, our_pct, dk_pct, gap, our_fp, dk_fp, fp_err, dk_own))

# Sort by DK exposure (descending)
rows.sort(key=lambda x: -x[4])

for name, team, salary, our_pct, dk_pct, gap, our_fp, dk_fp, fp_err, dk_own in rows:
    marker = ""
    if abs(gap) > 20:
        marker = " <<<" if gap < -20 else " >>>"
    print(f"{name:<30} {team:>5} {salary:>6} {our_pct:5.1f}% {dk_pct:5.1f}% {gap:+6.1f}% {our_fp:5.1f}  {dk_fp:5.1f}  {fp_err:+6.1f}% {dk_own:5.1f}%{marker}")

# Summary stats
print()
print("=" * 115)
print("SUMMARY")
print("=" * 115)

# Top-N overlap
dk_top10 = [r[0] for r in sorted(rows, key=lambda x: -x[4])[:10]]
our_top10 = [r[0] for r in sorted(rows, key=lambda x: -x[3])[:10]]
dk_top20 = [r[0] for r in sorted(rows, key=lambda x: -x[4])[:20]]
our_top20 = [r[0] for r in sorted(rows, key=lambda x: -x[3])[:20]]

overlap10 = len(set(dk_top10) & set(our_top10))
overlap20 = len(set(dk_top20) & set(our_top20))

print(f"Top-10 exposure overlap: {overlap10}/10")
print(f"Top-20 exposure overlap: {overlap20}/20")

# Mean absolute exposure gap
gaps = [abs(r[5]) for r in rows if r[4] > 0 or r[3] > 0]
print(f"Mean absolute exposure gap: {sum(gaps)/len(gaps):.1f}%")

# Mean FP error (for players with DK FP > 0)
fp_errors = [abs(r[8]) for r in rows if r[7] > 0]
if fp_errors:
    print(f"Mean projection error: {sum(fp_errors)/len(fp_errors):.1f}%")

# Correlation of exposure
import numpy as np
both = [(r[3], r[4]) for r in rows if r[3] > 0 or r[4] > 0]
if len(both) > 5:
    ours_arr = np.array([b[0] for b in both])
    dk_arr = np.array([b[1] for b in both])
    corr = np.corrcoef(ours_arr, dk_arr)[0, 1]
    print(f"Exposure correlation: {corr:.3f}")

# FP correlation for players in both
both_fp = [(r[6], r[7]) for r in rows if r[6] > 0 and r[7] > 0]
if len(both_fp) > 5:
    ours_fp = np.array([b[0] for b in both_fp])
    dk_fp_arr = np.array([b[1] for b in both_fp])
    fp_corr = np.corrcoef(ours_fp, dk_fp_arr)[0, 1]
    print(f"Projection correlation (players in both): {fp_corr:.3f}")

# Players in DK top-20 missing from ours
dk_top20_missing = [n for n in dk_top20 if our_exposure.get(n, 0) == 0]
if dk_top20_missing:
    print(f"\nDK top-20 players MISSING from our lineups:")
    for n in dk_top20_missing:
        dk_fp = dk_proj.get(n, {}).get("fp", 0)
        dk_sal = dk_proj.get(n, {}).get("salary", 0)
        dk_team = dk_proj.get(n, {}).get("team", "?")
        print(f"  {n} ({dk_team}) ${dk_sal:,} DK: {dk_exposure[n]/n_dk*100:.1f}%, FP={dk_fp:.1f}")

# Biggest over/under exposures
print(f"\nBiggest OVER-exposures (ours >> DK):")
over = sorted(rows, key=lambda x: -x[5])[:7]
for name, team, salary, our_pct, dk_pct, gap, our_fp, dk_fp, fp_err, dk_own in over:
    print(f"  {name:<30} {team} ${salary:,} ours={our_pct:.1f}% dk={dk_pct:.1f}% gap={gap:+.1f}% ourFP={our_fp:.1f} dkFP={dk_fp:.1f}")

print(f"\nBiggest UNDER-exposures (ours << DK):")
under = sorted(rows, key=lambda x: x[5])[:7]
for name, team, salary, our_pct, dk_pct, gap, our_fp, dk_fp, fp_err, dk_own in under:
    print(f"  {name:<30} {team} ${salary:,} ours={our_pct:.1f}% dk={dk_pct:.1f}% gap={gap:+.1f}% ourFP={our_fp:.1f} dkFP={dk_fp:.1f}")

# Biggest FP errors (absolute)
print(f"\nBiggest projection errors (|our - DK| / DK):")
fp_err_sorted = sorted(rows, key=lambda x: -abs(x[8]) if x[7] > 5 else 0)[:10]
for name, team, salary, our_pct, dk_pct, gap, our_fp, dk_fp, fp_err, dk_own in fp_err_sorted:
    if dk_fp > 5:
        print(f"  {name:<30} {team} ourFP={our_fp:.1f} dkFP={dk_fp:.1f} err={fp_err:+.1f}% our%={our_pct:.1f}% dk%={dk_pct:.1f}%")

# Ghost player check: players with 0 DK FP but >5% our exposure
print(f"\nGhost players (DK FP=0 but in our lineups):")
ghosts = [(r[0], r[1], r[2], r[3], r[6]) for r in rows if r[7] == 0 and r[3] > 1]
ghosts.sort(key=lambda x: -x[3])
for name, team, salary, our_pct, our_fp in ghosts[:10]:
    print(f"  {name:<30} {team} ${salary:,} our%={our_pct:.1f}% ourFP={our_fp:.1f}")

# Salary distribution comparison
print(f"\nSalary tier exposure comparison:")
tiers = [(3000, 4000, "$3K-$4K"), (4000, 5500, "$4K-$5.5K"), (5500, 7000, "$5.5K-$7K"),
         (7000, 9000, "$7K-$9K"), (9000, 12000, "$9K+")]
for lo, hi, label in tiers:
    our_tier = sum(r[3] for r in rows if lo <= r[2] < hi)
    dk_tier = sum(r[4] for r in rows if lo <= r[2] < hi)
    print(f"  {label:>12}: ours={our_tier:.0f}% dk={dk_tier:.0f}%")
