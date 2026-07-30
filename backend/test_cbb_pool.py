"""Quick test script for CBB pool build."""
import sys, time
sys.path.insert(0, 'C:/Working/Prediction_App/backend')

from app.services.lineup_optimizer_service import LineupOptimizerService
from app.services.dk_draftables_service import DKDraftablesService
from app.services.cbb_game_service import CBBGameService
from app.services.cbb_injury_service import CBBInjuryService
from app.services.cbb_api_service import CBBApiService

# Minimal services for CBB pool build
dk_svc = DKDraftablesService()
game_svc = CBBGameService()
inj_svc = CBBInjuryService()
data_svc = CBBApiService()
opt_svc = LineupOptimizerService(dk_draftables_service=dk_svc, cache_service=None)

def on_progress(step, completed, total):
    print(f"  [{completed}/{total}] {step}", flush=True)

print("Building CBB pool for DG 142118...", flush=True)
t0 = time.time()
try:
    pool = opt_svc.build_player_pool(
        platform="dk",
        draft_group_id=142118,
        game_date="2026-02-17",
        sport="cbb",
        on_progress=on_progress,
        data_service=data_svc,
        game_service_override=game_svc,
        injury_service_override=inj_svc,
    )
    elapsed = time.time() - t0
    print(f"Pool size: {len(pool)} players in {elapsed:.1f}s")
    for p in pool[:5]:
        print(f"  {p.player_name} ({p.team}) {p.position} ${p.salary} {p.projected_fp}fp")
except Exception as e:
    print(f"ERROR: {type(e).__name__}: {e}")
    import traceback
    traceback.print_exc()
