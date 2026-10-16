// Вкладка «Настройки»: какие модели фото и видео, движки озвучки и голоса включены; расписание, уведомления, уборка.
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

async function section(name, body) {
  try {
    render(await api("POST", `${API}/section/${name}`, body));
    showError("");
  } catch (err) {
    showError(err.message);
    load();
  }
}

function checkRow(label, hint, checked, onchange) {
  return h(
    "label",
    { class: "check" },
    h("input", { type: "checkbox", checked, onchange: (e) => onchange(e.target.checked) }),
    h("span", {}, label, hint ? h("span", { class: "hint" }, hint) : null),
  );
}

function renderSchedule(data) {
  const plan = data.schedule;
  const last = data.last_runs?.digest;
  fill(
    $("schedule"),
    h(
      "div",
      { class: "settings-stack" },
      checkRow("Собирать дайджест сам", "Выпуск создаётся и запускается в это время; дальше — как обычно: постер, проверка, публикация.", plan.enabled, (v) => section("schedule", { enabled: v })),
      h(
        "div",
        { class: "inline-fields" },
        h("label", { class: "field" }, h("span", { class: "hint" }, "Время"), h("input", { type: "time", value: plan.time, onchange: (e) => section("schedule", { time: e.target.value }) })),
        h(
          "label",
          { class: "field" },
          h("span", { class: "hint" }, "Новости"),
          h(
            "select",
            { onchange: (e) => section("schedule", { day: e.target.value }) },
            h("option", { value: "yesterday", selected: plan.day === "yesterday" }, "за вчера"),
            h("option", { value: "today", selected: plan.day === "today" }, "за сегодня"),
          ),
        ),
      ),
      last ? h("p", { class: "hint" }, `Последний запуск по расписанию: ${last}. Если платформа была выключена в это время, дайджест соберётся, как только она запустится, — в тот же день.`) : h("p", { class: "hint" }, "Если платформа была выключена в это время, дайджест соберётся, как только она запустится, — в тот же день."),
      checkRow("Уведомлять меня", "Когда выпуск ждёт тебя, упал или опубликован. На Mac — системное уведомление; своя команда — AIP_NOTIFY_CMD.", data.notify.enabled, (v) => section("notify", { enabled: v })),
      h("div", { class: "actions" }, h("button", { type: "button", class: "small-btn", onclick: () => api("POST", `${API}/notify-test`).then(render).catch((e) => showError(e.message)) }, "Проверить уведомление")),
      data.notifications.length
        ? h("details", {}, h("summary", { class: "link" }, `Последние уведомления (${data.notifications.length})`), h("ul", { class: "notes" }, ...data.notifications.map((n) => h("li", {}, h("span", { class: "muted small" }, n.at), ` ${n.title}: ${n.text}`))))
        : null,
    ),
  );
}

function render(data) {
  renderSchedule(data);
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
