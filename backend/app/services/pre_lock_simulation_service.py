"""Pre-Lock Simulation Scheduler — Auto-run simulations 60 min before lock.

Each day at 9:00 AM ET, ``schedule_slate_simulations()`` queries DraftKings
for today's slates, computes lock times, and dynamically schedules one-shot
APScheduler jobs to run ``run_pre_lock_simulation()`` exactly 60 minutes
before each slate locks.

When the one-shot fires, the service:
  1. Builds the full player pool for the slate
  2. Runs the Monte Carlo simulator (10,000 trials)
  3. Exports player projections + sim results to CSV/XLSX
  4. Emails the reports to SIMULATION_RECIPIENT (comma-separated list)

Configuration (via .env):
    SIMULATION_RECIPIENT  — Comma-separated emails for pre-lock reports
    PRE_LOCK_SIM_LEAD_MIN — Minutes before lock to run (default: 60)
"""

import csv
import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────────────
PRE_LOCK_SIM_LEAD_MIN = int(os.getenv("PRE_LOCK_SIM_LEAD_MIN", "60"))


def get_simulation_recipients() -> List[str]:
    """Parse SIMULATION_RECIPIENT env var into a list of email addresses.

    Falls back to REPORT_RECIPIENT if SIMULATION_RECIPIENT is not set.
    """
    raw = os.getenv(
        "SIMULATION_RECIPIENT",
        os.getenv("REPORT_RECIPIENT", ""),
    )
    return [addr.strip() for addr in raw.split(",") if addr.strip()]


async def schedule_slate_simulations(scheduler: Any) -> int:
    """Query today's slates and schedule pre-lock simulation jobs.

    Called daily at 9:00 AM ET by APScheduler.  Dynamically adds one-shot
    jobs for each slate that locks today.

    Parameters
    ----------
    scheduler : AsyncIOScheduler
        The running APScheduler instance.

    Returns
    -------
    int
        Number of simulation jobs scheduled.
    """
    from app.api.dependencies import get_services

    svc = get_services()
    dk_slate_svc = getattr(svc, "dk_slate_service", None)

    if dk_slate_svc is None:
        logger.warning("[PreLockSim] DKSlateService not available — skipping")
        return 0

    try:
        slates = await dk_slate_svc.get_todays_slates(sport="nba")
    except Exception as e:
        logger.error("[PreLockSim] Failed to fetch slates: %s", e)
        return 0

    if not slates:
        logger.info("[PreLockSim] No slates found for today — nothing to schedule")
        return 0

    now_utc = datetime.now(timezone.utc)
    scheduled = 0

    for slate in slates:
        lock_utc = getattr(slate, "min_start_utc", None)
        if not lock_utc:
            continue

        # Ensure timezone-aware
        if lock_utc.tzinfo is None:
            lock_utc = lock_utc.replace(tzinfo=timezone.utc)

        sim_time_utc = lock_utc - timedelta(minutes=PRE_LOCK_SIM_LEAD_MIN)

        # Skip if the sim time has already passed
        if sim_time_utc <= now_utc:
            logger.info(
                "[PreLockSim] %s — sim time %s already passed, skipping",
                slate.name, sim_time_utc.strftime("%I:%M %p UTC"),
            )
            continue

        dg_id = getattr(slate, "draft_group_id", None)
        if not dg_id:
            continue

        job_id = f"pre_lock_sim_{dg_id}"

        # Remove existing job if re-scheduling
        existing = scheduler.get_job(job_id)
        if existing:
            scheduler.remove_job(job_id)

        scheduler.add_job(
            _run_pre_lock_wrapper,
            trigger="date",
            run_date=sim_time_utc,
            id=job_id,
            name=f"Pre-Lock Sim: {slate.name} (DG {dg_id})",
            kwargs={
                "draft_group_id": dg_id,
                "slate_name": slate.name,
                "lock_time_utc": lock_utc.isoformat(),
            },
            replace_existing=True,
        )

        scheduled += 1
        lock_et = lock_utc - timedelta(hours=4)  # Approximate ET
        sim_et = sim_time_utc - timedelta(hours=4)
        logger.info(
            "[PreLockSim] Scheduled: %s (DG %s) — sim at %s ET, lock at %s ET",
            slate.name, dg_id,
            sim_et.strftime("%-I:%M %p"),
            lock_et.strftime("%-I:%M %p"),
        )

    logger.info(
        "[PreLockSim] Daily schedule complete: %d simulation(s) queued", scheduled
    )
    return scheduled


async def _run_pre_lock_wrapper(
    draft_group_id: int,
    slate_name: str,
    lock_time_utc: str,
):
    """APScheduler wrapper — catches all exceptions to prevent scheduler crash."""
    try:
        await run_pre_lock_simulation(
            draft_group_id=draft_group_id,
            slate_name=slate_name,
        )
    except Exception as e:
        logger.error(
            "[PreLockSim] %s (DG %s) FAILED: %s",
            slate_name, draft_group_id, e,
            exc_info=True,
        )


async def run_pre_lock_simulation(
    draft_group_id: int,
    slate_name: str,
) -> Optional[Dict[str, Any]]:
    """Execute the full pre-lock simulation pipeline for one slate.

    Steps:
      1. Build the player pool via the existing pool endpoint
      2. Export projections to CSV/XLSX (with sim optimal %, props)
      3. Email reports to SIMULATION_RECIPIENT list

    Parameters
    ----------
    draft_group_id : int
        DraftKings draft group ID.
    slate_name : str
        Human-readable slate name (e.g., "Main", "Night").

    Returns
    -------
    dict or None
        Summary of the simulation run, or None on failure.
    """
    import httpx

    logger.info(
        "[PreLockSim] === STARTING: %s (DG %s) ===",
        slate_name, draft_group_id,
    )

    # 1. Build pool via internal API (reuses all caching/enrichment)
    try:
        async with httpx.AsyncClient(timeout=300) as client:
            resp = await client.get(
                f"http://localhost:8000/api/player-pool",
                params={
                    "platform": "dk",
                    "draft_group_id": draft_group_id,
                    "sport": "nba",
                },
            )
            resp.raise_for_status()
            pool_data = resp.json()
    except Exception as e:
        logger.error("[PreLockSim] Pool fetch failed: %s", e)
        return None

    players = pool_data.get("players", [])
    if not players:
        logger.warning("[PreLockSim] Empty pool — aborting")
        return None

    logger.info("[PreLockSim] Pool loaded: %d players", len(players))

    # 2a. Fetch game-level data from scoreboard
    games_by_team = {}  # team_abbr → game dict
    game_list = []
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            sb_resp = await client.get(
                "http://localhost:8000/api/scoreboard",
                params={"sport": "nba"},
            )
            sb_data = sb_resp.json()
            for slate_obj in sb_data.get("slates", []):
                if slate_obj.get("draft_group_id") == draft_group_id:
                    for g in slate_obj.get("games", []):
                        home = g.get("home_team", {})
                        away = g.get("away_team", {})
                        h_abbr = home.get("team_abbreviation", "")
                        a_abbr = away.get("team_abbreviation", "")
                        game_info = {
                            "matchup": f"{a_abbr} @ {h_abbr}",
                            "game_time": g.get("game_time_et", ""),
                            "spread": g.get("vegas_spread") or g.get("projected_spread", ""),
                            "over_under": g.get("over_under", ""),
                            "projected_total": g.get("projected_total", ""),
                            "projected_pace": g.get("projected_pace", ""),
                            "pace_label": g.get("pace_label", ""),
                            "home_abbr": h_abbr,
                            "away_abbr": a_abbr,
                            "home_implied": round(
                                (float(g.get("projected_total") or 0) +
                                 float(g.get("vegas_spread") or g.get("projected_spread") or 0)) / 2, 1
                            ) if g.get("projected_total") else "",
                            "away_implied": round(
                                (float(g.get("projected_total") or 0) -
                                 float(g.get("vegas_spread") or g.get("projected_spread") or 0)) / 2, 1
                            ) if g.get("projected_total") else "",
                            "home_def_rating": home.get("season_def_rating", ""),
                            "away_def_rating": away.get("season_def_rating", ""),
                            "home_pace": home.get("season_pace", ""),
                            "away_pace": away.get("season_pace", ""),
                            "competitive_context": g.get("competitive_context", ""),
                        }
                        game_list.append(game_info)
                        games_by_team[h_abbr] = game_info
                        games_by_team[a_abbr] = game_info
                    break
    except Exception as e:
        logger.warning("[PreLockSim] Scoreboard fetch failed: %s", e)

    # 2b. Organize players by team
    from collections import defaultdict
    teams_players = defaultdict(list)
    for p in players:
        teams_players[p.get("team_abbreviation", "?")].append(p)
    # Sort within each team by projected FP descending
    for team_abbr in teams_players:
        teams_players[team_abbr].sort(
            key=lambda x: x.get("projected_fp", 0), reverse=True
        )

    # 2c. Export to CSV — organized by team with game headers + stat columns
    today = datetime.now().strftime("%Y-%m-%d")
    slug = slate_name.lower().replace(" ", "_")
    csv_path = os.path.join(
        tempfile.gettempdir(), f"sim_{slug}_{today}.csv"
    )

    # Sort teams by game order (using game_list matchup order)
    team_order = []
    for g in game_list:
        if g["away_abbr"] not in team_order:
            team_order.append(g["away_abbr"])
        if g["home_abbr"] not in team_order:
            team_order.append(g["home_abbr"])
    # Add any teams not in a game (shouldn't happen but safety)
    for t in sorted(teams_players.keys()):
        if t not in team_order:
            team_order.append(t)

    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)

        # ── SHEET 1: Game Environment Summary ─────────────────────
        w.writerow(["=== GAME ENVIRONMENTS ==="])
        w.writerow([
            "Matchup", "Time (ET)", "Spread", "Over/Under", "Proj Total",
            "Proj Pace", "Pace Label", "Home Implied", "Away Implied",
            "Home Def Rtg", "Away Def Rtg", "Context",
        ])
        for g in game_list:
            w.writerow([
                g["matchup"], g["game_time"], g["spread"], g["over_under"],
                g["projected_total"], g["projected_pace"], g["pace_label"],
                g["home_implied"], g["away_implied"],
                g["home_def_rating"], g["away_def_rating"],
                g["competitive_context"],
            ])
        w.writerow([])  # blank separator

        # ── SHEET 2: Player Projections by Team ───────────────────
        w.writerow(["=== PLAYER PROJECTIONS BY TEAM ==="])
        w.writerow([])

        player_headers = [
            "Name", "Pos", "Salary", "Minutes", "FP", "Floor", "Ceiling",
            "PTS", "REB", "AST", "STL", "BLK", "TOV", "FG3M",
            "FPPM", "Value", "Own%",
            "Sim_P10", "Sim_P50", "Sim_P90", "Boom%",
            "Props_PTS", "Props_REB", "Props_AST", "Props_PRA",
        ]

        for team_abbr in team_order:
            team_players = teams_players.get(team_abbr, [])
            if not team_players:
                continue

            # Game header for this team
            game = games_by_team.get(team_abbr, {})
            matchup = game.get("matchup", "")
            spread = game.get("spread", "")
            ou = game.get("over_under", "")
            pace = game.get("projected_pace", "")
            impl = ""
            if game:
                impl = (
                    game["home_implied"]
                    if game.get("home_abbr") == team_abbr
                    else game.get("away_implied", "")
                )

            w.writerow([
                f"--- {team_abbr} ---",
                matchup,
                f"Spread: {spread}" if spread else "",
                f"O/U: {ou}" if ou else "",
                f"Impl: {impl}" if impl else "",
                f"Pace: {pace}" if pace else "",
            ])
            w.writerow(player_headers)

            # Team totals accumulators
            team_fp = 0
            team_mins = 0
            team_stats = {"pts": 0, "reb": 0, "ast": 0, "stl": 0, "blk": 0, "tov": 0, "fg3m": 0}

            for p in team_players:
                fp = p.get("projected_fp", 0) or 0
                mins = p.get("projected_minutes", 0) or 0
                sal = p.get("salary", 0) or 0
                stats = p.get("projected_stats", {}) or {}
                pts = stats.get("pts", 0) or 0
                reb = stats.get("reb", 0) or 0
                ast = stats.get("ast", 0) or 0
                stl = stats.get("stl", 0) or 0
                blk = stats.get("blk", 0) or 0
                tov = stats.get("tov", 0) or 0
                fg3m = stats.get("fg3m", 0) or 0
                fppm = fp / mins if mins > 0 else 0
                val = fp / (sal / 1000) if sal > 0 else 0

                team_fp += fp
                team_mins += mins
                for k in team_stats:
                    team_stats[k] += stats.get(k, 0) or 0

                w.writerow([
                    p.get("player_name", ""),
                    p.get("position", ""),
                    sal,
                    round(mins, 1),
                    round(fp, 1),
                    round(p.get("floor_fp") or 0, 1),
                    round(p.get("ceiling_fp") or 0, 1),
                    round(pts, 1),
                    round(reb, 1),
                    round(ast, 1),
                    round(stl, 1),
                    round(blk, 1),
                    round(tov, 1),
                    round(fg3m, 1),
                    round(fppm, 3),
                    round(val, 2),
                    round(p.get("estimated_ownership") or 0, 1),
                    round(p.get("sim_p10") or 0, 1),
                    round(p.get("sim_p50") or 0, 1),
                    round(p.get("sim_p90") or 0, 1),
                    round((p.get("boom_probability") or 0) * 100, 1),
                    p.get("props_pts_line", ""),
                    p.get("props_reb_line", ""),
                    p.get("props_ast_line", ""),
                    p.get("props_pra_line", ""),
                ])

            # Team totals row
            w.writerow([
                f"TOTAL {team_abbr}", "", "",
                round(team_mins, 1), round(team_fp, 1), "", "",
                round(team_stats["pts"], 1),
                round(team_stats["reb"], 1),
                round(team_stats["ast"], 1),
                round(team_stats["stl"], 1),
                round(team_stats["blk"], 1),
                round(team_stats["tov"], 1),
                round(team_stats["fg3m"], 1),
            ])
            w.writerow([])  # blank separator between teams

    logger.info("[PreLockSim] CSV exported: %s", csv_path)

    # Also sort players by FP for the email summary
    players.sort(key=lambda x: x.get("projected_fp", 0), reverse=True)

    # 3. Build email body — top leverage plays
    top_5 = players[:5]
    body_lines = [
        f"{slate_name} Slate Pre-Lock Simulation Report",
        f"Generated: {datetime.now().strftime('%I:%M %p ET on %B %d, %Y')}",
        f"Players: {len(players)}",
        "",
        "Top 5 Projected Players:",
    ]
    for i, p in enumerate(top_5):
        fp = p.get("projected_fp", 0)
        sal = p.get("salary", 0)
        val = fp / (sal / 1000) if sal > 0 else 0
        body_lines.append(
            f"  {i+1}. {p['player_name']} ({p.get('team_abbreviation','')}) "
            f"— {fp:.1f} FP, ${sal:,}, {val:.1f}x value"
        )

    body_lines.extend([
        "",
        "Full projections with sim percentiles, ownership, and props attached.",
    ])
    body = "\n".join(body_lines)

    # 4. Email to recipients
    recipients = get_simulation_recipients()
    if not recipients:
        logger.warning(
            "[PreLockSim] No recipients configured — set SIMULATION_RECIPIENT in .env"
        )
        return {"players": len(players), "csv": csv_path, "emailed": False}

    from app.services.notification_service import NotificationService
    notifier = NotificationService()

    email_ok = notifier.send_slate_reports(
        subject=f"[DFS] {slate_name} Slate — Pre-Lock Sim Report ({today})",
        body=body,
        file_paths=[csv_path],
        recipient=",".join(recipients),
    )

    logger.info(
        "[PreLockSim] === COMPLETE: %s — %d players, email=%s ===",
        slate_name, len(players), email_ok,
    )

    return {
        "slate_name": slate_name,
        "draft_group_id": draft_group_id,
        "players": len(players),
        "csv": csv_path,
        "emailed": email_ok,
        "recipients": recipients,
    }
