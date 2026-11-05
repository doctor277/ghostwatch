from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from fastapi import WebSocket
from pydantic import ValidationError

from app.analytics.baseline import BaselineEngine
from app.config import (
    MAX_HISTORY_SAMPLES,
    MAX_LIVE_ALERTS,
    MAX_SNAPSHOT_BYTES,
    MAX_WEBSOCKET_CLIENTS,
    WEBSOCKET_SEND_TIMEOUT_SECONDS,
)
from app.detection.engine import DetectionEngine
from app.models import (
    MAX_ALERT_LABEL,
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
    HistoricalIntelligence,
    HistoricalProcessObservation,
    HistoricalSample,
    HistoryGap,
    HistorySettings,
    Incident,
    IncidentCreate,
    IncidentUpdate,
    MonitoredFolder,
    NetworkSample,
    Settings,
    StorageHealth,
    SystemSample,
    TelemetrySnapshot,
    WindowsChannelStatus,
    WindowsEventRecord,
    WindowsEventSettings,
)
from app.monitoring.file_integrity import FileIntegrityCollector
from app.monitoring.network import NetworkCollector
from app.monitoring.processes import ProcessCollector
from app.monitoring.system import SystemCollector
from app.monitoring.windows_events import WindowsEventCollector
from app.storage.database import Database, InvalidRecordError
from app.text_utils import bounded_identifier
from app.time_utils import as_utc

LOGGER = logging.getLogger(__name__)
MAX_PENDING_WRITES = 500
DROP_LOG_INTERVAL = 100
MAX_LIVE_EVENTS = 1000
SNAPSHOT_DEGRADED_FRAME = '{"type":"telemetry_degraded","reason":"snapshot_unavailable"}'


@dataclass(frozen=True)
class HistoryWriteBatch:
    sample: HistoricalSample
    processes: list[HistoricalProcessObservation]
    connection_summary: HistoricalConnectionSummary


class StorageUnavailableError(RuntimeError):
    pass


class TelemetryService:
    def __init__(
        self,
        database: Database,
        *,
        max_clients: int = MAX_WEBSOCKET_CLIENTS,
        send_timeout: float = WEBSOCKET_SEND_TIMEOUT_SECONDS,
        outbound_queue_size: int = 2,
        storage_retry_seconds: float = 5.0,
    ) -> None:
        self.database = database
        self.settings = Settings()
        self.detection_settings = DetectionSettings()
        self.ai_settings = AISettings()
        self.history_settings = HistorySettings()
        self.windows_event_settings = WindowsEventSettings()
        self.file_integrity_settings = FileIntegritySettings()
        self.system_collector = SystemCollector()
        self.network_collector = NetworkCollector()
        self.process_collector = ProcessCollector()
        self.windows_event_collector = WindowsEventCollector()
        self.file_integrity_collector = FileIntegrityCollector()
        self.detection_engine = DetectionEngine()
        self.baseline_engine = BaselineEngine()
        self.history: deque[dict[str, Any]] = deque(maxlen=MAX_HISTORY_SAMPLES)
        self.live_alerts: deque[Alert] = deque(maxlen=MAX_LIVE_ALERTS)
        self.live_events: deque[EventRecord] = deque(maxlen=MAX_LIVE_EVENTS)
        self.live_windows_events: deque[WindowsEventRecord] = deque(maxlen=2000)
        self.live_file_integrity_events: deque[FileIntegrityEvent] = deque(maxlen=2000)
        self.windows_channel_status: list[WindowsChannelStatus] = []
        self.latest: TelemetrySnapshot | None = None
        self.clients: set[WebSocket] = set()
        self.max_clients = max_clients
        self.send_timeout = send_timeout
        self.outbound_queue_size = outbound_queue_size
        self._client_queues: dict[WebSocket, asyncio.Queue[TelemetrySnapshot]] = {}
        self._sender_tasks: dict[WebSocket, asyncio.Task[None]] = {}
        self._clients_lock = asyncio.Lock()
        self._encoded_snapshot: tuple[TelemetrySnapshot, str | None] | None = None
        self._snapshot_encoding_failed = False
        self._task: asyncio.Task[None] | None = None
        self._history_task: asyncio.Task[None] | None = None
        self._windows_task: asyncio.Task[None] | None = None
        self._file_integrity_task: asyncio.Task[None] | None = None
        self._history_queue: asyncio.Queue[HistoryWriteBatch] = asyncio.Queue(
            maxsize=1000
        )
        self.history_dropped_samples = 0
        self.rejected_writes = 0
        self.dropped_alert_writes = 0
        self.dropped_event_writes = 0
        self._writing_alert: str | None = None
        self._writing_alert_update: str | None = None
        self._writing_event: str | None = None
        self._history_writes = 0
        self._windows_cursors: dict[str, int] = {}
        self._windows_seen: OrderedDict[str, None] = OrderedDict()
        self._file_integrity_seen: OrderedDict[str, None] = OrderedDict()
        self._stop = asyncio.Event()
        self._lifecycle_lock = asyncio.Lock()
        self._collection_lock = asyncio.Lock()
        self._sequence = 0
        self._pending_alerts: OrderedDict[str, Alert] = OrderedDict()
        self._pending_events: OrderedDict[str, EventRecord] = OrderedDict()
        self._pending_alert_updates: OrderedDict[str, AlertUpdate] = OrderedDict()
        # Serialize observation writes and state patches without blocking an
        # operator action behind a held database write: actions queue their patch.
        self._alert_persistence_lock = asyncio.Lock()
        self._logging_generation = 0
        self._history_generation = 0
        self._configuration_generations: dict[str, int] = {}
        self._storage_retry_seconds = storage_retry_seconds
        self._next_storage_retry = 0.0
        self._persisted_state_missing = False
        self.storage = StorageHealth(
            status="recovering",
            persistence_available=False,
        )

    async def initialize_storage(self) -> bool:
        try:
            version = await asyncio.to_thread(self.database.initialize)
            await self._load_persisted_state()
        except Exception as exc:
            self._persisted_state_missing = True
            self._mark_storage_failure(exc)
            return False
        self._persisted_state_missing = False
        self._mark_storage_success(version)
        await self._flush_pending()
        return True

    def _apply_saved_configuration(self, attribute: str, settings: Any) -> None:
        self._configuration_generations[attribute] = (
            self._configuration_generations.get(attribute, 0) + 1
        )
        setattr(self, attribute, settings)

    async def _load_configuration(self, attribute: str, load: Callable[[], Any]) -> None:
        generation = self._configuration_generations.get(attribute, 0)
        settings = await asyncio.to_thread(load)
        # A successful save owns the entire settings model. A recovery read
        # started before that save cannot restore older fields or preferences.
        if generation == self._configuration_generations.get(attribute, 0):
            setattr(self, attribute, settings)

    async def _load_persisted_state(self) -> None:
        await self._load_configuration("settings", self.database.load_settings)
        if not self.settings.event_logging_enabled:
            self._logging_generation += 1
            self._discard_pending_writes()
        await self._load_configuration(
            "detection_settings", self.database.load_detection_settings
        )
        await self._load_configuration("ai_settings", self.database.load_ai_settings)
        await self._load_configuration(
            "history_settings", self.database.load_history_settings
        )
        if not self.history_settings.enabled:
            self._discard_pending_history()
        await self._load_configuration(
            "windows_event_settings", self.database.load_windows_event_settings
        )
        await self._load_configuration(
            "file_integrity_settings", self.database.load_file_integrity_settings
        )
        self._windows_cursors = await asyncio.to_thread(
            self.database.load_windows_event_cursors
        )
        baseline_state = await asyncio.to_thread(
            self.database.load_baseline_state
        )
        self.baseline_engine.restore_state(baseline_state)

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self._task is not None:
                return
            try:
                await self._start_workers()
            except BaseException:
                # Startup cancellation must also release workers already created.
                await self._stop_workers()
                raise

    async def _start_workers(self) -> None:
        if not self.storage.persistence_available:
            await self.initialize_storage()
        self._stop.clear()
        self._history_task = asyncio.create_task(
            self._history_writer(), name="ghostwatch-history-writer"
        )
        self._windows_task = asyncio.create_task(
            self._windows_event_loop(), name="ghostwatch-windows-events"
        )
        self._file_integrity_task = asyncio.create_task(
            self._file_integrity_loop(), name="ghostwatch-file-integrity"
        )
        await self.collect_once()
        self._task = asyncio.create_task(self._run(), name="ghostwatch-telemetry")

    async def stop(self) -> None:
        async with self._lifecycle_lock:
            cleanup = asyncio.create_task(self._stop_workers(), name="ghostwatch-shutdown")
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                # Finish the bounded async cleanup before propagating cancellation.
                await cleanup
                raise

    async def _stop_workers(self) -> None:
        self._stop.set()
        producers = [
            task for task in (self._task, self._windows_task, self._file_integrity_task)
            if task is not None
        ]
        for task in producers:
            task.cancel()
        results = await asyncio.gather(*producers, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                LOGGER.warning("Monitoring worker stopped with %s", type(result).__name__)
        self._task = self._windows_task = self._file_integrity_task = None
        if self._history_task:
            if not self._history_task.done():
                with suppress(TimeoutError):
                    await asyncio.wait_for(self._history_queue.join(), timeout=2.0)
            self._history_task.cancel()
            results = await asyncio.gather(self._history_task, return_exceptions=True)
            for result in results:
                if isinstance(result, Exception):
                    LOGGER.warning("History worker stopped with %s", type(result).__name__)
            self._history_task = None
        # Account for batches the bounded drain could not persist; do not replay
        # stale work on a later start. An in-flight thread may still finish.
        while not self._history_queue.empty():
            self._history_queue.get_nowait()
            self._history_queue.task_done()
            self.history_dropped_samples += 1
        async with self._clients_lock:
            clients = list(self.clients)
        for client in clients:
            await self.unregister_client(client)
            with suppress(Exception):
                await asyncio.wait_for(client.close(code=1001), timeout=self.send_timeout)

    async def register_client(self, client: WebSocket) -> bool:
        async with self._clients_lock:
            if client in self.clients or len(self.clients) >= self.max_clients:
                return False
            self.clients.add(client)
            self._client_queues[client] = asyncio.Queue(maxsize=self.outbound_queue_size)
            return True

    async def activate_client(self, client: WebSocket) -> None:
        async with self._clients_lock:
            if client not in self.clients or client in self._sender_tasks:
                return
            self._sender_tasks[client] = asyncio.create_task(
                self._sender_loop(client), name="ghostwatch-websocket-sender"
            )

    async def unregister_client(self, client: WebSocket) -> None:
        async with self._clients_lock:
            self.clients.discard(client)
            self._client_queues.pop(client, None)
            task = self._sender_tasks.pop(client, None)
        if task and task is not asyncio.current_task():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    async def _sender_loop(self, client: WebSocket) -> None:
        try:
            while True:
                queue = self._client_queues.get(client)
                if queue is None:
                    return
                snapshot = await queue.get()
                if not await self.send_snapshot(client, snapshot):
                    with suppress(Exception):
                        await asyncio.wait_for(
                            client.close(code=1011, reason="Telemetry send timed out"),
                            timeout=self.send_timeout,
                        )
                    return
        finally:
            async with self._clients_lock:
                self.clients.discard(client)
                self._client_queues.pop(client, None)
                self._sender_tasks.pop(client, None)

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.settings.refresh_interval
                )
            except TimeoutError:
                try:
                    await self.collect_once()
                except Exception:
                    LOGGER.exception("Telemetry collection failed")

    async def _windows_event_loop(self) -> None:
        while True:
            if self.windows_event_settings.enabled:
                try:
                    await self.collect_windows_events_once()
                except Exception:
                    LOGGER.exception("Windows event collection failed")
            await asyncio.sleep(self.windows_event_settings.poll_interval_seconds)

    async def collect_windows_events_once(self) -> list[WindowsEventRecord]:
        batch = await asyncio.to_thread(
            self.windows_event_collector.collect,
            dict(self._windows_cursors),
            self.windows_event_settings,
        )
        self.windows_channel_status = batch.channels
        candidates: list[WindowsEventRecord] = []
        for event in batch.events:
            if event.id in self._windows_seen:
                continue
            self._windows_seen[event.id] = None
            self._windows_seen.move_to_end(event.id)
            while len(self._windows_seen) > 5000:
                self._windows_seen.popitem(last=False)
            candidates.append(event)
            self.live_windows_events.appendleft(event)

        inserted_ids = {event.id for event in candidates}
        if self.storage.persistence_available:
            try:
                next_cursors = {
                    status.channel: status.cursor
                    for status in batch.channels
                    if status.status == "healthy"
                }
                inserted_ids = set(
                    await asyncio.to_thread(
                        self.database.add_windows_event_batch,
                        batch.events,
                        next_cursors,
                        self.windows_event_settings.retention_hours,
                    )
                )
                self._windows_cursors.update(next_cursors)
                self._mark_storage_success()
            except Exception as exc:
                self._mark_storage_failure(exc)

        for event in candidates:
            if event.id in inserted_ids:
                await self._correlate_windows_event(event)
        return candidates

    async def _correlate_windows_event(self, event: WindowsEventRecord) -> None:
        if event.category not in {"defender_detection", "service_failure"}:
            return
        defender = event.category == "defender_detection"
        alert = Alert(
            id=str(uuid5(NAMESPACE_URL, f"ghostwatch:{event.id}")),
            timestamp=event.timestamp,
            severity="CRITICAL" if defender else "WARNING",
            source="windows_event",
            metric=event.category,
            explanation=(
                "Windows Defender recorded a detection event. Review Defender history "
                "and the normalized event evidence."
                if defender
                else "Windows recorded a service failure. Verify the affected service "
                "and surrounding System events."
            ),
            rule_id="windows_defender_detection" if defender else "windows_service_failure",
            evidence={
                "windows_event_id": event.id,
                "provider": event.provider,
                "event_id": event.event_id,
                "service_name": event.service_name,
            },
        )
        self._remember_live_alert(alert)
        correlated_event = self._alert_event(alert)
        self._remember_event(correlated_event)
        if self.settings.event_logging_enabled:
            await self._persist_alert(alert)
            await self._persist_event(correlated_event)

    async def _file_integrity_loop(self) -> None:
        while True:
            if self.file_integrity_settings.enabled:
                try:
                    await self.scan_file_integrity_once()
                except Exception:
                    LOGGER.exception("File integrity collection failed")
            await asyncio.sleep(self.file_integrity_settings.scan_interval_seconds)

    async def scan_file_integrity_once(self) -> list[FileIntegrityEvent]:
        if not self.storage.persistence_available:
            return []
        try:
            folders = await asyncio.to_thread(self.database.list_monitored_folders)
        except Exception as exc:
            self._mark_storage_failure(exc)
            return []
        accepted: list[FileIntegrityEvent] = []
        for folder in folders:
            if not folder.enabled:
                continue
            try:
                baseline = await asyncio.to_thread(
                    self.database.load_file_baseline, folder.id
                )
                result = await self._scan_folder(folder, baseline)
                inserted = set(
                    await asyncio.to_thread(
                        self.database.save_file_scan,
                        folder.id,
                        result,
                        self.file_integrity_settings.event_retention,
                    )
                )
            except Exception as exc:
                self._mark_storage_failure(exc)
                return accepted
            for event in result.events:
                if event.id not in inserted or event.id in self._file_integrity_seen:
                    continue
                self._file_integrity_seen[event.id] = None
                while len(self._file_integrity_seen) > 5000:
                    self._file_integrity_seen.popitem(last=False)
                self.live_file_integrity_events.appendleft(event)
                accepted.append(event)
                await self._correlate_file_integrity_event(event)
        if self.storage.persistence_available:
            self._mark_storage_success()
        return accepted

    async def _scan_folder(
        self, folder: MonitoredFolder, baseline: dict[str, FileBaselineEntry]
    ) -> FileScanResult:
        try:
            return await asyncio.to_thread(
                self.file_integrity_collector.scan,
                folder,
                baseline,
                self.file_integrity_settings,
            )
        except Exception as exc:
            # A collector defect is not a storage outage and must not blind other folders.
            LOGGER.warning("File integrity scan failed (%s)", type(exc).__name__)
            return FileScanResult(
                entries=list(baseline.values()),
                status="unavailable",
                status_detail=f"Folder scan failed ({type(exc).__name__})",
            )

    async def _correlate_file_integrity_event(
        self, event: FileIntegrityEvent
    ) -> None:
        severity = "INFO" if event.change_type == "created" else "WARNING"
        alert = Alert(
            id=str(uuid5(NAMESPACE_URL, f"ghostwatch:file-integrity:{event.id}")),
            timestamp=event.timestamp,
            severity=severity,
            source="file_integrity",
            metric=event.change_type,
            explanation=(
                f"A monitored file was {event.change_type}. Confirm whether this "
                "change was expected."
            ),
            rule_id=f"file_integrity_{event.change_type}",
            evidence={
                "folder_id": event.folder_id,
                "relative_path": event.relative_path,
                "previous_sha256": event.previous_sha256,
                "current_sha256": event.current_sha256,
            },
        )
        self._remember_live_alert(alert)
        correlated_event = self._alert_event(alert)
        self._remember_event(correlated_event)
        if self.settings.event_logging_enabled:
            await self._persist_alert(alert)
            await self._persist_event(correlated_event)

    async def _history_writer(self) -> None:
        while True:
            batch = await self._history_queue.get()
            try:
                if (
                    self.history_settings.enabled
                    and self.storage.persistence_available
                    and not self._persisted_state_missing
                ):
                    generation = self._history_generation
                    await asyncio.to_thread(
                        self.database.add_history_batch,
                        batch.sample,
                        batch.processes,
                        batch.connection_summary,
                        self.history_settings.retention_hours,
                        self.history_settings.max_samples,
                    )
                    self._history_writes += 1
                    if (
                        not self.history_settings.enabled
                        or generation != self._history_generation
                    ):
                        continue
                    await asyncio.to_thread(
                        self.database.save_baseline_state,
                        self.baseline_engine.export_state(),
                    )
                    if (
                        self._history_writes % 300 == 0
                        and self.history_settings.enabled
                        and generation == self._history_generation
                    ):
                        await asyncio.to_thread(
                            self.database.compact_history,
                            self.history_settings.compact_after_hours,
                            self.history_settings.retention_hours,
                        )
                elif self.history_settings.enabled:
                    self.history_dropped_samples += 1
            except Exception as exc:
                self.history_dropped_samples += 1
                self._mark_storage_failure(exc)
            finally:
                self._history_queue.task_done()

    def _discard_pending_history(self) -> None:
        self._history_generation += 1
        while not self._history_queue.empty():
            self._history_queue.get_nowait()
            self._history_queue.task_done()

    def _queue_history_sample(self, snapshot: TelemetrySnapshot) -> None:
        if not self.history_settings.enabled:
            return
        unavailable = sorted(
            set(snapshot.system.unavailable_metrics)
            | set(snapshot.network.unavailable_metrics)
        )
        if snapshot.network.connection_collection_status == "unavailable":
            status = "unavailable"
        elif unavailable:
            status = "degraded"
        else:
            status = "healthy"
        sample = HistoricalSample(
            timestamp=snapshot.system.timestamp,
            sequence=snapshot.sequence,
            cpu_percent=(
                None
                if "cpu_percent" in snapshot.system.unavailable_metrics
                else snapshot.system.cpu_percent
            ),
            memory_percent=(
                None
                if "memory_percent" in snapshot.system.unavailable_metrics
                else snapshot.system.memory_percent
            ),
            disk_percent=snapshot.system.disk_percent,
            upload_mbps=(
                None
                if "upload_mbps" in snapshot.network.unavailable_metrics
                else snapshot.network.upload_mbps
            ),
            download_mbps=(
                None
                if "download_mbps" in snapshot.network.unavailable_metrics
                else snapshot.network.download_mbps
            ),
            active_connections=(
                None
                if "active_connections" in snapshot.network.unavailable_metrics
                else snapshot.network.active_connections
            ),
            established_connections=(
                None
                if "established_connections" in snapshot.network.unavailable_metrics
                else snapshot.network.established_connections
            ),
            process_count=(
                None
                if "process_count" in snapshot.system.unavailable_metrics
                else snapshot.system.process_count
            ),
            collector_status=status,
            unavailable_metrics=unavailable,
        )
        processes = [
            HistoricalProcessObservation(
                timestamp=snapshot.system.timestamp,
                sequence=snapshot.sequence,
                pid=process.pid,
                create_time=process.create_time,
                name=process.name,
                cpu_percent=(
                    None
                    if "cpu_percent" in process.unavailable_fields
                    else process.cpu_percent
                ),
                memory_percent=(
                    None
                    if "memory_percent" in process.unavailable_fields
                    else process.memory_percent
                ),
                connection_count=process.connection_count,
                status=process.status,
                access_status=process.access_status,
            )
            for process in sorted(
                snapshot.processes,
                key=lambda item: (
                    item.cpu_percent,
                    item.memory_percent,
                    item.connection_count,
                ),
                reverse=True,
            )[: self.history_settings.process_sample_limit]
        ]
        connection_summary = HistoricalConnectionSummary(
            timestamp=snapshot.system.timestamp,
            sequence=snapshot.sequence,
            active_connections=sample.active_connections,
            established_connections=sample.established_connections,
            unique_remote_destinations=(
                None
                if "unique_remote_destinations" in snapshot.network.unavailable_metrics
                else snapshot.network.unique_remote_destinations
            ),
            new_connections=(
                None
                if snapshot.network.lifecycle_confidence == "unavailable"
                else snapshot.network.new_connections
            ),
            disappeared_connections=(
                None
                if snapshot.network.lifecycle_confidence == "unavailable"
                else snapshot.network.disappeared_connections
            ),
            collection_status=snapshot.network.connection_collection_status,
            lifecycle_confidence=snapshot.network.lifecycle_confidence,
            top_processes=snapshot.connection_rankings[:100],
        )
        item = HistoryWriteBatch(sample, processes, connection_summary)
        if self._history_queue.full():
            with suppress(asyncio.QueueEmpty):
                self._history_queue.get_nowait()
                self._history_queue.task_done()
                self.history_dropped_samples += 1
        self._history_queue.put_nowait(item)

    @staticmethod
    def _unavailable_system(exc: Exception) -> SystemSample:
        return SystemSample(
            timestamp=datetime.now(UTC),
            cpu_percent=0,
            memory_percent=0,
            memory_used=0,
            memory_total=0,
            process_count=0,
            uptime_seconds=0,
            collection_error=f"System telemetry unavailable ({type(exc).__name__})",
            unavailable_metrics=[
                "cpu_percent",
                "memory_percent",
                "memory_used",
                "memory_total",
                "process_count",
                "uptime_seconds",
                "disk_percent",
            ],
        )

    @staticmethod
    def _unavailable_network(exc: Exception) -> NetworkSample:
        return NetworkSample(
            bytes_sent_per_second=0,
            bytes_recv_per_second=0,
            upload_mbps=0,
            download_mbps=0,
            active_connections=0,
            established_connections=0,
            collection_error=f"Network telemetry unavailable ({type(exc).__name__})",
            unavailable_metrics=[
                "bytes_sent_per_second",
                "bytes_recv_per_second",
                "upload_mbps",
                "download_mbps",
                "active_connections",
                "established_connections",
                "unique_remote_destinations",
            ],
        )

    async def collect_once(self) -> TelemetrySnapshot:
        started = time.perf_counter()
        async with self._collection_lock:
            await self._maybe_recover_storage()
            try:
                system = await asyncio.to_thread(self.system_collector.collect)
            except Exception as exc:
                LOGGER.warning("System telemetry collection failed (%s)", type(exc).__name__)
                system = self._unavailable_system(exc)
            try:
                network, connections = await asyncio.to_thread(
                    self.network_collector.collect
                )
                connection_counts = getattr(
                    self.network_collector, "connection_counts_by_pid", {}
                )
                remote_endpoints = getattr(
                    self.network_collector, "remote_endpoints_by_pid", {}
                )
            except Exception as exc:
                LOGGER.warning("Network telemetry collection failed (%s)", type(exc).__name__)
                network = self._unavailable_network(exc)
                connections = []
                connection_counts = {}
                remote_endpoints = {}
            try:
                processes = await asyncio.to_thread(
                    self.process_collector.collect,
                    connections,
                    connection_counts,
                    remote_endpoints,
                )
                process_status = str(
                    getattr(self.process_collector, "collection_status", "healthy")
                )
                process_detail = str(
                    getattr(self.process_collector, "collection_detail", "")
                )
                enumerated_process_count = int(
                    getattr(self.process_collector, "enumerated_count", len(processes))
                )
                if (
                    process_status == "healthy"
                    and system.process_count > enumerated_process_count
                ):
                    process_status = "partial"
                    process_detail = (
                        "Process detail visibility is partial "
                        f"({enumerated_process_count} enumerated of "
                        f"{system.process_count} OS processes)"
                    )
                if process_status == "unavailable":
                    errors = [
                        error
                        for error in (system.collection_error, process_detail)
                        if error
                    ]
                    system = system.model_copy(
                        update={
                            "collection_error": "; ".join(errors),
                            "unavailable_metrics": list(
                                dict.fromkeys(
                                    [*system.unavailable_metrics, "process_details"]
                                )
                            ),
                        }
                    )
            except Exception as exc:
                LOGGER.warning("Process telemetry collection failed (%s)", type(exc).__name__)
                processes = []
                process_error = f"Process telemetry unavailable ({type(exc).__name__})"
                errors = [error for error in (system.collection_error, process_error) if error]
                system = system.model_copy(
                    update={
                        "collection_error": "; ".join(errors),
                        "unavailable_metrics": [
                            *system.unavailable_metrics,
                            "process_details",
                        ],
                    }
                )
                process_status = "unavailable"
                process_detail = process_error

            baselines = self.baseline_engine.observe(system, network)
            new_alerts = self.detection_engine.evaluate(
                system, network, self.settings, self.detection_settings
            )
            for alert in new_alerts:
                self._remember_live_alert(alert)
                try:
                    event = self._alert_event(alert)
                except ValidationError as exc:
                    # One alert must not cost the rest of this telemetry cycle.
                    LOGGER.warning("Alert event could not be created (%s)", type(exc).__name__)
                    event = None
                if event is not None:
                    self._remember_event(event)
                if self.settings.event_logging_enabled:
                    await self._persist_alert(alert)
                    if event is not None:
                        await self._persist_event(event)

            alerts = await self.list_alerts(50)
            self._sequence += 1
            duration_ms = (time.perf_counter() - started) * 1000
            snapshot = TelemetrySnapshot(
                sequence=self._sequence,
                system=system,
                network=network,
                connections=connections,
                processes=processes,
                alerts=alerts,
                connection_rankings=list(
                    getattr(self.network_collector, "connection_rankings", [])
                )[:100],
                recently_closed_connections=list(
                    getattr(self.network_collector, "recently_closed", [])
                )[:100],
                baselines=baselines,
                storage=self.storage.model_copy(update=self._queue_health()),
                collection_duration_ms=round(duration_ms, 2),
                process_collection_status=process_status,  # type: ignore[arg-type]
                process_collection_detail=process_detail,
                processes_enumerated=int(
                    getattr(self.process_collector, "enumerated_count", len(processes))
                ),
                processes_displayed=len(processes),
                process_records_skipped=int(
                    getattr(self.process_collector, "skipped_count", 0)
                ),
                idle_processes_excluded=int(
                    getattr(self.process_collector, "idle_excluded_count", 0)
                ),
            )
            self.latest = snapshot
            self._queue_history_sample(snapshot)
            self.history.append(
                {
                    "timestamp": system.timestamp.isoformat(),
                    "cpu_percent": system.cpu_percent,
                    "memory_percent": system.memory_percent,
                    "upload_mbps": network.upload_mbps,
                    "download_mbps": network.download_mbps,
                    "connections": network.active_connections,
                    "process_count": system.process_count,
                    "unavailable_metrics": sorted(
                        set(system.unavailable_metrics) | set(network.unavailable_metrics)
                    ),
                }
            )
        await self.broadcast(snapshot)
        return snapshot

    @staticmethod
    def _merge_grouped_alert(existing: Alert, alert: Alert) -> Alert:
        reopened = alert.condition_active is True and existing.condition_active is False
        return alert.model_copy(
            update={
                "first_occurrence": (
                    existing.first_occurrence or alert.first_occurrence or existing.timestamp
                ),
                "last_occurrence": alert.last_occurrence or existing.last_occurrence,
                "occurrence_count": existing.occurrence_count + alert.occurrence_count,
                "last_recovery": alert.last_recovery or existing.last_recovery,
                "acknowledged": False if reopened else existing.acknowledged,
                "dismissed": False if reopened else existing.dismissed,
                "persistence_pending": (
                    existing.persistence_pending or alert.persistence_pending
                ),
            }
        )

    def _remember_live_alert(self, alert: Alert, *, authoritative: bool = False) -> Alert:
        for index, existing in enumerate(self.live_alerts):
            if existing.id == alert.id:
                if (
                    not authoritative
                    and alert.rule_id == "collection_failure"
                    and alert.condition_active is True
                    and existing.condition_active is False
                ):
                    self._queue_alert_update(
                        alert.id, {"acknowledged": False, "dismissed": False}
                    )
                remembered = (
                    alert
                    if authoritative or alert.rule_id != "collection_failure"
                    else self._merge_grouped_alert(existing, alert)
                )
                self.live_alerts[index] = remembered
                return remembered
        self.live_alerts.appendleft(alert)
        return alert

    def _queue_pending_alert(self, alert: Alert) -> None:
        existing = self._pending_alerts.get(alert.id)
        if existing is not None and alert.rule_id == "collection_failure":
            alert = self._merge_grouped_alert(existing, alert)
        self._queue_pending(self._pending_alerts, alert.id, alert)

    def _remember_event(self, event: EventRecord) -> None:
        if any(existing.id == event.id for existing in self.live_events):
            return
        self.live_events.appendleft(event)

    @staticmethod
    def _alert_event(alert: Alert) -> EventRecord:
        severity = {
            "INFO": "LOW",
            "WARNING": "HIGH",
            "CRITICAL": "CRITICAL",
        }[alert.severity]
        return EventRecord(
            id=f"event-alert-{alert.id}",
            timestamp=alert.timestamp,
            event_type="alert",
            severity=severity,  # type: ignore[arg-type]
            title=alert.metric,
            summary=alert.explanation,
            related_id=bounded_identifier(alert.id, MAX_ALERT_LABEL),
            evidence=alert.evidence,
        )

    def _mark_storage_failure(self, exc: Exception) -> None:
        error_type = type(exc).__name__
        self.storage = StorageHealth(
            status="degraded",
            persistence_available=False,
            **self._queue_health(),
            last_success=self.storage.last_success,
            last_error_type=error_type,
            schema_version=self.storage.schema_version,
        )
        self._next_storage_retry = time.monotonic() + self._storage_retry_seconds
        LOGGER.warning("Persistent storage is degraded (%s)", error_type)
        alert = Alert(
            id=str(uuid5(NAMESPACE_URL, "ghostwatch:storage-degraded")),
            timestamp=datetime.now(UTC),
            severity="WARNING",
            source="Storage",
            metric="Persistent storage unavailable",
            explanation=(
                "Telemetry remains live in bounded memory. Persistence will be retried; "
                "the error category is recorded without database contents."
            ),
            rule_id="storage_failure",
            evidence={"error_type": error_type},
            persistence_pending=True,
        )
        self._remember_live_alert(alert)

    def _mark_storage_success(self, version: int | None = None) -> None:
        recovered = not self.storage.persistence_available
        self.storage = StorageHealth(
            status="healthy",
            persistence_available=True,
            **self._queue_health(),
            last_success=datetime.now(UTC),
            last_error_type=None,
            schema_version=version or self.storage.schema_version,
        )
        if recovered:
            event = EventRecord(
                id=str(uuid4()),
                timestamp=datetime.now(UTC),
                event_type="storage_recovery",
                severity="LOW",
                title="Persistent storage recovered",
                summary="Queued bounded alert and event writes are being reconciled.",
            )
            self._remember_event(event)
            # A full queue keeps its telemetry, so this notice stays in live memory.
            if self.settings.event_logging_enabled and len(self._pending_events) < MAX_PENDING_WRITES:
                self._queue_pending(self._pending_events, event.id, event)

    def _queue_health(self) -> dict[str, int]:
        return {
            "pending_writes": (
                len(self._pending_alerts) + len(self._pending_events)
                + len(self._pending_alert_updates)
            ),
            "dropped_alert_writes": self.dropped_alert_writes,
            "dropped_event_writes": self.dropped_event_writes,
        }

    def _queue_pending(
        self, queue: OrderedDict[str, Any], key: str, value: Any
    ) -> None:
        queue[key] = value
        queue.move_to_end(key)
        while len(queue) > MAX_PENDING_WRITES:
            self._evict_oldest(queue)
        self.storage = self.storage.model_copy(update=self._queue_health())

    def _evict_oldest(self, queue: OrderedDict[str, Any]) -> None:
        alerts = queue is self._pending_alerts or queue is self._pending_alert_updates
        writing = (
            self._writing_alert_update if queue is self._pending_alert_updates
            else self._writing_alert if alerts else self._writing_event
        )
        # A record being written may still be stored, so it is not lost yet.
        key = next(item for item in queue if item != writing)
        del queue[key]
        if alerts:
            self.dropped_alert_writes += 1
            self._clear_pending_flag(key)
            count = self.dropped_alert_writes
        else:
            self.dropped_event_writes += 1
            count = self.dropped_event_writes
        if count == 1 or count % DROP_LOG_INTERVAL == 0:
            kind = "alert" if alerts else "event"
            LOGGER.warning("Pending %s queue is full; %d writes dropped", kind, count)

    def _live_alert(self, alert_id: str) -> Alert | None:
        return next((item for item in self.live_alerts if item.id == alert_id), None)

    def _queue_alert_update(self, alert_id: str, changes: dict[str, bool]) -> None:
        if not changes or not self.settings.event_logging_enabled:
            return
        existing = self._pending_alert_updates.get(alert_id, AlertUpdate())
        patch = AlertUpdate(**(existing.model_dump(exclude_none=True) | changes))
        self._queue_pending(self._pending_alert_updates, alert_id, patch)
        current = self._live_alert(alert_id)
        if current is not None:
            self._remember_live_alert(
                current.model_copy(update={"persistence_pending": True}), authoritative=True
            )

    def _accept_stored_alert(self, stored: Alert, before: Alert | None) -> None:
        current = self._live_alert(stored.id)
        pending = self._pending_alerts.get(stored.id)
        result = stored
        if pending is not None:
            result = (
                self._merge_grouped_alert(stored, pending)
                if stored.rule_id == "collection_failure" else pending
            )
        if current is not None:
            changes: dict[str, Any] = {"incident_id": current.incident_id or stored.incident_id}
            if current is not before:
                changes.update(acknowledged=current.acknowledged, dismissed=current.dismissed)
            result = result.model_copy(update=changes)
        patch = self._pending_alert_updates.get(stored.id)
        if patch is not None:
            result = result.model_copy(update=patch.model_dump(exclude_none=True))
        self._remember_live_alert(result, authoritative=True)
        self._clear_pending_flag(stored.id)

    def _queue_with_operator_state(self, alert: Alert) -> None:
        # The live copy holds any acknowledge or dismiss that arrived during a write.
        current = self._live_alert(alert.id) or alert
        if not self.settings.event_logging_enabled:
            self._remember_live_alert(
                current.model_copy(update={"persistence_pending": False}),
                authoritative=True,
            )
            return
        pending = alert.model_copy(
            update={
                "acknowledged": current.acknowledged,
                "dismissed": current.dismissed,
                "persistence_pending": True,
            }
        )
        self._remember_live_alert(
            current.model_copy(update={"persistence_pending": True}), authoritative=True
        )
        self._queue_pending_alert(pending)

    async def _persist_alert(self, alert: Alert) -> bool:
        if not self.settings.event_logging_enabled:
            self._clear_pending_flag(alert.id)
            return False
        generation = self._logging_generation
        if (
            not self.storage.persistence_available
            or self._persisted_state_missing
            or self._alert_persistence_lock.locked()
            or alert.id in self._pending_alerts
        ):
            self._queue_with_operator_state(alert)
            return False
        async with self._alert_persistence_lock:
            return await self._write_alert(alert, generation)

    async def _write_alert(self, alert: Alert, generation: int) -> bool:
        before = self._live_alert(alert.id)
        try:
            stored = await asyncio.to_thread(
                self.database.add_alert, alert, self.settings.alert_retention
            )
        except InvalidRecordError as exc:
            self._reject_write(exc)
            return False
        except Exception as exc:
            if generation == self._logging_generation:
                newer = self._pending_alerts.get(alert.id)
                if newer is None:
                    self._queue_with_operator_state(alert)
                else:
                    # The attempted observation predates anything queued while
                    # it was in flight. Keep recovery/reopening in that order.
                    pending = (
                        self._merge_grouped_alert(alert, newer)
                        if alert.rule_id == "collection_failure" else newer
                    )
                    self._queue_pending(self._pending_alerts, alert.id, pending)
            self._mark_storage_failure(exc)
            return False
        self._accept_stored_alert(stored, before)
        self._mark_storage_success()
        return True

    async def _persist_event(self, event: EventRecord) -> bool:
        if not self.settings.event_logging_enabled:
            return False
        generation = self._logging_generation
        if not self.storage.persistence_available or self._persisted_state_missing:
            self._queue_pending(self._pending_events, event.id, event)
            return False
        try:
            await asyncio.to_thread(self.database.add_event, event)
        except InvalidRecordError as exc:
            self._reject_write(exc)
            return False
        except Exception as exc:
            if (
                self.settings.event_logging_enabled
                and generation == self._logging_generation
            ):
                self._queue_pending(self._pending_events, event.id, event)
            self._mark_storage_failure(exc)
            return False
        self._mark_storage_success()
        return True

    def _reject_write(self, exc: Exception) -> None:
        self.rejected_writes += 1
        LOGGER.warning("A record could not be persisted (%s)", type(exc).__name__)

    def _reject_queued_alert(self, alert_id: str, exc: Exception) -> None:
        self._pending_alerts.pop(alert_id, None)
        self._reject_write(exc)
        self._clear_pending_flag(alert_id)

    def _clear_pending_flag(self, alert_id: str) -> None:
        for index, alert in enumerate(self.live_alerts):
            if alert.id == alert_id:
                self.live_alerts[index] = alert.model_copy(update={
                    "persistence_pending": (
                        alert_id in self._pending_alerts or alert_id in self._pending_alert_updates
                    )
                })

    async def _maybe_recover_storage(self) -> None:
        # A direct write can restore health without passing through recovery.
        recovering = not self.storage.persistence_available
        if recovering:
            if time.monotonic() < self._next_storage_retry:
                return
            self.storage = self.storage.model_copy(update={"status": "recovering"})
        version = None
        try:
            if recovering:
                version = await asyncio.to_thread(self.database.initialize)
            await self._load_missed_state()
        except Exception as exc:
            self._mark_storage_failure(exc)
            return
        if recovering:
            self._mark_storage_success(version)
        if self._pending_alerts or self._pending_events or self._pending_alert_updates:
            await self._flush_pending()

    async def _load_missed_state(self) -> None:
        if not self._persisted_state_missing:
            return
        # Startup could not read the database, so saved settings such as a history
        # or logging opt-out are applied here, before any write can use the defaults.
        await self._load_persisted_state()
        self._persisted_state_missing = False
        if not self.settings.event_logging_enabled:
            self._discard_pending_writes()

    def _discard_pending_writes(self) -> None:
        self._pending_alerts.clear()
        self._pending_events.clear()
        self._pending_alert_updates.clear()
        for index, alert in enumerate(self.live_alerts):
            if alert.persistence_pending:
                self.live_alerts[index] = alert.model_copy(
                    update={"persistence_pending": False}
                )
        self.storage = self.storage.model_copy(update=self._queue_health())

    async def _flush_pending(self) -> None:
        if self._alert_persistence_lock.locked():
            return
        async with self._alert_persistence_lock:
            await self._flush_pending_locked()

    async def _flush_pending_locked(self) -> None:
        if not self.settings.event_logging_enabled:
            self._discard_pending_writes()
            return
        if not self.storage.persistence_available or self._persisted_state_missing:
            return
        for alert_id in list(self._pending_alerts):
            if not self.settings.event_logging_enabled:
                self._discard_pending_writes()
                return
            # Read each entry now, because an update can replace it during a write.
            alert = self._pending_alerts.get(alert_id)
            if alert is None:
                continue
            self._writing_alert = alert_id
            before = self._live_alert(alert_id)
            generation = self._logging_generation
            try:
                clean = alert.model_copy(update={"persistence_pending": False})
                stored = await asyncio.to_thread(
                    self.database.add_alert, clean, self.settings.alert_retention
                )
            except InvalidRecordError as exc:
                # A newer version is judged on its own by the next pass.
                if self._pending_alerts.get(alert_id) is alert:
                    self._reject_queued_alert(alert_id, exc)
                continue
            except Exception as exc:
                self._mark_storage_failure(exc)
                return
            finally:
                self._writing_alert = None
            if self._pending_alerts.get(alert_id) is not alert:
                # A new observation arrived while this delta was being written.
                # Retain only the unwritten count, never replay the committed delta.
                newer = self._pending_alerts.get(alert_id)
                if (
                    newer is not None and alert.rule_id == "collection_failure"
                    and generation == self._logging_generation
                ):
                    self._pending_alerts[alert_id] = newer.model_copy(update={
                        "occurrence_count": newer.occurrence_count - alert.occurrence_count
                    })
                self._accept_stored_alert(stored, before)
                continue
            self._pending_alerts.pop(alert_id, None)
            self._accept_stored_alert(stored, before)
        for alert_id in list(self._pending_alert_updates):
            if not self.settings.event_logging_enabled:
                self._discard_pending_writes()
                return
            # Apply state only after all observations for this alert are written.
            if alert_id in self._pending_alerts:
                continue
            patch = self._pending_alert_updates.get(alert_id)
            if patch is not None and not await self._write_alert_update(alert_id, patch):
                return
        for event_id in list(self._pending_events):
            if not self.settings.event_logging_enabled:
                self._discard_pending_writes()
                return
            event = self._pending_events.get(event_id)
            if event is None:
                continue
            self._writing_event = event_id
            try:
                await asyncio.to_thread(self.database.add_event, event)
            except InvalidRecordError as exc:
                self._pending_events.pop(event_id, None)
                self._reject_write(exc)
                continue
            except Exception as exc:
                self._mark_storage_failure(exc)
                return
            finally:
                self._writing_event = None
            self._pending_events.pop(event_id, None)
        self._mark_storage_success()

    async def list_alerts(
        self, limit: int = 100, include_dismissed: bool = False
    ) -> list[Alert]:
        persisted: list[Alert] = []
        if self.storage.persistence_available:
            try:
                persisted = await asyncio.to_thread(
                    self.database.list_alerts, limit, include_dismissed
                )
                self._mark_storage_success()
            except Exception as exc:
                self._mark_storage_failure(exc)
        merged = {alert.id: alert for alert in persisted}
        for alert in self.live_alerts:
            merged[alert.id] = alert
        alerts = sorted(merged.values(), key=lambda alert: alert.timestamp, reverse=True)
        if not include_dismissed:
            alerts = [alert for alert in alerts if not alert.dismissed]
        return alerts[:limit]

    async def _write_alert_update(self, alert_id: str, patch: AlertUpdate) -> bool:
        before = self._live_alert(alert_id)
        self._writing_alert_update = alert_id
        try:
            persisted = await asyncio.to_thread(self.database.update_alert, alert_id, patch)
        except Exception as exc:
            # The patch was queued before starting the write. An opt-out may
            # already have discarded it; never put that discarded work back.
            self._mark_storage_failure(exc)
            return False
        finally:
            self._writing_alert_update = None
        if self._pending_alert_updates.get(alert_id) is patch:
            self._pending_alert_updates.pop(alert_id, None)
        if persisted is not None:
            self._accept_stored_alert(persisted, before)
        else:
            self._clear_pending_flag(alert_id)
        self._mark_storage_success()
        return True

    async def update_alert(self, alert_id: str, update: AlertUpdate) -> Alert | None:
        generation = self._logging_generation
        changes = update.model_dump(exclude_none=True)
        current = self._live_alert(alert_id)
        if current is None:
            try:
                persisted = await asyncio.to_thread(self.database.get_alert, alert_id)
            except Exception as exc:
                self._mark_storage_failure(exc)
                raise StorageUnavailableError("Alert lookup is unavailable") from exc
            # Another action may have populated live state during the lookup.
            current = self._live_alert(alert_id) or persisted
            if current is None:
                return None
            pending = self._pending_alert_updates.get(alert_id)
            if pending is not None:
                current = current.model_copy(update=pending.model_dump(exclude_none=True))
            self._remember_live_alert(current, authoritative=True)
        if not changes:
            return current
        memory_result: Alert | None = None
        for index, alert in enumerate(self.live_alerts):
            if alert.id == alert_id:
                memory_result = alert.model_copy(update=changes)
                self.live_alerts[index] = memory_result
                break
        if generation == self._logging_generation:
            self._queue_alert_update(alert_id, changes)
        patch = self._pending_alert_updates.get(alert_id)
        if (
            patch is not None and self.storage.persistence_available
            and not self._persisted_state_missing
            and not self._alert_persistence_lock.locked()
            and alert_id not in self._pending_alerts
        ):
            async with self._alert_persistence_lock:
                await self._write_alert_update(alert_id, patch)
        if memory_result:
            event = EventRecord(
                id=str(uuid4()),
                timestamp=datetime.now(UTC),
                event_type="alert_update",
                severity="LOW",
                title="Alert state changed",
                summary="An alert was acknowledged or dismissed by the local operator.",
                related_id=bounded_identifier(alert_id, MAX_ALERT_LABEL),
                evidence=changes,
            )
            self._remember_event(event)
            if (
                self.settings.event_logging_enabled
                and generation == self._logging_generation
            ):
                await self._persist_event(event)
        return self._live_alert(alert_id) or memory_result

    async def send_snapshot(
        self, client: WebSocket, snapshot: TelemetrySnapshot | None = None
    ) -> bool:
        selected = snapshot or self.latest
        if selected is None:
            return True
        # A frame that cannot be encoded says nothing about this client's socket.
        payload = self.encode_snapshot(selected) or SNAPSHOT_DEGRADED_FRAME
        try:
            await asyncio.wait_for(client.send_text(payload), timeout=self.send_timeout)
            return True
        except Exception:
            return False

    def encode_snapshot(self, snapshot: TelemetrySnapshot) -> str | None:
        """Return the shared JSON for a snapshot, or None if it cannot be served safely."""
        if self._encoded_snapshot is not None and self._encoded_snapshot[0] is snapshot:
            return self._encoded_snapshot[1]
        payload: str | None = None
        failure: str | None = None
        try:
            payload = snapshot.model_dump_json()
            if len(payload.encode("utf-8")) > MAX_SNAPSHOT_BYTES:
                payload = None
                failure = "size limit exceeded"
        except Exception as exc:
            failure = type(exc).__name__
        if failure is not None and not self._snapshot_encoding_failed:
            LOGGER.warning("Telemetry snapshot could not be encoded (%s)", failure)
        elif failure is None and self._snapshot_encoding_failed:
            LOGGER.info("Telemetry snapshot encoding recovered")
        self._snapshot_encoding_failed = failure is not None
        self._encoded_snapshot = (snapshot, payload)
        return payload

    async def broadcast(self, snapshot: TelemetrySnapshot) -> None:
        async with self._clients_lock:
            clients = list(self.clients)
        overloaded: list[WebSocket] = []
        direct: list[WebSocket] = []
        for client in clients:
            queue = self._client_queues.get(client)
            if client not in self._sender_tasks:
                direct.append(client)
            elif queue is None or queue.full():
                overloaded.append(client)
            else:
                queue.put_nowait(snapshot)
        if direct:
            results = await asyncio.gather(
                *(self.send_snapshot(client, snapshot) for client in direct)
            )
            overloaded.extend(
                client for client, succeeded in zip(direct, results, strict=True) if not succeeded
            )
        for client in overloaded:
            await self.unregister_client(client)
            with suppress(Exception):
                await asyncio.wait_for(
                    client.close(code=1013, reason="Client could not keep up"),
                    timeout=self.send_timeout,
                )

    async def update_settings(self, settings: Settings) -> Settings:
        try:
            await asyncio.to_thread(self.database.save_settings, settings)
            # A saved opt-out takes effect even if the maintenance below fails.
            self._apply_saved_configuration("settings", settings)
            if not settings.event_logging_enabled:
                self._logging_generation += 1
                self._discard_pending_writes()
            await asyncio.to_thread(
                self.database.enforce_retention, settings.alert_retention
            )
        except Exception as exc:
            self._mark_storage_failure(exc)
            raise StorageUnavailableError(
                "Settings were not persisted; storage is unavailable"
            ) from exc
        self._mark_storage_success()
        await self.record_event(
            "settings_change",
            "LOW",
            "Monitoring settings updated",
            "Validated local monitoring settings were persisted.",
        )
        return settings

    async def update_detection_settings(
        self, settings: DetectionSettings
    ) -> DetectionSettings:
        try:
            await asyncio.to_thread(self.database.save_detection_settings, settings)
        except Exception as exc:
            self._mark_storage_failure(exc)
            raise StorageUnavailableError("Detection settings were not persisted") from exc
        self._apply_saved_configuration("detection_settings", settings)
        self._mark_storage_success()
        return settings

    async def update_ai_settings(self, settings: AISettings) -> AISettings:
        try:
            await asyncio.to_thread(self.database.save_ai_settings, settings)
        except Exception as exc:
            self._mark_storage_failure(exc)
            raise StorageUnavailableError("AI settings were not persisted") from exc
        self._apply_saved_configuration("ai_settings", settings)
        self._mark_storage_success()
        return settings

    async def update_history_settings(
        self, settings: HistorySettings
    ) -> HistorySettings:
        try:
            await asyncio.to_thread(
                self.database.save_history_settings, settings
            )
            # A saved opt-out takes effect even if the maintenance below fails.
            self._apply_saved_configuration("history_settings", settings)
            if not settings.enabled:
                self._discard_pending_history()
            await asyncio.to_thread(
                self.database.prune_telemetry,
                settings.retention_hours,
                settings.max_samples,
            )
            await asyncio.to_thread(
                self.database.compact_history,
                settings.compact_after_hours,
                settings.retention_hours,
            )
        except Exception as exc:
            self._mark_storage_failure(exc)
            raise StorageUnavailableError(
                "Historical telemetry settings were not persisted"
            ) from exc
        self._mark_storage_success()
        return settings

    async def update_windows_event_settings(
        self, settings: WindowsEventSettings
    ) -> WindowsEventSettings:
        try:
            await asyncio.to_thread(
                self.database.save_windows_event_settings, settings
            )
        except Exception as exc:
            self._mark_storage_failure(exc)
            raise StorageUnavailableError(
                "Windows event settings were not persisted"
            ) from exc
        self._apply_saved_configuration("windows_event_settings", settings)
        self._mark_storage_success()
        return settings

    async def query_windows_events(
        self,
        *,
        limit: int,
        channel: str | None,
        category: str | None,
        level: str | None,
        search: str | None,
        start: datetime | None,
        end: datetime | None,
    ) -> list[WindowsEventRecord]:
        if not self.storage.persistence_available:
            events = list(self.live_windows_events)
            if channel:
                events = [event for event in events if event.channel == channel]
            if category:
                events = [event for event in events if event.category == category]
            if level:
                events = [event for event in events if event.level == level]
            if search:
                query = search.casefold()
                events = [
                    event
                    for event in events
                    if query in f"{event.provider} {event.summary} {event.service_name or ''}".casefold()
                ]
            return events[:limit]
        try:
            result = await asyncio.to_thread(
                self.database.list_windows_events,
                limit=limit,
                channel=channel,
                category=category,
                level=level,
                search=search,
                start=start,
                end=end,
            )
        except Exception as exc:
            self._mark_storage_failure(exc)
            raise StorageUnavailableError("Windows events could not be read") from exc
        self._mark_storage_success()
        return result

    async def get_windows_event_summary(self) -> dict[str, object]:
        counts: dict[str, int] = {}
        if self.storage.persistence_available:
            try:
                counts = await asyncio.to_thread(self.database.windows_event_summary)
            except Exception as exc:
                self._mark_storage_failure(exc)
        if not counts:
            for event in self.live_windows_events:
                counts[event.category] = counts.get(event.category, 0) + 1
        return {
            "enabled": self.windows_event_settings.enabled,
            "counts": counts,
            "channels": [
                status.model_dump(mode="json") for status in self.windows_channel_status
            ],
        }

    async def update_file_integrity_settings(
        self, settings: FileIntegritySettings
    ) -> FileIntegritySettings:
        try:
            await asyncio.to_thread(
                self.database.save_file_integrity_settings, settings
            )
        except Exception as exc:
            self._mark_storage_failure(exc)
            raise StorageUnavailableError(
                "File integrity settings were not persisted"
            ) from exc
        self._apply_saved_configuration("file_integrity_settings", settings)
        self._mark_storage_success()
        return settings

    async def add_monitored_folder(self, path: str) -> MonitoredFolder:
        if not self.storage.persistence_available:
            raise StorageUnavailableError("File integrity storage is unavailable")
        try:
            resolved = await asyncio.to_thread(
                self.file_integrity_collector.validate_folder, path
            )
            existing = await asyncio.to_thread(self.database.list_monitored_folders)
            normalized = str(resolved).casefold()
            if any(folder.path.casefold() == normalized for folder in existing):
                raise ValueError("Folder is already monitored")
            now = datetime.now(UTC)
            pending = MonitoredFolder(
                id=str(uuid4()),
                path=str(resolved),
                created_at=now,
            )
            result = await asyncio.to_thread(
                self.file_integrity_collector.scan,
                pending,
                {},
                self.file_integrity_settings,
                emit_events=False,
            )
            if result.status == "unavailable":
                raise ValueError(result.status_detail)
            folder = pending.model_copy(
                update={
                    "baseline_created_at": now,
                    "status": result.status,
                    "status_detail": result.status_detail,
                }
            )
            await asyncio.to_thread(
                self.database.add_monitored_folder, folder, result.entries
            )
        except ValueError:
            raise
        except Exception as exc:
            self._mark_storage_failure(exc)
            raise StorageUnavailableError(
                "Monitored folder baseline could not be created"
            ) from exc
        self._mark_storage_success()
        return folder

    async def list_monitored_folders(self) -> list[MonitoredFolder]:
        if not self.storage.persistence_available:
            raise StorageUnavailableError("File integrity storage is unavailable")
        try:
            return await asyncio.to_thread(self.database.list_monitored_folders)
        except Exception as exc:
            self._mark_storage_failure(exc)
            raise StorageUnavailableError("Monitored folders could not be read") from exc

    async def set_monitored_folder_enabled(
        self, folder_id: str, enabled: bool
    ) -> MonitoredFolder | None:
        try:
            return await asyncio.to_thread(
                self.database.set_monitored_folder_enabled, folder_id, enabled
            )
        except Exception as exc:
            self._mark_storage_failure(exc)
            raise StorageUnavailableError("Monitored folder could not be updated") from exc

    async def remove_monitored_folder(self, folder_id: str) -> bool:
        try:
            return await asyncio.to_thread(
                self.database.delete_monitored_folder, folder_id
            )
        except Exception as exc:
            self._mark_storage_failure(exc)
            raise StorageUnavailableError("Monitored folder could not be removed") from exc

    async def list_file_integrity_events(
        self,
        *,
        limit: int,
        folder_id: str | None,
        change_type: str | None,
        search: str | None,
    ) -> list[FileIntegrityEvent]:
        if not self.storage.persistence_available:
            events = list(self.live_file_integrity_events)
            if folder_id:
                events = [event for event in events if event.folder_id == folder_id]
            if change_type:
                events = [event for event in events if event.change_type == change_type]
            if search:
                query = search.casefold()
                events = [
                    event
                    for event in events
                    if query in event.relative_path.casefold()
                ]
            return events[:limit]
        try:
            return await asyncio.to_thread(
                self.database.list_file_integrity_events,
                limit=limit,
                folder_id=folder_id,
                change_type=change_type,
                search=search,
            )
        except Exception as exc:
            self._mark_storage_failure(exc)
            raise StorageUnavailableError(
                "File integrity events could not be read"
            ) from exc

    async def database_query_history(
        self, start: datetime, end: datetime, limit: int
    ) -> list[HistoricalSample]:
        start = as_utc(start, label="history start")
        end = as_utc(end, label="history end")
        if not self.storage.persistence_available:
            raise StorageUnavailableError("Historical telemetry storage is unavailable")
        try:
            result = await asyncio.to_thread(
                self.database.list_historical_samples, start, end, limit
            )
        except Exception as exc:
            self._mark_storage_failure(exc)
            raise StorageUnavailableError(
                "Historical telemetry could not be read"
            ) from exc
        self._mark_storage_success()
        return result

    async def database_query_intelligence(
        self, start: datetime, end: datetime, limit: int
    ) -> HistoricalIntelligence:
        start = as_utc(start, label="history start")
        end = as_utc(end, label="history end")
        if not self.storage.persistence_available:
            raise StorageUnavailableError("Historical telemetry storage is unavailable")
        try:
            sample_page, process_page, connection_page = await asyncio.gather(
                asyncio.to_thread(
                    self.database.list_historical_samples_page, start, end, limit
                ),
                asyncio.to_thread(
                    self.database.list_process_observations_page, start, end, limit
                ),
                asyncio.to_thread(
                    self.database.list_connection_summaries_page, start, end, limit
                ),
            )
        except Exception as exc:
            self._mark_storage_failure(exc)
            raise StorageUnavailableError(
                "Historical intelligence could not be read"
            ) from exc
        samples, samples_truncated = sample_page
        processes, processes_truncated = process_page
        connections, connections_truncated = connection_page
        gaps: list[HistoryGap] = []
        previous = samples[0].timestamp if samples and samples_truncated else start
        for item in samples:
            threshold = max(30, item.resolution_seconds * 2)
            coverage_start = item.coverage_start or item.timestamp
            coverage_end = item.coverage_end or (
                item.timestamp + timedelta(seconds=item.resolution_seconds)
            )
            delta = (coverage_start - previous).total_seconds()
            if delta > threshold:
                gaps.append(
                    HistoryGap(start=previous, end=coverage_start, duration_seconds=delta)
                )
            previous = max(previous, coverage_end)
        tail = (end - previous).total_seconds()
        if tail > 30:
            gaps.append(HistoryGap(start=previous, end=end, duration_seconds=tail))
        self._mark_storage_success()
        return HistoricalIntelligence(
            start=start,
            end=end,
            samples=samples,
            process_observations=processes,
            connection_summaries=connections,
            gaps=gaps,
            truncated=(samples_truncated or processes_truncated or connections_truncated),
            truncated_datasets=[
                name
                for name, truncated in (
                    ("telemetry", samples_truncated),
                    ("processes", processes_truncated),
                    ("connections", connections_truncated),
                )
                if truncated
            ],
        )

    async def record_event(
        self,
        event_type: str,
        severity: str,
        title: str,
        summary: str,
        related_id: str | None = None,
        evidence: dict[str, Any] | None = None,
    ) -> EventRecord:
        event = EventRecord(
            id=str(uuid4()),
            timestamp=datetime.now(UTC),
            event_type=event_type,
            severity=severity,  # type: ignore[arg-type]
            title=title,
            summary=summary,
            related_id=related_id,
            evidence=evidence or {},
        )
        self._remember_event(event)
        if self.settings.event_logging_enabled:
            await self._persist_event(event)
        return event

    async def list_events(
        self,
        limit: int = 200,
        event_type: str | None = None,
        severity: str | None = None,
        search: str | None = None,
    ) -> list[EventRecord]:
        persisted: list[EventRecord] = []
        if self.storage.persistence_available:
            try:
                persisted = await asyncio.to_thread(
                    self.database.list_events,
                    limit,
                    event_type,
                    severity,
                    search,
                )
            except Exception as exc:
                self._mark_storage_failure(exc)
        merged = {event.id: event for event in persisted}
        for event in self.live_events:
            if event_type and event.event_type != event_type:
                continue
            if severity and event.severity != severity:
                continue
            if search and search.casefold() not in (
                f"{event.title} {event.summary}".casefold()
            ):
                continue
            merged[event.id] = event
        return sorted(merged.values(), key=lambda item: item.timestamp, reverse=True)[:limit]

    async def create_incident(self, request: IncidentCreate) -> Incident:
        alerts = await self.list_alerts(500, include_dismissed=True)
        selected = [alert for alert in alerts if alert.id in set(request.alert_ids)]
        if len(selected) != len(set(request.alert_ids)):
            raise ValueError("One or more selected alerts were not found")
        now = datetime.now(UTC)
        first = min((alert.timestamp for alert in selected), default=now)
        last = max((alert.timestamp for alert in selected), default=now)
        process_ids = sorted(
            {
                int(alert.evidence["pid"])
                for alert in selected
                if isinstance(alert.evidence.get("pid"), int)
            }
        )
        incident = Incident(
            id=f"INC-{now.strftime('%Y%m%d')}-{uuid4().hex[:8].upper()}",
            title=request.title,
            severity=request.severity,
            detection_timestamp=now,
            first_observed=first,
            last_observed=last,
            related_processes=process_ids,
            related_alerts=list(dict.fromkeys(request.alert_ids)),
            evidence_summary=request.evidence_summary,
            analyst_notes=request.analyst_notes,
        )
        try:
            await asyncio.to_thread(self.database.add_incident, incident)
        except Exception as exc:
            self._mark_storage_failure(exc)
            raise StorageUnavailableError("Incident was not persisted") from exc
        for alert_id in incident.related_alerts:
            current = self._live_alert(alert_id)
            if current is not None:
                self._remember_live_alert(
                    current.model_copy(update={"incident_id": incident.id}), authoritative=True
                )
        await self.record_event(
            "incident_created",
            "HIGH" if incident.severity != "INFO" else "LOW",
            f"Incident created: {incident.title}",
            "The local operator grouped selected evidence into an incident.",
            incident.id,
            {"alert_count": len(incident.related_alerts)},
        )
        return incident

    async def list_incidents(self) -> list[Incident]:
        if not self.storage.persistence_available:
            return []
        try:
            return await asyncio.to_thread(self.database.list_incidents)
        except Exception as exc:
            self._mark_storage_failure(exc)
            return []

    async def update_incident(
        self, incident_id: str, update: IncidentUpdate
    ) -> Incident | None:
        try:
            result = await asyncio.to_thread(
                self.database.update_incident,
                incident_id,
                update,
                datetime.now(UTC),
            )
        except Exception as exc:
            self._mark_storage_failure(exc)
            raise StorageUnavailableError("Incident update was not persisted") from exc
        if result:
            await self.record_event(
                "incident_update",
                "LOW",
                f"Incident updated: {result.title}",
                "The local operator changed incident status or notes.",
                result.id,
                update.model_dump(exclude_none=True),
            )
        return result
