"""Compare V4 lineups (sparse data + novelty bonus) against DK pre-contest data.

Reads:
  - lineups_mar10_v4.json          (our V4 generation)
  - DK_NBA_Main_Pre_Contest_Lineups.csv  (DK contest ownership/projections)

Outputs:
  - Projection correlation
  - Top-10 overlap
  - Ownership comparison (our exposure vs DK field ownership)
  - Exploration metrics (how many pool players drafted)
  - Sparse data heuristic impact check
"""

import csv
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

os.environ["PYTHONIOENCODING"] = "utf-8"
sys.stdout.reconfigure(encoding="utf-8")  # type: ignore

DK_CSV = Path(r"C:\Users\CFlem\Downloads\DK_NBA_Main_Pre_Contest_Lineups.csv")
V4_JSON = Path(__file__).parent / "lineups_mar10_v4.json"

# ── 1. Parse V4 lineups ─────────────────────────────────────────────

with open(V4_JSON, encoding="utf-8") as f:
    v4 = json.load(f)

lineups = v4["lineups"]
pool_size = v4.get("pool_size", "?")
gen_time = v4.get("generation_time_ms", 0) / 1000
num_gen = v4.get("num_generated", len(lineups))
num_req = v4.get("num_requested", "?")

# Build player projection map and exposure counts from V4
v4_proj: dict[str, float] = {}  # name -> projected_fp
v4_salary: dict[str, int] = {}
v4_team: dict[str, str] = {}
v4_exposure: Counter = Counter()
v4_total_lineups = len(lineups)

for lu in lineups:
    for p in lu["players"]:
        name = p["display_name"]
        v4_proj[name] = p["projected_fp"]
        v4_salary[name] = p["salary"]
        v4_team[name] = p["team_abbreviation"]
        v4_exposure[name] += 1

# ── 2. Parse DK contest CSV ──────────────────────────────────────────

# Format: ROI, Projected FP, OwnSum, Win%, Top10%, Cash%, Dupes, Lineups, Salary,
#         PG, SG, SF, PF, C, G, F, UTIL
# Player cells: "Name (DK_ID)"

dk_exposure: Counter = Counter()
dk_proj: dict[str, float] = {}
dk_total_lineups = 0

# Extract projected FP from the "Lineups" column player names
name_pattern = re.compile(r"^(.+?)\s*\((\d+)\)$")

with open(DK_CSV, encoding="utf-8") as f:
    reader = csv.DictReader(f)
    for row in reader:
        dk_total_lineups += 1
        # Parse player names from slot columns
        slot_cols = ["PG", "SG", "SF", "PF", "C", "G", "F", "UTIL"]
        for col in slot_cols:
            cell = row.get(col, "").strip()
            m = name_pattern.match(cell)
            if m:
                pname = m.group(1).strip()
                dk_exposure[pname] += 1

# DK projected FP per lineup (column "Projected FP")
dk_lineup_fps = []
with open(DK_CSV, encoding="utf-8") as f:
    reader = csv.DictReader(f)
    for row in reader:
        try:
            dk_lineup_fps.append(float(row["Projected FP"]))
        except (ValueError, KeyError):
            pass

# ── 3. Compute DK ownership % ────────────────────────────────────────

dk_own_pct: dict[str, float] = {
    name: count / dk_total_lineups * 100
    for name, count in dk_exposure.items()
}

v4_own_pct: dict[str, float] = {
    name: count / v4_total_lineups * 100
    for name, count in v4_exposure.items()
}

# ── 4. Normalize names for matching ──────────────────────────────────

def normalize(n: str) -> str:
    n = n.lower().strip()
    n = re.sub(r"\s+(jr|sr|ii|iii|iv|v)\.?$", "", n)
    n = re.sub(r"['\-\.]", "", n)
    return " ".join(n.split())

# Build normalized lookup for DK ownership
dk_own_norm = {normalize(k): (k, v) for k, v in dk_own_pct.items()}

# ── 5. Projection comparison ─────────────────────────────────────────

print("=" * 70)
print("V4 LINEUPS vs DK PRE-CONTEST COMPARISON")
print("=" * 70)
print()
print(f"V4 Generation: {num_gen}/{num_req} lineups | Pool: {pool_size} | Time: {gen_time:.1f}s")
print(f"DK Contest: {dk_total_lineups} lineups from pre-contest data")
print()

# ── 6. Ownership comparison ──────────────────────────────────────────

print("─" * 70)
print("TOP 20 DK OWNERSHIP vs OUR EXPOSURE")
print("─" * 70)

# Sort DK by ownership desc
dk_top = sorted(dk_own_pct.items(), key=lambda x: -x[1])[:30]

print(f"{'Player':<25} {'DK Own%':>8} {'V4 Exp%':>8} {'Delta':>8} {'Salary':>7}")
print("─" * 70)

matches_found = 0
for dk_name, dk_pct in dk_top[:20]:
    nk = normalize(dk_name)
    # Find matching V4 player
    v4_pct = 0.0
    sal = ""
    for v4n, v4p in v4_own_pct.items():
        if normalize(v4n) == nk:
            v4_pct = v4p
            sal = f"${v4_salary.get(v4n, 0):,}"
            matches_found += 1
            break
    delta = v4_pct - dk_pct
    flag = "  " if abs(delta) < 10 else ("▲" if delta > 0 else "▼")
    print(f"{dk_name:<25} {dk_pct:>7.1f}% {v4_pct:>7.1f}% {delta:>+7.1f}% {sal:>7} {flag}")

# ── 7. Our high-exposure players not in DK top ───────────────────────

print()
print("─" * 70)
print("V4 HIGH-EXPOSURE PLAYERS (>20%) NOT IN DK TOP-20")
print("─" * 70)

dk_top_names = {normalize(n) for n, _ in dk_top[:20]}
for v4n, v4p in sorted(v4_own_pct.items(), key=lambda x: -x[1]):
    if v4p < 20:
        break
    if normalize(v4n) not in dk_top_names:
        sal = v4_salary.get(v4n, 0)
        proj = v4_proj.get(v4n, 0)
        print(f"  {v4n:<25} Exp: {v4p:.1f}% | Proj: {proj:.1f} FP | ${sal:,}")

# ── 8. Exploration metrics ───────────────────────────────────────────

print()
print("─" * 70)
print("EXPLORATION METRICS")
print("─" * 70)

unique_players = len(v4_exposure)
never_drafted = pool_size - unique_players if isinstance(pool_size, int) else "?"
print(f"Pool size: {pool_size}")
print(f"Unique players drafted: {unique_players}")
print(f"Never selected: {never_drafted}")
print(f"Exploration rate: {unique_players}/{pool_size} = {unique_players/pool_size*100:.1f}%" if isinstance(pool_size, int) else "")

# Exposure distribution
exp_vals = sorted(v4_exposure.values(), reverse=True)
print(f"\nExposure distribution:")
print(f"  Max exposure: {exp_vals[0]}/{v4_total_lineups} ({exp_vals[0]/v4_total_lineups*100:.1f}%)")
print(f"  Players > 50%: {sum(1 for v in exp_vals if v/v4_total_lineups > 0.5)}")
print(f"  Players > 25%: {sum(1 for v in exp_vals if v/v4_total_lineups > 0.25)}")
print(f"  Players 1-5%: {sum(1 for v in exp_vals if v/v4_total_lineups <= 0.05 and v > 0)}")

# ── 9. Lineup quality ────────────────────────────────────────────────

print()
print("─" * 70)
print("LINEUP QUALITY")
print("─" * 70)

v4_fps = [lu["total_projected_fp"] for lu in lineups]
v4_sals = [lu["total_salary"] for lu in lineups]

print(f"V4 mean projected FP: {sum(v4_fps)/len(v4_fps):.1f}")
print(f"V4 median FP: {sorted(v4_fps)[len(v4_fps)//2]:.1f}")
print(f"V4 min/max FP: {min(v4_fps):.1f} / {max(v4_fps):.1f}")
print(f"V4 mean salary: ${sum(v4_sals)/len(v4_sals):,.0f}")

if dk_lineup_fps:
    print(f"\nDK contest mean projected FP: {sum(dk_lineup_fps)/len(dk_lineup_fps):.1f}")
    print(f"DK contest median FP: {sorted(dk_lineup_fps)[len(dk_lineup_fps)//2]:.1f}")
    print(f"DK contest min/max FP: {min(dk_lineup_fps):.1f} / {max(dk_lineup_fps):.1f}")

# ── 10. Team distribution ────────────────────────────────────────────

print()
print("─" * 70)
print("TEAM DISTRIBUTION")
print("─" * 70)

team_counts: Counter = Counter()
for lu in lineups:
    for p in lu["players"]:
        team_counts[p["team_abbreviation"]] += 1

total_slots = sum(team_counts.values())
for team, cnt in sorted(team_counts.items(), key=lambda x: -x[1]):
    pct = cnt / total_slots * 100
    bar = "█" * int(pct / 2)
    print(f"  {team:<5} {cnt:>4} slots ({pct:>5.1f}%) {bar}")

print(f"\nTeams represented: {len(team_counts)}")

# ── 11. Key sparse-data players check ─────────────────────────────────

print()
print("─" * 70)
print("SPARSE DATA PLAYERS CHECK")
print("─" * 70)

sparse_check = ["Kel'el Ware", "Kelel Ware"]
for name in sorted(v4_proj.keys()):
    if any(sc.lower() in name.lower() for sc in sparse_check):
        sal = v4_salary.get(name, 0)
        proj = v4_proj.get(name, 0)
        exp = v4_exposure.get(name, 0)
        print(f"  {name}: Proj {proj:.1f} FP | ${sal:,} | Exp: {exp}/{v4_total_lineups} ({exp/v4_total_lineups*100:.1f}%)")

# Also check for players with roster_change or low-salary patterns
print("\nPlayers with potentially sparse data (low salary + low exposure):")
for name, proj in sorted(v4_proj.items(), key=lambda x: -x[1]):
    sal = v4_salary.get(name, 0)
    exp = v4_exposure.get(name, 0)
    nk = normalize(name)
    dk_info = dk_own_norm.get(nk)
    if dk_info and dk_info[1] > 10 and exp == 0:
        print(f"  {name}: V4 Proj {proj:.1f} FP, ${sal:,} | DK Own: {dk_info[1]:.1f}% | V4 Exp: 0% ← MISSING")

print()
print("=" * 70)
print("COMPARISON COMPLETE")
print("=" * 70)
