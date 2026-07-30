import json, sys
from collections import Counter
sys.stdout.reconfigure(encoding="utf-8")
data = json.load(open("v14_lineups.json", encoding="utf-8"))
exposure = Counter()
player_info = {}
for lu in data["lineups"]:
    for p in lu["players"]:
        name = p.get("display_name") or p.get("player_name", "?")
        exposure[name] += 1
        if name not in player_info:
            player_info[name] = p
n = len(data["lineups"])
targets = [
    "Javon Small", "Deni Avdija", "Justin Edwards",
    "Cameron Payne", "Donovan Clingan", "Shai Gilgeous-Alexander",
    "Chet Holmgren", "Cam Spencer", "Walter Clayton",
]
for target in targets:
    matches = [(n2, c) for n2, c in exposure.items() if target.lower() in n2.lower()]
    if matches:
        for n2, c in matches:
            info = player_info.get(n2, {})
            fp = info.get("projected_fp", "?")
            sal = info.get("salary", "?")
            mins = info.get("projected_minutes", "?")
            print(f"{n2}: {c}/{n} = {c/n*100:.1f}%  FP={fp} sal=${sal} min={mins}")
    else:
        print(f"{target}: NOT IN POOL")
