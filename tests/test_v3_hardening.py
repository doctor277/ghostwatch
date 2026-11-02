from __future__ import annotations

import asyncio
import socket
import sqlite3
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.models import HistoricalSample
from app.monitoring.network import NetworkCollector
from app.monitoring.processes import ProcessCollector
from app.service import TelemetryService
from app.storage.database import Database


class StaticCollector:
    def __init__(self, value):
        self.value = value
        self.connection_counts_by_pid = {}
        self.connection_rankings = []
        self.remote_endpoints_by_pid = {}
        self.recently_closed = []

    def collect(self, *_args):
        return self.value


def test_schema_v3_migration_rolls_back_all_ddl_on_failure(tmp_path, monkeypatch):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.execute("INSERT INTO schema_meta VALUES ('schema_version', '2')")
        connection.execute(
            "CREATE TABLE settings (id INTEGER PRIMARY KEY, payload TEXT NOT NULL)"
        )

    def injected_failure(_connection):
        raise sqlite3.OperationalError("injected migration failure")

    monkeypatch.setattr(Database, "_ensure_alert_columns", staticmethod(injected_failure))
    with pytest.raises(sqlite3.OperationalError, match="injected migration failure"):
        Database(path).initialize()

    with sqlite3.connect(path) as connection:
        version = connection.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()[0]
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert version == "2"
    assert "telemetry_samples" not in tables
    assert len(list((tmp_path / "backups").glob("legacy-pre-v3-from-v2-*.db"))) == 1


def test_corrupted_history_metadata_recovers_without_exposing_payload(tmp_path, caplog):
    database = Database(tmp_path / "corrupt-history.db")
    database.initialize()
    timestamp = datetime.now(UTC)
    with sqlite3.connect(database.path) as connection:
        connection.execute(
            """INSERT INTO telemetry_samples
            (timestamp, sequence, collector_status, unavailable_metrics)
            VALUES (?, 1, 'impossible-private-status', 'secret-invalid-json')""",
            (timestamp.isoformat(),),
        )

    rows = database.list_historical_samples(timestamp, timestamp)

    assert rows[0].collector_status == "degraded"
    assert rows[0].unavailable_metrics == ["history_metadata_corrupt"]
    assert "secret-invalid-json" not in caplog.text
    assert "impossible-private-status" not in caplog.text


def test_corrupted_sqlite_file_is_not_overwritten(tmp_path):
    path = tmp_path / "broken.db"
    original = b"not-a-sqlite-database-private-content"
    path.write_bytes(original)

    with pytest.raises(sqlite3.DatabaseError):
        Database(path).initialize()

    assert path.read_bytes() == original


@pytest.mark.asyncio
async def test_history_database_outage_does_not_stop_monitoring_and_recovers(
    tmp_path, system_sample, network_sample, monkeypatch
):
    database = Database(tmp_path / "outage.db")
    service = TelemetryService(database, storage_retry_seconds=0)
    assert await service.initialize_storage()
    service.system_collector = StaticCollector(system_sample)
    service.network_collector = StaticCollector((network_sample, []))
    service.process_collector = StaticCollector([])
    original = database.add_history_batch
    attempts = 0

    def fail_once(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise sqlite3.OperationalError("database unavailable private detail")
        return original(*args, **kwargs)

    monkeypatch.setattr(database, "add_history_batch", fail_once)
    worker = asyncio.create_task(service._history_writer())
    try:
        first = await service.collect_once()
        await asyncio.wait_for(service._history_queue.join(), timeout=2)
        assert first.sequence == 1
        assert service.storage.status == "degraded"
        second = await service.collect_once()
        await asyncio.wait_for(service._history_queue.join(), timeout=2)
        assert second.sequence == 2
        assert service.storage.status == "healthy"
        assert database.telemetry_sample_count() == 1
    finally:
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker


def test_process_pid_reuse_clears_stale_identity_when_access_is_limited():
    class Process:
        def __init__(self, created: float, executable: str | None):
            self.info = {"pid": 44, "name": "worker.exe"}
            self.created = created
            self.executable = executable

        def create_time(self):
            return self.created

        def cpu_percent(self):
            return 1

        def memory_percent(self):
            return 1

        def memory_info(self):
            return SimpleNamespace(rss=100)

        def status(self):
            return "running"

        def ppid(self):
            return 1

        def exe(self):
            if self.executable is None:
                raise PermissionError
            return self.executable

        def username(self):
            if self.executable is None:
                raise PermissionError
            return "synthetic"

    class Processes:
        current = Process(1000, "C:\\old.exe")

        @staticmethod
        def cpu_count():
            return 1

        @classmethod
        def process_iter(cls, **_kwargs):
            return [cls.current]

    collector = ProcessCollector(
        Processes, metric_budget=1, identity_budget=1, sampling_interval_seconds=0
    )
    assert collector.collect([])[0].executable == "C:\\old.exe"
    Processes.current = Process(2000, None)

    reused = collector.collect([])[0]

    assert reused.create_time == datetime.fromtimestamp(2000, UTC)
    assert reused.executable is None
    assert "executable" in reused.unavailable_fields


def test_connection_churn_keeps_all_internal_collections_bounded():
    class Connections:
        cycle = 0

        @staticmethod
        def net_io_counters():
            return SimpleNamespace(bytes_sent=1, bytes_recv=1)

        @classmethod
        def net_connections(cls, kind):
            base = cls.cycle * 600
            cls.cycle += 1
            return [
                SimpleNamespace(
                    type=socket.SOCK_STREAM,
                    status="ESTABLISHED",
                    pid=index % 25 + 1,
                    laddr=("127.0.0.1", 10_000 + index),
                    raddr=("203.0.113.10", 443),
                )
                for index in range(base, base + 600)
            ]

        @staticmethod
        def Process(pid):
            return SimpleNamespace(name=lambda: f"process-{pid}.exe")

    tick = iter(range(1, 52))
    collector = NetworkCollector(
        Connections, clock=lambda: float(next(tick)), lifecycle_limit=500
    )
    for _ in range(50):
        sample, details = collector.collect()
        assert sample.active_connections == 600
        assert len(details) == 250
        assert len(collector._seen) <= 500
        assert len(collector.recently_closed) <= 100
        assert len(collector._name_cache) <= 25


def test_complete_churn_counts_all_closures_but_builds_only_bounded_details():
    class Connections:
        cycle = 0

        @staticmethod
        def net_io_counters():
            return SimpleNamespace(bytes_sent=1, bytes_recv=1)

        @classmethod
        def net_connections(cls, kind):
            base = cls.cycle * 600
            cls.cycle += 1
            return [
                SimpleNamespace(
                    type=socket.SOCK_STREAM,
                    status="ESTABLISHED",
                    pid=1,
                    laddr=("127.0.0.1", 10_000 + index),
                    raddr=("203.0.113.10", 443),
                )
                for index in range(base, base + 600)
            ]

        @staticmethod
        def Process(pid):
            return SimpleNamespace(name=lambda: "process.exe")

    tick = iter((1.0, 2.0))
    collector = NetworkCollector(
        Connections, clock=lambda: next(tick), lifecycle_limit=1000
    )
    collector.collect()
    sample, _ = collector.collect()

    assert sample.disappeared_connections == 600
    assert len(collector.recently_closed) == 100


def test_history_retention_prunes_related_tables(tmp_path):
    database = Database(tmp_path / "retention.db")
    database.initialize()
    old = datetime(2020, 1, 1, tzinfo=UTC)
    database.add_telemetry_sample(
        HistoricalSample(
            timestamp=old,
            sequence=1,
            collector_status="healthy",
        ),
        retention_hours=24,
    )
    database.prune_telemetry(24, 1000)

    with sqlite3.connect(database.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM telemetry_samples").fetchone()[0] == 0
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
