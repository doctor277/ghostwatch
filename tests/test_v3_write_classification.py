from __future__ import annotations

import logging
import sqlite3
from contextlib import closing
from datetime import UTC, datetime

import pytest

from app.models import Alert, EventRecord
from app.service import TelemetryService
from app.storage.database import Database, InvalidRecordError

PRIVATE = "synthetic-private-marker"
NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
NAIVE = datetime(2026, 1, 2, 3, 4, 5)
SQLITE_MAX_INTEGER = 2**63 - 1
IMPLEMENTATION_ERRORS = [TypeError, ValueError, OverflowError, AttributeError, KeyError]


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


def event(identifier="synthetic-event", **changes):
    values = {"id": identifier, "timestamp": NOW, "event_type": "synthetic", "title": "synthetic"}
    return EventRecord(**(values | changes))


def circular():
    value = {}
    value["self"] = value
    return value


INVALID_ALERTS = {
    "integer-out-of-range": {"occurrence_count": 2**70},
    "naive-timestamp": {"timestamp": NAIVE},
    "naive-optional-timestamp": {"first_occurrence": NAIVE},
    "timestamp-of-the-wrong-type": {"last_recovery": PRIVATE},
    "unencodable-text": {"explanation": f"{PRIVATE} \ud800"},
    "unencodable-label": {"metric": f"{PRIVATE} \ud800"},
    "circular-evidence": {"evidence": circular()},
    "unsupported-evidence-key": {"evidence": {(1,): PRIVATE}},
    "unsupported-column-type": {"source": [PRIVATE]},
}
INVALID_EVENTS = {
    "naive-timestamp": {"timestamp": NAIVE},
    "unencodable-text": {"summary": f"{PRIVATE} \ud800"},
    "circular-evidence": {"evidence": circular()},
}


def invalid_alert(changes, identifier="synthetic-alert"):
    return alert(id=identifier).model_copy(update=changes)


def invalid_event(changes, identifier="synthetic-event"):
    return event(identifier).model_copy(update=changes)


@pytest.fixture
async def service(tmp_path):
    item = TelemetryService(Database(tmp_path / "synthetic.db"), storage_retry_seconds=0)
    assert await item.initialize_storage()
    return item


def stored_ids(service):
    return [item.id for item in service.database.list_alerts(10, True)]


# Records that can never be written


@pytest.mark.parametrize("changes", INVALID_ALERTS.values(), ids=INVALID_ALERTS)
def test_database_identifies_an_invalid_alert(tmp_path, changes):
    database = Database(tmp_path / "synthetic.db")
    database.initialize()
    with pytest.raises(InvalidRecordError) as raised:
        database.add_alert(invalid_alert(changes), 100)
    assert PRIVATE not in str(raised.value)
    assert database.list_alerts(10, True) == []


@pytest.mark.parametrize("changes", INVALID_EVENTS.values(), ids=INVALID_EVENTS)
def test_database_identifies_an_invalid_event(tmp_path, changes):
    database = Database(tmp_path / "synthetic.db")
    database.initialize()
    with pytest.raises(InvalidRecordError) as raised:
        database.add_event(invalid_event(changes))
    assert PRIVATE not in str(raised.value)
    assert database.list_events(10) == []


def test_largest_storable_integer_is_accepted(tmp_path):
    database = Database(tmp_path / "synthetic.db")
    database.initialize()
    database.add_alert(alert(occurrence_count=SQLITE_MAX_INTEGER), 100)
    assert database.list_alerts(10, True)[0].occurrence_count == SQLITE_MAX_INTEGER


def test_grouped_count_overflow_leaves_the_stored_row_unchanged(tmp_path):
    database = Database(tmp_path / "synthetic.db")
    database.initialize()
    grouped = alert(rule_id="collection_failure", occurrence_count=SQLITE_MAX_INTEGER)
    database.add_alert(grouped, 100)
    with pytest.raises(InvalidRecordError):
        database.add_alert(grouped, 100)
    assert database.list_alerts(10, True)[0].occurrence_count == SQLITE_MAX_INTEGER


@pytest.mark.parametrize("changes", INVALID_ALERTS.values(), ids=INVALID_ALERTS)
async def test_invalid_alert_is_counted_and_valid_writes_continue(service, changes):
    assert not await service._persist_alert(invalid_alert(changes, "invalid"))
    assert await service._persist_alert(alert(id="valid"))
    assert service.rejected_writes == 1
    assert service.storage.status == "healthy"
    assert not service._pending_alerts
    assert stored_ids(service) == ["valid"]


@pytest.mark.parametrize("changes", INVALID_EVENTS.values(), ids=INVALID_EVENTS)
async def test_invalid_event_is_counted_and_valid_writes_continue(service, changes):
    assert not await service._persist_event(invalid_event(changes, "invalid"))
    assert await service._persist_event(event("valid"))
    assert service.rejected_writes == 1
    assert service.storage.status == "healthy"
    assert not service._pending_events
    stored = [item.id for item in service.database.list_events(10)]
    assert "valid" in stored and "invalid" not in stored


async def test_invalid_records_queued_in_an_outage_are_rejected_once(service, monkeypatch):
    service._mark_storage_failure(sqlite3.OperationalError("synthetic outage"))
    await service._persist_alert(invalid_alert({"occurrence_count": 2**70}, "invalid"))
    await service._persist_alert(alert(id="valid"))
    await service._persist_event(invalid_event({"timestamp": NAIVE}, "invalid"))
    await service._persist_event(event("valid"))
    assert list(service._pending_alerts) == ["invalid", "valid"]

    attempts = []
    real_add_alert = service.database.add_alert

    def add_alert(item, retention):
        attempts.append(item.id)
        return real_add_alert(item, retention)

    monkeypatch.setattr(service.database, "add_alert", add_alert)
    for _ in range(3):
        await service._maybe_recover_storage()

    assert attempts == ["invalid", "valid"]
    assert service.rejected_writes == 2
    assert service.storage.status == "healthy"
    assert not service._pending_alerts and not service._pending_events
    assert stored_ids(service) == ["valid"]
    assert "valid" in [item.id for item in service.database.list_events(10)]


# Failures that say nothing about the record


@pytest.mark.parametrize("error", IMPLEMENTATION_ERRORS)
async def test_implementation_error_during_a_write_is_not_a_rejection(
    service, monkeypatch, error
):
    def defect(*args):
        raise error(PRIVATE)

    monkeypatch.setattr(service.database, "_enforce_retention", defect)
    assert not await service._persist_alert(alert())

    assert service.rejected_writes == 0
    assert service.storage.status == "degraded"
    assert service.storage.last_error_type == error.__name__
    assert list(service._pending_alerts) == ["synthetic-alert"]

    monkeypatch.undo()
    await service._maybe_recover_storage()
    assert service.storage.status == "healthy"
    assert not service._pending_alerts
    assert stored_ids(service) == ["synthetic-alert"]


@pytest.mark.parametrize("error", IMPLEMENTATION_ERRORS)
async def test_implementation_error_during_an_event_write_is_not_a_rejection(
    service, monkeypatch, error
):
    def defect(*args):
        raise error(PRIVATE)

    monkeypatch.setattr(service.database, "add_event", defect)
    assert not await service._persist_event(event())

    assert service.rejected_writes == 0
    assert service.storage.status == "degraded"
    assert list(service._pending_events) == ["synthetic-event"]

    monkeypatch.undo()
    await service._maybe_recover_storage()
    assert service.storage.status == "healthy"
    assert not service._pending_events


@pytest.mark.parametrize("error", IMPLEMENTATION_ERRORS)
async def test_implementation_error_while_flushing_keeps_every_queued_record(
    service, monkeypatch, error
):
    service._mark_storage_failure(sqlite3.OperationalError("synthetic outage"))
    for identifier in ("first", "second"):
        await service._persist_alert(alert(id=identifier))
    await service._persist_event(event())

    def defect(*args):
        raise error(PRIVATE)

    monkeypatch.setattr(service.database, "_enforce_retention", defect)
    await service._maybe_recover_storage()

    assert service.rejected_writes == 0
    assert service.storage.status == "degraded"
    assert list(service._pending_alerts) == ["first", "second"]
    assert list(service._pending_events)[0] == "synthetic-event"

    monkeypatch.undo()
    await service._maybe_recover_storage()
    assert service.storage.status == "healthy"
    assert not service._pending_alerts and not service._pending_events
    assert sorted(stored_ids(service)) == ["first", "second"]


async def test_corrupt_stored_row_is_not_blamed_on_the_new_record(service):
    grouped = alert(rule_id="collection_failure", condition_active=True)
    assert await service._persist_alert(grouped)
    with closing(sqlite3.connect(service.database.path)) as connection, connection:
        connection.execute("UPDATE alerts SET timestamp = 'corrupt'")

    assert not await service._persist_alert(grouped)

    assert service.rejected_writes == 0
    assert service.storage.status == "degraded"
    assert list(service._pending_alerts) == [grouped.id]

    with closing(sqlite3.connect(service.database.path)) as connection, connection:
        connection.execute("UPDATE alerts SET timestamp = ?", (NOW.isoformat(),))
    await service._maybe_recover_storage()
    assert service.storage.status == "healthy"
    assert not service._pending_alerts


async def test_simulated_outage_loses_nothing_and_recovers(service, monkeypatch):
    def outage(*args, **kwargs):
        raise sqlite3.OperationalError("synthetic outage")

    monkeypatch.setattr(service.database, "_connect", outage)
    assert not await service._persist_alert(alert())
    assert not await service._persist_event(event())
    await service._maybe_recover_storage()

    assert service.rejected_writes == 0
    assert service.storage.status == "degraded"
    assert service.storage.pending_writes >= 2

    monkeypatch.undo()
    await service._maybe_recover_storage()
    assert service.storage.status == "healthy"
    assert not service._pending_alerts and not service._pending_events
    assert stored_ids(service) == ["synthetic-alert"]
    assert "synthetic-event" in [item.id for item in service.database.list_events(10)]


# Logs


async def test_write_failures_keep_record_content_out_of_logs(service, monkeypatch, caplog):
    def defect(*args):
        raise TypeError(PRIVATE)

    with caplog.at_level(logging.DEBUG):
        for changes in INVALID_ALERTS.values():
            await service._persist_alert(invalid_alert(changes))
        for changes in INVALID_EVENTS.values():
            await service._persist_event(invalid_event(changes))
        monkeypatch.setattr(service.database, "_enforce_retention", defect)
        await service._persist_alert(alert(explanation=PRIVATE))
        await service._maybe_recover_storage()
        monkeypatch.undo()
        await service._maybe_recover_storage()

    assert service.rejected_writes == len(INVALID_ALERTS) + len(INVALID_EVENTS)
    assert PRIVATE not in caplog.text
