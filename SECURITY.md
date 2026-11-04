# Security Policy

## Supported version

GhostWatch V3 is a local beta. Security fixes are considered for the current `main` branch only. Historical V2 and RC2-RC5 snapshots are retained as engineering records and are not supported releases.

## Reporting a vulnerability

Use GitHub's private **Report a vulnerability** form in the repository's **Security** tab. Private vulnerability reporting was verified as enabled on November 2, 2026.

Please include:

- the affected file, endpoint, or security boundary;
- a minimal reproduction using synthetic data;
- the expected and observed behavior; and
- the potential local impact.

Do not open a public issue for an undisclosed vulnerability. Do not upload credentials, private paths, real Windows event data, runtime databases, packet captures, diagnostic archives, or screenshots containing host telemetry. If a minimal reproduction cannot be shared safely, describe the issue without the sensitive artifact.

Reports are reviewed on a best-effort basis; this portfolio project has no guaranteed response SLA, paid support, or bug-bounty program. Please allow time for validation before public disclosure.

## Security scope

The supported deployment is one Windows user running the launcher on one computer with Uvicorn bound to `127.0.0.1`. LAN exposure, reverse proxies, tunnels, port forwarding, multi-user access, and deployment credentials are outside the supported security model.

Host/Origin enforcement, redaction, bounded WebSockets, and the default-deny release packager are important boundaries, but they do not provide isolation from another native process running as the same Windows user. GhostWatch is not an antivirus, EDR replacement, or penetration-tested security product.
