"""
Compare App v3 Lineups (saved) vs DK Pre-Contest Sims — offline analysis.
"""
import sys, io, csv, re, json
from collections import Counter, defaultdict

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

LINEUPS_PATH = r"C:\Working\Prediction_App\backend\v3_lineups.json"
CSV_PATH = r"C:\Users\CFlem\Downloads\DK_NBA_Main_Pre_Contest_Sims_Lineups (9).csv"
POSITIONS = ["PG", "SG", "SF", "PF", "C", "G", "F", "UTIL"]


def strip_dk_id(raw):
    return re.sub(r"\s*\(\d+\)", "", raw).strip()


def load_dk_sims(path):
    lineups = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            lineup = [strip_dk_id(row[pos].strip()) for pos in POSITIONS]
            lineups.append(lineup)
    return lineups


def load_app_lineups(path):
    with open(path) as f:
        data = json.load(f)
    lineups = []
    for lu in data.get("lineups", []):
        players = lu.get("players", [])
        names = [p.get("player_name", p.get("name", "???")) for p in players]
        lineups.append(names)
    # Also extract salary info for value analysis
    all_player_info = {}
    for lu in data.get("lineups", []):
        for p in lu.get("players", []):
            name = p.get("player_name", p.get("name", "???"))
            if name not in all_player_info:
                all_player_info[name] = {
                    "salary": p.get("salary", 0),
                    "projected_fp": p.get("projected_fp", 0),
                    "team": p.get("team_abbreviation", "???"),
                }
    return lineups, all_player_info


def get_exposure(lineups, total):
    ctr = Counter()
    for lu in lineups:
        for p in lu:
            ctr[p] += 1
    return {p: (cnt, cnt / total * 100) for p, cnt in ctr.most_common()}


def main():
    app_lineups, player_info = load_app_lineups(LINEUPS_PATH)
    dk_lineups = load_dk_sims(CSV_PATH)

    app_exp = get_exposure(app_lineups, len(app_lineups))
    dk_exp = get_exposure(dk_lineups, len(dk_lineups))

    all_players = sorted(
        set(list(app_exp.keys()) + list(dk_exp.keys())),
        key=lambda p: max(app_exp.get(p, (0, 0))[1], dk_exp.get(p, (0, 0))[1]),
        reverse=True,
    )

    print("=" * 95)
    print(f"  EXPOSURE COMPARISON: App (v3 — after 5 fixes) vs DK Pre-Contest Sims")
    print(f"  App Lineups: {len(app_lineups)}  |  DK Sims: {len(dk_lineups)}")
    print(f"  App Unique Players: {len(app_exp)}  |  DK Unique Players: {len(dk_exp)}")
    print("=" * 95)

    print(f"\n  {'Player':<30} {'Salary':>7} {'App%':>7} {'DK%':>7} {'Delta':>7}  {'Match':<10}")
    print(f"  {'-' * 73}")

    close_matches = 0
    total_abs_delta = 0.0
    for player in all_players:
        app_pct = app_exp.get(player, (0, 0))[1]
        dk_pct = dk_exp.get(player, (0, 0))[1]
        delta = app_pct - dk_pct
        sal = player_info.get(player, {}).get("salary", 0)

        if abs(delta) < 10:
            match = "CLOSE"
            close_matches += 1
        elif delta > 0:
            match = "OVER"
        else:
            match = "UNDER"

        total_abs_delta += abs(delta)
        sal_str = f"${sal:,}" if sal else "???"
        print(f"  {player:<30} {sal_str:>7} {app_pct:>6.1f}% {dk_pct:>6.1f}% {delta:>+6.1f}%  {match:<10}")

    avg_delta = total_abs_delta / len(all_players) if all_players else 0

    # ── Summary ─────────────────────────────────────────────
    print(f"\n{'=' * 95}")
    print(f"  SUMMARY METRICS")
    print(f"{'=' * 95}")
    print(f"  Close matches (|delta| < 10%): {close_matches}/{len(all_players)} ({close_matches / len(all_players) * 100:.0f}%)")
    print(f"  Average absolute delta:        {avg_delta:.1f}%")
    print(f"  App unique players:            {len(app_exp)}")
    print(f"  DK unique players:             {len(dk_exp)}")

    # ── Top-10 Comparison ───────────────────────────────────
    app_top10 = sorted(app_exp.items(), key=lambda x: -x[1][1])[:10]
    dk_top10 = sorted(dk_exp.items(), key=lambda x: -x[1][1])[:10]
    app_top10_names = {p for p, _ in app_top10}
    dk_top10_names = {p for p, _ in dk_top10}
    overlap_10 = len(app_top10_names & dk_top10_names)

    print(f"\n  TOP-10 MOST EXPOSED (overlap: {overlap_10}/10):")
    print(f"  {'Rank':<6} {'App Player':<30} {'App%':>6}  {'DK Player':<30} {'DK%':>6}")
    print(f"  {'-' * 82}")
    for i in range(10):
        ap, (_, apct) = app_top10[i] if i < len(app_top10) else ("", (0, 0))
        dp, (_, dpct) = dk_top10[i] if i < len(dk_top10) else ("", (0, 0))
        marker = " <=" if ap == dp else ""
        print(f"  {i + 1:<6} {ap:<30} {apct:>5.1f}%  {dp:<30} {dpct:>5.1f}%{marker}")

    # ── Value Plays ──────────────────────────────────────────
    print(f"\n  VALUE PLAYS (salary <= $4,500):")
    print(f"  {'Player':<30} {'Salary':>7} {'App%':>7} {'DK%':>7} {'Delta':>7}")
    print(f"  {'-' * 62}")
    value_players = []
    for player in all_players:
        sal = player_info.get(player, {}).get("salary", 99999)
        if sal <= 4500:
            app_pct = app_exp.get(player, (0, 0))[1]
            dk_pct = dk_exp.get(player, (0, 0))[1]
            if app_pct > 0 or dk_pct > 0:
                value_players.append((player, sal, app_pct, dk_pct))
    for p, sal, ap, dp in sorted(value_players, key=lambda x: -max(x[2], x[3])):
        delta = ap - dp
        print(f"  {p:<30} ${sal:>6,} {ap:>6.1f}% {dp:>6.1f}% {delta:>+6.1f}%")

    # ── Star Players ─────────────────────────────────────────
    print(f"\n  STAR PLAYERS (salary >= $7,000):")
    print(f"  {'Player':<30} {'Salary':>7} {'App%':>7} {'DK%':>7} {'Delta':>7}")
    print(f"  {'-' * 62}")
    star_players = []
    for player in all_players:
        sal = player_info.get(player, {}).get("salary", 0)
        if sal >= 7000:
            app_pct = app_exp.get(player, (0, 0))[1]
            dk_pct = dk_exp.get(player, (0, 0))[1]
            star_players.append((player, sal, app_pct, dk_pct))
    for p, sal, ap, dp in sorted(star_players, key=lambda x: -x[1]):
        delta = ap - dp
        print(f"  {p:<30} ${sal:>6,} {ap:>6.1f}% {dp:>6.1f}% {delta:>+6.1f}%")

    # ── Projection Source Mix ──────────────────────────────
    print(f"\n  PROJECTION SOURCE MIX (from pool):")
    sources = Counter()
    for pi in player_info.values():
        sources["known"] += 1
    print(f"  Total players with info: {sum(sources.values())}")

    print(f"\n{'=' * 95}")
    print(f"  END OF COMPARISON")
    print(f"{'=' * 95}")


if __name__ == "__main__":
    main()
