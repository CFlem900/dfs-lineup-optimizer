import nest_asyncio
nest_asyncio.apply()

import asyncio
import csv
import sys
import os
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main():
    from app.api.dependencies import get_services
    svc = get_services()
    los = svc.lineup_optimizer_service

    print("=" * 80)
    print("Building player pool for DG 143408 (2026-03-09 main slate)...")
    print("=" * 80)
    players, excluded = los.build_player_pool(return_excluded=True,
        platform="dk", draft_group_id=143408, game_date="2026-03-09", sport="nba")
    print("Pool built: {} active, {} excluded".format(len(players), len(excluded)))
    print()

    ext_path = Path.home() / "Downloads" / "DK_NBA_Main_Data_Hub_Projections (7).csv"
    ext = {}
    with open(ext_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            name = row["Player"].strip()
            ext[name] = {
                "fp": float(row["Projection"] or 0),
                "minutes": float(row["Minutes"] or 0),
                "salary": int(row["Salary"] or 0),
                "team": row["Team"].strip(),
                "position": row["Position"].strip(),
                "injury": row.get("Injury", "").strip(),
                "starting": row.get("Starting", "").strip(),
                "std_dev": float(row.get("Std Dev", 0) or 0),
            }
    print("External projections loaded: {} players".format(len(ext)))
    print()

    our_by_name = {}
    for p in players:
        our_by_name[p.player_name] = p
        if p.display_name:
            our_by_name[p.display_name] = p

    excl_by_name = {}
    for ep in excluded:
        excl_by_name[ep.player_name] = ep

    FOCUS_TEAMS = ["CLE", "OKC"]

    for team in FOCUS_TEAMS:
        print("=" * 80)
        print("  TEAM: {}".format(team))
        print("=" * 80)

        ext_team = {name: d for name, d in ext.items() if d["team"] == team}
        print("  External players for {}: {}".format(team, len(ext_team)))

        our_team = [p for p in players if p.team_abbreviation == team]
        our_excl_team = [p for p in excluded if p.team_abbreviation == team]
        print("  Our active pool for {}: {}".format(team, len(our_team)))
        print("  Our excluded for {}: {}".format(team, len(our_excl_team)))
        print()

        rows = []
        total_diff = 0.0
        matched = 0
        unmatched_ext = []
        unmatched_ours = set(p.player_name for p in our_team)

        for ext_name, ext_data in sorted(ext_team.items(), key=lambda x: -x[1]["fp"]):
            p = our_by_name.get(ext_name)
            ep = excl_by_name.get(ext_name) if p is None else None

            if p is not None:
                diff = p.projected_fp - ext_data["fp"]
                total_diff += diff
                matched += 1
                unmatched_ours.discard(p.player_name)
                if p.display_name:
                    unmatched_ours.discard(p.display_name)
                rows.append({
                    "name": ext_name, "pos": p.position, "salary": p.salary,
                    "our_fp": p.projected_fp, "ext_fp": ext_data["fp"], "diff": diff,
                    "our_min": p.projected_minutes, "ext_min": ext_data["minutes"],
                    "min_diff": p.projected_minutes - ext_data["minutes"],
                    "source": p.projection_source or "rotation",
                    "confidence": p.rotation_confidence,
                    "injury": p.injury_status or "", "injury_desc": p.injury_description or "",
                    "dk_fppg": p.dk_fppg, "dk_fppg_delta": p.dk_fppg_delta,
                    "floor": p.floor_fp, "ceiling": p.ceiling_fp,
                    "ext_starting": ext_data["starting"], "ext_injury": ext_data["injury"],
                    "status": "ACTIVE",
                })
            elif ep is not None:
                rows.append({
                    "name": ext_name, "pos": ep.position or "?", "salary": ep.salary or 0,
                    "our_fp": ep.projected_fp or 0, "ext_fp": ext_data["fp"],
                    "diff": (ep.projected_fp or 0) - ext_data["fp"],
                    "our_min": ep.projected_minutes or 0, "ext_min": ext_data["minutes"],
                    "min_diff": (ep.projected_minutes or 0) - ext_data["minutes"],
                    "source": ep.exclusion_reason, "confidence": 0,
                    "injury": "", "injury_desc": "",
                    "dk_fppg": None, "dk_fppg_delta": None,
                    "floor": None, "ceiling": None,
                    "ext_starting": ext_data["starting"], "ext_injury": ext_data["injury"],
                    "status": "EXCLUDED (" + ep.exclusion_reason + ")",
                })
            else:
                unmatched_ext.append(ext_name)
                rows.append({
                    "name": ext_name, "pos": ext_data["position"], "salary": ext_data["salary"],
                    "our_fp": None, "ext_fp": ext_data["fp"], "diff": None,
                    "our_min": None, "ext_min": ext_data["minutes"], "min_diff": None,
                    "source": "NOT IN POOL", "confidence": None,
                    "injury": "", "injury_desc": "",
                    "dk_fppg": None, "dk_fppg_delta": None,
                    "floor": None, "ceiling": None,
                    "ext_starting": ext_data["starting"], "ext_injury": ext_data["injury"],
                    "status": "NOT MATCHED",
                })

        rows.sort(key=lambda r: abs(r["diff"]) if r["diff"] is not None else -1, reverse=True)

        fmt_h = "  {:<25s} {:<6s} {:>6s} {:>7s} {:>7s} {:>7s} {:>7s} {:>7s} {:>6s} {:<16s} {:>7s} {:<12s} {:<20s}"
        print(fmt_h.format("Player","Pos","Sal","OurFP","ExtFP","Diff","OurMin","ExtMin","MinD","Source","RotConf","Injury","Status"))
        print(fmt_h.format("-"*24,"-"*5,"-"*6,"-"*6,"-"*6,"-"*6,"-"*6,"-"*6,"-"*5,"-"*15,"-"*6,"-"*11,"-"*19))

        for r in rows:
            ofp = "N/A" if r["our_fp"] is None else "{:.1f}".format(r["our_fp"])
            efp = "{:.1f}".format(r["ext_fp"])
            ds = "N/A" if r["diff"] is None else "{:+.1f}".format(r["diff"])
            om = "N/A" if r["our_min"] is None else "{:.1f}".format(r["our_min"])
            em = "{:.1f}".format(r["ext_min"])
            md = "N/A" if r["min_diff"] is None else "{:+.1f}".format(r["min_diff"])
            cf = "N/A" if r["confidence"] is None else "{:.2f}".format(r["confidence"])
            inj = r["injury"] or r["ext_injury"] or ""
            mk = ""
            if r["diff"] is not None and abs(r["diff"]) >= 3.0:
                mk = " ***"
            elif r["diff"] is not None and abs(r["diff"]) >= 1.5:
                mk = " **"
            line = fmt_h.format(r["name"],r["pos"],str(r["salary"]),ofp,efp,ds,om,em,md,r["source"],cf,inj,r["status"])
            print(line + mk)

        print()
        print("  SUMMARY for {}:".format(team))
        print("    Matched players: {}".format(matched))
        print("    Total FP bias (sum of diffs): {:+.2f}".format(total_diff))
        if matched > 0:
            print("    Avg FP bias per matched player: {:+.2f}".format(total_diff/matched))
        print()

        matched_rows = [r for r in rows if r["diff"] is not None and r["status"] == "ACTIVE"]
        if matched_rows:
            print("  TOP BIAS CONTRIBUTORS for {} (sorted by diff magnitude):".format(team))
            matched_rows.sort(key=lambda r: abs(r["diff"]), reverse=True)
            cumulative = 0.0
            for i, r in enumerate(matched_rows[:10], 1):
                cumulative += r["diff"]
                pct = (cumulative / total_diff * 100) if total_diff != 0 else 0
                dki = ""
                if r["dk_fppg"] is not None:
                    dki = " dk_fppg={:.1f}".format(r["dk_fppg"])
                fc = ""
                if r["floor"] is not None:
                    fc = " floor={:.1f} ceil={:.1f}".format(r["floor"], r["ceiling"])
                print("    {:2d}. {:<22s} diff={:+.2f}  min_diff={:+.1f}  src={:<12s}{}{}".format(
                    i, r["name"], r["diff"], r["min_diff"], r["source"], dki, fc))
                print("        cumulative={:+.2f} ({:.0f}% of team bias)".format(cumulative, pct))
            print()

        if unmatched_ext:
            print("  UNMATCHED external players (in ext but not our pool):")
            for name in unmatched_ext:
                d = ext[name]
                dollar = chr(36)
                print("    - {} ({}, {}, {}{}, ext_fp={:.1f}, min={:.1f}, injury={}, starting={})".format(
                    name, d["team"], d["position"], dollar, d["salary"], d["fp"], d["minutes"], d["injury"], d["starting"]))
            print()

        if unmatched_ours:
            print("  OUR PLAYERS not in external projections:")
            for name in sorted(unmatched_ours):
                p2 = next((x for x in our_team if x.player_name == name), None)
                if p2:
                    dollar = chr(36)
                    src = p2.projection_source or "rotation"
                    print("    - {} ({}, {}{}, our_fp={:.1f}, min={:.1f}, src={})".format(
                        name, p2.position, dollar, p2.salary, p2.projected_fp, p2.projected_minutes, src))
            print()

    # Full team bias summary
    print("=" * 80)
    print("  FULL TEAM BIAS SUMMARY (all teams)")
    print("=" * 80)

    team_biases = {}
    for ext_name, ext_data in ext.items():
        tm = ext_data["team"]
        p3 = our_by_name.get(ext_name)
        if p3 is not None:
            diff2 = p3.projected_fp - ext_data["fp"]
            if tm not in team_biases:
                team_biases[tm] = {"total": 0.0, "count": 0}
            team_biases[tm]["total"] += diff2
            team_biases[tm]["count"] += 1

    fmt_t = "  {:<6s} {:>10s} {:>8s} {:>10s}"
    print(fmt_t.format("Team","TotalBias","Players","AvgBias"))
    print(fmt_t.format("-"*5,"-"*9,"-"*7,"-"*9))
    for t, data in sorted(team_biases.items(), key=lambda x: x[1]["total"], reverse=True):
        avg = data["total"] / data["count"] if data["count"] > 0 else 0
        print("  {:<6s} {:>+10.2f} {:>8d} {:>+10.2f}".format(t, data["total"], data["count"], avg))

    print()
    print("Done.")


if __name__ == "__main__":
    main()
