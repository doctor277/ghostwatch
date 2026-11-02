# Ghostwatch V2 Known Limitations

- Windows executable publisher/Authenticode signature verification is not implemented.
- Reliable per-process network byte attribution is not available from the selected `psutil` data source; connection ownership is shown where Windows supplies a PID.
- Process CPU/memory history is not durably stored; current metrics and bounded session context are emphasized.
- Baselines are in-memory and reset when the service restarts. There is no durable day-over-day or week-over-week baseline model.
- Closed-connection lifecycle history is session-bounded rather than a complete durable audit log.
- Network search and filters are implemented, but the connection-detail table does not yet offer every requested clickable column sort or dedicated application/state selector.
- The timeline is searchable and the API supports type/severity filtering, but the UI has no zoomable/custom-range visualization or related-record deep-link workflow.
- Detection covers sustained CPU, memory, upload, connection count, process count, low disk, destination diversity, rapid connection growth, and collection failure. It does not implement every proposed memory-growth, process-creation-burst, short-lived-connection, or failure-rate model.
- Reports support eight report families and JSON/CSV/HTML. PDF generation, embedded charts, and arbitrary date pickers are not implemented.
- Optional GeoIP enrichment is not implemented; this avoids adding a database/download and preserves the no-network default.
- There is no tray icon, native desktop notification, start-on-login installer, signed executable, or packaged Windows service.
- Browser QA was automated interactively, but a checked-in Playwright/Selenium end-to-end suite is not present.
- Accessibility smoke checks and Lighthouse scored cleanly, but no formal WCAG audit with assistive-technology users was performed.
- Maximum-client and slow-client behavior is unit-tested; no hostile high-scale or multi-hour soak was run.
- Python 3.14.3 was used for final verification. The project declares Python >= 3.11, but the full matrix was not executed.
- The test suite emits one upstream `StarletteDeprecationWarning` advising migration from `httpx` to `httpx2` in the test client. It does not affect the production HTTP server path.
- Alerts are heuristic. Ghostwatch is not an antivirus, EDR replacement, packet analyzer, or proof of compromise.
