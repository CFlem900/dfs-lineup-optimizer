import json, sys
sys.stdout.reconfigure(encoding="utf-8")

for fname in ["cache/pool_085f59867237.json", "cache/pool_7a420f3dda31.json"]:
    try:
        data = json.load(open(fname, encoding="utf-8"))
        pool = data.get("players", [])
        print(f"\n=== {fname}: {len(pool)} entries ===")

        # Find Edwards, Payne, Avdija, SGA
        for entry in pool:
            name = entry.get("display_name") or entry.get("player_name", "?")
            for t in ["edwards", "payne", "avdija", "deni", "shai", "gilgeous"]:
                if t in name.lower():
                    print(f"  FOUND: {name} FP={entry.get('projected_fp')} sal=${entry.get('salary')} min={entry.get('projected_minutes')} src={entry.get('projection_source')}")

        # Count by team
        teams = {}
        for entry in pool:
            t = entry.get("team_abbreviation", "?")
            if t not in teams:
                teams[t] = []
            teams[t].append(entry.get("display_name") or entry.get("player_name", "?"))
        print(f"  Teams: {', '.join(f'{t}({len(ps)})' for t, ps in sorted(teams.items()))}")

        # Show OKC players
        if "OKC" in teams:
            print(f"\n  OKC players ({len(teams['OKC'])}):")
            for entry in pool:
                if entry.get("team_abbreviation") == "OKC":
                    name = entry.get("display_name") or entry.get("player_name", "?")
                    print(f"    {name:25s} FP={entry.get('projected_fp',0):5.1f} min={entry.get('projected_minutes',0):5.1f} sal=${entry.get('salary',0):5d} src={entry.get('projection_source','?')}")
    except Exception as e:
        print(f"{fname}: Error - {e}")
