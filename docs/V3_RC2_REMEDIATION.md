# GhostWatch V3 RC2 Independent Audit Remediation

Date: 2026-10-22  
Version: 3.0.0-rc.2  
Scope: isolated V3 staging tree only  
Status: audit defects remediated and engineering gates passed; not a declaration of general release readiness or penetration testing

## Finding 1: database compatibility

### Root cause

`Database.initialize()` read the existing schema version but only used it to decide whether to create a migration backup. It then unconditionally created v3 objects and upserted `schema_version=3`. A database declaring a version greater than the application's supported version could therefore have its metadata silently downgraded.

### Fix

- `app/storage/database.py` now raises `UnsupportedSchemaVersionError` before any database PRAGMA, backup, DDL, or metadata write when the declared version exceeds `SCHEMA_VERSION`.
- Compatibility inspection uses a byte-for-byte temporary snapshot of the database plus existing `-wal` and `-shm` sidecars. Uncheckpointed schema metadata is visible while the live database and sidecars remain untouched.
- Supported migrations retain the existing pre-migration backup and single-transaction DDL/version update behavior.

### Regression evidence

- A future version stored in an active WAL is rejected.
- Main database, WAL, and SHM bytes and modification timestamps remain identical after rejection.
- No migration backup is created for an unsupported future schema.
- A synthetic interrupted v2-to-v3 migration rolls back its DDL and retains version 2, then succeeds on retry.
- Existing valid migration, integrity-check, and corruption-recovery tests remain enabled.

## Finding 2: historical query correctness

### Root cause

`list_historical_samples()` selected ascending rollups first, subtracted them from the caller's limit, and only then queried raw telemetry. Older rollups could consume the whole limit and exclude newer raw observations. Intelligence truncation used `len(result) >= limit`, which falsely marked an exact-size result as truncated and could not identify which exported dataset was truncated.

### Fix

- `app/storage/database.py` queries a bounded `limit + 1` probe from both raw and rollup sources, merges them under one stable ordering, selects the newest requested observations, and returns them chronologically.
- The explicit ordering is timestamp ascending for presentation; at an identical timestamp an aggregate precedes a raw observation. Recent-limit selection applies the inverse ordering, so the raw observation wins an exact-timestamp cutoff.
- Process observations and connection summaries use the same newest-selection/chronological-presentation contract.
- Page methods return an exact `truncated` boolean based on the probe row.
- `app/service.py` avoids reporting the omitted head of a newest-limited query as missing data and accounts for a rollup's covered interval when finding gaps.
- `app/models.py` records `truncated_datasets`; `app/api/routes.py` reports truncation for the selected export dataset and emits `X-GhostWatch-History-Truncated` for JSON and CSV exports.

### Regression evidence

- Small mixed-resolution limits retain recent raw observations.
- A 240-observation mixed dataset returns a stable 150-row chronological page containing both resolutions.
- Exact-size exports report `truncated=false`; over-limit exports report `true`.
- Telemetry-only exports are not marked truncated when only process history exceeded its limit.
- Missing-data output does not convert an intentionally omitted head into a synthetic gap.

## Finding 3: Windows event parser reliability

### Root cause

`WindowsEventCollector._parse()` returned an empty list for XML parse errors. `collect()` could not distinguish that result from a valid empty query and marked the channel healthy, making a malformed or truncated response look like successful collection.

### Fix

- `app/monitoring/windows_events.py` now distinguishes empty success, complete parse failure, and partially invalid event records.
- Malformed or truncated nonempty responses produce `unavailable` with a generic diagnostic and the previous cursor.
- Valid empty output remains `healthy` with the previous cursor.
- A syntactically valid response containing unnormalizable event records is `degraded`; valid records may be deduplicated and stored, but the previous cursor is retained so skipped records can be retried.
- Timestamp, event ID, and record ID failures are treated as invalid records rather than replaced with fabricated values.
- `app/models.py` adds the explicit `degraded` channel state. Persistent cursors continue to advance only for `healthy` channels.

### Regression evidence

- Malformed text and truncated XML are unavailable and cursor-stable.
- Blank and `<Events />` responses are healthy empty results.
- Fully valid events normalize and advance the cursor.
- Mixed valid/invalid events are degraded and cursor-stable.
- Permission errors remain nonfatal and cursor-stable.
- Two consecutive failures followed by a valid response recover and advance correctly.

## Tests added

`tests/test_v3_rc2_remediation.py` adds 13 collected regression cases covering future-schema WAL safety, migration interruption/recovery, mixed-resolution limit behavior, large datasets, export truncation, gap semantics, malformed/truncated/empty/partial/valid Windows XML, and repeated failure recovery.

Focused command:

    python -m pytest tests/test_v3_rc2_remediation.py tests/test_v3_history.py tests/test_v3_windows_events.py tests/test_v3_hardening.py -q

Actual result:

    35 passed, 1 warning in 7.28s

Complete command:

    python -m pytest -q

Actual result:

    118 passed, 1 warning in 15.93s

The warning is the existing unsuppressed Starlette `TestClient` deprecation warning recommending `httpx2`.

## Windows integration

A bounded read-only collector call requested at most one event from each supported channel and printed no event content.

Sandboxed result:

    events=0
    Defender: permission_denied, cursor=0
    System: permission_denied, cursor=0
    Application: permission_denied, cursor=0

Approved host integration result:

    events=3
    Defender: healthy, cursor=2834
    System: healthy, cursor=32482
    Application: healthy, cursor=23959

No Windows security setting was changed.

## Synthetic socket benchmark

Command:

    python -m scripts.benchmark_endurance

Actual 20-cycle result:

    sockets=10000
    repeats=20
    latency_min_ms=265.62
    latency_median_ms=700.88
    latency_p95_ms=784.09
    latency_max_ms=784.09
    traced_current_bytes=3429233
    traced_peak_bytes=8549398
    traced_current_growth_bytes=155344
    lifecycle_entries=10000
    recently_closed_entries=100
    detail_entries=250

The measurement is synthetic, machine-specific, and not a multi-day soak result. The higher latency than RC1's recorded run is retained rather than normalized or hidden.

## Browser validation

Browser plugin availability: absent. The checked-in Playwright workflow was used with its loopback synthetic backend.

Command:

    npm run test:e2e

Actual result:

    12 passed (35.5s)

Desktop Chromium and Pixel 7 emulation covered page identity, all navigation sections, process interactions, alerts, settings persistence, history/export UI, incidents, reports, file integrity controls, Demo isolation, accessible forms, WebSocket recovery, and mobile navigation. No application console warning/error or page-error gate failed. The Node runner reported that `NO_COLOR` was ignored because `FORCE_COLOR` was set; this was not a browser-page warning.

## Environment notes and non-successful attempts

- The staging tree intentionally contains no `.venv`; the first `.venv\\Scripts\\python.exe` command therefore did not start.
- System Python 3.14.3 did not contain pytest.
- The dependency-complete interpreter from the unchanged V2 environment was used only to execute the V3 source and tests.
- Initial sandboxed pytest attempts could not create the default temporary directory or local `socketpair()` required by Starlette TestClient. Tests were rerun with a staging-local temporary directory and the approved local-socket capability. These were environment failures before application execution, not hidden test failures.

## Remaining limitations

- No penetration test was performed.
- Browser automation remains Chromium-only.
- The 10,000-socket benchmark is synthetic and not an extended soak.
- Event-log reset/wrap detection remains generic; parser and cursor recovery are now explicit.
- File integrity monitoring remains polling-based.
- Historical rollups remain hourly averages and storage has no byte quota.
- Local API security continues to rely on the documented same-Windows-user trust boundary.

Production V2 was not modified or merged.
