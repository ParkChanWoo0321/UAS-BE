const angle = 47 * Math.PI / 180;
const sine = Math.sin(angle);
const cosine = Math.cos(angle);
const mono = '"IBM Plex Mono", ui-monospace, "Liberation Mono", monospace';
const ui = '"Noto Sans KR", "Noto Sans CJK KR", "Malgun Gothic", sans-serif';
const decimal = new Intl.NumberFormat("ko-KR", { maximumFractionDigits: 1 });
const scientific = new Intl.NumberFormat("ko-KR", { notation: "scientific", maximumFractionDigits: 2 });
const number = (value) => (Math.abs(value) < 1e6 ? decimal : scientific).format(value);
const clamp = (value, low, high) => Math.max(low, Math.min(high, value));

function rotated([east, north]) {
  return [east * cosine + north * sine, east * sine - north * cosine];
}

function projection(width, height, target, track) {
  let left = 0, right = 0, top = 0, bottom = 0;
  for (const point of (target ? [target, ...track] : track)) {
    const [x, y] = rotated(point);
    left = Math.min(left, x);
    right = Math.max(right, x);
    top = Math.min(top, y);
    bottom = Math.max(bottom, y);
  }
  const margin = width < 450 ? 22 : 47;
  const scale = Math.min(
    .404 * Math.min(width / 978, height / 452),
    (width - margin * 2) / Math.max(1, right - left),
    (height - margin * 2) / Math.max(1, bottom - top),
  );
  const originX = clamp(width * 82 / 978, margin - left * scale, width - margin - right * scale);
  const originY = clamp(height * 335 / 452, margin - top * scale, height - margin - bottom * scale);
  return {
    scale,
    map(point) {
      const [x, y] = rotated(point);
      return [originX + x * scale, originY + y * scale];
    },
  };
}

function line(ctx, points, color, width, dash = []) {
  if (points.length < 2) return;
  ctx.beginPath();
  ctx.strokeStyle = color;
  ctx.lineWidth = width;
  ctx.lineCap = "round";
  ctx.lineJoin = "round";
  ctx.setLineDash(dash);
  ctx.moveTo(...points[0]);
  for (const point of points.slice(1)) ctx.lineTo(...point);
  ctx.stroke();
  ctx.setLineDash([]);
}

function grid(ctx, width, height) {
  ctx.beginPath();
  for (let x = 2; x < width; x += 76) {
    ctx.moveTo(x + .5, 0);
    ctx.lineTo(x + .5, height);
  }
  for (let y = 2; y < height; y += 73) {
    ctx.moveTo(0, y + .5);
    ctx.lineTo(width, y + .5);
  }
  ctx.lineWidth = 1;
  ctx.strokeStyle = "#fcf6db20";
  ctx.stroke();
}

function marker(ctx, [x, y], radius, color, kind, heading = 0) {
  ctx.beginPath();
  ctx.arc(x, y, radius, 0, Math.PI * 2);
  ctx.fillStyle = "#153b35";
  ctx.fill();
  ctx.lineWidth = 1.5;
  ctx.strokeStyle = color;
  ctx.stroke();
  ctx.save();
  ctx.translate(x, y);
  ctx.beginPath();
  if (kind === "home") {
    ctx.arc(0, 0, 4, 0, Math.PI * 2);
  } else if (kind === "target") {
    ctx.moveTo(0, -5);
    ctx.lineTo(5, 0);
    ctx.lineTo(0, 5);
    ctx.lineTo(-5, 0);
  } else {
    ctx.rotate(heading + Math.PI / 2);
    ctx.moveTo(0, -7);
    ctx.lineTo(5.5, 5);
    ctx.lineTo(0, 2);
    ctx.lineTo(-5.5, 5);
  }
  ctx.closePath();
  ctx.fillStyle = color;
  ctx.fill();
  ctx.restore();
}

function tag(ctx, width, height, point, name, detail, tone, position) {
  ctx.font = "500 11px " + mono;
  const size = Math.max(position.min ?? 100, ctx.measureText(detail).width + 20);
  const x = clamp(point[0] + position.x, 5, width - size - 5);
  const y = clamp(point[1] + position.y, 5, height - 51);
  ctx.beginPath();
  ctx.roundRect(x, y, size, 46, 7);
  ctx.fillStyle = "#153b3b";
  ctx.fill();
  ctx.lineWidth = 1;
  ctx.strokeStyle = "#51746a";
  ctx.stroke();
  ctx.fillStyle = tone;
  ctx.font = "bold 12px " + ui;
  ctx.fillText(name, x + 10, y + 17);
  ctx.font = "500 11px " + mono;
  ctx.fillStyle = "#f1e6cf";
  ctx.fillText(detail, x + 10, y + 33);
}

function ruler(ctx, height, scale) {
  const requested = 100 / scale;
  const order = 10 ** Math.floor(Math.log10(requested));
  const unit = [1, 2, 2.5, 5, 10].map((v) => v * order)
    .reduce((a, b) => Math.abs(a - requested) < Math.abs(b - requested) ? a : b);
  const length = unit * scale;
  const x = 25, y = height - 28;
  line(ctx, [[x, y + 4], [x, y], [x + length, y], [x + length, y + 4]], "#fff3d4", 1.5);
  ctx.font = "500 11px " + mono;
  const label = unit >= 1000 ? number(unit / 1000) + " km" : number(unit) + " m";
  ctx.beginPath();
  ctx.roundRect(x - 5, y - 28, ctx.measureText(label).width + 16, 20, 4);
  ctx.fillStyle = "#153b3b";
  ctx.fill();
  ctx.fillStyle = "#fff3d4";
  ctx.fillText(label, x + 1, y - 14);
}

export function drawFlightPlot(canvas, target, track, { current, returning }) {
  const width = canvas.clientWidth, height = canvas.clientHeight;
  if (width < 56 || height < 56) return;
  const ratio = window.devicePixelRatio || 1;
  const pixelWidth = Math.round(width * ratio), pixelHeight = Math.round(height * ratio);
  if (canvas.width !== pixelWidth || canvas.height !== pixelHeight) {
    canvas.width = pixelWidth;
    canvas.height = pixelHeight;
  }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  ctx.clearRect(0, 0, width, height);
  const { map, scale } = projection(width, height, target, track);
  const home = map([0, 0]), goal = target ? map(target) : null;
  const path = track.map(map);

  grid(ctx, width, height);
  if (goal) {
    line(ctx, [home, goal], "#122f2e99", 6);
    line(ctx, [home, goal], "#fff0ca", 2.8, [6, 8]);
  }
  line(ctx, path, "#16382f", 7);
  line(ctx, path, "#b5f0ce", 3.5);
  if (current && returning) {
    const route = [map(current), home];
    line(ctx, route, "#2d3027", 6);
    line(ctx, route, "#ffb28b", 3, [7, 6]);
  }

  marker(ctx, home, 12, "#fff3d4", "home");
  tag(ctx, width, height, home, "출발점", "N 0 / E 0", "#fff3d4", { x: 19, y: 5, min: 104 });
  if (goal) {
    marker(ctx, goal, 16, "#ffb48b", "target");
    tag(ctx, width, height, goal, "목표점", "N " + number(target[1]) + " / E " + number(target[0]), "#ffb48b", { x: 27, y: -33, min: 134 });
  }
  if (current) {
    const position = map(current);
    const previous = path.at(-2);
    const heading = previous ? Math.atan2(position[1] - previous[1], position[0] - previous[0]) : -.4;
    marker(ctx, position, 14, "#c6f4d8", "current", heading);
    tag(ctx, width, height, position, "기체 위치", number(current[1]) + " / " + number(current[0]) + " m", "#cbf5d8", { x: -62, y: 18, min: 127 });
  }
  ruler(ctx, height, scale);
}
