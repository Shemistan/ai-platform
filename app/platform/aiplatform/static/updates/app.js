// Вкладка «Обновления»: версии моделей и пайплайнов, реестр запрещённых организаций.
import { $, api, chip, fill, followTask, h, progressBar, taskCard } from "/ui.js";

const API = "/api/updates";
const MODEL_STATUS = { update: "есть обновление", ok: "актуально", unknown: "не знаю", error: "ошибка" };
const state = { data: null, timer: null, editing: false };

// Тон статуса модели: тот же словарь цветов, что у задач (chip в ui.js).
const MODEL_TONE = { update: "warning", ok: "success", unknown: "neutral", error: "danger" };

function when(ts) {
  return ts ? new Date(ts * 1000).toLocaleString("ru-RU", { dateStyle: "short", timeStyle: "short" }) : "";
}

function showError(message) {
  $("error").hidden = !message;
  $("error").textContent = message || "";
}

async function act(path, body) {
  try {
    showError("");
    render(await api("POST", `${API}/${path}`, body));
  } catch (err) {
    showError(err.message);
  }
}

// --- модели ---

function renderModels(job) {
  const running = job.status === "running";
  $("models-check").disabled = running;
  const results = job.results || [];
  const head = [];
  if (running) head.push(progressBar({ stage: job.progress || "Проверяю…", percent: 0 }));
  if (job.status === "failed") head.push(h("p", { class: "job-error" }, job.error));
  if (job.status === "done") {
    head.push(
      h("p", { class: job.updates ? "note" : "hint" }, job.updates ? `Есть обновления: ${job.updates}. Проверено ${when(job.finished)}.` : `Всё актуально. Проверено ${when(job.finished)}.`),
    );
  }
  if (!results.length) {
    $("models").replaceChildren(...head, running ? null : h("p", { class: "muted" }, "Ещё не проверяли — нажми «Проверить обновления»."));
    return;
  }
  const rows = [];
  let group = "";
  for (const r of results) {
    if (r.group !== group) {
      group = r.group;
      rows.push(h("tr", { class: "group" }, h("td", { colspan: 5 }, group)));
    }
    rows.push(
      h(
        "tr",
        {},
        h("td", {}, r.url ? h("a", { class: "link", href: r.url, target: "_blank", rel: "noopener" }, r.label) : r.label),
        h("td", { class: "version" }, r.installed || "—"),
        h("td", { class: "version" }, r.latest || "—"),
        h("td", {}, chip(MODEL_STATUS[r.status] || r.status, MODEL_TONE[r.status])),
        h("td", {}, h("div", {}, r.message), r.hint ? h("div", { class: "hint" }, r.hint) : null),
      ),
    );
  }
  $("models").replaceChildren(
    ...head,
    h(
      "div",
      { class: "table-wrap" },
      h("table", { class: "data-table" }, h("thead", {}, h("tr", {}, h("th", {}, "Что"), h("th", {}, "У нас"), h("th", {}, "В источнике"), h("th", {}, "Статус"), h("th", {}, "Подробности"))), h("tbody", {}, ...rows)),
    ),
  );
}

// --- реестр ---

function entryRow(e, statuses, mark = "") {
  return h(
    "tr",
    { class: mark ? `row-${mark}` : null },
    h("td", {}, h("strong", {}, e.name), h("div", { class: "aliases" }, e.aliases.join(", "))),
    h("td", {}, statuses[e.status] || e.status),
    h("td", { class: "disclaimer" }, `${e.disclaimer}.`),
    h("td", { class: "hint" }, e.basis, e.source ? [" ", h("a", { class: "link", href: e.source, target: "_blank", rel: "noopener" }, "источник")] : null),
  );
}

function entryTable(rows) {
  return h(
    "div",
    { class: "table-wrap" },
    h("table", { class: "data-table" }, h("thead", {}, h("tr", {}, h("th", {}, "Организация и как ищем"), h("th", {}, "Статус"), h("th", {}, "Пометка на видео"), h("th", {}, "Основание"))), h("tbody", {}, ...rows)),
  );
}

function renderProposal(reg) {
  const check = reg.check || {};
  const box = [];
  if (check.status === "running") box.push(progressBar({ stage: check.progress || "Проверяю…", percent: 0 }));
  if (check.status === "failed") box.push(h("p", { class: "job-error" }, check.error));
  if (check.status === "done" && !check.proposal) {
    const changed = check.diff && (check.diff.added.length || check.diff.changed.length || check.diff.removed.length);
    box.push(h("p", { class: "hint" }, changed ? `Предложение ${check.applied ? "применено" : "отклонено"}.` : `Изменений нет — проверено ${when(check.finished)}.`, check.notes ? ` ${check.notes}` : ""));
  }
  if (check.status === "done" && check.proposal) {
    const { added, changed, removed } = check.diff;
    const current = Object.fromEntries(reg.entries.map((e) => [e.id, e]));
    const rows = [
      ...check.proposal.filter((e) => added.includes(e.id)).map((e) => entryRow(e, reg.statuses, "added")),
      ...check.proposal.filter((e) => changed.includes(e.id)).map((e) => entryRow(e, reg.statuses, "changed")),
      ...removed.map((id) => entryRow(current[id], reg.statuses, "removed")),
    ];
    box.push(
      h(
        "div",
        { class: "proposal" },
        h("strong", {}, `LLM предлагает: новых ${added.length}, изменённых ${changed.length}, убрать ${removed.length}`),
        check.notes ? h("p", { class: "hint" }, check.notes) : null,
        h("p", { class: "hint" }, "Проверь основания по ссылкам: LLM может ошибиться. Применяется весь список целиком."),
        entryTable(rows),
        h(
          "div",
          { class: "actions" },
          h("button", { type: "button", class: "primary", onclick: () => act("registry/apply") }, "Применить"),
          h("button", { type: "button", class: "link", onclick: () => act("registry/dismiss") }, "Отклонить"),
        ),
      ),
    );
  }
  $("proposal").replaceChildren(...box);
  $("registry-check").disabled = check.status === "running";
}

function renderRegistry(reg) {
  $("registry-hint").textContent =
    `Если организация из списка упомянута в выпуске, на её слайдах внизу идёт мелкая пометка, а в посте — строка в конце. ` +
    `Последняя сверка: ${reg.checked || "не было"}${reg.customized ? "" : " (список по умолчанию)"}. ` +
    `«Проверить» просит LLM с веб-поиском сверить список с перечнями Минюста.`;
  $("registry-reset").hidden = !reg.customized;
  $("registry").replaceChildren(entryTable(reg.entries.map((e) => entryRow(e, reg.statuses))));
  if (!state.editing) {
    const plain = reg.entries.map(({ status_label, ...rest }) => rest);
    $("registry-json").value = JSON.stringify({ entries: plain }, null, 2);
  }
  renderProposal(reg);
}

function render(data) {
  state.data = data;
  renderModels(data.models);
  renderRegistry(data.registry);
  clearTimeout(state.timer);
  const busy = data.models.status === "running" || data.registry.check?.status === "running";
  state.timer = setTimeout(refresh, busy ? 1000 : 10000);
}

async function refresh() {
  try {
    render(await api("GET", `${API}/state`));
  } catch (err) {
    showError(err.message);
    state.timer = setTimeout(refresh, 5000);
  }
}

$("models-check").addEventListener("click", () => act("models/check"));
$("registry-check").addEventListener("click", () => act("registry/check"));
$("registry-json").addEventListener("input", () => (state.editing = true));
$("registry-save").addEventListener("click", async () => {
  await act("registry", { text: $("registry-json").value });
  if ($("error").hidden) state.editing = false;
});
$("registry-reset").addEventListener("click", async () => {
  state.editing = false;
  await act("registry/reset");
});
refresh();

// --- модели озвучки движка: что на диске, скачать основные или модели для сравнения ---

const voice = { picked: new Set(), active: null };

function renderVoiceModels(data) {
  const main = data.main.map((m) =>
    h("tr", {}, h("td", {}, m.label, h("span", { class: "sub" }, m.path)), h("td", {}, m.exists ? chip("на месте", "success") : chip("нет", "danger"))),
  );
  const mlx = data.mlx.map((m) => {
    const box = h("input", { type: "checkbox", checked: voice.picked.has(m.id), disabled: m.downloaded });
    box.addEventListener("change", () => {
      if (box.checked) voice.picked.add(m.id);
      else voice.picked.delete(m.id);
      renderVoiceButton();
    });
    return h(
      "tr",
      {},
      h("td", {}, h("label", { class: "check" }, box, h("span", {}, m.label, h("span", { class: "hint" }, `${m.note}, около ${m.size_gb} ГБ`)))),
      h("td", {}, m.downloaded ? chip("скачана", "success") : chip("не скачана")),
    );
  });
  const pick = h("button", { type: "button", class: "secondary", id: "voice-mlx", onclick: () => startVoice("mlx", { engines: [...voice.picked] }) }, "Скачать выбранные");
  fill(
    $("voice-models"),
    data.root_exists ? null : h("p", { class: "job-error" }, `Папки моделей ${data.root} нет: ${data.hint}`),
    h("table", { class: "data-table" }, h("thead", {}, h("tr", {}, h("th", {}, "Основные"), h("th", {}, ""))), h("tbody", {}, ...main)),
    h("table", { class: "data-table", style: "margin-top:16px" }, h("thead", {}, h("tr", {}, h("th", {}, "Для сравнения (mlx-audio, Apple Silicon)"), h("th", {}, ""))), h("tbody", {}, ...mlx)),
    h("div", { class: "toolbar", style: "margin-top:10px" }, pick, h("span", { class: "hint" }, "Higgs и Fish на huggingface.co просят принять лицензию — открой страницу модели и согласись.")),
  );
  renderVoiceButton();
}

function renderVoiceButton() {
  const btn = $("voice-mlx");
  if (btn) btn.disabled = !voice.picked.size || Boolean(voice.active);
  for (const id of ["voice-fetch", "voice-move"]) $(id).disabled = Boolean(voice.active);
}

async function loadVoiceModels() {
  try {
    const data = await api("GET", `${API}/voice-models`);
    renderVoiceModels(data);
    const active = data.tasks.find((t) => t.status === "queued" || t.status === "running");
    if (active && voice.active !== active.id) watchVoice(active);
  } catch (err) {
    fill($("voice-models"), h("p", { class: "job-error" }, err.message));
  }
}

async function watchVoice(task) {
  voice.active = task.id;
  renderVoiceButton();
  const cancel = (t) => api("POST", `${API}/tasks/${t.id}/cancel`);
  await followTask(`${API}/tasks/${task.id}`, (t) => fill($("voice-task"), taskCard(t, { full: true, onCancel: cancel })));
  voice.active = null;
  voice.picked.clear();
  loadVoiceModels();
}

async function startVoice(action, body = {}) {
  try {
    watchVoice(await api("POST", `${API}/voice-models/${action}`, body));
  } catch (err) {
    fill($("voice-task"), h("p", { class: "job-error" }, err.message));
  }
}

$("voice-fetch").addEventListener("click", () => startVoice("fetch"));
$("voice-move").addEventListener("click", () => startVoice("move"));
loadVoiceModels();
