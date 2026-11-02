from __future__ import annotations

import socket
import time
import tracemalloc
from types import SimpleNamespace

from app.monitoring.network import NetworkCollector


def row(index: int):
    return SimpleNamespace(
        type=socket.SOCK_STREAM,
        status="ESTABLISHED",
        pid=index % 25 + 1,
        laddr=("127.0.0.1", 10_000 + index),
        raddr=(f"203.0.{index // 250}.{index % 250 + 1}", 443),
    )


def run(size: int) -> tuple[float, int, int, int]:
    class SyntheticPsutil:
        @staticmethod
        def net_io_counters():
            return SimpleNamespace(bytes_sent=100, bytes_recv=200)

        @staticmethod
        def net_connections(kind):
            return (row(index) for index in range(size))

        @staticmethod
        def Process(pid):
            return SimpleNamespace(name=lambda: f"synthetic-{pid}.exe")

    collector = NetworkCollector(SyntheticPsutil, clock=lambda: 1)
    tracemalloc.start()
    started = time.perf_counter()
    sample, details = collector.collect()
    elapsed_ms = (time.perf_counter() - started) * 1000
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return elapsed_ms, peak, sample.active_connections, len(details)


def main() -> None:
    print("sockets,elapsed_ms,peak_bytes,aggregate_count,detail_count")
    for size in (500, 1_000, 5_000, 10_000):
        elapsed_ms, peak, aggregate, details = run(size)
        print(f"{size},{elapsed_ms:.2f},{peak},{aggregate},{details}")


if __name__ == "__main__":
    main()
