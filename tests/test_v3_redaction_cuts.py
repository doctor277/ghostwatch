from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.models import Alert
from app.privacy import redact_structure, redact_text

TRUNCATED = "[TRUNCATED]"


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


def cut_inside(prefix, secret):
    """Build text whose length limit falls inside the secret."""
    filler = "word " * ((1980 - len(prefix)) // 5)
    start = filler + prefix
    start += "w" * (1984 - len(start) - len(secret) // 2 - 1) + " "
    return alert(explanation=f"{start}{secret} and the rest of the sentence " * 2)


@pytest.mark.parametrize(
    "secret,fragment",
    [
        pytest.param("someone@example.com", "someone", id="email"),
        pytest.param("203.0.113.7", "203.0", id="ipv4"),
        pytest.param("2001:db8:1234:5678::1", "2001:db8", id="ipv6"),
        pytest.param("S-1-5-21-1111-2222-3333", "S-1-5-21", id="sid"),
        pytest.param("SYNTHETICDOMAIN\\someone", "SYNTHETICD", id="account"),
    ],
)
@pytest.mark.parametrize("level", ["standard", "strict"])
def test_value_cut_by_truncation_is_not_exposed_by_redaction(secret, fragment, level):
    item = cut_inside("value ", secret)
    assert item.explanation.endswith(TRUNCATED)
    assert fragment in item.explanation
    redacted = redact_structure({"explanation": item.explanation}, level)["explanation"]
    assert fragment not in redacted
    assert redacted.endswith(TRUNCATED)


def test_cut_label_is_not_exposed_by_redaction():
    item = alert(metric="m " * 90 + "someone@example.com" + " tail" * 10)
    assert "someone" in item.metric
    assert "someone" not in redact_text(item.metric)


def test_cut_value_inside_a_longer_string_is_redacted():
    assert "203.0" not in redact_text(f"note: 203.0 {TRUNCATED} | next column")


def test_text_before_the_cut_stays_readable():
    item = cut_inside("Readable context ", "someone@example.com")
    assert "Readable context" in redact_text(item.explanation)


def test_text_without_a_cut_is_not_changed_by_the_rule():
    assert redact_text("plain words stay") == "plain words stay"
    assert redact_text(TRUNCATED) == TRUNCATED


def test_redaction_of_a_long_run_without_spaces_is_linear():
    text = "x" * 200_000 + f" {TRUNCATED}"
    assert redact_text(text).endswith(TRUNCATED)


def test_cut_value_does_not_reach_a_redacted_report(tmp_path, snapshot):
    app = create_app(tmp_path / "synthetic.db", start_background=False)
    with TestClient(app) as client:
        service = app.state.telemetry
        item = cut_inside("value ", "someone@example.com")
        service._remember_live_alert(item)
        service.latest = snapshot.model_copy(update={"alerts": [item]})
        report = client.get("/api/reports/security-alerts.json?redact=true")
        assert report.status_code == 200
        assert TRUNCATED in report.text
        assert "someone" not in report.text
