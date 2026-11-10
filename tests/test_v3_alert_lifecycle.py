from __future__ import annotations

import asyncio
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from app.detection.engine import DetectionEngine
from app.models import (
    Alert,
    AlertUpdate,
    DetectionSettings,
    FileIntegrityEvent,
    IncidentCreate,
    IncidentUpdate,
    Settings,
    WindowsEventRecord,
)
from app.service import TelemetryService
from app.storage.database import Database

# Defaults: 90% memory threshold, 10 s sustained duration, 60 s cooldown, 2 s refresh.
BREACH = 95.0
CLEAR = 40.0
CHANGES = ["acknowledged", "dismissed"]
NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


class Clock:
    def __init__(self, start=0.0):
        self.value = start

    def __call__(self):
        return self.value

    def wall(self):
        return NOW + timedelta(seconds=self.value)


def at(seconds):
    return NOW + timedelta(seconds=seconds)


def engine(clock):
    return DetectionEngine(clock=clock, wall_clock=clock.wall)


class Static:
    def __init__(self, value):
        self.value = value
        self.connection_counts_by_pid = {}

    def collect(self, *args, **kwargs):
        return self.value


def sweep(engine, clock, system, network, start, stop):
    """Evaluate once per 2 s refresh over [start, stop) and return every alert."""
    emitted = []
    for second in range(start, stop, 2):
        clock.value = float(second)
        emitted.extend(engine.evaluate(system, network, Settings()))
    return emitted


async def monitored(path, clock, system, network):
    service = TelemetryService(Database(path), storage_retry_seconds=0)
    service.detection_engine = engine(clock)
    assert await service.initialize_storage()
    service.system_collector = Static(system)
    service.network_collector = Static((network, []))
    service.process_collector = Static([])
    return service


async def run(service, clock, start, stop):
    for second in range(start, stop, 2):
        clock.value = float(second)
        await service.collect_once()


def memory_rows(service):
    return [
        item
        for item in service.database.list_alerts(1000, include_dismissed=True)
        if item.rule_id == "memory"
    ]


def memory_events(service):
    return [
        item
        for item in service.database.list_events(1000, event_type="alert")
        if item.title.startswith("Memory")
    ]


def outage(*args):
    raise sqlite3.OperationalError("synthetic outage")


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


async def settle(service, passes=3):
    for _ in range(passes):
        await service._maybe_recover_storage()


async def observe(service, alert):
    service._remember_live_alert(alert)
    await service._persist_alert(alert)


# Detection engine


def test_sustained_breach_reports_one_alert_identity(system_sample, network_sample):
    clock = Clock()
    system_sample.memory_percent = BREACH

    emitted = sweep(engine(clock), clock, system_sample, network_sample, 0, 302)

    # Sustained at 10 s, then re-confirmed once per cooldown.
    assert [item.last_occurrence for item in emitted] == [
        at(10), at(70), at(130), at(190), at(250)
    ]
    assert len({item.id for item in emitted}) == 1
    assert {item.first_occurrence for item in emitted} == {at(10)}
    assert all(item.condition_active is True for item in emitted)
    assert all(item.occurrence_count == 1 for item in emitted)


def test_each_rule_keeps_its_own_alert(system_sample, network_sample):
    clock = Clock()
    system_sample.cpu_percent = BREACH
    system_sample.memory_percent = BREACH

    emitted = sweep(engine(clock), clock, system_sample, network_sample, 0, 12)

    assert sorted(item.rule_id for item in emitted) == ["cpu", "memory"]
    assert len({item.id for item in emitted}) == 2


def test_clear_sample_records_one_recovery(system_sample, network_sample):
    clock = Clock()
    detector = engine(clock)
    system_sample.memory_percent = BREACH
    [opened] = sweep(detector, clock, system_sample, network_sample, 0, 12)

    system_sample.memory_percent = CLEAR
    recovered = sweep(detector, clock, system_sample, network_sample, 12, 30)

    assert len(recovered) == 1
    assert recovered[0].id == opened.id
    assert recovered[0].condition_active is False
    assert recovered[0].occurrence_count == 0
    assert recovered[0].last_recovery == at(12)
    assert recovered[0].evidence["observed_percent"] == CLEAR


def test_unreadable_metric_is_not_a_recovery(system_sample, network_sample):
    clock = Clock()
    detector = engine(clock)
    system_sample.memory_percent = BREACH
    [opened] = sweep(detector, clock, system_sample, network_sample, 0, 12)

    system_sample.memory_percent = 0.0
    system_sample.unavailable_metrics = ["memory_percent"]
    assert sweep(detector, clock, system_sample, network_sample, 12, 30) == []

    system_sample.memory_percent = CLEAR
    system_sample.unavailable_metrics = []
    recovered = sweep(detector, clock, system_sample, network_sample, 30, 32)
    assert [(item.id, item.condition_active) for item in recovered] == [(opened.id, False)]


def test_new_episode_opens_inside_cooldown_but_repeats_are_throttled(system_sample, network_sample):
    clock = Clock()
    detector = engine(clock)
    system_sample.memory_percent = BREACH
    [opened] = sweep(detector, clock, system_sample, network_sample, 0, 12)
    system_sample.memory_percent = CLEAR
    sweep(detector, clock, system_sample, network_sample, 12, 14)

    system_sample.memory_percent = BREACH
    # The new episode qualifies at 24 s; its next repeated report is at 84 s.
    [recurred] = sweep(detector, clock, system_sample, network_sample, 14, 84)
    assert recurred.id != opened.id
    assert recurred.condition_active is True
    assert recurred.occurrence_count == 1
    assert recurred.first_occurrence == at(24)
    [repeated] = sweep(detector, clock, system_sample, network_sample, 84, 86)
    assert repeated.id == recurred.id
    assert repeated.first_occurrence == at(24)
    assert repeated.last_occurrence == at(84)


def test_restored_active_alert_recovers_on_a_clear_sample(system_sample, network_sample):
    clock = Clock()
    system_sample.memory_percent = BREACH
    [opened] = sweep(engine(clock), clock, system_sample, network_sample, 0, 12)
    system_sample.memory_percent = CLEAR

    restarted = engine(Clock(1000.0))
    restarted.restore_active([opened, opened.model_copy(update={"rule_id": "unrelated"})])
    recovered = restarted.evaluate(system_sample, network_sample, Settings())

    assert [(item.id, item.condition_active) for item in recovered] == [(opened.id, False)]
    assert recovered[0].last_recovery == at(1000)
    assert engine(Clock()).evaluate(system_sample, network_sample, Settings()) == []


# Service and storage


async def test_sustained_breach_keeps_one_stored_alert(tmp_path, system_sample, network_sample):
    clock = Clock()
    system_sample.memory_percent = BREACH
    service = await monitored(tmp_path / "synthetic.db", clock, system_sample, network_sample)
    await run(service, clock, 0, 12)
    [opened] = memory_rows(service)

    await run(service, clock, 12, 302)

    [row] = memory_rows(service)
    assert row.id == opened.id
    assert row.occurrence_count == 5
    assert row.condition_active is True
    assert (row.first_occurrence, row.last_occurrence) == (at(10), at(250))
    assert len(memory_events(service)) == 1
    [live] = [item for item in service.latest.alerts if item.rule_id == "memory"]
    assert (live.id, live.occurrence_count) == (row.id, 5)


@pytest.mark.parametrize("change", CHANGES)
async def test_operator_state_survives_a_sustained_breach(
    tmp_path, system_sample, network_sample, change
):
    clock = Clock()
    system_sample.memory_percent = BREACH
    service = await monitored(tmp_path / "synthetic.db", clock, system_sample, network_sample)
    await run(service, clock, 0, 12)
    [opened] = memory_rows(service)
    await service.update_alert(opened.id, AlertUpdate(**{change: True}))

    await run(service, clock, 12, 132)

    [row] = memory_rows(service)
    assert row.occurrence_count == 3
    assert getattr(row, change) is True
    [live] = [
        item
        for item in await service.list_alerts(50, include_dismissed=True)
        if item.rule_id == "memory"
    ]
    assert getattr(live, change) is True


async def test_clear_threshold_records_recovery_on_the_stored_alert(
    tmp_path, system_sample, network_sample
):
    clock = Clock()
    system_sample.memory_percent = BREACH
    service = await monitored(tmp_path / "synthetic.db", clock, system_sample, network_sample)
    await run(service, clock, 0, 12)
    [opened] = memory_rows(service)
    await service.update_alert(opened.id, AlertUpdate(acknowledged=True))
    await run(service, clock, 12, 72)

    system_sample.memory_percent = CLEAR
    await run(service, clock, 72, 90)

    [row] = memory_rows(service)
    assert row.condition_active is False
    assert row.last_recovery == at(72)
    assert row.occurrence_count == 2
    assert row.acknowledged is True
    assert (row.first_occurrence, row.last_occurrence) == (at(10), at(70))
    # The recovery updates the alert; it is not a second detection in the timeline.
    assert len(memory_events(service)) == 1


async def test_breach_after_recovery_preserves_the_previous_episode(
    tmp_path, system_sample, network_sample
):
    clock = Clock()
    system_sample.memory_percent = BREACH
    service = await monitored(tmp_path / "synthetic.db", clock, system_sample, network_sample)
    await run(service, clock, 0, 12)
    [opened] = memory_rows(service)
    await service.update_alert(opened.id, AlertUpdate(acknowledged=True, dismissed=True))
    system_sample.memory_percent = CLEAR
    await run(service, clock, 12, 20)

    system_sample.memory_percent = BREACH
    await run(service, clock, 20, 72)

    [row] = [item for item in memory_rows(service) if item.condition_active is True]
    assert row.id != opened.id
    assert row.condition_active is True
    assert row.occurrence_count == 1
    assert (row.acknowledged, row.dismissed) == (False, False)
    assert (row.first_occurrence, row.last_occurrence) == (at(30), at(30))
    assert row.last_recovery is None
    old = service.database.get_alert(opened.id)
    assert old.condition_active is False
    assert old.occurrence_count == 1
    assert (old.acknowledged, old.dismissed) == (True, True)
    assert old.last_recovery == at(12)
    # One timeline entry per breach.
    assert len(memory_events(service)) == 2


async def test_restart_continues_an_active_breach(tmp_path, system_sample, network_sample):
    path = tmp_path / "synthetic.db"
    system_sample.memory_percent = BREACH
    clock = Clock()
    first = await monitored(path, clock, system_sample, network_sample)
    await run(first, clock, 0, 12)
    [opened] = memory_rows(first)
    await first.update_alert(opened.id, AlertUpdate(acknowledged=True))

    clock = Clock(1000.0)
    restarted = await monitored(path, clock, system_sample, network_sample)
    await run(restarted, clock, 1000, 1012)

    [row] = memory_rows(restarted)
    assert row.id == opened.id
    assert row.occurrence_count == 2
    assert row.condition_active is True
    assert row.acknowledged is True
    assert (row.first_occurrence, row.last_occurrence) == (at(10), at(1010))


async def test_restart_records_a_recovery_missed_while_stopped(
    tmp_path, system_sample, network_sample
):
    path = tmp_path / "synthetic.db"
    system_sample.memory_percent = BREACH
    clock = Clock()
    first = await monitored(path, clock, system_sample, network_sample)
    await run(first, clock, 0, 12)
    [opened] = memory_rows(first)

    system_sample.memory_percent = CLEAR
    restarted = await monitored(path, Clock(1000.0), system_sample, network_sample)
    await restarted.collect_once()

    [row] = memory_rows(restarted)
    assert row.id == opened.id
    assert row.condition_active is False
    assert row.last_recovery == at(1000)
    assert row.occurrence_count == 1


async def test_observations_during_an_outage_merge_into_one_alert(
    tmp_path, system_sample, network_sample, monkeypatch
):
    clock = Clock()
    system_sample.memory_percent = BREACH
    service = await monitored(tmp_path / "synthetic.db", clock, system_sample, network_sample)
    await run(service, clock, 0, 12)

    with monkeypatch.context() as patch:
        patch.setattr(service.database, "add_alert", outage)
        # Observations at 70 s and 130 s wait in the bounded queue.
        await run(service, clock, 12, 132)
        assert service.storage.status == "degraded"
    await run(service, clock, 132, 134)

    [row] = memory_rows(service)
    assert row.occurrence_count == 3
    assert (row.first_occurrence, row.last_occurrence) == (at(10), at(130))
    assert row.condition_active is True
    assert service.storage.pending_writes == 0


def test_threshold_count_is_atomic_across_database_instances(
    tmp_path, system_sample, network_sample
):
    system_sample.memory_percent = BREACH
    clock = Clock()
    [observation] = sweep(engine(clock), clock, system_sample, network_sample, 0, 12)
    path = tmp_path / "synthetic.db"
    database = Database(path)
    database.initialize()
    instances = [Database(path) for _ in range(4)]
    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(lambda index: instances[index % 4].add_alert(observation, 50), range(40)))

    [row] = database.list_alerts(100, include_dismissed=True)
    assert row.occurrence_count == 40
    assert (row.first_occurrence, row.last_occurrence) == (at(10), at(10))


def test_threshold_update_rolls_back_with_retention_failure(
    tmp_path, system_sample, network_sample, monkeypatch
):
    system_sample.memory_percent = BREACH
    clock = Clock()
    first, second = sweep(engine(clock), clock, system_sample, network_sample, 0, 72)
    database = Database(tmp_path / "synthetic.db")
    database.initialize()
    stored = database.add_alert(first, 50)
    with monkeypatch.context() as patch:
        patch.setattr(database, "_enforce_retention", outage)
        with pytest.raises(sqlite3.OperationalError):
            database.add_alert(second, 50)

    assert database.get_alert(first.id) == stored
    with database._connect() as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


@pytest.mark.parametrize(
    ("rule", "field", "breached", "clear"),
    [
        ("cpu", "cpu_percent", 95, 20),
        ("memory", "memory_percent", 95, 40),
        ("bandwidth", "upload_mbps", 20, 0),
        ("connections", "established_connections", 1000, 5),
        ("process_count", "process_count", 1000, 120),
        ("disk_space", "disk_percent", 95, 30),
        ("destination_diversity", "unique_remote_destinations", 1000, 0),
        ("connection_spike", "established_connections", 100, 5),
    ],
)
def test_each_threshold_preserves_episode_across_unavailable_measurements(
    system_sample, network_sample, rule, field, breached, clear
):
    clock = Clock()
    detector = engine(clock)
    settings = Settings(threshold_duration=2)
    detection = DetectionSettings(enabled_rules={
        key: key == rule for key in DetectionSettings().enabled_rules
    })
    sample = system_sample if field in {
        "cpu_percent", "memory_percent", "process_count", "disk_percent"
    } else network_sample
    if rule == "connection_spike":
        detector.evaluate(system_sample, network_sample, settings, detection)
    setattr(sample, field, breached)
    opened = detector.evaluate(system_sample, network_sample, settings, detection)
    if rule != "connection_spike":
        clock.value = 2
        opened = detector.evaluate(system_sample, network_sample, settings, detection)
    [opened] = opened

    sample.unavailable_metrics = [field]
    setattr(sample, field, None if field == "disk_percent" else clear)
    clock.value = 4
    assert detector.evaluate(system_sample, network_sample, settings, detection) == []
    sample.unavailable_metrics = []
    setattr(sample, field, clear)
    clock.value = 6
    [recovered] = detector.evaluate(system_sample, network_sample, settings, detection)
    assert recovered.id == opened.id
    assert recovered.condition_active is False
    assert recovered.last_recovery == at(6)


async def test_recovery_during_a_failed_first_write_keeps_its_order(
    tmp_path, system_sample, network_sample, monkeypatch
):
    clock = Clock()
    detector = engine(clock)
    system_sample.memory_percent = BREACH
    [opened] = sweep(detector, clock, system_sample, network_sample, 0, 12)
    system_sample.memory_percent = CLEAR
    [recovered] = sweep(detector, clock, system_sample, network_sample, 12, 14)
    service = TelemetryService(Database(tmp_path / "synthetic.db"), storage_retry_seconds=0)
    assert await service.initialize_storage()

    with monkeypatch.context() as patch:
        held = HeldWrite(outage)
        patch.setattr(service.database, "add_alert", held)
        service._remember_live_alert(opened)
        write = asyncio.create_task(service._persist_alert(opened))
        await asyncio.wait_for(held.entered.wait(), timeout=5)
        await observe(service, recovered)
        held.released.set()
        await write
    await settle(service)

    row = service.database.get_alert(opened.id)
    assert row.condition_active is False
    assert row.occurrence_count == 1
    assert (row.first_occurrence, row.last_recovery) == (at(10), at(12))


async def test_observation_during_a_flush_is_counted_once(
    tmp_path, system_sample, network_sample, monkeypatch
):
    clock = Clock()
    system_sample.memory_percent = BREACH
    first, second = sweep(engine(clock), clock, system_sample, network_sample, 0, 72)
    service = TelemetryService(Database(tmp_path / "synthetic.db"), storage_retry_seconds=0)
    assert await service.initialize_storage()
    real_add_alert = service.database.add_alert

    with monkeypatch.context() as patch:
        patch.setattr(service.database, "add_alert", outage)
        await observe(service, first)
        held = HeldWrite(real_add_alert)
        patch.setattr(service.database, "add_alert", held)
        flush = asyncio.create_task(service._maybe_recover_storage())
        await asyncio.wait_for(held.entered.wait(), timeout=5)
        await observe(service, second)
        held.released.set()
        await flush

    # The write held the first observation; the second is still queued.
    assert service.database.get_alert(first.id).occurrence_count == 1
    [live] = [item for item in service.live_alerts if item.id == first.id]
    assert live.occurrence_count == 2
    await settle(service)
    assert service.database.get_alert(first.id).occurrence_count == 2
    assert service.storage.pending_writes == 0


async def test_operator_state_queued_for_old_episode_does_not_hide_new_episode(
    tmp_path, system_sample, network_sample, monkeypatch
):
    clock = Clock()
    system_sample.memory_percent = BREACH
    service = await monitored(tmp_path / "synthetic.db", clock, system_sample, network_sample)
    await run(service, clock, 0, 12)
    system_sample.memory_percent = CLEAR
    await run(service, clock, 12, 14)
    [recovered] = memory_rows(service)

    with monkeypatch.context() as patch:
        patch.setattr(service.database, "add_alert", outage)
        patch.setattr(service.database, "update_alert", outage)
        await service.update_alert(recovered.id, AlertUpdate(acknowledged=True, dismissed=True))
        system_sample.memory_percent = BREACH
        await run(service, clock, 14, 72)
    await run(service, clock, 72, 74)

    [row] = [item for item in memory_rows(service) if item.condition_active is True]
    assert row.id != recovered.id
    assert row.condition_active is True
    assert (row.acknowledged, row.dismissed) == (False, False)
    old = service.database.get_alert(recovered.id)
    assert old.condition_active is False
    assert (old.acknowledged, old.dismissed) == (True, True)
    assert service.storage.pending_writes == 0


async def test_live_alert_counts_a_sustained_breach_without_logging(
    tmp_path, system_sample, network_sample
):
    clock = Clock()
    system_sample.memory_percent = BREACH
    service = await monitored(tmp_path / "synthetic.db", clock, system_sample, network_sample)
    service.settings = service.settings.model_copy(update={"event_logging_enabled": False})

    await run(service, clock, 0, 302)

    [live] = [item for item in service.latest.alerts if item.rule_id == "memory"]
    assert (live.occurrence_count, live.first_occurrence, live.last_occurrence) == (
        5, at(10), at(250)
    )
    assert memory_rows(service) == []


async def test_retention_still_bounds_the_alert_table(tmp_path, system_sample, network_sample):
    clock = Clock()
    system_sample.memory_percent = BREACH
    service = await monitored(tmp_path / "synthetic.db", clock, system_sample, network_sample)
    service.settings = service.settings.model_copy(update={"alert_retention": 50})
    for index in range(60):
        service.database.add_alert(
            Alert(
                id=f"synthetic-old-{index}",
                timestamp=NOW - timedelta(days=1, seconds=index),
                severity="WARNING",
                source="synthetic-source",
                metric="synthetic-metric",
                explanation="synthetic",
            ),
            50,
        )

    await run(service, clock, 0, 302)

    rows = service.database.list_alerts(1000, include_dismissed=True)
    assert len(rows) == 50
    assert [item.occurrence_count for item in rows if item.rule_id == "memory"] == [5]


def windows_detection(identifier):
    return WindowsEventRecord(
        id=identifier,
        channel="synthetic-channel",
        record_id=1,
        timestamp=NOW,
        provider="synthetic-provider",
        event_id=1116,
        level="WARNING",
        category="defender_detection",
    )


def file_change(identifier):
    return FileIntegrityEvent(
        id=identifier,
        folder_id="synthetic-folder",
        timestamp=NOW,
        change_type="modified",
        relative_path="synthetic.txt",
    )


@pytest.mark.parametrize(
    ("correlate", "make", "rule_id"),
    [
        ("_correlate_windows_event", windows_detection, "windows_defender_detection"),
        ("_correlate_file_integrity_event", file_change, "file_integrity_modified"),
    ],
)
async def test_security_event_alerts_stay_independent(
    tmp_path, system_sample, network_sample, correlate, make, rule_id
):
    clock = Clock()
    system_sample.memory_percent = BREACH
    service = await monitored(tmp_path / "synthetic.db", clock, system_sample, network_sample)
    await run(service, clock, 0, 12)

    await getattr(service, correlate)(make("synthetic-event-1"))
    # The same security event correlated again is the same alert, not a second occurrence.
    await getattr(service, correlate)(make("synthetic-event-1"))
    await getattr(service, correlate)(make("synthetic-event-2"))
    await run(service, clock, 12, 72)

    security = [
        item
        for item in service.database.list_alerts(1000, include_dismissed=True)
        if item.rule_id == rule_id
    ]
    assert len(security) == 2
    assert [item.occurrence_count for item in security] == [1, 1]
    assert [item.condition_active for item in security] == [None, None]
    assert [item.occurrence_count for item in memory_rows(service)] == [2]


async def test_short_rebreach_inside_cooldown_is_a_complete_episode(
    tmp_path, system_sample, network_sample
):
    clock = Clock()
    system_sample.memory_percent = BREACH
    service = await monitored(tmp_path / "synthetic.db", clock, system_sample, network_sample)
    await run(service, clock, 0, 12)
    [first] = memory_rows(service)
    system_sample.memory_percent = CLEAR
    await run(service, clock, 12, 14)

    system_sample.memory_percent = BREACH
    await run(service, clock, 14, 40)
    [active] = [item for item in service.latest.alerts if item.condition_active is True]
    assert active.id != first.id
    assert (active.first_occurrence, active.last_occurrence, active.occurrence_count) == (
        at(24), at(24), 1
    )
    system_sample.memory_percent = CLEAR
    await run(service, clock, 40, 42)

    rows = sorted(memory_rows(service), key=lambda item: item.first_occurrence)
    assert len(rows) == 2
    assert [item.condition_active for item in rows] == [False, False]
    assert [item.last_recovery for item in rows] == [at(12), at(40)]
    assert [item.occurrence_count for item in rows] == [1, 1]
    assert sorted(item.timestamp for item in memory_events(service)) == [at(10), at(24)]


async def test_new_episode_cannot_rediscover_a_resolved_incident(
    tmp_path, system_sample, network_sample
):
    clock = Clock()
    system_sample.memory_percent = BREACH
    service = await monitored(tmp_path / "synthetic.db", clock, system_sample, network_sample)
    await run(service, clock, 0, 12)
    [first] = memory_rows(service)
    incident = await service.create_incident(IncidentCreate(
        title="Synthetic historical breach", severity="CRITICAL", alert_ids=[first.id]
    ))
    await service.update_incident(incident.id, IncidentUpdate(status="RESOLVED"))
    system_sample.memory_percent = CLEAR
    await run(service, clock, 12, 14)
    historical = service.database.get_alert(first.id)
    system_sample.memory_percent = 96
    await run(service, clock, 80, 92)

    [current] = [item for item in memory_rows(service) if item.condition_active is True]
    assert current.id != first.id
    assert current.incident_id is None
    assert service.database.get_alert(first.id) == historical
    assert service.database.get_alert_incident_ids([first.id, current.id]) == {
        first.id: incident.id, current.id: None
    }
    # Exercise the historical JSON lookup without the shortcut through alerts.incident_id.
    with service.database._connect() as connection:
        assert service.database._incident_for_alert(connection, first.id, None) == incident.id
        assert service.database._incident_for_alert(connection, current.id, None) is None
    resolved = service.database.get_incident(incident.id)
    assert resolved.status == "RESOLVED"
    assert resolved.related_alerts == [first.id]


@pytest.mark.parametrize("dismissed", [False, True])
async def test_restart_recovery_without_logging_preserves_episode_metadata(
    tmp_path, system_sample, network_sample, dismissed
):
    path = tmp_path / "synthetic.db"
    clock = Clock()
    system_sample.memory_percent = BREACH
    first = await monitored(path, clock, system_sample, network_sample)
    await run(first, clock, 0, 72)
    [opened] = memory_rows(first)
    await first.update_alert(opened.id, AlertUpdate(acknowledged=True, dismissed=dismissed))
    stored = first.database.get_alert(opened.id)
    first.database.save_settings(first.settings.model_copy(update={"event_logging_enabled": False}))

    system_sample.memory_percent = CLEAR
    restarted = await monitored(path, Clock(1000), system_sample, network_sample)
    await restarted.collect_once()

    live = restarted._live_alert(opened.id)
    assert live.condition_active is False
    assert live.last_recovery == at(1000)
    assert (live.occurrence_count, live.first_occurrence, live.last_occurrence) == (
        2, at(10), at(70)
    )
    assert (live.acknowledged, live.dismissed) == (True, dismissed)
    assert (opened.id in {item.id for item in restarted.latest.alerts}) is not dismissed
    assert restarted.database.get_alert(opened.id) == stored


async def test_restart_continuing_episode_does_not_duplicate_timeline(
    tmp_path, system_sample, network_sample
):
    path = tmp_path / "synthetic.db"
    clock = Clock()
    system_sample.memory_percent = BREACH
    first = await monitored(path, clock, system_sample, network_sample)
    await run(first, clock, 0, 12)
    [original_event] = memory_events(first)

    clock = Clock(1000)
    restarted = await monitored(path, clock, system_sample, network_sample)
    await run(restarted, clock, 1000, 1072)

    assert memory_events(restarted) == [original_event]
    timeline = await restarted.list_events(event_type="alert")
    assert timeline == [original_event]
    [row] = memory_rows(restarted)
    assert row.first_occurrence == at(10)
    assert row.occurrence_count == 3


async def test_previous_beta_active_identity_is_resumed_until_recovery(
    tmp_path, system_sample, network_sample
):
    path = tmp_path / "synthetic.db"
    database = Database(path)
    database.initialize()
    # The memory ID used by bc83f51, with synthetic lifecycle data.
    legacy = Alert(
        id="c128a828-f2b9-5970-b51b-de57bb1b7729", timestamp=at(70),
        severity="CRITICAL", source="System", metric="Memory 95.0%", explanation="synthetic",
        rule_id="memory", first_occurrence=at(10), last_occurrence=at(70),
        occurrence_count=2, condition_active=True, acknowledged=True,
    )
    database.add_alert(legacy, 50)
    original_event = TelemetryService._alert_event(legacy.model_copy(update={"timestamp": at(10)}))
    database.add_event(original_event)
    system_sample.memory_percent = BREACH
    clock = Clock(1000)
    service = await monitored(path, clock, system_sample, network_sample)
    await run(service, clock, 1000, 1012)
    [continued] = memory_rows(service)
    assert continued.id == legacy.id
    assert continued.first_occurrence == at(10)
    assert continued.occurrence_count == 3
    assert continued.acknowledged is True
    assert await service.list_events(event_type="alert") == [original_event]

    system_sample.memory_percent = CLEAR
    await run(service, clock, 1012, 1014)
    system_sample.memory_percent = BREACH
    await run(service, clock, 1014, 1026)
    [new] = [item for item in memory_rows(service) if item.condition_active is True]
    assert new.id != legacy.id
    assert new.occurrence_count == 1
    assert new.first_occurrence == at(1024)
    assert service.database.get_alert(legacy.id).condition_active is False


@pytest.mark.parametrize("queued", [False, True])
async def test_retention_expired_episode_is_not_recreated_by_recovery(
    tmp_path, system_sample, network_sample, monkeypatch, queued
):
    clock = Clock()
    system_sample.memory_percent = BREACH
    service = await monitored(tmp_path / "synthetic.db", clock, system_sample, network_sample)
    service.settings = service.settings.model_copy(update={"alert_retention": 50})
    await run(service, clock, 0, 12)
    [opened] = memory_rows(service)
    for index in range(50):
        service.database.add_alert(Alert(
            id=f"synthetic-new-{index}", timestamp=at(11) + timedelta(milliseconds=index),
            severity="WARNING", source="synthetic", metric="synthetic", explanation="synthetic"
        ), 50)
    assert service.database.get_alert(opened.id) is None

    system_sample.memory_percent = CLEAR
    with monkeypatch.context() as patch:
        if queued:
            patch.setattr(service.database, "add_alert", outage)
        await run(service, clock, 12, 14)
    await settle(service)

    assert service.database.get_alert(opened.id) is None
    live = service._live_alert(opened.id)
    assert live.condition_active is False
    assert (live.occurrence_count, live.first_occurrence, live.last_occurrence) == (
        1, at(10), at(10)
    )
    assert live.last_recovery == at(12)
    assert live.persistence_pending is False
    assert len(service.database.list_alerts(1000, include_dismissed=True)) == 50
    assert len(memory_events(service)) == 1
    assert service.storage.pending_writes == 0
