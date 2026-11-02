from __future__ import annotations

import copy
import json
import socket
import threading
import tracemalloc
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.ai import AIServiceUnavailable, LocalAIAnalyst
from app.models import AIAnalysisRequest, AISettings, Alert
from app.monitoring.network import NetworkCollector
from app.privacy import redact_structure
from app.reporting import render_report, report_payload


def connection(index: int, pid: int = 7):
    return SimpleNamespace(
        type=socket.SOCK_STREAM,
        status="ESTABLISHED",
        pid=pid,
        laddr=("127.0.0.1", 10_000 + index),
        raddr=(f"203.0.{index // 250}.{index % 250 + 1}", 443),
    )


class MutableNetwork:
    value = []

    @staticmethod
    def net_io_counters():
        return SimpleNamespace(bytes_sent=100, bytes_recv=200)

    @staticmethod
    def net_connections(kind):
        value = MutableNetwork.value
        if isinstance(value, Exception):
            raise value
        return iter(value)

    @staticmethod
    def Process(pid):
        return SimpleNamespace(name=lambda: f"process-{pid}.exe")


def test_connection_failure_preserves_lifecycle_and_recovers_without_duplicates():
    row = connection(1)
    MutableNetwork.value = [row]
    collector = NetworkCollector(MutableNetwork, clock=iter((1, 2, 3)).__next__)
    first, first_details = collector.collect()
    assert first.new_connections == 1

    MutableNetwork.value = PermissionError("private detail")
    failed, stale_details = collector.collect()
    assert failed.connection_collection_status == "unavailable"
    assert failed.lifecycle_confidence == "unavailable"
    assert failed.new_connections == failed.disappeared_connections == 0
    assert stale_details[0].id == first_details[0].id
    assert not collector.recently_closed

    MutableNetwork.value = [row]
    recovered, recovered_details = collector.collect()
    assert recovered.new_connections == recovered.disappeared_connections == 0
    assert not recovered_details[0].is_new
    assert not collector.recently_closed


def test_repeated_connection_failures_then_actual_closure():
    MutableNetwork.value = [connection(1)]
    collector = NetworkCollector(MutableNetwork, clock=iter((1, 2, 3, 4)).__next__)
    collector.collect()
    for error in (PermissionError("denied"), OSError("intermittent")):
        MutableNetwork.value = error
        sample, _ = collector.collect()
        assert sample.disappeared_connections == 0
        assert not collector.recently_closed
    MutableNetwork.value = []
    recovered, details = collector.collect()
    assert details == []
    assert recovered.disappeared_connections == 1
    assert len(collector.recently_closed) == 1


def test_multiple_connection_changes_after_failure_are_factual():
    first = connection(1)
    retained = connection(2)
    added = connection(3)
    MutableNetwork.value = [first, retained]
    collector = NetworkCollector(MutableNetwork, clock=iter((1, 2, 3)).__next__)
    collector.collect()
    MutableNetwork.value = OSError("temporary")
    collector.collect()
    MutableNetwork.value = [retained, added]
    sample, details = collector.collect()
    assert sample.new_connections == 1
    assert sample.disappeared_connections == 1
    assert sum(item.is_new for item in details) == 1
    assert len(collector.recently_closed) == 1


def test_mid_iteration_connection_failure_commits_no_partial_lifecycle():
    retained = connection(1)
    MutableNetwork.value = [retained]
    collector = NetworkCollector(MutableNetwork, clock=iter((1, 2, 3)).__next__)
    collector.collect()

    def interrupted():
        yield connection(2)
        raise OSError("interrupted")

    MutableNetwork.value = interrupted()
    failed, _ = collector.collect()
    assert failed.connection_collection_status == "unavailable"
    assert failed.new_connections == failed.disappeared_connections == 0
    assert len(collector._seen) == 1

    MutableNetwork.value = [retained]
    recovered, _ = collector.collect()
    assert recovered.new_connections == recovered.disappeared_connections == 0


@pytest.mark.parametrize("socket_count", [500, 1_000, 5_000, 10_000])
def test_large_socket_collection_bounds_details_and_records_peak(socket_count):
    class LargeNetwork(MutableNetwork):
        @staticmethod
        def net_connections(kind):
            return (connection(index, pid=index % 20 + 1) for index in range(socket_count))

    collector = NetworkCollector(LargeNetwork, clock=lambda: 1)
    tracemalloc.start()
    sample, details = collector.collect()
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert sample.active_connections == socket_count
    assert sample.established_connections == socket_count
    assert sum(item.connection_count for item in collector.connection_rankings) == socket_count
    assert len(details) == 250
    assert len(collector._seen) <= collector.lifecycle_limit
    assert peak < 40 * 1024 * 1024


def test_report_families_select_category_specific_data(snapshot):
    expected = {
        "system-health": "system_metrics",
        "network-activity": "network_metrics",
        "process-activity": "processes",
        "security-alerts": "alerts",
        "anomaly-summary": "baseline_deviations",
        "incidents": "incidents",
        "daily-summary": "summary",
        "weekly-summary": "summary",
    }
    generated = datetime(2026, 1, 2, tzinfo=UTC)
    for report_type, key in expected.items():
        payload = report_payload(
            report_type, snapshot, [], [], generated_at=generated
        )
        assert payload["schema_version"] == "3.0"
        assert payload["report_type"] == report_type
        assert key in payload["data"]
        json_body, _ = render_report(payload, "json")
        csv_body, _ = render_report(payload, "csv")
        html_body, _ = render_report(payload, "html")
        assert json.loads(json_body)["report_type"] == report_type
        assert key.encode() in csv_body
        assert key.encode() in html_body

    system = report_payload("system-health", snapshot, [], [])
    assert system["data"]["system_metrics"]["cpu_percent"] == snapshot.system.cpu_percent


def test_report_schema_rejects_unknown_family(snapshot):
    with pytest.raises(ValidationError):
        report_payload("not-a-report", snapshot, [], [])


def test_recursive_privacy_redaction_does_not_mutate_source():
    source = {
        "username": "DOMAIN\\alice",
        "nested": {
            "note": (
                "Contact alice@example.test from 203.0.113.8:443 or "
                "[2001:db8::5]:8443 and inspect C:\\Users\\alice\\private.txt"
            ),
            "command_line": ["tool.exe", "--token", "secret"],
            "remote_endpoints": ["203.0.113.9:443"],
        },
    }
    original = copy.deepcopy(source)
    redacted = redact_structure(source, "strict")
    assert source == original
    serialized = json.dumps(redacted)
    for sensitive in (
        "alice@example.test",
        "203.0.113.8",
        "2001:db8::5",
        "Users",
        "--token",
        "DOMAIN",
    ):
        assert sensitive not in serialized
    assert "[REDACTED_EMAIL]" in serialized
    assert "[REDACTED_IP]" in serialized
    assert "[REDACTED_PATH]" in serialized


def test_ai_context_recursively_redacts_alert_evidence(snapshot):
    sensitive = Alert(
        id="sensitive",
        timestamp=datetime.now(UTC),
        severity="WARNING",
        source="test",
        metric="Nested evidence",
        explanation="User alice@example.test contacted 203.0.113.10",
        evidence={
            "owner": "alice",
            "details": {"file_path": "C:\\Users\\alice\\secret.txt"},
        },
    )
    updated = snapshot.model_copy(update={"alerts": [sensitive]})
    context = LocalAIAnalyst.context(updated, True, "strict")
    serialized = json.dumps(context)
    assert "alice" not in serialized
    assert "203.0.113.10" not in serialized
    assert "secret.txt" not in serialized


class RedirectResponse:
    status = 302

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def geturl(self):
        return "https://attacker.example/collect"

    def read(self, size=-1):
        return b""


@pytest.mark.asyncio
async def test_ai_rejects_redirect_response_without_external_follow(snapshot):
    calls = []

    def opener(request, timeout):
        calls.append(request.full_url)
        return RedirectResponse()

    analyst = LocalAIAnalyst(opener=opener)
    with pytest.raises(AIServiceUnavailable, match="redirect"):
        await analyst.analyze(
            AIAnalysisRequest(question="Explain current activity"),
            AISettings(enabled=True),
            snapshot,
        )
    assert calls == ["http://127.0.0.1:11434/api/generate"]


@pytest.mark.asyncio
async def test_default_ai_transport_does_not_follow_local_redirect(snapshot):
    hits = []

    class RedirectHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            hits.append(self.path)
            self.send_response(302)
            self.send_header("Location", "http://192.0.2.10:9999/collect")
            self.end_headers()

        def log_message(self, format, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        endpoint = f"http://127.0.0.1:{server.server_port}"
        with pytest.raises(AIServiceUnavailable):
            await LocalAIAnalyst().analyze(
                AIAnalysisRequest(question="Explain current activity"),
                AISettings(enabled=True, endpoint=endpoint),
                snapshot,
            )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    assert hits == ["/api/generate"]


class OversizedResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def geturl(self):
        return "http://127.0.0.1:11434/api/generate"

    def read(self, size=-1):
        return b"x" * size


@pytest.mark.asyncio
async def test_ai_rejects_oversized_response(snapshot):
    analyst = LocalAIAnalyst(opener=lambda request, timeout: OversizedResponse())
    with pytest.raises(AIServiceUnavailable, match="size limit"):
        await analyst.analyze(
            AIAnalysisRequest(question="Explain current activity"),
            AISettings(enabled=True),
            snapshot,
        )


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://127.0.0.1",
        "http://localhost:70000",
        "http://127.0.0.1:11434/proxy",
        "https://127.0.0.1:11434",
        "http://192.0.2.1:11434",
    ],
)
def test_ai_endpoint_rejects_invalid_scheme_host_port_or_path(endpoint):
    with pytest.raises(ValidationError):
        AISettings(enabled=True, endpoint=endpoint)
