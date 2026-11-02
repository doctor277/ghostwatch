# GhostWatch V3 RC3 Data Integrity Remediation

Version: 3.0.0-rc.3  
Date: 2026-10-22  
Scope: isolated V3 staging tree only

This document records remediation of the three independently reproduced RC2 defects. It is not a penetration-test report and does not declare general release readiness.

## 1. Hourly compaction data loss

Root cause: the previous hourly `UPSERT` replaced an existing rollup with values calculated only from the latest raw subset. A later partial compaction of the same hour therefore discarded the earlier aggregate and reset `sample_count`.

Changes in `app/storage/database.py`:

- Compaction admits only fully eligible, completed UTC hour buckets.
- Existing rollups are merged with later observations by weighted average rather than replaced.
- Eight per-metric non-null counts are stored, so sparse CPU, memory, disk, network, connection, and process metrics use the correct denominator.
- Unavailable-metric markers are unioned, and collector health retains the worst observed state.
- `telemetry_compacted_keys` records compacted `(timestamp, sequence)` identities. This prevents a late or retried observation from being counted twice after its raw row has been removed.
- Existing raw rows overlapping legacy rollups are reconciled transactionally during initialization and then removed exactly once.
- Rollup creation, key recording, raw deletion, and retention pruning occur in one `BEGIN IMMEDIATE` transaction with rollback on error.
- Rollups remain retained until their `bucket_end` crosses the retention cutoff.

Migration compatibility:

- The supported database schema remains schema v3. New columns and the compacted-key table are added idempotently during v3 initialization.
- RC1/RC2 rollups did not record per-metric denominators. For those existing rows, a non-null metric count is conservatively backfilled from the stored total `sample_count`; null metrics receive zero. Existing aggregate values are not discarded.
- All compatibility work occurs in the same migration transaction and is covered by SQLite integrity checking. Unsupported future schema versions are still rejected before the live database or WAL/SHM files are opened for mutation.

## 2. Overlapping rollup queries

Root cause: rollups were selected by bucket start containment. A rollup covering 12:00–13:00 therefore failed a 12:30–13:30 query even though the intervals overlap.

Changes in `app/storage/database.py`, `app/models.py`, and `app/service.py`:

- Public history ranges use half-open semantics: `[start, end)`.
- Rollups use the interval-intersection predicate `bucket_start < end AND bucket_end > start`.
- Raw points use `timestamp >= start AND timestamp < end`.
- Raw points covered by a rollup are not returned separately.
- The global newest `limit` is selected across raw and rollup candidates, then results are returned chronologically.
- Every historical point exposes `coverage_start`, `coverage_end`, `resolution_seconds`, `sample_count`, and per-metric counts.
- Gap calculation uses coverage bounds rather than treating an hourly aggregate as an instantaneous point.
- A pre-existing internal diagnostic point lookup (`start == end`) is preserved only by the non-API convenience method. Public API ranges reject equal bounds and remain half-open.

## 3. Timezone normalization

Root cause: offset-bearing ISO strings were stored and compared lexically. Two strings denoting the same instant could therefore sort or filter differently.

Changes in `app/time_utils.py`, `app/storage/database.py`, `app/api/routes.py`, `app/service.py`, `app/reporting.py`, and `app/monitoring/windows_events.py`:

- A shared UTC conversion/parser rejects naive timestamps and emits canonical offset-aware UTC ISO 8601 values.
- New telemetry, process observations, connection summaries, alerts, events, incidents, Windows events, cursors, file-integrity records, monitored-folder dates, baselines, and rollups are stored in UTC.
- Initialization transactionally canonicalizes existing aware timestamps without reinterpreting wall-clock values.
- History, process, connection, Windows Event, export, and report ranges normalize inputs before filtering.
- Windows Event XML timestamps now use the same strict parser; malformed or naive timestamps make the event invalid rather than silently assuming local time.
- DST regression coverage verifies that repeated local wall-clock times with explicit EDT/EST offsets remain distinct UTC instants.

## Privacy remediation

All four release image assets were visually reviewed.

- `docs/browser-desktop.png` contains live host telemetry, including real process names/PIDs and current utilization. It is explicitly excluded by `scripts/package_release.py`.
- `docs/browser-demo-network-graph.png` is visibly labeled synthetic demo data and remains included.
- `docs/design-concept.png` and `docs/ghostwatch-v2-concept.png` are static design concepts and remain included.
- `tests/test_v3_rc3_data_integrity.py` asserts the live desktop capture is excluded while the labeled demo capture remains eligible.

## Files changed

- `app/__init__.py`
- `app/api/routes.py`
- `app/main.py`
- `app/models.py`
- `app/monitoring/windows_events.py`
- `app/reporting.py`
- `app/service.py`
- `app/storage/database.py`
- `app/time_utils.py` (new)
- `scripts/package_release.py`
- `tests/test_v3_rc3_data_integrity.py` (new)
- `README.md`
- `pyproject.toml`
- `docs/V3_RC3_REMEDIATION.md`
- `docs/V3_RC3_TEST_RESULTS.md`
- `docs/V3_RC3_RELEASE_NOTES.md`

## Regression coverage added

Thirteen RC3 tests cover:

1. Completed-hour gating and repeated-compaction idempotence.
2. Restart reconciliation of legacy partial rollups.
3. Sparse metric denominators and unavailable-state preservation.
4. Overlap selection, exact interval boundaries, and coverage metadata.
5. Late raw observation merging and durable duplicate suppression.
6. Newest-limit export and truncation behavior.
7. Equivalent UTC and offset-based telemetry queries.
8. Process, connection, Windows Event, and report UTC normalization.
9. DST repeated-wall-time behavior.
10. Naive timestamp rejection in storage and HTTP APIs.
11. Transactional canonicalization of existing timestamps.
12. Rollup retention at a partial cutoff boundary.
13. Release exclusion of the live desktop screenshot.

## Remaining limitations and risks

- Exact sparse per-metric counts cannot be reconstructed from legacy RC1/RC2 rollups because those releases did not store them. RC3 uses the only compatible estimate available and stores exact counts for all new merges.
- Live System and Application Event Log reads were unavailable to the verification account (`wevtutil` exit 5). Permission-denied behavior is tested, but a privileged live read was not performed.
- The suite emits one Starlette deprecation warning because the installed FastAPI TestClient compatibility layer still imports the legacy `httpx` adapter.
- The 10,000-socket measurements are synthetic allocation/latency checks, not an extended soak or a guarantee for other hardware.
- SQLite protects local consistency and crash recovery but is not designed for shared multi-host writers.
- Opening an RC3-updated database with an older RC2 binary is not a supported downgrade path.
- No penetration testing was performed. Production V2 was not modified and V3 was not merged.
