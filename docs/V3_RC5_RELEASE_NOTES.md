# GhostWatch V3 RC5 Release Notes

## Summary

RC5 closes two release-boundary defects: archive inventory can no longer follow linked source paths, and privacy scanning now covers every requested source-like textual format. All changes remain isolated from production V2.

## Security changes

- Rejects source file symlinks, linked directory components, Windows junctions, and reparse points.
- Requires every selected source to resolve beneath the repository root.
- Revalidates directory components and file identity before, during, and after reading.
- Preserves the binary allowlist while making it subordinate to link, containment, size, identity, and privacy checks.
- Rejects traversal, duplicate/case/Unicode-colliding, absolute, drive-qualified, and backslash ZIP names.
- Builds and validates a temporary ZIP before atomic no-clobber publication, preventing rejected inputs from leaving a partial requested artifact.
- Scans Python, JavaScript, JSON, HTML, CSS, PowerShell, TOML, Markdown, and plain text for Windows and Unix profile roots.
- Detects standard JSON-escaped Windows separators and mixed slash forms.
- Preserves safe executable source bytes and aborts instead of rewriting unsafe code.
- Uses exact fictional fixture exceptions rather than a directory-wide test exemption.
- Avoids echoing matched sensitive content in scanner errors.

## Verification summary

- Python: 181 passed; one disclosed third-party deprecation warning.
- Release inventory/privacy: 43 focused tests passed.
- Playwright: 14 passed.
- Ruff: passed after the documented generated-fixture correction.
- Python compilation: passed.
- JavaScript syntax: 8 files passed.
- PowerShell parser: passed.
- Host/Origin/WebSocket: 7 focused tests passed.
- SQLite migration/integrity: 59 focused tests passed.
- File integrity: 8 focused tests passed.
- Synthetic network benchmarks retained complete 10,000-socket aggregates and 250 detail rows.

See `docs/V3_RC5_TEST_RESULTS.md` for exact results and limitations and `docs/V3_RC5_REMEDIATION.md` for the boundary analysis, implementation, tests, and scanner coverage.

## Status

RC5 is a hardened release candidate. It has not been penetration-tested and is not declared generally release-ready.

