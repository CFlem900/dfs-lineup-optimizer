"""Fetch rebuilt pool from running backend and inspect MEM players + save to file."""
import sys
import json
import httpx

URL = "http://127.0.0.1:8000/api/player-pool"
PARAMS = {
    "platform": "dk",
    "draft_group_id": "143715",
    "game_date": "2026-03-12",
    "sport": "nba",
}

print("Requesting pool rebuild (this may take 3-5 minutes)...")
try:
    r = httpx.get(URL, params=PARAMS, timeout=600)
    r.raise_for_status()
except Exception as e:
    print(f"ERROR: {e}")
    sys.exit(1)

data = r.json()

# Extract players
if isinstance(data, dict):
    players = data.get("players", data.get("pool", []))
    excluded = data.get("excluded", [])
    meta = {k: v for k, v in data.items() if k not in ("players", "pool", "excluded")}
    print(f"Pool: {len(players)} players, {len(excluded)} excluded")
    if meta:
        print(f"Meta: {json.dumps(meta, indent=2, default=str)[:500]}")
elif isinstance(data, list):
    players = data
    excluded = []
    print(f"Pool: {len(players)} players")
else:
    print(f"Unknown format: {type(data)}")
    sys.exit(1)

# Save rebuilt pool to cache file for comparison
out_path = "cache/pool_rebuilt_postfix.json"
with open(out_path, "w", encoding="utf-8") as f:
    json.dump({"players": players, "excluded": excluded}, f)
print(f"Saved rebuilt pool to {out_path}")

# ── MEM deep dive ────────────────────────────────────────────
mem = [p for p in players if p.get("team_abbreviation") == "MEM"]
print(f"\n{'='*70}")
print(f"  MEM Players ({len(mem)} in pool)")
print(f"{'='*70}")
print(f"  {'Name':<25} {'Pos':>5} {'Min':>6} {'FP':>7} {'Salary':>7} {'Source':>14}")
print(f"  {'-'*25} {'-'*5} {'-'*6} {'-'*7} {'-'*7} {'-'*14}")
for p in sorted(mem, key=lambda x: x.get("projected_minutes", 0), reverse=True):
    print(
        f"  {p.get('player_name','?'):<25} "
        f"{p.get('position','?'):>5} "
        f"{p.get('projected_minutes', 0):>6.1f} "
        f"{p.get('projected_fp', 0):>7.1f} "
        f"${p.get('salary', 0):>6} "
        f"{(p.get('projection_source') or ''):>14}"
    )

# ── Key players check ────────────────────────────────────────
key_names = ["Javon Small", "Kyle Kuzma", "CJ McCollum", "Pelle Larsson",
             "Cameron Payne", "Olivier-Maxence Prosper", "Andre Drummond",
             "Jalen Green", "Daniel Gafford", "Ousmane Dieng"]
print(f"\n{'='*70}")
print(f"  KEY PLAYERS CHECK")
print(f"{'='*70}")
print(f"  {'Name':<25} {'Team':>4} {'Pos':>5} {'Min':>6} {'FP':>7} {'Salary':>7}")
print(f"  {'-'*25} {'-'*4} {'-'*5} {'-'*6} {'-'*7} {'-'*7}")
for name in key_names:
    found = [p for p in players if name.lower() in p.get("player_name", "").lower()]
    if found:
        p = found[0]
        print(
            f"  {p.get('player_name','?'):<25} "
            f"{p.get('team_abbreviation','?'):>4} "
            f"{p.get('position','?'):>5} "
            f"{p.get('projected_minutes', 0):>6.1f} "
            f"{p.get('projected_fp', 0):>7.1f} "
            f"${p.get('salary', 0):>6}"
        )
    else:
        print(f"  {name:<25} NOT FOUND")

print(f"\nDone. Run comparison with: python compare_projections_v2.py <ext_csv> cache/pool_rebuilt_postfix.json")
