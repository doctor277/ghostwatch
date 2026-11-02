import { colors, drawBandwidthChart, drawConnectionHistory, drawSparkline, drawSystemChart } from "./charts.js";
import { createDemoData } from "./demo.js";
import { NetworkGraph } from "./network-graph.js";
import { TelemetrySocket } from "./websocket.js";

const state = {
  snapshot: null,
  history: [],
  historical: null,
  windowsEvents: [],
  windowsEventSettings: null,
  fileIntegritySettings: null,
  monitoredFolders: [],
  fileIntegrityEvents: [],
  events: [],
  incidents: [],
  processSort: { key: "cpu_percent", direction: -1 },
  alertSeverity: "ALL",
  selectedAlerts: new Set(),
  selectedIncident: null,
  settings: null,
  detectionSettings: null,
  aiSettings: null,
  selectedReport: null,
  demo: false,
  socket: null,
};

const $ = (selector, parent = document) => parent.querySelector(selector);
const $$ = (selector, parent = document) => [...parent.querySelectorAll(selector)];
const text = (selector, value) => { const node = $(selector); if (node) node.textContent = String(value); };
let lastDrawerTrigger = null;
let lastDrawerPid = null;

function element(tag, className, content) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (content !== undefined) node.textContent = String(content);
  return node;
}

function bytes(value) {
  if (!Number.isFinite(value)) return "UNAVAILABLE";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let index = 0;
  let current = Math.max(0, value);
  while (current >= 1024 && index < units.length - 1) { current /= 1024; index += 1; }
  return `${current.toFixed(index > 1 ? 1 : 0)} ${units[index]}`;
}

const rate = (value) => Number.isFinite(value) ? `${bytes(value)}/s` : "UNAVAILABLE";
const duration = (value) => Number.isFinite(value) ? `${Math.floor(value / 60)}m ${Math.floor(value % 60)}s` : "—";
function uptime(value) {
  if (!Number.isFinite(value)) return "UNAVAILABLE";
  const seconds = Math.max(0, Math.floor(value));
  return `${Math.floor(seconds / 86400)}d ${Math.floor((seconds % 86400) / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`;
}

function isAvailable(sample, key) {
  return !((sample?.unavailable_metrics || []).includes(key));
}

function showToast(message) {
  const toast = $("#toast");
  toast.textContent = message;
  toast.classList.add("show");
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => toast.classList.remove("show"), 2800);
}

function debounce(callback, delay = 150) {
  let timer;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => callback(...args), delay);
  };
}

function switchView(name) {
  const drawer = $("#process-drawer");
  if (drawer.classList.contains("open")) {
    drawer.classList.remove("open");
    drawer.setAttribute("aria-hidden", "true");
    drawer.setAttribute("inert", "");
  }
  $$(".view").forEach((node) => node.classList.toggle("active", node.dataset.viewPanel === name));
  $$(".nav-item").forEach((node) => node.classList.toggle("active", node.dataset.view === name));
  $("#sidebar").classList.remove("open");
  $("#mobile-menu").setAttribute("aria-expanded", "false");
  history.replaceState(null, "", `#${name}`);
  if (name === "history") loadEvents();
  if (name === "intelligence") loadHistoricalIntelligence();
  if (name === "windows-events") loadWindowsEvents();
  if (name === "file-integrity") loadFileIntegrity();
  if (name === "incidents") loadIncidents();
  requestAnimationFrame(() => {
    drawAllCharts();
    if (name === "graph") graph.draw();
  });
}

function makeCell(row, value, className = "") {
  const cell = row.insertCell();
  cell.textContent = value ?? "—";
  if (className) cell.className = className;
  return cell;
}

function emptyTable(body, columns, message) {
  const row = body.insertRow();
  const cell = row.insertCell();
  cell.colSpan = columns;
  cell.textContent = message;
}

function filteredConnections() {
  if (!state.snapshot) return [];
  const query = $("#network-search").value.trim().toLowerCase();
  const scope = $("#network-scope").value;
  return state.snapshot.connections.filter((item) =>
    (scope === "ALL" || item.address_scope === scope)
    && (!query || [item.process_name, item.local_address, item.remote_address, item.status, item.pid]
      .some((value) => String(value ?? "").toLowerCase().includes(query)))
  );
}

function renderConnections() {
  const body = $("#network-table");
  body.replaceChildren();
  const connections = filteredConnections();
  if (!connections.length) {
    emptyTable(body, 8, "No accessible connections match this view");
  } else {
    connections.forEach((item) => {
      const row = body.insertRow();
      if (item.is_new) row.classList.add("new-row");
      makeCell(row, item.pid ?? "—");
      makeCell(row, item.process_name);
      makeCell(row, `${item.local_address}:${item.local_port ?? "—"}`);
      makeCell(row, `${item.remote_address}:${item.remote_port ?? "—"}`);
      makeCell(row, item.protocol);
      makeCell(row, item.status, item.status === "ESTABLISHED" ? "positive" : "");
      makeCell(row, item.address_scope);
      makeCell(row, duration(item.duration_seconds));
    });
  }
  const closed = $("#closed-table");
  closed.replaceChildren();
  const recent = state.snapshot?.recently_closed_connections || [];
  if (!recent.length) emptyTable(closed, 6, "No connection closures observed in this session");
  recent.forEach((item) => {
    const row = closed.insertRow();
    makeCell(row, item.process_name);
    makeCell(row, `${item.local_address}:${item.local_port ?? "—"}`);
    makeCell(row, `${item.remote_address}:${item.remote_port ?? "—"}`);
    makeCell(row, item.protocol);
    makeCell(row, item.last_seen ? new Date(item.last_seen).toLocaleTimeString() : "—");
    makeCell(row, duration(item.duration_seconds));
  });
}

function renderRanks(rankings) {
  const container = $("#connections-by-process");
  container.replaceChildren();
  const rows = (rankings || []).slice(0, 8);
  const max = rows[0]?.connection_count || 1;
  rows.forEach((item) => {
    const row = element("div", "rank-row");
    const label = element("span", "", item.process_name);
    label.title = `PID ${item.pid ?? "unavailable"} · ${item.destination_count} destinations`;
    const metric = element("span");
    const bar = element("div");
    const fill = element("i");
    fill.style.width = `${(item.connection_count / max) * 100}%`;
    bar.append(fill);
    metric.append(bar, element("b", "", item.connection_count));
    row.append(label, metric);
    container.append(row);
  });
  if (!rows.length) container.append(element("div", "empty-state", "No accessible ownership aggregates"));
}

function filteredProcesses() {
  if (!state.snapshot) return [];
  const query = $("#process-search").value.trim().toLowerCase();
  const items = state.snapshot.processes.filter((item) =>
    !query || [item.name, item.pid, item.executable, item.username]
      .some((value) => String(value ?? "").toLowerCase().includes(query))
  );
  const { key, direction } = state.processSort;
  return [...items].sort((a, b) => {
    const left = a[key] ?? ""; const right = b[key] ?? "";
    if (typeof left === "string") return left.localeCompare(String(right)) * direction;
    return (Number(left) - Number(right)) * direction;
  });
}

function renderProcessTable() {
  const body = $("#process-table");
  const focusedPid = document.activeElement?.closest?.("#process-table tr")?.dataset.pid;
  body.replaceChildren();
  const processes = filteredProcesses();
  text("#process-count-label", `${processes.length} VISIBLE`);
  if (!processes.length) {
    emptyTable(body, 8, "No matching accessible processes");
    return;
  }
  processes.forEach((process) => {
    const row = body.insertRow();
    row.className = "clickable";
    row.tabIndex = 0;
    row.dataset.pid = String(process.pid);
    if (process.cpu_percent >= 20 || process.memory_percent >= 10) row.classList.add("warning-row");
    makeCell(row, process.name);
    makeCell(row, process.pid);
    makeCell(row, process.parent_pid ?? "—");
    makeCell(row, `${process.cpu_percent.toFixed(1)}%`, process.cpu_percent >= 20 ? "positive" : "");
    makeCell(row, `${process.memory_percent.toFixed(2)}% · ${bytes(process.memory_bytes)}`);
    makeCell(row, process.status);
    makeCell(row, process.connection_count);
    makeCell(row, process.access_status);
    row.addEventListener("click", () => openProcess(process, row));
    row.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        openProcess(process, row);
      }
    });
  });
  if (focusedPid) body.querySelector(`tr[data-pid="${CSS.escape(focusedPid)}"]`)?.focus();
}

function openProcess(process, trigger) {
  lastDrawerTrigger = trigger;
  lastDrawerPid = String(process.pid);
  text("#drawer-name", process.name);
  text("#drawer-status", `${process.status.toUpperCase()} · ${process.access_status.toUpperCase()} ACCESS`);
  const details = $("#drawer-details");
  details.replaceChildren();
  [
    ["PID", process.pid],
    ["Parent PID", process.parent_pid ?? "Unavailable"],
    ["CPU", `${process.cpu_percent.toFixed(1)}%`],
    ["Memory", `${process.memory_percent.toFixed(2)}% (${bytes(process.memory_bytes)})`],
    ["Connections", process.connection_count],
    ["Executable", process.executable || "Unavailable"],
    ["Username", process.username || "Unavailable"],
    ["Created", process.create_time ? new Date(process.create_time).toLocaleString() : "Unavailable"],
    ["First observed", process.first_observed ? new Date(process.first_observed).toLocaleString() : "Unavailable"],
    ["Last observed", process.last_observed ? new Date(process.last_observed).toLocaleString() : "Unavailable"],
  ].forEach(([label, value]) => {
    const row = element("div");
    row.append(element("dt", "", label), element("dd", "", value));
    details.append(row);
  });
  const endpoints = $("#drawer-endpoints");
  endpoints.replaceChildren();
  (process.remote_endpoints || []).forEach((endpoint) => endpoints.append(element("li", "", endpoint)));
  if (!endpoints.children.length) endpoints.append(element("li", "", "No remote endpoint available in bounded detail."));
  const drawer = $("#process-drawer");
  drawer.classList.add("open");
  drawer.setAttribute("aria-hidden", "false");
  drawer.removeAttribute("inert");
  $("#drawer-close").focus();
}

function renderTree() {
  const container = $("#process-tree");
  container.replaceChildren();
  const processes = state.snapshot?.processes || [];
  const query = $("#tree-search").value.trim().toLowerCase();
  const byPid = new Map(processes.map((item) => [item.pid, item]));
  const children = new Map();
  processes.forEach((item) => {
    if (!children.has(item.parent_pid)) children.set(item.parent_pid, []);
    children.get(item.parent_pid).push(item);
  });
  const roots = processes.filter((item) => !byPid.has(item.parent_pid));
  const visited = new Set();
  function appendNode(process, parent, depth) {
    if (visited.has(process.pid) || depth > 20) return;
    visited.add(process.pid);
    const descendants = children.get(process.pid) || [];
    const matches = !query || process.name.toLowerCase().includes(query) || String(process.pid).includes(query);
    const wrapper = element("div", "tree-node");
    wrapper.setAttribute("role", "treeitem");
    wrapper.setAttribute("aria-expanded", String(descendants.length > 0));
    const row = element("button", "tree-row");
    row.type = "button";
    row.append(
      element("span", "tree-toggle", descendants.length ? "▾" : "·"),
      element("strong", "", `${process.name} · ${process.pid}`),
      element("small", "", `CPU ${process.cpu_percent.toFixed(1)}%`),
      element("small", "", `RAM ${process.memory_percent.toFixed(1)}%`),
      element("small", "", `${process.connection_count} conn.`),
    );
    if (matches) row.classList.add("tree-match");
    const branch = element("div", "tree-children");
    descendants.forEach((child) => appendNode(child, branch, depth + 1));
    row.addEventListener("click", (event) => {
      if (event.target.classList.contains("tree-toggle") && descendants.length) {
        const collapsed = branch.classList.toggle("collapsed");
        wrapper.setAttribute("aria-expanded", String(!collapsed));
        event.target.textContent = collapsed ? "▸" : "▾";
      } else {
        openProcess(process, row);
      }
    });
    wrapper.append(row, branch);
    parent.append(wrapper);
  }
  roots.slice(0, 200).forEach((root) => appendNode(root, container, 0));
  processes.filter((item) => !visited.has(item.pid)).slice(0, 100).forEach((item) => appendNode(item, container, 0));
  if (!container.children.length) container.append(element("div", "empty-state", "No accessible process relationships"));
}

function renderSignals(alerts) {
  const active = (alerts || []).filter((item) => !item.dismissed && item.condition_active !== false);
  const container = $("#overview-alerts");
  container.replaceChildren();
  container.classList.toggle("empty-state", active.length === 0);
  const count = active.filter((item) => !item.acknowledged).length;
  text("#nav-alert-count", count);
  $("#nav-alert-count").dataset.zero = String(count === 0);
  if (!active.length) {
    container.textContent = "No active signals";
    return;
  }
  active.slice(0, 5).forEach((alert) => {
    const row = element("div", "signal-item");
    row.append(element("span", `severity ${alert.severity}`, alert.severity));
    const copy = element("div", "signal-copy");
    copy.append(element("strong", "", alert.metric), element("small", "", alert.explanation));
    row.append(copy, element("time", "signal-time", new Date(alert.timestamp).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })));
    container.append(row);
  });
}

function renderAlerts() {
  const alerts = state.snapshot?.alerts || [];
  const visible = alerts.filter((item) => !item.dismissed && (state.alertSeverity === "ALL" || item.severity === state.alertSeverity));
  const container = $("#alert-list");
  container.replaceChildren();
  container.classList.toggle("empty-state", visible.length === 0);
  if (!visible.length) {
    container.textContent = "No alerts in this view";
    return;
  }
  visible.forEach((alert) => {
    const card = element("article", "alert-card");
    card.dataset.level = alert.severity;
    const selectLabel = element("label", "alert-select");
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = state.selectedAlerts.has(alert.id);
    checkbox.setAttribute("aria-label", `Select alert ${alert.metric}`);
    checkbox.addEventListener("change", () => {
      checkbox.checked ? state.selectedAlerts.add(alert.id) : state.selectedAlerts.delete(alert.id);
    });
    selectLabel.append(checkbox, element("span", `severity ${alert.severity}`, alert.severity));
    const identity = element("div");
    identity.append(element("h3", "", alert.metric), element("time", "", new Date(alert.timestamp).toLocaleString()));
    const copy = element("div");
    copy.append(element("p", "", alert.explanation));
    copy.append(element("div", "evidence", `Rule: ${alert.rule_id || "legacy"} · Evidence: ${JSON.stringify(alert.evidence || {})}`));
    if (alert.rule_id === "collection_failure") {
      const first = alert.first_occurrence ? new Date(alert.first_occurrence).toLocaleString() : "unknown";
      const last = alert.last_occurrence ? new Date(alert.last_occurrence).toLocaleString() : "unknown";
      const recovery = alert.last_recovery ? new Date(alert.last_recovery).toLocaleString() : "none observed";
      copy.append(element("div", "evidence", `STATE: ${alert.condition_active === false ? "RECOVERED" : "ACTIVE"} · First: ${first} · Last: ${last} · Occurrences: ${alert.occurrence_count || 0} · Last recovery: ${recovery}`));
    }
    if (alert.persistence_pending) copy.append(element("div", "evidence", "PERSISTENCE PENDING · live memory state"));
    const actions = element("div", "alert-actions");
    if (!alert.acknowledged) {
      const acknowledge = element("button", "mini-button", "ACKNOWLEDGE");
      acknowledge.addEventListener("click", () => updateAlert(alert.id, { acknowledged: true }));
      actions.append(acknowledge);
    } else {
      actions.append(element("span", "panel-counter", "ACKNOWLEDGED"));
    }
    const dismiss = element("button", "mini-button", "DISMISS");
    dismiss.addEventListener("click", () => updateAlert(alert.id, { dismissed: true }));
    actions.append(dismiss);
    card.append(selectLabel, identity, copy, actions);
    container.append(card);
  });
}

async function updateAlert(id, patch) {
  if (state.demo) {
    const index = state.snapshot.alerts.findIndex((item) => item.id === id);
    if (index >= 0) state.snapshot.alerts[index] = { ...state.snapshot.alerts[index], ...patch };
    renderSignals(state.snapshot.alerts);
    renderAlerts();
    showToast("Demo alert state changed in memory only");
    return;
  }
  const response = await fetch(`/api/alerts/${encodeURIComponent(id)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
  if (!response.ok) {
    showToast("Alert update failed");
    return;
  }
  const index = state.snapshot.alerts.findIndex((item) => item.id === id);
  if (index >= 0) state.snapshot.alerts[index] = await response.json();
  renderSignals(state.snapshot.alerts);
  renderAlerts();
  showToast(patch.dismissed ? "Alert dismissed" : "Alert acknowledged");
}

function renderBaselines(items) {
  const container = $("#baseline-list");
  container.replaceChildren();
  if (!items?.length) {
    container.className = "rank-list empty-state";
    container.textContent = "Collecting baseline samples…";
    return;
  }
  container.className = "rank-list";
  items.slice(0, 6).forEach((item) => {
    const row = element("div", "rank-row");
    row.append(element("span", "", item.explanation), element("b", `severity ${item.level === "HIGH" ? "CRITICAL" : item.level === "MODERATE" ? "WARNING" : "INFO"}`, item.level));
    container.append(row);
  });
}

function renderOverviewEvents() {
  const body = $("#overview-events");
  body.replaceChildren();
  state.events.slice(0, 6).forEach((event) => {
    const row = body.insertRow();
    makeCell(row, new Date(event.timestamp).toLocaleTimeString());
    makeCell(row, event.severity, event.severity === "LOW" ? "positive" : "");
    makeCell(row, event.title);
  });
  if (!state.events.length) emptyTable(body, 3, "No retained events available");
}

function renderSnapshot(snapshot) {
  if (!snapshot || snapshot.type !== "telemetry") return;
  state.snapshot = snapshot;
  const system = snapshot.system;
  const network = snapshot.network;
  state.history.push({
    timestamp: system.timestamp,
    cpu_percent: isAvailable(system, "cpu_percent") ? system.cpu_percent : null,
    memory_percent: isAvailable(system, "memory_percent") ? system.memory_percent : null,
    upload_mbps: isAvailable(network, "upload_mbps") ? network.upload_mbps : null,
    download_mbps: isAvailable(network, "download_mbps") ? network.download_mbps : null,
    connections: isAvailable(network, "active_connections") ? network.active_connections : null,
    process_count: isAvailable(system, "process_count") ? system.process_count : null,
  });
  state.history = state.history.slice(-300);

  text("#metric-cpu", isAvailable(system, "cpu_percent") ? `${system.cpu_percent.toFixed(1)}%` : "UNAVAILABLE");
  text("#metric-memory", isAvailable(system, "memory_percent") ? `${system.memory_percent.toFixed(1)}%` : "UNAVAILABLE");
  text("#metric-download", isAvailable(network, "bytes_recv_per_second") ? rate(network.bytes_recv_per_second) : "UNAVAILABLE");
  text("#metric-upload", isAvailable(network, "bytes_sent_per_second") ? rate(network.bytes_sent_per_second) : "UNAVAILABLE");
  text("#metric-connections", isAvailable(network, "active_connections") ? network.active_connections : "UNAVAILABLE");
  text("#metric-established", isAvailable(network, "established_connections") ? `${network.established_connections} established` : "Established count unavailable");
  text("#metric-processes", isAvailable(system, "process_count") ? system.process_count : "UNAVAILABLE");
  text("#metric-duration", `${snapshot.collection_duration_ms.toFixed(1)} ms`);
  text("#metric-uptime", `Uptime ${isAvailable(system, "uptime_seconds") ? uptime(system.uptime_seconds) : "unavailable"}`);
  text("#metric-memory-note", isAvailable(system, "memory_total") ? `${bytes(system.memory_used)} used · ${system.memory_available == null ? "available unknown" : `${bytes(system.memory_available)} available`} · ${bytes(system.memory_total)} total` : "Memory values unavailable");
  $("#memory-meter").style.width = isAvailable(system, "memory_percent") ? `${system.memory_percent}%` : "0%";
  text("#legend-download", isAvailable(network, "bytes_recv_per_second") ? rate(network.bytes_recv_per_second) : "—");
  text("#legend-upload", isAvailable(network, "bytes_sent_per_second") ? rate(network.bytes_sent_per_second) : "—");
  text("#network-total", isAvailable(network, "active_connections") ? network.active_connections : "—");
  text("#network-established", isAvailable(network, "established_connections") ? network.established_connections : "—");
  text("#network-destinations", isAvailable(network, "unique_remote_destinations") ? network.unique_remote_destinations : "—");
  text("#network-new", network.new_connections ?? "—");
  text("#network-closed", network.disappeared_connections ?? "—");
  text("#network-permission-note", network.collection_error || system.collection_error || "Metadata only; process ownership can be unavailable without elevated access.");
  const metrics = [["cpu", system.cpu_percent, isAvailable(system, "cpu_percent")], ["memory", system.memory_percent, isAvailable(system, "memory_percent")], ["disk", system.disk_percent, isAvailable(system, "disk_percent") && system.disk_percent !== null]];
  metrics.forEach(([name, value, available]) => {
    $(`#bar-${name}`).style.width = available ? `${Math.min(100, value)}%` : "0%";
    text(`#bar-${name}-label`, available ? `${value.toFixed(1)}%` : "UNAVAILABLE");
  });
  text("#gauge-cpu-value", isAvailable(system, "cpu_percent") ? `${system.cpu_percent.toFixed(1)}%` : "—");
  $("#cpu-gauge").style.setProperty("--value", isAvailable(system, "cpu_percent") ? system.cpu_percent : 0);
  text("#gauge-memory-value", isAvailable(system, "memory_percent") ? `${system.memory_percent.toFixed(1)}%` : "—");
  $("#memory-gauge").style.setProperty("--value", isAvailable(system, "memory_percent") ? system.memory_percent : 0);
  text("#summary-uptime", isAvailable(system, "uptime_seconds") ? uptime(system.uptime_seconds) : "Unavailable");
  text("#summary-processes", isAvailable(system, "process_count") ? `${system.process_count} OS · ${snapshot.processes_displayed} detailed` : "Unavailable");
  text("#summary-disk", system.disk_percent === null ? "Unavailable" : `${system.disk_percent.toFixed(1)}%`);
  text("#summary-sockets", isAvailable(network, "active_connections") ? network.active_connections : "Unavailable");
  const processCollectionStatus = snapshot.process_collection_status || "unknown";
  text("#summary-collection", `${snapshot.collection_duration_ms.toFixed(1)} ms · ${processCollectionStatus}`);
  text("#summary-memory-available", system.memory_available == null ? "Unavailable" : bytes(system.memory_available));
  text("#summary-memory-commit", system.commit_used == null || system.commit_limit == null || system.commit_percent == null ? "Unavailable" : `${bytes(system.commit_used)} / ${bytes(system.commit_limit)} (${system.commit_percent.toFixed(1)}%)`);
  text("#summary-memory-cache", system.memory_cached == null ? "Unavailable" : bytes(system.memory_cached));
  text("#summary-pagefile", system.pagefile_used == null || system.pagefile_total == null || system.pagefile_percent == null ? "Unavailable" : `${bytes(system.pagefile_used)} / ${bytes(system.pagefile_total)} (${system.pagefile_percent.toFixed(1)}%)`);
  text("#summary-process-visibility", `${processCollectionStatus} · enumerated ${snapshot.processes_enumerated ?? snapshot.processes.length}, displayed ${snapshot.processes_displayed ?? snapshot.processes.length}, skipped ${snapshot.process_records_skipped ?? 0}, idle excluded ${snapshot.idle_processes_excluded ?? 0}${snapshot.process_collection_detail ? ` · ${snapshot.process_collection_detail}` : ""}`);
  text("#last-update", `UPDATED ${new Date(system.timestamp).toLocaleTimeString()}`);
  const unavailable = [...new Set([...(system.unavailable_metrics || []), ...(network.unavailable_metrics || [])])];
  text("#availability-note", unavailable.length ? `Unavailable: ${unavailable.join(", ")}` : "All primary metrics available.");
  const storage = snapshot.storage || {};
  text("#storage-status", `STORAGE ${String(storage.status || "unknown").toUpperCase()}`);
  $("#storage-status").className = storage.persistence_available ? "status-good" : "status-warning";

  renderRanks(snapshot.connection_rankings);
  renderConnections();
  renderProcessTable();
  renderTree();
  renderSignals(snapshot.alerts);
  renderAlerts();
  renderBaselines(snapshot.baselines);
  graph.setConnections(snapshot.connections);
  text("#graph-count", `${graph.nodes.length} NODES · ${graph.edges.length} EDGES`);
  text("#graph-summary", `Graph contains ${graph.nodes.length} bounded nodes and ${graph.edges.length} relationships.`);
  drawAllCharts();
}

function drawAllCharts() {
  const values = (key) => state.history.map((item) => Number.isFinite(item[key]) ? item[key] : 0);
  drawSparkline($("#spark-cpu"), values("cpu_percent"), colors.green);
  drawSparkline($("#spark-download"), values("download_mbps"), colors.cyan);
  drawSparkline($("#spark-upload"), values("upload_mbps"), colors.purple);
  drawSparkline($("#spark-connections"), values("connections"), colors.green);
  drawSparkline($("#spark-processes"), values("process_count"), colors.green);
  drawSparkline($("#spark-uptime"), state.history.map((_, index) => index + 1), colors.green);
  drawBandwidthChart($("#network-chart"), state.history);
  drawBandwidthChart($("#network-chart-detail"), state.history);
  drawSystemChart($("#system-chart"), state.history);
  if (state.historical) {
    drawSystemChart($("#historical-system-chart"), state.historical.samples);
    drawBandwidthChart($("#historical-network-chart"), state.historical.samples);
    drawConnectionHistory($("#historical-connection-chart"), state.historical.samples);
  }
  const latest = state.history.at(-1);
  $("#network-chart").setAttribute("aria-label", latest ? `Bandwidth chart. Latest download ${latest.download_mbps ?? "unavailable"} MB per second and upload ${latest.upload_mbps ?? "unavailable"} MB per second.` : "No bandwidth data available.");
}

function historicalWindow() {
  const end = new Date();
  const hours = Number($("#intelligence-range").value);
  return { start: new Date(end.getTime() - hours * 3600000), end };
}

function renderHistoricalIntelligence() {
  const result = state.historical;
  const processBody = $("#historical-process-table");
  const connectionBody = $("#historical-connection-table");
  processBody.replaceChildren();
  connectionBody.replaceChildren();
  if (!result) {
    emptyTable(processBody, 7, state.demo ? "Historical storage is disabled in demo mode" : "No historical query loaded");
    emptyTable(connectionBody, 7, state.demo ? "Historical storage is disabled in demo mode" : "No historical query loaded");
    text("#intelligence-status", state.demo ? "Demo data is isolated and never persisted." : "Historical data is unavailable.");
    text("#history-gaps", "No retained range is available.");
    drawSystemChart($("#historical-system-chart"), []);
    drawBandwidthChart($("#historical-network-chart"), []);
    drawConnectionHistory($("#historical-connection-chart"), []);
    return;
  }
  result.process_observations.slice(-250).reverse().forEach((item) => {
    const row = processBody.insertRow();
    makeCell(row, new Date(item.timestamp).toLocaleString());
    makeCell(row, item.name);
    makeCell(row, item.pid);
    makeCell(row, Number.isFinite(item.cpu_percent) ? `${item.cpu_percent.toFixed(1)}%` : "Unavailable");
    makeCell(row, Number.isFinite(item.memory_percent) ? `${item.memory_percent.toFixed(1)}%` : "Unavailable");
    makeCell(row, item.connection_count ?? "Unavailable");
    makeCell(row, item.access_status);
  });
  result.connection_summaries.slice(-250).reverse().forEach((item) => {
    const row = connectionBody.insertRow();
    makeCell(row, new Date(item.timestamp).toLocaleString());
    makeCell(row, item.active_connections ?? "Unavailable");
    makeCell(row, item.established_connections ?? "Unavailable");
    makeCell(row, item.unique_remote_destinations ?? "Unavailable");
    makeCell(row, item.new_connections ?? "Unavailable");
    makeCell(row, item.disappeared_connections ?? "Unavailable");
    makeCell(row, item.lifecycle_confidence);
  });
  if (!result.process_observations.length) emptyTable(processBody, 7, "No retained process observations in this range");
  if (!result.connection_summaries.length) emptyTable(connectionBody, 7, "No retained connection summaries in this range");
  const rollups = result.samples.filter((item) => item.resolution_seconds > 0).length;
  text("#intelligence-status", `${result.samples.length} telemetry points · ${result.process_observations.length} process observations · ${rollups} compacted points${result.truncated ? " · result limited" : ""}`);
  if (result.gaps.length) {
    const missingSeconds = result.gaps.reduce((total, gap) => total + gap.duration_seconds, 0);
    text("#history-gaps", `${result.gaps.length} missing-data interval(s), totaling ${duration(missingSeconds)}. Charts leave unavailable values as visible gaps.`);
  } else {
    text("#history-gaps", "No missing-data intervals were detected in the selected retained range.");
  }
  drawSystemChart($("#historical-system-chart"), result.samples);
  drawBandwidthChart($("#historical-network-chart"), result.samples);
  drawConnectionHistory($("#historical-connection-chart"), result.samples);
}

async function loadHistoricalIntelligence() {
  if (state.demo) {
    state.historical = null;
    renderHistoricalIntelligence();
    return;
  }
  const { start, end } = historicalWindow();
  const params = new URLSearchParams({ start: start.toISOString(), end: end.toISOString(), limit: "10000" });
  try {
    const response = await fetch(`/api/history/intelligence?${params}`);
    if (!response.ok) throw new Error("history unavailable");
    state.historical = await response.json();
  } catch {
    state.historical = null;
    showToast("Historical intelligence is currently unavailable");
  }
  renderHistoricalIntelligence();
}

function exportHistorical(format) {
  if (state.demo) {
    showToast("Demo observations are never exported as retained history");
    return;
  }
  const { start, end } = historicalWindow();
  const params = new URLSearchParams({ start: start.toISOString(), end: end.toISOString(), dataset: "all", format, limit: "10000" });
  window.location.assign(`/api/history/export?${params}`);
}

function renderWindowsEvents(summary = null) {
  const counts = summary?.counts || {};
  text("#windows-defender-count", counts.defender_detection || 0);
  text("#windows-action-count", counts.defender_action || 0);
  text("#windows-service-count", counts.service_failure || 0);
  text("#windows-other-count", (counts.system || 0) + (counts.application || 0));
  const statuses = summary?.channels || [];
  text("#windows-channel-status", statuses.length
    ? statuses.map((item) => `${item.channel}: ${item.status}${item.detail ? ` (${item.detail})` : ""}`).join(" · ")
    : "No channel status is available yet.");
  text("#windows-privacy-status", "REDACTED VIEW · usernames, SIDs, home paths, and sensitive paths are masked for dashboard and screenshot safety. Raw records remain local and require an explicit API privacy=raw request.");
  const body = $("#windows-events-table");
  body.replaceChildren();
  state.windowsEvents.forEach((item) => {
    const row = body.insertRow();
    makeCell(row, new Date(item.timestamp).toLocaleString());
    makeCell(row, item.channel.replace("Microsoft-Windows-Windows Defender/Operational", "Defender"));
    makeCell(row, item.provider);
    makeCell(row, item.event_id);
    makeCell(row, item.level, ["ERROR", "CRITICAL"].includes(item.level) ? "negative" : "");
    makeCell(row, item.category);
    makeCell(row, item.summary);
  });
  if (!state.windowsEvents.length) emptyTable(body, 7, state.demo ? "Windows events are unavailable in demo mode" : "No retained Windows events match these filters");
  if (state.windowsEventSettings) {
    const form = $("#windows-events-settings");
    Object.entries(state.windowsEventSettings).forEach(([key, value]) => {
      const input = form.elements.namedItem(key);
      if (!input) return;
      if (input.type === "checkbox") input.checked = value; else input.value = value;
    });
  }
}

async function loadWindowsEvents() {
  if (state.demo) {
    state.windowsEvents = [];
    renderWindowsEvents();
    return;
  }
  const params = new URLSearchParams({ limit: "1000", privacy: "redacted" });
  const filters = {
    channel: $("#windows-channel").value,
    category: $("#windows-category").value,
    level: $("#windows-level").value,
    search: $("#windows-search").value.trim(),
  };
  Object.entries(filters).forEach(([key, value]) => { if (value) params.set(key, value); });
  try {
    const [eventsResponse, summaryResponse, settingsResponse] = await Promise.all([
      fetch(`/api/windows-events?${params}`),
      fetch("/api/windows-events/summary"),
      fetch("/api/windows-events/settings"),
    ]);
    if (!eventsResponse.ok || !summaryResponse.ok || !settingsResponse.ok) throw new Error("events unavailable");
    state.windowsEvents = await eventsResponse.json();
    state.windowsEventSettings = await settingsResponse.json();
    renderWindowsEvents(await summaryResponse.json());
  } catch {
    state.windowsEvents = [];
    renderWindowsEvents();
    showToast("Windows event summaries are currently unavailable");
  }
}

async function saveWindowsEventSettings(event) {
  event.preventDefault();
  if (state.demo) { showToast("Exit demo mode to change local collection settings"); return; }
  const form = event.currentTarget;
  const payload = {
    enabled: form.elements.enabled.checked,
    defender_enabled: form.elements.defender_enabled.checked,
    system_enabled: form.elements.system_enabled.checked,
    application_enabled: form.elements.application_enabled.checked,
    poll_interval_seconds: Number(form.elements.poll_interval_seconds.value),
    max_events_per_channel: Number(form.elements.max_events_per_channel.value),
    retention_hours: Number(form.elements.retention_hours.value),
  };
  const response = await fetch("/api/windows-events/settings", {
    method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
  });
  if (!response.ok) { showToast("Windows event settings were not saved"); return; }
  state.windowsEventSettings = await response.json();
  showToast("Windows event settings saved locally");
  await loadWindowsEvents();
}

function renderFileIntegrity() {
  const folderBody = $("#file-folder-table");
  folderBody.replaceChildren();
  state.monitoredFolders.forEach((folder) => {
    const row = folderBody.insertRow();
    makeCell(row, folder.path);
    makeCell(row, `${folder.status}${folder.status_detail ? ` · ${folder.status_detail}` : ""}`);
    makeCell(row, folder.baseline_created_at ? new Date(folder.baseline_created_at).toLocaleString() : "Pending");
    makeCell(row, folder.enabled ? "Yes" : "No");
    const actions = row.insertCell();
    const toggle = element("button", "text-button", folder.enabled ? "PAUSE" : "RESUME");
    toggle.type = "button";
    toggle.addEventListener("click", () => updateMonitoredFolder(folder.id, !folder.enabled));
    const remove = element("button", "text-button", "REMOVE");
    remove.type = "button";
    remove.addEventListener("click", () => removeMonitoredFolder(folder));
    actions.append(toggle, remove);
  });
  if (!state.monitoredFolders.length) emptyTable(folderBody, 5, "No folders are enrolled; monitoring is opt-in");
  const eventBody = $("#file-event-table");
  eventBody.replaceChildren();
  state.fileIntegrityEvents.forEach((item) => {
    const row = eventBody.insertRow();
    makeCell(row, new Date(item.timestamp).toLocaleString());
    makeCell(row, item.change_type, item.change_type === "created" ? "positive" : "negative");
    makeCell(row, item.relative_path);
    makeCell(row, item.previous_sha256 || "—");
    makeCell(row, item.current_sha256 || "—");
    makeCell(row, item.size === null ? "—" : bytes(item.size));
  });
  if (!state.fileIntegrityEvents.length) emptyTable(eventBody, 6, "No file integrity events match this view");
  if (state.fileIntegritySettings) {
    const form = $("#file-integrity-settings");
    Object.entries(state.fileIntegritySettings).forEach(([key, value]) => {
      const input = form.elements.namedItem(key);
      if (!input) return;
      if (input.type === "checkbox") input.checked = value; else input.value = value;
    });
  }
}

async function loadFileIntegrity() {
  if (state.demo) {
    state.monitoredFolders = [];
    state.fileIntegrityEvents = [];
    renderFileIntegrity();
    return;
  }
  const params = new URLSearchParams({ limit: "1000" });
  const change = $("#file-change-filter").value;
  const search = $("#file-event-search").value.trim();
  if (change) params.set("change_type", change);
  if (search) params.set("search", search);
  try {
    const [settings, folders, events] = await Promise.all([
      fetch("/api/file-integrity/settings"),
      fetch("/api/file-integrity/folders"),
      fetch(`/api/file-integrity/events?${params}`),
    ]);
    if (!settings.ok || !folders.ok || !events.ok) throw new Error("file integrity unavailable");
    state.fileIntegritySettings = await settings.json();
    state.monitoredFolders = await folders.json();
    state.fileIntegrityEvents = await events.json();
  } catch {
    showToast("File integrity data is currently unavailable");
  }
  renderFileIntegrity();
}

async function saveFileIntegritySettings(event) {
  event.preventDefault();
  if (state.demo) { showToast("Exit demo mode to change local monitoring"); return; }
  const form = event.currentTarget;
  const payload = {
    enabled: form.elements.enabled.checked,
    scan_interval_seconds: Number(form.elements.scan_interval_seconds.value),
    max_files_per_folder: Number(form.elements.max_files_per_folder.value),
    max_file_size_bytes: Number(form.elements.max_file_size_bytes.value),
    event_retention: Number(form.elements.event_retention.value),
  };
  const response = await fetch("/api/file-integrity/settings", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
  if (!response.ok) { showToast("File integrity settings were not saved"); return; }
  state.fileIntegritySettings = await response.json();
  showToast("File integrity settings saved locally");
  renderFileIntegrity();
}

async function addMonitoredFolder(event) {
  event.preventDefault();
  if (state.demo) { showToast("Exit demo mode to enroll a real folder"); return; }
  const form = event.currentTarget;
  const path = form.elements.namedItem("path").value.trim();
  const response = await fetch("/api/file-integrity/folders", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path }) });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    showToast(payload.detail || "Folder baseline could not be created");
    return;
  }
  form.reset();
  showToast("SHA-256 baseline created");
  await loadFileIntegrity();
}

async function updateMonitoredFolder(folderId, enabled) {
  const response = await fetch(`/api/file-integrity/folders/${encodeURIComponent(folderId)}`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ enabled }) });
  if (!response.ok) { showToast("Folder state was not updated"); return; }
  await loadFileIntegrity();
}

async function removeMonitoredFolder(folder) {
  if (!window.confirm(`Remove GhostWatch's baseline for ${folder.path}? No files will be deleted.`)) return;
  const response = await fetch(`/api/file-integrity/folders/${encodeURIComponent(folder.id)}`, { method: "DELETE" });
  if (!response.ok) { showToast("Folder baseline was not removed"); return; }
  showToast("Folder baseline and retained integrity events removed");
  await loadFileIntegrity();
}

async function scanFileIntegrityNow() {
  if (state.demo) { showToast("Demo mode does not scan local folders"); return; }
  const response = await fetch("/api/file-integrity/scan", { method: "POST" });
  if (!response.ok) { showToast("File integrity scan failed"); return; }
  const events = await response.json();
  showToast(`Integrity scan completed · ${events.length} new change(s)`);
  await loadFileIntegrity();
}

function renderTimeline() {
  const severity = $("#timeline-severity").value;
  const query = $("#timeline-search").value.trim().toLowerCase();
  const cutoff = Date.now() - Number($("#timeline-range").value) * 60000;
  const events = state.events.filter((event) =>
    new Date(event.timestamp).getTime() >= cutoff
    && (!severity || event.severity === severity)
    && (!query || `${event.title} ${event.summary} ${event.event_type}`.toLowerCase().includes(query))
  );
  const body = $("#history-table");
  body.replaceChildren();
  events.forEach((event) => {
    const row = body.insertRow();
    makeCell(row, new Date(event.timestamp).toLocaleString());
    makeCell(row, event.severity, event.severity === "LOW" ? "positive" : "");
    makeCell(row, event.event_type);
    makeCell(row, event.title);
    makeCell(row, event.summary);
  });
  if (!events.length) emptyTable(body, 5, "No retained events match this range and filter");
  const track = $("#timeline-track");
  track.replaceChildren();
  if (events.length) {
    const times = events.map((item) => new Date(item.timestamp).getTime());
    const min = Math.min(...times); const max = Math.max(...times);
    events.slice(0, 100).forEach((event) => {
      const dot = element("i", `timeline-dot ${event.severity}`);
      const value = new Date(event.timestamp).getTime();
      dot.style.left = `${((value - min) / Math.max(max - min, 1)) * 100}%`;
      dot.title = `${event.title} · ${new Date(event.timestamp).toLocaleString()}`;
      track.append(dot);
    });
  }
}

async function loadEvents() {
  if (!state.demo) {
    const response = await fetch("/api/events?limit=1000");
    if (response.ok) state.events = await response.json();
  }
  renderTimeline();
  renderOverviewEvents();
}

function renderIncidentList() {
  const container = $("#incident-list");
  const query = $("#incident-search").value.trim().toLowerCase();
  const incidents = state.incidents.filter((item) => !query || `${item.title} ${item.id} ${item.status}`.toLowerCase().includes(query));
  container.replaceChildren();
  container.classList.toggle("empty-state", incidents.length === 0);
  if (!incidents.length) {
    container.textContent = "No incidents match this view";
    return;
  }
  incidents.forEach((incident) => {
    const card = element("button", "incident-card");
    card.type = "button";
    if (state.selectedIncident?.id === incident.id) card.classList.add("selected");
    card.append(element("span", `severity ${incident.severity}`, incident.severity), element("span", "", incident.title), element("small", "", incident.status));
    card.addEventListener("click", () => {
      state.selectedIncident = incident;
      renderIncidentList();
      renderIncidentDetail();
    });
    container.append(card);
  });
}

function renderIncidentDetail() {
  const container = $("#incident-detail");
  container.replaceChildren();
  const incident = state.selectedIncident;
  if (!incident) {
    container.className = "empty-state";
    container.textContent = "Select an incident to inspect it.";
    return;
  }
  container.className = "incident-detail";
  container.append(element("h2", "", incident.title), element("p", "", `${incident.id} · ${new Date(incident.detection_timestamp).toLocaleString()}`), element("p", "", incident.evidence_summary || "No evidence summary was entered."));
  const statusLabel = element("label", "", "Status");
  const status = document.createElement("select");
  ["NEW", "INVESTIGATING", "RESOLVED", "DISMISSED"].forEach((value) => {
    const option = element("option", "", value);
    option.value = value; option.selected = value === incident.status; status.append(option);
  });
  statusLabel.append(status);
  const notesLabel = element("label", "", "Analyst notes");
  const notes = document.createElement("textarea");
  notes.rows = 8; notes.maxLength = 8000; notes.value = incident.analyst_notes || ""; notesLabel.append(notes);
  const save = element("button", "primary-button", "SAVE CASE");
  save.type = "button";
  save.addEventListener("click", () => updateIncident(incident.id, { status: status.value, analyst_notes: notes.value }));
  const related = element("p", "", `Related alerts: ${incident.related_alerts.length} · Processes: ${incident.related_processes.length} · Connections: ${incident.related_connections.length}`);
  container.append(statusLabel, notesLabel, related, save);
}

async function loadIncidents() {
  if (!state.demo) {
    const response = await fetch("/api/incidents");
    if (response.ok) state.incidents = await response.json();
  }
  renderIncidentList();
  renderIncidentDetail();
}

async function updateIncident(id, patch) {
  if (state.demo) {
    const index = state.incidents.findIndex((item) => item.id === id);
    state.incidents[index] = { ...state.incidents[index], ...patch, last_observed: new Date().toISOString() };
    state.selectedIncident = state.incidents[index];
    renderIncidentList(); renderIncidentDetail(); showToast("Demo case updated in memory only");
    return;
  }
  const response = await fetch(`/api/incidents/${encodeURIComponent(id)}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(patch) });
  if (!response.ok) { showToast("Incident update was not persisted"); return; }
  const updated = await response.json();
  const index = state.incidents.findIndex((item) => item.id === id);
  state.incidents[index] = updated; state.selectedIncident = updated;
  renderIncidentList(); renderIncidentDetail(); showToast("Incident saved locally");
}

async function createIncident(form) {
  if (!state.selectedAlerts.size) { showToast("Select at least one alert in Detection Center"); return; }
  const payload = {
    title: form.title.value,
    severity: form.severity.value,
    alert_ids: [...state.selectedAlerts],
    evidence_summary: form.evidence_summary.value,
    analyst_notes: form.analyst_notes.value,
  };
  if (state.demo) {
    const now = new Date().toISOString();
    const incident = { id: `DEMO-INCIDENT-${state.incidents.length + 1}`, ...payload, detection_timestamp: now, first_observed: now, last_observed: now, related_processes: [], related_connections: [], related_alerts: payload.alert_ids, status: "NEW" };
    delete incident.alert_ids;
    state.incidents.unshift(incident);
    state.selectedIncident = incident;
    renderIncidentList(); renderIncidentDetail(); showToast("Demo incident created in memory only");
    return;
  }
  const response = await fetch("/api/incidents", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
  if (!response.ok) { showToast("Incident was not created"); return; }
  const incident = await response.json();
  state.incidents.unshift(incident); state.selectedIncident = incident; state.selectedAlerts.clear();
  renderIncidentList(); renderIncidentDetail(); renderAlerts(); showToast("Incident persisted locally");
}

async function loadSettings() {
  const [coreResponse, detectionResponse, aiResponse] = await Promise.all([
    fetch("/api/settings"), fetch("/api/detection-settings"), fetch("/api/ai/settings"),
  ]);
  if (coreResponse.ok) {
    state.settings = await coreResponse.json();
    const form = $("#settings-form");
    Object.entries(state.settings).forEach(([key, value]) => {
      const input = form.elements.namedItem(key);
      if (!input) return;
      if (input.type === "checkbox") input.checked = value; else input.value = value;
    });
  }
  if (detectionResponse.ok) {
    state.detectionSettings = await detectionResponse.json();
    const form = $("#rules-form");
    Object.entries(state.detectionSettings).forEach(([key, value]) => {
      if (key === "enabled_rules") return;
      const input = form.elements.namedItem(key);
      if (input) input.value = value;
    });
    const toggles = $("#rule-toggles");
    toggles.replaceChildren(element("legend", "", "Enabled rules"));
    Object.entries(state.detectionSettings.enabled_rules).forEach(([key, enabled]) => {
      const label = element("label");
      const input = document.createElement("input");
      input.type = "checkbox"; input.name = key; input.checked = enabled;
      label.append(input, document.createTextNode(key.replaceAll("_", " ")));
      toggles.append(label);
    });
  }
  if (aiResponse.ok) {
    state.aiSettings = await aiResponse.json();
    const form = $("#ai-settings-form");
    Object.entries(state.aiSettings).forEach(([key, value]) => {
      const input = form.elements.namedItem(key);
      if (!input) return;
      if (input.type === "checkbox") input.checked = value; else input.value = value;
    });
    text("#ai-state", state.aiSettings.enabled ? "LOCAL OLLAMA ENABLED" : "DISABLED");
  }
}

async function saveSettings(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const payload = {
    refresh_interval: Number(form.refresh_interval.value),
    cpu_threshold: Number(form.cpu_threshold.value),
    memory_threshold: Number(form.memory_threshold.value),
    bandwidth_threshold_mbps: Number(form.bandwidth_threshold_mbps.value),
    connection_threshold: Number(form.connection_threshold.value),
    threshold_duration: Number(form.threshold_duration.value),
    alert_retention: Number(form.alert_retention.value),
    event_logging_enabled: form.event_logging_enabled.checked,
  };
  const response = await fetch("/api/settings", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
  if (!response.ok) { text("#settings-status", "NOT PERSISTED"); showToast("Settings were not persisted"); return; }
  state.settings = await response.json(); text("#settings-status", "SAVED LOCALLY"); showToast("Settings saved locally");
}

async function saveRules(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const enabled = {};
  $$('input[type="checkbox"]', $("#rule-toggles")).forEach((input) => { enabled[input.name] = input.checked; });
  const payload = {
    enabled_rules: enabled,
    cooldown_seconds: Number(form.cooldown_seconds.value),
    process_count_threshold: Number(form.process_count_threshold.value),
    disk_free_threshold_percent: Number(form.disk_free_threshold_percent.value),
    destination_diversity_threshold: Number(form.destination_diversity_threshold.value),
    connection_growth_threshold: Number(form.connection_growth_threshold.value),
  };
  const response = await fetch("/api/detection-settings", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
  text("#rules-status", response.ok ? "SAVED LOCALLY" : "NOT PERSISTED");
  showToast(response.ok ? "Detection rules saved" : "Detection rules were not persisted");
}

async function saveAISettings(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const payload = {
    enabled: form.enabled.checked,
    endpoint: form.endpoint.value,
    model: form.model.value,
    context_size: Number(form.context_size.value),
    timeout_seconds: Number(form.timeout_seconds.value),
    redact_telemetry: form.redact_telemetry.checked,
  };
  const response = await fetch("/api/ai/settings", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
  if (!response.ok) { showToast("AI settings were rejected or not persisted"); return; }
  state.aiSettings = await response.json();
  text("#ai-state", state.aiSettings.enabled ? "LOCAL OLLAMA ENABLED" : "DISABLED");
  showToast("Local AI settings saved");
}

function renderAI(result) {
  const output = $("#ai-output");
  output.replaceChildren();
  output.className = "ai-output";
  [
    ["OBSERVED EVIDENCE", result.observed_evidence],
    ["POSSIBLE EXPLANATIONS", result.possible_explanations],
    ["MISSING INFORMATION", result.missing_information],
    ["RECOMMENDED DEFENSIVE CHECKS", result.recommended_checks],
  ].forEach(([heading, body]) => {
    const section = element("section", "ai-section");
    section.append(element("h3", "", heading), element("p", "", body || "The model did not provide this section."));
    output.append(section);
  });
}

async function askAI(event) {
  event.preventDefault();
  if (state.demo) {
    renderAI({ observed_evidence: "Synthetic upload and destination diversity are above their demo baselines.", possible_explanations: "A normal update, backup, or browser workload could explain this pattern.", missing_information: "Demo mode has no packet payloads, signatures, or real host evidence.", recommended_checks: "In live mode, inspect owning processes and compare their executable paths and connection history." });
    return;
  }
  text("#ai-state", "WAITING FOR LOCAL OLLAMA");
  const response = await fetch("/api/ai/analyze", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ question: $("#ai-question").value, alert_ids: [...state.selectedAlerts].slice(0, 20) }) });
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    text("#ai-state", "OFFLINE OR DISABLED");
    showToast(data.detail || "Local Ollama is unavailable");
    return;
  }
  renderAI(await response.json());
  text("#ai-state", `LOCAL RESULT · ${state.aiSettings?.model || "OLLAMA"}`);
}

function enterDemo() {
  if (state.demo) return;
  state.socket?.disconnect();
  state.demo = true;
  const demo = createDemoData();
  state.history = [];
  state.events = demo.events;
  state.incidents = demo.incidents;
  $("#demo-banner").hidden = false;
  text("#mode-label", "DEMO");
  renderSnapshot(demo.snapshot);
  renderTimeline(); renderOverviewEvents(); renderIncidentList();
  showToast("Demo mode uses synthetic in-memory data only");
}

async function exitDemo() {
  state.demo = false;
  state.history = []; state.events = []; state.incidents = []; state.selectedIncident = null;
  $("#demo-banner").hidden = true;
  text("#mode-label", "LIVE");
  await loadInitialData();
  startSocket();
  showToast("Returned to live local monitoring");
}

function startSocket() {
  state.socket?.disconnect();
  state.socket = new TelemetrySocket(
    (snapshot) => { if (!state.demo) renderSnapshot(snapshot); },
    (status) => {
      if (state.demo) return;
      text("#socket-status", status);
      $("#socket-status").style.color = status === "CONNECTED" ? "var(--green)" : "var(--amber)";
    },
  );
  state.socket.connect();
}

function bindEvents() {
  $$(".nav-item").forEach((node) => node.addEventListener("click", () => switchView(node.dataset.view)));
  $$("[data-go]").forEach((node) => node.addEventListener("click", () => switchView(node.dataset.go)));
  $("#mobile-menu").addEventListener("click", (event) => {
    const open = $("#sidebar").classList.toggle("open");
    event.currentTarget.setAttribute("aria-expanded", String(open));
  });
  $("#drawer-close").addEventListener("click", () => {
    const drawer = $("#process-drawer");
    drawer.classList.remove("open"); drawer.setAttribute("aria-hidden", "true"); drawer.setAttribute("inert", "");
    const current = lastDrawerPid ? $(`#process-table tr[data-pid="${CSS.escape(lastDrawerPid)}"]`) : null;
    (current || lastDrawerTrigger)?.focus();
  });
  $("#process-search").addEventListener("input", debounce(renderProcessTable));
  $("#network-search").addEventListener("input", debounce(renderConnections));
  $("#network-scope").addEventListener("change", renderConnections);
  $("#tree-search").addEventListener("input", debounce(renderTree));
  $("#tree-expand").addEventListener("click", () => {
    const collapsed = $$(".tree-children:not(.collapsed)").length > 0;
    $$(".tree-children").forEach((node) => node.classList.toggle("collapsed", collapsed));
    text("#tree-expand", collapsed ? "EXPAND ALL" : "COLLAPSE ALL");
  });
  $$(".sort-button[data-sort]").forEach((node) => node.addEventListener("click", () => {
    const key = node.dataset.sort;
    state.processSort = state.processSort.key === key ? { key, direction: state.processSort.direction * -1 } : { key, direction: 1 };
    $$(".sort-button[data-sort]").forEach((button) => {
      button.removeAttribute("aria-sort");
      button.textContent = button.textContent.replace(/ [↑↓]$/, "");
    });
    node.setAttribute("aria-sort", state.processSort.direction > 0 ? "ascending" : "descending");
    node.textContent += state.processSort.direction > 0 ? " ↑" : " ↓";
    renderProcessTable();
  }));
  $$("[data-severity]").forEach((node) => node.addEventListener("click", () => {
    state.alertSeverity = node.dataset.severity;
    $$("[data-severity]").forEach((button) => button.classList.toggle("active", button === node));
    renderAlerts();
  }));
  $("#graph-search").addEventListener("input", debounce(() => {
    graph.setFilters($("#graph-search").value, $("#graph-protocol").value);
    text("#graph-count", `${graph.nodes.length} NODES · ${graph.edges.length} EDGES`);
  }));
  $("#graph-protocol").addEventListener("change", () => graph.setFilters($("#graph-search").value, $("#graph-protocol").value));
  $("#graph-reset").addEventListener("click", () => graph.reset());
  $("#history-refresh").addEventListener("click", loadEvents);
  $("#intelligence-refresh").addEventListener("click", loadHistoricalIntelligence);
  $("#intelligence-range").addEventListener("change", loadHistoricalIntelligence);
  $("#history-export-json").addEventListener("click", () => exportHistorical("json"));
  $("#history-export-csv").addEventListener("click", () => exportHistorical("csv"));
  $("#windows-events-refresh").addEventListener("click", loadWindowsEvents);
  ["windows-channel", "windows-category", "windows-level"].forEach((id) => $(`#${id}`).addEventListener("change", loadWindowsEvents));
  $("#windows-search").addEventListener("input", debounce(loadWindowsEvents, 250));
  $("#windows-events-settings").addEventListener("submit", saveWindowsEventSettings);
  $("#file-integrity-settings").addEventListener("submit", saveFileIntegritySettings);
  $("#file-folder-form").addEventListener("submit", addMonitoredFolder);
  $("#file-integrity-scan").addEventListener("click", scanFileIntegrityNow);
  $("#file-change-filter").addEventListener("change", loadFileIntegrity);
  $("#file-event-search").addEventListener("input", debounce(loadFileIntegrity, 250));
  ["timeline-range", "timeline-severity"].forEach((id) => $(`#${id}`).addEventListener("change", renderTimeline));
  $("#timeline-search").addEventListener("input", debounce(renderTimeline));
  $("#incident-search").addEventListener("input", debounce(renderIncidentList));
  $("#incident-new").addEventListener("click", () => {
    if (!state.selectedAlerts.size) { showToast("Select alerts in Detection Center first"); switchView("alerts"); return; }
    $("#incident-dialog").showModal();
  });
  $("#incident-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const submitter = event.submitter;
    if (submitter?.value === "cancel") { $("#incident-dialog").close(); return; }
    if (!form.reportValidity()) return;
    await createIncident(form);
    $("#incident-dialog").close(); form.reset(); switchView("incidents");
  });
  $("#settings-form").addEventListener("submit", saveSettings);
  $("#rules-form").addEventListener("submit", saveRules);
  $("#ai-settings-form").addEventListener("submit", saveAISettings);
  $("#ai-form").addEventListener("submit", askAI);
  $("#settings-reset").addEventListener("click", () => {
    const defaults = { refresh_interval: 2, cpu_threshold: 90, memory_threshold: 90, bandwidth_threshold_mbps: 3, connection_threshold: 100, threshold_duration: 10, alert_retention: 1000, event_logging_enabled: true };
    const form = $("#settings-form");
    Object.entries(defaults).forEach(([key, value]) => {
      const input = form.elements.namedItem(key);
      if (input.type === "checkbox") input.checked = value; else input.value = value;
    });
    showToast("Defaults loaded; press Save Settings to persist");
  });
  $("#enter-demo").addEventListener("click", enterDemo);
  $("#exit-demo").addEventListener("click", exitDemo);
  $$(".report-card").forEach((node) => node.addEventListener("click", () => {
    state.selectedReport = node.dataset.report;
    $$(".report-card").forEach((item) => item.classList.toggle("selected", item === node));
    const button = $("#report-download"); button.disabled = false; button.textContent = `EXPORT ${node.textContent.trim().split("\n")[0].toUpperCase()}`;
  }));
  $("#report-download").addEventListener("click", () => {
    if (!state.selectedReport) return;
    const format = $("#report-format").value;
    const redact = $("#report-redact").checked ? "true" : "false";
    window.location.assign(`/api/reports/${encodeURIComponent(state.selectedReport)}.${format}?redact=${redact}`);
  });
  window.addEventListener("resize", debounce(() => { drawAllCharts(); graph.draw(); }, 100));
  window.addEventListener("keydown", (event) => { if (event.key === "Escape" && $("#process-drawer").classList.contains("open")) $("#drawer-close").click(); });
  window.addEventListener("beforeunload", () => state.socket?.disconnect());
}

async function loadInitialData() {
  try {
    const [overviewResponse, historyResponse] = await Promise.all([fetch("/api/overview"), fetch("/api/history")]);
    if (historyResponse.ok) state.history = (await historyResponse.json()).samples;
    if (overviewResponse.ok) renderSnapshot(await overviewResponse.json());
  } catch {
    showToast("Waiting for the local monitor…");
  }
  await Promise.all([loadEvents(), loadIncidents(), loadSettings()]);
}

const graph = new NetworkGraph($("#network-graph"), (node) => {
  text("#graph-summary", `Selected ${node.kind} node: ${node.label}`);
  if (node.kind === "process") {
    const process = state.snapshot?.processes.find((item) => item.name === node.label);
    if (process) openProcess(process, $("#network-graph"));
  } else {
    showToast(`Selected ${node.kind}: ${node.label}`);
  }
});

async function initialize() {
  bindEvents();
  const updateClock = () => text("#clock", new Intl.DateTimeFormat([], { weekday: "short", month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" }).format(new Date()));
  updateClock(); setInterval(updateClock, 1000);
  await loadInitialData();
  startSocket();
  const initial = location.hash.slice(1);
  if (initial && $(`[data-view-panel="${CSS.escape(initial)}"]`)) switchView(initial);
}

initialize();
