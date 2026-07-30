from typing import List, Dict, Any
from datetime import date
import re
import unicodedata


def get_current_nba_season() -> str:
    """Return the current NBA season string (e.g. '2025-26').

    The NBA season runs Oct → Jun/Jul across two calendar years:
      - Oct 2025 through Sep 2026 → '2025-26'
      - Jan 2026 through Sep 2026 → '2025-26'
    We use October as the cutoff since that's when the regular
    season tips off and rosters are finalized.
    """
    today = date.today()
    year = today.year
    month = today.month
    if month >= 10:  # Oct–Dec → new season has started
        return f"{year}-{str(year + 1)[-2:]}"
    else:  # Jan–Sep → still in the season that started last Oct
        return f"{year - 1}-{str(year)[-2:]}"


def format_minutes(minutes: float) -> str:
    """Format minutes as MM:SS string."""
    whole_minutes = int(minutes)
    seconds = int((minutes - whole_minutes) * 60)
    return f"{whole_minutes}:{seconds:02d}"


def calculate_per_game_average(totals: List[float]) -> float:
    """Calculate per-game average from a list of totals."""
    if not totals:
        return 0.0
    return round(sum(totals) / len(totals), 1)


def normalize_to_total(
    values: Dict[int, float], target_total: float = 240.0
) -> Dict[int, float]:
    """Normalize a dict of values to sum to a target total."""
    current_total = sum(values.values())
    if current_total == 0:
        return values

    factor = target_total / current_total
    return {k: round(v * factor, 1) for k, v in values.items()}


def position_map(position: str) -> str:
    """Normalize position strings to standard abbreviations."""
    pos_map = {
        "Guard": "G",
        "Guard-Forward": "G-F",
        "Forward-Guard": "F-G",
        "Forward": "F",
        "Forward-Center": "F-C",
        "Center-Forward": "C-F",
        "Center": "C",
        "G": "G",
        "F": "F",
        "C": "C",
        "PG": "G",
        "SG": "G",
        "SF": "F",
        "PF": "F",
    }
    return pos_map.get(position, "F")


def clamp(value: float, min_val: float, max_val: float) -> float:
    """Clamp a value between min and max."""
    return max(min_val, min(value, max_val))


def normalize_player_name(name: str) -> str:
    """Normalize a player name for cross-source matching.

    Strips suffixes (Jr., Sr., III, IV, II), removes periods and extra
    whitespace, lowercases, and transliterates accented/diacritical
    characters to their ASCII equivalents.

    This is critical for matching names across data sources that handle
    diacriticals differently:
        - BDL API:     "Nikola Jokic"    (ASCII)
        - nba_api:     "Nikola Jokić"    (Unicode ć)
        - DraftKings:  "Nikola Jokic"    (ASCII)

    Examples:
        "Gary Trent Jr."       -> "gary trent"
        "P.J. Washington"      -> "pj washington"
        "Nikola Jokić"         -> "nikola jokic"
        "Luka Dončić"          -> "luka doncic"
        "Jonas Valančiūnas"    -> "jonas valanciunas"
    """
    n = name.strip().lower()
    # Transliterate accented characters to ASCII (ć→c, č→c, ñ→n, ū→u, etc.)
    n = unicodedata.normalize("NFKD", n)
    n = "".join(c for c in n if not unicodedata.combining(c))
    # Remove common suffixes
    n = re.sub(r"\b(jr\.?|sr\.?|iii|iv|ii)\s*$", "", n).strip()
    # Remove periods (P.J. -> PJ)
    n = n.replace(".", "")
    # Remove apostrophes and hyphens for uniform matching:
    #   De'Aaron Fox  -> deaaron fox
    #   Shai Gilgeous-Alexander -> shai gilgeous alexander
    #   Trayce Jackson-Davis -> trayce jackson davis
    n = n.replace("'", "").replace("\u2019", "")  # straight + curly apostrophe
    n = n.replace("-", " ")
    # Collapse whitespace
    n = re.sub(r"\s+", " ", n).strip()
    return n
