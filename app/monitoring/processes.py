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
from app.monitoring.windows_process_metrics import ProcessMetricReading, WindowsProcessMetrics
from app.text_utils import safe_text

# Shorter CPU-time windows are too noisy to report as a percentage.
MIN_CPU_WINDOW_SECONDS = 0.5
# A different creation time for a cached PID means Windows reused the PID.
PID_REUSE_TOLERANCE_SECONDS = 0.01
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
    # Creation time from native snapshots, used to detect PID reuse.
    started: float | None = None
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
                    self._apply_native(pid, reading, taken_at, now)
        elif full_pass:
            self.metric_source = "psutil"
            deadline = self.clock() + self.metric_time_budget_seconds
            for process, pid, _ in self._fallback_candidates(records, counts):
                self._refresh_metrics(process, self._cache[pid], now)
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
                self._cache[pid].identity_attempt = self._sampling_pass
                with getattr(process, "oneshot", nullcontext)():
                    self._refresh_identity(process, self._cache[pid])

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
        processes = self._bounded_selection(processes)
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

    @staticmethod
    def _bounded_selection(processes: list[ProcessInfo]) -> list[ProcessInfo]:
        """Keep the display bound without letting one ranking hide the other.

        Half of the rows are reserved for the heaviest CPU/memory consumers; the rest
        are filled in network-activity order, which is also the returned order.
        """

        def network_rank(item: ProcessInfo) -> tuple[int, float, int]:
            return (item.connection_count, item.cpu_percent, item.memory_bytes)

        if len(processes) <= MAX_PROCESSES:
            return sorted(processes, key=network_rank, reverse=True)
        by_resource = sorted(
            processes,
            key=lambda item: (item.cpu_percent, item.memory_bytes, item.connection_count),
            reverse=True,
        )
        selected = {item.pid: item for item in by_resource[: MAX_PROCESSES // 2]}
        for item in sorted(processes, key=network_rank, reverse=True):
            if len(selected) >= MAX_PROCESSES:
                break
            selected.setdefault(item.pid, item)
        return sorted(selected.values(), key=network_rank, reverse=True)

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
        self, pid: int, reading: ProcessMetricReading, taken_at: float, now: datetime
    ) -> None:
        cached = self._cache[pid]
        if (
            reading.started is not None
            and cached.started is not None
            and abs(reading.started - cached.started) > PID_REUSE_TOLERANCE_SECONDS
        ):
            # Windows reused the PID: nothing cached belongs to the new process.
            cached = self._cache[pid] = self._new_entry(cached.name, now)
        if reading.started is not None:
            cached.started = reading.started
        physical_memory = self._total_physical_memory()
        self._record_metrics(
            cached,
            cpu_seconds=reading.cpu_seconds,
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

    def _refresh_metrics(self, process: object, cached: CachedProcess, now: datetime) -> None:
        cached.metric_attempt = self._sampling_pass
        cpu_seconds = memory_percent = memory_bytes = None
        # oneshot() lets psutil answer every read below from one restricted-access query.
        with getattr(process, "oneshot", nullcontext)():
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
        cached.cpu_percent = round(min(100.0, max(0.0, percent)), 1)
        cached.cpu_window_seconds = round(elapsed, 1)
        cached.cpu_seconds = cpu_seconds
        cached.cpu_clock = at
        cached.unavailable_fields.discard("cpu_percent")

    @staticmethod
    def _refresh_identity(process: object, cached: CachedProcess) -> None:
        try:
            # psutil caches create_time() per Process instance and can keep an instance
            # across a PID reuse; the native snapshot reads it fresh every cycle.
            observed_create_time = datetime.fromtimestamp(
                cached.started if cached.started is not None else float(process.create_time()),
                UTC,
            )
            if (
                cached.create_time is not None
                and cached.create_time != observed_create_time
            ):
                cached.status = "unknown"
                cached.parent_pid = None
                cached.executable = None
                cached.username = None
                cached.first_observed = datetime.now(UTC)
                cached.unavailable_fields.update(
                    {"status", "parent_pid", "executable", "username"}
                )
                # CPU time read for the previous owner of this PID is not a baseline.
                ProcessCollector._reset_cpu(cached)
            cached.create_time = observed_create_time
            cached.unavailable_fields.discard("create_time")
        except (
            AttributeError,
            psutil.Error,
            OSError,
            OverflowError,
            RuntimeError,
            TimeoutError,
            TypeError,
            ValueError,
        ):
            pass
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
