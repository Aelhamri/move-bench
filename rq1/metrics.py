"""
RQ1-FINAL — RunMonitor : capture CPU% + RAM peak pendant l'exec d'une condition

Utilisé par bench5_master.py pour mesurer en background pendant chaque
exec(condition_script). Sauve dans `<bench_results>/<dataset>/rq1/metrics/<cid>.json`.

Méthodologie :
  - CPU% : delta cpu_times() / wall_time × 100  (par core, peut dépasser 100% sur
           multi-thread mais nos benches sont mono-thread parallel-rendering OFF)
  - RAM  : sampling thread @ 0.5s intervalle, retourne peak RSS

Note : les benches partagent la même session QGIS, donc RAM cumule entre conditions.
On capture donc à la fois le baseline (au démarrage de la condition) et le peak.
"""
import json
import time
import threading
from pathlib import Path

try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False


class RunMonitor:
    def __init__(self, condition_id, dataset, output_dir):
        self.cid = condition_id
        self.dataset = dataset
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._proc = psutil.Process() if HAS_PSUTIL else None
        self._ram_samples = []
        self._stop = threading.Event()
        self._thread = None
        self._t0 = None
        self._cpu_before = None
        self._ram_baseline = None

    def start(self):
        if not HAS_PSUTIL:
            return
        self._ram_baseline = self._proc.memory_info().rss / 1e6
        self._cpu_before = self._proc.cpu_times()
        self._t0 = time.perf_counter()
        self._stop.clear()
        self._ram_samples = []

        def sampler():
            while not self._stop.wait(0.5):
                try:
                    self._ram_samples.append(self._proc.memory_info().rss / 1e6)
                except Exception:
                    break

        self._thread = threading.Thread(target=sampler, daemon=True)
        self._thread.start()

    def stop(self):
        if not HAS_PSUTIL:
            return None
        wall_seconds = time.perf_counter() - self._t0
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

        cpu_after = self._proc.cpu_times()
        cpu_user = cpu_after.user - self._cpu_before.user
        cpu_sys  = cpu_after.system - self._cpu_before.system
        cpu_total = cpu_user + cpu_sys
        cpu_percent = 100.0 * cpu_total / wall_seconds if wall_seconds > 0 else 0.0

        ram_after = self._proc.memory_info().rss / 1e6
        ram_peak = max(self._ram_samples) if self._ram_samples else ram_after

        metrics = {
            'condition_id':       self.cid,
            'dataset':            self.dataset,
            'wall_seconds':       wall_seconds,
            'cpu_user_seconds':   cpu_user,
            'cpu_system_seconds': cpu_sys,
            'cpu_percent_avg':    cpu_percent,
            'ram_baseline_mb':    self._ram_baseline,
            'ram_after_mb':       ram_after,
            'ram_peak_mb':        ram_peak,
            'ram_delta_mb':       ram_after - self._ram_baseline,
            'ram_peak_delta_mb':  ram_peak - self._ram_baseline,
            'n_ram_samples':      len(self._ram_samples),
        }

        path = self.output_dir / f'{self.cid}_metrics.json'
        with open(path, 'w') as f:
            json.dump(metrics, f, indent=2)

        return metrics
