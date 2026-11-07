from __future__ import annotations

import sys

from app.storage.database import Database
from scripts import benchmark_persistence as benchmark


def test_archive_cases_reach_the_lookup_they_are_named_for(tmp_path):
    database = Database(tmp_path / "synthetic.db")
    database.initialize()
    benchmark.seed_archive(database.path, 5, 2)

    links = database.get_alert_incident_ids(
        [
            benchmark.alert_id(4, 1),
            benchmark.alert_id(0, 0),
            benchmark.linked_id(0),
            benchmark.row_id(0),
            benchmark.miss_id(0),
        ]
    )

    assert list(links.values()) == ["INC-00000004", "INC-00000000", "INC-00000000", None, None]


def test_archive_report_is_complete_and_leaves_the_archive_valid(tmp_path):
    report = benchmark.run(
        tmp_path, archives=((5, 2),), samples=benchmark.MIN_SAMPLES, max_seconds=1,
        batch_sizes=(1, 3), outage=False,
    )

    archive = report["archives"][0]
    assert archive["integrity_check"] == "ok"
    assert "SCAN incidents" in archive["plans"]["_incident_for_alert (no stored link)"]
    assert any("INDEX" in step for step in archive["plans"]["add_incident link update"])
    assert set(archive["cases"]) == {
        "initialize",
        "add_incident/hit",
        "add_incident/miss",
        *(
            f"{operation}/{case}"
            for operation in (
                "add_alert", "get_alert_incident_ids[1]", "get_alert_incident_ids[3]"
            )
            for case in ("stored_link", "member_newest", "member_oldest", "miss")
        ),
    }
    for values in archive["cases"].values():
        assert values["samples"] == benchmark.MIN_SAMPLES
        assert values["p50_ms"] <= values["p95_ms"] <= values["max_ms"]


def test_outage_scenario_holds_its_invariants(tmp_path, monkeypatch):
    monkeypatch.setattr("app.service.MAX_PENDING_WRITES", 3)

    result = benchmark.run_outage(tmp_path, overflow=2)

    assert result["during_outage"]["status"] == "degraded"
    assert result["during_outage"]["pending_writes"] == 6
    assert result["during_outage"]["dropped_alert_writes"] == 2
    assert result["during_outage"]["dropped_event_writes"] == 2
    assert result["after_recovery"] == {
        "status": "healthy",
        "pending_writes": 0,
        "stored_alerts": 3,
        "stored_events": 3,
        "grouped_occurrences": 3,
    }
    assert result["after_opt_out"]["pending_writes"] == 0
    assert result["after_opt_out"]["dropped_alert_writes"] == 4
    assert result["after_opt_out"]["stored_after_opt_out"] == 0
    assert result["integrity_check"] == "ok"


def test_command_removes_its_disposable_directory(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        sys, "argv",
        ["benchmark", "--archive", "3:1", "--samples", "5", "--skip-outage",
         "--workdir", str(tmp_path)],
    )

    benchmark.main()

    output = capsys.readouterr().out
    assert output.index("integrity_check=ok") < output.index("total_seconds=")
    assert list(tmp_path.iterdir()) == []
