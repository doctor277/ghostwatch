# GhostWatch V3 Current Beta Validation

## Scope

This document records GhostWatch V3 local-beta evidence in three separate groups: the latest published CI, local-only results on later commits, and dated historical snapshots. A result applies only to the commit and date it names. This document does not combine results from different commits or operating systems, and it does not imply penetration-testing certification. No public beta release has been created.

## Evidence status

| Group | Applies to | Result | Does not cover |
| --- | --- | --- | --- |
| Latest published CI | `c162eed` only, [hosted run 41974485263](https://github.com/doctor277/ghostwatch/actions/runs/41974485263) | Windows Python 3.11.9: 601 passed. Windows Python 3.13.17: 601 passed. Synthetic Chromium: 17 passed. Ruff, compilation and JavaScript syntax passed. One known Starlette/httpx deprecation warning per Python job | Any later commit |
| Local-only results, alert lifecycle fix | The threshold alert lifecycle commit on `fix/v3-alert-lifecycle`, based on `22a20a2`; see [below](#threshold-alert-lifecycle---november-10-2026) | 684 passed with one warning on Python 3.11.9. 42 passed on synthetic Chromium | Hosted CI, Python 3.13, any other browser engine |
| Local-only results, history fix | The history row-cap commit on `fix/v3-history-performance`, based on `7bae229`; see [below](#history-row-cap-retention---november-9-2026) | 663 passed with one warning on Python 3.11.9. 41 passed on synthetic Chromium | Hosted CI, Python 3.13, any other browser engine |
| Local-only results | `0332aa8` plus this documentation change; see [below](#local-only-results---november-7-2026) | 626 passed with one warning on Python 3.11.9. 41 passed on synthetic Chromium | Hosted CI, Python 3.13, any other browser engine |
| Dated historical snapshots | The sections below dated October 27 to November 3, 2026 | 222 Python and 15 Playwright, as recorded on each date | The current tree |

`c162eed` is the head of draft [PR #7](https://github.com/doctor277/ghostwatch/pull/7), which was open and unmerged at the last check on November 5, 2026. The merge decision belongs to the owner. The hosted figures above are carried over from that checkpoint and were not fetched again for the local run below.

## Threshold alert lifecycle - November 10, 2026

Status: local only, not pushed. **Hosted CI and Python 3.13 were not run on this
change.** It is one commit on `fix/v3-alert-lifecycle`, based on `22a20a2`, which
merged PR #8.

A sustained threshold breach now keeps one alert per rule. Later reports update its
occurrence count and last occurrence, a clear records a recovery on it, and a later
breach reopens it. Before this change, a sustained high-memory condition added a new
CRITICAL alert every 60 seconds. Security-event alerts are unchanged. The cause, the
behavior table, the tests and what remains are in the
[audit](V3_RESILIENCE_AUDIT.md#threshold-alert-lifecycle-2026-11-10).

Alerts written before this change are not migrated. Earlier per-cooldown rows stay as
they are until dismissed or removed by alert retention.

Environment: Windows 11, Python 3.11.9, Node 22.23.2, the installed Playwright
Chromium, and synthetic data only. No production or host database was read or changed.

| Command | Result |
| --- | --- |
| `python -m pytest -q --basetemp=<ignored scratch> -p no:cacheprovider` | `684 passed, 0 failed, 0 skipped, 1 warning` |
| `python -m pytest tests/test_v3_alert_lifecycle.py` (same options), first 17 tests before the fix | `16 failed, 1 passed` |
| `npm run test:e2e` | `42 passed, 0 failed, 0 skipped` |
| `python -m ruff check .` | `All checks passed!` |
| `python -m compileall -q app scripts tests` | Passed |
| `node --check`, the nine JavaScript files listed in the [README](../README.md#testing) | `9 passed, 0 failed` |
| `git diff --check` | Passed |

The release archive was not rebuilt for this change. The new test file falls under the
existing `tests` directory.

## History row-cap retention - November 9, 2026

Status: local only, not pushed. **Hosted CI and Python 3.13 were not run on this
change.** It is one commit on `fix/v3-history-performance`, based on `7bae229`, which
merged PR #7.

History row caps now count rows and delete only the oldest excess, instead of scanning
every row on each write. Retention behavior is unchanged. At the default settings after
7 days, a synthetic history write fell from 8,259 ms to about 960 ms at the median. The
cause, method, all measurements and what remains are in the
[audit](V3_RESILIENCE_AUDIT.md#history-row-cap-retention-2026-11-09).

Environment: Windows 11, Python 3.11.9, SQLite 3.45.1, Node 22.23.2, the installed
Playwright Chromium, and synthetic data only. No production or host database was read
or changed.

| Command | Result |
| --- | --- |
| `python -m pytest -q --basetemp=<ignored scratch> -p no:cacheprovider` | `663 passed, 0 failed, 0 skipped, 1 warning` |
| `python -m pytest tests/test_v3_history_retention.py` (same options), before the fix | `1 failed, 36 passed`: the work test, 16,001 against 263,001 VM steps |
| `npm run test:e2e` | `41 passed, 0 failed, 0 skipped` |
| `python -m ruff check .` | `All checks passed!` |
| `python -m compileall -q app scripts tests` | Passed |
| `node --check`, the nine JavaScript files listed in the [README](../README.md#testing) | `9 passed, 0 failed` |
| `git diff --check` | Passed |

The release archive was not rebuilt for this change. The new test file falls under the
existing `tests` directory.

## Local-only results - November 7, 2026

Status: local only, not pushed. **Hosted CI and Python 3.13 were not run on these changes.** These results describe local commits `31b26bd`, `7779a4a` and `0332aa8`, plus this uncommitted documentation change. They depend on PR #7 until that PR is merged.

Environment: Windows 11, Python 3.11.9 from an existing virtual environment, Node 22.23.2, the installed Playwright Chromium, and synthetic fixtures only.

| Command | Result |
| --- | --- |
| `python -m pytest -q --basetemp=<ignored scratch> -p no:cacheprovider` | `626 passed, 0 failed, 0 skipped, 1 warning` |
| `python -m pytest tests/test_v3_final_packaging.py tests/test_v3_rc4_hardening.py tests/test_v3_rc5_release.py -q` (same options) | `77 passed, 0 failed, 0 skipped` |
| `npm run test:e2e` | `41 passed, 0 failed, 0 skipped` |
| `python -m ruff check .` | `All checks passed!` |
| `python -m compileall -q app scripts tests` | Passed |
| `node --check`, the nine JavaScript files listed in the [README](../README.md#testing) | `9 passed, 0 failed` |
| PowerShell parser on `start.ps1` | `0 errors` |
| `git diff --check` | Passed |

`--basetemp` is required on this machine because the default user temp directory denies access. That is an environment limit, not a test failure. The published `601` and `17` figures are not results for this tree.

### Local release archive

`python scripts/package_release.py <ignored scratch>\ghostwatch-source.zip` built the archive under the ignored `.tmp/` directory. It was not published or uploaded, and the allowlist was not changed.

- Members: 118 unique entries, which is the 115 recorded at `c162eed` plus `e2e/telemetry-freshness.spec.js`, `scripts/benchmark_persistence.py` and `tests/test_v3_benchmark_persistence.py`. Every member is a tracked file. The three additions fall under the existing `e2e`, `scripts` and `tests` directories and are included intentionally: they use only synthetic data and are the documented way to repeat these checks.
- Tracked but excluded by the default-deny list: `.github/dependabot.yml`, `.github/workflows/ci.yml`, `AGENTS.md`, `CONTRIBUTING.md` and `SECURITY.md`.
- Root MIT `LICENSE` included. `docs/browser-demo-network-graph.png` is the only binary member and matches the tree byte for byte. Both concept images and `docs/browser-desktop.png` are absent.
- `verify_archive` passed, including the sensitive-payload scan. A separate scan of the members found no private key, API key, bearer token or local account name. Its SID, profile-path and email matches are all synthetic test fixtures. `docs/V3_RC3_TEST_RESULTS.md` keeps a historical repository folder name behind `<USER_HOME>`.
- All local Markdown links in the archive resolve except four: `README.md` and `docs/README.md` each link to `SECURITY.md` and `CONTRIBUTING.md`, which the archive excludes. Those links work in the repository. The allowlist was left unchanged, so this needs an owner decision before any archive is distributed.

The archive SHA-256 is reported with the local run, not here, because embedding it would change the archive it identifies.

### Not run

- Hosted CI, on any local commit: NOT RUN, because pushing needs owner approval.
- Python 3.13: NOT RUN. Only Python 3.11.9 and 3.14.3 are installed locally, and 3.14 has no project dependencies.
- Firefox, WebKit and native Edge: NOT RUN. Only Chromium assets are installed.
- `start.ps1` launch: NOT RUN. It would create an environment, install packages over the network and start real collectors. Only its syntax was checked. The Playwright server covers the loopback path with synthetic data.
- A 30-60 minute stability run and an eight-hour soak: NOT RUN. See the [audit](V3_RESILIENCE_AUDIT.md#stability-run).
- A complete default `benchmark_persistence` run in its final order: NOT VERIFIED. See the [audit](V3_RESILIENCE_AUDIT.md#environment-and-commands), which also holds the [benchmark measurements](V3_RESILIENCE_AUDIT.md#synthetic-persistence-benchmark-2026-11-07).

### Owner approval and manual checks

Code-quality recommendation from the merge-gate recheck: **APPROVE WITH FOLLOW-UPS**. See the [audit](V3_RESILIENCE_AUDIT.md#merge-gate-recheck-2026-11-05). This is not a GitHub approval.

1. Review the local commits, then decide whether to authorize any push.
2. Decide whether and when to merge PR #7. The watchdog commit depends on it.
3. After any authorized push, confirm hosted CI on the pushed commit, including Python 3.13.
4. Decide whether Firefox, WebKit and native Edge coverage is needed.
5. Decide whether to run a stability run or an eight-hour soak before claiming long-run behavior.
6. Run `start.ps1` manually on the intended machine. Confirm that non-elevated gaps in process, socket-owner and Event Log data are shown as unavailable, and that real data stays visually distinct from Demo Mode.
7. Decide how to handle the four archive links above, then rebuild and hash any archive from the exact commit being released.

## Accepted Windows closeout — October 27, 2026

Environment: Windows 11, loopback-only runtime, Python 3.11+ project environment, Chromium Playwright runner, synthetic browser fixtures.

Reported results:

- Python: `222 passed, 0 failed, 0 skipped, 1 warning`
- Playwright: `15 passed, 0 failed, 0 skipped`
- Focused release security: `77 passed, 0 failed, 0 skipped`
- Ruff: passed
- Python compilation: passed
- JavaScript syntax: 8 files passed
- PowerShell parser: passed
- SQLite schema: 3; integrity check: `ok`
- Live Host, Origin, and WebSocket security checks: passed

The warning was a Starlette/httpx TestClient deprecation warning. Chromium desktop and a Pixel 7 responsive emulation were tested. Firefox, WebKit, and native Edge-specific execution were not tested because their Playwright browser assets were not installed.

These are Windows closeout results. They must not be attributed to the independent Linux source audit.

## Commands

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m pytest tests\test_v3_final_packaging.py tests\test_v3_rc4_hardening.py tests\test_v3_rc5_release.py
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m compileall -q app scripts
node --check frontend\js\charts.js
node --check frontend\js\dashboard.js
node --check frontend\js\demo.js
node --check frontend\js\network-graph.js
node --check frontend\js\websocket.js
node --check e2e\dashboard.spec.js
node --check e2e\synthetic-backend.js
node --check playwright.config.js
npm ci
npm run test:e2e
```

## Portfolio-preparation rerun

Executed on Windows on November 1, 2026 after the README, documentation index, publication checklist, license-choice summary, and synthetic Demo Mode screenshot were updated:

- Full Python suite: `222 passed, 0 failed, 0 skipped, 1 warning in 61.24s`
- Focused release security: `77 passed, 0 failed, 0 skipped in 13.15s`
- Playwright: `15 passed, 0 failed, 0 skipped in 1.5m`
- Ruff: `All checks passed!`
- Python compilation: passed for `app` and `scripts`
- JavaScript syntax: `8 passed, 0 failed`
- PowerShell parser: `0 errors`
- Documentation artifact audit: passed; all local Markdown links resolve
- Demo screenshot: V3 title and branding verified, synthetic/not-saved banner visible, no browser console warnings or errors

The Playwright rerun used the already-installed Chromium revision, synthetic REST/WebSocket fixtures, 14 desktop cases, and one Pixel 7 responsive case. It emitted the same non-failing Node warning that `NO_COLOR` was ignored because `FORCE_COLOR` was set.

The documentation audit reported progressive-adoption warnings for absent `CONTRACT.md`, `TESTS.md`, and `REGRESSION.md` roles. Those roles are already covered by the application source, this validation record, and the checked-in test suites; duplicate owner documents were not created. It also identified excluded pytest fixture files under `data/` as possible orphans. No broken local Markdown link was found.

Current-source packaging is intentionally executed after this document is finalized so the ZIP can be compared exactly with the documented source tree. The resulting archive path, member count, integrity result, and SHA-256 are reported with the delivered artifact rather than embedded here and causing a self-referential archive hash.

## Current limitations

- Cross-engine browser behavior remains unverified outside Chromium.
- The short acceptance run does not replace an eight-hour or multi-day soak.
- Windows permissions can make process, socket-owner, or Event Log metadata partial or unavailable.
- Historic collection-failure records are preserved as evidence and are not silently rewritten.
- Optional local AI output is not a security verdict.
- Pending writes are held in bounded memory, so abrupt termination can lose them. There is no durable exactly-once guarantee across a crash.
- Browser evidence uses synthetic fixtures. It does not show the dashboard running against real collectors.
- Git commit SHAs now identify source states, and an archive SHA-256 identifies one generated archive. The earlier source snapshot had no Git metadata, so its archive SHA-256 was its only identity.

## Telemetry freshness watchdog - November 5, 2026

Status: local only, not pushed, and not covered by hosted CI. These results describe this change before it was committed as `7779a4a`, on top of `31b26bd`, on Windows 11, Python 3.11.9, Node 22.23.2 and synthetic Chromium. Python 3.13 was NOT VERIFIED for this change. The feature depends on PR #7, which remains draft and unmerged, so it should not be published until that PR is resolved.

`frontend/js/websocket.js` keeps one watchdog per `TelemetrySocket`:

- Timeout: `max(10 s, 3 x refresh_interval + 5 s)`. That is 11 s at the default 2 s, 10 s at 1 s, and 95 s at 30 s. The service waits a full interval and then collects, so three intervals plus the margin allow for collection time and scheduling delay.
- If settings failed to load, or the interval is outside 1-30 s, the timeout uses 30 s (95 s). A late warning is preferred to a false one.
- The interval is read when a socket opens and when a valid snapshot arrives. A saved interval therefore cannot shorten the wait for a frame the old interval is still due to deliver.
- Only a snapshot that passes the shape check and renders without throwing refreshes the watchdog. Degraded frames, malformed JSON and other JSON do not.
- Frames move a `performance.now()` stamp. One timer rechecks the elapsed time when it fires, so a delayed timer in a throttled tab does not misreport.

| Status | Meaning |
| --- | --- |
| `CONNECTING`, `RECONNECTING` | No open socket |
| `AWAITING TELEMETRY` | Socket open, no valid snapshot yet. This replaces the green `CONNECTED` previously shown on open |
| `CONNECTED` | Valid snapshot received within the timeout. This is the only green status |
| `TELEMETRY STALE` | Socket open, no valid snapshot within the timeout. The last values stay visible, and `#last-update` marks them stale |
| `TELEMETRY DEGRADED`, `DATA ERROR` | The latest frame was unusable. These are not replaced by stale |
| `DEMO DATA` | Demo Mode. No socket or watchdog runs |

The `MONITORING` and `LIVE` labels follow the same colour. A collection slower than the timeout shows amber until its frame arrives, which is an accurate description of what the browser has received. Collection time itself has no hard limit in this change.

Pre-fix evidence, with an extracted `31b26bd` tree plus the final `e2e/telemetry-freshness.spec.js` and `e2e/synthetic-backend.js`: 23 failed, 1 passed. The silent-socket test reported `Expected: "TELEMETRY STALE"`, `Received: "CONNECTED"`. The test that passed covers a snapshot that throws while rendering, which the earlier code already reported as `DATA ERROR`.

Results after the change:

- `npx playwright test e2e/telemetry-freshness.spec.js --repeat-each=3`: `72 passed`
- `npm run test:e2e`: `41 passed, 0 failed, 0 skipped` (39 desktop Chromium, 2 Pixel 7 emulation, 24 of the 41 from the new spec)
- `python -m pytest -q --basetemp=<ignored scratch>`: `622 passed, 0 failed, 0 skipped, 1 warning`
- `python -m ruff check .`, `python -m compileall -q app scripts tests`, and `node --check` on all nine JavaScript files: passed

Limits: a socket that connects receives the cached latest snapshot, so a reload or reconnect after collection has stopped shows `CONNECTED` for up to one timeout before `TELEMETRY STALE`. The watchdog measures frame arrival, not the age of the snapshot. Time is driven by Playwright's fake clock against the synthetic WebSocket harness. No real throttled background tab, real backend outage or browser other than Chromium was exercised.

## GitHub portfolio upgrade verification - November 3, 2026

Executed on Windows from the local `chore/portfolio-polish` branch after adding repository documentation, CI, and dependency automation. No application, frontend, database, V2, or historical RC implementation file changed.

- Full Python suite on Python 3.11.9: `222 passed, 0 failed, 0 skipped`
- Focused release-security suite: `77 passed, 0 failed, 0 skipped in 13.81s`
- Synthetic Playwright Chromium suite: `15 passed, 0 failed, 0 skipped` in approximately 94 seconds
- Ruff: `All checks passed!`
- Python compilation: passed for `app`, `scripts`, and `tests`
- JavaScript syntax: `8 passed, 0 failed`
- PowerShell parser: `0 errors`
- npm lockfile audit: `0 vulnerabilities`
- Python advisory audit: 30 resolved dependencies checked, `0 vulnerabilities`
- GitHub Actions workflow: actionlint passed using `rhysd/actionlint:1.7.12`
- Dependabot YAML: parsed and validated for pip, npm, and GitHub Actions
- Local Markdown links: `53 checked, 0 broken`
- Default-deny release scan: 99 selected files, `0 sensitive findings`
- Approved screenshot SHA-256 and visual synthetic-data markers: verified
- Excluded concept-image paths: absent from the public source tree

The first full pytest attempt in the managed execution environment reported `97 passed, 125 setup errors` because its default user temp directory was not writable. No test assertion failed. The unchanged suite was rerun with `TEMP` and `TMP` pointed to the repository's ignored `.tmp` directory; the authoritative JUnit result records all 222 tests passing.

Python 3.13 Windows wheels were available for all resolved pinned requirements, but the Python 3.13 test matrix was not executed locally. At that point, it remained a hosted-CI verification item. A Python 3.14 dependency-install experiment did not complete and was removed from the proposed CI matrix rather than publishing an unreliable job.

### First hosted GitHub Actions run

The first hosted [GitHub Actions run](https://github.com/doctor277/ghostwatch/actions/runs/41774858301) for pull request #1 completed successfully:

- Python 3.11: `222 passed, 0 failed, 0 skipped, 1 warning`
- Python 3.13: `222 passed, 0 failed, 0 skipped, 1 warning`
- Synthetic Playwright Chromium: `15 passed, 0 failed, 0 skipped`
- Ruff: passed in both Python matrix jobs
- Python compilation: passed in both Python matrix jobs
- JavaScript syntax: passed in the browser job

These hosted results close the previously open workflow-execution and Python 3.13 verification items. Python 3.13 was not executed locally for the portfolio upgrade; its test evidence is from the hosted Windows runner. The dated local results above remain separate and unchanged.

## MIT license finalization — November 2, 2026

The owner confirmed the legal name `Rafi Dahoui` and confirmed ownership or permission to release the GhostWatch-authored source, current V3 interface, and synthetic Demo Mode screenshot under MIT.

Changes were limited to licensing, public documentation, asset-release policy, package metadata, and focused packaging regressions. No application collector, detection, API, storage, frontend behavior, or security control was changed.

Final verification before archive creation:

- Full Python suite: `222 passed, 0 failed, 0 skipped, 1 warning in 24.47s`
- Focused release-security suite: `77 passed, 0 failed, 0 skipped in 6.67s`
- Focused MIT packaging tests: `27 passed in 1.18s`
- Corrected approved-binary symlink regression: `1 passed in 0.08s`
- Ruff: `All checks passed!`
- Python compilation: passed for `app` and `scripts`
- JavaScript syntax: `8 passed, 0 failed`
- PowerShell parser: `0 errors`
- Preflight release privacy and policy scan: passed
- Preflight members: 99 unique entries
- Root MIT `LICENSE`: included
- Synthetic V3 Demo Mode screenshot: included
- Both private concept images: excluded
- Third-party notices and asset-provenance record: included

The first complete run after changing the binary asset policy reported `221 passed, 1 failed`. The failure was an outdated security-test fixture that still used the newly excluded `design-concept.png` as an approved-binary symlink target. The fixture was changed to the still-approved `browser-demo-network-graph.png`, preserving the intended symlink rejection assertion; the targeted test and the complete suite then passed. No test was weakened or removed.

The one remaining warning is the known Starlette/httpx TestClient deprecation warning. The final archive hash is reported externally so that it identifies the exact completed payload without creating a self-referential documentation update.

