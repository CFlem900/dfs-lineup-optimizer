"""Check if GameService can return games for 2026-03-04."""
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# Import services
sys.path.insert(0, "C:/Working/Prediction_App/backend")
from app.api.routes import get_services

svc = get_services()

# Check game service
game_svc = svc.game_service
print(f"GameService type: {type(game_svc)}")
print(f"GameService available: {game_svc is not None}")

if game_svc:
    try:
        schedule = game_svc.get_games("2026-03-04")
        print(f"\nSchedule for 2026-03-04:")
        print(f"  Total games: {len(schedule.games)}")
        for g in schedule.games:
            print(f"  {g.home_team.team_abbreviation} vs {g.away_team.team_abbreviation}")
            print(f"    game_id: {g.game_id}")
            print(f"    projected_total: {g.projected_total}")
            print(f"    projected_pace: {g.projected_pace}")
    except Exception as e:
        print(f"  ERROR getting games: {e}")

# Also check what teams the pool has
print(f"\n--- Pool team check ---")
try:
    import requests
    r = requests.get("http://localhost:8000/api/player-pool", params={
        "draft_group_id": 143272, "platform": "dk", "sport": "nba"
    }, timeout=30)
    if r.status_code == 200:
        data = r.json()
        players = data.get("players", [])
        teams = set()
        for p in players:
            teams.add(p.get("team_abbreviation"))
        print(f"  Pool teams: {sorted(teams)}")
        print(f"  Pool size: {len(players)}")
    else:
        print(f"  Pool fetch failed: {r.status_code}")
except Exception as e:
    print(f"  Pool check error: {e}")
