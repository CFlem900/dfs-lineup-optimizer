"""Test: Generate lineups with logging to a file to capture ILP diagnostics."""
import requests
import json
import sys
import io
import logging
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# Set up a log handler that captures our app's log output
log_file = "scripts/ilp_diagnostics.log"
# Clear the file
with open(log_file, "w") as f:
    f.write("")

# Configure the root logger to capture app logs
handler = logging.FileHandler(log_file, encoding="utf-8")
handler.setLevel(logging.DEBUG)
handler.setFormatter(logging.Formatter("%(name)s - %(levelname)s - %(message)s"))
logging.getLogger("app").addHandler(handler)

API = "http://localhost:8000/api"
DGID = 142804

payload = {
    "platform": "dk",
    "sport": "nba",
    "draft_group_id": DGID,
    "num_lineups": 3,
    "contest_type": "gpp",
    "strategy": "max_projection",
    "optimality_threshold": 0.90,
}

print(f"Generating 3 GPP lineups ...")
try:
    resp = requests.post(f"{API}/generate-lineups", json=payload, timeout=300)
    data = resp.json()
except Exception as e:
    print(f"Request failed: {e}")
    sys.exit(1)

if resp.status_code != 200:
    print(f"ERROR ({resp.status_code}): {json.dumps(data, indent=2)[:2000]}")
    sys.exit(1)

baseline = data.get("baseline_projection_score", "?")
lineups = data.get("lineups", [])
fps = [sum(p.get("projected_fp", 0) for p in lu.get("players", [])) for lu in lineups]

print(f"Baseline: {baseline}")
print(f"Lineup FPs: {[f'{f:.1f}' for f in fps]}")
print(f"Best: {max(fps):.1f} ({max(fps)/float(baseline)*100:.1f}% of baseline)")
print(f"\n(Server logs would show ILP status in the uvicorn console)")
print(f"Since we can't capture those, let me add inline diagnostics...")
