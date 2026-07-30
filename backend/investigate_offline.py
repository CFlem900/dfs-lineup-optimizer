"""
Investigate player data from the saved v3 lineups JSON — offline analysis.
"""
import sys, io, json
from collections import Counter

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

LINEUPS_PATH = r"C:\Working\Prediction_App\backend\v3_lineups.json"

MISSING_FROM_COMPARISON = ["Javon Small", "Jahmai Mashack", "Kyle Filipowski", "John Konchar"]
VALUE_PLAYS = ["Walter Clayton Jr.", "Cam Spencer", "Quentin Grimes", "Trendon Watford",
               "Donovan Clingan", "Olivier-Maxence Prosper", "Taylor Hendricks"]
OVER_EXPOSED = ["Jalen Brunson", "VJ Edgecombe", "Jaren Jackson Jr.", "Jaylin Williams",
                "Walker Kessler", "Damian Lillard", "Scoot Henderson"]


def safe_float(val, default=0):
    if val is None:
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def main():
    with open(LINEUPS_PATH) as f:
        data = json.load(f)

    lineups = data.get("lineups", [])
    print(f"Total lineups: {len(lineups)}")

    # Build complete player info from ALL lineups
    all_players = {}
    exposure = Counter()
    for lu in lineups:
        for p in lu.get("players", []):
            name = p.get("player_name", p.get("name", "???"))
            exposure[name] += 1
            if name not in all_players:
                all_players[name] = p

    print(f"Unique players across lineups: {len(all_players)}")

    def print_player_detail(name):
        if name in all_players:
            p = all_players[name]
            sal = safe_float(p.get("salary"))
            proj = safe_float(p.get("projected_fp"))
            floor = safe_float(p.get("floor_fp"))
            ceil = safe_float(p.get("ceiling_fp"))
            fppg = safe_float(p.get("dk_fppg") or p.get("fppg"))
            own = safe_float(p.get("estimated_ownership"))
            src = p.get("projection_source") or "N/A"
            team = p.get("team_abbreviation", "???")
            exp_pct = exposure.get(name, 0) / len(lineups) * 100
            val_ratio = proj / (sal / 1000) if sal > 0 else 0
            print(f"  {name:<28} {team:<5} ${sal:>6,.0f} {proj:>6.1f} {floor:>6.1f} {ceil:>6.1f} {fppg:>6.1f} {own:>5.1f}% {val_ratio:>5.1f}x {exp_pct:>5.1f}% {src}")
        else:
            print(f"  {name:<28} NOT IN ANY LINEUP")

    header = f"  {'Player':<28} {'Team':<5} {'Salary':>7} {'Proj':>6} {'Floor':>6} {'Ceil':>6} {'FPPG':>6} {'Own%':>6} {'Val':>5} {'Exp%':>6} {'Source'}"
    sep = f"  {'-'*120}"

    # 1. Missing players
    print(f"\n{'='*130}")
    print(f"  PLAYERS WITH 0% IN COMPARISON (were they in lineups?)")
    print(f"{'='*130}")
    print(header)
    print(sep)
    for name in MISSING_FROM_COMPARISON:
        print_player_detail(name)

    # 2. Value plays
    print(f"\n{'='*130}")
    print(f"  VALUE PLAYS DETAIL")
    print(f"{'='*130}")
    print(header)
    print(sep)
    for name in VALUE_PLAYS:
        print_player_detail(name)

    # 3. Over-exposed in our app
    print(f"\n{'='*130}")
    print(f"  OVER-EXPOSED IN APP (barely in DK sims)")
    print(f"{'='*130}")
    print(header)
    print(sep)
    for name in OVER_EXPOSED:
        print_player_detail(name)

    # 4. Projection source breakdown
    print(f"\n{'='*130}")
    print(f"  PROJECTION SOURCE BREAKDOWN")
    print(f"{'='*130}")
    sources = Counter()
    for p in all_players.values():
        sources[p.get("projection_source") or "None"] += 1
    for src, count in sources.most_common():
        print(f"  {src:<30} {count:>4} players")

    # 5. Top-25 by value ratio
    print(f"\n{'='*130}")
    print(f"  TOP-25 BY VALUE RATIO (FP per $1K salary)")
    print(f"{'='*130}")
    print(header)
    print(sep)
    ratios = []
    for name, p in all_players.items():
        sal = safe_float(p.get("salary"))
        proj = safe_float(p.get("projected_fp"))
        if sal > 0 and proj > 0:
            ratio = proj / (sal / 1000)
            ratios.append((name, ratio))
    ratios.sort(key=lambda x: -x[1])
    for name, _ in ratios[:25]:
        print_player_detail(name)

    # 6. All players sorted by exposure
    print(f"\n{'='*130}")
    print(f"  ALL PLAYERS BY EXPOSURE")
    print(f"{'='*130}")
    print(header)
    print(sep)
    for name, count in exposure.most_common():
        print_player_detail(name)

    print(f"\n{'='*130}")


if __name__ == "__main__":
    main()
