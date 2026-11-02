# GitHub Publication Checklist

No publication action is automated by this checklist.

## Owner decisions

- [x] Select a license from [`LICENSE_OPTIONS.md`](LICENSE_OPTIONS.md) and add the exact approved `LICENSE` text — MIT, confirmed by Rafi Dahoui.
- [ ] Choose the public repository name and description.
- [ ] Decide whether historical RC and V2 documents should remain on the default branch or move to a clearly labeled archive branch/directory in a future, reviewed change.
- [ ] Review the project name, screenshots, and descriptive claims for portfolio use.

## Source and documentation

- [x] Confirm `README.md` identifies the build as a local beta with limitations.
- [x] Confirm current beta evidence is separate from historical RC2–RC5 records.
- [x] Confirm the two private concept images are documented and excluded from public archives.
- [x] Confirm the current screenshot visibly says V3 and Demo Mode, Synthetic Data, Not Saved.
- [ ] Confirm installation and test commands work from a clean extraction.
- [ ] Confirm no generated cache, environment, runtime database, or Playwright report is committed.

## Privacy and security

- [x] Run the default-deny release packager against the exact source intended for upload.
- [x] Verify ZIP integrity and exact payload inventory.
- [x] Confirm the archive contains no `data/`, `.venv/`, `node_modules/`, cache, log, database, WAL/SHM, test-result, or live-telemetry files.
- [x] Confirm the privacy scan finds no usernames, profile paths, SIDs, credentials, API keys, bearer tokens, or private keys.
- [x] Confirm the only public binary asset is explicitly allowlisted by its complete archive path.
- [ ] Review Git history before publication; deleting a secret only from the latest tree does not remove it from history.
- [ ] Enable GitHub secret scanning and dependency alerts if available for the repository.

## Validation

- [x] Full Python suite passes: 222 passed, 1 warning.
- [x] Focused release-security suite passes: 77 passed.
- [x] Ruff and Python compilation pass.
- [x] JavaScript syntax and PowerShell parsing pass.
- [ ] Synthetic Playwright suite passes with no unexpected console errors.
- [x] Screenshot contains synthetic data only.
- [ ] Final sanitized archive SHA-256 is recorded in the release notes or publication description.

## Publication wording

- [x] Do not claim penetration-testing certification, malware-free status, public-release readiness, or protection equivalent to an EDR/antivirus product.
- [x] Attribute Windows test figures to the Windows closeout run.
- [x] Describe Linux review conclusions separately and do not merge its counts with Windows execution results.
- [x] State that Windows permissions can limit visibility and that unavailable values are not inferred.
- [x] State the local-user trust boundary and loopback-only deployment model.

