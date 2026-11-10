from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.models import DetectionSettings, NetworkSample, Settings, SystemSample


@dataclass(frozen=True, slots=True)
class RuleEvaluation:
    key: str
    breached: bool
    severity: str
    source: str
    metric: str
    explanation: str
    duration: float
    evidence: dict[str, Any] = field(default_factory=dict)
    # False when the metric could not be read: not breached, but not cleared either.
    available: bool = True


def evaluate_rules(
    system: SystemSample,
    network: NetworkSample,
    settings: Settings,
    previous_connection_count: int | None,
    detection: DetectionSettings | None = None,
) -> list[RuleEvaluation]:
    detection = detection or DetectionSettings()
    system_unavailable = set(system.unavailable_metrics)
    network_unavailable = set(network.unavailable_metrics)
    collection_errors = [
        error for error in (system.collection_error, network.collection_error) if error
    ]
    collection_error = "; ".join(collection_errors)
    connection_jump = (
        "established_connections" not in network_unavailable
        and previous_connection_count is not None
        and network.established_connections - previous_connection_count
        >= detection.connection_growth_threshold
    )
    disk_free = None if system.disk_percent is None else 100.0 - system.disk_percent
    definitions = [
        RuleEvaluation(
            "cpu",
            "cpu_percent" not in system_unavailable
            and system.cpu_percent >= settings.cpu_threshold,
            "CRITICAL",
            "System",
            f"CPU {system.cpu_percent:.1f}%",
            f"CPU remained above the configured {settings.cpu_threshold:.0f}% threshold. "
            "High use is not proof of malicious activity.",
            settings.threshold_duration,
            {"observed_percent": system.cpu_percent, "threshold_percent": settings.cpu_threshold},
            "cpu_percent" not in system_unavailable,
        ),
        RuleEvaluation(
            "memory",
            "memory_percent" not in system_unavailable
            and system.memory_percent >= settings.memory_threshold,
            "CRITICAL",
            "System",
            f"Memory {system.memory_percent:.1f}%",
            f"Memory remained above the configured {settings.memory_threshold:.0f}% threshold.",
            settings.threshold_duration,
            {
                "observed_percent": system.memory_percent,
                "threshold_percent": settings.memory_threshold,
            },
            "memory_percent" not in system_unavailable,
        ),
        RuleEvaluation(
            "bandwidth",
            "upload_mbps" not in network_unavailable
            and network.upload_mbps >= settings.bandwidth_threshold_mbps,
            "WARNING",
            "Network",
            f"Upload {network.upload_mbps:.2f} MB/s",
            f"Outgoing bandwidth remained above {settings.bandwidth_threshold_mbps:.1f} "
            "MB/s. Review the responsible applications.",
            settings.threshold_duration,
            {
                "observed_mbps": network.upload_mbps,
                "threshold_mbps": settings.bandwidth_threshold_mbps,
            },
            "upload_mbps" not in network_unavailable,
        ),
        RuleEvaluation(
            "connections",
            "established_connections" not in network_unavailable
            and network.established_connections >= settings.connection_threshold,
            "WARNING",
            "Network",
            f"{network.established_connections} established connections",
            "Established TCP connections exceeded the configured threshold of "
            f"{settings.connection_threshold}.",
            settings.threshold_duration,
            {
                "observed_count": network.established_connections,
                "threshold_count": settings.connection_threshold,
            },
            "established_connections" not in network_unavailable,
        ),
        RuleEvaluation(
            "connection_spike",
            connection_jump,
            "WARNING",
            "Network",
            "Rapid connection increase",
            "Established connection count increased by at least "
            f"{detection.connection_growth_threshold} in one sampling interval.",
            0.0,
            {
                "previous_count": previous_connection_count,
                "observed_count": network.established_connections,
                "growth_threshold": detection.connection_growth_threshold,
            },
            "established_connections" not in network_unavailable
            and previous_connection_count is not None,
        ),
        RuleEvaluation(
            "process_count",
            "process_count" not in system_unavailable
            and system.process_count >= detection.process_count_threshold,
            "WARNING",
            "Process",
            f"{system.process_count} running processes",
            "The accessible process count exceeded the configured threshold. "
            "This may reflect normal workload growth.",
            settings.threshold_duration,
            {
                "observed_count": system.process_count,
                "threshold_count": detection.process_count_threshold,
            },
            "process_count" not in system_unavailable,
        ),
        RuleEvaluation(
            "disk_space",
            disk_free is not None and disk_free <= detection.disk_free_threshold_percent,
            "WARNING",
            "System",
            f"{disk_free:.1f}% disk free" if disk_free is not None else "Disk unavailable",
            "Available system-drive space is below the configured threshold.",
            settings.threshold_duration,
            {
                "free_percent": disk_free,
                "threshold_percent": detection.disk_free_threshold_percent,
            },
            disk_free is not None,
        ),
        RuleEvaluation(
            "destination_diversity",
            "unique_remote_destinations" not in network_unavailable
            and network.unique_remote_destinations
            >= detection.destination_diversity_threshold,
            "WARNING",
            "Network",
            f"{network.unique_remote_destinations} remote destinations",
            "The number of distinct remote destinations exceeded the configured threshold. "
            "Review the responsible processes before drawing conclusions.",
            settings.threshold_duration,
            {
                "observed_count": network.unique_remote_destinations,
                "threshold_count": detection.destination_diversity_threshold,
            },
            "unique_remote_destinations" not in network_unavailable,
        ),
        RuleEvaluation(
            "collection_failure",
            bool(collection_error),
            "INFO",
            "Collector",
            "Telemetry collection degraded",
            collection_error or "Telemetry collection recovered.",
            0.0,
            {"unavailable_metrics": sorted(system_unavailable | network_unavailable)},
        ),
    ]
    return [
        rule for rule in definitions if detection.enabled_rules.get(rule.key, False)
    ]
