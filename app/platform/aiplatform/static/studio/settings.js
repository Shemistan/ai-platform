// Студия: настройки выпуска — поля по одному, из них собраны форма «нового выпуска»,
// «Настройки выпуска» и «Параметры шага»; добавление канала Telegram.
import { api, busyClick, h } from "/ui.js";
import { scenario, state } from "./state.js";
import { renderCreate } from "./create.js";
import { publishPick } from "./publish.js";
import { act, show } from "./app.js";

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
    ai_badge: () =>
      check(
        "ai_badge",
        "Плашка «ИИ» в углу видео",
        "Мелкая метка в правом нижнем углу: озвучка и картинки сделаны нейросетью. Строка об ИИ в посте и метка в файле видео ставятся всегда.",
        values.ai_badge,
      ),
    disclaimers: () =>
      check(
        "disclaimers",
        "Помечать запрещённые в РФ организации",
        h("span", {}, "Мелкий текст на слайдах, где упомянута, например, Meta, и строка в конце поста. Список — на вкладке ", h("a", { class: "btn--link", href: "/updates/" }, "«Обновления»"), "."),
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
            h("p", { class: "hint" }, "Убрать канал из списка — на вкладке ", h("a", { class: "btn--link", href: "/channels/" }, "«Каналы»"), "."),
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
export function channelAdder(prefix) {
  const id = (name) => `${prefix}-${name}`;
  return h(
    "details",
    { class: "disclosure add-channel" },
    h("summary", {}, "Добавить канал Telegram"),
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
    h("button", { type: "button", class: "btn btn--secondary", onclick: busyClick(() => addChannel(prefix)) }, "Добавить"),
  );
}

export function settingsForm(values, prefix) {
  const f = fieldsOf(values, prefix);
  const group = (legend, ...children) => h("fieldset", {}, h("legend", {}, legend), ...children);
  if (scenario === "post") {
    return h(
      "div",
      { class: "settings-form" },
      group("Постер", f.poster_mode(), f.image_model()),
      group("Озвучка", f.voice_mode(), f.engine()),
      group("Оформление", f.frame(), f.branding(), f.ai_badge(), f.disclaimers()),
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
    group("Оформление", f.frame(), f.branding(), f.ai_badge(), f.disclaimers()),
    group("Публикация", channelAdder(prefix), f.tempo(), f.channels(), f.auto_publish()),
    scenario === "article" ? group("Источник — обязателен для публикации", f.source_url(), f.source_author()) : null,
    f.review_text(),
  );
}

const SETTING_NAMES = [
  "poster_mode", "draw_images", "images_mode", "image_model", "upscale_images", "clips", "clips_mode", "video_model",
  "voice_mode", "engine", "shorts", "review_text", "disclaimers", "ai_badge", "branding", "tempo", "frame", "auto_publish", "channels",
  "post_format", "draft", "slide_s", "reading",
]; // prettier-ignore

// Поля формы поста: остальные настройки у него не меняются.
const POST_SETTINGS = [
  "poster_mode", "image_model", "voice_mode", "engine", "review_text", "disclaimers", "ai_badge", "branding", "tempo", "frame",
  "auto_publish", "channels", "post_format",
]; // prettier-ignore

// Читает поля формы с этим префиксом; names — какие именно (engine читается вместе с голосом).
// У статьи ещё источник: ссылка и автор оригинала.
const ARTICLE_SETTINGS = [...SETTING_NAMES, "source_url", "source_author"];

export function readSettings(prefix, names = { post: POST_SETTINGS, article: ARTICLE_SETTINGS }[scenario] || SETTING_NAMES) {
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

// Параметры шага прямо в его карточке: поменял → «Перезапустить шаг» пересобирает шаг уже с ними.
export const STEP_FIELDS = {
  text: ["review_text"],
  poster: ["poster_mode", "image_model", "upscale_images"],
  images: ["draw_images", "images_mode", "image_model", "upscale_images"],
  clips: ["clips", "clips_mode", "video_model"],
  voice: ["voice_mode", "engine"],
  plan: ["slide_s"],
  render: ["draft", "frame", "tempo", "reading", "branding", "disclaimers", "ai_badge"],
  short: ["shorts", "tempo", "reading", "branding", "disclaimers", "ai_badge"],
};

export function stepSettings(step) {
  const names = STEP_FIELDS[step.id];
  if (!names) return null;
  const prefix = `step-${step.id}`;
  const f = fieldsOf({ ...state.project.settings, ...state.stepDrafts[step.id] }, prefix);
  return h(
    "details",
    // Раскрытость и набросок правки живут между перерисовками, пока шаг не перезапустили.
    {
      class: "disclosure step-settings",
      id: `${prefix}-settings`,
      open: state.openSettings.has(step.id),
      ontoggle: (e) => (e.target.open ? state.openSettings.add(step.id) : state.openSettings.delete(step.id)),
      onchange: () => (state.stepDrafts[step.id] = readSettings(prefix, names)),
    },
    h("summary", {}, "Параметры шага"),
    ...names.map((name) => f[name]()),
    h("p", { class: "hint" }, "Поменяй и нажми «Перезапустить шаг». Шаги после него сбросятся — «Запустить всё» соберёт их заново."),
  );
}

export function renderSettings(project) {
  return h(
    "details",
    { class: "panel disclosure disclosure--title", id: "edit-settings" },
    h("summary", {}, "Настройки выпуска"),
    settingsForm(project.settings, "edit"),
    h(
      "div",
      { class: "actions" },
      h("button", { type: "button", class: "btn btn--secondary", disabled: project.running, onclick: busyClick(() => act("settings", readSettings("edit"))) }, "Сохранить настройки"),
      h("span", { class: "hint" }, "Изменённые настройки сбросят только затронутые шаги."),
    ),
  );
}
