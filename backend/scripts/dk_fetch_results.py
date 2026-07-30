#!/usr/bin/env python3
"""Download DraftKings NBA contest standings and import into RotationEngine.

Usage
-----
  # Fetch today's completed NBA GPP contests and import them:
  python scripts/dk_fetch_results.py

  # Fetch a specific date:
  python scripts/dk_fetch_results.py --date 2026-02-10

  # Download only (skip auto-import):
  python scripts/dk_fetch_results.py --no-import

  # Specify contest type filter:
  python scripts/dk_fetch_results.py --type gpp

  # Use a specific contest ID:
  python scripts/dk_fetch_results.py --contest-id 12345678

  # Multiple contest IDs:
  python scripts/dk_fetch_results.py --contest-ids 12345,67890,11111

  # Manual cookie input (if browser_cookie3 not available):
  python scripts/dk_fetch_results.py --cookie "DSID=abc123..."

How It Works
------------
1. Reads your DraftKings session cookies from Chrome (you must be logged in)
2. Fetches the NBA contest lobby to find completed contests
3. Downloads the full standings CSV for each contest
4. Auto-imports each CSV into the RotationEngine tournament DB via the local API

Requirements
------------
  pip install requests browser_cookie3

If browser_cookie3 doesn't work (OS/browser issues), you can manually supply
your DK session cookie via --cookie flag. To get it:
  1. Open Chrome DevTools (F12) → Application tab → Cookies → draftkings.com
  2. Copy the full cookie string, or just the value of the "DSID" cookie

Notes
-----
- DraftKings requires authentication to download contest standings
- Only completed contests have downloadable results
- Large contests (10K+ entries) may be returned as ZIP files (handled automatically)
- CSVs are saved to backend/data/tournament_csvs/ for reuse
"""

import argparse
import io
import json
import os
import re
import sys
import time
import zipfile
from datetime import datetime, date
from pathlib import Path
from typing import Optional

try:
    import requests
except ImportError:
    print("ERROR: requests is required. Run: pip install requests")
    sys.exit(1)

# ── Paths ────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).resolve().parent
BACKEND_DIR = SCRIPT_DIR.parent
DATA_DIR = BACKEND_DIR / "data" / "tournament_csvs"
DATA_DIR.mkdir(parents=True, exist_ok=True)

# ── DraftKings URLs ──────────────────────────────────────────────────────
DK_BASE = "https://www.draftkings.com"
DK_LOBBY_URL = f"{DK_BASE}/lobby/getcontests?sport=NBA"
DK_CONTEST_URL = f"{DK_BASE}/contest/gamecenter/{{contest_id}}"
DK_EXPORT_URL = f"{DK_BASE}/contest/exportfullstandingscsv/{{contest_id}}"
DK_DETAILS_URL = f"{DK_BASE}/contest/detailspop?contestId={{contest_id}}"

# ── Local API ────────────────────────────────────────────────────────────
LOCAL_API = "http://localhost:8000/api"

# ── Common headers to look like a real browser ───────────────────────────
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
    """Create an authenticated DraftKings session.

    Tries browser_cookie3 to pull Chrome cookies first, then falls back
    to direct cookie DB copy (works while Chrome is open on Windows).
    """
    session = requests.Session()
    session.headers.update(HEADERS)

    cookie_str = _load_cookie_str(cookie_str)
    if cookie_str:
        print("[*] Using manually provided cookie")
        session.headers["Cookie"] = cookie_str
        return session

    # Try browser_cookie3
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

    print("[!] Could not load DraftKings cookies automatically.")
    print("")
    print("    Option 1: Close Chrome completely, then re-run this script")
    print("    Option 2: Provide cookies manually:")
    print("      1. Open Chrome -> draftkings.com (logged in)")
    print("      2. Press F12 -> Application tab -> Cookies -> draftkings.com")
    print("      3. Copy the cookie header string into a file, e.g. dk_cookie.txt")
    print("      4. Run with: --cookie @dk_cookie.txt")
    sys.exit(1)


def fetch_nba_contests(session: requests.Session) -> list:
    """Fetch the list of NBA contests from the DK lobby."""
    print(f"\n[*] Fetching NBA contest lobby...")
    resp = session.get(DK_LOBBY_URL)
    resp.raise_for_status()

    data = resp.json()
    contests = data.get("Contests", [])
    print(f"    Found {len(contests)} NBA contests in lobby")
    return contests


def filter_contests(
    contests: list,
    contest_type: Optional[str] = None,
    target_date: Optional[str] = None,
    contest_id: Optional[int] = None,
) -> list:
    """Filter contests to completed NBA GPP tournaments."""
    filtered = []

    for c in contests:
        cid = c.get("id") or c.get("ContestId") or c.get("contestId")
        name = c.get("n") or c.get("ContestName") or c.get("name", "")
        entries = c.get("m") or c.get("MaxEntries") or c.get("maximumEntries", 0)
        game_type = c.get("gameType", "").lower()
        status = c.get("cs", "").lower() or c.get("contestStatus", "").lower()

        # If looking for a specific contest
        if contest_id and str(cid) == str(contest_id):
            filtered.append({
                "id": cid,
                "name": name,
                "entries": entries,
                "game_type": game_type,
            })
            continue

        # Filter by completion status
        if status not in ("completed", "closed", "historical"):
            # DK lobby may use numeric or abbreviated status
            if c.get("cs") not in (4, 5):
                continue

        # Filter by type
        if contest_type:
            type_lower = contest_type.lower()
            if type_lower == "gpp" and "tournament" not in game_type and "gpp" not in name.lower():
                # Accept large-field contests as GPP
                if entries and entries < 100:
                    continue
            elif type_lower == "cash" and "head" not in game_type and "50/50" not in name and "double" not in name.lower():
                continue

        # Filter by date if specified
        if target_date:
            contest_start = c.get("sd", "") or c.get("startDate", "")
            if target_date not in str(contest_start):
                continue

        # Skip tiny contests (< 20 entries not useful for ML)
        if entries and entries < 20:
            continue

        filtered.append({
            "id": cid,
            "name": name,
            "entries": entries,
            "game_type": game_type,
        })

    print(f"    {len(filtered)} contests match filters")
    return filtered


def download_standings_csv(
    session: requests.Session,
    contest_id: int,
    contest_name: str,
) -> Optional[Path]:
    """Download the full standings CSV for a contest.

    DraftKings returns either a CSV directly or a ZIP for large contests.
    """
    safe_name = re.sub(r'[^\w\-]', '_', contest_name)[:60]
    csv_path = DATA_DIR / f"{contest_id}_{safe_name}.csv"

    # Skip if already downloaded
    if csv_path.exists() and csv_path.stat().st_size > 100:
        print(f"    [cached] {csv_path.name}")
        return csv_path

    url = DK_EXPORT_URL.format(contest_id=contest_id)
    print(f"    Downloading contest {contest_id}...")

    try:
        resp = session.get(url, timeout=60)

        if resp.status_code == 401 or resp.status_code == 403:
            print(f"    [!] Auth failed (HTTP {resp.status_code}). Cookie may be expired.")
            return None
        if resp.status_code == 404:
            print(f"    [!] Contest {contest_id} not found or not yet completed.")
            return None

        resp.raise_for_status()

        content_type = resp.headers.get("Content-Type", "")
        content = resp.content

        # Handle ZIP response (large contests)
        if "zip" in content_type or content[:4] == b"PK\x03\x04":
            print(f"    Extracting from ZIP archive...")
            with zipfile.ZipFile(io.BytesIO(content)) as zf:
                csv_files = [n for n in zf.namelist() if n.endswith(".csv")]
                if not csv_files:
                    print(f"    [!] ZIP contains no CSV files")
                    return None
                csv_data = zf.read(csv_files[0])
                csv_path.write_bytes(csv_data)
        else:
            csv_path.write_bytes(content)

        size_kb = csv_path.stat().st_size / 1024
        print(f"    Saved: {csv_path.name} ({size_kb:.1f} KB)")
        return csv_path

    except requests.RequestException as e:
        print(f"    [!] Download failed: {e}")
        return None


def import_to_app(csv_path: Path, contest_date: str, contest_type: str) -> bool:
    """Import a downloaded CSV into the RotationEngine via the local API."""
    contest_name = csv_path.stem  # filename without extension
    url = f"{LOCAL_API}/tournament/import"
    params = {
        "contest_date": contest_date,
        "contest_type": contest_type,
        "contest_name": contest_name,
    }

    try:
        with open(csv_path, "rb") as f:
            files = {"file": (csv_path.name, f, "text/csv")}
            resp = requests.post(url, params=params, files=files, timeout=120)

        if resp.ok:
            data = resp.json()
            print(f"    Imported: {data.get('message', 'OK')}")
            return True
        else:
            detail = ""
            try:
                detail = resp.json().get("detail", "")
            except Exception:
                detail = resp.text[:200]
            print(f"    [!] Import failed (HTTP {resp.status_code}): {detail}")
            return False

    except requests.ConnectionError:
        print(f"    [!] Cannot connect to {LOCAL_API}. Is the backend running?")
        return False
    except Exception as e:
        print(f"    [!] Import error: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(
        description="Download DraftKings NBA contest results and import into RotationEngine"
    )
    parser.add_argument(
        "--date", "-d",
        default=date.today().isoformat(),
        help="Contest date YYYY-MM-DD (default: today)",
    )
    parser.add_argument(
        "--type", "-t",
        default="gpp",
        choices=["gpp", "cash", "single_entry", "all"],
        help="Contest type filter (default: gpp)",
    )
    parser.add_argument(
        "--contest-id", "-c",
        type=int,
        default=None,
        help="Download a specific contest by ID",
    )
    parser.add_argument(
        "--contest-ids",
        default=None,
        help="Comma-separated list of contest IDs to download (e.g. 12345,67890)",
    )
    parser.add_argument(
        "--no-import",
        action="store_true",
        help="Download CSVs only, don't import into the app",
    )
    parser.add_argument(
        "--cookie",
        default=None,
        help="Manual DK cookie string (if browser_cookie3 unavailable)",
    )
    parser.add_argument(
        "--max-contests", "-n",
        type=int,
        default=10,
        help="Max number of contests to download (default: 10)",
    )
    parser.add_argument(
        "--import-dir",
        default=None,
        help="Import all CSVs from a local directory instead of downloading",
    )
    args = parser.parse_args()

    print("=" * 60)
    print("DraftKings Contest Results Downloader")
    print(f"Date: {args.date}  |  Type: {args.type}  |  Import: {not args.no_import}")
    print("=" * 60)

    # ── Mode 1: Import from local directory ──────────────────────────
    if args.import_dir:
        import_dir = Path(args.import_dir)
        if not import_dir.is_dir():
            print(f"[!] Directory not found: {import_dir}")
            sys.exit(1)

        csv_files = sorted(import_dir.glob("*.csv"))
        if not csv_files:
            print(f"[!] No CSV files found in {import_dir}")
            sys.exit(1)

        print(f"\n[*] Importing {len(csv_files)} CSV files from {import_dir}")
        success = 0
        for csv_path in csv_files:
            print(f"\n  >> {csv_path.name}")
            if import_to_app(csv_path, args.date, args.type):
                success += 1
            time.sleep(0.5)

        print(f"\n{'=' * 60}")
        print(f"Imported {success}/{len(csv_files)} files successfully")
        return

    # ── Mode 2: Download from DraftKings ─────────────────────────────
    session = get_dk_session(args.cookie)

    if args.contest_ids:
        # Download multiple specific contests
        contests = []
        for raw_id in args.contest_ids.split(","):
            raw_id = raw_id.strip()
            if raw_id:
                try:
                    cid = int(raw_id)
                    contests.append({"id": cid, "name": f"Contest_{cid}", "entries": 0})
                except ValueError:
                    print(f"    [!] Skipping invalid ID: {raw_id}")
        print(f"\n[*] {len(contests)} contest IDs provided")
    elif args.contest_id:
        # Download a specific contest
        contests = [{"id": args.contest_id, "name": f"Contest_{args.contest_id}", "entries": 0}]
    else:
        # Fetch and filter contests
        all_contests = fetch_nba_contests(session)
        type_filter = None if args.type == "all" else args.type
        contests = filter_contests(
            all_contests,
            contest_type=type_filter,
            target_date=args.date,
            contest_id=args.contest_id,
        )

    if not contests:
        print("\n[!] No matching contests found.")
        print("    - Make sure contests for this date are completed")
        print("    - Try: python scripts/dk_fetch_results.py --date YYYY-MM-DD")
        print("    - Or download CSVs manually from DK and use --import-dir")
        sys.exit(0)

    # Limit
    if len(contests) > args.max_contests:
        print(f"\n[*] Limiting to {args.max_contests} contests (use --max-contests to change)")
        contests = contests[:args.max_contests]

    # Download each contest
    print(f"\n[*] Downloading {len(contests)} contest standings...")
    downloaded = []
    for i, contest in enumerate(contests, 1):
        cid = contest["id"]
        cname = contest["name"]
        entries = contest.get("entries", "?")
        print(f"\n  [{i}/{len(contests)}] {cname} (ID: {cid}, ~{entries} entries)")

        csv_path = download_standings_csv(session, cid, cname)
        if csv_path:
            downloaded.append(csv_path)

        # Rate limit: be polite to DK servers
        if i < len(contests):
            time.sleep(2)

    if not downloaded:
        print("\n[!] No CSVs downloaded. Check authentication and contest availability.")
        sys.exit(1)

    print(f"\n[*] Downloaded {len(downloaded)} CSV files to {DATA_DIR}")

    # Auto-import
    if not args.no_import:
        print(f"\n[*] Importing into RotationEngine...")
        success = 0
        for csv_path in downloaded:
            print(f"\n  >> {csv_path.name}")
            if import_to_app(csv_path, args.date, args.type):
                success += 1
            time.sleep(0.5)

        print(f"\n{'=' * 60}")
        print(f"Downloaded: {len(downloaded)} | Imported: {success}/{len(downloaded)}")

        if success > 0:
            print(f"\nNext step: Run pattern analysis")
            print(f"  curl http://localhost:8000/api/tournament/analysis")
            print(f"  -- or click 'Run Pattern Analysis' in the UI")
    else:
        print(f"\n{'=' * 60}")
        print(f"Downloaded {len(downloaded)} CSV files (import skipped)")
        print(f"Files saved to: {DATA_DIR}")
        print(f"\nTo import later:")
        print(f"  python scripts/dk_fetch_results.py --import-dir {DATA_DIR} --date {args.date}")


if __name__ == "__main__":
    main()
