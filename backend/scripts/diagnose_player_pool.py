#!/usr/bin/env python3
"""Diagnose truncated player pool — prints counts at each filter gate.

Usage
-----
  python scripts/diagnose_player_pool.py --draft-group-id 12345
  python scripts/diagnose_player_pool.py --draft-group-id 12345 --game-date 2026-02-20
  python scripts/diagnose_player_pool.py --draft-group-id 12345 --sport nba --platform dk

Runs the same pipeline as ``_build_player_pool_inner()`` in
``lineup_optimizer_service.py`` but prints per-gate counts and
highlights the exact stage(s) causing mass player dropout.
"""

import argparse
import os
import sys
import time
from collections import defaultdict
from datetime import date as _date
from typing import Dict, List, Optional, Set

# ── Bootstrap ───────────────────────────────────────────────────────
_BACKEND = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, _BACKEND)

from app.services.dk_draftables_service import DKDraftablesService, _normalize_name
from app.services.nba_api_service import NBAApiService
from app.services.rotation_engine import RotationEngine, _normalize_for_match
from app.services.dfs_service import DFSService
from app.services.game_service import GameService
from app.services.dk_slate_service import DKSlateService
from app.services.injury_service import InjuryService


# ── ANSI helpers ────────────────────────────────────────────────────
class C:
    RED = "\033[91m"
    YEL = "\033[93m"
    GRN = "\033[92m"
    CYN = "\033[96m"
    BLD = "\033[1m"
    RST = "\033[0m"

def ok(msg):   print(f"  {C.GRN}[OK]{C.RST}  {msg}")
def warn(msg): print(f"  {C.YEL}[!!]{C.RST}  {msg}")
def fail(msg): print(f"  {C.RED}[XX]{C.RST}  {msg}")
def info(msg): print(f"  {C.CYN}[--]{C.RST}  {msg}")
def hdr(msg):  print(f"\n{C.BLD}{'=' * 60}\n  {msg}\n{'=' * 60}{C.RST}")
def sub(msg):  print(f"\n{C.BLD}  --- {msg} ---{C.RST}")


# ── Name matching (mirrors LineupOptimizerService._names_match) ─────
def _names_match(dk_name: str, nba_name: str) -> bool:
    """Check name match — same logic as optimizer."""
    a = _normalize_name(dk_name)
    b = _normalize_name(nba_name)
    if a == b:
        return True
    parts_a = a.split()
    parts_b = b.split()
    if parts_a and parts_b and len(parts_a) >= 2 and len(parts_b) >= 2:
        if (
            parts_a[-1] == parts_b[-1]
            and len(parts_a[0]) >= 3
            and len(parts_b[0]) >= 3
            and parts_a[0][:3] == parts_b[0][:3]
        ):
            return True
    return False


def run_diagnostic(
    draft_group_id: int,
    sport: str = "nba",
    platform: str = "dk",
    game_date: Optional[str] = None,
):
    gd = game_date or _date.today().isoformat()
    t_start = time.time()

    hdr(f"Player Pool Diagnostic — DG {draft_group_id}  ({sport.upper()} / {platform.upper()})  {gd}")

    # ── Service bootstrap ───────────────────────────────────────────
    sub("Bootstrapping services")
    nba_svc = NBAApiService()
    dk_draftables = DKDraftablesService()
    dk_slate_svc = DKSlateService()
    engine = RotationEngine()
    dfs_svc = DFSService()
    game_svc = GameService(dk_slate_service=dk_slate_svc)

    # Injury service — gracefully degrade if DB not available
    inj_svc = None
    try:
        inj_svc = InjuryService()
        ok("All services bootstrapped (including InjuryService)")
    except Exception as e:
        warn(f"InjuryService unavailable ({e}) — injury gates will be skipped")

    # ── Summary counters ────────────────────────────────────────────
    total_draftables = 0
    teams_total = 0
    teams_resolved = 0
    teams_rotation_ok = 0
    total_name_matches = 0
    total_name_misses = 0
    total_zero_minutes = 0
    total_low_games = 0
    total_zero_fp = 0
    total_injury_excluded = 0
    total_entries = 0

    # ── Gate 0: Fetch draftables ────────────────────────────────────
    sub("Gate 0: Fetch DK draftables")
    draftables = dk_draftables.get_draftables(draft_group_id)
    total_draftables = len(draftables)
    if not draftables:
        fail(f"EMPTY draftables for DG {draft_group_id} — pipeline would return []")
        print(f"\n  This is the root cause. Either:")
        print(f"    - The DraftGroup ID {draft_group_id} is invalid/expired")
        print(f"    - The DK API is down or rate-limiting")
        print(f"    - This is a Showdown DG (not Classic)")
        return
    ok(f"{total_draftables} draftable players fetched")

    # ── Gate 1: Group by team ───────────────────────────────────────
    sub("Gate 1: Group draftables by team")
    teams_in_slate: Dict[str, list] = {}
    for d in draftables:
        abbr = d.team_abbreviation.upper()
        teams_in_slate.setdefault(abbr, []).append(d)
    teams_total = len(teams_in_slate)
    for abbr, players in sorted(teams_in_slate.items()):
        info(f"{abbr}: {len(players)} draftable players")
    ok(f"{total_draftables} players across {teams_total} teams")

    # ── Gate 2: Resolve abbreviations ───────────────────────────────
    sub("Gate 2: Resolve team abbreviations (nba_api)")
    all_teams = nba_svc.get_all_teams()
    abbr_to_team: Dict[str, dict] = {
        t["abbreviation"].upper(): t for t in all_teams
    }
    nba_abbrs = sorted(abbr_to_team.keys())

    resolved = {}
    unresolved = []
    for abbr in teams_in_slate:
        team_info = abbr_to_team.get(abbr)
        if team_info:
            resolved[abbr] = team_info
            ok(f"{abbr} -> {team_info['full_name']} (id={team_info['id']})")
        else:
            unresolved.append(abbr)
            fail(f"{abbr} -> NOT FOUND in nba_api")

    teams_resolved = len(resolved)
    if unresolved:
        fail(f"{len(unresolved)} teams unresolved: {unresolved}")
        info(f"Available nba_api abbreviations: {nba_abbrs}")
    else:
        ok(f"All {teams_total} teams resolved")

    # ── Pre-fetch schedule + DvP (mirrors pipeline) ─────────────────
    sub("Pre-fetch: Schedule + opponents")
    opp_lookup: Dict[str, int] = {}
    dvp_cache: Dict[int, dict] = {}
    try:
        schedule = game_svc.get_games(gd)
        for g in schedule.games:
            h = g.home_team.team_abbreviation.upper()
            a = g.away_team.team_abbreviation.upper()
            opp_lookup[h] = g.away_team.team_id
            opp_lookup[a] = g.home_team.team_id
        ok(f"Schedule: {len(schedule.games)} games, {len(opp_lookup)} team-opponent pairs")
    except Exception as e:
        warn(f"Schedule fetch failed: {e} — DvP matchup factors unavailable")

    # ── Fetch injuries ──────────────────────────────────────────────
    injuries_full = []
    if inj_svc:
        try:
            injuries_full = inj_svc.get_all_injuries()
            ok(f"Injuries loaded: {len(injuries_full)} total")
        except Exception as e:
            warn(f"Injury fetch failed: {e}")

    # ── Salary lookup ───────────────────────────────────────────────
    salary_lookup = dk_draftables.build_salary_lookup(draft_group_id)

    # ── Gate 3-10: Process each team ────────────────────────────────
    hdr("Per-Team Pipeline Diagnostics")

    all_entries = []

    for abbr, dk_players in sorted(teams_in_slate.items()):
        sub(f"Team: {abbr} ({len(dk_players)} draftables)")

        # Gate 2 (already done above)
        team_info = resolved.get(abbr)
        if not team_info:
            fail(f"Skipped — abbreviation not resolved")
            continue

        team_id = team_info["id"]
        team_name = team_info["full_name"]

        # Gate 3: Build rotation
        try:
            dk_names = {dk_p.display_name for dk_p in dk_players}
            rotation = nba_svc.build_team_rotation(team_id, draftable_names=dk_names)
            if not rotation:
                fail(f"Empty rotation from build_team_rotation(team_id={team_id})")
                continue
            teams_rotation_ok += 1
            ok(f"Rotation: {len(rotation)} players")
        except Exception as e:
            fail(f"Rotation build exception: {e}")
            continue

        # Gate 4: Project team rotation (240-min normalization)
        try:
            team_injuries = []
            if inj_svc:
                try:
                    team_injuries = inj_svc.get_team_injuries(team_name)
                except Exception:
                    pass

            projected = engine.project_team_rotation(
                team_id=team_id,
                team_name=team_name,
                rotation=rotation,
                injuries=team_injuries,
                game_date=gd,
                all_injuries=injuries_full,
                sport=sport,
            )
            n_proj = len(projected.projections)
            n_zero = sum(1 for p in projected.projections if p.adjusted_minutes <= 0)
            total_zero_minutes += n_zero
            if n_zero:
                warn(f"Projection: {n_proj} players, {n_zero} zeroed by MIN_VIABLE_MINUTES")
            else:
                ok(f"Projection: {n_proj} players, all have minutes > 0")
        except Exception as e:
            fail(f"Projection exception: {e}")
            continue

        # Gate 5: DFS projection
        try:
            matchup_factors = None
            opp_id = opp_lookup.get(abbr)
            if opp_id:
                matchup_factors = dvp_cache.get(opp_id)
            dfs_svc.project_team_dfs(
                projected, rotation, matchup_factors=matchup_factors,
                game_service=game_svc, opponent_team_id=opp_id, sport=sport,
            )
            ok(f"DFS projection complete")
        except Exception as e:
            warn(f"DFS projection failed: {e}")

        # Build injury lookup (same as pipeline)
        _injury_lookup: Dict[str, dict] = {}
        for inj in team_injuries:
            _injury_lookup[_normalize_for_match(inj.player_name)] = {
                "status": inj.status,
                "description": inj.injury_description,
            }
        for inj in injuries_full:
            key = _normalize_for_match(inj.player_name)
            if key not in _injury_lookup:
                _injury_lookup[key] = {
                    "status": inj.status,
                    "description": inj.injury_description,
                }

        # Build games-played lookup
        _MIN_GAMES_FOR_POOL = 3
        _games_played_lookup: Dict[int, int] = {}
        for _rp in rotation:
            _gp = len(_rp.minutes_last_10) if _rp.minutes_last_10 else 0
            _games_played_lookup[_rp.player_id] = _gp

        # Gate 6-10: Match and filter each draftable
        team_matched = 0
        team_name_misses = []
        team_min_filter = 0
        team_low_games = 0
        team_zero_fp = 0
        team_injury = 0
        team_final = 0

        for dk_p in dk_players:
            # Gate 6: Name matching
            proj = None
            for p in projected.projections:
                if _names_match(dk_p.display_name, p.player_name):
                    proj = p
                    break

            # Gate 7: No match or zero minutes
            if not proj:
                team_name_misses.append(dk_p.display_name)
                continue
            if proj.adjusted_minutes <= 0:
                team_min_filter += 1
                continue
            team_matched += 1

            # Gate 8: Games-played filter
            _gp = _games_played_lookup.get(proj.player_id, 0)
            _MIN_SALARY_FOR_LOW_GAMES = 3600
            if _gp < _MIN_GAMES_FOR_POOL:
                if dk_p.salary < _MIN_SALARY_FOR_LOW_GAMES:
                    team_low_games += 1
                    info(f"  Low-games excluded: {dk_p.display_name} ({_gp} games, ${dk_p.salary})")
                    continue

            # Gate 9: Zero FP
            dk_fp = proj.dk_points or 0.0
            if dk_fp <= 0:
                team_zero_fp += 1
                info(f"  Zero FP: {dk_p.display_name} (dk_fp={dk_fp})")
                continue

            # Gate 10: Injury exclusion
            _inj_info = _injury_lookup.get(_normalize_for_match(proj.player_name))
            _inj_status = _inj_info["status"] if _inj_info else None
            _dk_status = (dk_p.status or "").strip().upper()
            if not _inj_status and _dk_status in ("O", "OUT"):
                _inj_status = "Out"
            elif not _inj_status and _dk_status in ("D", "DOUBTFUL"):
                _inj_status = "Doubtful"

            if _inj_status in ("Out", "Doubtful"):
                team_injury += 1
                info(f"  Injury excluded: {dk_p.display_name} ({_inj_status})")
                continue

            team_final += 1
            all_entries.append({
                "player_id": proj.player_id,
                "name": proj.player_name,
                "team": abbr,
                "salary": dk_p.salary,
                "fp": round(dk_fp, 1),
                "minutes": round(proj.adjusted_minutes, 1),
            })

        total_name_matches += team_matched
        total_name_misses += len(team_name_misses)
        total_low_games += team_low_games
        total_zero_fp += team_zero_fp
        total_injury_excluded += team_injury
        total_entries += team_final

        # Per-team summary
        if team_name_misses:
            fail(f"Name misses ({len(team_name_misses)}): {team_name_misses}")
            # Show available projection names for debugging
            proj_names = [p.player_name for p in projected.projections]
            info(f"Available projection names: {proj_names[:15]}...")
        if team_min_filter:
            warn(f"{team_min_filter} filtered by adjusted_minutes <= 0")
        ok(f"Final: {team_final} players from {abbr}")

    # ── Gate 11: Deduplication ──────────────────────────────────────
    hdr("Deduplication Check")
    seen: Dict[int, int] = {}
    deduped = []
    merges = 0
    for entry in all_entries:
        pid = entry["player_id"]
        if pid in seen:
            merges += 1
            existing = deduped[seen[pid]]
            warn(
                f"Duplicate: {entry['name']} (pid={pid}) — "
                f"team1={existing['team']} fp={existing['fp']}, "
                f"team2={entry['team']} fp={entry['fp']}"
            )
        else:
            seen[pid] = len(deduped)
            deduped.append(entry)

    if merges:
        info(f"Deduplicated: {len(all_entries)} -> {len(deduped)} ({merges} merges)")
    else:
        ok(f"No duplicates: {len(deduped)} unique players")

    # Cross-name collision check
    name_groups = defaultdict(list)
    for e in deduped:
        name_groups[_normalize_name(e["name"])].append(e)
    for name, entries in name_groups.items():
        if len(entries) > 1:
            warn(
                f"Name collision: '{name}' -> "
                f"{[(e['player_id'], e['team']) for e in entries]}"
            )

    # ── Gate 12: Cache threshold ────────────────────────────────────
    sub("Gate 12: Cache threshold")
    _min_cache = max(20, teams_total * 5)
    would_cache = len(deduped) >= _min_cache
    if would_cache:
        ok(f"Pool ({len(deduped)}) >= threshold ({_min_cache}) — would cache")
    else:
        fail(f"Pool ({len(deduped)}) < threshold ({_min_cache}) — would NOT cache")

    # ── Final summary ───────────────────────────────────────────────
    elapsed = time.time() - t_start
    hdr("SUMMARY")
    print(f"""
  Draft Group:         {draft_group_id}
  Sport / Platform:    {sport.upper()} / {platform.upper()}
  Game Date:           {gd}
  Elapsed:             {elapsed:.1f}s

  Draftables fetched:  {total_draftables}
  Teams in slate:      {teams_total}
  Teams resolved:      {teams_resolved} / {teams_total}
  Teams with rotation: {teams_rotation_ok} / {teams_resolved}

  Name matches:        {total_name_matches}
  Name misses:         {total_name_misses}
  Zero-minutes filter: {total_zero_minutes}
  Low-games filter:    {total_low_games}
  Zero-FP filter:      {total_zero_fp}
  Injury excluded:     {total_injury_excluded}

  Pre-dedup total:     {len(all_entries)}
  Post-dedup total:    {len(deduped)}
  Cache threshold:     {_min_cache}
  Would cache:         {"YES" if would_cache else "NO"}
""")

    # Highlight the likely root cause
    if total_draftables == 0:
        fail("ROOT CAUSE: DK draftables returned empty — bad DraftGroup ID or API failure")
    elif teams_total <= 2:
        fail(f"ROOT CAUSE: Only {teams_total} team(s) in draftables — likely a Showdown DraftGroup")
    elif teams_resolved < teams_total * 0.5:
        fail(
            f"ROOT CAUSE: {teams_total - teams_resolved} teams have unresolved abbreviations — "
            f"DK uses different abbreviations than nba_api"
        )
    elif teams_rotation_ok < teams_resolved * 0.5:
        fail(
            f"ROOT CAUSE: {teams_resolved - teams_rotation_ok} teams returned empty rotations — "
            f"NBA API failures or rate limiting"
        )
    elif total_name_misses > total_name_matches:
        fail(
            f"ROOT CAUSE: {total_name_misses} name misses vs {total_name_matches} matches — "
            f"name normalization mismatch between DK and NBA API"
        )
    elif total_zero_fp > total_name_matches * 0.5:
        fail(
            f"ROOT CAUSE: {total_zero_fp} players with zero FP — "
            f"DFS projection engine zeroing too many players"
        )
    elif len(deduped) < 30:
        warn(f"Pool suspiciously small ({len(deduped)}) — check per-team output above for specifics")
    else:
        ok(f"Pool looks healthy: {len(deduped)} players")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Diagnose truncated player pool")
    parser.add_argument("--draft-group-id", type=int, required=True, help="DK DraftGroup ID")
    parser.add_argument("--sport", default="nba", help="Sport: nba or cbb")
    parser.add_argument("--platform", default="dk", help="Platform: dk or fd")
    parser.add_argument("--game-date", default=None, help="Game date YYYY-MM-DD (default: today)")
    args = parser.parse_args()

    run_diagnostic(
        draft_group_id=args.draft_group_id,
        sport=args.sport,
        platform=args.platform,
        game_date=args.game_date,
    )
