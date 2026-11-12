from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.models import GROUPED_ALERT_RULES, Alert, AlertUpdate
from app.storage.database import Database

START = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)
RETENTION = 50


def episode(at: datetime, *, count: int, active: bool = True) -> Alert:
    """A memory-threshold episode observation delta, as the service writes it."""
    return Alert(
        id="episode-memory-1",
        timestamp=at,
        severity="CRITICAL",
        source="Detection",
        metric="Memory utilization",
        explanation="Memory above threshold",
        rule_id="memory",
        first_occurrence=START,
        last_occurrence=at,
        occurrence_count=count,
        condition_active=active,
    )


def event_alert(index: int, at: datetime) -> Alert:
    return Alert(
        id=f"windows-alert-{index}",
        timestamp=at,
        severity="WARNING",
        source="Windows Events",
        metric="Service failure",
        explanation="Synthetic service failure",
        rule_id="windows_service_failure",
    )


def stored(database: Database, alert_id: str) -> Alert | None:
    return next(
        (item for item in database.list_alerts(10_000, include_dismissed=True) if item.id == alert_id),
        None,
    )


def test_retention_keeps_a_still_active_episode(tmp_path):
    database = Database(tmp_path / "retention.db")
    database.initialize()
    database.add_alert(episode(START, count=3), RETENTION)
    database.update_alert("episode-memory-1", AlertUpdate(acknowledged=True))

    # Before the episode's next report, a burst of newer independent alerts arrives.
    for index in range(RETENTION + 10):
        database.add_alert(event_alert(index, START + timedelta(seconds=1 + index / 100)), RETENTION)
    kept = stored(database, "episode-memory-1")
    assert kept is not None and kept.occurrence_count == 3 and kept.acknowledged

    # The next report merges into the complete record instead of recreating a partial one.
    database.add_alert(episode(START + timedelta(seconds=60), count=1), RETENTION)
    merged = stored(database, "episode-memory-1")
    assert merged.occurrence_count == 4
    assert merged.first_occurrence == START
    assert merged.acknowledged
    # Retention stays bounded; the episode is now among the newest rows.
    assert len(database.list_alerts(10_000, include_dismissed=True)) == RETENTION


def test_retention_still_removes_a_recovered_episode(tmp_path):
    database = Database(tmp_path / "recovered.db")
    database.initialize()
    database.add_alert(episode(START, count=3, active=False), RETENTION)

    for index in range(RETENTION):
        database.add_alert(event_alert(index, START + timedelta(seconds=1 + index)), RETENTION)

    assert stored(database, "episode-memory-1") is None
    assert len(database.list_alerts(10_000, include_dismissed=True)) == RETENTION


def test_open_episode_exemption_is_bounded_by_the_grouped_rules(tmp_path):
    database = Database(tmp_path / "bounded.db")
    database.initialize()
    # Far more rows claim to be open than there are grouped rules (e.g. legacy rows).
    for index in range(30):
        database.add_alert(
            episode(START - timedelta(hours=1, seconds=index), count=1).model_copy(
                update={"id": f"stale-open-{index}"}
            ),
            RETENTION,
        )
    for index in range(RETENTION + 20):
        database.add_alert(event_alert(index, START + timedelta(seconds=index)), RETENTION)

    rows = database.list_alerts(10_000, include_dismissed=True)
    assert len(rows) == RETENTION + len(GROUPED_ALERT_RULES)
    # The newest open rows are the ones kept.
    kept_open = sorted(int(row.id.rsplit("-", 1)[1]) for row in rows if row.id.startswith("stale-open"))
    assert kept_open == list(range(len(GROUPED_ALERT_RULES)))
