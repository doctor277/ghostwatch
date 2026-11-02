from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from app.analytics.baseline import BaselineEngine
from app.main import create_app
from app.models import (
    ConnectionAggregate,
    HistoricalConnectionSummary,
    HistoricalProcessObservation,
    HistoricalSample,
    HistorySettings,
)
from app.storage.database import SCHEMA_VERSION, Database


def sample(timestamp: datetime, sequence: int) -> HistoricalSample:
    return HistoricalSample(
        timestamp=timestamp,
        sequence=sequence,
        cpu_percent=12.5,
        memory_percent=44.0,
        disk_percent=50.0,
        upload_mbps=1.2,
        download_mbps=2.3,
        active_connections=25,
        established_connections=20,
        process_count=150,
        collector_status="healthy",
    )


def test_v2_migration_creates_backup_and_history_schema(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO schema_meta VALUES ('schema_version', '2')"
        )
        connection.execute(
            "CREATE TABLE settings (id INTEGER PRIMARY KEY, payload TEXT NOT NULL)"
        )

    database = Database(path)
    assert database.initialize() == SCHEMA_VERSION == 3
    backups = list((tmp_path / "backups").glob("legacy-pre-v3-from-v2-*.db"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as backup:
        assert backup.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()[0] == "2"
        assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    with sqlite3.connect(path) as migrated:
        tables = {
            row[0]
            for row in migrated.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "telemetry_samples" in tables
        assert "process_observations" in tables
        assert "connection_summaries" in tables
        assert "telemetry_rollups" in tables
        assert "baseline_state" in tables
        assert migrated.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_history_round_trip_retention_and_row_bound(tmp_path):
    database = Database(tmp_path / "history.db")
    database.initialize()
    now = datetime.now(UTC)
    database.add_telemetry_sample(sample(now - timedelta(days=8), 1))
    for sequence in range(2, 7):
        database.add_telemetry_sample(
            sample(now + timedelta(microseconds=sequence), sequence),
            retention_hours=168,
            max_samples=3,
        )
    rows = database.list_telemetry_samples(
        now - timedelta(days=30), now + timedelta(days=1)
    )
    assert [row.sequence for row in rows] == [4, 5, 6]
    assert database.telemetry_sample_count() == 3


def test_history_api_and_settings_use_persisted_samples(tmp_path):
    app = create_app(tmp_path / "history.db", start_background=False)
    with TestClient(app) as client:
        now = datetime.now(UTC)
        app.state.database.add_telemetry_sample(sample(now, 9))
        response = client.get(
            "/api/history/query",
            params={
                "start": (now - timedelta(minutes=1)).isoformat(),
                "end": (now + timedelta(minutes=1)).isoformat(),
            },
        )
        assert response.status_code == 200
        assert response.json()[0]["sequence"] == 9

        updated = HistorySettings(
            enabled=True, retention_hours=24, max_samples=1000
        )
        saved = client.put(
            "/api/history/settings", json=updated.model_dump(mode="json")
        )
        assert saved.status_code == 200
        assert client.get("/api/history/settings").json() == updated.model_dump(
            mode="json"
        )


def test_history_api_rejects_invalid_range(tmp_path):
    app = create_app(tmp_path / "history.db", start_background=False)
    with TestClient(app) as client:
        now = datetime.now(UTC)
        response = client.get(
            "/api/history/query",
            params={"start": now.isoformat(), "end": now.isoformat()},
        )
        assert response.status_code == 422


def test_history_batch_persists_process_and_connection_observations(tmp_path):
    database = Database(tmp_path / "intelligence.db")
    database.initialize()
    now = datetime.now(UTC)
    process = HistoricalProcessObservation(
        timestamp=now,
        sequence=7,
        pid=123,
        create_time=now - timedelta(minutes=5),
        name="sample.exe",
        cpu_percent=4.5,
        memory_percent=2.5,
        connection_count=3,
        status="running",
    )
    connection = HistoricalConnectionSummary(
        timestamp=now,
        sequence=7,
        active_connections=12,
        established_connections=8,
        unique_remote_destinations=4,
        new_connections=2,
        disappeared_connections=1,
        collection_status="healthy",
        lifecycle_confidence="high",
        top_processes=[
            ConnectionAggregate(
                pid=123,
                process_name="sample.exe",
                connection_count=3,
                established_count=2,
                destination_count=2,
            )
        ],
    )

    database.add_history_batch(sample(now, 7), [process], connection)

    assert database.list_process_observations(
        now - timedelta(minutes=1), now + timedelta(minutes=1)
    ) == [process]
    assert database.list_connection_summaries(
        now - timedelta(minutes=1), now + timedelta(minutes=1)
    ) == [connection]


def test_history_compaction_rolls_up_old_samples_and_preserves_missing_values(tmp_path):
    database = Database(tmp_path / "compact.db")
    database.initialize()
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    first = sample(now - timedelta(hours=30), 1).model_copy(
        update={"cpu_percent": None, "unavailable_metrics": ["cpu_percent"]}
    )
    second = sample(now - timedelta(hours=30, minutes=-10), 2)
    database.add_telemetry_sample(first, retention_hours=168)
    database.add_telemetry_sample(second, retention_hours=168)

    compacted = database.compact_history(24, 168, now)
    rows = database.list_historical_samples(
        now - timedelta(days=2), now, limit=100
    )

    assert compacted == 2
    assert len(rows) == 1
    assert rows[0].resolution_seconds == 3600
    assert rows[0].sample_count == 2
    assert rows[0].cpu_percent == 12.5
    assert "cpu_percent" in rows[0].unavailable_metrics


def test_baseline_state_round_trip_is_bounded_and_restart_safe(tmp_path, system_sample, network_sample):
    database = Database(tmp_path / "baseline.db")
    database.initialize()
    engine = BaselineEngine(max_samples=3, min_samples=1)
    for _ in range(5):
        engine.observe(system_sample, network_sample)
    database.save_baseline_state(engine.export_state())

    restored = BaselineEngine(max_samples=3, min_samples=1)
    restored.restore_state(database.load_baseline_state())

    assert all(len(values) == 3 for values in restored.export_state().values())
    assert restored.observe(system_sample, network_sample)


def test_historical_intelligence_reports_gaps_and_exports_selected_data(tmp_path):
    app = create_app(tmp_path / "export.db", start_background=False)
    with TestClient(app) as client:
        end = datetime.now(UTC)
        start = end - timedelta(minutes=10)
        app.state.database.add_telemetry_sample(sample(end - timedelta(minutes=5), 3))
        params = {"start": start.isoformat(), "end": end.isoformat()}

        intelligence = client.get("/api/history/intelligence", params=params)
        exported = client.get(
            "/api/history/export",
            params={**params, "dataset": "telemetry", "format": "csv"},
        )

        assert intelligence.status_code == 200
        assert intelligence.json()["samples"][0]["sequence"] == 3
        assert intelligence.json()["gaps"]
        assert exported.status_code == 200
        assert "attachment;" in exported.headers["content-disposition"]
        assert "cpu_percent" in exported.text
