# Contributing

GhostWatch V3 is a Windows-focused local beta. Focused bug fixes, tests, documentation corrections, accessibility improvements, and security hardening are welcome. Large feature additions or changes to the loopback, privacy, release-packaging, or data-retention boundaries should be discussed in an issue before implementation.

## Development setup

Use Windows 10/11, PowerShell, Python 3.11 or newer, and Node.js 20 or newer for browser tests. Follow the setup and test commands in [README.md](README.md).

Before opening a pull request, run the full Python suite, Ruff, Python compilation, JavaScript syntax checks, and the synthetic Playwright suite. Add or update tests for behavior changes, keep commits focused, and explain any check that could not be run.

## Safety and privacy

- Keep the application loopback-only and preserve existing Host/Origin and WebSocket checks.
- Use temporary or synthetic test data. Never commit real telemetry, databases, logs, credentials, private paths, or diagnostic archives.
- Do not add the excluded concept images or unreviewed binary assets.
- Do not weaken security or release-packaging tests to make a change pass.
- Keep historical V2 and RC2-RC5 records intact unless a narrowly scoped correction is clearly labeled.

Security vulnerabilities should be reported privately according to [SECURITY.md](SECURITY.md), not through a public issue.
