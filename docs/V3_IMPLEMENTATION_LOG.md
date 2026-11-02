# Ghostwatch V3 Implementation Log

## Baseline — 2026-10-21

- Source directory contained no Git metadata, so no commit or worktree state exists.
- Python 3.14.3.
- Baseline command: python -m pytest -q.
- Result: 58 passed, 1 StarletteDeprecationWarning, 0 failed in 5.41 seconds.
- Existing dependency versions were captured from pip freeze.
- An isolated cache-free V3 working copy was created; the live database was not copied or modified.

## P0 audit remediation — COMPLETE

### Connection lifecycle false events

Outcome: fixed.

The vulnerable path converted connection-enumeration exceptions into an empty list, then diffed that synthetic empty state against the prior successful sample. NetworkCollector now commits lifecycle changes only after a successful complete iteration. Unavailable collection returns explicitly stale bounded detail with unavailable confidence, preserves prior lifecycle/rankings, and generates no false new/closed counts. Partial lifecycle tracking never infers closures.

Validation covers success -> failure -> recovery, repeated failures, true closure after recovery, simultaneous additions/removals, mid-iterator failure, and more than 250 sockets.

### Report content accuracy

Outcome: fixed.

Reports now use a Pydantic-validated schema 3.0 envelope and category-specific data builders for all eight families. System reports contain system/storage/collection metrics; network and process reports contain their respective telemetry; alerts, anomalies, incidents, daily, and weekly reports select appropriate records and time windows. JSON, generic field/value CSV, and escaped HTML render the selected data rather than assuming event-only rows.

### AI privacy redaction

Outcome: fixed.

A shared recursive privacy module redacts nested addresses, endpoints, emails, usernames, Windows/UNC paths, home-path fields, arguments, alert evidence, and notes before prompt serialization. Standard and strict levels are supported. The function rebuilds structures and deterministic tests prove the original evidence is unchanged.

### Large socket collection memory

Outcome: fixed within Ghostwatch's allocation boundary.

The collector no longer copies the psutil result or constructs ConnectionInfo objects for every row. It performs one incremental aggregate pass, constructs at most 250 detail objects, caches each process name once, computes unknown-owner rankings without rescanning, and bounds lifecycle state to 20,000 keys. Upstream psutil may still allocate its own platform result before returning it.

Actual synthetic benchmark:

| Sockets | Elapsed | Peak traced allocation | Aggregate count | Detail count |
|---:|---:|---:|---:|---:|
| 500 | 43.37 ms | 787,280 B | 500 | 250 |
| 1,000 | 41.88 ms | 875,548 B | 1,000 | 250 |
| 5,000 | 124.36 ms | 3,035,652 B | 5,000 | 250 |
| 10,000 | 227.25 ms | 4,887,388 B | 10,000 | 250 |

Command: python -m scripts.benchmark_sockets.

### Ollama redirect validation

Outcome: fixed.

The default transport uses a no-redirect handler, requires an explicit valid loopback port and no endpoint path, checks response status and final URL, and bounds request/response bytes and time. Mocked malicious responses and a disposable loopback server returning a redirect to a documentation-only external address were rejected without following the redirect. Normal injected loopback response behavior remains covered.

## P0 verification

Focused result after compatibility correction: 43 passed, 1 warning.

Adversarial review tests: 20 passed.

Final full command:

    python -m ruff check .
    python -m pytest -q --basetemp <disposable-path>

Final result:

    All checks passed!
    78 passed, 1 warning in 6.62s

The warning is the pre-existing FastAPI/Starlette TestClient deprecation warning about future httpx2 migration. It was not suppressed.

## P1 historical telemetry foundation — PARTIAL

- Schema version increased from 2 to 3.
- Existing versioned databases receive a timestamped online SQLite backup before migration.
- Migration creates indexed telemetry_samples storage and performs an integrity check before recording schema version 3.
- Historical samples preserve unavailable values as null and record collector status plus unavailable metric names.
- Retention is configurable from 24 hours through 90 days with a bounded maximum row count.
- A bounded 1,000-item background queue isolates historical writes from live publication and counts dropped samples.
- New APIs expose validated timezone-aware ranges up to 90 days and persisted history settings.
- Disposable migration tests verify the pre-v3 backup remains schema version 2 and both source/backup pass integrity checks.

Focused result: 15 passed, 1 warning. Full regression result after this slice: 82 passed, 1 warning in 7.08 seconds.

Still required at that checkpoint: tiered rollups, Historical Intelligence frontend, export, and persistent baselines.

## Phase A — Historical Intelligence — COMPLETE

Completed on 2026-10-22 in the isolated V3 staging directory. The V2 target remained untouched.

- Each enabled collection cycle now persists CPU, RAM, disk, upload/download, process count, and connection totals as a single atomic history batch.
- Up to the configured bounded number of high-activity process observations are retained per cycle without executable paths, usernames, or command lines.
- Full-set connection summaries retain active/established/destination/lifecycle counts, availability state, lifecycle confidence, and bounded process aggregates.
- Time-range queries combine recent raw data with hourly compacted rollups. Missing values remain null and unavailable metric names are retained.
- Raw telemetry older than the compaction window is rolled into hourly mean observations; all history tables obey time retention, and row ceilings bound aggregate, summary, and process records.
- Rolling baseline inputs are serialized to a dedicated SQLite row, validated on restore, and capped by BaselineEngine.max_samples so restarts do not reset learned history.
- The Historical Intelligence dashboard provides CPU/RAM, throughput, and connection charts, explicit gap summaries, process/connection tables, range selection, and user-triggered JSON/CSV export.
- Demo mode never queries or exports retained history.
- CSV export neutralizes spreadsheet-formula-leading strings.

Tests added: 4 (history batches, compaction/missing values, baseline restart state, intelligence query/export). Historical tests now total 8.

Exact focused gate:

    python -m ruff check app tests scripts
    python -m pytest tests/test_v3_history.py -q
    node --check frontend/js/charts.js
    node --check frontend/js/dashboard.js

Result:

    All checks passed!
    8 passed, 1 warning in 2.46s
    JavaScript syntax checks exited 0

Full regression command and result:

    python -m pytest -q
    86 passed, 1 warning in 8.07s

The warning remains the unsuppressed upstream FastAPI/Starlette TestClient deprecation warning. Known limitations: process observations are sampled from the bounded highest-activity set rather than every process; rollups are hourly means rather than min/max/percentile aggregates; retention is row/time bounded rather than a strict database-byte quota; historical charts provide fixed ranges rather than pointer zoom.

## Phase B — Windows Security Events — COMPLETE

- Added a read-only `wevtutil` collector that never invokes a shell and queries only Windows Defender Operational, System, and Application.
- Each channel uses its durable EventRecordID cursor; first collection reads only the configured newest bounded window and subsequent reads request IDs above the cursor.
- Normalization records provider, event ID, severity, category, summary, optional service name, and correlation tags. Raw XML is not retained.
- Primary-key plus `(channel, record_id)` uniqueness provides durable duplicate suppression, with an additional bounded live seen-set during database outages.
- Defender detections and service-control failures generate deterministic, non-duplicated live alerts. Alert persistence continues to honor the existing event-logging preference.
- Permission errors and unavailable channels are reported per channel without stopping telemetry or other event channels.
- Search and filters cover provider/summary/service, channel, category, level, and time range. The dashboard provides channel status, summaries, controls, filters, and a normalized table.
- Collection settings and cursors survive restart. No Windows security configuration is read or changed beyond the requested event-log reads.

Tests added: 6 (incremental cursor/deduplication, permission fallback, unsupported platform, durable filters/deduplication, correlation, API/settings).

Focused result:

    All checks passed!
    6 passed, 1 warning in 1.48s
    frontend/js/dashboard.js syntax check exited 0

Constrained live Windows verification (one newest record per channel, no event contents printed): all three channels returned `healthy`; normalized_events was 3. No Defender detection existed in that three-record sample, so live detection correlation remains synthetically verified rather than triggered on the host.

Full regression result:

    92 passed, 1 warning in 8.83s

Known limitations: Event Log summaries can contain locally sensitive text and are therefore kept local; localized/unrendered providers may yield structured field summaries instead of a human-formatted message; cursor reset after a Windows log is cleared is not yet handled specially; event correlation is deliberately limited to known Defender and service-failure IDs and is not a malware verdict.

## Phase C — File Integrity Monitor — COMPLETE

- Added an opt-in monitor that is disabled by default and accepts only explicit absolute, existing directory paths.
- Initial enrollment creates a SHA-256 baseline without generating change alerts. Content bytes are read only for hashing and are never stored.
- Subsequent scans detect file creation, content modification, and deletion. Successful scans atomically replace the baseline and insert events.
- Symbolic links are skipped, hashing is chunked, files that change during hashing are not trusted, and file count/size ceilings are configurable.
- Partial or bounded enumeration preserves unseen prior baseline entries and never infers deletions from incomplete coverage.
- Durable event IDs plus baseline advancement suppress repeated observations while allowing a later recurrence of the same content transition to be recorded.
- File changes generate bounded live alerts and optionally persistent alert/event records according to the existing event-logging preference.
- Folder management supports add/baseline, pause/resume, remove-baseline, manual scan, settings, and filtered event search. Removing a folder cascades only GhostWatch metadata; it never changes the enrolled directory or files.
- The dashboard exposes accessible settings and folder forms, coverage state, folder controls, manual scanning, and event filters.

Tests added: 5 (create/modify/delete/dedup, bounded no-false-deletion behavior, path validation, full API lifecycle with real-file preservation, settings validation/persistence).

Focused result:

    All checks passed!
    5 passed, 1 warning in 1.77s
    frontend/js/dashboard.js syntax check exited 0

Full regression result:

    97 passed, 1 warning in 9.94s

Known limitations: polling is used instead of OS change journals; renamed files appear as a deletion plus creation; only regular non-symlink files are hashed; incomplete folders remain partial until access/limits permit a complete scan; paths and hashes are sensitive local metadata and are not automatically redacted in the local integrity dashboard.

## Phase D — Playwright Automation — COMPLETE

Browser availability: the Browser plugin/skill was not available, so the documented fallback was the checked-in Playwright workflow. `@playwright/test` 1.64.1 and its lockfile are development-only dependencies; npm audit reported zero vulnerabilities. Managed Chromium 156.0.8078.4 was used.

The synthetic fixture intercepts every dashboard REST request and WebSocket connection before navigation. It uses documentation-only/synthetic paths, addresses, processes, alerts, incidents, Windows events, and file events; the test server starts without background monitoring. No real telemetry is presented as test evidence.

Coverage includes:

- every one of the 15 current navigation sections;
- network graph and process search/sort/detail drawer;
- alert acknowledgement/dismissal and stale-counter behavior;
- incident creation and updates;
- settings persistence across reload;
- Historical Intelligence charts, missing-data state, and export;
- explicit report generation/download;
- file integrity enrollment and manual scan controls;
- Demo mode write/scan isolation;
- keyboard-accessible form fields and drawer dismissal;
- WebSocket close/reconnect/recovery;
- responsive mobile navigation on a Pixel 7 viewport;
- browser console warnings/errors and uncaught page exceptions as test failures.

Browser testing found two real bugs: both folder enrollment and incident creation accessed `event.currentTarget` after an `await`, when the browser had already cleared it. Each handler now captures the form synchronously before awaiting and resets that stable reference afterward.

Final command and result:

    npm run test:e2e
    12 passed (26.9s)

Combined regression gate:

    python -m ruff check app tests scripts
    python -m pytest -q
    node --check frontend/js/dashboard.js
    node --check frontend/js/charts.js
    npm audit --audit-level=high

Result: Ruff clean; 97 passed, 1 warning in 9.36s; both JavaScript syntax checks exited 0; npm reported 0 vulnerabilities. The browser runner printed a non-application Node notice that `NO_COLOR` was ignored because `FORCE_COLOR` was set; page console health remained clean.

Known limitations: only managed Chromium was run, not Firefox/WebKit; screenshots are attached to the ignored Playwright report rather than committed; mobile coverage targets one representative viewport; browser automation uses deterministic mocks and therefore complements rather than replaces the live listener and Windows collector checks.

## Phase E — Engineering Hardening — COMPLETE

- Schema-v3 creation and schema-version recording now share one transaction. An injected failure after all new DDL proved rollback leaves the source database at version 2 with no v3 telemetry table, while the pre-migration backup remains available.
- Invalid historical JSON/status values recover to explicit degraded metadata. Recovery logs name only the field class, never the corrupt value. A non-SQLite file is rejected without overwriting its bytes.
- A history write outage test proved live telemetry sequences continue, storage becomes degraded, the next cycle recovers, and subsequent history persists.
- PID reuse detection now compares process creation time before refreshing identity. If a PID is reused and identity access is denied, prior executable/username/parent/status values are cleared rather than attributed to the new process.
- Fifty cycles of 600 entirely churning sockets proved detail, lifecycle, recently-closed, and process-name caches stay bounded. A separate complete-lifecycle test proved all 600 closures are counted while only 100 close-detail models are constructed.
- The initial 20×10,000 churn benchmark exposed 10,000 discarded close-detail model constructions: median 1197.17 ms, p95/max 1274.35 ms. Limiting model construction to the 100-row close-detail capacity reduced the identical workload to median 366.24 ms and p95/max 442.61 ms while preserving full closure counts.
- A 100×10,000 follow-up measured min 250.46 ms, median 386.06 ms, p95 454.37 ms, max 467.86 ms; traced current 3,451,208 bytes, peak 8,559,035 bytes, and growth 174,459 bytes. Final bounds were 10,000 lifecycle keys, 100 recently closed details, and 250 visible details.
- Retention/integrity tests and existing Windows permission-failure, storage-recovery, privacy-redaction, WebSocket timeout/client-cap, and 300+ connection regressions all remained green.
- Live listener verification showed only `127.0.0.1:8765` in LISTEN state, owning the Uvicorn process. `/api/health` returned 200 with `scope: local-only` and schema version 3. The temporary server was stopped and absence of the listener was confirmed.

Tests added in this phase: 8. Browser-discovered application fixes: 2. Performance fixes: 2 (bounded partial-lifecycle replacement and bounded close-detail construction). Total Python tests: 105.

Final quality gate:

    python -m ruff check .
    python -m pytest -q
    python -m compileall -q app scripts tests
    node --check <every frontend/js/*.js>
    npm audit --audit-level=high
    npm run test:e2e

Result: Ruff clean; 105 passed, 1 warning in 10.09s; compileall exited 0; every JavaScript syntax check exited 0; npm audit found 0 vulnerabilities; Playwright 12 passed in 26.6s.

The sole Python warning remains the upstream FastAPI/Starlette TestClient deprecation warning about future httpx2 migration. It is intentionally not suppressed.

Remaining risks and limitations:

- The 100-cycle benchmark is a synthetic allocation/latency study, not a multi-day soak or a guarantee on other hardware.
- Upstream `psutil.net_connections()` may allocate platform results before Ghostwatch can apply its own bounds.
- The loopback API trusts the local OS account and does not authenticate other native processes running as that user.
- Windows Event Log reset/wrap detection, native change journals, Firefox/WebKit browser runs, strict database byte quotas, and pointer-zoom historical charts remain future hardening opportunities.
- No penetration test, malware classification, remote scan, Windows security-setting change, startup registration, V2 merge, or production deployment was performed.
