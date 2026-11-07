from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.api.websocket import is_allowed_host, is_allowed_origin
from app.main import create_app


def test_origin_validation():
    assert is_allowed_origin("http://127.0.0.1:8765")
    assert is_allowed_origin("http://localhost:8765")
    assert is_allowed_origin("http://[::1]:8765")
    assert not is_allowed_origin("http://localhost:4444")
    assert not is_allowed_origin("https://localhost:8765")
    assert not is_allowed_origin("http://127.0.0.1:8765/path")
    assert not is_allowed_origin("https://example.com")
    assert not is_allowed_origin(None)
    assert is_allowed_host("127.0.0.1:8765")
    assert not is_allowed_host("attacker.example")


def test_websocket_receives_telemetry(tmp_path, snapshot):
    app = create_app(tmp_path / "ws.db", start_background=False)
    with TestClient(app) as client:
        app.state.telemetry.latest = snapshot
        with client.websocket_connect(
            "/ws/telemetry", headers={"origin": "http://127.0.0.1:8765"}
        ) as socket:
            payload = socket.receive_json()
            assert payload["type"] == "telemetry"
            assert payload["sequence"] == 1


def test_websocket_frame_satisfies_the_dashboard_shape_check(tmp_path, snapshot):
    # isTelemetrySnapshot in frontend/js/websocket.js rejects a frame without these.
    app = create_app(tmp_path / "ws.db", start_background=False)
    with TestClient(app) as client:
        app.state.telemetry.latest = snapshot
        with client.websocket_connect(
            "/ws/telemetry", headers={"origin": "http://127.0.0.1:8765"}
        ) as socket:
            payload = socket.receive_json()
    assert payload["type"] == "telemetry"
    assert isinstance(payload["sequence"], int)
    assert isinstance(payload["system"], dict)
    assert isinstance(payload["system"]["timestamp"], str)
    assert isinstance(payload["network"], dict)
    assert isinstance(payload["connections"], list)
    assert isinstance(payload["processes"], list)
    assert isinstance(payload["alerts"], list)


def test_websocket_rejects_remote_origin(tmp_path):
    app = create_app(tmp_path / "ws.db", start_background=False)
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect) as error:
            with client.websocket_connect(
                "/ws/telemetry", headers={"origin": "https://attacker.example"}
            ):
                pass
        assert error.value.code == 1008


def test_websocket_rejects_wrong_port_and_host(tmp_path):
    app = create_app(tmp_path / "ws.db", start_background=False)
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect) as wrong_origin:
            with client.websocket_connect(
                "/ws/telemetry", headers={"origin": "http://localhost:4444"}
            ):
                pass
        assert wrong_origin.value.code == 1008

        with pytest.raises(WebSocketDisconnect) as wrong_host:
            with client.websocket_connect(
                "/ws/telemetry",
                headers={"origin": "http://127.0.0.1:8765", "host": "attacker.example"},
            ):
                pass
        assert wrong_host.value.code == 1008


def test_websocket_client_limit(tmp_path):
    app = create_app(tmp_path / "ws.db", start_background=False)
    with TestClient(app) as client:
        app.state.telemetry.max_clients = 1
        with client.websocket_connect(
            "/ws/telemetry", headers={"origin": "http://127.0.0.1:8765"}
        ):
            with pytest.raises(WebSocketDisconnect) as limited:
                with client.websocket_connect(
                    "/ws/telemetry", headers={"origin": "http://127.0.0.1:8765"}
                ):
                    pass
            assert limited.value.code == 1013
