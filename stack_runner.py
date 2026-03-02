#!/usr/bin/env python3
"""Run all exchange collectors + spread dashboard as one resilient stack.

- Starts each script in its own subprocess
- Auto-restarts crashed processes
- Writes logs to ./logs
"""

from __future__ import annotations

import argparse
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / "logs"


@dataclass
class ProcSpec:
    name: str
    cmd: list[str]
    cwd: Path


def build_specs(python_bin: str, host: str, port: int) -> list[ProcSpec]:
    return [
        ProcSpec("binance", [python_bin, "binance.py"], ROOT / "binance"),
        ProcSpec("onus", [python_bin, "onus.py"], ROOT / "onus"),
        ProcSpec("bybit", [python_bin, "bybit.py"], ROOT / "bybit"),
        ProcSpec("gate", [python_bin, "gate.py"], ROOT / "gate"),
        ProcSpec(
            "dashboard",
            [python_bin, "spread_dashboard.py", "--host", host, "--port", str(port)],
            ROOT,
        ),
    ]


def start_process(spec: ProcSpec) -> subprocess.Popen:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"{spec.name}.log"
    log_file = log_path.open("ab", buffering=0)

    print(f"[START] {spec.name}: {' '.join(spec.cmd)} (cwd={spec.cwd})")
    return subprocess.Popen(
        spec.cmd,
        cwd=str(spec.cwd),
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )


def terminate_all(processes: dict[str, subprocess.Popen], grace_s: float = 8.0) -> None:
    for name, proc in processes.items():
        if proc.poll() is None:
            print(f"[STOP] terminating {name} pid={proc.pid}")
            proc.terminate()

    deadline = time.time() + grace_s
    while time.time() < deadline:
        if all(proc.poll() is not None for proc in processes.values()):
            return
        time.sleep(0.2)

    for name, proc in processes.items():
        if proc.poll() is None:
            print(f"[KILL] force kill {name} pid={proc.pid}")
            proc.kill()


def run_stack(python_bin: str, host: str, port: int, restart_delay: float) -> int:
    specs = build_specs(python_bin=python_bin, host=host, port=port)
    processes: dict[str, subprocess.Popen] = {}
    stop = False

    def _handle_signal(signum, _frame):
        nonlocal stop
        print(f"[SIGNAL] received {signum}, shutting down stack...")
        stop = True

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    for spec in specs:
        if not spec.cwd.exists():
            print(f"[WARN] skip {spec.name}, cwd missing: {spec.cwd}")
            continue
        processes[spec.name] = start_process(spec)

    if not processes:
        print("[ERROR] no processes started")
        return 1

    print("[RUNNING] stack started. Logs: ./logs/*.log")
    print(f"[DASHBOARD] open http://127.0.0.1:{port} (or server_ip:{port})")

    spec_map = {s.name: s for s in specs}

    while not stop:
        for name, proc in list(processes.items()):
            code = proc.poll()
            if code is None:
                continue

            print(f"[EXIT] {name} pid={proc.pid} code={code}")
            if stop:
                continue

            time.sleep(restart_delay)
            print(f"[RESTART] {name} in {restart_delay:.1f}s")
            processes[name] = start_process(spec_map[name])

        time.sleep(1.0)

    terminate_all(processes)
    print("[DONE] stack stopped")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run parser stack (collectors + dashboard)")
    parser.add_argument("--python", default=sys.executable, help="Python interpreter path")
    parser.add_argument("--host", default="0.0.0.0", help="Dashboard bind host")
    parser.add_argument("--port", default=8080, type=int, help="Dashboard port")
    parser.add_argument("--restart-delay", default=2.0, type=float, help="Restart delay for crashed process")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    raise SystemExit(run_stack(args.python, args.host, args.port, args.restart_delay))
