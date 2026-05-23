"""
RQ1-FINAL — Condition C3 : MOVE Fast Preview (MoveTrajectoryItem direct paint)
================================================================================
Bench du path "fast preview" de MOVE plugin : QgsMapCanvasItem qui paint
directement via QPainter sur le canvas, bypassant tout le QgsMapRendererJob
pipeline.

Source du code :
  /home/osboxes/move_pr_work/move/move_trajectory_item.py (MOVE plugin officiel)

Trade-offs (déjà documentés dans le code source) :
  - Pas de QgsVectorLayer (pas de layer tree, opacity, save/restore .qgz)
  - Pas d'identify, selection, attribute table
  - Pas de print composer
  - Pas d'on-the-fly CRS reproj
  - Pas d'interop autres plugins (Trajectools, MovingPandas)

C'est le concurrent direct de la PR memory layer (C6) — bench obligatoire
pour pré-répondre à "et avec MoveTrajectoryItem ?".

Pattern bench :
  - Build matview commun (même que MOVE upstream / upgrade)
  - Instancie MoveTrajectoryItem
  - Pour chaque frame : setTemporalRange + canvas.update + measure paint time
  - JSON output conforme bench5_aggregate format

Author: Ayoub El Hamri
"""

import sys, os, time, gc, json, statistics, resource, uuid
from datetime import datetime, timezone
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS_DIR.parent))
sys.path.insert(0, '/home/osboxes/move_pr_work/move')  # for MoveTrajectoryItem
import bench_config as cfg

import psycopg2
# FIX bug #8 : sanity check psycopg (v3) before import MoveTrajectoryItem
try:
    import psycopg  # noqa: F401
except ImportError:
    raise RuntimeError("FATAL: psycopg (v3) not installed. Run: pip install psycopg")
from move_trajectory_item import MoveTrajectoryItem

from qgis.PyQt.QtCore import QDateTime, Qt, QEventLoop, QTimer
from qgis.PyQt.QtWidgets import QApplication
from qgis.utils import iface
from qgis.core import (
    Qgis, QgsCoordinateReferenceSystem, QgsDateTimeRange, QgsMessageLog,
    QgsProject, QgsVectorLayer, QgsRectangle,
)


N_TRIPS_TARGET = int(os.environ.get("BENCH_N_TRIPS",
                                     os.environ.get("BENCH_LIMIT", "5000")))
N_FRAMES = int(os.environ.get("BENCH_N_FRAMES", "60"))
N_RUNS = int(os.environ.get("BENCH_N_RUNS", "5"))
LABEL = "c3_move_fast"

cfg.ensure_output_dir()
OUTPUT_DIR = cfg.RQ1_DIR
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def log(msg):
    QgsMessageLog.logMessage(str(msg), 'BENCH5-C3', level=Qgis.Info)
    print(f"[C3-FAST] {msg}")


def peak_rss_mb():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


# -----------------------------------------------------------------------------
log("=" * 72)
log(f"C3 MOVE_FAST | dataset={cfg.DATASET} | N={N_TRIPS_TARGET} | T={N_FRAMES} | runs={N_RUNS}")
log("=" * 72)
cfg.print_dataset_banner()


# -----------------------------------------------------------------------------
# Build matview (same SQL as bench_final_corrected for fair comparison)
# -----------------------------------------------------------------------------
VIEW_NAME = f"c3_fast_{uuid.uuid4().hex[:8]}"
inner_sql = cfg.trip_selection_sql(N_TRIPS_TARGET)
matview_sql = f"""
CREATE MATERIALIZED VIEW {VIEW_NAME} AS (
    WITH temp_1 AS ({inner_sql}),
         temp_raw AS (SELECT
             ST_SetSRID(geometry({cfg.TPOINT_COLUMN}, false), {cfg.DB_SRID}) AS geom_raw
         FROM temp_1),
         temp_2 AS (SELECT
             CASE
                 WHEN ST_GeometryType(geom_raw) = 'ST_LineString' THEN geom_raw
                 WHEN ST_GeometryType(geom_raw) = 'ST_MultiLineString' THEN ST_GeometryN(geom_raw, 1)
                 ELSE NULL
             END AS geom
         FROM temp_raw)
    SELECT row_number() OVER (ORDER BY 1) AS id, geom,
        to_timestamp(st_m(st_startpoint(geom))) AT TIME ZONE 'gmt' AS start_t,
        to_timestamp(st_m(st_endpoint(geom)))   AT TIME ZONE 'gmt' AS end_t
    FROM temp_2
    WHERE geom IS NOT NULL AND ST_GeometryType(geom) = 'ST_LineString'
      AND st_npoints(geom) >= 2
      AND st_m(st_startpoint(geom)) IS NOT NULL
      AND st_m(st_endpoint(geom)) IS NOT NULL
)
"""

t_load = time.perf_counter()
rss_before_load = peak_rss_mb()
with psycopg2.connect(host=cfg.DB_HOST, port=cfg.DB_PORT, dbname=cfg.DB_NAME,
                       user=cfg.DB_USER, password=cfg.DB_PASSWORD) as conn:
    with conn.cursor() as cur:
        cur.execute(f"DROP MATERIALIZED VIEW IF EXISTS {VIEW_NAME}")
        cur.execute(matview_sql)
        cur.execute(f"CREATE INDEX {VIEW_NAME}_geom_idx ON {VIEW_NAME} USING spgist (geom)")
        cur.execute(f"ANALYZE {VIEW_NAME}")
        cur.execute(f"SELECT count(*), min(start_t), max(end_t) FROM {VIEW_NAME}")
        n_features, gs, ge = cur.fetchone()
        conn.commit()
log(f"  matview built : {n_features} features")


# -----------------------------------------------------------------------------
# Setup canvas (parallel rendering OFF for honest measurement)
# -----------------------------------------------------------------------------
canvas = iface.mapCanvas()
saved_layers = canvas.layers()
saved_extent = canvas.extent()
saved_dest_crs = canvas.mapSettings().destinationCrs()
saved_temp_rng = canvas.temporalRange()
saved_parallel = canvas.isParallelRenderingEnabled()
saved_caching = canvas.isCachingEnabled()

canvas.setParallelRenderingEnabled(False)
canvas.setCachingEnabled(False)
canvas.setDestinationCrs(QgsCoordinateReferenceSystem(f"EPSG:{cfg.DB_SRID}"))
# FIX bug #11 : add an empty memory layer so canvas triggers paint cycle
# (with 0 layers, QgsMapRendererJob may not run)
_dummy = QgsVectorLayer(f"Point?crs=epsg:{cfg.DB_SRID}", "C3_DUMMY", "memory")
QgsProject.instance().addMapLayer(_dummy)
canvas.setLayers([_dummy])


# -----------------------------------------------------------------------------
# Instantiate MoveTrajectoryItem
# -----------------------------------------------------------------------------
db_params = {
    'host':     cfg.DB_HOST,
    'port':     cfg.DB_PORT,
    'database': cfg.DB_NAME,
    'username': cfg.DB_USER,
    'password': cfg.DB_PASSWORD,
}

t_item = time.perf_counter()
trip_item = MoveTrajectoryItem(canvas, VIEW_NAME, db_params)
TIME_item_load = time.perf_counter() - t_item
TIME_load = time.perf_counter() - t_load
rss_after_load = peak_rss_mb()
log(f"  MoveTrajectoryItem loaded : {trip_item.trip_count()} trips in {TIME_item_load:.1f}s")
log(f"  Total load              : {TIME_load:.1f}s, RSS {rss_after_load:.0f} MB (delta {rss_after_load - rss_before_load:+.0f})")


# FIX bug #10 : full extent via SQL ST_Extent (not 200 trip sample)
with psycopg2.connect(host=cfg.DB_HOST, port=cfg.DB_PORT, dbname=cfg.DB_NAME,
                       user=cfg.DB_USER, password=cfg.DB_PASSWORD) as conn:
    with conn.cursor() as cur:
        cur.execute(f"SELECT ST_Extent(geom)::text FROM {VIEW_NAME}")
        ext_text = cur.fetchone()[0]
if ext_text and ext_text.startswith('BOX('):
    coords = ext_text[4:-1].replace(',', ' ').split()
    minx, miny, maxx, maxy = map(float, coords)
    canvas.setExtent(QgsRectangle(minx, miny, maxx, maxy))
    log(f"  canvas extent set : ({minx:.0f}, {miny:.0f}) -> ({maxx:.0f}, {maxy:.0f})")


# -----------------------------------------------------------------------------
# Frame epochs (UTC explicit)
# -----------------------------------------------------------------------------
gs_epoch = gs.replace(tzinfo=timezone.utc).timestamp()
ge_epoch = ge.replace(tzinfo=timezone.utc).timestamp()
total_seconds = ge_epoch - gs_epoch
wide_step = total_seconds / N_FRAMES
frame_epochs = [(gs_epoch + (i + 0.5) * wide_step, 5) for i in range(N_FRAMES)]


# -----------------------------------------------------------------------------
# Bench loop : MoveTrajectoryItem responds to canvas.temporalRangeChanged
# We measure the time from setTemporalRange to canvas.update() completion
# -----------------------------------------------------------------------------
# FIX bugs #5, #7, #11, #13 : paint timing
# - retire double trip_item.update() (signal déjà déclenché par setTemporalRange)
# - utilise canvas.repaint() qui est synchrone (force paint cycle complet)
def wait_paint():
    """Synchronous paint via QWidget.repaint() (forces immediate paint)."""
    QApplication.processEvents()
    canvas.viewport().repaint()


# Attach the item to canvas signals (it auto-listens to temporalRangeChanged)
trip_item.attach_to_canvas_signals()

# Sanity check : count paint() calls to detect coalescing artifacts
_paint_count = [0]
_orig_paint = trip_item.paint
def _counting_paint(*args, **kwargs):
    _paint_count[0] += 1
    return _orig_paint(*args, **kwargs)
trip_item.paint = _counting_paint


log("")
log("=" * 72)
log(f"BENCH C3 (N_RUNS={N_RUNS}, run 1 dropped)")
log("=" * 72)

all_runs = []

for run_id in range(1, N_RUNS + 1):
    gc.collect()
    rss_before_run = peak_rss_mb()
    frame_records = []

    log(f"  RUN {run_id}/{N_RUNS}")
    for frame_idx, (fe, fdur) in enumerate(frame_epochs):
        t_frame_start = time.perf_counter()
        qt_s = QDateTime.fromMSecsSinceEpoch(int(fe * 1000), Qt.UTC)
        qt_e = QDateTime.fromMSecsSinceEpoch(int((fe + fdur) * 1000), Qt.UTC)
        canvas.setTemporalRange(QgsDateTimeRange(qt_s, qt_e))
        wait_paint()
        t_frame_end = time.perf_counter()
        frame_records.append({
            "run_id": run_id,
            "frame_idx": frame_idx,
            "is_warmup": frame_idx < cfg.FRAME_WARMUP,
            "frame_time_ms": (t_frame_end - t_frame_start) * 1000,
            "visible": -1,  # MoveTrajectoryItem doesn't expose visible count
        })

    rss_after_run = peak_rss_mb()
    effective_warmup = cfg.FRAME_WARMUP if N_FRAMES > cfg.FRAME_WARMUP else max(1, N_FRAMES // 10)
    steady = [f for i, f in enumerate(frame_records) if i >= effective_warmup]
    frame_ms = [f["frame_time_ms"] for f in steady]
    if not frame_ms:
        log(f"  WARNING: no steady frames, using all")
        frame_ms = [f["frame_time_ms"] for f in frame_records]

    run_summary = {
        "run_id":          run_id,
        "n_frames_total":  len(frame_records),
        "n_frames_steady": len(steady),
        "median_frame_ms": statistics.median(frame_ms),
        "median_fps":      1000.0 / statistics.median(frame_ms),
        "p5_fps":          1000.0 / statistics.quantiles(frame_ms, n=20)[18] if len(frame_ms) >= 20 else 1000.0 / max(frame_ms),
        "p95_fps":         1000.0 / statistics.quantiles(frame_ms, n=20)[0] if len(frame_ms) >= 20 else 1000.0 / min(frame_ms),
        "rss_before_mb":   rss_before_run,
        "rss_after_mb":    rss_after_run,
    }
    all_runs.append({"summary": run_summary, "frames": frame_records})

    log(f"    -> median {run_summary['median_frame_ms']:.1f} ms ({run_summary['median_fps']:.2f} FPS)")


# -----------------------------------------------------------------------------
# Cleanup
# -----------------------------------------------------------------------------
log(f"  TOTAL paint() invoked: {_paint_count[0]} (expected ~{N_FRAMES * N_RUNS})")
if _paint_count[0] < N_FRAMES * N_RUNS * 0.5:
    log(f"  WARNING: paint count too low — frame_time_ms may include noise from missed paints")

trip_item.cleanup()
del trip_item
QgsProject.instance().removeMapLayer(_dummy.id())
canvas.setParallelRenderingEnabled(saved_parallel)
canvas.setCachingEnabled(saved_caching)
canvas.setLayers(saved_layers)
canvas.setExtent(saved_extent)
canvas.setDestinationCrs(saved_dest_crs)
if saved_temp_rng is not None:
    canvas.setTemporalRange(saved_temp_rng)

with psycopg2.connect(host=cfg.DB_HOST, port=cfg.DB_PORT, dbname=cfg.DB_NAME,
                       user=cfg.DB_USER, password=cfg.DB_PASSWORD) as conn:
    with conn.cursor() as cur:
        cur.execute(f"DROP MATERIALIZED VIEW IF EXISTS {VIEW_NAME}")
        conn.commit()
gc.collect()


# -----------------------------------------------------------------------------
# Export JSON
# -----------------------------------------------------------------------------
summary_path = OUTPUT_DIR / f"bench5_{LABEL}_{cfg.DATASET}_summary.json"
summary = {
    "label":              LABEL,
    "dataset":            cfg.DATASET,
    "table":              cfg.TABLE_NAME,
    "srid":               cfg.DB_SRID,
    "date":               cfg.BENCH_DATE,
    "limit":              N_TRIPS_TARGET,
    "n_trips":            n_features,
    "n_frames_per_run":   N_FRAMES,
    "frame_step_seconds": cfg.FRAME_STEP_SECONDS,
    "warmup_frames":      cfg.FRAME_WARMUP,
    "n_runs":             N_RUNS,
    "load": {
        "item_seconds":      TIME_item_load,
        "total_seconds":     TIME_load,
        "rss_after_mb":      rss_after_load,
        "rss_delta_mb":      rss_after_load - rss_before_load,
    },
    "runs": [r["summary"] for r in all_runs],
}
with open(summary_path, "w") as f:
    json.dump(summary, f, indent=2, default=str)


# -----------------------------------------------------------------------------
# Verdict
# -----------------------------------------------------------------------------
runs_kept = [r["summary"] for r in all_runs[1:]] if len(all_runs) > 1 else [r["summary"] for r in all_runs]
median_of_medians = statistics.median([r["median_frame_ms"] for r in runs_kept])

log("")
log("=" * 72)
log(f"VERDICT C3 MOVE_FAST (run 1 dropped)")
log("=" * 72)
log(f"  Median frame time : {median_of_medians:.1f} ms ({1000/median_of_medians:.1f} FPS)")
log(f"  RSS peak          : {max(r['rss_after_mb'] for r in runs_kept):.0f} MB")
log(f"  Summary JSON      : {summary_path}")
log("=" * 72)
