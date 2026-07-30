"""Persistent, sport-keyed alias map for CSV-imported projections.

When a user uploads a projections CSV, some rows fail to match any pool
player by name (e.g. "Quenton Jackson" in the CSV but the rotation engine
has him as "Quentin Jackson", or a Jr./III suffix mismatch).  Without
intervention these rows silently fall back to the rotation engine's
projection.

This module provides a JSON-backed alias map that maps normalized CSV
names to the canonical normalized pool name, so subsequent imports apply
the same matches automatically. Aliases are partitioned by **sport** so
an "M. Brown" mapping made for an NBA slate can never collide with an
unrelated MLB or NFL player of the same surname.

File format (v2 — sport-partitioned)::

    {
      "version": 2,
      "aliases": {
        "nba": {
          "<csv_normalized_name>": { "canonical_name": ..., ... },
          ...
        },
        "nfl": { ... },
        "mlb": { ... }
      }
    }

For backward compat, files written under v1 (flat ``aliases`` dict with
no sport key) are migrated to ``aliases.nba`` on first read so existing
NBA aliases keep working.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# File lives next to backend root, alongside custom_projections.csv.
_ALIAS_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "projection_aliases.json")
)

_lock = threading.Lock()
# Top-level shape: {sport: {csv_norm: entry}}.
_aliases: Dict[str, Dict[str, Dict]] = {}
_mtime: float = 0.0

_DEFAULT_SPORT = "nba"


def _atomic_write(path: str, payload: dict) -> None:
    """Write JSON via temp file + replace so a crash never corrupts the store."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _normalize_sport(sport: Optional[str]) -> str:
    """Lower-case + default for None/empty."""
    if not sport:
        return _DEFAULT_SPORT
    return sport.strip().lower()


def _migrate_v1_to_v2(payload: dict) -> Dict[str, Dict[str, Dict]]:
    """Lift a flat v1 ``aliases`` dict into the sport-keyed v2 shape.

    All v1 entries are assumed to be NBA (they predate multi-sport
    support). Persisted v2 files round-trip unchanged.
    """
    raw = payload.get("aliases", {}) or {}
    if not raw:
        return {}
    # v2 already has the sport-keyed shape if the values are dicts of
    # dicts and every nested key looks like an alias entry (has either
    # 'canonical_normalized' or 'canonical_name'). Detect that.
    sample_val = next(iter(raw.values()), None)
    looks_v2 = (
        isinstance(sample_val, dict)
        and (
            not sample_val
            or all(
                isinstance(v, dict)
                and ("canonical_normalized" in v or "canonical_name" in v)
                for v in sample_val.values()
            )
        )
    )
    if looks_v2:
        return raw
    # v1: flat {csv_norm: entry} → migrate everything to nba.
    logger.info(
        "[ProjectionAlias] Migrating v1 alias file (%d entries) to v2 "
        "sport-keyed schema; existing entries treated as %r.",
        len(raw), _DEFAULT_SPORT,
    )
    return {_DEFAULT_SPORT: raw}


def _load_from_disk() -> None:
    """Read the alias file into the in-memory cache. No-op if missing."""
    global _aliases, _mtime
    if not os.path.isfile(_ALIAS_PATH):
        _aliases = {}
        _mtime = 0.0
        return
    try:
        mtime = os.path.getmtime(_ALIAS_PATH)
        if mtime == _mtime and _aliases:
            return  # already current
        with open(_ALIAS_PATH, "r", encoding="utf-8") as f:
            payload = json.load(f)
        if not isinstance(payload, dict):
            logger.warning(
                "[ProjectionAlias] %s is not a JSON object, ignoring",
                _ALIAS_PATH,
            )
            _aliases = {}
            _mtime = mtime
            return
        _aliases = _migrate_v1_to_v2(payload)
        _mtime = mtime
        total = sum(len(v) for v in _aliases.values())
        logger.info(
            "[ProjectionAlias] Loaded %d aliases (%s) from %s",
            total,
            ", ".join(f"{k}={len(v)}" for k, v in _aliases.items()) or "empty",
            _ALIAS_PATH,
        )
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(
            "[ProjectionAlias] Failed to load %s: %s. Starting empty.",
            _ALIAS_PATH, exc,
        )
        _aliases = {}
        _mtime = 0.0


def _save_to_disk() -> None:
    """Persist the in-memory cache to disk."""
    global _mtime
    payload = {"version": 2, "aliases": _aliases}
    try:
        _atomic_write(_ALIAS_PATH, payload)
        _mtime = os.path.getmtime(_ALIAS_PATH)
    except OSError as exc:
        logger.error("[ProjectionAlias] Failed to write %s: %s", _ALIAS_PATH, exc)
        raise


# ── Public API ─────────────────────────────────────────────────────


def get_alias(csv_normalized_name: str, sport: str = _DEFAULT_SPORT) -> Optional[str]:
    """Return the canonical normalized name for a CSV name within ``sport``.

    Sport-partitioned: an alias saved under ``"nba"`` is invisible to
    callers passing ``"nfl"`` (and vice versa), preventing cross-sport
    name-collision bugs.
    """
    sport_key = _normalize_sport(sport)
    with _lock:
        _load_from_disk()
        entry = _aliases.get(sport_key, {}).get(csv_normalized_name)
        if not entry:
            return None
        return entry.get("canonical_normalized")


def list_aliases(sport: Optional[str] = None) -> Dict[str, Dict]:
    """Return aliases for one sport, or all sports flattened.

    Parameters
    ----------
    sport : str, optional
        When given, returns ``{csv_norm: entry}`` for that sport only.
        When omitted, returns the full ``{sport: {csv_norm: entry}}``
        nested dict so callers can iterate every partition.
    """
    with _lock:
        _load_from_disk()
        if sport is None:
            return {
                s: {k: dict(v) for k, v in entries.items()}
                for s, entries in _aliases.items()
            }
        sport_key = _normalize_sport(sport)
        return {
            k: dict(v)
            for k, v in _aliases.get(sport_key, {}).items()
        }


def add_alias(
    csv_normalized_name: str,
    canonical_name: str,
    canonical_normalized: str,
    sport: str = _DEFAULT_SPORT,
    player_id: Optional[int] = None,
    team: Optional[str] = None,
    source: str = "manual",
) -> Dict:
    """Add or replace an alias under ``sport``. Persists immediately."""
    if not csv_normalized_name or not canonical_normalized:
        raise ValueError("csv_normalized_name and canonical_normalized are required")
    sport_key = _normalize_sport(sport)
    entry = {
        "canonical_name": canonical_name,
        "canonical_normalized": canonical_normalized,
        "sport": sport_key,
        "player_id": player_id,
        "team": team,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": source,
    }
    with _lock:
        _load_from_disk()
        _aliases.setdefault(sport_key, {})[csv_normalized_name] = entry
        _save_to_disk()
    logger.info(
        "[ProjectionAlias] [%s] Saved %r -> %r (player_id=%s, team=%s)",
        sport_key, csv_normalized_name, canonical_normalized, player_id, team,
    )
    return entry


def remove_alias(csv_normalized_name: str, sport: str = _DEFAULT_SPORT) -> bool:
    """Remove an alias under ``sport``. Returns True if it existed."""
    sport_key = _normalize_sport(sport)
    with _lock:
        _load_from_disk()
        bucket = _aliases.get(sport_key)
        if not bucket or csv_normalized_name not in bucket:
            return False
        del bucket[csv_normalized_name]
        # Drop empty sport buckets so list_aliases doesn't show ghost sports.
        if not bucket:
            _aliases.pop(sport_key, None)
        _save_to_disk()
    logger.info("[ProjectionAlias] [%s] Removed %r", sport_key, csv_normalized_name)
    return True


def clear_aliases(sport: Optional[str] = None) -> int:
    """Wipe aliases.

    Parameters
    ----------
    sport : str, optional
        When given, clears that sport only. When omitted, wipes
        every sport's aliases. Returns the count removed.
    """
    with _lock:
        _load_from_disk()
        if sport is None:
            n = sum(len(v) for v in _aliases.values())
            _aliases.clear()
        else:
            sport_key = _normalize_sport(sport)
            n = len(_aliases.get(sport_key, {}))
            _aliases.pop(sport_key, None)
        _save_to_disk()
    logger.info(
        "[ProjectionAlias] Cleared %d aliases (%s)",
        n, sport or "all sports",
    )
    return n
