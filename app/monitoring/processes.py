from __future__ import annotations

import time
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime

import psutil

from app.config import MAX_PROCESSES
from app.models import ConnectionInfo, ProcessInfo
from app.text_utils import safe_text


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
    """Incrementally samples Windows processes to bound per-cycle monitoring cost.

    PID/name enumeration is inexpensive and happens each cycle. Resource metrics are
    rotated through a bounded number of PIDs; expensive identity fields use a smaller
    budget. The API labels fields unavailable until actually observed.
    """

    def __init__(
        self,
        psutil_module=psutil,
        *,
        metric_budget: int = 8,
        identity_budget: int = 1,
        sampling_interval_seconds: float = 10.0,
        clock=time.monotonic,
    ) -> None:
        self.psutil = psutil_module
        self.metric_budget = metric_budget
        self.identity_budget = identity_budget
        self.sampling_interval_seconds = sampling_interval_seconds
        self.clock = clock
        self._cpu_count = max(1, self.psutil.cpu_count() or 1)
        self._cache: dict[int, CachedProcess] = {}
        self._cursor = 0
        self._next_sample = 0.0
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
            cached = self._cache.setdefault(pid, CachedProcess(name=name))
            cached.name = name
            cached.last_observed = now
            # Test doubles and some psutil versions may return requested data eagerly.
            self._apply_eager_info(cached, info)
            records.append((process, pid, name))

        self._cache = {pid: value for pid, value in self._cache.items() if pid in active_pids}
        if records and self.clock() >= self._next_sample:
            self._next_sample = self.clock() + self.sampling_interval_seconds
            connected = sorted(
                (record for record in records if record[1] in counts),
                key=lambda record: counts.get(record[1], 0),
                reverse=True,
            )
            priority = connected[: max(4, self.metric_budget // 2)]
            priority_pids = {record[1] for record in priority}
            remaining = [record for record in records if record[1] not in priority_pids]
            start = self._cursor % len(remaining) if remaining else 0
            rotated = remaining[start:] + remaining[:start]
            metric_records = (priority + rotated)[: self.metric_budget]
            self._cursor = (start + max(1, self.metric_budget - len(priority))) % max(
                len(remaining), 1
            )
            for process, pid, _ in metric_records:
                self._refresh_metrics(process, self._cache[pid])

            identity_candidates = sorted(
                records,
                key=lambda record: (
                    record[1] not in counts,
                    self._cache[record[1]].executable is not None,
                    -counts.get(record[1], 0),
                ),
            )
            for process, pid, _ in identity_candidates[: self.identity_budget]:
                self._refresh_identity(process, self._cache[pid])

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
        processes.sort(
            key=lambda item: (
                item.connection_count,
                item.cpu_percent,
                item.memory_bytes,
            ),
            reverse=True,
        )
        if self.skipped_count:
            self.collection_status = "partial"
            self.collection_detail = (
                f"{self.skipped_count} process record(s) changed or were inaccessible during collection"
            )
        return processes[:MAX_PROCESSES]

    @staticmethod
    def _apply_eager_info(cached: CachedProcess, info: dict[str, object]) -> None:
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

    def _refresh_metrics(self, process: object, cached: CachedProcess) -> None:
        try:
            cached.cpu_percent = round(
                min(100.0, max(0.0, float(process.cpu_percent()) / self._cpu_count)), 1
            )
            cached.unavailable_fields.discard("cpu_percent")
        except (AttributeError, psutil.Error, OSError, RuntimeError, TimeoutError, ValueError):
            pass
        try:
            cached.memory_percent = round(
                max(0.0, float(process.memory_percent())), 2
            )
            cached.unavailable_fields.discard("memory_percent")
        except (AttributeError, psutil.Error, OSError, RuntimeError, TimeoutError, ValueError):
            pass
        try:
            cached.memory_bytes = max(0, int(process.memory_info().rss))
            cached.unavailable_fields.discard("memory_bytes")
        except (AttributeError, psutil.Error, OSError, RuntimeError, TimeoutError, ValueError):
            pass

    @staticmethod
    def _refresh_identity(process: object, cached: CachedProcess) -> None:
        try:
            observed_create_time = datetime.fromtimestamp(
                float(process.create_time()), UTC
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
            cached.create_time = observed_create_time
            cached.unavailable_fields.discard("create_time")
        except (
            AttributeError,
            psutil.Error,
            OSError,
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
