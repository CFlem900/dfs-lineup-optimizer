"""MLB sport configuration.

DraftKings MLB Classic format (verified against
https://www.draftkings.com/help/rules/mlb):

  Roster: P, P, C, 1B, 2B, 3B, SS, OF, OF, OF  (10 slots)
  Salary cap: $50,000

Scoring is **polymorphic** — pitchers and hitters score on completely
disjoint stat lines, and DK rules state that a player listed at the P
slot is scored only on pitcher stats (Shohei Ohtani's batting line does
not contribute when he's the pitcher entry). This is the canonical
multi-class scoring case in DFS — see ``SportConfig.scoring_map`` and
``SportConfig.pos_to_class`` for the registry hooks.

Hitter scoring (DK Classic):
  Single (1B):      +3       Run (R):                +2
  Double (2B):      +5       Run Batted In (RBI):    +2
  Triple (3B):      +8       Walk (BB):              +2
  Home Run (HR):    +10      Hit By Pitch (HBP):     +2
  Stolen Base (SB): +5

Pitcher scoring (DK Classic):
  Inning Pitched (IP):           +2.25
  Strikeout (K):                 +2
  Win (W):                       +4
  Earned Run Allowed (ER):       -2
  Hit Allowed (H):               -0.6
  Walk Allowed (BB):             -0.6
  Hit Batsman (HBP allowed):     -0.6
  Complete Game (CG):            +2.5
  Complete Game Shutout (CGSO):  +2.5
  No Hitter (NH):                +5
"""

from __future__ import annotations

from app.sports.base import SportConfig


MLB_CONFIG: SportConfig = SportConfig(
    code="mlb",
    display_name="MLB",
    dk_lobby_url="https://www.draftkings.com/lobby/getcontests?sport=MLB",
    # 10-slot Classic roster
    dk_roster_slots=["P", "P", "C", "1B", "2B", "3B", "SS", "OF", "OF", "OF"],
    # Each defensive slot accepts only that position. OF is generic — DK
    # records it as "OF" without distinguishing LF/CF/RF, but if a future
    # data source provides them, they all map to "OF" for slot-fill.
    # P accepts both starting (SP) and relief (RP) tags from some feeds.
    dk_slot_eligibility={
        "P":  ["P", "SP", "RP"],
        "C":  ["C"],
        "1B": ["1B"],
        "2B": ["2B"],
        "3B": ["3B"],
        "SS": ["SS"],
        "OF": ["OF", "LF", "CF", "RF"],
    },
    salary_cap_dk=50_000,
    # Process most-constrained → least-constrained: P first (only 2 of 10
    # slots and a unique position), then defensive specialists, OF last
    # because it has the most eligible candidates.
    dk_slot_order=["P", "P", "C", "SS", "1B", "2B", "3B", "OF", "OF", "OF"],
    # MLB Classic gameTypeId verified against the live DK lobby on
    # 2026-05-03: gameTypeId=2 with 13 Classic DraftGroups present
    # for the day's slate. The placeholder value of 1 (which the
    # original Prompt 2.2 spec used) was wrong — DK uses gameTypeId=1
    # for NFL Classic, NOT MLB. Filtering by 1 returned 0 MLB DGs and
    # broke the slate-page Player Pool (Prompt 7.10 diagnostic).
    dk_classic_game_type_ids=(2,),
    # MLB doesn't use minutes — bench these to harmless defaults so any
    # cross-sport code that reads them won't crash. The lineup builder's
    # minute-cap paths only fire when ``projected_minutes > 0``, which
    # MLB pool entries won't set.
    max_player_minutes=9.0,         # nominal "innings" cap if anyone sets it
    starter_min_minutes=0.0,
    # MLB has 30 teams and a typical Classic slate spans 10-15 games — no
    # small-slate concern in the way CBB has it.
    small_slate_team_threshold=0,
    small_slate_min_salary_floor_pct=0.60,
    max_team_workers=2,
    # DK rule: max 5 hitters from any one team (the "5-stack"). Pitchers
    # don't count toward this — and there are only 2 P slots anyway.
    max_same_team_count=5,
    team_stack_cap_class="hitter",
    # ── Polymorphic scoring ──────────────────────────────────────────
    scoring_map={
        # Hitter coefficients (per-event)
        "hitter": {
            "s":   3.0,    # single
            "d":   5.0,    # double
            "t":   8.0,    # triple
            "hr": 10.0,    # home run
            "rbi": 2.0,
            "r":   2.0,    # run
            "bb":  2.0,    # walk
            "hbp": 2.0,    # hit by pitch
            "sb":  5.0,    # stolen base
        },
        # Pitcher coefficients (per-event)
        "pitcher": {
            "ip":   2.25,  # inning pitched (DK awards 2.25 per full IP)
            "k":    2.0,   # strikeout
            "w":    4.0,   # win
            "er":  -2.0,   # earned run allowed
            "h":   -0.6,   # hit allowed
            "bb":  -0.6,   # walk allowed
            "hbp": -0.6,   # hit batsman
            "cg":   2.5,   # complete game
            "cgso": 2.5,   # complete game shutout
            "no":   5.0,   # no hitter
        },
    },
    # Position → class lookup. Covers both the standard DK-spelt positions
    # and common feed variants (SP/RP for pitchers, LF/CF/RF for outfield,
    # DH for designated hitter). Unknown positions fall through and the
    # scorer treats them as hitters by default — but this map is exhaustive
    # for the positions actually used on DK.
    pos_to_class={
        # Pitchers — strict per DK rules; an Ohtani-style two-way listing
        # at "P" is scored ONLY on pitcher stats.
        "P":  "pitcher",
        "SP": "pitcher",
        "RP": "pitcher",
        # Hitters
        "C":  "hitter",
        "1B": "hitter",
        "2B": "hitter",
        "3B": "hitter",
        "SS": "hitter",
        "OF": "hitter",
        "LF": "hitter",
        "CF": "hitter",
        "RF": "hitter",
        "DH": "hitter",
    },
    # Flat dk_scoring left empty on purpose — MLB scoring is class-based.
    # ── Stacking rules (added in Prompt 4.1) ────────────────────────
    # Consumed by ``_ilp_optimize`` / ``_build_kbest_prob`` when
    # ``enable_stacking`` is True.
    #
    #   primary_stack_size   : target hitter count for the primary stack
    #       team. The optimizer enforces ≥ this many hitters from at
    #       least one team (with auto-relaxation 5 → 4 → 3 if the player
    #       pool can't satisfy it under the salary cap and pitcher fade).
    #   secondary_stack_size : target hitter count for the second team.
    #       Soft (best-effort): the model adds an objective bonus that
    #       biases the solver toward 5+3 / 4+3 distributions but does
    #       not hard-fail when the pool can't reach it.
    #   fade_opposing_hitters : when True, never select a hitter who is
    #       playing against a pitcher already in the lineup. Encoded as
    #       ``sum(opp_hitter_vars) <= 8 * (1 - pitcher_var)``.
    stack_rules={
        "primary_stack_size": 5,
        "secondary_stack_size": 3,
        "fade_opposing_hitters": True,
    },
    is_active=True,
)
