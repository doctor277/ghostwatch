# GitHub Publication Checklist

No publication action is automated by this checklist. Checked items record facts that were directly verified; open items still require owner action or evidence.

## Confirmed public repository state - November 2, 2026

- [x] Public repository created at `doctor277/ghostwatch` with `main` as the default branch.
- [x] The two original commits were verified unchanged: `fda9810c91d2fdefe0cb5d6dcdfbeabb3651d4ee` and `6499c012d21763b8407d7520967f171d22b54294`.
- [x] Both original commits use a GitHub `noreply` author email; no personal author email is exposed in the published history.
- [x] The baseline public tree at `6499c012d21763b8407d7520967f171d22b54294` contains exactly 99 approved tracked files.
- [x] GitHub recognizes the root `LICENSE` as MIT.
- [x] The synthetic V3 Demo Mode screenshot is published at `docs/browser-demo-network-graph.png`.
- [x] `docs/design-concept.png` and `docs/ghostwatch-v2-concept.png` are absent from the public tree.
- [x] The repository description identifies the local-first Windows monitoring dashboard and its Python, FastAPI, SQLite, and JavaScript stack.
- [x] The eight requested repository topics were applied and read back on November 3, 2026.

## Owner and portfolio decisions

- [x] Select MIT and add the exact approved `LICENSE` text - confirmed by Rafi Dahoui.
- [x] Retain historical RC and V2 documents on the default branch, clearly labeled as historical engineering evidence.
- [x] Review the project name, synthetic screenshot, local-beta wording, and portfolio claims.
- [x] Review and approve the initial `chore/portfolio-polish` diff before its first push.
- [ ] Review and approve the final pull-request diff before merge.

## Source and documentation

- [x] `README.md` identifies V3 as a local beta with explicit limitations.
- [x] Current beta evidence is separated from historical RC2-RC5 and V2 records.
- [x] The two private concept images are documented and excluded from public archives.
- [x] The published screenshot visibly identifies V3, Demo Mode, synthetic data, and the not-saved boundary.
- [x] No generated cache, environment, runtime database, Playwright report, or test result is committed in the 99-file baseline.
- [ ] Confirm the revised installation and test commands from a clean clone after the portfolio branch is published.

## Privacy and release security

- [x] Run the default-deny release packager against the exact 99-file baseline source.
- [x] Verify ZIP integrity and exact payload inventory.
- [x] Confirm the archive contains no `data/`, `.venv/`, `node_modules/`, cache, log, database, WAL/SHM, test-result, or live-telemetry files.
- [x] Confirm the baseline privacy scan finds no usernames, profile paths, SIDs, credentials, API keys, bearer tokens, or private keys.
- [x] Confirm the only public binary asset is explicitly allowlisted by its complete archive path.
- [x] Review both original commits for author privacy and sensitive-content exposure before portfolio changes.
- [x] Verify GitHub secret scanning, push protection, and private vulnerability reporting are enabled.
- [ ] Enable GitHub Dependabot alerts and Dependabot security updates in repository settings.
- [ ] Decide whether to add CodeQL after CI is stable; no code-scanning analysis is currently configured.

## Validation evidence

- [x] Full Python suite passed on the accepted Windows closeout: 222 passed, 1 warning.
- [x] Focused release-security suite passed: 77 passed.
- [x] Ruff, Python compilation, JavaScript syntax, and PowerShell parsing passed.
- [x] Synthetic Playwright suite passed on November 1, 2026: 15 passed with no unexpected page-console errors.
- [x] Screenshot contains synthetic data only.
- [x] Verify the new GitHub Actions workflow in the first hosted pull-request run: [successful run](https://github.com/doctor277/ghostwatch/actions/runs/41774858301), with Python 3.11 and 3.13 each reporting 222 passed and 1 warning, Playwright Chromium reporting 15 passed, and Ruff, Python compilation, and JavaScript syntax passing.
- [ ] Record a final sanitized archive SHA-256 if a separate release archive is published.

## Publication wording

- [x] Do not claim penetration-testing certification, malware-free status, production readiness, or protection equivalent to an EDR/antivirus product.
- [x] Attribute Windows test figures to dated Windows runs.
- [x] Keep independent Linux review conclusions separate from Windows execution counts.
- [x] State that Windows permissions can limit visibility and unavailable values are not inferred.
- [x] State the local-user trust boundary and loopback-only deployment model.
