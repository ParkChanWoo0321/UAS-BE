const endpoint = "/api/missions";
const assets = "/static/coastline/";
const format = new Intl.NumberFormat("ko-KR", { maximumFractionDigits: 1 });

async function requestMission(path, { method = "GET", data } = {}) {
  const headers = method === "GET" ? {} : { "X-Maritime-Control": "1" };
  const request = { cache: "no-store", method, headers };
  if (data) {
    headers["Content-Type"] = "application/json";
    request.body = JSON.stringify(data);
  }
  const response = await fetch(path, request);
  if (response.status === 204) return;
  const body = await response.json();
  if (!response.ok) {
    const error = new Error(body.detail || "임무 설정 요청에 실패했어요.");
    error.status = response.status;
    throw error;
  }
  return body;
}

function icon(name) {
  const image = document.createElement("img");
  image.src = assets + name + ".svg";
  image.alt = "";
  return image;
}

export function createMissionControls({ readTarget, applyTarget, resetTarget }) {
  const byId = (name) => document.getElementById("mission-" + name);
  const list = byId("list");
  const name = byId("name");
  const save = byId("save");
  const fresh = byId("new");
  const empty = byId("empty");
  const retry = byId("retry");
  const feedback = byId("feedback");
  const lockNote = byId("lock-note");
  const dialog = byId("delete-dialog");
  const description = byId("delete-description");
  let missions = [];
  let selectedId = null;
  let deleteId = null;
  let ready = false;
  let pending = false;
  let locked = false;

  function canEdit() {
    return ready && !pending && !locked;
  }

  function show(message = "", isError = false) {
    feedback.textContent = message;
    feedback.classList.toggle("error", isError);
    feedback.hidden = !message;
  }

  function makeRow(mission) {
    const selected = selectedId === mission.id;
    const row = document.createElement("li");
    row.className = "mission-item";
    const entry = document.createElement("button");
    entry.type = "button";
    entry.className = "mission-entry";
    entry.disabled = !canEdit();
    entry.dataset.id = mission.id;
    entry.setAttribute("aria-pressed", String(selected));
    const copy = document.createElement("span");
    const title = document.createElement("strong");
    title.textContent = mission.name;
    const values = document.createElement("code");
    values.textContent = "N " + format.format(mission.north) + " / E " + format.format(mission.east)
      + " m · " + format.format(mission.altitude) + " m";
    copy.append(title, values);
    entry.append(icon("folder"), copy);
    entry.addEventListener("click", async () => {
      if (!canEdit()) return;
      await operate(async () => {
        const item = await requestMission(endpoint + "/" + mission.id);
        if (locked) return;
        selectedId = item.id;
        applyTarget(item);
        name.value = item.name;
        show("임무를 불러왔습니다.");
      });
      list.querySelector('[data-id="' + mission.id + '"]')?.focus();
    });
    row.append(entry);
    if (selected && !locked) {
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "mission-delete danger";
      remove.disabled = !canEdit();
      remove.setAttribute("aria-label", mission.name + " 삭제");
      remove.append(icon("trash"), document.createTextNode("삭제"));
      remove.addEventListener("click", () => {
        deleteId = mission.id;
        dialog.returnValue = "";
        description.textContent = "‘" + mission.name + "’ 임무를 삭제할까요?";
        dialog.showModal();
      });
      row.append(remove);
    }
    return row;
  }

  function render() {
    const scroll = list.scrollTop;
    list.replaceChildren(...missions.map(makeRow));
    list.scrollTop = scroll;
    empty.hidden = missions.length > 0;
    empty.textContent = ready ? "저장된 임무가 없습니다." : "저장된 임무를 확인하고 있습니다.";
    name.disabled = !canEdit();
    save.disabled = !canEdit() || !name.value.trim();
    fresh.disabled = !canEdit();
    fresh.hidden = locked;
    lockNote.hidden = !locked;
    retry.disabled = pending;
  }

  async function refreshList() {
    missions = await requestMission(endpoint);
    if (!missions.some((mission) => mission.id === selectedId)) selectedId = null;
    ready = true;
    retry.hidden = true;
  }

  async function operate(action) {
    if (pending) return;
    pending = true;
    show();
    render();
    try {
      await action();
    } catch (error) {
      if (error.status === 404) {
        try { await refreshList(); } catch { retry.hidden = false; }
      }
      if (!ready) retry.hidden = false;
      show(error instanceof TypeError || error instanceof SyntaxError
        ? "임무 설정을 가져오지 못했어요. 서버 연결을 확인해 주세요."
        : error.message, true);
    } finally {
      pending = false;
      render();
    }
  }

  name.addEventListener("input", () => { save.disabled = !canEdit() || !name.value.trim(); });
  save.addEventListener("click", () => {
    if (!canEdit() || !name.value.trim()) return;
    const dataName = name.value.trim();
    const id = selectedId;
    operate(async () => {
      const saved = await requestMission(id ? endpoint + "/" + id : endpoint, {
        method: id ? "PATCH" : "POST",
        data: { ...readTarget(), name: dataName },
      });
      selectedId = saved.id;
      missions = id ? missions.map((item) => item.id === saved.id ? saved : item) : [saved, ...missions];
      name.value = saved.name;
      show("임무를 저장했습니다.");
    });
  });
  fresh.addEventListener("click", () => {
    if (!canEdit()) return;
    selectedId = null;
    name.value = "";
    resetTarget();
    show();
    render();
    name.focus();
  });
  retry.addEventListener("click", () => operate(refreshList));
  dialog.addEventListener("close", async () => {
    if (dialog.returnValue !== "delete" || !canEdit()) return;
    const id = deleteId;
    await operate(async () => {
      await requestMission(endpoint + "/" + id, { method: "DELETE" });
      missions = missions.filter((item) => item.id !== id);
      if (selectedId === id) {
        selectedId = null;
        name.value = "";
      }
      show("임무를 삭제했습니다.");
    });
    fresh.focus();
  });
  operate(refreshList);
  return {
    lockInputs(value) {
      if (locked === value) return;
      locked = value;
      if (locked && dialog.open) dialog.close();
      render();
    },
  };
}
