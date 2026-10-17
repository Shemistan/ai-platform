// Общие помощники страниц платформы: запросы к API и построение DOM без сборки и зависимостей.

export const $ = (id) => document.getElementById(id);

export async function api(method, path, body) {
  const init = { method, headers: {} };
  if (body instanceof Blob) {
    init.body = body;
  } else if (body !== undefined) {
    init.body = JSON.stringify(body);
    init.headers["Content-Type"] = "application/json";
  }
  const res = await fetch(path, init);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw Object.assign(new Error(data.error || `Ошибка ${res.status}`), { status: res.status, data });
  return data;
}

// h("p", { class: "x", onclick: fn }, "текст", child, null) — null и false пропускаются.
export function h(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
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
      active && onCancel ? h("button", { type: "button", class: "link danger", onclick: () => onCancel(task) }, "Отменить") : null,
    ),
    task.error ? h("p", { class: "job-error" }, task.error) : null,
    full ? logBlock(task.log) : task.last && active ? h("div", { class: "task-last" }, task.last) : null,
  );
}

const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// Опрос задачи до конца: onUpdate(задача) на каждом шаге, промис — итог.
export async function followTask(url, onUpdate, interval = 1000) {
  let last = null;
  while (true) {
    let task = null;
    try {
      task = await api("GET", url);
    } catch {
      await pause(interval * 2);
      continue;
    }
    onUpdate(task);
    if (last && task.status !== last) announce(`${task.title || "Задача"}: ${STATUS[task.status] || task.status}`);
    last = task.status;
    if (task.status !== "queued" && task.status !== "running") return task;
    await pause(interval);
  }
}
