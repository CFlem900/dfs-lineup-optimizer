"""Contest Recommender Service — ROI-maximising contest selection.

Fetches all available DraftKings contests from the lobby API, scores
each on four axes (overlay, prize structure, field strength, calibration
fit), and returns a ranked list with confidence scores and recommended
entry counts.

Completely isolated from lineup generation.
"""

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from typing import Dict, List, Optional, Tuple

import httpx

from app.models.ai import ContestRecommendation, ContestScore
from app.services.http_resilience import resilient_get, APIGroup

logger = logging.getLogger(__name__)

# ── DK Lobby API ─────────────────────────────────────────────────────

_DK_LOBBY_URLS = {
    "nba": "https://www.draftkings.com/lobby/getcontests?sport=NBA",
    "cbb": "https://www.draftkings.com/lobby/getcontests?sport=CBB",
}

_REQUEST_TIMEOUT = 15
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# ── Scoring weights ──────────────────────────────────────────────────

_W_OVERLAY = 0.30
_W_STRUCTURE = 0.25
_W_FIELD_STRENGTH = 0.25
_W_CALIBRATION = 0.20

# ── DFS industry standard rake ───────────────────────────────────────

_DK_RAKE_PCT = 10.0  # DraftKings keeps ~10% of entry fees

# ── Parallel scoring ────────────────────────────────────────────────
# DK happily serves the lobby + per-contest detail endpoints under
# moderate parallelism. 16 keeps total throughput high while staying
# well below DK's per-host rate limits.
_SCORE_CONCURRENCY = 16


class ContestRecommenderService:
    """Scores and ranks DraftKings contests by expected ROI potential."""

    def __init__(
        self,
        dk_contest_detail_service,
        calibration_service,
    ):
        self._dk_contests = dk_contest_detail_service
        self._calibration = calibration_service

        # Lobby cache: key = sport → (timestamp, list of contest dicts)
        self._lobby_cache: Dict[str, Tuple[float, List[Dict]]] = {}
        self._lobby_ttl = 600  # 10 minutes

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def recommend_contests(
        self,
        platform: str = "dk",
        sport: str = "nba",
        game_date: Optional[str] = None,
        draft_group_id: Optional[int] = None,
    ) -> ContestRecommendation:
        """Fetch all available contests and return scored recommendations.

        Parameters
        ----------
        platform : str
            "dk" (only DraftKings supported currently).
        sport : str
            "nba" or "cbb".
        game_date : str, optional
            ISO date (YYYY-MM-DD). Defaults to today.
        draft_group_id : int, optional
            Filter to contests on a specific DraftGroup (slate).
        """
        t0 = time.time()
        target = game_date or date.today().isoformat()

        # 1. Fetch lobby contests
        raw_contests = self._fetch_lobby_contests(sport)
        if not raw_contests:
            logger.warning("[ContestRec] No contests found in lobby")
            return ContestRecommendation(
                slate_date=target,
                platform=platform,
                sport=sport,
                draft_group_id=draft_group_id,
                generation_time_ms=int((time.time() - t0) * 1000),
            )

        # 2. Filter by draft_group_id if provided
        if draft_group_id is not None:
            raw_contests = [
                c for c in raw_contests
                if c.get("draft_group_id") == draft_group_id
            ]

        logger.info(
            f"[ContestRec] Scoring {len(raw_contests)} contests "
            f"(sport={sport}, dg={draft_group_id})"
        )

        # 3. Score each contest in parallel — _score_lobby_contest issues
        # an independent HTTP call per contest, so a thread pool fans the
        # work out by ~_SCORE_CONCURRENCY×.
        scored: List[ContestScore] = []
        max_workers = min(_SCORE_CONCURRENCY, max(1, len(raw_contests)))
        with ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="ContestScore"
        ) as pool:
            future_to_contest = {
                pool.submit(self._score_lobby_contest, rc, sport): rc
                for rc in raw_contests
            }
            for fut in as_completed(future_to_contest):
                rc = future_to_contest[fut]
                try:
                    cs = fut.result()
                    if cs:
                        scored.append(cs)
                except Exception as e:
                    logger.debug(f"[ContestRec] Skip contest {rc.get('id')}: {e}")

        # 4. Sort by overall_score descending
        scored.sort(key=lambda c: c.overall_score, reverse=True)

        total_entries = sum(c.recommended_entries for c in scored)
        elapsed = int((time.time() - t0) * 1000)

        logger.info(
            f"[ContestRec] Scored {len(scored)} contests in {elapsed}ms, "
            f"recommending {total_entries} total entries"
        )

        return ContestRecommendation(
            slate_date=target,
            platform=platform,
            sport=sport,
            draft_group_id=draft_group_id,
            contests=scored,
            total_contests_scanned=len(raw_contests),
            total_entries_recommended=total_entries,
            generation_time_ms=elapsed,
        )

    def score_contest_by_id(self, contest_id: str) -> Optional[ContestScore]:
        """Score a single contest by fetching its full detail."""
        detail = self._dk_contests.get_contest_detail(contest_id)
        if not detail:
            return None
        return self.score_contest(detail)

    def score_contest(self, detail) -> ContestScore:
        """Score a ContestDetail object on four axes.

        Parameters
        ----------
        detail : ContestDetail
            From ``DKContestDetailService.get_contest_detail()``.
        """
        contest_type = detail.inferred_contest_type()
        is_gpp = contest_type in ("gpp", "single_entry")

        # ── Component 1: Overlay Score ───────────────────────────────
        overlay = self._calc_overlay_score(
            detail.overlay_risk, detail.is_guaranteed
        )

        # ── Component 2: Structure Score ─────────────────────────────
        structure = self._calc_structure_score(
            detail.top_heavy_pct,
            detail.roi_breakeven_percentile,
            is_gpp,
        )

        # ── Component 3: Field Strength Score ────────────────────────
        fill_rate = (
            detail.current_entries / detail.max_entries
            if detail.max_entries > 0 else 1.0
        )
        field_strength = self._calc_field_strength_score(
            fill_rate,
            detail.max_entries_per_user,
            detail.entry_fee,
        )

        # ── Component 4: Calibration Fit Score ───────────────────────
        calibration_fit = self._calc_calibration_fit_score()

        # ── Aggregate ────────────────────────────────────────────────
        overall = (
            _W_OVERLAY * overlay
            + _W_STRUCTURE * structure
            + _W_FIELD_STRENGTH * field_strength
            + _W_CALIBRATION * calibration_fit
        )
        overall = round(min(100.0, max(0.0, overall)), 1)

        # ── Confidence ───────────────────────────────────────────────
        confidence = overall / 100.0
        if calibration_fit < 30:
            confidence *= 0.70
        if fill_rate > 0.95:
            confidence *= 0.85
        confidence = round(min(1.0, max(0.0, confidence)), 3)

        # ── Entry recommendation ─────────────────────────────────────
        rec_entries = self._recommend_entries(
            overall, contest_type, detail.max_entries_per_user
        )

        # ── Reasoning ────────────────────────────────────────────────
        reasoning = []
        if overlay >= 70:
            reasoning.append(
                f"High overlay ({detail.overlay_risk:.0f}% unfilled) — "
                f"guaranteed prize pool with fewer competitors."
            )
        if structure >= 70 and is_gpp:
            reasoning.append(
                f"Top-heavy payout ({detail.top_heavy_pct:.0f}%) rewards "
                f"high-ceiling lineups."
            )
        if field_strength >= 70:
            reasoning.append("Softer field — lower fill rate and/or casual pricing.")
        if calibration_fit >= 60:
            reasoning.append("Strong calibration data supports model confidence.")
        if not reasoning:
            reasoning.append("Average contest with moderate opportunity.")

        warnings = []
        if not detail.is_guaranteed and detail.overlay_risk > 20:
            warnings.append(
                "Non-guaranteed contest — may cancel if field doesn't fill."
            )
        if detail.entry_fee >= 100:
            warnings.append("High entry fee — ensure bankroll management.")
        if fill_rate > 0.95:
            warnings.append("Nearly full — field strength may be high.")

        return ContestScore(
            contest_id=detail.contest_id,
            contest_name=detail.contest_name,
            contest_type=contest_type,
            entry_fee=detail.entry_fee,
            total_prizes=detail.total_prizes,
            max_entries=detail.max_entries,
            current_entries=detail.current_entries,
            max_entries_per_user=detail.max_entries_per_user,
            overlay_risk=detail.overlay_risk,
            top_heavy_pct=detail.top_heavy_pct,
            breakeven_percentile=detail.roi_breakeven_percentile,
            overlay_score=round(overlay, 1),
            structure_score=round(structure, 1),
            field_strength_score=round(field_strength, 1),
            calibration_fit_score=round(calibration_fit, 1),
            overall_score=overall,
            recommended_entries=rec_entries,
            confidence=confidence,
            reasoning=reasoning,
            warnings=warnings,
        )

    # ------------------------------------------------------------------
    # Scoring components
    # ------------------------------------------------------------------

    @staticmethod
    def _calc_overlay_score(
        overlay_risk: float, is_guaranteed: bool
    ) -> float:
        """Overlay = prize pool stays same with fewer entries.

        Guaranteed contests with high overlay are +EV.  Non-guaranteed
        contests with overlay may cancel — cap their score.
        """
        score = min(100.0, overlay_risk * 2.0)
        if not is_guaranteed:
            score = min(score, 60.0)
        return score

    @staticmethod
    def _calc_structure_score(
        top_heavy_pct: float,
        breakeven_percentile: float,
        is_gpp: bool,
    ) -> float:
        """Prize structure favorability.

        GPP: top-heavy is good (skilled players benefit from variance).
        Cash: flat payout is good (consistency rewarded).
        """
        if is_gpp:
            base = min(100.0, top_heavy_pct * 1.5)
            # Lower breakeven percentile = easier to cash
            if breakeven_percentile < 30:
                base += 20
            elif breakeven_percentile < 50:
                base += 10
        else:
            # Cash: penalise top-heavy, reward flat
            base = max(0.0, 100.0 - top_heavy_pct * 2.0)
            if breakeven_percentile > 60:
                base -= 15

        return max(0.0, min(100.0, base))

    @staticmethod
    def _calc_field_strength_score(
        fill_rate: float,
        max_entries_per_user: int,
        entry_fee: float,
    ) -> float:
        """Estimate opponent field quality (lower = better for us).

        Heuristics:
        - Lower fill rate → weaker early adopters, higher score
        - High max_entries_per_user → sharps multi-entering
        - Low entry fee → more casual recreational players
        """
        base = max(0.0, 100.0 - fill_rate * 100.0)

        # Multi-entry sharps discount
        if max_entries_per_user > 20:
            base *= 0.80
        elif max_entries_per_user > 5:
            base *= 0.90

        # Entry fee adjustments
        if entry_fee <= 5:
            base += 15
        elif entry_fee <= 10:
            base += 8
        elif entry_fee >= 50:
            base -= 10
        elif entry_fee >= 100:
            base -= 20

        return max(0.0, min(100.0, base))

    def _calc_calibration_fit_score(self) -> float:
        """How well our model is calibrated for this contest type.

        Based on the number and confidence of active calibrations
        from CalibrationService (tournament-learned adjustments).
        """
        if not self._calibration:
            return 40.0  # neutral fallback

        try:
            all_cals = self._calibration.get_all()
        except Exception:
            return 40.0

        if not all_cals:
            return 30.0  # no calibrations = lower confidence

        # Count calibrations with meaningful confidence
        high_conf = sum(
            1 for v in all_cals.values()
            if isinstance(v, (int, float)) and abs(v - 1.0) > 0.01
        )
        score = min(100.0, 30.0 + high_conf * 10.0)
        return score

    # ------------------------------------------------------------------
    # Entry count recommendation
    # ------------------------------------------------------------------

    @staticmethod
    def _recommend_entries(
        overall_score: float,
        contest_type: str,
        max_entries_per_user: int,
    ) -> int:
        """Convert score into suggested lineup count."""
        if overall_score < 50:
            return 0

        if contest_type == "cash":
            return 1

        if overall_score >= 85:
            return min(max_entries_per_user, 20)
        if overall_score >= 75:
            return min(max_entries_per_user, 10)
        if overall_score >= 65:
            return min(max_entries_per_user, 3)
        return 1

    # ------------------------------------------------------------------
    # DK Lobby fetch
    # ------------------------------------------------------------------

    def _fetch_lobby_contests(self, sport: str = "nba") -> List[Dict]:
        """Fetch all contests from DK lobby.  Cached 10 minutes."""
        cache_key = sport
        cached = self._lobby_cache.get(cache_key)
        if cached:
            ts, data = cached
            if (time.time() - ts) < self._lobby_ttl:
                return data

        url = _DK_LOBBY_URLS.get(sport)
        if not url:
            logger.warning(f"[ContestRec] No lobby URL for sport={sport}")
            return []

        try:
            resp = resilient_get(
                url,
                group=APIGroup.DRAFTKINGS,
                headers={"User-Agent": _USER_AGENT},
            )
            raw = resp.json()
        except Exception as e:
            logger.error(f"[ContestRec] Lobby fetch failed: {e}")
            # Return stale cache if available
            if cached:
                return cached[1]
            return []

        contests = raw.get("Contests") or raw.get("contests") or []
        parsed = self._parse_lobby_contests(contests)

        self._lobby_cache[cache_key] = (time.time(), parsed)
        logger.info(f"[ContestRec] Fetched {len(parsed)} contests from DK lobby")
        return parsed

    @staticmethod
    def _parse_lobby_contests(contests: list) -> List[Dict]:
        """Parse sparse DK lobby contest entries into uniform dicts."""
        result = []
        for c in contests:
            try:
                contest_id = str(
                    c.get("id") or c.get("ContestId") or c.get("contestId") or ""
                )
                if not contest_id:
                    continue

                result.append({
                    "id": contest_id,
                    "name": c.get("n") or c.get("ContestName") or c.get("name") or "",
                    "entry_fee": float(c.get("a") or c.get("EntryFee") or 0),
                    "max_entries": int(c.get("m") or c.get("MaxEntries") or 0),
                    "current_entries": int(c.get("ec") or c.get("Entries") or 0),
                    "total_prizes": float(
                        c.get("fpp") or c.get("TotalPrizes") or
                        c.get("po") or 0
                    ),
                    "max_entries_per_user": int(
                        c.get("nt") or c.get("MaxEntriesPerUser") or
                        c.get("mec") or 1
                    ),
                    "draft_group_id": int(
                        c.get("dg") or c.get("DraftGroupId") or 0
                    ),
                    "game_type_id": int(
                        c.get("gameTypeId") or c.get("GameTypeId") or 0
                    ),
                    "is_guaranteed": bool(
                        c.get("IsGuaranteed") or c.get("guaranteed") or False
                    ),
                    "start_date": c.get("sdstring") or c.get("sd") or "",
                })
            except (ValueError, TypeError):
                continue
        return result

    def _score_lobby_contest(
        self, raw: Dict, sport: str
    ) -> Optional[ContestScore]:
        """Score a lobby contest, fetching full detail when available."""
        contest_id = raw.get("id", "")
        if not contest_id:
            return None

        # Try to get full ContestDetail (cached 30 min)
        detail = self._dk_contests.get_contest_detail(contest_id)
        if detail:
            return self.score_contest(detail)

        # Fallback: build a minimal ContestDetail-like object from lobby data
        return self._score_from_lobby_data(raw)

    def _score_from_lobby_data(self, raw: Dict) -> Optional[ContestScore]:
        """Score using only lobby-level data (no prize structure detail)."""
        max_entries = raw.get("max_entries", 0)
        current_entries = raw.get("current_entries", 0)
        entry_fee = raw.get("entry_fee", 0)
        total_prizes = raw.get("total_prizes", 0)
        max_per_user = raw.get("max_entries_per_user", 1)
        is_guaranteed = raw.get("is_guaranteed", False)
        name = raw.get("name", "")

        if max_entries <= 0:
            return None

        # Compute derived metrics inline
        overlay_risk = max(0.0, (1.0 - current_entries / max_entries) * 100)
        fill_rate = current_entries / max_entries

        # Infer contest type from name
        name_lower = name.lower()
        if any(kw in name_lower for kw in ["double up", "50/50", "h2h", "head-to-head"]):
            contest_type = "cash"
        elif max_per_user == 1:
            contest_type = "single_entry"
        else:
            contest_type = "gpp"

        is_gpp = contest_type in ("gpp", "single_entry")

        # Estimate top_heavy_pct and breakeven from entry fee / prizes
        if entry_fee > 0 and total_prizes > 0:
            rake_pct = max(0, (1 - total_prizes / (entry_fee * max_entries)) * 100)
            # Rough heuristic: GPPs ~40-60% top-heavy, cash ~5-10%
            top_heavy_est = 45.0 if is_gpp else 5.0
            breakeven_est = 20.0 if is_gpp else 50.0
        else:
            rake_pct = _DK_RAKE_PCT
            top_heavy_est = 40.0 if is_gpp else 5.0
            breakeven_est = 25.0

        # Score components
        overlay = self._calc_overlay_score(overlay_risk, is_guaranteed)
        structure = self._calc_structure_score(top_heavy_est, breakeven_est, is_gpp)
        field_strength = self._calc_field_strength_score(
            fill_rate, max_per_user, entry_fee
        )
        calibration_fit = self._calc_calibration_fit_score()

        overall = round(
            _W_OVERLAY * overlay
            + _W_STRUCTURE * structure
            + _W_FIELD_STRENGTH * field_strength
            + _W_CALIBRATION * calibration_fit,
            1,
        )
        overall = min(100.0, max(0.0, overall))

        confidence = overall / 100.0
        if calibration_fit < 30:
            confidence *= 0.70
        if fill_rate > 0.95:
            confidence *= 0.85
        confidence = round(min(1.0, max(0.0, confidence)), 3)

        rec_entries = self._recommend_entries(overall, contest_type, max_per_user)

        reasoning = ["Scored from lobby data (limited prize structure detail)."]
        warnings = []
        if not is_guaranteed and overlay_risk > 20:
            warnings.append("Non-guaranteed — may cancel if unfilled.")

        return ContestScore(
            contest_id=raw.get("id", ""),
            contest_name=name,
            contest_type=contest_type,
            entry_fee=entry_fee,
            total_prizes=total_prizes,
            max_entries=max_entries,
            current_entries=current_entries,
            max_entries_per_user=max_per_user,
            overlay_risk=round(overlay_risk, 1),
            top_heavy_pct=round(top_heavy_est, 1),
            breakeven_percentile=round(breakeven_est, 1),
            overlay_score=round(overlay, 1),
            structure_score=round(structure, 1),
            field_strength_score=round(field_strength, 1),
            calibration_fit_score=round(calibration_fit, 1),
            overall_score=overall,
            recommended_entries=rec_entries,
            confidence=confidence,
            reasoning=reasoning,
            warnings=warnings,
        )
