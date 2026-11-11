"""One bounded process selection for the dashboard, history and AI context."""

from __future__ import annotations

from app.models import ProcessInfo


def measured(process: ProcessInfo, field: str) -> float | None:
    """A current measurement; None when the value is stale or unavailable."""
    if process.metrics_stale or field in process.unavailable_fields:
        return None
    return getattr(process, field)


def _rank(value: float | None) -> float:
    # Any current measurement, including a real 0, ranks above a missing one.
    return -1.0 if value is None else float(value)


def network_rank(process: ProcessInfo) -> tuple[int, float, float]:
    return (
        process.connection_count,
        _rank(measured(process, "cpu_percent")),
        _rank(measured(process, "memory_bytes")),
    )


def select_processes(processes: list[ProcessInfo], limit: int) -> list[ProcessInfo]:
    """Choose at most ``limit`` rows so that no single ranking hides another.

    A third of the rows each go to the highest current CPU, the largest current
    working set and the most connections, so unconnected heavy processes stay
    visible. Remaining rows follow network order. Stale or unavailable values never
    rank. The result is in network order.
    """
    if len(processes) <= limit:
        return sorted(processes, key=network_rank, reverse=True)
    quota = limit // 3
    rankings = [
        sorted(
            (item for item in processes if (measured(item, field) or 0) > 0),
            key=lambda item, field=field: measured(item, field),
            reverse=True,
        )
        for field in ("cpu_percent", "memory_bytes")
    ]
    rankings.append(
        sorted(
            (item for item in processes if item.connection_count > 0),
            key=network_rank,
            reverse=True,
        )
    )
    selected: dict[int, ProcessInfo] = {}
    for ranking in rankings:
        added = 0
        for item in ranking:
            if added >= quota:
                break
            if item.pid not in selected:
                selected[item.pid] = item
                added += 1
    for item in sorted(processes, key=network_rank, reverse=True):
        if len(selected) >= limit:
            break
        selected.setdefault(item.pid, item)
    return sorted(selected.values(), key=network_rank, reverse=True)
