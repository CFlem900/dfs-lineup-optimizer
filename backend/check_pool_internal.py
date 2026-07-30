"""
Check internal pool data (dk_fppg, estimated_ownership, projection_source)
by reading directly from the pool cache diagnostics endpoint.
"""
import sys, io, json, requests

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

BASE = "http://localhost:8000/api"
DG = 143272

def main():
    # Use the diagnostics endpoint which returns more detail
    print("Fetching pool diagnostics...")
    r = requests.get(f"{BASE}/player-pool/diagnostics", params={
        "draft_group_id": DG, "platform": "dk", "sport": "nba"
    }, timeout=30)
    print(f"  Status: {r.status_code}")
    if r.status_code == 200:
        data = r.json()
        print(f"  Keys: {list(data.keys())}")
        print(json.dumps(data, indent=2, default=str)[:3000])
    else:
        print(f"  {r.text[:500]}")

    # Also try the pool endpoint but look at raw response
    print("\n\nFetching pool (checking full player fields)...")
    r = requests.get(f"{BASE}/player-pool", params={
        "draft_group_id": DG, "platform": "dk", "sport": "nba"
    }, timeout=300)
    print(f"  Status: {r.status_code}")
    if r.status_code == 200:
        data = r.json()
        players = data.get("players", [])
        print(f"  Total players: {len(players)}")

        # Check first 5 players for key fields
        target_names = ["Shai Gilgeous-Alexander", "Walter Clayton Jr.", "Quentin Grimes",
                        "Cam Spencer", "Javon Small", "Trendon Watford"]
        print("\n  KEY PLAYERS DETAIL:")
        for p in players:
            name = p.get("player_name", "")
            if name in target_names:
                print(f"\n  {name}:")
                print(f"    salary: {p.get('salary')}")
                print(f"    projected_fp: {p.get('projected_fp')}")
                print(f"    dk_fppg: {p.get('dk_fppg')}")
                print(f"    estimated_ownership: {p.get('estimated_ownership')}")
                print(f"    projection_source: {p.get('projection_source')}")
                print(f"    team: {p.get('team_abbreviation')}")
                print(f"    floor_fp: {p.get('floor_fp')}")
                print(f"    ceiling_fp: {p.get('ceiling_fp')}")
                print(f"    game_total: {p.get('game_total')}")
                print(f"    implied_team_total: {p.get('implied_team_total')}")
                print(f"    boom_probability: {p.get('boom_probability')}")
                print(f"    game_id: {p.get('game_id')}")
                # Show ALL keys for this player
                print(f"    ALL keys: {sorted(p.keys())}")

if __name__ == "__main__":
    main()
