from __future__ import annotations

import sys
import threading
import time
from types import SimpleNamespace

import psutil
import pytest

from app.monitoring.system import SystemCollector


class ThreadBaselinePsutil:
    """psutil 7.1 semantics: cpu_percent(interval=None) keeps one baseline per thread."""

    def __init__(self) -> None:
        self.times = SimpleNamespace(user=10.0, system=2.0, idle=88.0, interrupt=0.0, dpc=0.0)
        self._baselines: dict[int, SimpleNamespace] = {}

    def cpu_times(self, percpu=False):
        return SimpleNamespace(**vars(self.times))

    def cpu_percent(self, interval=None):
        thread = threading.get_ident()
        previous = self._baselines.get(thread)
        self._baselines[thread] = self.cpu_times()
        if previous is None:
            return 0.0
        total = sum(vars(self.times).values()) - sum(vars(previous).values())
        idle = self.times.idle - previous.idle
        return round(100.0 * (total - idle) / total, 1)

    def advance(self, busy: float, idle: float) -> None:
        self.times.user += busy
        self.times.idle += idle

    @staticmethod
    def virtual_memory():
        return SimpleNamespace(percent=50.0, used=500, total=1000)

    @staticmethod
    def disk_usage(path):
        return SimpleNamespace(percent=10.0)

    @staticmethod
    def pids():
        return [1]

    @staticmethod
    def boot_time():
        return 1.0


def collect_on_new_thread(collector):
    result = {}
    worker = threading.Thread(target=lambda: result.setdefault("sample", collector.collect()))
    worker.start()
    worker.join()
    return result["sample"]


def test_cpu_does_not_depend_on_the_executor_thread():
    system = ThreadBaselinePsutil()
    collector = SystemCollector(system, performance_memory_reader=lambda: {})
    collect_on_new_thread(collector)

    samples = []
    for busy in (4.0, 1.0, 9.0):
        system.advance(busy=busy, idle=10.0 - busy)
        samples.append(collect_on_new_thread(collector))  # Each on a different thread.

    assert [sample.cpu_percent for sample in samples] == [40.0, 10.0, 90.0]
    assert all("cpu_percent" not in sample.unavailable_metrics for sample in samples)


def test_first_cpu_sample_is_unavailable_not_zero():
    system = ThreadBaselinePsutil()
    collector = SystemCollector(system, performance_memory_reader=lambda: {})

    first = collector.collect()

    assert "cpu_percent" in first.unavailable_metrics
    assert first.collection_error is None  # Unmeasured, not failed.


def test_cpu_counter_reset_is_unavailable_rather_than_negative():
    system = ThreadBaselinePsutil()
    collector = SystemCollector(system, performance_memory_reader=lambda: {})
    collector.collect()
    system.times = SimpleNamespace(user=1.0, system=0.0, idle=1.0, interrupt=0.0, dpc=0.0)

    sample = collector.collect()

    assert "cpu_percent" in sample.unavailable_metrics
    system.advance(busy=1.0, idle=1.0)
    assert collector.collect().cpu_percent == 50.0


@pytest.mark.skipif(sys.platform != "win32", reason="live Windows counters")
def test_live_cpu_is_measured_across_threads():
    collector = SystemCollector()
    collect_on_new_thread(collector)
    time.sleep(0.3)

    sample = collect_on_new_thread(collector)

    assert "cpu_percent" not in sample.unavailable_metrics
    assert 0.0 <= sample.cpu_percent <= 100.0
    # Same accounting as psutil's system-wide figure.
    assert set(psutil.cpu_times()._fields) >= {"user", "system", "idle"}
