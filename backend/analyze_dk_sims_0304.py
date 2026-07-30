"""
DraftKings NBA Main Slate Pre-Contest Sims Lineup Analysis -- March 4, 2026
Parses 150 simulated lineups and produces exposure, stacking, and categorization reports.
"""

import csv
import re
import sys
import io
from collections import Counter, defaultdict
from itertools import combinations

# Force UTF-8 stdout on Windows to avoid charmap encoding errors
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

CSV_PATH = r"C:\Users\CFlem\Downloads\DK_NBA_Main_Pre_Contest_Sims_Lineups (9).csv"
TOTAL_LINEUPS = 150
POSITIONS = ["PG", "SG", "SF", "PF", "C", "G", "F", "UTIL"]

# ── Team mappings (best-effort based on 2025-26 NBA rosters) ──────────────
PLAYER_TEAM_MAP = {
    # OKC Thunder
    "Shai Gilgeous-Alexander": "OKC",
    "Chet Holmgren": "OKC",
    "Jahmai Mashack": "OKC",
    "Keyonte George": "OKC",
    # New York Knicks
    "Karl-Anthony Towns": "NYK",
    "Jalen Brunson": "NYK",
    "Mikal Bridges": "NYK",
    "OG Anunoby": "NYK",
    "Josh Hart": "NYK",
    # Philadelphia 76ers
    "Tyrese Maxey": "PHI",
    "Kelly Oubre Jr.": "PHI",
    "Andre Drummond": "PHI",
    "Kyle Filipowski": "PHI",
    # Boston Celtics
    "Jaylen Brown": "BOS",
    "Jrue Holiday": "BOS",
    "Derrick White": "BOS",
    # Portland Trail Blazers
    "Donovan Clingan": "POR",
    "Scoot Henderson": "POR",
    "Jerami Grant": "POR",
    "Toumani Camara": "POR",
    "Trendon Watford": "POR",
    "Dominick Barlow": "POR",
    # Dallas Mavericks
    "Quentin Grimes": "DAL",
    "Olivier-Maxence Prosper": "DAL",
    # Charlotte Hornets
    "Brandon Miller": "CHA",
    "LaMelo Ball": "CHA",
    # Sacramento Kings
    "Neemias Queta": "SAC",
    # Utah Jazz
    "Taylor Hendricks": "UTA",
    "Walter Clayton Jr.": "UTA",
    "Brice Sensabaugh": "UTA",
    "Keyonte George": "UTA",
    "John Konchar": "UTA",
    # Memphis Grizzlies
    "Cam Spencer": "MEM",
    "Javon Small": "MEM",
    "Ace Bailey": "MEM",
    "John Konchar": "MEM",
    # San Antonio Spurs
    "Cam Spencer": "SAS",
    # NOTE: Cam Spencer and Javon Small are tricky --let me re-check
    # Actually for 2025-26:
    #   Cam Spencer -> undrafted, likely on a two-way. Was with MEM summer league.
    #   Javon Small -> 2025 draft pick
    # Let me use a more careful mapping based on DK IDs & known rosters
    "Deni Avdija": "POR",
    "Isaiah Hartenstein": "OKC",
}

# Re-do the map cleanly (no duplicate keys)
PLAYER_TEAM_MAP = {
    # OKC Thunder
    "Shai Gilgeous-Alexander": "OKC",
    "Chet Holmgren": "OKC",
    "Isaiah Hartenstein": "OKC",
    # New York Knicks
    "Karl-Anthony Towns": "NYK",
    "Jalen Brunson": "NYK",
    "Mikal Bridges": "NYK",
    "OG Anunoby": "NYK",
    "Josh Hart": "NYK",
    # Philadelphia 76ers
    "Tyrese Maxey": "PHI",
    "Kelly Oubre Jr.": "PHI",
    "Andre Drummond": "PHI",
    "Kyle Filipowski": "PHI",
    # Boston Celtics
    "Jaylen Brown": "BOS",
    "Jrue Holiday": "BOS",
    "Derrick White": "BOS",
    # Portland Trail Blazers
    "Donovan Clingan": "POR",
    "Scoot Henderson": "POR",
    "Jerami Grant": "POR",
    "Toumani Camara": "POR",
    "Deni Avdija": "POR",
    # Dallas Mavericks
    "Quentin Grimes": "DAL",
    "Olivier-Maxence Prosper": "DAL",
    # Charlotte Hornets
    "Brandon Miller": "CHA",
    "LaMelo Ball": "CHA",
    # Sacramento Kings
    "Neemias Queta": "SAC",
    # Utah Jazz
    "Taylor Hendricks": "UTA",
    "Walter Clayton Jr.": "UTA",
    "Brice Sensabaugh": "UTA",
    "Keyonte George": "UTA",
    "John Konchar": "UTA",
    # Memphis Grizzlies
    "Cam Spencer": "MEM",
    "Javon Small": "MEM",
    "Jahmai Mashack": "MEM",
    "Ace Bailey": "MEM",
    # San Antonio Spurs
    "Dominick Barlow": "SAS",
    "Trendon Watford": "SAS",
}


def strip_dk_id(raw: str) -> str:
    """Remove DraftKings player ID in parentheses, e.g. 'Shai ... (42172825)' -> 'Shai ...'"""
    return re.sub(r"\s*\(\d+\)", "", raw).strip()


def load_lineups(path: str):
    lineups = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            lineup = []
            for pos in POSITIONS:
                raw = row[pos].strip()
                name = strip_dk_id(raw)
                lineup.append(name)
            lineups.append(lineup)
    return lineups


def player_exposures(lineups):
    ctr = Counter()
    for lu in lineups:
        for p in lu:
            ctr[p] += 1
    return ctr


def get_team(player: str) -> str:
    return PLAYER_TEAM_MAP.get(player, "???")


def team_stacks(lineups, stack_size: int):
    """Count how often N teammates appear together in a lineup."""
    stack_counter = Counter()
    for lu in lineups:
        # Group players by team
        team_groups = defaultdict(list)
        for p in lu:
            t = get_team(p)
            if t != "???":
                team_groups[t].append(p)
        # For each team with >= stack_size players, enumerate combos
        for team, players in team_groups.items():
            if len(players) >= stack_size:
                for combo in combinations(sorted(players), stack_size):
                    stack_counter[(team, combo)] += 1
    return stack_counter


def categorize_exposure(pct: float) -> str:
    if pct >= 50:
        return "CHALK CORE"
    elif pct >= 15:
        return "MID-RANGE"
    elif pct >= 5:
        return "LEVERAGE"
    else:
        return "LOW-OWNED"


# ── Known approximate DK salaries for value-play detection ────────────────
# These are rough estimates; players under ~$4500 appearing at high rates = punt/value
APPROX_SALARIES = {
    "Cam Spencer": 3200,
    "Javon Small": 3400,
    "Walter Clayton Jr.": 3600,
    "Jahmai Mashack": 3000,
    "Trendon Watford": 3800,
    "Taylor Hendricks": 3600,
    "Olivier-Maxence Prosper": 3800,
    "Dominick Barlow": 3000,
    "John Konchar": 3000,
    "Ace Bailey": 4000,
    "Brice Sensabaugh": 3200,
    "Toumani Camara": 3200,
    "Scoot Henderson": 4200,
    "Kyle Filipowski": 3800,
    "Neemias Queta": 3600,
    "Donovan Clingan": 4200,
    "Quentin Grimes": 4400,
    # Higher salary players (not value)
    "Shai Gilgeous-Alexander": 11000,
    "Karl-Anthony Towns": 8400,
    "Tyrese Maxey": 7800,
    "Jaylen Brown": 7600,
    "Jalen Brunson": 9200,
    "Jrue Holiday": 5800,
    "Derrick White": 5600,
    "Josh Hart": 5200,
    "Kelly Oubre Jr.": 5000,
    "OG Anunoby": 5400,
    "Mikal Bridges": 5200,
    "Jerami Grant": 5000,
    "Andre Drummond": 5000,
    "Brandon Miller": 5400,
    "LaMelo Ball": 8600,
    "Chet Holmgren": 7200,
    "Deni Avdija": 5200,
    "Isaiah Hartenstein": 6200,
    "Keyonte George": 4800,
}

VALUE_SALARY_THRESHOLD = 4500  # Players at or below this are value/punt plays


def main():
    lineups = load_lineups(CSV_PATH)
    total = len(lineups)

    print("=" * 80)
    print(f"  DraftKings NBA Main Slate -- Pre-Contest Sims Analysis (March 4, 2026)")
    print(f"  Total Lineups: {total}")
    print("=" * 80)

    # ── 1. Player Exposure ────────────────────────────────────────────────
    exp = player_exposures(lineups)
    unique_count = len(exp)
    print(f"\n{'-' * 80}")
    print(f"  UNIQUE PLAYERS: {unique_count}")
    print(f"{'-' * 80}")

    print(f"\n  PLAYER EXPOSURE RATES (sorted by exposure)")
    print(f"  {'Player':<30} {'Team':<6} {'Count':>5} {'Exp%':>7}  {'Category':<12}")
    print(f"  {'-' * 72}")
    for player, count in exp.most_common():
        pct = count / total * 100
        team = get_team(player)
        cat = categorize_exposure(pct)
        print(f"  {player:<30} {team:<6} {count:>5} {pct:>6.1f}%  {cat:<12}")

    # ── 2. Exposure by Category ───────────────────────────────────────────
    categories = defaultdict(list)
    for player, count in exp.most_common():
        pct = count / total * 100
        cat = categorize_exposure(pct)
        categories[cat].append((player, pct))

    print(f"\n{'-' * 80}")
    print(f"  EXPOSURE CATEGORIES")
    print(f"{'-' * 80}")
    for cat_name in ["CHALK CORE", "MID-RANGE", "LEVERAGE", "LOW-OWNED"]:
        players = categories.get(cat_name, [])
        print(f"\n  {cat_name} ({len(players)} players):")
        for p, pct in players:
            print(f"    {p:<30} {pct:>5.1f}%  [{get_team(p)}]")

    # ── 3. Team Exposure ──────────────────────────────────────────────────
    team_exp = defaultdict(float)
    team_players = defaultdict(set)
    for player, count in exp.items():
        t = get_team(player)
        pct = count / total * 100
        team_exp[t] += pct
        team_players[t].add(player)

    print(f"\n{'-' * 80}")
    print(f"  TEAM AGGREGATE EXPOSURE (total player-exposure across all slots)")
    print(f"{'-' * 80}")
    print(f"  {'Team':<8} {'Agg Exp%':>9} {'# Players':>10}")
    print(f"  {'-' * 30}")
    for team, agg in sorted(team_exp.items(), key=lambda x: -x[1]):
        print(f"  {team:<8} {agg:>8.1f}% {len(team_players[team]):>10}")

    # ── 4. Pair Stacks (2 teammates) ──────────────────────────────────────
    pair_stacks = team_stacks(lineups, 2)
    print(f"\n{'-' * 80}")
    print(f"  MOST COMMON PAIR STACKS (2 teammates in same lineup)")
    print(f"{'-' * 80}")
    print(f"  {'Team':<6} {'Player 1':<28} {'Player 2':<28} {'Count':>5} {'Rate%':>6}")
    print(f"  {'-' * 76}")
    for (team, combo), count in pair_stacks.most_common(30):
        pct = count / total * 100
        print(f"  {team:<6} {combo[0]:<28} {combo[1]:<28} {count:>5} {pct:>5.1f}%")

    # ── 5. Trio Stacks (3 teammates) ──────────────────────────────────────
    trio_stacks = team_stacks(lineups, 3)
    print(f"\n{'-' * 80}")
    print(f"  MOST COMMON TRIO STACKS (3 teammates in same lineup)")
    print(f"{'-' * 80}")
    if trio_stacks:
        print(f"  {'Team':<6} {'Players':<65} {'Count':>5} {'Rate%':>6}")
        print(f"  {'-' * 84}")
        for (team, combo), count in trio_stacks.most_common(25):
            pct = count / total * 100
            names = " / ".join(combo)
            print(f"  {team:<6} {names:<65} {count:>5} {pct:>5.1f}%")
    else:
        print("  (No trio stacks found)")

    # ── 6. Punt / Value Plays ─────────────────────────────────────────────
    print(f"\n{'-' * 80}")
    print(f"  PUNT / VALUE PLAYS (est. salary <= ${VALUE_SALARY_THRESHOLD}, sorted by exposure)")
    print(f"{'-' * 80}")
    print(f"  {'Player':<30} {'Team':<6} {'~Salary':>7} {'Count':>5} {'Exp%':>7}")
    print(f"  {'-' * 60}")
    value_plays = []
    for player, count in exp.most_common():
        sal = APPROX_SALARIES.get(player)
        if sal is not None and sal <= VALUE_SALARY_THRESHOLD:
            pct = count / total * 100
            value_plays.append((player, get_team(player), sal, count, pct))
    for p, t, sal, cnt, pct in value_plays:
        print(f"  {p:<30} {t:<6} ${sal:>6,} {cnt:>5} {pct:>6.1f}%")

    # ── 7. Position Distribution ──────────────────────────────────────────
    print(f"\n{'-' * 80}")
    print(f"  POSITION USAGE (which players appear in which slots)")
    print(f"{'-' * 80}")
    pos_usage = {pos: Counter() for pos in POSITIONS}
    with open(CSV_PATH, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            for pos in POSITIONS:
                name = strip_dk_id(row[pos].strip())
                pos_usage[pos][name] += 1

    for pos in POSITIONS:
        print(f"\n  {pos}:")
        for player, count in pos_usage[pos].most_common(5):
            pct = count / total * 100
            print(f"    {player:<30} {count:>4}x ({pct:>5.1f}%)")

    # ── 8. Lineup Diversity Score ─────────────────────────────────────────
    print(f"\n{'-' * 80}")
    print(f"  LINEUP DIVERSITY METRICS")
    print(f"{'-' * 80}")
    # How many unique lineups (as frozensets)
    lineup_sets = [frozenset(lu) for lu in lineups]
    unique_sets = len(set(lineup_sets))
    print(f"  Unique lineup combinations: {unique_sets} / {total}")
    # Average overlap between lineups
    overlaps = []
    sample = lineups[:50]  # sample for speed
    for i in range(len(sample)):
        for j in range(i + 1, len(sample)):
            overlap = len(set(sample[i]) & set(sample[j]))
            overlaps.append(overlap)
    avg_overlap = sum(overlaps) / len(overlaps) if overlaps else 0
    print(f"  Avg player overlap between lineups (sample of 50): {avg_overlap:.2f} / 8 slots")

    # Top duplicated lineup combos
    set_counter = Counter()
    for ls in lineup_sets:
        set_counter[ls] += 1
    dupes = [(s, c) for s, c in set_counter.most_common(5) if c > 1]
    if dupes:
        print(f"\n  Most repeated lineup combinations:")
        for s, c in dupes:
            print(f"    {c}x: {', '.join(sorted(s))}")
    else:
        print(f"  No duplicate lineup combinations found --all {total} are unique sets.")

    print(f"\n{'=' * 80}")
    print(f"  END OF ANALYSIS")
    print(f"{'=' * 80}")


if __name__ == "__main__":
    main()
