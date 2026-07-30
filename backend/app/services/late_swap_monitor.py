"""Late-Swap Monitoring Service.

Monitors upcoming games for at-risk players (injury updates, late scratches)
and provides swap suggestions using the existing lineup optimizer's swap engine.

Designed to be polled periodically by the frontend (every 5 min) when games
are approaching tip-off.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.models.lineup import OptimizedLineup, PlayerPoolEntry, LateSwapSuggestion

logger = logging.getLogger(__name__)

# Injury statuses considered "at-risk"
AT_RISK_STATUSES = {"Questionable", "Doubtful", "Game Time Decision", "GTD"}
OUT_STATUSES = {"Out", "Inactive", "Suspended"}


class LateSwapMonitor:
    """Monitors games for late-breaking changes that affect lineups.

    Tracks three categories of pre-lock changes:

    1. **Injury updates** — players ruled out, downgraded, or upgraded
       (via InjuryService with 5-min cache TTL).
    2. **News updates** — relevant headlines about player availability
       (via NewsService keyword scanning).
    3. **Roster changes** — players newly marked Out on DK, added to
       or removed from the DK pool (via DKDraftablesService status
       refresh).  This catches suspensions, emergency signings,
       G-League call-ups, and last-minute ineligibility changes.
    """

    def __init__(
        self,
        injury_service,
        game_service,
        news_service,
        lineup_optimizer_service=None,
        dk_draftables_service=None,
    ):
        self._injury = injury_service
        self._games = game_service
        self._news = news_service
        self._optimizer = lineup_optimizer_service
        self._dk = dk_draftables_service
        # Snapshot of last-seen draftables per draft group, used by
        # detect_roster_changes() to compute diffs.
        self._dk_snapshots: Dict[int, list] = {}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _flatten_injuries(injuries) -> List[Dict[str, str]]:
        """Normalise injuries into a flat list of dicts.

        get_all_injuries() can return:
        - A list of Pydantic PlayerStatus objects  (production path)
        - A list of plain dicts                    (some tests / CBB)
        - A dict mapping team -> list of dicts     (alternate format)
        """
        flat: List[Dict[str, str]] = []
        if isinstance(injuries, dict):
            # {team: [player_dict, ...]}
            for team, players in injuries.items():
                for p in (players if isinstance(players, list) else [players]):
                    entry = dict(p) if isinstance(p, dict) else {}
                    entry.setdefault("team", team)
                    flat.append(entry)
        elif isinstance(injuries, (list, tuple)):
            for inj in injuries:
                if isinstance(inj, dict):
                    flat.append(inj)
                else:
                    # Pydantic model — convert to dict
                    flat.append({
                        "player_name": getattr(inj, "player_name", ""),
                        "status": getattr(inj, "status", ""),
                        "injury_description": getattr(inj, "injury_description", ""),
                        "team": getattr(inj, "team", ""),
                        "comment": getattr(inj, "comment", ""),
                    })
        return flat

    def get_at_risk_players(
        self,
        lineups: List[OptimizedLineup],
        game_date: Optional[str] = None,
        minutes_until_lock: int = 60,
    ) -> List[Dict[str, Any]]:
        """Find players in lineups who have concerning injury/status updates.

        Returns list of at-risk player dicts with:
        - player_name, team, position, slot
        - injury_status, injury_detail
        - risk_level: "high" (Out/Doubtful), "medium" (Questionable/GTD)
        - lineup_index (which lineup they appear in)
        """
        at_risk = []

        # Get current injury report
        try:
            injuries = self._injury.get_all_injuries()
        except Exception as e:
            logger.warning(f"[LateSwap] Failed to fetch injuries: {e}")
            injuries = []

        # Normalise to flat list of dicts and build lookup
        flat_injuries = self._flatten_injuries(injuries)
        injury_lookup: Dict[str, Dict[str, str]] = {}
        for inj in flat_injuries:
            name = inj.get("player_name", "") or ""
            if name:
                injury_lookup[name.lower()] = {
                    "status": inj.get("status", ""),
                    "description": inj.get("injury_description", "") or inj.get("comment", ""),
                    "team": inj.get("team", ""),
                }

        # Check each lineup's players
        def _player_attr(obj, *keys, default=""):
            """Get attribute from dict or object, trying keys in order."""
            for k in keys:
                if isinstance(obj, dict):
                    val = obj.get(k)
                else:
                    val = getattr(obj, k, None)
                if val is not None:
                    return val
            return default

        for li, lineup in enumerate(lineups):
            players = lineup.players if hasattr(lineup, "players") else (lineup.get("players", []) if isinstance(lineup, dict) else [])
            for player in players:
                pname = _player_attr(player, "player_name", "name", default="")
                if not pname:
                    continue

                injury_info = injury_lookup.get(pname.lower())
                if not injury_info:
                    continue

                status = injury_info.get("status", "")
                if status in AT_RISK_STATUSES or status in OUT_STATUSES:
                    risk_level = "high" if status in OUT_STATUSES or status == "Doubtful" else "medium"
                    at_risk.append({
                        "player_name": pname,
                        "team": _player_attr(player, "team", "team_abbreviation", default=""),
                        "position": _player_attr(player, "position", default=""),
                        "slot": _player_attr(player, "slot", default=""),
                        "salary": _player_attr(player, "salary", default=0),
                        "projected_fp": _player_attr(player, "projected_fp", default=0),
                        "injury_status": status,
                        "injury_detail": injury_info.get("description", ""),
                        "risk_level": risk_level,
                        "lineup_index": li,
                    })

        # Sort: high risk first, then by projected_fp descending
        at_risk.sort(
            key=lambda x: (0 if x["risk_level"] == "high" else 1, -x.get("projected_fp", 0))
        )
        return at_risk

    def check_for_updates(
        self,
        game_date: Optional[str] = None,
        since_minutes: int = 30,
    ) -> Dict[str, Any]:
        """Check for recent injury/news updates that could affect lineups.

        Returns summary of recent changes with relevant player updates.
        """
        updates = {
            "injury_updates": [],
            "news_updates": [],
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "has_critical_updates": False,
        }

        # Check injury changes
        try:
            injuries = self._injury.get_all_injuries()
            flat_injuries = self._flatten_injuries(injuries)
            for inj in flat_injuries:
                status = inj.get("status", "") or ""
                if status in AT_RISK_STATUSES or status in OUT_STATUSES:
                    update = {
                        "player_name": inj.get("player_name", ""),
                        "team": inj.get("team", ""),
                        "status": status,
                        "detail": inj.get("injury_description", "") or inj.get("comment", ""),
                        "is_critical": status in OUT_STATUSES,
                    }
                    updates["injury_updates"].append(update)
                    if status in OUT_STATUSES:
                        updates["has_critical_updates"] = True
        except Exception as e:
            logger.warning(f"[LateSwap] Injury check failed: {e}")

        # Check recent news — get_news() may return (List[NewsItem], bool),
        # a list, or an object with .articles attribute
        try:
            news_result = self._news.get_news(limit=20)
            # Support multiple return shapes
            if isinstance(news_result, (list, tuple)):
                articles = news_result[0] if isinstance(news_result, tuple) else news_result
            elif hasattr(news_result, "articles"):
                articles = news_result.articles
            else:
                articles = []
            for article in (articles or [])[:10]:
                if isinstance(article, dict):
                    title = article.get("title", "") or article.get("headline", "") or ""
                    desc = article.get("description", "") or ""
                    source = article.get("source", "")
                    ts = article.get("published_at", "") or article.get("timestamp", "")
                else:
                    # Prefer .title (standard), fall back to .headline
                    title = ""
                    for attr in ("title", "headline"):
                        val = getattr(article, attr, None)
                        if isinstance(val, str) and val:
                            title = val
                            break
                    desc_val = getattr(article, "description", None)
                    desc = desc_val if isinstance(desc_val, str) else ""
                    source = getattr(article, "source", "") or ""
                    ts = getattr(article, "published_at", None) or getattr(article, "timestamp", None) or ""
                    if not isinstance(source, str):
                        source = ""
                    if not isinstance(ts, str):
                        ts = str(ts) if ts else ""
                content = title + " " + desc
                content_lower = content.lower()
                # Look for injury/status keywords
                if any(kw in content_lower for kw in [
                    "ruled out", "doubtful", "questionable", "scratched",
                    "will not play", "sitting out", "rest", "inactive",
                    "upgraded", "cleared", "will play",
                    "suspended", "waived", "signed", "called up",
                    "g-league", "two-way", "traded",
                ]):
                    updates["news_updates"].append({
                        "title": title,
                        "source": source,
                        "timestamp": ts,
                        "is_positive": any(
                            kw in content_lower
                            for kw in ["upgraded", "cleared", "will play"]
                        ),
                    })
        except Exception as e:
            logger.warning(f"[LateSwap] News check failed: {e}")

        return updates

    def suggest_swaps(
        self,
        lineups: List[OptimizedLineup],
        pool: List[PlayerPoolEntry],
    ) -> Dict[str, Any]:
        """Generate swap suggestions for all lineups using the optimizer's swap engine.

        Returns per-lineup swap suggestions plus a summary.
        """
        if not self._optimizer:
            return {"error": "Lineup optimizer not available", "swaps": [], "total_swaps": 0}

        all_swaps = []
        total = 0

        for i, lineup in enumerate(lineups):
            try:
                swaps = self._optimizer.detect_late_swaps(lineup, pool)
                # detect_late_swaps returns List[Dict], not Pydantic models
                swap_dicts = [
                    s if isinstance(s, dict) else s.model_dump()
                    for s in swaps
                ]
                all_swaps.append({
                    "lineup_index": i,
                    "swaps": swap_dicts,
                    "swap_count": len(swaps),
                })
                total += len(swaps)
            except Exception as e:
                logger.warning(f"[LateSwap] Swap detection failed for lineup {i}: {e}")
                all_swaps.append({
                    "lineup_index": i,
                    "swaps": [],
                    "swap_count": 0,
                    "error": str(e),
                })

        return {
            "lineup_swaps": all_swaps,
            "total_swaps": total,
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }

    def detect_roster_changes(
        self,
        draft_group_id: int,
    ) -> Dict[str, Any]:
        """Detect DK roster changes since the last check.

        Compares a fresh fetch of DK draftables against the previous
        snapshot to find:
        - Players newly marked as Out (status changed to "O")
        - Players newly added to the pool (emergency signings)
        - Players removed from the pool (waived/ineligible)

        The first call for a ``draft_group_id`` stores a baseline
        snapshot and returns empty diffs (nothing to compare against).
        Subsequent calls return the delta since the last check.

        This method is called by ``get_monitor_status()`` and can also
        be called independently for lightweight polling.
        """
        if not self._dk:
            return {
                "roster_changes": [],
                "newly_out": [],
                "newly_added": [],
                "newly_removed": [],
            }

        try:
            previous = self._dk_snapshots.get(draft_group_id, [])

            if not previous:
                # First check — store baseline, no diffs to report
                baseline = self._dk.get_draftables(draft_group_id)
                self._dk_snapshots[draft_group_id] = baseline
                return {
                    "roster_changes": [],
                    "newly_out": [],
                    "newly_added": [],
                    "newly_removed": [],
                    "baseline_stored": True,
                    "player_count": len(baseline),
                }

            changes = self._dk.detect_status_changes(
                draft_group_id, previous
            )

            # Update snapshot for next comparison
            self._dk_snapshots[draft_group_id] = self._dk.get_draftables(
                draft_group_id
            )

            # Build summary for the monitor
            all_changes = []
            for item in changes.get("newly_out", []):
                all_changes.append({
                    "type": "status_change",
                    "player": item["player"],
                    "team": item["team"],
                    "detail": f"Status changed to OUT (was {item['old_status']})",
                    "severity": "critical",
                })
            for item in changes.get("newly_added", []):
                all_changes.append({
                    "type": "player_added",
                    "player": item["player"],
                    "team": item["team"],
                    "detail": f"Added to DK pool (${item['salary']})",
                    "severity": "info",
                })
            for item in changes.get("newly_removed", []):
                all_changes.append({
                    "type": "player_removed",
                    "player": item["player"],
                    "team": item["team"],
                    "detail": f"Removed from DK pool (was ${item['salary']})",
                    "severity": "warning",
                })

            return {
                "roster_changes": all_changes,
                **changes,
            }

        except Exception as e:
            logger.warning(f"[LateSwap] Roster change detection failed: {e}")
            return {
                "roster_changes": [],
                "newly_out": [],
                "newly_added": [],
                "newly_removed": [],
                "error": str(e),
            }

    def get_monitor_status(
        self,
        lineups: List[OptimizedLineup],
        pool: List[PlayerPoolEntry],
        game_date: Optional[str] = None,
        draft_group_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Full monitoring snapshot: at-risk players + updates + roster changes + swaps.

        This is the main endpoint method — combines all checks into one response.

        When ``draft_group_id`` is provided, also performs a DK draftables
        refresh to detect roster changes (newly out, added, or removed
        players) since the last check.  This catches suspensions, emergency
        signings, and last-minute DK eligibility changes that don't appear
        on the standard injury report.
        """
        at_risk = self.get_at_risk_players(lineups, game_date)
        updates = self.check_for_updates(game_date)

        # Roster change detection (DK draftables diff)
        roster_changes: Dict[str, Any] = {}
        if draft_group_id:
            roster_changes = self.detect_roster_changes(draft_group_id)
            # If new "Out" players are detected, flag as critical
            if roster_changes.get("newly_out"):
                updates["has_critical_updates"] = True

        swaps = self.suggest_swaps(lineups, pool) if at_risk else {"lineup_swaps": [], "total_swaps": 0}

        return {
            "at_risk_players": at_risk,
            "at_risk_count": len(at_risk),
            "recent_updates": updates,
            "roster_changes": roster_changes,
            "swap_suggestions": swaps,
            "needs_attention": (
                len(at_risk) > 0
                or updates.get("has_critical_updates", False)
                or bool(roster_changes.get("roster_changes"))
            ),
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }
