from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.models import NetworkSample, SystemSample, TelemetrySnapshot


@pytest.fixture
def system_sample() -> SystemSample:
    return SystemSample(
        timestamp=datetime.now(UTC),
        cpu_percent=25,
        memory_percent=40,
        memory_used=4_000_000,
        memory_total=10_000_000,
        process_count=120,
        uptime_seconds=5000,
        disk_percent=30,
    )


@pytest.fixture
def network_sample() -> NetworkSample:
    return NetworkSample(
        bytes_sent_per_second=1000,
        bytes_recv_per_second=2000,
        upload_mbps=0.001,
        download_mbps=0.002,
        active_connections=10,
        established_connections=5,
    )


@pytest.fixture
def snapshot(system_sample, network_sample) -> TelemetrySnapshot:
    return TelemetrySnapshot(
        sequence=1,
        system=system_sample,
        network=network_sample,
        connections=[],
        processes=[],
        alerts=[],
    )
