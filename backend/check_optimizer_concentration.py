"""Investigate optimizer concentration: why SAS over-stacked, MIA under-used."""
import sys, io, json, csv
import numpy as np
from collections import Counter, defaultdict

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

# Load lineups
with open("lineups_mar10.json", encoding="utf-8") as f:
    data = json.load(f)

lineups = data.get("lineups", [])
print(f"Lineups: {len(lineups)}, Pool: {data['pool_size']}")

# Load DK projections for comparison
dk_proj = {}
with open(r"C:\Users\CFlem\Downloads\DK_NBA_Main_Data_Hub_Projections (8).csv",
          encoding="utf-8-sig") as f:
    for row in csv.DictReader(f):
        name = row.get("Player", "").strip()
        if name:
            dk_proj[name] = {
                "fp": float(row.get("Projection", 0) or 0),
                "ownership": float(row.get("Ownership %", "0").replace("%", "") or 0),
                "salary": int(row.get("Salary", 0) or 0),
            }

# 1. GAME STACK ANALYSIS
print("\n" + "=" * 80)
print("1. GAME STACK FREQUENCY (how often each game appears in lineups)")
print("=" * 80)

game_stacks = Counter()  # game pair → count
team_counts = Counter()  # per lineup team frequency

for lu in lineups:
    teams_in_lu = Counter()
    for p in lu.get("players", []):
        team = p.get("team_abbreviation", "?")
        teams_in_lu[team] += 1
    for team, cnt in teams_in_lu.items():
        team_counts[team] += cnt

# Normalize
n = len(lineups)
print(f"\n{'Team':>5} {'Total Slots':>12} {'Avg/Lineup':>11} {'% of 1200':>10}")
print("-" * 45)
for team, cnt in team_counts.most_common():
    print(f"{team:>5} {cnt:>12} {cnt/n:>10.1f} {cnt/(n*8)*100:>9.1f}%")

# 2. PER-PLAYER PROJECTION COMPARISON (our pool vs DK)
print("\n" + "=" * 80)
print("2. OUR PROJECTIONS vs DK (pool players, sorted by FP gap)")
print("=" * 80)

# Get all players from lineups with our projections
our_players = {}
for lu in lineups:
    for p in lu.get("players", []):
        name = p["player_name"]
        if name not in our_players:
            our_players[name] = p

print(f"\n{'Player':<25} {'Team':>4} {'Sal':>6} {'OurFP':>6} {'DKFP':>6} {'Gap':>6} {'OurVal':>7} {'DKVal':>7}")
print("-" * 80)

rows = []
for name, p in our_players.items():
    dk = dk_proj.get(name, {})
    our_fp = p.get("projected_fp", 0)
    dk_fp = dk.get("fp", 0)
    sal = p.get("salary", 0)
    our_val = our_fp / sal * 1000 if sal > 0 else 0
    dk_val = dk_fp / sal * 1000 if sal > 0 and dk_fp > 0 else 0
    gap = our_fp - dk_fp if dk_fp > 0 else 0
    rows.append((name, p.get("team_abbreviation", "?"), sal, our_fp, dk_fp, gap, our_val, dk_val))

# Show biggest positive gaps (where we project higher than DK)
rows.sort(key=lambda x: -x[5])
print("\nWe project HIGHER than DK (optimizer favors these):")
for name, team, sal, our_fp, dk_fp, gap, our_val, dk_val in rows[:15]:
    if dk_fp > 0:
        print(f"{name:<25} {team:>4} ${sal:>5} {our_fp:>5.1f} {dk_fp:>5.1f} {gap:>+5.1f} {our_val:>6.2f} {dk_val:>6.2f}")

print("\nWe project LOWER than DK (optimizer avoids these):")
rows.sort(key=lambda x: x[5])
for name, team, sal, our_fp, dk_fp, gap, our_val, dk_val in rows[:15]:
    if dk_fp > 0:
        print(f"{name:<25} {team:>4} ${sal:>5} {our_fp:>5.1f} {dk_fp:>5.1f} {gap:>+5.1f} {our_val:>6.2f} {dk_val:>6.2f}")

# 3. VALUE (FP/salary) COMPARISON
print("\n" + "=" * 80)
print("3. VALUE ANALYSIS: Our FP/$ vs DK FP/$ by team")
print("=" * 80)

team_value = defaultdict(list)
for name, p in our_players.items():
    team = p.get("team_abbreviation", "?")
    sal = p.get("salary", 0)
    our_fp = p.get("projected_fp", 0)
    if sal > 0:
        team_value[team].append(our_fp / sal * 1000)

print(f"\n{'Team':>5} {'Mean Val':>9} {'Players':>8}")
print("-" * 30)
for team in sorted(team_value.keys(), key=lambda t: -np.mean(team_value[t])):
    vals = team_value[team]
    print(f"{team:>5} {np.mean(vals):>8.2f} {len(vals):>8}")

# 4. STACKING PATTERNS
print("\n" + "=" * 80)
print("4. TEAM STACKING IN LINEUPS")
print("=" * 80)

stack_sizes = Counter()  # (team, count) → frequency
for lu in lineups:
    teams = Counter()
    for p in lu.get("players", []):
        teams[p.get("team_abbreviation", "?")] += 1
    for team, cnt in teams.items():
        if cnt >= 2:
            stack_sizes[(team, cnt)] += 1

print(f"\n{'Team':>5} {'Stack Size':>11} {'Frequency':>10} {'%':>6}")
print("-" * 40)
for (team, size), freq in sorted(stack_sizes.items(), key=lambda x: -x[1]):
    print(f"{team:>5} {size:>11} {freq:>10} {freq/n*100:>5.1f}%")

# 5. GAME TOTAL / VEGAS DATA
print("\n" + "=" * 80)
print("5. GAME TOTALS & VEGAS DATA (from our pool)")
print("=" * 80)

game_totals = {}
for name, p in our_players.items():
    team = p.get("team_abbreviation", "?")
    gt = p.get("game_total")
    spread = p.get("vegas_spread")
    if gt and team not in game_totals:
        game_totals[team] = {"game_total": gt, "spread": spread}

print(f"\n{'Team':>5} {'Game Total':>11} {'Spread':>8}")
print("-" * 30)
for team in sorted(game_totals.keys()):
    gt = game_totals[team]
    spread = gt.get("spread")
    spread_str = f"{spread:>+.1f}" if spread is not None else "N/A"
    print(f"{team:>5} {gt['game_total']:>11} {spread_str:>8}")

# 6. MIA PLAYERS IN POOL — why aren't they selected more?
print("\n" + "=" * 80)
print("6. MIA PLAYERS: Our projection vs DK + exposure")
print("=" * 80)

exposure = Counter()
for lu in lineups:
    for p in lu.get("players", []):
        exposure[p["player_name"]] += 1

# Get all MIA players from our pool (they may not be in lineups)
print(f"\n{'Player':<25} {'Sal':>6} {'OurFP':>6} {'DKFP':>6} {'Gap':>6} {'OurVal':>7} {'Our%':>6} {'DK%':>6}")
print("-" * 85)

mia_dk = {n: d for n, d in dk_proj.items() if d.get("salary", 0) > 0 and n in [
    "Bam Adebayo", "Tyler Herro", "Jaime Jaquez Jr.", "Kel'el Ware",
    "Myron Gardner", "Pelle Larsson", "Dru Smith", "Davion Mitchell",
]}

for name in sorted(mia_dk.keys(), key=lambda n: -mia_dk[n]["fp"]):
    dk = mia_dk[name]
    our = our_players.get(name, {})
    our_fp = our.get("projected_fp", 0)
    dk_fp = dk["fp"]
    sal = dk["salary"]
    gap = our_fp - dk_fp if our_fp > 0 else -dk_fp
    our_val = our_fp / sal * 1000 if sal > 0 and our_fp > 0 else 0
    our_pct = exposure.get(name, 0) / n * 100
    dk_pct = dk.get("ownership", 0)
    print(f"{name:<25} ${sal:>5} {our_fp:>5.1f} {dk_fp:>5.1f} {gap:>+5.1f} {our_val:>6.2f} {our_pct:>5.1f}% {dk_pct:>5.1f}%")

# 7. SAS PLAYERS: why so over-weighted?
print("\n" + "=" * 80)
print("7. SAS PLAYERS: Our projection vs DK + exposure")
print("=" * 80)

sas_players = {n: p for n, p in our_players.items() if p.get("team_abbreviation") == "SAS"}
print(f"\n{'Player':<25} {'Sal':>6} {'OurFP':>6} {'DKFP':>6} {'Gap':>6} {'OurVal':>7} {'Our%':>6} {'DK%':>6}")
print("-" * 85)
for name in sorted(sas_players.keys(), key=lambda n: -sas_players[n].get("projected_fp", 0)):
    p = sas_players[name]
    dk = dk_proj.get(name, {})
    our_fp = p.get("projected_fp", 0)
    dk_fp = dk.get("fp", 0)
    sal = p.get("salary", 0)
    gap = our_fp - dk_fp if dk_fp > 0 else 0
    our_val = our_fp / sal * 1000 if sal > 0 else 0
    our_pct = exposure.get(name, 0) / n * 100
    dk_pct = dk.get("ownership", 0)
    print(f"{name:<25} ${sal:>5} {our_fp:>5.1f} {dk_fp:>5.1f} {gap:>+5.1f} {our_val:>6.2f} {our_pct:>5.1f}% {dk_pct:>5.1f}%")
