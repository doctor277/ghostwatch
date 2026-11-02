# Ghostwatch V2 Test Results

Verification date: 2026-10-21 (America/Toronto). Commands were executed from the project root with the project's Python 3.14.3 virtual environment. The package declares Python >= 3.11; not every supported interpreter version was run.

## Automated regression suite

Command:

```powershell
.\.venv\Scripts\python.exe -m pytest -q --basetemp <writable-temp>\ghostwatch-v2-final-pytest
```

Actual output:

```text
..........................................................               [100%]
============================== warnings summary ===============================
...\site-packages\fastapi\testclient.py:1
  StarletteDeprecationWarning: Using `httpx` with `starlette.testclient` is deprecated; install `httpx2` instead.
    from starlette.testclient import TestClient as TestClient  # noqa

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
58 passed, 1 warning in 7.16s
```

Result: 58 passed, 0 failed, 1 warning in the synchronized target directory. The warning is upstream test-client compatibility guidance and was not hidden. A separate pre-sync run also passed all 58 tests in 7.40 seconds.

## Static and syntax checks

```text
$ .\.venv\Scripts\python.exe -m ruff check .
All checks passed!

$ .\.venv\Scripts\python.exe -m compileall -q app
compileall: PASS

$ node --check frontend\js\dashboard.js
$ node --check frontend\js\charts.js
$ node --check frontend\js\demo.js
$ node --check frontend\js\network-graph.js
$ node --check frontend\js\websocket.js
JavaScript syntax: PASS (5 files)

$ PowerShell parser check for start.ps1
PowerShell syntax: PASS

$ .\.venv\Scripts\python.exe -m pip check
No broken requirements found.

$ .\.venv\Scripts\python.exe -m pip_audit -r requirements.txt
No known vulnerabilities found
```

## Live security/runtime probes

The supported server command was used with `--host 127.0.0.1 --port 8765`. The final target run observed `127.0.0.1:8765`, state `Listen`, PID 37312; no `0.0.0.0:8765` or `[::]:8765` listener was observed.

HTTP probes:

```text
GET /api/health with valid Host -> 200
GET /api/health with Host: attacker.example -> 400
PUT /api/settings with Origin: http://attacker.example -> 403
GET /api/reports/system-health.html -> 200
```

Live WebSocket probes:

```text
ACCEPTED origin=http://127.0.0.1:8765 type=telemetry sequence=22
REJECTED origin=http://localhost:4444 status=403
REJECTED origin=https://attacker.example status=403
REJECTED origin=None status=403
```

## Browser verification

Automated browser interaction exercised all twelve navigation destinations, network search, process search/sorting/detail drawer, alert acknowledgement/dismissal, incident creation/update, timeline search/empty state, AI Demo output, settings persistence, report selection, graph selection, mobile navigation, and WebSocket reconnect after a server restart.

Lighthouse mobile snapshot (390 x 844): Accessibility 100, Best Practices 100, SEO 100, Agentic 100; 29 checks passed, 0 failed. The current-page console was clean after reload. This is a smoke/accessibility snapshot, not a complete conformance certification.

## Coverage notes

Regression tests specifically cover the >250 connection counting defect, logging-enabled/disabled live alerts, Host/Origin rejection, collector failures, WebSocket client/queue/send bounds, immediate retention enforcement, corrupted settings recovery, stale frontend counters, empty-table column spans, database backup integrity, report escaping/redaction, and bounded default process sampling.
