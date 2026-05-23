"""
RQ2 - Orchestrator.

Pour chaque (system in [qgis, flask], limit, run_id) :
  1. Verifier la stabilite DB
  2. Lancer le system (Flask sous-process / QGIS via qgis_process)
  3. Demarrer le resource_sampler en sous-process attache aux PIDs
  4. Lancer le bench (Playwright pour Flask, qgis_process pour QGIS)
  5. Stopper le sampler, archiver les CSV

Usage :
    # Bench complet (toute la matrice)
    python orchestrator.py

    # Un seul scenario
    python orchestrator.py --system flask --limit 1000 --run-id 1

    # Skip QGIS (pour debug Flask)
    python orchestrator.py --systems flask
"""

import argparse
import asyncio
import csv
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent
# bench_config.py is local to PR/RQ2/ (and identical to ~/bench_config.py)
sys.path.insert(0, str(THIS_DIR))
import bench_config as cfg

import psycopg2


# -----------------------------------------------------------------------------
# Args
# -----------------------------------------------------------------------------
def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--systems",  default="qgis,flask",
                   help="comma-separated: qgis,flask")
    p.add_argument("--limits",   default=None,
                   help="comma-separated limits (default = bench_config.SCENARIO_LIMITS)")
    p.add_argument("--n-runs",   type=int, default=cfg.N_RUNS_PER_SCENARIO)
    p.add_argument("--system",   default=None,
                   help="run only one system (shortcut for --systems X)")
    p.add_argument("--limit",    type=int, default=None,
                   help="run only one limit (shortcut for --limits X)")
    p.add_argument("--run-id",   type=int, default=None,
                   help="run only one run id (shortcut for --n-runs 1)")
    p.add_argument("--repo-root", default=os.environ.get("BENCH_REPO_ROOT", str(Path.home() / "RTDataHub")),
                   help="path to RTDataHub repo on the VM")
    p.add_argument("--flask-port", type=int, default=8095)
    p.add_argument("--skip-validation", action="store_true",
                   help="skip the DB stability check (debug)")
    return p.parse_args()


def systems_to_run(args):
    if args.system:
        return [args.system]
    return [s.strip() for s in args.systems.split(",") if s.strip()]

def limits_to_run(args):
    if args.limit is not None:
        return [args.limit]
    if args.limits:
        return [int(x) for x in args.limits.split(",") if x.strip()]
    return cfg.SCENARIO_LIMITS

def runs_to_do(args):
    if args.run_id is not None:
        return [args.run_id]
    return list(range(1, args.n_runs + 1))


# -----------------------------------------------------------------------------
# DB stability
# -----------------------------------------------------------------------------
def check_db_stable(expected: int = None) -> int:
    conn = psycopg2.connect(
        host=cfg.DB_HOST, port=cfg.DB_PORT,
        dbname=cfg.DB_NAME, user=cfg.DB_USER, password=cfg.DB_PASSWORD,
    )
    cur = conn.cursor()
    cur.execute(cfg.COUNT_VALIDATION_SQL)
    n = cur.fetchone()[0]
    cur.close()
    conn.close()
    if expected is not None and n != expected:
        raise RuntimeError(f"DB count changed: expected {expected}, got {n}")
    return n


# -----------------------------------------------------------------------------
# Flask process
# -----------------------------------------------------------------------------
class FlaskProcess:
    def __init__(self, repo_root: str, port: int):
        self.repo_root = repo_root
        self.port = port
        self.proc: subprocess.Popen | None = None

    def start(self):
        env = os.environ.copy()
        env["PYTHONPATH"] = self.repo_root
        cmd = [
            sys.executable, "-m", "scripts.bench.rq2.flask_bench_routes",
            "--listen-host", "127.0.0.1",
            "--listen-port", str(self.port),
        ]
        print(f"[orch] starting Flask: {' '.join(cmd)}")
        self.proc = subprocess.Popen(
            cmd,
            cwd=self.repo_root,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        # Wait until the port is up
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                import socket
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.5):
                    print(f"[orch] Flask up on port {self.port} (PID {self.proc.pid})")
                    return self.proc.pid
            except OSError:
                time.sleep(0.3)
        raise RuntimeError("Flask failed to start within 15s")

    def stop(self):
        if self.proc:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            print("[orch] Flask stopped")
            self.proc = None


# -----------------------------------------------------------------------------
# Resource sampler
# -----------------------------------------------------------------------------
def start_sampler(pids: list[int], names: list[str], output_csv: Path,
                  interval: float = 0.5) -> subprocess.Popen:
    sampler_path = THIS_DIR / "resource_sampler.py"
    cmd = [
        sys.executable, str(sampler_path),
        "--pids",     ",".join(str(p) for p in pids),
        "--names",    ",".join(names),
        "--output",   str(output_csv),
        "--interval", str(interval),
    ]
    print(f"[orch] starting sampler: {output_csv.name}")
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

def stop_sampler(proc: subprocess.Popen):
    if proc.poll() is None:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


# -----------------------------------------------------------------------------
# Flask bench via Playwright
# -----------------------------------------------------------------------------
async def run_flask_bench_async(limit: int, run_id: int, port: int,
                                 chrome_sampler_csv: Path = None) -> dict:
    """Lance Chromium headless, charge l'URL bench, attend BENCH_RESULT.

    v2 (post-3-agents review) : capture les PIDs Chrome (browser + renderer
    + GPU + utility) via psutil.children(recursive=True) APRÈS page.goto
    (= moment où tous les renderer process sont spawned), et lance un
    sampler dédié sur ces PIDs.
    """
    import psutil
    from playwright.async_api import async_playwright

    duration = cfg.STEADY_STATE_SECONDS
    url = (f"http://127.0.0.1:{port}/?bench_limit={limit}"
           f"&bench_run={run_id}&bench_duration={duration}")
    print(f"[orch] flask bench url: {url}")

    chrome_sampler_proc = None
    chrome_pids = []

    def _chromium_pids() -> set[int]:
        """Snapshot courant de tous les PIDs dont le nom contient 'chrom'."""
        result = set()
        for p in psutil.process_iter(["pid", "name"]):
            try:
                if "chrom" in (p.info["name"] or "").lower():
                    result.add(p.info["pid"])
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        return result

    async with async_playwright() as pw:
        # Snapshot AVANT le lancement pour isoler les nouveaux PIDs Chrome
        pids_before = _chromium_pids()

        # v2 fix : flags anti-throttle pour que requestAnimationFrame tourne
        # à pleine cadence en headless. Sans ces flags, Chromium throttle
        # les RAF à ~1/s quand le tab n'est pas focused/visible (= toujours
        # le cas en headless), ce qui faussait massivement le FPS soutenu.
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--disable-renderer-backgrounding",
                "--disable-background-timer-throttling",
                "--disable-backgrounding-occluded-windows",
                "--disable-features=CalculateNativeWinOcclusion,IntensiveWakeUpThrottling",
                # v2 fix : heap V8 4 GB pour absorber le pic de parse all_day
                # (combine avec le streaming NDJSON côté frontend pour limit=0).
                "--js-flags=--max-old-space-size=4096",
            ],
        )

        # Laisser Chromium spawner ses sous-process (renderer, GPU, utility)
        await asyncio.sleep(1.0)
        pids_after = _chromium_pids()
        chrome_pids = sorted(pids_after - pids_before)
        print(f"[orch] chrome PIDs captured (before/after diff): {chrome_pids}")

        # Lance un sampler dédié Chrome AVANT page.goto pour capturer la RAM
        # dès le chargement (parse JSON, rendu initial, etc.)
        if chrome_sampler_csv and chrome_pids:
            names = ([f"chrome-root"] +
                     [f"chrome-child-{i}" for i in range(len(chrome_pids) - 1)])
            chrome_sampler_proc = start_sampler(chrome_pids, names, chrome_sampler_csv)

        context = await browser.new_context(viewport={"width": 1920, "height": 1080})
        page    = await context.new_page()

        # Capture console (filtrée pour pas spammer)
        page.on("console", lambda m: (
            print(f"[chrome:{m.type}] {m.text}")
            if m.type in ("error", "warning") or "[bench]" in m.text else None
        ))

        # v2 fix : wait_until="load" au lieu de "networkidle" — la page
        # RTDataHub poll en continu (vehicles realtime), donc networkidle
        # ne se résout jamais et timeout après 60s.
        # Timeout 180s pour absorber les payload énormes (limit=0 = all_day
        # peut être ~200 MB de JSON pour 25k trips).
        try:
            await page.goto(url, wait_until="load", timeout=180000)
        except Exception as e:
            print(f"[orch] WARNING: page.goto failed for limit={limit}: {e}")
            if chrome_sampler_proc:
                stop_sampler(chrome_sampler_proc)
            try:
                await browser.close()
            except Exception:
                pass
            return {"chrome_pids": chrome_pids, "result": None, "error": str(e)}

        # Attendre BENCH_RESULT : duration + 90s de marge pour le chargement
        # (all_day 25k trips → ~200 MB NDJSON → ~30s fetch + 60s bench)
        deadline = time.time() + duration + 90
        result = None
        while time.time() < deadline:
            result = await page.evaluate("window.BENCH_RESULT || null")
            if result is not None:
                break
            await asyncio.sleep(0.5)

        # Stop chrome sampler avant browser.close (sinon les PIDs disparaissent)
        if chrome_sampler_proc:
            stop_sampler(chrome_sampler_proc)

        await browser.close()
        return {"chrome_pids": chrome_pids, "result": result}

def run_flask_bench(limit: int, run_id: int, port: int,
                    chrome_sampler_csv: Path = None) -> dict:
    return asyncio.run(run_flask_bench_async(limit, run_id, port, chrome_sampler_csv))


# -----------------------------------------------------------------------------
# QGIS bench (headless via qgis_process)
# -----------------------------------------------------------------------------
def run_qgis_bench(limit: int, run_id: int, repo_root: str) -> int:
    """
    Lance QGIS en mode batch : qgis_process run python:execute --script qgis_bench.py
    En pratique : la facon la plus simple est de lancer un python QGIS via le
    binaire qgis avec --code "exec(open('qgis_bench.py').read())".

    Retourne le PID du process QGIS pour le sampler.
    """
    env = os.environ.copy()
    env["BENCH_LIMIT"]  = str(limit)
    env["BENCH_RUN_ID"] = str(run_id)
    env["PYTHONPATH"]   = repo_root

    # PR/RQ2/ : on utilise bench6_qgis_c6.py qui est dans le même dossier.
    qgis_bench = THIS_DIR / "bench6_qgis_c6.py"

    # Methode 1 : lancer python avec QGIS env (suppose que pyqgis est dans PATH)
    # Methode 2 : qgis --code "exec(open('...').read())"
    # On utilise la methode 2 car elle est plus universelle.
    qgis_bin = shutil.which("qgis") or shutil.which("qgis-bin")
    if not qgis_bin:
        raise RuntimeError("qgis binary not found in PATH")

    cmd = [
        qgis_bin,
        "--nologo",
        "--code", str(qgis_bench),
    ]
    print(f"[orch] QGIS bench: limit={limit} run={run_id}")
    proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    # Attendre que qgis_bench.py ait ecrit son PID
    pid_file = cfg.RQ2_DIR / "qgis.pid"
    deadline = time.time() + 30
    qgis_pid = None
    while time.time() < deadline:
        if pid_file.exists():
            try:
                qgis_pid = int(pid_file.read_text().strip())
                break
            except Exception:
                pass
        time.sleep(0.3)
    if qgis_pid is None:
        proc.terminate()
        raise RuntimeError("QGIS bench did not write PID file within 30s")

    return qgis_pid, proc


# -----------------------------------------------------------------------------
# Postgres backend PIDs (server-side)
# Note : si Postgres tourne sur la VM, on peut sampler. Si Postgres tourne
# ailleurs (serveur via tunnel), on ne peut pas l'attacher en local. On
# loggue ce qu'on peut.
# -----------------------------------------------------------------------------
def find_postgres_pids() -> list[int]:
    """Retourne les PIDs des process postgres locaux (si presents sur la VM)."""
    import psutil
    pids = []
    for p in psutil.process_iter(["name"]):
        try:
            if p.info["name"] and "postgres" in p.info["name"].lower():
                pids.append(p.pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return pids


# -----------------------------------------------------------------------------
# Main loop
# -----------------------------------------------------------------------------
def main():
    args = parse_args()

    cfg.ensure_output_dir()
    output_dir = cfg.RQ2_DIR
    output_dir.mkdir(parents=True, exist_ok=True)
    print(f"[orch] outputs: {output_dir}")

    if not args.skip_validation:
        n = check_db_stable()
        print(f"[orch] DB validation OK: {n} trips on {cfg.BENCH_DATE}")
        expected_count = n
    else:
        expected_count = None

    systems = systems_to_run(args)
    limits  = limits_to_run(args)
    runs    = runs_to_do(args)

    # Flask : un seul process pour toute la session
    flask_proc = None
    flask_pid  = None
    if "flask" in systems:
        flask_proc = FlaskProcess(args.repo_root, args.flask_port)
        flask_pid  = flask_proc.start()

    try:
        for system in systems:
            for limit in limits:
                for run_id in runs:
                    print("\n" + "=" * 64)
                    print(f"  {system} | limit={limit} | run={run_id}")
                    print("=" * 64)

                    # DB stable ?
                    if expected_count is not None:
                        check_db_stable(expected_count)

                    # PIDs a sampler
                    pids, names = [], []
                    if system == "flask" and flask_pid:
                        pids.append(flask_pid); names.append("flask")
                    pids.extend(find_postgres_pids())
                    names.extend(["postgres"] * (len(pids) - len(names)))

                    sampler_csv = output_dir / f"resources_{system}_limit{limit}_run{run_id}.csv"

                    # Lancer le bench
                    if system == "flask":
                        # 2 samplers : (a) flask+postgres démarré avant + (b) Chrome
                        # démarré DANS run_flask_bench une fois les PIDs renderer
                        # spawned (cf. v2 post-3-agents review).
                        sampler = start_sampler(pids, names, sampler_csv)
                        chrome_sampler_csv = output_dir / f"resources_chrome_limit{limit}_run{run_id}.csv"
                        try:
                            print(f"[orch] running flask bench...")
                            res = run_flask_bench(limit, run_id, args.flask_port,
                                                  chrome_sampler_csv=chrome_sampler_csv)
                            print(f"[orch] flask bench done: {res.get('result')}")
                            print(f"[orch] chrome PIDs sampled: {res.get('chrome_pids')}")
                        finally:
                            stop_sampler(sampler)

                    elif system == "qgis":
                        try:
                            qgis_pid, qgis_proc = run_qgis_bench(limit, run_id, args.repo_root)
                            pids.append(qgis_pid); names.append("qgis")
                            sampler = start_sampler(pids, names, sampler_csv)
                            try:
                                qgis_proc.wait(timeout=cfg.STEADY_STATE_SECONDS + 120)
                            except subprocess.TimeoutExpired:
                                print("[orch] qgis bench timeout, killing")
                                qgis_proc.kill()
                            finally:
                                stop_sampler(sampler)
                        except Exception as e:
                            print(f"[orch] qgis bench error: {e}", file=sys.stderr)

                    # Pause entre runs pour laisser refroidir le cache et liberer la RAM
                    time.sleep(2)

    finally:
        if flask_proc:
            flask_proc.stop()

    print("\n[orch] all done -- run analyze.py to produce figures")


if __name__ == "__main__":
    main()
