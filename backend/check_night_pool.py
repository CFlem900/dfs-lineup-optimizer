import json, sys
sys.stdout.reconfigure(encoding="utf-8")

# Check the pool cache for the night slate
for fname in ["cache/pool_085f59867237.json", "cache/pool_7a420f3dda31.json",
              "cache/pool_a4598b84991b.json"]:
    try:
        data = json.load(open(fname, encoding="utf-8"))
        pool = data.get("players", [])
        teams = set()
        for e in pool:
            teams.add(e.get("team_abbreviation", "?"))
        teams_str = ", ".join(sorted(teams))
        print(f"\n=== {fname}: {len(pool)} entries ({teams_str}) ===")

        # Only inspect the night slate pool (ATL, MIL, LAC, IND)
        if "ATL" in teams and "MIL" in teams:
            # Check for Dieng
            for entry in pool:
                name = entry.get("display_name") or entry.get("player_name", "?")
                for target in ["dieng", "ousmane", "cam thomas", "cameron thomas"]:
                    if target in name.lower():
                        print(f"  FOUND: {name} FP={entry.get('projected_fp')} sal=${entry.get('salary')} min={entry.get('projected_minutes')} src={entry.get('projection_source')}")

            # Show MIL players
            print(f"\n  MIL players:")
            for entry in pool:
                if entry.get("team_abbreviation") == "MIL":
                    name = entry.get("display_name") or entry.get("player_name", "?")
                    print(f"    {name:30s} FP={entry.get('projected_fp',0):5.1f} min={entry.get('projected_minutes',0):5.1f} sal=${entry.get('salary',0):5d} src={entry.get('projection_source','?')}")

            # Show IND players
            print(f"\n  IND players:")
            for entry in pool:
                if entry.get("team_abbreviation") == "IND":
                    name = entry.get("display_name") or entry.get("player_name", "?")
                    print(f"    {name:30s} FP={entry.get('projected_fp',0):5.1f} min={entry.get('projected_minutes',0):5.1f} sal=${entry.get('salary',0):5d} src={entry.get('projection_source','?')}")
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"{fname}: Error - {e}")
