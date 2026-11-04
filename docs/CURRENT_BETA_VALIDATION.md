# GhostWatch V3 Current Beta Validation

## Scope

This document records the accepted Windows local-beta closeout and the subsequent portfolio-documentation verification. It does not combine results from different operating systems or imply penetration-testing certification.

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
- No Git metadata is present in this source snapshot, so an archive SHA-256 is the publication snapshot identity.

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

Python 3.13 Windows wheels were available for all resolved pinned requirements, but the Python 3.13 test matrix was not executed locally. It remains a hosted-CI verification item. A Python 3.14 dependency-install experiment did not complete and was removed from the proposed CI matrix rather than publishing an unreliable job.

The GitHub Actions workflow has been validated locally but has not run on GitHub. No CI badge is shown until a hosted run exists.

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

