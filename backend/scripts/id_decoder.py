"""DK Game-Type ID Decoder — scrape the DraftKings lobby for every unique
ContestTypeId (a.k.a. GameTypeId), hit the hidden rules API for each, and
produce a clean reference table + JSON export.

Endpoints used:
  Lobby       → https://www.draftkings.com/lobby/getcontests?sport=NBA
  DraftGroup  → https://api.draftkings.com/draftgroups/v1/{dg_id}
  Rules       → https://api.draftkings.com/lineups/v1/gametypes/{game_type_id}/rules

Usage (from backend/):
    ./venv/Scripts/python scripts/id_decoder.py [--sport NBA|MLB|NFL|CBB|ALL] [--out config/draftkings_game_types.json]
"""

import argparse
import io
import json
import sys
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path

# ── Fix Windows console encoding ──────────────────────────────────────
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import httpx

# ── Constants ─────────────────────────────────────────────────────────
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

DK_LOBBY_URLS = {
    "NBA": "https://www.draftkings.com/lobby/getcontests?sport=NBA",
    "NFL": "https://www.draftkings.com/lobby/getcontests?sport=NFL",
    "MLB": "https://www.draftkings.com/lobby/getcontests?sport=MLB",
    "CBB": "https://www.draftkings.com/lobby/getcontests?sport=CBB",
}

DK_RULES_URL = "https://api.draftkings.com/lineups/v1/gametypes/{game_type_id}/rules"
DK_DG_URL = "https://api.draftkings.com/draftgroups/v1/{dg_id}"

HEADERS = {"User-Agent": USER_AGENT}
TIMEOUT = 15

# Known game type labels (pre-populated for convenience)
KNOWN_LABELS = {
    1:   "Classic (Legacy)",
    70:  "NBA Classic",
    73:  "Tiers",
    81:  "Showdown Captain Mode",
    96:  "NBA Tiers",
    98:  "CBB Classic",
    111: "Showdown (Alt)",
    145: "Flash",
    167: "Pick6",
}


# ── Helpers ───────────────────────────────────────────────────────────

def fetch_json(url: str, label: str = "") -> dict | None:
    """GET a URL and return parsed JSON, or None on failure."""
    try:
        resp = httpx.get(url, headers=HEADERS, timeout=TIMEOUT, follow_redirects=True)
        if resp.status_code != 200:
            print(f"  ✗ {label or url}  →  HTTP {resp.status_code}")
            return None
        return resp.json()
    except Exception as e:
        print(f"  ✗ {label or url}  →  {e}")
        return None


def extract_game_type_ids_from_lobby(lobby: dict) -> dict[int, dict]:
    """Pull every unique GameTypeId from Contests + DraftGroups arrays.

    Returns {game_type_id: {dg_ids: set, contest_count: int, sample_name: str,
                            game_type_name: str}}
    """
    result: dict[int, dict] = {}

    # ── Contests array ────────────────────────────────────────────────
    for c in lobby.get("Contests", []) or lobby.get("contests", []):
        gt = c.get("gameTypeId") or c.get("GameTypeId")
        dg = c.get("dg") or c.get("DraftGroupId")
        name = c.get("n") or c.get("ContestName", "")
        if gt is None:
            continue
        gt = int(gt)
        if gt not in result:
            result[gt] = {
                "dg_ids": set(),
                "contest_count": 0,
                "sample_name": "",
                "game_type_name": "",
            }
        result[gt]["contest_count"] += 1
        if dg:
            result[gt]["dg_ids"].add(int(dg))
        if name and not result[gt]["sample_name"]:
            result[gt]["sample_name"] = name[:60]

    # ── DraftGroups root array ────────────────────────────────────────
    for dg in lobby.get("DraftGroups", []) or []:
        gt = dg.get("GameTypeId") or dg.get("gameTypeId")
        dg_id = dg.get("DraftGroupId") or dg.get("draftGroupId")
        gtn = dg.get("GameTypeName", "")
        if gt is None:
            continue
        gt = int(gt)
        if gt not in result:
            result[gt] = {
                "dg_ids": set(),
                "contest_count": 0,
                "sample_name": "",
                "game_type_name": gtn or "",
            }
        if dg_id:
            result[gt]["dg_ids"].add(int(dg_id))
        if gtn and not result[gt]["game_type_name"]:
            result[gt]["game_type_name"] = gtn

    return result


def query_rules_api(game_type_id: int) -> dict | None:
    """Hit the DK rules endpoint and return parsed constraints.

    Actual response shape (discovered via live probing):
      - Top-level keys: gameTypeName, gameTypeDescription, salaryCap,
        lineupTemplate, gameCount, uniquePlayers, allowLateSwap,
        draftType, rulesUrl, etc.
      - salaryCap: {isEnabled, minValue, maxValue}
      - lineupTemplate[]: {rosterSlot: {id, name, description}, order}
      - No nested "gameType" object — name/desc are top-level.
      - scoringRules may be empty [] for some endpoints.
    """
    url = DK_RULES_URL.format(game_type_id=game_type_id)
    data = fetch_json(url, label=f"Rules API (gameType={game_type_id})")
    if not data:
        return None

    # ── Parse salary cap ──────────────────────────────────────────────
    # Shape: salaryCap: {isEnabled: true, minValue: 0, maxValue: 50000}
    cap_obj = data.get("salaryCap") or {}
    salary_cap = None
    min_salary = None
    if cap_obj.get("isEnabled"):
        max_val = cap_obj.get("maxValue")
        if max_val is not None and max_val > 0:
            salary_cap = int(max_val)
        min_val = cap_obj.get("minValue")
        if min_val is not None and min_val > 0:
            min_salary = int(min_val)

    # ── Parse roster slots ────────────────────────────────────────────
    # Shape: lineupTemplate[]: {rosterSlot: {id, name, description}, order}
    roster_slots = []
    for slot in data.get("lineupTemplate", []) or []:
        rs = slot.get("rosterSlot", {})
        slot_name = rs.get("name", "?")
        slot_desc = rs.get("description", "")
        roster_slots.append({
            "name": slot_name,
            "description": slot_desc,
        })

    # ── Parse game type name / description ────────────────────────────
    # Top-level keys, NOT nested under "gameType"
    game_type_name = data.get("gameTypeName", "")
    game_type_desc = data.get("gameTypeDescription", "")

    # ── Roster constraints ────────────────────────────────────────────
    unique_players = data.get("uniquePlayers")
    allow_late_swap = data.get("allowLateSwap")
    game_count = data.get("gameCount")
    draft_type = data.get("draftType", "")
    rules_url = data.get("rulesUrl", "")

    # ── Scoring rules ─────────────────────────────────────────────────
    scoring_rules = []
    for cat in data.get("scoringRules", []) or []:
        cat_name = cat.get("name", "?")
        for rule in cat.get("scoringRuleEntries", []) or []:
            stat_name = rule.get("description", rule.get("name", "?"))
            points = rule.get("points")
            if points is not None:
                scoring_rules.append({
                    "category": cat_name,
                    "stat": stat_name,
                    "points": float(points),
                })

    # ── Team position limits ──────────────────────────────────────────
    team_limits = data.get("teamPositionLimits", []) or []

    return {
        "game_type_id": game_type_id,
        "name": game_type_name,
        "description": game_type_desc,
        "salary_cap": salary_cap,
        "min_salary_value": min_salary,
        "roster_size": len(roster_slots),
        "roster_slots": roster_slots,
        "unique_players": unique_players,
        "allow_late_swap": allow_late_swap,
        "game_count": game_count,
        "draft_type": draft_type,
        "rules_url": rules_url,
        "scoring_rules": scoring_rules,
        "team_position_limits": team_limits,
    }


def query_sample_draftgroup(dg_id: int) -> dict | None:
    """Fetch a DraftGroup detail for additional metadata."""
    url = DK_DG_URL.format(dg_id=dg_id)
    data = fetch_json(url, label=f"DraftGroup {dg_id}")
    if not data:
        return None
    dg = data.get("draftGroup", {})
    return {
        "dg_id": dg_id,
        "game_count": len(dg.get("games", [])),
        "contest_type_name": dg.get("contestType", {}).get("name", ""),
        "sport": dg.get("sport", ""),
        "min_start": dg.get("minStartTime", ""),
        "max_start": dg.get("maxStartTime", ""),
    }


# ── Main ──────────────────────────────────────────────────────────────

def decode_sport(sport: str, all_results: dict) -> None:
    """Decode all game type IDs for a single sport."""
    url = DK_LOBBY_URLS.get(sport)
    if not url:
        print(f"  ✗ Unknown sport: {sport}")
        return

    print(f"\n{'═' * 80}")
    print(f"  SPORT: {sport}")
    print(f"{'═' * 80}")

    # ── 1. Fetch lobby ────────────────────────────────────────────────
    print(f"\n  Fetching {sport} lobby...")
    lobby = fetch_json(url, label=f"{sport} Lobby")
    if not lobby:
        print(f"  ✗ Could not fetch {sport} lobby. Skipping.")
        return

    n_contests = len(lobby.get("Contests", []) or lobby.get("contests", []))
    n_dgs = len(lobby.get("DraftGroups", []) or [])
    print(f"  ✓ Lobby: {n_contests} contests, {n_dgs} DraftGroups")

    # ── 2. Extract unique game type IDs ───────────────────────────────
    gt_map = extract_game_type_ids_from_lobby(lobby)
    if not gt_map:
        print(f"  ✗ No game type IDs found for {sport}")
        return

    sorted_ids = sorted(gt_map.keys())
    print(f"  ✓ Found {len(sorted_ids)} unique GameTypeId(s): {sorted_ids}")

    # ── 3. Query rules API for each ───────────────────────────────────
    print(f"\n  ┌{'─' * 76}┐")
    print(f"  │  Querying Rules API for each GameTypeId...{' ' * 31}│")
    print(f"  └{'─' * 76}┘")

    for gt_id in sorted_ids:
        info = gt_map[gt_id]
        n_dg = len(info["dg_ids"])
        n_cont = info["contest_count"]

        print(f"\n  ── GameTypeId = {gt_id} ──────────────────────────────────")
        print(f"     Lobby:  {n_dg} DraftGroup(s), {n_cont} contest(s)")
        if info["game_type_name"]:
            print(f"     Lobby name: {info['game_type_name']}")
        if info["sample_name"]:
            print(f"     Sample contest: {info['sample_name']}")

        # Query rules
        time.sleep(0.15)  # gentle rate limit
        rules = query_rules_api(gt_id)

        if rules:
            print(f"     ✓ Rules API responded:")
            print(f"       Name:        {rules['name'] or '(empty)'}")
            if rules["description"]:
                print(f"       Description: {rules['description'][:80]}")
            print(f"       Salary cap:  ${rules['salary_cap']:,}" if rules["salary_cap"] else "       Salary cap:  N/A (draft-style)")
            if rules["draft_type"]:
                print(f"       Draft type:  {rules['draft_type']}")
            slot_names = [s["name"] for s in rules["roster_slots"]]
            print(f"       Roster:      {rules['roster_size']} slots → {', '.join(slot_names)}")
            if rules["allow_late_swap"] is not None:
                print(f"       Late swap:   {'Yes' if rules['allow_late_swap'] else 'No'}")
            if rules["unique_players"] is not None:
                print(f"       Unique players: {rules['unique_players']}")
            if rules["rules_url"]:
                print(f"       Rules URL:   {rules['rules_url']}")

            if rules["scoring_rules"]:
                print(f"       Scoring ({len(rules['scoring_rules'])} rules):")
                for sr in rules["scoring_rules"][:12]:  # Cap display at 12
                    sign = "+" if sr["points"] >= 0 else ""
                    print(f"         {sr['stat']:30s}  {sign}{sr['points']:.1f} pts")
                if len(rules["scoring_rules"]) > 12:
                    print(f"         ... and {len(rules['scoring_rules']) - 12} more")
        else:
            print(f"     ✗ Rules API returned no data (may be deprecated or sport-specific)")
            rules = {
                "game_type_id": gt_id,
                "name": info.get("game_type_name") or KNOWN_LABELS.get(gt_id, f"Unknown({gt_id})"),
                "description": "",
                "salary_cap": None,
                "min_salary_value": None,
                "roster_size": 0,
                "roster_slots": [],
                "scoring_rules": [],
                "unique_players": None,
                "allow_late_swap": None,
                "game_count": None,
                "draft_type": "",
                "rules_url": "",
                "team_position_limits": [],
            }

        # Query one sample DraftGroup for extra metadata
        sample_dg = None
        if info["dg_ids"]:
            sample_dg_id = sorted(info["dg_ids"])[0]
            time.sleep(0.15)
            sample_dg = query_sample_draftgroup(sample_dg_id)
            if sample_dg:
                print(f"     ✓ Sample DG {sample_dg_id}: {sample_dg['game_count']} games, "
                      f"type='{sample_dg['contest_type_name']}'")

        # Compile result
        key = f"{sport}:{gt_id}"
        all_results[key] = {
            "sport": sport,
            "game_type_id": gt_id,
            "name": rules["name"] or info.get("game_type_name") or KNOWN_LABELS.get(gt_id, ""),
            "description": rules.get("description", ""),
            "lobby_name": info.get("game_type_name", ""),
            "salary_cap": rules["salary_cap"],
            "min_salary_value": rules.get("min_salary_value"),
            "roster_size": rules["roster_size"],
            "roster_slots": [s["name"] for s in rules["roster_slots"]],
            "roster_slot_details": rules["roster_slots"],
            "unique_players": rules.get("unique_players"),
            "allow_late_swap": rules.get("allow_late_swap"),
            "draft_type": rules.get("draft_type", ""),
            "rules_url": rules.get("rules_url", ""),
            "scoring_rules": rules.get("scoring_rules", []),
            "team_position_limits": rules.get("team_position_limits", []),
            "lobby_stats": {
                "draft_group_count": n_dg,
                "contest_count": n_cont,
                "sample_contest": info.get("sample_name", ""),
            },
            "sample_draft_group": sample_dg,
        }


def print_summary(all_results: dict) -> None:
    """Print a clean summary table."""
    print(f"\n\n{'═' * 90}")
    print(f"  SUMMARY — All Decoded DraftKings Game Types")
    print(f"{'═' * 90}")

    # Header
    print(f"\n  {'Sport':5s}  {'ID':>5s}  {'Name':28s}  {'Cap':>9s}  {'Draft':12s}  {'Slots':>5s}  {'Roster':42s}  {'#DGs':>4s}  {'#Contests':>9s}")
    print(f"  {'─'*5}  {'─'*5}  {'─'*28}  {'─'*9}  {'─'*12}  {'─'*5}  {'─'*42}  {'─'*4}  {'─'*9}")

    for key in sorted(all_results.keys()):
        r = all_results[key]
        cap_str = f"${r['salary_cap']:,}" if r['salary_cap'] else "N/A"
        draft = (r.get("draft_type") or "salary")[:12]
        slots_str = ", ".join(r["roster_slots"])[:42] if r["roster_slots"] else "N/A"
        n_slots = str(r["roster_size"]) if r["roster_size"] else "?"
        name = (r["name"] or r.get("lobby_name", "") or "?")[:28]
        n_dgs = str(r["lobby_stats"]["draft_group_count"])
        n_cont = str(r["lobby_stats"]["contest_count"])

        print(f"  {r['sport']:5s}  {r['game_type_id']:>5d}  {name:28s}  {cap_str:>9s}  {draft:12s}  {n_slots:>5s}  {slots_str:42s}  {n_dgs:>4s}  {n_cont:>9s}")

    print()


def export_json(all_results: dict, out_path: Path) -> None:
    """Write results to JSON, stripping sets and adding metadata."""
    export = {
        "_meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "description": "DraftKings GameTypeId → rules mapping, scraped from lobby + rules API",
            "script": "scripts/id_decoder.py",
        },
        "game_types": {},
    }

    for key in sorted(all_results.keys()):
        r = dict(all_results[key])
        # Remove sample_draft_group for cleaner export (or keep it — user choice)
        export["game_types"][key] = r

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(export, indent=2, default=str), encoding="utf-8")
    print(f"  ✓ Exported {len(export['game_types'])} game types → {out_path}")


def main():
    parser = argparse.ArgumentParser(
        description="Decode DraftKings ContestTypeId values via lobby + rules API"
    )
    parser.add_argument(
        "--sport", default="NBA",
        help="Sport to query (NBA, NFL, MLB, CBB, ALL). Default: NBA",
    )
    parser.add_argument(
        "--out", default="config/draftkings_game_types.json",
        help="Output JSON path (relative to backend/). Default: config/draftkings_game_types.json",
    )
    args = parser.parse_args()

    sport = args.sport.upper()
    out_path = Path(__file__).resolve().parent.parent / args.out

    print("=" * 90)
    print(f"  DK GAME-TYPE ID DECODER")
    print(f"  Sport(s): {sport}  |  Output: {out_path}")
    print("=" * 90)

    all_results: dict = OrderedDict()

    if sport == "ALL":
        for s in DK_LOBBY_URLS:
            decode_sport(s, all_results)
            time.sleep(0.5)  # gentle inter-sport delay
    else:
        decode_sport(sport, all_results)

    if not all_results:
        print("\n  ✗ No game types decoded. Check network / DK lobby availability.")
        sys.exit(1)

    # ── Summary table ─────────────────────────────────────────────────
    print_summary(all_results)

    # ── JSON export ───────────────────────────────────────────────────
    export_json(all_results, out_path)

    print("=" * 90)
    print(f"  Done. {len(all_results)} game types decoded.")
    print("=" * 90)


if __name__ == "__main__":
    main()
