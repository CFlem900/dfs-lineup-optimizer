import json, csv, sys
from collections import Counter

sys.stdout.reconfigure(encoding="utf-8")

# === Load our lineups ===
data = json.load(open("night_v1_lineups.json", encoding="utf-8"))
print(f"=== Night Slate v1 vs DK Sims ===")
print(f"Lineups: {data['num_generated']}/{data['num_requested']}")
print(f"Pool size: {data['pool_size']}")
print(f"Time: {data['generation_time_ms']}ms")

# === Load DK sims ===
dk_sims_exposure = Counter()
DK_SIMS = r"C:\Users\CFlem\Downloads\DK_NBA_Night_Pre_Contest_Sims_Lineups (6).csv"
with open(DK_SIMS, encoding="utf-8") as f:
    reader = csv.DictReader(f)
    n_dk = 0
    for row in reader:
        n_dk += 1
        for col in ["PG", "SG", "SF", "PF", "C", "G", "F", "UTIL"]:
            val = row.get(col, "") or row.get("\ufeff" + col, "")
            if val:
                dk_sims_exposure[val.split("(")[0].strip()] += 1
print(f"DK sims: {n_dk} lineups")

# === Load DK Hub projections ===
dk_hub = {}
DK_HUB = r"C:\Users\CFlem\Downloads\DK_NBA_Night_Data_Hub_Projections.csv"
with open(DK_HUB, encoding="utf-8") as f:
    reader = csv.DictReader(f)
    for row in reader:
        name = row["Player"].strip()
        proj = float(row.get("Projection", 0) or 0)
        sal = int(row.get("Salary", 0) or 0)
        own = row.get("Ownership %", "")
        mins = float(row.get("Minutes", 0) or 0)
        team = row.get("Team", "")
        dk_hub[name] = {"proj": proj, "salary": sal, "ownership": own,
                        "minutes": mins, "team": team}

# === Our exposure ===
our_exposure = Counter()
player_data = {}
for lu in data["lineups"]:
    for p in lu["players"]:
        name = p.get("display_name") or p.get("player_name", "?")
        our_exposure[name] += 1
        if name not in player_data:
            player_data[name] = p

n_ours = len(data["lineups"])

# === Ghost check ===
print(f"\n--- Ghost Player Check ---")
all_clean = True
ghost_names = ["Zach Edey", "Jaren Jackson", "Walker Kessler",
               "Damian Lillard", "Kelly Oubre", "Bradley Beal",
               "Tyrese Haliburton", "Kevin Porter"]
for ghost in ghost_names:
    matches = [(n, c) for n, c in our_exposure.items() if ghost.lower() in n.lower()]
    if matches:
        all_clean = False
        for n, c in matches:
            print(f"  GHOST: {n} ({c}/{n_ours} = {c/n_ours*100:.1f}%)")
    else:
        print(f"  CLEAN: {ghost}")

# === Exposure comparison ===
dk_top10 = [n for n, _ in dk_sims_exposure.most_common(10)]
our_top10 = [n for n, _ in our_exposure.most_common(10)]
overlap_10 = len(set(dk_top10) & set(our_top10))

dk_top20 = set(n for n, _ in dk_sims_exposure.most_common(20))
our_top20 = set(n for n, _ in our_exposure.most_common(20))
overlap_20 = len(dk_top20 & our_top20)

print(f"\n--- Exposure vs DK Sims ---")
print(f"Top-10 overlap: {overlap_10}/10")
print(f"Top-20 overlap: {overlap_20}/20")

print(f"\n{'Player':30s} {'Ours%':>6s} {'DK%':>6s} {'Delta':>7s}  {'$':>5s} {'OurFP':>6s} {'DKFP':>6s} {'FP_D':>6s}")
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
        info = player_data.get(name, {})
        dk_info = dk_hub.get(name, {})
        our_fp = info.get("projected_fp", 0) if info else 0
        dk_fp = dk_info.get("proj", 0)
        sal = info.get("salary", dk_info.get("salary", 0))
        fp_delta = ""
        if our_fp > 0 and dk_fp > 0:
            fp_delta = f"{(our_fp-dk_fp)/dk_fp*100:+.0f}%"
        flag = " !!" if delta > 20 else ""
        print(f"  {name:30s} {our_pct:5.1f}% {dk_pct:5.1f}% {our_pct-dk_pct:+6.1f}%  ${sal:5} {our_fp:5.1f}  {dk_fp:5.1f}  {fp_delta:>5s}{flag}")
        if dk_pct > 5:
            total_delta += abs(our_pct - dk_pct)
            count += 1

if count > 0:
    print(f"\n  Avg delta (players >5% DK exp): {total_delta/count:.1f}%")

# === Projection accuracy ===
print(f"\n--- Projection Accuracy vs DK Data Hub ---")
deltas = []
for name in sorted(player_data.keys()):
    info = player_data[name]
    our_fp = info["projected_fp"]
    dk_match = dk_hub.get(name)
    if not dk_match:
        for dk_name in dk_hub:
            if name.lower() in dk_name.lower() or dk_name.lower() in name.lower():
                dk_match = dk_hub[dk_name]
                break
    if dk_match and dk_match["proj"] > 0:
        dk_fp = dk_match["proj"]
        pct = (our_fp - dk_fp) / dk_fp * 100
        deltas.append(abs(pct))
        flag = " !!" if abs(pct) > 30 else ""
        if abs(pct) > 15 or our_exposure.get(name, 0) > 5:
            print(f"  {name:30s} ours={our_fp:5.1f} dk={dk_fp:5.1f} {pct:+6.1f}%  ${info['salary']:5d}  min={info['projected_minutes']:.1f}{flag}")

if deltas:
    print(f"\n  Mean absolute error: {sum(deltas)/len(deltas):.1f}%")
    within_10 = sum(1 for d in deltas if d <= 10) / len(deltas) * 100
    within_20 = sum(1 for d in deltas if d <= 20) / len(deltas) * 100
    within_30 = sum(1 for d in deltas if d <= 30) / len(deltas) * 100
    print(f"  Within 10%: {within_10:.0f}%  Within 20%: {within_20:.0f}%  Within 30%: {within_30:.0f}%")

# === Missing from ours ===
print(f"\n--- Missing from our lineups (>10% DK exposure) ---")
missing_count = 0
for name, cnt in dk_sims_exposure.most_common(30):
    dk_pct = cnt / n_dk * 100
    if dk_pct > 10 and our_exposure.get(name, 0) == 0:
        dk_info = dk_hub.get(name, {})
        print(f"  {name:30s} DK:{dk_pct:5.1f}% proj={dk_info.get('proj',0):.1f}FP  ${dk_info.get('salary',0)}  team={dk_info.get('team','?')}")
        missing_count += 1

if missing_count == 0:
    print("  None! All high-exposure DK players are in our lineups.")

# === Lineup quality ===
scores = [lu["total_projected_fp"] for lu in data["lineups"]]
print(f"\n--- Lineup Quality ---")
print(f"  Avg total FP: {sum(scores)/len(scores):.1f}")
print(f"  Max total FP: {max(scores):.1f}")
print(f"  Min total FP: {min(scores):.1f}")

# === Summary ===
print(f"\n{'='*55}")
print(f"SCORECARD:")
print(f"  Lineups generated:   {data['num_generated']}/{data['num_requested']}")
print(f"  Pool size:           {data['pool_size']}")
print(f"  Ghost players:       {'ALL CLEAN' if all_clean else 'SOME REMAIN'}")
print(f"  Top-10 overlap:      {overlap_10}/10")
print(f"  Top-20 overlap:      {overlap_20}/20")
if count > 0:
    print(f"  Avg exposure delta:  {total_delta/count:.1f}%")
if deltas:
    print(f"  Mean proj error:     {sum(deltas)/len(deltas):.1f}%")
    print(f"  Within 10% of DK:   {within_10:.0f}%")
    print(f"  Within 20% of DK:   {within_20:.0f}%")
