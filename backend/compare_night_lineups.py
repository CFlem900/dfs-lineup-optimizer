"""Compare our generated lineups vs external DK Data Hub sim-optimal lineups.

Usage:
    cd backend && ./venv/Scripts/python compare_night_lineups.py
"""
import csv
import sys
import re
import time
import math
import unicodedata
import io

sys.path.insert(0, ".")
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import logging

logging.basicConfig(level=logging.WARNING, stream=sys.stderr)

from collections import Counter, defaultdict
from app.models.lineup import PlayerPoolEntry, MultiLineupRequest
from app.api.dependencies import get_services

svc = get_services()
los = svc.lineup_optimizer_service

# ── Config ────────────────────────────────────────────────────────
SIM_CSV = r"C:\Users\CFlem\Downloads\DK_NBA_Main_Pre_Contest_Sims_Lineups (17).csv"
EXT_PROJ_CSV = r"C:\Users\CFlem\Downloads\DK_NBA_Main_Data_Hub_Projections (11).csv"
NUM_LINEUPS = 150
GAME_DATE = "2026-03-11"

# Position -> eligible DK roster slots
SLOT_ELIG = {
    "PG": ["PG", "G", "UTIL"],
    "SG": ["SG", "G", "UTIL"],
    "SF": ["SF", "F", "UTIL"],
    "PF": ["PF", "F", "UTIL"],
    "C": ["C", "UTIL"],
}


def normalize(n):
    n = unicodedata.normalize("NFD", n)
    n = "".join(c for c in n if unicodedata.category(c) != "Mn")
    return n.strip().lower()


def parse_player(cell):
    """Parse 'Player Name (DK_ID)' -> (name, dk_id)"""
    m = re.match(r"^(.+?)\s*\((\d+)\)$", cell.strip())
    if m:
        return m.group(1).strip(), int(m.group(2))
    return cell.strip(), 0


def get_eligible_slots(position: str) -> list:
    """Map DK position string to eligible roster slots."""
    positions = [p.strip() for p in position.split("/")]
    slots = set()
    for pos in positions:
        slots.update(SLOT_ELIG.get(pos, ["UTIL"]))
    return sorted(slots)


# ── 1. Parse external projections ─────────────────────────────────
ext_by_name = {}  # normalized name -> {name, salary, position, team, proj, own, ...}
ext_by_dk_id = {}  # dk_player_id -> same dict

with open(EXT_PROJ_CSV, "r", encoding="utf-8-sig") as f:
    reader = csv.DictReader(f)
    for row in reader:
        name = row["Player"].strip()
        if not name:
            continue
        proj_str = row.get("Projection", "").strip()
        sal_str = row.get("Salary", "").strip()
        own_str = row.get("Ownership %", "0").strip() or "0"
        opt_str = row.get("Optimal %", "0").strip() or "0"
        team = row.get("Team", "").strip()
        position = row.get("Position", "").strip()
        mins = float(row.get("Minutes", "0") or "0")
        info = {
            "name": name,
            "proj": float(proj_str) if proj_str else 0.0,
            "salary": int(sal_str) if sal_str else 0,
            "own": float(own_str),
            "opt": float(opt_str),
            "team": team,
            "position": position,
            "minutes": mins,
            "ceiling": float(row.get("Ceiling", "0") or "0"),
            "floor": float(row.get("Floor", "0") or "0"),
        }
        ext_by_name[normalize(name)] = info

print(f"Loaded {len(ext_by_name)} players from external projections")

# ── 2. Parse external sim lineups ─────────────────────────────────
SLOTS = ["PG", "SG", "SF", "PF", "C", "G", "F", "UTIL"]
sim_lineups = []

import os
if os.path.exists(SIM_CSV):
    with open(SIM_CSV, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            lineup = {}
            for slot in SLOTS:
                name, dk_id = parse_player(row[slot])
                lineup[slot] = {"name": name, "dk_id": dk_id, "norm": normalize(name)}
            sim_lineups.append(lineup)
    n_sim = len(sim_lineups)
    print(f"Loaded {n_sim} external sim-optimal lineups")
else:
    n_sim = 0
    print(f"[WARN] Sim CSV not found: {SIM_CSV}")
    print("       Will generate our lineups only (no sim comparison).")

# ── 3. Analyze external sim lineups ──────────────────────────────
sim_exposure = Counter()
sim_slot_exposure = defaultdict(Counter)
sim_lineup_fps = []
sim_lineup_sals = []
sim_stack_counter = Counter()
avg_sim_fp = 0
avg_sim_sal = 0

if n_sim > 0:
    print()
    print("=" * 80)
    print(f"EXTERNAL SIM-OPTIMAL LINEUP ANALYSIS ({n_sim} lineups)")
    print("=" * 80)

    for lu in sim_lineups:
        lu_fp = 0
        lu_sal = 0
        for slot, info in lu.items():
            sim_exposure[info["norm"]] += 1
            sim_slot_exposure[slot][info["norm"]] += 1
            ext_p = ext_by_name.get(info["norm"])
            if ext_p:
                lu_fp += ext_p["proj"]
                lu_sal += ext_p["salary"]
        sim_lineup_fps.append(lu_fp)
        sim_lineup_sals.append(lu_sal)

    avg_sim_fp = sum(sim_lineup_fps) / n_sim
    avg_sim_sal = sum(sim_lineup_sals) / n_sim
    print(f"Avg sim lineup FP:     {avg_sim_fp:.1f}")
    print(f"Best sim lineup FP:    {max(sim_lineup_fps):.1f}")
    print(f"Worst sim lineup FP:   {min(sim_lineup_fps):.1f}")
    print(f"Avg sim salary:        ${avg_sim_sal:,.0f}")

    # Top sim exposure
    print()
    print(f"{'Player':30s} {'Sim%':>5s} {'ExtFP':>6s} {'Sal':>6s} {'Own%':>5s} {'Opt%':>5s}")
    print("-" * 65)
    for norm_name, cnt in sim_exposure.most_common(30):
        ext_p = ext_by_name.get(norm_name, {})
        pct = cnt / n_sim * 100
        print(
            f"{ext_p.get('name', norm_name):30s} {pct:4.0f}%  "
            f"{ext_p.get('proj', 0):5.1f}  ${ext_p.get('salary', 0):>5,}  "
            f"{ext_p.get('own', 0):4.1f}%  {ext_p.get('opt', 0):4.1f}%"
        )

    # Team stacking in sim lineups
    print()
    print("SIM TEAM STACKING (3+ players from same team):")
    for lu in sim_lineups:
        team_counts = Counter()
        for slot, info in lu.items():
            ext_p = ext_by_name.get(info["norm"], {})
            team = ext_p.get("team", "?")
            team_counts[team] += 1
        for team, cnt in team_counts.items():
            if cnt >= 3 and team != "?":
                sim_stack_counter[team] += 1

    for team, cnt in sim_stack_counter.most_common():
        print(f"  {team}: {cnt}/{n_sim} lineups ({cnt / n_sim * 100:.0f}%)")

# ── 4. Build player pool from projection CSV + our projections ───
# Load our engine projections from the compare_night_projections output
# But easier: just build the pool from ext projection data (salary/position)
# and use our engine's projections by re-running it.
# Simplest: build pool from DK CSV with ext projections as FP baseline.
print()
print("=" * 80)
print(f"GENERATING OUR {NUM_LINEUPS} LINEUPS")
print("=" * 80)

# Build PlayerPoolEntry objects from the projection CSV
pool = []
for idx, (norm, info) in enumerate(ext_by_name.items()):
    if info["proj"] <= 0 or info["salary"] <= 0:
        continue  # Skip OUT players
    position = info["position"]
    eligible = get_eligible_slots(position)
    pool.append(
        PlayerPoolEntry(
            player_id=idx + 1,
            player_name=info["name"],
            display_name=info["name"],
            position=position,
            team_abbreviation=info["team"],
            salary=info["salary"],
            projected_fp=info["proj"],
            floor_fp=info["floor"],
            ceiling_fp=info["ceiling"],
            projected_minutes=info["minutes"],
            dk_value=info["proj"] / info["salary"] * 1000 if info["salary"] else 0,
            eligible_slots=eligible,
            dk_player_id=0,
            estimated_ownership=info["own"],
        )
    )

print(f"Pool: {len(pool)} players with projections")

# Generate lineups using the optimizer's internal ILP solver
# We need to bypass build_player_pool and inject our own pool
t0 = time.time()
try:
    req = MultiLineupRequest(
        platform="dk",
        sport="nba",
        draft_group_id=0,  # dummy — we'll inject pool
        game_date=GAME_DATE,
        num_lineups=NUM_LINEUPS,
        strategy="max_projection",
        contest_type="gpp",
        enable_stacking=True,
        max_overlap=6,
        salary_floor_pct=0.95,
        seed=42,
    )

    # Monkey-patch: inject our pool into the enriched cache
    from app.services.lineup_optimizer_service import (
        _cache_key,
        _enriched_cache,
        _enriched_lock,
    )

    enrich_key = _cache_key("nba:dk", 0, GAME_DATE)
    with _enriched_lock:
        _enriched_cache[enrich_key] = (time.time(), pool)

    # Also need to make build_player_pool return our pool
    # (generate_lineups calls it first, but enriched cache skips most work)
    _orig_build = los.build_player_pool

    def _patched_build(*args, **kwargs):
        return list(pool)

    los.build_player_pool = _patched_build
    try:
        result = los.generate_lineups(req)
    finally:
        los.build_player_pool = _orig_build

    elapsed = time.time() - t0
    our_lineups = result.lineups
    print(
        f"Generated {result.num_generated}/{result.num_requested} in {elapsed:.1f}s  "
        f"pool={result.pool_size}  ILP={result.ilp_accepted_count}  "
        f"greedy={result.greedy_fallback_count}"
    )
except Exception as e:
    elapsed = time.time() - t0
    print(f"ERROR generating lineups ({elapsed:.1f}s): {e}")
    import traceback

    traceback.print_exc()
    our_lineups = []

if not our_lineups:
    print("No lineups generated — showing external analysis only.")
    sys.exit(0)

# ── 5. Build our exposure map ─────────────────────────────────────
our_n = len(our_lineups)
our_exposure = Counter()
our_team_exposure = Counter()
our_lineup_fps = []
our_lineup_sals = []

for lu in our_lineups:
    lu_fp = 0
    lu_sal = 0
    for p in lu.players:
        norm = normalize(p.player_name)
        our_exposure[norm] += 1
        our_team_exposure[p.team_abbreviation] += 1
        # Score using EXTERNAL projections (apples-to-apples)
        ext_p = ext_by_name.get(norm, {})
        lu_fp += ext_p.get("proj", p.projected_fp)
        lu_sal += p.salary
    our_lineup_fps.append(lu_fp)
    our_lineup_sals.append(lu_sal)

our_pct = {k: v / our_n * 100 for k, v in our_exposure.items()}
sim_pct = {k: v / n_sim * 100 for k, v in sim_exposure.items()} if n_sim > 0 else {}

# ── 6. Exposure analysis ─────────────────────────────────────────
all_players = set(sim_exposure.keys()) | set(our_exposure.keys())
comparison = []
for norm in all_players:
    s_pct = sim_pct.get(norm, 0)
    o_pct = our_pct.get(norm, 0)
    ext_p = ext_by_name.get(norm, {})
    comparison.append(
        {
            "name": ext_p.get("name", norm),
            "team": ext_p.get("team", "?"),
            "salary": ext_p.get("salary", 0),
            "sim_pct": s_pct,
            "our_pct": o_pct,
            "diff": o_pct - s_pct,
            "ext_fp": ext_p.get("proj", 0),
            "own": ext_p.get("own", 0),
        }
    )

comparison.sort(key=lambda x: -(x["sim_pct"] + x["our_pct"]))

if n_sim > 0:
    print()
    print("=" * 80)
    print(f"EXPOSURE COMPARISON: OURS ({our_n} LU) vs SIM-OPTIMAL ({n_sim} LU)")
    print("=" * 80)
    print(
        f"{'Player':25s} {'Team':4s} {'$':>6s} "
        f"{'Sim%':>5s} {'Our%':>5s} {'Diff':>6s} "
        f"{'ExtFP':>6s} {'Own%':>5s}"
    )
    print("-" * 75)
    for c in comparison:
        if c["sim_pct"] > 0 or c["our_pct"] > 2:
            print(
                f"{c['name']:25s} {c['team']:4s} ${c['salary']:>5,} "
                f"{c['sim_pct']:4.0f}%  {c['our_pct']:4.0f}%  {c['diff']:+5.0f}%  "
                f"{c['ext_fp']:5.1f}  {c['own']:4.1f}%"
            )

    # ── 7. Biggest exposure disagreements ─────────────────────────
    print()
    print("=" * 80)
    print("BIGGEST EXPOSURE GAPS")
    print("=" * 80)

    print("\nSim OVERWEIGHT vs us (sim% - our% > 10):")
    print(
        f"  {'Player':25s} {'Team':4s} {'Sim%':>5s} {'Our%':>5s} {'Gap':>6s} "
        f"{'ExtFP':>6s} {'$':>6s}"
    )
    print("  " + "-" * 72)
    over = sorted(comparison, key=lambda x: x["sim_pct"] - x["our_pct"], reverse=True)
    for c in over:
        gap = c["sim_pct"] - c["our_pct"]
        if gap < 10:
            break
        print(
            f"  {c['name']:25s} {c['team']:4s} {c['sim_pct']:4.0f}%  "
            f"{c['our_pct']:4.0f}%  {gap:+5.0f}%  "
            f"{c['ext_fp']:5.1f}  ${c['salary']:>5,}"
        )

    print("\nOur OVERWEIGHT vs sim (our% - sim% > 10):")
    print(
        f"  {'Player':25s} {'Team':4s} {'Sim%':>5s} {'Our%':>5s} {'Gap':>6s} "
        f"{'ExtFP':>6s} {'$':>6s}"
    )
    print("  " + "-" * 72)
    under = sorted(comparison, key=lambda x: x["our_pct"] - x["sim_pct"], reverse=True)
    for c in under:
        gap = c["our_pct"] - c["sim_pct"]
        if gap < 10:
            break
        print(
            f"  {c['name']:25s} {c['team']:4s} {c['sim_pct']:4.0f}%  "
            f"{c['our_pct']:4.0f}%  {gap:+5.0f}%  "
            f"{c['ext_fp']:5.1f}  ${c['salary']:>5,}"
        )
else:
    # No sim data — show our exposure only
    print()
    print("=" * 80)
    print(f"OUR EXPOSURE ({our_n} lineups) — No sim data for comparison")
    print("=" * 80)
    print(
        f"{'Player':25s} {'Team':4s} {'$':>6s} "
        f"{'Our%':>5s} {'ExtFP':>6s} {'Val':>5s} {'Own%':>5s}"
    )
    print("-" * 70)
    for c in comparison:
        if c["our_pct"] > 1:
            val = c["ext_fp"] / (c["salary"] / 1000) if c["salary"] else 0
            print(
                f"{c['name']:25s} {c['team']:4s} ${c['salary']:>5,} "
                f"{c['our_pct']:4.0f}%  {c['ext_fp']:5.1f}  {val:4.1f}x  "
                f"{c['own']:4.1f}%"
            )

# ── 8. FP totals ─────────────────────────────────────────────────
print()
print("=" * 80)
print("LINEUP TOTAL FP COMPARISON (using external projections)")
print("=" * 80)

avg_our_fp = sum(our_lineup_fps) / our_n
avg_our_sal = sum(our_lineup_sals) / our_n

if n_sim > 0:
    print(f"{'Metric':40s} {'Ours':>8s} {'Sim':>8s} {'Delta':>8s}")
    print("-" * 65)
    print(
        f"{'Avg lineup FP (ext proj)':40s} "
        f"{avg_our_fp:7.1f}  {avg_sim_fp:7.1f}  {avg_our_fp - avg_sim_fp:+7.1f}"
    )
    print(
        f"{'Best lineup FP (ext proj)':40s} "
        f"{max(our_lineup_fps):7.1f}  {max(sim_lineup_fps):7.1f}  "
        f"{max(our_lineup_fps) - max(sim_lineup_fps):+7.1f}"
    )
    print(
        f"{'Worst lineup FP (ext proj)':40s} "
        f"{min(our_lineup_fps):7.1f}  {min(sim_lineup_fps):7.1f}  "
        f"{min(our_lineup_fps) - min(sim_lineup_fps):+7.1f}"
    )
    print(
        f"{'Avg salary':40s} "
        f"${avg_our_sal:>6,.0f}  ${avg_sim_sal:>6,.0f}  "
        f"{avg_our_sal - avg_sim_sal:+7.0f}"
    )
else:
    print(f"{'Metric':40s} {'Ours':>8s}")
    print("-" * 50)
    print(f"{'Avg lineup FP (ext proj)':40s} {avg_our_fp:7.1f}")
    print(f"{'Best lineup FP (ext proj)':40s} {max(our_lineup_fps):7.1f}")
    print(f"{'Worst lineup FP (ext proj)':40s} {min(our_lineup_fps):7.1f}")
    print(f"{'Avg salary':40s} ${avg_our_sal:>6,.0f}")

# ── 9. Team stacking ─────────────────────────────────────────────
print()
print("=" * 80)
print("TEAM STACKING (3+ players from same team)")
print("=" * 80)

our_stack_counter = Counter()
for lu in our_lineups:
    team_counts = Counter()
    for p in lu.players:
        team_counts[p.team_abbreviation] += 1
    for team, cnt in team_counts.items():
        if cnt >= 3:
            our_stack_counter[team] += 1

all_stack_teams = sorted(set(sim_stack_counter.keys()) | set(our_stack_counter.keys()))
if n_sim > 0:
    print(f"{'Team':6s} {'Sim 3+':>8s} {'Our 3+':>8s}")
    print("-" * 30)
    for team in all_stack_teams:
        s_pct = sim_stack_counter.get(team, 0) / n_sim * 100
        o_pct = our_stack_counter.get(team, 0) / our_n * 100
        print(f"{team:6s} {s_pct:6.0f}%   {o_pct:6.0f}%")
else:
    print(f"{'Team':6s} {'Our 3+':>8s}")
    print("-" * 20)
    for team in all_stack_teams:
        o_pct = our_stack_counter.get(team, 0) / our_n * 100
        print(f"{team:6s} {o_pct:6.0f}%")

# ── 10. Correlation ──────────────────────────────────────────────
if n_sim > 0:
    both = list(all_players)
    o_vals = [our_pct.get(n, 0) for n in both]
    e_vals = [sim_pct.get(n, 0) for n in both]
    n = len(o_vals)
    mo = sum(o_vals) / n
    me = sum(e_vals) / n
    cov = sum((a - mo) * (b - me) for a, b in zip(o_vals, e_vals)) / n
    so = math.sqrt(sum((a - mo) ** 2 for a in o_vals) / n)
    se = math.sqrt(sum((b - me) ** 2 for b in e_vals) / n)
    r = cov / (so * se) if so and se else 0

    print()
    print(f"Exposure correlation (Pearson r): {r:.3f}")
    print(f"Unique players: ours={len(our_pct)}, sim={len(sim_pct)}, combined={len(all_players)}")

# ── 11. Players only in one set ──────────────────────────────────
if n_sim > 0:
    our_only = sorted(
        [(n, our_pct[n]) for n in our_pct if n not in sim_pct and our_pct[n] > 2],
        key=lambda x: -x[1],
    )
    ext_only = sorted(
        [(n, sim_pct[n]) for n in sim_pct if n not in our_pct and sim_pct[n] > 2],
        key=lambda x: -x[1],
    )
    if our_only:
        print()
        print("Players ONLY in our lineups (>2%):")
        for name, pct in our_only[:15]:
            ext_p = ext_by_name.get(name, {})
            print(f"  {ext_p.get('name', name):30s} {pct:.1f}%  FP={ext_p.get('proj', 0):.1f}  ${ext_p.get('salary', 0):,}")
    if ext_only:
        print()
        print("Players ONLY in sim lineups (>2%):")
        for name, pct in ext_only[:15]:
            ext_p = ext_by_name.get(name, {})
            print(f"  {ext_p.get('name', name):30s} {pct:.1f}%  FP={ext_p.get('proj', 0):.1f}  ${ext_p.get('salary', 0):,}")
