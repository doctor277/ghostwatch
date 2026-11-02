# Ghostwatch V2 User Guide

## Start and stop

Run `start.ps1` from PowerShell. The dashboard opens at `http://127.0.0.1:8765`. Keep the PowerShell window open; press Ctrl+C there to stop the server. If the port is occupied or startup fails, the launcher prints an actionable error.

Ghostwatch works without administrator rights, but Windows may hide usernames, executable details, or connection ownership for protected processes.

## Read the dashboard

- **Overview:** current host health, trends, collector/storage state, and important alerts.
- **Network Intelligence:** connection metadata, filters, lifecycle state, remote rankings, and owning processes when known.
- **Network Graph:** bounded local/process/remote topology. Select a node for details; aggregate cards remain authoritative when the graph is sampled.
- **Process Explorer:** search and sort the process table; select a row for read-only details.
- **Process Tree:** parent/child relationships from accessible process metadata.
- **Detection Center:** review active alerts, acknowledge those under review, dismiss resolved/noise items, and inspect evidence.
- **Security Incidents:** group selected alerts into an incident, track status, and add analyst notes.
- **Event Timeline:** search recorded detections and operational events.
- **AI Analyst:** keep disabled unless a trusted local Ollama instance is running. Demo analysis is synthetic and does not call a model.
- **Performance:** collector timing, queue/client state, and resource context.
- **Reports:** choose a report family and JSON/CSV/HTML format; enable redaction before sharing.
- **Settings:** configure thresholds, duration, refresh interval, retention, event logging, and detection rules.

## Alert interpretation

An alert states the observed metric, configured threshold, source, time, severity, and evidence. It is a prompt for investigation, not a verdict. Correlate the process, destination, timeline, user context, and expected workload before taking action.

Acknowledgement means an analyst has seen the alert. Dismissal removes it from the active list but does not claim it was harmless. When persistence is disabled, live alert actions remain consistent during the running session but are not durable across restart.

## Demo mode

Demo mode is explicitly labeled and uses reserved documentation addresses plus synthetic processes. It is stored only in browser memory and never writes synthetic telemetry into the live SQLite database. Exit Demo mode before interpreting local-host status.

## Privacy and sharing

Reports can contain process names, usernames, and addresses. Use redaction when sharing outside the machine and inspect the result. Runtime databases and exported reports should be protected with normal Windows account and filesystem controls.

## Troubleshooting

- **Warming up:** wait for the first telemetry cycle.
- **WebSocket disconnected:** the page retries automatically; verify the PowerShell server is still running and reload.
- **Collector degraded:** open Performance/Overview and inspect unavailable metrics; protected Windows resources may be expected.
- **Storage degraded:** live monitoring continues; check filesystem permissions and free space. Avoid deleting a live WAL database.
- **AI unavailable:** verify AI is enabled, Ollama is local, the configured URL is loopback, and the model exists.
- **Port occupied:** stop the existing listener on 8765 or identify it before starting Ghostwatch. The supported port is fixed because the Host/Origin allowlist is intentionally strict.
