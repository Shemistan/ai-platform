// Центр активности шапки (UX-036): подписи из /api/status — без DOM, тесты в node.
// status: { activity: [{ tab, href, title, status, stage, percent }], gpu, waiting }.

export function activitySummary({ activity = [], gpu = null, waiting = 0 } = {}) {
  const count = activity.length;
  return {
    count,
    label: count ? `Задачи: ${count}` : "Задач нет",
    // Имя владельца замка — вкладка («Фото», «Монтаж»): одна тяжёлая модель за раз (ADR 0004).
    gpu: gpu ? `Видеокарту занимает «${gpu}»${waiting ? ` · ждут очереди: ${waiting}` : ""}` : "Видеокарта свободна",
  };
}

// Строка задачи под названием: «В очереди» или «Шаг 3/9 · 33%».
export function activityLine(item) {
  if (item.status === "queued") return item.stage && item.stage !== "В очереди" ? `в очереди · ${item.stage}` : "в очереди";
  const percent = typeof item.percent === "number" ? `${Math.round(item.percent)}%` : "";
  return [item.stage, percent].filter(Boolean).join(" · ") || "идёт";
}
