// Этапы работы «Монтажа» (PRD-025): строки списка и сводка «прошло / осталось» — без DOM.
// Данные — project.stages и project.eta_s с сервера (montage/stages.py); now — секунды эпохи.

// Длительность для людей: 40 → «40 с», 750 → «12 мин», 3720 → «1 ч 2 мин».
export function span(value) {
  if (value === null || value === undefined) return "";
  const s = Math.max(0, Math.round(value));
  if (s < 60) return `${s} с`;
  const m = Math.round(s / 60);
  return m < 60 ? `${m} мин` : `${Math.floor(m / 60)} ч${m % 60 ? ` ${m % 60} мин` : ""}`;
}

// [{ id, label, status, percent, detail, note }]: note — сколько заняло (готово), сколько
// осталось (идёт), «пропущен» / «упал» / «отменён».
export function stageRows(stages, now) {
  return (stages || []).map((s) => {
    let note = "";
    if (s.status === "done" && s.started_at && s.finished_at) note = span(s.finished_at - s.started_at);
    else if (s.status === "running") {
      const went = s.started_at ? span(now - s.started_at) : "";
      note = s.eta_s != null ? `${went} · осталось ≈ ${span(s.eta_s)}` : went;
    } else if (s.status === "skipped") note = "пропущен";
    else if (s.status === "failed") note = "упал";
    else if (s.status === "canceled") note = "отменён";
    return { id: s.id, label: s.label, status: s.status, percent: Math.round(s.percent || 0), detail: s.detail || "", note };
  });
}

// Сводка над списком: этап N из M, прошло с начала действия, осталось всего.
export function summary(project, now) {
  const stages = project.stages || [];
  const started = stages.map((s) => s.started_at).filter(Boolean);
  const index = stages.findIndex((s) => s.status === "running");
  return {
    step: index >= 0 ? `Этап ${index + 1} из ${stages.length}` : "",
    elapsed: started.length ? span(now - Math.min(...started)) : "",
    left: project.eta_s != null ? `осталось ≈ ${span(project.eta_s)}` : stages.length ? "оценка появится по ходу" : "",
  };
}
