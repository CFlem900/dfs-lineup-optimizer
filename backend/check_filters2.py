"""Check exactly which players are filtered + why (unique names only)."""
import sys, io, json, requests
from collections import Counter

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

BASE = "http://localhost:8000"

from app.services.dk_draftables_service import DKDraftablesService
from app.services.lineup_optimizer_service import LineupOptimizerService

dk_svc = DKDraftablesService()
all_dk = dk_svc.get_draftables(143420)

# Deduplicate DK entries by name+team
dk_unique = {}
for p in all_dk:
    key = (p.display_name, p.team_abbreviation)
    if key not in dk_unique:
        dk_unique[key] = p

team_id_map = {
    "ATL": 1610612737, "DAL": 1610612742, "DET": 1610612765, "BKN": 1610612751,
    "BOS": 1610612738, "SAS": 1610612759, "TOR": 1610612761, "HOU": 1610612745,
    "MEM": 1610612763, "PHI": 1610612755, "PHX": 1610612756, "MIL": 1610612749,
    "WAS": 1610612764, "MIA": 1610612748,
}

# Load our pool from lineups
with open("lineups_mar10.json", encoding="utf-8") as f:
    ld = json.load(f)
our_pool_names = set()
for lu in ld.get("lineups", []):
    for p in lu.get("players", []):
        our_pool_names.add(p["player_name"])

# Also check DK projections for comparison
import csv
dk_proj = {}
with open(r"C:\Users\CFlem\Downloads\DK_NBA_Main_Data_Hub_Projections (8).csv",
          encoding="utf-8-sig") as f:
    for row in csv.DictReader(f):
        name = row.get("Player", "").strip()
        if name:
            dk_proj[name] = {
                "fp": float(row.get("Projection", 0) or 0),
                "ownership": float(row.get("Ownership %", "0").replace("%", "") or 0),
            }

print(f"DK unique players: {len(dk_unique)}")
print(f"Our pool (in lineups): {len(our_pool_names)}")
print(f"Pool size from response: {ld['pool_size']}")
print()

# For each team, get rotation and check who's filtered
filtered_out = []
total_name_miss = 0
total_zero_min = 0
total_low_games = 0
total_zero_fp = 0
total_injury = 0
total_in_pool = 0

for abbr in sorted(team_id_map.keys()):
    tid = team_id_map[abbr]
    try:
        r = requests.get(f"{BASE}/api/teams/{tid}/rotation",
                         params={"draft_group_id": 143420, "sport": "nba"},
                         timeout=120)
        rot_data = r.json()
    except:
        print(f"{abbr}: TIMEOUT")
        continue

    rot_projs = rot_data.get("projections", [])
    dk_team = {k: v for k, v in dk_unique.items() if k[1] == abbr}

    for (dk_name, _), dp in dk_team.items():
        # Find match in rotation
        rp = None
        for p in rot_projs:
            if LineupOptimizerService._names_match(dk_name, p["player_name"]):
                rp = p
                break

        if not rp:
            total_name_miss += 1
            dk_info = dk_proj.get(dk_name, {})
            if dk_info.get("ownership", 0) > 3:
                filtered_out.append((dk_name, abbr, dp.salary, "name_mismatch",
                                     dk_info.get("fp", 0), dk_info.get("ownership", 0)))
            continue

        adj_min = rp.get("adjusted_minutes", 0)
        dk_pts = rp.get("dk_points", 0)
        gp = len(rp.get("minutes_last_10", []))
        status = (dp.status or "").strip().upper()

        if adj_min <= 0:
            total_zero_min += 1
            dk_info = dk_proj.get(dk_name, {})
            if dk_info.get("ownership", 0) > 3:
                filtered_out.append((dk_name, abbr, dp.salary, f"zero_minutes (adj_min={adj_min:.1f})",
                                     dk_info.get("fp", 0), dk_info.get("ownership", 0)))
        elif gp < 3 and dp.salary < 3600 and adj_min < 12.0:
            total_low_games += 1
            dk_info = dk_proj.get(dk_name, {})
            if dk_info.get("ownership", 0) > 3:
                filtered_out.append((dk_name, abbr, dp.salary, f"low_games ({gp} games, ${dp.salary})",
                                     dk_info.get("fp", 0), dk_info.get("ownership", 0)))
        elif dk_pts <= 0:
            total_zero_fp += 1
            dk_info = dk_proj.get(dk_name, {})
            if dk_info.get("ownership", 0) > 3:
                filtered_out.append((dk_name, abbr, dp.salary, f"zero_fp",
                                     dk_info.get("fp", 0), dk_info.get("ownership", 0)))
        elif status in ("O", "OUT", "D", "DOUBTFUL"):
            total_injury += 1
        else:
            total_in_pool += 1

print("=" * 100)
print("FILTER SUMMARY (unique players)")
print("=" * 100)
print(f"  In rotation + passes all: {total_in_pool}")
print(f"  Name mismatch (not in rotation): {total_name_miss}")
print(f"  Zero minutes: {total_zero_min}")
print(f"  Low games + low salary: {total_low_games}")
print(f"  Zero FP: {total_zero_fp}")
print(f"  Injury: {total_injury}")
print(f"  TOTAL: {total_in_pool + total_name_miss + total_zero_min + total_low_games + total_zero_fp + total_injury}")

# Show filtered players with >3% DK ownership (significant misses)
filtered_out.sort(key=lambda x: -x[5])
print(f"\n{'=' * 100}")
print(f"HIGH-OWNERSHIP FILTERED PLAYERS (DK own > 3%):")
print(f"{'=' * 100}")
print(f"{'Player':<25} {'Team':>4} {'Sal':>6} {'DK FP':>6} {'DK Own':>6} Filter Reason")
print("-" * 100)
for name, team, sal, reason, dk_fp, dk_own in filtered_out:
    print(f"{name:<25} {team:>4} ${sal:>5} {dk_fp:>5.1f} {dk_own:>5.1f}% {reason}")
