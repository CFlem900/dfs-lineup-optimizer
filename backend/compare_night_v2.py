import json, csv, sys
from collections import Counter

sys.stdout.reconfigure(encoding="utf-8")

# Load v1 and v2 for comparison
v1_data = json.load(open("night_v1_lineups.json", encoding="utf-8"))
v2_data = json.load(open("night_v2_lineups.json", encoding="utf-8"))

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

# Build exposure for v2
v2_exposure = Counter()
v2_player_data = {}
for lu in v2_data["lineups"]:
    for p in lu["players"]:
        name = p.get("display_name") or p.get("player_name", "?")
        v2_exposure[name] += 1
        if name not in v2_player_data:
            v2_player_data[name] = p

n_v2 = len(v2_data["lineups"])

# Build exposure for v1
v1_exposure = Counter()
for lu in v1_data["lineups"]:
    for p in lu["players"]:
        name = p.get("display_name") or p.get("player_name", "?")
        v1_exposure[name] += 1
n_v1 = len(v1_data["lineups"])

# Overlap
dk_top10 = [n for n, _ in dk_sims_exposure.most_common(10)]
v2_top10 = [n for n, _ in v2_exposure.most_common(10)]
overlap_10 = len(set(dk_top10) & set(v2_top10))

dk_top20 = set(n for n, _ in dk_sims_exposure.most_common(20))
v2_top20 = set(n for n, _ in v2_exposure.most_common(20))
overlap_20 = len(dk_top20 & v2_top20)

v1_top10 = [n for n, _ in v1_exposure.most_common(10)]
v1_overlap_10 = len(set(dk_top10) & set(v1_top10))
v1_top20 = set(n for n, _ in v1_exposure.most_common(20))
v1_overlap_20 = len(dk_top20 & v1_top20)

print(f"=== Night Slate v2 vs v1 vs DK Sims ===")
print(f"v1: {v1_data['num_generated']}/{v1_data['num_requested']} lineups  Top-10: {v1_overlap_10}/10  Top-20: {v1_overlap_20}/20")
print(f"v2: {v2_data['num_generated']}/{v2_data['num_requested']} lineups  Top-10: {overlap_10}/10  Top-20: {overlap_20}/20")

print(f"\n{'Player':30s} {'v2%':>6s} {'v1%':>6s} {'DK%':>6s} {'v2FP':>5s} {'DKFP':>5s} {'FP%':>6s}")

all_names = set(list(v2_exposure.keys()) + list(dk_sims_exposure.keys()) + list(v1_exposure.keys()))
comparison = []
for name in all_names:
    v2_pct = v2_exposure.get(name, 0) / n_v2 * 100
    v1_pct = v1_exposure.get(name, 0) / n_v1 * 100
    dk_pct = dk_sims_exposure.get(name, 0) / n_dk * 100
    comparison.append((name, v2_pct, v1_pct, dk_pct))

comparison.sort(key=lambda x: max(x[1], x[3]), reverse=True)

total_delta = 0
count = 0
for name, v2_pct, v1_pct, dk_pct in comparison[:35]:
    if v2_pct > 0 or dk_pct > 5:
        info = v2_player_data.get(name, {})
        dk_info = dk_hub.get(name, {})
        v2_fp = info.get("projected_fp", 0)
        dk_fp = dk_info.get("proj", 0)
        fp_pct = f"{(v2_fp-dk_fp)/dk_fp*100:+.0f}%" if v2_fp > 0 and dk_fp > 0 else ""
        change = ""
        if v1_pct > 0 and v2_pct > 0:
            if v2_pct > v1_pct + 5:
                change = " UP"
            elif v2_pct < v1_pct - 5:
                change = " DN"
        elif v2_pct > 0 and v1_pct == 0:
            change = " NEW"
        elif v1_pct > 0 and v2_pct == 0:
            change = " OUT"
        flag = " !!" if abs(v2_pct - dk_pct) > 20 else ""
        print(f"  {name:30s} {v2_pct:5.1f}% {v1_pct:5.1f}% {dk_pct:5.1f}% {v2_fp:5.1f} {dk_fp:5.1f} {fp_pct:>5s}{change}{flag}")
        if dk_pct > 5:
            total_delta += abs(v2_pct - dk_pct)
            count += 1

if count > 0:
    print(f"\n  Avg delta (>5% DK): {total_delta/count:.1f}%  (v1 was 15.6%)")

# Projection accuracy
deltas = []
for name, info in v2_player_data.items():
    v2_fp = info["projected_fp"]
    dk_match = dk_hub.get(name)
    if not dk_match:
        for dk_name in dk_hub:
            if name.lower() in dk_name.lower() or dk_name.lower() in name.lower():
                dk_match = dk_hub[dk_name]
                break
    if dk_match and dk_match["proj"] > 0:
        dk_fp = dk_match["proj"]
        pct = abs(v2_fp - dk_fp) / dk_fp * 100
        deltas.append(pct)

if deltas:
    print(f"\n--- Projection Accuracy ---")
    print(f"  Mean abs error: {sum(deltas)/len(deltas):.1f}%")
    within_10 = sum(1 for d in deltas if d <= 10) / len(deltas) * 100
    within_20 = sum(1 for d in deltas if d <= 20) / len(deltas) * 100
    within_30 = sum(1 for d in deltas if d <= 30) / len(deltas) * 100
    print(f"  Within 10%: {within_10:.0f}%  Within 20%: {within_20:.0f}%  Within 30%: {within_30:.0f}%")

# Missing
print(f"\n--- Missing (>10% DK) ---")
missing = 0
for name, cnt in dk_sims_exposure.most_common(30):
    dk_pct = cnt / n_dk * 100
    if dk_pct > 10 and v2_exposure.get(name, 0) == 0:
        dk_info = dk_hub.get(name, {})
        print(f"  {name:30s} DK:{dk_pct:5.1f}%  proj={dk_info.get('proj',0):.1f}FP  ${dk_info.get('salary',0)}")
        missing += 1
if missing == 0:
    print("  None! All high-exposure DK players present.")

# Biggest improvements from v1 to v2
print(f"\n--- Biggest v1 -> v2 Changes ---")
changes = []
for name in all_names:
    v2_pct = v2_exposure.get(name, 0) / n_v2 * 100
    v1_pct = v1_exposure.get(name, 0) / n_v1 * 100
    dk_pct = dk_sims_exposure.get(name, 0) / n_dk * 100
    # Better = closer to DK
    v1_delta = abs(v1_pct - dk_pct)
    v2_delta = abs(v2_pct - dk_pct)
    if abs(v2_delta - v1_delta) > 3:
        changes.append((name, v1_pct, v2_pct, dk_pct, v1_delta - v2_delta))

changes.sort(key=lambda x: x[4], reverse=True)
for name, v1_pct, v2_pct, dk_pct, improvement in changes[:10]:
    direction = "BETTER" if improvement > 0 else "WORSE"
    print(f"  {name:30s} v1={v1_pct:5.1f}% v2={v2_pct:5.1f}% dk={dk_pct:5.1f}%  [{direction}: {improvement:+.1f}pp]")

# Summary
print(f"\n{'='*55}")
print(f"SCORECARD:")
print(f"  v1: {v1_data['num_generated']}/150  Top-10: {v1_overlap_10}/10  Top-20: {v1_overlap_20}/20")
print(f"  v2: {v2_data['num_generated']}/150  Top-10: {overlap_10}/10  Top-20: {overlap_20}/20")
if count > 0:
    print(f"  Avg exposure delta: {total_delta/count:.1f}%")
if deltas:
    print(f"  Mean proj error:    {sum(deltas)/len(deltas):.1f}%")
    print(f"  Within 10%: {within_10:.0f}%  Within 20%: {within_20:.0f}%")
