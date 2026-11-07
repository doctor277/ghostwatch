from __future__ import annotations

import asyncio
import sqlite3
from contextlib import nullcontext
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import psutil
import pytest

from app.main import create_app
from app.models import (
    Alert,
    AlertUpdate,
    DetectionSettings,
    FileBaselineEntry,
    FileScanResult,
    HistorySettings,
    MonitoredFolder,
    Settings,
)
from app.monitoring.network import NetworkCollector
from app.monitoring.processes import ProcessCollector
from app.service import StorageUnavailableError, TelemetryService
from app.storage.database import Database

WORKERS = ("_task", "_history_task", "_windows_task", "_file_integrity_task")


async def idle_worker():
    await asyncio.Event().wait()


def synthetic_service(tmp_path, monkeypatch):
    service = TelemetryService(Database(tmp_path / "synthetic.db"))
    service.storage.persistence_available = True
    for name in ("_run", "_history_writer", "_windows_event_loop", "_file_integrity_loop"):
        monkeypatch.setattr(service, name, idle_worker)
    monkeypatch.setattr(service, "collect_once", AsyncMock())
    return service


async def cleanup_tasks(tasks):
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize("cancel", [False, True])
async def test_startup_failure_cleans_created_workers(tmp_path, monkeypatch, cancel):
    service = synthetic_service(tmp_path, monkeypatch)
    created = []

    async def fail():
        created.extend(getattr(service, name) for name in WORKERS[1:])
        raise asyncio.CancelledError() if cancel else RuntimeError("synthetic startup")

    monkeypatch.setattr(service, "collect_once", fail)
    try:
        with pytest.raises(asyncio.CancelledError if cancel else RuntimeError):
            await service.start()
        assert all(task.done() for task in created)
        assert all(getattr(service, name) is None for name in WORKERS)
    finally:
        await cleanup_tasks(created)


async def test_repeated_start_keeps_one_worker_set(tmp_path, monkeypatch):
    service = synthetic_service(tmp_path, monkeypatch)
    created = []
    try:
        await service.start()
        original = [getattr(service, name) for name in WORKERS]
        created.extend(original)
        await service.start()
        created.extend(getattr(service, name) for name in WORKERS)
        assert [getattr(service, name) for name in WORKERS] == original
        await service.stop()
        await service.stop()
        assert all(task.done() for task in original)
    finally:
        await cleanup_tasks(created)


async def test_failed_worker_does_not_prevent_other_cleanup(tmp_path, monkeypatch):
    service = synthetic_service(tmp_path, monkeypatch)

    async def fail():
        raise RuntimeError("synthetic worker")

    monkeypatch.setattr(service, "_run", fail)
    await service.start()
    created = [getattr(service, name) for name in WORKERS]
    await asyncio.sleep(0)
    try:
        await service.stop()
        assert all(task.done() for task in created)
        assert all(getattr(service, name) is None for name in WORKERS)
    finally:
        await cleanup_tasks(created)


@pytest.mark.parametrize("cancel", [False, True])
async def test_exceptional_lifespan_stops_service(tmp_path, monkeypatch, cancel):
    stop = AsyncMock()
    monkeypatch.setattr(TelemetryService, "initialize_storage", AsyncMock())
    monkeypatch.setattr(TelemetryService, "start", AsyncMock())
    monkeypatch.setattr(TelemetryService, "stop", stop)
    app = create_app(tmp_path / "unused.db")
    with pytest.raises(asyncio.CancelledError if cancel else RuntimeError):
        async with app.router.lifespan_context(app):
            raise asyncio.CancelledError() if cancel else RuntimeError("synthetic lifespan")
    stop.assert_awaited_once()


@pytest.mark.parametrize("operation", ["load_file_baseline", "save_file_scan"])
async def test_file_scan_storage_failure_stays_degraded(tmp_path, monkeypatch, operation):
    service = TelemetryService(Database(tmp_path / "synthetic.db"))
    assert await service.initialize_storage()
    watched = tmp_path / "watched"
    watched.mkdir()
    await service.add_monitored_folder(str(watched))
    last_success = service.storage.last_success

    def fail(*args):
        raise sqlite3.OperationalError("synthetic private path must not be exposed")

    monkeypatch.setattr(service.database, operation, fail)
    assert await service.scan_file_integrity_once() == []
    assert service.storage.status == "degraded"
    assert not service.storage.persistence_available
    assert service.storage.last_success == last_success
    assert service.storage.last_error_type == "OperationalError"
    assert "private path" not in service.storage.model_dump_json()


async def test_collector_failure_is_isolated_from_storage_and_other_folders(
    tmp_path, monkeypatch
):
    service = TelemetryService(Database(tmp_path / "synthetic.db"))
    assert await service.initialize_storage()
    for name in ("first", "second"):
        watched = tmp_path / name
        watched.mkdir()
        await service.add_monitored_folder(str(watched))
    real_scan = service.file_integrity_collector.scan
    scanned = []

    def scan(folder, *args, **kwargs):
        scanned.append(folder.id)
        if len(scanned) == 1:
            raise RuntimeError("synthetic collector")
        return real_scan(folder, *args, **kwargs)

    monkeypatch.setattr(service.file_integrity_collector, "scan", scan)
    assert await service.scan_file_integrity_once() == []
    assert len(scanned) == 2
    assert service.storage.status == "healthy"
    assert service.storage.persistence_available
    folders = {
        folder.id: folder for folder in await asyncio.to_thread(
            service.database.list_monitored_folders
        )
    }
    assert folders[scanned[0]].status == "unavailable"
    assert folders[scanned[0]].status_detail == "Folder scan failed (RuntimeError)"
    assert folders[scanned[1]].status == "healthy"


async def test_surrogate_file_name_stays_monitored_without_degrading_storage(tmp_path):
    service = TelemetryService(Database(tmp_path / "db" / "synthetic.db"))
    assert await service.initialize_storage()
    watched = tmp_path / "watched"
    watched.mkdir()
    await service.add_monitored_folder(str(watched))
    target = watched / "evasive\ud800.txt"
    try:
        target.write_text("one")
    except (OSError, UnicodeError):
        pytest.skip("Filesystem does not accept lone surrogates")

    events = await service.scan_file_integrity_once()
    assert [(item.change_type, item.relative_path) for item in events] == [
        ("created", "evasive\\ud800.txt")
    ]
    target.write_text("changed")
    events = await service.scan_file_integrity_once()
    assert [item.change_type for item in events] == ["modified"]
    target.unlink()
    events = await service.scan_file_integrity_once()
    assert [item.change_type for item in events] == ["deleted"]
    assert service.storage.status == "healthy"


async def test_surrogate_file_present_when_folder_is_added(tmp_path):
    service = TelemetryService(Database(tmp_path / "db" / "synthetic.db"))
    assert await service.initialize_storage()
    watched = tmp_path / "watched"
    watched.mkdir()
    try:
        (watched / "evasive\ud800.txt").write_text("one")
    except (OSError, UnicodeError):
        pytest.skip("Filesystem does not accept lone surrogates")

    folder = await service.add_monitored_folder(str(watched))

    assert list(service.database.load_file_baseline(folder.id)) == ["evasive\\ud800.txt"]
    assert service.storage.status == "healthy"


def synthetic_alert():
    return Alert(
        id="synthetic-alert",
        timestamp=datetime.now(UTC),
        severity="WARNING",
        source="synthetic",
        metric="synthetic",
        explanation="synthetic",
    )


async def test_direct_write_recovery_still_flushes_pending_writes(tmp_path, monkeypatch):
    service = TelemetryService(Database(tmp_path / "synthetic.db"), storage_retry_seconds=60)
    assert await service.initialize_storage()
    alert = synthetic_alert()
    real_add_alert = service.database.add_alert

    def fail(*args):
        raise sqlite3.OperationalError("synthetic")

    monkeypatch.setattr(service.database, "add_alert", fail)
    assert not await service._persist_alert(alert)
    monkeypatch.setattr(service.database, "add_alert", real_add_alert)

    # A settings write succeeds and restores health without the recovery path.
    await service.update_detection_settings(DetectionSettings())
    assert service.storage.persistence_available
    await service._maybe_recover_storage()

    assert not service._pending_alerts
    assert not service._pending_events
    assert [stored.id for stored in service.database.list_alerts(10, True)] == [alert.id]
    assert not next(
        item for item in service.live_alerts if item.id == alert.id
    ).persistence_pending


async def failed_startup_service(database, monkeypatch):
    service = TelemetryService(database, storage_retry_seconds=0)

    def fail():
        raise sqlite3.OperationalError("synthetic")

    with monkeypatch.context() as patch:
        patch.setattr(database, "initialize", fail)
        assert not await service.initialize_storage()
    return service


async def test_recovery_reports_storage_available_only_after_settings_load(
    tmp_path, monkeypatch
):
    database = Database(tmp_path / "synthetic.db")
    database.initialize()
    service = await failed_startup_service(database, monkeypatch)
    real_load = database.load_history_settings
    available_during_load = []

    def spy():
        available_during_load.append(service.storage.persistence_available)
        return real_load()

    monkeypatch.setattr(database, "load_history_settings", spy)
    await service._maybe_recover_storage()
    assert available_during_load == [False]
    assert service.storage.persistence_available


async def test_history_is_not_written_until_saved_settings_are_loaded(tmp_path, monkeypatch):
    database = Database(tmp_path / "synthetic.db")
    database.initialize()
    service = await failed_startup_service(database, monkeypatch)
    # A direct write restores health before the next collection reconciles.
    await service.update_detection_settings(DetectionSettings())
    assert service.storage.persistence_available
    write = Mock()
    monkeypatch.setattr(database, "add_history_batch", write)
    writer = asyncio.create_task(service._history_writer())
    try:
        service._history_queue.put_nowait(
            SimpleNamespace(sample=None, processes=[], connection_summary=None)
        )
        await asyncio.wait_for(service._history_queue.join(), timeout=2.0)
    finally:
        await cleanup_tasks([writer])
    write.assert_not_called()
    assert service.history_dropped_samples == 1


async def test_saved_logging_opt_out_discards_writes_queued_under_defaults(
    tmp_path, monkeypatch
):
    database = Database(tmp_path / "synthetic.db")
    database.initialize()
    database.save_settings(Settings(event_logging_enabled=False))
    service = await failed_startup_service(database, monkeypatch)
    alert = synthetic_alert()
    service._remember_live_alert(alert)
    assert not await service._persist_alert(alert)

    await service._maybe_recover_storage()

    assert not service.settings.event_logging_enabled
    assert database.list_alerts(10, True) == []
    assert not service._pending_alerts
    assert not service.live_alerts[0].persistence_pending


@pytest.mark.parametrize(
    "update, maintenance, applied",
    [
        (
            lambda service: service.update_history_settings(HistorySettings(enabled=False)),
            "prune_telemetry",
            lambda service: not service.history_settings.enabled,
        ),
        (
            lambda service: service.update_settings(Settings(event_logging_enabled=False)),
            "enforce_retention",
            lambda service: not service.settings.event_logging_enabled,
        ),
    ],
)
async def test_saved_opt_out_applies_when_follow_up_maintenance_fails(
    tmp_path, monkeypatch, update, maintenance, applied
):
    service = TelemetryService(Database(tmp_path / "synthetic.db"))
    assert await service.initialize_storage()

    def fail(*args):
        raise sqlite3.OperationalError("synthetic")

    monkeypatch.setattr(service.database, maintenance, fail)
    with pytest.raises(StorageUnavailableError):
        await update(service)
    assert applied(service)


async def test_alert_update_during_flush_is_not_overwritten(tmp_path, monkeypatch):
    service = TelemetryService(Database(tmp_path / "synthetic.db"), storage_retry_seconds=60)
    assert await service.initialize_storage()
    database = service.database
    alert = synthetic_alert()
    service._remember_live_alert(alert)
    real_add_alert = database.add_alert

    def fail(*args):
        raise sqlite3.OperationalError("synthetic")

    monkeypatch.setattr(database, "add_alert", fail)
    assert not await service._persist_alert(alert)
    await service.update_detection_settings(DetectionSettings())
    loop = asyncio.get_running_loop()

    def add_then_acknowledge(*args):
        monkeypatch.setattr(database, "add_alert", real_add_alert)
        stored = real_add_alert(*args)
        asyncio.run_coroutine_threadsafe(
            service.update_alert(alert.id, AlertUpdate(acknowledged=True)), loop
        ).result(timeout=5.0)
        return stored

    monkeypatch.setattr(database, "add_alert", add_then_acknowledge)
    await service._maybe_recover_storage()
    await service._maybe_recover_storage()

    assert not service._pending_alerts
    assert next(item for item in service.live_alerts if item.id == alert.id).acknowledged
    assert database.list_alerts(10, True)[0].acknowledged


@pytest.mark.parametrize("direct_write_first", [False, True])
async def test_recovery_loads_settings_missed_at_startup(
    tmp_path, monkeypatch, direct_write_first
):
    database = Database(tmp_path / "synthetic.db")
    database.initialize()
    database.save_history_settings(HistorySettings(enabled=False))
    service = TelemetryService(database, storage_retry_seconds=0)
    real_initialize = database.initialize

    def fail():
        raise sqlite3.OperationalError("synthetic")

    monkeypatch.setattr(database, "initialize", fail)
    assert not await service.initialize_storage()
    assert service.history_settings.enabled
    monkeypatch.setattr(database, "initialize", real_initialize)

    if direct_write_first:
        await service.update_detection_settings(DetectionSettings())
    await service._maybe_recover_storage()
    assert service.storage.persistence_available
    assert not service.history_settings.enabled


async def test_recovery_after_runtime_failure_keeps_live_baseline(tmp_path, monkeypatch):
    service = TelemetryService(Database(tmp_path / "synthetic.db"), storage_retry_seconds=0)
    assert await service.initialize_storage()
    live_baseline = {"cpu_percent": [12.5]}
    service.baseline_engine.restore_state(live_baseline)
    service._mark_storage_failure(sqlite3.OperationalError("synthetic"))

    await service._maybe_recover_storage()

    assert service.storage.persistence_available
    assert service.baseline_engine.export_state()["cpu_percent"] == [12.5]


def surrogate_process():
    return SimpleNamespace(
        pid=7,
        info={"pid": 7, "name": "evasive\ud800.exe"},
        name=lambda: "evasive\ud800.exe",
        oneshot=lambda: nullcontext(),
        cpu_percent=lambda interval=None: 1.0,
        memory_percent=lambda: 1.0,
        memory_info=lambda: SimpleNamespace(rss=1),
        status=lambda: "running",
        ppid=lambda: 1,
        exe=lambda: "C:\\synthetic\\evasive\ud800.exe",
        username=lambda: "synthetic\ud800",
        create_time=lambda: 1.0,
    )


def test_surrogate_process_identity_stays_serializable():
    module = SimpleNamespace(
        process_iter=lambda *args, **kwargs: [surrogate_process()],
        cpu_count=lambda: 1,
        NoSuchProcess=psutil.NoSuchProcess,
        AccessDenied=psutil.AccessDenied,
    )
    processes = ProcessCollector(module, identity_budget=1).collect([])
    assert [item.name for item in processes] == ["evasive\\ud800.exe"]
    assert processes[0].executable == "C:\\synthetic\\evasive\\ud800.exe"
    assert processes[0].username == "synthetic\\ud800"
    assert "evasive" in processes[0].model_dump_json()


def test_surrogate_connection_process_name_stays_serializable():
    module = SimpleNamespace(Process=lambda pid: surrogate_process())
    collector = NetworkCollector(module)
    assert collector._process_name(7, 0.0) == "evasive\\ud800.exe"


def test_database_calls_close_their_connections(tmp_path, monkeypatch):
    database = Database(tmp_path / "synthetic.db")
    database.initialize()
    opened = []
    real_connect = sqlite3.connect

    def tracking_connect(*args, **kwargs):
        opened.append(real_connect(*args, **kwargs))
        return opened[-1]

    monkeypatch.setattr(sqlite3, "connect", tracking_connect)
    database.schema_version()
    database.backup(tmp_path / "backup.db")

    assert len(opened) == 3
    for connection in opened:
        with pytest.raises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")


def test_explicit_transaction_rolls_back_and_closes_on_error(tmp_path, monkeypatch):
    database = Database(tmp_path / "synthetic.db")
    database.initialize()
    folder = MonitoredFolder(
        id="synthetic-folder", path=str(tmp_path), created_at=datetime.now(UTC)
    )
    database.add_monitored_folder(
        folder,
        [FileBaselineEntry(relative_path="kept.txt", sha256="0" * 64, size=1, modified_ns=1)],
    )
    opened = []
    real_connect = sqlite3.connect

    def tracking_connect(*args, **kwargs):
        opened.append(real_connect(*args, **kwargs))
        return opened[-1]

    def fail(*args):
        raise sqlite3.OperationalError("synthetic")

    monkeypatch.setattr(sqlite3, "connect", tracking_connect)
    monkeypatch.setattr(database, "_insert_file_baselines", fail)
    with pytest.raises(sqlite3.OperationalError):
        database.save_file_scan(folder.id, FileScanResult(status="healthy"), 100)

    with pytest.raises(sqlite3.ProgrammingError):
        opened[0].execute("SELECT 1")
    assert list(database.load_file_baseline(folder.id)) == ["kept.txt"]


def test_database_connection_scope_rolls_back_and_closes_on_error(tmp_path):
    database = Database(tmp_path / "synthetic.db")
    database.initialize()
    with pytest.raises(RuntimeError):
        with database._connect() as connection:
            connection.execute(
                "INSERT INTO configuration (key, payload) VALUES ('synthetic', '{}')"
            )
            raise RuntimeError("synthetic")
    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("SELECT 1")
    with database._connect() as connection:
        row = connection.execute(
            "SELECT 1 FROM configuration WHERE key = 'synthetic'"
        ).fetchone()
    assert row is None
