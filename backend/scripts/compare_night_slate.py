"""
Compare our generated lineups vs external sim lineups for the DK Night Slate.

Our lineups:    night_slate_ours.json  (JSON from generate-lineups API)
External sims:  DK_NBA_Night_Pre_Contest_Sims_Lineups (9).csv

Usage:
    cd C:/Working/Prediction_App/backend
    PYTHONPATH=. ./venv/Scripts/python scripts/compare_night_slate.py
"""

import sys
import io
import json
import csv
import re
import unicodedata
import statistics
from collections import Counter, defaultdict
from pathlib import Path

# Force UTF-8 output on Windows
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# ── file paths ──────────────────────────────────────────────────────────
OURS_PATH = Path(r"C:\Working\Prediction_App\backend\night_slate_ours.json")
EXT_PATH = Path(r"C:\Users\CFlem\Downloads\DK_NBA_Night_Pre_Contest_Sims_Lineups (9).csv")

# ── helpers ─────────────────────────────────────────────────────────────

def parse_ext_name(raw: str) -> str:
    """'Josh Giddey (42216305)' -> 'Josh Giddey'"""
    m = re.match(r"^(.+?)\s*\(\d+\)$", raw.strip())
    return m.group(1).strip() if m else raw.strip()


def strip_accents(s: str) -> str:
    """Remove diacritics: e.g. Diabate -> Diabate, Cic -> Cic."""
    nfkd = unicodedata.normalize("NFKD", s)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def normalize(name: str) -> str:
    """Lowercase, strip accents & punctuation for fuzzy matching."""
    name = strip_accents(name)
    return re.sub(r"[^a-z ]", "", name.lower()).strip()


def safe_str(s: str) -> str:
    """Replace non-ASCII chars for safe console printing."""
    return strip_accents(s)


def hr(char="=", width=120):
    print(char * width)


# ── load our lineups ───────────────────────────────────────────────────
with open(OURS_PATH, "r", encoding="utf-8") as f:
    ours_data = json.load(f)

our_lineups = ours_data["lineups"]
our_count = len(our_lineups)

# Build player info dict  { normalized_name -> {name, team, salary, projected_fp} }
our_player_info: dict[str, dict] = {}
our_exposure: Counter = Counter()

for lu in our_lineups:
    for p in lu["players"]:
        key = normalize(p["player_name"])
        our_exposure[key] += 1
        if key not in our_player_info:
            our_player_info[key] = {
                "name": p["player_name"],
                "team": p.get("team_abbreviation", ""),
                "salary": p.get("salary", 0),
                "projected_fp": p.get("projected_fp", 0),
            }

# ── load external lineups ──────────────────────────────────────────────
ext_exposure: Counter = Counter()
ext_lineups_raw: list[list[str]] = []

with open(EXT_PATH, "r", encoding="utf-8-sig") as f:
    reader = csv.reader(f)
    header = next(reader)  # PG, SG, SF, PF, C, G, F, UTIL
    for row in reader:
        names = [parse_ext_name(cell) for cell in row]
        ext_lineups_raw.append(names)
        for n in names:
            ext_exposure[normalize(n)] += 1

ext_count = len(ext_lineups_raw)

# ── build combined player universe ─────────────────────────────────────
all_keys = sorted(set(our_exposure.keys()) | set(ext_exposure.keys()))

rows = []
for key in all_keys:
    info = our_player_info.get(key, {})
    name = info.get("name", key.title())
    team = info.get("team", "")
    salary = info.get("salary", 0)
    proj = info.get("projected_fp", 0)

    our_pct = (our_exposure.get(key, 0) / our_count) * 100
    ext_pct = (ext_exposure.get(key, 0) / ext_count) * 100
    diff = our_pct - ext_pct

    rows.append({
        "name": name,
        "team": team,
        "salary": salary,
        "proj": proj,
        "our_pct": our_pct,
        "ext_pct": ext_pct,
        "diff": diff,
        "abs_diff": abs(diff),
    })

# Sort by absolute difference descending
rows.sort(key=lambda r: r["abs_diff"], reverse=True)

# ── print side-by-side comparison ───────────────────────────────────────
hr()
print(f"{'NIGHT SLATE LINEUP COMPARISON':^120}")
print(f"{'Our lineups vs External Sim lineups':^120}")
hr()
print(f"  Our lineups:  {our_count}   |   External lineups: {ext_count}")
hr("-")

hdr = f"  {'Player':<26} {'Team':>4} {'Salary':>7} {'Proj':>5}  {'Ours %':>7}  {'Ext %':>7}  {'Diff':>7}  {'Visual'}"
print(hdr)
hr("-")

for r in rows:
    bar_len = int(abs(r["diff"]) / 2)
    if r["diff"] > 0:
        bar = "+" * bar_len
    elif r["diff"] < 0:
        bar = "-" * bar_len
    else:
        bar = ""

    sal_str = f"${r['salary']:,}" if r["salary"] else "   ---"
    proj_str = f"{r['proj']:.0f}" if r["proj"] else "  -"
    print(
        f"  {safe_str(r['name']):<26} {r['team']:>4} {sal_str:>7} {proj_str:>5}  "
        f"{r['our_pct']:6.1f}%  {r['ext_pct']:6.1f}%  "
        f"{r['diff']:+6.1f}%  {bar}"
    )

# ── summary stats ──────────────────────────────────────────────────────
hr()
print(f"{'SUMMARY STATISTICS':^120}")
hr("-")

our_unique = len([k for k in all_keys if our_exposure.get(k, 0) > 0])
ext_unique = len([k for k in all_keys if ext_exposure.get(k, 0) > 0])
our_only = len([k for k in all_keys if our_exposure.get(k, 0) > 0 and ext_exposure.get(k, 0) == 0])
ext_only = len([k for k in all_keys if ext_exposure.get(k, 0) > 0 and our_exposure.get(k, 0) == 0])
both = len([k for k in all_keys if our_exposure.get(k, 0) > 0 and ext_exposure.get(k, 0) > 0])

print(f"  Unique players (ours):       {our_unique}")
print(f"  Unique players (external):   {ext_unique}")
print(f"  Players in BOTH:             {both}")
print(f"  Players ONLY in ours:        {our_only}")
print(f"  Players ONLY in external:    {ext_only}")
print()

# Average salary per lineup
our_avg_sal = statistics.mean([lu["total_salary"] for lu in our_lineups])
print(f"  Avg lineup salary (ours):    ${our_avg_sal:,.0f}")

# External avg salary - approximate from our salary data for shared players
ext_salary_total = 0
ext_salary_count = 0
for lu_names in ext_lineups_raw:
    lu_sal = 0
    matched = 0
    for n in lu_names:
        info = our_player_info.get(normalize(n))
        if info:
            lu_sal += info["salary"]
            matched += 1
    if matched == 8:  # only count if we matched all 8 players
        ext_salary_total += lu_sal
        ext_salary_count += 1
if ext_salary_count > 0:
    ext_avg_sal = ext_salary_total / ext_salary_count
    print(f"  Avg lineup salary (ext):     ${ext_avg_sal:,.0f}  (estimated, {ext_salary_count}/{ext_count} lineups fully matched)")
else:
    # Try partial match
    for lu_names in ext_lineups_raw:
        lu_sal = 0
        for n in lu_names:
            info = our_player_info.get(normalize(n))
            if info:
                lu_sal += info["salary"]
        ext_salary_total += lu_sal
    ext_avg_sal = ext_salary_total / ext_count
    print(f"  Avg lineup salary (ext):     ${ext_avg_sal:,.0f}  (partial estimate)")

print()

# Average projected FP
our_avg_fp = statistics.mean([lu["total_projected_fp"] for lu in our_lineups])
our_avg_floor = statistics.mean([lu["total_floor_fp"] for lu in our_lineups])
our_avg_ceil = statistics.mean([lu["total_ceiling_fp"] for lu in our_lineups])
print(f"  Avg projected FP (ours):     {our_avg_fp:.1f}")
print(f"  Avg floor FP (ours):         {our_avg_floor:.1f}")
print(f"  Avg ceiling FP (ours):       {our_avg_ceil:.1f}")

# Quality scores
quality_scores = [lu["quality_score"] for lu in our_lineups if lu.get("quality_score")]
if quality_scores:
    print(f"  Avg quality score (ours):    {statistics.mean(quality_scores):.1f}")

print()

# High-exposure players (>50%)
our_high = sorted(
    [(k, (our_exposure[k] / our_count) * 100) for k in our_exposure if (our_exposure[k] / our_count) * 100 > 50],
    key=lambda x: -x[1],
)
ext_high = sorted(
    [(k, (ext_exposure[k] / ext_count) * 100) for k in ext_exposure if (ext_exposure[k] / ext_count) * 100 > 50],
    key=lambda x: -x[1],
)

print(f"  Players >50% exposure (ours):     {len(our_high)}")
for k, pct in our_high:
    info = our_player_info.get(k, {})
    print(f"    {safe_str(info.get('name', k.title())):<28} {pct:5.1f}%")

print(f"  Players >50% exposure (ext):      {len(ext_high)}")
for k, pct in ext_high:
    info = our_player_info.get(k, {})
    print(f"    {safe_str(info.get('name', k.title())):<28} {pct:5.1f}%")

# ── exposure correlation ───────────────────────────────────────────────
shared_keys = [k for k in all_keys if our_exposure.get(k, 0) > 0 or ext_exposure.get(k, 0) > 0]
our_vals = [(our_exposure.get(key, 0) / our_count) * 100 for key in shared_keys]
ext_vals = [(ext_exposure.get(key, 0) / ext_count) * 100 for key in shared_keys]

if len(our_vals) >= 2:
    mean_o = statistics.mean(our_vals)
    mean_e = statistics.mean(ext_vals)
    num = sum((o - mean_o) * (e - mean_e) for o, e in zip(our_vals, ext_vals))
    den_o = sum((o - mean_o) ** 2 for o in our_vals) ** 0.5
    den_e = sum((e - mean_e) ** 2 for e in ext_vals) ** 0.5
    corr = num / (den_o * den_e) if den_o * den_e > 0 else 0
    print(f"\n  Exposure correlation (Pearson r): {corr:.3f}")
    if corr > 0.7:
        print("    -> Strong positive correlation - builds are directionally aligned")
    elif corr > 0.4:
        print("    -> Moderate correlation - some agreement on key players")
    elif corr > 0:
        print("    -> Weak correlation - substantially different player preferences")
    else:
        print("    -> Negative/no correlation - very different build philosophies")

# ── biggest disagreements ──────────────────────────────────────────────
hr()
print(f"{'BIGGEST DISAGREEMENTS':^120}")
hr("-")

print("\n  ** Players WE ARE MUCH HIGHER ON (ours >> ext) **\n")
print(f"  {'Player':<28} {'Team':>4} {'Salary':>7} {'Proj':>5}  {'Ours %':>7}  {'Ext %':>7}  {'Diff':>7}")
print("  " + "-" * 82)
higher_on = [r for r in rows if r["diff"] > 0][:20]
for r in higher_on:
    sal_str = f"${r['salary']:,}" if r["salary"] else "   ---"
    proj_str = f"{r['proj']:.0f}" if r["proj"] else "  -"
    print(
        f"  {safe_str(r['name']):<28} {r['team']:>4} {sal_str:>7} {proj_str:>5}  "
        f"{r['our_pct']:6.1f}%  {r['ext_pct']:6.1f}%  {r['diff']:+6.1f}%"
    )

print(f"\n  ** Players WE ARE MUCH LOWER ON (ext >> ours) **\n")
print(f"  {'Player':<28} {'Team':>4} {'Salary':>7} {'Proj':>5}  {'Ours %':>7}  {'Ext %':>7}  {'Diff':>7}")
print("  " + "-" * 82)
lower_on = [r for r in rows if r["diff"] < 0][:20]
for r in lower_on:
    sal_str = f"${r['salary']:,}" if r["salary"] else "   ---"
    proj_str = f"{r['proj']:.0f}" if r["proj"] else "  -"
    print(
        f"  {safe_str(r['name']):<28} {r['team']:>4} {sal_str:>7} {proj_str:>5}  "
        f"{r['our_pct']:6.1f}%  {r['ext_pct']:6.1f}%  {r['diff']:+6.1f}%"
    )

# ── players ONLY in one set ───────────────────────────────────────────
print()
hr("-")
print("\n  ** Players ONLY in OUR lineups (not in external) **\n")
ours_only_rows = [r for r in rows if r["our_pct"] > 0 and r["ext_pct"] == 0]
ours_only_rows.sort(key=lambda r: -r["our_pct"])
print(f"  {'Player':<28} {'Team':>4} {'Salary':>7} {'Proj':>5}  {'Ours %':>7}")
print("  " + "-" * 60)
for r in ours_only_rows:
    sal_str = f"${r['salary']:,}" if r["salary"] else "   ---"
    proj_str = f"{r['proj']:.0f}" if r["proj"] else "  -"
    print(
        f"  {safe_str(r['name']):<28} {r['team']:>4} {sal_str:>7} {proj_str:>5}  "
        f"{r['our_pct']:6.1f}%"
    )

print(f"\n  ** Players ONLY in EXTERNAL lineups (not in ours) **\n")
ext_only_rows = [r for r in rows if r["ext_pct"] > 0 and r["our_pct"] == 0]
ext_only_rows.sort(key=lambda r: -r["ext_pct"])
print(f"  {'Player':<28} {'Team':>4} {'Salary':>7}  {'Ext %':>7}")
print("  " + "-" * 55)
for r in ext_only_rows:
    sal_str = f"${r['salary']:,}" if r["salary"] else "   ---"
    print(
        f"  {safe_str(r['name']):<28} {r['team']:>4} {sal_str:>7}  "
        f"{r['ext_pct']:6.1f}%"
    )

# ── quality distribution ───────────────────────────────────────────────
hr()
print(f"{'OUR LINEUP QUALITY DISTRIBUTION':^120}")
hr("-")

if quality_scores:
    grades = Counter(lu.get("quality_grade", "?") for lu in our_lineups)
    for grade in sorted(grades.keys()):
        count = grades[grade]
        pct = (count / our_count) * 100
        bar = "#" * int(pct / 2)
        print(f"  Grade {grade:>2}: {count:>4} lineups ({pct:5.1f}%)  {bar}")

    print()
    print(f"  Min quality:    {min(quality_scores):.1f}")
    print(f"  Max quality:    {max(quality_scores):.1f}")
    print(f"  Mean quality:   {statistics.mean(quality_scores):.1f}")
    print(f"  Median quality: {statistics.median(quality_scores):.1f}")
    print(f"  Stdev quality:  {statistics.stdev(quality_scores):.1f}")

    # Histogram buckets
    buckets = defaultdict(int)
    for s in quality_scores:
        bucket = int(s // 5) * 5
        buckets[bucket] += 1
    print("\n  Quality histogram (5-pt buckets):")
    for b in sorted(buckets.keys()):
        count = buckets[b]
        bar = "#" * count
        print(f"    {b:>3}-{b + 4:<3}: {count:>3}  {bar}")

# ── ILP & generation stats ────────────────────────────────────────────
print()
hr("-")
ilp_count = sum(1 for lu in our_lineups if lu.get("ilp_used"))
print(f"  ILP solver used: {ilp_count}/{our_count} lineups ({ilp_count/our_count*100:.0f}%)")

print(f"\n  Generation time:       {ours_data.get('generation_time_ms', 0):,} ms")
print(f"  Candidates generated:  {ours_data.get('num_candidates_generated', '?')}")
print(f"  ILP accepted:          {ours_data.get('ilp_accepted_count', '?')}")
print(f"  ILP failed:            {ours_data.get('ilp_failed_count', '?')}")
print(f"  Greedy fallback:       {ours_data.get('greedy_fallback_count', '?')}")
print(f"  Pool size:             {ours_data.get('pool_size', '?')}")
print(f"  Baseline proj score:   {ours_data.get('baseline_projection_score', '?')}")

# Salary usage distribution
sal_remaining = [lu["salary_remaining"] for lu in our_lineups]
print(f"\n  Salary remaining distribution:")
print(f"    Min:    ${min(sal_remaining):,}")
print(f"    Max:    ${max(sal_remaining):,}")
print(f"    Mean:   ${statistics.mean(sal_remaining):,.0f}")
print(f"    Median: ${statistics.median(sal_remaining):,.0f}")

hr()
print("Done.")
