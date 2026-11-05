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
- VERIFIED LOCALLY: alert bounds and write rejection below, Python 3.11.9 on Windows:
  339 pytest passed, 16 Playwright tests passed against the synthetic server, Ruff and
  compilation passed. Hosted CI has not run on this change.
- VERIFIED LOCALLY: pre-push review fixes below, Python 3.11.9 on Windows: 403 pytest
  passed, 16 Playwright tests passed against the synthetic server, Ruff and compilation
  passed. Hosted CI has not run on this change.
- VERIFIED: hosted run
  [41956814091](https://github.com/doctor277/ghostwatch/actions/runs/41956814091) at
  `8a233a4`: Python 3.11.9 and 3.13.17 each 403 collected and 403 passed, Chromium 16
  passed, with Ruff, compilation and JavaScript syntax passing. Counts are from the job
  logs.
- VERIFIED LOCALLY: data-integrity review below, Python 3.11.9 on Windows: 487 pytest
  passed on three consecutive runs, 16 Playwright tests passed against the synthetic
  server, Ruff and compilation passed. Hosted CI has not run on this change, and
  Python 3.13 is not installed locally.
- VERIFIED: hosted run
  [41961326980](https://github.com/doctor277/ghostwatch/actions/runs/41961326980) at
  `277571b`: Python 3.11.9 and 3.13.17 each 487 collected and 487 passed, Chromium 16
  passed, 15 desktop and 1 mobile, with Ruff, compilation and JavaScript syntax passing.
  Counts are from the job logs. This supersedes the note above that hosted CI had not
  run on the data-integrity review.
- VERIFIED LOCALLY: queue capacity below, Python 3.11.9 on Windows: 515 pytest passed,
  17 Playwright tests passed against the synthetic server, Ruff, compilation and
  JavaScript syntax passed. Hosted CI has not run on this change.
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

## Alert bounds

Originally an open finding: an explanation over 2,000 characters failed `EventRecord`
validation in `_alert_event` and aborted the collection cycle. Reproducing it at
`98a7de6` showed `Alert` had no limits at all, while `EventRecord` did. Reviewing the
sanitizer added in that commit found further defects. All were reproduced with synthetic
data, and no current source is known to produce these values.

| Input at `98a7de6` | Effect |
| --- | --- |
| Explanation of 2,001 or 1,000,000 characters | Every collection cycle aborted |
| 1,999 characters plus one lone surrogate | Aborted: escaping grew the text past the limit |
| Metric or identifier of 201 characters | Aborted through `title` or `related_id` |
| 8 MB of evidence | Collection continued, but the snapshot was unavailable |
| Integer of 5,001 digits, or a count of `2**70` | Storage degraded and the write was queued for good |
| Unwritable record queued ahead of a valid one | Storage stayed degraded and the valid record was never stored |
| Shared references, not cycles | Work doubled per level, about 1.3 s at depth 18, on the event loop |
| Generator | Fully consumed, so an endless one would never finish |
| `str` enum | Stored as `E.RED` instead of `red` |
| 33 levels of nesting | Valid JSON labeled `[UNSERIALIZABLE]` |

The fix keeps `EventRecord` validation unchanged and normalizes at the earliest boundary:

- `Alert` bounds the explanation to 2,000 characters and `source`, `metric` and `rule_id`
  to 200, after escaping. Any cut ends in `[TRUNCATED]`. Text within the limits is
  unchanged.
- An `id` or `incident_id` over 200 characters becomes `truncated-sha256-<hex>`. Cutting
  would break relationships, and the digest is stable for the same input.
- `safe_structure` has a budget of 10,000 nodes, refuses iterators without consuming
  them, marks integers over 1,024 bits, and keeps the value of `str` subclasses. Depth
  and budget limits report `[TRUNCATED]`. Cycles stay `[UNSERIALIZABLE]`.
- Evidence over 16 KiB is replaced whole by a `[TRUNCATED]` entry, not cut partway.
- `collect_once` catches `ValidationError` per alert, for alerts that bypass the model.
  The alert stays live and is persisted without an event.
- `TypeError`, `ValueError` and `OverflowError` from a write reject that one record. It
  is not queued, the queue continues, storage stays healthy and `/api/health` reports
  `rejected_writes`. Storage outages still queue and retry as before.

Severity, source, timestamps, rule and incident relationships are preserved. After the
fix, every case above completes with storage healthy, and the shared-reference case took
0.35 s at depth 22 with a 1 MB peak.

Regression tests are in `tests/test_v3_alert_bounds.py`. 34 of the 42 run before the fix
failed, and the other 8 are guards for behavior that must not change. The remaining 2
were excluded because they hang on the old code. Mutation checks: removing the
explanation limit failed 13 tests, the label limit 3, the identifier digest 1, the
evidence limit 3, the integer limit 2, the collection guard 1 and record rejection 6.
Refusing no generators and removing the node budget each ran until the timeout, while
the unmodified code passed both tests in 1.2 s.

`test_excessively_nested_evidence_is_bounded` in `tests/test_v3_alert_serialization.py`
now expects `[TRUNCATED]`, because over-deep JSON is not unserializable.

## Pre-push review

A review of `98a7de6` and `d79c2ab` before pushing them found the following. All of it
was reproduced with synthetic data, and no current collector is known to produce these
values. The comparison with `55ca2cb` used the same probes on an export of that commit.

| Finding at `d79c2ab` | At `55ca2cb` |
| --- | --- |
| A stored alert ID over 200 characters was listed as a digest with no matching row, so acknowledge, dismiss and incident links missed it | Worked |
| An iterator inside a dataclass hung `Alert` construction | Construction succeeded, serialization hung |
| A late non-plain key made each visit scan every key for one node of budget. 20,000 keys shared 9,000 times took 69 s including serialization | 18 s, and 1.7 GB of JSON |
| A 10 KB string shared 9,990 times peaked at 100.5 MB, because the budget counted nodes and not bytes | Not sanitized |
| A cut left `someone@examp` or `203.0.113` in text, which no redaction pattern matched | Such alerts aborted collection |
| Non-plain top-level keys raised `ValidationError` from the constructor | Not compared |

The fixes:

- `_row_to_alert` keeps the `id` and `incident_id` a row holds. The digest applies only
  to new alerts, so the database and API agree without a migration. Events bound
  `related_id` with the same digest, because `EventRecord` validation is unchanged.
- Sanitizer work is charged to one budget for the whole structure: every key inspected
  and every value visited against 10,000 nodes, and all text against 16 KiB before it is
  escaped. Going over the text limit stops the walk at once.
- Only an allowlist of leaf types reaches `to_jsonable_python`. Dataclasses and Pydantic
  models are read one level at a time. Anything else is marked, not iterated.
- Sets are sorted on their encoded form, because set order varies between processes.
- A failing container is marked without hiding other values, and a top-level mapping
  always stays a mapping.
- `bounded_text` escapes only the part of the input that can reach the result.
- `redact_text` replaces the word beside a `[TRUNCATED]` marker. The local view keeps it,
  and the rule applies to rows that are already stored.

`safe_structure` was folded into `safe_evidence`, which owns the budget.

Measured once with `tracemalloc` on, Python 3.11.9 on Windows. The first row was 69 s
with serialization before the fix, so those two numbers are not directly comparable:

| Input | Time | Peak |
| --- | --- | --- |
| 20,000 keys shared 9,000 times | 29.5 ms | 0.01 MB |
| Endless generator in a dataclass | 0.2 ms | under 0.01 MB |
| 10 KB string shared 9,990 times | 0.1 ms | 0.02 MB |
| 1 MB string shared 9,990 times | 0.1 ms | under 0.01 MB |
| Shared lists 40 levels deep | 387.5 ms | 1.01 MB |
| 9,999 integers of 1,024 bits | 81.8 ms | 6.93 MB |

Regression tests are in `tests/test_v3_alert_identity.py`,
`tests/test_v3_evidence_budget.py` and `tests/test_v3_redaction_cuts.py`. 29 of the first
62 failed before the fixes, and the rest are guards for behavior that must not change.
Their workloads are bounded: endless inputs are replaced by a counter that raises.
Mutation checks: restoring stored IDs failed 5, the `update_alert` event bound 1, key
charging 1, the text budget 3, shallow dataclass reading 3, shallow model reading 1,
refusing other iterables 2, set sorting 1, failure isolation 2, the top-level mapping 2,
cycle detection 1, the early cut 1 and the redaction rule 11.

## Data-integrity review

Reproduced at `8a233a4` with synthetic data. The rejection path above treated
`TypeError`, `ValueError` and `OverflowError` from a write as a bad record, and this
section replaces that part of it.

| Input at `8a233a4` | Effect |
| --- | --- |
| `TypeError` from `_enforce_retention`, three writes | 0 stored, 3 counted as rejected, storage `healthy` |
| One corrupt stored `timestamp` on a grouped alert | New telemetry rejected as invalid, storage `healthy`. The `ValueError` came from reading the old row |
| Newer update while an old queued version was rejected | Newer version deleted from the queue, so it never reached the database |
| Update to a later record during a flush | A stale copy taken before the update was written first |
| Update during a successful first write | Lost in memory and in the database after the API reported success |
| Update during a failing first write | The retry wrote the old version over it, in memory and in the database |
| Update to a grouped alert during an outage | Lost when the queued write ran, because grouped writes ignored its state |
| Queued writes when logging was saved as off | Stored on the next pass |
| Storage recovery after logging was saved as off | Recovery event stored |
| Failed alert update after logging was saved as off | Whole alert queued for storage |

The fixes:

- `add_alert` and `add_event` check a record before it reaches SQLite. A timestamp, text,
  integer, evidence value or column type that can never be stored raises
  `InvalidRecordError`, and its message holds no record content.
- Only `InvalidRecordError` rejects a record. Every other exception degrades storage and
  keeps the record queued, so an unexpected failure is visible and loses nothing. An
  invalid record queued during an outage is rejected once, and the queue continues.
- A flush reads each entry when it reaches it, and rejects only the version it attempted.
- A first write detects an update that arrived during it and queues the result for the
  next pass. Failed writes queue the live acknowledge and dismiss state.
- Grouped writes keep state carried by the incoming record. A reopened condition still
  clears it.
- A saved opt-out discards queued writes, even if follow-up maintenance fails. A flush in
  progress stops at the next record, and later failures do not queue anything. The
  recovery event stays in live memory.

The Windows event and file integrity paths needed no change. `EventRecord` validation did
not fail for any value their collectors can produce. A `ValidationError` needed a lone
surrogate in a bounded `WindowsEventRecord` field, which the XML parser cannot return, and
was raised building that record, not by the service. `uuid5` raises only for a surrogate
in an ID, and IDs come from three fixed channel names and an integer, or from a UUID.
Both workers already survive a failed pass, and tests now pin this.

Regression tests are in `tests/test_v3_write_classification.py`,
`tests/test_v3_operator_state.py`, `tests/test_v3_queue_opt_out.py` and
`tests/test_v3_worker_alerts.py`. Races are held open with events around the database
write, not timing. Three tests in `tests/test_v3_alert_bounds.py` asserted the old
classification by raising built-in errors, so they now raise `InvalidRecordError`. One
of them covered three error types, which is why the 403 cases became 401 before the 86
new ones.

Mutation checks reverted 18 changes and at least one test failed for each: restoring the
broad catch failed 11, and the integer, text, column type, timestamp type, timestamp
value and evidence checks failed 5, 7, 3, 3, 8 and 7. The rejection identity check failed
3, reading the newest alert 2, keeping acknowledge 2 and dismiss 2, detecting an update 3,
grouped acknowledge 1 and dismiss 1, the saved opt-out 4, the recovery event 1, the late
queue 2 and reading the newest event 1. The update guard failed its test before the fix.

Two mutation runs ended with exit code 1 and no pytest summary or stderr, and the cause
was not found. Neither repeated: 8 unmodified runs and 8 runs of one affected mutant all
completed with a summary, with `faulthandler` on.

## Queue capacity

`MAX_PENDING_WRITES` limits the alert queue and the event queue to 500 entries each.
`_queue_pending` is the only path that evicts on capacity. It removed the oldest entry
silently, so a long outage lost writes with no record. Reproduced at `277571b`:

| Input at `277571b` | Effect |
| --- | --- |
| More than 500 queued alerts or events | Oldest entries dropped with no count or log |
| Evicted alert | Live copy kept `persistence_pending` set for a write that would never run |
| Entry evicted while its write was running | Could be stored after being treated as lost |
| Storage recovery with a full event queue | The recovery notice evicted a queued telemetry event |
| `pending_writes` in `/api/health` during an outage | Stale until the next storage transition |

The fixes:

- `StorageHealth` gains additive `dropped_alert_writes` and `dropped_event_writes`, shown
  by `/api/health` and the telemetry snapshot. They survive storage transitions, so a
  loss stays visible after recovery while `status` returns to `healthy`.
- The dashboard adds the total to the storage status and shows it as a warning, so
  `STORAGE HEALTHY` is not shown in green after data was lost.
- Each eviction is counted once, per queue. Replacing a queued record is not an eviction.
- The entry being written is never evicted. The next oldest entry goes instead, and the
  500 entry limit still holds.
- An evicted alert clears `persistence_pending` on its live copy.
- The recovery notice is not queued into a full queue. It stays in live memory, so a
  housekeeping event cannot cost telemetry.
- `pending_writes` is refreshed whenever a queue changes or is discarded.
- A warning is logged for the first drop and every 100th, per queue. It holds counts only,
  never identifiers or record content.

Counted separately: rejected records stay in `rejected_writes`. Writes discarded by a
saved logging opt-out are not counted, because that is the operator's choice and not
loss, and opted-out writes never reach the queue.

The counts are held in memory and reset to 0 when the process starts. Earlier loss is not
reported after a restart. They count evictions, not records that are permanently missing:
an alert queued again after eviction is a new write, and it counts again if it is evicted.

Regression tests are in `tests/test_v3_queue_capacity.py`, 28 tests, and one Playwright
test covers the dashboard. All 24 tests in the first version failed before the fix, and
the Playwright test failed against the old dashboard. Writes in progress are held open
with events, not timing. Mutation checks reverted 14 changes: removing the alert count
failed 15, the event count 7, the in-flight guard 2, the alert and event write slots 1
each, clearing those slots 1 each, the pending flag 1, the queue refresh 11, the discard
refresh 1, keeping counts across transitions 3, the log bound 1 and the recovery notice
rule 2. The 14th, replacing the snapshot's refresh with `self.storage`, changed nothing,
because `self.storage` is now kept current. The line stays as the original source of
`pending_writes`.

## Open findings

These come from code review and have not been reproduced, unless noted.

- Reproduced: unencodable input to `POST /api/incidents` and
  `POST /api/file-integrity/folders` returns 503. A 422 would describe it better.
- A write failing with a `sqlite3.Error` that is really about the record, not the
  database, is still treated as an outage and retried. Other than `Alert` and
  `EventRecord`, models such as `WindowsEventRecord` have no equivalent normalization.
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
- A rejected record stays in the live view but is not stored. `rejected_writes` counts
  rejections since the last start and does not identify them.
- Text cut at a limit is not recoverable, and truncated evidence is dropped whole. An
  incident built from such an alert has no `related_processes` for it, without notice.
- The redaction rule removes the one word beside a cut. It depends on the existing
  patterns for any earlier part of a value split by spaces, and reports already
  exported are not changed.
- Model computed fields and custom serializers no longer appear in evidence, and types
  outside the allowlist, such as `pydantic_core.Url`, are marked. No alert source is
  known to pass these.
- A long identifier is still escaped and hashed in full. Its digest is unsalted, so a
  redacted report does not hide a guessable identifier.
- `Alert` construction in `app/detection/engine.py` is not covered by the per-alert guard
  in `collect_once`. Its values come from rule definitions.
- One record that keeps failing with an unexpected error blocks the queue behind it.
  Storage stays degraded and retries, so this is visible. Past 500 entries the oldest
  are evicted and counted, but which records were lost is not reported.
- Dropped write counts reset on restart, and they are not stored, so an operator who
  restarts during an outage loses that evidence.
- An update to an evicted alert reaches the database only if its row already exists.
- The storage recovery notice is not stored when the event queue is full.
- `history_dropped_samples` is counted but not shown by `/api/health`.
- An update saved by the next pass after a first write is lost if the process stops
  first. The queue is memory only.
- Writing a grouped alert again adds its count again, so a retry after a race or outage
  can inflate `occurrence_count`.
- An update to a record that was rejected stays in the live view only.
- The storage failure alert keeps `persistence_pending` set although it is never queued.
- An in-flight write finishes after an opt-out, and discarded queue entries are gone.
- The three worker loops log with `LOGGER.exception`, so a traceback could carry
  exception text. No path reviewed here reaches them with record content.
- Rows stored with longer text are bounded when read. The stored row is not rewritten.
- Evidence mutated after construction bypasses the model boundary, and one such alert
  makes the whole `/api/alerts` response a 503. No application code does this.
- `telemetry_degraded` covers snapshots that exist but cannot be served. The dashboard
  has no watchdog for frames that stop arriving while the socket stays open, so it can
  still show `CONNECTED` if collection itself stops.
- During a serialization failure the dashboard keeps the last rendered values under the
  degraded status. Playwright covers this path with synthetic frames only.
