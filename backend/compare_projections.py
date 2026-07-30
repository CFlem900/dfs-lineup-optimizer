"""Compare app's cached pool projections vs external Data Hub projections."""
import json, csv, os, statistics

# Load app's cached pool (today's Main slate DG=143715)
with open('cache/pool_482c1317850f.json') as f:
    pool_data = json.loads(f.readline())

players = pool_data.get('players', [])
print(f'App pool: {len(players)} players')

# Build lookup by name (lowered)
app_by_name = {}
for p in players:
    name = p.get('player_name', '').strip()
    app_by_name[name.lower()] = p

# Load external Data Hub CSV
ext_players = []
with open(r'C:/Users/CFlem/Downloads/DK_NBA_Main_Data_Hub_Projections (14).csv', 'r') as f:
    reader = csv.DictReader(f)
    for row in reader:
        try:
            proj = float(row['Projection'])
        except:
            proj = 0
        if proj > 0:
            ext_players.append(row)

print(f'External (Data Hub): {len(ext_players)} players with projections > 0')

# Name normalization
name_fixes = {
    'nicolas claxton': 'nic claxton',
    'cameron johnson': 'cam johnson',
}

# Compare: join on name + team
comparisons = []
matched = 0
unmatched_ext = []

for ext in ext_players:
    ext_name = ext['Player'].strip()
    ext_name_lower = ext_name.lower()
    ext_team = ext['Team']
    ext_proj = float(ext['Projection'])
    ext_sal = int(ext['Salary'])

    app = app_by_name.get(ext_name_lower)

    if not app:
        app = app_by_name.get(name_fixes.get(ext_name_lower, ''))

    if not app:
        parts = ext_name_lower.split()
        last_name = parts[-1] if parts else ''
        for k, v in app_by_name.items():
            if last_name in k and v.get('team_abbreviation', '') == ext_team:
                app = v
                break

    if app:
        matched += 1
        app_proj = app.get('projected_fp', 0)
        app_floor = app.get('floor_fp', 0)
        app_ceil = app.get('ceiling_fp', 0)
        app_mins = app.get('projected_minutes', 0)

        def sf(val):
            try: return float(val)
            except: return 0

        ext_floor = sf(ext.get('Floor', 0))
        ext_ceil = sf(ext.get('Ceiling', 0))
        ext_mins = sf(ext.get('Minutes', 0))

        diff = app_proj - ext_proj
        pct_diff = (diff / ext_proj * 100) if ext_proj else 0

        comparisons.append({
            'name': ext_name,
            'team': ext_team,
            'salary': ext_sal,
            'ext_proj': ext_proj,
            'app_proj': round(app_proj, 2),
            'diff': round(diff, 2),
            'pct_diff': round(pct_diff, 1),
            'ext_floor': ext_floor,
            'app_floor': round(app_floor, 2),
            'ext_ceil': ext_ceil,
            'app_ceil': round(app_ceil, 2),
            'ext_mins': ext_mins,
            'app_mins': round(app_mins, 1),
        })
    else:
        unmatched_ext.append((ext_name, ext_team, ext_proj))

print(f'Matched: {matched}, Unmatched: {len(unmatched_ext)}')

# ── TOP 40 COMPARISON ───────────────────────────────
SEP = '=' * 130
DASH = '-' * 130
print(f'\n{SEP}')
print(f'TOP 40 PLAYERS — PROJECTION COMPARISON (App vs Data Hub)')
print(f'{SEP}')
comparisons.sort(key=lambda x: x['ext_proj'], reverse=True)
hdr = f"{'Player':<26} {'Team':<4} {'Sal':>6} | {'App':>7} {'Ext':>7} {'Diff':>7} {'Diff%':>6} | {'A_Flr':>6} {'E_Flr':>6} | {'A_Ceil':>6} {'E_Ceil':>6} | {'A_Min':>5} {'E_Min':>5}"
print(hdr)
print(DASH)
for c in comparisons[:40]:
    marker = ''
    if abs(c['pct_diff']) > 15: marker = ' ***'
    elif abs(c['pct_diff']) > 10: marker = ' **'
    elif abs(c['pct_diff']) > 5: marker = ' *'
    line = (
        f"{c['name']:<26} {c['team']:<4} {c['salary']:>6} | "
        f"{c['app_proj']:>7.1f} {c['ext_proj']:>7.1f} {c['diff']:>+7.1f} {c['pct_diff']:>+5.1f}% | "
        f"{c['app_floor']:>6.1f} {c['ext_floor']:>6.1f} | "
        f"{c['app_ceil']:>6.1f} {c['ext_ceil']:>6.1f} | "
        f"{c['app_mins']:>5.1f} {c['ext_mins']:>5.1f}{marker}"
    )
    print(line)

# ── BIGGEST POSITIVE DIVERGENCES ────────────────────
print(f'\n{SEP}')
print(f'BIGGEST POSITIVE DIVERGENCES (App Proj HIGHER Than Data Hub)')
print(f'{SEP}')
by_diff = sorted([c for c in comparisons if c['ext_proj'] > 15], key=lambda x: x['diff'], reverse=True)
hdr2 = f"{'Player':<26} {'Team':<4} {'Sal':>6} | {'App':>7} {'Ext':>7} {'Diff':>7} {'Diff%':>6} | {'A_Min':>5} {'E_Min':>5}"
print(hdr2)
print(DASH)
for c in by_diff[:15]:
    mdiff = c['app_mins'] - c['ext_mins']
    print(f"{c['name']:<26} {c['team']:<4} {c['salary']:>6} | {c['app_proj']:>7.1f} {c['ext_proj']:>7.1f} {c['diff']:>+7.1f} {c['pct_diff']:>+5.1f}% | {c['app_mins']:>5.1f} {c['ext_mins']:>5.1f}")

# ── BIGGEST NEGATIVE DIVERGENCES ────────────────────
print(f'\n{SEP}')
print(f'BIGGEST NEGATIVE DIVERGENCES (App Proj LOWER Than Data Hub)')
print(f'{SEP}')
by_diff_neg = sorted([c for c in comparisons if c['ext_proj'] > 15], key=lambda x: x['diff'])
print(hdr2)
print(DASH)
for c in by_diff_neg[:15]:
    mdiff = c['app_mins'] - c['ext_mins']
    print(f"{c['name']:<26} {c['team']:<4} {c['salary']:>6} | {c['app_proj']:>7.1f} {c['ext_proj']:>7.1f} {c['diff']:>+7.1f} {c['pct_diff']:>+5.1f}% | {c['app_mins']:>5.1f} {c['ext_mins']:>5.1f}")

# ── MINUTES DIVERGENCES ─────────────────────────────
print(f'\n{SEP}')
print(f'BIGGEST MINUTES DIVERGENCES (where gap > 2 min, proj > 15)')
print(f'{SEP}')
mins_diff = sorted(
    [c for c in comparisons if c['ext_proj'] > 15 and abs(c['app_mins'] - c['ext_mins']) > 2],
    key=lambda x: abs(x['app_mins'] - x['ext_mins']), reverse=True
)
print(f"{'Player':<26} {'Team':<4} | {'A_Min':>7} {'E_Min':>7} {'MinDiff':>8} | {'App_Proj':>8} {'Ext_Proj':>8}")
print(DASH)
for c in mins_diff[:15]:
    mdiff = c['app_mins'] - c['ext_mins']
    print(f"{c['name']:<26} {c['team']:<4} | {c['app_mins']:>7.1f} {c['ext_mins']:>7.1f} {mdiff:>+8.1f} | {c['app_proj']:>8.1f} {c['ext_proj']:>8.1f}")

# ── AGGREGATE STATS ─────────────────────────────────
diffs_pct = [c['pct_diff'] for c in comparisons if c['ext_proj'] > 15]
diffs_abs = [c['diff'] for c in comparisons if c['ext_proj'] > 15]
print(f'\n{SEP}')
print(f'AGGREGATE COMPARISON STATS (players with proj > 15 FP)')
print(f'{SEP}')
n = len(diffs_pct)
print(f'Players compared:    {n}')
print(f'Mean difference:     {statistics.mean(diffs_abs):>+6.2f} FP ({statistics.mean(diffs_pct):>+5.1f}%)')
print(f'Median difference:   {statistics.median(diffs_abs):>+6.2f} FP ({statistics.median(diffs_pct):>+5.1f}%)')
print(f'Std dev difference:  {statistics.stdev(diffs_abs):>6.2f} FP ({statistics.stdev(diffs_pct):>5.1f}%)')
agree5 = sum(1 for d in diffs_pct if abs(d) <= 5)
agree10 = sum(1 for d in diffs_pct if abs(d) <= 10)
div15 = sum(1 for d in diffs_pct if abs(d) > 15)
div20 = sum(1 for d in diffs_pct if abs(d) > 20)
print(f'Agree within 5%:     {agree5}/{n} ({agree5/n*100:.0f}%)')
print(f'Agree within 10%:    {agree10}/{n} ({agree10/n*100:.0f}%)')
print(f'Diverge >15%:        {div15}/{n} ({div15/n*100:.0f}%)')
print(f'Diverge >20%:        {div20}/{n} ({div20/n*100:.0f}%)')
higher = sum(1 for d in diffs_abs if d > 0)
lower = sum(1 for d in diffs_abs if d < 0)
print(f'App higher overall:  {higher}/{n} ({higher/n*100:.0f}%)')
print(f'App lower overall:   {lower}/{n} ({lower/n*100:.0f}%)')

# Correlation
if len(comparisons) > 2:
    ext_vals = [c['ext_proj'] for c in comparisons if c['ext_proj'] > 15]
    app_vals = [c['app_proj'] for c in comparisons if c['ext_proj'] > 15]
    mean_ext = statistics.mean(ext_vals)
    mean_app = statistics.mean(app_vals)
    cov = sum((e - mean_ext) * (a - mean_app) for e, a in zip(ext_vals, app_vals)) / (n - 1)
    std_ext = statistics.stdev(ext_vals)
    std_app = statistics.stdev(app_vals)
    corr = cov / (std_ext * std_app) if std_ext and std_app else 0
    print(f'Correlation (r):     {corr:.4f}')

# Unmatched
if unmatched_ext:
    print(f'\nUnmatched external players (top 10 by proj):')
    for name, team, proj in sorted(unmatched_ext, key=lambda x: -x[2])[:10]:
        print(f'  {name} ({team}) - {proj:.1f} proj')
