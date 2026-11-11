from __future__ import annotations

import ctypes
import math
import os
import struct
import sys
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import psutil
import pytest
from fastapi.testclient import TestClient

from app.ai import LocalAIAnalyst
from app.config import MAX_PROCESSES
from app.main import create_app
from app.models import ProcessInfo
from app.monitoring import windows_process_metrics as native
from app.monitoring.process_selection import measured, select_processes
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
    def cpu_count():
        return 16

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


@pytest.mark.skipif(
    sys.platform != "win32" or struct.calcsize("P") != 8,
    reason="describes the 64-bit Windows ABI",
)
def test_layout_offsets_match_documented_winternl_structures():
    # Windows widths are spelled out: ULONG/LONG are 32-bit there, while ctypes.c_ulong
    # and ctypes.c_long are 64-bit on LP64 platforms.
    ulong, long_, ushort = ctypes.c_uint32, ctypes.c_int32, ctypes.c_uint16
    pointer, size_t, large_integer = ctypes.c_uint64, ctypes.c_uint64, ctypes.c_int64

    class UnicodeString(ctypes.Structure):
        _fields_ = [("Length", ushort), ("MaximumLength", ushort), ("Buffer", pointer)]

    # SYSTEM_PROCESS_INFORMATION exactly as published in winternl.h.
    class SystemProcessInformation(ctypes.Structure):
        _fields_ = [
            ("NextEntryOffset", ulong), ("NumberOfThreads", ulong),
            ("Reserved1", ctypes.c_uint8 * 48), ("ImageName", UnicodeString),
            ("BasePriority", long_), ("UniqueProcessId", pointer),
            ("Reserved2", pointer), ("HandleCount", ulong),
            ("SessionId", ulong), ("Reserved3", pointer),
            ("PeakVirtualSize", size_t), ("VirtualSize", size_t),
            ("Reserved4", ulong), ("PeakWorkingSetSize", size_t),
            ("WorkingSetSize", size_t), ("Reserved5", pointer),
            ("QuotaPagedPoolUsage", size_t), ("Reserved6", pointer),
            ("QuotaNonPagedPoolUsage", size_t), ("PagefileUsage", size_t),
            ("PeakPagefileUsage", size_t), ("PrivatePageCount", size_t),
            ("Reserved7", large_integer * 6),
        ]

    class SystemThreadInformation(ctypes.Structure):
        _fields_ = [
            ("Reserved1", large_integer * 3), ("Reserved2", ulong),
            ("StartAddress", pointer), ("ClientId", pointer * 2),
            ("Priority", long_), ("BasePriority", long_),
            ("Reserved3", ulong), ("ThreadState", ulong),
            ("WaitReason", ulong),
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


def test_reader_marks_cpu_time_beyond_process_lifetime_unavailable():
    started = time.time() - 60
    reader, _ = make_reader([
        {"pid": 4},
        OWN,
        # 60 s on 16 logical CPUs allows at most about 990 CPU-seconds.
        {"pid": 100, "created": started, "user": 10_000.0, "rss": 8192},
        {"pid": 101, "created": started, "user": 900.0, "kernel": 50.0},
        {"pid": 102, "created": started, "kernel": 0.0},
    ])

    readings = reader.read()

    assert readings is not None
    assert readings[100] == ProcessMetricReading(started, None, 8192)
    assert readings[101].cpu_seconds == 950.0  # Heavy but possible.
    assert readings[102].cpu_seconds == 0.0


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
                 exe_denied: bool = False, created: float = 1_700_000_000.0,
                 name: str | None = None) -> None:
        self.info = {"pid": pid, "name": name or f"synthetic-{pid}.exe"}
        self.executable = f"C:\\synthetic\\{name or f'{pid}.exe'}"
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

    def name(self):
        return self.info["name"]

    def create_time(self):
        if self.created is None:
            raise psutil.AccessDenied(pid=self.pid)
        return self.created

    def status(self):
        return "running"

    def ppid(self):
        return 1

    def exe(self):
        self.identity_reads += 1
        if self.exe_denied:
            raise psutil.AccessDenied(pid=self.pid)
        return self.executable

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
            process.pid: ProcessMetricReading(
                process.created, process.cpu_seconds, process.rss, process.name()
            )
            for process in self.processes
            if process.pid not in self.missing
        }


def make_collector(processes, clock, *, native_metrics=None, cpus=4, **options):
    module = SimpleNamespace(
        process_iter=lambda attrs, ad_value: list(processes),
        Process=lambda pid: next(process for process in processes if process.pid == pid),
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


def test_impossible_cpu_delta_is_rejected_not_clamped():
    clock = FakeClock()
    process = FakeProcess(10, clock)
    collector, _ = native_collector([process], clock, cpus=2)
    collector.collect([])

    process.cpu_seconds += 1000.0  # 1000 CPU-seconds in 2 s on 2 logical CPUs.
    clock.advance(2.0)
    rejected = collector.collect([])[0]
    assert "cpu_percent" in rejected.unavailable_fields
    assert rejected.cpu_percent == 0.0 and rejected.cpu_window_seconds is None

    # Measurement restarts from the rejected reading.
    process.cpu_seconds += 2.0
    clock.advance(2.0)
    assert collector.collect([])[0].cpu_percent == 50.0


@pytest.mark.parametrize(("used", "expected"), [(4.0, 100.0), (4.1, 100.0)])
def test_full_cpu_usage_is_kept_within_timing_tolerance(used, expected):
    clock = FakeClock()
    process = FakeProcess(10, clock)
    collector, _ = native_collector([process], clock, cpus=2)
    collector.collect([])

    process.cpu_seconds += used  # 100% and 102.5% of two CPUs for 2 s.
    clock.advance(2.0)
    row = collector.collect([])[0]

    assert row.cpu_percent == expected
    assert "cpu_percent" not in row.unavailable_fields


def test_native_cpu_beyond_process_lifetime_is_unavailable():
    clock = FakeClock()
    process = FakeProcess(10, clock)
    reader = FakeNativeMetrics([process])
    collector = make_collector([process], clock, native_metrics=reader)
    collector.collect([])
    process.cpu_seconds += 1.0
    clock.advance(2.0)
    assert collector.collect([])[0].cpu_percent == 12.5

    # The reader flags an impossible counter as None; memory is still current.
    reader.read = lambda: {10: ProcessMetricReading(process.created, None, 8192)}
    clock.advance(2.0)
    row = collector.collect([])[0]

    assert "cpu_percent" in row.unavailable_fields
    assert row.memory_bytes == 8192 and "memory_bytes" not in row.unavailable_fields


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
    assert {"executable", "username", "parent_pid", "status"} <= set(reused.unavailable_fields)
    assert reused.create_time == datetime.fromtimestamp(process.created, UTC)
    process.cpu_seconds += 0.5
    clock.advance(2.0)
    assert collector.collect([])[0].cpu_percent == 25.0


class PsutilLikeSystem:
    """process_iter() keeps each instance, and its cached name, across PID reuse.

    ``Process(pid)`` returns whoever owns the PID now, as a new psutil instance does.
    """

    def __init__(self, *owners: FakeProcess, cpus: int = 1) -> None:
        self.iterated = {owner.pid: owner for owner in owners}
        self.current = dict(self.iterated)
        self.cpus = cpus

    def process_iter(self, attrs, ad_value):
        return list(self.iterated.values())

    def Process(self, pid):
        if pid not in self.current:
            raise psutil.NoSuchProcess(pid)
        return self.current[pid]

    def cpu_count(self):
        return self.cpus

    @staticmethod
    def virtual_memory():
        return SimpleNamespace(total=PHYSICAL_MEMORY)


def reuse_pid(system, reader, clock, *, cpu_seconds, created_offset=30.0):
    """Give PID 10 to new.exe while process_iter() keeps yielding old.exe."""
    old = system.iterated[10]
    new = FakeProcess(10, clock, created=old.created + created_offset, name="new.exe")
    new.cpu_seconds = cpu_seconds
    system.current[10] = new
    if reader is not None:
        reader.processes = [new]
    return new


def test_native_pid_reuse_takes_name_and_identity_from_the_new_owner():
    clock = FakeClock()
    old = FakeProcess(10, clock, name="old.exe")
    old.cpu_seconds = 50.0
    system = PsutilLikeSystem(old)
    reader = FakeNativeMetrics([old])
    collector = ProcessCollector(system, native_metrics=reader, clock=clock,
                                 sampling_interval_seconds=INTERVAL)
    first = collector.collect([])[0]
    assert (first.name, first.executable) == ("old.exe", "C:\\synthetic\\old.exe")

    new = reuse_pid(system, reader, clock, cpu_seconds=60.0)
    clock.advance(2.0)  # Native detects the new owner before any identity pass.
    reused = collector.collect([])[0]

    assert reused.name == "new.exe"
    assert reused.executable is None and reused.username is None
    assert {"executable", "username", "parent_pid", "status", "cpu_percent"} <= set(
        reused.unavailable_fields
    )
    assert reused.create_time == datetime.fromtimestamp(new.created, UTC)

    # process_iter() still yields old.exe's instance; nothing of it comes back.
    clock.advance(8.0)
    identified = collector.collect([])[0]
    assert identified.name == "new.exe"
    assert identified.executable == "C:\\synthetic\\new.exe"

    # Once psutil replaces its instance, enumeration is trusted again.
    system.iterated[10] = new
    clock.advance(2.0)
    assert collector.collect([])[0].name == "new.exe"


@pytest.mark.parametrize("new_owner_cpu", [90.0, 2.0], ids=["counter-higher", "counter-lower"])
def test_fallback_never_computes_cpu_across_pid_owners(new_owner_cpu):
    clock = FakeClock()
    old = FakeProcess(10, clock, name="old.exe")
    old.cpu_seconds = 50.0
    system = PsutilLikeSystem(old)
    reader = FakeNativeMetrics([old])
    collector = ProcessCollector(system, native_metrics=reader, clock=clock,
                                 sampling_interval_seconds=INTERVAL, identity_budget=0)
    collector.collect([])  # Native baseline for old.exe.

    reader.available = False
    new = reuse_pid(system, None, clock, cpu_seconds=new_owner_cpu)
    clock.advance(INTERVAL)
    reused = collector.collect([])[0]

    assert collector.metric_source == "psutil"
    assert "cpu_percent" in reused.unavailable_fields
    assert reused.name == "new.exe"
    assert reused.create_time == datetime.fromtimestamp(new.created, UTC)

    new.cpu_seconds += 2.5
    clock.advance(INTERVAL)
    assert collector.collect([])[0].cpu_percent == 25.0  # new.exe alone, 2.5 s in 10 s.


def test_fallback_pid_reuse_is_detected_without_native_metrics():
    clock = FakeClock()
    old = FakeProcess(10, clock, name="old.exe")
    old.cpu_seconds = 50.0
    system = PsutilLikeSystem(old)
    collector = ProcessCollector(system, clock=clock, sampling_interval_seconds=INTERVAL)
    collector.collect([])
    assert collector.native_metrics is None

    reuse_pid(system, None, clock, cpu_seconds=80.0)
    clock.advance(INTERVAL)
    row = collector.collect([])[0]

    assert "cpu_percent" in row.unavailable_fields
    assert row.name == "new.exe"


def test_fallback_without_creation_time_attributes_nothing():
    clock = FakeClock()
    process = FakeProcess(10, clock)
    system = PsutilLikeSystem(process)
    collector = ProcessCollector(system, clock=clock, sampling_interval_seconds=INTERVAL)
    collector.collect([])
    process.cpu_seconds += 5.0
    clock.advance(INTERVAL)
    assert collector.collect([])[0].cpu_percent == 50.0

    process.created = None  # The owner can no longer be identified.
    process.cpu_seconds += 5.0
    clock.advance(INTERVAL)
    row = collector.collect([])[0]

    assert {"cpu_percent", "memory_percent", "memory_bytes"} <= set(row.unavailable_fields)


def test_identity_creation_time_comes_from_a_current_process_instance():
    clock = FakeClock()
    stale = FakeProcess(10, clock, created=1_600_000_000.0)
    current = FakeProcess(10, clock, created=1_700_000_500.0)
    system = PsutilLikeSystem(stale)
    system.current[10] = current
    reader = FakeNativeMetrics([current])
    collector = ProcessCollector(system, native_metrics=reader, clock=clock)

    row = collector.collect([])[0]

    # The cached instance's creation time is never published or compared.
    assert row.create_time == datetime.fromtimestamp(1_700_000_500.0, UTC)
    assert row.executable == "C:\\synthetic\\10.exe"


def test_native_creation_time_is_published_without_an_identity_read(tmp_path, snapshot):
    clock = FakeClock()
    process = FakeProcess(10, clock, created=1_700_000_500.0)
    collector, _ = native_collector([process], clock, identity_budget=0)

    row = collector.collect([])[0]

    assert process.identity_reads == 0
    assert row.create_time == datetime.fromtimestamp(1_700_000_500.0, UTC)
    assert "create_time" not in row.unavailable_fields
    service = TelemetryService(Database(tmp_path / "synthetic.db"))
    service._queue_history_sample(snapshot.model_copy(update={"processes": [row]}))
    observation = service._history_queue.get_nowait().processes[0]
    assert observation.create_time == row.create_time


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
    fields = {
        "pid": pid, "name": f"synthetic-{pid}.exe", "cpu_percent": cpu, "memory_percent": 1.0,
        "memory_bytes": 4096, "status": "running", "connection_count": connections,
    }
    return ProcessInfo(**{**fields, **update})


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


CONNECTED = [process_row(pid, cpu=0.0, connections=5) for pid in range(1000, 1150)]


def test_stale_cpu_never_outranks_a_fresh_reading():
    stale_hot = [process_row(pid, cpu=90.0, metrics_stale=True) for pid in range(1, 101)]
    fresh = process_row(500, cpu=60.0)

    selected = {row.pid for row in select_processes([*stale_hot, *CONNECTED, fresh], 200)}

    assert 500 in selected


def test_idle_memory_heavy_unconnected_process_keeps_a_row():
    light_cpu = [process_row(pid, cpu=0.5) for pid in range(1, 121)]
    hog = process_row(600, cpu=0.0, memory_bytes=8 * 1024**3)

    rows = select_processes([*light_cpu, *CONNECTED, hog], 200)

    assert 600 in {row.pid for row in rows}
    # Connected processes still get their share.
    assert sum(row.connection_count > 0 for row in rows) >= 200 // 3


def test_missing_values_never_rank_as_zero():
    measured_zero = process_row(1, cpu=0.0)
    unmeasured = process_row(2, cpu=0.0, unavailable_fields=["cpu_percent", "memory_bytes"])
    stale = process_row(3, cpu=99.0, metrics_stale=True)

    rows = select_processes([unmeasured, stale, measured_zero], 3)

    assert [row.pid for row in rows][0] == 1
    assert measured(unmeasured, "cpu_percent") is None
    assert measured(stale, "cpu_percent") is None


def test_history_keeps_fresh_heavy_processes_ahead_of_stale_rows(tmp_path, snapshot):
    service = TelemetryService(Database(tmp_path / "synthetic.db"))
    service.history_settings.process_sample_limit = 10
    stale_hot = [process_row(pid, cpu=90.0, metrics_stale=True) for pid in range(1, 31)]
    fresh = [process_row(700, cpu=50.0), process_row(701, cpu=0.0, memory_bytes=6 * 1024**3)]

    service._queue_history_sample(snapshot.model_copy(update={"processes": [*stale_hot, *fresh]}))
    observed = {item.pid: item for item in service._history_queue.get_nowait().processes}

    assert observed[700].cpu_percent == 50.0
    assert 701 in observed
    assert all(item.cpu_percent is None for pid, item in observed.items() if pid < 700)


def test_dashboard_history_and_ai_select_the_same_processes(tmp_path, snapshot):
    rows = [
        *[process_row(pid, cpu=1.0, connections=50 - pid) for pid in range(1, 30)],
        *[process_row(pid, cpu=40.0 + pid % 7) for pid in range(100, 110)],
        *[process_row(pid, cpu=0.0, memory_bytes=pid * 1024**3) for pid in range(200, 210)],
        process_row(300, cpu=95.0, metrics_stale=True),
    ]
    current = snapshot.model_copy(update={"processes": rows})
    service = TelemetryService(Database(tmp_path / "synthetic.db"))
    service.history_settings.process_sample_limit = 15

    service._queue_history_sample(current)
    history = {item.pid for item in service._history_queue.get_nowait().processes}
    ai = {item["pid"] for item in LocalAIAnalyst.context(current, False)["top_processes"]}

    assert history == ai == {row.pid for row in select_processes(rows, 15)}
    assert {104, 103, 209, 208} <= ai  # Busiest CPU and largest memory, all unconnected.
    assert 300 not in ai


def test_history_does_not_record_stale_process_values_as_current(tmp_path, snapshot):
    service = TelemetryService(Database(tmp_path / "synthetic.db"))
    rows = [process_row(1, cpu=40.0, metrics_stale=True), process_row(2, cpu=10.0)]

    service._queue_history_sample(snapshot.model_copy(update={"processes": rows}))
    batch = service._history_queue.get_nowait()

    observed = {item.pid: item for item in batch.processes}
    assert observed[1].cpu_percent is None and observed[1].memory_percent is None
    assert observed[2].cpu_percent == 10.0


# --- PID ownership regressions from the c6d7724 review --------------------------


def test_native_reused_pid_before_another_pid_never_recovers_the_old_name():
    clock = FakeClock()
    old = FakeProcess(10, clock, name="old.exe")
    other = FakeProcess(20, clock, name="other.exe")
    system = PsutilLikeSystem(old, other)
    reader = FakeNativeMetrics([old, other])
    collector = ProcessCollector(system, native_metrics=reader, clock=clock, identity_budget=0)
    collector.collect([])
    new = reuse_pid(system, None, clock, cpu_seconds=1.0)
    reader.processes = [new, other]

    for _ in range(4):
        clock.advance(2.0)
        row = by_pid(collector.collect([]))[10]
        assert row.name == "new.exe"
        assert row.create_time == datetime.fromtimestamp(new.created, UTC)


def test_native_first_discovery_does_not_trust_a_stale_enumerated_name():
    clock = FakeClock()
    old = FakeProcess(10, clock, name="old.exe")
    new = FakeProcess(10, clock, created=old.created + 30, name="new.exe")
    system = PsutilLikeSystem(old)
    system.current[10] = new
    collector = ProcessCollector(system, native_metrics=FakeNativeMetrics([new]), clock=clock)

    row = collector.collect([])[0]

    assert row.name == "new.exe"
    assert row.executable == "C:\\synthetic\\new.exe"
    assert row.create_time == datetime.fromtimestamp(new.created, UTC)


@pytest.mark.parametrize("identity_budget", [0, 1])
def test_native_snapshot_owner_is_not_combined_with_a_later_owner(identity_budget):
    clock = FakeClock()
    old = FakeProcess(10, clock, name="old.exe")
    system = PsutilLikeSystem(old)
    reader = FakeNativeMetrics([old])
    collector = ProcessCollector(system, native_metrics=reader, clock=clock,
                                 identity_budget=identity_budget)
    collector.collect([])
    owner_b = FakeProcess(10, clock, created=old.created + 30, name="B.exe")
    owner_b.rss = 8192
    owner_c = FakeProcess(10, clock, created=old.created + 60, name="C.exe")
    reader.processes = [owner_b]
    system.current[10] = owner_c  # Reused again after the native snapshot.
    clock.advance(INTERVAL)

    row = collector.collect([])[0]

    assert row.name in {"B.exe", "Unknown"}
    assert row.create_time == datetime.fromtimestamp(owner_b.created, UTC)
    assert row.memory_bytes == 8192
    assert row.executable is None and row.username is None
    assert {"executable", "username", "status", "parent_pid"} <= set(row.unavailable_fields)


def test_fallback_rejects_constructor_cached_owner_and_next_owner_cpu(monkeypatch):
    """Real psutil wrapper: A=50 s, then B=54 s must never become A at 40%."""
    clock = FakeClock()
    old = FakeProcess(10, clock, name="old.exe")
    old.cpu_seconds = 50.0
    system = PsutilLikeSystem(old)
    reader = FakeNativeMetrics([old])
    collector = ProcessCollector(system, native_metrics=reader, clock=clock, identity_budget=0)
    collector.collect([])
    reader.available = False
    state = {"created": old.created}

    class PlatformProcess:
        def __init__(self, pid):
            self.pid = pid

        def create_time(self, fast_only=False):
            return state["created"]

        def oneshot_enter(self):
            state["created"] = old.created + 30  # A exits after construction.

        def oneshot_exit(self):
            pass

        def cpu_times(self):
            return psutil._common.pcputimes(54.0, 0.0, 0.0, 0.0)

        def memory_info(self):
            return SimpleNamespace(rss=16384)

        def name(self):
            return "B.exe"

    monkeypatch.setattr(psutil, "WINDOWS", True)
    monkeypatch.setattr(psutil._psplatform, "Process", PlatformProcess)
    system.Process = psutil.Process
    clock.advance(INTERVAL)

    row = collector.collect([])[0]

    assert {"cpu_percent", "memory_percent", "memory_bytes"} <= set(row.unavailable_fields)
    assert row.cpu_percent == 0.0 and row.memory_bytes == 0
    assert row.executable is None


def test_identity_without_creation_time_cannot_overwrite_a_native_owner():
    clock = FakeClock()
    old = FakeProcess(10, clock, name="old.exe")
    system = PsutilLikeSystem(old)
    reader = FakeNativeMetrics([old])
    collector = ProcessCollector(system, native_metrics=reader, clock=clock)
    collector.collect([])
    new = FakeProcess(10, clock, name="new.exe")
    new.created = None
    system.current[10] = new
    clock.advance(INTERVAL)

    row = collector.collect([])[0]

    assert row.name == "old.exe"  # Native data still describes A.
    assert row.create_time == datetime.fromtimestamp(old.created, UTC)
    assert row.executable is None and row.username is None
    assert {"executable", "username", "status", "parent_pid"} <= set(row.unavailable_fields)
    assert row.access_status == "partial"


def test_native_without_creation_time_cannot_attach_rss_to_a_cached_owner():
    clock = FakeClock()
    old = FakeProcess(10, clock, name="old.exe")
    collector, reader = native_collector([old], clock, identity_budget=0)
    collector.collect([])
    reader.read = lambda: {10: ProcessMetricReading(None, 100.0, 8 * 1024**3)}
    clock.advance(2.0)

    row = collector.collect([])[0]

    assert row.name == "Unknown" and row.create_time is None
    assert row.memory_bytes == 0 and row.executable is None
    assert {"name", "create_time", "cpu_percent", "memory_percent", "memory_bytes"} <= set(
        row.unavailable_fields
    )


def named_snapshot(name: str = "snapshot.exe", *, base: int = 0x100000) -> bytes:
    encoded = name.encode("utf-16-le", "surrogatepass")
    data = bytearray(snapshot_bytes([{"pid": 10, "created": 1_700_000_000.0}]))
    struct.pack_into("<HH4xQ", data, 0x38, len(encoded), len(encoded), base + len(data))
    return bytes(data) + encoded


def test_native_image_name_is_read_from_the_same_bounds_checked_record():
    row = parse_process_snapshot(named_snapshot("native-\ud800.exe"), buffer_address=0x100000)[10]

    assert row.name == "native-\\ud800.exe"
    assert row.started == 1_700_000_000.0


@pytest.mark.parametrize(
    ("length", "maximum", "pointer"),
    [(3, 4, 0x100100), (4, 2, 0x100100), (4, 4, 0),
     (4, 4, 0x100020), (4, 4, 0x100101), (100, 100, 0x100100)],
    ids=["odd-length", "length-over-maximum", "null", "inside-header", "misaligned", "past-end"],
)
def test_native_image_name_rejects_invalid_unicode_string_bounds(length, maximum, pointer):
    data = bytearray(named_snapshot())
    struct.pack_into("<HH4xQ", data, 0x38, length, maximum, pointer)

    with pytest.raises(native.InvalidSnapshot):
        parse_process_snapshot(bytes(data), buffer_address=0x100000)


def test_fallback_close_creation_times_still_identify_different_owners():
    clock = FakeClock()
    old = FakeProcess(10, clock, name="old.exe")
    old.cpu_seconds = 50.0
    system = PsutilLikeSystem(old)
    collector = ProcessCollector(system, clock=clock, identity_budget=0)
    collector.collect([])
    new = reuse_pid(system, None, clock, cpu_seconds=54.0, created_offset=0.000005)
    clock.advance(INTERVAL)

    row = collector.collect([])[0]

    assert "cpu_percent" in row.unavailable_fields
    assert row.name == "new.exe"
    assert row.create_time == datetime.fromtimestamp(new.created, UTC)


class OffsetNativeMetrics(FakeNativeMetrics):
    """Native readings whose creation time is shifted, as FILETIME conversion can be."""

    def __init__(self, processes, shift) -> None:
        super().__init__(processes)
        self.shift = shift

    def read(self):
        readings = super().read()
        return readings and {
            pid: reading._replace(started=self.shift(reading.started))
            for pid, reading in readings.items()
        }


ONE_ULP = [
    pytest.param(lambda value: math.nextafter(value, math.inf), id="native-one-ulp-later"),
    pytest.param(lambda value: math.nextafter(value, -math.inf), id="native-one-ulp-earlier"),
]
CREATED = 1_794_425_522.9718287  # A creation time measured on the development host.


def ulp_collector(shift, *, native_available):
    clock = FakeClock()
    process = FakeProcess(10, clock, created=CREATED, name="app.exe")
    process.cpu_seconds = 50.0
    reader = OffsetNativeMetrics([process], shift)
    reader.available = native_available
    system = PsutilLikeSystem(process)
    collector = ProcessCollector(
        system, native_metrics=reader, clock=clock, sampling_interval_seconds=INTERVAL
    )
    return collector, reader, process, clock


@pytest.mark.parametrize("shift", ONE_ULP)
def test_native_to_fallback_keeps_one_owner_across_a_one_ulp_difference(shift):
    collector, reader, process, clock = ulp_collector(shift, native_available=True)
    collector.collect([])
    process.cpu_seconds += 1.0
    clock.advance(2.0)
    native_row = collector.collect([])[0]
    assert native_row.cpu_percent == 50.0
    assert native_row.executable == "C:\\synthetic\\app.exe"

    reader.available = False
    process.cpu_seconds += 4.0
    clock.advance(8.0)
    row = collector.collect([])[0]

    assert collector.metric_source == "psutil"
    # The native baseline carries over: 4 CPU-seconds in 8 s on one CPU.
    assert row.cpu_percent == 50.0 and row.cpu_window_seconds == 8.0
    assert (row.name, row.executable, row.username) == (
        "app.exe", "C:\\synthetic\\app.exe", "synthetic"
    )
    assert row.create_time == native_row.create_time
    assert not {"name", "executable", "username", "cpu_percent"} & set(row.unavailable_fields)


@pytest.mark.parametrize("shift", ONE_ULP)
def test_fallback_to_native_keeps_one_owner_across_a_one_ulp_difference(shift):
    collector, reader, process, clock = ulp_collector(shift, native_available=False)
    collector.collect([])
    process.cpu_seconds += 2.0
    clock.advance(INTERVAL)
    fallback_row = collector.collect([])[0]
    assert fallback_row.cpu_percent == 20.0
    assert fallback_row.executable == "C:\\synthetic\\app.exe"

    reader.available = True
    process.cpu_seconds += 1.0
    clock.advance(2.0)
    row = collector.collect([])[0]

    assert collector.metric_source == "native"
    assert row.cpu_percent == 50.0 and row.cpu_window_seconds == 2.0
    assert (row.executable, row.username, row.status, row.parent_pid) == (
        "C:\\synthetic\\app.exe", "synthetic", "running", 1
    )
    assert row.create_time == fallback_row.create_time
    assert not {"executable", "username", "status", "parent_pid", "cpu_percent"} & set(
        row.unavailable_fields
    )


def test_pid_reuse_across_sources_is_still_rejected_microseconds_apart():
    collector, reader, process, clock = ulp_collector(lambda value: value, native_available=True)
    collector.collect([])
    reader.available = False
    new = reuse_pid(collector.psutil, None, clock, cpu_seconds=54.0, created_offset=0.000005)
    clock.advance(INTERVAL)

    row = collector.collect([])[0]

    assert "cpu_percent" in row.unavailable_fields
    # Only the new owner appears; its identity read ran in this same pass.
    assert (row.name, row.executable) == ("new.exe", new.executable)
    assert row.create_time == datetime.fromtimestamp(new.created, UTC)


def test_one_ulp_matches_cannot_chain_into_a_larger_difference():
    shifts = iter([CREATED, math.nextafter(math.nextafter(CREATED, math.inf), math.inf)])
    collector, reader, process, clock = ulp_collector(
        lambda value: next(shifts), native_available=True
    )
    process.created = math.nextafter(CREATED, math.inf)  # psutil: one ulp later.
    collector.collect([])  # Native stores CREATED; the identity read matches within 1 ulp.

    process.cpu_seconds += 1.0
    clock.advance(2.0)
    row = collector.collect([])[0]  # Native now two ulp from the stored time.

    assert "cpu_percent" in row.unavailable_fields
    assert row.executable is None


@pytest.mark.parametrize("error", [psutil.AccessDenied, psutil.NoSuchProcess])
def test_fallback_discards_the_sample_when_final_owner_check_fails(error):
    clock = FakeClock()
    owner = FakeProcess(10, clock)
    system = PsutilLikeSystem(owner)
    collector = ProcessCollector(system, clock=clock, identity_budget=0)
    collector.collect([])
    original_times = owner.cpu_times

    def exiting_times():
        result = original_times()
        system.Process = lambda pid: (_ for _ in ()).throw(error(pid))
        return result

    owner.cpu_times = exiting_times
    owner.cpu_seconds += 4.0
    clock.advance(INTERVAL)

    row = collector.collect([])[0]

    assert row.name == "Unknown" and row.create_time is None
    assert row.cpu_percent == 0.0 and row.memory_bytes == 0
    assert row.metrics_observed is None
    assert {"cpu_percent", "memory_percent", "memory_bytes", "name"} <= set(row.unavailable_fields)


def test_identity_fields_are_staged_until_the_final_owner_check():
    clock = FakeClock()
    owner_a = FakeProcess(10, clock, name="A.exe")
    owner_b = FakeProcess(10, clock, created=owner_a.created + 30, name="B.exe")
    system = PsutilLikeSystem(owner_a)
    reader = FakeNativeMetrics([owner_a])

    def reused_exe():
        system.current[10] = owner_b
        return owner_b.executable

    owner_a.exe = reused_exe
    collector = ProcessCollector(system, native_metrics=reader, clock=clock)

    row = collector.collect([])[0]

    assert row.name == "A.exe" and row.create_time == datetime.fromtimestamp(owner_a.created, UTC)
    assert row.memory_bytes == 4096
    assert row.executable is None and row.username is None
    assert {"executable", "username", "status", "parent_pid"} <= set(row.unavailable_fields)


def test_native_system_pseudo_process_has_only_explicitly_safe_fields(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    clock = FakeClock()
    system = FakeProcess(4, clock, name="System")
    system.created = None
    collector, _ = native_collector([system], clock)

    row = collector.collect([])[0]

    assert row.name == "System" and row.create_time is None
    assert row.memory_bytes == 4096
    assert {"cpu_percent", "create_time", "executable", "username"} <= set(row.unavailable_fields)
    assert "memory_bytes" not in row.unavailable_fields


@pytest.mark.parametrize("scenario", ["first-discovery", "snapshot-race", "fallback-race", "no-time"])
def test_overview_and_persisted_history_receive_only_coherent_owners(tmp_path, snapshot, scenario):
    clock = FakeClock()
    old = FakeProcess(10, clock, name="old.exe")
    old.cpu_seconds = 50.0
    new = FakeProcess(10, clock, created=old.created + 30, name="new.exe")
    system = PsutilLikeSystem(old)
    reader = FakeNativeMetrics([old])
    collector = ProcessCollector(system, native_metrics=reader, clock=clock)
    if scenario == "first-discovery":
        reader.processes = [new]
        system.current[10] = new
        expected_name, expected_time = "new.exe", new.created
    else:
        collector.collect([])
        clock.advance(INTERVAL)
        if scenario == "snapshot-race":
            reader.processes = [new]
            system.current[10] = FakeProcess(10, clock, created=new.created + 30, name="C.exe")
            expected_name, expected_time = "new.exe", new.created
        elif scenario == "fallback-race":
            reader.available = False

            def replaced_times():
                system.current[10] = new
                return SimpleNamespace(user=54.0, system=0.0)

            old.cpu_times = replaced_times
            # The identity rotation cannot verify either owner in this fixture.
            new.created = None
            expected_name, expected_time = "Unknown", None
        else:
            reader.read = lambda: {10: ProcessMetricReading(None, 100.0, 8 * 1024**3)}
            expected_name, expected_time = "Unknown", None
    rows = collector.collect([])
    current = snapshot.model_copy(update={"processes": rows})
    app = create_app(tmp_path / "ownership.db", start_background=False)
    with TestClient(app) as client:
        service = app.state.telemetry
        service.latest = current
        response = client.get("/api/overview")
        assert response.status_code == 200
        wire = response.json()["processes"][0]
        assert wire["name"] == expected_name
        assert wire["create_time"] == (
            datetime.fromtimestamp(expected_time, UTC).isoformat().replace("+00:00", "Z")
            if expected_time is not None else None
        )
        if scenario != "first-discovery":
            assert wire["executable"] is None and wire["username"] is None
        service._queue_history_sample(current)
        batch = service._history_queue.get_nowait()
        app.state.database.add_history_batch(batch.sample, batch.processes, batch.connection_summary)
        observations = app.state.database.list_process_observations(
            current.system.timestamp - timedelta(seconds=1),
            current.system.timestamp + timedelta(seconds=1),
        )
        assert len(observations) == 1
        stored = observations[0]
        assert stored.name == expected_name
        assert stored.create_time == (
            datetime.fromtimestamp(expected_time, UTC) if expected_time is not None else None
        )
        if expected_time is None:
            assert stored.cpu_percent is None and stored.memory_percent is None
