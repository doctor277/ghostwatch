# V3 resilience audit

Engineering record for `feat/v3-resilience-hardening`, based on
`b97ab88fffe5bb50fdc26855e3ee8ce3bc4dc3a1`. That branch is published as draft
[PR #7](https://github.com/doctor277/ghostwatch/pull/7) at `c162eed`, which was open and
unmerged at the last check on 2026-11-05. Sections dated after that commit describe
local commits only, and no further push is authorized. No changes to production V2.

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
- VERIFIED: hosted run
  [41974485263](https://github.com/doctor277/ghostwatch/actions/runs/41974485263) at
  `c162eed`: Python 3.11.9 and 3.13.17 each 601 passed, Chromium 17 passed, with Ruff,
  compilation and JavaScript syntax passing. This is the latest published CI. It covers
  that commit only, not the local commits below.
- LOCAL ONLY: `31b26bd` merge-gate fixes, `7779a4a` telemetry freshness watchdog and
  `0332aa8` synthetic persistence benchmark. None has run on hosted CI or Python 3.13.
  Results are in the sections below and in
  [Current Beta Validation](CURRENT_BETA_VALIDATION.md#local-only-results---november-7-2026).
- LOCAL ONLY: history row-cap retention fix on `fix/v3-history-performance`, based on
  `7bae229` (PR #7 merged). Python 3.11.9 on Windows: 663 pytest passed, 41 Playwright
  tests passed against the synthetic server, Ruff, compilation and JavaScript syntax
  passed. Hosted CI and Python 3.13 have not run on it. See
  [History row-cap retention](#history-row-cap-retention-2026-11-09).
- LOCAL ONLY: threshold alert lifecycle fix on `fix/v3-alert-lifecycle`, based on
  `22a20a2`. Python 3.11.9 on Windows: 684 pytest passed, 42 Playwright tests passed
  against the synthetic server, Ruff, compilation and
  JavaScript syntax passed. Hosted CI and Python 3.13 have not run on it. See
  [Threshold alert lifecycle](#threshold-alert-lifecycle-2026-11-10).
- PENDING: open findings below. A supply-chain review of these changes is not recorded
  here.

Run 41888969539 above describes the portfolio branch, not the engineering changes.

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

## Persistence and privacy follow-up (F1-F4)

Recovered the interrupted local session at `1efd488b4c75581ec4cc9d783df2e9bda06f238e`
on `feat/v3-resilience-hardening`. The only uncommitted work was the 875-line
`tests/test_v3_persistence_races.py`; there were no tracked implementation edits or
untracked scratch scripts. Existing ignored artifacts included older pytest XML,
test directories and environments; they were preserved. A fresh run of the original
suite confirmed 515 passed, 0 failed and 0 skipped on Windows / Python 3.11.9.

The first targeted run exposed an unbounded wait for operator replay, which did not
exist yet. It was stopped; a run excluding that one case completed with 41 failed,
12 passed and 1 deselected. The failures included missing patch-queue helpers that
the interrupted test-first implementation had not supplied, as well as these
observable defects, all using synthetic records:

| Finding | Reproduction at the recovered HEAD | Root cause and correction |
| --- | --- | --- |
| F1, high: logging opt-out race | A held event write fails after a saved opt-out; recovery stores the event. A paired event also starts after its alert's in-flight write crosses the opt-out. | Persistence entry points and flushes did not enforce the setting themselves. They now check it before dispatch, discard queues when loading a saved opt-out, and use an opt-out generation to prevent failed writes or old operator audit events from reviving after an off/on toggle. Failures still degrade storage and report only the error category. |
| F2, medium: incident relationship regression | An operator change overlaps the first alert write, an incident links the row, then reconciliation overwrites its `incident_id` with `None`. | Operator replay wrote a stale whole alert. State now replays through `Database.update_alert`, which only changes the requested booleans. Observation writes preserve the stored relationship, or recover a link from incident membership if the incident predates the alert row. Creating an incident also updates the live copy. |
| F3, medium: grouped double count | Acknowledge or dismiss during one grouped write produces a stored count of 2 for one occurrence; an update to a queued occurrence can produce 3 instead of 2. | Operator reconciliation reused the additive observation path. Only observation deltas now reach `add_alert`. A delta committed during a flush is subtracted from any newer aggregate still queued, so the next pass writes only unwritten occurrences. Concurrent flushes and state patches share an async write lock. |
| F4, medium: grouped boolean reversals | An explicit `False` update fails during an outage and returns as `True` after recovery. | Boolean OR merged stale records, and changes made while already degraded had no replay path. A bounded state-patch queue now preserves explicit `True` and `False`, merges partial changes in order, and removes only the patch version successfully attempted. Grouped observations retain stored operator state; reopening queues an explicit reset before later operator decisions. |

`Database.add_alert` now uses `BEGIN IMMEDIATE` around the relationship lookup,
grouped read, upsert and retention. A zero-timeout second connection demonstrated
that the old grouped read allowed an intervening link write; the regression now
requires the write lock. Exceptions roll back the transaction. Malformed incident
membership JSON is ignored by this lookup without decoding unrelated incidents.
No schema migration or REST/WebSocket model change was introduced.

Additional deterministic regressions cover an observation queued during operator
replay, chronological recovery after a failed first write, rejection with a live
operator decision, and the existing empty-update lookup contract. History opt-outs
also discard waiting batches and prevent baseline follow-up after an in-flight batch
crosses the opt-out; enabling history again does not revive discarded samples.
Already-dispatched database work is allowed to complete.

All 54 recovered test cases were retained. Their event barriers now have bounded
waits. One assertion incorrectly counted only the operator patch while audit and
recovery events were also pending; it now includes those events. Older tests in
`test_v3_operator_state.py` and `test_v3_queue_capacity.py` were updated to inspect
partial state patches. Rejection-identity tests now supply an actual newer observation,
because changing acknowledgement cannot repair an invalid observation. Their
rejection, persisted-state and queue-capacity assertions remain; an added test checks
that a rejected observation still keeps the operator decision in the live view.

The new file has 60 passing cases: all 54 recovered cases plus six edge cases.
The previous grouped-count limitation below is resolved for operator replay and
normal transactional retries. These changes do not provide a durable retry journal:
pending observations and patches are still bounded memory, and process termination
can lose them. Patch evictions use the existing dropped-alert-write counter; each
of the alert, event and operator-patch queues has its own 500-entry limit. Unlinked
observations inspect incident membership lists; cost on very large incident archives
was not benchmarked at this point. See
[the later benchmark](#synthetic-persistence-benchmark-2026-11-07). Historical counts or relationships corrupted before this
fix are not rewritten automatically.

Final local validation on Windows / Python 3.11.9:

| Check | Result |
| --- | --- |
| Original suite, excluding the recovered test file | 515 passed, 0 failed, 0 skipped |
| Targeted persistence/privacy races | 60 passed, 0 failed, 0 skipped |
| Full Python suite | 575 passed, 0 failed, 0 skipped |
| Explicit database/privacy regression subset | 124 passed, 0 failed, 0 skipped |
| Desktop and mobile Chromium | 17 passed, 0 failed, 0 skipped |
| Ruff (`python -m ruff check .`) | Passed |
| Compilation (`python -m compileall -q app scripts tests`) | Passed |
| JavaScript syntax (`node --check`) | 8 files passed |
| Public inventory / existing sensitive-payload scanner | 114 files passed; MIT License and approved demo screenshot included; no historical concept images |
| Whitespace validation (`git diff --check`) | Passed |

All pytest runs used distinct workspace-local `--basetemp` directories, avoiding the
previous system temporary-directory permission problem. The database/privacy subset
ran `test_database`, `test_v3_history`, `test_v3_hardening`, `test_v3_p0`,
`test_v3_redaction_cuts`, `test_v3_queue_opt_out`, `test_v3_write_classification`,
`test_v3_file_integrity` and `test_websocket`. The full suite also covered migration,
rollback, retention, integrity, release packaging and other privacy regressions.
The full suite and subset each emitted one existing Starlette/httpx TestClient
deprecation warning. Chromium used the existing loopback-only synthetic server;
its 17 cases do not validate real system telemetry. Other Python versions and hosted
CI were not run for this follow-up. No frontend source, production V2, license,
network-binding, Host/Origin, WebSocket-origin or local-only AI configuration changed.

These fixes are suitable for further review in draft PR #7, subject to the documented
limitations and hosted CI after a separately approved push. This takeover made no
remote requests, push, merge, release or PR-state changes.

## Final reproduced race gaps

Follow-up at `d9f58432ef0764707d96371dcb743650af0a4dc0`. A read-only review
found schedules missing from the earlier 60-case persistence suite. These are the
four findings numbered F1-F4 in the final review; their numbering differs from the
earlier audit above. No existing regression tests were removed or weakened.

`tests/test_v3_final_races.py` holds database operations before execution or after
their real read/commit with bounded thread/async events. At the original HEAD, the
first 20 valid cases produced **17 failed, 3 passed**, including all four reported
defects. The passing cases protected ordinary idle/missing responses and rollback.
An initial configuration test used an unknown model field; it was corrected to the
validated `cooldown_seconds` field before recording that red result. Additional
coverage brought the new file to 26 cases.

| Finding | Reproduction before correction | Correction |
| --- | --- | --- |
| F1: stale settings recovery | Hold a recovery read of logging ON, save OFF, then release it. Runtime becomes ON while SQLite remains OFF, allowing new events. ON and OFF/ON schedules also restore stale unrelated thresholds. | Each settings model has a save generation. Recovery applies a whole loaded model only if no newer successful save occurred during its read. All six configuration models use this guard; failed saves do not advance it. There is no lock held across settings I/O. |
| F2: false 404 for an existing alert | An alert exists only in SQLite. While an unrelated write owns the persistence lock, PATCH returns 404 although its queued change later persists. | Resolve an absent live record through SQLite before modifying/queuing it. Merge any newer live state and pending patch after the lookup, then return a real record with the requested state. Missing records remain 404 without a phantom patch. Lookup failures return a sanitized 503. With logging OFF, an existing stored record can be updated in live memory without persisting the change. |
| F3: stale incident API relationship | Incident A commits but its completion is held. B commits and becomes the stored relationship; A then restores its older ID in live state. Acknowledgement continues returning that old link. | After incident creation, read committed alert relationships instead of publishing the operation's own ID. A transient per-alert refresh token prevents an older lookup from replacing a newer result, including overlapping refreshes for different alerts. Normal alert reconciliation accepts the database link unless live state actually changed during the write. Both incident history records remain intact. |
| F4: cancelled recovery double count | A grouped write is dispatched, `stop()` cancels its producer, the thread commits, then `start()` retries the unreconciled delta: count 1 becomes 2 without another observation. | One service-owned task holds the existing alert persistence lock across the database operation and queue reconciliation. Callers await it through `asyncio.shield`; caller cancellation still propagates immediately. Stop/start cannot dispatch a competing retry while the owner is pending. Successful commits remove their delta; failures retain retryable work. |

Cancellation coverage includes restart before and after the held operation finishes,
both successful commit and rollback, a genuinely new occurrence, cancellation of a
first write, and a newer explicit Boolean reversal during a cancelled operator patch.
Workers and collection are synthetic, and stop remains bounded while the held write
is unfinished. The owner task's callback retrieves unexpected errors and reports only
their type through the existing storage-health path.

Two additional tests protect concurrent incident refreshes, with overlapping and
disjoint alert IDs. Lookup privacy coverage also caught an OFF/ON transition during
the new persisted-alert lookup: an operator request started before opt-out must not
queue its old patch after logging returns. That case failed before the generation
check was added. No new event or alert contents are logged.

The incident lookup reuses the existing membership fallback for incidents created
before their alert row. Its batch read uses a single SQLite read transaction;
`add_alert` retains its `BEGIN IMMEDIATE` transaction for grouped writes, links and
retention. No schema migration, durable journal, frontend change or unrelated
persistence subsystem was introduced.

Final validation on Windows / Python 3.11.9:

| Check | Result |
| --- | --- |
| New final-race regressions | 26 passed, 0 failed, 0 skipped |
| Related persistence, lifecycle, service and storage regressions | 228 passed, 0 failed, 0 skipped |
| Full Python suite | 601 passed, 0 failed, 0 skipped |
| Explicit database/privacy regression subset | 124 passed, 0 failed, 0 skipped |
| Desktop/mobile Chromium | 17 passed, 0 failed, 0 skipped |
| Ruff | Passed |
| Python compilation (`app scripts tests`) | Passed |
| JavaScript syntax | 8 files passed |
| Sensitive-payload scan / public inventory | 115 files passed; MIT License and approved demo screenshot retained |
| Whitespace validation | Passed |

All pytest runs used distinct accessible workspace-local `--basetemp` directories.
The full suite, related suite and database/privacy subset each reported one existing
Starlette/httpx TestClient deprecation warning. Chromium used the existing synthetic
loopback server. Hosted CI and other Python versions have not run on these changes.

Limits: queues and operation ownership remain in memory. Abrupt process termination
can still lose queued work; this does not provide durable exactly-once ingestion.
An already-dispatched transaction can finish after stop or opt-out. Stop does not wait
indefinitely for it: reconciliation continues while the same event loop is alive,
and a later start on that service respects the active owner. Historical incorrect
counts/links are not repaired automatically. A failed post-commit incident refresh
returns 503 explicitly stating that the incident was persisted; blindly retrying
creation can create another incident. Large incident archives were unbenchmarked at
this point; see [the later benchmark](#synthetic-persistence-benchmark-2026-11-07).

This follow-up was local when written and is now part of published `c162eed`, covered by
hosted run 41974485263. PR #7 remained draft and unmerged by instruction; no production
V2 change is part of this work.

## Merge-gate recheck, 2026-11-05

Bounded local recheck of `d9f5843..c162eed`. `HEAD` was confirmed to be
`c162eed9704bee6fe4d387f38f478f884c3d811d` with read-only Git commands and no fetch.
Scope was the reviewed diff in `app/service.py`, `app/storage/database.py` and
`app/api/routes.py`, plus directly related logic. All data was synthetic, and no crash
or power-loss experiment was run.

**Recommendation for code quality: APPROVE WITH FOLLOW-UPS.** This is not a GitHub
approval. Hosted CI was not rerun, so its 601/17 results describe only the published
SHA and not this tree. The state of PR #7 was not rechecked. The fixes below were
uncommitted at the time of writing and are now local commit `31b26bd`. Automatic review notes rejected two earlier
revisions: the one with defects 1-3 only, because of defect 4, and the one with defects
1-6, because of defect 7. Both notes were treated as unverified claims and reproduced
with deterministic tests before any fix. Defects 4-6 were the second focused attempt on
one schedule 3 gap. Defect 7 is a separate gap, write completion rather than lookup,
fixed at its first attempt. Independent agent review is not part of this workflow
(owner instruction, 2026-11-05), so the recommendation rests on the tests and evidence
recorded here.

Interpreter: Windows Python **3.11.9 only**, using an existing virtual environment from
the sibling checkout, because the worktree has none. Nothing was installed, and
bytecode writes were disabled for every pytest run. Python 3.13 was not run: `py -0` lists only
3.11.9 and 3.14.3, and the installed 3.14 has no project dependencies. Playwright was
not run; this task made no frontend change.

### Results

Every pytest run below used its own workspace-local `--basetemp` and
`-p no:cacheprovider`. The right column ran under `.tmp/t01-r4/`. In the `c162eed`
column, the persistence, full-suite, Ruff and compile rows come from the earlier run on
a pristine `git archive` copy under `.tmp/t01-r2/`. The final-race cell's current
figure ran the current test file against the `c162eed` `app/service.py`.

| Check | At `c162eed` | With the seven fixes |
| --- | --- | --- |
| `python -m pytest tests/test_v3_final_races.py -q` | Original file: 26 passed, 0 failed, 0 skipped. Current file: 28 passed, 18 failed, 0 skipped | 46 passed, 0 failed, 0 skipped |
| `python -m pytest tests/test_v3_persistence_races.py -q` | 60 passed, 0 failed, 0 skipped | 60 passed, 0 failed, 0 skipped |
| `python -m pytest -q` | 601 passed, 0 failed, 0 skipped, 1 warning | 621 passed, 0 failed, 0 skipped, 1 warning |
| `python -m ruff check .` | Passed | Passed |
| `python -m compileall -q app scripts tests` | Passed | Passed |
| `python -m pytest .tmp/t01-r3/test_probes.py .tmp/t01-r3/test_reobserve_probe.py .tmp/t01-r3/test_gap_probe.py -q --rootdir=. -c pyproject.toml` | Not rerun | 22 passed, 0 failed, 0 skipped, 1 warning |

The warning is the known Starlette/httpx deprecation. The first probe run reported
6 failures, 2 of them mistakes in the probes: a wrong `create_app` keyword, and a
producer that re-fired on restart and so made a real second observation. The table
counts the corrected probes. They live in the ignored `.tmp/t01-r3/` directory and
are not part of the repository; the seven defects are covered in `tests/test_v3_final_races.py`.
That file then passed two further repeat runs, 46 passed each.

**`--basetemp` is required on this machine.** Without it, a run can report mass setup
errors unrelated to the code. The orchestrator's
`python -m pytest -q` gate reported 186 passed and 420 errors here, with no failed test in
its captured log. `%LOCALAPPDATA%\Temp\pytest-of-<user>` denies access on this machine:
rerunning `tests/test_websocket.py` that way gave 1 passed and 4 errors, each
`PermissionError: [WinError 5]` on that directory. The table above is the valid result.

### Schedule coverage

Assertions were read, not inferred from test names.

| Schedule | Direct coverage | Added independent probe | Outcome |
| --- | --- | --- | --- |
| 1. Slow recovery vs newer saved logging OFF, ON, OFF->ON | `test_settings_save_supersedes_older_recovery_read` asserts disk and memory equal the saved model and that a later event persists only when logging is on. `test_other_saved_configuration_survives_recovery` covers the other models. `test_opt_out_during_alert_lookup_cannot_revive_the_patch` covers the patch path. | `test_s1_queued_work_is_not_revived_by_slow_recovery` (queued alert and event stay discarded), `test_s1_save_commit_straddling_a_complete_recovery_read` | No defect |
| 2. SQLite-only PATCH under a busy write, 404, sanitized 503 | `test_persisted_only_alert_patch_response`, `test_persisted_only_patch_while_logging_off_is_live_only`, `test_alert_lookup_failure_is_unavailable_not_missing`. The busy case holds the write outside the real `Database._lock`, so this is partly indirect. | `test_s2_real_database_lock_held_does_not_stall_loop_or_invent_a_record`, `test_s2_degraded_but_readable_store_returns_the_record_and_defers_the_write`, `test_s2_failure_leaves_no_phantom_state`, `test_s2_end_to_end_503_body_is_sanitized` | Defects 2 and 3 below, fixed |
| 3. Concurrent incidents match the committed link, including after acknowledgement | `test_two_incident_completions_keep_the_committed_link`, `test_acknowledgement_reconciles_an_authoritative_link`, `test_delayed_incident_refresh_cannot_replace_a_newer_refresh` assert the committed link in database, live state and API response. | `test_s3_lookup_cannot_cache_a_link_older_than_a_committed_incident`, `test_s3_delayed_observation_cannot_restore_the_older_link`, `test_reobserved_linked_alert_while_degraded`, `test_gap`. The write-completion schedule of defect 7 is in the repository as `test_direct_write_cannot_restore_a_link_older_than_an_incident`, `test_flush_cannot_restore_a_link_older_than_an_incident` and `test_patch_write_cannot_restore_a_link_older_than_an_incident`. | Defects 1, 4, 5, 6 and 7 below, fixed |
| 4. Cancelled grouped write across same-loop stop/start | `test_cancelled_recovery_counts_each_observation_once` asserts count 1, then exactly 2 after a new detection, for commit and rollback in both restart orders. `test_cancelled_first_write_keeps_a_genuinely_new_occurrence`, `test_cancelled_operator_patch_preserves_a_newer_reversal`. | `test_s4_direct_write_cancelled_by_stop` covers the direct write path, which the first test does not | No defect |

### Reproduced defects

Each new test failed before the fix and passes after it, except two guards: 18 failed
and 28 passed in `tests/test_v3_final_races.py` against the `c162eed` service. Defect 3
also failed with the fix for defect 2 alone. The tests for defects 4 and 5 and for the
first form of defect 6 failed with defects 1-3 fixed: 6 failed and 32 passed. The
`patch-logging-on` case already passed, because the patch write returns the committed
row. `test_unreadable_link_does_not_block_a_live_alert_patch` already passed, because
it pins existing behavior that the defect 6 fix must keep. The 4 tests for defect 7
failed on the stale link with defects 1-6 fixed. With only that fix disabled in a
disposable copy, its 5 tests failed and the other 41 passed.

| Defect | Reproduction | Fix |
| --- | --- | --- |
| 1, schedule 3: a lookup caches an older incident link | PATCH of an alert absent from live memory reads the row. An incident then commits and refreshes, and the refresh skips the alert because it is not live. The old read is then stored in live memory. With an empty PATCH, or with logging off, nothing reconciles it, and live state overrides SQLite in API responses until the alert leaves live memory. | `_read_alert` repeats a read that any incident refresh overtook. This needs no await between the check and caching. `test_persisted_only_lookup_cannot_cache_an_older_incident_link` |
| 2, schedule 2: a queued alert is reported missing | `MAX_LIVE_ALERTS` is 200 and each queue holds 500. An outage queuing more than 200 distinct alerts evicts live copies, so a PATCH of one returned 404 and lost the operator action, though the alert was not truly missing. | `_read_alert` falls back to the pending alert when SQLite has no row. `test_alert_queued_beyond_live_capacity_is_not_missing` |
| 3, schedule 2: a flush overtakes that lookup | With the same evicted alert, the SQLite read finds no row, then a flush commits the alert and removes it from the queue before the read resumes. Neither source had it, so a PATCH returned 404 for a stored alert and lost the action. | `_read_alert` also repeats a read if the queued entry changed. The repeat finds the committed row, and an alert discarded by an opt-out remains a true 404. `test_queued_alert_flushed_during_lookup_is_not_missing` |
| 4, schedule 3: a restored queued alert loses its link | Found by review. While storage is degraded but readable, an incident commits for a queued alert with no row, and the refresh updates only the live copy. More queued alerts evict that copy. PATCH then restored the queued copy and returned and cached `incident_id=None`. The retry fence cannot help, because the incident finished before the lookup. | `_read_alert` applies the committed link to the queued copy. A failed link read is a sanitized 503 and restores nothing. `test_restored_queued_alert_keeps_the_committed_incident_link`, `test_link_lookup_failure_restores_no_queued_alert` |
| 5, schedule 3: a write accepted under a newer queued copy | Same family, found by probe. `_accept_stored_alert` adopted the queued copy and its older link when the alert was not live. | The accepted result always takes the stored link. `test_write_accepted_under_a_newer_queued_copy_keeps_the_committed_link` |
| 6, schedule 3: a re-observation hides the link | Same family, found by probe. While degraded, a new observation replaced the live copy and dropped its link until a flush. If the alert had been evicted, the observation had no link to inherit, and PATCH returned `incident_id=None` over a committed link. | Observations inherit the live link. PATCH of a queued, unlinked live alert first runs the existing `_refresh_incident_links`. If that read fails, the PATCH still applies to the live copy. `test_reobservation_while_degraded_keeps_the_committed_link`, `test_reobservation_after_eviction_reconciles_on_patch`, `test_unreadable_link_does_not_block_a_live_alert_patch` |
| 7, schedule 3: a finishing write restores an older link | Found by review and reproduced on all three accept paths. A write commits link A and pauses. More alerts evict the live copy, and incident B commits, so SQLite holds B but the refresh skips the absent alert. Accepting the write then restored A. Live state overrides SQLite in API responses, and PATCH refreshes only an empty link, so an acknowledgement also returned A. The lookup fence for defect 1 does not cover write completions. | Each write records the refresh sequence before dispatch. `_reconcile_written_link` runs the existing `_refresh_incident_links` after accepting a write that a refresh overlapped, and does no read otherwise. A failed read degrades storage but leaves the committed write accepted. `test_direct_write_cannot_restore_a_link_older_than_an_incident`, `test_flush_cannot_restore_a_link_older_than_an_incident`, `test_patch_write_cannot_restore_a_link_older_than_an_incident`, `test_failed_link_reconcile_keeps_the_committed_write` |

### Schedule 5 findings

No deadlock, unbounded stop or privacy bypass was found. Confirmed by reading the code
and by the `test_s5_*` probes, except where stated:

- **Deadlock:** `_alert_persistence_lock` is taken only inside the owner task, and an
  owner starts only when none is busy, so it is not contended. Every other path queues
  instead of waiting. `Database._lock` is not re-entrant, but `_incident_for_alert`
  takes no lock and `update_alert` calls `get_alert` after releasing it. An unexpected
  owner exception releases the lock and degrades storage, including when the caller
  was already cancelled.
- **Stop:** `stop()` cancels producers, which await the owner through `asyncio.shield`,
  so a held write does not delay it. A held patch returned from stop within the 1 s
  probe limit. A blocked SQLite thread cannot be interrupted; connections use a 2 s
  busy timeout, and the work queued meanwhile stays within the 500-entry limits.
- **Privacy:** `_write_alert`, `_write_alert_update` and each flush iteration recheck
  the setting or generation before dispatch. With an opt-out during a held flush, only
  the already-dispatched alert was stored; later alerts and the queued event were not.
  That dispatched write is the documented exception. The configuration fence covers
  every assignment: outside `__init__`, the models are set only through
  `_apply_saved_configuration` and `_load_configuration`.
- **Revised fixes:** the link reads added for defects 4 and 6 hold no service lock, so
  they add no lock order. A slow read delays only that PATCH, within the 2 s busy
  timeout when another writer holds SQLite. The PATCH generation is taken before those
  reads, so an opt-out during one still stops the patch from being queued. This item
  rests on reading the code and on the full suite; no probe holds those reads.
- **Defect 7 fix:** `_reconcile_written_link` only reads, so it cannot store anything
  after an opt-out. It runs inside the owner task while `_alert_persistence_lock` is
  held, but `_refresh_incident_links` never takes that lock, so it adds no wait cycle.
  A slow read extends that owner's work by at most the 2 s busy timeout, and producers
  meanwhile queue instead of waiting. `stop()` does not await it. Each write makes at
  most one such read. This rests on reading the code and on the full suite; no probe
  holds that read.

### Follow-ups

None blocks the local beta candidate. All are low severity.

- Not reproduced: `_persist_event` is not routed through the owner. If its caller is
  cancelled and the dispatched write then fails, the event is neither queued nor
  counted. This can lose an audit event, but cannot breach an opt-out.
- `_read_alert` has no fixed retry limit. Each retry needs an incident refresh or queue
  change that overlaps the read, so it is limited by local activity, not by a constant.
  It holds no lock while retrying and is not awaited by `stop()`.
- A PATCH of a queued alert absent from live memory still returns the sanitized 503
  while SQLite is unreadable, because the lookup fails first. This is honest, but the
  patch is not queued.
- A linked alert that was evicted and then re-observed during an outage shows no link
  in alert lists until a PATCH of it or the next successful flush. Observations do not
  read SQLite, which keeps them safe during an outage. SQLite holds the correct link
  throughout. If SQLite is unreadable, a PATCH response has the same gap.
- If the read after an overtaken write fails, storage degrades and the older link can
  stay in the live copy until the next write of that alert or its eviction. SQLite
  holds the correct link throughout. `test_failed_link_reconcile_keeps_the_committed_write`
  checks the commit and the degraded state, not that live link.
- A failed link read in PATCH goes through `_mark_storage_failure`, as the lookup path
  does, which restarts the storage retry delay.
- Persistence ownership is bound to one event loop. Stop/start on a different loop
  remains unsupported.

## Synthetic persistence benchmark, 2026-11-07

New local measurements from this run, on `7779a4a` plus the benchmark script, since
committed as `0332aa8`. Nothing here is hosted CI, and nothing was reused from the older 10k-socket
results. No application code, schema or index changed.

**Decision: no index or schema change.** The JSON scan is real and grows with the
archive, but it stays within normal write cost at sizes a single operator can reach.
Incidents come only from `POST /api/incidents`, one at a time. No path deletes them, so
the archive only grows, and the finding stays open for a future review.

### Environment and commands

| Item | Value |
| --- | --- |
| Platform | Windows 11 Home 10.0.26300 (`platform` reports `Windows-10`), Intel i7-1360P, 12 cores, 16 logical, 16 GB RAM |
| Runtime | Python 3.11.9, SQLite 3.45.1 |
| Storage | Disposable directories under the ignored `.tmp/`, on a path inside a OneDrive folder. Latency includes file open and commit cost there, so compare rows rather than quoting absolute values |
| Data | Synthetic rows only; no host data read and no real collector run |

All commands ran from the repository root, with a hard outer `timeout`:

```powershell
python -m scripts.benchmark_endurance
python -m scripts.benchmark_sockets
python -m scripts.benchmark_persistence
python -m scripts.benchmark_persistence --archive 3:1 --samples 5 --max-case-seconds 1
```

Both existing scripts need `python -m` from the root, because they import `app`. They
were not changed. The third command is the one repeatable benchmark command. Allow 20
minutes: it uses the default archives, 200 samples and a 5 second cap per case, except
that every case takes at least 5 samples. That floor is why slow cases overrun the cap.

**The archive figures and the outage figures come from separate runs.** The third
command hit its 1,020 second `timeout` (exit 124) after all seven archives printed, when
the outage still ran last. The fourth command then ran the outage alone, exit 0, in 22.8
seconds. The script now runs the outage first and prints it at once, so a timeout cannot
lose it again. That reorder was checked by the tests and the short command only. **A
complete default run in the final order was NOT VERIFIED**, because it did not fit the
remaining session time.

### Existing socket benchmarks

| Script | Input | Elapsed | Latency | Traced memory | Collector bounds |
| --- | --- | --- | --- | --- | --- |
| `benchmark_endurance` | 10,000 sockets, 20 repeats | 41.2 s | min 683.75, median 2,020.47, p95 and max 2,182.20 ms | peak 8,587,086 bytes, current 3,447,217, growth 159,360 | lifecycle 10,000, recently closed 100, details 250 |
| `benchmark_sockets` | 500 / 1,000 / 5,000 / 10,000 sockets, one pass each | 1.9 s | 57.37 / 94.10 / 297.44 / 678.00 ms | peak 786,164 / 889,420 / 3,054,292 / 4,915,088 bytes | details 250 at every size |

Both run under `tracemalloc`, which inflates latency. With 20 repeats, p95 is the maximum.
An earlier pass on the same commit gave an endurance median of 2,137.53 ms and a maximum
of 3,949.00 ms, so treat the maximum as noisy.

### Incident lookups

Each cell is p50 / p95 / max in ms, with the sample count in brackets. `stored_link` is
an alert row that already holds its link. `member_newest` and `member_oldest` are not
linked in `alerts` and sit in the first and last incidents scanned. `miss` is in no
incident, the normal path for each new alert. With under 20 samples, p95 is the maximum.

| Incidents x members | `add_alert/stored_link` | `add_alert/member_newest` | `add_alert/member_oldest` | `add_alert/miss` |
| --- | --- | --- | --- | --- |
| 100 x 2 | 11.667 / 21.641 / 25.269 (200) | 14.639 / 22.358 / 88.337 (174) | 14.66 / 22.346 / 101.354 (174) | 16.53 / 26.787 / 87.494 (148) |
| 100 x 100 | 16.011 / 25.102 / 73.369 (200) | 13.999 / 23.632 / 147.133 (164) | 18.596 / 28.053 / 36.293 (149) | 19.002 / 29.117 / 35.844 (152) |
| 1,000 x 2 | 16.021 / 24.066 / 109.238 (200) | 14.231 / 23.221 / 45.523 (170) | 18.989 / 30.92 / 130.918 (139) | 17.166 / 25.206 / 31.081 (160) |
| 1,000 x 100 | 14.297 / 22.37 / 61.273 (200) | 14.694 / 27.426 / 69.699 (165) | 54.282 / 80.119 / 169.009 (70) | 54.618 / 78.035 / 96.36 (73) |
| 10,000 x 2 | 13.996 / 22.547 / 45.128 (200) | 14.142 / 25.745 / 121.405 (168) | 35.733 / 50.804 / 135.995 (97) | 34.566 / 53.969 / 66.375 (99) |
| 10,000 x 100 | 14.808 / 24.547 / 268.452 (200) | 15.309 / 20.82 / 26.463 (168) | 404.28 / 439.021 / 439.021 (12) | 391.396 / 433.754 / 433.754 (13) |
| 100,000 x 2 | 9.275 / 21.448 / 51.33 (200) | 7.916 / 12.361 / 99.727 (200) | 205.681 / 273.39 / 298.651 (24) | 208.447 / 255.039 / 272.388 (22) |

| Incidents x members | `get_alert_incident_ids[1]/stored_link` | `get_alert_incident_ids[1]/miss` | `get_alert_incident_ids[100]/stored_link` | `get_alert_incident_ids[100]/member_oldest` | `get_alert_incident_ids[100]/miss` |
| --- | --- | --- | --- | --- | --- |
| 100 x 2 | 4.423 / 9.291 / 13.287 (200) | 5.521 / 11.839 / 16.415 (200) | 6.446 / 13.898 / 33.984 (200) | 15.386 / 22.314 / 30.804 (200) | 18.626 / 28.364 / 35.507 (200) |
| 100 x 100 | 4.596 / 8.545 / 12.007 (200) | 8.256 / 14.139 / 17.261 (200) | 5.455 / 9.989 / 12.629 (200) | 273.308 / 311.86 / 311.86 (19) | 264.848 / 323.085 / 323.085 (19) |
| 1,000 x 2 | 5.071 / 12.68 / 14.87 (200) | 6.419 / 12.702 / 17.142 (200) | 5.299 / 10.346 / 15.315 (200) | 122.935 / 149.955 / 167.335 (40) | 124.259 / 155.808 / 158.743 (40) |
| 1,000 x 100 | 4.591 / 7.507 / 12.741 (200) | 43.248 / 54.707 / 67.669 (111) | 5.399 / 9.347 / 15.548 (200) | 3831.647 / 4004.959 / 4004.959 (5) | 3797.776 / 4027.418 / 4027.418 (5) |
| 10,000 x 2 | 4.635 / 6.014 / 11.805 (200) | 22.261 / 29.3 / 42.326 (200) | 5.375 / 9.453 / 14.756 (200) | 1814.485 / 1890.545 / 1890.545 (5) | 1762.86 / 1900.464 / 1900.464 (5) |
| 10,000 x 100 | 4.647 / 8.485 / 11.895 (200) | 385.904 / 449.744 / 449.744 (13) | 5.895 / 11.42 / 94.63 (200) | 39356.572 / 44994.317 / 44994.317 (5) | 40241.756 / 45746.555 / 45746.555 (5) |
| 100,000 x 2 | 5.514 / 11.231 / 19.538 (200) | 193.079 / 244.188 / 261.176 (26) | 5.776 / 11.266 / 27.88 (200) | 18225.597 / 18877.753 / 18877.753 (5) | 19035.443 / 19392.772 / 19392.772 (5) |

| Incidents x members | `add_incident/hit` | `add_incident/miss` | `initialize` |
| --- | --- | --- | --- |
| 100 x 2 | 14.268 / 20.921 / 25.772 (115) | 11.777 / 26.754 / 59.307 (134) | 57.743 / 67.584 / 67.584 (5) |
| 100 x 100 | 16.447 / 24.825 / 41.786 (106) | 11.49 / 18.869 / 32.122 (140) | 55.292 / 59.84 / 59.84 (5) |
| 1,000 x 2 | 12.839 / 19.274 / 73.491 (127) | 11.443 / 17.058 / 100.805 (145) | 76.319 / 80.11 / 80.11 (5) |
| 1,000 x 100 | 13.906 / 22.475 / 30.906 (118) | 11.084 / 15.553 / 21.979 (153) | 110.12 / 126.257 / 126.257 (5) |
| 10,000 x 2 | 14.578 / 18.515 / 22.87 (120) | 11.97 / 18.785 / 124.298 (140) | 290.119 / 329.235 / 329.235 (5) |
| 10,000 x 100 | 16.022 / 24.973 / 86.053 (104) | 11.845 / 20.555 / 115.402 (137) | 724.957 / 743.304 / 743.304 (5) |
| 100,000 x 2 | 16.069 / 34.566 / 105.137 (99) | 11.425 / 19.613 / 143.793 (141) | 2840.478 / 2888.141 / 2888.141 (5) |

`add_incident` does not scan: hit and miss stay flat across all sizes. `initialize` is
measured because every storage recovery attempt repeats it, including
`PRAGMA integrity_check`. Telemetry history was empty and alerts stopped at 1,000, so
these databases understate a long-used one.

| Archive | Database bytes after seeding | Traced peak, 100 misses | `PRAGMA integrity_check` |
| --- | --- | --- | --- |
| 100 x 2 | 430,080 | 31,484 | ok |
| 100 x 100 | 819,200 | 31,484 | ok |
| 1,000 x 2 | 679,936 | 31,484 | ok |
| 1,000 x 100 | 4,579,328 | 31,484 | ok |
| 10,000 x 2 | 3,112,960 | 31,484 | ok |
| 10,000 x 100 | 42,115,072 | 31,484 | ok |
| 100,000 x 2 | 27,627,520 | 31,788 | ok |

Query plans, from `EXPLAIN QUERY PLAN` on the application's own SQL:

| Statement | Plan |
| --- | --- |
| `_incident_for_alert`, no stored link | `SCAN incidents`; `CORRELATED SCALAR SUBQUERY 1`; `SCAN json_each VIRTUAL TABLE INDEX 1:` |
| `_enforce_retention`, every `add_alert` | `SCAN alerts`; `LIST SUBQUERY 1`; `SCAN alerts USING INDEX alerts_timestamp_idx` |
| Stored link read | `SEARCH alerts USING INDEX sqlite_autoindex_alerts_1 (id=?)` |
| `add_incident` link update | `SEARCH alerts USING INDEX sqlite_autoindex_alerts_1 (id=?)` |

What the data shows:

- Cost tracks total stored memberships as well as incident count. Comparing p50 of
  `miss` with `stored_link`, `add_alert` shows no clear cost at 1,000 x 2, then adds
  about 40 ms at 1,000 x 100, 20 ms at 10,000 x 2, 200 ms at 100,000 x 2 and 375 ms at
  10,000 x 100. Each write holds `BEGIN IMMEDIATE` and the `Database` lock for that time.
- Against the default 2 second refresh interval, an alert write stays well inside one
  cycle at every measured size.
- The bad case is a batch of 100 unlinked IDs: about 3.8 s at 1,000 x 100, 19 s at
  100,000 x 2 and 40 s at 10,000 x 100, with the lock held. Other database calls wait
  for that time. This needs an incident citing 100 alerts with no stored link, such as
  alerts already pruned, in an archive far beyond normal manual use. At 1,000 x 2 it is
  about 125 ms. `get_alert_incident_ids[100]/stored_link` stays near 6 ms at the two
  largest sizes.
- A membership table or an index on it is the likely fix if real archives approach
  these sizes. It was not built, because that is a schema change no realistic size
  here justifies.

### Synthetic outage

Added because it goes beyond `tests/test_v3_queue_capacity.py`: it fills the real 500
entry limits, causes the outage with a real SQLite `BEGIN EXCLUSIVE` lock rather than a
patched failure, and reads `/api/health` over the test client. Failed invariants raise,
so a clean exit means all of them held. Fourth command, exit 0, `integrity_check` ok:

| Stage | Observed |
| --- | --- |
| Outage with 600 alerts and 600 events, 2.469 s under `tracemalloc`, traced peak 1,757,194 bytes | `/api/health`: `degraded`, `pending_writes` 1,000, `dropped_alert_writes` 100, `dropped_event_writes` 100, `last_error_type` `OperationalError` |
| Retry and opt-out while locked | Both failed. Queue kept at 1,000, logging stayed on, and the unsaved opt-out was not applied |
| Recovery, 13.656 s for 1,000 writes | `healthy`, `pending_writes` 0, drop counts kept at 100 each. 499 alerts plus the grouped row, and 500 events, stored oldest first without the evicted entries |
| Further recovery and flush passes | No replay: grouped count stayed at 2, and a new detection made it 3 |
| Second full outage, then a saved opt-out | `pending_writes` 0, counts rose only by the new evictions to 200 each, so the 1,000 discarded writes were not reported as loss. Nothing discarded was stored, including after logging was turned back on |

During the locked retry the log showed `PermissionError` as well as `OperationalError`:
on Windows the schema check's file copy cannot read a locked database. Storage stayed
degraded and recovered normally, so this changes only the reported error type.

### Stability run

**NOT RUN.** The orchestrator gives each agent invocation 40 minutes, and a 30-60 minute
run would not fit that window alongside the benchmarks. No duration or aggregate metric
is claimed, and no eight-hour soak has been run.

### Limitations

- One machine, one interpreter, one pass of each default archive, on a working laptop
  with other software running. Python 3.13 was not measured.
- Cases at the 5-sample floor give coarse percentiles. The 40 s figures are five samples.
- Single-threaded storage calls only. Lock waits seen by a concurrent caller, the
  dashboard and the full service loop were not measured.
- The outage scenario calls private service methods directly and runs no worker loops.

## History row-cap retention, 2026-11-09

Local only, on `fix/v3-history-performance` from `7bae229`. Not pushed.

**Cause.** `add_history_batch` runs `_prune_history_locked` on every history write, once
per refresh, 2 s by default. Each row cap was
`DELETE FROM t WHERE rowid NOT IN (SELECT rowid FROM t ORDER BY timestamp DESC LIMIT cap)`.
`EXPLAIN QUERY PLAN` gives `SCAN t` and `LIST SUBQUERY 1`: SQLite built a set of up to
`cap` kept row IDs, then visited every row, whether or not any row was over the cap. The
process cap is `min(max_samples * 100, 2,000,000)`, so at the defaults it is 2,000,000.
At 100 process rows per sample, that is about 11 hours of collection.

**Change.** `Database._cap_history_rows` counts the rows first. Only when the count is
over the cap does it delete the `count - cap` oldest rows by timestamp, through
`SEARCH t USING INTEGER PRIMARY KEY (rowid=?)` over a walk of the time index that stops
after that many rows. It covers the same three tables, with the same caps, still after
time retention, in the same connection and transaction. No schema, index, setting or
caller changed.

Each table still keeps exactly `min(count, cap)` rows, and no kept row is older than a
deleted one. Which of several rows that share the boundary timestamp survives was not
specified before and is not now.

`tests/test_v3_history_retention.py` adds 37 tests: newest rows kept when insertion order
differs from time order, nothing deleted at or below the cap, 25 seeded model checks
with repeated timestamps and a time cutoff, a full write at the cap, and a rollback of
the whole write when the process cap fails. The work test counts SQLite VM steps while
100 excess rows are removed. Before the change, 1,000 kept rows took 16,001 steps and
20,000 took 263,001, so the test failed. After it, both take 2,907. Pruning with nothing
to delete takes 84 steps at 1,000, 20,000 and 100,000 rows.

### Measurements

Windows 11 Home 10.0.26300, Intel i7-1360P (12 cores, 16 logical), 15.7 GB RAM,
Python 3.11.9, SQLite 3.45.1. Databases were under the user temp directory, not
OneDrive. `Win32_Battery` reported status 1, discharging, on the Balanced plan when
checked at the end. Power state was not controlled, which may explain the variance below.

A scratch script, not committed, created each database with `Database.initialize()` and
inserted synthetic rows directly: one sample every 2 s ending at the current time, with
100 process rows per sample. Each run copied the same seeded file. It then timed 20
`Database.add_history_batch` calls, each with one sample, 100 process rows and one
connection summary, at the case's `retention_hours` and `max_samples`. Statements were
timed by wrapping the connection passed to `_prune_history_locked`. Every run ended
with `PRAGMA integrity_check` = `ok` and the expected row counts.

| Case | Rows seeded | Settings |
| --- | --- | --- |
| 1h | 180,000 process, 1,800 samples, 1,800 summaries | Defaults: 168 h, `max_samples` 100,000 |
| default7d | 2,000,000 process, 43,200 samples, 100,000 summaries, 257,400 compacted keys in 143 rollups | Defaults, the 7-day steady state with 24 h left uncompacted |
| large | 2,000,000 process, 1,296,000 samples, 2,000,000 summaries | 2,160 h, `max_samples` 2,000,000 |

`add_history_batch` per write, median / p95 / max in ms. With 20 writes, p95 is the
19th value.

| Case | Before (`7bae229`) | After, pass 1 | After, pass 2 | After, 100 writes |
| --- | --- | --- | --- | --- |
| 1h | 686.59 / 752.25 / 805.91 | 49.39 / 83.88 / 97.64 | 86.09 / 139.22 / 156.37 | not run |
| default7d | 8,259.48 / 45,976.44 / 61,801.03 | 382.23 / 544.76 / 693.56 | 987.37 / 1,041.51 / 1,266.28 | 958.01 / 1,002.55 / 1,039.35 |
| large | 17,585.57 / 17,860.81 / 21,315.43 | 554.36 / 674.02 / 1,116.10 | 625.93 / 830.40 / 2,076.20 | not run |

Median ms per statement:

| Statement | 1h before / after | default7d before / after (100 writes) | large before / after (pass 1) |
| --- | --- | --- | --- |
| Process cap, old `NOT IN` | 573.67 | 7,484.87 | 7,086.43 |
| Process `COUNT(*)` + bounded delete | 16.99 + none | 583.76 + 7.53 | 198.12 + 3.77 |
| Summary cap, old `NOT IN` | 3.53 | 277.06 | 6,351.41 |
| Summary `COUNT(*)` + bounded delete | 0.17 + none | 27.81 + 0.63 | 189.54 + 0.27 |
| Sample cap, old `NOT IN` | 3.64 | 116.81 | 4,069.07 |
| Sample `COUNT(*)`, under the cap | 0.21 | 11.61 | 128.73 |
| `telemetry_compacted_keys` cleanup, unchanged | 0.11 / 0.04 | 246.76 / 230.92 | 0.10 / 0.05 |

At the default 7-day size, a write fell from 8.3 s to about 1 s at the median, and the
worst write from 61.8 s to 1.3 s. That brings writes inside the 2 s interval, but the
cost still grows with the table.

### What remains

- `COUNT(*)` is still linear. It reads every page of the smallest index through the new
  connection that `_connect` opens for each call. On the default7d process table, 15
  fresh-connection counts took 361 ms median (193 to 639). On one warm connection with a
  200 MB cache they took 17 ms. Removing that cost needs a maintained row count, which
  means triggers and a counter table (a schema change), or a long-lived history
  connection. Neither was built.
- The `telemetry_compacted_keys` orphan cleanup scans every compacted key on every
  write. At the default 7-day steady state it costs about 231 to 247 ms, unchanged by
  this fix, and is now the second-largest cost per write.
- `_enforce_retention` (alerts, at most 50,000), event retention (10,000) and
  file-integrity event retention use the same `NOT IN` pattern at smaller caps. They
  were not measured or changed.
- One machine, one interpreter, synthetic rows, and single-threaded calls. Lock waits
  seen by the dashboard or other storage calls were not measured.

## Threshold alert lifecycle, 2026-11-10

Historical implementation at `bc83f51`, on `fix/v3-alert-lifecycle` from
`22a20a2`. The episode correction below supersedes the per-rule identity and
cooldown behavior in this section. Neither change has been pushed.

**Cause.** `DetectionEngine.evaluate` named each threshold alert
`uuid5("ghostwatch:{rule}:{breach start}:{occurrence}")` and incremented `occurrence`
every time the cooldown expired. A sustained breach therefore produced a new alert ID,
row and timeline event once per cooldown, 60 s by default. Threshold alerts also carried
no lifecycle state and recorded no recovery, and the grouped-alert merge in
`TelemetryService` and `Database.add_alert` applied only to `collection_failure`.
Reproduced at `22a20a2` through the real engine, service and SQLite, with a synthetic
sample held at 97% memory for 5 minutes: 5 memory rows, each with its own ID and an
`occurrence_count` of 1.

**Change.** The threshold rules (`cpu`, `memory`, `bandwidth`, `connections`,
`connection_spike`, `process_count`, `disk_space` and `destination_diversity`) now use
the grouped-alert path that collection failures already used:

| Condition | Effect on the rule's one alert |
| --- | --- |
| Breach sustained for the configured duration | Opens it with a stable per-rule ID, `condition_active` true, `first_occurrence` and `last_occurrence` set, and a count of 1 |
| Breach still present when the cooldown expires | Count +1. `last_occurrence`, timestamp, metric and evidence are updated. `first_occurrence`, acknowledge and dismiss are kept |
| First readable sample within the threshold | `condition_active` false and `last_recovery` set. Count, first occurrence and operator state are kept. Severity stays the rule's |
| Metric unreadable | No change. An open alert stays open until a readable sample clears it |
| Breach again after a recovery | The same alert reopens with count +1, and acknowledge and dismiss are cleared, as for collection failures. It still waits for the cooldown since the rule last reported |
| Restart during a breach | The same alert continues, keeping its first occurrence and operator state |
| Restart after the condition cleared while stopped | Startup reads which threshold alerts are still stored as active. The first readable clear sample records their recovery |

Each observation is a delta that live memory, the bounded pending queue and SQLite merge
into that one record, under the existing race handling. `GROUPED_ALERT_RULES` in
`app/models.py` lists the rules this applies to. Windows event, file integrity and
storage alerts are not in it, so each security event stays its own alert.

The timeline gets one `alert` event per breach instead of one per cooldown. A recovery
updates the alert and adds no event, so it is not counted as a CRITICAL detection. The
Detection Center shows the lifecycle line (state, first, last, occurrences and last
recovery) for any alert that carries lifecycle state, not only collection failures.
Recovered alerts were already left out of active signals and the navigation count.

No schema, setting, API model or retention rule changed. `Database.active_alert_ids`
reads only the IDs of active rows at startup, so an unreadable stored row cannot block
storage initialization.

**Tests.** `tests/test_v3_alert_lifecycle.py` adds 21 tests against the real engine,
service and SQLite. A fake monotonic clock and an injected wall clock
(`DetectionEngine(wall_clock=...)`) give exact timestamps, and races are held open with
events around the database write, not timing. The first 17 were written before the fix:
16 failed at `22a20a2` with duplicate rows, missing lifecycle state or the missing
restore, and the guard that each rule keeps its own alert passed before and after. The
file now uses the injected wall clock, which the old engine does not accept, so it
cannot be rerun unchanged against `22a20a2`. The Playwright test
`threshold alerts show their lifecycle state` failed before the dashboard change.

Mutation checks reverted 16 parts of the fix one at a time, and at least one test failed
for each: the stable ID, cooldown gating, the unreadable-metric guard and the memory
availability flag in the engine; the startup restore and the active-ID lookup; storage
grouping; the six service merge sites, including the live reopen reset; per-breach
timeline events and the skipped recovery event; and adding security rules to the
grouped set. Three of the service merge sites are reached only during a race, and
their tests were added after the first mutation run found them uncovered. Merging a
stored write with a newer queued observation (`_accept_stored_alert`) was not covered
by any existing test, including for collection failures.

### Limitations recorded at bc83f51

- Rows written before this change keep their per-cooldown IDs and have no lifecycle
  state, so they still count as active until dismissed or removed by retention. They
  are not migrated.
- A rule disabled while its alert is open leaves the alert open, because no sample
  clears it. Re-enabling the rule resumes it.
- A breach that returns within the cooldown is not reported until the cooldown ends,
  so the alert can read `RECOVERED` for up to one cooldown while the threshold is
  breached again. This is the existing cooldown behavior.
- `occurrence_count` on a threshold alert counts reports, at most one per cooldown, not
  samples.
- Recovery needs one readable sample. A value that oscillates around the threshold can
  recover and reopen once per cooldown, and each reopen clears acknowledge and dismiss.
  There is no hysteresis.
- The startup restore covers threshold rules only. A collection failure stored as
  active when GhostWatch stopped stays active until the next failure and recovery, as
  before.
- A reopened alert keeps the incident link from its earlier breach, as grouped
  collection-failure alerts already did.
- A breach seen again after a restart adds one new timeline event, because the engine
  starts a new breach in that process.
- If startup storage failed and the saved state is loaded later, a recovery already
  queued in memory can be recorded twice. The second only moves `last_recovery` later.

## Alert episode correction after Codex review, 2026-11-10

The correction builds on `bc83f51`; it does not replace the monitoring or persistence
architecture. Each continuous threshold breach now owns an alert ID. Repeated
observations merge into that episode until a readable sample confirms recovery. A
later qualifying breach receives a new UUID, count and first occurrence. This also
applies when the new breach qualifies inside the previous episode's cooldown.

The five reviewed defects and their corrections are:

1. **Hidden rebreach:** cooldown previously suppressed the transition back to active.
   It now throttles only repeated observations of an already active episode. The
   deterministic regression opens at t=10, recovers at t=12, opens a second episode
   at t=24 and recovers it at t=40.
2. **Historical incident association:** a rule-wide ID allowed the old incident's
   `related_alerts` JSON to associate a new breach with the resolved case. Separate
   episode IDs preserve the recovered alert and its incident relationship. Tests
   check both `incident_id` and `_incident_for_alert()` without its stored-link
   shortcut; a new episode cannot rediscover the old incident.
3. **Lost metadata after restart:** restoring IDs alone left a recovery delta without
   its count, timestamps or operator state. Startup now restores full active
   threshold records into the engine and live view before evaluation. With logging
   disabled, recovery changes the live state while SQLite remains unchanged, including
   acknowledgement and dismissal.
4. **Duplicate or shifted timeline detection:** restored episodes retain their ID and
   first occurrence. Only the initial qualifying observation emits a timeline event;
   subsequent observations cannot insert another event or overwrite the original in
   the live timeline. The existing event-ID format is preserved for earlier beta rows.
5. **Empty recovery after retention:** `Database.add_alert()` checks for a missing
   threshold row inside its existing `BEGIN IMMEDIATE` transaction. A zero-occurrence
   recovery delta for an expired episode is intentionally not inserted. The live
   recovery retains its known history; direct writes and queued recovery both clear
   pending state without resurrecting an empty persisted record. A queued episode
   that includes actual observations can still be persisted normally.

No schema migration, network exposure or security-event identity policy changed.
Windows Defender, other Windows events and file-integrity alerts remain independent.
The existing transaction and bounded-retention policies remain in place. Tests also
exercise concurrent threshold increments across four database instances, rollback
after a retention failure, unavailable telemetry for all eight threshold rules and
continuation of an active ID written by `bc83f51`.

Validation commands and final counts are recorded in
[Current Beta Validation](CURRENT_BETA_VALIDATION.md#alert-episode-review-corrections---november-10-2026).
All added database fixtures are disposable synthetic data. No production database,
unrelated expensive benchmark or external service is needed for these regressions.

Remaining limitations:

- Occurrences count emitted reports, rather than every telemetry sample. Repeats
  within an episode still use the configured cooldown.
- Recovery requires a readable enabled rule. Disabling a rule freezes its existing
  state; unavailable telemetry does not establish recovery. No hysteresis was added.
- Restart continuation relies on retained, readable persisted lifecycle state.
  Retention-expired and never-persisted episodes cannot be reconstructed after exit.
  Logging-disabled transitions remain live-only, and pending queues remain bounded
  memory rather than a durable journal.
- Pre-lifecycle alerts with unspecified condition state remain historical rows until
  dismissed or removed by retention. Existing active beta lifecycle rows are resumed
  under their stored IDs, then subsequent episodes use distinct IDs. Previously
  conflated historical beta episodes are not split or rewritten.
- Collection-failure lifecycle behavior is unchanged; its startup-recovery limitation
  remains separate from these threshold episode corrections.

## Delayed restoration and display-cache correction after cf62570

The next focused correction preserves the episode identity, cooldown, incident,
restart and retention behavior described above while addressing the final review's
two reproduced failures.

**Shared cause:** lifecycle aggregation depended on the 200-entry display deque.
The detection engine retained observation deltas rather than full operator metadata,
and its startup restoration skipped a saved episode when a provisional episode
already occupied that rule. Consequently, eviction could remove the only complete
baseline, and delayed restoration could persist a second identity for one breach.

The service now keeps full active-episode metadata separately, with at most one
entry per threshold rule. It also retains at most eight restored startup baselines,
including live-only recoveries, so historical reads cannot resurrect unchanged
SQLite state or displace a newer active episode. Display limits are unchanged.
Observation merging, operator patches, incident refreshes and persistence acceptance
update the same authoritative metadata. A recovery delta without any complete
baseline is never displayed as a standalone alert.

Until initial storage restoration finishes, the engine retains bounded summaries of
the first possible continuation, first readable recovery and explicit operator
changes for each threshold rule. Restoration binds a continuation to its original
stored identity, transfers only its new observation count, and removes its duplicate
queued/live detection. Operator-update events are rebound to the stable identity.
An observed recovery is an episode boundary: subsequent breaches keep their separate
identities, even if they qualified before SQLite returned. Explicit operator changes
survive a saved logging opt-out discarding persistence queues.

Incident creation involving a provisional threshold identity returns the existing
503 error until storage restoration establishes the stable identity. This prevents
an incident from acquiring a reference that reconciliation would orphan. Independent
security-event selections are unchanged; no existing incident history is rewritten.

Restored episode deltas are written with an optional internal `require_existing`
guard. `Database.add_alert()` checks the baseline inside `BEGIN IMMEDIATE`. If
retention pruned the original row before a reconciled positive-count delta reaches
SQLite, the suffix is not inserted as incomplete history. The complete live episode
remains available. The default two-argument database call and schema are unchanged;
first-time episodes, including a queued initial breach and recovery, remain writable.

Fourteen added cases cover delayed restore with logging on/off, recovery before and
after restoration, 200 actual synthetic Defender correlations, retention and runtime
outages, explicit provisional operator changes, historical reads after eviction,
recurrence before SQLite returns, provisional incident references and retention
pruning during a queued reconciliation flush. The first nine cases all failed
against `cf62570`; the later operator/history and missing-baseline cases also failed
before their targeted corrections. Full results are recorded in
[Current Beta Validation](CURRENT_BETA_VALIDATION.md#final-lifecycle-state-corrections-after-cf62570).

Remaining limits are unchanged: bounded memory is not a durable journal, retention
can remove persisted history, startup monitoring uses defaults until saved settings
are readable, and previously corrupted or conflated history is not automatically
rewritten. No production database, cache-limit increase, schema migration or separate
expensive benchmark is part of this correction.

## Process monitoring accuracy, 2026-11-11

Local work on `fix/v3-process-accuracy`, based on `8fea111`. No push or merge is
authorized. Measurements come from one development host: Windows 11, 16 logical
CPUs, about 410 to 425 processes, GhostWatch running as a standard user.

**Root causes.** Five reported defects were reproduced against `8fea111`:

1. `psutil.Process.cpu_percent()` returns `0.0` on its first call for each `Process`
   instance. The collector stored that value as an available measurement, so a busy
   process showed 0.0% until its next refresh.
2. Each 10-second pass refreshed 8 PIDs, half of them reserved for network-active
   processes. A process without connections was refreshed about every 10 minutes,
   and its second reading averaged CPU over that whole interval.
3. Identity candidates were ordered by network activity before identification
   state. Once every connected process was identified, the one-read budget kept
   returning to them, so other processes were never identified. A connected
   process whose executable stays inaccessible held the budget indefinitely.
4. `last_observed` advanced on every enumeration, while CPU and memory could be
   minutes old. No field reported when metrics were actually read.
5. Rows were sorted by connection count before truncation to 200. An unmeasured
   process carried the placeholder 0.0 and was cut. The AI context took the first
   15 rows of the same order.

Cost was the underlying constraint. A standard user cannot open about 200 of the
processes, mostly `svchost.exe`. For each of those, psutil falls back to one
whole-system `NtQuerySystemInformation(SystemProcessInformation)` query (median 9 ms,
up to about 80 ms). Refreshing every process through psutil took 2.6 to 3.0 s.

**Changes.** `app/monitoring/windows_process_metrics.py` issues that query once
per collection cycle and reads CPU time, working set and creation time for every
process. Only the 64-bit layout is read. The kernel output is copied into `bytes`
and parsed with bounds-checked `struct` reads. Entries are rejected if their thread
array or next-entry offset is inconsistent, a PID repeats, times are negative, or a
working set exceeds physical memory. Each snapshot must also agree with psutil's
reading of the GhostWatch process (creation time within 10 ms, CPU time between
readings taken just before and after, working set within 32 MiB), and must cover
90% of the PIDs that psutil enumerates. A test pins the offsets to the published
`winternl.h` structures. Any failure returns no snapshot. After three consecutive
failures the reader disables itself and logs once.

Without a valid snapshot, the per-process psutil path remains the fallback. It
reads `cpu_times()` and memory least recently read first, with at most 64
processes and 0.1 s of reads per sampling pass.

`cpu_percent` is CPU time between two readings of the same process, divided by
elapsed time and the logical CPU count. That is 0 to 100% of total capacity, the
Task Manager Details convention. The value stays unavailable until two readings
exist; it is never a 0.0 placeholder. A native creation-time change for a PID
replaces the whole cache entry. Under the fallback, a CPU counter that goes
backwards restarts the measurement, because psutil can keep a `Process` instance
across PID reuse. Identity reads are least recently attempted first, in discovery
order, and use the native creation time when it is known. A failed metric read
makes that field unavailable instead of keeping an old value.

`ProcessInfo` adds `metrics_observed`, `metrics_stale` (over 30 s old) and
`cpu_window_seconds`. Existing fields are unchanged and the database schema is
unchanged. Half of the 200 rows were reserved for the largest CPU and memory
consumers, and the AI context reserved 5 of its 15 rows for the busiest current
measurements; both were replaced after review (see the
[review corrections](#process-accuracy-review-corrections-2026-11-11)). Stale or
unavailable values are sent to the AI and to history as
`null`. The dashboard shows `—` for unavailable values, labels stale ones, sorts
missing values last in both directions, and gives the sample time and averaging
window in the drawer.

**Measurements.** The collector timings below are 30 consecutive cycles each.

| Collector cycle | `8fea111` | This branch, native | This branch, psutil fallback |
| --- | ---: | ---: | ---: |
| Enumeration-only cycle, median / p95 | 9.1 / 12.4 ms | 19.5 / 29.1 ms | 9.8 / 15.4 ms |
| Sampling-pass cycle, median / p95 / max | 245.5 / 392.5 / 396.1 ms | 45.1 / 56.7 / 59.4 ms | 188.6 / 266.3 / 326.5 ms |
| First pass after start | 224.7 ms | 42.8 ms | 132.0 ms |
| Processes with refreshed metrics per pass | 8 | all | until 64 or 0.1 s |

At the default cadence of a cycle every 2 s and a pass every 10 s, average
collector time per cycle falls from about 56 ms to about 25 ms on the native path.
The native path costs more between passes because it refreshes every process on
every cycle.

The accuracy check used a disposable child process running one busy thread for
44 s, so the expected value is 6.25% of 16 logical CPUs. The reference was the
Windows perflib counter `Win32_PerfRawData_PerfProc_Process.PercentProcessorTime`
over `Timestamp_Sys100NS`, divided by the logical CPU count, sampled every 2 s. It
read 5.8 to 6.3%. `8fea111` never listed the process in 23 cycles. The native path
listed it from the second cycle (2 s), and its 22 readings ranged from 5.8 to 6.2%.
An earlier psutil-only attempt first reported a value at 42 s and spent about
220 ms in every cycle meanwhile; this design replaced it.

The native snapshot was cross-checked against psutil for 423 processes the user
can open, with no disagreement. The leak check used no tracemalloc in the long
collector runs. Over 2,000 native reads, RSS was flat between read 1,000 and read
2,000, and handles went from 183 to 184. Over 1,500 full-pass collector cycles, RSS
rose from 34.2 to about 37 MiB by cycle 600 and stayed there, with 590 to 600
handles. `8fea111` stayed between 33.4 and 34.2 MiB over 800 cycles.

**Regression tests.** `tests/test_v3_process_accuracy.py` covers:

- the layout against `winternl.h`;
- 11 malformed-buffer cases;
- psutil cross-check rejections, failure statuses, buffer growth and limits, and
  self-disabling;
- two live checks on 64-bit Windows;
- native and fallback CPU semantics, freshness, PID reuse, identity fairness and
  the display bound;
- the AI and history consumers.

A replay of the five defect scenarios against `8fea111`, using a psutil-faithful
fake, reproduced every defect. Playwright covers unavailable, stale and measured
rows. Results are in
[Current Beta Validation](CURRENT_BETA_VALIDATION.md#process-monitoring-accuracy---november-11-2026).

## Process accuracy review corrections, 2026-11-11

An independent review of `83343f3` returned six findings. All six were reproduced
against `83343f3` before the corrections below, with synthetic data only.

**Shared cause for the identity findings.** On Windows, psutil caches `name()`,
`ppid()`, `exe()` and `create_time()` on each `Process` instance. `process_iter()`
keeps an instance when its PID exits and is reused between two enumerations. The
collector therefore reused one owner's psutil data for the next owner.

1. **Name after PID reuse (high).** Native detection replaced the cache entry, but
   the old instance's cached name and identity came back. Reads that must describe
   the current owner now use a new `psutil.Process(pid)`. A detected reuse takes
   its name from that instance. The enumerated instance is recorded as stale, and
   its cached name is ignored until psutil replaces it.
2. **Fallback CPU after PID reuse (high).** The fallback compared CPU counters
   without checking the owner. With a higher counter it reported 40% for a process
   that had just started. Fallback reads now take the creation time from the new
   instance inside the same `oneshot()`, so protected processes still need one
   query. A different creation time replaces the entry. CPU, memory and identity
   are not attributed when the creation time is unavailable. psutil's `0` for the
   kernel pseudo-processes counts as unavailable.
3. **Known creation times not published (medium).** Every native or verified
   creation time now sets `create_time`, without waiting for the identity rotation,
   so history rows carry it from the first sample.
4. **Heavy processes excluded (medium).** Ranking used stale and placeholder
   values. Under `83343f3`:
   - 100 stale 90% rows displaced a fresh 60% process;
   - an idle 8 GiB unconnected process lost to 120 light-CPU processes;
   - history kept stale rows, stored as `null`, instead of a fresh 50% process.

   `app/monitoring/process_selection.py` now serves the dashboard bound, history
   samples and AI context. It ranks only current measurements, so a stale or
   unavailable value never outranks a real one, including a real 0. A third of the
   rows each go to current CPU, current working set and connections, and the
   remainder follows network order.
5. **Impossible CPU accepted (medium).** The reader now reports a process's CPU time
   as unavailable when it exceeds the process's lifetime times the logical CPU
   count, plus 2 s of grace. The working set stays usable. The collector rejects a
   delta above 105% of total capacity. That keeps legitimate full usage within
   timing jitter. A rejected delta restarts measurement from that reading instead
   of being clamped to 100%. Buffer, layout and cross-check validation are
   unchanged.
6. **Platform-specific layout test (low).** The test used `ctypes.c_ulong` and
   `c_long`, which are 64-bit on LP64 platforms. It now uses explicit 32-bit and
   64-bit types and runs only for 64-bit Windows.

**Measurements on the development host.** Each figure is the median of 30 cycles,
from three alternating runs of `83343f3` and the corrected collector.

| Cycle | `83343f3` | Corrected |
| --- | ---: | ---: |
| Native sampling pass | 31.6 to 34.5 ms | 37.5 to 52.8 ms |
| Native enumeration-only cycle | 12.6 to 14.4 ms | 13.7 to 17.6 ms |
| Fallback sampling pass (one run each) | 154.2 ms | 156.8 ms |

The native pass gained a few milliseconds. The identity read now verifies its new
instance's creation time, which costs one whole-system query for a protected
process. That is about 6 ms per 10-second pass.

The one-thread burner read 5.3 to 6.3% on the native path, against a Windows
perflib reference of 5.4 to 6.1%. A value was available from the second cycle
onward (22 of 23 cycles). Over 600 full-pass cycles per path, handles stayed
between 532 and 538 and memory stayed level.

The new regressions failed against `83343f3` and pass now; results are in
[Current Beta Validation](CURRENT_BETA_VALIDATION.md#process-accuracy-review-corrections---november-11-2026).

## PID ownership correction after c6d7724, 2026-11-11

The focused follow-up review blocked `c6d7724` on four remaining ownership defects.
Before changing the collector, seven deterministic cases failed against that
implementation: multi-PID reuse, stale first discovery, a native/psutil owner race
with identity budgets of zero and one, the false 40% fallback sample, an inaccessible
identity creation time, and native RSS with no creation time.

The corrections retain the native snapshot and the earlier CPU, freshness, ranking
and ABI changes:

1. **Wrong enumeration object.** Native updates no longer accept an enumerated
   process object. Enumeration supplies liveness only; its cached name and extra
   `info` fields never overwrite verified row fields. The PID 10/PID 20 regression
   continues for four cycles after reuse, so a restored old name is detected.
2. **Native identity.** `ImageName` is decoded from the same native record as creation
   time, CPU and RSS, including first discovery. The `UNICODE_STRING` address is
   translated into an offset into the copied buffer. Length, alignment and bounds
   must keep the string after the thread records and within its own process entry;
   no supplied pointer is dereferenced. Optional psutil metadata is staged and must
   match that native owner before it can be attached. A later owner cannot replace
   the snapshot's name or creation time during the identity pass.
3. **Non-atomic psutil reads.** Fallback metrics and identity reads have a creation
   time check before reading and an independent `Process(pid)` instance after
   reading, outside the first instance's `oneshot()` cache. A mismatch, missing time,
   `AccessDenied` or termination rejects attribution. No staged fallback CPU, RSS or
   name is published in that case. The regression uses the real psutil wrapper over
   a synthetic platform API: constructor-cached owner A at 50 CPU-seconds followed
   by owner B at 54 seconds can no longer produce A at 40%.
4. **Unknown ownership.** A native record without a creation time clears the cached
   owner and publishes unavailable fields. An unverifiable identity read clears the
   optional metadata instead of keeping an apparently available executable or user.
   The explicit exception is Windows PID 4, the non-reusable System pseudo-process:
   its native working set and fixed name remain usable, while CPU, creation time and
   psutil identity stay unavailable. PID 0 remains excluded.

The old 10 ms creation-time allowance could combine separate owners even when both
boundary reads succeeded; a 5 microsecond reuse fixture now resets the baseline.
As committed in `a8f8de4`, cached CPU counters required exact equality, so switching
metric sources restarted ownership whenever the native and psutil conversions
rounded differently. The [one-ulp correction](#one-ulp-source-switch-correction-2026-11-11)
below replaces that.

The new tests also exercise the actual overview route and persist history batches
to disposable SQLite databases, then read them back to check name, creation time and
unavailable resource values. Existing test doubles now implement `Process(pid)` so
the collector does not need a test-only path that trusts the enumerated instance.

**Limits.** psutil's PID-based getters are not a transaction pinned to one Windows
process handle. The checks reject detectable changes and verification failures;
they cannot distinguish owners whose creation times are indistinguishable at the
API's floating-point resolution, or prevent a process from exiting after a completed
sample. Native rows describe their snapshot; fallback rows describe their last
verified sample. Enumeration alone does not refresh ownership or metric freshness.
This is not a claim of atomic psutil sampling or absolute PID-reuse safety.

Validation and the focused performance comparison are recorded in
[Current Beta Validation](CURRENT_BETA_VALIDATION.md#pid-ownership-correction-after-c6d7724---november-11-2026).

## One-ulp source switch correction, 2026-11-11

An independent review of `a8f8de4` found no mixed-owner data but demonstrated one
defect. Native snapshots and psutil convert the same FILETIME creation time to a
float separately. On the development host, 296 of 367 processes matched exactly and
71 differed by exactly one floating-point step; none differed further. `_owner_entry`
compared them exactly, so switching between the native and psutil paths treated those
processes as new owners. The entry was reset, which discarded the CPU baseline and
cleared the executable, user, status and parent PID until the identity rotation
returned, and restarted `first_observed`. A live switch on 369 processes replaced 77
entries in each direction.

`_owner_entry` now uses `_same_owner`, which allows one floating-point step: about
0.24 us at current timestamps, far shorter than any exit and reuse of a PID. The first
stored creation time is kept, so repeated one-step matches cannot drift. Comparisons
within one source (the fallback's before-and-after check) stay exact. After the
change, the same live switch replaced 0 of 369 entries.

New regressions use `math.nextafter` in both directions for native-to-fallback and
fallback-to-native switches. They check that the CPU baseline carries over and that
identity and creation time are kept. All four failed against `a8f8de4`. Guards keep
the 5 microsecond reuse rejected within the fallback and across sources, and show
that a two-step difference is still a new owner.

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
  recovery attempt. It took about 2.8 s on a 27 MB synthetic database; larger ones are
  unmeasured. See [the benchmark](#synthetic-persistence-benchmark-2026-11-07).
- The incident JSON scan holds the `Database` lock for about 40 s on a batch of 100
  unlinked IDs at 10,000 x 100. See
  [the benchmark](#synthetic-persistence-benchmark-2026-11-07).
- Surrogates in monitored folder paths submitted through the API are untested.
- Each history write still counts every capped table. At 2,000,000 process rows that
  count took about 0.2 to 0.6 s at the median, with the `Database` lock held, and the `telemetry_compacted_keys`
  cleanup adds about 0.23 s at the default 7-day size. See
  [History row-cap retention](#history-row-cap-retention-2026-11-09).

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
- `Database.add_alert` accepts observation deltas. Calling it again for an observation
  that already committed counts another occurrence; callers must not replay committed
  observations as operator updates. The service now keeps those paths separate.
- An update to a record that was rejected stays in the live view only.
- The storage failure alert keeps `persistence_pending` set although it is never queued.
- An in-flight write finishes after an opt-out, and discarded queue entries are gone.
- The three worker loops log with `LOGGER.exception`, so a traceback could carry
  exception text. No path reviewed here reaches them with record content.
- Rows stored with longer text are bounded when read. The stored row is not rewritten.
- Evidence mutated after construction bypasses the model boundary, and one such alert
  makes the whole `/api/alerts` response a 503. No application code does this.
- `telemetry_degraded` covers snapshots that exist but cannot be served. Frames that
  stop arriving while the socket stays open now show `TELEMETRY STALE`. This is local
  work that depends on PR #7; the timeout and evidence are in
  [Current Beta Validation](CURRENT_BETA_VALIDATION.md#telemetry-freshness-watchdog---november-5-2026).
- During a serialization failure the dashboard keeps the last rendered values under the
  degraded status. Playwright covers this path with synthetic frames only.
- Native process metrics require 64-bit Python on Windows. They depend on the
  `SystemProcessInformation` time fields inside the structure's documented reserved
  block, at the offsets psutil also uses. Each snapshot is validated, but a layout
  change that still passed every check would go unnoticed.
- On the psutil fallback, processes that need the restricted-access query are read
  slowly. The ownership check after each read adds a second creation-time query for
  those processes. A first read of every process on the development host took 36
  passes for 367 processes at `a8f8de4`, about 360 s, against 13 passes at
  `c6d7724`. After the one-ulp correction it took 37 passes for 373 processes. After
  the first pass, 355 of 367 rows (366 of 373 in the second run) were named `Unknown`,
  because enumeration names are no longer trusted. A CPU value needs two
  reads. These conservative checks favour correct attribution over fallback coverage.
  Rows report the gap as unavailable or stale instead of guessing a name or value.
- On the native path, CPU is averaged over one collection interval (2 s by default),
  so it moves more between samples than a 10-second average.
- Identity fields still use one read per 10-second pass, so on a host with about 400
  processes the first full identity sweep takes about an hour.
- Under the fallback alone, a PID reuse is noticed only when that PID's turn in the
  rotation comes. Until then the row keeps the previous owner's name and values;
  they age to stale, but are not re-attributed.
- A PID reused within the few milliseconds between a native snapshot and a psutil
  read is not distinguishable. The creation-time check makes this the only window.
- psutil and the kernel snapshot report different creation times for the Idle and
  System pseudo-processes (PIDs 0 and 4). The native time is shown for System, so
  its history identity differs from rows stored before this change. Under the
  fallback alone, System has no creation time, so none of its metrics are attributed.
- The CPU lifetime check uses the wall clock. If the clock moves back by more than
  2 s, a young process's CPU reads as unavailable until the clock passes its
  creation time again.
