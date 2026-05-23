"""
Configuration partagee entre RQ1 et RQ2.

Source unique de verite pour :
- les presets de dataset (STIB Brussels / Danish AIS)
- la matrice de scenarios
- les requetes SQL canoniques
- les chemins de sortie

Selection du dataset :
    export BENCH_DATASET=ais     # Danish AIS (Ali's reference)
    export BENCH_DATASET=stib    # STIB Brussels (notre cas)

Chaque dataset a ses propres parametres de connexion, son ID column, etc.
"""

import os
from datetime import date, timedelta


# -----------------------------------------------------------------------------
# Selection du dataset
# -----------------------------------------------------------------------------
DATASET = os.environ.get("BENCH_DATASET", "stib").lower()
if DATASET not in ("stib", "ais"):
    raise ValueError(f"Unknown BENCH_DATASET={DATASET!r}, expected 'stib' or 'ais'")


# -----------------------------------------------------------------------------
# Preset STIB (defaut) : RT data, jour fige, filtre qualite
# -----------------------------------------------------------------------------
if DATASET == "stib":
    # RQ2 défaut : DB locale rtdatahub_local (port 5432, user rtdatahub).
    # Pour utiliser le tunnel ULB : exporter BENCH_DB_PORT=5433,
    # BENCH_DB_USER=mobilitydb, BENCH_DB_NAME=mobilitydb,
    # BENCH_DB_PASSWORD=mobilitydb.
    DB_HOST     = os.environ.get("BENCH_DB_HOST",     "localhost")
    DB_PORT     = int(os.environ.get("BENCH_DB_PORT", "5432"))
    DB_NAME     = os.environ.get("BENCH_DB_NAME",     "rtdatahub_local")
    DB_USER     = os.environ.get("BENCH_DB_USER",     "rtdatahub")
    DB_PASSWORD = os.environ.get("BENCH_DB_PASSWORD", "rtdatahub")
    DB_SRID     = 4326

    TABLE_NAME    = "rt.stib_trip"
    ID_COLUMN     = "trip_id"
    TPOINT_COLUMN = "trip"

    # RQ2 (DB locale rtdatahub_local) : 2026-05-02 = jour le plus dense
    # (71802 trips qualifiants). Fenêtre 12h-22h → 25890 trips dispo,
    # permet d'atteindre N=15000 et d'avoir un all_day significatif.
    BENCH_DATE      = os.environ.get("BENCH_DATE", "2026-05-02")
    BENCH_DATE_NEXT = (date.fromisoformat(BENCH_DATE) + timedelta(days=1)).isoformat()

    BENCH_HOUR_START = int(os.environ.get("BENCH_HOUR_START", "12"))
    BENCH_HOUR_END   = int(os.environ.get("BENCH_HOUR_END",   "22"))

    QUALITY_FILTER_SQL = (
        "trip IS NOT NULL "
        "AND point_count >= 5 "
        "AND extract(epoch FROM endTimestamp(trip) - startTimestamp(trip)) / 60 <= 240"
    )
    # Selectionne les trips QUI CHEVAUCHENT la fenetre [start, end] :
    # un trip qui demarre avant et finit pendant compte aussi.
    DATE_FILTER_SQL = (
        f"start_ts < '{BENCH_DATE} {BENCH_HOUR_END:02d}:00:00'::timestamp "
        f"AND end_ts >= '{BENCH_DATE} {BENCH_HOUR_START:02d}:00:00'::timestamp"
    )

    # STIB trips sont courts -> step rapproche aligne avec Flask /api/stib/trajectories
    FRAME_STEP_SECONDS = 5


# -----------------------------------------------------------------------------
# Preset AIS : Danish AIS, statique, pas de filtre date, parametres "Ali"
# -----------------------------------------------------------------------------
elif DATASET == "ais":
    DB_HOST     = os.environ.get("BENCH_DB_HOST",     "localhost")
    DB_PORT     = int(os.environ.get("BENCH_DB_PORT", "5432"))
    DB_NAME     = os.environ.get("BENCH_DB_NAME",     "AISdata01062023")
    DB_USER     = os.environ.get("BENCH_DB_USER",     "postgres")
    DB_PASSWORD = os.environ.get("BENCH_DB_PASSWORD", "postgres")
    DB_SRID     = 25832

    TABLE_NAME    = "public.ships"
    ID_COLUMN     = "mmsi"
    TPOINT_COLUMN = "trip"

    BENCH_DATE      = None     # pas de filtre date pour le snapshot AIS statique
    BENCH_DATE_NEXT = None
    BENCH_HOUR_START = None
    BENCH_HOUR_END   = None

    QUALITY_FILTER_SQL = "trip IS NOT NULL"
    DATE_FILTER_SQL    = ""

    # Parametres "Ali" : 1 frame = 1 minute (compatible avec ses chiffres)
    FRAME_STEP_SECONDS = 60


# -----------------------------------------------------------------------------
# Sortie
# -----------------------------------------------------------------------------
# Strategie idiot-proof :
# - Defaut : ~/bench_results/{DATASET} (auto-isolation par dataset)
# - Override via BENCH_OUTPUT_DIR (power user, mais on detecte les pieges)
# - Strip auto des /rq1, /rq2 traines pour eviter les doublons
# - Warning loud si l'override ne contient pas le nom du dataset
import sys as _sys
from pathlib import Path as _Path

_explicit_output = os.environ.get("BENCH_OUTPUT_DIR")
if _explicit_output:
    OUTPUT_DIR = os.path.expanduser(_explicit_output)
    # Idempotence : strip trailing /rq1 ou /rq2
    _p = _Path(OUTPUT_DIR)
    if _p.name in ("rq1", "rq2"):
        OUTPUT_DIR = str(_p.parent)
        print(f"[bench_config] BENCH_OUTPUT_DIR ended with /{_p.name}, stripped to {OUTPUT_DIR}",
              file=_sys.stderr)
    # Sanity check : le path contient-il le nom du dataset ?
    if DATASET not in OUTPUT_DIR.lower():
        print("", file=_sys.stderr)
        print("=" * 64, file=_sys.stderr)
        print(f"  WARNING: BENCH_OUTPUT_DIR mismatch with DATASET", file=_sys.stderr)
        print(f"  BENCH_OUTPUT_DIR = {OUTPUT_DIR}", file=_sys.stderr)
        print(f"  BENCH_DATASET    = {DATASET}", file=_sys.stderr)
        print(f"  -> path does NOT contain '{DATASET}' -> reading/writing wrong dataset!", file=_sys.stderr)
        print(f"  Fix:", file=_sys.stderr)
        print(f"     unset BENCH_OUTPUT_DIR    # to use default ~/bench_results/{DATASET}", file=_sys.stderr)
        print(f"  OR export BENCH_OUTPUT_DIR=~/bench_results/{DATASET}", file=_sys.stderr)
        print("=" * 64, file=_sys.stderr)
        print("", file=_sys.stderr)
else:
    OUTPUT_DIR = os.path.expanduser(f"~/bench_results/{DATASET}")

# Sous-dossiers par question de recherche : utiliser cfg.RQ1_DIR / cfg.RQ2_DIR
# au lieu de cfg.OUTPUT_DIR / "rq1" pour eviter la double imbrication.
RQ1_DIR = _Path(OUTPUT_DIR) / "rq1"
RQ2_DIR = _Path(OUTPUT_DIR) / "rq2"


# -----------------------------------------------------------------------------
# Test matrix
# -----------------------------------------------------------------------------
SCENARIO_LIMITS = [100, 250, 500, 1000, 2000, 5000, 0]   # 0 = tous

N_RUNS_PER_SCENARIO = 5
STEADY_STATE_SECONDS = 60     # uniquement RQ2
FRAME_WARMUP = 10             # frames jetees a l'analyse
N_FRAMES_SYNTHETIC = 120      # bench RQ1 synthetique


# -----------------------------------------------------------------------------
# Requete canonique : selection deterministe des trips a animer
# -----------------------------------------------------------------------------
def trip_selection_sql(limit: int = 0, table: str = None) -> str:
    """SQL deterministe partage QGIS / Flask pour selectionner les trips."""
    table = table or TABLE_NAME
    limit_clause = f"LIMIT {limit}" if limit > 0 else ""
    where_parts = [QUALITY_FILTER_SQL]
    if DATE_FILTER_SQL:
        where_parts.append(DATE_FILTER_SQL)
    where_sql = " AND ".join(where_parts)
    return f"""
        SELECT
            {ID_COLUMN},
            {TPOINT_COLUMN},
            startTimestamp({TPOINT_COLUMN}) AS t_start,
            endTimestamp({TPOINT_COLUMN})   AS t_end
        FROM {table}
        WHERE {where_sql}
        ORDER BY {ID_COLUMN}
        {limit_clause}
    """.strip()


# -----------------------------------------------------------------------------
# Requete de validation : la DB est-elle dans l'etat attendu ?
# -----------------------------------------------------------------------------
def count_validation_sql(table: str = None) -> str:
    table = table or TABLE_NAME
    where_parts = [QUALITY_FILTER_SQL]
    if DATE_FILTER_SQL:
        where_parts.append(DATE_FILTER_SQL)
    where_sql = " AND ".join(where_parts)
    return f"SELECT COUNT(*) AS n FROM {table} WHERE {where_sql}"


COUNT_VALIDATION_SQL = count_validation_sql()


def ensure_output_dir() -> str:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    return OUTPUT_DIR


# -----------------------------------------------------------------------------
# Garde-fou memoire pour le bench columnar
# -----------------------------------------------------------------------------
# La matrice columnar a une taille O(N_trips * N_frames * 16 bytes) (X et Y en
# float64). Avec 30k trips x 17 280 frames @ 5s sur une journee complete, on
# arrive a ~8 GB et QGIS crashe. Cette fonction estime la taille avant
# allocation et permet d'aborter proprement avec un message clair.
COLUMNAR_MATRIX_LIMIT_MB = float(os.environ.get("BENCH_COLUMNAR_LIMIT_MB", "4096"))

def estimate_columnar_matrix_mb(n_trips: int, n_frames: int, dtype_bytes: int = 8) -> float:
    """Estime la RAM en MB des matrices X et Y (deux matrices N x T)."""
    return n_trips * n_frames * 2 * dtype_bytes / 1e6


def assert_columnar_matrix_fits(n_trips: int, n_frames: int, dtype_bytes: int = 8):
    """Abort propre si la matrice estimee depasse COLUMNAR_MATRIX_LIMIT_MB."""
    mb = estimate_columnar_matrix_mb(n_trips, n_frames, dtype_bytes)
    if mb > COLUMNAR_MATRIX_LIMIT_MB:
        raise MemoryError(
            f"Columnar matrix would consume {mb:.0f} MB "
            f"({n_trips} trips x {n_frames} frames x 2 x {dtype_bytes} bytes), "
            f"exceeding limit of {COLUMNAR_MATRIX_LIMIT_MB:.0f} MB.\n"
            f"  Solutions:\n"
            f"    - reduce LIMIT (test smaller scenarios first)\n"
            f"    - narrow the time window: export BENCH_HOUR_START=17 BENCH_HOUR_END=19\n"
            f"    - increase the limit: export BENCH_COLUMNAR_LIMIT_MB=8192\n"
            f"  See README for memory budget guidance."
        )


# -----------------------------------------------------------------------------
# Diagnostic affiche au demarrage de chaque script
# -----------------------------------------------------------------------------
def print_dataset_banner():
    print(f"[bench_config] DATASET={DATASET}")
    print(f"               table={TABLE_NAME} id={ID_COLUMN} tpoint={TPOINT_COLUMN} srid={DB_SRID}")
    print(f"               db={DB_USER}@{DB_HOST}:{DB_PORT}/{DB_NAME}")
    if BENCH_DATE:
        print(f"               date={BENCH_DATE}")
    if BENCH_HOUR_START is not None and BENCH_HOUR_END is not None:
        n_frames = (BENCH_HOUR_END - BENCH_HOUR_START) * 3600 // FRAME_STEP_SECONDS
        print(f"               window={BENCH_HOUR_START:02d}h-{BENCH_HOUR_END:02d}h ({n_frames} frames @ {FRAME_STEP_SECONDS}s)")
    print(f"               frame_step={FRAME_STEP_SECONDS}s")
    print(f"               columnar_limit_mb={COLUMNAR_MATRIX_LIMIT_MB:.0f}")
    print(f"               output={OUTPUT_DIR}")
