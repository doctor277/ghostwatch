# GhostWatch V2 Implementation Plan

## Baseline and constraints

- The pre-change suite passes: `36 passed, 1 warning in 2.02s` on Python 3.14.3.
- The source directory is not a Git worktree, so a feature branch cannot be created. Work is staged in a separate copy and will be synchronized only after verification.
- GhostWatch remains loopback-only, defensive, read-only, and useful without internet access.
- Existing source, routes, launcher behavior, seven original dashboard areas, and tests will be preserved or migrated without weakening their assertions.
- Runtime databases, personal telemetry, screenshots containing live data, environments, caches, and secrets are excluded from deliverables.

## Architecture

GhostWatch V2 retains FastAPI, dependency-free browser JavaScript, psutil, Pydantic, and SQLite. The design adds bounded state and clear interfaces rather than replacing the working core:

1. **Collection layer** — resilient system, network, and process collectors report unavailable values explicitly and return partial results when permissions or counters fail.
2. **Snapshot layer** — one shared immutable telemetry snapshot contains bounded detail rows plus aggregates computed from the full accessible data set.
3. **Analytics layer** — configurable detection rules and a bounded rolling baseline generate evidence-backed anomaly descriptions without claiming malware.
4. **Reliability layer** — storage operations are isolated behind health state and retry/backlog handling; per-client WebSocket queues bound memory and disconnect stalled clients.
5. **Persistence layer** — versioned SQLite migrations add events and incidents with indexed, parameterized access and retention enforcement.
6. **API layer** — validated incident, event, report, rule/settings, and optional local-AI endpoints; cross-origin mutation protection and safe response headers.
7. **Presentation layer** — a responsive 12-section operations console with Canvas network graph, process tree, investigation drawers, timeline filters, incident workflow, report export, AI offline state, and explicit demo mode.

## Dependencies

- No new runtime dependency is planned. Ollama uses a strictly loopback HTTP endpoint through the Python standard library and is disabled by default.
- Existing test and lint dependencies remain pinned in `requirements.txt`.
- Browser interaction coverage uses the existing application and browser automation available in the verification environment; no CDN assets are permitted.

## Priorities and expected changes

### P0 — mandatory reliability and security

- Isolate SQLite initialization/read/write failures so fresh telemetry and live alerts continue.
- Expose honest storage health, retry recovery with bounded pending persistence, and deduplicate recovery writes.
- Aggregate socket counts and process rankings over every accessible connection while retaining the 250-row detail limit.
- Mark unavailable metrics as `null` plus collection diagnostics; detection rules skip missing evidence.
- Add per-client bounded WebSocket queues, send deadlines, connection limits, cleanup, and graceful shutdown.
- Add safe security headers, a restrictive same-origin CSP, strict Host/Origin checks, and unsafe-method Origin validation.

### P1 — investigations and operations

- Track connection first/last seen state, duration, scope, new/disappeared connections, filtering, and sorting.
- Enrich process rows with accessible identity and lifecycle metadata, relationships, remote endpoints, and bounded resource history.
- Add configurable rule enablement, cooldowns, thresholds, stable alert identities, and evidence snapshots.
- Add versioned event and incident persistence, APIs, status transitions, notes, and explicit local exports.
- Migrate the original seven sections into the 12-section V2 console and add a bounded interactive Canvas graph and process tree.

### P2 — analytics and operator tooling

- Add a bounded rolling baseline with minimum sample gates, averages, standard deviation, percentiles, and deviation labels.
- Add JSON, CSV, and self-contained HTML reporting with explicit download action, redaction, availability notes, and CSV formula neutralization.
- Add an optional, loopback-only Ollama analyst with limited structured context, timeout, redaction, offline handling, and no tools or cloud fallback.
- Improve launcher diagnostics and retain hash-based dependency installation avoidance.
- Add visible, isolated demo mode that never persists synthetic observations.

### P3 — optional

- A packaged executable, tray integration, GeoIP enrichment, PDF export, native notifications, and process-control actions are out of the verified core. They will not be represented as complete unless actually built and exercised.

## Risks and mitigations

- **Windows privilege variance:** fields that require elevation are optional and labeled unavailable; collectors continue with accessible data.
- **Large host state:** tables, histories, graph nodes, event backlogs, AI context, and WebSocket queues are all bounded.
- **SQLite corruption/locking:** failures become degraded state; the application does not delete or replace the user's database automatically.
- **Local attack surface:** loopback binding, Host/Origin enforcement, CSP, and mutation checks reduce browser-origin attacks but do not defend against a malicious native process under the same user account. This limitation is documented.
- **Anomaly uncertainty:** findings show observed evidence and missing context and never assert compromise.
- **Frontend performance:** rendering is rate-limited, graph sizes are capped, searches are debounced, and reduced-motion preferences are honored.

## Acceptance criteria

- All 36 original tests remain meaningful and pass.
- New deterministic regression tests cover storage outage/recovery, 300+ connection aggregation/ranking, missing metrics, WebSocket backpressure/cleanup, security headers and origins, incidents, events, reports, AI failures, retention, and shutdown.
- `pytest`, Ruff, Python compilation, JavaScript syntax checks, and PowerShell parsing are executed with actual output retained.
- A real loopback listener is observed on `127.0.0.1:8765`; rejected Host, Origin, and cross-origin mutation cases are exercised.
- Browser smoke checks cover every navigation section, graph/process interactions, alert and incident actions, filters, settings, reports, AI offline behavior, reconnection, keyboard operation, and mobile layout without unexpected console errors.
- Actual collection duration, service memory, listener state, database size, and representative WebSocket latency are measured and clearly scoped.
- Final documentation distinguishes completed, partially verified, environment-limited, and intentionally deferred work.
- The sanitized review archive contains only approved source, tests, manifests, configuration, evidence, and documentation and receives a SHA256 hash after a sensitive-data scan.
