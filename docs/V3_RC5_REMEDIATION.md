# GhostWatch V3 RC5 Remediation

## Scope and outcome

RC5 fixes the two reproduced RC4 release-packaging defects in the isolated V3 staging tree. Production V2 was not modified. This work validates a release boundary; it is not a penetration test or a declaration of general release readiness.

## Secure archive inventory

### Root cause

`scripts/package_release.py` previously enumerated files with `Path.rglob()` and accepted candidates through `Path.is_file()`. That predicate follows a file symlink, so an allowed-looking archive path could read content whose actual target was outside the repository. Selection also did not retain a file identity for comparison immediately before the read.

### Fix

- Inventory traversal now uses `os.walk(..., followlinks=False)` and validates every included base, directory component, and file with `lstat()`.
- Symbolic links, Windows junctions, and other Windows reparse-point paths are rejected before content is read.
- Every selected path is resolved and required to remain beneath the resolved repository root.
- Selected files retain device, inode, mode, size, and modification-time identity. Directory components and containment are revalidated immediately before opening, the opened handle is compared with the selected identity, and both handle and pathname identity are checked again after reading.
- Only regular files are eligible. Per-file and total uncompressed source limits are 50 MiB and 250 MiB.
- Archive names must be normalized relative POSIX paths without empty, dot, parent, absolute, drive-qualified, NUL, or backslash components.
- Exact, case-insensitive, and Unicode-normalized archive-name collisions are rejected.
- The existing explicit binary allowlist remains in force. A binary allowlist entry does not override link, containment, identity, size, or privacy validation.
- ZIP output is created in a same-directory temporary file, closed, reopened, checked for duplicate/traversal/link entries, integrity-tested, privacy-rescanned, and compared with the expected payload map before publication.
- Windows publishes the validated temporary archive with same-volume atomic `rename`, which refuses replacement. POSIX uses a no-clobber hard-link publication. Failures clean the temporary file and do not leave the requested destination partially created.

### Tests

`tests/test_v3_rc5_release.py` covers external text and allowlisted-binary file links, linked parent directories, Windows junctions where available with a deterministic reparse fallback, pre-read file and parent identity changes, direct root escapes, oversized inputs, existing destination preservation, duplicate and traversal ZIP members, corrupted ZIPs, ZIP symlink entries, Unicode filenames, and byte-preserving executable source packaging.

## Complete privacy scanning

### Root cause

Generic Windows and Unix profile-path detection previously ran only on public prose suffixes. Source-like Python, JavaScript, JSON, HTML, CSS, PowerShell, and TOML payloads could therefore contain an absolute profile path without failing the release scan.

### Fix

- Profile detection now applies to Python, JavaScript, JSON, HTML, CSS, PowerShell, TOML, Markdown, plain text, and extensionless text.
- Windows detection accepts repeated escaped backslashes and mixed slash separators. Standard JSON strings containing doubled Windows separators are covered.
- Unix `/home/<account>` and `/Users/<account>` roots are rejected in the same formats.
- Source-like release payloads must be UTF-8. Invalid encodings fail closed.
- Executable/source payloads are never rewritten. Their exact bytes are retained when safe; unsafe source aborts the release.
- Public Markdown, reStructuredText, and plain-text summaries retain the RC4 release-only redaction behavior. Original files remain unchanged.
- Scanner diagnostics report an archive-member ordinal and finding class, not the matched path, username, token, or secret value.
- Synthetic profile fixtures use an exact map of archive filename to exact normalized fictional profile root. There is no wildcard `tests/` exception; another account or another test file fails.

### Tests

The RC5 matrix supplies both Windows and Unix negative cases for all nine requested formats, JSON-escaped separators, mixed separators, non-echoing diagnostics, exact synthetic-fixture acceptance, and rejection of a sibling unapproved fixture.

## Additional changes

- `pyproject.toml`, `app/main.py`, `app/__init__.py`, and `README.md` identify RC5.
- Ruff now explicitly excludes `.tmp`, the configured generated test workspace, plus `node_modules`; repository source remains fully checked.
- The first RC5 Ruff attempt reported 20 findings in generated adversarial `.tmp` Python fixtures. The fixtures were made valid or changed to non-Python names where appropriate, the temporary workspace boundary was made explicit, and the final repository Ruff run passed.

## Files changed

- `scripts/package_release.py`
- `tests/test_v3_rc5_release.py`
- `pyproject.toml`
- `app/main.py`
- `app/__init__.py`
- `README.md`
- `docs/V3_RC5_REMEDIATION.md`
- `docs/V3_RC5_TEST_RESULTS.md`
- `docs/V3_RC5_RELEASE_NOTES.md`

## Remaining limitations

- The scanner is signature- and representation-based. It does not decode encrypted, compressed, steganographic, or arbitrary custom encodings embedded inside otherwise allowed content.
- JSON's standard escaped backslashes are covered; arbitrary Unicode escape obfuscation is not interpreted as a general-purpose language parser.
- Hard-linked regular files are validated as files inside the repository but are not rejected merely because another hard link may exist elsewhere.
- Allowlisted images require human review; pixel content is not semantically classified.
- Race resistance uses repeated path, component, and handle identity validation. It does not claim to provide an operating-system sandbox against a malicious process with concurrent write access to the staging tree.

