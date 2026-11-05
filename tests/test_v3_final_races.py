from __future__ import annotations

import asyncio
import sqlite3
import threading
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.api.routes import update_alert as patch_alert
from app.models import Alert, AlertUpdate, IncidentCreate
from app.service import TelemetryService
from app.storage.database import Database


def alert(identifier="synthetic", **changes):
    return Alert(**({
        "id": identifier, "timestamp": datetime(2026, 1, 2, tzinfo=UTC),
        "severity": "WARNING", "source": "synthetic", "metric": "synthetic",
        "explanation": "synthetic",
    } | changes))


class Gate:
    """Pause only the first call, before or after its real database operation."""

    def __init__(self, operation, *, after=False):
        self.operation = operation
        self.after = after
        self.loop = asyncio.get_running_loop()
        self.entered = asyncio.Event()
        self.finished = asyncio.Event()
        self.release = threading.Event()
        self.calls = 0

    def __call__(self, *args):
        self.calls += 1
        if self.calls != 1:
            return self.operation(*args)
        try:
            result = self.operation(*args) if self.after else None
            self.loop.call_soon_threadsafe(self.entered.set)
            if not self.release.wait(10):
                raise TimeoutError("Synthetic barrier was not released")
            return result if self.after else self.operation(*args)
        finally:
            self.loop.call_soon_threadsafe(self.finished.set)

    async def wait(self):
        await asyncio.wait_for(self.entered.wait(), timeout=5)


@pytest.fixture
async def service(tmp_path):
    item = TelemetryService(Database(tmp_path / "synthetic.db"), storage_retry_seconds=0)
    assert await item.initialize_storage()
    return item


async def settle(service):
    for _ in range(4):
        await service._maybe_recover_storage()


async def write(service, item):
    service._remember_live_alert(item)
    await service._persist_alert(item)


def request(service):
    return Request({"type": "http", "app": SimpleNamespace(
        state=SimpleNamespace(telemetry=service)
    )})


# F1: a completed settings save must supersede an older recovery read.
@pytest.mark.parametrize("initial,choices", [
    (True, [False]), (False, [True]), (True, [False, True]),
    (False, [True, False]),
], ids=["off", "on", "off-on", "on-off"])
async def test_settings_save_supersedes_older_recovery_read(service, monkeypatch, initial, choices):
    await service.update_settings(service.settings.model_copy(update={
        "event_logging_enabled": initial, "cpu_threshold": 81,
    }))
    service._persisted_state_missing = True
    service._mark_storage_failure(sqlite3.OperationalError("synthetic"))
    held = Gate(service.database.load_settings, after=True)
    monkeypatch.setattr(service.database, "load_settings", held)
    recovery = asyncio.create_task(service._maybe_recover_storage())
    try:
        await held.wait()
        for index, enabled in enumerate(choices):
            saved = service.settings.model_copy(update={
                "event_logging_enabled": enabled, "cpu_threshold": 61 + index,
                "memory_threshold": 72, "refresh_interval": 3,
            })
            await service.update_settings(saved)
    finally:
        held.release.set()
        await asyncio.wait_for(recovery, timeout=5)

    assert service.database.load_settings() == saved
    assert service.settings == saved
    await service.record_event("synthetic-probe", "LOW", "synthetic", "synthetic")
    stored = [e for e in service.database.list_events(50) if e.event_type == "synthetic-probe"]
    assert len(stored) == int(choices[-1])


@pytest.mark.parametrize("attribute,loader,updater,changes", [
    ("history_settings", "load_history_settings", "update_history_settings", {"enabled": False}),
    ("detection_settings", "load_detection_settings", "update_detection_settings", {"cooldown_seconds": 15}),
    ("ai_settings", "load_ai_settings", "update_ai_settings", {"enabled": True}),
    ("windows_event_settings", "load_windows_event_settings", "update_windows_event_settings", {"enabled": False}),
    ("file_integrity_settings", "load_file_integrity_settings", "update_file_integrity_settings", {"enabled": True}),
])
async def test_other_saved_configuration_survives_recovery(
    service, monkeypatch, attribute, loader, updater, changes
):
    held = Gate(getattr(service.database, loader), after=True)
    monkeypatch.setattr(service.database, loader, held)
    recovery = asyncio.create_task(service._load_persisted_state())
    try:
        await held.wait()
        current = getattr(service, attribute)
        saved = type(current).model_validate(current.model_dump() | changes)
        await getattr(service, updater)(saved)
    finally:
        held.release.set()
        await asyncio.wait_for(recovery, timeout=5)
    assert getattr(service, attribute) == saved
    assert getattr(service.database, loader)() == saved


# F2: a queued patch still needs a real response record, including after restart.
@pytest.mark.parametrize("busy", [False, True], ids=["idle", "busy"])
@pytest.mark.parametrize("exists", [False, True], ids=["missing", "existing"])
async def test_persisted_only_alert_patch_response(service, monkeypatch, busy, exists):
    if exists:
        service.database.add_alert(alert(), 100)
    assert service._live_alert("synthetic") is None
    task = None
    held = Gate(service.database.add_alert)
    if busy:
        monkeypatch.setattr(service.database, "add_alert", held)
        task = asyncio.create_task(service._persist_alert(alert("unrelated")))
    try:
        if busy:
            await held.wait()
        if exists:
            response = await patch_alert("synthetic", AlertUpdate(acknowledged=True), request(service))
            assert response.id == "synthetic"
            assert response.acknowledged is True
            assert response.occurrence_count == 1
            assert response.persistence_pending is busy
            if busy:
                assert service.database.get_alert("synthetic").acknowledged is False
                second = await patch_alert("synthetic", AlertUpdate(dismissed=True), request(service))
                assert second.acknowledged is True and second.dismissed is True
        else:
            with pytest.raises(HTTPException) as failure:
                await patch_alert("synthetic", AlertUpdate(acknowledged=True), request(service))
            assert failure.value.status_code == 404
            assert "synthetic" not in service._pending_alert_updates
    finally:
        held.release.set()
        if task is not None:
            await asyncio.wait_for(task, timeout=5)
    await settle(service)
    row = service.database.get_alert("synthetic")
    if exists:
        assert row.acknowledged is True
        assert row.dismissed is busy
        assert row.occurrence_count == 1
    else:
        assert row is None


async def test_persisted_only_patch_while_logging_off_is_live_only(service):
    service.database.add_alert(alert(), 100)
    await service.update_settings(service.settings.model_copy(update={"event_logging_enabled": False}))
    response = await patch_alert("synthetic", AlertUpdate(acknowledged=True), request(service))
    assert response.acknowledged is True
    assert response.persistence_pending is False
    assert service.database.get_alert("synthetic").acknowledged is False


async def test_alert_lookup_failure_is_unavailable_not_missing(service, monkeypatch):
    def unavailable(*args):
        raise sqlite3.OperationalError("synthetic-private-marker")

    monkeypatch.setattr(service.database, "get_alert", unavailable)
    with pytest.raises(HTTPException) as failure:
        await patch_alert("synthetic", AlertUpdate(acknowledged=True), request(service))
    assert failure.value.status_code == 503
    assert "synthetic-private-marker" not in failure.value.detail
    assert service.storage.status == "degraded"
    assert not service._pending_alert_updates


async def test_opt_out_during_alert_lookup_cannot_revive_the_patch(service, monkeypatch):
    service.database.add_alert(alert(), 100)
    held = Gate(service.database.get_alert, after=True)
    monkeypatch.setattr(service.database, "get_alert", held)
    action = asyncio.create_task(service.update_alert("synthetic", AlertUpdate(acknowledged=True)))
    try:
        await held.wait()
        for enabled in (False, True):
            await service.update_settings(service.settings.model_copy(update={"event_logging_enabled": enabled}))
    finally:
        held.release.set()
        response = await asyncio.wait_for(action, timeout=5)
    await settle(service)
    assert response.acknowledged is True
    assert response.persistence_pending is False
    assert service.database.get_alert("synthetic").acknowledged is False


# F3: delayed operation completion must not publish the operation's stale link.
async def test_two_incident_completions_keep_the_committed_link(service, monkeypatch):
    await write(service, alert())
    held = Gate(service.database.add_incident, after=True)
    monkeypatch.setattr(service.database, "add_incident", held)
    older = asyncio.create_task(service.create_incident(IncidentCreate(
        title="synthetic older", severity="WARNING", alert_ids=["synthetic"]
    )))
    try:
        await held.wait()
        newer = await service.create_incident(IncidentCreate(
            title="synthetic newer", severity="WARNING", alert_ids=["synthetic"]
        ))
    finally:
        held.release.set()
        first = await asyncio.wait_for(older, timeout=5)
    assert service.database.get_alert("synthetic").incident_id == newer.id
    assert service._live_alert("synthetic").incident_id == newer.id
    response = await patch_alert("synthetic", AlertUpdate(acknowledged=True), request(service))
    assert response.incident_id == newer.id
    assert (await service.list_alerts())[0].incident_id == newer.id
    incidents = await service.list_incidents()
    assert {i.id for i in incidents} == {first.id, newer.id}
    assert all(i.related_alerts == ["synthetic"] for i in incidents)


async def test_acknowledgement_reconciles_an_authoritative_link(service):
    await write(service, alert())
    newer = await service.create_incident(IncidentCreate(
        title="synthetic", severity="WARNING", alert_ids=["synthetic"]
    ))
    service._remember_live_alert(alert(incident_id="INC-synthetic-stale"), authoritative=True)
    response = await patch_alert("synthetic", AlertUpdate(acknowledged=True), request(service))
    assert response.incident_id == newer.id
    assert service.database.get_alert("synthetic").incident_id == newer.id


@pytest.mark.parametrize("overlap", [False, True], ids=["different-alerts", "same-alert"])
async def test_delayed_incident_refresh_cannot_replace_a_newer_refresh(service, monkeypatch, overlap):
    await write(service, alert())
    await write(service, alert("other"))
    held = Gate(service.database.get_alert_incident_ids, after=True)
    monkeypatch.setattr(service.database, "get_alert_incident_ids", held)
    older = asyncio.create_task(service.create_incident(IncidentCreate(
        title="synthetic older", severity="WARNING", alert_ids=["synthetic"]
    )))
    second_id = "synthetic" if overlap else "other"
    try:
        await held.wait()
        newer = await service.create_incident(IncidentCreate(
            title="synthetic newer", severity="WARNING", alert_ids=[second_id]
        ))
    finally:
        held.release.set()
        first = await asyncio.wait_for(older, timeout=5)
    expected = {"synthetic": newer.id} if overlap else {"synthetic": first.id, "other": newer.id}
    for identifier, link in expected.items():
        assert service._live_alert(identifier).incident_id == link
        assert service.database.get_alert(identifier).incident_id == link
    assert not service._incident_refreshes


# F4: cancellation propagates to the caller, but must not abandon reconciliation.
@pytest.mark.parametrize("restart_early", [False, True], ids=["commit-before-start", "start-before-commit"])
@pytest.mark.parametrize("fails", [False, True], ids=["commit", "rollback"])
async def test_cancelled_recovery_counts_each_observation_once(
    service, monkeypatch, restart_early, fails
):
    trigger = asyncio.Event()

    async def collect():
        await service._maybe_recover_storage()

    async def producer():
        await trigger.wait()
        await collect()
        await asyncio.Event().wait()

    async def idle():
        await asyncio.Event().wait()

    monkeypatch.setattr(service, "collect_once", collect)
    monkeypatch.setattr(service, "_run", producer)
    for name in ("_history_writer", "_windows_event_loop", "_file_integrity_loop"):
        monkeypatch.setattr(service, name, idle)
    await service.start()
    original = service.database.add_alert

    def first_write(*args):
        if fails:
            raise sqlite3.OperationalError("synthetic rollback")
        return original(*args)

    held = Gate(first_write)
    monkeypatch.setattr(service.database, "add_alert", held)
    service._mark_storage_failure(sqlite3.OperationalError("synthetic"))
    observation = alert(rule_id="collection_failure", condition_active=True)
    await write(service, observation)
    trigger.set()
    try:
        await held.wait()
        producer_task = service._task
        await asyncio.wait_for(service.stop(), timeout=1)
        assert producer_task.cancelled()
        if restart_early:
            await asyncio.wait_for(service.start(), timeout=1)
    finally:
        held.release.set()
        await asyncio.wait_for(held.finished.wait(), timeout=5)
        # A successful implementation owns reconciliation independently of the
        # cancelled producer. Wait for that owner before checking stable state.
        owner = getattr(service, "_alert_persistence_task", None)
        if owner is not None:
            await asyncio.wait_for(asyncio.shield(owner), timeout=5)
        monkeypatch.setattr(service.database, "add_alert", original)
    try:
        if not restart_early:
            await asyncio.wait_for(service.start(), timeout=5)
        await settle(service)
        assert service.database.get_alert("synthetic").occurrence_count == 1
        await write(service, observation)
        await settle(service)
        assert service.database.get_alert("synthetic").occurrence_count == 2
        assert not service._pending_alerts
    finally:
        await asyncio.wait_for(service.stop(), timeout=1)


async def test_cancelled_first_write_keeps_a_genuinely_new_occurrence(service, monkeypatch):
    observation = alert(rule_id="collection_failure", condition_active=True)
    service._remember_live_alert(observation)
    held = Gate(service.database.add_alert)
    monkeypatch.setattr(service.database, "add_alert", held)
    action = asyncio.create_task(service._persist_alert(observation))
    try:
        await held.wait()
        action.cancel()
        with pytest.raises(asyncio.CancelledError):
            await action
        await write(service, observation)
    finally:
        held.release.set()
        owner = service._alert_persistence_task
        if owner is not None:
            await asyncio.wait_for(asyncio.shield(owner), timeout=5)
    await settle(service)
    assert service.database.get_alert("synthetic").occurrence_count == 2
    assert service._live_alert("synthetic").occurrence_count == 2
    assert not service._pending_alerts


async def test_cancelled_operator_patch_preserves_a_newer_reversal(service, monkeypatch):
    await write(service, alert(rule_id="collection_failure", condition_active=True))
    held = Gate(service.database.update_alert)
    monkeypatch.setattr(service.database, "update_alert", held)
    action = asyncio.create_task(service.update_alert("synthetic", AlertUpdate(acknowledged=True)))
    try:
        await held.wait()
        action.cancel()
        with pytest.raises(asyncio.CancelledError):
            await action
        await service.update_alert("synthetic", AlertUpdate(acknowledged=False, dismissed=True))
    finally:
        held.release.set()
        owner = service._alert_persistence_task
        if owner is not None:
            await asyncio.wait_for(asyncio.shield(owner), timeout=5)
    await settle(service)
    row = service.database.get_alert("synthetic")
    assert (row.acknowledged, row.dismissed, row.occurrence_count) == (False, True, 1)
    assert not service._pending_alert_updates
