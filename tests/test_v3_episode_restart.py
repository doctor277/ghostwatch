"""Threshold episode identity across restarts and abrupt process loss.

Contract (docs/V3_RESILIENCE_AUDIT.md, "Restart blocker corrections"): a stored open
episode is continued after a restart only when the previous run stopped cleanly and
recorded it as still open. Otherwise its continuity cannot be proven: the first
readable sample marks it ended as "not confirmed" without inventing a recovery time or
evidence, and a breach is a new episode. Turning logging back on closes episodes that
ended during the opt-out in the same transaction that saves the setting.

Process loss is simulated by abandoning a service without stop() or any further flush.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading

import pytest

from app.models import Alert, AlertUpdate, IncidentCreate
from app.service import StorageUnavailableError
from app.storage.database import Database
from tests.test_v3_alert_lifecycle import (
    BREACH,
    CLEAR,
    at,
    memory_events,
    memory_rows,
    outage,
    run,
    settle,
)
from tests.test_v3_logging_opt_out import (
    break_storage,
    mend_storage,
    open_stored_episode,
    raw_alert_row,
    restarted,
    set_logging,
)


async def with_operator_history(service, opened):
    """Acknowledge and link the episode, returning its stored row and incident."""
    await service.update_alert(opened.id, AlertUpdate(acknowledged=True))
    incident = await service.create_incident(IncidentCreate(
        title="Synthetic episode", severity="CRITICAL", alert_ids=[opened.id]
    ))
    return service.database.get_alert(opened.id), incident


def session_record(path):
    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT value FROM schema_meta WHERE key = 'monitoring_session'"
        ).fetchone()
    return json.loads(row[0]) if row is not None else None


def assert_closed_unconfirmed(row, stored):
    # Only the state and the explanation change. The recovery that was never stored
    # is not re-invented: no recovery time, and the evidence is the episode's own.
    assert row.condition_active is False
    assert row.last_recovery is None
    assert "not confirmed" in row.explanation
    assert row == stored.model_copy(
        update={"condition_active": False, "explanation": row.explanation}
    )


def assert_new_episode(service, rows, prior_id, first_seen):
    [new_id] = set(rows) - {prior_id}
    new = rows[new_id]
    assert (new.condition_active, new.first_occurrence, new.occurrence_count) == (
        True, first_seen, 1
    )
    assert (new.acknowledged, new.incident_id) == (False, None)
    assert service.detection_engine.active_episode_id("memory") == new_id
    return new_id


# --- Blocker 1: re-enabling logging ------------------------------------------------


async def test_crash_right_after_reenabling_logging_does_not_resume_an_ended_episode(
    tmp_path, system_sample, network_sample
):
    service, clock, opened = await open_stored_episode(tmp_path, system_sample, network_sample)
    stored, incident = await with_operator_history(service, opened)
    [original_event] = memory_events(service)
    await set_logging(service, False)
    system_sample.memory_percent = CLEAR
    await run(service, clock, 72, 80)  # Ends while logging is off.

    await set_logging(service, True)
    # Durable before success is reported: no flush, settle or stop has run.
    ended = stored.model_copy(update={"condition_active": False})
    assert Database(service.database.path).get_alert(opened.id) == ended

    # Process loss immediately after the setting returned.
    system_sample.memory_percent = BREACH
    again = await restarted(service, clock, system_sample, network_sample)
    await run(again, clock, 200, 240)
    await settle(again)

    rows = {row.id: row for row in memory_rows(again)}
    assert rows[opened.id] == ended
    new_id = assert_new_episode(again, rows, opened.id, at(210))
    assert again.database.get_incident(incident.id).related_alerts == [opened.id]
    events = memory_events(again)
    assert original_event in events and len(events) == 2
    assert {event.related_id for event in events} == {opened.id, new_id}


async def test_reenabling_logging_while_storage_is_unavailable_fails_closed(
    tmp_path, system_sample, network_sample, monkeypatch
):
    service, clock, opened = await open_stored_episode(tmp_path, system_sample, network_sample)
    path = tmp_path / "synthetic.db"
    await set_logging(service, False)
    system_sample.memory_percent = CLEAR
    await run(service, clock, 72, 80)
    before = raw_alert_row(path, opened.id)

    break_storage(service, monkeypatch)
    with pytest.raises(StorageUnavailableError):
        await set_logging(service, True)

    # The opt-out stays in force in memory and on disk, and nothing was written.
    assert service.settings.event_logging_enabled is False
    assert Database(path).load_settings().event_logging_enabled is False
    await run(service, clock, 80, 84)
    await settle(service)
    assert raw_alert_row(path, opened.id) == before
    assert service.storage.pending_writes == 0

    mend_storage(service, monkeypatch)
    await set_logging(service, True)
    assert Database(path).get_alert(opened.id) == opened.model_copy(
        update={"condition_active": False}
    )


def test_reenabling_logging_and_closing_episodes_commit_together(tmp_path):
    database = Database(tmp_path / "synthetic.db")
    database.initialize()
    episode = Alert(
        id="synthetic-memory-episode", timestamp=at(70), severity="CRITICAL",
        source="System", metric="Memory 95.0%", explanation="synthetic", rule_id="memory",
        first_occurrence=at(10), last_occurrence=at(70), occurrence_count=2,
        condition_active=True,
    )
    database.add_alert(episode, 50)
    disabled = database.load_settings().model_copy(update={"event_logging_enabled": False})
    database.save_settings(disabled)
    with sqlite3.connect(database.path) as connection:
        connection.execute(
            "CREATE TRIGGER synthetic_failure BEFORE UPDATE ON settings "
            "BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END"
        )

    enabled = disabled.model_copy(update={"event_logging_enabled": True})
    with pytest.raises(sqlite3.DatabaseError):
        database.save_settings(enabled, still_open=())

    # The settings write failed after the episode update: both rolled back.
    assert database.get_alert(episode.id).condition_active is True
    assert database.load_settings().event_logging_enabled is False


async def test_reenabling_logging_waits_for_a_collection_in_progress(
    tmp_path, system_sample, network_sample, monkeypatch
):
    # A recovery evaluated while the setting is being saved must not slip between the
    # snapshot of open episodes and the commit, leaving its stored row open.
    service, clock, opened = await open_stored_episode(tmp_path, system_sample, network_sample)
    await set_logging(service, False)
    system_sample.memory_percent = CLEAR
    loop = asyncio.get_running_loop()
    entered, released = asyncio.Event(), threading.Event()
    collect = service.system_collector.collect

    def held_collect(*args):
        loop.call_soon_threadsafe(entered.set)
        assert released.wait(10)
        return collect(*args)

    monkeypatch.setattr(service.system_collector, "collect", held_collect)
    clock.value = 72.0
    collection = asyncio.create_task(service.collect_once())
    await asyncio.wait_for(entered.wait(), timeout=5)
    enabling = asyncio.create_task(set_logging(service, True))
    await asyncio.sleep(0.05)
    assert not enabling.done()
    released.set()
    await collection
    await enabling

    assert service.detection_engine.active_episode_id("memory") is None
    assert service.database.get_alert(opened.id).condition_active is False


# --- Blocker 2: transitions queued in memory during an outage ----------------------


@pytest.mark.parametrize("breached_at_restart", [False, True], ids=["clear", "breached"])
async def test_crash_after_recovery_queued_in_an_outage_does_not_resume_the_episode(
    tmp_path, system_sample, network_sample, monkeypatch, breached_at_restart
):
    service, clock, opened = await open_stored_episode(tmp_path, system_sample, network_sample)
    stored, incident = await with_operator_history(service, opened)
    [original_event] = memory_events(service)
    break_storage(service, monkeypatch)
    system_sample.memory_percent = CLEAR
    await run(service, clock, 72, 76)
    assert service._pending_alerts  # The recovery exists only in memory.

    # Process loss before the queue is replayed.
    system_sample.memory_percent = BREACH if breached_at_restart else CLEAR
    again = await restarted(service, clock, system_sample, network_sample)
    await run(again, clock, 200, 240)
    await settle(again)

    rows = {row.id: row for row in memory_rows(again)}
    assert_closed_unconfirmed(rows[opened.id], stored)
    assert again._live_alert(opened.id).last_recovery is None
    assert again.database.get_incident(incident.id).related_alerts == [opened.id]
    if breached_at_restart:
        new_id = assert_new_episode(again, rows, opened.id, at(210))
        events = memory_events(again)
        assert original_event in events and len(events) == 2
        assert {event.related_id for event in events} == {opened.id, new_id}
    else:
        assert set(rows) == {opened.id}
        assert again.detection_engine.active_episode_id("memory") is None
        assert memory_events(again) == [original_event]
    assert again.storage.pending_writes == 0


@pytest.mark.parametrize("stopped_cleanly", [True, False], ids=["clean_stop", "process_loss"])
async def test_continuous_breach_keeps_its_identity_only_after_a_clean_stop(
    tmp_path, system_sample, network_sample, stopped_cleanly
):
    service, clock, opened = await open_stored_episode(tmp_path, system_sample, network_sample)
    stored, incident = await with_operator_history(service, opened)
    [original_event] = memory_events(service)
    if stopped_cleanly:
        await service.stop()

    again = await restarted(service, clock, system_sample, network_sample)
    await run(again, clock, 200, 212)
    await settle(again)

    rows = {row.id: row for row in memory_rows(again)}
    if stopped_cleanly:
        [row] = rows.values()
        assert (row.id, row.condition_active, row.occurrence_count) == (
            opened.id, True, stored.occurrence_count + 1
        )
        assert (row.first_occurrence, row.last_occurrence) == (stored.first_occurrence, at(210))
        assert (row.acknowledged, row.incident_id) == (True, incident.id)
        assert memory_events(again) == [original_event]
    else:
        assert_closed_unconfirmed(rows[opened.id], stored)
        assert_new_episode(again, rows, opened.id, at(210))
        assert len(memory_events(again)) == 2


async def test_clean_stop_that_cannot_record_itself_is_treated_as_process_loss(
    tmp_path, system_sample, network_sample, monkeypatch
):
    service, clock, opened = await open_stored_episode(tmp_path, system_sample, network_sample)
    monkeypatch.setattr(service.database, "end_monitoring_session", outage)
    await service.stop()
    assert session_record(tmp_path / "synthetic.db") == {"state": "running"}

    again = await restarted(service, clock, system_sample, network_sample)
    await run(again, clock, 200, 212)
    await settle(again)

    rows = {row.id: row for row in memory_rows(again)}
    assert rows[opened.id].condition_active is False
    assert_new_episode(again, rows, opened.id, at(210))


async def test_clean_stop_after_a_queued_recovery_does_not_resume_the_episode(
    tmp_path, system_sample, network_sample, monkeypatch
):
    service, clock, opened = await open_stored_episode(tmp_path, system_sample, network_sample)
    stored = service.database.get_alert(opened.id)
    break_storage(service, monkeypatch)
    system_sample.memory_percent = CLEAR
    await run(service, clock, 72, 76)
    mend_storage(service, monkeypatch)
    # The recovery is still queued; only the episodes open live are recorded.
    await service.stop()
    assert session_record(tmp_path / "synthetic.db") == {
        "state": "stopped", "open_episodes": []
    }

    system_sample.memory_percent = BREACH
    again = await restarted(service, clock, system_sample, network_sample)
    await run(again, clock, 200, 212)
    await settle(again)

    rows = {row.id: row for row in memory_rows(again)}
    assert_closed_unconfirmed(rows[opened.id], stored)
    assert_new_episode(again, rows, opened.id, at(210))


async def test_unreadable_first_sample_keeps_an_unconfirmed_episode_unresolved(
    tmp_path, system_sample, network_sample
):
    service, clock, opened = await open_stored_episode(tmp_path, system_sample, network_sample)
    stored = service.database.get_alert(opened.id)

    system_sample.unavailable_metrics = ["memory_percent"]
    again = await restarted(service, clock, system_sample, network_sample)
    await run(again, clock, 200, 212)
    await settle(again)
    # No reading, so nothing is concluded and nothing new is linked to it.
    assert again.database.get_alert(opened.id) == stored
    assert again.detection_engine.active_episode_id("memory") is None

    system_sample.unavailable_metrics = []
    await run(again, clock, 212, 214)
    await settle(again)
    assert_closed_unconfirmed(again.database.get_alert(opened.id), stored)


async def test_session_record_holds_no_episode_from_a_logging_opt_out(
    tmp_path, system_sample, network_sample
):
    service, clock, opened = await open_stored_episode(tmp_path, system_sample, network_sample)
    await set_logging(service, False)
    system_sample.memory_percent = CLEAR
    await run(service, clock, 72, 76)
    system_sample.memory_percent = BREACH
    await run(service, clock, 76, 100)  # A live-only episode opens during the opt-out.
    assert service.detection_engine.active_episode_id("memory") not in (None, opened.id)

    await service.stop()
    assert session_record(tmp_path / "synthetic.db") == {
        "state": "stopped", "open_episodes": []
    }


async def test_restart_within_the_process_marks_the_session_running_again(
    tmp_path, system_sample, network_sample, monkeypatch
):
    service, clock, opened = await open_stored_episode(tmp_path, system_sample, network_sample)
    path = tmp_path / "synthetic.db"
    await service.stop()
    assert session_record(path) == {"state": "stopped", "open_episodes": [opened.id]}

    async def idle():
        await service._stop.wait()

    for worker in ("_history_writer", "_windows_event_loop", "_file_integrity_loop", "_run"):
        monkeypatch.setattr(service, worker, idle)
    await service.start()
    # A process loss from here on must not reuse the previous clean stop.
    assert session_record(path) == {"state": "running"}
    await service.stop()
    assert session_record(path) == {"state": "stopped", "open_episodes": [opened.id]}


@pytest.mark.parametrize("value", ["not json", "[]", '{"state": "stopped"}',
                                   '{"state": "stopped", "open_episodes": [1]}'])
def test_malformed_session_record_resumes_nothing(tmp_path, value):
    database = Database(tmp_path / "synthetic.db")
    database.initialize()
    with sqlite3.connect(database.path) as connection:
        connection.execute(
            "INSERT INTO schema_meta (key, value) VALUES ('monitoring_session', ?)", (value,)
        )
    assert database.begin_monitoring_session() == frozenset()
    assert session_record(database.path) == {"state": "running"}
