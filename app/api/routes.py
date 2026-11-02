from __future__ import annotations

import csv
import io
import json
from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response

from app.ai import AIServiceUnavailable
from app.models import (
    AIAnalysisRequest,
    AIAnalysisResponse,
    AISettings,
    Alert,
    AlertUpdate,
    DetectionSettings,
    EventRecord,
    FileIntegrityEvent,
    FileIntegritySettings,
    HistoricalIntelligence,
    HistoricalSample,
    HistorySettings,
    Incident,
    IncidentCreate,
    IncidentUpdate,
    MonitoredFolder,
    MonitoredFolderCreate,
    MonitoredFolderUpdate,
    Settings,
    TelemetrySnapshot,
    WindowsEventRecord,
    WindowsEventSettings,
)
from app.privacy import redact_text
from app.reporting import render_report, report_payload
from app.service import StorageUnavailableError
from app.time_utils import as_utc

router = APIRouter(prefix="/api")


def _history_window(
    start: datetime | None, end: datetime | None
) -> tuple[datetime, datetime]:
    selected_end = end or datetime.now(UTC)
    selected_start = start or selected_end - timedelta(hours=1)
    if selected_start.tzinfo is None or selected_end.tzinfo is None:
        raise HTTPException(status_code=422, detail="History timestamps require a timezone")
    selected_start = as_utc(selected_start, label="history start")
    selected_end = as_utc(selected_end, label="history end")
    if selected_start >= selected_end:
        raise HTTPException(status_code=422, detail="History start must precede end")
    if selected_end - selected_start > timedelta(days=90):
        raise HTTPException(status_code=422, detail="History range exceeds 90 days")
    return selected_start, selected_end


@router.get("/health")
async def health(request: Request) -> dict[str, object]:
    service = request.app.state.telemetry
    return {
        "status": "ok",
        "scope": "local-only",
        "monitoring": "active" if service.latest is not None else "warming-up",
        "storage": service.storage.model_dump(mode="json"),
        "websocket_clients": len(service.clients),
    }


@router.get("/overview", response_model=TelemetrySnapshot)
async def overview(request: Request) -> TelemetrySnapshot:
    service = request.app.state.telemetry
    if service.latest is None:
        raise HTTPException(status_code=503, detail="Telemetry is warming up")
    return service.latest


@router.get("/history")
async def history(request: Request) -> dict[str, list[dict[str, object]]]:
    return {"samples": list(request.app.state.telemetry.history)}


@router.get("/history/query", response_model=list[HistoricalSample])
async def historical_query(
    request: Request,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = Query(default=10_000, ge=1, le=100_000),
) -> list[HistoricalSample]:
    selected_start, selected_end = _history_window(start, end)
    try:
        return await request.app.state.telemetry.database_query_history(
            selected_start, selected_end, limit
        )
    except StorageUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/history/intelligence", response_model=HistoricalIntelligence)
async def historical_intelligence(
    request: Request,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = Query(default=10_000, ge=1, le=100_000),
) -> HistoricalIntelligence:
    selected_start, selected_end = _history_window(start, end)
    try:
        return await request.app.state.telemetry.database_query_intelligence(
            selected_start, selected_end, limit
        )
    except StorageUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/history/export")
async def export_history(
    request: Request,
    start: datetime | None = None,
    end: datetime | None = None,
    dataset: Literal["all", "telemetry", "processes", "connections"] = "all",
    format: Literal["json", "csv"] = "json",
    limit: int = Query(default=10_000, ge=1, le=100_000),
) -> Response:
    selected_start, selected_end = _history_window(start, end)
    try:
        result = await request.app.state.telemetry.database_query_intelligence(
            selected_start, selected_end, limit
        )
    except StorageUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    filename = f"ghostwatch-history-{selected_start:%Y%m%dT%H%M}-{selected_end:%Y%m%dT%H%M}"
    selected_truncated = (
        result.truncated
        if dataset == "all"
        else dataset in result.truncated_datasets
    )
    response_headers = {
        "Content-Disposition": f'attachment; filename="{filename}.json"',
        "X-GhostWatch-History-Truncated": str(selected_truncated).lower(),
    }
    if format == "json":
        payload = result.model_dump(mode="json")
        if dataset != "all":
            selected_key = {
                "telemetry": "samples",
                "processes": "process_observations",
                "connections": "connection_summaries",
            }[dataset]
            payload = {
                "start": payload["start"],
                "end": payload["end"],
                "dataset": dataset,
                selected_key: payload[selected_key],
                "truncated": selected_truncated,
            }
        response_headers["Content-Disposition"] = (
            f'attachment; filename="{filename}.json"'
        )
        return Response(
            content=result.model_dump_json(indent=2)
            if dataset == "all"
            else json.dumps(payload, indent=2),
            media_type="application/json",
            headers=response_headers,
        )

    selected = {
        "telemetry": result.samples,
        "processes": result.process_observations,
        "connections": result.connection_summaries,
    }
    rows: list[dict[str, object]] = []
    for name, items in selected.items():
        if dataset not in {"all", name}:
            continue
        for item in items:
            row = {"dataset": name, **item.model_dump(mode="json")}
            rows.append(row)
    fields = sorted({key for row in rows for key in row}) or ["dataset"]
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(
            {
                key: (
                    f"'{value}"
                    if isinstance(value, str) and value[:1] in {"=", "+", "-", "@"}
                    else value
                )
                for key, value in row.items()
            }
        )
    response_headers["Content-Disposition"] = f'attachment; filename="{filename}.csv"'
    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers=response_headers,
    )


@router.get("/history/settings", response_model=HistorySettings)
async def get_history_settings(request: Request) -> HistorySettings:
    return request.app.state.telemetry.history_settings


@router.put("/history/settings", response_model=HistorySettings)
async def update_history_settings(
    settings: HistorySettings, request: Request
) -> HistorySettings:
    try:
        return await request.app.state.telemetry.update_history_settings(settings)
    except StorageUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/windows-events", response_model=list[WindowsEventRecord])
async def windows_events(
    request: Request,
    limit: int = Query(default=500, ge=1, le=5000),
    channel: str | None = Query(default=None, max_length=200),
    category: str | None = Query(default=None, max_length=80),
    level: str | None = Query(default=None, max_length=20),
    search: str | None = Query(default=None, max_length=200),
    start: datetime | None = None,
    end: datetime | None = None,
    privacy: Literal["redacted", "raw"] = "redacted",
) -> list[WindowsEventRecord]:
    if start and start.tzinfo is None or end and end.tzinfo is None:
        raise HTTPException(status_code=422, detail="Event timestamps require a timezone")
    start = as_utc(start, label="event start") if start else None
    end = as_utc(end, label="event end") if end else None
    if start and end and start >= end:
        raise HTTPException(status_code=422, detail="Event start must precede end")
    try:
        events = await request.app.state.telemetry.query_windows_events(
            limit=limit,
            channel=channel,
            category=category,
            level=level,
            search=search,
            start=start,
            end=end,
        )
        if privacy == "raw":
            return events
        return [
            event.model_copy(
                update={
                    "summary": redact_text(event.summary),
                    "service_name": (
                        redact_text(event.service_name) if event.service_name else None
                    ),
                }
            )
            for event in events
        ]
    except StorageUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/windows-events/summary")
async def windows_event_summary(request: Request) -> dict[str, object]:
    return await request.app.state.telemetry.get_windows_event_summary()


@router.get("/windows-events/settings", response_model=WindowsEventSettings)
async def get_windows_event_settings(request: Request) -> WindowsEventSettings:
    return request.app.state.telemetry.windows_event_settings


@router.put("/windows-events/settings", response_model=WindowsEventSettings)
async def update_windows_event_settings(
    settings: WindowsEventSettings, request: Request
) -> WindowsEventSettings:
    try:
        return await request.app.state.telemetry.update_windows_event_settings(settings)
    except StorageUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/file-integrity/settings", response_model=FileIntegritySettings)
async def get_file_integrity_settings(request: Request) -> FileIntegritySettings:
    return request.app.state.telemetry.file_integrity_settings


@router.put("/file-integrity/settings", response_model=FileIntegritySettings)
async def update_file_integrity_settings(
    settings: FileIntegritySettings, request: Request
) -> FileIntegritySettings:
    try:
        return await request.app.state.telemetry.update_file_integrity_settings(settings)
    except StorageUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/file-integrity/folders", response_model=list[MonitoredFolder])
async def monitored_folders(request: Request) -> list[MonitoredFolder]:
    try:
        return await request.app.state.telemetry.list_monitored_folders()
    except StorageUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/file-integrity/folders", response_model=MonitoredFolder, status_code=201)
async def add_monitored_folder(
    payload: MonitoredFolderCreate, request: Request
) -> MonitoredFolder:
    try:
        return await request.app.state.telemetry.add_monitored_folder(payload.path)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except StorageUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.put("/file-integrity/folders/{folder_id}", response_model=MonitoredFolder)
async def update_monitored_folder(
    folder_id: str, payload: MonitoredFolderUpdate, request: Request
) -> MonitoredFolder:
    try:
        folder = await request.app.state.telemetry.set_monitored_folder_enabled(
            folder_id, payload.enabled
        )
    except StorageUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if folder is None:
        raise HTTPException(status_code=404, detail="Monitored folder not found")
    return folder


@router.delete("/file-integrity/folders/{folder_id}", status_code=204)
async def remove_monitored_folder(folder_id: str, request: Request) -> Response:
    try:
        removed = await request.app.state.telemetry.remove_monitored_folder(folder_id)
    except StorageUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not removed:
        raise HTTPException(status_code=404, detail="Monitored folder not found")
    return Response(status_code=204)


@router.post("/file-integrity/scan", response_model=list[FileIntegrityEvent])
async def scan_file_integrity(request: Request) -> list[FileIntegrityEvent]:
    return await request.app.state.telemetry.scan_file_integrity_once()


@router.get("/file-integrity/events", response_model=list[FileIntegrityEvent])
async def file_integrity_events(
    request: Request,
    limit: int = Query(default=500, ge=1, le=5000),
    folder_id: str | None = Query(default=None, max_length=100),
    change_type: str | None = Query(default=None, max_length=20),
    search: str | None = Query(default=None, max_length=200),
) -> list[FileIntegrityEvent]:
    try:
        return await request.app.state.telemetry.list_file_integrity_events(
            limit=limit,
            folder_id=folder_id,
            change_type=change_type,
            search=search,
        )
    except StorageUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/settings", response_model=Settings)
async def get_settings(request: Request) -> Settings:
    return request.app.state.telemetry.settings


@router.put("/settings", response_model=Settings)
async def update_settings(settings: Settings, request: Request) -> Settings:
    try:
        return await request.app.state.telemetry.update_settings(settings)
    except StorageUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/detection-settings", response_model=DetectionSettings)
async def get_detection_settings(request: Request) -> DetectionSettings:
    return request.app.state.telemetry.detection_settings


@router.put("/detection-settings", response_model=DetectionSettings)
async def update_detection_settings(
    settings: DetectionSettings, request: Request
) -> DetectionSettings:
    try:
        return await request.app.state.telemetry.update_detection_settings(settings)
    except StorageUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/ai/settings", response_model=AISettings)
async def get_ai_settings(request: Request) -> AISettings:
    return request.app.state.telemetry.ai_settings


@router.put("/ai/settings", response_model=AISettings)
async def update_ai_settings(settings: AISettings, request: Request) -> AISettings:
    try:
        return await request.app.state.telemetry.update_ai_settings(settings)
    except StorageUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/ai/analyze", response_model=AIAnalysisResponse)
async def analyze(
    payload: AIAnalysisRequest, request: Request
) -> AIAnalysisResponse:
    try:
        return await request.app.state.ai_analyst.analyze(
            payload,
            request.app.state.telemetry.ai_settings,
            request.app.state.telemetry.latest,
        )
    except AIServiceUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/alerts", response_model=list[Alert])
async def get_alerts(request: Request, include_dismissed: bool = False) -> list[Alert]:
    return await request.app.state.telemetry.list_alerts(200, include_dismissed)


@router.patch("/alerts/{alert_id}", response_model=Alert)
async def update_alert(alert_id: str, update: AlertUpdate, request: Request) -> Alert:
    result = await request.app.state.telemetry.update_alert(alert_id, update)
    if result is None:
        raise HTTPException(status_code=404, detail="Alert not found")
    return result


@router.get("/events", response_model=list[EventRecord])
async def get_events(
    request: Request,
    limit: int = Query(default=200, ge=1, le=1000),
    event_type: str | None = Query(default=None, max_length=80),
    severity: str | None = Query(default=None, pattern="^(LOW|MODERATE|HIGH|CRITICAL)$"),
    search: str | None = Query(default=None, max_length=100),
) -> list[EventRecord]:
    return await request.app.state.telemetry.list_events(
        limit, event_type, severity, search
    )


@router.post("/incidents", response_model=Incident, status_code=201)
async def create_incident(payload: IncidentCreate, request: Request) -> Incident:
    try:
        return await request.app.state.telemetry.create_incident(payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except StorageUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/incidents", response_model=list[Incident])
async def list_incidents(request: Request) -> list[Incident]:
    return await request.app.state.telemetry.list_incidents()


@router.patch("/incidents/{incident_id}", response_model=Incident)
async def update_incident(
    incident_id: str, payload: IncidentUpdate, request: Request
) -> Incident:
    try:
        result = await request.app.state.telemetry.update_incident(incident_id, payload)
    except StorageUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="Incident not found")
    return result


@router.get("/reports/{report_type}.{output_format}")
async def report(
    report_type: str,
    output_format: str,
    request: Request,
    redact: bool = True,
) -> Response:
    allowed_types = {
        "system-health",
        "network-activity",
        "security-alerts",
        "process-activity",
        "anomaly-summary",
        "incidents",
        "daily-summary",
        "weekly-summary",
    }
    if report_type not in allowed_types or output_format not in {"json", "csv", "html"}:
        raise HTTPException(status_code=404, detail="Unsupported report")
    events = await request.app.state.telemetry.list_events(1000)
    incidents = await request.app.state.telemetry.list_incidents()
    payload = report_payload(
        report_type,
        request.app.state.telemetry.latest,
        events,
        incidents,
        redact=redact,
    )
    body, media_type = render_report(payload, output_format)
    headers = {
        "Content-Disposition": (
            f'attachment; filename="ghostwatch-{report_type}.{output_format}"'
        ),
        "X-Content-Type-Options": "nosniff",
    }
    return Response(body, media_type=media_type, headers=headers)
