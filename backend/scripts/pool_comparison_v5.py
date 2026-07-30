"""Pool-level projection comparison (v5 — after optimizer tuning).

Rebuilds the player pool fresh (clears all caches), then compares
our projections against the external Data Hub CSV.
"""
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
from app.services.lineup_optimizer_service import (
    _pool_cache, _enriched_cache, _strategy_cache,
)

svc = get_services()
los = svc.lineup_optimizer_service

# ── Config ────────────────────────────────────────────────────────
DRAFT_GROUP_ID = 143408
GAME_DATE = "2026-03-09"
EXT_CSV = os.path.expanduser(
    "~/Downloads/DK_NBA_Main_Data_Hub_Projections (7).csv"
)

# ── 1. Clear ALL caches ──────────────────────────────────────────
_pool_cache.clear()
_enriched_cache.clear()
_strategy_cache.clear()

import glob as glob_mod
for f in glob_mod.glob("cache/pool_*.json"):
    os.remove(f)
    print(f"Deleted: {f}")

print("All caches cleared.\n")

# ── 2. Rebuild pool fresh ────────────────────────────────────────
t0 = time.time()
pool = los.build_player_pool(
    platform="dk",
    draft_group_id=DRAFT_GROUP_ID,
    game_date=GAME_DATE,
    sport="nba",
)
elapsed = time.time() - t0
print(f"Pool rebuilt: {len(pool)} players in {elapsed:.1f}s\n")

# Count by projection source
from collections import Counter, defaultdict
sources = Counter()
for p in pool:
    src = getattr(p, "projection_source", None) or "rotation"
    sources[src] += 1
print("Projection sources:")
for src, cnt in sources.most_common():
    print(f"  {src:35s}: {cnt}")
print()


def normalize(n):
    n = unicodedata.normalize("NFD", n)
    n = "".join(c for c in n if unicodedata.category(c) != "Mn")
    return n.strip().lower()


# ── 3. Load external projections ─────────────────────────────────
ext_projs = {}
with open(EXT_CSV, encoding="utf-8-sig") as f:
    reader = csv.DictReader(f)
    for row in reader:
        name = row.get("Player", row.get("Name", "")).strip()
        fp_raw = row.get("Projection", row.get("FPTS", row.get("projected_fp", ""))).strip()
        sal_raw = row.get("Salary", "").strip().replace("$", "").replace(",", "")
        mins_raw = row.get("Minutes", "").strip()
        own_raw = row.get("Ownership %", "").strip()
        if not name or not fp_raw:
            continue
        try:
            fp = float(fp_raw)
        except ValueError:
            continue
        salary = int(float(sal_raw)) if sal_raw else 0
        mins = float(mins_raw) if mins_raw else 0
        own = float(own_raw) if own_raw else 0
        ext_projs[normalize(name)] = {
            "name": name,
            "fp": fp,
            "salary": salary,
            "minutes": mins,
            "ownership": own,
        }

print(f"External projections loaded: {len(ext_projs)} players\n")

# ── 4. Match and compare ─────────────────────────────────────────
matched = []
our_only = []
ext_only = dict(ext_projs)

for p in pool:
    norm = normalize(p.player_name)
    ext = ext_only.pop(norm, None)
    if not ext:
        dn = normalize(getattr(p, "display_name", "") or "")
        ext = ext_only.pop(dn, None)
    if ext:
        src = getattr(p, "projection_source", None) or "rotation"
        conf = getattr(p, "rotation_confidence", 1.0)
        matched.append({
            "name": p.player_name,
            "team": p.team_abbreviation,
            "salary": p.salary,
            "our_fp": round(p.projected_fp, 1),
            "ext_fp": round(ext["fp"], 1),
            "our_mins": round(getattr(p, "projected_minutes", 0), 1),
            "ext_mins": round(ext["minutes"], 1),
            "ext_own": round(ext["ownership"], 1),
            "diff": round(p.projected_fp - ext["fp"], 1),
            "source": src,
            "confidence": conf,
        })
    else:
        our_only.append(p)

# ── 5. Statistics ─────────────────────────────────────────────────
diffs = [m["diff"] for m in matched]
abs_diffs = [abs(d) for d in diffs]
n = len(diffs)

mean_diff = sum(diffs) / n if n else 0
mae = sum(abs_diffs) / n if n else 0

# Pearson correlation
our_vals = [m["our_fp"] for m in matched]
ext_vals = [m["ext_fp"] for m in matched]
mean_o = sum(our_vals) / n
mean_e = sum(ext_vals) / n
cov = sum((o - mean_o) * (e - mean_e) for o, e in zip(our_vals, ext_vals)) / n
std_o = math.sqrt(sum((o - mean_o) ** 2 for o in our_vals) / n)
std_e = math.sqrt(sum((e - mean_e) ** 2 for e in ext_vals) / n)
r = cov / (std_o * std_e) if std_o and std_e else 0

# Source breakdown
source_counts = Counter(m["source"] for m in matched)
rotation_count = source_counts.get("rotation", 0)
fallback_count = sum(v for k, v in source_counts.items() if k != "rotation")

print("=" * 80)
print(f"PROJECTION COMPARISON — DG {DRAFT_GROUP_ID} ({GAME_DATE})")
print("=" * 80)
print(f"  Matched players:    {n}")
print(f"  Our only:           {len(our_only)}")
print(f"  Ext only:           {len(ext_only)}")
print(f"  Rotation-based:     {rotation_count}")
print(f"  Fallback sources:   {fallback_count}")
print()
print(f"  Correlation (r):    {r:.3f}")
print(f"  Mean bias:          {mean_diff:+.2f} FP")
print(f"  MAE:                {mae:.2f} FP")
print()

# ── 6. Per-team bias ──────────────────────────────────────────────
team_diffs = defaultdict(list)
for m in matched:
    team_diffs[m["team"]].append(m["diff"])

print("=" * 80)
print("PER-TEAM BIAS (avg our - ext)")
print("=" * 80)
team_rows = []
for team, diffs_t in sorted(team_diffs.items()):
    avg = sum(diffs_t) / len(diffs_t)
    team_rows.append((team, avg, len(diffs_t)))
team_rows.sort(key=lambda x: x[1], reverse=True)
for team, avg, cnt in team_rows:
    bar = "+" * int(max(0, avg) * 2) + "-" * int(max(0, -avg) * 2)
    print(f"  {team:4s} ({cnt:2d} players): {avg:+5.1f} FP  {bar}")
print()

# ── 7. Biggest divergences ───────────────────────────────────────
sorted_by_diff = sorted(matched, key=lambda m: m["diff"], reverse=True)

print("=" * 80)
print("TOP 15 OVER-PROJECTIONS (ours > external)")
print("=" * 80)
header = f"{'Player':25s} {'Team':4s} {'Salary':>7s} {'Our FP':>7s} {'Ext FP':>7s} {'Diff':>6s} {'Conf':>5s} {'Source':20s}"
print(header)
print("-" * len(header))
for m in sorted_by_diff[:15]:
    print(
        f"{m['name']:25s} {m['team']:4s} ${m['salary']:>6,} "
        f"{m['our_fp']:>6.1f}  {m['ext_fp']:>6.1f}  {m['diff']:>+5.1f}  "
        f"{m['confidence']:>4.1f}  {m['source']}"
    )
print()

print("=" * 80)
print("TOP 15 UNDER-PROJECTIONS (ours < external)")
print("=" * 80)
print(header)
print("-" * len(header))
for m in sorted_by_diff[-15:]:
    print(
        f"{m['name']:25s} {m['team']:4s} ${m['salary']:>6,} "
        f"{m['our_fp']:>6.1f}  {m['ext_fp']:>6.1f}  {m['diff']:>+5.1f}  "
        f"{m['confidence']:>4.1f}  {m['source']}"
    )
print()

# ── 8. Fallback-source players ────────────────────────────────────
fallback_players = [m for m in matched if m["source"] != "rotation"]
if fallback_players:
    print("=" * 80)
    print(f"FALLBACK-SOURCE PLAYERS ({len(fallback_players)})")
    print("=" * 80)
    for m in sorted(fallback_players, key=lambda x: x["diff"], reverse=True):
        print(
            f"  {m['name']:25s} {m['team']:4s} ${m['salary']:>6,} "
            f"our={m['our_fp']:.1f}  ext={m['ext_fp']:.1f}  "
            f"diff={m['diff']:+.1f}  conf={m['confidence']:.1f}  src={m['source']}"
        )
    print()

# ── 9. Accuracy by projection tier ───────────────────────────────
print("=" * 80)
print("ACCURACY BY PROJECTION TIER")
print("=" * 80)
tiers = [
    ("Studs (35+ ext FP)", lambda m: m["ext_fp"] >= 35),
    ("Mid-tier (20-35 ext FP)", lambda m: 20 <= m["ext_fp"] < 35),
    ("Value (10-20 ext FP)", lambda m: 10 <= m["ext_fp"] < 20),
    ("Deep bench (<10 ext FP)", lambda m: m["ext_fp"] < 10),
]
for label, filt in tiers:
    tier_players = [m for m in matched if filt(m)]
    if not tier_players:
        print(f"  {label:30s}: no players")
        continue
    tier_diffs = [m["diff"] for m in tier_players]
    tier_mae = sum(abs(d) for d in tier_diffs) / len(tier_diffs)
    tier_bias = sum(tier_diffs) / len(tier_diffs)
    print(f"  {label:30s}: n={len(tier_players):3d}  bias={tier_bias:+5.1f}  MAE={tier_mae:.1f}")
print()

# ── 9b. Adjusted statistics (excluding players external marks as OUT) ──
# Players with ext_fp < 2.0 are effectively OUT according to external
# sources.  Our model can't always detect these (injury data gaps).
# Separating them shows our accuracy for "both agree will play" players.
adj_matched = [m for m in matched if m["ext_fp"] >= 2.0]
out_matched = [m for m in matched if m["ext_fp"] < 2.0]
adj_diffs = [m["diff"] for m in adj_matched]
adj_abs = [abs(d) for d in adj_diffs]
adj_n = len(adj_diffs)
adj_mean = sum(adj_diffs) / adj_n if adj_n else 0
adj_mae = sum(adj_abs) / adj_n if adj_n else 0
adj_o = [m["our_fp"] for m in adj_matched]
adj_e = [m["ext_fp"] for m in adj_matched]
adj_mo = sum(adj_o) / adj_n
adj_me = sum(adj_e) / adj_n
adj_cov = sum((o - adj_mo) * (e - adj_me) for o, e in zip(adj_o, adj_e)) / adj_n
adj_so = math.sqrt(sum((o - adj_mo) ** 2 for o in adj_o) / adj_n)
adj_se = math.sqrt(sum((e - adj_me) ** 2 for e in adj_e) / adj_n)
adj_r = adj_cov / (adj_so * adj_se) if adj_so and adj_se else 0

print("=" * 80)
print("ADJUSTED STATS (excluding ext < 2 FP = external says OUT)")
print("=" * 80)
print(f"  Excluded (ext OUT):     {len(out_matched)} players")
if out_matched:
    out_names = [f"{m['name']} ({m['team']})" for m in sorted(out_matched, key=lambda x: x['diff'], reverse=True)]
    print(f"  Excluded players:       {', '.join(out_names)}")
print(f"  Adjusted matched:      {adj_n}")
print(f"  Correlation (r):        {adj_r:.3f}")
print(f"  Mean bias:              {adj_mean:+.2f} FP")
print(f"  MAE:                    {adj_mae:.2f} FP")
print()
# Adjusted tier breakdown
for label, filt in tiers:
    tp = [m for m in adj_matched if filt(m)]
    if not tp:
        continue
    td = [m["diff"] for m in tp]
    print(f"  {label:30s}: n={len(tp):3d}  bias={sum(td)/len(td):+5.1f}  MAE={sum(abs(d) for d in td)/len(td):.1f}")
print()

# ── 10. Top 20 external plays — our coverage ─────────────────────
print("=" * 80)
print("TOP 20 EXTERNAL PLAYS — OUR COVERAGE")
print("=" * 80)
# Sort matched by ext_fp descending
top_ext = sorted(matched, key=lambda m: m["ext_fp"], reverse=True)[:20]
header2 = f"{'#':>3s} {'Player':25s} {'Team':4s} {'Salary':>7s} {'Ext FP':>7s} {'Our FP':>7s} {'Diff':>6s} {'ExtOwn':>7s}"
print(header2)
print("-" * len(header2))
for i, m in enumerate(top_ext, 1):
    print(
        f"{i:>3d} {m['name']:25s} {m['team']:4s} ${m['salary']:>6,} "
        f"{m['ext_fp']:>6.1f}  {m['our_fp']:>6.1f}  {m['diff']:>+5.1f}  "
        f"{m['ext_own']:>5.1f}%"
    )
print()

# ── 11. Value plays — best FP/$ by external projections ──────────
print("=" * 80)
print("TOP 15 VALUE PLAYS (ext FP/$K) — OUR COMPARISON")
print("=" * 80)
value_plays = [m for m in matched if m["salary"] > 0 and m["ext_fp"] > 5]
value_plays.sort(key=lambda m: m["ext_fp"] / (m["salary"] / 1000), reverse=True)
header3 = f"{'Player':25s} {'Team':4s} {'Salary':>7s} {'Ext FP':>7s} {'Our FP':>7s} {'Ext Val':>8s} {'Our Val':>8s} {'Diff':>6s}"
print(header3)
print("-" * len(header3))
for m in value_plays[:15]:
    ext_val = m["ext_fp"] / (m["salary"] / 1000)
    our_val = m["our_fp"] / (m["salary"] / 1000) if m["salary"] else 0
    print(
        f"{m['name']:25s} {m['team']:4s} ${m['salary']:>6,} "
        f"{m['ext_fp']:>6.1f}  {m['our_fp']:>6.1f}  "
        f"{ext_val:>7.2f}x  {our_val:>7.2f}x  {m['diff']:>+5.1f}"
    )
print()

# ── 12. Minutes breakdown for big misses (|diff| > 8, ext > 2) ──
print("=" * 80)
print("MINUTES BREAKDOWN — BIG MISSES (|diff| > 8, ext > 2 FP)")
print("=" * 80)
big_misses = [m for m in matched if abs(m["diff"]) > 8 and m["ext_fp"] >= 2.0]
big_misses.sort(key=lambda m: m["diff"])
header4 = f"{'Player':25s} {'Team':4s} {'Sal':>6s} {'OurMin':>7s} {'ExtMin':>7s} {'OurFP':>6s} {'ExtFP':>6s} {'Diff':>6s} {'FP/Min':>7s}"
print(header4)
print("-" * len(header4))
for m in big_misses:
    fp_per_min = m["our_fp"] / m["our_mins"] if m["our_mins"] > 0 else 0
    print(
        f"{m['name']:25s} {m['team']:4s} ${m['salary']:>5,} "
        f"{m['our_mins']:>6.1f}  {m['ext_mins']:>6.1f}  "
        f"{m['our_fp']:>5.1f}  {m['ext_fp']:>5.1f}  {m['diff']:>+5.1f}  "
        f"{fp_per_min:>6.2f}"
    )
print()
