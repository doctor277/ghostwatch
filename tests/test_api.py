from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import create_app
from app.models import ProcessInfo


def test_health_and_overview(tmp_path, snapshot):
    app = create_app(tmp_path / "api.db", start_background=False)
    with TestClient(app) as client:
        app.state.telemetry.latest = snapshot
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["scope"] == "local-only"
        response = client.get("/api/overview")
        assert response.status_code == 200
        assert response.json()["sequence"] == 1


def test_invalid_settings_are_rejected(tmp_path):
    app = create_app(tmp_path / "api.db", start_background=False)
    with TestClient(app) as client:
        response = client.put("/api/settings", json={"refresh_interval": 0, "cpu_threshold": 101})
        assert response.status_code == 422


def test_settings_api_round_trip(tmp_path):
    app = create_app(tmp_path / "api.db", start_background=False)
    payload = {
        "refresh_interval": 4,
        "cpu_threshold": 80,
        "memory_threshold": 81,
        "bandwidth_threshold_mbps": 5,
        "connection_threshold": 200,
        "threshold_duration": 12,
        "alert_retention": 500,
        "event_logging_enabled": False,
    }
    with TestClient(app) as client:
        response = client.put("/api/settings", json=payload)
        assert response.status_code == 200
        assert client.get("/api/settings").json() == payload


def test_frontend_is_served(tmp_path):
    app = create_app(tmp_path / "api.db", start_background=False)
    with TestClient(app) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert "GHOST" in response.text and "WATCH" in response.text
        assert "cdn" not in response.text.lower()


def test_http_host_allowlist(tmp_path):
    app = create_app(tmp_path / "host.db", start_background=False)
    with TestClient(app) as client:
        assert client.get("/api/health", headers={"host": "127.0.0.1:8765"}).status_code == 200
        assert client.get("/api/health", headers={"host": "localhost:8765"}).status_code == 200
        assert client.get("/api/health", headers={"host": "attacker.example"}).status_code == 400
        assert client.get("/api/health", headers={"host": "127.0.0.1:4444"}).status_code == 400


def test_report_exports_are_redacted_by_default_with_explicit_raw_opt_in(
    tmp_path, snapshot
):
    app = create_app(tmp_path / "reports.db", start_background=False)
    private_process = ProcessInfo(
        pid=42,
        name="private.exe",
        cpu_percent=1,
        memory_percent=2,
        memory_bytes=1024,
        status="running",
        executable=r"C:\Users\PrivateUser\private.exe",
        username=r"DOMAIN\PrivateUser",
    )
    with TestClient(app) as client:
        app.state.telemetry.latest = snapshot.model_copy(
            update={"processes": [private_process]}
        )

        safe = client.get("/api/reports/process-activity.json")
        raw = client.get(
            "/api/reports/process-activity.json", params={"redact": "false"}
        )

        assert safe.status_code == raw.status_code == 200
        assert "PrivateUser" not in safe.text
        assert "[REDACTED]" in safe.text
        assert "PrivateUser" in raw.text
