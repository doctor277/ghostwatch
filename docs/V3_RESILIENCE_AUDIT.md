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

## Open findings

These come from code review. None has been reproduced or fixed.

- `send_snapshot` reports serialization errors as client failures, so any future
  unserializable field would disconnect every client.
- `model_dump_json()` runs once per client on the event loop. Cost is unmeasured.
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
