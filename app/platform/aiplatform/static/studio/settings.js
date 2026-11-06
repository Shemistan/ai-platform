// Студия: настройки выпуска — поля по одному, из них собраны форма «нового выпуска»,
// «Настройки выпуска» и «Параметры шага»; добавление канала Telegram.
import { api, busyClick, h } from "/ui.js";
import { fieldsOf as makeFields, readFields } from "./fields.js";
import { scenario, state } from "./state.js";
import { renderCreate } from "./create.js";
import { publishPick } from "./publish.js";
import { act, show } from "./app.js";

export const fieldsOf = (values, prefix) => makeFields(values, prefix, state.config);

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
      group("Публикация", channelAdder(prefix), f.post_format(), f.tempo(), f.channels(), f.auto_publish(), f.ad(), f.erid()),
      f.review_text(),
    );
  }
  return h(
    "div",
    { class: "settings-form" },
    f.draft(),
    group("Постер — первый кадр", f.poster_mode(), scenario === "digest" ? f.poster_deadline() : null),
    group("Картинки слайдов", f.draw_images(), f.images_mode(), f.image_model(), f.upscale_images()),
    group("Ролики", f.clips(), f.clips_mode(), f.video_model()),
    group("Озвучка", f.voice_mode(), f.engine()),
    group("Шортс", f.shorts()),
    group("Слайды", f.slide_s(), f.reading(), f.slide_motion()),
    group("Оформление", f.frame(), f.branding(), f.ai_badge(), f.disclaimers()),
    group("Публикация", channelAdder(prefix), f.tempo(), f.channels(), f.auto_publish(), f.ad(), f.erid()),
    scenario === "article" ? group("Источник — обязателен для публикации", f.source_url(), f.source_author(), f.source_license()) : null,
    scenario === "article" ? f.adapt_text() : null,
    f.review_text(),
  );
}

const SETTING_NAMES = [
  "poster_mode", "draw_images", "images_mode", "image_model", "upscale_images", "clips", "clips_mode", "video_model",
  "voice_mode", "engine", "shorts", "review_text", "disclaimers", "ai_badge", "branding", "tempo", "frame", "auto_publish", "channels",
  "post_format", "draft", "slide_s", "reading", "slide_motion", "ad", "erid",
]; // prettier-ignore

// Поля формы поста: остальные настройки у него не меняются.
const POST_SETTINGS = [
  "poster_mode", "image_model", "voice_mode", "engine", "review_text", "disclaimers", "ai_badge", "branding", "tempo", "frame",
  "auto_publish", "channels", "post_format", "ad", "erid",
]; // prettier-ignore

// Читает поля формы с этим префиксом; names — какие именно (engine читается вместе с голосом).
// У статьи ещё источник: ссылка и автор оригинала.
export const SOURCE_SETTINGS = ["source_url", "source_author", "source_license"];
export const AD_SETTINGS = ["ad", "erid"];
const ARTICLE_SETTINGS = [...SETTING_NAMES, ...SOURCE_SETTINGS, "adapt_text"];
const DIGEST_SETTINGS = [...SETTING_NAMES, "poster_deadline"]; // срок своей обложки (PRD-002)

export function readSettings(prefix, names = { post: POST_SETTINGS, article: ARTICLE_SETTINGS, digest: DIGEST_SETTINGS }[scenario] || SETTING_NAMES) {
  return readFields(prefix, names);
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
  render: ["draft", "frame", "tempo", "reading", "slide_motion", "branding", "disclaimers", "ai_badge"],
  short: ["shorts", "tempo", "reading", "slide_motion", "branding", "disclaimers", "ai_badge"],
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
