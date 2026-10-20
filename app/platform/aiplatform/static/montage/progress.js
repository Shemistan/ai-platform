// Панель «Прогресс» (ITERATIVE_REVIEW_SPEC §14): дерево стадий, подписи ETA, окно виртуализации логов, фильтры.
// Чистые функции без DOM; данные — GET progress (дерево), events (курсор), resources.

export const NODE_STATUS = {
  pending: "впереди", queued: "в очереди", running: "идёт", awaiting_approval: "ждёт тебя", paused: "пауза",
  retrying: "повтор", blocked: "заблокировано", failed: "упало", cancelled: "отменено", skipped: "пропущено",
  completed: "готово", none: "",
};
export const NODE_TONE = {
  running: "running", completed: "success", failed: "danger", blocked: "danger", cancelled: "neutral",
  awaiting_approval: "warning", paused: "warning", retrying: "warning", skipped: "neutral", pending: "neutral", queued: "neutral",
};
// Метка статуса: имя иконки ui.js (check, close, play) или короткий текст — рисует progress-ui.js.
export const NODE_MARK = { completed: "check", failed: "close", blocked: "close", cancelled: "close", skipped: "–", awaiting_approval: "!", paused: "‖", running: "play", retrying: "↻" };
export const SEVERITY_MARK = { debug: "·", info: "i", warning: "!", error: "✗" };

// Дерево → плоский список строк с глубиной и раскрытием; collapsed — Set id свёрнутых узлов.
export function flatten(tree, collapsed = new Set()) {
  const rows = [];
  const walk = (node, depth) => {
    const kids = node.children || [];
    rows.push({ ...nodeRow(node), depth, hasChildren: kids.length > 0, collapsed: collapsed.has(node.id) });
    if (!collapsed.has(node.id)) for (const kid of kids) walk(kid, depth + 1);
  };
  if (tree) walk(tree, 0);
  return rows;
}

export function nodeRow(node) {
  const c = node.counts || {};
  return {
    id: node.id, kind: node.kind, title: node.title, status: node.status,
    statusLabel: NODE_STATUS[node.status] ?? node.status, tone: NODE_TONE[node.status] || "neutral",
    mark: NODE_MARK[node.status] || "○",
    percent: node.indeterminate ? null : node.percent,
    indeterminate: !!node.indeterminate,
    counts: c.total ? `${c.completed}/${c.total}${c.failed ? ` · упало ${c.failed}` : ""}${c.skipped ? ` · пропущено ${c.skipped}` : ""}` : "",
    remaining: c.total ? c.remaining : null,
    elapsed: node.elapsed_s != null ? span(node.elapsed_s) : "",
    eta: ["running", "retrying", "queued"].includes(node.status) ? etaLabel(node.eta) : "",
    action: node.current_action || "",
    blocking: node.blocking_reason || "",
    units: node.units ? `${node.units.completed_units}/${node.units.total_units} ${node.units.unit}` : "",
    attempt: node.attempt ? `попытка ${node.attempt}` : "",
    metrics: node.metrics && node.metrics.render_fps ? `${node.metrics.render_fps} fps` : "",
  };
}

// ETA словами: «≈ 2 мин (1–3), оценка грубая» или «пока неизвестно».
export function etaLabel(eta) {
  if (!eta || eta.seconds == null) return eta?.basis === "idle" ? "" : "пока неизвестно";
  const conf = { low: "оценка грубая", medium: "оценка средняя", high: "оценка точная" }[eta.confidence] || "";
  const range = eta.low != null && eta.high != null && eta.high !== eta.low ? ` (${span(eta.low)}–${span(eta.high)})` : "";
  return `≈ ${span(eta.seconds)}${range}${conf ? `, ${conf}` : ""}`;
}

export function span(value) {
  if (value == null) return "";
  const s = Math.max(0, Math.round(value));
  if (s < 60) return `${s} с`;
  const m = Math.round(s / 60);
  return m < 60 ? `${m} мин` : `${Math.floor(m / 60)} ч${m % 60 ? ` ${m % 60} мин` : ""}`;
}

// Обзор: X/Y этапов и задач, активный этап, раунд N/max, ETA, последняя ошибка.
export function overview(tree, run) {
  if (!tree) return null;
  const runs = tree.children || [];
  const active = runs.find((r) => ["running", "queued", "retrying"].includes(r.status)) || runs[runs.length - 1];
  const stages = active ? collect(active, "stage") : [];
  const tasks = active ? collect(active, "task") : [];
  const current = stages.find((s) => s.status === "running");
  const errors = collect(tree, null).filter((n) => ["failed", "blocked"].includes(n.status));
  return {
    status: NODE_STATUS[tree.status] ?? tree.status, tone: NODE_TONE[tree.status] || "neutral",
    percent: active?.percent ?? null,
    stages: `${stages.filter((s) => s.status === "completed").length}/${stages.length}`,
    tasks: `${tasks.filter((t) => t.status === "completed").length}/${tasks.length}`,
    current: current ? `${current.title}${current.current_action ? ` — ${current.current_action}` : ""}` : "",
    round: run ? `${run.round || 0} / ${run.rounds_planned}` : "",
    eta: active && ["running", "retrying", "queued"].includes(active.status) ? etaLabel(active.eta) : "",
    elapsed: active?.elapsed_s != null ? span(active.elapsed_s) : "",
    lastError: errors.length ? `${errors[errors.length - 1].title}: ${errors[errors.length - 1].blocking_reason || errors[errors.length - 1].current_action || ""}` : "",
    waiting: tree.status === "awaiting_approval",
    updated: tree.last_updated || null,
  };
}

export function collect(node, kind) {
  const out = [];
  const walk = (n) => {
    if (!kind || n.kind === kind) out.push(n);
    for (const kid of n.children || []) walk(kid);
  };
  walk(node);
  return out;
}

// Карточки агентов: последнее событие по agent_id — статус, задача, счётчики.
export function agentCards(events, roles) {
  const byAgent = new Map();
  for (const ev of events || []) {
    if (!ev.agent_id) continue;
    const card = byAgent.get(ev.agent_id) || { id: ev.agent_id, done: 0, issues: 0, attempts: 0, latency: null, status: "pending", last: "" };
    card.status = ev.status !== "none" ? ev.status : card.status;
    card.last = ev.message || card.last;
    card.at = ev.occurred_at;
    if (ev.type === "reviewer_finished" || (ev.type === "job_finished" && ev.status === "completed")) card.done += 1;
    if (ev.metrics?.issues != null) card.issues += ev.metrics.issues;
    if (ev.metrics?.elapsed_s != null) card.latency = ev.metrics.elapsed_s;
    if (ev.attempt) card.attempts = Math.max(card.attempts, ev.attempt);
    if (ev.metrics?.model) card.model = ev.metrics.model;
    byAgent.set(ev.agent_id, card);
  }
  const labels = Object.fromEntries((roles || []).map((r) => [r.id, r.label]));
  return [...byAgent.values()].map((c) => ({
    ...c, label: labels[c.id] || c.id, statusLabel: NODE_STATUS[c.status] ?? c.status, tone: NODE_TONE[c.status] || "neutral",
  }));
}

// Виртуализация: какие строки рисовать при прокрутке; rowHeight px, overscan строк сверху и снизу.
export function windowRows(total, scrollTop, viewportHeight, rowHeight = 28, overscan = 8) {
  const first = Math.max(0, Math.floor(scrollTop / rowHeight) - overscan);
  const visible = Math.ceil(viewportHeight / rowHeight) + overscan * 2;
  const last = Math.min(total, first + visible);
  return { first, last, topPad: first * rowHeight, bottomPad: Math.max(0, (total - last) * rowHeight), height: total * rowHeight };
}

// Строка лога.
export function logRow(ev) {
  const t = new Date(ev.occurred_at * 1000);
  const where = [ev.stage_id, ev.agent_id, ev.job_id, ev.task_id].filter(Boolean).join(" · ");
  return {
    seq: ev.seq, time: `${String(t.getHours()).padStart(2, "0")}:${String(t.getMinutes()).padStart(2, "0")}:${String(t.getSeconds()).padStart(2, "0")}`,
    severity: ev.severity, mark: SEVERITY_MARK[ev.severity] || "", type: ev.type, status: ev.status,
    where, message: ev.message, round: ev.round_id || "", run: ev.run_id || "",
    seconds: ev.progress?.unit === "seconds" ? ev.progress.completed_units : null,
    task: ev.task_id || null, artifacts: ev.artifact_ids || [],
    tone: ev.severity === "error" ? "danger" : ev.severity === "warning" ? "warning" : "neutral",
  };
}

// Фильтры логов → query string для /events и /logs/export.
export function logQuery(filters, cursor = 0, limit = 500) {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(filters || {})) if (v !== "" && v != null) q.set(k, String(v));
  if (cursor) q.set("cursor", String(cursor));
  q.set("limit", String(limit));
  return q.toString();
}

// Слияние новых событий в буфер без дублей (по seq) и с лимитом в памяти.
export function mergeEvents(buffer, incoming, max = 100000) {
  const seen = new Set(buffer.map((e) => e.seq));
  const out = buffer.slice();
  for (const ev of incoming || []) {
    if (!seen.has(ev.seq)) {
      out.push(ev);
      seen.add(ev.seq);
    }
  }
  out.sort((a, b) => a.seq - b.seq);
  return out.length > max ? out.slice(out.length - max) : out;
}

// Ресурсы для карточек: только то, что есть.
export function resourceCards(res) {
  if (!res) return [];
  const gb = (b) => `${(b / 2 ** 30).toFixed(1)} ГБ`;
  const cards = [];
  if (res.cpu?.load_1m != null) cards.push({ id: "cpu", label: "CPU", value: `${res.cpu.load_1m} / ${res.cpu.cores}`, note: "load 1 мин / ядер" });
  if (res.ram?.total_bytes) cards.push({ id: "ram", label: "Память", value: res.ram.used_bytes != null ? `${gb(res.ram.used_bytes)} из ${gb(res.ram.total_bytes)}` : gb(res.ram.total_bytes), note: res.ram.used_bytes != null ? "занято" : "всего" });
  if (res.disk?.free_bytes != null) cards.push({ id: "disk", label: "Диск", value: gb(res.disk.free_bytes), note: "свободно" });
  cards.push({ id: "gpu", label: "GPU", value: res.gpu?.status === "unavailable" ? "нет метрик" : "", note: res.gpu?.reason || "" });
  if (res.queue) cards.push({ id: "queue", label: "Очередь", value: `${res.queue.depth}`, note: `ждут GPU: ${res.queue.gpu_waiting}` });
  if (res.render?.fps != null) cards.push({ id: "render", label: "Рендер", value: `${res.render.fps} fps`, note: res.render.speed ? `×${res.render.speed}` : "" });
  if (res.retries != null) cards.push({ id: "retries", label: "Повторы", value: `${res.retries}`, note: "попыток сверх первой" });
  return cards;
}

// История запусков из событий run_*: статус, длительность, причина.
export function runHistory(events) {
  const runs = new Map();
  for (const ev of events || []) {
    if (!ev.run_id) continue;
    const r = runs.get(ev.run_id) || { id: ev.run_id, action: ev.phase_id, status: "queued", started: ev.occurred_at, finished: null, reason: "", stages: new Set(), rounds: new Set() };
    if (ev.type.startsWith("run_")) {
      r.status = ev.status !== "none" ? ev.status : r.status;
      if (ev.reason) r.reason = ev.reason;
      if (["completed", "failed", "cancelled", "blocked", "awaiting_approval"].includes(ev.status)) r.finished = ev.occurred_at;
    }
    if (ev.stage_id) r.stages.add(ev.stage_id);
    if (ev.round_id) r.rounds.add(ev.round_id);
    runs.set(ev.run_id, r);
  }
  return [...runs.values()].reverse().map((r) => ({
    ...r, stages: r.stages.size, rounds: r.rounds.size, statusLabel: NODE_STATUS[r.status] ?? r.status, tone: NODE_TONE[r.status] || "neutral",
    duration: r.finished ? span(r.finished - r.started) : "",
  }));
}
