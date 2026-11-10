from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.text_utils import bounded_identifier, bounded_text, escape_surrogates, safe_evidence

MAX_ALERT_TEXT = 2000
MAX_ALERT_LABEL = 200


def _alert_text(value: Any) -> Any:
    return bounded_text(value, MAX_ALERT_TEXT)


def _alert_label(value: Any) -> Any:
    return bounded_text(value, MAX_ALERT_LABEL)


def _alert_identifier(value: Any) -> Any:
    return bounded_identifier(value, MAX_ALERT_LABEL)


class Settings(BaseModel):
    """Backward-compatible core settings persisted by GhostWatch 1.x."""

    model_config = ConfigDict(extra="forbid")

    refresh_interval: float = Field(default=2.0, ge=1.0, le=30.0)
    cpu_threshold: float = Field(default=90.0, ge=1.0, le=100.0)
    memory_threshold: float = Field(default=90.0, ge=1.0, le=100.0)
    bandwidth_threshold_mbps: float = Field(default=3.0, ge=0.1, le=10_000.0)
    connection_threshold: int = Field(default=100, ge=1, le=100_000)
    threshold_duration: float = Field(default=10.0, ge=2.0, le=300.0)
    alert_retention: int = Field(default=1000, ge=50, le=50_000)
    event_logging_enabled: bool = True


class HistorySettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    retention_hours: int = Field(default=168, ge=24, le=2160)
    max_samples: int = Field(default=100_000, ge=1_000, le=2_000_000)
    compact_after_hours: int = Field(default=24, ge=1, le=720)
    process_sample_limit: int = Field(default=100, ge=10, le=500)

    @model_validator(mode="after")
    def validate_compaction_window(self) -> HistorySettings:
        if self.compact_after_hours > self.retention_hours:
            raise ValueError("Compaction cannot begin after history retention expires")
        return self


class WindowsEventSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    defender_enabled: bool = True
    system_enabled: bool = True
    application_enabled: bool = True
    poll_interval_seconds: float = Field(default=15.0, ge=5.0, le=300.0)
    max_events_per_channel: int = Field(default=100, ge=1, le=1000)
    retention_hours: int = Field(default=168, ge=24, le=2160)


class FileIntegritySettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    scan_interval_seconds: float = Field(default=30.0, ge=10.0, le=3600.0)
    max_files_per_folder: int = Field(default=10_000, ge=1, le=100_000)
    max_file_size_bytes: int = Field(default=104_857_600, ge=1024, le=1_073_741_824)
    event_retention: int = Field(default=10_000, ge=100, le=100_000)


class MonitoredFolder(BaseModel):
    id: str
    path: str
    enabled: bool = True
    created_at: datetime
    baseline_created_at: datetime | None = None
    status: Literal["pending", "healthy", "partial", "unavailable"] = "pending"
    status_detail: str = ""


class MonitoredFolderCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=3, max_length=1000)


class MonitoredFolderUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool


class FileBaselineEntry(BaseModel):
    relative_path: str
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    size: int = Field(ge=0)
    modified_ns: int = Field(ge=0)


class FileIntegrityEvent(BaseModel):
    id: str
    folder_id: str
    timestamp: datetime
    change_type: Literal["created", "modified", "deleted"]
    relative_path: str
    previous_sha256: str | None = None
    current_sha256: str | None = None
    size: int | None = Field(default=None, ge=0)


class FileScanResult(BaseModel):
    entries: list[FileBaselineEntry] = Field(default_factory=list)
    events: list[FileIntegrityEvent] = Field(default_factory=list)
    status: Literal["healthy", "partial", "unavailable"]
    status_detail: str = ""
    scanned_files: int = Field(default=0, ge=0)
    skipped_files: int = Field(default=0, ge=0)


DEFAULT_RULE_ENABLEMENT = {
    "cpu": True,
    "memory": True,
    "bandwidth": True,
    "connections": True,
    "connection_spike": True,
    "process_count": True,
    "disk_space": True,
    "destination_diversity": True,
    "collection_failure": True,
    "storage_failure": True,
}

# Rules whose observations merge into an alert lifecycle. Threshold alerts keep
# one ID per continuous breach episode. Security-event
# alerts are not listed: each event is its own alert.
GROUPED_ALERT_RULES = frozenset(
    {
        "cpu",
        "memory",
        "bandwidth",
        "connections",
        "connection_spike",
        "process_count",
        "disk_space",
        "destination_diversity",
        "collection_failure",
    }
)
THRESHOLD_ALERT_RULES = GROUPED_ALERT_RULES - {"collection_failure"}


class DetectionSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled_rules: dict[str, bool] = Field(default_factory=lambda: dict(DEFAULT_RULE_ENABLEMENT))
    cooldown_seconds: float = Field(default=60.0, ge=5.0, le=3600.0)
    process_count_threshold: int = Field(default=500, ge=10, le=100_000)
    disk_free_threshold_percent: float = Field(default=10.0, ge=1.0, le=50.0)
    destination_diversity_threshold: int = Field(default=100, ge=5, le=100_000)
    connection_growth_threshold: int = Field(default=40, ge=5, le=100_000)

    @field_validator("enabled_rules")
    @classmethod
    def validate_rule_names(cls, value: dict[str, bool]) -> dict[str, bool]:
        unknown = set(value) - set(DEFAULT_RULE_ENABLEMENT)
        if unknown:
            raise ValueError(f"Unknown detection rule: {sorted(unknown)[0]}")
        return {**DEFAULT_RULE_ENABLEMENT, **value}


class AISettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    endpoint: str = "http://127.0.0.1:11434"
    model: str = Field(default="llama3.2", min_length=1, max_length=80, pattern=r"^[\w.:-]+$")
    context_size: int = Field(default=4096, ge=512, le=32768)
    timeout_seconds: float = Field(default=20.0, ge=1.0, le=120.0)
    redact_telemetry: bool = True
    redaction_level: Literal["standard", "strict"] = "strict"

    @field_validator("endpoint")
    @classmethod
    def loopback_endpoint_only(cls, value: str) -> str:
        from urllib.parse import urlparse

        parsed = urlparse(value)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("Ollama endpoint must use HTTP on a loopback host")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Ollama endpoint must not contain credentials, query, or fragment")
        if parsed.path not in {"", "/"}:
            raise ValueError("Ollama endpoint must not contain a path")
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError("Ollama endpoint contains an invalid port") from exc
        if port is None or not 1 <= port <= 65535:
            raise ValueError("Ollama endpoint must include a valid port")
        return value.rstrip("/")


Severity = Literal["INFO", "WARNING", "CRITICAL"]
AnomalyLevel = Literal["LOW", "MODERATE", "HIGH"]


class Alert(BaseModel):
    id: str
    timestamp: datetime
    severity: Severity
    source: str
    metric: str
    explanation: str
    acknowledged: bool = False
    dismissed: bool = False
    rule_id: str | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)
    incident_id: str | None = None
    persistence_pending: bool = False
    first_occurrence: datetime | None = None
    last_occurrence: datetime | None = None
    occurrence_count: int = Field(default=1, ge=0)
    condition_active: bool | None = None

    _bound_identifiers = field_validator("id", "incident_id", mode="before")(_alert_identifier)
    _bound_labels = field_validator("source", "metric", "rule_id", mode="before")(_alert_label)
    _bound_explanation = field_validator("explanation", mode="before")(_alert_text)
    _sanitize_evidence = field_validator("evidence", mode="before")(safe_evidence)
    last_recovery: datetime | None = None


class AlertUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    acknowledged: bool | None = None
    dismissed: bool | None = None


class SystemSample(BaseModel):
    timestamp: datetime
    cpu_percent: float
    memory_percent: float
    memory_used: int
    memory_total: int
    memory_available: int | None = None
    memory_cached: int | None = None
    commit_used: int | None = None
    commit_limit: int | None = None
    commit_percent: float | None = None
    pagefile_used: int | None = None
    pagefile_total: int | None = None
    pagefile_percent: float | None = None
    process_count: int
    uptime_seconds: float
    disk_percent: float | None = None
    collection_error: str | None = None
    unavailable_metrics: list[str] = Field(default_factory=list)


class NetworkSample(BaseModel):
    bytes_sent_per_second: float
    bytes_recv_per_second: float
    upload_mbps: float
    download_mbps: float
    active_connections: int
    established_connections: int
    unique_remote_destinations: int = 0
    new_connections: int = 0
    disappeared_connections: int = 0
    collection_error: str | None = None
    unavailable_metrics: list[str] = Field(default_factory=list)
    connection_collection_status: Literal["healthy", "partial", "unavailable"] = "healthy"
    lifecycle_confidence: Literal["high", "partial", "unavailable"] = "high"
    connection_collection_reason: Literal[
        "none", "permission_denied", "provider_error", "unsupported"
    ] = "none"


class HistoricalSample(BaseModel):
    timestamp: datetime
    sequence: int
    cpu_percent: float | None = None
    memory_percent: float | None = None
    disk_percent: float | None = None
    upload_mbps: float | None = None
    download_mbps: float | None = None
    active_connections: int | None = None
    established_connections: int | None = None
    process_count: int | None = None
    collector_status: Literal["healthy", "degraded", "unavailable"]
    unavailable_metrics: list[str] = Field(default_factory=list)
    resolution_seconds: int = Field(default=0, ge=0)
    sample_count: int = Field(default=1, ge=1)
    coverage_start: datetime | None = None
    coverage_end: datetime | None = None
    metric_sample_counts: dict[str, int] = Field(default_factory=dict)


AddressScope = Literal["loopback", "private", "link-local", "external", "unspecified", "unknown"]


class ConnectionInfo(BaseModel):
    id: str = ""
    pid: int | None
    process_name: str
    local_address: str
    local_port: int | None
    remote_address: str
    remote_port: int | None
    protocol: str
    status: str
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    duration_seconds: float | None = None
    address_scope: AddressScope = "unknown"
    is_new: bool = False


class ConnectionAggregate(BaseModel):
    pid: int | None
    process_name: str
    connection_count: int
    established_count: int
    destination_count: int


class HistoricalConnectionSummary(BaseModel):
    timestamp: datetime
    sequence: int
    active_connections: int | None = None
    established_connections: int | None = None
    unique_remote_destinations: int | None = None
    new_connections: int | None = None
    disappeared_connections: int | None = None
    collection_status: Literal["healthy", "partial", "unavailable"]
    lifecycle_confidence: Literal["high", "partial", "unavailable"]
    top_processes: list[ConnectionAggregate] = Field(default_factory=list)


class ProcessInfo(BaseModel):
    pid: int
    name: str
    cpu_percent: float
    memory_percent: float
    memory_bytes: int
    status: str
    connection_count: int = 0
    executable: str | None = None
    parent_pid: int | None = None
    create_time: datetime | None = None
    username: str | None = None
    first_observed: datetime | None = None
    last_observed: datetime | None = None
    remote_endpoints: list[str] = Field(default_factory=list)
    access_status: Literal["available", "partial", "unavailable"] = "available"
    unavailable_fields: list[str] = Field(default_factory=list)


class HistoricalProcessObservation(BaseModel):
    timestamp: datetime
    sequence: int
    pid: int
    create_time: datetime | None = None
    name: str
    cpu_percent: float | None = None
    memory_percent: float | None = None
    connection_count: int | None = None
    status: str
    access_status: Literal["available", "partial", "unavailable"] = "available"


class HistoryGap(BaseModel):
    start: datetime
    end: datetime
    duration_seconds: float = Field(ge=0)


class HistoricalIntelligence(BaseModel):
    start: datetime
    end: datetime
    samples: list[HistoricalSample] = Field(default_factory=list)
    process_observations: list[HistoricalProcessObservation] = Field(default_factory=list)
    connection_summaries: list[HistoricalConnectionSummary] = Field(default_factory=list)
    gaps: list[HistoryGap] = Field(default_factory=list)
    truncated: bool = False
    truncated_datasets: list[Literal["telemetry", "processes", "connections"]] = Field(
        default_factory=list
    )


class BaselineMetric(BaseModel):
    metric: str
    current: float
    mean: float
    standard_deviation: float
    percentile_95: float
    deviation_ratio: float
    level: AnomalyLevel
    sample_count: int
    explanation: str


class StorageHealth(BaseModel):
    status: Literal["healthy", "degraded", "recovering"] = "healthy"
    persistence_available: bool = True
    pending_writes: int = 0
    # Queued writes evicted at capacity since this process started.
    dropped_alert_writes: int = 0
    dropped_event_writes: int = 0
    last_success: datetime | None = None
    last_error_type: str | None = None
    schema_version: int | None = None


EventSeverity = Literal["LOW", "MODERATE", "HIGH", "CRITICAL"]


class EventRecord(BaseModel):
    id: str
    timestamp: datetime
    event_type: str = Field(min_length=1, max_length=80)
    severity: EventSeverity = "LOW"
    title: str = Field(min_length=1, max_length=MAX_ALERT_LABEL)
    summary: str = Field(default="", max_length=MAX_ALERT_TEXT)
    related_id: str | None = Field(default=None, max_length=MAX_ALERT_LABEL)
    evidence: dict[str, Any] = Field(default_factory=dict)

    _escape_text = field_validator(
        "id", "event_type", "title", "summary", "related_id", mode="before"
    )(escape_surrogates)
    _sanitize_evidence = field_validator("evidence", mode="before")(safe_evidence)


class WindowsEventRecord(BaseModel):
    id: str
    channel: str
    record_id: int = Field(ge=0)
    timestamp: datetime
    provider: str
    event_id: int = Field(ge=0)
    level: Literal["INFORMATION", "WARNING", "ERROR", "CRITICAL", "UNKNOWN"]
    category: Literal[
        "defender_detection",
        "defender_action",
        "service_failure",
        "system",
        "application",
    ]
    summary: str = Field(default="", max_length=4000)
    service_name: str | None = Field(default=None, max_length=300)
    correlation_tags: list[str] = Field(default_factory=list)


class WindowsChannelStatus(BaseModel):
    channel: str
    status: Literal[
        "healthy", "degraded", "permission_denied", "unavailable", "unsupported"
    ]
    detail: str = ""
    cursor: int = Field(default=0, ge=0)


class WindowsEventBatch(BaseModel):
    events: list[WindowsEventRecord] = Field(default_factory=list)
    channels: list[WindowsChannelStatus] = Field(default_factory=list)


IncidentStatus = Literal["NEW", "INVESTIGATING", "RESOLVED", "DISMISSED"]


class IncidentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=3, max_length=160)
    severity: Severity
    alert_ids: list[str] = Field(default_factory=list, max_length=100)
    evidence_summary: str = Field(default="", max_length=4000)
    analyst_notes: str = Field(default="", max_length=8000)


class IncidentUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: IncidentStatus | None = None
    analyst_notes: str | None = Field(default=None, max_length=8000)
    title: str | None = Field(default=None, min_length=3, max_length=160)


class Incident(BaseModel):
    id: str
    title: str
    severity: Severity
    detection_timestamp: datetime
    first_observed: datetime
    last_observed: datetime
    related_processes: list[int] = Field(default_factory=list)
    related_connections: list[str] = Field(default_factory=list)
    related_alerts: list[str] = Field(default_factory=list)
    evidence_summary: str = ""
    status: IncidentStatus = "NEW"
    analyst_notes: str = ""


class AIAnalysisRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=3, max_length=1000)
    alert_ids: list[str] = Field(default_factory=list, max_length=20)


class AIAnalysisResponse(BaseModel):
    observed_evidence: str
    possible_explanations: str
    missing_information: str
    recommended_checks: str
    model: str


class TelemetrySnapshot(BaseModel):
    type: Literal["telemetry"] = "telemetry"
    sequence: int
    system: SystemSample
    network: NetworkSample
    connections: list[ConnectionInfo]
    processes: list[ProcessInfo]
    alerts: list[Alert]
    connection_rankings: list[ConnectionAggregate] = Field(default_factory=list)
    recently_closed_connections: list[ConnectionInfo] = Field(default_factory=list)
    baselines: list[BaselineMetric] = Field(default_factory=list)
    storage: StorageHealth = Field(default_factory=StorageHealth)
    collection_duration_ms: float = 0.0
    process_collection_status: Literal["healthy", "partial", "unavailable"] = "healthy"
    process_collection_detail: str = ""
    processes_enumerated: int = 0
    processes_displayed: int = 0
    process_records_skipped: int = 0
    idle_processes_excluded: int = 0
