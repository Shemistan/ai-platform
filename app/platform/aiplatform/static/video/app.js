// Вкладка «Видео» (перенос UI videogen): промпт, лента кадров, живое фото, запуск и статус генерации. Без сборки и зависимостей.

import { $, api, chip, h, icon, iconButton, jobProgress, poller, seconds as fmtElapsed, STATUS, statusChip, toneOf, UPLOAD_LIMITS, uploadFile } from "/ui.js";
import { frameAt, frameCount, frameProblems as problemsOf, keyframeLimit, spread } from "./frames.js";

const state = {
  config: null,
  frames: [], // {key, name, preview, uploadId, uploading, error, frameIdx}
  manual: false, // позиции правили руками — не пересчитываем автоматически
  duration: 4,
  jobs: [],
  selectedId: null,
  submitting: false,
  living: null, // фото вкладки «Живое фото»: {name, preview, uploadId, uploading, error}
  livingOrientation: null,
  livingQuality: null,
  livingSubmitting: false,
};
let nextKey = 1;
let dragKey = null;

// Частота кадров, длительности, кадры и звук зависят от выбранной модели.
const model = () => state.config.models.find((m) => m.id === $("model").value) || state.config.models[0];
const fps = () => model().fps;
const numFrames = () => frameCount(state.duration, fps());
const maxFrames = () => keyframeLimit(numFrames(), state.config.min_keyframe_gap, model().max_keyframes);
const seconds = (frameIdx) => frameIdx / fps();
const fmtSec = (value) => `${+value.toFixed(2)} с`;

// ---------- кадры ----------

function distribute() {
  const at = spread(state.frames.length, numFrames() - 1);
  state.frames.forEach((frame, i) => {
    frame.frameIdx = at[i];
  });
}

function relayout({ keepManual = false } = {}) {
  if (!keepManual) state.manual = false;
  if (!state.manual) distribute();
  renderFrames();
}

function frameProblems() {
  return problemsOf(state.frames, { gap: state.config.min_keyframe_gap, last: numFrames() - 1, fps: fps() });
}

function roleOf(index) {
  if (index === 0) return "Начало";
  const frame = state.frames[index];
  return frame.frameIdx === numFrames() - 1 ? "Конец" : "Промежуточный";
}

async function addFiles(files) {
  const room = maxFrames() - state.frames.length;
  const accepted = [...files].filter((f) => f.type.startsWith("image/")).slice(0, room);
  if (!accepted.length) return;
  const added = accepted.map((file) => ({
    key: nextKey++,
    name: file.name,
    preview: URL.createObjectURL(file),
    uploadId: null,
    uploading: true,
    error: null,
    frameIdx: 0,
    file,
  }));
  state.frames.push(...added);
  relayout();
  await Promise.all(
    added.map(async (frame) => {
      try {
        frame.uploadId = (await uploadFile("/api/video/uploads", frame.file, { limit: (state.config?.upload_limits || UPLOAD_LIMITS).image })).id;
      } catch (err) {
        if (err.kind === "abort") return removeFrame(frame.key); // отменили — кадра как не было
        frame.error = err.message;
      }
      frame.uploading = false;
      delete frame.file;
      renderFrames();
    }),
  );
}

function removeFrame(key) {
  const index = state.frames.findIndex((f) => f.key === key);
  if (index < 0) return;
  URL.revokeObjectURL(state.frames[index].preview);
  state.frames.splice(index, 1);
  relayout();
}

function moveFrame(key, toIndex) {
  const from = state.frames.findIndex((f) => f.key === key);
  if (from < 0 || toIndex < 0 || toIndex >= state.frames.length || from === toIndex) return;
  const [frame] = state.frames.splice(from, 1);
  state.frames.splice(toIndex, 0, frame);
  relayout();
}

function setFrameTime(key, value) {
  const frame = state.frames.find((f) => f.key === key);
  const at = frameAt(value, fps());
  if (!frame || at === null) return renderFrames();
  frame.frameIdx = at;
  state.manual = true;
  renderFrames();
}

function frameCard(frame, index, problem) {
  const count = state.frames.length;
  const timeInput = h("input", {
    type: "text",
    inputmode: "decimal",
    class: "time-input",
    value: seconds(frame.frameIdx).toFixed(2),
    disabled: index === 0,
    "aria-label": `Время кадра ${index + 1}, секунды`,
    onchange: (e) => setFrameTime(frame.key, e.target.value),
  });
  return h(
    "li",
    {
      class: `frame-card${problem ? " bad" : ""}${frame.uploading ? " busy" : ""}`,
      draggable: "true",
      ondragstart: (e) => {
        dragKey = frame.key;
        e.dataTransfer.effectAllowed = "move";
        e.dataTransfer.setData("text/plain", String(frame.key));
      },
      ondragend: () => (dragKey = null),
      ondragover: (e) => {
        if (dragKey != null && dragKey !== frame.key) e.preventDefault();
      },
      ondrop: (e) => {
        if (dragKey == null) return;
        e.preventDefault();
        e.stopPropagation();
        moveFrame(dragKey, index);
      },
    },
    h("div", { class: "thumb" },
      h("img", { src: frame.preview, alt: frame.name, draggable: false }),
      h("span", { class: "badge" }, String(index + 1)),
      iconButton("close", `Убрать кадр ${index + 1}`, { class: "btn-icon--overlay remove", title: "Убрать кадр", onclick: () => removeFrame(frame.key) }),
    ),
    h("div", { class: "frame-card-body" },
      h("span", { class: "role" }, frame.uploading ? "Загрузка…" : roleOf(index)),
      h("label", { class: "time" }, timeInput, h("span", {}, "с")),
      h("div", { class: "order" },
        iconButton("prev", `Кадр ${index + 1} раньше`, { class: "btn-icon--outline", title: "Раньше", disabled: index === 0, onclick: () => moveFrame(frame.key, index - 1) }),
        iconButton("next", `Кадр ${index + 1} позже`, { class: "btn-icon--outline", title: "Позже", disabled: index === count - 1, onclick: () => moveFrame(frame.key, index + 1) }),
      ),
    ),
    problem && h("p", { class: "card-error" }, problem),
  );
}

function renderFrames() {
  const strip = $("strip");
  const problems = frameProblems();
  const items = [];
  state.frames.forEach((frame, index) => {
    if (index > 0) items.push(h("li", { class: "arrow", "aria-hidden": "true" }, icon("arrow")));
    items.push(frameCard(frame, index, problems[index]));
  });
  if (state.frames.length < maxFrames()) {
    if (state.frames.length) items.push(h("li", { class: "arrow", "aria-hidden": "true" }, icon("arrow")));
    items.push(
      h("li", { class: "add" },
        h("button", { type: "button", class: "add-tile", onclick: () => $("file-input").click() },
          h("span", { class: "plus" }, icon("plus", { size: 20 })),
          h("span", {}, state.frames.length ? "Следующий кадр" : "Добавить кадр"),
          h("span", { class: "muted small" }, "или перетащи фото сюда"),
        ),
      ),
    );
  }
  strip.replaceChildren(...items);

  const count = state.frames.length;
  $("frames-hint").textContent =
    maxFrames() === 0
      ? `${model().label} работает только по промпту: кадры ей не передаются. Для ролика по фото выбери LTX.`
      : count === 0
      ? "Без кадров видео строится только по промпту. Одно фото станет первым кадром, несколько — ключевыми кадрами по порядку. Фото обрезается по центру под размер кадра."
      : count === 1
        ? "Это фото станет первым кадром ролика. Добавь ещё — они пойдут следом, слева направо."
        : count <= 3
          ? "Ролик пройдёт через кадры слева направо. Порядок меняется перетаскиванием или стрелками, время — в поле под кадром."
          : "Кадров больше трёх: на LTX-2.3 Q4 картинка от этого заметно портится — персонаж смазывается на каждом кадре. Лучше оставить 2–3.";
  $("even-btn").hidden = !state.manual;
  renderRuler(problems);
  renderSubmit();
}

function renderRuler(problems) {
  const ruler = $("ruler");
  ruler.hidden = state.frames.length === 0;
  if (ruler.hidden) return;
  const last = numFrames() - 1;
  $("ruler-track").replaceChildren(
    ...state.frames.map((frame, index) =>
      h("span", {
        class: `marker${problems[index] ? " bad" : ""}`,
        style: `left:${Math.min(100, (frame.frameIdx / last) * 100)}%`,
        title: `Кадр ${index + 1} — ${fmtSec(seconds(frame.frameIdx))}`,
      }, String(index + 1)),
    ),
  );
  $("ruler-ticks").replaceChildren(
    ...Array.from({ length: state.duration + 1 }, (_, s) =>
      h("span", { style: `left:${(s / state.duration) * 100}%` }, `${s} с`),
    ),
  );
}

function renderSubmit() {
  const uploading = state.frames.some((f) => f.uploading);
  const invalid = frameProblems().some(Boolean);
  const button = $("submit");
  button.disabled = state.submitting || uploading || invalid;
  button.textContent = state.submitting ? "Отправляю…" : uploading ? "Кадры загружаются…" : "Сгенерировать";
}

// ---------- запуск ----------

function showFormError(text) {
  $("form-error").textContent = text || "";
  $("form-error").hidden = !text;
}

async function submit(event) {
  event.preventDefault();
  showFormError("");
  const prompt = $("prompt").value.trim();
  if (!prompt) {
    showFormError("Напиши промпт — описание того, что должно происходить в видео.");
    $("prompt").focus();
    return;
  }
  const seedRaw = $("seed").value.trim();
  state.submitting = true;
  renderSubmit();
  try {
    const job = await api("POST", "/api/video/jobs", {
      prompt,
      duration_s: state.duration,
      preset: $("preset").value,
      model: $("model").value,
      seed: seedRaw === "" ? null : Number(seedRaw),
      audio: model().audio && $("audio").checked,
      frames: state.frames.map((f) => ({ upload_id: f.uploadId, frame_idx: f.frameIdx })),
    });
    state.selectedId = job.id;
    upsertJob(job);
    renderJobs();
    schedulePoll();
  } catch (err) {
    showFormError(err.message);
  } finally {
    state.submitting = false;
    renderSubmit();
  }
}

// ---------- живое фото ----------

const livingSize = () =>
  state.config.living.qualities.find((q) => q.id === state.livingQuality).sizes[state.livingOrientation];

function segments(root, name, options, current, onPick) {
  $(root).replaceChildren(
    ...options.map((option) =>
      h("label", { class: "seg", title: option.title },
        h("input", { type: "radio", name, value: option.id, checked: option.id === current, onchange: () => onPick(option.id) }),
        h("span", {}, option.label),
      ),
    ),
  );
}

// Размер кадра для подписи: [704, 448] → «704×448».
const frameSize = ([w, hgt]) => `${w}×${hgt}`;

function renderLivingOptions() {
  const cfg = state.config.living;
  segments("living-orientations", "living-orientation", cfg.orientations, state.livingOrientation, (id) => {
    state.livingOrientation = id;
    renderLivingOptions();
  });
  const qualities = cfg.qualities.map((q) => ({ ...q, title: frameSize(q.sizes[state.livingOrientation]) }));
  segments("living-qualities", "living-quality", qualities, state.livingQuality, (id) => {
    state.livingQuality = id;
    renderLivingOptions();
  });
  const [width, height] = livingSize();
  const slow = state.livingQuality === cfg.default_quality ? "" : " Чем выше качество, тем дольше генерация и тем больше нужно памяти.";
  $("living-note").textContent = `Ролик будет ${width}×${height}; если пропорции фото другие, края обрежутся по центру.${slow}`;
}

async function setLivingFile(file) {
  if (!file || !file.type.startsWith("image/")) return;
  clearLiving();
  const photo = { name: file.name, preview: URL.createObjectURL(file), uploadId: null, uploading: true, error: null };
  state.living = photo;
  showLivingError("");
  renderLiving();
  try {
    photo.uploadId = (await uploadFile("/api/video/uploads", file, { limit: (state.config?.upload_limits || UPLOAD_LIMITS).image })).id;
  } catch (err) {
    if (err.kind === "abort") return state.living === photo && clearLiving(); // отменили — как до выбора
    photo.error = err.message;
  }
  photo.uploading = false;
  if (state.living === photo) renderLiving();
}

function clearLiving() {
  if (state.living) URL.revokeObjectURL(state.living.preview);
  state.living = null;
  renderLiving();
}

function showLivingError(text) {
  $("living-error").textContent = text || "";
  $("living-error").hidden = !text;
}

function renderLiving() {
  const photo = state.living;
  const box = $("living-photo");
  if (!photo) {
    box.replaceChildren(
      h("button", { type: "button", class: "add-tile", onclick: () => $("living-input").click() },
        h("span", { class: "plus" }, icon("plus", { size: 20 })),
        h("span", {}, "Выбрать фото"),
        h("span", { class: "muted small" }, "или перетащи его сюда"),
      ),
    );
  } else {
    box.replaceChildren(
      h("div", { class: `photo-frame${photo.uploading ? " busy" : ""}` },
        h("img", {
          src: photo.preview,
          alt: photo.name,
          // Ориентацию ролика предлагаем по снимку; переключатель остаётся за человеком.
          onload: (e) => {
            if (state.living !== photo || photo.measured) return;
            photo.measured = true;
            state.livingOrientation = e.target.naturalHeight > e.target.naturalWidth ? "portrait" : "landscape";
            renderLivingOptions();
          },
        }),
        iconButton("close", "Убрать фото", { class: "btn-icon--overlay remove", onclick: clearLiving }),
      ),
      ...(photo.error ? [h("p", { class: "card-error photo-note" }, photo.error)] : []),
    );
  }
  const unavailable = state.config.living.problem;
  if (unavailable) showLivingError(unavailable);
  const button = $("living-submit");
  button.disabled = Boolean(unavailable) || state.livingSubmitting || !photo?.uploadId;
  button.textContent = state.livingSubmitting ? "Отправляю…" : photo?.uploading ? "Фото загружается…" : "Оживить фото";
}

async function submitLiving(event) {
  event.preventDefault();
  const photo = state.living;
  if (!photo?.uploadId) return;
  showLivingError("");
  state.livingSubmitting = true;
  renderLiving();
  try {
    const job = await api("POST", "/api/video/living", {
      upload_id: photo.uploadId,
      model: $("living-model").value,
      orientation: state.livingOrientation,
      quality: state.livingQuality,
    });
    state.selectedId = job.id;
    upsertJob(job);
    renderJobs();
    schedulePoll();
  } catch (err) {
    showLivingError(err.message);
  } finally {
    state.livingSubmitting = false;
    renderLiving();
  }
}

// ---------- вкладки ----------

const TABS = { video: "composer", living: "living" };

function showTab(name) {
  if (!(name in TABS)) name = "video";
  for (const [tab, panel] of Object.entries(TABS)) {
    $(`tab-${tab}`).setAttribute("aria-selected", String(tab === name));
    $(panel).hidden = tab !== name;
  }
  history.replaceState(null, "", name === "video" ? location.pathname : `#${name}`);
}

// ---------- задачи ----------

const isLiving = (job) => job.kind === "living";
// У живого фото промпт служебный и один на все снимки — вместо него показываем название режима.
const jobTitle = (job) => (isLiving(job) ? "Живое фото" : job.prompt);

const isActive = (job) => job.status === "queued" || job.status === "running";

function upsertJob(job) {
  const index = state.jobs.findIndex((j) => j.id === job.id);
  if (index >= 0) state.jobs[index] = job;
  else state.jobs.unshift(job);
}

function filmstrip(job) {
  if (!job.frames.length) return h("p", { class: "muted small" }, "Без кадров — только по промпту");
  const items = [];
  job.frames.forEach((frame, index) => {
    if (index > 0) items.push(h("li", { class: "arrow", "aria-hidden": "true" }, icon("arrow")));
    items.push(
      h("li", { class: "mini" },
        h("img", { src: frame.url, alt: `Кадр ${index + 1}`, loading: "lazy" }),
        h("span", { class: "badge" }, String(index + 1)),
        h("span", { class: "mini-time" }, fmtSec(frame.time_s)),
      ),
    );
  });
  return h("ol", { class: "filmstrip" }, items);
}

function logBlock(job, open) {
  if (!job.log || !job.log.length) return null;
  const pre = h("pre", { class: "log" }, job.log.join("\n"));
  const details = h("details", { class: "disclosure log-wrap", open }, h("summary", {}, "Лог процесса"), pre);
  details.addEventListener("toggle", () => (pre.scrollTop = pre.scrollHeight));
  queueMicrotask(() => (pre.scrollTop = pre.scrollHeight));
  return details;
}

// muted — свойство, а не атрибут: без него браузер не даст автозапуск созданному из JS плееру.
function player(src) {
  const video = h("video", { class: "player", src, controls: true, loop: true, autoplay: true, playsinline: true });
  video.muted = true;
  return video;
}

let jobSignature = "";

function renderJob() {
  const root = $("job");
  const job = state.jobs.find((j) => j.id === state.selectedId);
  if (!job) {
    jobSignature = "";
    root.replaceChildren(h("p", { class: "empty" }, "Здесь появится статус генерации и готовое видео."));
    return;
  }
  const signature = JSON.stringify([job.id, job.status, job.progress, job.elapsed_s, job.queue_position, job.log?.length]);
  if (signature === jobSignature) return; // не трогаем DOM: иначе плеер сбросит воспроизведение
  jobSignature = signature;
  const logOpen = root.querySelector("details.log-wrap")?.open ?? job.status === "failed";

  const parts = [
    h("div", { class: "job-head" },
      statusChip(job.status),
      job.backend === "fake" ? chip("Демо — не модель", "warning") : null,
      h("span", { class: "muted small" }, `${job.model_label ? `${job.model_label} · ` : ""}${job.width}×${job.height} · ${job.duration_s} с · seed ${job.seed}${job.audio ? "" : " · без звука"}`),
    ),
    h("p", { class: "job-text" }, isLiving(job) ? "Живое фото: снимок стоит посередине ролика, до и после него — по половине." : job.prompt),
    filmstrip(job),
  ];
  if (isActive(job)) parts.push(jobProgress(job, { onCancel: (j) => cancelJob(j.id) }));
  if (job.status === "done") {
    parts.push(
      player(job.video_url),
      h("div", { class: "job-foot" },
        h("span", { class: "muted small" }, `Сгенерировано за ${fmtElapsed(job.elapsed_s)}`),
        h("a", { class: "btn btn--link", href: job.video_url, download: `video-${job.id}.mp4` }, "Скачать mp4"),
      ),
    );
  }
  if (job.status === "failed") parts.push(h("p", { class: "job-error", role: "alert" }, job.error));
  if (job.status === "canceled") parts.push(h("p", { class: "muted" }, "Генерация отменена."));
  parts.push(logBlock(job, logOpen));
  root.replaceChildren(...parts.filter(Boolean));
}

let historySignature = "";

function renderHistory() {
  const signature = JSON.stringify([state.selectedId, state.jobs.map((j) => [j.id, j.status, Math.round(j.progress.percent)])]);
  if (signature === historySignature) return;
  historySignature = signature;
  const list = $("history");
  if (!state.jobs.length) {
    list.replaceChildren(h("li", { class: "empty" }, "Пока пусто."));
    return;
  }
  list.replaceChildren(
    ...state.jobs.map((job) =>
      h("li", {},
        h("button", {
            type: "button",
            class: `history-item${job.id === state.selectedId ? " current" : ""}`,
            "aria-current": job.id === state.selectedId ? "true" : null,
            onclick: () => selectJob(job.id),
          },
          job.frames.length
            ? h("img", { class: "history-thumb", src: job.frames[0].url, alt: "", loading: "lazy" })
            : h("span", { class: "history-thumb text" }, "T"),
          h("span", { class: "history-text" },
            h("span", { class: "history-title" }, jobTitle(job)),
            h("span", { class: "muted small" },
              `${new Date(job.created_at * 1000).toLocaleString("ru-RU", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })} · ${isLiving(job) ? `${job.duration_s} с` : job.frames.length ? `кадров: ${job.frames.length}` : "без кадров"}`),
          ),
          chip(job.status === "running" ? `${Math.round(job.progress.percent)}%` : STATUS[job.status], toneOf(job.status)),
        ),
      ),
    ),
  );
}

function renderJobs() {
  renderJob();
  renderHistory();
}

async function selectJob(id) {
  state.selectedId = id;
  renderJobs();
  try {
    upsertJob(await api("GET", `/api/video/jobs/${id}`));
    renderJobs();
  } catch { /* задача останется в виде из списка */ }
}

async function cancelJob(id) {
  try {
    upsertJob(await api("POST", `/api/video/jobs/${id}/cancel`));
    renderJobs();
  } catch (err) {
    setBanner(err.message, "error");
  }
}

// Опрос очереди: раз в секунду, пока есть активные задачи, раз в 5 с в простое (poller из ui.js:
// в скрытой вкладке — раз в минуту, после сбоя связи — реже). Свои действия зовут schedulePoll().
const jobsPoll = poller({ run: (signal) => poll(signal), fast: 1000, slow: 5000, isActive: () => state.jobs.some(isActive), onError: (err) => setBanner(err.message, "error") });

function schedulePoll() {
  jobsPoll.now();
}

async function poll(signal) {
  const { jobs } = await api("GET", "/api/video/jobs", undefined, { signal });
  // Список приходит без лога; лог выбранной задачи берём отдельным запросом.
  const selected = jobs.find((j) => j.id === state.selectedId);
  const previous = state.jobs.find((j) => j.id === state.selectedId);
  if (selected) {
    const needFull = isActive(selected) || !previous?.log || previous.status !== selected.status;
    Object.assign(selected, needFull ? await api("GET", `/api/video/jobs/${selected.id}`, undefined, { signal }) : { log: previous.log });
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
  banner.setAttribute("role", shown.kind === "error" ? "alert" : "status"); // ошибку зачитать сразу
  banner.replaceChildren(...[].concat(shown.text));
}

const hasFiles = (e) => [...(e.dataTransfer?.types || [])].includes("Files");

function setupDropZone(zone, onFiles) {
  for (const type of ["dragenter", "dragover"]) {
    zone.addEventListener(type, (e) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      zone.classList.add("drop");
    });
  }
  zone.addEventListener("dragleave", (e) => {
    if (!zone.contains(e.relatedTarget)) zone.classList.remove("drop");
  });
  zone.addEventListener("drop", (e) => {
    zone.classList.remove("drop");
    if (!hasFiles(e)) return;
    e.preventDefault();
    onFiles(e.dataTransfer.files);
  });
}

async function init() {
  try {
    state.config = await api("GET", "/api/video/config");
  } catch (err) {
    setBanner(err.message, "error");
    return;
  }
  const cfg = state.config;
  state.duration = cfg.default_duration_s;
  const installed = cfg.models.filter((m) => m.installed);
  $("model-chip").textContent = installed.length ? installed.map((m) => m.label).join(" · ") : "модель не установлена";
  $("model").replaceChildren(
    ...cfg.models.map((m) =>
      h("option", { value: m.id, selected: m.id === cfg.default_model, disabled: !m.installed },
        m.installed ? m.label : `${m.label} — не установлена`),
    ),
  );
  const renderDurations = () => {
    const allowed = model().durations_s;
    if (!allowed.includes(state.duration)) state.duration = Math.max(...allowed.filter((s) => s <= state.duration), allowed[0]);
    $("durations").replaceChildren(
      ...allowed.map((s) =>
        h("label", { class: "seg" },
          h("input", {
            type: "radio",
            name: "duration",
            value: String(s),
            checked: s === state.duration,
            onchange: () => {
              state.duration = s;
              if (state.frames.length > maxFrames()) state.frames.length = maxFrames();
              relayout(); // позиции зависят от длины ролика — расставляем заново
            },
          }),
          h("span", {}, `${s} с`),
        ),
      ),
    );
  };
  const applyModel = () => {
    renderDurations();
    $("audio").disabled = !model().audio;
    $("audio").closest("label").title = model().audio ? "" : "Эта модель генерирует видео без звука";
    if (state.frames.length > maxFrames()) state.frames.length = maxFrames();
    relayout(); // частота кадров у моделей разная — позиции считаем заново
  };
  const showModelNote = () => {
    const missing = cfg.models.filter((m) => !m.installed).map((m) => `make models M=${m.id}`);
    const note = cfg.models.find((m) => m.id === $("model").value)?.note || "";
    $("model-note").textContent = [note, missing.length ? `Установить: ${missing.join(", ")}` : ""].filter(Boolean).join(". ");
  };
  $("model").addEventListener("change", () => {
    showModelNote();
    applyModel();
  });
  showModelNote();
  if (cfg.backend === "fake") {
    configBanner = {
      kind: "warn",
      text: "Демо-режим: модель НЕ запускается, ролик — простая склейка кадров. Для настоящей генерации останови сервис и запусти make run.",
    };
    $("model-chip").textContent = "демо, без модели";
  } else if (!cfg.runner_available) {
    configBanner = {
      kind: "warn",
      text: [`Команда «${cfg.runner_cmd}» не найдена — генерация не запустится. Как установить: ${cfg.install_hint}.`],
    };
  }
  setBanner(null);

  $("preset").replaceChildren(
    ...cfg.presets.map((p) => h("option", { value: p.id, selected: p.id === cfg.default_preset }, p.label)),
  );
  $("composer").addEventListener("submit", submit);
  $("prompt").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) $("composer").requestSubmit();
  });
  $("file-input").addEventListener("change", (e) => {
    addFiles(e.target.files);
    e.target.value = "";
  });
  $("even-btn").addEventListener("click", () => relayout());
  setupDropZone($("strip"), addFiles);
  setupDropZone($("living-photo"), (files) => setLivingFile(files[0]));
  // Промах мимо зоны не должен открывать картинку вместо страницы.
  for (const type of ["dragover", "drop"]) {
    window.addEventListener(type, (e) => hasFiles(e) && e.preventDefault());
  }

  $("moment-before").textContent = `${cfg.living.before_s} с до`;
  $("moment-after").textContent = `${cfg.living.after_s} с после`;
  state.livingOrientation = cfg.living.default_orientation;
  state.livingQuality = cfg.living.default_quality;
  $("living-model").replaceChildren(
    ...cfg.living.models.map((m) =>
      h("option", { value: m.id, selected: m.id === cfg.living.default_model, disabled: !m.installed },
        m.installed ? m.label : `${m.label} — не установлена`),
    ),
  );
  renderLivingOptions();
  $("living").addEventListener("submit", submitLiving);
  $("living-input").addEventListener("change", (e) => {
    setLivingFile(e.target.files[0]);
    e.target.value = "";
  });
  for (const name of Object.keys(TABS)) $(`tab-${name}`).addEventListener("click", () => showTab(name));
  showTab(location.hash.slice(1));
  renderLiving();
  applyModel();
  renderJobs();
  jobsPoll.start();
}

init();
