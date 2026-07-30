"""Quick check of key player projections in pool."""
import sys
sys.stdout.reconfigure(encoding="utf-8")
import logging
logging.basicConfig(level=logging.WARNING, stream=sys.stderr)

from app.api.dependencies import get_services
from app.services.dk_draftables_service import _normalize_name

svc = get_services()
los = svc.lineup_optimizer_service

pool = los.build_player_pool(
    platform="dk", draft_group_id=143382,
    game_date="2026-03-07", sport="nba"
)

targets = ["jimmy butler", "gary trent", "oscar tshiebwe", "jahmai mashack"]
for p in pool:
    norm = _normalize_name(p.player_name)
    if any(t in norm for t in targets):
        src = getattr(p, "projection_source", "?")
        print(
            f"{p.player_name:25s} {p.team_abbreviation:4s} "
            f"${p.salary:>6,}  fp={p.projected_fp:>6.1f}  "
            f"mins={p.projected_minutes:>5.1f}  "
            f"conf={p.rotation_confidence:.1f}  src={src}"
        )
