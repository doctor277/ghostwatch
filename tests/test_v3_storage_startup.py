from __future__ import annotations

import asyncio
import sqlite3
import threading

from app.service import TelemetryService
from app.storage.database import Database


async def test_clean_start_does_not_announce_a_storage_recovery(tmp_path):
    service = TelemetryService(Database(tmp_path / "clean.db"))

    assert await service.initialize_storage()

    assert service.storage.status == "healthy"
    assert "storage_recovery" not in [event.event_type for event in service.live_events]


async def test_recovery_after_a_real_failure_is_still_announced(tmp_path, monkeypatch):
    service = TelemetryService(Database(tmp_path / "flaky.db"), storage_retry_seconds=0)
    original = service.database.initialize

    def fail_once():
        monkeypatch.setattr(service.database, "initialize", original)
        raise OSError("synthetic outage")

    monkeypatch.setattr(service.database, "initialize", fail_once)
    assert not await service.initialize_storage()
    assert service.storage.status == "degraded"

    await service._maybe_recover_storage()

    assert service.storage.status == "healthy"
    assert service.live_events[0].event_type == "storage_recovery"


class Static:
    def __init__(self, value):
        self.value = value
        self.connection_counts_by_pid = {}

    def collect(self, *args, **kwargs):
        return self.value


async def started_history_service(path, system_sample, network_sample):
    service = TelemetryService(Database(path))
    assert await service.initialize_storage()
    service.system_collector = Static(system_sample)
    service.network_collector = Static((network_sample, []))
    service.process_collector = Static([])
    service._history_task = asyncio.create_task(service._history_writer())
    return service


async def test_shutdown_flushes_queued_history_and_restarts_cleanly(
    tmp_path, system_sample, network_sample
):
    path = tmp_path / "shutdown.db"
    service = await started_history_service(path, system_sample, network_sample)
    for _ in range(5):
        await service.collect_once()

    await service.stop()

    assert service.database.telemetry_sample_count() == 5
    assert service.history_dropped_samples == 0
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    restarted = TelemetryService(Database(path))
    assert await restarted.initialize_storage()
    assert restarted.storage.status == "healthy"
    assert "storage_recovery" not in [event.event_type for event in restarted.live_events]


async def test_shutdown_counts_history_it_could_not_flush(
    tmp_path, system_sample, network_sample, monkeypatch
):
    path = tmp_path / "stalled.db"
    service = await started_history_service(path, system_sample, network_sample)
    released = threading.Event()

    def stalled(*args, **kwargs):
        released.wait(10)  # Longer than the bounded shutdown drain.

    monkeypatch.setattr(service.database, "add_history_batch", stalled)
    for _ in range(4):
        await service.collect_once()

    await service.stop()
    released.set()

    # One batch was in flight; every other queued sample is reported, not silently lost.
    assert service.history_dropped_samples >= 3
    assert service._history_queue.qsize() == 0
