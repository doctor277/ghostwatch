export class TelemetrySocket {
  constructor(onData, onStatus) {
    this.onData = onData;
    this.onStatus = onStatus;
    this.socket = null;
    this.retry = 0;
    this.timer = null;
    this.closed = false;
    this.degraded = false;
  }

  setDegraded(status) {
    this.degraded = true;
    this.onStatus(status);
  }

  connect() {
    const protocol = location.protocol === "https:" ? "wss:" : "ws:";
    this.onStatus("CONNECTING");
    this.socket = new WebSocket(`${protocol}//${location.host}/ws/telemetry`);
    this.socket.addEventListener("open", () => {
      this.retry = 0;
      this.degraded = false;
      this.onStatus("CONNECTED");
    });
    this.socket.addEventListener("message", (event) => {
      try {
        const message = JSON.parse(event.data);
        if (message.type === "telemetry_degraded") {
          this.setDegraded("TELEMETRY DEGRADED");
          return;
        }
        this.onData(message);
        if (this.degraded) {
          this.degraded = false;
          this.onStatus("CONNECTED");
        }
      } catch {
        this.setDegraded("DATA ERROR");
      }
    });
    this.socket.addEventListener("close", () => {
      this.onStatus("RECONNECTING");
      if (!this.closed) {
        const delay = Math.min(1000 * (2 ** this.retry), 15000);
        this.retry += 1;
        this.timer = setTimeout(() => this.connect(), delay);
      }
    });
    this.socket.addEventListener("error", () => this.socket.close());
  }

  disconnect() {
    this.closed = true;
    clearTimeout(this.timer);
    this.socket?.close();
  }
}
