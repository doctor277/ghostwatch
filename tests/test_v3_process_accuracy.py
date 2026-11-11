from __future__ import annotations

import ctypes
import os
import struct
import time
from datetime import UTC, datetime
from types import SimpleNamespace

import psutil
import pytest

from app.ai import LocalAIAnalyst
from app.config import MAX_PROCESSES
from app.models import ProcessInfo
from app.monitoring import windows_process_metrics as native
from app.monitoring.processes import ProcessCollector
from app.monitoring.windows_process_metrics import (
    ProcessMetricReading,
    WindowsProcessMetrics,
    parse_process_snapshot,
)
from app.service import TelemetryService
from app.storage.database import Database

INTERVAL = 10.0
PHYSICAL_MEMORY = 16 * 1024**3
FILETIME_UNIX_EPOCH = 116_444_736_000_000_000
STATUS_INFO_LENGTH_MISMATCH = 0xC0000004
requires_native = pytest.mark.skipif(
    not native.native_supported(), reason="native snapshot needs 64-bit Windows Python"
)


# --- synthetic SystemProcessInformation buffers ---------------------------------


def snapshot_bytes(entries: list[dict]) -> bytes:
    """Build x64 SYSTEM_PROCESS_INFORMATION entries; neighbouring fields hold decoys."""
    chunks = []
    for index, entry in enumerate(entries):
        threads = entry.get("threads", 0)
        chunk = bytearray(0x100 + threads * 0x50 + entry.get("padding", 0))
        created = entry.get("created")
        struct.pack_into("<I", chunk, 0x04, threads)
        struct.pack_into("<Q", chunk, 0x18, 0xDEAD)  # CycleTime decoy
        struct.pack_into(
            "<q",
            chunk,
            0x20,
            0 if created is None else int(created * 10_000_000) + FILETIME_UNIX_EPOCH,
        )
        struct.pack_into("<q", chunk, 0x28, int(entry.get("user", 0.0) * 10_000_000))
        struct.pack_into("<q", chunk, 0x30, int(entry.get("kernel", 0.0) * 10_000_000))
        struct.pack_into("<Q", chunk, 0x50, entry["pid"])
        struct.pack_into("<Q", chunk, 0x58, 0xBEEF)  # parent PID decoy
        struct.pack_into("<Q", chunk, 0x88, entry.get("rss", 0) * 3)  # peak working set
        struct.pack_into("<Q", chunk, 0x90, entry.get("rss", 0))
        struct.pack_into("<Q", chunk, 0x98, 0xF00D)
        last = index == len(entries) - 1
        struct.pack_into("<I", chunk, 0x00, 0 if last else len(chunk))
        chunks.append(bytes(chunk))
    return b"".join(chunks)


OWN_PID = 4242
OWN = {"pid": OWN_PID, "created": 1_700_000_000.0, "user": 3.0, "kernel": 1.0, "rss": 50_000_000}


class FakeSystem:
    """psutil stand-in for the reader's cross-check of its own process."""

    def __init__(self, pids=(4, OWN_PID, 100), *, created=1_700_000_000.0, cpu=4.0, rss=50_000_000):
        self._pids = list(pids)
        self.own = SimpleNamespace(
            cpu_times=lambda: SimpleNamespace(user=cpu * 0.75, system=cpu * 0.25),
            create_time=lambda: created,
            memory_info=lambda: SimpleNamespace(rss=rss),
        )

    def pids(self):
        return list(self._pids)

    def Process(self, pid):
        assert pid == OWN_PID
        return self.own

    @staticmethod
    def virtual_memory():
        return SimpleNamespace(total=PHYSICAL_MEMORY)


class FakeNtQuery:
    def __init__(self, data: bytes, status: int = 0) -> None:
        self.data = data
        self.status = status
        self.sizes: list[int] = []

    def __call__(self, info_class, buffer, size, returned):
        assert info_class == 5
        self.sizes.append(size)
        returned.value = len(self.data)
        if self.status:
            return self.status
        if size < len(self.data):
            return STATUS_INFO_LENGTH_MISMATCH
        ctypes.memmove(buffer, self.data, len(self.data))
        return 0


def make_reader(entries=None, *, system=None, status=0):
    query = FakeNtQuery(
        snapshot_bytes(entries if entries is not None else [{"pid": 4}, OWN, {"pid": 100}]),
        status,
    )
    reader = WindowsProcessMetrics(system or FakeSystem(), nt_query=query, own_pid=OWN_PID)
    return reader, query


# --- native layout and parsing --------------------------------------------------


@pytest.mark.skipif(struct.calcsize("P") != 8, reason="64-bit layout")
def test_layout_offsets_match_documented_winternl_structures():
    class UnicodeString(ctypes.Structure):
        _fields_ = [("Length", ctypes.c_ushort), ("MaximumLength", ctypes.c_ushort),
                    ("Buffer", ctypes.c_void_p)]

    # SYSTEM_PROCESS_INFORMATION exactly as published in winternl.h.
    class SystemProcessInformation(ctypes.Structure):
        _fields_ = [
            ("NextEntryOffset", ctypes.c_ulong), ("NumberOfThreads", ctypes.c_ulong),
            ("Reserved1", ctypes.c_byte * 48), ("ImageName", UnicodeString),
            ("BasePriority", ctypes.c_long), ("UniqueProcessId", ctypes.c_void_p),
            ("Reserved2", ctypes.c_void_p), ("HandleCount", ctypes.c_ulong),
            ("SessionId", ctypes.c_ulong), ("Reserved3", ctypes.c_void_p),
            ("PeakVirtualSize", ctypes.c_size_t), ("VirtualSize", ctypes.c_size_t),
            ("Reserved4", ctypes.c_ulong), ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t), ("Reserved5", ctypes.c_void_p),
            ("QuotaPagedPoolUsage", ctypes.c_size_t), ("Reserved6", ctypes.c_void_p),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t), ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t), ("PrivatePageCount", ctypes.c_size_t),
            ("Reserved7", ctypes.c_longlong * 6),
        ]

    class SystemThreadInformation(ctypes.Structure):
        _fields_ = [
            ("Reserved1", ctypes.c_longlong * 3), ("Reserved2", ctypes.c_ulong),
            ("StartAddress", ctypes.c_void_p), ("ClientId", ctypes.c_void_p * 2),
            ("Priority", ctypes.c_long), ("BasePriority", ctypes.c_long),
            ("Reserved3", ctypes.c_ulong), ("ThreadState", ctypes.c_ulong),
            ("WaitReason", ctypes.c_ulong),
        ]

    assert ctypes.sizeof(SystemProcessInformation) == native._PROCESS_ENTRY_SIZE
    assert ctypes.sizeof(SystemThreadInformation) == native._THREAD_ENTRY_SIZE
    assert SystemProcessInformation.UniqueProcessId.offset == 0x50
    assert SystemProcessInformation.WorkingSetSize.offset == 0x90
    # CreateTime, UserTime and KernelTime are the last three LARGE_INTEGERs of Reserved1.
    reserved = SystemProcessInformation.Reserved1
    assert reserved.offset + reserved.size == 0x38
    assert native._ENTRY.size == 0x98


def test_parser_reads_documented_fields_and_ignores_neighbours():
    readings = parse_process_snapshot(
        snapshot_bytes([
            {"pid": 0, "kernel": 900.0, "threads": 2},
            {"pid": 8, "created": 1_700_000_123.5, "user": 1.5, "kernel": 0.25,
             "rss": 4096, "threads": 3, "padding": 16},
            {"pid": 12, "created": 1_700_000_200.0, "rss": 0},
        ])
    )
    assert readings == {
        0: ProcessMetricReading(started=None, cpu_seconds=900.0, rss=0),
        8: ProcessMetricReading(started=1_700_000_123.5, cpu_seconds=1.75, rss=4096),
        12: ProcessMetricReading(started=1_700_000_200.0, cpu_seconds=0.0, rss=0),
    }


def _with_next_offset(data: bytes, value: int) -> bytes:
    return struct.pack("<I", value) + data[4:]


@pytest.mark.parametrize(
    ("data", "max_rss"),
    [
        pytest.param(b"", None, id="empty"),
        pytest.param(snapshot_bytes([{"pid": 8}])[:0x90], None, id="truncated-entry"),
        pytest.param(snapshot_bytes([{"pid": 8, "threads": 2}])[:0x140], None, id="threads-past-end"),
        pytest.param(
            _with_next_offset(snapshot_bytes([{"pid": 8}, {"pid": 9}]), 0x104), None,
            id="misaligned-next",
        ),
        pytest.param(
            _with_next_offset(snapshot_bytes([{"pid": 8, "threads": 1}, {"pid": 9}]), 0x100),
            None, id="next-overlaps-threads",
        ),
        pytest.param(
            _with_next_offset(snapshot_bytes([{"pid": 8}, {"pid": 9}]), 0x1000), None,
            id="next-past-end",
        ),
        pytest.param(snapshot_bytes([{"pid": 8}, {"pid": 8}]), None, id="duplicate-pid"),
        pytest.param(snapshot_bytes([{"pid": 2**32}]), None, id="pid-out-of-range"),
        pytest.param(snapshot_bytes([{"pid": 8, "user": -1.0}]), None, id="negative-cpu"),
        pytest.param(snapshot_bytes([{"pid": 8, "created": -10.0}]), None, id="pre-1970-start"),
        pytest.param(snapshot_bytes([{"pid": 8, "rss": 2048}]), 1024, id="rss-over-physical"),
    ],
)
def test_parser_rejects_malformed_snapshots(data, max_rss):
    with pytest.raises(native.InvalidSnapshot):
        parse_process_snapshot(data, max_rss=max_rss)


# --- reader validation, buffers and failure handling ----------------------------


def test_reader_returns_snapshot_validated_against_psutil():
    reader, query = make_reader()

    readings = reader.read()

    assert readings is not None and set(readings) == {4, OWN_PID, 100}
    assert readings[OWN_PID] == ProcessMetricReading(1_700_000_000.0, 4.0, 50_000_000)
    assert reader.last_error is None
    assert query.sizes == [native._INITIAL_BUFFER_BYTES]


def test_reader_grows_buffer_and_remembers_size():
    large = [{"pid": 4, "threads": 8000}, OWN, {"pid": 100}]
    reader, query = make_reader(large)

    assert reader.read() is not None
    assert query.sizes[0] == native._INITIAL_BUFFER_BYTES
    assert query.sizes[-1] >= len(query.data)
    attempts = len(query.sizes)
    assert reader.read() is not None
    assert len(query.sizes) == attempts + 1  # The remembered size fits first time.


@pytest.mark.parametrize(
    ("entries", "system", "reason"),
    [
        ([{"pid": 4}, {"pid": 100}], FakeSystem(), "missing"),
        ([{"pid": 4}, OWN], FakeSystem(created=1_700_000_005.0), "creation time"),
        ([{"pid": 4}, OWN], FakeSystem(cpu=9.0), "CPU time"),
        ([{"pid": 4}, OWN], FakeSystem(rss=500_000_000), "working set"),
        ([OWN], FakeSystem(pids=range(4, 400, 4)), "cover"),
    ],
    ids=["own-pid-missing", "creation-time", "cpu-time", "working-set", "pid-coverage"],
)
def test_reader_rejects_snapshot_that_disagrees_with_psutil(entries, system, reason):
    reader, _ = make_reader(entries, system=system)

    assert reader.read() is None
    assert reason in reader.last_error
    assert reader.disabled_reason is None


@pytest.mark.parametrize(
    "status", [0xC0000022, 0x80000005], ids=["access-denied", "buffer-overflow-warning"]
)
def test_reader_treats_failure_statuses_as_unavailable(status):
    reader, _ = make_reader(status=status)
    assert reader.read() is None
    assert f"0x{status:08X}" in reader.last_error


def test_reader_refuses_snapshot_beyond_buffer_limit():
    class Endless:
        def __call__(self, info_class, buffer, size, returned):
            returned.value = size * 4
            return STATUS_INFO_LENGTH_MISMATCH

    reader = WindowsProcessMetrics(FakeSystem(), nt_query=Endless(), own_pid=OWN_PID)
    assert reader.read() is None
    assert "buffer limit" in reader.last_error


def test_reader_rejects_impossible_returned_length():
    def zero_length(info_class, buffer, size, returned):
        returned.value = 0
        return 0

    reader = WindowsProcessMetrics(FakeSystem(), nt_query=zero_length, own_pid=OWN_PID)
    assert reader.read() is None
    assert "returned snapshot length" in reader.last_error


def test_reader_turns_unexpected_errors_into_fallback():
    def exhausted(info_class, buffer, size, returned):
        raise MemoryError

    reader = WindowsProcessMetrics(FakeSystem(), nt_query=exhausted, own_pid=OWN_PID)
    assert reader.read() is None
    assert reader.last_error.startswith("MemoryError")


def test_reader_disables_itself_after_consecutive_failures():
    reader, query = make_reader([{"pid": 4}])  # Own process missing: always rejected.

    assert reader.read() is None and reader.read() is None
    query.data = snapshot_bytes([{"pid": 4}, OWN, {"pid": 100}])
    assert reader.read() is not None  # A success resets the failure count.

    query.data = snapshot_bytes([{"pid": 4}])
    for _ in range(native.MAX_CONSECUTIVE_FAILURES):
        assert reader.read() is None
    calls = len(query.sizes)
    assert reader.disabled_reason is not None
    assert reader.read() is None
    assert len(query.sizes) == calls  # Disabled readers stop querying.


def test_reader_is_disabled_without_64_bit_windows(monkeypatch):
    monkeypatch.setattr(native, "native_supported", lambda: False)
    reader = WindowsProcessMetrics(FakeSystem(), own_pid=OWN_PID)

    assert reader.read() is None
    assert reader.disabled_reason == "requires 64-bit Python on Windows"


@requires_native
def test_native_snapshot_matches_psutil_on_this_system():
    sample = []
    for process in psutil.process_iter(["pid"]):
        if process.pid in (0, 4):
            continue  # psutil reports creation time 0 for the kernel pseudo-processes.
        try:
            times = process.cpu_times()
            sample.append(
                (process, process.create_time(), times.user + times.system,
                 process.memory_info().rss)
            )
        except psutil.Error:
            continue
        if len(sample) == 25:
            break
    reader = WindowsProcessMetrics()

    readings = reader.read()

    assert readings is not None, reader.last_error
    assert os.getpid() in readings
    compared = 0
    for process, created, cpu_before, rss_before in sample:
        reading = readings.get(process.pid)
        try:
            times = process.cpu_times()
            rss_after = process.memory_info().rss
        except psutil.Error:
            continue  # Exited during the test.
        if reading is None:
            continue
        compared += 1
        assert reading.started == pytest.approx(created, abs=0.01)
        assert cpu_before - 0.05 <= reading.cpu_seconds <= times.user + times.system + 0.05
        assert abs(reading.rss - rss_after) <= max(64 * 1024**2, rss_before)
    assert compared >= 1


@requires_native
def test_native_cpu_time_tracks_real_work():
    reader = WindowsProcessMetrics()
    first = reader.read()
    started = time.perf_counter()
    while time.perf_counter() - started < 0.4:
        pass
    elapsed = time.perf_counter() - started
    second = reader.read()

    assert first is not None and second is not None, reader.last_error
    used = second[os.getpid()].cpu_seconds - first[os.getpid()].cpu_seconds
    assert 0.05 <= used <= elapsed + 0.25


# --- collector: native path -----------------------------------------------------


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeProcess:
    """psutil-like process for enumeration, fallback metric reads and identity."""

    def __init__(self, pid: int, clock: FakeClock, *, read_cost: float = 0.0,
                 exe_denied: bool = False, created: float = 1_700_000_000.0) -> None:
        self.info = {"pid": pid, "name": f"synthetic-{pid}.exe"}
        self.pid = pid
        self.clock = clock
        self.read_cost = read_cost
        self.exe_denied = exe_denied
        self.created = created
        self.cpu_seconds = 0.0
        self.rss = 4096
        self.cpu_denied = False
        self.metric_reads = 0
        self.identity_reads = 0

    def cpu_times(self):
        self.metric_reads += 1
        self.clock.advance(self.read_cost)
        if self.cpu_denied:
            raise psutil.AccessDenied(pid=self.pid)
        return SimpleNamespace(user=self.cpu_seconds * 0.75, system=self.cpu_seconds * 0.25)

    def memory_percent(self):
        return 100.0 * self.rss / PHYSICAL_MEMORY

    def memory_info(self):
        return SimpleNamespace(rss=self.rss)

    def create_time(self):
        return self.created

    def status(self):
        return "running"

    def ppid(self):
        return 1

    def exe(self):
        self.identity_reads += 1
        if self.exe_denied:
            raise psutil.AccessDenied(pid=self.pid)
        return f"C:\\synthetic\\{self.pid}.exe"

    def username(self):
        return "synthetic"


class FakeNativeMetrics:
    """Reads the fake processes' current counters, like one system-wide snapshot."""

    def __init__(self, processes: list[FakeProcess]) -> None:
        self.processes = processes
        self.available = True
        self.missing: set[int] = set()
        self.reads = 0

    def read(self):
        self.reads += 1
        if not self.available:
            return None
        return {
            process.pid: ProcessMetricReading(process.created, process.cpu_seconds, process.rss)
            for process in self.processes
            if process.pid not in self.missing
        }


def make_collector(processes, clock, *, native_metrics=None, cpus=4, **options):
    module = SimpleNamespace(
        process_iter=lambda attrs, ad_value: list(processes),
        cpu_count=lambda: cpus,
        virtual_memory=lambda: SimpleNamespace(total=PHYSICAL_MEMORY),
    )
    options.setdefault("sampling_interval_seconds", INTERVAL)
    return ProcessCollector(module, native_metrics=native_metrics, clock=clock, **options)


def native_collector(processes, clock, **options):
    reader = FakeNativeMetrics(processes)
    return make_collector(processes, clock, native_metrics=reader, **options), reader


def by_pid(rows):
    return {row.pid: row for row in rows}


def test_first_native_reading_is_unavailable_not_zero():
    clock = FakeClock()
    busy = FakeProcess(10, clock)
    busy.cpu_seconds = 500.0
    collector, _ = native_collector([busy], clock)

    first = collector.collect([])[0]

    assert "cpu_percent" in first.unavailable_fields
    assert first.cpu_window_seconds is None
    assert first.metrics_observed is not None
    assert first.memory_bytes == 4096


def test_native_cpu_percent_is_share_of_total_capacity_between_snapshots():
    clock = FakeClock()
    busy, idle = FakeProcess(10, clock), FakeProcess(11, clock)
    busy.rss = PHYSICAL_MEMORY // 8
    collector, _ = native_collector([busy, idle], clock, cpus=4)
    collector.collect([])

    # One fully busy logical CPU out of four for the whole 2-second cycle.
    busy.cpu_seconds += 2.0
    clock.advance(2.0)
    rows = by_pid(collector.collect([]))

    assert collector.metric_source == "native"
    assert rows[10].cpu_percent == 25.0
    assert rows[10].cpu_window_seconds == 2.0
    assert rows[10].memory_percent == 12.5
    assert "cpu_percent" not in rows[10].unavailable_fields
    # A measured zero is an available 0.0, unlike the unmeasured case.
    assert rows[11].cpu_percent == 0.0
    assert "cpu_percent" not in rows[11].unavailable_fields


def test_native_cpu_percent_is_clamped_to_total_capacity():
    clock = FakeClock()
    process = FakeProcess(10, clock)
    collector, _ = native_collector([process], clock, cpus=2)
    collector.collect([])

    process.cpu_seconds += 1000.0
    clock.advance(2.0)

    assert collector.collect([])[0].cpu_percent == 100.0


def test_native_snapshot_refreshes_every_process_every_cycle_without_per_process_reads():
    clock = FakeClock()
    processes = [FakeProcess(pid, clock) for pid in range(1, 401)]
    collector, reader = native_collector(processes, clock, cpus=1)
    collector.collect([])

    for process in processes:
        process.cpu_seconds += 0.2
    clock.advance(2.0)  # Not a sampling pass: native readings still refresh.
    rows = collector.collect([])

    assert reader.reads == 2
    assert sum(process.metric_reads for process in processes) == 0
    assert all(row.cpu_percent == 10.0 for row in rows)
    assert not any(row.metrics_stale for row in rows)


def test_native_creation_time_change_is_treated_as_pid_reuse():
    clock = FakeClock()
    process = FakeProcess(10, clock)
    process.cpu_seconds = 50.0
    collector, _ = native_collector([process], clock, cpus=1)
    collector.collect([])
    process.cpu_seconds += 1.0
    clock.advance(2.0)
    original = collector.collect([])[0]
    assert original.cpu_percent == 50.0
    assert original.executable == "C:\\synthetic\\10.exe"

    # The PID now belongs to a process that has used more CPU time in total.
    process.created += 30.0
    process.cpu_seconds = 400.0
    clock.advance(2.0)
    reused = collector.collect([])[0]

    assert "cpu_percent" in reused.unavailable_fields
    assert reused.executable is None and reused.status == "unknown"
    assert {"create_time", "executable", "username"} <= set(reused.unavailable_fields)
    process.cpu_seconds += 0.5
    clock.advance(2.0)
    assert collector.collect([])[0].cpu_percent == 25.0


def test_identity_creation_time_comes_from_native_snapshot():
    clock = FakeClock()
    process = FakeProcess(10, clock, created=1_700_000_500.0)
    collector, _ = native_collector([process], clock)
    # psutil can keep a Process instance (and its cached creation time) across PID reuse.
    process.create_time = lambda: 1_600_000_000.0

    row = collector.collect([])[0]

    assert row.create_time == datetime.fromtimestamp(1_700_000_500.0, UTC)


def test_process_missing_from_snapshot_ages_instead_of_looking_current():
    clock = FakeClock()
    process = FakeProcess(10, clock)
    collector, reader = native_collector([process], clock, stale_after_seconds=30.0)
    first = collector.collect([])[0]

    reader.missing.add(10)  # Enumerated, but not in (or exited before) the snapshot.
    clock.advance(20.0)
    second = collector.collect([])[0]
    assert second.metrics_observed == first.metrics_observed
    assert second.last_observed >= first.last_observed
    assert second.metrics_stale is False

    clock.advance(20.0)
    third = collector.collect([])[0]
    assert third.metrics_observed == first.metrics_observed
    assert third.metrics_stale is True


def test_failed_native_snapshot_falls_back_to_psutil_and_recovers():
    clock = FakeClock()
    processes = [FakeProcess(pid, clock) for pid in range(1, 7)]
    collector, reader = native_collector(processes, clock, metric_budget=2)
    reader.available = False

    collector.collect([])
    assert collector.metric_source == "psutil"
    assert [process.metric_reads for process in processes] == [1, 1, 0, 0, 0, 0]
    clock.advance(2.0)
    collector.collect([])  # Between sampling passes the fallback reads nothing.
    assert sum(process.metric_reads for process in processes) == 2

    reader.available = True
    clock.advance(2.0)
    rows = collector.collect([])
    assert collector.metric_source == "native"
    assert all(row.metrics_observed is not None for row in rows)
    assert sum(process.metric_reads for process in processes) == 2


# --- collector: psutil fallback path --------------------------------------------


def test_fallback_rotation_reaches_every_process_fairly():
    clock = FakeClock()
    processes = [FakeProcess(pid, clock) for pid in range(1, 10)]
    collector = make_collector(processes, clock, metric_budget=3)
    # The network-heaviest process must not monopolise the budget.
    counts = {1: 40}

    for _ in range(3):
        collector.collect([], counts)
        clock.advance(INTERVAL)
    assert [process.metric_reads for process in processes] == [1] * 9

    for process in processes:
        process.cpu_seconds += 1.0
    for _ in range(3):
        rows = by_pid(collector.collect([], counts))
        clock.advance(INTERVAL)
    # Every process has now been read twice, three passes (30 s) apart.
    assert all(rows[pid].cpu_window_seconds == 3 * INTERVAL for pid in rows)
    assert all(rows[pid].cpu_percent == round(100 / (4 * 30.0), 1) for pid in rows)


def test_fallback_pass_stops_at_time_budget_and_resumes_least_recently_read():
    clock = FakeClock()
    processes = [FakeProcess(pid, clock, read_cost=0.03) for pid in range(1, 21)]
    collector = make_collector(processes, clock, metric_time_budget_seconds=0.1)

    collector.collect([])
    assert sum(process.metric_reads for process in processes) == 4

    for _ in range(4):
        clock.advance(INTERVAL)
        collector.collect([])
    assert [process.metric_reads for process in processes] == [1] * 20


def test_fallback_denied_reads_are_unavailable_not_zero():
    clock = FakeClock()
    process = FakeProcess(10, clock)
    collector = make_collector([process], clock, cpus=1)
    collector.collect([])
    process.cpu_seconds += 5.0
    clock.advance(INTERVAL)
    assert collector.collect([])[0].cpu_percent == 50.0

    process.cpu_denied = True
    process.memory_info = process.memory_percent = _denied
    clock.advance(INTERVAL)
    row = collector.collect([])[0]

    assert {"cpu_percent", "memory_percent", "memory_bytes"} <= set(row.unavailable_fields)
    assert row.cpu_percent == 0.0 and row.memory_bytes == 0


def _denied():
    raise psutil.AccessDenied(pid=10)


def test_fallback_cpu_counter_going_backwards_restarts_measurement():
    clock = FakeClock()
    process = FakeProcess(10, clock)
    process.cpu_seconds = 900.0
    collector = make_collector([process], clock, cpus=1)
    collector.collect([])
    process.cpu_seconds += 5.0
    clock.advance(INTERVAL)
    assert collector.collect([])[0].cpu_percent == 50.0

    # psutil keeps a stale instance across PID reuse; the CPU counter drops.
    process.cpu_seconds = 0.5
    clock.advance(INTERVAL)
    assert "cpu_percent" in collector.collect([])[0].unavailable_fields

    process.cpu_seconds += 1.0
    clock.advance(INTERVAL)
    assert collector.collect([])[0].cpu_percent == 10.0


def test_never_sampled_process_reports_no_metric_timestamp():
    clock = FakeClock()
    process = FakeProcess(10, clock)
    process.cpu_times = process.memory_percent = process.memory_info = _denied
    collector = make_collector([process], clock)

    row = collector.collect([])[0]

    assert row.metrics_observed is None
    assert row.metrics_stale is False
    assert {"cpu_percent", "memory_percent", "memory_bytes"} <= set(row.unavailable_fields)


def test_metric_freshness_is_separate_from_enumeration():
    clock = FakeClock()
    process = FakeProcess(10, clock)
    collector = make_collector([process], clock, stale_after_seconds=30.0)
    first = collector.collect([])[0]

    clock.advance(5.0)  # Enumerated again, but no fallback read between passes.
    second = collector.collect([])[0]
    assert process.metric_reads == 1
    assert second.last_observed >= first.last_observed
    assert second.metrics_observed == first.metrics_observed

    process.cpu_times = process.memory_percent = process.memory_info = _denied
    clock.advance(40.0)
    third = collector.collect([])[0]
    assert third.metrics_observed == first.metrics_observed
    assert third.metrics_stale is True


# --- identity fairness and display bound ----------------------------------------


def test_identity_sampling_is_not_starved_by_an_inaccessible_process():
    clock = FakeClock()
    denied = FakeProcess(1, clock, exe_denied=True)
    others = [FakeProcess(pid, clock) for pid in range(2, 6)]
    processes = [denied, *others]
    collector, _ = native_collector(processes, clock, identity_budget=1)
    counts = {1: 50, 2: 1}  # The inaccessible process is the network-heaviest.

    for _ in range(5):
        collector.collect([], counts)
        clock.advance(INTERVAL)
    rows = by_pid(collector.collect([], counts))

    assert [process.identity_reads for process in processes] == [2, 1, 1, 1, 1]
    assert "executable" in rows[1].unavailable_fields
    assert all(rows[pid].executable == f"C:\\synthetic\\{pid}.exe" for pid in range(2, 6))


def test_identity_sampling_takes_new_processes_before_reverifying_identified_ones():
    clock = FakeClock()
    processes = [FakeProcess(pid, clock) for pid in (5, 6)]
    collector, _ = native_collector(processes, clock, identity_budget=1)
    counts = {6: 9}
    collector.collect([], counts)
    assert processes[1].identity_reads == 1  # Network-active first.

    # A later arrival queues behind the process already waiting.
    late = FakeProcess(1, clock)
    processes.append(late)
    for _ in range(2):
        clock.advance(INTERVAL)
        collector.collect([], counts)

    assert [process.identity_reads for process in processes] == [1, 1, 1]


def test_high_cpu_process_survives_the_display_bound():
    clock = FakeClock()
    connected = [FakeProcess(pid, clock) for pid in range(1, MAX_PROCESSES + 51)]
    busy = FakeProcess(9000, clock)
    collector, _ = native_collector([*connected, busy], clock, cpus=1)
    counts = {process.pid: 3 for process in connected}
    collector.collect([], counts)

    busy.cpu_seconds += 1.8
    clock.advance(2.0)
    rows = collector.collect([], counts)

    assert len(rows) == MAX_PROCESSES
    assert by_pid(rows)[9000].cpu_percent == 90.0
    assert rows[0].connection_count == 3  # Returned order is still network first.
    assert rows[-1].pid == 9000


# --- consumers ------------------------------------------------------------------


def process_row(pid: int, *, cpu: float, connections: int = 0, **update) -> ProcessInfo:
    return ProcessInfo(
        pid=pid, name=f"synthetic-{pid}.exe", cpu_percent=cpu, memory_percent=1.0,
        memory_bytes=4096, status="running", connection_count=connections, **update,
    )


def test_ai_context_includes_busy_processes_and_hides_unmeasured_values(snapshot):
    network = [process_row(pid, cpu=1.0, connections=50 - pid) for pid in range(1, 21)]
    busy = process_row(900, cpu=88.0)
    stale = process_row(901, cpu=70.0, metrics_stale=True)
    unmeasured = process_row(2, cpu=0.0, connections=48, unavailable_fields=["cpu_percent"])
    rows = [unmeasured, *network[2:], busy, stale]

    context = LocalAIAnalyst.context(snapshot.model_copy(update={"processes": rows}), False)
    listed = {item["pid"]: item for item in context["top_processes"]}

    assert len(listed) == 15
    assert listed[900]["cpu_percent"] == 88.0
    assert listed[2]["cpu_percent"] is None
    assert 901 not in listed or listed[901]["cpu_percent"] is None


def test_history_does_not_record_stale_process_values_as_current(tmp_path, snapshot):
    service = TelemetryService(Database(tmp_path / "synthetic.db"))
    rows = [process_row(1, cpu=40.0, metrics_stale=True), process_row(2, cpu=10.0)]

    service._queue_history_sample(snapshot.model_copy(update={"processes": rows}))
    batch = service._history_queue.get_nowait()

    observed = {item.pid: item for item in batch.processes}
    assert observed[1].cpu_percent is None and observed[1].memory_percent is None
    assert observed[2].cpu_percent == 10.0
