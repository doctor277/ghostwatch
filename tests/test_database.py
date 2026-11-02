from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from app.models import Alert, AlertUpdate, Settings
from app.storage.database import Database


def test_settings_round_trip(tmp_path):
    database = Database(tmp_path / "test.db")
    database.initialize()
    settings = Settings(refresh_interval=5, cpu_threshold=75)
    database.save_settings(settings)
    assert database.load_settings() == settings


def test_alert_crud_and_retention(tmp_path):
    database = Database(tmp_path / "test.db")
    database.initialize()
    for index in range(3):
        alert = Alert(
            id=str(index),
            timestamp=datetime.now(UTC),
            severity="INFO",
            source="test",
            metric=f"m{index}",
            explanation="safe",
        )
        database.add_alert(alert, retention=2)
    alerts = database.list_alerts(limit=10)
    assert len(alerts) == 2
    updated = database.update_alert(alerts[0].id, AlertUpdate(acknowledged=True))
    assert updated is not None and updated.acknowledged
    database.update_alert(alerts[0].id, AlertUpdate(dismissed=True))
    assert all(item.id != alerts[0].id for item in database.list_alerts())
    assert any(item.id == alerts[0].id for item in database.list_alerts(include_dismissed=True))


def test_missing_alert_returns_none(tmp_path):
    database = Database(tmp_path / "test.db")
    database.initialize()
    assert database.update_alert("missing", AlertUpdate(acknowledged=True)) is None


def test_retention_can_be_enforced_immediately(tmp_path):
    database = Database(tmp_path / "test.db")
    database.initialize()
    for index in range(5):
        database.add_alert(
            Alert(
                id=str(index),
                timestamp=datetime.now(UTC),
                severity="INFO",
                source="test",
                metric=f"m{index}",
                explanation="safe",
            ),
            retention=5,
        )
    database.enforce_retention(2)
    assert len(database.list_alerts(limit=10)) == 2


def test_invalid_settings_recovery_is_logged_without_payload(tmp_path, caplog):
    path = tmp_path / "test.db"
    database = Database(path)
    database.initialize()
    with sqlite3.connect(path) as connection:
        connection.execute("INSERT INTO settings (id, payload) VALUES (?, ?)", (1, "secret-bad"))
    settings = database.load_settings()
    assert settings == Settings()
    assert "safe defaults were restored" in caplog.text
    assert "secret-bad" not in caplog.text


def test_backup_is_a_consistent_sqlite_snapshot(tmp_path):
    database = Database(tmp_path / "live.db")
    database.initialize()
    database.save_settings(Settings(refresh_interval=7))

    backup_path = database.backup(tmp_path / "backups" / "snapshot.db")
    backup = Database(backup_path)
    assert backup.load_settings().refresh_interval == 7
    with sqlite3.connect(backup_path) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_collection_failures_are_grouped_and_reopen_after_recovery(tmp_path):
    database = Database(tmp_path / "grouped.db")
    database.initialize()
    now = datetime.now(UTC)
    alert_id = "stable-collection-failure"

    active = Alert(
        id=alert_id,
        timestamp=now,
        severity="INFO",
        source="Collector",
        metric="Telemetry collection degraded",
        explanation="Synthetic failure",
        rule_id="collection_failure",
        first_occurrence=now,
        last_occurrence=now,
        occurrence_count=1,
        condition_active=True,
    )
    database.add_alert(active, retention=100)
    database.add_alert(active, retention=100)
    database.update_alert(alert_id, AlertUpdate(acknowledged=True, dismissed=True))
    recovery = active.model_copy(
        update={
            "timestamp": now,
            "metric": "Telemetry collection recovered",
            "occurrence_count": 0,
            "condition_active": False,
            "last_recovery": now,
        }
    )
    recovered = database.add_alert(recovery, retention=100)
    reopened = database.add_alert(active, retention=100)

    assert recovered.occurrence_count == 2
    assert recovered.condition_active is False
    assert reopened.occurrence_count == 3
    assert reopened.condition_active is True
    assert not reopened.acknowledged
    assert not reopened.dismissed
