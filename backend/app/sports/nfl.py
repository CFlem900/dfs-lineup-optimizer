"""NFL sport configuration.

DraftKings NFL Classic format (verified against
https://www.draftkings.com/help/rules/4):

  Roster: QB, RB, RB, WR, WR, WR, TE, FLEX, DST  (9 players)
  Salary cap: $50,000
  FLEX may be filled by RB, WR, or TE.

Scoring (DK Classic):
  Passing:    0.04/yd, +4/TD, -1/INT, +3 bonus at 300+ pass yards
  Rushing:    0.10/yd, +6/TD, +3 bonus at 100+ rush yards
  Receiving:  +1/rec (full PPR), 0.10/yd, +6/TD, +3 bonus at 100+ rec yards
  Other:      -1 fumble lost, +2 two-point conversion, +6 off. fumble TD
  Defense (DST):
    +1/sack, +2/INT, +2/fum recovery, +2/safety, +6/TD, +2/blocked kick
    Points-allowed tier: 10 / 7 / 4 / 1 / 0 / -1 / -4 (for 0 / 1-6 / 7-13 / 14-20 / 21-27 / 28-34 / 35+ PA)

Bonuses are encoded in ``dk_scoring_bonuses`` so the threshold logic can
be applied at scoring time. The base per-unit coefficients live in
``dk_scoring``. DST points-allowed is intentionally NOT encoded here
yet — it's a tiered table, not a coefficient, and a later prompt will
add a dedicated ``dst_points_allowed_tiers`` field if/when we ship a
projection engine.
"""

from __future__ import annotations

from app.sports.base import SportConfig


NFL_CONFIG: SportConfig = SportConfig(
    code="nfl",
    display_name="NFL",
    dk_lobby_url="https://www.draftkings.com/lobby/getcontests?sport=NFL",
    # Per the prompt: 9-slot Classic roster
    dk_roster_slots=["QB", "RB", "RB", "WR", "WR", "WR", "TE", "FLEX", "DST"],
    # FLEX accepts RB/WR/TE per DK rules. All other slots are 1-to-1
    # except DST which only accepts a defense entity.
    dk_slot_eligibility={
        "QB": ["QB"],
        "RB": ["RB"],
        "WR": ["WR"],
        "TE": ["TE"],
        "FLEX": ["RB", "WR", "TE"],
        "DST": ["DST"],
    },
    # NFL Classic salary cap (verified on DK rules page)
    salary_cap_dk=50_000,
    # Slot processing order: most-constrained → least-constrained.
    # DST and QB have only one eligible position each (1 player slot pool),
    # so they're cheapest to lock in early. FLEX is most permissive — last.
    dk_slot_order=["DST", "QB", "TE", "RB", "RB", "WR", "WR", "WR", "FLEX"],
    # DK NFL Classic gameTypeId — confirmed as 1 for the 2026 season.
    dk_classic_game_type_ids=(1,),
    # NFL doesn't use minutes for projections (snap counts are the unit
    # of work). These two fields stay at sane defaults so any cross-sport
    # code that reads them doesn't trip — the lineup builder won't apply
    # them on NFL pools because no ``projected_minutes`` field is set.
    max_player_minutes=60.0,
    starter_min_minutes=0.0,
    # Skeleton service is single-threaded for now; bumping later when the
    # NFL data service ships in Prompt 1.2.
    max_team_workers=2,
    # NFL doesn't have small-slate problems the way CBB does — even a
    # Showdown card has 2 teams and a Sunday main slate has 26+. Disable.
    small_slate_team_threshold=0,
    small_slate_min_salary_floor_pct=0.60,
    # ── DK Classic scoring coefficients ──────────────────────────────
    dk_scoring={
        # Passing
        "pass_yd": 0.04,
        "pass_td": 4.0,
        "pass_int": -1.0,
        # Rushing
        "rush_yd": 0.1,
        "rush_td": 6.0,
        # Receiving (full-PPR)
        "rec": 1.0,
        "rec_yd": 0.1,
        "rec_td": 6.0,
        # Other offensive
        "fum_lost": -1.0,
        "two_pt": 2.0,
        "off_fum_td": 6.0,
        # Defense (per-event coefficients — points-allowed is tiered, not here)
        "dst_sack": 1.0,
        "dst_int": 2.0,
        "dst_fum_rec": 2.0,
        "dst_safety": 2.0,
        "dst_td": 6.0,
        "dst_blk_kick": 2.0,
    },
    # Threshold bonuses — fire when the stat exceeds the threshold.
    dk_scoring_bonuses=[
        {"name": "pass_300y", "stat": "pass_yd", "threshold": 300.0, "bonus": 3.0},
        {"name": "rush_100y", "stat": "rush_yd", "threshold": 100.0, "bonus": 3.0},
        {"name": "rec_100y",  "stat": "rec_yd",  "threshold": 100.0, "bonus": 3.0},
    ],
    # ── Stacking rules (added in Prompt 1.6) ─────────────────────────
    # Consumed by ``_ilp_optimize`` when ``enable_stacking`` is True.
    #   qb_min_pass_catchers : when a QB is selected, lineup MUST include
    #       at least this many WR/TE from the QB's team.
    #   qb_max_pass_catchers : ceiling on same-team pass-catchers paired
    #       with the QB; a Big-M conditional keeps the cap inactive when
    #       the QB isn't selected.
    #   require_bring_back   : when True, lineup must include at least one
    #       WR/RB/TE from the QB's opponent in the same game.
    stack_rules={
        "qb_min_pass_catchers": 1,
        "qb_max_pass_catchers": 2,
        "require_bring_back": True,
    },
    # Active so get_config('nfl') resolves. The skeleton ServiceContainer
    # entry comes in Prompt 1.2; until then NFL endpoints will route
    # correctly through the registry but data services are still NBA.
    is_active=True,
)
