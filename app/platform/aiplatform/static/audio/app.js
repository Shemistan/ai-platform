// Вкладка «Аудио»: текст → mp3 через движок narrator. Произношение на лету, тексты из библиотеки
// и md-файлов, очередь, проверка распознаванием, образцы голосов.
import { $, busyClick, chip, debounce, errorState, fill, followTask, h, logBlock, modal, narratorApi as api, countOf, fmtNum, plural, poller, progressBar, safe, seconds, shortDate, statusChip, taskCard } from "/ui.js";

const API = "/api/audio";
const TEMPOS = [1, 1.25, 1.5, 1.75, 2];
const SETTINGS_KEY = "aip.audio.settings";
// tempo — скорость плеера и скачиваемого mp3; одна на все задачи.
const state = { config: null, jobs: [], selected: null, tempo: 1, batch: null, previewSeq: 0 };

// --- настройки, которые помнит браузер ---

function remembered() {
  try {
    return JSON.parse(localStorage.getItem(SETTINGS_KEY) || "{}");
  } catch {
    return {};
  }
}

function remember() {
  const value = { engine: $("engine").value, voice: $("voice").value, speed: $("speed").value, steps: $("steps").value, verify: $("verify").checked };
  try {
    localStorage.setItem(SETTINGS_KEY, JSON.stringify(value));
  } catch {
    /* приватное окно — настройки просто не запомнятся */
  }
}

function engine() {
  return state.config.engines.find((e) => e.id === $("engine").value);
}

function fillEngines(saved) {
  const ready = state.config.engines.filter((e) => !e.problem);
  const wanted = saved.engine || state.config.default_engine;
  const pick = state.config.engines.find((e) => e.id === wanted && !e.problem) || ready[0] || state.config.engines[0];
  fill($("engine"), 
    ...state.config.engines.map((e) =>
      h("option", { value: e.id, selected: e.id === pick.id, disabled: Boolean(e.problem) }, e.problem ? `${e.label} — недоступен` : e.label),
    ),
  );
}

function fillVoices(saved = {}) {
  const e = engine();
  const voice = e.voices.includes(saved.voice) ? saved.voice : e.default_voice;
  fill($("voice"), ...e.voices.map((v) => h("option", { value: v, selected: v === voice }, v)));
  $("speed-field").hidden = !e.speed;
  $("steps-field").hidden = !e.steps;
  $("steps").placeholder = e.default_steps ? `${e.default_steps} по умолчанию` : "";
  $("voices-btn").hidden = !e.voices.includes("Charon");
  const note = $("engine-note");
  note.textContent = e.note;
  // Почему движок не запустится — понятной фразой, сырой вывод под «Подробностями».
  let slot = $("engine-problem");
  if (!slot) {
    slot = h("div", { id: "engine-problem" });
    note.after(slot);
  }
  const listError = state.config.engines_error;
  fill(
    slot,
    e.problem
      ? errorState({ title: `${e.label}: не запустится`, error: e.problem })
      : listError
        ? errorState({ title: "Движок не ответил — список движков может быть неполным", error: listError, onRetry: refreshConfig })
        : null,
  );
}

// --- произношение ---

const refreshPreview = debounce(async () => {
  const text = $("text").value;
  const seq = ++state.previewSeq;
  if (!text.trim()) {
    $("pronunciation").hidden = true;
    return;
  }
  try {
    const data = await api("POST", `${API}/preview`, { text });
    if (seq !== state.previewSeq) return;
    renderPreview(data);
  } catch {
    $("pronunciation").hidden = true;
  }
}, 600);

function renderPreview(data) {
  const parts = [];
  parts.push(data.changed ? `${data.changed} ${plural(data.changed, "строка переведена", "строки переведены", "строк переведено")} в произношение` : "текст уже готов к озвучке");
  if (data.issues.length) parts.push(`${data.issues.length} ${plural(data.issues.length, "замечание", "замечания", "замечаний")}`);
  $("pron-summary").textContent = `Как прочитает модель: ${parts.join(" · ")}`;
  $("pron-text").textContent = data.script;
  fill($("pron-remarks"), ...data.issues.map((i) => h("li", { class: "muted" }, `строка ${i.line}: ${i.message}`)));
  $("pronunciation").hidden = false;
}

function updateCount() {
  $("text-count").textContent = `${countOf($("text").value.length, "знак", "знака", "знаков")} из ${fmtNum(state.config.max_text)}`;
}

function setText(text) {
  $("text").value = text;
  updateCount();
  refreshPreview();
  $("text").focus();
}

// --- тексты из библиотеки и файлов ---

async function openLibrary() {
  const search = h("input", { type: "text", placeholder: "Поиск по названию", "aria-label": "Поиск" });
  const list = h("div", { class: "list" }, h("p", { class: "muted" }, "Загружаю…"));
  const { close } = modal("Текст из библиотеки", h("p", { class: "hint" }, "Статьи с текстом для озвучки и пересказы дайджестов."), search, list);
  let items = [];
  try {
    items = (await api("GET", `${API}/library`)).items;
  } catch (err) {
    fill(list, errorState({ title: "Библиотека не загрузилась", error: err, onRetry: () => (close(), openLibrary()) }));
    return;
  }
  const render = () => {
    const q = search.value.trim().toLowerCase();
    const shown = items.filter((i) => !q || i.title.toLowerCase().includes(q));
    const groups = [
      ["digest", "Дайджесты"],
      ["article", "Статьи"],
    ];
    const out = [];
    for (const [kind, label] of groups) {
      const part = shown.filter((i) => i.kind === kind);
      if (!part.length) continue;
      out.push(h("p", { class: "group-title" }, label));
      for (const item of part) {
        out.push(
          h(
            "button",
            {
              type: "button",
              class: "list-row",
              onclick: busyClick(async () => {
                const data = await api("GET", `${API}/library/${encodeURIComponent(item.id)}`);
                close();
                setText(data.text);
              }),
            },
            h("span", {}, item.title),
            h("span", { class: "muted small" }, shortDate(item.added)),
          ),
        );
      }
    }
    fill(list, ...(out.length ? out : [h("p", { class: "empty" }, q ? "Ничего не нашлось." : "В библиотеке пока нет текстов.")]));
  };
  search.addEventListener("input", render);
  render();
  search.focus();
}

async function handleFiles(files) {
  const md = [...files].filter((f) => /\.(md|markdown|txt)$/i.test(f.name) || f.type.startsWith("text/"));
  if (!md.length) {
    showError("Нужны текстовые файлы: .md или .txt.");
    return;
  }
  const articles = [];
  try {
    for (const file of md) {
      const { articles: parts } = await api("POST", `${API}/split`, { text: await file.text() });
      for (const part of parts) articles.push({ ...part, file: file.name });
    }
  } catch (err) {
    showError(err.message);
    return;
  }
  if (articles.length === 1) {
    setText(articles[0].text);
    return;
  }
  state.batch = { articles, files: md.map((f) => f.name) };
  renderBatch();
}

function renderBatch() {
  const box = $("batch");
  const batch = state.batch;
  if (!batch) {
    box.hidden = true;
    return;
  }
  const max = state.config.max_batch;
  const over = batch.articles.length > max;
  fill(box, 
    h(
      "div",
      { class: "card-head" },
      h("h3", {}, `Найдено ${batch.articles.length} ${plural(batch.articles.length, "статья", "статьи", "статей")}`),
      h("span", { class: "muted small" }, batch.files.join(", ")),
    ),
    h(
      "ol",
      { class: "list" },
      ...batch.articles.map((a, n) => h("li", { class: "list-row", style: "cursor:default" }, h("span", {}, `${n + 1}. ${a.title || "Без названия"}`), h("span", { class: "muted small" }, countOf(a.words, "слово", "слова", "слов")))),
    ),
    over ? h("p", { class: "form-error" }, `За раз — не больше ${max} ${plural(max, "статьи", "статей", "статей")}.`) : null,
    h("p", { class: "hint" }, "Каждая статья встанет в очередь отдельной задачей с настройками ниже — по порядку."),
    h(
      "div",
      { class: "toolbar" },
      h("button", { type: "button", class: "btn btn--primary", disabled: over, onclick: busyClick(submitBatch, "batch") }, `Все в очередь (${batch.articles.length})`),
      h("button", { type: "button", class: "btn btn--ghost btn--sm", onclick: () => ((state.batch = null), renderBatch()) }, "Отмена"),
    ),
  );
  box.hidden = false;
}

// --- отправка ---

function settings() {
  const e = engine();
  const out = { engine: e.id, voice: $("voice").value, verify: state.config.verify_available && $("verify").checked };
  if (e.speed) out.speed = Number($("speed").value);
  if (e.steps && $("steps").value) out.steps = Number($("steps").value);
  return out;
}

function showError(message) {
  const error = $("form-error");
  error.textContent = message;
  error.hidden = !message;
}

async function submit(event) {
  event.preventDefault();
  showError("");
  $("submit").disabled = true;
  try {
    state.selected = await api("POST", `${API}/jobs`, { text: $("text").value, ...settings() });
    renderJob(state.selected);
    state.jobs = [state.selected, ...state.jobs];
    renderHistory();
    remember();
    jobsPoll.later(); // задача в очереди — опрашивать чаще
  } catch (err) {
    showError(err.message);
  } finally {
    $("submit").disabled = false;
  }
}

async function submitBatch() {
  showError("");
  try {
    const articles = state.batch.articles.map((a) => ({ title: a.title, text: a.text }));
    const { jobs } = await api("POST", `${API}/jobs/batch`, { articles, ...settings() });
    state.batch = null;
    renderBatch();
    state.jobs = [...jobs.slice().reverse(), ...state.jobs];
    state.selected = jobs[0];
    renderJob(state.selected);
    renderHistory();
    remember();
    jobsPoll.later();
  } catch (err) {
    showError(err.message);
  }
}

// --- результат ---

function downloadHref(job) {
  const query = state.tempo === 1 ? "download=1" : `download=1&speed=${state.tempo}`;
  return `${job.audio_url}?${query}`;
}

// Скорость берётся и с кнопок, и из меню самого плеера; в mp3 — в пределах tempo_range.
function setTempo(rate, box) {
  const [low, high] = state.config.tempo_range;
  state.tempo = Math.round(Math.min(high, Math.max(low, rate)) * 100) / 100;
  const audio = box.querySelector("audio");
  if (audio && audio.playbackRate !== state.tempo) audio.playbackRate = state.tempo;
  for (const b of box.querySelectorAll(".tempo button")) b.setAttribute("aria-pressed", String(Number(b.value) === state.tempo));
  const link = box.querySelector("a[data-download]");
  if (link) {
    link.href = downloadHref(state.selected);
    link.textContent = state.tempo === 1 ? "Скачать mp3" : `Скачать mp3 ×${state.tempo}`;
  }
}

function player(job, box) {
  const audio = h("audio", { controls: true, src: job.audio_url, preload: "metadata" });
  audio.playbackRate = audio.defaultPlaybackRate = state.tempo;
  audio.addEventListener("ratechange", () => audio.playbackRate !== state.tempo && setTempo(audio.playbackRate, box));
  const tempo = h(
    "div",
    { class: "segmented tempo", role: "group", "aria-label": "Скорость" },
    h("span", { class: "muted small" }, "Скорость"),
    ...TEMPOS.map((t) => h("button", { type: "button", value: String(t), "aria-pressed": String(t === state.tempo), onclick: () => setTempo(t, box) }, `×${t}`)),
  );
  return [audio, tempo];
}

function marked(text, missed) {
  const lost = new Set(missed.map((w) => w.toLowerCase()));
  return text.split(/(\s+)/).map((part) => {
    const word = part.toLowerCase().replace(/[^\p{L}\p{N}-]/gu, "");
    return word && lost.has(word) ? h("mark", {}, part) : part;
  });
}

function checkBlock(check) {
  if (!check) return null;
  if (check.skipped) return h("p", { class: "hint warn" }, `Проверка пропущена: ${check.skipped}`);
  const score = Math.round(check.overall * 100);
  const threshold = Math.round(check.threshold * 100);
  const ok = check.overall >= check.threshold;
  return h(
    "div",
    { class: "card" },
    h(
      "div",
      { class: "check-head" },
      h("span", { class: `score ${ok ? "ok" : "warn"}` }, `${score}%`),
      h("span", { class: "muted" }, "совпадение по словам"),
      check.weak.length
        ? chip(`${check.weak.length} ${plural(check.weak.length, "абзац", "абзаца", "абзацев")} ниже ${threshold}%`, "warn")
        : chip("все абзацы чисто", "ok"),
    ),
    check.weak.length ? h("p", { class: "hint" }, "Распознавание тоже ошибается: это повод переслушать место, а не приговор. Подсвечены слова, которых не услышали.") : null,
    ...check.weak.map((w) =>
      h(
        "div",
        { class: "weak" },
        h("div", { class: "weak-head" }, h("span", {}, `Строка ${w.line}`), h("span", {}, `${Math.round(w.score * 100)}%`)),
        h("div", {}, ...marked(w.text, w.missed)),
        h("div", { class: "muted small" }, `услышано: ${w.heard || "—"}`),
      ),
    ),
  );
}

function renderJob(job) {
  const box = $("job");
  if (!job) {
    fill(box, h("p", { class: "muted" }, "Здесь появится озвучка."));
    return;
  }
  const active = job.status === "queued" || job.status === "running";
  const meta = [job.engine_label, job.voice, job.speed ? `×${job.speed}` : "", job.steps ? countOf(job.steps, "шаг", "шага", "шагов") : "", countOf(job.chars, "знак", "знака", "знаков")].filter(Boolean).join(" · ");
  fill(box, 
    h(
      "div",
      { class: "job" },
      h("div", { class: "job-head" }, statusChip(job.status), h("span", { class: "muted small" }, meta)),
      job.title ? h("h3", {}, job.title) : null,
      h("p", { class: "job-text" }, job.preview),
      active ? progressBar(job.progress) : null,
      job.queue_position ? h("p", { class: "muted small" }, `Перед ней в очереди: ${job.queue_position}`) : null,
      job.error ? h("p", { class: "job-error" }, job.error) : null,
      ...(job.audio_url ? player(job, box) : []),
      h(
        "div",
        { class: "actions" },
        job.audio_url ? h("a", { class: "btn btn--link", href: downloadHref(job), "data-download": "" }, state.tempo === 1 ? "Скачать mp3" : `Скачать mp3 ×${state.tempo}`) : null,
        job.duration_s ? h("span", { class: "muted small" }, `Длительность ${seconds(job.duration_s)}`) : null,
        job.elapsed_s ? h("span", { class: "muted small" }, `Озвучка заняла ${seconds(job.elapsed_s)}`) : null,
        job.text ? h("button", { type: "button", class: "btn btn--link", onclick: () => setText(job.text) }, "Текст в редактор") : null,
        active ? h("button", { type: "button", class: "btn btn--link btn--danger", onclick: busyClick(() => cancel(job.id), `cancel:${job.id}`) }, "Отменить") : null,
      ),
      checkBlock(job.check),
      logBlock(job.log),
    ),
  );
}

function renderHistory() {
  fill($("history"), 
    ...(state.jobs.length
      ? state.jobs.map((job) =>
          h(
            "li",
            {},
            h(
              "button",
              { type: "button", class: `history-item${state.selected?.id === job.id ? " current" : ""}`, onclick: safe(() => select(job.id)) },
              h(
                "span",
                { class: "history-text" },
                h("span", { class: "history-title" }, job.title || job.preview),
                h("span", { class: "muted small" }, [job.engine_label, job.voice, job.check && !job.check.skipped ? `проверка ${Math.round(job.check.overall * 100)}%` : ""].filter(Boolean).join(" · ")),
              ),
              statusChip(job.status),
            ),
          ),
        )
      : [h("li", { class: "muted" }, "Пока пусто.")]),
  );
}

async function select(id) {
  state.selected = await api("GET", `${API}/jobs/${id}`);
  renderJob(state.selected);
  renderHistory();
}

async function cancel(id) {
  state.selected = await api("POST", `${API}/jobs/${id}/cancel`);
  renderJob(state.selected);
}

// Опрос очереди: раз в секунду, пока есть активные задачи, раз в 5 с в простое (poller из ui.js:
// в скрытой вкладке — раз в минуту, после сбоя связи — реже, без «Uncaught (in promise)»).
const isRunning = (j) => j.status === "queued" || j.status === "running";
const jobsPoll = poller({ run: poll, fast: 1000, slow: 5000, isActive: () => state.jobs.some(isRunning) });

async function poll(signal) {
  const { jobs } = await api("GET", `${API}/jobs`, undefined, { signal });
  state.jobs = jobs;
  if (state.selected) {
    const fresh = jobs.find((j) => j.id === state.selected.id);
    if (fresh && (fresh.status !== state.selected.status || isRunning(fresh))) {
      state.selected = await api("GET", `${API}/jobs/${fresh.id}`, undefined, { signal });
      renderJob(state.selected);
    }
  }
  renderHistory();
}

// --- образцы голосов ---

async function openVoices() {
  const content = h("div", { class: "list" }, h("p", { class: "muted" }, "Загружаю…"));
  const taskBox = h("div", {});
  modal(
    "Образцы голосов",
    h("p", { class: "hint" }, "Голоса-клоны (ESpeech и модели MLX) повторяют голос образца: 3–12 секунд речи и её точный текст. Новый образец главнее встроенного и сразу доступен в списке голосов."),
    taskBox,
    content,
  );
  const reload = async () => {
    try {
      renderVoices(await api("GET", `${API}/voices`), content, taskBox, reload);
    } catch (err) {
      fill(content, errorState({ title: "Образцы не загрузились", error: err, onRetry: reload }));
    }
  };
  await reload();
}

function renderVoices(data, content, taskBox, reload) {
  const busy = Boolean(data.task);
  if (data.task) watchVoiceTask(data.task, taskBox, reload);
  fill(content, 
    ...data.voices.map((v) => {
      const tone = v.custom ? "info" : v.sample_url ? "" : "warn";
      const status = v.custom ? "записан вами" : v.sample_url ? "встроенный" : "не записан";
      const record = recordControls(v, data, busy, taskBox, reload);
      return h(
        "div",
        { class: "card" },
        h("div", { class: "card-head" }, h("h3", {}, v.voice), h("span", { class: "toolbar" }, v.seconds ? h("span", { class: "muted small" }, `${v.seconds} с`) : null, chip(status, tone))),
        v.sample_url ? h("audio", { controls: true, preload: "none", src: v.sample_url }) : null,
        v.text ? h("blockquote", { class: "quote" }, v.text) : null,
        record,
      );
    }),
  );
}

function recordControls(v, data, busy, taskBox, reload) {
  if (!v.record) return h("p", { class: "hint" }, "Образец из Supertonic — не перезаписывается.");
  const [low, high] = data.seconds_range;
  if (v.record === "gemini") {
    const text = h("textarea", { rows: 2, placeholder: `Свой текст образца — по желанию; иначе: «${data.default_text}»` });
    return h(
      "details",
      { class: "inline" },
      h("summary", {}, "Перезаписать образец"),
      h(
        "div",
        { class: "field", style: "margin-top:var(--sp-2)" },
        h("p", { class: "hint" }, "Голос Charon есть в Gemini TTS: движок сам запишет образец по ключу GEMINI_API_KEY. Можно и своей записью — загрузи её ниже."),
        text,
        h("div", { class: "toolbar" }, h("button", { type: "button", class: "btn btn--secondary", disabled: busy, onclick: busyClick(() => startRecord(v.stem, { text: text.value }, taskBox, reload), `voice:${v.stem}`) }, "Записать из Gemini TTS")),
        fileRecord(v, low, high, busy, taskBox, reload),
      ),
    );
  }
  return h("details", { class: "inline" }, h("summary", {}, "Записать свой образец"), fileRecord(v, low, high, busy, taskBox, reload));
}

function fileRecord(v, low, high, busy, taskBox, reload) {
  const file = h("input", { type: "file", accept: "audio/*,.wav,.mp3,.m4a" });
  const text = h("textarea", { rows: 2, placeholder: "Что сказано в записи — слово в слово, цифры словами" });
  const error = h("p", { class: "form-error", hidden: true });
  const save = async () => {
    error.hidden = true;
    if (!file.files[0] || !text.value.trim()) {
      error.textContent = "Нужны запись и её текст.";
      error.hidden = false;
      return;
    }
    try {
      const upload = await api("POST", `${API}/uploads`, file.files[0]);
      await startRecord(v.stem, { upload: upload.id, text: text.value }, taskBox, reload);
    } catch (err) {
      error.textContent = err.message;
      error.hidden = false;
    }
  };
  return h(
    "div",
    { class: "field", style: "margin-top:var(--sp-2)" },
    h("span", { class: "hint" }, `Запись голоса ${v.voice}: ${low}–${high} с ровной речи, без музыки и шума.`),
    file,
    text,
    error,
    h("div", { class: "toolbar" }, h("button", { type: "button", class: "btn btn--secondary", disabled: busy, onclick: busyClick(save, `voice:${v.stem}`) }, "Сохранить образец")),
  );
}

async function startRecord(stem, payload, taskBox, reload) {
  try {
    const task = await api("POST", `${API}/voices/${stem}`, payload);
    watchVoiceTask(task, taskBox, reload);
  } catch (err) {
    fill(taskBox, h("p", { class: "job-error" }, err.message));
  }
}

async function watchVoiceTask(task, taskBox, reload) {
  if (taskBox.dataset.watching === task.id) return;
  taskBox.dataset.watching = task.id;
  let final;
  try {
    final = await followTask(`${API}/tasks/${task.id}`, (t) => fill(taskBox, taskCard(t, { full: true })));
  } catch (err) {
    fill(taskBox, h("p", { class: "job-error" }, err.message));
    return;
  } finally {
    delete taskBox.dataset.watching;
  }
  if (final.status === "done") {
    await reload();
    await refreshConfig();
  }
}

// --- запуск ---

async function refreshConfig() {
  const saved = { engine: $("engine").value, voice: $("voice").value };
  state.config = await api("GET", `${API}/config`);
  fillEngines(saved);
  fillVoices(saved);
}

function bindDrop() {
  const zone = $("text");
  zone.addEventListener("dragover", (e) => {
    if (![...e.dataTransfer.types].includes("Files")) return;
    e.preventDefault();
    zone.classList.add("drop-target");
  });
  zone.addEventListener("dragleave", () => zone.classList.remove("drop-target"));
  zone.addEventListener("drop", (e) => {
    zone.classList.remove("drop-target");
    if (!e.dataTransfer.files.length) return;
    e.preventDefault();
    handleFiles(e.dataTransfer.files);
  });
}

async function init() {
  state.config = await api("GET", `${API}/config`);
  const saved = remembered();
  fillEngines(saved);
  fillVoices(saved);
  const [low, high] = state.config.speed_range;
  Object.assign($("speed"), { min: low, max: high });
  if (saved.speed) $("speed").value = saved.speed;
  $("speed-out").textContent = $("speed").value;
  if (saved.steps) $("steps").value = saved.steps;
  const [sLow, sHigh] = state.config.steps_range;
  Object.assign($("steps"), { min: sLow, max: sHigh });
  $("verify-field").hidden = !state.config.verify_available;
  $("verify").checked = saved.verify ?? true;

  $("engine").addEventListener("change", () => (fillVoices(), remember()));
  for (const id of ["voice", "steps", "verify"]) $(id).addEventListener("change", remember);
  $("speed").addEventListener("input", () => ($("speed-out").textContent = $("speed").value));
  $("speed").addEventListener("change", remember);
  $("text").addEventListener("input", () => (updateCount(), refreshPreview()));
  $("from-library").addEventListener("click", openLibrary);
  $("from-files").addEventListener("click", () => $("files").click());
  $("files").addEventListener("change", () => {
    if ($("files").files.length) handleFiles($("files").files);
    $("files").value = "";
  });
  $("voices-btn").addEventListener("click", openVoices);
  $("composer").addEventListener("submit", submit);
  bindDrop();
  updateCount();
  jobsPoll.start();
}

init().catch((err) => {
  fill($("job"), errorState({ title: "Не удалось загрузить настройки озвучки", error: err, onRetry: () => location.reload() }));
});
