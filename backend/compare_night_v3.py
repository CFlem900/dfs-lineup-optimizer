import json, csv, sys
from collections import Counter

sys.stdout.reconfigure(encoding="utf-8")

# Load all versions
versions = {}
for ver, fname in [("v1", "night_v1_lineups.json"),
                     ("v2", "night_v2_lineups.json"),
                     ("v3", "night_v3_lineups.json")]:
    data = json.load(open(fname, encoding="utf-8"))
    exposure = Counter()
    player_data = {}
    for lu in data["lineups"]:
        for p in lu["players"]:
            name = p.get("display_name") or p.get("player_name", "?")
            exposure[name] += 1
            if name not in player_data:
                player_data[name] = p
    n = len(data["lineups"])
    versions[ver] = {"data": data, "exposure": exposure,
                     "player_data": player_data, "n": n}

# DK sims
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

# DK Hub
dk_hub = {}
DK_HUB = r"C:\Users\CFlem\Downloads\DK_NBA_Night_Data_Hub_Projections.csv"
with open(DK_HUB, encoding="utf-8") as f:
    reader = csv.DictReader(f)
    for row in reader:
        name = row["Player"].strip()
        proj = float(row.get("Projection", 0) or 0)
        sal = int(row.get("Salary", 0) or 0)
        dk_hub[name] = {"proj": proj, "salary": sal}

# Compute overlaps for each version
for ver in ["v1", "v2", "v3"]:
    v = versions[ver]
    dk_top10 = set(n for n, _ in dk_sims_exposure.most_common(10))
    dk_top20 = set(n for n, _ in dk_sims_exposure.most_common(20))
    v_top10 = set(n for n, _ in v["exposure"].most_common(10))
    v_top20 = set(n for n, _ in v["exposure"].most_common(20))
    v["overlap_10"] = len(dk_top10 & v_top10)
    v["overlap_20"] = len(dk_top20 & v_top20)

    # Avg delta
    total_delta = 0
    count = 0
    for name, cnt in dk_sims_exposure.most_common(50):
        dk_pct = cnt / n_dk * 100
        if dk_pct > 5:
            v_pct = v["exposure"].get(name, 0) / v["n"] * 100
            total_delta += abs(v_pct - dk_pct)
            count += 1
    v["avg_delta"] = total_delta / count if count > 0 else 0

    # Projection accuracy
    deltas = []
    for name, info in v["player_data"].items():
        fp = info["projected_fp"]
        dk_match = dk_hub.get(name)
        if dk_match and dk_match["proj"] > 0:
            pct = abs(fp - dk_match["proj"]) / dk_match["proj"] * 100
            deltas.append(pct)
    v["proj_deltas"] = deltas

# Header
print("=== Night Slate v3 (no salary-min caps) ===")
d = versions["v3"]["data"]
print(f"Lineups: {d['num_generated']}/{d['num_requested']}  Pool: {d['pool_size']}")

# Comparison table
print(f"\n--- Version Comparison ---")
print(f"{'Metric':25s} {'v1':>8s} {'v2':>8s} {'v3':>8s}")
for ver in ["v1", "v2", "v3"]:
    v = versions[ver]
print(f"{'Lineups generated':25s} {versions['v1']['data']['num_generated']:>5}/150 {versions['v2']['data']['num_generated']:>5}/150 {versions['v3']['data']['num_generated']:>5}/150")
print(f"{'Top-10 overlap':25s} {versions['v1']['overlap_10']:>5}/10  {versions['v2']['overlap_10']:>5}/10  {versions['v3']['overlap_10']:>5}/10")
print(f"{'Top-20 overlap':25s} {versions['v1']['overlap_20']:>5}/20  {versions['v2']['overlap_20']:>5}/20  {versions['v3']['overlap_20']:>5}/20")
print(f"{'Avg exposure delta':25s} {versions['v1']['avg_delta']:>7.1f}% {versions['v2']['avg_delta']:>7.1f}% {versions['v3']['avg_delta']:>7.1f}%")

for ver in ["v1", "v2", "v3"]:
    d = versions[ver]["proj_deltas"]
    if d:
        mean = sum(d) / len(d)
        w10 = sum(1 for x in d if x <= 10) / len(d) * 100
        w20 = sum(1 for x in d if x <= 20) / len(d) * 100
        print(f"{'Mean proj error (' + ver + ')':25s} {mean:>7.1f}%  within10={w10:.0f}%  within20={w20:.0f}%")

# Exposure comparison for v3
v3 = versions["v3"]
print(f"\n{'Player':30s} {'v3%':>6s} {'v2%':>6s} {'DK%':>6s} {'v3FP':>5s} {'DKFP':>5s}")
all_names = set(list(v3["exposure"].keys()) + list(dk_sims_exposure.keys()))
comparison = []
for name in all_names:
    v3_pct = v3["exposure"].get(name, 0) / v3["n"] * 100
    v2_pct = versions["v2"]["exposure"].get(name, 0) / versions["v2"]["n"] * 100
    dk_pct = dk_sims_exposure.get(name, 0) / n_dk * 100
    comparison.append((name, v3_pct, v2_pct, dk_pct))

comparison.sort(key=lambda x: max(x[1], x[3]), reverse=True)
for name, v3_pct, v2_pct, dk_pct in comparison[:30]:
    if v3_pct > 0 or dk_pct > 5:
        info = v3["player_data"].get(name, {})
        dk_info = dk_hub.get(name, {})
        v3_fp = info.get("projected_fp", 0)
        dk_fp = dk_info.get("proj", 0)
        flag = " !!" if abs(v3_pct - dk_pct) > 20 else ""
        print(f"  {name:30s} {v3_pct:5.1f}% {v2_pct:5.1f}% {dk_pct:5.1f}% {v3_fp:5.1f} {dk_fp:5.1f}{flag}")

# Missing
print(f"\n--- Missing (>10% DK) ---")
missing = 0
for name, cnt in dk_sims_exposure.most_common(30):
    dk_pct = cnt / n_dk * 100
    if dk_pct > 10 and v3["exposure"].get(name, 0) == 0:
        dk_info = dk_hub.get(name, {})
        print(f"  {name:30s} DK:{dk_pct:5.1f}%  proj={dk_info.get('proj',0):.1f}FP  ${dk_info.get('salary',0)}")
        missing += 1
if missing == 0:
    print("  None!")
