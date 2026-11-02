# GhostWatch V3 RC5 Test Results

## Command notation

Commands were executed on Windows from `<PROJECT_ROOT>`, the isolated V3 staging tree. `<PYTHON>` denotes the existing Python 3.11+ virtual-environment interpreter used for verification. `<OUTPUT>` denotes the designated release-output folder.

## Complete Python suite

Command:

```powershell
& <PYTHON> -m pytest -q --basetemp=.tmp/pytest-rc5-final
```

Actual result:

```text
........................................................................ [ 39%]
........................................................................ [ 79%]
.....................................                                    [100%]
181 passed, 1 warning in 26.54s
```

The 148-test RC4 baseline is preserved and RC5 adds 33 tests. The warning is the existing unsuppressed `StarletteDeprecationWarning` from the installed FastAPI/Starlette test client recommending `httpx2`.

## Focused security and reliability suites

| Gate | Command summary | Actual result |
|---|---|---|
| RC5 release inventory/privacy | `pytest tests/test_v3_rc5_release.py tests/test_v3_rc4_hardening.py -k <release-security-expression>` | `43 passed, 7 deselected in 3.47s` |
| Host/Origin/WebSocket | `pytest tests/test_websocket.py tests/test_api.py tests/test_service.py -k 'websocket or host or origin or security or send_timeout or registration_limit'` | `7 passed, 9 deselected, 1 warning in 1.45s` |
| SQLite migration/integrity | `pytest tests/test_v3_history.py tests/test_v3_rc2_remediation.py tests/test_v3_rc3_data_integrity.py tests/test_v3_hardening.py tests/test_v3_rc4_hardening.py` | `59 passed, 1 warning in 16.15s` |
| File integrity monitoring | `pytest tests/test_v3_file_integrity.py tests/test_v3_rc4_hardening.py -k 'file_integrity'` | `8 passed, 14 deselected, 1 warning in 1.28s` |

The focused warnings are the same Starlette/httpx warning and were not suppressed.

## Static, syntax, and launcher gates

Commands:

```powershell
& <PYTHON> -m ruff check .
& <PYTHON> -m compileall -q app
Get-ChildItem frontend\js,e2e -Filter *.js | ForEach-Object { node --check $_.FullName }
node --check playwright.config.js
[System.Management.Automation.Language.Parser]::ParseFile((Resolve-Path .\start.ps1), [ref]$tokens, [ref]$errors)
```

Initial Ruff result:

```text
Found 20 errors.
```

All 20 were in generated `.tmp/pytest-rc5-*` adversarial fixture files. The fixtures were corrected and `.tmp` was explicitly identified as generated test workspace in Ruff configuration.

Final actual result:

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
14 passed (45.5s)
```

Node reported that `NO_COLOR` was ignored because `FORCE_COLOR` was set. This tooling warning was retained. Browser data and API/WebSocket failures were synthetic; no application console failure escaped the tests.

## Network collector benchmarks

Commands:

```powershell
& <PYTHON> -m scripts.benchmark_sockets
& <PYTHON> -m scripts.benchmark_endurance
```

Actual single-pass result:

```text
sockets,elapsed_ms,peak_bytes,aggregate_count,detail_count
500,26.54,788096,500,250
1000,36.09,875164,1000,250
5000,118.47,3035652,5000,250
10000,265.97,4887388,10000,250
```

Actual repeated 10,000-socket result:

```text
sockets=10000
repeats=20
latency_min_ms=251.95
latency_median_ms=675.21
latency_p95_ms=926.33
latency_max_ms=926.33
traced_current_bytes=3429891
traced_peak_bytes=8550045
traced_current_growth_bytes=153266
lifecycle_entries=10000
recently_closed_entries=100
detail_entries=250
```

These are synthetic measurements from one host under its then-current load. They demonstrate aggregate/detail invariants and bounded tracked state, not a production latency guarantee.

## Release archive verification procedure

The final artifact is built and checked with:

```powershell
& <PYTHON> scripts\package_release.py <OUTPUT>\ghostwatch-v3-rc5.zip
& <PYTHON> -m zipfile -t <OUTPUT>\ghostwatch-v3-rc5.zip
```

The independent reopen verifies sorted unique inventory, safe normalized names, no ZIP symlink entries, source-tree inclusion, forbidden-member exclusion, the full privacy scan, exact equality with a newly selected and securely read payload set, and SHA-256 agreement. Final member count and checksum are reported beside the completed artifact because embedding an archive's own checksum inside it would change that checksum.

## Remaining limitations

- Playwright uses a synthetic local backend and does not replace prolonged live-host operation or every assistive-technology combination.
- Network benchmark timing varies with host load.
- The privacy scan has the representation and image-analysis limitations documented in `docs/V3_RC5_REMEDIATION.md`.
- A live successful Windows Event Log read was not part of the RC5 packaging finding verification; prior RC4 validation documented access denial on this host and tested the permission fallback.
- No penetration test or general-release certification was performed.

