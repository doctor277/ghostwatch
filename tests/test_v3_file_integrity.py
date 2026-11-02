from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app
from app.models import FileIntegritySettings, MonitoredFolder
from app.monitoring.file_integrity import FileIntegrityCollector


def folder_model(path: Path) -> MonitoredFolder:
    now = datetime.now(UTC)
    return MonitoredFolder(
        id="folder-1",
        path=str(path.resolve()),
        created_at=now,
        baseline_created_at=now,
        status="healthy",
    )


def test_file_integrity_detects_create_modify_delete_and_then_deduplicates(tmp_path):
    root = tmp_path / "watched"
    root.mkdir()
    changed = root / "changed.txt"
    deleted = root / "deleted.txt"
    changed.write_text("before", encoding="utf-8")
    deleted.write_text("remove", encoding="utf-8")
    collector = FileIntegrityCollector()
    settings = FileIntegritySettings(max_files_per_folder=100)
    folder = folder_model(root)
    baseline_result = collector.scan(folder, {}, settings, emit_events=False)
    baseline = {entry.relative_path: entry for entry in baseline_result.entries}

    changed.write_text("after", encoding="utf-8")
    deleted.unlink()
    (root / "created.txt").write_text("new", encoding="utf-8")
    result = collector.scan(folder, baseline, settings)
    next_baseline = {entry.relative_path: entry for entry in result.entries}
    repeated = collector.scan(folder, next_baseline, settings)

    assert {(event.change_type, event.relative_path) for event in result.events} == {
        ("created", "created.txt"),
        ("modified", "changed.txt"),
        ("deleted", "deleted.txt"),
    }
    assert repeated.events == []
    assert result.status == "healthy"


def test_bounded_scan_preserves_unseen_baseline_and_never_infers_deletion(tmp_path):
    root = tmp_path / "bounded"
    root.mkdir()
    (root / "a.txt").write_text("a", encoding="utf-8")
    (root / "b.txt").write_text("b", encoding="utf-8")
    collector = FileIntegrityCollector()
    folder = folder_model(root)
    full = collector.scan(
        folder, {}, FileIntegritySettings(max_files_per_folder=10), emit_events=False
    )
    baseline = {entry.relative_path: entry for entry in full.entries}

    bounded = collector.scan(
        folder, baseline, FileIntegritySettings(max_files_per_folder=1)
    )

    assert bounded.status == "partial"
    assert bounded.events == []
    assert {entry.relative_path for entry in bounded.entries} == {"a.txt", "b.txt"}
    assert "deletions were not inferred" in bounded.status_detail


def test_file_integrity_rejects_relative_and_missing_folders(tmp_path):
    collector = FileIntegrityCollector()

    for path in ("relative", tmp_path / "missing"):
        try:
            collector.validate_folder(path)
        except ValueError:
            pass
        else:
            raise AssertionError("invalid folder path was accepted")


def test_file_integrity_api_manages_folder_and_preserves_real_files(tmp_path):
    root = tmp_path / "watched"
    root.mkdir()
    target = root / "evidence.txt"
    target.write_text("baseline", encoding="utf-8")
    app = create_app(tmp_path / "integrity.db", start_background=False)

    with TestClient(app) as client:
        added = client.post("/api/file-integrity/folders", json={"path": str(root)})
        assert added.status_code == 201
        folder_id = added.json()["id"]
        assert client.get("/api/file-integrity/events").json() == []

        target.write_text("changed", encoding="utf-8")
        scanned = client.post("/api/file-integrity/scan")
        repeated = client.post("/api/file-integrity/scan")
        events = client.get(
            "/api/file-integrity/events", params={"change_type": "modified"}
        )

        assert scanned.status_code == 200
        assert [event["change_type"] for event in scanned.json()] == ["modified"]
        assert repeated.json() == []
        assert events.json()[0]["relative_path"] == "evidence.txt"

        paused = client.put(
            f"/api/file-integrity/folders/{folder_id}", json={"enabled": False}
        )
        assert paused.json()["enabled"] is False
        removed = client.delete(f"/api/file-integrity/folders/{folder_id}")
        assert removed.status_code == 204
        assert target.read_text(encoding="utf-8") == "changed"
        assert client.get("/api/file-integrity/folders").json() == []


def test_file_integrity_settings_are_validated_and_persisted(tmp_path):
    app = create_app(tmp_path / "settings.db", start_background=False)
    updated = FileIntegritySettings(
        enabled=True,
        scan_interval_seconds=60,
        max_files_per_folder=500,
        max_file_size_bytes=1_000_000,
        event_retention=1000,
    )

    with TestClient(app) as client:
        response = client.put(
            "/api/file-integrity/settings", json=updated.model_dump(mode="json")
        )
        invalid = client.put(
            "/api/file-integrity/settings",
            json={**updated.model_dump(mode="json"), "scan_interval_seconds": 1},
        )

        assert response.status_code == 200
        assert client.get("/api/file-integrity/settings").json() == updated.model_dump(
            mode="json"
        )
        assert invalid.status_code == 422
