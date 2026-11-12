"""Event logging opt-out and storage outages across an alert episode's lifecycle.

Contract (Settings "Enable local event logging", docs/V3_RESILIENCE_AUDIT.md): while
logging is off, nothing about alerts or events is written to SQLite; live state keeps
working. When logging is turned back on, stored episodes that ended during the opt-out
are marked ended (condition_active only), without any time, explanation or evidence
from the opt-out period.
"""

from __future__ import annotations

import sqlite3

import pytest

from app.service import TelemetryService
from app.storage.database import Database
from tests.test_v3_alert_lifecycle import (
    BREACH,
    CLEAR,
    Clock,
    Static,
    at,
    engine,
    memory_events,
    memory_rows,
    monitored,
    outage,
    run,
    settle,
)


def raw_alert_row(path, alert_id):
    with sqlite3.connect(path) as connection:
        return connection.execute("SELECT * FROM alerts WHERE id = ?", (alert_id,)).fetchone()


async def set_logging(service, enabled):
    await service.update_settings(
        service.settings.model_copy(update={"event_logging_enabled": enabled})
    )


async def open_stored_episode(tmp_path, system_sample, network_sample):
    clock = Clock()
    system_sample.memory_percent = BREACH
    service = await monitored(tmp_path / "synthetic.db", clock, system_sample, network_sample)
    await run(service, clock, 0, 72)
    await settle(service)
    [opened] = memory_rows(service)
    assert opened.condition_active is True
    return service, clock, opened


async def restarted(service, clock, system_sample, network_sample):
    again = TelemetryService(Database(service.database.path), storage_retry_seconds=0)
    again.detection_engine = engine(clock)
    again.system_collector = Static(system_sample)
    again.network_collector = Static((network_sample, []))
    again.process_collector = Static([])
    assert await again.initialize_storage()
    return again


def break_storage(service, monkeypatch):
    for name in ("add_alert", "add_event", "close_ended_episodes"):
        monkeypatch.setattr(service.database, name, outage)
    service._mark_storage_failure(sqlite3.OperationalError("synthetic outage"))


def mend_storage(service, monkeypatch):
    monkeypatch.undo()


# --- Blocker 2: logging opt-out ---------------------------------------------------


async def test_recovery_while_logging_is_off_writes_nothing(
    tmp_path, system_sample, network_sample
):
    service, clock, opened = await open_stored_episode(tmp_path, system_sample, network_sample)
    path = tmp_path / "synthetic.db"
    await set_logging(service, False)
    before = raw_alert_row(path, opened.id)

    system_sample.memory_percent = CLEAR
    await run(service, clock, 72, 80)
    await settle(service)

    live = service._live_alert(opened.id)
    assert live.condition_active is False and live.last_recovery == at(72)
    assert raw_alert_row(path, opened.id) == before
    assert service.storage.pending_writes == 0


async def test_reenabling_logging_marks_ended_episodes_without_opt_out_details(
    tmp_path, system_sample, network_sample
):
    service, clock, opened = await open_stored_episode(tmp_path, system_sample, network_sample)
    await set_logging(service, False)
    system_sample.memory_percent = CLEAR
    await run(service, clock, 72, 80)

    await set_logging(service, True)
    await settle(service)

    [stored] = memory_rows(service)
    assert stored.condition_active is False
    # Nothing observed during the opt-out reaches disk: not when or how it ended.
    assert stored == opened.model_copy(update={"condition_active": False})
    assert len(memory_events(service)) == 1
    assert service.storage.pending_writes == 0


async def test_reenabling_logging_keeps_a_still_active_episode_open(
    tmp_path, system_sample, network_sample
):
    service, clock, opened = await open_stored_episode(tmp_path, system_sample, network_sample)
    await set_logging(service, False)
    await run(service, clock, 72, 80)
    await set_logging(service, True)
    await settle(service)
    await run(service, clock, 140, 142)  # Next observation after the cooldown.
    await settle(service)

    [stored] = memory_rows(service)
    assert stored.id == opened.id and stored.condition_active is True
    assert stored.last_occurrence == at(140)


@pytest.mark.parametrize("breached_at_restart", [False, True], ids=["clear", "breached"])
async def test_restart_with_logging_off_does_not_resume_an_unconfirmed_episode(
    tmp_path, system_sample, network_sample, breached_at_restart
):
    service, clock, opened = await open_stored_episode(tmp_path, system_sample, network_sample)
    path = tmp_path / "synthetic.db"
    await set_logging(service, False)
    system_sample.memory_percent = CLEAR
    await run(service, clock, 72, 80)  # Ends while logging is off: SQLite still says ACTIVE.
    await service.stop()
    before = raw_alert_row(path, opened.id)

    system_sample.memory_percent = BREACH if breached_at_restart else CLEAR
    again = await restarted(service, clock, system_sample, network_sample)
    await run(again, clock, 200, 240)
    await settle(again)

    stale = again._live_alert(opened.id)
    assert stale.condition_active is False
    assert stale.last_recovery == at(200)
    assert "not confirmed" in stale.explanation
    live_open = again.detection_engine.active_episode_id("memory")
    if breached_at_restart:
        # A breach after restart is a new episode, not a continuation of stale state.
        assert live_open is not None and live_open != opened.id
        assert again._live_alert(live_open).first_occurrence == at(210)
    else:
        assert live_open is None
    assert raw_alert_row(path, opened.id) == before


async def test_restart_after_reenabling_does_not_resume_an_ended_episode(
    tmp_path, system_sample, network_sample
):
    service, clock, opened = await open_stored_episode(tmp_path, system_sample, network_sample)
    await set_logging(service, False)
    system_sample.memory_percent = CLEAR
    await run(service, clock, 72, 80)
    await set_logging(service, True)
    await settle(service)
    await service.stop()

    system_sample.memory_percent = BREACH
    again = await restarted(service, clock, system_sample, network_sample)
    await run(again, clock, 200, 240)
    await settle(again)

    rows = {row.id: row for row in memory_rows(again)}
    assert rows[opened.id].condition_active is False
    assert rows[opened.id].occurrence_count == opened.occurrence_count
    [new_id] = set(rows) - {opened.id}
    assert rows[new_id].first_occurrence == at(210)


async def test_reconciliation_after_reenabling_waits_out_a_storage_outage(
    tmp_path, system_sample, network_sample, monkeypatch
):
    service, clock, opened = await open_stored_episode(tmp_path, system_sample, network_sample)
    await set_logging(service, False)
    system_sample.memory_percent = CLEAR
    await run(service, clock, 72, 80)
    await set_logging(service, True)
    # update_settings succeeded; the reconciliation write is what fails.
    break_storage(service, monkeypatch)
    await run(service, clock, 80, 84)
    await settle(service)
    # The reconciliation is pending and counted, alongside queued outage events.
    assert service._close_ended_episodes is True
    assert service.storage.pending_writes >= 1
    assert memory_rows(service)[0].condition_active is True

    mend_storage(service, monkeypatch)
    await run(service, clock, 84, 88)
    await settle(service)

    [stored] = memory_rows(service)
    assert stored == opened.model_copy(update={"condition_active": False})
    assert service.storage.pending_writes == 0


async def test_opting_out_again_cancels_a_pending_reconciliation(
    tmp_path, system_sample, network_sample, monkeypatch
):
    service, clock, opened = await open_stored_episode(tmp_path, system_sample, network_sample)
    path = tmp_path / "synthetic.db"
    before = raw_alert_row(path, opened.id)
    await set_logging(service, False)
    system_sample.memory_percent = CLEAR
    await run(service, clock, 72, 80)
    await set_logging(service, True)
    break_storage(service, monkeypatch)
    await run(service, clock, 80, 84)
    mend_storage(service, monkeypatch)
    await set_logging(service, False)
    await run(service, clock, 84, 90)
    await settle(service)

    assert raw_alert_row(path, opened.id) == before
    assert service.storage.pending_writes == 0
