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
has not been benchmarked. Historical counts or relationships corrupted before this
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
creation can create another incident. Large incident archives remain unbenchmarked.

This follow-up is local only. PR #7 remains draft and unmerged by instruction; no
remote operation, push, PR mutation or production V2 change is part of this work.

## Merge-gate recheck, 2026-11-05

Bounded local recheck of `d9f5843..c162eed`. `HEAD` was confirmed to be
`c162eed9704bee6fe4d387f38f478f884c3d811d` with read-only Git commands and no fetch.
Scope was the reviewed diff in `app/service.py`, `app/storage/database.py` and
`app/api/routes.py`, plus directly related logic. All data was synthetic, and no crash
or power-loss experiment was run.

**Recommendation for code quality: APPROVE WITH FOLLOW-UPS.** This is not a GitHub
approval. Hosted CI was not rerun, so its 601/17 results describe only the published
SHA and not this tree. The state of PR #7 was not rechecked. The fixes below are
uncommitted at the time of writing. Automatic review notes rejected two earlier
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
- `telemetry_degraded` covers snapshots that exist but cannot be served. The dashboard
  has no watchdog for frames that stop arriving while the socket stays open, so it can
  still show `CONNECTED` if collection itself stops.
- During a serialization failure the dashboard keeps the last rendered values under the
  degraded status. Playwright covers this path with synthetic frames only.
