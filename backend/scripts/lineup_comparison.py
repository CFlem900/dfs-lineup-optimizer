"""Generate lineups and compare against external projections."""
import sys
import json
import time
import os
import csv
import math
import unicodedata

sys.stdout.reconfigure(encoding="utf-8")

import logging
logging.basicConfig(level=logging.WARNING, stream=sys.stderr)

from app.api.dependencies import get_services
from app.models.lineup import MultiLineupRequest
from collections import Counter, defaultdict

svc = get_services()
los = svc.lineup_optimizer_service

# ── Generate 20 lineups per strategy ─────────────────────────────
strategies = ["max_projection", "balanced", "ceiling", "contrarian"]
all_results = {}

for strat in strategies:
    req = MultiLineupRequest(
        platform="dk",
        sport="nba",
        draft_group_id=143359,
        game_date="2026-03-06",
        num_lineups=20,
        strategy=strat,
        contest_type="gpp",
        enable_stacking=True,
        max_overlap=6,
        salary_floor_pct=0.95,
        seed=42,
    )
    t0 = time.time()
    resp = los.generate_lineups(req)
    elapsed = time.time() - t0
    all_results[strat] = resp
    print(
        f"{strat:20s}: {resp.num_generated}/{resp.num_requested} lineups "
        f"in {elapsed:.1f}s  pool={resp.pool_size}  "
        f"candidates={resp.num_candidates_generated}  "
        f"ILP={resp.ilp_accepted_count}  greedy={resp.greedy_fallback_count}"
    )

# ── Best max_projection lineup ───────────────────────────────────
print()
print("=" * 80)
print("BEST MAX_PROJECTION LINEUP")
print("=" * 80)
resp = all_results["max_projection"]
best = max(resp.lineups, key=lambda l: l.total_projected_fp)
print(
    f"Total FP: {best.total_projected_fp:.1f}  |  "
    f"Salary: ${best.total_salary:,}  |  "
    f"Remaining: ${best.salary_remaining:,}  |  "
    f"Quality: {best.quality_grade or 'N/A'} ({best.quality_score or 0:.1f})"
)
print(f"Floor: {best.total_floor_fp:.1f}  |  Ceiling: {best.total_ceiling_fp:.1f}")
print()
header = f"{'Slot':5s} {'Player':25s} {'Team':4s} {'Salary':>7s} {'FP':>6s} {'Floor':>6s} {'Ceil':>6s} {'Min':>5s}"
print(header)
print("-" * len(header))
for p in best.players:
    print(
        f"{p.roster_slot:5s} {p.player_name:25s} {p.team_abbreviation:4s} "
        f"${p.salary:>6,}  {p.projected_fp:5.1f}  {p.floor_fp:5.1f}  "
        f"{p.ceiling_fp:5.1f}  {p.projected_minutes:4.1f}"
    )

# ── Lineup FP distribution ───────────────────────────────────────
print()
print("=" * 80)
print("LINEUP FP DISTRIBUTION BY STRATEGY")
print("=" * 80)
for strat in strategies:
    resp = all_results[strat]
    fps = sorted([l.total_projected_fp for l in resp.lineups], reverse=True)
    sals = [l.total_salary for l in resp.lineups]
    avg_fp = sum(fps) / len(fps) if fps else 0
    avg_sal = sum(sals) / len(sals) if sals else 0
    grades = [l.quality_grade or "N/A" for l in resp.lineups]
    grade_dist = Counter(grades)
    print(
        f"{strat:20s}: avg={avg_fp:5.1f}fp  best={fps[0]:5.1f}  "
        f"worst={fps[-1]:5.1f}  avg_sal=${avg_sal:,.0f}  "
        f"grades={dict(grade_dist)}"
    )

# ── Player exposure (max_projection) ─────────────────────────────
print()
print("=" * 80)
print("PLAYER EXPOSURE (max_projection, 20 lineups)")
print("=" * 80)
resp = all_results["max_projection"]
exposure = Counter()
for lu in resp.lineups:
    for p in lu.players:
        exposure[f"{p.player_name}|{p.team_abbreviation}|{p.salary}"] += 1
header2 = f"{'Player':30s} {'Team':4s} {'Salary':>7s} {'Count':>5s} {'Exp%':>5s}"
print(header2)
print("-" * len(header2))
for key, cnt in exposure.most_common(30):
    name, team, sal = key.split("|")
    pct = cnt / 20 * 100
    print(f"{name:30s} {team:4s} ${int(sal):>6,}  {cnt:4d}  {pct:4.0f}%")

# ── Compare lineup players vs external projections ───────────────
print()
print("=" * 80)
print("LINEUP PLAYERS vs EXTERNAL PROJECTIONS")
print("=" * 80)

ext_path = os.path.expanduser("~/Downloads/DK_NBA_Main_Data_Hub_Projections (4).csv")
ext = {}
with open(ext_path, encoding="utf-8") as f:
    reader = csv.DictReader(f)
    for row in reader:
        name = row["Player"].strip()
        proj_str = row.get("Projection", "").strip()
        if not proj_str or not name:
            continue
        ext[name.lower()] = {
            "name": name,
            "proj": float(proj_str),
            "own": float(row.get("Ownership %", "0").strip() or "0"),
            "opt": float(row.get("Optimal %", "0").strip() or "0"),
        }

def normalize(n):
    n = unicodedata.normalize("NFD", n)
    n = "".join(c for c in n if unicodedata.category(c) != "Mn")
    return n.strip().lower()

# Build lookup
ext_by_norm = {}
for k, v in ext.items():
    ext_by_norm[normalize(v["name"])] = v

# For best lineup, compare each player
best = max(all_results["max_projection"].lineups, key=lambda l: l.total_projected_fp)
total_our = 0
total_ext = 0
print(
    f"{'Slot':5s} {'Player':25s} {'Team':4s} {'Our FP':>7s} {'Ext FP':>7s} "
    f"{'Diff':>6s} {'Own%':>5s} {'Opt%':>5s}"
)
print("-" * 75)
for p in best.players:
    norm_name = normalize(p.player_name)
    ext_p = ext_by_norm.get(norm_name)
    if not ext_p:
        for en, ev in ext_by_norm.items():
            if norm_name in en or en in norm_name:
                ext_p = ev
                break
    our_fp = p.projected_fp
    ext_fp = ext_p["proj"] if ext_p else 0
    own = ext_p["own"] if ext_p else 0
    opt = ext_p["opt"] if ext_p else 0
    diff = our_fp - ext_fp
    total_our += our_fp
    total_ext += ext_fp
    print(
        f"{p.roster_slot:5s} {p.player_name:25s} {p.team_abbreviation:4s} "
        f"{our_fp:6.1f}  {ext_fp:6.1f}  {diff:+5.1f}  {own:4.1f}  {opt:4.1f}"
    )
print("-" * 75)
print(
    f"{'':5s} {'TOTAL':25s} {'':4s} "
    f"{total_our:6.1f}  {total_ext:6.1f}  {total_our - total_ext:+5.1f}"
)

# ── Aggregate across all 20 lineups ──────────────────────────────
print()
print("=" * 80)
print("AGGREGATE: 20 MAX_PROJECTION LINEUPS vs EXTERNAL")
print("=" * 80)
resp = all_results["max_projection"]
lineup_totals_our = []
lineup_totals_ext = []
for lu in resp.lineups:
    our_sum = sum(p.projected_fp for p in lu.players)
    ext_sum = 0
    for p in lu.players:
        norm_name = normalize(p.player_name)
        ext_p = ext_by_norm.get(norm_name)
        if not ext_p:
            for en, ev in ext_by_norm.items():
                if norm_name in en or en in norm_name:
                    ext_p = ev
                    break
        ext_sum += ext_p["proj"] if ext_p else 0
    lineup_totals_our.append(our_sum)
    lineup_totals_ext.append(ext_sum)

avg_our = sum(lineup_totals_our) / len(lineup_totals_our)
avg_ext = sum(lineup_totals_ext) / len(lineup_totals_ext)
diffs = [o - e for o, e in zip(lineup_totals_our, lineup_totals_ext)]
avg_diff = sum(diffs) / len(diffs)
max_diff = max(diffs)
min_diff = min(diffs)

print(f"Avg our total FP:  {avg_our:.1f}")
print(f"Avg ext total FP:  {avg_ext:.1f}")
print(f"Avg difference:    {avg_diff:+.1f}")
print(f"Max difference:    {max_diff:+.1f}")
print(f"Min difference:    {min_diff:+.1f}")
print()

# Show each lineup's totals
for i, (o, e) in enumerate(zip(lineup_totals_our, lineup_totals_ext)):
    grade = resp.lineups[i].quality_grade or "N/A"
    sal = resp.lineups[i].total_salary
    print(
        f"  Lineup {i+1:2d}: our={o:5.1f}  ext={e:5.1f}  "
        f"diff={o-e:+5.1f}  sal=${sal:,}  grade={grade}"
    )

# ── Ownership comparison ─────────────────────────────────────────
print()
print("=" * 80)
print("OWNERSHIP EXPOSURE vs CONSENSUS")
print("=" * 80)
# For max_projection lineups, compare our exposure to consensus ownership
player_data = {}
for lu in all_results["max_projection"].lineups:
    for p in lu.players:
        key = normalize(p.player_name)
        if key not in player_data:
            ext_p = ext_by_norm.get(key)
            if not ext_p:
                for en, ev in ext_by_norm.items():
                    if key in en or en in key:
                        ext_p = ev
                        break
            player_data[key] = {
                "name": p.player_name,
                "team": p.team_abbreviation,
                "salary": p.salary,
                "count": 0,
                "own": ext_p["own"] if ext_p else 0,
                "opt": ext_p["opt"] if ext_p else 0,
                "our_fp": p.projected_fp,
                "ext_fp": ext_p["proj"] if ext_p else 0,
            }
        player_data[key]["count"] += 1

sorted_players = sorted(player_data.values(), key=lambda x: -x["count"])
print(
    f"{'Player':25s} {'Team':4s} {'$':>6s} {'Exp%':>5s} {'Own%':>5s} "
    f"{'Opt%':>5s} {'Lev':>5s} {'OurFP':>6s} {'ExtFP':>6s}"
)
print("-" * 80)
for pd in sorted_players[:20]:
    exp = pd["count"] / 20 * 100
    leverage = exp - pd["own"]
    print(
        f"{pd['name']:25s} {pd['team']:4s} ${pd['salary']:>5,} "
        f"{exp:4.0f}%  {pd['own']:4.1f}%  {pd['opt']:4.1f}%  "
        f"{leverage:+4.1f}  {pd['our_fp']:5.1f}  {pd['ext_fp']:5.1f}"
    )
