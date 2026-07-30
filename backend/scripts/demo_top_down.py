"""Demo: Top-Down Minute Allocator — Starter's Squeeze.

Shows how the allocator handles:
1. A healthy 10-man rotation (no bench bloat)
2. A primary PG ruled Out → backup promoted to 30+ minutes
3. Deep bench players correctly get 0 minutes

Simulates a New Orleans Pelicans scenario where the starting PG
is Out, and Killian Hayes (13.3 min season avg bench player)
inherits the starting role.
"""

import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    stream=sys.stdout,
)

from app.models.player import PlayerMinutes, PlayerStatus
from app.services.top_down_minutes import allocate_team_minutes

# ── Build a realistic 13-man rotation (NOP-like) ─────────────────────
def make_player(pid, name, pos, season_avg, last5=None, dnp=0, salary=None):
    last5 = last5 or [season_avg] * 5
    return PlayerMinutes(
        player_id=pid,
        player_name=name,
        position=pos,
        team_id=1610612740,
        minutes_last_5=last5,
        minutes_last_10=[season_avg] * 10,
        season_avg=season_avg,
        usage_rate=0.20,
        recent_dnp_streak=dnp,
        dk_salary=salary,
        pts_per_min=0.5, reb_per_min=0.2, ast_per_min=0.15,
        stl_per_min=0.03, blk_per_min=0.02, tov_per_min=0.05,
        fg3m_per_min=0.08,
    )


rotation = [
    # Starters
    make_player(1, "Dejounte Murray",  "PG", 30.0, salary=6900),
    make_player(2, "Trey Murphy III",  "SG", 34.6, salary=7600),
    make_player(3, "Brandon Ingram",   "SF", 34.2, salary=8200),
    make_player(4, "Zion Williamson",  "PF", 31.9, salary=7500),
    make_player(5, "Jeremiah Robinson-Earl", "C", 27.0, salary=4000),
    # Bench rotation (6th-9th man)
    make_player(6, "Killian Hayes",    "PG", 13.3, salary=3100),
    make_player(7, "Herbert Jones",    "SF", 21.7, salary=4800),
    make_player(8, "EJ Harkless",      "SG", 18.2, salary=3700),
    make_player(9, "Daeqwon Plowden",  "PF", 15.0, salary=4300),
    # Deep bench (10th-13th man) — should get 0
    make_player(10, "Jordan Hawkins",  "SG",  5.0, salary=3000),
    make_player(11, "Jaylen Nowell",   "PG",  3.0, salary=3000),
    make_player(12, "Karlo Matkovic",  "C",   2.0, salary=3000),
    make_player(13, "Matt Ryan",       "SF",  1.0, salary=3000),
]


# ══════════════════════════════════════════════════════════════════════
# SCENARIO 1: Fully Healthy Team
# ══════════════════════════════════════════════════════════════════════
print("=" * 70)
print("  SCENARIO 1: Fully Healthy Team")
print("=" * 70)

alloc1 = allocate_team_minutes(
    rotation=rotation,
    injuries=[],
    dk_injury_statuses={},
    rotation_depth=9,
    team_name="NOP (healthy)",
)

print(f"\n{'Player':<25s} {'Pos':>3s} {'SeasonAvg':>9s} {'Allocated':>9s} {'Role':>8s}")
print("-" * 60)
total = 0
for p in rotation:
    mins = alloc1[p.player_id]
    total += mins
    role = "STARTER" if mins >= 28 else ("BENCH" if mins > 0 else "DNP")
    flag = ""
    if mins == 0 and p.season_avg > 0:
        flag = " ← ZEROED"
    print(f"{p.player_name:<25s} {p.position:>3s} {p.season_avg:>9.1f} {mins:>9.1f} {role:>8s}{flag}")
print(f"\n  TOTAL: {total:.1f} min (target: 240.0)")


# ══════════════════════════════════════════════════════════════════════
# SCENARIO 2: Starting PG (Dejounte Murray) is OUT
# Killian Hayes should be PROMOTED to 30+ minutes
# ══════════════════════════════════════════════════════════════════════
print(f"\n{'=' * 70}")
print("  SCENARIO 2: Dejounte Murray OUT → Killian Hayes PROMOTED")
print("=" * 70)

injuries2 = [
    PlayerStatus(
        player_id=1,
        player_name="Dejounte Murray",
        status="Out",
        injury_description="Ankle sprain",
    ),
]

alloc2 = allocate_team_minutes(
    rotation=rotation,
    injuries=injuries2,
    dk_injury_statuses={1: "OUT"},
    rotation_depth=9,
    team_name="NOP (Murray OUT)",
)

print(f"\n{'Player':<25s} {'Pos':>3s} {'SeasonAvg':>9s} {'Allocated':>9s} {'Role':>8s} {'Change':>10s}")
print("-" * 70)
total2 = 0
for p in rotation:
    mins = alloc2[p.player_id]
    total2 += mins
    old_mins = alloc1[p.player_id]
    delta = mins - old_mins
    role = "STARTER" if mins >= 28 else ("BENCH" if mins > 0 else "DNP")
    flag = ""
    if p.player_name == "Killian Hayes" and mins >= 28:
        flag = " ← PROMOTED!"
    if p.player_name == "Dejounte Murray":
        flag = " ← OUT"
    delta_str = f"{delta:+.1f}" if delta != 0 else ""
    print(
        f"{p.player_name:<25s} {p.position:>3s} {p.season_avg:>9.1f} "
        f"{mins:>9.1f} {role:>8s} {delta_str:>10s}{flag}"
    )
print(f"\n  TOTAL: {total2:.1f} min (target: 240.0)")


# ══════════════════════════════════════════════════════════════════════
# COMPARISON: Old system vs New system for Hayes
# ══════════════════════════════════════════════════════════════════════
print(f"\n{'=' * 70}")
print("  KEY COMPARISON: Killian Hayes")
print("=" * 70)
hayes_old_healthy = 13.3  # season_avg baseline in old system
hayes_old_murray_out = 16.3  # after redistribution + normalization (from DK comparison)
hayes_new_healthy = alloc1[6]
hayes_new_murray_out = alloc2[6]

print(f"  Old system (bottom-up):")
print(f"    Healthy:     {hayes_old_healthy:.1f} min (season avg anchors baseline)")
print(f"    Murray OUT:  {hayes_old_murray_out:.1f} min (redistribution helps, but capped)")
print(f"  New system (top-down):")
print(f"    Healthy:     {hayes_new_healthy:.1f} min (bench allocation)")
print(f"    Murray OUT:  {hayes_new_murray_out:.1f} min (PROMOTED to starter!)")
print(f"    Improvement: {hayes_new_murray_out - hayes_old_murray_out:+.1f} min")

print()
