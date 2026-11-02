# GhostWatch V3 RC3 Test Results

Date: 2026-10-22  
Interpreter: Python 3.14.3 from the existing GhostWatch V2 virtual environment  
Working tree: isolated V3 staging tree

## Complete Python suite

Command:

```powershell
& '<USER_HOME>\rea-work\ghostwatch\.venv\Scripts\python.exe' -m pytest -q --basetemp=.tmp/pytest-rc3-final
```

Actual final output:

```text
........................................................................ [ 54%]
...........................................................              [100%]
============================== warnings summary ===============================
..\..\..\..\..\..\rea-work\ghostwatch\.venv\Lib\site-packages\fastapi\testclient.py:1
  <USER_HOME>\rea-work\ghostwatch\.venv\Lib\site-packages\fastapi\testclient.py:1: StarletteDeprecationWarning: Using `httpx` with `starlette.testclient` is deprecated; install `httpx2` instead.
    from starlette.testclient import TestClient as TestClient  # noqa

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
131 passed, 1 warning in 16.30s
```

The 131 tests comprise all 118 pre-RC3 tests without changed assertions plus 13 new RC3 regression tests.

Intermediate results retained for transparency:

- The first final-suite candidate produced `126 passed, 2 failed, 1 warning`. One failure exposed a pre-existing internal equal-bound diagnostic point lookup; compatibility was restored without changing the public half-open API. The other was an overly specific error-message regular expression in a newly added RC3 test; it was corrected to match the actual strict timezone diagnostic.
- An initial sandboxed focused run could not create pytest's default Windows temporary directory, so no test bodies executed (`10 errors`). All authoritative runs used the workspace-local `--basetemp` shown above.

## RC3 regression module

```text
.............                                                            [100%]
13 passed, 1 warning in 2.99s
```

## Ruff and Python compilation

Commands:

```powershell
& '<USER_HOME>\rea-work\ghostwatch\.venv\Scripts\python.exe' -m ruff check .
& '<USER_HOME>\rea-work\ghostwatch\.venv\Scripts\python.exe' -m compileall app
```

Actual output:

```text
All checks passed!
Listing 'app'...
Listing 'app\analytics'...
Listing 'app\api'...
Listing 'app\detection'...
Listing 'app\monitoring'...
Listing 'app\storage'...
```

Both commands exited 0.

## JavaScript and PowerShell syntax

Commands:

```powershell
$files = @((Get-ChildItem frontend/js,e2e -Filter *.js -File -Recurse).FullName) + (Resolve-Path playwright.config.js).Path
foreach ($file in $files) { node --check $file; if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE } }

$tokens = $null
$parseErrors = $null
[void][System.Management.Automation.Language.Parser]::ParseFile((Resolve-Path start.ps1), [ref]$tokens, [ref]$parseErrors)
```

Actual output:

```text
JavaScript syntax checks passed: 8 files
PowerShell syntax check passed: start.ps1
```

## Playwright browser suite

Command:

```powershell
$env:GHOSTWATCH_PYTHON='<USER_HOME>\rea-work\ghostwatch\.venv\Scripts\python.exe'
npm run test:e2e
```

Actual result:

```text
Running 12 tests using 1 worker
12 passed (29.5s)
```

Covered desktop/mobile navigation, process search/sort/drawer, alert actions and counters, settings persistence, historical gaps/export, incidents, reports, file-integrity controls, demo isolation, accessibility, WebSocket recovery, and mobile navigation. The synthetic server reported `Uvicorn running on http://127.0.0.1:8765`.

An earlier complete run also passed all 12 tests in 35.8 seconds. The 29.5-second result above is the final rerun against the finalized code.

Non-failing warning: Node reported that `NO_COLOR` was ignored because `FORCE_COLOR` was set.

## Focused security, migration, and Windows checks

Host/Origin and WebSocket regression command selected `tests/test_websocket.py`, HTTP origin enforcement, bounded queue behavior, and send timeout tests.

```text
8 passed, 1 warning in 2.10s
```

Migration/integrity command selected supported v2 migration, transaction rollback, corrupt-database preservation, future-schema WAL/SHM immutability, interrupted migration recovery, UTC canonicalization, and legacy-rollup restart reconciliation.

```text
8 passed, 1 warning in 2.05s
```

Windows Event parser, storage, API, correlation, cursor, malformed XML, empty response, permission failure, and recovery tests:

```text
12 passed, 7 deselected, 1 warning in 1.50s
```

Live read-only integration checks:

```text
Windows Event read check unavailable: channel=System exit=5
Windows Event read check unavailable: channel=Application exit=5
```

The current account lacked Event Log read permission. No Windows security setting was changed.

## Synthetic socket benchmarks

Commands:

```powershell
& '<USER_HOME>\rea-work\ghostwatch\.venv\Scripts\python.exe' -m scripts.benchmark_sockets
& '<USER_HOME>\rea-work\ghostwatch\.venv\Scripts\python.exe' -m scripts.benchmark_endurance
```

Actual scale sweep:

```text
sockets,elapsed_ms,peak_bytes,aggregate_count,detail_count
500,46.41,788096,500,250
1000,47.04,875164,1000,250
5000,136.99,3035652,5000,250
10000,277.59,4887388,10000,250
```

Actual 20-cycle 10,000-socket churn result:

```text
sockets=10000
repeats=20
latency_min_ms=257.58
latency_median_ms=389.83
latency_p95_ms=448.01
latency_max_ms=448.01
traced_current_bytes=3429888
traced_peak_bytes=8550045
traced_current_growth_bytes=153263
lifecycle_entries=10000
recently_closed_entries=100
detail_entries=250
```

These are synthetic results from this host and run. They are not long-duration production measurements.

## Quality-gate assessment

- Python suite: pass with one documented upstream warning.
- Ruff: pass.
- Python compilation: pass.
- JavaScript syntax: pass.
- PowerShell syntax: pass.
- Playwright: pass.
- Migration/SQLite integrity: pass.
- Host/Origin/WebSocket: pass.
- Synthetic performance: completed; invariants held.
- Live Windows Event integration: unavailable due to access denied; fallback behavior passed automated coverage.

RC3 is suitable for independent release-candidate inspection. This is not a general-availability declaration.
