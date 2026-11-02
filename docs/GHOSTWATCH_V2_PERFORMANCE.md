# Ghostwatch V2 Performance Evidence

These measurements are development-host observations, not universal guarantees. They were taken on Windows with the live dashboard server and real local telemetry.

## Collection strategy

- System/network collection is periodic and failure-isolated.
- Connection totals inspect the full list once; presentation retains at most 250 detail rows.
- Process collection uses a cache and rotating enrichment budget (8 metric refreshes and 1 identity refresh by default, with a 10-second enrichment interval).
- Frontend history is bounded to 300 samples.
- Graph rendering is bounded to 60 process nodes, 180 remote nodes, and 240 edges.
- Live alerts are bounded to 200 and WebSocket clients to 32.
- Each WebSocket client has a bounded latest-snapshot queue; slow sends time out after 2 seconds.

## Observed live probes

Three representative probes:

| Probe | Idle server CPU over 3s | Working set | Collection duration | API median | API p95 | Overview | Initial WebSocket |
|---|---:|---:|---:|---:|---:|---:|---:|
| 1 | 1.6% | 71,041,024 B | 67.54 ms | 15.714 ms | 29.758 ms | 25.875 ms | 106.993 ms |
| 2 | 2.1% | 71,168,000 B | 71.92 ms | 5.490 ms | 26.130 ms | 16.084 ms | 109.699 ms |
| 3 | 3.6% | 71,696,384 B | 71.41 ms | 5.288 ms | 30.606 ms | 15.642 ms | 119.266 ms |

SQLite database size during these probes: 65,536 bytes.

A 15-second/30-read collection-duration window observed minimum 40.12 ms, median 66.96 ms, and maximum 366.54 ms. The higher periodic samples correspond to rotating process enrichment. Average idle CPU remained low in the three probes above.

## Performance risks

- `psutil.net_connections()` cost depends on host connection volume and Windows permissions.
- Very high process counts increase the time needed for a complete rotation through cached enrichment.
- Canvas graph bounds deliberately trade completeness of visualization for responsiveness; aggregate counts remain complete.
- No multi-hour memory soak or high-scale 32-client concurrency soak was performed. Queue/client behavior is regression-tested with controlled doubles and representative live connections.
