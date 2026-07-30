"""Compare night v4 lineups against DK simulator lineups."""
import csv
import json
import re
from collections import Counter

# Load our lineups
with open("night_v4_lineups.json", encoding="utf-8") as f:
    ours = json.load(f)

# Load DK sim lineups — columns are PG,SG,SF,PF,C,G,F,UTIL
# Values are "Player Name (ID)"
dk_lineups = []
with open(r"C:\Users\CFlem\Downloads\DK_NBA_Night_Pre_Contest_Sims_Lineups (6).csv",
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

# Load DK projections — columns: Player,Salary,Position,Team,...,Projection,...,Ownership %,...
dk_proj = {}
with open(r"C:\Users\CFlem\Downloads\DK_NBA_Night_Data_Hub_Projections.csv",
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

print("=" * 105)
print(f"{'Player':<30} {'Team':>5} {'Our%':>6} {'DK%':>6} {'Gap':>7} {'OurFP':>6} {'DKFP':>6} {'FP Err':>7}")
print("=" * 105)

rows = []
for name in all_players:
    our_pct = (our_exposure[name] / n_ours * 100) if name in our_exposure else 0
    dk_pct = (dk_exposure[name] / n_dk * 100) if name in dk_exposure else 0
    gap = our_pct - dk_pct

    our_fp = our_players[name]["projected_fp"] if name in our_players else 0
    dk_fp = dk_proj.get(name, {}).get("fp", 0)
    fp_err = ((our_fp - dk_fp) / dk_fp * 100) if dk_fp > 0 else 0

    team = our_players[name]["team_abbreviation"] if name in our_players else "?"
    rows.append((name, team, our_pct, dk_pct, gap, our_fp, dk_fp, fp_err))

# Sort by DK exposure (descending)
rows.sort(key=lambda x: -x[3])

for name, team, our_pct, dk_pct, gap, our_fp, dk_fp, fp_err in rows:
    marker = ""
    if abs(gap) > 20:
        marker = " <<<" if gap < -20 else " >>>"
    print(f"{name:<30} {team:>5} {our_pct:5.1f}% {dk_pct:5.1f}% {gap:+6.1f}% {our_fp:5.1f}  {dk_fp:5.1f}  {fp_err:+6.1f}%{marker}")

# Summary stats
print()
print("=" * 105)
print("SUMMARY")
print("=" * 105)

# Top-N overlap
dk_top10 = [r[0] for r in sorted(rows, key=lambda x: -x[3])[:10]]
our_top10 = [r[0] for r in sorted(rows, key=lambda x: -x[2])[:10]]
dk_top20 = [r[0] for r in sorted(rows, key=lambda x: -x[3])[:20]]
our_top20 = [r[0] for r in sorted(rows, key=lambda x: -x[2])[:20]]

overlap10 = len(set(dk_top10) & set(our_top10))
overlap20 = len(set(dk_top20) & set(our_top20))

print(f"Top-10 overlap: {overlap10}/10")
print(f"Top-20 overlap: {overlap20}/20")

# Mean absolute exposure gap
gaps = [abs(r[4]) for r in rows if r[3] > 0 or r[2] > 0]
print(f"Mean absolute exposure gap: {sum(gaps)/len(gaps):.1f}%")

# Mean FP error (for players with DK FP > 0)
fp_errors = [abs(r[7]) for r in rows if r[6] > 0]
if fp_errors:
    print(f"Mean projection error: {sum(fp_errors)/len(fp_errors):.1f}%")

# Players in DK top-20 missing from ours
dk_top20_missing = [n for n in dk_top20 if our_exposure.get(n, 0) == 0]
if dk_top20_missing:
    print(f"\nDK top-20 players missing from our lineups:")
    for n in dk_top20_missing:
        dk_fp = dk_proj.get(n, {}).get("fp", 0)
        print(f"  {n} (DK: {dk_exposure[n]/n_dk*100:.1f}%, FP={dk_fp:.1f})")

# Biggest over/under exposures
print(f"\nBiggest OVER-exposures (ours >> DK):")
over = sorted(rows, key=lambda x: -x[4])[:5]
for name, team, our_pct, dk_pct, gap, our_fp, dk_fp, fp_err in over:
    print(f"  {name:<30} {team} ours={our_pct:.1f}% dk={dk_pct:.1f}% gap={gap:+.1f}%")

print(f"\nBiggest UNDER-exposures (ours << DK):")
under = sorted(rows, key=lambda x: x[4])[:5]
for name, team, our_pct, dk_pct, gap, our_fp, dk_fp, fp_err in under:
    print(f"  {name:<30} {team} ours={our_pct:.1f}% dk={dk_pct:.1f}% gap={gap:+.1f}%")

# IND bench comparison
print(f"\nIND Bench Players (key issue from v3):")
ind_bench = ["Taelon Peter", "Ben Sheppard", "Kobe Brown", "Kam Jones", "Obi Toppin"]
for name in ind_bench:
    our_pct = (our_exposure.get(name, 0) / n_ours * 100)
    dk_pct = (dk_exposure.get(name, 0) / n_dk * 100)
    our_fp = our_players.get(name, {}).get("projected_fp", 0) if name in our_players else 0
    dk_fp = dk_proj.get(name, {}).get("fp", 0)
    print(f"  {name:<25} ours={our_pct:5.1f}% dk={dk_pct:5.1f}%  ourFP={our_fp:5.1f} dkFP={dk_fp:5.1f}")

# Key targets
print(f"\nKey Target Players:")
targets = ["T.J. McConnell", "Jarace Walker", "Ousmane Dieng", "Andrew Nembhard",
           "Pascal Siakam", "Onyeka Okongwu", "Kawhi Leonard"]
for name in targets:
    our_pct = (our_exposure.get(name, 0) / n_ours * 100)
    dk_pct = (dk_exposure.get(name, 0) / n_dk * 100)
    our_fp = our_players.get(name, {}).get("projected_fp", 0) if name in our_players else 0
    dk_fp = dk_proj.get(name, {}).get("fp", 0)
    print(f"  {name:<25} ours={our_pct:5.1f}% dk={dk_pct:5.1f}%  ourFP={our_fp:5.1f} dkFP={dk_fp:5.1f}")

# Version comparison
print(f"\n{'='*105}")
print("VERSION COMPARISON (v3 → v4):")
print(f"  v3: 73/150 lineups, Top-10: 6/10, Top-20: 16/20, Mean error: 76.4%")
print(f"  v4: {ours['num_generated']}/150 lineups, Top-10: {overlap10}/10, Top-20: {overlap20}/20", end="")
if fp_errors:
    print(f", Mean error: {sum(fp_errors)/len(fp_errors):.1f}%")
else:
    print()
