# Ghostwatch V2 Features

## Monitoring and investigation

- System CPU, memory, disk, uptime, load, network rates, process totals, and collector health.
- Full-list established TCP aggregation with a 250-row display cap.
- Per-process connection counts where the OS exposes an owning PID.
- Connection lifecycle state, scope classification, protocol/state/search filters, and remote/process rankings.
- Incremental process sampling with name, PID, user when available, CPU, memory, status, start time, connection count, and read-only detail drawer.
- Process tree, sortable process table, accessible sort state, and search.
- Canvas network topology with bounded nodes/edges and selectable local/process/remote nodes.

## Detection and response workflow

- Explainable, configurable rules with duration and cooldown controls.
- Bounded live alert state independent of SQLite logging.
- Alert acknowledgement and dismissal with duplicate suppression.
- Incidents with severity, timestamps, linked alert/process/connection references, evidence summary, status, and analyst notes.
- Event timeline with search, severity/type API filtering, and truthful empty states.
- Storage and collector degradation surfaced as telemetry rather than hidden.

## Analysis and reporting

- In-memory rolling baseline and anomaly context.
- Eight report families: system health, network activity, security alerts, process activity, anomaly summary, incidents, daily summary, and weekly summary.
- JSON, CSV, and HTML export; optional sensitive-field redaction.
- Optional Ollama analyst with strict loopback URL validation, bounded prompt material, disabled-by-default operation, no tools, and no cloud fallback.

## Operations and UX

- Twelve dashboard workspaces with desktop and mobile navigation.
- WebSocket status/reconnect behavior and bounded server/client queues.
- Browser-memory-only synthetic Demo mode with persistent visual disclosure.
- Responsive layout, labeled fields, keyboard-operable navigation and sort controls, focus styles, and reduced-motion support.
- Windows launcher validates Python >= 3.11, caches the requirements hash, checks the port, waits for readiness, and handles browser-launch failure.

## Privacy posture

Ghostwatch processes metadata locally. It does not capture payloads, credentials, cookies, browser history, or keystrokes. No telemetry, report, or AI prompt is sent to a remote service by the application.
