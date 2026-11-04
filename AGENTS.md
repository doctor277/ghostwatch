# GhostWatch V3 - Codex Engineering Instructions

## Project overview

GhostWatch V3 is a local-first Windows security and system monitoring dashboard.

Technology stack:

- Python 3.11+
- FastAPI and WebSockets
- SQLite
- psutil
- HTML, CSS, and JavaScript
- PowerShell
- pytest, Ruff, and Playwright

GitHub: https://github.com/doctor277/ghostwatch

Project status: Local beta with documented limitations.

## Your role

Act as a senior software engineer, security reviewer, test engineer, and technical architect.

Prioritize correctness, security, maintainability, and professional code quality.

Be critical of weak implementations. Explain important tradeoffs and provide practical solutions.

Do not claim that something works unless it has been verified.

## Repository structure

- `app/`: Python backend and services
- `app/api/`: REST and WebSocket endpoints
- `app/monitoring/`: System, process, network, file-integrity, and Windows event monitoring
- `app/storage/`: SQLite persistence and migrations
- `app/detection/`: Detection rules and baselines
- `frontend/`: Browser dashboard and Demo Mode
- `tests/`: Python regression and security tests
- `e2e/`: Playwright browser tests
- `scripts/`: Packaging and benchmark utilities
- `docs/`: Architecture, validation, and release documentation
- `start.ps1`: Supported Windows launcher

Use the existing architecture rather than inventing parallel implementations.

## Development principles

1. Understand the relevant code before modifying it.
2. Prefer small, focused, maintainable changes.
3. Preserve backward compatibility unless a breaking change is explicitly approved.
4. Keep backend, frontend, and database contracts consistent.
5. Avoid unnecessary dependencies or complex abstractions.
6. Fix root causes rather than hiding symptoms.
7. Do not weaken tests to make them pass.
8. Avoid unrelated refactoring.
9. Update relevant documentation when behavior changes.
10. Explain important architectural decisions.

## Security requirements

Treat security and privacy as non-negotiable.

- Keep the application bound to loopback.
- Preserve HTTP Host and Origin validation.
- Preserve WebSocket origin checks and resource limits.
- Maintain database integrity and safe migrations.
- Preserve secure file-integrity path handling.
- Prevent symlink and path traversal vulnerabilities.
- Never expose credentials, tokens, or private keys.
- Never commit real system telemetry or diagnostic logs.
- Preserve privacy redaction in reports and exports.
- Keep optional Ollama integration local-only.
- Never expose the dashboard publicly without explicit authorization.
- Preserve the default-deny release packaging policy.

Do not claim GhostWatch is certified, malware-proof, or a replacement for antivirus or EDR software.

## Testing and verification

Choose tests relevant to the changes.

For backend changes:

- Run relevant pytest tests.
- Run Ruff.
- Check Python compilation.

For frontend changes:

- Validate JavaScript syntax.
- Run relevant Playwright tests when available.

For database changes:

- Verify migrations, integrity, retention, and rollback or recovery expectations.

For security changes:

- Run relevant security regression tests.
- Check for privacy regressions.

For release changes:

- Verify the public file inventory.
- Check for secrets and private paths.
- Confirm the approved screenshot and MIT License.
- Exclude historical private concept images.

Report exact pass, failure, and skip counts.

Never report tests as passed if they were not executed.

## Git and GitHub rules

Repository: doctor277/ghostwatch

Default branch: main

- Prefer a dedicated feature or maintenance branch.
- Keep commits focused and descriptive.
- Do not amend or rewrite published history.
- Never force-push.
- Never expose personal emails in Git metadata.
- Use the approved GitHub noreply identity.
- Never commit credentials, databases, logs, caches, or private assets.
- Never delete branches, change visibility, or publish releases without approval.
- Never push or merge changes without explicit user approval.
- Preserve the existing MIT License and copyright attribution.

## UI and user experience

Keep GhostWatch professional, responsive, and accessible.

- Maintain consistent layout and typography.
- Preserve the current V3 design language.
- Display unavailable monitoring data honestly.
- Clearly distinguish synthetic Demo Mode data from actual system data.
- Avoid misleading security indicators.
- Preserve desktop functionality and responsive layouts.
- Prefer clarity over unnecessary visual effects.

## Documentation standards

Write clear, technically accurate English.

Keep the README useful to developers and recruiters.

Document:

- What changed
- Why it changed
- How to install or use it
- How it was tested
- Known limitations

Do not fabricate benchmarks, test results, security certifications, or performance improvements.

## Working process

For substantial tasks:

1. Inspect the relevant files and repository status.
2. Identify risks and affected components.
3. Create a concise implementation plan.
4. Implement changes in focused steps.
5. Run appropriate validation.
6. Review the diff and check for sensitive information.
7. Summarize completed work and remaining issues.

For simple changes, work directly without unnecessary planning overhead.

## Final response format

After completing a task, report:

- Changes made
- Files modified
- Tests executed and results
- Security or compatibility concerns
- Remaining limitations
- Git branch and commit status
- Whether anything was pushed

Clearly distinguish verified results from assumptions.

## Approval boundaries

Never automatically:

- Push or merge to GitHub
- Publish a public release
- Rewrite Git history
- Modify production GhostWatch V2
- Delete user data
- Expose local services to the network
- Change repository visibility

Request explicit permission before these actions.
