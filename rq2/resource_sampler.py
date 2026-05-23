"""
RQ2 - Resource sampler (psutil).

Echantillonne CPU%, RSS, num_threads par PID + metriques systeme global,
ecrit un CSV. Tournable en daemon via Ctrl+C ou via signal SIGTERM.

Usage en standalone :
    python resource_sampler.py \
        --pids 1234,5678 \
        --names qgis,flask \
        --output ~/bench_results/rq2/resources.csv \
        --interval 0.5

L'orchestrator.py lance ce script en sous-process pour chaque (system, limit, run).
"""

import argparse
import csv
import os
import signal
import sys
import time
from pathlib import Path

import psutil


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--pids",     required=True, help="comma-separated list of PIDs")
    p.add_argument("--names",    required=True, help="comma-separated names (same order, for the CSV)")
    p.add_argument("--output",   required=True, help="output CSV path")
    p.add_argument("--interval", type=float, default=0.5, help="sampling interval in seconds")
    p.add_argument("--duration", type=float, default=0,   help="auto-stop after N seconds (0 = run until SIGTERM)")
    return p.parse_args()


def main():
    args = parse_args()
    pids  = [int(x) for x in args.pids.split(",")  if x.strip()]
    names = [x.strip() for x in args.names.split(",") if x.strip()]
    if len(pids) != len(names):
        print("ERR: pids and names must have same length", file=sys.stderr)
        sys.exit(1)

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)

    # Attache aux process
    procs = {}
    for pid, name in zip(pids, names):
        try:
            p = psutil.Process(pid)
            p.cpu_percent(None)         # init la mesure CPU%
            procs[pid] = (p, name)
        except psutil.NoSuchProcess:
            print(f"WARN: PID {pid} ({name}) not found, skipping", file=sys.stderr)

    if not procs:
        print("ERR: no valid PIDs to sample", file=sys.stderr)
        sys.exit(1)

    # Init system-wide CPU sampling
    psutil.cpu_percent(None)

    # Setup graceful exit
    stop = {"flag": False}
    def handler(signum, frame):
        stop["flag"] = True
    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT,  handler)

    print(f"[sampler] PIDs={list(procs.keys())} interval={args.interval}s -> {output}")
    started = time.time()

    with open(output, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "ts", "elapsed_s", "pid", "name",
            "cpu_pct", "rss_mb", "num_threads",
            "sys_cpu_pct", "sys_ram_used_pct", "sys_ram_used_mb",
        ])

        while not stop["flag"]:
            t = time.time()
            elapsed = t - started

            # System-wide
            sys_cpu = psutil.cpu_percent(None)
            vm = psutil.virtual_memory()
            sys_ram_pct = vm.percent
            sys_ram_mb = vm.used / 1e6

            # Per-process
            dead = []
            for pid, (proc, name) in procs.items():
                try:
                    cpu = proc.cpu_percent(None)
                    mem = proc.memory_info().rss / 1e6
                    threads = proc.num_threads()
                    writer.writerow([
                        f"{t:.3f}", f"{elapsed:.3f}", pid, name,
                        f"{cpu:.1f}", f"{mem:.1f}", threads,
                        f"{sys_cpu:.1f}", f"{sys_ram_pct:.1f}", f"{sys_ram_mb:.1f}",
                    ])
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    dead.append(pid)

            for pid in dead:
                print(f"[sampler] PID {pid} disappeared, dropping", file=sys.stderr)
                del procs[pid]

            f.flush()

            if not procs:
                print("[sampler] all PIDs dead, exiting", file=sys.stderr)
                break

            if args.duration > 0 and elapsed >= args.duration:
                break

            time.sleep(args.interval)

    print(f"[sampler] done -> {output}")


if __name__ == "__main__":
    main()
