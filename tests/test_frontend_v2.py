from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app

FRONTEND = Path(__file__).resolve().parents[1] / "frontend"


def test_all_v2_dashboard_sections_are_present_and_navigable():
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    sections = {
        "overview",
        "network",
        "graph",
        "processes",
        "tree",
        "alerts",
        "incidents",
        "history",
        "ai",
        "performance",
        "reports",
        "settings",
    }
    for section in sections:
        assert f'data-view="{section}"' in html
        assert f'data-view-panel="{section}"' in html


def test_frontend_uses_safe_dom_construction_and_local_modules():
    sources = [
        (FRONTEND / "js" / name).read_text(encoding="utf-8")
        for name in ("dashboard.js", "network-graph.js", "demo.js")
    ]
    combined = "\n".join(sources)
    assert "innerHTML" not in combined
    assert "eval(" not in combined
    assert "https://" not in combined
    assert "http://" not in combined


def test_frontend_assets_and_csp_are_compatible(tmp_path):
    app = create_app(tmp_path / "frontend.db", start_background=False)
    with TestClient(app) as client:
        page = client.get("/")
        script = client.get("/assets/js/dashboard.js")
        assert page.status_code == 200
        assert script.status_code == 200
        assert '<script type="module" src="/assets/js/dashboard.js"></script>' in page.text
        assert "<script>" not in page.text
        assert "script-src 'self'" in page.headers["content-security-policy"]
        assert "ws://[::1]" not in page.headers["content-security-policy"]


def test_forms_have_labels_and_demo_mode_is_explicit():
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    assert 'for="ai-question"' in html
    assert 'aria-label="Search graph nodes"' in html
    assert 'aria-label="Search process tree"' in html
    assert "DEMO MODE · SYNTHETIC DATA · NOT SAVED" in html


def test_process_sort_controls_are_keyboard_accessible():
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    script = (FRONTEND / "js" / "dashboard.js").read_text(encoding="utf-8")
    assert '<button class="sort-button" data-sort="cpu_percent" type="button">CPU</button>' in html
    assert '.sort-button[data-sort]' in script
    assert 'setAttribute("aria-sort"' in script
