import logging
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from typing import List, Optional, Dict, Any, Set, Tuple

import httpx
from nba_api.stats.endpoints import (
    commonteamroster,
    leaguedashplayerbiostats,
    playergamelog,
    leaguegamefinder,
    scoreboardv2,
)
from nba_api.stats.static import teams as nba_teams

from app.config import get_settings
from app.models.player import PlayerMinutes
from app.services.sport_data_service import SportDataService
from app.utils.helpers import get_current_nba_season, normalize_player_name

logger = logging.getLogger(__name__)

settings = get_settings()

# ---------------------------------------------------------------------------
# Module-level rotation cache — shared across NBAApiService instances so
# that hot-reload (uvicorn --reload) doesn't lose the cache.
# ---------------------------------------------------------------------------
_rotation_cache: Dict[int, Tuple[float, List["PlayerMinutes"]]] = {}
_ROTATION_CACHE_TTL = 1200  # 20 minutes — trades/injuries can change rosters mid-day

# League-average per-minute stat rates by position (2023-24 season).
# Used as Bayesian priors for players with small sample sizes (<15 games).
# Format: position -> {stat_field -> per_minute_rate}
_POSITION_PRIOR_RATES: Dict[str, Dict[str, float]] = {
    "PG": {"PTS": 0.55, "REB": 0.13, "AST": 0.25, "STL": 0.040, "BLK": 0.010, "TOV": 0.100, "FG3M": 0.080},
    "SG": {"PTS": 0.54, "REB": 0.13, "AST": 0.15, "STL": 0.035, "BLK": 0.012, "TOV": 0.080, "FG3M": 0.085},
    "SF": {"PTS": 0.50, "REB": 0.18, "AST": 0.12, "STL": 0.030, "BLK": 0.018, "TOV": 0.075, "FG3M": 0.070},
    "PF": {"PTS": 0.48, "REB": 0.22, "AST": 0.10, "STL": 0.025, "BLK": 0.025, "TOV": 0.070, "FG3M": 0.055},
    "C":  {"PTS": 0.48, "REB": 0.28, "AST": 0.08, "STL": 0.022, "BLK": 0.040, "TOV": 0.065, "FG3M": 0.025},
    "G":  {"PTS": 0.54, "REB": 0.13, "AST": 0.20, "STL": 0.038, "BLK": 0.011, "TOV": 0.090, "FG3M": 0.082},
    "F":  {"PTS": 0.49, "REB": 0.20, "AST": 0.11, "STL": 0.028, "BLK": 0.022, "TOV": 0.072, "FG3M": 0.062},
}

# Default minutes for recently-traded players with 0 games on their new team.
# Conservative "rotation player" defaults by position.  The spot-start system
# can elevate these further if a starter is injured.
_TRADE_DEFAULT_MINUTES: Dict[str, float] = {
    "G": 14.0, "PG": 14.0, "SG": 14.0,
    "F": 12.0, "SF": 12.0, "PF": 12.0,
    "C": 12.0, "G-F": 13.0, "F-G": 13.0, "F-C": 12.0, "C-F": 12.0,
}

# ---------------------------------------------------------------------------
# POSITION OVERRIDES — hardcoded fixes for BDL position misclassifications.
#
# BallDontLie uses simplified/generic positions ("G", "F", "G-F") and
# sometimes flat-out misclassifies players.  This dict maps player names
# (lowercase) to their correct NBA-style position strings.  Checked FIRST
# during PlayerMinutes construction — before BDL or DB cache position data.
#
# Keys:   lowercase full name  (e.g. "daniss jenkins")
# Values: corrected position   (e.g. "G" for guard family)
#
# When adding entries: use TopDownMinutes-compatible position strings.
# The allocator uses _POS_FAMILY mapping: G → PG/SG, F → SF/PF, C → C.
# For guard/forward hybrids use "G-F".
# ---------------------------------------------------------------------------
_POSITION_OVERRIDES_RAW: Dict[str, str] = {
    # BDL lists Jenkins as "F" but he's DET's starting PG
    "daniss jenkins": "G",
    # BDL lists Prosper as "G" but he's a PF/C
    "olivier-maxence prosper": "F-C",
}
# Pre-normalize keys so lookups match regardless of suffix/diacritics/punctuation
POSITION_OVERRIDES: Dict[str, str] = {
    normalize_player_name(k): v for k, v in _POSITION_OVERRIDES_RAW.items()
}


def _resolve_position(
    player_name: str,
    bdl_position: str,
    dk_position: Optional[str] = None,
) -> str:
    """Resolve a player's position with override → DK → BDL fallback chain.

    Priority:
      1. POSITION_OVERRIDES dict (hardcoded fixes for known BDL errors)
      2. DK position (when BDL position is generic "G"/"F"/"C" and DK
         has a more specific string like "PG/SG" or "SF/PF")
      3. BDL position as-is

    DK position mapping:
      "PG"    → "G"     "PG/SG" → "G"     "PG/SF" → "G-F"
      "SG"    → "G"     "SG/SF" → "G-F"
      "SF"    → "F"     "SF/PF" → "F"
      "PF"    → "F"     "PF/C"  → "F-C"
      "C"     → "C"

    Returns a position string compatible with TopDownMinutes allocator.
    """
    # 1. Hardcoded overrides (highest priority)
    override = POSITION_OVERRIDES.get(normalize_player_name(player_name))
    if override:
        logger.debug(
            "[PosResolve] OVERRIDE %s: %s → %s",
            player_name, bdl_position, override,
        )
        return override

    # 2. DK position fallback when BDL is generic/missing
    _generic_bdl = bdl_position in ("G", "F", "C", "", None)
    if dk_position and _generic_bdl:
        mapped = _dk_position_to_nba(dk_position)
        if mapped and mapped != bdl_position:
            logger.debug(
                "[PosResolve] DK FALLBACK %s: bdl=%s → dk=%s → %s",
                player_name, bdl_position, dk_position, mapped,
            )
            return mapped

    # 3. BDL position as-is
    return bdl_position or "F"


# DK position string → TopDownMinutes-compatible position.
_DK_POS_MAP: Dict[str, str] = {
    "PG": "G", "SG": "G", "SF": "F", "PF": "F", "C": "C",
    "PG/SG": "G", "SG/PG": "G",
    "SG/SF": "G-F", "SF/SG": "G-F",
    "PG/SF": "G-F", "SF/PG": "G-F",
    "SF/PF": "F", "PF/SF": "F",
    "PF/C": "F-C", "C/PF": "F-C",
    "PG/SG/SF": "G-F",
}


def _dk_position_to_nba(dk_pos: str) -> Optional[str]:
    """Convert a DK position string to TopDownMinutes-compatible format."""
    if not dk_pos:
        return None
    return _DK_POS_MAP.get(dk_pos.strip())


def _salary_adjusted_trade_minutes(position: str, dk_salary: int) -> float:
    """Trade-default minutes scaled by DK salary as a market role signal.

    DK pricing reflects market consensus on a player's expected role.
    Instead of flat position defaults (12-14 min), we scale up for
    high-salary players who are likely starters or key rotation pieces.

    Salary tiers (with smooth interpolation):
        $9K+    → 34 min (unquestioned starter)
        $8K+    → 30 min (starter)
        $6K-$8K → 26-30 min (interpolated key rotation / spot-starter)
        $4.5K-$6K → 20-26 min (interpolated rotation player)
        <$4.5K  → position default (12-14 min bench)
    """
    base = _TRADE_DEFAULT_MINUTES.get(position, 14.0)
    if dk_salary >= 9000:
        return max(base, 34.0)
    elif dk_salary >= 8000:
        return max(base, 30.0)
    elif dk_salary >= 6000:
        frac = (dk_salary - 6000) / 2000.0
        return max(base, 26.0 + frac * 4.0)
    elif dk_salary >= 4500:
        frac = (dk_salary - 4500) / 1500.0
        return max(base, 20.0 + frac * 6.0)
    else:
        return base


def build_synthetic_player(
    player_name: str,
    team_id: int,
    dk_salary: int,
    dk_fppg: float = 0.0,
    dk_position: str = "SF",
) -> PlayerMinutes:
    """Create a synthetic PlayerMinutes for a DK-listed player with zero BDL data.

    Used as a last-resort fallback when BDL returns absolutely no game logs
    for a player who appears in the DraftKings CSV.  Without this, such
    players (two-way, recent call-ups, 10-day contracts) are silently
    dropped from the optimizer pool — a catastrophic omission when DK
    ownership projections are non-trivial.

    Estimation logic:
      • minutes  = salary-driven curve (same as trade detection)
      • per-min rates = positional priors from _POSITION_PRIOR_RATES
      • If DK FPPG is provided, cross-check: fppg / fppm_estimate gives
        an implied minutes figure.  Use the higher of salary-driven and
        FPPG-implied minutes (FPPG captures recent form that salary lags).

    Args:
        player_name: Full name as it appears on DraftKings.
        team_id:     NBA team ID the player is rostered to tonight.
        dk_salary:   DraftKings salary ($3,000 – $12,000 typical range).
        dk_fppg:     DraftKings projected fantasy points per game (0 = unknown).
        dk_position: DK position string (e.g. "PG", "SF/PF", "C").

    Returns:
        A PlayerMinutes object ready for inclusion in the rotation pool.
    """
    # Resolve position through the override → DK → default chain
    pos = _resolve_position(player_name, "F", dk_position)

    # Minutes from salary curve
    salary_mins = _salary_adjusted_trade_minutes(pos, dk_salary)

    # FPPG-implied minutes: DK FPPG ÷ estimated fantasy-points-per-minute.
    # Average NBA FPPM across positions is roughly 1.0-1.3 on DK scoring.
    # Use 1.0 as a conservative denominator (1 FPPG ≈ 1 minute of play).
    fppg_mins = 0.0
    if dk_fppg > 0:
        # Conservative: assume 1.0 FPPM for guards, 1.1 for bigs (reb bonus)
        _fppm_est = 1.1 if pos in ("C", "F-C", "PF") else 1.0
        fppg_mins = dk_fppg / _fppm_est

    # Take the higher signal — salary lags form, FPPG captures recent usage
    projected_mins = max(salary_mins, fppg_mins)
    # Floor: DK wouldn't list them if they play 0 min
    projected_mins = max(projected_mins, 8.0)
    # Cap: synthetic players shouldn't dominate the allocation
    projected_mins = min(projected_mins, 28.0)

    # Per-minute stat priors
    pos_prior = _POSITION_PRIOR_RATES.get(
        pos, _POSITION_PRIOR_RATES.get("SF", {})
    )

    _synthetic_id = hash(f"synth-{player_name}-{team_id}") & 0x7FFFFFFF

    pm = PlayerMinutes(
        player_id=_synthetic_id,
        player_name=player_name,
        position=pos,
        team_id=team_id,
        minutes_last_5=[projected_mins] * 5,
        minutes_last_10=[projected_mins] * 10,
        season_avg=projected_mins,
        usage_rate=0.0,
        dk_salary=dk_salary,
        dk_position=dk_position,
        roster_change_detected=True,
        pts_per_min=pos_prior.get("PTS", 0.0),
        reb_per_min=pos_prior.get("REB", 0.0),
        ast_per_min=pos_prior.get("AST", 0.0),
        stl_per_min=pos_prior.get("STL", 0.0),
        blk_per_min=pos_prior.get("BLK", 0.0),
        tov_per_min=pos_prior.get("TOV", 0.0),
        fg3m_per_min=pos_prior.get("FG3M", 0.0),
    )

    logger.info(
        "[Synthetic] Created %s (team=%d, pos=%s, salary=$%d, "
        "fppg=%.1f → %.1f min)",
        player_name, team_id, pos, dk_salary, dk_fppg, projected_mins,
    )
    return pm


# Module-level cache for league-wide usage rates (USG_PCT).
# Fetched once per session from LeagueDashPlayerBioStats.
_usage_cache: Dict[int, float] = {}  # player_id → USG_PCT (0-100 scale)
_usage_cache_ts: float = 0.0
_USAGE_CACHE_TTL = 7200  # 2 hours


def compute_usage_from_gamelog(game_log: List[Dict], minutes_list: List[float]) -> float:
    """Compute approximate USG% from game-log FGA/FTA/TOV data.

    Uses the simplified formula derived from Basketball-Reference USG%:
        USG% = 100 * ((FGA + 0.44*FTA + TOV) * (Tm_MP/5)) / (MP * Tm_Poss)

    Approximating Tm_Poss ~ 100, Tm_MP = 240:
        USG = actions_per_min * 48 / 100 = actions_per_min * 0.48

    Returns a value in 0.05-0.45 scale (league average ~0.20).
    """
    total_actions = 0.0
    total_min = 0.0
    for g, mp in zip(game_log, minutes_list):
        if mp <= 0:
            continue
        fga = float(g.get("FGA", 0) or 0)
        fta = float(g.get("FTA", 0) or 0)
        tov = float(g.get("TOV", 0) or 0)
        total_actions += fga + 0.44 * fta + tov
        total_min += mp
    if total_min <= 0:
        return 0.20  # League average default
    # team_poss_per_min ~ 100/48 = 2.0833; USG = actions_per_min / team_poss_per_min
    raw = total_actions / total_min * 0.48
    return round(max(0.05, min(0.45, raw)), 4)


# ---------------------------------------------------------------------------
# Circuit breaker — fail-fast after N consecutive NBA API failures
# ---------------------------------------------------------------------------
class _CircuitBreaker:
    """Simple circuit breaker for the NBA API.

    State machine:
        CLOSED  → normal operation, requests pass through
        OPEN    → fail-fast (raise immediately) for ``recovery_s`` seconds
        HALF_OPEN → allow one probe request; success → CLOSED, failure → OPEN

    Thread-safe via a lock on state transitions.
    """

    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_s: float = 30.0,
    ):
        self.failure_threshold = failure_threshold
        self.recovery_s = recovery_s

        self._state = self.CLOSED
        self._consecutive_failures = 0
        self._opened_at: float = 0.0
        self._lock = threading.Lock()

    @property
    def state(self) -> str:
        with self._lock:
            if self._state == self.OPEN:
                # Auto-transition to HALF_OPEN after recovery window
                if time.time() - self._opened_at >= self.recovery_s:
                    self._state = self.HALF_OPEN
            return self._state

    def record_success(self):
        with self._lock:
            self._consecutive_failures = 0
            self._state = self.CLOSED

    def record_failure(self):
        with self._lock:
            self._consecutive_failures += 1
            if self._consecutive_failures >= self.failure_threshold:
                if self._state != self.OPEN:
                    logger.warning(
                        f"[CircuitBreaker] OPEN — {self._consecutive_failures} "
                        f"consecutive NBA API failures. Failing fast for "
                        f"{self.recovery_s}s."
                    )
                self._state = self.OPEN
                self._opened_at = time.time()

    def allow_request(self) -> bool:
        """Return True if the request should proceed."""
        return self.state != self.OPEN

    def get_diagnostics(self) -> dict:
        """Read-only snapshot of circuit breaker state for diagnostics."""
        with self._lock:
            # Inline state check (can't call self.state — it also acquires _lock)
            if self._state == self.OPEN:
                if time.time() - self._opened_at >= self.recovery_s:
                    self._state = self.HALF_OPEN
            _current = self._state
            return {
                "state": _current,
                "consecutive_failures": self._consecutive_failures,
                "failure_threshold": self.failure_threshold,
                "recovery_seconds": self.recovery_s,
                "opened_at": self._opened_at if _current != self.CLOSED else None,
                "seconds_since_open": (
                    round(time.time() - self._opened_at, 1)
                    if self._opened_at and _current != self.CLOSED
                    else None
                ),
            }

    def reset(self):
        """Force-reset circuit breaker to CLOSED state."""
        with self._lock:
            self._state = self.CLOSED
            self._consecutive_failures = 0
            self._opened_at = 0.0
            logger.info("[CircuitBreaker] Force-reset to CLOSED")


# Module-level singleton — shared across all NBAApiService instances.
_circuit_breaker = _CircuitBreaker(failure_threshold=5, recovery_s=30.0)


def get_circuit_breaker_diagnostics() -> dict:
    """Return diagnostic snapshot of the NBA API circuit breaker."""
    return _circuit_breaker.get_diagnostics()


def reset_circuit_breaker():
    """Force-reset the NBA API circuit breaker to CLOSED."""
    _circuit_breaker.reset()


def probe_nba_api(timeout_s: float = 3.0) -> bool:
    """Quick connectivity probe to stats.nba.com.

    Returns True if the API responded within ``timeout_s`` seconds.
    If it fails, pre-trips the circuit breaker so subsequent calls
    fail fast instead of waiting for full timeouts.
    """
    if _circuit_breaker.state == _CircuitBreaker.OPEN:
        return False  # Already tripped
    try:
        import httpx
        r = httpx.head(
            "https://stats.nba.com/stats/scoreboardv2",
            timeout=timeout_s,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        if r.status_code < 500:
            return True
    except Exception:
        pass
    # Pre-trip the circuit breaker
    logger.warning(
        f"[NBAApi] Connectivity probe FAILED (timeout={timeout_s}s) — "
        f"pre-tripping circuit breaker to avoid cascading timeouts"
    )
    for _ in range(_circuit_breaker.failure_threshold):
        _circuit_breaker.record_failure()
    return False


def clear_rotation_cache() -> int:
    """Clear the rotation cache and return number of evicted entries."""
    count = len(_rotation_cache)
    _rotation_cache.clear()
    logger.info(f"[RotationCache] Cleared {count} entries")
    return count


class NBAApiService(SportDataService):
    """Service for fetching NBA data with rate-limiting and retry logic."""

    # Max concurrent NBA API requests per team.  stats.nba.com is
    # aggressive with rate-limiting — too many concurrent requests
    # causes 30s read timeouts.  4 workers per team lets us overlap
    # request/response cycles while the 400ms rate limiter staggers
    # actual sends.
    _MAX_WORKERS = 4

    def __init__(self):
        self._last_request_time = 0.0
        self._min_request_interval = 0.4  # 400ms between requests — tighter but safe with circuit breaker
        self._lock = threading.Lock()  # protects _last_request_time
        self._db_cache = None  # Optional NBADataCacheService for DB-cached reads

    def _get_usage_rates(self, season: str = None) -> Dict[int, float]:
        """Get league-wide USG_PCT map from cache or API.

        Returns dict mapping player_id → USG_PCT (0.0 to ~0.40 scale).
        Uses the LeagueDashPlayerBioStats endpoint which returns USG_PCT
        for all active players in a single API call.

        When a DB cache (``_db_cache``) is available, reads from PostgreSQL
        first before falling back to the live NBA API.
        """
        global _usage_cache, _usage_cache_ts
        now = time.time()
        if _usage_cache and (now - _usage_cache_ts) < _USAGE_CACHE_TTL:
            return _usage_cache

        season = season or get_current_nba_season()

        # ── Try DB cache first ────────────────────────────────────
        if self._db_cache is not None:
            try:
                db_usg = self._db_cache.get_usage_rates_sync(season)
                if db_usg:
                    _usage_cache = db_usg
                    _usage_cache_ts = now
                    logger.info(
                        f"[USG] DB cache hit: {len(db_usg)} player usage rates"
                    )
                    return db_usg
            except Exception as e:
                logger.warning(f"[USG] DB cache read failed: {e}")

        try:
            # Fail fast when the NBA API is known to be down — avoids
            # blocking for ~90 s (3 retries × 30 s timeout) during pool
            # builds when stats.nba.com is unreachable.
            if not _circuit_breaker.allow_request():
                logger.debug("[USG] Circuit breaker OPEN — skipping API call")
                return _usage_cache or {}

            def _fetch():
                bio = leaguedashplayerbiostats.LeagueDashPlayerBioStats(
                    season=season,
                    per_mode_simple="PerGame",
                    timeout=settings.nba_api_timeout,
                )
                return bio.get_normalized_dict()["LeagueDashPlayerBioStats"]

            rows = self._retry_request(_fetch)
            usg_map: Dict[int, float] = {}
            for row in rows:
                pid = row.get("PLAYER_ID", 0)
                usg = row.get("USG_PCT", 0)
                if pid and usg:
                    try:
                        # USG_PCT from this endpoint is 0-100 scale (e.g. 28.5)
                        # Convert to 0-1 scale (0.285) for consistency with our usage
                        usg_val = float(usg)
                        if usg_val > 1.0:
                            usg_val = usg_val / 100.0
                        usg_map[int(pid)] = round(usg_val, 4)
                    except (ValueError, TypeError):
                        continue

            if usg_map:
                _usage_cache = usg_map
                _usage_cache_ts = now
                logger.info(f"[USG] Fetched usage rates for {len(usg_map)} players")
            return usg_map

        except Exception as e:
            logger.warning(f"[USG] Failed to fetch usage rates: {e}")
            return _usage_cache  # Return stale cache if available

    def _rate_limit(self):
        """Enforce rate limiting between API calls (thread-safe)."""
        with self._lock:
            elapsed = time.time() - self._last_request_time
            if elapsed < self._min_request_interval:
                wait = self._min_request_interval - elapsed
                # Release lock while sleeping so other threads can
                # queue up, then re-acquire to stamp the time.
                self._last_request_time = time.time() + wait
            else:
                self._last_request_time = time.time()
                wait = 0.0
        if wait > 0:
            time.sleep(wait)

    def _retry_request(self, func, *args, **kwargs) -> Any:
        """Retry a request with exponential backoff and circuit breaker.

        If the circuit breaker is OPEN (too many consecutive failures),
        raises immediately without hitting the NBA API.
        """
        if not _circuit_breaker.allow_request():
            raise RuntimeError(
                "NBA API circuit breaker is OPEN — too many consecutive "
                "failures.  Failing fast to avoid stalling the pipeline."
            )

        for attempt in range(settings.nba_api_max_retries):
            try:
                self._rate_limit()
                result = func(*args, **kwargs)
                _circuit_breaker.record_success()
                return result
            except Exception as e:
                _circuit_breaker.record_failure()
                wait_time = settings.nba_api_retry_delay * (2**attempt)
                logger.warning(
                    f"NBA API request failed (attempt {attempt + 1}/"
                    f"{settings.nba_api_max_retries}): {e}. "
                    f"Retrying in {wait_time}s..."
                )
                if attempt < settings.nba_api_max_retries - 1:
                    time.sleep(wait_time)
                else:
                    raise

    @staticmethod
    def get_all_teams() -> List[Dict[str, Any]]:
        """Return all NBA teams."""
        return nba_teams.get_teams()

    @staticmethod
    def _team_id_to_abbreviation(team_id: int) -> Optional[str]:
        """Map an NBA team ID to its 3-letter abbreviation.

        Used to filter game logs for traded players (drop pre-trade games).
        Returns None if the team ID is unknown (shouldn't happen for real
        NBA teams but gracefully degrades to no filtering).
        """
        for team in nba_teams.get_teams():
            if team["id"] == team_id:
                return team["abbreviation"]
        return None

    @staticmethod
    def find_team_by_name(team_name: str) -> Optional[Dict[str, Any]]:
        """Find a team by name or abbreviation."""
        all_teams = nba_teams.get_teams()
        team_name_lower = team_name.lower()
        for team in all_teams:
            if (
                team_name_lower in team["full_name"].lower()
                or team_name_lower == team["abbreviation"].lower()
                or team_name_lower in team["nickname"].lower()
            ):
                return team
        return None

    def get_team_roster(self, team_id: int, season: str = None) -> List[Dict]:
        """Fetch team roster for a given season."""
        season = season or get_current_nba_season()

        def _fetch():
            roster = commonteamroster.CommonTeamRoster(
                team_id=team_id,
                season=season,
                timeout=settings.nba_api_timeout,
            )
            return roster.get_normalized_dict()["CommonTeamRoster"]

        return self._retry_request(_fetch)

    def get_player_game_log(
        self, player_id: int, season: str = None, last_n: int = 10
    ) -> List[Dict]:
        """Fetch player game log for recent games."""
        season = season or get_current_nba_season()

        def _fetch():
            game_log = playergamelog.PlayerGameLog(
                player_id=player_id,
                season=season,
                timeout=settings.nba_api_timeout,
            )
            games = game_log.get_normalized_dict()["PlayerGameLog"]
            return games[:last_n]

        return self._retry_request(_fetch)

    def get_today_scoreboard(self) -> List[Dict]:
        """Fetch today's games from the scoreboard."""

        def _fetch():
            scoreboard = scoreboardv2.ScoreboardV2(
                timeout=settings.nba_api_timeout,
            )
            return scoreboard.get_normalized_dict()["GameHeader"]

        return self._retry_request(_fetch)

    @staticmethod
    def _parse_minutes(raw_min) -> float:
        """Safely parse the MIN field from NBA API.

        The field is usually an int (total minutes with seconds
        truncated), but some endpoints return "MM:SS" strings or
        decimal floats.  Handle all variants.
        """
        if raw_min is None:
            return 0.0
        if isinstance(raw_min, (int, float)):
            return float(raw_min)
        raw_str = str(raw_min).strip()
        if ":" in raw_str:
            parts = raw_str.split(":")
            try:
                return float(parts[0]) + float(parts[1]) / 60
            except (ValueError, IndexError):
                return 0.0
        try:
            return float(raw_str)
        except ValueError:
            return 0.0

    def build_player_minutes(
        self,
        player_id: int,
        player_name: str,
        position: str,
        team_id: int,
        birth_date: Optional[str] = None,
    ) -> Optional[PlayerMinutes]:
        """Build a PlayerMinutes object from API data.

        Fetches the **full** season game log, then delegates to
        ``_build_player_minutes_from_gamelog`` for the computation.
        Used for single-player lookups; the team rotation builder
        uses parallel fetching instead.
        """
        try:
            full_games = self.get_player_game_log(player_id, last_n=82)
            return self._build_player_minutes_from_gamelog(
                player_id=player_id,
                player_name=player_name,
                position=position,
                team_id=team_id,
                birth_date=birth_date,
                game_log=full_games,
            )
        except Exception as e:
            logger.error(f"Failed to build minutes for {player_name}: {e}")
            return None

    @staticmethod
    def find_rotation_cutoff(
        all_players: List[PlayerMinutes],
        min_rotation: int = 8,
        max_rotation: int = 13,
        default_rotation: int = 10,
        min_abs_gap: float = 2.0,
        min_gap_ratio: float = 0.20,
    ) -> tuple:
        """Find the natural rotation cutoff using relative gap analysis.

        Scans positions ``min_rotation`` through ``max_rotation`` for the
        largest *relative* drop in season_avg between consecutive players.
        A relative gap (``drop / current_avg``) correctly identifies tier
        boundaries: a 3-min drop from 8 → 5 is a 37.5% cliff, while
        30 → 27 is only 10% (noise).

        Both an absolute floor (``min_abs_gap``) and a relative threshold
        (``min_gap_ratio``) must be met to declare a meaningful cutoff.

        Args:
            all_players: Sorted descending by season_avg.
            min_rotation: Minimum players to keep.
            max_rotation: Maximum players to scan up to.
            default_rotation: Fallback if no meaningful gap is found.
            min_abs_gap: Minimum absolute gap in minutes.
            min_gap_ratio: Minimum gap as a fraction of the higher player.

        Returns:
            (cut_index, best_gap, best_gap_ratio) — cut_index is the
            number of players to include.
        """
        if len(all_players) <= min_rotation:
            return len(all_players), 0.0, 0.0

        best_cut = min(default_rotation, len(all_players))
        best_gap = 0.0
        best_gap_ratio = 0.0

        for i in range(min_rotation - 1, min(max_rotation, len(all_players))):
            if i + 1 < len(all_players):
                gap = all_players[i].season_avg - all_players[i + 1].season_avg
                denominator = all_players[i].season_avg
                gap_ratio = gap / denominator if denominator > 0 else 0.0

                if gap_ratio > best_gap_ratio:
                    best_gap_ratio = gap_ratio
                    best_gap = gap
                    best_cut = i + 1  # keep players 0..i

        # Require BOTH absolute and relative significance
        if best_gap < min_abs_gap or best_gap_ratio < min_gap_ratio:
            best_cut = min(default_rotation, len(all_players))
            best_gap = 0.0
            best_gap_ratio = 0.0

        return best_cut, best_gap, best_gap_ratio

    def _build_player_minutes_from_gamelog(
        self,
        player_id: int,
        player_name: str,
        position: str,
        team_id: int,
        birth_date: Optional[str],
        game_log: List[Dict],
    ) -> Optional[PlayerMinutes]:
        """Build a PlayerMinutes from a pre-fetched game log.

        This is the pure-computation half of ``build_player_minutes``;
        the NBA API call has already been made by the caller so that
        multiple players can be fetched in parallel.
        """
        try:
            if not game_log:
                logger.warning(f"No game data for player {player_name}")
                return None

            # ── Filter for current-team games only ─────────────────
            # Mid-season trades cause game logs to span two teams.
            # If we include pre-trade games, baselines/stat-rates are
            # anchored to the old team's system, which may be very
            # different (e.g. a player going from a bench role to a
            # starting role on a new team, or vice-versa).
            #
            # The NBA API's PlayerGameLog includes a "MATCHUP" field
            # like "PHI vs. ATL" or "PHI @ BOS", so we can detect
            # which team the player was on for each game.
            _team_abbr = self._team_id_to_abbreviation(team_id)
            if _team_abbr:
                pre_trade_count = 0
                filtered_log = []
                for g in game_log:
                    matchup = g.get("MATCHUP", "")
                    # MATCHUP format: "PHI vs. ATL" or "PHI @ BOS"
                    # The first 3 chars (before space) are the player's team
                    game_team = matchup.split(" ")[0].strip() if matchup else ""
                    if game_team.upper() == _team_abbr.upper():
                        filtered_log.append(g)
                    else:
                        pre_trade_count += 1

                if pre_trade_count > 0 and filtered_log:
                    logger.info(
                        f"Trade filter: {player_name} — dropped "
                        f"{pre_trade_count} pre-trade game(s), "
                        f"keeping {len(filtered_log)} on {_team_abbr}"
                    )
                    game_log = filtered_log
                elif pre_trade_count > 0 and not filtered_log:
                    # Just traded, 0 games on new team yet.
                    # Old-team minutes are misleading (e.g. 4 min bench role
                    # on old team ≠ expected role on new team).  Use a
                    # position-based default for minutes, but keep old-team
                    # data for per-minute stat rates (production transfers).
                    default_min = _TRADE_DEFAULT_MINUTES.get(position, 22.0)
                    logger.info(
                        f"Trade filter: {player_name} — ALL "
                        f"{pre_trade_count} games from old team, "
                        f"0 on {_team_abbr} — using {default_min} min "
                        f"position default (per-min rates from old data)"
                    )

                    # Compute per-minute stat rates from old-team data
                    # (useful — production rate transfers across teams)
                    old_played = [
                        g for g in game_log
                        if self._parse_minutes(g.get("MIN", 0)) > 0
                    ]
                    old_mins = [
                        self._parse_minutes(g.get("MIN", 0))
                        for g in old_played
                    ]
                    stat_fields = [
                        "PTS", "REB", "AST", "STL", "BLK", "TOV", "FG3M",
                    ]
                    total_old_min = sum(old_mins)
                    if total_old_min > 0:
                        rates = {
                            f: sum(
                                float(g.get(f, 0) or 0) for g in old_played
                            ) / total_old_min
                            for f in stat_fields
                        }
                    else:
                        pos_prior = _POSITION_PRIOR_RATES.get(
                            position,
                            _POSITION_PRIOR_RATES.get("SF", {}),
                        )
                        rates = {f: pos_prior.get(f, 0.0) for f in stat_fields}

                    # USG_PCT computed from game-log data (no API call needed)
                    _old_mins = [self._parse_minutes(g.get("MIN", 0)) for g in old_played]
                    usage_rate = compute_usage_from_gamelog(old_played, _old_mins)

                    # Shooting profile from old-team data
                    if total_old_min > 0:
                        _t_fga = sum(float(g.get("FGA", 0) or 0) for g in old_played)
                        _t_fta = sum(float(g.get("FTA", 0) or 0) for g in old_played)
                        _t_fg3a = sum(float(g.get("FG3A", 0) or 0) for g in old_played)
                        _t_fgm = sum(float(g.get("FGM", 0) or 0) for g in old_played)
                        _t_fg3m = sum(float(g.get("FG3M", 0) or 0) for g in old_played)
                        _t_ftm = sum(float(g.get("FTM", 0) or 0) for g in old_played)
                        _t_fga_rate = _t_fga / total_old_min
                        _t_fta_rate = _t_fta / total_old_min
                        _t_fg3a_rate = _t_fg3a / total_old_min
                        _t_fg2a = _t_fga - _t_fg3a
                        _t_fg2m = _t_fgm - _t_fg3m
                        _t_fg2_pct = _t_fg2m / _t_fg2a if _t_fg2a > 0 else None
                        _t_fg3_pct = _t_fg3m / _t_fg3a if _t_fg3a > 0 else None
                        _t_ft_pct = _t_ftm / _t_fta if _t_fta > 0 else None
                    else:
                        _t_fga_rate = _t_fta_rate = _t_fg3a_rate = None
                        _t_fg2_pct = _t_fg3_pct = _t_ft_pct = None

                    # Age calculation
                    player_age = None
                    if birth_date:
                        try:
                            bd_str = str(birth_date).strip()
                            if "T" in bd_str:
                                bd_str = bd_str.split("T")[0]
                            for fmt in ("%b %d, %Y", "%Y-%m-%d", "%m/%d/%Y"):
                                try:
                                    bd = datetime.strptime(bd_str, fmt).date()
                                    today = date.today()
                                    player_age = (
                                        today.year - bd.year
                                        - ((today.month, today.day) < (bd.month, bd.day))
                                    )
                                    break
                                except ValueError:
                                    continue
                        except Exception:
                            player_age = None

                    return PlayerMinutes(
                        player_id=player_id,
                        player_name=player_name,
                        position=position,
                        team_id=team_id,
                        minutes_last_5=[default_min] * 5,
                        minutes_last_10=[default_min] * 10,
                        season_avg=default_min,
                        usage_rate=round(usage_rate, 3),
                        age=player_age,
                        recent_dnp_streak=0,
                        roster_change_detected=True,
                        pts_per_min=round(rates["PTS"], 4),
                        reb_per_min=round(rates["REB"], 4),
                        ast_per_min=round(rates["AST"], 4),
                        stl_per_min=round(rates["STL"], 4),
                        blk_per_min=round(rates["BLK"], 4),
                        tov_per_min=round(rates["TOV"], 4),
                        fg3m_per_min=round(rates["FG3M"], 4),
                        fg3a_rate=round(_t_fg3a_rate, 4) if _t_fg3a_rate is not None else None,
                        fga_rate=round(_t_fga_rate, 4) if _t_fga_rate is not None else None,
                        fta_rate=round(_t_fta_rate, 4) if _t_fta_rate is not None else None,
                        fg3_pct=round(_t_fg3_pct, 4) if _t_fg3_pct is not None else None,
                        fg2_pct=round(_t_fg2_pct, 4) if _t_fg2_pct is not None else None,
                        ft_pct=round(_t_ft_pct, 4) if _t_ft_pct is not None else None,
                    )

            all_minutes_raw = [
                self._parse_minutes(g.get("MIN", 0))
                for g in game_log
            ]

            # Filter out DNP / 0-minute games from the season average.
            # A player who missed 5 games for DNP-CD should not have those
            # zeroes drag down their per-game average.  We keep the raw
            # list intact for normalization indexing, but compute season_avg
            # only from games where the player actually played.
            played_minutes = [m for m in all_minutes_raw if m > 0]
            season_avg = sum(played_minutes) / len(played_minutes) if played_minutes else 0.0

            # Count consecutive recent DNPs (0-minute games) from most recent.
            # Game log is reverse-chronological (index 0 = most recent).
            # This detects suspended/inactive players whose season_avg is
            # still high but who haven't played in weeks.
            _dnp_streak = 0
            for _rm in all_minutes_raw:
                if _rm <= 0:
                    _dnp_streak += 1
                else:
                    break

            # Use raw list for normalization (needs index alignment with game_log)
            all_minutes = all_minutes_raw

            # --- Context-aware normalization (blowout / OT) ---
            # Threshold raised from 0.75 → 0.85 to catch "soft blowout"
            # games where a star plays 30 min instead of 34 because of a
            # 15-point lead.  At 0.75, those games slip through and drag
            # down the EMA, systematically under-projecting top starters
            # on elite teams (e.g. Jokic, Giannis) who are frequently in
            # blowout-win situations.
            BLOWOUT_THRESHOLD = 0.85
            OT_REGULATION_MIN = 48.0
            MIN_GAMES_FOR_NORM = 5
            # Foul trouble / early exit threshold:  If a player logs
            # less than X% of their season average, the game is almost
            # certainly an anomaly (foul trouble, ejection, in-game
            # injury, or a coach decision to pull them early).  One
            # such game can destroy the EMA (alpha=0.6 puts 60% weight
            # on the most recent game), dropping a 27-min player to
            # 13 min EMA.  Normalize these extreme outliers to the
            # season average so a single bad game doesn't wreck the
            # projection.
            #
            # ADAPTIVE threshold:
            #   Starters (≥25 min avg): 0.45 — a 27-min player at
            #     <12.3 min, or a 30-min player at <13.5 min.  Starters
            #     playing <45% of normal is clearly anomalous (foul
            #     trouble, ejection, minor tweak).
            #   Bench (<25 min avg): 0.33 — a 15-min player at <5 min.
            #     Bench players legitimately have higher game-to-game
            #     minute variance, so only flag truly extreme outliers.
            #     Using 0.45 for bench players inflates their baselines
            #     by normalizing legitimate short-minute games.
            FOUL_TROUBLE_THRESHOLD_STARTER = 0.45
            FOUL_TROUBLE_THRESHOLD_BENCH = 0.33
            _STARTER_MIN_THRESHOLD = 25.0

            normalized_minutes = []
            played_game_indices = []  # indices into game_log for games with >0 min
            for i, raw_min in enumerate(all_minutes):
                if raw_min <= 0:
                    continue  # Skip DNP / inactive games entirely
                adj = raw_min
                if season_avg > 0 and len(played_minutes) >= MIN_GAMES_FOR_NORM:
                    if raw_min > OT_REGULATION_MIN:
                        adj = max(season_avg, OT_REGULATION_MIN)
                    elif raw_min < season_avg * (
                        FOUL_TROUBLE_THRESHOLD_STARTER
                        if season_avg >= _STARTER_MIN_THRESHOLD
                        else FOUL_TROUBLE_THRESHOLD_BENCH
                    ):
                        # Extreme outlier — foul trouble, ejection, or
                        # in-game injury.  Always normalize regardless
                        # of blowout signals.
                        adj = season_avg
                    elif raw_min < season_avg * BLOWOUT_THRESHOLD:
                        plus_minus = abs(float(game_log[i].get("PLUS_MINUS", 0) or 0))
                        pts_in_game = float(game_log[i].get("PTS", 0) or 0)
                        per_min_pts = pts_in_game / raw_min if raw_min > 0 else 0
                        if plus_minus >= 15 or per_min_pts >= season_avg * 0.022:
                            adj = season_avg
                normalized_minutes.append(round(adj, 1))
                played_game_indices.append(i)

            minutes_last_5 = normalized_minutes[:5] if len(normalized_minutes) >= 5 else normalized_minutes
            minutes_last_10 = normalized_minutes[:10] if len(normalized_minutes) >= 10 else normalized_minutes

            # Build a filtered game log aligned with normalized_minutes (DNPs excluded)
            played_game_log = [game_log[i] for i in played_game_indices]
            played_all_minutes = [all_minutes[i] for i in played_game_indices]

            # USG_PCT computed from game-log data (no API call needed)
            usage_rate = compute_usage_from_gamelog(played_game_log, played_all_minutes)

            # --- Per-minute stat rates (DNP-filtered, recency-weighted) ---
            stat_fields = ["PTS", "REB", "AST", "STL", "BLK", "TOV", "FG3M"]

            def _compute_rates(games_slice, mins_slice):
                total_m = sum(mins_slice)
                if total_m <= 0:
                    return {f: 0.0 for f in stat_fields}
                return {f: sum(float(g.get(f, 0) or 0) for g in games_slice) / total_m for f in stat_fields}

            def _compute_recency_weighted_rates(games_slice, mins_slice, decay=0.95):
                """Exponentially weighted per-minute rates (recent games weighted more).

                Input is reverse-chronological (index 0 = most recent).
                Weight for game i = decay^i, so most recent gets weight 1.0,
                second most recent gets 0.95, third gets 0.9025, etc.
                """
                if not games_slice or not mins_slice:
                    return {f: 0.0 for f in stat_fields}
                n = len(games_slice)
                weights = [decay ** i for i in range(n)]
                total_weighted_min = sum(w * m for w, m in zip(weights, mins_slice))
                if total_weighted_min <= 0:
                    return {f: 0.0 for f in stat_fields}
                return {
                    f: sum(
                        w * float(g.get(f, 0) or 0)
                        for w, g in zip(weights, games_slice)
                    ) / total_weighted_min
                    for f in stat_fields
                }

            season_rates = _compute_rates(played_game_log, played_all_minutes)
            # Recency-weighted rates give exponentially more importance to recent games
            recent_rates = _compute_recency_weighted_rates(
                played_game_log, played_all_minutes, decay=0.95
            )

            SEASON_WEIGHT, RECENT_WEIGHT = 0.70, 0.30
            if len(played_game_log) >= 10:
                blended = {f: SEASON_WEIGHT * season_rates[f] + RECENT_WEIGHT * recent_rates[f] for f in stat_fields}
            else:
                blended = season_rates

            # --- Small-sample regression to position-average priors ---
            # Players with fewer than 15 games have noisy stats.
            # Blend with league-average per-minute rates for their position
            # using Bayesian shrinkage: effective = (n*player + k*prior) / (n+k)
            REGRESSION_K = 10  # Strength of prior (effective sample size)
            REGRESSION_THRESHOLD = 15  # Games below which we regress
            n_played = len(played_game_log)
            if n_played < REGRESSION_THRESHOLD:
                pos_prior = _POSITION_PRIOR_RATES.get(
                    position, _POSITION_PRIOR_RATES.get("SF", {})  # fallback
                )
                shrinkage = n_played / (n_played + REGRESSION_K)
                for f in stat_fields:
                    prior_rate = pos_prior.get(f, 0.0)
                    blended[f] = shrinkage * blended[f] + (1 - shrinkage) * prior_rate

            # --- Shooting profile (enables decomposed DFS projection) ---
            # Compute per-minute shot attempt rates and shooting percentages
            # from the played game log (DNP-filtered, same games as per-min
            # rates).  When populated, DFS projection uses the more accurate
            # _project_stats_decomposed() which correctly models the DK 3PM
            # bonus, shot-type DvP adjustments, and FT volume.
            _total_min = sum(played_all_minutes)
            _total_fga = sum(float(g.get("FGA", 0) or 0) for g in played_game_log)
            _total_fta = sum(float(g.get("FTA", 0) or 0) for g in played_game_log)
            _total_fg3a = sum(float(g.get("FG3A", 0) or 0) for g in played_game_log)
            _total_fgm = sum(float(g.get("FGM", 0) or 0) for g in played_game_log)
            _total_fg3m = sum(float(g.get("FG3M", 0) or 0) for g in played_game_log)
            _total_ftm = sum(float(g.get("FTM", 0) or 0) for g in played_game_log)

            # Per-minute attempt rates
            _fga_rate = _total_fga / _total_min if _total_min > 0 else None
            _fta_rate = _total_fta / _total_min if _total_min > 0 else None
            _fg3a_rate = _total_fg3a / _total_min if _total_min > 0 else None

            # Shooting percentages
            _fg2a = _total_fga - _total_fg3a
            _fg2m = _total_fgm - _total_fg3m
            _fg2_pct = _fg2m / _fg2a if _fg2a > 0 else None
            _fg3_pct = _total_fg3m / _total_fg3a if _total_fg3a > 0 else None
            _ft_pct = _total_ftm / _total_fta if _total_fta > 0 else None

            # --- Player age ---
            player_age = None
            if birth_date:
                try:
                    bd_str = str(birth_date).strip()
                    if "T" in bd_str:
                        bd_str = bd_str.split("T")[0]
                    for fmt in ("%b %d, %Y", "%Y-%m-%d", "%m/%d/%Y"):
                        try:
                            bd = datetime.strptime(bd_str, fmt).date()
                            today = date.today()
                            player_age = today.year - bd.year - ((today.month, today.day) < (bd.month, bd.day))
                            break
                        except ValueError:
                            continue
                except Exception:
                    player_age = None

            return PlayerMinutes(
                player_id=player_id,
                player_name=player_name,
                position=position,
                team_id=team_id,
                minutes_last_5=minutes_last_5,
                minutes_last_10=minutes_last_10,
                season_avg=round(season_avg, 1),
                usage_rate=round(usage_rate, 3),
                age=player_age,
                recent_dnp_streak=_dnp_streak,
                pts_per_min=round(blended["PTS"], 4),
                reb_per_min=round(blended["REB"], 4),
                ast_per_min=round(blended["AST"], 4),
                stl_per_min=round(blended["STL"], 4),
                blk_per_min=round(blended["BLK"], 4),
                tov_per_min=round(blended["TOV"], 4),
                fg3m_per_min=round(blended["FG3M"], 4),
                fg3a_rate=round(_fg3a_rate, 4) if _fg3a_rate is not None else None,
                fga_rate=round(_fga_rate, 4) if _fga_rate is not None else None,
                fta_rate=round(_fta_rate, 4) if _fta_rate is not None else None,
                fg3_pct=round(_fg3_pct, 4) if _fg3_pct is not None else None,
                fg2_pct=round(_fg2_pct, 4) if _fg2_pct is not None else None,
                ft_pct=round(_ft_pct, 4) if _ft_pct is not None else None,
            )
        except Exception as e:
            logger.error(f"Failed to build minutes for {player_name}: {e}")
            return None

    def build_team_rotation(
        self,
        team_id: int,
        season: str = None,
        max_players: int = 0,
        draftable_names: Optional[Set[str]] = None,
        cache_service=None,
        prefetched_roster=None,
        prefetched_game_logs=None,
        draftable_positions: Optional[Dict[str, str]] = None,
        draftable_salaries: Optional[Dict[str, int]] = None,
        draftable_statuses: Optional[Dict[str, str]] = None,
    ) -> List[PlayerMinutes]:
        """Build game-night rotation for a team.

        The goal is to identify the 8-13 players who would actually
        see the floor in a competitive game.  The approach:

        1. Gather everyone on the roster with > 0 season avg.
        2. Sort by season average (descending).
        3. Find the natural "rotation cutoff" — the biggest *relative*
           gap in minutes between consecutive players, scanning
           positions 8 through 13.  A relative gap (drop / current avg)
           correctly identifies tier boundaries: a 3-min drop from 8
           to 5 is a 37.5% cliff, while 30 to 27 is noise.
        4. Require both an absolute floor (2 min) and a relative
           threshold (20%) to declare a meaningful gap.
        5. If no meaningful gap is found, default to 10 players.
        6. Callers can override the cap via max_players (0 = auto).

        Results are cached for 30 minutes so consecutive page loads
        are instant.  Player game logs are fetched in parallel (4
        concurrent threads) to cut first-load time from ~10s to ~3s.

        When a ``cache_service`` (NBADataCacheService) is provided and
        has fresh data, game logs and rosters are read from PostgreSQL
        instead of making live NBA API calls (~140 calls → 0).

        Args:
            team_id: NBA team ID.
            season: Season string (e.g. "2025-26"). Auto-detected if None.
            max_players: Override the maximum rotation size. 0 = auto (13).
            draftable_names: If provided, only fetch game logs for roster
                players whose name appears in this set (case-insensitive
                substring match).  Deep-bench players not in the DK pool
                are skipped, which can cut API calls by 30-40%.
            cache_service: Optional NBADataCacheService for DB-cached reads.
            draftable_salaries: DK name → salary lookup for trade detection
                minute scaling.  Minimum-salary players get reduced minutes.
            draftable_positions: Optional mapping of DK display_name → DK
                position (e.g. "PG", "SF/PF").  Used by trade detection
                to assign a position when a DK-listed player isn't on the
                team's NBA API roster (recently traded).
            draftable_statuses: Optional mapping of DK display_name → DK
                injury status (e.g. "O", "Q", "GTD").  Used by trade
                detection to skip players who are OUT/Doubtful — they
                should not consume the team's 240-min budget.
        """
        # --- Check rotation cache ---
        now = time.time()
        if team_id in _rotation_cache:
            cached_at, cached_rotation = _rotation_cache[team_id]
            if now - cached_at < _ROTATION_CACHE_TTL:
                # When draftable_names is provided, verify the cached
                # rotation covers them.  The pre-warm builds rotations
                # WITHOUT draftable_names (no trade detection), so the
                # cache may have only 8-10 core players.  The pool
                # builder needs ALL DK-listed players in the rotation
                # so they can match by name.  If the cache is missing
                # DK players, skip it and rebuild with trade detection.
                if draftable_names:
                    _cached_names = {
                        normalize_player_name(p.player_name)
                        for p in cached_rotation
                    }
                    _missing = [
                        n for n in draftable_names
                        if not any(
                            normalize_player_name(n) in cn
                            or cn in normalize_player_name(n)
                            for cn in _cached_names
                        )
                    ]
                    if _missing:
                        logger.info(
                            f"Rotation cache SKIP for team {team_id}: "
                            f"{len(_missing)} DK name(s) not in cached "
                            f"rotation ({len(cached_rotation)} players) — "
                            f"rebuilding with trade detection. "
                            f"Missing: {sorted(_missing)[:5]}"
                        )
                    else:
                        logger.info(
                            f"Rotation cache hit for team {team_id} "
                            f"({len(cached_rotation)} players, "
                            f"age={now - cached_at:.0f}s)"
                        )
                        return cached_rotation
                else:
                    logger.info(
                        f"Rotation cache hit for team {team_id} "
                        f"({len(cached_rotation)} players, "
                        f"age={now - cached_at:.0f}s)"
                    )
                    return cached_rotation

        season = season or get_current_nba_season()

        # ── Try DB cache first (instant reads, no NBA API calls) ──
        _used_db_cache = False
        # Use pre-fetched data (from async endpoint) or fall back to sync wrappers
        _prefetched_roster = prefetched_roster
        _prefetched_logs = prefetched_game_logs
        if _prefetched_roster and _prefetched_logs:
            cache_service = True  # signal to enter the DB cache path
        if cache_service is not None:
            try:
                roster = _prefetched_roster or (
                    cache_service.get_team_roster_sync(team_id, season)
                    if cache_service is not True else None
                )
                if roster:
                    # Bulk-read all game logs for this team in one query
                    team_logs = _prefetched_logs or (
                        cache_service.get_team_game_logs_sync(team_id, season)
                        if cache_service is not True else None
                    )
                    if team_logs:
                        t_start = time.time()

                        # Filter roster to DK-relevant players
                        # Use normalized names to handle diacritical mismatches
                        # (BDL/DK "Jokic" vs NBA API "Jokić")
                        if draftable_names:
                            norm_dk = {normalize_player_name(n) for n in draftable_names}
                            filtered_roster = []
                            for p in roster:
                                pnorm = normalize_player_name(p.get("PLAYER", ""))
                                if any(dk in pnorm or pnorm in dk for dk in norm_dk):
                                    filtered_roster.append(p)
                            skipped = len(roster) - len(filtered_roster)
                            if skipped > 0:
                                logger.info(
                                    f"Skipping {skipped} deep-bench players for team "
                                    f"{team_id} (not in DK pool) [DB cache]"
                                )
                            roster = filtered_roster

                        # Build PlayerMinutes from cached game logs
                        all_players: List[PlayerMinutes] = []
                        _no_log_names: List[str] = []  # Track for synthetic fallback
                        for p in roster:
                            pid = p.get("PLAYER_ID")
                            pname = p.get("PLAYER", "?")
                            game_log = team_logs.get(pid, [])

                            # Resolve position: override → DK → BDL
                            _dk_pos = (
                                draftable_positions.get(pname)
                                if draftable_positions else None
                            )
                            _resolved_pos = _resolve_position(
                                pname,
                                p.get("POSITION", "F"),
                                _dk_pos,
                            )

                            if not game_log:
                                # No game logs — check if DK lists this player
                                # with non-trivial salary.  If so, create a
                                # synthetic entry so they survive into the pool.
                                _dk_sal = (
                                    draftable_salaries.get(pname, 0)
                                    if draftable_salaries else 0
                                )
                                if _dk_sal >= 3000 and draftable_names and (
                                    pname.lower() in {n.lower() for n in draftable_names}
                                    or normalize_player_name(pname) in {
                                        normalize_player_name(n) for n in draftable_names
                                    }
                                ):
                                    pm = build_synthetic_player(
                                        player_name=pname,
                                        team_id=team_id,
                                        dk_salary=_dk_sal,
                                        dk_position=_dk_pos or _resolved_pos,
                                    )
                                    pm.player_id = pid  # Keep the real ID
                                    all_players.append(pm)
                                    logger.info(
                                        "[Rotation] Synthetic fallback for %s "
                                        "(no game logs, DK sal=$%d)",
                                        pname, _dk_sal,
                                    )
                                else:
                                    _no_log_names.append(pname)
                                continue

                            pm = self._build_player_minutes_from_gamelog(
                                player_id=pid,
                                player_name=pname,
                                position=_resolved_pos,
                                team_id=team_id,
                                birth_date=p.get("BIRTH_DATE"),
                                game_log=game_log,
                            )
                            if pm and pm.season_avg > 0:
                                all_players.append(pm)

                        if _no_log_names:
                            logger.debug(
                                "[Rotation] %d roster players skipped (no game "
                                "logs, not in DK pool): %s",
                                len(_no_log_names),
                                ", ".join(_no_log_names[:5]),
                            )

                        logger.info(
                            f"DB cache hit: {len(all_players)}/{len(roster)} "
                            f"players for team {team_id} in "
                            f"{time.time() - t_start:.3f}s (0 API calls)"
                        )
                        # Only mark as successful if we actually got
                        # usable players.  When the DB has BDL roster
                        # IDs but NBA API game-log IDs, zero players
                        # will match — fall through to BDL live instead
                        # of returning an empty rotation.
                        if all_players:
                            _used_db_cache = True
            except Exception as e:
                logger.warning(
                    f"DB cache read failed for team {team_id}: {e}"
                )
                _used_db_cache = False

        # ── Live API path (fallback or no cache) ──────────────────
        # When skip_nba_api_live is True, stats.nba.com is disabled for
        # live requests — return empty rotation so the DK fallback
        # handles projections downstream.
        if not _used_db_cache and settings.skip_nba_api_live:
            logger.info(
                f"[Rotation] Skipping live NBA API for team {team_id} "
                f"(skip_nba_api_live=True, DB cache unavailable). "
                f"Returning empty rotation for DK fallback."
            )
            return []

        if not _used_db_cache:
            roster = self.get_team_roster(team_id, season)

            # --- Filter roster to DK-relevant players ---
            # Use normalized names to handle diacritical mismatches
            if draftable_names:
                norm_dk = {normalize_player_name(n) for n in draftable_names}
                filtered_roster = []
                for p in roster:
                    pnorm = normalize_player_name(p.get("PLAYER", ""))
                    # Keep if any DK name is a substring match or vice-versa
                    if any(dk in pnorm or pnorm in dk for dk in norm_dk):
                        filtered_roster.append(p)
                skipped = len(roster) - len(filtered_roster)
                if skipped > 0:
                    logger.info(
                        f"Skipping {skipped} deep-bench players for team "
                        f"{team_id} (not in DK pool)"
                    )
                roster = filtered_roster

            # --- Parallel game-log fetching ---
            # Each thread fetches one player's full-season game log.
            # The thread-safe rate limiter staggers requests so we
            # don't hammer stats.nba.com.
            def _fetch_one(player: Dict) -> Optional[PlayerMinutes]:
                try:
                    game_log = self.get_player_game_log(
                        player["PLAYER_ID"], season=season, last_n=82
                    )
                    # Resolve position: override → DK → BDL
                    _dk_pos = (
                        draftable_positions.get(player["PLAYER"])
                        if draftable_positions else None
                    )
                    _resolved_pos = _resolve_position(
                        player["PLAYER"],
                        player.get("POSITION", "F"),
                        _dk_pos,
                    )
                    return self._build_player_minutes_from_gamelog(
                        player_id=player["PLAYER_ID"],
                        player_name=player["PLAYER"],
                        position=_resolved_pos,
                        team_id=team_id,
                        birth_date=player.get("BIRTH_DATE"),
                        game_log=game_log,
                    )
                except Exception as e:
                    logger.error(
                        f"Failed to fetch game log for "
                        f"{player.get('PLAYER', '?')}: {e}"
                    )
                    return None

            all_players: List[PlayerMinutes] = []
            t_start = time.time()

            with ThreadPoolExecutor(max_workers=self._MAX_WORKERS) as pool:
                futures = {pool.submit(_fetch_one, p): p for p in roster}
                for future in as_completed(futures):
                    result = future.result()
                    if result and result.season_avg > 0:
                        all_players.append(result)

            logger.info(
                f"Fetched {len(all_players)}/{len(roster)} player game logs "
                f"for team {team_id} in {time.time() - t_start:.1f}s "
                f"({self._MAX_WORKERS} workers)"
            )

        # ── Trade detection: find DK-listed players missing from roster ──
        # When a player is recently traded, the NBA API roster for their
        # new team may not include them yet (stale data / propagation lag).
        # DraftKings is faster — they list the player under the new team
        # on trade day.  Detect the mismatch and individually fetch the
        # missing player's game log so they get a real projection.
        #
        # Guards:
        # - We do NOT gate on the circuit breaker here.  The exception
        #   fallback (below) already creates position-default PlayerMinutes
        #   when the API call fails, so even with stats.nba.com down every
        #   DK-listed player still gets a projection.
        # - When many players are missing we still process the top N by
        #   salary.  BDL can have sparse game logs (e.g. only 2 of 16 CHI
        #   players) which is NOT an API failure — it just means BDL lacks
        #   data for recently traded / new players.
        # NOTE: We no longer require len(all_players) > 0.  When BDL provides
        # the roster but has sparse game logs, the rotation can be empty even
        # though the data pipeline is healthy.
        _MAX_TRADE_DETECTION_FETCH = 15  # Max players to attempt per team
        if draftable_names:
            # Normalize names to handle diacritical mismatches
            rotation_names_norm = {normalize_player_name(pm.player_name) for pm in all_players}
            missing_dk: list = []
            for dk_name in draftable_names:
                dk_norm = normalize_player_name(dk_name)
                # Fuzzy match: DK "Rob Dillingham" vs NBA "Rob Dillingham"
                found = any(
                    dk_norm in rn or rn in dk_norm
                    for rn in rotation_names_norm
                )
                if not found:
                    missing_dk.append(dk_name)

            if missing_dk:
                # Cap the number we process.  The exception fallback
                # creates default projections instantly (no API wait),
                # so this is cheap even when stats.nba.com is down.
                total_missing = len(missing_dk)
                missing_dk = missing_dk[:_MAX_TRADE_DETECTION_FETCH]
                logger.warning(
                    f"Trade detection: {total_missing} DK draftable(s) "
                    f"not found on team {team_id} roster "
                    f"(processing {len(missing_dk)} of {total_missing}): "
                    f"{sorted(missing_dk)}"
                )
                from nba_api.stats.static import players as nba_players_static

                for dk_name in missing_dk:
                    # 0. Skip players whose DK status is OUT or
                    # Doubtful.  These players are on the roster but
                    # definitely not playing tonight.  Adding them with
                    # synthetic minutes would consume the team's 240-min
                    # budget and compress real rotation players (e.g.
                    # Damian Lillard listed on POR as OUT all season
                    # was stealing ~14 min from Clingan/Jrue/etc.).
                    if draftable_statuses:
                        _dk_st_td = (
                            draftable_statuses.get(dk_name, "") or ""
                        ).upper()
                        if _dk_st_td in {"OUT", "O", "D", "DOUBTFUL"}:
                            logger.info(
                                f"Trade detection: Skipping {dk_name} "
                                f"— DK status is '{_dk_st_td}' (not "
                                f"playing)"
                            )
                            continue

                    # 1. Look up NBA player ID via static data
                    matches = nba_players_static.find_players_by_full_name(dk_name)
                    if not matches:
                        # Try last name only (handles "Rob" vs "Robert" etc.)
                        last_name = dk_name.split()[-1] if " " in dk_name else dk_name
                        matches = nba_players_static.find_players_by_last_name(last_name)
                        # Filter to active players whose full name is close
                        # Use normalized names for diacritical-safe matching
                        dk_norm_td = normalize_player_name(dk_name)
                        matches = [
                            m for m in matches
                            if m.get("is_active")
                            and (
                                dk_norm_td in normalize_player_name(m["full_name"])
                                or normalize_player_name(m["full_name"]) in dk_norm_td
                            )
                        ]

                    # Determine position from DK data or default
                    pos = "G-F"  # safe default (eligible for both)
                    if draftable_positions and dk_name in draftable_positions:
                        # Map DK positions (PG, SG, SF, PF, C) to NBA-style
                        dk_pos = draftable_positions[dk_name]
                        # DK uses "PG", "SG", "SF", "PF", "C", "PG/SG", etc.
                        # Take the first position for the NBA format
                        pos = dk_pos.split("/")[0] if "/" in dk_pos else dk_pos

                    if not matches:
                        # ── Unknown player fallback ──────────────────────
                        # NBA static data doesn't have this player (common
                        # for international rookies like Ben Saraf, recent
                        # G-League callups, or players with name mismatches).
                        # Instead of dropping them entirely (which causes
                        # 0% pool inclusion for high-ownership DK players),
                        # create a default entry using DK data.  Empty
                        # minutes_last_5/10 triggers the sparse data
                        # heuristic in the rotation engine, which falls
                        # back to season_avg for baseline minutes.
                        # DK salary-driven trade minutes: smooth curve with
                        # interpolation instead of coarse 3-tier buckets.
                        _dk_sal = (
                            draftable_salaries.get(dk_name, 0)
                            if draftable_salaries else 0
                        )
                        default_min = _salary_adjusted_trade_minutes(pos, _dk_sal)

                        pos_prior = _POSITION_PRIOR_RATES.get(
                            pos, _POSITION_PRIOR_RATES.get("SF", {})
                        )
                        _synthetic_id = hash(dk_name) & 0x7FFFFFFF
                        pm = PlayerMinutes(
                            player_id=_synthetic_id,
                            player_name=dk_name,
                            position=pos,
                            team_id=team_id,
                            minutes_last_5=[],
                            minutes_last_10=[],
                            season_avg=default_min,
                            usage_rate=0.0,
                            roster_change_detected=True,
                            pts_per_min=pos_prior.get("PTS", 0.0),
                            reb_per_min=pos_prior.get("REB", 0.0),
                            ast_per_min=pos_prior.get("AST", 0.0),
                            stl_per_min=pos_prior.get("STL", 0.0),
                            blk_per_min=pos_prior.get("BLK", 0.0),
                            tov_per_min=pos_prior.get("TOV", 0.0),
                            fg3m_per_min=pos_prior.get("FG3M", 0.0),
                        )
                        all_players.append(pm)
                        logger.info(
                            f"Trade detection: Added {dk_name} "
                            f"(synthetic ID {_synthetic_id}) "
                            f"to team {team_id} with {default_min} min "
                            f"(no NBA ID found, using DK data, pos={pos}, "
                            f"salary=${_dk_sal:,})"
                        )
                        continue

                    player_info = matches[0]
                    nba_pid = player_info["id"]

                    # 2. Create position-default PlayerMinutes.
                    #
                    # We intentionally do NOT call get_player_game_log()
                    # here.  stats.nba.com is frequently unreachable and
                    # each call burns ~90 s on retries+timeouts.  BDL
                    # already provided the game-log data for players it
                    # knows about — anyone reaching this point is NOT in
                    # BDL's roster, so we have no fast data source.
                    #
                    # NBA-data-aware scaling: players marked `is_active=False`
                    # in NBA static data are almost always two-way /
                    # G-League players (e.g. Tristen Newton) who average
                    # <5 mpg.  BDL also excludes them from team rosters.
                    # Giving them position-default (22-24 min) would steal
                    # minutes from real rotation players during 240-min
                    # normalization.  We do NOT use salary to cap minutes
                    # because that would prevent identifying value plays
                    # (e.g. a $3,000 player getting real rotation minutes).
                    _is_inactive = not player_info.get("is_active", True)

                    if _is_inactive:
                        # NBA static data says inactive — likely two-way /
                        # G-League call-up.  Give deep-bench minutes.
                        default_min = 5.0
                    else:
                        # DK salary-driven trade minutes: smooth curve with
                        # interpolation instead of coarse 3-tier buckets.
                        _dk_sal = (
                            draftable_salaries.get(dk_name, 0)
                            if draftable_salaries else 0
                        )
                        default_min = _salary_adjusted_trade_minutes(pos, _dk_sal)

                    pos_prior = _POSITION_PRIOR_RATES.get(
                        pos, _POSITION_PRIOR_RATES.get("SF", {})
                    )
                    pm = PlayerMinutes(
                        player_id=nba_pid,
                        player_name=player_info["full_name"],
                        position=pos,
                        team_id=team_id,
                        minutes_last_5=[default_min] * 5,
                        minutes_last_10=[default_min] * 10,
                        season_avg=default_min,
                        usage_rate=0.0,
                        roster_change_detected=True,
                        pts_per_min=pos_prior.get("PTS", 0.0),
                        reb_per_min=pos_prior.get("REB", 0.0),
                        ast_per_min=pos_prior.get("AST", 0.0),
                        stl_per_min=pos_prior.get("STL", 0.0),
                        blk_per_min=pos_prior.get("BLK", 0.0),
                        tov_per_min=pos_prior.get("TOV", 0.0),
                        fg3m_per_min=pos_prior.get("FG3M", 0.0),
                    )
                    all_players.append(pm)
                    logger.info(
                        f"Trade detection: Added {dk_name} (NBA ID {nba_pid}) "
                        f"to team {team_id} with {default_min} min "
                        f"(active={not _is_inactive}, pos={pos}, "
                        f"roster_change=True)"
                    )

        all_players.sort(key=lambda x: x.season_avg, reverse=True)

        if not all_players:
            return all_players

        # ------------------------------------------------------------------
        # Determine the effective max rotation size.
        # ------------------------------------------------------------------
        effective_max = min(max_players, 15) if max_players > 0 else 13

        best_cut, best_gap, best_gap_ratio = self.find_rotation_cutoff(
            all_players, max_rotation=effective_max
        )

        # Include ALL DK-listed players in rotation — don't hard-cut.
        # Players below the cutoff keep their real (lower) season_avg
        # minutes so the projection engine gives them appropriately
        # low but non-zero projections.  This ensures every DK-listed
        # player appears in the player pool for lineup construction.
        rotation = all_players

        core_count = best_cut
        deep_count = len(all_players) - best_cut

        logger.info(
            f"Rotation for team {team_id}: {core_count} core + "
            f"{deep_count} deep bench players "
            f"(gap={best_gap:.1f} min, ratio={best_gap_ratio:.1%} "
            f"at position {best_cut}), "
            f"total season_avg={sum(p.season_avg for p in rotation):.1f}"
        )

        # --- Attach DK salaries to PlayerMinutes ---
        # Used by the rotation engine's auto-out logic: high-salary
        # players with short DNP streaks are exempt from auto-out
        # (DK wouldn't price them at $5K+ if they're suspended).
        if draftable_salaries:
            _norm_sal = {
                normalize_player_name(n): sal
                for n, sal in draftable_salaries.items()
            }
            _matched = 0
            for pm in rotation:
                pm_norm = normalize_player_name(pm.player_name)
                # Exact normalized match first
                if pm_norm in _norm_sal:
                    pm.dk_salary = _norm_sal[pm_norm]
                    _matched += 1
                else:
                    # Substring match (handles "Rob Dillingham" vs
                    # "Robert Dillingham" etc.)
                    for dk_norm, sal in _norm_sal.items():
                        if dk_norm in pm_norm or pm_norm in dk_norm:
                            pm.dk_salary = sal
                            _matched += 1
                            break
            if _matched:
                logger.debug(
                    f"Attached DK salaries to {_matched}/{len(rotation)} "
                    f"players for team {team_id}"
                )

        # --- Attach DK positions ---
        if draftable_positions:
            _norm_pos = {
                normalize_player_name(n): pos
                for n, pos in draftable_positions.items()
            }
            for pm in rotation:
                pm_norm = normalize_player_name(pm.player_name)
                if pm_norm in _norm_pos:
                    pm.dk_position = _norm_pos[pm_norm]
                else:
                    for dk_norm, pos in _norm_pos.items():
                        if dk_norm in pm_norm or pm_norm in dk_norm:
                            pm.dk_position = pos
                            break

        # --- Inject missing DK-listed players as synthetics ---
        # After the main rotation is built from BDL/DB cache, check for
        # DK-listed players that aren't in the rotation at all.  These are
        # typically recent call-ups, trade acquisitions, or players BDL
        # doesn't track.  Without injection, they're invisible to the
        # optimizer — a significant omission if DK projects them for
        # 10-25+ minutes.
        if draftable_names and draftable_salaries and rotation:
            _existing_norm = {
                normalize_player_name(pm.player_name) for pm in rotation
            }
            _injected = 0
            for dk_name in draftable_names:
                dk_norm = normalize_player_name(dk_name)
                # Check if already in rotation (exact or substring match)
                _found = dk_norm in _existing_norm or any(
                    dk_norm in en or en in dk_norm
                    for en in _existing_norm
                )
                if _found:
                    continue
                # Get salary — skip low-salary players ($3000 with 0 min
                # projected are end-of-bench DNPs)
                dk_sal = draftable_salaries.get(dk_name.lower(), 0)
                if dk_sal < 3100:
                    continue  # $3000 min-salary = likely DNP
                dk_pos = (draftable_positions or {}).get(
                    dk_name.lower(), "SF"
                )
                synth = build_synthetic_player(
                    player_name=dk_name,
                    team_id=team_id,
                    dk_salary=dk_sal,
                    dk_fppg=0.0,
                    dk_position=dk_pos,
                )
                rotation.append(synth)
                _injected += 1
            if _injected:
                logger.info(
                    f"[DK Inject] Added {_injected} synthetic player(s) "
                    f"for team {team_id} from DK draftables"
                )

        # --- Store in cache (only if non-empty) ---
        # Never cache empty rotations — they indicate data-source failure
        # (e.g. DB cache ID mismatch, BDL rate limit), not a legitimate
        # empty roster.  Caching [] would block BDL retry for 20 min.
        if rotation:
            _rotation_cache[team_id] = (time.time(), rotation)

        return rotation
