"""Check why players are missing — use cached rotation data directly."""
import sys, io, json, requests
from collections import Counter

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

BASE = "http://localhost:8000"

# Get DK draftables
from app.services.dk_draftables_service import DKDraftablesService
dk_svc = DKDraftablesService()
all_dk = dk_svc.get_draftables(143420)

team_id_map = {
    "ATL": 1610612737, "DAL": 1610612742, "DET": 1610612765, "BKN": 1610612751,
    "BOS": 1610612738, "SAS": 1610612759, "TOR": 1610612761, "HOU": 1610612745,
    "MEM": 1610612763, "PHI": 1610612755, "PHX": 1610612756, "MIL": 1610612749,
    "WAS": 1610612764, "MIA": 1610612748,
}

# Teams we already have cached rotation for (use 120s timeout)
print("=" * 100)
print(f"{'Team':>4} {'Rot':>4} {'DK':>4}  Coverage  DK-only names (not in rotation)")
print("=" * 100)

total_rot = 0
total_dk = 0
all_missing_important = []

target_missing = {
    "Kel'el Ware", "Dyson Daniels", "Trendon Watford", "Andre Drummond",
    "Gregory Jackson", "Scotty Pippen Jr.", "Ousmane Dieng", "Adem Bona",
    "Carter Bryant", "Collin Gillespie", "VJ Edgecombe", "Kelly Oubre Jr.",
    "Ty Jerome", "Drake Powell", "Cam Thomas",
}

for abbr in sorted(team_id_map.keys()):
    tid = team_id_map[abbr]
    try:
        r = requests.get(f"{BASE}/api/teams/{tid}/rotation",
                         params={"draft_group_id": 143420, "sport": "nba"},
                         timeout=120)
        rot_data = r.json()
    except Exception as e:
        print(f"{abbr:>4}  --- TIMEOUT ({e.__class__.__name__}) ---")
        continue

    rot_projs = rot_data.get("projections", [])
    rot_names_norm = {}
    for p in rot_projs:
        norm = p["player_name"].lower().replace(".", "").replace("'", "").strip()
        rot_names_norm[norm] = p["player_name"]

    dk_team = [p for p in all_dk if p.team_abbreviation == abbr]
    total_rot += len(rot_projs)
    total_dk += len(dk_team)

    # Find DK players not in rotation
    dk_only = []
    for dp in dk_team:
        dn = dp.display_name.lower().replace(".", "").replace("'", "").strip()
        if dn not in rot_names_norm:
            # Try partial match
            matched = False
            for rn in rot_names_norm:
                # Last name match
                d_parts = dn.split()
                r_parts = rn.split()
                if len(d_parts) >= 2 and len(r_parts) >= 2:
                    if d_parts[-1] == r_parts[-1] and d_parts[0][0] == r_parts[0][0]:
                        matched = True
                        break
            if not matched:
                is_important = dp.display_name in target_missing
                dk_only.append((dp.display_name, dp.salary, dp.status, is_important))

    coverage = f"{len(rot_projs)/len(dk_team)*100:.0f}%" if dk_team else "?"

    # Show important missing first
    imp = [x for x in dk_only if x[3]]
    other_high_sal = [x for x in dk_only if not x[3] and x[1] >= 4000]

    dk_only_str = ""
    if imp:
        dk_only_str += " | ".join(f"**{n}** ${s}" for n, s, _, _ in imp)
    if other_high_sal:
        if dk_only_str:
            dk_only_str += " | "
        dk_only_str += " | ".join(f"{n} ${s}" for n, s, _, _ in sorted(other_high_sal, key=lambda x: -x[1])[:5])

    print(f"{abbr:>4} {len(rot_projs):>4} {len(dk_team):>4}  {coverage:>8}  {dk_only_str}")

print("=" * 100)
print(f"{'TOT':>4} {total_rot:>4} {total_dk:>4}  {total_rot/total_dk*100:.0f}%")

# Show the actual rotation names for teams with important missing players
print("\n\nROTATION NAMES vs MISSING TARGETS:")
for abbr in ["ATL", "MEM", "MIA", "MIL", "PHI", "PHX", "SAS", "BKN"]:
    tid = team_id_map[abbr]
    try:
        r = requests.get(f"{BASE}/api/teams/{tid}/rotation",
                         params={"draft_group_id": 143420, "sport": "nba"},
                         timeout=120)
        rot_data = r.json()
    except:
        continue
    rot_projs = rot_data.get("projections", [])
    names = [(p["player_name"], round(p.get("adjusted_minutes", 0), 1), round(p.get("dk_points", 0), 1)) for p in rot_projs]
    names.sort(key=lambda x: -x[2])
    print(f"\n{abbr} rotation ({len(names)} players):")
    for n, m, fp in names:
        print(f"  {n:<30} min={m:5.1f} fp={fp:5.1f}")
