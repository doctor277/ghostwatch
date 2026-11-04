from __future__ import annotations

import asyncio
import sqlite3
from contextlib import nullcontext
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import psutil
import pytest

from app.main import create_app
from app.models import (
    FileBaselineEntry,
    FileScanResult,
    MonitoredFolder,
)
from app.monitoring.network import NetworkCollector
from app.monitoring.processes import ProcessCollector
from app.service import TelemetryService
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
