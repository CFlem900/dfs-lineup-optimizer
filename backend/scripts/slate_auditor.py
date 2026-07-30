"""Slate Auditor — diagnose phantom / duplicate DFS slates.

Queries the DraftKings lobby API, the sticky cache on disk, and the
local scoreboard endpoint to show exactly what slates exist and why
the frontend might be showing an extra one.

Usage (from backend/):
    ./venv/Scripts/python scripts/slate_auditor.py [YYYY-MM-DD]
"""

import io
import json
import os
import sys

# Fix Windows console encoding
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# ── 0. Setup ──────────────────────────────────────────────────────────
TARGET = sys.argv[1] if len(sys.argv) > 1 else date.today().isoformat()

try:
    import zoneinfo
    EASTERN = zoneinfo.ZoneInfo("America/New_York")
except Exception:
    EASTERN = None

def to_et(utc_dt):
    if utc_dt is None:
        return "n/a"
    if EASTERN:
        return utc_dt.astimezone(EASTERN).strftime("%I:%M %p ET")
    return (utc_dt + timedelta(hours=-5)).strftime("%I:%M %p ET")

def to_et_date(utc_dt):
    if utc_dt is None:
        return "n/a"
    if EASTERN:
        return utc_dt.astimezone(EASTERN).date().isoformat()
    return (utc_dt + timedelta(hours=-5)).date().isoformat()


# ══════════════════════════════════════════════════════════════════════
#  SECTION 1 — Live DK Lobby Query
# ══════════════════════════════════════════════════════════════════════
print("=" * 90)
print(f"  SLATE AUDITOR — target date: {TARGET}")
print("=" * 90)

import httpx

DK_LOBBY_URL = "https://www.draftkings.com/lobby/getcontests?sport=NBA"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

print("\n┌─────────────────────────────────────────────────────────────────┐")
print("│  1. DK LOBBY — ALL DraftGroups (raw, unfiltered)              │")
print("└─────────────────────────────────────────────────────────────────┘")

try:
    resp = httpx.get(DK_LOBBY_URL, headers={"User-Agent": USER_AGENT}, timeout=15, follow_redirects=True)
    lobby = resp.json()
except Exception as e:
    print(f"  ✗ Lobby fetch failed: {e}")
    lobby = {}

# Collect ALL unique DraftGroup IDs from Contests
contests = lobby.get("Contests", []) or lobby.get("contests", [])
dg_from_contests = {}  # dg_id → {gameTypeId, sdstring, contest_count}
for c in contests:
    gt = c.get("gameTypeId") or c.get("GameTypeId")
    dg = c.get("dg") or c.get("DraftGroupId")
    sd = c.get("sdstring") or c.get("StartDateString", "")
    name = c.get("n") or c.get("ContestName", "")
    if dg:
        if dg not in dg_from_contests:
            dg_from_contests[dg] = {"gameTypeId": gt, "sd": sd, "count": 0, "sample_name": name}
        dg_from_contests[dg]["count"] += 1

# Also from DraftGroups root array
dg_from_root = {}
for dg in lobby.get("DraftGroups", []):
    gt = dg.get("GameTypeId") or dg.get("gameTypeId")
    dg_id = dg.get("DraftGroupId") or dg.get("draftGroupId")
    sd = dg.get("ContestStartTimeSuffix", "")
    gc = dg.get("GameCount", "?")
    sort_order = dg.get("SortOrder", "?")
    game_type_name = dg.get("GameTypeName", "?")
    if dg_id:
        dg_from_root[dg_id] = {
            "gameTypeId": gt, "sd": sd, "game_count": gc,
            "sort_order": sort_order, "game_type_name": game_type_name,
        }

# Merge both sources
all_dg_ids = sorted(set(dg_from_contests.keys()) | set(dg_from_root.keys()))

# Pretty-print
GAME_TYPE_LABELS = {
    1: "Classic (NBA Legacy?)",
    70: "NBA Classic",
    81: "NBA Showdown Captain",
    96: "NBA Tiers",
    98: "CBB Classic",
    145: "NBA Flash",
    167: "NBA Pick6",
}

print(f"\n  Found {len(all_dg_ids)} unique DraftGroup IDs across lobby data:\n")
print(f"  {'DG ID':>8}  {'gameTypeId':>10}  {'Type Name':20s}  {'SD Label':18s}  {'#Games':>6}  {'#Contests':>9}  {'Sample Contest'}")
print(f"  {'─'*8}  {'─'*10}  {'─'*20}  {'─'*18}  {'─'*6}  {'─'*9}  {'─'*30}")

classic_ids = []
non_classic_ids = []

for dg_id in all_dg_ids:
    gt = None
    sd = ""
    gc = "?"
    cnt = 0
    sample = ""
    type_name = ""

    if dg_id in dg_from_root:
        gt = dg_from_root[dg_id]["gameTypeId"]
        sd = dg_from_root[dg_id]["sd"]
        gc = dg_from_root[dg_id]["game_count"]
        type_name = dg_from_root[dg_id]["game_type_name"]
    if dg_id in dg_from_contests:
        gt = gt or dg_from_contests[dg_id]["gameTypeId"]
        sd = sd or dg_from_contests[dg_id]["sd"]
        cnt = dg_from_contests[dg_id]["count"]
        sample = dg_from_contests[dg_id]["sample_name"][:35]

    gt_label = GAME_TYPE_LABELS.get(gt, f"Unknown({gt})")
    if not type_name or type_name == "?":
        type_name = gt_label

    marker = " ◄ CLASSIC" if gt == 70 else ""
    print(f"  {dg_id:>8}  {gt or '?':>10}  {type_name:20s}  {sd:18s}  {str(gc):>6}  {cnt:>9}  {sample}{marker}")

    if gt == 70:
        classic_ids.append(dg_id)
    else:
        non_classic_ids.append(dg_id)

print(f"\n  Summary: {len(classic_ids)} Classic (gameType=70), {len(non_classic_ids)} non-Classic")

# ══════════════════════════════════════════════════════════════════════
#  SECTION 2 — DraftGroup Detail for each Classic DG
# ══════════════════════════════════════════════════════════════════════
print("\n┌─────────────────────────────────────────────────────────────────┐")
print("│  2. CLASSIC DraftGroup DETAILS (games inside each)            │")
print("└─────────────────────────────────────────────────────────────────┘")

import re

def parse_dk_dt(dt_str):
    if not dt_str:
        return None
    try:
        clean = re.sub(r"\.(\d{6})\d*Z$", r".\1+00:00", dt_str)
        if clean.endswith("Z"):
            clean = clean[:-1] + "+00:00"
        return datetime.fromisoformat(clean)
    except Exception:
        try:
            return datetime.fromisoformat(dt_str[:19]).replace(tzinfo=timezone.utc)
        except Exception:
            return None

classic_details = {}
for dg_id in classic_ids:
    url = f"https://api.draftkings.com/draftgroups/v1/{dg_id}"
    try:
        r = httpx.get(url, headers={"User-Agent": USER_AGENT}, timeout=15, follow_redirects=True)
        data = r.json().get("draftGroup", {})
    except Exception as e:
        print(f"\n  ✗ DG {dg_id} fetch failed: {e}")
        continue

    games = data.get("games", [])
    min_start = parse_dk_dt(data.get("minStartTime", ""))
    max_start = parse_dk_dt(data.get("maxStartTime", ""))

    et_dates = set()
    for g in games:
        s = parse_dk_dt(g.get("startDate", ""))
        if s:
            et_dates.add(to_et_date(s))

    classic_details[dg_id] = {
        "game_count": len(games),
        "min_start": min_start,
        "max_start": max_start,
        "games": games,
        "et_dates": et_dates,
    }

    on_target = TARGET in et_dates
    flag = "✓" if on_target else "✗ OFF-DATE"

    print(f"\n  DG {dg_id}  |  {len(games)} games  |  {to_et(min_start)} → {to_et(max_start)}  |  ET dates: {sorted(et_dates)}  [{flag}]")
    print(f"  {'Matchup':20s}  {'Start (UTC)':25s}  {'Start (ET)':15s}  {'ET Date':12s}  {'Status'}")
    print(f"  {'─'*20}  {'─'*25}  {'─'*15}  {'─'*12}  {'─'*15}")
    for g in games:
        desc = g.get("description", "?")
        start = parse_dk_dt(g.get("startDate", ""))
        status = g.get("status", "?")
        print(f"  {desc:20s}  {str(start):25s}  {to_et(start):15s}  {to_et_date(start):12s}  {status}")


# ══════════════════════════════════════════════════════════════════════
#  SECTION 3 — Sticky Cache Analysis
# ══════════════════════════════════════════════════════════════════════
print("\n┌─────────────────────────────────────────────────────────────────┐")
print("│  3. STICKY CACHE — what the server remembers                  │")
print("└─────────────────────────────────────────────────────────────────┘")

STICKY_PATH = Path(__file__).resolve().parent.parent / "cache" / "dk_slate_sticky.json"
if STICKY_PATH.exists():
    sticky = json.loads(STICKY_PATH.read_text(encoding="utf-8"))
    nba_key = f"nba:{TARGET}"
    if nba_key in sticky:
        entries = sticky[nba_key]
        print(f"\n  Key '{nba_key}' has {len(entries)} DraftGroups:\n")
        for dg_id_str, info in entries.items():
            min_s = parse_dk_dt(info.get("min_start_utc", ""))
            max_s = parse_dk_dt(info.get("max_start_utc", ""))
            gc = info.get("game_count", "?")
            label = info.get("label", "?")
            in_lobby = int(dg_id_str) in classic_ids
            lobby_flag = "✓ in lobby" if in_lobby else "⚠ NOT in lobby (stale?)"
            print(f"  DG {dg_id_str:>8}  |  {gc} games  |  label={label!r:25s}  |  {to_et(min_s)} → {to_et(max_s)}  [{lobby_flag}]")
            for g in info.get("games", []):
                desc = g.get("description", "?")
                print(f"    └ {desc}")
    else:
        print(f"\n  No '{nba_key}' key.  Keys present: {list(sticky.keys())}")

    # Check for stale keys (other dates that might be bleeding through)
    stale_keys = [k for k in sticky if k.startswith("nba:") and k != nba_key]
    if stale_keys:
        print(f"\n  ⚠ Found stale NBA keys in sticky cache: {stale_keys}")
        for k in stale_keys:
            print(f"    {k}: {len(sticky[k])} DraftGroups")
else:
    print(f"\n  No sticky cache file at: {STICKY_PATH}")


# ══════════════════════════════════════════════════════════════════════
#  SECTION 4 — Local Scoreboard API (what the frontend sees)
# ══════════════════════════════════════════════════════════════════════
print("\n┌─────────────────────────────────────────────────────────────────┐")
print("│  4. SCOREBOARD ENDPOINT — what the frontend actually renders  │")
print("└─────────────────────────────────────────────────────────────────┘")

import requests

SCOREBOARD_URL = f"http://localhost:8000/api/scoreboard?game_date={TARGET}&sport=nba"
try:
    r = requests.get(SCOREBOARD_URL, timeout=20)
    if r.status_code == 200:
        sched = r.json()
        slates = sched.get("slates", [])
        games = sched.get("games", [])
        print(f"\n  Total games from NBA API: {sched.get('game_count', '?')}")
        print(f"  Total slates returned:    {len(slates)}")
        print()

        for i, sl in enumerate(slates, 1):
            dg = sl.get("draft_group_id")
            in_lobby = dg in classic_ids if dg else False
            lobby_flag = ""
            if dg is None:
                lobby_flag = "  ⚠ NO DK DRAFT_GROUP (orphan slate)"
            elif not in_lobby:
                lobby_flag = "  ⚠ DG NOT in today's lobby (stale?)"
            else:
                lobby_flag = "  ✓ in lobby"

            print(f"  Slate {i}: {sl['name']:10s}  |  label={sl.get('label','?')!r}")
            print(f"           draft_group_id={dg}  |  game_count={sl['game_count']}  |  is_live={sl.get('is_live')}  |  late_swap={sl.get('late_swap_active')}{lobby_flag}")

            for g in sl.get("games", []):
                home = g.get("home_team", {}).get("team_abbreviation", "?")
                away = g.get("away_team", {}).get("team_abbreviation", "?")
                status = g.get("game_status", "?")
                time_et = g.get("game_time_et", "?")
                print(f"           └ {away:4s} @ {home:4s}  {time_et:12s}  [{status}]")
            print()

        # ── Identify the phantom ──
        if len(slates) > 4:
            print("  " + "═" * 60)
            print(f"  ⚠ PHANTOM DETECTED: {len(slates)} slates (expected ≤ 4)")
            print("  " + "═" * 60)
            for i, sl in enumerate(slates, 1):
                dg = sl.get("draft_group_id")
                if dg is None:
                    print(f"\n  → Slate {i} ({sl['name']!r}) has NO draft_group_id.")
                    print(f"    This is an ORPHAN slate — games that weren't matched")
                    print(f"    to any DK DraftGroup. These games are:")
                    for g in sl.get("games", []):
                        home = g.get("home_team", {}).get("team_abbreviation", "?")
                        away = g.get("away_team", {}).get("team_abbreviation", "?")
                        print(f"      {away} @ {home}")
                    print(f"\n    FIX: Likely a team abbreviation mismatch, or these games")
                    print(f"    are in a Showdown-only DraftGroup and not in Classic.")

                elif dg not in classic_ids:
                    print(f"\n  → Slate {i} ({sl['name']!r}) DG={dg} is NOT in today's lobby.")
                    print(f"    This is a STALE entry from the sticky cache that wasn't pruned.")
                    print(f"    FIX: Clear sticky cache for this DG or tighten expiry logic.")

        # Also check: are there games in multiple slates?
        print("\n  ── Game-to-Slate Membership ──")
        game_slates = {}  # game_id → [slate_names]
        for sl in slates:
            for g in sl.get("games", []):
                gid = g.get("game_id", "?")
                home = g.get("home_team", {}).get("team_abbreviation", "?")
                away = g.get("away_team", {}).get("team_abbreviation", "?")
                key = f"{away}@{home}"
                game_slates.setdefault(key, []).append(sl["name"])

        for key, slate_names in sorted(game_slates.items()):
            dup = "  ◄ MULTI-SLATE" if len(slate_names) > 1 else ""
            print(f"  {key:12s} → {', '.join(slate_names)}{dup}")

    else:
        print(f"\n  ✗ Scoreboard returned {r.status_code}: {r.text[:200]}")
except requests.ConnectionError:
    print(f"\n  ✗ Server not running at localhost:8000. Start the server first.")
except Exception as e:
    print(f"\n  ✗ Scoreboard error: {e}")


# ══════════════════════════════════════════════════════════════════════
#  SECTION 5 — Diagnosis Summary
# ══════════════════════════════════════════════════════════════════════
print("\n┌─────────────────────────────────────────────────────────────────┐")
print("│  5. DIAGNOSIS SUMMARY                                        │")
print("└─────────────────────────────────────────────────────────────────┘")

print(f"\n  DK Lobby Classic DraftGroups: {len(classic_ids)}")
print(f"  Sticky Cache DraftGroups:     {len(sticky.get(f'nba:{TARGET}', {})) if STICKY_PATH.exists() else 'N/A'}")
print(f"  Non-Classic DraftGroups:      {len(non_classic_ids)} (Showdown/Tiers/etc)")
if non_classic_ids:
    for nci in non_classic_ids:
        gt = dg_from_root.get(nci, {}).get("gameTypeId") or dg_from_contests.get(nci, {}).get("gameTypeId")
        tn = dg_from_root.get(nci, {}).get("game_type_name", "?")
        gc = dg_from_root.get(nci, {}).get("game_count", "?")
        print(f"    DG {nci}: gameType={gt} ({tn}), {gc} games")

print("\n  Possible phantom causes:")
print("    1. Orphan slate — games from NBA API not in any Classic DraftGroup")
print("    2. Stale sticky cache — DG from a previous date not yet expired")
print("    3. Non-Classic DG leaking through — Showdown/Tiers with wrong gameTypeId")
print("    4. Date bleed — DG games span midnight UTC, ET conversion mismatch")
print()
print("=" * 90)
