import requests, json, sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

r = requests.get("http://localhost:8000/api/dk/draft-groups")
print(f"Status: {r.status_code}")
data = r.json()
print(f"Type: {type(data)}")
if isinstance(data, dict):
    print(json.dumps(data, indent=2)[:2000])
elif isinstance(data, list):
    for dg in data[:20]:
        print(f"  DG={dg.get('draft_group_id')} sport={dg.get('sport')} games={dg.get('game_count','')} type={dg.get('contest_type','')}")
