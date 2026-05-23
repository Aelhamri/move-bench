"""
RQ2 - Endpoints Flask additionnels pour le benchmark.

A monter sur l'app Flask existante via :

    from rq2.flask_bench_routes import register_bench_routes
    register_bench_routes(app, ctx)   # dans create_app() ou apres

Endpoints :
  - GET  /api/bench/scenario/<int:limit>
        Selection deterministe partagee avec QGIS : meme requete SQL
        (cf bench_config.trip_selection_sql), pre-echantillonnee a
        FRAME_STEP_SECONDS, retournee au format /api/stib/trajectories.
        Permet de garantir que QGIS et Flask animent les MEMES trips.

  - POST /api/bench/log
        Le frontend envoie ses metriques (per-frame elapsed_s + frame_time_ms)
        + load phase. Le serveur les ecrit dans un CSV / JSON sur disque.

L'orchestrator.py monte ces routes au demarrage de Flask.
"""

import csv
import json
import os
import sys
import time
from pathlib import Path

from flask import Flask, request, jsonify, make_response

THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS_DIR.parent))
import bench_config as cfg


# tsample interval pour Flask -- doit etre identique a QGIS bench
SAMPLE_INTERVAL = f"{cfg.FRAME_STEP_SECONDS} seconds"


def _trip_selection_sql_with_tsample(limit: int) -> str:
    """SQL similaire a celui du QGIS bench mais retourne aussi line/mode pour le frontend."""
    limit_clause = f"LIMIT {limit}" if limit > 0 else ""
    return f"""
        WITH selected AS (
            SELECT trip_id, lineid, direction, line_trip_id, trip
            FROM rt.stib_trip
            WHERE {cfg.DATE_FILTER_SQL}
              AND {cfg.QUALITY_FILTER_SQL}
            ORDER BY trip_id
            {limit_clause}
        ),
        anchor AS (
            SELECT MIN(startTimestamp(trip)) AS t0 FROM selected
        ),
        sampled AS (
            SELECT
                s.trip_id,
                s.lineid,
                s.direction,
                s.line_trip_id,
                inst
            FROM selected s, anchor a,
                 LATERAL unnest(instants(tsample(s.trip, '{SAMPLE_INTERVAL}'::interval, a.t0))) AS inst
        )
        SELECT
            trip_id,
            lineid,
            direction,
            line_trip_id,
            array_agg(
                jsonb_build_array(
                    (extract(epoch FROM getTimestamp(inst)) * 1000)::bigint,
                    ST_X(getValue(inst)::geometry),
                    ST_Y(getValue(inst)::geometry),
                    NULL
                ) ORDER BY getTimestamp(inst)
            ) AS samples
        FROM sampled
        GROUP BY trip_id, lineid, direction, line_trip_id
        ORDER BY trip_id;
    """


def register_bench_routes(app: Flask, ctx) -> None:
    """Monte les endpoints /api/bench/* sur l'app Flask."""

    cfg.ensure_output_dir()
    output_dir = cfg.RQ2_DIR
    output_dir.mkdir(parents=True, exist_ok=True)

    @app.route("/api/bench/scenario/<int:limit>")
    def bench_scenario(limit: int):
        """Selection deterministe partagee avec QGIS, pre-echantillonnee."""
        t_sql_start = time.perf_counter()
        with ctx.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(_trip_selection_sql_with_tsample(limit))
                rows = cur.fetchall()
        t_sql_end = time.perf_counter()

        trips = []
        for trip_id, lineid, direction, line_trip_id, samples in rows:
            if not samples:
                continue
            trips.append({
                "trip_id":      trip_id,
                "lineid":       lineid,
                "direction":    direction,
                "line_trip_id": line_trip_id,
                "samples":      samples,    # [[t_ms, lon, lat, null], ...]
            })

        return jsonify({
            "ok":               True,
            "scenario_limit":   limit,
            "n_trips":          len(trips),
            "sample_interval":  SAMPLE_INTERVAL,
            "sql_seconds":      t_sql_end - t_sql_start,
            "trips":            trips,
        })

    @app.route("/api/bench/scenario_stream/<int:limit>")
    def bench_scenario_stream(limit: int):
        """v2 (post-3-agents review) : streaming NDJSON pour les datasets
        all_day où la version monolithique cause OOM Chrome (~200 MB JSON).
        Format : 1 trip JSON par ligne (newline-delimited JSON, NDJSON).
        Le client peut parser incrémentalement avec ReadableStream et
        libérer la mémoire entre chunks. Sample interval IDENTIQUE à
        l'endpoint monolithique (5s) → pas de biais sur la résolution
        temporelle. Seul le format de transport change ; la donnée
        finale dans le store JS est strictement la même."""
        import json as _json
        from flask import Response, stream_with_context

        sql = _trip_selection_sql_with_tsample(limit)

        def generate():
            with ctx.pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(sql)
                    # On itère le résultat (psycopg3 fait du fetch incrémental
                    # via le client buffer ; pas de server-side cursor pour
                    # éviter les complications transactionnelles).
                    while True:
                        rows = cur.fetchmany(100)
                        if not rows:
                            break
                        for trip_id, lineid, direction, line_trip_id, samples in rows:
                            if not samples:
                                continue
                            line = _json.dumps({
                                "trip_id":      trip_id,
                                "lineid":       lineid,
                                "direction":    direction,
                                "line_trip_id": line_trip_id,
                                "samples":      samples,
                            }, separators=(",", ":"))
                            yield line + "\n"

        return Response(stream_with_context(generate()),
                        mimetype="application/x-ndjson")

    @app.route("/api/bench/log", methods=["POST"])
    def bench_log():
        """
        Le frontend POST ses metriques.
        Body JSON :
            {
              "limit": 1000,
              "run_id": 1,
              "load": {
                "fetch_start_ms": 1234567,
                "fetch_end_ms":   1234890,
                "parse_end_ms":   1234990,
                "first_frame_ms": 1235100,
                "payload_bytes":  12345678,
                "n_trips":        1000
              },
              "frames": [
                {"frame_idx": 0, "elapsed_s": 0.016, "frame_time_ms": 16.5, "visible": 800},
                ...
              ]
            }
        """
        data = request.get_json(silent=True) or {}
        limit  = int(data.get("limit", 0))
        run_id = int(data.get("run_id", 1))

        # Ecrire le CSV des frames (v2 post-3-agents review).
        # frame_time_ms est conservé en alias = frame_interval_ms (= cadence rAF
        # = frame budget perçu, vsync-aligned, métrique symétrique avec
        # QGIS SequentialJob).  frame_cpu_ms = coût JS pur dans tpPlayLoop.
        frames = data.get("frames", [])
        csv_path = output_dir / f"flask_limit{limit}_run{run_id}.csv"
        with open(csv_path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["run_id", "frame_idx", "elapsed_s", "is_warmup",
                        "frame_time_ms", "frame_cpu_ms", "frame_interval_ms",
                        "visible"])
            for fr in frames:
                interval = fr.get("frame_interval_ms", fr.get("frame_time_ms", 0))
                cpu      = fr.get("frame_cpu_ms",      fr.get("frame_time_ms", 0))
                w.writerow([
                    run_id,
                    fr.get("frame_idx", 0),
                    fr.get("elapsed_s", 0),
                    int(fr.get("elapsed_s", 0) < 5.0),
                    interval,
                    cpu,
                    interval,
                    fr.get("visible", 0),
                ])

        # Ecrire le JSON load
        load = data.get("load", {})
        load_path = output_dir / f"flask_limit{limit}_run{run_id}_load.json"
        with open(load_path, "w") as f:
            json.dump({
                "label":   "flask",
                "date":    cfg.BENCH_DATE,
                "limit":   limit,
                "run_id":  run_id,
                "n_trips": load.get("n_trips", 0),
                "load": {
                    "fetch_seconds":  (load.get("fetch_end_ms",   0) - load.get("fetch_start_ms", 0)) / 1000.0,
                    "parse_seconds":  (load.get("parse_end_ms",   0) - load.get("fetch_end_ms",   0)) / 1000.0,
                    "first_frame_ms": load.get("first_frame_ms",  0) - load.get("fetch_start_ms", 0),
                    "payload_bytes":  load.get("payload_bytes",   -1),
                },
            }, f, indent=2)

        return jsonify({
            "ok":         True,
            "csv":        str(csv_path),
            "load":       str(load_path),
            "n_frames":   len(frames),
        })


# -----------------------------------------------------------------------------
# Standalone helper : lancer l'app Flask de RTDataHub avec les routes bench
# -----------------------------------------------------------------------------
def main():
    """
    Standalone : lance l'app Flask classique + register_bench_routes en plus.

    Usage :
        python -m scripts.bench.rq2.flask_bench_routes \
            --listen-port 8090
    """
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--listen-port", type=int, default=8090)
    parser.add_argument("--max-db-connections", type=int, default=10)
    args = parser.parse_args()

    from werkzeug.serving import make_server
    from psycopg_pool import ConnectionPool

    # Reuse l'app existante de RTDataHub
    sys.path.insert(0, "/opt/RTDataHub")  # adapter selon la VM
    from src.map.server.config import ServerConfig
    from src.map.server.context import MapAppContext
    from src.map.server.app import create_app

    cfg_server = ServerConfig(
        dbname=cfg.DB_NAME, user=cfg.DB_USER, password=cfg.DB_PASSWORD,
        host=cfg.DB_HOST, port=cfg.DB_PORT,
        default_hours=24.0,
        max_db_connections=args.max_db_connections,
        db_acquire_timeout_s=300.0,
        db_statement_timeout_ms=600000,   # 10 min : large pour all_day
        tile_cache_ttl_s=160.0,
        tile_cache_max_entries=4000,
        counts_cache_ttl_s=4.0,
    )
    pool = ConnectionPool(
        conninfo=cfg_server.conninfo,
        min_size=2, max_size=cfg_server.max_db_connections,
        timeout=cfg_server.db_acquire_timeout_s, open=True,
    )
    app = create_app(cfg_server, pool)

    # Inject ctx pour bench routes (recree)
    ctx = MapAppContext(cfg_server, pool)
    register_bench_routes(app, ctx)

    print(f"[bench-flask] PID {os.getpid()} listening on http://{args.listen_host}:{args.listen_port}")
    print(f"[bench-flask] bench routes : /api/bench/scenario/<N> + POST /api/bench/log")

    server = make_server(args.listen_host, args.listen_port, app, threaded=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        pool.close()


if __name__ == "__main__":
    main()
