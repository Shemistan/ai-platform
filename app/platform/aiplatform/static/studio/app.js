// Сценарии «Дайджест новостей», «Видео по статье» и «Пост в канал»: выпуск — цепочка шагов от текста до публикации.
// Каждый шаг виден, его результат можно поправить, загрузить свой или перезапустить шаг.
import { $, announce, api, confirmAction, confirmPublish, debounce, h, logBlock, progressBar, safe, STATUS, statusChip, toneOf } from "/ui.js";

const API = "/api/studio";
const SCENARIO_TITLES = { digest: "Дайджест новостей", article: "Видео по статье", post: "Пост в канал" };
const wantedScenario = new URLSearchParams(location.search).get("scenario");
const scenario = SCENARIO_TITLES[wantedScenario] ? wantedScenario : "article";
// У поста нет слайдов: эти шаги всегда пропущены — их не показываем.
const HIDDEN_STEPS = { post: new Set(["plan", "images", "clips", "short"]) };
const CAPTION_LIMIT = 1024;
const HINTS = {
  digest:
    "narrator собирает свежие новости и пишет пересказ → постер → слайды с картинками → озвучка → видео 16:9 (и шортс 9:16) → публикация.",
  article: "Вставь готовую статью → постер → слайды с картинками → озвучка → видео 16:9 (и шортс 9:16) → публикация.",
  post: "Свой постер, своя озвучка (или озвучка подписи) и подпись → одно видео с постером под звук или картинка с аудио → в каналы.",
};
// gen — поколение отрисовки: +1 заставляет перерисовать все блоки (после сохранения правки, нового канала).
// expanded — раскрытие шагов руками: «id выпуска:id шага» → true/false, живёт между перерисовками.
const state = { config: null, projects: [], project: null, gen: 0, drafts: {}, stepDrafts: {}, timer: null, publish: {}, expanded: {}, statuses: null };

// --- настройки: поля по одному — из них собраны форма «нового выпуска», «настройки выпуска» и карточки шагов ---

function fieldsOf(values, prefix) {
  const cfg = state.config;
  const id = (name) => `${prefix}-${name}`;
  const radios = (name, options) =>
    h(
      "div",
      { class: "radio-row" },
      ...options.map(([value, label, checked]) => h("label", {}, h("input", { type: "radio", name: id(name), value, checked }), label)),
    );
  const check = (name, label, hint, checked) =>
    h(
      "label",
      { class: "check" },
      h("input", { type: "checkbox", id: id(name), checked }),
      h("span", {}, label, hint ? h("span", { class: "hint" }, hint) : null),
    );
  const select = (name, hint, options) =>
    h(
      "label",
      { class: "field" },
      h("span", { class: "hint" }, hint),
      h("select", { id: id(name) }, ...options.map(([value, label, selected]) => h("option", { value, selected }, label))),
    );
  const models = (list, current) => list.map((m) => [m.id, m.label + (m.installed ? "" : " — не установлена"), m.id === current]);
  const modes = (name, local, upload) =>
    radios(name, [
      ["local", local, values[name] !== "upload"],
      ["upload", upload, values[name] === "upload"],
    ]);
  return {
    poster_mode: () => modes("poster_mode", "Сгенерировать локальной моделью", "Загружу сам по промпту"),
    draw_images: () => check("draw_images", "Картинки на слайдах", "Иначе фон слайдов — градиент.", values.draw_images),
    images_mode: () => modes("images_mode", "Сгенерировать локальной моделью", "Сделаю сам по промптам во внешней модели"),
    image_model: () => select("image_model", "Модель для постера и картинок", models(cfg.image_models, values.image_model)),
    upscale_images: () =>
      select(
        "upscale_images",
        "Увеличить сгенерированные картинки и постер (SeedVR2: 2K ~20 с, 4K ~1,5 мин на картинку)",
        [["none", "Не увеличивать"], ["2k", "До 2K"], ["4k", "До 4K"]].map(([v, label]) => [v, label, v === values.upscale_images]),
      ),
    clips: () =>
      check(
        "clips",
        "Добавить ролики в ключевые моменты",
        "LLM выберет до 3 самых динамичных слайдов — там вместо картинки будет ролик (крутится по кругу без своего звука). Остальные слайды — картинка с медленным наездом.",
        values.clips,
      ),
    clips_mode: () => modes("clips_mode", "Сгенерировать локальной моделью", "Сделаю сам по промптам во внешней модели"),
    video_model: () => select("video_model", "Модель видео (ролик 5 с, 704×448)", models(cfg.video_models, values.video_model)),
    voice_mode: () =>
      radios("voice_mode", [
        ["tts", "Озвучить локальной моделью", values.voice_mode !== "upload"],
        ["upload", "Загружу свой mp3", values.voice_mode === "upload"],
      ]),
    engine: () => {
      const engine = cfg.engines.find((e) => e.id === values.engine) || cfg.engines[0];
      const voiceOptions = (e, current) => [
        h("option", { value: "" }, `по умолчанию (${e.default_voice})`),
        ...e.voices.map((v) => h("option", { value: v, selected: v === current }, v)),
      ];
      const voiceSelect = h("select", { id: id("voice") }, ...voiceOptions(engine, values.voice));
      const engineSelect = h(
        "select",
        { id: id("engine"), onchange: (event) => voiceSelect.replaceChildren(...voiceOptions(cfg.engines.find((x) => x.id === event.target.value), "")) },
        ...cfg.engines.map((e) => h("option", { value: e.id, selected: e.id === engine.id }, e.label)),
      );
      return h(
        "div",
        { class: "settings" },
        h("label", { class: "field" }, h("span", { class: "hint" }, "Движок"), engineSelect),
        h("label", { class: "field" }, h("span", { class: "hint" }, "Голос"), voiceSelect),
      );
    },
    shorts: () => check("shorts", "Сделать и вертикальное видео 9:16", "Короткий сценарий 45–60 с для YouTube Shorts, TikTok, Reels.", values.shorts),
    frame: () =>
      select(
        "frame",
        "Кадр видео (шортс всегда 9:16)",
        [
          ["poster", "Как постер — пропорции картинки обложки"],
          ["square", "Квадрат 1:1"],
          ["landscape", "Широкий 16:9"],
        ].map(([v, label]) => [v, label, v === (values.frame || "landscape")]),
      ),
    branding: () =>
      check(
        "branding",
        "Плашка «подпишись» и кнопка «плей»",
        "Первый кадр — обложка: постер притемнён, по центру плей, внизу крупная плашка; дальше плашка плавно уезжает в угол и остаётся до конца. Этот кадр — обложка видео в Telegram.",
        values.branding,
      ),
    disclaimers: () =>
      check(
        "disclaimers",
        "Помечать запрещённые в РФ организации",
        h("span", {}, "Мелкий текст на слайдах, где упомянута, например, Meta, и строка в конце поста. Список — на вкладке ", h("a", { class: "link", href: "/updates/" }, "«Обновления»"), "."),
        values.disclaimers,
      ),
    tempo: () =>
      select(
        "tempo",
        "Скорость в видео — ускоряет озвучку без смены высоты голоса, слайды подстраиваются под неё",
        [0.75, 1, 1.25, 1.5, 1.75, 2].map((t) => [t, `×${String(t).replace(".", ",")}`, t === Number(values.tempo || 1)]),
      ),
    slide_s: () =>
      select(
        "slide_s",
        "Сколько держится слайд — ориентир для плана: чем дольше, тем меньше слайдов и больше времени прочитать каждый",
        [4, 6, 8, 10, 12].map((t) => [t, `~${t} с`, t === Number(values.slide_s || 6)]),
      ),
    reading: () =>
      select(
        "reading",
        "Время на чтение — слайд не сменится, пока не прочитан его текст (по числу слов); следующие догонят озвучку позже",
        [
          ["normal", "Обычно — 3 слова в секунду"],
          ["slow", "Медленно — 2 слова в секунду"],
          ["off", "Не ждать — слайд меняется вместе с фразой"],
        ].map(([v, label]) => [v, label, v === (values.reading || "normal")]),
      ),
    channels: () =>
      cfg.channels.length
        ? h(
            "div",
            {},
            ...cfg.channels.map((c) =>
              h(
                "label",
                { class: "channel" },
                h("input", { type: "checkbox", name: id("channels"), value: c.id, checked: values.channels.includes(c.id) }),
                c.label,
                h("span", { class: "muted" }, c.format === "vertical" ? "9:16" : "16:9"),
                c.problem ? h("span", { class: "chip tone-danger" }, c.problem) : null,
              ),
            ),
            // Удаление канала затрагивает все сценарии — только на вкладке «Каналы», с подтверждением.
            h("p", { class: "hint" }, "Убрать канал из списка — на вкладке ", h("a", { class: "link", href: "/channels/" }, "«Каналы»"), "."),
          )
        : h("p", { class: "hint" }, `Каналы не настроены: ${cfg.channels_file}`),
    // Необратимое: выпуск уйдёт в каналы без просмотра — тумблер предупреждающего цвета, а не обычный флажок.
    auto_publish: () =>
      h(
        "label",
        { class: "check auto-publish" },
        h("input", { type: "checkbox", role: "switch", id: id("auto_publish"), checked: values.auto_publish }),
        h("span", {}, "Публиковать сразу, без моей проверки", h("span", { class: "hint warn" }, "Опубликуется без просмотра: выпуск уйдёт в каналы, как только соберётся.")),
      ),
    post_format: () =>
      h(
        "div",
        { class: "field" },
        h("span", { class: "hint" }, "Вид поста в Telegram: видео одним постом или картинка с подписью и следом аудио. Пакеты для ручной загрузки — всегда видео."),
        radios("post_format", [
          ["video", "Видео", values.post_format !== "photo"],
          ["photo", "Картинка, затем аудио", values.post_format === "photo"],
        ]),
      ),
    // «Видео по статье»: без источника выпуск не опубликуется — строка в конце поста и на последнем слайде.
    source_url: () =>
      h("label", { class: "field" }, h("span", { class: "hint" }, "Ссылка на оригинал статьи"), h("input", { type: "url", id: id("source_url"), value: values.source_url || "", placeholder: "https://…" })),
    source_author: () =>
      h("label", { class: "field" }, h("span", { class: "hint" }, "Автор оригинала"), h("input", { type: "text", id: id("source_author"), value: values.source_author || "", placeholder: "Имя автора или издание" })),
    review_text: () => check("review_text", "Пауза после текста — проверю его перед генерацией", null, values.review_text),
    draft: () =>
      check(
        "draft",
        "Черновик: без картинок и роликов, кадр вполовину",
        "Быстро посмотреть структуру и ритм. «Сделать чистовик» дорисует картинки и соберёт видео в полном кадре.",
        values.draft,
      ),
  };
}

// Добавить канал Telegram — в форме настроек и прямо в шаге «Публикация».
function channelAdder(prefix) {
  const id = (name) => `${prefix}-${name}`;
  return h(
    "details",
    { class: "add-channel" },
    h("summary", { class: "link" }, "Добавить канал Telegram"),
    h(
      "div",
      { class: "settings" },
      h("label", { class: "field" }, h("span", { class: "hint" }, "Название"), h("input", { type: "text", id: id("new-label"), placeholder: "Канал новостей" })),
      h("label", { class: "field" }, h("span", { class: "hint" }, "id: -100…, -100…_тема или @username"), h("input", { type: "text", id: id("new-chat"), placeholder: "-1003755616451_3" })),
      h(
        "label",
        { class: "field" },
        h("span", { class: "hint" }, "Видео"),
        h("select", { id: id("new-format") }, h("option", { value: "landscape" }, "16:9"), h("option", { value: "vertical" }, "9:16 (шортс)")),
      ),
    ),
    h("p", { class: "form-error", id: id("new-error"), hidden: true }),
    h("button", { type: "button", class: "secondary", onclick: () => addChannel(prefix) }, "Добавить"),
  );
}

function settingsForm(values, prefix) {
  const f = fieldsOf(values, prefix);
  const group = (legend, ...children) => h("fieldset", {}, h("legend", {}, legend), ...children);
  if (scenario === "post") {
    return h(
      "div",
      { class: "settings-form" },
      group("Постер", f.poster_mode(), f.image_model()),
      group("Озвучка", f.voice_mode(), f.engine()),
      group("Оформление", f.frame(), f.branding(), f.disclaimers()),
      group("Публикация", channelAdder(prefix), f.post_format(), f.tempo(), f.channels(), f.auto_publish()),
      f.review_text(),
    );
  }
  return h(
    "div",
    { class: "settings-form" },
    f.draft(),
    group("Постер — первый кадр", f.poster_mode()),
    group("Картинки слайдов", f.draw_images(), f.images_mode(), f.image_model(), f.upscale_images()),
    group("Ролики", f.clips(), f.clips_mode(), f.video_model()),
    group("Озвучка", f.voice_mode(), f.engine()),
    group("Шортс", f.shorts()),
    group("Слайды", f.slide_s(), f.reading()),
    group("Оформление", f.frame(), f.branding(), f.disclaimers()),
    group("Публикация", channelAdder(prefix), f.tempo(), f.channels(), f.auto_publish()),
    scenario === "article" ? group("Источник — обязателен для публикации", f.source_url(), f.source_author()) : null,
    f.review_text(),
  );
}

const SETTING_NAMES = [
  "poster_mode", "draw_images", "images_mode", "image_model", "upscale_images", "clips", "clips_mode", "video_model",
  "voice_mode", "engine", "shorts", "review_text", "disclaimers", "branding", "tempo", "frame", "auto_publish", "channels",
  "post_format", "draft", "slide_s", "reading",
]; // prettier-ignore
// Поля формы поста: остальные настройки у него не меняются.
const POST_SETTINGS = [
  "poster_mode", "image_model", "voice_mode", "engine", "review_text", "disclaimers", "branding", "tempo", "frame",
  "auto_publish", "channels", "post_format",
]; // prettier-ignore

// Читает поля формы с этим префиксом; names — какие именно (engine читается вместе с голосом).
// У статьи ещё источник: ссылка и автор оригинала.
const ARTICLE_SETTINGS = [...SETTING_NAMES, "source_url", "source_author"];

function readSettings(prefix, names = { post: POST_SETTINGS, article: ARTICLE_SETTINGS }[scenario] || SETTING_NAMES) {
  const get = (name) => document.getElementById(`${prefix}-${name}`);
  const radio = (name) => document.querySelector(`input[name="${prefix}-${name}"]:checked`)?.value;
  const special = {
    poster_mode: () => radio("poster_mode"),
    images_mode: () => radio("images_mode"),
    clips_mode: () => radio("clips_mode"),
    voice_mode: () => radio("voice_mode"),
    post_format: () => radio("post_format"),
    engine: () => ({ engine: get("engine").value, voice: get("voice").value }),
    tempo: () => ({ tempo: Number(get("tempo").value) }),
    slide_s: () => ({ slide_s: Number(get("slide_s").value) }),
    channels: () => ({ channels: [...document.querySelectorAll(`input[name="${prefix}-channels"]:checked`)].map((i) => i.value) }),
  };
  const out = {};
  for (const name of names) {
    const read = special[name];
    if (read) {
      const value = read();
      Object.assign(out, typeof value === "object" ? value : { [name]: value });
    } else {
      const input = get(name);
      out[name] = input.type === "checkbox" ? input.checked : input.value;
    }
  }
  return out;
}

// --- оценка времени и проверка до запуска ---

function duration(s) {
  if (!s) return "меньше минуты";
  const m = Math.round(s / 60);
  if (m < 1) return "меньше минуты";
  return m >= 60 ? `${Math.floor(m / 60)} ч ${m % 60} мин` : `${m} мин`;
}

// «≈ 1 ч 40 мин: картинки 18 шт. — 36 мин, озвучка — 50 мин…» и предупреждения: что сломается или будет ждать.
function checkBox(data, { done = false } = {}) {
  if (!data) return null;
  const { estimate: est, preflight = [] } = data;
  const parts = (est?.parts || []).map((p) => `${p.label}${p.count ? ` (${p.count})` : ""} — ${duration(p.seconds)}`);
  const head = done || !est?.parts?.length ? null : h("p", {}, h("strong", {}, `≈ ${duration(est.total_s)}`), h("span", { class: "muted" }, ` до готового видео${est.video_s ? `, ролик ~${duration(est.video_s)}` : ""}`));
  const items = preflight.map((x) => h("li", { class: x.level }, x.text));
  if (!head && !items.length) return null;
  return h(
    "div",
    { class: "check-box" },
    head,
    parts.length && !done ? h("details", {}, h("summary", { class: "link" }, "Из чего складывается"), h("ul", { class: "small" }, ...parts.map((t) => h("li", {}, t))), h("p", { class: "hint" }, est.from_history ? `По истории задач этой машины (${est.from_history}).` : "Истории задач ещё нет — оценка по умолчанию.")) : null,
    items.length ? h("ul", { class: "preflight" }, ...items) : null,
  );
}

const refreshCreateCheck = debounce(async () => {
  const box = $("create-check");
  if (!box) return;
  const payload = { scenario, settings: readSettings("new") };
  if (scenario !== "digest") Object.assign(payload, { text: $("text")?.value || "", post: $("post")?.value || "" });
  try {
    box.replaceChildren(checkBox(await api("POST", `${API}/preflight`, payload)) || "");
  } catch {
    box.replaceChildren();
  }
}, 400);

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
      : scenario === "post"
        ? postFields()
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
  const defaults = { ...state.config.defaults, ...(state.config.scenario_defaults?.[scenario] || {}) };
  $("create-settings").replaceChildren(settingsForm(defaults, "new"));
  refreshCreateCheck();
}

// Подпись поста со счётчиком: Telegram берёт под видео до 1024 знаков.
function captionCounter(area) {
  const count = h("span", { class: "hint" });
  const update = () => {
    const visible = area.value.replace(/\*\*/g, "").replace(/\[([^\]]+)\]\([^)]+\)/g, "$1").length;
    count.textContent = `${visible} знаков из ${CAPTION_LIMIT}${visible > CAPTION_LIMIT ? " — длинно: вместо подписи уйдёт заголовок" : ""}`;
    count.classList.toggle("warn", visible > CAPTION_LIMIT);
  };
  area.addEventListener("input", update);
  update();
  return count;
}

function postFields() {
  const post = h("textarea", { id: "post", rows: 7, placeholder: "**Жирный** и [ссылка](https://…) — как в post.md дайджеста." });
  return [
    h("label", { class: "field" }, h("span", { class: "field-label" }, "Заголовок"), h("input", { type: "text", id: "title", placeholder: "необязательно — из первой строки подписи" })),
    h("label", { class: "field" }, h("span", { class: "field-label" }, "Подпись к посту"), post, captionCounter(post)),
    h(
      "details",
      {},
      h("summary", { class: "link" }, "Текст для озвучки — если озвучиваем здесь"),
      h("textarea", { id: "text", rows: 5, placeholder: "Пусто — озвучим подпись. С «Загружу свой mp3» не нужен." }),
    ),
    h("p", { class: "hint" }, "Постер и mp3 перетащишь в шаги выпуска после создания."),
  ];
}

async function create(event, forceNew = false) {
  event?.preventDefault();
  const error = $("create-error");
  error.hidden = true;
  const payload = { scenario, settings: readSettings("new"), run: true, force_new: forceNew };
  if (scenario === "digest") payload.day = $("day").value;
  else if (scenario === "post") Object.assign(payload, { title: $("title").value, post: $("post").value, text: $("text").value });
  else Object.assign(payload, { title: $("title").value, text: $("text").value });
  $("create-btn").disabled = true;
  try {
    const project = await api("POST", `${API}/projects`, payload);
    if (scenario !== "digest") for (const id of ["text", "post"]) if ($(id)) $(id).value = "";
    await loadProjects();
    show(project);
  } catch (err) {
    const existing = err.data?.existing;
    // Тот же день или тот же текст: открыть готовый выпуск вместо новой генерации — или всё же создать новый.
    error.replaceChildren(
      err.message,
      ...(existing
        ? [
            " ",
            h("button", { type: "button", class: "link", onclick: () => (existing.scenario === scenario ? open(existing.id) : (location.href = `?scenario=${existing.scenario}&project=${existing.id}`)) }, "Открыть"),
            " · ",
            h("button", { type: "button", class: "link", onclick: () => create(null, true) }, "Создать новый"),
          ]
        : []),
    );
    error.hidden = false;
  } finally {
    $("create-btn").disabled = false;
  }
}

async function makeFinal() {
  if (await act("settings", { draft: false })) await act("run", {});
}

// Длинная статья: серия выпусков по главам или выжимка на 5–8 минут — отдельным выпуском.
function longTextActions(project) {
  if (project.scenario !== "article" || !project.preflight?.some((x) => x.text.includes("серии"))) return null;
  const summary = project.summary || {};
  return h(
    "div",
    { class: "actions" },
    h("button", { type: "button", class: "secondary", disabled: project.running, onclick: splitProject }, "Разбить на серию"),
    h("button", { type: "button", class: "secondary", disabled: summary.status === "running", onclick: () => act("summary") }, summary.status === "running" ? "Выжимка пишется…" : "Сделать выжимку"),
    summary.status === "done" ? h("a", { class: "link", href: `?scenario=article&project=${summary.project}` }, "Открыть выжимку") : null,
    summary.status === "failed" ? h("span", { class: "job-error" }, summary.error) : null,
  );
}

async function splitProject() {
  try {
    const { projects } = await api("POST", `${API}/projects/${state.project.id}/split`, {});
    await loadProjects();
    await open(projects[0].id);
  } catch (err) {
    alertBox(err.message);
  }
}

async function cloneProject() {
  try {
    const copy = await api("POST", `${API}/projects/${state.project.id}/clone`);
    await loadProjects();
    await open(copy.id);
  } catch (err) {
    alertBox(err.message);
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

// Фоновое обновление списка: сбой — в общую плашку ошибок, без «Uncaught (in promise)».
const reloadList = safe(() => loadProjects());

async function open(id) {
  state.drafts = {};
  state.stepDrafts = {};
  show(await api("GET", `${API}/projects/${id}`));
  reloadList();
  history.replaceState(null, "", `?scenario=${scenario}&project=${id}`);
}

// --- действия над выпуском ---

// --- каналы ---

function rerenderSettings(prefix) {
  if (prefix === "create") return renderCreate();
  state.gen += 1;
  show(state.project);
}

async function addChannel(prefix) {
  const get = (name) => document.getElementById(`${prefix}-${name}`);
  const error = get("new-error");
  try {
    // Каналы правит только вкладка «Каналы» (ChannelStore) — сценарии зовут её API.
    const res = await api("POST", "/api/channels/channels", { label: get("new-label").value, chat_id: get("new-chat").value, format: get("new-format").value });
    state.config.channels = res.channels;
    for (const name of ["new-label", "new-chat"]) get(name).value = ""; // иначе перерисовка вернёт набранное
    if (prefix === "step-publish") publishPick(state.project).channels.add(res.added); // канал из шага — сразу в выбранные
    rerenderSettings(prefix);
  } catch (err) {
    error.hidden = false;
    error.textContent = err.message;
  }
}

// Выбор каналов и «повторно» у кнопки «Опубликовать» живут между перерисовками страницы.
function publishPick(project) {
  if (!state.publish[project.id]) state.publish[project.id] = { channels: new Set(project.settings.channels), force: false, ignoreVerdict: false, noDisclaimers: "" };
  return state.publish[project.id];
}

// Публикация необратима: сначала подтверждение — каналы по названиям, начало подписи, вердикт судьи.
async function publishNow() {
  const project = state.project;
  const pick = publishPick(project);
  const chosen = state.config.channels.filter((c) => pick.channels.has(c.id));
  if (!chosen.length) return alertBox("Отметь хотя бы один канал в шаге «Публикация».");
  const unsaved = state.drafts.post !== undefined && state.drafts.post !== project.artifacts.post;
  const ok = await confirmPublish({
    channels: chosen,
    caption: project.artifacts.post || "",
    verdict: project.review?.verdict,
    review: project.review?.text,
    note: unsaved ? "Правка подписи не сохранена — уйдёт сохранённая подпись." : null,
  });
  if (!ok) return;
  await act("publish", { channels: [...pick.channels], force: pick.force, ignore_verdict: pick.ignoreVerdict, ignore_disclaimers: pick.noDisclaimers });
  pick.force = false;
}

async function act(path, body) {
  try {
    show(await api("POST", `${API}/projects/${state.project.id}/${path}`, body ?? {}));
    return true;
  } catch (err) {
    alertBox(err.message);
    return false;
  }
}

// Параметры шага прямо в его карточке: поменял → «Перезапустить шаг» пересобирает шаг уже с ними.
const STEP_FIELDS = {
  text: ["review_text"],
  poster: ["poster_mode", "image_model", "upscale_images"],
  images: ["draw_images", "images_mode", "image_model", "upscale_images"],
  clips: ["clips", "clips_mode", "video_model"],
  voice: ["voice_mode", "engine"],
  plan: ["slide_s"],
  render: ["draft", "frame", "tempo", "reading", "branding", "disclaimers"],
  short: ["shorts", "tempo", "reading", "branding", "disclaimers"],
};

const openSettings = new Set();

function stepSettings(step) {
  const names = STEP_FIELDS[step.id];
  if (!names) return null;
  const prefix = `step-${step.id}`;
  const f = fieldsOf({ ...state.project.settings, ...state.stepDrafts[step.id] }, prefix);
  return h(
    "details",
    // Раскрытость и набросок правки живут между перерисовками, пока шаг не перезапустили.
    {
      class: "step-settings",
      id: `${prefix}-settings`,
      open: openSettings.has(step.id),
      ontoggle: (e) => (e.target.open ? openSettings.add(step.id) : openSettings.delete(step.id)),
      onchange: () => (state.stepDrafts[step.id] = readSettings(prefix, names)),
    },
    h("summary", { class: "link" }, "Параметры шага"),
    ...names.map((name) => f[name]()),
    h("p", { class: "hint" }, "Поменяй и нажми «Перезапустить шаг». Шаги после него сбросятся — «Запустить всё» соберёт их заново."),
  );
}

// Перезапуск сбрасывает все зависимые шаги — это десятки минут GPU, поэтому сначала спрашиваем.
async function rerunStep(stepId) {
  const project = state.project;
  const step = project.steps.find((s) => s.id === stepId);
  if (step.status !== "pending") {
    const visible = visibleSteps(project);
    const resets = visible.filter((s) => (step.dependents || []).includes(s.id) && s.status !== "pending");
    const ok = await confirmAction({
      title: `Перезапустить «${step.label}»?`,
      body: resets.length
        ? [h("p", {}, "Сбросятся и соберутся заново:"), h("ul", { class: "confirm-list" }, ...resets.map((s) => h("li", {}, `${visible.indexOf(s) + 1}. ${s.label}`)))]
        : h("p", {}, "Другие шаги не затронуты."),
      confirmLabel: "Перезапустить",
      danger: resets.length > 0,
    });
    if (!ok) return;
  }
  const names = STEP_FIELDS[step.id];
  const settings = names && document.getElementById(`step-${step.id}-settings`) ? readSettings(`step-${step.id}`, names) : undefined;
  if (await act("run", { step: step.id, settings })) delete state.stepDrafts[step.id];
  state.gen += 1; // параметры шага — из сохранённых настроек, а не из набросков
  show(state.project);
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
    const fresh = await api("POST", `${API}/projects/${state.project.id}/artifacts/${name}`, { text });
    delete state.drafts[name];
    state.gen += 1;
    show(fresh);
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
  // id — чтобы после перерисовки вернуть фокус и курсор; текст живёт в state.drafts.
  const area = h("textarea", { id: `editor-${name}`, rows, class: mono ? "mono" : null, oninput: (e) => (state.drafts[name] = e.target.value) });
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
        const btn = e.currentTarget;
        try {
          await navigator.clipboard.writeText(text);
          btn.textContent = "Скопировано";
        } catch {
          btn.textContent = "Не вышло";
        }
        announce(btn.textContent);
        setTimeout(() => (btn.textContent = "Скопировать промпт"), 1500);
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

// Виды слайдов плана: картинка или динамическая сцена, которую рисует сборка.
const SLIDE_KINDS = { image: "картинка", bullets: "пункты", stat: "число", chart: "график", flow: "схема", compare: "сравнение", quote: "цитата" };

function slideItems(s) {
  const num = (p) => `${p.label}: ${p.value}${s.unit ? " " + s.unit : ""}`;
  switch (s.kind) {
    case "stat":
      return [`${s.prefix || ""}${s.value}${s.suffix || ""} — ${s.label || ""}`];
    case "chart":
      return (s.data || []).map(num);
    case "flow":
      return [(s.steps || []).join(" → ")];
    case "compare":
      return [`${s.left?.title || "Было"}: ${(s.left?.items || []).join("; ")}`, `${s.right?.title || "Стало"}: ${(s.right?.items || []).join("; ")}`];
    case "quote":
      return [`«${s.text}»${s.author ? " — " + s.author : ""}`];
    default:
      return s.bullets || [];
  }
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
  const tab = { photo: "Фото", video: "Видео", audio: "Аудио" }[step.job.tab] || step.job.tab;
  return h("a", { class: "link", href: `/${step.job.tab}/` }, `задача на вкладке «${tab}»`);
}

const VERDICT_TONE = { готово: "success", "готово с оговорками": "warning", "не готово": "danger" };

// Вердикт судьи дайджеста: отдельный запуск claude проверил выпуск по первоисточникам.
function verdictBox(review) {
  if (!review) return null;
  const label = review.verdict ? `Судья: ${review.verdict}` : "Судья: вердикта нет";
  return h(
    "div",
    { class: "note verdict" },
    h("span", { class: `chip tone-${VERDICT_TONE[review.verdict] || "neutral"}` }, label),
    review.verdict === "не готово" ? h("span", { class: "hint" }, " Публикация ждёт: поправь выпуск или разреши её в шаге «Публикация».") : null,
    h("details", {}, h("summary", { class: "link" }, "Отчёт судьи"), h("pre", { class: "log" }, review.text)),
  );
}

function disclaimerBox() {
  const found = state.project.disclaimers || [];
  if (!found.length) return null;
  if (!state.project.settings.disclaimers) {
    return h("p", { class: "hint" }, `В тексте есть ${found.map((d) => d.name).join(", ")} — пометки выключены в настройках выпуска.`);
  }
  return h(
    "div",
    { class: "note disclaimers" },
    h("strong", {}, "Пометки на видео и в посте"),
    h("ul", {}, ...found.map((d) => h("li", {}, d.note))),
  );
}

function stepBody(step) {
  const p = state.project;
  const a = p.artifacts;
  const f = p.files;
  switch (step.id) {
    case "text":
      if (p.scenario === "post") {
        const captionArea = editor("post", a.post, 6);
        return [
          h("p", { class: "field-label" }, "Подпись к посту"),
          captionArea,
          captionCounter(captionArea.querySelector("textarea")),
          h("details", {}, h("summary", { class: "link" }, "Текст для озвучки"), editor("text", a.text, 6)),
        ];
      }
      return [verdictBox(p.review), a.text || step.status !== "pending" ? editor("text", a.text, 12) : h("p", { class: "muted" }, "Текст появится после сбора новостей.")];
    case "poster_prompt":
      return a.poster_prompt
        ? [
            editor("poster_prompt", a.poster_prompt, 3),
            copyButton(a.poster_prompt),
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
        const kind = s.kind || (s.image_prompt ? "image" : "bullets");
        const where = `абзац ${s.para + 1}, фраза ${(s.sent || 0) + 1} · ${SLIDE_KINDS[kind] || kind}`;
        const items = slideItems(s);
        return h(
          "div",
          { class: "slide-card" },
          img ? h("img", { src: img, alt: "" }) : h("div", { class: "ph" }, kind === "image" ? "" : SLIDE_KINDS[kind] || ""),
          h("div", {}, h("strong", {}, s.title), h("div", { class: "para" }, where), items.length ? h("ul", {}, ...items.map((b) => h("li", {}, b))) : null),
        );
      });
      const dynamic = a.plan.slides.filter((s) => (s.kind || (s.image_prompt ? "image" : "bullets")) !== "image").length;
      return [
        a.plan.title ? h("p", {}, h("span", { class: "muted" }, "Название: "), a.plan.title) : null,
        h("p", { class: "muted small" }, `${a.plan.slides.length} слайдов: ${a.plan.slides.length - dynamic} с картинкой, ${dynamic} динамических`),
        h("div", { class: "slides-list" }, ...cards),
        h("details", {}, h("summary", { class: "link" }, "Править план (JSON)"), editor("plan", JSON.stringify(a.plan, null, 2), 14, true)),
      ];
    }
    case "images":
      if (!p.settings.draw_images) return [];
      return [step.status === "running" ? jobLink(step) : null, ...mediaRows("images")];
    case "clips":
      if (!p.settings.clips) return [];
      return [step.status === "running" ? jobLink(step) : null, ...mediaRows("clips")];
    case "voice":
      return [
        f.voice ? h("audio", { controls: true, src: f.voice, preload: "metadata" }) : null,
        h("div", { class: "actions" }, uploader("voice", "audio/mpeg,audio/wav,audio/mp4,.m4a", f.voice ? "Заменить своим mp3" : "Загрузить озвучку"), jobLink(step)),
      ];
    case "render":
      return [
        disclaimerBox(),
        ...(f.video
          ? [h("video", { controls: true, src: f.video, preload: "metadata", poster: f.poster || null }), h("a", { class: "link", href: `${f.video.split("?")[0]}?download=1` }, "Скачать видео")]
          : []),
      ];
    case "short":
      return [
        a.short ? h("details", {}, h("summary", { class: "link" }, `Сценарий шортса: ${a.short.beats.length} частей`), editor("short", JSON.stringify(a.short, null, 2), 10, true)) : null,
        f.short_video
          ? h("div", {}, h("video", { class: "vertical-video", controls: true, src: f.short_video, preload: "metadata" }), h("p", {}, h("a", { class: "link", href: `${f.short_video.split("?")[0]}?download=1` }, "Скачать шортс 9:16")))
          : null,
      ];
    case "publish": {
      const pick = publishPick(p);
      const sentTo = new Set((p.published || []).filter((r) => r.sent).map((r) => r.channel));
      return [
        p.scenario === "post" ? null : editor("post", a.post, 5),
        h(
          "div",
          { class: "publish-box" },
          h("strong", {}, "Куда опубликовать"),
          h(
            "div",
            { class: "publish-channels" },
            ...state.config.channels.map((c) =>
              h(
                "label",
                { class: "channel" },
                h("input", { type: "checkbox", checked: pick.channels.has(c.id), onchange: (e) => (e.target.checked ? pick.channels.add(c.id) : pick.channels.delete(c.id)) }),
                c.label,
                h("span", { class: "muted" }, c.format === "vertical" ? "9:16" : "16:9"),
                sentTo.has(c.id) ? h("span", { class: "chip tone-success" }, "уже опубликовано") : null,
                c.problem ? h("span", { class: "chip tone-danger" }, c.problem) : null,
              ),
            ),
          ),
          h(
            "label",
            { class: "check" },
            h("input", { type: "checkbox", checked: pick.force, onchange: (e) => (pick.force = e.target.checked) }),
            h("span", {}, "Опубликовать повторно", h("span", { class: "hint" }, "Отправить и туда, куда выпуск уже ушёл — как FORCE=1 у make news-publish.")),
          ),
          p.review?.verdict === "не готово"
            ? h(
                "label",
                { class: "check" },
                h("input", { type: "checkbox", checked: pick.ignoreVerdict, onchange: (e) => (pick.ignoreVerdict = e.target.checked) }),
                h("span", {}, "Опубликовать несмотря на вердикт", h("span", { class: "hint" }, "Судья сказал «не готово» — как --force у make news-publish.")),
              )
            : null,
          // Пометки выключены, а в тексте есть упоминания из реестра — публикация только с причиной.
          p.settings.disclaimers === false && p.disclaimers?.length
            ? h(
                "label",
                { class: "check" },
                h("span", {}, `Без пометок о: ${p.disclaimers.map((d) => d.name).join(", ")}`, h("span", { class: "hint" }, "Пометки выключены. Причина обязательна — она сохранится в записи публикации.")),
                h("input", { type: "text", value: pick.noDisclaimers, placeholder: "Причина публикации без пометок", oninput: (e) => (pick.noDisclaimers = e.target.value) }),
              )
            : null,
          channelAdder("step-publish"),
          h("button", { type: "button", class: "primary", disabled: p.running, onclick: publishNow }, "Опубликовать…"),
        ),
        p.published
          ? h(
              "ul",
              { class: "results" },
              ...p.published.map((r) => {
                // Журнал публикации (BE-001): ответа Telegram не было — решает человек, проверив канал.
                const unclear = r.parts ? Object.values(r.parts).some((x) => ["sending", "unknown"].includes(x.state)) : ["sending", "unknown"].includes(r.state) && !r.message_id;
                const mark = unclear ? "?" : r.skipped ? "↷" : r.state === "failed" ? "✗" : "✓";
                return h(
                  "li",
                  {},
                  `${mark} ${r.label}: ${r.message} `,
                  ...(r.files || []).filter((x) => x.endsWith(".mp4")).map((x) => h("a", { class: "link", href: `${API}/projects/${p.id}/files/${x}?download=1` }, "скачать")),
                  ...(unclear && !p.running
                    ? [
                        h("button", { type: "button", onclick: () => act("publish/resolve", { channel: r.channel, posted: true }) }, "Пост есть"),
                        h("button", { type: "button", onclick: () => act("publish/resolve", { channel: r.channel, posted: false }) }, "Поста нет"),
                      ]
                    : []),
                );
              }),
            )
          : null,
      ];
    }
    default:
      return [];
  }
}

// --- шаги: свёрнутые готовые, раскрытые активные ---

const visibleSteps = (project) => project.steps.filter((s) => !HIDDEN_STEPS[project.scenario]?.has(s.id));

// Готовые и пропущенные шаги по умолчанию свёрнуты в строку; раскрытие руками помнится между опросами.
function collapsed(step) {
  const own = state.expanded[`${state.project.id}:${step.id}`];
  if (own !== undefined) return !own;
  return step.status === "done" || step.status === "skipped";
}

function toggleStep(stepId) {
  const step = state.project.steps.find((s) => s.id === stepId);
  state.expanded[`${state.project.id}:${stepId}`] = collapsed(step);
  show(state.project);
}

// Прокрутить к шагу (раскрыв его) — из степпера и главной кнопки шапки.
function goToStep(stepId) {
  const step = state.project.steps.find((s) => s.id === stepId);
  if (step && collapsed(step)) {
    state.expanded[`${state.project.id}:${stepId}`] = true;
    show(state.project);
  }
  const node = $(`step-${stepId}`);
  if (!node) return;
  node.scrollIntoView({ behavior: "smooth", block: "start" });
  node.focus({ preventScroll: true });
}

const plural = (n, one, few, many) => {
  const d = n % 10;
  const dd = n % 100;
  return `${n} ${d === 1 && dd !== 11 ? one : d >= 2 && d <= 4 && (dd < 12 || dd > 14) ? few : many}`;
};

// Сводка свёрнутого шага одной строкой: «71 слайд, 26 с картинкой».
function stepSummary(step) {
  const p = state.project;
  const a = p.artifacts;
  const f = p.files;
  if (step.status === "skipped") return step.note || "";
  switch (step.id) {
    case "text": {
      const words = (p.scenario === "post" ? a.post : a.text)?.split(/\s+/).filter(Boolean).length || 0;
      return [plural(words, "слово", "слова", "слов"), p.review?.verdict ? `судья: ${p.review.verdict}` : ""].filter(Boolean).join(" · ");
    }
    case "poster_prompt":
      return a.poster_prompt ? `${a.poster_prompt.slice(0, 80)}${a.poster_prompt.length > 80 ? "…" : ""}` : "";
    case "poster":
      return f.poster ? "постер готов" : "";
    case "plan": {
      if (!a.plan) return "";
      const n = a.plan.slides.length;
      const withImage = a.plan.slides.filter((s) => (s.kind || (s.image_prompt ? "image" : "bullets")) === "image").length;
      return `${plural(n, "слайд", "слайда", "слайдов")}, ${withImage} с картинкой`;
    }
    case "images":
      return plural(Object.keys(f.images || {}).length, "картинка", "картинки", "картинок");
    case "clips":
      return plural(p.slides.filter((s) => s.clip).length, "ролик", "ролика", "роликов");
    case "voice":
      return f.voice ? "озвучка готова" : "";
    case "render":
      return f.video ? "видео собрано" : "";
    case "short":
      return f.short_video ? "шортс собран" : "";
    case "publish": {
      const sent = (p.published || []).filter((r) => r.sent).map((r) => r.label);
      return sent.length ? `опубликовано: ${sent.join(", ")}` : "";
    }
    default:
      return "";
  }
}

function renderStep(step) {
  const p = state.project;
  const approveLabel = { text: "Продолжить", images: "Продолжить без недостающих", clips: "Продолжить без недостающих" }[step.id];
  const localMode = (step.id === "images" && p.settings.images_mode !== "upload") || (step.id === "clips" && p.settings.clips_mode !== "upload");
  const canApprove = step.status === "waiting" && approveLabel && !localMode;
  const folded = collapsed(step);
  const bodyId = `step-${step.id}-body`;
  const head = h(
    "div",
    { class: "step-head" },
    h("h3", {}, step.label),
    statusChip(step.status),
    folded ? h("span", { class: "step-summary muted small" }, stepSummary(step)) : null,
    h("button", { type: "button", class: "link", "aria-expanded": String(!folded), "aria-controls": bodyId, onclick: () => toggleStep(step.id) }, folded ? "Развернуть" : "Свернуть"),
    step.status !== "running"
      ? step.status === "pending"
        ? h("button", { type: "button", class: "link", disabled: p.running, onclick: () => rerunStep(step.id) }, "Выполнить шаг")
        : h("button", { type: "button", class: "secondary small", disabled: p.running, onclick: () => rerunStep(step.id) }, "Перезапустить шаг")
      : null,
  );
  const attrs = { class: `step ${step.status} tone-${toneOf(step.status)}${folded ? " collapsed" : ""}`, id: `step-${step.id}`, tabindex: "-1" };
  if (folded) return h("article", attrs, head);
  return h(
    "article",
    attrs,
    head,
    h(
      "div",
      { class: "step-main", id: bodyId },
      step.status === "running" ? progressBar(step.progress || { stage: "…", percent: 0 }) : null,
      step.error ? h("p", { class: "job-error" }, step.error) : null,
      step.note ? h("p", { class: "step-note" }, step.note) : null,
      step.status === "pending" && step.blocked_by.length
        ? h("p", { class: "hint" }, `Начнётся, когда будет готово: ${step.blocked_by.join(", ")}.`)
        : null,
      canApprove ? h("button", { type: "button", class: "primary", onclick: () => act("approve", { step: step.id }) }, approveLabel) : null,
      stepSettings(step),
      h("div", { class: "step-body" }, ...stepBody(step)),
    ),
  );
}

// --- шапка: главное действие по состоянию выпуска, степпер ---

const UPLOADS = {
  poster: ["poster_mode", "Загрузить постер"],
  images: ["images_mode", "Загрузить картинки"],
  clips: ["clips_mode", "Загрузить ролики"],
  voice: ["voice_mode", "Загрузить озвучку"],
};

// Что выпуску нужно дальше: { label, run, runAll } или null, если делать нечего.
function nextAction(project) {
  if (project.running) return null;
  const steps = visibleSteps(project);
  const need = steps.find((s) => s.status === "waiting" || s.status === "failed");
  if (need?.status === "failed") return { label: `Повторить шаг ${steps.indexOf(need) + 1}: ${need.label}`, run: () => rerunStep(need.id) };
  if (need) {
    if (need.id === "publish") {
      const blocked = project.review?.verdict === "не готово" && !publishPick(project).ignoreVerdict;
      return blocked ? { label: "К публикации: судья против", run: () => goToStep("publish") } : { label: "Опубликовать…", run: publishNow };
    }
    if (need.id === "text" && project.settings.review_text) return { label: "Текст ок, продолжить", run: () => act("approve", { step: "text" }) };
    const [mode, label] = UPLOADS[need.id] || [];
    if (mode && project.settings[mode] === "upload") return { label, run: () => goToStep(need.id) };
    return { label: `К шагу «${need.label}»`, run: () => goToStep(need.id) };
  }
  if (steps.some((s) => s.status === "pending")) return { label: "Запустить всё", run: () => act("run", {}), runAll: true };
  return null;
}

function overallStatus(project) {
  const steps = project.steps;
  if (project.running) return "running";
  if (steps.some((s) => s.status === "failed")) return "failed";
  if (steps.some((s) => s.status === "waiting")) return "waiting";
  return steps.every((s) => s.status === "done" || s.status === "skipped") ? "done" : "pending";
}

function renderHead(project) {
  const running = project.running;
  const next = nextAction(project);
  return h(
    "div",
    { class: "panel project-head" },
    h("h2", {}, project.title),
    statusChip(overallStatus(project)),
    running ? h("button", { type: "button", class: "secondary", onclick: () => act("cancel") }, "Остановить") : null,
    next ? h("button", { type: "button", class: "primary", onclick: next.run }, next.label) : null,
    // «Запустить всё» — главное действие, только когда выпуску больше ничего от тебя не нужно.
    !running && !next?.runAll ? h("button", { type: "button", class: "secondary", onclick: () => act("run", {}) }, "Запустить всё") : null,
    project.settings.draft ? h("span", { class: "chip tone-warning" }, "черновик") : null,
    project.settings.draft && !running
      ? h("button", { type: "button", class: "secondary", title: "Дорисовать картинки и ролики и собрать видео в полном кадре", onclick: makeFinal }, "Сделать чистовик")
      : null,
    project.archived ? h("span", { class: "chip tone-neutral" }, "в архиве") : null,
    running
      ? null
      : h("button", { type: "button", class: "link", title: "Те же настройки и готовые результаты до сборки — чтобы попробовать другой вариант, не трогая этот", onclick: cloneProject }, "Копия выпуска"),
    running
      ? null
      : h(
          "button",
          { type: "button", class: "link", onclick: async () => (await act("archive", { archived: !state.project.archived })) && reloadList() },
          project.archived ? "Вернуть из архива" : "В архив",
        ),
    h("p", { class: "form-error", id: "project-error", role: "alert", hidden: true }),
  );
}

const MARKS = { done: "✓", skipped: "↷", running: "●", waiting: "!", failed: "×", pending: "○" };

// Карта пайплайна: липкая строка шагов со статусами, клик — к шагу.
function renderStepper(project) {
  const steps = visibleSteps(project);
  return h(
    "nav",
    { class: "stepper", "aria-label": "Шаги выпуска" },
    h(
      "ol",
      {},
      ...steps.map((s, i) =>
        h(
          "li",
          {},
          h(
            "button",
            { type: "button", class: `stepper-item ${s.status} tone-${toneOf(s.status)}`, title: STATUS[s.status] || s.status, onclick: () => goToStep(s.id) },
            h("span", { class: "mark", "aria-hidden": "true" }, MARKS[s.status] || "○"),
            h("span", {}, `${i + 1}. ${s.label}`),
            h("span", { class: "sr-only" }, `: ${STATUS[s.status] || s.status}`),
          ),
        ),
      ),
    ),
  );
}

function renderSettings(project) {
  return h(
    "details",
    { class: "panel settings-box", id: "edit-settings" },
    h("summary", {}, "Настройки выпуска"),
    settingsForm(project.settings, "edit"),
    h(
      "div",
      { class: "actions" },
      h("button", { type: "button", class: "secondary", disabled: project.running, onclick: () => act("settings", readSettings("edit")) }, "Сохранить настройки"),
      h("span", { class: "hint" }, "Изменённые настройки сбросят только затронутые шаги."),
    ),
  );
}

// --- отрисовка по блокам: опрос перерисовывает только изменившиеся блоки и шаги ---

// Данные выпуска, от которых зависит тело шага (кроме самого шага, настроек и running).
const STEP_DATA = {
  text: (p) => [p.artifacts.text, p.artifacts.post, p.review],
  poster_prompt: (p) => [p.artifacts.poster_prompt],
  poster: (p) => [p.artifacts.poster_prompt, p.files.poster],
  plan: (p) => [p.artifacts.plan, p.files.images],
  images: (p) => [p.slides],
  clips: (p) => [p.slides],
  voice: (p) => [p.files.voice],
  render: (p) => [p.files.video, p.files.poster, p.disclaimers],
  short: (p) => [p.artifacts.short, p.files.short_video],
  publish: (p) => {
    const pick = publishPick(p);
    return [p.artifacts.post, p.published, p.review, state.config.channels, [...pick.channels], pick.force, pick.ignoreVerdict];
  },
};

const blockSig = new WeakMap();
const TEXT_INPUTS = new Set(["text", "search", "url", "email", "number", "tel", "password", "date", "time", "datetime-local", "month", "week"]);
const isEditable = (el) => Boolean(el) && (el.tagName === "TEXTAREA" || el.tagName === "SELECT" || el.isContentEditable || (el.tagName === "INPUT" && TEXT_INPUTS.has(el.type)));

// В блоке фокус в поле, которое не вернуть после перерисовки (открытый список, поле без id) — блок подождёт.
function holdsInput(node) {
  const el = document.activeElement;
  return node.contains(el) && isEditable(el) && (el.tagName === "SELECT" || el.isContentEditable || !el.id);
}

const fieldKey = (el) => (el.type === "radio" ? (el.name ? `${el.name}=${el.value}` : null) : el.id || null);

// Несохранённые правки полей блока: чекбоксы, переключатели, списки и однострочные поля (тексты — в state.drafts).
function editsOf(node) {
  const out = new Map();
  for (const el of node.querySelectorAll("input, select")) {
    const key = fieldKey(el);
    if (!key || el.type === "file") continue;
    if (el.type === "checkbox" || el.type === "radio") {
      if (el.checked !== el.defaultChecked) out.set(key, el.checked);
    } else if (el.tagName === "SELECT") {
      if ([...el.options].some((o) => o.selected !== o.defaultSelected)) out.set(key, el.value);
    } else if (el.value !== el.defaultValue) out.set(key, el.value);
  }
  return out;
}

function applyEdits(node, edits) {
  if (!edits.size) return;
  for (const el of node.querySelectorAll("input, select")) {
    const key = fieldKey(el);
    if (!key || !edits.has(key)) continue;
    if (el.type === "checkbox" || el.type === "radio") el.checked = edits.get(key);
    else el.value = edits.get(key);
  }
}

const detailsKey = (d) => d.id || (d.querySelector(":scope > summary")?.textContent || "").replace(/\d+/g, "#");

// Перенести из старого узла блока в новый то, что человек уже сделал: правки полей, раскрытые <details>,
// и сами медиа-элементы с тем же адресом — ролик в превью не начнётся сначала, плеер не собьётся.
function carryOver(prev, node) {
  applyEdits(node, editsOf(prev));
  const opened = new Map([...prev.querySelectorAll("details")].map((d) => [detailsKey(d), d.open]));
  for (const d of node.querySelectorAll("details")) if (opened.has(detailsKey(d))) d.open = opened.get(detailsKey(d));
  const media = new Map();
  for (const m of prev.querySelectorAll("video[src], audio[src], img[src]")) {
    const key = `${m.tagName}:${m.getAttribute("src")}`;
    if (!media.has(key)) media.set(key, m);
  }
  for (const m of node.querySelectorAll("video[src], audio[src], img[src]")) {
    const key = `${m.tagName}:${m.getAttribute("src")}`;
    const twin = media.get(key);
    if (!twin) continue;
    media.delete(key);
    twin.className = m.className;
    m.replaceWith(twin); // перенос в том же такте: браузер не ставит видео на паузу и не грузит заново
  }
}

// items: [{ key, sig, build }] — узел с той же подписью остаётся как есть, изменившийся пересобирается.
function reconcile(parent, items, projectId) {
  const old = new Map();
  for (const n of parent.children) if (n.dataset.key) old.set(n.dataset.key, n);
  const nodes = items.map(({ key, sig, build }) => {
    const prev = old.get(key);
    if (prev && (blockSig.get(prev) === sig || holdsInput(prev))) return prev;
    const node = build();
    node.dataset.key = key;
    node.dataset.project = projectId;
    blockSig.set(node, sig);
    if (prev && prev.dataset.project === projectId) carryOver(prev, node);
    return node;
  });
  for (const n of [...parent.children]) if (!nodes.includes(n)) n.remove();
  nodes.forEach((n, i) => {
    if (parent.children[i] !== n) parent.insertBefore(n, parent.children[i] || null);
  });
}

// Фокус и курсор в поле переживают перерисовку его блока.
function captureFocus() {
  const el = document.activeElement;
  if (!el?.id || !$("project").contains(el)) return null;
  let start = null;
  let end = null;
  try {
    start = el.selectionStart;
    end = el.selectionEnd;
  } catch {
    /* у чекбоксов и списков выделения нет */
  }
  return { el, id: el.id, start, end, scroll: el.scrollTop };
}

function restoreFocus(saved) {
  if (!saved || saved.el.isConnected) return;
  const twin = document.getElementById(saved.id);
  if (!twin) return;
  twin.focus({ preventScroll: true });
  try {
    if (saved.start !== null) twin.setSelectionRange(saved.start, saved.end);
  } catch {
    /* поле без выделения */
  }
  twin.scrollTop = saved.scroll;
}

// Скринридеру — только переходы шагов («готово», «ошибка», «ждёт вас»), не каждую перерисовку.
function announceChanges(project) {
  const now = Object.fromEntries(project.steps.map((s) => [s.id, s.status]));
  const before = state.statuses?.id === project.id ? state.statuses.steps : null;
  state.statuses = { id: project.id, steps: now };
  if (!before) return;
  const changed = visibleSteps(project).filter((s) => before[s.id] !== s.status && ["done", "failed", "waiting"].includes(s.status));
  if (changed.length) announce(changed.map((s) => `Шаг «${s.label}»: ${STATUS[s.status]}`).join(". "));
}

function show(project) {
  state.project = project;
  announceChanges(project);
  const saved = captureFocus();
  const base = [state.gen, project.id];
  const sig = (...parts) => JSON.stringify([base, ...parts]);
  const statuses = project.steps.map((s) => s.status);
  const check = checkBox(project, { done: !project.estimate?.parts?.length });
  const root = $("project");
  reconcile(
    root,
    [
      {
        key: "head",
        sig: sig(project.title, project.running, statuses, project.settings, project.archived, project.review?.verdict, publishPick(project).ignoreVerdict),
        build: () => renderHead(project),
      },
      { key: "settings", sig: sig(project.settings, state.config.channels, project.running), build: () => renderSettings(project) },
      check
        ? { key: "check", sig: sig(project.estimate, project.preflight, project.summary, project.running, project.scenario), build: () => h("div", { class: "panel" }, check, longTextActions(project)) }
        : null,
      { key: "stepper", sig: sig(statuses), build: () => renderStepper(project) },
      { key: "steps", sig: sig(), build: () => h("div", { class: "steps" }) },
      { key: "log", sig: sig(project.log), build: () => h("div", { class: "panel" }, logBlock(project.log, "Журнал выпуска") || h("p", { class: "muted small" }, "Журнал пуст.")) },
    ].filter(Boolean),
    project.id,
  );
  reconcile(
    root.querySelector(':scope > [data-key="steps"]'),
    visibleSteps(project).map((step) => ({
      key: step.id,
      sig: sig(step, project.running, project.settings, collapsed(step), STEP_DATA[step.id]?.(project)),
      build: () => renderStep(step),
    })),
    project.id,
  );
  restoreFocus(saved);
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
      const { id, running } = state.project;
      const fresh = await api("GET", `${API}/projects/${id}`);
      if (state.project.id !== id) return; // пока ждали ответ, открыли другой выпуск
      // Блоки с полем в фокусе не пересобираются или возвращают фокус и курсор — см. reconcile.
      show(fresh);
      if (fresh.running !== running) reloadList();
    }
  } catch {
    schedule();
  }
}

// Липкий степпер встаёт под шапку платформы: её высота зависит от ширины окна.
function trackNavHeight() {
  const nav = document.querySelector(".platform-nav");
  if (nav) document.documentElement.style.setProperty("--nav-h", `${nav.offsetHeight}px`);
}
window.addEventListener("resize", debounce(trackNavHeight, 150));

async function init() {
  trackNavHeight();
  $("scenario-title").textContent = SCENARIO_TITLES[scenario];
  $("scenario-hint").textContent = HINTS[scenario];
  document.title = `${$("scenario-title").textContent} — AI Platform`;
  state.config = await api("GET", `${API}/config`);
  renderCreate();
  $("create").addEventListener("submit", create);
  $("create").addEventListener("input", refreshCreateCheck);
  $("create").addEventListener("change", refreshCreateCheck);
  await loadProjects();
  const wanted = new URLSearchParams(location.search).get("project");
  const first = wanted || state.projects[0]?.id;
  if (first) await open(first).catch(() => {});
  setInterval(reloadList, 10000);
}

init().catch((err) => {
  $("project").replaceChildren(h("p", { class: "job-error" }, `Не удалось загрузить сценарии: ${err.message}`));
});
