// Вкладка «Настройки»: какие модели фото и видео, движки озвучки и голоса включены.
import { $, api, chip, fill, h } from "/ui.js";

const API = "/api/settings";

function showError(message) {
  const box = $("error");
  box.textContent = message || "";
  box.hidden = !message;
}

async function toggle(body) {
  try {
    render(await api("POST", `${API}/toggle`, body));
    showError("");
  } catch (err) {
    showError(err.message);
    load();
  }
}

function installedChip(item) {
  if (item.installed === true) return chip("установлена", "ok");
  if (item.installed === false) return chip("не установлена", "warn");
  return null;
}

// Строка каталога: флажок «включено», название, пометки и под ними — extra (голоса движка).
function row(item, kind, note, extra = null) {
  const box = h("input", {
    type: "checkbox",
    checked: item.enabled,
    onchange: (e) => toggle({ kind, id: item.id, enabled: e.target.checked }),
  });
  return h(
    "div",
    { class: `toggle-row${item.enabled ? "" : " off"}` },
    h("label", {}, box),
    h(
      "div",
      { class: "toggle-body" },
      h(
        "label",
        { class: "toggle-title", onclick: () => box.click() },
        h("span", { class: "toggle-name" }, item.label),
        installedChip(item),
        item.enabled ? null : chip("выключено"),
      ),
      note ? h("span", { class: "hint" }, note) : null,
      extra,
    ),
  );
}

function voiceButtons(engine) {
  return h(
    "div",
    { class: "voices", role: "group", "aria-label": `Голоса ${engine.label}` },
    ...engine.voices.map((v) =>
      h(
        "button",
        {
          type: "button",
          "aria-pressed": String(v.enabled),
          disabled: !engine.enabled,
          title: v.id === engine.default_voice ? "голос по умолчанию" : null,
          onclick: () => toggle({ kind: "voice", engine: engine.id, id: v.id, enabled: !v.enabled }),
        },
        v.id === engine.default_voice ? `${v.id} ★` : v.id,
      ),
    ),
  );
}

function render(data) {
  const { photo, video, audio } = data.sections;
  fill($("photo"), h("div", { class: "toggle-list" }, ...photo.map((m) => row(m, "photo", m.note))));
  fill($("video"), h("div", { class: "toggle-list" }, ...video.map((m) => row(m, "video", m.note))));
  const engines = audio.map((e) => {
    const on = e.voices.filter((v) => v.enabled).length;
    return row(e, "audio", `${e.note} · голосов включено: ${on} из ${e.voices.length}`, voiceButtons(e));
  });
  fill($("audio"), h("div", { class: "toggle-list" }, ...engines));
  $("file").textContent = `Настройки хранятся в ${data.file}.`;
}

async function load() {
  try {
    render(await api("GET", `${API}/state`));
  } catch (err) {
    showError(`Настройки не загрузились: ${err.message}`);
  }
}

load();
