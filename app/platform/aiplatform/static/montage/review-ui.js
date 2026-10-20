// Панель «Ревью и улучшение» (ITERATIVE_REVIEW_SPEC §2, §11, PRD-028): DOM поверх чистых функций review.js.
// Все кнопки зовут API; данных «из головы» нет. ctx: { api, base, project, config, story, refresh, setBanner }.

import { api, chip, confirmAction, fill, h, toast } from "/ui.js";
import { STATE_LABEL, abPair, abSync, clock, comparisonView, filterIssues, filterOptions, issueCard, planRows, roundRow, runSummary, versionRow } from "./review.js";

const cache = { runId: null, runs: null, round: null, issues: null, plan: null, comparison: null, versions: null, key: "" };
const filters = { severity: "", scene: "", role: "", status: "" };
let decisions = {}; // issue_id → accepted | rejected | deferred (до отправки)
let roundShown = null;

export function resetReviewCache() {
  cache.key = "";
  cache.runs = null;
}

// Загрузка данных запуска: список запусков, текущий, раунд, замечания, план, сравнение, версии.
export async function loadReview(ctx, signal) {
  const p = ctx.project;
  const key = `${p.id}:${p.updated_at}:${p.status}`;
  const runs = (await api("GET", `${ctx.base}/review-runs`, undefined, { signal })).runs;
  const open = runs.find((r) => !r.final) || runs[runs.length - 1] || null;
  const run = open ? await api("GET", `${ctx.base}/review-runs/${open.run_id}`, undefined, { signal }) : null;
  const n = run ? (roundShown && roundShown <= run.round ? roundShown : run.round) : 0;
  const same = cache.key === key && cache.runId === run?.run_id && cache.round === n && !run?.active;
  if (same) return;
  let issues = null, plan = null, comparison = null;
  if (run && n) {
    [issues, plan, comparison] = await Promise.all([
      api("GET", `${ctx.base}/review-runs/${run.run_id}/rounds/${n}/issues`, undefined, { signal }),
      api("GET", `${ctx.base}/review-runs/${run.run_id}/rounds/${n}/repair-plan`, undefined, { signal }),
      api("GET", `${ctx.base}/review-runs/${run.run_id}/rounds/${n}/comparison`, undefined, { signal }),
    ]);
  }
  const versions = (await api("GET", `${ctx.base}/versions`, undefined, { signal })).versions;
  Object.assign(cache, { key, runs, run, runId: run?.run_id || null, round: n, issues, plan, comparison, versions });
  if (run?.run_id !== cache.lastRunForDecisions) {
    decisions = {};
    cache.lastRunForDecisions = run?.run_id;
  }
}

export function reviewData() {
  return cache;
}

export function reviewPanel(ctx) {
  const run = cache.run;
  const busy = ctx.project.status === "queued" || ctx.project.status === "running";
  const body = run ? runView(ctx, run, busy) : settingsForm(ctx, busy);
  return h("section", { class: "panel stack review-panel", "aria-label": "Ревью и улучшение" },
    h("div", { class: "field-head" }, h("h2", {}, "Ревью и улучшение"),
      h("div", { class: "toolbar" }, run ? chip(STATE_LABEL[run.state] || run.state, runSummary(run).tone) : chip("не запущено", "neutral"))),
    h("p", { class: "muted small" }, "Независимые роли смотрят готовый ролик, замечания сводятся в план точечных правок, кандидат проходит технический QA и сравнение с baseline. Без мультимодальной модели роли «глаз» судят по манифестам и помечены как требующие проверки."),
    body,
  );
}

// ---------- настройки и запуск ----------

function settingsForm(ctx, busy) {
  const cfg = ctx.config.review;
  const d = cfg.defaults;
  const rounds = h("input", { type: "number", min: "1", max: "5", step: "1", value: String(d.max_rounds), "aria-label": "Раундов" });
  const mode = h("select", { "aria-label": "Режим" }, ...cfg.modes.map((m) => h("option", { value: m.id, selected: m.id === d.mode }, m.label)));
  const profile = h("select", { "aria-label": "Профиль" }, ...cfg.profiles.map((p) => h("option", { value: p.id, selected: p.id === d.review_profile }, PROFILE_LABEL[p.id] || p.id)));
  const assets = h("input", { type: "number", min: "0", step: "1", value: String(d.max_new_assets), "aria-label": "Новых ассетов" });
  const gpu = h("input", { type: "number", min: "0", step: "1", value: String(d.max_gpu_minutes), "aria-label": "Минут тяжёлых процессов" });
  const wall = h("input", { type: "number", min: "0", step: "1", value: String(d.max_wall_time_minutes), "aria-label": "Минут всего" });
  const narrative = h("input", { type: "checkbox", checked: d.allow_narrative_edits });
  const noGain = h("input", { type: "checkbox", checked: d.stop_when_no_material_gain });
  const roleBoxes = cfg.roles.map((r) => {
    const box = h("input", { type: "checkbox", checked: true, value: r.id });
    return h("label", { class: "check", title: r.about }, box, h("span", {}, r.label, h("span", { class: "muted" }, ` — ${KIND_LABEL[r.kind] || r.kind}`)));
  });
  const scenes = (ctx.story?.scenes || []).filter((s) => s.enabled !== false);
  const locks = scenes.map((s) => { const box = h("input", { type: "checkbox", value: s.id }); return h("label", { class: "check" }, box, h("span", {}, `${s.id} · ${s.intent}`)); });
  const error = h("p", { class: "form-error", role: "alert", hidden: true });
  const start = h("button", { type: "button", class: "btn btn--primary", disabled: busy || !ctx.project.output }, "Запустить ревью");
  start.addEventListener("click", async () => {
    const policy = {
      max_rounds: Number(rounds.value), mode: mode.value, review_profile: profile.value, max_new_assets: Number(assets.value),
      max_gpu_minutes: Number(gpu.value), max_wall_time_minutes: Number(wall.value), allow_narrative_edits: narrative.checked,
      stop_when_no_material_gain: noGain.checked,
      reviewer_roles: roleBoxes.map((l) => l.querySelector("input")).filter((b) => b.checked).map((b) => b.value),
      locked_scenes: locks.map((l) => l.querySelector("input")).filter((b) => b.checked).map((b) => b.value),
    };
    start.disabled = true;
    try {
      const run = await api("POST", `${ctx.base}/review-runs`, policy);
      await api("POST", `${ctx.base}/review-runs/${run.run_id}/start`);
      toast("Ревью запущено");
      resetReviewCache();
      await ctx.refresh();
    } catch (err) {
      error.textContent = err.message;
      error.hidden = false;
      start.disabled = false;
    }
  });
  const estimate = `Плановая стоимость: до ${d.max_rounds} раундов, каждый — анализ (секунды), пересборка изменившегося (≈ 0,3 × длительность ролика плюс 3 × длительность каждой перерисованной сцены) и QA.`;
  return h("div", { class: "stack" },
    h("div", { class: "review-grid" },
      h("label", { class: "field narrow" }, h("span", { class: "field-label" }, "Раундов (1–5)"), rounds),
      h("label", { class: "field" }, h("span", { class: "field-label" }, "Режим"), mode),
      h("label", { class: "field" }, h("span", { class: "field-label" }, "Профиль"), profile),
      h("label", { class: "field narrow" }, h("span", { class: "field-label" }, "Новых ассетов, макс."), assets),
      h("label", { class: "field narrow" }, h("span", { class: "field-label" }, "Минут тяжёлых процессов"), gpu),
      h("label", { class: "field narrow" }, h("span", { class: "field-label" }, "Минут всего"), wall),
    ),
    h("label", { class: "check" }, narrative, h("span", {}, "Разрешить правки речи (вырезки) ", h("span", { class: "muted" }, "— по умолчанию запрещено"))),
    h("label", { class: "check" }, noGain, h("span", {}, "Останавливаться, когда улучшения больше нет")),
    h("details", { class: "disclosure" }, h("summary", {}, `Роли-ревьюеры: ${cfg.roles.length}`), h("div", { class: "review-roles" }, ...roleBoxes)),
    locks.length ? h("details", { class: "disclosure" }, h("summary", {}, "Защитить сцены (не трогать)"), h("div", { class: "review-roles" }, ...locks)) : null,
    h("p", { class: "muted small" }, estimate),
    error,
    h("div", { class: "toolbar" }, start, ctx.project.output ? null : h("span", { class: "muted small" }, "Сначала собери ролик.")),
  );
}

const PROFILE_LABEL = { balanced: "сбалансированный", educational: "обучающий", dynamic_youtube: "динамичный YouTube", minimalist: "минимализм" };
const KIND_LABEL = { measure: "замеры", heuristic: "по манифестам, без VLM", llm: "LLM" };

// ---------- запуск ----------

function runView(ctx, run, busy) {
  const s = runSummary(run);
  const act = async (verb) => {
    try {
      await api("POST", `${ctx.base}/review-runs/${run.run_id}/${verb}`);
      resetReviewCache();
      await ctx.refresh();
    } catch (err) {
      ctx.setBanner(err.message, "error");
    }
  };
  const stop = async () => {
    const ok = await confirmAction({ title: "Остановить ревью?", body: "Текущая стадия прервётся; принятые версии останутся.", confirmLabel: "Остановить", danger: true });
    if (ok) await act("stop");
  };
  const budget = run.budget || {};
  return h("div", { class: "stack" },
    h("ul", { class: "facts" },
      h("li", {}, h("b", {}, s.round), h("span", { class: "muted" }, s.done ? ` · завершено ${s.done}` : "")),
      h("li", {}, h("b", {}, `${Math.round((budget.gpu_s || 0) / 60)} мин`), h("span", { class: "muted" }, ` тяжёлых процессов из ${run.policy.max_gpu_minutes}`)),
      h("li", {}, h("b", {}, String(budget.new_assets || 0)), h("span", { class: "muted" }, ` новых ассетов из ${run.policy.max_new_assets}`)),
      h("li", {}, h("b", {}, String(budget.llm_calls || 0)), h("span", { class: "muted" }, " вызовов LLM")),
      h("li", {}, h("b", {}, run.policy.mode), h("span", { class: "muted" }, ` · ${PROFILE_LABEL[run.policy.review_profile] || run.policy.review_profile}`)),
    ),
    s.blocking ? h("div", { class: "callout", role: "status" }, s.blocking) : null,
    s.stop ? h("p", { class: "muted small" }, `Остановка: ${s.stop}.`) : null,
    s.error ? h("p", { class: "job-error", role: "alert" }, s.error) : null,
    h("div", { class: "toolbar" },
      s.canStart ? h("button", { type: "button", class: "btn btn--primary", disabled: busy, onclick: () => act("start") }, "Начать") : null,
      s.canPause && !s.waitingPlan && !s.waitingDecision ? h("button", { type: "button", class: "btn btn--secondary", onclick: () => act("pause") }, "Пауза") : null,
      s.canResume ? h("button", { type: "button", class: "btn btn--primary", disabled: busy, onclick: () => act("resume") }, "Продолжить") : null,
      s.canStop ? h("button", { type: "button", class: "btn btn--link btn--danger", onclick: stop }, "Остановить") : null,
      run.final ? h("button", { type: "button", class: "btn btn--secondary", disabled: busy, onclick: () => { cache.runs = null; cache.run = null; cache.key = ""; ctx.rerender(); } }, "Новый запуск") : null,
    ),
    roundsList(ctx, run),
    cache.issues ? issuesSection(ctx, run, busy) : null,
    cache.plan && cache.plan.actions?.length ? planSection(ctx, run, busy) : null,
    cache.comparison && !cache.comparison.pending ? comparisonSection(ctx, run) : null,
    versionsSection(ctx, run, busy),
  );
}

function roundsList(ctx, run) {
  if (!run.rounds?.length) return h("p", { class: "muted small" }, "Раундов ещё нет.");
  return h("ol", { class: "rounds" }, ...run.rounds.map((r) => {
    const row = roundRow(r);
    const current = r.n === cache.round;
    return h("li", { class: `round-row${current ? " current" : ""}` },
      h("button", { type: "button", class: "round-title", "aria-pressed": current ? "true" : "false", onclick: () => { roundShown = r.n; cache.key = ""; ctx.refresh(); } }, row.title, " ", chip(row.decision, row.decisionTone)),
      h("div", { class: "round-stages" }, ...row.stages.map((st) => h("span", { class: `round-stage tone-${st.tone}`, title: st.status }, st.label))),
      h("span", { class: "muted small" }, row.issues, row.scenes ? ` · сцены: ${row.scenes}` : "", row.elapsed != null ? ` · ${row.elapsed} с` : ""),
      row.reason ? h("span", { class: "muted small" }, row.reason) : null,
    );
  }));
}

// ---------- замечания и решения ----------

function issuesSection(ctx, run, busy) {
  const data = cache.issues;
  const list = filterIssues(data.issues, filters);
  const opts = filterOptions(data.issues);
  const fps = ctx.story?.fps || 30;
  const select = (key, label, values, labels = {}) => {
    const el = h("select", { "aria-label": label, onchange: () => { filters[key] = el.value; ctx.rerender(); } },
      h("option", { value: "" }, label), ...values.map((v) => h("option", { value: v, selected: filters[key] === v }, labels[v] || v)));
    return el;
  };
  const reports = (data.reports || []).map((r) => h("span", { class: "chip tone-neutral", title: r.degraded_reason || "" },
    `${ROLE_LABELS(ctx)[r.role] || r.role}: ${r.issues}${r.degraded ? " ⚠" : ""}${r.status === "unavailable" ? " (нет LLM)" : ""}`));
  const waiting = run.state === "awaiting_approval" && cache.round === run.round;
  const rows = list.map((issue) => issueRow(ctx, run, issue, fps, waiting && !busy));
  const c = data.counts || {};
  return h("div", { class: "stack" },
    h("div", { class: "field-head" }, h("h3", {}, `Замечания раунда ${cache.round}: ${c.total || 0}`),
      h("div", { class: "toolbar" }, select("severity", "серьёзность", ["blocker", "major", "minor", "suggestion"], { blocker: "блокер", major: "серьёзно", minor: "мелочь", suggestion: "идея" }),
        select("scene", "сцена", opts.scenes), select("role", "роль", opts.roles, ROLE_LABELS(ctx)), select("status", "статус", ["proposed", "accepted", "rejected", "deferred", "fixed", "failed", "regressed", "invalid"]))),
    h("div", { class: "toolbar" }, ...reports),
    (data.reports || []).some((r) => r.degraded) ? h("p", { class: "muted small" }, "⚠ — роль без мультимодальной модели: судит по манифестам и замерам, кадры-улики — для твоей проверки.") : null,
    rows.length ? h("ul", { class: "issue-list" }, ...rows) : h("p", { class: "muted small" }, "Под фильтр ничего не попало."),
    waiting ? decisionsBar(ctx, run, busy) : null,
  );
}

function ROLE_LABELS(ctx) {
  return Object.fromEntries((ctx.config.review.roles || []).map((r) => [r.id, r.label]));
}

function issueRow(ctx, run, issue, fps, canDecide) {
  const card = issueCard(issue, fps);
  const frames = card.frames.map((f) => h("img", { class: "issue-frame", src: `${ctx.base}/review-runs/${run.run_id}/rounds/${cache.round}/bundle/${f}`, alt: `кадр-улика ${f}`, loading: "lazy" }));
  const choice = decisions[issue.issue_id] || "";
  const pick = (value) => { decisions[issue.issue_id] = value; ctx.rerender(); };
  const lockScene = issue.change?.scene_id ? h("button", { type: "button", class: "btn btn--link btn--sm", onclick: () => { decisions[`lock:${issue.change.scene_id}`] = true; pick("rejected"); } }, `защитить ${issue.change.scene_id}`) : null;
  return h("li", { class: `issue-item tone-${card.severityTone}${choice ? ` chosen-${choice}` : ""}` },
    h("div", { class: "issue-head" },
      chip(card.severity, card.severityTone), " ",
      h("button", { type: "button", class: "phrase-time", title: "Перейти к месту в ролике", onclick: () => ctx.seek(card.seconds) }, card.at), " ",
      h("b", {}, card.title), " ", chip(card.status, card.statusTone),
      card.degraded ? h("span", { class: "muted small", title: "без мультимодальной модели" }, " ⚠ проверь сам") : null),
    h("p", { class: "muted small" }, `${card.roles} · улика: ${card.evidence} · уверенность ${card.confidence}% · ${card.change}`,
      card.conflicts.length ? ` · конфликт с ${card.conflicts.join(", ")}` : "", card.reason ? ` · ${card.reason}` : ""),
    issue.impact || issue.proposed_change ? h("p", { class: "small" }, issue.impact ? `Чем плохо: ${issue.impact}. ` : "", issue.proposed_change ? `Что сделать: ${issue.proposed_change}. ` : "", issue.acceptance_check ? `Проверка: ${issue.acceptance_check}.` : "") : null,
    frames.length ? h("div", { class: "issue-frames" }, ...frames) : null,
    canDecide && card.canDecide ? h("div", { class: "toolbar" },
      h("button", { type: "button", class: `btn btn--sm ${choice === "accepted" ? "btn--primary" : "btn--secondary"}`, "aria-pressed": String(choice === "accepted"), onclick: () => pick("accepted") }, "Принять"),
      h("button", { type: "button", class: `btn btn--sm ${choice === "rejected" ? "btn--primary" : "btn--secondary"}`, "aria-pressed": String(choice === "rejected"), onclick: () => pick("rejected") }, "Отклонить"),
      h("button", { type: "button", class: `btn btn--sm ${choice === "deferred" ? "btn--primary" : "btn--secondary"}`, "aria-pressed": String(choice === "deferred"), onclick: () => pick("deferred") }, "Отложить"),
      lockScene) : null,
  );
}

function decisionsBar(ctx, run, busy) {
  const send = async (approveAll) => {
    const issues = Object.entries(decisions).filter(([k]) => !k.startsWith("lock:")).map(([issue_id, status]) => ({ issue_id, status }));
    const locked = Object.keys(decisions).filter((k) => k.startsWith("lock:")).map((k) => k.slice(5));
    try {
      await api("POST", `${ctx.base}/review-runs/${run.run_id}/rounds/${cache.round}/decisions`, { issues, locked_scenes: locked, approve_plan: approveAll });
      toast(approveAll ? "План утверждён — правки пошли" : "Решения записаны");
      decisions = {};
      resetReviewCache();
      await ctx.refresh();
    } catch (err) {
      ctx.setBanner(err.message, "error");
    }
  };
  const rejectAll = async () => {
    const ok = await confirmAction({ title: "Отклонить все правки раунда?", body: "Запуск завершится без пересборки.", confirmLabel: "Отклонить всё", danger: true });
    if (!ok) return;
    for (const i of cache.issues.issues) if (["proposed", "accepted", "deferred"].includes(i.status)) decisions[i.issue_id] = "rejected";
    await send(true);
  };
  return h("div", { class: "toolbar" },
    h("button", { type: "button", class: "btn btn--primary", disabled: busy, onclick: () => send(true) }, "Утвердить план"),
    h("button", { type: "button", class: "btn btn--secondary", disabled: busy || !Object.keys(decisions).length, onclick: () => send(false) }, "Записать решения"),
    h("button", { type: "button", class: "btn btn--link btn--danger", disabled: busy, onclick: rejectAll }, "Отклонить всё"),
    h("span", { class: "muted small" }, "Принятое исполняется, отклонённое — нет; без решения — как решит план."),
  );
}

// ---------- план, сравнение, версии ----------

function planSection(ctx, run, busy) {
  const plan = cache.plan;
  const rows = planRows(plan);
  const est = plan.estimate || {};
  return h("details", { class: "disclosure", open: run.state === "awaiting_approval" },
    h("summary", {}, `План исправлений: ${rows.length} действий · ≈${Math.round(est.render_s || 0)} с пересборки, ассетов ${est.assets || 0}${est.within_budget === false ? " · не в бюджете" : ""}`),
    h("ul", { class: "list" }, ...rows.map((r) => h("li", { class: "list-row list-row--static" },
      h("span", {}, r.label, " ", chip(r.status, r.tone), r.safe ? chip("безопасно", "success") : null, r.approval === "required" ? chip("нужен человек", "warning") : null),
      h("span", { class: "muted small" }, [r.cost, r.test, r.reason].filter(Boolean).join(" · "))))),
    run.state === "awaiting_approval" && cache.round === run.round ? h("div", { class: "toolbar" },
      h("button", { type: "button", class: "btn btn--primary", disabled: busy, onclick: async () => {
        try { await api("POST", `${ctx.base}/review-runs/${run.run_id}/rounds/${cache.round}/execute`); resetReviewCache(); await ctx.refresh(); } catch (err) { ctx.setBanner(err.message, "error"); }
      } }, "Исполнить план целиком")) : null,
  );
}

function comparisonSection(ctx, run) {
  const v = comparisonView(cache.comparison);
  if (!v) return null;
  const decide = async (decision) => {
    try {
      await api("POST", `${ctx.base}/review-runs/${run.run_id}/rounds/${cache.round}/decide`, { decision });
      resetReviewCache();
      await ctx.refresh();
    } catch (err) {
      ctx.setBanner(err.message, "error");
    }
  };
  const waiting = run.state === "awaiting_decision" && cache.round === run.round;
  return h("section", { class: "stack" },
    h("div", { class: "field-head" }, h("h3", {}, `Сравнение ${v.baseline} → ${v.candidate}`), chip(`${v.decision} · ${v.decidedBy}`, v.tone)),
    h("p", { class: "small" }, v.reason),
    h("div", { class: "compare-grid" },
      h("div", {}, h("h4", {}, "Hard gates"), h("ul", { class: "qa-list" }, ...v.gates.map((g) => h("li", { class: `qa-item ${g.ok ? "qa-info" : "qa-error"}` }, g.ok ? "✓ " : "✗ ", g.id, h("span", { class: "muted small" }, ` ${g.detail}`))))),
      h("div", {}, h("h4", {}, "Объективно"), h("table", { class: "data-table small" }, h("thead", {}, h("tr", {}, h("th", {}, "метрика"), h("th", {}, v.baseline), h("th", {}, v.candidate))),
        h("tbody", {}, ...v.metrics.map((m) => h("tr", { class: `tone-${m.tone}` }, h("td", {}, m.key), h("td", {}, String(m.a)), h("td", {}, String(m.b))))))),
      h("div", {}, h("h4", {}, `Субъективно (${v.subjectiveMode === "judge" ? `судья, профиль ${v.profile}` : v.subjectiveMode === "human_review" ? "без LLM — оцени сам" : "пропущено"})`),
        v.subjective.length ? h("table", { class: "data-table small" }, h("thead", {}, h("tr", {}, h("th", {}, "измерение"), h("th", {}, "A"), h("th", {}, "B"), h("th", {}, "вес"), h("th", {}, "улика"))),
          h("tbody", {}, ...v.subjective.map((s) => h("tr", {}, h("td", {}, s.dimension), h("td", {}, String(s.a)), h("td", {}, String(s.b)), h("td", {}, String(s.weight)), h("td", { class: "small" }, s.evidence))))) : null,
        v.delta != null ? h("p", { class: "muted small" }, `Взвешенная дельта: ${v.delta.toLocaleString("ru-RU", { signDisplay: "always" })}`) : null),
    ),
    v.diff ? h("p", { class: "muted small" }, `Изменились сцены: ${v.diff.scenes_changed.join(", ") || "—"}; ролик ${v.diff.output_changed ? "другой" : "тот же"}; QA Δ ошибок ${v.diff.qa_delta.error}, предупреждений ${v.diff.qa_delta.warning}.`) : null,
    abPlayer(ctx, run, v.baseline, v.candidate),
    waiting ? h("div", { class: "toolbar" },
      h("button", { type: "button", class: "btn btn--primary", onclick: () => decide("accept") }, `Принять ${v.candidate}`),
      h("button", { type: "button", class: "btn btn--secondary", onclick: () => decide("reject") }, `Откатить к ${v.baseline}`)) : null,
  );
}

function abPlayer(ctx, run, a, b) {
  const { labels } = abPair(run, cache.versions, null);
  const selA = h("select", { "aria-label": "Версия A" }, ...labels.map((l) => h("option", { value: l, selected: l === a }, l)));
  const selB = h("select", { "aria-label": "Версия B" }, ...labels.map((l) => h("option", { value: l, selected: l === b }, l)));
  const va = h("video", { class: "player ab", preload: "metadata", controls: true, playsinline: true, muted: true });
  const vb = h("video", { class: "player ab", preload: "metadata", controls: true, playsinline: true });
  const src = (v, label) => { v.src = `${ctx.base}/versions/${label}/video`; };
  src(va, a); src(vb, b);
  selA.addEventListener("change", () => src(va, selA.value));
  selB.addEventListener("change", () => src(vb, selB.value));
  let master = vb;
  const sync = (from, to) => { const t = abSync(from.currentTime, to.currentTime); if (t != null) to.currentTime = t; };
  for (const [x, y] of [[va, vb], [vb, va]]) {
    x.addEventListener("play", () => { master = x; y.play().catch(() => {}); sync(x, y); });
    x.addEventListener("pause", () => { if (master === x) y.pause(); });
    x.addEventListener("seeked", () => sync(x, y));
    x.addEventListener("timeupdate", () => { if (master === x) sync(x, y); });
  }
  const toggle = h("button", { type: "button", class: "btn btn--secondary btn--sm", "aria-pressed": "false" }, "Показать только B");
  toggle.addEventListener("click", () => { const only = toggle.getAttribute("aria-pressed") !== "true"; toggle.setAttribute("aria-pressed", String(only)); va.hidden = only; toggle.textContent = only ? "Показать A и B" : "Показать только B"; });
  return h("div", { class: "stack" },
    h("div", { class: "toolbar" }, h("label", { class: "field narrow" }, h("span", { class: "field-label" }, "A"), selA), h("label", { class: "field narrow" }, h("span", { class: "field-label" }, "B"), selB), toggle,
      h("span", { class: "muted small" }, "Плееры идут синхронно: play, пауза и перемотка в одном повторяются в другом.")),
    h("div", { class: "ab-grid" }, va, vb),
  );
}

function versionsSection(ctx, run, busy) {
  const rows = (cache.versions || []).map(versionRow);
  const restore = async (label) => {
    const ok = await confirmAction({ title: `Вернуть ${label} в проект?`, body: "Манифесты и ролик копируются из версии; текущие файлы перезапишутся (сами версии не меняются).", confirmLabel: "Вернуть" });
    if (!ok) return;
    try {
      await api("POST", `${ctx.base}/review-runs/${run.run_id}/select-revision`, { version: label });
      toast(`В проекте — ${label}`);
      resetReviewCache();
      await ctx.refresh();
    } catch (err) {
      ctx.setBanner(err.message, "error");
    }
  };
  return h("details", { class: "disclosure" }, h("summary", {}, `Версии: ${rows.length} · в проекте ${run.selected_version || run.accepted_version}`),
    h("ul", { class: "list" }, ...rows.map((r) => h("li", { class: "list-row" },
      h("span", {}, h("b", {}, r.label), " ", chip(r.status, r.tone), h("span", { class: "muted small" }, ` раунд ${r.round} · ${clock(r.duration)} · ${(r.size / 2 ** 20).toFixed(0)} МБ · ${r.sha}${r.reason ? ` · ${r.reason}` : ""}`)),
      h("span", { class: "toolbar" },
        h("a", { class: "btn btn--link btn--sm", href: `${ctx.base}/versions/${r.label}/video` }, "смотреть"),
        h("button", { type: "button", class: "btn btn--link btn--sm", disabled: busy || run.active, onclick: () => restore(r.label) }, "вернуть в проект"))))),
    h("p", { class: "muted small" }, "Экспорт выбранной версии: верни её в проект и нажми «Экспортировать» ниже — provenance запомнит версию."));
}
