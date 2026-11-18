"""Bounded live smoke or soak test of GhostWatch on Windows, with a disposable database.

Usage (from the repository root, in the project virtual environment):

    python scripts/live_smoke.py --seconds 300      # bounded smoke test
    python scripts/live_smoke.py --seconds 28800    # eight-hour soak

Starts the real application (real collectors) on 127.0.0.1:8765 against a database in
a new temporary directory, never data/ghostwatch.db. It samples /api/health,
/api/overview and the WebSocket stream, compares system CPU with GetSystemTimes and
memory with psutil, records GhostWatch's own CPU, memory and handles, checks the
Host/Origin protections and loopback binding, shuts down with CTRL_BREAK, and checks
database integrity. Output is aggregate numbers only: no process names, paths or
addresses. Port 8765 must be free.
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import json
import os
import shutil
import signal
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import psutil
import websockets

BASE = "http://127.0.0.1:8765"
REPO = Path(__file__).resolve().parents[1]
LAUNCHER = """from pathlib import Path
from app.main import create_app
app = create_app(Path(__file__).resolve().parent / "smoke.db")
"""


def get(path: str, headers: dict[str, str] | None = None) -> tuple[int, object]:
    request = urllib.request.Request(BASE + path, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as exc:
        return exc.code, None


def system_times() -> tuple[int, int]:
    from ctypes import wintypes

    idle, kernel, user = wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME()
    ctypes.WinDLL("kernel32").GetSystemTimes(
        ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
    )

    def value(item: wintypes.FILETIME) -> int:
        return (item.dwHighDateTime << 32) | item.dwLowDateTime

    return value(idle), value(kernel) + value(user)  # Kernel time includes idle.


def percentile(values: list[float], fraction: float) -> float:
    return sorted(values)[max(0, int(fraction * len(values)) - 1)]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seconds", type=float, default=300.0)
    parser.add_argument("--keep", action="store_true", help="keep the temporary database")
    args = parser.parse_args()
    if sys.platform != "win32":
        sys.exit("live_smoke.py measures Windows collectors and must run on Windows")

    workdir = Path(tempfile.mkdtemp(prefix="ghostwatch-smoke-"))
    (workdir / "smoke_server.py").write_text(LAUNCHER, encoding="utf-8")
    # The base interpreter (not the venv launcher) so the server's own exit is observed.
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(p for p in sys.path if p.endswith("site-packages")))
    server = subprocess.Popen(
        [sys._base_executable, "-m", "uvicorn", "smoke_server:app", "--app-dir", str(workdir),
         "--host", "127.0.0.1", "--port", "8765", "--log-level", "warning"],
        cwd=REPO, env=env, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
    )
    try:
        run(server, workdir, args.seconds)
    finally:
        if server.poll() is None:
            server.kill()
        if not args.keep:
            shutil.rmtree(workdir, ignore_errors=True)
        else:
            print(f"database kept in {workdir}")


def run(server: subprocess.Popen, workdir: Path, seconds: float) -> None:
    for _ in range(60):
        try:
            if get("/api/health")[0] == 200:
                break
        except OSError:
            time.sleep(0.5)
    else:
        sys.exit("server did not start")

    frames: list[int] = []
    sequences: list[int] = []
    intervals: list[float] = []
    stop = threading.Event()

    async def watch() -> None:
        async with websockets.connect(
            "ws://127.0.0.1:8765/ws/telemetry", origin=BASE, max_size=8_000_000
        ) as socket:
            last = time.monotonic()
            while not stop.is_set():
                try:
                    message = await asyncio.wait_for(socket.recv(), timeout=1.0)
                except TimeoutError:
                    continue
                data = json.loads(message)
                if "sequence" in data:
                    now = time.monotonic()
                    sequences.append(data["sequence"])
                    intervals.append(now - last)
                    frames.append(len(message))
                    last = now

    watcher = threading.Thread(target=lambda: asyncio.run(watch()), daemon=True)
    watcher.start()
    processes = [psutil.Process(server.pid)]
    processes += processes[0].children(recursive=True)
    for process in processes:
        process.cpu_percent(None)
    health_rows, cpu_errors, memory_errors, own = [], [], [], []
    previous, previous_sequence = system_times(), None
    started = time.monotonic()
    while time.monotonic() - started < seconds:
        time.sleep(5)
        _, health = get("/api/health")
        health_rows.append(health)
        _, overview = get("/api/overview")
        now = system_times()
        system = overview["system"]
        if overview["sequence"] != previous_sequence and "cpu_percent" not in system["unavailable_metrics"]:
            reference = 100.0 * (1 - (now[0] - previous[0]) / (now[1] - previous[1]))
            cpu_errors.append(abs(system["cpu_percent"] - reference))
        memory_errors.append(abs(system["memory_percent"] - psutil.virtual_memory().percent))
        previous, previous_sequence = now, overview["sequence"]
        own.append((
            sum(p.cpu_percent(None) for p in processes) / psutil.cpu_count(),
            sum(p.memory_info().rss for p in processes) / 2**20,
            sum(p.num_handles() for p in processes),
            overview["collection_duration_ms"],
        ))

    security = {"foreign Host rejected": get("/api/health", {"Host": "attacker.example"})[0] == 400}
    request = urllib.request.Request(
        BASE + "/api/settings", method="PUT", data=b"{}",
        headers={"Origin": "https://attacker.example", "Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(request, timeout=10)
        security["cross-origin mutation rejected"] = False
    except urllib.error.HTTPError as exc:
        security["cross-origin mutation rejected"] = exc.code == 403
    listeners = [
        c.laddr.ip for c in psutil.net_connections("tcp")
        if c.status == "LISTEN" and c.laddr.port == 8765
    ]
    security["loopback only"] = bool(listeners) and set(listeners) == {"127.0.0.1"}
    workers = get("/api/health")[1]["workers"]

    stop.set()
    watcher.join(5)
    server.send_signal(signal.CTRL_BREAK_EVENT)
    # uvicorn shuts down gracefully, then re-raises CTRL_BREAK, which exits with 3.
    exit_code = server.wait(60)
    with sqlite3.connect(workdir / "smoke.db") as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        samples = connection.execute("SELECT COUNT(*) FROM telemetry_samples").fetchone()[0]

    gaps = sum(1 for a, b in zip(sequences, sequences[1:], strict=False) if b != a + 1)
    print(f"duration {seconds:.0f} s; frames {len(frames)}; sequence gaps {gaps}; "
          f"frame interval p50 {statistics.median(intervals[1:]):.2f} s, max {max(intervals[1:]):.2f} s")
    print(f"health statuses {sorted({h['status'] for h in health_rows})}; snapshot age max "
          f"{max(h['snapshot_age_seconds'] or 0 for h in health_rows):.1f} s; history dropped max "
          f"{max(h['storage']['history_dropped_samples'] for h in health_rows)}; workers {workers}")
    print(f"system CPU vs GetSystemTimes (5 s windows, up to 2 s frame skew): MAE "
          f"{statistics.mean(cpu_errors):.2f} pts, p95 {percentile(cpu_errors, 0.95):.2f}; "
          f"memory vs psutil MAE {statistics.mean(memory_errors):.2f} pts")
    print(f"GhostWatch CPU mean {statistics.mean(o[0] for o in own):.2f}% of machine, max "
          f"{max(o[0] for o in own):.2f}; RSS {min(o[1] for o in own):.0f}-{max(o[1] for o in own):.0f} MiB; "
          f"handles {min(o[2] for o in own)}-{max(o[2] for o in own)}; collection p50 "
          f"{statistics.median(o[3] for o in own):.0f} ms, p95 {percentile([o[3] for o in own], 0.95):.0f} ms")
    print(f"security {security}")
    print(f"shutdown exit {exit_code}; integrity {integrity}; samples stored {samples}")


if __name__ == "__main__":
    main()
