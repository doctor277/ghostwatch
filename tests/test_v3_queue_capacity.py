from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.models import Alert, AlertUpdate, EventRecord
from app.service import MAX_PENDING_WRITES, TelemetryService
from app.storage.database import Database, InvalidRecordError

PRIVATE = "synthetic-private-marker"
NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
CAPACITY = 3


def alert(identifier="synthetic-alert", **changes):
    values = {
        "id": identifier,
        "timestamp": NOW,
        "severity": "WARNING",
        "source": "synthetic-source",
        "metric": "synthetic-metric",
        "explanation": "synthetic",
    }
    return Alert(**(values | changes))


def event(identifier="synthetic-event", **changes):
    values = {"id": identifier, "timestamp": NOW, "event_type": "synthetic", "title": "synthetic"}
    return EventRecord(**(values | changes))


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


def fail_storage(service):
    service._mark_storage_failure(sqlite3.OperationalError("synthetic outage"))


def outage(*args):
    raise sqlite3.OperationalError("synthetic outage")


@pytest.fixture
async def service(tmp_path):
    item = TelemetryService(Database(tmp_path / "synthetic.db"), storage_retry_seconds=0)
    assert await item.initialize_storage()
    return item


@pytest.fixture
def small_queue(monkeypatch):
    monkeypatch.setattr("app.service.MAX_PENDING_WRITES", CAPACITY)


async def queue_alerts(service, count, prefix="alert"):
    for index in range(count):
        item = alert(f"{prefix}-{index}")
        service._remember_live_alert(item)
        await service._persist_alert(item)


async def queue_events(service, count, prefix="event"):
    for index in range(count):
        await service._persist_event(event(f"{prefix}-{index}"))


def stored_alerts(service):
    return sorted(item.id for item in service.database.list_alerts(10, True))


def stored_events(service):
    return [item.id for item in service.database.list_events(10)]


def live(service, identifier):
    return next(item for item in service.live_alerts if item.id == identifier)


# Counting


async def test_alert_evictions_are_counted(service, small_queue):
    fail_storage(service)
    await queue_alerts(service, CAPACITY + 2)

    assert list(service._pending_alerts) == ["alert-2", "alert-3", "alert-4"]
    assert service.storage.dropped_alert_writes == 2
    assert service.storage.dropped_event_writes == 0


async def test_event_evictions_are_counted(service, small_queue):
    fail_storage(service)
    await queue_events(service, CAPACITY + 2)

    assert list(service._pending_events)[-3:] == ["event-2", "event-3", "event-4"]
    assert len(service._pending_events) == CAPACITY
    assert service.storage.dropped_event_writes == 2
    assert service.storage.dropped_alert_writes == 0


async def test_mixed_writes_are_counted_per_queue(service, small_queue):
    fail_storage(service)
    await queue_alerts(service, CAPACITY + 1)
    await queue_events(service, CAPACITY + 3)

    assert len(service._pending_alerts) == CAPACITY
    assert len(service._pending_events) == CAPACITY
    assert service.storage.dropped_alert_writes == 1
    assert service.storage.dropped_event_writes == 3
    assert service.storage.pending_writes == CAPACITY * 2


async def test_real_queue_limit_is_enforced(service):
    fail_storage(service)
    for index in range(MAX_PENDING_WRITES + 1):
        await service._persist_event(event(f"event-{index}"))
    assert MAX_PENDING_WRITES == 500
    assert len(service._pending_events) == MAX_PENDING_WRITES
    assert service.storage.dropped_event_writes == 1


async def test_replacing_a_queued_record_is_not_an_eviction(service, small_queue):
    fail_storage(service)
    await queue_alerts(service, CAPACITY)
    for _ in range(10):
        await service._persist_alert(alert("alert-0"))
    assert len(service._pending_alerts) == CAPACITY
    assert service.storage.dropped_alert_writes == 0


async def test_update_to_a_queued_alert_is_not_an_eviction(service, small_queue, monkeypatch):
    fail_storage(service)
    await queue_alerts(service, CAPACITY)
    await service._maybe_recover_storage()
    fail_storage(service)
    await queue_alerts(service, CAPACITY)

    monkeypatch.setattr(service.database, "update_alert", outage)
    await service.update_alert("alert-1", AlertUpdate(acknowledged=True))

    assert service._pending_alert_updates["alert-1"].acknowledged is True
    assert service.storage.dropped_alert_writes == 0


async def test_rejections_and_evictions_are_counted_apart(service, small_queue):
    fail_storage(service)
    invalid = alert("invalid").model_copy(update={"occurrence_count": 2**70})
    await service._persist_alert(invalid)
    await queue_alerts(service, CAPACITY - 1)
    await service._maybe_recover_storage()

    assert service.rejected_writes == 1
    assert service.storage.dropped_alert_writes == 0

    fail_storage(service)
    await queue_alerts(service, CAPACITY + 1, "later")
    assert service.rejected_writes == 1
    assert service.storage.dropped_alert_writes == 1


# Health


async def test_counts_survive_every_storage_transition(service, small_queue, monkeypatch):
    fail_storage(service)
    await queue_alerts(service, CAPACITY + 1)
    await queue_events(service, CAPACITY + 2)

    def assert_counts():
        assert service.storage.dropped_alert_writes == 1
        assert service.storage.dropped_event_writes == 2

    assert_counts()
    with monkeypatch.context() as patch:
        patch.setattr(service.database, "initialize", outage)
        await service._maybe_recover_storage()
        assert service.storage.status == "degraded"
        assert_counts()
    await service._maybe_recover_storage()
    assert service.storage.status == "healthy"
    assert_counts()
    fail_storage(service)
    assert_counts()


async def test_healthy_storage_still_shows_earlier_loss(service, small_queue):
    fail_storage(service)
    await queue_alerts(service, CAPACITY + 1)
    await service._maybe_recover_storage()

    assert service.storage.status == "healthy"
    assert service.storage.pending_writes == 0
    assert service.storage.dropped_alert_writes == 1


async def test_health_is_current_as_soon_as_a_write_is_dropped(service, small_queue):
    fail_storage(service)
    await queue_alerts(service, CAPACITY + 1)
    assert service.storage.pending_writes == CAPACITY
    assert service.storage.dropped_alert_writes == 1


def test_health_route_exposes_dropped_writes(tmp_path, monkeypatch):
    monkeypatch.setattr("app.service.MAX_PENDING_WRITES", 1)
    app = create_app(tmp_path / "synthetic.db", start_background=False)
    with TestClient(app) as client:
        assert client.get("/api/health").json()["storage"]["dropped_alert_writes"] == 0
        service = app.state.telemetry
        fail_storage(service)

        async def overflow():
            await queue_alerts(service, 2)
            await queue_events(service, 3)

        asyncio.run(overflow())
        storage = client.get("/api/health").json()["storage"]
        assert storage["dropped_alert_writes"] == 1
        assert storage["dropped_event_writes"] >= 2


async def test_snapshot_exposes_dropped_writes(
    tmp_path, system_sample, network_sample, small_queue
):
    class Static:
        def __init__(self, value):
            self.value = value

        def collect(self, *args, **kwargs):
            return self.value

    class Detector:
        def evaluate(self, *args, **kwargs):
            return []

    service = TelemetryService(Database(tmp_path / "synthetic.db"), storage_retry_seconds=0)
    assert await service.initialize_storage()
    service.system_collector = Static(system_sample)
    service.network_collector = Static((network_sample, []))
    service.process_collector = Static([])
    service.detection_engine = Detector()
    await service.collect_once()

    fail_storage(service)
    await queue_alerts(service, CAPACITY + 1)
    snapshot = await service.collect_once()

    assert snapshot.storage.dropped_alert_writes == 1


async def test_counts_reset_on_restart(service, small_queue, tmp_path):
    fail_storage(service)
    await queue_alerts(service, CAPACITY + 1)
    restarted = TelemetryService(Database(tmp_path / "synthetic.db"))
    assert await restarted.initialize_storage()
    assert restarted.storage.dropped_alert_writes == 0
    assert restarted.storage.dropped_event_writes == 0


# Recovery


async def test_recovery_writes_the_retained_records_only(service, small_queue):
    fail_storage(service)
    await queue_alerts(service, CAPACITY + 2)
    await queue_events(service, CAPACITY + 1, "queued")
    await service._maybe_recover_storage()

    assert service.storage.status == "healthy"
    assert not service._pending_alerts and not service._pending_events
    assert stored_alerts(service) == ["alert-2", "alert-3", "alert-4"]
    assert "queued-0" not in stored_events(service)
    assert "queued-3" in stored_events(service)
    assert service.storage.dropped_alert_writes == 2


async def test_recovery_notice_does_not_cost_a_queued_event(service, small_queue):
    fail_storage(service)
    await queue_events(service, CAPACITY)
    queued = list(service._pending_events)
    await service._maybe_recover_storage()

    assert service.storage.dropped_event_writes == 0
    assert set(queued) <= set(stored_events(service))
    assert service.live_events[0].event_type == "storage_recovery"


async def test_recovery_notice_is_stored_when_the_queue_has_room(service, small_queue):
    fail_storage(service)
    await queue_events(service, CAPACITY - 1)
    await service._maybe_recover_storage()
    await service._maybe_recover_storage()

    stored = service.database.list_events(10)
    assert "storage_recovery" in [item.event_type for item in stored]
    assert service.storage.dropped_event_writes == 0


async def test_evicted_alert_no_longer_claims_to_be_pending(service, small_queue):
    fail_storage(service)
    await queue_alerts(service, CAPACITY + 1)

    assert live(service, "alert-0").persistence_pending is False
    assert live(service, "alert-1").persistence_pending is True


async def test_alert_queued_again_after_eviction_is_a_new_write(service, small_queue):
    fail_storage(service)
    await queue_alerts(service, CAPACITY + 1)
    await service._persist_alert(alert("alert-0"))

    assert "alert-0" in service._pending_alerts
    assert live(service, "alert-0").persistence_pending is True
    assert service.storage.dropped_alert_writes == 2


async def test_alert_write_in_progress_is_not_evicted(service, small_queue):
    real_add_alert = service.database.add_alert
    fail_storage(service)
    await queue_alerts(service, CAPACITY)
    service.storage = service.storage.model_copy(update={"persistence_available": True})

    held = HeldWrite(real_add_alert)
    service.database.add_alert = held
    flush = asyncio.create_task(service._flush_pending())
    await held.entered.wait()
    fail_storage(service)
    await service._persist_alert(alert("newest"))
    assert "alert-0" in service._pending_alerts
    assert "alert-1" not in service._pending_alerts
    held.released.set()
    await flush

    assert service.storage.dropped_alert_writes == 1
    assert "alert-0" in stored_alerts(service)
    assert "alert-1" not in stored_alerts(service)


async def test_event_write_in_progress_is_not_evicted(service, small_queue, monkeypatch):
    real_add_event = service.database.add_event
    fail_storage(service)
    await queue_events(service, CAPACITY)
    service.storage = service.storage.model_copy(update={"persistence_available": True})
    first = next(iter(service._pending_events))

    held = HeldWrite(real_add_event)
    monkeypatch.setattr(service.database, "add_event", held)
    flush = asyncio.create_task(service._flush_pending())
    await held.entered.wait()
    fail_storage(service)
    await service._persist_event(event("newest"))
    assert first in service._pending_events
    held.released.set()
    await flush

    assert service.storage.dropped_event_writes == 1
    assert first in stored_events(service)


async def test_write_slot_is_released_after_a_failure(service, small_queue):
    def reject(*args):
        raise InvalidRecordError("synthetic rejection")

    fail_storage(service)
    await queue_alerts(service, CAPACITY)
    service.storage = service.storage.model_copy(update={"persistence_available": True})
    service.database.add_alert = reject
    await service._flush_pending()

    fail_storage(service)
    await queue_alerts(service, CAPACITY + 1, "later")
    assert service.storage.dropped_alert_writes == 1
    assert "later-0" not in service._pending_alerts


async def test_alert_write_slot_is_released_after_an_outage(service, small_queue):
    fail_storage(service)
    await queue_alerts(service, CAPACITY)
    service.storage = service.storage.model_copy(update={"persistence_available": True})
    service.database.add_alert = outage
    await service._flush_pending()

    await service._persist_alert(alert("newest"))
    assert "alert-0" not in service._pending_alerts
    assert "alert-1" in service._pending_alerts


async def test_event_write_slot_is_released_after_an_outage(service, small_queue, monkeypatch):
    fail_storage(service)
    await queue_events(service, CAPACITY)
    first, second = list(service._pending_events)[:2]
    service.storage = service.storage.model_copy(update={"persistence_available": True})
    monkeypatch.setattr(service.database, "add_event", outage)
    await service._flush_pending()

    await service._persist_event(event("newest"))
    assert first not in service._pending_events
    assert second in service._pending_events


# Opt-out


async def test_opt_out_discard_is_not_counted_as_loss(service, small_queue):
    fail_storage(service)
    await queue_alerts(service, CAPACITY)
    await queue_events(service, CAPACITY)
    service._discard_pending_writes()

    assert service.storage.pending_writes == 0
    assert service.storage.dropped_alert_writes == 0
    assert service.storage.dropped_event_writes == 0


async def test_opted_out_writes_never_reach_the_queue_or_the_counts(service, small_queue):
    await service.update_settings(
        service.settings.model_copy(update={"event_logging_enabled": False})
    )
    fail_storage(service)
    for index in range(CAPACITY + 2):
        item = alert(f"alert-{index}")
        service._remember_live_alert(item)
        service._queue_with_operator_state(item)

    assert not service._pending_alerts
    assert service.storage.dropped_alert_writes == 0


async def test_counts_stay_after_an_opt_out(service, small_queue):
    fail_storage(service)
    await queue_alerts(service, CAPACITY + 1)
    service._discard_pending_writes()
    assert service.storage.dropped_alert_writes == 1


# Privacy


async def test_drop_reporting_holds_no_record_content(service, small_queue, caplog):
    fail_storage(service)
    with caplog.at_level(logging.DEBUG):
        for index in range(CAPACITY + 1):
            await service._persist_alert(
                alert(f"{PRIVATE}-{index}", explanation=PRIVATE, evidence={"path": PRIVATE})
            )
        for index in range(CAPACITY + 1):
            await service._persist_event(event(f"{PRIVATE}-{index}", summary=PRIVATE))

    assert service.storage.dropped_alert_writes == 1
    assert service.storage.dropped_event_writes >= 1
    assert PRIVATE not in caplog.text
    assert PRIVATE not in service.storage.model_dump_json()


async def test_drop_logging_is_bounded(service, small_queue, caplog):
    fail_storage(service)
    with caplog.at_level(logging.DEBUG, logger="app.service"):
        caplog.clear()
        await queue_alerts(service, CAPACITY + 1000)
    drops = [record for record in caplog.records if "queue is full" in record.getMessage()]
    assert 1 <= len(drops) <= 12
    assert "1000" in drops[-1].getMessage()
