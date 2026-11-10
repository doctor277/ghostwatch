import { expect, test } from "@playwright/test";

import { installSyntheticBackend } from "./synthetic-backend.js";

let backend;
let consoleProblems;

test.beforeEach(async ({ page }) => {
  consoleProblems = [];
  page.on("console", (message) => {
    if (["error", "warning"].includes(message.type())) consoleProblems.push(`${message.type()}: ${message.text()}`);
  });
  page.on("pageerror", (error) => consoleProblems.push(`pageerror: ${error.message}`));
  backend = await installSyntheticBackend(page);
  await page.goto("/");
  await expect(page).toHaveTitle(/GhostWatch/);
  await expect(page.locator("[data-view-panel=overview]")).toBeVisible();
  await expect(page.locator("#socket-status")).toHaveText("CONNECTED");
});

test.afterEach(() => {
  expect(consoleProblems).toEqual([]);
});

test("all navigation sections render with synthetic data", async ({ page }, testInfo) => {
  const sections = [
    ["overview", "OVERVIEW"],
    ["network", "NETWORK INTELLIGENCE"],
    ["graph", "NETWORK GRAPH"],
    ["processes", "PROCESS EXPLORER"],
    ["tree", "PROCESS TREE"],
    ["alerts", "DETECTION CENTER"],
    ["incidents", "SECURITY INCIDENTS"],
    ["history", "EVENT TIMELINE"],
    ["intelligence", "HISTORICAL INTELLIGENCE"],
    ["windows-events", "WINDOWS SECURITY EVENTS"],
    ["file-integrity", "FILE INTEGRITY MONITOR"],
    ["ai", "GHOSTWATCH AI ANALYST"],
    ["performance", "SYSTEM PERFORMANCE"],
    ["reports", "REPORTING CENTER"],
    ["settings", "SETTINGS"],
  ];
  for (const [name, heading] of sections) {
    await page.locator(`[data-view="${name}"]`).click();
    const panel = page.locator(`[data-view-panel="${name}"]`);
    await expect(panel).toBeVisible();
    await expect(panel.getByRole("heading", { level: 1 })).toHaveText(heading);
  }
  await page.locator('[data-view="graph"]').click();
  await expect(page.locator("#network-graph")).toBeVisible();
  await expect(page.locator("#graph-count")).toContainText("NODES");
  await testInfo.attach("desktop-network-graph", { body: await page.screenshot(), contentType: "image/png" });
});

test("process search, sorting, drawer, and keyboard close work", async ({ page }) => {
  await page.locator('[data-view="processes"]').click();
  await page.locator("#process-search").fill("browser-demo");
  await expect(page.locator("#process-table tr")).toHaveCount(1);
  await page.locator('[data-sort="cpu_percent"]').click();
  await expect(page.locator('[data-sort="cpu_percent"]')).toHaveAttribute("aria-sort", "ascending");
  await page.locator("#process-table tr").click();
  await expect(page.locator("#process-drawer")).toHaveClass(/open/);
  await expect(page.locator("#drawer-name")).toContainText("browser-demo.exe");
  await page.keyboard.press("Escape");
  await expect(page.locator("#process-drawer")).not.toHaveClass(/open/);
});

test("alerts acknowledge and dismiss without stale counters", async ({ page }) => {
  await page.locator('[data-view="alerts"]').click();
  await page.getByRole("button", { name: "ACKNOWLEDGE" }).first().click();
  await expect(page.locator("#alert-list")).toContainText("ACKNOWLEDGED");
  await page.getByRole("button", { name: "DISMISS" }).first().click();
  await expect(page.locator("#nav-alert-count")).toHaveText("0");
});

test("threshold alerts show their lifecycle state", async ({ page }) => {
  const recoveredAt = new Date().toISOString();
  backend.state.snapshot.alerts = [{
    id: "synthetic-memory-alert", timestamp: recoveredAt, severity: "CRITICAL", source: "System",
    metric: "Memory 41.0%", explanation: "Recovered: the latest sample is within the configured threshold.",
    acknowledged: false, dismissed: false, rule_id: "memory", evidence: { observed_percent: 41, threshold_percent: 90 },
    persistence_pending: false, first_occurrence: new Date(Date.now() - 600000).toISOString(),
    last_occurrence: new Date(Date.now() - 60000).toISOString(), occurrence_count: 4,
    condition_active: false, last_recovery: recoveredAt,
  }];
  backend.sendSnapshot();
  await page.locator('[data-view="alerts"]').click();
  await expect(page.locator("#alert-list")).toContainText("STATE: RECOVERED");
  await expect(page.locator("#alert-list")).toContainText("Occurrences: 4");
  await expect(page.locator("#nav-alert-count")).toHaveText("0");
});

test("settings persist across a browser reload", async ({ page }) => {
  await page.locator('[data-view="settings"]').click();
  await page.locator('#settings-form [name="refresh_interval"]').fill("5");
  await page.locator("#settings-form").getByRole("button", { name: "SAVE SETTINGS" }).click();
  await expect(page.locator("#settings-status")).toHaveText("SAVED LOCALLY");
  await page.reload();
  await page.locator('[data-view="settings"]').click();
  await expect(page.locator('#settings-form [name="refresh_interval"]')).toHaveValue("5");
});

test("historical charts expose gaps and selected export", async ({ page }) => {
  await page.locator('[data-view="intelligence"]').click();
  await expect(page.locator("#intelligence-status")).toContainText("24 telemetry points");
  await expect(page.locator("#history-gaps")).toContainText("missing-data interval");
  await expect(page.locator("#historical-process-table tr")).toHaveCount(3);
  const download = page.waitForEvent("download");
  await page.locator("#history-export-json").click();
  expect((await download).suggestedFilename()).toBe("synthetic-history.json");
});

test("alerts can seed an incident and the case can be updated", async ({ page }) => {
  await page.locator('[data-view="alerts"]').click();
  await page.getByRole("checkbox", { name: /Select alert/ }).first().check();
  await page.locator('[data-view="incidents"]').click();
  await page.locator("#incident-new").click();
  await page.locator('#incident-form [name="title"]').fill("Synthetic investigation");
  await page.locator('#incident-form [name="evidence_summary"]').fill("Synthetic evidence only");
  await page.locator('#incident-form button[value="create"]').click();
  await expect(page.locator("#incident-detail")).toContainText("Synthetic investigation");
  await page.locator("#incident-detail textarea").fill("Reviewed in browser automation");
  await page.locator("#incident-detail").getByRole("button", { name: "SAVE CASE" }).click();
  await expect(page.locator("#toast")).toContainText("Incident saved locally");
});

test("report generation begins only after explicit selection", async ({ page }) => {
  await page.locator('[data-view="reports"]').click();
  await expect(page.locator("#report-download")).toBeDisabled();
  await page.locator('[data-report="system-health"]').click();
  const download = page.waitForEvent("download");
  await page.locator("#report-download").click();
  expect((await download).suggestedFilename()).toBe("synthetic-report.json");
});

test("file integrity controls manage synthetic folders", async ({ page }) => {
  await page.locator('[data-view="file-integrity"]').click();
  await expect(page.locator("#file-folder-table")).toContainText("C:\\SYNTHETIC\\watched");
  await page.locator("#file-folder-path").fill("C:\\SYNTHETIC\\added");
  await page.locator("#file-folder-form").getByRole("button", { name: "CREATE SHA-256 BASELINE" }).click();
  await expect(page.locator("#file-folder-table")).toContainText("C:\\SYNTHETIC\\added");
  await page.locator("#file-integrity-scan").click();
  await expect(page.locator("#toast")).toContainText("1 new change");
});

test("demo mode is isolated from persistence and scanning", async ({ page }) => {
  await page.locator('[data-view="settings"]').click();
  await page.locator("#enter-demo").click();
  await expect(page.locator("#demo-banner")).toBeVisible();
  const writesBefore = backend.state.requests.filter((item) => /^(POST|PUT|PATCH|DELETE)/.test(item)).length;
  await page.locator('[data-view="file-integrity"]').click();
  await page.locator("#file-integrity-scan").click();
  await expect(page.locator("#toast")).toContainText("Demo mode does not scan");
  await page.locator('[data-view="windows-events"]').click();
  const writesAfter = backend.state.requests.filter((item) => /^(POST|PUT|PATCH|DELETE)/.test(item)).length;
  expect(writesAfter).toBe(writesBefore);
});

test("Windows event view is explicitly privacy redacted", async ({ page }) => {
  await page.locator('[data-view="windows-events"]').click();
  await expect(page.locator("#windows-privacy-status")).toContainText("REDACTED VIEW");
  await expect(page.locator("#windows-events-table")).toContainText("[REDACTED_USER]");
  await expect.poll(() => backend.state.requests.some((item) => item === "GET /api/windows-events")).toBe(true);
});

test("form fields have accessible names and keyboard focus is visible", async ({ page }) => {
  await page.locator('[data-view="file-integrity"]').click();
  await expect(page.locator("#file-folder-path")).toHaveAccessibleName("Absolute folder path");
  await page.locator("#file-folder-path").focus();
  await expect(page.locator("#file-folder-path")).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(page.locator("#file-folder-form button")).toBeFocused();
});

test("WebSocket reconnects after a synthetic server restart", async ({ page }) => {
  backend.disconnectLatest();
  await expect(page.locator("#socket-status")).toHaveText("RECONNECTING");
  await expect.poll(() => backend.state.sockets.length, { timeout: 5000 }).toBeGreaterThan(1);
  await expect(page.locator("#socket-status")).toHaveText("CONNECTED");
});

test("WebSocket recovers from a repeated synthetic restart storm", async ({ page }) => {
  for (let restart = 0; restart < 3; restart += 1) {
    const socketCount = backend.state.sockets.length;
    backend.disconnectLatest();
    await expect(page.locator("#socket-status")).toHaveText("RECONNECTING");
    await expect.poll(() => backend.state.sockets.length, { timeout: 5000 }).toBeGreaterThan(socketCount);
    await expect(page.locator("#socket-status")).toHaveText("CONNECTED");
  }
});

test("degraded telemetry replaces CONNECTED and clears on a valid snapshot", async ({ page }) => {
  const socketCount = backend.state.sockets.length;
  backend.sendDegraded();
  await expect(page.locator("#socket-status")).toHaveText("TELEMETRY DEGRADED");
  backend.sendSnapshot();
  await expect(page.locator("#socket-status")).toHaveText("CONNECTED");
  expect(backend.state.sockets.length).toBe(socketCount);
});

test("dropped writes stay visible while storage is healthy", async ({ page }) => {
  const status = page.locator("#storage-status");
  await expect(status).toHaveText("STORAGE HEALTHY");
  backend.state.snapshot.storage = {
    ...backend.state.snapshot.storage,
    status: "healthy",
    persistence_available: true,
    dropped_alert_writes: 2,
    dropped_event_writes: 3,
  };
  backend.sendSnapshot();
  await expect(status).toHaveText("STORAGE HEALTHY · 5 WRITES DROPPED");
  await expect(status).toHaveClass("status-warning");
  await expect(status).toHaveAttribute("title", /since GhostWatch started/);
});

test("frontend presents a safe settings persistence failure", async ({ page }) => {
  await page.locator('[data-view="settings"]').click();
  backend.failNext("PUT", "/api/settings");
  await page.locator('#settings-form [name="refresh_interval"]').fill("6");
  await page.locator("#settings-form").getByRole("button", { name: "SAVE SETTINGS" }).click();
  await expect(page.locator("#settings-status")).toHaveText("NOT PERSISTED");
  await expect(page.locator("#toast")).toContainText("Settings were not persisted");
  expect(consoleProblems).toEqual([
    "error: Failed to load resource: the server responded with a status of 503 (Service Unavailable)",
  ]);
  consoleProblems.length = 0;
});

test("@mobile navigation opens, selects a section, and closes", async ({ page }, testInfo) => {
  await expect(page.locator("#mobile-menu")).toBeVisible();
  await page.locator("#mobile-menu").click();
  await expect(page.locator("#sidebar")).toHaveClass(/open/);
  await page.locator('[data-view="intelligence"]').click();
  await expect(page.locator('[data-view-panel="intelligence"]')).toBeVisible();
  await expect(page.locator("#sidebar")).not.toHaveClass(/open/);
  await expect(page.locator("#mobile-menu")).toHaveAttribute("aria-expanded", "false");
  await testInfo.attach("mobile-historical-intelligence", { body: await page.screenshot(), contentType: "image/png" });
});
