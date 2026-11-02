# Ghostwatch V2 Security Review

This was a defensive source and behavior review, not penetration testing.

## Findings addressed

1. **Truncated socket totals:** aggregate counts and detection inputs now traverse the full connection list; only rendered detail rows remain capped at 250.
2. **Live alerts coupled to logging:** bounded live alert state is independent of persistence, so disabling SQLite event logging no longer hides detections.
3. **Browser boundary weakness:** HTTP Host values and WebSocket Host/Origin values use exact allowlists. Unsafe HTTP methods reject a supplied non-local Origin.
4. **Collector reliability:** system and network failures produce explicit unavailable/error state without crashing the telemetry loop.
5. **WebSocket resource bounds:** client count, per-client queue, initial send, and normal send are bounded and timed out.
6. **Retention delay:** lowering alert retention enforces the new bound immediately.
7. **Settings recovery:** invalid persisted settings restore safe defaults and log a generic warning without echoing private content.
8. **Report injection risks:** HTML is escaped; CSV cells beginning with spreadsheet formula markers are neutralized; optional redaction removes sensitive values.
9. **SQLite backup consistency:** backups use SQLite's online backup API rather than copying a live WAL database file.
10. **Dependency advisories:** FastAPI, Starlette, pytest, and pytest-asyncio pins were upgraded; `pip-audit` reports no known vulnerabilities for `requirements.txt` at verification time.

## Boundary validation

- Supported bind: `127.0.0.1:8765` only.
- Allowed browser hosts: the configured loopback hostnames on port 8765.
- Allowed WebSocket origins: exact HTTP loopback dashboard origins on port 8765; credentials, paths, queries, and fragments are rejected.
- CSP permits only same-origin assets and the two IPv4/localhost WebSocket sources used by the supported launcher.
- Frames, objects, cameras, microphones, geolocation, payment, and USB access are blocked by headers.
- AI configuration accepts loopback Ollama only; AI is disabled by default and cannot invoke tools.
- No shell execution, packet capture, active blocking, remote scanning, CDN, analytics, or cloud API is present.

The review traced rejected Host/Origin inputs to middleware/WebSocket close paths before endpoint state mutation or socket acceptance. Positive and negative unit tests and live WebSocket probes verify the intended paths.

## Remaining risks

- The API has no user authentication. Same-user local processes are in the trust boundary and can call loopback endpoints directly.
- A user who manually starts Uvicorn with a non-loopback bind defeats the supported deployment model even though Host checks still reduce browser access. The launcher must remain the supported entry point.
- Host/Origin checks are browser boundary controls, not a defense against malware already executing as the user.
- Process/connection metadata can be sensitive. Local database and exported-report permissions inherit the user's filesystem protections.
- Heuristic rules can create false positives and false negatives. An alert is not proof of compromise.
- Dependency audit results are time-bound and should be rerun before releases.
- No independent penetration test, fuzzing campaign, or long-duration hostile soak was performed.

## Files requiring manual inspection

- `app/main.py` — middleware order, headers, bind assumptions.
- `app/api/websocket.py` — Host/Origin policy and close behavior.
- `app/service.py` — alert/persistence separation, queues, recovery, task lifecycle.
- `app/ai.py` — loopback validation and prompt/output boundaries.
- `app/reporting.py` — encoding, escaping, and redaction.
- `app/storage/database.py` — migrations, retention, backup, and dynamic SQL from model-controlled fields.
- `frontend/js/dashboard.js` and `frontend/js/websocket.js` — DOM safety, state mutation, reconnect behavior.
- `start.ps1` — interpreter selection, port check, server arguments, and child-process cleanup.
