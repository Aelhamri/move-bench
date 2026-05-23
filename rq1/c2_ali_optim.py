"""
RQ1-FINAL — Condition C2 : Ali OPTIMIZED par Ayoub
====================================================
Pattern strict : reste dans paradigme PyMEOS OO Interactive Mode (loop par
feature par frame), MAIS optimisations "low-hanging fruit" qu'un dev qui
profile son code ferait :

  - pymeos_cffi raw (bypass PyMEOS OO wrapper)
  - geo_as_ewkb buffer direct (pas de Shapely roundtrip)
  - provider direct (skip startEditing / commitChanges undo stack)
  - triggerRepaint (au lieu d'edit buffer)

C'est exactement la condition L3b_dp de bench_ali_max_optim.py, isolée et
avec dump JSON conforme bench5_aggregate format.

Author: Ayoub El Hamri
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
import pymeos_cffi as pc
from pymeos_cffi.functions import _lib, _ffi

from qgis.PyQt.QtCore import QVariant
from qgis.core import (
    Qgis, QgsCoordinateReferenceSystem, QgsFeature, QgsField,
    QgsGeometry, QgsMessageLog, QgsProject, QgsVectorLayer,
)


# ENV vars (cohérent avec autres bench5_*)
N_TRIPS_TARGET = int(os.environ.get("BENCH_N_TRIPS",
                                     os.environ.get("BENCH_LIMIT", "5000")))
N_FRAMES = int(os.environ.get("BENCH_N_FRAMES", "60"))
N_RUNS = int(os.environ.get("BENCH_N_RUNS", "5"))
LABEL = "c2_ali_optim"

cfg.ensure_output_dir()
OUTPUT_DIR = cfg.RQ1_DIR
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def log(msg):
    QgsMessageLog.logMessage(str(msg), 'BENCH5-C2', level=Qgis.Info)
    print(f"[C2-ALI-OPTIM] {msg}")


def peak_rss_mb():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


# -----------------------------------------------------------------------------
log("=" * 72)
log(f"C2 ALI_OPTIM | dataset={cfg.DATASET} | N={N_TRIPS_TARGET} | T={N_FRAMES} | runs={N_RUNS}")
log("=" * 72)
cfg.print_dataset_banner()

pymeos_initialize()

# Streaming load (identical to C1 for fair comparison)
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

mem = QgsVectorLayer(f"Point?crs=epsg:{cfg.DB_SRID}&index=no", "C2_ALI_OPTIM", "memory")
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
    with conn.cursor(name='c2_ali_optim_stream') as cur:
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
            inner = pyseq._inner
            idx += 1
            feat = QgsFeature(mem.fields())
            feat.setAttributes([str(tid)])
            geom = QgsGeometry()
            feat.setGeometry(geom)
            features_to_add.append(feat)
            trip_data.append({
                'inner':    inner,
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
log(f"Load done : {TIME_load:.1f}s ({N_TRIPS_LOADED} trips), RSS {rss_after_load:.0f} MB")

# Frame epochs (median window — same as C1 for direct comparison)
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
PG_EPOCH = datetime(2000, 1, 1, tzinfo=timezone.utc)
frame_ts_int = [int((dt - PG_EPOCH).total_seconds() * 1_000_000) for dt in frame_dts]

EMPTY_WKB = bytes.fromhex('010100000000000000000000000000000000000000')


# -----------------------------------------------------------------------------
# CONDITION C2: Ali OPTIMIZED (L3b_dp from bench_ali_max_optim)
# -----------------------------------------------------------------------------
def ali_optim_frame(frame_idx):
    """L3b raw EWKB + provider direct (skip edit buffer)."""
    ts_int = frame_ts_int[frame_idx]
    fe = frame_epochs[frame_idx]
    fn = pc.tpoint_value_at_timestamptz
    ewkb_fn = _lib.geo_as_ewkb
    size_p = _ffi.new("size_t *")
    geometries = {}
    n = 0
    for trip in trip_data:
        if fe < trip['s_epoch'] or fe > trip['e_epoch']:
            trip['qgsgeom'].fromWkb(EMPTY_WKB)
            geometries[trip['fid']] = trip['qgsgeom']
            continue
        res = fn(trip['inner'], ts_int, True)
        if res is None or res[0] is None:
            trip['qgsgeom'].fromWkb(EMPTY_WKB)
            geometries[trip['fid']] = trip['qgsgeom']
            continue
        buf_ptr = ewkb_fn(_ffi.cast("const GSERIALIZED *", res[0]),
                          b"NDR", size_p)
        wkb = bytes(_ffi.buffer(buf_ptr, size_p[0]))
        trip['qgsgeom'].fromWkb(wkb)
        n += 1
        geometries[trip['fid']] = trip['qgsgeom']
    pr.changeGeometryValues(geometries)
    mem.triggerRepaint()
    return n


# -----------------------------------------------------------------------------
# Multi-run benchmark
# -----------------------------------------------------------------------------
log("")
log("=" * 72)
log(f"BENCH C2 (N_RUNS={N_RUNS}, run 1 dropped)")
log("=" * 72)

all_runs = []

for run_id in range(1, N_RUNS + 1):
    gc.collect()
    rss_before_run = peak_rss_mb()
    frame_records = []

    log(f"  RUN {run_id}/{N_RUNS}")
    for frame_idx in range(N_FRAMES):
        t_frame_start = time.perf_counter()
        n_visible = ali_optim_frame(frame_idx)
        t_frame_end = time.perf_counter()
        frame_records.append({
            "run_id": run_id,
            "frame_idx": frame_idx,
            "is_warmup": frame_idx < cfg.FRAME_WARMUP,
            "frame_time_ms": float((t_frame_end - t_frame_start) * 1000),
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
        "median_frame_ms": float(statistics.median(frame_ms)),
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
# Export JSON (FIX bug #3 : C2 produit maintenant un JSON conforme)
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
log(f"VERDICT C2 ALI_OPTIM (run 1 dropped)")
log("=" * 72)
log(f"  Median frame time : {median_of_medians:.1f} ms ({1000/median_of_medians:.1f} FPS)")
log(f"  Summary JSON      : {summary_path}")
log("=" * 72)
