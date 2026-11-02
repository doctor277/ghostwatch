# GhostWatch V3 Final Binary Allowlist Hardening

## Scope and outcome

This final packaging hardening fixes the confirmed default-allow extension bypass in the isolated V3 staging tree. Production V2 was not modified. The work is a focused release-boundary remediation, not a penetration test or a general-release certification.

## Confirmed vulnerable path

Before the fix, `scripts/package_release.py` rejected a candidate only when its suffix appeared in the partial `BINARY_SUFFIXES` set and its complete archive path was absent from `APPROVED_BINARY_FILES`. An unknown suffix skipped that condition. `_release_payload()` then treated the file as opaque bytes and ZIP creation included it.

The pre-patch reproduction created `docs/unapproved-dump.bin`, built a temporary release, reopened it, and reported:

```text
pre_patch_bin_accepted=True
```

## Security invariant

Every selected release member must satisfy exactly one admission rule:

1. Its suffix is a known source/text suffix; or
2. It is the exact required extensionless `.gitignore`; or
3. Its complete relative POSIX archive path is present in `APPROVED_BINARY_FILES`.

Everything else is rejected before release bytes are written. A recognized binary suffix is not required for rejection; unknown types are denied by default.

## Implementation

- Removed the partial `BINARY_SUFFIXES` decision.
- Added `APPROVED_EXTENSIONLESS_TEXT_FILES` containing only the required root `.gitignore`.
- `TEXT_SUFFIXES` permits CSS, HTML, JavaScript, JSON, Markdown, PowerShell, Python, reStructuredText, TOML, and plain text.
- `_is_approved_text_file()` and `_release_type_finding()` form the shared type-admission boundary.
- `_selected_inventory()` rejects every candidate outside the text/config set unless its complete archive path is exactly allowlisted.
- `_release_payload()` independently fails closed if an invalid inventory entry reaches the read boundary.
- `scan_sensitive_payloads()` applies the same admission policy when reopening or independently verifying a ZIP, so hand-crafted unknown members are rejected as well.
- Existing link/junction checks, repository containment, file identity revalidation, archive-name validation, size limits, privacy scanning, temporary archive validation, atomic publication, and exact-payload verification remain intact.

## Regression coverage

`tests/test_v3_final_packaging.py` adds 27 cases covering:

- `.bin` and uppercase `.BIN`
- `.exe`
- `.pfx`
- `.p12`
- `.dat`
- `.zip`
- an arbitrary unknown extension
- unapproved extensionless files
- every exact approved binary path with byte preservation
- an allowlisted filename at the wrong relative path
- every permitted source/text suffix with byte preservation
- the required extensionless `.gitignore`
- a linked unknown binary
- cleanup when post-write archive verification fails
- independent ZIP rejection of an unknown binary member

The broader focused release suite also retains traversal, duplicate-name, corruption, junction, symlink, identity, privacy, Unicode, and size-limit tests.

## Bypass and compatibility review

The post-patch challenge explicitly tested uppercase, compound, extensionless, and backup-style aliases plus a hand-crafted ZIP member:

```text
payload.BIN=rejected
payload.tar.gz=rejected
README=rejected
.gitignore.bak=rejected
manual_unknown_member=rejected
```

The legitimate project inventory contains only approved source/config types and the three exact allowlisted images. Existing source bytes and approved binary bytes remain byte-exact.

## Remaining limitations

- Admission is path/extension policy, not file-format signature validation. An exact approved image path is trusted as a reviewed binary asset.
- A permitted textual extension can contain arbitrary valid UTF-8 text; privacy and secret signatures are still applied, but this is not semantic source-code analysis.
- The scanner retains the representation, hard-link, and image-content limitations documented in `docs/V3_RC5_REMEDIATION.md`.
- No penetration test was performed.

