"""CBB (NCAA Men's Basketball) sport configuration.

Values mirror the existing module-level constants in
``app.services.lineup_optimizer_service`` and ``app.services.dk_slate_service``.
"""

from __future__ import annotations

from app.sports.base import SportConfig


CBB_CONFIG: SportConfig = SportConfig(
    code="cbb",
    display_name="NCAA",
    # Source: app/services/dk_slate_service.py:34
    dk_lobby_url="https://www.draftkings.com/lobby/getcontests?sport=CBB",
    # Source: app/services/lineup_optimizer_service.py:704
    dk_roster_slots=["G", "G", "G", "F", "F", "F", "UTIL", "UTIL"],
    # Source: app/services/lineup_optimizer_service.py:724-728
    dk_slot_eligibility={
        "G": ["PG", "SG", "G"],
        "F": ["SF", "PF", "C", "F"],
        "UTIL": ["PG", "SG", "SF", "PF", "C", "G", "F"],
    },
    # Source: app/services/lineup_optimizer_service.py:700 (CBB shares NBA's DK cap)
    salary_cap_dk=50_000,
    # Source: app/services/lineup_optimizer_service.py:747
    dk_slot_order=["F", "F", "F", "G", "G", "G", "UTIL", "UTIL"],
    # Source: app/services/lineup_optimizer_service.py:1628 — DK has labelled both 70 and 98 as Classic
    dk_classic_game_type_ids=(70, 98),
    # Source: app/services/lineup_optimizer_service.py:5167, 7880 — 40-min games + small OT
    max_player_minutes=45.0,
    # Source: app/services/lineup_optimizer_service.py:7805
    starter_min_minutes=24.0,
    # Source: app/services/lineup_optimizer_service.py:3103 — cbbpy not thread-safe
    max_team_workers=1,
    # Source: app/services/lineup_optimizer_service.py:5949
    small_slate_team_threshold=6,
    small_slate_min_salary_floor_pct=0.60,
    is_active=True,
)
