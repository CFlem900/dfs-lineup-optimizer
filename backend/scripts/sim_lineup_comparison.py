"""Compare our generated lineups against external sim-optimal lineups."""
import sys
import json
import time
import os
import csv
import re
import unicodedata

sys.stdout.reconfigure(encoding="utf-8")

import logging
logging.basicConfig(level=logging.WARNING, stream=sys.stderr)

from app.api.dependencies import get_services
from app.models.lineup import MultiLineupRequest
from collections import Counter, defaultdict

svc = get_services()
los = svc.lineup_optimizer_service


def normalize(n):
    n = unicodedata.normalize("NFD", n)
    n = "".join(c for c in n if unicodedata.category(c) != "Mn")
    return n.strip().lower()


# ── Load external sim lineups ────────────────────────────────────
sim_path = os.path.expanduser(
    "~/Downloads/DK_NBA_Main_Pre_Contest_Sims_Lineups (14).csv"
)
sim_lineups = []
with open(sim_path, encoding="utf-8-sig") as f:
    reader = csv.DictReader(f)
    for row in reader:
        lineup = {}
        for slot in ["PG", "SG", "SF", "PF", "C", "G", "F", "UTIL"]:
            raw = row[slot].strip()
            # Parse "Player Name (DK_ID)"
            m = re.match(r"^(.+?)\s*\((\d+)\)$", raw)
            if m:
                lineup[slot] = {
                    "name": m.group(1).strip(),
                    "dk_id": int(m.group(2)),
                }
            else:
                lineup[slot] = {"name": raw, "dk_id": 0}
        sim_lineups.append(lineup)

print(f"Loaded {len(sim_lineups)} external sim-optimal lineups")

# ── Load external projections for FP lookup ──────────────────────
ext_path = os.path.expanduser(
    "~/Downloads/DK_NBA_Main_Data_Hub_Projections (4).csv"
)
ext_by_name = {}
with open(ext_path, encoding="utf-8") as f:
    reader = csv.DictReader(f)
    for row in reader:
        name = row["Player"].strip()
        proj_str = row.get("Projection", "").strip()
        sal_str = row.get("Salary", "").strip()
        own_str = row.get("Ownership %", "0").strip() or "0"
        opt_str = row.get("Optimal %", "0").strip() or "0"
        team = row.get("Team", "").strip()
        if not proj_str or not name:
            continue
        ext_by_name[normalize(name)] = {
            "name": name,
            "proj": float(proj_str),
            "salary": int(sal_str) if sal_str else 0,
            "own": float(own_str),
            "opt": float(opt_str),
            "team": team,
        }

# ── Load our pool for FP/salary lookup ───────────────────────────
with open("cache/pool_143359_v4.json") as f:
    pool_data = json.load(f)
pool = pool_data["entries"]
pool_by_norm = {}
pool_by_dk_id = {}
for p in pool:
    pool_by_norm[normalize(p["player_name"])] = p
    dk_id = p.get("dk_player_id")
    if dk_id:
        pool_by_dk_id[dk_id] = p

# ── Analyze sim lineups ──────────────────────────────────────────
print()
print("=" * 80)
print("EXTERNAL SIM-OPTIMAL LINEUP ANALYSIS (150 lineups)")
print("=" * 80)

# Player exposure in sim lineups
sim_exposure = Counter()
sim_slot_exposure = defaultdict(Counter)
sim_lineup_ext_fps = []

for lu in sim_lineups:
    lu_ext_fp = 0
    for slot, info in lu.items():
        norm = normalize(info["name"])
        sim_exposure[norm] += 1
        sim_slot_exposure[slot][norm] += 1
        ext_p = ext_by_name.get(norm)
        if ext_p:
            lu_ext_fp += ext_p["proj"]
    sim_lineup_ext_fps.append(lu_ext_fp)

avg_sim_fp = sum(sim_lineup_ext_fps) / len(sim_lineup_ext_fps)
print(f"Avg sim lineup FP (ext projections): {avg_sim_fp:.1f}")
print(f"Best sim lineup FP: {max(sim_lineup_ext_fps):.1f}")
print(f"Worst sim lineup FP: {min(sim_lineup_ext_fps):.1f}")

# Top sim exposure
print()
print(f"{'Player':30s} {'Sim%':>5s} {'ExtFP':>6s} {'Own%':>5s} {'Opt%':>5s}")
print("-" * 60)
n_sim = len(sim_lineups)
for norm_name, cnt in sim_exposure.most_common(25):
    ext_p = ext_by_name.get(norm_name, {})
    pct = cnt / n_sim * 100
    print(
        f"{ext_p.get('name', norm_name):30s} {pct:4.0f}%  "
        f"{ext_p.get('proj', 0):5.1f}  {ext_p.get('own', 0):4.1f}%  "
        f"{ext_p.get('opt', 0):4.1f}%"
    )


# ── Generate our lineups ─────────────────────────────────────────
print()
print("=" * 80)
print("GENERATING OUR LINEUPS (20 max_projection)")
print("=" * 80)

req = MultiLineupRequest(
    platform="dk",
    sport="nba",
    draft_group_id=143359,
    game_date="2026-03-06",
    num_lineups=20,
    strategy="max_projection",
    contest_type="gpp",
    enable_stacking=True,
    max_overlap=6,
    salary_floor_pct=0.95,
    seed=42,
)
t0 = time.time()
resp = los.generate_lineups(req)
elapsed = time.time() - t0
print(
    f"Generated {resp.num_generated}/{resp.num_requested} in {elapsed:.1f}s  "
    f"pool={resp.pool_size}  ILP={resp.ilp_accepted_count}"
)

# Our exposure
our_exposure = Counter()
for lu in resp.lineups:
    for p in lu.players:
        our_exposure[normalize(p.player_name)] += 1

# ── Side-by-side exposure comparison ─────────────────────────────
print()
print("=" * 80)
print("EXPOSURE COMPARISON: OURS (20 LU) vs SIM-OPTIMAL (150 LU)")
print("=" * 80)

# Union of all players
all_players = set(sim_exposure.keys()) | set(our_exposure.keys())
comparison = []
for norm in all_players:
    sim_pct = sim_exposure.get(norm, 0) / n_sim * 100
    our_pct = our_exposure.get(norm, 0) / 20 * 100
    ext_p = ext_by_name.get(norm, {})
    pool_p = pool_by_norm.get(norm, {})
    comparison.append({
        "name": ext_p.get("name", norm),
        "team": ext_p.get("team", pool_p.get("team_abbreviation", "?")),
        "salary": ext_p.get("salary", pool_p.get("salary", 0)),
        "sim_pct": sim_pct,
        "our_pct": our_pct,
        "diff": our_pct - sim_pct,
        "ext_fp": ext_p.get("proj", 0),
        "our_fp": pool_p.get("projected_fp", 0),
        "own": ext_p.get("own", 0),
    })

# Sort by combined exposure
comparison.sort(key=lambda x: -(x["sim_pct"] + x["our_pct"]))

print(
    f"{'Player':25s} {'Team':4s} {'$':>6s} "
    f"{'Sim%':>5s} {'Our%':>5s} {'Diff':>6s} "
    f"{'ExtFP':>6s} {'OurFP':>6s} {'Own%':>5s}"
)
print("-" * 85)
for c in comparison[:35]:
    print(
        f"{c['name']:25s} {c['team']:4s} ${c['salary']:>5,} "
        f"{c['sim_pct']:4.0f}%  {c['our_pct']:4.0f}%  {c['diff']:+5.0f}%  "
        f"{c['ext_fp']:5.1f}  {c['our_fp']:5.1f}  {c['own']:4.1f}%"
    )

# ── Key differences ──────────────────────────────────────────────
print()
print("=" * 80)
print("BIGGEST EXPOSURE GAPS (sim has, we don't / we have, sim doesn't)")
print("=" * 80)

# Players sim loves but we underweight
print("\nSim OVERWEIGHT vs us (sim% - our% > 10):")
print(
    f"  {'Player':25s} {'Team':4s} {'Sim%':>5s} {'Our%':>5s} {'Gap':>6s} "
    f"{'ExtFP':>6s} {'OurFP':>6s}"
)
print("  " + "-" * 75)
over = sorted(comparison, key=lambda x: x["sim_pct"] - x["our_pct"], reverse=True)
for c in over:
    gap = c["sim_pct"] - c["our_pct"]
    if gap < 10:
        break
    print(
        f"  {c['name']:25s} {c['team']:4s} {c['sim_pct']:4.0f}%  "
        f"{c['our_pct']:4.0f}%  {gap:+5.0f}%  "
        f"{c['ext_fp']:5.1f}  {c['our_fp']:5.1f}"
    )

# Players we love but sim underweights
print("\nOur OVERWEIGHT vs sim (our% - sim% > 10):")
print(
    f"  {'Player':25s} {'Team':4s} {'Sim%':>5s} {'Our%':>5s} {'Gap':>6s} "
    f"{'ExtFP':>6s} {'OurFP':>6s}"
)
print("  " + "-" * 75)
under = sorted(comparison, key=lambda x: x["our_pct"] - x["sim_pct"], reverse=True)
for c in under:
    gap = c["our_pct"] - c["sim_pct"]
    if gap < 10:
        break
    print(
        f"  {c['name']:25s} {c['team']:4s} {c['sim_pct']:4.0f}%  "
        f"{c['our_pct']:4.0f}%  {gap:+5.0f}%  "
        f"{c['ext_fp']:5.1f}  {c['our_fp']:5.1f}"
    )

# ── FP totals comparison ─────────────────────────────────────────
print()
print("=" * 80)
print("LINEUP TOTAL FP COMPARISON (using external projections)")
print("=" * 80)

# Score our lineups using EXTERNAL projections (apples-to-apples)
our_ext_fps = []
our_our_fps = []
for lu in resp.lineups:
    ext_sum = 0
    our_sum = 0
    for p in lu.players:
        norm = normalize(p.player_name)
        ext_p = ext_by_name.get(norm)
        if not ext_p:
            for en, ev in ext_by_name.items():
                if norm in en or en in norm:
                    ext_p = ev
                    break
        ext_sum += ext_p["proj"] if ext_p else 0
        our_sum += p.projected_fp
    our_ext_fps.append(ext_sum)
    our_our_fps.append(our_sum)

avg_our_ext = sum(our_ext_fps) / len(our_ext_fps)
avg_our_our = sum(our_our_fps) / len(our_our_fps)
avg_sim_ext = sum(sim_lineup_ext_fps) / len(sim_lineup_ext_fps)

print(f"{'Metric':40s} {'Ours':>8s} {'Sim':>8s} {'Delta':>8s}")
print("-" * 65)
print(
    f"{'Avg lineup FP (ext projections)':40s} "
    f"{avg_our_ext:7.1f}  {avg_sim_ext:7.1f}  {avg_our_ext - avg_sim_ext:+7.1f}"
)
print(
    f"{'Best lineup FP (ext projections)':40s} "
    f"{max(our_ext_fps):7.1f}  {max(sim_lineup_ext_fps):7.1f}  "
    f"{max(our_ext_fps) - max(sim_lineup_ext_fps):+7.1f}"
)
print(
    f"{'Worst lineup FP (ext projections)':40s} "
    f"{min(our_ext_fps):7.1f}  {min(sim_lineup_ext_fps):7.1f}  "
    f"{min(our_ext_fps) - min(sim_lineup_ext_fps):+7.1f}"
)
print(
    f"{'Avg lineup FP (our projections)':40s} "
    f"{avg_our_our:7.1f}  {'N/A':>8s}  {'':>8s}"
)

# ── Show our lineups scored by ext projections ────────────────────
print()
print("Our 20 lineups scored by EXTERNAL projections:")
for i, (ext_fp, our_fp) in enumerate(zip(our_ext_fps, our_our_fps)):
    grade = resp.lineups[i].quality_grade or "N/A"
    sal = resp.lineups[i].total_salary
    print(
        f"  LU {i+1:2d}: ext={ext_fp:5.1f}  ours={our_fp:5.1f}  "
        f"gap={our_fp - ext_fp:+5.1f}  sal=${sal:,}  grade={grade}"
    )

# ── Common players analysis ──────────────────────────────────────
print()
print("=" * 80)
print("PLAYER OVERLAP: TOP SIM PLAYERS IN OUR LINEUPS")
print("=" * 80)

# Top 15 most-used sim players — do we use them too?
print(
    f"{'Player':25s} {'Team':4s} {'SimExp':>6s} {'OurExp':>6s} "
    f"{'InPool':>6s} {'ExtFP':>6s} {'OurFP':>6s}"
)
print("-" * 75)
for norm, cnt in sim_exposure.most_common(20):
    sim_pct = cnt / n_sim * 100
    our_cnt = our_exposure.get(norm, 0)
    our_pct = our_cnt / 20 * 100
    in_pool = "Yes" if norm in pool_by_norm else "NO"
    ext_p = ext_by_name.get(norm, {})
    pool_p = pool_by_norm.get(norm, {})
    print(
        f"{ext_p.get('name', norm):25s} "
        f"{ext_p.get('team', pool_p.get('team_abbreviation', '?')):4s} "
        f"{sim_pct:5.0f}%  {our_pct:5.0f}%  "
        f"{in_pool:>6s}  "
        f"{ext_p.get('proj', 0):5.1f}  {pool_p.get('projected_fp', 0):5.1f}"
    )

# ── Stacking comparison ─────────────────────────────────────────
print()
print("=" * 80)
print("TEAM STACKING COMPARISON")
print("=" * 80)

# Sim stacking
sim_team_stacks = Counter()
for lu in sim_lineups:
    team_counts = Counter()
    for slot, info in lu.items():
        ext_p = ext_by_name.get(normalize(info["name"]), {})
        team = ext_p.get("team", "?")
        team_counts[team] += 1
    for team, cnt in team_counts.items():
        if cnt >= 3:
            sim_team_stacks[team] += 1

# Our stacking
our_team_stacks = Counter()
for lu in resp.lineups:
    team_counts = Counter()
    for p in lu.players:
        team_counts[p.team_abbreviation] += 1
    for team, cnt in team_counts.items():
        if cnt >= 3:
            our_team_stacks[team] += 1

all_stack_teams = set(sim_team_stacks.keys()) | set(our_team_stacks.keys())
print(f"{'Team':6s} {'Sim 3+':>7s} {'Our 3+':>7s}")
print("-" * 25)
for team in sorted(all_stack_teams):
    s_cnt = sim_team_stacks.get(team, 0)
    o_cnt = our_team_stacks.get(team, 0)
    s_pct = s_cnt / n_sim * 100
    o_pct = o_cnt / 20 * 100
    print(f"{team:6s} {s_pct:5.0f}%   {o_pct:5.0f}%")
