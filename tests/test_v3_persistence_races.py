from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
from contextlib import closing
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.models import Alert, AlertUpdate, EventRecord, IncidentCreate
from app.service import MAX_PENDING_WRITES, TelemetryService
from app.storage.database import Database, InvalidRecordError

PRIVATE = "synthetic-private-marker"
NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
CHANGES = ["acknowledged", "dismissed"]
OUTCOMES = {"successful": False, "failing": True}


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


def grouped(**changes):
    return alert(**({"rule_id": "collection_failure", "condition_active": True} | changes))


def event(identifier="synthetic-event"):
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


class Recorder:
    def __init__(self, write):
        self.write = write
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        return self.write(*args)


def outage(*args):
    raise sqlite3.OperationalError(PRIVATE)


@pytest.fixture
async def service(tmp_path):
    item = TelemetryService(Database(tmp_path / "synthetic.db"), storage_retry_seconds=0)
    assert await item.initialize_storage()
    return item


def queue(service, identifier="synthetic-alert"):
    item = alert(identifier).model_copy(update={"persistence_pending": True})
    service._remember_live_alert(item)
    service._pending_alerts[identifier] = item
    return item


def live(service, identifier="synthetic-alert"):
    return next(item for item in service.live_alerts if item.id == identifier)


def stored(service, identifier="synthetic-alert"):
    return service.database.get_alert(identifier)


def stored_events(service):
    return [item.id for item in service.database.list_events(10)]


def fail_storage(service):
    service._mark_storage_failure(sqlite3.OperationalError(PRIVATE))


def with_logging(service, enabled):
    return service.settings.model_copy(update={"event_logging_enabled": enabled})


async def opt_out(service):
    await service.update_settings(with_logging(service, False))


async def opt_in(service):
    await service.update_settings(with_logging(service, True))


async def link_incident(service, *identifiers):
    return await service.create_incident(
        IncidentCreate(title="synthetic", severity="WARNING", alert_ids=list(identifiers))
    )


async def settle(service, passes=3):
    for _ in range(passes):
        await service._maybe_recover_storage()


async def write_grouped(service, **changes):
    item = grouped(**changes)
    service._remember_live_alert(item)
    assert await service._persist_alert(item)
    return item


async def detect(service, item):
    service._remember_live_alert(item)
    await service._persist_alert(item)


async def hold_alert_write(service, write, operation):
    held = HeldWrite(write)
    service.database.add_alert = held
    task = asyncio.create_task(operation)
    await asyncio.wait_for(held.entered.wait(), timeout=5)
    return held, task


async def finish(service, held, task):
    held.released.set()
    await task
    service.database.add_alert = held.write


async def race_with_write(service, action, item=None, write=None):
    """Run an action while a first write of the alert is open."""
    item = item or alert()
    service._remember_live_alert(item)
    real_add_alert = service.database.add_alert
    held, task = await hold_alert_write(
        service, write or real_add_alert, service._persist_alert(item)
    )
    await action()
    await finish(service, held, task)
    service.database.add_alert = real_add_alert


def update(service, change, value=True, identifier="synthetic-alert"):
    return lambda: service.update_alert(identifier, AlertUpdate(**{change: value}))


# F1: logging opt-out


@pytest.mark.parametrize("failing", OUTCOMES.values(), ids=OUTCOMES)
async def test_event_write_during_an_opt_out_is_not_queued(service, monkeypatch, failing):
    held = HeldWrite(outage if failing else service.database.add_event)
    monkeypatch.setattr(service.database, "add_event", held)
    task = asyncio.create_task(service._persist_event(event()))
    await asyncio.wait_for(held.entered.wait(), timeout=5)
    await opt_out(service)
    held.released.set()
    await task
    monkeypatch.undo()
    await settle(service)

    assert not service._pending_events
    assert service.storage.pending_writes == 0
    assert ("synthetic-event" in stored_events(service)) is (not failing)


async def test_failed_write_after_an_opt_out_still_reports_the_outage(service, monkeypatch):
    held = HeldWrite(outage)
    monkeypatch.setattr(service.database, "add_event", held)
    task = asyncio.create_task(service._persist_event(event()))
    await asyncio.wait_for(held.entered.wait(), timeout=5)
    await opt_out(service)
    held.released.set()
    await task

    assert service.storage.status == "degraded"
    assert service.storage.last_error_type == "OperationalError"
    assert service.storage.persistence_available is False


async def test_event_write_failing_after_logging_returns_is_not_queued(service, monkeypatch):
    held = HeldWrite(outage)
    monkeypatch.setattr(service.database, "add_event", held)
    task = asyncio.create_task(service._persist_event(event()))
    await asyncio.wait_for(held.entered.wait(), timeout=5)
    await opt_out(service)
    await opt_in(service)
    held.released.set()
    await task
    monkeypatch.undo()
    await settle(service)

    assert "synthetic-event" not in stored_events(service)


async def test_alert_write_failing_after_logging_returns_is_not_queued(service):
    async def action():
        await opt_out(service)
        await opt_in(service)

    await race_with_write(service, action, write=outage)
    await settle(service)

    assert not service._pending_alerts
    assert stored(service) is None
    assert live(service).persistence_pending is False


async def test_update_failing_after_logging_returns_is_not_queued(service, monkeypatch):
    item = alert()
    service._remember_live_alert(item)
    assert await service._persist_alert(item)
    held = HeldWrite(outage)
    monkeypatch.setattr(service.database, "update_alert", held)
    task = asyncio.create_task(service.update_alert(item.id, AlertUpdate(acknowledged=True)))
    await asyncio.wait_for(held.entered.wait(), timeout=5)
    await opt_out(service)
    await opt_in(service)
    held.released.set()
    await task

    assert service.storage.pending_writes == 0
    assert live(service).persistence_pending is False


@pytest.mark.parametrize("available", [True, False], ids=["healthy", "degraded"])
async def test_no_persistence_attempt_starts_after_an_opt_out(service, monkeypatch, available):
    alerts = Recorder(service.database.add_alert)
    events = Recorder(service.database.add_event)
    monkeypatch.setattr(service.database, "add_alert", alerts)
    monkeypatch.setattr(service.database, "add_event", events)
    await opt_out(service)
    if not available:
        fail_storage(service)
    item = alert()
    service._remember_live_alert(item)

    assert await service._persist_alert(item) is False
    assert await service._persist_event(event()) is False

    assert alerts.calls == [] and events.calls == []
    assert not service._pending_alerts and not service._pending_events
    assert live(service).persistence_pending is False


async def test_paired_event_is_not_written_after_an_opt_out_during_its_alert(service):
    """Callers check the setting once, then write an alert and its event in turn."""
    item = alert()
    service._remember_live_alert(item)
    real_add_alert = service.database.add_alert

    async def persist_pair():
        await service._persist_alert(item)
        await service._persist_event(event("paired"))

    held, task = await hold_alert_write(service, real_add_alert, persist_pair())
    await opt_out(service)
    await finish(service, held, task)
    await settle(service)

    assert "paired" not in stored_events(service)
    assert not service._pending_events


async def test_completed_writes_before_an_opt_out_stay_stored(service):
    item = alert()
    service._remember_live_alert(item)
    assert await service._persist_alert(item)
    assert await service._persist_event(event())

    await opt_out(service)
    await settle(service)

    assert stored(service) is not None
    assert "synthetic-event" in stored_events(service)


async def test_alert_write_in_flight_at_an_opt_out_completes_once(service):
    await race_with_write(service, lambda: opt_out(service))
    await settle(service)

    assert stored(service) is not None
    assert not service._pending_alerts
    assert live(service).persistence_pending is False
    assert service.storage.status == "healthy"


async def test_flush_enforces_the_setting_on_its_own(service, monkeypatch):
    """The flush must hold even if a queued entry got past the opt-out discard."""
    alerts = Recorder(service.database.add_alert)
    events = Recorder(service.database.add_event)
    updates = Recorder(service.database.update_alert)
    monkeypatch.setattr(service.database, "add_alert", alerts)
    monkeypatch.setattr(service.database, "add_event", events)
    monkeypatch.setattr(service.database, "update_alert", updates)
    queue(service)
    service._pending_events["queued"] = event("queued")
    service._pending_alert_updates["other"] = AlertUpdate(acknowledged=True)
    service.settings = with_logging(service, False)

    await service._flush_pending()

    assert alerts.calls == [] and events.calls == [] and updates.calls == []
    assert service.storage.pending_writes == 0
    assert live(service).persistence_pending is False


async def test_startup_load_of_a_saved_opt_out_stops_the_queue(tmp_path):
    database = Database(tmp_path / "synthetic.db")
    first = TelemetryService(database)
    assert await first.initialize_storage()
    await opt_out(first)

    service = TelemetryService(database, storage_retry_seconds=0)
    queue(service)
    service._pending_events["queued"] = event("queued")
    assert await service.initialize_storage()

    assert not service._pending_alerts and not service._pending_events
    assert stored(service) is None
    assert "queued" not in stored_events(service)


async def test_flush_write_failing_during_an_opt_out_stays_out_of_the_queues(service):
    real_add_alert = service.database.add_alert
    queue(service)
    held, task = await hold_alert_write(service, outage, service._flush_pending())
    await opt_out(service)
    await finish(service, held, task)
    service.database.add_alert = real_add_alert
    await settle(service)

    assert service.storage.status == "healthy"
    assert not service._pending_alerts
    assert stored(service) is None


async def test_opt_out_discards_queued_operator_updates(service):
    await write_grouped(service)
    fail_storage(service)
    await service.update_alert("synthetic-alert", AlertUpdate(acknowledged=True))
    assert service._pending_alert_updates

    await opt_out(service)
    await settle(service)

    assert not service._pending_alert_updates
    assert stored(service).acknowledged is False
    assert live(service).persistence_pending is False


class StaticCollector:
    def __init__(self, value):
        self.value = value
        self.connection_rankings = []
        self.recently_closed = []

    def collect(self, *args, **kwargs):
        return self.value


async def test_history_write_in_flight_at_an_opt_out_starts_nothing_more(
    service, system_sample, network_sample, monkeypatch
):
    service.system_collector = StaticCollector(system_sample)
    service.network_collector = StaticCollector((network_sample, []))
    service.process_collector = StaticCollector([])
    baselines = Recorder(service.database.save_baseline_state)
    held = HeldWrite(service.database.add_history_batch)
    monkeypatch.setattr(service.database, "save_baseline_state", baselines)
    monkeypatch.setattr(service.database, "add_history_batch", held)
    worker = asyncio.create_task(service._history_writer())
    try:
        await service.collect_once()
        await asyncio.wait_for(held.entered.wait(), timeout=5)
        settings = service.history_settings.model_copy(update={"enabled": False})
        await service.update_history_settings(settings)
        held.released.set()
        await asyncio.wait_for(service._history_queue.join(), timeout=5)
    finally:
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker

    assert baselines.calls == []


async def test_history_batch_queued_before_an_opt_out_is_not_written(
    service, system_sample, network_sample, monkeypatch
):
    service.system_collector = StaticCollector(system_sample)
    service.network_collector = StaticCollector((network_sample, []))
    service.process_collector = StaticCollector([])
    history = Recorder(service.database.add_history_batch)
    monkeypatch.setattr(service.database, "add_history_batch", history)
    await service.collect_once()
    settings = service.history_settings.model_copy(update={"enabled": False})
    await service.update_history_settings(settings)

    worker = asyncio.create_task(service._history_writer())
    try:
        await asyncio.wait_for(service._history_queue.join(), timeout=5)
    finally:
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker

    assert history.calls == []


async def test_history_opt_in_does_not_revive_pre_opt_out_batches(
    service, system_sample, network_sample, monkeypatch
):
    service.system_collector = StaticCollector(system_sample)
    service.network_collector = StaticCollector((network_sample, []))
    service.process_collector = StaticCollector([])
    writes = Recorder(service.database.add_history_batch)
    monkeypatch.setattr(service.database, "add_history_batch", writes)
    await service.collect_once()
    await service.update_history_settings(service.history_settings.model_copy(update={"enabled": False}))
    await service.update_history_settings(service.history_settings.model_copy(update={"enabled": True}))

    worker = asyncio.create_task(service._history_writer())
    try:
        await asyncio.wait_for(service._history_queue.join(), timeout=5)
    finally:
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker

    assert writes.calls == []


# F2: incident relationships


@pytest.mark.parametrize("change", CHANGES)
async def test_link_made_after_an_overlapping_write_survives_reconciliation(service, change):
    await race_with_write(service, update(service, change))
    incident = await link_incident(service, "synthetic-alert")
    await settle(service)

    row = stored(service)
    assert row.incident_id == incident.id
    assert getattr(row, change) is True
    assert live(service).incident_id == incident.id
    assert live(service).persistence_pending is False


async def test_link_made_during_a_first_write_is_kept(service):
    links = []

    async def action():
        links.append(await link_incident(service, "synthetic-alert"))

    await race_with_write(service, action)
    await settle(service)

    assert stored(service).incident_id == links[0].id
    assert live(service).incident_id == links[0].id
    assert not service._pending_alerts


async def test_link_made_during_a_flush_is_kept(service):
    real_add_alert = service.database.add_alert
    queue(service)
    held, task = await hold_alert_write(service, real_add_alert, service._flush_pending())
    incident = await link_incident(service, "synthetic-alert")
    await finish(service, held, task)
    await settle(service)

    assert stored(service).incident_id == incident.id
    assert live(service).incident_id == incident.id


async def test_link_made_before_a_queued_record_is_written_is_kept(service):
    queue(service)
    incident = await link_incident(service, "synthetic-alert")

    await settle(service)

    assert stored(service).incident_id == incident.id
    assert not service._pending_alerts


async def test_operator_update_during_an_outage_keeps_the_link(service):
    item = alert()
    service._remember_live_alert(item)
    assert await service._persist_alert(item)
    incident = await link_incident(service, item.id)
    fail_storage(service)

    await service.update_alert(item.id, AlertUpdate(dismissed=True))
    await settle(service)

    row = stored(service)
    assert row.dismissed is True
    assert row.incident_id == incident.id


async def test_operator_update_does_not_rewrite_the_alert_record(service, monkeypatch):
    item = alert()
    await race_with_write(service, update(service, "acknowledged"), item)
    rewrites = Recorder(service.database.add_alert)
    monkeypatch.setattr(service.database, "add_alert", rewrites)

    await settle(service)

    assert rewrites.calls == []
    assert stored(service).acknowledged is True


def test_rewriting_an_alert_keeps_its_stored_link(tmp_path):
    database = Database(tmp_path / "synthetic.db")
    database.initialize()
    database.add_alert(alert(), 100)
    with database._lock, database._connect() as connection:
        connection.execute("UPDATE alerts SET incident_id = 'INC-synthetic'")

    written = database.add_alert(alert(), 100)

    assert written.incident_id == "INC-synthetic"
    assert database.get_alert("synthetic-alert").incident_id == "INC-synthetic"


def test_unreadable_incident_does_not_block_alert_writes(tmp_path):
    database = Database(tmp_path / "synthetic.db")
    database.initialize()
    with database._lock, database._connect() as connection:
        connection.execute(
            """INSERT INTO incidents
            (id, title, severity, detection_timestamp, first_observed, last_observed,
             related_processes, related_connections, related_alerts, evidence_summary,
             status, analyst_notes)
            VALUES ('INC-bad', 't', 'INFO', ?, ?, ?, '[]', '[]', 'not json', '', 'open', '')""",
            (NOW.isoformat(), NOW.isoformat(), NOW.isoformat()),
        )

    assert database.add_alert(alert(), 100).incident_id is None


async def test_link_survives_a_restart(service, tmp_path):
    await race_with_write(service, update(service, "acknowledged"))
    incident = await link_incident(service, "synthetic-alert")
    await settle(service)

    restarted = TelemetryService(Database(tmp_path / "synthetic.db"))
    assert await restarted.initialize_storage()

    row = stored(restarted)
    assert row.incident_id == incident.id
    assert row.acknowledged is True


# F3: grouped occurrence counts


@pytest.mark.parametrize("change", CHANGES)
async def test_update_during_a_grouped_write_is_not_a_detection(service, change):
    await race_with_write(service, update(service, change), grouped())
    await settle(service)

    row = stored(service)
    assert row.occurrence_count == 1
    assert getattr(row, change) is True
    assert live(service).occurrence_count == 1
    assert live(service).persistence_pending is False


async def test_update_during_a_grouped_flush_is_not_a_detection(service):
    real_add_alert = service.database.add_alert
    item = grouped().model_copy(update={"persistence_pending": True})
    service._remember_live_alert(item)
    service._pending_alerts[item.id] = item

    held, task = await hold_alert_write(service, real_add_alert, service._flush_pending())
    await service.update_alert(item.id, AlertUpdate(acknowledged=True))
    await finish(service, held, task)
    await settle(service)

    assert stored(service).occurrence_count == 1
    assert stored(service).acknowledged is True
    assert service.storage.pending_writes == 0


@pytest.mark.parametrize("change", CHANGES)
async def test_grouped_update_during_an_outage_is_not_a_detection(service, change, monkeypatch):
    await write_grouped(service)

    with monkeypatch.context() as patch:
        patch.setattr(service.database, "update_alert", outage)
        await service.update_alert("synthetic-alert", AlertUpdate(**{change: True}))
    await settle(service)

    assert stored(service).occurrence_count == 1
    assert live(service).occurrence_count == 1
    assert getattr(stored(service), change) is True


async def test_update_to_a_queued_detection_adds_it_once(service):
    await write_grouped(service)
    fail_storage(service)
    await detect(service, grouped())

    await service.update_alert("synthetic-alert", AlertUpdate(acknowledged=True))
    await settle(service)

    assert stored(service).occurrence_count == 2
    assert stored(service).acknowledged is True


async def test_detection_queued_during_a_flush_is_added_once(service):
    real_add_alert = service.database.add_alert
    first = grouped().model_copy(update={"persistence_pending": True})
    service._remember_live_alert(first)
    service._pending_alerts[first.id] = first

    held, task = await hold_alert_write(service, real_add_alert, service._flush_pending())
    service.storage = service.storage.model_copy(update={"persistence_available": False})
    await detect(service, grouped())
    await finish(service, held, task)
    await settle(service)

    assert stored(service).occurrence_count == 2
    assert not service._pending_alerts


async def test_count_is_stable_through_an_outage_and_repeated_flushes(service, monkeypatch):
    await write_grouped(service)
    fail_storage(service)
    await service.update_alert("synthetic-alert", AlertUpdate(acknowledged=True))
    await detect(service, grouped())

    with monkeypatch.context() as patch:
        patch.setattr(service.database, "update_alert", outage)
        patch.setattr(service.database, "add_alert", outage)
        await service._flush_pending()
        service.storage = service.storage.model_copy(update={"persistence_available": True})
        await service._flush_pending()
    assert service.storage.status == "degraded"

    await settle(service, 5)

    assert stored(service).occurrence_count == 2
    assert stored(service).acknowledged is True
    assert service.storage.pending_writes == 0


async def test_count_is_stable_after_a_restart(service, tmp_path):
    await race_with_write(service, update(service, "acknowledged"), grouped())
    await settle(service)

    restarted = TelemetryService(Database(tmp_path / "synthetic.db"), storage_retry_seconds=0)
    assert await restarted.initialize_storage()
    await settle(restarted)

    assert stored(restarted).occurrence_count == 1
    assert stored(restarted).acknowledged is True


async def test_update_queued_before_a_reopen_does_not_apply_to_it(service):
    await write_grouped(service, condition_active=False, occurrence_count=1)
    fail_storage(service)
    await service.update_alert("synthetic-alert", AlertUpdate(acknowledged=True, dismissed=True))

    await detect(service, grouped())
    await settle(service)

    row = stored(service)
    assert (row.acknowledged, row.dismissed) == (False, False)
    assert row.occurrence_count == 2
    assert row.condition_active is True
    assert (live(service).acknowledged, live(service).dismissed) == (False, False)


async def test_update_made_after_a_reopen_applies_to_it(service):
    await write_grouped(service, condition_active=False, occurrence_count=1)
    fail_storage(service)
    await detect(service, grouped())

    await service.update_alert("synthetic-alert", AlertUpdate(acknowledged=True))
    await settle(service)

    assert stored(service).acknowledged is True
    assert stored(service).occurrence_count == 2


async def test_resolve_and_reopen_within_an_outage_clears_operator_state(service):
    await write_grouped(service, acknowledged=True, dismissed=True)
    fail_storage(service)
    await detect(service, grouped(condition_active=False, occurrence_count=0))
    await detect(service, grouped())

    await settle(service)

    row = stored(service)
    assert (row.acknowledged, row.dismissed) == (False, False)
    assert (live(service).acknowledged, live(service).dismissed) == (False, False)
    assert row.occurrence_count == 2


# F4: grouped boolean reversal


@pytest.mark.parametrize("change", CHANGES)
async def test_reversal_during_an_outage_reaches_the_database(service, change, monkeypatch):
    await write_grouped(service, **{change: True})

    with monkeypatch.context() as patch:
        patch.setattr(service.database, "update_alert", outage)
        await service.update_alert("synthetic-alert", AlertUpdate(**{change: False}))
    await settle(service)

    assert getattr(stored(service), change) is False
    assert getattr(live(service), change) is False
    assert stored(service).occurrence_count == 1


@pytest.mark.parametrize("change", CHANGES)
async def test_reversal_survives_a_queued_detection(service, change):
    await write_grouped(service, **{change: True})
    fail_storage(service)
    await detect(service, grouped())
    await service.update_alert("synthetic-alert", AlertUpdate(**{change: False}))

    await settle(service)

    assert getattr(stored(service), change) is False
    assert getattr(live(service), change) is False
    assert stored(service).occurrence_count == 2


@pytest.mark.parametrize("change", CHANGES)
async def test_reversal_during_a_grouped_write_is_kept(service, change):
    await write_grouped(service, **{change: True})

    async def reverse():
        await service.update_alert("synthetic-alert", AlertUpdate(**{change: False}))

    await race_with_write(service, reverse, grouped(**{change: True}))
    await settle(service)

    assert getattr(stored(service), change) is False
    assert getattr(live(service), change) is False
    assert stored(service).occurrence_count == 2


async def test_later_update_replaces_a_queued_one(service):
    await write_grouped(service)
    fail_storage(service)
    await service.update_alert("synthetic-alert", AlertUpdate(acknowledged=True))
    await service.update_alert("synthetic-alert", AlertUpdate(acknowledged=False, dismissed=True))

    await settle(service)

    assert stored(service).acknowledged is False
    assert stored(service).dismissed is True


async def test_healthy_update_replaces_a_queued_one(service, monkeypatch):
    await write_grouped(service)
    with monkeypatch.context() as patch:
        patch.setattr(service.database, "update_alert", outage)
        await service.update_alert("synthetic-alert", AlertUpdate(acknowledged=True))
    service.storage = service.storage.model_copy(update={"persistence_available": True})

    await service.update_alert("synthetic-alert", AlertUpdate(acknowledged=False))
    await settle(service)

    assert stored(service).acknowledged is False


async def test_update_arriving_during_its_replay_is_saved_by_the_next_pass(service, monkeypatch):
    await write_grouped(service)
    fail_storage(service)
    await service.update_alert("synthetic-alert", AlertUpdate(acknowledged=True))

    real_update = service.database.update_alert
    held = HeldWrite(real_update)
    monkeypatch.setattr(service.database, "update_alert", held)
    task = asyncio.create_task(service._maybe_recover_storage())
    await asyncio.wait_for(held.entered.wait(), timeout=5)
    queued = dict(service._pending_alert_updates)
    held.released.set()
    service._queue_alert_update("synthetic-alert", {"dismissed": True})
    await task
    monkeypatch.undo()
    await settle(service)

    assert queued
    assert stored(service).acknowledged is True
    assert stored(service).dismissed is True
    assert live(service).persistence_pending is False


# Queue accounting and storage health


async def test_update_while_storage_is_already_degraded_is_saved(service):
    item = alert()
    service._remember_live_alert(item)
    assert await service._persist_alert(item)
    fail_storage(service)

    updated = await service.update_alert(item.id, AlertUpdate(acknowledged=True))

    assert updated.persistence_pending is True
    assert service.storage.pending_writes >= 1
    await settle(service)
    assert stored(service).acknowledged is True
    assert live(service).persistence_pending is False


async def test_queued_updates_are_bounded_and_counted(service, monkeypatch):
    monkeypatch.setattr("app.service.MAX_PENDING_WRITES", 2)
    for name in ("first", "second", "third"):
        service._remember_live_alert(alert(name))
    fail_storage(service)

    for name in ("first", "second", "third"):
        await service.update_alert(name, AlertUpdate(acknowledged=True))

    assert list(service._pending_alert_updates) == ["second", "third"]
    assert service.dropped_alert_writes == 1
    assert live(service, "first").persistence_pending is False
    assert live(service, "second").persistence_pending is True


async def test_real_update_queue_limit_is_enforced(service):
    fail_storage(service)
    for index in range(MAX_PENDING_WRITES + 1):
        service._queue_alert_update(f"alert-{index}", {"acknowledged": True})

    assert len(service._pending_alert_updates) == MAX_PENDING_WRITES


async def test_update_replay_failure_keeps_the_queue_and_reports_it(service, monkeypatch):
    await write_grouped(service)
    fail_storage(service)
    await service.update_alert("synthetic-alert", AlertUpdate(acknowledged=True))

    monkeypatch.setattr(service.database, "update_alert", outage)
    await settle(service)

    assert service.storage.status == "degraded"
    assert list(service._pending_alert_updates) == ["synthetic-alert"]
    # Audit and recovery events also remain queued behind the failing patch.
    assert not service._pending_alerts
    assert service.storage.pending_writes == 1 + len(service._pending_events)


async def test_update_to_a_missing_row_leaves_nothing_pending(service):
    service._remember_live_alert(alert())
    fail_storage(service)
    await service.update_alert("synthetic-alert", AlertUpdate(acknowledged=True))

    await settle(service)

    assert service.storage.pending_writes == 0
    assert live(service).persistence_pending is False
    assert live(service).acknowledged is True


async def test_outage_and_replay_do_not_log_sensitive_values(service, monkeypatch, caplog):
    secret = alert(evidence={"command_line": PRIVATE})
    service._remember_live_alert(secret)
    assert await service._persist_alert(secret)
    caplog.set_level(logging.DEBUG)

    with monkeypatch.context() as patch:
        patch.setattr(service.database, "update_alert", outage)
        patch.setattr(service.database, "add_event", outage)
        await service.update_alert(secret.id, AlertUpdate(acknowledged=True))
        await service._persist_event(event())
        await settle(service)
    await settle(service)

    assert service.storage.status == "healthy"
    assert PRIVATE not in caplog.text
    assert PRIVATE not in str(SimpleNamespace(health=service.storage))


async def test_new_observation_during_a_failed_first_write_keeps_its_order(service):
    await write_grouped(service, acknowledged=True, dismissed=True)

    async def resolve():
        await detect(service, grouped(condition_active=False, occurrence_count=0))

    await race_with_write(service, resolve, grouped(), write=outage)
    await settle(service)

    row = stored(service)
    assert row.condition_active is False
    assert row.occurrence_count == 2
    assert row.acknowledged is True and row.dismissed is True


async def test_observation_during_operator_replay_is_not_counted_twice(service, monkeypatch):
    await write_grouped(service)
    fail_storage(service)
    await service.update_alert("synthetic-alert", AlertUpdate(acknowledged=True))
    held = HeldWrite(service.database.update_alert)
    monkeypatch.setattr(service.database, "update_alert", held)
    task = asyncio.create_task(service._maybe_recover_storage())
    try:
        await asyncio.wait_for(held.entered.wait(), timeout=5)
        await detect(service, grouped())
        # A simultaneous recovery pass must not start another replay.
        await service._flush_pending()
    finally:
        held.released.set()
        await task
    monkeypatch.undo()
    await settle(service)

    assert stored(service).occurrence_count == 2
    assert live(service).occurrence_count == 2
    assert stored(service).acknowledged is True
    assert live(service).persistence_pending is False


async def test_rejected_observation_keeps_the_operator_decision_live(service, monkeypatch):
    queue(service)
    fail_storage(service)
    await service.update_alert("synthetic-alert", AlertUpdate(acknowledged=True))

    def invalid(*args):
        raise InvalidRecordError("synthetic")

    monkeypatch.setattr(service.database, "add_alert", invalid)
    await settle(service)

    assert service.rejected_writes == 1
    assert stored(service) is None
    assert live(service).acknowledged is True
    assert live(service).persistence_pending is False
    assert service.storage.pending_writes == 0


async def test_grouped_read_and_write_hold_one_transaction(service, monkeypatch):
    await write_grouped(service)
    held = HeldWrite(service.database._row_to_alert)
    monkeypatch.setattr(service.database, "_row_to_alert", held)
    task = asyncio.create_task(asyncio.to_thread(service.database.add_alert, grouped(), 100))
    try:
        await asyncio.wait_for(held.entered.wait(), timeout=5)
        # A different connection must not commit a link between the grouped
        # read and upsert. Use a zero busy timeout to avoid timing assertions.
        with closing(sqlite3.connect(service.database.path, timeout=0)) as connection:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                connection.execute("UPDATE alerts SET incident_id = 'INC-synthetic'")
    finally:
        held.released.set()
        await task


async def test_empty_update_can_read_an_alert_not_in_live_memory(service):
    service.database.add_alert(alert(), 100)

    result = await service.update_alert("synthetic-alert", AlertUpdate())

    assert result is not None
    assert result.id == "synthetic-alert"
    assert result.occurrence_count == 1
