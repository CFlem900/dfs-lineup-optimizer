"""
Analyze DraftKings NBA Sims Lineups CSV.

Parses all lineups, computes player exposure rates, identifies stacking
patterns (teammate pairs/trios), counts unique players, and flags notable
patterns like punt-play usage.
"""

import csv
import re
from collections import Counter
from itertools import combinations
from pathlib import Path

CSV_PATH = Path(r"C:\Users\CFlem\Downloads\DK_NBA_Night_Pre_Contest_Sims_Lineups (5).csv")

# ── Known NBA team rosters (players in this slate mapped to teams) ──────────
# We infer teams from the player names present in the data.
PLAYER_TEAMS = {
    # LAL
    "LeBron James": "LAL",
    "Austin Reaves": "LAL",
    "Rui Hachimura": "LAL",
    "Jaxson Hayes": "LAL",
    "Russell Westbrook": "LAL",  # currently on a roster, adjust if needed
    # PHX
    "Devin Booker": "PHX",
    "Ryan Dunn": "PHX",
    "Oso Ighodaro": "PHX",
    # DAL
    "Luka Doncic": "DAL",
    "Nique Clifford": "DAL",
    # NOP
    "Zion Williamson": "NOP",
    "Trey Murphy III": "NOP",
    "Herbert Jones": "NOP",
    "Yves Missi": "NOP",
    "Dejounte Murray": "NOP",
    "Jeremiah Fears": "NOP",
    "Devin Carter": "NOP",
    # CHA
    "Mark Williams": "CHA",
    "Jake LaRavia": "CHA",  # traded or current
    "Saddiq Bey": "CHA",  # adjust if needed
    # HOU
    "Jalen Green": "HOU",
    # SAC
    "Malik Monk": "SAC",
    "Deandre Ayton": "SAC",
    "DeMar DeRozan": "SAC",
    "Collin Gillespie": "SAC",
    # DEN
    "Maxime Raynaud": "DEN",
    "Daeqwon Plowden": "DEN",
    # OKC
    "Precious Achiuwa": "OKC",  # adjust per trade deadline
    # MEM
    "Marcus Smart": "MEM",
    "Luke Kennard": "MEM",
    "Jake LaRavia": "MEM",
    "Saddiq Bey": "MEM",  # will be overridden by last assignment
    # misc
    "Grayson Allen": "PHX",
    "Royce O'Neale": "PHX",
    "Patrick Baldwin Jr.": "DAL",
    "Drew Eubanks": "PHX",
    "Derik Queen": "HOU",
}

# Fix conflicts -- use the most likely 2025-26 roster assignment
PLAYER_TEAMS["Jake LaRavia"] = "MEM"
PLAYER_TEAMS["Saddiq Bey"] = "DEN"  # adjust as needed


def strip_dk_id(raw: str) -> str:
    """Remove DraftKings player ID in parentheses, e.g. 'LeBron James (42163698)' -> 'LeBron James'."""
    return re.sub(r"\s*\(\d+\)", "", raw).strip()


def load_lineups(path: Path) -> list[list[str]]:
    """Load CSV and return a list of lineups, each lineup is a list of clean player names."""
    lineups = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader)  # skip header row
        for row in reader:
            if not row or not row[0].strip():
                continue
            players = [strip_dk_id(cell) for cell in row]
            lineups.append(players)
    return lineups


def compute_exposure(lineups: list[list[str]]) -> list[tuple[str, int, float]]:
    """Return [(player, count, pct)] sorted descending by count."""
    counter: Counter = Counter()
    for lu in lineups:
        for p in lu:
            counter[p] += 1
    total = len(lineups)
    return sorted(
        [(name, cnt, round(cnt / total * 100, 1)) for name, cnt in counter.items()],
        key=lambda x: -x[1],
    )


def compute_pair_stacks(lineups: list[list[str]], top_n: int = 20) -> list[tuple[tuple[str, str], int, float]]:
    """Find most common teammate pairs across lineups."""
    pair_counter: Counter = Counter()
    total = len(lineups)
    for lu in lineups:
        # Group players by team
        team_groups: dict[str, list[str]] = {}
        for p in lu:
            team = PLAYER_TEAMS.get(p)
            if team:
                team_groups.setdefault(team, []).append(p)
        # Count all pairs of teammates
        for team, members in team_groups.items():
            if len(members) >= 2:
                for pair in combinations(sorted(members), 2):
                    pair_counter[pair] += 1
    return [
        (pair, cnt, round(cnt / total * 100, 1))
        for pair, cnt in pair_counter.most_common(top_n)
    ]


def compute_trio_stacks(lineups: list[list[str]], top_n: int = 15) -> list[tuple[tuple[str, ...], int, float]]:
    """Find most common teammate trios across lineups."""
    trio_counter: Counter = Counter()
    total = len(lineups)
    for lu in lineups:
        team_groups: dict[str, list[str]] = {}
        for p in lu:
            team = PLAYER_TEAMS.get(p)
            if team:
                team_groups.setdefault(team, []).append(p)
        for team, members in team_groups.items():
            if len(members) >= 3:
                for trio in combinations(sorted(members), 3):
                    trio_counter[trio] += 1
    return [
        (trio, cnt, round(cnt / total * 100, 1))
        for trio, cnt in trio_counter.most_common(top_n)
    ]


def detect_punt_players(exposure: list[tuple[str, int, float]], threshold_pct: float = 15.0) -> list[tuple[str, float]]:
    """
    Flag likely punt/value plays that appear at high exposure.
    Punt players are cheap, low-ceiling guys used to enable stars.
    We flag anyone with exposure >= threshold who is NOT a star-tier name.
    """
    STAR_NAMES = {
        "LeBron James", "Luka Doncic", "Devin Booker", "Zion Williamson",
        "Dejounte Murray", "Austin Reaves", "Trey Murphy III", "Jalen Green",
        "Malik Monk", "DeMar DeRozan",
    }
    punts = []
    for name, _, pct in exposure:
        if pct >= threshold_pct and name not in STAR_NAMES:
            punts.append((name, pct))
    return punts


def main():
    lineups = load_lineups(CSV_PATH)
    total_lineups = len(lineups)
    print("=" * 72)
    print(f"  DraftKings NBA Sims Lineup Analysis")
    print(f"  Source: {CSV_PATH.name}")
    print(f"  Total Lineups: {total_lineups}")
    print("=" * 72)

    # ── Exposure ──────────────────────────────────────────────────────────
    exposure = compute_exposure(lineups)
    unique_count = len(exposure)

    print(f"\n  Unique Players Used: {unique_count}\n")

    print("-" * 72)
    print(f"  {'Rank':<6}{'Player':<28}{'Count':>6}{'Exposure %':>12}")
    print("-" * 72)
    for i, (name, cnt, pct) in enumerate(exposure[:30], 1):
        team = PLAYER_TEAMS.get(name, "???")
        bar = "#" * int(pct / 2)
        print(f"  {i:<6}{name + ' (' + team + ')':<28}{cnt:>6}{pct:>10.1f}%  {bar}")
    print("-" * 72)

    # ── Teammate Pair Stacks ─────────────────────────────────────────────
    print(f"\n{'=' * 72}")
    print("  Top 20 Teammate Pair Stacks")
    print("-" * 72)
    print(f"  {'Rank':<6}{'Pair':<48}{'Count':>6}{'%':>8}")
    print("-" * 72)
    pair_stacks = compute_pair_stacks(lineups, top_n=20)
    for i, (pair, cnt, pct) in enumerate(pair_stacks, 1):
        team = PLAYER_TEAMS.get(pair[0], "???")
        label = f"{pair[0]} + {pair[1]} ({team})"
        print(f"  {i:<6}{label:<48}{cnt:>6}{pct:>6.1f}%")
    print("-" * 72)

    # ── Teammate Trio Stacks ─────────────────────────────────────────────
    print(f"\n{'=' * 72}")
    print("  Top 15 Teammate Trio Stacks")
    print("-" * 72)
    print(f"  {'Rank':<6}{'Trio':<56}{'Count':>6}{'%':>8}")
    print("-" * 72)
    trio_stacks = compute_trio_stacks(lineups, top_n=15)
    for i, (trio, cnt, pct) in enumerate(trio_stacks, 1):
        team = PLAYER_TEAMS.get(trio[0], "???")
        label = f"{trio[0]} / {trio[1]} / {trio[2]} ({team})"
        print(f"  {i:<6}{label:<56}{cnt:>6}{pct:>6.1f}%")
    print("-" * 72)

    # ── Team Exposure ────────────────────────────────────────────────────
    print(f"\n{'=' * 72}")
    print("  Team Exposure (total player-slots by team)")
    print("-" * 72)
    team_slots: Counter = Counter()
    for lu in lineups:
        for p in lu:
            t = PLAYER_TEAMS.get(p)
            if t:
                team_slots[t] += 1
    total_slots = total_lineups * 8
    print(f"  {'Team':<8}{'Slots':>8}{'% of All Slots':>16}")
    print("-" * 72)
    for team, cnt in team_slots.most_common():
        print(f"  {team:<8}{cnt:>8}{cnt / total_slots * 100:>14.1f}%")
    print("-" * 72)

    # ── Notable Patterns / Punt Plays ────────────────────────────────────
    print(f"\n{'=' * 72}")
    print("  Notable Patterns & Punt Plays")
    print("-" * 72)
    punts = detect_punt_players(exposure, threshold_pct=15.0)
    if punts:
        print("  High-exposure value/punt plays (non-star, >= 15% exposure):")
        for name, pct in sorted(punts, key=lambda x: -x[1]):
            team = PLAYER_TEAMS.get(name, "???")
            print(f"    - {name} ({team}): {pct:.1f}%")
    else:
        print("  No obvious punt plays detected at >= 15% exposure.")

    # Chalk vs leverage summary
    print()
    chalk = [name for name, _, pct in exposure if pct >= 50.0]
    leverage = [name for name, _, pct in exposure if 5.0 <= pct <= 15.0]
    low_owned = [name for name, _, pct in exposure if pct < 5.0]
    print(f"  Chalk core (>= 50% exposure):  {len(chalk)} players")
    for name in chalk:
        pct = next(p for n, _, p in exposure if n == name)
        print(f"    - {name}: {pct:.1f}%")
    print(f"  Leverage range (5-15%):        {len(leverage)} players")
    for name in leverage:
        pct = next(p for n, _, p in exposure if n == name)
        print(f"    - {name}: {pct:.1f}%")
    print(f"  Low-owned (<5%):               {len(low_owned)} players")
    for name in low_owned:
        cnt = next(c for n, c, _ in exposure if n == name)
        print(f"    - {name}: {cnt} lineup(s)")

    print(f"\n{'=' * 72}")
    print("  Analysis complete.")
    print("=" * 72)


if __name__ == "__main__":
    main()
