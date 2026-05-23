"""
RQ2 — Aggregate v2 (post-3-agents review).
==========================================

Changements vs v1 :
  ✓ Métrique PRINCIPALE = FPS soutenu (= n_frames_steady / duration_steady),
    symétrique avec la cadence rAF côté Flask.
  ✓ p95 frame_time_ms reporté (en plus de la médiane) — sensibilité aux stalls
  ✓ t-Student log-space en plus du bootstrap percentile (CI plus honnête à N=4)
  ✓ Range observé [min, max] reporté en plus des CI
  ✓ Tableau run-par-run printé pour justifier le drop run 1
  ✓ frames is_wrap (bouclage temporel) excluees côté QGIS (déjà fait dans le bench)
  ✓ Nouvelle colonne CSV : fps_sustained (vs ancien fps = 1000/median_ms)

Author: Ayoub El Hamri
"""

import csv
import json
import math
import random
import statistics
import sys
from pathlib import Path
from collections import defaultdict

THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS_DIR))
import bench_config as cfg

random.seed(42)
N_BOOT = 1000
T_VALUE_DF3_95 = 3.182    # t.ppf(0.975, df=3) — n=4 runs

INPUT_DIR  = cfg.RQ2_DIR
OUTPUT_DIR = THIS_DIR
SYSTEMS    = ["qgis", "flask"]
LIMIT_LABELS = {0: "all_day"}


def parse_filename(stem: str):
    parts = stem.split("_")
    if len(parts) < 3:
        return None
    try:
        sys_name = parts[0]
        limit    = int(parts[1].replace("limit", ""))
        run_id   = int(parts[2].replace("run", ""))
        return sys_name, limit, run_id
    except (ValueError, IndexError):
        return None


def peak_rss_from_resources_csv(csv_path: Path) -> float:
    """Somme du RSS max par rôle (flask+postgres, ou chrome-*) depuis un CSV
    resources_*.csv généré par resource_sampler.py.
    Retourne 0.0 si le fichier n'existe pas ou est vide."""
    if not csv_path.exists():
        return 0.0
    rows = []
    try:
        with open(csv_path) as f:
            rows = list(csv.DictReader(f))
    except Exception:
        return 0.0
    if not rows:
        return 0.0
    # Chaque ligne a "name" + "rss_mb". On prend le pic par nom, puis on somme.
    by_name: dict[str, float] = {}
    for row in rows:
        name = row.get("name", "unknown")
        try:
            rss = float(row.get("rss_mb", 0) or 0)
        except ValueError:
            continue
        if rss > by_name.get(name, 0.0):
            by_name[name] = rss
    return sum(by_name.values())


def load_runs(system: str) -> dict:
    """{limit: {run_id: {"frames": [...], "load": {...}}}}"""
    out = defaultdict(dict)
    for csv_p in INPUT_DIR.glob(f"{system}_limit*_run*.csv"):
        parsed = parse_filename(csv_p.stem)
        if parsed is None:
            continue
        _, limit, run_id = parsed
        load_p = csv_p.with_name(csv_p.stem + "_load.json")
        with open(csv_p) as f:
            frames = list(csv.DictReader(f))
        load = {}
        if load_p.exists():
            with open(load_p) as f:
                load = json.load(f)
        # RSS depuis les CSV resources (QGIS + Flask server + Chrome)
        res_server_csv  = INPUT_DIR / f"resources_{system}_limit{limit}_run{run_id}.csv"
        res_chrome_csv  = INPUT_DIR / f"resources_chrome_limit{limit}_run{run_id}.csv"
        rss_server = peak_rss_from_resources_csv(res_server_csv)
        rss_chrome = peak_rss_from_resources_csv(res_chrome_csv)
        out[limit][run_id] = {
            "frames": frames,
            "load":   load,
            "rss_server_mb": rss_server,
            "rss_chrome_mb": rss_chrome,
        }
    return out


def is_wrap_frame(f: dict) -> bool:
    """QGIS-only : on a un flag is_wrap dans le CSV. Flask : pas de wrap."""
    v = f.get("is_wrap", "0")
    return str(v) in ("1", "True", "true")


def is_warmup_frame(f: dict) -> bool:
    v = f.get("is_warmup", "0")
    return str(v) in ("1", "True", "true")


def steady_frames(frames: list[dict]) -> list[dict]:
    """Drop warmup + wrap (côté QGIS)."""
    return [f for f in frames if not is_warmup_frame(f) and not is_wrap_frame(f)]


def fps_sustained_from_run(r: dict) -> float | None:
    """v2 : prefer le FPS soutenu pré-calculé dans le summary JSON, sinon
    le recalcule depuis les frames CSV."""
    run_block = r["load"].get("run") or {}
    fps = run_block.get("fps_sustained")
    if fps is not None and fps > 0:
        return float(fps)
    # fallback : calculer depuis le CSV
    steady = steady_frames(r["frames"])
    if not steady:
        return None
    el = [float(f["elapsed_s"]) for f in steady]
    duration = el[-1] - el[0] if len(el) > 1 else 0
    return len(steady) / duration if duration > 0 else None


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * p / 100.0
    lo = int(math.floor(k))
    hi = int(math.ceil(k))
    if lo == hi:
        return s[lo]
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def frame_time_ms_distribution(r: dict) -> list[float]:
    steady = steady_frames(r["frames"])
    return [float(f["frame_time_ms"]) for f in steady
            if float(f.get("frame_time_ms", 0)) > 0]


def bootstrap_ci95(values: list[float], n_boot: int = N_BOOT) -> tuple[float, float]:
    if len(values) < 2:
        v = values[0] if values else 0
        return v, v
    boots = []
    for _ in range(n_boot):
        sample = [random.choice(values) for _ in values]
        boots.append(statistics.median(sample))
    boots.sort()
    return boots[int(0.025 * n_boot)], boots[int(0.975 * n_boot)]


def t_student_log_ci95(values: list[float]) -> tuple[float, float]:
    """t-Student CI95 sur log(values), ré-exponentié. Plus honnête que
    bootstrap percentile pour N=4 (couverture nominale ~95% au lieu de
    ~75%). df = n-1 = 3, t = 3.182.
    """
    if len(values) < 2:
        v = values[0] if values else 0
        return v, v
    logs = [math.log(v) for v in values if v > 0]
    if len(logs) < 2:
        v = values[0]
        return v, v
    mean_log = statistics.mean(logs)
    sd_log   = statistics.stdev(logs)
    se_log   = sd_log / math.sqrt(len(logs))
    half     = T_VALUE_DF3_95 * se_log
    return math.exp(mean_log - half), math.exp(mean_log + half)


def aggregate_cell(runs_for_limit: dict, drop_run_1: bool = True) -> dict | None:
    """For one (system, limit) cell across all its runs."""
    run_ids = sorted(runs_for_limit.keys())
    if not run_ids:
        return None
    kept = run_ids[1:] if (drop_run_1 and len(run_ids) > 1) else run_ids

    # Per-run aggregates
    fps_per_run     = []
    median_ms_per_run = []
    p95_ms_per_run  = []
    loads_per_run   = []
    first_frames    = []
    rss_peaks       = []
    n_trips         = None
    raw_runs        = []   # pour le tableau run-par-run

    for run_id in kept:
        r = runs_for_limit[run_id]
        fps = fps_sustained_from_run(r)
        if fps is None:
            continue
        fps_per_run.append(fps)

        dist = frame_time_ms_distribution(r)
        if dist:
            median_ms_per_run.append(statistics.median(dist))
            p95_ms_per_run.append(percentile(dist, 95))

        load_block = r["load"].get("load") or {}
        run_block  = r["load"].get("run")  or {}
        loads_per_run.append(float(load_block.get("total_seconds")
                                   or load_block.get("fetch_seconds") or 0))
        first_frames.append(float(run_block.get("first_frame_ms")
                                  or load_block.get("first_frame_ms") or 0))
        # RSS total = server (flask+postgres ou qgis) + Chrome (si capturé)
        # Les CSV resources_* sont la source de vérité pour tous les systèmes.
        # run_block.get("rss_after_mb") reste dispo pour QGIS (fallback legacy).
        rss_total = r.get("rss_server_mb", 0.0) + r.get("rss_chrome_mb", 0.0)
        if rss_total == 0.0:
            rss_total = float(run_block.get("rss_after_mb") or 0)
        rss_peaks.append(rss_total)
        if n_trips is None:
            n_trips = r["load"].get("n_trips")

        raw_runs.append({
            "run_id": run_id,
            "fps":    fps,
            "med_ms": median_ms_per_run[-1] if median_ms_per_run else 0,
            "p95_ms": p95_ms_per_run[-1] if p95_ms_per_run else 0,
            "load_s": loads_per_run[-1],
        })

    if not fps_per_run:
        return None

    median_fps = statistics.median(fps_per_run)
    boot_lo, boot_hi   = bootstrap_ci95(fps_per_run)
    tlog_lo, tlog_hi   = t_student_log_ci95(fps_per_run)

    return {
        "n_trips":            n_trips,
        "n_runs_kept":        len(fps_per_run),
        # FPS soutenu = métrique principale
        "fps_sustained_med":  median_fps,
        "fps_boot_lo":        boot_lo,
        "fps_boot_hi":        boot_hi,
        "fps_tlog_lo":        tlog_lo,
        "fps_tlog_hi":        tlog_hi,
        "fps_min":            min(fps_per_run),
        "fps_max":            max(fps_per_run),
        # Distribution frame_time_ms (médianes inter-runs)
        "median_ms_med":      statistics.median(median_ms_per_run) if median_ms_per_run else 0,
        "p95_ms_med":         statistics.median(p95_ms_per_run) if p95_ms_per_run else 0,
        # Charge phase
        "load_seconds_med":   statistics.median(loads_per_run) if loads_per_run else 0.0,
        "first_frame_ms_med": statistics.median(first_frames) if first_frames else 0.0,
        "rss_peak_mb_med":    statistics.median(rss_peaks) if rss_peaks else 0.0,
        "raw_runs":           raw_runs,
        "all_runs":           sorted(runs_for_limit.keys()),
    }


# -----------------------------------------------------------------------------
# Build matrix
# -----------------------------------------------------------------------------
print("=" * 110)
print("RQ2 v2 — Cross-system aggregate (post-3-agents review)")
print("  Métrique principale : FPS soutenu (= n_frames_steady / duration_steady)")
print("  CI : bootstrap percentile (N=1000) + t-Student log-space (df=3, t=3.182)")
print(f"  Reading from : {INPUT_DIR}")
print("=" * 110)

matrix = {}
all_limits = set()
all_runs_loaded = {}
for system in SYSTEMS:
    runs = load_runs(system)
    all_runs_loaded[system] = runs
    for limit, runs_for_limit in runs.items():
        all_limits.add(limit)
        agg = aggregate_cell(runs_for_limit)
        matrix[(system, limit)] = agg
limits_sorted = sorted(all_limits, key=lambda x: (x == 0, x))


# -----------------------------------------------------------------------------
# Tableau RUN-PAR-RUN (justifie le drop run 1)
# -----------------------------------------------------------------------------
print()
print("=" * 110)
print("Tableau run-par-run (avant drop) — justification empirique du drop run 1")
print("=" * 110)
print(f"{'System':<8} | {'Limit':>8} | {'Run':>4} | {'FPS soutenu':>12} | {'Median ms':>10} | {'p95 ms':>8} | {'Load s':>8}")
print("-" * 80)
for system in SYSTEMS:
    runs_all = all_runs_loaded.get(system, {})
    for limit in limits_sorted:
        run_ids = sorted(runs_all.get(limit, {}).keys())
        for run_id in run_ids:
            r = runs_all[limit][run_id]
            fps = fps_sustained_from_run(r)
            dist = frame_time_ms_distribution(r)
            med = statistics.median(dist) if dist else 0
            p95 = percentile(dist, 95) if dist else 0
            load = (r["load"].get("load") or {}).get("total_seconds") or 0
            lab = LIMIT_LABELS.get(limit, str(limit))
            tag = " (dropped)" if run_id == 1 else ""
            print(f"{system:<8} | {lab:>8} | {run_id:>4} | {fps or 0:>12.2f} | {med:>10.2f} | {p95:>8.2f} | {load:>8.2f}{tag}")
        print("-" * 80)


# -----------------------------------------------------------------------------
# Tableau récap aggregate (post drop run 1)
# -----------------------------------------------------------------------------
print()
print("=" * 130)
print("RÉCAP AGGREGATE (drop run 1, N effectif = 4)")
print("=" * 130)
print(f"{'System':<8} | {'Limit':>8} | {'N':>3} | "
      f"{'FPS soutenu':>12} | {'Boot CI95':>20} | {'tLog CI95':>20} | {'Range':>17} | "
      f"{'Med ms':>8} | {'p95 ms':>8} | {'Load s':>7} | {'RSS MB':>7}")
print("-" * 145)
for system in SYSTEMS:
    for limit in limits_sorted:
        m = matrix.get((system, limit))
        lab = LIMIT_LABELS.get(limit, str(limit))
        if m is None:
            print(f"{system:<8} | {lab:>8} |  -  |     -        |          -          |          -          |        -        |    -    |    -    |    -    |    -")
            continue
        boot = f"[{m['fps_boot_lo']:6.1f}-{m['fps_boot_hi']:6.1f}]"
        tlog = f"[{m['fps_tlog_lo']:6.1f}-{m['fps_tlog_hi']:6.1f}]"
        rng  = f"[{m['fps_min']:6.1f}-{m['fps_max']:6.1f}]"
        print(f"{system:<8} | {lab:>8} | {m['n_runs_kept']:>3} | "
              f"{m['fps_sustained_med']:>12.2f} | {boot:>20} | {tlog:>20} | {rng:>17} | "
              f"{m['median_ms_med']:>8.2f} | {m['p95_ms_med']:>8.2f} | "
              f"{m['load_seconds_med']:>7.2f} | {m['rss_peak_mb_med']:>7.0f}")
print("-" * 145)


# -----------------------------------------------------------------------------
# Speedup ratios (Flask FPS / QGIS FPS)
# -----------------------------------------------------------------------------
print()
print("=" * 100)
print("Speedup Flask/QGIS sur FPS soutenu (>1 = Flask plus rapide)")
print("  Ratio CI via interval arithmetic conservateur (CI t-Log).")
print("=" * 100)
for limit in limits_sorted:
    q = matrix.get(("qgis", limit))
    f = matrix.get(("flask", limit))
    lab = LIMIT_LABELS.get(limit, str(limit))
    if q is None or f is None:
        print(f"  {lab:>8} : missing")
        continue
    ratio = f["fps_sustained_med"] / q["fps_sustained_med"] if q["fps_sustained_med"] else 0
    if q["fps_tlog_lo"] > 0 and q["fps_tlog_hi"] > 0:
        ratio_lo = f["fps_tlog_lo"] / q["fps_tlog_hi"]
        ratio_hi = f["fps_tlog_hi"] / q["fps_tlog_lo"]
        ci = f"[CI tLog: {ratio_lo:.2f}-{ratio_hi:.2f}]"
    else:
        ci = ""
    marker = "★" if ratio > 1.5 or ratio < 0.67 else " "
    print(f"  {marker} {lab:>8} : Flask {f['fps_sustained_med']:>7.2f} FPS  "
          f"vs QGIS {q['fps_sustained_med']:>7.2f} FPS  →  {ratio:5.2f}× {ci}")


# -----------------------------------------------------------------------------
# CSV export
# -----------------------------------------------------------------------------
csv_out = OUTPUT_DIR / "bench6_cross_matrix.csv"
with open(csv_out, "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["system", "limit", "limit_label", "n_trips", "n_runs_kept",
                "fps_sustained_med",
                "fps_boot_lo", "fps_boot_hi",
                "fps_tlog_lo", "fps_tlog_hi",
                "fps_min", "fps_max",
                "median_ms_med", "p95_ms_med",
                "load_seconds_med", "first_frame_ms_med", "rss_peak_mb_med"])
    for system in SYSTEMS:
        for limit in limits_sorted:
            m = matrix.get((system, limit))
            lab = LIMIT_LABELS.get(limit, str(limit))
            if m is None:
                w.writerow([system, limit, lab, "", "", "", "", "", "", "", "", "", "", "", "", "", ""])
            else:
                w.writerow([system, limit, lab, m["n_trips"], m["n_runs_kept"],
                            m["fps_sustained_med"],
                            m["fps_boot_lo"], m["fps_boot_hi"],
                            m["fps_tlog_lo"], m["fps_tlog_hi"],
                            m["fps_min"], m["fps_max"],
                            m["median_ms_med"], m["p95_ms_med"],
                            m["load_seconds_med"], m["first_frame_ms_med"],
                            m["rss_peak_mb_med"]])

print()
print(f"CSV written: {csv_out}")
print()
print("=" * 110)
print("Next: python3 bench6_charts.py  →  4 figures (à adapter aussi pour fps_sustained)")
print("=" * 110)
