"""Test FPPG matching in fallback path."""
import asyncio
from app.services.dk_available_players_service import DKAvailablePlayersService
from app.services.dk_draftables_service import DKDraftablesService, _normalize_name


async def test():
    dk_svc = DKDraftablesService()
    ap_svc = DKAvailablePlayersService()

    players = dk_svc.get_draftables(143272)
    print(f"Draftables: {len(players)}")

    fppg_lookup = ap_svc.build_fppg_lookup(143272)
    print(f"FPPG lookup: {len(fppg_lookup)} entries")

    # Test ALL teams
    teams = set()
    for p in players:
        t = (p.team_abbreviation or "").upper()
        if t:
            teams.add(t)

    total_fppg = 0
    total_sal = 0

    for team in sorted(teams):
        team_players = [p for p in players if (p.team_abbreviation or "").upper() == team]
        sorted_dk = sorted(team_players, key=lambda p: p.salary, reverse=True)
        seen = set()
        unique_dk = []
        for p in sorted_dk:
            dname = (p.display_name or "").strip()
            if dname and dname not in seen:
                seen.add(dname)
                unique_dk.append(p)

        fppg_ct = 0
        sal_ct = 0
        for rank, dk_p in enumerate(unique_dk):
            status = (getattr(dk_p, "status", "") or "").strip().upper()
            if status in ("O", "OUT", "D", "DOUBTFUL"):
                continue

            norm = _normalize_name(dk_p.display_name)
            key_team = f"{norm}:{team}"
            key_empty = f"{norm}:"
            dk_fppg_val = fppg_lookup.get(key_team) or fppg_lookup.get(key_empty)

            if dk_fppg_val and dk_fppg_val > 0:
                fppg_ct += 1
                total_fppg += 1
            else:
                sal_ct += 1
                total_sal += 1
                print(f"  SAL_EST: {team} {dk_p.display_name:<30s} norm={norm!r}")

        print(f"{team}: {len(unique_dk)} players -> {fppg_ct} FPPG, {sal_ct} salary_estimate")

    print(f"\nTOTAL: {total_fppg} FPPG, {total_sal} salary_estimate")
    print(f"FPPG hit rate: {total_fppg / (total_fppg + total_sal) * 100:.1f}%")


asyncio.run(test())
