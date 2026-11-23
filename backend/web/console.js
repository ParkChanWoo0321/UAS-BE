import "./app.js?v=coastline";

const operations = document.getElementById("operations");
const training = document.getElementById("training");
const operationsTab = document.getElementById("operations-tab");
const trainingTab = document.getElementById("training-tab");
let loadTraining;
let previousView;

function navigate() {
  const route = location.hash.slice(1);
  const showTraining = route === "training";
  if (route === "workspace" && previousView !== undefined) return;
  operations.hidden = showTraining;
  training.hidden = !showTraining;
  const activeTab = showTraining ? trainingTab : operationsTab;
  const inactiveTab = showTraining ? operationsTab : trainingTab;
  activeTab.setAttribute("aria-current", "page");
  inactiveTab.removeAttribute("aria-current");
  document.title = showTraining ? "Coastline · 선박 감지 학습" : "Coastline · 해양 임무 관제";

  if (showTraining) {
    loadTraining ??= import("./training.js?v=coastline-model").catch(() => {
      loadTraining = undefined;
      document.getElementById("dataset-status").textContent = "학습 화면을 불러오지 못했습니다. 페이지를 새로고침해 주세요.";
    });
  }
  requestAnimationFrame(() => {
    if (!showTraining && (route === "mission" || route === "history")) {
      document.getElementById(route).scrollIntoView();
    } else if (previousView !== showTraining && route !== "workspace") {
      window.scrollTo(0, 0);
    }
    previousView = showTraining;
  });
}

window.addEventListener("hashchange", navigate);
navigate();
