from __future__ import annotations

import ctypes
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime

import psutil

from app.models import SystemSample


def _windows_performance_memory() -> dict[str, int]:
    """Read aggregate Windows memory accounting without requiring elevation."""
    if os.name != "nt":
        return {}

    class PerformanceInformation(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_ulong),
            ("CommitTotal", ctypes.c_size_t),
            ("CommitLimit", ctypes.c_size_t),
            ("CommitPeak", ctypes.c_size_t),
            ("PhysicalTotal", ctypes.c_size_t),
            ("PhysicalAvailable", ctypes.c_size_t),
            ("SystemCache", ctypes.c_size_t),
            ("KernelTotal", ctypes.c_size_t),
            ("KernelPaged", ctypes.c_size_t),
            ("KernelNonpaged", ctypes.c_size_t),
            ("PageSize", ctypes.c_size_t),
            ("HandleCount", ctypes.c_ulong),
            ("ProcessCount", ctypes.c_ulong),
            ("ThreadCount", ctypes.c_ulong),
        ]

    info = PerformanceInformation()
    info.cb = ctypes.sizeof(info)
    get_performance_info = ctypes.windll.psapi.GetPerformanceInfo
    get_performance_info.argtypes = [ctypes.POINTER(PerformanceInformation), ctypes.c_ulong]
    get_performance_info.restype = ctypes.c_int
    if not get_performance_info(ctypes.byref(info), info.cb):
        raise OSError("GetPerformanceInfo failed")
    page_size = int(info.PageSize)
    return {
        "memory_available": int(info.PhysicalAvailable) * page_size,
        "memory_cached": int(info.SystemCache) * page_size,
        "commit_used": int(info.CommitTotal) * page_size,
        "commit_limit": int(info.CommitLimit) * page_size,
        "system_process_count": int(info.ProcessCount),
    }


class SystemCollector:
    def __init__(
        self,
        psutil_module=psutil,
        performance_memory_reader: Callable[[], dict[str, int]] | None = None,
    ) -> None:
        self.psutil = psutil_module
        self.performance_memory_reader = (
            _windows_performance_memory
            if performance_memory_reader is None and psutil_module is psutil
            else performance_memory_reader
        )

    def collect(self) -> SystemSample:
        errors: list[str] = []
        unavailable: list[str] = []
        try:
            memory = self.psutil.virtual_memory()
            memory_percent = round(float(memory.percent), 1)
            memory_used = int(memory.used)
            memory_total = int(memory.total)
            memory_available = int(
                getattr(memory, "available", max(0, memory_total - memory_used))
            )
        except (psutil.Error, OSError, RuntimeError, TypeError, ValueError) as exc:
            memory_percent = 0.0
            memory_used = 0
            memory_total = 0
            memory_available = None
            errors.append(f"Memory telemetry unavailable ({type(exc).__name__})")
            unavailable.extend(
                ("memory_percent", "memory_used", "memory_total", "memory_available")
            )
        memory_cached = None
        commit_used = None
        commit_limit = None
        commit_percent = None
        performance_process_count = None
        if self.performance_memory_reader is not None:
            try:
                performance = self.performance_memory_reader()
                memory_available = performance.get("memory_available", memory_available)
                memory_cached = performance.get("memory_cached")
                commit_used = performance.get("commit_used")
                commit_limit = performance.get("commit_limit")
                performance_process_count = performance.get("system_process_count")
                if commit_used is not None and commit_limit:
                    commit_percent = round(commit_used / commit_limit * 100.0, 1)
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                errors.append(f"Windows memory accounting unavailable ({type(exc).__name__})")
                unavailable.extend(
                    ("memory_cached", "commit_used", "commit_limit", "commit_percent")
                )
        pagefile_used = None
        pagefile_total = None
        pagefile_percent = None
        if hasattr(self.psutil, "swap_memory"):
            try:
                paging = self.psutil.swap_memory()
                pagefile_used = int(paging.used)
                pagefile_total = int(paging.total)
                pagefile_percent = round(float(paging.percent), 1)
            except (psutil.Error, OSError, RuntimeError, TypeError, ValueError) as exc:
                errors.append(f"Paging telemetry unavailable ({type(exc).__name__})")
                unavailable.extend(("pagefile_used", "pagefile_total", "pagefile_percent"))
        try:
            cpu_percent = round(float(self.psutil.cpu_percent(interval=None)), 1)
        except (psutil.Error, OSError, RuntimeError, TypeError, ValueError) as exc:
            cpu_percent = 0.0
            errors.append(f"CPU telemetry unavailable ({type(exc).__name__})")
            unavailable.append("cpu_percent")
        try:
            disk_percent = float(self.psutil.disk_usage("C:\\").percent)
        except (psutil.Error, OSError, RuntimeError, TypeError, ValueError) as exc:
            disk_percent = None
            errors.append(f"Disk telemetry unavailable ({type(exc).__name__})")
            unavailable.append("disk_percent")
        try:
            enumerated_process_count = len(self.psutil.pids())
            process_count = (
                performance_process_count
                if performance_process_count is not None and performance_process_count > 0
                else enumerated_process_count
            )
        except (psutil.Error, OSError, RuntimeError, TypeError, ValueError) as exc:
            if performance_process_count is not None and performance_process_count > 0:
                process_count = performance_process_count
            else:
                process_count = 0
                errors.append(f"Process count unavailable ({type(exc).__name__})")
                unavailable.append("process_count")
        try:
            uptime_seconds = max(0.0, time.time() - float(self.psutil.boot_time()))
        except (psutil.Error, OSError, RuntimeError, TypeError, ValueError) as exc:
            uptime_seconds = 0.0
            errors.append(f"System uptime unavailable ({type(exc).__name__})")
            unavailable.append("uptime_seconds")
        return SystemSample(
            timestamp=datetime.now(UTC),
            cpu_percent=cpu_percent,
            memory_percent=memory_percent,
            memory_used=memory_used,
            memory_total=memory_total,
            memory_available=memory_available,
            memory_cached=memory_cached,
            commit_used=commit_used,
            commit_limit=commit_limit,
            commit_percent=commit_percent,
            pagefile_used=pagefile_used,
            pagefile_total=pagefile_total,
            pagefile_percent=pagefile_percent,
            process_count=process_count,
            uptime_seconds=uptime_seconds,
            disk_percent=disk_percent,
            collection_error="; ".join(errors) or None,
            unavailable_metrics=unavailable,
        )
