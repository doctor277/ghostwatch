# Independent Audit Remediation

Prepared: 2026-10-21  
Project: Ghostwatch  
Scope: remediation of the supplied independent source-review findings

This document records engineering remediation and verification. It is not a penetration test,
security certification, or claim that every defect has been found.

## Outcome summary

- Full connection counts now drive established-socket detection and per-process totals while the UI
  detail payload remains capped at 250 rows.
- New alerts remain visible in bounded process memory even when SQLite event logging is disabled.
- HTTP Host and WebSocket Origin enforcement now require the intended loopback names and port 8765.
- Collector failures produce degraded telemetry and informational alerts without exposing exception
  messages.
- WebSocket clients and sends are bounded.
- Lower SQLite retention is enforced immediately.
- Frontend empty states, alert counters, mobile navigation state, and form semantics were corrected.
- The Windows launcher validates Python 3.11+, caches dependency-install state, detects an occupied
  port, waits for readiness, and reports startup/browser failures more clearly.

## File-by-file changes

### Backend and security

- `app/config.py`
  - Added fixed Host/Origin allowlists for HTTP loopback port 8765.
  - Added bounds for live alerts and concurrent WebSocket clients.
  - Centralized the WebSocket send timeout.
- `app/main.py`
  - Added a pure ASGI Host-checking middleware that rejects non-allowlisted HTTP requests with 400.
  - Allows `testserver` only for the explicit `start_background=False` test factory mode.
- `app/api/websocket.py`
  - Requires both an allowlisted Host header and an exact allowlisted Origin.
  - Rejects missing, wrong-scheme, wrong-port, path-bearing, credential-bearing, and remote origins.
  - Enforces the client limit before acceptance and uses the common timeout for the initial snapshot.
- `app/api/routes.py`
  - Routes alert reads and updates through the telemetry service so in-memory and persistent states
    remain consistent.
- `app/models.py`
  - Added an optional, sanitized system collection-error field to degraded snapshots.
- `app/service.py`
  - Added a bounded 200-alert live deque and ID-based merging with SQLite alerts.
  - Added consistent acknowledge/dismiss handling for live-only and persisted alerts.
  - Added collector fallbacks that keep snapshots and collection-failure rules running.
  - Passes full per-PID socket totals into process collection.
  - Added a 32-client WebSocket cap, registration lock, initial/broadcast send timeouts, and failed
    client closure.
  - Enforces SQLite retention immediately when settings are updated.
- `app/monitoring/network.py`
  - Counts established TCP sockets and per-PID connections across the full psutil result.
  - Still builds at most 250 connection detail rows.
  - Converts counter and connection-enumeration failures into sanitized degraded telemetry.
- `app/monitoring/processes.py`
  - Accepts full per-PID socket counts while retaining compatibility with the detail-list fallback.
- `app/monitoring/system.py`
  - Handles CPU, memory, disk, PID-list, and boot-time failures independently and returns a partial
    snapshot with sanitized error indicators.
- `app/detection/rules.py`
  - Collection-failure alerts now cover both system and network failures.
- `app/storage/database.py`
  - Added immediate reusable retention enforcement.
  - Logs a generic warning when corrupt settings are replaced by safe defaults; stored content is not
    included in the log.

### Frontend

- `frontend/js/dashboard.js`
  - Resets the alert counter even when the active list becomes empty.
  - Hides dismissed alerts immediately.
  - Uses correct 6/8-column connection empty states and adds correct process-table empty states.
  - Makes process rows keyboard operable and restores focus to the current PID row after closing the
    details drawer, even when live telemetry has replaced the original row element.
  - Keeps mobile-menu `aria-expanded` synchronized and surfaces system collection degradation.
- `frontend/index.html`
  - Added mobile menu relationship/state attributes.
  - Replaced three non-form `label` elements that triggered accessibility findings.
  - Clarified that history can contain live session alerts as well as SQLite events.
- `frontend/css/styles.css`
  - Updated performance-bar selectors for the corrected non-form markup.
- `frontend/js/websocket.js` and `frontend/js/charts.js`
  - No implementation change; both were syntax-checked and exercised in browser smoke testing.

### Windows launcher and documentation

- `start.ps1`
  - Parses and enforces Python 3.11+ for both the system interpreter and existing virtual environment.
  - Installs dependencies only when the SHA256 of `requirements.txt` differs from the venv stamp.
  - Detects an occupied loopback port before startup.
  - Starts Uvicorn as a tracked child, waits up to 15 seconds for health, reports early exit/readiness
    failure, warns on browser-launch failure, and cleans up the child process.
  - Continues to bind Uvicorn explicitly to `127.0.0.1:8765`.
- `README.md`
  - Documents exact local Host/Origin controls and the local-process trust boundary.
  - Documents dependency installation caching.

### Regression tests

- `tests/test_monitoring.py`
  - Covers 300 established sockets, the 250-row detail limit, full detection input, full per-process
    counts, failed network counters, and failed system collection.
- `tests/test_service.py`
  - Covers logging-disabled live alerts, logging-enabled persistence/deduplication, acknowledge and
    dismiss behavior, unexpected collector failure alerts, WebSocket cap/timeout, prompt retention,
    and bounded live-alert memory.
- `tests/test_api.py`
  - Covers valid and invalid HTTP Host values.
- `tests/test_websocket.py`
  - Covers exact allowed origins, wrong port/scheme/path, arbitrary Host rejection, and client limit.
- `tests/test_database.py`
  - Covers immediate pruning and privacy-safe corrupt-settings recovery logging.

## Local API trust model

The supported deployment is a single-user desktop process bound to `127.0.0.1:8765`. Browser
requests must use an allowlisted Host and WebSocket requests must additionally carry an exact HTTP
loopback Origin for port 8765. Normal access through `http://127.0.0.1:8765` is preserved.

There is intentionally no authentication between operating-system processes on the same machine.
Any local process able to connect to loopback is inside the API trust boundary and can read telemetry
or change settings. Host/Origin validation protects the browser boundary; it does not authenticate
local software. Ghostwatch must not be rebound to a non-loopback interface or exposed through a
network-accessible reverse proxy without adding authentication and transport security.

## Verification results

Final verification was executed against the updated project, not only the staging copy. Exact raw
outputs are retained under `docs/audit-evidence/`.

- Pytest: `36 passed, 1 warning in 1.68s`.
- Warning: Starlette's installed test client uses the deprecated AnyIO `BlockingPortal` alias.
- Ruff: passed with no findings.
- `compileall app`: passed.
- JavaScript syntax: all three frontend modules passed `node --check`.
- PowerShell syntax: `start.ps1` parsed without errors.
- HTTP security: intended Host returned 200; arbitrary Host and wrong loopback port returned 400.
- WebSocket security: valid origin worked; wrong port, wrong Host, and remote origin were rejected;
  client-cap and slow-send behavior have regression coverage.
- Runtime: health returned local-only status and the only listener on port 8765 was
  `127.0.0.1:8765`.
- Browser: Chromium-based in-app Playwright smoke checks covered all seven sections, searching,
  sorting, keyboard drawer operation and focus restoration, settings persistence, mobile menu state,
  empty table spans, alert action control availability, form-label accessibility, and a real
  `RECONNECTING` to `CONNECTED` WebSocket recovery after a server restart.
- Browser console: no errors or warnings were observed during the seven-section navigation pass.
- Launcher: a second launch against occupied port 8765 exited with a clear error; dependency install
  was skipped because the requirements hash matched.
- Environment note: a sandboxed rerun stalled because its policy blocked Python's local event-loop
  socketpair. A faulthandler trace identified `socket._fallback_socketpair`; the same exact suite was
  then run outside that network restriction and passed. The diagnostic is preserved separately.

## Remaining limitations

- Local operating-system processes remain trusted; there is no per-process authentication.
- The WebSocket client cap is a code configuration constant rather than a user-facing setting.
- Non-persisted alerts intentionally disappear when the application process exits.
- A launcher port preflight cannot eliminate the small race between the availability check and
  Uvicorn binding; an intervening bind is reported as startup failure.
- The dependency stamp detects `requirements.txt` changes, not manual mutation of installed packages.
- Browser testing covers Chromium on representative desktop/mobile viewports, not every browser,
  DPI, Windows release, accessibility technology, or long-duration reconnect scenario.
- Alert acknowledge/dismiss state transitions are regression-tested through the application API and
  service, and their browser controls were rendered in the smoke test. The smoke run did not mutate
  the user's existing live alerts. There is no dedicated frontend unit-test harness yet for the
  stale-zero counter branch or button callbacks.
- Performance testing covers deterministic large-socket inputs, not a sustained production soak or
  hostile local denial-of-service campaign.
- The upstream Starlette/AnyIO deprecation warning remains until the dependency stack removes the
  deprecated alias.

## Evidence files

- `docs/audit-evidence/pytest.txt`
- `docs/audit-evidence/ruff.txt`
- `docs/audit-evidence/compileall.txt`
- `docs/audit-evidence/javascript-syntax.txt`
- `docs/audit-evidence/powershell-syntax.txt`
- `docs/audit-evidence/http-security.txt`
- `docs/audit-evidence/runtime-listener.txt`
- `docs/audit-evidence/browser-smoke.txt`
- `docs/audit-evidence/websocket-live.txt`
- `docs/audit-evidence/launcher.txt`
- `docs/audit-evidence/pytest-sandbox-diagnostic.txt`
- `docs/audit-evidence/archive-scan.txt`

