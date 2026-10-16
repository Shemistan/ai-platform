// Вкладка «Новости»: источники, главное за день, добор упавших, обзоры для YouTube.
import { $, api, chip, copyButton, fill, followTask, h, modal, shortDate, taskCard } from "/ui.js";

const API = "/api/news";
const KINDS = {
  security: "уязвимость",
  breaking_change: "breaking change",
  release: "релиз",
  new_feature: "новая возможность",
  ai_tool: "AI-инструмент",
  engineering_deep_dive: "разбор",
  incident: "инцидент",
};
const FILES = { "teleprompter.md": "Суфлёр", "materials.md": "Материалы", "post.md": "Пост", "events.md": "События" };
const state = { tab: "sources", sourceFilter: "all", day: yesterday() };

function yesterday() {
  const d = new Date(Date.now() - 86400000);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

function when(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : `${shortDate(iso.slice(0, 10))}, ${d.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" })}`;
}

function segmentedButtons(values, current, onPick) {
  return h(
    "div",
    { class: "segmented", role: "group" },
    ...values.map(([value, label]) => h("button", { type: "button", "aria-pressed": String(value === current), onclick: () => onPick(value) }, label)),
  );
}

// Задача вкладки: карточка с логом, по концу — перерисовать раздел.
async function runTask(url, body, box) {
  try {
    const task = await api("POST", url, body);
    await followTask(`${API}/tasks/${task.id}`, (t) => fill(box, taskCard(t, { full: true, onCancel: cancel })));
    show(state.tab);
  } catch (err) {
    fill(box, h("p", { class: "job-error" }, err.message));
  }
}

async function cancel(task) {
  await api("POST", `${API}/tasks/${task.id}/cancel`);
}

function recentTask(tasks, box) {
  const active = (tasks || []).find((t) => t.status === "queued" || t.status === "running");
  if (active) followTask(`${API}/tasks/${active.id}`, (t) => fill(box, taskCard(t, { full: true, onCancel: cancel }))).then(() => show(state.tab));
}

// --- источники ---

function lastRun(s) {
  if (!s.enabled) return [chip("выключен"), s.note ? h("span", { class: "sub" }, s.note) : null];
  if (!s.last) return [chip("ещё не собирали")];
  const rescued = s.last.error && s.last.error.includes("добрал");
  return [
    s.last.ok ? chip("ок", "ok") : rescued ? chip("добрал агент", "info") : chip("сбой", "bad"),
    h("span", { class: "sub" }, when(s.last.at) + (s.last.ok ? ` · новых ${s.last.new}` : "")),
    !s.last.ok && s.last.error ? h("span", { class: "sub" }, s.last.error) : null,
  ];
}

function usefulness(s) {
  if (!s.processed) return h("span", { class: "muted" }, "—");
  const share = Math.round((100 * s.important) / s.processed);
  return h(
    "div",
    { class: "usefulness" },
    h("span", { class: "small" }, `${s.important} важных из ${s.processed}`),
    h("div", { class: "bar" }, h("div", { class: "bar-fill", style: `width:${Math.min(100, Math.max(share, 2))}%` })),
    s.useless ? chip("мало пользы", "warn") : null,
  );
}

async function showSources() {
  const data = await api("GET", `${API}/sources`);
  const taskBox = h("div", { class: "tasks" });
  const filters = [
    ["all", `Все (${data.sources.length})`],
    ["failed", `Со сбоем (${data.sources.filter((s) => s.failed).length})`],
    ["useless", `Мало пользы (${data.sources.filter((s) => s.useless).length})`],
    ["off", `Выключенные (${data.sources.filter((s) => !s.enabled).length})`],
  ];
  const pick = (s) => ({ all: true, failed: s.failed, useless: s.useless, off: !s.enabled })[state.sourceFilter];
  const rows = data.sources.filter(pick).map((s) =>
    h(
      "tr",
      {},
      h("td", {}, s.name, h("span", { class: "sub" }, `${s.id} · ${s.kind}${s.secondary ? " · вторичный" : ""}`)),
      h("td", { class: "num" }, s.authority.toFixed(2)),
      h("td", {}, ...lastRun(s)),
      h("td", {}, usefulness(s)),
      h("td", { class: "num" }, s.enabled ? h("button", { type: "button", class: "small-btn", onclick: () => runTask(`${API}/sources/collect`, { only: s.id }, taskBox) }, "Собрать") : null),
    ),
  );
  fill(
    $("view"),
    h(
      "div",
      { class: "card-head" },
      segmentedButtons(filters, state.sourceFilter, (v) => ((state.sourceFilter = v), showSources())),
      h("button", { type: "button", class: "secondary", onclick: () => runTask(`${API}/sources/collect`, {}, taskBox) }, "Собрать все"),
    ),
    h("p", { class: "hint", style: "margin:10px 0" }, `Вес — авторитет источника в оценке важности. «Мало пользы» — разобрано от ${data.useless_after} материалов, а в выпуски не попало ничего: понизить вес или выключить в config/news-sources.toml движка.`),
    taskBox,
    rows.length
      ? h("div", { class: "table-wrap" }, h("table", { class: "data-table" }, h("thead", {}, h("tr", {}, h("th", {}, "Источник"), h("th", {}, "Вес"), h("th", {}, "Последний сбор"), h("th", {}, "Польза"), h("th", {}, ""))), h("tbody", {}, ...rows)))
      : h("p", { class: "empty" }, "Под фильтр ничего не попало."),
  );
  recentTask(data.tasks, taskBox);
}

// --- главное за день ---

async function showTop() {
  const data = await api("GET", `${API}/top?day=${state.day}`);
  const dayInput = h("input", { type: "date", value: state.day, max: yesterday(), style: "width:auto" });
  dayInput.addEventListener("change", () => {
    state.day = dayInput.value || yesterday();
    showTop();
  });
  const issue = data.issue;
  const verdictTone = { готово: "ok", "готово с оговорками": "warn", "не готово": "bad" };
  fill(
    $("view"),
    h(
      "div",
      { class: "card-head" },
      h("div", { class: "toolbar" }, h("span", { class: "field-label" }, "День"), dayInput),
      h(
        "div",
        { class: "toolbar" },
        issue.exists ? chip("выпуск собран", "ok") : chip("выпуска нет"),
        issue.verdict ? chip(`судья: ${issue.verdict}`, verdictTone[issue.verdict]) : null,
        h("a", { class: "link", href: "/studio/?scenario=digest" }, "Дайджест новостей →"),
      ),
    ),
    h("p", { class: "hint", style: "margin:10px 0" }, "События, опубликованные в этот день, по важности 1–10: в выпуск идёт от 7, обязательные — от 9 и уязвимости. Видно, что не вошло."),
    data.events.length
      ? h(
          "div",
          {},
          ...data.events.map((e) =>
            h(
              "div",
              { class: "event" },
              h("span", { class: `importance${e.importance >= 8 ? " high" : e.importance >= 6 ? " mid" : ""}`, title: `балл ${e.score}` }, String(e.importance)),
              h(
                "div",
                { style: "display:grid;gap:4px" },
                h("h3", {}, e.url ? h("a", { href: e.url, target: "_blank", rel: "noopener" }, e.title) : e.title),
                e.why ? h("p", { class: "muted small" }, e.why) : null,
                h("div", { class: "toolbar" }, chip(KINDS[e.kind] || e.kind, "info"), e.issued ? chip("в выпуске", "ok") : chip("не вошло"), h("span", { class: "muted small" }, e.sources.join(", "))),
              ),
            ),
          ),
        )
      : h("p", { class: "empty" }, "За этот день оценённых событий нет: сбор не запускали или агент ещё не разобрал материалы."),
  );
}

// --- добор ---

async function showFailed() {
  const data = await api("GET", `${API}/failed`);
  const taskBox = h("div", { class: "tasks" });
  const day = h("input", { type: "date", value: state.day, max: yesterday(), style: "width:auto" });
  fill(
    $("view"),
    h("p", { class: "hint" }, "Источники, у которых упал последний сбор (404, 429, лента не разобралась). Агент открывает их страницы сам и добавляет найденное в базу — то же делает и агент выпуска на шаге «Добор». Источник от этого не чинится: сбой остаётся с пометкой «добрал агент»."),
    data.failed.length
      ? h(
          "div",
          { class: "table-wrap", style: "margin-top:12px" },
          h(
            "table",
            { class: "data-table" },
            h("thead", {}, h("tr", {}, h("th", {}, "Источник"), h("th", {}, "Когда"), h("th", {}, "Ошибка"))),
            h("tbody", {}, ...data.failed.map((f) => h("tr", {}, h("td", {}, h("a", { class: "link", href: f.url, target: "_blank", rel: "noopener" }, f.name), h("span", { class: "sub" }, `${f.source} · ${f.kind}`)), h("td", {}, when(f.at)), h("td", {}, f.error)))),
          ),
        )
      : h("p", { class: "empty" }, "Упавших источников нет — добирать нечего."),
    h(
      "div",
      { class: "toolbar", style: "margin-top:12px" },
      h("span", { class: "field-label" }, "Материалы за"),
      day,
      h("button", { type: "button", class: "secondary", disabled: !data.failed.length, onclick: () => runTask(`${API}/failed/rescue`, { day: day.value }, taskBox) }, "Добрать агентом"),
    ),
    taskBox,
  );
  recentTask(data.tasks, taskBox);
}

// --- обзоры ---

function openReview(id) {
  const body = h("div", { class: "field" }, h("p", { class: "muted" }, "Загружаю…"));
  modal("Обзор", body);
  api("GET", `${API}/reviews/${encodeURIComponent(id)}`).then((r) => {
    const names = Object.keys(r.files);
    let current = names[0];
    const text = h("pre", { class: "preview-text", style: "max-height:55vh" });
    const tools = h("div", { class: "toolbar" });
    const tabs = h("div", { class: "segmented", role: "group" });
    const render = () => {
      text.textContent = r.files[current];
      fill(tabs, ...names.map((n) => h("button", { type: "button", "aria-pressed": String(n === current), onclick: () => ((current = n), render()) }, FILES[n] || n)));
      const blob = URL.createObjectURL(new Blob([r.files[current]], { type: "text/markdown" }));
      fill(tools, copyButton(() => r.files[current]), h("a", { class: "small-btn", href: blob, download: `${id}-${current}` }, "Скачать"));
    };
    render();
    fill(body, h("h3", {}, r.title), h("div", { class: "card-head" }, tabs, tools), text);
  });
}

async function showReviews() {
  const data = await api("GET", `${API}/reviews`);
  const taskBox = h("div", { class: "tasks" });
  const now = new Date();
  const month = h("input", { type: "month", value: `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}`, style: "width:auto" });
  fill(
    $("view"),
    h("p", { class: "hint" }, "Видео, которое снимаешь сам и читаешь с суфлёра. Агент отбирает события за неделю (суббота–пятница) или за месяц и пишет суфлёр, материалы для монтажа и пост."),
    h(
      "div",
      { class: "toolbar", style: "margin:12px 0" },
      h("button", { type: "button", class: "secondary", onclick: () => runTask(`${API}/reviews/weekly`, {}, taskBox) }, "Обзор недели"),
      h("span", { class: "muted" }, "или"),
      month,
      h("button", { type: "button", class: "secondary", onclick: () => runTask(`${API}/reviews/monthly`, { month: month.value }, taskBox) }, "Обзор месяца"),
    ),
    taskBox,
    data.reviews.length
      ? h(
          "div",
          { class: "list", style: "margin-top:12px" },
          ...data.reviews.map((r) =>
            h(
              "button",
              { type: "button", class: "card review-card", onclick: () => openReview(r.id) },
              h("div", { class: "card-head" }, h("h3", {}, r.title), h("span", { class: "toolbar" }, chip(r.kind === "weekly" ? "неделя" : "месяц", "info"), h("span", { class: "muted small" }, shortDate(r.added)))),
              h("div", { class: "toolbar" }, ...r.files.map((f) => chip(FILES[f] || f))),
            ),
          ),
        )
      : h("p", { class: "empty" }, "Обзоров пока нет."),
  );
  recentTask(data.tasks, taskBox);
}

// --- разделы ---

const VIEWS = { sources: showSources, top: showTop, failed: showFailed, reviews: showReviews };

async function show(tab) {
  state.tab = tab;
  for (const b of $("tabs").querySelectorAll("button")) b.setAttribute("aria-pressed", String(b.value === tab));
  history.replaceState(null, "", `#${tab}`);
  try {
    await VIEWS[tab]();
  } catch (err) {
    fill($("view"), h("p", { class: "job-error" }, err.message));
  }
}

for (const b of $("tabs").querySelectorAll("button")) b.addEventListener("click", () => show(b.value));
window.addEventListener("hashchange", () => {
  const tab = location.hash.slice(1);
  if (VIEWS[tab] && tab !== state.tab) show(tab);
});
show(VIEWS[location.hash.slice(1)] ? location.hash.slice(1) : "sources");
