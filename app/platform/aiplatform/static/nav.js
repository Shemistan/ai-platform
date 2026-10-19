// Общая шапка платформы: вкладки, состояние GPU и предупреждения — из /api/config.
// Подключается каждой страницей вкладки: <script type="module" src="/nav.js"></script>.
import { currentTab, groupTabs } from "/core/nav-groups.js";
import { api, loadLast, poller, saveLast } from "/ui.js";

// Раздел «Если не работает при установке» (app/platform/README.md) — ссылка из баннера проблем.
const HELP_URL =
  "https://github.com/Shemistan/ai-platform/blob/main/app/platform/README.md#если-не-работает-при-установке";

// Вкладки шапки — единственный список на сервере: /api/config → tabs (server.nav_tabs, FE-007).
// [{ id, label, href, group, group_label, badge?, default? }]; группы — меню (UX-028). Последний ответ лежит
// в localStorage: шапка рисуется сразу, без ожидания сервера, и уточняется по ответу.
const NAV_CACHE = "nav-tabs";

function el(tag, attrs = {}, text = "") {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  if (text) node.textContent = text;
  return node;
}

function tabLink(tab, current) {
  const link = el("a", { href: tab.href }, tab.label);
  if (tab.id === current) link.setAttribute("aria-current", "page");
  if (tab.badge) link.append(el("span", { class: "badge", id: `platform-${tab.badge}`, hidden: "" }));
  return link;
}

// Меню группы: кнопка «Медиа · Монтаж ▾» и список ссылок. Открыто одно меню за раз; Esc и клик
// мимо закрывают, стрелки ходят по пунктам.
function closeMenus(except = null) {
  for (const button of document.querySelectorAll(".platform-nav .menu > button[aria-expanded='true']")) {
    if (button === except) continue;
    button.setAttribute("aria-expanded", "false");
    button.nextElementSibling.hidden = true;
  }
}

function menu(group, current, index) {
  const id = `nav-menu-${index}`;
  const button = el("button", { type: "button", "aria-expanded": "false", "aria-controls": id });
  button.append(el("span", {}, group.label));
  if (group.current) button.append(el("span", { class: "current" }, ` · ${group.current.label}`));
  button.append(el("span", { class: "caret", "aria-hidden": "true" }, "▾"));
  const list = el("ul", { class: "menu-list", id, hidden: "" });
  for (const tab of group.tabs) {
    const item = el("li");
    item.append(tabLink(tab, current));
    list.append(item);
  }
  const links = () => [...list.querySelectorAll("a")];
  const open = (focus) => {
    closeMenus(button);
    button.setAttribute("aria-expanded", "true");
    list.hidden = false;
    if (focus) links()[focus === "last" ? links().length - 1 : 0]?.focus();
  };
  button.addEventListener("click", () => (button.getAttribute("aria-expanded") === "true" ? closeMenus() : open()));
  button.addEventListener("keydown", (event) => {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      open(event.key === "ArrowDown" ? "first" : "last");
    }
  });
  list.addEventListener("keydown", (event) => {
    const all = links();
    const at = all.indexOf(document.activeElement);
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      all[(at + (event.key === "ArrowDown" ? 1 : -1) + all.length) % all.length]?.focus();
    } else if (event.key === "Escape") {
      closeMenus();
      button.focus();
    }
  });
  const item = el("li", { class: `menu${group.active ? " active" : ""}` });
  item.append(button, list);
  return item;
}

// Группа из одной вкладки — ссылка, из нескольких — меню (UX-028).
function fillTabs(list, tabs) {
  const current = currentTab(tabs, location);
  list.replaceChildren();
  groupTabs(tabs, current).forEach((group, i) => {
    if (group.tabs.length > 1) return list.append(menu(group, current, i));
    const item = el("li");
    item.append(tabLink(group.tabs[0], current));
    list.append(item);
  });
  showAttention();
}

function render() {
  const nav = el("nav", { class: "platform-nav", "aria-label": "Разделы платформы" });
  nav.append(el("span", { class: "brand" }, "AI Platform"));
  const list = el("ul", { class: "tabs" });
  const cached = loadLast(NAV_CACHE)?.data;
  if (Array.isArray(cached)) fillTabs(list, cached);
  nav.append(list);
  const gpu = el("span", { class: "gpu", id: "platform-gpu", title: "Тяжёлые модели идут по одной" }, "GPU: …");
  nav.append(gpu);
  document.body.prepend(nav);
  // Высота шапки — в --nav-h: под неё встаёт липкий степпер студии.
  if (window.ResizeObserver) {
    new ResizeObserver(() => document.documentElement.style.setProperty("--nav-h", `${nav.offsetHeight}px`)).observe(nav);
  }
  // Одна область объявлений на страницу: announce() из ui.js пишет сюда только переходы («готово», «ошибка»).
  if (!document.getElementById("sr-status")) {
    document.body.append(el("div", { id: "sr-status", class: "sr-only", role: "status", "aria-live": "polite" }));
  }
  // Клик мимо меню и Esc закрывают открытое меню групп.
  document.addEventListener("click", (event) => {
    if (!event.target.closest?.(".platform-nav .menu")) closeMenus();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeMenus();
  });
  return { gpu, list };
}

// Сколько выпусков ждут человека — счётчик у вкладки с badge: "attention" («Выпуски»).
let attention = 0;
function showAttention() {
  const badge = document.getElementById("platform-attention");
  if (!badge) return;
  badge.textContent = attention ? String(attention) : "";
  badge.hidden = !attention;
  badge.title = attention ? `Ждут тебя: ${attention}` : "";
}

// Состояние GPU и счётчик «Выпусков»: раз в 3 с, пока GPU занят, раз в 6 с в простое;
// в скрытой вкладке — раз в минуту, при сбое связи — реже (poller из ui.js).
function pollGpu(gpu) {
  let busy = false;
  const show = (data) => {
    busy = Boolean(data.gpu);
    gpu.classList.toggle("busy", busy);
    gpu.textContent = busy
      ? `GPU: ${data.gpu}${data.waiting ? ` · ждут ${data.waiting}` : ""}`
      : "GPU: свободен";
    attention = data.studio_attention || 0;
    showAttention();
  };
  poller({
    run: async (signal) => show(await api("GET", "/api/status", undefined, { signal, timeout: 10000 })),
    fast: 3000,
    slow: 6000,
    isActive: () => busy,
    onError: () => (gpu.textContent = "Нет связи с сервисом"),
  }).start();
}

async function loadConfig(nav, list) {
  try {
    const data = await api("GET", "/api/config", undefined, { timeout: 10000 });
    if (Array.isArray(data.tabs)) {
      if (JSON.stringify(data.tabs) !== JSON.stringify(loadLast(NAV_CACHE)?.data)) fillTabs(list, data.tabs);
      saveLast(NAV_CACHE, data.tabs);
    }
    if (data.version) {
      // Что сейчас запущено — справа в шапке, после GPU (docs/README.md «Версии»).
      nav.append(el("span", { class: "version", title: "Версия платформы" }, `v${data.version}`));
    }
    if (!data.problems || !data.problems.length) return;
    const box = el("div", { class: "platform-problems", role: "status" });
    box.append(el("strong", {}, data.backend === "fake" ? "Демо-режим. " : "Не всё настроено:"));
    const items = el("ul");
    for (const p of data.problems) items.append(el("li", {}, p));
    box.append(items);
    // Что делать с каждой проблемой — таблица «Если не работает при установке» в README сервиса.
    box.append(el("a", { class: "help", href: HELP_URL, target: "_blank", rel: "noopener" }, "Что делать →"));
    nav.after(box);
  } catch {
    /* шапка работает и без конфига */
  }
}

const { gpu, list } = render();
pollGpu(gpu);
loadConfig(gpu.parentElement, list);
