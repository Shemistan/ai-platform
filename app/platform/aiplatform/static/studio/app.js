// Сценарии «Дайджест новостей» и «Видео по статье»: выпуск — цепочка шагов от текста до публикации.
// Каждый шаг виден, его результат можно поправить, загрузить свой или перезапустить шаг.
import { $, api, h, logBlock, progressBar, seconds, statusChip } from "/ui.js";

const API = "/api/studio";
const scenario = new URLSearchParams(location.search).get("scenario") === "digest" ? "digest" : "article";
const HINTS = {
  digest:
    "narrator собирает свежие новости и пишет пересказ → постер → слайды с картинками → озвучка → видео 16:9 (и шортс 9:16) → публикация.",
  article: "Вставь готовую статью → постер → слайды с картинками → озвучка → видео 16:9 (и шортс 9:16) → публикация.",
};
const state = { config: null, projects: [], project: null, signature: "", drafts: {}, timer: null };

// --- настройки (общая форма для «нового выпуска» и «настроек выпуска») ---

function settingsForm(values, prefix) {
  const cfg = state.config;
  const id = (name) => `${prefix}-${name}`;
  const radio = (name, value, label, checked) =>
    h("label", {}, h("input", { type: "radio", name: id(name), value, checked }), label);
  const check = (name, label, hint, checked) =>
    h(
      "label",
      { class: "check" },
      h("input", { type: "checkbox", id: id(name), checked }),
      h("span", {}, label, hint ? h("span", { class: "hint" }, hint) : null),
    );
  const engine = cfg.engines.find((e) => e.id === values.engine) || cfg.engines[0];
  const voiceSelect = h(
    "select",
    { id: id("voice") },
    h("option", { value: "" }, `по умолчанию (${engine.default_voice})`),
    ...engine.voices.map((v) => h("option", { value: v, selected: v === values.voice }, v)),
  );
  const engineSelect = h(
    "select",
    {
      id: id("engine"),
      onchange: (event) => {
        const e = cfg.engines.find((x) => x.id === event.target.value);
        voiceSelect.replaceChildren(
          h("option", { value: "" }, `по умолчанию (${e.default_voice})`),
          ...e.voices.map((v) => h("option", { value: v }, v)),
        );
      },
    },
    ...cfg.engines.map((e) => h("option", { value: e.id, selected: e.id === values.engine }, e.label)),
  );
  const channels = cfg.channels.length
    ? cfg.channels.map((c) =>
        h(
          "label",
          { class: "channel" },
          h("input", { type: "checkbox", name: id("channels"), value: c.id, checked: values.channels.includes(c.id) }),
          c.label,
          h("span", { class: "muted" }, c.format === "vertical" ? "9:16" : "16:9"),
          c.problem ? h("span", { class: "status failed" }, c.problem) : null,
        ),
      )
    : [h("p", { class: "hint" }, `Каналы не настроены: ${cfg.channels_file}`)];
  return h(
    "div",
    { class: "settings-form" },
    h(
      "fieldset",
      {},
      h("legend", {}, "Постер — первый кадр"),
      h(
        "div",
        { class: "radio-row" },
        radio("poster_mode", "local", "Сгенерировать локальной моделью", values.poster_mode === "local"),
        radio("poster_mode", "upload", "Загружу сам по промпту", values.poster_mode === "upload"),
      ),
    ),
    h(
      "fieldset",
      {},
      h("legend", {}, "Картинки слайдов"),
      check("draw_images", "Картинки на слайдах", "Иначе фон слайдов — градиент.", values.draw_images),
      h(
        "div",
        { class: "radio-row" },
        radio("images_mode", "local", "Сгенерировать локальной моделью", values.images_mode !== "upload"),
        radio("images_mode", "upload", "Сделаю сам по промптам во внешней модели", values.images_mode === "upload"),
      ),
      h(
        "label",
        { class: "field" },
        h("span", { class: "hint" }, "Модель для постера и картинок"),
        h(
          "select",
          { id: id("image_model") },
          ...cfg.image_models.map((m) =>
            h("option", { value: m.id, selected: m.id === values.image_model }, m.label + (m.installed ? "" : " — не установлена")),
          ),
        ),
      ),
    ),
    h(
      "fieldset",
      {},
      h("legend", {}, "Видео для слайдов"),
      check(
        "clips",
        "Видео-фон слайдов — сделаю сам по промптам во внешней модели",
        "На шаге «Видео для слайдов» будут промпты и окна, куда перетащить готовые ролики (MP4, MOV, WebM). Ролик заменяет картинку слайда и крутится по кругу без своего звука.",
        values.clips,
      ),
    ),
    h(
      "fieldset",
      {},
      h("legend", {}, "Озвучка"),
      h(
        "div",
        { class: "radio-row" },
        radio("voice_mode", "tts", "Озвучить narrator", values.voice_mode === "tts"),
        radio("voice_mode", "upload", "Загружу свой mp3", values.voice_mode === "upload"),
      ),
      h("div", { class: "settings" }, h("label", { class: "field" }, h("span", { class: "hint" }, "Движок"), engineSelect), h("label", { class: "field" }, h("span", { class: "hint" }, "Голос"), voiceSelect)),
    ),
    h(
      "fieldset",
      {},
      h("legend", {}, "Шортс"),
      check("shorts", "Сделать и вертикальное видео 9:16", "Короткий сценарий 45–60 с для YouTube Shorts, TikTok, Reels.", values.shorts),
    ),
    h(
      "fieldset",
      {},
      h("legend", {}, "Публикация"),
      ...channels,
      check("auto_publish", "Публиковать сразу, без моей проверки", null, values.auto_publish),
    ),
    check("review_text", "Пауза после текста — проверю его перед генерацией", null, values.review_text),
  );
}

function readSettings(prefix) {
  const get = (name) => document.getElementById(`${prefix}-${name}`);
  const radio = (name) => document.querySelector(`input[name="${prefix}-${name}"]:checked`)?.value;
  return {
    poster_mode: radio("poster_mode"),
    draw_images: get("draw_images").checked,
    images_mode: radio("images_mode"),
    image_model: get("image_model").value,
    clips: get("clips").checked,
    voice_mode: radio("voice_mode"),
    engine: get("engine").value,
    voice: get("voice").value,
    shorts: get("shorts").checked,
    review_text: get("review_text").checked,
    auto_publish: get("auto_publish").checked,
    channels: [...document.querySelectorAll(`input[name="${prefix}-channels"]:checked`)].map((i) => i.value),
  };
}

// --- создание ---

function renderCreate() {
  const yesterday = new Date(Date.now() - 86400000).toISOString().slice(0, 10);
  $("source-fields").replaceChildren(
    ...(scenario === "digest"
      ? [
          h(
            "label",
            { class: "field" },
            h("span", { class: "field-label" }, "День выпуска"),
            h("input", { type: "date", id: "day", value: yesterday, max: new Date().toISOString().slice(0, 10) }),
            h("span", { class: "hint" }, "narrator обойдёт источники и напишет пересказ дня — это занимает несколько минут."),
          ),
        ]
      : [
          h("label", { class: "field" }, h("span", { class: "field-label" }, "Заголовок"), h("input", { type: "text", id: "title", placeholder: "необязательно — возьмём из текста" })),
          h(
            "label",
            { class: "field" },
            h("span", { class: "field-label" }, "Текст статьи"),
            h("textarea", { id: "text", rows: 10, placeholder: "Готовый текст для озвучки. Пустая строка — новый абзац; слайды привязываются к абзацам." }),
          ),
        ]),
  );
  $("create-settings").replaceChildren(settingsForm(state.config.defaults, "new"));
}

async function create(event) {
  event.preventDefault();
  const error = $("create-error");
  error.hidden = true;
  const payload = { scenario, settings: readSettings("new"), run: true };
  if (scenario === "digest") payload.day = $("day").value;
  else Object.assign(payload, { title: $("title").value, text: $("text").value });
  $("create-btn").disabled = true;
  try {
    const project = await api("POST", `${API}/projects`, payload);
    if (scenario === "article") $("text").value = "";
    await loadProjects();
    show(project);
  } catch (err) {
    error.textContent = err.message;
    error.hidden = false;
  } finally {
    $("create-btn").disabled = false;
  }
}

// --- список ---

async function loadProjects() {
  const { projects } = await api("GET", `${API}/projects?scenario=${scenario}`);
  state.projects = projects;
  $("projects").replaceChildren(
    ...(projects.length
      ? projects.map((p) =>
          h(
            "li",
            {},
            h(
              "button",
              { type: "button", class: `history-item${state.project?.id === p.id ? " current" : ""}`, onclick: () => open(p.id) },
              h("span", { class: "history-text" }, h("span", { class: "history-title" }, p.title), h("span", { class: "muted small" }, new Date(p.created_at * 1000).toLocaleString("ru"))),
              statusChip(p.status),
            ),
          ),
        )
      : [h("li", { class: "muted" }, "Пока нет выпусков.")]),
  );
}

async function open(id) {
  state.drafts = {};
  show(await api("GET", `${API}/projects/${id}`));
  loadProjects();
  history.replaceState(null, "", `?scenario=${scenario}&project=${id}`);
}

// --- действия над выпуском ---

async function act(path, body) {
  try {
    show(await api("POST", `${API}/projects/${state.project.id}/${path}`, body ?? {}));
  } catch (err) {
    alertBox(err.message);
  }
}

function alertBox(message) {
  const box = $("project-error");
  if (box) {
    box.textContent = message;
    box.hidden = false;
  }
}

async function saveArtifact(name, text) {
  try {
    show(await api("POST", `${API}/projects/${state.project.id}/artifacts/${name}`, { text }));
    delete state.drafts[name];
    state.signature = "";
    show(state.project);
  } catch (err) {
    alertBox(err.message);
  }
}

async function upload(what, file) {
  try {
    show(await api("POST", `${API}/projects/${state.project.id}/upload/${what}`, file));
  } catch (err) {
    alertBox(err.message);
  }
}

// --- отрисовка выпуска ---

function editor(name, value, rows = 8, mono = false) {
  const area = h("textarea", { rows, class: mono ? "mono" : null, oninput: (e) => (state.drafts[name] = e.target.value) });
  area.value = state.drafts[name] ?? value ?? "";
  return h(
    "div",
    { class: "field" },
    area,
    h(
      "div",
      { class: "actions" },
      h("button", { type: "button", class: "secondary", disabled: state.project.running, onclick: () => saveArtifact(name, area.value) }, "Сохранить правку"),
      h("span", { class: "hint" }, "Правка сбросит шаги, которые от неё зависят."),
    ),
  );
}

function uploader(what, accept, label) {
  return h(
    "label",
    { class: "upload secondary" },
    label,
    h("input", { type: "file", accept, hidden: true, disabled: stepLocked(what), onchange: (e) => e.target.files[0] && upload(what, e.target.files[0]) }),
  );
}

async function uploadSlide(kind, key, file) {
  try {
    show(await api("POST", `${API}/projects/${state.project.id}/upload/${kind}/${key}`, file));
  } catch (err) {
    alertBox(err.message);
  }
}

const MEDIA = {
  images: { accept: "image/png,image/jpeg,image/webp", hint: "Перетащи картинку (PNG, JPEG, WebP) или нажми" },
  clips: { accept: "video/mp4,video/quicktime,video/webm", hint: "Перетащи видео (MP4, MOV, WebM) или нажми" },
  poster: { accept: "image/png,image/jpeg,image/webp", hint: "Перетащи постер (PNG, JPEG, WebP) или нажми" },
};

// Окно для готового файла: перетащить или выбрать. Внутри — превью того, что уже лежит.
function dropZone(kind, url, onFile) {
  const busy = stepLocked(kind);
  const input = h("input", { type: "file", accept: MEDIA[kind].accept, hidden: true, onchange: (e) => e.target.files[0] && onFile(e.target.files[0]) });
  let preview = h("span", { class: "drop-hint" }, MEDIA[kind].hint);
  if (url && kind === "clips") {
    preview = h("video", { src: url, loop: true, autoplay: true, playsinline: true, preload: "metadata" });
    preview.muted = true; // атрибут muted у созданного скриптом элемента автоплей не разрешает
  } else if (url) {
    preview = h("img", { src: url, alt: "" });
  }
  const zone = h(
    "div",
    {
      class: `drop-zone${url ? " filled" : ""}${busy ? " disabled" : ""}`,
      role: "button",
      tabindex: busy ? "-1" : "0",
      title: url ? "Перетащи другой файл, чтобы заменить" : null,
      onclick: () => !busy && input.click(),
      onkeydown: (e) => (e.key === "Enter" || e.key === " ") && !busy && (e.preventDefault(), input.click()),
      ondragover: (e) => {
        if (busy) return;
        e.preventDefault();
        zone.classList.add("over");
      },
      ondragleave: () => zone.classList.remove("over"),
      ondrop: (e) => {
        e.preventDefault();
        zone.classList.remove("over");
        const file = e.dataTransfer.files[0];
        if (file && !busy) onFile(file);
      },
    },
    preview,
    input,
  );
  return zone;
}

function copyButton(text) {
  return h(
    "button",
    {
      type: "button",
      class: "link",
      onclick: async (e) => {
        await navigator.clipboard?.writeText(text);
        e.target.textContent = "Скопировано";
        setTimeout(() => (e.target.textContent = "Скопировать промпт"), 1500);
      },
    },
    "Скопировать промпт",
  );
}

// Строка «промпт слева — окно для готового файла справа».
function promptRow(label, prompt, zone) {
  return h(
    "div",
    { class: "prompt-row" },
    h("div", { class: "prompt-side" }, h("strong", {}, label), h("p", { class: "prompt-text" }, prompt), copyButton(prompt)),
    zone,
  );
}

function mediaRows(kind) {
  const field = kind === "clips" ? "video_prompt" : "image_prompt";
  const fileField = kind === "clips" ? "clip" : "image";
  const rows = state.project.slides
    .filter((s) => s[field])
    .map((s) => promptRow(`Слайд ${Number(s.key)} · ${s.title}`, s[field], dropZone(kind, s[fileField], (file) => uploadSlide(kind, s.key, file))));
  return rows.length ? [h("div", { class: "prompt-rows" }, ...rows)] : [];
}

// Файлы шагу можно класть и во время запуска — пока сам шаг и зависящие от него не выполняются.
const stepLocked = (id) => Boolean(state.project.steps.find((s) => s.id === id)?.locked);

function jobLink(step) {
  if (!step.job) return null;
  return h("a", { class: "link", href: `/${step.job.tab}/` }, `задача на вкладке «${step.job.tab === "photo" ? "Фото" : "Аудио"}»`);
}

function stepBody(step) {
  const p = state.project;
  const a = p.artifacts;
  const f = p.files;
  switch (step.id) {
    case "text":
      return [a.text || step.status !== "pending" ? editor("text", a.text, 12) : h("p", { class: "muted" }, "Текст появится после сбора новостей.")];
    case "poster_prompt":
      return a.poster_prompt
        ? [
            editor("poster_prompt", a.poster_prompt, 3),
            h("button", { type: "button", class: "link", onclick: () => navigator.clipboard?.writeText(a.poster_prompt) }, "Скопировать промпт"),
          ]
        : [];
    case "poster":
      if (p.settings.poster_mode === "upload" && a.poster_prompt) {
        return [h("div", { class: "prompt-rows" }, promptRow("Постер — первый кадр", a.poster_prompt, dropZone("poster", f.poster, (file) => upload("poster", file))))];
      }
      return [
        f.poster ? h("img", { class: "poster-img", src: f.poster, alt: "Постер" }) : null,
        h("div", { class: "actions" }, uploader("poster", "image/png,image/jpeg,image/webp", f.poster ? "Заменить своей картинкой" : "Загрузить постер"), jobLink(step)),
      ];
    case "plan": {
      if (!a.plan) return [];
      const cards = a.plan.slides.map((s, i) => {
        const key = String(i + 1).padStart(2, "0");
        const img = f.images[key];
        return h(
          "div",
          { class: "slide-card" },
          img ? h("img", { src: img, alt: "" }) : h("div", { class: "ph" }),
          h("div", {}, h("strong", {}, s.title), h("div", { class: "para" }, `с абзаца ${s.para + 1}`), s.bullets.length ? h("ul", {}, ...s.bullets.map((b) => h("li", {}, b))) : null),
        );
      });
      return [
        a.plan.title ? h("p", {}, h("span", { class: "muted" }, "Название: "), a.plan.title) : null,
        h("div", { class: "slides-list" }, ...cards),
        h("details", {}, h("summary", { class: "link" }, "Править план (JSON)"), editor("plan", JSON.stringify(a.plan, null, 2), 14, true)),
      ];
    }
    case "images":
      if (!p.settings.draw_images) return [];
      return [step.status === "running" ? jobLink(step) : null, ...mediaRows("images")];
    case "clips":
      return p.settings.clips ? mediaRows("clips") : [];
    case "voice":
      return [
        f.voice ? h("audio", { controls: true, src: f.voice, preload: "metadata" }) : null,
        h("div", { class: "actions" }, uploader("voice", "audio/mpeg,audio/wav,audio/mp4,.m4a", f.voice ? "Заменить своим mp3" : "Загрузить озвучку"), jobLink(step)),
      ];
    case "render":
      return f.video
        ? [h("video", { controls: true, src: f.video, preload: "metadata", poster: f.poster || null }), h("a", { class: "link", href: `${f.video.split("?")[0]}?download=1` }, "Скачать видео 16:9")]
        : [];
    case "short":
      return [
        a.short ? h("details", {}, h("summary", { class: "link" }, `Сценарий шортса: ${a.short.beats.length} частей`), editor("short", JSON.stringify(a.short, null, 2), 10, true)) : null,
        f.short_video
          ? h("div", {}, h("video", { class: "vertical-video", controls: true, src: f.short_video, preload: "metadata" }), h("p", {}, h("a", { class: "link", href: `${f.short_video.split("?")[0]}?download=1` }, "Скачать шортс 9:16")))
          : null,
      ];
    case "publish": {
      const ready = p.settings.channels.length;
      return [
        editor("post", a.post, 5),
        ready
          ? h("p", { class: "hint" }, "Каналы: ", p.settings.channels.map((id) => state.config.channels.find((c) => c.id === id)?.label || id).join(", "))
          : h("p", { class: "hint" }, "Выбери каналы в настройках выпуска."),
        step.status === "waiting" && ready
          ? h("button", { type: "button", class: "primary", onclick: () => act("approve", { step: "publish" }) }, "Опубликовать")
          : null,
        p.published
          ? h(
              "ul",
              { class: "results" },
              ...p.published.map((r) =>
                h(
                  "li",
                  {},
                  `✓ ${r.label}: ${r.message} `,
                  ...(r.files || []).filter((x) => x.endsWith(".mp4")).map((x) => h("a", { class: "link", href: `${API}/projects/${p.id}/files/${x}?download=1` }, "скачать")),
                ),
              ),
            )
          : null,
      ];
    }
    default:
      return [];
  }
}

function renderStep(step) {
  const p = state.project;
  const approveLabel = { text: "Продолжить", images: "Продолжить без недостающих", clips: "Продолжить без недостающих" }[step.id];
  const canApprove = step.status === "waiting" && approveLabel && !(step.id === "images" && p.settings.images_mode !== "upload");
  return h(
    "article",
    { class: `step ${step.status}` },
    h(
      "div",
      { class: "step-head" },
      h("h3", {}, step.label),
      statusChip(step.status),
      step.status !== "running"
        ? h("button", { type: "button", class: "link", disabled: p.running, onclick: () => act("run", { step: step.id }) }, step.status === "pending" ? "Выполнить шаг" : "Перезапустить шаг")
        : null,
    ),
    step.status === "running" ? progressBar(step.progress || { stage: "…", percent: 0 }) : null,
    step.error ? h("p", { class: "job-error" }, step.error) : null,
    step.note ? h("p", { class: "step-note" }, step.note) : null,
    step.status === "pending" && step.blocked_by.length
      ? h("p", { class: "hint" }, `Начнётся, когда будет готово: ${step.blocked_by.join(", ")}.`)
      : null,
    canApprove ? h("button", { type: "button", class: "primary", onclick: () => act("approve", { step: step.id }) }, approveLabel) : null,
    h("div", { class: "step-body" }, ...stepBody(step)),
  );
}

function show(project) {
  state.project = project;
  const signature = JSON.stringify(project);
  if (signature === state.signature) return schedule();
  state.signature = signature;
  const running = project.running;
  $("project").replaceChildren(
    h(
      "div",
      { class: "panel project-head" },
      h("h2", {}, project.title),
      statusChip(running ? "running" : project.steps.some((s) => s.status === "failed") ? "failed" : project.steps.some((s) => s.status === "waiting") ? "waiting" : project.steps.every((s) => s.status === "done" || s.status === "skipped") ? "done" : "pending"),
      running
        ? h("button", { type: "button", class: "secondary", onclick: () => act("cancel") }, "Остановить")
        : h("button", { type: "button", class: "primary", onclick: () => act("run", {}) }, "Запустить всё"),
      h("p", { class: "form-error", id: "project-error", role: "alert", hidden: true }),
    ),
    h(
      "details",
      { class: "panel settings-box" },
      h("summary", {}, "Настройки выпуска"),
      settingsForm(project.settings, "edit"),
      h(
        "div",
        { class: "actions" },
        h(
          "button",
          {
            type: "button",
            class: "secondary",
            disabled: running,
            onclick: () => act("settings", readSettings("edit")),
          },
          "Сохранить настройки",
        ),
        h("span", { class: "hint" }, "Изменённые настройки сбросят только затронутые шаги."),
      ),
    ),
    h("div", { class: "steps" }, ...project.steps.map(renderStep)),
    h("div", { class: "panel" }, logBlock(project.log, "Журнал выпуска") || h("p", { class: "muted small" }, "Журнал пуст.")),
  );
  schedule();
}

function schedule() {
  clearTimeout(state.timer);
  state.timer = setTimeout(refresh, state.project?.running ? 1000 : 5000);
}

// Пока человек тащит файл, страницу не перерисовываем — иначе окно под курсором пропадёт.
document.addEventListener("dragover", () => (state.dragging = Date.now()));

async function refresh() {
  try {
    if (state.project && Date.now() - (state.dragging || 0) < 1500) return schedule();
    if (state.project) {
      const editing = document.activeElement?.tagName === "TEXTAREA" || document.activeElement?.tagName === "SELECT";
      const fresh = await api("GET", `${API}/projects/${state.project.id}`);
      if (!editing || fresh.running !== state.project.running) show(fresh);
      else schedule();
      if (fresh.running !== state.project.running) loadProjects();
    }
  } catch {
    schedule();
  }
}

async function init() {
  $("scenario-title").textContent = scenario === "digest" ? "Дайджест новостей" : "Видео по статье";
  $("scenario-hint").textContent = HINTS[scenario];
  document.title = `${$("scenario-title").textContent} — AI Platform`;
  state.config = await api("GET", `${API}/config`);
  renderCreate();
  $("create").addEventListener("submit", create);
  await loadProjects();
  const wanted = new URLSearchParams(location.search).get("project");
  const first = wanted || state.projects[0]?.id;
  if (first) await open(first).catch(() => {});
  setInterval(loadProjects, 10000);
}

init().catch((err) => {
  $("project").replaceChildren(h("p", { class: "job-error" }, `Не удалось загрузить сценарии: ${err.message}`));
});
