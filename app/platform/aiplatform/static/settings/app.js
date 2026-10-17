// Вкладка «Настройки»: какие модели фото и видео, движки озвучки и голоса включены; расписание, уведомления, уборка.
import { $, api, chip, fill, h } from "/ui.js";

const API = "/api/settings";

function showError(message) {
  const box = $("error");
  box.textContent = message || "";
  box.hidden = !message;
}

async function toggle(body) {
  // Каталог перерисовывается целиком: фокус возвращаем на тот же флажок или кнопку голоса.
  const focused = document.activeElement?.id;
  try {
    render(await api("POST", `${API}/toggle`, body));
    showError("");
    if (focused) $(focused)?.focus();
  } catch (err) {
    showError(err.message);
    load();
    loadCleanup();
  }
}

function installedChip(item) {
  if (item.installed === true) return chip("установлена", "ok");
  if (item.installed === false) return chip("не установлена", "warn");
  return null;
}

// Строка каталога: флажок «включено», название, пометки и под ними — extra (голоса движка).
// Подпись связана с флажком через for/id, пояснение — через aria-describedby: имя флажка = название.
function row(item, kind, note, extra = null) {
  const id = `t-${kind}-${String(item.id).replace(/[^\w-]/g, "_")}`;
  const box = h("input", {
    type: "checkbox",
    id,
    checked: item.enabled,
    "aria-describedby": note ? `${id}-note` : null,
    onchange: (e) => toggle({ kind, id: item.id, enabled: e.target.checked }),
  });
  return h(
    "div",
    { class: `toggle-row${item.enabled ? "" : " off"}` },
    box,
    h(
      "div",
      { class: "toggle-body" },
      h(
        "div",
        { class: "toggle-title" },
        h("label", { class: "toggle-name", for: id }, item.label),
        installedChip(item),
        item.enabled ? null : chip("выключено"),
      ),
      note ? h("span", { class: "hint", id: `${id}-note` }, note) : null,
      extra,
    ),
  );
}

function voiceButtons(engine) {
  return h(
    "div",
    { class: "segmented voices", role: "group", "aria-label": `Голоса ${engine.label}` },
    ...engine.voices.map((v) =>
      h(
        "button",
        {
          type: "button",
          id: `v-${engine.id}-${v.id}`.replace(/[^\w-]/g, "_"),
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
    loadCleanup();
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
  // schedule.json: {day, result, error, at, attempts}; старая запись — строка-дата.
  const run = data.last_runs?.digest;
  const last = typeof run === "string" ? run : run?.result === "failed" ? `${run.day} — не удался (${run.error}), попыток ${run.attempts}` : run?.day;
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
      checkRow("Уведомлять меня", "Когда выпуск ждёт тебя, упал или опубликован. В Telegram — если указан чат ниже; на Mac — системное уведомление; своя команда — AIP_NOTIFY_CMD.", data.notify.enabled, (v) => section("notify", { enabled: v })),
      h(
        "label",
        { class: "field" },
        h("span", { class: "hint" }, "Мой чат в Telegram (id или @username) — бот каналов пишет туда; сначала напиши боту /start"),
        h("input", { type: "text", value: data.notify.telegram_chat || "", placeholder: "123456789", onchange: (e) => section("notify", { telegram_chat: e.target.value.trim() }) }),
      ),
      h("div", { class: "actions" }, h("button", { type: "button", class: "btn btn--ghost btn--sm", onclick: () => api("POST", `${API}/notify-test`).then(render).catch((e) => showError(e.message)) }, "Проверить уведомление")),
      data.notifications.length
        ? h("details", {}, h("summary", { class: "btn btn--link" }, `Последние уведомления (${data.notifications.length})`), h("ul", { class: "notes" }, ...data.notifications.map((n) => h("li", {}, h("span", { class: "muted small" }, n.at), ` ${n.title}: ${n.text}`))))
        : null,
    ),
  );
}

const mb = (bytes) => (bytes >= 2 ** 30 ? `${(bytes / 2 ** 30).toFixed(1)} ГБ` : `${Math.round(bytes / 2 ** 20)} МБ`);
const cleanupState = { data: null, confirm: false, busy: false };

async function loadCleanup() {
  try {
    cleanupState.data = await api("GET", `${API}/cleanup`);
  } catch (err) {
    showError(`Уборка: ${err.message}`);
  }
  if (state.data) renderUpkeep(state.data);
}

async function runCleanup() {
  if (!cleanupState.confirm) {
    cleanupState.confirm = true;
    return renderUpkeep(state.data);
  }
  cleanupState.busy = true;
  renderUpkeep(state.data);
  try {
    cleanupState.data = await api("POST", `${API}/cleanup/run`);
    cleanupState.result = cleanupState.data.found;
    cleanupState.data = await api("GET", `${API}/cleanup`);
  } catch (err) {
    showError(`Уборка: ${err.message}`);
  }
  Object.assign(cleanupState, { confirm: false, busy: false });
  renderUpkeep(state.data);
}

function renderUpkeep(data) {
  const c = cleanupState.data;
  const found = c?.found;
  const what = found ? [found.jobs && `задач вкладок: ${found.jobs}`, found.cache && `записей кэша: ${found.cache}`, found.archived && `выпусков из архива: ${found.archived}`].filter(Boolean) : [];
  const done = cleanupState.result;
  fill(
    $("upkeep"),
    h(
      "div",
      { class: "settings-stack" },
      checkRow("Обновлять отклик каналов раз в сутки", "Подписчики и просмотры публичных каналов Telegram — на доске «Выпуски», в «Что работает».", data.stats.auto, (v) => section("stats", { auto: v })),
      c
        ? h(
            "table",
            { class: "usage" },
            h("tbody", {}, ...c.usage.map((u) => h("tr", {}, h("th", {}, u.label), h("td", {}, mb(u.bytes))))),
          )
        : h("p", { class: "muted" }, "Считаю место…"),
      h(
        "div",
        { class: "inline-fields" },
        h(
          "label",
          { class: "field" },
          h("span", { class: "hint" }, "Удалять старше"),
          h("select", { onchange: (e) => section("cleanup", { days: Number(e.target.value) }).then(loadCleanup) }, ...data.cleanup.choices.map((d) => h("option", { value: d, selected: d === data.cleanup.days }, `${d} дней`))),
        ),
      ),
      checkRow("Убирать раз в сутки сам", null, data.cleanup.auto, (v) => section("cleanup", { auto: v })),
      h("p", { class: "hint" }, "Удаляются только завершённые задачи вкладок «Фото», «Видео», «Аудио» (выпуски уже скопировали файлы к себе), давно не нужные записи кэша и выпуски из архива. Неархивные выпуски и идущие задачи не трогаются."),
      found ? h("p", {}, what.length ? `Сейчас под уборку: ${what.join(", ")} — ${mb(found.bytes)}.` : "Сейчас убирать нечего.") : null,
      h(
        "div",
        { class: "actions" },
        h("button", { type: "button", class: cleanupState.confirm ? "btn btn--danger btn--sm" : "btn btn--ghost btn--sm", disabled: cleanupState.busy || !what.length, onclick: runCleanup }, cleanupState.busy ? "Убираю…" : cleanupState.confirm ? `Да, удалить ${found ? mb(found.bytes) : ""}` : "Убрать сейчас"),
        cleanupState.confirm ? h("button", { type: "button", class: "btn btn--link", onclick: () => ((cleanupState.confirm = false), renderUpkeep(state.data)) }, "Отмена") : null,
        done ? h("span", { class: "hint" }, `Освобождено ${mb(done.bytes)}.`) : null,
      ),
    ),
  );
}

const state = { data: null };

function render(data) {
  state.data = data;
  renderSchedule(data);
  renderUpkeep(data);
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
loadCleanup();
