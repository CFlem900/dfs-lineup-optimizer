"""Beat Reporter NLP Engine — Extract starter confirmations from tweet text.

Scans raw text alerts (Underdog NBA, Shams Charania, Woj, etc.) for
"confirmed starter" signals and matches extracted player names against
the DraftKings player pool using normalized string matching.

The scanner uses a two-gate approach:
  1. **Negative gate**: Reject tweets containing "out", "ruled out",
     "will not play", etc. — these are injury reports, not starter news.
  2. **Positive gate**: Accept tweets containing "will start", "starting
     lineup", "gets the start", etc.

When a match is found and the player's DK salary is <= $5,000, the
player is flagged as ``news_confirmed_starter = True`` on their
``PlayerMinutes`` object, which triggers a 30-minute projection floor
in the TopDownMinutes engine.

Usage
-----
    parser = NewsParserService()

    # Scan tweets against the DK player pool
    overrides = parser.scan_alerts(
        alerts=["[Underdog NBA] Bez Mbeng will start for the Grizzlies tonight."],
        dk_player_names={"bez mbeng": player_obj, "ja morant": player_obj, ...}
    )
    # overrides = [{"normalized_name": "bez mbeng", "source": "Underdog NBA", ...}]
"""

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

from app.utils.helpers import normalize_player_name

logger = logging.getLogger(__name__)

# ── Keyword patterns ──────────────────────────────────────────────────

# Negative gate: if ANY of these appear, the tweet is about a player
# being OUT, not starting.  These are checked FIRST to avoid false
# positives like "X will start in place of Y (ruled out)".
_NEGATIVE_PATTERNS = re.compile(
    r"""
    \b(?:
        ruled\s+out
        | will\s+not\s+play
        | won'?t\s+play
        | is\s+out\b
        | has\s+been\s+ruled\s+out
        | will\s+miss
        | will\s+sit
        | sidelined
        | placed\s+on\s+(?:IL|IR)
        | out\s+(?:tonight|indefinitely|for\s+the\s+season)
        | did\s+not\s+(?:play|participate|travel)
        | dnp
        | suspended
        | inactive
    )\b
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Positive gate: tweet must contain at least one of these to signal
# a confirmed starter.
_POSITIVE_PATTERNS = re.compile(
    r"""
    \b(?:
        will\s+start
        | is\s+starting
        | starts?\s+(?:at|for|tonight|today|in\s+place)
        | starting\s+lineup
        | gets?\s+the\s+start
        | named\s+(?:a\s+)?starter
        | confirmed\s+(?:as\s+)?start(?:er|ing)
        | in\s+the\s+starting\s+(?:lineup|five|unit)
        | elevated\s+to\s+(?:the\s+)?start(?:er|ing)
        | inserted\s+into\s+(?:the\s+)?starting
        | promoted\s+to\s+start(?:er|ing)
        | moved\s+(?:into|to)\s+(?:the\s+)?starting
        | expected\s+to\s+start
        | set\s+to\s+start
        | slated\s+to\s+start
        | tabbed\s+to\s+start
    )\b
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Source extraction: grab the [Source Name] from tweet prefix
_SOURCE_RE = re.compile(r"^\[([^\]]+)\]")

# ── Configuration ─────────────────────────────────────────────────────
NEWS_STARTER_SALARY_CAP = 5000      # Only flag players with DK salary <= this
NEWS_STARTER_MINUTE_FLOOR = 30.0    # Forced projection for news-confirmed starters
NEWS_STARTER_SYNTHETIC_FPPM = 0.85  # Fallback FPPM for news-confirmed sparse players


@dataclass
class NewsStarterOverride:
    """A confirmed-starter detection from a news alert."""
    player_name: str                    # Original name from the tweet
    normalized_name: str                # Normalized for matching
    source: str                         # e.g. "Underdog NBA", "Shams Charania"
    raw_text: str                       # The full tweet text
    detected_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


class NewsParserService:
    """NLP scanner that extracts starter confirmations from tweet text.

    Stateless — each ``scan_alerts()`` call processes fresh input.
    Thread-safe (no mutable shared state).
    """

    def scan_alerts(
        self,
        alerts: List[str],
        dk_player_names: Dict[str, Any],
    ) -> List[NewsStarterOverride]:
        """Scan a batch of alerts for confirmed-starter signals.

        Parameters
        ----------
        alerts : list[str]
            Raw tweet/alert text strings.
        dk_player_names : dict[str, Any]
            Mapping of normalized player name → player object (or any truthy value).
            Built from the DK player pool before calling this method.

        Returns
        -------
        list[NewsStarterOverride]
            Detected starter overrides.  Empty if no matches found.
        """
        overrides: List[NewsStarterOverride] = []
        seen_names: Set[str] = set()  # Dedup within a single batch

        for raw_text in alerts:
            if not raw_text or not isinstance(raw_text, str):
                continue

            text = raw_text.strip()

            # Gate 1: Reject negative (injury/out) tweets
            if _NEGATIVE_PATTERNS.search(text):
                logger.debug(
                    "[NewsNLP] REJECTED (negative gate): %.80s...", text
                )
                continue

            # Gate 2: Require positive (starting) signal
            if not _POSITIVE_PATTERNS.search(text):
                logger.debug(
                    "[NewsNLP] SKIPPED (no positive trigger): %.80s...", text
                )
                continue

            # Extract source
            source_match = _SOURCE_RE.match(text)
            source = source_match.group(1) if source_match else "Unknown"

            # Entity resolution: normalize the entire tweet and scan for
            # any DK player name appearing as a substring.
            text_normalized = normalize_player_name(text)

            for dk_norm_name in dk_player_names:
                if not dk_norm_name:
                    continue
                # Require the full normalized name to appear as a substring
                # in the normalized tweet text.  This handles:
                #   "bez mbeng" in "underdog nba bez mbeng will start..."
                if dk_norm_name in text_normalized and dk_norm_name not in seen_names:
                    seen_names.add(dk_norm_name)

                    # Recover the original display name if possible
                    player_obj = dk_player_names[dk_norm_name]
                    display_name = (
                        getattr(player_obj, "player_name", None)
                        or getattr(player_obj, "display_name", None)
                        or dk_norm_name
                    )

                    override = NewsStarterOverride(
                        player_name=display_name,
                        normalized_name=dk_norm_name,
                        source=source,
                        raw_text=text,
                    )
                    overrides.append(override)

                    logger.info(
                        "[NewsNLP] MATCH: '%s' confirmed starting | "
                        "source=%s | tweet=%.100s...",
                        display_name, source, text,
                    )

        if overrides:
            logger.info(
                "[NewsNLP] Batch complete: %d starter(s) detected from %d alerts",
                len(overrides), len(alerts),
            )
        return overrides

    def apply_overrides(
        self,
        overrides: List[NewsStarterOverride],
        rotation: List,
        team_abbr: str = "",
    ) -> int:
        """Stamp ``news_confirmed_starter = True`` on matched rotation players.

        Parameters
        ----------
        overrides : list[NewsStarterOverride]
            Detected starters from ``scan_alerts()``.
        rotation : list[PlayerMinutes]
            The team's rotation list (modified in-place).
        team_abbr : str
            For logging.

        Returns
        -------
        int
            Number of players flagged.
        """
        if not overrides:
            return 0

        override_names = {o.normalized_name: o for o in overrides}
        flagged = 0

        for rp in rotation:
            rp_norm = normalize_player_name(rp.player_name or "")
            match = override_names.get(rp_norm)
            if not match:
                continue

            dk_sal = getattr(rp, "dk_salary", None) or 0
            if dk_sal > NEWS_STARTER_SALARY_CAP:
                logger.info(
                    "[NewsNLP] SKIP (salary $%s > $%s cap): %s",
                    f"{dk_sal:,}", f"{NEWS_STARTER_SALARY_CAP:,}",
                    rp.player_name,
                )
                continue

            # Skip if already confirmed by a stronger signal
            if getattr(rp, "is_confirmed_starter", None) is True:
                logger.debug(
                    "[NewsNLP] SKIP (already confirmed starter): %s",
                    rp.player_name,
                )
                continue

            rp.news_confirmed_starter = True
            rp.is_confirmed_starter = True  # Also set the standard flag
            flagged += 1

            logger.info(
                "[News NLP Override] %s confirmed starting | "
                "source=%s, salary=$%s | Assigned %.1f min baseline",
                rp.player_name, match.source,
                f"{dk_sal:,}" if dk_sal else "N/A",
                NEWS_STARTER_MINUTE_FLOOR,
            )

        if flagged:
            logger.info(
                "[NewsNLP][%s] %d player(s) flagged as news-confirmed starters",
                team_abbr, flagged,
            )

        return flagged
