"""End-to-end integration test for MLB lineup generation (Prompt 7.14).

The acceptance criteria from the prompt spec:

  * The process completes without hanging (strict timeout).
  * Returns exactly 3 lineups.
  * Each lineup has exactly 10 players matching the MLB slot
    requirements (P, P, C, 1B, 2B, 3B, SS, OF, OF, OF).
  * No lineup violates the "Max 5 hitters per team" rule
    (DK MLB Classic — pitchers are exempt from this cap).

This is the headline regression test for the MLB / NFL
"direct-ILP fast path" added in Prompt 7.14, which bypasses
the NBA-tuned K-Best diversification loop.  K-Best was
hanging on MLB pools because the strict 5-stack + pitcher
fade constraints interacted badly with K-Best's overlap-
exclusion pruning, leaving CBC to burn the time budget
proving infeasibility.  The direct-ILP path iterates the
ILP with per-attempt projection noise + a soft "previously
used" penalty for diversification, time-bounded so it can't
hang.

The test mocks ``build_player_pool`` and ``_enrich_pool`` so
the optimizer never touches the live DraftKings / projections
APIs — the run is fully deterministic and fast (target < 30 s).
"""

from __future__ import annotations

import threading
from typing import Dict, List, Tuple
from unittest.mock import patch

import pytest

from app.models.lineup import (
    MultiLineupRequest,
    MultiLineupResponse,
    PlayerPoolEntry,
)
from app.services.lineup_optimizer_service import (
    LineupOptimizerService,
    _PULP_AVAILABLE,
    _enriched_cache,
    _enriched_lock,
    _strategy_cache,
    _strategy_lock,
)


# Hard ceiling — the spec calls this "strict timeout" so a hang
# fails the test rather than blocking CI.  The direct-ILP path
# typically completes in 1–3 seconds for 3 lineups; 60 s is a
# generous upper bound that leaves headroom for slow CBC builds.
_HARD_TIMEOUT_S = 60.0


# ─────────────────────────────────────────────────────────────────
# MLB pool fixture — 4 games, 8 teams, 9 players per team
# ─────────────────────────────────────────────────────────────────


def _make_mlb_pool() -> List[PlayerPoolEntry]:
    """Build an 8-team MLB pool with feasible 10-slot Classic lineups.

    Mirrors the pool used by ``test_sport_config.test_mlb_ilp_*`` so
    this integration test exercises the same realistic shape that the
    unit tests already validate at the ``_ilp_optimize`` level.

    Layout: 4 games × 2 teams × (1 SP + 8 hitters covering every
    defensive position) = 72 players.  Salaries fit comfortably
    under the $50K cap with realistic 10-man builds.
    """

    def _p(pid, name, pos, sal, fp, team, game):
        return PlayerPoolEntry(
            player_id=pid,
            player_name=name,
            display_name=name,
            position=pos,
            eligible_slots=[pos],
            team_abbreviation=team,
            salary=sal,
            projected_fp=fp,
            floor_fp=fp * 0.7,
            ceiling_fp=fp * 1.4,
            projected_minutes=0,
            dk_value=fp / max(sal / 1000, 1),
            estimated_ownership=10.0,
            sim_std=fp * 0.3,
            rotation_confidence=1.0,
            game_id=game,
        )

    games = [
        ("LAD", "SF",  "LADSF"),
        ("NYY", "BOS", "NYYBOS"),
        ("HOU", "TEX", "HOUTEX"),
        ("ATL", "PHI", "ATLPHI"),
    ]

    # Stagger team strength so the solver has a clear primary stack
    # to chase (LAD/NYY) and a strong opposing pitcher to fade (SF).
    team_fp_scale = {
        "LAD": 1.20, "NYY": 1.18, "BOS": 1.05, "HOU": 1.00,
        "TEX": 0.95, "ATL": 1.10, "PHI": 0.95, "SF":  0.85,
    }
    pitcher_fp = {
        "LAD": 22.0, "NYY": 18.0, "BOS": 17.0, "HOU": 16.0,
        "TEX": 15.0, "ATL": 19.0, "PHI": 16.5, "SF":  26.0,
    }

    pool: List[PlayerPoolEntry] = []
    pid = 1
    for home, away, gid in games:
        for team in (home, away):
            scale = team_fp_scale[team]
            hitter_specs = [
                ("C",  3500, 11.0),
                ("1B", 4200, 13.5),
                ("2B", 3800, 12.0),
                ("3B", 4100, 13.0),
                ("SS", 3900, 12.5),
                ("OF", 4500, 14.0),
                ("OF", 4000, 12.5),
                ("OF", 3700, 11.5),
            ]
            for h_pos, sal, fp in hitter_specs:
                pool.append(
                    _p(pid, f"{team}-{h_pos}{pid}", h_pos,
                       sal, fp * scale, team, gid)
                )
                pid += 1
            # 1 starting pitcher per team
            pool.append(
                _p(pid, f"{team}-SP", "P",
                   7500, pitcher_fp[team], team, gid)
            )
            pid += 1

    return pool


def _classify_lineup(
    players,
) -> Tuple[List, Dict[str, int]]:
    """Split a lineup's players into pitchers and hitter-counts-per-team.

    Mirrors the helper from ``test_sport_config`` — pitchers are
    excluded from the team-stack cap because DK MLB Classic only
    counts hitters toward the 5-stack rule.
    """
    pitchers = [
        p for p in players
        if (p.position or "").split("/")[0].upper() in ("P", "SP", "RP")
    ]
    hitters_by_team: Dict[str, int] = {}
    for p in players:
        primary = (p.position or "").split("/")[0].upper()
        if primary in ("P", "SP", "RP"):
            continue
        team = (p.team_abbreviation or "").upper()
        hitters_by_team[team] = hitters_by_team.get(team, 0) + 1
    return pitchers, hitters_by_team


# ─────────────────────────────────────────────────────────────────
# Test fixtures
# ─────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _flush_module_caches():
    """Clear the module-level enriched / strategy caches before every test.

    ``generate_lineups`` reuses cached enriched pools across calls
    (keyed by ``platform:draft_group_id:game_date``).  A previous
    test that hit the same key would otherwise serve a stale pool
    here and bypass the mocked ``_enrich_pool`` patch.
    """
    with _enriched_lock:
        _enriched_cache.clear()
    with _strategy_lock:
        _strategy_cache.clear()
    yield
    with _enriched_lock:
        _enriched_cache.clear()
    with _strategy_lock:
        _strategy_cache.clear()


@pytest.fixture
def mlb_optimizer() -> LineupOptimizerService:
    """LineupOptimizerService with all collaborators stubbed to None.

    The integration test replaces ``build_player_pool`` and
    ``_enrich_pool`` directly, so none of the real services are
    touched and a None-arg constructor is safe.
    """
    return LineupOptimizerService(
        dfs_service=None,
        dk_draftables_service=None,
        nba_service=None,
        injury_service=None,
        rotation_engine=None,
    )


# ─────────────────────────────────────────────────────────────────
# Acceptance test — Prompt 7.14
# ─────────────────────────────────────────────────────────────────


def _run_with_timeout(fn, timeout_s: float):
    """Run ``fn`` on a daemon thread and fail if it exceeds ``timeout_s``.

    Pytest's ``@pytest.mark.timeout`` plugin would cover this, but
    threading-based timeout keeps the test self-contained and
    avoids a plugin dependency in CI.
    """
    result: Dict[str, object] = {}

    def _target():
        try:
            result["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 — surface ANY error
            result["error"] = exc

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    thread.join(timeout=timeout_s)

    if thread.is_alive():
        # Daemon thread keeps running — that's fine; we just fail.
        pytest.fail(
            f"MLB lineup generation hung past {timeout_s:.0f}s — "
            f"the K-Best bypass is not terminating. "
            f"Check the [MultiLineup/MLB] direct-ILP logs for "
            f"the consecutive-failure short-circuit."
        )

    if "error" in result:
        raise result["error"]  # type: ignore[misc]
    return result["value"]


@pytest.mark.skipif(not _PULP_AVAILABLE, reason="PuLP / CBC not installed")
def test_mlb_generate_3_lineups_full_pipeline(mlb_optimizer):
    """End-to-end MLB generation must terminate, return 3 valid lineups,
    and respect the DK 5-hitters-per-team cap.

    This exercises the full ``generate_lineups`` pipeline:
      Phase 0  — pool build/enrichment (mocked).
      Phase 1  — overgeneration target sizing.
      Phase 2  — MLB direct-ILP fast path (Prompt 7.14 — bypasses K-Best).
      Phase 2.5 — salary utilization gate.
      Phase 3  — quality floor + scoring.
      Phase 4  — best-N selection with diversity.
    """
    pool = _make_mlb_pool()

    request = MultiLineupRequest(
        platform="dk",
        sport="mlb",
        draft_group_id=999_001,  # high ID — won't collide with file cache
        game_date="2026-05-02",
        num_lineups=3,
        strategy="balanced",
        contest_type="gpp",
        enable_stacking=True,
        salary_floor_pct=0.90,
        max_exposure=1.0,  # disable global stud-tier auto-cap
    )

    # Patch every service entry-point that would otherwise hit the
    # network / disk.  ``_apply_overrides`` and ``_prefetch_correlations``
    # are no-ops on a clean pool, so we leave them live to keep the
    # integration test honest.
    #
    # The GPP per-tier auto-exposure caps (mid-tier 55%, value 50%) are
    # designed for 20+ lineup builds — at n=3 they collapse to "1
    # appearance per mid-tier player", which on a small mock pool drops
    # legitimate lineups during Phase 4a-post.  We zero out both
    # auto-exposure helpers so the test exercises the lineup-generation
    # math, not the exposure-cap math.  Per-player exposure has its own
    # dedicated tests in test_lineup_optimizer.py.
    with patch.object(mlb_optimizer, "build_player_pool", return_value=pool), \
         patch.object(mlb_optimizer, "_enrich_pool", return_value=pool), \
         patch.object(
             LineupOptimizerService, "_compute_auto_exposure_caps",
             staticmethod(lambda *a, **kw: {}),
         ), \
         patch.object(
             LineupOptimizerService, "_compute_auto_min_exposure",
             staticmethod(lambda *a, **kw: {}),
         ), \
         patch(
             "app.services.lineup_optimizer_service."
             "_load_enriched_pool_from_file",
             return_value=None,
         ):
        def _go() -> MultiLineupResponse:
            return mlb_optimizer.generate_lineups(request)

        result = _run_with_timeout(_go, timeout_s=_HARD_TIMEOUT_S)

    # ── Shape ────────────────────────────────────────────────────
    assert isinstance(result, MultiLineupResponse)
    assert result.sport == "mlb"
    assert result.platform == "dk"

    # ── Lineup count ─────────────────────────────────────────────
    assert result.num_generated == 3, (
        f"Expected exactly 3 lineups, got {result.num_generated}. "
        f"Warnings: {result.warnings}"
    )
    assert len(result.lineups) == 3

    # ── Per-lineup validation ────────────────────────────────────
    for i, lineup in enumerate(result.lineups, start=1):
        # 10 slots — DK MLB Classic
        assert len(lineup.players) == 10, (
            f"Lineup {i} has {len(lineup.players)} players "
            f"(expected 10 for MLB DK Classic)"
        )

        # Slot composition: every required slot filled exactly once
        # (with OF appearing 3× and P appearing 2×).
        slots = [p.roster_slot for p in lineup.players]
        slot_counts = {s: slots.count(s) for s in set(slots)}
        assert slot_counts.get("P", 0) == 2, (
            f"Lineup {i} has {slot_counts.get('P', 0)} pitcher(s); "
            f"MLB requires 2"
        )
        assert slot_counts.get("OF", 0) == 3, (
            f"Lineup {i} has {slot_counts.get('OF', 0)} OF; "
            f"MLB requires 3"
        )
        for required_slot in ("C", "1B", "2B", "3B", "SS"):
            assert slot_counts.get(required_slot, 0) == 1, (
                f"Lineup {i} missing exactly-1 {required_slot} slot "
                f"(got {slot_counts.get(required_slot, 0)})"
            )

        # Salary cap respected
        assert lineup.total_salary <= 50_000, (
            f"Lineup {i} salary ${lineup.total_salary:,} exceeds "
            f"$50,000 MLB DK cap"
        )

        # No duplicate players within a lineup
        pids = [p.player_id for p in lineup.players]
        assert len(set(pids)) == len(pids), (
            f"Lineup {i} has duplicate players: {pids}"
        )

        # ── DK rule: max 5 hitters from any one team ─────────────
        # Pitchers are exempt — they don't count toward this cap.
        _, hitters_by_team = _classify_lineup(lineup.players)
        max_hitters = max(hitters_by_team.values()) if hitters_by_team else 0
        assert max_hitters <= 5, (
            f"Lineup {i} violates the 5-hitter cap: "
            f"{hitters_by_team} (one team has {max_hitters} hitters)"
        )


@pytest.mark.skipif(not _PULP_AVAILABLE, reason="PuLP / CBC not installed")
def test_mlb_lineups_are_diverse(mlb_optimizer):
    """The 3 generated lineups should not be identical clones.

    The direct-ILP fast path injects per-attempt Gaussian projection
    noise + a "previously used" soft penalty.  With a healthy 8-team
    pool there is no reason all three lineups should land on the
    exact same player set — that would prove the diversity heuristic
    is broken.
    """
    pool = _make_mlb_pool()
    request = MultiLineupRequest(
        platform="dk",
        sport="mlb",
        draft_group_id=999_002,
        game_date="2026-05-02",
        num_lineups=3,
        strategy="balanced",
        contest_type="gpp",
        enable_stacking=True,
        salary_floor_pct=0.90,
        max_exposure=1.0,
    )

    with patch.object(mlb_optimizer, "build_player_pool", return_value=pool), \
         patch.object(mlb_optimizer, "_enrich_pool", return_value=pool), \
         patch.object(
             LineupOptimizerService, "_compute_auto_exposure_caps",
             staticmethod(lambda *a, **kw: {}),
         ), \
         patch.object(
             LineupOptimizerService, "_compute_auto_min_exposure",
             staticmethod(lambda *a, **kw: {}),
         ), \
         patch(
             "app.services.lineup_optimizer_service."
             "_load_enriched_pool_from_file",
             return_value=None,
         ):
        result = _run_with_timeout(
            lambda: mlb_optimizer.generate_lineups(request),
            timeout_s=_HARD_TIMEOUT_S,
        )

    fingerprints = {
        frozenset(p.player_id for p in lu.players)
        for lu in result.lineups
    }
    # 3 lineups should produce at least 2 unique player-sets.  Demanding
    # all 3 unique is too strict for a tiny mock pool — but identical
    # triples (1 fingerprint) would mean the diversity loop is dead.
    assert len(fingerprints) >= 2, (
        f"All {len(result.lineups)} lineups have the identical player "
        f"set — direct-ILP diversity heuristic is broken."
    )
