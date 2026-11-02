export function createDemoData() {
  const now = new Date();
  const processNames = ["browser-demo.exe", "update-demo.exe", "editor-demo.exe", "service-demo.exe"];
  const processes = processNames.map((name, index) => ({
    pid: 4100 + index,
    name,
    parent_pid: index ? 4100 : 800,
    cpu_percent: 4 + index * 3.2,
    memory_percent: 2 + index * 1.7,
    memory_bytes: (180 + index * 95) * 1024 * 1024,
    status: "running",
    connection_count: 7 - index,
    executable: `C:\\DEMO\\${name}`,
    username: "DEMO\\operator",
    create_time: new Date(now - (index + 1) * 3600000).toISOString(),
    first_observed: new Date(now - 120000).toISOString(),
    last_observed: now.toISOString(),
    remote_endpoints: [`203.0.113.${10 + index}:443`],
    access_status: "available",
  }));
  const connections = Array.from({ length: 20 }, (_, index) => ({
    id: `demo-connection-${index}`,
    pid: processes[index % processes.length].pid,
    process_name: processes[index % processes.length].name,
    local_address: "192.0.2.10",
    local_port: 50000 + index,
    remote_address: `203.0.113.${20 + (index % 9)}`,
    remote_port: index % 3 ? 443 : 53,
    protocol: index % 3 ? "TCP" : "UDP",
    status: index % 3 ? "ESTABLISHED" : "NONE",
    first_seen: new Date(now - index * 12000).toISOString(),
    last_seen: now.toISOString(),
    duration_seconds: index * 12,
    address_scope: "external",
    is_new: index < 3,
  }));
  const alerts = [
    { id: "demo-alert-1", timestamp: now.toISOString(), severity: "WARNING", source: "Network", metric: "Synthetic outbound deviation", explanation: "Demo bandwidth is above its synthetic baseline. This is not a threat verdict.", acknowledged: false, dismissed: false, rule_id: "bandwidth", evidence: { observed_mbps: 8.4, baseline_mbps: .7 }, persistence_pending: false },
    { id: "demo-alert-2", timestamp: new Date(now - 180000).toISOString(), severity: "INFO", source: "Collector", metric: "Synthetic collection recovery", explanation: "A demo collector recovered without losing live display data.", acknowledged: true, dismissed: false, rule_id: "collection_failure", evidence: { unavailable_metrics: ["demo_metric"] }, persistence_pending: false, first_occurrence: new Date(now - 300000).toISOString(), last_occurrence: new Date(now - 240000).toISOString(), occurrence_count: 3, condition_active: false, last_recovery: new Date(now - 180000).toISOString() },
  ];
  const events = alerts.map((item, index) => ({ id: `demo-event-${index}`, timestamp: item.timestamp, event_type: "demo_alert", severity: item.severity === "INFO" ? "LOW" : "HIGH", title: item.metric, summary: item.explanation, related_id: item.id, evidence: item.evidence }));
  const incidents = [{ id: "DEMO-INCIDENT-001", title: "Synthetic traffic review", severity: "WARNING", detection_timestamp: now.toISOString(), first_observed: new Date(now - 300000).toISOString(), last_observed: now.toISOString(), related_processes: [4100], related_connections: ["demo-connection-1"], related_alerts: ["demo-alert-1"], evidence_summary: "Synthetic evidence for interface demonstration only.", status: "INVESTIGATING", analyst_notes: "Demo analyst note. Not a real incident." }];
  const rankings = processes.map((process) => ({ pid: process.pid, process_name: process.name, connection_count: process.connection_count, established_count: process.connection_count - 1, destination_count: process.connection_count }));
  return {
    snapshot: {
      type: "telemetry", sequence: 9999,
      system: { timestamp: now.toISOString(), cpu_percent: 27.4, memory_percent: 48.2, memory_used: 16_000_000_000, memory_total: 32_000_000_000, memory_available: 16_000_000_000, memory_cached: 4_000_000_000, commit_used: 20_000_000_000, commit_limit: 48_000_000_000, commit_percent: 41.7, pagefile_used: 1_000_000_000, pagefile_total: 8_000_000_000, pagefile_percent: 12.5, process_count: 214, uptime_seconds: 190000, disk_percent: 63.1, collection_error: null, unavailable_metrics: [] },
      network: { bytes_sent_per_second: 8_800_000, bytes_recv_per_second: 14_200_000, upload_mbps: 8.39, download_mbps: 13.54, active_connections: 284, established_connections: 241, unique_remote_destinations: 73, new_connections: 3, disappeared_connections: 2, collection_error: null, unavailable_metrics: [] },
      connections, processes, alerts, connection_rankings: rankings, recently_closed_connections: connections.slice(0, 3).map((item) => ({ ...item, id: `closed-${item.id}`, status: "CLOSED" })),
      baselines: [{ metric: "bandwidth_mbps", current: 21.93, mean: 6.4, standard_deviation: 2.1, percentile_95: 9.2, deviation_ratio: 2.43, level: "HIGH", sample_count: 120, explanation: "Synthetic combined bandwidth is above its demo rolling average." }],
      storage: { status: "healthy", persistence_available: true, pending_writes: 0, last_success: now.toISOString(), last_error_type: null, schema_version: 2 },
      collection_duration_ms: 31.6, process_collection_status: "healthy", process_collection_detail: "", processes_enumerated: 214, processes_displayed: processes.length, process_records_skipped: 0, idle_processes_excluded: 1,
    },
    events,
    incidents,
  };
}
