"""
RQ2 — Bench QGIS contender = C6 (MOVE upgrade, memory layer cache)
====================================================================

Adapte la condition C6 du RQ1 (move_inmemory.py) en mode wall-clock 60s
pour comparaison directe avec le bench Flask (qui tourne aussi en wall-clock).

Différences vs RQ1/move_inmemory.py :
  - Boucle frame en wall-clock cfg.STEADY_STATE_SECONDS (= 60s) au lieu
    de N_FRAMES fixe (= 120). Aligne la quantité de travail mesurée
    sur le bench Flask.
  - Dropping warmup : 5s wall-clock au début (pas N premières frames).
  - PID file écrit pour que resource_sampler.py s'attache.
  - Sortie au format orchestrator.py : CSV frames + JSON load.

Pipeline (identique RQ1 C6) :
  1. Build matview Postgres (5000 trips intra-jour selon cfg)
  2. Charge features dans QgsVectorLayer("postgres") éphémère
  3. Materialize en QgsVectorLayer("memory") (= la nouveauté du PR)
  4. Detach postgres, garde memory
  5. Apply QgsGeometryGeneratorSymbolLayer + expression line_interpolate_point
     (IDENTIQUE à MOVE upstream)
  6. Boucle wall-clock : QgsMapRendererSequentialJob synchrone par frame
     = équivalent parallel rendering OFF (rigueur RQ1)

Usage :
  Headless via orchestrator.py :
    qgis --code "exec(open('bench6_qgis_c6.py').read())"
  avec env vars BENCH_LIMIT, BENCH_RUN_ID.

Rigueur RQ1 répliquée :
  - Drop run 1 (méthode bench5)
  - Bootstrap CI95 fait à l'aggregate (cf bench6_aggregate.py)
  - Parallel rendering OFF (= QgsMapRendererSequentialJob synchrone)
  - Pas de canvas → pas besoin de wait_for_complete_render
    (le SequentialJob bloque jusqu'à completion réelle du pixel buffer)

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

try:
    THIS_DIR = Path(__file__).resolve().parent
except NameError:
    # When launched via `qgis --code <script>`, __file__ is not set.
    # Export BENCH_SCRIPT_DIR=/path/to/move-bench/rq2 before running.
    THIS_DIR = Path(os.environ['BENCH_SCRIPT_DIR'])
sys.path.insert(0, str(THIS_DIR))
sys.path.insert(0, str(THIS_DIR.parent))   # for bench_config
import bench_config as cfg

import psycopg2

from qgis.PyQt.QtCore import QSize, QDateTime
from qgis.PyQt.QtCore import QVariant

from qgis.core import (
    Qgis,
    QgsDataSourceUri,
    QgsDateTimeRange,
    QgsFeature,
    QgsField,
    QgsGeometry,
    QgsGeometryGeneratorSymbolLayer,
    QgsMapRendererSequentialJob,
    QgsMapSettings,
    QgsMessageLog,
    QgsProject,
    QgsVectorLayer,
    QgsVectorLayerTemporalProperties,
    QgsWkbTypes,
)


# -----------------------------------------------------------------------------
# Paramètres
# -----------------------------------------------------------------------------
LIMIT    = int(os.environ.get("BENCH_LIMIT", "1000"))
RUN_ID   = int(os.environ.get("BENCH_RUN_ID", "1"))
RENDER_W = int(os.environ.get("BENCH_RENDER_W", "1920"))
RENDER_H = int(os.environ.get("BENCH_RENDER_H", "1080"))

cfg.ensure_output_dir()
OUTPUT_DIR = cfg.RQ2_DIR
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
LABEL = "qgis"   # même label que l'ancien qgis_bench.py → analyze.py compatible


def log(msg):
    QgsMessageLog.logMessage(str(msg), 'RQ2-QGIS-C6', level=Qgis.Info)
    print(f"[QGIS-C6] {msg}")


def peak_rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


# Écrire le PID pour resource_sampler
pid_file = OUTPUT_DIR / "qgis.pid"
pid_file.write_text(str(os.getpid()))
log(f"PID {os.getpid()} écrit dans {pid_file}")


# -----------------------------------------------------------------------------
# Phase load : matview Postgres (= MoveQuery.create_temporal_view)
# -----------------------------------------------------------------------------
log("=" * 64)
log(f"RQ2-QGIS-C6 (memory layer) | dataset={cfg.DATASET} | "
    f"limit={LIMIT or 'all'} | run={RUN_ID}")
log("=" * 64)
cfg.print_dataset_banner()

VIEW_NAME = f"bench6_c6_{uuid.uuid4().hex[:8]}"

inner_sql = cfg.trip_selection_sql(LIMIT)
matview_sql = f"""
CREATE MATERIALIZED VIEW {VIEW_NAME} AS (
    WITH temp_1 AS (
        {inner_sql}
    ), temp_2 AS (
        SELECT geometry({cfg.TPOINT_COLUMN}, false) AS geom
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
        index_bytes = int(cur.fetchone()[0])

        cur.execute(f"SELECT min(start_t), max(end_t) FROM {VIEW_NAME}")
        global_start, global_end = cur.fetchone()

        conn.commit()

t_load_end = time.perf_counter()
TIME_load_matview = t_load_end - t_load_start
log(f"  matview {VIEW_NAME} | features={n_features} | "
    f"size={matview_bytes/1e6:.1f}MB indexes={index_bytes/1e6:.1f}MB")
log(f"  matview build={TIME_matview:.3f}s indexes={TIME_indexes:.3f}s analyze={TIME_analyze:.3f}s")
log(f"  trips window  : {global_start} -> {global_end}")


# -----------------------------------------------------------------------------
# Phase setup A : couche postgres temporaire pour aspirer les features
# -----------------------------------------------------------------------------
t_pg_layer_start = time.perf_counter()
uri = QgsDataSourceUri()
uri.setConnection(cfg.DB_HOST, str(cfg.DB_PORT), cfg.DB_NAME,
                  cfg.DB_USER, cfg.DB_PASSWORD,
                  QgsDataSourceUri.SslDisable)
uri.setDataSource("public", VIEW_NAME, "geom", "", "id")
uri.setSrid(str(srid))
uri.setWkbType(QgsWkbTypes.LineStringM)

pg_layer = QgsVectorLayer(uri.uri(), "RQ2_C6_pg_temp", "postgres")
if not pg_layer.isValid():
    raise RuntimeError(f"Layer failed to load from view {VIEW_NAME}")
TIME_pg_layer = time.perf_counter() - t_pg_layer_start


# -----------------------------------------------------------------------------
# Phase setup B : MATERIALIZE en couche memory (la nouveauté du PR C6)
# -----------------------------------------------------------------------------
t_mat_start = time.perf_counter()

mem_layer = QgsVectorLayer(
    f"LineStringM?crs=epsg:{srid}",
    "RQ2_C6_memory",
    "memory",
)
mem_pr = mem_layer.dataProvider()
mem_pr.addAttributes([
    QgsField("id",      QVariant.LongLong),
    QgsField("start_t", QVariant.DateTime),
    QgsField("end_t",   QVariant.DateTime),
])
mem_layer.updateFields()

mem_features = []
for pg_feat in pg_layer.getFeatures():
    f = QgsFeature(mem_layer.fields())
    f.setGeometry(QgsGeometry(pg_feat.geometry()))
    f.setAttributes([
        pg_feat["id"],
        pg_feat["start_t"],
        pg_feat["end_t"],
    ])
    mem_features.append(f)

mem_pr.addFeatures(mem_features)
mem_layer.updateExtents()

TIME_materialize = time.perf_counter() - t_mat_start
log(f"  materialize : {TIME_materialize:.3f}s ({len(mem_features)} features → memory)")

# Détache le postgres layer (on n'en a plus besoin)
del pg_layer


# -----------------------------------------------------------------------------
# Phase setup C : add to project + temporal + symbol expression (= C5/MOVE)
# -----------------------------------------------------------------------------
QgsProject.instance().addMapLayer(mem_layer)

tp = mem_layer.temporalProperties()
tp.setIsActive(True)
tp.setMode(QgsVectorLayerTemporalProperties.ModeFeatureDateTimeStartAndEndFromFields)
tp.setStartField("start_t")
tp.setEndField("end_t")

# Expression IDENTIQUE à MOVE upstream
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
mem_layer.renderer().symbol().changeSymbolLayer(0, point_layer)
mem_layer.triggerRepaint()

TIME_layer_setup = time.perf_counter() - t_pg_layer_start
rss_after_load = peak_rss_mb()

TIME_load_total = TIME_load_matview + TIME_layer_setup
log(f"  load total (matview + materialize + symbol) : {TIME_load_total:.3f}s")
log(f"  RSS post-load : {rss_after_load:.0f} MB (Δ {rss_after_load - rss_before_load:+.0f})")


# -----------------------------------------------------------------------------
# Setup map settings (= équivalent canvas headless avec parallel OFF)
# -----------------------------------------------------------------------------
ms = QgsMapSettings()
ms.setOutputSize(QSize(RENDER_W, RENDER_H))
ms.setLayers([mem_layer])
ms.setDestinationCrs(mem_layer.crs())
ms.setExtent(mem_layer.extent())
ms.setIsTemporal(True)


# -----------------------------------------------------------------------------
# Phase animation : wall-clock cfg.STEADY_STATE_SECONDS, drop 5s warmup
# -----------------------------------------------------------------------------
total_seconds = (global_end - global_start).total_seconds()
midpoint = global_start + timedelta(seconds=total_seconds / 2)

log("")
log("=" * 64)
log(f"ANIMATION ({cfg.STEADY_STATE_SECONDS}s wall-clock, 5s warmup dropped)")
log("=" * 64)

frame_records = []
rss_before_run = peak_rss_mb()

# v2 (post-3-agents review) : désactiver le GC autour de la boucle frame
# pour éviter qu'un cycle Python collect() fire en plein milieu et fausse
# le frame_time. On force un collect avant t_run_start pour partir clean.
import gc
gc.collect()
gc_was_enabled = gc.isenabled()
gc.disable()

# Flag les frames "wraparound" : quand offset_s repasse à zéro, le cache
# bbox QGIS est invalidé et la frame est atypique. On les marque pour
# pouvoir les exclure de l'aggregate.
_prev_offset = -1.0

t_run_start = time.perf_counter()
t_first_frame = None
frame_idx = 0

deadline = t_run_start + cfg.STEADY_STATE_SECONDS

while time.perf_counter() < deadline:
    # Avancer le temporal cursor d'un FRAME_STEP_SECONDS à chaque frame.
    # On boucle dans la fenêtre des trips (modulo).
    offset_s = (frame_idx * cfg.FRAME_STEP_SECONDS) % max(1.0, total_seconds)
    is_wrap = offset_s < _prev_offset
    _prev_offset = offset_s
    ts = global_start + timedelta(seconds=offset_s)

    qt_start = QDateTime(ts)
    qt_end   = QDateTime(ts + timedelta(seconds=cfg.FRAME_STEP_SECONDS))
    ms.setTemporalRange(QgsDateTimeRange(qt_start, qt_end))

    t_frame_start = time.perf_counter()
    job = QgsMapRendererSequentialJob(ms)
    job.start()
    job.waitForFinished()      # bloque jusqu'à completion pixel buffer
    t_frame_end = time.perf_counter()

    frame_time_ms = (t_frame_end - t_frame_start) * 1000

    if t_first_frame is None:
        t_first_frame = frame_time_ms

    elapsed = t_frame_end - t_run_start

    frame_records.append({
        "run_id":         RUN_ID,
        "frame_idx":      frame_idx,
        "elapsed_s":      elapsed,
        "is_warmup":      elapsed < 5.0,         # 5s warmup wall-clock
        "is_wrap":        is_wrap,               # frame de bouclage temporel
        "frame_time_ms":  frame_time_ms,
        "render_time_ms": frame_time_ms,
        "visible":        n_features,            # toutes visibles (memory layer)
    })

    frame_idx += 1

t_run_end = time.perf_counter()
TIME_run_total = t_run_end - t_run_start
rss_after_run = peak_rss_mb()

# Restore GC
if gc_was_enabled:
    gc.enable()

log(f"  -> {len(frame_records)} frames en {TIME_run_total:.1f}s")
log(f"  -> first_frame    : {t_first_frame:.1f}ms")
log(f"  -> RSS post-run   : {rss_after_run:.0f} MB")


# -----------------------------------------------------------------------------
# Cleanup matview (BENCH_KEEP_MATVIEW=1 pour la garder)
# -----------------------------------------------------------------------------
if not os.environ.get("BENCH_KEEP_MATVIEW"):
    try:
        with psycopg2.connect(
                host=cfg.DB_HOST, port=cfg.DB_PORT,
                dbname=cfg.DB_NAME, user=cfg.DB_USER, password=cfg.DB_PASSWORD,
        ) as conn:
            with conn.cursor() as cur:
                cur.execute(f"DROP MATERIALIZED VIEW IF EXISTS {VIEW_NAME}")
                conn.commit()
        log(f"  matview {VIEW_NAME} dropped")
    except Exception as e:
        log(f"  WARNING: cleanup failed: {e}")


# -----------------------------------------------------------------------------
# Stats par-run (résumé local — bootstrap CI95 cross-run fait à l'aggregate)
# -----------------------------------------------------------------------------
# v2 (post-3-agents review) : on exclut aussi les frames wraparound de la
# distribution steady-state (cache bbox invalidé → atypique).
steady = [f for f in frame_records if not f["is_warmup"] and not f["is_wrap"]]
frame_ms = [f["frame_time_ms"] for f in steady]

# Métrique principale = FPS soutenu (= n_frames_steady / duration_steady),
# symétrique avec la cadence rAF côté Flask.
if steady:
    duration_steady = steady[-1]["elapsed_s"] - steady[0]["elapsed_s"]
    fps_sustained   = len(steady) / duration_steady if duration_steady > 0 else 0
else:
    duration_steady = 0
    fps_sustained   = 0

if frame_ms:
    median_ms  = statistics.median(frame_ms)
    median_fps = 1000.0 / median_ms
    if len(frame_ms) >= 20:
        p5_ms  = statistics.quantiles(frame_ms, n=20)[0]
        p50_ms = statistics.median(frame_ms)
        p95_ms = statistics.quantiles(frame_ms, n=20)[18]
        p99_ms = statistics.quantiles(frame_ms, n=100)[98] if len(frame_ms) >= 100 else p95_ms
        p5_fps = 1000.0 / p95_ms
        p95_fps = 1000.0 / p5_ms
    else:
        p5_ms = p50_ms = p95_ms = p99_ms = median_ms
        p5_fps = p95_fps = median_fps
else:
    median_ms = median_fps = p5_fps = p95_fps = 0.0
    p5_ms = p50_ms = p95_ms = p99_ms = 0.0


# -----------------------------------------------------------------------------
# Export CSV + JSON (compat orchestrator.py / analyze.py)
# -----------------------------------------------------------------------------
csv_path = OUTPUT_DIR / f"{LABEL}_limit{LIMIT}_run{RUN_ID}.csv"
with open(csv_path, "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["run_id", "frame_idx", "elapsed_s", "is_warmup", "is_wrap",
                "frame_time_ms", "render_time_ms", "visible"])
    for r in frame_records:
        w.writerow([r["run_id"], r["frame_idx"], r["elapsed_s"],
                    int(r["is_warmup"]), int(r["is_wrap"]),
                    r["frame_time_ms"], r["render_time_ms"], r["visible"]])

load_path = OUTPUT_DIR / f"{LABEL}_limit{LIMIT}_run{RUN_ID}_load.json"
with open(load_path, "w") as f:
    json.dump({
        "label":               LABEL,
        "condition":           "C6_memory_layer",
        "dataset":             cfg.DATASET,
        "date":                cfg.BENCH_DATE,
        "limit":               LIMIT,
        "run_id":              RUN_ID,
        "n_trips":             n_features,
        "n_frames_run":        len(frame_records),
        "render_size":         [RENDER_W, RENDER_H],
        "matview": {
            "name":              VIEW_NAME,
            "matview_size_mb":   matview_bytes / 1e6,
            "indexes_size_mb":   index_bytes / 1e6,
            "matview_seconds":   TIME_matview,
            "indexes_seconds":   TIME_indexes,
            "analyze_seconds":   TIME_analyze,
        },
        "load": {
            "matview_seconds":      TIME_load_matview,
            "layer_setup_seconds":  TIME_layer_setup,
            "materialize_seconds":  TIME_materialize,
            "total_seconds":        TIME_load_total,
            "rss_before_mb":        rss_before_load,
            "rss_after_mb":         rss_after_load,
        },
        "run": {
            "duration_seconds":   TIME_run_total,
            "duration_steady":    duration_steady,
            "first_frame_ms":     t_first_frame or 0,
            "n_frames":           len(frame_records),
            "n_frames_steady":    len(steady),
            "n_frames_wrap":      sum(1 for f in frame_records if f["is_wrap"]),
            # Métrique PRINCIPALE (post-3-agents review) : FPS soutenu wall-clock,
            # symétrique avec cadence rAF côté Flask.
            "fps_sustained":      fps_sustained,
            # Métriques secondaires : distribution des frame_time_ms steady.
            "median_frame_ms":    median_ms,
            "p5_frame_ms":        p5_ms,
            "p50_frame_ms":       p50_ms,
            "p95_frame_ms":       p95_ms,
            "p99_frame_ms":       p99_ms,
            "median_fps":         median_fps,
            "p5_fps":             p5_fps,
            "p95_fps":            p95_fps,
            "rss_before_mb":      rss_before_run,
            "rss_after_mb":       rss_after_run,
        },
    }, f, indent=2, default=str)


log("")
log("=" * 64)
log(f"RÉSULTATS QGIS-C6 run {RUN_ID}")
log("=" * 64)
log(f"  Trips         : {n_features}")
log(f"  Frames total  : {len(frame_records)} en {TIME_run_total:.1f}s")
log(f"  Steady (no warmup, no wrap) : {len(steady)} en {duration_steady:.1f}s")
log(f"  FPS soutenu   : {fps_sustained:.2f}  ← métrique principale (sym. Flask)")
log(f"  FPS median    : {median_fps:.2f}  (p5={p5_fps:.2f} p95={p95_fps:.2f})")
log(f"  Frame ms      : p5={p5_ms:.2f} p50={p50_ms:.2f} p95={p95_ms:.2f} p99={p99_ms:.2f}")
log(f"  First frame   : {t_first_frame or 0:.1f} ms")
log(f"  RSS peak      : {rss_after_run:.0f} MB")
log(f"  CSV           : {csv_path}")
log(f"  Load JSON     : {load_path}")
log("=" * 64)

# Force QGIS to quit (qgis --code laisse le GUI ouvert sinon → SIGTERM par
# l'orchestrator. os._exit court-circuite la Qt event loop proprement vu
# qu'on a déjà flushé les fichiers).
os._exit(0)
