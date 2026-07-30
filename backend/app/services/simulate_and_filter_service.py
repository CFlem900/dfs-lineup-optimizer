"""Simulate-and-Filter lineup generation pipeline.

An alternative to the standard lineup optimizer that:
1. Runs N Monte Carlo simulations per game on the slate
2. For each simulated "world", finds the optimal lineup (greedy or ILP)
3. Fingerprints each result and counts how often each unique lineup appears
4. Returns the top-K lineups sorted by frequency

The intuition: lineups that appear as optimal across many different
simulated outcomes are structurally robust — they don't depend on one
specific stat line hitting.  High-frequency lineups are the "true"
best builds; low-frequency ones are situational upside plays.

This service is intentionally isolated from LineupOptimizerService
so the two pipelines can be A/B tested independently.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, FrozenSet, List, Optional, Set, Tuple

import numpy as np

from app.models.lineup import (
    OptimizedLineup,
    PlayerPoolEntry,
    SimFilterLineup,
    SimFilterRequest,
    SimFilterResponse,
)
from app.models.simulation import SimulationConfig
from app.services.simulation_engine import SimulationEngine

logger = logging.getLogger(__name__)


class SimulateAndFilterService:
    """Standalone simulate-and-filter pipeline for lineup generation."""

    def __init__(self, optimizer: "LineupOptimizerService"):
        """Accept the existing optimizer for pool building and solver access.

        Parameters
        ----------
        optimizer : LineupOptimizerService
            Used for ``build_player_pool``, ``_greedy_fill_scored``,
            ``_ilp_optimize``, ``_build_lineup_from_assignment``,
            ``_get_slot_eligible_positions``, ``_player_matches_slot``,
            and game context building.
        """
        self._opt = optimizer

    # ──────────────────────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────────────────────

    def generate(self, request: SimFilterRequest) -> SimFilterResponse:
        """Run the full simulate-and-filter pipeline."""
        t0 = time.time()

        platform = request.platform
        sport = request.sport
        mode = getattr(request, "mode", "classic")

        # ── Roster / salary config ────────────────────────────────
        from app.services.lineup_optimizer_service import (
            DK_SALARY_CAP,
            FD_SALARY_CAP,
            DK_ROSTER_SLOTS,
            DK_SLOT_ORDER,
            FD_ROSTER_SLOTS,
            FD_SLOT_ORDER,
            DK_CBB_ROSTER_SLOTS,
            DK_CBB_SLOT_ORDER,
            DK_SHOWDOWN_SALARY_CAP,
            DK_SHOWDOWN_SLOTS,
            _index_slots,
            _base_slot,
        )

        if mode == "showdown" and platform == "dk":
            salary_cap = DK_SHOWDOWN_SALARY_CAP
            roster_slots = list(DK_SHOWDOWN_SLOTS)
            slot_order = list(range(len(roster_slots)))
        else:
            salary_cap = DK_SALARY_CAP if platform == "dk" else FD_SALARY_CAP
            if platform == "dk" and sport == "cbb":
                roster_slots = list(DK_CBB_ROSTER_SLOTS)
                slot_order = list(DK_CBB_SLOT_ORDER)
            elif platform == "dk":
                roster_slots = list(DK_ROSTER_SLOTS)
                slot_order = list(DK_SLOT_ORDER)
            else:
                roster_slots = list(FD_ROSTER_SLOTS)
                slot_order = list(FD_SLOT_ORDER)

        indexed_slots = _index_slots(slot_order)

        # ── Build player pool ─────────────────────────────────────
        pool = self._opt.build_player_pool(
            platform=platform,
            draft_group_id=request.draft_group_id,
            game_date=request.game_date,
            excluded_player_ids=request.excluded_players,
            sport=sport,
        )

        if request.projection_overrides:
            pool = self._opt._apply_overrides(
                pool, request.projection_overrides
            )

        if not pool:
            raise ValueError("Empty player pool — check draft_group_id and date.")

        logger.info(
            f"[SimFilter] Pool: {len(pool)} players, "
            f"{request.num_simulations} sims, solver={request.solver_mode}"
        )

        # ── Phase 1: Run simulations ──────────────────────────────
        num_sims = request.num_simulations
        fp_key = "dk" if platform == "dk" else "fd"

        raw_fps = self._run_simulations(pool, num_sims, request, sport, fp_key)

        # Synthetic fallback for players without sim data
        rng = np.random.default_rng(request.seed)
        for p in pool:
            if p.player_id not in raw_fps:
                fp_mean = p.projected_fp
                fp_std = max((p.ceiling_fp - p.floor_fp) / 3.3, 1.0)
                synth = rng.normal(fp_mean, fp_std, num_sims)
                raw_fps[p.player_id] = np.clip(
                    synth, max(p.floor_fp * 0.5, 0), p.ceiling_fp * 1.3
                )

        # ── Phase 2: Solve each iteration ─────────────────────────
        locked_ids = list(request.locked_players or [])
        excluded_set = set(request.excluded_players or [])

        # ── Fresh DK injury status check ───────────────────────
        # The player pool may be served from cache (up to 30 min old).
        # DK injury statuses change throughout the day (late scratches,
        # GTD → OUT upgrades).  Force-refresh draftables to get the
        # authoritative current status and filter out players who are
        # now OUT/Doubtful even if the cached pool entry has
        # injury_status=None.
        dk_out_ids: set = set()
        dk_svc = getattr(self._opt, "dk_draftables_service", None)
        if dk_svc and request.draft_group_id:
            try:
                fresh_draftables = dk_svc.get_draftables(
                    request.draft_group_id, force_refresh=True
                )
                for d in fresh_draftables:
                    st = (getattr(d, "status", "") or "").strip().upper()
                    if st in ("O", "OUT", "D", "DOUBTFUL"):
                        _did = getattr(d, "dk_player_id", None)
                        if _did:
                            dk_out_ids.add(_did)
                if dk_out_ids:
                    logger.info(
                        f"[SimFilter] Fresh DK status check: "
                        f"{len(dk_out_ids)} OUT/Doubtful player(s) "
                        f"will be excluded"
                    )
            except Exception as e:
                logger.warning(
                    f"[SimFilter] Fresh DK status fetch failed, "
                    f"relying on pool injury_status only: {e}"
                )

        available_pool = [
            p for p in pool
            if p.player_id not in excluded_set
            and p.player_id not in dk_out_ids
            and (p.injury_status or "").upper() not in ("OUT", "DOUBTFUL")
        ]

        _n_injury_filtered = len(pool) - len(available_pool) - len(excluded_set & {p.player_id for p in pool})
        if _n_injury_filtered > 0:
            logger.info(
                f"[SimFilter] Filtered {_n_injury_filtered} injured player(s) "
                f"from pool ({len(pool)} → {len(available_pool)} available)"
            )

        # Rebuild pool_by_id from available_pool so the greedy solver's
        # locked-player path can't force-add injured/excluded players.
        pool_by_id = {p.player_id: p for p in available_pool}

        results: List[Tuple[FrozenSet[int], float]] = []
        lineup_cache: Dict[FrozenSet[int], Dict[str, PlayerPoolEntry]] = {}

        if request.solver_mode == "ilp":
            results, lineup_cache = self._solve_iterations_ilp(
                available_pool, pool_by_id, raw_fps, num_sims,
                platform, salary_cap, slot_order, indexed_slots,
                locked_ids, mode, sport, request,
            )
        else:
            results, lineup_cache = self._solve_iterations_greedy(
                available_pool, pool_by_id, raw_fps, num_sims,
                platform, salary_cap, slot_order, indexed_slots,
                locked_ids, sport,
            )

        if not results:
            raise ValueError(
                "No valid lineups found across all simulation iterations."
            )

        # ── Phase 3: Count frequency + rank ───────────────────────
        freq_count: Dict[FrozenSet[int], int] = defaultdict(int)
        fp_totals: Dict[FrozenSet[int], List[float]] = defaultdict(list)

        for fingerprint, sim_fp in results:
            freq_count[fingerprint] += 1
            fp_totals[fingerprint].append(sim_fp)

        # Sort by frequency descending, then by avg FP as tiebreaker
        ranked = sorted(
            freq_count.keys(),
            key=lambda fp: (freq_count[fp], np.mean(fp_totals[fp])),
            reverse=True,
        )

        # ── Phase 4: Build response ───────────────────────────────
        n_solved = len(results)
        top_k = ranked[: request.num_lineups]

        sim_filter_lineups: List[SimFilterLineup] = []
        for fingerprint in top_k:
            assignment = lineup_cache.get(fingerprint)
            if not assignment:
                continue

            lineup_obj = self._opt._build_lineup_from_assignment(
                assignment, platform, salary_cap, roster_slots, sport=sport,
            )
            if lineup_obj is None:
                continue

            fps = fp_totals[fingerprint]
            sim_filter_lineups.append(
                SimFilterLineup(
                    lineup=lineup_obj,
                    frequency=freq_count[fingerprint],
                    frequency_pct=round(
                        100.0 * freq_count[fingerprint] / n_solved, 2
                    ),
                    avg_sim_fp=round(float(np.mean(fps)), 2),
                    max_sim_fp=round(float(np.max(fps)), 2),
                )
            )

        elapsed_ms = int((time.time() - t0) * 1000)
        logger.info(
            f"[SimFilter] Done: {len(sim_filter_lineups)} lineups from "
            f"{len(freq_count)} unique / {n_solved} solved in {elapsed_ms}ms"
        )

        return SimFilterResponse(
            platform=platform,
            sport=sport,
            lineups=sim_filter_lineups,
            num_simulations=num_sims,
            num_iterations_solved=n_solved,
            num_unique_lineups=len(freq_count),
            num_returned=len(sim_filter_lineups),
            solver_mode=request.solver_mode,
            pool_size=len(pool),
            generation_time_ms=elapsed_ms,
        )

    # ──────────────────────────────────────────────────────────────
    # Phase 1: Simulation
    # ──────────────────────────────────────────────────────────────

    def _run_simulations(
        self,
        pool: List[PlayerPoolEntry],
        num_sims: int,
        request: SimFilterRequest,
        sport: str,
        fp_key: str,
    ) -> Dict[int, "np.ndarray"]:
        """Run Monte Carlo simulations for all games on the slate.

        Returns ``{player_id: ndarray(num_sims,)}`` with per-iteration FP.
        """
        # Build game context
        game_ctx = getattr(self._opt, "_game_lookup_cache", {})
        if not game_ctx:
            game_ctx = self._opt._build_dk_game_context(
                request.draft_group_id
            )
        if not game_ctx:
            logger.warning("[SimFilter] No game context — using synthetic FP only")
            return {}

        _cached_teams = getattr(self._opt, "_team_data_cache", {}) or {}

        # Deduplicate games
        seen_game_ids: Set[str] = set()
        unique_games = []
        for team_data in game_ctx.values():
            gi = team_data.get("game_info")
            if not gi:
                continue
            gid = gi.game_id
            if gid in seen_game_ids:
                continue
            seen_game_ids.add(gid)
            unique_games.append(gi)

        if not unique_games:
            return {}

        sim_config = SimulationConfig(
            num_simulations=num_sims,
            seed=request.seed,
        )

        all_raw_fps: Dict[int, "np.ndarray"] = {}

        def _sim_game(g):
            """Simulate one game, return {pid: ndarray(N,)}."""
            local_sim = SimulationEngine(sim_config)
            home_abbr = g.home_team.team_abbreviation.upper()
            away_abbr = g.away_team.team_abbreviation.upper()
            home_cached = _cached_teams.get(home_abbr)
            away_cached = _cached_teams.get(away_abbr)
            if not (home_cached and away_cached):
                return {}
            try:
                _, raw = local_sim.simulate_game_raw(
                    game_info=g,
                    home_rotation=home_cached["projected"],
                    home_players=home_cached["rotation"],
                    away_rotation=away_cached["projected"],
                    away_players=away_cached["rotation"],
                    sport=sport,
                )
                return {pid: fp_dict[fp_key] for pid, fp_dict in raw.items()}
            except Exception as e:
                logger.warning(f"[SimFilter] Sim failed for game: {e}")
                return {}

        with ThreadPoolExecutor(
            max_workers=min(6, len(unique_games)),
            thread_name_prefix="simfilter",
        ) as executor:
            futures = {executor.submit(_sim_game, g): g for g in unique_games}
            for future in as_completed(futures):
                try:
                    game_fps = future.result(timeout=15)
                    all_raw_fps.update(game_fps)
                except Exception as e:
                    logger.warning(f"[SimFilter] Game sim future error: {e}")

        logger.info(
            f"[SimFilter] Simulated {len(unique_games)} games, "
            f"{len(all_raw_fps)} players with {num_sims} iterations each"
        )
        return all_raw_fps

    # ──────────────────────────────────────────────────────────────
    # Phase 2a: Greedy solver (fast, ~1ms per iteration)
    # ──────────────────────────────────────────────────────────────

    def _solve_iterations_greedy(
        self,
        pool: List[PlayerPoolEntry],
        pool_by_id: Dict[int, PlayerPoolEntry],
        raw_fps: Dict[int, "np.ndarray"],
        num_sims: int,
        platform: str,
        salary_cap: int,
        slot_order: List[str],
        indexed_slots: List[str],
        locked_ids: List[int],
        sport: str,
    ) -> Tuple[
        List[Tuple[FrozenSet[int], float]],
        Dict[FrozenSet[int], Dict[str, PlayerPoolEntry]],
    ]:
        """Solve every iteration with the fast greedy solver."""
        results: List[Tuple[FrozenSet[int], float]] = []
        lineup_cache: Dict[FrozenSet[int], Dict[str, PlayerPoolEntry]] = {}
        locked_set = set(locked_ids)

        for iter_idx in range(num_sims):
            # Build score map for this iteration
            iter_scores = {
                pid: float(arr[iter_idx])
                for pid, arr in raw_fps.items()
                if iter_idx < len(arr)
            }

            def score_fn(p: PlayerPoolEntry, _s=iter_scores) -> float:
                return _s.get(p.player_id, p.projected_fp)

            # Seed locked players
            lineup: Dict[str, PlayerPoolEntry] = {}
            used_ids: Set[int] = set()
            remaining_salary = salary_cap
            remaining_slots = list(indexed_slots)

            for locked_id in locked_ids:
                if locked_id in pool_by_id:
                    player = pool_by_id[locked_id]
                    from app.services.lineup_optimizer_service import _base_slot
                    for isl in list(remaining_slots):
                        base = _base_slot(isl)
                        elig = self._opt._get_slot_eligible_positions(
                            base, platform, sport
                        )
                        if self._opt._player_matches_slot(player.position, elig):
                            lineup[isl] = player
                            used_ids.add(player.player_id)
                            remaining_salary -= player.salary
                            remaining_slots.remove(isl)
                            break

            # Greedy fill
            try:
                lineup = self._opt._greedy_fill_scored(
                    pool, lineup, remaining_slots, used_ids,
                    remaining_salary, platform, score_fn, sport=sport,
                )
            except Exception:
                continue

            if len(lineup) < len(indexed_slots):
                continue

            # Validate salary
            total_salary = sum(p.salary for p in lineup.values())
            if total_salary > salary_cap:
                continue

            # Fingerprint + store
            fingerprint = frozenset(p.player_id for p in lineup.values())
            sim_fp = sum(
                iter_scores.get(p.player_id, p.projected_fp)
                for p in lineup.values()
            )
            results.append((fingerprint, sim_fp))
            if fingerprint not in lineup_cache:
                lineup_cache[fingerprint] = dict(lineup)

        logger.info(
            f"[SimFilter/Greedy] Solved {len(results)}/{num_sims} iterations, "
            f"{len(lineup_cache)} unique lineups"
        )
        return results, lineup_cache

    # ──────────────────────────────────────────────────────────────
    # Phase 2b: ILP solver (slower, provably optimal per iteration)
    # ──────────────────────────────────────────────────────────────

    def _solve_iterations_ilp(
        self,
        pool: List[PlayerPoolEntry],
        pool_by_id: Dict[int, PlayerPoolEntry],
        raw_fps: Dict[int, "np.ndarray"],
        num_sims: int,
        platform: str,
        salary_cap: int,
        slot_order: List[str],
        indexed_slots: List[str],
        locked_ids: List[int],
        mode: str,
        sport: str,
        request: SimFilterRequest,
    ) -> Tuple[
        List[Tuple[FrozenSet[int], float]],
        Dict[FrozenSet[int], Dict[str, PlayerPoolEntry]],
    ]:
        """Solve iterations with the ILP solver (parallel)."""
        results: List[Tuple[FrozenSet[int], float]] = []
        lineup_cache: Dict[FrozenSet[int], Dict[str, PlayerPoolEntry]] = {}

        def _solve_one(iter_idx: int):
            iter_scores = {
                pid: float(arr[iter_idx])
                for pid, arr in raw_fps.items()
                if iter_idx < len(arr)
            }

            def score_fn(p: PlayerPoolEntry, _s=iter_scores) -> float:
                return _s.get(p.player_id, p.projected_fp)

            try:
                ilp_result = self._opt._ilp_optimize(
                    pool=pool,
                    platform=platform,
                    salary_cap=salary_cap,
                    slot_order=slot_order,
                    locked_player_ids=locked_ids,
                    score_fn=score_fn,
                    mode=mode,
                    sport=sport,
                    contest_type=request.contest_type,
                )
            except Exception:
                return None

            if not ilp_result or len(ilp_result) < len(indexed_slots):
                return None

            total_salary = sum(p.salary for p in ilp_result.values())
            if total_salary > salary_cap:
                return None

            fingerprint = frozenset(p.player_id for p in ilp_result.values())
            sim_fp = sum(
                iter_scores.get(p.player_id, p.projected_fp)
                for p in ilp_result.values()
            )
            return fingerprint, sim_fp, dict(ilp_result)

        # Parallel ILP solving
        max_workers = min(4, num_sims)
        with ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="simfilter-ilp",
        ) as executor:
            futures = {
                executor.submit(_solve_one, i): i
                for i in range(num_sims)
            }
            for future in as_completed(futures):
                try:
                    result = future.result(timeout=30)
                    if result is None:
                        continue
                    fp, sim_fp, assignment = result
                    results.append((fp, sim_fp))
                    if fp not in lineup_cache:
                        lineup_cache[fp] = assignment
                except Exception:
                    continue

        logger.info(
            f"[SimFilter/ILP] Solved {len(results)}/{num_sims} iterations, "
            f"{len(lineup_cache)} unique lineups"
        )
        return results, lineup_cache
