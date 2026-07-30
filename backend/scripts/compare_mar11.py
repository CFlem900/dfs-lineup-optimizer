"""Compare DK sim-optimal lineups against our cached pool & projections — Mar 11 2026.

Does NOT call live APIs — uses the cached pool + DK Data Hub projections only.
"""
import sys
import json
import os
import csv
import re
import unicodedata
import math

sys.stdout.reconfigure(encoding="utf-8")

from collections import Counter, defaultdict


def normalize(n):
    n = unicodedata.normalize("NFD", n)
    n = "".join(c for c in n if unicodedata.category(c) != "Mn")
    return n.strip().lower()


# ── Load external sim lineups ────────────────────────────────────
sim_path = os.path.expanduser(
    "~/Downloads/DK_NBA_Main_Pre_Contest_Sims_Lineups (18).csv"
)
sim_lineups = []
with open(sim_path, encoding="utf-8-sig") as f:
    reader = csv.DictReader(f)
    for row in reader:
        lineup = {}
        for slot in ["PG", "SG", "SF", "PF", "C", "G", "F", "UTIL"]:
            raw = row[slot].strip()
            m = re.match(r"^(.+?)\s*\((\d+)\)$", raw)
            if m:
                lineup[slot] = {
                    "name": m.group(1).strip(),
                    "dk_id": int(m.group(2)),
                }
            else:
                lineup[slot] = {"name": raw, "dk_id": 0}
        sim_lineups.append(lineup)

n_sim = len(sim_lineups)
print(f"Loaded {n_sim} external sim-optimal lineups")

# ── Load external projections (DK Data Hub) ──────────────────────
ext_path = os.path.expanduser(
    "~/Downloads/DK_NBA_Main_Data_Hub_Projections (11).csv"
)
ext_by_name = {}
ext_by_norm = {}
with open(ext_path, encoding="utf-8") as f:
    reader = csv.DictReader(f)
    for row in reader:
        name = row["Player"].strip()
        proj_str = row.get("Projection", "").strip()
        sal_str = row.get("Salary", "").strip()
        own_str = row.get("Ownership %", "0").strip() or "0"
        opt_str = row.get("Optimal %", "0").strip() or "0"
        team = row.get("Team", "").strip()
        ceil_str = row.get("Ceiling", "0").strip() or "0"
        floor_str = row.get("Floor", "0").strip() or "0"
        stddev_str = row.get("Std Dev", "0").strip() or "0"
        pos = row.get("Position", "").strip()
        if not proj_str or not name:
            continue
        data = {
            "name": name,
            "proj": float(proj_str),
            "salary": int(sal_str) if sal_str else 0,
            "own": float(own_str),
            "opt": float(opt_str),
            "team": team,
            "ceiling": float(ceil_str),
            "floor": float(floor_str),
            "stddev": float(stddev_str),
            "pos": pos,
        }
        ext_by_name[name] = data
        ext_by_norm[normalize(name)] = data

print(f"External projections: {len(ext_by_norm)} players")

# ── Load our cached pool ─────────────────────────────────────────
pool_path = os.path.join(os.path.dirname(__file__), "..", "cache", "pool_17d86867ffbc.json")
with open(pool_path, encoding="utf-8") as f:
    pool_data = json.load(f)
pool = pool_data.get("entries") or pool_data.get("players", [])
pool_by_norm = {}
pool_by_dk_id = {}
for p in pool:
    pool_by_norm[normalize(p["player_name"])] = p
    dk_id = p.get("dk_player_id")
    if dk_id:
        pool_by_dk_id[dk_id] = p

print(f"Our pool: {len(pool)} players from cache")
teams_in_pool = sorted(set(p.get("team_abbreviation", "?") for p in pool))
print(f"Teams in pool: {', '.join(teams_in_pool)}")


def get_ext(name_or_norm, dk_id=0):
    """Look up external projection data by name or dk_id."""
    norm = normalize(name_or_norm) if name_or_norm else ""
    ext = ext_by_norm.get(norm)
    if ext:
        return ext
    # Try pool dk_id → name → ext lookup
    pp = pool_by_dk_id.get(dk_id) if dk_id else None
    if pp:
        return ext_by_norm.get(normalize(pp["player_name"]))
    return None


def get_pool(name_or_norm, dk_id=0):
    """Look up our pool entry by name or dk_id."""
    norm = normalize(name_or_norm) if name_or_norm else ""
    pp = pool_by_norm.get(norm)
    if pp:
        return pp
    return pool_by_dk_id.get(dk_id) if dk_id else None


# ══════════════════════════════════════════════════════════════════
# SECTION 1: SIM LINEUP ANALYSIS
# ══════════════════════════════════════════════════════════════════
print()
print("=" * 95)
print("EXTERNAL SIM-OPTIMAL LINEUP ANALYSIS (150 lineups)")
print("=" * 95)

sim_exposure = Counter()
sim_lineup_fps = []
sim_lineup_salaries = []
sim_team_per_lineup = []

for lu in sim_lineups:
    lu_fp = 0
    lu_sal = 0
    lu_teams = Counter()
    for slot, info in lu.items():
        norm = normalize(info["name"])
        sim_exposure[norm] += 1
        ext = get_ext(info["name"], info["dk_id"])
        pp = get_pool(info["name"], info["dk_id"])
        if ext:
            lu_fp += ext["proj"]
            lu_sal += ext["salary"]
            lu_teams[ext["team"]] += 1
        elif pp:
            lu_fp += pp.get("projected_fp", 0)
            lu_sal += pp.get("salary", 0)
            lu_teams[pp.get("team_abbreviation", "?")] += 1
    sim_lineup_fps.append(lu_fp)
    sim_lineup_salaries.append(lu_sal)
    sim_team_per_lineup.append(lu_teams)

avg_sim_fp = sum(sim_lineup_fps) / len(sim_lineup_fps)
avg_sim_sal = sum(sim_lineup_salaries) / len(sim_lineup_salaries)
print(f"Avg sim lineup FP (ext proj):  {avg_sim_fp:.1f}")
print(f"Best sim lineup FP:            {max(sim_lineup_fps):.1f}")
print(f"Worst sim lineup FP:           {min(sim_lineup_fps):.1f}")
print(f"Avg sim salary:                ${avg_sim_sal:,.0f}")

# Unique players used
unique_sim = set(sim_exposure.keys())
print(f"Unique players used:           {len(unique_sim)}")

# ── Top sim exposure ─────────────────────────────────────────────
print()
print(f"{'Player':28s} {'Tm':4s} {'Sim%':>5s} {'ExtFP':>6s} {'Sal':>7s} {'Val':>5s} {'Own%':>5s} {'Opt%':>5s}")
print("-" * 75)
for norm_name, cnt in sim_exposure.most_common(35):
    ext = ext_by_norm.get(norm_name, {})
    pp = pool_by_norm.get(norm_name, {})
    pct = cnt / n_sim * 100
    sal = ext.get("salary", pp.get("salary", 0))
    fp = ext.get("proj", pp.get("projected_fp", 0))
    value = fp / (sal / 1000) if sal > 0 else 0
    name = ext.get("name", pp.get("player_name", norm_name))
    team = ext.get("team", pp.get("team_abbreviation", "?"))
    print(
        f"{name:28s} {team:4s} {pct:4.0f}%  "
        f"{fp:5.1f}  ${sal:>5,}  {value:4.1f}x  "
        f"{ext.get('own', 0):4.1f}%  {ext.get('opt', 0):4.1f}%"
    )

# ══════════════════════════════════════════════════════════════════
# SECTION 2: OUR POOL ANALYSIS
# ══════════════════════════════════════════════════════════════════
print()
print("=" * 95)
print("OUR POOL vs EXTERNAL PROJECTIONS (projection alignment)")
print("=" * 95)

# Compare projections for players in both datasets
proj_comparison = []
for p in pool:
    norm = normalize(p["player_name"])
    ext = ext_by_norm.get(norm)
    if not ext:
        continue
    our_fp = p.get("projected_fp", 0)
    ext_fp = ext["proj"]
    if our_fp > 0 and ext_fp > 0:
        diff = our_fp - ext_fp
        pct_err = diff / ext_fp * 100
        proj_comparison.append({
            "name": ext["name"],
            "team": ext["team"],
            "salary": ext["salary"],
            "our_fp": our_fp,
            "ext_fp": ext_fp,
            "diff": diff,
            "pct_err": pct_err,
            "own": ext["own"],
            "sim_pct": sim_exposure.get(norm, 0) / n_sim * 100,
        })

proj_comparison.sort(key=lambda x: abs(x["pct_err"]), reverse=True)
print(
    f"{'Player':25s} {'Tm':3s} {'OurFP':>6s} {'ExtFP':>6s} {'Diff':>7s} "
    f"{'%Err':>6s} {'Sal':>7s} {'Own%':>5s} {'SimE':>5s}"
)
print("-" * 85)
for c in proj_comparison[:25]:
    flag = " !!!" if abs(c["pct_err"]) > 25 else ""
    print(
        f"{c['name']:25s} {c['team']:3s} {c['our_fp']:5.1f}  {c['ext_fp']:5.1f}  "
        f"{c['diff']:+6.1f}  {c['pct_err']:+5.1f}%  ${c['salary']:>5,}  "
        f"{c['own']:4.1f}%  {c['sim_pct']:4.0f}%{flag}"
    )

# Aggregate stats
diffs = [c["pct_err"] for c in proj_comparison]
abs_diffs = [abs(d) for d in diffs]
print()
print(f"Players compared:  {len(proj_comparison)}")
print(f"Mean signed error: {sum(diffs)/len(diffs):+.1f}%")
print(f"Mean abs error:    {sum(abs_diffs)/len(abs_diffs):.1f}%")
print(f"Median abs error:  {sorted(abs_diffs)[len(abs_diffs)//2]:.1f}%")

# ── Missing from our pool ────────────────────────────────────────
print()
print("Players in sim but MISSING from our pool:")
print(f"  {'Player':25s} {'Tm':3s} {'SimE':>5s} {'ExtFP':>6s} {'Sal':>7s}")
print("  " + "-" * 55)
missing_count = 0
for norm, cnt in sim_exposure.most_common():
    if norm not in pool_by_norm:
        ext = ext_by_norm.get(norm, {})
        sim_pct = cnt / n_sim * 100
        if sim_pct >= 1:
            missing_count += 1
            print(
                f"  {ext.get('name', norm):25s} {ext.get('team', '?'):3s} "
                f"{sim_pct:4.0f}%  {ext.get('proj', 0):5.1f}  "
                f"${ext.get('salary', 0):>5,}"
            )
if missing_count == 0:
    print("  (none with >1% exposure)")

# ══════════════════════════════════════════════════════════════════
# SECTION 3: EXPOSURE GAP ANALYSIS
# ══════════════════════════════════════════════════════════════════
print()
print("=" * 95)
print("EXPOSURE ANALYSIS: SIM vs OWNERSHIP vs OUR POOL VALUE")
print("=" * 95)

# Build comparison for all sim-used players
analysis = []
for norm, cnt in sim_exposure.items():
    ext = ext_by_norm.get(norm, {})
    pp = pool_by_norm.get(norm, {})
    sal = ext.get("salary", pp.get("salary", 0))
    ext_fp = ext.get("proj", 0)
    our_fp = pp.get("projected_fp", 0)
    value = ext_fp / (sal / 1000) if sal > 0 else 0
    our_value = our_fp / (sal / 1000) if sal > 0 else 0
    analysis.append({
        "name": ext.get("name", pp.get("player_name", norm)),
        "norm": norm,
        "team": ext.get("team", pp.get("team_abbreviation", "?")),
        "salary": sal,
        "sim_pct": cnt / n_sim * 100,
        "own": ext.get("own", 0),
        "opt": ext.get("opt", 0),
        "ext_fp": ext_fp,
        "our_fp": our_fp,
        "ext_value": value,
        "our_value": our_value,
        "ceiling": ext.get("ceiling", pp.get("ceiling_fp", 0)),
        "in_pool": norm in pool_by_norm,
    })

# ── Sim overweight vs ownership (leverage plays in the sims) ─────
print()
print("SIM LEVERAGE PLAYS (sim exposure much higher than field ownership):")
print(
    f"  {'Player':25s} {'Tm':3s} {'SimE':>5s} {'Own%':>5s} {'Gap':>5s} "
    f"{'ExtFP':>6s} {'Val':>5s} {'Sal':>7s}"
)
print("  " + "-" * 75)
lev_plays = sorted(analysis, key=lambda x: x["sim_pct"] - x["own"], reverse=True)
for c in lev_plays[:15]:
    gap = c["sim_pct"] - c["own"]
    if gap < 2:
        break
    print(
        f"  {c['name']:25s} {c['team']:3s} {c['sim_pct']:4.0f}%  {c['own']:4.1f}%  "
        f"{gap:+4.0f}  {c['ext_fp']:5.1f}  {c['ext_value']:4.1f}x  ${c['salary']:>5,}"
    )

# ── Sim underweight vs ownership (chalk fades) ──────────────────
print()
print("CHALK FADES (high ownership, low sim exposure):")
print(
    f"  {'Player':25s} {'Tm':3s} {'Own%':>5s} {'SimE':>5s} {'Gap':>5s} "
    f"{'ExtFP':>6s} {'Val':>5s} {'Sal':>7s}"
)
print("  " + "-" * 75)
chalk_fades = sorted(
    [c for c in analysis if c["own"] > 5],
    key=lambda x: x["own"] - x["sim_pct"], reverse=True
)
for c in chalk_fades[:10]:
    gap = c["own"] - c["sim_pct"]
    if gap < 2:
        break
    print(
        f"  {c['name']:25s} {c['team']:3s} {c['own']:4.1f}%  {c['sim_pct']:4.0f}%  "
        f"{gap:+4.0f}  {c['ext_fp']:5.1f}  {c['ext_value']:4.1f}x  ${c['salary']:>5,}"
    )

# ══════════════════════════════════════════════════════════════════
# SECTION 4: STACKING ANALYSIS
# ══════════════════════════════════════════════════════════════════
print()
print("=" * 95)
print("TEAM STACKING IN SIM LINEUPS")
print("=" * 95)

sim_2stacks = Counter()
sim_3stacks = Counter()
sim_4stacks = Counter()

for lu_teams in sim_team_per_lineup:
    for team, cnt in lu_teams.items():
        if cnt >= 2:
            sim_2stacks[team] += 1
        if cnt >= 3:
            sim_3stacks[team] += 1
        if cnt >= 4:
            sim_4stacks[team] += 1

print(f"{'Team':6s} {'2+Stack':>8s} {'3+Stack':>8s} {'4+Stack':>8s} {'AvgPlayers':>10s}")
print("-" * 45)
# Team total exposure (total player-slots per team)
team_total_slots = Counter()
for lu_teams in sim_team_per_lineup:
    for team, cnt in lu_teams.items():
        team_total_slots[team] += cnt

for team in sorted(set(sim_2stacks.keys()) | set(sim_3stacks.keys())):
    if team == "?":
        continue
    s2 = sim_2stacks.get(team, 0) / n_sim * 100
    s3 = sim_3stacks.get(team, 0) / n_sim * 100
    s4 = sim_4stacks.get(team, 0) / n_sim * 100
    avg_pl = team_total_slots.get(team, 0) / n_sim
    print(f"{team:6s} {s2:6.0f}%   {s3:6.0f}%   {s4:6.0f}%   {avg_pl:9.2f}")

# ── Most common 2-man stacks ────────────────────────────────────
print()
print("TOP 2-MAN STACKS IN SIM:")
from itertools import combinations
stack_2_counter = Counter()
for lu in sim_lineups:
    team_groups = defaultdict(list)
    for slot, info in lu.items():
        ext = get_ext(info["name"], info["dk_id"])
        pp = get_pool(info["name"], info["dk_id"])
        team = (ext or {}).get("team") or (pp or {}).get("team_abbreviation", "?")
        name = (ext or {}).get("name") or info["name"]
        team_groups[team].append(name)
    for team, players in team_groups.items():
        if len(players) >= 2:
            for combo in combinations(sorted(players), 2):
                stack_2_counter[(team, combo)] += 1

print(f"  {'Team':5s} {'Stack':40s} {'Count':>5s} {'%':>5s}")
print("  " + "-" * 60)
for (team, combo), cnt in stack_2_counter.most_common(20):
    pct = cnt / n_sim * 100
    combo_str = " + ".join(combo)
    print(f"  {team:5s} {combo_str:40s} {cnt:4d}  {pct:4.0f}%")

# ── Most common 3-man stacks ────────────────────────────────────
print()
print("TOP 3-MAN STACKS IN SIM:")
stack_3_counter = Counter()
for lu in sim_lineups:
    team_groups = defaultdict(list)
    for slot, info in lu.items():
        ext = get_ext(info["name"], info["dk_id"])
        pp = get_pool(info["name"], info["dk_id"])
        team = (ext or {}).get("team") or (pp or {}).get("team_abbreviation", "?")
        name = (ext or {}).get("name") or info["name"]
        team_groups[team].append(name)
    for team, players in team_groups.items():
        if len(players) >= 3:
            for combo in combinations(sorted(players), 3):
                stack_3_counter[(team, combo)] += 1

print(f"  {'Team':5s} {'Stack':55s} {'Count':>5s} {'%':>5s}")
print("  " + "-" * 75)
for (team, combo), cnt in stack_3_counter.most_common(15):
    pct = cnt / n_sim * 100
    combo_str = " + ".join(combo)
    print(f"  {team:5s} {combo_str:55s} {cnt:4d}  {pct:4.0f}%")

# ══════════════════════════════════════════════════════════════════
# SECTION 5: VALUE TIER ANALYSIS
# ══════════════════════════════════════════════════════════════════
print()
print("=" * 95)
print("SALARY TIER DISTRIBUTION IN SIM LINEUPS")
print("=" * 95)

tier_sim = Counter()
tier_total_fp = defaultdict(float)
tier_count = defaultdict(int)

for norm, cnt in sim_exposure.items():
    ext = ext_by_norm.get(norm, {})
    sal = ext.get("salary", 0)
    fp = ext.get("proj", 0)
    if sal >= 10000:
        tier = "$10K+"
    elif sal >= 8000:
        tier = "$8K-10K"
    elif sal >= 6000:
        tier = "$6K-8K"
    elif sal >= 4000:
        tier = "$4K-6K"
    else:
        tier = "<$4K"
    tier_sim[tier] += cnt
    tier_total_fp[tier] += fp * cnt
    tier_count[tier] += cnt

total_slots = n_sim * 8
print(f"{'Tier':12s} {'Slots':>6s} {'%':>5s} {'AvgFP':>7s}")
print("-" * 35)
for tier in ["$10K+", "$8K-10K", "$6K-8K", "$4K-6K", "<$4K"]:
    slots = tier_sim.get(tier, 0)
    pct = slots / total_slots * 100
    avg_fp = tier_total_fp[tier] / tier_count[tier] if tier_count[tier] > 0 else 0
    print(f"{tier:12s} {slots:5d}  {pct:4.1f}%  {avg_fp:6.1f}")

# ══════════════════════════════════════════════════════════════════
# SECTION 6: OUR POOL VALUE vs SIM EXPOSURE
# ══════════════════════════════════════════════════════════════════
print()
print("=" * 95)
print("CORRELATION: OUR POOL VALUE vs SIM EXPOSURE")
print("=" * 95)
print("(Players where our value score disagrees with sim usage)")
print()

# For each player with sim exposure and in our pool, compare our value vs sim usage
value_vs_sim = []
for c in analysis:
    if c["in_pool"] and c["sim_pct"] > 0 and c["our_value"] > 0:
        value_vs_sim.append(c)

# Sort by biggest value-exposure mismatch
# High value but low sim exposure → we might overrate
# Low value but high sim exposure → we might underrate
for c in value_vs_sim:
    # Normalize sim exposure to a value-equivalent score
    c["value_rank"] = c["our_value"]
    c["sim_rank"] = c["sim_pct"]

print("Players we VALUE HIGHLY but SIM IGNORES (potential overrates):")
print(
    f"  {'Player':25s} {'Tm':3s} {'OurVal':>6s} {'SimE':>5s} "
    f"{'OurFP':>6s} {'ExtFP':>6s} {'Sal':>7s}"
)
print("  " + "-" * 70)
our_overrates = [c for c in value_vs_sim if c["our_value"] >= 5.0 and c["sim_pct"] < 10]
our_overrates.sort(key=lambda x: x["our_value"], reverse=True)
for c in our_overrates[:10]:
    print(
        f"  {c['name']:25s} {c['team']:3s} {c['our_value']:5.1f}x  {c['sim_pct']:4.0f}%  "
        f"{c['our_fp']:5.1f}  {c['ext_fp']:5.1f}  ${c['salary']:>5,}"
    )

print()
print("Players SIM LOVES but we UNDERVALUE (potential underrates):")
print(
    f"  {'Player':25s} {'Tm':3s} {'SimE':>5s} {'OurVal':>6s} "
    f"{'OurFP':>6s} {'ExtFP':>6s} {'Sal':>7s}"
)
print("  " + "-" * 70)
our_underrates = [c for c in value_vs_sim if c["sim_pct"] >= 30 and c["our_value"] < 5.0]
our_underrates.sort(key=lambda x: x["sim_pct"], reverse=True)
for c in our_underrates[:10]:
    print(
        f"  {c['name']:25s} {c['team']:3s} {c['sim_pct']:4.0f}%  {c['our_value']:5.1f}x  "
        f"{c['our_fp']:5.1f}  {c['ext_fp']:5.1f}  ${c['salary']:>5,}"
    )

# ══════════════════════════════════════════════════════════════════
# SECTION 7: GAME ENVIRONMENT
# ══════════════════════════════════════════════════════════════════
print()
print("=" * 95)
print("GAME-LEVEL EXPOSURE IN SIM LINEUPS")
print("=" * 95)

# Group teams into games based on ext data
team_opponents = {}
for p in pool:
    team = p.get("team_abbreviation", "")
    opp = p.get("opponent_abbreviation", "")
    if team and opp:
        team_opponents[team] = opp

# Build game pairs
games_seen = set()
game_pairs = []
for team, opp in team_opponents.items():
    game_key = tuple(sorted([team, opp]))
    if game_key not in games_seen:
        games_seen.add(game_key)
        game_pairs.append(game_key)

print(f"{'Game':12s} {'TotalSlots':>10s} {'AvgSlots':>9s} {'%ofLineup':>10s}")
print("-" * 45)
for t1, t2 in sorted(game_pairs):
    game_slots = team_total_slots.get(t1, 0) + team_total_slots.get(t2, 0)
    avg_slots = game_slots / n_sim
    pct_lineup = avg_slots / 8 * 100
    print(f"{t1}@{t2:6s} {game_slots:9d}  {avg_slots:8.2f}  {pct_lineup:8.1f}%")

# ══════════════════════════════════════════════════════════════════
# SECTION 8: KEY TAKEAWAYS
# ══════════════════════════════════════════════════════════════════
print()
print("=" * 95)
print("KEY TAKEAWAYS")
print("=" * 95)

# Top 5 chalk plays the sim confirms
print()
print("CONFIRMED CHALK (high own% AND high sim exposure):")
confirmed = sorted(
    [c for c in analysis if c["own"] > 15 and c["sim_pct"] > 30],
    key=lambda x: x["sim_pct"], reverse=True
)
for c in confirmed[:5]:
    print(
        f"  {c['name']:25s} Own={c['own']:.1f}%  Sim={c['sim_pct']:.0f}%  "
        f"FP={c['ext_fp']:.1f}  Val={c['ext_value']:.1f}x"
    )

# Top 5 contrarian plays
print()
print("BEST CONTRARIAN PLAYS (low own% but high sim exposure):")
contrarian = sorted(
    [c for c in analysis if c["own"] < 10 and c["sim_pct"] > 15],
    key=lambda x: x["sim_pct"] - c["own"], reverse=True
)
for c in contrarian[:5]:
    print(
        f"  {c['name']:25s} Own={c['own']:.1f}%  Sim={c['sim_pct']:.0f}%  "
        f"FP={c['ext_fp']:.1f}  Val={c['ext_value']:.1f}x"
    )

# Primary game stack
if game_pairs:
    print()
    print("PRIMARY GAME (highest avg slots per lineup):")
    best_game = max(game_pairs, key=lambda g: team_total_slots.get(g[0], 0) + team_total_slots.get(g[1], 0))
    bg_slots = team_total_slots.get(best_game[0], 0) + team_total_slots.get(best_game[1], 0)
    print(
        f"  {best_game[0]}@{best_game[1]}: {bg_slots/n_sim:.2f} avg slots/lineup "
        f"({bg_slots/(n_sim*8)*100:.0f}% of lineup)"
    )

print()
print("=" * 95)
print("ANALYSIS COMPLETE")
print("=" * 95)
