# Ghostwatch V3 Verification Evidence

Status: release-candidate verification completed in the isolated V3 staging directory on 2026-10-22. The production V2 target was not modified. This is engineering verification, not a penetration test.

## Phase boundaries

### Phase A

Commands:

    python -m ruff check app tests scripts
    python -m pytest tests/test_v3_history.py -q
    node --check frontend/js/charts.js
    node --check frontend/js/dashboard.js
    python -m pytest -q

Actual results:

    All checks passed!
    8 passed, 1 warning in 2.46s
    JavaScript checks exited 0
    86 passed, 1 warning in 8.07s

### Phase B

Commands:

    python -m pytest tests/test_v3_windows_events.py -q
    python -c <bounded WindowsEventCollector call with max_events_per_channel=1>
    python -m pytest -q

Actual results:

    6 passed, 1 warning in 1.48s
    [('Microsoft-Windows-Windows Defender/Operational', 'healthy', 2827),
     ('System', 'healthy', 32449),
     ('Application', 'healthy', 23905)]
    normalized_events 3
    categories ['application', 'system']
    92 passed, 1 warning in 8.83s

No event content was printed during the live bounded check.

### Phase C

Commands:

    python -m pytest tests/test_v3_file_integrity.py -q
    node --check frontend/js/dashboard.js
    python -m pytest -q

Actual results:

    5 passed, 1 warning in 1.77s
    JavaScript check exited 0
    97 passed, 1 warning in 9.94s

### Phase D

Commands:

    npm install --save-dev @playwright/test
    npx playwright install chromium
    npm run test:e2e
    npm audit --audit-level=high

Actual results:

    added 3 packages, audited 4 packages, found 0 vulnerabilities
    Playwright managed Chromium 156.0.8078.4 installed
    12 passed (26.9s)
    found 0 vulnerabilities

The first browser pass was 8 passed/4 failed and revealed three test issues plus one application form bug. Enabling console/page-error gates then revealed the same async-lifecycle issue in incident creation. Both application bugs were fixed and focused reruns passed before the full 12-test gate.

### Phase E

Commands:

    python -m pytest tests/test_v3_hardening.py -q
    python -m scripts.benchmark_endurance
    python -c "from scripts.benchmark_endurance import main; main(size=10000, repeats=100)"
    python -m ruff check .
    python -m pytest -q
    python -m compileall -q app scripts tests
    node --check <every frontend/js/*.js>
    npm audit --audit-level=high
    npm run test:e2e

Actual focused result:

    8 passed in 1.39s

Actual optimized 20-cycle benchmark:

    sockets=10000
    repeats=20
    latency_min_ms=265.37
    latency_median_ms=366.24
    latency_p95_ms=442.61
    latency_max_ms=442.61
    traced_current_bytes=3429201
    traced_peak_bytes=8549388
    traced_current_growth_bytes=155224
    lifecycle_entries=10000
    recently_closed_entries=100
    detail_entries=250

Actual 100-cycle bounded-memory follow-up:

    sockets=10000
    repeats=100
    latency_min_ms=250.46
    latency_median_ms=386.06
    latency_p95_ms=454.37
    latency_max_ms=467.86
    traced_current_bytes=3451208
    traced_peak_bytes=8559035
    traced_current_growth_bytes=174459
    lifecycle_entries=10000
    recently_closed_entries=100
    detail_entries=250

Actual final gate:

    All checks passed!
    105 passed, 1 warning in 10.09s
    compileall exited 0
    all frontend JavaScript syntax checks exited 0
    found 0 vulnerabilities
    12 Playwright tests passed in 26.6s

Actual listener check:

    LocalAddress  : 127.0.0.1
    LocalPort     : 8765
    State         : Listen
    health_status=200
    health scope=local-only, schema_version=3
    listener_stopped=true

## Warning retained

One upstream warning appears in TestClient-backed Python runs:

    StarletteDeprecationWarning: Using `httpx` with `starlette.testclient` is
    deprecated; install `httpx2` instead.

It was not hidden or suppressed. Playwright's Node runner also noted that `NO_COLOR` was ignored because `FORCE_COLOR` was set; this was a runner environment notice, not a page-console warning.
