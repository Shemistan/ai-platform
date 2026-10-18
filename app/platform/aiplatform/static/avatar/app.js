// Вкладка «Диктор»: персонажи, речь (текст или своя озвучка), запуск и статус задачи. Без сборки.

import { $, api, chip, confirmAction, fill, h, icon, iconButton, jobProgress, loadLast, logBlock, modal, poller, saveLast, seconds as fmtElapsed, STATUS, statusChip, toast, toneOf, UPLOAD_LIMITS, uploadFile } from "/ui.js";
import { characterPayload, jobPayload } from "./form.js";

const LAST = "avatar"; // запомненный выбор: персонаж, движок, голос, настройки видео

const state = {
  config: null,
  audio: null, // /api/audio/config: движки и голоса
  characters: [],
  character: null,
  source: "text",
  speech: null, // своя озвучка: { name, uploadId, uploading, error }
  frame: null,
  size: null,
  jobs: [],
  selectedId: null,
  submitting: false,
};

const limits = () => state.config?.upload_limits || UPLOAD_LIMITS;
const isActive = (job) => job.status === "queued" || job.status === "running";

function remember() {
  saveLast(LAST, {
    character: state.character,
    engine: $("engine").value,
    voice: $("voice").value,
    model: $("model").value,
    frame: state.frame,
    size: state.size,
    enhance: $("enhance").checked,
  });
}

// ---------- персонажи ----------

async function loadCharacters() {
  state.characters = (await api("GET", "/api/avatar/characters")).characters;
  if (!state.characters.some((c) => c.id === state.character)) state.character = state.characters[0]?.id || null;
  renderCharacters();
}

function renderCharacters() {
  const cards = state.characters.map((c) =>
    h("li", { class: `character${c.id === state.character ? " current" : ""}` },
      h("button", {
          type: "button",
          class: "character-pick",
          "aria-pressed": String(c.id === state.character),
          onclick: () => {
            state.character = c.id;
            remember();
            renderCharacters();
          },
        },
        h("img", { src: c.image_url, alt: "", loading: "lazy" }),
        h("span", { class: "character-name" }, c.name),
        h("span", { class: "character-kind muted small" }, c.kind_label),
      ),
      h("div", { class: "character-tools" },
        h("button", { type: "button", class: "btn btn--link btn--sm", onclick: () => renameCharacter(c) }, "Переименовать"),
        iconButton("trash", `Удалить персонажа «${c.name}»`, { onclick: () => deleteCharacter(c) }),
      ),
    ),
  );
  const add = h("li", {},
    h("button", { type: "button", class: "character-add", onclick: addCharacter }, icon("plus", { size: 20 }), "Добавить персонажа"),
  );
  fill($("characters"), ...cards, add);
}

function addCharacter() {
  const cfg = state.config;
  const upload = { uploadId: null, uploading: false };
  const input = h("input", { type: "file", accept: "image/png,image/jpeg,image/webp", hidden: true });
  const preview = h("img", { class: "preview", alt: "", hidden: true });
  const pick = h("button", { type: "button", class: "btn btn--secondary" }, "Выбрать картинку");
  const name = h("input", { type: "text", maxlength: "80", placeholder: "Например: Я в студии" });
  let kind = "photo";
  const kinds = h("div", { class: "segmented segmented--radio" },
    ...cfg.kinds.map((k) =>
      h("label", { class: "seg" },
        h("input", { type: "radio", name: "char-kind", value: k.id, checked: k.id === kind, onchange: () => ((kind = k.id), showConsent()) }),
        h("span", {}, k.label),
      ),
    ),
  );
  const confirmed = h("input", { type: "checkbox" });
  const owner = h("input", { type: "text", maxlength: "200", placeholder: "Чьё лицо: имя и фамилия" });
  let form = "self";
  const forms = h("div", { class: "segmented segmented--radio" },
    ...cfg.consent_forms.map((f) =>
      h("label", { class: "seg" },
        h("input", { type: "radio", name: "char-consent", value: f.id, checked: f.id === form, onchange: () => (form = f.id) }),
        h("span", {}, f.label),
      ),
    ),
  );
  const consent = h("div", { class: "consent" },
    h("label", { class: "check" }, confirmed,
      h("span", {}, "Это моё лицо или у меня есть письменное согласие человека на фото — его лицо заговорит синтетической речью.")),
    forms,
    h("label", { class: "field" }, h("span", { class: "field-label" }, "Чьё лицо"), owner),
  );
  const showConsent = () => (consent.hidden = kind !== "photo");
  const error = h("p", { class: "form-error", role: "alert", hidden: true });
  const save = h("button", { type: "submit", class: "btn btn--primary" }, "Добавить");
  const body = h("form", { class: "new-character", novalidate: true },
    h("p", { class: "hint" }, "Лучше всего — лицо анфас, крупно, ровный свет, рот закрыт. Рисованному персонажу нужно лицо в человеческих пропорциях: иначе модель его не найдёт."),
    h("div", { class: "toolbar" }, pick, input), preview,
    h("label", { class: "field" }, h("span", { class: "field-label" }, "Имя"), name),
    h("fieldset", { class: "field" }, h("legend", { class: "field-label" }, "Вид"), kinds),
    consent, error, save,
  );
  const dialog = modal("Новый персонаж", body);
  showConsent();
  pick.addEventListener("click", () => input.click());
  input.addEventListener("change", async () => {
    const file = input.files[0];
    if (!file) return;
    preview.src = URL.createObjectURL(file);
    preview.hidden = false;
    if (!name.value.trim()) name.value = file.name.replace(/\.[^.]+$/, "");
    upload.uploading = true;
    upload.uploadId = null;
    error.hidden = true;
    try {
      upload.uploadId = (await uploadFile("/api/avatar/uploads", file, { limit: limits().image })).id;
    } catch (err) {
      if (err.kind !== "abort") ((error.textContent = err.message), (error.hidden = false));
    }
    upload.uploading = false;
  });
  body.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (upload.uploading) return ((error.textContent = "Картинка ещё загружается."), (error.hidden = false));
    const { payload, error: problem } = characterPayload({
      uploadId: upload.uploadId, name: name.value, kind, consent: { confirmed: confirmed.checked, owner: owner.value, form },
    });
    if (problem) return ((error.textContent = problem), (error.hidden = false));
    save.disabled = true;
    try {
      const item = await api("POST", "/api/avatar/characters", payload);
      state.character = item.id;
      await loadCharacters();
      remember();
      dialog.close();
      toast(`Персонаж «${item.name}» добавлен`);
    } catch (err) {
      error.textContent = err.message;
      error.hidden = false;
      save.disabled = false;
    }
  });
}

function renameCharacter(c) {
  const name = h("input", { type: "text", maxlength: "80", value: c.name });
  const error = h("p", { class: "form-error", role: "alert", hidden: true });
  const body = h("form", { class: "new-character", novalidate: true },
    h("label", { class: "field" }, h("span", { class: "field-label" }, "Имя"), name), error,
    h("button", { type: "submit", class: "btn btn--primary" }, "Сохранить"),
  );
  const dialog = modal("Переименовать персонажа", body);
  name.select();
  body.addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      await api("POST", `/api/avatar/characters/${c.id}/rename`, { name: name.value });
      await loadCharacters();
      dialog.close();
    } catch (err) {
      error.textContent = err.message;
      error.hidden = false;
    }
  });
}

async function deleteCharacter(c) {
  const ok = await confirmAction({
    title: `Удалить персонажа «${c.name}»?`,
    body: "Картинка персонажа удалится. Уже готовые видео останутся в истории.",
    confirmLabel: "Удалить",
    danger: true,
  });
  if (!ok) return;
  try {
    await api("POST", `/api/avatar/characters/${c.id}/delete`);
    await loadCharacters();
    toast("Персонаж удалён");
  } catch (err) {
    setBanner(err.message, "error");
  }
}

// ---------- речь ----------

const engine = () => state.audio?.engines.find((e) => e.id === $("engine").value);

function renderVoices(preferred) {
  const e = engine();
  if (!e) return;
  const voice = e.voices.includes(preferred) ? preferred : e.default_voice;
  fill($("voice"), ...e.voices.map((v) => h("option", { value: v, selected: v === voice }, v)));
  $("speed-field").hidden = !e.speed;
}

function renderSource() {
  $("speech-text").hidden = state.source !== "text";
  $("speech-audio").hidden = state.source !== "audio";
  const s = state.speech;
  $("audio-name").textContent = !s ? "mp3, wav или m4a" : s.uploading ? `${s.name} — загружается…` : s.error ? `${s.name}: ${s.error}` : s.name;
}

async function setSpeechFile(file) {
  if (!file) return;
  state.speech = { name: file.name, uploadId: null, uploading: true, error: null };
  renderSource();
  try {
    state.speech.uploadId = (await uploadFile("/api/avatar/uploads", file, { limit: limits().media })).id;
  } catch (err) {
    state.speech = err.kind === "abort" ? null : { ...state.speech, error: err.message };
  }
  if (state.speech) state.speech.uploading = false;
  renderSource();
}

function radios(id, name, items, current, onPick) {
  fill($(id), ...items.map((item) =>
    h("label", { class: "seg" },
      h("input", { type: "radio", name, value: String(item.id), checked: item.id === current, onchange: () => onPick(item.id) }),
      h("span", {}, item.label),
    ),
  ));
}

// ---------- запуск ----------

function showError(text) {
  $("form-error").textContent = text || "";
  $("form-error").hidden = !text;
}

async function submit(e) {
  e.preventDefault();
  if (state.submitting) return;
  if (state.source === "audio" && state.speech?.uploading) return showError("Озвучка ещё загружается.");
  const { payload, error } = jobPayload({
    character: state.character,
    source: state.source,
    text: $("text").value,
    engine: $("engine").value,
    voice: $("voice").value,
    speed: engine()?.speed ? $("speed").value : "",
    uploadId: state.speech?.uploadId,
    model: $("model").value,
    size: state.size,
    frame: state.frame,
    enhance: $("enhance").checked,
    maxText: state.config.max_text,
  });
  if (error) return showError(error);
  showError(null);
  state.submitting = true;
  $("submit").disabled = true;
  try {
    const job = await api("POST", "/api/avatar/jobs", payload);
    remember();
    upsertJob(job);
    state.selectedId = job.id;
    renderJobs();
    jobsPoll.now();
  } catch (err) {
    showError(err.message);
  } finally {
    state.submitting = false;
    $("submit").disabled = false;
  }
}

// ---------- задачи ----------

function upsertJob(job) {
  const i = state.jobs.findIndex((j) => j.id === job.id);
  if (i < 0) state.jobs.unshift(job);
  else state.jobs[i] = { ...state.jobs[i], ...job };
}

const frameLabel = (id) => state.config?.frames.find((f) => f.id === id)?.label || id;

function describeJob(job) {
  const speech = job.voice ? `${job.voice.engine_label}, ${job.voice.voice}` : "своя озвучка";
  const parts = [job.character.name, speech, frameLabel(job.frame), String(job.size)];
  if (job.duration_s) parts.push(`${Math.round(job.duration_s)} с речи`);
  return parts.join(" · ");
}

let logOpen = false;

function renderJob() {
  const root = $("job");
  const job = state.jobs.find((j) => j.id === state.selectedId);
  if (!job) {
    root.replaceChildren(h("p", { class: "muted" }, "Здесь появится видео. Выбери персонажа, напиши текст и нажми «Подготовить»."));
    return;
  }
  const open = root.querySelector("details.log-wrap");
  if (open) logOpen = open.open;
  const log = logBlock(job.log);
  if (log) log.open = logOpen;
  fill(root,
    h("div", { class: "job-head" },
      job.face_url ? h("img", { class: "job-face", src: job.face_url, alt: "" }) : null,
      statusChip(job.status),
      job.backend === "fake" ? chip("Демо — не модель", "warning") : null,
      h("span", { class: "muted small" }, describeJob(job)),
    ),
    job.preview ? h("p", { class: "job-text" }, job.preview) : null,
    isActive(job) ? jobProgress(job, { onCancel: (j) => cancelJob(j.id) }) : null,
    job.status === "done" ? h("video", { class: "player", src: job.video_url, controls: true, preload: "metadata", playsinline: true }) : null,
    job.status === "done"
      ? h("div", { class: "job-foot" },
          h("span", { class: "muted small" }, `Готово за ${fmtElapsed(job.elapsed_s)}`),
          h("a", { class: "btn btn--link", href: `${job.video_url}?download=1` }, "Скачать mp4"),
        )
      : null,
    job.status === "failed" ? h("p", { class: "job-error", role: "alert" }, job.error) : null,
    job.status === "canceled" ? h("p", { class: "muted" }, "Отменено.") : null,
    log,
  );
}

let historySignature = "";

function renderHistory() {
  const signature = JSON.stringify([state.selectedId, state.jobs.map((j) => [j.id, j.status, Math.round(j.progress.percent)])]);
  if (signature === historySignature) return;
  historySignature = signature;
  if (!state.jobs.length) return fill($("history"), h("li", { class: "empty" }, "Пока пусто."));
  fill($("history"), ...state.jobs.map((job) =>
    h("li", {},
      h("button", {
          type: "button",
          class: `history-item${job.id === state.selectedId ? " current" : ""}`,
          "aria-current": job.id === state.selectedId ? "true" : null,
          onclick: () => selectJob(job.id),
        },
        job.face_url ? h("img", { class: "history-thumb", src: job.face_url, alt: "", loading: "lazy" }) : null,
        h("span", { class: "history-text" },
          h("span", { class: "history-title" }, job.preview || job.character.name),
          h("span", { class: "muted small" },
            `${new Date(job.created_at * 1000).toLocaleString("ru-RU", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })} · ${job.character.name}`),
        ),
        chip(job.status === "running" ? `${Math.round(job.progress.percent)}%` : STATUS[job.status], toneOf(job.status)),
      ),
    ),
  ));
}

function renderJobs() {
  renderJob();
  renderHistory();
}

async function selectJob(id) {
  state.selectedId = id;
  renderJobs();
  try {
    upsertJob(await api("GET", `/api/avatar/jobs/${id}`));
    renderJobs();
  } catch { /* останется вид из списка */ }
}

async function cancelJob(id) {
  try {
    upsertJob(await api("POST", `/api/avatar/jobs/${id}/cancel`));
    renderJobs();
  } catch (err) {
    setBanner(err.message, "error");
  }
}

// Опрос: раз в секунду, пока есть активные задачи, раз в 5 с в простое (poller из ui.js).
const jobsPoll = poller({ run: (signal) => poll(signal), fast: 1000, slow: 5000, isActive: () => state.jobs.some(isActive), onError: (err) => setBanner(err.message, "error") });

async function poll(signal) {
  const { jobs } = await api("GET", "/api/avatar/jobs", undefined, { signal });
  // Список — без лога; лог выбранной задачи берём отдельным запросом.
  const selected = jobs.find((j) => j.id === state.selectedId);
  const previous = state.jobs.find((j) => j.id === state.selectedId);
  if (selected) {
    const needFull = isActive(selected) || !previous?.log || previous.status !== selected.status;
    Object.assign(selected, needFull ? await api("GET", `/api/avatar/jobs/${selected.id}`, undefined, { signal }) : { log: previous.log });
  }
  state.jobs = jobs;
  if (!state.selectedId && jobs.length) return selectJob(jobs[0].id);
  renderJobs();
  setBanner(null);
}

// ---------- запуск страницы ----------

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
    [state.config, state.audio] = await Promise.all([api("GET", "/api/avatar/config"), api("GET", "/api/audio/config")]);
  } catch (err) {
    setBanner(err.message, "error");
    return;
  }
  const cfg = state.config;
  const last = loadLast(LAST)?.data || {};
  state.character = last.character || null;
  state.frame = cfg.frames.some((f) => f.id === last.frame) ? last.frame : cfg.default_frame;

  const installed = cfg.models.filter((m) => m.installed);
  $("model-chip").textContent = cfg.backend === "fake" ? "демо, без модели" : installed.length ? installed.map((m) => m.label).join(" · ") : "модель не установлена";
  fill($("model"), ...cfg.models.map((m) =>
    h("option", { value: m.id, selected: m.id === (last.model || cfg.default_model), disabled: !m.installed },
      m.installed ? m.label : `${m.label} — не установлена`)));
  const model = () => cfg.models.find((m) => m.id === $("model").value) || cfg.models[0];
  const applyModel = () => {
    const m = model();
    $("model-note").textContent = [m.note, m.installed ? "" : `Установить: make models M=${m.id}`].filter(Boolean).join(". ");
    if (!m.sizes.includes(state.size)) state.size = m.sizes.includes(last.size) ? last.size : m.sizes[0];
    radios("sizes", "size", m.sizes.map((s) => ({ id: s, label: s === m.sizes[0] ? `${s} — быстрее` : `${s} — чётче` })), state.size, (s) => (state.size = s));
  };
  $("model").addEventListener("change", applyModel);
  applyModel();
  radios("frames", "frame", cfg.frames, state.frame, (f) => (state.frame = f));
  $("enhance").checked = Boolean(last.enhance);

  radios("sources", "source", [{ id: "text", label: "Озвучить текст" }, { id: "audio", label: "Готовая озвучка" }], state.source, (s) => {
    state.source = s;
    renderSource();
  });
  const engines = state.audio.engines;
  const engineId = engines.some((e) => e.id === last.engine) ? last.engine : state.audio.default_engine;
  fill($("engine"), ...engines.map((e) => h("option", { value: e.id, selected: e.id === engineId }, e.label)));
  $("engine").addEventListener("change", () => renderVoices($("voice").value));
  renderVoices(last.voice);
  const [low, high] = state.audio.speed_range;
  Object.assign($("speed"), { min: low, max: high });
  $("text").addEventListener("input", () => ($("text-count").textContent = $("text").value.length ? `${$("text").value.length} знаков` : ""));
  $("text").addEventListener("keydown", (e) => e.key === "Enter" && (e.metaKey || e.ctrlKey) && $("composer").requestSubmit());
  $("audio-pick").addEventListener("click", () => $("audio-input").click());
  $("audio-input").addEventListener("change", (e) => {
    setSpeechFile(e.target.files[0]);
    e.target.value = "";
  });
  renderSource();

  if (cfg.backend === "fake") {
    configBanner = { kind: "warn", text: "Демо-режим: модель НЕ запускается, видео — картинка персонажа под звук. Для настоящей анимации запусти make run." };
  } else if (!installed.length) {
    configBanner = { kind: "warn", text: `SadTalker не установлен — видео не подготовить. ${cfg.install_hint}` };
  }
  setBanner(null);
  $("composer").addEventListener("submit", submit);
  try {
    await loadCharacters();
  } catch (err) {
    setBanner(err.message, "error");
  }
  renderJobs();
  jobsPoll.start();
}

init();
