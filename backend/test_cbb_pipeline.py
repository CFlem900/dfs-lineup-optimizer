import sys
sys.path.insert(0, '.')

# Test 1: Check what CBBpy actually returns for a known team
print("=" * 60)
print("TEST 1: CBBpy raw data inspection")
print("=" * 60)

try:
    from cbbpy import mens_scraper as ms
    import pandas as pd
    
    # Use a well-known team
    team = "Duke Blue Devils"
    season = 2026
    
    result = ms.get_games_team(team, season, info=False, box=True, pbp=False)
    
    if isinstance(result, tuple):
        box_df = None
        for item in result:
            if isinstance(item, pd.DataFrame) and not item.empty:
                box_df = item
                break
    elif isinstance(result, pd.DataFrame):
        box_df = result
    else:
        box_df = None
    
    if box_df is not None:
        print(f"Columns: {list(box_df.columns)}")
        print(f"Total rows: {len(box_df)}")
        print(f"Sample data (first 5 rows):")
        print(box_df.head().to_string())
        print()
        
        # Check unique teams in the data
        if 'team' in box_df.columns:
            unique_teams = box_df['team'].unique()
            print(f"Unique teams in data: {unique_teams}")
            print(f"Number of unique teams: {len(unique_teams)}")
            
            # Filter to Duke only
            duke_df = box_df[box_df['team'].astype(str).str.lower().str.contains('duke', na=False)]
            print(f"\nAfter filtering to Duke: {len(duke_df)} rows (was {len(box_df)})")
            
            # Check for TEAM rows
            team_rows = duke_df[duke_df['player'].astype(str).str.lower() == 'team']
            print(f"TEAM totals rows: {len(team_rows)}")
            
            # Get actual player rows
            player_df = duke_df[~duke_df['player'].astype(str).str.lower().isin(['team', 'nan', ''])]
            unique_players = player_df['player'].unique()
            print(f"Unique Duke players: {len(unique_players)}")
            print(f"Player names: {list(unique_players[:10])}")
            
            # Check minutes for a starter
            if len(unique_players) > 0:
                first_player = unique_players[0]
                player_games = player_df[player_df['player'] == first_player]
                if 'min' in player_df.columns:
                    mins = player_games['min'].tolist()
                    print(f"\n{first_player} minutes per game: {mins[:10]}")
                    print(f"Average minutes: {sum([float(m) for m in mins if pd.notna(m)]) / max(len(mins), 1):.1f}")
                
                # Check 3pm column
                if '3pm' in player_df.columns:
                    threepts = player_games['3pm'].tolist()
                    print(f"{first_player} 3pm per game: {threepts[:10]}")
                elif '3PM' in player_df.columns:
                    print("Column is uppercase '3PM'")
                elif 'FG3M' in player_df.columns:
                    print("Column is 'FG3M'")
                else:
                    print(f"No 3pm column found. Available: {[c for c in player_df.columns if '3' in str(c).lower()]}")
        else:
            print("No 'team' column found!")
            print(f"Available columns: {list(box_df.columns)}")
    else:
        print("No box score data returned")
        
except Exception as e:
    print(f"CBBpy test failed: {e}")
    import traceback
    traceback.print_exc()

# Test 2: Run the actual CBB service pipeline
print("\n" + "=" * 60)
print("TEST 2: CBBApiService.build_team_rotation()")
print("=" * 60)

try:
    from app.services.cbb_api_service import CBBApiService
    
    svc = CBBApiService()
    
    # Try Duke (ESPN ID 150)
    rotation = svc.build_team_rotation(150)
    
    print(f"Players in rotation: {len(rotation)}")
    total_min = 0
    for p in rotation:
        total_min += p.season_avg
        print(f"  {p.player_name:25s} pos={p.position:3s} avg_min={p.season_avg:5.1f} "
              f"pts/min={p.pts_per_min:.3f} reb/min={p.reb_per_min:.3f} "
              f"ast/min={p.ast_per_min:.3f} fg3m/min={p.fg3m_per_min:.3f}")
    
    print(f"\nTotal team minutes: {total_min:.1f} (should be near 200 for CBB)")
    
except Exception as e:
    print(f"CBBApiService test failed: {e}")
    import traceback
    traceback.print_exc()

# Test 3: Check rotation engine normalization
print("\n" + "=" * 60)
print("TEST 3: RotationEngine.project_team_rotation() with sport='cbb'")
print("=" * 60)

try:
    from app.services.rotation_engine import RotationEngine
    from app.models.game import GameInfo, TeamGameStats
    
    engine = RotationEngine()
    
    if rotation:
        # Create a mock GameInfo
        home_team = TeamGameStats(
            team_id=150,
            team_name="Duke Blue Devils",
            team_abbreviation="DUKE",
            wins=20, losses=5,
        )
        away_team = TeamGameStats(
            team_id=2305,
            team_name="North Carolina Tar Heels",
            team_abbreviation="UNC",
            wins=18, losses=7,
        )
        game_info = GameInfo(
            game_id="test123",
            game_date="2026-02-16",
            home_team=home_team,
            away_team=away_team,
            projected_total=145.0,
            projected_home_score=75.0,
            projected_away_score=70.0,
            projected_spread=-5.0,
            projected_pace=68.0,
        )
        
        result = engine.project_team_rotation(
            team_id=150,
            team_name="Duke Blue Devils",
            rotation=rotation,
            injuries=[],
            game_date="2026-02-16",
            game_info=game_info,
            sport="cbb",
        )
        
        print(f"Total projected minutes: {result.total_minutes:.1f}")
        for p in result.projections:
            if p.adjusted_minutes > 0:
                print(f"  {p.player_name:25s} baseline={p.baseline_minutes:5.1f} "
                      f"adjusted={p.adjusted_minutes:5.1f} pace_factor={p.pace_factor:.4f}")
        
        # Check pace factor
        print(f"\nExpected pace_factor for CBB (68/68): {68.0/68.0:.4f}")
        print(f"Wrong pace_factor if using NBA avg (68/101.9): {68.0/101.9:.4f}")
        
except Exception as e:
    print(f"RotationEngine test failed: {e}")
    import traceback
    traceback.print_exc()

# Test 4: Check DFS scoring
print("\n" + "=" * 60)
print("TEST 4: DFS fantasy point projections")
print("=" * 60)

try:
    from app.services.dfs_service import DFSService
    
    dfs = DFSService()
    
    if rotation and result:
        dfs_result = dfs.project_team_dfs(
            result, rotation, sport="cbb"
        )
        
        print(f"Team DK total: {dfs_result.team_dk_total:.1f}")
        for dp in sorted(dfs_result.projections, key=lambda x: x.dk_points, reverse=True):
            if dp.dk_points > 0:
                print(f"  {dp.player_name:25s} min={dp.projected_minutes:5.1f} "
                      f"pts={dp.pts:5.1f} reb={dp.reb:4.1f} ast={dp.ast:4.1f} "
                      f"fg3m={dp.fg3m:4.1f} dk={dp.dk_points:5.1f}")
                      
except Exception as e:
    print(f"DFS test failed: {e}")
    import traceback
    traceback.print_exc()
