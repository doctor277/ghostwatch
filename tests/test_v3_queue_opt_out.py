from __future__ import annotations

import asyncio
import sqlite3
import threading
from datetime import UTC, datetime

import pytest

from app.models import Alert, AlertUpdate, EventRecord
from app.service import StorageUnavailableError, TelemetryService
from app.storage.database import Database

NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def alert(**changes):
    values = {
        "id": "synthetic-alert",
        "timestamp": NOW,
        "severity": "WARNING",
        "source": "synthetic-source",
        "metric": "synthetic-metric",
        "explanation": "synthetic",
    }
    return Alert(**(values | changes))


def event(identifier):
    return EventRecord(id=identifier, timestamp=NOW, event_type="synthetic", title="synthetic")


class HeldWrite:
    """Keep a database write open until the test has made its change."""

    def __init__(self, write):
        self.write = write
        self.loop = asyncio.get_running_loop()
        self.entered = asyncio.Event()
        self.released = threading.Event()

    def __call__(self, *args):
        self.loop.call_soon_threadsafe(self.entered.set)
        assert self.released.wait(10)
        return self.write(*args)


def outage(*args):
    raise sqlite3.OperationalError("synthetic outage")


@pytest.fixture
async def service(tmp_path):
    item = TelemetryService(Database(tmp_path / "synthetic.db"), storage_retry_seconds=0)
    assert await item.initialize_storage()
    return item


def queue(service, identifier="synthetic-alert"):
    item = alert(id=identifier).model_copy(update={"persistence_pending": True})
    service._remember_live_alert(item)
    service._pending_alerts[identifier] = item


def opted_out(service):
    return service.settings.model_copy(update={"event_logging_enabled": False})


def stored_events(service):
    return [item.id for item in service.database.list_events(10)]


async def test_saved_opt_out_discards_queued_writes(service):
    queue(service)
    service._pending_events["queued"] = event("queued")

    await service.update_settings(opted_out(service))
    await service._maybe_recover_storage()

    assert not service._pending_alerts and not service._pending_events
    assert service.database.list_alerts(10, True) == []
    assert "queued" not in stored_events(service)
    assert service.live_alerts[0].persistence_pending is False


async def test_opt_out_applies_when_follow_up_maintenance_fails(service, monkeypatch):
    queue(service)
    monkeypatch.setattr(service.database, "enforce_retention", outage)

    with pytest.raises(StorageUnavailableError):
        await service.update_settings(opted_out(service))
    monkeypatch.undo()
    await service._maybe_recover_storage()

    assert not service._pending_alerts
    assert service.database.list_alerts(10, True) == []


async def test_unsaved_opt_out_keeps_queued_writes(service, monkeypatch):
    queue(service)
    monkeypatch.setattr(service.database, "save_settings", outage)

    with pytest.raises(StorageUnavailableError):
        await service.update_settings(opted_out(service))

    assert list(service._pending_alerts) == ["synthetic-alert"]
    assert service.settings.event_logging_enabled is True


async def test_opt_out_during_a_flush_stops_the_remaining_writes(service):
    real_add_alert = service.database.add_alert
    queue(service, "first")
    queue(service, "second")
    service._pending_events["queued"] = event("queued")

    held = HeldWrite(real_add_alert)
    service.database.add_alert = held
    task = asyncio.create_task(service._flush_pending())
    await held.entered.wait()
    await service.update_settings(opted_out(service))
    held.released.set()
    await task

    assert service.database.get_alert("second") is None
    assert "queued" not in stored_events(service)
    assert not service._pending_alerts and not service._pending_events


async def test_opt_out_during_a_flush_stops_the_remaining_events(service, monkeypatch):
    real_add_event = service.database.add_event
    service._pending_events["first"] = event("first")
    service._pending_events["second"] = event("second")

    held = HeldWrite(real_add_event)
    monkeypatch.setattr(service.database, "add_event", held)
    task = asyncio.create_task(service._flush_pending())
    await held.entered.wait()
    await service.update_settings(opted_out(service))
    held.released.set()
    await task

    assert "second" not in stored_events(service)
    assert not service._pending_events


@pytest.mark.parametrize("failing", [False, True], ids=["successful", "failing"])
async def test_update_during_a_write_is_not_queued_after_an_opt_out(service, failing):
    write = outage if failing else service.database.add_alert
    item = alert()
    service._remember_live_alert(item)

    held = HeldWrite(write)
    service.database.add_alert = held
    task = asyncio.create_task(service._persist_alert(item))
    await held.entered.wait()
    await service.update_alert(item.id, AlertUpdate(acknowledged=True))
    await service.update_settings(opted_out(service))
    held.released.set()
    await task

    assert not service._pending_alerts
    remembered = next(entry for entry in service.live_alerts if entry.id == item.id)
    assert remembered.persistence_pending is False
    assert remembered.acknowledged is True


async def test_failed_update_is_not_queued_after_an_opt_out(service, monkeypatch):
    item = alert()
    assert await service._persist_alert(item)
    await service.update_settings(opted_out(service))
    service._remember_live_alert(item)
    monkeypatch.setattr(service.database, "update_alert", outage)

    updated = await service.update_alert(item.id, AlertUpdate(acknowledged=True))

    assert not service._pending_alerts
    assert updated.acknowledged is True
    assert updated.persistence_pending is False


async def test_recovery_event_is_not_stored_after_an_opt_out(service):
    await service.update_settings(opted_out(service))
    before = stored_events(service)
    service._mark_storage_failure(sqlite3.OperationalError("synthetic outage"))
    await service._maybe_recover_storage()
    await service._maybe_recover_storage()
    assert service.storage.status == "healthy"
    assert not service._pending_events
    assert stored_events(service) == before
    assert service.live_events[0].event_type == "storage_recovery"


async def test_logging_resumes_after_it_is_enabled_again(service):
    await service.update_settings(opted_out(service))
    enabled = service.settings.model_copy(update={"event_logging_enabled": True})
    await service.update_settings(enabled)
    assert await service._persist_alert(alert())
    assert service.database.get_alert("synthetic-alert") is not None
