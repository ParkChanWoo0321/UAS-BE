const endpoint = "/api/missions";

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

export function createMissionControls(panel, { readTarget, applyTarget }) {
  const find = (name) => panel.querySelector(`#mission-${name}`);
  const select = find("select");
  const name = find("name");
  const load = find("load");
  const create = find("create");
  const update = find("update");
  const remove = find("delete");
  const retry = find("retry");
  const feedback = find("feedback");
  const dialog = document.getElementById("mission-delete-dialog");
  const deleteDescription = document.getElementById("mission-delete-description");

  let missions = [];
  let loadedId = "";
  let deleteId = "";
  let ready = false;
  let pending = false;
  let inputsLocked = false;

  function show(message = "", isError = false) {
    feedback.textContent = message;
    feedback.classList.toggle("error", isError);
    feedback.hidden = !message;
  }

  function render() {
    const selected = select.value;
    const canEdit = ready && !pending && !inputsLocked;
    select.disabled = !ready || pending || missions.length === 0;
    load.disabled = !canEdit || !selected;
    name.disabled = !canEdit;
    create.disabled = !canEdit;
    update.disabled = !canEdit || !selected || loadedId !== selected;
    remove.disabled = !ready || pending || !selected;
    retry.disabled = pending;
  }

  function renderOptions(selected = "") {
    const label = missions.length ? "저장된 설정을 선택하세요" : "저장된 설정이 없어요";
    select.replaceChildren(
      new Option(label, ""),
      ...missions.map((mission) => new Option(mission.name, String(mission.id))),
    );
    select.value = missions.some((mission) => String(mission.id) === selected) ? selected : "";
  }

  async function refreshList() {
    missions = await requestMission(endpoint);
    ready = true;
    retry.hidden = true;
    renderOptions(select.value);
  }

  async function operate(action) {
    if (pending) return;
    pending = true;
    show();
    render();
    try {
      await action();
    } catch (error) {
      if (!ready) retry.hidden = false;
      if (error.status === 404) {
        try {
          await refreshList();
        } catch {
          retry.hidden = false;
        }
      }
      const message = error instanceof TypeError || error instanceof SyntaxError
        ? "임무 설정을 가져오지 못했어요. 서버 연결을 확인해 주세요."
        : error.message;
      show(message, true);
    } finally {
      pending = false;
      render();
    }
  }

  function save(missionId = "") {
    const missionName = name.value.trim();
    if (!missionName) {
      show("임무 이름을 입력해 주세요.", true);
      name.focus();
      return;
    }
    operate(async () => {
      const data = { ...readTarget(), name: missionName };
      const saved = await requestMission(missionId ? `${endpoint}/${missionId}` : endpoint, {
        method: missionId ? "PATCH" : "POST",
        data,
      });
      loadedId = String(saved.id);
      if (missionId) {
        missions = missions.map((mission) => mission.id === saved.id ? saved : mission);
      } else {
        missions.unshift(saved);
      }
      name.value = saved.name;
      renderOptions(loadedId);
      show(`‘${saved.name}’ 설정을 저장했어요.`);
    });
  }

  select.addEventListener("change", () => {
    show();
    render();
  });
  load.addEventListener("click", () => {
    const selected = select.value;
    operate(async () => {
      const mission = await requestMission(`${endpoint}/${selected}`);
      if (inputsLocked) {
        show("시뮬레이션을 종료한 뒤 설정을 불러와 주세요.", true);
        return;
      }
      applyTarget(mission);
      name.value = mission.name;
      loadedId = selected;
      show(`‘${mission.name}’ 설정을 불러왔어요.`);
    });
  });
  create.addEventListener("click", () => save());
  update.addEventListener("click", () => save(loadedId));
  retry.addEventListener("click", () => operate(refreshList));
  remove.addEventListener("click", () => {
    deleteId = select.value;
    const selectedName = select.selectedOptions[0].text;
    deleteDescription.textContent = `‘${selectedName}’ 설정을 삭제할까요?`;
    dialog.returnValue = "";
    dialog.showModal();
  });
  dialog.addEventListener("close", async () => {
    if (dialog.returnValue !== "delete") return;
    const id = deleteId;
    await operate(async () => {
      await requestMission(`${endpoint}/${id}`, { method: "DELETE" });
      missions = missions.filter((mission) => String(mission.id) !== id);
      if (loadedId === id) {
        loadedId = "";
        name.value = "";
      }
      renderOptions();
      show("임무 설정을 삭제했어요.");
    });
    (select.disabled ? name : select).focus();
  });

  operate(refreshList);
  return {
    lockInputs(locked) {
      inputsLocked = locked;
      render();
    },
  };
}
