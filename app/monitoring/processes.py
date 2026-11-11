from __future__ import annotations

import time
from collections import Counter
from collections.abc import Mapping
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import UTC, datetime

import psutil

from app.config import MAX_PROCESSES
from app.models import ConnectionInfo, ProcessInfo
from app.monitoring.process_selection import select_processes
from app.monitoring.windows_process_metrics import ProcessMetricReading, WindowsProcessMetrics
from app.text_utils import safe_text

# Shorter CPU-time windows are too noisy to report as a percentage.
MIN_CPU_WINDOW_SECONDS = 0.5
# A different creation time for a cached PID means Windows reused the PID.
PID_REUSE_TOLERANCE_SECONDS = 0.01
# Timing jitter can put a fully busy process slightly above 100%. Anything beyond
# this needs more CPU time than all logical processors could supply.
CPU_PERCENT_TOLERANCE = 1.05
_READ_ERRORS = (AttributeError, psutil.Error, OSError, RuntimeError, TimeoutError, ValueError)


@dataclass(slots=True)
class CachedProcess:
    name: str
    cpu_percent: float = 0.0
    memory_percent: float = 0.0
    memory_bytes: int = 0
    status: str = "unknown"
    parent_pid: int | None = None
    create_time: datetime | None = None
    executable: str | None = None
    username: str | None = None
    first_observed: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_observed: datetime = field(default_factory=lambda: datetime.now(UTC))
    # Collection cycle that discovered this process; orders identity reads first come,
    # first served.
    discovered: int = 0
    # When resource metrics were last read (wall clock and collector clock).
    metrics_observed: datetime | None = None
    metrics_clock: float | None = None
    cpu_window_seconds: float | None = None
    # CPU-time baseline for the next percentage calculation.
    cpu_seconds: float | None = None
    cpu_clock: float = 0.0
    # Creation time of the owner these values describe, from the native snapshot or
    # a new psutil instance. A different creation time means the PID was reused.
    started: float | None = None
    # process_iter()'s instance for this PID when it still belongs to a previous owner.
    stale_instance: object | None = None
    # Sampling pass of the last psutil metric read and identity read (0 = never).
    metric_attempt: int = 0
    identity_attempt: int = 0
    unavailable_fields: set[str] = field(
        default_factory=lambda: {
            "cpu_percent",
            "memory_percent",
            "memory_bytes",
            "status",
            "parent_pid",
            "create_time",
            "executable",
            "username",
        }
    )


class ProcessCollector:
    """Samples Windows processes with bounded per-cycle cost.

    PID/name enumeration happens every cycle. CPU time and working set for every
    process come from one validated whole-system snapshot per cycle
    (``WindowsProcessMetrics``). When that snapshot is unavailable or rejected,
    psutil reads metrics per process instead, least recently read first, at most
    ``metric_budget`` processes or ``metric_time_budget_seconds`` per sampling pass.
    Expensive identity fields rotate least recently attempted first under
    ``identity_budget`` per pass. Fields stay listed as unavailable until observed,
    and each row reports when its resource metrics were last read.

    ``cpu_percent`` is CPU time consumed between two readings divided by the elapsed
    time and by the logical CPU count: 0-100% of total machine capacity, the
    convention of the Task Manager Details tab. It is unavailable until two
    readings of the same process exist.
    """

    def __init__(
        self,
        psutil_module=psutil,
        *,
        native_metrics=None,
        metric_budget: int = 64,
        identity_budget: int = 1,
        sampling_interval_seconds: float = 10.0,
        metric_time_budget_seconds: float = 0.1,
        stale_after_seconds: float = 30.0,
        clock=time.monotonic,
    ) -> None:
        self.psutil = psutil_module
        # Native snapshots describe the real system, so they are only taken by default
        # alongside the real psutil module. Tests pass fake readers explicitly.
        if native_metrics is None and psutil_module is psutil:
            native_metrics = WindowsProcessMetrics()
        self.native_metrics = native_metrics
        self.metric_budget = metric_budget
        self.identity_budget = identity_budget
        self.sampling_interval_seconds = sampling_interval_seconds
        self.metric_time_budget_seconds = metric_time_budget_seconds
        self.stale_after_seconds = stale_after_seconds
        self.clock = clock
        self._cpu_count = max(1, self.psutil.cpu_count() or 1)
        self._physical_memory: int | None = None
        self._cache: dict[int, CachedProcess] = {}
        self._collections = 0
        self._sampling_pass = 0
        self._next_sample = 0.0
        self.metric_source = "none"
        self.collection_status = "healthy"
        self.collection_detail = ""
        self.enumerated_count = 0
        self.skipped_count = 0
        self.idle_excluded_count = 0

    def collect(
        self,
        connections: list[ConnectionInfo],
        connection_counts: Mapping[int, int] | None = None,
        remote_endpoints: Mapping[int, list[str]] | None = None,
    ) -> list[ProcessInfo]:
        counts = (
            Counter(connection_counts)
            if connection_counts is not None
            else Counter(item.pid for item in connections if item.pid is not None)
        )
        endpoints = remote_endpoints or {}
        self.collection_status = "healthy"
        self.collection_detail = ""
        self.enumerated_count = 0
        self.skipped_count = 0
        self.idle_excluded_count = 0
        try:
            iterator = list(self.psutil.process_iter(attrs=["pid", "name"], ad_value=None))
        except (psutil.AccessDenied, PermissionError) as exc:
            self.collection_status = "unavailable"
            self.collection_detail = (
                f"Process enumeration unavailable ({type(exc).__name__}; access was denied)"
            )
            return []
        except (OSError, RuntimeError, TimeoutError) as exc:
            self.collection_status = "unavailable"
            self.collection_detail = f"Process enumeration unavailable ({type(exc).__name__})"
            return []
        self.enumerated_count = len(iterator)
        self._collections += 1
        now = datetime.now(UTC)
        records: list[tuple[object, int, str]] = []
        active_pids: set[int] = set()
        for process in iterator:
            try:
                info = process.info
                pid = int(info["pid"])
                name = safe_text(info.get("name") or "Unknown")
            except (
                psutil.NoSuchProcess,
                psutil.AccessDenied,
                ProcessLookupError,
                KeyError,
                TypeError,
                ValueError,
            ):
                self.skipped_count += 1
                continue
            if pid == 0 or name.casefold() in {"system idle process", "idle"}:
                self.idle_excluded_count += 1
                continue
            active_pids.add(pid)
            cached = self._cache.get(pid)
            if cached is None:
                cached = self._cache[pid] = self._new_entry(name, now)
            elif cached.stale_instance is not None:
                if process is cached.stale_instance:
                    # psutil caches the name on this instance; it is the previous owner's.
                    name = cached.name
                else:
                    cached.stale_instance = None
            cached.name = name
            cached.last_observed = now
            # Test doubles and some psutil versions may return requested data eagerly.
            if self._apply_eager_info(cached, info):
                cached.metrics_observed = now
                cached.metrics_clock = self.clock()
            records.append((process, pid, name))

        self._cache = {pid: value for pid, value in self._cache.items() if pid in active_pids}
        full_pass = bool(records) and self.clock() >= self._next_sample
        if full_pass:
            self._next_sample = self.clock() + self.sampling_interval_seconds
            self._sampling_pass += 1

        readings = None
        taken_at = 0.0
        if records and self.native_metrics is not None:
            started = self.clock()
            readings = self.native_metrics.read()
            taken_at = (started + self.clock()) / 2
        if readings is not None:
            self.metric_source = "native"
            for _, pid, _ in records:
                reading = readings.get(pid)
                # A process started after the snapshot is read on the next cycle.
                if reading is not None:
                    self._apply_native(process, pid, reading, taken_at, now)
        elif full_pass:
            self.metric_source = "psutil"
            deadline = self.clock() + self.metric_time_budget_seconds
            for process, pid, _ in self._fallback_candidates(records, counts):
                self._refresh_metrics(process, pid, now)
                if self.clock() >= deadline:
                    break

        if full_pass:
            # Never-attempted processes first (network-active ones leading, then in
            # discovery order), then the least recently attempted, so a process whose
            # identity stays inaccessible cannot hold the budget.
            identity_candidates = sorted(
                records,
                key=lambda record: (
                    self._cache[record[1]].identity_attempt,
                    -counts.get(record[1], 0),
                    self._cache[record[1]].discovered,
                    record[1],
                ),
            )
            for process, pid, _ in identity_candidates[: self.identity_budget]:
                self._refresh_identity(process, pid, now)

        current = self.clock()
        processes = [
            ProcessInfo(
                pid=pid,
                name=cached.name,
                cpu_percent=cached.cpu_percent,
                memory_percent=cached.memory_percent,
                memory_bytes=cached.memory_bytes,
                status=cached.status,
                connection_count=counts.get(pid, 0),
                executable=cached.executable,
                parent_pid=cached.parent_pid,
                create_time=cached.create_time,
                username=cached.username,
                first_observed=cached.first_observed,
                last_observed=cached.last_observed,
                metrics_observed=cached.metrics_observed,
                metrics_stale=(
                    cached.metrics_clock is not None
                    and current - cached.metrics_clock > self.stale_after_seconds
                ),
                cpu_window_seconds=cached.cpu_window_seconds,
                remote_endpoints=list(endpoints.get(pid, []))[:50],
                access_status=(
                    "available"
                    if not cached.unavailable_fields
                    else "partial"
                ),
                unavailable_fields=sorted(cached.unavailable_fields),
            )
            for pid, cached in self._cache.items()
        ]
        processes = select_processes(processes, MAX_PROCESSES)
        if self.skipped_count:
            self.collection_status = "partial"
            self.collection_detail = (
                f"{self.skipped_count} process record(s) changed or were inaccessible during collection"
            )
        return processes

    def _new_entry(self, name: str, now: datetime) -> CachedProcess:
        return CachedProcess(
            name=name, first_observed=now, last_observed=now, discovered=self._collections
        )

    def _current_process(self, pid: int, enumerated: object | None) -> object | None:
        """A psutil Process for whoever owns the PID now.

        psutil caches the name, parent, executable and creation time on each Process
        instance, and process_iter() can keep an instance after its PID is reused.
        Reads that must describe the current owner use a new instance.
        """
        factory = getattr(self.psutil, "Process", None)
        if factory is None:  # Test doubles without Process().
            return enumerated
        try:
            return factory(pid)
        except _READ_ERRORS:
            return None

    @staticmethod
    def _creation_time(process: object) -> float | None:
        try:
            value = float(process.create_time())
        except (*_READ_ERRORS, OverflowError, TypeError):
            return None
        # psutil reports 0 for the Idle and System pseudo-processes.
        return value if value > 0 else None

    def _owner_entry(
        self, pid: int, enumerated: object, started: float | None, now: datetime
    ) -> CachedProcess:
        """The cache entry for the PID's current owner, identified by creation time."""
        cached = self._cache[pid]
        if started is None:
            return cached
        if (
            cached.started is not None
            and abs(started - cached.started) > PID_REUSE_TOLERANCE_SECONDS
        ):
            cached = self._replace_owner(pid, enumerated, now)
        cached.started = started
        try:
            cached.create_time = datetime.fromtimestamp(started, UTC)
        except (OverflowError, OSError, ValueError):
            return cached
        cached.unavailable_fields.discard("create_time")
        return cached

    def _replace_owner(self, pid: int, enumerated: object, now: datetime) -> CachedProcess:
        """Start over for a reused PID: nothing cached for the previous owner applies."""
        entry = self._cache[pid] = self._new_entry("Unknown", now)
        entry.stale_instance = enumerated
        current = self._current_process(pid, None)
        if current is not None:
            try:
                entry.name = safe_text(current.name())
            except (*_READ_ERRORS, TypeError):
                pass
        return entry

    def _fallback_candidates(
        self, records: list[tuple[object, int, str]], counts: Mapping[int, int]
    ) -> list[tuple[object, int, str]]:
        """This pass's psutil metric reads: least recently read first."""
        ordered = sorted(
            records,
            key=lambda record: (
                self._cache[record[1]].metric_attempt,
                -counts.get(record[1], 0),
                record[1],
            ),
        )
        return ordered[: max(0, self.metric_budget)]

    @staticmethod
    def _apply_eager_info(cached: CachedProcess, info: dict[str, object]) -> bool:
        memory_info = info.get("memory_info")
        eager = {
            "cpu_percent": info.get("cpu_percent"),
            "memory_percent": info.get("memory_percent"),
            "memory_bytes": getattr(memory_info, "rss", None),
            "status": info.get("status"),
        }
        if eager["cpu_percent"] is not None:
            cached.cpu_percent = round(max(0.0, float(eager["cpu_percent"])), 1)
            cached.unavailable_fields.discard("cpu_percent")
        if eager["memory_percent"] is not None:
            cached.memory_percent = round(max(0.0, float(eager["memory_percent"])), 2)
            cached.unavailable_fields.discard("memory_percent")
        if eager["memory_bytes"] is not None:
            cached.memory_bytes = max(0, int(eager["memory_bytes"]))
            cached.unavailable_fields.discard("memory_bytes")
        if eager["status"] is not None:
            cached.status = str(eager["status"])
            cached.unavailable_fields.discard("status")
        return any(
            eager[name] is not None for name in ("cpu_percent", "memory_percent", "memory_bytes")
        )

    def _apply_native(
        self,
        enumerated: object,
        pid: int,
        reading: ProcessMetricReading,
        taken_at: float,
        now: datetime,
    ) -> None:
        cached = self._owner_entry(pid, enumerated, reading.started, now)
        physical_memory = self._total_physical_memory()
        self._record_metrics(
            cached,
            # Without a creation time the counter cannot be tied to one owner.
            cpu_seconds=reading.cpu_seconds if reading.started is not None else None,
            memory_percent=(
                100.0 * reading.rss / physical_memory if physical_memory else None
            ),
            memory_bytes=reading.rss,
            at=taken_at,
            now=now,
        )

    def _total_physical_memory(self) -> int | None:
        if self._physical_memory is None:
            try:
                self._physical_memory = int(self.psutil.virtual_memory().total) or None
            except _READ_ERRORS + (TypeError,):
                return None
        return self._physical_memory

    def _refresh_metrics(self, enumerated: object, pid: int, now: datetime) -> None:
        self._cache[pid].metric_attempt = self._sampling_pass
        started = cpu_seconds = memory_percent = memory_bytes = None
        at = self.clock()
        process = self._current_process(pid, enumerated)
        if process is not None:
            # oneshot() lets psutil answer every read below from one restricted-access
            # query, including the creation time that identifies the owner.
            with getattr(process, "oneshot", nullcontext)():
                started = self._creation_time(process)
                try:
                    times = process.cpu_times()
                    cpu_seconds = float(times.user) + float(times.system)
                except _READ_ERRORS:
                    pass
                at = self.clock()
                try:
                    memory_percent = float(process.memory_percent())
                except _READ_ERRORS:
                    pass
                try:
                    memory_bytes = int(process.memory_info().rss)
                except _READ_ERRORS:
                    pass
        cached = self._owner_entry(pid, enumerated, started, now)
        cached.metric_attempt = self._sampling_pass
        if started is None:
            # Values that cannot be tied to one owner are not attributed to this row.
            cpu_seconds = memory_percent = memory_bytes = None
        self._record_metrics(
            cached,
            cpu_seconds=cpu_seconds,
            memory_percent=memory_percent,
            memory_bytes=memory_bytes,
            at=at,
            now=now,
        )

    def _record_metrics(
        self,
        cached: CachedProcess,
        *,
        cpu_seconds: float | None,
        memory_percent: float | None,
        memory_bytes: int | None,
        at: float,
        now: datetime,
    ) -> None:
        """Store one reading; a field that could not be read becomes unavailable."""
        if cpu_seconds is None:
            self._reset_cpu(cached)
        else:
            self._update_cpu(cached, cpu_seconds, at)
        if memory_percent is None:
            cached.memory_percent = 0.0
            cached.unavailable_fields.add("memory_percent")
        else:
            cached.memory_percent = round(max(0.0, memory_percent), 2)
            cached.unavailable_fields.discard("memory_percent")
        if memory_bytes is None:
            cached.memory_bytes = 0
            cached.unavailable_fields.add("memory_bytes")
        else:
            cached.memory_bytes = max(0, memory_bytes)
            cached.unavailable_fields.discard("memory_bytes")
        if cpu_seconds is not None or memory_percent is not None or memory_bytes is not None:
            cached.metrics_observed = now
            cached.metrics_clock = at

    @staticmethod
    def _reset_cpu(cached: CachedProcess) -> None:
        cached.cpu_percent = 0.0
        cached.cpu_window_seconds = None
        cached.cpu_seconds = None
        cached.unavailable_fields.add("cpu_percent")

    def _update_cpu(self, cached: CachedProcess, cpu_seconds: float, at: float) -> None:
        previous = cached.cpu_seconds
        if previous is None or cpu_seconds < previous:
            # First reading, or the counter went backwards because the PID now belongs
            # to another process: there is no interval yet, which is not 0% usage.
            self._reset_cpu(cached)
            cached.cpu_seconds = cpu_seconds
            cached.cpu_clock = at
            return
        elapsed = at - cached.cpu_clock
        if elapsed < MIN_CPU_WINDOW_SECONDS:
            return
        percent = 100.0 * (cpu_seconds - previous) / (elapsed * self._cpu_count)
        if percent > 100.0 * CPU_PERCENT_TOLERANCE:
            # Impossible usage means a bad counter: measure again from this reading
            # rather than reporting a believable clamped value.
            self._reset_cpu(cached)
            cached.cpu_seconds = cpu_seconds
            cached.cpu_clock = at
            return
        cached.cpu_percent = round(min(100.0, max(0.0, percent)), 1)
        cached.cpu_window_seconds = round(elapsed, 1)
        cached.cpu_seconds = cpu_seconds
        cached.cpu_clock = at
        cached.unavailable_fields.discard("cpu_percent")

    def _refresh_identity(self, enumerated: object, pid: int, now: datetime) -> None:
        self._cache[pid].identity_attempt = self._sampling_pass
        process = self._current_process(pid, enumerated)
        if process is None:
            return
        with getattr(process, "oneshot", nullcontext)():
            # A new instance's creation time replaces the entry if the owner changed.
            cached = self._owner_entry(pid, enumerated, self._creation_time(process), now)
            cached.identity_attempt = self._sampling_pass
            calls = {
                "status": ("status", str),
                "parent_pid": ("ppid", int),
                "executable": ("exe", safe_text),
                "username": ("username", safe_text),
            }
            for field_name, (method_name, converter) in calls.items():
                try:
                    value = converter(getattr(process, method_name)())
                    setattr(cached, field_name, value)
                    cached.unavailable_fields.discard(field_name)
                except (
                    AttributeError,
                    psutil.Error,
                    OSError,
                    RuntimeError,
                    TimeoutError,
                    TypeError,
                    ValueError,
                ):
                    continue
