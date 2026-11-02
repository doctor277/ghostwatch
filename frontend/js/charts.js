const colors = {
  green: "#29f49a",
  cyan: "#24d9ff",
  purple: "#b858ff",
  grid: "rgba(111, 154, 186, .12)",
  muted: "#6e8aa1",
};

function fitCanvas(canvas) {
  const rect = canvas.getBoundingClientRect();
  const ratio = window.devicePixelRatio || 1;
  const width = Math.max(1, Math.floor(rect.width * ratio));
  const height = Math.max(1, Math.floor(rect.height * ratio));
  if (canvas.width !== width || canvas.height !== height) {
    canvas.width = width;
    canvas.height = height;
  }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  return { ctx, width: rect.width, height: rect.height };
}

function linePath(ctx, values, bounds, max, color, fill = false) {
  if (!values.length) return;
  const { left, top, width, height } = bounds;
  ctx.beginPath();
  let drawing = false;
  values.forEach((value, index) => {
    if (!Number.isFinite(value)) {
      drawing = false;
      return;
    }
    const x = left + (index / Math.max(values.length - 1, 1)) * width;
    const y = top + height - (Math.max(0, value) / max) * height;
    drawing ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
    drawing = true;
  });
  ctx.strokeStyle = color;
  ctx.lineWidth = 1.8;
  ctx.lineJoin = "round";
  ctx.stroke();
  if (fill && values.every(Number.isFinite)) {
    const gradient = ctx.createLinearGradient(0, top, 0, top + height);
    gradient.addColorStop(0, `${color}38`);
    gradient.addColorStop(1, `${color}00`);
    ctx.lineTo(left + width, top + height);
    ctx.lineTo(left, top + height);
    ctx.closePath();
    ctx.fillStyle = gradient;
    ctx.fill();
  }
}

export function drawSparkline(canvas, values, color = colors.green) {
  if (!canvas) return;
  const { ctx, width, height } = fitCanvas(canvas);
  ctx.clearRect(0, 0, width, height);
  const max = Math.max(...values, 1) * 1.1;
  linePath(ctx, values, { left: 1, top: 3, width: width - 2, height: height - 6 }, max, color, true);
}

export function drawBandwidthChart(canvas, history) {
  if (!canvas) return;
  const { ctx, width, height } = fitCanvas(canvas);
  ctx.clearRect(0, 0, width, height);
  const margin = { left: 48, right: 14, top: 16, bottom: 30 };
  const bounds = { left: margin.left, top: margin.top, width: width - margin.left - margin.right, height: height - margin.top - margin.bottom };
  const download = history.map((item) => Number.isFinite(item.download_mbps) ? item.download_mbps : null);
  const upload = history.map((item) => Number.isFinite(item.upload_mbps) ? item.upload_mbps : null);
  const available = [...download, ...upload].filter(Number.isFinite);
  const max = Math.max(...available, 0.5) * 1.15;
  ctx.font = "10px ui-monospace, monospace";
  ctx.fillStyle = colors.muted;
  ctx.textAlign = "right";
  for (let i = 0; i <= 4; i += 1) {
    const y = bounds.top + (i / 4) * bounds.height;
    ctx.beginPath(); ctx.moveTo(bounds.left, y); ctx.lineTo(bounds.left + bounds.width, y);
    ctx.strokeStyle = colors.grid; ctx.lineWidth = 1; ctx.stroke();
    ctx.fillText(`${(max * (1 - i / 4)).toFixed(1)}`, bounds.left - 8, y + 3);
  }
  for (let i = 0; i <= 8; i += 1) {
    const x = bounds.left + (i / 8) * bounds.width;
    ctx.beginPath(); ctx.moveTo(x, bounds.top); ctx.lineTo(x, bounds.top + bounds.height);
    ctx.strokeStyle = colors.grid; ctx.stroke();
  }
  linePath(ctx, download, bounds, max, colors.cyan, true);
  linePath(ctx, upload, bounds, max, colors.purple, false);
  ctx.textAlign = "left";
  ctx.fillText("MB/s", 8, 14);
  if (history.length) {
    ctx.fillText(new Date(history[0].timestamp).toLocaleTimeString([], { minute: "2-digit", second: "2-digit" }), bounds.left, height - 8);
    ctx.textAlign = "right";
    ctx.fillText(new Date(history.at(-1).timestamp).toLocaleTimeString([], { minute: "2-digit", second: "2-digit" }), width - margin.right, height - 8);
  }
}

export function drawSystemChart(canvas, history) {
  if (!canvas) return;
  const { ctx, width, height } = fitCanvas(canvas);
  ctx.clearRect(0, 0, width, height);
  const bounds = { left: 46, top: 16, width: width - 60, height: height - 46 };
  ctx.font = "10px ui-monospace, monospace";
  ctx.fillStyle = colors.muted;
  ctx.textAlign = "right";
  for (let i = 0; i <= 4; i += 1) {
    const y = bounds.top + (i / 4) * bounds.height;
    ctx.beginPath(); ctx.moveTo(bounds.left, y); ctx.lineTo(bounds.left + bounds.width, y);
    ctx.strokeStyle = colors.grid; ctx.stroke();
    ctx.fillText(`${100 - i * 25}%`, bounds.left - 8, y + 3);
  }
  linePath(ctx, history.map((item) => Number.isFinite(item.cpu_percent) ? item.cpu_percent : null), bounds, 100, colors.green, true);
  linePath(ctx, history.map((item) => Number.isFinite(item.memory_percent) ? item.memory_percent : null), bounds, 100, colors.purple, false);
  ctx.textAlign = "left"; ctx.fillStyle = colors.green; ctx.fillText("CPU", bounds.left, height - 8);
  ctx.fillStyle = colors.purple; ctx.fillText("MEMORY", bounds.left + 42, height - 8);
}

export function drawConnectionHistory(canvas, history) {
  if (!canvas) return;
  const { ctx, width, height } = fitCanvas(canvas);
  ctx.clearRect(0, 0, width, height);
  const bounds = { left: 46, top: 16, width: width - 60, height: height - 46 };
  const active = history.map((item) => Number.isFinite(item.active_connections) ? item.active_connections : null);
  const established = history.map((item) => Number.isFinite(item.established_connections) ? item.established_connections : null);
  const max = Math.max(...[...active, ...established].filter(Number.isFinite), 1) * 1.1;
  ctx.font = "10px ui-monospace, monospace";
  ctx.fillStyle = colors.muted;
  ctx.textAlign = "right";
  for (let i = 0; i <= 4; i += 1) {
    const y = bounds.top + (i / 4) * bounds.height;
    ctx.beginPath(); ctx.moveTo(bounds.left, y); ctx.lineTo(bounds.left + bounds.width, y);
    ctx.strokeStyle = colors.grid; ctx.stroke();
    ctx.fillText(String(Math.round(max * (1 - i / 4))), bounds.left - 8, y + 3);
  }
  linePath(ctx, active, bounds, max, colors.green, true);
  linePath(ctx, established, bounds, max, colors.cyan, false);
  ctx.textAlign = "left"; ctx.fillStyle = colors.green; ctx.fillText("ACTIVE", bounds.left, height - 8);
  ctx.fillStyle = colors.cyan; ctx.fillText("ESTABLISHED", bounds.left + 58, height - 8);
}

export { colors };
