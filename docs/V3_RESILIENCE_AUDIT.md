# V3 resilience audit

Work in progress on the local `feat/v3-resilience-hardening` branch, based on
`b97ab88fffe5bb50fdc26855e3ee8ce3bc4dc3a1`. No engineering changes are authorized
for push. No changes to production V2.

## Progress record

- VERIFIED: Phase 0 repository identity, clean tree, expected history, fetched
  remote refs and Git integrity. One unreachable dangling blob; no corruption.
- VERIFIED: Phase 1 pushed only the approved documentation commit and updated
  [PR #1](https://github.com/doctor277/ghostwatch/pull/1).
  [Hosted run 41888969539](https://github.com/doctor277/ghostwatch/actions/runs/41888969539)
  succeeded: Python 3.11 and 3.13 each 222 passed / one known Starlette TestClient
  deprecation warning; Chromium 15 passed; Ruff, compilation and JS syntax passed.
  PR was open, unmerged and mergeable. Main was unchanged.
- VERIFIED: Phase 2 created the local engineering branch from the approved commit.
- VERIFIED LOCALLY: lifecycle, persistence and collector fixes below, Python 3.11.9
  on Windows only. Local commits only. Hosted CI has not run on these changes and
  Python 3.13 is not installed locally.
- VERIFIED LOCALLY: snapshot serialization hardening below, Python 3.11.9 on Windows:
  265 pytest passed, 16 Playwright tests passed against the synthetic server, Ruff
  and compilation passed. Hosted CI has not run on these changes.
- VERIFIED: hosted run
  [41919724946](https://github.com/doctor277/ghostwatch/actions/runs/41919724946) at
  `55ca2cb`: Python 3.11.9 and 3.13.17 each 265 passed, Chromium 16 passed.
- VERIFIED LOCALLY: alert serialization hardening below, Python 3.11.9 on Windows:
  295 pytest passed, 16 Playwright tests passed against the synthetic server, Ruff and
  compilation passed. Hosted CI has not run on this change.
- PENDING: open findings below, synthetic performance and memory measurements,
  supply-chain review, and release documentation.

These hosted results describe the portfolio branch, not the engineering changes.

## Fixed

Every fix has a regression test in `tests/test_v3_resilience.py`. Of its 26 tests,
25 fail against `b97ab88`. The other is a guard that recovery after a runtime
failure keeps the live baseline, which holds before and after the change.

| Finding | Effect before the fix | Fix |
| --- | --- | --- |
| Startup failure or cancellation leaked workers | Tasks kept running after a failed start | `start()` and `stop()` are serialized and release every worker |
| Lifespan skipped `stop()` on error | Exceptional exit left telemetry running | `try`/`finally` around `yield` |
| File scan reported success after a storage failure | A failed write was cleared to healthy | Scan stops at the first storage failure |
| Collector errors were treated as storage errors | One folder's crash degraded persistence and hid the rest | `_scan_folder` marks only that folder unavailable and keeps its baseline |
| Lone surrogate in a file name | Hashing succeeded, then event creation raised. One file blinded its folder and degraded storage | `safe_text` keeps the file monitored under an escaped name |
| Lone surrogate in a process identity | psutil returns the raw name for a real process. A snapshot containing it cannot be serialized, which reads as a failed send to every client | `safe_text` applied to process names, executables, usernames and connection process names |
| Pending writes stranded after a direct write restored health | Alerts stayed `persistence_pending` until the next outage | Each collection reconciles pending writes while healthy |
| Recovery never loaded settings missed at startup | A saved history opt-out reverted to the default, enabled | State is loaded once after a failed startup load, before storage reports available. Runtime recovery does not reload it |
| Writes could run under default settings during recovery | History or queued alerts could reach disk despite a saved opt-out | The history writer waits for saved settings, and queued writes are discarded if logging was saved as off |
| A saved opt-out was not applied if follow-up maintenance failed | Disk said disabled, memory stayed enabled until restart | Settings apply as soon as they are saved. The API still reports the failure |
| An alert update during a flush was overwritten | An acknowledgement was lost from the live view | The flush keeps a changed alert queued and saves it on the next pass |
| `sqlite3` connections were never closed | Open handles until garbage collection, 39 call sites plus `backup` | `_connect` closes in one place and `backup` uses `closing` |

A connection audit over the full test suite found 664 connections left open by
`app/` code at `b97ab88` and none of 927 after the fix.

The surrogate process path was reproduced with a real process for psutil's behavior
and with `ProcessInfo` for the serialization failure. The full broadcast path was
not exercised end to end with a live process.

## Snapshot serialization

Originally an open review finding. Later reproduced at `9e4f29d` through the real
routes, with a lone surrogate and with an unserializable object in alert evidence:

| Path | Before | After |
| --- | --- | --- |
| WebSocket broadcast | All 3 healthy clients closed with `1011`, labeled as a send timeout | Clients stay connected and receive `telemetry_degraded` |
| New WebSocket client | Closed with `1011` | Stays connected and receives `telemetry_degraded` |
| `GET /api/overview` | Plain-text 500 | JSON 503 with a fixed message |
| `NaN` or infinite values | Encoded as `null` | Unchanged: not a failure |

Root cause: both paths serialized inside handling meant for something else. The
WebSocket `try` treated an encoding error as a dead socket, and REST left it unhandled.

`TelemetryService.encode_snapshot` is now the shared boundary. It uses only the native
encoder, has no fallback encoder, and encodes each snapshot once. It keeps one entry
and refuses payloads over `MAX_SNAPSHOT_BYTES` (4 MiB, against about 315 KB measured for
a synthetic near-limit snapshot). Failures are logged once by exception type until
recovery, and snapshot contents are never logged or sent.

The dashboard shows `TELEMETRY DEGRADED` instead of `CONNECTED` and clears it on the
next valid snapshot, which also clears `DATA ERROR`. `/api/health` reports `snapshot` as
`warming-up`, `available` or `unavailable`. Valid `/api/overview` bodies are
byte-identical to those at `9e4f29d`.

Regression tests are in `tests/test_v3_snapshot_serialization.py`. Restoring the old
WebSocket path fails 7 of its 17 tests, and restoring the old REST path fails 4.

## Alert serialization

Originally recorded as a 500 from `/api/alerts`. Tracing it at `55ca2cb` showed the
cause was not in that route. `Alert` and `EventRecord` accepted values that consumers
could not handle, and each consumer failed differently. Reproduced with synthetic
alerts injected the way `collect_once` handles them:

| Input | Failures at `55ca2cb` |
| --- | --- |
| Lone surrogate in alert text | `_alert_event` raised, so every collection cycle aborted and `latest` froze. The queued write could never succeed, so storage stayed degraded through every recovery attempt. `/api/alerts`, `PATCH /api/alerts/{id}` and 12 report variants returned 500 |
| Lone surrogate in evidence | `/api/alerts`, the PATCH route, `/api/events` and 16 report variants returned 500. The alert and event routes stayed broken after a restart |
| Unserializable object in evidence | `/api/alerts`, the PATCH route, `/api/events` and all 48 report variants returned 500. The AI prompt received the object's `repr` |
| Circular evidence | `/api/alerts` returned 500 and the failed write degraded storage |
| Lone surrogate in a nested evidence key | `/api/alerts` returned 500 |

No current collector is known to produce these values: surrogate sources were escaped in
`d088c41` and Windows event text passes through an XML parser. Their impact, not a known
live trigger, is why this was fixed before review.

The fix has two layers:

- Model boundary: `Alert` and `EventRecord` escape lone surrogates with `safe_text`, and
  `safe_structure` makes evidence JSON-compatible. Values it cannot represent, including
  objects and cycles, become an explicit `[UNSERIALIZABLE]` marker and never their
  `repr`. The alert is kept, so one bad value cannot hide the others. Stored rows pass
  through the same models, so existing databases are covered.
- Response boundary: `PydanticSerializationError` and `UnicodeEncodeError` return a fixed
  JSON 503 and log only the exception type. This covers evidence mutated after
  construction. It also replaces the plain-text 500 from `POST /api/incidents` and
  `POST /api/file-integrity/folders` for unencodable input.

After the fix, all four matrix shapes returned 200 on all 55 route checks, storage stayed
healthy, nothing stayed pending, and restart was clean. Valid alerts, `NaN`, infinite
values, tuples and datetimes keep their JSON output.

Regression tests are in `tests/test_v3_alert_serialization.py`. 26 of its 30 tests failed
at `55ca2cb`. Mutation checks: removing text escaping failed 5, removing evidence
sanitizing 16, removing the response handlers 6, and replacing the marker with `str()` 4.
Removing cycle detection made the branching-cycle test run until the 110 second limit.

`tests/test_v3_snapshot_serialization.py` now assigns malformed text after construction,
because the constructor would otherwise repair it before reaching that boundary.

## Open findings

These come from code review and have not been reproduced, unless noted.

- Reproduced with synthetic data: an alert explanation over 2,000 characters fails
  `EventRecord` validation in `_alert_event` and aborts the collection cycle. No current
  source is known to produce text that long.
- Reproduced: unencodable input to `POST /api/incidents` and
  `POST /api/file-integrity/folders` returns 503. A 422 would describe it better.
- A permanently failing queued write can keep storage degraded, because
  `_flush_pending` stops at the first failure. The reproduced causes are fixed, but no
  general limit on retries exists.
- `broadcast` can send directly to a client that is registered but not yet accepted,
  then close it. The window is narrow.
- `unregister_client` suppresses `CancelledError`, including the caller's own.
- `_stop_workers` closes clients one at a time, up to 64 seconds at 32 clients.
- `baseline_engine.observe` and `detection_engine.evaluate` are outside the
  collector error handling, so a failure there skips the snapshot and leaves
  `latest` stale.
- `initialize` runs `PRAGMA integrity_check` inside a write transaction on every
  recovery attempt. Cost on large databases is unmeasured.
- Surrogates in monitored folder paths submitted through the API are untested.

## Known limitations

- While startup storage is unavailable, default settings apply until recovery.
  History is dropped while degraded, but Windows events are still collected into
  bounded memory even if saved settings disable them.
- An escaped name is a display form. Only the on-disk path is authoritative.
- Evidence mutated after construction bypasses the model boundary, and one such alert
  makes the whole `/api/alerts` response a 503. No application code does this.
- `telemetry_degraded` covers snapshots that exist but cannot be served. The dashboard
  has no watchdog for frames that stop arriving while the socket stays open, so it can
  still show `CONNECTED` if collection itself stops.
- During a serialization failure the dashboard keeps the last rendered values under the
  degraded status. Playwright covers this path with synthetic frames only.
