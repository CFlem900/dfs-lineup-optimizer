#!/usr/bin/env python3
"""Backfill DraftKings NBA contest results into RotationEngine.

The DK lobby API only returns *active* contests, so historical contest
discovery uses one of two strategies:

  1. **Entry history CSV** (recommended) -- Download your DK entry history
     from My Contests > History > Download Entry History, then pass it here.
     The script extracts contest IDs from that CSV and downloads full standings.

  2. **Direct contest IDs** -- If you already know the contest IDs, supply
     them via --contest-ids.

Usage
-----
  # Strategy 1 -- entry history CSV (recommended):
  python scripts/dk_backfill_month.py --entry-history entry-history.csv --cookie @dk_cookie.txt

  # Strategy 2 -- direct contest IDs:
  python scripts/dk_backfill_month.py --contest-ids 12345,67890,11111 --cookie @dk_cookie.txt

  # Import previously downloaded CSVs from data dir:
  python scripts/dk_backfill_month.py --import-only

  # Download-only mode (no auto-import):
  python scripts/dk_backfill_month.py --entry-history entry-history.csv --no-import --cookie @dk_cookie.txt

  # Filter by date range:
  python scripts/dk_backfill_month.py --entry-history entry-history.csv --from 2026-01-15 --to 2026-02-12 --cookie @dk_cookie.txt

How It Works
------------
1. Authenticates via Chrome cookies (browser_cookie3) or manual --cookie
2. Reads contest IDs from entry history CSV or --contest-ids
3. Optionally filters by date range and contest type
4. Downloads full standings CSV from exportfullstandingscsv endpoint
5. Imports into RotationEngine via the local API
6. Runs AI pattern analysis on imported data

Requirements
------------
  pip install requests browser_cookie3

Notes
-----
- DK entry history CSV is downloaded from: My Contests > History > Download Entry History
- Contest standings CSVs are cached in backend/data/tournament_csvs/
- Large contests (10K+) may return ZIPs (handled automatically)
- DK may not retain contest data beyond ~7 days after completion
"""

import argparse
import csv
import io
import json
import os
import re
import sys
import time
import zipfile
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Dict, List, Optional

try:
    import requests
except ImportError:
    print("ERROR: requests is required. Run: pip install requests")
    sys.exit(1)

# -- Paths ------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
BACKEND_DIR = SCRIPT_DIR.parent
DATA_DIR = BACKEND_DIR / "data" / "tournament_csvs"
DATA_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = DATA_DIR / "_backfill_state.json"

# -- DraftKings URLs --------------------------------------------------------
DK_BASE = "https://www.draftkings.com"
DK_API = "https://api.draftkings.com"
DK_EXPORT_URL = f"{DK_BASE}/contest/exportfullstandingscsv/{{contest_id}}"

# -- Local API --------------------------------------------------------------
LOCAL_API = "http://localhost:8000/api"

# -- Browser headers --------------------------------------------------------
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.draftkings.com/lobby",
}


# -- State management -------------------------------------------------------

def load_state() -> Dict:
    """Load backfill progress state."""
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"completed_dates": [], "downloaded_contests": [], "failed_contests": []}


def save_state(state: Dict):
    """Persist backfill progress."""
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# -- DraftKings authentication ----------------------------------------------

def _try_copy_chrome_cookies() -> Optional["http.cookiejar.CookieJar"]:
    """Try to read Chrome cookies by copying the DB (works while Chrome is open)."""
    import shutil
    import sqlite3
    import tempfile
    import http.cookiejar

    local_app = os.environ.get("LOCALAPPDATA", "")
    cookie_paths = [
        os.path.join(local_app, "Google", "Chrome", "User Data", "Default", "Cookies"),
        os.path.join(local_app, "Google", "Chrome", "User Data", "Default", "Network", "Cookies"),
        os.path.join(local_app, "Google", "Chrome", "User Data", "Profile 1", "Cookies"),
        os.path.join(local_app, "Google", "Chrome", "User Data", "Profile 1", "Network", "Cookies"),
    ]

    db_path = None
    for p in cookie_paths:
        if os.path.isfile(p):
            db_path = p
            break

    if not db_path:
        return None

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    try:
        shutil.copy2(db_path, tmp.name)
    except PermissionError:
        os.unlink(tmp.name)
        return None

    jar = http.cookiejar.CookieJar()
    try:
        conn = sqlite3.connect(tmp.name)
        cursor = conn.execute(
            "SELECT name, value, host_key, path, expires_utc, is_secure "
            "FROM cookies WHERE host_key LIKE '%draftkings.com%'"
        )
        for name, value, host, path, expires, secure in cursor.fetchall():
            if not value:
                continue
            cookie = http.cookiejar.Cookie(
                version=0, name=name, value=value,
                port=None, port_specified=False,
                domain=host, domain_specified=True, domain_initial_dot=host.startswith("."),
                path=path, path_specified=True,
                secure=bool(secure), expires=expires or None,
                discard=False, comment=None, comment_url=None,
                rest={}, rfc2109=False,
            )
            jar.set_cookie(cookie)
        conn.close()
    except Exception:
        pass
    finally:
        os.unlink(tmp.name)

    return jar if len(jar) > 0 else None


def _load_cookie_str(cookie_arg: Optional[str]) -> Optional[str]:
    """Resolve cookie from --cookie arg (raw string or @filepath)."""
    if not cookie_arg:
        return None
    if cookie_arg.startswith("@"):
        fpath = Path(cookie_arg[1:])
        if not fpath.exists():
            for base in (SCRIPT_DIR, BACKEND_DIR, DATA_DIR):
                candidate = base / cookie_arg[1:]
                if candidate.exists():
                    fpath = candidate
                    break
        if fpath.exists():
            txt = fpath.read_text(encoding="utf-8").strip()
            print(f"[*] Loaded cookie from {fpath}")
            return txt
        else:
            print(f"[!] Cookie file not found: {cookie_arg[1:]}")
            sys.exit(1)
    return cookie_arg


def get_dk_session(cookie_str: Optional[str] = None) -> requests.Session:
    """Create an authenticated DraftKings session."""
    session = requests.Session()
    session.headers.update(HEADERS)

    cookie_str = _load_cookie_str(cookie_str)
    if cookie_str:
        print("[*] Using manually provided cookie")
        session.headers["Cookie"] = cookie_str
        return session

    # Try browser_cookie3 first
    try:
        import browser_cookie3
        cj = browser_cookie3.chrome(domain_name=".draftkings.com")
        session.cookies = cj
        cookie_names = [c.name for c in cj]
        if "DSID" not in cookie_names and "sessionToken" not in cookie_names:
            print("[!] WARNING: No DraftKings auth cookies found in Chrome.")
            print("    Make sure you are logged into draftkings.com in Chrome.")
        else:
            auth_cookies = [n for n in cookie_names if n in ("DSID", "sessionToken", "csid")]
            print(f"[*] Loaded DK cookies from Chrome: {', '.join(auth_cookies)}")
        return session
    except ImportError:
        pass
    except Exception:
        pass

    # Fallback: copy Chrome cookie DB manually
    print("[*] browser_cookie3 failed, trying direct cookie DB copy...")
    jar = _try_copy_chrome_cookies()
    if jar:
        session.cookies = jar
        cookie_names = [c.name for c in jar]
        auth_cookies = [n for n in cookie_names if n in ("DSID", "sessionToken", "csid")]
        if auth_cookies:
            print(f"[*] Loaded DK cookies via DB copy: {', '.join(auth_cookies)}")
            return session
        else:
            print("[!] Found cookies but no DK auth tokens.")

    print("[!] Could not load DraftKings cookies automatically.")
    print("")
    print("    Option 1: Close Chrome completely, then re-run this script")
    print("    Option 2: Provide cookies manually:")
    print("      1. Open Chrome -> draftkings.com (logged in)")
    print("      2. Press F12 -> Application tab -> Cookies -> draftkings.com")
    print("      3. Copy the cookie header string into a file, e.g. dk_cookie.txt")
    print("      4. Run: python scripts/dk_backfill_month.py --cookie @dk_cookie.txt")
    sys.exit(1)


# -- Entry history parsing --------------------------------------------------

def parse_entry_history(
    history_path: str,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    sport_filter: str = "NBA",
    contest_type_filter: Optional[str] = None,
) -> List[Dict]:
    """Parse a DraftKings entry history CSV to extract contest info.

    DK entry history CSV typically has columns like:
      Contest_Key (or Contest Key), Contest_Date_EST (or Contest Date EST),
      Sport, Contest_Name, Entry_Fee, Place, Points, Winnings, etc.

    Returns list of dicts: {id, name, date, sport, entry_fee, place, points, winnings}
    """
    fpath = Path(history_path)
    if not fpath.exists():
        # Try relative to common locations
        for base in (Path.cwd(), SCRIPT_DIR, BACKEND_DIR, DATA_DIR):
            candidate = base / history_path
            if candidate.exists():
                fpath = candidate
                break

    if not fpath.exists():
        print(f"[!] Entry history file not found: {history_path}")
        sys.exit(1)

    print(f"[*] Parsing entry history: {fpath}")

    # Read with BOM handling
    raw = fpath.read_bytes()
    if raw[:3] == b"\xef\xbb\xbf":
        raw = raw[3:]
    text = raw.decode("utf-8", errors="replace")

    reader = csv.DictReader(io.StringIO(text))

    # Normalize column names (DK uses various formats)
    fieldnames = reader.fieldnames or []
    col_map = {}
    for fn in fieldnames:
        clean = fn.strip().lstrip("\ufeff").strip()
        key = clean.lower().replace(" ", "_").replace("-", "_")
        col_map[key] = fn

    def _get(row: dict, *keys: str) -> str:
        """Get value from row using flexible key matching."""
        for k in keys:
            # Direct
            if k in row and row[k]:
                return str(row[k]).strip()
            # Via col_map
            if k in col_map and col_map[k] in row and row[col_map[k]]:
                return str(row[col_map[k]]).strip()
        return ""

    contests = {}  # keyed by contest_id to deduplicate
    skipped_sport = 0
    skipped_date = 0
    skipped_type = 0

    for row in reader:
        # Extract contest ID
        contest_id = _get(row, "contest_key", "contestkey", "contest_id", "contestid",
                         "contest key", "contest id")
        if not contest_id:
            continue

        # Try to parse as integer
        try:
            contest_id_int = int(float(contest_id))
        except (ValueError, TypeError):
            continue

        # Sport filter
        sport = _get(row, "sport", "sport_name")
        if sport_filter and sport.upper() != sport_filter.upper():
            skipped_sport += 1
            continue

        # Date extraction and filtering
        date_str = _get(row, "contest_date_est", "contest_date", "date",
                       "contest date est", "contest date")
        contest_date = None
        if date_str:
            # Try common date formats
            for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y", "%m/%d/%y",
                       "%Y-%m-%dT%H:%M:%S", "%m/%d/%Y %I:%M %p",
                       "%m/%d/%Y %H:%M"):
                try:
                    contest_date = datetime.strptime(date_str.split(" ")[0] if " " in date_str and "T" not in date_str else date_str[:10], fmt.split(" ")[0] if " " in fmt else fmt).date()
                    break
                except ValueError:
                    continue

        if contest_date:
            if start_date and contest_date < start_date:
                skipped_date += 1
                continue
            if end_date and contest_date > end_date:
                skipped_date += 1
                continue

        # Contest type filter
        contest_name = _get(row, "contest_name", "contest name", "name", "title")
        entry_fee_str = _get(row, "entry_fee", "entry fee", "entryfee", "fee")
        entry_fee = 0.0
        if entry_fee_str:
            try:
                entry_fee = float(entry_fee_str.replace("$", "").replace(",", ""))
            except ValueError:
                pass

        if contest_type_filter and contest_type_filter != "all":
            name_lower = contest_name.lower()
            if contest_type_filter == "gpp":
                # Skip if it looks like cash/H2H
                if any(kw in name_lower for kw in ["head-to-head", "h2h", "50/50", "double up"]):
                    skipped_type += 1
                    continue
            elif contest_type_filter == "cash":
                if not any(kw in name_lower for kw in ["head-to-head", "h2h", "50/50", "double up"]):
                    skipped_type += 1
                    continue

        # Parse place/points/winnings
        place = _get(row, "place", "rank", "position")
        points = _get(row, "points", "fpts", "fantasy_points")
        winnings = _get(row, "winnings", "prize", "payout")

        if str(contest_id_int) not in contests:
            contests[str(contest_id_int)] = {
                "id": contest_id_int,
                "name": contest_name,
                "date": contest_date.isoformat() if contest_date else "",
                "sport": sport,
                "entry_fee": entry_fee,
                "place": place,
                "points": points,
                "winnings": winnings,
            }

    result = list(contests.values())

    # Sort by date (newest first)
    result.sort(key=lambda c: c.get("date", ""), reverse=True)

    print(f"    Found {len(result)} unique NBA contests in entry history")
    if skipped_sport > 0:
        print(f"    Skipped {skipped_sport} non-{sport_filter} entries")
    if skipped_date > 0:
        print(f"    Skipped {skipped_date} entries outside date range")
    if skipped_type > 0:
        print(f"    Skipped {skipped_type} entries not matching type filter")

    return result


# -- Download & Import ------------------------------------------------------

def download_standings_csv(
    session: requests.Session,
    contest_id: int,
    contest_name: str,
    contest_date: str,
) -> Optional[Path]:
    """Download full standings CSV. Returns path or None on failure."""
    safe_name = re.sub(r'[^\w\-]', '_', contest_name)[:50]
    csv_path = DATA_DIR / f"{contest_date}_{contest_id}_{safe_name}.csv"

    # Skip if already downloaded
    if csv_path.exists() and csv_path.stat().st_size > 100:
        return csv_path

    url = DK_EXPORT_URL.format(contest_id=contest_id)

    try:
        resp = session.get(url, timeout=90)

        if resp.status_code in (401, 403):
            print(f"      [!] Auth failed (HTTP {resp.status_code})")
            return None
        if resp.status_code == 404:
            return None

        resp.raise_for_status()
        content = resp.content
        content_type = resp.headers.get("Content-Type", "")

        # Handle ZIP
        if "zip" in content_type or content[:4] == b"PK\x03\x04":
            with zipfile.ZipFile(io.BytesIO(content)) as zf:
                csv_files = [n for n in zf.namelist() if n.endswith(".csv")]
                if not csv_files:
                    return None
                csv_path.write_bytes(zf.read(csv_files[0]))
        else:
            # Verify it looks like CSV, not an HTML error page
            text_preview = content[:200].decode("utf-8", errors="ignore")
            if "<html" in text_preview.lower() or "<title" in text_preview.lower():
                return None
            csv_path.write_bytes(content)

        if csv_path.stat().st_size < 100:
            csv_path.unlink(missing_ok=True)
            return None

        return csv_path

    except Exception:
        return None


def import_to_app(csv_path: Path, contest_date: str, contest_type: str) -> bool:
    """Import CSV into RotationEngine via local API."""
    url = f"{LOCAL_API}/tournament/import"
    params = {
        "contest_date": contest_date,
        "contest_type": contest_type,
        "contest_name": csv_path.stem,
    }

    try:
        with open(csv_path, "rb") as f:
            files = {"file": (csv_path.name, f, "text/csv")}
            resp = requests.post(url, params=params, files=files, timeout=120)

        if resp.ok:
            return True
        return False

    except requests.ConnectionError:
        return False
    except Exception:
        return False


def run_analysis() -> bool:
    """Trigger the AI tournament analysis endpoint."""
    try:
        resp = requests.get(f"{LOCAL_API}/tournament/analysis", timeout=300)
        if resp.ok:
            data = resp.json()
            if data.get("analysis"):
                patterns = data["analysis"].get("patterns", [])
                cals = data["analysis"].get("calibration_adjustments", {})
                print(f"    {len(patterns)} patterns, {len(cals)} calibration adjustments")
                reasoning = data["analysis"].get("reasoning", "")
                if reasoning:
                    print(f"    {reasoning[:200]}")
                return True
            else:
                print(f"    {data.get('message', 'No analysis returned')}")
                return False
        else:
            print(f"    HTTP {resp.status_code}")
            return False
    except requests.ConnectionError:
        print("    [!] Backend not reachable")
        return False
    except Exception as e:
        print(f"    [!] {e}")
        return False


# -- Main -------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Backfill DraftKings NBA contest results into RotationEngine",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Using DK entry history CSV (recommended):
  python scripts/dk_backfill_month.py --entry-history entry-history.csv --cookie @dk_cookie.txt

  # Direct contest IDs:
  python scripts/dk_backfill_month.py --contest-ids 12345,67890 --cookie @dk_cookie.txt

  # Filter by date range:
  python scripts/dk_backfill_month.py --entry-history eh.csv --from 2026-01-15 --to 2026-02-12

  # Import cached CSVs (no download needed):
  python scripts/dk_backfill_month.py --import-only

How to get your DK entry history:
  1. Go to draftkings.com -> My Contests
  2. Click the "History" tab
  3. Click "Download Entry History"
  4. Save the CSV file
  5. Run: python scripts/dk_backfill_month.py --entry-history <file.csv>
""",
    )

    # Contest discovery methods
    discovery = parser.add_argument_group("Contest Discovery (pick one)")
    discovery.add_argument(
        "--entry-history", dest="entry_history", default=None,
        help="Path to DK entry history CSV (download from My Contests > History)",
    )
    discovery.add_argument(
        "--contest-ids", dest="contest_ids", default=None,
        help="Comma-separated list of DK contest IDs to download",
    )

    # Filtering
    filtering = parser.add_argument_group("Filtering")
    filtering.add_argument(
        "--from", dest="date_from", default=None,
        help="Start date YYYY-MM-DD (default: 30 days ago)",
    )
    filtering.add_argument(
        "--to", dest="date_to", default=None,
        help="End date YYYY-MM-DD (default: yesterday)",
    )
    filtering.add_argument(
        "--days", type=int, default=30,
        help="Number of days to look back (default: 30)",
    )
    filtering.add_argument(
        "--type", "-t", default="gpp",
        choices=["gpp", "cash", "single_entry", "all"],
        help="Contest type filter (default: gpp)",
    )
    filtering.add_argument(
        "--min-fee", dest="min_fee", type=float, default=0,
        help="Minimum entry fee filter (default: 0)",
    )

    # Behavior
    behavior = parser.add_argument_group("Behavior")
    behavior.add_argument(
        "--no-import", action="store_true",
        help="Download only, skip import to app",
    )
    behavior.add_argument(
        "--import-only", action="store_true",
        help="Skip downloading, import all cached CSVs from data dir",
    )
    behavior.add_argument(
        "--no-analysis", action="store_true",
        help="Skip running AI analysis after import",
    )
    behavior.add_argument(
        "--max-contests", type=int, default=50,
        help="Max contests to download (default: 50)",
    )
    behavior.add_argument(
        "--resume", action="store_true",
        help="Resume from last backfill progress (skip already downloaded)",
    )

    # Auth
    auth = parser.add_argument_group("Authentication")
    auth.add_argument(
        "--cookie", default=None,
        help="DK cookie string or @filepath (e.g. --cookie @dk_cookie.txt)",
    )

    args = parser.parse_args()

    # Date range
    today = date.today()
    if args.date_to:
        end_date = date.fromisoformat(args.date_to)
    else:
        end_date = today - timedelta(days=1)

    if args.date_from:
        start_date = date.fromisoformat(args.date_from)
    else:
        start_date = end_date - timedelta(days=args.days - 1)

    print("=" * 60)
    print("DraftKings NBA Contest Backfill")
    print("=" * 60)

    # -- Import-only mode ---------------------------------------------------
    if args.import_only:
        csv_files = sorted(DATA_DIR.glob("*.csv"))
        csv_files = [f for f in csv_files if not f.name.startswith("_")]
        if not csv_files:
            print(f"\n[!] No CSV files in {DATA_DIR}")
            sys.exit(1)

        print(f"\n[*] Importing {len(csv_files)} cached CSVs...")
        success = 0
        for i, csv_path in enumerate(csv_files, 1):
            fname = csv_path.stem
            date_match = re.match(r"(\d{4}-\d{2}-\d{2})", fname)
            contest_date = date_match.group(1) if date_match else today.isoformat()

            if import_to_app(csv_path, contest_date, args.type):
                success += 1
                sys.stdout.write(f"\r    Imported {success}/{i} ({csv_path.name})    ")
            else:
                sys.stdout.write(f"\r    Failed  {csv_path.name}                    ")
            sys.stdout.flush()
            time.sleep(0.3)

        print(f"\n\n[*] Imported {success}/{len(csv_files)} CSVs")

        if not args.no_analysis and success > 0:
            print(f"\n[*] Running AI tournament analysis...")
            run_analysis()
        return

    # -- Validate contest discovery method ----------------------------------
    if not args.entry_history and not args.contest_ids:
        print("\n[!] No contest discovery method specified.")
        print("")
        print("You need to tell the script which contests to download. Options:")
        print("")
        print("  Option 1 - DK Entry History CSV (recommended):")
        print("    1. Go to draftkings.com -> My Contests -> History tab")
        print("    2. Click 'Download Entry History' to get a CSV")
        print("    3. Run: python scripts/dk_backfill_month.py --entry-history <file.csv> --cookie @dk_cookie.txt")
        print("")
        print("  Option 2 - Direct Contest IDs:")
        print("    Run: python scripts/dk_backfill_month.py --contest-ids 12345,67890 --cookie @dk_cookie.txt")
        print("")
        print("  Option 3 - Import local CSVs (already downloaded):")
        print("    Run: python scripts/dk_backfill_month.py --import-only")
        sys.exit(1)

    # -- Build contest list -------------------------------------------------
    contests = []

    if args.entry_history:
        # Parse entry history CSV
        contests = parse_entry_history(
            args.entry_history,
            start_date=start_date,
            end_date=end_date,
            sport_filter="NBA",
            contest_type_filter=args.type,
        )

        # Apply min fee filter
        if args.min_fee > 0:
            before = len(contests)
            contests = [c for c in contests if c.get("entry_fee", 0) >= args.min_fee]
            if len(contests) < before:
                print(f"    Filtered to {len(contests)} contests with fee >= ${args.min_fee:.0f}")

    elif args.contest_ids:
        # Parse comma-separated contest IDs
        ids_raw = args.contest_ids.split(",")
        for raw_id in ids_raw:
            raw_id = raw_id.strip()
            if raw_id:
                try:
                    cid = int(raw_id)
                    contests.append({
                        "id": cid,
                        "name": f"Contest_{cid}",
                        "date": today.isoformat(),
                        "sport": "NBA",
                    })
                except ValueError:
                    print(f"    [!] Skipping invalid contest ID: {raw_id}")
        print(f"[*] {len(contests)} contest IDs provided")

    if not contests:
        print("\n[!] No contests to process.")
        if args.entry_history:
            print("    Check that your entry history CSV contains NBA contests")
            print(f"    in the date range {start_date} to {end_date}")
        sys.exit(0)

    # Limit
    if len(contests) > args.max_contests:
        print(f"\n[*] Limiting to {args.max_contests} contests (use --max-contests to change)")
        contests = contests[:args.max_contests]

    print(f"\nRange: {start_date} to {end_date}")
    print(f"Type: {args.type} | Contests to process: {len(contests)}")
    print(f"Import: {not args.no_import} | Analysis: {not args.no_analysis}")
    print("-" * 60)

    # -- Authenticate -------------------------------------------------------
    session = get_dk_session(args.cookie)

    # Verify auth
    print(f"\n[*] Verifying DraftKings authentication...")
    try:
        test_resp = session.get(f"{DK_BASE}/lobby", timeout=10, allow_redirects=False)
        if test_resp.status_code in (301, 302):
            location = test_resp.headers.get("Location", "")
            if "login" in location.lower() or "sign-in" in location.lower():
                print("[!] Not authenticated -- redirected to login page.")
                print("    Log into draftkings.com in Chrome and retry.")
                sys.exit(1)
        print("    Authentication OK")
    except Exception as e:
        print(f"    [!] Could not verify auth: {e}")

    # -- Load state for resume mode -----------------------------------------
    state = load_state() if args.resume else {"completed_dates": [], "downloaded_contests": [], "failed_contests": []}

    # -- Download contests --------------------------------------------------
    total_downloaded = 0
    total_imported = 0
    total_skipped = 0
    total_failed = 0
    total_cached = 0

    print(f"\n[*] Processing {len(contests)} contests...\n")

    for ci, contest in enumerate(contests, 1):
        cid = contest["id"]
        cname = contest.get("name", f"Contest_{cid}")
        cdate = contest.get("date", today.isoformat())
        place = contest.get("place", "")
        points = contest.get("points", "")

        # Skip if already downloaded in previous run
        if args.resume and str(cid) in state.get("downloaded_contests", []):
            total_cached += 1
            continue

        # Display contest info
        info_parts = [f"ID:{cid}"]
        if cdate:
            info_parts.append(cdate)
        if place:
            info_parts.append(f"#{place}")
        if points:
            info_parts.append(f"{points}pts")
        info_str = " | ".join(info_parts)

        sys.stdout.write(f"  [{ci}/{len(contests)}] {cname[:45]:45s} ({info_str}) ... ")
        sys.stdout.flush()

        csv_path = download_standings_csv(session, cid, cname, cdate)

        if csv_path:
            was_cached = csv_path.exists() and str(cid) in state.get("downloaded_contests", [])
            if not was_cached:
                total_downloaded += 1
            else:
                total_cached += 1

            state.setdefault("downloaded_contests", []).append(str(cid))

            if not args.no_import:
                if import_to_app(csv_path, cdate, args.type):
                    total_imported += 1
                    size_kb = csv_path.stat().st_size / 1024
                    print(f"OK ({size_kb:.0f}KB, imported)")
                else:
                    print(f"downloaded (import failed)")
            else:
                size_kb = csv_path.stat().st_size / 1024
                print(f"saved ({size_kb:.0f}KB)")
        else:
            state.setdefault("failed_contests", []).append(str(cid))
            total_failed += 1
            print(f"not available")

        save_state(state)

        # Rate limit
        if ci < len(contests):
            time.sleep(2)

    # -- Summary ------------------------------------------------------------
    print(f"\n{'=' * 60}")
    print("BACKFILL COMPLETE")
    print(f"{'=' * 60}")
    print(f"Contests processed: {len(contests)}")
    print(f"CSVs downloaded:    {total_downloaded} new + {total_cached} cached")
    print(f"CSVs imported:      {total_imported}")
    print(f"Not available:      {total_failed}")
    print(f"Cache dir:          {DATA_DIR}")

    # Count total cached CSVs
    cached = list(DATA_DIR.glob("*.csv"))
    cached = [f for f in cached if not f.name.startswith("_")]
    print(f"Total cached:       {len(cached)} CSV files")

    # Run analysis
    if not args.no_analysis and total_imported > 0:
        print(f"\n[*] Running AI tournament pattern analysis...")
        run_analysis()
    elif not args.no_analysis and total_imported == 0 and not args.no_import:
        print(f"\n[*] No new imports -- skipping analysis")
        if total_failed > 0:
            print(f"    Note: {total_failed} contests were not available on DK")
            print(f"    (DK may not retain standings beyond ~7 days after completion)")
        print(f"    To import cached CSVs: python scripts/dk_backfill_month.py --import-only")
    else:
        print(f"\nNext steps:")
        print(f"  1. Import:  python scripts/dk_backfill_month.py --import-only")
        print(f"  2. Analyze: curl http://localhost:8000/api/tournament/analysis")


if __name__ == "__main__":
    main()
