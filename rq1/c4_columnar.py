"""
RQ1 - Condition B : COLUMN-ORIENTED (tsample + NumPy + WKB vectorise)
======================================================================
Optimisation column-oriented :

  Phase 1 (load) : tsample() cote MobilityDB pre-calcule toutes les
                   positions a intervalles fixes -> matrices NumPy X[N,T] et Y[N,T]
  Phase 2 (frame): X[:, t] / Y[:, t] = lecture memoire contigue O(N), zero
                   appel value_at_timestamp dans la boucle.
                   WKB construit en bulk via NumPy view (pas de Shapely).

Memes corrections methodologiques que baseline.py :
  1. Pas de try/except dans la hot loop (active_mask via np.isnan)
  2. Warm-up frames jetees a l'analyse
  3. Multi-run, run 1 jete
  4. Peak RSS

Author: Ayoub El Hamri
"""

import sys
import os
import time
import csv
import json
import struct
import resource
import statistics
from datetime import timedelta
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS_DIR.parent))
import bench_config as cfg

import numpy as np
import psycopg2
from pymeos import pymeos_initialize
from shapely.geometry import Point

from qgis.PyQt.QtCore import QVariant
from qgis.core import (
    Qgis,
    QgsVectorLayerTemporalProperties,
    QgsFeature,
    QgsGeometry,
    QgsVectorLayer,
    QgsField,
    QgsProject,
    QgsMessageLog,
)
from PyQt5.QtCore import QDateTime


LIMIT    = int(os.environ.get("BENCH_LIMIT", "0"))
# bench5 override : honor BENCH_N_FRAMES / BENCH_N_RUNS env vars if set
N_FRAMES = int(os.environ.get("BENCH_N_FRAMES", str(cfg.N_FRAMES_SYNTHETIC)))
_N_RUNS_OVERRIDE = int(os.environ.get("BENCH_N_RUNS", str(cfg.N_RUNS_PER_SCENARIO)))

cfg.ensure_output_dir()
OUTPUT_DIR = cfg.RQ1_DIR
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

LABEL = "columnar"

# Intervalle de tsample = pas de frame -> le scrub est aligne sur les samples
SAMPLE_INTERVAL = f"{cfg.FRAME_STEP_SECONDS} seconds"


def log(msg):
    QgsMessageLog.logMessage(str(msg), 'RQ1-Columnar', level=Qgis.Info)
    print(f"[COLUMNAR] {msg}")


def peak_rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


# -----------------------------------------------------------------------------
# Phase 1 : tsample() + matrice NumPy
# -----------------------------------------------------------------------------
pymeos_initialize()

log("=" * 64)
log(f"RQ1-COLUMNAR | dataset={cfg.DATASET} | "
    f"date={cfg.BENCH_DATE or 'static'} | limit={LIMIT or 'all'} | sample={SAMPLE_INTERVAL}")
log("=" * 64)
cfg.print_dataset_banner()

t_load_start = time.perf_counter()
rss_before_load = peak_rss_mb()

conn = psycopg2.connect(
    host=cfg.DB_HOST, port=cfg.DB_PORT,
    dbname=cfg.DB_NAME, user=cfg.DB_USER, password=cfg.DB_PASSWORD,
)
cur = conn.cursor()

# Validation
cur.execute(cfg.COUNT_VALIDATION_SQL)
date_label = f"sur {cfg.BENCH_DATE}" if cfg.BENCH_DATE else "(snapshot statique)"
log(f"  Validation DB : {cur.fetchone()[0]} trips utilisables {date_label}")

# Construction du WHERE composite (compatible STIB / AIS)
_where_parts = [cfg.QUALITY_FILTER_SQL]
if cfg.DATE_FILTER_SQL:
    _where_parts.append(cfg.DATE_FILTER_SQL)
WHERE_SQL = " AND ".join(_where_parts)

# Selection CTE deterministe partagee par les deux requetes ci-dessous
limit_clause = f"LIMIT {LIMIT}" if LIMIT > 0 else ""
SELECTION_CTE = f"""
    SELECT {cfg.ID_COLUMN}, {cfg.TPOINT_COLUMN}
    FROM {cfg.TABLE_NAME}
    WHERE {WHERE_SQL}
    ORDER BY {cfg.ID_COLUMN}
    {limit_clause}
"""

# Bornes pour ancrer tsample :
#  - si BENCH_HOUR_START/END sont definis (cas STIB), on ancre la matrice
#    sur la fenetre du bench (matrice bornee, pas d'overflow temporel)
#  - sinon (cas AIS), on prend les bornes globales des trips selectionnes
if cfg.BENCH_HOUR_START is not None and cfg.BENCH_HOUR_END is not None:
    cur.execute(f"""
        SELECT
            '{cfg.BENCH_DATE} {cfg.BENCH_HOUR_START:02d}:00:00'::timestamp AS t_min,
            '{cfg.BENCH_DATE} {cfg.BENCH_HOUR_END:02d}:00:00'::timestamp   AS t_max,
            (SELECT COUNT(*) FROM ({SELECTION_CTE}) sel)                   AS n
    """)
else:
    cur.execute(f"""
        WITH selected AS ({SELECTION_CTE})
        SELECT
            MIN(startTimestamp({cfg.TPOINT_COLUMN})) AS t_min,
            MAX(endTimestamp({cfg.TPOINT_COLUMN}))   AS t_max,
            COUNT(*)                                  AS n
        FROM selected
    """)
(window_start, window_end, n_selected) = cur.fetchone()
log(f"  Selection : {n_selected} trips, fenetre matrice = {window_start} -> {window_end}")

# tsample() ancre sur window_start, ET filtre les instants dans la fenetre.
# Le filtre BETWEEN reduit la matrice a exactement la fenetre du bench :
# au lieu de N x (8h equivalent), on obtient N x (window_end - window_start).
log(f"  Executing tsample('{SAMPLE_INTERVAL}'::interval) bounded to window...")
t_tsample_start = time.perf_counter()
cur.execute(f"""
    WITH selected AS ({SELECTION_CTE})
    SELECT
        sub.{cfg.ID_COLUMN},
        array_agg(getTimestamp(inst) ORDER BY getTimestamp(inst)) AS ts_list,
        array_agg(ST_X(getValue(inst)::geometry) ORDER BY getTimestamp(inst)) AS xs,
        array_agg(ST_Y(getValue(inst)::geometry) ORDER BY getTimestamp(inst)) AS ys,
        MIN(getTimestamp(inst)) AS t_start,
        MAX(getTimestamp(inst)) AS t_end
    FROM (
        SELECT
            {cfg.ID_COLUMN},
            unnest(instants(tsample(
                {cfg.TPOINT_COLUMN},
                '{SAMPLE_INTERVAL}'::interval,
                %s::timestamptz
            ))) AS inst
        FROM selected
    ) sub
    WHERE getTimestamp(inst) >= %s::timestamptz
      AND getTimestamp(inst) <  %s::timestamptz
    GROUP BY sub.{cfg.ID_COLUMN}
    HAVING COUNT(*) > 0
    ORDER BY sub.{cfg.ID_COLUMN}
""", (window_start, window_start, window_end))
rows = cur.fetchall()
t_tsample_end = time.perf_counter()
TIME_tsample = t_tsample_end - t_tsample_start
log(f"  -> tsample termine en {TIME_tsample:.3f}s ({len(rows)} trips)")

# Construction matrice NumPy
log("  Building NumPy matrix...")
t_matrix_start = time.perf_counter()

all_ts_set = set()
for (_, ts_list, _, _, _, _) in rows:
    all_ts_set.update(ts_list)
all_timestamps = sorted(all_ts_set)
N_frames_total = len(all_timestamps)
N_ships        = len(rows)
ts_to_col      = {ts: i for i, ts in enumerate(all_timestamps)}

estimated_mb = cfg.estimate_columnar_matrix_mb(N_ships, N_frames_total)
log(f"  Matrice {N_ships}x{N_frames_total} ({estimated_mb:.1f} MB estime, "
    f"limite={cfg.COLUMNAR_MATRIX_LIMIT_MB:.0f} MB)")

# Garde-fou : abort propre AVANT np.full() qui ferait crasher QGIS si trop gros
cfg.assert_columnar_matrix_fits(N_ships, N_frames_total)

X_mat = np.full((N_ships, N_frames_total), np.nan, dtype=np.float64)
Y_mat = np.full((N_ships, N_frames_total), np.nan, dtype=np.float64)

trip_ids      = []
trip_bounds   = {}    # row_idx -> (t_start, t_end)

for row_idx, (trip_id, ts_list, xs, ys, t_s, t_e) in enumerate(rows):
    trip_ids.append(trip_id)
    trip_bounds[row_idx] = (t_s, t_e)
    for ts, x, y in zip(ts_list, xs, ys):
        if x is not None and y is not None:
            col = ts_to_col[ts]
            X_mat[row_idx, col] = x
            Y_mat[row_idx, col] = y

t_matrix_end = time.perf_counter()
TIME_matrix = t_matrix_end - t_matrix_start
log(f"  -> Matrice construite en {TIME_matrix:.3f}s")

cur.close()
conn.close()

t_load_end = time.perf_counter()
TIME_load = t_load_end - t_load_start
rss_after_load = peak_rss_mb()
log(f"  -> Load total {TIME_load:.3f}s | RSS {rss_after_load:.0f} MB (delta {rss_after_load - rss_before_load:+.0f})")


# -----------------------------------------------------------------------------
# VectorLayer QGIS
# -----------------------------------------------------------------------------
vlayer = QgsVectorLayer(
    f"Point?crs=epsg:{cfg.DB_SRID}",
    "RQ1_Columnar_Optimized",
    "memory",
)
pr = vlayer.dataProvider()
pr.addAttributes([
    QgsField("id", QVariant.String),
    QgsField("start_time", QVariant.DateTime),
    QgsField("end_time",   QVariant.DateTime),
])
vlayer.updateFields()

tp = vlayer.temporalProperties()
tp.setIsActive(True)
tp.setMode(QgsVectorLayerTemporalProperties.ModeFeatureDateTimeStartAndEndFromFields)
tp.setStartField("start_time")
tp.setEndField("end_time")
vlayer.updateFields()
QgsProject.instance().addMapLayer(vlayer)

vlayer_fields = vlayer.fields()
features_list = []
geometries    = {}

for row_idx, trip_id in enumerate(trip_ids):
    feat = QgsFeature(vlayer_fields)
    t_s, t_e = trip_bounds[row_idx]
    feat.setAttributes([str(trip_id), QDateTime(t_s), QDateTime(t_e)])
    geom = QgsGeometry()
    geometries[row_idx + 1] = geom    # QGIS feature IDs start at 1
    feat.setGeometry(geom)
    features_list.append(feat)

pr.addFeatures(features_list)
vlayer.updateExtents()
objects_count = len(trip_ids)
log(f"  -> VectorLayer : {objects_count} features")


# -----------------------------------------------------------------------------
# WKB builder vectorise (pre-compute une fois)
# -----------------------------------------------------------------------------
WKB_HEADER = struct.pack('<BI', 1, 1)   # little-endian, Point 2D
empty_geom_wkb = Point().wkb

def build_wkb_matrix(xs_col: np.ndarray, ys_col: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """Construit (N,21) bytes via NumPy. Aucune boucle Python ici."""
    n = len(xs_col)
    buf = np.empty((n, 21), dtype=np.uint8)
    buf[:, 0]   = 1            # byte order
    buf[:, 1]   = 1            # type LSB
    buf[:, 2:5] = 0            # type MSBs

    x_safe = np.where(mask, xs_col, 0.0).astype('<f8')
    y_safe = np.where(mask, ys_col, 0.0).astype('<f8')
    buf[:, 5:13]  = x_safe.view(np.uint8).reshape(n, 8)
    buf[:, 13:21] = y_safe.view(np.uint8).reshape(n, 8)
    return buf


# -----------------------------------------------------------------------------
# Phase 2 : multi-run animation
# -----------------------------------------------------------------------------
log("")
log("=" * 64)
log(f"BENCHMARK COLUMNAR | {N_FRAMES} frames | {_N_RUNS_OVERRIDE} runs (run 1 jete)")
log("=" * 64)

n_frames_run = min(N_FRAMES, N_frames_total)
# Demarrer au MIDPOINT de la matrice (pareil que baseline.py).
# Ainsi le bench frappe les frames les plus charges (peak hour, vis~hauteur).
midpoint_col = N_frames_total // 2
start_col    = max(0, midpoint_col - n_frames_run // 2)
end_col      = min(N_frames_total, start_col + n_frames_run)
n_frames_run = end_col - start_col
log(f"  Bench frames cols [{start_col}, {end_col}) "
    f"= {all_timestamps[start_col]} -> {all_timestamps[end_col-1]}")

all_runs = []

for run_id in range(1, _N_RUNS_OVERRIDE + 1):
    log("")
    log("-" * 64)
    log(f"RUN {run_id}/{_N_RUNS_OVERRIDE}")
    log("-" * 64)

    rss_before_run = peak_rss_mb()
    frame_records = []

    for frame_idx in range(n_frames_run):
        t_frame_start = time.perf_counter()

        col = start_col + frame_idx

        # ============================================================
        # COLUMN SLICE
        # ============================================================
        t_slice_start = time.perf_counter()
        xs   = X_mat[:, col]
        ys   = Y_mat[:, col]
        mask = ~np.isnan(xs)
        t_slice_end = time.perf_counter()

        # ============================================================
        # WKB BULK BUILD (NumPy) + boucle inevitable QGIS
        # ============================================================
        t_build_start = time.perf_counter()
        wkb_mat = build_wkb_matrix(xs, ys, mask)
        visible = 0
        for row_idx in range(objects_count):
            fid = row_idx + 1
            if mask[row_idx]:
                geometries[fid].fromWkb(bytes(wkb_mat[row_idx]))
                visible += 1
            else:
                geometries[fid].fromWkb(empty_geom_wkb)
        t_build_end = time.perf_counter()

        # ============================================================
        # QGIS UPDATE
        # ============================================================
        t_update_start = time.perf_counter()
        vlayer.startEditing()
        pr.changeGeometryValues(geometries)
        vlayer.commitChanges()
        t_update_end = time.perf_counter()

        t_frame_end = time.perf_counter()

        frame_records.append({
            "run_id":         run_id,
            "frame_idx":      frame_idx,
            "is_warmup":      frame_idx < cfg.FRAME_WARMUP,
            "frame_time_ms":  (t_frame_end   - t_frame_start) * 1000,
            "slice_ms":       (t_slice_end   - t_slice_start) * 1000,
            "geom_build_ms":  (t_build_end   - t_build_start) * 1000,
            "geom_update_ms": (t_update_end  - t_update_start) * 1000,
            "visible":        visible,
            "skipped":        objects_count - visible,
        })

        if frame_idx % 20 == 0:
            ft = frame_records[-1]["frame_time_ms"]
            fps = 1000.0 / ft if ft > 0 else 0
            log(f"  frame {frame_idx:3d} | vis={visible:5d} | "
                f"slice={frame_records[-1]['slice_ms']:.2f}ms "
                f"build={frame_records[-1]['geom_build_ms']:.1f}ms "
                f"update={frame_records[-1]['geom_update_ms']:.1f}ms | "
                f"frame={ft:.1f}ms ({fps:.1f} FPS)")

    rss_after_run = peak_rss_mb()

    steady = [f for f in frame_records if not f["is_warmup"]]
    frame_ms = [f["frame_time_ms"] for f in steady]

    run_summary = {
        "run_id":          run_id,
        "n_frames_total":  len(frame_records),
        "n_frames_steady": len(steady),
        "median_frame_ms": statistics.median(frame_ms),
        "median_fps":      1000.0 / statistics.median(frame_ms),
        "p5_fps":          1000.0 / statistics.quantiles(frame_ms, n=20)[18],
        "p95_fps":         1000.0 / statistics.quantiles(frame_ms, n=20)[0],
        "rss_before_mb":   rss_before_run,
        "rss_after_mb":    rss_after_run,
    }
    all_runs.append({"summary": run_summary, "frames": frame_records})

    log(f"  -> run {run_id} | FPS median={run_summary['median_fps']:.2f} | "
        f"RSS={rss_after_run:.0f} MB")


# -----------------------------------------------------------------------------
# Export
# -----------------------------------------------------------------------------
csv_path = OUTPUT_DIR / f"{LABEL}_limit{LIMIT}.csv"
with open(csv_path, "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["run_id", "frame_idx", "is_warmup",
                "frame_time_ms", "slice_ms", "geom_build_ms", "geom_update_ms",
                "visible", "skipped"])
    for run in all_runs:
        for r in run["frames"]:
            w.writerow([r["run_id"], r["frame_idx"], int(r["is_warmup"]),
                        r["frame_time_ms"], r["slice_ms"], r["geom_build_ms"], r["geom_update_ms"],
                        r["visible"], r["skipped"]])

summary_path = OUTPUT_DIR / f"{LABEL}_limit{LIMIT}_summary.json"
summary = {
    "label":              LABEL,
    "dataset":            cfg.DATASET,
    "table":              cfg.TABLE_NAME,
    "srid":               cfg.DB_SRID,
    "date":               cfg.BENCH_DATE,
    "limit":              LIMIT,
    "n_trips":            objects_count,
    "n_frames_per_run":   n_frames_run,
    "frame_step_seconds": cfg.FRAME_STEP_SECONDS,
    "sample_interval":    SAMPLE_INTERVAL,
    "warmup_frames":      cfg.FRAME_WARMUP,
    "n_runs":             _N_RUNS_OVERRIDE,
    "load": {
        "tsample_seconds":   TIME_tsample,
        "matrix_seconds":    TIME_matrix,
        "total_seconds":     TIME_load,
        "matrix_rss_delta_mb": rss_after_load - rss_before_load,
        "rss_after_mb":      rss_after_load,
    },
    "runs": [r["summary"] for r in all_runs],
}
with open(summary_path, "w") as f:
    json.dump(summary, f, indent=2, default=str)


# -----------------------------------------------------------------------------
# Rapport
# -----------------------------------------------------------------------------
runs_kept = [r["summary"] for r in all_runs[1:]]
median_of_medians = statistics.median([r["median_fps"] for r in runs_kept])

log("")
log("=" * 64)
log(f"RESULTATS RQ1-COLUMNAR (run 1 jete)")
log("=" * 64)
log(f"  Trips animes        : {objects_count}")
log(f"  FPS median (median) : {median_of_medians:.2f}")
log(f"  RSS peak            : {max(r['rss_after_mb'] for r in runs_kept):.0f} MB")
log("")
log(f"  CSV     : {csv_path}")
log(f"  Summary : {summary_path}")
log("=" * 64)
log(f"  Lance maintenant rq1/plot_rq1.py (en dehors de QGIS)")
