"""Targeted test: build MEM rotation and check if vacancy promotion fires for Javon Small.

This bypasses the full pool builder and calls the rotation engine directly
for a single team, so we can see the vacancy detection in action.
"""
import sys
import os
import logging

sys.path.insert(0, ".")
os.environ.setdefault("SKIP_NBA_API_LIVE", "1")

# Enable detailed logging for top-down allocator
logging.basicConfig(
    level=logging.INFO,
    format="%(name)s | %(message)s",
    stream=sys.stdout,
)
# Focus on relevant loggers
for name in ["app.services.top_down_minutes", "app.services.rotation_engine"]:
    logging.getLogger(name).setLevel(logging.DEBUG)
# Suppress noisy loggers
for name in ["httpx", "httpcore", "urllib3", "app.services.nba_api_service",
             "app.services.balldontlie_service", "app.services.game_service",
             "app.services.nba_data_cache_service"]:
    logging.getLogger(name).setLevel(logging.WARNING)

from app.services.nba_api_service import NBAApiService
from app.services.balldontlie_service import BallDontLieService
from app.services.nba_multi_source import NBAMultiSourceService
from app.services.dk_draftables_service import DKDraftablesService
from app.services.rotation_engine import RotationEngine
from app.config.constants import DK_TO_NBA_ABBR_ALIASES as _DK_ALIASES

print("=" * 70)
print("  MEM VACANCY PROMOTION TEST")
print("=" * 70)

# ── 1. Get MEM draftables ──────────────────────────────────────
dk_svc = DKDraftablesService()
draftables = dk_svc.get_draftables(143715)  # Main slate DG

# Filter to MEM
mem_dk = []
for d in draftables:
    abbr = d.team_abbreviation.upper()
    abbr = _DK_ALIASES.get(abbr, abbr)
    if abbr == "MEM":
        mem_dk.append(d)

print(f"\nMEM draftables ({len(mem_dk)} players):")
for d in sorted(mem_dk, key=lambda x: x.salary, reverse=True):
    status = f" [{d.status}]" if d.status else ""
    print(f"  {d.display_name:<25} {d.position:>5} ${d.salary:>5}{status}")

# Build salary lookup
dk_salary_lookup = {d.display_name.strip(): d.salary for d in mem_dk}
dk_position_lookup = {d.display_name.strip(): d.position for d in mem_dk}
dk_status_lookup = {d.display_name.strip(): (d.status or "") for d in mem_dk}

# ── 2. Build rotation for MEM ──────────────────────────────────
nba_svc = NBAApiService()
bdl_svc = BallDontLieService()
multi_svc = NBAMultiSourceService(nba_svc, bdl_svc)

# MEM team ID
MEM_TEAM_ID = 1610612763

print(f"\nBuilding MEM rotation (team_id={MEM_TEAM_ID})...")
rotation = multi_svc.build_team_rotation(
    team_id=MEM_TEAM_ID,
    draftable_salaries=dk_salary_lookup,
    draftable_positions=dk_position_lookup,
    draftable_statuses=dk_status_lookup,
)

print(f"\nMEM rotation ({len(rotation)} players):")
print(f"  {'Name':<25} {'BDL':>5} {'DK Pos':>7} {'SeasonAvg':>9} {'Last5Avg':>8} {'DK$':>6} {'Trade':>6}")
print(f"  {'-'*25} {'-'*5} {'-'*7} {'-'*9} {'-'*8} {'-'*6} {'-'*6}")
for p in sorted(rotation, key=lambda x: x.season_avg, reverse=True):
    last5 = sum(p.minutes_last_5) / len(p.minutes_last_5) if p.minutes_last_5 else 0
    trade = "TRADE" if p.roster_change_detected else ""
    dk_pos = getattr(p, "dk_position", None) or "?"
    print(
        f"  {p.player_name:<25} {p.position:>5} {dk_pos:>7} "
        f"{p.season_avg:>9.1f} {last5:>8.1f} "
        f"${(p.dk_salary or 0):>5} {trade:>6}"
    )

# ── 3. Get injuries ────────────────────────────────────────────
from app.services.injury_service import InjuryService
inj_svc = InjuryService()
all_injuries = inj_svc.get_team_injuries(MEM_TEAM_ID) or []

# Also build DK injury statuses
dk_injury_statuses = {}
for p in rotation:
    for d in mem_dk:
        if d.display_name.strip().lower() in p.player_name.lower() or p.player_name.lower() in d.display_name.strip().lower():
            if d.status:
                dk_injury_statuses[p.player_id] = d.status
                break

print(f"\nInjuries ({len(all_injuries)}):")
for inj in all_injuries:
    print(f"  {inj.player_name:<25} → {inj.status}")
print(f"\nDK injury statuses: {dk_injury_statuses}")

# ── 4. Run top-down allocator directly ─────────────────────────
from app.services.top_down_minutes import allocate_team_minutes

print(f"\n{'='*70}")
print("  RUNNING TOP-DOWN ALLOCATOR WITH VACANCY DETECTION")
print(f"{'='*70}\n")

result = allocate_team_minutes(
    rotation=rotation,
    injuries=all_injuries,
    dk_injury_statuses=dk_injury_statuses,
    team_name="MEM",
)

print(f"\n{'='*70}")
print("  ALLOCATION RESULTS")
print(f"{'='*70}")
print(f"  {'Name':<25} {'Pos':>5} {'SeasonAvg':>9} {'Allocated':>9} {'Change':>8}")
print(f"  {'-'*25} {'-'*5} {'-'*9} {'-'*9} {'-'*8}")
for p in sorted(rotation, key=lambda x: result.get(x.player_id, 0), reverse=True):
    alloc = result.get(p.player_id, 0)
    change = alloc - p.season_avg if alloc > 0 else 0
    marker = " ★" if p.player_name == "Javon Small" else ""
    print(
        f"  {p.player_name:<25} {p.position:>5} "
        f"{p.season_avg:>9.1f} {alloc:>9.1f} {change:>+8.1f}{marker}"
    )

total = sum(result.values())
print(f"\n  Total: {total:.1f} min (target: 240.0)")

# ── 5. Highlight Javon Small ──────────────────────────────────
small = [p for p in rotation if "small" in p.player_name.lower()]
if small:
    p = small[0]
    alloc = result.get(p.player_id, 0)
    last5 = sum(p.minutes_last_5) / len(p.minutes_last_5) if p.minutes_last_5 else 0
    print(f"\n{'='*70}")
    print(f"  JAVON SMALL DEEP DIVE")
    print(f"{'='*70}")
    print(f"  Position:    {p.position}")
    print(f"  Season avg:  {p.season_avg:.1f} min")
    print(f"  Last 5 avg:  {last5:.1f} min")
    print(f"  Last 5 raw:  {p.minutes_last_5}")
    print(f"  DK salary:   ${p.dk_salary or 'N/A'}")
    print(f"  Trade:       {p.roster_change_detected}")
    print(f"  ALLOCATED:   {alloc:.1f} min")
    print(f"  Target:      32+ min (starter)")
    if alloc >= 28:
        print(f"  ✓ VACANCY PROMOTION WORKED!")
    else:
        print(f"  ✗ VACANCY PROMOTION DID NOT FIRE — investigating...")
        # Check: is there a star PG who is Out?
        from app.services.top_down_minutes import VACANCY_STAR_THRESHOLD
        for pp in rotation:
            if pp.player_id != p.player_id:
                in_injuries = any(i.player_id == pp.player_id and i.status == "Out" for i in all_injuries)
                dk_out = dk_injury_statuses.get(pp.player_id, "").upper() in ("OUT", "O")
                if (in_injuries or dk_out) and pp.season_avg >= VACANCY_STAR_THRESHOLD:
                    print(f"    Found star Out: {pp.player_name} ({pp.position}, avg={pp.season_avg})")
                elif pp.season_avg >= VACANCY_STAR_THRESHOLD:
                    print(f"    Star (not Out): {pp.player_name} ({pp.position}, avg={pp.season_avg})")
        print(f"    Vacancy threshold: season_avg >= {VACANCY_STAR_THRESHOLD}")
