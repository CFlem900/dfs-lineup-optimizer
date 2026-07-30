import json, sys, csv
from collections import Counter

sys.stdout.reconfigure(encoding="utf-8")

data = json.load(open("v15b_lineups.json", encoding="utf-8"))
print(f"=== v15b vs New DK Sims ===")
print(f"Lineups: {data['num_generated']}/{data['num_requested']}")
print(f"Pool size: {data['pool_size']}")
print(f"Time: {data['generation_time_ms']}ms")

# Load new DK sims
dk_sims_exposure = Counter()
DK_SIMS_FILE = r"C:\Users\CFlem\Downloads\DK_NBA_Main_Pre_Contest_Sims_Lineups (10).csv"
with open(DK_SIMS_FILE, encoding="utf-8") as f:
    reader = csv.DictReader(f)
    for row in reader:
        for col in ["PG", "SG", "SF", "PF", "C", "G", "F", "UTIL"]:
            val = row.get(col, "") or row.get("\ufeff" + col, "")
            if val:
                dk_sims_exposure[val.split("(")[0].strip()] += 1
n_dk = 150

# Load DK Hub
dk_hub = {}
DK_HUB_FILE = r"C:\Users\CFlem\Downloads\DK_NBA_Main_Data_Hub_Projections.csv"
with open(DK_HUB_FILE, encoding="utf-8") as f:
    reader = csv.DictReader(f)
    for row in reader:
        dk_hub[row["Player"].strip()] = {
            "proj": float(row.get("Projection", 0) or 0),
            "salary": int(row.get("Salary", 0) or 0),
        }

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

# Key player check
print("\n--- Key Player Check ---")
for target in ["Justin Edwards", "Cameron Payne", "Javon Small",
               "Deni Avdija", "Donovan Clingan", "Tyrese Maxey",
               "Shai Gilgeous-Alexander", "Quentin Grimes"]:
    matches = [(n, c) for n, c in our_exposure.items() if target.lower() in n.lower()]
    dk_exp = sum(c for n, c in dk_sims_exposure.items() if target.lower() in n.lower())
    if matches:
        for n, c in matches:
            info = player_data[n]
            print(f"  {n:25s} {c}/{n_ours}={c/n_ours*100:5.1f}% (DK:{dk_exp/n_dk*100:5.1f}%)  "
                  f"FP={info['projected_fp']}  sal=${info['salary']}  min={info['projected_minutes']}")
    else:
        dk_info = dk_hub.get(target, {})
        print(f"  {target:25s} MISSING (DK:{dk_exp/n_dk*100:5.1f}%  hub_proj={dk_info.get('proj',0):.1f}  sal=${dk_info.get('salary',0)})")

# Ghost check
print("\n--- Ghost Player Check ---")
all_clean = True
for ghost in ["Zach Edey", "Jaren Jackson", "Walker Kessler", "Damian Lillard"]:
    matches = [(n, c) for n, c in our_exposure.items() if ghost.lower() in n.lower()]
    if matches:
        all_clean = False
        for n, c in matches:
            print(f"  STILL IN: {n} ({c}/{n_ours})")
    else:
        print(f"  CLEAN: {ghost}")

# Top exposure overlap
dk_top10 = [n for n, _ in dk_sims_exposure.most_common(10)]
our_top10 = [n for n, _ in our_exposure.most_common(10)]
overlap_10 = len(set(dk_top10) & set(our_top10))
dk_top20 = set(n for n, _ in dk_sims_exposure.most_common(20))
our_top20 = set(n for n, _ in our_exposure.most_common(20))
overlap_20 = len(dk_top20 & our_top20)

print(f"\n--- Exposure Overlap ---")
print(f"Top-10: {overlap_10}/10")
print(f"Top-20: {overlap_20}/20")

# Missing from our lineups (>10% DK)
print(f"\n--- Missing (>10% DK exposure) ---")
for name, cnt in dk_sims_exposure.most_common(50):
    dk_pct = cnt / n_dk * 100
    if dk_pct > 10 and our_exposure.get(name, 0) == 0:
        dk_info = dk_hub.get(name, {})
        print(f"  {name:25s} DK:{dk_pct:5.1f}%  proj={dk_info.get('proj',0):.1f}FP  ${dk_info.get('salary',0)}")

# Summary
scores = [lu["total_projected_fp"] for lu in data["lineups"]]
print(f"\n{'='*50}")
print(f"SCORECARD:")
print(f"  Lineups: {data['num_generated']}/{data['num_requested']}")
print(f"  Pool size: {data['pool_size']}")
print(f"  Ghosts: {'ALL CLEAN' if all_clean else 'SOME REMAIN'}")
print(f"  Top-10 overlap: {overlap_10}/10")
print(f"  Top-20 overlap: {overlap_20}/20")
print(f"  Avg lineup FP: {sum(scores)/len(scores):.1f}")
