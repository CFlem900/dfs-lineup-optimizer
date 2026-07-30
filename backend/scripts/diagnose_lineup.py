#!/usr/bin/env python
"""Diagnostic script: build player pool for DG 143408 and inspect key players."""
import sys, os, unicodedata

sys.stdout.reconfigure(encoding="utf-8")
os.chdir(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ".")

from app.api.dependencies import get_services

svc = get_services()
los = svc.lineup_optimizer_service

print("=" * 90)
print("Building player pool for DG 143408, game_date=2026-03-09, platform=dk, sport=nba ...")
print("=" * 90)

pool = los.build_player_pool(
    platform="dk",
    draft_group_id=143408,
    game_date="2026-03-09",
    sport="nba",
)

print(f"\nPool built: {len(pool)} players\n")


# ── Helpers ───────────────────────────────────────────────────────────────
def strip_diacritics(s: str) -> str:
    """Remove diacritical marks for fuzzy name matching."""
    return "".join(
        c for c in unicodedata.normalize("NFD", s)
        if unicodedata.category(c) != "Mn"
    ).lower()


def fmt(p):
    """Return a formatted summary line for a PlayerPoolEntry."""
    value = (p.projected_fp / p.salary * 1000) if p.salary else 0.0
    src = p.projection_source or "rotation"
    return (
        f"  {p.player_name:<24s} {p.team_abbreviation:<4s} "
        f"Pos={p.position:<8s} Salary=${p.salary:>6,d}  "
        f"Proj={p.projected_fp:6.2f}  Value={value:5.2f}  "
        f"Min={p.projected_minutes:5.1f}  "
        f"Conf={p.rotation_confidence:.2f}  "
        f"Src={src}"
    )


# ── 1) Jokic ──────────────────────────────────────────────────────────────
print("=" * 90)
print("1) JOKIC")
print("=" * 90)
jokics = [p for p in pool if "jokic" in strip_diacritics(p.player_name)]
if jokics:
    for p in jokics:
        print(fmt(p))
        print(f"     eligible_slots={p.eligible_slots}  dk_value={p.dk_value}  floor={p.floor_fp:.2f}  ceil={p.ceiling_fp:.2f}")
else:
    print("  *** Jokic NOT FOUND in pool ***")


# ── 2) All MEM players ───────────────────────────────────────────────────
print()
print("=" * 90)
print("2) ALL MEMPHIS (MEM) PLAYERS")
print("=" * 90)
mem = sorted([p for p in pool if p.team_abbreviation == "MEM"],
             key=lambda p: p.projected_fp, reverse=True)
if mem:
    for p in mem:
        print(fmt(p))
else:
    print("  *** No MEM players found ***")


# ── 3) Top 20 by value ──────────────────────────────────────────────────
print()
print("=" * 90)
print("3) TOP 20 PLAYERS BY VALUE (proj / salary * 1000)")
print("=" * 90)
by_value = sorted(pool, key=lambda p: (p.projected_fp / p.salary * 1000) if p.salary else 0, reverse=True)
for i, p in enumerate(by_value[:20], 1):
    print(f"  {i:>2}. {fmt(p)}")


# ── 4) All C-eligible players sorted by projected_fp ────────────────────
print()
print("=" * 90)
print("4) ALL C-ELIGIBLE PLAYERS (sorted by projected_fp)")
print("=" * 90)
centers = sorted(
    [p for p in pool if "C" in p.eligible_slots],
    key=lambda p: p.projected_fp, reverse=True,
)
for i, p in enumerate(centers, 1):
    print(f"  {i:>2}. {fmt(p)}")


# ── 5) All players with salary >= $10,000 sorted by value ───────────────
print()
print("=" * 90)
print("5) HIGH-SALARY PLAYERS (>= $10,000) sorted by value")
print("=" * 90)
expensive = sorted(
    [p for p in pool if p.salary >= 10000],
    key=lambda p: (p.projected_fp / p.salary * 1000) if p.salary else 0, reverse=True,
)
for i, p in enumerate(expensive, 1):
    print(f"  {i:>2}. {fmt(p)}")

print()
print("=" * 90)
print("DONE")
print("=" * 90)
