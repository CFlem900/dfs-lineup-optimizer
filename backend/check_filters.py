"""Check exactly why each target player gets filtered from the pool."""
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

target_checks = [
    ("Kel'el Ware", "MIA"), ("Dyson Daniels", "ATL"),
    ("Trendon Watford", "PHI"), ("Andre Drummond", "PHI"),
    ("Scotty Pippen Jr.", "MEM"), ("Ousmane Dieng", "MIL"),
    ("Adem Bona", "PHI"), ("Carter Bryant", "SAS"),
    ("Collin Gillespie", "PHX"), ("VJ Edgecombe", "PHI"),
    ("Kelly Oubre Jr.", "PHI"), ("Ty Jerome", "MEM"),
    ("Drake Powell", "BKN"), ("Cam Thomas", "MIL"),
]

print("=" * 140)
print(f"{'Player':<25} {'Team':>4} {'DK$':>6} {'DK Status':>10} {'Rot Min':>8} {'Rot FP':>7} {'Games':>6} {'Filter Reason'}")
print("=" * 140)

for dk_name, team in target_checks:
    tid = team_id_map[team]

    # DK info
    dk_match = [p for p in all_dk if dk_name.lower() in p.display_name.lower() and p.team_abbreviation == team]
    dk_sal = dk_match[0].salary if dk_match else 0
    dk_status = (dk_match[0].status or "None") if dk_match else "?"

    # Rotation info
    try:
        r = requests.get(f"{BASE}/api/teams/{tid}/rotation",
                         params={"draft_group_id": 143420, "sport": "nba"},
                         timeout=120)
        rot_data = r.json()
    except:
        print(f"{dk_name:<25} {team:>4} ${dk_sal:>5} {'TIMEOUT':>10}")
        continue

    rot_projs = rot_data.get("projections", [])

    # Find this player in rotation
    rot_player = None
    from app.services.lineup_optimizer_service import LineupOptimizerService
    for p in rot_projs:
        if LineupOptimizerService._names_match(dk_name, p["player_name"]):
            rot_player = p
            break

    if not rot_player:
        print(f"{dk_name:<25} {team:>4} ${dk_sal:>5} {dk_status:>10} {'N/A':>8} {'N/A':>7} {'N/A':>6} NOT IN ROTATION")
        continue

    adj_min = rot_player.get("adjusted_minutes", 0)
    dk_pts = rot_player.get("dk_points", 0)
    games_played = len(rot_player.get("minutes_last_10", []))

    # Check each filter condition
    filter_reason = "PASSES ALL ✓"

    # Filter 1: zero minutes
    if adj_min <= 0:
        filter_reason = f"ZERO MINUTES (adj_min={adj_min:.1f})"
    # Filter 2: low games + low salary
    elif games_played < 3:
        if dk_sal >= 3600:
            filter_reason = f"LOW GAMES ({games_played}) but salary ${dk_sal} >= $3600 → INCLUDED"
        elif adj_min >= 12.0:
            filter_reason = f"LOW GAMES ({games_played}) but min={adj_min:.1f} >= 12 → INCLUDED"
        else:
            filter_reason = f"LOW GAMES ({games_played}) + min salary → EXCLUDED"
    # Filter 3: zero FP
    elif dk_pts <= 0:
        filter_reason = f"ZERO FP (dk_pts={dk_pts:.1f})"
    # Filter 4: injury status
    elif dk_status in ("O", "OUT"):
        filter_reason = f"INJURY OUT (status={dk_status})"
    elif dk_status in ("D", "DOUBTFUL"):
        filter_reason = f"INJURY DOUBTFUL (status={dk_status})"

    print(f"{dk_name:<25} {team:>4} ${dk_sal:>5} {dk_status:>10} {adj_min:>7.1f} {dk_pts:>6.1f} {games_played:>6} {filter_reason}")

# Now check how many of the 173 pool vs 806 DK are being lost at each stage
print("\n\n" + "=" * 80)
print("AGGREGATE FILTER ANALYSIS")
print("=" * 80)

# For each team, count how many DK players pass each filter
total_dk = 0
total_in_rotation = 0
total_zero_min = 0
total_low_games = 0
total_zero_fp = 0
total_injury = 0
total_pass = 0

for abbr in sorted(team_id_map.keys()):
    tid = team_id_map[abbr]
    try:
        r = requests.get(f"{BASE}/api/teams/{tid}/rotation",
                         params={"draft_group_id": 143420, "sport": "nba"},
                         timeout=120)
        rot_data = r.json()
    except:
        continue

    rot_projs = rot_data.get("projections", [])
    dk_team = [p for p in all_dk if p.team_abbreviation == abbr]

    # Match DK to rotation
    matched = 0
    zero_min = 0
    low_games = 0
    zero_fp = 0
    injury = 0
    passed = 0

    for dp in dk_team:
        total_dk += 1
        # Find match
        rp = None
        for p in rot_projs:
            if LineupOptimizerService._names_match(dp.display_name, p["player_name"]):
                rp = p
                break

        if not rp:
            continue

        matched += 1
        total_in_rotation += 1

        adj_min = rp.get("adjusted_minutes", 0)
        dk_pts = rp.get("dk_points", 0)
        gp = len(rp.get("minutes_last_10", []))
        status = (dp.status or "").strip().upper()

        if adj_min <= 0:
            zero_min += 1
            total_zero_min += 1
        elif gp < 3 and dp.salary < 3600 and adj_min < 12.0:
            low_games += 1
            total_low_games += 1
        elif dk_pts <= 0:
            zero_fp += 1
            total_zero_fp += 1
        elif status in ("O", "OUT", "D", "DOUBTFUL"):
            injury += 1
            total_injury += 1
        else:
            passed += 1
            total_pass += 1

    unmatched = len(dk_team) - matched
    print(f"{abbr}: DK={len(dk_team)} matched={matched} unmatched={unmatched} | zero_min={zero_min} low_games={low_games} zero_fp={zero_fp} injury={injury} → passed={passed}")

print(f"\nTOTAL: DK={total_dk} in_rotation={total_in_rotation} | zero_min={total_zero_min} low_games={total_low_games} zero_fp={total_zero_fp} injury={total_injury} → passed={total_pass}")
print(f"Pool should be ~{total_pass} players (actual: 173)")
print(f"Unmatched (no rotation): {total_dk - total_in_rotation}")
