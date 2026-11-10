from __future__ import annotations

import random
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest

from app.models import (
    HistoricalConnectionSummary,
    HistoricalProcessObservation,
    HistoricalSample,
)
from app.storage.database import Database

BASE = datetime(2026, 1, 1, tzinfo=UTC)
# A cutoff older than every row, so only the row caps can delete.
KEEP_ALL = BASE - timedelta(days=365)
CAPPED_TABLES = ("telemetry_samples", "connection_summaries", "process_observations")
INSERTS = {
    "telemetry_samples": (
        "INSERT INTO telemetry_samples (timestamp, sequence, collector_status) "
        "VALUES (?, ?, 'healthy')"
    ),
    "connection_summaries": (
        "INSERT INTO connection_summaries "
        "(timestamp, sequence, collection_status, lifecycle_confidence) "
        "VALUES (?, ?, 'healthy', 'high')"
    ),
    "process_observations": (
        "INSERT INTO process_observations "
        "(timestamp, sequence, pid, name, status, access_status) "
        "VALUES (?, ?, ?, 'synthetic.exe', 'running', 'available')"
    ),
}


def stamp(seconds: int) -> datetime:
    return BASE + timedelta(seconds=seconds)


def row_limit(table: str, max_samples: int) -> int:
    if table == "process_observations":
        return min(max_samples * 100, 2_000_000)
    return max_samples


def initialized(path) -> Database:
    database = Database(path)
    database.initialize()
    return database


def insert(connection: sqlite3.Connection, table: str, rows: list[tuple[int, int]]) -> None:
    """Insert (seconds, sequence) rows in the given order. Each sequence is unique."""
    if table == "process_observations":
        values = [(stamp(seconds).isoformat(), 1, sequence) for seconds, sequence in rows]
    else:
        values = [(stamp(seconds).isoformat(), sequence) for seconds, sequence in rows]
    connection.executemany(INSERTS[table], values)


def stored(path, table: str) -> list[tuple[str, int]]:
    """Stored (timestamp, sequence) pairs; process rows keep their sequence in pid."""
    column = "pid" if table == "process_observations" else "sequence"
    with closing(sqlite3.connect(path)) as connection:
        return sorted(
            connection.execute(f"SELECT timestamp, {column} FROM {table}").fetchall()  # noqa: S608
        )


def integrity(path) -> str:
    with closing(sqlite3.connect(path)) as connection:
        return connection.execute("PRAGMA integrity_check").fetchone()[0]


@pytest.mark.parametrize("table", CAPPED_TABLES)
def test_row_cap_keeps_the_newest_rows_by_timestamp_not_insertion_order(tmp_path, table):
    database = initialized(tmp_path / "cap.db")
    limit = row_limit(table, 3)
    order = list(range(limit + 40))
    random.Random(7).shuffle(order)
    with closing(sqlite3.connect(database.path)) as connection, connection:
        insert(connection, table, [(seconds, seconds) for seconds in order])
        Database._prune_history_locked(connection, KEEP_ALL, 3)

    assert [sequence for _, sequence in stored(database.path, table)] == list(
        range(40, limit + 40)
    )
    assert integrity(database.path) == "ok"


@pytest.mark.parametrize("table", CAPPED_TABLES)
@pytest.mark.parametrize("extra", [0, -1])
def test_row_cap_deletes_nothing_at_or_below_the_limit(tmp_path, table, extra):
    database = initialized(tmp_path / "cap.db")
    rows = [(seconds, seconds) for seconds in range(row_limit(table, 3) + extra)]
    with closing(sqlite3.connect(database.path)) as connection, connection:
        insert(connection, table, rows)
        Database._prune_history_locked(connection, KEEP_ALL, 3)

    assert len(stored(database.path, table)) == len(rows)


@pytest.mark.parametrize("seed", range(25))
def test_time_and_row_retention_keep_the_newest_rows_inside_the_window(tmp_path, seed):
    """Model check: each table keeps the newest rows at or after the cutoff, up to its cap.

    Timestamps repeat, as process rows of one sample do, and arrive out of order. Which
    of several rows sharing the boundary timestamp survives is not specified, so the
    model compares the timestamps kept rather than the rows.
    """
    rng = random.Random(seed)
    max_samples = rng.randint(1, 4)
    cutoff_seconds = rng.randint(0, 20)
    database = initialized(tmp_path / "model.db")
    expected: dict[str, list[str]] = {}
    with closing(sqlite3.connect(database.path)) as connection, connection:
        for table in CAPPED_TABLES:
            limit = row_limit(table, max_samples)
            count = rng.randint(0, limit * 2 + 30)
            rows = [(rng.randint(0, 60), sequence) for sequence in range(count)]
            insert(connection, table, rows)
            in_window = sorted(
                (
                    stamp(seconds).isoformat()
                    for seconds, _ in rows
                    if seconds >= cutoff_seconds
                ),
                reverse=True,
            )
            expected[table] = sorted(in_window[:limit])
        Database._prune_history_locked(connection, stamp(cutoff_seconds), max_samples)

    for table in CAPPED_TABLES:
        kept = [timestamp for timestamp, _ in stored(database.path, table)]
        assert kept == expected[table], table
    assert integrity(database.path) == "ok"


def vm_steps(connection: sqlite3.Connection, action) -> int:
    steps = 0

    def count_step() -> int:
        nonlocal steps
        steps += 1
        return 0

    connection.set_progress_handler(count_step, 1)
    try:
        action()
    finally:
        connection.set_progress_handler(None, 1)
    return steps


def test_row_cap_work_does_not_grow_with_the_rows_it_keeps(tmp_path):
    """The cap runs on every history write, every two seconds by default.

    Selecting every retained row ID and then scanning the table made each write cost
    grow with the table, even far below the cap. Removing the same 100 excess rows must
    take the same number of SQLite VM steps whether 1,000 or 20,000 rows are kept. The
    page walk inside COUNT(*) is a single VM step, so it is not measured here.
    """
    steps = {}
    for retained in (1_000, 20_000):
        database = initialized(tmp_path / f"{retained}.db")
        max_samples = retained // 100
        rows = [(seconds, seconds) for seconds in range(retained + 100)]
        with closing(sqlite3.connect(database.path)) as connection, connection:
            insert(connection, "process_observations", rows)
            steps[retained] = vm_steps(
                connection,
                lambda connection=connection, limit=max_samples: (
                    Database._prune_history_locked(connection, KEEP_ALL, limit)
                ),
            )
        kept = stored(database.path, "process_observations")
        assert [sequence for _, sequence in kept] == list(range(100, retained + 100))

    assert steps[20_000] <= steps[1_000] * 1.1, steps


def batch(seconds: int, sequence: int, processes: int):
    timestamp = datetime.now(UTC) + timedelta(seconds=seconds)
    return (
        HistoricalSample(timestamp=timestamp, sequence=sequence, collector_status="healthy"),
        [
            HistoricalProcessObservation(
                timestamp=timestamp, sequence=sequence, pid=pid, name="synthetic.exe",
                status="running",
            )
            for pid in range(processes)
        ],
        HistoricalConnectionSummary(
            timestamp=timestamp, sequence=sequence, collection_status="healthy",
            lifecycle_confidence="high",
        ),
    )


def fill_to_cap(database: Database, max_samples: int) -> None:
    """Store max_samples writes of 100 processes each: every table sits at its cap."""
    for sequence in range(max_samples):
        database.add_history_batch(*batch(sequence, sequence, 100), 168, max_samples)


def counts(path) -> dict[str, int]:
    with closing(sqlite3.connect(path)) as connection:
        return {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608
            for table in CAPPED_TABLES
        }


def test_history_write_at_the_cap_replaces_the_oldest_sample(tmp_path):
    database = initialized(tmp_path / "steady.db")
    fill_to_cap(database, 3)

    database.add_history_batch(*batch(10, 10, 100), 168, 3)

    assert counts(database.path) == {
        "telemetry_samples": 3,
        "connection_summaries": 3,
        "process_observations": 300,
    }
    with closing(sqlite3.connect(database.path)) as connection:
        assert {
            row[0] for row in connection.execute("SELECT sequence FROM process_observations")
        } == {1, 2, 10}
        assert [
            row[0]
            for row in connection.execute(
                "SELECT sequence FROM telemetry_samples ORDER BY timestamp"
            )
        ] == [1, 2, 10]
    assert integrity(database.path) == "ok"


def test_failed_row_cap_rolls_back_the_whole_history_write(tmp_path):
    database = initialized(tmp_path / "atomic.db")
    fill_to_cap(database, 3)
    before = stored(database.path, "telemetry_samples")
    with closing(sqlite3.connect(database.path)) as connection, connection:
        # Nothing is old enough for time retention, so the first process delete is the
        # row cap. It runs after the sample and summary caps have already deleted.
        connection.execute(
            "CREATE TRIGGER fail_process_cap BEFORE DELETE ON process_observations "
            "BEGIN SELECT RAISE(ABORT, 'synthetic cap failure'); END"
        )

    with pytest.raises(sqlite3.IntegrityError, match="synthetic cap failure"):
        database.add_history_batch(*batch(10, 10, 100), 168, 3)

    assert counts(database.path) == {
        "telemetry_samples": 3,
        "connection_summaries": 3,
        "process_observations": 300,
    }
    assert stored(database.path, "telemetry_samples") == before
    assert integrity(database.path) == "ok"
