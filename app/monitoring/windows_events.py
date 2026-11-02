from __future__ import annotations

import os
import re
import subprocess
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from app.models import (
    WindowsChannelStatus,
    WindowsEventBatch,
    WindowsEventRecord,
    WindowsEventSettings,
)
from app.time_utils import parse_utc

DEFENDER_CHANNEL = "Microsoft-Windows-Windows Defender/Operational"
SYSTEM_CHANNEL = "System"
APPLICATION_CHANNEL = "Application"

SERVICE_FAILURE_IDS = {7000, 7001, 7009, 7011, 7023, 7024, 7031, 7034}
DEFENDER_DETECTION_IDS = {1006, 1007, 1008, 1009, 1116}
DEFENDER_ACTION_IDS = {1010, 1011, 1012, 1117, 1118, 1119}
LEVELS = {
    1: "CRITICAL",
    2: "ERROR",
    3: "WARNING",
    4: "INFORMATION",
}

Runner = Callable[..., subprocess.CompletedProcess[str]]


class WindowsEventParseError(ValueError):
    """A successful command returned a nonempty response that was not valid XML."""


@dataclass(frozen=True)
class _ParseResult:
    events: list[WindowsEventRecord]
    invalid_events: int = 0


class WindowsEventCollector:
    """Read-only, incremental wrapper around Windows' built-in wevtutil."""

    def __init__(
        self,
        runner: Runner = subprocess.run,
        *,
        platform_name: str | None = None,
    ) -> None:
        self._runner = runner
        self._platform_name = platform_name or os.name

    @staticmethod
    def enabled_channels(settings: WindowsEventSettings) -> list[str]:
        channels: list[str] = []
        if settings.defender_enabled:
            channels.append(DEFENDER_CHANNEL)
        if settings.system_enabled:
            channels.append(SYSTEM_CHANNEL)
        if settings.application_enabled:
            channels.append(APPLICATION_CHANNEL)
        return channels

    def collect(
        self,
        cursors: dict[str, int],
        settings: WindowsEventSettings,
    ) -> WindowsEventBatch:
        channels = self.enabled_channels(settings)
        if self._platform_name != "nt":
            return WindowsEventBatch(
                channels=[
                    WindowsChannelStatus(
                        channel=channel,
                        status="unsupported",
                        detail="Windows Event Log APIs are unavailable on this platform",
                        cursor=max(0, cursors.get(channel, 0)),
                    )
                    for channel in channels
                ]
            )

        events: list[WindowsEventRecord] = []
        statuses: list[WindowsChannelStatus] = []
        seen: set[str] = set()
        for channel in channels:
            cursor = max(0, cursors.get(channel, 0))
            command = ["wevtutil", "qe", channel]
            if cursor:
                command.append(f"/q:*[System[(EventRecordID>{cursor})]]")
                command.append("/rd:false")
            else:
                command.append("/rd:true")
            command.extend(
                [
                    "/f:xml",
                    f"/c:{settings.max_events_per_channel}",
                    "/e:Events",
                ]
            )
            try:
                result = self._runner(
                    command,
                    capture_output=True,
                    text=True,
                    timeout=8.0,
                    check=False,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except (OSError, subprocess.SubprocessError) as exc:
                statuses.append(
                    WindowsChannelStatus(
                        channel=channel,
                        status="unavailable",
                        detail=f"Event query failed ({type(exc).__name__})",
                        cursor=cursor,
                    )
                )
                continue
            if result.returncode != 0:
                error = (result.stderr or "").lower()
                permission_denied = "access is denied" in error or "error 5" in error
                statuses.append(
                    WindowsChannelStatus(
                        channel=channel,
                        status="permission_denied" if permission_denied else "unavailable",
                        detail=(
                            "Permission denied; GhostWatch continued without this channel"
                            if permission_denied
                            else "Windows Event Log channel could not be read"
                        ),
                        cursor=cursor,
                    )
                )
                continue
            try:
                parsed = self._parse(channel, result.stdout or "")
            except WindowsEventParseError:
                statuses.append(
                    WindowsChannelStatus(
                        channel=channel,
                        status="unavailable",
                        detail="Windows Event Log response could not be parsed; cursor retained",
                        cursor=cursor,
                    )
                )
                continue
            accepted: list[WindowsEventRecord] = []
            for event in parsed.events:
                if event.record_id <= cursor or event.id in seen:
                    continue
                seen.add(event.id)
                accepted.append(event)
            accepted.sort(key=lambda event: (event.record_id, event.timestamp))
            events.extend(accepted)
            degraded = parsed.invalid_events > 0
            new_cursor = (
                cursor
                if degraded
                else max([cursor, *(event.record_id for event in accepted)])
            )
            statuses.append(
                WindowsChannelStatus(
                    channel=channel,
                    status="degraded" if degraded else "healthy",
                    detail=(
                        f"{parsed.invalid_events} event record"
                        f"{'s' if parsed.invalid_events != 1 else ''} could not be "
                        "normalized; cursor retained for retry"
                        if degraded
                        else ""
                    ),
                    cursor=new_cursor,
                )
            )
        return WindowsEventBatch(events=events, channels=statuses)

    def _parse(self, channel: str, output: str) -> _ParseResult:
        cleaned = re.sub(r"<\?xml[^>]*\?>", "", output).strip()
        if not cleaned:
            return _ParseResult([])
        if cleaned.startswith("<Event") and not cleaned.startswith("<Events"):
            cleaned = f"<Events>{cleaned}</Events>"
        elif not cleaned.startswith("<Events"):
            raise WindowsEventParseError
        try:
            root = ET.fromstring(cleaned)
        except ET.ParseError as exc:
            raise WindowsEventParseError from exc
        if root.tag.rsplit("}", 1)[-1] != "Events":
            raise WindowsEventParseError
        if (root.text or "").strip():
            raise WindowsEventParseError
        records: list[WindowsEventRecord] = []
        invalid_events = 0
        for node in root.findall(".//{*}Event"):
            system = node.find("{*}System")
            if system is None:
                invalid_events += 1
                continue
            record_id = self._integer(system.findtext("{*}EventRecordID"))
            event_id = self._integer(system.findtext("{*}EventID"))
            if record_id is None or record_id < 0 or event_id is None or event_id < 0:
                invalid_events += 1
                continue
            provider_node = system.find("{*}Provider")
            provider = (
                provider_node.attrib.get("Name", "Unknown")
                if provider_node is not None
                else "Unknown"
            )
            time_node = system.find("{*}TimeCreated")
            timestamp = self._timestamp(
                time_node.attrib.get("SystemTime") if time_node is not None else None
            )
            if timestamp is None:
                invalid_events += 1
                continue
            level_code = self._integer(system.findtext("{*}Level"))
            values = self._event_values(node)
            category, tags = self._classify(channel, event_id)
            summary = self._summary(node, values, provider, event_id)
            service_name = self._service_name(values) if category == "service_failure" else None
            records.append(
                WindowsEventRecord(
                    id=f"windows:{channel}:{record_id}",
                    channel=channel,
                    record_id=record_id,
                    timestamp=timestamp,
                    provider=provider[:300],
                    event_id=event_id,
                    level=LEVELS.get(level_code or 0, "UNKNOWN"),
                    category=category,
                    summary=summary,
                    service_name=service_name,
                    correlation_tags=tags,
                )
            )
        return _ParseResult(records, invalid_events)

    @staticmethod
    def _integer(value: str | None) -> int | None:
        try:
            return int(value) if value is not None else None
        except ValueError:
            return None

    @staticmethod
    def _timestamp(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            return parse_utc(value, label="Windows event timestamp")
        except ValueError:
            return None

    @staticmethod
    def _event_values(node: ET.Element) -> dict[str, str]:
        values: dict[str, str] = {}
        for index, data in enumerate(node.findall(".//{*}EventData/{*}Data"), start=1):
            text = (data.text or "").strip()
            if text:
                values[data.attrib.get("Name") or f"param{index}"] = text
        return values

    @staticmethod
    def _classify(channel: str, event_id: int) -> tuple[str, list[str]]:
        if channel == DEFENDER_CHANNEL and event_id in DEFENDER_DETECTION_IDS:
            return "defender_detection", ["defender", "detection"]
        if channel == DEFENDER_CHANNEL and event_id in DEFENDER_ACTION_IDS:
            return "defender_action", ["defender", "action"]
        if channel == SYSTEM_CHANNEL and event_id in SERVICE_FAILURE_IDS:
            return "service_failure", ["service", "failure"]
        if channel == APPLICATION_CHANNEL:
            return "application", ["application"]
        return "system", ["system"]

    @staticmethod
    def _summary(
        node: ET.Element, values: dict[str, str], provider: str, event_id: int
    ) -> str:
        message = node.findtext(".//{*}RenderingInfo/{*}Message")
        if message and message.strip():
            return " ".join(message.split())[:4000]
        evidence = "; ".join(f"{key}={value}" for key, value in list(values.items())[:20])
        fallback = f"{provider} event {event_id}"
        return f"{fallback}: {evidence}"[:4000] if evidence else fallback

    @staticmethod
    def _service_name(values: dict[str, str]) -> str | None:
        for key in ("ServiceName", "serviceName", "param1"):
            if values.get(key):
                return values[key][:300]
        return None
