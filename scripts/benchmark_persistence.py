"""Synthetic incident-link and storage-outage benchmark.

Run from the repository root with ``python -m scripts.benchmark_persistence``.
Every database is created under a disposable directory in the ignored ``.tmp``
folder, or under ``--workdir``, and removed afterwards. No host data is read.
The outage runs first and each archive is printed when it finishes, so a timeout
keeps earlier results. The default set took about 17 minutes on the audited
machine: ``--max-case-seconds`` cannot cut a case below ``MIN_SAMPLES``.
"""
from __future__ import annotations

import argparse
import json
import platform
import sqlite3
import statistics
import tempfile
import time
import tracemalloc
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

import app.service as service_module
from app.main import create_app
from app.models import Alert, EventRecord, Incident
from app.service import StorageUnavailableError
from app.storage.database import Database
from scripts.benchmark_endurance import percentile

# (incidents, alerts per incident). 100 is the IncidentCreate.alert_ids limit.
DEFAULT_ARCHIVES = (
    (100, 2),
    (100, 100),
    (1_000, 2),
    (1_000, 100),
    (10_000, 2),
    (10_000, 100),
    (100_000, 2),
)
# The default Settings.alert_retention, so each write pays its normal pruning.
RETAINED_ALERTS = 1_000
LINKED_ALERTS = 100
BATCH_SIZES = (1, 100)
DEFAULT_SAMPLES = 200
MIN_SAMPLES = 5
MAX_CASE_SECONDS = 5.0
OUTAGE_OVERFLOW = 100
NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def alert_id(incident: int, member: int) -> str:
    return f"synthetic-alert-{incident:08d}-{member:03d}"


def row_id(index: int) -> str:
    return f"synthetic-row-{index:08d}"


def linked_id(index: int) -> str:
    return f"synthetic-linked-{index:08d}"


def miss_id(index: int) -> str:
    return f"synthetic-missing-{index:08d}"


def synthetic_alert(identifier: str, **changes) -> Alert:
    values = {
        "id": identifier,
        "timestamp": NOW,
        "severity": "WARNING",
        "source": "synthetic-source",
        "metric": "synthetic-metric",
        "explanation": "synthetic",
    }
    return Alert(**(values | changes))


def synthetic_event(identifier: str) -> EventRecord:
    return EventRecord(id=identifier, timestamp=NOW, event_type="synthetic", title="synthetic")


def synthetic_incident(identifier: str, members: list[str]) -> Incident:
    return Incident(
        id=identifier,
        title="synthetic incident",
        severity="WARNING",
        detection_timestamp=NOW,
        first_observed=NOW,
        last_observed=NOW,
        related_alerts=members,
    )


def seed_archive(path: Path, incidents: int, members: int) -> None:
    """Bulk insert the archive, so seeding cost stays out of the measurements."""
    stamp = NOW.isoformat()

    def incident_rows() -> Iterable[tuple]:
        for index in range(incidents):
            related = json.dumps([alert_id(index, member) for member in range(members)])
            yield (
                f"INC-{index:08d}", "synthetic incident", "WARNING",
                stamp, stamp, stamp, related, "NEW",
            )

    def alert_rows() -> Iterable[tuple]:
        # The oldest rows are pruned first, so a sample's own row always survives.
        for index in range(RETAINED_ALERTS - LINKED_ALERTS):
            when = (NOW - timedelta(seconds=index + 1)).isoformat()
            yield row_id(index), when, None
        for index in range(LINKED_ALERTS):
            yield linked_id(index), stamp, "INC-00000000"

    connection = sqlite3.connect(path)
    try:
        with connection:
            connection.executemany(
                """INSERT INTO incidents
                (id, title, severity, detection_timestamp, first_observed, last_observed,
                 related_alerts, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                incident_rows(),
            )
            connection.executemany(
                """INSERT INTO alerts
                (id, timestamp, severity, source, metric, explanation, incident_id)
                VALUES (?, ?, 'WARNING', 'synthetic-source', 'synthetic-metric',
                        'synthetic', ?)""",
                alert_rows(),
            )
    finally:
        connection.close()


class PlanRecorder:
    """Run the application's own SQL, so a reported plan cannot drift from it."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.plans: list[str] = []

    def execute(self, sql: str, parameters: tuple = ()):
        rows = self.connection.execute(f"EXPLAIN QUERY PLAN {sql}", parameters)
        self.plans.extend(row[3] for row in rows)
        return self.connection.execute(sql, parameters)


def query_plans(path: Path) -> dict[str, list[str]]:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        unstored = PlanRecorder(connection)
        Database._incident_for_alert(unstored, miss_id(0), None)  # noqa: SLF001
        retention = PlanRecorder(connection)
        Database._enforce_retention(retention, RETAINED_ALERTS)  # noqa: SLF001
        connection.rollback()
        stored = PlanRecorder(connection)
        stored.execute("SELECT incident_id FROM alerts WHERE id = ?", (miss_id(0),))
        linking = PlanRecorder(connection)
        linking.execute(
            "UPDATE alerts SET incident_id = ? WHERE id IN (?, ?)",
            ("INC-00000000", miss_id(0), miss_id(1)),
        )
        connection.rollback()
    finally:
        connection.close()
    return {
        "_incident_for_alert (no stored link)": unstored.plans,
        "_enforce_retention (every add_alert)": retention.plans,
        "stored link read": stored.plans,
        "add_incident link update": linking.plans,
    }


def integrity_check(path: Path) -> str:
    connection = sqlite3.connect(path)
    try:
        return connection.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        connection.close()


def measure(
    run: Callable[[int], object],
    reset: Callable[[int], object],
    samples: int,
    max_seconds: float,
) -> dict[str, float | int]:
    """Time ``run`` alone; ``reset`` restores the archive between samples."""
    latencies: list[float] = []
    deadline = time.monotonic() + max_seconds
    for index in range(samples):
        started = time.perf_counter()
        run(index)
        latencies.append((time.perf_counter() - started) * 1000)
        reset(index)
        if len(latencies) >= MIN_SAMPLES and time.monotonic() >= deadline:
            break
    return {
        "samples": len(latencies),
        "p50_ms": round(statistics.median(latencies), 3),
        "p95_ms": round(percentile(latencies, 0.95), 3),
        "max_ms": round(max(latencies), 3),
    }


def execute_reset(path: Path, sql: str, parameters: Iterable[tuple]) -> None:
    connection = sqlite3.connect(path, timeout=5.0)
    try:
        with connection:
            connection.executemany(sql, parameters)
    finally:
        connection.close()


def run_archive(
    directory: Path,
    incidents: int,
    members: int,
    samples: int,
    max_seconds: float,
    batch_sizes: tuple[int, ...],
) -> dict[str, object]:
    path = directory / f"incidents-{incidents}-{members}.db"
    database = Database(path)
    database.initialize()
    seed_archive(path, incidents, members)
    size_bytes = path.stat().st_size

    # Scan order is newest first, so these bracket the JSON membership scan.
    every_member = [
        (incident, member) for incident in range(min(incidents, 100)) for member in range(members)
    ]
    oldest = [alert_id(*item) for item in every_member[:max(batch_sizes)]]
    newest = [
        alert_id(incident, member)
        for incident in range(incidents - 1, max(incidents - 101, -1), -1)
        for member in range(members)
    ][:max(batch_sizes)]

    def unstored(pool: list[str]) -> Callable[[int], str]:
        return lambda index: pool[index % len(pool)]

    id_sources: dict[str, Callable[[int], str]] = {
        "stored_link": lambda index: linked_id(index % LINKED_ALERTS),
        "member_newest": unstored(newest),
        "member_oldest": unstored(oldest),
        "miss": miss_id,
    }
    delete_alert = "DELETE FROM alerts WHERE id = ?"
    results: dict[str, dict[str, float | int]] = {}

    for case, source in id_sources.items():
        results[f"add_alert/{case}"] = measure(
            lambda index, source=source: database.add_alert(
                synthetic_alert(source(index)), RETAINED_ALERTS
            ),
            (
                (lambda index: None) if case == "stored_link"
                else lambda index, source=source: execute_reset(
                    path, delete_alert, [(source(index),)]
                )
            ),
            samples,
            max_seconds,
        )

    for batch in batch_sizes:
        for case, source in id_sources.items():
            identifiers = [source(index) for index in range(batch)]
            results[f"get_alert_incident_ids[{batch}]/{case}"] = measure(
                lambda index, identifiers=identifiers: database.get_alert_incident_ids(
                    identifiers
                ),
                lambda index: None,
                samples,
                max_seconds,
            )

    existing = [row_id(index) for index in range(members)]
    absent = [miss_id(index) for index in range(members)]
    for case, related in (("hit", existing), ("miss", absent)):
        results[f"add_incident/{case}"] = measure(
            lambda index, related=related: database.add_incident(
                synthetic_incident(f"INC-sample-{index}", related)
            ),
            lambda index, related=related: (
                execute_reset(
                    path, "DELETE FROM incidents WHERE id = ?", [(f"INC-sample-{index}",)]
                ),
                execute_reset(
                    path,
                    "UPDATE alerts SET incident_id = NULL WHERE id = ?",
                    [(item,) for item in related],
                ),
            ),
            samples,
            max_seconds,
        )

    # Every storage recovery attempt repeats this, including its integrity check.
    results["initialize"] = measure(
        lambda index: database.initialize(), lambda index: None, MIN_SAMPLES, max_seconds
    )

    tracemalloc.start()
    database.get_alert_incident_ids([miss_id(index) for index in range(max(batch_sizes))])
    _, traced_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return {
        "incidents": incidents,
        "members": members,
        "database_bytes": size_bytes,
        "traced_peak_bytes": traced_peak,
        "integrity_check": integrity_check(path),
        "plans": query_plans(path),
        "cases": results,
    }


def stored_ids(path: Path, table: str, prefix: str) -> list[str]:
    connection = sqlite3.connect(path)
    try:
        rows = connection.execute(
            f"SELECT id FROM {table} WHERE id LIKE ? ORDER BY rowid", (f"{prefix}%",)
        ).fetchall()
    finally:
        connection.close()
    return [row[0] for row in rows]


def occurrence_count(path: Path, identifier: str) -> int:
    connection = sqlite3.connect(path)
    try:
        return connection.execute(
            "SELECT occurrence_count FROM alerts WHERE id = ?", (identifier,)
        ).fetchone()[0]
    finally:
        connection.close()


def require(condition: bool, label: str) -> None:
    if not condition:
        raise RuntimeError(f"synthetic outage invariant failed: {label}")


def hold_write_lock(path: Path) -> sqlite3.Connection:
    """Cause a real SQLite write outage, instead of patching the database."""
    connection = sqlite3.connect(path, isolation_level=None, timeout=0)
    connection.execute("BEGIN EXCLUSIVE")
    return connection


def run_outage(directory: Path, overflow: int = OUTAGE_OVERFLOW) -> dict[str, object]:
    capacity = service_module.MAX_PENDING_WRITES
    path = directory / "outage.db"
    application = create_app(path, start_background=False)
    grouped = "synthetic-grouped"
    result: dict[str, object] = {"capacity": capacity, "overflow": overflow}

    with TestClient(application) as client:
        service = application.state.telemetry
        service._storage_retry_seconds = 0  # noqa: SLF001

        def call(function, *arguments):
            return client.portal.call(function, *arguments)

        def health() -> dict[str, object]:
            return client.get("/api/health").json()["storage"]

        async def observe_alert(item: Alert) -> None:
            service._remember_live_alert(item)  # noqa: SLF001
            await service._persist_alert(item)  # noqa: SLF001

        async def fill(prefix: str, final: Alert | None) -> None:
            for index in range(capacity + overflow - (final is not None)):
                await observe_alert(synthetic_alert(f"{prefix}-alert-{index:04d}"))
            if final is not None:
                await observe_alert(final)
            for index in range(capacity + overflow):
                await service._persist_event(  # noqa: SLF001
                    synthetic_event(f"{prefix}-event-{index:04d}")
                )

        def grouped_alert() -> Alert:
            return synthetic_alert(grouped, rule_id="collection_failure", occurrence_count=1)

        call(observe_alert, grouped_alert())
        require(occurrence_count(path, grouped) == 1, "first observation committed once")
        require(health()["dropped_alert_writes"] == 0, "no loss before the outage")

        # Outage past both queue limits.
        lock = hold_write_lock(path)
        tracemalloc.start()
        started = time.perf_counter()
        call(fill, "first", grouped_alert())
        result["fill_seconds"] = round(time.perf_counter() - started, 3)
        _, result["fill_traced_peak_bytes"] = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        during = health()
        require(during["status"] == "degraded", "health reports the outage")
        require(during["pending_writes"] == capacity * 2, "queues stop at their limit")
        require(during["dropped_alert_writes"] == overflow, "alert evictions are counted")
        require(during["dropped_event_writes"] == overflow, "event evictions are counted")
        result["during_outage"] = {
            key: during[key]
            for key in (
                "status", "pending_writes", "dropped_alert_writes", "dropped_event_writes",
                "last_error_type",
            )
        }

        # A retry and an opt-out both fail while the lock is held, and lose nothing.
        call(service._maybe_recover_storage)  # noqa: SLF001
        require(health()["status"] == "degraded", "failed retry stays degraded")
        off = service.settings.model_copy(update={"event_logging_enabled": False})
        try:
            call(service.update_settings, off)
        except StorageUnavailableError:
            pass
        else:
            raise RuntimeError("synthetic outage invariant failed: unsaved opt-out accepted")
        require(service.settings.event_logging_enabled, "unsaved opt-out is not applied")
        require(health()["pending_writes"] == capacity * 2, "failed attempts keep the queue")
        lock.close()

        # Recovery writes what was retained, oldest first.
        started = time.perf_counter()
        call(service._maybe_recover_storage)  # noqa: SLF001
        result["recovery_seconds"] = round(time.perf_counter() - started, 3)
        after = health()
        require(after["status"] == "healthy", "storage recovers")
        require(after["pending_writes"] == 0, "queues drain")
        require(after["dropped_alert_writes"] == overflow, "alert count survives recovery")
        require(after["dropped_event_writes"] == overflow, "event count survives recovery")
        alerts = stored_ids(path, "alerts", "first-alert-")
        events = stored_ids(path, "events", "first-event-")
        require(
            alerts == [f"first-alert-{i:04d}" for i in range(overflow, capacity + overflow - 1)],
            "retained alerts are stored oldest first, without the evicted ones",
        )
        require(
            events == [f"first-event-{i:04d}" for i in range(overflow, capacity + overflow)],
            "retained events are stored oldest first, without the evicted ones",
        )
        require(occurrence_count(path, grouped) == 2, "queued delta applied once")
        notices = [e for e in service.live_events if e.event_type == "storage_recovery"]
        require(bool(notices), "recovery notice stays in the live view")

        # Further passes replay nothing, and a new detection counts exactly once.
        call(service._maybe_recover_storage)  # noqa: SLF001
        call(service._flush_pending)  # noqa: SLF001
        require(occurrence_count(path, grouped) == 2, "no replay of a committed observation")
        require(stored_ids(path, "alerts", "first-alert-") == alerts, "no alert replay")
        require(stored_ids(path, "events", "first-event-") == events, "no event replay")
        call(observe_alert, grouped_alert())
        require(occurrence_count(path, grouped) == 3, "a new detection increments once")
        result["after_recovery"] = {
            "status": after["status"],
            "pending_writes": after["pending_writes"],
            "stored_alerts": len(alerts) + 1,
            "stored_events": len(events),
            "grouped_occurrences": occurrence_count(path, grouped),
        }

        # A saved opt-out discards the queue without calling it loss, and nothing returns.
        lock = hold_write_lock(path)
        call(fill, "second", None)
        require(health()["pending_writes"] == capacity * 2, "second outage fills the queues")
        lock.close()
        call(service.update_settings, off)
        opted_out = health()
        require(opted_out["pending_writes"] == 0, "opt-out discards the queue")
        require(opted_out["dropped_alert_writes"] == overflow * 2, "discard is not an alert loss")
        require(opted_out["dropped_event_writes"] == overflow * 2, "discard is not an event loss")
        call(observe_alert, synthetic_alert("second-alert-after-opt-out"))
        call(service._maybe_recover_storage)  # noqa: SLF001
        on = service.settings.model_copy(update={"event_logging_enabled": True})
        call(service.update_settings, on)
        call(service._flush_pending)  # noqa: SLF001
        require(not stored_ids(path, "alerts", "second-"), "no discarded alert is stored")
        require(not stored_ids(path, "events", "second-"), "no discarded event is stored")
        result["after_opt_out"] = {
            "pending_writes": opted_out["pending_writes"],
            "dropped_alert_writes": opted_out["dropped_alert_writes"],
            "dropped_event_writes": opted_out["dropped_event_writes"],
            "stored_after_opt_out": 0,
        }

    result["integrity_check"] = integrity_check(path)
    require(result["integrity_check"] == "ok", "integrity_check")
    return result


def run(
    directory: Path,
    archives: tuple[tuple[int, int], ...] = DEFAULT_ARCHIVES,
    samples: int = DEFAULT_SAMPLES,
    max_seconds: float = MAX_CASE_SECONDS,
    batch_sizes: tuple[int, ...] = BATCH_SIZES,
    outage: bool = True,
    on_archive: Callable[[dict[str, object]], object] = lambda archive: None,
    on_outage: Callable[[dict[str, object]], object] = lambda result: None,
) -> dict[str, object]:
    started = time.perf_counter()
    report: dict[str, object] = {
        "environment": {
            "platform": platform.platform(terse=True),
            "python": platform.python_version(),
            "sqlite": sqlite3.sqlite_version,
        },
        "settings": {
            "retained_alerts": RETAINED_ALERTS,
            "samples": samples,
            "min_samples": MIN_SAMPLES,
            "max_case_seconds": max_seconds,
        },
        "archives": [],
    }
    if outage:
        report["outage"] = run_outage(directory)
        on_outage(report["outage"])
    for incidents, members in archives:
        archive = run_archive(directory, incidents, members, samples, max_seconds, batch_sizes)
        if archive["integrity_check"] != "ok":
            raise RuntimeError("synthetic archive failed PRAGMA integrity_check")
        report["archives"].append(archive)
        on_archive(archive)
    report["total_seconds"] = round(time.perf_counter() - started, 1)
    return report


def print_archive(archive: dict[str, object]) -> None:
    """Print as each archive finishes, so a timeout keeps the earlier results."""
    for case, values in archive["cases"].items():
        print(
            f"| {archive['incidents']:,} | {archive['members']} | `{case}` "
            f"| {values['samples']} | {values['p50_ms']} | {values['p95_ms']} "
            f"| {values['max_ms']} |"
        )
    print(
        f"archive incidents={archive['incidents']} members={archive['members']} "
        f"database_bytes={archive['database_bytes']} "
        f"traced_peak_bytes={archive['traced_peak_bytes']} "
        f"integrity_check={archive['integrity_check']}",
        flush=True,
    )


def print_outage(result: dict[str, object]) -> None:
    print("outage=" + json.dumps(result, indent=2), flush=True)


def print_summary(report: dict[str, object]) -> None:
    print(json.dumps({key: report[key] for key in ("environment", "settings")}))
    print("plans=" + json.dumps(report["archives"][-1]["plans"], indent=2))
    print(f"total_seconds={report['total_seconds']}")


def parse_archive(value: str) -> tuple[int, int]:
    incidents, members = value.split(":", 1)
    return int(incidents), int(members)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--archive", action="append", type=parse_archive, metavar="INCIDENTS:MEMBERS",
        help="archive to measure; repeatable. Defaults to the documented set.",
    )
    parser.add_argument("--samples", type=int, default=DEFAULT_SAMPLES)
    parser.add_argument("--max-case-seconds", type=float, default=MAX_CASE_SECONDS)
    parser.add_argument("--skip-outage", action="store_true")
    parser.add_argument(
        "--workdir", type=Path, default=Path(".tmp"),
        help="parent of the disposable directory; keep it on an ignored path.",
    )
    options = parser.parse_args()
    options.workdir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="benchmark-persistence-", dir=options.workdir, ignore_cleanup_errors=True
    ) as directory:
        print("| incidents | members | case | samples | p50 ms | p95 ms | max ms |")
        print("| --- | --- | --- | --- | --- | --- | --- |", flush=True)
        print_summary(
            run(
                Path(directory),
                tuple(options.archive or DEFAULT_ARCHIVES),
                options.samples,
                options.max_case_seconds,
                outage=not options.skip_outage,
                on_archive=print_archive,
                on_outage=print_outage,
            )
        )


if __name__ == "__main__":
    main()
