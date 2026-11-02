# Ghostwatch V3 Project Omega Master Plan

Status date: 2026-10-21  
Baseline: Ghostwatch V2 2.0.0, Python 3.14.3, 58 tests passing with one upstream TestClient warning  
Source control: the supplied directory is not a Git repository; no commit or branch exists

## Existing architecture

Ghostwatch is a loopback-only FastAPI/Uvicorn process. Resilient psutil collectors feed a shared TelemetryService, which applies rules and rolling in-memory baselines, maintains bounded live state, optionally persists alerts/events/incidents/settings in SQLite, and publishes snapshots through bounded per-client WebSocket queues. A dependency-free HTML/CSS/JavaScript dashboard consumes REST and WebSocket data. Optional AI analysis calls an explicitly configured loopback Ollama endpoint.

## Proposed V3 architecture

V3 retains the proven V2 pipeline and adds modules at explicit boundaries:

- network collection: availability-aware lifecycle state and streaming aggregation;
- privacy: one recursive structured-data redactor shared by AI and reports;
- reporting: validated, category-specific report schemas and renderers;
- storage: schema-v3 transactional migration plus historical samples, rollups, baselines, audit records, indicators, and optional feature state;
- Windows intelligence: permission-aware normalized read-only event and posture collectors;
- file integrity: opt-in bounded watcher/verification engine over user-selected paths;
- correlation/detection: explainable bounded windows over normalized stored events;
- frontend: grouped V3 navigation and synthetic, isolated Demo fixtures;
- verification: deterministic Python tests plus checked-in browser automation when the local Node/browser environment supports it.

Live telemetry must never wait for durable history, Windows logs, file monitoring, AI, notifications, or report generation.

## Mandatory audit findings and enforcement boundaries

1. Connection lifecycle failures: NetworkCollector.collect must commit lifecycle changes only after successful enumeration. Unavailable enumeration preserves the last known lifecycle and exposes low confidence.
2. Report accuracy: report_payload must dispatch to type-specific builders validated against a report envelope before rendering.
3. AI privacy: structured redaction must recurse before prompt serialization without mutating source objects.
4. Large socket memory: NetworkCollector.collect must aggregate a single iterable pass and construct no more than the configured detail limit.
5. Ollama redirects: the AI transport must use a no-redirect opener, validate the request target, reject 3xx, and bound request/response bytes and time.

## Feature dependencies and phases

### P0 — correctness and security

- Fix and regression-test all five findings.
- Preserve complete connection aggregates and detection inputs.
- Validate report selection and all three formats.
- Add deterministic privacy and redirect tests.
- Measure 500/1,000/5,000/10,000 synthetic sockets.
- Run all baseline tests, Ruff, compileall, JS syntax, package checks, and a focused bypass review.

### P1 — durable investigations

- Transactional schema-v3 migration with pre-migration backup and integrity validation.
- Tiered historical telemetry and bounded retention/quota.
- Windows event normalization and permission states.
- Opt-in file-integrity engine using disposable test directories.
- Correlation rules and incident audit history.
- Historical/report APIs and dashboard sections.
- Checked-in browser tests if Playwright can be installed and executed reproducibly.

### P2 — intelligence and UX

- Persistent baselines, offline indicator matching, posture/privacy/health dashboards.
- Investigation timeline, detection profiles/tuning, V3 graph filters.
- Typed local AI summaries using the shared privacy layer.
- Grouped 19-section responsive navigation and complete Demo fixtures.

### P3 — optional Windows integration

- Opt-in rate-limited notifications.
- Tray prototype and executable feasibility only if built and launched successfully.

## Privacy implications

Historical data, event logs, watched paths, incident notes, command lines, endpoints, and hashes can identify a user or workstation. New collectors remain disabled or permission-aware where appropriate. AI receives only bounded redacted structures. File content is never persisted or uploaded. No external indicator lookup, analytics, cloud model, scheduled task, startup registration, or report transmission is enabled automatically.

## Technical risks

- Windows APIs and permissions vary by edition, policy, elevation, and language.
- psutil.net_connections may itself allocate a full OS result list; Ghostwatch can bound its own objects but cannot control upstream allocation.
- Durable high-resolution telemetry can grow quickly without rollups and quotas.
- Event-log and file-watcher bursts require queues, deduplication, and explicit drop accounting.
- A full Python 3.11–3.14 matrix depends on locally installed interpreters.
- Browser automation installation may require network access and a large browser download.
- The loopback API has no same-user native-process authentication.

## Acceptance criteria

- Every completed feature has a deterministic test or recorded live verification.
- Existing 58 tests remain meaningful and passing.
- Failed connection enumeration creates no false close/open lifecycle events or anomaly samples.
- Report families contain correct category metrics and explicit missing-data notes.
- Redaction covers nested IPv4/IPv6, email, username, Windows/home paths, arguments, notes, evidence, and endpoints without mutating inputs.
- AI redirects and oversized responses are rejected without contacting external hosts.
- 10,000 synthetic sockets preserve aggregate/ranking correctness while Ghostwatch detail construction remains capped.
- Migrations use disposable legacy copies, backup before change, rollback on failure, and integrity checks.
- All queues, histories, caches, and rendered datasets are bounded.
- Final documentation marks every requested feature COMPLETE, PARTIAL, NOT IMPLEMENTED, or BLOCKED.

## Test strategy

Start with focused reproductions for each P0 finding, then run the owning module tests and full suite. P1/P2 use synthetic psutil rows, disposable SQLite databases/directories, normalized Windows-event fixtures, mocked Ollama responses, and deterministic clocks. Live checks are restricted to loopback. Performance tests record elapsed time and tracemalloc peak allocations rather than enforcing hardware-specific numbers unless a conservative regression ceiling is justified.

## Rollback strategy

All implementation occurs in an isolated V3 copy because the source has no Git history. Before synchronization, every release file is SHA256-compared to its staged source and the original target remains untouched during development. Database migrations operate only after an online pre-migration backup and transaction begin; failure rolls back and retains the backup. Optional collectors are disabled by default. The final review archive excludes runtime databases and can reconstruct the source release.

## Implementation status

| Area | Status |
|---|---|
| Baseline inspection and 58-test run | COMPLETE |
| Five independent audit fixes | COMPLETE |
| Historical telemetry and schema v3 | COMPLETE (Phase A) |
| Windows events | COMPLETE (Phase B) |
| File integrity | COMPLETE (Phase C) |
| Correlation and V3 incidents | NOT IMPLEMENTED |
| Persistent baselines and threat indicators | NOT IMPLEMENTED |
| V3 dashboards and Playwright | COMPLETE (Phase D scope) |
| Notifications/tray/executable | NOT IMPLEMENTED |
| Engineering hardening | COMPLETE (Phase E scope) |
| Final release archive and evidence | COMPLETE |
