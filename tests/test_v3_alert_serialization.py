from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.ai import LocalAIAnalyst
from app.main import create_app
from app.models import AISettings, Alert, EventRecord, Settings
from app.service import TelemetryService
from app.storage.database import Database

MARKER = "[UNSERIALIZABLE]"
PRIVATE = "synthetic-private-marker"
UNAVAILABLE = {"detail": "The requested data is unavailable"}
LOCAL_ORIGIN = {"origin": "http://127.0.0.1:8765"}
REPORT_TYPES = (
    "system-health",
    "network-activity",
    "security-alerts",
    "process-activity",
    "anomaly-summary",
    "incidents",
    "daily-summary",
    "weekly-summary",
)


def circular():
    evidence = {}
    evidence["self"] = evidence
    return evidence


def branching_cycle():
    evidence = {}
    evidence["left"] = evidence
    evidence["right"] = evidence
    return evidence


def alert(**changes):
    values = {
        "id": "synthetic-alert",
        "timestamp": datetime.now(UTC),
        "severity": "WARNING",
        "source": "synthetic",
        "metric": "synthetic",
        "explanation": "synthetic",
    }
    return Alert(**(values | changes))


MALFORMED = {
    "text": lambda: alert(explanation=f"{PRIVATE} \ud800"),
    "metric": lambda: alert(metric="metric \ud800"),
    "evidence": lambda: alert(evidence={"name": f"{PRIVATE} \ud800"}),
    "nested-key": lambda: alert(evidence={"outer": {"key \ud800": 1}}),
    "object": lambda: alert(evidence={"value": object()}),
    "object-key": lambda: alert(evidence={"outer": {object(): 1}}),
    "circular": lambda: alert(evidence=circular()),
    "branching-cycle": lambda: alert(evidence=branching_cycle()),
}


@pytest.fixture
def application(tmp_path, snapshot):
    app = create_app(tmp_path / "synthetic.db", start_background=False)
    with TestClient(app) as client:
        app.state.telemetry.latest = snapshot
        yield app, client


def inject(client, service, item):
    """Mirror how collect_once remembers and persists a new alert."""

    async def run():
        service._remember_live_alert(item)
        service._remember_event(service._alert_event(item))
        await service._persist_alert(item)
        await service._persist_event(service._alert_event(item))

    client.portal.call(run)


@pytest.mark.parametrize("name", MALFORMED)
def test_malformed_alert_is_served_on_every_route(application, snapshot, name):
    app, client = application
    service = app.state.telemetry
    item = MALFORMED[name]()
    inject(client, service, item)
    service.latest = snapshot.model_copy(update={"alerts": [item]})

    assert service.storage.status == "healthy"
    assert not service._pending_alerts
    assert not service._pending_events
    assert [row["id"] for row in client.get("/api/alerts").json()] == [item.id]
    assert client.get("/api/alerts", params={"include_dismissed": True}).status_code == 200
    assert client.patch(
        f"/api/alerts/{item.id}", json={"acknowledged": True}, headers=LOCAL_ORIGIN
    ).json()["acknowledged"]
    assert client.get("/api/events").status_code == 200
    assert client.get("/api/overview").status_code == 200
    assert client.get("/api/health").json()["snapshot"] == "available"
    for report_type in REPORT_TYPES:
        for output_format in ("json", "csv", "html"):
            for redact in (True, False):
                response = client.get(
                    f"/api/reports/{report_type}.{output_format}", params={"redact": redact}
                )
                assert response.status_code == 200, (report_type, output_format, redact)


def test_lone_surrogates_are_escaped_not_replaced():
    item = alert(explanation="name \ud800", evidence={"key \ud800": ["value \ud800"]})
    assert item.explanation == "name \\ud800"
    assert item.evidence == {"key \\ud800": ["value \\ud800"]}
    event = EventRecord(
        id="synthetic",
        timestamp=datetime.now(UTC),
        event_type="alert",
        title="title \ud800",
        summary="summary \ud800",
        evidence={"name": "\ud800"},
    )
    assert (event.title, event.summary) == ("title \\ud800", "summary \\ud800")
    assert event.evidence == {"name": "\\ud800"}


@pytest.mark.parametrize(
    "evidence, expected",
    [
        ({"value": object()}, {"value": MARKER}),
        ({"outer": {object(): 1}}, {"outer": MARKER}),
        ({"items": [1, object()]}, {"items": [1, MARKER]}),
    ],
)
def test_unrepresentable_evidence_is_marked_without_its_repr(evidence, expected):
    assert alert(evidence=evidence).evidence == expected


@pytest.mark.parametrize("factory", [circular, branching_cycle])
def test_cyclic_evidence_terminates_with_a_marker(factory):
    item = alert(evidence=factory())
    assert MARKER in json.dumps(item.evidence)
    assert len(json.dumps(item.evidence)) < 256


def test_excessively_nested_evidence_is_bounded():
    evidence = value = {}
    for _ in range(5000):
        value["next"] = {}
        value = value["next"]
    assert MARKER in json.dumps(alert(evidence=evidence).evidence)


def test_valid_alert_contract_is_unchanged(application):
    app, client = application
    service = app.state.telemetry
    moment = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    evidence = {
        "count": 1,
        "ratio": 0.5,
        "flag": True,
        "missing": None,
        "text": "caf\u00e9 \U0001f600",
        "items": [1, "two", {"three": 3}],
        "pair": (1, 2),
        "when": moment,
    }
    item = alert(evidence=evidence, explanation="valid caf\u00e9 \U0001f600")
    baseline = Alert.model_construct(**(item.model_dump() | {"evidence": evidence}))
    service._remember_live_alert(item)

    assert item.explanation == "valid caf\u00e9 \U0001f600"
    row = client.get("/api/alerts").json()[0]
    assert row == json.loads(baseline.model_dump_json())


def test_non_finite_evidence_keeps_its_existing_null_encoding(application):
    app, client = application
    app.state.telemetry._remember_live_alert(alert(evidence={"value": float("nan")}))
    assert client.get("/api/alerts").json()[0]["evidence"] == {"value": None}


def test_rows_stored_before_this_fix_are_served_after_restart(tmp_path):
    path = tmp_path / "synthetic.db"
    database = Database(path)
    database.initialize()
    stored = Alert.model_construct(**alert().model_dump())
    stored.evidence = {"name": f"{PRIVATE} \ud800"}
    database.add_alert(stored, 100)
    database.add_event(
        EventRecord.model_construct(
            id="synthetic-event",
            timestamp=datetime.now(UTC),
            event_type="alert",
            severity="LOW",
            title="synthetic",
            summary="synthetic",
            related_id=None,
            evidence={"name": f"{PRIVATE} \ud800"},
        )
    )

    app = create_app(path, start_background=False)
    with TestClient(app) as client:
        assert client.get("/api/alerts").json()[0]["evidence"] == {
            "name": f"{PRIVATE} \\ud800"
        }
        events = {row["id"]: row for row in client.get("/api/events").json()}
        assert events["synthetic-event"]["evidence"] == {"name": f"{PRIVATE} \\ud800"}


async def test_malformed_alert_does_not_abort_collection(
    tmp_path, system_sample, network_sample
):
    class Static:
        def __init__(self, value):
            self.value = value

        def collect(self, *args, **kwargs):
            return self.value

    class Detector:
        alerts = []

        def evaluate(self, *args, **kwargs):
            return self.alerts

    service = TelemetryService(Database(tmp_path / "synthetic.db"))
    assert await service.initialize_storage()
    service.system_collector = Static(system_sample)
    service.network_collector = Static((network_sample, []))
    service.process_collector = Static([])
    service.detection_engine = Detector()
    await service.collect_once()

    service.detection_engine.alerts = [alert(explanation="malformed \ud800")]
    for sequence in (2, 3):
        snapshot = await service.collect_once()
        assert snapshot.sequence == sequence
        assert service.encode_snapshot(snapshot) is not None
    assert service.storage.status == "healthy"
    assert not service._pending_alerts
    assert [item.id for item in service.database.list_alerts(10, True)] == ["synthetic-alert"]


@pytest.mark.parametrize(
    "path",
    ["/api/alerts", "/api/events", "/api/reports/security-alerts.json"],
)
@pytest.mark.parametrize("bad_value", [object(), f"{PRIVATE} \ud800"])
def test_residual_failures_are_sanitized_and_recover(
    application, snapshot, caplog, path, bad_value
):
    app, client = application
    service = app.state.telemetry
    item = alert()
    event = service._alert_event(item)
    service._remember_live_alert(item)
    service._remember_event(event)
    service.latest = snapshot.model_copy(update={"alerts": [item]})
    # Mutation after construction is the one route around model validation.
    item.evidence["value"] = bad_value
    event.evidence["value"] = bad_value

    with caplog.at_level(logging.DEBUG):
        response = client.get(path)

    if path.endswith(".json") and isinstance(bad_value, str):
        # Report rendering is ASCII-escaped JSON and has always handled this shape.
        assert response.status_code == 200
    else:
        assert response.status_code == 503
        assert response.json() == UNAVAILABLE
        assert response.headers["content-type"] == "application/json"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert PRIVATE not in response.text
        assert PRIVATE not in caplog.text

    del item.evidence["value"]
    del event.evidence["value"]
    assert client.get(path).status_code == 200


def test_unencodable_user_input_gets_a_sanitized_error(application):
    _, client = application
    escaped_title = PRIVATE + chr(92) + "ud800"
    response = client.post(
        "/api/incidents",
        content=f'{{"title":"{escaped_title}","severity":"INFO"}}'.encode(),
        headers=LOCAL_ORIGIN | {"content-type": "application/json"},
    )
    assert response.status_code == 503
    assert response.json() == UNAVAILABLE
    assert PRIVATE not in response.text


def test_redaction_still_applies_to_sanitized_evidence(application, snapshot):
    app, client = application
    service = app.state.telemetry
    item = alert(
        evidence={
            "path": f"C:\\synthetic\\{PRIVATE}\ud800",
            "username": f"{PRIVATE}\ud800",
            "value": object(),
        }
    )
    service._remember_live_alert(item)
    service.latest = snapshot.model_copy(update={"alerts": [item]})

    redacted = client.get("/api/reports/security-alerts.json").text
    assert PRIVATE not in redacted
    assert MARKER in redacted
    assert "[REDACTED]" in redacted


def test_ai_context_does_not_receive_object_representations(application, snapshot):
    app, client = application
    service = app.state.telemetry
    prompts = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, *args):
            answer = dict.fromkeys(
                (
                    "observed_evidence",
                    "possible_explanations",
                    "missing_information",
                    "recommended_checks",
                ),
                "synthetic",
            )
            return json.dumps({"response": json.dumps(answer)}).encode()

        def geturl(self):
            return "http://127.0.0.1:11434/api/generate"

    def opener(request, timeout):
        prompts.append(json.loads(request.data)["prompt"])
        return Response()

    app.state.ai_analyst = LocalAIAnalyst(opener=opener)
    service.ai_settings = AISettings(enabled=True)
    item = alert(explanation=f"{PRIVATE} \ud800", evidence={"value": object()})
    service.latest = snapshot.model_copy(update={"alerts": [item]})

    response = client.post(
        "/api/ai/analyze", json={"question": "synthetic"}, headers=LOCAL_ORIGIN
    )
    assert response.status_code == 200
    assert "<object" not in prompts[0]


def test_host_and_origin_checks_hold_with_malformed_alerts(application):
    app, client = application
    item = MALFORMED["text"]()
    app.state.telemetry._remember_live_alert(item)

    assert client.get("/api/alerts", headers={"host": "attacker.example"}).status_code == 400
    forbidden = client.patch(
        f"/api/alerts/{item.id}",
        json={"acknowledged": True},
        headers={"origin": "https://example.com"},
    )
    assert forbidden.status_code == 403
    assert not next(row for row in app.state.telemetry.live_alerts).acknowledged


def test_event_logging_opt_out_still_applies_to_sanitized_alerts(application, snapshot):
    app, client = application
    service = app.state.telemetry
    service.settings = Settings(event_logging_enabled=False)
    item = MALFORMED["text"]()
    service._remember_live_alert(item)
    assert client.get("/api/alerts").status_code == 200
    assert service.database.list_alerts(10, True) == []
