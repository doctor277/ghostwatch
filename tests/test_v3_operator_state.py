from __future__ import annotations

import asyncio
import sqlite3
import threading
from datetime import UTC, datetime

import pytest

from app.models import Alert, AlertUpdate
from app.service import TelemetryService
from app.storage.database import Database, InvalidRecordError

CHANGES = ["acknowledged", "dismissed"]


def alert(**changes):
    values = {
        "id": "synthetic-alert",
        "timestamp": datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
        "severity": "WARNING",
        "source": "synthetic-source",
        "metric": "synthetic-metric",
        "explanation": "synthetic",
    }
    return Alert(**(values | changes))


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


def reject(*args):
    raise InvalidRecordError("synthetic rejection")


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
    return item


def live(service, identifier="synthetic-alert"):
    return next(item for item in service.live_alerts if item.id == identifier)


async def while_held(service, write, action, operation):
    """Run an operation, apply an action during its database write, then finish it."""
    held = HeldWrite(write)
    service.database.add_alert = held
    task = asyncio.create_task(operation)
    await held.entered.wait()
    await action()
    held.released.set()
    await task
    return held


def update(service, change, identifier="synthetic-alert"):
    return lambda: service.update_alert(identifier, AlertUpdate(**{change: True}))


def replace_and_update(service, change, identifier="synthetic-alert"):
    async def action():
        await service.update_alert(identifier, AlertUpdate(**{change: True}))
        # A state patch cannot repair a rejected observation. Supply a genuinely
        # newer observation to exercise the queue's rejection identity guard.
        await service._persist_alert(alert(id=identifier))
    return action


# An old rejection


@pytest.mark.parametrize("change", CHANGES)
async def test_old_rejection_keeps_a_newer_update(service, change):
    real_add_alert = service.database.add_alert
    queue(service)

    await while_held(service, reject, replace_and_update(service, change), service._flush_pending())

    assert service.rejected_writes == 0
    assert getattr(service._pending_alerts["synthetic-alert"], change) is True
    assert live(service).persistence_pending is True

    service.database.add_alert = real_add_alert
    await service._maybe_recover_storage()
    assert not service._pending_alerts
    assert live(service).persistence_pending is False
    assert getattr(live(service), change) is True
    assert getattr(service.database.get_alert("synthetic-alert"), change) is True


async def test_newer_version_that_is_also_invalid_is_rejected_once(service):
    queue(service)

    await while_held(
        service, reject, replace_and_update(service, "acknowledged"), service._flush_pending()
    )
    await service._maybe_recover_storage()
    await service._maybe_recover_storage()

    assert service.rejected_writes == 1
    assert not service._pending_alerts
    assert live(service).persistence_pending is False
    assert live(service).acknowledged is True


async def test_old_rejection_does_not_stop_other_queued_records(service):
    real_add_alert = service.database.add_alert

    def first_is_rejected(item, retention):
        if item.id == "first":
            reject()
        return real_add_alert(item, retention)

    queue(service, "first")
    queue(service, "second")

    await while_held(
        service,
        first_is_rejected,
        replace_and_update(service, "acknowledged", "first"),
        service._flush_pending(),
    )

    assert list(service._pending_alerts) == ["first"]
    assert service.database.get_alert("second") is not None
    assert live(service, "second").persistence_pending is False
    assert service.storage.status == "healthy"


# A retry


async def test_flush_applies_a_patch_without_replacing_a_later_observation(service):
    real_add_alert = service.database.add_alert
    written = []

    def add_alert(item, retention):
        written.append((item.id, item.acknowledged))
        return real_add_alert(item, retention)

    queue(service, "first")
    queue(service, "second")

    await while_held(
        service,
        add_alert,
        update(service, "acknowledged", "second"),
        service._flush_pending(),
    )

    assert written == [("first", False), ("second", False)]
    stored = service.database.get_alert("second")
    assert stored.acknowledged is True
    assert stored.occurrence_count == 1


@pytest.mark.parametrize("change", CHANGES)
async def test_update_during_a_successful_flush_is_saved_by_the_next_pass(service, change):
    real_add_alert = service.database.add_alert
    queue(service)

    await while_held(service, real_add_alert, update(service, change), service._flush_pending())
    service.database.add_alert = real_add_alert
    await service._maybe_recover_storage()

    assert not service._pending_alerts
    assert live(service).persistence_pending is False
    assert getattr(service.database.get_alert("synthetic-alert"), change) is True


@pytest.mark.parametrize("change", CHANGES)
async def test_update_during_a_failing_flush_survives_the_outage(service, change):
    real_add_alert = service.database.add_alert
    queue(service)

    await while_held(service, outage, update(service, change), service._flush_pending())
    assert service.storage.status == "degraded"
    assert getattr(service._pending_alert_updates["synthetic-alert"], change) is True

    service.database.add_alert = real_add_alert
    await service._maybe_recover_storage()
    assert service.storage.status == "healthy"
    assert getattr(service.database.get_alert("synthetic-alert"), change) is True


# A first write


@pytest.mark.parametrize("change", CHANGES)
async def test_update_during_a_successful_first_write_survives(service, change):
    real_add_alert = service.database.add_alert
    item = alert()
    service._remember_live_alert(item)

    await while_held(
        service, real_add_alert, update(service, change), service._persist_alert(item)
    )

    assert getattr(live(service), change) is True
    assert live(service).persistence_pending is True

    service.database.add_alert = real_add_alert
    await service._maybe_recover_storage()
    assert not service._pending_alerts
    assert live(service).persistence_pending is False
    assert getattr(live(service), change) is True
    assert getattr(service.database.get_alert("synthetic-alert"), change) is True


@pytest.mark.parametrize("change", CHANGES)
async def test_update_during_a_failing_first_write_survives(service, change):
    real_add_alert = service.database.add_alert
    item = alert()
    service._remember_live_alert(item)

    await while_held(service, outage, update(service, change), service._persist_alert(item))

    assert getattr(service._pending_alerts["synthetic-alert"], change) is True

    service.database.add_alert = real_add_alert
    await service._maybe_recover_storage()
    assert service.storage.status == "healthy"
    assert not service._pending_alerts
    assert getattr(live(service), change) is True
    assert getattr(service.database.get_alert("synthetic-alert"), change) is True


async def test_write_without_an_update_stays_clean(service):
    item = alert()
    service._remember_live_alert(item)
    assert await service._persist_alert(item)
    assert not service._pending_alerts
    assert live(service).persistence_pending is False


# Grouped alerts


@pytest.mark.parametrize("change", CHANGES)
async def test_grouped_alert_update_survives_an_outage(service, monkeypatch, change):
    item = alert(rule_id="collection_failure", condition_active=True)
    service._remember_live_alert(item)
    assert await service._persist_alert(item)

    with monkeypatch.context() as patch:
        patch.setattr(service.database, "update_alert", outage)
        await service.update_alert(item.id, AlertUpdate(**{change: True}))
    await service._maybe_recover_storage()

    assert service.storage.status == "healthy"
    assert getattr(live(service), change) is True
    assert getattr(service.database.get_alert(item.id), change) is True


async def test_reopened_grouped_alert_still_clears_its_update(service):
    resolved = alert(
        rule_id="collection_failure",
        condition_active=False,
        acknowledged=True,
        dismissed=True,
    )
    service.database.add_alert(resolved, 100)
    reopened = alert(rule_id="collection_failure", condition_active=True)
    stored = service.database.add_alert(reopened, 100)
    assert stored.acknowledged is False
    assert stored.dismissed is False
