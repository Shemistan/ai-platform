// Вкладка «Фото» (перенос UI photogen): режим, промпт, исходное фото с маской или полями, запуск и результат. Без сборки и зависимостей.

import { $, api, busyClick, chip, h, jobProgress, poller, STATUS, statusChip, toneOf, UPLOAD_LIMITS, uploadFile } from "/ui.js";
import { loadOriented } from "./exif.js";
import { geometry as geometryOf } from "./geometry.js";

const state = {
  config: null,
  mode: "txt2img",
  detail: "standard",
  source: null, // {image, width, height, close} — фото как его выбрал пользователь, с учётом EXIF-поворота (exif.js)
  pads: { left: 25, top: 0, right: 25, bottom: 0 }, // поля outpaint, % от стороны фото
  tool: "brush",
  strengthTouched: false,
  jobs: [],
  selectedId: null,
  submitting: false,
};

const MODE_HINTS = {
  txt2img: "Картинка с нуля по текстовому описанию.",
  img2img: "Загрузи фото и опиши, что с ним сделать: сменить фон, стиль, свет.",
  inpaint: "Загрузи фото, закрась кистью область и опиши, что должно быть на её месте.",
  outpaint: "Загрузи фото и задай поля: модель дорисует сцену за границами кадра.",
};
const PAD_LABELS = { left: "Слева", right: "Справа", top: "Сверху", bottom: "Снизу" };

const currentModel = () => state.config.models.find((m) => m.id === $("model").value);
const usable = (model) => model.installed && model.modes.includes(state.mode);

// ---------- геометрия: к какому размеру приводится фото ----------

// Расчёт — чистая функция в geometry.js (тесты без DOM); здесь только сбор входов со страницы.
function geometry() {
  return geometryOf({
    source: state.source,
    pads: state.pads,
    outpaint: state.mode === "outpaint",
    grid: state.config.grid,
    maxSide: state.config.max_side,
    workSide: Number($("work-side").value),
  });
}

function renderStage() {
  const has = Boolean(state.source);
  const editing = state.mode !== "txt2img";
  $("drop").hidden = has;
  $("stage-wrap").hidden = $("stage-foot").hidden = !has;
  $("replace-btn").hidden = !has;
  $("mask-tools").hidden = !(has && state.mode === "inpaint");
  $("mask-canvas").hidden = state.mode !== "inpaint";
  $("pads").hidden = !(has && state.mode === "outpaint");
  if (!has || !editing) return;

  const g = geometry();
  const frame = $("frame");
  frame.style.width = `min(100%, ${g.cw}px, calc(60vh * ${g.cw / g.ch}))`;
  frame.style.aspectRatio = `${g.cw} / ${g.ch}`;
  const photo = $("photo-canvas");
  const mask = $("mask-canvas");
  if (photo.width !== g.sw || photo.height !== g.sh) {
    photo.width = g.sw;
    photo.height = g.sh;
    photo.getContext("2d").drawImage(state.source.image, 0, 0, g.sw, g.sh);
    // Маска живёт в координатах рабочего размера: при его смене переносим нарисованное.
    const old = h("canvas", { width: mask.width || 1, height: mask.height || 1 });
    if (mask.width) old.getContext("2d").drawImage(mask, 0, 0);
    mask.width = g.sw;
    mask.height = g.sh;
    mask.getContext("2d").drawImage(old, 0, 0, g.sw, g.sh);
  }
  for (const canvas of [photo, mask]) {
    Object.assign(canvas.style, {
      left: `${(100 * g.pad.left) / g.cw}%`,
      top: `${(100 * g.pad.top) / g.ch}%`,
      width: `${(100 * g.sw) / g.cw}%`,
      height: `${(100 * g.sh) / g.ch}%`,
    });
  }
  const grown = g.cw !== g.sw || g.ch !== g.sh;
  $("stage-info").textContent =
    `Исходник ${state.source.width}×${state.source.height} → в работу ${g.sw}×${g.sh}` +
    (grown ? ` → результат ${g.cw}×${g.ch}` : "");
  for (const side of Object.keys(PAD_LABELS)) $(`pad-out-${side}`).textContent = g.pad[side] ? `+${g.pad[side]} px` : "нет";
}

async function setSource(file) {
  if (!file || !file.type.startsWith("image/")) return;
  let source;
  try {
    source = await loadOriented(file);
  } catch {
    return showFormError("Не получилось открыть картинку. Поддерживаются PNG, JPEG и WebP.");
  }
  state.source?.close();
  state.source = source;
  $("photo-canvas").width = 0; // заставит renderStage перерисовать фото
  clearMask();
  showFormError(null);
  renderStage();
}

// ---------- маска ----------

function clearMask() {
  const mask = $("mask-canvas");
  mask.getContext("2d").clearRect(0, 0, mask.width, mask.height);
}

function setupBrush() {
  const mask = $("mask-canvas");
  const ctx = mask.getContext("2d");
  let last = null;
  const point = (e) => ({
    x: (e.offsetX / mask.clientWidth) * mask.width,
    y: (e.offsetY / mask.clientHeight) * mask.height,
  });
  const stroke = (from, to) => {
    ctx.globalCompositeOperation = state.tool === "eraser" ? "destination-out" : "source-over";
    ctx.strokeStyle = "#ff3b30";
    ctx.lineCap = ctx.lineJoin = "round";
    ctx.lineWidth = Number($("brush-size").value);
    ctx.beginPath();
    ctx.moveTo(from.x, from.y);
    ctx.lineTo(to.x, to.y);
    ctx.stroke();
  };
  mask.addEventListener("pointerdown", (e) => {
    mask.setPointerCapture(e.pointerId);
    last = point(e);
    stroke(last, last);
  });
  mask.addEventListener("pointermove", (e) => {
    if (!last) return;
    const next = point(e);
    stroke(last, next);
    last = next;
  });
  for (const type of ["pointerup", "pointercancel"]) mask.addEventListener(type, () => (last = null));

  for (const tool of ["brush", "eraser"]) {
    $(`tool-${tool}`).addEventListener("click", () => {
      state.tool = tool;
      $("tool-brush").setAttribute("aria-pressed", String(tool === "brush"));
      $("tool-eraser").setAttribute("aria-pressed", String(tool === "eraser"));
    });
  }
  $("mask-clear").addEventListener("click", clearMask);
}

function maskIsEmpty() {
  const mask = $("mask-canvas");
  const { data } = mask.getContext("2d").getImageData(0, 0, mask.width, mask.height);
  for (let i = 3; i < data.length; i += 4) if (data[i] > 0) return false;
  return true;
}

const toPng = (canvas) =>
  new Promise((resolve, reject) =>
    canvas.toBlob((blob) => (blob ? resolve(blob) : reject(new Error("Не получилось подготовить картинку."))), "image/png"),
  );

// Маска для модели: белое — перерисовать, чёрное — оставить.
function maskBlob() {
  const mask = $("mask-canvas");
  const out = h("canvas", { width: mask.width, height: mask.height });
  const ctx = out.getContext("2d");
  ctx.drawImage(mask, 0, 0);
  ctx.globalCompositeOperation = "source-in";
  ctx.fillStyle = "#fff";
  ctx.fillRect(0, 0, out.width, out.height);
  ctx.globalCompositeOperation = "destination-over";
  ctx.fillStyle = "#000";
  ctx.fillRect(0, 0, out.width, out.height);
  return toPng(out);
}

// ---------- форма ----------

function showFormError(message) {
  const box = $("form-error");
  box.hidden = !message;
  box.textContent = message || "";
}

function promptHint(model) {
  if (state.mode === "txt2img") return "Что в кадре, какой свет, ракурс, настроение. Cmd+Enter — запустить.";
  if (state.mode === "inpaint") {
    return "Опиши сцену целиком — такой, какой она должна стать, вместе с тем, что остаётся вокруг закрашенной области.";
  }
  if (state.mode === "outpaint") return "Опиши расширенную сцену целиком: что уже есть на фото и что появится на новых полях.";
  return model.edit_by_instruction
    ? "Напиши инструкцию: «сделай профессиональный студийный портрет, сохрани лицо, фон серый студийный»."
    : `${model.label} не понимает инструкции: опиши, какой должна стать картинка целиком. Сколько останется от исходника, решает «Сила изменений».`;
}

function syncControls() {
  const cfg = state.config;
  const editing = state.mode !== "txt2img";
  $("mode-hint").textContent = MODE_HINTS[state.mode];
  $("source-section").hidden = !editing;
  $("size-field").hidden = editing;
  $("work-field").hidden = !editing;

  // Модель: недоступные для режима и неустановленные видны, но выбрать их нельзя.
  const select = $("model");
  const wanted = select.value;
  select.replaceChildren(
    ...cfg.models.map((m) => {
      const why = !m.modes.includes(state.mode) ? " — не умеет этот режим" : !m.installed ? " — не установлена" : "";
      return h("option", { value: m.id, disabled: Boolean(why) }, m.label + why);
    }),
  );
  const candidates = [wanted, cfg.default_models[state.mode], ...cfg.models.map((m) => m.id)];
  const pick = candidates.map((id) => cfg.models.find((m) => m.id === id)).find((m) => m && usable(m));
  select.value = pick ? pick.id : "";
  const model = pick;
  $("submit").disabled = !model || state.submitting;

  const missing = cfg.models.filter((m) => !m.installed && m.modes.includes(state.mode));
  $("model-note").textContent = [
    model ? `${model.label}: ${model.note}.` : "Для этого режима нет установленной модели.",
    missing.length ? `Установить: ${missing.map((m) => `make models M=${m.id}`).join(", ")}.` : "",
  ].filter(Boolean).join(" ");
  $("prompt-hint").textContent = model ? promptHint(model) : "";

  // Уровни детализации показывают шаги выбранной модели.
  $("details").replaceChildren(
    ...cfg.details.map((d) =>
      h("label", { class: "seg" },
        h("input", {
          type: "radio", name: "detail", value: d.id, checked: d.id === state.detail,
          onchange: () => { state.detail = d.id; syncControls(); },
        }),
        h("span", {}, model ? `${d.label} · ${model.steps[d.id]} шаг.` : d.label),
      ),
    ),
  );
  if (!model) return renderStage();

  $("steps-field").hidden = !model.custom_steps;
  $("steps").max = model.max_steps;
  $("steps").placeholder = `${model.steps[state.detail]} — по детализации`;
  if (Number($("steps").value) > model.max_steps) $("steps").value = "";
  $("guidance-field").hidden = !model.guidance;
  if (model.guidance) {
    Object.assign($("guidance"), { min: model.guidance.min, max: model.guidance.max, placeholder: String(model.guidance.default) });
  }
  $("negative-field").hidden = !model.negative;
  const hasStrength = model.strength_modes.includes(state.mode);
  $("strength-field").hidden = !hasStrength;
  if (hasStrength) {
    if (!state.strengthTouched) $("strength").value = cfg.default_strength[state.mode];
    $("strength-out").textContent = Number($("strength").value).toFixed(2);
    $("strength-hint").textContent = state.mode === "inpaint"
      ? "1 — область рисуется заново (заменить или убрать объект). 0.5–0.75 — изменить, сохранив форму и цвета."
      : "Меньше — ближе к исходному фото, больше — свободнее от него.";
  }
  renderStage();
}

function setMode(mode) {
  state.mode = mode;
  state.strengthTouched = false;
  showFormError(null);
  syncControls();
}

async function buildPayload() {
  const model = currentModel();
  const payload = {
    mode: state.mode,
    prompt: $("prompt").value,
    model: model.id,
    style: $("style").value,
    detail: state.detail,
  };
  if ($("seed").value !== "") payload.seed = Number($("seed").value);
  if (model.custom_steps && $("steps").value !== "") payload.steps = Number($("steps").value);
  if (model.guidance && $("guidance").value !== "") payload.guidance = Number($("guidance").value);
  if (model.negative && $("negative").value.trim()) payload.negative_prompt = $("negative").value;
  if (model.strength_modes.includes(state.mode)) payload.strength = Number($("strength").value);

  if (state.mode === "txt2img") {
    payload.size = $("size").value;
    return payload;
  }
  if (!state.source) throw new Error("Загрузи исходное фото.");
  const g = geometry();
  if (state.mode === "inpaint" && maskIsEmpty()) throw new Error("Закрась кистью область, которую нужно изменить.");
  if (state.mode === "outpaint") {
    if (g.cw === g.sw && g.ch === g.sh) throw new Error("Задай поле хотя бы с одной стороны.");
    payload.padding = g.pad;
  }
  const limit = (state.config?.upload_limits || UPLOAD_LIMITS).image;
  payload.source = (await uploadFile("/api/photo/uploads", await toPng($("photo-canvas")), { limit, label: "Исходное фото" })).id;
  if (state.mode === "inpaint") payload.mask = (await uploadFile("/api/photo/uploads", await maskBlob(), { limit, label: "Маска" })).id;
  return payload;
}

async function submit(event) {
  event.preventDefault();
  if (state.submitting || !currentModel()) return;
  if (!$("prompt").value.trim()) return showFormError("Напиши промпт.");
  state.submitting = true;
  $("submit").disabled = true;
  $("submit").textContent = "Отправляю…";
  showFormError(null);
  try {
    const job = await api("POST", "/api/photo/jobs", await buildPayload());
    upsertJob(job);
    state.selectedId = job.id;
    renderJobs();
    schedulePoll();
  } catch (err) {
    showFormError(err.message);
  }
  state.submitting = false;
  $("submit").textContent = "Сгенерировать";
  $("submit").disabled = !currentModel();
}

// ---------- задачи ----------

const isActive = (job) => job.status === "queued" || job.status === "running";
const isUpscale = (job) => job.mode === "upscale";
const modeLabel = (id) =>
  id === "upscale" ? state.config.upscale.mode_label : state.config.modes.find((m) => m.id === id)?.label || id;

function upsertJob(job) {
  const index = state.jobs.findIndex((j) => j.id === job.id);
  if (index >= 0) state.jobs[index] = job;
  else state.jobs.unshift(job);
}

function fmtElapsed(total) {
  if (total < 60) return `${total.toFixed(1)} с`;
  const s = Math.round(total);
  return `${Math.floor(s / 60)} мин ${String(s % 60).padStart(2, "0")} с`;
}

function inputsBlock(job) {
  if (!job.source_url) return null;
  const pad = job.padding;
  return h("div", { class: "inputs" },
    h("figure", {}, h("img", { src: job.source_url, alt: "Исходное фото", loading: "lazy" }), h("figcaption", {}, "исходник")),
    job.mask_url ? h("figure", {}, h("img", { src: job.mask_url, alt: "Маска", loading: "lazy" }), h("figcaption", {}, "маска")) : null,
    pad ? h("p", { class: "muted small" }, `Поля: слева ${pad.left}, справа ${pad.right}, сверху ${pad.top}, снизу ${pad.bottom} px`) : null,
  );
}

function logBlock(job, open) {
  if (!job.log || !job.log.length) return null;
  const pre = h("pre", { class: "log" }, job.log.join("\n"));
  const details = h("details", { class: "log-wrap", open }, h("summary", {}, "Лог процесса"), pre);
  details.addEventListener("toggle", () => (pre.scrollTop = pre.scrollHeight));
  queueMicrotask(() => (pre.scrollTop = pre.scrollHeight));
  return details;
}

function jobMeta(job) {
  if (isUpscale(job)) {
    const [w, h] = job.source_size;
    return [job.model_label, modeLabel(job.mode), `${w}×${h} → ${job.width}×${job.height}`].join(" · ");
  }
  return [
    job.model_label,
    modeLabel(job.mode),
    `${job.width}×${job.height}`,
    job.preset || `шагов ${job.steps}`,
    job.guidance != null ? `guidance ${job.guidance}` : null,
    job.strength != null ? `сила ${job.strength}` : null,
    `seed ${job.seed}`,
  ].filter(Boolean).join(" · ");
}

function upscaleBlock(job) {
  const targets = state.config.upscale.targets.filter((t) => job.upscale_targets?.includes(t.id));
  if (!targets.length) return null;
  return h("div", { class: "upscale" },
    h("span", { class: "muted small" }, "Увеличить с дорисовкой деталей (SeedVR2):"),
    ...targets.map((t) =>
      h("button", { type: "button", class: "btn btn--secondary btn--sm", onclick: busyClick(() => upscaleJob(job.id, t.id), `upscale:${job.id}`) }, `до ${t.label}`),
    ),
    h("span", { class: "muted small" }, "2K — ~20 с, 4K — ~1,5 мин, ~18 ГБ памяти; первый запуск докачает ~7 ГБ весов."),
  );
}

async function upscaleJob(id, target) {
  try {
    const job = await api("POST", `/api/photo/jobs/${id}/upscale`, { target });
    upsertJob(job);
    state.selectedId = job.id;
    renderJobs();
    schedulePoll();
  } catch (err) {
    showFormError(err.message);
  }
}

let jobSignature = "";

function renderJob() {
  const root = $("job");
  const job = state.jobs.find((j) => j.id === state.selectedId);
  if (!job) {
    jobSignature = "";
    root.replaceChildren(h("p", { class: "empty" }, "Здесь появится статус генерации и готовая картинка."));
    return;
  }
  const signature = JSON.stringify([job.id, job.status, job.progress, job.elapsed_s, job.queue_position, job.log?.length]);
  if (signature === jobSignature) return; // не трогаем DOM: иначе картинка перезагрузится и мигнёт
  jobSignature = signature;
  const logOpen = root.querySelector("details.log-wrap")?.open ?? job.status === "failed";

  const parts = [
    h("div", { class: "job-head" },
      statusChip(job.status),
      job.backend === "fake" ? chip("Демо — не модель", "warning") : null,
      h("span", { class: "muted small" }, jobMeta(job)),
    ),
    h("p", { class: "job-text" }, job.prompt),
  ];
  if (isActive(job)) parts.push(jobProgress(job, { onCancel: (j) => cancelJob(j.id), elapsed: fmtElapsed }));
  if (job.status === "done") {
    parts.push(
      h("a", { class: "result-link", href: job.image_url, target: "_blank", title: "Открыть в полном размере" },
        h("img", { class: "result", src: job.image_url, alt: job.prompt, width: job.width, height: job.height }),
      ),
      h("div", { class: "job-foot" },
        h("span", { class: "took" }, `${isUpscale(job) ? "Увеличено" : "Сгенерировано"} за ${fmtElapsed(job.elapsed_s)}`),
        h("a", { class: "btn btn--primary", href: `${job.image_url}?download=1`, download: `photo-${job.id}.png` }, "Скачать PNG"),
      ),
      upscaleBlock(job),
    );
  }
  if (job.status === "failed") parts.push(h("p", { class: "job-error", role: "alert" }, job.error));
  if (job.status === "canceled") parts.push(h("p", { class: "muted" }, "Генерация отменена."));
  parts.push(inputsBlock(job), logBlock(job, logOpen));
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
    ...state.jobs.map((job) => {
      const thumb = job.image_url || job.source_url;
      const when = new Date(job.created_at * 1000).toLocaleString("ru-RU", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
      return h("li", {},
        h("button", {
            type: "button",
            class: `history-item${job.id === state.selectedId ? " current" : ""}`,
            "aria-current": job.id === state.selectedId ? "true" : null,
            onclick: () => selectJob(job.id),
          },
          thumb
            ? h("img", { class: "history-thumb", src: thumb, alt: "", loading: "lazy" })
            : h("span", { class: "history-thumb text" }, "T"),
          h("span", { class: "history-text" },
            h("span", { class: "history-title" }, job.prompt),
            h("span", { class: "muted small" },
              [when, job.model_label, modeLabel(job.mode), job.status === "done" ? fmtElapsed(job.elapsed_s) : null].filter(Boolean).join(" · ")),
          ),
          chip(job.status === "running" ? `${Math.round(job.progress.percent)}%` : STATUS[job.status], toneOf(job.status)),
        ),
      );
    }),
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
    upsertJob(await api("GET", `/api/photo/jobs/${id}`));
    renderJobs();
  } catch { /* задача останется в виде из списка */ }
}

async function cancelJob(id) {
  try {
    upsertJob(await api("POST", `/api/photo/jobs/${id}/cancel`));
    renderJobs();
  } catch (err) {
    showFormError(err.message);
  }
}

// Опрос очереди: раз в секунду, пока есть активные задачи, раз в 5 с в простое (poller из ui.js:
// в скрытой вкладке — раз в минуту, после сбоя связи — реже). Свои действия зовут schedulePoll().
const jobsPoll = poller({ run: (signal) => poll(signal), fast: 1000, slow: 5000, isActive: () => state.jobs.some(isActive), onError: (err) => setBanner(err.message, "error") });

function schedulePoll() {
  jobsPoll.now();
}

async function poll(signal) {
  const { jobs } = await api("GET", "/api/photo/jobs", undefined, { signal });
  // Список приходит без лога; лог выбранной задачи берём отдельным запросом.
  const selected = jobs.find((j) => j.id === state.selectedId);
  const previous = state.jobs.find((j) => j.id === state.selectedId);
  if (selected) {
    const needFull = isActive(selected) || !previous?.log || previous.status !== selected.status;
    Object.assign(selected, needFull ? await api("GET", `/api/photo/jobs/${selected.id}`, undefined, { signal }) : { log: previous.log });
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

function setupDropZone() {
  const hasFiles = (e) => [...(e.dataTransfer?.types || [])].includes("Files");
  for (const zone of [$("drop"), $("stage-wrap")]) {
    for (const type of ["dragenter", "dragover"]) {
      zone.addEventListener(type, (e) => {
        if (!hasFiles(e)) return;
        e.preventDefault();
        zone.classList.add("over");
      });
    }
    zone.addEventListener("dragleave", (e) => {
      if (!zone.contains(e.relatedTarget)) zone.classList.remove("over");
    });
    zone.addEventListener("drop", (e) => {
      zone.classList.remove("over");
      if (!hasFiles(e)) return;
      e.preventDefault();
      setSource(e.dataTransfer.files[0]);
    });
  }
  // Промах мимо зоны не должен открывать картинку вместо страницы.
  for (const type of ["dragover", "drop"]) {
    window.addEventListener(type, (e) => hasFiles(e) && e.preventDefault());
  }
}

function configureBanner(cfg) {
  const installed = cfg.models.filter((m) => m.installed);
  $("model-chip").textContent = installed.length ? `моделей установлено: ${installed.length} из ${cfg.models.length}` : "модели не установлены";
  if (cfg.backend === "fake") {
    $("model-chip").textContent = "демо, без моделей";
    configBanner = {
      kind: "warn",
      text: "Демо-режим: модели НЕ запускаются, вместо картинки — цветной градиент. Для настоящей генерации останови сервис и запусти make run.",
    };
  } else if (!installed.length) {
    configBanner = {
      kind: "warn",
      text: [
        "Ни одна модель не установлена — или не подключён диск с весами (",
        h("code", {}, cfg.models_dir),
        "). Установка: ",
        h("code", {}, "make models M=z-image-turbo"),
        `. Подробнее: ${cfg.install_hint}.`,
      ],
    };
  } else if (Object.values(cfg.runners).includes(false)) {
    const lost = Object.entries(cfg.runners).filter(([, ok]) => !ok).map(([name]) => name).join(", ");
    configBanner = { kind: "warn", text: `Пайплайн ${lost} не найден — его модели не запустятся. Как установить: ${cfg.install_hint}.` };
  }
  setBanner(null);
}

async function init() {
  try {
    state.config = await api("GET", "/api/photo/config");
  } catch (err) {
    setBanner(err.message, "error");
    return;
  }
  const cfg = state.config;
  state.detail = cfg.default_detail;
  configureBanner(cfg);

  $("modes").replaceChildren(
    ...cfg.modes.map((m) =>
      h("label", { class: "seg" },
        h("input", { type: "radio", name: "mode", value: m.id, checked: m.id === state.mode, onchange: () => setMode(m.id) }),
        h("span", {}, m.label),
      ),
    ),
  );
  $("size").replaceChildren(...cfg.sizes.map((s) => h("option", { value: s.id, selected: s.id === cfg.default_size }, s.label)));
  $("work-side").replaceChildren(
    ...cfg.work_sides.map((s) => h("option", { value: String(s), selected: s === cfg.default_work_side }, `до ${s} px по длинной стороне`)),
  );
  $("style").replaceChildren(...cfg.styles.map((s) => h("option", { value: s.id, selected: s.id === cfg.default_style }, s.label)));
  $("strength").min = cfg.min_strength;
  $("pads").replaceChildren(
    ...Object.entries(PAD_LABELS).map(([side, label]) =>
      h("label", { class: "pad" },
        h("span", { class: "pad-head" }, h("span", {}, label), h("output", { id: `pad-out-${side}` })),
        h("input", {
          type: "range", min: "0", max: "100", step: "5", value: String(state.pads[side]),
          oninput: (e) => { state.pads[side] = Number(e.target.value); renderStage(); },
        }),
      ),
    ),
  );

  $("model").addEventListener("change", syncControls);
  $("work-side").addEventListener("change", renderStage);
  $("strength").addEventListener("input", () => {
    state.strengthTouched = true;
    $("strength-out").textContent = Number($("strength").value).toFixed(2);
  });
  $("composer").addEventListener("submit", submit);
  $("prompt").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) $("composer").requestSubmit();
  });
  for (const id of ["drop", "replace-btn"]) $(id).addEventListener("click", () => $("file-input").click());
  $("file-input").addEventListener("change", (e) => {
    setSource(e.target.files[0]);
    e.target.value = "";
  });
  setupBrush();
  setupDropZone();
  syncControls();
  renderJobs();
  jobsPoll.start();
}

init();
