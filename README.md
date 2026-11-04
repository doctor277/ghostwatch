# GhostWatch V3

**Status: local beta** | Windows 10/11 | Loopback-only dashboard

GhostWatch is a local-first Windows monitoring and investigation dashboard for exploring system health, processes, network connections, Windows events, file-integrity changes, and explainable detections. It combines a FastAPI backend, local SQLite history, and a dependency-free browser interface without sending host telemetry to a cloud service.

![GhostWatch V3 dashboard in Demo Mode using synthetic data](docs/browser-demo-network-graph.png)

> The screenshot shows the current V3 interface in Demo Mode. Its processes, addresses, alerts, and relationships are synthetic, remain in browser memory, and are not stored as host telemetry.

GhostWatch is a single-user learning and portfolio project, not an antivirus, EDR replacement, remote scanner, penetration-testing toolkit, or proof that a computer is uncompromised.

## What it demonstrates

- Bounded collection of Windows system, process, socket, and Event Log metadata
- Explainable threshold and baseline detections with local incident tracking
- Opt-in SHA-256 file-integrity baselines with path-safety controls
- Historical telemetry, retention, rollups, exports, and missing-data indicators
- A secured loopback REST/WebSocket boundary and privacy-redacted reports
- An isolated browser Demo Mode and synthetic Playwright regression suite
- Optional local-only Ollama analysis, disabled by default and never treated as a security verdict

## Technology

| Area | Implementation |
|---|---|
| Backend | Python 3.11+, FastAPI, Uvicorn, Pydantic, psutil |
| Storage | SQLite schema v3 with WAL, migrations, retention, and rollups |
| Frontend | HTML, CSS, and JavaScript with Canvas charts and WebSockets |
| Testing | pytest, Ruff, Playwright Chromium, and Node syntax checks |
| Platform | Windows 10/11, PowerShell launcher, loopback-only service |

## Architecture

```text
Windows collectors
  system | processes | sockets | event logs | enrolled folders
                         |
                         v
          monitoring + detection + baselines
                 |                 |
                 v                 v
       SQLite history       bounded live state
                 \                 /
                  v               v
             FastAPI REST + WebSocket
                         |
                         v
              browser dashboard
```

Collectors degrade independently when Windows permissions or providers limit access. `app/storage/` owns persistence, `app/detection/` contains explainable rules, `app/api/` exposes the local interface, and `frontend/` contains the dependency-free dashboard. Tests use temporary databases and synthetic browser fixtures.

## Quick start

```powershell
git clone https://github.com/doctor277/ghostwatch.git
cd ghostwatch
powershell -ExecutionPolicy Bypass -File .\start.ps1
```

Requirements:

- Windows 10 or Windows 11
- PowerShell 5.1 or newer
- Python 3.11 or newer available as `python` on `PATH` (3.11 verified locally; CI targets 3.11 and 3.13)
- Internet access on the first run to install the pinned packages in `requirements.txt`

The launcher creates `.venv`, verifies the Python version, installs dependencies when the requirements hash changes, checks port `8765`, starts Uvicorn on `127.0.0.1`, waits for the health endpoint, and opens the dashboard. Use `-NoBrowser` to suppress automatic browser launch. Press `Ctrl+C` in the launcher window to stop GhostWatch.

### Demo Mode

Open **Settings** and select **Enter Demo Mode**. A yellow banner identifies the data as synthetic and not saved. Demo Mode does not scan local folders or persist its observations, making it the safest way to explore the complete interface.

## Testing

Create a development environment and install the pinned Python dependencies:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Run the Python suite, focused release-security regressions, linting, and compilation checks:

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m pytest tests\test_v3_final_packaging.py tests\test_v3_rc4_hardening.py tests\test_v3_rc5_release.py
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m compileall -q app scripts tests
```

Node.js 20 or newer is required only for browser and JavaScript checks:

```powershell
npm ci
node --check frontend\js\charts.js
node --check frontend\js\dashboard.js
node --check frontend\js\demo.js
node --check frontend\js\network-graph.js
node --check frontend\js\websocket.js
node --check e2e\dashboard.spec.js
node --check e2e\synthetic-backend.js
node --check playwright.config.js
npx playwright install chromium
npm run test:e2e
```

The Playwright suite starts a temporary loopback server and supplies synthetic REST/WebSocket data. Browser binaries are downloaded separately and are not committed or distributed with this repository.

## Verified results

The accepted Windows closeout on **October 27, 2026** reported 222 Python tests passed, 15 synthetic Playwright tests passed, and 77 focused release-security tests passed. Ruff, Python compilation, JavaScript syntax, PowerShell parsing, SQLite integrity, and live Host/Origin/WebSocket checks also passed.

The portfolio-preparation rerun on **November 1, 2026** reproduced 222 Python passes, 15 Playwright passes, and 77 focused release-security passes. These are dated Windows results, not a claim that every future commit or platform has passed. See [Current Beta Validation](docs/CURRENT_BETA_VALIDATION.md) for exact commands, timings, warnings, and scope.

The local portfolio-upgrade branch was revalidated on **November 3, 2026**: 222 Python tests, 77 focused release-security tests, and 15 synthetic Playwright tests passed with no failures or skips. The hosted GitHub Actions workflow has not run yet.

## Security and privacy boundaries

- The supported launcher binds only to `127.0.0.1:8765`; do not expose it through a LAN listener, reverse proxy, tunnel, or port-forward.
- HTTP `Host`/unsafe-request `Origin` checks and WebSocket `Host`/`Origin` checks enforce the intended browser boundary. They are not authentication against another native process running as the same Windows user.
- GhostWatch captures metadata, not packet payloads. It does not scan remote systems, block traffic, or terminate processes.
- File-integrity monitoring is opt-in. File contents are hashed but not stored.
- Default dashboard/export paths redact usernames, SIDs, home directories, and sensitive paths. Raw Windows event data remains local.
- Runtime databases, sidecars, logs, environments, test artifacts, real telemetry screenshots, and excluded concept images are ignored or rejected by the release packager.
- Optional AI sends redacted context only to a user-configured loopback Ollama endpoint. There is no cloud fallback or tool access.

Report suspected vulnerabilities privately as described in [SECURITY.md](SECURITY.md). Do not attach real host telemetry, credentials, databases, or diagnostic archives to public issues.

## Limitations

- This local beta supports one Windows user on one computer and has no multi-user authentication or remote deployment model.
- Windows permissions can make process details, socket ownership, or Event Log data partial or unavailable; GhostWatch reports gaps instead of inferring values.
- Chromium is the verified browser engine; Firefox, WebKit, and native Edge-specific behavior are not currently covered.
- Python 3.11 is verified locally. Python 3.13 dependency wheels were checked, but that CI leg remains unverified until the first hosted workflow run.
- The accepted short runs do not replace long-duration soak testing, independent penetration testing, or production security certification.
- Historical RC2-RC5 and V2 documents are retained for engineering traceability and do not describe the current beta state.

## Project documentation

- [Documentation index](docs/README.md)
- [Current beta validation](docs/CURRENT_BETA_VALIDATION.md)
- [GitHub publication checklist](docs/GITHUB_PUBLICATION_CHECKLIST.md)
- [Public asset provenance](docs/ASSET_PROVENANCE.md)
- [Third-party notices](docs/THIRD_PARTY_NOTICES.md)
- [Contributing guidelines](CONTRIBUTING.md)

## License

GhostWatch-authored source and documentation are available under the [MIT License](LICENSE), copyright 2026 Rafi Dahoui. Third-party packages, optional tools, browser binaries, models, fonts, and other components remain under their own terms; see [Third-Party Notices](docs/THIRD_PARTY_NOTICES.md).
