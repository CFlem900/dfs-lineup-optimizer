"""Compare our generated lineups against external sim lineups."""
import sys
import csv
import re
import time
import os
import unicodedata
import math

sys.stdout.reconfigure(encoding="utf-8")

import logging
logging.basicConfig(level=logging.WARNING, stream=sys.stderr)

from app.api.dependencies import get_services
from app.models.lineup import MultiLineupRequest

svc = get_services()
los = svc.lineup_optimizer_service

# ── Config ───────────────────────────────────────────────────────────
DRAFT_GROUP_ID = 143408
GAME_DATE = "2026-03-09"
EXT_CSV = os.path.expanduser(
    "~/Downloads/DK_NBA_Main_Pre_Contest_Sims_Lineups (15).csv"
)
NUM_LINEUPS = 150


def normalize(n):
    n = unicodedata.normalize("NFD", n)
    n = "".join(c for c in n if unicodedata.category(c) != "Mn")
    return n.strip().lower()


def parse_player(cell):
    m = re.match(r"^(.+?)\s*\((\d+)\)$", cell.strip())
    return normalize(m.group(1)) if m else normalize(cell.strip())


# ── 1. Generate our lineups ─────────────────────────────────────────
print(f"Generating {NUM_LINEUPS} lineups for DG {DRAFT_GROUP_ID}...")
t0 = time.time()
req = MultiLineupRequest(
    platform="dk",
    draft_group_id=DRAFT_GROUP_ID,
    sport="nba",
    num_lineups=NUM_LINEUPS,
    game_date=GAME_DATE,
    strategy="max_projection",
    contest_type="gpp",
)
result = los.generate_lineups(req)
elapsed = time.time() - t0
lineups = result.lineups
print(f"Generated {len(lineups)} lineups in {elapsed:.1f}s")

# ── 2. Parse external lineups ───────────────────────────────────────
ext_lineups = []
with open(EXT_CSV, encoding="utf-8-sig") as f:
    reader = csv.reader(f)
    next(reader)  # skip header
    for row in reader:
        if len(row) >= 8:
            ext_lineups.append([parse_player(c) for c in row[:8]])
print(f"External lineups: {len(ext_lineups)}")

# ── 3. Build exposure maps ──────────────────────────────────────────
our_exposure = {}
our_team_exposure = {}
for lu in lineups:
    for p in lu.players:
        name = normalize(p.player_name)
        team = p.team_abbreviation
        our_exposure[name] = our_exposure.get(name, 0) + 1
        our_team_exposure[team] = our_team_exposure.get(team, 0) + 1

ext_exposure = {}
for lu in ext_lineups:
    for name in lu:
        ext_exposure[name] = ext_exposure.get(name, 0) + 1

our_n = len(lineups)
ext_n = len(ext_lineups)
our_pct = {k: v / our_n * 100 for k, v in our_exposure.items()}
ext_pct = {k: v / ext_n * 100 for k, v in ext_exposure.items()}

# ── 4. Merge and compare ────────────────────────────────────────────
all_players = set(our_pct.keys()) | set(ext_pct.keys())
comparison = []
for name in all_players:
    o = our_pct.get(name, 0)
    e = ext_pct.get(name, 0)
    comparison.append({"name": name, "our_pct": o, "ext_pct": e, "diff": o - e})

comparison.sort(key=lambda x: x["ext_pct"], reverse=True)

print()
print("=" * 85)
print(f"{'Player':30s} {'Ext %':>7s} {'Our %':>7s} {'Diff':>7s}  Bar")
print("=" * 85)
for c in comparison:
    if c["ext_pct"] > 0 or c["our_pct"] > 5:
        bar_o = "+" * int(c["our_pct"] / 2)
        bar_e = "-" * int(c["ext_pct"] / 2)
        print(
            f"{c['name']:30s} {c['ext_pct']:>6.1f}% "
            f"{c['our_pct']:>6.1f}% {c['diff']:>+6.1f}%  "
            f"O:{bar_o} E:{bar_e}"
        )

# ── 5. Correlation ──────────────────────────────────────────────────
both = list(all_players)
o_vals = [our_pct.get(n, 0) for n in both]
e_vals = [ext_pct.get(n, 0) for n in both]
n = len(o_vals)
mo = sum(o_vals) / n
me = sum(e_vals) / n
cov = sum((a - mo) * (b - me) for a, b in zip(o_vals, e_vals)) / n
so = math.sqrt(sum((a - mo) ** 2 for a in o_vals) / n)
se = math.sqrt(sum((b - me) ** 2 for b in e_vals) / n)
r = cov / (so * se) if so and se else 0
print()
print(f"Exposure correlation: r = {r:.3f}")
print(f"Total unique players: ours={len(our_pct)}, ext={len(ext_pct)}, combined={len(all_players)}")

# ── 6. Team exposure ────────────────────────────────────────────────
pool = los.build_player_pool(
    platform="dk", draft_group_id=DRAFT_GROUP_ID,
    game_date=GAME_DATE, sport="nba",
)
name_to_team = {normalize(p.player_name): p.team_abbreviation for p in pool}

ext_team_exp = {}
for lu in ext_lineups:
    for name in lu:
        team = name_to_team.get(name, "?")
        ext_team_exp[team] = ext_team_exp.get(team, 0) + 1

all_teams = sorted(set(our_team_exposure.keys()) | set(ext_team_exp.keys()))
print()
print("=" * 60)
print(f"{'Team':6s} {'Our slots':>10s} {'Ext slots':>10s} {'Our %':>7s} {'Ext %':>7s}")
print("=" * 60)
for team in all_teams:
    if team == "?":
        continue
    o_s = our_team_exposure.get(team, 0)
    e_s = ext_team_exp.get(team, 0)
    print(
        f"{team:6s} {o_s:>10d} {e_s:>10d} "
        f"{o_s / (our_n * 8) * 100:>6.1f}% "
        f"{e_s / (ext_n * 8) * 100:>6.1f}%"
    )

# ── 7. Players only in one set ──────────────────────────────────────
our_only = sorted(
    [(n, our_pct[n]) for n in our_pct if n not in ext_pct and our_pct[n] > 2],
    key=lambda x: -x[1],
)
ext_only = sorted(
    [(n, ext_pct[n]) for n in ext_pct if n not in our_pct and ext_pct[n] > 2],
    key=lambda x: -x[1],
)
if our_only:
    print()
    print("Players ONLY in our lineups (>2%):")
    for name, pct in our_only[:15]:
        print(f"  {name:30s} {pct:.1f}%")
if ext_only:
    print()
    print("Players ONLY in external lineups (>2%):")
    for name, pct in ext_only[:15]:
        print(f"  {name:30s} {pct:.1f}%")

# ── 8. Biggest exposure disagreements ───────────────────────────────
comparison.sort(key=lambda x: abs(x["diff"]), reverse=True)
print()
print("TOP 15 EXPOSURE DISAGREEMENTS:")
print(f"{'Player':30s} {'Ext %':>7s} {'Our %':>7s} {'Diff':>7s}")
print("-" * 55)
for c in comparison[:15]:
    print(f"{c['name']:30s} {c['ext_pct']:>6.1f}% {c['our_pct']:>6.1f}% {c['diff']:>+6.1f}%")
