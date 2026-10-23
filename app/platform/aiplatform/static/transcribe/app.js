// Вкладка «Расшифровка»: видео или аудио → текст, Markdown, субтитры. Без сборки.

import { $, api, chip, confirmAction, copyButton, fill, h, icon, jobProgress, logBlock, poller, statusChip, toast, toneOf, uploadFile } from "/ui.js";
import { stageRows, summary } from "../montage/stages.js";
import { clock, sizeLabel } from "./view.js";

const API = "/api/transcribe";
const STATUS_LABEL = { draft: "черновик" };
const FORMAT_LABEL = { txt: "Текст .txt", md: "Markdown .md", srt: "Субтитры .srt", vtt: "Субтитры .vtt", json: "Слова .json" };
const STAGE_ICONS = { done: "check", failed: "close", canceled: "close" };

const state = {
  config: null,
  jobs: [],
  selectedId: new URLSearchParams(location.search).get("id"),
  job: null,
  upload: null, // { name, percent, error }
};

const isActive = (j) => j?.status === "queued" || j?.status === "running";
const statusChipOf = (status) => (STATUS_LABEL[status] ? chip(STATUS_LABEL[status], "neutral") : statusChip(status));
const modelLabel = (id) => state.config?.models.find((m) => m.id === id)?.label || id;

// ---------- список ----------

let listSignature = "";

function renderList() {
  const signature = JSON.stringify([state.selectedId, state.jobs.map((j) => [j.id, j.title, j.status, Math.round(j.progress?.percent || 0)])]);
  if (signature === listSignature) return;
  listSignature = signature;
  if (!state.jobs.length) return fill($("jobs"), h("li", { class: "empty" }, "Пока пусто. Перетащи файл в окно слева."));
  fill($("jobs"), ...state.jobs.map((j) =>
    h("li", {},
      h("button", {
          type: "button",
          class: `history-item${j.id === state.selectedId ? " current" : ""}`,
          "aria-current": j.id === state.selectedId ? "true" : null,
          onclick: () => select(j.id),
        },
        h("span", { class: "history-text" },
          h("span", { class: "history-title" }, j.title),
          h("span", { class: "muted small" },
            new Date(j.created_at * 1000).toLocaleString("ru-RU", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }),
            j.source ? ` · ${clock(j.source.duration)}` : "",
            j.result ? ` · слов ${j.result.words}` : ""),
        ),
        j.status === "running" ? chip(`${Math.round(j.progress.percent)}%`, toneOf("running")) : statusChipOf(j.status),
      ),
    ),
  ));
}

async function select(id) {
  state.selectedId = id;
  state.job = null;
  history.replaceState(null, "", id ? `?id=${id}` : location.pathname);
  renderList();
  await refresh();
}

async function refresh(signal) {
  if (!state.selectedId) return renderJob();
  try {
    state.job = await api("GET", `${API}/jobs/${state.selectedId}`, undefined, { signal });
  } catch (err) {
    if (err.status === 404) return select(null);
    throw err;
  }
  renderJob();
}

// ---------- карточка ----------

let renderedKey = "";

function renderJob() {
  const root = $("job");
  const j = state.job && state.job.id === state.selectedId ? state.job : null;
  if (!j) {
    renderedKey = "";
    return fill(root, newForm());
  }
  // Пока идёт работа, опрос обновляет только блок «Работа»: плеер и поля не сбиваются.
  const key = JSON.stringify([j.id, j.status, j.updated_at, !!state.upload]);
  if (key === renderedKey) {
    if (isActive(j)) $("work")?.replaceWith(progress(j));
    return;
  }
  renderedKey = key;
  const blocks = [head(j)];
  if (isActive(j)) blocks.push(progress(j));
  if (j.status === "failed") blocks.push(h("section", { class: "panel stack" }, h("p", { class: "job-error", role: "alert" }, j.error), logBlock(j.log)));
  if (j.status === "canceled") blocks.push(h("section", { class: "panel" }, h("p", { class: "muted" }, "Отменено.")));
  if (j.status === "done") blocks.push(result(j));
  if (!isActive(j)) blocks.push(options(j));
  fill(root, ...blocks);
}

// Пустое состояние: сразу поле для файла и настройки — выбрал файл, и расшифровка пошла.
function newForm() {
  const cfg = state.config;
  if (!cfg) return null;
  const form = optionFields(cfg.defaults);
  const up = state.upload;
  const input = h("input", { type: "file", accept: acceptList(), hidden: true });
  input.addEventListener("change", () => input.files[0] && startNew(input.files[0], form.read()));
  const drop = h("div", { class: "drop" },
    icon("plus", { size: 24 }),
    h("p", {}, "Перетащи сюда видео или аудио"),
    h("p", { class: "muted small" }, cfg.accepted),
    up ? h("p", { class: up.error ? "form-error" : "muted small", "data-upload": "1", role: up.error ? "alert" : null }, up.error ? `${up.name}: ${up.error}` : `${up.name} — ${up.percent}%`) : null,
    h("button", { type: "button", class: "btn btn--primary", disabled: !!up && !up.error, onclick: () => input.click() }, "Выбрать файл"),
    input,
  );
  dropTarget(drop, (file) => startNew(file, form.read()));
  return h("section", { class: "panel stack" },
    h("h2", {}, "Текст из видео или аудио"),
    h("p", { class: "muted" }, "Речь расшифровывается локальной моделью, на выходе — текст абзацами, Markdown с таймкодами и субтитры. Ничего не уходит в сеть."),
    drop,
    form.node,
  );
}

function acceptList() {
  return (state.config?.accept || []).map((ext) => `.${ext}`).join(",") + ",video/*,audio/*";
}

function dropTarget(node, onFile) {
  node.addEventListener("dragover", (e) => (e.preventDefault(), node.classList.add("drag")));
  node.addEventListener("dragleave", () => node.classList.remove("drag"));
  node.addEventListener("drop", (e) => {
    e.preventDefault();
    node.classList.remove("drag");
    const file = e.dataTransfer.files[0];
    if (file) onFile(file);
  });
}

// Поля модели, языка и терминов → { node, read() }.
function optionFields(o) {
  const cfg = state.config;
  const model = h("select", { id: "opt-model" },
    ...cfg.models.map((m) => h("option", { value: m.id, selected: m.id === (o.model || cfg.default_model), disabled: !m.installed }, m.installed ? `${m.label} — ${m.note}` : `${m.label} — не установлена`)));
  const language = h("select", { id: "opt-language" }, ...cfg.languages.map((l) => h("option", { value: l.id, selected: l.id === o.language }, l.label)));
  const terms = h("textarea", { rows: 2, maxlength: String(cfg.max_terms), placeholder: "Claude Code, goroutine, ffmpeg — английские слова и имена, которые звучат" }, o.terms || "");
  const node = h("div", { class: "stack" },
    h("div", { class: "options" },
      h("label", { class: "field" }, h("span", { class: "field-label" }, "Модель"), model),
      h("label", { class: "field" }, h("span", { class: "field-label" }, "Язык речи"), language),
    ),
    h("label", { class: "field" }, h("span", { class: "field-label" }, "Термины ", h("span", { class: "muted" }, "— подсказка модели, по желанию")), terms),
    cfg.models.length ? null : h("p", { class: "form-error" }, "Все модели расшифровки выключены во вкладке «Настройки»."),
  );
  return { node, read: () => ({ model: model.value, language: language.value, terms: terms.value }) };
}

async function startNew(file, opts) {
  state.upload = { name: file.name, percent: 0, error: null };
  renderJob();
  let job = null;
  let uploaded = false;
  try {
    const title = file.name.replace(/\.[^.]+$/, "");
    job = await api("POST", `${API}/jobs`, { title, options: opts });
    await sendFile(job, file);
    uploaded = true;
    job = await api("POST", `${API}/jobs/${job.id}/start`, {});
    state.upload = null;
    await select(job.id);
  } catch (err) {
    if (uploaded) {
      // Файл принят, не запустилась модель — запись остаётся: можно выбрать другую модель и запустить.
      state.upload = null;
      await select(job.id);
      setBanner(err.message, "error");
    } else {
      if (job) await api("POST", `${API}/jobs/${job.id}/delete`).catch(() => {}); // черновик без файла не нужен
      if (err.kind === "abort") state.upload = null;
      else state.upload.error = err.message;
      renderJob();
    }
  }
  listPoll.now();
}

function sendFile(job, file) {
  return uploadFile(`${API}/jobs/${job.id}/source`, file, {
    limit: state.config.max_upload_bytes,
    headers: { "X-File-Name": encodeURIComponent(file.name) },
    onProgress: ({ percent }) => {
      if (state.upload) state.upload.percent = percent;
      const row = document.querySelector("[data-upload]");
      if (row) row.textContent = `${file.name} — ${percent}%`;
    },
  });
}

function head(j) {
  const title = h("input", { type: "text", value: j.title, maxlength: "120", class: "title-input", "aria-label": "Название" });
  title.addEventListener("change", () => update(j, { title: title.value }));
  const src = j.source;
  return h("section", { class: "panel stack" },
    h("div", { class: "field-head" },
      h("label", { class: "field grow" }, h("span", { class: "field-label" }, "Название"), title),
      h("div", { class: "toolbar" },
        statusChipOf(j.status),
        j.backend === "fake" ? chip("Демо — без моделей", "warning") : null,
        h("button", { type: "button", class: "btn btn--link btn--danger", onclick: () => remove(j), disabled: isActive(j) }, "Удалить"),
      ),
    ),
    src ? player(j) : h("p", { class: "muted" }, "Файла нет — загрузи его ниже."),
    src ? h("p", { class: "facts" }, h("span", {}, src.name || (src.kind === "video" ? "видео" : "аудио")), h("span", {}, clock(src.duration)), h("span", {}, sizeLabel(src.size))) : null,
  );
}

function player(j) {
  const tag = j.source.kind === "video" ? "video" : "audio";
  return h(tag, { src: j.source_url, controls: true, preload: "metadata", playsinline: true, class: "player", id: "player" });
}

function seek(t) {
  const media = $("player");
  if (!media) return;
  media.currentTime = t;
  media.play().catch(() => {});
}

function result(j) {
  const r = j.result || {};
  const paras = j.paragraphs || [];
  return h("section", { class: "panel stack" },
    h("div", { class: "result-head" },
      h("h2", {}, "Текст"),
      h("div", { class: "toolbar" }, copyButton(() => j.text, "Скопировать текст")),
    ),
    h("p", { class: "facts" },
      h("span", {}, `слов ${r.words ?? 0}`),
      h("span", {}, `знаков ${r.chars ?? 0}`),
      h("span", {}, modelLabel(r.model)),
      r.elapsed_s ? h("span", {}, `расшифровка заняла ${clock(r.elapsed_s)}`) : null,
    ),
    h("div", { class: "downloads" }, ...Object.entries(j.files || {}).map(([ext, url]) =>
      h("a", { class: "btn btn--secondary btn--sm", href: url, download: "" }, FORMAT_LABEL[ext] || ext))),
    h("div", { class: "paragraphs" }, ...paras.map((p) =>
      h("div", { class: "para" },
        h("button", { type: "button", class: "para-time", title: "Слушать с этого места", onclick: () => seek(p.s) }, clock(p.s)),
        h("p", {}, p.text),
      ))),
  );
}

// Настройки для повторного прогона: другая модель, язык или термины; можно заменить файл.
function options(j) {
  const form = optionFields(j.options);
  const up = state.upload;
  const input = h("input", { type: "file", accept: acceptList(), hidden: true });
  input.addEventListener("change", () => input.files[0] && replace(j, input.files[0]));
  const error = h("p", { class: "form-error", role: "alert", hidden: true });
  const start = h("button", { type: "button", class: "btn btn--primary", disabled: !j.source }, j.status === "draft" ? "Расшифровать" : "Расшифровать заново");
  start.addEventListener("click", async () => {
    if (j.status === "done") {
      const ok = await confirmAction({ title: "Расшифровать заново?", body: "Нынешний текст заменится новым.", confirmLabel: "Расшифровать" });
      if (!ok) return;
    }
    error.hidden = true;
    start.disabled = true;
    try {
      state.job = await api("POST", `${API}/jobs/${j.id}/start`, form.read());
      renderedKey = "";
      renderJob();
      listPoll.now();
    } catch (err) {
      error.textContent = err.message;
      error.hidden = false;
      start.disabled = false;
    }
  });
  const section = h("section", { class: "panel stack" },
    h("h2", {}, j.status === "done" ? "Другой моделью или языком" : "Настройки"),
    form.node,
    up ? h("p", { class: up.error ? "form-error" : "muted small", "data-upload": "1" }, up.error ? `${up.name}: ${up.error}` : `${up.name} — ${up.percent}%`) : null,
    error,
    h("div", { class: "toolbar" },
      start,
      h("button", { type: "button", class: "btn btn--secondary", disabled: !!up && !up.error, onclick: () => input.click() }, j.source ? "Заменить файл" : "Выбрать файл"),
      input,
    ),
  );
  dropTarget(section, (file) => replace(j, file));
  return section;
}

async function replace(j, file) {
  state.upload = { name: file.name, percent: 0, error: null };
  renderedKey = "";
  renderJob();
  try {
    state.job = await sendFile(j, file);
    state.upload = null;
  } catch (err) {
    if (err.kind === "abort") state.upload = null;
    else state.upload.error = err.message;
  }
  renderedKey = "";
  renderJob();
  listPoll.now();
}

async function update(j, body) {
  try {
    state.job = await api("POST", `${API}/jobs/${j.id}`, body);
    renderedKey = "";
    renderJob();
    listPoll.now();
  } catch (err) {
    setBanner(err.message, "error");
  }
}

async function remove(j) {
  const ok = await confirmAction({ title: `Удалить «${j.title}»?`, body: "Удалятся файл и расшифровка.", confirmLabel: "Удалить", danger: true });
  if (!ok) return;
  try {
    await api("POST", `${API}/jobs/${j.id}/delete`);
    state.jobs = state.jobs.filter((x) => x.id !== j.id);
    toast("Запись удалена");
    await select(null);
  } catch (err) {
    setBanner(err.message, "error");
  }
}

async function cancel(j) {
  try {
    state.job = await api("POST", `${API}/jobs/${j.id}/cancel`);
    renderedKey = "";
    renderJob();
    listPoll.now();
  } catch (err) {
    setBanner(err.message, "error");
  }
}

// ---------- работа ----------

function progress(j) {
  const now = Date.now() / 1000;
  const info = summary(j, now);
  const rows = stageRows(j.stages, now);
  return h("section", { class: "panel stack", id: "work" },
    h("h2", {}, "Работа"),
    jobProgress(j, { onCancel: () => cancel(j) }),
    rows.length ? h("p", { class: "muted small stage-summary" }, [info.step, info.elapsed && `прошло ${info.elapsed}`, info.left].filter(Boolean).join(" · ")) : null,
    rows.length ? h("ol", { class: "stages" }, ...rows.map(stageRow)) : null,
    logBlock(j.log),
  );
}

function stageRow(s) {
  return h("li", { class: `stage-row stage-row--${s.status}` },
    h("span", { class: "stage-icon", "aria-hidden": "true" }, STAGE_ICONS[s.status] ? icon(STAGE_ICONS[s.status]) : h("span", { class: "stage-dot" })),
    h("div", { class: "stage-body" },
      h("div", { class: "stage-head" },
        h("span", { class: "stage-label" }, s.label, h("span", { class: "sr-only" }, ` — ${s.status === "running" ? "идёт" : s.note || "впереди"}`)),
        h("span", { class: "muted small" }, s.status === "running" ? `${s.percent}% · ${s.note}` : s.note),
      ),
      s.detail && s.status === "running" ? h("span", { class: "muted small" }, s.detail) : null,
    ),
  );
}

// ---------- опрос и запуск ----------

const listPoll = poller({
  run: async (signal) => {
    state.jobs = (await api("GET", `${API}/jobs`, undefined, { signal })).jobs;
    renderList();
    if (state.selectedId) await refresh(signal);
    setBanner(null);
  },
  fast: 1000,
  slow: 5000,
  isActive: () => state.jobs.some(isActive),
  onError: (err) => setBanner(err.message, "error"),
});

let configBanner = null;
const BANNER_TONE = { warn: "warning", error: "danger" };

function setBanner(text, kind) {
  const banner = $("banner");
  const shown = text ? { text, kind } : configBanner;
  banner.hidden = !shown;
  if (!shown) return;
  banner.className = `banner${BANNER_TONE[shown.kind] ? ` tone-${BANNER_TONE[shown.kind]}` : ""}`;
  banner.setAttribute("role", shown.kind === "error" ? "alert" : "status");
  banner.replaceChildren(...[].concat(shown.text));
}

async function init() {
  try {
    state.config = await api("GET", `${API}/config`);
  } catch (err) {
    return setBanner(err.message, "error");
  }
  const cfg = state.config;
  const ready = cfg.models.filter((m) => m.installed);
  $("mode-chip").textContent = cfg.backend === "fake" ? "демо, без моделей" : `моделей готово ${ready.length}`;
  if (cfg.backend === "fake") {
    configBanner = { kind: "warn", text: "Демо-режим: вместо модели — имитация, текст ненастоящий." };
  } else if (!ready.length) {
    configBanner = { kind: "warn", text: "Ни одной модели расшифровки не установлено: make models M=whisper-large-v3-turbo в app/platform." };
  }
  setBanner(null);
  $("new-job").addEventListener("click", () => select(null));
  renderJob();
  listPoll.start();
}

init();
