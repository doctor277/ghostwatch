# GhostWatch V3 RC4 Remediation

## Scope

RC4 addresses the three independently reported RC3 findings without changing the production V2 tree. The work remains a release candidate: it is not a penetration-test result and does not establish general release readiness.

## Release privacy

### Root cause

The earlier packager copied selected text files byte-for-byte and treated broad documentation trees as releasable. Raw command evidence and review packets could therefore reproduce a local account name or an absolute profile path. Images also had no fail-closed release allowlist.

### Changes

- `scripts/package_release.py` now excludes superseded private evidence (`REVIEW_EVIDENCE.md`, `REVIEW_PACKET.md`, `docs/audit-evidence/`, and `docs/review/`) without editing those originals.
- Public Markdown, reStructuredText, and text summaries are copied through a release-only redactor. Windows and Unix profile roots become `<USER_HOME>` and the current account name becomes `<redacted-user>`.
- Source, test, configuration, and frontend files remain byte-faithful; the redactor is not used to conceal implementation content.
- Secret signatures are scanned in every payload. The current local account name is scanned in both payload text and archive member names.
- Release prose is rejected if a generic Windows or Unix profile path remains after sanitization.
- Binary screenshots and documents fail closed unless explicitly allowlisted. The live desktop telemetry screenshot is explicitly excluded; the allowlist contains only previously reviewed design or synthetic-demo images.
- The ZIP is assembled from the exact payloads that were scanned, preventing a scan/copy mismatch.

### Tests

`tests/test_v3_rc4_hardening.py` verifies redaction without source modification, raw evidence exclusion, live screenshot exclusion, secret detection, generic profile-path detection, binary member-name scanning before text decoding, unapproved-binary rejection, archive-member privacy, and byte-faithful synthetic source fixtures.

### Scanner coverage and limits

The scanner covers UTF-8 release text, archive names, common private-key/API-token signatures, Windows/Unix profile roots in public prose, the current username, forbidden directories, database/log/environment suffixes, and an explicit binary allowlist. It is deterministic and fail-closed for unknown binary assets.

It does not prove that arbitrary encoded, encrypted, compressed, steganographic, or previously unknown credential formats are safe. Allowlisted images require human review; the scanner does not semantically inspect pixels. This is release-hygiene validation, not data-loss-prevention certification.

## Safe database migrations

### Root cause

Legacy timestamps such as `2026-01-01T00:00:00Z` and `2025-12-31T19:00:00-05:00` can represent the same UTC instant while remaining distinct SQLite text keys. Updating one row at a time to canonical UTC could encounter a unique-key conflict after migration had begun.

### Changes

- `app/storage/database.py` defines every timestamp-bearing normalized uniqueness key used by historical telemetry, compacted-key provenance, process observations, connection summaries, and rollups.
- Initialization first copies the database plus existing WAL/SHM sidecars into a disposable same-volume snapshot.
- The snapshot canonicalizes each timestamp component in memory and detects logical key collisions before the live database is opened for migration.
- A second collision check runs inside the migration transaction to protect against a race between preflight and update.
- Collisions raise `TimestampNormalizationCollisionError` with the affected table and row IDs, plus an explicit recovery instruction. Values that may contain host telemetry are not included.
- The live migration remains inside `BEGIN IMMEDIATE`; every exception rolls back. Existing pre-migration backups and post-migration `PRAGMA integrity_check` remain in place.

### Tests

Regression coverage proves that a collision leaves the main database and existing WAL/SHM bytes unchanged, that distinguishing the conflicting observation permits recovery without losing either measurement, that rollup collisions are atomic, and that successful migrations pass SQLite integrity checks. Existing tests continue to cover future-schema rejection, interrupted migration rollback, valid migration, overlap reconciliation, retention, and corrupt-storage recovery.

## File-integrity path safety

### Root cause

The prior collector used `resolve()` for an enrolled root but did not explicitly define whether the user-selected root itself, or one of its parent path components, could be a symlink or Windows junction.

### Changes

- `app/monitoring/file_integrity.py` rejects a monitored root when the selected path or any parent component is a symbolic link or junction.
- The error tells the operator to enroll the resolved target explicitly.
- A real, explicitly selected target directory remains valid.
- Internal directory and file links remain skipped, and recursive traversal keeps `followlinks=False`.
- `frontend/index.html` documents this behavior beside the enrollment control.

### Tests

Regression tests cover a linked root, a root reached through a linked parent, acceptance of the explicit real target, skipped internal links, and interrupted enumeration preserving the prior baseline without inferring deletions.

## Additional reliability coverage

- `e2e/synthetic-backend.js` can inject one-shot synthetic API failures.
- `e2e/dashboard.spec.js` adds three-cycle WebSocket restart-storm recovery and a settings-persistence failure flow that requires the safe `NOT PERSISTED` state and expected user notification.
- Historical retention, storage initialization recovery, malformed Windows timestamp fallback, strict report redaction, and privacy-safe packaging received additional deterministic tests.
- Version metadata in `pyproject.toml`, `app/main.py`, `app/__init__.py`, and `README.md` is aligned to RC4.

## Files changed for RC4

- `scripts/package_release.py`: sanitized, fail-closed release assembly and scanning.
- `app/storage/database.py`: preflight normalized-key collision detection.
- `app/monitoring/file_integrity.py`: explicit root symlink/junction policy.
- `frontend/index.html`: operator-facing path policy.
- `e2e/synthetic-backend.js`: controlled failure injection.
- `e2e/dashboard.spec.js`: reconnect-storm and frontend failure tests.
- `tests/test_v3_rc4_hardening.py`: RC4 backend/security/privacy regressions.
- `pyproject.toml`, `app/main.py`, `app/__init__.py`, `README.md`: RC4 identity and documentation.

