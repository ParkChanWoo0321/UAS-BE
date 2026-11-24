const byId = (id) => document.getElementById(id);
const section = byId("captures");
const status = byId("capture-status");
const gallery = byId("capture-list");
const collectionStatus = byId("capture-collection-status");
const toggle = byId("capture-toggle");
const dialog = byId("capture-dialog");
const player = byId("capture-player");
const download = byId("capture-download");
const date = new Intl.DateTimeFormat("ko-KR", {
  timeZone: "Asia/Seoul", dateStyle: "medium", timeStyle: "medium",
});
const measurement = new Intl.NumberFormat("ko-KR", { maximumFractionDigits: 1 });
const perPage = 12;
let page = 0;
let total = 0;
let revision = 0;
let rendered = "";
let collectionEnabled;
let changing = false;
let collectionError = false;

function element(tag, value) {
  const node = document.createElement(tag);
  if (value !== undefined) node.textContent = value;
  return node;
}

function renderCollection() {
  toggle.disabled = collectionEnabled === undefined || changing;
  if (collectionEnabled !== undefined) {
    toggle.textContent = collectionEnabled ? "수집 일시정지" : "수집 시작";
    toggle.classList.toggle("primary", !collectionEnabled);
  }
  const message = changing ? "수집 설정을 저장하는 중입니다."
    : collectionError ? "수집 상태를 변경하지 못했습니다."
    : collectionEnabled === undefined ? "수집 상태를 확인하는 중입니다."
    : collectionEnabled ? "자동 수집 켜짐 · 순찰 중 감지한 선박을 저장합니다." : "자동 수집 일시정지";
  if (collectionStatus.textContent !== message) collectionStatus.textContent = message;
}

function card(item) {
  const path = "/api/captures/" + item.run_id + "/" + item.id;
  const shot = element("li");
  shot.className = "capture-card panel";
  const photo = element("a");
  photo.href = path + "/photo";
  photo.target = "_blank";
  photo.rel = "noopener";
  photo.setAttribute("aria-label", "실행 #" + item.run_id + " 촬영 사진 원본 보기");
  const image = element("img");
  image.src = photo.href;
  image.alt = "기체 카메라에서 촬영한 선박";
  image.loading = "lazy";
  photo.append(image);
  const content = element("div");
  content.className = "capture-info";
  const heading = element("h2", "선박 " + item.boats + "척");
  const timestamp = element("time", date.format(new Date(item.captured_at)));
  timestamp.dateTime = item.captured_at;
  const detection = item.confidence > 0
    ? "YOLO 신뢰도 " + measurement.format(item.confidence * 100) + "%"
    : "시뮬레이션 영상 분석";
  const meta = element("p", "실행 #" + item.run_id + " · " + detection);
  const position = element("p", "기체 위치 N " + measurement.format(item.position.north)
    + " / E " + measurement.format(item.position.east) + " m · 고도 " + measurement.format(item.position.altitude) + " m");
  meta.className = position.className = "note";
  content.append(heading, timestamp, meta, position);
  if (item.has_video) {
    const watch = element("button", "영상 보기");
    watch.type = "button";
    watch.addEventListener("click", () => {
      player.src = path + "/video";
      download.href = player.src;
      download.download = "patrol-" + item.run_id + "-" + item.id + ".webm";
      dialog.showModal();
    });
    content.append(watch);
  }
  shot.append(photo, content);
  return shot;
}

async function refresh() {
  if (section.hidden) return;
  const sequence = ++revision;
  try {
    const response = await fetch("/api/captures?limit=" + perPage + "&offset=" + (page * perPage), { cache: "no-store" });
    if (!response.ok) throw new Error("Could not load capture records");
    const result = await response.json();
    if (sequence !== revision) return;
    if (!changing) {
      collectionEnabled = result.collection_enabled;
      renderCollection();
    }
    total = result.total;
    const pages = Math.max(1, Math.ceil(total / perPage));
    if (page >= pages) {
      page = pages - 1;
      return refresh();
    }
    const key = JSON.stringify(result.items);
    if (key !== rendered) {
      gallery.replaceChildren(...result.items.map(card));
      rendered = key;
    }
    const message = total ? "촬영 기록 " + total + "건" : "아직 저장된 촬영 기록이 없습니다.";
    if (status.textContent !== message) status.textContent = message;
    byId("capture-pagination").hidden = pages <= 1;
    byId("capture-page").textContent = (page + 1) + " / " + pages;
    byId("capture-previous").disabled = page === 0;
    byId("capture-next").disabled = page + 1 >= pages;
  } catch {
    if (sequence === revision) status.textContent = "MySQL에 연결하지 못했습니다. 잠시 후 다시 시도합니다.";
  }
}

toggle.addEventListener("click", async () => {
  if (changing || collectionEnabled === undefined) return;
  changing = true;
  collectionError = false;
  renderCollection();
  try {
    const response = await fetch("/api/captures/collection", {
      method: "PATCH",
      headers: { "Content-Type": "application/json", "X-Maritime-Control": "1" },
      body: JSON.stringify({ enabled: !collectionEnabled }),
    });
    if (!response.ok) throw new Error("Could not update collection status");
    collectionEnabled = (await response.json()).collection_enabled;
  } catch {
    collectionError = true;
  } finally {
    changing = false;
    renderCollection();
    refresh();
  }
});

byId("capture-previous").addEventListener("click", () => { if (page > 0) { page--; refresh(); } });
byId("capture-next").addEventListener("click", () => { if ((page + 1) * perPage < total) { page++; refresh(); } });
dialog.addEventListener("close", () => {
  player.pause();
  player.removeAttribute("src");
  player.load();
});
window.addEventListener("hashchange", refresh);
window.setInterval(refresh, 5000);
refresh();
