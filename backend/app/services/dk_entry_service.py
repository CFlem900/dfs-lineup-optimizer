"""DraftKings entry automation service using Playwright browser automation.

Provides end-to-end automation for:
1. Downloading the DK entries CSV template for a given draft group
2. Filling the template with optimised lineup player IDs
3. Uploading the filled CSV back to DraftKings

Uses Playwright (headless Chromium) with Chrome session cookies injected
so the user only needs to be logged into DK in their regular Chrome browser.
"""

import csv
import io
import logging
import math
import os
import re
import tempfile
import time
from collections import defaultdict
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from app.models.dk_entry import (
    DKEntriesTemplate,
    DKEntryRow,
    DKFillResult,
    DKFullResult,
    DKUploadResult,
    FillSelectionOptions,
    LineupMeta,
)


# Contest names that map to "cash" play (low variance, high floor wins).
# Anything else is treated as GPP (high ceiling wins).
_CASH_PATTERN = re.compile(
    r"\b(double[\s-]?up|50/50|h2h|head[\s-]?to[\s-]?head|triple[\s-]?up)\b",
    re.IGNORECASE,
)


def _is_cash_contest(contest_name: str) -> bool:
    return bool(_CASH_PATTERN.search(contest_name or ""))


def _smart_assignment(
    entries: List[DKEntryRow],
    lineup_meta: Optional[List[LineupMeta]],
    num_lineups: int,
    options: FillSelectionOptions,
) -> Tuple[List[int], List[str], Dict[str, object]]:
    """Decide which lineup index fills each entry.

    Returns
    -------
    assignment : list[int]
        Same length as ``entries``; ``assignment[i]`` is the lineup index for
        ``entries[i]``.
    warnings : list[str]
        Selector warnings (e.g. exposure cap violated, dedup couldn't hold).
    summary : dict
        Per-mode diagnostics.
    """
    num_entries = len(entries)
    warnings: List[str] = []

    # ── Per-lineup priority lists ────────────────────────────────────
    # When meta is missing, both rankings collapse to original index order
    # (preserves the lineup order the user generated).
    if lineup_meta and len(lineup_meta) == num_lineups:
        cash_order = sorted(
            range(num_lineups),
            key=lambda i: (
                lineup_meta[i].floor or lineup_meta[i].projection,
                lineup_meta[i].projection,
            ),
            reverse=True,
        )
        gpp_order = sorted(
            range(num_lineups),
            key=lambda i: (
                lineup_meta[i].ceiling or lineup_meta[i].projection,
                lineup_meta[i].projection,
            ),
            reverse=True,
        )
    else:
        if lineup_meta:
            warnings.append(
                f"lineup_meta length {len(lineup_meta)} != "
                f"{num_lineups} lineups — falling back to index order"
            )
        cash_order = list(range(num_lineups))
        gpp_order = list(range(num_lineups))

    # ── Exposure cap ────────────────────────────────────────────────
    if options.max_exposure_pct is not None and options.max_exposure_pct > 0:
        exposure_cap = max(1, math.ceil(num_entries * options.max_exposure_pct))
    else:
        exposure_cap = None

    # If the cap is so tight we can't possibly fill every entry, warn.
    if exposure_cap is not None and exposure_cap * num_lineups < num_entries:
        warnings.append(
            f"Exposure cap of {options.max_exposure_pct:.0%} × "
            f"{num_lineups} lineups = {exposure_cap * num_lineups} slots, "
            f"but {num_entries} entries need filling — cap will be relaxed "
            "for late entries."
        )

    # ── Process entries: cash first so safest lineups land in cash spots,
    # then GPP. Within each group we keep the original CSV order so the
    # output stays predictable.
    is_cash = [_is_cash_contest(e.contest_name) for e in entries]
    cash_idx = [i for i in range(num_entries) if is_cash[i]]
    gpp_idx = [i for i in range(num_entries) if not is_cash[i]]
    process_order = cash_idx + gpp_idx

    used_count: List[int] = [0] * num_lineups
    used_in_contest: Dict[int, set] = defaultdict(set)
    assignment: List[int] = [-1] * num_entries
    dedup_violations = 0

    for entry_idx in process_order:
        entry = entries[entry_idx]
        ranking = cash_order if is_cash[entry_idx] else gpp_order

        chosen: Optional[int] = None

        # Tier 1: full constraints — exposure cap AND per-contest dedup
        for li in ranking:
            if exposure_cap is not None and used_count[li] >= exposure_cap:
                continue
            if (
                options.dedupe_per_contest
                and li in used_in_contest[entry.contest_id]
            ):
                continue
            chosen = li
            break

        # Tier 2: relax exposure cap, keep dedup
        if chosen is None and exposure_cap is not None:
            for li in ranking:
                if (
                    options.dedupe_per_contest
                    and li in used_in_contest[entry.contest_id]
                ):
                    continue
                chosen = li
                break

        # Tier 3: dedup is impossible (more entries in this contest than
        # lineups available). DK will reject — record a violation. We still
        # need to write *some* lineup to keep the CSV well-formed; pick the
        # least-used lineup (breaking ties by priority) so the failure spreads
        # across lineups instead of all collapsing onto lineup 0.
        if chosen is None:
            chosen = min(ranking, key=lambda li: (used_count[li], ranking.index(li)))
            if options.dedupe_per_contest:
                dedup_violations += 1

        assignment[entry_idx] = chosen
        used_count[chosen] += 1
        used_in_contest[entry.contest_id].add(chosen)

    if dedup_violations:
        warnings.append(
            f"{dedup_violations} entries could not be de-duplicated within "
            "their contest (more entries per contest than lineups available). "
            "DK will reject duplicates — increase lineup count to fix."
        )

    summary = {
        "mode": "auto",
        "cash_entries": sum(is_cash),
        "gpp_entries": num_entries - sum(is_cash),
        "lineup_usage": {
            i: used_count[i] for i in range(num_lineups) if used_count[i] > 0
        },
        "exposure_cap": exposure_cap,
        "dedup_violations": dedup_violations,
    }
    return assignment, warnings, summary

logger = logging.getLogger(__name__)

# DraftKings URLs
DK_BASE = "https://www.draftkings.com"
DK_LINEUPS_URL = f"{DK_BASE}/lineups"
DK_UPLOAD_URL = f"{DK_BASE}/lineup/upload"

# Timeouts
_PAGE_TIMEOUT = 30_000     # 30 s for page loads
_DOWNLOAD_TIMEOUT = 60_000  # 60 s for CSV download
_UPLOAD_TIMEOUT = 60_000    # 60 s for upload processing


class DKEntryService:
    """Browser-automated DraftKings entry management."""

    def __init__(self):
        self._download_dir = Path(tempfile.mkdtemp(prefix="dk_entries_"))

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def download_entries_template(
        self,
        draft_group_id: int,
        sport: str = "cbb",
        progress: Optional[Callable] = None,
    ) -> DKEntriesTemplate:
        """Download the DK entries CSV template via Playwright.

        Parameters
        ----------
        draft_group_id : int
            The DraftKings draft group ID for the slate.
        sport : str
            Sport key: "nba" or "cbb".
        progress : callable, optional
            ``progress(step, detail)`` callback for SSE streaming.

        Returns
        -------
        DKEntriesTemplate
            Parsed template with entries, player reference, and raw CSV.
        """
        from playwright.sync_api import sync_playwright
        from app.services.dk_auth import get_dk_cookies

        if progress:
            progress("auth", "Extracting DK cookies from Chrome...")

        cookies = get_dk_cookies()

        if progress:
            progress("browser", "Launching browser...")

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                accept_downloads=True,
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/122.0.0.0 Safari/537.36"
                ),
            )
            # Inject DK session cookies
            context.add_cookies(cookies)

            page = context.new_page()
            page.set_default_timeout(_PAGE_TIMEOUT)

            try:
                if progress:
                    progress("navigate", "Navigating to DK lineups page...")

                page.goto(DK_LINEUPS_URL, wait_until="domcontentloaded")

                # Wait for page to fully load
                page.wait_for_timeout(2000)

                # Check if we got redirected to login
                if "login" in page.url.lower() or "sign-in" in page.url.lower():
                    raise RuntimeError(
                        "Redirected to login page — DK session expired. "
                        "Please log into draftkings.com in Chrome."
                    )

                if progress:
                    progress("select", f"Selecting {sport.upper()} slate...")

                # Select sport tab (NBA or CBB)
                self._select_sport_tab(page, sport)
                page.wait_for_timeout(1500)

                # Select "Classic" style if available
                self._select_classic_style(page)
                page.wait_for_timeout(1000)

                # Select the correct slate by draft group ID
                self._select_slate(page, draft_group_id)
                page.wait_for_timeout(1500)

                if progress:
                    progress("download", "Downloading entries CSV...")

                # Trigger CSV download
                csv_path = self._trigger_download(page)

                if progress:
                    progress("parse", "Parsing entries template...")

                # Parse the CSV
                raw_csv = Path(csv_path).read_text(encoding="utf-8-sig")
                template = self._parse_entries_csv(raw_csv, draft_group_id, sport)

                if progress:
                    progress(
                        "done",
                        f"Downloaded {len(template.entries)} entries "
                        f"across {len(template.contest_summary)} contests",
                    )

                return template

            finally:
                context.close()
                browser.close()

    def fill_entries(
        self,
        template: DKEntriesTemplate,
        lineup_player_ids: List[List[int]],
        lineup_meta: Optional[List[LineupMeta]] = None,
        selection: Optional[FillSelectionOptions] = None,
    ) -> DKFillResult:
        """Fill a DK entries template with optimised lineup player IDs.

        Parameters
        ----------
        template : DKEntriesTemplate
            Parsed template from ``download_entries_template()``.
        lineup_player_ids : list of list of int
            Each inner list is one lineup's dk_player_ids in slot order
            matching ``template.roster_slots``.
        lineup_meta : list of LineupMeta, optional
            Parallel list of per-lineup floor/ceiling/projection used by the
            'auto' selector to tier lineups against contest type.
        selection : FillSelectionOptions, optional
            Selector configuration. Defaults to ``mode='auto'`` with per-contest
            dedup enabled.

        Returns
        -------
        DKFillResult
            Filled CSV content and metadata.
        """
        if selection is None:
            selection = FillSelectionOptions()

        if not lineup_player_ids:
            return DKFillResult(
                entries_filled=0,
                lineups_used=0,
                warnings=["No lineups provided"],
                filled_csv=template.raw_csv,
            )

        num_entries = len(template.entries)
        num_lineups = len(lineup_player_ids)
        num_slots = len(template.roster_slots)
        cycling = num_lineups < num_entries

        warnings = []

        # Validate all player IDs exist in the template's reference table
        valid_ids = set(template.player_reference.keys())
        for i, lu in enumerate(lineup_player_ids):
            if len(lu) != num_slots:
                warnings.append(
                    f"Lineup {i+1} has {len(lu)} players, expected {num_slots}"
                )
            for pid in lu:
                if pid and pid not in valid_ids:
                    name = f"ID {pid}"
                    warnings.append(
                        f"Lineup {i+1}: player {name} not in DK template "
                        f"(wrong slate?)"
                    )

        if cycling:
            warnings.append(
                f"Cycling {num_lineups} lineups across {num_entries} entries"
            )

        # ── Decide which lineup goes in which entry ──────────────────
        if selection.mode == "round_robin":
            assignment = [idx % num_lineups for idx in range(num_entries)]
            selection_summary: Dict[str, object] = {
                "mode": "round_robin",
                "lineup_usage": {
                    li: assignment.count(li) for li in set(assignment)
                },
            }
        else:  # 'auto' (default)
            assignment, sel_warnings, selection_summary = _smart_assignment(
                template.entries, lineup_meta, num_lineups, selection
            )
            warnings.extend(sel_warnings)

        # Build the filled CSV using proper csv module to handle commas in
        # contest names (e.g. "NBA $50K Slam, $5 Entry")
        raw = template.raw_csv.replace("\r\n", "\n").replace("\r", "\n")
        reader = csv.reader(io.StringIO(raw))
        all_rows = list(reader)
        header_row = all_rows[0] if all_rows else []

        output = io.StringIO()
        writer = csv.writer(output, lineterminator="\n")

        # Write header
        writer.writerow(header_row)

        contest_counts: Dict[str, int] = {}

        for idx, entry in enumerate(template.entries):
            lineup = lineup_player_ids[assignment[idx]]

            # Get original row to preserve extra columns (instructions, etc.)
            orig_row = all_rows[idx + 1] if idx + 1 < len(all_rows) else []

            # Build the filled row
            row = [
                str(entry.entry_id),
                entry.contest_name,
                str(entry.contest_id),
                entry.entry_fee,
            ]
            # Add player IDs for each slot
            for slot_idx in range(num_slots):
                if slot_idx < len(lineup):
                    row.append(str(lineup[slot_idx]))
                else:
                    row.append("")

            # Preserve remaining columns (instructions, player ref, etc.)
            extra_start = 4 + num_slots
            if len(orig_row) > extra_start:
                row.extend(orig_row[extra_start:])

            writer.writerow(row)
            contest_counts[entry.contest_name] = (
                contest_counts.get(entry.contest_name, 0) + 1
            )

        # Add any remaining lines (player reference table, etc.)
        data_end = 1 + num_entries
        for remaining_row in all_rows[data_end:]:
            writer.writerow(remaining_row)

        filled_csv = output.getvalue()

        return DKFillResult(
            entries_filled=num_entries,
            lineups_used=len(set(assignment)),
            lineup_cycling=cycling,
            warnings=warnings,
            filled_csv=filled_csv,
            contest_summary=contest_counts,
            selection_summary=selection_summary,
        )

    def upload_entries(
        self,
        filled_csv: str,
        progress: Optional[Callable] = None,
    ) -> DKUploadResult:
        """Upload a filled entries CSV to DraftKings via Playwright.

        Parameters
        ----------
        filled_csv : str
            The filled CSV content to upload.
        progress : callable, optional
            ``progress(step, detail)`` callback for SSE streaming.

        Returns
        -------
        DKUploadResult
        """
        from playwright.sync_api import sync_playwright
        from app.services.dk_auth import get_dk_cookies

        if progress:
            progress("auth", "Extracting DK cookies from Chrome...")

        cookies = get_dk_cookies()

        # Write CSV to temp file
        csv_path = self._download_dir / "DKEntries_filled.csv"
        csv_path.write_text(filled_csv, encoding="utf-8-sig")

        if progress:
            progress("browser", "Launching browser...")

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/122.0.0.0 Safari/537.36"
                ),
            )
            context.add_cookies(cookies)

            page = context.new_page()
            page.set_default_timeout(_PAGE_TIMEOUT)

            try:
                if progress:
                    progress("navigate", "Navigating to DK upload page...")

                page.goto(DK_UPLOAD_URL, wait_until="domcontentloaded")
                page.wait_for_timeout(2000)

                # Check for login redirect
                if "login" in page.url.lower() or "sign-in" in page.url.lower():
                    return DKUploadResult(
                        success=False,
                        errors=["Session expired — redirected to login page"],
                    )

                if progress:
                    progress("upload", "Uploading entries CSV...")

                # Find the file input and upload
                result = self._perform_upload(page, str(csv_path))

                if progress:
                    if result.success:
                        progress(
                            "done",
                            f"Successfully updated {result.entries_updated} entries",
                        )
                    else:
                        progress("error", f"Upload failed: {'; '.join(result.errors)}")

                return result

            finally:
                context.close()
                browser.close()

    def download_fill_upload(
        self,
        draft_group_id: int,
        sport: str,
        lineup_player_ids: List[List[int]],
        progress: Optional[Callable] = None,
    ) -> DKFullResult:
        """Full automated flow: download template -> fill -> upload.

        Parameters
        ----------
        draft_group_id : int
            DraftKings draft group ID for the slate.
        sport : str
            Sport: "nba" or "cbb".
        lineup_player_ids : list of list of int
            Lineup player IDs in slot order.
        progress : callable, optional
            ``progress(step, detail)`` callback.

        Returns
        -------
        DKFullResult
        """
        result = DKFullResult()

        # Step 1: Download template
        try:
            template = self.download_entries_template(
                draft_group_id, sport, progress=progress,
            )
            result.download_ok = True
            result.template = template
        except Exception as e:
            logger.error(f"DK entry download failed: {e}")
            result.error = f"Download failed: {e}"
            if progress:
                progress("error", str(e))
            return result

        if not template.entries:
            result.error = "No entries found in DK template — do you have contests entered?"
            if progress:
                progress("error", result.error)
            return result

        # Step 2: Fill entries
        try:
            if progress:
                progress("fill", "Filling entries with optimised lineups...")

            fill_result = self.fill_entries(template, lineup_player_ids)
            result.fill_ok = True
            result.fill_result = fill_result
        except Exception as e:
            logger.error(f"DK entry fill failed: {e}")
            result.error = f"Fill failed: {e}"
            if progress:
                progress("error", str(e))
            return result

        # Step 3: Upload
        try:
            upload_result = self.upload_entries(
                fill_result.filled_csv, progress=progress,
            )
            result.upload_ok = upload_result.success
            result.upload_result = upload_result
        except Exception as e:
            logger.error(f"DK entry upload failed: {e}")
            result.error = f"Upload failed: {e}"
            if progress:
                progress("error", str(e))

        return result

    # ------------------------------------------------------------------
    # Private: Playwright page interactions
    # ------------------------------------------------------------------

    def _select_sport_tab(self, page, sport: str):
        """Click the sport tab on the DK lineups page."""
        sport_label = {"nba": "NBA", "cbb": "CBB"}.get(sport, sport.upper())

        # Try multiple selector strategies for the sport tab
        selectors = [
            f"button:has-text('{sport_label}')",
            f"a:has-text('{sport_label}')",
            f"[data-sport='{sport_label}']",
            f".sport-tab:has-text('{sport_label}')",
            f"text={sport_label}",
        ]
        for sel in selectors:
            try:
                el = page.query_selector(sel)
                if el and el.is_visible():
                    el.click()
                    logger.info(f"Clicked sport tab: {sport_label} via {sel}")
                    return
            except Exception:
                continue

        logger.warning(f"Could not find sport tab for {sport_label} — may already be selected")

    def _select_classic_style(self, page):
        """Select 'Classic' game style if the dropdown exists."""
        selectors = [
            "button:has-text('Classic')",
            "a:has-text('Classic')",
            "[data-style='classic']",
            "text=Classic",
        ]
        for sel in selectors:
            try:
                el = page.query_selector(sel)
                if el and el.is_visible():
                    el.click()
                    logger.info(f"Selected Classic style via {sel}")
                    return
            except Exception:
                continue

    def _select_slate(self, page, draft_group_id: int):
        """Select the correct slate/draft group from the dropdown."""
        # Try to find a select or dropdown with the draft group ID
        # DK may use various selector patterns

        # Strategy 1: Look for select elements
        selects = page.query_selector_all("select")
        for sel in selects:
            options = sel.query_selector_all("option")
            for opt in options:
                val = opt.get_attribute("value") or ""
                text = opt.inner_text()
                if str(draft_group_id) in val or str(draft_group_id) in text:
                    sel.select_option(value=val)
                    logger.info(f"Selected slate via select option: {text}")
                    return

        # Strategy 2: Look for buttons/links with DG ID
        dg_selectors = [
            f"[data-draft-group-id='{draft_group_id}']",
            f"[data-draftgroupid='{draft_group_id}']",
            f"[value='{draft_group_id}']",
        ]
        for sel in dg_selectors:
            try:
                el = page.query_selector(sel)
                if el:
                    el.click()
                    logger.info(f"Selected slate via {sel}")
                    return
            except Exception:
                continue

        logger.warning(
            f"Could not find slate selector for DG {draft_group_id} — "
            f"using whatever slate is already selected"
        )

    def _trigger_download(self, page) -> str:
        """Click the download button and capture the downloaded CSV file path."""
        # Look for download button
        download_selectors = [
            "button:has-text('Download')",
            "a:has-text('Download')",
            "button:has-text('Export')",
            "a:has-text('Export')",
            ".download-btn",
            "[data-action='download']",
        ]

        for sel in download_selectors:
            try:
                el = page.query_selector(sel)
                if el and el.is_visible():
                    # Start waiting for download before clicking
                    with page.expect_download(timeout=_DOWNLOAD_TIMEOUT) as download_info:
                        el.click()
                    download = download_info.value
                    # Save the download
                    save_path = str(self._download_dir / download.suggested_filename)
                    download.save_as(save_path)
                    logger.info(f"Downloaded entries CSV: {save_path}")
                    return save_path
            except Exception as e:
                logger.debug(f"Download attempt with {sel} failed: {e}")
                continue

        raise RuntimeError(
            "Could not find or click the Download button on the DK lineups page. "
            "The page layout may have changed."
        )

    def _perform_upload(self, page, csv_path: str) -> DKUploadResult:
        """Upload a CSV file on the DK upload page and parse the result."""
        # Find file input
        file_input = page.query_selector("input[type='file']")
        if not file_input:
            # Try to find hidden file inputs
            file_input = page.query_selector("input[accept='.csv']")
        if not file_input:
            # Click an upload button that might reveal the file input
            upload_btn_selectors = [
                "button:has-text('Upload')",
                "button:has-text('Choose File')",
                "button:has-text('Select File')",
                ".upload-btn",
            ]
            for sel in upload_btn_selectors:
                try:
                    el = page.query_selector(sel)
                    if el and el.is_visible():
                        el.click()
                        page.wait_for_timeout(1000)
                        file_input = page.query_selector("input[type='file']")
                        if file_input:
                            break
                except Exception:
                    continue

        if not file_input:
            return DKUploadResult(
                success=False,
                errors=["Could not find file upload input on DK upload page"],
            )

        # Upload the file
        file_input.set_input_files(csv_path)
        logger.info(f"Set file input to {csv_path}")

        # Wait for processing
        page.wait_for_timeout(3000)

        # Look for a submit/confirm button
        submit_clicked = False
        submit_selectors = [
            "button:has-text('Import')",
            "button:has-text('Submit')",
            "button:has-text('Upload')",
            "button:has-text('Save')",
            "button:has-text('Confirm')",
            "button[type='submit']",
        ]
        for sel in submit_selectors:
            try:
                el = page.query_selector(sel)
                if el and el.is_visible():
                    el.click()
                    submit_clicked = True
                    logger.info(f"Clicked submit button via {sel}")
                    break
            except Exception:
                continue

        if not submit_clicked:
            logger.warning("Could not find submit button — file was set but not confirmed")
            return DKUploadResult(
                success=False,
                errors=["Could not find submit/confirm button after file upload"],
            )

        # Wait for DK to process the upload
        page.wait_for_timeout(5000)

        # Parse the result from the page
        return self._parse_upload_result(page)

    def _parse_upload_result(self, page) -> DKUploadResult:
        """Parse the upload result from the DK page after submission."""
        page_text = page.inner_text("body").lower()

        # Look for success indicators
        success_patterns = [
            r"(\d+)\s*entr(?:y|ies)\s*(?:updated|saved|imported)",
            r"successfully\s*(?:updated|saved|imported)\s*(\d+)",
            r"(\d+)\s*lineup",
        ]
        for pattern in success_patterns:
            match = re.search(pattern, page_text)
            if match:
                count = int(match.group(1))
                return DKUploadResult(
                    success=True,
                    entries_updated=count,
                    message=f"Successfully updated {count} entries",
                )

        # Check for generic success
        if "success" in page_text or "updated" in page_text or "saved" in page_text:
            return DKUploadResult(
                success=True,
                message="Upload appears successful",
            )

        # Check for errors
        error_patterns = [
            r"error[:\s]+(.+?)(?:\.|$)",
            r"fail(?:ed)?[:\s]+(.+?)(?:\.|$)",
            r"invalid[:\s]+(.+?)(?:\.|$)",
            r"not eligible",
        ]
        errors = []
        for pattern in error_patterns:
            matches = re.findall(pattern, page_text)
            errors.extend(matches)

        if errors:
            return DKUploadResult(
                success=False,
                errors=errors[:5],  # Cap at 5 errors
            )

        # Can't determine result — take a screenshot for debugging
        try:
            screenshot_path = str(self._download_dir / "upload_result.png")
            page.screenshot(path=screenshot_path)
            logger.info(f"Upload result screenshot saved: {screenshot_path}")
        except Exception:
            pass

        return DKUploadResult(
            success=False,
            errors=["Could not determine upload result — check DraftKings website"],
        )

    # ------------------------------------------------------------------
    # Private: CSV parsing
    # ------------------------------------------------------------------

    def _parse_entries_csv(
        self, raw_csv: str, draft_group_id: int, sport: str,
    ) -> DKEntriesTemplate:
        """Parse a DK entries CSV template into structured data."""
        reader = csv.reader(io.StringIO(raw_csv))
        rows = list(reader)

        if not rows:
            raise ValueError("Empty CSV file")

        header = rows[0]

        # Find the roster slot columns (between Entry Fee and the blank/Instructions column)
        # Header: Entry ID, Contest Name, Contest ID, Entry Fee, G, G, G, F, F, F, UTIL, UTIL, , Instructions, ...
        roster_start = 4  # After Entry Fee
        roster_end = roster_start
        for i in range(roster_start, len(header)):
            if not header[i].strip() or header[i].strip() == "Instructions":
                break
            roster_end = i + 1

        roster_slots = [header[i].strip() for i in range(roster_start, roster_end)]
        num_slots = len(roster_slots)

        # Parse entry rows (rows 1..N until we hit empty or instruction-only rows)
        entries = []
        contest_summary: Dict[str, int] = {}
        for row_idx in range(1, len(rows)):
            row = rows[row_idx]
            if len(row) < 4:
                # Blank rows or player reference section — stop parsing entries
                break
            # Entry ID should be numeric
            try:
                entry_id = int(row[0].strip())
            except (ValueError, IndexError):
                # Could be a blank intermediate row or the start of
                # the player reference section — skip it
                continue

            contest_name = row[1].strip() if len(row) > 1 else ""
            try:
                contest_id = int(row[2].strip()) if len(row) > 2 else 0
            except ValueError:
                contest_id = 0
            entry_fee = row[3].strip() if len(row) > 3 else ""

            # Extract player IDs
            player_ids = []
            for i in range(roster_start, roster_start + num_slots):
                if i < len(row) and row[i].strip():
                    try:
                        player_ids.append(int(row[i].strip()))
                    except ValueError:
                        player_ids.append(None)
                else:
                    player_ids.append(None)

            entries.append(DKEntryRow(
                entry_id=entry_id,
                contest_name=contest_name,
                contest_id=contest_id,
                entry_fee=entry_fee,
                player_ids=player_ids,
            ))
            contest_summary[contest_name] = contest_summary.get(contest_name, 0) + 1

        # Parse the player reference table (appears after entries + blank row)
        # Look for rows with player ID data after the entry rows
        player_reference: Dict[int, str] = {}
        ref_start = 1 + len(entries)
        for row_idx in range(ref_start, len(rows)):
            row = rows[row_idx]
            # Look for rows that have an ID column value (numeric, 7-8 digits)
            for cell_idx, cell in enumerate(row):
                cell = cell.strip()
                if cell and cell.isdigit() and len(cell) >= 7:
                    pid = int(cell)
                    # The name is usually in the column before or 2 before
                    name = ""
                    if cell_idx >= 1 and row[cell_idx - 1].strip():
                        name = row[cell_idx - 1].strip()
                    elif cell_idx >= 2 and row[cell_idx - 2].strip():
                        name = row[cell_idx - 2].strip()
                    if name:
                        player_reference[pid] = name

        return DKEntriesTemplate(
            draft_group_id=draft_group_id,
            sport=sport,
            roster_slots=roster_slots,
            entries=entries,
            player_reference=player_reference,
            raw_csv=raw_csv,
            contest_summary=contest_summary,
        )
