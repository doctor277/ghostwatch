from __future__ import annotations

from app.detection.engine import DetectionEngine
from app.models import Settings


class Clock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value


def test_duration_based_cpu_alert(system_sample, network_sample):
    clock = Clock()
    engine = DetectionEngine(clock=clock, cooldown_seconds=60)
    settings = Settings(cpu_threshold=80, threshold_duration=10)
    system_sample.cpu_percent = 95
    assert engine.evaluate(system_sample, network_sample, settings) == []
    clock.value = 9
    assert engine.evaluate(system_sample, network_sample, settings) == []
    clock.value = 10
    alerts = engine.evaluate(system_sample, network_sample, settings)
    assert len(alerts) == 1
    assert alerts[0].severity == "CRITICAL"
    assert "not proof" in alerts[0].explanation


def test_breach_reset_requires_full_duration(system_sample, network_sample):
    clock = Clock()
    engine = DetectionEngine(clock=clock)
    settings = Settings(cpu_threshold=80, threshold_duration=10)
    system_sample.cpu_percent = 95
    engine.evaluate(system_sample, network_sample, settings)
    clock.value = 6
    system_sample.cpu_percent = 10
    engine.evaluate(system_sample, network_sample, settings)
    clock.value = 7
    system_sample.cpu_percent = 95
    engine.evaluate(system_sample, network_sample, settings)
    clock.value = 12
    assert engine.evaluate(system_sample, network_sample, settings) == []


def test_bandwidth_threshold_alert(system_sample, network_sample):
    clock = Clock()
    engine = DetectionEngine(clock=clock)
    settings = Settings(bandwidth_threshold_mbps=3, threshold_duration=2)
    network_sample.upload_mbps = 4
    engine.evaluate(system_sample, network_sample, settings)
    clock.value = 2
    alerts = engine.evaluate(system_sample, network_sample, settings)
    assert any(alert.metric.startswith("Upload") for alert in alerts)


def test_connection_spike_alerts_immediately(system_sample, network_sample):
    clock = Clock()
    engine = DetectionEngine(clock=clock)
    settings = Settings(connection_threshold=10000)
    engine.evaluate(system_sample, network_sample, settings)
    network_sample.established_connections = 50
    alerts = engine.evaluate(system_sample, network_sample, settings)
    assert any(alert.metric == "Rapid connection increase" for alert in alerts)


def test_collection_failure_alert(system_sample, network_sample):
    engine = DetectionEngine(clock=lambda: 0)
    network_sample.collection_error = "AccessDenied"
    alerts = engine.evaluate(system_sample, network_sample, Settings())
    assert alerts[0].severity == "INFO"


def test_collection_failure_has_stable_identity_counts_and_recovery(
    system_sample, network_sample
):
    clock = Clock()
    engine = DetectionEngine(clock=clock)
    network_sample.collection_error = "Connection details unavailable"

    first = engine.evaluate(system_sample, network_sample, Settings())[0]
    clock.value = 1
    second = engine.evaluate(system_sample, network_sample, Settings())[0]
    network_sample.collection_error = None
    recovered = engine.evaluate(system_sample, network_sample, Settings())[0]

    assert first.id == second.id == recovered.id
    assert first.condition_active is True
    assert second.occurrence_count == 1
    assert recovered.condition_active is False
    assert recovered.occurrence_count == 0
    assert recovered.last_recovery is not None


def test_cooldown_prevents_alert_spam(system_sample, network_sample):
    clock = Clock()
    engine = DetectionEngine(clock=clock, cooldown_seconds=60)
    settings = Settings(memory_threshold=50, threshold_duration=2)
    system_sample.memory_percent = 95
    engine.evaluate(system_sample, network_sample, settings)
    clock.value = 2
    assert engine.evaluate(system_sample, network_sample, settings)
    clock.value = 3
    assert engine.evaluate(system_sample, network_sample, settings) == []
