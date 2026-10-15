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
  if (!res.ok) throw new Error(data.error || `Ошибка ${res.status}`);
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
