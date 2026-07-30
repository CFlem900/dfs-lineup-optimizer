"""LateSwapService — ILP re-optimisation for active DraftKings entries.

Takes an imported ``ActiveEntry`` (from the CSV upload pipeline), evaluates
which players are locked (game tipped) vs. unlocked (game not started),
and re-optimises the open slots using a PuLP/CBC Integer Linear Program.

Key design decisions:

1. **Self-contained** — Consumes ``ActiveEntry.lineup`` JSONB directly
   without coupling to ``LineupOptimizerService`` internals.

2. **``has_started`` as authority** — Lock status comes from
   ``LiveGameStateService.GameState.has_started`` (``period > 0`` or
   ``is_final``).  Delayed games with ``period == 0`` are NOT locked.

3. **UTIL rearrangement** — After the ILP solve, the player with the
   *latest* scheduled game time is moved to UTIL (if UTIL is among the
   open slots) for maximum future swap flexibility.

4. **No ``threads`` param** — CBC's ``threads=1`` is broken on Windows
   (see MEMORY.md).  The solver defaults to single-threaded anyway.

Usage::

    svc = LateSwapService(
        live_game_state_service=live_gs,
        entry_import_service=entry_svc,
    )
    result = await svc.optimize_and_update_entry(
        entry_id="12345",
        draft_group_id=91234,
        player_pool=pool,
        game_date="2026-02-28",
    )
"""

from __future__ import annotations

import logging
import threading
import warnings
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ============================================================================
# Attempt to import PuLP
# ============================================================================

try:
    import pulp
    _PULP_AVAILABLE = True
except ImportError:  # pragma: no cover
    pulp = None  # type: ignore[assignment]
    _PULP_AVAILABLE = False

# ============================================================================
# Constants (imported from central config where available)
# ============================================================================

DK_SALARY_CAP = 50_000

# ILP solver config — sourced from app.config.constants at solve time
# so hot-reload / override is honoured.

# NBA Classic slot eligibility (position -> eligible set)
_NBA_SLOT_ELIGIBILITY: Dict[str, set] = {
    "PG": {"PG"},
    "SG": {"SG"},
    "SF": {"SF"},
    "PF": {"PF"},
    "C":  {"C"},
    "G":  {"PG", "SG"},
    "F":  {"SF", "PF"},
    "UTIL": {"PG", "SG", "SF", "PF", "C"},
}

_CBB_SLOT_ELIGIBILITY: Dict[str, set] = {
    "G":  {"PG", "SG", "G"},
    "F":  {"SF", "PF", "C", "F"},
    "UTIL": {"PG", "SG", "SF", "PF", "C", "G", "F"},
}


# ============================================================================
# Data Classes
# ============================================================================

@dataclass
class LockedPlayer:
    """A player whose game has started — slot cannot be changed."""

    indexed_slot: str       # "PG_0", "SG_0", etc.
    slot: str               # "PG", "SG", etc.
    dk_player_id: int
    player_name: str
    salary: int
    team: str


@dataclass
class EntryState:
    """Classification of an entry's slots into locked vs. open."""

    locked_players: List[LockedPlayer]
    open_slots: List[str]       # indexed slot keys ("SF_0", "UTIL_0")
    locked_salary: int
    remaining_salary: int
    all_locked: bool


@dataclass
class SwapDetail:
    """A single player swap within the late-swap result."""

    slot: str
    old_player: str
    new_player: str
    old_salary: int
    new_salary: int
    projected_fp_gain: float


@dataclass
class LateSwapResult:
    """Full result of a late-swap optimisation for one entry."""

    success: bool
    entry_id: str
    lineup: List[Dict]          # Updated 8-player JSONB-compatible list
    total_salary: int
    total_projected_fp: float
    swaps: List[SwapDetail]
    locked_count: int
    open_count: int
    remaining_salary_used: int
    warnings: List[str] = field(default_factory=list)


# ============================================================================
# Slot Indexing Helpers (standalone copies from lineup_optimizer_service.py)
# ============================================================================

def _index_slots(slot_list: List[str]) -> List[str]:
    """Convert a slot list to indexed keys so duplicates become unique.

    Example: ["PG", "SG", "SF", "PF", "C", "G", "F", "UTIL"]
           -> ["PG_0", "SG_0", "SF_0", "PF_0", "C_0", "G_0", "F_0", "UTIL_0"]
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

    Example: "PG_1" -> "PG", "UTIL_0" -> "UTIL"
    """
    return indexed_key.rsplit("_", 1)[0]


# ============================================================================
# LateSwapService
# ============================================================================

class LateSwapService:
    """ILP-based late-swap re-optimiser for imported ActiveEntry lineups.

    Dependencies
    ------------
    live_game_state_service : LiveGameStateService
        Fetches real-time game states from BALLDONTLIE.
    entry_import_service : EntryImportService
        Loads / updates ``ActiveEntry`` data from PostgreSQL.
    """

    def __init__(
        self,
        live_game_state_service=None,
        entry_import_service=None,
    ):
        self._live_gs = live_game_state_service
        self._entry_svc = entry_import_service

    # ------------------------------------------------------------------
    # 1. State Evaluation
    # ------------------------------------------------------------------

    def evaluate_entry_state(
        self,
        entry_lineup: List[Dict],
        game_states: Dict[str, Any],
        salary_cap: int = DK_SALARY_CAP,
    ) -> EntryState:
        """Classify each roster slot as LOCKED or UNLOCKED.

        Parameters
        ----------
        entry_lineup : list[dict]
            The 8-player lineup JSONB from ``ActiveEntry``.  Each dict
            has: ``slot``, ``dk_player_id``, ``player_name``, ``salary``,
            ``team``.
        game_states : dict[str, GameState]
            Live game states keyed by BDL team abbreviation.
        salary_cap : int
            Total salary budget (default 50 000).

        Returns
        -------
        EntryState
            Locked / open classification for the full lineup.
        """
        from app.services.live_game_state_service import normalise_to_bdl

        # Build indexed slot keys from the slot names
        slot_names = [p["slot"] for p in entry_lineup]
        indexed = _index_slots(slot_names)

        locked_players: List[LockedPlayer] = []
        open_slots: List[str] = []

        for i, player in enumerate(entry_lineup):
            isl = indexed[i]
            base = player["slot"]
            team_raw = player.get("team") or ""
            bdl_team = normalise_to_bdl(team_raw) if team_raw else ""

            gs = game_states.get(bdl_team) if bdl_team else None
            has_started = gs.has_started if gs else False

            if has_started:
                locked_players.append(LockedPlayer(
                    indexed_slot=isl,
                    slot=base,
                    dk_player_id=player.get("dk_player_id", 0),
                    player_name=player.get("player_name", ""),
                    salary=player.get("salary", 0),
                    team=team_raw,
                ))
            else:
                open_slots.append(isl)

        locked_salary = sum(lp.salary for lp in locked_players)
        remaining = salary_cap - locked_salary

        return EntryState(
            locked_players=locked_players,
            open_slots=open_slots,
            locked_salary=locked_salary,
            remaining_salary=remaining,
            all_locked=(len(open_slots) == 0),
        )

    # ------------------------------------------------------------------
    # 2. ILP Solver for Open Slots
    # ------------------------------------------------------------------

    def _solve_late_swap(
        self,
        entry_state: EntryState,
        player_pool: List[Any],
        game_states: Dict[str, Any],
        sport: str = "nba",
    ) -> Optional[Dict[str, Any]]:
        """Solve the late-swap ILP for open roster slots.

        Returns a dict mapping indexed_slot -> PlayerPoolEntry for each
        open slot, or None if the solver fails / is unavailable.

        ILP Formulation
        ===============
        Locked players are implicitly hardcoded (x_i = 1) by *excluding*
        them from the solver entirely and *deducting* their salaries from
        the cap upfront.  This is mathematically equivalent to creating
        variables with forced upper/lower bounds of 1, but yields a
        smaller, faster model (fewer variables = faster CBC branch-and-bound).

        ::

            ═══════════════════════════════════════════════════════════
            LATE-SWAP ILP MODEL
            ═══════════════════════════════════════════════════════════

            Given:
              salary_cap     = 50,000  (DK Classic)
              locked_salary  = sum of salaries for players whose games
                               have started (GameState.has_started == True)
              remaining_salary = salary_cap - locked_salary

            Sets:
              J = {open indexed slots}   e.g. {"PF_0", "C_0", "UTIL_0"}
              P = {eligible players}     after all pre-filters (below)
              E(j) = {positions eligible for slot j}
                     from _NBA_SLOT_ELIGIBILITY / _CBB_SLOT_ELIGIBILITY

            Decision variables:
              x[p, j] in {0, 1}
                = 1 if player p is assigned to open slot j
                Created ONLY when position(p) in E(j)  [C4 enforcement]

            Objective:
              maximize  sum_{p,j}  projected_fp(p) * x[p, j]

            Subject to:
              C1 -- Dynamic salary budget (remaining after locked salary):
                    sum_{p,j}  salary(p) * x[p, j]  <=  remaining_salary

              C2 -- Slot fill (every open slot gets exactly one player):
                    sum_p  x[p, j] = 1     for all j in J

              C3 -- Player uniqueness (each player used at most once):
                    sum_j  x[p, j] <= 1    for all p in P

              C4 -- Positional eligibility (enforced at variable creation):
                    x[p, j] is only created when position(p) in E(j)
                    so no variable exists for ineligible assignments.

              C5 -- Game-started filter (enforced by pool pre-filtering):
                    Players on teams where GameState.has_started == True
                    are removed from P before variable creation.

            Pool pre-filters applied before variable creation:
              - Exclude players already locked in this entry (locked_ids)
              - Exclude injury_status in {"Out", "Doubtful"}
              - Exclude players whose games have started (has_started)
              - Exclude players whose salary alone exceeds remaining_salary
            ═══════════════════════════════════════════════════════════
        """
        if not _PULP_AVAILABLE or pulp is None:
            logger.warning("[LateSwap-ILP] PuLP not available")
            return None

        if not entry_state.open_slots:
            return None

        from app.services.live_game_state_service import normalise_to_bdl

        # ═══════════════════════════════════════════════════════════════
        # STEP 1: Determine eligibility map for this sport
        # ═══════════════════════════════════════════════════════════════
        # NBA Classic: PG->{PG}, SG->{SG}, G->{PG,SG}, F->{SF,PF},
        #              UTIL->{PG,SG,SF,PF,C}
        elig_map = _NBA_SLOT_ELIGIBILITY if sport == "nba" else _CBB_SLOT_ELIGIBILITY

        # ═══════════════════════════════════════════════════════════════
        # STEP 2: Collect locked player IDs to exclude from the pool
        # These players are effectively hardcoded (x_i = 1) by being
        # excluded from the solver and having their salary pre-deducted.
        # ═══════════════════════════════════════════════════════════════
        locked_ids = {lp.dk_player_id for lp in entry_state.locked_players}

        # ═══════════════════════════════════════════════════════════════
        # STEP 3: Dynamic budget calculation
        # remaining_salary = salary_cap - locked_salary
        # This is the C1 RHS — the budget the ILP has to work with.
        # ═══════════════════════════════════════════════════════════════
        remaining = entry_state.remaining_salary

        logger.info(
            "[LateSwap-ILP] Budget: $%d remaining ($%d locked across %d player(s)), "
            "%d open slot(s): %s",
            remaining,
            entry_state.locked_salary,
            len(entry_state.locked_players),
            len(entry_state.open_slots),
            [_base_slot(j) for j in entry_state.open_slots],
        )

        # ═══════════════════════════════════════════════════════════════
        # STEP 4: Filter the player pool (C5 enforcement + pruning)
        #   - Not in locked_ids  (already in lineup, game started)
        #   - Not injured Out/Doubtful
        #   - Game not started   (GameState.has_started == False)
        #   - Salary <= remaining (trivial pruning — can't afford alone)
        # ═══════════════════════════════════════════════════════════════
        available: List[Any] = []

        for p in player_pool:
            pid = getattr(p, "dk_player_id", None) or getattr(p, "player_id", 0)
            if pid in locked_ids:
                continue
            inj = getattr(p, "injury_status", None) or ""
            if inj in ("Out", "Doubtful"):
                continue
            if p.salary > remaining:
                continue
            # C5: Game-started filter via BDL live telemetry
            team_raw = getattr(p, "team_abbreviation", "") or ""
            bdl_team = normalise_to_bdl(team_raw) if team_raw else ""
            gs = game_states.get(bdl_team)
            if gs and gs.has_started:
                continue
            available.append(p)

        if not available:
            logger.warning("[LateSwap-ILP] No eligible players after filtering")
            return None

        logger.info(
            "[LateSwap-ILP] Pool filtered: %d -> %d eligible players",
            len(player_pool), len(available),
        )

        player_lookup = {}
        for p in available:
            pid = getattr(p, "dk_player_id", None) or getattr(p, "player_id", 0)
            player_lookup[pid] = p

        # ═══════════════════════════════════════════════════════════════
        # STEP 5: Build the ILP model
        #
        # Variable naming: ls_{player_id}_{indexed_slot}
        # e.g. ls_12345_PF_0 = 1 means player 12345 fills PF slot 0
        # ═══════════════════════════════════════════════════════════════
        prob = pulp.LpProblem(
            f"LateSwap_{threading.get_ident()}", pulp.LpMaximize,
        )

        # x[(pid, indexed_slot)] -> Binary decision variable
        x: Dict[Tuple[int, str], Any] = {}
        # vars_by_slot[j] -> [(pid, var), ...] for constraint C2
        vars_by_slot: Dict[str, List[Tuple[int, Any]]] = {
            j: [] for j in entry_state.open_slots
        }
        # vars_by_player[pid] -> [(j, var), ...] for constraint C3
        vars_by_player: Dict[int, List[Tuple[str, Any]]] = {}

        # ── C4: Positional eligibility (enforced here at variable creation)
        # We only create x[p, j] when player p's position is in the
        # eligible set E(j) for slot j.  No variable = no assignment.
        for p in available:
            pid = getattr(p, "dk_player_id", None) or getattr(p, "player_id", 0)
            pos = getattr(p, "position", "")

            for j in entry_state.open_slots:
                base = _base_slot(j)
                eligible_positions = elig_map.get(base, set())
                if pos not in eligible_positions:
                    continue  # C4: position not eligible for this slot

                var = pulp.LpVariable(
                    f"ls_{pid}_{j}", cat="Binary",
                )
                x[(pid, j)] = var
                vars_by_slot[j].append((pid, var))
                if pid not in vars_by_player:
                    vars_by_player[pid] = []
                vars_by_player[pid].append((j, var))

        if not x:
            logger.warning("[LateSwap-ILP] No eligible variables created")
            return None

        logger.info(
            "[LateSwap-ILP] Model: %d binary variables, "
            "%d eligible players across %d open slots",
            len(x), len(vars_by_player), len(entry_state.open_slots),
        )

        # ── Objective: maximize total projected fantasy points ────────
        # max  sum_{p,j}  projected_fp(p) * x[p, j]
        prob += pulp.lpSum(
            player_lookup[pid].projected_fp * var
            for (pid, _), var in x.items()
        )

        # ── C1: Dynamic salary budget constraint ─────────────────────
        # sum_{p,j}  salary(p) * x[p, j]  <=  remaining_salary
        #
        # remaining_salary = 50,000 - locked_salary
        # This ensures the total lineup salary (locked + new) stays
        # within the DK $50K cap.
        prob += (
            pulp.lpSum(
                player_lookup[pid].salary * var
                for (pid, _), var in x.items()
            ) <= remaining,
            "C1_salary_cap",
        )

        # ── C2: Slot fill — every open slot gets exactly one player ──
        # sum_p  x[p, j] = 1   for all j in open_slots
        for j in entry_state.open_slots:
            slot_vars = vars_by_slot[j]
            if slot_vars:
                prob += (
                    pulp.lpSum(var for _, var in slot_vars) == 1,
                    f"C2_fill_{j}",
                )
            else:
                # No eligible players can fill this slot — infeasible
                logger.warning(
                    "[LateSwap-ILP] No candidates for slot %s — "
                    "model infeasible", j,
                )
                return None

        # ── C3: Player uniqueness — each player used at most once ────
        # sum_j  x[p, j] <= 1   for all p in available
        for pid, pv_list in vars_by_player.items():
            prob += (
                pulp.lpSum(var for _, var in pv_list) <= 1,
                f"C3_uniq_{pid}",
            )

        # ═══════════════════════════════════════════════════════════════
        # STEP 6: Solve with CBC
        #
        # Solver config from app.config.constants:
        #   timeLimit  = ILP_CBC_TIME_LIMIT  (8 seconds)
        #   presolve   = ILP_CBC_PRESOLVE    (True)
        #   gapRel     = ILP_CBC_GAP_REL     (0.005 = 0.5%)
        #
        # CRITICAL: Do NOT pass threads= parameter.
        # CBC threads=1 is broken on Windows — causes cbc.exe to hang
        # for large pools. CBC defaults to single-threaded anyway.
        # ═══════════════════════════════════════════════════════════════
        from app.config.constants import (
            ILP_CBC_TIME_LIMIT,
            ILP_CBC_PRESOLVE,
            ILP_CBC_GAP_REL,
        )

        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message=".*warmStart requires keepFiles.*",
                category=UserWarning,
            )
            solver = pulp.PULP_CBC_CMD(
                msg=0,
                timeLimit=ILP_CBC_TIME_LIMIT,
                presolve=ILP_CBC_PRESOLVE,
                gapRel=ILP_CBC_GAP_REL,
                # NOTE: Never pass threads= — broken on Windows (MEMORY.md)
            )
            try:
                status = prob.solve(solver)
            except Exception as e:
                logger.warning("[LateSwap-ILP] CBC solver error: %s", e)
                return None

        # ═══════════════════════════════════════════════════════════════
        # STEP 7: Validate solver status
        # Accept LpStatusOptimal or LpSolutionIntegerFeasible (the
        # incumbent solution within the gapRel tolerance).
        # ═══════════════════════════════════════════════════════════════
        if status == pulp.constants.LpStatusOptimal:
            pass
        elif prob.sol_status == pulp.constants.LpSolutionIntegerFeasible:
            logger.info(
                "[LateSwap-ILP] Feasible incumbent "
                "(obj=%.2f)", pulp.value(prob.objective),
            )
        else:
            logger.warning(
                "[LateSwap-ILP] Non-optimal status: %s",
                pulp.LpStatus.get(status, status),
            )
            return None

        # ═══════════════════════════════════════════════════════════════
        # STEP 8: Extract the ILP solution
        # Read x[p, j].varValue — values > 0.5 indicate assignment.
        # ═══════════════════════════════════════════════════════════════
        ilp_result: Dict[str, Any] = {}
        for (pid, j), var in x.items():
            if var.varValue is not None and var.varValue > 0.5:
                ilp_result[j] = player_lookup[pid]

        if len(ilp_result) != len(entry_state.open_slots):
            logger.warning(
                "[LateSwap-ILP] Incomplete: %d/%d open slots filled",
                len(ilp_result), len(entry_state.open_slots),
            )
            return None

        # ── Post-solve salary verification ────────────────────────────
        ilp_salary = sum(p.salary for p in ilp_result.values())
        if ilp_salary > remaining:
            logger.error(
                "[LateSwap-ILP] SALARY VIOLATION: $%d > $%d remaining",
                ilp_salary, remaining,
            )
            return None

        logger.info(
            "[LateSwap-ILP] SOLVED: %d open slots filled, "
            "$%d / $%d budget used (%.0f%%), "
            "projected FP = %.1f",
            len(ilp_result),
            ilp_salary,
            remaining,
            (ilp_salary / remaining * 100) if remaining > 0 else 0,
            sum(p.projected_fp for p in ilp_result.values()),
        )

        return ilp_result

    # ------------------------------------------------------------------
    # 3. UTIL Rearrangement for Maximum Future Flexibility
    # ------------------------------------------------------------------

    def _rearrange_util_for_flexibility(
        self,
        open_slots: List[str],
        ilp_result: Dict[str, Any],
        game_states: Dict[str, Any],
        sport: str = "nba",
    ) -> Dict[str, Any]:
        """Move the latest-game-time player to the UTIL slot.

        Among unlocked players assigned to open slots, the player whose
        team has the **latest** ``GameState.scheduled_tip`` should be in
        UTIL.  This preserves maximum future swap flexibility (their
        game hasn't started yet, so we can change them last).

        If UTIL is not among the open slots, or no scheduled tips are
        available, the ILP result is returned unchanged.
        """
        from app.services.live_game_state_service import normalise_to_bdl

        elig_map = _NBA_SLOT_ELIGIBILITY if sport == "nba" else _CBB_SLOT_ELIGIBILITY

        # Find the UTIL slot among open slots
        util_slots = [j for j in open_slots if _base_slot(j) == "UTIL"]
        if not util_slots:
            return ilp_result

        util_slot = util_slots[0]

        # Gather (indexed_slot, player, scheduled_tip) for all open-slot assignments
        assignments: List[Tuple[str, Any, Optional[datetime]]] = []
        for j, player in ilp_result.items():
            team_raw = getattr(player, "team_abbreviation", "") or ""
            bdl_team = normalise_to_bdl(team_raw) if team_raw else ""
            gs = game_states.get(bdl_team)
            tip = gs.scheduled_tip if gs else None
            assignments.append((j, player, tip))

        if not assignments:
            return ilp_result

        # Find the player with the latest scheduled tip
        latest = None
        latest_tip: Optional[datetime] = None
        for j, player, tip in assignments:
            if tip is not None:
                if latest_tip is None or tip > latest_tip:
                    latest = (j, player)
                    latest_tip = tip

        if latest is None:
            # No scheduled tips available — can't rearrange
            return ilp_result

        latest_slot, latest_player = latest

        # If already in UTIL, no swap needed
        if latest_slot == util_slot:
            return ilp_result

        # Check eligibility: the current UTIL player must be eligible
        # for the latest player's current slot, and the latest player
        # must be eligible for UTIL (always true for NBA Classic).
        latest_pos = getattr(latest_player, "position", "")
        util_elig = elig_map.get("UTIL", set())
        if latest_pos not in util_elig:
            # Can't move into UTIL (shouldn't happen for NBA Classic)
            return ilp_result

        current_util_player = ilp_result.get(util_slot)
        if current_util_player is None:
            return ilp_result

        # The current UTIL player needs to be eligible for the target slot
        target_base = _base_slot(latest_slot)
        target_elig = elig_map.get(target_base, set())
        current_util_pos = getattr(current_util_player, "position", "")
        if current_util_pos not in target_elig:
            # Can't swap — eligibility violation
            logger.debug(
                "[LateSwap-UTIL] Can't rearrange: %s (%s) not eligible "
                "for %s",
                getattr(current_util_player, "player_name", "?"),
                current_util_pos, target_base,
            )
            return ilp_result

        # Perform the swap
        result = dict(ilp_result)
        result[util_slot] = latest_player
        result[latest_slot] = current_util_player

        logger.info(
            "[LateSwap-UTIL] Rearranged: %s -> UTIL (tip %s), "
            "%s -> %s",
            getattr(latest_player, "player_name", "?"),
            latest_tip.strftime("%I:%M %p") if latest_tip else "?",
            getattr(current_util_player, "player_name", "?"),
            target_base,
        )

        return result

    # ------------------------------------------------------------------
    # 4. Main Orchestrator
    # ------------------------------------------------------------------

    def optimize_entry(
        self,
        entry_data: Dict,
        player_pool: List[Any],
        game_states: Dict[str, Any],
        sport: str = "nba",
        salary_cap: int = DK_SALARY_CAP,
    ) -> LateSwapResult:
        """Optimise a single entry's open slots via ILP.

        Parameters
        ----------
        entry_data : dict
            A single entry dict from ``get_entries_for_slate()``.
            Must have ``entry_id`` and ``lineup`` (list of dicts).
        player_pool : list[PlayerPoolEntry]
            Full player pool from the lineup optimizer.
        game_states : dict[str, GameState]
            Live game states from ``LiveGameStateService``.
        sport : str
            "nba" or "cbb".
        salary_cap : int
            Total salary budget (default 50 000).

        Returns
        -------
        LateSwapResult
            Contains the full 8-player lineup (locked + optimised),
            swap details, and diagnostics.
        """
        entry_id = str(entry_data.get("entry_id", ""))
        lineup = entry_data.get("lineup", [])
        warnings_list: List[str] = []

        if not lineup:
            return LateSwapResult(
                success=False,
                entry_id=entry_id,
                lineup=lineup,
                total_salary=0,
                total_projected_fp=0.0,
                swaps=[],
                locked_count=0,
                open_count=0,
                remaining_salary_used=0,
                warnings=["Entry has no lineup data"],
            )

        # Step 1: Evaluate locked / open state
        state = self.evaluate_entry_state(lineup, game_states, salary_cap)

        if state.all_locked:
            total_sal = sum(p.get("salary", 0) for p in lineup)
            return LateSwapResult(
                success=True,
                entry_id=entry_id,
                lineup=lineup,
                total_salary=total_sal,
                total_projected_fp=0.0,  # Can't compute without pool
                swaps=[],
                locked_count=len(state.locked_players),
                open_count=0,
                remaining_salary_used=0,
                warnings=["All slots locked — no swaps possible"],
            )

        # Step 2: ILP solve for open slots
        ilp_result = self._solve_late_swap(
            entry_state=state,
            player_pool=player_pool,
            game_states=game_states,
            sport=sport,
        )

        if ilp_result is None:
            total_sal = sum(p.get("salary", 0) for p in lineup)
            return LateSwapResult(
                success=False,
                entry_id=entry_id,
                lineup=lineup,
                total_salary=total_sal,
                total_projected_fp=0.0,
                swaps=[],
                locked_count=len(state.locked_players),
                open_count=len(state.open_slots),
                remaining_salary_used=0,
                warnings=["ILP solver failed — original lineup unchanged"],
            )

        # Step 3: UTIL rearrangement for flexibility
        ilp_result = self._rearrange_util_for_flexibility(
            open_slots=state.open_slots,
            ilp_result=ilp_result,
            game_states=game_states,
            sport=sport,
        )

        # Step 4: Merge locked players + ILP result into full lineup
        slot_names = [p["slot"] for p in lineup]
        indexed = _index_slots(slot_names)

        # Build lookup from original lineup for swap comparison
        original_by_slot: Dict[str, Dict] = {}
        for i, p in enumerate(lineup):
            original_by_slot[indexed[i]] = p

        # Build the merged lineup
        new_lineup: List[Dict] = []
        swaps: List[SwapDetail] = []
        total_fp = 0.0

        for i, isl in enumerate(indexed):
            if any(lp.indexed_slot == isl for lp in state.locked_players):
                # Locked — keep original
                new_lineup.append(lineup[i])
            elif isl in ilp_result:
                # ILP-assigned player
                new_player = ilp_result[isl]
                new_entry = {
                    "slot": _base_slot(isl),
                    "dk_player_id": getattr(new_player, "dk_player_id", None)
                                    or getattr(new_player, "player_id", 0),
                    "player_name": getattr(new_player, "player_name", ""),
                    "salary": new_player.salary,
                    "team": getattr(new_player, "team_abbreviation", ""),
                }
                new_lineup.append(new_entry)

                # Track swap (if player changed)
                old = original_by_slot.get(isl, {})
                old_pid = old.get("dk_player_id", 0)
                new_pid = new_entry["dk_player_id"]
                if old_pid != new_pid:
                    old_fp = 0.0
                    new_fp = getattr(new_player, "projected_fp", 0.0)
                    # Try to find the old player's FP in the pool
                    for pp in player_pool:
                        pp_id = getattr(pp, "dk_player_id", None) or getattr(pp, "player_id", 0)
                        if pp_id == old_pid:
                            old_fp = pp.projected_fp
                            break

                    swaps.append(SwapDetail(
                        slot=_base_slot(isl),
                        old_player=old.get("player_name", ""),
                        new_player=new_entry["player_name"],
                        old_salary=old.get("salary", 0),
                        new_salary=new_entry["salary"],
                        projected_fp_gain=round(new_fp - old_fp, 2),
                    ))

                total_fp += getattr(new_player, "projected_fp", 0.0)
            else:
                # Fallback — shouldn't happen if ILP solved correctly
                new_lineup.append(lineup[i])
                warnings_list.append(f"Slot {isl} missing from ILP result")

        # Add locked players' FP (from pool if available)
        for lp in state.locked_players:
            for pp in player_pool:
                pp_id = getattr(pp, "dk_player_id", None) or getattr(pp, "player_id", 0)
                if pp_id == lp.dk_player_id:
                    total_fp += pp.projected_fp
                    break

        total_salary = sum(p.get("salary", 0) for p in new_lineup)
        ilp_salary_used = sum(
            p.get("salary", 0) for i, p in enumerate(new_lineup)
            if indexed[i] in ilp_result
        )

        if swaps:
            total_gain = sum(s.projected_fp_gain for s in swaps)
            warnings_list.append(
                f"Late-swap optimised {len(swaps)} slot(s) "
                f"(+{total_gain:.1f} projected FP)"
            )

        return LateSwapResult(
            success=True,
            entry_id=entry_id,
            lineup=new_lineup,
            total_salary=total_salary,
            total_projected_fp=round(total_fp, 2),
            swaps=swaps,
            locked_count=len(state.locked_players),
            open_count=len(state.open_slots),
            remaining_salary_used=ilp_salary_used,
            warnings=warnings_list,
        )

    # ------------------------------------------------------------------
    # 5. Async: Load + Optimise + Update DB
    # ------------------------------------------------------------------

    async def optimize_and_update_entry(
        self,
        entry_id: str,
        draft_group_id: int,
        player_pool: List[Any],
        game_date: str,
        sport: str = "nba",
        salary_cap: int = DK_SALARY_CAP,
    ) -> LateSwapResult:
        """High-level: load entry -> optimise -> update DB.

        Parameters
        ----------
        entry_id : str
            The DK entry ID to optimise.
        draft_group_id : int
            DraftGroup ID for the slate.
        player_pool : list[PlayerPoolEntry]
            Full player pool from the lineup optimizer.
        game_date : str
            Date in YYYY-MM-DD format for game-state lookup.
        sport : str
            "nba" or "cbb".
        salary_cap : int
            Total salary budget.

        Returns
        -------
        LateSwapResult
        """
        # Step 1: Load entry from DB
        if self._entry_svc is None:
            return LateSwapResult(
                success=False, entry_id=entry_id,
                lineup=[], total_salary=0, total_projected_fp=0.0,
                swaps=[], locked_count=0, open_count=0,
                remaining_salary_used=0,
                warnings=["EntryImportService not available"],
            )

        entries = await self._entry_svc.get_entries_for_slate(
            draft_group_id=draft_group_id, sport=sport,
        )

        target = None
        for e in entries:
            if str(e.get("entry_id")) == str(entry_id):
                target = e
                break

        if target is None:
            return LateSwapResult(
                success=False, entry_id=entry_id,
                lineup=[], total_salary=0, total_projected_fp=0.0,
                swaps=[], locked_count=0, open_count=0,
                remaining_salary_used=0,
                warnings=[f"Entry {entry_id} not found in DB for DG={draft_group_id}"],
            )

        # Step 2: Fetch game states
        if self._live_gs is None:
            return LateSwapResult(
                success=False, entry_id=entry_id,
                lineup=target.get("lineup", []),
                total_salary=target.get("total_salary", 0),
                total_projected_fp=0.0,
                swaps=[], locked_count=0, open_count=0,
                remaining_salary_used=0,
                warnings=["LiveGameStateService not available"],
            )

        game_states = await self._live_gs.fetch_game_states(game_date)

        # Step 3: Optimise
        result = self.optimize_entry(
            entry_data=target,
            player_pool=player_pool,
            game_states=game_states,
            sport=sport,
            salary_cap=salary_cap,
        )

        # Step 4: Update DB if optimisation succeeded and swaps were made
        if result.success and result.swaps:
            await self._update_entry_in_db(
                entry_id=entry_id,
                new_lineup=result.lineup,
                total_salary=result.total_salary,
            )

        return result

    async def optimize_all_entries(
        self,
        draft_group_id: int,
        player_pool: List[Any],
        game_date: str,
        sport: str = "nba",
        salary_cap: int = DK_SALARY_CAP,
    ) -> List[LateSwapResult]:
        """Optimise all entries for a draft group in sequence.

        Returns a list of ``LateSwapResult`` — one per entry.
        """
        if self._entry_svc is None:
            return []

        entries = await self._entry_svc.get_entries_for_slate(
            draft_group_id=draft_group_id, sport=sport,
        )

        if not entries:
            return []

        # Fetch game states once for all entries
        if self._live_gs is None:
            return [
                LateSwapResult(
                    success=False,
                    entry_id=str(e.get("entry_id", "")),
                    lineup=e.get("lineup", []),
                    total_salary=e.get("total_salary", 0),
                    total_projected_fp=0.0,
                    swaps=[], locked_count=0, open_count=0,
                    remaining_salary_used=0,
                    warnings=["LiveGameStateService not available"],
                )
                for e in entries
            ]

        game_states = await self._live_gs.fetch_game_states(game_date)

        results: List[LateSwapResult] = []
        for entry in entries:
            result = self.optimize_entry(
                entry_data=entry,
                player_pool=player_pool,
                game_states=game_states,
                sport=sport,
                salary_cap=salary_cap,
            )

            # Update DB if swaps were made
            if result.success and result.swaps:
                await self._update_entry_in_db(
                    entry_id=str(entry.get("entry_id", "")),
                    new_lineup=result.lineup,
                    total_salary=result.total_salary,
                )

            results.append(result)

        total_swaps = sum(len(r.swaps) for r in results)
        entries_altered = sum(1 for r in results if r.swaps)
        logger.info(
            "[LateSwap] Batch complete: %d entries, %d total swaps",
            len(results), total_swaps,
        )

        # ── SMS Alert: Late Swap Completion ──────────────────────
        if total_swaps > 0:
            try:
                from app.services.notification_service import NotificationService
                notifier = NotificationService()
                if notifier.sms_available:
                    from datetime import datetime
                    _now = datetime.now().strftime("%-I:%M %p")
                    # Collect unique new player names from swaps
                    _new_players = set()
                    for r in results:
                        for s in r.swaps:
                            _in = s.get("in", s.get("new_player", ""))
                            if _in:
                                _new_players.add(_in)
                    _core = ", ".join(list(_new_players)[:5])
                    notifier.send_sms_alert(
                        f"[DFS ALERT] {_now} Swap Complete. "
                        f"{entries_altered} lineup(s) altered, "
                        f"{total_swaps} swap(s). "
                        f"New Core: {_core}"
                    )
            except Exception as _sms_err:
                logger.debug("[LateSwap] SMS alert failed: %s", _sms_err)

        return results

    # ------------------------------------------------------------------
    # DB Helpers
    # ------------------------------------------------------------------

    @staticmethod
    async def _update_entry_in_db(
        entry_id: str,
        new_lineup: List[Dict],
        total_salary: int,
    ) -> bool:
        """Upsert the optimised lineup back to the active_entries table."""
        from app.db.database import is_db_available, get_session
        from app.db.models import ActiveEntry
        from sqlalchemy import update

        if not is_db_available():
            logger.warning("[LateSwap] DB unavailable — lineup not persisted")
            return False

        try:
            async with get_session() as session:
                stmt = (
                    update(ActiveEntry)
                    .where(ActiveEntry.entry_id == str(entry_id))
                    .values(
                        lineup=new_lineup,
                        total_salary=total_salary,
                        updated_at=datetime.now(timezone.utc),
                    )
                )
                await session.execute(stmt)
                await session.commit()

            logger.info(
                "[LateSwap] Updated entry %s in DB (salary=$%d)",
                entry_id, total_salary,
            )
            return True

        except Exception as e:
            logger.error(
                "[LateSwap] DB update failed for entry %s: %s",
                entry_id, e, exc_info=True,
            )
            return False


# ============================================================================
# Delta Patcher — Pre-Lock Fast Swap Engine
# ============================================================================
# When a late scratch is detected before lock, the full ILP re-solve in
# LateSwapService.optimize_and_update_entry is overkill (and slow).
# The delta patcher uses a greedy single-player replacement per affected
# lineup: rip the scratched player, find the highest-EV legal substitute,
# patch in-place.  Processes 150 lineups in <50ms.

@dataclass
class PatchResult:
    """Result of patching a single lineup."""

    lineup_index: int
    swapped_out: str                # Scratched player name
    swapped_in: str                 # Replacement player name
    swapped_in_id: int
    old_salary: int
    new_salary: int
    old_projected_fp: float
    new_projected_fp: float
    fp_delta: float                 # positive = improvement
    salary_delta: int
    lineup_total_salary: int
    lineup_total_fp: float


@dataclass
class FastPatchReport:
    """Aggregate result of fast_patch_lineups()."""

    scratched_player_id: int
    scratched_player_name: str
    lineups_scanned: int
    lineups_affected: int
    lineups_patched: int
    lineups_failed: int             # No valid replacement found
    patches: List[PatchResult]
    failed_indices: List[int]       # Lineup indices with no legal swap
    elapsed_ms: float


def fast_patch_lineups(
    scratched_player_id: int,
    player_pool: List[Any],
    lineups: List[Any],
    salary_cap: int = DK_SALARY_CAP,
    min_unique_games: int = 2,
    sport: str = "nba",
) -> FastPatchReport:
    """Greedy delta-patch for a single scratched player across all lineups.

    This is the pre-lock speed path.  No ILP, no PuLP, no threads.
    Pure Python greedy replacement in O(L × P) where L = affected lineups
    and P = pool size.

    Parameters
    ----------
    scratched_player_id : int
        The player_id of the scratched player.
    player_pool : list[PlayerPoolEntry]
        The current player pool with updated projections (post-scratch).
        Must already reflect the new TopDownMinutes allocations.
    lineups : list[OptimizedLineup]
        The saved lineups to patch.  Modified IN-PLACE for affected lineups.
    salary_cap : int
        DK salary cap (default 50,000).
    min_unique_games : int
        Minimum distinct games required in a valid DK lineup (default 2).
    sport : str
        "nba" or "cbb" — determines slot eligibility rules.

    Returns
    -------
    FastPatchReport
        Summary of all patches applied plus any lineups that couldn't be fixed.
    """
    import time
    _t0 = time.perf_counter()

    slot_elig = _NBA_SLOT_ELIGIBILITY if sport == "nba" else _CBB_SLOT_ELIGIBILITY

    # ── Build pool lookup ────────────────────────────────────────────
    # Pre-sort by projected_fp descending so the first legal match is
    # the best one (greedy optimality for single-swap).
    pool_sorted = sorted(
        player_pool,
        key=lambda p: getattr(p, "projected_fp", 0) or 0,
        reverse=True,
    )

    # Set of all player_ids in the pool (for O(1) membership checks)
    pool_by_id = {
        getattr(p, "player_id", 0): p for p in player_pool
    }

    # Identify the scratched player's name (for logging)
    _scratched_p = pool_by_id.get(scratched_player_id)
    _scratched_name = (
        getattr(_scratched_p, "player_name", f"ID:{scratched_player_id}")
        if _scratched_p
        else f"ID:{scratched_player_id}"
    )

    patches: List[PatchResult] = []
    failed_indices: List[int] = []

    for li, lineup in enumerate(lineups):
        players = lineup.players if hasattr(lineup, "players") else []

        # ── Step 1: Find the scratched player in this lineup ─────────
        scratched_slot_idx = None
        scratched_lp = None
        for pi, lp in enumerate(players):
            if lp.player_id == scratched_player_id:
                scratched_slot_idx = pi
                scratched_lp = lp
                break

        if scratched_slot_idx is None:
            continue  # This lineup doesn't contain the scratched player

        # ── Step 2: Compute budget and constraints ───────────────────
        # Remove scratched player's salary to get remaining spend
        lineup_salary_without = lineup.total_salary - scratched_lp.salary
        available_budget = salary_cap - lineup_salary_without

        # The slot the scratched player was filling
        target_slot = scratched_lp.roster_slot

        # Positions eligible for this slot
        eligible_positions = slot_elig.get(target_slot, set())

        # Player IDs already in this lineup (excluding the scratched one)
        existing_ids = {
            lp.player_id for lp in players
            if lp.player_id != scratched_player_id
        }

        # Game IDs already in this lineup (excluding the scratched player)
        # Used for the 2-game minimum validation
        existing_game_ids = set()
        for lp in players:
            if lp.player_id == scratched_player_id:
                continue
            gid = getattr(lp, "game_id", None)
            if gid:
                existing_game_ids.add(gid)

        # ── Step 3: Greedy scan for best replacement ─────────────────
        # Pool is pre-sorted by projected_fp descending, so the first
        # candidate that passes all filters is the optimal swap.
        best_candidate = None
        for candidate in pool_sorted:
            cid = candidate.player_id
            c_salary = candidate.salary or 0
            c_fp = getattr(candidate, "projected_fp", 0) or 0
            c_pos = (candidate.position or "").upper()

            # Filter 1: Not already in lineup
            if cid in existing_ids:
                continue

            # Filter 2: Not the scratched player themselves
            if cid == scratched_player_id:
                continue

            # Filter 3: Fits the salary budget
            if c_salary > available_budget:
                continue

            # Filter 4: Position eligible for the target slot
            # Split dual positions (PG/SG → check both)
            c_positions = {
                pp.strip() for pp in c_pos.replace("-", "/").split("/")
            }
            if not (c_positions & eligible_positions):
                continue

            # Filter 5: Must not be scratched/Out themselves
            c_minutes = getattr(candidate, "projected_minutes", 0) or 0
            if c_minutes <= 0 or c_fp <= 0:
                continue

            # Filter 6: 2-game minimum validation
            # If the lineup currently has players from only 1 game
            # (after removing the scratched player), the replacement
            # MUST come from a different game to satisfy the DK rule.
            c_game_id = getattr(candidate, "game_id", None)
            if len(existing_game_ids) < min_unique_games:
                # Need this candidate to bring a new game
                if c_game_id and c_game_id not in existing_game_ids:
                    pass  # Good — adds a new game
                elif len(existing_game_ids) >= min_unique_games - 1:
                    pass  # Already have enough games from other players
                else:
                    continue  # Would leave lineup with < min games

            best_candidate = candidate
            break

        # ── Step 4: Apply the patch or record failure ────────────────
        if best_candidate is None:
            failed_indices.append(li)
            logger.warning(
                "[DeltaPatch] Lineup %d: NO valid replacement for %s "
                "(%s slot, budget=$%d)",
                li, _scratched_name, target_slot, available_budget,
            )
            continue

        # Build the replacement LineupPlayer
        from app.models.lineup import LineupPlayer
        replacement = LineupPlayer(
            player_id=best_candidate.player_id,
            player_name=best_candidate.player_name,
            display_name=getattr(best_candidate, "display_name", None),
            position=best_candidate.position,
            roster_slot=target_slot,
            team_abbreviation=best_candidate.team_abbreviation,
            salary=best_candidate.salary,
            projected_fp=getattr(best_candidate, "projected_fp", 0) or 0,
            floor_fp=getattr(best_candidate, "floor_fp", 0) or 0,
            ceiling_fp=getattr(best_candidate, "ceiling_fp", 0) or 0,
            projected_minutes=getattr(best_candidate, "projected_minutes", 0) or 0,
            projected_stats=getattr(best_candidate, "projected_stats", None),
            dk_player_id=getattr(best_candidate, "dk_player_id", None),
        )

        # Patch in-place
        old_fp = scratched_lp.projected_fp
        players[scratched_slot_idx] = replacement

        # Update lineup totals
        new_total_salary = lineup_salary_without + replacement.salary
        fp_delta = replacement.projected_fp - old_fp
        new_total_fp = lineup.total_projected_fp + fp_delta

        lineup.total_salary = new_total_salary
        lineup.salary_remaining = salary_cap - new_total_salary
        lineup.total_projected_fp = round(new_total_fp, 2)

        # Recompute floor/ceiling
        lineup.total_floor_fp = round(
            sum(lp.floor_fp for lp in players), 2
        )
        lineup.total_ceiling_fp = round(
            sum(lp.ceiling_fp for lp in players), 2
        )

        patches.append(PatchResult(
            lineup_index=li,
            swapped_out=_scratched_name,
            swapped_in=best_candidate.player_name,
            swapped_in_id=best_candidate.player_id,
            old_salary=scratched_lp.salary,
            new_salary=replacement.salary,
            old_projected_fp=round(old_fp, 2),
            new_projected_fp=round(replacement.projected_fp, 2),
            fp_delta=round(fp_delta, 2),
            salary_delta=replacement.salary - scratched_lp.salary,
            lineup_total_salary=new_total_salary,
            lineup_total_fp=round(new_total_fp, 2),
        ))

        logger.info(
            "[DeltaPatch] Lineup %d: %s ($%d, %.1ffp) → %s ($%d, %.1ffp) "
            "[%s slot, delta=%.1ffp, sal=$%d→$%d]",
            li, _scratched_name, scratched_lp.salary, old_fp,
            best_candidate.player_name, replacement.salary,
            replacement.projected_fp, target_slot, fp_delta,
            new_total_salary - replacement.salary + scratched_lp.salary,
            new_total_salary,
        )

    elapsed_ms = (time.perf_counter() - _t0) * 1000

    report = FastPatchReport(
        scratched_player_id=scratched_player_id,
        scratched_player_name=_scratched_name,
        lineups_scanned=len(lineups),
        lineups_affected=len(patches) + len(failed_indices),
        lineups_patched=len(patches),
        lineups_failed=len(failed_indices),
        patches=patches,
        failed_indices=failed_indices,
        elapsed_ms=round(elapsed_ms, 1),
    )

    logger.info(
        "[DeltaPatch] COMPLETE: %s scratched — %d/%d lineups patched "
        "(%d failed) in %.1fms",
        _scratched_name, report.lineups_patched, report.lineups_affected,
        report.lineups_failed, report.elapsed_ms,
    )

    return report


# ============================================================================
# Combinatorial Sub-Slate Optimizer — Mini-ILP Delta Engine
# ============================================================================
# Upgrades the greedy 1-for-1 swap to a full combinatorial re-optimization
# of ALL unlocked slots in an affected lineup.  When a star is scratched
# 20 minutes before lock, the greedy patcher leaves $3K-$5K in dead salary.
# The combo patcher spends that salary optimally across 2-4 open slots.
#
# Performance target: <2 seconds per lineup, <30 seconds for 150 lineups.
# Achieved via aggressive pool pre-filtering (salary + position eligibility)
# before ILP variable creation, plus a tight 1.5s solver time limit.

# ── Timing constants ─────────────────────────────────────────────────
_COMBO_ILP_TIME_LIMIT = 1.5       # CBC solver time limit per lineup (seconds)
_COMBO_ILP_GAP_REL = 0.01        # 1% optimality gap (speed over perfection)
_COMBO_POOL_MAX_PER_SLOT = 40    # Max candidates per slot (pre-filter)
_COMBO_MIN_UNIQUE_GAMES = 2      # DK minimum games rule


@dataclass
class ComboPatchResult:
    """Result of combo-patching a single lineup."""

    lineup_index: int
    slots_unlocked: int           # How many slots were re-optimized
    slots_changed: int            # How many actually swapped players
    swaps: List[SwapDetail]       # Individual swap details
    old_total_fp: float
    new_total_fp: float
    fp_delta: float
    old_total_salary: int
    new_total_salary: int
    salary_delta: int
    solve_time_ms: float
    method: str                   # "ilp" or "greedy_fallback"


@dataclass
class ComboPatchReport:
    """Aggregate result of combo_patch_lineups()."""

    scratched_player_id: int
    scratched_player_name: str
    lineups_scanned: int
    lineups_affected: int
    lineups_patched: int
    lineups_failed: int
    total_fp_gained: float        # Sum of fp_delta across all patched lineups
    avg_salary_recovered: float   # Average salary recaptured per lineup
    patches: List[ComboPatchResult]
    failed_indices: List[int]
    elapsed_ms: float


def _player_positions(pos_str: str) -> set:
    """Parse a position string like 'PG/SG' or 'SF-PF' into a set."""
    return {p.strip() for p in pos_str.replace("-", "/").split("/") if p.strip()}


def _slot_accepts_player(slot: str, player_pos: str, elig_map: dict) -> bool:
    """Check if a player's position(s) can fill a given roster slot."""
    eligible = elig_map.get(slot, set())
    return bool(_player_positions(player_pos) & eligible)


def combo_patch_lineups(
    scratched_player_id: int,
    player_pool: List[Any],
    lineups: List[Any],
    salary_cap: int = DK_SALARY_CAP,
    min_unique_games: int = _COMBO_MIN_UNIQUE_GAMES,
    sport: str = "nba",
    game_start_times: Optional[Dict[str, bool]] = None,
) -> ComboPatchReport:
    """Combinatorial sub-slate optimization for scratched-player lineups.

    For each affected lineup:
    1. Identify ALL unlocked slots (scratched player + any player whose
       game hasn't started yet)
    2. Lock the remaining players as fixed constants
    3. Solve a mini-ILP over the unlocked slots to maximize total EV
       within the remaining salary budget
    4. Patch the lineup in-place with the optimal combination

    Parameters
    ----------
    scratched_player_id : int
        The player_id that triggered this patch.
    player_pool : list[PlayerPoolEntry]
        Current pool with updated projections.
    lineups : list[OptimizedLineup]
        Saved lineups — modified IN-PLACE.
    salary_cap : int
        DK salary cap (default 50,000).
    min_unique_games : int
        Minimum distinct games required (default 2).
    sport : str
        "nba" or "cbb".
    game_start_times : dict[str, bool] | None
        Mapping of game_id -> has_started (True = locked).
        If None, only the scratched player's slot is unlocked.

    Returns
    -------
    ComboPatchReport
    """
    import time as _time
    _t0 = _time.perf_counter()

    elig_map = _NBA_SLOT_ELIGIBILITY if sport == "nba" else _CBB_SLOT_ELIGIBILITY
    _game_started = game_start_times or {}

    # Build pool lookup
    pool_by_id = {
        getattr(p, "player_id", 0): p for p in player_pool
    }
    _scratched_p = pool_by_id.get(scratched_player_id)
    _scratched_name = (
        getattr(_scratched_p, "player_name", f"ID:{scratched_player_id}")
        if _scratched_p
        else f"ID:{scratched_player_id}"
    )

    # Pre-filter pool: remove zero-projection and scratched players
    viable_pool = [
        p for p in player_pool
        if (getattr(p, "projected_fp", 0) or 0) > 0
        and (getattr(p, "projected_minutes", 0) or 0) > 0
        and p.player_id != scratched_player_id
    ]

    patches: List[ComboPatchResult] = []
    failed_indices: List[int] = []

    for li, lineup in enumerate(lineups):
        players = lineup.players if hasattr(lineup, "players") else []

        # ── Step 1: Does this lineup contain the scratched player? ───
        has_scratched = any(
            lp.player_id == scratched_player_id for lp in players
        )
        if not has_scratched:
            continue

        # ── Step 2: Classify slots as locked vs. unlocked ────────────
        # Unlocked = scratched player OR player whose game hasn't started
        locked_players = []     # (index, LineupPlayer)
        unlocked_slots = []     # (index, LineupPlayer, roster_slot)

        for pi, lp in enumerate(players):
            if lp.player_id == scratched_player_id:
                # Always unlocked (scratched)
                unlocked_slots.append((pi, lp, lp.roster_slot))
            elif _game_started.get(getattr(lp, "game_id", None), False):
                # Game has started — locked
                locked_players.append((pi, lp))
            else:
                # Game hasn't started — unlocked (can be re-optimized)
                unlocked_slots.append((pi, lp, lp.roster_slot))

        if not unlocked_slots:
            failed_indices.append(li)
            continue

        # ── Step 3: Compute budget and constraints ───────────────────
        locked_salary = sum(lp.salary for _, lp in locked_players)
        locked_fp = sum(lp.projected_fp for _, lp in locked_players)
        remaining_salary = salary_cap - locked_salary

        locked_ids = {lp.player_id for _, lp in locked_players}
        locked_game_ids = {
            getattr(lp, "game_id", None)
            for _, lp in locked_players
            if getattr(lp, "game_id", None)
        }

        # Slots to fill (with indexed keys for uniqueness)
        open_slot_keys = []  # (original_index, indexed_key, base_slot)
        _slot_counts: Dict[str, int] = {}
        for pi, lp, slot in unlocked_slots:
            idx = _slot_counts.get(slot, 0)
            _slot_counts[slot] = idx + 1
            indexed_key = f"{slot}_{idx}"
            open_slot_keys.append((pi, indexed_key, slot))

        # ── Step 4: Pre-filter pool per slot ─────────────────────────
        # Only keep candidates that fit at least one open slot AND
        # are within salary budget.  Sort by FP desc and take top N.
        slot_candidates: Dict[str, List] = {ik: [] for _, ik, _ in open_slot_keys}

        for p in viable_pool:
            if p.player_id in locked_ids:
                continue
            p_sal = p.salary or 0
            if p_sal > remaining_salary:
                continue
            p_pos = (p.position or "").upper()

            for _pi, ik, base_slot in open_slot_keys:
                if _slot_accepts_player(base_slot, p_pos, elig_map):
                    slot_candidates[ik].append(p)

        # Trim each slot to top N by projected_fp
        for ik in slot_candidates:
            slot_candidates[ik].sort(
                key=lambda p: getattr(p, "projected_fp", 0) or 0,
                reverse=True,
            )
            slot_candidates[ik] = slot_candidates[ik][:_COMBO_POOL_MAX_PER_SLOT]

        # Deduplicate: collect all unique candidate player IDs
        all_candidate_ids = set()
        for cands in slot_candidates.values():
            for p in cands:
                all_candidate_ids.add(p.player_id)

        if not all_candidate_ids:
            failed_indices.append(li)
            logger.warning(
                "[ComboPatch] Lineup %d: No candidates for %d open slots "
                "(budget=$%d)",
                li, len(open_slot_keys), remaining_salary,
            )
            continue

        # ── Step 5: Build & solve mini-ILP ───────────────────────────
        _solve_t0 = _time.perf_counter()
        result_assignments = None
        method = "ilp"

        if _PULP_AVAILABLE and len(open_slot_keys) > 1:
            try:
                result_assignments = _solve_combo_ilp(
                    open_slot_keys=open_slot_keys,
                    slot_candidates=slot_candidates,
                    pool_by_id=pool_by_id,
                    remaining_salary=remaining_salary,
                    locked_game_ids=locked_game_ids,
                    min_unique_games=min_unique_games,
                    elig_map=elig_map,
                )
            except Exception as exc:
                logger.warning(
                    "[ComboPatch] Lineup %d: ILP failed (%s), "
                    "falling back to greedy",
                    li, exc,
                )

        # Greedy fallback (single-slot or ILP failure)
        if result_assignments is None:
            method = "greedy_fallback"
            result_assignments = _solve_combo_greedy(
                open_slot_keys=open_slot_keys,
                slot_candidates=slot_candidates,
                remaining_salary=remaining_salary,
                locked_game_ids=locked_game_ids,
                min_unique_games=min_unique_games,
            )

        _solve_ms = (_time.perf_counter() - _solve_t0) * 1000

        if result_assignments is None:
            failed_indices.append(li)
            logger.warning(
                "[ComboPatch] Lineup %d: No feasible solution "
                "(%s, %d open slots, budget=$%d)",
                li, method, len(open_slot_keys), remaining_salary,
            )
            continue

        # ── Step 6: Apply the solution in-place ──────────────────────
        swaps = []
        for (orig_idx, _ik, slot), new_player in zip(
            open_slot_keys, result_assignments
        ):
            old_lp = players[orig_idx]
            if new_player.player_id == old_lp.player_id:
                continue  # Same player — no swap needed

            from app.models.lineup import LineupPlayer
            replacement = LineupPlayer(
                player_id=new_player.player_id,
                player_name=new_player.player_name,
                display_name=getattr(new_player, "display_name", None),
                position=new_player.position,
                roster_slot=slot,
                team_abbreviation=new_player.team_abbreviation,
                salary=new_player.salary,
                projected_fp=getattr(new_player, "projected_fp", 0) or 0,
                floor_fp=getattr(new_player, "floor_fp", 0) or 0,
                ceiling_fp=getattr(new_player, "ceiling_fp", 0) or 0,
                projected_minutes=getattr(new_player, "projected_minutes", 0) or 0,
                projected_stats=getattr(new_player, "projected_stats", None),
                dk_player_id=getattr(new_player, "dk_player_id", None),
                game_id=getattr(new_player, "game_id", None),
            )
            players[orig_idx] = replacement

            swaps.append(SwapDetail(
                slot=slot,
                old_player=old_lp.player_name,
                new_player=new_player.player_name,
                old_salary=old_lp.salary,
                new_salary=new_player.salary,
                projected_fp_gain=round(
                    replacement.projected_fp - old_lp.projected_fp, 2
                ),
            ))

        # Update lineup totals
        new_total_salary = sum(lp.salary for lp in players)
        new_total_fp = round(sum(lp.projected_fp for lp in players), 2)
        old_total_fp = lineup.total_projected_fp

        lineup.total_salary = new_total_salary
        lineup.salary_remaining = salary_cap - new_total_salary
        lineup.total_projected_fp = new_total_fp
        lineup.total_floor_fp = round(sum(lp.floor_fp for lp in players), 2)
        lineup.total_ceiling_fp = round(sum(lp.ceiling_fp for lp in players), 2)

        fp_delta = round(new_total_fp - old_total_fp, 2)

        patches.append(ComboPatchResult(
            lineup_index=li,
            slots_unlocked=len(open_slot_keys),
            slots_changed=len(swaps),
            swaps=swaps,
            old_total_fp=round(old_total_fp, 2),
            new_total_fp=new_total_fp,
            fp_delta=fp_delta,
            old_total_salary=lineup.total_salary - sum(
                s.new_salary - s.old_salary for s in swaps
            ),
            new_total_salary=new_total_salary,
            salary_delta=sum(s.new_salary - s.old_salary for s in swaps),
            solve_time_ms=round(_solve_ms, 1),
            method=method,
        ))

        if swaps:
            logger.info(
                "[ComboPatch] Lineup %d: %d/%d slots re-optimized via %s "
                "(%.1fms, FP %+.1f, Sal $%d→$%d) %s",
                li, len(swaps), len(open_slot_keys), method, _solve_ms,
                fp_delta, new_total_salary - sum(s.new_salary - s.old_salary for s in swaps),
                new_total_salary,
                " | ".join(
                    f"{s.old_player}→{s.new_player}" for s in swaps
                ),
            )

    elapsed_ms = (_time.perf_counter() - _t0) * 1000

    total_fp_gained = sum(p.fp_delta for p in patches)
    avg_sal_recovered = (
        sum(p.salary_delta for p in patches) / max(1, len(patches))
        if patches else 0.0
    )

    report = ComboPatchReport(
        scratched_player_id=scratched_player_id,
        scratched_player_name=_scratched_name,
        lineups_scanned=len(lineups),
        lineups_affected=len(patches) + len(failed_indices),
        lineups_patched=len(patches),
        lineups_failed=len(failed_indices),
        total_fp_gained=round(total_fp_gained, 2),
        avg_salary_recovered=round(avg_sal_recovered, 0),
        patches=patches,
        failed_indices=failed_indices,
        elapsed_ms=round(elapsed_ms, 1),
    )

    logger.info(
        "[ComboPatch] COMPLETE: %s scratched — %d/%d lineups patched "
        "(%d failed) in %.1fms | Total FP gained: %+.1f, "
        "Avg salary recovered: $%+.0f",
        _scratched_name, report.lineups_patched, report.lineups_affected,
        report.lineups_failed, report.elapsed_ms,
        report.total_fp_gained, report.avg_salary_recovered,
    )

    return report


def _solve_combo_ilp(
    open_slot_keys: List[Tuple[int, str, str]],
    slot_candidates: Dict[str, List],
    pool_by_id: Dict[int, Any],
    remaining_salary: int,
    locked_game_ids: set,
    min_unique_games: int,
    elig_map: dict,
) -> Optional[List]:
    """Solve the mini-ILP for one lineup's open slots.

    Returns a list of PlayerPoolEntry objects (one per open slot, in order)
    or None if infeasible.
    """
    prob = pulp.LpProblem("ComboPatch", pulp.LpMaximize)

    # ── Decision variables ───────────────────────────────────────────
    # x[(pid, indexed_slot)] = 1 if player pid fills indexed_slot
    x: Dict[Tuple[int, str], Any] = {}
    vars_by_slot: Dict[str, List[Tuple[int, Any]]] = {
        ik: [] for _, ik, _ in open_slot_keys
    }
    vars_by_player: Dict[int, List[Tuple[str, Any]]] = {}

    for _, ik, base_slot in open_slot_keys:
        for p in slot_candidates.get(ik, []):
            pid = p.player_id
            key = (pid, ik)
            if key in x:
                continue  # Already created
            var = pulp.LpVariable(f"cp_{pid}_{ik}", cat="Binary")
            x[key] = var
            vars_by_slot[ik].append((pid, var))
            if pid not in vars_by_player:
                vars_by_player[pid] = []
            vars_by_player[pid].append((ik, var))

    if not x:
        return None

    # ── Objective: maximize total projected FP ───────────────────────
    prob += pulp.lpSum(
        (getattr(pool_by_id.get(pid), "projected_fp", 0) or 0) * var
        for (pid, _), var in x.items()
    )

    # ── C1: Salary budget ────────────────────────────────────────────
    prob += (
        pulp.lpSum(
            (getattr(pool_by_id.get(pid), "salary", 0) or 0) * var
            for (pid, _), var in x.items()
        ) <= remaining_salary,
        "C1_salary",
    )

    # ── C2: Each open slot filled by exactly one player ──────────────
    for _, ik, _ in open_slot_keys:
        slot_vars = vars_by_slot.get(ik, [])
        if not slot_vars:
            return None  # Infeasible — no candidates for a slot
        prob += (
            pulp.lpSum(var for _, var in slot_vars) == 1,
            f"C2_fill_{ik}",
        )

    # ── C3: Each player used at most once ────────────────────────────
    for pid, pv_list in vars_by_player.items():
        prob += (
            pulp.lpSum(var for _, var in pv_list) <= 1,
            f"C3_uniq_{pid}",
        )

    # ── C4: Minimum unique games (DK 2-game rule) ────────────────────
    # Collect all game_ids from candidates.  If locked players already
    # cover enough games, this constraint is automatically satisfied.
    if min_unique_games > 0 and len(locked_game_ids) < min_unique_games:
        # Need at least (min_unique_games - len(locked_game_ids)) new games
        needed = min_unique_games - len(locked_game_ids)
        game_ids_in_candidates: Dict[str, List] = {}  # game_id -> [(pid, ik, var)]
        for (pid, ik), var in x.items():
            p = pool_by_id.get(pid)
            gid = getattr(p, "game_id", None) if p else None
            if gid and gid not in locked_game_ids:
                game_ids_in_candidates.setdefault(gid, []).append(var)

        if game_ids_in_candidates and needed > 0:
            # Binary: game g is "used" if any player from game g is selected
            game_used = {}
            for gid, g_vars in game_ids_in_candidates.items():
                game_used[gid] = pulp.LpVariable(
                    f"game_{gid}", cat="Binary",
                )
                # game_used[g] >= x[p,j] for any player in game g
                # Simplified: game_used[g] <= sum of all x[p,j] in game g
                # and game_used[g] >= x[p,j] for each (implied by sum >= game_used)
                prob += (
                    pulp.lpSum(g_vars) >= game_used[gid],
                    f"C4_game_lb_{gid}",
                )
                prob += (
                    game_used[gid] * len(g_vars) >= pulp.lpSum(g_vars),
                    f"C4_game_ub_{gid}",
                )

            prob += (
                pulp.lpSum(game_used.values()) >= needed,
                "C4_min_games",
            )

    # ── Solve ────────────────────────────────────────────────────────
    import warnings
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=UserWarning)
        solver = pulp.PULP_CBC_CMD(
            msg=0,
            timeLimit=_COMBO_ILP_TIME_LIMIT,
            presolve=True,
            gapRel=_COMBO_ILP_GAP_REL,
        )
        try:
            status = prob.solve(solver)
        except Exception:
            return None

    # Accept optimal or feasible incumbent
    if status != pulp.constants.LpStatusOptimal:
        if prob.sol_status != pulp.constants.LpSolutionIntegerFeasible:
            return None

    # ── Extract solution ─────────────────────────────────────────────
    result = []
    for _, ik, _ in open_slot_keys:
        assigned_pid = None
        for pid, var in vars_by_slot[ik]:
            if var.varValue is not None and var.varValue > 0.5:
                assigned_pid = pid
                break
        if assigned_pid is None:
            return None  # Slot unfilled — shouldn't happen
        p = pool_by_id.get(assigned_pid)
        if p is None:
            return None
        result.append(p)

    return result


def _solve_combo_greedy(
    open_slot_keys: List[Tuple[int, str, str]],
    slot_candidates: Dict[str, List],
    remaining_salary: int,
    locked_game_ids: set,
    min_unique_games: int,
) -> Optional[List]:
    """Greedy fallback: fill slots one at a time, highest FP first.

    Used when PuLP is unavailable or ILP fails.  Not globally optimal
    but guaranteed fast.
    """
    used_ids: set = set()
    used_salary = 0
    result = []

    # Sort slots by number of candidates (most constrained first)
    sorted_slots = sorted(
        open_slot_keys,
        key=lambda s: len(slot_candidates.get(s[1], [])),
    )

    for _, ik, base_slot in sorted_slots:
        budget_left = remaining_salary - used_salary
        best = None
        for p in slot_candidates.get(ik, []):
            if p.player_id in used_ids:
                continue
            if (p.salary or 0) > budget_left:
                continue
            best = p
            break  # Pre-sorted by FP desc, first valid = best

        if best is None:
            return None  # Can't fill this slot
        result.append(best)
        used_ids.add(best.player_id)
        used_salary += best.salary or 0

    # Re-order result to match original open_slot_keys order
    # (sorted_slots may have reordered them)
    idx_map = {ik: i for i, (_, ik, _) in enumerate(sorted_slots)}
    final = [None] * len(open_slot_keys)
    for i, (_, ik, _) in enumerate(open_slot_keys):
        final[i] = result[idx_map[ik]]

    return final
