from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

import pytest

from app.models import FileIntegrityEvent, WindowsEventRecord
from app.service import TelemetryService
from app.storage.database import Database

NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
PRIVATE = "synthetic-private-marker"


def windows_event(**changes):
    values = {
        "id": "windows:System:1",
        "channel": "System",
        "record_id": 1,
        "timestamp": NOW,
        "provider": "synthetic",
        "event_id": 7031,
        "level": "ERROR",
        "category": "service_failure",
    }
    return WindowsEventRecord(**(values | changes))


def file_event(**changes):
    values = {
        "id": "synthetic-event",
        "folder_id": "synthetic-folder",
        "timestamp": NOW,
        "change_type": "modified",
        "relative_path": "synthetic.txt",
    }
    return FileIntegrityEvent(**(values | changes))


@pytest.fixture
async def service(tmp_path):
    item = TelemetryService(Database(tmp_path / "synthetic.db"), storage_retry_seconds=0)
    assert await item.initialize_storage()
    return item


def assert_stored(service):
    assert service.storage.status == "healthy"
    assert service.rejected_writes == 0
    stored = service.database.list_alerts(10, True)
    assert len(stored) == 1
    assert any(item.related_id == stored[0].id for item in service.live_events)


@pytest.mark.parametrize(
    "changes",
    [
        pytest.param({"summary": "x" * 4000}, id="longest-summary"),
        pytest.param({"service_name": "x" * 300}, id="longest-service"),
        pytest.param({"provider": "x" * 5000}, id="long-provider"),
        pytest.param({"provider": "\ud800 synthetic"}, id="unencodable-provider"),
        pytest.param({"category": "defender_detection"}, id="defender"),
        pytest.param({"correlation_tags": ["x" * 5000] * 10}, id="long-tags"),
    ],
)
async def test_windows_event_at_its_limits_reaches_storage(service, changes):
    await service._correlate_windows_event(windows_event(**changes))
    assert_stored(service)


@pytest.mark.parametrize(
    "changes",
    [
        pytest.param({"relative_path": "\ud800" * 5000}, id="long-unencodable-path"),
        pytest.param({"relative_path": "x" * 100_000}, id="long-path"),
        pytest.param({"size": 2**70}, id="large-size"),
        pytest.param({"change_type": "created"}, id="created"),
        pytest.param({"change_type": "deleted"}, id="deleted"),
    ],
)
async def test_file_event_at_its_limits_reaches_storage(service, changes):
    await service._correlate_file_integrity_event(file_event(**changes))
    assert_stored(service)


@pytest.mark.parametrize(
    "loop,collect,settings",
    [
        ("_windows_event_loop", "collect_windows_events_once", "windows_event_settings"),
        ("_file_integrity_loop", "scan_file_integrity_once", "file_integrity_settings"),
    ],
)
async def test_worker_survives_a_failed_pass(service, monkeypatch, loop, collect, settings):
    passes = []

    async def failing_pass():
        passes.append(1)
        raise RuntimeError("synthetic failure")

    async def stop_after_two(_):
        if len(passes) >= 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(service, collect, failing_pass)
    monkeypatch.setattr(asyncio, "sleep", stop_after_two)
    enabled = getattr(service, settings).model_copy(update={"enabled": True})
    monkeypatch.setattr(service, settings, enabled)

    with pytest.raises(asyncio.CancelledError):
        await getattr(service, loop)()

    assert len(passes) == 2


async def test_correlation_failures_do_not_write_record_content(service, caplog):
    with caplog.at_level(logging.DEBUG):
        await service._correlate_file_integrity_event(file_event(relative_path=PRIVATE))
        await service._correlate_windows_event(windows_event(provider=PRIVATE))
    assert PRIVATE not in caplog.text
