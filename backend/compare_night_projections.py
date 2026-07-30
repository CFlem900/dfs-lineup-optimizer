"""Compare our rotation engine projections vs external DK Data Hub projections.

Usage:
    cd backend && ./venv/Scripts/python compare_night_projections.py
"""

import csv
import sys
import time
import logging
import io

sys.path.insert(0, ".")

# Force UTF-8 stdout to avoid cp1252 UnicodeEncodeError on Windows
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s %(name)s: %(message)s",
    stream=sys.stderr,
)
# Quiet noisy loggers
for noisy in ("httpx", "httpcore", "app.services.http_resilience"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

from datetime import datetime

from app.services.nba_api_service import NBAApiService
from app.services.dfs_service import DFSService
from app.services.balldontlie_service import BallDontLieService
from app.services.nba_multi_source import NBAMultiSourceService
from app.services.rotation_engine import RotationEngine
from app.models.player import PlayerProjection, PlayerStatus
from app.utils.helpers import normalize_player_name
from nba_api.stats.static import teams as nba_teams

# ── 1. Parse external projections ──────────────────────────────────
EXT_CSV = r"C:\Users\CFlem\Downloads\DK_NBA_Night_Data_Hub_Projections (6).csv"

ext_projs = {}  # name -> {salary, position, team, minutes, projection, ...}
teams_in_slate = set()

with open(EXT_CSV, "r", encoding="utf-8-sig") as f:
    reader = csv.DictReader(f)
    for row in reader:
        name = row["Player"].strip()
        team = row["Team"].strip()
        proj = float(row["Projection"]) if row["Projection"] else 0.0
        mins = float(row["Minutes"]) if row["Minutes"] else 0.0
        salary = int(row["Salary"]) if row["Salary"] else 0
        position = row["Position"].strip()
        starting = row.get("Starting", "").strip().lower() == "true"
        injury = row.get("Injury", "").strip()

        ext_projs[name] = {
            "salary": salary,
            "position": position,
            "team": team,
            "minutes": mins,
            "projection": proj,
            "starting": starting,
            "injury": injury,
            "ownership": float(row["Ownership %"]) if row["Ownership %"] else 0,
            "ceiling": float(row["Ceiling"]) if row["Ceiling"] else 0,
            "floor": float(row["Floor"]) if row["Floor"] else 0,
            "value": float(row["Value"]) if row["Value"] else 0,
        }
        if team:
            teams_in_slate.add(team)

print(f"\n{'='*80}")
print(f"PROJECTION COMPARISON: Our Engine vs External (DK Data Hub)")
print(f"{'='*80}")
print(f"External source: {len(ext_projs)} players across {sorted(teams_in_slate)}")
print(f"Players with projections: {sum(1 for v in ext_projs.values() if v['projection'] > 0)}")
print(f"Players out/unknown: {sum(1 for v in ext_projs.values() if v['projection'] == 0)}")

# ── 1b. Derive injury statuses from DK CSV ────────────────────────
# Players with Projection=0 and Minutes=0 are effectively OUT.
team_injuries = {}  # team_abbr -> List[PlayerStatus]
for name, info in ext_projs.items():
    if info["projection"] == 0 and info["minutes"] == 0:
        status = PlayerStatus(
            player_id=0,  # _match_injuries_to_roster matches by name
            player_name=name,
            status="Out",
            injury_description=info.get("injury", ""),
        )
        team_injuries.setdefault(info["team"], []).append(status)

all_injuries_flat = [inj for lst in team_injuries.values() for inj in lst]

print(f"\nInjury report (derived from DK CSV):")
for team_abbr in sorted(team_injuries):
    out_names = [inj.player_name for inj in team_injuries[team_abbr]]
    print(f"  {team_abbr}: {len(out_names)} OUT -> {', '.join(out_names)}")

# ── 2. Build team_id lookup ────────────────────────────────────────
ABBR_TO_ID = {t["abbreviation"]: t["id"] for t in nba_teams.get_teams()}

# ── 3. Run our rotation engine for each team ───────────────────────
bdl = BallDontLieService()
nba_api_svc = NBAApiService()
data_svc = NBAMultiSourceService(nba_api=nba_api_svc, balldontlie=bdl)
dfs_service = DFSService()
engine = RotationEngine()

# Build DK draftable info per team
team_dk_names = {}   # team_abbr -> set of names
team_dk_salaries = {}  # team_abbr -> {name: salary}
team_dk_positions = {}  # team_abbr -> {name: position}
team_dk_statuses = {}  # team_abbr -> {name: status}

for name, info in ext_projs.items():
    team = info["team"]
    team_dk_names.setdefault(team, set()).add(name)
    team_dk_salaries.setdefault(team, {})[name] = info["salary"]
    team_dk_positions.setdefault(team, {})[name] = info["position"]
    if info["injury"] and info["injury"] != "None":
        team_dk_statuses.setdefault(team, {})[name] = info["injury"]

our_projs = {}  # name -> {minutes, dk_points, stats...}

print(f"\nRunning our rotation engine for {len(teams_in_slate)} teams...")
print("-" * 60)

for team_abbr in sorted(teams_in_slate):
    team_id = ABBR_TO_ID.get(team_abbr)
    if not team_id:
        print(f"  SKIP {team_abbr}: no NBA team_id found")
        continue

    dk_names = team_dk_names.get(team_abbr, set())
    dk_salaries = team_dk_salaries.get(team_abbr, {})
    dk_positions = team_dk_positions.get(team_abbr, {})
    dk_statuses = team_dk_statuses.get(team_abbr, {})

    t0 = time.time()
    try:
        rotation = data_svc.build_team_rotation(
            team_id=team_id,
            draftable_names=dk_names,
            draftable_salaries=dk_salaries,
            draftable_positions=dk_positions,
            draftable_statuses=dk_statuses,
        )
    except Exception as e:
        print(f"  ERROR {team_abbr}: rotation build failed: {e}")
        continue

    elapsed = time.time() - t0

    if not rotation:
        print(f"  WARN {team_abbr}: empty rotation returned ({elapsed:.1f}s)")
        continue

    # Build injury-adjusted TeamRotation via the rotation engine
    injuries = team_injuries.get(team_abbr, [])

    # Build dk_injury_statuses dict (player_id -> DK status string)
    dk_inj_by_id = {}
    for p in rotation:
        pn = normalize_player_name(p.player_name)
        for inj in injuries:
            if normalize_player_name(inj.player_name) == pn:
                dk_inj_by_id[p.player_id] = "OUT"

    team_rotation = engine.project_team_rotation(
        team_id=team_id,
        team_name=team_abbr,
        rotation=rotation,
        injuries=injuries,
        game_date="2026-03-10",
        apply_coach_adjustments=False,  # isolate injury effect
        all_injuries=all_injuries_flat,
        dk_injury_statuses=dk_inj_by_id,
    )

    team_dfs = dfs_service.project_team_dfs(
        team_rotation=team_rotation,
        rotation=rotation,
    )

    # Print team summary with injury info
    n_out = len(injuries)
    player_count = len(team_dfs.projections)
    team_total = team_dfs.team_dk_total
    out_str = f", {n_out} OUT" if n_out else ""
    print(f"  {team_abbr}: {player_count} players, team DK total={team_total:.1f}{out_str} ({elapsed:.1f}s)")

    # Show key redistributions for OUT players
    if injuries:
        mins_map = {p.player_name: p.adjusted_minutes for p in team_rotation.projections}
        baseline_map = {p.player_name: p.baseline_minutes for p in team_rotation.projections}
        for inj in injuries:
            matched = [p for p in team_rotation.projections
                       if normalize_player_name(p.player_name) == normalize_player_name(inj.player_name)]
            if matched:
                print(f"    {inj.player_name} OUT: {matched[0].baseline_minutes:.1f} min zeroed")
        # Show who absorbed minutes (biggest gains)
        gains = [(p.player_name, p.adjusted_minutes - p.baseline_minutes)
                 for p in team_rotation.projections
                 if p.adjusted_minutes > p.baseline_minutes + 1.0]
        gains.sort(key=lambda x: x[1], reverse=True)
        for name, gain in gains[:5]:
            adj = mins_map[name]
            base = baseline_map[name]
            print(f"    {name}: {base:.1f} -> {adj:.1f} min (+{gain:.1f})")

    for proj in team_dfs.projections:
        our_projs[proj.player_name] = {
            "minutes": proj.projected_minutes,
            "dk_points": proj.dk_points,
            "dk_floor": proj.dk_floor,
            "dk_ceiling": proj.dk_ceiling,
            "pts": proj.pts,
            "reb": proj.reb,
            "ast": proj.ast,
        }

# ── 4. Compare side-by-side ────────────────────────────────────────
print(f"\n{'='*80}")
print(f"SIDE-BY-SIDE COMPARISON (sorted by external projection desc)")
print(f"{'='*80}")
print(
    f"{'Player':<25s} {'Team':>4s} {'Sal':>6s} "
    f"{'Ext Min':>7s} {'Our Min':>7s} "
    f"{'Ext Proj':>8s} {'Our Proj':>8s} {'Diff':>7s} {'%Diff':>6s}"
)
print("-" * 95)

# Only compare players with external projections > 0
compare_rows = []

for ext_name, ext_info in ext_projs.items():
    if ext_info["projection"] <= 0:
        continue

    # Try exact match first, then normalized match
    our = our_projs.get(ext_name)
    if not our:
        ext_norm = normalize_player_name(ext_name)
        for our_name, our_info in our_projs.items():
            if normalize_player_name(our_name) == ext_norm:
                our = our_info
                break

    compare_rows.append((ext_name, ext_info, our))

# Sort by external projection descending
compare_rows.sort(key=lambda x: x[1]["projection"], reverse=True)

total_ext = 0
total_ours = 0
matched = 0
big_diffs = []

for ext_name, ext_info, our in compare_rows:
    ext_proj = ext_info["projection"]
    ext_mins = ext_info["minutes"]
    team = ext_info["team"]
    salary = ext_info["salary"]

    if our:
        our_proj = our["dk_points"]
        our_mins = our["minutes"]
        diff = our_proj - ext_proj
        pct_diff = (diff / ext_proj * 100) if ext_proj > 0 else 0
        total_ext += ext_proj
        total_ours += our_proj
        matched += 1

        flag = ""
        if abs(pct_diff) > 20:
            flag = " ***"
            big_diffs.append((ext_name, team, ext_proj, our_proj, diff, pct_diff))

        print(
            f"{ext_name:<25s} {team:>4s} ${salary:>5d} "
            f"{ext_mins:>7.1f} {our_mins:>7.1f} "
            f"{ext_proj:>8.1f} {our_proj:>8.1f} {diff:>+7.1f} {pct_diff:>+5.1f}%{flag}"
        )
    else:
        print(
            f"{ext_name:<25s} {team:>4s} ${salary:>5d} "
            f"{ext_mins:>7.1f} {'--':>7s} "
            f"{ext_proj:>8.1f} {'--':>8s} {'--':>7s} {'--':>6s}  (not in our pool)"
        )

# ── 5. Summary stats ──────────────────────────────────────────────
print(f"\n{'='*80}")
print("SUMMARY")
print(f"{'='*80}")
print(f"Players compared:     {matched}/{len(compare_rows)} "
      f"({len(compare_rows) - matched} missing from our engine)")
if matched > 0:
    avg_ext = total_ext / matched
    avg_ours = total_ours / matched
    avg_diff = (total_ours - total_ext) / matched
    print(f"Avg external proj:    {avg_ext:.1f} FPPG")
    print(f"Avg our projection:   {avg_ours:.1f} FPPG")
    print(f"Avg difference:       {avg_diff:+.1f} FPPG ({avg_diff/avg_ext*100:+.1f}%)")
    print(f"Total ext slate:      {total_ext:.1f}")
    print(f"Total our slate:      {total_ours:.1f}")

if big_diffs:
    print(f"\nLARGE DISCREPANCIES (>20% difference):")
    print(f"{'Player':<25s} {'Team':>4s} {'Ext':>7s} {'Ours':>7s} {'Diff':>7s} {'%Diff':>6s}")
    print("-" * 60)
    big_diffs.sort(key=lambda x: abs(x[5]), reverse=True)
    for name, team, ext_p, our_p, diff, pct in big_diffs:
        print(f"{name:<25s} {team:>4s} {ext_p:>7.1f} {our_p:>7.1f} {diff:>+7.1f} {pct:>+5.1f}%")
