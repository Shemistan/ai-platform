// Общие помощники страниц платформы: запросы к API и построение DOM без сборки и зависимостей.

export const $ = (id) => document.getElementById(id);

export const API_TIMEOUT_MS = 30000;

// Ошибка запроса к API. kind: network — сервис не отвечает, timeout — не уложился в срок,
// http — ответ с кодом ошибки, abort — запрос отменили сами (signal). status и data — у http;
// code — машинный код ошибки сервера (docs/api.md), у http; иначе null.
export class ApiError extends Error {
  constructor(message, { kind, status = 0, data = {}, cause } = {}) {
    super(message, cause ? { cause } : undefined);
    this.name = "ApiError";
    this.kind = kind;
    this.status = status;
    this.data = data;
    this.code = typeof data?.code === "string" ? data.code : null;
  }
}

// Текст ответа-ошибки без JSON: код и первая непустая строка (HTML-теги срезаны).
function httpMessage(status, text) {
  if (status === 413) return "Файл больше лимита.";
  const line = String(text || "")
    .replace(/<[^>]*>/g, " ")
    .split("\n")
    .map((s) => s.trim())
    .find(Boolean);
  return line ? `Ошибка ${status}: ${line.slice(0, 200)}` : `Ошибка ${status}`;
}

// api(метод, путь, тело, { signal, timeout }) → JSON ответа или ApiError.
// timeout в мс: по умолчанию 30 с, у загрузок (тело — Blob/File) — без срока; 0 — без срока.
export async function api(method, path, body, { signal, timeout } = {}) {
  // X-AIP: 1 — признак запроса своей страницы: без него и без Origin сервер POST отклонит (SEC-002).
  const init = { method, headers: { "X-AIP": "1" } };
  if (body instanceof Blob) {
    init.body = body;
  } else if (body !== undefined) {
    init.body = JSON.stringify(body);
    init.headers["Content-Type"] = "application/json";
  }
  const limit = timeout ?? (body instanceof Blob ? 0 : API_TIMEOUT_MS);
  const ctrl = new AbortController();
  let timedOut = false;
  const timer = limit ? setTimeout(() => ((timedOut = true), ctrl.abort()), limit) : null;
  const relay = () => ctrl.abort();
  if (signal) {
    if (signal.aborted) ctrl.abort();
    else signal.addEventListener("abort", relay, { once: true });
  }
  init.signal = ctrl.signal;
  let res;
  let text;
  try {
    res = await fetch(path, init);
    text = await res.text();
  } catch (err) {
    if (timedOut) throw new ApiError(`Сервис не ответил за ${Math.round(limit / 1000)} с.`, { kind: "timeout", cause: err });
    if (signal?.aborted) throw new ApiError("Запрос отменён.", { kind: "abort", cause: err });
    throw new ApiError("Сервис не отвечает. Проверь, что он запущен.", { kind: "network", cause: err });
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener("abort", relay);
  }
  let data = null;
  try {
    data = text ? JSON.parse(text) : {};
  } catch {
    data = null;
  }
  if (!res.ok) {
    let message = data?.error || httpMessage(res.status, data ? "" : text);
    // 500 от сервера несёт request_id — по нему ошибку находят в логе.
    if (data?.request_id) message = `${message.replace(/\.$/, "")}. Код для поиска в логе: ${data.request_id}`;
    throw new ApiError(message, { kind: "http", status: res.status, data: data || {} });
  }
  return data || {};
}

// Ответы, за которыми сервер ходит в движок narrator (bridge — до 120 с, скачивание статьи — до 180 с):
// вкладки «Статьи», «Новости», «Каналы», «Аудио» зовут API с этим сроком вместо 30 с.
export const NARRATOR_TIMEOUT_MS = 190000;
export const narratorApi = (method, path, body, opts = {}) => api(method, path, body, { timeout: NARRATOR_TIMEOUT_MS, ...opts });

// Атрибуты-ссылки: сюда приходят адреса из лент, статей и реестра, то есть чужие данные.
const URL_ATTRS = new Set(["href", "src", "action", "formaction", "poster", "xlink:href"]);
const MEDIA_ATTRS = new Set(["src", "poster"]);

// Ссылка безопасна: http(s), относительная, blob:, а в src/poster ещё data:image|audio|video.
// javascript:, vbscript:, data:text/html и прочее — нет: атрибут не ставим (SEC-003).
export function safeUrl(key, value) {
  let url;
  try {
    url = new URL(String(value), location.href); // разбор как в браузере: пробелы, регистр, \t в схеме
  } catch {
    return false;
  }
  if (url.protocol === "http:" || url.protocol === "https:" || url.protocol === "blob:") return true;
  return url.protocol === "data:" && MEDIA_ATTRS.has(key) && /^data:(image|audio|video)\//i.test(url.href);
}

// h("p", { class: "x", onclick: fn }, "текст", child, null) — null и false пропускаются.
export function h(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (URL_ATTRS.has(key.toLowerCase()) && !safeUrl(key.toLowerCase(), value)) continue;
    if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (key === "value") node.value = value;
    else if (value === true) node.setAttribute(key, "");
    else node.setAttribute(key, value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

export const STATUS = {
  queued: "в очереди",
  running: "идёт",
  done: "готово",
  failed: "ошибка",
  canceled: "отменено",
  waiting: "ждёт вас",
  pending: "не начат",
  skipped: "пропущен",
};

// Тон статуса задачи — один словарь цветов на все вкладки (классы .tone-* в ui.css).
export const STATUS_TONE = {
  queued: "info",
  running: "info",
  done: "success",
  failed: "danger",
  canceled: "neutral",
  waiting: "warning",
  pending: "neutral",
  skipped: "neutral",
};

export function toneOf(status) {
  return STATUS_TONE[status] || "neutral";
}

export function statusChip(status) {
  return chip(STATUS[status] || status, toneOf(status));
}

export function progressBar(progress) {
  const percent = Math.round(progress?.percent || 0);
  return h(
    "div",
    { class: "progress" },
    h("div", { class: "progress-head" }, h("span", { class: "stage" }, progress?.stage || ""), h("span", { class: "percent" }, `${percent}%`)),
    h("div", { class: "bar" }, h("div", { class: "bar-fill", style: `width:${percent}%` })),
  );
}

// Прогресс задачи генерации (Фото, Видео): в очереди — полосатая полоса и место в очереди,
// дальше — этап, шаг, процент, сколько прошло и «Отменить», если передан onCancel.
export function jobProgress(job, { onCancel, elapsed = seconds } = {}) {
  const p = job.progress || {};
  const percent = Math.round(p.percent || 0);
  const queued = job.status === "queued";
  const stage = queued
    ? job.queue_position > 0 ? `Ждёт очереди: впереди ${job.queue_position}` : "Сейчас начнётся"
    : p.stage || "";
  return h(
    "div",
    { class: "progress" },
    h(
      "div",
      { class: "progress-head" },
      h("span", { class: "stage" }, stage, p.steps ? h("span", { class: "muted" }, ` · шаг ${p.step} из ${p.steps}`) : null),
      h("span", { class: "percent" }, queued ? "" : `${percent}%`),
    ),
    h(
      "div",
      { class: `bar${queued ? " waiting" : ""}`, role: "progressbar", "aria-valuemin": "0", "aria-valuemax": "100", "aria-valuenow": String(percent) },
      h("div", { class: "bar-fill", style: `width:${queued ? 0 : p.percent || 0}%` }),
    ),
    h(
      "div",
      { class: "progress-foot" },
      h("span", { class: "muted small" }, queued || job.elapsed_s == null ? "" : `Прошло ${elapsed(job.elapsed_s)}`),
      onCancel ? h("button", { type: "button", class: "btn btn--link btn--danger", onclick: busyClick(() => onCancel(job), `cancel:${job.id}`) }, "Отменить") : null,
    ),
  );
}

export function logBlock(lines, title = "Лог") {
  if (!lines || !lines.length) return null;
  return h("details", { class: "log-wrap" }, h("summary", {}, `${title} (${lines.length})`), h("pre", { class: "log" }, lines.join("\n")));
}

export function seconds(value) {
  if (value === null || value === undefined) return "";
  const s = Math.round(value);
  return s >= 60 ? `${Math.floor(s / 60)} мин ${s % 60} с` : `${s} с`;
}

// --- Общие компоненты вкладок движка ---

// fill(узел, ...дети) — как replaceChildren, но null и false пропускаются, как в h().
export function fill(node, ...children) {
  node.replaceChildren(...children.flat().filter((c) => c !== null && c !== undefined && c !== false));
  return node;
}

// Старые имена тонов у chip(): ok/warn/bad — те же success/warning/danger.
const TONE_ALIAS = { ok: "success", warn: "warning", bad: "danger" };

// chip("готово", "success") — метка состояния: neutral (по умолчанию), info, success, warning, danger.
export function chip(text, tone = "") {
  return h("span", { class: `chip tone-${TONE_ALIAS[tone] || tone || "neutral"}` }, text);
}

export function debounce(fn, ms) {
  let timer = null;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), ms);
  };
}

// «12 окт.», с годом — если не этот.
export function shortDate(value) {
  if (!value) return "";
  const d = new Date(value.length <= 10 ? `${value}T12:00:00` : value);
  if (Number.isNaN(d.getTime())) return value;
  const opts = { day: "numeric", month: "short" };
  if (d.getFullYear() !== new Date().getFullYear()) opts.year = "numeric";
  return d.toLocaleDateString("ru-RU", opts);
}

export function minutes(value) {
  if (!value) return "";
  const m = Math.floor(value / 60);
  const s = Math.round(value % 60);
  return m ? `${m}:${String(s).padStart(2, "0")}` : `${s} с`;
}

export function copyButton(getText, label = "Скопировать") {
  const btn = h("button", { type: "button", class: "btn btn--ghost btn--sm" }, label);
  btn.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(typeof getText === "function" ? getText() : getText);
      btn.textContent = "Скопировано";
      toast("Скопировано");
    } catch {
      btn.textContent = "Не вышло";
      toast("Не удалось скопировать: браузер не дал доступ к буферу обмена", "danger");
    }
    setTimeout(() => (btn.textContent = label), 1500);
  });
  return btn;
}

// Модальное окно на <dialog>: modal("Заголовок", ...содержимое) → { dialog, body, close }.
export function modal(title, ...children) {
  const body = h("div", { class: "modal-body" }, ...children);
  const close = h("button", { type: "button", class: "icon-btn", "aria-label": "Закрыть" }, "×");
  const dialog = h("dialog", { class: "modal" }, h("div", { class: "modal-head" }, h("h2", {}, title), close), body);
  close.addEventListener("click", () => dialog.close());
  dialog.addEventListener("close", () => dialog.remove());
  dialog.addEventListener("click", (e) => e.target === dialog && dialog.close());
  document.body.append(dialog);
  dialog.showModal();
  return { dialog, body, close: () => dialog.close() };
}

// Подтверждение необратимого действия: await confirmAction({ title, body, confirmLabel, danger }) → true/false.
// «Отмена», крестик, Esc и клик мимо окна — false. body — узел, строка или массив узлов.
export function confirmAction({ title, body = null, confirmLabel = "Да", cancelLabel = "Отмена", danger = false }) {
  return new Promise((resolve) => {
    let answer = false;
    const yes = h("button", { type: "button", class: danger ? "btn btn--primary btn--danger" : "btn btn--primary" }, confirmLabel);
    const no = h("button", { type: "button", class: "btn btn--ghost btn--sm" }, cancelLabel);
    const { dialog, close } = modal(title, ...[].concat(body), h("div", { class: "toolbar confirm-actions" }, yes, no));
    yes.addEventListener("click", () => ((answer = true), close()));
    no.addEventListener("click", () => close());
    dialog.addEventListener("close", () => resolve(answer));
    no.focus(); // по Enter ничего необратимого не случится
  });
}

const VERDICT_TONES = { готово: "success", "готово с оговорками": "warning", "не готово": "danger" };

// Подтверждение публикации (доска и шаг «Публикация»): каналы по названиям, начало подписи, вердикт судьи.
// channels: [{ label, type }], caption — текст поста, review — отчёт судьи (необязательно).
export function confirmPublish({ channels, caption = "", verdict = null, review = null, note = null }) {
  const names = channels.map((c) => c.label).join(", ");
  const text = caption.trim();
  const head = text.split("\n").filter((line) => line.trim()).slice(0, 3).join("\n");
  const where = channels.every((c) => c.type === "telegram") ? "Опубликовать в Telegram" : "Опубликовать";
  return confirmAction({
    title: "Опубликовать выпуск?",
    body: [
      h("div", {}, h("p", { class: "field-label" }, "Куда"), h("ul", { class: "confirm-list" }, ...channels.map((c) => h("li", {}, c.label)))),
      h(
        "div",
        {},
        h("p", { class: "field-label" }, "Подпись"),
        head ? h("p", { class: "preview-text" }, head.length < text.length ? `${head}…` : head) : h("p", { class: "muted" }, "Подписи нет."),
      ),
      h(
        "div",
        {},
        h("span", { class: "field-label" }, "Судья: "),
        verdict ? chip(verdict, VERDICT_TONES[verdict] || "") : h("span", { class: "muted" }, "вердикта нет"),
        review && verdict !== "готово" ? h("details", { class: "inline" }, h("summary", {}, "Отчёт судьи"), h("pre", { class: "log" }, review)) : null,
      ),
      note ? h("p", { class: "hint warn" }, note) : null,
      h("p", { class: "hint" }, "Отменить отправленный пост отсюда нельзя."),
    ],
    confirmLabel: `${where}: ${names}`,
    danger: true,
  });
}

// Одна на страницу область объявлений для скринридера (#sr-status ставит nav.js):
// панели перерисовываются часто, поэтому вслух — только переходы, которые объявили явно.
export function announce(text) {
  if (!text) return;
  let box = document.getElementById("sr-status");
  if (!box) {
    box = h("div", { id: "sr-status", class: "sr-only", role: "status", "aria-live": "polite" });
    document.body.append(box);
  }
  // Сначала очистить: тот же текст подряд иначе не зачитается.
  box.textContent = "";
  setTimeout(() => (box.textContent = text), 50);
}

export const TOAST_MS = 4000;

// Подтверждение фонового действия («Сохранено», «Скопировано», «Запущено»): тост в правом нижнем углу.
// tone — success | info | warning | danger. Ошибка (danger) висит до закрытия, остальные гаснут через TOAST_MS.
// Скринридеру — через общую область announce(); сам стек не живая область, чтобы не читать дважды.
export function toast(text, tone = "success") {
  if (!text) return null;
  let stack = document.getElementById("toasts");
  if (!stack) {
    stack = h("div", { id: "toasts", class: "toasts" });
    document.body.append(stack);
  }
  const close = h("button", { type: "button", class: "icon-btn", "aria-label": "Скрыть" }, "×");
  const item = h("div", { class: `toast tone-${tone}` }, h("span", {}, text), close);
  const remove = () => item.remove();
  close.addEventListener("click", remove);
  stack.append(item);
  announce(text);
  if (tone !== "danger") setTimeout(remove, TOAST_MS);
  return item;
}

// Карточка фоновой задачи вкладки: статус, этап, сколько идёт, последняя строка или лог.
export function taskCard(task, { onCancel, full = false } = {}) {
  if (!task) return null;
  const active = task.status === "queued" || task.status === "running";
  const where = task.queue_position ? `перед ней ${task.queue_position}` : task.stage;
  const meta = [active ? where : "", task.elapsed_s ? seconds(task.elapsed_s) : ""].filter(Boolean).join(" · ");
  return h(
    "div",
    { class: "task" },
    h(
      "div",
      { class: "task-head" },
      statusChip(task.status),
      h("span", { class: "task-title" }, task.title),
      h("span", { class: "muted small" }, meta),
      active && onCancel ? h("button", { type: "button", class: "btn btn--link btn--danger", onclick: busyClick(() => onCancel(task), `cancel:${task.id}`) }, "Отменить") : null,
    ),
    task.error ? h("p", { class: "job-error" }, task.error) : null,
    full ? logBlock(task.log) : task.last && active ? h("div", { class: "task-last" }, task.last) : null,
  );
}

// --- Понятные ошибки ---

// Известные ошибки → человеческий текст: reason — что случилось, help — что делать.
// engine — поломка движка narrator: она одна на все вкладки и подробно описана в баннере шапки,
// поэтому на странице — коротко и со ссылкой на «Настройки». Формулировки уточняет TXT-003.
const ENGINE_TEXT = /invalid choice|unrecognized arguments|usage: narrate|\bbridge\b|движок (упал|не ответил)|движок narrator|команда «[^»]+» не найдена|нужен движок/i;
const KNOWN_ERRORS = [
  {
    test: (err) => err?.kind === "network",
    reason: "Сервис платформы не отвечает.",
    help: "Проверь, что он запущен (make run), и нажми «Повторить».",
  },
  {
    test: (err) => err?.kind === "timeout",
    reason: "Сервис не ответил вовремя.",
    help: "Возможно, он занят тяжёлой задачей — повтори через минуту.",
  },
  {
    test: (err, text) => err?.data?.kind === "unavailable" || ENGINE_TEXT.test(text),
    engine: true,
    reason: "Движок narrator недоступен или устарел.",
    help: "Что именно не так — в баннере вверху страницы; путь к движку — в «Настройках».",
  },
  {
    test: (_err, text) => /unauthorized|invalid token|bot token|токен (не принят|неверный)|\b401\b/i.test(text),
    reason: "Telegram не принял токен бота.",
    help: "Проверь NARRATOR_TELEGRAM_TOKEN или bot_token.md движка.",
  },
  {
    test: (_err, text) => /network is unreachable|getaddrinfo|nodename nor servname|name or service not known|temporary failure in name resolution|connection refused|urlopen error/i.test(text),
    reason: "Нет связи с внешним сервисом.",
    help: "Проверь интернет и нажми «Повторить».",
  },
];

// explainError(ошибка или текст) → { reason, help, details, engine }: details — сырой текст для
// «Подробностей», если он не совпадает с reason. Неизвестная короткая однострочная ошибка сервера
// уже написана для человека — показываем как есть; длинная или многострочная — общий текст.
export function explainError(error) {
  const text = String((typeof error === "string" ? error : error?.message) || "");
  const known = KNOWN_ERRORS.find((k) => k.test(typeof error === "string" ? null : error, text));
  const requestId = typeof error === "object" ? error?.data?.request_id : null;
  if (known) return { reason: known.reason, help: known.help, details: text || null, engine: Boolean(known.engine) };
  if (requestId) return { reason: "Внутренняя ошибка сервиса.", help: `Код для поиска в логе: ${requestId}.`, details: text, engine: false };
  if (text && text.length <= 200 && !text.includes("\n")) return { reason: text, help: null, details: null, engine: false };
  return { reason: "Что-то пошло не так.", help: "Нажми «Повторить»; если не поможет — пришли «Подробности».", details: text || null, engine: false };
}

// Блок ошибки загрузки: заголовок, одна фраза причины, «что делать», «Повторить» и сырой вывод
// под «Подробностями». errorState({ title, error, onRetry }) — reason/help/details можно задать сами.
export function errorState({ title = "Не удалось загрузить", error = null, reason, help, details, onRetry = null, note = null } = {}) {
  const auto = explainError(error);
  const why = reason ?? auto.reason;
  const what = help ?? auto.help;
  const raw = details ?? auto.details;
  return h(
    "div",
    { class: "error-state", role: "alert" },
    h("strong", { class: "error-title" }, title),
    note ? h("p", { class: "hint" }, note) : null,
    why ? h("p", {}, why) : null,
    what || auto.engine ? h("p", { class: "hint" }, what || "", auto.engine ? [" ", h("a", { class: "btn btn--link", href: "/settings/" }, "Открыть «Настройки»")] : null) : null,
    onRetry ? h("div", { class: "toolbar" }, h("button", { type: "button", class: "btn btn--ghost btn--sm", onclick: busyClick(onRetry) }, "Повторить")) : null,
    raw && raw !== why ? h("details", { class: "inline" }, h("summary", {}, "Подробности"), h("pre", { class: "log" }, raw)) : null,
  );
}

// Последний удачный ответ в браузере: при поломке страница показывает его с пометкой «данные от …».
export function saveLast(key, data) {
  try {
    localStorage.setItem(`aip:last:${key}`, JSON.stringify({ at: Date.now(), data }));
  } catch {
    /* переполнено или запрещено — просто без запаса */
  }
}

export function loadLast(key) {
  try {
    const raw = localStorage.getItem(`aip:last:${key}`);
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
}

// «12 окт., 14:05» — когда сохранили запас.
export function stamp(ms) {
  return new Date(ms).toLocaleString("ru-RU", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}

// --- Ошибки обработчиков ---

let toastTimer = null;

// Ошибка без своего места на странице (обработчик клика, фоновое обновление) — плашка внизу экрана.
export function reportError(err) {
  if (err?.kind === "abort") return;
  const message = err?.message || String(err);
  console.warn(err);
  let box = document.getElementById("platform-toast");
  if (!box) {
    box = h("div", { id: "platform-toast", class: "platform-toast", role: "alert" });
    document.body.append(box);
  }
  const close = h("button", { type: "button", class: "icon-btn", "aria-label": "Скрыть" }, "×");
  close.addEventListener("click", () => (box.hidden = true));
  fill(box, h("span", {}, message), close);
  box.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (box.hidden = true), 8000);
}

// safe(fn) — обёртка обработчика события: ошибка (и отклонённый промис) уходит в reportError,
// а не в «Uncaught (in promise)».
export function safe(fn) {
  return (...args) => {
    try {
      return Promise.resolve(fn(...args)).catch(reportError);
    } catch (err) {
      reportError(err);
      return Promise.resolve();
    }
  };
}

// --- Кнопки на время запроса ---

const inflight = new Map();

// busy(кнопка, () => промис, ключ) — кнопка неактивна (aria-busy, aria-disabled, стиль в ui.css
// гасит её и клики) до ответа. disabled не трогаем: его ставит перерисовка по состоянию, и снятие
// в finally не должно его перебить. Повторный вызов, пока первый не кончился, нового запроса не шлёт:
// с тем же ключом (или той же кнопкой) возвращает промис первого. Ключ нужен, когда перерисовка
// подменяет кнопку новой.
export function busy(btn, work, key = null) {
  const id = key ?? btn;
  if (id && inflight.has(id)) return inflight.get(id);
  const target = typeof btn?.setAttribute === "function" ? btn : null;
  target?.setAttribute("aria-busy", "true");
  target?.setAttribute("aria-disabled", "true");
  const run = (async () => {
    try {
      return await work();
    } finally {
      if (id) inflight.delete(id);
      target?.removeAttribute("aria-busy");
      target?.removeAttribute("aria-disabled");
    }
  })();
  if (id) inflight.set(id, run);
  return run;
}

// Идёт ли запрос с этим ключом: перерисовка рисует кнопку неактивной, пока ответа нет.
export const isBusy = (key) => inflight.has(key);

// Обработчик клика для кнопки, шлющей запрос: busy() по нажатой кнопке (и ключу) плюс safe().
// key — строка или функция от события.
export function busyClick(fn, key = null) {
  return (event) => {
    const btn = typeof event?.currentTarget?.setAttribute === "function" ? event.currentTarget : null;
    const id = typeof key === "function" ? key(event) : key;
    return busy(btn, () => fn(event), id).catch(reportError);
  };
}

// --- Загрузка файлов: прогресс, отмена, лимит (FE-003) ---

// Лимиты загрузки по умолчанию; точные приходят в /config модуля (upload_limits).
export const UPLOAD_LIMITS = { image: 25 * 1024 * 1024, media: 200 * 1024 * 1024 };

const megabytes = (bytes) => Math.round(bytes / 2 ** 20);

let activeUploads = 0;
// Уход со страницы обрывает загрузку молча — пока она идёт, браузер переспросит.
function guardUnload(event) {
  event.preventDefault();
  event.returnValue = ""; // старые Chrome и Safari показывают вопрос только так
}
function uploadStarted() {
  if (activeUploads++ === 0) globalThis.addEventListener?.("beforeunload", guardUnload);
}
function uploadFinished() {
  if (--activeUploads === 0) globalThis.removeEventListener?.("beforeunload", guardUnload);
}
export const uploadsInFlight = () => activeUploads;

// Строка загрузки в лотке внизу экрана: имя, полоска, процент, «Отменить».
function uploadRow(label, onCancel) {
  if (typeof document === "undefined") return null;
  let tray = document.getElementById("upload-tray");
  if (!tray) {
    tray = h("div", { id: "upload-tray", class: "upload-tray", role: "status", "aria-label": "Загрузки" });
    document.body.append(tray);
  }
  const fill = h("div", { class: "bar-fill", style: "width:0%" });
  const bar = h("div", { class: "bar", role: "progressbar", "aria-valuemin": "0", "aria-valuemax": "100", "aria-valuenow": "0", "aria-label": `Загрузка: ${label}` }, fill);
  const percent = h("span", { class: "percent" }, "0%");
  const row = h(
    "div",
    { class: "upload-row progress" },
    h("div", { class: "progress-head" }, h("span", { class: "stage" }, label), percent),
    bar,
    h("div", { class: "progress-foot" }, h("span", {}), h("button", { type: "button", class: "btn btn--link btn--danger", onclick: onCancel }, "Отменить")),
  );
  tray.append(row);
  return {
    update(p) {
      fill.style.width = `${p}%`;
      bar.setAttribute("aria-valuenow", String(p));
      percent.textContent = `${p}%`;
    },
    remove() {
      row.remove();
      if (!tray.childElementCount) tray.remove();
    },
  };
}

// uploadFile(url, файл, { limit, label, onProgress, signal }) → JSON ответа или ApiError, как у api().
// Больше limit — отказ «Файл больше N МБ.» без запроса (kind: too_big): иначе сервер ответит 413,
// не дочитав тело, и браузер покажет «Failed to fetch». Пока идёт — строка с полоской и «Отменить»
// внизу экрана и предупреждение при уходе со страницы. Отмена (кнопкой или signal) — kind: abort.
// XMLHttpRequest, а не fetch: только у него есть прогресс отправки тела.
export function uploadFile(url, file, { limit = 0, label = file?.name || "файл", onProgress = null, signal = null } = {}) {
  if (limit && file.size > limit) {
    return Promise.reject(new ApiError(`Файл больше ${megabytes(limit)} МБ.`, { kind: "too_big", data: { limit } }));
  }
  if (signal?.aborted) return Promise.reject(new ApiError("Загрузка отменена.", { kind: "abort" }));
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    const cancel = () => xhr.abort();
    const row = uploadRow(label, cancel);
    let done = false;
    const finish = (settle, value) => {
      if (done) return;
      done = true;
      signal?.removeEventListener("abort", cancel);
      row?.remove();
      uploadFinished();
      settle(value);
    };
    xhr.open("POST", url);
    xhr.setRequestHeader("X-AIP", "1"); // как у api(): признак запроса своей страницы (SEC-002)
    xhr.upload.onprogress = (e) => {
      if (!e.lengthComputable) return;
      const percent = Math.min(100, Math.round((100 * e.loaded) / e.total));
      row?.update(percent);
      onProgress?.({ loaded: e.loaded, total: e.total, percent });
    };
    xhr.onload = () => {
      const text = xhr.responseText;
      let data = null;
      try {
        data = text ? JSON.parse(text) : {};
      } catch {
        data = null;
      }
      if (xhr.status >= 200 && xhr.status < 300) return finish(resolve, data || {});
      let message = data?.error || httpMessage(xhr.status, data ? "" : text);
      if (data?.request_id) message = `${message.replace(/\.$/, "")}. Код для поиска в логе: ${data.request_id}`;
      finish(reject, new ApiError(message, { kind: "http", status: xhr.status, data: data || {} }));
    };
    xhr.onerror = () => finish(reject, new ApiError("Сервис не отвечает. Проверь, что он запущен.", { kind: "network" }));
    xhr.onabort = () => finish(reject, new ApiError("Загрузка отменена.", { kind: "abort" }));
    signal?.addEventListener("abort", cancel, { once: true });
    uploadStarted();
    xhr.send(file);
  });
}

// --- Перерисовка блоков без потери ввода (студия, доска) ---

const blockSig = new WeakMap();
const TEXT_INPUTS = new Set(["text", "search", "url", "email", "number", "tel", "password", "date", "time", "datetime-local", "month", "week"]);
const isEditable = (el) => Boolean(el) && (el.tagName === "TEXTAREA" || el.tagName === "SELECT" || el.isContentEditable || (el.tagName === "INPUT" && TEXT_INPUTS.has(el.type)));

// В блоке фокус в поле, которое не вернуть после перерисовки (открытый список, поле без id) — блок подождёт.
function holdsInput(node) {
  const el = document.activeElement;
  return node.contains(el) && isEditable(el) && (el.tagName === "SELECT" || el.isContentEditable || !el.id);
}

const fieldKey = (el) => (el.type === "radio" ? (el.name ? `${el.name}=${el.value}` : null) : el.id || null);

// Несохранённые правки полей блока: чекбоксы, переключатели, списки и однострочные поля (тексты студия держит в state.drafts).
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

// reconcile(родитель, [{ key, sig, build }], scope) — узел с той же подписью остаётся как есть, изменившийся
// пересобирается (правки полей с id и раскрытые <details> переносятся, если scope тот же), лишние удаляются,
// порядок — как в items. Блок с полем в фокусе, которое не вернуть (без id, открытый список), ждёт.
export function reconcile(parent, items, scope = "") {
  const old = new Map();
  for (const n of parent.children) if (n.dataset.key) old.set(n.dataset.key, n);
  const nodes = items.map(({ key, sig, build }) => {
    const prev = old.get(key);
    if (prev && (blockSig.get(prev) === sig || holdsInput(prev))) return prev;
    const node = build();
    node.dataset.key = key;
    node.dataset.scope = scope;
    blockSig.set(node, sig);
    if (prev && prev.dataset.scope === scope) carryOver(prev, node);
    return node;
  });
  for (const n of [...parent.children]) if (!nodes.includes(n)) n.remove();
  nodes.forEach((n, i) => {
    if (parent.children[i] !== n) parent.insertBefore(n, parent.children[i] || null);
  });
}

// Фокус и курсор в поле с id внутри root переживают перерисовку его блока: captureFocus до, restoreFocus после.
export function captureFocus(root) {
  const el = document.activeElement;
  if (!el?.id || !root?.contains(el)) return null;
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

export function restoreFocus(saved) {
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

// --- Опрос ---

export const POLL_MAX_BACKOFF_MS = 30000;
export const POLL_HIDDEN_MS = 60000;

// Пауза до следующего опроса: fast — пока идёт работа, slow — в простое; после failures сбоев
// подряд — вдвое дольше на каждый (не дольше 30 с); в скрытой вкладке — ×10, но не чаще hiddenMs
// (Infinity — не опрашивать, пока вкладку не откроют).
export function pollDelay({ fast, slow, active, hidden = false, failures = 0, hiddenMs = POLL_HIDDEN_MS }) {
  let delay = active ? fast : slow;
  if (failures) delay = Math.min(Math.max(delay, fast) * 2 ** failures, Math.max(POLL_MAX_BACKOFF_MS, delay));
  if (hidden) delay = Math.max(delay * 10, hiddenMs);
  return delay;
}

// poller({ run, fast, slow, isActive, onError }) → { start, stop, now, later }.
// run(signal) — один опрос (промис); isActive() — идёт ли работа (быстрый интервал);
// onError(err, failures) — сбой (интервал растёт сам). Запрос в полёте отменяется по stop().
// В скрытой вкладке опрос редкий, при возврате — сразу. timers и doc — подмена для тестов.
export function poller({ run, fast = 1000, slow = 5000, isActive = () => false, onError = null, hiddenMs = POLL_HIDDEN_MS, timers = globalThis, doc = globalThis.document }) {
  let timer = null;
  let ctrl = null;
  let stopped = true;
  let failures = 0;
  let again = false;

  const hidden = () => Boolean(doc?.hidden);
  const clear = () => {
    if (timer !== null) timers.clearTimeout(timer);
    timer = null;
  };
  const schedule = (delay) => {
    clear();
    if (stopped || delay === Infinity) return;
    timer = timers.setTimeout(tick, delay);
  };
  const next = () => pollDelay({ fast, slow, active: Boolean(isActive()), hidden: hidden(), failures, hiddenMs });

  async function tick() {
    timer = null;
    if (stopped) return;
    if (ctrl) {
      again = true; // опрос уже идёт — повторить сразу после него
      return;
    }
    const mine = (ctrl = new AbortController());
    try {
      await run(mine.signal);
      failures = 0;
    } catch (err) {
      if (mine.signal.aborted || err?.kind === "abort") return;
      failures += 1;
      try {
        onError?.(err, failures);
      } catch (inner) {
        console.warn(inner);
      }
    } finally {
      if (ctrl === mine) ctrl = null;
    }
    if (stopped) return;
    if (again) {
      again = false;
      schedule(0);
    } else schedule(next());
  }

  const onVisibility = () => {
    if (stopped) return;
    if (hidden()) {
      if (!ctrl) schedule(next());
    } else self.now();
  };

  const self = {
    // start() — опросить сейчас; start(мс) — первый опрос через паузу.
    start(delay = 0) {
      if (!stopped) return self;
      stopped = false;
      doc?.addEventListener?.("visibilitychange", onVisibility);
      schedule(delay);
      return self;
    },
    stop() {
      stopped = true;
      clear();
      ctrl?.abort();
      ctrl = null;
      again = false;
      doc?.removeEventListener?.("visibilitychange", onVisibility);
    },
    // Опросить сейчас (после своего действия или возврата на вкладку).
    now() {
      if (stopped) return;
      if (ctrl) again = true;
      else schedule(0);
    },
    // Отложить следующий опрос на обычную паузу (данные только что обновили сами).
    later() {
      if (!stopped && !ctrl) schedule(next());
    },
    get failures() {
      return failures;
    },
  };
  return self;
}

const following = new Map();

// Опрос задачи до конца поверх poller: onUpdate(задача) на каждом шаге, промис — итог.
// Ответ 4xx (задачу удалили) — отказ с ошибкой; сбой сети — повтор с растущей паузой,
// после maxErrors сбоев подряд — отказ. На одну задачу (url) — один опрос: повторный вызов
// возвращает тот же промис, а onUpdate берётся последний (страница перерисовала свой блок).
export function followTask(url, onUpdate, opts = {}) {
  const { interval = 1000, signal, maxErrors = 20, timers, doc } = typeof opts === "number" ? { interval: opts } : opts;
  const known = following.get(url);
  if (known) {
    known.onUpdate = onUpdate;
    return known.promise;
  }
  const entry = { onUpdate };
  following.set(url, entry);
  entry.promise = new Promise((resolve, reject) => {
    let last = null;
    const finish = (settle, value) => {
      job.stop();
      signal?.removeEventListener("abort", onAbort);
      if (following.get(url) === entry) following.delete(url);
      settle(value);
    };
    const onAbort = () => finish(reject, new ApiError("Опрос задачи отменён.", { kind: "abort" }));
    const job = poller({
      fast: interval,
      slow: interval,
      ...(timers ? { timers } : {}),
      ...(doc !== undefined ? { doc } : {}),
      run: async (sig) => {
        const task = await api("GET", url, undefined, { signal: sig });
        entry.onUpdate(task);
        if (last && task.status !== last) announce(`${task.title || "Задача"}: ${STATUS[task.status] || task.status}`);
        last = task.status;
        if (task.status !== "queued" && task.status !== "running") finish(resolve, task);
      },
      onError: (err, failures) => {
        if ((err.kind === "http" && err.status >= 400 && err.status < 500) || failures >= maxErrors) finish(reject, err);
      },
    });
    if (signal?.aborted) return onAbort();
    signal?.addEventListener("abort", onAbort, { once: true });
    job.start();
  });
  return entry.promise;
}
