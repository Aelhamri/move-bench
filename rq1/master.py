"""
RQ1-FINAL — Master orchestrator pour bench cross-comparison 6 conditions × 2 datasets
======================================================================================
Lance séquentiellement les 6 conditions sur le dataset courant (BENCH_DATASET).
Chaque condition est un script bench standalone qui produit son propre JSON.

Usage dans QGIS console (FRESH session):

    import sys, os
    os.environ['BENCH_DATASET'] = 'ais'      # ou 'stib'
    os.environ['BENCH_N_TRIPS']  = '5000'
    os.environ['BENCH_N_FRAMES'] = '60'
    os.environ['BENCH_N_RUNS']   = '5'
    if 'bench_config' in sys.modules:
        del sys.modules['bench_config']
    exec(open('/home/osboxes/rq1/bench5_master.py').read())

À la fin : 6 fichiers JSON dans /home/osboxes/bench_results/<dataset>/rq1/
Lance ensuite bench5_aggregate.py pour produire la matrice 6×2.

NOTE: pour le 2ème dataset, redémarre QGIS et change BENCH_DATASET.

Author: Ayoub El Hamri
"""

import sys, os, gc, time
from pathlib import Path

THIS_DIR = Path('/home/osboxes/rq1')
sys.path.insert(0, str(THIS_DIR))
from bench5_metrics import RunMonitor

# CRITICAL FIX (review bug #1) : C1/C3 lisent BENCH_N_TRIPS, C4/C5/C6 lisent BENCH_LIMIT.
# Synchroniser pour que TOUTES les conditions tournent sur la même taille de subset.
if 'BENCH_N_TRIPS' in os.environ and 'BENCH_LIMIT' not in os.environ:
    os.environ['BENCH_LIMIT'] = os.environ['BENCH_N_TRIPS']
# Et l'inverse
if 'BENCH_LIMIT' in os.environ and 'BENCH_N_TRIPS' not in os.environ:
    os.environ['BENCH_N_TRIPS'] = os.environ['BENCH_LIMIT']

# Force reload bench_config to pick up correct dataset
for mod_name in list(sys.modules):
    if mod_name.startswith('bench_config'):
        del sys.modules[mod_name]

CONDITIONS = [
    ('c1_ali_naive',     'bench5_c1_ali_naive.py',  'Ali naïve géométrique (PyMEOS OO loop)'),
    ('c2_ali_optim',     'bench5_c2_ali_optim.py',  'Ali optimized par Ayoub (raw EWKB + provider direct)'),
    ('c3_move_fast',     'bench5_c3_fast.py',       'MOVE Fast Preview (MoveTrajectoryItem)'),
    ('c4_columnar',      'columnar.py',             'Columnar Ayoub (NumPy precompute)'),
    ('c5_move_upstream', 'move.py',                 'MOVE upstream (postgres + line_interpolate_point)'),
    ('c6_move_upgrade',  'move_inmemory.py',        'MOVE upgrade (memory layer + line_interpolate_point)'),
]


print("=" * 80)
print(f"BENCH5-MASTER | dataset={os.environ.get('BENCH_DATASET', 'undefined')} | "
      f"N_TRIPS={os.environ.get('BENCH_N_TRIPS', 'default')} | "
      f"N_FRAMES={os.environ.get('BENCH_N_FRAMES', 'default')} | "
      f"N_RUNS={os.environ.get('BENCH_N_RUNS', 'default')}")
print("=" * 80)


t_master_start = time.perf_counter()

for cid, script, desc in CONDITIONS:
    script_path = THIS_DIR / script
    if not script_path.exists():
        print(f"[MASTER] !! SKIP {cid}: script {script} not found")
        continue

    print()
    print("=" * 80)
    print(f"[MASTER] STARTING {cid} : {desc}")
    print(f"[MASTER]   script: {script_path}")
    print("=" * 80)

    # CPU+RAM monitoring (background thread sampling)
    metrics_dir = Path('/home/osboxes/bench_results') / os.environ.get('BENCH_DATASET', 'unknown') / 'rq1' / 'metrics'
    monitor = RunMonitor(cid, os.environ.get('BENCH_DATASET', 'unknown'), metrics_dir)
    monitor.start()

    t0 = time.perf_counter()
    try:
        # FIX bug #4 : isolated namespace per condition (avoid global pollution)
        cond_ns = {
            '__name__':    '__main__',
            '__file__':    str(script_path),
            '__builtins__': __builtins__,
        }
        exec(compile(open(str(script_path)).read(), str(script_path), 'exec'), cond_ns)
    except SystemExit:
        pass
    except Exception as e:
        import traceback
        print(f"[MASTER] !!! {cid} CRASHED: {e}")
        traceback.print_exc()
        print(f"[MASTER] !!! continuing with next condition")
    elapsed = time.perf_counter() - t0
    metrics = monitor.stop()
    if metrics:
        print(f"[MASTER] {cid} done in {elapsed:.0f}s | CPU avg {metrics['cpu_percent_avg']:.0f}% | "
              f"RAM peak {metrics['ram_peak_mb']:.0f} MB (delta +{metrics['ram_peak_delta_mb']:+.0f})")
    else:
        print(f"[MASTER] {cid} done in {elapsed:.0f}s")

    # Aggressive cleanup between conditions
    del cond_ns
    gc.collect()

t_master_total = time.perf_counter() - t_master_start
print()
print("=" * 80)
print(f"[MASTER] ALL CONDITIONS DONE in {t_master_total:.0f}s ({t_master_total/60:.1f} min)")
print(f"[MASTER] Now run bench5_aggregate.py to build the 6×2 matrix chart")
print("=" * 80)
