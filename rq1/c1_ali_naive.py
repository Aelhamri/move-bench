"""
RQ1-FINAL — Condition C1 : Ali Naïve (Interactive Mode géométrique honnête)
=============================================================================
Reproduit l'algorithme Interactive Mode publié par Ali Manzer (master thesis
2024) pour rendre les positions interpolées de trajectoires MobilityDB.

CORRECTION CRITIQUE vs `bench_ali_max_optim.py` L1_baseline :
  - Suppression du pré-filtre temporel `if fe < s_epoch or fe > e_epoch`
    (lignes 165-168 du bench_ali_max_optim) — Ali ORIGINAL n'a pas ce filter.
  - Try/except pour catch les out-of-range (style Ali) avec coût exception.
  - Le code Ali update ATTRIBUTS scalaires (TFloat sog), mais pour comparer
    les approches de RENDU DE POSITIONS, on l'adapte à `changeGeometryValues`
    qui est ce qu'Ali aurait fait pour visualiser des points.

Pattern strict :
  - `value_at_timestamp(dt)` PyMEOS OO (pas pymeos_cffi)
  - `pt.wkb` Shapely roundtrip
  - `startEditing/commitChanges` edit buffer
  - try/except sur tous les trips (pas de pré-filtre)

Author: Ayoub El Hamri (continuation Ali Manzer 2024)
"""

import sys, os, time, gc, json, statistics, resource
from datetime import datetime, timezone
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS_DIR.parent))
import bench_config as cfg

import psycopg2
import numpy as np
from pymeos import pymeos_initialize, TGeomPointSeq

from qgis.PyQt.QtCore import QVariant, QDateTime
from qgis.core import (
    Qgis, QgsCoordinateReferenceSystem, QgsFeature, QgsField,
    QgsGeometry, QgsMessageLog, QgsProject, QgsVectorLayer,
)


N_TRIPS_TARGET = int(os.environ.get("BENCH_N_TRIPS", "5000"))
N_FRAMES = int(os.environ.get("BENCH_N_FRAMES", "60"))
N_RUNS = int(os.environ.get("BENCH_N_RUNS", "5"))
LABEL = "c1_ali_naive"

cfg.ensure_output_dir()
OUTPUT_DIR = cfg.RQ1_DIR
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def log(msg):
    QgsMessageLog.logMessage(str(msg), 'BENCH5-C1', level=Qgis.Info)
    print(f"[C1-ALI-NAIVE] {msg}")


def peak_rss_mb():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


# -----------------------------------------------------------------------------
log("=" * 72)
log(f"C1 ALI_NAIVE | dataset={cfg.DATASET} | N={N_TRIPS_TARGET} | T={N_FRAMES} | runs={N_RUNS}")
log("=" * 72)
cfg.print_dataset_banner()

pymeos_initialize()

# Streaming load (identical to bench_ali_max_optim.py for fair comparison)
sql = f"""
    SELECT {cfg.ID_COLUMN},
           asText({cfg.TPOINT_COLUMN}),
           EXTRACT(EPOCH FROM startTimestamp({cfg.TPOINT_COLUMN})),
           EXTRACT(EPOCH FROM endTimestamp({cfg.TPOINT_COLUMN}))
    FROM {cfg.TABLE_NAME}
    WHERE {cfg.QUALITY_FILTER_SQL}
"""
if cfg.DATE_FILTER_SQL:
    sql += f"      AND {cfg.DATE_FILTER_SQL}\n"
sql += f"    ORDER BY {cfg.ID_COLUMN}\n"
sql += f"    LIMIT {N_TRIPS_TARGET}"

# Memory layer setup
mem = QgsVectorLayer(f"Point?crs=epsg:{cfg.DB_SRID}&index=no", "C1_ALI_NAIVE", "memory")
mem.setCrs(QgsCoordinateReferenceSystem(f"EPSG:{cfg.DB_SRID}"))
pr = mem.dataProvider()
pr.addAttributes([QgsField("id", QVariant.String)])
mem.updateFields()
QgsProject.instance().addMapLayer(mem)

trip_data = []
features_to_add = []
rss_before_load = peak_rss_mb()
t_load = time.perf_counter()

with psycopg2.connect(host=cfg.DB_HOST, port=cfg.DB_PORT, dbname=cfg.DB_NAME,
                      user=cfg.DB_USER, password=cfg.DB_PASSWORD) as conn:
    with conn.cursor(name='c1_ali_naive_stream') as cur:
        cur.itersize = 100
        cur.execute(sql)
        idx = 0
        for tid, wkt, s_ep, e_ep in cur:
            if wkt is None or s_ep is None or e_ep is None:
                continue
            try:
                pyseq = TGeomPointSeq(string=wkt)
            except Exception:
                continue
            idx += 1
            feat = QgsFeature(mem.fields())
            feat.setAttributes([str(tid)])
            geom = QgsGeometry()
            feat.setGeometry(geom)
            features_to_add.append(feat)
            trip_data.append({
                'pyseq':    pyseq,
                's_epoch':  float(s_ep),
                'e_epoch':  float(e_ep),
                'fid':      idx,
                'qgsgeom':  geom,
            })

pr.addFeatures(features_to_add)
mem.updateExtents()
TIME_load = time.perf_counter() - t_load
rss_after_load = peak_rss_mb()
N_TRIPS_LOADED = len(trip_data)  # FIX bug #6 : capture before cleanup
log(f"Load done : {TIME_load:.1f}s ({N_TRIPS_LOADED} trips), RSS {rss_after_load:.0f} MB (delta {rss_after_load - rss_before_load:+.0f})")

# Frame epochs (median window — same as bench_ali_max_optim for comparability)
all_s = np.array([t['s_epoch'] for t in trip_data])
all_e = np.array([t['e_epoch'] for t in trip_data])
median_s = float(np.median(all_s))
median_e = float(np.median(all_e))
# FIX bug #12 : guard against median_s >= median_e
if median_s >= median_e:
    log(f"WARNING: median_start ({median_s}) >= median_end ({median_e}), using global window")
    median_s, median_e = float(all_s.min()), float(all_e.max())
frame_epochs = np.linspace(median_s, median_e, N_FRAMES)
frame_dts = [datetime.fromtimestamp(e, tz=timezone.utc) for e in frame_epochs]

EMPTY_WKB = bytes.fromhex('010100000000000000000000000000000000000000')


# -----------------------------------------------------------------------------
# CONDITION C1: Ali NAIVE (no pre-filter, try/except style Ali, OO PyMEOS)
# -----------------------------------------------------------------------------
def ali_naive_frame(frame_idx):
    """L'algo Ali fidele : aucun pre-filtre temporel, try/except sur tout."""
    dt = frame_dts[frame_idx]
    geometries = {}
    n = 0
    for trip in trip_data:
        try:
            pt = trip['pyseq'].value_at_timestamp(dt)
            if pt is None:  # FIX bug #9 : explicit fail for out-of-range
                raise ValueError("out of range")
            trip['qgsgeom'].fromWkb(pt.wkb)
            n += 1
        except Exception:
            # Ali: silent fail style — geometry stays empty
            trip['qgsgeom'].fromWkb(EMPTY_WKB)
        geometries[trip['fid']] = trip['qgsgeom']
    mem.startEditing()
    pr.changeGeometryValues(geometries)
    mem.commitChanges()
    # FIX bug #4 (review) : clear undo stack to avoid RAM growth on N×N_RUNS commits
    mem.undoStack().clear()
    return n


# -----------------------------------------------------------------------------
# Multi-run benchmark
# -----------------------------------------------------------------------------
log("")
log("=" * 72)
log(f"BENCH C1 (N_RUNS={N_RUNS}, run 1 dropped)")
log("=" * 72)

all_runs = []

for run_id in range(1, N_RUNS + 1):
    gc.collect()
    rss_before_run = peak_rss_mb()
    frame_records = []

    log(f"  RUN {run_id}/{N_RUNS}")
    for frame_idx in range(N_FRAMES):
        t_frame_start = time.perf_counter()
        n_visible = ali_naive_frame(frame_idx)
        t_frame_end = time.perf_counter()
        frame_records.append({
            "run_id": run_id,
            "frame_idx": frame_idx,
            "is_warmup": frame_idx < cfg.FRAME_WARMUP,
            "frame_time_ms": (t_frame_end - t_frame_start) * 1000,
            "visible": n_visible,
        })

    rss_after_run = peak_rss_mb()
    # Adaptive warmup : if N_FRAMES <= FRAME_WARMUP, fall back to drop only first 10%
    effective_warmup = cfg.FRAME_WARMUP if N_FRAMES > cfg.FRAME_WARMUP else max(1, N_FRAMES // 10)
    steady = [f for i, f in enumerate(frame_records) if i >= effective_warmup]
    frame_ms = [f["frame_time_ms"] for f in steady]
    if not frame_ms:
        log(f"  WARNING: no steady frames (N_FRAMES={N_FRAMES}, warmup={effective_warmup}), using all")
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
QgsProject.instance().removeMapLayer(mem.id())
trip_data.clear()
features_to_add.clear()
gc.collect()


# -----------------------------------------------------------------------------
# Export JSON (compatible with bench5_aggregate format)
# -----------------------------------------------------------------------------
summary_path = OUTPUT_DIR / f"bench5_{LABEL}_{cfg.DATASET}_summary.json"
summary = {
    "label":              LABEL,
    "dataset":            cfg.DATASET,
    "table":              cfg.TABLE_NAME,
    "srid":               cfg.DB_SRID,
    "date":               cfg.BENCH_DATE,
    "limit":              N_TRIPS_TARGET,
    "n_trips":            N_TRIPS_LOADED,
    "n_frames_per_run":   N_FRAMES,
    "frame_step_seconds": cfg.FRAME_STEP_SECONDS,
    "warmup_frames":      cfg.FRAME_WARMUP,
    "n_runs":             N_RUNS,
    "load": {
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
log(f"VERDICT C1 ALI_NAIVE (run 1 dropped)")
log("=" * 72)
log(f"  Median frame time : {median_of_medians:.1f} ms ({1000/median_of_medians:.1f} FPS)")
log(f"  RSS peak          : {max(r['rss_after_mb'] for r in runs_kept):.0f} MB")
log(f"  Summary JSON      : {summary_path}")
log("=" * 72)
