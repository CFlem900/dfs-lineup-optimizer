"""Mar 11, 2026 — Projection comparison: External Data Hub vs RotationEngine.

Compares 212 external projections against our cached pool (186 players)
for the DK NBA Main slate (DG 143648, 12 teams, 6 games, 7:30 PM ET lock).

Uses the ENRICHED pool file when available for richer data.
"""

import csv
import json
import os
import sys

# ── Load external projections ─────────────────────────────────────────
EXT_CSV = r"C:\Users\CFlem\Downloads\DK_NBA_Main_Data_Hub_Projections (12).csv"

with open(EXT_CSV, encoding="utf-8") as f:
    reader = csv.DictReader(f)
    ext_rows = list(reader)

print(f"{'='*80}")
print(f"  DK NBA Main Slate — Mar 11, 2026 — Projection Comparison")
print(f"  External: {len(ext_rows)} players | Source: DK Data Hub Projections (12)")
print(f"{'='*80}\n")

# Build ext lookup by normalized name
def normalize(name: str) -> str:
    return name.strip().lower().replace(".", "").replace("'", "").replace("-", " ")

def safe_float(val, default=0.0):
    try:
        return float(val) if val and val.strip() else default
    except (ValueError, TypeError):
        return default

def safe_int(val, default=0):
    try:
        return int(val) if val and val.strip() else default
    except (ValueError, TypeError):
        return default

ext_lookup = {}
for r in ext_rows:
    key = normalize(r["Player"])
    proj = safe_float(r.get("Projection"))
    if proj == 0:
        continue  # skip players with no projection
    ext_lookup[key] = {
        "name": r["Player"],
        "salary": safe_int(r.get("Salary")),
        "position": r.get("Position", ""),
        "team": r.get("Team", ""),
        "opponent": r.get("Opponent", ""),
        "minutes": safe_float(r.get("Minutes")),
        "projection": proj,
        "value": safe_float(r.get("Value")),
        "ownership": safe_float(r.get("Ownership %")),
        "optimal": safe_float(r.get("Optimal %")),
        "leverage": safe_float(r.get("Leverage")),
        "std_dev": safe_float(r.get("Std Dev")),
        "boom": safe_float(r.get("Boom")),
        "bust": safe_float(r.get("Bust")),
        "ceiling": safe_float(r.get("Ceiling")),
        "floor": safe_float(r.get("Floor")),
        "starting": (r.get("Starting", "").strip().lower() == "true"),
    }

# ── Load our pool (prefer enriched) ──────────────────────────────────
CACHE_DIR = os.path.join(os.path.dirname(__file__), "..", "cache")
ENRICHED_FILE = os.path.join(CACHE_DIR, "enriched_17d86867ffbc.json")
RAW_FILE = os.path.join(CACHE_DIR, "pool_17d86867ffbc.json")

pool_file = ENRICHED_FILE if os.path.exists(ENRICHED_FILE) else RAW_FILE
with open(pool_file, encoding="utf-8") as f:
    pool_data = json.load(f)

pool = pool_data["players"]
is_enriched = pool_data.get("enriched", False)
print(f"Our pool: {len(pool)} players | File: {os.path.basename(pool_file)}")
print(f"Enriched: {is_enriched} | Key: {pool_data.get('key', '?')}")
print()

# Build pool lookup
pool_lookup = {}
for p in pool:
    key = normalize(p["player_name"])
    pool_lookup[key] = p

# ── Match players ─────────────────────────────────────────────────────
matched = []
ext_only = []
pool_only = []

for key, ext in ext_lookup.items():
    ours = pool_lookup.get(key)
    if ours:
        matched.append((ext, ours))
    else:
        ext_only.append(ext)

for key, ours in pool_lookup.items():
    if key not in ext_lookup:
        pool_only.append(ours)

print(f"Matched: {len(matched)} | Ext-only: {len(ext_only)} | Pool-only: {len(pool_only)}")
print()

# ══════════════════════════════════════════════════════════════════════
# 1. PROJECTION DELTAS — Top 30 by salary
# ══════════════════════════════════════════════════════════════════════
print(f"{'─'*80}")
print(f"  1. PROJECTION COMPARISON — Top 30 by Salary")
print(f"{'─'*80}")

matched_sorted = sorted(matched, key=lambda x: x[0]["salary"], reverse=True)

print(f"{'Player':<22s} {'Sal':>6s} {'Ext FP':>7s} {'Our FP':>7s} {'Delta':>7s} {'Δ%':>6s} {'ExtMin':>6s} {'OurMin':>6s} {'Strt':>4s}")
print(f"{'─'*22} {'─'*6} {'─'*7} {'─'*7} {'─'*7} {'─'*6} {'─'*6} {'─'*6} {'─'*4}")

total_ext = 0
total_ours = 0
big_misses = []

for ext, ours in matched_sorted[:30]:
    ext_fp = ext["projection"]
    our_fp = ours["projected_fp"]
    delta = our_fp - ext_fp
    delta_pct = (delta / ext_fp * 100) if ext_fp > 0 else 0
    ext_min = ext["minutes"]
    our_min = ours.get("projected_minutes", 0) or 0
    starting = "Y" if ext["starting"] else "N"

    total_ext += ext_fp
    total_ours += our_fp

    flag = ""
    if abs(delta_pct) > 15:
        flag = " ← BIG"
        big_misses.append((ext["name"], ext["salary"], ext_fp, our_fp, delta, delta_pct, ext_min, our_min))

    name = ext["name"][:21]
    print(
        f"{name:<22s} {ext['salary']:>6,d} {ext_fp:>7.1f} {our_fp:>7.1f} "
        f"{delta:>+7.1f} {delta_pct:>+5.1f}% {ext_min:>6.1f} {our_min:>6.1f} {starting:>4s}{flag}"
    )

avg_ext = total_ext / min(len(matched_sorted), 30)
avg_ours = total_ours / min(len(matched_sorted), 30)
print(f"\n  Avg (top 30): Ext={avg_ext:.1f} FP  Ours={avg_ours:.1f} FP  Avg Delta={avg_ours - avg_ext:+.1f} FP ({(avg_ours - avg_ext)/avg_ext*100:+.1f}%)")

# ══════════════════════════════════════════════════════════════════════
# 2. BIGGEST MISSES — |delta| > 15%
# ══════════════════════════════════════════════════════════════════════
all_big_misses = []
for ext, ours in matched_sorted:
    ext_fp = ext["projection"]
    our_fp = ours["projected_fp"]
    delta = our_fp - ext_fp
    delta_pct = (delta / ext_fp * 100) if ext_fp > 0 else 0
    if abs(delta_pct) > 15:
        all_big_misses.append((ext, ours, delta, delta_pct))

print(f"\n{'─'*80}")
print(f"  2. BIGGEST PROJECTION DIVERGENCES (|Δ%| > 15%) — {len(all_big_misses)} players")
print(f"{'─'*80}")

# Separate under vs over
under = [(e, o, d, dp) for e, o, d, dp in all_big_misses if dp < 0]
over = [(e, o, d, dp) for e, o, d, dp in all_big_misses if dp > 0]

if under:
    print(f"\n  WE UNDER-PROJECT ({len(under)} players):")
    print(f"  {'Player':<22s} {'Sal':>6s} {'Ext':>6s} {'Ours':>6s} {'Δ%':>6s} {'ExtMin':>6s} {'OurMin':>6s} {'Strt':>4s}")
    for ext, ours, delta, delta_pct in sorted(under, key=lambda x: x[3]):
        name = ext["name"][:21]
        our_min = ours.get("projected_minutes", 0) or 0
        starting = "Y" if ext["starting"] else "N"
        print(
            f"  {name:<22s} {ext['salary']:>6,d} {ext['projection']:>6.1f} {ours['projected_fp']:>6.1f} "
            f"{delta_pct:>+5.1f}% {ext['minutes']:>6.1f} {our_min:>6.1f} {starting:>4s}"
        )

if over:
    print(f"\n  WE OVER-PROJECT ({len(over)} players):")
    print(f"  {'Player':<22s} {'Sal':>6s} {'Ext':>6s} {'Ours':>6s} {'Δ%':>6s} {'ExtMin':>6s} {'OurMin':>6s} {'Strt':>4s}")
    for ext, ours, delta, delta_pct in sorted(over, key=lambda x: -x[3]):
        name = ext["name"][:21]
        our_min = ours.get("projected_minutes", 0) or 0
        starting = "Y" if ext["starting"] else "N"
        print(
            f"  {name:<22s} {ext['salary']:>6,d} {ext['projection']:>6.1f} {ours['projected_fp']:>6.1f} "
            f"{delta_pct:>+5.1f}% {ext['minutes']:>6.1f} {our_min:>6.1f} {starting:>4s}"
        )

# ══════════════════════════════════════════════════════════════════════
# 3. MINUTES COMPARISON
# ══════════════════════════════════════════════════════════════════════
print(f"\n{'─'*80}")
print(f"  3. MINUTES COMPARISON — Starters Only")
print(f"{'─'*80}")

starters = [(e, o) for e, o in matched_sorted if e["starting"]]
min_deltas = []
for ext, ours in starters:
    our_min = ours.get("projected_minutes", 0) or 0
    ext_min = ext["minutes"]
    if ext_min > 0 and our_min > 0:
        min_deltas.append(our_min - ext_min)

if min_deltas:
    avg_min_delta = sum(min_deltas) / len(min_deltas)
    abs_min_delta = sum(abs(d) for d in min_deltas) / len(min_deltas)
    print(f"  {len(starters)} starters | Avg minute delta: {avg_min_delta:+.1f} min | Avg |delta|: {abs_min_delta:.1f} min")

    # Biggest minute gaps (starters)
    big_min = [(e, o, o.get("projected_minutes", 0) - e["minutes"])
               for e, o in starters
               if abs((o.get("projected_minutes", 0) or 0) - e["minutes"]) > 3.0]
    if big_min:
        print(f"\n  Starters with |minute gap| > 3.0:")
        print(f"  {'Player':<22s} {'Sal':>6s} {'ExtMin':>6s} {'OurMin':>6s} {'MinΔ':>6s} {'ExtFP':>6s} {'OurFP':>6s}")
        for ext, ours, md in sorted(big_min, key=lambda x: x[2]):
            name = ext["name"][:21]
            our_min = ours.get("projected_minutes", 0) or 0
            print(
                f"  {name:<22s} {ext['salary']:>6,d} {ext['minutes']:>6.1f} {our_min:>6.1f} "
                f"{md:>+6.1f} {ext['projection']:>6.1f} {ours['projected_fp']:>6.1f}"
            )

# ══════════════════════════════════════════════════════════════════════
# 4. VALUE ANALYSIS — Best value spots (ext Value > 5.5x)
# ══════════════════════════════════════════════════════════════════════
print(f"\n{'─'*80}")
print(f"  4. HIGH-VALUE PLAYS (Ext Value > 5.5x) — Are we aligned?")
print(f"{'─'*80}")

high_value = [(e, o) for e, o in matched_sorted if e["value"] >= 5.5]
print(f"  {'Player':<22s} {'Sal':>6s} {'ExtVal':>6s} {'OurVal':>6s} {'ExtFP':>6s} {'OurFP':>6s} {'Δ%':>6s} {'Own%':>5s} {'Opt%':>5s}")
for ext, ours in sorted(high_value, key=lambda x: x[0]["value"], reverse=True):
    our_val = ours.get("dk_value") or (ours["projected_fp"] / (ours["salary"] / 1000)) if ours["salary"] > 0 else 0
    ext_fp = ext["projection"]
    our_fp = ours["projected_fp"]
    delta_pct = (our_fp - ext_fp) / ext_fp * 100 if ext_fp > 0 else 0
    name = ext["name"][:21]
    print(
        f"  {name:<22s} {ext['salary']:>6,d} {ext['value']:>6.2f} {our_val:>6.2f} "
        f"{ext_fp:>6.1f} {our_fp:>6.1f} {delta_pct:>+5.1f}% {ext['ownership']:>5.1f} {ext['optimal']:>5.1f}"
    )

# ══════════════════════════════════════════════════════════════════════
# 5. OWNERSHIP vs OPTIMAL — Leverage spots
# ══════════════════════════════════════════════════════════════════════
print(f"\n{'─'*80}")
print(f"  5. TOP LEVERAGE PLAYS (Optimal% - Ownership% > 3)")
print(f"{'─'*80}")

leverage_plays = [(e, o) for e, o in matched_sorted if e["leverage"] > 3.0]
print(f"  {'Player':<22s} {'Sal':>6s} {'Own%':>5s} {'Opt%':>5s} {'Lev':>5s} {'ExtFP':>6s} {'OurFP':>6s} {'Δ%':>6s}")
for ext, ours in sorted(leverage_plays, key=lambda x: x[0]["leverage"], reverse=True)[:15]:
    ext_fp = ext["projection"]
    our_fp = ours["projected_fp"]
    delta_pct = (our_fp - ext_fp) / ext_fp * 100 if ext_fp > 0 else 0
    name = ext["name"][:21]
    print(
        f"  {name:<22s} {ext['salary']:>6,d} {ext['ownership']:>5.1f} {ext['optimal']:>5.1f} "
        f"{ext['leverage']:>+5.1f} {ext_fp:>6.1f} {our_fp:>6.1f} {delta_pct:>+5.1f}%"
    )

# ══════════════════════════════════════════════════════════════════════
# 6. ENRICHMENT STATUS CHECK
# ══════════════════════════════════════════════════════════════════════
print(f"\n{'─'*80}")
print(f"  6. ENRICHMENT STATUS — Our Pool Quality")
print(f"{'─'*80}")

dk_fppg_count = sum(1 for p in pool if p.get("dk_fppg") is not None and p.get("dk_fppg", 0) > 0)
std_dev_count = sum(1 for p in pool if p.get("std_dev_multiplier") is not None)
sim_count = sum(1 for p in pool if p.get("sim_p10") is not None)
game_total_count = sum(1 for p in pool if p.get("game_total") is not None)
ownership_count = sum(1 for p in pool if p.get("estimated_ownership") is not None)
noise_arch_count = sum(1 for p in pool if p.get("noise_archetype") is not None)
props_count = sum(1 for p in pool if p.get("props_pts_line") is not None)

print(f"  DK FPPG:            {dk_fppg_count:>4d}/{len(pool)} ({dk_fppg_count/len(pool)*100:.0f}%)")
print(f"  Noise Archetype:    {noise_arch_count:>4d}/{len(pool)} ({noise_arch_count/len(pool)*100:.0f}%)")
print(f"  std_dev_multiplier: {std_dev_count:>4d}/{len(pool)} ({std_dev_count/len(pool)*100:.0f}%)")
print(f"  Sim Percentiles:    {sim_count:>4d}/{len(pool)} ({sim_count/len(pool)*100:.0f}%)")
print(f"  Game Total:         {game_total_count:>4d}/{len(pool)} ({game_total_count/len(pool)*100:.0f}%)")
print(f"  Ownership:          {ownership_count:>4d}/{len(pool)} ({ownership_count/len(pool)*100:.0f}%)")
print(f"  DK Props:           {props_count:>4d}/{len(pool)} ({props_count/len(pool)*100:.0f}%)")

# ══════════════════════════════════════════════════════════════════════
# 7. SALARY TIER ANALYSIS
# ══════════════════════════════════════════════════════════════════════
print(f"\n{'─'*80}")
print(f"  7. PROJECTION BIAS BY SALARY TIER")
print(f"{'─'*80}")

tiers = [
    ("Premium ($9K+)", 9000, 99999),
    ("Mid ($6K-$8.9K)", 6000, 8999),
    ("Value ($4K-$5.9K)", 4000, 5999),
    ("Punt (<$4K)", 0, 3999),
]

for label, lo, hi in tiers:
    tier_players = [(e, o) for e, o in matched_sorted if lo <= e["salary"] <= hi]
    if not tier_players:
        continue
    deltas = []
    for ext, ours in tier_players:
        if ext["projection"] > 0:
            deltas.append((ours["projected_fp"] - ext["projection"]) / ext["projection"] * 100)
    if deltas:
        avg_d = sum(deltas) / len(deltas)
        median_d = sorted(deltas)[len(deltas) // 2]
        print(f"  {label:<20s}: {len(tier_players):>3d} players | Avg Δ%: {avg_d:>+6.1f}% | Median Δ%: {median_d:>+6.1f}%")

# ══════════════════════════════════════════════════════════════════════
# 8. GAME-LEVEL EXPOSURE COMPARISON
# ══════════════════════════════════════════════════════════════════════
print(f"\n{'─'*80}")
print(f"  8. GAME-LEVEL PROJECTION COMPARISON")
print(f"{'─'*80}")

# Group by game (Team vs Opponent)
games = {}
for ext, ours in matched:
    game_key = tuple(sorted([ext["team"], ext["opponent"]]))
    if game_key not in games:
        games[game_key] = {"ext_total": 0, "our_total": 0, "count": 0, "starters_ext": 0, "starters_ours": 0}
    games[game_key]["ext_total"] += ext["projection"]
    games[game_key]["our_total"] += ours["projected_fp"]
    games[game_key]["count"] += 1

print(f"  {'Game':<15s} {'Players':>7s} {'Ext Total':>10s} {'Our Total':>10s} {'Delta':>8s} {'Δ%':>6s}")
game_pairs = sorted(games.items(), key=lambda x: x[1]["ext_total"], reverse=True)
for (t1, t2), stats in game_pairs:
    delta = stats["our_total"] - stats["ext_total"]
    delta_pct = delta / stats["ext_total"] * 100 if stats["ext_total"] > 0 else 0
    print(
        f"  {t1}@{t2:<10s} {stats['count']:>7d} {stats['ext_total']:>10.1f} {stats['our_total']:>10.1f} "
        f"{delta:>+8.1f} {delta_pct:>+5.1f}%"
    )

# ══════════════════════════════════════════════════════════════════════
# 9. KEY TAKEAWAYS
# ══════════════════════════════════════════════════════════════════════
print(f"\n{'─'*80}")
print(f"  9. KEY TAKEAWAYS")
print(f"{'─'*80}")

# Overall bias
all_deltas = []
for ext, ours in matched:
    if ext["projection"] > 0:
        all_deltas.append((ours["projected_fp"] - ext["projection"]) / ext["projection"] * 100)

if all_deltas:
    avg_all = sum(all_deltas) / len(all_deltas)
    over_count = sum(1 for d in all_deltas if d > 0)
    under_count = sum(1 for d in all_deltas if d < 0)
    big_over = sum(1 for d in all_deltas if d > 15)
    big_under = sum(1 for d in all_deltas if d < -15)
    print(f"  Overall bias: {avg_all:+.1f}% across {len(matched)} matched players")
    print(f"  Over-project: {over_count} | Under-project: {under_count}")
    print(f"  Big divergences (|Δ|>15%): {big_over} over + {big_under} under = {big_over + big_under} total")
    print(f"  Enrichment: {'ENRICHED' if is_enriched else 'RAW (pre-enrichment)'} pool")
    if dk_fppg_count == 0:
        print(f"  ⚠  DK FPPG: MISSING — circuit breaker may have tripped")
    if std_dev_count == 0:
        print(f"  ⚠  Noise profiles: MISSING — AI sim tuning did not run")
    if sim_count == 0:
        print(f"  ⚠  Sim percentiles: MISSING — simulation engine did not run")

print()
