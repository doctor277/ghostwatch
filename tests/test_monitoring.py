from __future__ import annotations

import socket
from types import SimpleNamespace

import psutil

from app.detection.rules import evaluate_rules
from app.models import ConnectionInfo, Settings
from app.monitoring.network import NetworkCollector
from app.monitoring.processes import ProcessCollector
from app.monitoring.system import SystemCollector


class FakeSystemPsutil:
    @staticmethod
    def virtual_memory():
        return SimpleNamespace(percent=62.5, used=625, total=1000)

    @staticmethod
    def cpu_percent(interval=None):
        return 17.4

    @staticmethod
    def disk_usage(path):
        return SimpleNamespace(percent=41.0)

    @staticmethod
    def pids():
        return [1, 2, 3]

    @staticmethod
    def boot_time():
        return 1.0


def test_system_collector_collects_cpu_and_memory(monkeypatch):
    monkeypatch.setattr("app.monitoring.system.time.time", lambda: 101.0)
    sample = SystemCollector(FakeSystemPsutil).collect()
    assert sample.cpu_percent == 17.4
    assert sample.memory_percent == 62.5
    assert sample.process_count == 3
    assert sample.uptime_seconds == 100


def test_system_collector_reports_available_commit_cache_and_paging(monkeypatch):
    class MemoryPsutil(FakeSystemPsutil):
        @staticmethod
        def virtual_memory():
            return SimpleNamespace(percent=75, used=750, total=1000, available=250)

        @staticmethod
        def swap_memory():
            return SimpleNamespace(used=200, total=500, percent=40)

    monkeypatch.setattr("app.monitoring.system.time.time", lambda: 101.0)
    sample = SystemCollector(
        MemoryPsutil,
        performance_memory_reader=lambda: {
            "memory_available": 240,
            "memory_cached": 150,
            "commit_used": 1200,
            "commit_limit": 2000,
        },
    ).collect()

    assert sample.memory_available == 240
    assert sample.memory_cached == 150
    assert sample.commit_used == 1200
    assert sample.commit_limit == 2000
    assert sample.commit_percent == 60.0
    assert sample.pagefile_used == 200
    assert sample.pagefile_total == 500


def test_system_collector_prefers_native_os_process_count():
    sample = SystemCollector(
        FakeSystemPsutil,
        performance_memory_reader=lambda: {"system_process_count": 212},
    ).collect()

    assert sample.process_count == 212


def test_paging_failure_is_explicit_without_losing_physical_memory():
    class PagingFailure(FakeSystemPsutil):
        @staticmethod
        def swap_memory():
            raise RuntimeError("private provider detail")

    sample = SystemCollector(PagingFailure).collect()

    assert sample.memory_total == 1000
    assert sample.pagefile_total is None
    assert "pagefile_total" in sample.unavailable_metrics
    assert "RuntimeError" in (sample.collection_error or "")
    assert "private provider detail" not in (sample.collection_error or "")


def test_network_rates_and_counter_reset():
    collector = NetworkCollector(clock=lambda: 0)
    assert collector.calculate_rates(100, 200, 1) == (0, 0)
    assert collector.calculate_rates(300, 500, 3) == (100, 150)
    assert collector.calculate_rates(10, 20, 5) == (0, 0)
    assert collector.calculate_rates(30, 60, 7) == (10, 20)


def test_network_zero_elapsed_is_safe():
    collector = NetworkCollector()
    collector.calculate_rates(100, 100, 1)
    assert collector.calculate_rates(200, 200, 1) == (0, 0)


class PermissionDeniedNetwork:
    @staticmethod
    def net_io_counters():
        return SimpleNamespace(bytes_sent=100, bytes_recv=200)

    @staticmethod
    def net_connections(kind):
        raise psutil.AccessDenied(pid=1)


def test_connection_permission_error_is_reported():
    collector = NetworkCollector(PermissionDeniedNetwork, clock=lambda: 1)
    sample, connections = collector.collect()
    assert connections == []
    assert "AccessDenied" in sample.collection_error
    assert sample.connection_collection_reason == "permission_denied"


def test_mid_enumeration_permission_error_is_classified_without_partial_counts():
    class Interrupted(PermissionDeniedNetwork):
        @staticmethod
        def Process(pid):
            return SimpleNamespace(name=lambda: "synthetic.exe")

        @staticmethod
        def net_connections(kind):
            def rows():
                yield SimpleNamespace(
                    type=socket.SOCK_STREAM,
                    status="ESTABLISHED",
                    pid=7,
                    laddr=("127.0.0.1", 12345),
                    raddr=("127.0.0.1", 443),
                )
                raise psutil.AccessDenied(pid=1)

            return rows()

    sample, details = NetworkCollector(Interrupted, clock=lambda: 1).collect()

    assert details == []
    assert sample.connection_collection_status == "unavailable"
    assert sample.connection_collection_reason == "permission_denied"
    assert "active_connections" in sample.unavailable_metrics


class VanishingProcess:
    @property
    def info(self):
        raise psutil.NoSuchProcess(pid=99)


class ValidProcess:
    info = {
        "pid": 7,
        "name": "sample.exe",
        "cpu_percent": 2.5,
        "memory_percent": 1.2,
        "memory_info": SimpleNamespace(rss=1234),
        "status": "running",
    }

    def name(self):
        return "sample.exe"

    def create_time(self):
        return 1_700_000_000.0


class FakeProcessPsutil:
    @staticmethod
    def Process(pid):
        assert pid == 7
        return ValidProcess()

    @staticmethod
    def process_iter(attrs, ad_value):
        return [VanishingProcess(), ValidProcess()]

    @staticmethod
    def cpu_count():
        return 1


def test_process_disappearance_is_ignored():
    connection = ConnectionInfo(
        pid=7,
        process_name="sample.exe",
        local_address="127.0.0.1",
        local_port=1,
        remote_address="—",
        remote_port=None,
        protocol="TCP",
        status="LISTEN",
    )
    result = ProcessCollector(FakeProcessPsutil).collect([connection])
    assert len(result) == 1
    assert result[0].name == "sample.exe"
    assert result[0].connection_count == 1


def test_idle_process_is_excluded_from_consumer_rankings():
    class IdleProcess:
        info = {
            "pid": 0,
            "name": "System Idle Process",
            "cpu_percent": 99.0,
            "memory_percent": 0.0,
            "memory_info": SimpleNamespace(rss=0),
            "status": "running",
        }

    class Processes(FakeProcessPsutil):
        @staticmethod
        def process_iter(attrs, ad_value):
            return [IdleProcess(), ValidProcess()]

    collector = ProcessCollector(Processes)
    result = collector.collect([])

    assert [item.name for item in result] == ["sample.exe"]
    assert collector.enumerated_count == 2
    assert collector.idle_excluded_count == 1


def test_process_enumeration_permission_failure_has_safe_status():
    class Denied:
        @staticmethod
        def cpu_count():
            return 1

        @staticmethod
        def process_iter(attrs, ad_value):
            raise psutil.AccessDenied(pid=1)

    collector = ProcessCollector(Denied)
    assert collector.collect([]) == []
    assert collector.collection_status == "unavailable"
    assert "access was denied" in collector.collection_detail


def test_network_address_tuple_compatibility():
    assert NetworkCollector._address(("127.0.0.1", 443)) == ("127.0.0.1", 443)
    assert NetworkCollector._address(()) == ("—", None)


class ManyConnectionsPsutil:
    @staticmethod
    def net_io_counters():
        return SimpleNamespace(bytes_sent=100, bytes_recv=200)

    @staticmethod
    def net_connections(kind):
        return [
            SimpleNamespace(
                type=socket.SOCK_STREAM,
                status="ESTABLISHED",
                pid=7,
                laddr=("127.0.0.1", 10000 + index),
                raddr=("127.0.0.1", 443),
            )
            for index in range(300)
        ]

    @staticmethod
    def Process(pid):
        return SimpleNamespace(name=lambda: "sample.exe")


def test_full_connection_counts_preserve_bounded_detail_rows(system_sample):
    collector = NetworkCollector(ManyConnectionsPsutil, clock=lambda: 1)
    sample, connections = collector.collect()
    assert sample.active_connections == 300
    assert sample.established_connections == 300
    assert len(connections) == 250
    assert collector.connection_counts_by_pid[7] == 300
    connection_rule = next(
        rule
        for rule in evaluate_rules(
            system_sample,
            sample,
            Settings(connection_threshold=275),
            previous_connection_count=None,
        )
        if rule.key == "connections"
    )
    assert connection_rule.breached

    processes = ProcessCollector(FakeProcessPsutil).collect(
        connections, collector.connection_counts_by_pid
    )
    assert processes[0].connection_count == 300


class FailedCountersPsutil(ManyConnectionsPsutil):
    @staticmethod
    def net_io_counters():
        raise OSError("unavailable")


def test_network_counter_failure_returns_degraded_sample():
    sample, connections = NetworkCollector(FailedCountersPsutil, clock=lambda: 1).collect()
    assert sample.bytes_sent_per_second == 0
    assert sample.established_connections == 300
    assert "Network counters unavailable" in sample.collection_error
    assert len(connections) == 250


class FailedSystemPsutil:
    @staticmethod
    def virtual_memory():
        raise OSError("unavailable")

    cpu_percent = virtual_memory
    disk_usage = virtual_memory
    pids = virtual_memory
    boot_time = virtual_memory


def test_system_collection_failures_return_degraded_sample():
    sample = SystemCollector(FailedSystemPsutil).collect()
    assert sample.cpu_percent == 0
    assert sample.memory_total == 0
    assert sample.process_count == 0
    assert sample.uptime_seconds == 0
    assert sample.disk_percent is None
    assert "Memory telemetry unavailable" in sample.collection_error
