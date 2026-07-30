import json, sys
sys.stdout.reconfigure(encoding="utf-8")

data = json.load(open("v10_test.json", encoding="utf-8"))

# Find Rupert's data from lineups
for lu in data["lineups"]:
    for p in lu["players"]:
        name = p.get("display_name") or p.get("player_name", "")
        if "rupert" in name.lower():
            print(f"Name: {name}")
            print(f"  Salary: ${p['salary']}")
            print(f"  Projected FP: {p['projected_fp']}")
            print(f"  Projected Minutes: {p['projected_minutes']}")
            print(f"  Floor/Ceil: {p['floor_fp']}/{p['ceiling_fp']}")
            stats = p.get("projected_stats", {})
            if stats:
                print(f"  Stats: {stats}")
            fp_per_min = p['projected_fp'] / p['projected_minutes'] if p['projected_minutes'] > 0 else 0
            print(f"  FP/min: {fp_per_min:.2f}")
            break
    else:
        continue
    break

# Also check Sandfort and Williams
for target in ["sandfort", "jaylin williams", "gregory jackson", "vit krejci", "aaron wiggins"]:
    for lu in data["lineups"]:
        for p in lu["players"]:
            name = (p.get("display_name") or p.get("player_name", "")).lower()
            if target in name:
                print(f"\n{p.get('display_name') or p.get('player_name')}:")
                print(f"  ${p['salary']} | {p['projected_fp']}FP | {p['projected_minutes']}min | FP/min={p['projected_fp']/max(p['projected_minutes'],0.1):.2f}")
                break
        else:
            continue
        break
