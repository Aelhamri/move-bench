"""
RQ1-FINAL — Aggregate JSON outputs into cross-comparison matrix
=================================================================
Lit tous les fichiers `bench5_<condition>_<dataset>_summary.json` dans les
2 dossiers `/home/osboxes/bench_results/{stib,ais}/rq1/` et produit :

  1. Tableau text (printé) : matrice 6 × 2 avec median ms/frame + IQR + FPS
  2. CSV `cross_comparison_matrix.csv`
  3. Bootstrap CI95 sur les ratios MOVE_upstream → autres approches

Condition labels (exactement le pattern utilisé par bench5_master + scripts existants) :
  c1_ali_naive    -> Ali naïve (PyMEOS OO)
  c2_ali_optim    -> Ali optim (bench_ali_max_optim L3b_dp_se result)
  c3_move_fast    -> MOVE Fast Preview (MoveTrajectoryItem)
  c4_columnar     -> Columnar (NumPy precompute)
  c5_move_upstream-> MOVE upstream (postgres + expression)
  c6_move_upgrade -> MOVE upgrade (memory + expression)

NOTE: bench_ali_max_optim.py ne produit PAS de JSON par défaut. On lit ses
résultats via le print log (ou on l'adapte). Pour C2, fallback sur le
fichier `bench5_c2_ali_optim_*.json` si présent, sinon WARNING.

Usage (hors QGIS, en Python natif):
    python3 /home/osboxes/rq1/bench5_aggregate.py

Author: Ayoub El Hamri
"""

import json
import statistics
import csv
import random
import sys
from pathlib import Path

# FIX bug #14 : reproducible bootstrap
random.seed(42)
N_BOOT = 1000

DATASETS = ['stib', 'ais']
CONDITIONS = [
    ('c1_ali_naive',     'Ali naïve'),
    ('c2_ali_optim',     'Ali optim'),
    ('c3_move_fast',     'MOVE Fast Preview'),
    ('c4_columnar',      'Columnar'),
    ('c5_move_upstream', 'MOVE upstream'),
    ('c6_move_upgrade',  'MOVE upgrade'),
]

import os as _os
OUTPUT_DIR = Path(__file__).resolve().parent


def load_summary(dataset, condition_id):
    """Try to load JSON summary for (dataset, condition). Return dict or None.

    FIX bug #2 (review) : naming patterns aligned with what each bench
    actually produces. C4/C5/C6 use `<LABEL>_limit<N>_summary.json`.
    """
    base_dir = Path(_os.path.expanduser(f'~/bench_results/{dataset}/rq1'))
    paths_to_try = [base_dir / f'bench5_{condition_id}_{dataset}_summary.json']

    # Fallback patterns for legacy bench scripts
    cid_to_glob = {
        'c4_columnar':      'columnar_limit*_summary.json',
        'c5_move_upstream': 'move_limit*_summary.json',
        'c6_move_upgrade':  'move_inmemory_limit*_summary.json',
    }
    if condition_id in cid_to_glob:
        # Take the most recent matching file
        matches = sorted(base_dir.glob(cid_to_glob[condition_id]),
                         key=lambda p: p.stat().st_mtime if p.exists() else 0,
                         reverse=True)
        paths_to_try.extend(matches)

    for p in paths_to_try:
        if p.exists():
            with open(p) as f:
                data = json.load(f)
            # FIX bug #5 (review) : assert dataset matches
            if data.get('dataset') and data['dataset'] != dataset:
                print(f"  WARNING: {p.name} dataset={data['dataset']} != requested {dataset}, skipping")
                continue
            return data
    return None


def median_of_medians_with_ci(summary, drop_run_1=True):
    """Extract median frame time across runs, with bootstrap CI95."""
    runs = summary.get('runs', [])
    if drop_run_1 and len(runs) > 1:
        runs = runs[1:]
    if not runs:
        return None
    medians = [r['median_frame_ms'] for r in runs]
    median = statistics.median(medians)

    # Bootstrap CI95 (1000 samples). NOTE: with N_RUNS=5 (4 kept), CI95 is wide;
    # for tighter CI, increase BENCH_N_RUNS in bench config.
    if len(medians) >= 2:
        bootstraps = []
        for _ in range(N_BOOT):
            sample = [random.choice(medians) for _ in medians]
            bootstraps.append(statistics.median(sample))
        bootstraps.sort()
        ci_lo = bootstraps[int(0.025 * N_BOOT)]
        ci_hi = bootstraps[int(0.975 * N_BOOT)]
    else:
        ci_lo = ci_hi = median

    return {
        'median_ms': median,
        'ci95_lo': ci_lo,
        'ci95_hi': ci_hi,
        'iqr_ms': max(medians) - min(medians) if len(medians) > 1 else 0,
        'min_ms': min(medians),
        'max_ms': max(medians),
        'fps': 1000.0 / median if median > 0 else 0,
        'n_runs_kept': len(runs),
    }


def load_metrics(dataset, condition_id):
    """Load CPU/RAM metrics dumped by bench5_master."""
    p = Path(_os.path.expanduser(f'~/bench_results/{dataset}/rq1/metrics/{condition_id}_metrics.json'))
    if p.exists():
        with open(p) as f:
            return json.load(f)
    return None


# -----------------------------------------------------------------------------
# Build matrix
# -----------------------------------------------------------------------------
print("=" * 100)
print(f"BENCH5 CROSS-DATASET MATRIX (drop run 1, median + bootstrap CI95)")
print("=" * 100)

matrix = {}  # matrix[(condition_id, dataset)] = {median_ms, ci95_lo, ci95_hi, ...}
metrics_matrix = {}  # cpu/ram metrics per cell
missing = []

for cid, _ in CONDITIONS:
    for dataset in DATASETS:
        summary = load_summary(dataset, cid)
        if summary is None:
            missing.append((cid, dataset))
            matrix[(cid, dataset)] = None
        else:
            matrix[(cid, dataset)] = median_of_medians_with_ci(summary)
        metrics_matrix[(cid, dataset)] = load_metrics(dataset, cid)


# -----------------------------------------------------------------------------
# Print table
# -----------------------------------------------------------------------------
print()
print(f"{'Condition':<22} | {'STIB':<35} | {'AIS':<35}")
print("-" * 100)
for cid, label in CONDITIONS:
    cells = []
    for dataset in DATASETS:
        m = matrix[(cid, dataset)]
        if m is None:
            cells.append("MISSING")
        else:
            cells.append(f"{m['median_ms']:7.1f} ms ({m['fps']:6.1f} FPS) "
                         f"[{m['ci95_lo']:.0f}-{m['ci95_hi']:.0f}]")
    print(f"{label:<22} | {cells[0]:<35} | {cells[1]:<35}")
print("-" * 100)

if missing:
    print()
    print(f"WARNING: {len(missing)} cells missing:")
    for cid, ds in missing:
        print(f"  - {cid} on {ds}")


# -----------------------------------------------------------------------------
# CPU + RAM table
# -----------------------------------------------------------------------------
print()
print("=" * 100)
print("CPU + RAM PEAK per condition (measured by bench5_master)")
print("=" * 100)
print(f"{'Condition':<22} | {'STIB CPU%/RAM peak':<35} | {'AIS CPU%/RAM peak':<35}")
print("-" * 100)
for cid, label in CONDITIONS:
    cells = []
    for dataset in DATASETS:
        m = metrics_matrix.get((cid, dataset))
        if m is None:
            cells.append("MISSING")
        else:
            cells.append(f"{m['cpu_percent_avg']:5.0f}% | {m['ram_peak_mb']:6.0f} MB "
                         f"(Δ {m['ram_peak_delta_mb']:+5.0f})")
    print(f"{label:<22} | {cells[0]:<35} | {cells[1]:<35}")
print("-" * 100)
print("Note: RAM cumule entre conditions dans la même session QGIS (ram_peak_delta = pic intra-condition)")


# -----------------------------------------------------------------------------
# Speedup ratios vs MOVE upstream (the headline)
# -----------------------------------------------------------------------------
print()
print("=" * 100)
print("SPEEDUP vs MOVE upstream (C5)")
print("=" * 100)
for dataset in DATASETS:
    base = matrix.get(('c5_move_upstream', dataset))
    if base is None:
        print(f"  {dataset}: MOVE upstream missing, can't compute ratios")
        continue
    print(f"\n  {dataset.upper()} (MOVE upstream baseline = {base['median_ms']:.1f} ms):")
    for cid, label in CONDITIONS:
        if cid == 'c5_move_upstream':
            continue
        m = matrix.get((cid, dataset))
        if m is None:
            continue
        ratio = base['median_ms'] / m['median_ms']
        # Bootstrap CI on ratio
        ratio_ci_lo = base['ci95_lo'] / m['ci95_hi']
        ratio_ci_hi = base['ci95_hi'] / m['ci95_lo']
        marker = "✓" if ratio > 1.5 else " "
        print(f"  {marker} {label:<22} : {ratio:5.2f}x  [CI95: {ratio_ci_lo:.2f}-{ratio_ci_hi:.2f}]")


# -----------------------------------------------------------------------------
# CSV export
# -----------------------------------------------------------------------------
csv_path = OUTPUT_DIR / 'cross_matrix.csv'
with open(csv_path, 'w', newline='') as f:
    w = csv.writer(f)
    w.writerow(['condition_id', 'condition_label', 'dataset', 'median_ms',
                'ci95_lo', 'ci95_hi', 'iqr_ms', 'min_ms', 'max_ms', 'fps', 'n_runs_kept',
                'cpu_percent_avg', 'ram_peak_mb', 'ram_peak_delta_mb', 'wall_seconds'])
    for cid, label in CONDITIONS:
        for dataset in DATASETS:
            m = matrix[(cid, dataset)]
            mt = metrics_matrix.get((cid, dataset))
            if m is None:
                w.writerow([cid, label, dataset, '', '', '', '', '', '', '', '', '', '', '', ''])
            else:
                w.writerow([cid, label, dataset, m['median_ms'],
                            m['ci95_lo'], m['ci95_hi'], m['iqr_ms'],
                            m['min_ms'], m['max_ms'], m['fps'], m['n_runs_kept'],
                            mt['cpu_percent_avg'] if mt else '',
                            mt['ram_peak_mb'] if mt else '',
                            mt['ram_peak_delta_mb'] if mt else '',
                            mt['wall_seconds'] if mt else ''])
print()
print(f"CSV written: {csv_path}")
print()
print("=" * 100)
print("Next: import into matplotlib for the headline chart (chap. Results page 1)")
print("=" * 100)
