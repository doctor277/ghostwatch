from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.models import (
    EventRecord,
    FileBaselineEntry,
    FileIntegritySettings,
    MonitoredFolder,
    WindowsEventSettings,
)
from app.monitoring.file_integrity import FileIntegrityCollector
from app.monitoring.windows_events import SYSTEM_CHANNEL, WindowsEventCollector
from app.reporting import report_payload
from app.service import TelemetryService
from app.storage.database import Database, TimestampNormalizationCollisionError
from scripts.package_release import (
    REDACTED_HOME,
    UNIX_PROFILE,
    WINDOWS_PROFILE,
    build,
    scan_sensitive_payloads,
)


def _sidecars(path: Path) -> dict[str, tuple[bytes, int]]:
    watched = [path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")]
    return {
        item.name: (item.read_bytes(), item.stat().st_mtime_ns)
        for item in watched
        if item.exists()
    }


def _folder(path: Path) -> MonitoredFolder:
    now = datetime.now(UTC)
    return MonitoredFolder(
        id="synthetic-folder",
        path=str(path),
        created_at=now,
        baseline_created_at=now,
        status="healthy",
    )


def _make_directory_symlink(target: Path, link: Path) -> bool:
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        assert getattr(exc, "winerror", None) in {5, 1314}
        return False
    return True


def test_release_archive_redacts_profiles_without_altering_private_evidence(tmp_path):
    root = tmp_path / "source"
    docs = root / "docs"
    private = docs / "audit-evidence"
    docs.mkdir(parents=True)
    private.mkdir()
    personal = str(Path.home() / "private" / "evidence.txt")
    public_document = docs / "summary.md"
    raw_document = private / "raw.txt"
    public_document.write_text(f"Command output: {personal}\n", encoding="utf-8")
    raw_document.write_text(f"Original private evidence: {personal}\n", encoding="utf-8")
    (docs / "browser-desktop.png").write_bytes(b"live telemetry")
    (docs / "browser-demo-network-graph.png").write_bytes(b"synthetic demo")
    destination = tmp_path / "release.zip"

    count, _digest = build(root, destination)

    assert count == 2
    assert personal in public_document.read_text(encoding="utf-8")
    assert personal in raw_document.read_text(encoding="utf-8")
    with zipfile.ZipFile(destination) as archive:
        names = set(archive.namelist())
        summary = archive.read("docs/summary.md").decode("utf-8")
    assert REDACTED_HOME in summary
    assert personal not in summary
    assert "docs/audit-evidence/raw.txt" not in names
    assert "docs/browser-desktop.png" not in names
    assert "docs/browser-demo-network-graph.png" in names


def test_release_archive_fails_closed_for_unapproved_binary_asset(tmp_path):
    root = tmp_path / "source"
    docs = root / "docs"
    docs.mkdir(parents=True)
    (docs / "unreviewed-screenshot.png").write_bytes(b"unknown provenance")

    with pytest.raises(RuntimeError, match="Unapproved binary release asset"):
        build(root, tmp_path / "release.zip")


def test_release_scanner_still_rejects_credentials_after_path_redaction():
    synthetic_secret = ("sk-" + "A" * 32).encode()
    with pytest.raises(RuntimeError, match="OpenAI-style key"):
        scan_sensitive_payloads({"docs/public.md": synthetic_secret})


def test_release_scanner_detects_unsanitized_cross_platform_profile_paths():
    with pytest.raises(RuntimeError, match="user profile path"):
        scan_sensitive_payloads(
            {
                "docs/windows.md": rb"C:\Users\private-account\evidence.txt",
                "docs/unix.md": b"/home/private-account/evidence.txt",
            }
        )


def test_release_scanner_checks_binary_payload_names_before_text_decode(monkeypatch):
    monkeypatch.setenv("USERNAME", "CaseyRC4")
    monkeypatch.setenv("USER", "")

    with pytest.raises(RuntimeError, match="local username in archive path"):
        scan_sensitive_payloads({"assets/CaseyRC4.bin": b"\xff\xfe\x00"})


def test_project_release_payload_has_no_profiles_or_private_diagnostics(tmp_path):
    root = Path(__file__).resolve().parent.parent
    destination = tmp_path / "ghostwatch-privacy-test.zip"
    build(root, destination)

    with zipfile.ZipFile(destination) as archive:
        names = set(archive.namelist())
        payloads = {name: archive.read(name) for name in names}
    scan_sensitive_payloads(payloads)
    assert not any(name.startswith("docs/audit-evidence/") for name in names)
    assert not any(name.startswith("docs/review/") for name in names)
    assert "REVIEW_EVIDENCE.md" not in names
    assert "REVIEW_PACKET.md" not in names
    assert "docs/browser-desktop.png" not in names
    for name, data in payloads.items():
        if Path(name).suffix.lower() not in {".md", ".rst", ".txt"}:
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        assert not WINDOWS_PROFILE.search(text), name
        assert not UNIX_PROFILE.search(text), name


def test_timestamp_collision_aborts_before_touching_database_or_wal(tmp_path):
    path = tmp_path / "collision.db"
    database = Database(path)
    database.initialize()
    with sqlite3.connect(path) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.executemany(
            """INSERT INTO telemetry_samples
            (timestamp, sequence, cpu_percent, collector_status)
            VALUES (?, 7, ?, 'healthy')""",
            [
                ("2026-01-15T12:00:00+00:00", 10.0),
                ("2026-01-15T07:00:00-05:00", 20.0),
            ],
        )
        writer.commit()
        before = _sidecars(path)

        with pytest.raises(
            TimestampNormalizationCollisionError,
            match=r"telemetry_samples rowids .*No migration changes were applied",
        ):
            Database(path).initialize()

        after = _sidecars(path)
        rows = writer.execute(
            "SELECT timestamp, sequence, cpu_percent FROM telemetry_samples ORDER BY rowid"
        ).fetchall()
    assert before == after
    assert rows == [
        ("2026-01-15T12:00:00+00:00", 7, 10.0),
        ("2026-01-15T07:00:00-05:00", 7, 20.0),
    ]


def test_timestamp_collision_recovery_preserves_both_measurements(tmp_path):
    path = tmp_path / "collision-recovery.db"
    database = Database(path)
    database.initialize()
    with sqlite3.connect(path) as connection:
        connection.executemany(
            """INSERT INTO telemetry_samples
            (timestamp, sequence, cpu_percent, collector_status)
            VALUES (?, 7, ?, 'healthy')""",
            [
                ("2026-01-15T12:00:00+00:00", 10.0),
                ("2026-01-15T07:00:00-05:00", 20.0),
            ],
        )

    with pytest.raises(TimestampNormalizationCollisionError):
        Database(path).initialize()
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE telemetry_samples SET sequence = 8 "
            "WHERE timestamp = '2026-01-15T07:00:00-05:00'"
        )

    Database(path).initialize()
    with sqlite3.connect(path) as connection:
        rows = connection.execute(
            "SELECT timestamp, sequence, cpu_percent FROM telemetry_samples "
            "ORDER BY sequence"
        ).fetchall()
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    assert rows == [
        ("2026-01-15T12:00:00+00:00", 7, 10.0),
        ("2026-01-15T12:00:00+00:00", 8, 20.0),
    ]
    assert integrity == "ok"


def test_rollup_timestamp_collision_is_diagnostic_and_atomic(tmp_path):
    path = tmp_path / "rollup-collision.db"
    database = Database(path)
    database.initialize()
    with sqlite3.connect(path) as connection:
        connection.executemany(
            """INSERT INTO telemetry_rollups
            (bucket_start, bucket_end, resolution_seconds, sample_count,
             cpu_percent, collector_status)
            VALUES (?, ?, 3600, 1, ?, 'healthy')""",
            [
                ("2026-01-15T12:00:00+00:00", "2026-01-15T13:00:00+00:00", 10.0),
                ("2026-01-15T07:00:00-05:00", "2026-01-15T08:00:00-05:00", 20.0),
            ],
        )
    before = path.read_bytes()

    with pytest.raises(
        TimestampNormalizationCollisionError, match="telemetry_rollups rowids"
    ):
        Database(path).initialize()

    assert path.read_bytes() == before
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM telemetry_rollups").fetchone()[0] == 2
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_file_integrity_rejects_symlink_root_but_accepts_explicit_target(
    tmp_path, monkeypatch
):
    target = tmp_path / "target"
    link = tmp_path / "linked-root"
    target.mkdir()
    (target / "safe.txt").write_text("synthetic", encoding="utf-8")
    linked = _make_directory_symlink(target, link)
    if not linked:
        monkeypatch.setattr(Path, "is_symlink", lambda selected: selected == link)
    collector = FileIntegrityCollector()

    with pytest.raises(ValueError, match="symbolic link or junction"):
        collector.validate_folder(link)
    assert collector.validate_folder(target) == target.resolve()


def test_internal_directory_symlink_is_skipped_without_false_deletion(
    tmp_path, monkeypatch
):
    root = tmp_path / "watched"
    external = tmp_path / "external"
    root.mkdir()
    external.mkdir()
    (root / "kept.txt").write_text("kept", encoding="utf-8")
    (external / "outside.txt").write_text("outside", encoding="utf-8")
    link = root / "external-link"
    linked = _make_directory_symlink(external, link)
    if not linked:
        link.mkdir()
        (link / "outside.txt").write_text("outside", encoding="utf-8")
        monkeypatch.setattr(Path, "is_symlink", lambda selected: selected == link)
    collector = FileIntegrityCollector()
    result = collector.scan(
        _folder(root), {}, FileIntegritySettings(max_files_per_folder=100), emit_events=False
    )

    assert result.status == "healthy"
    assert [entry.relative_path for entry in result.entries] == ["kept.txt"]


def test_file_integrity_rejects_root_reached_through_symlink_parent(
    tmp_path, monkeypatch
):
    target = tmp_path / "target"
    watched = target / "watched"
    alias = tmp_path / "alias"
    target.mkdir()
    watched.mkdir()
    linked = _make_directory_symlink(target, alias)
    if not linked:
        monkeypatch.setattr(Path, "is_symlink", lambda selected: selected == alias)

    with pytest.raises(ValueError, match="enroll its resolved target explicitly"):
        FileIntegrityCollector.validate_folder(alias / "watched")


def test_file_integrity_scan_interruption_preserves_previous_baseline(
    tmp_path, monkeypatch
):
    root = tmp_path / "watched"
    root.mkdir()
    target = root / "stable.txt"
    target.write_text("stable", encoding="utf-8")
    previous = FileBaselineEntry(
        relative_path="stable.txt",
        sha256="a" * 64,
        size=6,
        modified_ns=target.stat().st_mtime_ns,
    )

    def interrupted_walk(*_args, **_kwargs):
        yield str(root), [], ["stable.txt"]
        raise OSError("synthetic enumeration interruption")

    monkeypatch.setattr(os, "walk", interrupted_walk)
    result = FileIntegrityCollector().scan(
        _folder(root),
        {previous.relative_path: previous},
        FileIntegritySettings(max_files_per_folder=100),
    )

    assert result.status == "unavailable"
    assert result.events == []
    assert result.entries == [previous]
    assert "Folder enumeration failed" in result.status_detail


def test_compacted_key_retention_stays_bounded_with_rollups(tmp_path):
    database = Database(tmp_path / "retention.db")
    database.initialize()
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    for sequence in range(100):
        observed = now - timedelta(hours=10) + timedelta(seconds=sequence)
        with sqlite3.connect(database.path) as connection:
            connection.execute(
                """INSERT INTO telemetry_samples
                (timestamp, sequence, cpu_percent, collector_status)
                VALUES (?, ?, 10.0, 'healthy')""",
                (observed.isoformat(), sequence),
            )
    assert database.compact_history(1, 24, now) == 100
    with sqlite3.connect(database.path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM telemetry_compacted_keys"
        ).fetchone()[0] == 100

    database.compact_history(1, 1, now + timedelta(hours=12))
    with sqlite3.connect(database.path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM telemetry_rollups"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM telemetry_compacted_keys"
        ).fetchone()[0] == 0


@pytest.mark.asyncio
async def test_storage_initialization_failure_recovers_without_private_diagnostics(
    tmp_path, monkeypatch
):
    database = Database(tmp_path / "storage-recovery.db")
    database.initialize()
    service = TelemetryService(database, storage_retry_seconds=0)
    original_initialize = database.initialize

    def locked():
        raise sqlite3.OperationalError(
            r"database is locked at C:\Users\private-account\telemetry.db"
        )

    monkeypatch.setattr(database, "initialize", locked)
    assert await service.initialize_storage() is False
    assert service.storage.status == "degraded"
    assert service.storage.last_error_type == "OperationalError"
    assert "private-account" not in json.dumps(
        [alert.model_dump(mode="json") for alert in service.live_alerts]
    )

    monkeypatch.setattr(database, "initialize", original_initialize)
    assert await service.initialize_storage() is True
    assert service.storage.status == "healthy"
    assert service.storage.persistence_available is True


def test_windows_event_naive_timestamp_is_degraded_and_cursor_is_retained():
    xml = """
    <Events><Event xmlns="http://schemas.microsoft.com/win/2004/08/events/event">
      <System><Provider Name="Synthetic"/><EventID>1</EventID><Level>4</Level>
      <TimeCreated SystemTime="2026-10-22T10:00:00"/>
      <EventRecordID>42</EventRecordID></System>
    </Event></Events>
    """

    def runner(command, **_kwargs):
        return subprocess.CompletedProcess(command, 0, stdout=xml, stderr="")

    settings = WindowsEventSettings(
        defender_enabled=False,
        application_enabled=False,
        max_events_per_channel=25,
    )
    batch = WindowsEventCollector(runner, platform_name="nt").collect(
        {SYSTEM_CHANNEL: 41}, settings
    )

    assert batch.events == []
    assert batch.channels[0].status == "degraded"
    assert batch.channels[0].cursor == 41
    assert "cursor retained" in batch.channels[0].detail


def test_redacted_report_does_not_expose_profile_paths_or_accounts():
    event = EventRecord(
        id="synthetic-event",
        timestamp=datetime.now(UTC),
        event_type="synthetic",
        severity="LOW",
        title="Synthetic event",
        summary=(
            r"Account DOMAIN\private-account read "
            r"C:\Users\private-account\Documents\evidence.txt"
        ),
    )
    payload = report_payload(
        "daily-summary", None, [event], [], redact=True, redaction_level="strict"
    )
    serialized = json.dumps(payload)

    assert "private-account" not in serialized
    assert "C:\\Users" not in serialized
    assert "[REDACTED_PATH]" in serialized
