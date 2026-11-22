import { createMissionControls } from "./missions.js";

const byId = (id) => document.getElementById(id);
const form = byId("mission-form");
const inputs = ["north", "east", "altitude"].map(byId);
const canvas = byId("track");
const track = [];
const phases = {
  launching: "시작 중", waiting: "PX4 연결 대기", arming: "시동 요청", takeoff: "멀티콥터 비행",
  transition: "기체 전환 중", cruise: "고정익 순항", patrol: "순찰 구역", returning: "귀환 중",
  landed: "착륙 완료", disarmed: "시동 해제", failsafe: "페일세이프",
  signal_lost: "신호 끊김", stopping: "종료 중", stopped: "종료", failed: "실행 실패",
  interrupted: "연결 종료",
};
const modes = { 0: "수동", 1: "고도 제어", 2: "위치 제어", 3: "자동 임무", 4: "대기 비행",
  5: "RTL", 14: "Offboard", 17: "이륙", 18: "착륙" };
const format = new Intl.NumberFormat("ko-KR", { maximumFractionDigits: 1 });
const dateFormat = new Intl.DateTimeFormat("ko-KR", {
  timeZone: "Asia/Seoul", dateStyle: "short", timeStyle: "medium",
});
let state = null;
let trackedId = null;
let busy = false;
let revision = 0;
let networkError = false;
let actionError = "";

function readTarget() {
  const target = Object.fromEntries(inputs.map((input) => [input.name, input.valueAsNumber]));
  if (Object.values(target).some((value) => !Number.isFinite(value)) || target.altitude <= 0.1) {
    throw new Error("좌표를 숫자로 입력하고 고도는 0.1m보다 크게 입력해 주세요.");
  }
  return target;
}

const missionControls = createMissionControls(byId("mission-library"), {
  readTarget,
  applyTarget(mission) {
    inputs.forEach((input) => { input.value = mission[input.name]; });
    draw();
  },
});

function meters(value) {
  return Number.isFinite(value) ? `${format.format(value)} m` : "—";
}

function badge(element, text, tone) {
  if (element.dataset.value === text && element.dataset.tone === tone) return;
  element.dataset.value = text;
  element.dataset.tone = tone;
  element.classList.toggle("good", tone === "good");
  element.classList.toggle("bad", tone === "bad");
  element.replaceChildren(document.createElement("span"), document.createTextNode(text));
}

function render() {
  const run = state?.run;
  const active = Boolean(run?.active);
  const telemetry = active && run.connected ? run.telemetry : null;
  if (track.length && run?.id !== trackedId) track.length = 0;
  if (run && run.id !== trackedId) {
    trackedId = run.id;
    if (active) inputs.forEach((input) => { input.value = run[input.name]; });
  }
  if (Number.isFinite(telemetry?.north) && Number.isFinite(telemetry?.east)) {
    const last = track.at(-1);
    if (!last || Math.hypot(last[0] - telemetry.east, last[1] - telemetry.north) >= 0.8) {
      track.push([telemetry.east, telemetry.north]);
      if (track.length > 1600) track.splice(1, 1);
    }
  }

  badge(byId("db-status"), state?.database ? "MySQL 연결됨" : (state ? "MySQL 연결 필요" : "MySQL 확인 중"),
    state?.database ? "good" : (state ? "bad" : ""));
  badge(byId("connection"), telemetry ? "PX4 연결됨" : (active ? "PX4 응답 대기" : "PX4 대기 중"),
    telemetry ? "good" : "");

  const label = run ? phases[run.phase] || run.phase : "대기 중";
  const phase = byId("phase");
  if (phase.textContent !== label) phase.textContent = label;
  phase.classList.toggle("good", ["takeoff", "transition", "cruise", "patrol", "returning", "landed"].includes(run?.phase));
  phase.classList.toggle("bad", ["failsafe", "signal_lost", "failed", "interrupted"].includes(run?.phase));
  byId("run-id").hidden = !run;
  if (run) byId("run-id").textContent = `#${run.id}`;

  inputs.forEach((input) => { input.disabled = active || busy; });
  missionControls.lockInputs(active || busy);
  byId("start").disabled = !state?.database || !state?.launcher || state.external_run || active || busy || networkError;
  byId("return").disabled = !run?.can_return || busy || networkError;
  byId("stop").disabled = !active || busy || networkError || run.phase === "stopping";

  const message = actionError || (networkError ? "웹 앱 연결을 다시 시도하고 있어요." : "")
    || run?.message || (run?.return_pending || telemetry?.rtl_requested && run.phase !== "returning"
      && run.phase !== "landed" ? "PX4 귀환 응답을 기다리고 있어요." : "")
    || (state?.external_run ? "일반 터미널에서 실행한 시뮬레이션이 이미 켜져 있어요." : "")
    || (state && !state.launcher ? "maritime 실행 도구를 찾을 수 없어요." : "")
    || (state && !state.database ? "로컬 MySQL 연결을 확인해 주세요." : "");
  byId("message").textContent = message;
  byId("message").hidden = !message;
  byId("position").textContent = telemetry?.position_valid
    ? `${format.format(telemetry.north)} / ${format.format(telemetry.east)} m` : "—";
  byId("height").textContent = meters(telemetry?.altitude);
  byId("distance").textContent = meters(telemetry?.target_distance);
  byId("mode").textContent = !Number.isInteger(telemetry?.nav) ? "—"
    : [modes[telemetry.nav] || `PX4 ${telemetry.nav}`,
      telemetry.fixed_wing ? "고정익" : telemetry.armed && !telemetry.transitioning ? "멀티콥터" : ""]
      .filter(Boolean).join(" · ");

  const history = state?.history || [];
  byId("history-scroll").hidden = !history.length;
  byId("history-empty").hidden = Boolean(history.length);
  byId("history-empty").textContent = !state ? "실행 이력을 불러오고 있어요."
    : !state.database ? "MySQL에 연결되면 실행 이력을 볼 수 있어요." : "아직 저장된 실행 이력이 없어요.";
  byId("history-rows").replaceChildren(...history.map((item) => {
    const row = document.createElement("tr");
    for (const text of [
      dateFormat.format(new Date(item.started_at)), meters(item.north), meters(item.east),
      meters(item.altitude), phases[item.phase] || item.phase,
    ]) {
      const cell = row.insertCell();
      cell.textContent = text;
    }
    return row;
  }));
  draw();
}

function draw() {
  const width = canvas.clientWidth;
  const height = canvas.clientHeight;
  if (!width || !height) return;
  const pixels = window.devicePixelRatio || 1;
  if (canvas.width !== Math.round(width * pixels) || canvas.height !== Math.round(height * pixels)) {
    canvas.width = Math.round(width * pixels);
    canvas.height = Math.round(height * pixels);
  }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(pixels, 0, 0, pixels, 0, 0);
  ctx.clearRect(0, 0, width, height);
  const run = state?.run;
  const north = run?.active ? run.north : byId("north").valueAsNumber;
  const east = run?.active ? run.east : byId("east").valueAsNumber;
  const target = [Number.isFinite(east) ? east : 0, Number.isFinite(north) ? north : 0];
  const points = [[0, 0], target, ...track];
  const x = points.map((point) => point[0]);
  const y = points.map((point) => point[1]);
  const spanX = Math.max(100, Math.max(...x) - Math.min(...x));
  const spanY = Math.max(100, Math.max(...y) - Math.min(...y));
  const scale = Math.min((width - 95) / spanX, (height - 65) / spanY);
  const originX = width / 2 - (Math.max(...x) + Math.min(...x)) / 2 * scale;
  const originY = height / 2 + (Math.max(...y) + Math.min(...y)) / 2 * scale;
  const px = (meters) => originX + meters * scale;
  const py = (meters) => originY - meters * scale;
  const baseStep = 10 ** Math.floor(Math.log10(70 / scale));
  const step = [1, 2, 5, 10].map((n) => n * baseStep).find((n) => n * scale >= 65);
  const gridStep = step || 10 * baseStep;
  ctx.lineWidth = 1;
  ctx.font = "12px system-ui, sans-serif";
  ctx.strokeStyle = "#213847";
  ctx.fillStyle = "#95acbf";
  const formatGrid = new Intl.NumberFormat("ko-KR", { notation: "compact", maximumFractionDigits: 1 });
  for (let value = Math.ceil((-originX / scale) / gridStep) * gridStep, index = 0;
    px(value) <= width && index < 20; value += gridStep, index++) {
    const at = px(value);
    ctx.beginPath(); ctx.moveTo(at, 0); ctx.lineTo(at, height); ctx.stroke();
    ctx.fillText(formatGrid.format(value), at + 4, height - 8);
  }
  for (let value = Math.ceil(((originY - height) / scale) / gridStep) * gridStep, index = 0;
    py(value) >= 0 && index < 20; value += gridStep, index++) {
    const at = py(value);
    ctx.beginPath(); ctx.moveTo(0, at); ctx.lineTo(width, at); ctx.stroke();
    ctx.fillText(formatGrid.format(value), 6, at - 4);
  }
  ctx.fillStyle = "#b5c7d5";
  ctx.fillText("북쪽 ↑", width - 54, 19);
  ctx.fillText("동쪽 →", width - 53, height - 8);
  if (track.length > 1) {
    ctx.beginPath();
    ctx.moveTo(px(track[0][0]), py(track[0][1]));
    for (const [e, n] of track.slice(1)) ctx.lineTo(px(e), py(n));
    ctx.strokeStyle = "#68dfcf"; ctx.lineWidth = 2.2; ctx.stroke();
  }
  ctx.beginPath();
  ctx.arc(px(0), py(0), 5, 0, Math.PI * 2);
  ctx.lineWidth = 2; ctx.strokeStyle = "#d8e9f7"; ctx.stroke();
  const tx = px(target[0]), ty = py(target[1]);
  ctx.beginPath();
  ctx.moveTo(tx, ty - 7); ctx.lineTo(tx + 7, ty);
  ctx.lineTo(tx, ty + 7); ctx.lineTo(tx - 7, ty); ctx.closePath();
  ctx.fillStyle = "#efbf76"; ctx.fill();
  if (track.length) {
    const [e, n] = track.at(-1);
    ctx.beginPath(); ctx.arc(px(e), py(n), 5, 0, Math.PI * 2);
    ctx.fillStyle = "#68dfcf"; ctx.fill();
    ctx.strokeStyle = "#103d42"; ctx.stroke();
  }
}

async function refresh() {
  const request = ++revision;
  try {
    const response = await fetch("/api/status", { cache: "no-store" });
    if (!response.ok) throw new Error("HTTP status failed");
    const payload = await response.json();
    if (request !== revision) return;
    state = payload;
    networkError = false;
  } catch {
    if (request !== revision) return;
    networkError = true;
  }
  render();
}

async function post(path, payload) {
  if (busy) return;
  busy = true;
  actionError = "";
  render();
  try {
    const response = await fetch(path, {
      method: "POST", headers: { "Content-Type": "application/json", "X-Maritime-Control": "1" },
      body: JSON.stringify(payload || {}),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(typeof result.detail === "string" ? result.detail : "좌표와 고도를 확인해 주세요.");
  } catch (error) {
    actionError = error.message || "시뮬레이션 요청에 실패했어요.";
  } finally {
    busy = false;
    await refresh();
  }
}

form.addEventListener("submit", (event) => {
  event.preventDefault();
  try {
    post("/api/runs", readTarget());
  } catch (error) {
    actionError = error.message;
    render();
  }
});
form.addEventListener("input", draw);
byId("return").addEventListener("click", () => { if (state?.run) post(`/api/runs/${state.run.id}/return`); });
byId("stop").addEventListener("click", () => { if (state?.run) post(`/api/runs/${state.run.id}/stop`); });
new ResizeObserver(draw).observe(canvas);

async function poll() {
  await refresh();
  window.setTimeout(poll, 1100);
}
poll();
