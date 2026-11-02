# GhostWatch V3 Final Binary Allowlist Test Results

## Command notation

Commands ran on Windows from `<PROJECT_ROOT>`, the isolated V3 staging tree. `<PYTHON>` denotes the existing Python 3.11+ virtual-environment interpreter. `<OUTPUT>` denotes the designated artifact folder.

## Pre-patch reproduction

The temporary reproduction built a source root containing only `docs/unapproved-dump.bin`, reopened the generated ZIP, and printed:

```text
pre_patch_bin_accepted=True
```

## Focused release security suite

Command:

```powershell
& <PYTHON> -m pytest -q --basetemp=.tmp/pytest-final-packaging-focused tests/test_v3_final_packaging.py tests/test_v3_rc5_release.py tests/test_v3_rc4_hardening.py -k '<release-security-expression>'
```

Actual result:

```text
70 passed, 7 deselected in 8.01s
```

The separate alternate-representation challenge rejected uppercase, compound, unapproved extensionless, backup-style, and manually inserted ZIP members.

## Complete Python suite

Command:

```powershell
& <PYTHON> -m pytest -q --basetemp=.tmp/pytest-final-binary-release
```

Actual result:

```text
........................................................................ [ 34%]
........................................................................ [ 69%]
................................................................         [100%]
208 passed, 1 warning in 24.34s
```

The 181-test baseline is preserved and 27 tests were added. The warning is the existing unsuppressed `StarletteDeprecationWarning` from the installed FastAPI/Starlette test client recommending `httpx2`.

## Static, compilation, JavaScript, and launcher gates

Commands:

```powershell
& <PYTHON> -m ruff check .
& <PYTHON> -m compileall -q app
Get-ChildItem frontend\js,e2e -Filter *.js | ForEach-Object { node --check $_.FullName }
node --check playwright.config.js
[System.Management.Automation.Language.Parser]::ParseFile((Resolve-Path .\start.ps1), [ref]$tokens, [ref]$errors)
```

Actual results:

```text
All checks passed!
compileall: passed
node --check: 8 files passed
PowerShell parser: passed (672 tokens)
```

## Playwright

Command:

```powershell
$env:GHOSTWATCH_PYTHON = '<PYTHON>'
npm run test:e2e
```

Actual result:

```text
Running 14 tests using 1 worker
14 passed (41.5s)
```

Node reported that `NO_COLOR` was ignored because `FORCE_COLOR` was set. This tooling warning was retained. The browser suite uses its checked-in synthetic local backend.

## Final archive procedure

The final artifact is built and verified with:

```powershell
& <PYTHON> scripts\package_release.py <OUTPUT>\ghostwatch-v3-rc5-final.zip
& <PYTHON> -m zipfile -t <OUTPUT>\ghostwatch-v3-rc5-final.zip
```

The independent reopen checks required source trees and evidence, forbidden-member exclusion, safe unique names, no ZIP link entries, default-deny type admission for every member, repository-contained non-linked source inventory, full privacy scanning, exact payload equality, ZIP integrity, and two independent SHA-256 calculations. Final member count and checksum are reported beside the artifact because embedding an archive's checksum inside itself would alter that checksum.

## Limitations

- Browser checks use synthetic data.
- No new live Windows Event Log integration probe was required for this packaging-only finding.
- Binary admission trusts exact reviewed paths and does not parse image formats.
- No penetration test or general-release certification was performed.

