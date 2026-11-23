import { drawFlightPlot } from "./chart.js?v=coastline";
import { createMissionControls } from "./missions.js?v=coastline";

const byId = (id) => document.getElementById(id);
const form = byId("mission-form");
const inputs = ["north", "east", "altitude"].map(byId);
const canvas = byId("track");
const track = [];
const steps = Array.from(document.querySelectorAll("[data-step]"));
const observed = new Set();
let completedHistory = "";
const phases = {
  launching: "시작 중", waiting: "PX4 연결 대기", arming: "시동 요청", takeoff: "이륙 중",
  transition: "고정익 전환 중", cruise: "고정익 순항 중", patrol: "순찰 중", returning: "귀환 중",
  landed: "착륙 완료", disarmed: "시동 해제", failsafe: "페일세이프",
  signal_lost: "신호 끊김", status_delayed: "비행 상태 갱신 대기",
  stopping: "종료 중", stopped: "종료", failed: "실행 실패",
  interrupted: "연결 종료",
};
const goodPhases = new Set(["takeoff", "transition", "cruise", "patrol", "returning", "landed"]);
const badPhases = new Set(["failsafe", "signal_lost", "failed", "interrupted"]);
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

const missionControls = createMissionControls({
  readTarget,
  applyTarget(mission) {
    inputs.forEach((input) => { input.value = mission[input.name]; });
    draw();
  },
  resetTarget() {
    inputs.forEach((input) => { input.value = input.defaultValue; });
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

function currentPosition(telemetry) {
  if (!telemetry?.position_valid) return null;
  const point = [telemetry.east, telemetry.north];
  return point.every(Number.isFinite) ? point : null;
}

function renderHistory(history) {
  byId("history-scroll").hidden = !history.length;
  byId("history-empty").hidden = Boolean(history.length);
  byId("history-empty").textContent = !state ? "실행 이력을 불러오고 있어요."
    : !state.database ? "MySQL에 연결되면 실행 이력을 볼 수 있어요." : "시뮬레이션을 실행하면 여기에 기록됩니다.";
  const value = JSON.stringify(history);
  if (value === completedHistory) return;
  completedHistory = value;
  byId("history-rows").replaceChildren(...history.map((item) => {
    const row = document.createElement("tr");
    const date = new Date(item.started_at);
    for (const text of [
      "#" + item.id, "N " + meters(item.north) + " / E " + meters(item.east), meters(item.altitude),
    ]) row.insertCell().textContent = text;
    const status = document.createElement("span");
    status.className = "history-phase";
    status.textContent = phases[item.phase] || item.phase;
    status.classList.toggle("good", goodPhases.has(item.phase));
    status.classList.toggle("bad", badPhases.has(item.phase));
    row.insertCell().append(status);
    row.insertCell().textContent = Number.isFinite(date.getTime()) ? dateFormat.format(date) : "—";
    return row;
  }));
}

function renderSteps(run) {
  const phase = run?.phase;
  const current = steps.findIndex((step) => step.dataset.step === phase);
  if (current !== -1) observed.add(phase);
  steps.forEach((step, index) => {
    const isCurrent = current === index;
    const done = !isCurrent && observed.has(step.dataset.step);
    const label = isCurrent ? (phase === "landed" ? "완료" : "진행 중")
      : done ? "완료" : index < current ? "미확인" : "대기";
    const icon = (done || phase === "landed" && isCurrent) ? "step-done" : isCurrent ? "step-current" : "step-wait";
    step.classList.toggle("current", isCurrent);
    const image = step.querySelector("img");
    const source = "/static/coastline/" + icon + ".svg";
    if (image.getAttribute("src") !== source) image.src = source;
    step.querySelector(".step-status").textContent = label;
  });
}

function render() {
  const run = state?.run;
  const active = Boolean(run?.active);
  const telemetry = active && run.connected ? run.telemetry : null;
  if (track.length && run?.id !== trackedId) track.length = 0;
  if (run && run.id !== trackedId) {
    trackedId = run.id;
    observed.clear();
    if (active) inputs.forEach((input) => { input.value = run[input.name]; });
  }
  const point = currentPosition(telemetry);
  if (point) {
    const last = track.at(-1);
    if (!last || Math.hypot(last[0] - point[0], last[1] - point[1]) >= 0.8) {
      track.push(point);
      if (track.length > 1600) track.splice(1, 1);
    }
  }

  badge(byId("db-status"), state?.database ? "MySQL 연결됨" : (state ? "MySQL 연결 필요" : "MySQL 확인 중"),
    state?.database ? "good" : (state ? "bad" : ""));
  badge(byId("connection"), telemetry ? "PX4 연결됨" : (active ? "PX4 응답 대기" : "PX4 연결 대기"),
    telemetry ? "good" : "");

  const phase = run?.phase;
  const returning = active && phase === "returning";
  const pendingReturn = active && !returning && phase !== "landed"
    && Boolean(run?.return_pending || telemetry?.rtl_requested);
  const label = active || badPhases.has(phase) ? phases[phase] || phase : "실행 대기";
  badge(byId("phase"), label, goodPhases.has(phase) && !returning && active ? "good" : badPhases.has(phase) ? "bad" : "");
  byId("operation-intro").textContent = returning ? "지도에서 출발점까지의 귀환 경로를 확인하세요."
    : active ? "비행 상태와 기체의 이동 경로를 확인하세요." : "출발점과 목표점 사이의 예정 경로를 확인하세요.";
  byId("mission-plan").hidden = active;
  byId("mission-run").hidden = !active;
  if (active) {
    byId("run-id").textContent = "#" + run.id;
    byId("active-target").textContent = "N " + meters(run.north) + " / E " + meters(run.east);
    byId("active-altitude").textContent = meters(run.altitude);
    renderSteps(run);
  }

  inputs.forEach((input) => { input.disabled = active || busy; });
  missionControls.lockInputs(active || busy);
  byId("start").disabled = !state?.database || !state?.launcher || state.external_run || active || busy || networkError;
  byId("return").hidden = !active || !run?.can_return || returning || pendingReturn;
  byId("return").disabled = !run?.can_return || busy || networkError;
  const returnStatus = byId("return-status");
  returnStatus.hidden = !returning && !pendingReturn;
  returnStatus.textContent = returning ? "출발점으로 돌아가는 중입니다." : "PX4의 귀환 응답을 기다리고 있습니다.";
  byId("stop").disabled = !active || busy || networkError || run.phase === "stopping";

  const message = actionError || (networkError ? "웹 앱 연결을 다시 시도하고 있어요." : "")
    || run?.message
    || (state?.external_run ? "일반 터미널에서 실행한 시뮬레이션이 이미 켜져 있어요." : "")
    || (state && !state.launcher ? "maritime 실행 도구를 찾을 수 없어요." : "")
    || (state && !state.database ? "로컬 MySQL 연결을 확인해 주세요." : "");
  byId("message").textContent = message;
  byId("message").hidden = !message;
  byId("position").textContent = point ? format.format(point[1]) + " / " + format.format(point[0]) : "—";
  byId("height").textContent = meters(telemetry?.altitude);
  byId("distance").textContent = meters(telemetry?.target_distance);
  byId("mode").textContent = !Number.isInteger(telemetry?.nav) ? "—"
    : modes[telemetry.nav] || "PX4 " + telemetry.nav;
  byId("mode-hint").textContent = telemetry?.fixed_wing ? "고정익"
    : telemetry?.armed && !telemetry.transitioning ? "멀티콥터" : telemetry ? "PX4" : "수신 대기";
  renderHistory(state?.history || []);
  draw();
}

function draw() {
  const run = state?.run;
  const north = run?.active ? run.north : byId("north").valueAsNumber;
  const east = run?.active ? run.east : byId("east").valueAsNumber;
  const limit = 3.402823466e38;
  const target = [east, north].every((value) => Math.abs(value) <= limit) ? [east, north] : null;
  const current = currentPosition(run?.active && run.connected ? run.telemetry : null);
  const returning = run?.phase === "returning" && current !== null;
  const distance = target ? Math.hypot(...target) : NaN;
  byId("planned-distance").textContent = distance >= 1000 ? (distance / 1000).toFixed(2) + " km" : meters(distance);
  byId("track-key").hidden = track.length < 2;
  byId("return-key").hidden = !returning;
  for (const input of inputs) input.style.setProperty("--number-width", Math.max(2.2, input.value.length + .3) + "ch");
  drawFlightPlot(canvas, target, track, { current, returning });
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
  if (byId("start").disabled) return;
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
