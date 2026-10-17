// Общая шапка платформы: вкладки, состояние GPU и предупреждения из /api/config.
// Подключается каждой страницей вкладки: <script type="module" src="/nav.js"></script>.

const TABS = [
  { id: "photo", label: "Фото", href: "/photo/" },
  { id: "video", label: "Видео", href: "/video/" },
  { id: "audio", label: "Аудио", href: "/audio/" },
  { sep: true },
  { id: "board", label: "Выпуски", href: "/board/" },
  { id: "digest", label: "Дайджест новостей", href: "/studio/?scenario=digest" },
  { id: "article", label: "Видео по статье", href: "/studio/?scenario=article" },
  { id: "post", label: "Пост в канал", href: "/studio/?scenario=post" },
  { sep: true },
  { id: "library", label: "Статьи", href: "/library/" },
  { id: "news", label: "Новости", href: "/news/" },
  { sep: true },
  { id: "channels", label: "Каналы", href: "/channels/" },
  { id: "updates", label: "Обновления", href: "/updates/" },
  { id: "settings", label: "Настройки", href: "/settings/" },
];

function currentTab() {
  const first = location.pathname.split("/").filter(Boolean)[0] || "";
  if (first === "studio") {
    const wanted = new URLSearchParams(location.search).get("scenario");
    return ["digest", "post"].includes(wanted) ? wanted : "article";
  }
  return first;
}

function el(tag, attrs = {}, text = "") {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  if (text) node.textContent = text;
  return node;
}

function render() {
  const nav = el("nav", { class: "platform-nav", "aria-label": "Разделы платформы" });
  nav.append(el("span", { class: "brand" }, "AI Platform"));
  const list = el("ul", { class: "tabs" });
  const current = currentTab();
  for (const tab of TABS) {
    if (tab.sep) {
      list.append(el("li", { class: "sep", "aria-hidden": "true" }));
      continue;
    }
    const link = el("a", { href: tab.href }, tab.label);
    if (tab.id === current) link.setAttribute("aria-current", "page");
    if (tab.id === "board") link.append(el("span", { class: "badge", id: "platform-attention", hidden: "" }));
    const item = el("li");
    item.append(link);
    list.append(item);
  }
  nav.append(list);
  const gpu = el("span", { class: "gpu", id: "platform-gpu", title: "Тяжёлые модели идут по одной" }, "GPU: …");
  nav.append(gpu);
  document.body.prepend(nav);
  // Одна область объявлений на страницу: announce() из ui.js пишет сюда только переходы («готово», «ошибка»).
  if (!document.getElementById("sr-status")) {
    document.body.append(el("div", { id: "sr-status", class: "sr-only", role: "status", "aria-live": "polite" }));
  }
  return gpu;
}

async function pollGpu(gpu) {
  try {
    const res = await fetch("/api/status");
    const data = await res.json();
    const busy = Boolean(data.gpu);
    gpu.classList.toggle("busy", busy);
    gpu.textContent = busy
      ? `GPU: ${data.gpu}${data.waiting ? ` · ждут ${data.waiting}` : ""}`
      : "GPU: свободен";
    // Сколько выпусков ждут человека — счётчик у вкладки «Выпуски».
    const badge = document.getElementById("platform-attention");
    if (badge) {
      const n = data.studio_attention || 0;
      badge.textContent = n ? String(n) : "";
      badge.hidden = !n;
      badge.title = n ? `Ждут тебя: ${n}` : "";
    }
  } catch {
    gpu.textContent = "GPU: нет связи";
  }
  setTimeout(() => pollGpu(gpu), 3000);
}

async function showProblems(nav) {
  try {
    const res = await fetch("/api/config");
    const data = await res.json();
    if (!data.problems || !data.problems.length) return;
    const box = el("div", { class: "platform-problems", role: "status" });
    box.append(el("strong", {}, data.backend === "fake" ? "Демо-режим. " : "Не всё настроено:"));
    const list = el("ul");
    for (const p of data.problems) list.append(el("li", {}, p));
    box.append(list);
    nav.after(box);
  } catch {
    /* шапка работает и без конфига */
  }
}

const gpu = render();
pollGpu(gpu);
showProblems(gpu.parentElement);
