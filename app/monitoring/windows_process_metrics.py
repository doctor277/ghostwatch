"""Whole-system Windows process CPU and memory snapshot, validated before use.

psutil reads CPU time and working set one process at a time. A standard user
cannot open most service processes, and for each of those psutil falls back to
NtQuerySystemInformation(SystemProcessInformation): a query that returns every
process on the system. Refreshing all processes that way repeats the
whole-system query once per protected process. This module issues it once.

Only the 64-bit SYSTEM_PROCESS_INFORMATION layout is read. The kernel output is
copied into a bytes object and parsed with bounds-checked ``struct`` reads, so
a malformed buffer raises instead of reading outside it. Every snapshot must
also agree with psutil's own reading of the current process and cover the PIDs
psutil enumerates; otherwise it is rejected and the caller falls back to psutil.
"""

from __future__ import annotations

import ctypes
import functools
import logging
import os
import struct
import sys
from collections.abc import Callable
from typing import NamedTuple

import psutil

LOGGER = logging.getLogger(__name__)

_SYSTEM_PROCESS_INFORMATION = 5
_STATUS_INFO_LENGTH_MISMATCH = 0xC0000004
_STATUS_BUFFER_TOO_SMALL = 0xC0000023
_NTSTATUS_WARNING_OR_ERROR = 0x80000000

# x64 SYSTEM_PROCESS_INFORMATION prefix: NextEntryOffset (0x00), NumberOfThreads
# (0x04), CreateTime (0x20), UserTime (0x28), KernelTime (0x30), UniqueProcessId
# (0x50) and WorkingSetSize (0x90). winternl.h documents the entry offset, PID
# and working set; the times sit in its Reserved1 block, at the offsets psutil's
# C extension also reads. A process entry is 0x100 bytes followed by one 0x50-byte
# SYSTEM_THREAD_INFORMATION per thread.
_ENTRY = struct.Struct("<II24xqqq24xQ56xQ")
_PROCESS_ENTRY_SIZE = 0x100
_THREAD_ENTRY_SIZE = 0x50
_MAX_PID = 0xFFFFFFFF

_FILETIME_UNIX_EPOCH = 116_444_736_000_000_000
_TICKS_PER_SECOND = 10_000_000

_INITIAL_BUFFER_BYTES = 512 * 1024
_MAX_BUFFER_BYTES = 64 * 1024 * 1024
_MAX_QUERY_ATTEMPTS = 4

# Cross-check tolerances against psutil's reading of the current process. Both
# sources use the same kernel counters (measured: identical CPU time and creation
# time); the working set moves by the size of the query buffer itself.
_CREATE_TIME_TOLERANCE_SECONDS = 0.01
_CPU_TIME_TOLERANCE_SECONDS = 0.05
_WORKING_SET_TOLERANCE_BYTES = 32 * 1024 * 1024
_MIN_PID_COVERAGE = 0.9

MAX_CONSECUTIVE_FAILURES = 3


class InvalidSnapshot(ValueError):
    """The snapshot does not match the expected layout or psutil's view."""


class ProcessMetricReading(NamedTuple):
    # Creation time in seconds since the Unix epoch; None when Windows reports none.
    started: float | None
    # User plus kernel CPU time consumed since the process started.
    cpu_seconds: float
    # Working set, which psutil reports as rss on Windows.
    rss: int


def native_supported() -> bool:
    return sys.platform == "win32" and struct.calcsize("P") == 8


def parse_process_snapshot(
    data: bytes, *, max_rss: int | None = None
) -> dict[int, ProcessMetricReading]:
    """Parse SystemProcessInformation output, rejecting anything inconsistent."""
    readings: dict[int, ProcessMetricReading] = {}
    offset = 0
    for _ in range(len(data) // _PROCESS_ENTRY_SIZE + 1):
        try:
            next_entry, threads, created, user, kernel, pid, rss = _ENTRY.unpack_from(
                data, offset
            )
        except struct.error as exc:
            raise InvalidSnapshot("process entry extends past the buffer") from exc
        entry_end = offset + _PROCESS_ENTRY_SIZE + threads * _THREAD_ENTRY_SIZE
        if entry_end > len(data):
            raise InvalidSnapshot("thread entries extend past the buffer")
        if next_entry and (next_entry % 8 or offset + next_entry < entry_end):
            raise InvalidSnapshot("next entry offset overlaps the current entry")
        if pid > _MAX_PID or pid in readings:
            raise InvalidSnapshot("invalid or duplicate process ID")
        if user < 0 or kernel < 0 or 0 < created < _FILETIME_UNIX_EPOCH or created < 0:
            raise InvalidSnapshot("invalid process time")
        if max_rss is not None and rss > max_rss:
            raise InvalidSnapshot("working set exceeds physical memory")
        readings[pid] = ProcessMetricReading(
            started=(
                (created - _FILETIME_UNIX_EPOCH) / _TICKS_PER_SECOND if created else None
            ),
            cpu_seconds=(user + kernel) / _TICKS_PER_SECOND,
            rss=rss,
        )
        if next_entry == 0:
            return readings
        offset += next_entry
    raise InvalidSnapshot("process entries do not terminate")


@functools.cache
def _nt_query_system_information() -> Callable[..., int]:
    # A private WinDLL instance keeps these prototypes off the shared ctypes.windll.
    function = ctypes.WinDLL("ntdll").NtQuerySystemInformation
    function.argtypes = [
        ctypes.c_ulong,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_ulong),
    ]
    function.restype = ctypes.c_long
    return function


class WindowsProcessMetrics:
    """Reads CPU time, working set and creation time for every process at once.

    ``read()`` returns ``None`` whenever the snapshot is unavailable or fails
    validation, so callers fall back to per-process psutil reads. After
    ``MAX_CONSECUTIVE_FAILURES`` failures in a row the reader disables itself.
    """

    def __init__(
        self,
        psutil_module=psutil,
        *,
        nt_query: Callable[..., int] | None = None,
        own_pid: int | None = None,
    ) -> None:
        self.psutil = psutil_module
        self._nt_query = nt_query
        self._own_pid = os.getpid() if own_pid is None else own_pid
        self._own_process = None
        self._buffer_bytes = _INITIAL_BUFFER_BYTES
        self._physical_memory: int | None = None
        self.consecutive_failures = 0
        self.last_error: str | None = None
        self.disabled_reason: str | None = None
        if nt_query is None and not native_supported():
            self.disabled_reason = "requires 64-bit Python on Windows"

    def read(self) -> dict[int, ProcessMetricReading] | None:
        if self.disabled_reason is not None:
            return None
        try:
            enumerated = set(self.psutil.pids())
            before = self._own_reading()
            data = self._query()
            after = self._own_reading()
            readings = parse_process_snapshot(data, max_rss=self._max_rss())
            self._cross_check(readings, enumerated, before, after)
        except Exception as exc:
            # Any failure means there is no trustworthy snapshot; the collector falls
            # back to psutil instead of losing the cycle.
            self._record_failure(exc)
            return None
        self.consecutive_failures = 0
        self.last_error = None
        return readings

    def _own_reading(self) -> ProcessMetricReading:
        if self._own_process is None:
            self._own_process = self.psutil.Process(self._own_pid)
        times = self._own_process.cpu_times()
        return ProcessMetricReading(
            started=float(self._own_process.create_time()),
            cpu_seconds=float(times.user) + float(times.system),
            rss=int(self._own_process.memory_info().rss),
        )

    def _max_rss(self) -> int | None:
        if self._physical_memory is None:
            try:
                self._physical_memory = int(self.psutil.virtual_memory().total)
            except (AttributeError, OSError, psutil.Error, TypeError, ValueError):
                return None
        return self._physical_memory

    def _query(self) -> bytes:
        function = self._nt_query or _nt_query_system_information()
        size = self._buffer_bytes
        for _ in range(_MAX_QUERY_ATTEMPTS):
            buffer = ctypes.create_string_buffer(size)
            returned = ctypes.c_ulong(0)
            status = function(_SYSTEM_PROCESS_INFORMATION, buffer, size, returned) & 0xFFFFFFFF
            if status in (_STATUS_INFO_LENGTH_MISMATCH, _STATUS_BUFFER_TOO_SMALL):
                # Leave room for processes created before the retry.
                size = max(size * 2, returned.value + returned.value // 4)
                if size > _MAX_BUFFER_BYTES:
                    raise OSError("process snapshot exceeds the buffer limit")
                continue
            if status & _NTSTATUS_WARNING_OR_ERROR:
                raise OSError(f"NtQuerySystemInformation failed (NTSTATUS 0x{status:08X})")
            if not 0 < returned.value <= size:
                raise InvalidSnapshot("invalid returned snapshot length")
            self._buffer_bytes = min(
                _MAX_BUFFER_BYTES,
                max(_INITIAL_BUFFER_BYTES, returned.value + returned.value // 4),
            )
            return ctypes.string_at(buffer, returned.value)
        raise OSError("process snapshot kept growing between attempts")

    def _cross_check(
        self,
        readings: dict[int, ProcessMetricReading],
        enumerated: set[int],
        before: ProcessMetricReading,
        after: ProcessMetricReading,
    ) -> None:
        own = readings.get(self._own_pid)
        if own is None:
            raise InvalidSnapshot("current process is missing from the snapshot")
        if own.started is None or abs(own.started - after.started) > (
            _CREATE_TIME_TOLERANCE_SECONDS
        ):
            raise InvalidSnapshot("creation time disagrees with psutil")
        if not (
            before.cpu_seconds - _CPU_TIME_TOLERANCE_SECONDS
            <= own.cpu_seconds
            <= after.cpu_seconds + _CPU_TIME_TOLERANCE_SECONDS
        ):
            raise InvalidSnapshot("CPU time disagrees with psutil")
        if not (
            min(before.rss, after.rss) - _WORKING_SET_TOLERANCE_BYTES
            <= own.rss
            <= max(before.rss, after.rss) + _WORKING_SET_TOLERANCE_BYTES
        ):
            raise InvalidSnapshot("working set disagrees with psutil")
        if enumerated and len(enumerated & readings.keys()) < _MIN_PID_COVERAGE * len(
            enumerated
        ):
            raise InvalidSnapshot("snapshot does not cover the enumerated processes")

    def _record_failure(self, exc: Exception) -> None:
        self.consecutive_failures += 1
        # Messages describe the snapshot or GhostWatch's own process, never another one.
        self.last_error = f"{type(exc).__name__}: {exc}"
        if self.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            self.disabled_reason = self.last_error
            LOGGER.warning(
                "Native process metrics disabled; using per-process psutil sampling (%s)",
                self.last_error,
            )
