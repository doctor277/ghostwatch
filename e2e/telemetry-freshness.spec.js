import { expect, test } from "@playwright/test";

import { createDemoData } from "../frontend/js/demo.js";
import { installSyntheticBackend } from "./synthetic-backend.js";

const STALE = "TELEMETRY STALE";
const AWAITING = "AWAITING TELEMETRY";
const AMBER = "rgb(255, 189, 46)";
const GREEN = "rgb(41, 244, 154)";
// Default 2 s refresh: max(10 s, 3 x 2 s + 5 s).
const DEADLINE_MS = 11000;

let backend;
let consoleProblems;
let frameMarker = 100;

function trackSocketTimers() {
  const active = new Set();
  let started = 0;
  const nativeSet = window.setTimeout;
  const nativeClear = window.clearTimeout;
  window.setTimeout = (callback, delay, ...args) => {
    const fromSocket = new Error().stack.includes("websocket.js");
    const id = nativeSet(() => { active.delete(id); callback(...args); }, delay);
    if (fromSocket) { active.add(id); started += 1; }
    return id;
  };
  window.clearTimeout = (id) => { active.delete(id); nativeClear(id); };
  window.socketTimers = () => ({ active: active.size, started });
}

const activeTimers = async (page) => (await page.evaluate(() => window.socketTimers())).active;
const startedTimers = async (page) => (await page.evaluate(() => window.socketTimers())).started;
const socketStatus = (page) => page.locator("#socket-status");

async function expectStatus(page, label, color = AMBER) {
  await expect(socketStatus(page)).toHaveText(label);
  await expect(socketStatus(page)).toHaveCSS("color", color);
}

async function openDashboard(page, prepare) {
  await page.clock.install();
  await page.addInitScript(trackSocketTimers);
  backend = await installSyntheticBackend(page);
  prepare?.(backend);
  await page.goto("/");
}

// Page time moves only on command from here on.
async function freezeClock(page) {
  await page.clock.pauseAt(await page.evaluate(() => Date.now() + 1000));
}

async function deliverSnapshot(page) {
  frameMarker += 1;
  backend.state.snapshot = { ...backend.state.snapshot, collection_duration_ms: frameMarker };
  backend.sendSnapshot();
  await expect(page.locator("#metric-duration")).toHaveText(`${frameMarker.toFixed(1)} ms`);
}

async function openConnected(page, prepare) {
  await openDashboard(page, prepare);
  await expectStatus(page, "CONNECTED", GREEN);
  await freezeClock(page);
  await deliverSnapshot(page);
}

test.beforeEach(({ page }) => {
  consoleProblems = [];
  page.on("console", (message) => {
    if (["error", "warning"].includes(message.type())) consoleProblems.push(`${message.type()}: ${message.text()}`);
  });
  page.on("pageerror", (error) => consoleProblems.push(`pageerror: ${error.message}`));
});

test.afterEach(() => {
  expect(consoleProblems).toEqual([]);
});

test("a silent open socket turns stale at the deadline and stays open", async ({ page }) => {
  await openConnected(page);
  await page.clock.runFor(DEADLINE_MS - 1);
  await expectStatus(page, "CONNECTED", GREEN);
  await page.clock.runFor(1);
  await expectStatus(page, STALE);
  await expect(page.locator("#mode-label")).toHaveCSS("color", AMBER);
  await expect(page.locator("#monitor-status")).toHaveCSS("color", AMBER);
  await expect(page.locator(".live-wave")).toHaveCSS("color", AMBER);
  await expect(page.locator("#last-update")).toContainText("STALE");
  await expect(socketStatus(page)).toHaveAttribute("title", /last received/);
  expect(backend.state.sockets).toHaveLength(1);
  expect(backend.state.closedSockets).toBe(0);
  expect(await activeTimers(page)).toBe(0);
});

test("a good snapshot clears stale without another socket", async ({ page }) => {
  await openConnected(page);
  await page.clock.runFor(DEADLINE_MS);
  await expectStatus(page, STALE);
  await deliverSnapshot(page);
  await expectStatus(page, "CONNECTED", GREEN);
  await expect(page.locator("#mode-label")).toHaveCSS("color", GREEN);
  await expect(page.locator("#monitor-status")).toHaveCSS("color", GREEN);
  await expect(page.locator("#last-update")).toContainText("UPDATED");
  await expect(socketStatus(page)).not.toHaveAttribute("title", /./);
  expect(backend.state.sockets).toHaveLength(1);
  expect(backend.state.closedSockets).toBe(0);
  await page.clock.runFor(DEADLINE_MS);
  await expectStatus(page, STALE);
});

test("regular frames share one timer instead of starting one per frame", async ({ page }) => {
  await openConnected(page);
  const started = await startedTimers(page);
  for (let frame = 0; frame < 3; frame += 1) {
    await page.clock.runFor(3000);
    await deliverSnapshot(page);
  }
  expect(await startedTimers(page)).toBe(started);
  expect(await activeTimers(page)).toBe(1);
  await expectStatus(page, "CONNECTED", GREEN);
});

test("a throttled timer rechecks elapsed time before reporting stale", async ({ page }) => {
  await openConnected(page);
  await page.clock.fastForward(10000);
  await deliverSnapshot(page);
  // The timer due at 11 s fires 9 s late, only 10 s after the newest frame.
  await page.clock.fastForward(10000);
  await expectStatus(page, "CONNECTED", GREEN);
  expect(await activeTimers(page)).toBe(1);
  await page.clock.runFor(999);
  await expectStatus(page, "CONNECTED", GREEN);
  await page.clock.runFor(1);
  await expectStatus(page, STALE);
});

test("degraded frames do not reset freshness and are not overridden by stale", async ({ page }) => {
  await openConnected(page);
  await page.clock.runFor(6000);
  backend.sendDegraded();
  await expectStatus(page, "TELEMETRY DEGRADED");
  await page.clock.runFor(DEADLINE_MS - 6000);
  // The original deadline passed and nothing rearmed it.
  expect(await activeTimers(page)).toBe(0);
  await page.clock.runFor(60000);
  await expectStatus(page, "TELEMETRY DEGRADED");
  await deliverSnapshot(page);
  await expectStatus(page, "CONNECTED", GREEN);
  expect(await activeTimers(page)).toBe(1);
});

test("malformed and invalid frames do not reset freshness or reach the dashboard", async ({ page }) => {
  const snapshot = createDemoData().snapshot;
  const frames = [
    ["malformed JSON", "{not json"],
    ["null", "null"],
    ["a JSON string", "\"telemetry\""],
    ["an array", "[]"],
    ["an empty object", "{}"],
    ["an unknown type", JSON.stringify({ type: "heartbeat" })],
    ["a bare telemetry type", JSON.stringify({ type: "telemetry" })],
    ["a missing system", JSON.stringify({ ...snapshot, system: undefined })],
    ["a process list that is not a list", JSON.stringify({ ...snapshot, processes: {} })],
  ];
  await openConnected(page);
  for (const [name, frame] of frames) {
    await test.step(name, async () => {
      await deliverSnapshot(page);
      await expectStatus(page, "CONNECTED", GREEN);
      await page.clock.runFor(6000);
      const rendered = await page.locator("#metric-duration").textContent();
      backend.sendRaw(frame);
      await expectStatus(page, "DATA ERROR");
      await page.clock.runFor(DEADLINE_MS - 6000);
      expect(await activeTimers(page)).toBe(0);
      await page.clock.runFor(60000);
      await expectStatus(page, "DATA ERROR");
      await expect(page.locator("#metric-duration")).toHaveText(rendered);
    });
  }
});

test("a snapshot the dashboard cannot render does not count as fresh", async ({ page }) => {
  await openConnected(page);
  await page.clock.runFor(6000);
  backend.sendRaw(JSON.stringify({ ...backend.state.snapshot, collection_duration_ms: null }));
  await expectStatus(page, "DATA ERROR");
  await page.clock.runFor(DEADLINE_MS - 6000);
  expect(await activeTimers(page)).toBe(0);
  await deliverSnapshot(page);
  await expectStatus(page, "CONNECTED", GREEN);
});

test("failure frames replace stale, and only a valid snapshot turns it green", async ({ page }) => {
  await openConnected(page);
  await page.clock.runFor(DEADLINE_MS);
  await expectStatus(page, STALE);
  backend.sendDegraded();
  await expectStatus(page, "TELEMETRY DEGRADED");
  backend.sendRaw("{not json");
  await expectStatus(page, "DATA ERROR");
  await deliverSnapshot(page);
  await expectStatus(page, "CONNECTED", GREEN);
});

test("a closed socket cancels the watchdog so stale cannot override RECONNECTING", async ({ page }) => {
  await openConnected(page);
  await page.clock.runFor(DEADLINE_MS - 500);
  backend.disconnectLatest();
  await expectStatus(page, "RECONNECTING");
  expect(await activeTimers(page)).toBe(1);
  // Crosses the old deadline but not the 1 s reconnect delay.
  await page.clock.runFor(900);
  await expectStatus(page, "RECONNECTING");
  expect(backend.state.sockets).toHaveLength(1);
  await page.clock.runFor(100);
  await expect.poll(() => backend.state.sockets.length).toBe(2);
  await expectStatus(page, "CONNECTED", GREEN);
  expect(await activeTimers(page)).toBe(1);
});

test("a reconnected socket must earn green and gets a single fresh watchdog", async ({ page }) => {
  await openConnected(page);
  backend.state.silent = true;
  backend.disconnectLatest();
  await expectStatus(page, "RECONNECTING");
  await page.clock.runFor(1000);
  await expect.poll(() => backend.state.sockets.length).toBe(2);
  await expectStatus(page, AWAITING);
  expect(await activeTimers(page)).toBe(1);
  await page.clock.runFor(DEADLINE_MS - 1);
  await expectStatus(page, AWAITING);
  await page.clock.runFor(1);
  await expectStatus(page, STALE);
  expect(await activeTimers(page)).toBe(0);
  await deliverSnapshot(page);
  await expectStatus(page, "CONNECTED", GREEN);
  expect(backend.state.sockets).toHaveLength(2);
});

test("an opened socket is not green before its first valid frame", async ({ page }) => {
  await openDashboard(page, (synthetic) => { synthetic.state.silent = true; });
  await expectStatus(page, AWAITING);
  await expect(page.locator("#mode-label")).toHaveCSS("color", AMBER);
  await freezeClock(page);
  await page.clock.runFor(DEADLINE_MS);
  await expectStatus(page, STALE);
  await deliverSnapshot(page);
  await expectStatus(page, "CONNECTED", GREEN);
});

test("the deadline follows the slowest configured refresh interval", async ({ page }) => {
  await openConnected(page, (synthetic) => { synthetic.state.settings.refresh_interval = 30; });
  await page.clock.runFor(94999);
  await expectStatus(page, "CONNECTED", GREEN);
  await page.clock.runFor(1);
  await expectStatus(page, STALE);
});

test("the deadline never drops below its floor at the fastest interval", async ({ page }) => {
  await openConnected(page, (synthetic) => { synthetic.state.settings.refresh_interval = 1; });
  await page.clock.runFor(9999);
  await expectStatus(page, "CONNECTED", GREEN);
  await page.clock.runFor(1);
  await expectStatus(page, STALE);
});

test("failed settings fall back to the slowest interval instead of a false stale", async ({ page }) => {
  await openConnected(page, (synthetic) => { synthetic.failNext("GET", "/api/settings"); });
  await page.clock.runFor(94999);
  await expectStatus(page, "CONNECTED", GREEN);
  await page.clock.runFor(1);
  await expectStatus(page, STALE);
  expect(consoleProblems).toEqual([
    "error: Failed to load resource: the server responded with a status of 503 (Service Unavailable)",
  ]);
  consoleProblems.length = 0;
});

test("a saved interval applies from the next valid frame, not while the old one is pending", async ({ page }) => {
  await openConnected(page, (synthetic) => { synthetic.state.settings.refresh_interval = 30; });
  await page.locator('[data-view="settings"]').click();
  await page.locator('#settings-form [name="refresh_interval"]').fill("2");
  await page.locator("#settings-form").getByRole("button", { name: "SAVE SETTINGS" }).click();
  await expect(page.locator("#settings-status")).toHaveText("SAVED LOCALLY");
  // Past the new 11 s deadline, while the 30 s interval is still pending.
  await page.clock.runFor(25000);
  await expectStatus(page, "CONNECTED", GREEN);
  await deliverSnapshot(page);
  await page.clock.runFor(DEADLINE_MS - 1);
  await expectStatus(page, "CONNECTED", GREEN);
  await page.clock.runFor(1);
  await expectStatus(page, STALE);
});

test("Demo Mode shows no live status and runs no socket or timer", async ({ page }) => {
  await openConnected(page);
  await page.locator('[data-view="settings"]').click();
  await page.locator("#enter-demo").click();
  await expect(page.locator("#demo-banner")).toBeVisible();
  await expectStatus(page, "DEMO DATA");
  await expect(page.locator("#mode-label")).toHaveCSS("color", AMBER);
  await expect(page.locator("#monitor-status")).toHaveCSS("color", AMBER);
  await expect(page.locator(".live-wave")).toHaveCSS("color", AMBER);
  await expect.poll(() => backend.state.closedSockets).toBe(1);
  expect(await activeTimers(page)).toBe(0);
  await page.clock.runFor(300000);
  await expectStatus(page, "DEMO DATA");
  expect(backend.state.sockets).toHaveLength(1);
  expect(await activeTimers(page)).toBe(0);

  await page.locator("#exit-demo").click();
  await expect.poll(() => backend.state.sockets.length).toBe(2);
  await expectStatus(page, "CONNECTED", GREEN);
  await expect(page.locator("#mode-label")).toHaveText("LIVE");
  await expect(page.locator("#mode-label")).toHaveCSS("color", GREEN);
  expect(await activeTimers(page)).toBe(1);
});

test("entering Demo Mode while stale leaves no stale label behind", async ({ page }) => {
  await openConnected(page);
  await page.clock.runFor(DEADLINE_MS);
  await expectStatus(page, STALE);
  await page.locator('[data-view="settings"]').click();
  await page.locator("#enter-demo").click();
  await expectStatus(page, "DEMO DATA");
  await expect(socketStatus(page)).not.toHaveAttribute("title", /./);
});

test("entering Demo Mode cancels a pending reconnect", async ({ page }) => {
  await openConnected(page);
  backend.disconnectLatest();
  await expectStatus(page, "RECONNECTING");
  expect(await activeTimers(page)).toBe(1);
  await page.locator('[data-view="settings"]').click();
  await page.locator("#enter-demo").click();
  await expectStatus(page, "DEMO DATA");
  expect(await activeTimers(page)).toBe(0);
  await page.clock.runFor(60000);
  expect(backend.state.sockets).toHaveLength(1);
  await expectStatus(page, "DEMO DATA");
});

test("Demo Mode entered during the first load never opens a socket", async ({ page }) => {
  let release;
  const held = new Promise((resolve) => { release = resolve; });
  await page.clock.install();
  await page.addInitScript(trackSocketTimers);
  backend = await installSyntheticBackend(page);
  await page.route(/\/api\/events\?/, async (route) => { await held; await route.fallback(); });
  await page.goto("/");
  await page.locator('[data-view="settings"]').click();
  await page.locator("#enter-demo").click();
  await expect(page.locator("#demo-banner")).toBeVisible();
  release();
  await expect.poll(() => backend.state.requests.includes("GET /api/events")).toBe(true);
  await page.clock.runFor(1000);
  expect(backend.state.sockets).toHaveLength(0);
  expect(await activeTimers(page)).toBe(0);
  await expectStatus(page, "DEMO DATA");
});

test("a replaced socket cannot write over the newer status", async ({ page }) => {
  await openConnected(page);
  await page.locator('[data-view="settings"]').click();
  await page.locator("#enter-demo").click();
  await page.locator("#exit-demo").click();
  await expect.poll(() => backend.state.sockets.length).toBe(2);
  await expectStatus(page, "CONNECTED", GREEN);
  await expect.poll(() => backend.state.closedSockets).toBe(1);
  await expectStatus(page, "CONNECTED", GREEN);
  expect(await activeTimers(page)).toBe(1);
});

test("stale status is exposed to assistive technology", async ({ page }) => {
  await openConnected(page);
  await expect(socketStatus(page)).toHaveRole("status");
  await expect(socketStatus(page)).toHaveAttribute("aria-live", "polite");
  await page.clock.runFor(DEADLINE_MS);
  await expect(page.getByRole("status").filter({ hasText: STALE })).toBeVisible();
});

test("@mobile stale status stays visible and non-green on a phone", async ({ page }) => {
  await openConnected(page);
  await page.clock.runFor(DEADLINE_MS);
  await expectStatus(page, STALE);
  await expect(socketStatus(page)).toBeVisible();
  await expect(socketStatus(page)).toBeInViewport({ ratio: 1 });
  await expect(page.getByRole("status").filter({ hasText: STALE })).toBeVisible();
  await deliverSnapshot(page);
  await expectStatus(page, "CONNECTED", GREEN);
  expect(backend.state.sockets).toHaveLength(1);
});

test("the timeout formula allows for the real refresh cadence", async () => {
  const { staleTimeoutMs } = await import("../frontend/js/websocket.js");
  expect(staleTimeoutMs(1)).toBe(10000);
  expect(staleTimeoutMs(1.5)).toBe(10000);
  expect(staleTimeoutMs(2)).toBe(11000);
  expect(staleTimeoutMs(5)).toBe(20000);
  expect(staleTimeoutMs(30)).toBe(95000);
  for (const unknown of [undefined, null, NaN, Infinity, 0, -2, 31, "2"]) {
    expect(staleTimeoutMs(unknown)).toBe(95000);
  }
});

test("the shape check accepts the demo fixture and rejects partial snapshots", async () => {
  const { isTelemetrySnapshot } = await import("../frontend/js/websocket.js");
  const snapshot = createDemoData().snapshot;
  expect(isTelemetrySnapshot(snapshot)).toBe(true);
  for (const field of ["system", "network", "connections", "processes", "alerts", "sequence"]) {
    expect(isTelemetrySnapshot({ ...snapshot, [field]: undefined })).toBe(false);
  }
  expect(isTelemetrySnapshot({ ...snapshot, sequence: "1" })).toBe(false);
  expect(isTelemetrySnapshot({ ...snapshot, type: "telemetry_degraded" })).toBe(false);
  expect(isTelemetrySnapshot({ ...snapshot, system: [] })).toBe(false);
  expect(isTelemetrySnapshot({ ...snapshot, system: { ...snapshot.system, timestamp: undefined } })).toBe(false);
  expect(isTelemetrySnapshot(null)).toBe(false);
  expect(isTelemetrySnapshot([])).toBe(false);
});
