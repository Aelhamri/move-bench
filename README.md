# move-bench

Benchmarks RQ1 / RQ2 pour le mémoire MobilityDB+QGIS (Ayoub El Hamri, ULB 2026).

## Structure

```
bench_config.py          # config commune (DB, datasets, N_FRAMES, …)
rq1/                     # RQ1 : 6 conditions × 2 datasets (STIB 17k, AIS 6k)
  c1_ali_naive.py        # C1 – Ali naïf (PyMEOS OO loop + edit buffer)
  c2_ali_optim.py        # C2 – Ali optimisé (raw EWKB + provider direct)
  c3_move_fast_preview.py# C3 – MOVE Fast Preview (QgsMapCanvasItem)
  c4_columnar.py         # C4 – Columnar Ayoub (NumPy precompute)
  c5_move_upstream.py    # C5 – MOVE upstream (postgres + line_interpolate_point)
  c6_move_upgrade.py     # C6 – MOVE upgrade (memory layer cache)
  master.py              # orchestrateur séquentiel 6 conditions
  aggregate.py           # agrège les JSON par condition
  metrics.py             # calcul métriques (FPS, speedup, …)
  charts.py              # figures publication (matrix, waterfall, pareto)
  results/
    cross_matrix.csv     # résultats agrégés bench5

rq2/                     # RQ2 : wall-clock 60s, QGIS vs Flask/JS
  c6_qgis_bench.py       # C6 QGIS adapté wall-clock
  orchestrator.py        # lance QGIS + Flask en parallèle
  flask_bench_routes.py  # routes Flask pour bench côté serveur
  frontend_bench.js      # bench JS côté navigateur
  resource_sampler.py    # CPU/RAM sampling (attach via PID)
  aggregate.py / charts.py
  results/
    cross_matrix.csv

move_plugin/             # fork MOVE plugin (base de la PR)
  move_trajectory_item.py  # ← implémentation QgsMapCanvasItem (C3/Fast Preview)
  move_task.py             # tâche async MobilityDB → features
  move_query.py            # requêtes SQL MobilityDB
  move_dockwidget.py       # UI dock
  move.py                  # point d'entrée plugin QGIS
```

## Usage rapide

```bash
# Configurer la connexion DB
cp bench_config.py.example bench_config.py  # adapter host/port/dbname

# RQ1 — toutes conditions, dataset STIB
BENCH_DATASET=stib BENCH_N_TRIPS=100 python rq1/master.py

# RQ2 — via orchestrateur
python rq2/orchestrator.py
```

## Conditions C1–C6

| ID | Nom | Mesure |
|----|-----|--------|
| C1 | Ali naïf | compute-only (commitChanges sans rendu) |
| C2 | Ali optimisé | compute-only |
| C3 | MOVE Fast Preview | compute + rendu (QPainter direct) |
| C4 | Columnar NumPy | compute-only |
| C5 | MOVE upstream | compute + rendu (QgsMapRendererSequentialJob) |
| C6 | MOVE upgrade (memory) | compute + rendu |

C3 vs C5/C6 : apples-to-oranges sur le rendu — voir METHODOLOGY.md dans rq1/.
