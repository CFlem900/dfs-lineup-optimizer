"""Shared DraftKings cookie extraction for browser automation.

Extracts DK session cookies from Chrome and provides them in formats
suitable for both ``requests.Session`` and Playwright browser contexts.

Cookie extraction strategies (tried in order):
0. Cached cookies from a previous successful extraction (fast path)
1. ``browser_cookie3`` library (handles Shadow Copy + DPAPI for v10 cookies)
2. Direct Chrome cookie DB copy + DPAPI decryption for v10 cookies
   (works when Chrome is closed *or* when the Cookies DB can be copied)
3. Kill Chrome → copy cookie DB → decrypt → relaunch Chrome
   (briefly closes Chrome to release its exclusive file lock)
4. Playwright interactive login — opens a visible browser at DK login,
   user logs in manually, cookies are captured and cached for future use.

Note on Chrome v20 (App-Bound Encryption):
    Chrome 127+ encrypts cookies with App-Bound Encryption (v20 prefix).
    v20 keys are bound to the Chrome binary via a SYSTEM-level DPAPI key,
    making them impossible to decrypt from user-mode Python code.
    Methods 1-3 will only work for cookies still using v10 encryption.
    Method 4 (Playwright login) is the reliable fallback for v20 systems.
"""

import base64
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# DK auth cookie names we care about
_AUTH_COOKIE_NAMES = {"DSID", "sessionToken", "csid"}

# Path to cached cookies file
_CACHE_DIR = Path(__file__).resolve().parent.parent.parent / ".dk_cache"
_COOKIE_CACHE_FILE = _CACHE_DIR / "dk_cookies.json"

# Common headers to look like a real browser
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


# ── Cookie cache ─────────────────────────────────────────────────────

def _save_cookies_to_cache(cookies: List[Dict]) -> None:
    """Persist cookies to disk for fast reload on next extraction."""
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        data = {
            "cookies": cookies,
            "saved_at": time.time(),
        }
        _COOKIE_CACHE_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
        logger.info(f"Cached {len(cookies)} DK cookies to {_COOKIE_CACHE_FILE}")
    except Exception as e:
        logger.warning(f"Failed to cache cookies: {e}")


def _load_cookies_from_cache(max_age_hours: float = 12.0) -> Optional[List[Dict]]:
    """Load cached cookies if they exist and are fresh enough.

    Parameters
    ----------
    max_age_hours : float
        Maximum age of cached cookies before they're considered stale.
        DK sessions typically last several hours.

    Returns None if cache is missing, stale, or lacks auth cookies.
    """
    if not _COOKIE_CACHE_FILE.is_file():
        return None

    try:
        data = json.loads(_COOKIE_CACHE_FILE.read_text(encoding="utf-8"))
        cookies = data.get("cookies", [])
        saved_at = data.get("saved_at", 0)

        age_hours = (time.time() - saved_at) / 3600
        if age_hours > max_age_hours:
            logger.info(f"Cached cookies are {age_hours:.1f}h old (max {max_age_hours}h) — stale")
            return None

        auth_found = [c["name"] for c in cookies if c["name"] in _AUTH_COOKIE_NAMES]
        if not auth_found:
            logger.info("Cached cookies have no auth tokens — stale")
            return None

        logger.info(
            f"Loaded {len(cookies)} cached DK cookies "
            f"({age_hours:.1f}h old, auth: {', '.join(auth_found)})"
        )
        return cookies
    except Exception as e:
        logger.warning(f"Failed to load cached cookies: {e}")
        return None


def clear_cookie_cache() -> None:
    """Clear the cached cookies file."""
    try:
        if _COOKIE_CACHE_FILE.is_file():
            _COOKIE_CACHE_FILE.unlink()
            logger.info("Cleared DK cookie cache")
    except Exception as e:
        logger.warning(f"Failed to clear cookie cache: {e}")


# ── Chrome cookie decryption helpers (Windows DPAPI + AES-GCM) ──────

def _get_chrome_encryption_key() -> Optional[bytes]:
    """Read Chrome's AES-256-GCM key from Local State and decrypt it with DPAPI.

    Chrome 80+ encrypts cookies with AES-256-GCM using a key stored in
    ``Local State``, itself encrypted with Windows DPAPI.  This function
    decrypts that key so we can read ``encrypted_value`` from the Cookies DB.

    Returns None if decryption isn't possible (non-Windows, missing files, etc.)
    """
    try:
        import win32crypt  # pywin32
    except ImportError:
        logger.warning("win32crypt (pywin32) not installed — cannot decrypt Chrome cookies. "
                       "Install with: pip install pywin32")
        return None

    local_state_path = os.path.join(
        os.environ.get("LOCALAPPDATA", ""),
        "Google", "Chrome", "User Data", "Local State",
    )
    if not os.path.isfile(local_state_path):
        logger.info("Chrome Local State file not found")
        return None

    try:
        with open(local_state_path, "r", encoding="utf-8") as f:
            local_state = json.load(f)
        encrypted_key_b64 = local_state["os_crypt"]["encrypted_key"]
        encrypted_key = base64.b64decode(encrypted_key_b64)
        # Strip the 'DPAPI' prefix (first 5 bytes)
        encrypted_key = encrypted_key[5:]
        # Decrypt with DPAPI (only works under the same Windows user session)
        decrypted_key = win32crypt.CryptUnprotectData(encrypted_key, None, None, None, 0)[1]
        logger.info("Successfully decrypted Chrome encryption key via DPAPI")
        return decrypted_key
    except Exception as e:
        logger.warning(f"Failed to get Chrome encryption key: {type(e).__name__}: {e}")
        return None


def _decrypt_chrome_value(encrypted_value: bytes, key: bytes) -> Optional[str]:
    """Decrypt a Chrome cookie ``encrypted_value`` using AES-256-GCM.

    Chrome 80+ encrypted values start with ``v10`` (3-byte prefix), followed
    by a 12-byte nonce and then the AES-GCM ciphertext + 16-byte auth tag.

    Chrome 127+ uses ``v20`` (App-Bound Encryption) which requires SYSTEM-level
    access to decrypt — we return None for v20 cookies.
    """
    try:
        prefix = encrypted_value[:3]
        if prefix == b"v10":
            # AES-256-GCM: nonce (12 bytes) + ciphertext + tag (16 bytes)
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
            nonce = encrypted_value[3:15]
            ciphertext = encrypted_value[15:]
            aesgcm = AESGCM(key)
            return aesgcm.decrypt(nonce, ciphertext, None).decode("utf-8")
        elif prefix == b"v20":
            # App-Bound Encryption — cannot decrypt from user-mode Python
            return None
        else:
            # Older DPAPI-only encryption (pre-Chrome 80)
            try:
                import win32crypt
                return win32crypt.CryptUnprotectData(
                    encrypted_value, None, None, None, 0
                )[1].decode("utf-8")
            except Exception:
                return None
    except Exception:
        return None


def _find_chrome_cookie_db() -> Optional[str]:
    """Find the Chrome Cookies database file path."""
    local_app = os.environ.get("LOCALAPPDATA", "")
    cookie_paths = [
        os.path.join(local_app, "Google", "Chrome", "User Data", "Default", "Network", "Cookies"),
        os.path.join(local_app, "Google", "Chrome", "User Data", "Default", "Cookies"),
        os.path.join(local_app, "Google", "Chrome", "User Data", "Profile 1", "Network", "Cookies"),
        os.path.join(local_app, "Google", "Chrome", "User Data", "Profile 1", "Cookies"),
    ]
    for p in cookie_paths:
        if os.path.isfile(p):
            return p
    return None


def _read_dk_cookies_from_db(conn: sqlite3.Connection, key: bytes) -> List[Dict]:
    """Read and decrypt DraftKings cookies from an open SQLite connection."""
    cookies = []
    v20_count = 0
    try:
        cursor = conn.execute(
            "SELECT name, encrypted_value, value, host_key, path, expires_utc, is_secure "
            "FROM cookies WHERE host_key LIKE '%draftkings.com%'"
        )
        for name, encrypted_value, value, host, path, expires, secure in cursor.fetchall():
            # Try plain-text value first, then decrypt
            cookie_value = value
            if not cookie_value and encrypted_value:
                if encrypted_value[:3] == b"v20":
                    v20_count += 1
                cookie_value = _decrypt_chrome_value(encrypted_value, key)

            if not cookie_value:
                continue

            # Normalize domain for Playwright compatibility
            domain = host
            if domain and not domain.startswith(".") and "draftkings.com" in domain:
                domain = "." + domain

            cookies.append({
                "name": name,
                "value": cookie_value,
                "domain": domain,
                "path": path or "/",
                "secure": bool(secure),
                "expires": expires or -1,
            })
    except Exception as e:
        logger.warning(f"Failed to read/decrypt Chrome cookie DB: {e}")

    if v20_count > 0:
        logger.info(
            f"Skipped {v20_count} v20 (App-Bound) cookies — "
            f"these require Chrome's elevation service to decrypt"
        )

    return cookies


def _try_decrypt_chrome_cookies() -> Optional[List[Dict]]:
    """Read and decrypt Chrome cookies using DPAPI + AES-256-GCM.

    This handles Chrome 80-126 where cookie values are encrypted
    with ``v10`` prefix.  Copies the DB to avoid lock issues.
    Will not work for Chrome 127+ v20 (App-Bound) encrypted cookies.
    """
    key = _get_chrome_encryption_key()
    if not key:
        return None

    db_path = _find_chrome_cookie_db()
    if not db_path:
        logger.info("No Chrome cookie DB found")
        return None

    logger.info(f"Reading Chrome cookie DB: {db_path}")

    # Strategy 1: Copy DB to temp file (works when Chrome is closed)
    conn = None
    tmp_path = None
    try:
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        tmp_path = tmp.name
        shutil.copy2(db_path, tmp_path)
        conn = sqlite3.connect(tmp_path)
        logger.info("Chrome cookie DB copied successfully")
    except (PermissionError, OSError) as copy_err:
        logger.info(
            f"Cannot copy Chrome cookie DB ({type(copy_err).__name__}: "
            f"{copy_err}), trying immutable mode..."
        )
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            tmp_path = None

        # Strategy 2: Open the DB directly in immutable mode (read-only, no locks).
        try:
            uri = f"file:{db_path}?immutable=1"
            conn = sqlite3.connect(uri, uri=True)
            logger.info("Opened Chrome cookie DB directly (immutable mode)")
        except Exception as e2:
            logger.warning(f"Immutable DB open also failed: {e2}")
            return None

    if not conn:
        return None

    cookies = _read_dk_cookies_from_db(conn, key)
    conn.close()

    if tmp_path and os.path.exists(tmp_path):
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    if cookies:
        logger.info(f"Decrypted {len(cookies)} DK cookies from Chrome DB")
    return cookies if cookies else None


# ── Kill-Copy-Relaunch strategy ──────────────────────────────────────

def _get_chrome_exe_path() -> Optional[str]:
    """Find the Chrome executable path on Windows."""
    candidates = [
        os.path.join(os.environ.get("PROGRAMFILES", ""), "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(os.environ.get("PROGRAMFILES(X86)", ""), "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "Application", "chrome.exe"),
    ]
    for p in candidates:
        if os.path.isfile(p):
            return p
    return None


def _kill_copy_relaunch_cookies() -> Optional[List[Dict]]:
    """Kill Chrome, copy the cookie DB while unlocked, then relaunch Chrome.

    Chrome holds an exclusive Windows file lock on the Cookies DB that cannot
    be bypassed by any read-sharing mode.  This function briefly kills all
    Chrome processes to release the lock, copies the DB to a temp file,
    relaunches Chrome with ``--restore-last-session`` so the user's tabs are
    preserved, and then decrypts the copied cookies.

    Note: This will only decrypt v10 cookies.  v20 (App-Bound) cookies
    require Chrome's SYSTEM-level elevation service and cannot be read here.

    Returns a list of cookie dicts in Playwright format, or None on failure.
    """
    key = _get_chrome_encryption_key()
    if not key:
        logger.info("Cannot get Chrome encryption key — kill-copy-relaunch skipped")
        return None

    db_path = _find_chrome_cookie_db()
    if not db_path:
        return None

    chrome_exe = _get_chrome_exe_path()

    # Step 1: Kill Chrome to release the file lock
    logger.info("Killing Chrome processes to release cookie DB lock...")
    try:
        subprocess.run(
            ["taskkill", "/IM", "chrome.exe", "/F"],
            capture_output=True, timeout=10,
        )
    except Exception as e:
        logger.warning(f"taskkill failed: {e}")
        return None

    # Wait for Chrome to fully exit and release file handles
    time.sleep(2)

    # Step 2: Copy the cookie DB while Chrome is dead
    tmp_path = None
    conn = None
    try:
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        tmp_path = tmp.name
        shutil.copy2(db_path, tmp_path)
        logger.info(f"Copied Chrome cookie DB ({os.path.getsize(tmp_path)} bytes)")
        conn = sqlite3.connect(tmp_path)
    except (PermissionError, OSError) as e:
        logger.warning(f"Copy failed even after killing Chrome: {e}")
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        # Still try to relaunch Chrome
        _relaunch_chrome(chrome_exe)
        return None

    # Step 3: Relaunch Chrome immediately (don't wait for cookie extraction)
    _relaunch_chrome(chrome_exe)

    # Step 4: Read and decrypt cookies from the copied DB
    cookies = _read_dk_cookies_from_db(conn, key)
    conn.close()

    if tmp_path and os.path.exists(tmp_path):
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    if cookies:
        logger.info(f"Extracted {len(cookies)} DK cookies via kill-copy-relaunch")
    return cookies if cookies else None


def _relaunch_chrome(chrome_exe: Optional[str] = None) -> None:
    """Relaunch Chrome with --restore-last-session."""
    if not chrome_exe:
        chrome_exe = _get_chrome_exe_path()
    if chrome_exe:
        try:
            logger.info("Relaunching Chrome with --restore-last-session...")
            subprocess.Popen(
                [chrome_exe, "--restore-last-session"],
                creationflags=subprocess.DETACHED_PROCESS
                | subprocess.CREATE_NEW_PROCESS_GROUP,
            )
        except Exception as e:
            logger.warning(f"Failed to relaunch Chrome: {e}")
    else:
        logger.warning("Could not find Chrome exe — please reopen Chrome manually")


# ── Playwright interactive login ─────────────────────────────────────

def _try_playwright_login() -> Optional[List[Dict]]:
    """Open a visible Playwright browser at DK login for manual authentication.

    This is the fallback for Chrome 127+ where cookies use App-Bound
    Encryption (v20) and cannot be decrypted from user-mode Python.

    Opens a visible Chromium window at draftkings.com/lobby.  If the user
    is already logged in (session cookies from a previous Playwright run),
    cookies are captured immediately.  Otherwise the user must log in
    manually.  Once authenticated, cookies are extracted and cached.

    Returns a list of cookie dicts, or None on failure.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.warning("Playwright not installed — cannot do interactive login")
        return None

    # Use a persistent Playwright profile so DK's "remember me" works
    pw_profile = _CACHE_DIR / "playwright_profile"
    pw_profile.mkdir(parents=True, exist_ok=True)

    logger.info("Opening Playwright browser for DK login...")

    try:
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(
                user_data_dir=str(pw_profile),
                headless=False,
                args=["--disable-gpu"],
                timeout=30000,
            )

            page = ctx.new_page()
            page.goto(
                "https://www.draftkings.com/lobby",
                wait_until="domcontentloaded",
                timeout=20000,
            )

            current_url = page.url
            logger.info(f"Navigated to: {current_url}")

            # Check if already authenticated (not redirected to login/signup)
            if "login" in current_url.lower() or "sign-in" in current_url.lower() or "signup" in current_url.lower():
                logger.info(
                    "DK requires login — waiting for user to authenticate in the browser window. "
                    "Please log in to DraftKings in the popup window."
                )
                # Wait for navigation away from login page (user logs in)
                try:
                    page.wait_for_url(
                        lambda url: (
                            "lobby" in url.lower()
                            and "login" not in url.lower()
                            and "sign-in" not in url.lower()
                            and "signup" not in url.lower()
                        ),
                        timeout=120000,  # 2 minutes for user to log in
                    )
                    logger.info(f"Login detected — now at: {page.url}")
                    # Give DK a moment to set all cookies
                    time.sleep(2)
                except Exception:
                    logger.warning("Timed out waiting for DK login (2 minutes)")
                    ctx.close()
                    return None
            else:
                logger.info("Already authenticated in Playwright profile!")

            # Extract cookies
            raw_cookies = ctx.cookies([
                "https://www.draftkings.com",
                "https://sportsbook.draftkings.com",
            ])
            dk_cookies = [
                c for c in raw_cookies
                if "draftkings" in c.get("domain", "").lower()
            ]

            cookies = []
            for c in dk_cookies:
                domain = c.get("domain", "")
                if domain and not domain.startswith(".") and "draftkings.com" in domain:
                    domain = "." + domain
                cookies.append({
                    "name": c["name"],
                    "value": c["value"],
                    "domain": domain,
                    "path": c.get("path", "/"),
                    "secure": c.get("secure", False),
                    "expires": c.get("expires", -1),
                })

            ctx.close()

            if cookies:
                logger.info(f"Captured {len(cookies)} DK cookies via Playwright login")
            return cookies if cookies else None

    except Exception as e:
        logger.warning(f"Playwright login failed: {type(e).__name__}: {e}")
        return None


# ── Public API ───────────────────────────────────────────────────────

def get_dk_cookies(skip_cache: bool = False) -> List[Dict]:
    """Get DraftKings cookies in Playwright-compatible format.

    Returns list of dicts: ``[{name, value, domain, path, secure, expires}]``

    Tries (in order):
    0. Cached cookies from a previous extraction
    1. ``browser_cookie3`` (reads Chrome cookies, uses Shadow Copy on Windows)
    2. Direct Chrome DB copy + DPAPI/AES-GCM decryption (v10 only)
    3. Kill Chrome → copy DB → decrypt → relaunch (v10 only, brief Chrome restart)
    4. Playwright interactive login (opens browser, user logs in, cookies cached)

    Raises
    ------
    RuntimeError
        If no DK cookies could be extracted.
    """
    errors = []

    # ── Method 0: Cached cookies (fast path) ──
    if not skip_cache:
        cached = _load_cookies_from_cache()
        if cached:
            return cached

    # ── Method 1: browser_cookie3 ──
    try:
        import browser_cookie3
        logger.info("Attempting browser_cookie3 cookie extraction...")

        _com_initialised = False
        try:
            import pythoncom
            pythoncom.CoInitialize()
            _com_initialised = True
        except ImportError:
            pass

        try:
            cj = browser_cookie3.chrome(domain_name=".draftkings.com")
        finally:
            if _com_initialised:
                try:
                    pythoncom.CoUninitialize()
                except Exception:
                    pass

        cookies = []
        for c in cj:
            domain = c.domain
            if domain and not domain.startswith(".") and "draftkings.com" in domain:
                domain = "." + domain
            cookies.append({
                "name": c.name,
                "value": c.value,
                "domain": domain,
                "path": c.path or "/",
                "secure": c.secure,
                "expires": c.expires or -1,
            })
        auth_found = [c["name"] for c in cookies if c["name"] in _AUTH_COOKIE_NAMES]
        if auth_found:
            logger.info(f"Loaded DK cookies via browser_cookie3: {', '.join(auth_found)}")
            _save_cookies_to_cache(cookies)
            return cookies
        else:
            all_names = [c["name"] for c in cookies]
            msg = (
                f"browser_cookie3 found {len(cookies)} DK cookies but no auth tokens "
                f"(need DSID/sessionToken/csid). Names: {all_names[:10]}"
            )
            logger.warning(msg)
            errors.append(msg)
    except ImportError:
        logger.info("browser_cookie3 not installed — skipping")
        errors.append("browser_cookie3 not installed")
    except Exception as e:
        msg = f"browser_cookie3 failed: {type(e).__name__}: {e}"
        logger.warning(msg)
        errors.append(msg)

    # ── Method 2: Direct DB copy with DPAPI decryption ──
    logger.info("Trying direct Chrome cookie DB with DPAPI decryption...")
    try:
        cookies = _try_decrypt_chrome_cookies()
    except Exception as e:
        cookies = None
        msg = f"DPAPI decrypt failed: {type(e).__name__}: {e}"
        logger.warning(msg)
        errors.append(msg)

    if cookies:
        auth_found = [c["name"] for c in cookies if c["name"] in _AUTH_COOKIE_NAMES]
        if auth_found:
            logger.info(f"Loaded DK cookies via DPAPI decrypt: {', '.join(auth_found)}")
            _save_cookies_to_cache(cookies)
            return cookies
        else:
            all_names = [c["name"] for c in cookies]
            errors.append(
                f"DPAPI decrypted {len(cookies)} cookies but no auth tokens "
                f"(v20 App-Bound?): {all_names[:10]}"
            )
    elif not any("DPAPI" in e for e in errors):
        errors.append(
            "DPAPI fallback returned no cookies (Chrome encryption key "
            "unavailable or cookie DB not found)"
        )

    # ── Method 3: Kill Chrome → copy → decrypt → relaunch ──
    logger.info("Trying kill-Chrome-copy-relaunch approach...")
    try:
        cookies = _kill_copy_relaunch_cookies()
    except Exception as e:
        cookies = None
        msg = f"Kill-copy-relaunch failed: {type(e).__name__}: {e}"
        logger.warning(msg)
        errors.append(msg)

    if cookies:
        auth_found = [c["name"] for c in cookies if c["name"] in _AUTH_COOKIE_NAMES]
        if auth_found:
            logger.info(f"Loaded DK cookies via kill-copy-relaunch: {', '.join(auth_found)}")
            _save_cookies_to_cache(cookies)
            return cookies
        else:
            all_names = [c["name"] for c in cookies]
            errors.append(
                f"Kill-copy-relaunch found {len(cookies)} cookies "
                f"but no auth tokens (all v20?): {all_names[:10]}"
            )

    # ── Method 4: Playwright interactive login ──
    logger.info(
        "All automated methods failed (Chrome likely uses v20 App-Bound Encryption). "
        "Falling back to Playwright interactive login..."
    )
    try:
        cookies = _try_playwright_login()
    except Exception as e:
        cookies = None
        msg = f"Playwright login failed: {type(e).__name__}: {e}"
        logger.warning(msg)
        errors.append(msg)

    if cookies:
        auth_found = [c["name"] for c in cookies if c["name"] in _AUTH_COOKIE_NAMES]
        if auth_found:
            logger.info(f"Loaded DK cookies via Playwright login: {', '.join(auth_found)}")
            _save_cookies_to_cache(cookies)
            return cookies
        else:
            all_names = [c["name"] for c in cookies]
            errors.append(
                f"Playwright login found {len(cookies)} cookies "
                f"but no auth tokens: {all_names[:10]}"
            )

    # All methods failed
    error_detail = "; ".join(errors) if errors else "No cookies found"

    raise RuntimeError(
        f"Could not extract DraftKings cookies. "
        f"Chrome 127+ uses App-Bound Encryption (v20) which prevents "
        f"automated cookie reading. Please use the DK Auth button in the "
        f"app to log in via the popup browser window. "
        f"[{error_detail}]"
    )


def get_dk_cookies_for_requests() -> Dict[str, str]:
    """Get DraftKings cookies as a simple ``{name: value}`` dict for ``requests``."""
    cookies = get_dk_cookies()
    return {c["name"]: c["value"] for c in cookies}


def has_dk_auth() -> bool:
    """Check if DK authentication cookies are available (no network call)."""
    try:
        cookies = get_dk_cookies()
        auth_names = {c["name"] for c in cookies} & _AUTH_COOKIE_NAMES
        return len(auth_names) > 0
    except Exception:
        return False


async def verify_dk_auth() -> Dict:
    """Verify DK authentication by making a quick request to the lobby.

    Returns dict with ``{authenticated: bool, cookies_found: list, error: str|None}``
    """
    import asyncio
    import httpx

    # get_dk_cookies() is synchronous (reads Chrome DB) — run off the event loop
    try:
        cookies = await asyncio.to_thread(get_dk_cookies)
    except Exception as e:
        logger.warning(f"DK cookie extraction failed: {e}")
        return {
            "authenticated": False,
            "cookies_found": [],
            "error": str(e),
        }

    auth_names = [c["name"] for c in cookies if c["name"] in _AUTH_COOKIE_NAMES]

    # Quick check: hit the lobby and see if we get redirected to login
    try:
        cookie_dict = {c["name"]: c["value"] for c in cookies}
        async with httpx.AsyncClient(
            headers=HEADERS,
            cookies=cookie_dict,
            follow_redirects=False,
            timeout=10.0,
        ) as client:
            resp = await client.get("https://www.draftkings.com/lobby")
            if resp.status_code in (301, 302):
                location = resp.headers.get("location", "")
                if "login" in location.lower() or "sign-in" in location.lower():
                    return {
                        "authenticated": False,
                        "cookies_found": auth_names,
                        "error": "Session expired — redirected to login page",
                    }
            return {
                "authenticated": True,
                "cookies_found": auth_names,
                "error": None,
            }
    except Exception as e:
        logger.warning(f"DK lobby check failed: {type(e).__name__}: {e}")
        return {
            "authenticated": False,
            "cookies_found": auth_names,
            "error": f"Network error checking DK session: {e}",
        }


async def login_dk_interactive() -> Dict:
    """Trigger Playwright interactive login for DraftKings.

    This is called when the user clicks "Login to DK" in the frontend.
    Opens a visible browser window, waits for the user to log in,
    captures cookies, and caches them.

    Returns dict with ``{authenticated: bool, cookies_found: list, error: str|None}``
    """
    import asyncio

    try:
        cookies = await asyncio.to_thread(_try_playwright_login)
    except Exception as e:
        return {
            "authenticated": False,
            "cookies_found": [],
            "error": f"Playwright login error: {e}",
        }

    if not cookies:
        return {
            "authenticated": False,
            "cookies_found": [],
            "error": "No cookies captured — login may have timed out",
        }

    auth_found = [c["name"] for c in cookies if c["name"] in _AUTH_COOKIE_NAMES]
    if auth_found:
        _save_cookies_to_cache(cookies)
        return {
            "authenticated": True,
            "cookies_found": auth_found,
            "error": None,
        }
    else:
        return {
            "authenticated": False,
            "cookies_found": [c["name"] for c in cookies[:10]],
            "error": "Login completed but no auth tokens found (DSID/sessionToken/csid)",
        }
