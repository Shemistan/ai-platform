// Общие помощники страниц платформы: запросы к API и построение DOM без сборки и зависимостей.

export const $ = (id) => document.getElementById(id);

export const API_TIMEOUT_MS = 30000;

// Ошибка запроса к API. kind: network — сервис не отвечает, timeout — не уложился в срок,
// http — ответ с кодом ошибки, abort — запрос отменили сами (signal). status и data — у http.
export class ApiError extends Error {
  constructor(message, { kind, status = 0, data = {}, cause } = {}) {
    super(message, cause ? { cause } : undefined);
    this.name = "ApiError";
    this.kind = kind;
    this.status = status;
    this.data = data;
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
    const message = data?.error || httpMessage(res.status, data ? "" : text);
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

export function statusChip(status) {
  return h("span", { class: `status ${status}` }, STATUS[status] || status);
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
      onCancel ? h("button", { type: "button", class: "link danger", onclick: () => onCancel(job) }, "Отменить") : null,
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

// chip("готово", "ok") — метка состояния: ok, warn, bad, info или без тона.
export function chip(text, tone = "") {
  return h("span", { class: `chip${tone ? ` ${tone}` : ""}` }, text);
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
  const btn = h("button", { type: "button", class: "small-btn" }, label);
  btn.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(typeof getText === "function" ? getText() : getText);
      btn.textContent = "Скопировано";
    } catch {
      btn.textContent = "Не вышло";
    }
    announce(btn.textContent);
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
    const yes = h("button", { type: "button", class: danger ? "primary danger" : "primary" }, confirmLabel);
    const no = h("button", { type: "button", class: "small-btn" }, cancelLabel);
    const { dialog, close } = modal(title, ...[].concat(body), h("div", { class: "toolbar confirm-actions" }, yes, no));
    yes.addEventListener("click", () => ((answer = true), close()));
    no.addEventListener("click", () => close());
    dialog.addEventListener("close", () => resolve(answer));
    no.focus(); // по Enter ничего необратимого не случится
  });
}

const VERDICT_TONES = { готово: "ok", "готово с оговорками": "warn", "не готово": "bad" };

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
      active && onCancel ? h("button", { type: "button", class: "link danger", onclick: safe(() => onCancel(task)) }, "Отменить") : null,
    ),
    task.error ? h("p", { class: "job-error" }, task.error) : null,
    full ? logBlock(task.log) : task.last && active ? h("div", { class: "task-last" }, task.last) : null,
  );
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

const pause = (ms, signal) =>
  new Promise((resolve) => {
    const timer = setTimeout(resolve, ms);
    signal?.addEventListener("abort", () => (clearTimeout(timer), resolve()), { once: true });
  });

const following = new Map();

// Опрос задачи до конца: onUpdate(задача) на каждом шаге, промис — итог.
// Ответ 4xx (задачу удалили) — отказ с ошибкой; сбой сети — повтор с растущей паузой,
// после maxErrors сбоев подряд — отказ. На одну задачу (url) — один опрос: повторный вызов
// возвращает тот же промис, а onUpdate берётся последний (страница перерисовала свой блок).
export function followTask(url, onUpdate, opts = {}) {
  const { interval = 1000, signal, maxErrors = 20 } = typeof opts === "number" ? { interval: opts } : opts;
  const known = following.get(url);
  if (known) {
    known.onUpdate = onUpdate;
    return known.promise;
  }
  const entry = { onUpdate };
  following.set(url, entry);
  entry.promise = (async () => {
    let last = null;
    let errors = 0;
    try {
      while (true) {
        if (signal?.aborted) throw new ApiError("Опрос задачи отменён.", { kind: "abort" });
        let task;
        try {
          task = await api("GET", url, undefined, { signal });
        } catch (err) {
          if (err.kind === "abort" || (err.kind === "http" && err.status >= 400 && err.status < 500)) throw err;
          if (++errors >= maxErrors) throw err;
          await pause(Math.min(interval * 2 ** errors, 30000), signal);
          continue;
        }
        errors = 0;
        entry.onUpdate(task);
        if (last && task.status !== last) announce(`${task.title || "Задача"}: ${STATUS[task.status] || task.status}`);
        last = task.status;
        if (task.status !== "queued" && task.status !== "running") return task;
        await pause(interval, signal);
      }
    } finally {
      if (following.get(url) === entry) following.delete(url);
    }
  })();
  return entry.promise;
}
