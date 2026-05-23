"""
RQ1 - Condition C : MOVE plugin (Maxime Schoemans, github.com/MobilityDB/move)
==============================================================================
Replication FIDELE de l'approche MOVE pour comparaison vs:
  - baseline.py (Ali, row-oriented Python loop)
  - columnar.py (Ayoub, matrix-oriented NumPy slicing)

Approche MOVE:
  - Pre-materialise toutes les trajectoires en LineStringM cote Postgres
    via geometry(<tgeompoint>, false). M = epoch timestamp.
  - Cree une couche QGIS branchee sur la materialized view + une expression
    QgsGeometryGeneratorSymbolLayer 'line_interpolate_point(... @map_end_time)'
    -> ZERO Python par frame, le rendu est natif QGIS.
  - Ce bench mesure le rendu synchrone via QgsMapRendererSequentialJob
    pour pouvoir le chronometrer (le rendu canvas normal est asynchrone).

Le SQL et l'expression sont copies fidelement de move/move_query.py:258-298
et move/move.py:445-447 (commit 7b5b3ed, 2025-03-29).

A executer dans la console Python de QGIS sur la VM, MEME setup que les autres.

Author: Ayoub El Hamri
"""

import sys
import os
import time
import csv
import json
import resource
import statistics
import uuid
from datetime import timedelta
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS_DIR.parent))
import bench_config as cfg

import psycopg2

from qgis.PyQt.QtCore import QSize, QDateTime
from qgis.core import (
    Qgis,
    QgsDataSourceUri,
    QgsDateTimeRange,
    QgsGeometryGeneratorSymbolLayer,
    QgsMapRendererSequentialJob,
    QgsMapSettings,
    QgsMessageLog,
    QgsProject,
    QgsVectorLayer,
    QgsWkbTypes,
)


# -----------------------------------------------------------------------------
# Parametres
# -----------------------------------------------------------------------------
LIMIT    = int(os.environ.get("BENCH_LIMIT", "0"))
N_FRAMES = int(os.environ.get("BENCH_N_FRAMES", str(cfg.N_FRAMES_SYNTHETIC)))
_N_RUNS_OVERRIDE = int(os.environ.get("BENCH_N_RUNS", str(cfg.N_RUNS_PER_SCENARIO)))
RENDER_W = int(os.environ.get("BENCH_RENDER_W", "800"))
RENDER_H = int(os.environ.get("BENCH_RENDER_H", "600"))

cfg.ensure_output_dir()
OUTPUT_DIR = cfg.RQ1_DIR
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

LABEL = "move"


def log(msg):
    QgsMessageLog.logMessage(str(msg), 'RQ1-MOVE', level=Qgis.Info)
    print(f"[MOVE] {msg}")


def peak_rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


# -----------------------------------------------------------------------------
# Phase load : build materialized view (== MoveQuery.create_temporal_view)
# -----------------------------------------------------------------------------
log("=" * 64)
log(f"RQ1-MOVE | dataset={cfg.DATASET} | "
    f"date={cfg.BENCH_DATE or 'static'} | limit={LIMIT or 'all'}")
log("=" * 64)
cfg.print_dataset_banner()

VIEW_NAME = f"move_bench_tpoint_{uuid.uuid4().hex[:8]}"

inner_sql = cfg.trip_selection_sql(LIMIT)
# inner_sql renvoie 4 colonnes (id, tpoint, t_start, t_end) ; temp_2 ne reference
# que <tpoint> donc les autres sont juste portees sans cout. Strictement la meme
# selection deterministe de trips que baseline.py et columnar.py.
#
# IMPORTANT : on N'UTILISE PAS shiftTime() (que MOVE utilise en mode live pour
# decaler les trips au "today"). Pour le bench, on garde les timestamps reels
# afin de pouvoir definir la fenetre des frames sur la plage reelle des trips
# (cf. timestamps[] plus bas).
matview_sql = f"""
CREATE MATERIALIZED VIEW {VIEW_NAME} AS (
    WITH temp_1 AS (
        {inner_sql}
    ), temp_2 AS (
        SELECT
            geometry({cfg.TPOINT_COLUMN}, false) AS geom
        FROM temp_1
    )
    SELECT
        row_number() OVER () AS id,
        geom,
        to_timestamp(st_m(st_startpoint(geom))) AT TIME ZONE 'gmt' AS start_t,
        to_timestamp(st_m(st_endpoint(geom)))   AT TIME ZONE 'gmt' AS end_t
    FROM temp_2
)
"""

t_load_start = time.perf_counter()
rss_before_load = peak_rss_mb()

with psycopg2.connect(
        host=cfg.DB_HOST, port=cfg.DB_PORT,
        dbname=cfg.DB_NAME, user=cfg.DB_USER, password=cfg.DB_PASSWORD,
) as conn:
    with conn.cursor() as cur:
        cur.execute(f"DROP MATERIALIZED VIEW IF EXISTS {VIEW_NAME}")

        t_matview_start = time.perf_counter()
        cur.execute(matview_sql)
        TIME_matview = time.perf_counter() - t_matview_start

        # 3 indexes comme MOVE (move_query.py:194-196)
        t_idx_start = time.perf_counter()
        cur.execute(f"CREATE INDEX {VIEW_NAME}_startt_idx ON {VIEW_NAME} (start_t)")
        cur.execute(f"CREATE INDEX {VIEW_NAME}_endt_idx   ON {VIEW_NAME} (end_t)")
        cur.execute(f"CREATE INDEX {VIEW_NAME}_geom_idx   ON {VIEW_NAME} USING spgist (geom)")
        TIME_indexes = time.perf_counter() - t_idx_start

        t_analyze_start = time.perf_counter()
        cur.execute(f"ANALYZE {VIEW_NAME}")
        TIME_analyze = time.perf_counter() - t_analyze_start

        cur.execute(f"SELECT st_srid(geom) FROM {VIEW_NAME} WHERE geom IS NOT NULL LIMIT 1")
        srid_row = cur.fetchone()
        srid = srid_row[0] if srid_row else cfg.DB_SRID

        cur.execute(f"SELECT count(*) FROM {VIEW_NAME}")
        n_features = cur.fetchone()[0]

        cur.execute(f"SELECT pg_relation_size('{VIEW_NAME}')")
        matview_bytes = int(cur.fetchone()[0])

        cur.execute(f"""
            SELECT COALESCE(sum(pg_relation_size(c.oid)), 0)
            FROM pg_class c
            JOIN pg_index i ON i.indexrelid = c.oid
            WHERE i.indrelid = '{VIEW_NAME}'::regclass
        """)
        # SUM(bigint) renvoie numeric -> Decimal en Python ; on cast en int.
        index_bytes = int(cur.fetchone()[0])

        cur.execute(f"SELECT min(start_t), max(end_t) FROM {VIEW_NAME}")
        global_start, global_end = cur.fetchone()

        conn.commit()

t_load_end = time.perf_counter()
rss_after_load = peak_rss_mb()
TIME_load = t_load_end - t_load_start

log(f"  -> matview {VIEW_NAME}")
log(f"     features        : {n_features}")
log(f"     matview size    : {matview_bytes/1e6:.1f} MB")
log(f"     indexes size    : {index_bytes/1e6:.1f} MB")
log(f"  -> matview create  : {TIME_matview:.3f}s")
log(f"  -> indexes create  : {TIME_indexes:.3f}s")
log(f"  -> analyze         : {TIME_analyze:.3f}s")
log(f"  -> total load      : {TIME_load:.3f}s")
log(f"  -> RSS Python      : {rss_after_load:.1f} MB")
log(f"  -> trips window    : {global_start} -> {global_end}")


# -----------------------------------------------------------------------------
# Phase setup : couche QGIS branchee sur la matview + symbol expression MOVE
# (== MoveGeomTask + add_tpoint_layer dans move/move.py)
# -----------------------------------------------------------------------------
t_layer_start = time.perf_counter()

uri = QgsDataSourceUri()
uri.setConnection(cfg.DB_HOST, str(cfg.DB_PORT), cfg.DB_NAME,
                  cfg.DB_USER, cfg.DB_PASSWORD,
                  QgsDataSourceUri.SslDisable)
uri.setDataSource("public", VIEW_NAME, "geom", "", "id")
uri.setSrid(str(srid))
uri.setWkbType(QgsWkbTypes.LineStringM)

vlayer = QgsVectorLayer(uri.uri(), "RQ1_MOVE_native", "postgres")
if not vlayer.isValid():
    raise RuntimeError(f"Layer failed to load from view {VIEW_NAME}")

QgsProject.instance().addMapLayer(vlayer)
vlayer.temporalProperties().setIsActive(True)

# Expression copiee de move/move.py:445-447
geom_expr = (
    'line_interpolate_point(\n'
    '  $geometry,\n'
    '  1.0 * (\n'
    '    ( epoch(@map_end_time)/1000 )\n'
    '    - m(start_point($geometry))\n'
    '  ) / (\n'
    '    m(end_point($geometry))\n'
    '    - m(start_point($geometry))\n'
    '  )\n'
    '  * length($geometry)\n'
    ') '
)
point_layer = QgsGeometryGeneratorSymbolLayer.create({
    'SymbolType':       'Marker',
    'geometryModifier': geom_expr,
})
vlayer.renderer().symbol().changeSymbolLayer(0, point_layer)
vlayer.triggerRepaint()

TIME_layer_setup = time.perf_counter() - t_layer_start
log(f"  -> layer setup     : {TIME_layer_setup*1000:.1f}ms")


# -----------------------------------------------------------------------------
# Phase animation : rendu synchrone par frame via QgsMapRendererSequentialJob
# -----------------------------------------------------------------------------
# On construit un QgsMapSettings independant du canvas pour controler
# precisement la taille du rendu (sinon -> depend de la fenetre QGIS de
# l'utilisateur, non reproductible). Pour chaque frame on fixe le temporal
# range, on lance le job synchrone, on chronometre.
ms = QgsMapSettings()
ms.setOutputSize(QSize(RENDER_W, RENDER_H))
ms.setLayers([vlayer])
ms.setDestinationCrs(vlayer.crs())
ms.setExtent(vlayer.extent())
ms.setIsTemporal(True)

# Meme strategie de fenetre que baseline.py : centree sur le milieu de la
# plage des trips, par pas de FRAME_STEP_SECONDS.
total_seconds = (global_end - global_start).total_seconds()
midpoint = global_start + timedelta(seconds=total_seconds / 2)
timestamps = [midpoint + timedelta(seconds=i * cfg.FRAME_STEP_SECONDS)
              for i in range(N_FRAMES)]

log("")
log(f"  Frames        : {N_FRAMES} a {cfg.FRAME_STEP_SECONDS}s d'intervalle")
log(f"  Render size   : {RENDER_W}x{RENDER_H}")
log(f"  Warm-up       : {cfg.FRAME_WARMUP} premieres frames jetees a l'analyse")
log(f"  Runs          : {_N_RUNS_OVERRIDE} (run 1 jete)")


all_runs = []

for run_id in range(1, _N_RUNS_OVERRIDE + 1):
    log("")
    log("-" * 64)
    log(f"RUN {run_id}/{_N_RUNS_OVERRIDE}")
    log("-" * 64)

    rss_before_run = peak_rss_mb()
    frame_records  = []

    for frame_idx, ts in enumerate(timestamps):
        # Fixe le temporal range de la map ; l'expression
        # line_interpolate_point(... @map_end_time ...) lit depuis la.
        qt_start = QDateTime(ts)
        qt_end   = QDateTime(ts + timedelta(seconds=cfg.FRAME_STEP_SECONDS))
        ms.setTemporalRange(QgsDateTimeRange(qt_start, qt_end))

        t_frame_start = time.perf_counter()
        job = QgsMapRendererSequentialJob(ms)
        job.start()
        job.waitForFinished()
        t_frame_end = time.perf_counter()

        frame_time_ms = (t_frame_end - t_frame_start) * 1000

        frame_records.append({
            "run_id":         run_id,
            "frame_idx":      frame_idx,
            "is_warmup":      frame_idx < cfg.FRAME_WARMUP,
            "frame_time_ms":  frame_time_ms,
            "render_time_ms": frame_time_ms,
        })

        if frame_idx % 20 == 0:
            fps = 1000.0 / frame_time_ms if frame_time_ms > 0 else 0
            log(f"  frame {frame_idx:3d} | render={frame_time_ms:.1f}ms ({fps:.1f} FPS)")

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
        f"p5={run_summary['p5_fps']:.2f} p95={run_summary['p95_fps']:.2f} | "
        f"RSS={rss_after_run:.0f} MB")


# -----------------------------------------------------------------------------
# Cleanup matview (BENCH_KEEP_MATVIEW=1 pour la garder)
# -----------------------------------------------------------------------------
# Ordre important : on enleve la couche QGIS AVANT de droper la matview, sinon
# le canvas continue de rafraichir une couche dont le backend a disparu et
# QGIS spamme des WARNING "relation does not exist" en boucle.
if not os.environ.get("BENCH_KEEP_MATVIEW"):
    try:
        QgsProject.instance().removeMapLayer(vlayer.id())
    except Exception as e:
        log(f"  -> WARNING: layer removal failed: {e}")
    try:
        with psycopg2.connect(
                host=cfg.DB_HOST, port=cfg.DB_PORT,
                dbname=cfg.DB_NAME, user=cfg.DB_USER, password=cfg.DB_PASSWORD,
        ) as conn:
            with conn.cursor() as cur:
                cur.execute(f"DROP MATERIALIZED VIEW IF EXISTS {VIEW_NAME}")
                conn.commit()
        log(f"  -> matview {VIEW_NAME} dropped (BENCH_KEEP_MATVIEW=1 pour la garder)")
    except Exception as e:
        log(f"  -> WARNING: cleanup failed: {e}")


# -----------------------------------------------------------------------------
# Export
# -----------------------------------------------------------------------------
csv_path = OUTPUT_DIR / f"{LABEL}_limit{LIMIT}.csv"
with open(csv_path, "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["run_id", "frame_idx", "is_warmup",
                "frame_time_ms", "render_time_ms"])
    for run in all_runs:
        for f_rec in run["frames"]:
            w.writerow([f_rec["run_id"], f_rec["frame_idx"], int(f_rec["is_warmup"]),
                        f_rec["frame_time_ms"], f_rec["render_time_ms"]])

summary_path = OUTPUT_DIR / f"{LABEL}_limit{LIMIT}_summary.json"
summary = {
    "label":              LABEL,
    "dataset":            cfg.DATASET,
    "table":              cfg.TABLE_NAME,
    "srid":               cfg.DB_SRID,
    "date":               cfg.BENCH_DATE,
    "limit":              LIMIT,
    "n_trips":            n_features,
    "n_frames_per_run":   N_FRAMES,
    "frame_step_seconds": cfg.FRAME_STEP_SECONDS,
    "warmup_frames":      cfg.FRAME_WARMUP,
    "n_runs":             _N_RUNS_OVERRIDE,
    "render_size":        [RENDER_W, RENDER_H],
    "matview": {
        "name":                VIEW_NAME,
        "matview_size_mb":     matview_bytes / 1e6,
        "indexes_size_mb":     index_bytes / 1e6,
        "n_features":          n_features,
        "matview_seconds":     TIME_matview,
        "indexes_seconds":     TIME_indexes,
        "analyze_seconds":     TIME_analyze,
        "layer_setup_seconds": TIME_layer_setup,
    },
    "load": {
        "total_seconds":  TIME_load,
        "rss_before_mb":  rss_before_load,
        "rss_after_mb":   rss_after_load,
    },
    "runs": [r["summary"] for r in all_runs],
}
with open(summary_path, "w") as f:
    json.dump(summary, f, indent=2, default=str)


# -----------------------------------------------------------------------------
# Rapport (run 1 jete)
# -----------------------------------------------------------------------------
runs_kept = [r["summary"] for r in all_runs[1:]]
median_of_medians = statistics.median([r["median_fps"] for r in runs_kept])
median_p5  = statistics.median([r["p5_fps"]  for r in runs_kept])
median_p95 = statistics.median([r["p95_fps"] for r in runs_kept])

log("")
log("=" * 64)
log(f"RESULTATS RQ1-MOVE (run 1 jete, N={len(runs_kept)} runs gardes)")
log("=" * 64)
log(f"  Trips animes        : {n_features}")
log(f"  matview disque      : {matview_bytes/1e6:.1f} MB + indexes {index_bytes/1e6:.1f} MB")
log(f"  Load total          : {TIME_load:.2f}s")
log(f"  FPS median (median) : {median_of_medians:.2f}")
log(f"  FPS p5    (median)  : {median_p5:.2f}")
log(f"  FPS p95   (median)  : {median_p95:.2f}")
log(f"  RSS peak Python     : {max(r['rss_after_mb'] for r in runs_kept):.0f} MB")
log("")
log(f"  CSV     : {csv_path}")
log(f"  Summary : {summary_path}")
log("=" * 64)
log(f"  Compare maintenant vs baseline_limit{LIMIT}_summary.json")
log(f"  et columnar_limit{LIMIT}_summary.json")
