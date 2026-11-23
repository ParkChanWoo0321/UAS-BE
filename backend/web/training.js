const byId = (id) => document.getElementById(id);
const api = "/api/training";
const control = { "X-Maritime-Control": "1" };
const pageSize = 24;
const supported = /\.(jpe?g|png|webp|bmp|tiff?|mp4|mov|m4v|avi|webm|mkv)$/i;
let page = 0;
let view = "all";
let samples = [];
let selected = null;
let total = 0;
let positive = 0;
let boxes = [];
let drawing = null;
let saving = false;
let uploading = false;
let failures = [];
let listVersion = 0;
let ready = false;
let starting = false;
let job = null;
let model = null;
let invalidatedModels = 0;
let jobVersion = 0;
let removing = false;
let sourceToRemove = null;
let pollTimer;

async function request(path, method = "GET", data) {
  const options = { method, cache: "no-store" };
  if (method !== "GET") options.headers = { ...control };
  if (data !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(data);
  }
  const response = await fetch(api + path, options);
  if (!response.ok) {
    const details = await response.json().catch(() => ({}));
    throw new Error(typeof details.detail === "string" ? details.detail : "요청을 처리하지 못했습니다.");
  }
  return response.json();
}

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function isActive() {
  return job?.status === "queued" || job?.status === "running";
}

function updateStart() {
  const start = byId("start-training");
  start.disabled = !ready || isActive() || starting || uploading || removing || positive < 2;
  start.textContent = model?.needs_retraining ? "남은 자료로 재학습" : "학습 시작";
}

function sendFile(file) {
  return new Promise((resolve, reject) => {
    const upload = new XMLHttpRequest();
    upload.open("POST", api + "/media?name=" + encodeURIComponent(file.name));
    upload.setRequestHeader("X-Maritime-Control", "1");
    upload.responseType = "json";
    upload.timeout = 420000;
    upload.onload = () => {
      if (upload.status === 201 && upload.response) resolve(upload.response);
      else reject(new Error(upload.response?.detail || "파일을 올리지 못했습니다."));
    };
    upload.onerror = () => reject(new Error("웹 콘솔과 연결할 수 없습니다."));
    upload.ontimeout = () => reject(new Error("파일 업로드 시간이 초과됐습니다."));
    upload.send(file);
  });
}

function renderFailures() {
  const list = byId("upload-errors");
  list.replaceChildren(...failures.map(({ file, message }) => element("li", file.name + " · " + message)));
  list.hidden = failures.length === 0;
  byId("retry-upload").hidden = failures.length === 0 || uploading;
}

async function uploadBatch(input) {
  if (uploading) return;
  const files = Array.from(input);
  if (!files.length) return;
  uploading = true;
  updateStart();
  failures = [];
  renderFailures();
  const status = byId("upload-status");
  const progress = byId("upload-progress");
  const filePicker = byId("files");
  const folderPicker = byId("folder");
  filePicker.disabled = folderPicker.disabled = true;
  progress.max = files.length;
  progress.value = 0;
  progress.hidden = false;
  let next = 0;
  let completed = 0;
  let added = 0;
  let duplicates = 0;
  let ignored = 0;
  status.textContent = "0 / " + files.length + "개 파일 처리 중";
  const process = async () => {
    while (next < files.length) {
      const file = files[next++];
      if (!supported.test(file.name)) {
        ignored++;
      } else {
        try {
          const result = await sendFile(file);
          if (result.duplicate) duplicates++;
          else added += result.samples;
        } catch (error) {
          failures.push({ file, message: error.message });
        }
      }
      completed++;
      progress.value = completed;
      status.textContent = completed + " / " + files.length + "개 파일 처리 중";
    }
  };
  try {
    await Promise.all(Array.from({ length: Math.min(4, files.length) }, process));
    const details = ["이미지 " + added + "장 추가"];
    if (duplicates) details.push("중복 " + duplicates + "개 제외");
    if (ignored) details.push("지원하지 않는 파일 " + ignored + "개 제외");
    if (failures.length) details.push(failures.length + "개 실패");
    status.textContent = details.join(" · ");
    await loadSamples();
  } finally {
    uploading = false;
    filePicker.disabled = folderPicker.disabled = false;
    renderFailures();
    updateStart();
  }
}

function imageUrl(item, thumbnail = false) {
  return api + "/samples/" + item.id + "/image" + (thumbnail ? "?thumbnail=true" : "");
}

function itemName(item) {
  return item.seconds === null ? item.name : item.name + " · " + item.seconds + "초";
}

function renderSamples() {
  const grid = byId("sample-grid");
  const empty = view === "all" ? "사진이나 영상을 올리면 여기에 표시됩니다." : "이 검토 상태에 해당하는 이미지가 없습니다.";
  byId("dataset-status").textContent = total ? total.toLocaleString("ko-KR") + "장" : "";
  byId("dataset-empty").hidden = total > 0;
  byId("dataset-empty-message").textContent = total ? "" : empty;
  grid.replaceChildren(...samples.map(item => {
    const name = itemName(item);
    const card = element("button", undefined, "sample-card");
    card.type = "button";
    card.title = name;
    const image = element("img");
    image.src = imageUrl(item, true);
    image.alt = "";
    image.loading = "lazy";
    const state = item.boxes === null ? "미검토" : item.boxes.length ? "선박 " + item.boxes.length + "척" : "선박 없음";
    card.append(image, element("span", name, "sample-title"), element("span", state, "sample-status"));
    card.addEventListener("click", () => selectSample(item, true));
    return card;
  }));
  const pages = Math.max(1, Math.ceil(total / pageSize));
  byId("pagination").hidden = pages === 1;
  byId("prev-page").disabled = page === 0;
  byId("next-page").disabled = page + 1 >= pages;
  byId("page-label").textContent = (page + 1) + " / " + pages;
}

async function loadSamples() {
  const version = ++listVersion;
  try {
    const result = await request("/samples?view=" + view + "&offset=" + (page * pageSize) + "&limit=" + pageSize);
    if (version !== listVersion) return false;
    if (page > 0 && page * pageSize >= result.count) {
      page = Math.max(0, Math.ceil(result.count / pageSize) - 1);
      return loadSamples();
    }
    samples = result.items;
    total = result.count;
    positive = result.stats.positive;
    byId("count-all").textContent = result.stats.total.toLocaleString("ko-KR");
    byId("count-pending").textContent = (result.stats.total - result.stats.reviewed).toLocaleString("ko-KR");
    byId("count-positive").textContent = positive.toLocaleString("ko-KR");
    renderSamples();
    updateStart();
    return true;
  } catch (error) {
    if (version === listVersion) {
      byId("dataset-empty").hidden = true;
      byId("dataset-status").textContent = error.message;
    }
    return false;
  }
}

const editor = byId("editor");
const image = byId("label-image");
const layer = byId("box-layer");
const sourceDialog = byId("source-delete-dialog");

function datasetFeedback(message, isError = false) {
  const feedback = byId("dataset-feedback");
  feedback.textContent = message;
  feedback.classList.toggle("error", isError);
}

function closeEditor() {
  editor.hidden = true;
  selected = drawing = null;
  boxes = [];
  image.removeAttribute("src");
}

function renderBoxes() {
  const makeShape = (box, draft) => {
    const shape = document.createElementNS("http://www.w3.org/2000/svg", "rect");
    for (const field of ["x", "y", "width", "height"]) shape.setAttribute(field, box[field] * 1000);
    if (draft) shape.classList.add("draft");
    return shape;
  };
  layer.replaceChildren(...boxes.map(box => makeShape(box, false)));
  if (drawing?.box) layer.append(makeShape(drawing.box, true));
  byId("save-labels").disabled = saving || removing || !boxes.length;
  byId("save-negative").disabled = saving || removing || !!boxes.length;
  byId("delete-source").disabled = saving || removing;
  byId("close-editor").disabled = saving || removing;
}

function renderEditor() {
  renderBoxes();
  const list = byId("box-list");
  list.replaceChildren(...boxes.map((_, i) => {
    const item = element("li", "선박 " + (i + 1));
    const remove = element("button", "박스 삭제");
    remove.type = "button";
    remove.setAttribute("aria-label", "선박 " + (i + 1) + " 박스 삭제");
    remove.disabled = saving || removing;
    remove.addEventListener("click", () => {
      boxes.splice(i, 1);
      renderEditor();
    });
    item.append(remove);
    return item;
  }));
}

function selectSample(item, scroll = false) {
  if (saving || removing) return;
  selected = item;
  drawing = null;
  boxes = (item.boxes || []).slice();
  image.src = imageUrl(item);
  byId("editor-file").textContent = itemName(item);
  byId("delete-source").textContent = item.kind === "video" ? "영상 삭제" : "사진 삭제";
  byId("label-status").textContent = "";
  editor.hidden = false;
  renderEditor();
  if (scroll) editor.scrollIntoView({ block: "start" });
}

function normalized(event) {
  const bounds = layer.getBoundingClientRect();
  const clamp = (value) => Math.min(1, Math.max(0, value));
  return {
    x: clamp((event.clientX - bounds.left) / bounds.width),
    y: clamp((event.clientY - bounds.top) / bounds.height)
  };
}

function updateDrawing(event) {
  const end = normalized(event);
  const start = drawing.start;
  drawing.box = {
    x: Math.min(start.x, end.x), y: Math.min(start.y, end.y),
    width: Math.abs(start.x - end.x), height: Math.abs(start.y - end.y)
  };
}

layer.addEventListener("pointerdown", event => {
  if (!selected || saving || removing || drawing || event.button !== 0 || !image.complete || !image.naturalWidth) return;
  if (boxes.length >= 300) {
    byId("label-status").textContent = "이미지 한 장에는 박스를 최대 300개까지 저장할 수 있습니다.";
    return;
  }
  layer.setPointerCapture(event.pointerId);
  drawing = { pointer: event.pointerId, start: normalized(event) };
  event.preventDefault();
});
layer.addEventListener("pointermove", event => {
  if (drawing?.pointer !== event.pointerId) return;
  updateDrawing(event);
  renderBoxes();
});
layer.addEventListener("pointerup", event => {
  if (drawing?.pointer !== event.pointerId) return;
  updateDrawing(event);
  const box = drawing.box;
  const bounds = layer.getBoundingClientRect();
  if (box.width * bounds.width >= 4 && box.height * bounds.height >= 4) boxes.push(box);
  drawing = null;
  renderEditor();
});
layer.addEventListener("pointercancel", event => {
  if (drawing?.pointer !== event.pointerId) return;
  drawing = null;
  renderEditor();
});

async function saveLabels(data) {
  if (!selected || saving || removing) return;
  const item = selected;
  const previous = samples.findIndex(sample => sample.id === item.id);
  const oldPage = page;
  const oldView = view;
  saving = true;
  renderEditor();
  const label = byId("label-status");
  try {
    const saved = await request("/samples/" + item.id + "/annotation", "PUT", { boxes: data });
    item.boxes = saved.boxes;
    let next;
    if (await loadSamples() && oldView === view && previous >= 0 && page === oldPage) {
      next = view === "pending" ? samples[previous] : samples.find(sample => sample.id > item.id);
      if (!next && (page + 1) * pageSize < total) {
        page++;
        if (await loadSamples()) next = samples[0];
      }
    }
    saving = false;
    if (next) selectSample(next);
    label.textContent = next ? "이전 이미지의 라벨을 저장했습니다." : "라벨을 저장했습니다.";
  } catch (error) {
    label.textContent = error.message;
  } finally {
    saving = false;
    renderEditor();
  }
}

async function deleteSource(item) {
  removing = true;
  renderEditor();
  updateStart();
  datasetFeedback("");
  try {
    const result = await request("/media/" + item.source_id, "DELETE");
    if (selected?.source_id === item.source_id) closeEditor();
    const subject = item.kind === "video" ? "영상과 프레임" : "사진";
    const messages = [subject + "을 삭제했습니다."];
    if (result.invalidated_models) messages.push("모델 " + result.invalidated_models + "개는 재학습이 필요합니다.");
    if (result.cleanup_pending) messages.push("저장 파일 정리를 다시 시도합니다.");
    datasetFeedback(messages.join(" "));
    await Promise.all([loadSamples(), refreshJob()]);
  } catch (error) {
    if (selected) byId("label-status").textContent = error.message;
    else datasetFeedback(error.message, true);
  } finally {
    removing = false;
    renderEditor();
    updateStart();
    if (editor.hidden) byId("sample-filter").focus({ preventScroll: true });
  }
}

function renderModel() {
  byId("current-model").hidden = !model;
  if (!model) return;
  const trained = model.kind === "trained";
  byId("model-name").textContent = (trained ? "학습 모델 · " : "원본 모델 · ") + model.filename;
  const tag = byId("model-tag");
  tag.textContent = model.needs_retraining ? "재학습 필요" : "사용 중";
  tag.classList.toggle("good", trained);
  byId("model-notice").textContent = model.needs_retraining ? "현재 원본 모델을 사용합니다. 남은 자료로 다시 학습할 수 있습니다."
    : invalidatedModels ? "이전 학습 결과 " + invalidatedModels + "개는 재학습이 필요합니다." : "";
  const download = byId("weights-download");
  download.hidden = !model.available;
  download.textContent = trained ? "모델 다운로드" : "원본 모델 다운로드";
}

function renderJob() {
  const status = byId("job-status");
  const message = byId("job-results");
  const progress = byId("job-progress");
  const done = job?.status === "completed";
  const stale = job?.status === "needs_retraining";
  renderModel();
  byId("artifacts").hidden = !job || job.status === "queued";
  for (const [id, kind] of [["report-download", "report"], ["log-download", "log"]]) {
    const link = byId(id);
    link.hidden = kind === "report" && !done && !stale;
    if (job) link.href = api + "/jobs/" + job.id + "/files/" + kind;
  }
  progress.hidden = !job || stale || job.status === "failed";
  if (job) {
    progress.max = job.epochs;
    progress.value = job.epoch;
  }
  const statuses = {
    queued: "학습을 준비하고 있습니다.",
    running: "학습 중 · " + job?.epoch + " / " + job?.epochs + "회",
    completed: "학습을 완료했습니다.",
    needs_retraining: "재학습 필요 · 이 학습에 사용한 자료가 삭제되었습니다.",
    failed: job?.message || "학습에 실패했습니다."
  };
  status.textContent = job ? statuses[job.status] : ready ? "CPU 학습 환경이 준비됐습니다." : "학습 환경 또는 YOLO 기본 가중치 설치가 필요합니다.";
  if (job?.summary && !stale) {
    const parts = ["학습 " + job.summary.train + "장", "검증 " + job.summary.validation + "장"];
    if (job.summary.split === "single_video") parts.push("같은 영상에서 시간순 분리");
    if (done && job.summary.metrics?.map50 !== undefined) parts.push("mAP50 " + (job.summary.metrics.map50 * 100).toFixed(1) + "%");
    message.textContent = parts.join(" · ");
  } else {
    message.textContent = "";
  }
  updateStart();
}

async function refreshJob() {
  window.clearTimeout(pollTimer);
  const version = ++jobVersion;
  try {
    const state = await request("/jobs/latest");
    if (version !== jobVersion) return;
    ready = state.media_ready && state.weights_ready;
    job = state.job;
    model = state.model;
    invalidatedModels = state.invalidated_models;
    renderJob();
  } catch (error) {
    if (version !== jobVersion) return;
    byId("job-status").textContent = error.message;
  }
  if (isActive()) pollTimer = window.setTimeout(refreshJob, 1500);
}

byId("job-form").addEventListener("submit", async event => {
  event.preventDefault();
  if (byId("start-training").disabled) return;
  const form = new FormData(event.currentTarget);
  starting = true;
  updateStart();
  try {
    job = await request("/jobs", "POST", { epochs: Number(form.get("epochs")), image_size: Number(form.get("image_size")) });
    renderJob();
    await refreshJob();
  } catch (error) {
    byId("job-status").textContent = error.message;
  } finally {
    starting = false;
    updateStart();
  }
});

for (const id of ["files", "folder"]) {
  byId(id).addEventListener("change", event => {
    uploadBatch(event.currentTarget.files);
    event.currentTarget.value = "";
  });
}
byId("folder-picker").hidden = !("webkitdirectory" in byId("folder"));
byId("retry-upload").addEventListener("click", () => uploadBatch(failures.map(entry => entry.file)));
const drop = byId("drop-zone");
drop.addEventListener("dragover", event => {
  event.preventDefault();
  if (!uploading) drop.classList.add("is-dragging");
});
drop.addEventListener("dragleave", event => {
  if (!drop.contains(event.relatedTarget)) drop.classList.remove("is-dragging");
});
drop.addEventListener("drop", event => {
  event.preventDefault();
  drop.classList.remove("is-dragging");
  uploadBatch(event.dataTransfer.files);
});

byId("close-editor").addEventListener("click", () => {
  if (!saving && !removing) closeEditor();
});
byId("delete-source").addEventListener("click", () => {
  if (!selected || saving || removing) return;
  sourceToRemove = selected;
  sourceDialog.returnValue = "";
  const name = "‘" + selected.name + "’";
  byId("source-delete-description").textContent = (selected.kind === "video"
    ? name + "에서 추출한 모든 프레임과 라벨을 삭제합니다."
    : name + " 사진과 라벨을 삭제합니다.") + " 이 자료를 사용한 모델에는 재학습이 필요합니다.";
  sourceDialog.showModal();
});
sourceDialog.addEventListener("close", () => {
  const item = sourceToRemove;
  sourceToRemove = null;
  if (sourceDialog.returnValue === "delete" && item) deleteSource(item);
});
byId("save-labels").addEventListener("click", () => saveLabels(boxes));
byId("save-negative").addEventListener("click", () => saveLabels([]));
byId("sample-filter").addEventListener("change", event => {
  page = 0;
  view = event.currentTarget.value;
  loadSamples();
});
byId("prev-page").addEventListener("click", () => { if (page > 0) { page--; loadSamples(); } });
byId("next-page").addEventListener("click", () => { if ((page + 1) * pageSize < total) { page++; loadSamples(); } });

loadSamples();
refreshJob();
window.addEventListener("focus", refreshJob);
