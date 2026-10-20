// Панель «Ревью и улучшение» (ITERATIVE_REVIEW_SPEC §11, PRD-028): чистые функции без DOM.
// Данные — запуск (review_run), замечания (review_issue), план (repair_plan), сравнение (review_comparison).

export const STATE_LABEL = {
  idle: "не запущено", preparing: "бандл", reviewing: "роли смотрят", consolidating: "сводим замечания",
  awaiting_approval: "ждёт утверждения плана", repairing: "правки", rendering: "пересборка", validating: "проверка",
  comparing: "сравнение версий", awaiting_decision: "ждёт решения по версии", accepted: "версия принята",
  rejected: "версия отклонена", completed: "завершено", paused: "пауза", cancelled: "остановлено", failed: "ошибка",
  blocked: "заблокировано",
};
export const STATE_TONE = {
  completed: "success", accepted: "success", failed: "danger", blocked: "danger", cancelled: "neutral",
  awaiting_approval: "warning", awaiting_decision: "warning", paused: "warning", rejected: "warning", idle: "neutral",
};
export const SEVERITY_LABEL = { blocker: "блокер", major: "серьёзно", minor: "мелочь", suggestion: "идея" };
export const SEVERITY_TONE = { blocker: "danger", major: "danger", minor: "warning", suggestion: "neutral" };
export const ISSUE_STATUS = {
  proposed: "предложено", accepted: "принято", rejected: "отклонено", deferred: "отложено", fixed: "исправлено",
  failed: "не исправилось", regressed: "вернулось", invalid: "негодное",
};
export const EVIDENCE_LABEL = {
  observed_video: "кадры", observed_audio: "звук", transcript: "слова", source_document: "материалы", heuristic: "эвристика",
};
export const STOP_LABEL = {
  max_rounds: "дошли до лимита раундов", user: "остановлено человеком", budget: "бюджет исчерпан",
  no_material_gain: "улучшения больше нет", repeated_issues: "замечания повторяются", oscillation: "правки ходят туда-обратно",
  no_executable_actions: "исполнимых правок нет", no_approved_actions: "правки не утверждены",
};
export const ROUND_STAGES = ["analyze", "consolidate", "approve", "repair", "render", "validate", "compare"];
export const ROUND_STAGE_LABEL = {
  analyze: "анализ", consolidate: "сводка", approve: "утверждение", repair: "правки", render: "пересборка",
  validate: "проверка", compare: "сравнение",
};

// Отфильтровать и упорядочить замечания: severity → уверенность; фильтры по severity, сцене, роли, статусу.
export function filterIssues(issues, { severity = "", scene = "", role = "", status = "" } = {}) {
  const order = ["blocker", "major", "minor", "suggestion"];
  return (issues || [])
    .filter((i) => !severity || i.severity === severity)
    .filter((i) => !scene || (i.scene_ids || []).includes(scene))
    .filter((i) => !role || (i.reviewer_roles || [i.reviewer_role]).includes(role))
    .filter((i) => !status || i.status === status)
    .sort((a, b) => order.indexOf(a.severity) - order.indexOf(b.severity) || b.confidence - a.confidence);
}

// Значения для селектов фильтров из самих замечаний.
export function filterOptions(issues) {
  const scenes = new Set();
  const roles = new Set();
  for (const i of issues || []) {
    for (const s of i.scene_ids || []) scenes.add(s);
    for (const r of i.reviewer_roles || [i.reviewer_role]) roles.add(r);
  }
  return { scenes: [...scenes].sort(), roles: [...roles].sort() };
}

// Карточка замечания: подписи, таймкод, улики-кадры (по round_id — путь к бандлу).
export function issueCard(issue, fps = 30) {
  const seconds = issue.start_frame / fps;
  const frames = (issue.evidence || []).filter((e) => e.artifact_id && /^frames\//.test(e.artifact_id)).map((e) => e.artifact_id);
  const canDecide = ["proposed", "deferred", "accepted"].includes(issue.status);
  return {
    id: issue.issue_id,
    title: issue.observation,
    severity: SEVERITY_LABEL[issue.severity] || issue.severity,
    severityTone: SEVERITY_TONE[issue.severity] || "neutral",
    status: ISSUE_STATUS[issue.status] || issue.status,
    statusTone: issue.status === "fixed" ? "success" : ["failed", "regressed", "invalid"].includes(issue.status) ? "danger" : "neutral",
    roles: (issue.reviewer_roles || [issue.reviewer_role]).join(", "),
    at: clock(seconds),
    seconds,
    evidence: EVIDENCE_LABEL[issue.evidence_type] || issue.evidence_type,
    degraded: !!issue.degraded,
    frames,
    change: changeLabel(issue.change),
    confidence: Math.round((issue.confidence || 0) * 100),
    reason: issue.status_reason || "",
    canDecide,
    conflicts: issue.conflicts_with || [],
  };
}

export function changeLabel(change) {
  if (!change || change.op === "none") return "без автоправки";
  const s = change.scene_id ? ` ${change.scene_id}` : "";
  const f = change.frames != null ? ` на ${Math.abs(change.frames)} кадров` : "";
  return {
    scene_extend: `удлинить${s}${f}`, scene_shorten: `укоротить${s}${f}`, scene_shift: `сдвинуть${s}${f}`,
    scene_disable: `выключить${s}`, scene_props: `переделать props${s}`,
    caption_words: `субтитры до ${change.max_words} слов`, cut_words: `вырезать слова ${change.from}–${change.to}`,
  }[change.op] || change.op;
}

export function clock(seconds) {
  const s = Math.max(0, Math.round(seconds || 0));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

// Полоса раунда: стадии со статусом и подписью.
export function roundRow(round) {
  const stages = (round.stages || []).map((s) => ({
    id: s.id, label: ROUND_STAGE_LABEL[s.id] || s.id, status: s.status,
    tone: s.status === "done" ? "success" : s.status === "awaiting" ? "warning" : ["failed", "blocked"].includes(s.status) ? "danger" : "neutral",
  }));
  const c = round.counts || {};
  return {
    n: round.n,
    title: `Раунд ${round.n} · ${round.base_version}${round.candidate_version ? ` → ${round.candidate_version}` : ""}`,
    stages,
    issues: `замечаний ${c.total || 0}: открыто ${c.open || 0}, принято ${c.accepted || 0}, отклонено ${c.rejected || 0}, исправлено ${c.fixed || 0}`,
    scenes: (round.scenes_touched || []).join(", "),
    decision: round.decision ? decisionLabel(round.decision) : "идёт",
    decisionTone: round.decision === "accepted" ? "success" : round.decision === "rejected" ? "warning" : "neutral",
    reason: round.decision_reason || "",
    elapsed: round.finished_at && round.started_at ? Math.round(round.finished_at - round.started_at) : null,
  };
}

export function decisionLabel(decision) {
  return { accepted: "принят", rejected: "откат", no_actions: "правок нет", blocked: "блокер", deferred: "отложено" }[decision] || decision;
}

// Сводка запуска для обзора: раунд N / max, состояние, причина, версии, бюджет.
export function runSummary(run) {
  if (!run) return null;
  return {
    state: STATE_LABEL[run.state] || run.state,
    tone: STATE_TONE[run.state] || (run.active ? "running" : "neutral"),
    round: `раунд ${run.round || 0} / ${run.rounds_planned}`,
    done: run.rounds_done,
    stop: run.stop_reason ? STOP_LABEL[run.stop_reason] || run.stop_reason : "",
    blocking: run.blocking_reason || "",
    versions: run.versions || [],
    accepted: run.accepted_version,
    selected: run.selected_version,
    budget: run.budget || {},
    canPause: run.active || run.waiting,
    canResume: run.state === "paused",
    canStop: !run.final,
    canStart: run.state === "idle",
    waitingPlan: run.state === "awaiting_approval",
    waitingDecision: run.state === "awaiting_decision",
    error: run.error || "",
  };
}

// Строки плана: действие, оценка, что ждёт человека.
export function planRows(plan) {
  return (plan?.actions || []).map((a) => ({
    id: a.action_id,
    label: changeLabel({ op: a.op, scene_id: a.scene_id, ...a.params }),
    status: a.status,
    tone: a.status === "done" ? "success" : ["failed", "rejected"].includes(a.status) ? "danger" : a.status === "approved" ? "success" : "neutral",
    approval: a.approval,
    safe: a.safe,
    cost: a.estimate ? `≈${Math.round(a.estimate.render_s)} с${a.estimate.assets ? `, ассетов ${a.estimate.assets}` : ""}` : "",
    reason: a.status_reason || a.error || "",
    test: a.test || "",
    issues: a.issue_ids || [],
  }));
}

// Таблица сравнения: hard gates, объективные метрики (хуже/лучше), субъективные измерения — раздельно.
export function comparisonView(cmp) {
  if (!cmp || cmp.pending) return null;
  const obj = cmp.objective || { baseline: {}, candidate: {} };
  const metrics = Object.keys(obj.baseline || {}).map((k) => ({
    key: k, a: obj.baseline[k], b: obj.candidate?.[k],
    tone: (obj.worse || []).includes(k) ? "danger" : (obj.better || []).includes(k) ? "success" : "neutral",
  }));
  const subj = cmp.subjective || {};
  return {
    decision: cmp.decision, decidedBy: cmp.decided_by, reason: cmp.reason,
    tone: cmp.decision === "accepted" ? "success" : cmp.decision === "rejected" ? "warning" : "neutral",
    gates: (cmp.hard_gates || []).map((g) => ({ id: g.id, ok: g.ok, detail: g.detail })),
    hardOk: cmp.hard_ok,
    metrics,
    subjective: (subj.items || []).map((i) => ({ dimension: i.dimension, a: i.a, b: i.b, evidence: i.evidence, weight: i.weight })),
    subjectiveMode: subj.mode, delta: subj.weighted_delta, profile: subj.profile,
    fixed: cmp.fixed_issues || [], regressed: cmp.regressed_issues || [],
    diff: cmp.diff || null,
    baseline: cmp.baseline, candidate: cmp.candidate,
  };
}

// Синхронное A/B-воспроизведение: какое время выставить второму плееру и нужно ли переключить активный.
export function abSync(masterTime, slaveTime, tolerance = 0.15) {
  return Math.abs(masterTime - slaveTime) > tolerance ? masterTime : null;
}

// Что выбрать для A/B: baseline и кандидат раунда или две любые версии.
export function abPair(run, versions, round) {
  const labels = (versions || []).map((v) => v.label);
  const a = round?.base_version || run?.accepted_version || labels[0];
  const b = round?.candidate_version || labels.find((l) => l !== a) || a;
  return { a, b, labels };
}

export function versionRow(v) {
  return {
    label: v.label, round: v.round, accepted: v.accepted,
    status: v.accepted === true ? "принята" : v.accepted === false ? "отклонена" : "кандидат",
    tone: v.accepted === true ? "success" : v.accepted === false ? "warning" : "neutral",
    reason: v.reason || "", size: v.output?.size || 0, duration: v.output?.duration || 0, sha: (v.output?.sha256 || "").slice(0, 12),
  };
}
