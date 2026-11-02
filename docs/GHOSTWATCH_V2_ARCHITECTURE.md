# Ghostwatch V2 Architecture

## Runtime boundary

Ghostwatch is one local Uvicorn/FastAPI process serving a static, dependency-free dashboard and a loopback API at `127.0.0.1:8765`. The supported launcher fixes the bind address. The browser receives an initial WebSocket snapshot and bounded updates; REST endpoints provide configuration, events, incidents, reports, and optional AI analysis.

```text
Windows + psutil
  -> resilient collectors
  -> TelemetryService
       -> bounded history/live-alert state
       -> rules + baseline engine
       -> SQLite persistence (optional event logging)
       -> per-client bounded WebSocket queues
  -> FastAPI REST/WebSocket
  -> local browser dashboard
```

## Backend modules

- `app/monitoring/system.py` collects CPU, memory, disk, boot, and process totals. Individual failures mark named metrics unavailable.
- `app/monitoring/network.py` aggregates every TCP connection for totals and per-process counts while limiting detailed rows to 250. It tracks lifecycle state and remote rankings without payload inspection.
- `app/monitoring/processes.py` keeps an incremental cache and rotates expensive process enrichment within a budget. Disappearing and inaccessible processes do not abort a collection cycle.
- `app/detection/rules.py` evaluates configured CPU, memory, upload, connection, process-count, disk-space, destination-diversity, connection-growth, and collection-health rules.
- `app/detection/engine.py` enforces sustained-duration and cooldown semantics and creates stable evidence-bearing alert IDs.
- `app/analytics/baseline.py` maintains in-memory rolling baselines and anomaly observations.
- `app/service.py` owns lifecycle, collection, bounded history, bounded live alerts, storage isolation/recovery, incidents, and WebSocket fan-out.
- `app/storage/database.py` provides schema version 2, WAL mode, parameterized queries, migrations, retention, online backup, settings, events, alerts, and incidents.
- `app/reporting.py` renders JSON, CSV, and HTML with redaction, HTML escaping, and spreadsheet-formula neutralization.
- `app/ai.py` performs optional loopback-only Ollama calls. It is disabled by default and has no tool execution or cloud fallback.

## Frontend architecture

`frontend/index.html` defines semantic navigation and labeled controls. `dashboard.js` owns state/rendering and delegates reusable charts and topology to `charts.js` and `network-graph.js`. `websocket.js` provides reconnect/backoff status. `demo.js` creates deterministic, documentation-safe synthetic telemetry. No framework, CDN, runtime package download, `innerHTML`, `eval`, or third-party script is used.

The graph is intentionally bounded to 60 process nodes, 180 remote nodes, and 240 edges. Tables use truthful empty states rather than fabricated content.

## Persistence and failure domains

Live alerts and persistent alerts are separate. Disabling logging affects SQLite writes, not live detections. Storage failures are surfaced in health/status telemetry, queued where safe, and do not terminate collection. Lowering alert retention causes immediate enforcement.

Collector failure is represented through `unavailable_metrics` and `collection_error`; zero is not substituted for an unavailable observation when that would be misleading.

## Trust boundary

Browser access is constrained by exact Host/Origin checks and security headers. Native processes running under the same local OS account remain trusted. This design is intentionally local-only and unauthenticated; it must not be exposed to a LAN or public interface.
