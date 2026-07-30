"""Compare App v4 Lineups vs DK Pre-Contest Sims."""
import sys, io, csv, re, json
from collections import Counter

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

V3_PATH = r"C:\Working\Prediction_App\backend\v3_lineups.json"
V4_PATH = r"C:\Working\Prediction_App\backend\v4_lineups.json"
CSV_PATH = r"C:\Users\CFlem\Downloads\DK_NBA_Main_Pre_Contest_Sims_Lineups (9).csv"
POSITIONS = ["PG", "SG", "SF", "PF", "C", "G", "F", "UTIL"]


def strip_dk_id(raw):
    return re.sub(r"\s*\(\d+\)", "", raw).strip()


def load_dk_sims(path):
    lineups = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            lineups.append([strip_dk_id(row[pos].strip()) for pos in POSITIONS])
    return lineups


def load_app_lineups(path):
    with open(path) as f:
        data = json.load(f)
    lineups = []
    for lu in data.get("lineups", []):
        names = [p.get("player_name", "?") for p in lu.get("players", [])]
        lineups.append(names)
    return lineups


def get_exposure(lineups):
    total = len(lineups)
    ctr = Counter()
    for lu in lineups:
        for p in lu:
            ctr[p] += 1
    return {p: cnt / total * 100 for p, cnt in ctr.most_common()}, total


def compare(label_a, lineups_a, label_b, lineups_b):
    exp_a, n_a = get_exposure(lineups_a)
    exp_b, n_b = get_exposure(lineups_b)
    all_p = sorted(set(list(exp_a.keys()) + list(exp_b.keys())),
                   key=lambda p: max(exp_a.get(p, 0), exp_b.get(p, 0)), reverse=True)

    print(f"\n  {label_a} ({n_a} LU) vs {label_b} ({n_b} LU)")
    print(f"  Unique: {len(exp_a)} vs {len(exp_b)}")
    print(f"\n  {'Player':<28} {label_a[:6]:>7} {label_b[:6]:>7} {'Delta':>7}")
    print(f"  {'-'*55}")

    close = 0
    total_delta = 0
    for p in all_p:
        a = exp_a.get(p, 0)
        b = exp_b.get(p, 0)
        d = a - b
        total_delta += abs(d)
        if abs(d) < 10:
            close += 1
        print(f"  {p:<28} {a:>6.1f}% {b:>6.1f}% {d:>+6.1f}%")

    avg_d = total_delta / len(all_p) if all_p else 0
    print(f"\n  Close (<10%): {close}/{len(all_p)} ({close/len(all_p)*100:.0f}%)")
    print(f"  Avg |delta|: {avg_d:.1f}%")

    # Top-10 overlap
    top_a = [p for p, _ in sorted(exp_a.items(), key=lambda x: -x[1])[:10]]
    top_b = [p for p, _ in sorted(exp_b.items(), key=lambda x: -x[1])[:10]]
    overlap = len(set(top_a) & set(top_b))
    print(f"  Top-10 overlap: {overlap}/10")
    return avg_d, close, len(all_p)


def main():
    dk = load_dk_sims(CSV_PATH)
    v3 = load_app_lineups(V3_PATH)
    v4 = load_app_lineups(V4_PATH)

    print("=" * 70)
    print("  COMPARISON: v3 vs v4 vs DK Sims")
    print(f"  DK Sims: {len(dk)} | v3: {len(v3)} | v4: {len(v4)}")
    print("=" * 70)

    print("\n" + "=" * 70)
    print("  v4 (after server restart + dk_fppg fix) vs DK Sims")
    print("=" * 70)
    compare("v4", v4, "DK", dk)

    print("\n" + "=" * 70)
    print("  v3 (before dk_fppg fix) vs DK Sims [reference]")
    print("=" * 70)
    compare("v3", v3, "DK", dk)

    print("\n" + "=" * 70)
    print("  v4 vs v3 (what changed?)")
    print("=" * 70)
    compare("v4", v4, "v3", v3)


if __name__ == "__main__":
    main()
