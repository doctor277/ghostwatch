import { createDemoData } from "../frontend/js/demo.js";

const now = () => new Date().toISOString();

function historicalSamples() {
  const end = Date.now();
  return Array.from({ length: 24 }, (_, index) => ({
    timestamp: new Date(end - (23 - index) * 300000).toISOString(),
    sequence: index + 1,
    cpu_percent: index === 9 ? null : 18 + (index % 8),
    memory_percent: 42 + (index % 5),
    disk_percent: 61,
    upload_mbps: index === 9 ? null : 1.2 + index / 20,
    download_mbps: index === 9 ? null : 2.8 + index / 10,
    active_connections: 100 + index,
    established_connections: 70 + index,
    process_count: 190 + (index % 3),
    collector_status: index === 9 ? "degraded" : "healthy",
    unavailable_metrics: index === 9 ? ["cpu_percent", "upload_mbps", "download_mbps"] : [],
    resolution_seconds: 0,
    sample_count: 1,
  }));
}

export async function installSyntheticBackend(page) {
  const demo = createDemoData();
  const state = {
    snapshot: structuredClone(demo.snapshot),
    events: structuredClone(demo.events),
    incidents: structuredClone(demo.incidents),
    requests: [],
    settings: {
      refresh_interval: 2,
      cpu_threshold: 90,
      memory_threshold: 90,
      bandwidth_threshold_mbps: 3,
      connection_threshold: 100,
      threshold_duration: 10,
      alert_retention: 1000,
      event_logging_enabled: true,
    },
    detectionSettings: {
      enabled_rules: { cpu: true, memory: true, bandwidth: true, connections: true, connection_spike: true, process_count: true, disk_space: true, destination_diversity: true, collection_failure: true, storage_failure: true },
      cooldown_seconds: 60,
      process_count_threshold: 500,
      disk_free_threshold_percent: 10,
      destination_diversity_threshold: 100,
      connection_growth_threshold: 40,
    },
    aiSettings: { enabled: false, endpoint: "http://127.0.0.1:11434", model: "llama3.2", context_size: 4096, timeout_seconds: 20, redact_telemetry: true, redaction_level: "strict" },
    windowsSettings: { enabled: true, defender_enabled: true, system_enabled: true, application_enabled: true, poll_interval_seconds: 15, max_events_per_channel: 100, retention_hours: 168 },
    fileSettings: { enabled: false, scan_interval_seconds: 30, max_files_per_folder: 10000, max_file_size_bytes: 104857600, event_retention: 10000 },
    folders: [{ id: "synthetic-folder", path: "C:\\SYNTHETIC\\watched", enabled: true, created_at: now(), baseline_created_at: now(), status: "healthy", status_detail: "" }],
    sockets: [],
    failures: new Map(),
  };
  const samples = historicalSamples();
  const intelligence = {
    start: samples[0].timestamp,
    end: now(),
    samples,
    process_observations: state.snapshot.processes.slice(0, 3).map((item) => ({ timestamp: now(), sequence: 1, pid: item.pid, create_time: item.create_time, name: item.name, cpu_percent: item.cpu_percent, memory_percent: item.memory_percent, connection_count: item.connection_count, status: item.status, access_status: item.access_status })),
    connection_summaries: [{ timestamp: now(), sequence: 1, active_connections: 284, established_connections: 241, unique_remote_destinations: 73, new_connections: 3, disappeared_connections: 2, collection_status: "healthy", lifecycle_confidence: "high", top_processes: state.snapshot.connection_rankings }],
    gaps: [{ start: new Date(Date.now() - 3600000).toISOString(), end: new Date(Date.now() - 3300000).toISOString(), duration_seconds: 300 }],
    truncated: false,
  };
  const windowsEvents = [{ id: "windows:System:1", channel: "System", record_id: 1, timestamp: now(), provider: "Service Control Manager", event_id: 7031, level: "ERROR", category: "service_failure", summary: "[REDACTED_USER] service stopped unexpectedly", service_name: "Synthetic Service", correlation_tags: ["service", "failure"] }];
  const fileEvents = [{ id: "file-event-1", folder_id: "synthetic-folder", timestamp: now(), change_type: "modified", relative_path: "config\\policy.json", previous_sha256: "a".repeat(64), current_sha256: "b".repeat(64), size: 128 }];

  const json = (route, payload, status = 200, headers = {}) => route.fulfill({ status, contentType: "application/json", headers, body: JSON.stringify(payload) });
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const method = request.method();
    state.requests.push(`${method} ${path}`);
    const failureKey = `${method} ${path}`;
    if (state.failures.has(failureKey)) {
      const status = state.failures.get(failureKey);
      state.failures.delete(failureKey);
      return json(route, { detail: "Synthetic request failure" }, status);
    }
    if (path === "/api/overview") return json(route, state.snapshot);
    if (path === "/api/history") return json(route, { samples });
    if (path === "/api/history/intelligence") return json(route, intelligence);
    if (path === "/api/history/export") return route.fulfill({ status: 200, headers: { "Content-Type": "application/json", "Content-Disposition": "attachment; filename=synthetic-history.json" }, body: JSON.stringify(intelligence) });
    if (path === "/api/events") return json(route, state.events);
    if (path === "/api/incidents" && method === "GET") return json(route, state.incidents);
    if (path === "/api/incidents" && method === "POST") {
      const payload = request.postDataJSON();
      const incident = { id: "SYNTHETIC-NEW", title: payload.title, severity: payload.severity, detection_timestamp: now(), first_observed: now(), last_observed: now(), related_processes: [], related_connections: [], related_alerts: payload.alert_ids, evidence_summary: payload.evidence_summary, status: "NEW", analyst_notes: payload.analyst_notes };
      state.incidents.unshift(incident); return json(route, incident, 201);
    }
    if (path.startsWith("/api/incidents/") && method === "PATCH") {
      const item = state.incidents.find((incident) => path.endsWith(incident.id));
      Object.assign(item, request.postDataJSON(), { last_observed: now() }); return json(route, item);
    }
    if (path.startsWith("/api/alerts/") && method === "PATCH") {
      const item = state.snapshot.alerts.find((alert) => path.endsWith(alert.id));
      Object.assign(item, request.postDataJSON()); return json(route, item);
    }
    if (path === "/api/settings") {
      if (method === "PUT") state.settings = request.postDataJSON();
      return json(route, state.settings);
    }
    if (path === "/api/detection-settings") {
      if (method === "PUT") state.detectionSettings = request.postDataJSON();
      return json(route, state.detectionSettings);
    }
    if (path === "/api/ai/settings") {
      if (method === "PUT") state.aiSettings = request.postDataJSON();
      return json(route, state.aiSettings);
    }
    if (path === "/api/ai/analyze") return json(route, { observed_evidence: "Synthetic evidence only.", possible_explanations: "Expected test fixture.", missing_information: "No real host evidence.", recommended_checks: "Use live mode for local observations.", model: "synthetic" });
    if (path === "/api/windows-events") return json(route, windowsEvents);
    if (path === "/api/windows-events/summary") return json(route, { enabled: true, counts: { service_failure: 1 }, channels: [{ channel: "System", status: "healthy", detail: "", cursor: 1 }] });
    if (path === "/api/windows-events/settings") {
      if (method === "PUT") state.windowsSettings = request.postDataJSON();
      return json(route, state.windowsSettings);
    }
    if (path === "/api/file-integrity/settings") {
      if (method === "PUT") state.fileSettings = request.postDataJSON();
      return json(route, state.fileSettings);
    }
    if (path === "/api/file-integrity/folders" && method === "GET") return json(route, state.folders);
    if (path === "/api/file-integrity/folders" && method === "POST") {
      const folder = { id: "synthetic-added", path: request.postDataJSON().path, enabled: true, created_at: now(), baseline_created_at: now(), status: "healthy", status_detail: "" };
      state.folders.push(folder); return json(route, folder, 201);
    }
    if (path.startsWith("/api/file-integrity/folders/") && method === "PUT") {
      const folder = state.folders.find((item) => path.endsWith(item.id)); Object.assign(folder, request.postDataJSON()); return json(route, folder);
    }
    if (path.startsWith("/api/file-integrity/folders/") && method === "DELETE") {
      state.folders = state.folders.filter((item) => !path.endsWith(item.id)); return route.fulfill({ status: 204, body: "" });
    }
    if (path === "/api/file-integrity/events") return json(route, fileEvents);
    if (path === "/api/file-integrity/scan") return json(route, fileEvents);
    if (path.startsWith("/api/reports/")) return route.fulfill({ status: 200, headers: { "Content-Type": "application/json", "Content-Disposition": "attachment; filename=synthetic-report.json" }, body: JSON.stringify({ schema_version: "3.0", report_type: "synthetic" }) });
    return json(route, { detail: `Unhandled synthetic route ${method} ${path}` }, 500);
  });

  await page.routeWebSocket("**/ws/telemetry", (socket) => {
    state.sockets.push(socket);
    setTimeout(() => socket.send(JSON.stringify(state.snapshot)), 25);
  });

  return {
    state,
    disconnectLatest() {
      state.sockets.at(-1)?.close({ code: 1012, reason: "Synthetic restart" });
    },
    failNext(method, path, status = 503) {
      state.failures.set(`${method} ${path}`, status);
    },
  };
}
