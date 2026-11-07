const MIN_REFRESH_SECONDS = 1;
const MAX_REFRESH_SECONDS = 30;
const STALE_FLOOR_MS = 10000;
const STALE_MARGIN_MS = 5000;

// The service waits a full interval and then collects, so frames arrive at least one
// interval apart. Three intervals plus a margin allow for collection time and scheduling
// delay. An unknown interval uses the slowest one: a late warning beats a false one.
export function staleTimeoutMs(refreshSeconds) {
  const known = Number.isFinite(refreshSeconds)
    && refreshSeconds >= MIN_REFRESH_SECONDS && refreshSeconds <= MAX_REFRESH_SECONDS;
  const interval = known ? refreshSeconds : MAX_REFRESH_SECONDS;
  return Math.max(STALE_FLOOR_MS, interval * 3000 + STALE_MARGIN_MS);
}

const isRecord = (value) => typeof value === "object" && value !== null && !Array.isArray(value);

// The required fields of TelemetrySnapshot in app/models.py.
export function isTelemetrySnapshot(message) {
  return isRecord(message)
    && message.type === "telemetry"
    && Number.isInteger(message.sequence)
    && isRecord(message.system)
    && typeof message.system.timestamp === "string"
    && isRecord(message.network)
    && Array.isArray(message.connections)
    && Array.isArray(message.processes)
    && Array.isArray(message.alerts);
}

export class TelemetrySocket {
  constructor(onData, onStatus, getRefreshInterval = () => undefined) {
    this.onData = onData;
    this.onStatus = onStatus;
    this.getRefreshInterval = getRefreshInterval;
    this.socket = null;
    this.retry = 0;
    this.timer = null;
    this.status = null;
    this.staleTimer = null;
    this.staleAfter = staleTimeoutMs();
    this.freshAt = 0;
  }

  setStatus(status) {
    if (status === this.status) return;
    this.status = status;
    this.onStatus(status);
  }

  startStaleTimer(delay) {
    clearTimeout(this.staleTimer);
    this.staleTimer = setTimeout(() => this.checkStale(), delay);
  }

  stopStaleTimer() {
    clearTimeout(this.staleTimer);
    this.staleTimer = null;
  }

  markFresh() {
    this.freshAt = performance.now();
    // The interval is read only here, so a saved value cannot shorten the wait for a
    // frame that the old interval is still due to deliver.
    const staleAfter = staleTimeoutMs(this.getRefreshInterval());
    if (this.staleTimer === null || staleAfter !== this.staleAfter) this.startStaleTimer(staleAfter);
    this.staleAfter = staleAfter;
    this.setStatus("CONNECTED");
  }

  // Frames only move freshAt. The one pending timer rechecks the elapsed time, which also
  // keeps a timer delayed in a throttled background tab from misreporting.
  checkStale() {
    this.staleTimer = null;
    const remaining = this.staleAfter - (performance.now() - this.freshAt);
    if (remaining > 0) {
      this.startStaleTimer(remaining);
    } else if (this.status === "CONNECTED" || this.status === "AWAITING TELEMETRY") {
      this.setStatus("TELEMETRY STALE");
    }
  }

  connect() {
    const protocol = location.protocol === "https:" ? "wss:" : "ws:";
    this.setStatus("CONNECTING");
    const socket = new WebSocket(`${protocol}//${location.host}/ws/telemetry`);
    this.socket = socket;
    socket.addEventListener("open", () => {
      if (socket !== this.socket) return;
      this.retry = 0;
      this.freshAt = performance.now();
      this.staleAfter = staleTimeoutMs(this.getRefreshInterval());
      this.startStaleTimer(this.staleAfter);
      this.setStatus("AWAITING TELEMETRY");
    });
    socket.addEventListener("message", (event) => {
      if (socket !== this.socket) return;
      try {
        const message = JSON.parse(event.data);
        if (message.type === "telemetry_degraded") {
          this.setStatus("TELEMETRY DEGRADED");
          return;
        }
        if (!isTelemetrySnapshot(message)) throw new TypeError("Not a telemetry snapshot");
        this.onData(message);
        this.markFresh();
      } catch {
        this.setStatus("DATA ERROR");
      }
    });
    socket.addEventListener("close", () => {
      if (socket !== this.socket) return;
      this.stopStaleTimer();
      this.setStatus("RECONNECTING");
      const delay = Math.min(1000 * (2 ** this.retry), 15000);
      this.retry += 1;
      this.timer = setTimeout(() => this.connect(), delay);
    });
    socket.addEventListener("error", () => socket.close());
  }

  disconnect() {
    clearTimeout(this.timer);
    this.stopStaleTimer();
    const socket = this.socket;
    this.socket = null;
    socket?.close();
  }
}
