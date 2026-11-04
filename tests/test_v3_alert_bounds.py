from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, datetime
from enum import Enum

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.models import Alert, EventRecord
from app.service import TelemetryService
from app.storage.database import Database

TRUNCATED = "[TRUNCATED]"
UNSERIALIZABLE = "[UNSERIALIZABLE]"
PRIVATE = "synthetic-private-marker"
LOCAL_ORIGIN = {"origin": "http://127.0.0.1:8765"}
EVIDENCE_LIMIT = 16 * 1024


def alert(**changes):
    values = {
        "id": "synthetic-alert",
        "timestamp": datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
        "severity": "CRITICAL",
        "source": "synthetic-source",
        "metric": "synthetic-metric",
        "explanation": "synthetic",
    }
    return Alert(**(values | changes))


class Static:
    def __init__(self, value):
        self.value = value

    def collect(self, *args, **kwargs):
        return self.value


class Detector:
    def __init__(self):
        self.alerts = []

    def evaluate(self, *args, **kwargs):
        return self.alerts


@pytest.fixture
async def collecting(tmp_path, system_sample, network_sample):
    service = TelemetryService(Database(tmp_path / "synthetic.db"), storage_retry_seconds=0)
    assert await service.initialize_storage()
    service.system_collector = Static(system_sample)
    service.network_collector = Static((network_sample, []))
    service.process_collector = Static([])
    service.detection_engine = Detector()
    await service.collect_once()
    return service


@pytest.mark.parametrize("length", [1999, 2000])
def test_explanations_within_the_contract_are_unchanged(length):
    text = "x" * length
    item = alert(explanation=text)
    assert item.explanation == text
    assert TelemetryService._alert_event(item).summary == text


@pytest.mark.parametrize("length", [2001, 2100, 1_000_000])
def test_oversized_explanations_are_bounded_and_marked(length):
    item = alert(explanation="x" * length)
    assert len(item.explanation) == 2000
    assert item.explanation.startswith("x" * 1900)
    assert item.explanation.endswith(TRUNCATED)
    assert TelemetryService._alert_event(item).summary == item.explanation


@pytest.mark.parametrize(
    "text",
    ["\u00e9" * 2001, "\U0001f600" * 2001, "x" * 1999 + "\ud800", "\ud800" * 1000],
)
def test_unicode_and_surrogate_text_is_bounded_and_encodable(text):
    item = alert(explanation=text)
    assert len(item.explanation) <= 2000
    assert item.explanation.endswith(TRUNCATED)
    item.explanation.encode("utf-8")
    assert TelemetryService._alert_event(item).summary == item.explanation


def test_two_thousand_multibyte_characters_are_unchanged():
    text = "\U0001f600" * 2000
    assert alert(explanation=text).explanation == text


@pytest.mark.parametrize("field", ["metric", "source", "rule_id"])
def test_labels_are_bounded_and_marked(field):
    item = alert(**{field: "m" * 201})
    value = getattr(item, field)
    assert len(value) == 200
    assert value.endswith(TRUNCATED)
    assert alert(**{field: "m" * 200}).model_dump()[field] == "m" * 200


def test_long_identifiers_map_to_a_stable_marked_digest():
    long_id = "i" * 201
    first, second = alert(id=long_id, incident_id=long_id), alert(id=long_id)
    assert first.id == second.id == first.incident_id
    assert first.id != alert(id="j" * 201).id
    assert len(first.id) <= 200
    assert first.id.startswith("truncated-sha256-")
    assert alert(id="i" * 200).id == "i" * 200
    assert TelemetryService._alert_event(first).related_id == first.id


def test_alert_identity_and_relationships_are_preserved():
    item = alert(
        explanation="x" * 3000,
        rule_id="synthetic-rule",
        incident_id="synthetic-incident",
        acknowledged=True,
        occurrence_count=7,
    )
    event = TelemetryService._alert_event(item)
    assert item.id == "synthetic-alert"
    assert item.severity == "CRITICAL"
    assert item.source == "synthetic-source"
    assert item.metric == "synthetic-metric"
    assert item.timestamp == datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert item.rule_id == "synthetic-rule"
    assert item.incident_id == "synthetic-incident"
    assert item.acknowledged
    assert item.occurrence_count == 7
    assert (event.severity, event.related_id) == ("CRITICAL", "synthetic-alert")
    assert event.timestamp == item.timestamp


async def test_oversized_alert_does_not_stop_valid_alerts_or_telemetry(collecting):
    service = collecting
    service.detection_engine.alerts = [
        alert(id="oversized", explanation="x" * 1_000_000),
        alert(id="valid"),
    ]
    for sequence in (2, 3, 4):
        assert (await service.collect_once()).sequence == sequence
    stored = {item.id: item for item in service.database.list_alerts(10, True)}
    assert set(stored) == {"oversized", "valid"}
    assert len(stored["oversized"].explanation) == 2000
    assert service.storage.status == "healthy"
    assert service.encode_snapshot(service.latest) is not None


async def test_alert_that_bypassed_validation_does_not_abort_collection(collecting, caplog):
    service = collecting
    bypassed = Alert.model_construct(
        **(alert(id="bypassed").model_dump() | {"explanation": PRIVATE * 200})
    )
    service.detection_engine.alerts = [bypassed, alert(id="valid")]

    with caplog.at_level(logging.DEBUG, logger="app.service"):
        snapshot = await service.collect_once()

    assert snapshot.sequence == 2
    assert {item.id for item in service.live_alerts} == {"bypassed", "valid"}
    assert {item.id for item in service.database.list_alerts(10, True)} == {"bypassed", "valid"}
    related = {event.related_id for event in service.live_events}
    assert "valid" in related
    assert "bypassed" not in related
    assert PRIVATE not in caplog.text
    assert "Alert event could not be created (ValidationError)" in caplog.text


async def test_recovery_needs_no_restart_when_a_bad_alert_stops_firing(collecting):
    service = collecting
    service.detection_engine.alerts = [alert(explanation="x" * 3000)]
    await service.collect_once()
    service.detection_engine.alerts = []
    snapshot = await service.collect_once()
    assert snapshot.sequence == 3
    assert service.encode_snapshot(snapshot) is not None
    assert service.storage.status == "healthy"


def test_oversized_alert_survives_restart_on_rest_and_reports(tmp_path):
    path = tmp_path / "synthetic.db"
    app = create_app(path, start_background=False)
    with TestClient(app) as client:
        service = app.state.telemetry
        item = alert(explanation="x" * 5000)
        client.portal.call(service._persist_alert, item)
        client.portal.call(service._persist_event, service._alert_event(item))

    app = create_app(path, start_background=False)
    with TestClient(app) as client:
        rows = client.get("/api/alerts").json()
        assert len(rows[0]["explanation"]) == 2000
        assert rows[0]["severity"] == "CRITICAL"
        assert client.get("/api/events").status_code == 200
        for output_format in ("json", "csv", "html"):
            assert client.get(f"/api/reports/security-alerts.{output_format}").status_code == 200
        patched = client.patch(
            f"/api/alerts/{rows[0]['id']}", json={"acknowledged": True}, headers=LOCAL_ORIGIN
        )
        assert patched.json()["acknowledged"]


def test_oversized_row_from_before_this_fix_is_bounded_on_read(tmp_path):
    database = Database(tmp_path / "synthetic.db")
    database.initialize()
    stored = Alert.model_construct(**alert().model_dump())
    stored.explanation = "x" * 3000
    database.add_alert(stored, 100)
    assert len(database.list_alerts(10, True)[0].explanation) == 2000


async def test_oversized_alert_queued_during_outage_flushes_after_recovery(
    collecting, monkeypatch
):
    service = collecting
    real_add_alert = service.database.add_alert

    def fail(*args):
        raise sqlite3.OperationalError("synthetic outage")

    monkeypatch.setattr(service.database, "add_alert", fail)
    service.detection_engine.alerts = [alert(explanation="x" * 3000)]
    await service.collect_once()
    assert service.storage.status == "degraded"
    assert len(service._pending_alerts) == 1

    monkeypatch.setattr(service.database, "add_alert", real_add_alert)
    service.detection_engine.alerts = []
    await service.collect_once()
    assert service.storage.status == "healthy"
    assert not service._pending_alerts
    assert len(service.database.list_alerts(10, True)[0].explanation) == 2000


@pytest.mark.parametrize("error", [ValueError, TypeError, OverflowError])
async def test_unwritable_record_is_rejected_without_degrading_storage(
    collecting, monkeypatch, caplog, error
):
    service = collecting

    def fail(*args):
        raise error(PRIVATE)

    monkeypatch.setattr(service.database, "add_alert", fail)
    with caplog.at_level(logging.DEBUG, logger="app.service"):
        assert not await service._persist_alert(alert())
    assert service.storage.status == "healthy"
    assert not service._pending_alerts
    assert service.rejected_writes == 1
    assert PRIVATE not in caplog.text


async def test_unwritable_queued_record_does_not_block_the_queue(collecting, monkeypatch):
    service = collecting
    real_add_alert = service.database.add_alert
    attempts = []

    def add_alert(item, retention):
        attempts.append(item.id)
        if item.id == "poison":
            raise ValueError("synthetic record failure")
        return real_add_alert(item, retention)

    poison = alert(id="poison").model_copy(update={"persistence_pending": True})
    service._remember_live_alert(poison)
    service._pending_alerts["poison"] = poison
    service._pending_alerts["valid"] = alert(id="valid")
    monkeypatch.setattr(service.database, "add_alert", add_alert)

    await service._maybe_recover_storage()
    await service._maybe_recover_storage()

    assert attempts == ["poison", "valid"]
    assert not service._pending_alerts
    assert service.rejected_writes == 1
    assert service.storage.status == "healthy"
    assert [item.id for item in service.database.list_alerts(10, True)] == ["valid"]
    assert not next(item for item in service.live_alerts if item.id == "poison").persistence_pending


async def test_unwritable_queued_event_does_not_block_the_queue(collecting, monkeypatch):
    service = collecting
    real_add_event = service.database.add_event

    def add_event(event):
        if event.id == "poison":
            raise ValueError("synthetic record failure")
        return real_add_event(event)

    for identifier in ("poison", "valid"):
        service._pending_events[identifier] = EventRecord(
            id=identifier,
            timestamp=datetime.now(UTC),
            event_type="synthetic",
            title="synthetic",
        )
    monkeypatch.setattr(service.database, "add_event", add_event)

    await service._maybe_recover_storage()

    assert not service._pending_events
    assert service.rejected_writes == 1
    assert service.storage.status == "healthy"


async def test_storage_outages_still_queue_and_retry(collecting, monkeypatch):
    service = collecting
    real_add_alert = service.database.add_alert

    def fail(*args):
        raise sqlite3.OperationalError("synthetic outage")

    monkeypatch.setattr(service.database, "add_alert", fail)
    assert not await service._persist_alert(alert())
    assert service.storage.status == "degraded"
    assert "synthetic-alert" in service._pending_alerts
    assert service.rejected_writes == 0

    await service._maybe_recover_storage()
    assert "synthetic-alert" in service._pending_alerts

    monkeypatch.setattr(service.database, "add_alert", real_add_alert)
    await service._maybe_recover_storage()
    assert not service._pending_alerts
    assert service.storage.status == "healthy"


async def test_overflowing_occurrence_count_does_not_poison_storage(collecting):
    service = collecting
    service.detection_engine.alerts = [alert(occurrence_count=2**70), alert(id="valid")]
    assert (await service.collect_once()).sequence == 2
    assert service.storage.status == "healthy"
    assert not service._pending_alerts
    assert service.rejected_writes == 1
    assert [item.id for item in service.database.list_alerts(10, True)] == ["valid"]


def test_rejected_writes_are_reported_by_health(tmp_path):
    app = create_app(tmp_path / "synthetic.db", start_background=False)
    with TestClient(app) as client:
        assert client.get("/api/health").json()["rejected_writes"] == 0
        app.state.telemetry.rejected_writes = 2
        assert client.get("/api/health").json()["rejected_writes"] == 2


def test_oversized_evidence_is_replaced_by_an_explicit_marker():
    item = alert(evidence={"blob": PRIVATE * 2_000_000})
    assert item.evidence == {TRUNCATED: f"Evidence exceeded {EVIDENCE_LIMIT} bytes"}
    assert TelemetryService._alert_event(item).evidence == item.evidence


def test_evidence_within_the_limit_is_unchanged():
    evidence = {"blob": "x" * (EVIDENCE_LIMIT - 64)}
    assert alert(evidence=evidence).evidence == evidence


async def test_oversized_evidence_keeps_the_snapshot_available(collecting):
    service = collecting
    service.detection_engine.alerts = [alert(evidence={"blob": "x" * (8 * 1024 * 1024)})]
    snapshot = await service.collect_once()
    assert service.encode_snapshot(snapshot) is not None


def test_shared_references_cannot_expand_exponentially():
    shared = []
    for _ in range(40):
        shared = [shared, shared]
    item = alert(evidence={"value": shared})
    assert TRUNCATED in json.dumps(item.evidence)
    assert len(json.dumps(item.evidence)) <= EVIDENCE_LIMIT + 64


def test_wide_evidence_is_bounded_and_marked():
    item = alert(evidence={"value": list(range(1_000_000))})
    assert TRUNCATED in json.dumps(item.evidence)


def test_generators_are_marked_and_not_consumed():
    consumed = []

    def values():
        while True:
            consumed.append(1)
            yield 1

    assert alert(evidence={"value": values()}).evidence == {"value": UNSERIALIZABLE}
    assert consumed == []


def test_depth_limit_is_not_reported_as_unserializable():
    evidence = value = {}
    for _ in range(40):
        value["next"] = {}
        value = value["next"]
    text = json.dumps(alert(evidence=evidence).evidence)
    assert TRUNCATED in text
    assert UNSERIALIZABLE not in text


def test_cycles_stay_unserializable():
    evidence = {}
    evidence["self"] = evidence
    assert UNSERIALIZABLE in json.dumps(alert(evidence=evidence).evidence)


@pytest.mark.parametrize("value", [10**5000, -(10**5000)], ids=["positive", "negative"])
async def test_oversized_integers_are_marked_and_do_not_poison_storage(collecting, value):
    service = collecting
    item = alert(evidence={"value": value})
    assert item.evidence == {"value": UNSERIALIZABLE}
    assert await service._persist_alert(item)
    assert service.storage.status == "healthy"


def test_ordinary_integers_are_unchanged():
    evidence = {"value": 2**64, "small": -1, "flag": True}
    assert alert(evidence=evidence).evidence == evidence


def test_string_enums_keep_their_value():
    class Color(str, Enum):
        RED = "red"

    assert alert(evidence={"value": Color.RED}).evidence == {"value": "red"}


def test_truncated_content_does_not_reach_responses_or_logs(tmp_path, caplog):
    app = create_app(tmp_path / "synthetic.db", start_background=False)
    with TestClient(app) as client:
        service = app.state.telemetry
        service._remember_live_alert(alert(explanation="x" * 2100 + PRIVATE))
        with caplog.at_level(logging.DEBUG):
            response = client.get("/api/alerts")
        assert response.status_code == 200
        assert PRIVATE not in response.text
        assert PRIVATE not in caplog.text


def test_redaction_applies_to_oversized_alerts(tmp_path, snapshot):
    app = create_app(tmp_path / "synthetic.db", start_background=False)
    with TestClient(app) as client:
        service = app.state.telemetry
        item = alert(
            explanation="x" * 3000,
            evidence={"path": f"C:\\synthetic\\{PRIVATE}", "username": PRIVATE},
        )
        service._remember_live_alert(item)
        service.latest = snapshot.model_copy(update={"alerts": [item]})
        report = client.get("/api/reports/security-alerts.json")
        assert report.status_code == 200
        assert PRIVATE not in report.text
        assert "[REDACTED]" in report.text


def test_host_and_origin_checks_hold_with_oversized_alerts(tmp_path):
    app = create_app(tmp_path / "synthetic.db", start_background=False)
    with TestClient(app) as client:
        service = app.state.telemetry
        service._remember_live_alert(alert(explanation="x" * 3000))
        assert client.get("/api/alerts", headers={"host": "attacker.example"}).status_code == 400
        forbidden = client.patch(
            "/api/alerts/synthetic-alert",
            json={"acknowledged": True},
            headers={"origin": "https://example.com"},
        )
        assert forbidden.status_code == 403
        assert not service.live_alerts[0].acknowledged
