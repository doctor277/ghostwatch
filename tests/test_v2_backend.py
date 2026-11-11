from __future__ import annotations

import asyncio
import json
import socket
import sqlite3
import urllib.error
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.ai import AIServiceUnavailable, LocalAIAnalyst
from app.analytics.baseline import BaselineEngine
from app.main import create_app
from app.models import (
    AIAnalysisRequest,
    AISettings,
    Alert,
    DetectionSettings,
    Settings,
    TelemetrySnapshot,
)
from app.monitoring.network import NetworkCollector
from app.monitoring.processes import ProcessCollector
from app.monitoring.windows_process_metrics import WindowsProcessMetrics
from app.reporting import csv_safe, redact_payload, render_report, report_payload
from app.service import TelemetryService
from app.storage.database import SCHEMA_VERSION, Database


class StaticCollector:
    def __init__(self, value):
        self.value = value
        self.connection_counts_by_pid = {}
        self.connection_rankings = []
        self.remote_endpoints_by_pid = {}
        self.recently_closed = []

    def collect(self, *args):
        return self.value


class StaticDetector:
    def __init__(self, alert):
        self.alert = alert

    def evaluate(self, *args):
        return [self.alert]


def alert(alert_id: str = "recoverable") -> Alert:
    return Alert(
        id=alert_id,
        timestamp=datetime.now(UTC),
        severity="WARNING",
        source="test",
        metric="Storage recovery test",
        explanation="Synthetic deterministic test event",
    )


@pytest.mark.asyncio
async def test_database_write_failure_does_not_interrupt_snapshot_and_recovers(
    tmp_path, system_sample, network_sample, monkeypatch
):
    database = Database(tmp_path / "recovery.db")
    service = TelemetryService(database, storage_retry_seconds=0)
    await service.initialize_storage()
    service.system_collector = StaticCollector(system_sample)
    service.network_collector = StaticCollector((network_sample, []))
    service.process_collector = StaticCollector([])
    service.detection_engine = StaticDetector(alert())

    original = database.add_alert
    attempts = 0

    def fail_once(value, retention):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise sqlite3.OperationalError("database is locked: private path omitted")
        return original(value, retention)

    monkeypatch.setattr(database, "add_alert", fail_once)
    first = await service.collect_once()
    assert first.sequence == 1
    assert first.storage.status == "degraded"
    assert any(item.persistence_pending for item in first.alerts)

    second = await service.collect_once()
    assert second.sequence == 2
    assert second.storage.status == "healthy"
    assert [item.id for item in database.list_alerts()].count("recoverable") == 1


@pytest.mark.asyncio
async def test_database_initialization_failure_still_publishes_live_snapshot(
    tmp_path, system_sample, network_sample
):
    service = TelemetryService(Database(tmp_path), storage_retry_seconds=60)
    assert not await service.initialize_storage()
    service.system_collector = StaticCollector(system_sample)
    service.network_collector = StaticCollector((network_sample, []))
    service.process_collector = StaticCollector([])
    service.detection_engine = StaticDetector(alert("memory-only"))
    snapshot = await service.collect_once()
    assert snapshot.sequence == 1
    assert snapshot.storage.status == "degraded"
    assert any(item.id == "memory-only" for item in snapshot.alerts)


class RankedConnections:
    @staticmethod
    def net_io_counters():
        return SimpleNamespace(bytes_sent=1, bytes_recv=1)

    @staticmethod
    def net_connections(kind):
        rows = []
        for pid, count in ((7, 300), (8, 25)):
            rows.extend(
                SimpleNamespace(
                    type=socket.SOCK_STREAM,
                    status="ESTABLISHED",
                    pid=pid,
                    laddr=("127.0.0.1", 10000 + index),
                    raddr=("203.0.113.10", 443),
                )
                for index in range(count)
            )
        return rows

    @staticmethod
    def Process(pid):
        return SimpleNamespace(name=lambda: f"process-{pid}.exe")


def test_connection_rankings_use_all_sockets_not_bounded_details():
    collector = NetworkCollector(RankedConnections, clock=lambda: 10)
    sample, details = collector.collect()
    assert sample.active_connections == 325
    assert sample.established_connections == 325
    assert len(details) == 250
    assert collector.connection_rankings[0].pid == 7
    assert collector.connection_rankings[0].connection_count == 300
    assert collector.connection_rankings[1].connection_count == 25


def test_connection_lifecycle_and_address_scope():
    class MutableConnections(RankedConnections):
        rows = [
            SimpleNamespace(
                type=socket.SOCK_STREAM,
                status="ESTABLISHED",
                pid=7,
                laddr=("127.0.0.1", 10000),
                raddr=("8.8.8.8", 443),
            )
        ]

        @staticmethod
        def net_connections(kind):
            return MutableConnections.rows

    times = iter((1.0, 3.0))
    collector = NetworkCollector(MutableConnections, clock=lambda: next(times))
    _, first = collector.collect()
    assert first[0].is_new and first[0].address_scope == "external"
    MutableConnections.rows = []
    sample, second = collector.collect()
    assert second == []
    assert sample.disappeared_connections == 1
    assert collector.recently_closed[0].status == "CLOSED"


def test_unavailable_metrics_are_explicit():
    class Broken:
        @staticmethod
        def net_io_counters():
            raise TimeoutError("secret")

        @staticmethod
        def net_connections(kind):
            raise OSError("secret")

    sample, _ = NetworkCollector(Broken, clock=lambda: 1).collect()
    assert "upload_mbps" in sample.unavailable_metrics
    assert "active_connections" in sample.unavailable_metrics
    assert "secret" not in (sample.collection_error or "")


def test_process_sampling_work_is_bounded():
    class Process:
        def __init__(self, pid):
            self.info = {"pid": pid, "name": f"p{pid}"}

        def cpu_times(self):
            FakeProcesses.metric_calls += 1
            return SimpleNamespace(user=1.0, system=0.0)

        def memory_percent(self):
            FakeProcesses.metric_calls += 1
            return 1

        def memory_info(self):
            FakeProcesses.metric_calls += 1
            return SimpleNamespace(rss=1)

        def status(self):
            FakeProcesses.identity_calls += 1
            return "running"

        def ppid(self):
            FakeProcesses.identity_calls += 1
            return 1

        def exe(self):
            FakeProcesses.identity_calls += 1
            return "C:\\safe.exe"

        def username(self):
            FakeProcesses.identity_calls += 1
            return "user"

        def create_time(self):
            FakeProcesses.identity_calls += 1
            return 1

    class FakeProcesses:
        metric_calls = 0
        identity_calls = 0

        @staticmethod
        def process_iter(attrs, ad_value):
            return [Process(pid) for pid in range(1, 301)]

        @staticmethod
        def cpu_count():
            return 1

    collector = ProcessCollector(
        FakeProcesses,
        metric_budget=20,
        identity_budget=1,
        sampling_interval_seconds=4,
        clock=lambda: 10,
    )
    result = collector.collect([], {})
    assert len(result) == 200
    assert FakeProcesses.metric_calls == 60
    assert FakeProcesses.identity_calls == 5


def test_process_collector_defaults_keep_windows_sampling_cost_bounded():
    class FakeProcesses:
        @staticmethod
        def cpu_count():
            return 8

    collector = ProcessCollector(FakeProcesses)
    # Metrics normally come from one native snapshot per cycle. The per-process
    # psutil fallback, identity lookups and fallback pass duration stay bounded.
    assert collector.metric_budget == 64
    assert collector.identity_budget == 1
    assert collector.sampling_interval_seconds == 10.0
    assert collector.metric_time_budget_seconds == 0.1
    # A fake psutil module never pairs with a snapshot of the real system.
    assert collector.native_metrics is None
    assert isinstance(ProcessCollector().native_metrics, WindowsProcessMetrics)


@pytest.mark.asyncio
async def test_bounded_websocket_queue_disconnects_overloaded_client(tmp_path, snapshot):
    service = TelemetryService(
        Database(tmp_path / "queue.db"),
        outbound_queue_size=1,
        send_timeout=0.01,
    )

    class Client:
        closed = None

        async def send_text(self, payload):
            await asyncio.Event().wait()

        async def close(self, code, reason=""):
            self.closed = code

    client = Client()
    assert await service.register_client(client)
    service._sender_tasks[client] = asyncio.current_task()
    service._client_queues[client].put_nowait(snapshot)
    await service.broadcast(snapshot)
    assert client not in service.clients
    assert client.closed == 1013


def test_security_headers_and_cross_origin_mutation(tmp_path):
    app = create_app(tmp_path / "security.db", start_background=False)
    with TestClient(app) as client:
        response = client.get("/api/health")
        assert response.headers["x-frame-options"] == "DENY"
        assert "default-src 'self'" in response.headers["content-security-policy"]
        rejected = client.put(
            "/api/settings",
            headers={"origin": "https://attacker.example"},
            json=Settings().model_dump(),
        )
        assert rejected.status_code == 403
        accepted = client.put(
            "/api/settings",
            headers={"origin": "http://127.0.0.1:8765"},
            json=Settings().model_dump(),
        )
        assert accepted.status_code == 200


def test_schema_migration_preserves_legacy_alert(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE alerts (
                id TEXT PRIMARY KEY, timestamp TEXT NOT NULL, severity TEXT NOT NULL,
                source TEXT NOT NULL, metric TEXT NOT NULL, explanation TEXT NOT NULL,
                acknowledged INTEGER NOT NULL DEFAULT 0,
                dismissed INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        connection.execute(
            "INSERT INTO alerts VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "legacy",
                datetime.now(UTC).isoformat(),
                "INFO",
                "test",
                "legacy",
                "preserved",
                0,
                0,
            ),
        )
    database = Database(path)
    assert database.initialize() == SCHEMA_VERSION
    assert database.schema_version() == SCHEMA_VERSION
    assert database.get_alert("legacy").explanation == "preserved"


def test_incident_lifecycle_and_event_api(tmp_path):
    app = create_app(tmp_path / "incident.db", start_background=False)
    with TestClient(app) as client:
        app.state.telemetry._remember_live_alert(alert("selected"))
        created = client.post(
            "/api/incidents",
            json={
                "title": "Investigate local anomaly",
                "severity": "WARNING",
                "alert_ids": ["selected"],
                "evidence_summary": "One selected local alert.",
            },
        )
        assert created.status_code == 201
        incident_id = created.json()["id"]
        updated = client.patch(
            f"/api/incidents/{incident_id}",
            json={"status": "INVESTIGATING", "analyst_notes": "Review in progress."},
        )
        assert updated.status_code == 200
        assert updated.json()["status"] == "INVESTIGATING"
        assert client.get("/api/events?event_type=incident_update").json()


def test_reports_escape_html_and_prevent_csv_formula_injection():
    assert csv_safe("=WEBSERVICE('bad')").startswith("'")
    event = SimpleNamespace(
        model_dump=lambda mode: {
            "timestamp": datetime.now(UTC).isoformat(),
            "severity": "HIGH",
            "title": "<script>alert(1)</script>",
            "summary": "=cmd",
        }
    )
    payload = report_payload("security-alerts", None, [event], [])
    csv_body, _ = render_report(payload, "csv")
    html_body, _ = render_report(payload, "html")
    assert b"'=cmd" in csv_body
    assert b"<script>" not in html_body
    assert b"&lt;script&gt;" in html_body
    redacted = redact_payload(
        {"username": "private-user", "remote_address": "8.8.8.8", "note": "C:\\secret\\file"}
    )
    assert redacted["username"] == "[REDACTED]"
    assert redacted["remote_address"] == "[REDACTED]"
    assert "secret" not in redacted["note"]


def test_detection_configuration_rejects_unknown_rule():
    with pytest.raises(ValidationError):
        DetectionSettings(enabled_rules={"made_up_rule": True})


def test_baseline_requires_history_before_reporting(system_sample, network_sample):
    baseline = BaselineEngine(min_samples=3)
    assert baseline.observe(system_sample, network_sample) == []
    assert baseline.observe(system_sample, network_sample) == []
    assert baseline.observe(system_sample, network_sample) == []
    results = baseline.observe(system_sample, network_sample)
    assert results and all(item.sample_count == 3 for item in results)


class FakeHTTPResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


@pytest.mark.asyncio
async def test_local_ai_mock_and_offline_state(snapshot: TelemetrySnapshot):
    response = {
        "observed_evidence": "CPU is observable.",
        "possible_explanations": "Normal workload is possible.",
        "missing_information": "No executable signature data.",
        "recommended_checks": "Review the process detail.",
    }
    analyst = LocalAIAnalyst(
        opener=lambda request, timeout: FakeHTTPResponse(
            {"response": json.dumps(response)}
        )
    )
    result = await analyst.analyze(
        AIAnalysisRequest(question="Explain current activity"),
        AISettings(enabled=True),
        snapshot,
    )
    assert result.observed_evidence == "CPU is observable."

    def offline(request, timeout):
        raise urllib.error.URLError("offline private detail")

    with pytest.raises(AIServiceUnavailable) as error:
        await LocalAIAnalyst(opener=offline).analyze(
            AIAnalysisRequest(question="Explain current activity"),
            AISettings(enabled=True),
            snapshot,
        )
    assert "private detail" not in str(error.value)


def test_ai_endpoint_must_be_loopback():
    with pytest.raises(ValidationError):
        AISettings(enabled=True, endpoint="https://example.com")
