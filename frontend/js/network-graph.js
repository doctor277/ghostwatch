const PALETTE = {
  local: "#29f49a",
  process: "#24d9ff",
  remote: "#b858ff",
  edge: "rgba(62, 132, 166, .36)",
  text: "#b9d1df",
  muted: "#68879a",
};

function fit(canvas) {
  const rect = canvas.getBoundingClientRect();
  const ratio = window.devicePixelRatio || 1;
  canvas.width = Math.max(1, Math.floor(rect.width * ratio));
  canvas.height = Math.max(1, Math.floor(rect.height * ratio));
  const context = canvas.getContext("2d");
  context.setTransform(ratio, 0, 0, ratio, 0, 0);
  return { context, width: rect.width, height: rect.height };
}

export class NetworkGraph {
  constructor(canvas, onSelect) {
    this.canvas = canvas;
    this.onSelect = onSelect;
    this.connections = [];
    this.nodes = [];
    this.edges = [];
    this.scale = 1;
    this.offsetX = 0;
    this.offsetY = 0;
    this.dragging = false;
    this.last = null;
    this.selected = null;
    this.search = "";
    this.protocol = "ALL";
    this.bind();
  }

  bind() {
    this.canvas.addEventListener("pointerdown", (event) => {
      this.dragging = true;
      this.last = { x: event.clientX, y: event.clientY };
      this.canvas.setPointerCapture(event.pointerId);
    });
    this.canvas.addEventListener("pointermove", (event) => {
      if (!this.dragging || !this.last) return;
      this.offsetX += event.clientX - this.last.x;
      this.offsetY += event.clientY - this.last.y;
      this.last = { x: event.clientX, y: event.clientY };
      this.draw();
    });
    this.canvas.addEventListener("pointerup", (event) => {
      const moved = this.last
        ? Math.abs(event.clientX - this.last.x) + Math.abs(event.clientY - this.last.y)
        : 0;
      this.dragging = false;
      this.last = null;
      if (moved < 3) this.selectAt(event.offsetX, event.offsetY);
    });
    this.canvas.addEventListener("wheel", (event) => {
      event.preventDefault();
      this.scale = Math.min(2.4, Math.max(.55, this.scale * (event.deltaY > 0 ? .9 : 1.1)));
      this.draw();
    }, { passive: false });
    this.canvas.addEventListener("keydown", (event) => {
      if (event.key === "+" || event.key === "=") this.scale = Math.min(2.4, this.scale * 1.1);
      else if (event.key === "-") this.scale = Math.max(.55, this.scale * .9);
      else if (event.key === "0") this.reset();
      else return;
      event.preventDefault();
      this.draw();
    });
  }

  reset() {
    this.scale = 1;
    this.offsetX = 0;
    this.offsetY = 0;
    this.draw();
  }

  setFilters(search, protocol) {
    this.search = search.trim().toLowerCase();
    this.protocol = protocol;
    this.build();
  }

  setConnections(connections) {
    this.connections = connections || [];
    this.build();
  }

  build() {
    const filtered = this.connections.filter((item) =>
      (this.protocol === "ALL" || item.protocol === this.protocol)
      && (!this.search || [item.process_name, item.remote_address, item.pid]
        .some((value) => String(value ?? "").toLowerCase().includes(this.search)))
    );
    const processCounts = new Map();
    filtered.forEach((item) => processCounts.set(item.process_name, (processCounts.get(item.process_name) || 0) + 1));
    const allowedProcesses = new Set([...processCounts.entries()].sort((a, b) => b[1] - a[1]).slice(0, 60).map(([name]) => name));
    const limited = filtered.filter((item) => allowedProcesses.has(item.process_name) && item.remote_address !== "—").slice(0, 240);
    const processNames = [...new Set(limited.map((item) => item.process_name))];
    const remoteNames = [...new Set(limited.map((item) => item.remote_address))].slice(0, 180);
    const local = { id: "local", label: "LOCAL COMPUTER", kind: "local", x: 0, y: 0, radius: 17 };
    const processes = processNames.map((label, index) => {
      const angle = (index / Math.max(processNames.length, 1)) * Math.PI * 2;
      return { id: `process:${label}`, label, kind: "process", x: Math.cos(angle) * 170, y: Math.sin(angle) * 170, radius: 10 };
    });
    const remotes = remoteNames.map((label, index) => {
      const angle = (index / Math.max(remoteNames.length, 1)) * Math.PI * 2 + .12;
      const radius = 300 + (index % 3) * 26;
      return { id: `remote:${label}`, label, kind: "remote", x: Math.cos(angle) * radius, y: Math.sin(angle) * radius, radius: 7 };
    });
    const byId = new Map([local, ...processes, ...remotes].map((node) => [node.id, node]));
    const edgeIds = new Set();
    const edges = [];
    processes.forEach((node) => edges.push({ from: local, to: node, status: "LOCAL" }));
    limited.forEach((item) => {
      const from = byId.get(`process:${item.process_name}`);
      const to = byId.get(`remote:${item.remote_address}`);
      const id = `${from?.id}|${to?.id}`;
      if (from && to && !edgeIds.has(id)) {
        edgeIds.add(id);
        edges.push({ from, to, status: item.status });
      }
    });
    this.nodes = [local, ...processes, ...remotes];
    this.edges = edges;
    this.draw();
  }

  screen(node, width, height) {
    return {
      x: width / 2 + this.offsetX + node.x * this.scale,
      y: height / 2 + this.offsetY + node.y * this.scale,
    };
  }

  selectAt(x, y) {
    const rect = this.canvas.getBoundingClientRect();
    const node = [...this.nodes].reverse().find((candidate) => {
      const point = this.screen(candidate, rect.width, rect.height);
      return Math.hypot(point.x - x, point.y - y) <= Math.max(12, candidate.radius * this.scale + 5);
    });
    this.selected = node || null;
    this.draw();
    if (node) this.onSelect(node);
  }

  draw() {
    const { context: ctx, width, height } = fit(this.canvas);
    ctx.clearRect(0, 0, width, height);
    ctx.save();
    this.edges.forEach((edge) => {
      const from = this.screen(edge.from, width, height);
      const to = this.screen(edge.to, width, height);
      ctx.beginPath();
      ctx.moveTo(from.x, from.y);
      ctx.lineTo(to.x, to.y);
      ctx.strokeStyle = edge.status === "ESTABLISHED" ? "rgba(41,244,154,.32)" : PALETTE.edge;
      ctx.lineWidth = edge.from.kind === "local" ? 1.2 : .8;
      ctx.stroke();
    });
    this.nodes.forEach((node) => {
      const point = this.screen(node, width, height);
      const color = PALETTE[node.kind];
      ctx.beginPath();
      ctx.arc(point.x, point.y, node.radius * Math.min(this.scale, 1.4), 0, Math.PI * 2);
      ctx.fillStyle = node === this.selected ? "#ffffff" : color;
      ctx.shadowColor = color;
      ctx.shadowBlur = node === this.selected ? 20 : 8;
      ctx.fill();
      ctx.shadowBlur = 0;
      if (this.scale > .7 || node.kind === "local") {
        ctx.fillStyle = node === this.selected ? "#ffffff" : PALETTE.text;
        ctx.font = `${node.kind === "local" ? 10 : 9}px ui-monospace, monospace`;
        ctx.textAlign = "center";
        ctx.fillText(node.label.slice(0, 28), point.x, point.y + node.radius + 14);
      }
    });
    ctx.restore();
  }
}
