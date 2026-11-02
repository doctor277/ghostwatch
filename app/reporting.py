from __future__ import annotations

import csv
import html
import io
import json
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models import EventRecord, Incident, TelemetrySnapshot
from app.privacy import RedactionLevel, redact_structure
from app.time_utils import as_utc, parse_utc

ReportType = Literal[
    "system-health",
    "network-activity",
    "security-alerts",
    "process-activity",
    "anomaly-summary",
    "incidents",
    "daily-summary",
    "weekly-summary",
]


class ReportEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["3.0"] = "3.0"
    report_type: ReportType
    generated_at: datetime
    range_start: datetime
    range_end: datetime
    scope: str
    data_availability_notes: list[str] = Field(default_factory=list)
    data: dict[str, Any]


def csv_safe(value: object) -> str:
    text = str(value)
    if text.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + text
    return text


def redact_payload(value: Any, level: RedactionLevel = "standard") -> Any:
    return redact_structure(value, level)


def _event_dicts(
    events: list[EventRecord], start: datetime | None = None
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for event in events:
        row = event.model_dump(mode="json")
        timestamp = row.get("timestamp")
        observed = (
            timestamp
            if isinstance(timestamp, datetime)
            else parse_utc(str(timestamp), label="report event timestamp")
        )
        observed = as_utc(observed, label="report event timestamp")
        if start is None or observed >= start:
            rows.append(row)
    return rows


def _incident_dicts(
    incidents: list[Incident], start: datetime | None = None
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for incident in incidents:
        row = incident.model_dump(mode="json")
        timestamp = row.get("last_observed")
        observed = (
            timestamp
            if isinstance(timestamp, datetime)
            else parse_utc(str(timestamp), label="report incident timestamp")
        )
        observed = as_utc(observed, label="report incident timestamp")
        if start is None or observed >= start:
            rows.append(row)
    return rows


def _severity_counts(events: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for event in events:
        severity = str(event.get("severity", "UNKNOWN"))
        counts[severity] = counts.get(severity, 0) + 1
    return counts


def _type_specific_data(
    report_type: ReportType,
    snapshot: TelemetrySnapshot | None,
    events: list[EventRecord],
    incidents: list[Incident],
    range_start: datetime,
) -> dict[str, Any]:
    event_rows = _event_dicts(events, range_start)
    incident_rows = _incident_dicts(incidents, range_start)
    if report_type == "system-health":
        return {
            "system_metrics": (
                snapshot.system.model_dump(mode="json") if snapshot else None
            ),
            "storage_health": (
                snapshot.storage.model_dump(mode="json") if snapshot else None
            ),
            "collection_duration_ms": (
                snapshot.collection_duration_ms if snapshot else None
            ),
            "monitoring_events": [
                row
                for row in event_rows
                if row.get("event_type")
                in {"collection_failure", "storage_failure", "storage_recovery"}
            ],
        }
    if report_type == "network-activity":
        return {
            "network_metrics": (
                snapshot.network.model_dump(mode="json") if snapshot else None
            ),
            "connection_rankings": (
                [
                    item.model_dump(mode="json")
                    for item in snapshot.connection_rankings
                ]
                if snapshot
                else []
            ),
            "connection_details": (
                [item.model_dump(mode="json") for item in snapshot.connections]
                if snapshot
                else []
            ),
            "network_events": [
                row for row in event_rows if "network" in str(row.get("event_type", ""))
            ],
        }
    if report_type == "process-activity":
        return {
            "processes": (
                [item.model_dump(mode="json") for item in snapshot.processes]
                if snapshot
                else []
            ),
            "process_events": [
                row for row in event_rows if "process" in str(row.get("event_type", ""))
            ],
        }
    if report_type == "security-alerts":
        return {
            "alerts": (
                [item.model_dump(mode="json") for item in snapshot.alerts]
                if snapshot
                else []
            ),
            "detection_events": event_rows,
            "severity_counts": _severity_counts(event_rows),
        }
    if report_type == "anomaly-summary":
        return {
            "baseline_deviations": (
                [item.model_dump(mode="json") for item in snapshot.baselines]
                if snapshot
                else []
            ),
            "anomaly_events": [
                row
                for row in event_rows
                if "anomal" in str(row.get("event_type", "")).casefold()
                or row.get("event_type") == "alert"
            ],
        }
    if report_type == "incidents":
        return {"incidents": incident_rows}
    return {
        "current_snapshot": snapshot.model_dump(mode="json") if snapshot else None,
        "events": event_rows,
        "incidents": incident_rows,
        "summary": {
            "event_count": len(event_rows),
            "incident_count": len(incident_rows),
            "severity_counts": _severity_counts(event_rows),
        },
    }


def report_payload(
    report_type: str,
    snapshot: TelemetrySnapshot | None,
    events: list[EventRecord],
    incidents: list[Incident],
    redact: bool = False,
    generated_at: datetime | None = None,
    redaction_level: RedactionLevel = "standard",
) -> dict[str, Any]:
    now = as_utc(generated_at or datetime.now(UTC), label="report generation timestamp")
    ranges = {
        "daily-summary": timedelta(days=1),
        "weekly-summary": timedelta(days=7),
    }
    window = ranges.get(report_type, timedelta(days=1))
    range_start = now - window
    notes: list[str] = []
    if snapshot is None:
        notes.append("No live telemetry snapshot was available at generation time.")
    else:
        unavailable = sorted(
            set(snapshot.system.unavailable_metrics)
            | set(snapshot.network.unavailable_metrics)
        )
        if unavailable:
            notes.append("Unavailable metrics: " + ", ".join(unavailable))
    try:
        typed_report_type: ReportType = report_type  # type: ignore[assignment]
        data = _type_specific_data(
            typed_report_type, snapshot, events, incidents, range_start
        )
        envelope = ReportEnvelope.model_validate(
            {
                "report_type": report_type,
                "generated_at": now,
                "range_start": range_start,
                "range_end": now,
                "scope": "Local observations only; anomalies are not proof of compromise.",
                "data_availability_notes": notes,
                "data": data,
            }
        ).model_dump(mode="json")
    except Exception:
        # Preserve validation errors for the API boundary without partial report output.
        raise
    return redact_payload(envelope, redaction_level) if redact else envelope


def _flatten(value: Any, prefix: str = "data") -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    if isinstance(value, dict):
        for key, item in value.items():
            rows.extend(_flatten(item, f"{prefix}.{key}"))
    elif isinstance(value, list):
        if not value:
            rows.append((prefix, "[]"))
        else:
            for index, item in enumerate(value):
                rows.extend(_flatten(item, f"{prefix}[{index}]"))
    else:
        rows.append((prefix, "" if value is None else str(value)))
    return rows


def render_report(payload: dict[str, Any], output_format: str) -> tuple[bytes, str]:
    validated = ReportEnvelope.model_validate(payload).model_dump(mode="json")
    if output_format == "json":
        return (
            json.dumps(validated, indent=2, default=str).encode("utf-8"),
            "application/json",
        )
    rows = _flatten(validated["data"])
    if output_format == "csv":
        stream = io.StringIO(newline="")
        writer = csv.writer(stream)
        writer.writerow(["field", "value"])
        for field, value in rows:
            writer.writerow([csv_safe(field), csv_safe(value)])
        return stream.getvalue().encode("utf-8-sig"), "text/csv"
    if output_format == "html":
        availability = "".join(
            f"<li>{html.escape(note)}</li>"
            for note in validated["data_availability_notes"]
        ) or "<li>No known data gaps were reported.</li>"
        table_rows = "".join(
            "<tr>"
            f"<th>{html.escape(field)}</th>"
            f"<td>{html.escape(value)}</td>"
            "</tr>"
            for field, value in rows
        )
        document = f"""<!doctype html>
<html lang="en"><meta charset="utf-8"><title>GhostWatch report</title>
<style>body{{font:14px system-ui;background:#07111c;color:#d8e8f5;padding:2rem}}
table{{border-collapse:collapse;width:100%}}td,th{{border:1px solid #29445c;padding:.5rem;
text-align:left;vertical-align:top}}small{{color:#9fb3c5}}</style>
<h1>GhostWatch {html.escape(str(validated['report_type']))} report</h1>
<p>Generated {html.escape(str(validated['generated_at']))}</p>
<p>Range {html.escape(str(validated['range_start']))} to
{html.escape(str(validated['range_end']))}</p>
<small>{html.escape(str(validated['scope']))}</small>
<h2>Data availability</h2><ul>{availability}</ul>
<h2>Report data</h2><table><tbody>{table_rows}</tbody></table></html>"""
        return document.encode("utf-8"), "text/html"
    raise ValueError("Unsupported report format")
