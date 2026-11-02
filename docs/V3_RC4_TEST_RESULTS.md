# GhostWatch V3 RC4 Test Results

## Environment and command notation

Commands were executed on Windows from `<PROJECT_ROOT>`, the isolated V3 staging tree. `<PYTHON>` denotes the existing Python 3.11+ virtual-environment interpreter used for verification. Reusing that interpreter did not modify the production V2 source tree.

## Complete Python suite

Command:

```powershell
& <PYTHON> -m pytest -q --basetemp=.tmp/pytest-rc4-release-final
```

Actual result:

```text
........................................................................ [ 48%]
........................................................................ [ 97%]
....                                                                     [100%]
148 passed, 1 warning in 19.83s
```

The warning was `StarletteDeprecationWarning`: the installed FastAPI test client imports the deprecated httpx-backed `starlette.testclient`; the environment recommends `httpx2`. It was not suppressed.

The previous 131-test baseline is preserved. RC4 adds 17 Python regressions.

## Focused verification

| Gate | Command | Actual result |
|---|---|---|
| RC4 regressions | `& <PYTHON> -m pytest -q --basetemp=.tmp/pytest-rc4-scanner tests/test_v3_rc4_hardening.py` | `17 passed in 5.37s` |
| Host/Origin/WebSocket | `& <PYTHON> -m pytest -q --basetemp=.tmp/pytest-rc4-security tests/test_websocket.py tests/test_api.py tests/test_service.py -k 'websocket or host or origin or security or send_timeout or registration_limit'` | `7 passed, 9 deselected, 1 warning in 1.74s` |
| Migration/integrity | `& <PYTHON> -m pytest -q --basetemp=.tmp/pytest-rc4-integrity tests/test_v3_history.py tests/test_v3_rc2_remediation.py tests/test_v3_rc3_data_integrity.py tests/test_v3_hardening.py tests/test_v3_rc4_hardening.py` | `58 passed, 1 warning in 13.84s` |
| Windows parser/recovery | `& <PYTHON> -m pytest -q --basetemp=.tmp/pytest-rc4-windows tests/test_v3_windows_events.py tests/test_v3_rc2_remediation.py -k 'windows or malformed or permission or recovery or empty'` | `12 passed, 7 deselected, 1 warning in 1.78s` |

Focused warnings are the same unsuppressed Starlette/httpx deprecation described above.

## Static and syntax gates

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

## Playwright browser suite

Command:

```powershell
$env:GHOSTWATCH_PYTHON = '<PYTHON>'
npm run test:e2e
```

The first run found one test-harness mismatch: the newly injected, expected HTTP 503 appeared in Chromium's console while the suite still required an empty console. The test was corrected to require exactly that controlled 503 and clear it only after the safe UI state had been asserted. No application behavior was weakened.

Final actual result:

```text
Running 14 tests using 1 worker
14 passed (38.0s)
```

Coverage includes every navigation workspace, process search/sort/detail, alerts, incidents, historical gaps/export, reports, file-integrity controls, demo isolation, accessible labels/focus, settings persistence, controlled settings failure, mobile navigation, ordinary WebSocket recovery, and three consecutive reconnect cycles.

Node emitted a tooling warning that `NO_COLOR` was ignored because `FORCE_COLOR` was set. It was not an application console failure and was not suppressed.

## Actual listener and Windows integration

A temporary staging Uvicorn server was launched with:

```powershell
& <PYTHON> -m uvicorn scripts.e2e_server:app --host 127.0.0.1 --port 8765
```

`Get-NetTCPConnection -State Listen -LocalPort 8765` and an HTTP health request reported:

```text
listener verification: health=200; addresses=127.0.0.1; listener_count=1; owning_processes=1
```

A read-only live Windows Event Log probe used:

```powershell
wevtutil qe System /c:1 /rd:true /f:xml
```

Actual result:

```text
wevtutil System read: unavailable; exit=5; diagnostic_chars=48
```

The host denied access. No event content or private diagnostic text was recorded. Permission fallback is covered by the 12 focused automated tests, but a successful live Windows event read is unverified on this host.

## Synthetic socket benchmarks

Commands:

```powershell
& <PYTHON> -m scripts.benchmark_sockets
& <PYTHON> -m scripts.benchmark_endurance
```

Actual single-pass result:

```text
sockets,elapsed_ms,peak_bytes,aggregate_count,detail_count
500,24.29,788096,500,250
1000,34.92,875164,1000,250
5000,105.62,3035652,5000,250
10000,202.40,4887388,10000,250
```

Actual repeated 10,000-socket result:

```text
sockets=10000
repeats=20
latency_min_ms=222.00
latency_median_ms=323.09
latency_p95_ms=360.47
latency_max_ms=360.47
traced_current_bytes=3429874
traced_peak_bytes=8550054
traced_current_growth_bytes=153249
lifecycle_entries=10000
recently_closed_entries=100
detail_entries=250
```

These are synthetic local measurements, not production capacity guarantees. The benchmark validates complete aggregate counting, the 250-row detail bound, bounded recent-closure state, and repeated churn invariants.

## Release archive verification

The release was built with:

```powershell
& <PYTHON> scripts\package_release.py <OUTPUT>\ghostwatch-v3-rc4.zip
& <PYTHON> -m zipfile -t <OUTPUT>\ghostwatch-v3-rc4.zip
```

An independent reopen then checked unique sorted member names, required files and source trees, every forbidden directory/name/suffix, the absence of private evidence and the live telemetry screenshot, the same sensitive-content rules, and exact equality with freshly generated scanned payloads.

Actual result before final checksum publication:

```text
files=87
Done testing
archive verification: passed; members=87; required=7; exact_payload_match=true
privacy rescan: passed; forbidden_members=0; sensitive_findings=0
```

The final SHA-256 is reported beside the archive rather than embedded here, because placing an archive's own checksum inside it would change that checksum.

## Remaining limitations

- A live successful Windows Event Log read could not be verified because the host returned access denied.
- Playwright used its checked-in synthetic backend. It does not replace long-duration observation of real host telemetry or every assistive-technology combination.
- The socket measurements are short synthetic benchmarks on one machine and are sensitive to host load.
- The privacy scanner cannot recognize every possible secret encoding or infer private content inside pixels; allowlisted images still require human review.
- No penetration test, hostile multi-user boundary test, or general-release certification was performed.

