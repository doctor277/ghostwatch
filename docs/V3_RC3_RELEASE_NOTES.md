# GhostWatch V3 RC3 Release Notes

Version: 3.0.0-rc.3  
Date: 2026-10-22

RC3 is a data-integrity remediation release candidate prepared in the isolated V3 staging tree. It has not been merged into production V2.

## Fixed

- Hourly historical compaction no longer replaces earlier partial aggregates. Completed-hour gating, exact per-metric weights, unavailable-state merging, and durable compacted identities make repeated runs and late retries idempotent.
- Existing V3 rollup/raw overlaps are reconciled transactionally on startup.
- Historical queries now include every rollup whose coverage intersects the requested half-open interval.
- Mixed raw/rollup queries exclude duplicate coverage, retain chronological output, and apply limits to the newest observations globally.
- History records now include explicit coverage bounds and per-metric sample counts.
- All persisted/query timestamps use canonical UTC. Offset-equivalent requests return the same observations, naive values are rejected, and explicit DST offsets remain distinct.
- Windows Event timestamps use strict UTC parsing and continue to preserve cursors on malformed input.
- The release packager now always excludes `docs/browser-desktop.png`, which contains live host telemetry.

## Validation summary

- 131 Python tests passed; 13 are new RC3 regressions.
- Ruff and Python compilation passed.
- Eight JavaScript syntax checks and the PowerShell launcher parse passed.
- 12 Playwright tests passed on desktop and mobile Chromium projects.
- Focused Host/Origin/WebSocket, migration/integrity, and Windows Event suites passed.
- Synthetic 10,000-socket scale and 20-cycle churn benchmarks completed with aggregate/detail and bounded-state invariants intact.

See `docs/V3_RC3_TEST_RESULTS.md` for commands and actual output.

## Compatibility notes

- Database schema metadata remains v3. RC3 adds compatible rollup count columns and a compacted-key table during idempotent initialization.
- Existing aware timestamps are converted to the equivalent UTC instant transactionally.
- Naive legacy timestamps are not silently interpreted; initialization fails safely so an operator can resolve their provenance.
- Existing non-null rollup values are preserved. Exact sparse denominators missing from RC1/RC2 cannot be reconstructed and use total `sample_count` as a documented compatibility estimate.
- Downgrading an RC3-updated database to an older RC2 application is unsupported.

## Privacy

The source archive uses an explicit allowlist, secret-pattern scan, and cache/runtime exclusions. The live desktop screenshot is excluded; labeled synthetic demo and static design assets remain.

## Known limitations

- Live Windows Event Log reads could not be completed under the verification account because System and Application returned access denied. Permission-aware fallback behavior is automated and passing.
- One upstream Starlette TestClient deprecation warning remains.
- Performance results are synthetic and host-specific, not an extended soak.
- File paths, Windows Event summaries, incident notes, and explicit exports remain locally sensitive data controlled by the user's filesystem permissions.
- No penetration testing was performed.

## Release readiness

RC3 passes the implemented automated and static quality gates and is ready for independent release-candidate inspection. General release readiness is not declared; privileged Windows integration coverage and extended soak testing remain external validation items.
