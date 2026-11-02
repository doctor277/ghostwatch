from __future__ import annotations

import gc
import socket
import statistics
import time
import tracemalloc
from types import SimpleNamespace

from app.monitoring.network import NetworkCollector


class ChurningSockets:
    def __init__(self, size: int) -> None:
        self.size = size
        self.cycle = 0

    @staticmethod
    def net_io_counters():
        return SimpleNamespace(bytes_sent=100, bytes_recv=200)

    def net_connections(self, kind):
        base = self.cycle * self.size
        self.cycle += 1
        return (
            SimpleNamespace(
                type=socket.SOCK_STREAM,
                status="ESTABLISHED",
                pid=index % 50 + 1,
                laddr=("127.0.0.1", 10_000 + index % 50_000),
                raddr=(f"203.0.{(index // 250) % 250}.{index % 250 + 1}", 443),
            )
            for index in range(base, base + self.size)
        )

    @staticmethod
    def Process(pid):
        return SimpleNamespace(name=lambda: f"synthetic-{pid}.exe")


def percentile(values: list[float], ratio: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * ratio))]


def main(size: int = 10_000, repeats: int = 20) -> None:
    source = ChurningSockets(size)
    ticks = iter(range(1, repeats + 2))
    collector = NetworkCollector(source, clock=lambda: float(next(ticks)))
    latencies: list[float] = []
    currents: list[int] = []
    tracemalloc.start()
    for _ in range(repeats):
        started = time.perf_counter()
        sample, details = collector.collect()
        latencies.append((time.perf_counter() - started) * 1000)
        gc.collect()
        current, _peak = tracemalloc.get_traced_memory()
        currents.append(current)
        if sample.active_connections != size or len(details) != 250:
            raise RuntimeError("synthetic aggregate/detail invariant failed")
        if len(collector._seen) > collector.lifecycle_limit:  # noqa: SLF001
            raise RuntimeError("lifecycle state exceeded its configured bound")
        if len(collector.recently_closed) > 100:
            raise RuntimeError("recently closed state exceeded its bound")
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    print(f"sockets={size}")
    print(f"repeats={repeats}")
    print(f"latency_min_ms={min(latencies):.2f}")
    print(f"latency_median_ms={statistics.median(latencies):.2f}")
    print(f"latency_p95_ms={percentile(latencies, 0.95):.2f}")
    print(f"latency_max_ms={max(latencies):.2f}")
    print(f"traced_current_bytes={current}")
    print(f"traced_peak_bytes={peak}")
    print(f"traced_current_growth_bytes={currents[-1] - currents[0]}")
    print(f"lifecycle_entries={len(collector._seen)}")  # noqa: SLF001
    print(f"recently_closed_entries={len(collector.recently_closed)}")
    print(f"detail_entries={len(details)}")


if __name__ == "__main__":
    main()
