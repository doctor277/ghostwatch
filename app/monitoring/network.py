from __future__ import annotations

import hashlib
import ipaddress
import socket
import time
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import psutil

from app.config import MAX_CONNECTIONS
from app.models import ConnectionAggregate, ConnectionInfo, NetworkSample


@dataclass(slots=True)
class CounterState:
    bytes_sent: int
    bytes_recv: int
    timestamp: float


@dataclass(slots=True)
class SeenConnection:
    first_seen: datetime
    first_monotonic: float
    last_seen: datetime


class NetworkCollector:
    """Aggregate every socket while bounding detail and lifecycle allocations."""

    def __init__(
        self,
        psutil_module=psutil,
        clock=time.monotonic,
        detail_limit: int = MAX_CONNECTIONS,
        lifecycle_limit: int = 20_000,
    ) -> None:
        self.psutil = psutil_module
        self.clock = clock
        self.detail_limit = max(1, detail_limit)
        self.lifecycle_limit = max(self.detail_limit, lifecycle_limit)
        self._previous: CounterState | None = None
        self._seen: dict[tuple[object, ...], SeenConnection] = {}
        self._name_cache: dict[int, tuple[str, float]] = {}
        self._last_details: list[ConnectionInfo] = []
        self._last_connection_sample: NetworkSample | None = None
        self.connection_counts_by_pid: Counter[int] = Counter()
        self.connection_rankings: list[ConnectionAggregate] = []
        self.remote_endpoints_by_pid: dict[int, list[str]] = {}
        self.recently_closed: deque[ConnectionInfo] = deque(maxlen=100)

    def calculate_rates(self, sent: int, received: int, now: float) -> tuple[float, float]:
        current = CounterState(sent, received, now)
        if self._previous is None:
            self._previous = current
            return 0.0, 0.0
        elapsed = now - self._previous.timestamp
        sent_delta = sent - self._previous.bytes_sent
        received_delta = received - self._previous.bytes_recv
        self._previous = current
        if elapsed <= 0 or sent_delta < 0 or received_delta < 0:
            return 0.0, 0.0
        return sent_delta / elapsed, received_delta / elapsed

    @staticmethod
    def _address(value: Any) -> tuple[str, int | None]:
        if not value:
            return "—", None
        if hasattr(value, "ip"):
            return str(value.ip), int(value.port)
        if isinstance(value, tuple):
            return str(value[0]), int(value[1]) if len(value) > 1 else None
        return str(value), None

    @staticmethod
    def _scope(address: str) -> str:
        if address == "—":
            return "unspecified"
        try:
            parsed = ipaddress.ip_address(address.split("%", 1)[0])
        except ValueError:
            return "unknown"
        if parsed.is_loopback:
            return "loopback"
        if parsed.is_link_local:
            return "link-local"
        if parsed.is_private:
            return "private"
        if parsed.is_unspecified:
            return "unspecified"
        return "external"

    @staticmethod
    def _connection_key(
        pid: int | None,
        protocol: str,
        local_address: str,
        local_port: int | None,
        remote_address: str,
        remote_port: int | None,
    ) -> tuple[object, ...]:
        return (pid, protocol, local_address, local_port, remote_address, remote_port)

    @staticmethod
    def _connection_id(key: tuple[object, ...]) -> str:
        digest = hashlib.sha256(repr(key).encode("utf-8")).hexdigest()[:20]
        return f"conn-{digest}"

    def _process_name(self, pid: int | None, now: float) -> str:
        if pid in (0, None):
            return "System" if pid == 0 else "Unavailable"
        cached = self._name_cache.get(pid)
        if cached and cached[1] >= now:
            return cached[0]
        try:
            name = str(self.psutil.Process(pid).name() or "Unknown")
        except (psutil.NoSuchProcess, psutil.AccessDenied, ProcessLookupError, OSError):
            name = "Unavailable"
        self._name_cache[pid] = (name, now + 60.0)
        return name

    def _unavailable_connections(
        self,
        sent_rate: float,
        recv_rate: float,
        errors: list[str],
        unavailable: list[str],
        reason: str = "provider_error",
    ) -> tuple[NetworkSample, list[ConnectionInfo]]:
        unavailable.extend(
            ("active_connections", "established_connections", "unique_remote_destinations")
        )
        previous = self._last_connection_sample
        sample = NetworkSample(
            bytes_sent_per_second=round(sent_rate, 2),
            bytes_recv_per_second=round(recv_rate, 2),
            upload_mbps=round(sent_rate / 1_048_576, 3),
            download_mbps=round(recv_rate / 1_048_576, 3),
            active_connections=previous.active_connections if previous else 0,
            established_connections=previous.established_connections if previous else 0,
            unique_remote_destinations=(
                previous.unique_remote_destinations if previous else 0
            ),
            new_connections=0,
            disappeared_connections=0,
            collection_error="; ".join(errors) or "Connection details unavailable",
            unavailable_metrics=list(dict.fromkeys(unavailable)),
            connection_collection_status="unavailable",
            lifecycle_confidence="unavailable",
            connection_collection_reason=reason,
        )
        return sample, list(self._last_details)

    def collect(self) -> tuple[NetworkSample, list[ConnectionInfo]]:
        errors: list[str] = []
        unavailable: list[str] = []
        now_monotonic = self.clock()
        now_datetime = datetime.now(UTC)
        try:
            counters = self.psutil.net_io_counters()
            sent = int(counters.bytes_sent)
            received = int(counters.bytes_recv)
            if sent < 0 or received < 0:
                raise ValueError("negative network counter")
            sent_rate, recv_rate = self.calculate_rates(sent, received, now_monotonic)
        except (psutil.Error, OSError, RuntimeError, TimeoutError, TypeError, ValueError) as exc:
            sent_rate, recv_rate = 0.0, 0.0
            errors.append(f"Network counters unavailable ({type(exc).__name__})")
            unavailable.extend(
                ("bytes_sent_per_second", "bytes_recv_per_second", "upload_mbps", "download_mbps")
            )

        try:
            raw_connections = self.psutil.net_connections(kind="inet")
        except (psutil.AccessDenied, PermissionError) as exc:
            errors.append(
                f"Connection details unavailable ({type(exc).__name__}; access was denied)"
            )
            return self._unavailable_connections(
                sent_rate, recv_rate, errors, unavailable, "permission_denied"
            )
        except NotImplementedError as exc:
            errors.append(f"Connection details unsupported ({type(exc).__name__})")
            return self._unavailable_connections(
                sent_rate, recv_rate, errors, unavailable, "unsupported"
            )
        except (psutil.Error, OSError, RuntimeError, TimeoutError, TypeError, ValueError) as exc:
            errors.append(
                f"Connection details unavailable ({type(exc).__name__}; provider or sandbox failure)"
            )
            return self._unavailable_connections(
                sent_rate, recv_rate, errors, unavailable, "provider_error"
            )

        counts: Counter[int] = Counter()
        total_by_pid: Counter[int | None] = Counter()
        established_by_pid: Counter[int | None] = Counter()
        destinations_by_pid: dict[int | None, set[str]] = defaultdict(set)
        endpoints_by_pid: dict[int, set[str]] = defaultdict(set)
        names: dict[int | None, str] = {}
        current_keys: set[tuple[object, ...]] = set()
        details: list[ConnectionInfo] = []
        established = 0
        active = 0
        remote_destinations: set[str] = set()
        new_count = 0
        lifecycle_truncated = False

        try:
            for item in raw_connections:
                status = str(getattr(item, "status", "NONE")).upper()
                pid_value = getattr(item, "pid", None)
                pid = int(pid_value) if pid_value is not None else None
                socket_type = getattr(item, "type", socket.SOCK_STREAM)
                protocol = "TCP" if socket_type == socket.SOCK_STREAM else "UDP"
                local_address, local_port = self._address(getattr(item, "laddr", None))
                remote_address, remote_port = self._address(getattr(item, "raddr", None))
                key = self._connection_key(
                    pid, protocol, local_address, local_port, remote_address, remote_port
                )
                active += 1
                total_by_pid[pid] += 1
                if pid is not None:
                    counts[pid] += 1
                if pid not in names:
                    names[pid] = self._process_name(pid, now_monotonic)
                if protocol == "TCP" and status == "ESTABLISHED":
                    established += 1
                    established_by_pid[pid] += 1
                if remote_address != "—":
                    remote_destinations.add(remote_address)
                    destinations_by_pid[pid].add(remote_address)
                    if pid is not None and len(endpoints_by_pid[pid]) < 50:
                        endpoint = (
                            f"{remote_address}:{remote_port}"
                            if remote_port is not None
                            else remote_address
                        )
                        endpoints_by_pid[pid].add(endpoint)

                tracked = key in self._seen or len(current_keys) < self.lifecycle_limit
                if tracked:
                    current_keys.add(key)
                else:
                    lifecycle_truncated = True
                seen = self._seen.get(key)
                is_new = seen is None
                if is_new and tracked:
                    new_count += 1
                if len(details) < self.detail_limit:
                    first_seen = seen.first_seen if seen else now_datetime
                    first_monotonic = seen.first_monotonic if seen else now_monotonic
                    details.append(
                        ConnectionInfo(
                            id=self._connection_id(key),
                            pid=pid,
                            process_name=names[pid],
                            local_address=local_address,
                            local_port=local_port,
                            remote_address=remote_address,
                            remote_port=remote_port,
                            protocol=protocol,
                            status=status,
                            first_seen=first_seen,
                            last_seen=now_datetime,
                            duration_seconds=round(
                                max(0.0, now_monotonic - first_monotonic), 1
                            ),
                            address_scope=self._scope(remote_address),
                            is_new=is_new,
                        )
                    )
        except (psutil.AccessDenied, PermissionError) as exc:
            errors.append(
                f"Connection details unavailable ({type(exc).__name__}; access was denied mid-enumeration)"
            )
            return self._unavailable_connections(
                sent_rate, recv_rate, errors, unavailable, "permission_denied"
            )
        except (psutil.Error, OSError, RuntimeError, TimeoutError, TypeError, ValueError) as exc:
            errors.append(
                f"Connection details unavailable ({type(exc).__name__}; provider failed mid-enumeration)"
            )
            return self._unavailable_connections(
                sent_rate, recv_rate, errors, unavailable, "provider_error"
            )

        disappeared_keys: set[tuple[object, ...]] = set()
        if not lifecycle_truncated:
            disappeared_keys = set(self._seen) - current_keys
            closed_detail_limit = self.recently_closed.maxlen or 0
            for index, key in enumerate(disappeared_keys):
                seen = self._seen.pop(key)
                if index >= closed_detail_limit:
                    continue
                pid, protocol, local_address, local_port, remote_address, remote_port = key
                self.recently_closed.appendleft(
                    ConnectionInfo(
                        id=self._connection_id(key),
                        pid=pid if isinstance(pid, int) else None,
                        process_name=self._process_name(
                            pid if isinstance(pid, int) else None, now_monotonic
                        ),
                        local_address=str(local_address),
                        local_port=local_port if isinstance(local_port, int) else None,
                        remote_address=str(remote_address),
                        remote_port=remote_port if isinstance(remote_port, int) else None,
                        protocol=str(protocol),
                        status="CLOSED",
                        first_seen=seen.first_seen,
                        last_seen=seen.last_seen,
                        duration_seconds=round(
                            max(0.0, now_monotonic - seen.first_monotonic), 1
                        ),
                        address_scope=self._scope(str(remote_address)),
                    )
                )
        else:
            # A partial lifecycle sample cannot infer closures. Retain only the
            # bounded keys observed in this sample so sustained churn cannot grow
            # stale lifecycle state beyond lifecycle_limit.
            self._seen = {
                key: seen for key, seen in self._seen.items() if key in current_keys
            }

        for key in current_keys:
            seen = self._seen.get(key)
            if seen is None:
                self._seen[key] = SeenConnection(
                    now_datetime, now_monotonic, now_datetime
                )
            else:
                seen.last_seen = now_datetime

        self.connection_counts_by_pid = counts
        self.remote_endpoints_by_pid = {
            pid: sorted(endpoints) for pid, endpoints in endpoints_by_pid.items()
        }
        self.connection_rankings = sorted(
            (
                ConnectionAggregate(
                    pid=pid,
                    process_name=names.get(pid, "Unavailable"),
                    connection_count=count,
                    established_count=established_by_pid.get(pid, 0),
                    destination_count=len(destinations_by_pid.get(pid, set())),
                )
                for pid, count in total_by_pid.items()
            ),
            key=lambda row: row.connection_count,
            reverse=True,
        )
        lifecycle_status = "partial" if lifecycle_truncated else "high"
        sample = NetworkSample(
            bytes_sent_per_second=round(sent_rate, 2),
            bytes_recv_per_second=round(recv_rate, 2),
            upload_mbps=round(sent_rate / 1_048_576, 3),
            download_mbps=round(recv_rate / 1_048_576, 3),
            active_connections=active,
            established_connections=established,
            unique_remote_destinations=len(remote_destinations),
            new_connections=new_count,
            disappeared_connections=len(disappeared_keys),
            collection_error="; ".join(errors) or None,
            unavailable_metrics=list(dict.fromkeys(unavailable)),
            connection_collection_status=(
                "partial" if lifecycle_truncated else "healthy"
            ),
            lifecycle_confidence=lifecycle_status,
        )
        self._last_connection_sample = sample
        self._last_details = details
        return sample, details
