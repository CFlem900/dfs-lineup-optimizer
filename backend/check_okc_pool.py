import json, sys
sys.stdout.reconfigure(encoding="utf-8")

data = json.load(open("v15b_lineups.json", encoding="utf-8"))

# Find all OKC players in the lineups
okc_players = {}
for lu in data["lineups"]:
    for p in lu["players"]:
        if p.get("team_abbreviation") == "OKC":
            name = p.get("display_name") or p.get("player_name", "?")
            if name not in okc_players:
                okc_players[name] = p

print("OKC players in pool (appearing in lineups):")
for name in sorted(okc_players.keys()):
    p = okc_players[name]
    src = p.get("projection_source", "rotation")
    print(f"  {name:25s} FP={p['projected_fp']:5.1f} min={p['projected_minutes']:5.1f} sal=${p['salary']:5d} src={src}")

# Count OKC players
print(f"\nTotal OKC players in lineups: {len(okc_players)}")

# Check all teams
teams = {}
for lu in data["lineups"]:
    for p in lu["players"]:
        t = p.get("team_abbreviation", "?")
        if t not in teams:
            teams[t] = set()
        name = p.get("display_name") or p.get("player_name", "?")
        teams[t].add(name)

print(f"\nAll teams and player counts:")
for t in sorted(teams.keys()):
    print(f"  {t}: {len(teams[t])} players")
