// Вкладка «Каналы»: каналы публикации, бот движка, подписчики и приём подписок.
import { $, narratorApi as api, chip, fill, h, modal, shortDate } from "/ui.js";

const API = "/api/channels";
const FORMATS = { landscape: "Видео 16:9", vertical: "Шортс 9:16" };
const state = { data: null, subscribers: null, addFormat: "landscape" };

function segmented(box, value, onPick) {
  for (const b of box.querySelectorAll("button")) {
    b.setAttribute("aria-pressed", String(b.value === value));
    b.onclick = () => {
      for (const other of box.querySelectorAll("button")) other.setAttribute("aria-pressed", String(other === b));
      onPick(b.value);
    };
  }
}

// --- каналы ---

function renderChannels() {
  const data = state.data;
  const box = $("channels");
  if (data.error) {
    fill(box, h("p", { class: "job-error" }, `Каналы не читаются: ${data.error}`));
    return;
  }
  const rows = data.channels.map((c) =>
    h(
      "tr",
      {},
      h("td", {}, c.label, h("span", { class: "sub" }, c.type === "telegram" ? c.chat_id : "пакет для ручной загрузки")),
      h("td", {}, chip(FORMATS[c.format] || c.format, "info")),
      h("td", {}, c.ready ? chip("готов", "ok") : h("span", { class: "chip warn", title: c.problem }, c.problem.startsWith("нет токена") ? "нет токена" : "не настроен"), c.ready ? null : h("span", { class: "sub" }, c.problem)),
      h(
        "td",
        { class: "num" },
        h("span", { class: "toolbar", style: "justify-content:flex-end" }, h("button", { type: "button", class: "small-btn", onclick: () => editChannel(c) }, "Изменить"), h("button", { type: "button", class: "small-btn danger", onclick: () => removeChannel(c) }, "Убрать")),
      ),
    ),
  );
  fill(box, 
    data.channels.length
      ? h("div", { class: "table-wrap" }, h("table", { class: "data-table" }, h("thead", {}, h("tr", {}, h("th", {}, "Канал"), h("th", {}, "Что уходит"), h("th", {}, "Состояние"), h("th", {}, ""))), h("tbody", {}, ...rows)))
      : h("p", { class: "empty" }, "Каналов нет — добавь первый ниже."),
    h("p", { class: "hint", style: "margin-top:10px" }, data.custom ? `Список — ${data.file}.` : `Пока своего списка нет: показаны каналы движка (telegram_tems.md) и пакеты для ручной загрузки. Первая правка сохранит список в ${data.file}.`),
  );
  const tools = $("channel-tools");
  fill(tools, 
    data.importable.length
      ? h("button", { type: "button", class: "small-btn accent", title: data.importable.map((c) => c.label).join(", "), onclick: importNarrator }, `Взять каналы движка (${data.importable.length})`)
      : null,
  );
}

function editChannel(channel) {
  let format = channel.format;
  const label = h("input", { type: "text", value: channel.label });
  const token = h("input", { type: "text", value: channel.token_env || "", placeholder: "по умолчанию — бот движка" });
  const formats = h("div", { class: "segmented", role: "group" }, ...Object.entries(FORMATS).map(([v, t]) => h("button", { type: "button", value: v }, t)));
  segmented(formats, format, (v) => (format = v));
  const error = h("p", { class: "form-error", hidden: true });
  const save = async () => {
    try {
      state.data = { ...state.data, ...(await api("POST", `${API}/channels/${channel.id}`, { label: label.value, format, token_env: token.value })) };
      renderChannels();
      close();
    } catch (err) {
      error.textContent = err.message;
      error.hidden = false;
    }
  };
  const { close } = modal(
    channel.label,
    h("label", { class: "field" }, h("span", { class: "field-label" }, "Название"), label),
    h("div", { class: "field" }, h("span", { class: "field-label" }, "Что уходит"), formats),
    channel.type === "telegram" ? h("label", { class: "field" }, h("span", { class: "field-label" }, "Переменная с токеном бота"), token, h("span", { class: "hint" }, "Свой бот у канала — имя переменной окружения с его токеном. Сам токен в файлы не пишется.")) : null,
    error,
    h("div", { class: "toolbar" }, h("button", { type: "button", class: "primary", onclick: save }, "Сохранить")),
  );
}

async function removeChannel(channel) {
  const { close } = modal(
    "Убрать канал?",
    h("p", {}, `«${channel.label}» пропадёт из списка каналов публикации. Уже опубликованное не тронется.`),
    h(
      "div",
      { class: "toolbar" },
      h(
        "button",
        {
          type: "button",
          class: "primary",
          onclick: async () => {
            state.data = { ...state.data, ...(await api("POST", `${API}/channels/${channel.id}/delete`)) };
            renderChannels();
            close();
          },
        },
        "Убрать",
      ),
      h("button", { type: "button", class: "small-btn", onclick: () => close() }, "Отмена"),
    ),
  );
}

async function importNarrator() {
  await api("POST", `${API}/channels/import`);
  await load();
}

async function addChannel(event) {
  event.preventDefault();
  const error = $("add-error");
  error.hidden = true;
  try {
    const body = { label: $("add-label").value, chat_id: $("add-chat").value, format: state.addFormat, token_env: $("add-token").value };
    state.data = { ...state.data, ...(await api("POST", `${API}/channels`, body)) };
    for (const id of ["add-label", "add-chat", "add-token"]) $(id).value = "";
    renderChannels();
  } catch (err) {
    error.textContent = err.message;
    error.hidden = false;
  }
}

// --- бот и подписчики ---

function renderBot(extra = {}) {
  const bot = { ...state.data.bot, ...extra };
  const names = Object.entries(bot.channels || {});
  fill($("bot"), 
    h(
      "div",
      { class: "toolbar" },
      bot.token ? chip("токен найден", "ok") : chip("токена нет", "bad"),
      bot.username ? h("span", {}, `@${bot.username}`) : bot.token ? h("span", { class: "muted small" }, "«Проверить» спросит имя у Telegram") : null,
    ),
    bot.token_error ? h("p", { class: "hint warn" }, bot.token_error) : null,
    h("p", { class: "hint" }, "Бот движка шлёт озвучки статей и посты сценариев. Токен — в переменной NARRATOR_TELEGRAM_TOKEN или в bot_token.md движка; в браузер он не попадает."),
    names.length
      ? h("div", { class: "field" }, h("span", { class: "field-label" }, "Каналы движка (telegram_tems.md)"), h("ul", { class: "list" }, ...names.map(([n, id]) => h("li", { class: "toolbar" }, h("code", {}, n), h("span", { class: "muted small" }, id)))))
      : bot.channels_error
        ? h("p", { class: "hint warn" }, bot.channels_error)
        : null,
  );
}

function renderServe() {
  const s = state.data.serve;
  const toggle = h("input", { type: "checkbox", checked: s.running });
  toggle.addEventListener("change", async () => {
    toggle.disabled = true;
    state.data.serve = await api("POST", `${API}/serve/${toggle.checked ? "start" : "stop"}`);
    renderServe();
    schedulePoll(1500);
  });
  const since = s.running && s.started_at ? `с ${new Date(s.started_at * 1000).toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" })}` : "";
  fill($("serve"), 
    h(
      "div",
      { class: "card", style: "margin-bottom:12px" },
      h("label", { class: "switch" }, toggle, "Принимать подписки", s.running ? chip(`идёт ${since}`, "ok") : chip("выключено")),
      h("p", { class: "hint" }, "Пока включено, бот отвечает на /start и /stop. Подписчикам озвучки статей идут в личку, когда канал статей выключен (NARRATOR_TELEGRAM_CHANNEL=off)."),
      s.error ? h("p", { class: "hint warn" }, s.error) : null,
      s.log.length ? h("pre", { class: "log" }, s.log.slice(-8).join("\n")) : null,
    ),
  );
}

function renderSubscribers() {
  const data = state.subscribers;
  const box = $("subscribers");
  if (!data) return;
  const total = data.subscribers.length;
  fill(box, 
    h("div", { class: "stat-row" }, h("div", { class: "stat" }, h("b", {}, String(data.active)), h("span", {}, "подписаны")), h("div", { class: "stat" }, h("b", {}, String(total - data.active)), h("span", {}, "отписались"))),
    total
      ? h(
          "div",
          { class: "table-wrap", style: "margin-top:12px" },
          h(
            "table",
            { class: "data-table" },
            h("thead", {}, h("tr", {}, h("th", {}, "Кто"), h("th", {}, "С какого дня"), h("th", {}, ""))),
            h(
              "tbody",
              {},
              ...data.subscribers.map((s) =>
                h("tr", {}, h("td", {}, s.name || "без имени", h("span", { class: "sub" }, String(s.chat_id))), h("td", {}, shortDate(s.added)), h("td", {}, s.active ? chip("подписан", "ok") : chip(s.note ? `отписан: ${s.note}` : "отписан"))),
              ),
            ),
          ),
        )
      : h("p", { class: "empty" }, "Пока никто не подписался."),
    data.error ? h("p", { class: "hint warn" }, data.error) : null,
  );
}

function renderUnsent() {
  const n = state.data.unsent;
  $("unsent-box").hidden = !n;
  if (!n) return;
  fill($("unsent"), h("p", {}, `Озвучек статей, что ещё не ушли в канал: ${n}.`), h("a", { class: "link", href: "/library/?filter=unsent" }, "Открыть в «Статьях» →"));
}

async function loadSubscribers(sync = false) {
  $("subs-sync").disabled = true;
  try {
    state.subscribers = await api("GET", `${API}/subscribers${sync ? "?sync=1" : ""}`);
    renderSubscribers();
  } catch (err) {
    fill($("subscribers"), h("p", { class: "job-error" }, err.message));
  } finally {
    $("subs-sync").disabled = false;
  }
}

async function load() {
  state.data = await api("GET", `${API}/state`);
  renderChannels();
  renderBot();
  renderServe();
  renderUnsent();
}

let pollTimer = null;

function schedulePoll(ms) {
  clearTimeout(pollTimer);
  pollTimer = setTimeout(pollServe, ms);
}

// Пока бот принимает подписки — его лог и список подписчиков свежие.
async function pollServe() {
  try {
    const wasRunning = state.data.serve.running;
    state.data.serve = await api("GET", `${API}/serve`);
    renderServe();
    if (state.data.serve.running || wasRunning) loadSubscribers();
  } catch {
    /* сервер мог перезапуститься */
  }
  schedulePoll(state.data.serve.running ? 3000 : 15000);
}

async function init() {
  segmented($("add-format"), "landscape", (v) => (state.addFormat = v));
  $("add-form").addEventListener("submit", addChannel);
  $("bot-check").addEventListener("click", async () => {
    $("bot-check").disabled = true;
    try {
      renderBot(await api("POST", `${API}/bot/check`));
    } catch (err) {
      renderBot({ token_error: err.message });
    } finally {
      $("bot-check").disabled = false;
    }
  });
  $("subs-sync").addEventListener("click", () => loadSubscribers(true));
  await load();
  loadSubscribers();
  schedulePoll(3000);
}

init().catch((err) => fill($("channels"), h("p", { class: "job-error" }, `Не удалось загрузить: ${err.message}`)));
