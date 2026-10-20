// Панель «Прогресс» (ITERATIVE_REVIEW_SPEC §14, PRD-028): обзор, дерево стадий, агенты, раунды, консоль логов,
// ресурсы, история запусков. Данные — только из backend: дерево GET progress, события по SSE
// (EventSource с Last-Event-ID) или опросом по курсору, ресурсы GET resources. DOM поверх progress.js.

import { ICONS, api, chip, fill, h, icon, toast } from "/ui.js";
import { agentCards, etaLabel, flatten, logQuery, logRow, mergeEvents, overview, resourceCards, runHistory, windowRows } from "./progress.js";

const ROW_H = 26;
const state = {
  pid: null, tree: null, events: [], cursor: 0, source: null, fallbackTimer: null, resources: null, agents: null,
  tab: "overview", collapsed: new Set(), filters: { severity: "", stage_id: "", agent_id: "", q: "", round_id: "", type: "" },
  follow: true, scrollTop: 0, lastTreeAt: 0, treeTimer: null, rerender: null,
};
const TABS = [["overview", "Обзор"], ["pipeline", "Этапы"], ["agents", "Агенты"], ["rounds", "Раунды"], ["logs", "Логи"], ["resources", "Ресурсы"], ["history", "История"]];

export function progressState() {
  return state;
}

// Подключить проект: закрыть прошлый поток, взять снапшот и события, открыть SSE (или опрос).
export async function attachProgress(ctx) {
  if (state.pid === ctx.project.id && (state.source || state.fallbackTimer)) return;
  detachProgress();
  state.pid = ctx.project.id;
  state.events = [];
  state.cursor = 0;
  state.rerender = ctx.rerender;
  await loadTree(ctx);
  await backfill(ctx);
  openStream(ctx);
}

export function detachProgress() {
  if (state.source) { state.source.close(); state.source = null; }
  if (state.fallbackTimer) { clearInterval(state.fallbackTimer); state.fallbackTimer = null; }
  if (state.treeTimer) { clearTimeout(state.treeTimer); state.treeTimer = null; }
}

async function loadTree(ctx) {
  try {
    state.tree = await api("GET", `${ctx.base}/progress`);
    state.lastTreeAt = Date.now();
  } catch (err) {
    ctx.setBanner(err.message, "error");
  }
}

async function backfill(ctx) {
  let more = true;
  while (more) {
    const page = await api("GET", `${ctx.base}/events?${logQuery({}, state.cursor, 2000)}`);
    state.events = mergeEvents(state.events, page.events);
    state.cursor = Math.max(state.cursor, page.next_cursor);
    more = page.has_more;
  }
}

function openStream(ctx) {
  if (typeof EventSource === "undefined") return openPolling(ctx);
  const url = `${ctx.base}/events/stream?last_event_id=${state.cursor}`;
  const es = new EventSource(url);
  state.source = es;
  es.addEventListener("snapshot", (e) => { try { state.tree = JSON.parse(e.data); state.lastTreeAt = Date.now(); } catch { /* пропуск */ } scheduleRender(); });
  es.addEventListener("bye", () => { es.close(); state.source = null; openStream(ctx); });
  es.onmessage = () => {};
  es.onerror = () => {
    if (es.readyState === EventSource.CLOSED) { state.source = null; openPolling(ctx); }
  };
  const onEvent = (e) => {
    let item;
    try { item = JSON.parse(e.data); } catch { return; }
    if (item.seq <= state.cursor && state.events.some((x) => x.seq === item.seq)) return; // дубль после реконнекта
    state.events = mergeEvents(state.events, [item]);
    state.cursor = Math.max(state.cursor, item.seq);
    if (!item.type.endsWith("_progress") || Date.now() - state.lastTreeAt > 2000) refreshTreeSoon(ctx);
    scheduleRender();
  };
  for (const t of EVENT_TYPES) es.addEventListener(t, onEvent);
}

const EVENT_TYPES = ["run_queued", "run_started", "run_finished", "run_failed", "run_cancelled", "run_blocked", "run_completed", "approval_required",
  "stage_started", "stage_progress", "stage_finished", "job_started", "job_progress", "job_finished", "job_failed", "scene_rendered", "log",
  "round_started", "reviewer_finished", "issues_consolidated", "repair_started", "qa_failed", "comparison_ready", "revision_accepted", "revision_rejected"];

function openPolling(ctx) {
  if (state.fallbackTimer) return;
  state.fallbackTimer = setInterval(async () => {
    try {
      const page = await api("GET", `${ctx.base}/events?${logQuery({}, state.cursor, 2000)}`);
      if (page.events.length) {
        state.events = mergeEvents(state.events, page.events);
        state.cursor = Math.max(state.cursor, page.next_cursor);
        await loadTree(ctx);
        scheduleRender();
      }
    } catch { /* следующий тик */ }
  }, 2000);
}

function refreshTreeSoon(ctx) {
  if (state.treeTimer) return;
  state.treeTimer = setTimeout(async () => { state.treeTimer = null; await loadTree(ctx); scheduleRender(); }, 500);
}

let renderQueued = false;
function scheduleRender() {
  if (renderQueued || !state.rerender) return;
  renderQueued = true;
  requestAnimationFrame(() => { renderQueued = false; state.rerender(); });
}

// ---------- панель ----------

export function progressPanel(ctx, run) {
  const tabs = h("div", { class: "tabs", role: "tablist", "aria-label": "Прогресс" }, ...TABS.map(([id, label]) =>
    h("button", { type: "button", role: "tab", class: `tab${state.tab === id ? " active" : ""}`, "aria-selected": String(state.tab === id), onclick: () => { state.tab = id; ctx.rerender(); } }, label)));
  const body = { overview: overviewView, pipeline: pipelineView, agents: agentsView, rounds: roundsView, logs: logsView, resources: resourcesView, history: historyView }[state.tab](ctx, run);
  const live = state.source ? chip("SSE", "success") : state.fallbackTimer ? chip("опрос", "warning") : chip("нет связи", "neutral");
  return h("section", { class: "panel stack progress-panel", "aria-label": "Прогресс" },
    h("div", { class: "field-head" }, h("h2", {}, "Прогресс"), h("div", { class: "toolbar" }, live, h("span", { class: "muted small" }, `событий ${state.events.length}`))),
    tabs,
    h("div", { role: "tabpanel" }, body),
  );
}

function overviewView(ctx, run) {
  const o = overview(state.tree?.tree, run);
  if (!o) return h("p", { class: "muted small" }, "Событий ещё нет: запусти действие.");
  const tree = state.tree?.tree;
  const counts = countIssues(state.events);
  return h("div", { class: "stack" },
    h("div", { class: "overview-grid" },
      bigNumber(o.percent != null ? `${Math.round(o.percent)}%` : "—", "общий прогресс", o.tone),
      bigNumber(o.stages, "этапов"), bigNumber(o.tasks, "задач"), run ? bigNumber(o.round, "раунд") : null,
      bigNumber(o.elapsed || "—", "прошло"), bigNumber(o.eta || "—", "осталось"),
      bigNumber(String(counts.errors), "ошибок", counts.errors ? "danger" : "neutral"), bigNumber(String(counts.warnings), "предупреждений", counts.warnings ? "warning" : "neutral"),
      run ? bigNumber(`${run.versions?.length || 0}`, `версий (${run.accepted_version || "—"})`) : null,
      run ? bigNumber(`${counts.accepted}/${counts.rejected}`, "принято/откат") : null,
    ),
    h("p", { class: "small" }, chip(o.status, o.tone), " ", o.current ? h("span", {}, o.current) : null),
    o.waiting ? h("div", { class: "callout", role: "status" }, "Нужно твоё решение — смотри панель ревью или план вырезок.") : null,
    o.lastError ? h("p", { class: "job-error" }, o.lastError) : null,
    tree?.last_updated ? h("p", { class: "muted small" }, `Обновлено ${new Date(tree.last_updated * 1000).toLocaleTimeString("ru-RU")}; проценты и ETA — расчёт сервера по единицам стадий и истории машины.`) : null,
  );
}

function countIssues(events) {
  let errors = 0, warnings = 0, accepted = 0, rejected = 0;
  for (const e of events) {
    if (e.severity === "error") errors += 1;
    if (e.severity === "warning") warnings += 1;
    if (e.type === "revision_accepted") accepted += 1;
    if (e.type === "revision_rejected") rejected += 1;
  }
  return { errors, warnings, accepted, rejected };
}

function bigNumber(value, label, tone = "neutral") {
  return h("div", { class: `big-number tone-${tone}` }, h("b", {}, value), h("span", { class: "muted small" }, label));
}

function pipelineView(ctx) {
  const rows = flatten(state.tree?.tree, state.collapsed);
  if (!rows.length) return h("p", { class: "muted small" }, "Дерева ещё нет.");
  return h("ol", { class: "tree" }, ...rows.map((r) => {
    const toggle = r.hasChildren ? h("button", { type: "button", class: "tree-toggle", "aria-expanded": String(!r.collapsed), "aria-label": r.collapsed ? "развернуть" : "свернуть", onclick: () => { if (state.collapsed.has(r.id)) state.collapsed.delete(r.id); else state.collapsed.add(r.id); ctx.rerender(); } }, r.collapsed ? "▸" : "▾") : h("span", { class: "tree-toggle" }, "");
    return h("li", { class: `tree-row kind-${r.kind} tone-${r.tone}`, style: `--depth:${r.depth}` },
      toggle,
      h("span", { class: "tree-mark", "aria-hidden": "true" }, ICONS[r.mark] ? icon(r.mark, { size: 14 }) : r.mark),
      h("span", { class: "tree-title" }, r.title, h("span", { class: "sr-only" }, ` — ${r.statusLabel}`)),
      h("span", { class: "tree-meta muted small" }, [r.statusLabel, r.counts, r.units, r.percent != null ? `${Math.round(r.percent)}%` : r.indeterminate ? "идёт…" : "", r.elapsed, r.eta, r.metrics, r.attempt].filter(Boolean).join(" · ")),
      r.percent != null && ["running", "retrying"].includes(r.status) ? h("div", { class: "bar tree-bar", role: "progressbar", "aria-valuemin": "0", "aria-valuemax": "100", "aria-valuenow": String(Math.round(r.percent)), "aria-label": r.title }, h("div", { class: "bar-fill", style: `width:${r.percent}%` })) : r.indeterminate && r.status === "running" ? h("div", { class: "bar waiting tree-bar", "aria-label": `${r.title}: объём неизвестен` }) : null,
      r.action || r.blocking ? h("span", { class: "small tree-action" }, r.blocking ? `⛔ ${r.blocking}` : r.action) : null,
    );
  }));
}

function agentsView(ctx) {
  const cards = agentCards(state.events, ctx.config.review?.roles || []);
  if (!cards.length) return h("p", { class: "muted small" }, "Агенты ещё не работали.");
  return h("div", { class: "agents-grid" }, ...cards.map((c) => h("article", { class: `agent-card tone-${c.tone}` },
    h("div", { class: "card-head" }, h("h3", {}, c.label), chip(c.statusLabel, c.tone)),
    h("p", { class: "muted small" }, [c.model ? `модель ${c.model}` : "", `выполнено ${c.done}`, c.issues ? `замечаний ${c.issues}` : "", c.attempts > 1 ? `попыток ${c.attempts}` : "", c.latency != null ? `${c.latency} с` : ""].filter(Boolean).join(" · ")),
    h("p", { class: "small" }, c.last || "—"),
  )));
}

function roundsView(ctx, run) {
  if (!run?.rounds?.length) return h("p", { class: "muted small" }, "Раундов ревью нет.");
  return h("div", { class: "stack" }, ...run.rounds.map((r) => {
    const evs = state.events.filter((e) => e.round_id === r.round_id);
    const changed = evs.filter((e) => e.type === "scene_rendered" && e.status === "completed").length;
    const qa = evs.filter((e) => e.type === "qa_failed").length;
    return h("div", { class: "round-progress" },
      h("div", { class: "field-head" }, h("h3", {}, `Раунд ${r.n} · ${r.base_version}${r.candidate_version ? ` → ${r.candidate_version}` : ""}`), chip(r.decision || r.state, r.decision === "accepted" ? "success" : r.decision === "rejected" ? "warning" : "neutral")),
      h("div", { class: "round-stages" }, ...(r.stages || []).map((s) => h("span", { class: `round-stage tone-${s.status === "done" ? "success" : s.status === "awaiting" ? "warning" : ["failed", "blocked"].includes(s.status) ? "danger" : s.status === "running" ? "running" : "neutral"}` }, s.id, h("span", { class: "sr-only" }, ` ${s.status}`)))),
      h("p", { class: "muted small" }, `замечаний: открыто ${r.counts?.open || 0}, принято ${r.counts?.accepted || 0}, отклонено ${r.counts?.rejected || 0}, исправлено ${r.counts?.fixed || 0} · затронуто сцен ${(r.scenes_touched || []).length}, перерисовано ${changed} · QA-сбоев ${qa}${r.decision_reason ? ` · ${r.decision_reason}` : ""}`),
    );
  }));
}

// ---------- консоль логов с виртуализацией ----------

function logsView(ctx) {
  const f = state.filters;
  const filtered = state.events.filter((e) =>
    (!f.severity || e.severity === f.severity) && (!f.stage_id || e.stage_id === f.stage_id) && (!f.agent_id || e.agent_id === f.agent_id) &&
    (!f.round_id || e.round_id === f.round_id) && (!f.type || e.type === f.type) && (!f.q || JSON.stringify(e).toLowerCase().includes(f.q.toLowerCase())));
  const stagesSeen = [...new Set(state.events.map((e) => e.stage_id).filter(Boolean))];
  const agentsSeen = [...new Set(state.events.map((e) => e.agent_id).filter(Boolean))];
  const roundsSeen = [...new Set(state.events.map((e) => e.round_id).filter(Boolean))];
  const typesSeen = [...new Set(state.events.map((e) => e.type))];
  const sel = (key, label, values) => {
    const el = h("select", { "aria-label": label, onchange: () => { f[key] = el.value; ctx.rerender(); } }, h("option", { value: "" }, label), ...values.map((v) => h("option", { value: v, selected: f[key] === v }, v)));
    return el;
  };
  const search = h("input", { type: "search", value: f.q, placeholder: "поиск", "aria-label": "Поиск по логам" });
  search.addEventListener("input", () => { f.q = search.value; ctx.rerender(); });
  const follow = h("button", { type: "button", class: `btn btn--sm ${state.follow ? "btn--primary" : "btn--secondary"}`, "aria-pressed": String(state.follow), onclick: () => { state.follow = !state.follow; ctx.rerender(); } }, state.follow ? "Автопрокрутка вкл" : "Автопрокрутка выкл");
  const copy = h("button", { type: "button", class: "btn btn--secondary btn--sm", onclick: async () => {
    try { await navigator.clipboard.writeText(filtered.map((e) => JSON.stringify(e)).join("\n")); toast("Скопировано"); } catch { ctx.setBanner("Буфер обмена недоступен", "error"); }
  } }, "Скопировать");
  const q = logQuery({ severity: f.severity, stage_id: f.stage_id, agent_id: f.agent_id, round_id: f.round_id, type: f.type, q: f.q });
  const box = h("div", { class: "log-console", tabindex: "0", "aria-label": `Логи, ${filtered.length} строк` });
  const inner = h("div", { class: "log-inner" });
  box.append(inner);
  const render = () => {
    const w = windowRows(filtered.length, box.scrollTop, box.clientHeight || 320, ROW_H);
    inner.style.height = `${w.height}px`;
    const rows = filtered.slice(w.first, w.last).map((e) => {
      const r = logRow(e);
      return h("div", { class: `log-row tone-${r.tone}`, style: `top:${0}px` },
        h("span", { class: "log-time" }, r.time), h("span", { class: "log-sev", "aria-label": r.severity }, r.mark),
        h("span", { class: "log-type" }, r.type), h("span", { class: "log-where" }, r.where),
        h("span", { class: "log-msg" }, r.message, r.seconds != null ? h("button", { type: "button", class: "phrase-time", onclick: () => ctx.seek(r.seconds) }, ` ${Math.round(r.seconds)} с`) : null,
          r.task && /^scene_/.test(r.task) ? h("a", { class: "small", href: `${ctx.base}/scenes/${r.task}/index.html`, target: "_blank" }, " сцена") : null,
          ...r.artifacts.map((a) => h("span", { class: "muted small" }, ` ${a}`))));
    });
    fill(inner, h("div", { class: "log-slice", style: `transform:translateY(${w.topPad}px)` }, ...rows));
  };
  box.addEventListener("scroll", () => { state.follow = box.scrollTop + box.clientHeight >= box.scrollHeight - ROW_H; render(); });
  queueMicrotask(() => { if (state.follow) box.scrollTop = box.scrollHeight; render(); });
  return h("div", { class: "stack" },
    h("div", { class: "toolbar" }, sel("severity", "серьёзность", ["debug", "info", "warning", "error"]), sel("stage_id", "стадия", stagesSeen), sel("agent_id", "агент", agentsSeen),
      roundsSeen.length ? sel("round_id", "раунд", roundsSeen) : null, sel("type", "тип", typesSeen), search, follow, copy,
      h("a", { class: "btn btn--link btn--sm", href: `${ctx.base}/logs/export?${q}` }, "JSONL"), h("a", { class: "btn btn--link btn--sm", href: `${ctx.base}/logs/export?format=csv&${q}` }, "CSV")),
    box,
    h("p", { class: "muted small" }, `Показано ${filtered.length} из ${state.events.length}; длинные списки рисуются окном, клик по секундам — к месту в ролике.`),
  );
}

async function fetchResources(ctx) {
  try { state.resources = await api("GET", `${ctx.base}/resources`); } catch { state.resources = null; }
}

function resourcesView(ctx) {
  if (!state.resources || Date.now() - (state.resources.sampled_at * 1000) > 5000) fetchResources(ctx).then(() => scheduleRender());
  const cards = resourceCards(state.resources);
  if (!cards.length) return h("p", { class: "muted small" }, "Замеры загружаются…");
  return h("div", { class: "stack" },
    h("div", { class: "overview-grid" }, ...cards.map((c) => bigNumber(c.value, `${c.label}${c.note ? ` · ${c.note}` : ""}`))),
    (state.resources.warnings || []).length ? h("div", { class: "callout" }, state.resources.warnings.join("; ")) : null,
    h("p", { class: "muted small" }, "Только то, что машина даёт измерить: загрузка, память, диск, очередь, скорость ffmpeg. GPU/VRAM на macOS без root не читаются — так и пишем."),
  );
}

function historyView() {
  const rows = runHistory(state.events);
  if (!rows.length) return h("p", { class: "muted small" }, "Запусков ещё не было.");
  return h("table", { class: "data-table small" }, h("thead", {}, h("tr", {}, h("th", {}, "запуск"), h("th", {}, "действие"), h("th", {}, "статус"), h("th", {}, "длительность"), h("th", {}, "этапов"), h("th", {}, "раундов"), h("th", {}, "причина"))),
    h("tbody", {}, ...rows.map((r) => h("tr", {}, h("td", { class: "mono" }, r.id), h("td", {}, r.action || ""), h("td", {}, chip(r.statusLabel, r.tone)), h("td", {}, r.duration || "идёт"), h("td", {}, String(r.stages)), h("td", {}, String(r.rounds)), h("td", { class: "small" }, r.reason || "")))));
}

export { etaLabel };
