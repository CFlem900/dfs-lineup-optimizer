"""DFS lineup optimizer for DraftKings and FanDuel.

Builds optimal DFS lineups by:
1. Constructing a player pool (salary + projections merged)
2. Enriching the pool with simulation, expert-signal, and game-context data
3. Pre-assigning locked players
4. Greedy filling slots (most-constrained first)
5. Iterative pairwise swap improvement
6. Multi-lineup generation with diversity enforcement

Reuses existing services for salary data, rotation projections,
DFS fantasy-point scoring, Monte Carlo simulations, expert signals,
and game-level projections.

Internally, lineup dicts use *indexed* slot keys ("PG_0", "PG_1", etc.)
so that FanDuel's duplicate slot names (PG×2, SG×2, …) don't collide.
The index is stripped when building the public response.
"""

import asyncio
import hashlib
import json
import logging
import os
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed, wait as _futures_wait
from datetime import date, timedelta
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from app.config.constants import DK_TO_NBA_ABBR_ALIASES
from app.models.lineup import (
    ExcludedPlayerEntry,
    LineupPlayer,
    MultiLineupRequest,
    MultiLineupResponse,
    OptimizeRequest,
    OptimizedLineup,
    PlayerPoolEntry,
)
from app.services.nba_api_service import (
    _POSITION_PRIOR_RATES,
    _TRADE_DEFAULT_MINUTES,
    probe_nba_api,
)
from app.services.ownership_model import project_ownership as rules_project_ownership
from app.utils.exceptions import DataDegradationError, LineupGenerationError
from app.utils.helpers import normalize_player_name

# ILP solver (optional — graceful fallback to greedy if not installed)
try:
    import pulp
    _PULP_AVAILABLE = True
except ImportError:
    pulp = None  # type: ignore[assignment]
    _PULP_AVAILABLE = False

logger = logging.getLogger(__name__)

if not _PULP_AVAILABLE:
    logger.info("[ILP] PuLP not installed — using greedy optimizer fallback")

# ---------------------------------------------------------------------------
# Module-level caches — survive across requests within the same process.
# Cleared on server restart; file cache survives restarts.
# Protected by threading.Lock to prevent race conditions when accessed
# concurrently from async handlers or thread-pool workers (SSE stream).
# ---------------------------------------------------------------------------
_pool_cache: Dict[str, Tuple[float, List[PlayerPoolEntry], int]] = {}  # (timestamp, pool, expected_team_count)
_enriched_cache: Dict[str, Tuple[float, List[PlayerPoolEntry]]] = {}
_strategy_cache: Dict[str, Tuple[float, object]] = {}  # strategy adjustments alongside enriched pool
_pool_lock = threading.Lock()
_enriched_lock = threading.Lock()
_strategy_lock = threading.Lock()
_POOL_CACHE_TTL = 1800         # 30 minutes (was 15 — rotations stable intraday)
_POOL_FALLBACK_TTL = 300       # 5 minutes — pools with DK-fallback teams re-build sooner
_ENRICHED_CACHE_TTL = 1800     # 30 minutes (was 10 — sim results stable unless injuries change)
_FILE_CACHE_TTL = 7200         # 2 hours (file cache is disk-persistent)
_CACHE_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "cache")

# Per-slate build locks — prevents two concurrent requests (e.g. prewarm
# daemon + user SSE stream) from both building the same pool.  The second
# caller waits on the lock, then hits the fresh cache.
_build_locks: Dict[str, threading.Lock] = {}
_build_locks_meta_lock = threading.Lock()  # protects _build_locks dict itself

# Re-export shared alias map under the private name used throughout this module.
_DK_TO_NBA_ABBR_ALIASES = DK_TO_NBA_ABBR_ALIASES

# Improvement #4: External ownership import (CSV upload override).
# Module-level dict so imported ownership persists across generate calls
# until the server restarts or a new CSV is uploaded.
_imported_ownership: Dict[str, float] = {}  # normalized_name -> ownership_pct
_imported_ownership_lock = threading.Lock()

# External confirmed-starters import (CSV upload override).
# Same pattern as ownership import — persists until server restart or new upload.
_imported_starters: Dict[str, bool] = {}  # normalized_name -> True
_imported_starters_lock = threading.Lock()

# External projection import (CSV upload override).
# Same pattern as ownership import — persists until server restart or new upload.
_imported_projections: Dict[str, Dict[str, float]] = {}  # norm_name -> {projected_fp, floor_fp, ceiling_fp}
_imported_projections_lock = threading.Lock()


def _apply_imported_projection_overrides(pool) -> int:
    """Apply CSV-imported projection overrides to a pool in place.

    Returns the number of players whose projections were overridden.
    No-op when ``_imported_projections`` is empty.
    """
    if not _imported_projections:
        return 0
    from app.services.dk_draftables_service import _normalize_name
    count = 0
    with _imported_projections_lock:
        for entry in pool:
            ext = _imported_projections.get(_normalize_name(entry.player_name))
            if not ext:
                continue
            if "projected_fp" in ext:
                entry.projected_fp = ext["projected_fp"]
            if "floor_fp" in ext:
                entry.floor_fp = ext["floor_fp"]
            if "ceiling_fp" in ext:
                entry.ceiling_fp = ext["ceiling_fp"]
            # Sanitize: keep floor <= projected <= ceiling so downstream
            # synthetic-noise and sim-rescale clip bounds stay well-formed.
            if entry.floor_fp > entry.ceiling_fp:
                entry.floor_fp, entry.ceiling_fp = (
                    entry.ceiling_fp, entry.floor_fp
                )
            entry.floor_fp = max(0.0, min(entry.floor_fp, entry.projected_fp))
            entry.ceiling_fp = max(entry.ceiling_fp, entry.projected_fp)
            entry.projection_source = "external_import"
            entry.rotation_confidence = 1.0
            if entry.salary and entry.salary > 0:
                entry.dk_value = round(entry.projected_fp / entry.salary * 1000, 2)
            count += 1
    return count


def _apply_rotation_role(pool, sport: str) -> int:
    """Derive ``rotation_role`` for each pool entry (Prompt 7.8).

    The classification is purely a function of ``projected_minutes`` +
    ``injury_status`` + the sport's ``starter_min_minutes`` threshold:

      "Out"     — injury_status in {"Out", "Doubtful"}, OR
                  projected_minutes ≤ 0
      "Starter" — projected_minutes ≥ cfg.starter_min_minutes
      "Bench"   — 0 < projected_minutes < cfg.starter_min_minutes

    NFL / MLB use ``starter_min_minutes = 0`` in their SportConfig so
    every active player there lands in "Starter" — that's correct,
    those sports don't have a bench-rotation tier the way NBA does.
    The /api/player-pool endpoint calls this AFTER projections are
    finalized (post CSV-override) so the role reflects the final
    minutes value the user will see.

    Idempotent — safe to call multiple times. Returns the count of
    entries that received a non-None role (i.e., the count of pool
    entries whose role classification was successfully derived).
    """
    try:
        from app.sports import get_config
        cfg = get_config(sport)
        threshold = float(getattr(cfg, "starter_min_minutes", 28.0) or 0.0)
    except Exception:
        # Unknown sport / registry hiccup — fall back to NBA default
        # rather than crashing the pool fetch over a derived field.
        threshold = 28.0

    # Sports with no minutes concept (NFL = snap counts, MLB = full
    # game) ship ``starter_min_minutes = 0`` from their SportConfig.
    # We branch on that to avoid mis-classifying every NFL/MLB player
    # as "Out" just because ``projected_minutes`` defaults to 0.0.
    no_minutes_concept = threshold <= 0

    classified = 0
    for entry in pool:
        if entry.injury_status in ("Out", "Doubtful"):
            entry.rotation_role = "Out"
        elif no_minutes_concept:
            # NFL / MLB: any non-injured player is a "Starter".
            # There's no bench-tier classification because the
            # sport doesn't track minutes — players either play or
            # don't, with the inactive case already caught above.
            entry.rotation_role = "Starter"
        elif not entry.projected_minutes or entry.projected_minutes <= 0:
            entry.rotation_role = "Out"
        elif entry.projected_minutes >= threshold:
            entry.rotation_role = "Starter"
        else:
            entry.rotation_role = "Bench"
        classified += 1
    return classified


# ── Custom Projections Override (file-based) ──────────────────────────
# Reads optional custom_projections.csv from the backend root.
# Injects missing players (dropped by name-matching) or overrides existing
# players with exact DK-spelling-matched projections.
# Auto-reloads when file mtime changes — no server restart needed.
_CUSTOM_PROJ_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "custom_projections.csv"
)
_custom_projections: Dict[str, Dict[str, Any]] = {}  # exact DK display_name -> projection data
_custom_projections_mtime: float = 0.0


def _load_custom_projections() -> Dict[str, Dict[str, Any]]:
    """Load manual override projections from custom_projections.csv.

    Auto-reloads if file mtime has changed.  Returns empty dict if
    file doesn't exist (no-op by default).

    CSV format::

        Player Name,Team,Position,Projected Minutes,FPPM
        GG Jackson,MEM,PF,28,1.15

    Computes:
        projected_fp = Projected Minutes × FPPM
        floor_fp     = projected_fp × 0.65
        ceiling_fp   = projected_fp × 1.45
    """
    global _custom_projections, _custom_projections_mtime
    import csv as _csv

    path = os.path.abspath(_CUSTOM_PROJ_PATH)
    if not os.path.isfile(path):
        if _custom_projections:
            logger.info("[CustomProj] File removed — clearing overrides")
            _custom_projections.clear()
            _custom_projections_mtime = 0.0
        return _custom_projections

    mtime = os.path.getmtime(path)
    if mtime == _custom_projections_mtime:
        return _custom_projections  # already loaded, no change

    overrides: Dict[str, Dict[str, Any]] = {}
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            reader = _csv.DictReader(f)
            for row in reader:
                name = (row.get("Player Name") or "").strip()
                if not name:
                    continue
                team = (row.get("Team") or "").strip().upper()
                position = (row.get("Position") or "SF").strip().upper()
                try:
                    minutes = float(row.get("Projected Minutes") or 0)
                    fppm = float(row.get("FPPM") or 0)
                except (ValueError, TypeError):
                    logger.warning("[CustomProj] Skipping bad row: %s", row)
                    continue
                if minutes <= 0 or fppm <= 0:
                    logger.warning(
                        "[CustomProj] Skipping %s: minutes=%.1f, fppm=%.2f",
                        name, minutes, fppm,
                    )
                    continue

                proj_fp = round(minutes * fppm, 1)
                overrides[name] = {
                    "team": team,
                    "position": position,
                    "projected_minutes": round(minutes, 1),
                    "fppm": fppm,
                    "projected_fp": proj_fp,
                    "floor_fp": round(proj_fp * 0.65, 1),
                    "ceiling_fp": round(proj_fp * 1.45, 1),
                }

        _custom_projections = overrides
        _custom_projections_mtime = mtime
        if overrides:
            logger.info(
                "[CustomProj] Loaded %d manual overrides from %s: %s",
                len(overrides), path,
                ", ".join(
                    f"{n} ({d['team']}, {d['projected_minutes']:.0f}m, "
                    f"{d['projected_fp']:.1f}fp)"
                    for n, d in overrides.items()
                ),
            )
    except Exception as e:
        logger.error("[CustomProj] Failed to load %s: %s", path, e)

    return _custom_projections


def _cache_key(platform: str, draft_group_id: int, game_date: str) -> str:
    """Build a deterministic cache key."""
    return f"{platform}:{draft_group_id}:{game_date}"


def _file_cache_path(key: str) -> str:
    """Convert a cache key to a filesystem path."""
    h = hashlib.md5(key.encode()).hexdigest()[:12]
    return os.path.join(_CACHE_DIR, f"pool_{h}.json")


def _save_pool_to_file(key: str, pool: List[PlayerPoolEntry], expected_teams: int = 0) -> None:
    """Persist a player pool to a JSON file for cross-restart caching."""
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        path = _file_cache_path(key)
        data = {
            "timestamp": time.time(),
            "key": key,
            "players": [p.model_dump() for p in pool],
            "expected_teams": expected_teams,
        }
        with open(path, "w") as f:
            json.dump(data, f)
        logger.info(f"[Cache] Saved pool to file: {path} ({len(pool)} players)")
    except Exception as e:
        logger.warning(f"[Cache] Failed to save pool file: {e}")


def _load_pool_from_file(key: str) -> Optional[Tuple[List[PlayerPoolEntry], int]]:
    """Load a player pool from file cache if still fresh.

    Returns (pool, expected_teams) or None if cache is missing/expired.
    """
    try:
        path = _file_cache_path(key)
        if not os.path.exists(path):
            return None
        with open(path, "r") as f:
            data = json.load(f)
        age = time.time() - data.get("timestamp", 0)
        if age > _FILE_CACHE_TTL:
            logger.info(f"[Cache] File cache expired ({age:.0f}s > {_FILE_CACHE_TTL}s)")
            return None
        players = [PlayerPoolEntry(**p) for p in data["players"]]
        expected_teams = data.get("expected_teams", 0)
        logger.info(f"[Cache] Loaded pool from file: {len(players)} players (age={age:.0f}s)")
        return players, expected_teams
    except Exception as e:
        logger.warning(f"[Cache] Failed to load pool file: {e}")
        return None


def _get_fallback_teams(pool: List[PlayerPoolEntry]) -> List[str]:
    """Return team abbreviations where ALL entries used DK fallback.

    When BDL/NBA-API fails for a team, the pool builder creates synthetic
    entries with ``projection_source`` set to "dk_fppg" or "salary_estimate".
    These have baseline salary-ranked minutes (24m avg) instead of real
    rotation-projected minutes (30-35m for starters).

    A pool containing fallback-only teams should use a shorter cache TTL
    (``_POOL_FALLBACK_TTL``) so fresh rotation data is fetched sooner once
    the API recovers from transient 429s / timeouts.
    """
    _team_has_rotation: Dict[str, bool] = {}
    for p in pool:
        team = p.team_abbreviation
        if team not in _team_has_rotation:
            _team_has_rotation[team] = False
        # Normal rotation entries have projection_source=None
        if p.projection_source is None:
            _team_has_rotation[team] = True
    return sorted(
        t for t, has_rot in _team_has_rotation.items() if not has_rot
    )


def _effective_pool_ttl(pool: List[PlayerPoolEntry]) -> float:
    """Return effective cache TTL based on pool quality.

    Full TTL (30 min) for pools built entirely from rotation data.
    Short TTL (5 min) if any teams used DK fallback — gives BDL/API
    time to recover from transient 429s before retrying.
    """
    fb_teams = _get_fallback_teams(pool)
    if fb_teams:
        return _POOL_FALLBACK_TTL
    return _POOL_CACHE_TTL


# ---------------------------------------------------------------------------
# Enriched pool file cache — persists the ENRICHED pool to disk so
# downstream scripts and server restarts don't lose sim/FPPG data.
# Separate from the raw pool_*.json (which is pre-enrichment).
# ---------------------------------------------------------------------------
_ENRICHED_FILE_CACHE_TTL = 7200  # 2 hours (matches raw file cache)
_ENRICHED_RETRY_DELAY_S = 60     # Retry delay when enrichment fails validation


def _enriched_file_cache_path(key: str) -> str:
    """Convert a cache key to an enriched file cache path."""
    h = hashlib.md5(key.encode()).hexdigest()[:12]
    return os.path.join(_CACHE_DIR, f"enriched_{h}.json")


def _save_enriched_pool_to_file(
    key: str, pool: List[PlayerPoolEntry],
) -> None:
    """Persist an ENRICHED player pool to a JSON file.

    Only called after the pool passes enrichment validation — this
    file is the 'last known good' enriched pool that downstream
    scripts and recovery logic can trust.
    """
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        path = _enriched_file_cache_path(key)
        data = {
            "timestamp": time.time(),
            "key": key,
            "enriched": True,
            "players": [p.model_dump() for p in pool],
        }
        with open(path, "w") as f:
            json.dump(data, f)
        logger.info(
            f"[Cache] Saved ENRICHED pool to file: {path} ({len(pool)} players)"
        )
    except Exception as e:
        logger.warning(f"[Cache] Failed to save enriched pool file: {e}")


def _load_enriched_pool_from_file(
    key: str,
) -> Optional[List[PlayerPoolEntry]]:
    """Load a previously validated enriched pool from file cache.

    Returns the pool list or None if cache is missing/expired.
    Used as fallback when a fresh enrichment fails validation.
    """
    try:
        path = _enriched_file_cache_path(key)
        if not os.path.exists(path):
            return None
        with open(path, "r") as f:
            data = json.load(f)
        age = time.time() - data.get("timestamp", 0)
        if age > _ENRICHED_FILE_CACHE_TTL:
            logger.info(
                f"[Cache] Enriched file cache expired ({age:.0f}s > "
                f"{_ENRICHED_FILE_CACHE_TTL}s)"
            )
            return None
        players = [PlayerPoolEntry(**p) for p in data["players"]]
        logger.info(
            f"[Cache] Loaded ENRICHED pool from file: "
            f"{len(players)} players (age={age:.0f}s)"
        )
        return players
    except Exception as e:
        logger.warning(f"[Cache] Failed to load enriched pool file: {e}")
        return None


# ── Enrichment quality gate thresholds ────────────────────────────────
# These constants control the minimum enrichment coverage required
# before a pool is considered "good enough" to persist to disk.
_FPPG_CHECK_TOP_N = 20          # Check FPPG on top N players by salary
_FPPG_MIN_COVERAGE = 15         # At least this many must have dk_fppg
_SIM_TUNING_MIN_COVERAGE = 15   # At least this many must have std_dev_multiplier


def _validate_pool_enrichment(
    pool: List[PlayerPoolEntry],
    noise_override_count: int = 0,
) -> None:
    """Validate that critical enrichment data populated successfully.

    Raises DataDegradationError if the pool is degraded, preventing
    it from being cached to disk and overwriting a previous good cache.

    Checks
    ------
    1. **DK FPPG** (circuit breaker check):
       Sort pool by salary descending, take top 20.  If fewer than 15
       have a valid ``dk_fppg`` value, the DraftKings circuit breaker
       likely tripped during ``_fetch_dk_fppg()``.

    2. **Sim Tuning** (noise profile check):
       Count ALL pool entries with a valid ``std_dev_multiplier``.
       If fewer than 15 have one, both the direct Anthropic API call
       AND the deterministic role-classification fallback failed.
       A secondary signal (``noise_override_count`` + ``sim_p10/p50/p90``)
       is checked as a safety net — if the simulation engine ran
       successfully, the pool is still usable even without explicit
       noise profiles on every entry.

    Parameters
    ----------
    pool : list[PlayerPoolEntry]
        The enriched player pool to validate.
    noise_override_count : int
        Number of per-stat sigma overrides generated by ``_fetch_sim_tuning()``
        and fed into the simulation engine.  Acts as a secondary signal
        when ``std_dev_multiplier`` is missing (the deterministic fallback
        produces per-stat sigmas but not player-level multipliers).

    Raises
    ------
    DataDegradationError
        If any enrichment check fails.
    """
    failed_checks: list[str] = []

    # ── Check 1: DK FPPG coverage on top-salary players ──────────
    top_by_salary = sorted(pool, key=lambda p: p.salary, reverse=True)[:_FPPG_CHECK_TOP_N]
    fppg_count = sum(1 for p in top_by_salary if p.dk_fppg is not None and p.dk_fppg > 0)
    fppg_pct = fppg_count / max(len(top_by_salary), 1) * 100

    if fppg_count < _FPPG_MIN_COVERAGE:
        failed_checks.append(
            f"DK FPPG: only {fppg_count}/{len(top_by_salary)} top-salary "
            f"players have dk_fppg ({fppg_pct:.0f}% coverage, "
            f"need >={_FPPG_MIN_COVERAGE}) — circuit breaker likely tripped"
        )

    # ── Check 2: Sim Tuning noise profiles ────────────────────────
    # Primary signal: count players with std_dev_multiplier populated
    # (set by either the direct Anthropic API call or the salary-tier
    # fallback in _generate_fallback_enrichment).
    std_dev_count = sum(
        1 for p in pool
        if p.std_dev_multiplier is not None and p.std_dev_multiplier > 0
    )

    if std_dev_count < _SIM_TUNING_MIN_COVERAGE:
        # Secondary safety net: if the deterministic role-classifier
        # populated per-stat sigma overrides (noise_override_count > 0)
        # or the simulation engine produced percentiles (sim_p10/p50/p90),
        # the pool is still usable for lineup generation — the ILP
        # composite scorer can function without player-level multipliers.
        sim_enriched = sum(
            1 for p in pool
            if p.sim_p10 is not None or p.sim_p50 is not None or p.sim_p90 is not None
        )
        has_secondary_sim_data = noise_override_count > 0 or sim_enriched > 0

        if not has_secondary_sim_data:
            failed_checks.append(
                f"Sim Tuning: only {std_dev_count}/{len(pool)} players have "
                f"std_dev_multiplier (need >={_SIM_TUNING_MIN_COVERAGE}), "
                f"AND 0 per-stat sigma overrides, AND 0 sim percentiles — "
                f"simulation tuning completely failed"
            )
        else:
            # Warn but don't fail — secondary data is sufficient
            logger.warning(
                f"[Enrich] Sim tuning partial: only {std_dev_count} players "
                f"have std_dev_multiplier, but {noise_override_count} per-stat "
                f"overrides + {sim_enriched} sim percentiles available "
                f"(secondary data sufficient, not blocking cache)"
            )

    # ── Verdict ───────────────────────────────────────────────────
    if failed_checks:
        msg = (
            f"CRITICAL: Pool enrichment failed validation. "
            f"Refusing to overwrite cache. "
            f"Failures: {'; '.join(failed_checks)}. "
            f"Retrying in {_ENRICHED_RETRY_DELAY_S} seconds..."
        )
        raise DataDegradationError(
            message=msg,
            fppg_coverage=fppg_pct,
            sim_coverage=std_dev_count,
            failed_checks=failed_checks,
        )


def clear_optimizer_cache(
    platform: str = None,
    draft_group_id: int = None,
    game_date: str = None,
) -> int:
    """Clear optimizer caches.  If all args given, clear specific key; otherwise clear all.

    Returns the number of cache entries cleared.
    Thread-safe: acquires both cache locks before modifying.
    """
    cleared = 0
    with _pool_lock, _enriched_lock, _strategy_lock:
        if platform and draft_group_id and game_date:
            key = _cache_key(platform, draft_group_id, game_date)
            if key in _pool_cache:
                del _pool_cache[key]
                cleared += 1
            if key in _enriched_cache:
                del _enriched_cache[key]
                cleared += 1
            if key in _strategy_cache:
                del _strategy_cache[key]
                cleared += 1
            path = _file_cache_path(key)
            if os.path.exists(path):
                os.remove(path)
                cleared += 1
            enriched_path = _enriched_file_cache_path(key)
            if os.path.exists(enriched_path):
                os.remove(enriched_path)
                cleared += 1
        else:
            cleared += len(_pool_cache) + len(_enriched_cache) + len(_strategy_cache)
            _pool_cache.clear()
            _enriched_cache.clear()
            _strategy_cache.clear()
            # Remove all file caches (raw pool_* and enriched_*)
            if os.path.isdir(_CACHE_DIR):
                for fname in os.listdir(_CACHE_DIR):
                    if (fname.startswith("pool_") or fname.startswith("enriched_")) and fname.endswith(".json"):
                        os.remove(os.path.join(_CACHE_DIR, fname))
                        cleared += 1
    logger.info(f"[Cache] Cleared {cleared} cache entries")
    return cleared


# ---------------------------------------------------------------------------
# CBB team abbreviation resolution
# ---------------------------------------------------------------------------
# DraftKings and ESPN use different abbreviations for college teams.
# For example DK might use "KAN" while ESPN uses "KU" for Kansas.
# This mapping + fuzzy fallback bridges the gap.

# Known DK → ESPN abbreviation mismatches (extend as needed).
_DK_TO_ESPN_CBB_ABBR: Dict[str, str] = {
    # DK abbr → ESPN abbr  (extend as mismatches are discovered)
    "UH": "HOU",       # Houston Cougars
    "KAN": "KU",       # Kansas Jayhawks
    "CONN": "UCONN",   # Connecticut Huskies
    "IUPU": "IUPUI",
    "NCST": "NCSU",    # NC State Wolfpack
    "ORST": "ORST",
    "LOU": "LOU",
    "WASH": "WASH",
    "MSST": "MSST",
    "OKST": "OKST",
    "KAST": "KANS",
    "TXAM": "TA&M",    # Texas A&M
    "USM": "SMISS",    # Southern Miss
    "WIS": "WIS",
    "NEB": "NEB",
    "WISC": "WIS",
    "PITT": "PITT",
    "PURD": "PUR",     # Purdue
    "ILL": "ILL",
    "MICH": "MICH",
    "MIOH": "M-OH",    # Miami (OH)
    "BGSU": "BGSU",
    "IOWA": "IOWA",
    "UCLA": "UCLA",
    "HALL": "SHU",      # Seton Hall
    "GTOWN": "GTWN",   # Georgetown
    "CREI": "CREI",
    "MARQ": "MARQ",
    "NOVA": "NOVA",     # Villanova
    "TXST": "TXST",
    "UNLV": "UNLV",
}


def _resolve_cbb_abbr_map(
    dk_abbrs: set,
    espn_teams: List[Dict],
) -> Dict[str, Dict]:
    """Resolve DraftKings CBB team abbreviations to ESPN team dicts.

    Strategy:
      1. Direct abbreviation match (case-insensitive)
      2. Known alias mapping (_DK_TO_ESPN_CBB_ABBR)
      3. Fuzzy matching — DK abbreviation appears in the ESPN team's
         full_name, nickname, or abbreviation (substring match)

    Returns a dict mapping each DK abbreviation → ESPN team dict.
    """
    result: Dict[str, Dict] = {}

    # Index ESPN teams
    espn_by_abbr = {t["abbreviation"].upper(): t for t in espn_teams}
    espn_by_name_lower = {}
    for t in espn_teams:
        key = t["full_name"].lower()
        espn_by_name_lower[key] = t
        # Also index by nickname
        nick = t.get("nickname", "").lower()
        if nick:
            espn_by_name_lower[nick] = t

    for dk_abbr in dk_abbrs:
        up = dk_abbr.upper()

        # 1. Direct abbreviation match
        if up in espn_by_abbr:
            result[dk_abbr] = espn_by_abbr[up]
            continue

        # 2. Known alias
        mapped = _DK_TO_ESPN_CBB_ABBR.get(up, "").upper()
        if mapped and mapped in espn_by_abbr:
            result[dk_abbr] = espn_by_abbr[mapped]
            continue

        # 3. Fuzzy substring: check if DK abbreviation (≥3 chars)
        #    appears as a prefix/substring in an ESPN team's full name
        #    or if the ESPN abbreviation is a prefix of the DK abbreviation
        matched = False
        dk_lower = dk_abbr.lower()
        for t in espn_teams:
            espn_abbr_lower = t["abbreviation"].lower()
            full_lower = t["full_name"].lower()
            nick_lower = t.get("nickname", "").lower()

            # DK abbr matches start of ESPN full name word
            # e.g. DK "DUKE" → ESPN full_name "Duke Blue Devils"
            full_words = full_lower.split() if full_lower else []
            if full_words and dk_lower in full_words[0]:
                result[dk_abbr] = t
                matched = True
                break

            # ESPN abbr is prefix of DK abbr or vice versa (≥3 chars)
            if len(dk_lower) >= 3 and len(espn_abbr_lower) >= 3:
                if dk_lower.startswith(espn_abbr_lower) or espn_abbr_lower.startswith(dk_lower):
                    result[dk_abbr] = t
                    matched = True
                    break

            # DK abbr appears anywhere in full name (for longer abbrs)
            if len(dk_lower) >= 4 and dk_lower in full_lower:
                result[dk_abbr] = t
                matched = True
                break

            # DK abbr appears anywhere in nickname
            if len(dk_lower) >= 4 and nick_lower and dk_lower in nick_lower:
                result[dk_abbr] = t
                matched = True
                break

        if not matched:
            logger.warning(
                f"[CBB] Could not resolve DK abbreviation '{dk_abbr}' "
                f"to any ESPN team"
            )

    return result


# ---------------------------------------------------------------------------
# Platform constants
# ---------------------------------------------------------------------------
# ── Sport-specific values now live in app.sports.* ───────────────────
# The legacy module-level constants below are kept for backward compat
# with callers that import them directly. New code MUST go through
# ``app.sports.get_config(sport)`` so adding a new sport (NFL/MLB) is a
# single registry edit rather than scattered ``if sport == 'X':`` branches.
from app.sports import get_config as _get_sport_cfg  # noqa: E402

DK_SALARY_CAP = _get_sport_cfg("nba").salary_cap_dk  # 50_000
FD_SALARY_CAP = 60_000

DK_ROSTER_SLOTS = list(_get_sport_cfg("nba").dk_roster_slots)
DK_CBB_ROSTER_SLOTS = list(_get_sport_cfg("cbb").dk_roster_slots)
FD_ROSTER_SLOTS = ["PG", "PG", "SG", "SG", "SF", "SF", "PF", "PF", "C"]

# Showdown / Captain mode (single-game) — not yet in SportConfig because
# Showdown rules differ across sports and aren't part of the Classic flow.
DK_SHOWDOWN_SALARY_CAP = 50_000
DK_SHOWDOWN_SLOTS = ["CPT", "FLEX", "FLEX", "FLEX", "FLEX", "FLEX"]
CPT_MULTIPLIER = 1.5

DK_SLOT_ELIGIBILITY: Dict[str, List[str]] = dict(_get_sport_cfg("nba").dk_slot_eligibility)
DK_CBB_SLOT_ELIGIBILITY: Dict[str, List[str]] = dict(_get_sport_cfg("cbb").dk_slot_eligibility)

DK_SHOWDOWN_ELIGIBILITY: Dict[str, List[str]] = {
    "CPT": ["PG", "SG", "SF", "PF", "C"],
    "FLEX": ["PG", "SG", "SF", "PF", "C"],
}

FD_SLOT_ELIGIBILITY: Dict[str, List[str]] = {
    "PG": ["PG"],
    "SG": ["SG"],
    "SF": ["SF"],
    "PF": ["PF"],
    "C": ["C"],
}

# Slot processing order (most constrained → least constrained).
# C goes first because it has the fewest eligible players; UTIL goes
# last because any player can fill it.
DK_SLOT_ORDER = list(_get_sport_cfg("nba").dk_slot_order)
DK_CBB_SLOT_ORDER = list(_get_sport_cfg("cbb").dk_slot_order)
FD_SLOT_ORDER = ["C", "PG", "PG", "SG", "SG", "SF", "SF", "PF", "PF"]


def _check_assist_synergy(
    p_a: Any, p_b: Any,
    ast_threshold: float,
    assisted_fg_threshold: float,
) -> bool:
    """Check if a player pair has PG→Big assist synergy.

    Returns True if one player is a high-assist guard and the other
    is a PF/C with high assisted-FG%.  This pair should receive a
    BONUS instead of a cannibalization penalty.

    The check is symmetric: (guard, big) and (big, guard) both work.
    """
    _BIG_POSITIONS = {"PF", "C"}

    def _is_high_assist_guard(p) -> bool:
        pos = (getattr(p, "position", "") or "").upper()
        positions = {pp.strip() for pp in pos.replace("-", "/").split("/")}
        is_guard = bool(positions & {"PG", "SG", "G"})
        ast = getattr(p, "ast_per_game", None) or 0
        return is_guard and ast >= ast_threshold

    def _is_assisted_big(p) -> bool:
        pos = (getattr(p, "position", "") or "").upper()
        positions = {pp.strip() for pp in pos.replace("-", "/").split("/")}
        is_big = bool(positions & _BIG_POSITIONS)
        afg = getattr(p, "assisted_fg_pct", None) or 0
        return is_big and afg >= assisted_fg_threshold

    return (
        (_is_high_assist_guard(p_a) and _is_assisted_big(p_b))
        or (_is_high_assist_guard(p_b) and _is_assisted_big(p_a))
    )


def _index_slots(slot_list: List[str]) -> List[str]:
    """Convert a slot list to indexed keys so duplicates become unique.

    Example: ["PG", "PG", "SG"] → ["PG_0", "PG_1", "SG_0"]
    """
    counts: Dict[str, int] = {}
    indexed: List[str] = []
    for slot in slot_list:
        idx = counts.get(slot, 0)
        indexed.append(f"{slot}_{idx}")
        counts[slot] = idx + 1
    return indexed


def _base_slot(indexed_key: str) -> str:
    """Strip the index suffix to recover the base slot name.

    Example: "PG_1" → "PG", "UTIL_0" → "UTIL"
    """
    return indexed_key.rsplit("_", 1)[0]

# FD salary estimation multiplier (FD $60K / 9 slots vs DK $50K / 8 slots)
FD_SALARY_RATIO = 1.2

# ---------------------------------------------------------------------------
# Overgeneration constants — generate more candidates than requested,
# then select the best diverse subset for higher quality lineups.
# ---------------------------------------------------------------------------
_OVERGEN_MULTIPLIER_SMALL = 3.0     # requests of 1-20 lineups → 3× raw candidates
_OVERGEN_MULTIPLIER_MEDIUM = 2.5    # requests of 21-80 → 2.5×
_OVERGEN_MULTIPLIER_LARGE = 2.0     # requests of 81-150 → 2×
_OVERGEN_MAX_CANDIDATES = 500       # hard cap — more raw candidates survive quality filters
_OVERGEN_MIN_CANDIDATES = 6         # minimum even for 1-lineup requests

# ---------------------------------------------------------------------------
# Fill-loop constants — when the initial overgeneration batch doesn't
# produce enough diverse lineups, generate individual replacements.
# ---------------------------------------------------------------------------
_FILL_MAX_ATTEMPTS_MULTIPLIER = 15  # max_attempts = n_requested × this (up from 5 — retry until fulfilled)
_FILL_CONSEC_REJECT_THRESHOLD = 25  # relax constraints after this many consecutive rejects (was 50)
_FILL_OVERLAP_RELAX_STEP = 1        # increase max_overlap by this per relaxation
_FILL_QUALITY_RELAX_FACTOR = 0.97   # multiply quality floor by this per relaxation (was 0.99 — faster relaxation)
_FILL_MAX_RELAXATION_ROUNDS = 6     # hard-stop: after N relaxation rounds, break and return what we have


# ---------------------------------------------------------------------------
# Shared exposure state — thread-safe draft counts for cross-worker
# coordination during K-Best iterative generation.
# ---------------------------------------------------------------------------
class _SharedExposureCounts:
    """Thread-safe player draft counts shared across K-Best workers.

    Uses a ``threading.Lock`` to protect the dict since
    read-modify-write (``dict[k] = dict.get(k, 0) + 1``) is NOT
    atomic even under the GIL.
    """

    __slots__ = ("_lock", "_counts")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts: Dict[int, int] = {}

    def increment_batch(self, player_ids: List[int]) -> None:
        """Atomically increment counts for all players in a lineup."""
        with self._lock:
            for pid in player_ids:
                self._counts[pid] = self._counts.get(pid, 0) + 1

    def snapshot(self) -> Dict[int, int]:
        """Return a shallow copy of current counts.

        ``dict.copy()`` is atomic under CPython's GIL for simple dicts.
        A stale-by-one-lineup snapshot is acceptable — the quadratic
        penalty is continuous and tolerates minor lag.
        """
        return self._counts.copy()


class LineupOptimizerService:
    """Builds optimal DFS lineups from rotation projections + salary data."""

    def __init__(
        self,
        dfs_service,
        dk_draftables_service,
        nba_service,
        injury_service,
        rotation_engine,
        simulation_engine=None,
        expert_signal_service=None,
        game_service=None,
        # AI Agents (optional — graceful degradation when None)
        news_projection_agent=None,
        ownership_agent=None,
        lineup_strategy_agent=None,
        simulation_tuning_agent=None,
        calibration_service=None,
        correlation_service=None,
        # DK data services (optional — graceful degradation)
        dk_props_service=None,
        vegas_player_props_service=None,
        dk_available_players_service=None,
        # Fade/leverage integration (optional)
        fade_service=None,
        # CBB-specific services (optional — used when sport="cbb")
        cbb_data_service=None,
        cbb_game_service=None,
        cbb_injury_service=None,
        # Line movement agent (optional — provides BDL live odds)
        line_movement_agent=None,
        # Solver tracking (optional — logs runs to DB for ROI analysis)
        solver_tracking_service=None,
    ):
        self.dfs_service = dfs_service
        self.dk_draftables_service = dk_draftables_service
        self.nba_service = nba_service
        self.injury_service = injury_service
        self.engine = rotation_engine
        self.simulation_engine = simulation_engine
        self.expert_signal_service = expert_signal_service
        self.game_service = game_service
        # CBB services
        self.cbb_data_service = cbb_data_service
        self.cbb_game_service = cbb_game_service
        self.cbb_injury_service = cbb_injury_service
        # AI agents
        self.news_projection_agent = news_projection_agent
        self.ownership_agent = ownership_agent
        self.lineup_strategy_agent = lineup_strategy_agent
        self.simulation_tuning_agent = simulation_tuning_agent
        # Calibration service (auto-applies learned adjustments)
        self.calibration_service = calibration_service
        # Correlation service (for stack player selection)
        self._correlation_service = correlation_service
        # DK data services (props + FPPG)
        self.dk_props_service = dk_props_service
        self.vegas_player_props_service = vegas_player_props_service
        self.dk_available_players_service = dk_available_players_service
        # Fade service (optional — for fade/leverage integration)
        self.fade_service = fade_service
        # Line movement agent (provides BDL live odds for blowout penalty)
        self.line_movement_agent = line_movement_agent
        # Solver tracking service (logs runs to DB for ROI analysis)
        self.solver_tracking_service = solver_tracking_service
        # Cached correlation weights {(pid_a, pid_b): correlation}
        self._cached_correlations: Dict[Tuple[int, int], float] = {}
        # Strategy adjustments cache (populated during enrichment)
        self._strategy_adjustments = None
        # Contest-driven solver config (populated per generate_lineups call)
        self._lineup_strategy = None
        # Per-player fade/leverage scores (populated during enrichment)
        self._fade_leverage_scores: Dict[int, Dict[str, float]] = {}
        # GPP improvements — per-generation instance state
        self._slate_avg_game_total: float = 0.0
        self._secondary_stack_game_id: Optional[str] = None
        self._slate_adjustments: Optional[Dict] = None

    # ------------------------------------------------------------------
    # Correlation pre-fetch
    # ------------------------------------------------------------------

    def _prefetch_correlations(self, pool: List[PlayerPoolEntry]) -> None:
        """Pre-fetch player-pair correlations for stacking decisions.

        Extracts unique team abbreviations from the pool, looks up team
        IDs from the DB, and fetches pairwise correlations via the
        CorrelationService.  Results are stored in
        ``self._cached_correlations`` as {(min_pid, max_pid): corr}.

        Runs async code in a new event loop on a background thread to
        avoid blocking or conflicting with any existing asyncio loop.
        """
        if not self._correlation_service:
            return

        self._cached_correlations = {}

        # Collect unique team abbreviations from pool
        teams = set()
        for p in pool:
            if p.team_abbreviation:
                teams.add(p.team_abbreviation.upper())

        if not teams:
            return

        async def _fetch_all():
            """Fetch intra-team and cross-team correlations for stacking."""
            correlations: Dict[Tuple[int, int], float] = {}

            try:
                from app.db.database import is_db_available, get_session
                from app.db.models import PlayerMinutesHistory
                from sqlalchemy import select, distinct
                from datetime import datetime, timedelta, timezone

                if not is_db_available():
                    return correlations

                # Map team abbreviations to team IDs
                team_id_map: Dict[str, int] = {}
                cutoff = datetime.now(timezone.utc) - timedelta(days=60)
                async with get_session() as session:
                    for team_abbr in teams:
                        stmt = (
                            select(distinct(PlayerMinutesHistory.team_id))
                            .where(
                                PlayerMinutesHistory.team_abbreviation == team_abbr,
                                PlayerMinutesHistory.game_date >= cutoff,
                            )
                            .limit(1)
                        )
                        result = await session.execute(stmt)
                        team_id = result.scalar()
                        if team_id is None:
                            continue
                        team_id_map[team_abbr] = team_id

                        # Intra-team correlations
                        data = await self._correlation_service.get_correlated_pairs(
                            team_id=team_id,
                            min_correlation=0.15,
                            days=60,
                        )
                        for pair in data.get("pairs", []):
                            pid_a = pair["player_a_id"]
                            pid_b = pair["player_b_id"]
                            key = (min(pid_a, pid_b), max(pid_a, pid_b))
                            correlations[key] = pair["correlation"]

                # Cross-team correlations for bring-back logic.
                # Group pool by game_id to find opposing team pairs.
                game_teams: Dict[str, set] = {}
                for p in pool:
                    if p.game_id and p.team_abbreviation:
                        abbr = p.team_abbreviation.upper()
                        tid = team_id_map.get(abbr)
                        if tid:
                            game_teams.setdefault(p.game_id, set()).add(tid)

                for _gid, tids in game_teams.items():
                    tids_list = sorted(tids)
                    if len(tids_list) >= 2:
                        try:
                            game_data = await self._correlation_service.get_game_correlations(
                                home_team_id=tids_list[0],
                                away_team_id=tids_list[1],
                                days=60,
                            )
                            for pair in game_data.get("cross_team_pairs", []):
                                pid_a = pair["player_a_id"]
                                pid_b = pair["player_b_id"]
                                key = (min(pid_a, pid_b), max(pid_a, pid_b))
                                correlations[key] = pair["correlation"]
                        except Exception:
                            pass  # Cross-team fetch is best-effort
            except Exception as exc:
                logger.debug(f"Correlation pre-fetch failed: {exc}")

            return correlations

        # Run async code in a new thread with its own event loop
        result = {}
        def _run():
            nonlocal result
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    result = loop.run_until_complete(_fetch_all())
                finally:
                    loop.close()
            except Exception as exc:
                logger.debug(f"Correlation pre-fetch thread failed: {exc}")

        try:
            t = threading.Thread(target=_run, daemon=True)
            t.start()
            t.join(timeout=5.0)  # 5s max wait — don't block pool build too long
            if result:
                self._cached_correlations = result
                logger.info(
                    f"[Correlation] Pre-fetched {len(result)} player-pair "
                    f"correlations for {len(teams)} teams"
                )
        except Exception as exc:
            logger.debug(f"Correlation pre-fetch failed: {exc}")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_cached_pool(
        self,
        sport: str = "nba",
        draft_group_id: Optional[int] = None,
    ) -> Optional[List[PlayerPoolEntry]]:
        """Return a player pool from the cache, if fresh.

        When *draft_group_id* is provided, returns the exact pool for
        that DraftGroup (cache key format ``<platform>:<dgid>:<date>``).
        Otherwise falls back to scanning the cache for any fresh pool
        matching the sport prefix (legacy behaviour).

        Filters by sport prefix to prevent cross-sport cache hits.
        Returns ``None`` if no valid pool is cached.
        """
        now = time.time()
        with _pool_lock:
            if draft_group_id is not None:
                # Exact match: look for any key containing :<dgid>:
                _dg_str = f":{draft_group_id}:"
                for key, entry in _pool_cache.items():
                    if _dg_str in key and not key.endswith(":inj"):
                        cached_at = entry[0]
                        cached_pool = entry[1]
                        if now - cached_at < _POOL_CACHE_TTL:
                            return cached_pool
                return None

            # Legacy: any fresh pool for the sport
            prefix = f"{sport}:"
            for key, entry in _pool_cache.items():
                cached_at = entry[0]
                cached_pool = entry[1]
                if key.startswith(prefix) and not key.endswith(":inj") and now - cached_at < _POOL_CACHE_TTL:
                    return cached_pool
        return None

    # Process teams with limited concurrency.  The rate limiter in
    # NBAApiService gates actual API throughput — more workers means
    # more teams ready to go when the rate limiter opens a slot,
    # reducing idle time between team completions.
    # Reduced from 6-8 to 2: BDL rate limiter (600 RPM) can't handle
    # 6-8 concurrent teams each making 2+ API calls simultaneously.
    # With 2 workers, BDL requests are staggered enough to stay under
    # burst limits while still providing parallelism for the CPU-bound
    # projection work.
    _MAX_TEAM_WORKERS = 2

    def build_player_pool(
        self,
        platform: str,
        draft_group_id: int,
        game_date: Optional[str] = None,
        excluded_player_ids: Optional[List[int]] = None,
        on_progress: Optional[Callable[[str, int, int], None]] = None,
        sport: str = "nba",
        data_service=None,
        game_service_override=None,
        injury_service_override=None,
        recent_weight: Optional[float] = None,
        return_excluded: bool = False,
        cancelled: Optional[threading.Event] = None,
    ):
        """Build the full player pool for a slate.

        Merges DK draftables (salary + position) with our rotation
        projections (minutes + FP) to produce a single flat list of
        players annotated with eligible roster slots.

        Teams are processed **in parallel** (up to 3 concurrently) to
        dramatically reduce cold-cache load times.  Deep-bench players
        not in the DK draftable list are skipped to reduce NBA API calls.

        Args:
            on_progress: Optional callback ``(step_label, completed, total)``
                for streaming progress to the frontend.
            return_excluded: When True, return a tuple
                ``(pool, excluded_players)`` instead of just ``pool``.
                Backward-compatible — all existing callers get
                ``List[PlayerPoolEntry]`` by default.
            cancelled: Optional threading.Event set when the client
                disconnects (SSE stream abort).  When set, the build
                short-circuits to avoid wasting threads on an abandoned
                request.

        Returns:
            ``List[PlayerPoolEntry]`` when *return_excluded* is False (default).
            ``Tuple[List[PlayerPoolEntry], List[ExcludedPlayerEntry]]``
            when *return_excluded* is True.
        """
        excluded = set(excluded_player_ids or [])
        gd = game_date or date.today().isoformat()
        t_pool_start = time.time()

        # ── Resolve sport-specific services ────────────────────────
        # NBA / CBB services are constructor-injected. MLB / NFL aren't,
        # so when no override is passed (e.g. from /api/generate-lineups
        # which doesn't pass overrides like /api/player-pool does), they
        # fell back to NBA services and the resulting pool was empty.
        # Prompt 7.13: route MLB / NFL through the service container,
        # matching the pattern that already works elsewhere.
        if sport == "cbb":
            _data_svc = data_service or self.cbb_data_service or self.nba_service
            _game_svc = game_service_override or self.cbb_game_service or self.game_service
            _injury_svc = injury_service_override or self.cbb_injury_service or self.injury_service
        elif sport in ("mlb", "nfl") and not (data_service and game_service_override):
            # Look up sport-specific services from the container when
            # caller didn't pass overrides. Falling back to NBA here
            # would silently return an empty pool because
            # NBADataService.get_team_by_abbreviation('LAD') returns
            # None — every MLB team would be skipped.
            try:
                from app.api.dependencies import get_services as _get_svc
                _container = _get_svc()
                _data_svc = data_service or _container.get_data_service(sport)
                _game_svc = game_service_override or _container.get_game_service(sport)
                _injury_svc = injury_service_override or _container.get_injury_service(sport)
            except Exception as _svc_exc:
                logger.warning(
                    "[Pool] Could not resolve %s services from container: %s — "
                    "falling back to NBA defaults (pool will likely be empty)",
                    sport.upper(), _svc_exc,
                )
                _data_svc = data_service or self.nba_service
                _game_svc = game_service_override or self.game_service
                _injury_svc = injury_service_override or self.injury_service
        else:
            _data_svc = data_service or self.nba_service
            _game_svc = game_service_override or self.game_service
            _injury_svc = injury_service_override or self.injury_service

        # ── Cache check (in-memory → file → build) ───────────────
        cache_key = _cache_key(f"{sport}:{platform}", draft_group_id, gd)
        now = time.time()

        # ── Slate-aware injury invalidation ──────────────────────
        # Fetch draftables early (cached, ~instant) so we can compute
        # an injury hash scoped to only teams in this slate, avoiding
        # cache busts from unrelated injury changes.
        draftables = self.dk_draftables_service.get_draftables(draft_group_id)
        if not draftables:
            logger.warning(
                f"No draftables for DG {draft_group_id} — "
                f"the DraftGroup may be in Preliminary state "
                f"(salaries not yet published) or the ID may be invalid."
            )
            return ([], []) if return_excluded else []

        _slate_team_names: List[str] = list({
            d.team_abbreviation.upper() for d in draftables
        })

        with _pool_lock, _enriched_lock, _strategy_lock:
            injury_hash = _injury_svc.get_injury_hash(team_names=_slate_team_names)
            _injury_hash_key = f"{cache_key}:inj"
            _prev_hash = _pool_cache.get(_injury_hash_key, (0, ""))[1]
            if isinstance(_prev_hash, str) and _prev_hash and _prev_hash != injury_hash:
                logger.info(
                    f"[Cache] Injury data changed for slate teams "
                    f"({_prev_hash} → {injury_hash}), "
                    f"busting pool cache for {cache_key}"
                )
                _pool_cache.pop(cache_key, None)
                _enriched_cache.pop(cache_key, None)
                _strategy_cache.pop(cache_key, None)
                # Also remove file cache
                path = _file_cache_path(cache_key)
                if os.path.exists(path):
                    os.remove(path)
            _pool_cache[_injury_hash_key] = (now, injury_hash)

            # Expected team count from current draftables (authoritative)
            _expected_team_count = len(_slate_team_names)

            # Layer 1: in-memory cache
            if cache_key in _pool_cache:
                _cached_tuple = _pool_cache[cache_key]
                cached_at = _cached_tuple[0]
                cached_pool = _cached_tuple[1]
                _cached_expected = _cached_tuple[2] if len(_cached_tuple) > 2 else 0
                _eff_ttl = _effective_pool_ttl(cached_pool)
                if now - cached_at < _eff_ttl:
                    # Validate: check team count against the slate, not just pool size
                    _teams_in_mem = len(set(p.team_abbreviation for p in cached_pool))
                    _mem_expected_min = max(20, _expected_team_count * 5)
                    _teams_missing = _expected_team_count - _teams_in_mem
                    if _teams_missing > 0:
                        logger.warning(
                            f"[Cache] Memory cache missing {_teams_missing} team(s) "
                            f"({_teams_in_mem}/{_expected_team_count} teams, "
                            f"{len(cached_pool)} players). Discarding partial pool."
                        )
                        del _pool_cache[cache_key]
                    elif len(cached_pool) < _mem_expected_min:
                        logger.warning(
                            f"[Cache] Memory cache too small ({len(cached_pool)} players, "
                            f"{_teams_in_mem} teams, need >= {_mem_expected_min}). Discarding."
                        )
                        del _pool_cache[cache_key]
                    else:
                        # Deep-copy so enrichment/overrides don't mutate the cache.
                        # Also re-filter against current DK injury statuses —
                        # the cached pool may predate a late scratch / GTD→OUT
                        # upgrade on DraftKings.
                        _dk_out = set()
                        for _d in draftables:
                            _dst = (getattr(_d, "status", "") or "").strip().upper()
                            if _dst in ("O", "OUT", "D", "DOUBTFUL"):
                                _did = getattr(_d, "dk_player_id", None)
                                if _did:
                                    _dk_out.add(_did)
                        result = [
                            p.copy() for p in cached_pool
                            if p.player_id not in excluded
                            and p.player_id not in _dk_out
                        ]
                        elapsed = time.time() - t_pool_start
                        _fb_teams = _get_fallback_teams(cached_pool)
                        _fb_note = f" (fallback teams: {', '.join(_fb_teams)}, TTL={_eff_ttl:.0f}s)" if _fb_teams else ""
                        _dk_out_note = f", {len(_dk_out)} DK-out filtered" if _dk_out else ""
                        logger.info(
                            f"[Cache] Pool memory hit: {len(result)} players "
                            f"(age={now - cached_at:.0f}s) in {elapsed:.3f}s{_fb_note}{_dk_out_note}"
                        )
                        if on_progress:
                            on_progress("Pool loaded (cached)", 1, 1)
                        return (result, []) if return_excluded else result
                else:
                    # Cache expired — log if it was a fallback pool expiring early
                    _fb_teams = _get_fallback_teams(cached_pool)
                    if _fb_teams:
                        logger.info(
                            f"[Cache] Fallback pool expired (age={now - cached_at:.0f}s, "
                            f"TTL={_eff_ttl:.0f}s). Teams on DK fallback: "
                            f"{', '.join(_fb_teams)}. Rebuilding with fresh rotation data."
                        )
                    del _pool_cache[cache_key]

        # Layer 2: file cache (outside pool lock — file I/O is slow)
        _file_result = _load_pool_from_file(cache_key)
        file_pool = None
        if _file_result is not None:
            file_pool, _file_expected = _file_result
            # Validate file cache against expected team count from slate
            _teams_in_file = set(p.team_abbreviation for p in file_pool)
            _file_teams_missing = _expected_team_count - len(_teams_in_file)
            _expected_min = max(20, _expected_team_count * 5)
            if _file_teams_missing > 0:
                logger.warning(
                    f"[Cache] File cache missing {_file_teams_missing} team(s) "
                    f"({len(_teams_in_file)}/{_expected_team_count} teams, "
                    f"{len(file_pool)} players). Discarding partial cache."
                )
                _stale_path = _file_cache_path(cache_key)
                if os.path.exists(_stale_path):
                    os.remove(_stale_path)
                file_pool = None
            elif len(file_pool) < _expected_min:
                logger.warning(
                    f"[Cache] File cache suspiciously small "
                    f"({len(file_pool)} players across {len(_teams_in_file)} "
                    f"teams, need >= {_expected_min}). Discarding stale cache."
                )
                _stale_path = _file_cache_path(cache_key)
                if os.path.exists(_stale_path):
                    os.remove(_stale_path)
                file_pool = None
            else:
                # Quality gate: check if file cache has fallback-only teams
                _file_fb_teams = _get_fallback_teams(file_pool)
                if _file_fb_teams:
                    # Use shorter TTL for fallback pools — check file age
                    _file_path = _file_cache_path(cache_key)
                    _file_age = time.time() - os.path.getmtime(_file_path) if os.path.exists(_file_path) else 0
                    if _file_age > _POOL_FALLBACK_TTL:
                        logger.info(
                            f"[Cache] File cache has DK-fallback teams "
                            f"({', '.join(_file_fb_teams)}) and age={_file_age:.0f}s "
                            f"> {_POOL_FALLBACK_TTL}s. Discarding to retry rotation builds."
                        )
                        if os.path.exists(_file_path):
                            os.remove(_file_path)
                        file_pool = None
        if file_pool is not None:
            with _pool_lock:
                _pool_cache[cache_key] = (now, file_pool, _expected_team_count)  # promote to memory
            # Re-filter against current DK injury statuses (same as memory hit)
            _dk_out_file = set()
            for _d in draftables:
                _dst = (getattr(_d, "status", "") or "").strip().upper()
                if _dst in ("O", "OUT", "D", "DOUBTFUL"):
                    _did = getattr(_d, "dk_player_id", None)
                    if _did:
                        _dk_out_file.add(_did)
            result = [
                p.copy() for p in file_pool
                if p.player_id not in excluded
                and p.player_id not in _dk_out_file
            ]
            elapsed = time.time() - t_pool_start
            _dk_out_note = f", {len(_dk_out_file)} DK-out filtered" if _dk_out_file else ""
            logger.info(
                f"[Cache] Pool file hit: {len(result)} players in {elapsed:.3f}s{_dk_out_note}"
            )
            if on_progress:
                on_progress("Pool loaded (cached)", 1, 1)
            return (result, []) if return_excluded else result

        # Layer 3: build from scratch
        # ──────────────────────────────────────────────────────────
        # Pre-check: quickly probe stats.nba.com connectivity.
        # If unreachable, pre-trip the circuit breaker so _process_team
        # fails fast and activates DK fallback immediately instead of
        # waiting 15-30s per API call × 5 failures to trip naturally.
        # Skip when skip_nba_api_live=True — stats.nba.com is not used
        # in the live path; probing it only poisons the circuit breaker.
        from app.config import Settings as _Settings
        if sport == "nba" and not _Settings().skip_nba_api_live:
            probe_nba_api(timeout_s=3.0)

        # Acquire a per-slate build lock so that concurrent requests
        # for the *same* slate (e.g. prewarm + SSE stream) don't
        # duplicate the expensive team rotation work.  The second
        # caller will wait, then hit the fresh memory cache above.
        with _build_locks_meta_lock:
            if cache_key not in _build_locks:
                _build_locks[cache_key] = threading.Lock()
            _slate_lock = _build_locks[cache_key]

        _lock_t0 = time.time()
        _lock_acquired = _slate_lock.acquire(blocking=True, timeout=120)
        if not _lock_acquired:
            # Could not acquire after 2 minutes — don't leave user waiting
            logger.warning(
                f"[Pool] Build lock timeout for {cache_key} "
                f"after {time.time() - _lock_t0:.1f}s — "
                "checking cache once more before fallback"
            )
        else:
            _lock_wait = time.time() - _lock_t0
            if _lock_wait > 5.0:
                logger.warning(
                    f"[Pool] Build lock for {cache_key} acquired after "
                    f"{_lock_wait:.1f}s wait (another build was in progress)"
                )

        try:
            # Re-check memory + file cache (another thread may have
            # finished while we waited on the lock)
            now = time.time()
            with _pool_lock:
                if cache_key in _pool_cache:
                    _cached_tuple2 = _pool_cache[cache_key]
                    cached_at = _cached_tuple2[0]
                    cached_pool = _cached_tuple2[1]
                    _eff_ttl2 = _effective_pool_ttl(cached_pool)
                    if now - cached_at < _eff_ttl2:
                        _teams_in_recheck = len(set(p.team_abbreviation for p in cached_pool))
                        if _teams_in_recheck < _expected_team_count:
                            logger.warning(
                                f"[Cache] Memory cache (after lock) missing teams "
                                f"({_teams_in_recheck}/{_expected_team_count}). Discarding."
                            )
                            del _pool_cache[cache_key]
                        else:
                            result = [p.copy() for p in cached_pool if p.player_id not in excluded]
                            elapsed = time.time() - t_pool_start
                            logger.info(
                                f"[Cache] Pool memory hit (after lock): {len(result)} "
                                f"players in {elapsed:.3f}s"
                            )
                            if on_progress:
                                on_progress("Pool loaded (cached)", 1, 1)
                            return (result, []) if return_excluded else result

            _file_result_2 = _load_pool_from_file(cache_key)
            file_pool_2 = None
            if _file_result_2 is not None:
                file_pool_2, _file_expected_2 = _file_result_2
                # Validate file cache against expected team count
                _teams_fp2 = set(p.team_abbreviation for p in file_pool_2)
                _fp2_missing = _expected_team_count - len(_teams_fp2)
                _min_fp2 = max(20, _expected_team_count * 5)
                if _fp2_missing > 0:
                    logger.warning(
                        f"[Cache] File cache (after lock) missing {_fp2_missing} team(s) "
                        f"({len(_teams_fp2)}/{_expected_team_count}). Discarding."
                    )
                    _path2 = _file_cache_path(cache_key)
                    if os.path.exists(_path2):
                        os.remove(_path2)
                    file_pool_2 = None
                elif len(file_pool_2) < _min_fp2:
                    logger.warning(
                        f"[Cache] File cache (after lock) suspiciously small "
                        f"({len(file_pool_2)} players). Discarding."
                    )
                    _path2 = _file_cache_path(cache_key)
                    if os.path.exists(_path2):
                        os.remove(_path2)
                    file_pool_2 = None
                else:
                    # Quality gate: fallback-only teams → shorter TTL
                    _fb2 = _get_fallback_teams(file_pool_2)
                    if _fb2:
                        _fp2_path = _file_cache_path(cache_key)
                        _fp2_age = time.time() - os.path.getmtime(_fp2_path) if os.path.exists(_fp2_path) else 0
                        if _fp2_age > _POOL_FALLBACK_TTL:
                            logger.info(
                                f"[Cache] File cache (after lock) has DK-fallback "
                                f"teams ({', '.join(_fb2)}) and age={_fp2_age:.0f}s "
                                f"> {_POOL_FALLBACK_TTL}s. Discarding."
                            )
                            if os.path.exists(_fp2_path):
                                os.remove(_fp2_path)
                            file_pool_2 = None
            if file_pool_2 is not None:
                with _pool_lock:
                    _pool_cache[cache_key] = (now, file_pool_2, _expected_team_count)
                # Re-filter against current DK injury statuses
                _dk_out_fp2 = set()
                for _d in draftables:
                    _dst = (getattr(_d, "status", "") or "").strip().upper()
                    if _dst in ("O", "OUT", "D", "DOUBTFUL"):
                        _did = getattr(_d, "dk_player_id", None)
                        if _did:
                            _dk_out_fp2.add(_did)
                result = [
                    p.copy() for p in file_pool_2
                    if p.player_id not in excluded
                    and p.player_id not in _dk_out_fp2
                ]
                elapsed = time.time() - t_pool_start
                _dk_out_note = f", {len(_dk_out_fp2)} DK-out filtered" if _dk_out_fp2 else ""
                logger.info(
                    f"[Cache] Pool file hit (after lock): {len(result)} "
                    f"players in {elapsed:.3f}s{_dk_out_note}"
                )
                if on_progress:
                    on_progress("Pool loaded (cached)", 1, 1)
                return (result, []) if return_excluded else result

            return self._build_player_pool_inner(
                platform=platform,
                draft_group_id=draft_group_id,
                game_date=gd,
                excluded=excluded,
                draftables=draftables,
                on_progress=on_progress,
                sport=sport,
                cache_key=cache_key,
                _data_svc=_data_svc,
                _game_svc=_game_svc,
                _injury_svc=_injury_svc,
                t_pool_start=t_pool_start,
                recent_weight=recent_weight,
                return_excluded=return_excluded,
                cancelled=cancelled,
            )
        finally:
            if _lock_acquired:
                _slate_lock.release()

    def _build_player_pool_inner(
        self,
        *,
        platform,
        draft_group_id,
        game_date,
        excluded,
        draftables,
        on_progress,
        sport,
        cache_key,
        _data_svc,
        _game_svc,
        _injury_svc,
        t_pool_start,
        recent_weight=None,
        return_excluded: bool = False,
        cancelled: Optional[threading.Event] = None,
    ):
        """Inner pool build — called with per-slate lock held."""
        gd = game_date

        # ── Circuit breaker health check ─────────────────────────────
        try:
            from app.services.nba_api_service import get_circuit_breaker_diagnostics
            _cb = get_circuit_breaker_diagnostics()
            if _cb["state"] != "CLOSED":
                logger.warning(
                    f"[Pool] Circuit breaker is {_cb['state']} — "
                    f"{_cb['consecutive_failures']} consecutive failures, "
                    f"opened {_cb.get('seconds_since_open', '?')}s ago. "
                    f"Teams will fail fast until breaker resets."
                )
            else:
                logger.info("[Pool] Circuit breaker: CLOSED (healthy)")
        except Exception:
            pass  # non-critical diagnostic

        # (draftables already fetched above for injury hash)

        # 2. Group draftable players by team abbreviation
        # Apply DK → NBA alias mapping early so that downstream
        # abbreviation resolution doesn't silently drop entire teams.
        teams_in_slate: Dict[str, list] = {}
        for d in draftables:
            abbr = d.team_abbreviation.upper()
            abbr = _DK_TO_NBA_ABBR_ALIASES.get(abbr, abbr)
            teams_in_slate.setdefault(abbr, []).append(d)

        logger.info(
            f"[Pool] Draftables: {len(draftables)} across "
            f"{len(teams_in_slate)} teams: "
            f"{', '.join(f'{k}({len(v)})' for k, v in sorted(teams_in_slate.items()))}"
        )

        # ── DraftGroup type validation ─────────────────────────────
        # Showdown (single-game) DraftGroups have <= 2 teams.
        if len(teams_in_slate) <= 2:
            logger.warning(
                f"[Pool] DraftGroup {draft_group_id} has only "
                f"{len(teams_in_slate)} team(s): "
                f"{sorted(teams_in_slate.keys())}. "
                f"This looks like a SHOWDOWN slate, not Classic."
            )
        elif len(teams_in_slate) <= 4:
            logger.info(
                f"[Pool] DraftGroup {draft_group_id} has "
                f"{len(teams_in_slate)} teams — small slate "
                f"(expected 6+ for main Classic)"
            )

        # ── DraftGroup gameType verification (definitive slate type) ──
        _GAME_TYPE_LABELS = {70: "Classic", 96: "Showdown", 98: "CBB Classic"}
        try:
            import httpx as _httpx
            _dg_url = f"https://api.draftkings.com/draftgroups/v1/{draft_group_id}"
            _dg_resp = _httpx.get(_dg_url, timeout=10, follow_redirects=True)
            _dg_resp.raise_for_status()
            _dg_data = _dg_resp.json().get("draftGroup", {})
            # DK API uses "gameTypeId" (not "gameType") at the top level.
            # The nested contestType object has "gameType" as a string label.
            _game_type_id = (
                _dg_data.get("gameTypeId")
                or _dg_data.get("gameType")
                or (_dg_data.get("contestType") or {}).get("contestTypeId")
            )
            _gt_label = _GAME_TYPE_LABELS.get(_game_type_id, f"Unknown({_game_type_id})")
            _dg_games = _dg_data.get("games", [])
            _dg_state = _dg_data.get("draftGroupState", "Unknown")
            logger.info(
                f"[Pool] DraftGroup {draft_group_id}: "
                f"gameTypeId={_game_type_id} ({_gt_label}), "
                f"state={_dg_state}, "
                f"games={len(_dg_games)}, sport={sport}"
            )

            # Guard: Preliminary slates have no salaries yet — draftables
            # will be empty, producing a 0-player pool.
            if _dg_state == "Preliminary":
                logger.error(
                    f"[Pool] DG {draft_group_id} is in PRELIMINARY state "
                    f"(salaries not yet published). "
                    f"Start: {_dg_data.get('minStartTime', '?')}. "
                    f"Cannot build pool — returning empty."
                )

            # Validate game type against the sport's registered Classic IDs.
            from app.sports import get_config as _get_sport_cfg
            _legal_ids = _get_sport_cfg(sport).dk_classic_game_type_ids
            if _game_type_id not in _legal_ids:
                logger.error(
                    f"[Pool] WRONG GAME TYPE: DG {draft_group_id} is "
                    f"{_gt_label} (gameTypeId={_game_type_id}), not "
                    f"{sport.upper()} Classic (legal: {_legal_ids}). "
                    f"This explains the small player pool. "
                    f"The frontend may be passing the wrong DraftGroup ID."
                )
        except Exception as _dg_err:
            logger.warning(
                f"[Pool] Could not verify DraftGroup gameType: {_dg_err}"
            )

        # Flag teams with very few draftables (incomplete DK data)
        for _ta, _dl in sorted(teams_in_slate.items()):
            if len(_dl) < 8:
                logger.warning(
                    f"[Pool][{_ta}] Only {len(_dl)} draftables "
                    f"— may indicate incomplete DK roster data"
                )

        # Build a per-team set of draftable display names so we can
        # skip deep-bench roster players who aren't on DK at all.
        # Also build a name → position map for trade detection (when a
        # DK-listed player isn't on the NBA API roster, we need their
        # position to build a PlayerMinutes for them).
        team_draftable_names: Dict[str, Set[str]] = {}
        team_draftable_positions: Dict[str, Dict[str, str]] = {}
        team_draftable_salaries: Dict[str, Dict[str, int]] = {}
        team_draftable_statuses: Dict[str, Dict[str, str]] = {}
        for abbr, dk_players in teams_in_slate.items():
            team_draftable_names[abbr] = {
                dk_p.display_name for dk_p in dk_players
            }
            team_draftable_positions[abbr] = {
                dk_p.display_name: (dk_p.position or "SF")
                for dk_p in dk_players
            }
            team_draftable_salaries[abbr] = {
                dk_p.display_name: dk_p.salary
                for dk_p in dk_players
            }
            team_draftable_statuses[abbr] = {
                dk_p.display_name: (dk_p.status or "")
                for dk_p in dk_players
            }

        # 3. Resolve team IDs from abbreviations (sport-aware)
        all_teams = _data_svc.get_all_teams()
        abbr_to_team: Dict[str, Dict] = {
            t["abbreviation"].upper(): t for t in all_teams
        }

        if sport == "cbb":
            # DraftKings and ESPN often use different abbreviations for
            # college teams.  Build a DK-abbr → ESPN-team mapping so
            # every team in the slate can be resolved.
            dk_abbrs = set(teams_in_slate.keys())
            unresolved = dk_abbrs - set(abbr_to_team.keys())
            if unresolved:
                logger.info(
                    f"[CBB] {len(unresolved)} DK abbreviations not in ESPN: "
                    f"{sorted(unresolved)}"
                )
                cbb_resolved = _resolve_cbb_abbr_map(unresolved, all_teams)
                for dk_abbr, espn_team in cbb_resolved.items():
                    abbr_to_team[dk_abbr.upper()] = espn_team
                    logger.info(
                        f"[CBB] Mapped DK '{dk_abbr}' → ESPN "
                        f"'{espn_team['abbreviation']}' "
                        f"({espn_team['full_name']})"
                    )

        total_teams = len(teams_in_slate)

        # 4. Pre-fetch shared data
        if on_progress:
            on_progress("Preparing salary data", 0, total_teams)

        salary_lookup = self.dk_draftables_service.build_salary_lookup(
            draft_group_id
        )
        injuries_full = _injury_svc.get_all_injuries()
        slot_elig = DK_SLOT_ELIGIBILITY if platform == "dk" else FD_SLOT_ELIGIBILITY

        # 4b. Build opponent lookup + pre-fetch ALL DvP matchup factors
        # Pre-fetching DvP factors here (instead of per-team in the thread
        # pool) avoids redundant calls and removes serial DvP lookups from
        # the critical path.
        opp_lookup: Dict[str, int] = {}
        dvp_cache: Dict[int, dict] = {}  # opponent_team_id → matchup factors
        if _game_svc:
            try:
                if on_progress:
                    on_progress("Fetching schedule & matchups", 0, total_teams)

                schedule = _game_svc.get_games(gd)
                _opp_ids_to_fetch: Set[int] = set()
                for g in schedule.games:
                    h_abbr = g.home_team.team_abbreviation.upper()
                    a_abbr = g.away_team.team_abbreviation.upper()
                    opp_lookup[h_abbr] = g.away_team.team_id
                    opp_lookup[a_abbr] = g.home_team.team_id
                    _opp_ids_to_fetch.add(g.home_team.team_id)
                    _opp_ids_to_fetch.add(g.away_team.team_id)

                if on_progress:
                    on_progress("Fetching DvP factors", 0, total_teams)

                # Batch-fetch DvP factors for all opponents in parallel
                t_dvp = time.time()
                def _fetch_dvp(opp_id: int):
                    try:
                        return opp_id, _game_svc.get_dvp_matchup_factors(opp_id)
                    except Exception:
                        return opp_id, None

                with ThreadPoolExecutor(max_workers=4, thread_name_prefix="dvp") as dvp_pool:
                    futures = {dvp_pool.submit(_fetch_dvp, oid): oid for oid in _opp_ids_to_fetch}
                    for future in as_completed(futures):
                        opp_id, factors = future.result()
                        if factors:
                            dvp_cache[opp_id] = factors

                logger.info(
                    f"[Pool] Pre-fetched {len(dvp_cache)} DvP factor sets "
                    f"in {time.time() - t_dvp:.1f}s"
                )
            except Exception as e:
                logger.warning(f"[Pool] Could not build opponent lookup: {e}", exc_info=True)

        if on_progress:
            on_progress("Building team rotations", 0, total_teams)

        # Flag any team abbreviations that won't resolve *before* we
        # spin up thread workers, so the warning appears before per-team
        # output and is easy to spot in logs.
        _unresolved_abbrs = [a for a in teams_in_slate if a not in abbr_to_team]
        if _unresolved_abbrs:
            logger.warning(
                f"[Pool] {len(_unresolved_abbrs)} team abbreviations unresolved: "
                f"{_unresolved_abbrs}. Available: {sorted(abbr_to_team.keys())}"
            )

        # ── 4c. SEQUENTIAL rotation pre-fetch ────────────────────────
        # BDL (BallDontLie) is the sole live data source when DB cache
        # is empty and NBA API is disabled.  BDL has a 600 RPM rate
        # limit and burst limits that cause 429 errors when 6-8 thread
        # workers hit the API simultaneously.
        #
        # Fix: pre-fetch rotations ONE TEAM AT A TIME before the
        # parallel _process_team phase.  Each successful fetch populates
        # the nba_api_service rotation cache (60 min TTL), so when the
        # parallel workers call build_team_rotation() they hit cache
        # instantly.  A small delay between teams avoids BDL bursts.
        if sport == "nba":
            _prefetch_t0 = time.time()
            _prefetch_ok = 0
            _prefetch_fail = 0
            _prefetch_cached = 0

            for _pf_idx, _pf_abbr in enumerate(sorted(teams_in_slate.keys())):
                # ── Early exit on client disconnect ──
                if cancelled and cancelled.is_set():
                    logger.info(
                        "[Pool] Client disconnected — aborting pre-fetch "
                        f"after {_pf_idx}/{total_teams} teams"
                    )
                    break

                _pf_team_info = abbr_to_team.get(_pf_abbr)
                if not _pf_team_info:
                    _alias = _DK_TO_NBA_ABBR_ALIASES.get(_pf_abbr)
                    if _alias:
                        _pf_team_info = abbr_to_team.get(_alias)
                if not _pf_team_info:
                    continue
                _pf_team_id = _pf_team_info["id"]

                # Skip if already in rotation cache
                from app.services.nba_api_service import _rotation_cache, _ROTATION_CACHE_TTL
                if _pf_team_id in _rotation_cache:
                    _cached_at, _ = _rotation_cache[_pf_team_id]
                    if time.time() - _cached_at < _ROTATION_CACHE_TTL:
                        _prefetch_ok += 1
                        _prefetch_cached += 1
                        # Still send progress so the frontend doesn't stall
                        if on_progress:
                            on_progress(
                                f"Pre-fetching rotations ({_pf_idx + 1}/{total_teams}) [cached]",
                                _pf_idx + 1, total_teams,
                            )
                        continue

                try:
                    _dk_names = team_draftable_names.get(_pf_abbr)
                    _dk_pos = team_draftable_positions.get(_pf_abbr)
                    _dk_sal = team_draftable_salaries.get(_pf_abbr)
                    _dk_sts = team_draftable_statuses.get(_pf_abbr)
                    _db_cache = getattr(_data_svc, '_db_cache', None)
                    _pf_rot = _data_svc.build_team_rotation(
                        _pf_team_id,
                        draftable_names=_dk_names,
                        cache_service=_db_cache,
                        draftable_positions=_dk_pos,
                        draftable_salaries=_dk_sal,
                        draftable_statuses=_dk_sts,
                        db_cache_only=True,  # NEVER call BDL in the live path
                    )
                    if _pf_rot:
                        _prefetch_ok += 1
                        logger.info(
                            f"[Pool][prefetch] {_pf_abbr}: "
                            f"{len(_pf_rot)} players OK"
                        )
                    else:
                        _prefetch_fail += 1
                        logger.warning(
                            f"[Pool][prefetch] {_pf_abbr}: empty rotation"
                        )
                except Exception as _pf_err:
                    _prefetch_fail += 1
                    logger.warning(
                        f"[Pool][prefetch] {_pf_abbr} failed: {_pf_err}"
                    )

                if on_progress:
                    on_progress(
                        f"Pre-fetching rotations ({_pf_idx + 1}/{total_teams})",
                        _pf_idx + 1, total_teams,
                    )

            logger.info(
                f"[Pool] Rotation pre-fetch complete (DB cache only): "
                f"{_prefetch_ok}/{total_teams} OK "
                f"({_prefetch_cached} cached), "
                f"{_prefetch_fail} failed, "
                f"{time.time() - _prefetch_t0:.1f}s"
            )

        # Pre-computed team data cache for reuse in enrichment simulation.
        # Populated by _process_team, consumed by _enrich_pool.
        _team_intermediate: Dict[str, dict] = {}
        _team_intermediate_lock = threading.Lock()

        # 5. Process teams IN PARALLEL
        def _process_team(abbr: str, dk_players: list) -> List[PlayerPoolEntry]:
            """Process a single team: rotation → projection → pool entries."""
            team_info = abbr_to_team.get(abbr)
            if not team_info:
                # Try DK → NBA alias as a last resort
                _alias = _DK_TO_NBA_ABBR_ALIASES.get(abbr)
                if _alias:
                    team_info = abbr_to_team.get(_alias)
                    if team_info:
                        logger.info(
                            f"[Pool] Resolved DK '{abbr}' → NBA '{_alias}' via alias"
                        )
            if not team_info:
                logger.warning(f"Cannot resolve team for abbr '{abbr}' (sport={sport})")
                return [], [], {"abbr": abbr, "draftables": len(dk_players), "error": "unresolved_abbr"}

            team_id = team_info["id"]
            team_name = team_info["full_name"]

            # ── DK fallback: synthetic pool entries when rotation fails ──
            def _build_fallback_entries(t_abbr, t_dk_players):
                """Build approximate pool entries from DK data alone.

                Two tiers:
                  A) DK FPPG (from DK Available Players API — not NBA)
                  B) Salary-based estimate (FP ≈ salary / 200)

                Used when build_team_rotation fails (NBA API down).
                """
                # Tier A: try DK FPPG lookup (DK API, not NBA API)
                fppg_lookup = {}
                if self.dk_available_players_service:
                    try:
                        fppg_lookup = self.dk_available_players_service.build_fppg_lookup(
                            draft_group_id
                        )
                    except Exception as _e:
                        logger.debug(f"[Fallback][{t_abbr}] DK FPPG fetch failed: {_e}")

                # Dedup dk_players by display_name — DK often has multiple
                # draftable entries per player (one per eligible roster slot).
                # Keep the first entry per name (highest salary preferred).
                _seen_names: set = set()
                _unique_dk = []
                for _dp in sorted(t_dk_players, key=lambda p: p.salary, reverse=True):
                    _dname = (_dp.display_name or "").strip()
                    if _dname and _dname not in _seen_names:
                        _seen_names.add(_dname)
                        _unique_dk.append(_dp)

                # Sort by salary descending for minutes estimation
                sorted_dk = _unique_dk  # already sorted above

                _MINUTES_BY_RANK = [
                    34.0, 33.0, 32.0, 31.0, 30.0,   # starters
                    22.0, 20.0, 16.0,                 # key bench
                    10.0, 8.0,                        # deep bench
                    4.0, 3.0, 2.0, 1.0, 0.5,         # end of bench
                ]

                fb_entries = []
                fb_excluded = []
                _n_fppg = 0
                _n_salary = 0

                for rank, dk_p in enumerate(sorted_dk):
                    # Filter: skip ruled-out / doubtful
                    dk_status = (getattr(dk_p, "status", "") or "").strip().upper()
                    if dk_status in ("O", "OUT", "D", "DOUBTFUL"):
                        _reason = "injury_out" if dk_status in ("O", "OUT") else "injury_doubtful"
                        _status_label = "Out" if dk_status in ("O", "OUT") else "Doubtful"
                        fb_excluded.append(ExcludedPlayerEntry(
                            player_id=dk_p.dk_player_id or 0,
                            player_name=dk_p.display_name,
                            display_name=dk_p.display_name,
                            position=(dk_p.position or "SF").upper(),
                            team_abbreviation=t_abbr,
                            salary=dk_p.salary,
                            injury_status=_status_label,
                            exclusion_reason=_reason,
                            exclusion_detail=f"DK status: {_status_label}",
                        ))
                        continue

                    dk_pos = (dk_p.position or "SF").upper()
                    minutes = _MINUTES_BY_RANK[rank] if rank < len(_MINUTES_BY_RANK) else 0.5

                    # Tier A: DK FPPG
                    from app.services.dk_draftables_service import _normalize_name
                    _norm = _normalize_name(dk_p.display_name)
                    # DK Available Players API often returns empty team abbr,
                    # so try both "name:TEAM" and "name:" keys.
                    dk_fppg_val = (
                        fppg_lookup.get(f"{_norm}:{t_abbr}")
                        or fppg_lookup.get(f"{_norm}:")
                    )

                    if dk_fppg_val and dk_fppg_val > 0:
                        fp = round(dk_fppg_val, 1)
                        _source = "dk_fppg"
                        _n_fppg += 1
                    else:
                        # Tier B: salary-based estimate
                        fp = round(dk_p.salary / 1000 * 5.0, 1)
                        _source = "salary_estimate"
                        _n_salary += 1

                    floor_fp = round(fp * 0.65, 1)
                    ceil_fp = round(fp * 1.45, 1)
                    _sal = dk_p.salary
                    value = round(fp / _sal * 1000, 2) if _sal > 0 else 0.0

                    eligible = self._get_eligible_slots(dk_pos, platform, sport)

                    # Position-based stat estimation
                    pos_key = dk_pos.split("/")[0]
                    rates = _POSITION_PRIOR_RATES.get(
                        pos_key, _POSITION_PRIOR_RATES.get("SF", {})
                    )
                    proj_stats = {
                        "pts": round(rates.get("PTS", 0.5) * minutes, 1),
                        "reb": round(rates.get("REB", 0.15) * minutes, 1),
                        "ast": round(rates.get("AST", 0.12) * minutes, 1),
                        "stl": round(rates.get("STL", 0.03) * minutes, 1),
                        "blk": round(rates.get("BLK", 0.02) * minutes, 1),
                        "tov": round(rates.get("TOV", 0.07) * minutes, 1),
                        "fg3m": round(rates.get("FG3M", 0.06) * minutes, 1),
                    }

                    # Injury status from DK
                    inj_status = None
                    inj_desc = None
                    if dk_status in ("Q", "QUESTIONABLE"):
                        inj_status = "Questionable"
                        inj_desc = "DraftKings status: Questionable"
                    elif dk_status in ("GTD",):
                        inj_status = "GTD"
                        inj_desc = "DraftKings status: Game Time Decision"

                    fb_entries.append(PlayerPoolEntry(
                        player_id=dk_p.dk_player_id or (hash(dk_p.display_name) & 0x7FFFFFFF),
                        player_name=dk_p.display_name,
                        display_name=dk_p.display_name,
                        position=dk_pos,
                        team_abbreviation=t_abbr,
                        salary=_sal,
                        projected_fp=fp,
                        floor_fp=floor_fp,
                        ceiling_fp=ceil_fp,
                        projected_minutes=round(minutes, 1),
                        dk_value=value,
                        eligible_slots=eligible,
                        dk_player_id=dk_p.dk_player_id,
                        projected_stats=proj_stats,
                        rotation_confidence=0.6,
                        injury_status=inj_status,
                        injury_description=inj_desc,
                        projection_source=_source,
                    ))

                logger.warning(
                    f"[Pool][{t_abbr}] FALLBACK: {len(fb_entries)} synthetic entries "
                    f"from DK draftables (fppg={_n_fppg}, salary={_n_salary}), "
                    f"{len(fb_excluded)} excluded"
                )
                return fb_entries, fb_excluded

            try:
                # Rotation + projection — pass draftable names to
                # skip deep-bench players who aren't on DK
                dk_names = team_draftable_names.get(abbr)
                dk_positions = team_draftable_positions.get(abbr)
                dk_salaries = team_draftable_salaries.get(abbr)
                dk_statuses = team_draftable_statuses.get(abbr)
                # Pass DB cache for NBA data if available
                _db_cache = getattr(_data_svc, '_db_cache', None)
                rotation = _data_svc.build_team_rotation(
                    team_id, draftable_names=dk_names,
                    cache_service=_db_cache,
                    draftable_positions=dk_positions,
                    draftable_salaries=dk_salaries,
                    draftable_statuses=dk_statuses,
                    db_cache_only=True,  # NEVER call BDL in the live path
                )
                if not rotation:
                    # Retry once immediately — no sleep needed since
                    # BDL is the sole live source (skip_nba_api_live=True)
                    # and pre-fetch should have warmed the rotation cache.
                    logger.warning(
                        f"[Pool][{abbr}] Empty rotation on first attempt "
                        f"(team_id={team_id}), retrying immediately ..."
                    )
                    rotation = _data_svc.build_team_rotation(
                        team_id, draftable_names=dk_names,
                        cache_service=_db_cache,
                        draftable_positions=dk_positions,
                        draftable_salaries=dk_salaries,
                        draftable_statuses=dk_statuses,
                        db_cache_only=True,  # NEVER call BDL in the live path
                    )
                if not rotation:
                    logger.warning(
                        f"[Pool][{abbr}] Empty rotation (DB cache miss) — "
                        f"activating DK salary fallback for all {len(dk_players)} players. "
                        f"Ensure 4 AM refresh completed successfully."
                    )
                    _fb, _fb_excl = _build_fallback_entries(abbr, dk_players)
                    return _fb, _fb_excl, {
                        "abbr": abbr, "draftables": len(dk_players),
                        "final": len(_fb), "source": "fallback",
                    }

                # ── Partial rotation guard ─────────────────────────────
                # If the rotation has very few players compared to unique
                # DK draftables, the NBA API likely returned incomplete
                # data (stale DB cache, circuit breaker, etc.).  Using a
                # tiny rotation causes most players to go through the
                # rescue path with trade-default minutes, then the 240-min
                # re-normalization crushes everyone's projections (e.g.
                # Wembanyama at 9 min instead of 33).  The full DK
                # fallback (salary-ranked minutes) is more accurate.
                _unique_dk_count = len(dk_names) if dk_names else len(dk_players)
                _MIN_ROTATION_COVERAGE = 0.40  # need at least 40% of DK names
                if _unique_dk_count > 0 and len(rotation) / _unique_dk_count < _MIN_ROTATION_COVERAGE:
                    logger.warning(
                        f"[Pool][{abbr}] Partial rotation: only {len(rotation)} "
                        f"of {_unique_dk_count} DK players ({len(rotation)/_unique_dk_count:.0%} coverage) "
                        f"— activating DK fallback to avoid crushed projections"
                    )
                    _fb, _fb_excl = _build_fallback_entries(abbr, dk_players)
                    return _fb, _fb_excl, {
                        "abbr": abbr, "draftables": len(dk_players),
                        "rotation": len(rotation),
                        "final": len(_fb), "source": "fallback_partial_rotation",
                    }

                # ── Trace dict: tracks player counts at every pipeline gate ──
                _trace = {"abbr": abbr, "draftables": len(dk_players), "rotation": len(rotation)}

                team_injuries = _injury_svc.get_team_injuries(team_name)

                # Build DK injury status dict (player_id → DK status)
                # so the rotation engine can exempt Q/GTD/Probable players
                # from auto-Out (they may be returning from injury).
                _dk_status_by_id: Dict[int, str] = {}
                _dk_status_by_name: Dict[str, str] = {}
                for _dkp in dk_players:
                    _dks = (getattr(_dkp, "status", "") or "").strip()
                    if _dks:
                        _dkn = normalize_player_name(
                            getattr(_dkp, "display_name", "") or ""
                        )
                        if _dkn:
                            _dk_status_by_name[_dkn] = _dks
                if _dk_status_by_name:
                    for _rp in rotation:
                        _rpn = normalize_player_name(_rp.player_name or "")
                        _matched_st = _dk_status_by_name.get(_rpn)
                        if _matched_st:
                            _dk_status_by_id[_rp.player_id] = _matched_st

                # ── Stamp market signals for Chalk Override ──────────
                # External ownership + confirmed starters ride on
                # PlayerMinutes into allocate_team_minutes() Phase 0d.
                if _imported_ownership or _imported_starters:
                    from app.services.dk_draftables_service import _normalize_name as _norm_fn
                    _own_stamped = 0
                    _start_stamped = 0
                    with _imported_ownership_lock:
                        _own_snapshot = dict(_imported_ownership)
                    with _imported_starters_lock:
                        _start_snapshot = dict(_imported_starters)
                    for _rp in rotation:
                        _rp_norm = _norm_fn(_rp.player_name)
                        _own_val = _own_snapshot.get(_rp_norm)
                        if _own_val is not None:
                            _rp.market_ownership = _own_val
                            _own_stamped += 1
                        if _rp_norm in _start_snapshot:
                            _rp.is_confirmed_starter = True
                            _start_stamped += 1
                    if _own_stamped or _start_stamped:
                        logger.info(
                            f"[Pool][{abbr}] Chalk signals stamped: "
                            f"{_own_stamped} ownership, {_start_stamped} starters"
                        )

                # ── DK Pricing Anomaly → Situational Starter Flag ─────
                # DK's pricing algorithms often reveal confirmed roles
                # before the news breaks.  A G-League call-up priced at
                # $4,000+ (above the $3,000 minimum) with < 5 games in
                # BDL signals DK expects significant playing time.
                #
                # Flag these players so TopDownMinutes can reserve a
                # 24-minute baseline before standard distribution.
                _DK_SITUATIONAL_SALARY_FLOOR = 3000
                _SITUATIONAL_MAX_GAMES = 5
                _sit_flagged = 0
                for _rp in rotation:
                    _dk_sal = getattr(_rp, "dk_salary", None) or 0
                    # Skip players already confirmed as starters
                    if getattr(_rp, "is_confirmed_starter", None) is True:
                        continue
                    # Condition A: DK salary strictly above minimum
                    if _dk_sal <= _DK_SITUATIONAL_SALARY_FLOOR:
                        continue
                    # Condition B: < 5 games in local BDL database
                    _games_played = (
                        sum(1 for m in _rp.minutes_last_10 if m > 0)
                        if _rp.minutes_last_10 else 0
                    )
                    if _games_played >= _SITUATIONAL_MAX_GAMES:
                        continue
                    # Both conditions met: flag as situational starter
                    _rp.is_situational_starter = True
                    _sit_flagged += 1
                    logger.info(
                        "[Pool][%s] SITUATIONAL STARTER: %s (%s) — "
                        "salary=$%s (> $%s min), games=%d (< %d) → "
                        "DK pricing implies confirmed role",
                        abbr, _rp.player_name, _rp.position,
                        f"{_dk_sal:,}", f"{_DK_SITUATIONAL_SALARY_FLOOR:,}",
                        _games_played, _SITUATIONAL_MAX_GAMES,
                    )
                if _sit_flagged:
                    logger.info(
                        f"[Pool][{abbr}] {_sit_flagged} situational starter(s) "
                        f"detected via DK pricing anomaly"
                    )

                # ── Beat Reporter NLP → News-Confirmed Starters ────────
                # Scan recent news/tweets for "will start" / "starting lineup"
                # signals that confirm a fringe player is starting tonight.
                # Sources (in priority order):
                #   1. Discord channel (Underdog NBA relay bot)
                #   2. NewsService (ESPN, NBA.com, RotoWire scrapes)
                #   3. Manually injected alerts (_manual_news_alerts)
                # This fires BEFORE Vegas props so the news signal has priority
                # when both agree.
                if hasattr(self, '_news_parser') and self._news_parser:
                    try:
                        # Source 1: Discord channel (real-time beat reporter tweets)
                        _discord_alerts = []
                        _discord_svc = getattr(self, '_discord_news_service', None)
                        if _discord_svc and _discord_svc.is_available:
                            _discord_alerts = _discord_svc.fetch_latest_alerts(limit=10)
                            if _discord_alerts:
                                logger.info(
                                    "[Pool][%s] Discord: %d alert(s) fetched",
                                    abbr, len(_discord_alerts),
                                )

                        # Source 2: NewsService (ESPN, NBA.com, RotoWire)
                        _news_alerts = []
                        if hasattr(self, '_news_service') and self._news_service:
                            _news_items = self._news_service.get_news(team_id=team_id)
                            _news_alerts = [
                                getattr(item, "headline", "") or getattr(item, "title", "")
                                for item in (_news_items or [])
                            ]

                        # Source 3: Manually injected alerts
                        _manual_alerts = getattr(self, '_manual_news_alerts', []) or []

                        # Merge all sources: Discord first (freshest), then scraped, then manual
                        _all_alerts = _discord_alerts + _news_alerts + list(_manual_alerts)

                        if _all_alerts:
                            # Build DK name lookup for this team
                            _dk_name_lookup = {
                                normalize_player_name(rp.player_name or ""): rp
                                for rp in rotation
                                if rp.player_name
                            }
                            _news_overrides = self._news_parser.scan_alerts(
                                alerts=_all_alerts,
                                dk_player_names=_dk_name_lookup,
                            )
                            if _news_overrides:
                                _news_flagged = self._news_parser.apply_overrides(
                                    overrides=_news_overrides,
                                    rotation=rotation,
                                    team_abbr=abbr,
                                )
                    except Exception as _news_exc:
                        logger.warning(
                            "[Pool][%s] News NLP scan failed: %s", abbr, _news_exc
                        )

                # ── Vegas PRA Prop → Implied Minutes Override ─────────
                # If The Odds API has a PRA prop for a fringe player
                # (salary ≤ $4,500), stamp their implied minutes onto the
                # PlayerMinutes object BEFORE TopDownMinutes runs.  This
                # ensures G-League call-ups and deep bench players with
                # zero BDL history get a real minute projection.
                if self.vegas_player_props_service and self.vegas_player_props_service.is_available:
                    try:
                        from app.services.vegas_player_props_service import (
                            calculate_implied_minutes,
                            get_synthetic_fppm,
                            VEGAS_SALARY_THRESHOLD,
                        )
                        _vegas_props = self.vegas_player_props_service.fetch_player_pra_props()
                        _vegas_stamped = 0
                        if _vegas_props:
                            for _rp in rotation:
                                _rpn = normalize_player_name(_rp.player_name or "")
                                _vp = _vegas_props.get(_rpn)
                                if not _vp:
                                    continue
                                _dk_sal = getattr(_rp, "dk_salary", None) or 0
                                if _dk_sal > VEGAS_SALARY_THRESHOLD:
                                    continue  # Only override fringe players

                                pra_line = _vp["pra_line"]
                                # Recalculate with actual DK position for accuracy
                                _dk_pos = getattr(_rp, "dk_position", "") or _rp.position
                                implied_min = calculate_implied_minutes(pra_line, _dk_pos)

                                _rp.vegas_implied_minutes = implied_min
                                _rp.vegas_confirmed = True

                                # If player has zero per-minute stats, inject
                                # synthetic FPPM so the optimizer can score them
                                _has_stats = (
                                    _rp.pts_per_min > 0
                                    or _rp.reb_per_min > 0
                                    or _rp.ast_per_min > 0
                                )
                                if not _has_stats:
                                    fppm = get_synthetic_fppm(_dk_pos)
                                    # Decompose FPPM into approximate per-min rates
                                    # PRA/min ≈ FPPM for fringe players, split:
                                    #   PTS ~45%, REB ~30%, AST ~25% of PRA
                                    _rp.pts_per_min = round(fppm * 0.45, 3)
                                    _rp.reb_per_min = round(fppm * 0.30, 3)
                                    _rp.ast_per_min = round(fppm * 0.25, 3)
                                    # Minimal defensive stats
                                    _rp.stl_per_min = 0.02
                                    _rp.blk_per_min = 0.02
                                    _rp.tov_per_min = 0.03
                                    logger.info(
                                        "[Pool][%s] VEGAS SYNTHETIC STATS: %s (%s) — "
                                        "no BDL history, injecting %.2f FPPM "
                                        "(pts=%.3f, reb=%.3f, ast=%.3f per min)",
                                        abbr, _rp.player_name, _dk_pos,
                                        fppm, _rp.pts_per_min,
                                        _rp.reb_per_min, _rp.ast_per_min,
                                    )

                                _vegas_stamped += 1
                                logger.info(
                                    "[Pool][%s] VEGAS CONFIRMED: %s (%s) — "
                                    "PRA=%.1f, implied_min=%.1f, salary=$%s",
                                    abbr, _rp.player_name, _dk_pos,
                                    pra_line, implied_min,
                                    f"{_dk_sal:,}",
                                )
                            if _vegas_stamped:
                                logger.info(
                                    f"[Pool][{abbr}] {_vegas_stamped} player(s) "
                                    f"stamped with Vegas implied minutes"
                                )
                    except Exception as e:
                        logger.warning(f"[Pool][{abbr}] Vegas props overlay failed: {e}")

                projected = self.engine.project_team_rotation(
                    team_id=team_id,
                    team_name=team_name,
                    rotation=rotation,
                    injuries=team_injuries,
                    game_date=gd,
                    all_injuries=injuries_full,
                    sport=sport,
                    recent_weight_override=recent_weight,
                    dk_injury_statuses=_dk_status_by_id if _dk_status_by_id else None,
                )
                # Log how many players were zeroed by 240-min normalization
                _zero_min = sum(
                    1 for p in projected.projections
                    if p.adjusted_minutes <= 0
                )
                _n_surviving_norm = len(projected.projections) - _zero_min
                _total_adj_min = sum(p.adjusted_minutes for p in projected.projections)
                _trace["projected"] = len(projected.projections)
                _trace["after_normalization"] = _n_surviving_norm
                if _zero_min:
                    logger.info(
                        f"[Pool][{abbr}] {_zero_min}/{len(projected.projections)} "
                        f"players zeroed by MIN_VIABLE_MINUTES"
                    )

                # Use pre-fetched DvP matchup factors (batched before team processing)
                matchup_factors = None
                opp_id = opp_lookup.get(abbr)
                if opp_id:
                    matchup_factors = dvp_cache.get(opp_id)
                self.dfs_service.project_team_dfs(
                    projected, rotation, matchup_factors=matchup_factors,
                    game_service=_game_svc,
                    opponent_team_id=opp_id,
                    sport=sport,
                )

                # Cache intermediate data for reuse in enrichment simulation
                _tim_t0 = time.time()
                with _team_intermediate_lock:
                    _team_intermediate[abbr] = {
                        "rotation": rotation,
                        "injuries": team_injuries,
                        "projected": projected,
                        "matchup_factors": matchup_factors,
                    }
                _tim_wait = time.time() - _tim_t0
                if _tim_wait > 2.0:
                    logger.warning(
                        f"[Pool][{abbr}] _team_intermediate_lock held for "
                        f"{_tim_wait:.1f}s — possible contention"
                    )

                # Build injury status lookup for this team
                # Use normalized names to handle ESPN formatting differences
                # (e.g. "D.J. Burns Jr." vs "DJ Burns")
                from app.services.rotation_engine import _normalize_for_match

                _injury_lookup: Dict[str, dict] = {}
                for inj in team_injuries:
                    _injury_lookup[_normalize_for_match(inj.player_name)] = {
                        "status": inj.status,
                        "description": inj.injury_description,
                    }
                # Also check the full list for cross-team trades
                for inj in injuries_full:
                    key = _normalize_for_match(inj.player_name)
                    if key not in _injury_lookup:
                        _injury_lookup[key] = {
                            "status": inj.status,
                            "description": inj.injury_description,
                        }

                entries: List[PlayerPoolEntry] = []
                _name_misses: List[str] = []   # Track unmatched DK names

                # Build games-played lookup from rotation data.
                # Players with very few games (e.g. two-way / G-League
                # call-ups like Yuki Kawamura) get excluded — their
                # per-game averages are unreliable and they're unlikely
                # to be active on any given night.
                _MIN_GAMES_FOR_POOL = 3  # Minimum games played this season
                _games_played_lookup: Dict[int, int] = {}
                for _rp in rotation:
                    # minutes_last_10 contains up to 10 most recent
                    # games where the player actually played (DNPs excluded).
                    _gp = len(_rp.minutes_last_10) if _rp.minutes_last_10 else 0
                    _games_played_lookup[_rp.player_id] = _gp

                # ── Filter counters for trace logging ──
                _cnt_name_matched = 0
                _cnt_zero_min = 0
                _cnt_low_games = 0
                _cnt_zero_fp = 0
                _cnt_injury = 0
                _cnt_excluded = 0
                excluded_entries: List[ExcludedPlayerEntry] = []

                # Match DK draftable → our projection
                for dk_p in dk_players:
                    match = self.dk_draftables_service.match_salary(
                        player_name=dk_p.display_name,
                        team_abbreviation=abbr,
                        lookup=salary_lookup,
                    )

                    # Find matching projection by name
                    proj = None
                    for p in projected.projections:
                        if self._names_match(
                            dk_p.display_name, p.player_name
                        ):
                            proj = p
                            break

                    if not proj:
                        _name_misses.append(dk_p.display_name)
                        excluded_entries.append(ExcludedPlayerEntry(
                            player_id=dk_p.dk_player_id or 0,
                            player_name=dk_p.display_name,
                            display_name=dk_p.display_name,
                            position=(dk_p.position or "SF").upper(),
                            team_abbreviation=abbr,
                            salary=dk_p.salary,
                            exclusion_reason="name_mismatch",
                            exclusion_detail=f"No rotation match for '{dk_p.display_name}'",
                        ))
                        continue
                    _cnt_name_matched += 1
                    if proj.adjusted_minutes <= 0:
                        _cnt_zero_min += 1
                        excluded_entries.append(ExcludedPlayerEntry(
                            player_id=proj.player_id,
                            player_name=proj.player_name,
                            display_name=dk_p.display_name,
                            position=(dk_p.position or "SF").upper(),
                            team_abbreviation=abbr,
                            salary=dk_p.salary,
                            exclusion_reason="zero_minutes",
                            exclusion_detail="Projected 0 minutes after normalization",
                            projected_minutes=0.0,
                        ))
                        continue
                    if proj.player_id in excluded:
                        _cnt_excluded += 1
                        continue

                    # ── Low-sample / two-way player filter ───────────
                    # Players with very few NBA games and minimum DK
                    # salary are likely two-way / G-League call-ups
                    # (e.g. Kawamura with 2 games at $3,000).  Their
                    # per-game averages are unreliable and they're
                    # unlikely to be active on any given night.
                    #
                    # However, recently signed replacement players
                    # (e.g. PHI signing players while PG is suspended)
                    # may also have few games but a real DK salary
                    # above minimum.  DK only assigns salaries above
                    # $3,500 to players they expect to play.  So we
                    # use salary as a signal: minimum-salary ($3,000-
                    # $3,500) + few games = likely G-League/two-way;
                    # higher salary + few games = new signing who
                    # will play.
                    _MIN_SALARY_FOR_LOW_GAMES = 3600  # Above DK min floor
                    _LOW_GAMES_MIN_PROJ = 12.0  # rotation-engine override
                    _gp = _games_played_lookup.get(proj.player_id, 0)
                    if _gp < _MIN_GAMES_FOR_POOL:
                        if dk_p.salary >= _MIN_SALARY_FOR_LOW_GAMES:
                            logger.info(
                                "Including %s despite only %d game(s) "
                                "— DK salary $%d suggests active role "
                                "(new signing / trade?)",
                                proj.player_name, _gp, dk_p.salary,
                            )
                        elif proj.adjusted_minutes >= _LOW_GAMES_MIN_PROJ:
                            # Rotation engine projects real minutes
                            # despite low salary — player is a legit
                            # rotation piece (e.g. cheap rookie starter).
                            logger.info(
                                "Including %s despite only %d game(s) "
                                "and $%d salary — rotation engine "
                                "projects %.1f min (>= %.0f threshold)",
                                proj.player_name, _gp, dk_p.salary,
                                proj.adjusted_minutes,
                                _LOW_GAMES_MIN_PROJ,
                            )
                        else:
                            logger.info(
                                "Excluding %s from pool — only %d game(s) "
                                "played + minimum salary $%d (likely "
                                "two-way/G-League)",
                                proj.player_name, _gp, dk_p.salary,
                            )
                            _cnt_low_games += 1
                            excluded_entries.append(ExcludedPlayerEntry(
                                player_id=proj.player_id,
                                player_name=proj.player_name,
                                display_name=dk_p.display_name,
                                position=(dk_p.position or "SF").upper(),
                                team_abbreviation=abbr,
                                salary=dk_p.salary,
                                exclusion_reason="low_games",
                                exclusion_detail=f"Only {_gp} game(s) + min salary ${dk_p.salary} (likely G-League/two-way)",
                                projected_minutes=round(proj.adjusted_minutes, 1),
                            ))
                            continue

                    dk_salary = dk_p.salary
                    dk_fp = proj.dk_points or 0.0
                    fd_fp = proj.fd_points or 0.0

                    if platform == "fd":
                        salary = int(round(dk_salary * FD_SALARY_RATIO, -2))
                        fp = fd_fp
                        floor_fp = proj.fd_floor or fp * 0.8
                        ceil_fp = proj.fd_ceiling or fp * 1.2
                    else:
                        salary = dk_salary
                        fp = dk_fp
                        floor_fp = proj.dk_floor or fp * 0.8
                        ceil_fp = proj.dk_ceiling or fp * 1.2

                    # Apply learned salary tier projection adjustment
                    # (corrects systematic over/under-projection by salary tier)
                    if self.calibration_service and salary > 0:
                        if salary >= 8000:
                            _tier_key = "high"
                        elif salary >= 5000:
                            _tier_key = "mid"
                        else:
                            _tier_key = "value"
                        _tier_adj = self.calibration_service.get_salary_tier_projection_adj(_tier_key)
                        if _tier_adj != 1.0:
                            fp *= _tier_adj
                            floor_fp *= _tier_adj
                            ceil_fp *= _tier_adj

                    # ── DK salary anomaly: min-salary ($3K) PLACEHOLDER
                    # players with roster_change_detected are almost
                    # certainly entries DK lists at minimum salary on a
                    # team they don't actually play for (e.g. Jaren
                    # Jackson Jr. listed on UTA at $3K post-trade).
                    # We only discount when roster_change_detected is
                    # True to avoid filtering real cheap rotation
                    # players (e.g. Justin Edwards $3K on PHI).
                    _DK_MIN_SALARY_CEILING = 3100
                    if (
                        salary <= _DK_MIN_SALARY_CEILING
                        and proj.adjusted_minutes > 8.0
                        and getattr(proj, "roster_change_detected", False)
                    ):
                        logger.warning(
                            "Salary anomaly: %s ($%d) projected %.1f min "
                            "— traded placeholder, applying 0.15x discount",
                            dk_p.display_name, salary,
                            proj.adjusted_minutes,
                        )
                        fp *= 0.15
                        floor_fp *= 0.15
                        ceil_fp *= 0.15

                    value = round(fp / salary * 1000, 2) if salary > 0 else 0.0

                    # ── Zero-FP safety net: never include a player ───
                    # with no projected production (likely zeroed by
                    # injury redistribution or bad data).
                    if fp <= 0:
                        _cnt_zero_fp += 1
                        excluded_entries.append(ExcludedPlayerEntry(
                            player_id=proj.player_id,
                            player_name=proj.player_name,
                            display_name=dk_p.display_name,
                            position=(dk_p.position or "SF").upper(),
                            team_abbreviation=abbr,
                            salary=salary,
                            exclusion_reason="zero_fp",
                            exclusion_detail="Projected 0 fantasy points after adjustments",
                            projected_minutes=round(proj.adjusted_minutes, 1),
                            projected_fp=0.0,
                        ))
                        continue

                    # ── Sparse-data FP floor: use DK FPPG when our
                    # projection is suspiciously low for a player DK
                    # expects to produce (high salary + sparse data).
                    if fp < 10.0 and salary >= 4000:
                        _pm_for_floor = None
                        for _rp in rotation:
                            if _rp.player_id == proj.player_id:
                                _pm_for_floor = _rp
                                break
                        if (
                            _pm_for_floor is not None
                            and (
                                len(_pm_for_floor.minutes_last_10)
                                + len(_pm_for_floor.minutes_last_5)
                            ) < 3
                        ):
                            _fppg_floor = None
                            if self.dk_available_players_service:
                                try:
                                    _fl = (
                                        self.dk_available_players_service
                                        .build_fppg_lookup(draft_group_id)
                                    )
                                    from app.services.dk_draftables_service import (
                                        _normalize_name,
                                    )
                                    _nk = _normalize_name(dk_p.display_name)
                                    _fppg_floor = (
                                        _fl.get(f"{_nk}:{abbr}")
                                        or _fl.get(f"{_nk}:")
                                    )
                                except Exception:
                                    pass
                            if _fppg_floor and _fppg_floor > fp:
                                logger.warning(
                                    "Sparse-data FP floor: %s FP %.1f "
                                    "-> DK FPPG %.1f (salary $%d, "
                                    "%d recent games)",
                                    dk_p.display_name, fp, _fppg_floor,
                                    salary,
                                    len(_pm_for_floor.minutes_last_10),
                                )
                                fp = round(_fppg_floor, 1)
                                floor_fp = round(fp * 0.65, 1)
                                ceil_fp = round(fp * 1.45, 1)

                    # Determine eligible slots (sport-aware for CBB)
                    dk_pos = dk_p.position.upper()
                    eligible = self._get_eligible_slots(dk_pos, platform, sport)

                    # Look up injury status for this player (normalized)
                    _inj_info = _injury_lookup.get(_normalize_for_match(proj.player_name))
                    _inj_status = _inj_info["status"] if _inj_info else None
                    _inj_desc = _inj_info["description"] if _inj_info else None

                    # ── Secondary safety net: DK's own status field ──
                    # DK uses "O" = Out, "D" = Doubtful.  If our injury
                    # service missed this player (name mismatch), DK's
                    # status is an authoritative fallback.
                    _dk_status = (dk_p.status or "").strip().upper()
                    if not _inj_status and _dk_status in ("O", "OUT"):
                        _inj_status = "Out"
                        _inj_desc = "DraftKings status: Out"
                        logger.info(
                            "DK status override: %s marked Out by DK "
                            "(not found in injury report)",
                            dk_p.display_name,
                        )
                    elif not _inj_status and _dk_status in ("D", "DOUBTFUL"):
                        _inj_status = "Doubtful"
                        _inj_desc = "DraftKings status: Doubtful"

                    # ── Skip ruled-out / doubtful players ──────────────
                    if _inj_status in ("Out", "Doubtful"):
                        logger.info(
                            "Excluding %s from pool — injury status: %s (%s)",
                            proj.player_name, _inj_status,
                            _inj_desc or "no detail",
                        )
                        _cnt_injury += 1
                        _excl_reason = "injury_out" if _inj_status == "Out" else "injury_doubtful"
                        excluded_entries.append(ExcludedPlayerEntry(
                            player_id=proj.player_id,
                            player_name=proj.player_name,
                            display_name=dk_p.display_name,
                            position=(dk_p.position or "SF").upper(),
                            team_abbreviation=abbr,
                            salary=salary,
                            injury_status=_inj_status,
                            injury_description=_inj_desc,
                            exclusion_reason=_excl_reason,
                            exclusion_detail=f"Injury: {_inj_status} — {_inj_desc or 'no detail'}",
                            projected_minutes=round(proj.adjusted_minutes, 1),
                            projected_fp=round(fp, 1),
                        ))
                        continue

                    entries.append(
                        PlayerPoolEntry(
                            player_id=proj.player_id,
                            player_name=proj.player_name,
                            display_name=dk_p.display_name or proj.player_name,
                            position=dk_pos,
                            team_abbreviation=abbr,
                            salary=salary,
                            projected_fp=round(fp, 1),
                            floor_fp=round(floor_fp, 1),
                            ceiling_fp=round(ceil_fp, 1),
                            projected_minutes=round(
                                proj.adjusted_minutes, 1
                            ),
                            dk_value=round(value, 2),
                            eligible_slots=eligible,
                            dk_player_id=dk_p.dk_player_id,
                            projected_stats=proj.projected_stats,
                            rotation_confidence=proj.confidence,
                            injury_status=_inj_status,
                            injury_description=_inj_desc,
                        )
                    )

                # ── Rescue unmatched active DK players ─────────────────
                # When new roster additions (trades, G-League call-ups,
                # free agent signings) appear on DK but aren't in our
                # rotation data, create conservative fallback entries
                # instead of dropping them.  Then re-normalize the
                # team's minutes to 240 so existing players' inflated
                # minutes are corrected.
                #
                # Without this, a team that traded away 2 starters and
                # added 4 new players would have only 9 pool entries
                # sharing 240 minutes instead of 13, massively
                # inflating the remaining players' projections.
                _cnt_rescued = 0
                _rescued_minutes = 0.0

                # Identify name_mismatch exclusions for ACTIVE DK players
                _mismatch_names = {
                    excl.player_name
                    for excl in excluded_entries
                    if excl.exclusion_reason == "name_mismatch"
                }

                if _mismatch_names:
                    # Build DK player lookup by display_name for quick access
                    _dk_by_name: Dict[str, "DKPlayerSalary"] = {}
                    for _dp in dk_players:
                        _dname = (_dp.display_name or "").strip()
                        if _dname and _dname not in _dk_by_name:
                            _dk_by_name[_dname] = _dp

                    # Lazy-load DK FPPG lookup (only when rescue needed)
                    _rescue_fppg: Dict[str, float] = {}
                    if self.dk_available_players_service:
                        try:
                            _rescue_fppg = (
                                self.dk_available_players_service
                                .build_fppg_lookup(draft_group_id)
                            )
                        except Exception as _e:
                            logger.debug(
                                f"[Rescue][{abbr}] DK FPPG fetch failed: {_e}"
                            )

                    _rescued_names: List[str] = []

                    for _mname in sorted(_mismatch_names):
                        _dk_p = _dk_by_name.get(_mname)
                        if not _dk_p:
                            continue

                        # Skip OUT / Doubtful players — they shouldn't
                        # absorb minutes even as fallback entries.
                        _dk_st = (
                            getattr(_dk_p, "status", "") or ""
                        ).strip().upper()
                        if _dk_st in ("O", "OUT", "D", "DOUBTFUL"):
                            continue

                        # Skip $3K minimum-salary placeholders UNLESS
                        # DK actually expects them to produce (FPPG > 5).
                        # JJJ on UTA at $3K after trade = placeholder (0 FPPG).
                        # Justin Edwards at $3K with 20+ FPPG = legit.
                        if (_dk_p.salary or 0) <= 3100:
                            from app.services.dk_draftables_service import (
                                _normalize_name as _resc_norm,
                            )
                            _resc_key = _resc_norm(_dk_p.display_name)
                            _resc_fppg_val = (
                                _rescue_fppg.get(f"{_resc_key}:{abbr}")
                                or _rescue_fppg.get(f"{_resc_key}:")
                                or 0.0
                            )
                            if _resc_fppg_val <= 5.0:
                                logger.info(
                                    "[Rescue][%s] Skipping $%d min-salary "
                                    "placeholder (FPPG=%.1f): %s",
                                    abbr, _dk_p.salary or 0,
                                    _resc_fppg_val,
                                    _dk_p.display_name,
                                )
                                continue
                            else:
                                logger.info(
                                    "[Rescue][%s] Allowing $%d player "
                                    "with FPPG=%.1f: %s",
                                    abbr, _dk_p.salary or 0,
                                    _resc_fppg_val,
                                    _dk_p.display_name,
                                )

                        # ── Minutes: position-based trade defaults ──
                        _r_pos = (_dk_p.position or "SF").upper()
                        _r_pos_key = _r_pos.split("/")[0]
                        _r_minutes = _TRADE_DEFAULT_MINUTES.get(
                            _r_pos_key,
                            _TRADE_DEFAULT_MINUTES.get("SF", 22.0),
                        )

                        # ── FP: DK FPPG (Tier A) or salary estimate ──
                        from app.services.dk_draftables_service import (
                            _normalize_name,
                        )
                        _r_norm = _normalize_name(_dk_p.display_name)
                        _r_fppg = (
                            _rescue_fppg.get(f"{_r_norm}:{abbr}")
                            or _rescue_fppg.get(f"{_r_norm}:")
                        )
                        _r_sal_est = round(_dk_p.salary / 1000 * 4.5, 1)
                        if _r_fppg and _r_fppg > 0:
                            # Use MAX of FPPG and salary estimate: FPPG may
                            # be stale (bench role on old team) while DK
                            # salary signals expected production tonight.
                            _r_fp = round(max(_r_fppg, _r_sal_est), 1)
                            _r_source = (
                                "dk_fppg" if _r_fppg >= _r_sal_est
                                else "salary_over_fppg"
                            )
                        else:
                            _r_fp = _r_sal_est
                            _r_source = "salary_estimate"

                        _r_floor = round(_r_fp * 0.65, 1)
                        _r_ceil = round(_r_fp * 1.55, 1)
                        _r_sal = _dk_p.salary
                        _r_value = (
                            round(_r_fp / _r_sal * 1000, 2)
                            if _r_sal > 0 else 0.0
                        )

                        # ── Stats: position-based per-minute rates ──
                        _r_rates = _POSITION_PRIOR_RATES.get(
                            _r_pos_key,
                            _POSITION_PRIOR_RATES.get("SF", {}),
                        )
                        _r_stats = {
                            "pts": round(
                                _r_rates.get("PTS", 0.5) * _r_minutes, 1
                            ),
                            "reb": round(
                                _r_rates.get("REB", 0.15) * _r_minutes, 1
                            ),
                            "ast": round(
                                _r_rates.get("AST", 0.12) * _r_minutes, 1
                            ),
                            "stl": round(
                                _r_rates.get("STL", 0.03) * _r_minutes, 1
                            ),
                            "blk": round(
                                _r_rates.get("BLK", 0.02) * _r_minutes, 1
                            ),
                            "tov": round(
                                _r_rates.get("TOV", 0.07) * _r_minutes, 1
                            ),
                            "fg3m": round(
                                _r_rates.get("FG3M", 0.06) * _r_minutes, 1
                            ),
                        }

                        # ── Injury status from DK ──
                        _r_inj = None
                        _r_inj_desc = None
                        if _dk_st in ("Q", "QUESTIONABLE"):
                            _r_inj = "Questionable"
                            _r_inj_desc = "DraftKings status: Questionable"
                        elif _dk_st in ("GTD",):
                            _r_inj = "GTD"
                            _r_inj_desc = (
                                "DraftKings status: Game Time Decision"
                            )

                        _r_eligible = self._get_eligible_slots(
                            _r_pos, platform, sport
                        )

                        entries.append(PlayerPoolEntry(
                            player_id=(
                                _dk_p.dk_player_id
                                or (hash(_dk_p.display_name) & 0x7FFFFFFF)
                            ),
                            player_name=_dk_p.display_name,
                            display_name=_dk_p.display_name,
                            position=_r_pos,
                            team_abbreviation=abbr,
                            salary=_r_sal,
                            projected_fp=_r_fp,
                            floor_fp=_r_floor,
                            ceiling_fp=_r_ceil,
                            projected_minutes=round(_r_minutes, 1),
                            dk_value=_r_value,
                            eligible_slots=_r_eligible,
                            dk_player_id=_dk_p.dk_player_id,
                            projected_stats=_r_stats,
                            rotation_confidence=0.6,
                            injury_status=_r_inj,
                            injury_description=_r_inj_desc,
                            projection_source=f"unmatched_{_r_source}",
                        ))
                        _rescued_names.append(_mname)
                        _cnt_rescued += 1
                        _rescued_minutes += _r_minutes

                    # Remove rescued players from excluded_entries
                    if _rescued_names:
                        _rescued_set = set(_rescued_names)
                        excluded_entries = [
                            e for e in excluded_entries
                            if not (
                                e.exclusion_reason == "name_mismatch"
                                and e.player_name in _rescued_set
                            )
                        ]
                        _name_misses = [
                            n for n in _name_misses
                            if n not in _rescued_set
                        ]
                        logger.info(
                            f"[Pool][{abbr}] Rescued {_cnt_rescued} "
                            f"unmatched DK players with fallback "
                            f"projections ({_rescued_minutes:.0f} min): "
                            f"{_rescued_names}"
                        )

                # ── Re-normalize team minutes to 240 ──────────────────
                # After adding rescued players, the team total may
                # exceed 240 minutes.  Scale all entries proportionally
                # so the team total is exactly 240.  This corrects the
                # inflation that occurred when the rotation engine
                # distributed 240 minutes among fewer players than will
                # actually play.
                if _cnt_rescued > 0 and entries:
                    # DK lists multiple entries per player (one per
                    # eligible roster slot, e.g. PG/SG → 2 entries).
                    # We must compute the team total on UNIQUE players
                    # only — otherwise the duplicated minutes (e.g.
                    # 800 min across 40 entries instead of 240 for 13
                    # players) cause a massive over-scaling that crushes
                    # every player's projection to ~30% of true value.
                    _seen_names: set = set()
                    _unique_total = 0.0
                    for e in entries:
                        _ename = (e.player_name or "").strip()
                        if _ename and _ename not in _seen_names:
                            _seen_names.add(_ename)
                            _unique_total += e.projected_minutes

                    _TARGET_MINUTES = 240.0
                    if _unique_total > _TARGET_MINUTES + 0.5:
                        _scale = _TARGET_MINUTES / _unique_total
                        _pct_reclaimed = (1.0 - _scale) * 100

                        for e in entries:
                            _old_min = e.projected_minutes
                            if _old_min <= 0:
                                continue
                            _new_min = round(_old_min * _scale, 1)
                            _ratio = _new_min / _old_min

                            e.projected_minutes = _new_min
                            e.projected_fp = round(
                                e.projected_fp * _ratio, 1
                            )
                            e.floor_fp = round(e.floor_fp * _ratio, 1)
                            e.ceiling_fp = round(
                                e.ceiling_fp * _ratio, 1
                            )
                            if e.projected_stats:
                                e.projected_stats = {
                                    k: round(v * _ratio, 1)
                                    for k, v in e.projected_stats.items()
                                }
                            e.dk_value = (
                                round(
                                    e.projected_fp / e.salary * 1000, 2
                                )
                                if e.salary > 0 else 0.0
                            )

                        logger.info(
                            f"[Pool][{abbr}] Re-normalized team minutes: "
                            f"{_unique_total:.0f} -> {_TARGET_MINUTES:.0f} "
                            f"({_pct_reclaimed:.1f}% reduction, "
                            f"{_cnt_rescued} rescued, "
                            f"{len(_seen_names)} unique players)"
                        )
                    else:
                        logger.info(
                            f"[Pool][{abbr}] Rescue re-normalization "
                            f"skipped: unique_total={_unique_total:.0f} "
                            f"<= {_TARGET_MINUTES:.0f} "
                            f"({len(_seen_names)} unique players, "
                            f"{_cnt_rescued} rescued)"
                        )

                # Log unmatched DK names for this team (remaining after rescue)
                if _name_misses:
                    logger.warning(
                        f"[Pool][{abbr}] {len(_name_misses)} DK names "
                        f"unmatched (OUT/Doubtful): {_name_misses[:5]}"
                        + (f" (+{len(_name_misses) - 5} more)" if len(_name_misses) > 5 else "")
                    )

                # ── Per-team trace summary ──
                logger.info(
                    f"[Pool][{abbr}] Trace: {len(dk_players)} draftables "
                    f"-> {len(rotation)} rotation -> {_n_surviving_norm} post-norm "
                    f"-> {_cnt_name_matched} matched ({len(_name_misses)} misses) "
                    f"-> {_cnt_rescued} rescued "
                    f"-> {len(entries)} final "
                    f"(zero_min={_cnt_zero_min}, low_games={_cnt_low_games}, "
                    f"zero_fp={_cnt_zero_fp}, injury={_cnt_injury}, "
                    f"excluded={_cnt_excluded})"
                )
                _trace.update({
                    "name_matched": _cnt_name_matched,
                    "name_misses": len(_name_misses),
                    "name_miss_list": _name_misses[:10],
                    "rescued": _cnt_rescued,
                    "rescued_minutes": round(_rescued_minutes, 1),
                    "zero_min": _cnt_zero_min,
                    "low_games": _cnt_low_games,
                    "zero_fp": _cnt_zero_fp,
                    "injury": _cnt_injury,
                    "excluded": _cnt_excluded,
                    "final": len(entries),
                })

                return entries, excluded_entries, _trace

            except Exception as e:
                logger.error(
                    f"Failed to build pool for team {abbr}: {e}",
                    exc_info=True,
                )
                # Fallback: build synthetic entries from DK data
                _fb, _fb_excl = _build_fallback_entries(abbr, dk_players)
                if _fb:
                    return _fb, _fb_excl, {
                        "abbr": abbr, "draftables": len(dk_players),
                        "final": len(_fb), "source": "fallback",
                        "original_error": str(e),
                    }
                return [], _fb_excl, {"abbr": abbr, "draftables": len(dk_players), "error": str(e)}

        pool: List[PlayerPoolEntry] = []
        all_excluded: List[ExcludedPlayerEntry] = []
        _all_traces: List[dict] = []
        teams_completed = 0

        # ── Bail out if client already disconnected ──
        if cancelled and cancelled.is_set():
            logger.info("[Pool] Client disconnected before parallel phase — aborting")
            if return_excluded:
                return ([], [])
            return []

        # CBB uses heavier blocking CBBpy scraping per team — cap
        # concurrent workers to avoid exhausting the process thread pool
        # and starving the async event loop (which handles health
        # checks, scoreboard, and other API requests).
        # Per-sport cap lives in the SportConfig (CBB=1 because cbbpy is
        # not thread-safe; NBA=2). Bounded above by the instance default
        # so a future bump in cfg can't accidentally exceed runtime budget.
        from app.sports import get_config as _get_sport_cfg
        _workers = min(_get_sport_cfg(sport).max_team_workers, self._MAX_TEAM_WORKERS)

        with ThreadPoolExecutor(
            max_workers=_workers,
            thread_name_prefix=f"pool-{sport}",
        ) as executor:
            future_to_abbr = {
                executor.submit(_process_team, abbr, dk_players): abbr
                for abbr, dk_players in teams_in_slate.items()
            }
            for future in as_completed(future_to_abbr):
                abbr = future_to_abbr[future]
                try:
                    result = future.result()
                    # _process_team returns (entries, excluded_entries, trace_dict)
                    if isinstance(result, tuple) and len(result) == 3:
                        entries, team_excluded, trace = result
                    elif isinstance(result, tuple) and len(result) == 2:
                        # Legacy fallback (shouldn't happen)
                        entries, trace = result
                        team_excluded = []
                    else:
                        # Defensive: in case of unexpected return
                        entries = result if isinstance(result, list) else []
                        team_excluded = []
                        trace = {"abbr": abbr, "error": "unexpected_return_type"}
                    if not isinstance(entries, list):
                        logger.error(
                            f"[Pool] Team {abbr}: entries is "
                            f"{type(entries).__name__}, not list. "
                            f"Converting to empty list."
                        )
                        entries = []
                    pool.extend(entries)
                    all_excluded.extend(team_excluded)
                    _all_traces.append(trace)
                    teams_completed += 1
                    if on_progress:
                        on_progress(
                            f"Processed {abbr}",
                            teams_completed,
                            total_teams,
                        )
                    logger.info(
                        f"Team {abbr}: {len(entries)} players "
                        f"({teams_completed}/{total_teams})"
                    )
                except Exception as e:
                    teams_completed += 1
                    _all_traces.append({"abbr": abbr, "error": str(e)})
                    logger.error(f"Team {abbr} failed: {e}", exc_info=True)

        # ── Pipeline summary across all teams ──
        _t_draftables = sum(t.get("draftables", 0) for t in _all_traces)
        _t_rotation = sum(t.get("rotation", 0) for t in _all_traces)
        _t_matched = sum(t.get("name_matched", 0) for t in _all_traces)
        _t_misses = sum(t.get("name_misses", 0) for t in _all_traces)
        _t_final = sum(t.get("final", 0) for t in _all_traces)
        _errored = [t["abbr"] for t in _all_traces if "error" in t]

        logger.info(
            f"[Pool] Summary: {_t_draftables} draftables -> {_t_rotation} rotation "
            f"-> {_t_matched} matched ({_t_misses} misses) -> {_t_final} pre-dedup "
            f"-> {len(pool)} in pool"
        )
        if _errored:
            logger.warning(f"[Pool] {len(_errored)} teams ERRORED: {_errored}")

        _fallback_teams = [t["abbr"] for t in _all_traces if t.get("source") == "fallback"]
        _fallback_total = sum(t.get("final", 0) for t in _all_traces if t.get("source") == "fallback")
        if _fallback_teams:
            logger.warning(
                f"[Pool] {len(_fallback_teams)} teams used FALLBACK projections "
                f"(NBA API unavailable): {_fallback_teams} ({_fallback_total} players)"
            )

        # Log per-team breakdown for full visibility
        for t in sorted(_all_traces, key=lambda x: x.get("abbr", "")):
            if "error" in t:
                logger.warning(
                    f"[Pool] Team {t['abbr']}: ERRORED — {t['error']} "
                    f"(draftables={t.get('draftables', '?')})"
                )

        # ── DK Roster Rule ────────────────────────────────────────
        # Final safety net: any DK-listed player who is NOT
        # Out/Doubtful and is NOT already in the pool must be
        # force-included.  This catches players who fell through
        # ALL upstream filters (trade detection failure, name
        # mismatch, sparse game logs, etc.).  Without this,
        # high-ownership players like rookies, international
        # players, and recent trades can be completely missing
        # from the optimization pool.
        from app.services.dk_draftables_service import (
            _normalize_name as _roster_norm,
        )
        _pool_names_norm: set = set()
        for _pe in pool:
            _pool_names_norm.add(_roster_norm(_pe.player_name))

        _dk_roster_rescued = 0
        _dk_roster_names: List[str] = []

        # Build FPPG lookup once (DK API, cached)
        _roster_fppg: Dict[str, float] = {}
        if self.dk_available_players_service and draft_group_id:
            try:
                _roster_fppg = (
                    self.dk_available_players_service
                    .build_fppg_lookup(draft_group_id)
                )
            except Exception as _e:
                logger.debug(f"[DK Roster Rule] FPPG fetch failed: {_e}")

        # Track which DK names we've already processed (dedup
        # by display_name since DK lists players multiple times
        # for multi-position eligibility like PG/SG).
        _dk_roster_seen: set = set()

        for _rr_abbr, _rr_dk_list in teams_in_slate.items():
            for _rr_dk in _rr_dk_list:
                _rr_name = (_rr_dk.display_name or "").strip()
                if not _rr_name:
                    continue
                _rr_name_key = _roster_norm(_rr_name)
                if _rr_name_key in _dk_roster_seen:
                    continue
                _dk_roster_seen.add(_rr_name_key)

                # Already in pool — skip
                if _rr_name_key in _pool_names_norm:
                    continue

                # Skip Out / Doubtful
                _rr_st = (
                    getattr(_rr_dk, "status", "") or ""
                ).strip().upper()
                if _rr_st in ("O", "OUT", "D", "DOUBTFUL"):
                    continue

                # ── Build fallback entry from DK data ──
                _rr_pos = (_rr_dk.position or "SF").upper()
                _rr_pos_key = _rr_pos.split("/")[0]
                _rr_sal = _rr_dk.salary or 3000

                # Minutes: salary-tier based
                if _rr_sal >= 8000:
                    _rr_min = 30.0
                elif _rr_sal >= 6000:
                    _rr_min = 26.0
                elif _rr_sal >= 4500:
                    _rr_min = 20.0
                else:
                    _rr_min = _TRADE_DEFAULT_MINUTES.get(
                        _rr_pos_key,
                        _TRADE_DEFAULT_MINUTES.get("SF", 12.0),
                    )

                # FP: DK FPPG (best) or salary estimate
                _rr_fppg = None
                if _roster_fppg:
                    _rr_fppg = (
                        _roster_fppg.get(f"{_rr_name_key}:{_rr_abbr}")
                        or _roster_fppg.get(f"{_rr_name_key}:")
                    )

                _rr_sal_est = round(_rr_sal / 1000 * 5.0, 1)
                if _rr_fppg and _rr_fppg > 0:
                    _rr_fp = round(max(_rr_fppg, _rr_sal_est), 1)
                    _rr_src = (
                        "dk_roster_rule_fppg"
                        if _rr_fppg >= _rr_sal_est
                        else "dk_roster_rule_salary"
                    )
                else:
                    _rr_fp = _rr_sal_est
                    _rr_src = "dk_roster_rule_salary"

                _rr_floor = round(_rr_fp * 0.60, 1)
                _rr_ceil = round(_rr_fp * 1.50, 1)
                _rr_value = (
                    round(_rr_fp / _rr_sal * 1000, 2)
                    if _rr_sal > 0 else 0.0
                )

                # Position-based stat estimation
                _rr_rates = _POSITION_PRIOR_RATES.get(
                    _rr_pos_key,
                    _POSITION_PRIOR_RATES.get("SF", {}),
                )
                _rr_stats = {
                    "pts": round(
                        _rr_rates.get("PTS", 0.5) * _rr_min, 1
                    ),
                    "reb": round(
                        _rr_rates.get("REB", 0.15) * _rr_min, 1
                    ),
                    "ast": round(
                        _rr_rates.get("AST", 0.12) * _rr_min, 1
                    ),
                    "stl": round(
                        _rr_rates.get("STL", 0.03) * _rr_min, 1
                    ),
                    "blk": round(
                        _rr_rates.get("BLK", 0.02) * _rr_min, 1
                    ),
                    "tov": round(
                        _rr_rates.get("TOV", 0.07) * _rr_min, 1
                    ),
                    "fg3m": round(
                        _rr_rates.get("FG3M", 0.06) * _rr_min, 1
                    ),
                }

                # Injury status from DK
                _rr_inj = None
                _rr_inj_desc = None
                if _rr_st in ("Q", "QUESTIONABLE"):
                    _rr_inj = "Questionable"
                    _rr_inj_desc = "DraftKings status: Questionable"
                elif _rr_st in ("GTD",):
                    _rr_inj = "GTD"
                    _rr_inj_desc = (
                        "DraftKings status: Game Time Decision"
                    )

                _rr_eligible = self._get_eligible_slots(
                    _rr_pos, platform, sport
                )

                pool.append(PlayerPoolEntry(
                    player_id=(
                        _rr_dk.dk_player_id
                        or (hash(_rr_name) & 0x7FFFFFFF)
                    ),
                    player_name=_rr_name,
                    display_name=_rr_name,
                    position=_rr_pos,
                    team_abbreviation=_rr_abbr,
                    salary=_rr_sal,
                    projected_fp=_rr_fp,
                    floor_fp=_rr_floor,
                    ceiling_fp=_rr_ceil,
                    projected_minutes=round(_rr_min, 1),
                    dk_value=_rr_value,
                    eligible_slots=_rr_eligible,
                    dk_player_id=_rr_dk.dk_player_id,
                    projected_stats=_rr_stats,
                    rotation_confidence=0.5,
                    injury_status=_rr_inj,
                    injury_description=_rr_inj_desc,
                    projection_source=_rr_src,
                ))
                _pool_names_norm.add(_rr_name_key)
                _dk_roster_rescued += 1
                _dk_roster_names.append(
                    f"{_rr_name} ({_rr_abbr}, ${_rr_sal:,}, "
                    f"{_rr_fp:.1f}FP, src={_rr_src})"
                )

        if _dk_roster_rescued:
            logger.warning(
                f"[Pool] DK Roster Rule: force-included "
                f"{_dk_roster_rescued} missing player(s): "
                f"{_dk_roster_names[:10]}"
                + (
                    f" (+{_dk_roster_rescued - 10} more)"
                    if _dk_roster_rescued > 10 else ""
                )
            )
        else:
            logger.info(
                "[Pool] DK Roster Rule: all DK players already in pool"
            )

        # ── CUSTOM PROJECTIONS OVERRIDE ──────────────────────────────
        # Load manual overrides from custom_projections.csv.
        # Two cases:
        #   1. Player already in pool (bad data) → override projections
        #   2. Player missing from pool → inject new entry from DK draftable
        # Uses exact DK display_name matching to bypass BDL name issues.
        _custom = _load_custom_projections()
        if _custom:
            _custom_injected = 0
            _custom_overridden = 0

            # Build flat DK draftable lookup: display_name → (DKPlayerSalary, team_abbr)
            _dk_by_name: Dict[str, tuple] = {}
            for _c_abbr, _c_dk_list in teams_in_slate.items():
                for _c_dk in _c_dk_list:
                    _c_dn = (_c_dk.display_name or "").strip()
                    if _c_dn:
                        _dk_by_name[_c_dn] = (_c_dk, _c_abbr)

            for _c_name, _c_data in _custom.items():
                # Find matching DK draftable (exact display_name match)
                _c_match = _dk_by_name.get(_c_name)
                if not _c_match:
                    logger.warning(
                        "[CustomProj] '%s' not found in DK draftables "
                        "— skipping (player must be on DK slate)",
                        _c_name,
                    )
                    continue

                _c_dk_player, _c_dk_abbr = _c_match

                # Validate team if specified in CSV
                if _c_data["team"] and _c_data["team"] != _c_dk_abbr:
                    logger.warning(
                        "[CustomProj] Team mismatch for '%s': "
                        "CSV says %s, DK says %s — using DK team",
                        _c_name, _c_data["team"], _c_dk_abbr,
                    )

                # Check if player already exists in pool
                _c_norm = _roster_norm(_c_name)
                _c_found_idx = None
                for _idx, _pe in enumerate(pool):
                    if _roster_norm(_pe.player_name) == _c_norm:
                        _c_found_idx = _idx
                        break

                if _c_found_idx is not None:
                    # Player exists but has bad data → override projections
                    _existing = pool[_c_found_idx]
                    _existing.projected_fp = _c_data["projected_fp"]
                    _existing.floor_fp = _c_data["floor_fp"]
                    _existing.ceiling_fp = _c_data["ceiling_fp"]
                    _existing.projected_minutes = _c_data["projected_minutes"]
                    _existing.rotation_confidence = 1.0
                    _existing.projection_source = "custom_csv"
                    if _existing.salary and _existing.salary > 0:
                        _existing.dk_value = round(
                            _existing.projected_fp / _existing.salary * 1000, 2
                        )
                    _custom_overridden += 1
                    logger.info(
                        "[CustomProj] Override: %s (%s, $%s) → "
                        "%.1f FP, %.0f min, %.2f FPPM",
                        _c_name, _c_dk_abbr,
                        f"{_existing.salary:,}",
                        _c_data["projected_fp"],
                        _c_data["projected_minutes"],
                        _c_data["fppm"],
                    )
                else:
                    # Player missing entirely → inject new pool entry
                    _c_sal = _c_dk_player.salary or 3000
                    _c_pos = (
                        _c_dk_player.position or _c_data["position"]
                    ).upper()
                    _c_eligible = self._get_eligible_slots(
                        _c_pos, platform, sport,
                    )
                    _c_value = round(
                        _c_data["projected_fp"] / _c_sal * 1000, 2
                    ) if _c_sal > 0 else 0.0

                    pool.append(PlayerPoolEntry(
                        player_id=_c_dk_player.dk_player_id,
                        player_name=_c_name,
                        display_name=_c_name,
                        position=_c_pos,
                        team_abbreviation=_c_dk_abbr,
                        salary=_c_sal,
                        projected_fp=_c_data["projected_fp"],
                        floor_fp=_c_data["floor_fp"],
                        ceiling_fp=_c_data["ceiling_fp"],
                        projected_minutes=_c_data["projected_minutes"],
                        dk_value=_c_value,
                        eligible_slots=_c_eligible,
                        dk_player_id=_c_dk_player.dk_player_id,
                        rotation_confidence=1.0,
                        projection_source="custom_csv",
                    ))
                    _pool_names_norm.add(_c_norm)
                    _custom_injected += 1
                    logger.info(
                        "[CustomProj] Inject: %s (%s, $%s, %s) → "
                        "%.1f FP, %.0f min, %.2f FPPM",
                        _c_name, _c_dk_abbr,
                        f"{_c_sal:,}", _c_pos,
                        _c_data["projected_fp"],
                        _c_data["projected_minutes"],
                        _c_data["fppm"],
                    )

            if _custom_injected or _custom_overridden:
                logger.info(
                    "[CustomProj] Applied %d injections + %d overrides "
                    "from custom_projections.csv",
                    _custom_injected, _custom_overridden,
                )

        # Deduplicate by player_id — a player can appear multiple
        # times when DK lists them under different positions (e.g.
        # PG and SG entries) or if they match across teams.
        # Keep the entry with the best projected_fp and merge
        # eligible_slots from all duplicate entries.
        seen: Dict[int, int] = {}  # player_id → index in deduped
        deduped: List[PlayerPoolEntry] = []
        for entry in pool:
            pid = entry.player_id
            if pid in seen:
                existing = deduped[seen[pid]]
                # Merge eligible slots
                merged_slots = list(
                    dict.fromkeys(existing.eligible_slots + entry.eligible_slots)
                )
                logger.info(
                    f"[Dedup] Merge: {entry.player_name} (pid={pid}, "
                    f"team={entry.team_abbreviation}) — "
                    f"existing FP={existing.projected_fp}, "
                    f"new FP={entry.projected_fp}, "
                    f"kept={'new' if entry.projected_fp > existing.projected_fp else 'existing'}, "
                    f"merged slots={merged_slots}"
                )
                # Keep the entry with higher projected FP
                if entry.projected_fp > existing.projected_fp:
                    entry.eligible_slots = merged_slots
                    deduped[seen[pid]] = entry
                else:
                    existing.eligible_slots = merged_slots
            else:
                seen[pid] = len(deduped)
                deduped.append(entry)

        if len(deduped) < len(pool):
            logger.info(
                f"Deduplicated player pool: {len(pool)} → {len(deduped)} "
                f"(removed {len(pool) - len(deduped)} duplicates)"
            )

        # Cross-name collision audit: detect same normalized name with
        # different player_ids (possible data bug or legitimate same-name
        # players like Marcus/Markieff Morris).
        from app.services.dk_draftables_service import (
            _normalize_name as _dk_normalize,
        )
        _name_groups: Dict[str, list] = {}
        for _e in deduped:
            _name_groups.setdefault(
                _dk_normalize(_e.player_name), []
            ).append(_e)
        for _nm, _entries in _name_groups.items():
            if len(_entries) > 1:
                logger.warning(
                    f"[Dedup] Name collision: '{_nm}' → "
                    f"{[(_e.player_id, _e.team_abbreviation) for _e in _entries]}"
                )

        # Sort by projected FP descending
        deduped.sort(key=lambda p: p.projected_fp, reverse=True)

        # ── DK OUT FAILSAFE ─────────────────────────────────────────
        # Last line of defence: if DraftKings explicitly marks a player
        # as "O" (Out) or "IR" in their draftables status, zero their
        # projected minutes and mark injury_status="Out" regardless of
        # what the BDL injury sync returned.  This guarantees ruled-out
        # players (Wendell Carter Jr., Dejounte Murray, etc.) are
        # removed by the 9-Man Guillotine even if the BDL API was
        # 429'd or returned stale data.
        #
        # Build lookup: normalized_name:TEAM → DK status
        from app.services.dk_draftables_service import (
            _normalize_name as _dk_out_norm,
        )
        _dk_out_lookup: Dict[str, str] = {}
        for _fo_abbr, _fo_dk_list in teams_in_slate.items():
            for _fo_dk in _fo_dk_list:
                _fo_st = (getattr(_fo_dk, "status", "") or "").strip().upper()
                if _fo_st in ("O", "OUT", "IR"):
                    _fo_name = _dk_out_norm(
                        getattr(_fo_dk, "display_name", "") or ""
                    )
                    if _fo_name:
                        _dk_out_lookup[f"{_fo_name}:{_fo_abbr}"] = _fo_st

        _dk_out_zeroed = 0
        _dk_out_removed = []
        if _dk_out_lookup:
            for entry in deduped:
                _ek = f"{_dk_out_norm(entry.player_name)}:{entry.team_abbreviation}"
                if _ek in _dk_out_lookup:
                    _old_fp = entry.projected_fp
                    _old_min = entry.projected_minutes
                    entry.projected_minutes = 0.0
                    entry.projected_fp = 0.0
                    entry.floor_fp = 0.0
                    entry.ceiling_fp = 0.0
                    entry.injury_status = "Out"
                    entry.injury_description = (
                        f"DK CSV Failsafe: status={_dk_out_lookup[_ek]}"
                    )
                    _dk_out_zeroed += 1
                    _dk_out_removed.append(
                        f"{entry.player_name} ({entry.team_abbreviation} "
                        f"${entry.salary:,} | was {_old_fp:.1f}fp/{_old_min:.0f}min)"
                    )

            if _dk_out_zeroed:
                logger.warning(
                    f"[DK OUT FAILSAFE] Zeroed {_dk_out_zeroed} players "
                    f"ruled Out by DraftKings: {', '.join(_dk_out_removed)}"
                )

        # Now remove zero-FP / zero-minute players from the pool so
        # the optimizer never sees them.  (The entries already exist in
        # all_excluded from the per-team phase if they were caught
        # earlier; this catches stragglers the BDL miss let through.)
        _pre_failsafe_len = len(deduped)
        deduped = [
            p for p in deduped
            if p.projected_fp > 0 and p.projected_minutes > 0
        ]
        _failsafe_removed = _pre_failsafe_len - len(deduped)
        if _failsafe_removed:
            logger.info(
                f"[DK OUT FAILSAFE] Removed {_failsafe_removed} zero-projection "
                f"players from pool ({_pre_failsafe_len} -> {len(deduped)})"
            )

        # ── Min-salary projection sanity cap ──────────────────────
        # Players at DK minimum salary ($3,000-$3,500) are typically
        # end-of-bench / not expected to play.  If our rotation
        # engine projects them at >7x FP per $1K salary (extreme
        # outlier territory), cap the projection.  This catches
        # phantom projections for traded/inactive players that the
        # rotation engine still has historical data for.
        #
        # Also: players with projected minutes < 5 and FP > 15 are
        # suspicious — very few players score 15+ FP in under 5 min.
        _MIN_SAL_THRESHOLD = 3500
        _MAX_FP_PER_K = 7.0  # 7 FP per $1K = $3K player capped at 21 FP
        _capped_count = 0
        for entry in deduped:
            if entry.salary and entry.salary <= _MIN_SAL_THRESHOLD:
                _sal_cap = (entry.salary / 1000) * _MAX_FP_PER_K
                if entry.projected_fp > _sal_cap:
                    logger.info(
                        f"[Pool] Min-salary cap: {entry.player_name} "
                        f"({entry.team_abbreviation}) "
                        f"${entry.salary:,} — {entry.projected_fp:.1f} "
                        f"-> {_sal_cap:.1f} FP "
                        f"(exceeds {_MAX_FP_PER_K}x FP/$K)"
                    )
                    _ratio = _sal_cap / entry.projected_fp
                    entry.projected_fp = round(_sal_cap, 1)
                    entry.floor_fp = round(entry.floor_fp * _ratio, 1)
                    entry.ceiling_fp = round(entry.ceiling_fp * _ratio, 1)
                    if entry.salary > 0:
                        entry.dk_value = round(
                            entry.projected_fp / entry.salary * 1000, 2
                        )
                    _capped_count += 1
        if _capped_count:
            logger.info(
                f"[Pool] Min-salary cap applied to {_capped_count} players"
            )

        # ── DK FPPG availability + placeholder check ────────────────
        # Uses DK's own FPPG data to detect phantom projections:
        #
        # Case 1: FPPG = 0 → player has no DK production history.
        #         Cap aggressively (likely not expected to play).
        #
        # Case 2: Min-salary ($3K-$3.1K) with FPPG >> salary → DK
        #         placeholder.  DK keeps historical FPPG (e.g. 37.3
        #         for Butler) but prices at $3K minimum when they know
        #         the player is OUT / won't play.  A FPPG-to-salary
        #         ratio > 4.0 at min salary is impossible for a legit
        #         rostered player — DK would price them higher.
        _dk_fppg_map: Dict[str, float] = {}
        _dk_fppg_capped = 0
        _PLACEHOLDER_SAL_THRESHOLD = 3100
        _PLACEHOLDER_FPPG_RATIO = 4.0  # FPPG / (sal/$K) above this = OUT
        if self.dk_available_players_service and draft_group_id:
            try:
                # Reset circuit breaker if needed (props failures
                # during pool build may have opened it)
                from app.services.http_resilience import (
                    _breakers, APIGroup,
                )
                _dk_cb = _breakers.get(APIGroup.DRAFTKINGS)
                if _dk_cb and _dk_cb.state != "CLOSED":
                    logger.info(
                        f"[Pool] DK circuit breaker was "
                        f"{_dk_cb.state} — resetting for FPPG "
                        f"availability check"
                    )
                    _dk_cb.reset()

                _avail_players = (
                    self.dk_available_players_service
                    .get_available_players(draft_group_id)
                )
                from app.services.dk_draftables_service import (
                    _normalize_name as _fppg_norm,
                )
                for _ap in _avail_players.values():
                    _nk = _fppg_norm(_ap.display_name)
                    _tk = _ap.team_abbreviation.upper()
                    _dk_fppg_map[f"{_nk}:{_tk}"] = _ap.dk_fppg
                    _dk_fppg_map[_nk] = _ap.dk_fppg

                for entry in deduped:
                    _enorm = _fppg_norm(entry.player_name)
                    _eteam = entry.team_abbreviation.upper()
                    _efppg = _dk_fppg_map.get(
                        f"{_enorm}:{_eteam}",
                        _dk_fppg_map.get(_enorm, -1.0),
                    )
                    if _efppg < 0:
                        continue  # Not found in DK data

                    _sal_k = (entry.salary or 3000) / 1000
                    _is_placeholder = False
                    _cap_reason = ""

                    # Case 1: FPPG = 0 → no production expected
                    if _efppg == 0.0 and entry.projected_fp > 0:
                        _is_placeholder = True
                        _cap_reason = "DK FPPG=0"

                    # Case 2: Min-salary with FPPG >> salary
                    elif (
                        _efppg > 0
                        and entry.salary
                        and entry.salary <= _PLACEHOLDER_SAL_THRESHOLD
                        and _efppg / _sal_k > _PLACEHOLDER_FPPG_RATIO
                    ):
                        _is_placeholder = True
                        _cap_reason = (
                            f"DK placeholder (FPPG={_efppg:.1f} "
                            f"at ${entry.salary:,} = "
                            f"{_efppg/_sal_k:.1f}x)"
                        )

                    if _is_placeholder:
                        _ph_cap = round(max(_sal_k * 1.5, 3.0), 1)
                        if entry.projected_fp > _ph_cap:
                            logger.warning(
                                f"[Pool] Phantom cap: "
                                f"{entry.player_name} "
                                f"({entry.team_abbreviation}) "
                                f"${entry.salary:,} — "
                                f"{entry.projected_fp:.1f} → "
                                f"{_ph_cap:.1f} FP "
                                f"({_cap_reason})"
                            )
                            _ratio = _ph_cap / entry.projected_fp
                            entry.projected_fp = _ph_cap
                            entry.floor_fp = round(
                                entry.floor_fp * _ratio, 1
                            )
                            entry.ceiling_fp = round(
                                entry.ceiling_fp * _ratio, 1
                            )
                            if entry.salary and entry.salary > 0:
                                entry.dk_value = round(
                                    entry.projected_fp
                                    / entry.salary * 1000, 2
                                )
                            _dk_fppg_capped += 1

                if _dk_fppg_capped:
                    logger.warning(
                        f"[Pool] Phantom projection cap applied to "
                        f"{_dk_fppg_capped} players"
                    )
            except Exception as _e:
                logger.warning(
                    f"[Pool] DK FPPG availability check failed: {_e}"
                )

        # ── Fallback-source projection cap ─────────────────────────
        # Fallback players (unmatched_salary_over_fppg, etc.) get
        # position-default minutes and formula-based FP.  Cap at 5x
        # FP/$K to prevent inflated projections for higher-salary
        # fallback players that escape the min-salary cap.
        _FALLBACK_MAX_FP_PER_K = 5.0
        _fallback_capped = 0
        for entry in deduped:
            _src = getattr(entry, "projection_source", None) or ""
            if _src.startswith("unmatched_") or _src == "salary_estimate":
                if entry.salary and entry.salary > 0:
                    _fb_cap = (entry.salary / 1000) * _FALLBACK_MAX_FP_PER_K
                    if entry.projected_fp > _fb_cap:
                        logger.info(
                            f"[Pool] Fallback cap: "
                            f"{entry.player_name} "
                            f"({entry.team_abbreviation}) "
                            f"${entry.salary:,} — "
                            f"{entry.projected_fp:.1f} → "
                            f"{_fb_cap:.1f} FP "
                            f"(>{_FALLBACK_MAX_FP_PER_K}x FP/$K "
                            f"for {_src})"
                        )
                        _ratio = _fb_cap / entry.projected_fp
                        entry.projected_fp = round(_fb_cap, 1)
                        entry.floor_fp = round(
                            entry.floor_fp * _ratio, 1
                        )
                        entry.ceiling_fp = round(
                            entry.ceiling_fp * _ratio, 1
                        )
                        entry.dk_value = round(
                            entry.projected_fp / entry.salary * 1000, 2
                        )
                        _fallback_capped += 1
        if _fallback_capped:
            logger.info(
                f"[Pool] Fallback source cap applied to "
                f"{_fallback_capped} players"
            )

        # ── Low-minutes projection cap ─────────────────────────────
        # Players with very few projected minutes (<10) cannot
        # realistically sustain high FP/min rates.  Cap at 1.2 FP/min
        # (elite starter pace is ~1.5 FP/min).  This catches deep
        # bench over-projections from stale season averages.
        _LOW_MIN_THRESHOLD = 10.0
        _MAX_FP_PER_MIN = 1.2
        _lowmin_capped = 0
        for entry in deduped:
            _mins = getattr(entry, "projected_minutes", 0) or 0
            if 0 < _mins < _LOW_MIN_THRESHOLD:
                _min_cap = round(_mins * _MAX_FP_PER_MIN, 1)
                if entry.projected_fp > _min_cap and _min_cap > 0:
                    logger.info(
                        f"[Pool] Low-min cap: "
                        f"{entry.player_name} "
                        f"({entry.team_abbreviation}) "
                        f"{_mins:.1f} min — "
                        f"{entry.projected_fp:.1f} → "
                        f"{_min_cap:.1f} FP "
                        f"({entry.projected_fp/_mins:.1f} → "
                        f"{_MAX_FP_PER_MIN} FP/min)"
                    )
                    _ratio = _min_cap / entry.projected_fp
                    entry.projected_fp = _min_cap
                    entry.floor_fp = round(
                        entry.floor_fp * _ratio, 1
                    )
                    entry.ceiling_fp = round(
                        entry.ceiling_fp * _ratio, 1
                    )
                    if entry.salary and entry.salary > 0:
                        entry.dk_value = round(
                            entry.projected_fp / entry.salary * 1000,
                            2,
                        )
                    _lowmin_capped += 1
        if _lowmin_capped:
            logger.info(
                f"[Pool] Low-minutes cap applied to "
                f"{_lowmin_capped} players"
            )

        # ── Apply rules-based ownership projections ───────────────
        # Uses the standalone ownership model to give every player an
        # estimated_ownership value immediately, without requiring AI
        # enrichment.  The AI ownership agent (in _enrich_pool) can
        # refine these later during lineup generation.
        try:
            num_games = len(teams_in_slate) // 2
            pool_dicts = [
                {
                    "player_id": p.player_id,
                    "player_name": p.player_name,
                    "position": p.position,
                    "salary": p.salary,
                    "projected_fp": p.projected_fp,
                    "dk_value": p.dk_value or 0,
                    "expert_sentiment": p.expert_sentiment or "",
                    "expert_signal_count": p.expert_signal_count,
                    "game_total": p.game_total,
                    "projected_minutes": p.projected_minutes,
                    "floor_fp": p.floor_fp,
                    "ceiling_fp": p.ceiling_fp,
                    "is_b2b": p.is_b2b,
                }
                for p in deduped
            ]
            # Pass learned ownership weights from calibration service
            _ownership_weights = None
            if self.calibration_service:
                _ownership_weights = self.calibration_service.get_all_ownership_weights() or None
            ownership_map = rules_project_ownership(
                pool_dicts, platform=platform, num_games=num_games,
                learned_weights=_ownership_weights,
            )
            applied = 0
            for entry in deduped:
                if entry.player_id in ownership_map:
                    entry.estimated_ownership = ownership_map[entry.player_id]
                    applied += 1
            logger.info(
                f"[Ownership] Rules-based projection applied to {applied} players"
            )
        except Exception as e:
            logger.warning(f"[Ownership] Rules-based projection failed: {e}")

        # Store team intermediate data for reuse in _enrich_pool simulation
        self._team_data_cache = _team_intermediate

        elapsed = time.time() - t_pool_start
        logger.info(
            f"Built player pool: {len(deduped)} players for "
            f"DG {draft_group_id} ({platform.upper()}) in {elapsed:.1f}s"
        )

        # ── Save to caches (full pool before exclusions) ──────────
        # Only cache if we got a reasonable number of players AND
        # all teams are represented — avoid persisting partial builds
        # that are missing entire teams (e.g. from API failures).
        _min_cache_size = max(20, total_teams * 5)
        _teams_in_pool = len(set(p.team_abbreviation for p in deduped))
        _teams_missing_from_pool = total_teams - _teams_in_pool
        if _teams_missing_from_pool > 0:
            logger.warning(
                f"[Cache] Skipping cache save — pool missing "
                f"{_teams_missing_from_pool} team(s) "
                f"({_teams_in_pool}/{total_teams} teams, "
                f"{len(deduped)} players). Partial builds are not cached."
            )
        elif len(deduped) >= _min_cache_size:
            with _pool_lock:
                _pool_cache[cache_key] = (time.time(), deduped, total_teams)
            _save_pool_to_file(cache_key, deduped, expected_teams=total_teams)
        else:
            logger.warning(
                f"[Cache] Skipping cache save — pool too small "
                f"({len(deduped)} players, need ≥{_min_cache_size})"
            )

        # Apply exclusions to returned list
        if excluded:
            deduped = [p for p in deduped if p.player_id not in excluded]

        if return_excluded:
            return deduped, all_excluded
        return deduped

    def optimize(self, request: OptimizeRequest) -> OptimizedLineup:
        """Generate an optimal lineup for the given slate and platform."""
        platform = request.platform
        mode = getattr(request, "mode", "classic")
        showdown_game_id = getattr(request, "game_id", None)

        # Determine sport early — needed for roster slot selection
        _sport = getattr(request, "sport", "nba")

        if mode == "showdown" and platform == "dk":
            salary_cap = DK_SHOWDOWN_SALARY_CAP
            roster_slots = list(DK_SHOWDOWN_SLOTS)
            slot_order = list(range(len(roster_slots)))
        else:
            salary_cap = DK_SALARY_CAP if platform == "dk" else FD_SALARY_CAP
            if platform == "dk":
                from app.sports import get_config as _get_sport_cfg
                _cfg = _get_sport_cfg(_sport)
                roster_slots = list(_cfg.dk_roster_slots)
                slot_order = list(_cfg.dk_slot_order)
            else:
                roster_slots = list(FD_ROSTER_SLOTS)
                slot_order = list(FD_SLOT_ORDER)

        # 1. Build pool (sport-aware)
        pool = self.build_player_pool(
            platform=platform,
            draft_group_id=request.draft_group_id,
            game_date=request.game_date,
            excluded_player_ids=request.excluded_players,
            sport=_sport,
            recent_weight=getattr(request, "recent_weight", None),
        )

        # In showdown mode, filter to single game
        if mode == "showdown" and showdown_game_id:
            pool = [
                p for p in pool
                if getattr(p, "game_id", None) == showdown_game_id
            ]

        if not pool:
            raise ValueError("No players available for this slate")

        # 1b. Apply user projection overrides (takes final precedence)
        pool = self._apply_overrides(pool, request.projection_overrides, sport=_sport)

        # 2. Index the slot order so duplicates get unique keys
        indexed_order = _index_slots(slot_order)

        # 3. Pre-assign locked players
        lineup: Dict[str, PlayerPoolEntry] = {}
        used_ids: Set[int] = set()
        remaining_salary = salary_cap
        remaining_slots = list(indexed_order)
        warnings: List[str] = []

        for locked_id in request.locked_players:
            player = next((p for p in pool if p.player_id == locked_id), None)
            if not player:
                warnings.append(
                    f"Locked player {locked_id} not found in pool"
                )
                continue

            # Find best slot for this player
            assigned = False
            for isl in remaining_slots:
                base = _base_slot(isl)
                elig = self._get_slot_eligible_positions(base, platform, _sport)
                if self._player_matches_slot(player.position, elig) and isl not in lineup:
                    lineup[isl] = player
                    used_ids.add(player.player_id)
                    remaining_salary -= player.salary
                    remaining_slots.remove(isl)
                    assigned = True
                    break

            if not assigned:
                warnings.append(
                    f"Could not assign locked player {player.player_name} "
                    f"to any open slot"
                )

        # 4. Build scoring function with noise for nondeterminism
        rng = random.Random(request.seed)  # None = nondeterministic
        _contest_type = getattr(request, "contest_type", "gpp")
        _is_gpp_single = _contest_type in ("gpp", "single_entry")

        def single_score_fn(p: PlayerPoolEntry) -> float:
            if _is_gpp_single:
                # GPP: blend projection with ceiling (sim_p90 or ceiling_fp)
                ceil = p.sim_p90 if p.sim_p90 else p.ceiling_fp
                return (0.5 * p.projected_fp + 0.5 * ceil) * rng.uniform(0.90, 1.10)
            return p.projected_fp * rng.uniform(0.90, 1.10)

        # 5. Optimize with quality-gate retry: ILP solver first, greedy fallback.
        #    If the initial build fails the structural quality gate, retry
        #    with different random noise up to LINEUP_QUALITY_SINGLE_MAX_RETRIES.
        from app.config.constants import LINEUP_QUALITY_SINGLE_MAX_RETRIES

        best_attempt: Optional[Dict[str, PlayerPoolEntry]] = None
        best_attempt_score: float = -1.0

        for attempt in range(LINEUP_QUALITY_SINGLE_MAX_RETRIES + 1):
            if attempt > 0:
                # Re-seed the RNG for noise variation on retries
                retry_seed = (request.seed or 0) + attempt * 7919
                rng = random.Random(retry_seed)

                def single_score_fn(p: PlayerPoolEntry, _r=rng) -> float:
                    if _is_gpp_single:
                        ceil = p.sim_p90 if p.sim_p90 else p.ceiling_fp
                        return (0.5 * p.projected_fp + 0.5 * ceil) * _r.uniform(0.90, 1.10)
                    return p.projected_fp * _r.uniform(0.90, 1.10)

                # Reset lineup state for retry
                lineup = {}
                used_ids = set()
                remaining_salary = salary_cap
                remaining_slots = list(indexed_order)
                for locked_id in request.locked_players:
                    player = next((p for p in pool if p.player_id == locked_id), None)
                    if not player:
                        continue
                    for isl in remaining_slots:
                        base = _base_slot(isl)
                        elig = self._get_slot_eligible_positions(base, platform, _sport)
                        if self._player_matches_slot(player.position, elig) and isl not in lineup:
                            lineup[isl] = player
                            used_ids.add(player.player_id)
                            remaining_salary -= player.salary
                            remaining_slots.remove(isl)
                            break

            # ── Greedy pipeline first ─────────────────────────────────
            lineup = self._greedy_fill_scored(
                pool, lineup, remaining_slots, used_ids,
                remaining_salary, platform, single_score_fn,
                sport=_sport,
            )
            # First attempt gets full iterations; retries use reduced
            # iterations since we already have a baseline and just need
            # noise-driven diversity to pass the quality gate.
            _improve_iters = 50 if attempt == 0 else 25
            lineup = self._iterative_improve_scored(
                lineup, pool, salary_cap, platform, single_score_fn,
                max_iterations=_improve_iters,
                sport=_sport,
            )
            # Two-slot swap only on first attempt — the O(pool²) cost
            # is too high to repeat across retries.
            if attempt == 0:
                lineup = self._two_slot_swap_improve(
                    lineup, pool, salary_cap, platform, single_score_fn,
                    sport=_sport,
                )

            # ── ILP refinement with warm start ────────────────────────
            if _PULP_AVAILABLE and len(lineup) == len(indexed_order):
                greedy_score = sum(single_score_fn(p) for p in lineup.values())
                try:
                    ilp_result = self._ilp_optimize(
                        pool=pool,
                        platform=platform,
                        salary_cap=salary_cap,
                        slot_order=slot_order,
                        locked_player_ids=request.locked_players,
                        score_fn=single_score_fn,
                        mode=mode,
                        sport=_sport,
                        warm_start_lineup=lineup,
                        warm_start_score=greedy_score,
                        contest_type=_contest_type,
                        time_limit=3,
                    )
                    if ilp_result and len(ilp_result) == len(indexed_order):
                        ilp_score = sum(
                            single_score_fn(p) for p in ilp_result.values()
                        )
                        if ilp_score > greedy_score:
                            logger.info(
                                f"[Hybrid] ILP improved over greedy: "
                                f"{ilp_score:.2f} > {greedy_score:.2f}"
                            )
                            lineup = ilp_result
                        else:
                            logger.debug(
                                f"[Hybrid] Greedy result retained: "
                                f"{greedy_score:.2f} >= {ilp_score:.2f}"
                            )
                except Exception as e:
                    logger.warning(f"[Hybrid] ILP refinement failed: {e}")

            # Quick quality check on the built lineup
            attempt_salary = sum(p.salary for p in lineup.values())
            attempt_teams = len(set(p.team_abbreviation for p in lineup.values()))
            attempt_fp = sum(p.projected_fp for p in lineup.values())
            attempt_score = attempt_fp * (attempt_salary / salary_cap)

            if attempt_score > best_attempt_score:
                best_attempt = dict(lineup)
                best_attempt_score = attempt_score

            # Structural pass: salary floor + team diversity
            salary_pct = attempt_salary / salary_cap if salary_cap > 0 else 0
            if salary_pct >= 0.88 and attempt_teams >= 2:
                break  # Good enough — use this attempt
            elif attempt < LINEUP_QUALITY_SINGLE_MAX_RETRIES:
                logger.info(
                    f"[Quality] Attempt {attempt + 1} failed gate "
                    f"(sal={salary_pct:.1%}, teams={attempt_teams}), retrying"
                )

        # Use the best attempt found across all retries
        lineup = best_attempt or lineup

        # 7. Build response — map indexed keys back to display slot names
        #    In showdown mode, apply CPT multiplier to the first slot.
        #    For CPT, use ceiling-weighted projection (the 1.5× magnifies upside).
        total_salary = sum(p.salary for p in lineup.values())
        total_fp = 0.0
        total_floor = 0.0
        total_ceil = 0.0

        indexed_roster = _index_slots(roster_slots)
        players = []
        for idx, isl in enumerate(indexed_roster):
            p = lineup.get(isl)
            if p:
                base = _base_slot(isl)
                is_cpt = mode == "showdown" and base == "CPT"
                mult = CPT_MULTIPLIER if is_cpt else 1.0

                if is_cpt:
                    # CPT uses ceiling-weighted projection for display FP
                    ceiling = (p.sim_p90 if hasattr(p, 'sim_p90') and p.sim_p90 else p.ceiling_fp)
                    fp = ((0.75 * ceiling + 0.25 * p.projected_fp) * mult)
                else:
                    fp = p.projected_fp * mult
                floor = p.floor_fp * mult
                ceil = p.ceiling_fp * mult
                total_fp += fp
                total_floor += floor
                total_ceil += ceil

                players.append(
                    LineupPlayer(
                        player_id=p.player_id,
                        player_name=p.player_name,
                        display_name=p.display_name or p.player_name,
                        position=p.position,
                        roster_slot=base,
                        team_abbreviation=p.team_abbreviation,
                        salary=p.salary,
                        projected_fp=round(fp, 1),
                        floor_fp=round(floor, 1),
                        ceiling_fp=round(ceil, 1),
                        projected_minutes=p.projected_minutes,
                        projected_stats=p.projected_stats,
                        dk_player_id=p.dk_player_id,
                    )
                )

        result = OptimizedLineup(
            platform=platform,
            sport=_sport,
            players=players,
            total_salary=total_salary,
            salary_remaining=salary_cap - total_salary,
            total_projected_fp=round(total_fp, 1),
            total_floor_fp=round(total_floor, 1),
            total_ceiling_fp=round(total_ceil, 1),
            salary_cap=salary_cap,
            roster_slots=roster_slots,
            warnings=warnings,
        )

        # Attach quality assessment
        q_score, q_grade, q_warnings = self._assess_lineup_quality(
            result, salary_cap, pool=pool,
        )
        result.quality_score = q_score
        result.quality_grade = q_grade
        if q_warnings:
            result.warnings.extend(q_warnings)

        return result

    # ------------------------------------------------------------------
    # Pool enrichment
    # ------------------------------------------------------------------

    def _enrich_pool(
        self,
        pool: List[PlayerPoolEntry],
        platform: str,
        game_date: str,
        contest_type: str = "gpp",
        sport: str = "nba",
        draft_group_id: Optional[int] = None,
    ) -> List[PlayerPoolEntry]:
        """Attach simulation, expert-signal, and game-context data.

        Enrichment is best-effort — any source that fails is logged and
        skipped so the optimizer can still run with base projections.

        Phases are parallelized into tiers for speed:
          Tier 1 (parallel): game context, expert signals, sim tuning, news
          Tier 2 (sequential): simulation percentiles (needs Tier 1 results)
          Tier 3 (parallel): ownership projection, strategy adjustments
        """
        t_enrich_start = time.time()

        # Resolve sport-aware game service. NBA + CBB are wired in
        # via constructor injection; MLB / NFL are looked up from the
        # service container by sport key (Prompt 7.13). Without this
        # branch ``_enrich_pool`` would fall back to ``self.game_service``
        # (NBA) for MLB/NFL — the schedule lookup would return zero
        # games, ``game_lookup`` would be empty, no game_id would
        # attach to pool entries, and ``_get_stackable_game_pool``
        # would see "0 stackable games" → ILP infeasible → 0 lineups
        # generated. (Symptom: "Generated 0 of N requested lineups
        # after 6 generation rounds" on MLB or NFL.)
        _game_svc = self.game_service
        if sport == "cbb" and self.cbb_game_service:
            _game_svc = self.cbb_game_service
        elif sport in ("mlb", "nfl"):
            try:
                from app.api.dependencies import get_services
                _resolved = get_services().get_game_service(sport)
                if _resolved is not None:
                    _game_svc = _resolved
            except Exception as _svc_exc:
                logger.warning(
                    "[Enrich] Could not resolve %s game service: %s — "
                    "falling back to NBA service (game_lookup will be empty)",
                    sport.upper(), _svc_exc,
                )

        # ══════════════════════════════════════════════════════════════
        # TIER 1 — Independent data fetching (run in parallel)
        # ══════════════════════════════════════════════════════════════
        game_lookup: Dict[str, dict] = {}
        b2b_teams: Set[str] = set()
        _noise_overrides: Optional[Dict[int, Dict[str, float]]] = None
        _expert_signals = None
        _news_items = None

        def _fetch_game_context():
            """Tier 1a: Game context + B2B detection."""
            nonlocal game_lookup, b2b_teams
            if not _game_svc:
                return
            try:
                schedule = _game_svc.get_games(game_date)
                for g in schedule.games:
                    home_abbr = g.home_team.team_abbreviation.upper()
                    away_abbr = g.away_team.team_abbreviation.upper()
                    game_lookup[home_abbr] = {
                        "pace": g.projected_pace,
                        "total": g.projected_total,
                        "opp_def": g.away_team.season_def_rating,
                        "game_info": g,
                        "game_id": getattr(g, "game_id", None) or f"{home_abbr}_{away_abbr}",
                        "opponent": away_abbr,
                    }
                    game_lookup[away_abbr] = {
                        "pace": g.projected_pace,
                        "total": g.projected_total,
                        "opp_def": g.home_team.season_def_rating,
                        "game_info": g,
                        "game_id": getattr(g, "game_id", None) or f"{home_abbr}_{away_abbr}",
                        "opponent": home_abbr,
                    }
                # ── Overlay BDL live odds onto GameInfo ──────────
                # Skip when OddsFetcherService already resolved odds
                # inside get_games() — avoids duplicate BDL API calls
                # and lock contention.
                _odds_already_resolved = (
                    hasattr(_game_svc, "_odds_fetcher")
                    and _game_svc._odds_fetcher is not None
                )
                if (
                    not _odds_already_resolved
                    and self.line_movement_agent
                    and hasattr(self.line_movement_agent, 'fetch_live_odds')
                ):
                    try:
                        bdl_odds = self.line_movement_agent.fetch_live_odds(
                            game_date
                        )
                        _overlaid = 0
                        for team_abbr, odds_entry in bdl_odds.items():
                            ctx = game_lookup.get(team_abbr)
                            if not ctx:
                                continue
                            g_info = ctx.get("game_info")
                            if not g_info:
                                continue
                            bdl_spread = odds_entry.get("spread")
                            if bdl_spread is not None:
                                g_info.vegas_spread = float(bdl_spread)
                            bdl_ou = odds_entry.get("over_under")
                            if bdl_ou is not None:
                                g_info.over_under = float(bdl_ou)
                            _overlaid += 1
                        if _overlaid:
                            logger.info(
                                f"[Enrich] BDL live odds overlaid on "
                                f"{_overlaid} team entries"
                            )
                    except Exception as e:
                        logger.warning(
                            f"[Enrich] BDL odds overlay failed: {e}"
                        )

                # ── Compute game context modifiers from live odds ──
                if self.line_movement_agent and hasattr(
                    self.line_movement_agent, "compute_game_context_modifier_for_game"
                ):
                    try:
                        _modifiers_attached = 0
                        _seen_game_ids: set = set()
                        for team_abbr, ctx in game_lookup.items():
                            g_info = ctx.get("game_info")
                            if not g_info:
                                continue
                            # Deduplicate: each game has two team entries
                            gid = getattr(g_info, "game_id", None)
                            if gid and gid in _seen_game_ids:
                                continue  # modifier already attached via the other team
                            if (
                                g_info.vegas_spread is not None
                                and g_info.over_under is not None
                            ):
                                modifier = self.line_movement_agent.compute_game_context_modifier_for_game(
                                    spread=g_info.vegas_spread,
                                    over_under=g_info.over_under,
                                    home_team=g_info.home_team.team_abbreviation,
                                    away_team=g_info.away_team.team_abbreviation,
                                )
                                g_info.context_modifier = modifier
                                _modifiers_attached += 1
                                if gid:
                                    _seen_game_ids.add(gid)
                        if _modifiers_attached:
                            logger.info(
                                f"[Enrich] Game context modifiers attached "
                                f"to {_modifiers_attached} games"
                            )
                    except Exception as e:
                        logger.warning(
                            f"[Enrich] Game context modifier computation "
                            f"failed: {e}"
                        )

                # B2B check
                try:
                    gd = date.fromisoformat(game_date)
                    yesterday = (gd - timedelta(days=1)).isoformat()
                    for abbr in game_lookup:
                        for g in schedule.games:
                            h_abbr = g.home_team.team_abbreviation.upper()
                            a_abbr = g.away_team.team_abbreviation.upper()
                            if abbr in (h_abbr, a_abbr):
                                team_id = (
                                    g.home_team.team_id
                                    if abbr == h_abbr
                                    else g.away_team.team_id
                                )
                                if _game_svc.has_game_on_date(
                                    team_id, yesterday
                                ):
                                    b2b_teams.add(abbr)
                                break
                except Exception as e:
                    logger.warning(f"[Enrich] B2B check failed: {e}")
                logger.info(
                    f"[Enrich] Game context: {len(game_lookup)} teams "
                    f"({len(b2b_teams)} on B2B)"
                )
                # Cache for sim-optimal access (game_lookup is function-local)
                self._game_lookup_cache = game_lookup
            except Exception as e:
                logger.warning(f"[Enrich] Game context failed: {e}")

        def _fetch_expert_signals():
            """Tier 1b: Expert signals."""
            nonlocal _expert_signals
            if not self.expert_signal_service:
                return
            try:
                player_names = [p.player_name for p in pool]
                signals_resp = self.expert_signal_service.get_signals(
                    player_names=player_names, limit=100
                )
                _expert_signals = signals_resp.signals
                logger.info(f"[Enrich] Fetched {len(_expert_signals)} expert signals")
            except Exception as e:
                logger.warning(f"[Enrich] Expert signals fetch failed: {e}")

        def _fetch_sim_tuning():
            """Tier 1c: AI noise profiles via direct Anthropic SDK call.

            Calls claude-3-5-haiku-latest with a strict 10s timeout to
            classify the top-60 players by archetype and assign per-player
            variance multipliers (std_dev, ceiling, floor).  Results are
            stored on pool entries AND converted to per-stat sigma format
            for the simulation engine.

            On failure (APIError, timeout, JSON parse), logs a warning and
            leaves _noise_overrides as None — the caller's fallback path
            will assign salary-tier heuristics.
            """
            nonlocal _noise_overrides
            from app.services.simulation_engine import STAT_NOISE_SIGMA, STAT_ORDER

            # ── Build top-60 payload sorted by projected FP ───────────
            top_60 = sorted(pool, key=lambda p: p.projected_fp, reverse=True)[:60]
            if not top_60:
                return

            payload_lines = []
            for p in top_60:
                payload_lines.append(
                    f"ID:{p.player_id} | {p.player_name} | "
                    f"${p.salary:,} | Proj FP: {p.projected_fp:.1f}"
                )
            user_prompt = (
                f"Slate date: {game_date}, Platform: {platform}\n\n"
                + "\n".join(payload_lines)
            )

            _SIM_TUNING_SYSTEM_PROMPT = (
                "You are an expert NBA DFS Quantitative Analyst. "
                "Assign a mathematical Noise Profile to each player. "
                "Return exactly four fields per player: archetype, "
                "std_dev_multiplier, ceiling_multiplier, floor_multiplier. "
                "Guidelines: Safe stars: std_dev 0.14-0.16, floor 0.6-0.7. "
                "Volatile scorers: std_dev 0.20-0.25, ceiling 1.7-2.0. "
                "OUTPUT STRICTLY AS A MINIFIED JSON ARRAY."
            )

            # ── Call Anthropic SDK directly with strict timeout ────────
            try:
                from anthropic import Anthropic, APIError, APITimeoutError

                client = Anthropic(timeout=10.0)
                response = client.messages.create(
                    model="claude-3-5-haiku-latest",
                    max_tokens=4096,
                    temperature=0.2,
                    system=_SIM_TUNING_SYSTEM_PROMPT,
                    messages=[{"role": "user", "content": user_prompt}],
                )
                raw_text = ""
                for block in response.content:
                    if hasattr(block, "text"):
                        raw_text += block.text

                logger.info(
                    f"[Enrich] Sim tuning API call complete "
                    f"({response.usage.input_tokens}in/"
                    f"{response.usage.output_tokens}out tokens)"
                )

                # ── Safe JSON parsing: strip markdown fences ──────────
                content = raw_text.strip()
                if content.startswith("```"):
                    lines = content.split("\n")
                    lines = [
                        ln for ln in lines
                        if not ln.strip().startswith("```")
                    ]
                    content = "\n".join(lines).strip()

                profiles = json.loads(content)
                if not isinstance(profiles, list):
                    logger.warning(
                        "[Enrich] Sim tuning: expected JSON array, "
                        f"got {type(profiles).__name__}"
                    )
                    raise json.JSONDecodeError(
                        "Expected array", content, 0
                    )

                # ── Map results back to pool entries ──────────────────
                # Build lookup by player_id (AI returns string IDs)
                profile_map: Dict[int, dict] = {}
                for prof in profiles:
                    try:
                        pid = int(prof.get("player_id", 0))
                    except (ValueError, TypeError):
                        continue
                    if pid > 0:
                        profile_map[pid] = prof

                # Reference std_dev for converting player-level multiplier
                # to per-stat sigmas.  0.18 is the empirical mid-range
                # (guards 0.18, bigs 0.15, bench 0.25 → average ≈ 0.18).
                _BASELINE_STD_DEV = 0.18

                applied = 0
                noise_map: Dict[int, Dict[str, float]] = {}
                for entry in pool:
                    prof = profile_map.get(entry.player_id)
                    if not prof:
                        continue

                    # Store raw multipliers on pool entry
                    _arch = prof.get("archetype")
                    _std = prof.get("std_dev_multiplier")
                    _ceil = prof.get("ceiling_multiplier")
                    _floor = prof.get("floor_multiplier")

                    if _arch:
                        entry.noise_archetype = str(_arch)
                    if isinstance(_std, (int, float)) and 0.05 <= _std <= 0.50:
                        entry.std_dev_multiplier = round(float(_std), 4)
                    if isinstance(_ceil, (int, float)) and 1.0 <= _ceil <= 3.0:
                        entry.ceiling_multiplier = round(float(_ceil), 4)
                    if isinstance(_floor, (int, float)) and 0.0 <= _floor <= 1.0:
                        entry.floor_multiplier = round(float(_floor), 4)

                    # Convert to per-stat sigma for simulation engine
                    if entry.std_dev_multiplier:
                        scale = entry.std_dev_multiplier / _BASELINE_STD_DEV
                        sigmas = {
                            stat: max(0.05, min(0.60, STAT_NOISE_SIGMA.get(stat, 0.15) * scale))
                            for stat in STAT_ORDER
                        }
                        noise_map[entry.player_id] = sigmas
                        applied += 1

                if noise_map:
                    _noise_overrides = noise_map
                    logger.info(
                        f"[Enrich] Sim tuning: {applied} player noise profiles "
                        f"from {len(profile_map)} AI responses"
                    )
                else:
                    logger.warning(
                        "[Enrich] Sim tuning: AI returned profiles but "
                        "none mapped to pool entries"
                    )

            except ImportError:
                logger.warning(
                    "[Enrich] Sim tuning: anthropic package not installed"
                )
            except (APIError, APITimeoutError) as api_err:
                logger.warning(
                    f"[Enrich] Sim tuning API failed: {api_err}"
                )
            except json.JSONDecodeError as jde:
                logger.warning(
                    f"[Enrich] Sim tuning JSON parse failed: {jde}"
                )
            except Exception as e:
                logger.warning(f"[Enrich] Sim tuning unexpected error: {e}")

            # ── Fallback chain ────────────────────────────────────────
            # 1. If AI failed, try the deterministic SimulationTuningAgent
            #    (rule-based role classification — no API call needed).
            if not _noise_overrides and self.simulation_tuning_agent:
                try:
                    players_ctx = [
                        {
                            "player_id": p.player_id,
                            "player_name": p.player_name,
                            "position": p.position,
                            "salary": p.salary,
                            "projected_fp": p.projected_fp,
                            "floor_fp": p.floor_fp,
                            "ceiling_fp": p.ceiling_fp,
                            "projected_minutes": p.projected_minutes,
                            "is_b2b": p.is_b2b,
                            "injury_status": p.injury_status,
                            "vegas_spread": p.vegas_spread,
                        }
                        for p in top_60
                    ]
                    from app.services.agents.simulation_tuning_agent import (
                        classify_player_roles,
                        compute_role_sigma_profiles,
                    )
                    role_map = classify_player_roles(players_ctx)
                    if role_map:
                        _noise_overrides = compute_role_sigma_profiles(role_map)
                        logger.info(
                            f"[Enrich] Sim tuning fallback (deterministic): "
                            f"{len(_noise_overrides)} profiles from role classification"
                        )
                except Exception as e:
                    logger.warning(
                        f"[Enrich] Sim tuning deterministic fallback failed: {e}"
                    )

            # 2. If deterministic also failed, try calibration-based overrides.
            if not _noise_overrides and self.calibration_service:
                cal_noise = self.calibration_service.get_noise_overrides()
                if cal_noise:
                    global_sigmas = {
                        stat: STAT_NOISE_SIGMA.get(stat, 0.15) * mult
                        for stat, mult in cal_noise.items()
                    }
                    _noise_overrides = {
                        p.player_id: global_sigmas
                        for p in top_60
                    }
                    logger.info(
                        f"[Enrich] Calibration noise overrides: "
                        f"{len(cal_noise)} stat adjustments applied globally"
                    )

        def _fetch_news():
            """Tier 1d: Fetch news items (processing happens later)."""
            nonlocal _news_items
            if not (self.news_projection_agent and self.news_projection_agent.is_available):
                return
            try:
                from app.services.news_service import NewsService
                _news = NewsService()
                items, _ = _news.get_news(limit=30)
                _news_items = items
                logger.info(f"[Enrich] Fetched {len(items or [])} news items")
            except Exception as e:
                logger.warning(f"[Enrich] News fetch failed: {e}")

        # ── Tier 1e/1f: DK Sportsbook props + FPPG ──────────────────
        _props_lookup: Dict[str, object] = {}
        _fppg_lookup: Dict[str, float] = {}

        def _fetch_dk_props():
            """Tier 1e: DK Sportsbook player prop lines."""
            nonlocal _props_lookup
            if not self.dk_props_service:
                return
            try:
                _props_lookup = self.dk_props_service.get_player_props(game_date)
                logger.info(
                    f"[Enrich] DK props: {len(_props_lookup)} players"
                )
            except Exception as e:
                logger.warning(f"[Enrich] DK props fetch failed: {e}")

        def _fetch_dk_fppg():
            """Tier 1f: DK Available Players FPPG."""
            nonlocal _fppg_lookup
            if not self.dk_available_players_service:
                return
            try:
                # Reset the DraftKings circuit breaker before this critical
                # fetch.  During pool build, DK props or other DK API calls
                # may have tripped the breaker (transient 429s / timeouts),
                # but FPPG data is essential for projection correction and
                # must not be blocked by earlier transient failures.
                from app.services.http_resilience import (
                    _breakers, APIGroup,
                )
                _dk_breaker = _breakers.get(APIGroup.DRAFTKINGS)
                if _dk_breaker and _dk_breaker.state != "CLOSED":
                    logger.info(
                        f"[Enrich] DK circuit breaker was {_dk_breaker.state}"
                        f" — resetting for FPPG fetch"
                    )
                    _dk_breaker.reset()

                # Use the draft_group_id passed from generate_lineups.
                # Previously this tried to discover the DG ID from the
                # draftables cache, which is empty when the pool loads
                # from file cache (skipping the draftables API call).
                _dg_id = draft_group_id
                if not _dg_id:
                    # Fallback: try draftables cache keys
                    from app.api.dependencies import get_services
                    svc = get_services()
                    for _dg_id in svc.dk_draftables_service._cache.keys():
                        break
                    else:
                        logger.warning(
                            "[Enrich] DK FPPG: no draft_group_id available"
                        )
                        return
                _fppg_lookup = self.dk_available_players_service.build_fppg_lookup(_dg_id)
                if _fppg_lookup:
                    logger.info(
                        f"[Enrich] DK FPPG: {len(_fppg_lookup)} players "
                        f"from DG {_dg_id}"
                    )
            except Exception as e:
                logger.warning(f"[Enrich] DK FPPG fetch failed: {e}")

        # Run Tier 1 in parallel with a hard timeout so unreachable
        # external services (stats.nba.com, AI, Twitter) don't stall
        # the entire lineup generation pipeline.
        # NOTE: We use shutdown(wait=False) to avoid blocking on slow
        # threads — the ThreadPoolExecutor context manager's __exit__
        # calls shutdown(wait=True) which would negate the timeout.
        from app.config.constants import ENRICH_TIER1_TIMEOUT_S

        _t1_pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="enrich-t1")
        futures = [
            _t1_pool.submit(_fetch_game_context),
            _t1_pool.submit(_fetch_expert_signals),
            _t1_pool.submit(_fetch_sim_tuning),
            _t1_pool.submit(_fetch_news),
            _t1_pool.submit(_fetch_dk_props),
            _t1_pool.submit(_fetch_dk_fppg),
        ]
        # Wait for game context (futures[0]) with extra grace since
        # stacking depends on game_id being populated.
        _game_ctx_future = futures[0]
        _other_futures = futures[1:]

        done, not_done = _futures_wait(
            futures, timeout=ENRICH_TIER1_TIMEOUT_S,
        )
        for f in done:
            try:
                f.result()
            except Exception as e:
                logger.warning(f"[Enrich] Tier 1 task failed: {e}")
        if not_done:
            # If game context specifically timed out, give it extra time
            # (stacking is completely broken without game_id)
            if _game_ctx_future in not_done:
                logger.warning(
                    f"[Enrich] Game context still running — "
                    f"waiting up to 30s extra (stacking needs game_id)"
                )
                _gc_done, _ = _futures_wait(
                    [_game_ctx_future], timeout=30.0,
                )
                if _gc_done:
                    try:
                        _game_ctx_future.result()
                    except Exception as e:
                        logger.warning(
                            f"[Enrich] Game context failed: {e}"
                        )
                    not_done = not_done - _gc_done
                else:
                    logger.warning(
                        "[Enrich] Game context still not done after "
                        "extra wait — stacking will be disabled"
                    )
            if not_done:
                logger.warning(
                    f"[Enrich] Tier 1 timeout: {len(not_done)}/{len(futures)} "
                    f"tasks still running after {ENRICH_TIER1_TIMEOUT_S}s — "
                    f"proceeding with partial enrichment"
                )
            for f in not_done:
                f.cancel()
        _t1_pool.shutdown(wait=False)

        t1_elapsed = time.time() - t_enrich_start
        logger.info(f"[Enrich] Tier 1 completed in {t1_elapsed:.1f}s")

        # ── Apply Tier 1 results to pool ───────────────────────────
        # Game context
        for entry in pool:
            abbr = entry.team_abbreviation.upper()
            ctx = game_lookup.get(abbr)
            if ctx:
                entry.game_pace = ctx["pace"]
                entry.game_total = ctx["total"]
                entry.opponent_def_rating = ctx["opp_def"]
                entry.is_b2b = abbr in b2b_teams
                entry.game_id = ctx.get("game_id")
                entry.opponent_abbreviation = ctx.get("opponent")
                _gi = ctx.get("game_info")
                if _gi:
                    entry.game_commence_time = getattr(
                        _gi, "game_time_et", None
                    )
                    # Improvement #1: Implied team total from GameInfo
                    # (projected_home_score / projected_away_score already
                    # computed with 80/20 Vegas blend in game_service.py)
                    is_home = (
                        abbr
                        == getattr(
                            getattr(_gi, "home_team", None),
                            "team_abbreviation", "",
                        ).upper()
                    )
                    if is_home:
                        entry.implied_team_total = getattr(
                            _gi, "projected_home_score", None
                        )
                    else:
                        entry.implied_team_total = getattr(
                            _gi, "projected_away_score", None
                        )
                    # Also populate vegas_spread on pool entry
                    _raw_spread = getattr(_gi, "vegas_spread", None)
                    if _raw_spread is None:
                        _raw_spread = getattr(
                            _gi, "projected_spread", None
                        )
                    if _raw_spread is not None:
                        entry.vegas_spread = (
                            _raw_spread if is_home else -_raw_spread
                        )

        # ── MLB environmental adjustment pass (Prompts 6.1 + 4.4) ────
        # Combine the static park factor with the LIVE wind multiplier
        # to produce a per-player ``adjusted_fp``. Math:
        #
        #   wind_mult =
        #       1.0                                          if dome / no weather
        #       calculate_wind_multiplier(speed, dir, hdg)   otherwise
        #
        #   env_mult =
        #       run_factor * wind_mult                       for hitters
        #       pitcher_factor                               for pitchers
        #
        #   adjusted_fp = projected_fp * env_mult
        #
        # Pitchers ignore wind in the MVP — DK pitcher scoring is K/IP-
        # driven and wind has a much smaller effect on those stats than
        # on HR/run-scoring for hitters.
        #
        # ``projected_fp`` itself is intentionally left untouched so the
        # UI keeps showing the raw CSV value; the optimizer routes
        # through ``adjusted_fp`` via ``_effective_projection()``.
        if sport == "mlb":
            try:
                from app.sports import get_config as _get_sport_cfg
                from app.sports.mlb_park_factors import (
                    compute_environmental_multiplier,
                )

                _mlb_cfg = _get_sport_cfg("mlb")
                _pos_to_class = _mlb_cfg.pos_to_class or {}
                _adjusted_count = 0
                _adjusted_at_neutral = 0
                # Postponement-risk surveillance (Prompt 7.3) — track
                # the set of unique high-precip games in the pool so we
                # can emit a single grouped warning rather than one per
                # affected player.
                _HIGH_PRECIP_THRESHOLD = 75
                _high_precip_games: Dict[str, dict] = {}
                for entry in pool:
                    _ctx = game_lookup.get(entry.team_abbreviation.upper())
                    _gi = (_ctx or {}).get("game_info")
                    _venue = getattr(_gi, "venue", None) if _gi else None
                    weather = (
                        getattr(_gi, "weather", None) if _gi else None
                    )
                    env_mult = compute_environmental_multiplier(
                        venue=_venue,
                        weather=weather,
                        position=entry.position,
                        pos_to_class=_pos_to_class,
                    )
                    entry.adjusted_fp = entry.projected_fp * env_mult
                    if env_mult == 1.0:
                        _adjusted_at_neutral += 1
                    else:
                        _adjusted_count += 1

                    # High-precip flag: remember the game so we surface
                    # one warning per game, not one per player. The UI
                    # still renders the rain badge per the
                    # GameSlateCard.jsx logic; this log is a separate
                    # safety net for the operator running generation.
                    if weather:
                        precip = weather.get("precip_prob")
                        gid = (_ctx or {}).get("game_id")
                        if (
                            isinstance(precip, (int, float))
                            and precip >= _HIGH_PRECIP_THRESHOLD
                            and gid is not None
                            and gid not in _high_precip_games
                        ):
                            _high_precip_games[gid] = {
                                "venue": _venue,
                                "precip_prob": int(precip),
                                "team": entry.team_abbreviation.upper(),
                            }

                logger.info(
                    f"[Enrich] MLB env multipliers applied: "
                    f"{_adjusted_count} adjusted, "
                    f"{_adjusted_at_neutral} held at neutral"
                )

                # Single grouped warning for any high-precip games. The
                # MVP surface for postponement risk: surfaces in the UI
                # via the rain badge, and surfaces in the log here so
                # operators tailing the server can see it before they
                # commit a lineup. A future prompt may add an
                # ``exclude_high_weather_risk`` request param to hard-
                # exclude these players from the pool.
                if _high_precip_games:
                    summary = ", ".join(
                        f"{g['venue'] or 'unknown park'} ({g['team']}, "
                        f"{g['precip_prob']}%)"
                        for g in _high_precip_games.values()
                    )
                    logger.warning(
                        f"[Enrich] HIGH PRECIP RISK on {len(_high_precip_games)} "
                        f"MLB game(s) — postponement possible: {summary}. "
                        f"Consider excluding players in these games or wait "
                        f"for an official postponement update."
                    )
            except Exception as e:
                # Don't fail enrichment over env factors — leave
                # adjusted_fp as None so the optimizer falls back to
                # raw projected_fp via _effective_projection().
                logger.warning(
                    f"[Enrich] MLB env-multiplier pass failed: {e}",
                    exc_info=True,
                )

        # ── NFL environmental adjustment pass (Prompt 7.5) ───────────
        # Position-aware wind penalty: outdoor games with wind ≥15 mph
        # get a class-specific multiplier (kickers/DST -15%, QB/WR/TE
        # -8%, RB +2%). Domes / no-weather / sub-15 mph wind all
        # collapse to 1.0. Same ``adjusted_fp`` storage pattern as MLB
        # so ``_effective_projection`` and the UI badge components
        # (Prompt 7.2 / 7.4) work without sport-specific branching.
        if sport == "nfl":
            try:
                from app.sports.nfl_park_factors import (
                    compute_nfl_environmental_multiplier,
                )

                _adjusted_count = 0
                _adjusted_at_neutral = 0
                for entry in pool:
                    _ctx = game_lookup.get(entry.team_abbreviation.upper())
                    _gi = (_ctx or {}).get("game_info")
                    weather = (
                        getattr(_gi, "weather", None) if _gi else None
                    )
                    env_mult = compute_nfl_environmental_multiplier(
                        weather=weather,
                        position=entry.position,
                    )
                    entry.adjusted_fp = entry.projected_fp * env_mult
                    if env_mult == 1.0:
                        _adjusted_at_neutral += 1
                    else:
                        _adjusted_count += 1

                logger.info(
                    f"[Enrich] NFL wind penalties applied: "
                    f"{_adjusted_count} adjusted, "
                    f"{_adjusted_at_neutral} held at neutral"
                )
            except Exception as e:
                logger.warning(
                    f"[Enrich] NFL env-multiplier pass failed: {e}",
                    exc_info=True,
                )

        # DK Sportsbook props → attach market lines + signal
        if _props_lookup:
            try:
                from app.services.dk_draftables_service import _normalize_name
                props_attached = 0
                for entry in pool:
                    normalized = _normalize_name(entry.player_name)
                    abbr = entry.team_abbreviation.upper()
                    key = f"{normalized}:{abbr}"
                    player_props = _props_lookup.get(key)
                    if not player_props:
                        # Try without team (some props may lack team abbr)
                        player_props = _props_lookup.get(normalized)
                    if player_props:
                        pts_line = player_props.get_line("pts")
                        reb_line = player_props.get_line("reb")
                        ast_line = player_props.get_line("ast")
                        pra_line = player_props.get_line("pra")

                        if pts_line:
                            entry.props_pts_line = pts_line.line
                        if reb_line:
                            entry.props_reb_line = reb_line.line
                        if ast_line:
                            entry.props_ast_line = ast_line.line
                        if pra_line:
                            entry.props_pra_line = pra_line.line

                        # Compute average delta across available props
                        deltas = []
                        if entry.projected_stats:
                            for stat_key in ("pts", "reb", "ast"):
                                line = player_props.get_line(stat_key)
                                our_val = entry.projected_stats.get(stat_key, 0)
                                if line and line.line > 0 and our_val > 0:
                                    d_pct = (our_val - line.line) / line.line * 100
                                    deltas.append(d_pct)

                        if deltas:
                            avg_delta = sum(deltas) / len(deltas)
                            entry.props_delta_pct = round(avg_delta, 1)
                            if abs(avg_delta) <= 5.0:
                                entry.props_signal = "aligned"
                            elif avg_delta > 5.0:
                                entry.props_signal = "bullish"
                            else:
                                entry.props_signal = "bearish"

                        props_attached += 1

                logger.info(f"[Enrich] DK props attached to {props_attached} pool entries")
            except Exception as e:
                logger.warning(f"[Enrich] DK props application failed: {e}")

        # DK FPPG → attach DK's own projection + compute delta + blend
        if _fppg_lookup:
            try:
                from app.config.constants import (
                    DK_FPPG_DIVERGENCE_THRESHOLD,
                    DK_FPPG_BLEND_WEIGHT,
                    DK_FPPG_BLEND_ASYMMETRIC,
                )
                from app.services.dk_draftables_service import _normalize_name
                fppg_attached = 0
                fppg_blended = 0
                for entry in pool:
                    normalized = _normalize_name(entry.player_name)
                    abbr = entry.team_abbreviation.upper()
                    # Try team-specific key first, then empty-team fallback
                    # (build_fppg_lookup often uses empty team strings)
                    key = f"{normalized}:{abbr}"
                    dk_fppg = _fppg_lookup.get(key) or _fppg_lookup.get(f"{normalized}:")
                    if dk_fppg and dk_fppg > 0:
                        entry.dk_fppg = round(dk_fppg, 1)
                        entry.dk_fppg_delta = round(entry.projected_fp - dk_fppg, 1)
                        fppg_attached += 1

                        # Blend projection toward DK FPPG when divergence
                        # exceeds threshold — DK's aggregate is a free
                        # market-consensus signal that anchors outliers.
                        #
                        # Directional FPPG blend:
                        # - Upward (our < DK): aggressive correction at
                        #   15% threshold — our under-projections need
                        #   strong pull toward market consensus.
                        # - Downward (our > DK): gentler correction at
                        #   30% threshold — FPPG is a season average that
                        #   can underestimate game-specific role expansion
                        #   (spot starts, injuries), so we preserve more
                        #   of our game-context edge.
                        if entry.projected_fp > 0:
                            if dk_fppg > 0:
                                delta_pct = abs(entry.projected_fp - dk_fppg) / dk_fppg
                            else:
                                # DK FPPG is 0 — extreme divergence
                                delta_pct = 10.0
                            _is_over = entry.projected_fp > dk_fppg

                            # Direction-aware threshold and cap:
                            # - Upward (our < DK): 15% threshold, aggressive
                            # - Downward (our > DK): 30% threshold, gentle,
                            #   and CAPPED at 100% divergence.  Above 100%
                            #   the player is likely in an expanded role
                            #   (spot start, injury fill-in) and their low
                            #   season FPPG doesn't reflect tonight's usage.
                            _threshold = (
                                0.30 if (_is_over and DK_FPPG_BLEND_ASYMMETRIC)
                                else DK_FPPG_DIVERGENCE_THRESHOLD
                            )
                            # Skip downward blend for breakout players:
                            # Extreme divergence (>100%) AND meaningful FPPG
                            # (>=5) — player is in an expanded role tonight
                            # and their moderate historical FPPG doesn't
                            # reflect tonight's usage.
                            #
                            # DO NOT skip when FPPG < 5.0 — these players
                            # have near-zero historical production and are
                            # almost certainly not playing (end-of-bench,
                            # recently traded, etc.).  They need aggressive
                            # downward correction.
                            _skip_breakout = (
                                _is_over
                                and DK_FPPG_BLEND_ASYMMETRIC
                                and delta_pct >= 1.00
                                and dk_fppg >= 5.0
                            )

                            # Near-zero FPPG: force aggressive downward
                            # blend even below the 30% threshold.  Players
                            # with <2 FPPG who we project >5 FP are almost
                            # certainly not playing tonight.
                            _force_down = (
                                _is_over
                                and dk_fppg < 2.0
                                and entry.projected_fp > 5.0
                            )

                            if _skip_breakout:
                                logger.info(
                                    f"[Enrich] FPPG skip (breakout): "
                                    f"{entry.player_name} our={entry.projected_fp:.1f} "
                                    f"> dk={dk_fppg:.1f} ({delta_pct:.0%}) — "
                                    f"likely expanded role, preserving projection"
                                )
                            elif _force_down or delta_pct > _threshold:
                                old_fp = entry.projected_fp
                                if _force_down:
                                    # Near-zero FPPG: player almost certainly
                                    # won't play.  Aggressively pull toward DK.
                                    _ours_w = 0.20
                                elif _is_over:
                                    # Gentler downward weights for 30-100%
                                    # divergence — correct moderate over-
                                    # projections while preserving edge.
                                    if delta_pct >= 0.75:
                                        _ours_w = 0.65
                                    elif delta_pct >= 0.50:
                                        _ours_w = 0.75
                                    else:  # 0.30 - 0.50
                                        _ours_w = 0.85
                                else:
                                    # Upward blend — pull toward DK consensus
                                    # for under-projections.  More aggressive
                                    # at moderate divergence to catch under-
                                    # projected rotation players (e.g. Murray
                                    # 24.6 vs consensus 31.8).
                                    if delta_pct >= 2.00:
                                        _ours_w = 0.20
                                    elif delta_pct >= 1.00:
                                        _ours_w = 0.30
                                    elif delta_pct >= 0.50:
                                        _ours_w = 0.40
                                    elif delta_pct >= 0.30:
                                        _ours_w = 0.55
                                    elif delta_pct >= 0.15:
                                        _ours_w = 0.70
                                    else:
                                        _ours_w = DK_FPPG_BLEND_WEIGHT
                                entry.projected_fp = round(
                                    old_fp * _ours_w
                                    + dk_fppg * (1.0 - _ours_w),
                                    1,
                                )
                                entry.dk_fppg_delta = round(entry.projected_fp - dk_fppg, 1)
                                fppg_blended += 1
                                _dir = "DOWN" if _is_over else "UP"
                                if delta_pct > 0.25:
                                    logger.info(
                                        f"[Enrich] FPPG blend {_dir}: {entry.player_name} "
                                        f"our={old_fp:.1f} → blended={entry.projected_fp:.1f} "
                                        f"dk={dk_fppg:.1f} ({delta_pct:.0%}, w={_ours_w:.0%})"
                                    )
                            elif _is_over and delta_pct > 0.25:
                                logger.info(
                                    f"[Enrich] FPPG skip (below 30% threshold): "
                                    f"{entry.player_name} our={entry.projected_fp:.1f} "
                                    f"> dk={dk_fppg:.1f} ({delta_pct:.0%})"
                                )

                # Log unmatched players with high projections (helps
                # identify blend gaps where bench inflation slips through)
                _unmatched_high = [
                    e for e in pool
                    if e.dk_fppg is None
                    and e.projected_fp > 10.0
                ]
                if _unmatched_high:
                    for _um in sorted(
                        _unmatched_high,
                        key=lambda x: x.projected_fp,
                        reverse=True,
                    )[:10]:
                        logger.warning(
                            f"[Enrich] NO FPPG match: {_um.player_name} "
                            f"({_um.team_abbreviation}) "
                            f"fp={_um.projected_fp:.1f} — "
                            f"projection unanchored by market data"
                        )

                logger.info(
                    f"[Enrich] DK FPPG attached to {fppg_attached} pool entries, "
                    f"blended {fppg_blended}, "
                    f"unmatched(fp>10)={len(_unmatched_high)}"
                )
            except Exception as e:
                logger.warning(f"[Enrich] DK FPPG application failed: {e}")

        # (Salary-minutes cap removed — salary does not determine
        #  playing time; cheap starters are value plays, not errors.)

        # ── External projection import override ────────────────────────
        # If the user uploaded a consensus projection CSV, override pool
        # entries with the external values.  Applied AFTER FPPG blend so
        # it takes final precedence.
        _proj_overrides = _apply_imported_projection_overrides(pool)
        if _proj_overrides:
            logger.info(
                f"[Enrich] External projection override: "
                f"{_proj_overrides} players updated from imported CSV"
            )

        # Fade / leverage scores for optimizer integration
        if self.fade_service:
            try:
                self._fade_leverage_scores = self.fade_service.get_player_scores(pool)
                logger.info(
                    f"[Enrich] Fade/leverage scores: {len(self._fade_leverage_scores)} players tagged"
                )
            except Exception as e:
                logger.warning(f"[Enrich] Fade/leverage scoring failed: {e}")
                self._fade_leverage_scores = {}

        # Expert signals → sentiment
        if _expert_signals:
            try:
                player_sentiment: Dict[str, Dict[str, int]] = {}
                for sig in _expert_signals:
                    for mentioned in (sig.mentioned_players or []):
                        lm = mentioned.lower()
                        for entry in pool:
                            en = entry.player_name.lower()
                            if lm in en or en in lm:
                                pid = str(entry.player_id)
                                if pid not in player_sentiment:
                                    player_sentiment[pid] = {
                                        "bullish": 0, "bearish": 0, "neutral": 0,
                                    }
                                sent = sig.sentiment or "neutral"
                                player_sentiment[pid][sent] = (
                                    player_sentiment[pid].get(sent, 0) + 1
                                )

                mention_count = 0
                for entry in pool:
                    pid = str(entry.player_id)
                    counts = player_sentiment.get(pid)
                    if counts:
                        total = sum(counts.values())
                        entry.expert_signal_count = total
                        mention_count += 1
                        best_sent = max(counts, key=counts.get)
                        entry.expert_sentiment = best_sent
                        boost = (
                            counts.get("bullish", 0) * 0.05
                            - counts.get("bearish", 0) * 0.05
                        )
                        entry.expert_confidence_boost = max(-0.1, min(0.1, boost))

                logger.info(
                    f"[Enrich] Expert signals: {len(_expert_signals)} signals, "
                    f"{mention_count} player mentions"
                )
            except Exception as e:
                logger.warning(f"[Enrich] Expert signal processing failed: {e}")

        # News → projection adjustments (rules-based + AI)
        # Step 1: Rules-based deterministic pipeline (works without AI)
        _rules_applied = 0
        if _news_items:
            try:
                _rules_applied = self._apply_rules_based_news(
                    _news_items, pool, sport=sport
                )
                if _rules_applied:
                    logger.info(
                        f"[Enrich] Rules-based news: {_rules_applied} "
                        f"adjustments applied"
                    )
            except Exception as e:
                logger.warning(f"[Enrich] Rules-based news failed: {e}")

        # Step 2: AI agent refinement (can override/supplement rules)
        if _news_items and self.news_projection_agent:
            try:
                news_dicts = [
                    {"id": getattr(n, "id", ""), "headline": getattr(n, "headline", ""),
                     "description": getattr(n, "description", ""),
                     "relevance": getattr(n, "relevance", "general")}
                    for n in _news_items
                ]
                adjustments = self.news_projection_agent.extract_adjustments(news_dicts)
                applied = 0
                # O(1) lookup by name instead of scanning entire pool per adjustment
                _pool_by_name_lower: Dict[str, PlayerPoolEntry] = {
                    e.player_name.lower(): e for e in pool
                }
                for adj in adjustments:
                    entry = _pool_by_name_lower.get(adj.player_name.lower())
                    if entry is None:
                        continue
                    if adj.minutes_override is not None:
                        # Sport-specific cap from registry (NBA=53, CBB=45)
                        from app.sports import get_config as _get_sport_cfg
                        _max_min = _get_sport_cfg(sport).max_player_minutes
                        entry.projected_minutes = min(adj.minutes_override, _max_min)
                        applied += 1
                    # Apply usage_modifier (previously ignored)
                    if adj.usage_modifier is not None:
                        entry.projected_fp = round(
                            entry.projected_fp * adj.usage_modifier, 1
                        )
                        entry.ceiling_fp = round(
                            entry.ceiling_fp * adj.usage_modifier, 1
                        )
                        entry.floor_fp = round(
                            entry.floor_fp * adj.usage_modifier, 1
                        )
                        applied += 1
                if applied:
                    logger.info(f"[Enrich] AI news adjustments: {applied} players modified")
            except Exception as e:
                logger.warning(f"[Enrich] News projection failed: {e}")

        # ══════════════════════════════════════════════════════════════
        # TIER 2 — Simulation percentiles + ownership (concurrent)
        # Ownership only needs pool + Tier 1 data, so it runs alongside
        # simulation rather than waiting for it.
        # ══════════════════════════════════════════════════════════════

        # Launch ownership projection early — it doesn't depend on sim
        _ownership_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="enrich-own")
        def _run_ownership():
            _num_games = len(set(p.team_abbreviation for p in pool)) // 2
            pool_dicts = [
                {"player_id": p.player_id, "player_name": p.player_name,
                 "position": p.position, "salary": p.salary,
                 "projected_fp": p.projected_fp,
                 "dk_value": p.dk_value or 0,
                 "expert_sentiment": p.expert_sentiment or "",
                 "game_total": p.game_total or "",
                 "projected_minutes": p.projected_minutes or 0,
                 "floor_fp": p.floor_fp or 0,
                 "ceiling_fp": p.ceiling_fp or 0,
                 "team_abbreviation": p.team_abbreviation or "",
                 "injury_status": p.injury_status or "",
                 "is_b2b": p.is_b2b or False,
                 "vegas_spread": p.vegas_spread}
                for p in pool
            ]
            slate_ctx = {"platform": platform, "num_games": _num_games}

            # Try AI agent first (includes its own rules-based fallback)
            if self.ownership_agent:
                try:
                    result = self.ownership_agent.project_ownership(
                        pool_dicts, slate_ctx,
                    )
                    if result:
                        return result
                except Exception as e:
                    logger.warning(f"[Enrich] Ownership agent failed: {e}")

            # Last-resort: direct rules-based model call
            try:
                from app.services.ownership_model import project_ownership
                result = project_ownership(
                    pool_dicts, platform=platform, num_games=_num_games,
                )
                if result:
                    logger.info(
                        f"[Enrich] Ownership fallback (rules-based): "
                        f"{len(result)} players"
                    )
                    return result
            except Exception as e:
                logger.warning(f"[Enrich] Ownership rules fallback failed: {e}")
            return {}

        _own_future = _ownership_executor.submit(_run_ownership)

        # Time budget for simulation tier — if enrichment has already
        # consumed most of the request's 60s timeout, skip simulation
        # and use base projections (which are already good enough).
        _SIM_BUDGET_S = 30.0
        _enrichment_elapsed = time.time() - t_enrich_start

        if self.simulation_engine and game_lookup and _enrichment_elapsed < _SIM_BUDGET_S:
            try:
                from app.models.simulation import SimulationConfig
                from app.services.simulation_engine import SimulationEngine

                sim_config = SimulationConfig(num_simulations=2500)  # was 1000 — better P10/P90 tail stability

                sim_player_results: Dict[int, dict] = {}

                # Deduplicate games (each game appears twice in game_lookup — once per team)
                seen_game_ids: set = set()
                unique_games = []
                for g_ctx in game_lookup.values():
                    g = g_ctx["game_info"]
                    if g.game_id not in seen_game_ids:
                        seen_game_ids.add(g.game_id)
                        unique_games.append(g)

                # Reuse pre-computed team data from build_player_pool
                _cached_teams = getattr(self, "_team_data_cache", {})

                def _simulate_single_game(g):
                    """Simulate one game and return {player_id: percentiles}."""
                    fast_sim = SimulationEngine(sim_config)
                    results = {}
                    try:
                        home_abbr = g.home_team.team_abbreviation.upper()
                        away_abbr = g.away_team.team_abbreviation.upper()
                        home_cached = _cached_teams.get(home_abbr)
                        away_cached = _cached_teams.get(away_abbr)

                        # Use cached data when available, fall back to live fetch
                        if home_cached and away_cached:
                            home_rot = home_cached["rotation"]
                            away_rot = away_cached["rotation"]
                            home_proj = home_cached["projected"]
                            away_proj = away_cached["projected"]
                        else:
                            home_id = g.home_team.team_id
                            away_id = g.away_team.team_id
                            # Use sport-appropriate data service for fallback
                            _data_svc = (
                                self.cbb_data_service
                                if sport == "cbb" and self.cbb_data_service
                                else self.nba_service
                            )
                            if sport == "nba":
                                _nba_cache = getattr(self.nba_service, '_db_cache', None)
                                home_rot = _data_svc.build_team_rotation(
                                    home_id, cache_service=_nba_cache,
                                    db_cache_only=True,  # No live BDL
                                )
                                away_rot = _data_svc.build_team_rotation(
                                    away_id, cache_service=_nba_cache,
                                    db_cache_only=True,  # No live BDL
                                )
                            else:
                                home_rot = _data_svc.build_team_rotation(home_id)
                                away_rot = _data_svc.build_team_rotation(away_id)
                            if not home_rot or not away_rot:
                                return results
                            _inj_svc = (
                                self.cbb_injury_service
                                if sport == "cbb" and hasattr(self, "cbb_injury_service") and self.cbb_injury_service
                                else self.injury_service
                            )
                            home_inj = _inj_svc.get_team_injuries(g.home_team.team_name)
                            away_inj = _inj_svc.get_team_injuries(g.away_team.team_name)
                            all_inj = self.injury_service.get_all_injuries()
                            home_proj = self.engine.project_team_rotation(
                                team_id=home_id, team_name=g.home_team.team_name,
                                rotation=home_rot, injuries=home_inj,
                                game_date=game_date, game_info=g, all_injuries=all_inj,
                                sport=sport,
                            )
                            away_proj = self.engine.project_team_rotation(
                                team_id=away_id, team_name=g.away_team.team_name,
                                rotation=away_rot, injuries=away_inj,
                                game_date=game_date, game_info=g, all_injuries=all_inj,
                                sport=sport,
                            )
                            home_dvp = None
                            away_dvp = None
                            if self.game_service:
                                try:
                                    home_dvp = self.game_service.get_dvp_matchup_factors(away_id)
                                    away_dvp = self.game_service.get_dvp_matchup_factors(home_id)
                                except Exception:
                                    pass
                            self.dfs_service.project_team_dfs(
                                home_proj, home_rot, matchup_factors=home_dvp, sport=sport,
                            )
                            self.dfs_service.project_team_dfs(
                                away_proj, away_rot, matchup_factors=away_dvp, sport=sport,
                            )

                        result = fast_sim.simulate_game(
                            game_info=g,
                            home_rotation=home_proj,
                            home_players=home_rot,
                            away_rotation=away_proj,
                            away_players=away_rot,
                            player_noise_overrides=_noise_overrides,
                            sport=sport,
                        )

                        for team_res in [result.home_team, result.away_team]:
                            for pr in team_res.players:
                                pct_key = (
                                    "dk_percentiles"
                                    if platform == "dk"
                                    else "fd_percentiles"
                                )
                                pcts = getattr(pr, pct_key, {}) or {}
                                std_key = (
                                    "std_dk_points"
                                    if platform == "dk"
                                    else "std_fd_points"
                                )
                                results[pr.player_id] = {
                                    "p10": pcts.get("p10"),
                                    "p50": pcts.get("p50"),
                                    "p90": pcts.get("p90"),
                                    "std": getattr(pr, std_key, None),
                                }
                    except Exception as e:
                        logger.warning(
                            f"[Enrich] Sim failed for game {g.game_id}: {e}"
                        )
                    return results

                # Run simulations in parallel (6 workers — each game is
                # mostly NumPy/pandas so the GIL is released frequently).
                # Collect results with a hard overall time budget;
                # cancel any un-started futures once we exceed it.
                simulated_games = 0
                skipped_games = 0
                _sim_deadline = t_enrich_start + _SIM_BUDGET_S
                with ThreadPoolExecutor(
                    max_workers=6, thread_name_prefix="enrich-sim"
                ) as sim_pool:
                    futures = {
                        sim_pool.submit(_simulate_single_game, g): g.game_id
                        for g in unique_games
                    }
                    # Per-game timeout: divide remaining budget fairly
                    # across games so one slow game doesn't starve the rest.
                    _n_games = len(unique_games)
                    _per_game_timeout = max(
                        5.0, (_sim_deadline - time.time()) / max(_n_games, 1)
                    )
                    for future in as_completed(futures):
                        # Check overall time budget
                        if time.time() > _sim_deadline:
                            # Cancel remaining queued (not yet running)
                            # futures — running ones can't be interrupted
                            for f in futures:
                                f.cancel()
                            skipped_games = sum(
                                1 for f in futures if f.cancelled()
                            )
                            break
                        try:
                            game_results = future.result(
                                timeout=_per_game_timeout
                            )
                        except TimeoutError:
                            gid = futures.get(future, "?")
                            logger.warning(
                                f"[Enrich] Sim for game {gid} timed out "
                                f"(>{_per_game_timeout:.0f}s)"
                            )
                            continue
                        except Exception as exc:
                            logger.warning(f"[Enrich] Sim future error: {exc}")
                            continue
                        if game_results:
                            sim_player_results.update(game_results)
                            simulated_games += 1
                if skipped_games:
                    logger.warning(
                        f"[Enrich] {skipped_games} game sims cancelled "
                        f"(time budget exceeded)"
                    )

                enriched_sims = 0
                for entry in pool:
                    sim_data = sim_player_results.get(entry.player_id)
                    if sim_data:
                        entry.sim_p10 = sim_data.get("p10")
                        entry.sim_p50 = sim_data.get("p50")
                        entry.sim_p90 = sim_data.get("p90")
                        entry.sim_std = sim_data.get("std")
                        enriched_sims += 1

                logger.info(
                    f"[Enrich] Simulation: {simulated_games} games, "
                    f"{enriched_sims} players enriched"
                )
            except Exception as e:
                logger.warning(f"[Enrich] Simulation enrichment failed: {e}")
        elif _enrichment_elapsed >= _SIM_BUDGET_S:
            logger.warning(
                f"[Enrich] Simulation SKIPPED — enrichment already at "
                f"{_enrichment_elapsed:.1f}s (budget: {_SIM_BUDGET_S}s). "
                f"Using base projections."
            )

        # Improvement #6: Compute boom_probability for each player.
        # Prefer sim_p90 / projected_fp (simulation upside ratio); fall
        # back to ceiling_fp / projected_fp when sims unavailable.
        _boom_computed = 0
        for entry in pool:
            if entry.projected_fp and entry.projected_fp > 0:
                if entry.sim_p90:
                    entry.boom_probability = entry.sim_p90 / entry.projected_fp
                    _boom_computed += 1
                elif entry.ceiling_fp:
                    entry.boom_probability = entry.ceiling_fp / entry.projected_fp
                    _boom_computed += 1
        if _boom_computed:
            logger.info(
                f"[Enrich] Boom probability computed for {_boom_computed} players"
            )

        t2_elapsed = time.time() - t_enrich_start
        logger.info(f"[Enrich] Tier 2 completed in {t2_elapsed:.1f}s")

        # ══════════════════════════════════════════════════════════════
        # TIER 3 — Collect ownership (launched before Tier 2) + strategy
        # ══════════════════════════════════════════════════════════════
        ownership_result = {}
        try:
            from app.config.constants import ENRICH_AI_AGENT_TIMEOUT_S
            ownership_result = _own_future.result(
                timeout=ENRICH_AI_AGENT_TIMEOUT_S
            )
        except TimeoutError:
            logger.warning(
                f"[Enrich] Ownership projection timed out "
                f"(>{ENRICH_AI_AGENT_TIMEOUT_S}s) — fallback will fill gaps"
            )
        except Exception as e:
            logger.warning(f"[Enrich] Ownership future failed: {e}")
        finally:
            _ownership_executor.shutdown(wait=False)

        # Apply ownership results
        if ownership_result:
            for entry in pool:
                if entry.player_id in ownership_result:
                    entry.estimated_ownership = ownership_result[entry.player_id]
            logger.info(f"[Enrich] Ownership: {len(ownership_result)} players projected")

        # Improvement #4: Override with imported external ownership (CSV)
        if _imported_ownership:
            from app.services.dk_draftables_service import _normalize_name
            _own_overrides = 0
            with _imported_ownership_lock:
                for entry in pool:
                    _norm = _normalize_name(entry.player_name)
                    if _norm in _imported_ownership:
                        entry.estimated_ownership = _imported_ownership[_norm]
                        _own_overrides += 1
            if _own_overrides:
                logger.info(
                    f"[Enrich] External ownership override: "
                    f"{_own_overrides} players updated from imported CSV"
                )

        # Run strategy after ownership is applied (strategy depends on ownership)
        strategy_result = None
        if self.lineup_strategy_agent and self.lineup_strategy_agent.is_available:
            try:
                strat_pool_dicts = [
                    {"player_id": p.player_id, "player_name": p.player_name,
                     "position": p.position, "salary": p.salary,
                     "projected_fp": p.projected_fp,
                     "floor_fp": p.floor_fp, "ceiling_fp": p.ceiling_fp,
                     "team_abbreviation": p.team_abbreviation,
                     "game_total": p.game_total or ""}
                    for p in sorted(pool, key=lambda x: x.projected_fp, reverse=True)[:50]
                ]
                ownership_map = {p.player_id: p.estimated_ownership or 0 for p in pool}
                strategy_result = self.lineup_strategy_agent.get_strategy_adjustments(
                    strat_pool_dicts, contest_type=contest_type, ownership=ownership_map,
                )
            except Exception as e:
                logger.warning(f"[Enrich] Strategy agent failed: {e}")

        if strategy_result:
            self._strategy_adjustments = strategy_result
            logger.info(
                f"[Enrich] Strategy: {len(strategy_result.player_score_modifiers)} modifiers"
            )

        # ══════════════════════════════════════════════════════════════
        # TIER 4 — Last-resort heuristic fill for any remaining gaps
        # ══════════════════════════════════════════════════════════════
        # If AI agents failed, simulation was skipped/crashed, and the
        # ownership model couldn't run, some pool entries may still lack
        # sim_std, sim_p10/p50/p90, or estimated_ownership.  Apply
        # position-based variance and salary-tier ownership heuristics
        # so composite_score and the ILP optimizer can still function.
        _fallback_count = self._generate_fallback_enrichment(pool, platform)
        if _fallback_count == 0:
            _missing_sim = sum(1 for p in pool if p.sim_std is None)
            _missing_own = sum(1 for p in pool if p.estimated_ownership is None)
            if _missing_sim == 0 and _missing_own == 0:
                logger.info("[Enrich] All players fully enriched (no fallback needed)")

        total_elapsed = time.time() - t_enrich_start
        logger.info(f"[Enrich] Total enrichment completed in {total_elapsed:.1f}s")

        # Store noise override count for enrichment validation gate.
        # _noise_overrides is function-local; expose the count so the
        # caller (generate_lineups) can pass it to _validate_pool_enrichment.
        self._last_noise_override_count = (
            len(_noise_overrides) if _noise_overrides else 0
        )

        return pool

    # ------------------------------------------------------------------
    # Multi-lineup generation
    # ------------------------------------------------------------------

    def generate_lineups(
        self, request: MultiLineupRequest
    ) -> MultiLineupResponse:
        """Generate N diverse, high-quality lineups using enriched pool.

        Uses an **overgenerate-then-filter** pipeline:
          1. Generate more candidates than requested (tiered multiplier)
          2. Score each candidate with a holistic lineup-level metric
          3. Select the best N that satisfy diversity (max_overlap)

        This replaces the old sequential-accept approach and produces
        meaningfully higher-quality lineup sets, especially for GPP.
        """
        start_ms = time.time()
        self._lineup_strategy = None  # reset per-call
        platform = request.platform
        mode = getattr(request, "mode", "classic")
        showdown_game_id = getattr(request, "game_id", None)
        _is_late_swap = getattr(request, "is_late_swap", False)

        # Determine sport early — needed for roster slot selection
        _sport = getattr(request, "sport", "nba")

        if _is_late_swap:
            logger.info(
                "[Lineup] Late-swap mode: generating lineups for live slate "
                "(DG=%s, sport=%s)", request.draft_group_id, _sport
            )

        if mode == "showdown" and platform == "dk":
            salary_cap = DK_SHOWDOWN_SALARY_CAP
            roster_slots = list(DK_SHOWDOWN_SLOTS)
            slot_order = list(range(len(roster_slots)))
        else:
            salary_cap = DK_SALARY_CAP if platform == "dk" else FD_SALARY_CAP
            if platform == "dk":
                from app.sports import get_config as _get_sport_cfg
                _cfg = _get_sport_cfg(_sport)
                roster_slots = list(_cfg.dk_roster_slots)
                slot_order = list(_cfg.dk_slot_order)
            else:
                roster_slots = list(FD_ROSTER_SLOTS)
                slot_order = list(FD_SLOT_ORDER)

        # ── Phase 0: Build & enrich pool (sport-aware) ────────────────
        pool = self.build_player_pool(
            platform=platform,
            draft_group_id=request.draft_group_id,
            game_date=request.game_date,
            excluded_player_ids=request.excluded_players,
            sport=_sport,
            recent_weight=getattr(request, "recent_weight", None),
        )
        if not pool:
            raise ValueError("No players available for this slate")

        # In showdown mode, filter pool to the single target game
        if mode == "showdown" and showdown_game_id:
            pool = [
                p for p in pool
                if getattr(p, "game_id", None) == showdown_game_id
            ]
            if not pool:
                raise ValueError("No players found for the selected showdown game")

        gd = request.game_date or date.today().isoformat()
        contest_type = getattr(request, "contest_type", "gpp")

        # Check enrichment cache — enriched data is reusable across
        # strategy/contest_type changes (only scoring uses those).
        # Include sport prefix to prevent cross-sport cache collisions.
        #
        # Cache layers (checked in order):
        #   1. In-memory enriched cache (_enriched_cache) — fastest
        #   2. Enriched file cache (enriched_*.json) — survives restarts
        #   3. Fresh enrichment via _enrich_pool() — slowest
        enrich_key = _cache_key(f"{_sport}:{platform}", request.draft_group_id, gd)
        now = time.time()
        _enriched_hit = False
        with _enriched_lock:
            if enrich_key in _enriched_cache:
                cached_at, cached_enriched = _enriched_cache[enrich_key]
                if now - cached_at < _ENRICHED_CACHE_TTL:
                    pool = [p.model_copy() for p in cached_enriched]
                    with _strategy_lock:
                        if enrich_key in _strategy_cache:
                            _, cached_strat = _strategy_cache[enrich_key]
                            self._strategy_adjustments = cached_strat
                    logger.info(
                        f"[Cache] Enrichment cache hit ({len(pool)} players, "
                        f"age={now - cached_at:.0f}s)"
                    )
                    _enriched_hit = True

        # Layer 2: enriched file cache (disk-persistent, validated)
        if not _enriched_hit:
            _enriched_file_pool = _load_enriched_pool_from_file(enrich_key)
            if _enriched_file_pool:
                pool = _enriched_file_pool
                with _enriched_lock:
                    _enriched_cache[enrich_key] = (time.time(), pool)
                logger.info(
                    f"[Cache] Enriched file cache hit — promoted to memory "
                    f"({len(pool)} players)"
                )
                _enriched_hit = True

        # _enrich_pool() applies CSV projection overrides for fresh
        # enrichment, but cached pools (memory or file) bypass that step.
        # Re-apply here so imports are honored regardless of cache state.
        if _enriched_hit:
            _cached_overrides = _apply_imported_projection_overrides(pool)
            if _cached_overrides:
                logger.info(
                    f"[Cache] Applied {_cached_overrides} imported "
                    f"projection overrides to cached pool"
                )

        if not _enriched_hit:
            pool = self._enrich_pool(
                pool, platform, gd,
                contest_type=contest_type, sport=_sport,
                draft_group_id=request.draft_group_id,
            )

            # ── Enrichment validation gate ────────────────────────────
            # Validate that critical enrichment data (DK FPPG, sim
            # tuning) populated successfully before caching.  If
            # validation fails, fall back to the last known good
            # enriched file cache rather than persisting degraded data.
            _noise_count = getattr(self, "_last_noise_override_count", 0)
            _enrichment_valid = True
            try:
                _validate_pool_enrichment(pool, noise_override_count=_noise_count)
                logger.info(
                    "[Enrich] Validation PASSED — pool is fully enriched "
                    f"(noise_profiles={_noise_count})"
                )
            except DataDegradationError as dde:
                _enrichment_valid = False
                logger.critical(dde.message)
                for check in dde.failed_checks:
                    logger.critical(f"  - {check}")

                # ── Fallback: load last known good enriched cache ─────
                _fallback_pool = _load_enriched_pool_from_file(enrich_key)
                if _fallback_pool:
                    logger.info(
                        f"[Cache] Falling back to last known good enriched "
                        f"pool ({len(_fallback_pool)} players)"
                    )
                    pool = _fallback_pool
                else:
                    logger.warning(
                        "[Cache] No previous enriched cache available — "
                        "proceeding with degraded pool (NOT caching to disk)"
                    )

                # ── Retry after delay ─────────────────────────────────
                # For the FIRST failure, attempt a single retry after a
                # cooldown.  The DK circuit breaker may have reset by
                # then.  We do NOT loop indefinitely — one retry, then
                # accept the result.
                #
                # Only retry when enrichment services are actually wired
                # (i.e. not in unit tests with mocked/None services) AND
                # there's no fallback cache to use.  A retry without
                # real services would just fail identically.
                _has_enrich_services = (
                    self.dk_available_players_service is not None
                    or self.simulation_tuning_agent is not None
                )
                if not _fallback_pool and _has_enrich_services:
                    logger.info(
                        f"[Enrich] Waiting {_ENRICHED_RETRY_DELAY_S}s "
                        f"for circuit breaker cooldown before retry..."
                    )
                    time.sleep(_ENRICHED_RETRY_DELAY_S)
                    pool_retry = self._enrich_pool(
                        pool, platform, gd,
                        contest_type=contest_type, sport=_sport,
                        draft_group_id=request.draft_group_id,
                    )
                    _noise_count_retry = getattr(
                        self, "_last_noise_override_count", 0
                    )
                    try:
                        _validate_pool_enrichment(
                            pool_retry,
                            noise_override_count=_noise_count_retry,
                        )
                        logger.info(
                            "[Enrich] Retry PASSED — enrichment recovered"
                        )
                        pool = pool_retry
                        _enrichment_valid = True
                    except DataDegradationError as retry_dde:
                        logger.critical(
                            f"[Enrich] Retry FAILED — still degraded: "
                            f"{retry_dde.message}"
                        )
                        pool = pool_retry  # Use whatever we got

            # ── Persist to caches ─────────────────────────────────────
            with _enriched_lock:
                _enriched_cache[enrich_key] = (time.time(), pool)
            with _strategy_lock:
                _strategy_cache[enrich_key] = (time.time(), self._strategy_adjustments)

            # Only persist enriched pool to FILE cache if validation
            # passed — this protects the "last known good" file from
            # being overwritten by degraded data.
            if _enrichment_valid:
                _save_enriched_pool_to_file(enrich_key, pool)

        # Apply user projection overrides (takes final precedence)
        pool = self._apply_overrides(pool, request.projection_overrides, sport=_sport)

        # Pre-fetch correlation data for stack selection (GPP only)
        if contest_type in ("gpp", "single_entry"):
            self._prefetch_correlations(pool)

        n_requested = request.num_lineups

        # ── Contest-driven strategy override ─────────────────────────
        # If a contest_id was provided, let Agent 2 determine the
        # solver path and scoring weights.  This takes precedence over
        # the user-selected strategy / contest_type for automated builds.
        _contest_id = getattr(request, "contest_id", None)
        if _contest_id:
            _ls = self._resolve_contest_strategy(_contest_id)
            if _ls:
                self._lineup_strategy = _ls
                logger.info(
                    f"[ContestStrategy] {_ls.contest_category}: "
                    f"path={_ls.solver_path}, alpha={_ls.ownership_leverage_alpha:.2f}, "
                    f"weights=({_ls.w_p50:.2f}/{_ls.w_p90:.2f}/{_ls.w_floor:.2f}), "
                    f"source={_ls.source}"
                )
                if _ls.contest_type_override:
                    contest_type = _ls.contest_type_override
                if _ls.solver_path == "sim_optimal":
                    request = request.model_copy(
                        update={"strategy": "sim_optimal"}
                    )
                elif _ls.strategy_override:
                    request = request.model_copy(
                        update={"strategy": _ls.strategy_override}
                    )

        # ── Sim-Optimal early branch ──────────────────────────────────
        # When strategy is "sim_optimal", delegate to a dedicated method
        # that uses Monte Carlo iteration vectors as ILP objective
        # coefficients instead of noise-injected composite scores.
        if request.strategy == "sim_optimal":
            if not _PULP_AVAILABLE:
                raise ValueError(
                    "Sim-optimal requires the PuLP/CBC solver. "
                    "Install with: pip install pulp"
                )
            _game_ctx = getattr(self, "_game_lookup_cache", {})
            if not _game_ctx:
                # Fallback: build game context from DK competitions API
                _game_ctx = self._build_dk_game_context(
                    request.draft_group_id
                )
            if not _game_ctx:
                raise ValueError(
                    "Sim-optimal requires game context data. "
                    "Ensure the game schedule is available for this date."
                )
            try:
                sim_lineups = self._generate_sim_optimal(
                    pool=pool,
                    platform=platform,
                    salary_cap=salary_cap,
                    roster_slots=roster_slots,
                    slot_order=slot_order,
                    request=request,
                    game_lookup=_game_ctx,
                    sport=_sport,
                )
            except Exception as e:
                logger.error(
                    f"[SimOptimal] Failed: {e} — falling back to max_projection",
                    exc_info=True,
                )
                fallback_req = request.model_copy(
                    update={"strategy": "max_projection"}
                )
                return self.generate_lineups(fallback_req)

            # Attach quality grades
            for lu in sim_lineups:
                qs, qg, qw = self._assess_lineup_quality(
                    lu, salary_cap, pool=pool,
                )
                lu.quality_score = qs
                lu.quality_grade = qg
                if qw:
                    lu.warnings.extend(qw)

            elapsed_ms = int((time.time() - start_ms) * 1000)
            return MultiLineupResponse(
                platform=platform,
                sport=_sport,
                lineups=sim_lineups,
                strategy="sim_optimal",
                num_requested=n_requested,
                num_generated=len(sim_lineups),
                pool_size=len(pool),
                generation_time_ms=elapsed_ms,
                num_candidates_generated=len(sim_lineups),
                baseline_projection_score=getattr(
                    self, "_sim_opt_baseline_score", None
                ),
                min_projection_floor=getattr(
                    self, "_sim_opt_min_proj_floor", None
                ),
                baseline_optimal_lineup=getattr(
                    self, "_sim_opt_baseline_lineup", None
                ),
            )

        # ── Phase 1: Compute overgeneration target ───────────────────
        from app.config.constants import OVERSAMPLE_TARGET
        _is_gpp_contest = contest_type in ("gpp", "single_entry")
        if _is_gpp_contest and n_requested >= 20:
            # Oversampling architecture: generate a massive pool, let
            # Phase 4 Portfolio ILP curate the best N with strict overlap.
            internal_count = OVERSAMPLE_TARGET
            multiplier = internal_count / max(1, n_requested)
            logger.info(
                f"[MultiLineup] Oversampling: {n_requested} requested → "
                f"generating {internal_count} candidates (oversample mode)"
            )
        else:
            if n_requested <= 20:
                multiplier = _OVERGEN_MULTIPLIER_SMALL
            elif n_requested <= 80:
                multiplier = _OVERGEN_MULTIPLIER_MEDIUM
            else:
                multiplier = _OVERGEN_MULTIPLIER_LARGE

            internal_count = max(
                int(n_requested * multiplier),
                _OVERGEN_MIN_CANDIDATES,
            )
            internal_count = min(internal_count, _OVERGEN_MAX_CANDIDATES)
            logger.info(
                f"[MultiLineup] Overgeneration: {n_requested} requested → "
                f"generating {internal_count} candidates (×{multiplier})"
            )

        # ── Phase 2: Generate candidate lineups ──────────────────────
        # During overgeneration the exposure penalty is dampened so that
        # later candidates aren't crippled.  The final selection step
        # (Phase 4) handles diversity via max_overlap.
        candidates: List[OptimizedLineup] = []
        exposure: Dict[int, int] = {}
        warnings: List[str] = []
        rng = random.Random(request.seed)  # None = nondeterministic

        # Game stacking setup (GPP only)
        is_gpp = contest_type in ("gpp", "single_entry")
        enable_stacking = getattr(request, "enable_stacking", True) and is_gpp
        salary_floor_pct = getattr(request, "salary_floor_pct", 0.98)

        # ── Dynamic stacking overrides (Prompt 5.1) ─────────────────────
        # Pull user-supplied per-request override values once and pass
        # them down through every ILP call site. ``None`` means "use the
        # SportConfig default" so partial overrides (e.g., set bring-back
        # off but leave QB min unchanged) work transparently.
        stack_overrides: Dict[str, Any] = {
            "primary_stack_size": getattr(request, "primary_stack_size", None),
            "secondary_stack_size": getattr(request, "secondary_stack_size", None),
            "require_bring_back": getattr(request, "require_bring_back", None),
        }
        if any(v is not None for v in stack_overrides.values()):
            logger.info(
                f"[MultiLineup] Stack overrides active: {stack_overrides}"
            )

        # Small-slate guard: when the slate has very few teams, the salary
        # cap is unreachable with the available pool — relax the floor so
        # the ILP stays feasible. Threshold and floor cap come from the
        # SportConfig (NBA disables this with threshold=0; CBB triggers it
        # at <=6 teams with a 60% floor cap).
        from app.sports import get_config as _get_sport_cfg
        _ss_cfg = _get_sport_cfg(_sport)
        _num_teams = len(set(p.team_abbreviation for p in pool))
        if _ss_cfg.small_slate_team_threshold > 0 and _num_teams <= _ss_cfg.small_slate_team_threshold:
            salary_floor_pct = min(salary_floor_pct, _ss_cfg.small_slate_min_salary_floor_pct)
            logger.info(
                f"[MultiLineup] {_sport.upper()} small slate ({_num_teams} teams): "
                f"relaxed salary floor to {salary_floor_pct:.0%}"
            )

        salary_floor = int(salary_cap * salary_floor_pct) if salary_floor_pct > 0 else 0

        # ── Optimality Floor: baseline solve + threshold ───────────────
        min_projection_floor = None
        baseline_projection_score = None
        baseline_optimal_lineup: Optional[OptimizedLineup] = None
        _opt_threshold = getattr(request, "optimality_threshold", None)
        if _opt_threshold is not None and _PULP_AVAILABLE:
            baseline_projection_score, baseline_optimal_lineup = (
                self._compute_baseline_projection_score(
                    pool=pool,
                    platform=platform,
                    salary_cap=salary_cap,
                    slot_order=slot_order,
                    locked_player_ids=list(request.locked_players or []),
                    salary_floor=salary_floor,
                    mode=mode,
                    sport=_sport,
                )
            )
            if baseline_projection_score and baseline_projection_score > 0:
                min_projection_floor = baseline_projection_score * _opt_threshold
                logger.info(
                    f"[MultiLineup] Optimality floor: "
                    f"baseline={baseline_projection_score:.1f}, "
                    f"threshold={_opt_threshold}, "
                    f"floor={min_projection_floor:.1f}"
                )
            else:
                logger.warning(
                    "[MultiLineup] Baseline projection solve failed — "
                    "optimality floor disabled for this run"
                )

        # ── Projected FP quality floor (post-generation gate) ────────────
        # Separate from the ILP C7b constraint (which is disabled because it
        # makes constrained ILPs infeasible).  This floor applies AFTER
        # generation, catching degenerate lineups that slip through the
        # heavily-constrained solver with very low total projected FP.
        from app.config.constants import LINEUP_QUALITY_MIN_PROJECTION_PCT

        _min_projected_fp: Optional[float] = None
        if baseline_projection_score and baseline_projection_score > 0:
            _min_projected_fp = baseline_projection_score * LINEUP_QUALITY_MIN_PROJECTION_PCT
            logger.info(
                f"[MultiLineup] Projected FP floor: "
                f"baseline={baseline_projection_score:.1f} × "
                f"{LINEUP_QUALITY_MIN_PROJECTION_PCT:.0%} = "
                f"{_min_projected_fp:.1f}"
            )

        # Exposure limit: max_exposure (0.1-1.0) limits per-player appearance %.
        # Values <= 0 are degenerate (no feasible lineup) — treat as disabled
        # rather than silently clamping to 1 appearance.
        max_exposure = getattr(request, "max_exposure", None)
        max_appearances = (
            max(1, int(max_exposure * n_requested))
            if max_exposure is not None and max_exposure > 0 else None
        )
        if max_appearances is not None:
            logger.info(
                f"[MultiLineup] Exposure limit: {max_exposure:.0%} → "
                f"max {max_appearances} appearances per player"
            )

        # Per-player exposure limits: player_id → absolute max/min appearances
        _player_max_expo_raw: Dict[int, float] = getattr(
            request, "player_max_exposure", {}
        ) or {}
        _player_min_expo_raw: Dict[int, float] = getattr(
            request, "player_min_exposure", {}
        ) or {}

        # ── GPP auto-exposure: tier-based caps + top-player min floors ──
        if is_gpp:
            _player_max_expo_raw = self._compute_auto_exposure_caps(
                pool, contest_type, max_exposure, _player_max_expo_raw,
            )
            _player_min_expo_raw = self._compute_auto_min_exposure(
                pool, contest_type, _player_min_expo_raw,
            )

        # ── GPP contrarian slot assignments ─────────────────────────────
        _contrarian_locks: List[Optional[int]] = []
        if is_gpp:
            _contrarian_locks = self._select_contrarian_locks(
                pool, n_requested, rng,
            )

        _player_max_appearances: Dict[int, int] = {
            int(pid): max(1, int(frac * n_requested))
            for pid, frac in _player_max_expo_raw.items()
            if 0.0 < frac <= 1.0
        }
        _player_min_appearances: Dict[int, int] = {
            int(pid): max(1, int(frac * n_requested))
            for pid, frac in _player_min_expo_raw.items()
            if 0.0 < frac <= 1.0
        }
        if _player_max_appearances:
            logger.info(
                f"[MultiLineup] Per-player max exposure: "
                f"{len(_player_max_appearances)} players capped"
            )
        if _player_min_appearances:
            logger.info(
                f"[MultiLineup] Per-player min exposure: "
                f"{len(_player_min_appearances)} player targets"
            )

        # Cumulative ownership cap
        _max_cum_own: Optional[float] = getattr(
            request, "max_cumulative_ownership", None
        )
        if _max_cum_own is not None and _max_cum_own > 0:
            logger.info(
                f"[MultiLineup] Cumulative ownership cap: {_max_cum_own:.1f}%"
            )

        # ── Improvement #8: Compute slate-average game total ──────────
        # Used by cross-game affinity scoring in _compute_composite_score.
        _game_totals_map: Dict[str, float] = {}
        for p in pool:
            if p.game_id and p.game_total:
                try:
                    _game_totals_map[p.game_id] = float(p.game_total)
                except (ValueError, TypeError):
                    pass
        self._slate_avg_game_total = (
            sum(_game_totals_map.values()) / len(_game_totals_map)
            if _game_totals_map
            else 0.0
        )
        if self._slate_avg_game_total > 0:
            logger.info(
                f"[MultiLineup] Slate avg game total: {self._slate_avg_game_total:.1f} "
                f"({len(_game_totals_map)} games)"
            )

        # ── Improvement #5: Slate-size adaptive parameters ───────────
        self._slate_adjustments = self._compute_slate_adjustments(
            pool, contest_type
        )

        # ── Phase 1 Pool Pruning: Junk Player Guillotine ────────────
        # Remove sub-threshold players from the pool at the earliest
        # stage so they never enter composite scoring, K-Best, or ILP.
        #
        # Thresholds are sport-aware (Prompt 7.13). The original
        # constants (20 FP floor / 5.0x value) are NBA-tuned and would
        # prune 100% of an MLB pool: hitters typically project 8–14 FP
        # at salary $3K–$5K (value ratio ~2–3x). Same problem for NFL
        # where most non-stud players sit below 20 FP. Each sport gets
        # its own floor / value pair so the prune step keeps the
        # legitimate value plays for that sport.
        from app.config.constants import (
            KBEST_PROJECTION_FLOOR,
            KBEST_PROJECTION_FLOOR_VALUE_EXEMPT,
        )
        _SPORT_PRUNE_THRESHOLDS = {
            "nba": (KBEST_PROJECTION_FLOOR,        KBEST_PROJECTION_FLOOR_VALUE_EXEMPT),
            "cbb": (KBEST_PROJECTION_FLOOR,        KBEST_PROJECTION_FLOOR_VALUE_EXEMPT),
            "mlb": (5.0,  1.5),   # hitters 5 FP min; salary exemption at 1.5x
            "nfl": (5.0,  1.5),   # most NFL skill players below 20 FP
        }
        _floor, _value_exempt = _SPORT_PRUNE_THRESHOLDS.get(
            _sport, (KBEST_PROJECTION_FLOOR, KBEST_PROJECTION_FLOOR_VALUE_EXEMPT),
        )
        _locked_ids = set(request.locked_players or [])
        _pool_before = len(pool)
        pool = [
            p for p in pool
            if p.player_id in _locked_ids
            or p.projected_fp >= _floor
            or (
                p.salary and p.salary > 0 and p.projected_fp
                and (p.projected_fp / (p.salary / 1000)) > _value_exempt
            )
        ]
        _pool_pruned = _pool_before - len(pool)
        if _pool_pruned > 0:
            logger.info(
                f"[MultiLineup] Phase 1 pool prune ({_sport}): {_pool_before} → "
                f"{len(pool)} ({_pool_pruned} below {_floor:.0f} FP / "
                f"{_value_exempt:.1f}x value)"
            )

        # ── Dynamic value threshold for ownership dampener ────────────
        # Compute slate-relative threshold so injury-heavy slates where
        # most value plays are at 5.0-5.5x don't get fully penalized.
        self._slate_value_threshold, self._slate_value_ceiling = (
            self._calculate_slate_value_threshold(pool)
        )

        # Compute elite core player IDs for portfolio-level exemptions.
        # These players are subtracted from portfolio ILP overlap counts
        # and exempted from quadratic exposure decay.
        #
        # Two qualification paths:
        #   1. Elite Core (value_ratio > 6.5) — original path
        #   2. Mega Chalk (value_ratio > 6.0 OR ownership > 50%) — broader
        #      net for players the field will own at extreme rates.
        # Both paths merge into the same _elite_core_pids set so the
        # entire downstream pipeline (conflict graph, exposure filter)
        # treats them identically.
        from app.config.constants import (
            ELITE_CORE_VALUE_THRESHOLD as _ECVT,
            MEGA_CHALK_VALUE_THRESHOLD,
            MEGA_CHALK_OWNERSHIP_THRESHOLD,
        )
        self._elite_core_pids: set = set()
        self._mega_chalk_pids: set = set()   # track separately for logging
        for p in pool:
            _vr = 0.0
            if (
                p.salary and p.salary > 0
                and p.projected_fp and p.projected_fp > 0
            ):
                _vr = p.projected_fp / (p.salary / 1000)

            # Path 1: Elite Core (strict value threshold)
            if _vr > _ECVT:
                self._elite_core_pids.add(p.player_id)
                continue

            # Path 2: Mega Chalk (lower value bar OR high ownership)
            _is_mega_chalk = (
                _vr > MEGA_CHALK_VALUE_THRESHOLD
                or (
                    p.estimated_ownership is not None
                    and p.estimated_ownership >= MEGA_CHALK_OWNERSHIP_THRESHOLD
                )
            )
            if _is_mega_chalk:
                self._mega_chalk_pids.add(p.player_id)
                self._elite_core_pids.add(p.player_id)

        if self._elite_core_pids:
            _ec_only = self._elite_core_pids - self._mega_chalk_pids
            _ec_names = [
                p.player_name for p in pool
                if p.player_id in _ec_only
            ]
            _mc_names = [
                f"{p.player_name} (${p.salary:,} | "
                f"val={p.projected_fp / (p.salary / 1000):.1f}x | "
                f"own={p.estimated_ownership or 0:.0f}%)"
                for p in pool
                if p.player_id in self._mega_chalk_pids
            ]
            if _ec_names:
                logger.info(
                    f"[MultiLineup] Elite Core (portfolio-exempt): "
                    f"{', '.join(_ec_names)}"
                )
            if _mc_names:
                logger.info(
                    f"[MultiLineup] Mega Chalk Exemption granted: "
                    f"{', '.join(_mc_names)}"
                )

        # ── Team value density map for correlation stacking ──────────
        # For each team, count how many pool players exceed the
        # correlation stack min value threshold.  Teams with 2+ such
        # players are "stackable" — their players get a per-player
        # composite score bonus so the K-Best ILP naturally pairs them.
        from app.config.constants import CORRELATION_STACK_MIN_AVG_VALUE
        _team_hv_counts: Dict[str, int] = {}
        for p in pool:
            if (
                p.salary and p.salary > 0
                and p.projected_fp and p.projected_fp > 0
            ):
                vr = p.projected_fp / (p.salary / 1000)
                if vr >= CORRELATION_STACK_MIN_AVG_VALUE:
                    team = (p.team_abbreviation or "").upper()
                    _team_hv_counts[team] = _team_hv_counts.get(team, 0) + 1
        # Only mark teams with 2+ high-value players as stackable
        self._stackable_teams: set = {
            t for t, c in _team_hv_counts.items() if c >= 2
        }
        if self._stackable_teams:
            logger.info(
                f"[MultiLineup] Stackable teams (2+ players ≥ "
                f"{CORRELATION_STACK_MIN_AVG_VALUE:.1f}x): "
                f"{', '.join(sorted(self._stackable_teams))}"
            )

        # ── Blowout script allocation (Portfolio Diversity) ───────────
        # Reserve a fraction of candidates for lineups stacked toward
        # blowout games (large |spread|).  These capture bench-value
        # upside when deep-roster players log garbage-time minutes.
        from app.config.constants import (
            BLOWOUT_SCRIPT_PCT,
            BLOWOUT_SPREAD_THRESHOLD,
        )

        _blowout_games: List[dict] = []
        if enable_stacking:
            # Identify games with extreme spreads
            _seen_blowout_games: Dict[str, dict] = {}
            for p in pool:
                gid = p.game_id
                if not gid or gid in _seen_blowout_games:
                    continue
                spread = getattr(p, "vegas_spread", None)
                if spread is not None and abs(spread) >= BLOWOUT_SPREAD_THRESHOLD:
                    _seen_blowout_games[gid] = {
                        "game_id": gid,
                        "teams": set(),
                        "game_total": getattr(p, "game_total", None) or 0,
                        "spread": spread,
                    }
                if gid in _seen_blowout_games:
                    _seen_blowout_games[gid]["teams"].add(
                        p.team_abbreviation.upper()
                    )
            _blowout_games = [
                g for g in _seen_blowout_games.values()
                if len(g["teams"]) >= 2
            ]

        _blowout_reserved = (
            max(1, int(internal_count * BLOWOUT_SCRIPT_PCT))
            if _blowout_games and is_gpp
            else 0
        )
        # Blowout candidates are the LAST N indices in the loop
        _blowout_start_idx = internal_count - _blowout_reserved
        if _blowout_reserved > 0:
            logger.info(
                f"[MultiLineup] Blowout script allocation: "
                f"{_blowout_reserved}/{internal_count} candidates reserved "
                f"({len(_blowout_games)} blowout games detected)"
            )

        # ── Phase 2: K-Best iterative ILP generation ─────────────────
        # Build PuLP prob once per stack target per Gaussian noise seed,
        # then iteratively solve and add exclusion constraints to generate
        # diverse lineups efficiently.  Replaces the old per-candidate
        # parallel dispatch with a structured K-Best loop.
        from app.config.constants import (
            MULTI_LINEUP_TIME_BUDGET,
            MULTI_LINEUP_PARALLEL_WORKERS,
            KBEST_MAX_OVERLAP,
        )

        _phase2_start = time.time()
        _time_budget = MULTI_LINEUP_TIME_BUDGET

        # Shared exposure state for cross-worker coordination
        from app.config.constants import EXPOSURE_PENALTY_DEFAULT_CAP
        _shared_exposure = _SharedExposureCounts()
        from app.config.constants import ABSOLUTE_GLOBAL_MAX_EXPOSURE
        _eff_max_exposure: float = min(
            max_exposure if max_exposure is not None else EXPOSURE_PENALTY_DEFAULT_CAP,
            ABSOLUTE_GLOBAL_MAX_EXPOSURE,
        )

        # Step 1: Determine stack target allocation
        stack_targets: List[dict] = []
        if enable_stacking:
            _game_pool = self._get_stackable_game_pool(pool)
            if _game_pool:
                _assignments = self._allocate_stack_targets(
                    _game_pool, internal_count, rng,
                )

                # Reserve blowout allocations
                _blowout_alloc = 0
                if _blowout_reserved > 0 and _blowout_games:
                    for bg in _blowout_games:
                        _bg_teams = sorted(bg["teams"])
                        _fav_team = None
                        for p in pool:
                            if p.game_id == bg["game_id"]:
                                sp = getattr(p, "vegas_spread", None)
                                if sp is not None and sp < 0:
                                    _fav_team = p.team_abbreviation.upper()
                                    break
                        _bg_primary = (
                            _fav_team
                            if _fav_team
                            and _fav_team in (
                                _bg_teams[0],
                                _bg_teams[1] if len(_bg_teams) > 1 else _bg_teams[0],
                            )
                            else rng.choice(_bg_teams)
                        )
                        _bg_sz, _bg_bb = self._compute_dynamic_stack_params(
                            rng, pool, {
                                "game_id": bg["game_id"],
                                "team_a": _bg_teams[0],
                                "team_b": _bg_teams[1] if len(_bg_teams) > 1 else _bg_teams[0],
                                "game_total": bg["game_total"],
                            }, contest_type,
                        )
                        _blowout_ct = max(
                            2, _blowout_reserved // max(1, len(_blowout_games)),
                        )
                        stack_targets.append({
                            "game_id": bg["game_id"],
                            "primary_team": _bg_primary,
                            "size": _bg_sz,
                            "bring_back": _bg_bb,
                            "target_count": _blowout_ct,
                        })
                        _blowout_alloc += _blowout_ct

                # Normal stack targets (reduce by blowout allocation)
                _normal_budget = max(0, internal_count - _blowout_alloc)
                if _normal_budget > 0 and _assignments:
                    _normal_total = sum(c for _, c in _assignments)
                    for game, count in _assignments:
                        # Scale down proportionally by blowout reservation
                        _adj_count = max(
                            2,
                            int(count * (_normal_budget / max(1, _normal_total))),
                        )
                        _spt = rng.choice([game["team_a"], game["team_b"]])
                        _ssz, _sbb = self._compute_dynamic_stack_params(
                            rng, pool, game, contest_type,
                        )
                        # Improvement #3: Secondary game stack
                        _sec_gid = self._select_secondary_stack_game(
                            _game_pool, game["game_id"],
                        )
                        stack_targets.append({
                            "game_id": game["game_id"],
                            "primary_team": _spt,
                            "size": _ssz,
                            "bring_back": _sbb,
                            "target_count": _adj_count,
                            "secondary_game_id": _sec_gid,
                        })
            else:
                # No viable games for stacking
                stack_targets.append({
                    "game_id": None,
                    "primary_team": None,
                    "size": 0,
                    "bring_back": False,
                    "target_count": internal_count,
                })
        else:
            # No stacking: single K-Best loop
            stack_targets.append({
                "game_id": None,
                "primary_team": None,
                "size": 0,
                "bring_back": False,
                "target_count": internal_count,
            })

        # ── Stud-lock injection ──────────────────────────────────────
        # For top projected players with min exposure targets, inject
        # dedicated stack targets with force-locks to guarantee enough
        # candidates exist for Portfolio ILP min exposure constraints.
        _stud_lock_map: Dict[int, int] = {}  # {player_id: game_id}
        if is_gpp and _player_min_appearances:
            # Build player_id → game_id mapping
            _pid_to_game: Dict[int, Optional[str]] = {
                p.player_id: p.game_id for p in pool
            }
            _pid_to_name: Dict[int, str] = {
                p.player_id: p.player_name for p in pool
            }

            for _sl_pid, _sl_min in sorted(
                _player_min_appearances.items(),
                key=lambda x: -x[1],
            ):
                _sl_game = _pid_to_game.get(_sl_pid)
                if not _sl_game:
                    continue
                # Allocate stud-lock targets: min_count lineups
                # Split from existing stack targets for this game
                _sl_count = min(_sl_min, internal_count // 3)
                if _sl_count < 3:
                    continue

                # Find matching game in stack targets
                _sl_matched = False
                for _st_idx, _st in enumerate(stack_targets):
                    if _st["game_id"] == _sl_game:
                        # Steal lineups from this target for stud-lock
                        _steal = min(
                            _sl_count,
                            _st["target_count"] // 2,
                        )
                        if _steal >= 3:
                            _st["target_count"] -= _steal
                            stack_targets.append({
                                **_st,
                                "target_count": _steal,
                                "_stud_lock_pid": _sl_pid,
                            })
                            _stud_lock_map[_sl_pid] = _sl_game
                            logger.info(
                                f"[StudLock] Dedicated {_steal} candidates "
                                f"for {_pid_to_name.get(_sl_pid, _sl_pid)} "
                                f"(pid={_sl_pid}, min_target={_sl_min})"
                            )
                            _sl_matched = True
                            break

                if not _sl_matched:
                    # No matching game target — create a new unstacked
                    # target with force-lock
                    stack_targets.append({
                        "game_id": _sl_game,
                        "primary_team": None,
                        "size": 0,
                        "bring_back": False,
                        "target_count": min(_sl_count, 20),
                        "_stud_lock_pid": _sl_pid,
                    })
                    _stud_lock_map[_sl_pid] = _sl_game
                    logger.info(
                        f"[StudLock] New unstacked target: "
                        f"{min(_sl_count, 20)} candidates for "
                        f"{_pid_to_name.get(_sl_pid, _sl_pid)} "
                        f"(pid={_sl_pid})"
                    )

            # Recalculate internal_count
            internal_count = sum(st["target_count"] for st in stack_targets)

        logger.info(
            f"[KBest] Phase 2 dispatching {len(stack_targets)} stack "
            f"targets for {internal_count} total candidates"
        )

        # Step 2: Dispatch K-Best loops (parallel across stack targets)
        # ── MLB / NFL direct-ILP fast path (Prompt 7.14) ─────────────
        # K-Best was designed around NBA's 8-slot roster + game-stack
        # diversification model. For MLB it routinely hangs because
        # the strict 5-stack + pitcher-fade constraint stack
        # interacts badly with K-Best's overlap-exclusion: after a
        # few iterations the eligible-hitters pool shrinks below the
        # 5-per-team threshold and CBC burns the time budget proving
        # infeasibility.
        #
        # The path below sidesteps K-Best entirely. It iterates
        # ``_ilp_optimize`` directly — the same call site that the
        # unit tests in test_sport_config.py exercise successfully
        # for MLB stacking (Prompt 4.1: 10/10 lineups generated).
        # Diversification comes from per-iteration projection noise
        # plus a soft "previously-used" penalty injected into the
        # score function. Time-bounded so it can't hang.
        if _sport in ("mlb", "nfl"):
            logger.info(
                f"[MultiLineup/{_sport.upper()}] Bypassing K-Best — "
                f"direct ILP iteration with {_sport.upper()}-aware "
                f"stack constraints. Target: {internal_count} candidates, "
                f"time budget: {_time_budget:.0f}s"
            )

            _direct_start = time.time()
            _used_pids: Dict[int, int] = {}  # pid -> times used
            _direct_seen: set = set()  # lineup-fingerprint dedup
            _direct_candidates: List[OptimizedLineup] = []
            _consec_failures = 0
            _MAX_CONSEC_FAILURES = 8

            for _attempt in range(internal_count * 3):
                if len(_direct_candidates) >= internal_count:
                    break
                _elapsed = time.time() - _direct_start
                if _elapsed >= _time_budget:
                    logger.info(
                        f"[MultiLineup/{_sport.upper()}] Time budget "
                        f"exhausted ({_elapsed:.1f}s) — produced "
                        f"{len(_direct_candidates)}/{internal_count} candidates"
                    )
                    break

                _att_rng = random.Random(rng.randint(0, 2**31))

                def _direct_score_fn(p, _r=_att_rng, _u=_used_pids):
                    base = self._effective_projection(p)
                    # Soft diversity penalty — each prior selection
                    # docks 0.3 FP. A 4× over-used hitter loses 1.2 FP,
                    # enough to flip the optimizer to a fresh stack.
                    penalty = 0.3 * _u.get(p.player_id, 0)
                    # Tiny Gaussian noise so two attempts with the same
                    # used-pids state still produce different rankings.
                    noise = _r.gauss(0, base * 0.03) if base > 0 else 0.0
                    return base - penalty + noise

                try:
                    _att_result = self._ilp_optimize(
                        pool=pool,
                        platform=platform,
                        salary_cap=salary_cap,
                        slot_order=slot_order,
                        locked_player_ids=list(request.locked_players or []),
                        score_fn=_direct_score_fn,
                        salary_floor=salary_floor,
                        mode=mode,
                        sport=_sport,
                        contest_type=contest_type,
                        time_limit=8,
                        enable_stacking=enable_stacking,
                        stack_overrides=stack_overrides,
                    )
                except Exception as exc:
                    logger.debug(
                        f"[MultiLineup/{_sport.upper()}] ILP error "
                        f"on attempt {_attempt}: {exc}"
                    )
                    _consec_failures += 1
                    if _consec_failures >= _MAX_CONSEC_FAILURES:
                        logger.warning(
                            f"[MultiLineup/{_sport.upper()}] "
                            f"{_consec_failures} consecutive ILP failures "
                            f"— stopping early"
                        )
                        break
                    continue

                if not _att_result or len(_att_result) != len(slot_order):
                    _consec_failures += 1
                    if _consec_failures >= _MAX_CONSEC_FAILURES:
                        logger.warning(
                            f"[MultiLineup/{_sport.upper()}] "
                            f"{_consec_failures} consecutive infeasible "
                            f"solves — stopping early"
                        )
                        break
                    continue

                _consec_failures = 0
                _opt_lineup = self._build_lineup_from_assignment(
                    lineup=_att_result,
                    platform=platform,
                    salary_cap=salary_cap,
                    roster_slots=roster_slots,
                    sport=_sport,
                )
                if _opt_lineup is None:
                    continue

                # Dedup on player-set fingerprint
                _fp = frozenset(p.player_id for p in _opt_lineup.players)
                if _fp in _direct_seen:
                    continue
                _direct_seen.add(_fp)
                _direct_candidates.append(_opt_lineup)
                # Bump usage count so subsequent iterations diversify
                for _p in _opt_lineup.players:
                    _used_pids[_p.player_id] = _used_pids.get(_p.player_id, 0) + 1

            logger.info(
                f"[MultiLineup/{_sport.upper()}] Direct-ILP path "
                f"produced {len(_direct_candidates)} candidates in "
                f"{time.time() - _direct_start:.1f}s"
            )
            candidates.extend(_direct_candidates)
            # Skip the K-Best ThreadPoolExecutor block by emptying
            # stack_targets — no futures will be submitted.
            stack_targets = []

        # CBC releases the GIL during solve so threads run truly parallel.
        def _kbest_worker(
            stack_cfg: dict,
            seed_offset: int,
        ) -> List:
            per_stack_budget = max(
                10.0,
                _time_budget * (
                    stack_cfg["target_count"] / max(1, internal_count)
                ),
            )
            # Merge stud-lock into locked players if present
            _worker_locked = list(request.locked_players or [])
            _sl_pid = stack_cfg.get("_stud_lock_pid")
            if _sl_pid is not None and _sl_pid not in _worker_locked:
                _worker_locked.append(_sl_pid)
            return self._kbest_generate_for_stack(
                pool=pool,
                platform=platform,
                salary_cap=salary_cap,
                roster_slots=roster_slots,
                slot_order=slot_order,
                locked_player_ids=_worker_locked,
                stack_config=stack_cfg,
                strategy=request.strategy,
                contest_type=contest_type,
                sport=_sport,
                mode=mode,
                target_count=stack_cfg["target_count"],
                max_overlap=(
                    self._slate_adjustments.get("max_overlap_override")
                    or KBEST_MAX_OVERLAP
                ) if self._slate_adjustments else KBEST_MAX_OVERLAP,
                time_budget=per_stack_budget,
                master_seed=rng.randint(0, 2**31) + seed_offset,
                min_projected_fp=_min_projected_fp,
                salary_floor_pct=salary_floor_pct,
                # Exposure-aware generation
                shared_exposure=_shared_exposure,
                n_requested_final=n_requested,
                max_exposure=_eff_max_exposure,
                player_max_exposure=_player_max_expo_raw or None,
                # Per-request stacking overrides (Prompt 5.1)
                stack_overrides=stack_overrides,
            )

        _gen_pool = ThreadPoolExecutor(
            max_workers=MULTI_LINEUP_PARALLEL_WORKERS,
            thread_name_prefix="kbest-gen",
        )
        futures = {
            _gen_pool.submit(_kbest_worker, st, i): i
            for i, st in enumerate(stack_targets)
        }
        for future in as_completed(futures):
            # Collect the result FIRST, then check the time budget for
            # subsequent futures.  Previously the time check fired before
            # future.result(), discarding a full batch of valid lineups
            # when the K-Best worker finished just past the budget.
            try:
                remaining_budget = max(
                    5.0,
                    _time_budget * 1.1 - (time.time() - _phase2_start),
                )
                batch = future.result(timeout=remaining_budget)
                candidates.extend(batch)
                for lu in batch:
                    for p in lu.players:
                        exposure[p.player_id] = (
                            exposure.get(p.player_id, 0) + 1
                        )
            except LineupGenerationError:
                for f in futures:
                    f.cancel()
                _gen_pool.shutdown(wait=False)
                raise
            except Exception as e:
                logger.warning(
                    f"[KBest] Stack worker failed: {e}", exc_info=True,
                )
            # Now check time budget for remaining futures
            elapsed = time.time() - _phase2_start
            if elapsed > _time_budget:
                for f in futures:
                    f.cancel()
                logger.warning(
                    f"[KBest] Time budget exhausted "
                    f"({elapsed:.1f}s > {_time_budget:.0f}s), "
                    f"collected {len(candidates)} candidates"
                )
                break

        _gen_pool.shutdown(wait=False)

        _phase2_elapsed = time.time() - _phase2_start
        _exp_snap = _shared_exposure.snapshot()
        if _exp_snap:
            _max_exp_count = max(_exp_snap.values())
            _max_exp_pct = _max_exp_count / max(n_requested, 1)
            logger.info(
                f"[KBest] Phase 2 completed in {_phase2_elapsed:.1f}s: "
                f"{len(candidates)} candidates from "
                f"{len(stack_targets)} stack targets | "
                f"Exposure: max={_max_exp_count} ({_max_exp_pct:.0%}), "
                f"unique_players={len(_exp_snap)}"
            )
        else:
            logger.info(
                f"[KBest] Phase 2 completed in {_phase2_elapsed:.1f}s: "
                f"{len(candidates)} candidates from "
                f"{len(stack_targets)} stack targets"
            )

        if not candidates:
            logger.warning(
                f"[KBest] ALL stack targets produced 0 candidates! "
                f"({len(stack_targets)} targets, {internal_count} requested) "
                f"-- falling through to fill loop. "
                f"Pool size: {len(pool)}"
            )
            # Don't return empty — let Phases 4b/4c try to generate
            # individual lineups via the fill loop.  This recovers from
            # time-budget exhaustion, bad stacking configs, and thin slates.

        # ── Phase 2.5: Salary utilization gate ───────────────────────
        # Discard candidates that fail to use enough of the salary cap.
        # Use the configured hard floor (95%) when we have plenty of
        # candidates.  If the candidate pool is already tight (< 2× the
        # request), soften to 92% so we don't starve the downstream
        # diversity selector.
        from app.config.constants import (
            SALARY_UTILIZATION_HARD_FLOOR,
            MIN_SALARY_FLOOR,
        )

        if salary_cap > 0:
            # Absolute minimum: MIN_SALARY_FLOOR ($49,300) as a fraction
            _abs_min_pct = MIN_SALARY_FLOOR / salary_cap
            _sal_floor_hard = SALARY_UTILIZATION_HARD_FLOOR
            # Adaptive: soften when candidate pool is thin, but NEVER
            # below the absolute dollar floor
            if len(candidates) < n_requested * 2:
                _sal_floor_hard = max(
                    _abs_min_pct,
                    min(_sal_floor_hard, 0.92),
                )
                logger.info(
                    f"[MultiLineup] Salary gate softened to "
                    f"{_sal_floor_hard:.0%} (thin pool: "
                    f"{len(candidates)} candidates for "
                    f"{n_requested} requested)"
                )
            _pre_sal_count = len(candidates)
            candidates = [
                lu for lu in candidates
                if (lu.total_salary / salary_cap) >= _sal_floor_hard
            ]
            _sal_dropped = _pre_sal_count - len(candidates)
            if _sal_dropped > 0:
                logger.info(
                    f"[MultiLineup] Salary utilization gate: dropped "
                    f"{_sal_dropped} candidates below "
                    f"{_sal_floor_hard:.0%} salary cap utilization "
                    f"({len(candidates)} remaining)"
                )

        # ── Phase 3: Score all candidates & apply relative quality floor ─
        _phase3_start = time.time()
        from app.config.constants import (
            LINEUP_QUALITY_RELATIVE_FLOOR,
            LINEUP_QUALITY_RELATIVE_FLOOR_PURE_MAX,
        )

        scored_candidates: List[Tuple[OptimizedLineup, float]] = [
            (lu, self._score_lineup(lu, pool, request.strategy, contest_type, salary_cap))
            for lu in candidates
        ]

        # Drop candidates whose score falls below the relative floor.
        # Pure max uses a tighter floor since its minimal noise means
        # low-scoring candidates are genuinely weak, not unlucky rolls.
        # Adaptive: when the candidate pool is tight (< 2× requested),
        # soften the floor so we don't starve the diversity selector.
        _quality_floor = (
            LINEUP_QUALITY_RELATIVE_FLOOR_PURE_MAX
            if request.strategy == "pure_max"
            else LINEUP_QUALITY_RELATIVE_FLOOR
        )
        if len(scored_candidates) < n_requested * 2:
            _quality_floor = max(0.50, _quality_floor - 0.15)
            logger.info(
                f"[MultiLineup] Quality floor softened to "
                f"{_quality_floor:.0%} (thin pool: "
                f"{len(scored_candidates)} candidates for "
                f"{n_requested} requested)"
            )
        floor_score = 0.0
        if scored_candidates:
            best_candidate_score = max(sc for _, sc in scored_candidates)
            floor_score = best_candidate_score * _quality_floor
            pre_filter = len(scored_candidates)
            scored_candidates = [
                (lu, sc) for lu, sc in scored_candidates
                if sc >= floor_score
            ]
            dropped = pre_filter - len(scored_candidates)
            if dropped > 0:
                logger.info(
                    f"[MultiLineup] Dropped {dropped} candidates below "
                    f"relative quality floor ({_quality_floor:.0%} "
                    f"of best={best_candidate_score:.1f})"
                )

        # ── Raw projected FP floor (safety net) ──────────────────────
        # Catches any low-FP lineups that slipped through Phase 2 or
        # arrived via a code path that didn't apply the gate.
        if _min_projected_fp and _min_projected_fp > 0 and scored_candidates:
            _pre_fp_count = len(scored_candidates)
            scored_candidates = [
                (lu, sc) for lu, sc in scored_candidates
                if lu.total_projected_fp >= _min_projected_fp
            ]
            _fp_dropped = _pre_fp_count - len(scored_candidates)
            if _fp_dropped > 0:
                logger.info(
                    f"[MultiLineup] Phase 3 FP floor: dropped "
                    f"{_fp_dropped} candidates below "
                    f"{_min_projected_fp:.1f} projected FP "
                    f"({len(scored_candidates)} remaining)"
                )

        logger.info(
            f"[MultiLineup] Phase 3 completed in "
            f"{time.time() - _phase3_start:.1f}s: "
            f"{len(scored_candidates)} scored candidates remain"
        )

        # ── Phase 4: Select best N with diversity enforcement ────────
        # Try joint portfolio ILP when conditions are met (GPP, enough
        # lineups, PuLP available, candidate count reasonable).
        _phase4_start = time.time()
        from app.config.constants import (
            PORTFOLIO_ILP_MAX_CANDIDATES,
            PORTFOLIO_ILP_MIN_LINEUPS,
        )
        selected: List[OptimizedLineup] = []
        use_ilp = (
            _PULP_AVAILABLE
            and is_gpp
            and n_requested >= PORTFOLIO_ILP_MIN_LINEUPS
            and len(scored_candidates) <= PORTFOLIO_ILP_MAX_CANDIDATES
        )
        if use_ilp:
            ilp_result = self._portfolio_optimize(
                scored_candidates,
                n_requested,
                max_overlap=request.max_overlap,
                player_min_appearances=_player_min_appearances or None,
                elite_core_pids=getattr(self, "_elite_core_pids", None),
            )
            if ilp_result is not None:
                selected = ilp_result
                logger.info(
                    f"[MultiLineup] Portfolio ILP selected {len(selected)} lineups"
                )

        # Greedy fallback (or primary path for cash / small requests)
        if not selected:
            selected = self._select_best_diverse(
                scored_candidates, n_requested, request.max_overlap,
            )

        # ── Phase 4a-post: Per-player exposure filter ────────────────
        # Walk the selected list and remove lineups that push any
        # player above their per-player max_exposure cap.  Removed
        # slots become shortfall that the fill loop (Phase 4b) fills
        # with exposure-aware generation.
        # Elite Core players are exempt from this filter — their
        # dominant value warrants uncapped exposure.
        _ec_pids = getattr(self, "_elite_core_pids", None) or set()
        if _player_max_appearances and selected:
            _expo_filter_counts: Dict[int, int] = {}
            _expo_filtered: List[OptimizedLineup] = []
            _expo_dropped = 0
            for lu in selected:
                _lu_pids = [p.player_id for p in lu.players]
                _would_exceed = False
                for pid in _lu_pids:
                    if pid in _player_max_appearances and pid not in _ec_pids:
                        if (
                            _expo_filter_counts.get(pid, 0) + 1
                            > _player_max_appearances[pid]
                        ):
                            _would_exceed = True
                            break
                if _would_exceed:
                    _expo_dropped += 1
                else:
                    _expo_filtered.append(lu)
                    for pid in _lu_pids:
                        _expo_filter_counts[pid] = (
                            _expo_filter_counts.get(pid, 0) + 1
                        )
            if _expo_dropped:
                selected = _expo_filtered
                warnings.append(
                    f"Removed {_expo_dropped} lineup(s) to enforce "
                    f"per-player exposure limits"
                )
                logger.info(
                    f"[MultiLineup] Per-player exposure filter: dropped "
                    f"{_expo_dropped}, kept {len(selected)}/{n_requested}"
                )

        # ── Seed exposure dict from Phase 4 selected lineups ─────────
        # The exposure dict was empty during parallel candidate
        # generation. Populate it now so the fill loop's score function
        # dampening and per-player exclusions start from accurate counts.
        for lu in selected:
            for p in lu.players:
                exposure[p.player_id] = exposure.get(p.player_id, 0) + 1

        # ── Phase 4a-min: Min-exposure swap enforcement ───────────
        # If the portfolio selection couldn't meet min exposure targets
        # (e.g. not enough candidates contain a top player), replace
        # lowest-scoring lineups with force-locked versions.
        if _player_min_appearances and len(selected) == n_requested:
            _min_expo_deficit: Dict[int, int] = {}
            for _me_pid, _me_min in _player_min_appearances.items():
                _me_cur = exposure.get(_me_pid, 0)
                if _me_cur < _me_min:
                    _min_expo_deficit[_me_pid] = _me_min - _me_cur

            if _min_expo_deficit:
                logger.info(
                    f"[MultiLineup] Min exposure deficit: "
                    f"{sum(_min_expo_deficit.values())} total shortfall "
                    f"across {len(_min_expo_deficit)} players"
                )

                # Score existing lineups to find worst candidates for replacement
                _lu_scores = []
                for _si, _slu in enumerate(selected):
                    _s_pids = {p.player_id for p in _slu.players}
                    # Don't replace lineups that contain deficit players
                    _has_deficit_player = any(
                        pid in _s_pids for pid in _min_expo_deficit
                    )
                    if _has_deficit_player:
                        _lu_scores.append((_si, float('inf')))
                    else:
                        _lu_total = sum(p.projected_fp for p in _slu.players)
                        _lu_scores.append((_si, _lu_total))

                # Sort by score ascending (worst first)
                _lu_scores.sort(key=lambda x: x[1])

                _total_needed = sum(_min_expo_deficit.values())
                _replace_count = min(_total_needed, len(selected) // 4)
                _replace_indices = set()
                _replaced = 0

                for _ri, _rscore in _lu_scores:
                    if _replaced >= _replace_count:
                        break
                    if _rscore == float('inf'):
                        continue
                    _replace_indices.add(_ri)
                    _replaced += 1

                if _replace_indices:
                    # Remove replaced lineups and update exposure
                    _kept: List["OptimizedLineup"] = []
                    for _ki, _klu in enumerate(selected):
                        if _ki in _replace_indices:
                            for p in _klu.players:
                                exposure[p.player_id] = max(
                                    0, exposure.get(p.player_id, 0) - 1
                                )
                        else:
                            _kept.append(_klu)
                    selected = _kept
                    logger.info(
                        f"[MultiLineup] Removed {len(_replace_indices)} "
                        f"worst lineups for min-exposure replacement "
                        f"(kept {len(selected)})"
                    )

        # ── Phase 4b: Fill loop — generate replacement candidates ────
        # When the initial batch doesn't produce enough diverse lineups
        # OR min exposure enforcement removed some lineups, generate
        # individual candidates with fresh noise until the portfolio
        # is full (or max_attempts is exhausted).
        #
        # Re-define variables that the old Phase 2 block used to provide:
        _min_relax_floor: float = getattr(
            request, "minimum_relaxation_floor", 0.75
        )

        def _make_fill_score_fn(
            idx: int, exp: Dict[int, int], fill_rng: random.Random,
        ) -> Callable[[PlayerPoolEntry], float]:
            """Build a score closure for the Phase 4b fill loop."""
            def _sfn(p: PlayerPoolEntry) -> float:
                return self._compute_composite_score(
                    p, request.strategy, exp, idx, fill_rng,
                    contest_type=contest_type, sport=_sport,
                )
            return _sfn

        if len(selected) < n_requested:
            _fill_start = time.time()
            _fill_max_attempts = n_requested * _FILL_MAX_ATTEMPTS_MULTIPLIER
            _fill_attempt = 0
            _fill_consec_rejects = 0
            _fill_relax_rounds = 0
            _fill_accepted = 0
            _fill_overlap = request.max_overlap  # starts at user's setting
            _fill_quality_floor = floor_score if scored_candidates else 0.0

            # Build ID sets for accepted lineups (for overlap check)
            _accepted_id_sets: List[set] = [
                {p.player_id for p in lu.players} for lu in selected
            ]

            logger.info(
                f"[MultiLineup] Fill loop: need {n_requested - len(selected)} more "
                f"lineups (max_attempts={_fill_max_attempts})"
            )

            while len(selected) < n_requested and _fill_attempt < _fill_max_attempts:
                _fill_attempt += 1

                # Fresh RNG seed for diversity
                _fill_seed = rng.randint(0, 2**31)
                _fill_rng = random.Random(_fill_seed)
                _fill_idx = internal_count + _fill_attempt

                # Build score function with fresh noise
                _fill_sfn = _make_fill_score_fn(_fill_idx, exposure, _fill_rng)

                # Randomised game stacking
                _fill_si: Optional[List[int]] = None
                _fill_sgid: Optional[str] = None
                _fill_spt: Optional[str] = None
                _fill_ssz: int = 0
                _fill_sbb: bool = False

                if enable_stacking:
                    _fill_target = self._identify_stackable_games(pool, _fill_rng)
                    if _fill_target:
                        _fill_ssz, _fill_sbb = self._compute_dynamic_stack_params(
                            _fill_rng, pool, _fill_target, contest_type,
                        )
                        _fill_sgid = _fill_target["game_id"]
                        _fill_spt = _fill_rng.choice(
                            [_fill_target["team_a"], _fill_target["team_b"]]
                        )
                        _fill_si = self._select_stack_players(
                            pool, _fill_target, _fill_rng,
                            stack_size=_fill_ssz,
                            bring_back=_fill_sbb,
                            correlation_weights=self._cached_correlations or None,
                        )

                # ── Per-player exposure exclusions ─────────────────
                # Players who have hit their per-player max_exposure cap
                # are excluded from this solve entirely (x_i = 0 by pool
                # removal).  Also enforce the global max_exposure limit
                # if set.
                _exposure_excludes: set = set()
                if _player_max_appearances:
                    for _ep_id, _ep_max in _player_max_appearances.items():
                        if exposure.get(_ep_id, 0) >= _ep_max:
                            _exposure_excludes.add(_ep_id)
                if max_appearances is not None:
                    for _ep_id, _ep_cnt in exposure.items():
                        if _ep_cnt >= max_appearances:
                            _exposure_excludes.add(_ep_id)

                # ── Per-player min exposure: force-lock under-exposed players
                # Aggressively lock players who are behind their min target.
                # Old logic only locked when shortfall >= remaining, which
                # was too late (often never triggered).  New: lock whenever
                # the player still needs more appearances, with probability
                # proportional to how far behind they are.
                _fill_locked = list(request.locked_players or [])
                if _player_min_appearances:
                    _remaining = n_requested - len(selected)
                    for _mp_id, _mp_min in _player_min_appearances.items():
                        _cur = exposure.get(_mp_id, 0)
                        _shortfall = _mp_min - _cur
                        if _shortfall > 0 and _remaining > 0:
                            # Force-lock when running out of room, OR
                            # probabilistically when significantly behind target
                            _need_ratio = _shortfall / _remaining
                            if (
                                _need_ratio >= 0.5
                                or _fill_rng.random() < _need_ratio * 1.2
                            ):
                                if _mp_id not in _exposure_excludes:
                                    _fill_locked.append(_mp_id)

                # ── Contrarian slot: force a low-owned upside player ──
                _fill_lineup_idx = len(selected)
                if (
                    _contrarian_locks
                    and _fill_lineup_idx < len(_contrarian_locks)
                    and _contrarian_locks[_fill_lineup_idx] is not None
                ):
                    _cid = _contrarian_locks[_fill_lineup_idx]
                    if (
                        _cid not in _exposure_excludes
                        and _cid not in _fill_locked
                    ):
                        _fill_locked.append(_cid)

                # Generate single candidate via ILP
                try:
                    _fill_lu = self._build_single_lineup(
                        pool=pool,
                        platform=platform,
                        salary_cap=salary_cap,
                        roster_slots=roster_slots,
                        slot_order=slot_order,
                        locked_player_ids=_fill_locked,
                        extra_excludes=_exposure_excludes,
                        score_fn=_fill_sfn,
                        stack_player_ids=_fill_si,
                        salary_floor=salary_floor,
                        stack_game_id=_fill_sgid,
                        stack_primary_team=_fill_spt,
                        stack_size=_fill_ssz,
                        stack_bring_back=_fill_sbb,
                        sport=_sport,
                        mode=mode,
                        contest_type=contest_type,
                        skip_ilp=False,
                        ilp_time_limit=ILP_CANDIDATE_TIME_LIMIT,
                        # C7b removed — min_projection handled by quality gate
                        min_projection_floor=None,
                        baseline_projection_score=baseline_projection_score,
                        minimum_relaxation_floor=_min_relax_floor,
                        max_cumulative_ownership=_max_cum_own,
                        enable_stacking=enable_stacking,
                        stack_overrides=stack_overrides,
                    )
                except LineupGenerationError:
                    if _exposure_excludes:
                        # Per-player exclusions made the floor unreachable
                        # for this attempt — treat as soft failure, not a
                        # fatal crash.  The fill loop will keep trying with
                        # different stacking/noise seeds.
                        _fill_consec_rejects += 1
                        continue
                    raise
                except Exception:
                    _fill_consec_rejects += 1
                    continue

                if _fill_lu is None:
                    _fill_consec_rejects += 1
                    continue

                # ── Evaluation gate ──────────────────────────────
                # 1. Structural quality + projected FP floor
                # Clamp min_salary_pct so it never drops below
                # MIN_SALARY_FLOOR / salary_cap (hard $49,300 floor).
                from app.config.constants import MIN_SALARY_FLOOR as _MSF
                _fill_min_sal_pct = max(
                    salary_floor_pct * 0.90,
                    _MSF / salary_cap if salary_cap > 0 else 0.99,
                )
                if not self._passes_quality_gate(
                    _fill_lu,
                    salary_cap,
                    expected_players=len(roster_slots),
                    min_salary_pct=_fill_min_sal_pct,
                    min_projected_fp=_min_projected_fp,
                ):
                    _fill_consec_rejects += 1
                    continue

                # 2. Salary hard floor (relaxable — starts at 95%, drops
                #    to 93% after overlap relaxation kicks in, but NEVER
                #    below MIN_SALARY_FLOOR)
                if salary_cap > 0:
                    _fill_sal_floor = max(
                        _MSF / salary_cap,
                        0.93,
                        SALARY_UTILIZATION_HARD_FLOOR
                        - 0.005 * (_fill_overlap - request.max_overlap),
                    )
                    if (_fill_lu.total_salary / salary_cap) < _fill_sal_floor:
                        _fill_consec_rejects += 1
                        continue

                # 3. Score quality floor
                _fill_score = self._score_lineup(
                    _fill_lu, pool, request.strategy, contest_type, salary_cap,
                )
                if _fill_quality_floor > 0 and _fill_score < _fill_quality_floor:
                    _fill_consec_rejects += 1
                    continue

                # 4. Diversity: overlap with ALL accepted lineups
                _fill_ids = {p.player_id for p in _fill_lu.players}
                _overlap_violation = False
                for _prev_ids in _accepted_id_sets:
                    if len(_fill_ids & _prev_ids) > _fill_overlap:
                        _overlap_violation = True
                        break
                if _overlap_violation:
                    _fill_consec_rejects += 1
                    # Dynamic relaxation after N consecutive rejects
                    if _fill_consec_rejects >= _FILL_CONSEC_REJECT_THRESHOLD:
                        _fill_relax_rounds += 1
                        # HARD STOP: after N relaxation rounds, the solver
                        # has exhausted valid combinations.  Return what we
                        # have rather than forcing garbage lineups.
                        if _fill_relax_rounds >= _FILL_MAX_RELAXATION_ROUNDS:
                            logger.warning(
                                f"[MultiLineup/Fill] HARD STOP: "
                                f"{_fill_relax_rounds} relaxation rounds "
                                f"exhausted — returning {len(selected)} "
                                f"lineups (target was {n_requested})"
                            )
                            break
                        _fill_overlap = min(
                            _fill_overlap + _FILL_OVERLAP_RELAX_STEP,
                            len(roster_slots) - 1,
                        )
                        _fill_quality_floor *= _FILL_QUALITY_RELAX_FACTOR
                        _fill_consec_rejects = 0
                        logger.warning(
                            f"[MultiLineup/Fill] Relaxing constraints "
                            f"(round {_fill_relax_rounds}/"
                            f"{_FILL_MAX_RELAXATION_ROUNDS}): "
                            f"max_overlap→{_fill_overlap}, "
                            f"quality_floor→{_fill_quality_floor:.1f} "
                            f"(accepted {len(selected)}/{n_requested})"
                        )
                    continue

                # ✓ Accepted — add to portfolio
                selected.append(_fill_lu)
                _accepted_id_sets.append(_fill_ids)
                scored_candidates.append((_fill_lu, _fill_score))
                for p in _fill_lu.players:
                    exposure[p.player_id] = exposure.get(p.player_id, 0) + 1
                _fill_consec_rejects = 0
                _fill_accepted += 1

            _fill_elapsed = time.time() - _fill_start
            if _fill_accepted > 0 or _fill_attempt > 0:
                logger.info(
                    f"[MultiLineup] Fill loop completed in {_fill_elapsed:.1f}s: "
                    f"accepted {_fill_accepted}/{_fill_attempt} attempts "
                    f"(portfolio now {len(selected)}/{n_requested})"
                )

        logger.info(
            f"[MultiLineup] Phase 4 completed in "
            f"{time.time() - _phase4_start:.1f}s: "
            f"selected {len(selected)}/{n_requested} lineups"
        )

        # ── Phase 4c: Retry pass with relaxed constraints ───────────
        # If the fill loop couldn't fulfill the request, run a second
        # pass with aggressively relaxed overlap / quality constraints.
        # This is a safety net — later lineups may be slightly less
        # diverse or lower-quality, but the user gets all N lineups.
        if len(selected) < n_requested:
            _retry_shortfall = n_requested - len(selected)
            logger.info(
                f"[MultiLineup] Phase 4c retry: still need {_retry_shortfall} "
                f"lineups, retrying with relaxed constraints"
            )

            # Relaxed limits: overlap +2 from user setting, quality floor halved
            _retry_overlap = min(
                request.max_overlap + 2, len(roster_slots) - 1
            )
            _retry_quality_floor = (
                floor_score * 0.50 if scored_candidates else 0.0
            )
            _retry_max_attempts = _retry_shortfall * 25
            _retry_attempt = 0
            _retry_consec = 0
            _retry_accepted = 0

            # Rebuild _accepted_id_sets from current selections.  The fill
            # loop may have already populated this, but rebuilding is cheap
            # and ensures correctness regardless of earlier code paths.
            _accepted_id_sets = [
                {p.player_id for p in lu.players} for lu in selected
            ]

            # Time fence: guarantee at least 30s for retry pass, regardless
            # of how long previous phases took.  The user expects all N
            # lineups; spending an extra 30s to fulfill that is worthwhile.
            _retry_time_start = time.time()
            _retry_time_limit = max(
                30.0,
                MULTI_LINEUP_TIME_BUDGET - (time.time() - _phase2_start),
            )

            while (
                len(selected) < n_requested
                and _retry_attempt < _retry_max_attempts
                and (time.time() - _retry_time_start) < _retry_time_limit
            ):
                _retry_attempt += 1
                _retry_seed = rng.randint(0, 2**31)
                _retry_rng = random.Random(_retry_seed)
                _retry_idx = internal_count + n_requested * _FILL_MAX_ATTEMPTS_MULTIPLIER + _retry_attempt

                _retry_sfn = _make_fill_score_fn(
                    _retry_idx, exposure, _retry_rng,
                )

                # Randomised game stacking (same as fill loop)
                _retry_si: Optional[List[int]] = None
                _retry_sgid: Optional[str] = None
                _retry_spt: Optional[str] = None
                _retry_ssz: int = 0
                _retry_sbb: bool = False

                if enable_stacking:
                    _retry_target = self._identify_stackable_games(
                        pool, _retry_rng,
                    )
                    if _retry_target:
                        _retry_ssz, _retry_sbb = (
                            self._compute_dynamic_stack_params(
                                _retry_rng, pool, _retry_target, contest_type,
                            )
                        )
                        _retry_sgid = _retry_target["game_id"]
                        _retry_spt = _retry_rng.choice(
                            [_retry_target["team_a"], _retry_target["team_b"]]
                        )
                        _retry_si = self._select_stack_players(
                            pool, _retry_target, _retry_rng,
                            stack_size=_retry_ssz,
                            bring_back=_retry_sbb,
                            correlation_weights=(
                                self._cached_correlations or None
                            ),
                        )

                # Per-player exposure exclusions (same as fill loop)
                _retry_excludes: set = set()
                if _player_max_appearances:
                    for _ep_id, _ep_max in _player_max_appearances.items():
                        if exposure.get(_ep_id, 0) >= _ep_max:
                            _retry_excludes.add(_ep_id)
                if max_appearances is not None:
                    for _ep_id, _ep_cnt in exposure.items():
                        if _ep_cnt >= max_appearances:
                            _retry_excludes.add(_ep_id)

                # Min-exposure forced locks (aggressive — same as Phase 4b)
                _retry_locked = list(request.locked_players or [])
                _retry_rng = random.Random(rng.randint(0, 2**31))
                if _player_min_appearances:
                    _remaining = n_requested - len(selected)
                    for _mp_id, _mp_min in _player_min_appearances.items():
                        _cur = exposure.get(_mp_id, 0)
                        _shortfall_exp = _mp_min - _cur
                        if _shortfall_exp > 0 and _remaining > 0:
                            _need_ratio = _shortfall_exp / _remaining
                            if (
                                _need_ratio >= 0.5
                                or _retry_rng.random() < _need_ratio * 1.2
                            ):
                                if _mp_id not in _retry_excludes:
                                    _retry_locked.append(_mp_id)

                # Solve single candidate via ILP
                try:
                    _retry_lu = self._build_single_lineup(
                        pool=pool,
                        platform=platform,
                        salary_cap=salary_cap,
                        roster_slots=roster_slots,
                        slot_order=slot_order,
                        locked_player_ids=_retry_locked,
                        extra_excludes=_retry_excludes,
                        score_fn=_retry_sfn,
                        stack_player_ids=_retry_si,
                        salary_floor=salary_floor,
                        stack_game_id=_retry_sgid,
                        stack_primary_team=_retry_spt,
                        stack_size=_retry_ssz,
                        stack_bring_back=_retry_sbb,
                        sport=_sport,
                        mode=mode,
                        contest_type=contest_type,
                        skip_ilp=False,
                        ilp_time_limit=ILP_CANDIDATE_TIME_LIMIT,
                        min_projection_floor=None,
                        baseline_projection_score=baseline_projection_score,
                        minimum_relaxation_floor=_min_relax_floor,
                        max_cumulative_ownership=_max_cum_own,
                        enable_stacking=enable_stacking,
                        stack_overrides=stack_overrides,
                    )
                except LineupGenerationError:
                    _retry_consec += 1
                    # After 20 consecutive solver errors, relax constraints
                    # instead of aborting — the user wants all N lineups.
                    if _retry_consec >= 20 and _retry_consec % 20 == 0:
                        _retry_overlap = min(
                            _retry_overlap + 1,
                            len(roster_slots) - 1,
                        )
                        _retry_quality_floor *= 0.85
                        logger.warning(
                            f"[MultiLineup/Retry] {_retry_consec} consecutive "
                            f"errors — relaxing: overlap→{_retry_overlap}, "
                            f"quality_floor→{_retry_quality_floor:.1f}"
                        )
                    continue
                except Exception:
                    _retry_consec += 1
                    continue

                if _retry_lu is None:
                    _retry_consec += 1
                    continue

                # ── Relaxed evaluation gates ────────────────────
                # 1. Structural quality (relaxed salary pct, but
                #    NEVER below MIN_SALARY_FLOOR)
                from app.config.constants import MIN_SALARY_FLOOR as _MSF
                _retry_min_sal_pct = max(
                    salary_floor_pct * 0.85,
                    _MSF / salary_cap if salary_cap > 0 else 0.99,
                )
                if not self._passes_quality_gate(
                    _retry_lu,
                    salary_cap,
                    expected_players=len(roster_slots),
                    min_salary_pct=_retry_min_sal_pct,
                    min_projected_fp=(
                        (_min_projected_fp or 0) * 0.85
                    ),
                ):
                    _retry_consec += 1
                    continue

                # 2. Relaxed salary hard floor (90% instead of 95%,
                #    but NEVER below MIN_SALARY_FLOOR)
                _retry_hard_pct = max(
                    0.90,
                    _MSF / salary_cap if salary_cap > 0 else 0.99,
                )
                if salary_cap > 0:
                    if (_retry_lu.total_salary / salary_cap) < _retry_hard_pct:
                        _retry_consec += 1
                        continue

                # 3. Score quality floor (halved)
                _retry_score = self._score_lineup(
                    _retry_lu, pool, request.strategy,
                    contest_type, salary_cap,
                )
                if (
                    _retry_quality_floor > 0
                    and _retry_score < _retry_quality_floor
                ):
                    _retry_consec += 1
                    continue

                # 4. Diversity: overlap with ALL accepted lineups
                _retry_ids = {p.player_id for p in _retry_lu.players}
                _retry_overlap_ok = True
                for _prev_ids in _accepted_id_sets:
                    if len(_retry_ids & _prev_ids) > _retry_overlap:
                        _retry_overlap_ok = False
                        break
                if not _retry_overlap_ok:
                    _retry_consec += 1
                    # Further relax after 15 consecutive rejects
                    if _retry_consec >= 15:
                        _retry_overlap = min(
                            _retry_overlap + 1,
                            len(roster_slots) - 1,
                        )
                        _retry_quality_floor *= 0.90
                        _retry_consec = 0
                        logger.warning(
                            f"[MultiLineup/Retry] Further relaxing: "
                            f"overlap→{_retry_overlap}, "
                            f"quality_floor→{_retry_quality_floor:.1f}"
                        )
                    continue

                # ✓ Accepted
                selected.append(_retry_lu)
                _accepted_id_sets.append(_retry_ids)
                scored_candidates.append((_retry_lu, _retry_score))
                for p in _retry_lu.players:
                    exposure[p.player_id] = (
                        exposure.get(p.player_id, 0) + 1
                    )
                _retry_consec = 0
                _retry_accepted += 1

            logger.info(
                f"[MultiLineup] Phase 4c retry completed: "
                f"accepted {_retry_accepted}/{_retry_attempt} attempts "
                f"(portfolio now {len(selected)}/{n_requested})"
            )

        # ── Retry loop: re-run the pipeline if we're still short ──────
        # The generation is non-deterministic (noise seeds, RNG).  Instead
        # of relaxing quality, simply re-run with a fresh seed, keep what
        # we already have, and only ask for the shortfall.  Each retry
        # progressively relaxes diversity so we converge on the target.
        _MAX_RETRIES = 5
        _retry_round = 0
        _all_candidates = list(candidates)
        _all_scored = list(scored_candidates)

        while len(selected) < n_requested and _retry_round < _MAX_RETRIES:
            _retry_round += 1
            _still_need = n_requested - len(selected)

            # Time fence: stop retrying if we've used > 80% of a generous
            # outer budget (4× the per-run budget).
            _total_elapsed = time.time() - start_ms
            if _total_elapsed > MULTI_LINEUP_TIME_BUDGET * 4:
                logger.info(
                    f"[MultiLineup] Retry budget exhausted after "
                    f"{_total_elapsed:.0f}s — stopping with "
                    f"{len(selected)}/{n_requested}"
                )
                break

            logger.info(
                f"[MultiLineup] ── Retry round {_retry_round}/{_MAX_RETRIES}: "
                f"need {_still_need} more quality lineups ──"
            )

            # Build a sub-request for just the shortfall, with a new seed
            _orig_seed = getattr(request, "seed", None)
            _retry_seed = (
                (_orig_seed + _retry_round * 9999) if _orig_seed is not None
                else int(time.time() * 1000) + _retry_round
            )
            _sub_request = request.model_copy(update={
                "num_lineups": _still_need,
                "seed": _retry_seed,
            })

            # Collect player-ID sets from already-accepted lineups so we
            # can enforce diversity against them in the sub-run.
            _existing_id_sets: List[set] = [
                {p.player_id for p in lu.players} for lu in selected
            ]

            # ── Re-run Phases 1-4 for the shortfall ─────────────────
            # Retries need extra overgeneration because diversity against
            # the existing portfolio is harder.  Boost multiplier by 50%
            # per retry round (round 1→1.5×, round 2→2×, round 3→2.5×).
            _sub_n = _still_need
            _retry_boost = 1.0 + 0.5 * _retry_round
            if _sub_n <= 20:
                _sub_mult = _OVERGEN_MULTIPLIER_SMALL * _retry_boost
            elif _sub_n <= 80:
                _sub_mult = _OVERGEN_MULTIPLIER_MEDIUM * _retry_boost
            else:
                _sub_mult = _OVERGEN_MULTIPLIER_LARGE * _retry_boost

            _sub_internal = max(
                int(_sub_n * _sub_mult), _OVERGEN_MIN_CANDIDATES,
            )
            _sub_internal = min(_sub_internal, _OVERGEN_MAX_CANDIDATES)
            _sub_rng = random.Random(_retry_seed)

            # Phase 2: K-Best with fresh seed
            _sub_enable_stacking = enable_stacking
            _sub_exposure: Dict[int, int] = dict(exposure)  # copy current

            _sub_start = time.time()
            _sub_time_budget = MULTI_LINEUP_TIME_BUDGET

            _sub_stack_targets: List[dict] = []
            if _sub_enable_stacking:
                _sub_game_pool = self._get_stackable_game_pool(pool)
                if _sub_game_pool:
                    _sub_assignments = self._allocate_stack_targets(
                        _sub_game_pool, _sub_internal, _sub_rng,
                    )
                    # Convert (game_dict, count) tuples → dict format
                    for _sg, _sc in _sub_assignments:
                        _spt = _sub_rng.choice([_sg["team_a"], _sg["team_b"]])
                        _ssz, _sbb = self._compute_dynamic_stack_params(
                            _sub_rng, pool, _sg, contest_type,
                        )
                        _sub_stack_targets.append({
                            "game_id": _sg["game_id"],
                            "primary_team": _spt,
                            "size": _ssz,
                            "bring_back": _sbb,
                            "target_count": _sc,
                        })
            if not _sub_stack_targets:
                _sub_stack_targets = [{
                    "game_id": None,
                    "primary_team": None,
                    "size": 0,
                    "bring_back": False,
                    "target_count": _sub_internal,
                }]

            from app.config.constants import KBEST_MAX_OVERLAP
            _sub_candidates: List[OptimizedLineup] = []
            for st in _sub_stack_targets:
                _sub_batch = self._kbest_generate_for_stack(
                    pool=pool,
                    platform=platform,
                    salary_cap=salary_cap,
                    roster_slots=roster_slots,
                    slot_order=slot_order,
                    locked_player_ids=list(_sub_request.locked_players or []),
                    stack_config=st,
                    strategy=_sub_request.strategy,
                    contest_type=contest_type,
                    sport=_sport,
                    mode=mode,
                    target_count=st["target_count"],
                    max_overlap=(
                        self._slate_adjustments.get("max_overlap_override")
                        or KBEST_MAX_OVERLAP
                    ) if self._slate_adjustments else KBEST_MAX_OVERLAP,
                    time_budget=_sub_time_budget,
                    master_seed=_retry_seed + hash(str(st.get("game_id"))),
                    min_projected_fp=_min_projected_fp,
                    salary_floor_pct=salary_floor_pct,
                    shared_exposure=None,
                    n_requested_final=n_requested,
                    max_exposure=min(
                        max_exposure if max_exposure is not None else 0.55,
                        0.55,  # ABSOLUTE_GLOBAL_MAX_EXPOSURE
                    ),
                    player_max_exposure=(
                        _player_max_appearances
                        if _player_max_appearances else {}
                    ),
                )
                _sub_candidates.extend(_sub_batch)

            if not _sub_candidates:
                logger.info(
                    f"[MultiLineup] Retry {_retry_round}: K-Best produced "
                    f"0 candidates — skipping"
                )
                continue

            # Phase 2.5 + 3: Salary gate & quality scoring
            # Never drop below MIN_SALARY_FLOOR regardless of
            # adaptive softening.
            if salary_cap > 0:
                from app.config.constants import MIN_SALARY_FLOOR as _MSF
                _sub_sal_floor = max(
                    _MSF / salary_cap,
                    0.92 if len(_sub_candidates) < _sub_n * 2
                    else SALARY_UTILIZATION_HARD_FLOOR,
                )
                _sub_candidates = [
                    lu for lu in _sub_candidates
                    if (lu.total_salary / salary_cap) >= _sub_sal_floor
                ]

            _sub_scored = [
                (lu, self._score_lineup(
                    lu, pool, _sub_request.strategy, contest_type, salary_cap,
                ))
                for lu in _sub_candidates
            ]

            if _sub_scored:
                _sub_best = max(sc for _, sc in _sub_scored)
                _sub_qfloor_pct = max(
                    0.40, _quality_floor - 0.12 * _retry_round,
                )
                _sub_floor = _sub_best * _sub_qfloor_pct
                _sub_scored = [
                    (lu, sc) for lu, sc in _sub_scored
                    if sc >= _sub_floor
                ]

            # Phase 4: Diversity selection against existing lineups
            # Only accept candidates that don't overlap too much with
            # already-selected lineups.  Progressively relax overlap
            # threshold each retry round so later rounds can fill gaps.
            _sub_accepted = 0
            _sub_base_overlap = request.max_overlap
            _sub_max_overlap = min(
                _sub_base_overlap + _retry_round,  # +1 per retry round
                len(roster_slots) - 1,  # never allow full-roster overlap
            )

            # Two-pass approach: first pass with current overlap, second
            # pass with +1 more if we still need lineups.
            _sub_sorted = sorted(_sub_scored, key=lambda x: -x[1])

            for _pass_num in range(2):
                _pass_overlap = _sub_max_overlap + _pass_num
                _pass_overlap = min(_pass_overlap, len(roster_slots) - 1)
                for lu, sc in _sub_sorted:
                    if len(selected) >= n_requested:
                        break
                    _lu_ids = {p.player_id for p in lu.players}

                    # Skip if already accepted
                    if any(
                        _lu_ids == prev_ids for prev_ids in _existing_id_sets
                    ):
                        continue

                    # Check against ALL previously selected lineups
                    _violates = False
                    for prev_ids in _existing_id_sets:
                        if len(_lu_ids & prev_ids) > _pass_overlap:
                            _violates = True
                            break
                    if _violates:
                        continue

                    # ✓ Diverse & quality — accept
                    selected.append(lu)
                    _existing_id_sets.append(_lu_ids)
                    scored_candidates.append((lu, sc))
                    _all_candidates.append(lu)
                    _all_scored.append((lu, sc))
                    for p in lu.players:
                        exposure[p.player_id] = (
                            exposure.get(p.player_id, 0) + 1
                        )
                    _sub_accepted += 1

                if len(selected) >= n_requested:
                    break

            logger.info(
                f"[MultiLineup] Retry {_retry_round}: accepted "
                f"{_sub_accepted} from {len(_sub_candidates)} candidates "
                f"(portfolio now {len(selected)}/{n_requested})"
            )

        # Merge all candidates for diagnostics
        candidates = _all_candidates

        # ── Phase 5: Attach quality grades & finalize response ────────
        if len(selected) < n_requested:
            warnings.append(
                f"Generated {len(selected)} of {n_requested} requested lineups "
                f"after {_retry_round + 1} generation rounds. "
                f"The player pool may not support this many diverse "
                f"quality lineups."
            )

        # Build score lookup for selected lineups (for relative ranking)
        score_lookup: Dict[int, float] = {
            id(lu): sc for lu, sc in scored_candidates
        }
        best_score_val = max(
            (sc for _, sc in scored_candidates), default=1.0
        )

        for lu in selected:
            lu_score = score_lookup.get(id(lu))
            q_score, q_grade, q_warnings = self._assess_lineup_quality(
                lu, salary_cap, pool=pool,
                best_score=best_score_val,
                lineup_score=lu_score,
            )
            lu.quality_score = q_score
            lu.quality_grade = q_grade
            if q_warnings:
                lu.warnings.extend(q_warnings)

        elapsed_ms = int((time.time() - start_ms) * 1000)

        # ── ILP diagnostic counters ────────────────────────────────────
        _ilp_accepted = sum(
            1 for lu in candidates if getattr(lu, "ilp_used", None) is True
        )
        _ilp_failed = sum(
            1 for lu in candidates if getattr(lu, "ilp_used", None) is False
        )
        _greedy_only = sum(
            1 for lu in candidates if getattr(lu, "ilp_used", None) is None
        )

        logger.info(
            f"[MultiLineup] DONE: {len(selected)}/{n_requested} lineups "
            f"in {elapsed_ms}ms "
            f"({_retry_round} retry rounds, "
            f"pool={len(pool)}, "
            f"candidates_built={len(candidates)}, "
            f"scored={len(scored_candidates)}, "
            f"ilp_accepted={_ilp_accepted}, ilp_failed={_ilp_failed}, "
            f"greedy_only={_greedy_only})"
        )

        # Clean up instance-level generation state
        self._slate_avg_game_total = 0.0
        self._secondary_stack_game_id = None
        self._slate_adjustments = None

        return MultiLineupResponse(
            platform=platform,
            sport=_sport,
            lineups=selected,
            strategy=request.strategy,
            num_requested=n_requested,
            num_generated=len(selected),
            pool_size=len(pool),
            generation_time_ms=elapsed_ms,
            warnings=warnings,
            num_candidates_generated=len(candidates),
            baseline_projection_score=baseline_projection_score,
            baseline_optimal_lineup=baseline_optimal_lineup,
            min_projection_floor=min_projection_floor,
            ilp_accepted_count=_ilp_accepted,
            ilp_failed_count=_ilp_failed,
            greedy_fallback_count=_greedy_only,
        )

    # ------------------------------------------------------------------
    # Rules-based news pipeline
    # ------------------------------------------------------------------

    def _apply_rules_based_news(
        self,
        news_items: list,
        pool: List[PlayerPoolEntry],
        sport: str = "nba",
    ) -> int:
        """Apply deterministic keyword-based news adjustments to pool.

        Scans news headlines/descriptions for keywords and applies
        adjustments WITHOUT requiring the AI agent.  This ensures critical
        news (ruled out, surprise start, expanded role) is captured even
        when the LLM is unavailable or slow.

        Returns the count of adjustments applied.
        """
        from app.config.constants import (
            NEWS_RULES_DNP_KEYWORDS,
            NEWS_RULES_GTD_KEYWORDS,
            NEWS_RULES_STARTER_KEYWORDS,
            NEWS_RULES_EXPANDED_ROLE_KEYWORDS,
            NEWS_RULES_REDUCED_ROLE_KEYWORDS,
            NEWS_RULES_EXPANDED_USAGE_BOOST,
            NEWS_RULES_REDUCED_USAGE_CUT,
            NEWS_RULES_GTD_MINUTES_FACTOR,
        )

        applied = 0
        # Build a lookup for fast matching
        pool_by_name: Dict[str, PlayerPoolEntry] = {}
        for entry in pool:
            pool_by_name[entry.player_name.lower()] = entry

        for news in news_items:
            headline = getattr(news, "headline", "") or ""
            description = getattr(news, "description", "") or ""
            relevance = getattr(news, "relevance", "general") or "general"
            text = f"{headline} {description}".lower()

            if not text.strip():
                continue

            # Try to find which player(s) this news is about
            # Match against pool player names in the text
            matched_entries = []
            for pname_lower, entry in pool_by_name.items():
                # Check last name match (more reliable than full name)
                name_parts = pname_lower.split()
                if len(name_parts) >= 2:
                    last_name = name_parts[-1]
                    # Require at least the last name + partial first
                    if last_name in text and (
                        name_parts[0][:3] in text or pname_lower in text
                    ):
                        matched_entries.append(entry)
                elif pname_lower in text:
                    matched_entries.append(entry)

            if not matched_entries:
                continue

            for entry in matched_entries:
                # Check keywords in priority order (most impactful first)

                # DNP / Ruled out → zero minutes
                if any(kw in text for kw in NEWS_RULES_DNP_KEYWORDS):
                    if relevance in ("injury", "lineup"):
                        entry.projected_minutes = 0.0
                        entry.projected_fp = 0.0
                        entry.floor_fp = 0.0
                        entry.ceiling_fp = 0.0
                        entry.injury_status = entry.injury_status or "Out"
                        applied += 1
                        logger.debug(
                            f"[NewsRules] DNP: {entry.player_name} "
                            f"← \"{headline[:60]}\""
                        )
                        continue  # No further adjustments needed

                # GTD → slightly reduced minutes expectation
                if any(kw in text for kw in NEWS_RULES_GTD_KEYWORDS):
                    if relevance in ("injury", "lineup"):
                        entry.projected_minutes = round(
                            entry.projected_minutes * NEWS_RULES_GTD_MINUTES_FACTOR, 1
                        )
                        # Scale FP proportionally
                        entry.projected_fp = round(
                            entry.projected_fp * NEWS_RULES_GTD_MINUTES_FACTOR, 1
                        )
                        if not entry.injury_status:
                            entry.injury_status = "Questionable"
                        applied += 1
                        continue

                # Surprise starter → boost to starter-level minutes
                if any(kw in text for kw in NEWS_RULES_STARTER_KEYWORDS):
                    if relevance in ("lineup", "general"):
                        # Sport-specific starter threshold from registry
                        # (NBA=28 in 48-min game, CBB=24 in 40-min game)
                        from app.sports import get_config as _get_sport_cfg
                        _starter_min = _get_sport_cfg(sport).starter_min_minutes
                        if entry.projected_minutes < _starter_min:
                            old_min = entry.projected_minutes
                            entry.projected_minutes = max(
                                entry.projected_minutes, _starter_min
                            )
                            ratio = (
                                entry.projected_minutes / old_min
                                if old_min > 0 else 1.5
                            )
                            entry.projected_fp = round(
                                entry.projected_fp * ratio, 1
                            )
                            entry.ceiling_fp = round(
                                entry.ceiling_fp * ratio, 1
                            )
                            entry.floor_fp = round(
                                entry.floor_fp * min(ratio, 1.3), 1
                            )
                            applied += 1
                            logger.debug(
                                f"[NewsRules] Starter: {entry.player_name} "
                                f"min {old_min:.0f}→{entry.projected_minutes:.0f}"
                            )
                        continue

                # Expanded role → usage boost
                if any(kw in text for kw in NEWS_RULES_EXPANDED_ROLE_KEYWORDS):
                    entry.projected_fp = round(
                        entry.projected_fp * NEWS_RULES_EXPANDED_USAGE_BOOST, 1
                    )
                    entry.ceiling_fp = round(
                        entry.ceiling_fp * NEWS_RULES_EXPANDED_USAGE_BOOST, 1
                    )
                    applied += 1
                    continue

                # Reduced role → usage cut
                if any(kw in text for kw in NEWS_RULES_REDUCED_ROLE_KEYWORDS):
                    entry.projected_fp = round(
                        entry.projected_fp * NEWS_RULES_REDUCED_USAGE_CUT, 1
                    )
                    entry.ceiling_fp = round(
                        entry.ceiling_fp * NEWS_RULES_REDUCED_USAGE_CUT, 1
                    )
                    entry.floor_fp = round(
                        entry.floor_fp * NEWS_RULES_REDUCED_USAGE_CUT, 1
                    )
                    applied += 1
                    continue

        return applied

    # ------------------------------------------------------------------
    # User projection overrides
    # ------------------------------------------------------------------

    @staticmethod
    def _apply_overrides(
        pool: List[PlayerPoolEntry],
        overrides: Optional[Dict[int, Dict[str, float]]],
        sport: str = "nba",
    ) -> List[PlayerPoolEntry]:
        """Apply user-specified projection overrides to pool entries.

        Overrides are applied AFTER pool building and enrichment,
        so user edits take final precedence over AI agents and
        rule-based projections.  Also recomputes dk_value when
        projected_fp changes.
        """
        if not overrides:
            return pool

        ALLOWED = {"projected_minutes", "projected_fp", "floor_fp", "ceiling_fp"}
        # Sport-specific minutes cap from registry (NBA=53, CBB=45)
        from app.sports import get_config as _get_sport_cfg
        _max_minutes = _get_sport_cfg(sport).max_player_minutes
        applied = 0

        for entry in pool:
            player_overrides = overrides.get(entry.player_id)
            if not player_overrides:
                continue

            for field, value in player_overrides.items():
                if field in ALLOWED:
                    v = round(float(value), 1)
                    # Safety cap: projected_minutes cannot exceed regulation + OT
                    if field == "projected_minutes":
                        v = min(v, _max_minutes)
                    setattr(entry, field, v)

            # Recompute value metric when FP changes
            if "projected_fp" in player_overrides and entry.salary > 0:
                entry.dk_value = round(
                    entry.projected_fp / entry.salary * 1000, 2
                )
            applied += 1

        if applied:
            logger.info(f"[Overrides] Applied user edits to {applied} players")

        return pool

    # ------------------------------------------------------------------
    # GPP Auto-Exposure Caps & Contrarian Locks
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_auto_exposure_caps(
        pool: List["PlayerPoolEntry"],
        contest_type: str,
        user_max_exposure: Optional[float],
        user_player_max_exposure: Dict[int, float],
    ) -> Dict[int, float]:
        """Compute tier-based per-player exposure caps for GPP contests.

        Classifies players into studs / mid-tier / value-punt based on
        projected_fp rank and salary, then assigns automatic exposure caps.
        User-set per-player caps take precedence (minimum of auto vs user).

        Only active for GPP/single_entry contests.  Returns the user dict
        unchanged for cash contests.
        """
        if contest_type not in ("gpp", "single_entry"):
            return dict(user_player_max_exposure)

        from app.config.constants import (
            GPP_AUTO_EXPO_STUD_PERCENTILE,
            GPP_AUTO_EXPO_MID_PERCENTILE,
            GPP_AUTO_EXPO_MID_CAP,
            GPP_AUTO_EXPO_VALUE_CAP,
            GPP_AUTO_EXPO_VALUE_SALARY_THRESHOLD,
            EXPOSURE_PENALTY_DEFAULT_CAP,
            ABSOLUTE_GLOBAL_MAX_EXPOSURE,
            ELITE_CORE_VALUE_THRESHOLD,
            ELITE_CORE_MAX_EXPOSURE,
            MEGA_CHALK_VALUE_THRESHOLD,
            MEGA_CHALK_OWNERSHIP_THRESHOLD,
            MEGA_CHALK_MAX_EXPOSURE,
            STRONG_MID_TIER_SALARY_MIN,
            STRONG_MID_TIER_SALARY_MAX,
            STRONG_MID_TIER_VALUE_THRESHOLD,
            STRONG_MID_TIER_OPTIMAL_THRESHOLD,
            STRONG_MID_TIER_MAX_EXPOSURE,
        )

        global_cap = (
            user_max_exposure
            if user_max_exposure is not None
            else EXPOSURE_PENALTY_DEFAULT_CAP
        )

        # Sort pool by projected_fp descending to determine rank percentiles
        sorted_pool = sorted(
            pool, key=lambda p: p.projected_fp, reverse=True,
        )
        n = len(sorted_pool)
        if n == 0:
            return dict(user_player_max_exposure)

        stud_cutoff = max(1, int(n * GPP_AUTO_EXPO_STUD_PERCENTILE))
        mid_cutoff = max(
            stud_cutoff + 1,
            int(n * GPP_AUTO_EXPO_MID_PERCENTILE),
        )

        merged_caps: Dict[int, float] = dict(user_player_max_exposure)

        for rank, player in enumerate(sorted_pool):
            pid = player.player_id

            if rank < stud_cutoff:
                # Studs: no auto-cap (use global default)
                auto_cap = global_cap
            elif rank < mid_cutoff:
                # Mid-tier
                auto_cap = GPP_AUTO_EXPO_MID_CAP
            else:
                # Value/punt: tighter cap only if salary is low
                if player.salary < GPP_AUTO_EXPO_VALUE_SALARY_THRESHOLD:
                    auto_cap = GPP_AUTO_EXPO_VALUE_CAP
                else:
                    auto_cap = GPP_AUTO_EXPO_MID_CAP

            # Low rotation confidence: tighter cap for uncertain players
            if player.rotation_confidence < 0.7:
                auto_cap = min(auto_cap, 0.15)

            # Elite Core / Mega Chalk / Strong Mid-Tier overrides:
            # High-value or high-ownership plays get relaxed caps,
            # superseding tier classification and rotation confidence.
            if (
                player.salary and player.salary > 0
                and player.projected_fp and player.projected_fp > 0
            ):
                _vr = player.projected_fp / (player.salary / 1000)
                _opt_pct = getattr(player, "optimal_pct", None) or 0.0
                if _vr > ELITE_CORE_VALUE_THRESHOLD:
                    auto_cap = ELITE_CORE_MAX_EXPOSURE
                elif (
                    _vr > MEGA_CHALK_VALUE_THRESHOLD
                    or (
                        player.estimated_ownership is not None
                        and player.estimated_ownership
                        >= MEGA_CHALK_OWNERSHIP_THRESHOLD
                    )
                ):
                    auto_cap = MEGA_CHALK_MAX_EXPOSURE
                elif (
                    STRONG_MID_TIER_SALARY_MIN
                    <= player.salary
                    <= STRONG_MID_TIER_SALARY_MAX
                    and (
                        _vr >= STRONG_MID_TIER_VALUE_THRESHOLD
                        or _opt_pct >= STRONG_MID_TIER_OPTIMAL_THRESHOLD
                    )
                ):
                    auto_cap = max(auto_cap, STRONG_MID_TIER_MAX_EXPOSURE)

            # Merge: take the minimum of auto-cap and any user-set cap
            if pid in merged_caps:
                merged_caps[pid] = min(merged_caps[pid], auto_cap)
            elif auto_cap != global_cap:
                # Add if auto-cap differs from global (more or less restrictive)
                merged_caps[pid] = auto_cap

        # Count strong mid-tier players for logging
        _strong_mid_count = sum(
            1 for p in sorted_pool
            if (
                p.salary and p.salary > 0
                and p.projected_fp and p.projected_fp > 0
                and STRONG_MID_TIER_SALARY_MIN <= p.salary <= STRONG_MID_TIER_SALARY_MAX
                and (
                    p.projected_fp / (p.salary / 1000) >= STRONG_MID_TIER_VALUE_THRESHOLD
                    or (getattr(p, "optimal_pct", None) or 0.0) >= STRONG_MID_TIER_OPTIMAL_THRESHOLD
                )
            )
        )

        # ── ABSOLUTE GLOBAL CEILING ──────────────────────────────────
        # Regardless of tier classification, value ratio, or ownership,
        # NO player can exceed ABSOLUTE_GLOBAL_MAX_EXPOSURE.
        # This is the final guardrail against catastrophic ruin.
        _clamped_count = 0
        for pid in list(merged_caps.keys()):
            if merged_caps[pid] > ABSOLUTE_GLOBAL_MAX_EXPOSURE:
                merged_caps[pid] = ABSOLUTE_GLOBAL_MAX_EXPOSURE
                _clamped_count += 1
        # Also enforce on the global_cap itself
        global_cap = min(global_cap, ABSOLUTE_GLOBAL_MAX_EXPOSURE)

        logger.info(
            f"[AutoExposure] Tier caps applied: "
            f"{stud_cutoff} studs (cap={global_cap:.0%}), "
            f"{mid_cutoff - stud_cutoff} mid-tier "
            f"(cap={GPP_AUTO_EXPO_MID_CAP:.0%}), "
            f"{_strong_mid_count} strong mid-tier "
            f"(cap={STRONG_MID_TIER_MAX_EXPOSURE:.0%}), "
            f"{n - mid_cutoff} value/punt "
            f"(cap={GPP_AUTO_EXPO_VALUE_CAP:.0%}), "
            f"total capped: {len(merged_caps)} players"
            f" | Absolute ceiling {ABSOLUTE_GLOBAL_MAX_EXPOSURE:.0%} "
            f"clamped {_clamped_count} players"
        )

        return merged_caps

    @staticmethod
    def _compute_auto_min_exposure(
        pool: List["PlayerPoolEntry"],
        contest_type: str,
        user_player_min_exposure: Dict[int, float],
    ) -> Dict[int, float]:
        """Set minimum exposure floors for top projected players in GPP.

        Identifies the top-3 projected players and assigns minimum exposure
        targets.  Does NOT override user-set minimums.
        """
        if contest_type not in ("gpp", "single_entry"):
            return dict(user_player_min_exposure)

        from app.config.constants import (
            GPP_AUTO_MIN_EXPO_TOP1,
            GPP_AUTO_MIN_EXPO_TOP2,
            GPP_AUTO_MIN_EXPO_TOP3,
            ELITE_CORE_VALUE_THRESHOLD,
            ELITE_CORE_MIN_EXPOSURE,
        )

        sorted_pool = sorted(
            pool, key=lambda p: p.projected_fp, reverse=True,
        )
        auto_mins = [
            GPP_AUTO_MIN_EXPO_TOP1,
            GPP_AUTO_MIN_EXPO_TOP2,
            GPP_AUTO_MIN_EXPO_TOP3,
        ]

        merged: Dict[int, float] = dict(user_player_min_exposure)

        for i, min_target in enumerate(auto_mins):
            if i >= len(sorted_pool):
                break
            pid = sorted_pool[i].player_id
            if pid not in merged:
                merged[pid] = min_target
                logger.info(
                    f"[AutoMinExposure] #{i + 1} projected player "
                    f"{sorted_pool[i].player_name} "
                    f"(id={pid}, proj={sorted_pool[i].projected_fp:.1f}) "
                    f"→ min exposure {min_target:.0%}"
                )

        # Elite Core boost: top projected players with extreme value
        # get a higher min-exposure floor (0.80) to ensure dominant
        # plays are not under-represented.
        for i in range(min(len(auto_mins), len(sorted_pool))):
            p = sorted_pool[i]
            pid = p.player_id
            if (
                p.salary and p.salary > 0
                and p.projected_fp and p.projected_fp > 0
            ):
                vr = p.projected_fp / (p.salary / 1000)
                if vr > ELITE_CORE_VALUE_THRESHOLD:
                    new_min = max(
                        merged.get(pid, 0.0),
                        ELITE_CORE_MIN_EXPOSURE,
                    )
                    merged[pid] = new_min
                    logger.info(
                        f"[AutoMinExposure] Elite Core boost: "
                        f"{p.player_name} → min {new_min:.0%} "
                        f"(value={vr:.2f}x)"
                    )

        return merged

    @staticmethod
    def _select_contrarian_locks(
        pool: List["PlayerPoolEntry"],
        n_lineups: int,
        rng: "random.Random",
    ) -> List[Optional[int]]:
        """Select low-owned upside players to force-lock into GPP lineups.

        For portfolios of 10+ lineups, returns a list of length n_lineups
        where ~25% of entries contain a player_id to force-lock, and the
        rest are None.

        Eligible players: ownership < 5%, ceiling > 1.5× proj, proj >= 10.
        """
        from app.config.constants import (
            GPP_CONTRARIAN_SLOT_PCT,
            GPP_CONTRARIAN_MAX_OWNERSHIP,
            GPP_CONTRARIAN_MIN_UPSIDE_RATIO,
            GPP_CONTRARIAN_MIN_PROJECTION,
            GPP_CONTRARIAN_MIN_LINEUPS,
        )

        assignments: List[Optional[int]] = [None] * n_lineups

        if n_lineups < GPP_CONTRARIAN_MIN_LINEUPS:
            return assignments

        # Find eligible contrarian plays
        eligible = [
            p for p in pool
            if (
                p.estimated_ownership is not None
                and p.estimated_ownership < GPP_CONTRARIAN_MAX_OWNERSHIP
                and p.projected_fp >= GPP_CONTRARIAN_MIN_PROJECTION
                and p.ceiling_fp > p.projected_fp * GPP_CONTRARIAN_MIN_UPSIDE_RATIO
            )
        ]

        if not eligible:
            logger.info(
                "[ContrarianSlot] No eligible low-owned upside players found"
            )
            return assignments

        # Sort by upside ratio descending for diversity
        eligible.sort(
            key=lambda p: p.ceiling_fp / max(p.projected_fp, 1.0),
            reverse=True,
        )

        n_contrarian = max(1, int(n_lineups * GPP_CONTRARIAN_SLOT_PCT))

        # Assign contrarian locks to random lineup indices
        contrarian_indices = rng.sample(
            range(n_lineups), min(n_contrarian, n_lineups),
        )

        for idx in contrarian_indices:
            # Rotate through eligible players for diversity
            pick = eligible[idx % len(eligible)]
            assignments[idx] = pick.player_id

        picked_names = set()
        for a in assignments:
            if a is not None:
                p = next((pp for pp in pool if pp.player_id == a), None)
                if p:
                    picked_names.add(p.player_name)

        logger.info(
            f"[ContrarianSlot] Assigned {n_contrarian} contrarian locks "
            f"from {len(eligible)} eligible players: {picked_names}"
        )

        return assignments

    # ------------------------------------------------------------------
    # Continuous ownership leverage
    # ------------------------------------------------------------------

    def _calculate_slate_value_threshold(
        self, pool: list,
    ) -> tuple[float, float]:
        """Compute dynamic slate-relative value threshold.

        Returns (threshold, ceiling) based on the 90th percentile of
        value_ratio = projected_fp / (salary / 1000) across the slate.

        On an injury-heavy slate where many players project at 5.2-5.5x,
        the 90th percentile might be ~5.1x — meaning a 50% owned player
        at 5.3x value EXCEEDS the threshold and bypasses the penalty.

        Falls back to static constants if pool is empty/invalid.
        """
        from app.config.constants import (
            GOOD_CHALK_VALUE_THRESHOLD,
            GOOD_CHALK_VALUE_CEILING,
            GOOD_CHALK_VALUE_PERCENTILE,
        )
        values = []
        for p in pool:
            if (
                getattr(p, "projected_fp", 0) and p.projected_fp > 0
                and getattr(p, "salary", 0) and p.salary > 0
            ):
                values.append(p.projected_fp / (p.salary / 1000))

        if len(values) < 5:
            return GOOD_CHALK_VALUE_THRESHOLD, GOOD_CHALK_VALUE_CEILING

        values.sort()
        idx = int(len(values) * GOOD_CHALK_VALUE_PERCENTILE)
        idx = min(idx, len(values) - 1)
        p90 = values[idx]

        # Threshold = 90th percentile (players above this are "good chalk")
        # Ceiling = threshold + 2.0 (players here are fully immune)
        threshold = p90
        ceiling = threshold + 2.0

        logger.info(
            f"[ValueThreshold] Dynamic: p90={p90:.2f}, threshold={threshold:.2f}, "
            f"ceiling={ceiling:.2f} (from {len(values)} players)"
        )
        return threshold, ceiling

    def _ownership_leverage_multiplier(
        self, ownership_pct: float, strategy: str,
        value_ratio: float | None = None,
    ) -> float:
        """Sigmoid ownership leverage with value-adjusted dampening.

        Uses logistic function: Penalty = Max_Penalty / (1 + e^(-k*(own - midpoint)))

        Penalty at key ownership levels (default k=12, midpoint=0.40):
          -  5% owned → penalty ≈ 0.02%  → multiplier ≈ 1.000
          - 25% owned → penalty ≈ 2.2%   → multiplier ≈ 0.978
          - 45% owned → penalty ≈ 10.6%  → multiplier ≈ 0.894
          - 60% owned → penalty ≈ 14.7%  → multiplier ≈ 0.853

        The 'Good Chalk' value dampener is applied AFTER the sigmoid,
        so elite plays (> 6.0x value) can bypass the curve entirely.
        """
        import math
        from app.config.constants import (
            OWNERSHIP_SIGMOID_K,
            OWNERSHIP_SIGMOID_MIDPOINT,
            OWNERSHIP_SIGMOID_CONTRARIAN_K,
            OWNERSHIP_SIGMOID_CONTRARIAN_MIDPOINT,
            OWNERSHIP_MAX_PENALTY_PCT,
        )

        if ownership_pct <= 0:
            return 1.15  # Moderate boost for zero-owned

        # Contest-driven override (LineupStrategy alpha=0 → cash, no leverage)
        if self._lineup_strategy:
            _ls_alpha = self._lineup_strategy.ownership_leverage_alpha
            if _ls_alpha == 0.0:
                return 1.0  # Cash: no leverage

        is_contrarian = strategy == "contrarian"

        # Select sigmoid parameters by strategy
        k = (OWNERSHIP_SIGMOID_CONTRARIAN_K if is_contrarian
             else OWNERSHIP_SIGMOID_K)
        midpoint = (OWNERSHIP_SIGMOID_CONTRARIAN_MIDPOINT if is_contrarian
                    else OWNERSHIP_SIGMOID_MIDPOINT)

        # CalibrationService scaling (k ↔ alpha, midpoint ↔ baseline)
        if self.calibration_service:
            cal_alpha = self.calibration_service.get_ownership_leverage_alpha(strategy)
            if cal_alpha != 1.0:
                k *= cal_alpha  # Scale steepness
            cal_baseline = self.calibration_service.get_ownership_leverage_baseline(strategy)
            if cal_baseline != 1.0:
                midpoint *= cal_baseline  # Shift midpoint

        # LineupStrategy scaling (non-cash, non-zero alpha)
        if self._lineup_strategy:
            from app.config.constants import (
                OWNERSHIP_LEVERAGE_ALPHA,
                OWNERSHIP_LEVERAGE_BASELINE,
            )
            _ls_alpha = self._lineup_strategy.ownership_leverage_alpha
            _ls_baseline = self._lineup_strategy.ownership_leverage_baseline
            if _ls_alpha != OWNERSHIP_LEVERAGE_ALPHA:
                k *= _ls_alpha / max(0.01, OWNERSHIP_LEVERAGE_ALPHA)
            if _ls_baseline != OWNERSHIP_LEVERAGE_BASELINE:
                midpoint *= _ls_baseline / max(0.01, OWNERSHIP_LEVERAGE_BASELINE)

        # Slate-size adaptive scaling
        if hasattr(self, '_slate_adjustments') and self._slate_adjustments:
            k *= self._slate_adjustments.get("alpha_mult", 1.0)

        # Sigmoid penalty: Penalty = Max_Penalty / (1 + e^(-k * (own - midpoint)))
        own_dec = ownership_pct / 100.0
        penalty = OWNERSHIP_MAX_PENALTY_PCT / (1.0 + math.exp(-k * (own_dec - midpoint)))

        # Apply value dampener (Good Chalk bypass for elite plays)
        penalty = self._apply_value_dampener(penalty, value_ratio)

        return 1.0 - penalty

    def _apply_value_dampener(
        self, penalty: float, value_ratio: float | None,
    ) -> float:
        """Dampen ownership penalty for high-value players.

        Uses the dynamic slate threshold if computed, else falls back
        to static constants.  Players above threshold get exponentially
        reduced penalty; at ceiling the penalty is zeroed entirely.
        """
        if value_ratio is None or penalty <= 0:
            return penalty

        # Use dynamic threshold if available (set during generate_lineups)
        threshold = getattr(self, "_slate_value_threshold", None)
        ceiling = getattr(self, "_slate_value_ceiling", None)

        if threshold is None or ceiling is None:
            from app.config.constants import (
                GOOD_CHALK_VALUE_THRESHOLD,
                GOOD_CHALK_VALUE_CEILING,
            )
            threshold = GOOD_CHALK_VALUE_THRESHOLD
            ceiling = GOOD_CHALK_VALUE_CEILING

        if value_ratio >= ceiling:
            return 0.0  # Fully immune
        elif value_ratio > threshold:
            # Linear dampener: 1.0 at threshold → 0.0 at ceiling
            dampener = 1.0 - (value_ratio - threshold) / (ceiling - threshold)
            return penalty * dampener
        return penalty  # Below threshold: full penalty

    # ------------------------------------------------------------------
    # Contest-driven strategy resolution
    # ------------------------------------------------------------------

    def _resolve_contest_strategy(self, contest_id: str):
        """Fetch contest detail and determine strategy via Agent 2.

        Returns a ``LineupStrategy`` or ``None`` on failure.
        """
        try:
            from app.api.dependencies import get_services
            svc = get_services()
            detail = svc.dk_contest_detail_service.get_contest_detail(contest_id)
            if not detail:
                logger.warning(
                    f"[ContestStrategy] No detail for contest {contest_id}"
                )
                return None
            if not self.lineup_strategy_agent:
                logger.warning(
                    "[ContestStrategy] No strategy agent available"
                )
                return None
            return self.lineup_strategy_agent.determine_contest_strategy(detail)
        except Exception as e:
            logger.warning(f"[ContestStrategy] Resolution failed: {e}")
            return None

    # ------------------------------------------------------------------
    # Sim-Optimal lineup generation
    # ------------------------------------------------------------------

    def _build_lineup_from_assignment(
        self,
        lineup: Dict[str, "PlayerPoolEntry"],
        platform: str,
        salary_cap: int,
        roster_slots: List[str],
        sport: str = "nba",
        fp_override: Optional[float] = None,
    ) -> Optional[OptimizedLineup]:
        """Convert an ``{indexed_slot: PlayerPoolEntry}`` dict to an
        :class:`OptimizedLineup` response object.

        Factored out of ``_build_single_lineup`` so that sim-optimal and
        other direct-ILP paths can build responses without duplicating
        slot-mapping and salary-total logic.

        Parameters
        ----------
        fp_override : float, optional
            If provided, use this as ``total_projected_fp`` instead of
            summing ``projected_fp`` from pool entries.  Useful when the
            objective value came from a simulation iteration rather than
            the median projection.
        """
        total_salary = sum(p.salary for p in lineup.values())
        total_fp = fp_override if fp_override is not None else sum(
            p.projected_fp for p in lineup.values()
        )
        total_floor = sum(p.floor_fp for p in lineup.values())
        total_ceil = sum(p.ceiling_fp for p in lineup.values())

        # ── Environmental adjustment totals (Prompt 7.2) ──────────────
        # Surface ``total_adjusted_fp`` only when at least one selected
        # player has an env adjustment — keeps the response shape clean
        # for NBA / NFL / CBB lineups that don't run through the MLB
        # park-factor pass. For MLB lineups where some players resolved
        # a venue and others didn't, the unresolved ones contribute their
        # raw ``projected_fp`` so the total stays meaningful.
        any_adjusted = any(
            p.adjusted_fp is not None for p in lineup.values()
        )
        if any_adjusted:
            total_adj = sum(
                (p.adjusted_fp if p.adjusted_fp is not None else p.projected_fp)
                for p in lineup.values()
            )
            total_adjusted_fp: Optional[float] = round(total_adj, 1)
        else:
            total_adjusted_fp = None

        indexed_roster = _index_slots(roster_slots)
        players: List[LineupPlayer] = []
        for isl in indexed_roster:
            p = lineup.get(isl)
            if p:
                players.append(
                    LineupPlayer(
                        player_id=p.player_id,
                        player_name=p.player_name,
                        display_name=p.display_name or p.player_name,
                        position=p.position,
                        roster_slot=_base_slot(isl),
                        team_abbreviation=p.team_abbreviation,
                        salary=p.salary,
                        projected_fp=p.projected_fp,
                        floor_fp=p.floor_fp,
                        ceiling_fp=p.ceiling_fp,
                        projected_minutes=p.projected_minutes,
                        projected_stats=p.projected_stats,
                        dk_player_id=p.dk_player_id,
                        # Carry env adjustment through to the response so
                        # the lineup card can render the ±% badge per
                        # player without an extra lookup.
                        adjusted_fp=p.adjusted_fp,
                    )
                )

        if not players:
            return None

        return OptimizedLineup(
            platform=platform,
            sport=sport,
            players=players,
            total_salary=total_salary,
            salary_remaining=salary_cap - total_salary,
            total_projected_fp=round(total_fp, 1),
            total_floor_fp=round(total_floor, 1),
            total_ceiling_fp=round(total_ceil, 1),
            total_adjusted_fp=total_adjusted_fp,
            salary_cap=salary_cap,
            roster_slots=roster_slots,
        )

    def _build_dk_game_context(
        self,
        draft_group_id: int,
    ) -> Dict[str, dict]:
        """Build game_lookup from DK draftables API competitions.

        Fallback when stats.nba.com is unreachable.  Constructs synthetic
        :class:`GameInfo` objects using league-average pace/scoring defaults.
        The simulation engine uses these for pace sampling and score targets.
        """
        import httpx as _httpx
        from app.models.game import GameInfo, TeamGameStats

        try:
            url = f"https://api.draftkings.com/draftgroups/v1/draftgroups/{draft_group_id}/draftables"
            resp = _httpx.get(
                url, timeout=15,
                headers={"User-Agent": "Mozilla/5.0"},
                follow_redirects=True,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.warning(f"[SimOptimal] DK competitions fetch failed: {e}")
            return {}

        competitions = data.get("competitions", [])
        if not competitions:
            return {}

        # League-average defaults for synthetic GameInfo
        _DEFAULT_PACE = 100.0
        _DEFAULT_PPG = 114.0
        _DEFAULT_DEF_RTG = 114.0
        _DEFAULT_OFF_RTG = 114.0

        game_lookup: Dict[str, dict] = {}
        for comp in competitions:
            home = comp.get("homeTeam", {})
            away = comp.get("awayTeam", {})
            home_abbr = (home.get("abbreviation") or "").upper()
            away_abbr = (away.get("abbreviation") or "").upper()
            if not home_abbr or not away_abbr:
                continue

            comp_id = comp.get("competitionId", 0)
            game_id = f"DK_{comp_id}"

            home_stats = TeamGameStats(
                team_id=home.get("teamId", 0),
                team_name=home.get("teamName", home_abbr),
                team_abbreviation=home_abbr,
                season_pace=_DEFAULT_PACE,
                season_off_rating=_DEFAULT_OFF_RTG,
                season_def_rating=_DEFAULT_DEF_RTG,
                season_ppg=_DEFAULT_PPG,
                season_opp_ppg=_DEFAULT_PPG,
                last_5_ppg=_DEFAULT_PPG,
            )
            away_stats = TeamGameStats(
                team_id=away.get("teamId", 0),
                team_name=away.get("teamName", away_abbr),
                team_abbreviation=away_abbr,
                season_pace=_DEFAULT_PACE,
                season_off_rating=_DEFAULT_OFF_RTG,
                season_def_rating=_DEFAULT_DEF_RTG,
                season_ppg=_DEFAULT_PPG,
                season_opp_ppg=_DEFAULT_PPG,
                last_5_ppg=_DEFAULT_PPG,
            )

            game_info = GameInfo(
                game_id=game_id,
                game_date=date.today().isoformat(),
                game_status="Scheduled",
                home_team=home_stats,
                away_team=away_stats,
                projected_total=_DEFAULT_PPG * 2,
                projected_home_score=_DEFAULT_PPG,
                projected_away_score=_DEFAULT_PPG,
                projected_spread=0.0,
                projected_pace=_DEFAULT_PACE,
                pace_label="Average",
            )

            game_lookup[home_abbr] = {
                "pace": _DEFAULT_PACE,
                "total": _DEFAULT_PPG * 2,
                "opp_def": _DEFAULT_DEF_RTG,
                "game_info": game_info,
                "game_id": game_id,
                "opponent": away_abbr,
            }
            game_lookup[away_abbr] = {
                "pace": _DEFAULT_PACE,
                "total": _DEFAULT_PPG * 2,
                "opp_def": _DEFAULT_DEF_RTG,
                "game_info": game_info,
                "game_id": game_id,
                "opponent": home_abbr,
            }

        logger.info(
            f"[SimOptimal] Built DK fallback game context: "
            f"{len(competitions)} games, {len(game_lookup)} team entries"
        )
        self._game_lookup_cache = game_lookup
        return game_lookup

    def _generate_sim_optimal(
        self,
        pool: List[PlayerPoolEntry],
        platform: str,
        salary_cap: int,
        roster_slots: List[str],
        slot_order: List[str],
        request: "MultiLineupRequest",
        game_lookup: Dict[str, dict],
        sport: str = "nba",
    ) -> List[OptimizedLineup]:
        """Generate lineups via simulation-optimal iteration selection.

        For each candidate lineup:
          1. Pick a unique Monte Carlo iteration (a simulated "world")
          2. Use that iteration's exact per-player FP values as ILP
             objective coefficients
          3. Solve the ILP for the provably optimal lineup in that world
          4. Portfolio-select the best diverse final set

        Game stacking emerges organically — when a simulation iteration
        has a high-scoring game, the ILP naturally selects multiple
        players from that game because their FP values are high.
        """
        import numpy as np
        from concurrent.futures import ThreadPoolExecutor, as_completed

        from app.config.constants import SALARY_UTILIZATION_HARD_FLOOR
        from app.models.simulation import SimulationConfig
        from app.services.simulation_engine import SimulationEngine

        t0 = time.time()
        n_requested = request.num_lineups
        fp_key = "dk" if platform == "dk" else "fd"

        # ── Phase 1: Run simulations for all games ────────────────
        # Determine simulation count: enough for overgeneration but
        # not wasteful.  Minimum 500 for stable within-game correlations.
        overgen_mult = 3 if n_requested <= 20 else 2
        n_candidates_target = max(n_requested * overgen_mult, 6)
        num_sims = max(500, min(n_candidates_target + 100, 2000))

        sim_config = SimulationConfig(
            num_simulations=num_sims,
            seed=getattr(request, "seed", None),
        )

        # Deduplicate games (each game appears in game_lookup twice,
        # once per team abbreviation)
        seen_game_ids: set = set()
        unique_games = []
        for g_ctx in game_lookup.values():
            g = g_ctx["game_info"]
            gid = getattr(g, "game_id", None) or id(g)
            if gid not in seen_game_ids:
                seen_game_ids.add(gid)
                unique_games.append(g)

        _cached_teams = getattr(self, "_team_data_cache", {})
        _noise_overrides = getattr(self, "_sim_noise_overrides", None)

        # Collect raw FP vectors: {player_id: ndarray(num_sims,)}
        all_raw_fps: Dict[int, "np.ndarray"] = {}

        def _sim_game(g):
            """Simulate one game, return {pid: ndarray(N,)}."""
            local_sim = SimulationEngine(sim_config)
            home_abbr = g.home_team.team_abbreviation.upper()
            away_abbr = g.away_team.team_abbreviation.upper()
            home_cached = _cached_teams.get(home_abbr)
            away_cached = _cached_teams.get(away_abbr)
            if not (home_cached and away_cached):
                logger.warning(
                    f"[SimOptimal] Missing cached team data for "
                    f"{home_abbr} or {away_abbr} — skipping game"
                )
                return {}
            try:
                _, raw_fps = local_sim.simulate_game_raw(
                    game_info=g,
                    home_rotation=home_cached["projected"],
                    home_players=home_cached["rotation"],
                    away_rotation=away_cached["projected"],
                    away_players=away_cached["rotation"],
                    player_noise_overrides=_noise_overrides,
                    sport=sport,
                )
                # Extract platform-specific FP arrays
                result = {}
                for pid, fp_dict in raw_fps.items():
                    result[pid] = fp_dict[fp_key]
                return result
            except Exception as e:
                logger.warning(
                    f"[SimOptimal] Sim failed for game "
                    f"{getattr(g, 'game_id', '?')}: {e}"
                )
                return {}

        # Run simulations in parallel
        with ThreadPoolExecutor(
            max_workers=6, thread_name_prefix="sim-opt"
        ) as sim_pool:
            futures = {
                sim_pool.submit(_sim_game, g): g for g in unique_games
            }
            for future in as_completed(futures):
                try:
                    game_fps = future.result(timeout=15)
                    all_raw_fps.update(game_fps)
                except Exception as e:
                    logger.warning(f"[SimOptimal] Game sim future error: {e}")

        sim_elapsed = time.time() - t0
        logger.info(
            f"[SimOptimal] Simulated {len(unique_games)} games, "
            f"{len(all_raw_fps)} players with raw vectors "
            f"({num_sims} iterations) in {sim_elapsed:.1f}s"
        )

        # ── Phase 2: Player ID mapping ────────────────────────────
        # For pool players not in any simulation, generate noise-based
        # FP arrays using their floor/ceiling range.  This provides
        # cross-iteration variance even when full game simulation is
        # unavailable (e.g. stats.nba.com down).  Each iteration still
        # produces a different "world" with different optimal lineups.
        pool_by_id = {p.player_id: p for p in pool}
        rng = np.random.default_rng(getattr(request, "seed", None))
        _n_synth = 0
        for pid, p in pool_by_id.items():
            if pid not in all_raw_fps:
                fp_mean = p.projected_fp
                fp_range = max(p.ceiling_fp - p.floor_fp, 1.0)
                fp_std = fp_range / 3.3  # ~99% within floor-ceiling
                synth = rng.normal(fp_mean, fp_std, num_sims)
                # Clip to reasonable bounds
                all_raw_fps[pid] = np.clip(
                    synth,
                    max(p.floor_fp * 0.5, 0.0),
                    p.ceiling_fp * 1.3,
                )
                _n_synth += 1
        if _n_synth:
            logger.info(
                f"[SimOptimal] Synthetic noise arrays for {_n_synth} "
                f"players (no sim data available)"
            )

        # ── Phase 2.5: Rescale sim arrays to match projected_fp ───
        # Sim-engine output is derived from historical stats, so a
        # player's raw FP array mean may not equal their displayed
        # `projected_fp` (which now includes CSV-imported overrides
        # via `_apply_imported_projection_overrides`).  Rescale each
        # array so its mean equals `projected_fp`, preserving the
        # sim's cross-player correlation structure (teammates,
        # opponents) and relative variance.
        n_rescaled = 0
        for pid, entry in pool_by_id.items():
            raw_array = all_raw_fps.get(pid)
            if raw_array is None or len(raw_array) == 0:
                continue
            sim_mean = float(np.mean(raw_array))
            target_mean = float(entry.projected_fp)
            if target_mean <= 0 or abs(sim_mean - target_mean) < 0.05:
                continue
            # Multiplicative preserves CV but blows up when sim_mean is
            # small or the ratio is extreme (raw values then mass-clip
            # to the ceiling, flattening the distribution).  Fall back
            # to additive shift in those cases.
            if (
                sim_mean >= 1.0
                and 0.33 <= target_mean / sim_mean <= 3.0
            ):
                adjusted = raw_array * (target_mean / sim_mean)
            else:
                adjusted = raw_array + (target_mean - sim_mean)
            all_raw_fps[pid] = np.clip(
                adjusted,
                max(entry.floor_fp * 0.5, 0.0),
                max(entry.ceiling_fp * 1.3, target_mean * 1.5),
            )
            n_rescaled += 1
        if n_rescaled:
            logger.info(
                f"[SimOptimal] Rescaled sim FP arrays for {n_rescaled} "
                f"players to align with projected_fp"
            )

        # ── Phase 3: Solve ILPs per iteration ─────────────────────
        n_candidates = min(n_candidates_target, num_sims)
        iteration_indices = rng.choice(
            num_sims, size=n_candidates, replace=False
        )

        indexed_slots = _index_slots(slot_order)
        salary_floor_pct = getattr(request, "salary_floor_pct", 0.98)
        salary_floor = int(salary_cap * salary_floor_pct)
        locked_ids = list(request.locked_players or [])
        excluded_set = set(request.excluded_players or [])

        # Optimality floor for sim-optimal path
        _sim_min_proj_floor = None
        _sim_baseline_score = None
        _sim_baseline_lineup: Optional[OptimizedLineup] = None
        _sim_opt_threshold = getattr(request, "optimality_threshold", None)
        if _sim_opt_threshold is not None and _PULP_AVAILABLE:
            _sim_baseline_score, _sim_baseline_lineup = (
                self._compute_baseline_projection_score(
                    pool=pool,
                    platform=platform,
                    salary_cap=salary_cap,
                    slot_order=slot_order,
                    locked_player_ids=locked_ids,
                    salary_floor=salary_floor,
                    mode=getattr(request, "mode", "classic"),
                    sport=sport,
                )
            )
            if _sim_baseline_score and _sim_baseline_score > 0:
                _sim_min_proj_floor = _sim_baseline_score * _sim_opt_threshold
                logger.info(
                    f"[SimOptimal] Optimality floor: "
                    f"baseline={_sim_baseline_score:.1f}, "
                    f"threshold={_sim_opt_threshold}, "
                    f"floor={_sim_min_proj_floor:.1f}"
                )
        # Store on instance for caller to include in response metadata
        self._sim_opt_baseline_score = _sim_baseline_score
        self._sim_opt_min_proj_floor = _sim_min_proj_floor
        self._sim_opt_baseline_lineup = _sim_baseline_lineup

        # Filter pool once
        available_pool = [
            p for p in pool
            if p.player_id not in excluded_set
            and (p.injury_status or "").upper() not in ("OUT", "DOUBTFUL")
        ]

        exposure: Dict[int, int] = {}
        max_appearances = None
        if (
            request.max_exposure is not None
            and 0 < request.max_exposure < 1.0
        ):
            max_appearances = max(1, int(request.max_exposure * n_requested))

        candidates: List[Tuple[OptimizedLineup, float]] = []
        ilp_failures = 0

        def _solve_iteration(iter_idx: int, cand_num: int):
            """Build and solve ILP for one simulation iteration."""
            # Build score map for this specific iteration
            iter_scores: Dict[int, float] = {}
            for pid in pool_by_id:
                if pid in all_raw_fps:
                    iter_scores[pid] = float(all_raw_fps[pid][iter_idx])
                else:
                    iter_scores[pid] = pool_by_id[pid].projected_fp

            def score_fn(p: PlayerPoolEntry) -> float:
                return iter_scores.get(p.player_id, p.projected_fp)

            # Apply exposure exclusions
            iter_pool = available_pool
            if max_appearances is not None:
                iter_pool = [
                    p for p in available_pool
                    if exposure.get(p.player_id, 0) < max_appearances
                ]

            # Build greedy warm-start for ILP speed
            try:
                greedy = self._greedy_fill_scored(
                    iter_pool, {}, list(indexed_slots),
                    set(), salary_cap, platform, score_fn,
                    sport=sport,
                )
            except Exception:
                greedy = {}

            warm_start = greedy if len(greedy) == len(indexed_slots) else None
            warm_score = (
                sum(score_fn(p) for p in greedy.values())
                if warm_start else None
            )

            # Solve ILP — no stacking constraints; simulation provides
            # natural game correlation
            _floor_for_iter = _sim_min_proj_floor
            ilp_result = self._ilp_optimize(
                pool=iter_pool,
                platform=platform,
                salary_cap=salary_cap,
                slot_order=slot_order,
                locked_player_ids=locked_ids,
                score_fn=score_fn,
                salary_floor=salary_floor,
                stack_game_id=None,
                stack_primary_team=None,
                stack_size=0,
                bring_back=False,
                mode=getattr(request, "mode", "classic"),
                sport=sport,
                warm_start_lineup=warm_start,
                warm_start_score=warm_score,
                contest_type=getattr(request, "contest_type", "gpp"),
                min_projection_floor=_floor_for_iter,
                max_cumulative_ownership=getattr(
                    request, "max_cumulative_ownership", None
                ),
            )
            # Dynamic relaxation for sim-optimal (skip on hard-floor breach)
            if (
                ilp_result is None
                and _floor_for_iter is not None
                and _sim_baseline_score
                and _sim_baseline_score > 0
            ):
                _st = _floor_for_iter / _sim_baseline_score
                _sim_hard_floor = getattr(
                    request, "minimum_relaxation_floor", 0.75
                )
                while ilp_result is None and _st >= _sim_hard_floor:
                    _st -= 0.05
                    if _st < _sim_hard_floor:
                        break
                    _floor_for_iter = _sim_baseline_score * _st
                    ilp_result = self._ilp_optimize(
                        pool=iter_pool,
                        platform=platform,
                        salary_cap=salary_cap,
                        slot_order=slot_order,
                        locked_player_ids=locked_ids,
                        score_fn=score_fn,
                        salary_floor=salary_floor,
                        stack_game_id=None,
                        stack_primary_team=None,
                        stack_size=0,
                        bring_back=False,
                        mode=getattr(request, "mode", "classic"),
                        sport=sport,
                        warm_start_lineup=warm_start,
                        warm_start_score=warm_score,
                        contest_type=getattr(request, "contest_type", "gpp"),
                        min_projection_floor=_floor_for_iter,
                        max_cumulative_ownership=getattr(
                            request, "max_cumulative_ownership", None
                        ),
                    )

            if not ilp_result or len(ilp_result) < len(indexed_slots):
                return None

            # Sim-world total FP — used only for portfolio ranking
            # (candidates[i][1]).  Displayed total_projected_fp comes
            # from sum(p.projected_fp) in _build_lineup_from_assignment
            # so the lineup row total matches the visible player rows
            # after CSV projection imports.
            sim_total = sum(
                iter_scores.get(p.player_id, p.projected_fp)
                for p in ilp_result.values()
            )

            lineup_obj = self._build_lineup_from_assignment(
                ilp_result, platform, salary_cap, roster_slots,
                sport=sport,
            )
            if lineup_obj is None:
                return None

            # Quality gate: salary utilization
            if salary_cap > 0:
                util = lineup_obj.total_salary / salary_cap
                if util < SALARY_UTILIZATION_HARD_FLOOR:
                    return None

            return lineup_obj, sim_total

        # Solve iterations — parallelized for speed.  CBC releases the
        # GIL during native solve, so threading helps.
        t_ilp_start = time.time()

        # Note: exposure tracking requires sequential processing when
        # max_exposure is set, to avoid over-counting.  When exposure
        # is unlimited, we can fully parallelize.
        if max_appearances is not None:
            # Sequential — exposure-aware
            for cand_num, iter_idx in enumerate(iteration_indices):
                result = _solve_iteration(int(iter_idx), cand_num)
                if result:
                    lu, score = result
                    candidates.append((lu, score))
                    for p in lu.players:
                        exposure[p.player_id] = exposure.get(
                            p.player_id, 0
                        ) + 1
                else:
                    ilp_failures += 1
        else:
            # Parallel — no exposure tracking needed
            with ThreadPoolExecutor(
                max_workers=4, thread_name_prefix="sim-ilp"
            ) as ilp_pool:
                futures = {
                    ilp_pool.submit(
                        _solve_iteration, int(idx), i
                    ): i
                    for i, idx in enumerate(iteration_indices)
                }
                for future in as_completed(futures):
                    try:
                        result = future.result(timeout=10)
                        if result:
                            candidates.append(result)
                        else:
                            ilp_failures += 1
                    except Exception as e:
                        logger.debug(
                            f"[SimOptimal] ILP future error: {e}"
                        )
                        ilp_failures += 1

        ilp_elapsed = time.time() - t_ilp_start
        logger.info(
            f"[SimOptimal] Solved {len(candidates)} / "
            f"{n_candidates} ILPs in {ilp_elapsed:.1f}s "
            f"({ilp_failures} failures)"
        )

        if not candidates:
            raise RuntimeError(
                "SimOptimal produced zero valid lineups. "
                "Check pool size and constraints."
            )

        # ── Phase 4: Portfolio selection ───────────────────────────
        # Reuse existing diversity-aware selection infrastructure.
        selected: Optional[List[OptimizedLineup]] = None
        max_overlap = request.max_overlap

        if (
            _PULP_AVAILABLE
            and n_requested >= 3
            and len(candidates) <= 450
        ):
            selected = self._portfolio_optimize(
                candidates, n_requested,
                max_overlap=max_overlap,
                elite_core_pids=getattr(self, "_elite_core_pids", None),
            )

        if not selected:
            selected = self._select_best_diverse(
                candidates, n_requested, max_overlap,
            )

        # Relaxed fallback if strict diversity can't fill N lineups
        if len(selected) < n_requested and max_overlap < len(roster_slots) - 1:
            relaxed = self._select_best_diverse(
                candidates, n_requested,
                min(max_overlap + 1, len(roster_slots) - 1),
            )
            if len(relaxed) > len(selected):
                selected = relaxed

        total_elapsed = time.time() - t0
        logger.info(
            f"[SimOptimal] Complete: {len(selected)} lineups selected "
            f"from {len(candidates)} candidates in {total_elapsed:.1f}s"
        )

        return selected

    # ------------------------------------------------------------------
    # Composite scoring
    # ------------------------------------------------------------------

    @staticmethod
    def _effective_projection(player: PlayerPoolEntry) -> float:
        """Return the projection the optimizer should maximise for ``player``.

        Routes through ``adjusted_fp`` when it's set (Prompt 6.1: MLB
        park-factor pass populates this) and falls back to ``projected_fp``
        otherwise. Non-MLB sports leave ``adjusted_fp`` at ``None`` so
        their behaviour is byte-identical to the pre-park-factor path.

        The ``projected_fp`` field itself is never modified — it stays
        as the raw CSV/source value so the UI keeps showing what the
        user typed. ``adjusted_fp`` is the optimizer-internal value.
        """
        if player.adjusted_fp is not None:
            return player.adjusted_fp
        return player.projected_fp

    def _compute_composite_score(
        self,
        player: PlayerPoolEntry,
        strategy: str,
        exposure: Dict[int, int],
        lineup_idx: int,
        rng: random.Random,
        contest_type: str = "gpp",
        sport: str = "nba",
    ) -> float:
        """Score a player for lineup selection based on strategy and contest type.

        GPP tournaments demand high-ceiling, low-ownership, correlated
        lineups that can finish in the top 1%.  Cash games want safe
        floor-based builds.  This scoring function adapts accordingly.

        Applies expert confidence boost, AI strategy modifiers,
        ownership leverage (GPP), and exposure penalty.
        """
        boost = 1.0 + player.expert_confidence_boost
        is_gpp = contest_type in ("gpp", "single_entry")

        # Park-aware projection (Prompt 6.1): MLB pool entries carry
        # ``adjusted_fp`` after the venue-based multiplier is applied
        # in ``_enrich_pool``. Routing every projection read in this
        # function through ``_effective_projection`` makes the entire
        # composite score park-aware — Coors hitters score higher,
        # Petco hitters score lower, while non-MLB sports stay
        # byte-identical because ``adjusted_fp`` is None for them.
        proj_fp = self._effective_projection(player)

        # ── Contest-driven weights (from LineupStrategy) ────────────
        # When a LineupStrategy is active (contest_id was provided),
        # use its explicit w_p50 / w_p90 / w_floor weights and
        # ownership alpha instead of the hardcoded strategy branches.
        _ls = self._lineup_strategy
        if _ls and _ls.solver_path == "composite":
            p50 = player.sim_p50 if player.sim_p50 else proj_fp
            p90 = player.sim_p90 if player.sim_p90 else player.ceiling_fp
            floor_val = player.sim_p10 if player.sim_p10 else player.floor_fp
            base = (_ls.w_p50 * p50 + _ls.w_p90 * p90 + _ls.w_floor * floor_val) * boost

            # Ownership leverage with contest-driven alpha
            if (
                _ls.ownership_leverage_alpha > 0.0
                and player.estimated_ownership is not None
            ):
                _vr = (
                    proj_fp / (player.salary / 1000)
                    if player.salary and player.salary > 0 and proj_fp
                    else None
                )
                base *= self._ownership_leverage_multiplier(
                    player.estimated_ownership, strategy, value_ratio=_vr
                )

        # ── Base score by strategy (manual / no contest_id) ─────────
        elif strategy == "pure_max":
            # Pure projection maximization — no ceiling blend, no variance
            # reward.  Designed for users who want the highest raw projected
            # FP lineups possible.
            base = proj_fp * boost

        elif strategy == "max_projection":
            if is_gpp:
                # GPP: even "max projection" should lean toward upside
                upside = player.sim_p90 if player.sim_p90 else player.ceiling_fp
                base = (0.70 * proj_fp + 0.30 * upside) * boost
            else:
                base = proj_fp * boost

        elif strategy == "balanced":
            if is_gpp:
                base = (
                    0.35 * proj_fp
                    + 0.15 * player.floor_fp
                    + 0.50 * player.ceiling_fp
                ) * boost
            else:
                # Cash: use sim_p10 (simulation floor) when available for
                # a more accurate floor estimate than the rule-based floor_fp
                floor = player.sim_p10 if player.sim_p10 else player.floor_fp
                base = (
                    0.50 * proj_fp
                    + 0.25 * floor
                    + 0.25 * player.ceiling_fp
                ) * boost

        elif strategy == "ceiling":
            upside = player.sim_p90 if player.sim_p90 else player.ceiling_fp
            if is_gpp:
                # GPP ceiling: maximize upside, minimal floor drag
                base = (0.15 * proj_fp + 0.85 * upside) * boost
            else:
                base = (0.4 * proj_fp + 0.6 * upside) * boost
            # Penalize B2B players (higher injury/rest risk)
            if player.is_b2b:
                base *= 0.93 if is_gpp else 0.95

        elif strategy == "contrarian":
            # Contrarian / chalk-fade: maximize ceiling while heavily
            # penalizing high-ownership players.  Designed for large-
            # field GPPs where differentiation beats raw projection.
            upside = player.sim_p90 if player.sim_p90 else player.ceiling_fp
            base = (0.25 * proj_fp + 0.75 * upside) * boost
            # Continuous ownership leverage (replaces hard-coded step functions)
            if player.estimated_ownership is not None:
                _vr = (
                    proj_fp / (player.salary / 1000)
                    if player.salary and player.salary > 0 and proj_fp
                    else None
                )
                base *= self._ownership_leverage_multiplier(
                    player.estimated_ownership, "contrarian", value_ratio=_vr
                )
            if player.is_b2b:
                base *= 0.90
        else:
            base = proj_fp * boost

        # ── GPP: Punt play floor-quality gate ───────────────────────
        # Low-salary players with near-zero floors get penalised in GPP
        # to prevent over-exposure to "punt plays" that bust.
        if is_gpp and player.salary > 0:
            from app.config.constants import (
                GPP_PUNT_FLOOR_GATE_SALARY,
                GPP_PUNT_FLOOR_MILD_THRESHOLD,
                GPP_PUNT_FLOOR_SEVERE_THRESHOLD,
                GPP_PUNT_FLOOR_MILD_PENALTY,
                GPP_PUNT_FLOOR_SEVERE_PENALTY,
            )
            # Skip punt floor gate for players with valid DK FPPG data —
            # they have real historical performance backing their projection.
            if player.salary < GPP_PUNT_FLOOR_GATE_SALARY and not player.dk_fppg:
                _floor_val = (
                    player.sim_p10
                    if player.sim_p10 is not None
                    else player.floor_fp
                )
                if _floor_val < GPP_PUNT_FLOOR_SEVERE_THRESHOLD:
                    base *= GPP_PUNT_FLOOR_SEVERE_PENALTY
                elif _floor_val < GPP_PUNT_FLOOR_MILD_THRESHOLD:
                    base *= GPP_PUNT_FLOOR_MILD_PENALTY

        # ── GPP: Continuous ownership leverage ────────────────────────
        # Smooth power-law curve replaces step functions to avoid cliff
        # effects at ownership boundaries.  Skipped when LineupStrategy
        # already applied ownership leverage above.
        if (
            not _ls
            and is_gpp
            and strategy != "contrarian"
            and player.estimated_ownership is not None
        ):
            _vr = (
                proj_fp / (player.salary / 1000)
                if player.salary and player.salary > 0 and proj_fp
                else None
            )
            base *= self._ownership_leverage_multiplier(
                player.estimated_ownership, strategy, value_ratio=_vr
            )

        # ── GPP: Sim-to-Optimal Leverage ──────────────────────────────
        # When Monte Carlo leverage ratios are available, apply a smooth
        # multiplier that boosts under-owned optimal plays and penalizes
        # over-owned chalk whose sim upside doesn't justify their price.
        #
        # The leverage_ratio = (optimal_pct / ownership_pct):
        #   > 1.5 → market undervalues this player → boost score
        #   0.8-1.5 → fairly valued → minimal adjustment
        #   < 0.8 → market overvalues (chalk trap) → penalize
        #
        # Multiplier formula (smooth sigmoid-like):
        #   mult = 1.0 + SIM_LEVERAGE_SCALE × (ratio - 1.0)
        #   clamped to [SIM_LEVERAGE_FLOOR, SIM_LEVERAGE_CAP]
        if (
            is_gpp
            and getattr(player, "sim_leverage_ratio", None) is not None
        ):
            _slr = player.sim_leverage_ratio
            from app.config.constants import (
                SIM_LEVERAGE_SCALE,
                SIM_LEVERAGE_CAP,
                SIM_LEVERAGE_FLOOR,
            )
            _lev_mult = 1.0 + SIM_LEVERAGE_SCALE * (_slr - 1.0)
            _lev_mult = max(SIM_LEVERAGE_FLOOR, min(SIM_LEVERAGE_CAP, _lev_mult))
            base *= _lev_mult

        # ── GPP: Game environment boost ───────────────────────────────
        # Continuous linear curve replaces stepwise thresholds to
        # eliminate cliff effects (e.g. 239 total → +9.5% vs old +5%).
        # boost = (total - baseline) × scale, clamped to [floor, cap].
        if is_gpp and player.game_total:
            try:
                from app.config.constants import (
                    GAME_TOTAL_BOOST_NBA_BASELINE, GAME_TOTAL_BOOST_NBA_SCALE,
                    GAME_TOTAL_BOOST_NBA_CAP, GAME_TOTAL_BOOST_NBA_FLOOR,
                    GAME_TOTAL_BOOST_CBB_BASELINE, GAME_TOTAL_BOOST_CBB_SCALE,
                    GAME_TOTAL_BOOST_CBB_CAP, GAME_TOTAL_BOOST_CBB_FLOOR,
                )
                total = float(player.game_total)
                if sport == "cbb":
                    _baseline = GAME_TOTAL_BOOST_CBB_BASELINE
                    _scale = GAME_TOTAL_BOOST_CBB_SCALE
                    _cap = GAME_TOTAL_BOOST_CBB_CAP
                    _floor = GAME_TOTAL_BOOST_CBB_FLOOR
                else:
                    _baseline = GAME_TOTAL_BOOST_NBA_BASELINE
                    _scale = GAME_TOTAL_BOOST_NBA_SCALE
                    _cap = GAME_TOTAL_BOOST_NBA_CAP
                    _floor = GAME_TOTAL_BOOST_NBA_FLOOR
                boost_pct = (total - _baseline) * _scale
                boost_pct = max(_floor, min(_cap, boost_pct))
                base *= (1.0 + boost_pct)
            except (ValueError, TypeError):
                pass

        # ── GPP: Implied team total boost ────────────────────────────
        # Improvement #1: Per-team implied total (from Vegas O/U + spread).
        # Players on teams with higher implied scoring environments get
        # a bounded multiplier boost.
        if is_gpp and player.implied_team_total:
            from app.config.constants import (
                GPP_IMPLIED_TOTAL_BASELINE,
                GPP_IMPLIED_TOTAL_SCALE,
                GPP_IMPLIED_TOTAL_CAP,
                GPP_IMPLIED_TOTAL_FLOOR,
            )
            _impl_delta = player.implied_team_total - GPP_IMPLIED_TOTAL_BASELINE
            _impl_boost = max(
                GPP_IMPLIED_TOTAL_FLOOR,
                min(GPP_IMPLIED_TOTAL_CAP, _impl_delta * GPP_IMPLIED_TOTAL_SCALE),
            )
            base *= (1.0 + _impl_boost)

        # ── GPP: Cross-game affinity ─────────────────────────────────
        # Improvement #8: Relative game total vs slate average.
        # Players from the highest-total games on the slate get a boost,
        # while low-total games get penalized.
        if (
            is_gpp
            and player.game_total
            and hasattr(self, "_slate_avg_game_total")
            and self._slate_avg_game_total > 0
        ):
            from app.config.constants import (
                GPP_GAME_AFFINITY_POWER,
                GPP_GAME_AFFINITY_CAP,
                GPP_GAME_AFFINITY_FLOOR,
            )
            _ratio = float(player.game_total) / self._slate_avg_game_total
            _affinity = max(
                GPP_GAME_AFFINITY_FLOOR,
                min(GPP_GAME_AFFINITY_CAP, _ratio ** GPP_GAME_AFFINITY_POWER),
            )
            base *= _affinity

        # ── GPP: Secondary game stack bonus ──────────────────────────
        # Improvement #3: Players from the secondary stack game get a
        # small bonus to encourage 3+1+2 lineup structure.
        if (
            is_gpp
            and hasattr(self, "_secondary_stack_game_id")
            and self._secondary_stack_game_id
            and player.game_id == self._secondary_stack_game_id
        ):
            from app.config.constants import GPP_SECONDARY_STACK_BONUS
            base *= (1.0 + GPP_SECONDARY_STACK_BONUS)

        # ── GPP: Correlation stack teammate density bonus ────────────
        # If this player's team has 2+ high-value teammates in the
        # pool (identified in generate_lineups), boost composite score
        # so the K-Best ILP naturally pairs teammates together.
        if (
            is_gpp
            and hasattr(self, "_stackable_teams")
            and self._stackable_teams
        ):
            _team_key = (player.team_abbreviation or "").upper()
            if _team_key in self._stackable_teams:
                from app.config.constants import CORRELATION_STACK_TEAMMATE_BONUS
                base *= (1.0 + CORRELATION_STACK_TEAMMATE_BONUS)

        # ── GPP: Variance bonus (reward high-ceiling volatility) ─────
        # Improvement #6: Boom/bust variance — use boom_probability
        # (ceiling/projection ratio) instead of flat sim_std bonus.
        # Players with higher ceiling upside get bigger GPP boost.
        if is_gpp:
            from app.config.constants import (
                GPP_BOOM_VARIANCE_SCALE,
                GPP_BOOM_BASELINE,
            )
            boom_prob = player.boom_probability
            if boom_prob is None and player.projected_fp and player.projected_fp > 0:
                boom_prob = (
                    (player.ceiling_fp / player.projected_fp)
                    if player.ceiling_fp
                    else GPP_BOOM_BASELINE
                )
            if boom_prob is not None:
                variance_mult = 1.0 + GPP_BOOM_VARIANCE_SCALE * (
                    boom_prob - GPP_BOOM_BASELINE
                )
                base *= max(0.92, min(1.12, variance_mult))
        elif player.sim_std:
            # Non-GPP fallback: keep original sim_std bonus
            base *= (1.0 + min(player.sim_std, 20) * 0.005)

        # ── GPP: Salary-value efficiency bonus ─────────────────────────
        # Rewards players with high projected FP relative to salary cost.
        # Cheap value plays (e.g., 25 FP at $4K) get boosted; expensive
        # low-value plays get penalised.
        if is_gpp and player.salary and player.salary > 0 and player.projected_fp:
            from app.config.constants import (
                GPP_VALUE_BASELINE,
                GPP_VALUE_SCALE,
                GPP_VALUE_CAP,
                GPP_VALUE_FLOOR,
            )
            value_ratio = player.projected_fp / (player.salary / 1000)
            value_delta = (value_ratio - GPP_VALUE_BASELINE) * GPP_VALUE_SCALE
            base *= max(GPP_VALUE_FLOOR, min(GPP_VALUE_CAP, 1.0 + value_delta))

        # ── Opponent defense rating (continuous linear curve) ─────────
        # Linear DRtg adjustment replaces stepwise thresholds to
        # eliminate cliff effects (e.g., 114.9 → +4% vs 115.0 → +6%).
        if player.opponent_def_rating:
            try:
                from app.config.constants import (
                    DRTG_NEUTRAL_NBA, DRTG_BOOST_PER_POINT_NBA,
                    DRTG_PENALTY_PER_POINT_NBA, DRTG_BOOST_CAP_NBA,
                    DRTG_PENALTY_FLOOR_NBA, DRTG_NEUTRAL_CBB,
                    DRTG_BOOST_PER_POINT_CBB, DRTG_PENALTY_PER_POINT_CBB,
                    DRTG_BOOST_CAP_CBB, DRTG_PENALTY_FLOOR_CBB,
                )
                def_rtg = float(player.opponent_def_rating)
                if sport == "cbb":
                    _neutral = DRTG_NEUTRAL_CBB
                    _boost_slope = DRTG_BOOST_PER_POINT_CBB
                    _pen_slope = DRTG_PENALTY_PER_POINT_CBB
                    _cap = DRTG_BOOST_CAP_CBB
                    _floor = DRTG_PENALTY_FLOOR_CBB
                else:
                    _neutral = DRTG_NEUTRAL_NBA
                    _boost_slope = DRTG_BOOST_PER_POINT_NBA
                    _pen_slope = DRTG_PENALTY_PER_POINT_NBA
                    _cap = DRTG_BOOST_CAP_NBA
                    _floor = DRTG_PENALTY_FLOOR_NBA
                if def_rtg > _neutral:
                    _drtg_adj = min((def_rtg - _neutral) * _boost_slope, _cap)
                else:
                    _drtg_adj = max((def_rtg - _neutral) * _pen_slope, _floor)
                base *= (1.0 + _drtg_adj)
            except (ValueError, TypeError):
                pass

        # ── Rotation confidence penalty (continuous) ─────────────────
        # Linear interpolation replaces step function to eliminate cliff
        # effects.  At confidence=1.0 → no penalty; at 0.0 → full penalty
        # (×ROTATION_CONFIDENCE_PENALTY_LOW).
        from app.config.constants import (
            ROTATION_CONFIDENCE_PENALTY_LOW,
            ROTATION_CONFIDENCE_PENALTY_MED,
        )
        if player.rotation_confidence < 1.0:
            # Linear: penalty_low + (1 - penalty_low) * confidence
            _conf_mult = (
                ROTATION_CONFIDENCE_PENALTY_LOW
                + (1.0 - ROTATION_CONFIDENCE_PENALTY_LOW)
                * player.rotation_confidence
            )
            base *= _conf_mult

        # ── GPP: Fallback-source projection penalty ──────────────────
        # Players with fabricated projections (unmatched name rescue,
        # salary-based estimates) have no rotation data backing their
        # FP estimate.  In GPP these are high-bust-risk plays.
        if is_gpp and player.projection_source:
            _FALLBACK_PREFIXES = ("unmatched_", "salary_estimate")
            if any(
                player.projection_source.startswith(pfx)
                for pfx in _FALLBACK_PREFIXES
            ):
                base *= 0.70  # 30% score reduction

        # ── Game pace factor (continuous linear curve) ───────────────
        # Linear pace adjustment replaces stepwise thresholds.
        if player.game_pace:
            try:
                from app.config.constants import (
                    PACE_NEUTRAL_NBA, PACE_BOOST_PER_UNIT_NBA,
                    PACE_PENALTY_PER_UNIT_NBA, PACE_BOOST_CAP_NBA,
                    PACE_PENALTY_FLOOR_NBA, PACE_NEUTRAL_CBB,
                    PACE_BOOST_PER_UNIT_CBB, PACE_PENALTY_PER_UNIT_CBB,
                    PACE_BOOST_CAP_CBB, PACE_PENALTY_FLOOR_CBB,
                )
                pace = float(player.game_pace)
                if sport == "cbb":
                    _neutral = PACE_NEUTRAL_CBB
                    _boost_slope = PACE_BOOST_PER_UNIT_CBB
                    _pen_slope = PACE_PENALTY_PER_UNIT_CBB
                    _cap = PACE_BOOST_CAP_CBB
                    _floor = PACE_PENALTY_FLOOR_CBB
                else:
                    _neutral = PACE_NEUTRAL_NBA
                    _boost_slope = PACE_BOOST_PER_UNIT_NBA
                    _pen_slope = PACE_PENALTY_PER_UNIT_NBA
                    _cap = PACE_BOOST_CAP_NBA
                    _floor = PACE_PENALTY_FLOOR_NBA
                if pace > _neutral:
                    _pace_adj = min((pace - _neutral) * _boost_slope, _cap)
                else:
                    _pace_adj = max((pace - _neutral) * _pen_slope, _floor)
                base *= (1.0 + _pace_adj)
            except (ValueError, TypeError):
                pass

        # ── GPP: Ceiling-to-salary value (position-adjusted) ─────────
        # GPP rewards upside per dollar, not just raw projection.
        # Guards get a small boost due to higher assist/3PM variance.
        if is_gpp and player.salary and player.salary > 0:
            from app.config.constants import POSITION_GPP_VALUE_MULTIPLIERS
            ceiling = player.sim_p90 if player.sim_p90 else player.ceiling_fp
            ceil_per_k = ceiling / (player.salary / 1000)
            pos_mult = POSITION_GPP_VALUE_MULTIPLIERS.get(player.position, 1.0)
            if ceil_per_k >= 6.5:
                base *= 1.06 * pos_mult  # Elite ceiling value
            elif ceil_per_k >= 5.5:
                base *= 1.04 * pos_mult  # Strong ceiling value
            elif ceil_per_k >= 4.5:
                base *= 1.02 * pos_mult  # Moderate ceiling value

        # ── Gaussian noise from Monte Carlo std_dev ──────────────────
        # Instead of flat +/-N% uniform noise, draw from a Gaussian
        # centred on the composite base with sigma proportional to
        # the player's MC simulation standard deviation.
        #
        # Scaling: sim_std is in raw FP units, but base has multiplicative
        # factors applied (ceiling blend, game env, ownership leverage).
        # Preserve the coefficient of variation:
        #   scaled_sigma = sim_std * (base / projected_fp)
        #
        # Fallback (no sim_std available): 10% of base as default sigma.
        if player.projected_fp and player.projected_fp > 0 and player.sim_std:
            _sigma = player.sim_std * (base / player.projected_fp)
        else:
            _sigma = base * 0.10

        # ── Dynamic variance scaling: widen sigma for cheap/volatile players
        # Only increases variance (more spiky), does NOT shift the mean.
        from app.config.constants import (
            VARIANCE_SCALE_LOW_SALARY_THRESHOLD,
            VARIANCE_SCALE_LOW_SALARY_MULT,
            VARIANCE_SCALE_LOW_CONFIDENCE_MULT,
            VARIANCE_SCALE_MAX_COMBINED,
        )
        _var_scale = 1.0
        if player.salary and player.salary < VARIANCE_SCALE_LOW_SALARY_THRESHOLD:
            _var_scale *= VARIANCE_SCALE_LOW_SALARY_MULT
        if player.rotation_confidence < 0.75:
            _var_scale *= VARIANCE_SCALE_LOW_CONFIDENCE_MULT
        _sigma *= min(_var_scale, VARIANCE_SCALE_MAX_COMBINED)

        base = rng.gauss(base, _sigma)
        # Floor at zero -- negative scores break ILP maximisation
        if base < 0:
            base = 0.01

        # ── Composite score clamp ──────────────────────────────────────
        # Prevent extreme multiplier cascading: after all multiplicative
        # factors (ownership, game total, variance, defense, pace, noise),
        # clamp so composite never exceeds MAX_MULT × raw projected_fp.
        from app.config.constants import COMPOSITE_SCORE_MAX_MULTIPLIER
        if player.projected_fp and player.projected_fp > 0:
            base = min(base, player.projected_fp * COMPOSITE_SCORE_MAX_MULTIPLIER)

        # ── Apply AI strategy adjustments (Agent 2 — Game Theory) ─────
        if self._strategy_adjustments is not None:
            try:
                modifiers = self._strategy_adjustments.player_score_modifiers
                if player.player_id in modifiers:
                    base *= modifiers[player.player_id]
            except Exception:
                pass  # Graceful degradation

        # ── Apply tournament/backtest calibration adjustments ─────────
        if self.calibration_service:
            try:
                cals = self.calibration_service

                # NOTE: Position bias is NOT applied here — it's already
                # applied in RotationEngine.get_baseline_projection() to the
                # minutes estimate, which flows through to projected FP.
                # Applying it again here would double-dip the adjustment.

                # Salary tier preference — position-aware thresholds
                # so a $7200 C is "high" (compressed range) while
                # a $7200 PG is "mid" (broader range).
                from app.config.constants import (
                    POSITION_SALARY_TIERS,
                    POSITION_SALARY_TIERS_DEFAULT,
                )
                _pos_tiers = POSITION_SALARY_TIERS.get(
                    player.position, POSITION_SALARY_TIERS_DEFAULT,
                )
                if player.salary >= _pos_tiers["high"]:
                    tier_adj = cals.get_salary_tier_adjustment("high")
                elif player.salary >= _pos_tiers["mid"]:
                    tier_adj = cals.get_salary_tier_adjustment("mid")
                else:
                    tier_adj = cals.get_salary_tier_adjustment("value")
                if tier_adj != 1.0:
                    base *= tier_adj

                # Ownership threshold (GPP: tighten/loosen chalk fading)
                if is_gpp and player.estimated_ownership is not None:
                    own_cal = cals.get_ownership_threshold_adj()
                    if own_cal != 1.0 and player.estimated_ownership >= 20:
                        base *= own_cal

                # Game context: high-total game boost calibration
                if player.game_total:
                    try:
                        total = float(player.game_total)
                        if total >= 230:
                            game_cal = cals.get_game_context_multiplier("high_total")
                            if game_cal != 1.0:
                                base *= game_cal
                    except (ValueError, TypeError):
                        pass
            except Exception:
                pass  # Graceful degradation

        # ── Fade / leverage integration ─────────────────────────────────
        # FadeService identifies fade candidates (high ownership + low ceiling)
        # and leverage plays (low ownership + high ceiling) with scored
        # confidence.  Apply as a targeted multiplier on top of the general
        # ownership leverage curve.
        if is_gpp and self._fade_leverage_scores:
            fl_data = self._fade_leverage_scores.get(player.player_id)
            if fl_data:
                from app.config.constants import FADE_PENALTY_WEIGHT, LEVERAGE_BOOST_WEIGHT
                if "fade_score" in fl_data:
                    base *= (1.0 - fl_data["fade_score"] * FADE_PENALTY_WEIGHT)
                elif "leverage_score" in fl_data:
                    base *= (1.0 + fl_data["leverage_score"] * LEVERAGE_BOOST_WEIGHT)

        # ── Exposure penalty (contest-type-aware) ─────────────────────
        # Capped so scores never drop below 20% of the pre-penalty value.
        k = exposure.get(player.player_id, 0)
        if k > 0:
            if is_gpp:
                # GPP: softer penalty — allow leverage stacking across builds
                penalty = min(k * 0.015, 0.80)
            else:
                # Cash: stronger penalty — force diversification
                penalty = min(k * 0.07, 0.80)
            base *= (1.0 - penalty)

        return max(base, 0.0)

    # ------------------------------------------------------------------
    # Overgenerate-then-filter helpers
    # ------------------------------------------------------------------

    def _score_lineup(
        self,
        lineup: OptimizedLineup,
        pool: List[PlayerPoolEntry],
        strategy: str,
        contest_type: str,
        salary_cap: int,
    ) -> float:
        """Score a completed lineup for post-generation ranking.

        Returns a single float where higher = better.  Blends:
          - Total projected FP (always important)
          - Total ceiling FP (GPP) or total floor FP (cash)
          - Salary efficiency (penalize wasted cap)
          - Ownership differentiation (GPP: reward low-ownership builds)

        This is intentionally separate from ``_compute_composite_score``,
        which operates at the per-player level during greedy construction
        and includes noise, exposure penalties, and other construction-time
        concerns that should *not* affect post-hoc lineup ranking.
        """
        is_gpp = contest_type in ("gpp", "single_entry")

        # ── Component 1: Projection-based quality ────────────────────
        proj = lineup.total_projected_fp

        # ── Component 2: Strategy-aligned upside or safety ───────────
        if strategy == "pure_max":
            secondary = proj  # 100% projection — no ceiling/floor blend
        elif strategy in ("ceiling", "contrarian") or (strategy == "max_projection" and is_gpp):
            secondary = lineup.total_ceiling_fp
        elif strategy == "balanced":
            secondary = (lineup.total_ceiling_fp + lineup.total_floor_fp) / 2.0
        else:
            secondary = lineup.total_floor_fp

        # ── Component 3: Salary efficiency ───────────────────────────
        # Reward using ≥95% of cap.  Penalize wasting budget.
        salary_usage = lineup.total_salary / salary_cap if salary_cap > 0 else 1.0
        if salary_usage >= 0.95:
            salary_factor = 1.0
        elif salary_usage >= 0.90:
            salary_factor = 0.98
        else:
            salary_factor = 0.95

        # ── Component 4: Ownership leverage (GPP only) ───────────────
        # Continuous power-law at lineup level (dampened alpha) plus a
        # bonus for lineups with multiple low-owned upside players.
        ownership_factor = 1.0
        if is_gpp:
            own_lookup: Dict[int, float] = {}
            ceil_lookup: Dict[int, float] = {}
            proj_lookup: Dict[int, float] = {}
            for p in pool:
                if p.estimated_ownership is not None:
                    own_lookup[p.player_id] = p.estimated_ownership
                ceil_lookup[p.player_id] = p.ceiling_fp
                proj_lookup[p.player_id] = p.projected_fp

            if own_lookup:
                player_ownerships = [
                    own_lookup.get(p.player_id, 10.0)
                    for p in lineup.players
                ]
                avg_ownership = (
                    sum(player_ownerships)
                    / max(len(player_ownerships), 1)
                )

                # Continuous power-law: baseline=12% → neutral,
                # lower → boost, higher → fade.  Light alpha=0.15
                # since player-level already applies value-aware leverage.
                _lineup_own_alpha = 0.15
                _lineup_own_baseline = 12.0
                if avg_ownership > 0:
                    ownership_factor = 1.0 / (
                        (avg_ownership / _lineup_own_baseline)
                        ** _lineup_own_alpha
                    )
                    ownership_factor = max(0.92, min(1.08, ownership_factor))

                # Low-owned upside bonus: reward lineups with 2+ sub-5%
                # owned players who have genuine upside (ceiling > 1.3×).
                _low_own_upside_count = 0
                for p in lineup.players:
                    pid = p.player_id
                    _own = own_lookup.get(pid, 10.0)
                    _ceil = ceil_lookup.get(pid, 0.0)
                    _proj = proj_lookup.get(pid, 0.0)
                    if _own < 5.0 and _proj > 0 and _ceil > _proj * 1.3:
                        _low_own_upside_count += 1
                if _low_own_upside_count >= 2:
                    ownership_factor *= 1.05

        # ── Component 5: Game stacking quality (GPP only) ────────────
        stacking_factor = 1.0
        if is_gpp:
            # Group players by game_id
            game_counts: Dict[str, List[str]] = {}
            for p in lineup.players:
                # Look up game_id from pool
                pool_entry = next(
                    (pp for pp in pool if pp.player_id == p.player_id),
                    None,
                )
                if pool_entry and pool_entry.game_id:
                    gid = pool_entry.game_id
                    if gid not in game_counts:
                        game_counts[gid] = []
                    game_counts[gid].append(p.team_abbreviation)

            # Use learned stacking weights from tournament analysis
            # when available, otherwise fall back to defaults.
            if self.calibration_service:
                _stack_3man = self.calibration_service.get_stacking_weight("3man_weight")
                _stack_2man = self.calibration_service.get_stacking_weight("2man_weight")
                _bringback = self.calibration_service.get_stacking_weight("bringback_weight")
            else:
                _stack_3man = 1.0
                _stack_2man = 1.0
                _bringback = 1.0
            # Fall back to defaults when calibrations return 1.0 (unset)
            stack_3man = _stack_3man if _stack_3man != 1.0 else 1.08
            stack_2man = _stack_2man if _stack_2man != 1.0 else 1.03
            bringback = _bringback if _bringback != 1.0 else 1.05

            # Check for game stacks (2+ players in same game)
            for gid, teams in game_counts.items():
                count = len(teams)
                if count >= 3:
                    stacking_factor *= stack_3man  # Strong 3+ player game stack
                    # Bring-back bonus: tier by opposing-team player count.
                    # 4-2 / 3-2 stacks have measurably better tournament EV
                    # than 4-1 / 3-1 because the run-it-back captures the
                    # losing-side garbage-time scoring; reward accordingly.
                    from collections import Counter
                    team_counts = Counter(teams)
                    primary_count = team_counts.most_common(1)[0][1]
                    bring_back = count - primary_count
                    if bring_back >= 2:
                        # Wider bring-back — apply base bringback then a
                        # small extra (3%) for the second opposing player.
                        stacking_factor *= bringback * 1.03
                    elif bring_back == 1:
                        stacking_factor *= bringback  # standard 1-player bring-back
                elif count == 2:
                    stacking_factor *= stack_2man  # Modest 2-player stack

        # ── Component 6: Correlation quality bonus (GPP only) ───────
        correlation_factor = 1.0
        if is_gpp and self._cached_correlations and game_counts:
            # Compute average pairwise correlation among same-game players
            corr_values = []
            pool_lookup = {p.player_id: p for p in pool}
            for gid, _teams in game_counts.items():
                game_player_ids = [
                    p.player_id for p in lineup.players
                    if pool_lookup.get(p.player_id)
                    and pool_lookup[p.player_id].game_id == gid
                ]
                if len(game_player_ids) >= 2:
                    for i in range(len(game_player_ids)):
                        for j in range(i + 1, len(game_player_ids)):
                            pair_key = (
                                min(game_player_ids[i], game_player_ids[j]),
                                max(game_player_ids[i], game_player_ids[j]),
                            )
                            corr = self._cached_correlations.get(pair_key)
                            if corr is not None:
                                corr_values.append(corr)

            if corr_values:
                avg_corr = sum(corr_values) / len(corr_values)
                if avg_corr > 0.4:
                    correlation_factor = 1.06
                elif avg_corr > 0.3:
                    correlation_factor = 1.04
                elif avg_corr > 0.2:
                    correlation_factor = 1.02

        # ── Component 7: Salary floor penalty ──────────────────────
        salary_floor_factor = 1.0
        if salary_usage < 0.95:
            salary_floor_factor = 0.92  # Strongly penalize wasted salary

        # ── Component 8: Correlation stack bonus (GPP only) ─────────
        # Additive FP bonus for lineups with 2-3 high-value same-team
        # stacks.  This makes the Portfolio ILP prefer correlated
        # lineups that pair injury-value teammates.
        corr_stack_bonus = 0.0
        if is_gpp:
            corr_stack_bonus = self._calculate_lineup_correlation_bonus(
                lineup.players, pool
            )

        # ── Component 9: Ceiling-asymmetry bonus (GPP only) ─────────
        # The 0.5/0.5 proj+ceiling blend treats two lineups with equal
        # (proj+ceiling)/2 as identical.  In tournaments, the lineup with
        # the *higher ceiling-to-projection ratio* has meaningfully more
        # upside tail and is worth more $/entry.  Reward that asymmetry
        # with a small multiplicative bonus capped at +5%.
        ceiling_asym_factor = 1.0
        if is_gpp and proj > 0 and lineup.total_ceiling_fp > 0:
            ceil_ratio = lineup.total_ceiling_fp / proj
            # NBA classic GPP lineups typically run ratio 1.15–1.45.
            # Tier the bonus so only genuinely upside-asymmetric builds
            # get rewarded, not garden-variety high-projection lineups.
            if ceil_ratio >= 1.35:
                ceiling_asym_factor = 1.05
            elif ceil_ratio >= 1.30:
                ceiling_asym_factor = 1.03
            elif ceil_ratio >= 1.25:
                ceiling_asym_factor = 1.015

        # ── Combine ──────────────────────────────────────────────────
        if is_gpp:
            raw = 0.50 * proj + 0.50 * secondary
        else:
            raw = 0.60 * proj + 0.40 * secondary

        # Add correlation bonus BEFORE multipliers so it compounds
        # with stacking/correlation factors.
        raw += corr_stack_bonus

        return (
            raw
            * salary_factor
            * ownership_factor
            * stacking_factor
            * correlation_factor
            * ceiling_asym_factor
            * salary_floor_factor
        )

    # ------------------------------------------------------------------
    # Correlation stack bonus
    # ------------------------------------------------------------------

    @staticmethod
    def _calculate_lineup_correlation_bonus(
        lineup_players: list,
        pool: List["PlayerPoolEntry"],
    ) -> float:
        """Calculate dynamic FP bonus for same-team correlation stacks.

        Groups lineup players by team.  For each team with 2-3 players
        whose average FP/$1K exceeds ``CORRELATION_STACK_MIN_AVG_VALUE``
        (4.6x), awards a scaled bonus:

            bonus = base (1.5) + 0.5 FP per 0.1x above threshold

        Examples (threshold=4.6x):
          - 4.6x avg → 1.5 FP  (just eligible)
          - 5.0x avg → 1.5 + (4 * 0.5) = 3.5 FP
          - 5.5x avg → 1.5 + (9 * 0.5) = 6.0 FP

        Stacks of 4+ players are NOT rewarded to prevent over-concentration.

        Parameters
        ----------
        lineup_players : list
            Players in the lineup (OptimizedLineup.players or similar).
        pool : list of PlayerPoolEntry
            Full player pool (used to look up salary / projected_fp).

        Returns
        -------
        float
            Total bonus FP to add to the lineup score (0.0 if none).
        """
        from app.config.constants import (
            CORRELATION_STACK_MIN_AVG_VALUE,
            CORRELATION_STACK_MAX_PLAYERS,
            CORRELATION_STACK_BASE_BONUS_FP,
            CORRELATION_STACK_BONUS_PER_TICK,
            CORRELATION_STACK_BONUS_CAP_FP,
        )

        pool_lookup: Dict[int, "PlayerPoolEntry"] = {
            p.player_id: p for p in pool
        }

        # Group lineup players by team
        team_players: Dict[str, list] = {}
        for p in lineup_players:
            pid = getattr(p, "player_id", None)
            if pid is None:
                continue
            pe = pool_lookup.get(pid)
            if pe is None:
                continue
            team = (pe.team_abbreviation or "").upper()
            if team:
                team_players.setdefault(team, []).append(pe)

        total_bonus = 0.0
        for team, players in team_players.items():
            n = len(players)
            # Only reward 2-3 player stacks; 4+ is over-concentration
            if n < 2 or n > CORRELATION_STACK_MAX_PLAYERS:
                continue
            # Compute average value ratio for this stack
            value_ratios = []
            for pe in players:
                if pe.salary and pe.salary > 0 and pe.projected_fp:
                    value_ratios.append(pe.projected_fp / (pe.salary / 1000))
            if not value_ratios:
                continue
            avg_value = sum(value_ratios) / len(value_ratios)
            if avg_value >= CORRELATION_STACK_MIN_AVG_VALUE:
                # Dynamic bonus: base + 0.5 FP per 0.1x above threshold
                ticks = (avg_value - CORRELATION_STACK_MIN_AVG_VALUE) / 0.1
                bonus = CORRELATION_STACK_BASE_BONUS_FP + ticks * CORRELATION_STACK_BONUS_PER_TICK
                total_bonus += min(bonus, CORRELATION_STACK_BONUS_CAP_FP)

        return total_bonus

    # ------------------------------------------------------------------
    # Lineup quality assessment
    # ------------------------------------------------------------------

    @staticmethod
    def _assess_lineup_quality(
        lineup: "OptimizedLineup",
        salary_cap: int,
        pool: Optional[List["PlayerPoolEntry"]] = None,
        best_score: Optional[float] = None,
        lineup_score: Optional[float] = None,
    ) -> Tuple[float, str, List[str]]:
        """Compute a normalised quality score and letter grade for a lineup.

        The quality score (0–100) blends several cheap-to-compute signals:

        1. **Salary efficiency** (25%): how much of the cap is used.
        2. **Projection quality** (35%): total projected FP relative to
           a theoretical maximum (sum of top-N FP from the pool).
        3. **Team diversity** (15%): number of distinct teams represented.
        4. **Floor safety** (15%): floor-to-projection ratio — a high
           ratio means the lineup is less likely to bust.
        5. **Relative ranking** (10%): when ``best_score`` and ``lineup_score``
           are provided, ratio of this lineup's holistic score to the best.

        Returns ``(score_0_100, grade_letter, quality_warnings)``.
        """
        from app.config.constants import (
            LINEUP_QUALITY_MIN_SALARY_PCT,
            LINEUP_QUALITY_MIN_TEAMS,
        )

        quality_warnings: List[str] = []
        components: Dict[str, float] = {}

        # ── 1. Salary efficiency (0–1) ─────────────────────────────
        salary_pct = lineup.total_salary / salary_cap if salary_cap > 0 else 1.0
        if salary_pct >= 0.98:
            sal_score = 1.0
        elif salary_pct >= 0.95:
            sal_score = 0.85
        elif salary_pct >= 0.90:
            sal_score = 0.65
        else:
            sal_score = max(0.0, salary_pct / 0.90 * 0.50)
        components["salary"] = sal_score

        if salary_pct < LINEUP_QUALITY_MIN_SALARY_PCT:
            quality_warnings.append(
                f"Low salary usage ({salary_pct:.0%} of cap)"
            )

        # ── 2. Projection quality (0–1) ────────────────────────────
        # Use value-per-dollar ranking to approximate the salary-constrained
        # theoretical max, which is much more realistic than simply summing
        # the top-N projected FP regardless of salary feasibility.
        if pool and lineup.players:
            n_slots = len(lineup.players)
            # Sort by FP-per-dollar (value) so the "best" approximates what
            # an ILP solver could achieve under the salary cap.
            valued = sorted(
                pool,
                key=lambda p: p.projected_fp / max(p.salary, 1),
                reverse=True,
            )
            budget = salary_cap
            theoretical_fp = 0.0
            picked = 0
            for p in valued:
                if picked >= n_slots:
                    break
                if p.salary <= budget:
                    theoretical_fp += p.projected_fp
                    budget -= p.salary
                    picked += 1
            theoretical_max = theoretical_fp if theoretical_fp > 0 else 1.0
            proj_ratio = (
                lineup.total_projected_fp / theoretical_max
                if theoretical_max > 0 else 0.0
            )
            # Clamp and scale: 0.60 ratio → 0, 1.0 ratio → 1.0
            proj_score = max(0.0, min(1.0, (proj_ratio - 0.60) / 0.40))
        else:
            proj_score = 0.5  # No pool → neutral
        components["projection"] = proj_score

        # ── 3. Team diversity (0–1) ────────────────────────────────
        unique_teams = len(set(p.team_abbreviation for p in lineup.players))
        n_slots = len(lineup.players) if lineup.players else 8
        # Perfect = ≥4 teams; minimum = 2
        diversity_score = min(1.0, max(0.0, (unique_teams - 1) / 3.0))
        components["diversity"] = diversity_score

        if unique_teams < LINEUP_QUALITY_MIN_TEAMS:
            quality_warnings.append(
                f"Only {unique_teams} team(s) represented"
            )

        # ── 4. Floor safety (0–1) ──────────────────────────────────
        if lineup.total_projected_fp > 0 and lineup.total_floor_fp > 0:
            floor_ratio = lineup.total_floor_fp / lineup.total_projected_fp
            # 0.70 ratio → 0.0, 0.85 ratio → 1.0
            floor_score = max(0.0, min(1.0, (floor_ratio - 0.70) / 0.15))
        else:
            floor_score = 0.0
        components["floor_safety"] = floor_score

        # ── 5. Relative ranking (0–1) ──────────────────────────────
        if best_score and lineup_score and best_score > 0:
            relative = lineup_score / best_score
            relative_score = max(0.0, min(1.0, relative))
        else:
            relative_score = 0.75  # Unknown → above-average default
        components["relative"] = relative_score

        # ── Weighted blend ─────────────────────────────────────────
        weights = {
            "salary": 0.15,       # Reduced from 0.25 — less penalty for cheap value plays
            "projection": 0.40,   # Increased from 0.35 — prioritise projection quality
            "diversity": 0.15,
            "floor_safety": 0.15,
            "relative": 0.15,     # Increased from 0.10 — reward relative score strength
        }
        raw_score = sum(
            components[k] * weights[k] for k in weights
        )
        score_100 = round(raw_score * 100, 1)

        # ── Letter grade ───────────────────────────────────────────
        if score_100 >= 90:
            grade = "A+"
        elif score_100 >= 80:
            grade = "A"
        elif score_100 >= 70:
            grade = "B+"
        elif score_100 >= 60:
            grade = "B"
        elif score_100 >= 50:
            grade = "C+"
        elif score_100 >= 40:
            grade = "C"
        else:
            grade = "D"

        return score_100, grade, quality_warnings

    @staticmethod
    def _passes_quality_gate(
        lineup: "OptimizedLineup",
        salary_cap: int,
        expected_players: int = 0,
        min_salary_pct: Optional[float] = None,
        min_projected_fp: Optional[float] = None,
    ) -> bool:
        """Fast structural check — does the lineup meet minimum viability?

        This is a quick pass/fail used during overgeneration to discard
        obviously bad candidates *before* the more expensive scoring step.

        Args:
            expected_players: Expected number of roster slots (e.g. 8 for DK
                classic, 6 for showdown).  0 = skip count check.
            min_salary_pct: Override for salary floor.  When None, uses the
                global ``LINEUP_QUALITY_MIN_SALARY_PCT`` constant.
            min_projected_fp: Minimum total projected FP for the lineup.
                When provided, lineups below this floor are rejected.
                Typically set to ``baseline_optimal * MIN_PROJECTION_PCT``.
        """
        from app.config.constants import (
            LINEUP_QUALITY_MIN_SALARY_PCT,
            LINEUP_QUALITY_MIN_TEAMS,
        )

        # All slots filled?
        if not lineup.players:
            return False

        # Correct player count (if specified)
        if expected_players > 0 and len(lineup.players) < expected_players:
            return False

        # Salary floor
        floor_pct = min_salary_pct if min_salary_pct is not None else LINEUP_QUALITY_MIN_SALARY_PCT
        salary_pct = lineup.total_salary / salary_cap if salary_cap > 0 else 0
        if salary_pct < floor_pct:
            return False

        # Team diversity
        unique_teams = len(set(p.team_abbreviation for p in lineup.players))
        if unique_teams < LINEUP_QUALITY_MIN_TEAMS:
            return False

        # Projected FP floor (catches degenerate low-projection lineups)
        if min_projected_fp is not None and min_projected_fp > 0:
            if lineup.total_projected_fp < min_projected_fp:
                return False

        return True

    @staticmethod
    def _lineup_game_vector(lineup: "OptimizedLineup") -> Dict[str, int]:
        """Build a game-stack vector: game_key → number of players.

        Two lineups with similar game vectors are highly correlated in
        outcomes, even if they share few individual players, because they
        are exposed to the same game environments.
        """
        vec: Dict[str, int] = {}
        for p in lineup.players:
            # Use (team_abbreviation) as a rough game-environment proxy.
            # Players on the same team are in the same game.
            key = p.team_abbreviation
            vec[key] = vec.get(key, 0) + 1
        return vec

    @staticmethod
    def _game_vector_correlation(
        vec_a: Dict[str, int], vec_b: Dict[str, int],
    ) -> float:
        """Compute cosine similarity between two game-stack vectors.

        Returns a value in [0, 1] where 1 = identical game exposure.
        """
        all_keys = set(vec_a) | set(vec_b)
        if not all_keys:
            return 0.0
        dot = sum(vec_a.get(k, 0) * vec_b.get(k, 0) for k in all_keys)
        mag_a = sum(v * v for v in vec_a.values()) ** 0.5
        mag_b = sum(v * v for v in vec_b.values()) ** 0.5
        if mag_a == 0 or mag_b == 0:
            return 0.0
        return dot / (mag_a * mag_b)

    @staticmethod
    def _select_best_diverse(
        candidates: List[Tuple["OptimizedLineup", float]],
        num_to_select: int,
        max_overlap: int,
    ) -> List["OptimizedLineup"]:
        """Select the best N lineups from candidates, enforcing diversity.

        Uses a greedy approach with two diversity mechanisms:

        1. **Hard constraint**: player overlap — a lineup is rejected if it
           shares more than ``max_overlap`` players with any selected lineup.
        2. **Soft penalty**: game-stack correlation — a lineup's effective
           score is reduced when its game-exposure vector is too similar to
           already-selected lineups.  This prevents the portfolio from
           concentrating on one game environment even when players differ.

        The soft penalty is applied by adjusting scores and re-sorting the
        remaining candidates after each selection, so higher-quality diverse
        lineups can leapfrog lower-quality correlated ones.

        Returns:
            List of selected ``OptimizedLineup`` objects, ordered by score.
        """
        from app.config.constants import (
            PORTFOLIO_MAX_GAME_CORRELATION,
            PORTFOLIO_CORRELATION_PENALTY,
        )

        # Pre-compute game vectors for all candidates
        cand_data = []
        for lineup, score in candidates:
            cand_data.append({
                "lineup": lineup,
                "base_score": score,
                "ids": {p.player_id for p in lineup.players},
                "game_vec": LineupOptimizerService._lineup_game_vector(lineup),
                "used": False,
            })

        selected: List[OptimizedLineup] = []
        selected_id_sets: List[Set[int]] = []
        selected_game_vecs: List[Dict[str, int]] = []

        for _round in range(num_to_select):
            best_idx = -1
            best_adj_score = -1e18

            for i, cd in enumerate(cand_data):
                if cd["used"]:
                    continue

                # Hard constraint: player overlap
                violates = False
                for prev_ids in selected_id_sets:
                    if len(cd["ids"] & prev_ids) > max_overlap:
                        violates = True
                        break
                if violates:
                    continue

                # Soft penalty: game-stack correlation
                adj_score = cd["base_score"]
                if selected_game_vecs:
                    max_sim = max(
                        LineupOptimizerService._game_vector_correlation(
                            cd["game_vec"], sv
                        )
                        for sv in selected_game_vecs
                    )
                    if max_sim > PORTFOLIO_MAX_GAME_CORRELATION:
                        excess = (max_sim - PORTFOLIO_MAX_GAME_CORRELATION) / (
                            1.0 - PORTFOLIO_MAX_GAME_CORRELATION + 1e-9
                        )
                        penalty = 1.0 - PORTFOLIO_CORRELATION_PENALTY * min(excess, 1.0)
                        adj_score *= penalty

                if adj_score > best_adj_score:
                    best_adj_score = adj_score
                    best_idx = i

            if best_idx < 0:
                break  # No more valid candidates

            cd = cand_data[best_idx]
            cd["used"] = True
            selected.append(cd["lineup"])
            selected_id_sets.append(cd["ids"])
            selected_game_vecs.append(cd["game_vec"])

        return selected

    @staticmethod
    def _portfolio_optimize(
        candidates: List[Tuple["OptimizedLineup", float]],
        num_to_select: int,
        max_overlap: int = 6,
        player_min_appearances: Optional[Dict[int, int]] = None,
        elite_core_pids: Optional[set] = None,
    ) -> Optional[List["OptimizedLineup"]]:
        """Jointly select the best diverse portfolio of lineups using ILP.

        Uses PuLP to solve a binary ILP that maximizes total portfolio
        score subject to cardinality, pairwise diversity, and minimum
        player exposure constraints.

        Parameters
        ----------
        candidates : list of (OptimizedLineup, score) tuples
        num_to_select : int
            Exact number of lineups to include in the portfolio.
        max_overlap : int
            Max players shared between any two selected lineups (hard constraint).

        Returns
        -------
        list of OptimizedLineup or None
            Optimal portfolio, or None if solver fails / infeasible.
        """
        from app.config.constants import (
            PORTFOLIO_ILP_SOLVER_TIMEOUT,
            PORTFOLIO_ILP_DIVERSITY_PENALTY,
            PORTFOLIO_ILP_MIN_EXPO_PENALTY,
        )

        if not _PULP_AVAILABLE or pulp is None:
            return None

        # ── Step 0: Discard low-salary candidates ─────────────────────
        # Safety net: filter out any lineup that slipped through Phase 2
        # without meeting the minimum salary floor.
        from app.config.constants import MIN_SALARY_FLOOR
        _pre_sal = len(candidates)
        candidates = [
            (lu, sc) for lu, sc in candidates
            if lu.total_salary >= MIN_SALARY_FLOOR
        ]
        _sal_dropped = _pre_sal - len(candidates)
        if _sal_dropped > 0:
            logger.info(
                f"[PortfolioILP] Dropped {_sal_dropped} candidates below "
                f"${MIN_SALARY_FLOOR:,} salary floor"
            )

        n_cand = len(candidates)
        if n_cand < num_to_select:
            return None

        # ── Step 1: Fast Set Pre-computation ──────────────────────────
        # Convert all candidate lineups into frozenset[player_id] for
        # O(1) intersection lookups.  Done BEFORE any PuLP objects.
        lineup_ids: List[Set[int]] = []
        all_players: Set[int] = set()
        for lu, _sc in candidates:
            pids = {p.player_id for p in lu.players}
            lineup_ids.append(pids)
            all_players.update(pids)

        _ec = elite_core_pids or set()

        # ── Step 2: Build the Sparse Conflict Graph ───────────────
        # Scan all N*(N-1)/2 pairs but ONLY store edges that exceed
        # the soft-penalty overlap threshold (ov > 4).  Pairs with
        # trivial overlap (≤4 shared non-elite players) need zero
        # ILP constraints and are discarded immediately.
        #
        # Elite Core exemption: shared elite-core players are NOT
        # counted toward the overlap total, so e.g. Jokic in both
        # lineups doesn't inflate the conflict score.
        #
        # Memory savings: for 500 candidates, raw combos = 124,750.
        # Typical conflict graph edges ≈ 50-200 (99.8% pruned).
        _SOFT_OV_THRESHOLD = 4
        _n_raw_combos = n_cand * (n_cand - 1) // 2

        hard_conflicts: List[Tuple[int, int]] = []      # ov > max_overlap
        soft_conflicts: List[Tuple[int, int, int]] = []  # 4 < ov <= max_overlap
        for i in range(n_cand):
            for j in range(i + 1, n_cand):
                _shared = lineup_ids[i] & lineup_ids[j]
                if _ec:
                    _shared = _shared - _ec
                ov = len(_shared)
                if ov > max_overlap:
                    hard_conflicts.append((i, j))
                elif ov > _SOFT_OV_THRESHOLD:
                    soft_conflicts.append((i, j, ov))

        _n_graph_edges = len(hard_conflicts) + len(soft_conflicts)
        _pct_pruned = (1.0 - _n_graph_edges / max(1, _n_raw_combos)) * 100
        logger.warning(
            f"[PortfolioILP] Conflict graph: {_n_raw_combos:,} raw combos → "
            f"{_n_graph_edges} edges ({len(hard_conflicts)} hard, "
            f"{len(soft_conflicts)} soft) — "
            f"{_pct_pruned:.1f}% pruned"
        )

        try:
            base_scores = [sc for _lu, sc in candidates]
            score_range = max(base_scores) - min(base_scores) if len(base_scores) > 1 else 1.0
            score_range = max(score_range, 0.01)

            # Pre-build player → lineup membership index for min-exposure
            _pid_to_lineup_indices: Dict[int, List[int]] = {}
            if player_min_appearances:
                for i, pids in enumerate(lineup_ids):
                    for pid in pids:
                        if pid in player_min_appearances:
                            _pid_to_lineup_indices.setdefault(pid, []).append(i)

            # ── Retry loop: relax max_overlap on Infeasible ───────
            _effective_max_ov = max_overlap
            _MAX_RELAX_ATTEMPTS = 3
            for _relax_attempt in range(_MAX_RELAX_ATTEMPTS):
                # On retry with relaxed max_overlap, some former hard
                # conflicts become soft — recompute classification.
                if _relax_attempt > 0:
                    _new_hard = []
                    _new_soft = list(soft_conflicts)
                    for i, j in hard_conflicts:
                        _shared = lineup_ids[i] & lineup_ids[j]
                        if _ec:
                            _shared = _shared - _ec
                        ov = len(_shared)
                        if ov > _effective_max_ov:
                            _new_hard.append((i, j))
                        elif ov > _SOFT_OV_THRESHOLD:
                            _new_soft.append((i, j, ov))
                    hard_conflicts = _new_hard
                    soft_conflicts = _new_soft

                logger.warning(
                    f"[PortfolioILP] Building (attempt {_relax_attempt + 1}): "
                    f"{n_cand} candidates, select {num_to_select}, "
                    f"max_overlap={_effective_max_ov}, "
                    f"{len(hard_conflicts)} hard + {len(soft_conflicts)} soft "
                    f"constraints (from {_n_raw_combos:,} combos), "
                    f"timeout={PORTFOLIO_ILP_SOLVER_TIMEOUT}s"
                )

                # ── Step 3: Build ILP ─────────────────────────────
                prob = pulp.LpProblem("PortfolioSelection", pulp.LpMaximize)

                # Binary decision variables: y[i] = 1 if lineup i is selected
                y = [
                    pulp.LpVariable(f"y_{i}", cat=pulp.LpBinary)
                    for i in range(n_cand)
                ]

                # Hard conflict constraints: mutually exclusive pairs
                for i, j in hard_conflicts:
                    prob += y[i] + y[j] <= 1, f"hard_overlap_{i}_{j}"

                # Soft diversity penalties via auxiliary z-variables.
                # z[i,j] = 1 iff both y[i]=1 AND y[j]=1.  Penalize
                # proportional to overlap count in the objective.
                z_vars: Dict[Tuple[int, int], Any] = {}
                for i, j, ov in soft_conflicts:
                    z_var = pulp.LpVariable(
                        f"z_{i}_{j}", cat=pulp.LpBinary
                    )
                    z_vars[(i, j)] = z_var
                    prob += z_var <= y[i], f"z_le_yi_{i}_{j}"
                    prob += z_var <= y[j], f"z_le_yj_{i}_{j}"
                    prob += z_var >= y[i] + y[j] - 1, f"z_ge_sum_{i}_{j}"

                # Objective: maximize total score - diversity penalty
                obj = pulp.lpSum(y[i] * base_scores[i] for i in range(n_cand))
                _soft_ov_lookup = {(i, j): ov for i, j, ov in soft_conflicts}
                for (i, j), z_var in z_vars.items():
                    ov = _soft_ov_lookup[(i, j)]
                    penalty = PORTFOLIO_ILP_DIVERSITY_PENALTY * score_range * ov
                    obj -= z_var * penalty

                # Constraint: select exactly num_to_select lineups
                prob += (
                    pulp.lpSum(y[i] for i in range(n_cand)) == num_to_select,
                    "Cardinality",
                )

                # ── Soft min-exposure constraints ─────────────────
                _min_expo_applied = 0
                _shortfall_vars: Dict[int, Any] = {}
                if player_min_appearances:
                    for pid, min_count in player_min_appearances.items():
                        lu_indices = _pid_to_lineup_indices.get(pid, [])
                        if not lu_indices:
                            logger.info(
                                f"[PortfolioILP] Min exposure for pid={pid} "
                                f"(target={min_count}) impossible — "
                                f"player in 0 candidates"
                            )
                            continue
                        effective_min = min(min_count, len(lu_indices))
                        if effective_min > 0:
                            shortfall_p = pulp.LpVariable(
                                f"shortfall_{pid}",
                                lowBound=0,
                                cat=pulp.LpContinuous,
                            )
                            _shortfall_vars[pid] = shortfall_p
                            prob += (
                                pulp.lpSum(y[i] for i in lu_indices) + shortfall_p
                                >= effective_min,
                                f"soft_min_expo_{pid}",
                            )
                            obj -= shortfall_p * PORTFOLIO_ILP_MIN_EXPO_PENALTY
                            _min_expo_applied += 1

                    if _min_expo_applied:
                        logger.warning(
                            f"[PortfolioILP] Applied {_min_expo_applied} "
                            f"soft min-exposure constraints "
                            f"(penalty={PORTFOLIO_ILP_MIN_EXPO_PENALTY})"
                        )

                # Set objective AFTER all penalty terms are added
                prob += obj, "TotalPortfolioScore"

                # ── Solve ─────────────────────────────────────────
                solver = pulp.PULP_CBC_CMD(
                    msg=0,
                    timeLimit=PORTFOLIO_ILP_SOLVER_TIMEOUT,
                )
                status = prob.solve(solver)

                if pulp.LpStatus[status] == "Optimal":
                    break  # Success — exit retry loop

                # Not optimal — try relaxing if attempts remain
                if _relax_attempt < _MAX_RELAX_ATTEMPTS - 1:
                    _effective_max_ov += 1
                    logger.warning(
                        f"[PortfolioILP] {pulp.LpStatus[status]} — "
                        f"relaxing max_overlap to {_effective_max_ov} "
                        f"(attempt {_relax_attempt + 2})"
                    )
                else:
                    logger.warning(
                        f"[PortfolioILP] Solver status: {pulp.LpStatus[status]} "
                        f"after {_MAX_RELAX_ATTEMPTS} attempts, "
                        f"falling back to greedy"
                    )
                    return None

            # ── Post-solve: report soft min-exposure shortfalls ───
            if _shortfall_vars:
                _any_shortfall = False
                for pid, sv in _shortfall_vars.items():
                    sv_val = pulp.value(sv)
                    if sv_val is not None and sv_val > 0.01:
                        _any_shortfall = True
                        target = min(
                            player_min_appearances[pid],
                            len(_pid_to_lineup_indices.get(pid, [])),
                        )
                        actual = target - sv_val
                        logger.warning(
                            f"[PortfolioILP] Soft min-expo shortfall: "
                            f"pid={pid} target={target}, "
                            f"actual≈{actual:.1f}, shortfall={sv_val:.2f}"
                        )
                if not _any_shortfall:
                    logger.warning(
                        "[PortfolioILP] All min-exposure targets fully satisfied"
                    )

            # Extract selected lineups
            selected = []
            for i in range(n_cand):
                if pulp.value(y[i]) is not None and pulp.value(y[i]) > 0.5:
                    selected.append(candidates[i][0])

            if len(selected) != num_to_select:
                logger.warning(
                    f"[PortfolioILP] Selected {len(selected)} != "
                    f"requested {num_to_select}, falling back"
                )
                return None

            # Sort by score descending
            score_lookup = {id(lu): sc for lu, sc in candidates}
            selected.sort(key=lambda lu: score_lookup.get(id(lu), 0), reverse=True)

            logger.warning(
                f"[PortfolioILP] ✓ Selected {len(selected)} lineups via ILP "
                f"(max_overlap={_effective_max_ov}, "
                f"total score: {sum(score_lookup.get(id(lu), 0) for lu in selected):.1f})"
            )
            return selected

        except Exception as e:
            logger.warning(f"[PortfolioILP] Solver error: {e}, falling back to greedy")
            return None

    # ------------------------------------------------------------------
    # Heuristic fallback enrichment (when AI + simulation both fail)
    # ------------------------------------------------------------------

    @staticmethod
    def _generate_fallback_enrichment(
        pool: List[PlayerPoolEntry],
        platform: str = "dk",
    ) -> int:
        """Apply salary-tier noise profiles, variance, and ownership heuristics.

        Called as a last-resort when BOTH the AI enrichment layer AND the
        Monte Carlo simulation engine fail or are skipped.  Populates
        noise profile multipliers, ``sim_p10``, ``sim_p50``, ``sim_p90``,
        ``sim_std``, and ``estimated_ownership`` on pool entries that are
        still missing those fields, using mathematical heuristics so the
        composite scorer and ILP optimizer can still function.

        Noise profile heuristic (salary-tier)
        --------------------------------------
        * $9000+  (Safe Star):     std_dev=0.15, ceil=1.35, floor=0.65
        * $6000–$8999 (Mid-Tier):  std_dev=0.18, ceil=1.50, floor=0.40
        * <$6000  (Volatile):      std_dev=0.22, ceil=1.75, floor=0.25

        Variance heuristic
        ------------------
        Uses ``std_dev_multiplier`` (from AI or salary-tier fallback above)
        to compute sim_std.  Falls back to position-based CV when
        std_dev_multiplier is unavailable:
          * Guards  (PG, SG, G): σ = 18% of projected_fp
          * Wings   (SF, F):     σ = 16% of projected_fp
          * Bigs    (PF, C):     σ = 15% of projected_fp
          * UTIL:                σ = 17% of projected_fp

        P10/P90 prefer ceiling/floor multipliers when available, falling
        back to ±1.28σ from the normal distribution.

        Ownership heuristic
        --------------------
        Salary-tier based (DK scale, $3000–$12000):
          * $9000+:  18% ownership  (star plays)
          * $7000–$8999: 14%        (mid-tier)
          * $5000–$6999: 8%         (role players)
          * $3000–$4999: 4%         (punt plays)

        Returns the count of players enriched.
        """
        # ── Salary-tier noise profile defaults ─────────────────────
        # (salary_floor, std_dev, ceiling_mult, floor_mult, archetype)
        _NOISE_TIERS = [
            (9000, 0.15, 1.35, 0.65, "Safe Star"),
            (6000, 0.18, 1.50, 0.40, "Mid-Tier Scorer"),
            (0,    0.22, 1.75, 0.25, "Volatile Value"),
        ]

        # Position → coefficient of variation for std_dev calculation
        _POS_CV: Dict[str, float] = {
            "PG": 0.18, "SG": 0.18, "G": 0.18,
            "SF": 0.16, "F": 0.16,
            "PF": 0.15, "C": 0.15,
            "UTIL": 0.17,
        }

        # Salary tier → ownership %  (DK salary scale)
        _SAL_OWNERSHIP = [
            (9000, 18.0),   # Stars
            (7000, 14.0),   # Mid-tier
            (5000,  8.0),   # Role players
            (0,     4.0),   # Punt plays
        ]

        enriched = 0
        noise_filled = 0
        for entry in pool:
            _touched = False

            # ── Noise profile fill (salary-tier heuristic) ────────
            if entry.std_dev_multiplier is None and entry.salary:
                for sal_floor, std_dev, ceil_m, floor_m, arch in _NOISE_TIERS:
                    if entry.salary >= sal_floor:
                        entry.std_dev_multiplier = std_dev
                        entry.ceiling_multiplier = ceil_m
                        entry.floor_multiplier = floor_m
                        entry.noise_archetype = arch
                        noise_filled += 1
                        _touched = True
                        break

            # ── Variance fill ─────────────────────────────────────
            if entry.sim_std is None and entry.projected_fp and entry.projected_fp > 0:
                # Prefer std_dev_multiplier (from AI or salary-tier above)
                if entry.std_dev_multiplier:
                    std = entry.projected_fp * entry.std_dev_multiplier
                else:
                    pos = (entry.position or "UTIL").upper()
                    if "/" in pos:
                        pos = pos.split("/")[0]
                    cv = _POS_CV.get(pos, 0.17)
                    std = entry.projected_fp * cv

                entry.sim_std = round(std, 2)
                _touched = True

                # Fill percentiles — prefer multipliers when available
                if entry.sim_p50 is None:
                    entry.sim_p50 = round(entry.projected_fp, 1)
                if entry.sim_p10 is None:
                    if entry.floor_multiplier:
                        entry.sim_p10 = round(
                            entry.projected_fp * entry.floor_multiplier, 1
                        )
                    else:
                        entry.sim_p10 = round(
                            max(entry.projected_fp - 1.28 * std, 0.0), 1
                        )
                if entry.sim_p90 is None:
                    if entry.ceiling_multiplier:
                        entry.sim_p90 = round(
                            entry.projected_fp * entry.ceiling_multiplier, 1
                        )
                    else:
                        entry.sim_p90 = round(
                            entry.projected_fp + 1.28 * std, 1
                        )

            # ── Ownership fill ────────────────────────────────────
            if entry.estimated_ownership is None and entry.salary:
                for threshold, own_pct in _SAL_OWNERSHIP:
                    if entry.salary >= threshold:
                        entry.estimated_ownership = own_pct
                        _touched = True
                        break

            if _touched:
                enriched += 1

        if enriched:
            logger.warning(
                f"[Enrich] FALLBACK HEURISTICS ACTIVE: {enriched}/{len(pool)} "
                f"players enriched (noise_profiles={noise_filled}, "
                f"variance+ownership={enriched - noise_filled}) — "
                f"AI enrichment and simulation both unavailable"
            )

        return enriched

    def _compute_baseline_projection_score(
        self,
        pool: List[PlayerPoolEntry],
        platform: str,
        salary_cap: int,
        slot_order: List[str],
        locked_player_ids: List[int],
        salary_floor: int = 0,
        mode: str = "classic",
        sport: str = "nba",
    ) -> Tuple[Optional[float], Optional[OptimizedLineup]]:
        """Single ILP solve with raw median projections to find theoretical max.

        Uses ``projected_fp`` as the objective (no noise, no ceiling blend)
        and ``contest_type="cash"`` to skip GPP-specific constraints
        (ownership cap, pivot rule).  Returns ``(baseline_total, optimal_lineup)``
        where ``optimal_lineup`` is the full :class:`OptimizedLineup` (players,
        salary, etc.) for the unconstrained optimum so the UI can display the
        actual lineup, not just its score. Returns ``(None, None)`` on solver
        failure.

        IMPORTANT: This must use projected_fp (median projections), NOT
        noise-adjusted composite scores, to establish an accurate ceiling
        that the optimality floor is computed against.
        """
        # Build a clean median-projection lookup — no noise, no ceiling
        # blend, no ownership leverage.  This gives a true theoretical
        # maximum against which the optimality floor is computed.
        # MLB routes through ``_effective_projection`` so the baseline
        # reflects park-adjusted upside (Coors lineups should baseline
        # higher); other sports get the raw projection unchanged.
        median_scores = {
            p.player_id: self._effective_projection(p) for p in pool
        }

        _proj_label = "adjusted_fp" if sport == "mlb" else "projected_fp"
        logger.info(
            f"[Baseline] Initiating baseline solve with {len(pool)} players "
            f"(using {_proj_label}, no noise/ceiling blend)"
        )

        # Diagnostic: log the top-10 projection values so we can verify
        # the baseline is not being dragged down by bad projections.
        top10 = sorted(
            pool, key=lambda p: self._effective_projection(p), reverse=True,
        )[:10]
        logger.info(
            f"[Baseline] Top 10 {_proj_label}: "
            + ", ".join(
                f"{p.player_name}={self._effective_projection(p):.1f}"
                for p in top10
            )
        )

        result = self._ilp_optimize(
            pool=pool,
            platform=platform,
            salary_cap=salary_cap,
            slot_order=slot_order,
            locked_player_ids=locked_player_ids,
            score_fn=lambda p: median_scores.get(p.player_id, 0),
            salary_floor=salary_floor,
            mode=mode,
            sport=sport,
            contest_type="cash",  # Skip GPP constraints for clean baseline
            time_limit=30,
        )
        if result is None:
            logger.warning("[Baseline] Baseline ILP solve returned None")
            return None, None

        baseline_total = sum(
            median_scores.get(p.player_id, 0) for p in result.values()
        )
        logger.info(
            f"[Baseline] Optimal baseline: {baseline_total:.1f} DKFP "
            f"({len(result)} players, {_proj_label})"
        )

        # Reuse the standard assignment-to-OptimizedLineup helper. fp_override
        # ensures total_projected_fp matches the baseline_total we computed
        # from raw projected_fp (no noise), even though _ilp_optimize used the
        # same score_fn — this is just defensive precision.
        try:
            optimal_lineup = self._build_lineup_from_assignment(
                lineup=result,
                platform=platform,
                salary_cap=salary_cap,
                roster_slots=slot_order,
                sport=sport,
                fp_override=baseline_total,
            )
        except Exception as exc:
            logger.warning(
                f"[Baseline] Failed to materialize optimal lineup: {exc}"
            )
            optimal_lineup = None

        return baseline_total, optimal_lineup

    def _build_single_lineup(
        self,
        pool: List[PlayerPoolEntry],
        platform: str,
        salary_cap: int,
        roster_slots: List[str],
        slot_order: List[str],
        locked_player_ids: List[int],
        extra_excludes: Set[int],
        score_fn: Callable[[PlayerPoolEntry], float],
        stack_player_ids: Optional[List[int]] = None,
        salary_floor: int = 0,
        stack_game_id: Optional[str] = None,
        stack_primary_team: Optional[str] = None,
        stack_size: int = 0,
        stack_bring_back: bool = False,
        mode: str = "classic",
        sport: str = "nba",
        contest_type: str = "gpp",
        skip_ilp: bool = False,
        ilp_time_limit: Optional[int] = None,
        max_improve_iters: int = 100,
        skip_two_swap: bool = False,
        min_projection_floor: Optional[float] = None,
        baseline_projection_score: Optional[float] = None,
        minimum_relaxation_floor: float = 0.75,
        max_cumulative_ownership: Optional[float] = None,
        enable_stacking: bool = False,
        stack_overrides: Optional[Dict[str, Any]] = None,
    ) -> Optional[OptimizedLineup]:
        """Build one lineup using a custom scoring function.

        This is the core building block for both single and multi-lineup
        generation.  Mirrors the logic of ``optimize()`` but uses the
        provided ``score_fn`` for player ranking.

        Args:
            stack_player_ids: Pre-selected player IDs for game stacking
                              (greedy fallback only — force-locked).
            salary_floor: Minimum total salary (0 = no floor).
            stack_game_id: Target game for ILP stacking constraints.
            stack_primary_team: Primary team abbreviation for stacking.
            stack_size: Number of players to stack from primary team.
            stack_bring_back: Whether to require a bring-back player.
            mode: "classic" or "showdown".
            min_projection_floor: Hard minimum sum-of-projected_fp
                that the ILP must meet.
            baseline_projection_score: The baseline optimal projection
                score (used to compute thresholds during relaxation).
            minimum_relaxation_floor: Hard floor threshold fraction —
                if relaxation drops below this, raise LineupGenerationError.
            max_cumulative_ownership: Hard cap on the sum of all selected
                players' projected ownership (%). None = no cap.
                Auto-relaxes by 10% per retry on infeasibility.
        """
        indexed_order = _index_slots(slot_order)

        lineup: Dict[str, PlayerPoolEntry] = {}
        used_ids: Set[int] = set()
        remaining_salary = salary_cap
        remaining_slots = list(indexed_order)
        warnings: List[str] = []

        # Filter out extra excludes and injured players (Out / Doubtful)
        available_pool = [
            p for p in pool
            if p.player_id not in extra_excludes
            and p.injury_status not in ("Out", "Doubtful")
        ]
        logger.info(
            f"[Hybrid] Initiating lineup build with {len(available_pool)} "
            f"available players (from {len(pool)} pool, "
            f"{len(pool) - len(available_pool)} excluded/injured)"
        )
        if len(available_pool) < 50:
            logger.warning(
                f"[Hybrid] LOW POOL SIZE: only {len(available_pool)} players "
                f"available — lineup quality may be degraded. "
                f"Excludes={len(extra_excludes)}, "
                f"injured={sum(1 for p in pool if p.injury_status in ('Out', 'Doubtful'))}"
            )

        # ── Step 1: Always run greedy pipeline first ──────────────────
        # Pre-assign locked players (user locks + stack locks)
        all_locks = list(locked_player_ids)
        if stack_player_ids:
            for sid in stack_player_ids:
                if sid not in all_locks:
                    all_locks.append(sid)

        for locked_id in all_locks:
            player = next(
                (p for p in available_pool if p.player_id == locked_id),
                None,
            )
            if not player:
                continue

            assigned = False
            for isl in remaining_slots:
                base = _base_slot(isl)
                elig = self._get_slot_eligible_positions(base, platform, sport)
                if self._player_matches_slot(player.position, elig) and isl not in lineup:
                    lineup[isl] = player
                    used_ids.add(player.player_id)
                    remaining_salary -= player.salary
                    remaining_slots.remove(isl)
                    assigned = True
                    break

            if not assigned:
                warnings.append(
                    f"Could not assign locked player {player.player_name}"
                )

        # Greedy fill with custom scoring
        lineup = self._greedy_fill_scored(
            available_pool,
            lineup,
            remaining_slots,
            used_ids,
            remaining_salary,
            platform,
            score_fn,
            sport=sport,
        )

        # Iterative improvement with custom scoring (single-slot swaps)
        lineup = self._iterative_improve_scored(
            lineup, available_pool, salary_cap, platform, score_fn,
            max_iterations=max_improve_iters,
            sport=sport,
        )

        # Two-slot swap improvement (trade-down-up scenarios)
        # Skipped in budget mode — the O(pool²) cost is too high for
        # diversity-only candidates that Phase 4 will mostly discard.
        if not skip_two_swap:
            lineup = self._two_slot_swap_improve(
                lineup, available_pool, salary_cap, platform, score_fn,
                sport=sport,
            )

        # Enforce salary floor if specified
        if salary_floor > 0:
            lineup = self._enforce_salary_floor(
                lineup, available_pool, salary_cap, salary_floor,
                platform, self._get_slot_eligible_positions,
                sport=sport,
            )

        # ── Step 2: ILP refinement with warm start ───────────────────
        # C7b (projection floor) is NOT passed to the candidate ILP.
        # The baseline solve uses no stacking, no ceiling blend, no GPP
        # constraints → pure projected_fp max.  But candidates optimise
        # a ceiling-heavy composite WITH stacking + ownership + pivot.
        # Enforcing projected_fp >= 90% of unconstrained baseline made
        # the ILP infeasible for most stacking targets → CBC timeout →
        # 100% greedy fallback.  Quality is enforced post-ILP and by
        # Phase 3/4 scoring + portfolio selection.
        _ilp_used = None   # None=skipped, False=attempted+greedy, True=ILP accepted
        # (defined before the `if` so it's always in scope for the response)
        if _PULP_AVAILABLE and not skip_ilp and len(lineup) == len(indexed_order):
            _ilp_used = False  # Mark as attempted
            greedy_score = sum(score_fn(p) for p in lineup.values())
            greedy_proj_total = sum(p.projected_fp for p in lineup.values())
            try:
                ilp_result = self._ilp_optimize(
                    pool=available_pool,
                    platform=platform,
                    salary_cap=salary_cap,
                    slot_order=slot_order,
                    locked_player_ids=locked_player_ids,
                    score_fn=score_fn,
                    salary_floor=salary_floor,
                    stack_game_id=stack_game_id,
                    stack_primary_team=stack_primary_team,
                    stack_size=stack_size,
                    bring_back=stack_bring_back,
                    mode=mode,
                    sport=sport,
                    warm_start_lineup=lineup,
                    warm_start_score=greedy_score,
                    contest_type=contest_type,
                    time_limit=ilp_time_limit,
                    min_projection_floor=min_projection_floor,
                    max_cumulative_ownership=max_cumulative_ownership,
                    enable_stacking=enable_stacking,
                    stack_overrides=stack_overrides,
                )

                # ── Ownership cap relaxation ────────────────────
                # If ILP infeasible and cumulative ownership cap is
                # active, relax by 10% per retry (up to 3×).
                _OWN_RELAX_MAX_RETRIES = 3
                if (
                    ilp_result is None
                    and max_cumulative_ownership is not None
                    and max_cumulative_ownership > 0
                ):
                    _own_cap = max_cumulative_ownership
                    for _own_retry in range(_OWN_RELAX_MAX_RETRIES):
                        _own_cap *= 1.10
                        logger.warning(
                            f"[Hybrid] Ownership cap infeasible, "
                            f"relaxing to {_own_cap:.1f}% "
                            f"(attempt {_own_retry + 1}/"
                            f"{_OWN_RELAX_MAX_RETRIES})"
                        )
                        ilp_result = self._ilp_optimize(
                            pool=available_pool,
                            platform=platform,
                            salary_cap=salary_cap,
                            slot_order=slot_order,
                            locked_player_ids=locked_player_ids,
                            score_fn=score_fn,
                            salary_floor=salary_floor,
                            stack_game_id=stack_game_id,
                            stack_primary_team=stack_primary_team,
                            stack_size=stack_size,
                            bring_back=stack_bring_back,
                            mode=mode,
                            sport=sport,
                            warm_start_lineup=lineup,
                            warm_start_score=greedy_score,
                            contest_type=contest_type,
                            time_limit=ilp_time_limit,
                            min_projection_floor=min_projection_floor,
                            max_cumulative_ownership=_own_cap,
                            enable_stacking=enable_stacking,
                            stack_overrides=stack_overrides,
                        )
                        if ilp_result is not None:
                            break

                # ── ILP acceptance logic ──────────────────────────
                if ilp_result and len(ilp_result) == len(indexed_order):
                    ilp_score = sum(score_fn(p) for p in ilp_result.values())
                    ilp_proj_total = sum(
                        p.projected_fp for p in ilp_result.values()
                    )

                    # Post-ILP quality gate: reject ILP solutions that
                    # traded too much projection for ceiling.  Use 80%
                    # of baseline as a soft floor (not 90% like C7b).
                    _POST_ILP_MIN_PROJ_FRAC = 0.80  # was 0.75 — tighter guard against projection sacrifice
                    _soft_floor = (
                        baseline_projection_score * _POST_ILP_MIN_PROJ_FRAC
                        if baseline_projection_score
                        else 0
                    )
                    if _soft_floor > 0 and ilp_proj_total < _soft_floor:
                        logger.warning(
                            f"[Hybrid] ILP result projected_fp "
                            f"({ilp_proj_total:.1f}) below soft floor "
                            f"({_soft_floor:.1f} = {_POST_ILP_MIN_PROJ_FRAC:.0%} of baseline "
                            f"{baseline_projection_score:.1f}) — "
                            f"keeping greedy "
                            f"(greedy_proj={greedy_proj_total:.1f})"
                        )
                    elif ilp_score > greedy_score:
                        logger.info(
                            f"[Hybrid] ILP improved over greedy: "
                            f"composite {ilp_score:.2f} > {greedy_score:.2f}, "
                            f"proj_fp {ilp_proj_total:.1f} vs "
                            f"greedy {greedy_proj_total:.1f}"
                        )
                        lineup = ilp_result
                        _ilp_used = True
                    elif (
                        ilp_proj_total > greedy_proj_total * 1.05
                        and ilp_score >= greedy_score * 0.97
                    ):
                        # ── Projection-aware tiebreaker ──────────
                        # When composite scores are within 3%, prefer
                        # the lineup with 5%+ better projected_fp.
                        # Avoids discarding high-projection ILP
                        # results due to marginal composite jitter.
                        logger.info(
                            f"[Hybrid] ILP accepted via projection "
                            f"tiebreaker: proj {ilp_proj_total:.1f} > "
                            f"greedy {greedy_proj_total:.1f} (+5%), "
                            f"composite {ilp_score:.2f} vs "
                            f"{greedy_score:.2f} (within 3%)"
                        )
                        lineup = ilp_result
                        _ilp_used = True
                    elif (
                        strategy in ("ceiling", "contrarian")
                        and ilp_score >= greedy_score * 0.95
                    ):
                        # ── Ceiling tiebreaker ──────────────────────
                        # For ceiling/contrarian strategies, if composite
                        # scores are within 5%, prefer ILP result with
                        # 3%+ higher total ceiling (sim_p90 or ceiling_fp).
                        ilp_ceiling = sum(
                            (getattr(p, "sim_p90", None) or p.ceiling_fp or p.projected_fp)
                            for p in ilp_result.values()
                        )
                        greedy_ceiling = sum(
                            (getattr(p, "sim_p90", None) or p.ceiling_fp or p.projected_fp)
                            for p in lineup.values()
                        )
                        if ilp_ceiling > greedy_ceiling * 1.03:
                            logger.info(
                                f"[Hybrid] ILP accepted via ceiling "
                                f"tiebreaker: ceiling {ilp_ceiling:.1f} > "
                                f"greedy {greedy_ceiling:.1f} (+3%), "
                                f"composite {ilp_score:.2f} vs "
                                f"{greedy_score:.2f} (within 5%)"
                            )
                            lineup = ilp_result
                            _ilp_used = True
                        else:
                            logger.info(
                                f"[Hybrid] Greedy retained (ceiling tie): "
                                f"ILP ceil={ilp_ceiling:.1f}, "
                                f"greedy ceil={greedy_ceiling:.1f}, "
                                f"composite {greedy_score:.2f} vs "
                                f"{ilp_score:.2f}"
                            )
                    else:
                        logger.info(
                            f"[Hybrid] Greedy retained over ILP: "
                            f"composite {greedy_score:.2f} >= {ilp_score:.2f}, "
                            f"greedy_proj={greedy_proj_total:.1f}, "
                            f"ilp_proj={ilp_proj_total:.1f}"
                        )
                else:
                    logger.warning(
                        f"[Hybrid] ILP returned None "
                        f"(timeout/infeasible) — keeping greedy "
                        f"(greedy_proj={greedy_proj_total:.1f})"
                    )
            except LineupGenerationError:
                raise
            except Exception as e:
                logger.debug(f"[Hybrid] ILP refinement failed: {e}")

        # ── Late-swap roster-slot optimisation ──────────────────────
        # Re-assign the same 8 players to positional slots so that
        # the latest-game players sit in UTIL / G / F flex slots,
        # maximising late-swap optionality.
        if mode != "showdown":
            lineup = self._optimize_roster_slots(
                lineup, roster_slots, platform=platform, sport=sport,
            )

        # Build response via shared helper
        return self._dict_to_optimized_lineup(
            lineup, platform, sport, salary_cap, roster_slots,
            ilp_used=_ilp_used, warnings=warnings,
        )

    # ------------------------------------------------------------------
    # Dict-to-OptimizedLineup conversion helper
    # ------------------------------------------------------------------

    def _dict_to_optimized_lineup(
        self,
        lineup: Dict[str, "PlayerPoolEntry"],
        platform: str,
        sport: str,
        salary_cap: int,
        roster_slots: List[str],
        ilp_used: bool = True,
        warnings: Optional[List[str]] = None,
    ) -> Optional["OptimizedLineup"]:
        """Convert an {indexed_slot: PlayerPoolEntry} dict to OptimizedLineup.

        Shared by ``_build_single_lineup`` and the K-Best iterative loop
        so lineup packaging logic is not duplicated.
        """
        from app.models.lineup import OptimizedLineup, LineupPlayer

        if warnings is None:
            warnings = []

        total_salary = sum(p.salary for p in lineup.values())
        total_fp = sum(p.projected_fp for p in lineup.values())
        total_floor = sum(p.floor_fp for p in lineup.values())
        total_ceil = sum(p.ceiling_fp for p in lineup.values())

        indexed_roster = _index_slots(roster_slots)
        players = []
        for isl in indexed_roster:
            p = lineup.get(isl)
            if p:
                players.append(
                    LineupPlayer(
                        player_id=p.player_id,
                        player_name=p.player_name,
                        display_name=p.display_name or p.player_name,
                        position=p.position,
                        roster_slot=_base_slot(isl),
                        team_abbreviation=p.team_abbreviation,
                        salary=p.salary,
                        projected_fp=p.projected_fp,
                        floor_fp=p.floor_fp,
                        ceiling_fp=p.ceiling_fp,
                        projected_minutes=p.projected_minutes,
                        projected_stats=p.projected_stats,
                        dk_player_id=p.dk_player_id,
                    )
                )

        if not players:
            return None

        return OptimizedLineup(
            platform=platform,
            sport=sport,
            players=players,
            total_salary=total_salary,
            salary_remaining=salary_cap - total_salary,
            total_projected_fp=round(total_fp, 1),
            total_floor_fp=round(total_floor, 1),
            total_ceiling_fp=round(total_ceil, 1),
            salary_cap=salary_cap,
            roster_slots=roster_slots,
            warnings=warnings,
            ilp_used=ilp_used,
        )

    # ------------------------------------------------------------------
    # Scored variants of greedy fill / iterative improve
    # ------------------------------------------------------------------

    def _greedy_fill_scored(
        self,
        pool: List[PlayerPoolEntry],
        lineup: Dict[str, PlayerPoolEntry],
        remaining_slots: List[str],
        used_ids: Set[int],
        remaining_salary: int,
        platform: str,
        score_fn: Callable[[PlayerPoolEntry], float],
        sport: str = "nba",
    ) -> Dict[str, PlayerPoolEntry]:
        """Fill remaining slots greedily using a custom scoring function.

        When cached correlations are available (GPP), candidates who share
        a game with already-selected lineup players receive a correlation-
        informed boost, creating valuable secondary correlations.
        """
        from app.config.constants import (
            GREEDY_FILL_CORRELATION_WEIGHT,
            GREEDY_FILL_CORRELATION_BOOST,
            GREEDY_FILL_SAME_GAME_BONUS,
        )

        if remaining_slots and "_" not in remaining_slots[0]:
            remaining_slots = _index_slots(remaining_slots)

        has_correlations = bool(self._cached_correlations)

        for i, isl in enumerate(remaining_slots):
            base = _base_slot(isl)
            elig_positions = self._get_slot_eligible_positions(
                base, platform, sport
            )
            future_slots = remaining_slots[i + 1:]

            min_future_salary = self._min_salary_for_slots(
                pool, future_slots, used_ids, platform, sport
            )

            budget = remaining_salary - min_future_salary

            candidates = [
                p
                for p in pool
                if (base in p.eligible_slots or self._player_matches_slot(p.position, elig_positions))
                and p.player_id not in used_ids
                and p.salary <= budget
                and p.injury_status not in ("Out", "Doubtful")
            ]

            if not candidates:
                candidates = [
                    p
                    for p in pool
                    if (base in p.eligible_slots or self._player_matches_slot(p.position, elig_positions))
                    and p.player_id not in used_ids
                    and p.salary <= remaining_salary
                    and p.injury_status not in ("Out", "Doubtful")
                ]

            if not candidates:
                logger.warning(
                    f"Cannot fill slot {base} — no eligible players "
                    f"within budget (${remaining_salary} remaining)"
                )
                continue

            # ── Correlation-aware scoring ──────────────────────────
            # For each candidate, compute avg correlation to players
            # already in the lineup.  Blend with base score.
            if has_correlations and lineup:
                selected_ids = [p.player_id for p in lineup.values()]
                selected_games = {
                    p.game_id for p in lineup.values() if p.game_id
                }

                def _corr_boosted_score(candidate: PlayerPoolEntry) -> float:
                    base_score = score_fn(candidate)

                    # Compute avg correlation to already-selected players
                    corr_vals = []
                    for sel_id in selected_ids:
                        pair_key = (
                            min(candidate.player_id, sel_id),
                            max(candidate.player_id, sel_id),
                        )
                        corr = self._cached_correlations.get(pair_key)
                        if corr is not None:
                            corr_vals.append(corr)

                    if corr_vals:
                        avg_corr = sum(corr_vals) / len(corr_vals)
                        corr_signal = base_score * (
                            1.0 + avg_corr * GREEDY_FILL_CORRELATION_BOOST
                        )
                        final = (
                            (1.0 - GREEDY_FILL_CORRELATION_WEIGHT) * base_score
                            + GREEDY_FILL_CORRELATION_WEIGHT * corr_signal
                        )
                    elif (
                        candidate.game_id
                        and candidate.game_id in selected_games
                    ):
                        # Same game but no correlation data — small bonus
                        final = base_score * (1.0 + GREEDY_FILL_SAME_GAME_BONUS)
                    else:
                        final = base_score

                    return final

                best = max(candidates, key=_corr_boosted_score)
            else:
                best = max(candidates, key=score_fn)

            lineup[isl] = best
            used_ids.add(best.player_id)
            remaining_salary -= best.salary

        return lineup

    def _iterative_improve_scored(
        self,
        lineup: Dict[str, PlayerPoolEntry],
        pool: List[PlayerPoolEntry],
        salary_cap: int,
        platform: str,
        score_fn: Callable[[PlayerPoolEntry], float],
        max_iterations: int = 100,
        sport: str = "nba",
    ) -> Dict[str, PlayerPoolEntry]:
        """Pairwise swap improvement using custom scoring function.

        Pre-computes scores once per iteration to avoid RNG noise
        producing inconsistent baselines during comparison.
        """
        used_ids = {p.player_id for p in lineup.values()}

        # Pre-compute a score cache: evaluate each player once per iteration
        # so noise is fixed across comparisons within the same pass.
        for iteration in range(max_iterations):
            improved = False
            score_cache: Dict[int, float] = {}
            for p in lineup.values():
                score_cache[p.player_id] = score_fn(p)
            for p in pool:
                if p.player_id not in score_cache:
                    score_cache[p.player_id] = score_fn(p)

            current_total_score = sum(
                score_cache[p.player_id] for p in lineup.values()
            )
            current_total_salary = sum(
                p.salary for p in lineup.values()
            )

            # Best-improvement search: evaluate ALL candidates per slot,
            # track the best swap globally across all slots, then apply it.
            # This avoids taking a +2 swap when a +8 swap exists elsewhere.
            best_slot = None
            best_candidate = None
            best_new_score = current_total_score
            best_new_salary = current_total_salary

            for isl, current in list(lineup.items()):
                base = _base_slot(isl) if "_" in isl else isl
                elig_positions = self._get_slot_eligible_positions(
                    base, platform, sport
                )

                for candidate in pool:
                    if candidate.player_id in used_ids:
                        continue
                    if not self._player_matches_slot(candidate.position, elig_positions):
                        continue
                    # Never swap in an injured player
                    if candidate.injury_status in ("Out", "Doubtful"):
                        continue

                    new_salary = (
                        current_total_salary
                        - current.salary
                        + candidate.salary
                    )
                    if new_salary > salary_cap:
                        continue

                    new_score = (
                        current_total_score
                        - score_cache[current.player_id]
                        + score_cache[candidate.player_id]
                    )
                    if new_score > best_new_score:
                        best_slot = isl
                        best_candidate = candidate
                        best_new_score = new_score
                        best_new_salary = new_salary

            if best_slot is not None and best_candidate is not None:
                old_player = lineup[best_slot]
                used_ids.discard(old_player.player_id)
                used_ids.add(best_candidate.player_id)
                lineup[best_slot] = best_candidate
                current_total_salary = best_new_salary
                current_total_score = best_new_score
                improved = True

            if not improved:
                break

        return lineup

    def _two_slot_swap_improve(
        self,
        lineup: Dict[str, "PlayerPoolEntry"],
        pool: List["PlayerPoolEntry"],
        salary_cap: int,
        platform: str,
        score_fn,
        max_iterations: int = 50,
        sport: str = "nba",
    ) -> Dict[str, "PlayerPoolEntry"]:
        """Two-slot swap improvement: trade down at one slot to upgrade another.

        Tries all pairs of slots simultaneously.  Can find "trade down at PG
        to fund an upgrade at C" improvements that single-slot swaps miss.

        Parameters
        ----------
        max_iterations : int
            Maximum number of swap rounds (each round tries all slot pairs).
        """
        from app.config.constants import TWO_SLOT_SWAP_MAX_ITERATIONS
        max_iterations = min(max_iterations, TWO_SLOT_SWAP_MAX_ITERATIONS)

        used_ids = {p.player_id for p in lineup.values()}
        slot_keys = list(lineup.keys())

        for iteration in range(max_iterations):
            improved = False

            # Pre-compute scores once per iteration to avoid RNG noise
            # inconsistencies during comparison.
            score_cache: Dict[int, float] = {}
            for p in lineup.values():
                score_cache[p.player_id] = score_fn(p)
            for p in pool:
                if p.player_id not in score_cache:
                    score_cache[p.player_id] = score_fn(p)

            current_total_score = sum(
                score_cache[p.player_id] for p in lineup.values()
            )
            current_salary = sum(p.salary for p in lineup.values())

            for i in range(len(slot_keys)):
                if improved:
                    break
                for j in range(i + 1, len(slot_keys)):
                    if improved:
                        break

                    slot_a = slot_keys[i]
                    slot_b = slot_keys[j]
                    player_a = lineup[slot_a]
                    player_b = lineup[slot_b]

                    base_a = _base_slot(slot_a) if "_" in slot_a else slot_a
                    base_b = _base_slot(slot_b) if "_" in slot_b else slot_b
                    elig_a = self._get_slot_eligible_positions(base_a, platform, sport)
                    elig_b = self._get_slot_eligible_positions(base_b, platform, sport)

                    # Try replacing both slots simultaneously
                    best_score = current_total_score
                    best_pair = None

                    for cand_a in pool:
                        if cand_a.player_id in used_ids:
                            continue
                        if not self._player_matches_slot(cand_a.position, elig_a):
                            continue
                        # Never swap in an injured player
                        if cand_a.injury_status in ("Out", "Doubtful"):
                            continue

                        for cand_b in pool:
                            if cand_b.player_id in used_ids:
                                continue
                            if cand_b.player_id == cand_a.player_id:
                                continue
                            if not self._player_matches_slot(cand_b.position, elig_b):
                                continue
                            # Never swap in an injured player
                            if cand_b.injury_status in ("Out", "Doubtful"):
                                continue

                            new_salary = (
                                current_salary
                                - player_a.salary - player_b.salary
                                + cand_a.salary + cand_b.salary
                            )
                            if new_salary > salary_cap:
                                continue

                            new_score = (
                                current_total_score
                                - score_cache[player_a.player_id]
                                - score_cache[player_b.player_id]
                                + score_cache[cand_a.player_id]
                                + score_cache[cand_b.player_id]
                            )
                            if new_score > best_score:
                                best_score = new_score
                                best_pair = (cand_a, cand_b)

                    if best_pair is not None:
                        cand_a, cand_b = best_pair
                        used_ids.discard(player_a.player_id)
                        used_ids.discard(player_b.player_id)
                        used_ids.add(cand_a.player_id)
                        used_ids.add(cand_b.player_id)
                        lineup[slot_a] = cand_a
                        lineup[slot_b] = cand_b
                        improved = True

            if not improved:
                break

        return lineup

    # ------------------------------------------------------------------
    # NFL stacking helper — shared by _ilp_optimize and _build_kbest_prob
    # ------------------------------------------------------------------

    def _apply_nfl_stack_constraints(
        self,
        prob: Any,
        pool: List[PlayerPoolEntry],
        player_lookup: Dict[int, PlayerPoolEntry],
        vars_by_player: Dict[int, List[Tuple[str, Any]]],
        sport: str,
        *,
        qb_min_override: Optional[int] = None,
        require_bring_back_override: Optional[bool] = None,
    ) -> None:
        """Apply NFL QB-pass-catcher and bring-back constraints.

        ``qb_min_override`` and ``require_bring_back_override`` (Prompt 5.1)
        are user-supplied per-request values that take precedence over the
        SportConfig defaults. ``None`` means "use the config value" so the
        UI can mix overrides (e.g., set bring-back off but leave QB min at
        the default 1).

        Reads ``stack_rules`` from the SportConfig and adds three types of
        ILP constraints (no-op when sport != 'nfl' or rules dict is empty):

        1. **QB → pass-catcher minimum** — when a team's QB is selected,
           at least ``qb_min_pass_catchers`` WR/TE from that same team must
           also be selected. Encoded as ``sum(pc_T) >= qb_min * qb_T`` so
           the constraint deactivates when ``qb_T = 0`` (no QB → no min).

        2. **QB → pass-catcher maximum** — same-team WR/TE count is capped
           at ``qb_max_pass_catchers`` when the QB is selected. Encoded as
           ``sum(pc_T) <= qb_max + BIG_M*(1-qb_T)``: when ``qb_T = 1`` the
           cap binds; when ``qb_T = 0`` the slack term lifts it above any
           feasible value (BIG_M = number of pass-catchers on team T,
           always ≥ ``sum(pc_T)``).

        3. **Bring-back** — when ``require_bring_back`` is True, selecting
           a QB requires at least one offensive skill player (WR/RB/TE)
           from the QB's opponent in the SAME game. Encoded as
           ``sum(opp_skill_G_T) >= qb_T``.

        The position field is split on '/' and the primary token (uppercase)
        is used for classification; an "RB/WR" entry counts as RB.
        """
        if sport != "nfl":
            return
        try:
            from app.sports import get_config as _get_sport_cfg
            cfg = _get_sport_cfg(sport)
        except Exception:
            return
        rules = cfg.stack_rules or {}
        # User overrides win when present; otherwise fall back to config.
        qb_min = (
            int(qb_min_override)
            if qb_min_override is not None
            else int(rules.get("qb_min_pass_catchers", 0) or 0)
        )
        qb_max = int(rules.get("qb_max_pass_catchers", 0) or 0)
        require_bb = (
            bool(require_bring_back_override)
            if require_bring_back_override is not None
            else bool(rules.get("require_bring_back", False))
        )
        # Ensure the override doesn't push qb_min above the config qb_max
        # (would make every QB infeasible). When the user picks a min that
        # exceeds the existing max, lift the cap so the lower-bound rule
        # remains satisfiable.
        if qb_max > 0 and qb_min > qb_max:
            qb_max = qb_min
        if qb_min <= 0 and qb_max <= 0 and not require_bb:
            return

        import pulp as _pulp

        # ── Bucket players by team and game ──────────────────────────
        # team → {qb_pids, pc_pids (WR/TE), skill_pids (WR/RB/TE)}
        team_buckets: Dict[str, Dict[str, List[int]]] = {}
        team_to_game: Dict[str, Optional[str]] = {}
        for p in pool:
            team = (p.team_abbreviation or "").upper()
            if not team:
                continue
            primary_pos = (p.position or "").split("/")[0].strip().upper()
            bucket = team_buckets.setdefault(team, {
                "qb": [], "pc": [], "skill": [],
            })
            if primary_pos == "QB":
                bucket["qb"].append(p.player_id)
            elif primary_pos in ("WR", "TE"):
                bucket["pc"].append(p.player_id)
                bucket["skill"].append(p.player_id)
            elif primary_pos == "RB":
                bucket["skill"].append(p.player_id)
            # DST and others are irrelevant to the stack rules.
            if team not in team_to_game and p.game_id:
                team_to_game[team] = p.game_id

        # Build per-team game → opponents map for bring-back lookups.
        # Two teams that share a game_id are opponents of each other.
        game_to_teams: Dict[str, List[str]] = {}
        for team, gid in team_to_game.items():
            if gid:
                game_to_teams.setdefault(gid, []).append(team)

        # ── Per-team QB-pass-catcher constraints ─────────────────────
        for team, b in team_buckets.items():
            qb_pids = [pid for pid in b["qb"] if pid in vars_by_player]
            pc_pids = [pid for pid in b["pc"] if pid in vars_by_player]
            if not qb_pids:
                continue  # no QB on this team — nothing to anchor

            qb_sum = _pulp.lpSum(
                var for pid in qb_pids
                for _j, var in vars_by_player[pid]
            )
            pc_sum = _pulp.lpSum(
                var for pid in pc_pids
                for _j, var in vars_by_player[pid]
            ) if pc_pids else 0

            # Lower bound: qb_T = 1  ⇒  pc_T >= qb_min
            if qb_min > 0:
                if not pc_pids and qb_min > 0:
                    # No pass-catchers available on this team — the QB
                    # cannot satisfy the min, so prevent it from being
                    # selected. Equivalent to qb_sum <= 0.
                    prob += (qb_sum <= 0, f"nfl_qb_no_pc_{team}")
                else:
                    prob += (
                        pc_sum - qb_min * qb_sum >= 0,
                        f"nfl_qb_min_pc_{team}",
                    )

            # Upper bound: qb_T = 1  ⇒  pc_T <= qb_max  (Big-M conditional)
            # BIG_M = pool count of pass-catchers on team T; this is the
            # smallest M for which pc_T <= qb_max + M*(1 - qb_T) is loose
            # enough to be inactive when qb_T = 0.
            if qb_max > 0 and pc_pids:
                big_m = max(qb_max + 1, len(pc_pids))
                prob += (
                    pc_sum + big_m * qb_sum <= qb_max + big_m,
                    f"nfl_qb_max_pc_{team}",
                )

        # ── Bring-back: at least 1 WR/RB/TE from QB's opponent ─────
        if require_bb:
            for team, b in team_buckets.items():
                qb_pids = [pid for pid in b["qb"] if pid in vars_by_player]
                if not qb_pids:
                    continue
                gid = team_to_game.get(team)
                if not gid:
                    continue
                opp_teams = [
                    t for t in game_to_teams.get(gid, []) if t != team
                ]
                if not opp_teams:
                    continue  # solo game — no opponent in pool

                # Aggregate skill players (WR/RB/TE) across all opponent
                # teams in this game (typically just one).
                opp_skill_pids: List[int] = []
                for ot in opp_teams:
                    opp_skill_pids.extend(team_buckets[ot]["skill"])
                opp_skill_pids = [
                    pid for pid in opp_skill_pids if pid in vars_by_player
                ]

                qb_sum = _pulp.lpSum(
                    var for pid in qb_pids
                    for _j, var in vars_by_player[pid]
                )
                if not opp_skill_pids:
                    # No bring-back candidate exists — disable the QB so
                    # the model stays feasible by other QB choices.
                    prob += (qb_sum <= 0, f"nfl_bb_no_opp_{team}")
                    continue
                opp_sum = _pulp.lpSum(
                    var for pid in opp_skill_pids
                    for _j, var in vars_by_player[pid]
                )
                prob += (
                    opp_sum - qb_sum >= 0,
                    f"nfl_bring_back_{team}",
                )

    # ------------------------------------------------------------------
    # MLB stacking helper — pitcher fade + team-stack constraints
    # ------------------------------------------------------------------

    def _apply_mlb_stack_constraints(
        self,
        prob: Any,
        pool: List[PlayerPoolEntry],
        player_lookup: Dict[int, PlayerPoolEntry],
        vars_by_player: Dict[int, List[Tuple[str, Any]]],
        sport: str,
        *,
        primary_size_override: Optional[int] = None,
        secondary_size_override: Optional[int] = None,
    ) -> None:
        """Apply MLB pitcher-fade and team-stack constraints from sport config.

        ``primary_size_override`` and ``secondary_size_override`` (Prompt 5.1)
        let the UI dial in non-default distributions like 4-4 or 5-2 without
        breaking the generic API contract. ``None`` means "use the config
        default" (5-3). The pitcher-fade rule has no override — it's a hard
        DK-rules-driven constraint and should always be active when MLB
        stacking is enabled.

        No-op when ``sport != 'mlb'`` or ``stack_rules`` is empty.

        Three rules drive MLB DFS correlation:

        1. **Pitcher fade (HARD)** — never select a hitter who is playing
           against a pitcher already in the lineup. Encoded as
           ``sum(opp_hitter_T) + BIG_M * pitcher_P <= BIG_M`` with
           ``BIG_M = 8`` (max hitter slots). When ``pitcher_P = 1`` the
           RHS collapses to 0, locking out every opposing hitter; when
           ``pitcher_P = 0`` the constraint is loose and inactive.

        2. **Primary stack (HARD, ≥)** — at least one team must contribute
           ``primary_stack_size`` hitters (typically 5, the DK cap). A
           binary auxiliary ``y_T`` per team plus ``sum(y_T) >= 1`` and
           ``hitters_T >= primary_size * y_T`` forces exactly one team to
           anchor the lineup. Combined with the existing ``max_same_team_count``
           hitter cap of 5, this produces a clean 5-stack on the primary
           team.

        3. **Secondary stack (SOFT, objective bonus)** — a binary ``z_T``
           per team marks "T has ≥ secondary_size hitters and isn't the
           primary". A small bonus ``SECONDARY_BONUS * sum(z_T)`` is added
           to the maximisation objective so the solver gravitates toward
           5-3 / 5-2 distributions when the player pool supports them,
           without forcing infeasibility on slim slates.

        Pitchers are identified via ``pos_to_class`` (so ``"P" / "SP" / "RP"``
        all route to the pitcher class). Two-way listings (Ohtani at "P")
        are handled correctly because DK rules score them as pitcher only.
        """
        if sport != "mlb":
            return
        try:
            from app.sports import get_config as _gc
            cfg = _gc(sport)
        except Exception:
            return
        rules = cfg.stack_rules or {}
        primary_size = (
            int(primary_size_override)
            if primary_size_override is not None
            else int(rules.get("primary_stack_size", 0) or 0)
        )
        secondary_size = (
            int(secondary_size_override)
            if secondary_size_override is not None
            else int(rules.get("secondary_stack_size", 0) or 0)
        )
        fade = bool(rules.get("fade_opposing_hitters", False))
        # Defensive: also reject the over-budget case at the helper level
        # so direct callers (tests, future internal pipelines) get the
        # same protection as API consumers.
        if primary_size + secondary_size > 8:
            raise ValueError(
                f"MLB stack-size overrides exceed 8 hitter slots: "
                f"primary={primary_size}, secondary={secondary_size}"
            )
        if primary_size <= 0 and secondary_size <= 0 and not fade:
            return
        pos_to_class = cfg.pos_to_class or {}

        import pulp as _pulp

        # ── Bucket players by team and class ─────────────────────────
        team_pitchers: Dict[str, List[int]] = {}
        team_hitters: Dict[str, List[int]] = {}
        pitcher_to_team: Dict[int, str] = {}
        team_to_game: Dict[str, Optional[str]] = {}

        for p in pool:
            team = (p.team_abbreviation or "").upper()
            if not team:
                continue
            primary_pos = (p.position or "").split("/")[0].strip().upper()
            cls = pos_to_class.get(primary_pos)
            if cls == "pitcher":
                team_pitchers.setdefault(team, []).append(p.player_id)
                pitcher_to_team[p.player_id] = team
            elif cls == "hitter":
                team_hitters.setdefault(team, []).append(p.player_id)
            # team_to_game pulls from any player on the team — pitchers
            # included, since they share game_id with their team-mates.
            if team not in team_to_game and p.game_id:
                team_to_game[team] = p.game_id

        game_to_teams: Dict[str, List[str]] = {}
        for t, g in team_to_game.items():
            if g:
                game_to_teams.setdefault(g, []).append(t)

        def _player_sum(pids: List[int]):
            """Sum of ``vars_by_player`` rows for the given player IDs.
            Returns 0 (literal int, valid in PuLP affine expressions) when
            the list is empty so callers don't need to special-case it."""
            if not pids:
                return 0
            return _pulp.lpSum(
                var for pid in pids
                for _j, var in vars_by_player.get(pid, [])
            )

        # ── (1) Pitcher fade ─────────────────────────────────────────
        if fade:
            BIG_M = 8  # max hitter slots in DK MLB Classic
            for pitcher_pid, p_team in pitcher_to_team.items():
                if pitcher_pid not in vars_by_player:
                    continue
                gid = team_to_game.get(p_team)
                if not gid:
                    continue
                opp_teams = [t for t in game_to_teams.get(gid, []) if t != p_team]
                if not opp_teams:
                    continue
                opp_hitter_pids: List[int] = []
                for ot in opp_teams:
                    opp_hitter_pids.extend(team_hitters.get(ot, []))
                opp_hitter_pids = [h for h in opp_hitter_pids if h in vars_by_player]
                if not opp_hitter_pids:
                    continue
                pitcher_var = _pulp.lpSum(
                    var for _j, var in vars_by_player[pitcher_pid]
                )
                opp_h_sum = _player_sum(opp_hitter_pids)
                # opp_h_sum + BIG_M * pitcher_var <= BIG_M
                # ⇔ pitcher_var = 1  ⇒  opp_h_sum <= 0
                prob += (
                    opp_h_sum + BIG_M * pitcher_var <= BIG_M,
                    f"mlb_fade_{pitcher_pid}",
                )

        # ── (2) Primary stack (hard) ─────────────────────────────────
        y_vars: Dict[str, Any] = {}
        if primary_size > 0:
            teams_with_hitters = [t for t, h in team_hitters.items() if h]
            if teams_with_hitters:
                for t in teams_with_hitters:
                    y_vars[t] = _pulp.LpVariable(
                        f"mlb_primary_{t}", cat="Binary",
                    )
                prob += (
                    _pulp.lpSum(y_vars.values()) >= 1,
                    "mlb_primary_select",
                )
                for t, y in y_vars.items():
                    hsum = _player_sum(team_hitters[t])
                    prob += (
                        hsum - primary_size * y >= 0,
                        f"mlb_primary_size_{t}",
                    )

        # ── (3) Secondary stack (soft via objective bonus) ───────────
        if secondary_size > 0 and primary_size > 0:
            teams_with_hitters = [t for t, h in team_hitters.items() if h]
            z_vars: Dict[str, Any] = {}
            for t in teams_with_hitters:
                z = _pulp.LpVariable(f"mlb_secondary_{t}", cat="Binary")
                z_vars[t] = z
                hsum = _player_sum(team_hitters[t])
                prob += (
                    hsum - secondary_size * z >= 0,
                    f"mlb_secondary_size_{t}",
                )
                # The secondary team can't be the primary team
                if t in y_vars:
                    prob += (
                        y_vars[t] + z <= 1,
                        f"mlb_primary_secondary_excl_{t}",
                    )
            if z_vars:
                # At most one team claims the secondary bonus — prevents
                # double-counting and keeps the bias precisely aimed at
                # 5-3 / 5-2 distributions.
                prob += (
                    _pulp.lpSum(z_vars.values()) <= 1,
                    "mlb_secondary_unique",
                )
                # Bonus added directly to the existing objective. The
                # value is calibrated against typical MLB hitter FP
                # (12–18) — small enough that the solver won't draft
                # demonstrably worse hitters just to claim it, large
                # enough to flip distributions when two configurations
                # are within ~2 FP. Empirically tracks 5-3 ≥ ~70% of
                # lineups on a typical 10-15-game slate.
                SECONDARY_BONUS = 2.0
                try:
                    prob.objective += SECONDARY_BONUS * _pulp.lpSum(
                        z_vars.values()
                    )
                except Exception:
                    # PuLP edge cases (e.g. objective set via setObjective
                    # with a non-additive expr) — silently skip the bias
                    # rather than break the solve.
                    pass

    # ------------------------------------------------------------------
    # ILP (Integer Linear Programming) optimizer — provably optimal
    # ------------------------------------------------------------------

    def _ilp_optimize(
        self,
        pool: List[PlayerPoolEntry],
        platform: str,
        salary_cap: int,
        slot_order: List[str],
        locked_player_ids: List[int],
        score_fn: Callable[[PlayerPoolEntry], float],
        salary_floor: int = 0,
        stack_game_id: Optional[str] = None,
        stack_primary_team: Optional[str] = None,
        stack_size: int = 0,
        bring_back: bool = False,
        mode: str = "classic",
        sport: str = "nba",
        warm_start_lineup: Optional[Dict[str, "PlayerPoolEntry"]] = None,
        warm_start_score: Optional[float] = None,
        contest_type: str = "gpp",
        time_limit: Optional[int] = None,
        min_projection_floor: Optional[float] = None,
        max_cumulative_ownership: Optional[float] = None,
        enable_stacking: bool = False,
        stack_overrides: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, PlayerPoolEntry]]:
        """Solve lineup optimization as an ILP using PuLP/CBC.

        Finds the mathematically optimal player-to-slot assignment that
        maximizes the sum of ``score_fn`` values subject to salary cap,
        position eligibility, uniqueness, lock/exclude, and optional
        stacking constraints.

        Args:
            time_limit: Override CBC solver timeout in seconds. When
                ``None``, falls back to ``ILP_CBC_TIME_LIMIT`` constant.

        Returns a dict ``{indexed_slot: PlayerPoolEntry}`` or ``None``
        if the solver fails (infeasible, timeout, or PuLP not installed).
        """
        if not _PULP_AVAILABLE or pulp is None:
            return None

        indexed_slots = _index_slots(slot_order)
        player_lookup = {p.player_id: p for p in pool}
        locked_set = set(locked_player_ids)

        # Pre-compute scores once (score_fn may include randomness)
        scores: Dict[int, float] = {}
        for p in pool:
            scores[p.player_id] = score_fn(p)

        # For showdown CPT, compute ceiling-weighted scores that maximize
        # upside — the 1.5× multiplier magnifies ceiling, making ceiling-
        # oriented player selection mathematically optimal for GPP.
        cpt_scores: Dict[int, float] = {}
        if mode == "showdown":
            from app.config.constants import (
                SHOWDOWN_CPT_CEILING_WEIGHT,
                SHOWDOWN_CPT_FLOOR_PENALTY,
            )
            for p in pool:
                ceiling = p.sim_p90 if p.sim_p90 else p.ceiling_fp
                # Blend: 75% ceiling + 25% projection, times CPT multiplier
                cpt_base = (
                    SHOWDOWN_CPT_CEILING_WEIGHT * ceiling
                    + (1.0 - SHOWDOWN_CPT_CEILING_WEIGHT) * p.projected_fp
                ) * CPT_MULTIPLIER
                # Penalize low-floor CPT candidates (high risk of bust)
                if p.sim_p10 and p.sim_p10 < p.projected_fp * 0.40:
                    cpt_base *= SHOWDOWN_CPT_FLOOR_PENALTY
                # Improvement #7: Contrarian captain — penalize chalk CPTs
                if p.estimated_ownership and p.estimated_ownership > 0:
                    from app.config.constants import (
                        SHOWDOWN_CPT_OWNERSHIP_ALPHA,
                        SHOWDOWN_CPT_OWNERSHIP_BASELINE,
                    )
                    _cpt_own_ratio = p.estimated_ownership / SHOWDOWN_CPT_OWNERSHIP_BASELINE
                    _cpt_leverage = 1.0 / (_cpt_own_ratio ** SHOWDOWN_CPT_OWNERSHIP_ALPHA)
                    _cpt_leverage = max(0.50, min(1.50, _cpt_leverage))
                    cpt_base *= _cpt_leverage
                cpt_scores[p.player_id] = cpt_base

        # ── Build the model ──────────────────────────────────────────
        import threading as _threading
        prob = pulp.LpProblem(f"DFS_{_threading.get_ident()}", pulp.LpMaximize)

        # Decision variables: x[(player_id, indexed_slot)] = Binary
        # Only create where position eligibility allows.
        x: Dict[Tuple[int, str], "pulp.LpVariable"] = {}

        # Pre-build indexes for efficient constraint creation
        vars_by_slot: Dict[str, List[Tuple[int, "pulp.LpVariable"]]] = {
            j: [] for j in indexed_slots
        }
        vars_by_player: Dict[int, List[Tuple[str, "pulp.LpVariable"]]] = {}

        for p in pool:
            for j in indexed_slots:
                base = _base_slot(j)
                elig = self._get_slot_eligible_positions(base, platform, sport)
                if self._player_matches_slot(p.position, elig):
                    var = pulp.LpVariable(
                        f"x_{p.player_id}_{j}", cat="Binary"
                    )
                    x[(p.player_id, j)] = var
                    vars_by_slot[j].append((p.player_id, var))
                    if p.player_id not in vars_by_player:
                        vars_by_player[p.player_id] = []
                    vars_by_player[p.player_id].append((j, var))

        if not x:
            return None

        # ── Warm start: set initial variable values from greedy lineup ──
        _warm_start_active = False
        if warm_start_lineup:
            warm_assignments = set()
            for isl, player in warm_start_lineup.items():
                warm_assignments.add((player.player_id, isl))
            for (pid, j), var in x.items():
                if (pid, j) in warm_assignments:
                    var.setInitialValue(1.0)
                else:
                    var.setInitialValue(0.0)
            _warm_start_active = True

        # ── Objective: maximize total composite score ─────────────────
        obj_terms = []
        for (pid, j), var in x.items():
            base = _base_slot(j)
            if mode == "showdown" and base == "CPT" and cpt_scores:
                # Use ceiling-weighted CPT score (already includes 1.5× mult)
                coeff = cpt_scores.get(pid, scores.get(pid, 0.0) * CPT_MULTIPLIER)
            elif mode == "showdown" and base == "CPT":
                coeff = scores.get(pid, 0.0) * CPT_MULTIPLIER
            else:
                coeff = scores.get(pid, 0.0)
            obj_terms.append(coeff * var)
        prob += pulp.lpSum(obj_terms)

        # ── C1: Salary cap ────────────────────────────────────────────
        prob += (
            pulp.lpSum(
                player_lookup[pid].salary * var
                for (pid, _j), var in x.items()
            ) <= salary_cap,
            "salary_cap",
        )

        # ── C2: Each slot filled exactly once ─────────────────────────
        for j in indexed_slots:
            slot_vars = vars_by_slot[j]
            if slot_vars:
                prob += (
                    pulp.lpSum(var for _, var in slot_vars) == 1,
                    f"fill_{j}",
                )

        # ── C3: Each player used at most once ─────────────────────────
        for pid, pv_list in vars_by_player.items():
            prob += (
                pulp.lpSum(var for _, var in pv_list) <= 1,
                f"uniq_{pid}",
            )

        # ── C5: Locked players must appear ────────────────────────────
        for locked_id in locked_set:
            if locked_id in vars_by_player:
                prob += (
                    pulp.lpSum(
                        var for _, var in vars_by_player[locked_id]
                    ) == 1,
                    f"lock_{locked_id}",
                )

        # ── C7: Salary floor (optional) ──────────────────────────────
        if salary_floor > 0:
            prob += (
                pulp.lpSum(
                    player_lookup[pid].salary * var
                    for (pid, _j), var in x.items()
                ) >= salary_floor,
                "salary_floor",
            )

        # ── C7b: Projection floor — optimality floor constraint ─────
        if min_projection_floor is not None and min_projection_floor > 0:
            prob += (
                pulp.lpSum(
                    player_lookup[pid].projected_fp * var
                    for (pid, _j), var in x.items()
                ) >= min_projection_floor,
                "projection_floor",
            )

        # ── C8: Stacking constraints (optional, GPP) ─────────────────
        if stack_game_id and stack_size > 0:
            primary_pids = set()
            opp_pids = set()
            for p in pool:
                if p.game_id == stack_game_id:
                    if (
                        stack_primary_team
                        and p.team_abbreviation
                        and p.team_abbreviation.upper()
                        == stack_primary_team.upper()
                    ):
                        primary_pids.add(p.player_id)
                    elif stack_primary_team:
                        opp_pids.add(p.player_id)

            # At least (stack_size - 1 if bring_back else stack_size)
            # from primary team
            if primary_pids:
                n_primary = max(1, stack_size - (1 if bring_back else 0))
                primary_vars = [
                    var
                    for (pid, _j), var in x.items()
                    if pid in primary_pids
                ]
                if primary_vars:
                    prob += (
                        pulp.lpSum(primary_vars) >= n_primary,
                        "stack_primary",
                    )

            # At least 1 from opposing team (bring-back)
            if bring_back and opp_pids:
                opp_vars = [
                    var
                    for (pid, _j), var in x.items()
                    if pid in opp_pids
                ]
                if opp_vars:
                    prob += (
                        pulp.lpSum(opp_vars) >= 1,
                        "stack_bringback",
                    )

        # ── C8b: NFL stacking rules (Prompt 1.6) ─────────────────────
        # NBA / CBB / MLB use the legacy game-stack constraints above
        # (stack_primary / stack_bringback). NFL adds position-aware
        # rules — QB must be paired with team's WR/TE within a min/max
        # band, plus a bring-back from the QB's opponent. Helper is a
        # no-op for non-NFL sports or when ``stack_rules`` is empty.
        # ``stack_overrides`` (Prompt 5.1) carries optional user-supplied
        # override values; absent keys fall back to SportConfig defaults.
        _ovr = stack_overrides or {}
        if enable_stacking:
            self._apply_nfl_stack_constraints(
                prob=prob,
                pool=pool,
                player_lookup=player_lookup,
                vars_by_player=vars_by_player,
                sport=sport,
                qb_min_override=_ovr.get("primary_stack_size"),
                require_bring_back_override=_ovr.get("require_bring_back"),
            )
            # ── C8c: MLB stacking + pitcher fade (Prompt 4.1) ────────
            # Strict pitcher fade (no hitter vs selected pitcher), strict
            # primary stack of 5, soft secondary-stack objective bonus.
            # No-op for non-MLB sports.
            self._apply_mlb_stack_constraints(
                prob=prob,
                pool=pool,
                player_lookup=player_lookup,
                vars_by_player=vars_by_player,
                sport=sport,
                primary_size_override=_ovr.get("primary_stack_size"),
                secondary_size_override=_ovr.get("secondary_stack_size"),
            )

        # ── C_CANNIBAL: Usage Cannibalization Penalties ─────────────────
        # Penalize high-USG teammate pairs in the objective function and
        # hard-cap same-team stacking at 3 players.
        #
        # For each pair of same-team players where BOTH have USG% > 22%,
        # introduce a penalty variable z_ij that activates when both are
        # selected: z_ij >= x_i + x_j - 1 (linearized AND).
        # The penalty subtracts from the objective: -PENALTY × z_ij.
        #
        # Exception: if Player A is a high-assist guard (AST/game >= 5.5)
        # and Player B is a PF/C with assisted_fg_pct >= 55%, the pair
        # gets a BONUS instead of a penalty (PG→Big pick-and-roll synergy).
        from app.config.constants import (
            CANNIBALIZATION_USG_THRESHOLD,
            CANNIBALIZATION_PENALTY_PER_PAIR,
            CANNIBALIZATION_MAX_SAME_TEAM,
            CANNIBALIZATION_ASSIST_PERCENTILE,
            CANNIBALIZATION_ASSISTED_FG_PCT,
            CANNIBALIZATION_SYNERGY_BONUS,
        )

        # Group players by team
        _team_players: Dict[str, List[int]] = {}
        for p in pool:
            team = (p.team_abbreviation or "").upper()
            if team:
                _team_players.setdefault(team, []).append(p.player_id)

        # Player "selected" indicator: s_pid = sum of all x[pid, *] vars
        # (equals 1 when the player is in the lineup, 0 otherwise)
        _player_selected: Dict[int, Any] = {}
        for pid, pv_list in vars_by_player.items():
            _player_selected[pid] = pulp.lpSum(var for _, var in pv_list)

        # ── Per-sport same-team stack cap (Prompt 2.2) ──────────────
        # Replaces the legacy NBA-tuned ``CANNIBALIZATION_MAX_SAME_TEAM = 3``
        # constant with values read from the SportConfig:
        #   NBA / CBB  → cap=3, all players counted
        #   NFL        → cap=5, all players counted (stack-friendly)
        #   MLB        → cap=5, only HITTERS counted (DK rule: pitchers
        #                are exempt from the 5-stack limit, and there
        #                are only 2 P slots anyway)
        from app.sports import get_config as _get_sport_cfg
        try:
            _cfg_team = _get_sport_cfg(sport)
            _team_cap = _cfg_team.max_same_team_count
            _team_cap_class = _cfg_team.team_stack_cap_class
            _pos_to_class = _cfg_team.pos_to_class or {}
        except Exception:
            _team_cap = CANNIBALIZATION_MAX_SAME_TEAM
            _team_cap_class = None
            _pos_to_class = {}

        def _player_counts_toward_team_cap(pid: int) -> bool:
            """When ``team_stack_cap_class`` is set, only players with
            that scoring class count toward the cap. Otherwise every
            player counts (legacy NBA/CBB behavior preserved)."""
            if not _team_cap_class:
                return True
            p = player_lookup.get(pid)
            if p is None:
                return True
            primary = (p.position or "").split("/")[0].strip().upper()
            return _pos_to_class.get(primary) == _team_cap_class

        for team, team_pids in _team_players.items():
            team_vars = [
                _player_selected[pid]
                for pid in team_pids
                if pid in _player_selected
                and _player_counts_toward_team_cap(pid)
            ]
            if len(team_vars) > _team_cap:
                prob += (
                    pulp.lpSum(team_vars) <= _team_cap,
                    f"max_team_{team}",
                )

        # Pairwise usage cannibalization penalties
        _cannibal_pairs = 0
        _synergy_pairs = 0
        for team, team_pids in _team_players.items():
            # Filter to high-usage players on this team
            high_usg = [
                pid for pid in team_pids
                if pid in player_lookup
                and (getattr(player_lookup[pid], "usage_rate", None) or 0) >= CANNIBALIZATION_USG_THRESHOLD
                and pid in _player_selected
            ]
            if len(high_usg) < 2:
                continue

            for i in range(len(high_usg)):
                for j in range(i + 1, len(high_usg)):
                    pid_a, pid_b = high_usg[i], high_usg[j]
                    p_a = player_lookup[pid_a]
                    p_b = player_lookup[pid_b]

                    # Check for PG→Big assist synergy exception
                    is_synergy = _check_assist_synergy(
                        p_a, p_b,
                        CANNIBALIZATION_ASSIST_PERCENTILE,
                        CANNIBALIZATION_ASSISTED_FG_PCT,
                    )

                    # Create z variable: z >= s_a + s_b - 1
                    # z is 1 when both players are selected, 0 otherwise
                    z = pulp.LpVariable(
                        f"z_cannibal_{pid_a}_{pid_b}",
                        lowBound=0, upBound=1, cat="Continuous",
                    )
                    prob += (
                        z >= _player_selected[pid_a] + _player_selected[pid_b] - 1,
                        f"cannibal_link_{pid_a}_{pid_b}",
                    )

                    if is_synergy:
                        # Positive synergy: bonus for PG→Big
                        obj_terms.append(CANNIBALIZATION_SYNERGY_BONUS * z)
                        _synergy_pairs += 1
                    else:
                        # Negative cannibalization: penalty
                        obj_terms.append(-CANNIBALIZATION_PENALTY_PER_PAIR * z)
                        _cannibal_pairs += 1

        if _cannibal_pairs or _synergy_pairs:
            # Re-set objective with penalty/bonus terms included
            prob.setObjective(pulp.lpSum(obj_terms))
            logger.debug(
                "[ILP] Usage cannibalization: %d penalty pairs, "
                "%d synergy pairs across %d teams",
                _cannibal_pairs, _synergy_pairs, len(_team_players),
            )

        # ── C9: GPP tournament constraints ─────────────────────────────
        is_gpp = contest_type in ("gpp", "single_entry")
        if is_gpp:
            from app.config.constants import (
                GPP_OWNERSHIP_CAP as _DEFAULT_OWN_CAP,
                GPP_PIVOT_OWNERSHIP_THRESHOLD as _DEFAULT_PIVOT,
                GPP_PIVOT_MIN_COUNT as _DEFAULT_PIVOT_MIN,
                GPP_CEILING_WEIGHT as _DEFAULT_CEIL_W,
                GPP_BRINGBACK_SALARY_THRESHOLD as _DEFAULT_BB_SAL,
                GPP_BRINGBACK_USAGE_THRESHOLD,
                GPP_BRINGBACK_ENABLED,
            )

            # Override with learned GPP blueprint values when available
            _cal = self.calibration_service
            GPP_OWNERSHIP_CAP = (
                _cal.get_gpp_ownership_cap() if _cal else None
            ) or _DEFAULT_OWN_CAP
            GPP_PIVOT_OWNERSHIP_THRESHOLD = (
                _cal.get_gpp_pivot_threshold() if _cal else None
            ) or _DEFAULT_PIVOT
            GPP_PIVOT_MIN_COUNT = (
                _cal.get_gpp_pivot_min_count() if _cal else None
            ) or _DEFAULT_PIVOT_MIN
            GPP_CEILING_WEIGHT = (
                _cal.get_gpp_ceiling_weight() if _cal else None
            ) or _DEFAULT_CEIL_W
            GPP_BRINGBACK_SALARY_THRESHOLD = (
                _cal.get_gpp_bringback_salary_threshold() if _cal else None
            ) or _DEFAULT_BB_SAL

            # C9a — Ceiling-tilted objective: blend score_fn with ceiling
            # Improvement #5: slate-size adaptive ceiling weight
            _eff_ceiling_weight = GPP_CEILING_WEIGHT
            if hasattr(self, '_slate_adjustments') and self._slate_adjustments:
                _eff_ceiling_weight *= self._slate_adjustments.get("ceiling_weight_mult", 1.0)
            if _eff_ceiling_weight > 0.0:
                ceiling_terms = []
                for (pid, j), var in x.items():
                    p = player_lookup[pid]
                    ceil_val = p.sim_p90 if p.sim_p90 else p.ceiling_fp
                    # NOTE: Ownership leverage intentionally NOT applied here.
                    # scores[pid] already includes ownership leverage from
                    # _compute_composite_score — re-applying would double-penalize.
                    base_coeff = scores.get(pid, 0.0)
                    # For showdown CPT slots, apply CPT multiplier to ceiling too
                    base_slot = _base_slot(j)
                    if mode == "showdown" and base_slot == "CPT":
                        if cpt_scores:
                            base_coeff = cpt_scores.get(pid, base_coeff)
                        ceil_val *= CPT_MULTIPLIER
                    blended = (
                        (1.0 - _eff_ceiling_weight) * base_coeff
                        + _eff_ceiling_weight * ceil_val
                    )
                    ceiling_terms.append(blended * var)
                # Replace the existing objective with the ceiling-blended one
                prob.setObjective(pulp.lpSum(ceiling_terms))

            # C9b (ownership cap) and C9c (pivot rule) REMOVED.
            # Ownership-based filtering is now handled by the Phase 3
            # quality scoring + portfolio selection, not by the solver.
            # This eliminates infeasible conflicts with stacking + ceiling
            # constraints that caused 100% greedy fallback.

            # C9d — Bring-back correlation rule
            # For each game that has a high-salary or high-usage "anchor",
            # if the anchor is selected the solver must also select at
            # least one player from the opposing team in the same game.
            #
            # Formulation (per-game indicator constraint):
            #   sum(opp_team_vars) >= anchor_selected
            # When anchor_selected=1, forces opp>=1.  When 0, trivially satisfied.
            if GPP_BRINGBACK_ENABLED and mode != "showdown":
                # Group players by game_id and team
                game_teams: Dict[str, Dict[str, List[int]]] = {}
                for p in pool:
                    if not p.game_id or p.player_id not in vars_by_player:
                        continue
                    gid = p.game_id
                    team = (p.team_abbreviation or "").upper()
                    if not team:
                        continue
                    if gid not in game_teams:
                        game_teams[gid] = {}
                    if team not in game_teams[gid]:
                        game_teams[gid][team] = []
                    game_teams[gid][team].append(p.player_id)

                # Identify anchor players (high salary or high usage)
                anchor_pids: Dict[str, set] = {}  # game_id -> set of anchor pids
                anchor_teams: Dict[int, str] = {}  # pid -> team
                for p in pool:
                    if not p.game_id or p.player_id not in vars_by_player:
                        continue
                    is_anchor = (
                        p.salary >= GPP_BRINGBACK_SALARY_THRESHOLD
                        or (
                            p.projected_stats
                            and p.projected_stats.get("usage_rate", 0) >= GPP_BRINGBACK_USAGE_THRESHOLD
                        )
                    )
                    if is_anchor:
                        gid = p.game_id
                        if gid not in anchor_pids:
                            anchor_pids[gid] = set()
                        anchor_pids[gid].add(p.player_id)
                        anchor_teams[p.player_id] = (p.team_abbreviation or "").upper()

                # Add per-anchor constraints
                for gid, anchors in anchor_pids.items():
                    teams_in_game = game_teams.get(gid, {})
                    if len(teams_in_game) < 2:
                        continue  # Need both teams present

                    for anchor_pid in anchors:
                        anchor_team = anchor_teams[anchor_pid]

                        # Collect all ILP vars for the opposing team in this game
                        opp_vars = []
                        for team, pids in teams_in_game.items():
                            if team == anchor_team:
                                continue
                            for opp_pid in pids:
                                if opp_pid in vars_by_player:
                                    for _, var in vars_by_player[opp_pid]:
                                        opp_vars.append(var)

                        if not opp_vars:
                            continue

                        # anchor_selected = sum of all slot assignments for this anchor
                        anchor_vars = [var for _, var in vars_by_player[anchor_pid]]

                        # Constraint: sum(opp) >= anchor_selected
                        # If anchor is in the lineup (anchor_selected=1), at least
                        # one opponent from the same game must also be selected.
                        prob += (
                            pulp.lpSum(opp_vars) >= pulp.lpSum(anchor_vars),
                            f"bringback_{gid}_{anchor_pid}",
                        )

        # ── C10: Cumulative ownership cap (user-specified) ──────────────
        # Hard constraint on the sum of all selected players' projected
        # ownership.  Separate from the GPP per-player cap (C9b) — this
        # limits TOTAL lineup chalkiness.  Auto-relaxation (10% per retry)
        # is handled by the caller (_build_single_lineup).
        if max_cumulative_ownership is not None and max_cumulative_ownership > 0:
            _cum_own_vars = [
                (pid, var)
                for (pid, _j), var in x.items()
                if player_lookup[pid].estimated_ownership is not None
            ]
            if _cum_own_vars:
                prob += (
                    pulp.lpSum(
                        player_lookup[pid].estimated_ownership * var
                        for pid, var in _cum_own_vars
                    ) <= max_cumulative_ownership,
                    "cumulative_ownership_cap",
                )

        # ── Solve ─────────────────────────────────────────────────────
        from app.config.constants import (
            ILP_CBC_TIME_LIMIT, ILP_CBC_PRESOLVE,
            ILP_CBC_GAP_REL, ILP_CBC_CUTOFF_ENABLED,
            ILP_CBC_CUTOFF_DISCOUNT,
        )

        cbc_options = []
        if (
            ILP_CBC_CUTOFF_ENABLED
            and warm_start_score is not None
            and warm_start_score > 0
        ):
            # PuLP converts Maximize(obj) → Minimize(-obj) internally.
            # CBC cutoff prunes nodes where internal_obj > cutoff.
            # So cutoff = -(greedy * discount) prunes branches with
            # original obj < greedy * discount.
            _cutoff_val = -(warm_start_score * ILP_CBC_CUTOFF_DISCOUNT)
            cbc_options.append(f"cutoff {_cutoff_val:.4f}")

        import warnings as _warnings
        with _warnings.catch_warnings():
            # PuLP on Windows warns that warmStart needs keepFiles;
            # empirically works fine without it.
            _warnings.filterwarnings(
                "ignore",
                message=".*warmStart requires keepFiles.*",
                category=UserWarning,
            )
            _effective_time_limit = time_limit if time_limit is not None else ILP_CBC_TIME_LIMIT
            solver = pulp.PULP_CBC_CMD(
                msg=0,
                timeLimit=_effective_time_limit,
                warmStart=_warm_start_active,
                presolve=ILP_CBC_PRESOLVE,
                gapRel=ILP_CBC_GAP_REL,
                options=cbc_options or None,
            )
            try:
                status = prob.solve(solver)
            except Exception as e:
                logger.warning(f"[ILP] CBC solver error: {e}")
                return None

        # Accept optimal OR any integer-feasible incumbent (timeout/gap).
        # Callers verify ilp_score > greedy_score before replacing greedy,
        # so a suboptimal incumbent is never accepted over a better greedy.
        if status == pulp.constants.LpStatusOptimal:
            pass  # Provably optimal
        elif prob.sol_status == pulp.constants.LpSolutionIntegerFeasible:
            logger.info(
                "[ILP] Feasible incumbent accepted "
                f"(obj={pulp.value(prob.objective):.2f})"
            )
        else:
            logger.warning(
                f"[ILP] Non-optimal status: "
                f"{pulp.LpStatus.get(status, status)}, "
                f"sol_status={prob.sol_status}, "
                f"time_limit={_effective_time_limit}s, "
                f"pool={len(pool)}, "
                f"projection_floor={min_projection_floor}"
            )
            return None

        # ── Extract solution ──────────────────────────────────────────
        result: Dict[str, PlayerPoolEntry] = {}
        for (pid, j), var in x.items():
            if var.varValue is not None and var.varValue > 0.5:
                result[j] = player_lookup[pid]

        if len(result) != len(indexed_slots):
            logger.warning(
                f"[ILP] Incomplete solution: {len(result)}/{len(indexed_slots)} slots"
            )
            return None

        # ── Salary cap verification ──────────────────────────────────
        _ilp_total_salary = sum(p.salary for p in result.values())
        if _ilp_total_salary > salary_cap:
            logger.error(
                f"[ILP] SALARY CAP VIOLATION: ${_ilp_total_salary:,} > "
                f"${salary_cap:,} — solver returned infeasible solution! "
                f"Discarding ILP result."
            )
            return None

        # ── Projection floor post-verification ──────────────────────
        # Belt-and-suspenders: even if the solver says "Optimal", verify
        # the projection floor constraint is actually satisfied.  This
        # catches edge cases where CBC returns a near-feasible incumbent
        # that slightly violates the constraint (floating-point).
        if min_projection_floor is not None and min_projection_floor > 0:
            _ilp_proj_total = sum(
                player_lookup[p.player_id].projected_fp
                for p in result.values()
            )
            # Allow 0.1 FP tolerance for floating-point imprecision
            if _ilp_proj_total < min_projection_floor - 0.1:
                logger.warning(
                    f"[ILP] PROJECTION FLOOR VIOLATION: "
                    f"{_ilp_proj_total:.1f} < {min_projection_floor:.1f} "
                    f"— solver returned sub-floor solution! "
                    f"Discarding ILP result."
                )
                return None

        return result

    # ------------------------------------------------------------------
    # K-Best iterative ILP — build base problem (no solve)
    # ------------------------------------------------------------------

    def _build_kbest_prob(
        self,
        pool: List[PlayerPoolEntry],
        scores: Dict[int, float],
        platform: str,
        salary_cap: int,
        slot_order: List[str],
        locked_player_ids: List[int],
        stack_game_id: Optional[str],
        stack_primary_team: Optional[str],
        stack_size: int,
        bring_back: bool,
        mode: str,
        sport: str,
        contest_type: str,
        stack_overrides: Optional[Dict[str, Any]] = None,
    ) -> Optional[Tuple]:
        """Build a PuLP problem for K-Best iterative solving.

        Creates all decision variables and constraints (C1 salary, C2 slot
        fill, C3 uniqueness, C5 locks, C8 stacking, C9a ceiling blend,
        C9d bring-back) but does NOT solve.  The caller solves iteratively,
        appending exclusion constraints between solves.

        Args:
            scores: Pre-computed ``{player_id: composite_score}`` dict
                    (already includes Gaussian noise for this seed).

        Returns:
            ``(prob, x, player_lookup, indexed_slots)`` or ``None`` if
            no decision variables could be created.
        """
        import pulp
        import threading as _threading

        indexed_slots = _index_slots(slot_order)

        # Player lookup by ID for constraint building
        player_lookup: Dict[int, PlayerPoolEntry] = {
            p.player_id: p for p in pool
        }

        # ── Decision variables ──────────────────────────────────────
        prob = pulp.LpProblem(
            f"KBest_{_threading.get_ident()}", pulp.LpMaximize,
        )

        x: Dict[Tuple[int, str], "pulp.LpVariable"] = {}
        vars_by_slot: Dict[str, List[Tuple[int, "pulp.LpVariable"]]] = {
            j: [] for j in indexed_slots
        }
        vars_by_player: Dict[int, List[Tuple[str, "pulp.LpVariable"]]] = {}

        for p in pool:
            for j in indexed_slots:
                base = _base_slot(j)
                elig = self._get_slot_eligible_positions(
                    base, platform, sport,
                )
                if self._player_matches_slot(p.position, elig):
                    var = pulp.LpVariable(
                        f"x_{p.player_id}_{j}", cat="Binary",
                    )
                    x[(p.player_id, j)] = var
                    vars_by_slot[j].append((p.player_id, var))
                    if p.player_id not in vars_by_player:
                        vars_by_player[p.player_id] = []
                    vars_by_player[p.player_id].append((j, var))

        if not x:
            return None

        # ── Objective: maximise total composite score ───────────────
        obj_terms = []
        for (pid, j), var in x.items():
            coeff = scores.get(pid, 0.0)
            obj_terms.append(coeff * var)
        prob += pulp.lpSum(obj_terms)

        # ── C1 — Salary cap ────────────────────────────────────────
        prob += (
            pulp.lpSum(
                player_lookup[pid].salary * var
                for (pid, _j), var in x.items()
            ) <= salary_cap,
            "salary_cap",
        )

        # ── C1b — Salary floor (minimum spend) ────────────────────
        # Prevent the solver from returning cheap lineups that leave
        # too much salary on the table.
        from app.config.constants import MIN_SALARY_FLOOR
        prob += (
            pulp.lpSum(
                player_lookup[pid].salary * var
                for (pid, _j), var in x.items()
            ) >= MIN_SALARY_FLOOR,
            "salary_floor",
        )

        # ── C2 — Slot fill (each slot exactly 1 player) ───────────
        for j in indexed_slots:
            slot_vars = vars_by_slot[j]
            if slot_vars:
                prob += (
                    pulp.lpSum(var for _, var in slot_vars) == 1,
                    f"fill_{j}",
                )

        # ── C3 — Player uniqueness (each player at most 1 slot) ───
        for pid, pv_list in vars_by_player.items():
            prob += (
                pulp.lpSum(var for _, var in pv_list) <= 1,
                f"uniq_{pid}",
            )

        # ── C5 — Locked player enforcement ─────────────────────────
        if locked_player_ids:
            for lpid in locked_player_ids:
                if lpid in vars_by_player:
                    prob += (
                        pulp.lpSum(
                            var for _, var in vars_by_player[lpid]
                        ) == 1,
                        f"lock_{lpid}",
                    )

        # ── C8 — Stacking constraints ──────────────────────────────
        if stack_game_id and stack_size > 0 and stack_primary_team:
            primary_vars = []
            opp_vars = []
            for (pid, j), var in x.items():
                p = player_lookup[pid]
                if p.game_id == stack_game_id:
                    if (
                        p.team_abbreviation
                        and p.team_abbreviation.upper() == stack_primary_team.upper()
                    ):
                        primary_vars.append(var)
                    else:
                        opp_vars.append(var)

            n_primary = max(1, stack_size - (1 if bring_back else 0))
            if primary_vars:
                prob += (
                    pulp.lpSum(primary_vars) >= n_primary,
                    "stack_primary",
                )
            if bring_back and opp_vars:
                prob += (
                    pulp.lpSum(opp_vars) >= 1,
                    "stack_bringback",
                )

        # ── C8a — NFL stacking rules (Prompt 1.6) ──────────────────
        # No-op for non-NFL sports / empty rules. K-Best is invoked
        # only on the GPP/stacking path so we always apply when sport=nfl.
        # Prompt 5.1: route per-request overrides to the helpers.
        _ovr = stack_overrides or {}
        self._apply_nfl_stack_constraints(
            prob=prob,
            pool=pool,
            player_lookup=player_lookup,
            vars_by_player=vars_by_player,
            sport=sport,
            qb_min_override=_ovr.get("primary_stack_size"),
            require_bring_back_override=_ovr.get("require_bring_back"),
        )

        # ── C8a' — MLB stacking + pitcher fade (Prompt 4.1) ──────
        # Strict pitcher fade + strict primary 5-stack, soft secondary
        # bonus. No-op for non-MLB sports.
        self._apply_mlb_stack_constraints(
            prob=prob,
            pool=pool,
            player_lookup=player_lookup,
            vars_by_player=vars_by_player,
            sport=sport,
            primary_size_override=_ovr.get("primary_stack_size"),
            secondary_size_override=_ovr.get("secondary_stack_size"),
        )

        # ── C8b — Minimum stud inclusion (GPP only) ─────────────────
        # Force at least 1 player with salary >= $9000 to prevent the
        # optimizer from building all-midrange lineups.
        if contest_type == "gpp" and mode != "showdown" and sport == "nba":
            _STUD_SALARY_THRESHOLD = 9000
            stud_vars = []
            for (pid, j), var in x.items():
                p = player_lookup[pid]
                if p.salary >= _STUD_SALARY_THRESHOLD:
                    stud_vars.append(var)
            if stud_vars:
                prob += (
                    pulp.lpSum(stud_vars) >= 1,
                    "min_stud",
                )

        # ── C9a — Ceiling-blended objective (GPP only) ─────────────
        if contest_type == "gpp" and mode != "showdown":
            from app.config.constants import GPP_CEILING_WEIGHT
            # Improvement #5: slate-size adaptive ceiling weight
            _kb_ceiling_w = GPP_CEILING_WEIGHT
            if hasattr(self, '_slate_adjustments') and self._slate_adjustments:
                _kb_ceiling_w *= self._slate_adjustments.get("ceiling_weight_mult", 1.0)
            if _kb_ceiling_w and _kb_ceiling_w > 0:
                ceiling_terms = []
                for (pid, j), var in x.items():
                    p = player_lookup[pid]
                    base_coeff = scores.get(pid, 0.0)
                    ceil_val = (
                        p.sim_p90 if p.sim_p90 else p.ceiling_fp
                    )
                    # NOTE: Ownership leverage intentionally NOT applied here.
                    # scores[pid] already includes ownership leverage from
                    # _compute_composite_score — re-applying would double-penalize.
                    blended = (
                        (1.0 - _kb_ceiling_w) * base_coeff
                        + _kb_ceiling_w * ceil_val
                    )
                    ceiling_terms.append(blended * var)
                prob.setObjective(pulp.lpSum(ceiling_terms))

        # C9b/C9c intentionally omitted — ownership handled by Phase 3

        # ── C9d — Bring-back correlation rule ──────────────────────
        if contest_type == "gpp" and mode != "showdown":
            from app.config.constants import (
                GPP_BRINGBACK_ENABLED,
                GPP_BRINGBACK_SALARY_THRESHOLD,
            )
            if GPP_BRINGBACK_ENABLED:
                game_teams: Dict[str, Dict[str, List[int]]] = {}
                for p in pool:
                    if not p.game_id or p.player_id not in vars_by_player:
                        continue
                    gid = p.game_id
                    team = (p.team_abbreviation or "").upper()
                    if gid not in game_teams:
                        game_teams[gid] = {}
                    if team not in game_teams[gid]:
                        game_teams[gid][team] = []
                    game_teams[gid][team].append(p.player_id)

                for gid, teams_dict in game_teams.items():
                    team_list = list(teams_dict.keys())
                    if len(team_list) < 2:
                        continue
                    for anchor_team in team_list:
                        opp_team = [
                            t for t in team_list if t != anchor_team
                        ]
                        if not opp_team:
                            continue
                        anchor_pids = [
                            pid for pid in teams_dict[anchor_team]
                            if player_lookup[pid].salary
                            >= GPP_BRINGBACK_SALARY_THRESHOLD
                        ]
                        if not anchor_pids:
                            continue
                        opp_all_pids = []
                        for ot in opp_team:
                            opp_all_pids.extend(teams_dict[ot])
                        opp_cvars = [
                            var
                            for (pid, _j), var in x.items()
                            if pid in opp_all_pids
                        ]
                        if not opp_cvars:
                            continue
                        for apid in anchor_pids:
                            a_vars = [
                                var
                                for _, var in vars_by_player.get(apid, [])
                            ]
                            if a_vars:
                                prob += (
                                    pulp.lpSum(opp_cvars)
                                    >= pulp.lpSum(a_vars),
                                    f"bb_{gid}_{apid}",
                                )

        # ── Capture effective per-player coefficients ─────────────
        # If C9a ceiling blend was applied, `base_coeffs` reflects the
        # blended value; otherwise it's the raw Gaussian-noised score.
        # The exposure penalty helper multiplies these by a decay factor.
        base_coeffs: Dict[int, float] = dict(scores)
        if contest_type == "gpp" and mode != "showdown":
            from app.config.constants import GPP_CEILING_WEIGHT as _cw
            if _cw and _cw > 0:
                for pid in player_lookup:
                    if pid in scores:
                        p = player_lookup[pid]
                        ceil_val = p.sim_p90 if p.sim_p90 else p.ceiling_fp
                        base_coeffs[pid] = (
                            (1.0 - _cw) * scores[pid]
                            + _cw * ceil_val
                        )

        return (prob, x, player_lookup, indexed_slots, base_coeffs)

    # ------------------------------------------------------------------
    # Exposure-aware objective penalty
    # ------------------------------------------------------------------

    @staticmethod
    def _apply_exposure_penalties(
        prob,  # pulp.LpProblem
        x: Dict[Tuple[int, str], Any],
        base_coeffs: Dict[int, float],
        shared_counts: "_SharedExposureCounts",
        n_requested: int,
        max_exposure: float,
        locked_player_ids: List[int],
        player_max_exposure: Optional[Dict[int, float]] = None,
        kbest_iter: int = 0,
        novelty_deltas: Optional[Dict[int, float]] = None,
        elite_core_pids: Optional[set] = None,
    ) -> int:
        """Rewrite PuLP objective with quadratic exposure penalties in-place.

        For each player *i*:

        1. ``current_exp = draft_count[i] / n_requested``
        2. If ``current_exp >= eff_max`` → ``x_i.upBound = 0`` (hard lockout)
        3. Else → ``coeff_i *= 1.0 - (current_exp / eff_max)**2``  (quadratic decay)

        The penalised objective is set via ``prob.setObjective()`` which
        overwrites the objective in memory without rebuilding the
        constraint matrix.

        Parameters
        ----------
        prob : pulp.LpProblem
            The current K-Best ILP problem (modified in-place).
        x : dict
            Decision variables ``{(player_id, indexed_slot): LpVariable}``.
        base_coeffs : dict
            Effective per-player coefficient after ceiling blend.
        shared_counts : _SharedExposureCounts
            Thread-safe draft counts shared across workers.
        n_requested : int
            Final portfolio size (denominator for exposure %).
        max_exposure : float
            Global exposure cap (0.0–1.0).
        locked_player_ids : list
            Player IDs that are force-locked (exempt from penalty).
        player_max_exposure : dict, optional
            Per-player fractional caps ``{player_id: float}``.

        Returns
        -------
        int
            Number of unique players locked out this call.
        """
        import pulp as _pulp

        locked_set = set(locked_player_ids or [])
        snap = shared_counts.snapshot()
        per_caps = player_max_exposure or {}

        lockout_count = 0
        seen_lockouts: set = set()

        # ── Additive novelty for undrafted players ──────────────────
        _deltas = novelty_deltas or {}

        new_terms = []
        for (pid, j), var in x.items():
            base = base_coeffs.get(pid, 0.0)

            # Locked players are exempt — C5 forces sum(vars) == 1
            if pid in locked_set:
                new_terms.append(base * var)
                continue

            curr_count = snap.get(pid, 0)
            curr_exp = curr_count / max(n_requested, 1)
            eff_max = per_caps.get(pid, max_exposure)

            # ABSOLUTE GLOBAL CEILING: enforce regardless of tier/elite status.
            # This is the final safety net — no player exceeds this cap.
            from app.config.constants import ABSOLUTE_GLOBAL_MAX_EXPOSURE
            eff_max = min(eff_max, ABSOLUTE_GLOBAL_MAX_EXPOSURE)

            # Elite Core: exempt from quadratic decay (they keep full
            # coefficient until lockout) but NOT exempt from hard lockout.
            # Even elite plays must respect ABSOLUTE_GLOBAL_MAX_EXPOSURE.
            _is_elite = bool(elite_core_pids and pid in elite_core_pids)

            if curr_exp >= eff_max:
                # Hard lockout — force variable to 0 (applies to ALL players)
                var.upBound = 0
                if pid not in seen_lockouts:
                    seen_lockouts.add(pid)
                    lockout_count += 1
                new_terms.append(0.0 * var)
            else:
                # Reset upBound in case it was locked in a prior iteration
                var.upBound = 1
                ratio = curr_exp / max(eff_max, 0.01)
                if _is_elite:
                    penalty_factor = 1.0
                else:
                    penalty_factor = 1.0 - ratio ** 2

                # Additive novelty: undrafted players get a flat FP
                # boost + variance spike; instantly reverts once
                # drafted (count >= 1).
                # Safety gate: only apply novelty if base score exceeds
                # NOVELTY_MIN_BASE_SCORE to prevent inflating fringe
                # 12-15 FP bench players into lineup consideration.
                from app.config.constants import NOVELTY_MIN_BASE_SCORE
                if (
                    curr_count == 0
                    and pid in _deltas
                    and base > NOVELTY_MIN_BASE_SCORE
                ):
                    effective = max(0.01, base + _deltas[pid]) * penalty_factor
                else:
                    effective = base * penalty_factor

                new_terms.append(effective * var)

        prob.setObjective(_pulp.lpSum(new_terms))
        return lockout_count

    # ------------------------------------------------------------------
    # K-Best iterative ILP — generate candidates for a single stack
    # ------------------------------------------------------------------

    def _kbest_generate_for_stack(
        self,
        pool: List[PlayerPoolEntry],
        platform: str,
        salary_cap: int,
        roster_slots: List[str],
        slot_order: List[str],
        locked_player_ids: List[int],
        stack_config: Dict,
        strategy: str,
        contest_type: str,
        sport: str,
        mode: str,
        target_count: int,
        max_overlap: int,
        time_budget: float,
        master_seed: int,
        min_projected_fp: Optional[float],
        salary_floor_pct: float,
        # Exposure-aware generation parameters
        shared_exposure: Optional["_SharedExposureCounts"] = None,
        n_requested_final: int = 20,
        max_exposure: float = 0.55,
        player_max_exposure: Optional[Dict[int, float]] = None,
        # Per-request stacking overrides (Prompt 5.1)
        stack_overrides: Optional[Dict[str, Any]] = None,
    ) -> List:
        """K-Best iterative ILP solver for a single stack target.

        Builds the PuLP ``prob`` once per Gaussian noise seed, then
        iteratively solves and appends exclusion constraints until the
        solver returns Infeasible (all combinations with <= ``max_overlap``
        shared players exhausted) or the target count is reached.

        When Infeasible, bumps the noise seed, redraws Gaussian scores,
        rebuilds the base matrix, and starts a new K-Best loop.

        Args:
            stack_config: Dict with keys ``game_id``, ``primary_team``,
                ``size``, ``bring_back``.
            max_overlap: Maximum number of shared players between any
                two lineups produced in the same K-Best inner loop.
            time_budget: Wall-clock seconds allocated to this stack target.
            master_seed: Base seed for reproducible Gaussian noise.

        Returns:
            List of ``OptimizedLineup`` objects.
        """
        import pulp
        from app.config.constants import (
            ILP_CBC_TIME_LIMIT,
            ILP_CBC_PRESOLVE,
            ILP_CBC_GAP_REL,
            KBEST_MAX_NOISE_SEEDS,
            KBEST_PROJECTION_FLOOR,
            KBEST_PROJECTION_FLOOR_VALUE_EXEMPT,
            ELITE_CORE_VALUE_THRESHOLD,
            OVERSAMPLE_TARGET,
            OVERSAMPLE_NOISE_SEEDS,
            OVERSAMPLE_DEDUP,
        )

        # ── Pool pruning: strict projection floor ─────────────────
        # Remove low-FP bench fillers from the feasible region unless
        # they are elite salary-saving punts (high FP/$1K).
        # Locked players are always kept regardless of projection.
        #
        # Sport-aware thresholds (Prompt 7.13) — same rationale as
        # the upstream Phase 1 prune: the NBA-tuned 20 FP / 5.0x
        # constants would prune 100% of an MLB pool (hitters project
        # 8–14 FP at $3K–$5K, value ratio 2–3x). Each sport gets its
        # own floor / value pair.
        _SPORT_KB_THRESHOLDS = {
            "nba": (KBEST_PROJECTION_FLOOR,        KBEST_PROJECTION_FLOOR_VALUE_EXEMPT),
            "cbb": (KBEST_PROJECTION_FLOOR,        KBEST_PROJECTION_FLOOR_VALUE_EXEMPT),
            "mlb": (5.0,  1.5),
            "nfl": (5.0,  1.5),
        }
        _kb_floor, _kb_value = _SPORT_KB_THRESHOLDS.get(
            sport, (KBEST_PROJECTION_FLOOR, KBEST_PROJECTION_FLOOR_VALUE_EXEMPT),
        )
        _locked_set = set(locked_player_ids or [])
        _orig_pool_size = len(pool)
        pool = [
            p for p in pool
            if p.player_id in _locked_set
            or p.projected_fp >= _kb_floor
            or (
                p.salary and p.salary > 0 and p.projected_fp
                and (p.projected_fp / (p.salary / 1000)) > _kb_value
            )
        ]
        _pruned = _orig_pool_size - len(pool)
        if _pruned > 0:
            logger.info(
                f"[KBest] Pool pruned ({sport}): {_orig_pool_size} → {len(pool)} "
                f"({_pruned} below {_kb_floor:.0f} FP / {_kb_value:.1f}x value)"
            )

        # ── Identify "Elite Core" — free squares for overlap ──────
        # Players with value_ratio > ELITE_CORE_VALUE_THRESHOLD are
        # excluded from the K-Best overlap exclusion constraint.
        # This lets elite chalk (e.g., Kam Jones at 7.77x) appear in
        # every lineup without consuming overlap budget.
        elite_core_pids: set = set()
        for p in pool:
            if (
                p.salary and p.salary > 0
                and p.projected_fp and p.projected_fp > 0
            ):
                vr = p.projected_fp / (p.salary / 1000)
                if vr > ELITE_CORE_VALUE_THRESHOLD:
                    elite_core_pids.add(p.player_id)
        if elite_core_pids:
            _ec_names = [
                p.player_name for p in pool
                if p.player_id in elite_core_pids
            ]
            logger.info(
                f"[KBest] Elite Core (overlap-exempt): "
                f"{', '.join(_ec_names)} "
                f"({len(elite_core_pids)} players, "
                f"threshold={ELITE_CORE_VALUE_THRESHOLD:.1f}x)"
            )

        generated: List = []
        noise_seed = 0
        start_time = time.time()
        _seen_fingerprints: set = set()  # For oversampling dedup

        # Oversampling mode: target >= OVERSAMPLE_TARGET → 1 lineup per
        # noise seed, no exclusion constraints, many more seeds.
        _oversample_mode = target_count >= OVERSAMPLE_TARGET

        # Adaptive noise seed limit: scale with target count so we
        # generate enough structurally-diverse candidates.  Each seed
        # produces a different scoring perspective, yielding different
        # optimal player selections.
        if _oversample_mode:
            adaptive_max_seeds = OVERSAMPLE_NOISE_SEEDS
        else:
            adaptive_max_seeds = min(
                KBEST_MAX_NOISE_SEEDS,
                max(5, target_count // 8 + 3),
            )

        while (
            len(generated) < target_count
            and noise_seed < adaptive_max_seeds
        ):
            elapsed = time.time() - start_time
            if elapsed >= time_budget:
                break

            # ── 1. New RNG from (master_seed + noise_seed) ──────────
            rng = random.Random(master_seed + noise_seed)

            # ── 2. Pre-compute Gaussian-noised scores for all players
            # Improvement #3: Set secondary stack game for this batch
            self._secondary_stack_game_id = stack_config.get(
                "secondary_game_id"
            )
            scores: Dict[int, float] = {}
            for p in pool:
                sc = self._compute_composite_score(
                    p, strategy, {}, 0, rng,
                    contest_type=contest_type, sport=sport,
                )
                scores[p.player_id] = sc

            # ── 2b. Pre-compute novelty deltas for undrafted boost ──
            # Each undrafted player gets delta = ADDITIVE_FP + gauss(0, sigma * (SPIKE - 1))
            # The flat +3.5 shifts the mean up; the variance spike widens the spread.
            # Computed once per seed, applied per iteration in _apply_exposure_penalties.
            from app.config.constants import (
                NOVELTY_ADDITIVE_FP,
                NOVELTY_VARIANCE_SPIKE_MULT,
                NOVELTY_ENABLED,
                KBEST_OVERLAP_WINDOW,
                VARIANCE_SCALE_LOW_SALARY_THRESHOLD,
                VARIANCE_SCALE_LOW_SALARY_MULT,
                VARIANCE_SCALE_LOW_CONFIDENCE_MULT,
                VARIANCE_SCALE_MAX_COMBINED,
            )
            novelty_deltas: Dict[int, float] = {}
            if NOVELTY_ENABLED:
                for p in pool:
                    _sc = scores.get(p.player_id, 0.0)
                    # Recompute sigma using same logic as _compute_composite_score
                    if p.projected_fp and p.projected_fp > 0 and p.sim_std:
                        _n_sigma = p.sim_std * (_sc / p.projected_fp)
                    else:
                        _n_sigma = _sc * 0.10
                    # Apply same dynamic variance scaling
                    _n_vs = 1.0
                    if p.salary and p.salary < VARIANCE_SCALE_LOW_SALARY_THRESHOLD:
                        _n_vs *= VARIANCE_SCALE_LOW_SALARY_MULT
                    if p.rotation_confidence < 0.75:
                        _n_vs *= VARIANCE_SCALE_LOW_CONFIDENCE_MULT
                    _n_sigma *= min(_n_vs, VARIANCE_SCALE_MAX_COMBINED)
                    # Spike-only component: extra sigma beyond normal
                    _spike_sigma = _n_sigma * (NOVELTY_VARIANCE_SPIKE_MULT - 1.0)
                    _spike_draw = (
                        rng.gauss(0, _spike_sigma) if _spike_sigma > 0 else 0.0
                    )
                    novelty_deltas[p.player_id] = NOVELTY_ADDITIVE_FP + _spike_draw

            # ── 3. Build base ILP prob (once per noise seed) ────────
            build_result = self._build_kbest_prob(
                pool, scores, platform, salary_cap,
                slot_order, locked_player_ids,
                stack_config.get("game_id"),
                stack_config.get("primary_team"),
                stack_config.get("size", 0),
                stack_config.get("bring_back", False),
                mode, sport, contest_type,
                stack_overrides=stack_overrides,
            )
            if build_result is None:
                noise_seed += 1
                continue
            prob, x, player_lookup, indexed_slots, base_coeffs = build_result

            # ── 4. K-Best inner loop ────────────────────────────────
            kbest_iter = 0
            _excl_names: List[str] = []  # Track constraint names for rolling window
            while len(generated) < target_count:
                elapsed = time.time() - start_time
                if elapsed >= time_budget:
                    break

                remaining = max(2, int(time_budget - elapsed))
                _tl = min(ILP_CBC_TIME_LIMIT, remaining)
                solver = pulp.PULP_CBC_CMD(
                    msg=0,
                    timeLimit=_tl,
                    presolve=ILP_CBC_PRESOLVE,
                    gapRel=ILP_CBC_GAP_REL,
                )

                # ── Exposure-aware objective update ──────────────
                if shared_exposure is not None:
                    _lockouts = self._apply_exposure_penalties(
                        prob, x, base_coeffs,
                        shared_exposure, n_requested_final,
                        max_exposure, locked_player_ids,
                        player_max_exposure,
                        kbest_iter=kbest_iter,
                        novelty_deltas=novelty_deltas,
                        elite_core_pids=elite_core_pids,
                    )
                    if _lockouts > 0:
                        logger.debug(
                            f"[KBest] Exposure penalties: {_lockouts} "
                            f"players locked out (seed={noise_seed}, "
                            f"iter={kbest_iter})"
                        )

                try:
                    status = prob.solve(solver)
                except Exception as e:
                    logger.warning(
                        f"[KBest] CBC solver error (seed={noise_seed}, "
                        f"iter={kbest_iter}): {e}"
                    )
                    break

                # Accept optimal or integer-feasible incumbent
                if status != pulp.constants.LpStatusOptimal:
                    if (
                        prob.sol_status
                        != pulp.constants.LpSolutionIntegerFeasible
                    ):
                        logger.debug(
                            f"[KBest] Infeasible after {kbest_iter} "
                            f"iterations (seed={noise_seed}) -- "
                            f"exhausted overlap combinations"
                        )
                        break  # New noise seed

                # Extract {indexed_slot: PlayerPoolEntry}
                lineup_dict: Dict[str, PlayerPoolEntry] = {}
                for (pid, j), var in x.items():
                    if var.varValue is not None and var.varValue > 0.5:
                        lineup_dict[j] = player_lookup[pid]
                if len(lineup_dict) != len(indexed_slots):
                    logger.warning(
                        f"[KBest] Incomplete solution: "
                        f"{len(lineup_dict)}/{len(indexed_slots)} "
                        f"(seed={noise_seed}, iter={kbest_iter})"
                    )
                    break

                # Salary cap verification
                total_sal = sum(p.salary for p in lineup_dict.values())
                if total_sal > salary_cap:
                    logger.warning(
                        f"[KBest] Salary cap violation: "
                        f"${total_sal:,} > ${salary_cap:,}"
                    )
                    break

                # Convert to OptimizedLineup
                opt = self._dict_to_optimized_lineup(
                    lineup_dict, platform, sport, salary_cap,
                    roster_slots, ilp_used=True,
                )
                if opt is None:
                    break

                # Oversampling dedup: skip exact duplicate lineups
                if _oversample_mode and OVERSAMPLE_DEDUP:
                    _fp = frozenset(p.player_id for p in opt.players)
                    if _fp in _seen_fingerprints:
                        logger.debug(
                            f"[KBest] Duplicate lineup skipped "
                            f"(seed={noise_seed})"
                        )
                        kbest_iter += 1
                        break  # Next noise seed will redraw scores
                    _seen_fingerprints.add(_fp)

                # Quality gate — clamp salary pct to MIN_SALARY_FLOOR
                from app.config.constants import MIN_SALARY_FLOOR as _MSF
                _kb_min_sal_pct = max(
                    salary_floor_pct * 0.90,
                    _MSF / salary_cap if salary_cap > 0 else 0.99,
                )
                if self._passes_quality_gate(
                    opt, salary_cap,
                    expected_players=len(roster_slots),
                    min_salary_pct=_kb_min_sal_pct,
                    min_projected_fp=min_projected_fp,
                ):
                    generated.append(opt)
                    # Update shared exposure for cross-worker visibility
                    if shared_exposure is not None:
                        shared_exposure.increment_batch(
                            [p.player_id for p in opt.players]
                        )
                else:
                    logger.debug(
                        f"[KBest] Lineup rejected by quality gate "
                        f"(seed={noise_seed}, iter={kbest_iter})"
                    )

                # ── Exclusion constraint handling ──────────────────────
                if _oversample_mode:
                    # Oversampling: NO overlap exclusion constraints.
                    # Diversity comes entirely from Monte Carlo noise
                    # injection (new Gaussian draw each seed).  Phase 4
                    # Portfolio ILP handles strict overlap curation.
                    # Break to next noise seed (1 lineup per seed).
                    kbest_iter += 1
                    break
                else:
                    # Legacy K-Best: exclusion with rolling window.
                    # Prevent the solver from returning the same (or
                    # nearly same) lineup.  The constraint says: of the
                    # 8 variables that were selected, at most
                    # ``max_overlap`` may be 1 in the next solution.
                    # Elite Core players are "free squares" — excluded
                    # from the overlap sum.
                    selected_vars = [
                        x[(pid, j)]
                        for (pid, j), var in x.items()
                        if var.varValue is not None and var.varValue > 0.5
                        and pid not in elite_core_pids
                    ]
                    _cname = f"excl_{noise_seed}_{kbest_iter}"
                    prob += (
                        pulp.lpSum(selected_vars) <= max_overlap,
                        _cname,
                    )
                    _excl_names.append(_cname)

                    # Prune oldest constraint if beyond rolling window
                    if len(_excl_names) > KBEST_OVERLAP_WINDOW:
                        _stale = _excl_names.pop(0)
                        if _stale in prob.constraints:
                            del prob.constraints[_stale]

                    kbest_iter += 1

            if not _oversample_mode:
                logger.info(
                    f"[KBest] Seed {noise_seed}: produced {kbest_iter} "
                    f"lineups in {time.time() - start_time:.1f}s "
                    f"(total={len(generated)}/{target_count})"
                )
            noise_seed += 1

        # Log oversampling summary
        if _oversample_mode:
            _n_dupes = len(_seen_fingerprints) - len(generated) if _seen_fingerprints else 0
            logger.info(
                f"[KBest] Oversample complete: {len(generated)} unique "
                f"lineups from {noise_seed} seeds "
                f"({len(_seen_fingerprints)} unique fingerprints, "
                f"{noise_seed - len(_seen_fingerprints)} dupes skipped) "
                f"in {time.time() - start_time:.1f}s"
            )

        # ── Exploration coverage diagnostics ──────────────────────────
        if shared_exposure is not None and generated:
            snap = shared_exposure.snapshot()
            pool_pids = {p.player_id for p in pool}
            drafted = {pid for pid in snap if snap[pid] > 0 and pid in pool_pids}
            logger.info(
                f"[KBest] Exploration: {len(drafted)}/{len(pool_pids)} "
                f"pool players drafted ({len(pool_pids) - len(drafted)} never selected)"
            )

        return generated

    # ------------------------------------------------------------------
    # Optimizer internals (original projected_fp-based versions)
    # ------------------------------------------------------------------

    def _greedy_fill(
        self,
        pool: List[PlayerPoolEntry],
        lineup: Dict[str, PlayerPoolEntry],
        remaining_slots: List[str],
        used_ids: Set[int],
        remaining_salary: int,
        platform: str,
        sport: str = "nba",
    ) -> Dict[str, PlayerPoolEntry]:
        """Fill remaining slots greedily (most constrained first).

        ``remaining_slots`` may contain indexed keys ("PG_0") or plain
        slot names ("PG").  If plain names are passed (e.g., from tests)
        they are indexed automatically so FD duplicates don't collide.

        For each slot, picks the highest-projected-FP eligible player
        whose salary leaves enough room for the cheapest eligible
        player in every remaining unfilled slot.
        """
        # Auto-index if the caller passed plain slot names
        if remaining_slots and "_" not in remaining_slots[0]:
            remaining_slots = _index_slots(remaining_slots)

        for i, isl in enumerate(remaining_slots):
            base = _base_slot(isl)
            elig_positions = self._get_slot_eligible_positions(
                base, platform, sport
            )
            future_slots = remaining_slots[i + 1:]

            # Calculate minimum salary needed for all future slots
            min_future_salary = self._min_salary_for_slots(
                pool, future_slots, used_ids, platform, sport
            )

            budget = remaining_salary - min_future_salary

            candidates = [
                p for p in pool
                if (base in p.eligible_slots or self._player_matches_slot(p.position, elig_positions))
                and p.player_id not in used_ids
                and p.salary <= budget
                and p.injury_status not in ("Out", "Doubtful")
            ]

            if not candidates:
                # Fallback: ignore min-future check, just find anyone
                candidates = [
                    p for p in pool
                    if (base in p.eligible_slots or self._player_matches_slot(p.position, elig_positions))
                    and p.player_id not in used_ids
                    and p.salary <= remaining_salary
                    and p.injury_status not in ("Out", "Doubtful")
                ]

            if not candidates:
                logger.warning(
                    f"Cannot fill slot {base} — no eligible players "
                    f"within budget (${remaining_salary} remaining)"
                )
                continue

            # Pick highest projected FP
            best = max(candidates, key=lambda p: p.projected_fp)
            lineup[isl] = best
            used_ids.add(best.player_id)
            remaining_salary -= best.salary

        return lineup

    def _iterative_improve(
        self,
        lineup: Dict[str, PlayerPoolEntry],
        pool: List[PlayerPoolEntry],
        salary_cap: int,
        platform: str,
        max_iterations: int = 100,
        sport: str = "nba",
    ) -> Dict[str, PlayerPoolEntry]:
        """Attempt pairwise swaps to improve total projected FP.

        Lineup keys may be indexed ("PG_0") or plain ("PG"); the base
        slot name is extracted for eligibility checks.
        """
        used_ids = {p.player_id for p in lineup.values()}

        for iteration in range(max_iterations):
            improved = False

            for isl, current in list(lineup.items()):
                base = _base_slot(isl) if "_" in isl else isl
                elig_positions = self._get_slot_eligible_positions(
                    base, platform, sport
                )
                current_total_fp = sum(
                    p.projected_fp for p in lineup.values()
                )
                current_total_salary = sum(
                    p.salary for p in lineup.values()
                )

                for candidate in pool:
                    if candidate.player_id in used_ids:
                        if candidate.player_id != current.player_id:
                            continue
                        else:
                            continue  # skip self
                    if not self._player_matches_slot(candidate.position, elig_positions):
                        continue
                    # Never swap in an injured player
                    if candidate.injury_status in ("Out", "Doubtful"):
                        continue

                    new_salary = (
                        current_total_salary
                        - current.salary
                        + candidate.salary
                    )
                    if new_salary > salary_cap:
                        continue

                    new_fp = (
                        current_total_fp
                        - current.projected_fp
                        + candidate.projected_fp
                    )
                    if new_fp > current_total_fp:
                        # Accept the swap
                        used_ids.discard(current.player_id)
                        used_ids.add(candidate.player_id)
                        lineup[isl] = candidate
                        improved = True
                        break  # restart slot scan

            if not improved:
                break

        return lineup

    # ------------------------------------------------------------------
    # Game stacking & bring-back helpers
    # ------------------------------------------------------------------

    @staticmethod
    @staticmethod
    def _get_stackable_game_pool(
        pool: List[PlayerPoolEntry],
    ) -> List[Dict]:
        """Return all viable games for stacking with quality weights.

        Unlike ``_identify_stackable_games`` (which picks ONE random game),
        this returns the full list so the K-Best orchestrator can allocate
        lineups proportionally across all stack targets.

        Returns list of dicts with ``game_id``, ``team_a``, ``team_b``,
        ``game_total``, ``weight``.

        Prompt 7.14: when ``p.game_id`` is missing for every entry (the
        symptom we've seen on MLB after a stale enriched-pool cache hit),
        fall back to synthesizing one "game" per *team pair* using
        ``opponent_abbreviation`` so stacking still works. If even that
        is missing, a single "All Games" pseudo-target is emitted so the
        K-Best loop has something to dispatch instead of zero stacks.
        """
        games: Dict[str, Dict] = {}
        _missing_game_ids = 0
        for p in pool:
            gid = p.game_id
            if not gid:
                _missing_game_ids += 1
                # Synthesize a stable game_id from the team-vs-opponent
                # pair when present. Sorted so home/away players resolve
                # to the same key.
                team = (p.team_abbreviation or "").upper()
                opp = (p.opponent_abbreviation or "").upper() if hasattr(p, "opponent_abbreviation") else ""
                if team and opp:
                    pair = tuple(sorted([team, opp]))
                    gid = f"synth-{pair[0]}-{pair[1]}"
                else:
                    continue
            if gid not in games:
                games[gid] = {
                    "game_id": gid,
                    "teams": set(),
                    "game_total": p.game_total or 0,
                    "player_count": 0,
                }
            games[gid]["teams"].add(
                (p.team_abbreviation or "").upper()
            )
            games[gid]["player_count"] += 1

        viable = [
            g for g in games.values()
            if len(g["teams"]) >= 2 and g["player_count"] >= 4
        ]
        if _missing_game_ids and not viable:
            logger.warning(
                "[Stackable] %d/%d pool entries missing game_id and no "
                "team-pair fallback succeeded — slate stacking disabled "
                "for this run. Likely cause: stale enriched-pool cache "
                "from before sport-aware enrichment landed.",
                _missing_game_ids, len(pool),
            )
        if not viable:
            return []

        result = []
        for g in viable:
            total = g["game_total"] or 220
            w = max(1.0, (total - 200) ** 1.5) if total > 200 else 1.0
            teams = sorted(g["teams"])
            result.append({
                "game_id": g["game_id"],
                "team_a": teams[0],
                "team_b": teams[1] if len(teams) > 1 else teams[0],
                "game_total": g["game_total"],
                "weight": w,
            })
        return result

    @staticmethod
    def _select_secondary_stack_game(
        game_pool: List[Dict],
        primary_game_id: Optional[str],
    ) -> Optional[str]:
        """Pick the best secondary stack game (highest game total ≠ primary).

        Improvement #3: Players from the secondary game get a small scoring
        bonus, encouraging 3+1+2 stacking patterns.

        Returns the game_id of the secondary stack game, or None.
        """
        candidates = [
            g for g in game_pool
            if g["game_id"] != primary_game_id
        ]
        if not candidates:
            return None
        best = max(
            candidates,
            key=lambda g: g.get("game_total") or 0,
        )
        return best["game_id"]

    @staticmethod
    def _allocate_stack_targets(
        game_pool: List[Dict],
        total_lineups: int,
        rng: "random.Random",
    ) -> List[Tuple[Dict, int]]:
        """Distribute lineup count across stack targets proportionally.

        Each game gets a share of ``total_lineups`` weighted by its
        game-total quality weight.  Minimum 2 lineups per target (below
        that K-Best has no benefit over single-solve).

        Returns list of ``(game_dict, lineup_count)`` tuples.
        """
        from app.config.constants import KBEST_MIN_LINEUPS_PER_STACK

        if not game_pool:
            return []

        total_w = sum(g["weight"] for g in game_pool)
        if total_w <= 0:
            total_w = len(game_pool)

        # Proportional allocation with minimum per-stack floor
        raw_alloc = []
        for g in game_pool:
            share = (g["weight"] / total_w) * total_lineups
            raw_alloc.append((g, max(KBEST_MIN_LINEUPS_PER_STACK, int(share))))

        # If total allocation exceeds budget, trim smallest targets
        allocated = sum(c for _, c in raw_alloc)
        if allocated > total_lineups:
            # Sort by weight ascending; trim from weakest
            raw_alloc.sort(key=lambda x: x[0]["weight"])
            while allocated > total_lineups and len(raw_alloc) > 1:
                _, c = raw_alloc[0]
                allocated -= c
                raw_alloc.pop(0)

        # If under-allocated, top up the highest-weight target
        allocated = sum(c for _, c in raw_alloc)
        if allocated < total_lineups and raw_alloc:
            raw_alloc.sort(key=lambda x: x[0]["weight"], reverse=True)
            deficit = total_lineups - allocated
            g, c = raw_alloc[0]
            raw_alloc[0] = (g, c + deficit)

        return raw_alloc

    def _identify_stackable_games(
        self,
        pool: List[PlayerPoolEntry],
        rng: random.Random,
        min_game_total: float = 220.0,
    ) -> Optional[dict]:
        """Pick a target game for stacking, weighted toward high-total games.

        Returns dict with ``game_id``, ``team_a``, ``team_b``, ``game_total``
        or None if no suitable game exists.
        """
        # Group players by game_id
        games: Dict[str, Dict] = {}
        for p in pool:
            gid = p.game_id
            if not gid:
                continue
            if gid not in games:
                games[gid] = {
                    "game_id": gid,
                    "teams": set(),
                    "game_total": p.game_total or 0,
                    "player_count": 0,
                }
            games[gid]["teams"].add(p.team_abbreviation.upper())
            games[gid]["player_count"] += 1

        # Filter: need at least 2 teams and enough players
        viable = [
            g for g in games.values()
            if len(g["teams"]) >= 2 and g["player_count"] >= 4
        ]
        if not viable:
            return None

        # Weight by game total — high-total games get more stacking love
        weights = []
        for g in viable:
            total = g["game_total"] or 220
            # Games above threshold get exponentially more weight
            w = max(1.0, (total - 200) ** 1.5) if total > 200 else 1.0
            weights.append(w)

        # Weighted random selection
        total_w = sum(weights)
        r = rng.random() * total_w
        cumulative = 0.0
        for i, g in enumerate(viable):
            cumulative += weights[i]
            if r <= cumulative:
                teams = sorted(g["teams"])
                return {
                    "game_id": g["game_id"],
                    "team_a": teams[0],
                    "team_b": teams[1] if len(teams) > 1 else teams[0],
                    "game_total": g["game_total"],
                }

        # Fallback
        g = viable[-1]
        teams = sorted(g["teams"])
        return {
            "game_id": g["game_id"],
            "team_a": teams[0],
            "team_b": teams[1] if len(teams) > 1 else teams[0],
            "game_total": g["game_total"],
        }

    def _compute_slate_adjustments(
        self,
        pool: List[PlayerPoolEntry],
        contest_type: str = "gpp",
    ) -> Dict:
        """Compute slate-size-dependent parameter adjustments.

        Improvement #5: Small slates (2-3 games) need stronger stacking
        and higher ceiling weight; large slates (7+ games) need more
        contrarian ownership leverage and tighter diversity.

        Returns a dict of adjustments consumed by ILP/scoring code.
        """
        from app.config.constants import (
            GPP_SLATE_SMALL_MAX_GAMES,
            GPP_SLATE_LARGE_MIN_GAMES,
            GPP_SLATE_SMALL_CEILING_MULT,
            GPP_SLATE_SMALL_ALPHA_MULT,
            GPP_SLATE_LARGE_ALPHA_MULT,
            GPP_SLATE_LARGE_MAX_OVERLAP,
        )

        game_ids = {p.game_id for p in pool if p.game_id}
        num_games = len(game_ids)
        adj: Dict = {
            "num_games": num_games,
            "ceiling_weight_mult": 1.0,
            "alpha_mult": 1.0,
            "force_3man_stack": False,
            "max_overlap_override": None,
        }

        if contest_type not in ("gpp", "single_entry"):
            return adj

        if num_games <= GPP_SLATE_SMALL_MAX_GAMES:
            adj["ceiling_weight_mult"] = GPP_SLATE_SMALL_CEILING_MULT
            adj["alpha_mult"] = GPP_SLATE_SMALL_ALPHA_MULT
            adj["force_3man_stack"] = True
            logger.info(
                f"[SlateAdapt] Small slate ({num_games} games): "
                f"ceiling×{GPP_SLATE_SMALL_CEILING_MULT:.2f}, "
                f"alpha×{GPP_SLATE_SMALL_ALPHA_MULT:.2f}, "
                f"force 3-man stack"
            )
        elif num_games >= GPP_SLATE_LARGE_MIN_GAMES:
            adj["alpha_mult"] = GPP_SLATE_LARGE_ALPHA_MULT
            adj["max_overlap_override"] = GPP_SLATE_LARGE_MAX_OVERLAP
            logger.info(
                f"[SlateAdapt] Large slate ({num_games} games): "
                f"alpha×{GPP_SLATE_LARGE_ALPHA_MULT:.2f}, "
                f"max_overlap={GPP_SLATE_LARGE_MAX_OVERLAP}"
            )

        return adj

    def _compute_dynamic_stack_params(
        self,
        rng: random.Random,
        pool: List[PlayerPoolEntry],
        target_game: dict,
        contest_type: str = "gpp",
    ) -> Tuple[int, bool]:
        """Determine stack size and bring-back rate from learned calibrations.

        Uses tournament-learned stacking weights to shift the default
        2-man/3-man split and bring-back rate.  In GPP mode, also gates
        against stacking high-ownership games (chalk avoidance).

        Returns ``(stack_size, bring_back)``.
        """
        from app.config.constants import (
            DEFAULT_STACK_3MAN_RATIO,
            DEFAULT_BRINGBACK_RATE,
            STACK_OWNERSHIP_GATE_THRESHOLD,
        )

        ratio_3man = DEFAULT_STACK_3MAN_RATIO
        bringback_rate = DEFAULT_BRINGBACK_RATE

        if self.calibration_service:
            w_3man = self.calibration_service.get_stacking_weight("3man_weight")
            w_2man = self.calibration_service.get_stacking_weight("2man_weight")
            w_bb = self.calibration_service.get_stacking_weight("bringback_weight")

            # Shift ratios based on learned weights
            if w_3man != 1.0 and w_3man > 1.05:
                ratio_3man = min(0.80, DEFAULT_STACK_3MAN_RATIO + 0.15)
            elif w_2man != 1.0 and w_2man > w_3man:
                ratio_3man = max(0.30, DEFAULT_STACK_3MAN_RATIO - 0.15)

            # Adjust bring-back rate from calibration
            if w_bb != 1.0:
                bringback_rate = max(0.40, min(0.90, DEFAULT_BRINGBACK_RATE * w_bb))

        # GPP ownership gating: reduce 3-man stacking in chalk-heavy games
        if contest_type in ("gpp", "single_entry") and target_game:
            game_id = target_game["game_id"]
            game_players = [
                p for p in pool
                if p.game_id == game_id
                and p.estimated_ownership is not None
                and p.projected_fp > 10.0
            ]
            if game_players:
                avg_own = sum(
                    p.estimated_ownership for p in game_players
                ) / len(game_players)
                if avg_own > STACK_OWNERSHIP_GATE_THRESHOLD:
                    ratio_3man = max(0.20, ratio_3man - 0.20)

        # Improvement #5: Slate-size adaptive — force 3-man on small slates
        if (
            hasattr(self, '_slate_adjustments')
            and self._slate_adjustments
            and self._slate_adjustments.get("force_3man_stack")
        ):
            ratio_3man = max(ratio_3man, 0.85)  # near-guaranteed 3-man

        stack_size = 3 if rng.random() < ratio_3man else 2
        bring_back = rng.random() < bringback_rate

        return stack_size, bring_back

    @staticmethod
    def _select_stack_players(
        pool: List[PlayerPoolEntry],
        target_game: dict,
        rng: random.Random,
        stack_size: int = 3,
        bring_back: bool = True,
        correlation_weights: Optional[Dict[Tuple[int, int], float]] = None,
    ) -> List[int]:
        """Select player IDs for a game stack with optional bring-back.

        Uses a correlation-driven approach:
        - **First player**: selected by projection (highest FP gets chosen).
        - **Subsequent players**: weighted blend of projection and
          correlation to already-selected teammates.
        - **Bring-back**: uses cross-team correlation data (if available)
          to pick the best opposing player for the stack.
        - **3-man quality gate**: if avg pairwise correlation among
          primary team players is below the floor, downgrades to 2-man.
        """
        from app.config.constants import (
            STACK_PROJECTION_WEIGHT,
            STACK_CORRELATION_WEIGHT,
            STACK_3MAN_CORRELATION_FLOOR,
            BRINGBACK_CROSS_TEAM_CORR_WEIGHT,
            BRINGBACK_PROJECTION_WEIGHT,
            BRINGBACK_CEILING_WEIGHT,
            BRINGBACK_NEGATIVE_CORR_WEIGHT,
        )

        game_id = target_game["game_id"]
        team_a = target_game["team_a"]
        team_b = target_game["team_b"]

        # Pick primary team randomly (weighted toward higher total roster)
        team_a_players = [
            p for p in pool
            if p.game_id == game_id and p.team_abbreviation.upper() == team_a
            and p.projected_fp > 5.0
        ]
        team_b_players = [
            p for p in pool
            if p.game_id == game_id and p.team_abbreviation.upper() == team_b
            and p.projected_fp > 5.0
        ]

        if not team_a_players and not team_b_players:
            return []

        # Choose primary stack team
        if len(team_a_players) >= len(team_b_players):
            primary, secondary = team_a_players, team_b_players
        else:
            primary, secondary = team_b_players, team_a_players

        # Mix it up sometimes
        if rng.random() < 0.35 and secondary:
            primary, secondary = secondary, primary

        # Select primary team stack players
        primary_count = (stack_size - 1) if (bring_back and secondary) else stack_size
        primary_count = min(primary_count, len(primary))

        selected_ids: List[int] = []
        remaining = list(primary)

        for _ in range(primary_count):
            if not remaining:
                break

            max_proj = max(p.projected_fp for p in remaining) or 1.0
            weights = []

            for p in remaining:
                if not selected_ids:
                    # First player: pure projection-based selection
                    w = max(0.1, p.projected_fp * rng.uniform(0.85, 1.15))
                else:
                    # Subsequent players: blend projection + correlation
                    proj_component = p.projected_fp / max_proj  # normalized 0-1

                    corr_component = 0.0
                    if correlation_weights:
                        corr_sum = 0.0
                        corr_count = 0
                        for sel_id in selected_ids:
                            pair_key = (
                                min(sel_id, p.player_id),
                                max(sel_id, p.player_id),
                            )
                            corr = correlation_weights.get(pair_key)
                            if corr is not None:
                                corr_sum += corr
                                corr_count += 1
                        if corr_count > 0:
                            corr_component = max(corr_sum / corr_count, 0.0)

                    w = max(0.1, (
                        STACK_PROJECTION_WEIGHT * proj_component
                        + STACK_CORRELATION_WEIGHT * corr_component
                    ) * rng.uniform(0.85, 1.15))

                weights.append(w)

            # Weighted random selection
            total = sum(weights)
            r = rng.random() * total
            cumulative = 0.0
            chosen_idx = len(remaining) - 1
            for idx, w in enumerate(weights):
                cumulative += w
                if r <= cumulative:
                    chosen_idx = idx
                    break
            selected_ids.append(remaining[chosen_idx].player_id)
            remaining.pop(chosen_idx)

        # 3-man correlation quality gate: downgrade to 2-man if avg
        # pairwise correlation among primary players is too weak.
        if len(selected_ids) >= 3 and correlation_weights:
            corr_vals = []
            for i in range(len(selected_ids)):
                for j in range(i + 1, len(selected_ids)):
                    pair_key = (
                        min(selected_ids[i], selected_ids[j]),
                        max(selected_ids[i], selected_ids[j]),
                    )
                    corr = correlation_weights.get(pair_key)
                    if corr is not None:
                        corr_vals.append(corr)
            if corr_vals and (sum(corr_vals) / len(corr_vals)) < STACK_3MAN_CORRELATION_FLOOR:
                # Keep the best-correlated pair (first two selected)
                selected_ids = selected_ids[:2]

        # Bring-back: pick one opponent using 3-component scoring:
        # projection (0.35) + ceiling (0.25) + negative correlation (0.40)
        # Negative cross-team correlations are preferred as natural hedges.
        if bring_back and secondary:
            opp_candidates = [p for p in secondary if p.player_id not in selected_ids]
            if opp_candidates:
                max_opp_proj = max(p.projected_fp for p in opp_candidates) or 1.0
                max_opp_ceil = max(
                    (getattr(p, "ceiling_fp", 0) or p.projected_fp)
                    for p in opp_candidates
                ) or 1.0
                weights = []
                for p in opp_candidates:
                    proj_norm = p.projected_fp / max_opp_proj

                    # Ceiling component: prefer high-ceiling opponents
                    ceil_val = getattr(p, "ceiling_fp", 0) or p.projected_fp
                    ceil_norm = ceil_val / max_opp_ceil

                    # Negative correlation component: prefer opponents with
                    # negative cross-team correlations to our primary stack
                    # (they hedge against our stack underperforming).
                    neg_corr_component = 0.0
                    if correlation_weights and selected_ids:
                        corr_vals = []
                        for sel_id in selected_ids:
                            pair_key = (
                                min(sel_id, p.player_id),
                                max(sel_id, p.player_id),
                            )
                            corr = correlation_weights.get(pair_key)
                            if corr is not None:
                                corr_vals.append(corr)
                        if corr_vals:
                            avg_corr = sum(corr_vals) / len(corr_vals)
                            # Transform: negative corr → high score,
                            # positive corr → low score, zero → 0.5
                            # Range: corr in [-1,1] → score in [0,1]
                            neg_corr_component = (1.0 - avg_corr) / 2.0

                    w = max(0.1, (
                        BRINGBACK_PROJECTION_WEIGHT * proj_norm
                        + BRINGBACK_CEILING_WEIGHT * ceil_norm
                        + BRINGBACK_NEGATIVE_CORR_WEIGHT * neg_corr_component
                    ) * rng.uniform(0.85, 1.15))
                    weights.append(w)

                total = sum(weights)
                r = rng.random() * total
                cumulative = 0.0
                chosen_idx = len(opp_candidates) - 1
                for idx, w in enumerate(weights):
                    cumulative += w
                    if r <= cumulative:
                        chosen_idx = idx
                        break
                selected_ids.append(opp_candidates[chosen_idx].player_id)

        return selected_ids

    @staticmethod
    def _enforce_salary_floor(
        lineup: Dict[str, "PlayerPoolEntry"],
        pool: List["PlayerPoolEntry"],
        salary_cap: int,
        salary_floor: int,
        platform: str,
        get_slot_eligible_positions,
        sport: str = "nba",
    ) -> Dict[str, "PlayerPoolEntry"]:
        """Swap up cheap players to meet the salary floor.

        Iterates slots from cheapest player upward, swapping each with
        the best affordable upgrade until the floor is met.
        """
        total_salary = sum(p.salary for p in lineup.values())
        if total_salary >= salary_floor:
            return lineup

        used_ids = {p.player_id for p in lineup.values()}

        # Sort slots by current player salary (cheapest first)
        slots_by_salary = sorted(
            lineup.items(),
            key=lambda x: x[1].salary,
        )

        for isl, current in slots_by_salary:
            if total_salary >= salary_floor:
                break

            base = _base_slot(isl) if "_" in isl else isl
            elig_positions = get_slot_eligible_positions(base, platform, sport)

            # How much more do we need?
            deficit = salary_floor - total_salary

            # Find the best upgrade that helps close the deficit
            best_upgrade = None
            best_upgrade_fp = -1.0

            for candidate in pool:
                if candidate.player_id in used_ids:
                    continue
                if not LineupOptimizerService._player_matches_slot(candidate.position, elig_positions):
                    continue
                salary_diff = candidate.salary - current.salary
                if salary_diff <= 0:
                    continue  # Must be more expensive
                new_total = total_salary + salary_diff
                if new_total > salary_cap:
                    continue
                # Pick the upgrade that is most expensive while still having good FP
                if candidate.projected_fp > best_upgrade_fp:
                    best_upgrade = candidate
                    best_upgrade_fp = candidate.projected_fp

            if best_upgrade:
                salary_diff = best_upgrade.salary - current.salary
                used_ids.discard(current.player_id)
                used_ids.add(best_upgrade.player_id)
                lineup[isl] = best_upgrade
                total_salary += salary_diff

        return lineup

    def _min_salary_for_slots(
        self,
        pool: List[PlayerPoolEntry],
        slots: List[str],
        used_ids: Set[int],
        platform: str,
        sport: str = "nba",
    ) -> int:
        """Calculate the minimum salary needed to fill the given slots.

        For each slot, finds the cheapest eligible unused player.
        Slots may be indexed ("PG_0") or plain ("PG").
        This is used by the greedy filler to avoid overspending early.
        """
        total = 0
        temp_used = set(used_ids)

        for slot in slots:
            base = _base_slot(slot) if "_" in slot else slot
            elig = self._get_slot_eligible_positions(base, platform, sport)
            cheapest = None
            for p in pool:
                if (
                    (base in p.eligible_slots or self._player_matches_slot(p.position, elig))
                    and p.player_id not in temp_used
                    and p.injury_status not in ("Out", "Doubtful")
                    and (cheapest is None or p.salary < cheapest.salary)
                ):
                    cheapest = p

            if cheapest:
                total += cheapest.salary
                temp_used.add(cheapest.player_id)

        return total

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _get_eligible_slots(
        position: str, platform: str, sport: str = "nba"
    ) -> List[str]:
        """Return which roster slots a player with this position can fill.

        Handles dual-position strings like "PG/SG", "SF/PF", "PF/C" by
        splitting on "/" and checking each individual position.

        CBB DraftKings uses simplified positions (G, F, G/F) instead of
        NBA-style (PG, SG, SF, PF, C).  A "G" maps to guard slots
        (G, UTIL) and "F" maps to forward slots (F, UTIL).

        When sport is "cbb", uses DK_CBB_SLOT_ELIGIBILITY so eligible
        slots match the actual CBB roster format (G, G, G, F, F, F, UTIL, UTIL).
        """
        # Split dual-position strings: "PG/SG" → ["PG", "SG"]
        player_positions = [p.strip() for p in position.split("/")]

        # Expand CBB generic positions to NBA-style equivalents so the
        # standard slot eligibility map works.
        expanded = []
        for pp in player_positions:
            if pp == "G":
                expanded.extend(["PG", "SG"])
            elif pp == "F":
                expanded.extend(["SF", "PF", "C"])
            else:
                expanded.append(pp)
        # De-dup while preserving order
        expanded = list(dict.fromkeys(expanded))

        # Also include the raw CBB positions for CBB eligibility matching
        raw_positions = list(dict.fromkeys(player_positions + expanded))

        if platform == "dk":
            from app.sports import get_config as _get_sport_cfg
            elig_map = _get_sport_cfg(sport).dk_slot_eligibility
            slots = []
            for slot, eligible_positions in elig_map.items():
                if any(pp in eligible_positions for pp in raw_positions):
                    slots.append(slot)
            return slots
        else:
            # FD: each individual position maps to its named slot(s)
            return list(dict.fromkeys(pp for pp in expanded))

    @staticmethod
    def _get_slot_eligible_positions(
        slot: str, platform: str, sport: str = "nba"
    ) -> List[str]:
        """Return which positions can fill this roster slot.

        Uses sport-specific eligibility maps so CBB slots (G, F, UTIL)
        correctly accept CBB position codes (G, F, C).
        """
        if platform == "dk":
            # Prefer the sport-specific eligibility map. This must come BEFORE
            # the showdown fallback because NFL's FLEX slot exists in both
            # NFL's classic eligibility (RB/WR/TE) and DK_SHOWDOWN_ELIGIBILITY
            # (NBA showdown's PG/SG/SF/PF/C). Showdown's FLEX is a different
            # concept than NFL's FLEX — only fall through to it for slots
            # that aren't in the sport map (i.e. CPT in NBA showdown).
            from app.sports import get_config as _get_sport_cfg
            elig = _get_sport_cfg(sport).dk_slot_eligibility
            if slot in elig:
                return elig[slot]
            # Fallback for showdown-only slots (CPT, and FLEX in NBA showdown
            # when it's not in the classic map for that sport).
            if slot in DK_SHOWDOWN_ELIGIBILITY:
                return DK_SHOWDOWN_ELIGIBILITY[slot]
            # Last-ditch: legacy NBA fallback for slots that don't exist in
            # this sport's map (defensive for cross-sport call sites).
            return _get_sport_cfg("nba").dk_slot_eligibility.get(slot, [])
        else:
            return FD_SLOT_ELIGIBILITY.get(slot, [slot])

    # ------------------------------------------------------------------
    # Late-swap roster slot optimisation
    # ------------------------------------------------------------------

    @classmethod
    def _parse_game_time_minutes(cls, time_str: Optional[str]) -> int:
        """Convert "7:00 PM ET" → minutes-since-midnight (1140).

        Returns 0 if the string can't be parsed so unknown-time players
        are placed in strict slots first.
        """
        import re

        if not time_str:
            return 0
        m = re.match(r"(\d{1,2}):(\d{2})\s*(AM|PM)", time_str, re.IGNORECASE)
        if not m:
            return 0
        hour = int(m.group(1))
        minute = int(m.group(2))
        ampm = m.group(3).upper()
        if ampm == "PM" and hour != 12:
            hour += 12
        elif ampm == "AM" and hour == 12:
            hour = 0
        return hour * 60 + minute

    # Flex-priority weights: higher weight → prefer later-game players
    # in this slot to maximise late-swap flexibility.
    _SLOT_FLEX_WEIGHT: Dict[str, int] = {
        "UTIL": 100,
        "G": 10,
        "F": 10,
        "PG": 1,
        "SG": 1,
        "SF": 1,
        "PF": 1,
        "C": 1,
    }

    @classmethod
    def _optimize_roster_slots(
        cls,
        lineup: Dict[str, "PlayerPoolEntry"],
        roster_slots: List[str],
        platform: str = "dk",
        sport: str = "nba",
    ) -> Dict[str, "PlayerPoolEntry"]:
        """Re-assign roster slots to maximise late-swap flexibility.

        After the solver selects the best 8 players and assigns them to
        positional slots, this post-processor **keeps the same 8 players**
        but re-shuffles their slot assignments so that:

        1. The player with the **latest** tip-off sits in UTIL (any-position
           swap target).
        2. The next-latest players fill G / F flex slots.
        3. Early-game players occupy the strict positional slots (PG, SG, …)
           where swaps are limited to same-position replacements.

        Uses a mini-ILP (assignment problem) when PuLP is available,
        otherwise falls back to a greedy heuristic.

        Parameters
        ----------
        lineup
            Current ILP/greedy result: ``{indexed_slot: PlayerPoolEntry}``.
        roster_slots
            Ordered list of roster slot names (e.g. ``DK_ROSTER_SLOTS``).
        platform
            ``"dk"`` or ``"fd"``.
        sport
            ``"nba"`` or ``"cbb"``.

        Returns
        -------
        Dict[str, PlayerPoolEntry]
            New mapping with the same players, potentially in different
            slots.
        """
        # Only optimise for DK classic mode (showdown / FD have no
        # meaningful flex hierarchy).
        if platform != "dk" or sport not in ("nba", "cbb"):
            return lineup

        players = list(lineup.values())
        if not players:
            return lineup

        # ── Derive game-time minutes for each player ─────────────────
        game_times: Dict[int, int] = {}
        for p in players:
            game_times[p.player_id] = cls._parse_game_time_minutes(
                getattr(p, "game_commence_time", None)
            )

        # If all game times are identical (single-game slate or unknown),
        # there is nothing to optimise — return as-is.
        unique_times = set(game_times.values())
        if len(unique_times) <= 1:
            return lineup

        indexed = _index_slots(roster_slots)

        # ── Build eligibility map ────────────────────────────────────
        # For each (player, indexed_slot) pair, can this player legally
        # fill this slot?
        eligible: Dict[tuple, bool] = {}
        for p in players:
            p_positions = cls._expand_player_position(p.position)
            for isl in indexed:
                base = _base_slot(isl)
                slot_pos = cls._get_slot_eligible_positions(
                    base, platform, sport,
                )
                eligible[(p.player_id, isl)] = any(
                    pos in slot_pos for pos in p_positions
                )

        # ── Try mini-ILP (assignment problem) ────────────────────────
        if _PULP_AVAILABLE:
            result = cls._roster_slot_ilp(
                players, indexed, game_times, eligible,
            )
            if result is not None:
                return result

        # ── Greedy fallback ──────────────────────────────────────────
        return cls._roster_slot_greedy(
            players, indexed, game_times, eligible,
        )

    @classmethod
    def _roster_slot_ilp(
        cls,
        players: List["PlayerPoolEntry"],
        indexed_slots: List[str],
        game_times: Dict[int, int],
        eligible: Dict[tuple, bool],
    ) -> Optional[Dict[str, "PlayerPoolEntry"]]:
        """Solve the roster-slot assignment as a binary ILP.

        Objective: maximise ∑ flex_weight[slot] × game_time[player] × y
        Subject to:
            - Each player assigned to exactly one slot
            - Each slot assigned exactly one player
            - Assignment respects position eligibility
        """
        import pulp  # type: ignore

        prob = pulp.LpProblem("RosterSlotOptim", pulp.LpMaximize)

        # Decision variables
        y: Dict[tuple, pulp.LpVariable] = {}
        for p in players:
            for isl in indexed_slots:
                if eligible.get((p.player_id, isl), False):
                    y[(p.player_id, isl)] = pulp.LpVariable(
                        f"y_{p.player_id}_{isl}",
                        cat=pulp.LpBinary,
                    )

        if not y:
            return None

        # Objective: maximise late-game players in flex slots
        prob += pulp.lpSum(
            cls._SLOT_FLEX_WEIGHT.get(_base_slot(isl), 1)
            * game_times.get(pid, 0)
            * var
            for (pid, isl), var in y.items()
        )

        # C1: Each player in exactly one slot
        for p in players:
            p_vars = [
                var for (pid, _), var in y.items() if pid == p.player_id
            ]
            if p_vars:
                prob += pulp.lpSum(p_vars) == 1

        # C2: Each slot has exactly one player
        for isl in indexed_slots:
            s_vars = [
                var for (_, s), var in y.items() if s == isl
            ]
            if s_vars:
                prob += pulp.lpSum(s_vars) == 1

        # Solve
        try:
            solver = pulp.PULP_CBC_CMD(msg=0, timeLimit=2)
            prob.solve(solver)
        except Exception:
            return None

        if prob.status != pulp.constants.LpStatusOptimal:
            return None

        # Extract result
        p_lookup = {p.player_id: p for p in players}
        result: Dict[str, "PlayerPoolEntry"] = {}
        for (pid, isl), var in y.items():
            if var.varValue is not None and var.varValue > 0.5:
                result[isl] = p_lookup[pid]

        if len(result) != len(players):
            return None  # incomplete assignment — fallback to greedy

        return result

    @classmethod
    def _roster_slot_greedy(
        cls,
        players: List["PlayerPoolEntry"],
        indexed_slots: List[str],
        game_times: Dict[int, int],
        eligible: Dict[tuple, bool],
    ) -> Dict[str, "PlayerPoolEntry"]:
        """Greedy heuristic: assign latest-game players to flex slots first.

        Sort players by game_time descending, then assign each player to
        the highest-flex-weight eligible slot that is still unfilled.
        """
        sorted_players = sorted(
            players,
            key=lambda p: game_times.get(p.player_id, 0),
            reverse=True,
        )

        # Sort slots by flex weight descending so UTIL is tried first,
        # then G/F, then strict positional.
        slot_priority = sorted(
            indexed_slots,
            key=lambda s: cls._SLOT_FLEX_WEIGHT.get(_base_slot(s), 1),
            reverse=True,
        )

        assigned: Dict[str, "PlayerPoolEntry"] = {}
        used_players: set = set()
        used_slots: set = set()

        for p in sorted_players:
            for isl in slot_priority:
                if isl in used_slots:
                    continue
                if not eligible.get((p.player_id, isl), False):
                    continue
                assigned[isl] = p
                used_players.add(p.player_id)
                used_slots.add(isl)
                break

        # If greedy couldn't place everyone (shouldn't happen with a valid
        # lineup), fall back to original order.
        if len(assigned) != len(players):
            # Put unplaced players into any remaining slot
            remaining_slots = [
                s for s in indexed_slots if s not in used_slots
            ]
            unplaced = [
                p for p in players if p.player_id not in used_players
            ]
            for p, isl in zip(unplaced, remaining_slots):
                assigned[isl] = p

        return assigned

    @staticmethod
    def _expand_player_position(position: str) -> List[str]:
        """Expand a player position string into individual NBA-style positions.

        Handles CBB dual-position strings like "G/F", "F/C" and CBB generic
        positions "G" → ["PG", "SG"], "F" → ["SF", "PF", "C"].

        Examples:
            "PG"  → ["PG"]
            "G"   → ["PG", "SG"]
            "F"   → ["SF", "PF", "C"]
            "G/F" → ["PG", "SG", "SF", "PF", "C"]
            "F/C" → ["SF", "PF", "C"]
        """
        parts = [p.strip() for p in position.split("/")]
        expanded: List[str] = []
        for pp in parts:
            if pp == "G":
                expanded.extend(["PG", "SG"])
            elif pp == "F":
                expanded.extend(["SF", "PF", "C"])
            else:
                expanded.append(pp)
        return list(dict.fromkeys(expanded))  # de-dup, preserve order

    @classmethod
    def _player_matches_slot(
        cls, player_position: str, slot_eligible: List[str]
    ) -> bool:
        """Check if a player's position is eligible for a slot.

        Properly handles CBB dual positions like "G/F" by expanding to
        individual positions and checking if any match the slot's eligible
        list.
        """
        expanded = cls._expand_player_position(player_position)
        return any(pos in slot_eligible for pos in expanded)

    @staticmethod
    def _names_match(dk_name: str, nba_name: str) -> bool:
        """Check if a DK draftable name matches an NBA API player name.

        Handles:
        - Suffix differences (Jr., Sr., III, IV)
        - Period removal (P.J. → PJ)
        - Accent transliteration (Jokić → Jokic)
        - Hyphen/apostrophe differences (Gilgeous-Alexander, O'Brien)
        - Short first names (PJ — 2 chars)
        - Name-order swaps (rare but possible)

        Avoids false positives on common surnames
        (e.g. "Marcus Morris" vs "Markieff Morris").
        """
        from app.services.dk_draftables_service import _normalize_name

        # Normalize + strip hyphens/apostrophes (aligns with
        # rotation_engine._normalize_for_match behaviour)
        a = _normalize_name(dk_name).replace("-", "").replace("'", "")
        b = _normalize_name(nba_name).replace("-", "").replace("'", "")

        if a == b:
            return True

        # Fallback 1: last name exact + first-name prefix match
        parts_a = a.split()
        parts_b = b.split()
        if parts_a and parts_b and len(parts_a) >= 2 and len(parts_b) >= 2:
            if parts_a[-1] == parts_b[-1]:
                fa, fb = parts_a[0], parts_b[0]
                min_len = min(len(fa), len(fb))
                if min_len >= 4:
                    # Long first names: compare first 4 chars to avoid
                    # false positives (Marcus/Markieff Morris)
                    if fa[:4] == fb[:4]:
                        return True
                elif min_len >= 3:
                    # Medium first names: compare first 3
                    if fa[:3] == fb[:3]:
                        return True
                elif min_len >= 2:
                    # Short first name (e.g. "PJ") → exact first-name match
                    # to avoid false positives on longer names
                    if fa == fb:
                        return True

        # Fallback 2: sorted-token comparison (catches name-order swaps)
        if sorted(a.split()) == sorted(b.split()):
            return True

        # Fallback 3: Nuclear normalize — strip ALL spaces and compare.
        # Catches subtle whitespace/encoding differences:
        #   "deandre" vs "de andre", "mcconnell" vs "mc connell"
        a_nuclear = a.replace(" ", "")
        b_nuclear = b.replace(" ", "")
        if a_nuclear == b_nuclear:
            return True

        # Fallback 4: Fuzzy match with high threshold (0.85+).
        # Catches minor typos and transliteration differences
        # that slip past all other normalizers.
        from difflib import SequenceMatcher
        if len(a) >= 6 and len(b) >= 6:
            ratio = SequenceMatcher(None, a, b).ratio()
            if ratio >= 0.88:
                return True

        return False

    # ------------------------------------------------------------------
    # Late swap automation
    # ------------------------------------------------------------------

    def detect_late_swaps(
        self,
        lineup: OptimizedLineup,
        pool: List[PlayerPoolEntry],
        portfolio_exposure: Optional[Dict[int, int]] = None,
        total_lineups: int = 1,
        sport: str = "nba",
    ) -> List[Dict]:
        """Detect players in a lineup who are ruled out and suggest swaps.

        When ``portfolio_exposure`` is provided (dict of player_id → count
        across the full portfolio), replacement selection blends projection
        with a diversity bonus to avoid all lineups converging on the same
        replacement player.

        Returns a list of swap suggestions, each containing:
          - ``slot``: the roster slot of the affected player
          - ``out_player``: dict with player info being swapped out
          - ``in_player``: dict with recommended replacement info
          - ``reason``: why the swap is needed
          - ``fp_delta``: change in projected FP
        """
        from app.config.constants import (
            LATE_SWAP_DIVERSITY_WEIGHT,
            LATE_SWAP_PROJECTION_WEIGHT,
            LATE_SWAP_MAX_REPLACEMENT_EXPOSURE,
        )

        swaps: List[Dict] = []
        platform = lineup.platform
        used_ids = {p.player_id for p in lineup.players}
        has_portfolio = bool(portfolio_exposure) and total_lineups > 1

        for lp in lineup.players:
            # Check if player is ruled out
            pool_entry = next(
                (p for p in pool if p.player_id == lp.player_id),
                None,
            )
            if not pool_entry:
                continue

            status = pool_entry.injury_status
            if status not in ("Out", "Doubtful"):
                continue

            # Player is out — find the best replacement
            slot = lp.roster_slot
            elig_positions = self._get_slot_eligible_positions(slot, platform, sport)

            # Budget: can spend up to the out player's salary + remaining cap
            budget = lp.salary + lineup.salary_remaining

            eligible_replacements = []
            for candidate in pool:
                if candidate.player_id in used_ids:
                    continue
                if not self._player_matches_slot(candidate.position, elig_positions):
                    continue
                if candidate.salary > budget:
                    continue
                # Skip other injured players
                if candidate.injury_status in ("Out", "Doubtful"):
                    continue
                eligible_replacements.append(candidate)

            if not eligible_replacements:
                continue

            if has_portfolio:
                # Portfolio-aware scoring: blend projection + diversity
                max_proj = max(c.projected_fp for c in eligible_replacements) or 1.0
                max_exposure = max(
                    LATE_SWAP_MAX_REPLACEMENT_EXPOSURE * total_lineups, 1.0
                )

                best_replacement = None
                best_score = -1.0

                for candidate in eligible_replacements:
                    # Normalized projection (0-1)
                    proj_norm = candidate.projected_fp / max_proj if max_proj > 0 else 0.0

                    # Diversity bonus: lower exposure → higher bonus
                    exposure_count = portfolio_exposure.get(candidate.player_id, 0)
                    exposure_frac = exposure_count / total_lineups

                    # If already at max exposure, hard penalty
                    if exposure_frac >= LATE_SWAP_MAX_REPLACEMENT_EXPOSURE:
                        diversity_bonus = -0.20  # Strong penalty
                    else:
                        # Linear: 1.0 at 0% exposure → 0.0 at max
                        diversity_bonus = 1.0 - (
                            exposure_frac / LATE_SWAP_MAX_REPLACEMENT_EXPOSURE
                        )

                    score = (
                        LATE_SWAP_PROJECTION_WEIGHT * proj_norm
                        + LATE_SWAP_DIVERSITY_WEIGHT * diversity_bonus
                    )

                    if score > best_score:
                        best_score = score
                        best_replacement = candidate
            else:
                # Single-lineup fallback: pure projection
                best_replacement = max(
                    eligible_replacements, key=lambda c: c.projected_fp
                )

            if best_replacement:
                swaps.append({
                    "slot": slot,
                    "out_player": {
                        "player_id": lp.player_id,
                        "player_name": lp.player_name,
                        "team": lp.team_abbreviation,
                        "salary": lp.salary,
                        "projected_fp": lp.projected_fp,
                        "injury_status": status,
                        "injury_description": pool_entry.injury_description or "",
                    },
                    "in_player": {
                        "player_id": best_replacement.player_id,
                        "player_name": best_replacement.player_name,
                        "team": best_replacement.team_abbreviation,
                        "salary": best_replacement.salary,
                        "projected_fp": best_replacement.projected_fp,
                    },
                    "reason": f"{lp.player_name} is {status}"
                             + (f" ({pool_entry.injury_description})"
                                if pool_entry.injury_description else ""),
                    "fp_delta": round(best_replacement.projected_fp - lp.projected_fp, 1),
                })

        return swaps

    def apply_late_swaps(
        self,
        lineup: OptimizedLineup,
        pool: List[PlayerPoolEntry],
        portfolio_exposure: Optional[Dict[int, int]] = None,
        total_lineups: int = 1,
    ) -> OptimizedLineup:
        """Apply all late swaps to a lineup and return the updated lineup.

        Automatically swaps out players who are ruled Out or Doubtful
        with the best available replacement at the same roster slot.

        When ``portfolio_exposure`` is provided, replacements are selected
        with diversity awareness to prevent all lineups from converging
        on the same replacement.
        """
        swaps = self.detect_late_swaps(
            lineup, pool,
            portfolio_exposure=portfolio_exposure,
            total_lineups=total_lineups,
        )
        if not swaps:
            return lineup

        players = list(lineup.players)
        warnings = list(lineup.warnings)

        for swap in swaps:
            out_id = swap["out_player"]["player_id"]
            in_info = swap["in_player"]

            # Find the pool entry for the replacement
            replacement = next(
                (p for p in pool if p.player_id == in_info["player_id"]),
                None,
            )
            if not replacement:
                continue

            # Find and replace the player in the lineup
            for i, p in enumerate(players):
                if p.player_id == out_id:
                    players[i] = LineupPlayer(
                        player_id=replacement.player_id,
                        player_name=replacement.player_name,
                        display_name=replacement.display_name or replacement.player_name,
                        position=replacement.position,
                        roster_slot=p.roster_slot,
                        team_abbreviation=replacement.team_abbreviation,
                        salary=replacement.salary,
                        projected_fp=replacement.projected_fp,
                        floor_fp=replacement.floor_fp,
                        ceiling_fp=replacement.ceiling_fp,
                        projected_minutes=replacement.projected_minutes,
                        projected_stats=replacement.projected_stats,
                        dk_player_id=replacement.dk_player_id,
                    )
                    warnings.append(
                        f"Late swap: {swap['out_player']['player_name']} "
                        f"({swap['out_player']['injury_status']}) → "
                        f"{replacement.player_name}"
                    )
                    # Update portfolio exposure for subsequent lineups
                    if portfolio_exposure is not None:
                        portfolio_exposure[replacement.player_id] = (
                            portfolio_exposure.get(replacement.player_id, 0) + 1
                        )
                    break

        total_salary = sum(p.salary for p in players)
        total_fp = round(sum(p.projected_fp for p in players), 1)
        total_floor = round(sum(p.floor_fp for p in players), 1)
        total_ceil = round(sum(p.ceiling_fp for p in players), 1)

        return OptimizedLineup(
            platform=lineup.platform,
            sport=lineup.sport,
            players=players,
            total_salary=total_salary,
            salary_remaining=lineup.salary_cap - total_salary,
            total_projected_fp=total_fp,
            total_floor_fp=total_floor,
            total_ceiling_fp=total_ceil,
            salary_cap=lineup.salary_cap,
            roster_slots=lineup.roster_slots,
            warnings=warnings,
        )

    def apply_portfolio_late_swaps(
        self,
        lineups: List[OptimizedLineup],
        pool: List[PlayerPoolEntry],
    ) -> List[OptimizedLineup]:
        """Apply late swaps across an entire portfolio with diversity tracking.

        Processes lineups sequentially, maintaining a running exposure count
        so that later lineups prefer different replacement players than
        earlier ones, avoiding convergent portfolios.
        """
        if not lineups:
            return lineups

        # Build portfolio-wide player exposure tracker
        portfolio_exposure: Dict[int, int] = {}
        for lu in lineups:
            for p in lu.players:
                portfolio_exposure[p.player_id] = (
                    portfolio_exposure.get(p.player_id, 0) + 1
                )

        total_lineups = len(lineups)
        updated: List[OptimizedLineup] = []

        for lu in lineups:
            swapped = self.apply_late_swaps(
                lu, pool,
                portfolio_exposure=portfolio_exposure,
                total_lineups=total_lineups,
            )
            updated.append(swapped)

        return updated

    # ------------------------------------------------------------------
    # Late-swap ILP re-optimizer (real-time game-lock aware)
    # ------------------------------------------------------------------

    def optimize_late_swap(
        self,
        lineup: OptimizedLineup,
        pool: List[PlayerPoolEntry],
        locked_slots: Dict[str, "LineupPlayer"],
        open_slots: List[str],
        platform: str,
        salary_cap: int,
        sport: str = "nba",
    ) -> Optional[OptimizedLineup]:
        """Re-optimize a lineup's open (unlocked) slots via ILP.

        Locked slots contain players whose games have already started —
        they cannot be changed.  The ILP creates variables *only* for
        the open slots and uses a reduced salary budget::

            remaining_cap = salary_cap - sum(locked_player.salary)

        Falls back to ``None`` (caller uses greedy) when PuLP is
        unavailable, the solver fails, or no variables can be created.
        """
        if not _PULP_AVAILABLE or pulp is None:
            return None

        if not open_slots:
            return None

        # ── Budget ──────────────────────────────────────────────────
        locked_ids = {p.player_id for p in locked_slots.values()}
        locked_salary_total = sum(p.salary for p in locked_slots.values())
        remaining_cap = salary_cap - locked_salary_total

        # ── Filter pool ────────────────────────────────────────────
        available = [
            p for p in pool
            if p.player_id not in locked_ids
            and p.injury_status not in ("Out", "Doubtful")
        ]
        if not available:
            return None

        player_lookup = {p.player_id: p for p in available}

        # ── Build ILP ──────────────────────────────────────────────
        import threading as _threading

        prob = pulp.LpProblem(
            f"LateSwap_{_threading.get_ident()}", pulp.LpMaximize,
        )

        x: Dict[Tuple[int, str], "pulp.LpVariable"] = {}
        vars_by_slot: Dict[str, List[Tuple[int, "pulp.LpVariable"]]] = {
            j: [] for j in open_slots
        }
        vars_by_player: Dict[int, List[Tuple[str, "pulp.LpVariable"]]] = {}

        for p in available:
            for j in open_slots:
                base = _base_slot(j)
                elig = self._get_slot_eligible_positions(base, platform, sport)
                if self._player_matches_slot(p.position, elig):
                    var = pulp.LpVariable(
                        f"ls_{p.player_id}_{j}", cat="Binary",
                    )
                    x[(p.player_id, j)] = var
                    vars_by_slot[j].append((p.player_id, var))
                    if p.player_id not in vars_by_player:
                        vars_by_player[p.player_id] = []
                    vars_by_player[p.player_id].append((j, var))

        if not x:
            logger.debug("[LateSwap-ILP] No eligible variables created")
            return None

        # ── Objective: maximize projected FP ────────────────────────
        prob += pulp.lpSum(
            player_lookup[pid].projected_fp * var
            for (pid, _), var in x.items()
        )

        # ── C1: Salary cap (reduced by locked salary) ──────────────
        prob += (
            pulp.lpSum(
                player_lookup[pid].salary * var
                for (pid, _), var in x.items()
            ) <= remaining_cap,
            "salary_cap",
        )

        # ── C2: Each open slot filled exactly once ──────────────────
        for j in open_slots:
            slot_vars = vars_by_slot[j]
            if slot_vars:
                prob += (
                    pulp.lpSum(var for _, var in slot_vars) == 1,
                    f"fill_{j}",
                )

        # ── C3: Each player used at most once ───────────────────────
        for pid, pv_list in vars_by_player.items():
            prob += (
                pulp.lpSum(var for _, var in pv_list) <= 1,
                f"uniq_{pid}",
            )

        # ── Solve ───────────────────────────────────────────────────
        from app.config.constants import (
            ILP_CBC_TIME_LIMIT,
            ILP_CBC_PRESOLVE,
            ILP_CBC_GAP_REL,
        )

        import warnings as _warnings

        with _warnings.catch_warnings():
            _warnings.filterwarnings(
                "ignore",
                message=".*warmStart requires keepFiles.*",
                category=UserWarning,
            )
            solver = pulp.PULP_CBC_CMD(
                msg=0,
                timeLimit=ILP_CBC_TIME_LIMIT,
                presolve=ILP_CBC_PRESOLVE,
                gapRel=ILP_CBC_GAP_REL,
            )
            try:
                status = prob.solve(solver)
            except Exception as e:
                logger.warning(f"[LateSwap-ILP] CBC solver error: {e}")
                return None

        if status == pulp.constants.LpStatusOptimal:
            pass
        elif prob.sol_status == pulp.constants.LpSolutionIntegerFeasible:
            logger.info(
                f"[LateSwap-ILP] Feasible incumbent "
                f"(obj={pulp.value(prob.objective):.2f})"
            )
        else:
            logger.debug(
                f"[LateSwap-ILP] Non-optimal: "
                f"{pulp.LpStatus.get(status, status)}"
            )
            return None

        # ── Extract ILP solution for open slots ─────────────────────
        ilp_result: Dict[str, PlayerPoolEntry] = {}
        for (pid, j), var in x.items():
            if var.varValue is not None and var.varValue > 0.5:
                ilp_result[j] = player_lookup[pid]

        if len(ilp_result) != len(open_slots):
            logger.warning(
                f"[LateSwap-ILP] Incomplete: "
                f"{len(ilp_result)}/{len(open_slots)} open slots"
            )
            return None

        # Salary verification
        ilp_salary = sum(p.salary for p in ilp_result.values())
        if ilp_salary > remaining_cap:
            logger.error(
                f"[LateSwap-ILP] SALARY VIOLATION: "
                f"${ilp_salary:,} > ${remaining_cap:,}"
            )
            return None

        # ── Merge locked + ILP → OptimizedLineup ───────────────────
        roster_slots = lineup.roster_slots
        indexed_roster = _index_slots(roster_slots)
        players: List[LineupPlayer] = []
        warnings: List[str] = list(lineup.warnings or [])

        for isl in indexed_roster:
            if isl in locked_slots:
                # Keep locked player unchanged
                players.append(locked_slots[isl])
            elif isl in ilp_result:
                p = ilp_result[isl]
                players.append(
                    LineupPlayer(
                        player_id=p.player_id,
                        player_name=p.player_name,
                        display_name=p.display_name or p.player_name,
                        position=p.position,
                        roster_slot=_base_slot(isl),
                        team_abbreviation=p.team_abbreviation,
                        salary=p.salary,
                        projected_fp=p.projected_fp,
                        floor_fp=p.floor_fp,
                        ceiling_fp=p.ceiling_fp,
                        projected_minutes=p.projected_minutes,
                        projected_stats=p.projected_stats,
                        dk_player_id=p.dk_player_id,
                    )
                )
            else:
                logger.warning(
                    f"[LateSwap-ILP] Missing slot {isl} in merge"
                )
                return None

        total_salary = sum(p.salary for p in players)
        total_fp = sum(p.projected_fp for p in players)
        total_floor = sum(p.floor_fp for p in players)
        total_ceil = sum(p.ceiling_fp for p in players)

        n_swapped = sum(
            1 for isl in indexed_roster
            if isl in ilp_result
            and ilp_result[isl].player_id != next(
                (lp.player_id for lp in lineup.players
                 if _index_slots(roster_slots)[lineup.players.index(lp)] == isl),
                None,
            )
        )
        if n_swapped > 0:
            warnings.append(
                f"ILP late-swap re-optimized {n_swapped} open slot(s)"
            )

        return OptimizedLineup(
            platform=lineup.platform,
            sport=lineup.sport,
            players=players,
            total_salary=total_salary,
            salary_remaining=salary_cap - total_salary,
            total_projected_fp=round(total_fp, 1),
            total_floor_fp=round(total_floor, 1),
            total_ceiling_fp=round(total_ceil, 1),
            salary_cap=salary_cap,
            roster_slots=roster_slots,
            warnings=warnings,
        )

    # ------------------------------------------------------------------
    # Live Late-Swap: Real-Time Game Telemetry + ILP Re-Optimization
    # ------------------------------------------------------------------

    def optimize_live_late_swap(
        self,
        dk_entry: Dict[str, Any],
        pool: List[PlayerPoolEntry],
        game_states: Dict[str, "GameState"],
        platform: str = "dk",
        salary_cap: int = DK_SALARY_CAP,
        sport: str = "nba",
    ) -> Optional[OptimizedLineup]:
        """Re-optimize a live DraftKings entry using real-time game telemetry.

        Ingests a JSON payload representing a live DK entry (current roster
        and remaining salary), checks each player's ``has_started`` flag
        from ``game_states``, locks started-game slots via ``x_i = 1``
        constraints, and re-optimizes open slots with updated projections.

        This replaces the two-step lock-then-optimize flow with a single
        ILP formulation where locked players participate in the model as
        fixed variables rather than being excluded from the variable space.

        Args:
            dk_entry: JSON dict representing the live DK entry.
                Expected shape::

                    {
                        "entry_id": "12345",
                        "contest_id": "67890",
                        "salary_remaining": 3200,
                        "roster": [
                            {
                                "roster_slot": "PG",
                                "player_name": "Kyrie Irving",
                                "player_id": 101108,
                                "dk_player_id": 20000567,
                                "team_abbreviation": "DAL",
                                "salary": 8400,
                                "projected_fp": 42.5,
                            },
                            ...
                        ]
                    }

            pool: Enriched player pool (from ``build_player_pool`` +
                ``_enrich_pool``).  Projections should reflect the latest
                InjurySyncService data.
            game_states: Dict mapping team abbreviation (BDL canonical)
                to ``GameState`` objects from ``LiveGameStateService``.
            platform: "dk" or "fd".
            salary_cap: Total salary cap (default 50,000 for DK).
            sport: "nba" or "cbb".

        Returns:
            A new ``OptimizedLineup`` with locked players preserved and
            open slots re-optimized, or ``None`` if the ILP fails.
        """
        if not _PULP_AVAILABLE or pulp is None:
            logger.warning("[LiveLateSwap-ILP] PuLP not installed")
            return None

        from app.services.live_game_state_service import (
            normalise_to_bdl,
        )

        # ── Parse the DK entry payload ───────────────────────────────
        roster_entries = dk_entry.get("roster", [])
        if not roster_entries:
            logger.warning("[LiveLateSwap-ILP] Empty roster in dk_entry")
            return None

        # Determine roster slots from the entry
        if platform == "dk":
            from app.sports import get_config as _get_sport_cfg
            roster_slots = list(_get_sport_cfg(sport).dk_roster_slots)
        else:
            roster_slots = list(FD_ROSTER_SLOTS)

        indexed_roster = _index_slots(roster_slots)

        if len(roster_entries) != len(indexed_roster):
            logger.error(
                "[LiveLateSwap-ILP] Roster size mismatch: "
                "entry=%d, expected=%d",
                len(roster_entries),
                len(indexed_roster),
            )
            return None

        # ── Classify each slot as locked (has_started) or open ────────
        locked_players: Dict[str, LineupPlayer] = {}  # indexed_slot → player
        open_slots: List[str] = []
        entry_player_ids: Set[int] = set()

        for entry_player, indexed_slot in zip(roster_entries, indexed_roster):
            team_abbr = (entry_player.get("team_abbreviation") or "").upper()
            bdl_team = normalise_to_bdl(team_abbr)

            gs = game_states.get(bdl_team)
            has_started = gs.has_started if gs else False

            pid = entry_player.get("player_id", 0)
            entry_player_ids.add(pid)

            lp = LineupPlayer(
                player_id=pid,
                player_name=entry_player.get("player_name", "Unknown"),
                display_name=entry_player.get(
                    "display_name",
                    entry_player.get("player_name", "Unknown"),
                ),
                position=entry_player.get("position", ""),
                roster_slot=_base_slot(indexed_slot),
                team_abbreviation=team_abbr,
                salary=entry_player.get("salary", 0),
                projected_fp=entry_player.get("projected_fp", 0.0),
                floor_fp=entry_player.get("floor_fp", 0.0),
                ceiling_fp=entry_player.get("ceiling_fp", 0.0),
                projected_minutes=entry_player.get("projected_minutes", 0.0),
                projected_stats=entry_player.get("projected_stats"),
                dk_player_id=entry_player.get("dk_player_id"),
            )

            if has_started:
                locked_players[indexed_slot] = lp
            else:
                open_slots.append(indexed_slot)

        if not open_slots:
            # All slots locked — return the entry as-is
            all_players = [locked_players[s] for s in indexed_roster]
            total_salary = sum(p.salary for p in all_players)
            return OptimizedLineup(
                platform=platform,
                sport=sport,
                players=all_players,
                total_salary=total_salary,
                salary_remaining=salary_cap - total_salary,
                total_projected_fp=round(
                    sum(p.projected_fp for p in all_players), 1
                ),
                total_floor_fp=round(
                    sum(p.floor_fp for p in all_players), 1
                ),
                total_ceiling_fp=round(
                    sum(p.ceiling_fp for p in all_players), 1
                ),
                salary_cap=salary_cap,
                roster_slots=roster_slots,
                warnings=["All slots locked — no swaps possible"],
            )

        logger.info(
            "[LiveLateSwap-ILP] %d locked, %d open slots to re-optimize",
            len(locked_players),
            len(open_slots),
        )

        # ── Compute reduced budget ───────────────────────────────────
        locked_salary = sum(p.salary for p in locked_players.values())
        remaining_cap = salary_cap - locked_salary

        # ── Filter pool for open-slot candidates ──────────────────────
        locked_ids = {p.player_id for p in locked_players.values()}
        available = [
            p for p in pool
            if p.player_id not in locked_ids
            and p.injury_status not in ("Out", "Doubtful")
            and p.salary <= remaining_cap
        ]

        if not available:
            logger.warning(
                "[LiveLateSwap-ILP] No eligible candidates in pool "
                "(locked_ids=%d, remaining_cap=$%d)",
                len(locked_ids),
                remaining_cap,
            )
            return None

        # Also filter out players whose games have already started
        # (they can't be added to open slots if their game is locked)
        pool_available: List[PlayerPoolEntry] = []
        for p in available:
            p_team = normalise_to_bdl(
                (p.team_abbreviation or "").upper()
            )
            p_gs = game_states.get(p_team)
            if p_gs and p_gs.has_started:
                continue  # Can't swap in a player whose game started
            pool_available.append(p)

        if not pool_available:
            logger.warning(
                "[LiveLateSwap-ILP] All eligible players have "
                "started games"
            )
            return None

        player_lookup = {p.player_id: p for p in pool_available}

        # ── Build ILP ─────────────────────────────────────────────────
        import threading as _threading

        prob = pulp.LpProblem(
            f"LiveLateSwap_{_threading.get_ident()}", pulp.LpMaximize,
        )

        x: Dict[Tuple[int, str], "pulp.LpVariable"] = {}
        vars_by_slot: Dict[str, List[Tuple[int, "pulp.LpVariable"]]] = {
            j: [] for j in open_slots
        }
        vars_by_player: Dict[int, List[Tuple[str, "pulp.LpVariable"]]] = {}

        for p in pool_available:
            for j in open_slots:
                base = _base_slot(j)
                elig = self._get_slot_eligible_positions(
                    base, platform, sport
                )
                if self._player_matches_slot(p.position, elig):
                    var = pulp.LpVariable(
                        f"lls_{p.player_id}_{j}", cat="Binary",
                    )
                    x[(p.player_id, j)] = var
                    vars_by_slot[j].append((p.player_id, var))
                    if p.player_id not in vars_by_player:
                        vars_by_player[p.player_id] = []
                    vars_by_player[p.player_id].append((j, var))

        if not x:
            logger.debug(
                "[LiveLateSwap-ILP] No eligible variables created "
                "for %d open slots",
                len(open_slots),
            )
            return None

        # ── Objective: maximize projected FP ──────────────────────────
        prob += pulp.lpSum(
            player_lookup[pid].projected_fp * var
            for (pid, _), var in x.items()
        )

        # ── C1: Salary cap (reduced by locked salary) ────────────────
        prob += (
            pulp.lpSum(
                player_lookup[pid].salary * var
                for (pid, _), var in x.items()
            ) <= remaining_cap,
            "salary_cap",
        )

        # ── C2: Each open slot filled exactly once ────────────────────
        for j in open_slots:
            slot_vars = vars_by_slot[j]
            if slot_vars:
                prob += (
                    pulp.lpSum(var for _, var in slot_vars) == 1,
                    f"fill_{j}",
                )
            else:
                logger.warning(
                    "[LiveLateSwap-ILP] Slot %s has zero eligible "
                    "candidates",
                    j,
                )
                return None

        # ── C3: Each player used at most once ─────────────────────────
        for pid, pv_list in vars_by_player.items():
            prob += (
                pulp.lpSum(var for _, var in pv_list) <= 1,
                f"uniq_{pid}",
            )

        # ── Solve ─────────────────────────────────────────────────────
        from app.config.constants import (
            ILP_CBC_TIME_LIMIT,
            ILP_CBC_PRESOLVE,
            ILP_CBC_GAP_REL,
        )

        import warnings as _warnings

        with _warnings.catch_warnings():
            _warnings.filterwarnings(
                "ignore",
                message=".*warmStart requires keepFiles.*",
                category=UserWarning,
            )
            solver = pulp.PULP_CBC_CMD(
                msg=0,
                timeLimit=ILP_CBC_TIME_LIMIT,
                presolve=ILP_CBC_PRESOLVE,
                gapRel=ILP_CBC_GAP_REL,
            )
            try:
                status = prob.solve(solver)
            except Exception as e:
                logger.warning(
                    "[LiveLateSwap-ILP] CBC solver error: %s", e
                )
                return None

        if status == pulp.constants.LpStatusOptimal:
            pass
        elif prob.sol_status == pulp.constants.LpSolutionIntegerFeasible:
            logger.info(
                "[LiveLateSwap-ILP] Feasible incumbent "
                "(obj=%.2f)",
                pulp.value(prob.objective),
            )
        else:
            logger.debug(
                "[LiveLateSwap-ILP] Non-optimal: %s",
                pulp.LpStatus.get(status, status),
            )
            return None

        # ── Extract ILP solution for open slots ──────────────────────
        ilp_result: Dict[str, PlayerPoolEntry] = {}
        for (pid, j), var in x.items():
            if var.varValue is not None and var.varValue > 0.5:
                ilp_result[j] = player_lookup[pid]

        if len(ilp_result) != len(open_slots):
            logger.warning(
                "[LiveLateSwap-ILP] Incomplete: %d/%d open slots filled",
                len(ilp_result),
                len(open_slots),
            )
            return None

        # Salary verification
        ilp_salary = sum(p.salary for p in ilp_result.values())
        if ilp_salary > remaining_cap:
            logger.error(
                "[LiveLateSwap-ILP] SALARY VIOLATION: "
                "$%d > $%d remaining",
                ilp_salary,
                remaining_cap,
            )
            return None

        # ── Merge locked + ILP → OptimizedLineup ─────────────────────
        players: List[LineupPlayer] = []
        warnings_list: List[str] = []

        for isl in indexed_roster:
            if isl in locked_players:
                players.append(locked_players[isl])
            elif isl in ilp_result:
                p = ilp_result[isl]
                players.append(
                    LineupPlayer(
                        player_id=p.player_id,
                        player_name=p.player_name,
                        display_name=p.display_name or p.player_name,
                        position=p.position,
                        roster_slot=_base_slot(isl),
                        team_abbreviation=p.team_abbreviation,
                        salary=p.salary,
                        projected_fp=p.projected_fp,
                        floor_fp=p.floor_fp,
                        ceiling_fp=p.ceiling_fp,
                        projected_minutes=p.projected_minutes,
                        projected_stats=p.projected_stats,
                        dk_player_id=p.dk_player_id,
                    )
                )
            else:
                logger.warning(
                    "[LiveLateSwap-ILP] Missing slot %s in merge", isl
                )
                return None

        total_salary = sum(p.salary for p in players)
        total_fp = sum(p.projected_fp for p in players)
        total_floor = sum(p.floor_fp for p in players)
        total_ceil = sum(p.ceiling_fp for p in players)

        # Count actual swaps (players that differ from the original entry)
        original_ids = [
            e.get("player_id", 0) for e in roster_entries
        ]
        n_swapped = sum(
            1 for i, isl in enumerate(indexed_roster)
            if isl in ilp_result
            and ilp_result[isl].player_id != original_ids[i]
        )

        if n_swapped > 0:
            warnings_list.append(
                f"Live ILP re-optimized {n_swapped} of "
                f"{len(open_slots)} open slot(s) "
                f"(${remaining_cap:,} budget, "
                f"{len(pool_available)} candidates)"
            )

        logger.info(
            "[LiveLateSwap-ILP] Solution: salary=$%d/%d, "
            "fp=%.1f, swaps=%d/%d open",
            total_salary,
            salary_cap,
            total_fp,
            n_swapped,
            len(open_slots),
        )

        return OptimizedLineup(
            platform=platform,
            sport=sport,
            players=players,
            total_salary=total_salary,
            salary_remaining=salary_cap - total_salary,
            total_projected_fp=round(total_fp, 1),
            total_floor_fp=round(total_floor, 1),
            total_ceiling_fp=round(total_ceil, 1),
            salary_cap=salary_cap,
            roster_slots=roster_slots,
            warnings=warnings_list,
        )
