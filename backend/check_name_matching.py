"""Check name matching between DK draftables and rotation projections."""
import sys, io, json, requests

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

BASE = "http://localhost:8000"

from app.services.dk_draftables_service import DKDraftablesService
dk_svc = DKDraftablesService()
all_dk = dk_svc.get_draftables(143420)

team_id_map = {
    "ATL": 1610612737, "DAL": 1610612742, "DET": 1610612765, "BKN": 1610612751,
    "BOS": 1610612738, "SAS": 1610612759, "TOR": 1610612761, "HOU": 1610612745,
    "MEM": 1610612763, "PHI": 1610612755, "PHX": 1610612756, "MIL": 1610612749,
    "WAS": 1610612764, "MIA": 1610612748,
}

# Load the _names_match function from lineup optimizer
from app.services.lineup_optimizer_service import LineupOptimizerService

# Check the name matching for specific missing players
target_checks = [
    ("Kel'el Ware", "MIA"), ("Dyson Daniels", "ATL"),
    ("Trendon Watford", "PHI"), ("Andre Drummond", "PHI"),
    ("Gregory Jackson", "MEM"), ("Scotty Pippen Jr.", "MEM"),
    ("Ousmane Dieng", "MIL"), ("Adem Bona", "PHI"),
    ("Carter Bryant", "SAS"), ("Collin Gillespie", "PHX"),
    ("VJ Edgecombe", "PHI"), ("Kelly Oubre Jr.", "PHI"),
    ("Ty Jerome", "MEM"), ("Drake Powell", "BKN"),
    ("Cam Thomas", "MIL"), ("Egor Demin", "BKN"),
    ("Alexandre Sarr", "WAS"),
]

print("=" * 110)
print(f"{'DK Name':<30} {'Team':>4} {'Rot Name':<30} {'Match?':>7} {'Issue'}")
print("=" * 110)

for dk_name, team in target_checks:
    tid = team_id_map[team]
    try:
        r = requests.get(f"{BASE}/api/teams/{tid}/rotation",
                         params={"draft_group_id": 143420, "sport": "nba"},
                         timeout=120)
        rot_data = r.json()
    except:
        print(f"{dk_name:<30} {team:>4} {'TIMEOUT':<30}")
        continue

    rot_projs = rot_data.get("projections", [])

    # Find closest rotation name
    best_match = None
    for p in rot_projs:
        rn = p["player_name"]
        if LineupOptimizerService._names_match(dk_name, rn):
            best_match = rn
            break

    if best_match:
        print(f"{dk_name:<30} {team:>4} {best_match:<30} {'YES':>7}")
    else:
        # Find similar names
        similar = []
        for p in rot_projs:
            rn = p["player_name"]
            # Check last name match
            dk_parts = dk_name.lower().replace(".", "").split()
            rn_parts = rn.lower().replace(".", "").split()
            if dk_parts[-1] == rn_parts[-1]:
                similar.append(rn)
            elif any(w in rn.lower() for w in dk_parts if len(w) > 3):
                similar.append(rn)

        if similar:
            issue = f"Name mismatch → close: {similar}"
        else:
            issue = "NOT in rotation at all"
        print(f"{dk_name:<30} {team:>4} {'---':<30} {'NO':>7} {issue}")

# Also check: what's the DK name for Egor Demin vs Egor Dëmin?
print("\n\nSPECIAL CHARACTER COMPARISON:")
for dp in all_dk:
    if "egor" in dp.display_name.lower() or "demin" in dp.display_name.lower():
        # Hex dump the name
        name_bytes = dp.display_name.encode('utf-8')
        print(f"DK: '{dp.display_name}' → bytes: {name_bytes}")

# Check rotation name
for team, tid in [("BKN", 1610612751)]:
    r = requests.get(f"{BASE}/api/teams/{tid}/rotation",
                     params={"draft_group_id": 143420, "sport": "nba"},
                     timeout=120)
    rot_data = r.json()
    for p in rot_data.get("projections", []):
        if "egor" in p["player_name"].lower() or "min" in p["player_name"].lower():
            name_bytes = p["player_name"].encode('utf-8')
            print(f"ROT: '{p['player_name']}' → bytes: {name_bytes}")
