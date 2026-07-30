import json, csv
from collections import Counter

data = json.load(open("v8_test.json", encoding="utf-8"))
print(f"Lineups: {data['num_generated']}/{data['num_requested']}")
print(f"Pool size: {data['pool_size']}")
print(f"Generation time: {data['generation_time_ms']}ms")

# Flatten all players from lineups
all_players = []
exposure = Counter()
for lu in data["lineups"]:
    for p in lu["players"]:
        name = p.get("display_name") or p.get("player_name", "?")
        exposure[name] += 1
        all_players.append(p)

n_lineups = len(data["lineups"])
print(f"\n--- Top 20 by exposure ---")
for name, cnt in exposure.most_common(20):
    # Get player info from first appearance
    info = next(p for p in all_players if (p.get("display_name") or p.get("player_name")) == name)
    pct = cnt / n_lineups * 100
    print(f"  {name:25s} {pct:5.1f}%  ${info['salary']:5d}  {info['projected_fp']:5.1f}FP  {info['projected_minutes']:4.1f}min  {info['team_abbreviation']}")

# Check for ghost players
ghost_names = ["Zach Edey", "Jaren Jackson", "Walker Kessler", "Damian Lillard", "Kelly Oubre", "Tosan Evbuomwan", "Tyrese Martin"]
print(f"\n--- Ghost player check ---")
for ghost in ghost_names:
    matches = [(name, cnt) for name, cnt in exposure.items() if ghost.lower() in name.lower()]
    if matches:
        for name, cnt in matches:
            info = next(p for p in all_players if (p.get("display_name") or p.get("player_name")) == name)
            print(f"  FOUND: {name:25s} {cnt/n_lineups*100:5.1f}%  ${info['salary']:5d}  {info['projected_fp']:5.1f}FP")
    else:
        print(f"  CLEAN: {ghost} — not in any lineup")

# Load DK Data Hub for comparison
print(f"\n--- Projection comparison vs DK Data Hub ---")
dk_hub = {}
try:
    with open("C:/Users/CFlem/Downloads/DK_NBA_Main_Data_Hub_Projections.csv", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            dk_hub[row["Player"].strip()] = {
                "proj": float(row.get("Projection", 0) or 0),
                "salary": int(row.get("Salary", 0) or 0),
                "ownership": row.get("Ownership %", ""),
                "minutes": float(row.get("Minutes", 0) or 0),
            }
except Exception as e:
    print(f"  Could not load DK hub: {e}")

if dk_hub:
    # Match our top-exposure players to DK hub
    print(f"{'Player':25s} {'Ours':>6s} {'DK':>6s} {'Delta':>7s} {'Our$':>6s} {'DKmin':>5s}")
    for name, cnt in exposure.most_common(30):
        info = next(p for p in all_players if (p.get("display_name") or p.get("player_name")) == name)
        our_fp = info["projected_fp"]
        # Try matching DK hub
        dk_match = dk_hub.get(name)
        if not dk_match:
            # Try partial match
            for dk_name, dk_info in dk_hub.items():
                if name.lower() in dk_name.lower() or dk_name.lower() in name.lower():
                    dk_match = dk_info
                    break
        if dk_match:
            dk_fp = dk_match["proj"]
            delta = our_fp - dk_fp
            pct_delta = (delta / dk_fp * 100) if dk_fp > 0 else 999
            flag = "!!" if abs(pct_delta) > 30 else ""
            print(f"  {name:25s} {our_fp:5.1f}  {dk_fp:5.1f}  {delta:+6.1f} ({pct_delta:+.0f}%)  ${info['salary']:5d}  {dk_match['minutes']:4.0f}  {flag}")
        else:
            print(f"  {name:25s} {our_fp:5.1f}  {'N/A':>5s}  {'':>7s}  ${info['salary']:5d}")

# Average lineup score comparison
print(f"\n--- Lineup scores ---")
scores = [lu["total_projected_fp"] for lu in data["lineups"]]
print(f"  Avg total FP: {sum(scores)/len(scores):.1f}")
print(f"  Max total FP: {max(scores):.1f}")
print(f"  Min total FP: {min(scores):.1f}")
