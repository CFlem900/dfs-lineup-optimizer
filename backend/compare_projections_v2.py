"""Compare external Data Hub projections vs app's cached player pool.

Usage:
    python compare_projections_v2.py <external_csv> <pool_json>
"""
import csv
import json
import sys
import os
from collections import defaultdict

# Force UTF-8 output on Windows
if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")


def safe_float(v, default=0.0):
    try:
        return float(v)
    except (ValueError, TypeError):
        return default


def normalize_name(name: str) -> str:
    """Normalize player name for matching."""
    return (
        name.strip()
        .replace(".", "")
        .replace("'", "'")
        .replace("'", "'")
        .lower()
    )


# Known name fixes: external name → app name
NAME_FIXES = {
    "nicolas claxton": "nic claxton",
    "kenyon martin jr": "kenyon martin jr.",
    "oj simpson": "o.j. simpson",
    "cj mccollum": "c.j. mccollum",
    "rj barrett": "r.j. barrett",
    "pj washington": "p.j. washington",
    "tj mcconnell": "t.j. mcconnell",
    "cam thomas": "cameron thomas",
    "nic claxton": "nicolas claxton",
}


def main():
    ext_csv = sys.argv[1] if len(sys.argv) > 1 else r"C:\Users\CFlem\Downloads\DK_NBA_Main_Data_Hub_Projections (16).csv"
    pool_json = sys.argv[2] if len(sys.argv) > 2 else r"C:\Working\Prediction_App\backend\cache\pool_482c1317850f.json"

    # ── Load external CSV ────────────────────────────────────────
    ext_players = {}
    with open(ext_csv, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            proj = safe_float(row.get("Projection", 0))
            if proj <= 0:
                continue
            name = row["Player"].strip()
            ext_players[normalize_name(name)] = {
                "name": name,
                "team": row.get("Team", ""),
                "pos": row.get("Position", ""),
                "salary": int(safe_float(row.get("Salary", 0))),
                "proj": proj,
                "minutes": safe_float(row.get("Minutes", 0)),
                "own": safe_float(row.get("Ownership %", 0)),
                "optimal": safe_float(row.get("Optimal %", 0)),
                "starting": row.get("Starting", "").lower() == "true",
                "injury": row.get("Injury", "None"),
                "ceiling": safe_float(row.get("Ceiling", 0)),
                "floor": safe_float(row.get("Floor", 0)),
                "std_dev": safe_float(row.get("Std Dev", 0)),
                "leverage": safe_float(row.get("Leverage", 0)),
            }

    # ── Load app pool ────────────────────────────────────────────
    with open(pool_json, "r", encoding="utf-8") as f:
        raw = json.loads(f.readline())

    # Pool file may be a dict with "players" key or a raw list
    if isinstance(raw, dict) and "players" in raw:
        pool_data = raw["players"]
    elif isinstance(raw, list):
        pool_data = raw
    else:
        print(f"ERROR: Unknown pool format. Keys: {list(raw.keys()) if isinstance(raw, dict) else type(raw)}")
        return

    app_players = {}
    for p in pool_data:
        name = (p.get("display_name") or p.get("player_name", "")).strip()
        app_players[normalize_name(name)] = {
            "name": name,
            "team": p.get("team_abbreviation", ""),
            "pos": p.get("position", ""),
            "salary": p.get("salary", 0),
            "proj": round(p.get("projected_fp", 0), 1),
            "minutes": round(p.get("projected_minutes", 0), 1),
            "ceiling": round(p.get("ceiling_fp", 0), 1),
            "floor": round(p.get("floor_fp", 0), 1),
        }

    # ── Match players ────────────────────────────────────────────
    matched = []
    ext_unmatched = []
    app_unmatched = []

    for norm_name, ext in ext_players.items():
        # Try direct match
        app = app_players.get(norm_name)
        if not app:
            # Try name fixes
            fixed = NAME_FIXES.get(norm_name)
            if fixed:
                app = app_players.get(fixed)
            if not app:
                # Try substring match
                for app_norm, app_p in app_players.items():
                    if norm_name in app_norm or app_norm in norm_name:
                        app = app_p
                        break
        if app:
            diff = app["proj"] - ext["proj"]
            min_diff = app["minutes"] - ext["minutes"]
            matched.append({
                "name": ext["name"],
                "team": ext["team"],
                "pos": ext["pos"],
                "salary": ext["salary"],
                "ext_proj": ext["proj"],
                "app_proj": app["proj"],
                "diff": diff,
                "ext_min": ext["minutes"],
                "app_min": app["minutes"],
                "min_diff": min_diff,
                "own": ext["own"],
                "starting": ext["starting"],
                "ext_ceil": ext["ceiling"],
                "app_ceil": app["ceiling"],
                "ext_floor": ext["floor"],
                "app_floor": app["floor"],
                "leverage": ext["leverage"],
            })
        else:
            ext_unmatched.append(ext)

    # ── Stats ────────────────────────────────────────────────────
    print(f"\n{'='*80}")
    print(f"  PROJECTION COMPARISON: App Pool vs Data Hub")
    print(f"  External: {os.path.basename(ext_csv)}")
    print(f"  App Pool: {os.path.basename(pool_json)}")
    print(f"{'='*80}")

    print(f"\n  Matched: {len(matched)} | Ext unmatched: {len(ext_unmatched)}")

    if ext_unmatched:
        print(f"  Unmatched external: {', '.join(p['name'] for p in ext_unmatched[:10])}")

    # Correlation
    if len(matched) >= 5:
        ext_vals = [m["ext_proj"] for m in matched]
        app_vals = [m["app_proj"] for m in matched]
        n = len(ext_vals)
        mean_e = sum(ext_vals) / n
        mean_a = sum(app_vals) / n
        cov = sum((e - mean_e) * (a - mean_a) for e, a in zip(ext_vals, app_vals)) / n
        std_e = (sum((e - mean_e) ** 2 for e in ext_vals) / n) ** 0.5
        std_a = (sum((a - mean_a) ** 2 for a in app_vals) / n) ** 0.5
        r = cov / (std_e * std_a) if std_e > 0 and std_a > 0 else 0
        mean_diff = sum(m["diff"] for m in matched) / n
        abs_mean_diff = sum(abs(m["diff"]) for m in matched) / n
        higher_count = sum(1 for m in matched if m["diff"] > 0)

        # RMSE
        rmse = (sum(m["diff"] ** 2 for m in matched) / n) ** 0.5

        # Minutes correlation
        ext_min = [m["ext_min"] for m in matched if m["ext_min"] > 0 and m["app_min"] > 0]
        app_min = [m["app_min"] for m in matched if m["ext_min"] > 0 and m["app_min"] > 0]
        if len(ext_min) >= 5:
            n_m = len(ext_min)
            me = sum(ext_min) / n_m
            ma = sum(app_min) / n_m
            cov_m = sum((e - me) * (a - ma) for e, a in zip(ext_min, app_min)) / n_m
            std_em = (sum((e - me) ** 2 for e in ext_min) / n_m) ** 0.5
            std_am = (sum((a - ma) ** 2 for a in app_min) / n_m) ** 0.5
            r_min = cov_m / (std_em * std_am) if std_em > 0 and std_am > 0 else 0
        else:
            r_min = 0

        print(f"\n  ── Aggregate Stats ──")
        print(f"  Projection correlation (r): {r:.4f}")
        print(f"  Minutes correlation (r):    {r_min:.4f}")
        print(f"  Mean diff (App - Hub):      {mean_diff:+.2f} FP")
        print(f"  Mean absolute diff:         {abs_mean_diff:.2f} FP")
        print(f"  RMSE:                       {rmse:.2f} FP")
        print(f"  App higher for:             {higher_count}/{n} ({100*higher_count/n:.0f}%)")

    # ── Biggest divergences (sorted by absolute diff) ────────────
    matched.sort(key=lambda m: abs(m["diff"]), reverse=True)

    print(f"\n  ── TOP 25 DIVERGENCES (|App - Hub|) ──")
    print(f"  {'Player':<22} {'Team':>4} {'Pos':>5} {'$':>6}  {'Hub':>6} {'App':>6} {'Diff':>7}  {'Hub Min':>7} {'App Min':>7} {'Δ Min':>6}  {'Own%':>5} {'Start':>5}")
    print(f"  {'-'*22} {'-'*4} {'-'*5} {'-'*6}  {'-'*6} {'-'*6} {'-'*7}  {'-'*7} {'-'*7} {'-'*6}  {'-'*5} {'-'*5}")

    for m in matched[:25]:
        start_mark = "★" if m["starting"] else ""
        print(
            f"  {m['name']:<22} {m['team']:>4} {m['pos']:>5} "
            f"${m['salary']:>5}  "
            f"{m['ext_proj']:>6.1f} {m['app_proj']:>6.1f} {m['diff']:>+7.1f}  "
            f"{m['ext_min']:>7.1f} {m['app_min']:>7.1f} {m['min_diff']:>+6.1f}  "
            f"{m['own']:>5.1f} {start_mark:>5}"
        )

    # ── Key player deep dive (high ownership divergences) ────────
    high_own_diverge = [m for m in matched if m["own"] >= 8 and abs(m["diff"]) >= 3]
    high_own_diverge.sort(key=lambda m: abs(m["diff"]), reverse=True)

    if high_own_diverge:
        print(f"\n  ── HIGH-OWNERSHIP DIVERGENCES (Own ≥ 8%, |Diff| ≥ 3 FP) ──")
        print(f"  {'Player':<22} {'Team':>4}  {'Hub':>6} {'App':>6} {'Diff':>7}  {'Hub Min':>7} {'App Min':>7} {'Δ Min':>6}  {'Own%':>5}")
        print(f"  {'-'*22} {'-'*4}  {'-'*6} {'-'*6} {'-'*7}  {'-'*7} {'-'*7} {'-'*6}  {'-'*5}")
        for m in high_own_diverge:
            print(
                f"  {m['name']:<22} {m['team']:>4}  "
                f"{m['ext_proj']:>6.1f} {m['app_proj']:>6.1f} {m['diff']:>+7.1f}  "
                f"{m['ext_min']:>7.1f} {m['app_min']:>7.1f} {m['min_diff']:>+6.1f}  "
                f"{m['own']:>5.1f}"
            )

    # ── Starters misalignment ────────────────────────────────────
    starter_issues = [
        m for m in matched
        if m["starting"] and m["app_min"] < 24
    ]
    if starter_issues:
        starter_issues.sort(key=lambda m: m["app_min"])
        print(f"\n  ── STARTER MISALIGNMENT (Hub says Starting, App < 24 min) ──")
        print(f"  {'Player':<22} {'Team':>4}  {'Hub Min':>7} {'App Min':>7}  {'Hub FP':>6} {'App FP':>6}")
        print(f"  {'-'*22} {'-'*4}  {'-'*7} {'-'*7}  {'-'*6} {'-'*6}")
        for m in starter_issues:
            print(
                f"  {m['name']:<22} {m['team']:>4}  "
                f"{m['ext_min']:>7.1f} {m['app_min']:>7.1f}  "
                f"{m['ext_proj']:>6.1f} {m['app_proj']:>6.1f}"
            )

    # ── Leverage opportunities ───────────────────────────────────
    leverage_plays = [
        m for m in matched
        if m["leverage"] > 2.0 and m["diff"] > 2.0
    ]
    leverage_plays.sort(key=lambda m: m["leverage"], reverse=True)

    if leverage_plays:
        print(f"\n  ── POSITIVE LEVERAGE PLAYS (Hub Lev > +2, App Higher) ──")
        print(f"  {'Player':<22} {'Team':>4}  {'Hub FP':>6} {'App FP':>6} {'Diff':>7}  {'Own%':>5} {'Lev':>5}")
        print(f"  {'-'*22} {'-'*4}  {'-'*6} {'-'*6} {'-'*7}  {'-'*5} {'-'*5}")
        for m in leverage_plays[:10]:
            print(
                f"  {m['name']:<22} {m['team']:>4}  "
                f"{m['ext_proj']:>6.1f} {m['app_proj']:>6.1f} {m['diff']:>+7.1f}  "
                f"{m['own']:>5.1f} {m['leverage']:>+5.1f}"
            )

    # ── Negative leverage warnings ───────────────────────────────
    neg_leverage = [
        m for m in matched
        if m["leverage"] < -2.0 and m["diff"] > 2.0
    ]
    neg_leverage.sort(key=lambda m: m["leverage"])

    if neg_leverage:
        print(f"\n  ── NEGATIVE LEVERAGE WARNINGS (Hub Lev < -2, App Higher) ──")
        print(f"  {'Player':<22} {'Team':>4}  {'Hub FP':>6} {'App FP':>6} {'Diff':>7}  {'Own%':>5} {'Lev':>5}")
        print(f"  {'-'*22} {'-'*4}  {'-'*6} {'-'*6} {'-'*7}  {'-'*5} {'-'*5}")
        for m in neg_leverage[:10]:
            print(
                f"  {m['name']:<22} {m['team']:>4}  "
                f"{m['ext_proj']:>6.1f} {m['app_proj']:>6.1f} {m['diff']:>+7.1f}  "
                f"{m['own']:>5.1f} {m['leverage']:>+5.1f}"
            )

    # ── Game-level comparison ────────────────────────────────────
    game_diffs = defaultdict(list)
    for m in matched:
        game_diffs[m["team"]].append(m["diff"])

    print(f"\n  ── TEAM-LEVEL BIAS (Avg App - Hub per team) ──")
    team_stats = []
    for team, diffs in game_diffs.items():
        avg = sum(diffs) / len(diffs)
        team_stats.append((team, avg, len(diffs)))
    team_stats.sort(key=lambda x: x[1], reverse=True)
    for team, avg, n in team_stats:
        bar = "+" * int(max(0, avg)) + "-" * int(max(0, -avg))
        print(f"  {team:>4}: {avg:>+6.1f} FP (n={n}) {bar}")

    print(f"\n{'='*80}")
    print(f"  Pool includes vacancy promotion fix (dynamic starter detection).")
    print(f"{'='*80}\n")


if __name__ == "__main__":
    main()
