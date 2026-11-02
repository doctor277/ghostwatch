# Ghostwatch V3 RC2

Version: 3.0.0-rc.2  
Schema: 3  
Status: release candidate for independent inspection, not declared generally available

## Completed scope

- Durable telemetry, process observations, connection summaries, hourly rollups, time ranges, missing-data state, restart-safe baselines, and explicit JSON/CSV export.
- Read-only incremental Defender/System/Application event summaries with durable cursors, deduplication, filters, permission fallback, and bounded correlation alerts.
- Disabled-by-default SHA-256 file-integrity monitoring for explicitly enrolled directories, with safe partial coverage and create/modify/delete alerts.
- Synthetic checked-in Playwright coverage for all 15 current sections, desktop/mobile behavior, keyboard access, Demo isolation, settings, incidents, reports, and WebSocket recovery.
- Transactional schema migration rollback, corrupted-history fallback, storage-outage recovery, PID-reuse safety, socket-churn bounds, and repeated 10,000-socket measurements.
- Future-schema compatibility rejection without mutating database or WAL state, newest-first mixed-resolution history limiting with chronological output, exact per-dataset export truncation, and parse-aware Windows event cursor recovery.

## Trust and privacy boundary

The server binds to `127.0.0.1:8765` and validates HTTP Host plus unsafe-request/WebSocket Origin. This protects browser-origin boundaries, not against another native process running as the same Windows user. Ghostwatch performs no remote scanning, packet capture, process termination, firewall change, security-setting change, cloud transmission, background startup registration, or silent report upload.

Windows event text, monitored relative paths, hashes, incident notes, and historical endpoints may be sensitive local metadata. They remain in the local SQLite database and explicit local exports. File contents are hashed but never stored. Demo data is synthetic and never persisted as retained history.

## Quality status

- Python: 118 passed, 1 known upstream warning.
- Browser: 12 Playwright tests passed on managed Chromium, desktop plus Pixel 7 emulation.
- Ruff, compileall, and all frontend JavaScript syntax checks passed.
- npm audit: 0 vulnerabilities.
- Listener: verified `127.0.0.1:8765` only; health 200; temporary process stopped.

## Remaining limitations

- No penetration test was performed.
- Windows Event Log reset/wrap handling is generic rather than explicit.
- File monitoring uses polling, not USN/change journals; renames appear as delete/create.
- Historical rollups are hourly means, without strict database-byte quotas or chart pointer zoom.
- High-cardinality process history samples a bounded activity-ranked subset.
- The 100-cycle socket test is synthetic and not a multi-day soak.
- Browser automation has not run in Firefox or WebKit.
- Upstream psutil can allocate its platform socket result before Ghostwatch bounds its own objects.
- Optional local Ollama analysis remains dependent on a separately operated loopback model service.

See `V3_IMPLEMENTATION_LOG.md` and `V3_VERIFICATION_EVIDENCE.md` for the complete phase-by-phase record.

## Source package

The RC source archive is built by `scripts/package_release.py` from an explicit whitelist. It contains 101 files and was verified to include the application, frontend, Python tests, Playwright suite, scripts, documentation, launcher, Python manifests, and Node lockfile. It excludes Git/environment/cache directories, runtime data, databases, logs, Playwright artifacts, and secret files. The packager scans selected content for private-key, common API-key, AWS-key, and bearer-token patterns before writing the ZIP and refuses to overwrite an existing archive.
