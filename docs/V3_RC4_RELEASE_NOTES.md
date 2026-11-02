# GhostWatch V3 RC4 Release Notes

## Candidate summary

RC4 hardens release privacy, schema-v3 timestamp migration safety, and file-integrity enrollment paths. It also expands deterministic backend and browser reliability coverage. Production V2 has not been merged or modified.

## Security and privacy changes

- Public release prose is sanitized during packaging; private raw evidence is excluded but not altered.
- Unknown binary release assets fail closed, and real host telemetry screenshots are prohibited.
- Archive payloads and names are scanned before the exact scanned bytes are written to ZIP.
- Timestamp-normalization collisions are rejected before any live migration change, including when equivalent timestamps use different offsets.
- Existing WAL and SHM sidecars are included in disposable compatibility checks and remain unchanged on a collision failure.
- File-integrity roots may not be symlinks, junctions, or traverse linked parent components. Operators must enroll the real resolved target explicitly.

## Reliability changes

- Migration collision diagnostics identify safe row locations without echoing telemetry values.
- Interrupted file scans retain known baseline entries and do not infer deletions from incomplete enumeration.
- Browser automation now exercises repeated WebSocket recovery and a user-visible persistence failure.
- Additional tests cover retention cleanup, storage recovery, Windows parser degradation, strict report redaction, and release ZIP privacy.

## Verification summary

- Python: 148 passed; one unsuppressed third-party deprecation warning.
- Ruff: passed.
- Python compilation: passed.
- JavaScript syntax: 8 files passed.
- PowerShell parser: passed.
- Playwright: 14 passed.
- Focused migration/integrity: 58 passed.
- Focused Host/Origin/WebSocket: 7 passed.
- Focused Windows parser/recovery: 12 passed.
- Actual listener: one `127.0.0.1:8765` listener; health 200.
- Synthetic network benchmark: 10,000 aggregate sockets, 250 detail rows; 20 repeated churn cycles completed.

See `docs/V3_RC4_TEST_RESULTS.md` for exact commands, outputs, warnings, benchmark values, and limitations. See `docs/V3_RC4_REMEDIATION.md` for root causes, fixes, tests, and scanner coverage.

## Upgrade and operator notes

- Back up local data before testing a release candidate.
- If migration reports a timestamp collision, do not delete either observation. Preserve the database and use the table/row IDs in the diagnostic to distinguish or merge the logical records deliberately before retrying.
- Re-enroll a file-integrity folder using its real target path if a symlink or junction root is rejected.
- Review release scanner output and the binary allowlist whenever adding documentation assets.

## Known limitations

- Successful live Windows Event Log collection was not demonstrated on the verification host because the read-only probe was denied.
- Browser checks use synthetic data and do not establish long-duration production stability.
- Privacy scanning is signature- and policy-based; it is not a complete DLP system or semantic image classifier.
- RC4 has not undergone penetration testing and is not declared generally release-ready.

