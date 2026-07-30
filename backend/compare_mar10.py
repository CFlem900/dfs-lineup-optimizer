"""Compare our 03/10/2026 main slate lineups against DK Data Hub projections."""
import csv
import json
import re
import sys
import io
import numpy as np
from collections import Counter

# Force UTF-8 output on Windows
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')


def normalize(name):
    """Normalize player name for matching: remove periods, Jr/Sr/II/III, lowercase."""
    n = name.strip().replace(".", "")
    return n.lower()


# Load our lineups
with open("lineups_mar10_v3.json", encoding="utf-8") as f:
    ours = json.load(f)

# Load DK projections
dk_proj = {}
dk_proj_norm = {}
with open(
    r"C:\Users\CFlem\Downloads\DK_NBA_Main_Data_Hub_Projections (8).csv",
    encoding="utf-8-sig",
) as f:
    reader = csv.DictReader(f)
    for row in reader:
        name = row.get("Player", "").strip()
        if name:
            dk_proj[name] = {
                "fp": float(row.get("Projection", 0) or 0),
                "salary": int(row.get("Salary", 0) or 0),
                "ownership": float(
                    row.get("Ownership %", "0").replace("%", "") or 0
                ),
                "minutes": float(row.get("Minutes", 0) or 0),
                "team": row.get("Team", "?").strip(),
                "std_dev": float(row.get("Std Dev", 0) or 0),
                "ceiling": float(row.get("Ceiling", 0) or 0),
                "floor": float(row.get("Floor", 0) or 0),
                "optimal": float(row.get("Optimal %", "0").replace("%", "") or 0),
                "starting": row.get("Starting", "").strip(),
            }
            dk_proj_norm[normalize(name)] = name

print(f"Our lineups: {ours['num_generated']}/{ours['num_requested']}")
print(f"Our pool size: {ours['pool_size']}")
print(f"DK projections: {len(dk_proj)} players")
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

n_ours = ours["num_generated"]


def get_dk_data(name):
    if name in dk_proj:
        return dk_proj[name]
    nn = normalize(name)
    if nn in dk_proj_norm:
        return dk_proj[dk_proj_norm[nn]]
    return {}


# Build all-player table
all_norm = {}
for name in our_exposure:
    all_norm[normalize(name)] = name
for name in dk_proj:
    nn = normalize(name)
    if nn not in all_norm:
        all_norm[nn] = name

all_players = list(all_norm.values())

rows = []
for name in all_players:
    our_pct = (our_exposure[name] / n_ours * 100) if name in our_exposure else 0
    our_fp = our_players[name]["projected_fp"] if name in our_players else 0
    dk_data = get_dk_data(name)
    dk_fp = dk_data.get("fp", 0)
    dk_own = dk_data.get("ownership", 0)
    dk_opt = dk_data.get("optimal", 0)
    fp_err = ((our_fp - dk_fp) / dk_fp * 100) if dk_fp > 0 else 0
    team = (
        our_players[name].get("team_abbreviation", "")
        if name in our_players
        else dk_data.get("team", "?")
    )
    salary = (
        our_players[name].get("salary", 0)
        if name in our_players
        else dk_data.get("salary", 0)
    )
    dk_std = dk_data.get("std_dev", 0)
    rows.append(
        (name, team, salary, our_pct, dk_own, dk_opt, our_fp, dk_fp, fp_err, dk_std)
    )

# Sort by DK ownership
rows.sort(key=lambda x: -x[4])

print("=" * 130)
print(
    f"{'Player':<30} {'Team':>5} {'Sal':>6} {'Our%':>6} {'DKOwn':>6} {'DKOpt':>6} {'Gap':>7} {'OurFP':>6} {'DKFP':>6} {'FP Err':>7} {'DKStd':>6}"
)
print("=" * 130)

for name, team, salary, our_pct, dk_own, dk_opt, our_fp, dk_fp, fp_err, dk_std in rows[
    :50
]:
    gap = our_pct - dk_own
    marker = ""
    if abs(gap) > 15:
        marker = " <<<" if gap < -15 else " >>>"
    print(
        f"{name:<30} {team:>5} {salary:>6} {our_pct:5.1f}% {dk_own:5.1f}% {dk_opt:5.1f}% {gap:+6.1f}% {our_fp:5.1f}  {dk_fp:5.1f}  {fp_err:+6.1f}% {dk_std:5.1f}{marker}"
    )

# Summary
print()
print("=" * 130)
print("SUMMARY")
print("=" * 130)

# Top-N overlap
dk_top10 = [r[0] for r in sorted(rows, key=lambda x: -x[4])[:10]]
our_top10 = [r[0] for r in sorted(rows, key=lambda x: -x[3])[:10]]
dk_top20 = [r[0] for r in sorted(rows, key=lambda x: -x[4])[:20]]
our_top20 = [r[0] for r in sorted(rows, key=lambda x: -x[3])[:20]]

overlap10 = len(set(dk_top10) & set(our_top10))
overlap20 = len(set(dk_top20) & set(our_top20))
print(f"Top-10 ownership overlap: {overlap10}/10")
print(f"Top-20 ownership overlap: {overlap20}/20")

# Mean absolute ownership gap (for players with >1% on either side)
significant = [(r[3], r[4]) for r in rows if r[3] > 1 or r[4] > 1]
gaps = [abs(a - b) for a, b in significant]
if gaps:
    print(f"Mean absolute ownership gap (>1%): {sum(gaps)/len(gaps):.1f}%")

# FP correlation
both_fp = [(r[6], r[7]) for r in rows if r[6] > 0 and r[7] > 0]
if len(both_fp) > 5:
    ours_fp = np.array([b[0] for b in both_fp])
    dk_fp_arr = np.array([b[1] for b in both_fp])
    fp_corr = np.corrcoef(ours_fp, dk_fp_arr)[0, 1]
    print(f"Projection correlation: {fp_corr:.3f} ({len(both_fp)} players)")

# Mean FP error
fp_errors = [abs(r[8]) for r in rows if r[7] > 5]
if fp_errors:
    print(f"Mean projection error (DK FP>5): {sum(fp_errors)/len(fp_errors):.1f}%")

# Biggest over/under exposures
print()
print("Biggest OVER-exposures (ours >> DK ownership):")
over = sorted(rows, key=lambda x: -(x[3] - x[4]))[:7]
for name, team, salary, our_pct, dk_own, dk_opt, our_fp, dk_fp, fp_err, dk_std in over:
    gap = our_pct - dk_own
    print(
        f"  {name:<30} {team} ${salary:,} ours={our_pct:.1f}% dk_own={dk_own:.1f}% gap={gap:+.1f}% ourFP={our_fp:.1f} dkFP={dk_fp:.1f}"
    )

print()
print("Biggest UNDER-exposures (ours << DK ownership):")
under = sorted(rows, key=lambda x: (x[3] - x[4]))[:7]
for name, team, salary, our_pct, dk_own, dk_opt, our_fp, dk_fp, fp_err, dk_std in under:
    gap = our_pct - dk_own
    print(
        f"  {name:<30} {team} ${salary:,} ours={our_pct:.1f}% dk_own={dk_own:.1f}% gap={gap:+.1f}% ourFP={our_fp:.1f} dkFP={dk_fp:.1f}"
    )

# Biggest FP errors
print()
print("Biggest projection errors (|our - DK| / DK, DK FP>10):")
fp_err_sorted = sorted(rows, key=lambda x: -abs(x[8]) if x[7] > 10 else 0)[:10]
for name, team, salary, our_pct, dk_own, dk_opt, our_fp, dk_fp, fp_err, dk_std in fp_err_sorted:
    if dk_fp > 10:
        print(
            f"  {name:<30} {team} ourFP={our_fp:.1f} dkFP={dk_fp:.1f} err={fp_err:+.1f}% our%={our_pct:.1f}% dk_own={dk_own:.1f}%"
        )

# Ghost players (in our lineups but 0 DK FP)
ghosts = [(r[0], r[1], r[2], r[3], r[6]) for r in rows if r[7] == 0 and r[3] > 1]
if ghosts:
    print()
    print("Ghost players (0 DK FP but in our lineups):")
    ghosts.sort(key=lambda x: -x[3])
    for name, team, salary, our_pct, our_fp in ghosts[:10]:
        print(
            f"  {name:<30} {team} ${salary:,} our%={our_pct:.1f}% ourFP={our_fp:.1f}"
        )

# DK high-owned missing from our lineups
dk_high_missing = [
    (r[0], r[1], r[2], r[4], r[7]) for r in rows if r[4] > 5 and r[3] == 0
]
if dk_high_missing:
    print()
    print("DK high-owned (>5%) MISSING from our lineups:")
    dk_high_missing.sort(key=lambda x: -x[3])
    for name, team, salary, dk_own, dk_fp in dk_high_missing[:10]:
        print(
            f"  {name:<30} {team} ${salary:,} dk_own={dk_own:.1f}% dkFP={dk_fp:.1f}"
        )

# Salary tier comparison
print()
print("Salary tier exposure:")
tiers = [
    (3000, 4500, "$3K-$4.5K"),
    (4500, 6000, "$4.5K-$6K"),
    (6000, 7500, "$6K-$7.5K"),
    (7500, 9000, "$7.5K-$9K"),
    (9000, 15000, "$9K+"),
]
for lo, hi, label in tiers:
    our_tier = sum(r[3] for r in rows if lo <= r[2] < hi)
    dk_tier = sum(r[4] for r in rows if lo <= r[2] < hi)
    print(f"  {label:>12}: ours={our_tier:5.0f}% dk_own={dk_tier:5.0f}%")

# Mean lineup projected FP
lineup_fps = [lu.get("total_projected_fp", 0) for lu in ours.get("lineups", [])]
if lineup_fps:
    print()
    print("Lineup stats:")
    print(f"  Mean projected FP: {np.mean(lineup_fps):.1f}")
    print(f"  Min: {min(lineup_fps):.1f} / Max: {max(lineup_fps):.1f}")
    print(f"  Std dev: {np.std(lineup_fps):.1f}")
