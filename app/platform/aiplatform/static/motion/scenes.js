// «Моушн» (PRD-026): чистые функции экрана сцен — без DOM, тестируются node --test.

// «12:30», «1:02:05», «750» → секунды; null — не разобрать.
export function parseClock(text) {
  const raw = String(text ?? "").trim();
  if (!/^\d+(?::\d{1,2}){0,2}(?:[.,]\d+)?$/.test(raw)) return null;
  const parts = raw.replace(",", ".").split(":").map(Number);
  if (parts.slice(1).some((n) => n >= 60)) return null;
  return parts.reduce((total, n) => total * 60 + n, 0);
}

// Секунды → «М:СС» или «Ч:ММ:СС».
export function clock(seconds) {
  const s = Math.max(0, Math.floor(seconds || 0));
  const mm = String(Math.floor(s / 60) % 60).padStart(2, "0");
  const ss = String(s % 60).padStart(2, "0");
  return s >= 3600 ? `${Math.floor(s / 3600)}:${mm}:${ss}` : `${Math.floor(s / 60)}:${ss}`;
}

const STEP = {
  storyboard: "режиссёр ставит сцену",
  review: "техред проверяет",
  animate: "аниматор пишет код",
  render: "рендер",
  preview: "превью",
};
const STATUS = {
  queued: ["в очереди", "neutral"],
  working: ["в работе", "info"],
  done: ["готово", "success"],
  failed: ["не получилась", "danger"],
  dropped: ["снята техредом", "warning"],
  canceled: ["отменена", "neutral"],
};

// Подпись и тон статуса сцены: в работе — какая роль сейчас.
export function sceneStatus(scene) {
  if (scene.status === "working" && STEP[scene.step]) return { label: STEP[scene.step], tone: "info" };
  const [label, tone] = STATUS[scene.status] || [scene.status, "neutral"];
  return { label: scene.status === "done" && scene.enabled === false ? "выключена" : label, tone };
}

// Сводка над сценами: готово, в пакете, снято, упало.
export function sceneSummary(scenes) {
  const list = scenes || [];
  const done = list.filter((s) => s.status === "done");
  return {
    total: list.length,
    done: done.length,
    packed: done.filter((s) => s.enabled !== false).length,
    dropped: list.filter((s) => s.status === "dropped").length,
    failed: list.filter((s) => s.status === "failed").length,
  };
}

// Сцены по времени ролика.
export function byTime(scenes) {
  return [...(scenes || [])].sort((a, b) => a.at - b.at);
}
