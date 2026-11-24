import "./app.js?v=coastline";

const views = {
  operations: "해양 임무 관제",
  captures: "순찰 촬영",
  training: "선박 감지 학습",
};
let loadTraining;
let loadCaptures;
let previousView;

function navigate() {
  const route = location.hash.slice(1);
  if (route === "workspace" && previousView !== undefined) return;
  const view = Object.hasOwn(views, route) ? route : "operations";
  for (const id of Object.keys(views)) {
    document.getElementById(id).hidden = view !== id;
    const tab = document.getElementById(id + "-tab");
    if (view === id) tab.setAttribute("aria-current", "page");
    else tab.removeAttribute("aria-current");
  }
  document.title = "Coastline · " + views[view];

  if (view === "training") {
    loadTraining ??= import("./training.js?v=coastline-model").catch(() => {
      loadTraining = undefined;
      document.getElementById("dataset-status").textContent = "학습 화면을 불러오지 못했습니다. 페이지를 새로고침해 주세요.";
    });
  }
  if (view === "captures") {
    loadCaptures ??= import("./captures.js").catch(() => {
      loadCaptures = undefined;
      document.getElementById("capture-status").textContent = "촬영 기록 화면을 불러오지 못했습니다.";
    });
  }
  requestAnimationFrame(() => {
    if (route === "mission" || route === "history") {
      document.getElementById(route).scrollIntoView();
    } else if (previousView !== view && route !== "workspace") {
      window.scrollTo(0, 0);
    }
    previousView = view;
  });
}

window.addEventListener("hashchange", navigate);
navigate();
