from __future__ import annotations

import sqlite3
import subprocess
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.models import HistoricalSample, WindowsEventSettings
from app.monitoring.windows_events import SYSTEM_CHANNEL, WindowsEventCollector
from app.storage.database import (
    SCHEMA_VERSION,
    Database,
    UnsupportedSchemaVersionError,
)


def _sample(timestamp: datetime, sequence: int) -> HistoricalSample:
    return HistoricalSample(
        timestamp=timestamp,
        sequence=sequence,
        cpu_percent=float(sequence),
        memory_percent=40.0,
        collector_status="healthy",
    )


def _insert_rollup(database: Database, timestamp: datetime, value: float) -> None:
    with sqlite3.connect(database.path) as connection:
        connection.execute(
            """INSERT INTO telemetry_rollups
            (bucket_start, bucket_end, resolution_seconds, sample_count,
             cpu_percent, collector_status)
            VALUES (?, ?, 3600, 60, ?, 'healthy')""",
            (timestamp.isoformat(), (timestamp + timedelta(hours=1)).isoformat(), value),
        )


def _insert_raw_range(
    database: Database, base: datetime, sequences: range
) -> None:
    with sqlite3.connect(database.path) as connection:
        connection.executemany(
            """INSERT INTO telemetry_samples
            (timestamp, sequence, cpu_percent, memory_percent, collector_status)
            VALUES (?, ?, ?, 40.0, 'healthy')""",
            [
                ((base + timedelta(hours=sequence)).isoformat(), sequence, float(sequence))
                for sequence in sequences
            ],
        )


def _insert_rollup_range(database: Database, base: datetime, hours: range) -> None:
    with sqlite3.connect(database.path) as connection:
        connection.executemany(
            """INSERT INTO telemetry_rollups
            (bucket_start, bucket_end, resolution_seconds, sample_count,
             cpu_percent, collector_status)
            VALUES (?, ?, 3600, 60, ?, 'healthy')""",
            [
                (
                    (base + timedelta(hours=hour)).isoformat(),
                    (base + timedelta(hours=hour + 1)).isoformat(),
                    float(hour),
                )
                for hour in hours
            ],
        )


def _event_xml(record_id: int = 12, event_id: int = 7031) -> str:
    return f"""
    <Event xmlns="http://schemas.microsoft.com/win/2004/08/events/event">
      <System>
        <Provider Name="Service Control Manager" />
        <EventID>{event_id}</EventID><Level>2</Level>
        <TimeCreated SystemTime="2026-10-22T10:00:00Z" />
        <EventRecordID>{record_id}</EventRecordID>
      </System>
      <EventData><Data Name="ServiceName">Example Service</Data></EventData>
    </Event>
    """


def _system_only_settings() -> WindowsEventSettings:
    return WindowsEventSettings(
        defender_enabled=False,
        application_enabled=False,
        max_events_per_channel=25,
    )


def test_future_schema_in_wal_is_rejected_without_touching_database_or_sidecars(tmp_path):
    path = tmp_path / "future.db"
    database = Database(path)
    database.initialize()

    with sqlite3.connect(path) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute(
            "UPDATE schema_meta SET value = ? WHERE key = 'schema_version'", ("999",)
        )
        writer.commit()
        watched = [path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")]
        before = {
            item.name: (item.read_bytes(), item.stat().st_mtime_ns)
            for item in watched
            if item.exists()
        }

        with pytest.raises(UnsupportedSchemaVersionError, match="999"):
            Database(path).initialize()

        after = {
            item.name: (item.read_bytes(), item.stat().st_mtime_ns)
            for item in watched
            if item.exists()
        }

    assert before == after
    assert not (tmp_path / "backups").exists()


def test_interrupted_supported_migration_recovers_without_downgrading(tmp_path, monkeypatch):
    path = tmp_path / "recover.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.execute("INSERT INTO schema_meta VALUES ('schema_version', '2')")

    original = Database._ensure_alert_columns

    def fail_once(_connection):
        raise sqlite3.OperationalError("synthetic interruption")

    monkeypatch.setattr(Database, "_ensure_alert_columns", staticmethod(fail_once))
    with pytest.raises(sqlite3.OperationalError, match="synthetic interruption"):
        Database(path).initialize()

    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()[0] == "2"
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name='telemetry_samples'"
        ).fetchone()[0] == 0

    monkeypatch.setattr(Database, "_ensure_alert_columns", staticmethod(original))
    assert Database(path).initialize() == SCHEMA_VERSION
    assert Database(path).schema_version() == SCHEMA_VERSION


def test_mixed_history_limit_selects_newest_observations_then_orders_chronologically(tmp_path):
    database = Database(tmp_path / "mixed.db")
    database.initialize()
    base = datetime(2026, 1, 1, tzinfo=UTC)
    for hour in range(4):
        _insert_rollup(database, base + timedelta(hours=hour), float(hour))
    for sequence in range(4, 7):
        database.add_telemetry_sample(
            _sample(base + timedelta(hours=sequence), sequence), retention_hours=10_000
        )

    rows = database.list_historical_samples(base, base + timedelta(hours=8), limit=2)

    assert [row.sequence for row in rows] == [5, 6]
    assert [row.timestamp for row in rows] == sorted(row.timestamp for row in rows)
    assert all(row.resolution_seconds == 0 for row in rows)


def test_mixed_history_large_limit_has_stable_order_and_both_resolutions(tmp_path):
    database = Database(tmp_path / "large.db")
    database.initialize()
    base = datetime(2026, 1, 1, tzinfo=UTC)
    _insert_rollup_range(database, base, range(120))
    _insert_raw_range(database, base, range(120, 240))

    rows = database.list_historical_samples(base, base + timedelta(hours=250), limit=150)

    assert len(rows) == 150
    assert rows == sorted(rows, key=database.history_order_key)
    assert {row.resolution_seconds for row in rows} == {0, 3600}
    assert rows[-1].sequence == 239


def test_history_export_reports_exact_truncation_and_keeps_recent_raw_data(tmp_path):
    app = create_app(tmp_path / "export.db", start_background=False)
    base = datetime(2026, 1, 1, tzinfo=UTC)
    with TestClient(app) as client:
        _insert_rollup(app.state.database, base, 1.0)
        for sequence in range(1, 4):
            app.state.database.add_telemetry_sample(
                _sample(base + timedelta(hours=sequence), sequence),
                retention_hours=10_000,
            )
        params = {
            "start": base.isoformat(),
            "end": (base + timedelta(hours=5)).isoformat(),
            "dataset": "telemetry",
            "format": "json",
            "limit": 2,
        }

        truncated = client.get("/api/history/export", params=params)
        exact = client.get("/api/history/export", params={**params, "limit": 4})
        csv_export = client.get(
            "/api/history/export", params={**params, "format": "csv"}
        )

    assert truncated.status_code == 200
    assert [row["sequence"] for row in truncated.json()["samples"]] == [2, 3]
    assert truncated.json()["truncated"] is True
    assert exact.status_code == 200
    assert exact.json()["truncated"] is False
    assert [row["resolution_seconds"] for row in exact.json()["samples"]] == [3600, 0, 0, 0]
    assert exact.headers["x-ghostwatch-history-truncated"] == "false"
    assert csv_export.status_code == 200
    assert csv_export.headers["x-ghostwatch-history-truncated"] == "true"


def test_selected_export_truncation_is_scoped_to_the_selected_dataset(tmp_path):
    app = create_app(tmp_path / "dataset-export.db", start_background=False)
    base = datetime(2026, 1, 1, tzinfo=UTC)
    with TestClient(app) as client:
        app.state.database.add_telemetry_sample(
            _sample(base + timedelta(hours=1), 1), retention_hours=10_000
        )
        with sqlite3.connect(app.state.database.path) as connection:
            connection.executemany(
                """INSERT INTO process_observations
                (timestamp, sequence, pid, name, status, access_status)
                VALUES (?, ?, ?, ?, 'running', 'available')""",
                [
                    ((base + timedelta(hours=2)).isoformat(), index, index, f"p{index}")
                    for index in range(3)
                ],
            )
        response = client.get(
            "/api/history/export",
            params={
                "start": base.isoformat(),
                "end": (base + timedelta(hours=3)).isoformat(),
                "dataset": "telemetry",
                "format": "json",
                "limit": 2,
            },
        )

    assert response.json()["truncated"] is False
    assert response.headers["x-ghostwatch-history-truncated"] == "false"


def test_truncated_history_does_not_report_the_unselected_head_as_missing(tmp_path):
    app = create_app(tmp_path / "gaps.db", start_background=False)
    base = datetime(2026, 1, 1, tzinfo=UTC)
    with TestClient(app) as client:
        for sequence in range(3):
            app.state.database.add_telemetry_sample(
                _sample(base + timedelta(minutes=sequence), sequence),
                retention_hours=10_000,
            )
        response = client.get(
            "/api/history/intelligence",
            params={
                "start": base.isoformat(),
                "end": (base + timedelta(minutes=3)).isoformat(),
                "limit": 1,
            },
        )

    payload = response.json()
    assert payload["truncated"] is True
    assert payload["samples"][0]["sequence"] == 2
    assert all(gap["start"] != base.isoformat().replace("+00:00", "Z") for gap in payload["gaps"])


@pytest.mark.parametrize("payload", ["<Events><Event>", "not xml at all"])
def test_malformed_nonempty_windows_xml_is_unavailable_and_preserves_cursor(payload):
    def runner(command, **_kwargs):
        return subprocess.CompletedProcess(command, 0, stdout=payload, stderr="")

    batch = WindowsEventCollector(runner, platform_name="nt").collect(
        {SYSTEM_CHANNEL: 41}, _system_only_settings()
    )

    assert batch.events == []
    assert batch.channels[0].status == "unavailable"
    assert batch.channels[0].cursor == 41
    assert payload not in batch.channels[0].detail


@pytest.mark.parametrize("payload", ["", "  <Events />  "])
def test_valid_empty_windows_results_are_healthy(payload):
    def runner(command, **_kwargs):
        return subprocess.CompletedProcess(command, 0, stdout=payload, stderr="")

    batch = WindowsEventCollector(runner, platform_name="nt").collect(
        {SYSTEM_CHANNEL: 41}, _system_only_settings()
    )

    assert batch.events == []
    assert batch.channels[0].status == "healthy"
    assert batch.channels[0].cursor == 41


def test_partially_invalid_windows_response_is_degraded_without_cursor_advance():
    payload = f"<Events>{_event_xml(42)}<Event><System /></Event></Events>"

    def runner(command, **_kwargs):
        return subprocess.CompletedProcess(command, 0, stdout=payload, stderr="")

    batch = WindowsEventCollector(runner, platform_name="nt").collect(
        {SYSTEM_CHANNEL: 41}, _system_only_settings()
    )

    assert [event.record_id for event in batch.events] == [42]
    assert batch.channels[0].status == "degraded"
    assert batch.channels[0].cursor == 41
    assert "1 event" in batch.channels[0].detail


def test_windows_parser_recovers_after_repeated_failures_and_then_advances_cursor():
    outputs = iter(["<Events>", "truncated", f"<Events>{_event_xml(52)}</Events>"])

    def runner(command, **_kwargs):
        return subprocess.CompletedProcess(command, 0, stdout=next(outputs), stderr="")

    collector = WindowsEventCollector(runner, platform_name="nt")
    statuses = [
        collector.collect({SYSTEM_CHANNEL: 50}, _system_only_settings())
        for _ in range(3)
    ]

    assert [batch.channels[0].status for batch in statuses] == [
        "unavailable",
        "unavailable",
        "healthy",
    ]
    assert [batch.channels[0].cursor for batch in statuses] == [50, 50, 52]
    assert [event.record_id for event in statuses[-1].events] == [52]
