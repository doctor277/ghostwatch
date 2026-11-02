# Ghostwatch V2 Changelog

## 2.0.0 — 2026-10-21

### Monitoring and performance

- Counted established sockets and rule inputs across the complete OS connection list while retaining a 250-row detail limit.
- Corrected per-process connection aggregation where owner PIDs are available.
- Added explicit unavailable/error telemetry for system and network collector failures.
- Added cached, rotating process enrichment and bounded frontend topology/history.

### Detection, storage, and workflow

- Separated bounded live alert state from optional SQLite persistence.
- Added configurable rule controls, duration/cooldown behavior, stable evidence IDs, rolling baselines, events, and incident workflow.
- Added SQLite schema versioning, WAL mode, indexes, migrations, immediate retention enforcement, online backups, and storage-recovery queues.
- Added eight report types in JSON, CSV, and HTML with redaction and injection-safe rendering.
- Added optional disabled-by-default loopback-only Ollama analysis.

### Security and reliability

- Added strict HTTP Host and WebSocket Host/Origin allowlists.
- Added security headers and restrictive Content Security Policy.
- Added a configurable 32-client WebSocket maximum, bounded per-client queues, and send timeouts.
- Added generic corrupted-settings recovery logs without echoing stored private content.
- Upgraded pinned FastAPI/Starlette/test dependencies and verified no known requirement vulnerabilities.

### Frontend and Windows operations

- Expanded the dashboard to twelve responsive workspaces.
- Added process/connection search, process sorting/details/tree, network graph, incidents, timeline, report export, AI workspace, performance view, and mobile navigation.
- Fixed stale alert counters and table empty-state column spans.
- Added explicit browser-memory-only Demo mode and accessible control labels/sort state.
- Hardened `start.ps1` with Python >= 3.11 enforcement, requirements-hash caching, occupied-port detection, readiness wait, browser failure messaging, and child cleanup.

### Verification

- Preserved the original 36-test behavior and expanded the suite to 58 passing tests.
- Completed Ruff, compileall, JavaScript syntax, PowerShell parse, package consistency, dependency audit, live listener, Host/Origin, WebSocket, browser workflow, mobile, and performance checks.

### Design-fidelity ledger

The generated concept image informed the dark operations-console hierarchy, cyan/amber status language, restrained card density, left navigation, central topology focus, and high-contrast typography. The implementation deliberately differs by using bounded real Canvas data, no decorative world map, no fabricated threat feed, and explicit Demo labeling. Synthetic visual evidence uses reserved documentation networks and contains no personal telemetry.
