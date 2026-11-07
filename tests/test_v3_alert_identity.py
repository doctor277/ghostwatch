from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.models import Alert, AlertUpdate
from app.service import TelemetryService
from app.storage.database import Database

LOCAL_ORIGIN = {"origin": "http://127.0.0.1:8765"}
LONG_ID = "legacy-" + "x" * 293
LONG_INCIDENT = "legacy-incident-" + "y" * 284


def alert(**changes):
    values = {
        "id": "synthetic-alert",
        "timestamp": datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
        "severity": "WARNING",
        "source": "synthetic-source",
        "metric": "synthetic-metric",
        "explanation": "synthetic",
    }
    return Alert(**(values | changes))


def legacy_row(database, **changes):
    """Store identifiers the way an earlier build could, without the model bound."""
    stored = alert().model_copy(update={"id": LONG_ID} | changes)
    database.add_alert(stored, 100)
    return stored


def test_stored_long_identifiers_are_listed_as_stored(tmp_path):
    database = Database(tmp_path / "synthetic.db")
    database.initialize()
    legacy_row(database, incident_id=LONG_INCIDENT)
    item = database.list_alerts(10, True)[0]
    assert item.id == LONG_ID
    assert item.incident_id == LONG_INCIDENT


@pytest.mark.parametrize("change", ["acknowledged", "dismissed"])
def test_stored_long_identifier_can_be_updated_by_its_listed_id(tmp_path, change):
    database = Database(tmp_path / "synthetic.db")
    database.initialize()
    legacy_row(database)
    listed = database.list_alerts(10, True)[0]
    updated = database.update_alert(listed.id, AlertUpdate(**{change: True}))
    assert updated is not None
    assert getattr(database.get_alert(listed.id), change) is True


def test_stored_long_identifier_stays_consistent_over_the_api(tmp_path):
    app = create_app(tmp_path / "synthetic.db", start_background=False)
    with TestClient(app) as client:
        legacy_row(app.state.telemetry.database)
        listed = client.get("/api/alerts").json()
        assert [item["id"] for item in listed] == [LONG_ID]
        patched = client.patch(
            f"/api/alerts/{LONG_ID}", json={"acknowledged": True}, headers=LOCAL_ORIGIN
        )
        assert patched.status_code == 200
        assert patched.json()["id"] == LONG_ID
        assert patched.json()["acknowledged"] is True
        dismissed = client.patch(
            f"/api/alerts/{LONG_ID}", json={"dismissed": True}, headers=LOCAL_ORIGIN
        )
        assert dismissed.json()["dismissed"] is True


def test_incident_links_a_stored_long_identifier(tmp_path):
    app = create_app(tmp_path / "synthetic.db", start_background=False)
    with TestClient(app) as client:
        database = app.state.telemetry.database
        legacy_row(database)
        created = client.post(
            "/api/incidents",
            json={"title": "Synthetic", "severity": "WARNING", "alert_ids": [LONG_ID]},
            headers=LOCAL_ORIGIN,
        )
        assert created.status_code == 201
        assert created.json()["related_alerts"] == [LONG_ID]
        assert database.get_alert(LONG_ID).incident_id == created.json()["id"]


async def test_live_alert_with_a_long_identifier_can_be_updated(tmp_path):
    service = TelemetryService(Database(tmp_path / "synthetic.db"))
    assert await service.initialize_storage()
    service.live_alerts.append(legacy_row(service.database))
    updated = await service.update_alert(LONG_ID, AlertUpdate(acknowledged=True))
    assert updated is not None and updated.id == LONG_ID
    assert updated.acknowledged is True


def test_event_for_a_stored_long_identifier_stays_within_its_contract():
    stored = alert().model_copy(update={"id": LONG_ID})
    related_id = TelemetryService._alert_event(stored).related_id
    assert related_id == alert(id=LONG_ID).id


def test_new_long_identifiers_are_still_digested_consistently(tmp_path):
    database = Database(tmp_path / "synthetic.db")
    database.initialize()
    item = alert(id=LONG_ID)
    assert item.id.startswith("truncated-sha256-")
    database.add_alert(item, 100)
    assert database.list_alerts(10, True)[0].id == item.id
    assert database.update_alert(item.id, AlertUpdate(acknowledged=True)) is not None
