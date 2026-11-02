from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import tempfile
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Lock

from pydantic import BaseModel

from app.models import (
    AISettings,
    Alert,
    AlertUpdate,
    DetectionSettings,
    EventRecord,
    FileBaselineEntry,
    FileIntegrityEvent,
    FileIntegritySettings,
    FileScanResult,
    HistoricalConnectionSummary,
    HistoricalProcessObservation,
    HistoricalSample,
    HistorySettings,
    Incident,
    IncidentUpdate,
    MonitoredFolder,
    Settings,
    WindowsEventRecord,
    WindowsEventSettings,
)
from app.time_utils import as_utc, parse_utc, utc_iso

LOGGER = logging.getLogger(__name__)
SCHEMA_VERSION = 3
ROLLUP_METRICS = (
    "cpu_percent",
    "memory_percent",
    "disk_percent",
    "upload_mbps",
    "download_mbps",
    "active_connections",
    "established_connections",
    "process_count",
)
TIMESTAMP_COLUMNS = {
    "alerts": ("timestamp", "first_occurrence", "last_occurrence", "last_recovery"),
    "events": ("timestamp",),
    "incidents": ("detection_timestamp", "first_observed", "last_observed"),
    "telemetry_samples": ("timestamp",),
    "telemetry_compacted_keys": ("timestamp", "bucket_start"),
    "process_observations": ("timestamp", "create_time"),
    "connection_summaries": ("timestamp",),
    "telemetry_rollups": ("bucket_start", "bucket_end"),
    "baseline_state": ("updated_at",),
    "windows_events": ("timestamp",),
    "windows_event_cursors": ("updated_at",),
    "monitored_folders": ("created_at", "baseline_created_at"),
    "file_integrity_events": ("timestamp",),
}
NORMALIZED_UNIQUE_KEYS = {
    "telemetry_samples": ("timestamp", "sequence"),
    "telemetry_compacted_keys": ("timestamp", "sequence"),
    "process_observations": ("timestamp", "sequence", "pid", "create_time"),
    "connection_summaries": ("timestamp", "sequence"),
    "telemetry_rollups": ("bucket_start",),
}


class UnsupportedSchemaVersionError(RuntimeError):
    """Raised before writes when a database requires a newer GhostWatch schema."""


class TimestampNormalizationCollisionError(RuntimeError):
    """Raised before migration when canonical UTC keys would collide."""


class Database:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = Lock()

    def initialize(self) -> int:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        previous_version = self._existing_schema_version()
        if previous_version > SCHEMA_VERSION:
            raise UnsupportedSchemaVersionError(
                f"Database schema version {previous_version} is newer than supported "
                f"version {SCHEMA_VERSION}"
            )
        self._preflight_timestamp_normalization()
        if 0 < previous_version < SCHEMA_VERSION:
            self._pre_migration_backup(previous_version)
        with self._lock, self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.executescript(
                """
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS settings (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS configuration (
                    key TEXT PRIMARY KEY,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS alerts (
                    id TEXT PRIMARY KEY,
                    timestamp TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    source TEXT NOT NULL,
                    metric TEXT NOT NULL,
                    explanation TEXT NOT NULL,
                    acknowledged INTEGER NOT NULL DEFAULT 0,
                    dismissed INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS events (
                    id TEXT PRIMARY KEY,
                    timestamp TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    title TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    related_id TEXT,
                    evidence TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS incidents (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    detection_timestamp TEXT NOT NULL,
                    first_observed TEXT NOT NULL,
                    last_observed TEXT NOT NULL,
                    related_processes TEXT NOT NULL DEFAULT '[]',
                    related_connections TEXT NOT NULL DEFAULT '[]',
                    related_alerts TEXT NOT NULL DEFAULT '[]',
                    evidence_summary TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    analyst_notes TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS alerts_timestamp_idx ON alerts(timestamp DESC);
                CREATE INDEX IF NOT EXISTS events_timestamp_idx ON events(timestamp DESC);
                CREATE INDEX IF NOT EXISTS events_type_time_idx
                    ON events(event_type, timestamp DESC);
                CREATE INDEX IF NOT EXISTS incidents_status_time_idx
                    ON incidents(status, last_observed DESC);
                CREATE TABLE IF NOT EXISTS telemetry_samples (
                    timestamp TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    cpu_percent REAL,
                    memory_percent REAL,
                    disk_percent REAL,
                    upload_mbps REAL,
                    download_mbps REAL,
                    active_connections INTEGER,
                    established_connections INTEGER,
                    process_count INTEGER,
                    collector_status TEXT NOT NULL,
                    unavailable_metrics TEXT NOT NULL DEFAULT '[]',
                    PRIMARY KEY (timestamp, sequence)
                );
                CREATE INDEX IF NOT EXISTS telemetry_samples_time_idx
                    ON telemetry_samples(timestamp DESC);
                CREATE TABLE IF NOT EXISTS telemetry_compacted_keys (
                    timestamp TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    bucket_start TEXT NOT NULL,
                    PRIMARY KEY (timestamp, sequence)
                );
                CREATE INDEX IF NOT EXISTS telemetry_compacted_keys_bucket_idx
                    ON telemetry_compacted_keys(bucket_start);
                CREATE TABLE IF NOT EXISTS process_observations (
                    timestamp TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    pid INTEGER NOT NULL,
                    create_time TEXT NOT NULL DEFAULT '',
                    name TEXT NOT NULL,
                    cpu_percent REAL,
                    memory_percent REAL,
                    connection_count INTEGER,
                    status TEXT NOT NULL,
                    access_status TEXT NOT NULL,
                    PRIMARY KEY (timestamp, sequence, pid, create_time)
                );
                CREATE INDEX IF NOT EXISTS process_observations_time_idx
                    ON process_observations(timestamp DESC);
                CREATE INDEX IF NOT EXISTS process_observations_identity_idx
                    ON process_observations(pid, create_time, timestamp DESC);
                CREATE TABLE IF NOT EXISTS connection_summaries (
                    timestamp TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    active_connections INTEGER,
                    established_connections INTEGER,
                    unique_remote_destinations INTEGER,
                    new_connections INTEGER,
                    disappeared_connections INTEGER,
                    collection_status TEXT NOT NULL,
                    lifecycle_confidence TEXT NOT NULL,
                    top_processes TEXT NOT NULL DEFAULT '[]',
                    PRIMARY KEY (timestamp, sequence)
                );
                CREATE INDEX IF NOT EXISTS connection_summaries_time_idx
                    ON connection_summaries(timestamp DESC);
                CREATE TABLE IF NOT EXISTS telemetry_rollups (
                    bucket_start TEXT PRIMARY KEY,
                    bucket_end TEXT NOT NULL,
                    resolution_seconds INTEGER NOT NULL,
                    sample_count INTEGER NOT NULL,
                    cpu_percent REAL,
                    memory_percent REAL,
                    disk_percent REAL,
                    upload_mbps REAL,
                    download_mbps REAL,
                    active_connections REAL,
                    established_connections REAL,
                    process_count REAL,
                    collector_status TEXT NOT NULL,
                    unavailable_metrics TEXT NOT NULL DEFAULT '[]',
                    cpu_percent_count INTEGER NOT NULL DEFAULT 0,
                    memory_percent_count INTEGER NOT NULL DEFAULT 0,
                    disk_percent_count INTEGER NOT NULL DEFAULT 0,
                    upload_mbps_count INTEGER NOT NULL DEFAULT 0,
                    download_mbps_count INTEGER NOT NULL DEFAULT 0,
                    active_connections_count INTEGER NOT NULL DEFAULT 0,
                    established_connections_count INTEGER NOT NULL DEFAULT 0,
                    process_count_count INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS telemetry_rollups_time_idx
                    ON telemetry_rollups(bucket_start DESC);
                CREATE TABLE IF NOT EXISTS baseline_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS windows_events (
                    id TEXT PRIMARY KEY,
                    channel TEXT NOT NULL,
                    record_id INTEGER NOT NULL,
                    timestamp TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    event_id INTEGER NOT NULL,
                    level TEXT NOT NULL,
                    category TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    service_name TEXT,
                    correlation_tags TEXT NOT NULL DEFAULT '[]',
                    UNIQUE(channel, record_id)
                );
                CREATE INDEX IF NOT EXISTS windows_events_time_idx
                    ON windows_events(timestamp DESC);
                CREATE INDEX IF NOT EXISTS windows_events_filter_idx
                    ON windows_events(channel, category, level, timestamp DESC);
                CREATE TABLE IF NOT EXISTS windows_event_cursors (
                    channel TEXT PRIMARY KEY,
                    record_id INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS monitored_folders (
                    id TEXT PRIMARY KEY,
                    path TEXT NOT NULL UNIQUE,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    baseline_created_at TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    status_detail TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS file_baselines (
                    folder_id TEXT NOT NULL REFERENCES monitored_folders(id) ON DELETE CASCADE,
                    relative_path TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    modified_ns INTEGER NOT NULL,
                    PRIMARY KEY(folder_id, relative_path)
                );
                CREATE TABLE IF NOT EXISTS file_integrity_events (
                    id TEXT PRIMARY KEY,
                    folder_id TEXT NOT NULL REFERENCES monitored_folders(id) ON DELETE CASCADE,
                    timestamp TEXT NOT NULL,
                    change_type TEXT NOT NULL,
                    relative_path TEXT NOT NULL,
                    previous_sha256 TEXT,
                    current_sha256 TEXT,
                    size INTEGER
                );
                CREATE INDEX IF NOT EXISTS file_integrity_events_time_idx
                    ON file_integrity_events(timestamp DESC);
                CREATE INDEX IF NOT EXISTS file_integrity_events_filter_idx
                    ON file_integrity_events(folder_id, change_type, timestamp DESC);
                """
            )
            try:
                self._ensure_alert_columns(connection)
                self._ensure_rollup_columns(connection)
                self._normalize_stored_timestamps(connection)
                self._backfill_rollup_counts(connection)
                self._reconcile_rollup_overlaps(connection)
                connection.execute(
                    "INSERT INTO schema_meta (key, value) VALUES ('schema_version', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (str(SCHEMA_VERSION),),
                )
                integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
                if integrity != "ok":
                    raise sqlite3.DatabaseError("database integrity verification failed")
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return SCHEMA_VERSION

    def _existing_schema_version(self) -> int:
        if not self.path.is_file() or self.path.stat().st_size == 0:
            return 0
        # Inspect a byte-for-byte snapshot so a compatibility failure cannot update,
        # checkpoint, create, or otherwise mutate the live database's WAL sidecars.
        # Copying the existing WAL and SHM also makes uncheckpointed metadata visible.
        try:
            # Keep the read-only compatibility snapshot on the same writable
            # volume as the database. Sandboxed Windows runtimes may expose a
            # nominal system temp path that cannot actually create files.
            with tempfile.TemporaryDirectory(
                prefix=".ghostwatch-schema-check-", dir=self.path.parent
            ) as temp:
                snapshot = Path(temp) / self.path.name
                shutil.copyfile(self.path, snapshot)
                for suffix in ("-wal", "-shm"):
                    source = self.path.with_name(self.path.name + suffix)
                    if source.is_file():
                        shutil.copyfile(
                            source, snapshot.with_name(snapshot.name + suffix)
                        )
                with closing(sqlite3.connect(snapshot)) as connection:
                    connection.row_factory = sqlite3.Row
                    tables = {
                        row["name"]
                        for row in connection.execute(
                            "SELECT name FROM sqlite_master WHERE type='table'"
                        ).fetchall()
                    }
                    if not tables:
                        return 0
                    if "schema_meta" not in tables:
                        return 1
                    row = connection.execute(
                        "SELECT value FROM schema_meta WHERE key = ?",
                        ("schema_version",),
                    ).fetchone()
                    return int(row["value"]) if row else 1
        except (sqlite3.Error, OSError, ValueError):
            raise

    def _preflight_timestamp_normalization(self) -> None:
        """Detect canonical-key collisions on a disposable WAL-aware snapshot."""
        if not self.path.is_file() or self.path.stat().st_size == 0:
            return
        with tempfile.TemporaryDirectory(
            prefix=".ghostwatch-timestamp-check-", dir=self.path.parent
        ) as temp:
            snapshot = Path(temp) / self.path.name
            shutil.copyfile(self.path, snapshot)
            for suffix in ("-wal", "-shm"):
                source = self.path.with_name(self.path.name + suffix)
                if source.is_file():
                    shutil.copyfile(source, snapshot.with_name(snapshot.name + suffix))
            with closing(sqlite3.connect(snapshot)) as connection:
                connection.row_factory = sqlite3.Row
                self._assert_no_timestamp_normalization_collisions(connection)

    @staticmethod
    def _assert_no_timestamp_normalization_collisions(
        connection: sqlite3.Connection,
    ) -> None:
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        collisions: list[str] = []
        for table, key_columns in NORMALIZED_UNIQUE_KEYS.items():
            if table not in tables:
                continue
            columns = {
                row["name"]
                for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
            }
            if not set(key_columns).issubset(columns):
                continue
            selected = ", ".join(key_columns)
            rows = connection.execute(  # noqa: S608
                f"SELECT rowid, {selected} FROM {table}"
            ).fetchall()
            normalized_groups: dict[tuple[object, ...], list[int]] = {}
            timestamp_columns = set(TIMESTAMP_COLUMNS.get(table, ()))
            for row in rows:
                normalized_key: list[object] = []
                for column in key_columns:
                    value = row[column]
                    if column in timestamp_columns and value not in {None, ""}:
                        value = utc_iso(
                            parse_utc(str(value), label=f"{table}.{column}"),
                            label=f"{table}.{column}",
                        )
                    normalized_key.append(value)
                normalized_groups.setdefault(tuple(normalized_key), []).append(
                    int(row["rowid"])
                )
            for rowids in normalized_groups.values():
                if len(rowids) > 1:
                    collisions.append(
                        f"{table} rowids {', '.join(str(rowid) for rowid in rowids)}"
                    )
        if collisions:
            locations = "; ".join(collisions)
            raise TimestampNormalizationCollisionError(
                "UTC timestamp normalization would collide for "
                f"{locations}. Merge or otherwise distinguish those duplicate "
                "logical observations, then retry. No migration changes were applied."
            )

    def _pre_migration_backup(self, previous_version: int) -> Path:
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        destination = (
            self.path.parent
            / "backups"
            / f"{self.path.stem}-pre-v{SCHEMA_VERSION}-from-v{previous_version}-{timestamp}.db"
        )
        return self.backup(destination)

    @staticmethod
    def _ensure_alert_columns(connection: sqlite3.Connection) -> None:
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(alerts)").fetchall()
        }
        additions = {
            "rule_id": "TEXT",
            "evidence": "TEXT NOT NULL DEFAULT '{}'",
            "incident_id": "TEXT",
            "first_occurrence": "TEXT",
            "last_occurrence": "TEXT",
            "occurrence_count": "INTEGER NOT NULL DEFAULT 1",
            "condition_active": "INTEGER",
            "last_recovery": "TEXT",
        }
        for name, definition in additions.items():
            if name not in columns:
                connection.execute(f"ALTER TABLE alerts ADD COLUMN {name} {definition}")

    @staticmethod
    def _ensure_rollup_columns(connection: sqlite3.Connection) -> None:
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(telemetry_rollups)").fetchall()
        }
        for metric in ROLLUP_METRICS:
            count_column = f"{metric}_count"
            if count_column not in columns:
                connection.execute(
                    f"ALTER TABLE telemetry_rollups ADD COLUMN {count_column} "
                    "INTEGER NOT NULL DEFAULT 0"
                )

    @staticmethod
    def _normalize_stored_timestamps(connection: sqlite3.Connection) -> None:
        """Canonicalize legacy aware timestamps transactionally; reject naive values."""
        Database._assert_no_timestamp_normalization_collisions(connection)
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        for table, candidates in TIMESTAMP_COLUMNS.items():
            if table not in tables:
                continue
            existing = {
                row["name"]
                for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
            }
            columns = [column for column in candidates if column in existing]
            if not columns:
                continue
            selected = ", ".join(columns)
            rows = connection.execute(
                f"SELECT rowid, {selected} FROM {table}"
            ).fetchall()
            for row in rows:
                changes: dict[str, str] = {}
                for column in columns:
                    value = row[column]
                    if value in {None, ""}:
                        continue
                    normalized = utc_iso(
                        parse_utc(str(value), label=f"{table}.{column}"),
                        label=f"{table}.{column}",
                    )
                    if normalized != value:
                        changes[column] = normalized
                if changes:
                    assignments = ", ".join(f"{column} = ?" for column in changes)
                    connection.execute(
                        f"UPDATE {table} SET {assignments} WHERE rowid = ?",
                        (*changes.values(), row["rowid"]),
                    )

    @staticmethod
    def _backfill_rollup_counts(connection: sqlite3.Connection) -> None:
        # RC1/RC2 stored only an aggregate and total count. For those rows, the
        # only lossless-compatible estimate is that every non-null aggregate was
        # present in each source sample. New RC3 rollups store exact metric counts.
        for metric in ROLLUP_METRICS:
            connection.execute(
                f"UPDATE telemetry_rollups SET {metric}_count = "
                f"CASE WHEN {metric} IS NULL THEN 0 ELSE sample_count END "
                f"WHERE {metric}_count = 0"
            )

    @classmethod
    def _reconcile_rollup_overlaps(cls, connection: sqlite3.Connection) -> None:
        """Merge legacy raw rows already covered by a rollup exactly once."""
        rollups = connection.execute(
            "SELECT bucket_start, bucket_end FROM telemetry_rollups "
            "ORDER BY bucket_start"
        ).fetchall()
        for rollup in rollups:
            members = connection.execute(
                "SELECT rowid AS source_rowid, * FROM telemetry_samples "
                "WHERE timestamp >= ? AND timestamp < ? ORDER BY timestamp",
                (rollup["bucket_start"], rollup["bucket_end"]),
            ).fetchall()
            if not members:
                continue
            bucket = parse_utc(
                rollup["bucket_start"], label="telemetry_rollups.bucket_start"
            )
            cls._merge_rollup_members(connection, bucket, members)
            connection.executemany(
                "INSERT OR IGNORE INTO telemetry_compacted_keys "
                "(timestamp, sequence, bucket_start) VALUES (?, ?, ?)",
                [
                    (row["timestamp"], row["sequence"], utc_iso(bucket))
                    for row in members
                ],
            )
            connection.executemany(
                "DELETE FROM telemetry_samples WHERE rowid = ?",
                [(int(row["source_rowid"]),) for row in members],
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=2.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @staticmethod
    def _decode_json_list(value: object, fallback: list | None = None) -> list:
        try:
            decoded = json.loads(value) if isinstance(value, str) else value
            if not isinstance(decoded, list):
                raise TypeError
            return decoded
        except (json.JSONDecodeError, TypeError):
            LOGGER.warning("Stored list metadata was invalid; a safe fallback was used")
            return list(fallback or [])

    @staticmethod
    def _collector_status(value: object) -> str:
        if value in {"healthy", "degraded", "unavailable"}:
            return str(value)
        LOGGER.warning("Stored history status was invalid; degraded status was restored")
        return "degraded"

    def schema_version(self) -> int:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM schema_meta WHERE key = ?", ("schema_version",)
            ).fetchone()
        return int(row["value"]) if row else 0

    def load_settings(self) -> Settings:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM settings WHERE id = ?", (1,)
            ).fetchone()
        if row is None:
            return Settings()
        try:
            return Settings.model_validate_json(row["payload"])
        except (ValueError, TypeError):
            LOGGER.warning("Stored settings were invalid; safe defaults were restored")
            return Settings()

    def save_settings(self, settings: Settings) -> None:
        payload = settings.model_dump_json()
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO settings (id, payload) VALUES (?, ?) "
                "ON CONFLICT(id) DO UPDATE SET payload = excluded.payload",
                (1, payload),
            )

    def load_detection_settings(self) -> DetectionSettings:
        return self._load_configuration("detection", DetectionSettings)

    def save_detection_settings(self, settings: DetectionSettings) -> None:
        self._save_configuration("detection", settings)

    def load_ai_settings(self) -> AISettings:
        return self._load_configuration("ai", AISettings)

    def save_ai_settings(self, settings: AISettings) -> None:
        self._save_configuration("ai", settings)

    def load_history_settings(self) -> HistorySettings:
        return self._load_configuration("history", HistorySettings)

    def save_history_settings(self, settings: HistorySettings) -> None:
        self._save_configuration("history", settings)

    def load_windows_event_settings(self) -> WindowsEventSettings:
        return self._load_configuration("windows_events", WindowsEventSettings)

    def save_windows_event_settings(self, settings: WindowsEventSettings) -> None:
        self._save_configuration("windows_events", settings)

    def load_file_integrity_settings(self) -> FileIntegritySettings:
        return self._load_configuration("file_integrity", FileIntegritySettings)

    def save_file_integrity_settings(self, settings: FileIntegritySettings) -> None:
        self._save_configuration("file_integrity", settings)

    def _load_configuration(self, key: str, model: type[BaseModel]):
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM configuration WHERE key = ?", (key,)
            ).fetchone()
        if row is None:
            return model()
        try:
            return model.model_validate_json(row["payload"])
        except (ValueError, TypeError):
            LOGGER.warning("Stored %s configuration was invalid; safe defaults were restored", key)
            return model()

    def _save_configuration(self, key: str, value: BaseModel) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO configuration (key, payload) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET payload = excluded.payload",
                (key, value.model_dump_json()),
            )

    def add_alert(self, alert: Alert, retention: int) -> Alert:
        with self._lock, self._connect() as connection:
            stored = alert
            if alert.rule_id == "collection_failure":
                row = connection.execute(
                    "SELECT * FROM alerts WHERE id = ?", (alert.id,)
                ).fetchone()
                if row is not None:
                    existing = self._row_to_alert(row)
                    reopened = (
                        alert.condition_active is True
                        and existing.condition_active is False
                    )
                    stored = alert.model_copy(
                        update={
                            "first_occurrence": (
                                existing.first_occurrence
                                or alert.first_occurrence
                                or existing.timestamp
                            ),
                            "last_occurrence": (
                                alert.last_occurrence or existing.last_occurrence
                            ),
                            "occurrence_count": (
                                existing.occurrence_count + alert.occurrence_count
                            ),
                            "last_recovery": (
                                alert.last_recovery or existing.last_recovery
                            ),
                            "acknowledged": (
                                False if reopened else existing.acknowledged
                            ),
                            "dismissed": False if reopened else existing.dismissed,
                        }
                    )
            connection.execute(
                """INSERT INTO alerts
                (id, timestamp, severity, source, metric, explanation, acknowledged,
                 dismissed, rule_id, evidence, incident_id, first_occurrence,
                 last_occurrence, occurrence_count, condition_active, last_recovery)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    timestamp=excluded.timestamp,
                    severity=excluded.severity,
                    source=excluded.source,
                    metric=excluded.metric,
                    explanation=excluded.explanation,
                    acknowledged=excluded.acknowledged,
                    dismissed=excluded.dismissed,
                    rule_id=excluded.rule_id,
                    evidence=excluded.evidence,
                    incident_id=excluded.incident_id,
                    first_occurrence=excluded.first_occurrence,
                    last_occurrence=excluded.last_occurrence,
                    occurrence_count=excluded.occurrence_count,
                    condition_active=excluded.condition_active,
                    last_recovery=excluded.last_recovery""",
                (
                    stored.id,
                    utc_iso(stored.timestamp, label="alert timestamp"),
                    stored.severity,
                    stored.source,
                    stored.metric,
                    stored.explanation,
                    int(stored.acknowledged),
                    int(stored.dismissed),
                    stored.rule_id,
                    json.dumps(stored.evidence, separators=(",", ":"), default=str),
                    stored.incident_id,
                    (
                        utc_iso(stored.first_occurrence, label="alert first occurrence")
                        if stored.first_occurrence
                        else None
                    ),
                    (
                        utc_iso(stored.last_occurrence, label="alert last occurrence")
                        if stored.last_occurrence
                        else None
                    ),
                    stored.occurrence_count,
                    (
                        int(stored.condition_active)
                        if stored.condition_active is not None
                        else None
                    ),
                    (
                        utc_iso(stored.last_recovery, label="alert last recovery")
                        if stored.last_recovery
                        else None
                    ),
                ),
            )
            self._enforce_retention(connection, retention)
        return stored

    def enforce_retention(self, retention: int) -> None:
        with self._lock, self._connect() as connection:
            self._enforce_retention(connection, retention)

    @staticmethod
    def _enforce_retention(connection: sqlite3.Connection, retention: int) -> None:
        connection.execute(
            "DELETE FROM alerts WHERE id NOT IN "
            "(SELECT id FROM alerts ORDER BY timestamp DESC LIMIT ?)",
            (retention,),
        )

    def list_alerts(self, limit: int = 100, include_dismissed: bool = False) -> list[Alert]:
        query = "SELECT * FROM alerts"
        params: list[object] = []
        if not include_dismissed:
            query += " WHERE dismissed = ?"
            params.append(0)
        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        with self._lock, self._connect() as connection:
            rows = connection.execute(query, tuple(params)).fetchall()
        return [self._row_to_alert(row) for row in rows]

    def update_alert(self, alert_id: str, update: AlertUpdate) -> Alert | None:
        changes = update.model_dump(exclude_none=True)
        if not changes:
            return self.get_alert(alert_id)
        assignments = ", ".join(f"{key} = ?" for key in changes)
        values = [int(value) for value in changes.values()]
        values.append(alert_id)
        with self._lock, self._connect() as connection:
            connection.execute(
                f"UPDATE alerts SET {assignments} WHERE id = ?",  # noqa: S608 - model keys only
                tuple(values),
            )
        return self.get_alert(alert_id)

    def get_alert(self, alert_id: str) -> Alert | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM alerts WHERE id = ?", (alert_id,)
            ).fetchone()
        return self._row_to_alert(row) if row else None

    @staticmethod
    def _row_to_alert(row: sqlite3.Row) -> Alert:
        keys = set(row.keys())
        try:
            evidence = json.loads(row["evidence"]) if "evidence" in keys else {}
        except (json.JSONDecodeError, TypeError):
            evidence = {}
        return Alert(
            id=row["id"],
            timestamp=parse_utc(row["timestamp"], label="alerts.timestamp"),
            severity=row["severity"],
            source=row["source"],
            metric=row["metric"],
            explanation=row["explanation"],
            acknowledged=bool(row["acknowledged"]),
            dismissed=bool(row["dismissed"]),
            rule_id=row["rule_id"] if "rule_id" in keys else None,
            evidence=evidence,
            incident_id=row["incident_id"] if "incident_id" in keys else None,
            first_occurrence=(
                parse_utc(row["first_occurrence"], label="alerts.first_occurrence")
                if "first_occurrence" in keys and row["first_occurrence"]
                else None
            ),
            last_occurrence=(
                parse_utc(row["last_occurrence"], label="alerts.last_occurrence")
                if "last_occurrence" in keys and row["last_occurrence"]
                else None
            ),
            occurrence_count=(
                int(row["occurrence_count"])
                if "occurrence_count" in keys
                else 1
            ),
            condition_active=(
                bool(row["condition_active"])
                if "condition_active" in keys and row["condition_active"] is not None
                else None
            ),
            last_recovery=(
                parse_utc(row["last_recovery"], label="alerts.last_recovery")
                if "last_recovery" in keys and row["last_recovery"]
                else None
            ),
        )

    def add_event(self, event: EventRecord, retention: int = 10_000) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """INSERT INTO events
                (id, timestamp, event_type, severity, title, summary, related_id, evidence)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO NOTHING""",
                (
                    event.id,
                    utc_iso(event.timestamp, label="event timestamp"),
                    event.event_type,
                    event.severity,
                    event.title,
                    event.summary,
                    event.related_id,
                    json.dumps(event.evidence, separators=(",", ":"), default=str),
                ),
            )
            connection.execute(
                "DELETE FROM events WHERE id NOT IN "
                "(SELECT id FROM events ORDER BY timestamp DESC LIMIT ?)",
                (retention,),
            )

    def list_events(
        self,
        limit: int = 200,
        event_type: str | None = None,
        severity: str | None = None,
        search: str | None = None,
    ) -> list[EventRecord]:
        clauses: list[str] = []
        params: list[object] = []
        if event_type:
            clauses.append("event_type = ?")
            params.append(event_type)
        if severity:
            clauses.append("severity = ?")
            params.append(severity)
        if search:
            clauses.append("(title LIKE ? OR summary LIKE ?)")
            safe_search = f"%{search[:100]}%"
            params.extend((safe_search, safe_search))
        query = "SELECT * FROM events"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(min(max(limit, 1), 1000))
        with self._lock, self._connect() as connection:
            rows = connection.execute(query, tuple(params)).fetchall()
        result: list[EventRecord] = []
        for row in rows:
            try:
                evidence = json.loads(row["evidence"])
            except (json.JSONDecodeError, TypeError):
                evidence = {}
            result.append(
                EventRecord(
                    id=row["id"],
                    timestamp=parse_utc(row["timestamp"], label="events.timestamp"),
                    event_type=row["event_type"],
                    severity=row["severity"],
                    title=row["title"],
                    summary=row["summary"],
                    related_id=row["related_id"],
                    evidence=evidence,
                )
            )
        return result

    def load_windows_event_cursors(self) -> dict[str, int]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT channel, record_id FROM windows_event_cursors"
            ).fetchall()
        return {row["channel"]: int(row["record_id"]) for row in rows}

    def add_windows_event_batch(
        self,
        events: list[WindowsEventRecord],
        cursors: dict[str, int],
        retention_hours: int,
    ) -> list[str]:
        cutoff = datetime.now(UTC) - timedelta(hours=retention_hours)
        inserted: list[str] = []
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                for event in events:
                    result = connection.execute(
                        """INSERT OR IGNORE INTO windows_events
                        (id, channel, record_id, timestamp, provider, event_id,
                         level, category, summary, service_name, correlation_tags)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            event.id,
                            event.channel,
                            event.record_id,
                            utc_iso(event.timestamp, label="Windows event timestamp"),
                            event.provider,
                            event.event_id,
                            event.level,
                            event.category,
                            event.summary,
                            event.service_name,
                            json.dumps(event.correlation_tags, separators=(",", ":")),
                        ),
                    )
                    if result.rowcount:
                        inserted.append(event.id)
                updated_at = utc_iso(datetime.now(UTC))
                connection.executemany(
                    "INSERT INTO windows_event_cursors "
                    "(channel, record_id, updated_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(channel) DO UPDATE SET "
                    "record_id=MAX(record_id, excluded.record_id), "
                    "updated_at=excluded.updated_at",
                    [
                        (channel, max(0, record_id), updated_at)
                        for channel, record_id in cursors.items()
                    ],
                )
                connection.execute(
                    "DELETE FROM windows_events WHERE timestamp < ?",
                    (utc_iso(cutoff),),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return inserted

    def list_windows_events(
        self,
        *,
        limit: int = 500,
        channel: str | None = None,
        category: str | None = None,
        level: str | None = None,
        search: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[WindowsEventRecord]:
        clauses: list[str] = []
        params: list[object] = []
        for field, value in (("channel", channel), ("category", category), ("level", level)):
            if value:
                clauses.append(f"{field} = ?")
                params.append(value)
        if search:
            clauses.append("(summary LIKE ? OR provider LIKE ? OR service_name LIKE ?)")
            pattern = f"%{search[:200]}%"
            params.extend([pattern, pattern, pattern])
        if start:
            clauses.append("timestamp >= ?")
            params.append(utc_iso(start, label="Windows event start"))
        if end:
            clauses.append("timestamp < ?")
            params.append(utc_iso(end, label="Windows event end"))
        query = "SELECT * FROM windows_events"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(min(max(limit, 1), 5000))
        with self._lock, self._connect() as connection:
            rows = connection.execute(query, tuple(params)).fetchall()
        return [self._row_to_windows_event(row) for row in rows]

    def windows_event_summary(self) -> dict[str, int]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT category, COUNT(*) AS count FROM windows_events GROUP BY category"
            ).fetchall()
        return {row["category"]: int(row["count"]) for row in rows}

    @staticmethod
    def _row_to_windows_event(row: sqlite3.Row) -> WindowsEventRecord:
        return WindowsEventRecord(
            id=row["id"],
            channel=row["channel"],
            record_id=row["record_id"],
            timestamp=parse_utc(row["timestamp"], label="windows_events.timestamp"),
            provider=row["provider"],
            event_id=row["event_id"],
            level=row["level"],
            category=row["category"],
            summary=row["summary"],
            service_name=row["service_name"],
            correlation_tags=Database._decode_json_list(row["correlation_tags"]),
        )

    def add_monitored_folder(
        self, folder: MonitoredFolder, entries: list[FileBaselineEntry]
    ) -> None:
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """INSERT INTO monitored_folders
                    (id, path, enabled, created_at, baseline_created_at, status,
                     status_detail) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        folder.id,
                        folder.path,
                        int(folder.enabled),
                        utc_iso(folder.created_at, label="folder creation timestamp"),
                        utc_iso(
                            folder.baseline_created_at,
                            label="folder baseline timestamp",
                        )
                        if folder.baseline_created_at
                        else None,
                        folder.status,
                        folder.status_detail,
                    ),
                )
                self._insert_file_baselines(connection, folder.id, entries)
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def list_monitored_folders(self) -> list[MonitoredFolder]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM monitored_folders ORDER BY created_at ASC"
            ).fetchall()
        return [self._row_to_monitored_folder(row) for row in rows]

    def get_monitored_folder(self, folder_id: str) -> MonitoredFolder | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM monitored_folders WHERE id = ?", (folder_id,)
            ).fetchone()
        return self._row_to_monitored_folder(row) if row else None

    def set_monitored_folder_enabled(
        self, folder_id: str, enabled: bool
    ) -> MonitoredFolder | None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE monitored_folders SET enabled = ? WHERE id = ?",
                (int(enabled), folder_id),
            )
        return self.get_monitored_folder(folder_id)

    def delete_monitored_folder(self, folder_id: str) -> bool:
        with self._lock, self._connect() as connection:
            result = connection.execute(
                "DELETE FROM monitored_folders WHERE id = ?", (folder_id,)
            )
        return bool(result.rowcount)

    def load_file_baseline(self, folder_id: str) -> dict[str, FileBaselineEntry]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM file_baselines WHERE folder_id = ?",
                (folder_id,),
            ).fetchall()
        return {
            row["relative_path"]: FileBaselineEntry(
                relative_path=row["relative_path"],
                sha256=row["sha256"],
                size=row["size"],
                modified_ns=row["modified_ns"],
            )
            for row in rows
        }

    def save_file_scan(
        self,
        folder_id: str,
        result: FileScanResult,
        event_retention: int,
    ) -> list[str]:
        inserted: list[str] = []
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                if result.status != "unavailable":
                    connection.execute(
                        "DELETE FROM file_baselines WHERE folder_id = ?", (folder_id,)
                    )
                    self._insert_file_baselines(connection, folder_id, result.entries)
                for event in result.events:
                    row = connection.execute(
                        """INSERT OR IGNORE INTO file_integrity_events
                        (id, folder_id, timestamp, change_type, relative_path,
                         previous_sha256, current_sha256, size)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            event.id,
                            event.folder_id,
                            utc_iso(event.timestamp, label="file integrity timestamp"),
                            event.change_type,
                            event.relative_path,
                            event.previous_sha256,
                            event.current_sha256,
                            event.size,
                        ),
                    )
                    if row.rowcount:
                        inserted.append(event.id)
                connection.execute(
                    "UPDATE monitored_folders SET status = ?, status_detail = ? "
                    "WHERE id = ?",
                    (result.status, result.status_detail, folder_id),
                )
                connection.execute(
                    "DELETE FROM file_integrity_events WHERE id NOT IN "
                    "(SELECT id FROM file_integrity_events "
                    "ORDER BY timestamp DESC LIMIT ?)",
                    (event_retention,),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return inserted

    def list_file_integrity_events(
        self,
        *,
        limit: int = 500,
        folder_id: str | None = None,
        change_type: str | None = None,
        search: str | None = None,
    ) -> list[FileIntegrityEvent]:
        clauses: list[str] = []
        params: list[object] = []
        if folder_id:
            clauses.append("folder_id = ?")
            params.append(folder_id)
        if change_type:
            clauses.append("change_type = ?")
            params.append(change_type)
        if search:
            clauses.append("relative_path LIKE ?")
            params.append(f"%{search[:200]}%")
        query = "SELECT * FROM file_integrity_events"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(min(max(limit, 1), 5000))
        with self._lock, self._connect() as connection:
            rows = connection.execute(query, tuple(params)).fetchall()
        return [
            FileIntegrityEvent(
                id=row["id"],
                folder_id=row["folder_id"],
                timestamp=parse_utc(
                    row["timestamp"], label="file_integrity_events.timestamp"
                ),
                change_type=row["change_type"],
                relative_path=row["relative_path"],
                previous_sha256=row["previous_sha256"],
                current_sha256=row["current_sha256"],
                size=row["size"],
            )
            for row in rows
        ]

    @staticmethod
    def _insert_file_baselines(
        connection: sqlite3.Connection,
        folder_id: str,
        entries: list[FileBaselineEntry],
    ) -> None:
        connection.executemany(
            "INSERT INTO file_baselines "
            "(folder_id, relative_path, sha256, size, modified_ns) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                (
                    folder_id,
                    entry.relative_path,
                    entry.sha256,
                    entry.size,
                    entry.modified_ns,
                )
                for entry in entries
            ],
        )

    @staticmethod
    def _row_to_monitored_folder(row: sqlite3.Row) -> MonitoredFolder:
        return MonitoredFolder(
            id=row["id"],
            path=row["path"],
            enabled=bool(row["enabled"]),
            created_at=parse_utc(row["created_at"], label="monitored_folders.created_at"),
            baseline_created_at=(
                parse_utc(
                    row["baseline_created_at"],
                    label="monitored_folders.baseline_created_at",
                )
                if row["baseline_created_at"]
                else None
            ),
            status=row["status"],
            status_detail=row["status_detail"],
        )

    def add_incident(self, incident: Incident) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """INSERT INTO incidents
                (id, title, severity, detection_timestamp, first_observed, last_observed,
                 related_processes, related_connections, related_alerts, evidence_summary,
                 status, analyst_notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    incident.id,
                    incident.title,
                    incident.severity,
                    utc_iso(incident.detection_timestamp, label="incident detection timestamp"),
                    utc_iso(incident.first_observed, label="incident first observation"),
                    utc_iso(incident.last_observed, label="incident last observation"),
                    json.dumps(incident.related_processes),
                    json.dumps(incident.related_connections),
                    json.dumps(incident.related_alerts),
                    incident.evidence_summary,
                    incident.status,
                    incident.analyst_notes,
                ),
            )
            if incident.related_alerts:
                placeholders = ",".join("?" for _ in incident.related_alerts)
                connection.execute(
                    f"UPDATE alerts SET incident_id = ? WHERE id IN ({placeholders})",
                    (incident.id, *incident.related_alerts),
                )

    def list_incidents(self, limit: int = 200) -> list[Incident]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM incidents ORDER BY last_observed DESC LIMIT ?",
                (min(max(limit, 1), 1000),),
            ).fetchall()
        return [self._row_to_incident(row) for row in rows]

    def get_incident(self, incident_id: str) -> Incident | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM incidents WHERE id = ?", (incident_id,)
            ).fetchone()
        return self._row_to_incident(row) if row else None

    def update_incident(
        self, incident_id: str, update: IncidentUpdate, timestamp: datetime
    ) -> Incident | None:
        changes = update.model_dump(exclude_none=True)
        if not changes:
            return self.get_incident(incident_id)
        changes["last_observed"] = utc_iso(timestamp, label="incident update timestamp")
        assignments = ", ".join(f"{key} = ?" for key in changes)
        values = list(changes.values()) + [incident_id]
        with self._lock, self._connect() as connection:
            connection.execute(
                f"UPDATE incidents SET {assignments} WHERE id = ?",  # noqa: S608
                tuple(values),
            )
        return self.get_incident(incident_id)

    def add_telemetry_sample(
        self,
        sample: HistoricalSample,
        retention_hours: int = 168,
        max_samples: int = 100_000,
    ) -> None:
        self.add_history_batch(sample, [], None, retention_hours, max_samples)

    def add_history_batch(
        self,
        sample: HistoricalSample,
        processes: list[HistoricalProcessObservation],
        connection_summary: HistoricalConnectionSummary | None,
        retention_hours: int = 168,
        max_samples: int = 100_000,
    ) -> None:
        cutoff = datetime.now(UTC) - timedelta(hours=retention_hours)
        sample_timestamp = utc_iso(sample.timestamp, label="telemetry timestamp")
        with self._lock, self._connect() as connection:
            already_compacted = connection.execute(
                "SELECT 1 FROM telemetry_compacted_keys "
                "WHERE timestamp = ? AND sequence = ?",
                (sample_timestamp, sample.sequence),
            ).fetchone()
            sample_insert = None
            if already_compacted is None:
                sample_insert = connection.execute(
                    """INSERT INTO telemetry_samples
                (timestamp, sequence, cpu_percent, memory_percent, disk_percent,
                 upload_mbps, download_mbps, active_connections,
                 established_connections, process_count, collector_status,
                 unavailable_metrics)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(timestamp, sequence) DO NOTHING""",
                (
                    sample_timestamp,
                    sample.sequence,
                    sample.cpu_percent,
                    sample.memory_percent,
                    sample.disk_percent,
                    sample.upload_mbps,
                    sample.download_mbps,
                    sample.active_connections,
                    sample.established_connections,
                    sample.process_count,
                    sample.collector_status,
                    json.dumps(sample.unavailable_metrics, separators=(",", ":")),
                    ),
                )
            if sample_insert is not None and sample_insert.rowcount:
                covering_rollup = connection.execute(
                    "SELECT bucket_start FROM telemetry_rollups "
                    "WHERE bucket_start <= ? AND bucket_end > ? LIMIT 1",
                    (sample_timestamp, sample_timestamp),
                ).fetchone()
                if covering_rollup:
                    member = connection.execute(
                        "SELECT rowid AS source_rowid, * FROM telemetry_samples "
                        "WHERE timestamp = ? AND sequence = ?",
                        (sample_timestamp, sample.sequence),
                    ).fetchone()
                    if member:
                        bucket = parse_utc(
                            covering_rollup["bucket_start"],
                            label="telemetry_rollups.bucket_start",
                        )
                        self._merge_rollup_members(connection, bucket, [member])
                        connection.execute(
                            "INSERT OR IGNORE INTO telemetry_compacted_keys "
                            "(timestamp, sequence, bucket_start) VALUES (?, ?, ?)",
                            (sample_timestamp, sample.sequence, utc_iso(bucket)),
                        )
                        connection.execute(
                            "DELETE FROM telemetry_samples WHERE rowid = ?",
                            (member["source_rowid"],),
                        )
            connection.executemany(
                """INSERT INTO process_observations
                (timestamp, sequence, pid, create_time, name, cpu_percent,
                 memory_percent, connection_count, status, access_status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(timestamp, sequence, pid, create_time) DO NOTHING""",
                [
                    (
                        utc_iso(item.timestamp, label="process observation timestamp"),
                        item.sequence,
                        item.pid,
                        utc_iso(item.create_time, label="process creation timestamp")
                        if item.create_time
                        else "",
                        item.name,
                        item.cpu_percent,
                        item.memory_percent,
                        item.connection_count,
                        item.status,
                        item.access_status,
                    )
                    for item in processes
                ],
            )
            if connection_summary is not None:
                connection.execute(
                    """INSERT INTO connection_summaries
                    (timestamp, sequence, active_connections,
                     established_connections, unique_remote_destinations,
                     new_connections, disappeared_connections, collection_status,
                     lifecycle_confidence, top_processes)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(timestamp, sequence) DO NOTHING""",
                    (
                        utc_iso(
                            connection_summary.timestamp,
                            label="connection summary timestamp",
                        ),
                        connection_summary.sequence,
                        connection_summary.active_connections,
                        connection_summary.established_connections,
                        connection_summary.unique_remote_destinations,
                        connection_summary.new_connections,
                        connection_summary.disappeared_connections,
                        connection_summary.collection_status,
                        connection_summary.lifecycle_confidence,
                        json.dumps(
                            [item.model_dump(mode="json") for item in connection_summary.top_processes],
                            separators=(",", ":"),
                        ),
                    ),
                )
            self._prune_history_locked(connection, cutoff, max_samples)

    def list_telemetry_samples(
        self,
        start: datetime,
        end: datetime,
        limit: int = 10_000,
    ) -> list[HistoricalSample]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM telemetry_samples "
                "WHERE timestamp >= ? AND timestamp < ? "
                "ORDER BY timestamp ASC LIMIT ?",
                (
                    utc_iso(start, label="history start"),
                    utc_iso(end, label="history end"),
                    min(max(limit, 1), 100_000),
                ),
            ).fetchall()
        return [self._row_to_sample(row) for row in rows]

    def list_historical_samples(
        self,
        start: datetime,
        end: datetime,
        limit: int = 10_000,
    ) -> list[HistoricalSample]:
        # Preserve the pre-v3 diagnostic point-lookup contract for callers that
        # pass one instant. Public range APIs reject equal bounds and always use
        # the half-open page query below.
        if start == end:
            end = as_utc(end, label="history end") + timedelta(microseconds=1)
        return self.list_historical_samples_page(start, end, limit)[0]

    @staticmethod
    def history_order_key(sample: HistoricalSample) -> tuple[datetime, int, int]:
        """Chronological display order; a raw point follows a rollup at an exact tie."""
        return (
            sample.timestamp,
            1 if sample.resolution_seconds == 0 else 0,
            sample.sequence,
        )

    def list_historical_samples_page(
        self,
        start: datetime,
        end: datetime,
        limit: int = 10_000,
    ) -> tuple[list[HistoricalSample], bool]:
        """Return the newest ``limit`` mixed-resolution points in chronological order."""
        bounded_limit = min(max(limit, 1), 100_000)
        probe_limit = bounded_limit + 1
        start_value = utc_iso(start, label="history start")
        end_value = utc_iso(end, label="history end")
        with self._lock, self._connect() as connection:
            rollups = connection.execute(
                "SELECT * FROM telemetry_rollups "
                "WHERE bucket_start < ? AND bucket_end > ? "
                "ORDER BY bucket_start DESC LIMIT ?",
                (end_value, start_value, probe_limit),
            ).fetchall()
            raw = connection.execute(
                "SELECT samples.* FROM telemetry_samples AS samples "
                "WHERE samples.timestamp >= ? AND samples.timestamp < ? "
                "AND NOT EXISTS ("
                "SELECT 1 FROM telemetry_rollups AS rollups "
                "WHERE samples.timestamp >= rollups.bucket_start "
                "AND samples.timestamp < rollups.bucket_end"
                ") ORDER BY samples.timestamp DESC, samples.sequence DESC LIMIT ?",
                (start_value, end_value, probe_limit),
            ).fetchall()
        samples = [self._row_to_rollup(row) for row in rollups]
        samples.extend(self._row_to_sample(row) for row in raw)
        newest = sorted(samples, key=self.history_order_key, reverse=True)[:bounded_limit]
        return sorted(newest, key=self.history_order_key), len(samples) > bounded_limit

    def list_process_observations(
        self, start: datetime, end: datetime, limit: int = 10_000
    ) -> list[HistoricalProcessObservation]:
        return self.list_process_observations_page(start, end, limit)[0]

    def list_process_observations_page(
        self, start: datetime, end: datetime, limit: int = 10_000
    ) -> tuple[list[HistoricalProcessObservation], bool]:
        bounded_limit = min(max(limit, 1), 100_000)
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM process_observations "
                "WHERE timestamp >= ? AND timestamp < ? "
                "ORDER BY timestamp DESC, cpu_percent DESC, pid ASC LIMIT ?",
                (
                    utc_iso(start, label="process history start"),
                    utc_iso(end, label="process history end"),
                    bounded_limit + 1,
                ),
            ).fetchall()
        truncated = len(rows) > bounded_limit
        observations = [
            HistoricalProcessObservation(
                timestamp=parse_utc(
                    row["timestamp"], label="process_observations.timestamp"
                ),
                sequence=row["sequence"],
                pid=row["pid"],
                create_time=(
                    parse_utc(
                        row["create_time"], label="process_observations.create_time"
                    )
                    if row["create_time"]
                    else None
                ),
                name=row["name"],
                cpu_percent=row["cpu_percent"],
                memory_percent=row["memory_percent"],
                connection_count=row["connection_count"],
                status=row["status"],
                access_status=row["access_status"],
            )
            for row in rows[:bounded_limit]
        ]
        observations.sort(
            key=lambda item: (
                item.timestamp,
                -(item.cpu_percent if item.cpu_percent is not None else -1.0),
                item.pid,
            )
        )
        return observations, truncated

    def list_connection_summaries(
        self, start: datetime, end: datetime, limit: int = 10_000
    ) -> list[HistoricalConnectionSummary]:
        return self.list_connection_summaries_page(start, end, limit)[0]

    def list_connection_summaries_page(
        self, start: datetime, end: datetime, limit: int = 10_000
    ) -> tuple[list[HistoricalConnectionSummary], bool]:
        bounded_limit = min(max(limit, 1), 100_000)
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM connection_summaries "
                "WHERE timestamp >= ? AND timestamp < ? "
                "ORDER BY timestamp DESC, sequence DESC LIMIT ?",
                (
                    utc_iso(start, label="connection history start"),
                    utc_iso(end, label="connection history end"),
                    bounded_limit + 1,
                ),
            ).fetchall()
        truncated = len(rows) > bounded_limit
        summaries = [
            HistoricalConnectionSummary(
                timestamp=parse_utc(
                    row["timestamp"], label="connection_summaries.timestamp"
                ),
                sequence=row["sequence"],
                active_connections=row["active_connections"],
                established_connections=row["established_connections"],
                unique_remote_destinations=row["unique_remote_destinations"],
                new_connections=row["new_connections"],
                disappeared_connections=row["disappeared_connections"],
                collection_status=row["collection_status"],
                lifecycle_confidence=row["lifecycle_confidence"],
                top_processes=self._decode_json_list(row["top_processes"]),
            )
            for row in rows[:bounded_limit]
        ]
        summaries.sort(key=lambda item: (item.timestamp, item.sequence))
        return summaries, truncated

    @classmethod
    def _merge_rollup_members(
        cls,
        connection: sqlite3.Connection,
        bucket: datetime,
        members: list[sqlite3.Row],
    ) -> None:
        """Merge raw members into one rollup using exact per-metric weights."""
        bucket_start = utc_iso(bucket)
        bucket_end = utc_iso(bucket + timedelta(hours=1))
        existing = connection.execute(
            "SELECT * FROM telemetry_rollups WHERE bucket_start = ?",
            (bucket_start,),
        ).fetchone()

        counts: dict[str, int] = {}
        averages: dict[str, float | None] = {}
        for metric in ROLLUP_METRICS:
            new_values = [row[metric] for row in members if row[metric] is not None]
            old_count = int(existing[f"{metric}_count"] or 0) if existing else 0
            old_value = existing[metric] if existing else None
            metric_count = old_count + len(new_values)
            weighted_total = sum(new_values)
            if old_count and old_value is not None:
                weighted_total += float(old_value) * old_count
            counts[metric] = metric_count
            averages[metric] = weighted_total / metric_count if metric_count else None

        unavailable = {
            metric
            for row in members
            for metric in cls._decode_json_list(
                row["unavailable_metrics"], ["history_metadata_corrupt"]
            )
        }
        if existing:
            unavailable.update(
                cls._decode_json_list(
                    existing["unavailable_metrics"], ["history_metadata_corrupt"]
                )
            )
        status_rank = {"healthy": 0, "degraded": 1, "unavailable": 2}
        statuses = [row["collector_status"] for row in members]
        if existing:
            statuses.append(existing["collector_status"])
        collector_status = max(
            statuses, key=lambda status: status_rank.get(status, 2)
        )
        sample_count = len(members) + (int(existing["sample_count"]) if existing else 0)
        connection.execute(
            """INSERT INTO telemetry_rollups
            (bucket_start, bucket_end, resolution_seconds, sample_count,
             cpu_percent, memory_percent, disk_percent, upload_mbps,
             download_mbps, active_connections, established_connections,
             process_count, collector_status, unavailable_metrics,
             cpu_percent_count, memory_percent_count, disk_percent_count,
             upload_mbps_count, download_mbps_count,
             active_connections_count, established_connections_count,
             process_count_count)
            VALUES (?, ?, 3600, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(bucket_start) DO UPDATE SET
              bucket_end=excluded.bucket_end,
              resolution_seconds=excluded.resolution_seconds,
              sample_count=excluded.sample_count,
              cpu_percent=excluded.cpu_percent,
              memory_percent=excluded.memory_percent,
              disk_percent=excluded.disk_percent,
              upload_mbps=excluded.upload_mbps,
              download_mbps=excluded.download_mbps,
              active_connections=excluded.active_connections,
              established_connections=excluded.established_connections,
              process_count=excluded.process_count,
              collector_status=excluded.collector_status,
              unavailable_metrics=excluded.unavailable_metrics,
              cpu_percent_count=excluded.cpu_percent_count,
              memory_percent_count=excluded.memory_percent_count,
              disk_percent_count=excluded.disk_percent_count,
              upload_mbps_count=excluded.upload_mbps_count,
              download_mbps_count=excluded.download_mbps_count,
              active_connections_count=excluded.active_connections_count,
              established_connections_count=excluded.established_connections_count,
              process_count_count=excluded.process_count_count""",
            (
                bucket_start,
                bucket_end,
                sample_count,
                *(averages[metric] for metric in ROLLUP_METRICS),
                collector_status,
                json.dumps(sorted(unavailable), separators=(",", ":")),
                *(counts[metric] for metric in ROLLUP_METRICS),
            ),
        )

    def compact_history(
        self,
        compact_after_hours: int,
        retention_hours: int,
        now: datetime | None = None,
    ) -> int:
        selected_now = as_utc(now or datetime.now(UTC), label="compaction time")
        compact_before = selected_now - timedelta(hours=compact_after_hours)
        completed_before = compact_before.replace(minute=0, second=0, microsecond=0)
        retain_after = selected_now - timedelta(hours=retention_hours)
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                rows = connection.execute(
                    "SELECT rowid AS source_rowid, * FROM telemetry_samples "
                    "WHERE timestamp >= ? AND timestamp < ? ORDER BY timestamp ASC",
                    (utc_iso(retain_after), utc_iso(completed_before)),
                ).fetchall()
                buckets: dict[datetime, list[sqlite3.Row]] = {}
                for row in rows:
                    timestamp = parse_utc(
                        row["timestamp"], label="telemetry_samples.timestamp"
                    )
                    bucket = timestamp.replace(minute=0, second=0, microsecond=0)
                    buckets.setdefault(bucket, []).append(row)

                processed_rowids: list[int] = []
                for bucket, members in buckets.items():
                    existing = connection.execute(
                        "SELECT * FROM telemetry_rollups WHERE bucket_start = ?",
                        (utc_iso(bucket),),
                    ).fetchone()
                    if bucket < retain_after and existing is None:
                        # Do not create a partial aggregate at the retention boundary.
                        continue
                    self._merge_rollup_members(connection, bucket, members)
                    connection.executemany(
                        "INSERT OR IGNORE INTO telemetry_compacted_keys "
                        "(timestamp, sequence, bucket_start) VALUES (?, ?, ?)",
                        [
                            (row["timestamp"], row["sequence"], utc_iso(bucket))
                            for row in members
                        ],
                    )
                    processed_rowids.extend(int(row["source_rowid"]) for row in members)

                connection.executemany(
                    "DELETE FROM telemetry_samples WHERE rowid = ?",
                    [(rowid,) for rowid in processed_rowids],
                )
                connection.execute(
                    "DELETE FROM telemetry_samples WHERE timestamp < ?",
                    (utc_iso(retain_after),),
                )
                connection.execute(
                    "DELETE FROM telemetry_rollups WHERE bucket_end <= ?",
                    (utc_iso(retain_after),),
                )
                connection.execute(
                    "DELETE FROM telemetry_compacted_keys WHERE NOT EXISTS ("
                    "SELECT 1 FROM telemetry_rollups AS rollups "
                    "WHERE rollups.bucket_start = telemetry_compacted_keys.bucket_start)"
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return len(processed_rowids)

    def save_baseline_state(self, payload: dict[str, list[float]]) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO baseline_state (id, payload, updated_at) VALUES (1, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET payload=excluded.payload, "
                "updated_at=excluded.updated_at",
                (
                    json.dumps(payload, separators=(",", ":")),
                    utc_iso(datetime.now(UTC)),
                ),
            )

    def load_baseline_state(self) -> dict[str, list[float]]:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM baseline_state WHERE id = 1"
            ).fetchone()
        if row is None:
            return {}
        try:
            payload = json.loads(row["payload"])
            if not isinstance(payload, dict):
                raise TypeError
            return payload
        except (json.JSONDecodeError, TypeError):
            LOGGER.warning("Stored baseline state was invalid; a fresh baseline was started")
            return {}

    def telemetry_sample_count(self) -> int:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM telemetry_samples"
            ).fetchone()
        return int(row["count"])

    def prune_telemetry(self, retention_hours: int, max_samples: int) -> None:
        cutoff = datetime.now(UTC) - timedelta(hours=retention_hours)
        with self._lock, self._connect() as connection:
            self._prune_history_locked(connection, cutoff, max_samples)

    @staticmethod
    def _prune_history_locked(
        connection: sqlite3.Connection, cutoff: datetime, max_samples: int
    ) -> None:
        cutoff_value = utc_iso(cutoff)
        for table, column, comparison in (
            ("telemetry_samples", "timestamp", "<"),
            ("process_observations", "timestamp", "<"),
            ("connection_summaries", "timestamp", "<"),
            ("telemetry_rollups", "bucket_end", "<="),
        ):
            connection.execute(
                f"DELETE FROM {table} WHERE {column} {comparison} ?",  # noqa: S608
                (cutoff_value,),
            )
        connection.execute(
            "DELETE FROM telemetry_compacted_keys WHERE NOT EXISTS ("
            "SELECT 1 FROM telemetry_rollups AS rollups "
            "WHERE rollups.bucket_start = telemetry_compacted_keys.bucket_start)"
        )
        connection.execute(
            "DELETE FROM telemetry_samples WHERE rowid NOT IN "
            "(SELECT rowid FROM telemetry_samples ORDER BY timestamp DESC LIMIT ?)",
            (max_samples,),
        )
        connection.execute(
            "DELETE FROM connection_summaries WHERE rowid NOT IN "
            "(SELECT rowid FROM connection_summaries "
            "ORDER BY timestamp DESC LIMIT ?)",
            (max_samples,),
        )
        process_row_limit = min(max_samples * 100, 2_000_000)
        connection.execute(
            "DELETE FROM process_observations WHERE rowid NOT IN "
            "(SELECT rowid FROM process_observations "
            "ORDER BY timestamp DESC LIMIT ?)",
            (process_row_limit,),
        )
    @staticmethod
    def _row_to_sample(row: sqlite3.Row) -> HistoricalSample:
        return HistoricalSample(
            timestamp=parse_utc(row["timestamp"], label="telemetry_samples.timestamp"),
            sequence=row["sequence"],
            cpu_percent=row["cpu_percent"],
            memory_percent=row["memory_percent"],
            disk_percent=row["disk_percent"],
            upload_mbps=row["upload_mbps"],
            download_mbps=row["download_mbps"],
            active_connections=row["active_connections"],
            established_connections=row["established_connections"],
            process_count=row["process_count"],
            collector_status=Database._collector_status(row["collector_status"]),
            unavailable_metrics=Database._decode_json_list(
                row["unavailable_metrics"], ["history_metadata_corrupt"]
            ),
            coverage_start=parse_utc(
                row["timestamp"], label="telemetry_samples.timestamp"
            ),
            coverage_end=parse_utc(
                row["timestamp"], label="telemetry_samples.timestamp"
            ),
            metric_sample_counts={
                metric: 1 for metric in ROLLUP_METRICS if row[metric] is not None
            },
        )

    @staticmethod
    def _row_to_rollup(row: sqlite3.Row) -> HistoricalSample:
        def rounded(name: str) -> int | None:
            return round(row[name]) if row[name] is not None else None

        return HistoricalSample(
            timestamp=parse_utc(row["bucket_start"], label="telemetry_rollups.bucket_start"),
            sequence=-1,
            cpu_percent=row["cpu_percent"],
            memory_percent=row["memory_percent"],
            disk_percent=row["disk_percent"],
            upload_mbps=row["upload_mbps"],
            download_mbps=row["download_mbps"],
            active_connections=rounded("active_connections"),
            established_connections=rounded("established_connections"),
            process_count=rounded("process_count"),
            collector_status=Database._collector_status(row["collector_status"]),
            unavailable_metrics=Database._decode_json_list(
                row["unavailable_metrics"], ["history_metadata_corrupt"]
            ),
            resolution_seconds=row["resolution_seconds"],
            sample_count=row["sample_count"],
            coverage_start=parse_utc(
                row["bucket_start"], label="telemetry_rollups.bucket_start"
            ),
            coverage_end=parse_utc(
                row["bucket_end"], label="telemetry_rollups.bucket_end"
            ),
            metric_sample_counts={
                metric: int(row[f"{metric}_count"]) for metric in ROLLUP_METRICS
            },
        )

    @staticmethod
    def _row_to_incident(row: sqlite3.Row) -> Incident:
        return Incident(
            id=row["id"],
            title=row["title"],
            severity=row["severity"],
            detection_timestamp=parse_utc(
                row["detection_timestamp"], label="incidents.detection_timestamp"
            ),
            first_observed=parse_utc(
                row["first_observed"], label="incidents.first_observed"
            ),
            last_observed=parse_utc(
                row["last_observed"], label="incidents.last_observed"
            ),
            related_processes=json.loads(row["related_processes"]),
            related_connections=json.loads(row["related_connections"]),
            related_alerts=json.loads(row["related_alerts"]),
            evidence_summary=row["evidence_summary"],
            status=row["status"],
            analyst_notes=row["analyst_notes"],
        )

    def backup(self, destination: Path) -> Path:
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            with sqlite3.connect(self.path) as source, sqlite3.connect(destination) as target:
                source.backup(target)
        return destination
