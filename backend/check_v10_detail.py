import json, sys
from collections import Counter
sys.stdout.reconfigure(encoding="utf-8")

data = json.load(open("v10_lineups.json", encoding="utf-8"))
exposure = Counter()
player_data = {}
for lu in data["lineups"]:
    for p in lu["players"]:
        name = p.get("display_name") or p.get("player_name", "?")
        exposure[name] += 1
        if name not in player_data:
            player_data[name] = p

n = len(data["lineups"])

# Check specific players
targets = ["Rayan Rupert", "Payton Sandfort", "Javon Small", "Jaylen Brown",
           "Donovan Clingan", "Cam Spencer", "Trendon Watford"]
print("--- Key Player Check ---")
for t in targets:
    cnt = 0
    info = None
    for name, c in exposure.items():
        if t.lower() in name.lower():
            cnt = c
            info = player_data[name]
            break
    if info:
        print(f"  {t:25s} {cnt/n*100:5.1f}%  ${info['salary']:5d}  {info['projected_fp']:5.1f}FP  {info['projected_minutes']:4.1f}min")
    else:
        print(f"  {t:25s}   0.0%  (not in any lineup)")

# Version comparison summary
print("\n=== VERSION COMPARISON ===")
print(f"{'Version':8s} {'Lups':>5s} {'Top10':>6s} {'Top20':>6s} {'<10%':>5s} {'<20%':>5s} {'Ghosts':>7s}")
print(f"{'v3 base':8s} {'~60':>5s} {'1/10':>6s} {'~5/20':>6s} {'?':>5s} {'?':>5s} {'73.8%':>7s}")
print(f"{'v6 r2':8s} {'84':>5s} {'4/10':>6s} {'~9/20':>6s} {'?':>5s} {'?':>5s} {'~50%':>7s}")
print(f"{'v8 fix':8s} {'150':>5s} {'3/10':>6s} {'8/20':>6s} {'49%':>5s} {'74%':>5s} {'0%':>7s}")
print(f"{'v10 cap':8s} {'150':>5s} {'2/10':>6s} {'6/20':>6s} {'49%':>5s} {'71%':>5s} {'0%':>7s}")
