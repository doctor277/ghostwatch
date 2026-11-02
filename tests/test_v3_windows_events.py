from __future__ import annotations

import subprocess
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.models import (
    WindowsChannelStatus,
    WindowsEventBatch,
    WindowsEventRecord,
    WindowsEventSettings,
)
from app.monitoring.windows_events import (
    DEFENDER_CHANNEL,
    SYSTEM_CHANNEL,
    WindowsEventCollector,
)
from app.privacy import redact_text
from app.service import TelemetryService
from app.storage.database import Database


def event_xml(
    record_id: int,
    event_id: int,
    *,
    provider: str = "Service Control Manager",
    service: str = "Example Service",
) -> str:
    return f"""
    <Event xmlns="http://schemas.microsoft.com/win/2004/08/events/event">
      <System>
        <Provider Name="{provider}" />
        <EventID>{event_id}</EventID><Level>2</Level>
        <TimeCreated SystemTime="2026-10-22T10:00:00.0000000Z" />
        <EventRecordID>{record_id}</EventRecordID>
      </System>
      <EventData><Data Name="ServiceName">{service}</Data></EventData>
    </Event>
    """


def test_windows_collector_is_incremental_and_deduplicates_records():
    calls: list[list[str]] = []

    def runner(command, **_kwargs):
        calls.append(command)
        xml = f"<Events>{event_xml(10, 7031)}{event_xml(10, 7031)}</Events>"
        return subprocess.CompletedProcess(command, 0, stdout=xml, stderr="")

    collector = WindowsEventCollector(runner, platform_name="nt")
    settings = WindowsEventSettings(
        defender_enabled=False,
        application_enabled=False,
        max_events_per_channel=25,
    )

    first = collector.collect({}, settings)
    second = collector.collect({SYSTEM_CHANNEL: 10}, settings)

    assert len(first.events) == 1
    assert first.events[0].category == "service_failure"
    assert first.events[0].service_name == "Example Service"
    assert first.channels[0].cursor == 10
    assert any("EventRecordID>10" in argument for argument in calls[1])
    assert second.events == []


def test_windows_collector_reports_permission_failure_without_raising():
    def denied(command, **_kwargs):
        return subprocess.CompletedProcess(command, 5, stdout="", stderr="Access is denied.")

    collector = WindowsEventCollector(denied, platform_name="nt")
    settings = WindowsEventSettings(
        defender_enabled=False, application_enabled=False
    )

    batch = collector.collect({}, settings)

    assert batch.events == []
    assert batch.channels[0].status == "permission_denied"
    assert "continued" in batch.channels[0].detail


def test_non_windows_platform_has_explicit_unsupported_status():
    collector = WindowsEventCollector(platform_name="posix")
    batch = collector.collect({}, WindowsEventSettings())

    assert batch.events == []
    assert {status.status for status in batch.channels} == {"unsupported"}


def test_windows_event_storage_suppresses_duplicates_and_supports_filters(tmp_path):
    database = Database(tmp_path / "events.db")
    database.initialize()
    event = WindowsEventRecord(
        id="windows:System:77",
        channel=SYSTEM_CHANNEL,
        record_id=77,
        timestamp=datetime.now(UTC),
        provider="Service Control Manager",
        event_id=7034,
        level="ERROR",
        category="service_failure",
        summary="Example Service terminated unexpectedly",
        service_name="Example Service",
        correlation_tags=["service", "failure"],
    )

    assert database.add_windows_event_batch([event, event], {SYSTEM_CHANNEL: 77}, 168) == [
        event.id
    ]
    assert database.add_windows_event_batch([event], {SYSTEM_CHANNEL: 77}, 168) == []
    assert database.load_windows_event_cursors()[SYSTEM_CHANNEL] == 77
    assert database.list_windows_events(category="service_failure", search="Example") == [
        event
    ]
    assert database.windows_event_summary() == {"service_failure": 1}


@pytest.mark.asyncio
async def test_windows_event_correlation_creates_one_live_alert(tmp_path):
    database = Database(tmp_path / "correlation.db")
    service = TelemetryService(database)
    assert await service.initialize_storage()
    event = WindowsEventRecord(
        id=f"windows:{DEFENDER_CHANNEL}:42",
        channel=DEFENDER_CHANNEL,
        record_id=42,
        timestamp=datetime.now(UTC),
        provider="Microsoft-Windows-Windows Defender",
        event_id=1116,
        level="WARNING",
        category="defender_detection",
        summary="Synthetic Defender event",
        correlation_tags=["defender", "detection"],
    )

    class FakeCollector:
        def collect(self, _cursors, _settings):
            return WindowsEventBatch(
                events=[event],
                channels=[
                    WindowsChannelStatus(
                        channel=DEFENDER_CHANNEL, status="healthy", cursor=42
                    )
                ],
            )

    service.windows_event_collector = FakeCollector()
    await service.collect_windows_events_once()
    await service.collect_windows_events_once()

    alerts = [alert for alert in service.live_alerts if alert.rule_id == "windows_defender_detection"]
    assert len(alerts) == 1
    assert alerts[0].severity == "CRITICAL"


def test_windows_event_api_search_summary_and_settings(tmp_path):
    app = create_app(tmp_path / "api.db", start_background=False)
    with TestClient(app) as client:
        event = WindowsEventRecord(
            id="windows:Application:9",
            channel="Application",
            record_id=9,
            timestamp=datetime.now(UTC),
            provider="Synthetic Provider",
            event_id=1000,
            level="ERROR",
            category="application",
            summary="Synthetic application failure",
            correlation_tags=["application"],
        )
        app.state.database.add_windows_event_batch([event], {"Application": 9}, 168)

        found = client.get("/api/windows-events", params={"search": "Synthetic"})
        summary = client.get("/api/windows-events/summary")
        updated = WindowsEventSettings(enabled=False)
        saved = client.put(
            "/api/windows-events/settings", json=updated.model_dump(mode="json")
        )

        assert found.status_code == 200
        assert found.json()[0]["record_id"] == 9
        assert summary.json()["counts"] == {"application": 1}
        assert saved.status_code == 200
        assert client.get("/api/windows-events/settings").json() == updated.model_dump(
            mode="json"
        )


def test_windows_event_api_redacts_private_identifiers_by_default(tmp_path):
    app = create_app(tmp_path / "privacy.db", start_background=False)
    with TestClient(app) as client:
        event = WindowsEventRecord(
            id="windows:System:91",
            channel="System",
            record_id=91,
            timestamp=datetime.now(UTC),
            provider="Service Control Manager",
            event_id=7031,
            level="ERROR",
            category="service_failure",
            summary=(
                r"User=PrivateUser SID=S-1-5-21-111-222-333-1001 "
                r"Path=C:\Users\PrivateUser\Documents\sensitive.txt EventID=7031"
            ),
            service_name=r"DOMAIN\PrivateUser",
            correlation_tags=["service", "failure"],
        )
        app.state.database.add_windows_event_batch([event], {"System": 91}, 168)

        redacted = client.get("/api/windows-events").json()[0]
        raw = client.get("/api/windows-events", params={"privacy": "raw"}).json()[0]

        assert "PrivateUser" not in redacted["summary"]
        assert "S-1-5-21" not in redacted["summary"]
        assert "[REDACTED_USER]" in redacted["summary"]
        assert "[REDACTED_SID]" in redacted["summary"]
        assert "[REDACTED_PATH]" in redacted["summary"]
        assert redacted["event_id"] == 7031
        assert raw["summary"] == event.summary
        assert raw["service_name"] == event.service_name


def test_windows_event_redaction_handles_spaced_accounts_and_paths():
    redacted = redact_text(
        r"User=Private User Path=C:\Users\Private User\Secret Folder\item.txt EventID=7031"
    )

    assert "Private User" not in redacted
    assert "Secret Folder" not in redacted
    assert "[REDACTED_USER]" in redacted
    assert "[REDACTED_PATH]" in redacted
    assert "EventID=7031" in redacted
