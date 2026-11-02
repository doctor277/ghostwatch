from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from app.detection.engine import DetectionEngine
from app.models import Alert, AlertUpdate, Settings
from app.service import TelemetryService
from app.storage.database import Database


class StaticCollector:
    def __init__(self, value):
        self.value = value
        self.connection_counts_by_pid = {}

    def collect(self, *args):
        return self.value


class StaticDetector:
    def __init__(self, alert):
        self.alert = alert

    def evaluate(self, *args):
        return [self.alert]


def make_alert(alert_id: str = "live") -> Alert:
    return Alert(
        id=alert_id,
        timestamp=datetime.now(UTC),
        severity="WARNING",
        source="test",
        metric="Live test alert",
        explanation="Regression test",
    )


def configured_service(tmp_path, system_sample, network_sample, *, logging_enabled):
    database = Database(tmp_path / "service.db")
    database.initialize()
    service = TelemetryService(database)
    service.settings = Settings(event_logging_enabled=logging_enabled)
    service.system_collector = StaticCollector(system_sample)
    service.network_collector = StaticCollector((network_sample, []))
    service.process_collector = StaticCollector([])
    service.detection_engine = StaticDetector(make_alert())
    return service, database


@pytest.mark.asyncio
async def test_live_alerts_remain_visible_when_logging_is_disabled(
    tmp_path, system_sample, network_sample
):
    service, database = configured_service(
        tmp_path, system_sample, network_sample, logging_enabled=False
    )
    snapshot = await service.collect_once()
    assert [alert.id for alert in snapshot.alerts] == ["live"]
    assert database.list_alerts() == []

    acknowledged = await service.update_alert("live", AlertUpdate(acknowledged=True))
    assert acknowledged is not None and acknowledged.acknowledged
    dismissed = await service.update_alert("live", AlertUpdate(dismissed=True))
    assert dismissed is not None and dismissed.dismissed
    assert await service.list_alerts() == []
    assert [alert.id for alert in await service.list_alerts(include_dismissed=True)] == ["live"]


@pytest.mark.asyncio
async def test_persisted_and_live_alerts_are_deduplicated(
    tmp_path, system_sample, network_sample
):
    service, database = configured_service(
        tmp_path, system_sample, network_sample, logging_enabled=True
    )
    snapshot = await service.collect_once()
    assert [alert.id for alert in snapshot.alerts] == ["live"]
    assert [alert.id for alert in database.list_alerts()] == ["live"]


@pytest.mark.asyncio
async def test_unexpected_collection_failure_produces_live_alert(
    tmp_path, system_sample, network_sample
):
    service, _ = configured_service(
        tmp_path, system_sample, network_sample, logging_enabled=False
    )

    class BrokenCollector:
        def collect(self):
            raise RuntimeError("private detail")

    service.system_collector = BrokenCollector()
    service.detection_engine = DetectionEngine(clock=lambda: 0)
    snapshot = await service.collect_once()
    assert "RuntimeError" in snapshot.system.collection_error
    assert "private detail" not in snapshot.system.collection_error
    assert any(alert.metric == "Telemetry collection degraded" for alert in snapshot.alerts)


@pytest.mark.asyncio
async def test_process_visibility_is_partial_when_native_total_exceeds_enumeration(
    tmp_path, system_sample, network_sample
):
    service, _ = configured_service(
        tmp_path, system_sample, network_sample, logging_enabled=False
    )

    class RestrictedProcessCollector(StaticCollector):
        collection_status = "healthy"
        collection_detail = ""
        enumerated_count = 5
        skipped_count = 0
        idle_excluded_count = 1

    service.process_collector = RestrictedProcessCollector([])
    snapshot = await service.collect_once()

    assert snapshot.system.process_count == 120
    assert snapshot.processes_enumerated == 5
    assert snapshot.process_collection_status == "partial"
    assert "5 enumerated of 120 OS processes" in snapshot.process_collection_detail


@pytest.mark.asyncio
async def test_websocket_registration_limit_and_send_timeout(tmp_path, snapshot):
    service = TelemetryService(Database(tmp_path / "service.db"), max_clients=1, send_timeout=0.001)
    first = object()
    second = object()
    assert await service.register_client(first)
    assert not await service.register_client(second)
    await service.unregister_client(first)
    assert await service.register_client(second)

    class SlowClient:
        async def send_text(self, payload):
            await asyncio.sleep(0.05)

    assert not await service.send_snapshot(SlowClient(), snapshot)


@pytest.mark.asyncio
async def test_lowering_retention_prunes_sqlite_immediately(tmp_path):
    database = Database(tmp_path / "service.db")
    database.initialize()
    for index in range(55):
        database.add_alert(make_alert(str(index)), retention=1000)
    service = TelemetryService(database)
    await service.update_settings(Settings(alert_retention=50))
    assert len(database.list_alerts(limit=100)) == 50


def test_live_alert_memory_is_bounded(tmp_path):
    service = TelemetryService(Database(tmp_path / "service.db"))
    for index in range(250):
        service._remember_live_alert(make_alert(str(index)))
    assert len(service.live_alerts) == 200
    assert service.live_alerts[0].id == "249"
    assert service.live_alerts[-1].id == "50"


@pytest.mark.asyncio
@pytest.mark.parametrize("logging_enabled", [False, True])
async def test_collection_failure_grouping_is_bounded_with_or_without_logging(
    tmp_path, system_sample, network_sample, logging_enabled
):
    service, database = configured_service(
        tmp_path, system_sample, network_sample, logging_enabled=logging_enabled
    )
    network_sample.collection_error = "Synthetic provider failure"
    service.detection_engine = DetectionEngine(clock=lambda: 0)

    for _ in range(5):
        snapshot = await service.collect_once()

    grouped = [item for item in snapshot.alerts if item.rule_id == "collection_failure"]
    assert len(grouped) == 1
    assert grouped[0].occurrence_count == 5
    assert grouped[0].condition_active is True
    if logging_enabled:
        persisted = database.get_alert(grouped[0].id)
        assert persisted is not None and persisted.occurrence_count == 5
    else:
        assert database.list_alerts() == []

    network_sample.collection_error = None
    recovered = await service.collect_once()
    grouped = [item for item in recovered.alerts if item.rule_id == "collection_failure"]
    assert len(grouped) == 1
    assert grouped[0].occurrence_count == 5
    assert grouped[0].condition_active is False
    assert grouped[0].last_recovery is not None
