import json, sys
sys.stdout.reconfigure(encoding="utf-8")

# Check both pool cache files
for fname in ["cache/pool_085f59867237.json", "cache/pool_7a420f3dda31.json"]:
    try:
        data = json.load(open(fname, encoding="utf-8"))
        if isinstance(data, list):
            pool = data
        elif isinstance(data, dict):
            pool = data.get("pool", data.get("entries", []))
        else:
            print(f"{fname}: unexpected type {type(data)}")
            continue

        print(f"\n=== {fname}: {len(pool)} entries ===")
        # Find Edwards, Payne, Avdija
        for entry in pool:
            name = entry.get("display_name") or entry.get("player_name", "?")
            if any(t.lower() in name.lower() for t in ["edwards", "payne", "avdija", "deni"]):
                print(f"  FOUND: {name} FP={entry.get('projected_fp')} sal=${entry.get('salary')} min={entry.get('projected_minutes')} src={entry.get('projection_source')}")

        # Count by team
        teams = {}
        for entry in pool:
            t = entry.get("team_abbreviation", "?")
            if t not in teams:
                teams[t] = []
            teams[t].append(entry.get("display_name") or entry.get("player_name", "?"))

        print(f"  Teams: {', '.join(f'{t}({len(ps)})' for t, ps in sorted(teams.items()))}")

        # List OKC players
        if "OKC" in teams:
            print(f"\n  OKC players:")
            for entry in pool:
                if entry.get("team_abbreviation") == "OKC":
                    name = entry.get("display_name") or entry.get("player_name", "?")
                    print(f"    {name:25s} FP={entry.get('projected_fp',0):5.1f} min={entry.get('projected_minutes',0):5.1f} sal=${entry.get('salary',0):5d} src={entry.get('projection_source','?')}")
    except Exception as e:
        print(f"{fname}: Error - {e}")
