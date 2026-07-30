"""Analyze external DraftKings NBA lineups CSV for exposure, core players, and team distribution."""

import csv
import re
import sys
import io
from collections import Counter, defaultdict

# Force UTF-8 output on Windows
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

CSV_PATH = r"C:\Users\CFlem\Downloads\DK_NBA_Night_Pre_Contest_Sims_Lineups (9).csv"

def parse_player(cell: str):
    """Parse 'Name (dk_player_id)' -> (name, dk_id)"""
    cell = cell.strip()
    m = re.match(r'^(.+?)\s*\((\d+)\)$', cell)
    if m:
        return m.group(1).strip(), int(m.group(2))
    return cell, None

def main():
    lineups = []
    with open(CSV_PATH, 'r', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        for row in reader:
            lineup = []
            for pos in ['PG', 'SG', 'SF', 'PF', 'C', 'G', 'F', 'UTIL']:
                name, dk_id = parse_player(row[pos])
                lineup.append({'name': name, 'dk_id': dk_id, 'slot': pos})
            lineups.append(lineup)

    total = len(lineups)
    print(f"{'=' * 70}")
    print(f"  EXTERNAL LINEUP ANALYSIS -- {total} lineups")
    print(f"{'=' * 70}")

    # --- Player exposure ---
    player_count = Counter()
    player_names = {}
    player_slots = defaultdict(Counter)

    for lu in lineups:
        for p in lu:
            dk_id = p['dk_id']
            player_count[dk_id] += 1
            player_names[dk_id] = p['name']
            player_slots[dk_id][p['slot']] += 1

    unique_players = len(player_count)
    unique_ids = sorted(player_count.keys())

    print(f"\n  Total unique players: {unique_players}")
    print(f"  Total lineups: {total}")

    # --- Top 30 by exposure ---
    ranked = player_count.most_common(30)
    print(f"\n{'-' * 70}")
    print(f"  TOP 30 PLAYERS BY EXPOSURE")
    print(f"{'-' * 70}")
    print(f"  {'Rank':<5} {'Player':<28} {'Count':>6} {'Exposure':>9}  Slots")
    print(f"  {'----':<5} {'----------------------------':<28} {'-----':>6} {'--------':>9}  {'--------------------'}")
    for i, (dk_id, cnt) in enumerate(ranked, 1):
        pct = cnt / total * 100
        slots = ", ".join(f"{s}:{c}" for s, c in player_slots[dk_id].most_common())
        print(f"  {i:<5} {player_names[dk_id]:<28} {cnt:>6} {pct:>8.1f}%  {slots}")

    # --- Core players (>50%) ---
    core = [(dk_id, cnt) for dk_id, cnt in player_count.most_common() if cnt / total * 100 > 50]
    print(f"\n{'-' * 70}")
    print(f"  CORE PLAYERS (>50% exposure)")
    print(f"{'-' * 70}")
    if core:
        for dk_id, cnt in core:
            pct = cnt / total * 100
            print(f"  {player_names[dk_id]:<30} {cnt:>4}/{total}  ({pct:.1f}%)")
    else:
        print("  (none)")

    # --- Exposure tiers ---
    print(f"\n{'-' * 70}")
    print(f"  EXPOSURE TIER DISTRIBUTION")
    print(f"{'-' * 70}")
    tier_defs = [
        (">66%",  lambda p: p > 66),
        ("50-66%", lambda p: 50 <= p < 66),
        ("33-50%", lambda p: 33 <= p < 50),
        ("20-33%", lambda p: 20 <= p < 33),
        ("10-20%", lambda p: 10 <= p < 20),
        ("<10%",   lambda p: p < 10),
    ]
    for label, test in tier_defs:
        players_in = [dk_id for dk_id, cnt in player_count.items()
                      if test(cnt / total * 100)]
        names = [player_names[dk_id] for dk_id in players_in]
        print(f"  {label:<12}: {len(players_in)} players")
        if players_in and len(players_in) <= 10:
            for n in sorted(names):
                pct = player_count[[k for k,v in player_names.items() if v == n][0]] / total * 100
                print(f"               - {n} ({pct:.1f}%)")

    # --- Positional analysis ---
    print(f"\n{'-' * 70}")
    print(f"  SLOT USAGE (unique players per slot)")
    print(f"{'-' * 70}")
    slot_players = defaultdict(set)
    for lu in lineups:
        for p in lu:
            slot_players[p['slot']].add(p['dk_id'])
    for slot in ['PG', 'SG', 'SF', 'PF', 'C', 'G', 'F', 'UTIL']:
        players_in_slot = slot_players[slot]
        names_sorted = sorted(player_names[pid] for pid in players_in_slot)
        print(f"  {slot:<6}: {len(players_in_slot)} unique  ({', '.join(names_sorted)})")

    # --- Co-occurrence / stacking analysis ---
    print(f"\n{'-' * 70}")
    print(f"  PLAYER PAIR CO-OCCURRENCE (top 20)")
    print(f"{'-' * 70}")
    pair_count = Counter()
    for lu in lineups:
        ids = sorted(set(p['dk_id'] for p in lu))
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                pair_count[(ids[i], ids[j])] += 1

    top_pairs = pair_count.most_common(20)
    print(f"  {'Player A':<24} {'Player B':<24} {'Count':>6} {'Rate':>7}")
    print(f"  {'------------------------':<24} {'------------------------':<24} {'-----':>6} {'------':>7}")
    for (a, b), cnt in top_pairs:
        pct = cnt / total * 100
        print(f"  {player_names[a]:<24} {player_names[b]:<24} {cnt:>6} {pct:>6.1f}%")

    # --- All unique DK IDs ---
    print(f"\n{'-' * 70}")
    print(f"  ALL UNIQUE DK PLAYER IDs ({len(unique_ids)} players)")
    print(f"{'-' * 70}")
    print(f"  {'DK ID':<12} {'Player':<30} {'Exp':>6}")
    print(f"  {'----------':<12} {'----------------------------':<30} {'-----':>6}")
    for dk_id in unique_ids:
        cnt = player_count[dk_id]
        pct = cnt / total * 100
        print(f"  {dk_id:<12} {player_names[dk_id]:<30} {pct:>5.1f}%")

    print(f"\n{'=' * 70}")
    print(f"  ANALYSIS COMPLETE")
    print(f"{'=' * 70}")

if __name__ == '__main__':
    main()
