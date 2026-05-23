# move-bench

Benchmark suite for [MOVE](https://github.com/MobilityDB/move) — a QGIS plugin for
animating MobilityDB trajectories. Measures six rendering approaches (C1–C6) across
two datasets (STIB Brussels buses, Danish AIS ships).

Part of Ayoub El Hamri's master's thesis, ULB 2026.

---

## Prerequisites

| Requirement | Version tested |
|-------------|---------------|
| QGIS        | 3.34 LTR or 3.40+ |
| PostgreSQL  | 14+ with MobilityDB 1.2+ |
| Python      | 3.10+ (QGIS embedded) |
| pymeos      | 1.2+ (`pip install pymeos pymeos-cffi`) |
| psycopg2    | any (`pip install psycopg2-binary`) |
| psycopg     | v3 (`pip install psycopg`) — C3 only |
| numpy, matplotlib, scipy | for analysis scripts |

The QGIS Python environment is used for all bench conditions (C1–C6) because
they call QGIS rendering APIs. Install Python deps into that environment:

```bash
# locate QGIS Python, e.g.:
/usr/bin/python3 -m pip install pymeos pymeos-cffi psycopg2-binary psycopg numpy matplotlib scipy
# or use the QGIS Python path shown in QGIS > Help > About
```

---

## Repository layout

```
bench_config.py              # shared config: DB connection, datasets, scenarios
rq1/
  c1_ali_naive.py            # C1 – Ali naive   (PyMEOS OO + edit buffer)
  c2_ali_optim.py            # C2 – Ali optim   (raw EWKB + provider direct)
  c3_move_fast_preview.py    # C3 – MOVE Fast Preview (QgsMapCanvasItem)
  c4_columnar.py             # C4 – Columnar NumPy precompute
  c5_move_upstream.py        # C5 – MOVE upstream (postgres layer + expression)
  c6_move_upgrade.py         # C6 – MOVE upgrade (memory layer + expression)
  master.py                  # runs all six conditions sequentially
  aggregate.py               # builds 6×2 matrix from JSON outputs
  metrics.py                 # CPU/RAM sampler (used by master.py)
  charts.py                  # generates publication figures (PDF + PNG)
  results/
    cross_matrix.csv         # pre-computed results (STIB 17k + AIS 6k)
rq2/
  c6_qgis_bench.py           # C6 in wall-clock mode (60 s steady state)
  orchestrator.py            # launches QGIS + Flask in parallel
  flask_bench_routes.py      # Flask server bench routes
  frontend_bench.js          # browser-side bench (Playwright/Puppeteer)
  resource_sampler.py        # CPU/RAM sampling via PID attachment
  aggregate.py / charts.py   # RQ2 analysis
  results/
    cross_matrix.csv         # pre-computed RQ2 results
move_plugin/
  move_trajectory_item.py    # QgsMapCanvasItem implementation (C3 source)
  move_task.py               # async fetch task
  move_query.py              # MobilityDB SQL queries
  move_dockwidget.py         # UI dock widget
  move.py                    # QGIS plugin entry point
```

---

## Configuration

All scripts read from `bench_config.py` at the repo root. Override via environment
variables — no file editing needed.

### Database connection

**STIB dataset (default)**:

```bash
export BENCH_DATASET=stib
export BENCH_DB_HOST=localhost
export BENCH_DB_PORT=5432
export BENCH_DB_NAME=rtdatahub_local
export BENCH_DB_USER=rtdatahub
export BENCH_DB_PASSWORD=rtdatahub
```

**AIS dataset**:

```bash
export BENCH_DATASET=ais
export BENCH_DB_NAME=AISdata01062023
export BENCH_DB_USER=postgres
export BENCH_DB_PASSWORD=postgres
```

### Key parameters

| Variable | Default | Description |
|----------|---------|-------------|
| `BENCH_DATASET` | `stib` | Dataset: `stib` or `ais` |
| `BENCH_N_TRIPS` | 5000 | Number of trips to animate |
| `BENCH_N_FRAMES` | 120 | Frames per run (RQ1) |
| `BENCH_N_RUNS` | 5 | Runs per condition (run 1 discarded) |
| `BENCH_HOUR_START` | 12 | Window start hour (STIB only) |
| `BENCH_HOUR_END` | 22 | Window end hour (STIB only) |
| `BENCH_OUTPUT_DIR` | `~/bench_results/<dataset>` | Output root |
| `BENCH_COLUMNAR_LIMIT_MB` | 4096 | RAM guard for C4 precomputed matrix |

---

## Running RQ1 (six conditions)

RQ1 scripts run inside QGIS because they invoke `QgsMapRendererSequentialJob`,
`QgsVectorLayer`, and `QgsMapCanvasItem`. Use the **QGIS Python console**
(Plugins → Python Console) or the `qgis --code` headless mode.

### Quick start — QGIS console

1. Open QGIS (no project needed).
2. Open the Python Console (`Ctrl+Alt+P`).
3. Paste and run:

```python
import sys, os

# --- configure ---
REPO = '/path/to/move-bench'          # <-- change this
os.environ['BENCH_DATASET']    = 'stib'
os.environ['BENCH_N_TRIPS']    = '100'   # start small
os.environ['BENCH_N_FRAMES']   = '30'
os.environ['BENCH_N_RUNS']     = '3'
os.environ['BENCH_SCRIPT_DIR'] = REPO + '/rq1'

# --- clean module cache (required on re-run) ---
for m in list(sys.modules):
    if m.startswith('bench_config') or m.startswith('metrics'):
        del sys.modules[m]

exec(open(REPO + '/rq1/master.py').read())
```

Results (JSON) are written to `~/bench_results/stib/rq1/`.

### Run a single condition

```python
import sys, os

REPO = '/path/to/move-bench'
os.environ['BENCH_DATASET']    = 'stib'
os.environ['BENCH_N_TRIPS']    = '500'
os.environ['BENCH_SCRIPT_DIR'] = REPO + '/rq1'

sys.path.insert(0, REPO + '/rq1')
sys.path.insert(0, REPO)

exec(open(REPO + '/rq1/c6_move_upgrade.py').read())
```

### Run headless (qgis --code)

```bash
export BENCH_DATASET=stib
export BENCH_N_TRIPS=5000
export BENCH_SCRIPT_DIR=/path/to/move-bench/rq1

qgis --code /path/to/move-bench/rq1/master.py
```

---

## Building the 6×2 matrix and charts

After all conditions complete, run **outside QGIS** (plain Python):

```bash
cd /path/to/move-bench

# Aggregate JSON → cross_matrix.csv
python3 rq1/aggregate.py

# Generate publication figures (PDF + PNG in rq1/charts/)
python3 rq1/charts.py
```

Pre-computed results are already in `rq1/results/cross_matrix.csv` if you just
want to regenerate the charts without re-running the bench:

```bash
cp rq1/results/cross_matrix.csv rq1/
python3 rq1/charts.py
```

---

## Running RQ2 (QGIS vs Flask wall-clock)

RQ2 compares C6 (QGIS memory layer) against a Flask server over a 60 s
steady-state window.

```bash
export BENCH_DATASET=stib
export BENCH_LIMIT=5000
export BENCH_SCRIPT_DIR=/path/to/move-bench/rq2

# Start the orchestrator (launches QGIS + Flask)
python3 /path/to/move-bench/rq2/orchestrator.py
```

Aggregate and chart:

```bash
python3 rq2/aggregate.py
python3 rq2/charts.py
```

---

## Conditions C1–C6 at a glance

| ID | Name | Rendering pipeline | What is measured |
|----|------|--------------------|-----------------|
| C1 | Ali naive | PyMEOS OO → edit buffer → `commitChanges` | compute-only |
| C2 | Ali optimized | raw EWKB → `changeGeometryValues` | compute-only |
| C3 | MOVE Fast Preview | `QgsMapCanvasItem` QPainter direct | compute + render |
| C4 | Columnar NumPy | pre-built XY matrices → provider | compute-only |
| C5 | MOVE upstream | postgres layer + `line_interpolate_point` expression | compute + render |
| C6 | MOVE upgrade | memory layer cache + same expression | compute + render |

> **C1/C2/C4 vs C3/C5/C6**: the first group measures geometry computation
> without pixel rendering (`commitChanges` triggers no raster pass). The
> second group measures end-to-end `QgsMapRendererSequentialJob` time. Compare
> within each group; cross-group ratios require a footnote.

---

## Key results (pre-computed, STIB 17k trips)

| Condition | FPS | Speedup vs C5 |
|-----------|-----|---------------|
| C5 MOVE upstream | 1.5 | 1× (baseline) |
| C6 MOVE upgrade  | 4.2 | ~3× |
| C3 Fast Preview  | 112 | ~75× |

C3's gain comes from bypassing the `QgsMapRendererJob` pipeline entirely;
it loses identify/select/print-composer/CRS-reprojection support.
See [MOVE PR draft](move_plugin/) for the proposed additive architecture.

---

## Memory budget (C4)

C4 pre-builds a full `(n_trips × n_frames)` float64 matrix. With default
settings (17k trips × 7200 frames at 5 s step) that exceeds 4 GB and QGIS
will crash. The config guards against this:

```bash
# Narrow the time window (recommended)
export BENCH_HOUR_START=17
export BENCH_HOUR_END=19     # 2 h → 1440 frames → ~400 MB for 5k trips

# Or raise the limit (only if you have the RAM)
export BENCH_COLUMNAR_LIMIT_MB=8192
```

---

## License

Bench scripts: MIT.  
MOVE plugin in `move_plugin/`: original [MOVE license](move_plugin/LICENSE).
