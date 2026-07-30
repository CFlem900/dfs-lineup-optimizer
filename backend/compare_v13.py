import json, csv, sys
from collections import Counter

sys.stdout.reconfigure(encoding="utf-8")

data = json.load(open("v14_lineups.json", encoding="utf-8"))
print(f"=== v14 vs New DK Sims ===")
print(f"Lineups: {data['num_generated']}/{data['num_requested']}")
print(f"Pool size: {data['pool_size']}")
print(f"Generation time: {data['generation_time_ms']}ms")

# Load new DK sims (file 10)
dk_sims_exposure = Counter()
dk_sims_lineups = []
DK_SIMS_FILE = r"C:\Users\CFlem\Downloads\DK_NBA_Main_Pre_Contest_Sims_Lineups (10).csv"
try:
    with open(DK_SIMS_FILE, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            lu_players = []
            for col in ["PG", "SG", "SF", "PF", "C", "G", "F", "UTIL"]:
                val = row.get(col, "")
                if val:
                    name = val.split("(")[0].strip()
                    lu_players.append(name)
                    dk_sims_exposure[name] += 1
            dk_sims_lineups.append(lu_players)
    n_dk = len(dk_sims_lineups)
    print(f"DK sims loaded: {n_dk} lineups")
except Exception as e:
    print(f"Could not load DK sims: {e}")
    n_dk = 0

# Load DK Data Hub (if available)
dk_hub = {}
DK_HUB_FILE = r"C:\Users\CFlem\Downloads\DK_NBA_Main_Data_Hub_Projections.csv"
try:
    with open(DK_HUB_FILE, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            dk_hub[row["Player"].strip()] = {
                "proj": float(row.get("Projection", 0) or 0),
                "salary": int(row.get("Salary", 0) or 0),
                "ownership": row.get("Ownership %", ""),
                "minutes": float(row.get("Minutes", 0) or 0),
            }
except Exception as e:
    print(f"DK hub not available: {e}")

# Our exposure
our_exposure = Counter()
player_data = {}
for lu in data["lineups"]:
    for p in lu["players"]:
        name = p.get("display_name") or p.get("player_name", "?")
        our_exposure[name] += 1
        if name not in player_data:
            player_data[name] = p

n_ours = len(data["lineups"])

# Ghost player check
ghost_names = ["Zach Edey", "Jaren Jackson", "Walker Kessler", "Damian Lillard",
               "Kelly Oubre", "Tosan Evbuomwan", "Tyrese Martin"]
print(f"\n--- Ghost Player Check ---")
all_clean = True
for ghost in ghost_names:
    matches = [(name, cnt) for name, cnt in our_exposure.items() if ghost.lower() in name.lower()]
    if matches:
        for name, cnt in matches:
            print(f"  STILL IN: {name} ({cnt}/{n_ours} = {cnt/n_ours*100:.1f}%)")
            all_clean = False
    else:
        print(f"  CLEAN: {ghost}")

# DK Sims top exposure comparison
if n_dk > 0:
    dk_top10 = [name for name, _ in dk_sims_exposure.most_common(10)]
    our_top10 = [name for name, _ in our_exposure.most_common(10)]
    overlap_10 = len(set(dk_top10) & set(our_top10))

    dk_top20 = set(name for name, _ in dk_sims_exposure.most_common(20))
    our_top20 = set(name for name, _ in our_exposure.most_common(20))
    overlap_20 = len(dk_top20 & our_top20)

    print(f"\n--- Exposure vs DK Sims ---")
    print(f"Top-10 overlap: {overlap_10}/10")
    print(f"Top-20 overlap: {overlap_20}/20")

    print(f"\n{'Player':25s} {'Ours%':>6s} {'DK%':>6s} {'Delta':>7s}")
    all_names = set(list(our_exposure.keys()) + list(dk_sims_exposure.keys()))
    comparison = []
    for name in all_names:
        our_pct = our_exposure.get(name, 0) / n_ours * 100
        dk_pct = dk_sims_exposure.get(name, 0) / n_dk * 100
        comparison.append((name, our_pct, dk_pct, abs(our_pct - dk_pct)))

    comparison.sort(key=lambda x: max(x[1], x[2]), reverse=True)

    total_delta = 0
    count = 0
    for name, our_pct, dk_pct, delta in comparison[:35]:
        if our_pct > 0 or dk_pct > 0:
            flag = " !!" if delta > 20 else ""
            print(f"  {name:25s} {our_pct:5.1f}% {dk_pct:5.1f}% {our_pct-dk_pct:+6.1f}%{flag}")
            if dk_pct > 5:
                total_delta += abs(our_pct - dk_pct)
                count += 1

    if count > 0:
        print(f"\n  Avg delta (players >5% DK exp): {total_delta/count:.1f}%")

# Projection comparison vs DK Data Hub
if dk_hub:
    print(f"\n--- Projection Accuracy vs DK Data Hub ---")
    print(f"{'Player':25s} {'Ours':>6s} {'DK':>6s} {'Delta':>8s}  {'$':>5s}")

    deltas = []
    for name, cnt in our_exposure.most_common(35):
        info = player_data[name]
        our_fp = info["projected_fp"]
        dk_match = dk_hub.get(name)
        if not dk_match:
            for dk_name, dk_info in dk_hub.items():
                if name.lower() in dk_name.lower() or dk_name.lower() in name.lower():
                    dk_match = dk_info
                    break
        if dk_match and dk_match["proj"] > 0:
            dk_fp = dk_match["proj"]
            delta = our_fp - dk_fp
            pct = delta / dk_fp * 100
            deltas.append(abs(pct))
            flag = " !!" if abs(pct) > 30 else ""
            print(f"  {name:25s} {our_fp:5.1f}  {dk_fp:5.1f}  {delta:+5.1f} ({pct:+.0f}%)  ${info['salary']:5d}{flag}")

    if deltas:
        print(f"\n  Mean absolute projection error: {sum(deltas)/len(deltas):.1f}%")
        within_10 = sum(1 for d in deltas if d <= 10) / len(deltas) * 100
        within_20 = sum(1 for d in deltas if d <= 20) / len(deltas) * 100
        within_30 = sum(1 for d in deltas if d <= 30) / len(deltas) * 100
        print(f"  Within 10%: {within_10:.0f}%")
        print(f"  Within 20%: {within_20:.0f}%")
        print(f"  Within 30%: {within_30:.0f}%")

# Missing from our lineups
if n_dk > 0:
    print(f"\n--- Missing from our lineups (>10% DK exposure) ---")
    for name, cnt in dk_sims_exposure.most_common(50):
        dk_pct = cnt / n_dk * 100
        if dk_pct > 10 and our_exposure.get(name, 0) == 0:
            dk_info = dk_hub.get(name, {})
            proj = dk_info.get("proj", 0)
            sal = dk_info.get("salary", 0)
            print(f"  {name:25s} DK: {dk_pct:5.1f}%  proj={proj:.1f}FP  ${sal}")

# Lineup quality
scores = [lu["total_projected_fp"] for lu in data["lineups"]]
print(f"\n--- Lineup Quality ---")
print(f"  Avg total FP: {sum(scores)/len(scores):.1f}")
print(f"  Max total FP: {max(scores):.1f}")
print(f"  Min total FP: {min(scores):.1f}")

# Summary
print(f"\n{'='*50}")
print(f"SCORECARD:")
print(f"  Lineups generated:   {data['num_generated']}/{data['num_requested']}")
print(f"  Ghost players:       {'ALL ELIMINATED' if all_clean else 'SOME REMAIN'}")
if n_dk > 0:
    print(f"  Top-10 overlap:      {overlap_10}/10")
    print(f"  Top-20 overlap:      {overlap_20}/20")
    if count > 0:
        print(f"  Avg exposure delta:  {total_delta/count:.1f}%")
if deltas:
    print(f"  Mean proj error:     {sum(deltas)/len(deltas):.1f}%")
    print(f"  Within 10% of DK:   {within_10:.0f}%")
    print(f"  Within 20% of DK:   {within_20:.0f}%")
