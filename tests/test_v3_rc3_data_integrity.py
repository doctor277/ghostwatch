from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.models import (
    HistoricalConnectionSummary,
    HistoricalProcessObservation,
    HistoricalSample,
    WindowsEventRecord,
)
from app.reporting import report_payload
from app.storage.database import Database
from scripts.package_release import selected_files


def _sample(
    timestamp: datetime,
    sequence: int,
    *,
    cpu: float | None = 10.0,
    memory: float | None = 40.0,
    unavailable: list[str] | None = None,
) -> HistoricalSample:
    return HistoricalSample(
        timestamp=timestamp,
        sequence=sequence,
        cpu_percent=cpu,
        memory_percent=memory,
        collector_status="degraded" if unavailable else "healthy",
        unavailable_metrics=unavailable or [],
    )


def _insert_rollup(
    database: Database,
    start: datetime,
    *,
    cpu: float = 10.0,
    sample_count: int = 1,
) -> None:
    with sqlite3.connect(database.path) as connection:
        connection.execute(
            """INSERT INTO telemetry_rollups
            (bucket_start, bucket_end, resolution_seconds, sample_count,
             cpu_percent, collector_status)
            VALUES (?, ?, 3600, ?, ?, 'healthy')""",
            (start.isoformat(), (start + timedelta(hours=1)).isoformat(), sample_count, cpu),
        )


def test_compaction_waits_for_completed_hour_and_is_idempotent(tmp_path):
    database = Database(tmp_path / "partial.db")
    database.initialize()
    bucket = datetime(2026, 10, 20, 10, tzinfo=UTC)
    database.add_telemetry_sample(_sample(bucket + timedelta(minutes=5), 1, cpu=20.0), 10_000)
    database.add_telemetry_sample(_sample(bucket + timedelta(minutes=55), 2, cpu=80.0), 10_000)

    assert database.compact_history(2, 10_000, bucket + timedelta(hours=2, minutes=30)) == 0
    assert database.telemetry_sample_count() == 2
    assert database.compact_history(2, 10_000, bucket + timedelta(hours=3)) == 2
    assert database.compact_history(2, 10_000, bucket + timedelta(hours=3)) == 0

    rows = database.list_historical_samples(bucket, bucket + timedelta(hours=1))
    assert len(rows) == 1
    assert rows[0].cpu_percent == 50.0
    assert rows[0].sample_count == 2
    assert rows[0].metric_sample_counts["cpu_percent"] == 2


def test_restart_reconciles_legacy_partial_rollup_without_double_counting(tmp_path):
    path = tmp_path / "legacy-partial.db"
    database = Database(path)
    database.initialize()
    bucket = datetime(2026, 10, 20, 10, tzinfo=UTC)
    _insert_rollup(database, bucket, cpu=10.0)
    with sqlite3.connect(path) as connection:
        connection.execute(
            """INSERT INTO telemetry_samples
            (timestamp, sequence, cpu_percent, memory_percent,
             collector_status, unavailable_metrics)
            VALUES (?, 2, 30.0, NULL, 'degraded', '["memory_percent"]')""",
            ((bucket + timedelta(minutes=45)).isoformat(),),
        )

    restarted = Database(path)
    restarted.initialize()
    rows = restarted.list_historical_samples(bucket, bucket + timedelta(hours=1))
    assert restarted.telemetry_sample_count() == 0
    assert len(rows) == 1
    assert rows[0].cpu_percent == 20.0
    assert rows[0].sample_count == 2
    assert rows[0].metric_sample_counts["cpu_percent"] == 2
    assert rows[0].metric_sample_counts["memory_percent"] == 0
    assert "memory_percent" in rows[0].unavailable_metrics

    restarted.initialize()
    again = restarted.list_historical_samples(bucket, bucket + timedelta(hours=1))
    assert again[0].cpu_percent == 20.0
    assert again[0].sample_count == 2


def test_compaction_records_exact_sparse_metric_denominators(tmp_path):
    database = Database(tmp_path / "sparse.db")
    database.initialize()
    bucket = datetime(2026, 10, 20, 8, tzinfo=UTC)
    database.add_telemetry_sample(
        _sample(bucket + timedelta(minutes=5), 1, cpu=None, memory=20.0, unavailable=["cpu_percent"]),
        10_000,
    )
    database.add_telemetry_sample(
        _sample(bucket + timedelta(minutes=35), 2, cpu=60.0, memory=40.0), 10_000
    )

    database.compact_history(1, 10_000, bucket + timedelta(hours=2))
    row = database.list_historical_samples(bucket, bucket + timedelta(hours=1))[0]
    assert row.cpu_percent == 60.0
    assert row.memory_percent == 30.0
    assert row.metric_sample_counts["cpu_percent"] == 1
    assert row.metric_sample_counts["memory_percent"] == 2


def test_mixed_history_uses_half_open_overlap_and_exposes_coverage(tmp_path):
    database = Database(tmp_path / "overlap.db")
    database.initialize()
    bucket = datetime(2026, 10, 20, 12, tzinfo=UTC)
    _insert_rollup(database, bucket)

    overlap = database.list_historical_samples(
        bucket + timedelta(minutes=30), bucket + timedelta(hours=1, minutes=30)
    )
    left_boundary = database.list_historical_samples(
        bucket - timedelta(hours=1), bucket
    )
    right_boundary = database.list_historical_samples(
        bucket + timedelta(hours=1), bucket + timedelta(hours=2)
    )

    assert len(overlap) == 1
    assert overlap[0].coverage_start == bucket
    assert overlap[0].coverage_end == bucket + timedelta(hours=1)
    assert left_boundary == []
    assert right_boundary == []


def test_late_raw_sample_is_merged_into_covering_rollup_once(tmp_path):
    database = Database(tmp_path / "late-sample.db")
    database.initialize()
    bucket = datetime(2026, 10, 20, 12, tzinfo=UTC)
    _insert_rollup(database, bucket, cpu=20.0)
    # Load the legacy count metadata before accepting a late observation.
    database.initialize()

    late = _sample(bucket + timedelta(minutes=30), 7, cpu=80.0)
    database.add_telemetry_sample(late, 10_000)
    database.add_telemetry_sample(late, 10_000)

    rows = database.list_historical_samples(bucket, bucket + timedelta(hours=1))
    assert database.telemetry_sample_count() == 0
    assert len(rows) == 1
    assert rows[0].cpu_percent == 50.0
    assert rows[0].sample_count == 2
    assert rows[0].metric_sample_counts["cpu_percent"] == 2


def test_newest_limit_and_api_export_keep_overlap_metadata(tmp_path):
    app = create_app(tmp_path / "export.db", start_background=False)
    bucket = datetime(2026, 10, 20, 10, tzinfo=UTC)
    with TestClient(app) as client:
        _insert_rollup(app.state.database, bucket)
        for sequence in range(1, 4):
            app.state.database.add_telemetry_sample(
                _sample(bucket + timedelta(hours=sequence), sequence, cpu=float(sequence)),
                10_000,
            )
        response = client.get(
            "/api/history/export",
            params={
                "start": (bucket + timedelta(minutes=30)).isoformat(),
                "end": (bucket + timedelta(hours=4)).isoformat(),
                "dataset": "telemetry",
                "format": "json",
                "limit": 2,
            },
        )

    payload = response.json()
    assert response.status_code == 200
    assert payload["truncated"] is True
    assert [row["sequence"] for row in payload["samples"]] == [2, 3]


def test_equivalent_offsets_query_the_same_utc_interval(tmp_path):
    database = Database(tmp_path / "offsets.db")
    database.initialize()
    observed = datetime(2026, 10, 20, 16, 30, tzinfo=UTC)
    database.add_telemetry_sample(_sample(observed, 1), 10_000)
    eastern = timezone(timedelta(hours=-4))

    utc_rows = database.list_historical_samples(
        observed - timedelta(minutes=1), observed + timedelta(minutes=1)
    )
    offset_rows = database.list_historical_samples(
        (observed - timedelta(minutes=1)).astimezone(eastern),
        (observed + timedelta(minutes=1)).astimezone(eastern),
    )
    assert utc_rows == offset_rows

    with sqlite3.connect(database.path) as connection:
        stored = connection.execute("SELECT timestamp FROM telemetry_samples").fetchone()[0]
    assert stored == observed.isoformat()


def test_utc_normalization_covers_process_connection_windows_and_reports(tmp_path):
    database = Database(tmp_path / "time-surfaces.db")
    database.initialize()
    observed = datetime(2026, 10, 20, 16, 30, tzinfo=UTC)
    process = HistoricalProcessObservation(
        timestamp=observed.astimezone(timezone(timedelta(hours=-4))),
        sequence=3,
        pid=42,
        name="synthetic.exe",
        status="running",
    )
    connection = HistoricalConnectionSummary(
        timestamp=observed.astimezone(timezone(timedelta(hours=5, minutes=30))),
        sequence=3,
        collection_status="healthy",
        lifecycle_confidence="high",
    )
    database.add_history_batch(_sample(observed, 3), [process], connection, 10_000)
    windows_event = WindowsEventRecord(
        id="synthetic-windows-event",
        channel="System",
        record_id=9,
        timestamp=observed.astimezone(timezone(timedelta(hours=-7))),
        provider="Synthetic Provider",
        event_id=1,
        level="INFORMATION",
        category="system",
    )
    database.add_windows_event_batch([windows_event], {"System": 9}, 10_000)

    start_utc = observed - timedelta(minutes=1)
    end_utc = observed + timedelta(minutes=1)
    offset = timezone(timedelta(hours=-5))
    start_offset = start_utc.astimezone(offset)
    end_offset = end_utc.astimezone(offset)
    assert database.list_process_observations(start_utc, end_utc) == (
        database.list_process_observations(start_offset, end_offset)
    )
    assert database.list_connection_summaries(start_utc, end_utc) == (
        database.list_connection_summaries(start_offset, end_offset)
    )
    assert database.list_windows_events(start=start_utc, end=end_utc) == (
        database.list_windows_events(start=start_offset, end=end_offset)
    )

    report = report_payload(
        "daily-summary",
        None,
        [],
        [],
        generated_at=observed.astimezone(offset),
    )
    assert report["generated_at"] == observed.isoformat().replace("+00:00", "Z")
    assert report["range_end"] == report["generated_at"]


def test_dst_fold_observations_remain_distinct_in_utc(tmp_path):
    database = Database(tmp_path / "dst.db")
    database.initialize()
    # The repeated 01:30 wall-clock time is represented with the explicit EDT
    # and EST offsets supplied by browsers/clients at the DST transition.
    first = datetime(2026, 11, 1, 1, 30, tzinfo=timezone(timedelta(hours=-4)))
    second = datetime(2026, 11, 1, 1, 30, tzinfo=timezone(timedelta(hours=-5)))
    database.add_telemetry_sample(_sample(first, 1), 10_000)
    database.add_telemetry_sample(_sample(second, 2), 10_000)

    rows = database.list_historical_samples(
        datetime(2026, 11, 1, 5, tzinfo=UTC),
        datetime(2026, 11, 1, 7, tzinfo=UTC),
    )
    assert [row.timestamp for row in rows] == [
        datetime(2026, 11, 1, 5, 30, tzinfo=UTC),
        datetime(2026, 11, 1, 6, 30, tzinfo=UTC),
    ]


def test_naive_datetimes_are_rejected_by_storage_and_api(tmp_path):
    database = Database(tmp_path / "naive.db")
    database.initialize()
    naive = datetime(2026, 10, 20, 12)
    with pytest.raises(ValueError, match="timezone offset"):
        database.add_telemetry_sample(_sample(naive, 1), 10_000)
    with pytest.raises(ValueError, match="timezone offset"):
        database.list_historical_samples(naive, naive + timedelta(hours=1))

    app = create_app(tmp_path / "naive-api.db", start_background=False)
    with TestClient(app) as client:
        response = client.get(
            "/api/history/query",
            params={"start": naive.isoformat(), "end": (naive + timedelta(hours=1)).isoformat()},
        )
    assert response.status_code == 422


def test_initialize_canonicalizes_aware_legacy_timestamps_without_data_loss(tmp_path):
    path = tmp_path / "canonical.db"
    database = Database(path)
    database.initialize()
    with sqlite3.connect(path) as connection:
        connection.execute(
            """INSERT INTO telemetry_samples
            (timestamp, sequence, cpu_percent, collector_status)
            VALUES ('2026-10-20T12:30:00-04:00', 7, 25.0, 'healthy')"""
        )

    Database(path).initialize()
    with sqlite3.connect(path) as connection:
        stored = connection.execute(
            "SELECT timestamp, sequence, cpu_percent FROM telemetry_samples"
        ).fetchone()
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    assert stored == ("2026-10-20T16:30:00+00:00", 7, 25.0)
    assert integrity == "ok"


def test_retention_keeps_rollup_until_its_coverage_ends(tmp_path):
    database = Database(tmp_path / "retention.db")
    database.initialize()
    bucket = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(hours=2)
    _insert_rollup(database, bucket)

    with sqlite3.connect(database.path) as connection:
        Database._prune_history_locked(
            connection, bucket + timedelta(minutes=30), max_samples=100
        )
    assert database.list_historical_samples(
        bucket, bucket + timedelta(hours=1)
    )

    with sqlite3.connect(database.path) as connection:
        Database._prune_history_locked(
            connection, bucket + timedelta(hours=1), max_samples=100
        )
    assert database.list_historical_samples(
        bucket, bucket + timedelta(hours=1)
    ) == []


def test_release_packager_excludes_live_desktop_telemetry():
    root = Path(__file__).resolve().parent.parent
    packaged = {path.relative_to(root).as_posix() for path in selected_files(root)}
    assert "docs/browser-desktop.png" not in packaged
    assert "docs/browser-demo-network-graph.png" in packaged
