"""NBA sport configuration.

Values mirror the existing module-level constants in
``app.services.lineup_optimizer_service`` and ``app.services.dk_slate_service``.
Once the registry is wired through those services (Prompt 0.2), the
constants there can defer to this config.
"""

from __future__ import annotations

from app.sports.base import SportConfig


NBA_CONFIG: SportConfig = SportConfig(
    code="nba",
    display_name="NBA",
    # Source: app/services/dk_slate_service.py:33
    dk_lobby_url="https://www.draftkings.com/lobby/getcontests?sport=NBA",
    # Source: app/services/lineup_optimizer_service.py:703
    dk_roster_slots=["PG", "SG", "SF", "PF", "C", "G", "F", "UTIL"],
    # Source: app/services/lineup_optimizer_service.py:713-722
    dk_slot_eligibility={
        "PG": ["PG"],
        "SG": ["SG"],
        "SF": ["SF"],
        "PF": ["PF"],
        "C": ["C"],
        "G": ["PG", "SG"],
        "F": ["SF", "PF"],
        "UTIL": ["PG", "SG", "SF", "PF", "C"],
    },
    # Source: app/services/lineup_optimizer_service.py:700
    salary_cap_dk=50_000,
    # Source: app/services/lineup_optimizer_service.py:746
    dk_slot_order=["C", "PG", "SG", "SF", "PF", "G", "F", "UTIL"],
    # Source: app/services/lineup_optimizer_service.py:1621 — only ID 70 is Classic for NBA
    dk_classic_game_type_ids=(70,),
    # Source: app/services/lineup_optimizer_service.py:5167, 7880
    max_player_minutes=53.0,
    # Source: app/services/lineup_optimizer_service.py:7805
    starter_min_minutes=28.0,
    # Source: app/services/lineup_optimizer_service.py:1125 / 3103
    max_team_workers=2,
    # Source: app/services/lineup_optimizer_service.py:5949 — disabled for NBA
    small_slate_team_threshold=0,
    small_slate_min_salary_floor_pct=0.60,
    is_active=True,
)
