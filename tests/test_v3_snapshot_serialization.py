from __future__ import annotations

import asyncio
import json
import logging

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.config import MAX_SNAPSHOT_BYTES
from app.main import create_app
from app.models import Alert, TelemetrySnapshot
from app.service import SNAPSHOT_DEGRADED_FRAME, TelemetryService
from app.storage.database import Database

LOCAL_ORIGIN = {"origin": "http://127.0.0.1:8765"}
UNAVAILABLE = {"detail": "Telemetry snapshot is unavailable"}
PRIVATE_MARKER = "synthetic-private-marker"


def with_alert(snapshot, sequence, explanation="synthetic"):
    alert = Alert(
        id="synthetic-alert",
        timestamp=snapshot.system.timestamp,
        severity="INFO",
        source="synthetic",
        metric="synthetic",
        explanation=explanation,
    )
    return snapshot.model_copy(update={"sequence": sequence, "alerts": [alert]})


def surrogate_snapshot(snapshot, sequence=2):
    return with_alert(snapshot, sequence, f"{PRIVATE_MARKER} \ud800")


def object_evidence_snapshot(snapshot, sequence=2):
    malformed = with_alert(snapshot, sequence)
    malformed.alerts[0].evidence["value"] = object()
    return malformed


MALFORMED = [surrogate_snapshot, object_evidence_snapshot]


@pytest.fixture
def application(tmp_path):
    app = create_app(tmp_path / "synthetic.db", start_background=False)
    with TestClient(app) as client:
        yield app, client


def connect(client):
    return client.websocket_connect("/ws/telemetry", headers=LOCAL_ORIGIN)


@pytest.mark.parametrize("malformed", MALFORMED)
def test_healthy_clients_stay_connected_and_recover(application, snapshot, malformed):
    app, client = application
    service = app.state.telemetry
    service.latest = snapshot
    with connect(client) as first, connect(client) as second, connect(client) as third:
        sockets = (first, second, third)
        assert [socket.receive_json()["sequence"] for socket in sockets] == [1, 1, 1]

        client.portal.call(service.broadcast, malformed(snapshot))
        assert [socket.receive_text() for socket in sockets] == [SNAPSHOT_DEGRADED_FRAME] * 3
        assert len(service.clients) == 3

        recovered = with_alert(snapshot, 3)
        client.portal.call(service.broadcast, recovered)
        for socket in sockets:
            payload = socket.receive_json()
            assert payload["type"] == "telemetry"
            assert payload["sequence"] == 3
        assert len(service.clients) == 3


@pytest.mark.parametrize("malformed", MALFORMED)
def test_new_client_during_malformed_state_recovers(application, snapshot, malformed):
    app, client = application
    service = app.state.telemetry
    service.latest = malformed(snapshot)
    with connect(client) as socket:
        assert socket.receive_text() == SNAPSHOT_DEGRADED_FRAME
        assert len(service.clients) == 1
        client.portal.call(service.broadcast, with_alert(snapshot, 3))
        assert socket.receive_json()["sequence"] == 3


async def test_genuine_socket_failure_disconnects_only_that_client(tmp_path, snapshot):
    class Client:
        def __init__(self, failing=False):
            self.failing = failing
            self.frames = []
            self.closed = []

        async def send_text(self, text):
            if self.failing:
                raise RuntimeError("synthetic socket failure")
            self.frames.append(text)

        async def close(self, code=1000, reason=None):
            self.closed.append(code)

    service = TelemetryService(Database(tmp_path / "synthetic.db"))
    healthy, broken = Client(), Client(failing=True)
    for item in (healthy, broken):
        assert await service.register_client(item)
        await service.activate_client(item)

    await service.broadcast(surrogate_snapshot(snapshot))
    await asyncio.sleep(0.05)

    assert service.clients == {healthy}
    assert broken.closed == [1011]
    assert healthy.closed == []
    assert healthy.frames == [SNAPSHOT_DEGRADED_FRAME]
    await service.stop()


@pytest.mark.parametrize("malformed", [with_alert, surrogate_snapshot])
async def test_shared_frame_is_encoded_once_per_snapshot(
    tmp_path, snapshot, monkeypatch, malformed
):
    service = TelemetryService(Database(tmp_path / "synthetic.db"))
    selected = malformed(snapshot, 2)
    calls = []
    real_dump = TelemetrySnapshot.model_dump_json

    def counting_dump(self, *args, **kwargs):
        calls.append(self.sequence)
        return real_dump(self, *args, **kwargs)

    monkeypatch.setattr(TelemetrySnapshot, "model_dump_json", counting_dump)
    for _ in range(3):
        service.encode_snapshot(selected)
    assert calls == [2]


@pytest.mark.parametrize("malformed", MALFORMED)
def test_rest_malformed_snapshot_is_sanitized_and_recovers(application, snapshot, malformed):
    app, client = application
    service = app.state.telemetry
    service.latest = malformed(snapshot)

    response = client.get("/api/overview")
    assert response.status_code == 503
    assert response.json() == UNAVAILABLE
    assert response.headers["content-type"] == "application/json"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert client.get("/api/health").json()["snapshot"] == "unavailable"

    service.latest = with_alert(snapshot, 3)
    response = client.get("/api/overview")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.json() == json.loads(service.latest.model_dump_json())
    assert client.get("/api/health").json()["snapshot"] == "available"


def test_non_finite_values_are_not_treated_as_malformed(application, snapshot):
    app, client = application
    service = app.state.telemetry
    service.latest = snapshot.model_copy(
        update={"system": snapshot.system.model_copy(update={"cpu_percent": float("nan")})}
    )
    response = client.get("/api/overview")
    assert response.status_code == 200
    assert response.json()["system"]["cpu_percent"] is None


def test_overview_still_reports_warming_up(application):
    _, client = application
    response = client.get("/api/overview")
    assert response.status_code == 503
    assert response.json() == {"detail": "Telemetry is warming up"}
    assert client.get("/api/health").json()["snapshot"] == "warming-up"


def test_failure_signals_do_not_expose_snapshot_contents(application, snapshot, caplog):
    app, client = application
    service = app.state.telemetry
    service.latest = surrogate_snapshot(snapshot)

    with caplog.at_level(logging.DEBUG, logger="app.service"):
        response = client.get("/api/overview")
        with connect(client) as socket:
            frame = socket.receive_text()

    assert PRIVATE_MARKER not in response.text
    assert PRIVATE_MARKER not in frame
    assert PRIVATE_MARKER not in caplog.text
    assert "ud800" not in caplog.text
    assert json.loads(frame) == {
        "type": "telemetry_degraded",
        "reason": "snapshot_unavailable",
    }


def test_repeated_failures_log_once_until_recovery(tmp_path, snapshot, caplog):
    service = TelemetryService(Database(tmp_path / "synthetic.db"))
    with caplog.at_level(logging.INFO, logger="app.service"):
        for sequence in (2, 3, 4):
            assert service.encode_snapshot(surrogate_snapshot(snapshot, sequence)) is None
        assert service.encode_snapshot(with_alert(snapshot, 5)) is not None
        assert service.encode_snapshot(surrogate_snapshot(snapshot, 6)) is None
    messages = [record.getMessage() for record in caplog.records]
    assert messages == [
        "Telemetry snapshot could not be encoded (PydanticSerializationError)",
        "Telemetry snapshot encoding recovered",
        "Telemetry snapshot could not be encoded (PydanticSerializationError)",
    ]


def test_oversized_snapshot_is_bounded(application, snapshot, monkeypatch):
    app, client = application
    service = app.state.telemetry
    monkeypatch.setattr("app.service.MAX_SNAPSHOT_BYTES", 256)
    service.latest = with_alert(snapshot, 2, "x" * 512)

    assert service.encode_snapshot(service.latest) is None
    assert service._encoded_snapshot[1] is None
    assert client.get("/api/overview").status_code == 503
    with connect(client) as socket:
        assert socket.receive_text() == SNAPSHOT_DEGRADED_FRAME


def test_encoding_retains_only_the_latest_snapshot(tmp_path, snapshot):
    service = TelemetryService(Database(tmp_path / "synthetic.db"))
    first, second = with_alert(snapshot, 2), with_alert(snapshot, 3)
    service.encode_snapshot(first)
    service.encode_snapshot(second)
    assert service._encoded_snapshot[0] is second
    assert len(SNAPSHOT_DEGRADED_FRAME) < 128
    assert len(second.model_dump_json().encode("utf-8")) < MAX_SNAPSHOT_BYTES


@pytest.mark.parametrize("malformed", MALFORMED)
def test_host_and_origin_checks_hold_during_malformed_state(application, snapshot, malformed):
    app, client = application
    app.state.telemetry.latest = malformed(snapshot)

    for headers in (
        {"origin": "https://example.com"},
        {"origin": "http://127.0.0.1:8765", "host": "attacker.example"},
    ):
        with pytest.raises(WebSocketDisconnect) as error:
            with client.websocket_connect("/ws/telemetry", headers=headers):
                pass
        assert error.value.code == 1008
    assert not app.state.telemetry.clients

    response = client.get("/api/overview", headers={"host": "attacker.example"})
    assert response.status_code == 400
