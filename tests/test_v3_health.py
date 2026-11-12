from __future__ import annotations

import time

from fastapi.testclient import TestClient

from app.main import create_app


class FinishedTask:
    """Stands in for an asyncio.Task that has already finished."""

    def __init__(self, error: BaseException | None = None, cancelled: bool = False) -> None:
        self._error, self._cancelled = error, cancelled

    def done(self):
        return True

    def cancelled(self):
        return self._cancelled

    def exception(self):
        return self._error


class RunningTask:
    def done(self):
        return False


def test_health_reports_history_loss_queue_depth_and_snapshot_age(tmp_path, snapshot):
    app = create_app(tmp_path / "health.db", start_background=False)
    with TestClient(app) as client:
        service = app.state.telemetry
        service.latest = snapshot
        service.latest_monotonic = time.monotonic() - 3.0
        service.history_dropped_samples = 4

        body = client.get("/api/health").json()

    assert body["status"] == "ok"
    assert body["monitoring"] == "active"
    assert 3.0 <= body["snapshot_age_seconds"] < 30.0
    assert body["stale_after_seconds"] == 11.0  # Same rule as the dashboard watchdog.
    assert body["storage"]["history_dropped_samples"] == 4
    assert body["storage"]["history_queue_depth"] == 0
    assert body["workers"] == {
        "telemetry": "not_started",
        "history_writer": "not_started",
        "windows_events": "not_started",
        "file_integrity": "not_started",
    }


def test_health_is_degraded_when_telemetry_is_stale(tmp_path, snapshot):
    app = create_app(tmp_path / "stale.db", start_background=False)
    with TestClient(app) as client:
        service = app.state.telemetry
        service.latest = snapshot
        service.latest_monotonic = time.monotonic() - 120.0

        response = client.get("/api/health")

    assert response.status_code == 200  # The launcher's readiness check is unchanged.
    assert response.json()["status"] == "degraded"
    assert response.json()["monitoring"] == "stale"


def test_health_is_degraded_when_a_worker_has_died(tmp_path, snapshot):
    app = create_app(tmp_path / "worker.db", start_background=False)
    with TestClient(app) as client:
        service = app.state.telemetry
        service.latest = snapshot
        service.latest_monotonic = time.monotonic()
        service._task = RunningTask()
        service._history_task = FinishedTask(RuntimeError("synthetic"))
        service._windows_task = RunningTask()
        service._file_integrity_task = FinishedTask(cancelled=True)

        body = client.get("/api/health").json()

    assert body["workers"] == {
        "telemetry": "running",
        "history_writer": "failed",
        "windows_events": "running",
        "file_integrity": "stopped",
    }
    assert body["status"] == "degraded"
