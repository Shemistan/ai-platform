// Чистые функции экранов MASTER_SPEC «Монтажа»: раскадровка, таймлайн, QA, ворота — без DOM, тесты в node.

import { clock } from "./plan.js";

export const STATUS_LABEL = {
  proposed: "предложена",
  approved: "утверждена",
  rendered: "отрисована",
  blocked: "заблокирована",
  failed: "не отрисовалась",
  disabled: "выключена",
};
export const STATUS_TONE = { proposed: "neutral", approved: "info", rendered: "success", blocked: "warning", failed: "danger", disabled: "neutral" };
export const GATE_LABEL = { edl: "план вырезок", storyboard: "раскадровка", export: "экспорт" };

// Кадры → секунды результата и «м:сс».
export const frameClock = (frame, fps) => clock((frame || 0) / (fps || 30));

// Карточка сцены для сетки: подписи, время, оценка.
export function sceneCard(scene, templates, fps) {
  const t = (templates || []).find((x) => x.id === scene.template_id);
  const score = scene.score ?? 0;
  return {
    id: scene.id,
    title: scene.intent || scene.id,
    template: t ? t.label : scene.template_id,
    at: frameClock(scene.edit_start_frame, fps),
    seconds: Math.round((scene.duration_frames / (fps || 30)) * 10) / 10,
    status: scene.status,
    statusLabel: STATUS_LABEL[scene.status] || scene.status,
    tone: STATUS_TONE[scene.status] || "neutral",
    enabled: scene.enabled !== false,
    score: `${score}/25`,
    scoreTone: score >= 20 ? "success" : score >= 16 ? "neutral" : "warning",
    rationale: scene.rationale || "",
    claims: scene.grounding_claim_ids || [],
    error: scene.error || "",
    thumb: scene.status === "rendered",
  };
}

// Правка сцены из формы: секунды → кадры; ошибка текстом.
export function scenePatch(scene, { start, seconds, enabled }, fps, totalFrames) {
  const patch = { id: scene.id };
  if (enabled !== undefined) patch.enabled = !!enabled;
  if (start !== undefined && start !== "") {
    const s = parseClock(start);
    if (s === null) return { error: "Начало — время вида м:сс или секунды." };
    patch.edit_start_frame = Math.round(s * fps);
  }
  if (seconds !== undefined && seconds !== "") {
    const d = Number(String(seconds).replace(",", "."));
    if (!Number.isFinite(d) || d < 2.5 || d > 12) return { error: "Длительность сцены — от 2,5 до 12 секунд." };
    patch.duration_frames = Math.round(d * fps);
  }
  const startFrame = patch.edit_start_frame ?? scene.edit_start_frame;
  const frames = patch.duration_frames ?? scene.duration_frames;
  if (totalFrames && startFrame + frames > totalFrames) return { error: "Сцена выходит за конец ролика." };
  return { patch };
}

// «1:23», «83», «1:02:05» → секунды; null — не время.
export function parseClock(text) {
  const parts = String(text ?? "").trim().replace(",", ".").split(":");
  if (!parts.length || parts.some((p) => p === "" || !/^\d+(\.\d+)?$/.test(p))) return null;
  return parts.reduce((acc, p) => acc * 60 + Number(p), 0);
}

// Биты списком: время, вид, цель, утверждения.
export const PURPOSE_LABEL = { hook: "зацепка", setup: "контекст", explanation: "объяснение", demo: "показ", recap: "итог", cta: "призыв", other: "прочее" };

export function beatRows(beats, fps) {
  return (beats || []).map((b) => ({
    id: b.id,
    at: frameClock(b.start_frame, fps),
    purpose: PURPOSE_LABEL[b.purpose] || b.purpose,
    goal: b.goal,
    priority: b.priority,
    claims: (b.claims || []).length,
    cut: b.end_frame <= b.start_frame,
  }));
}

// Полосы дорожек таймлайна: left/width в процентах от длины ролика.
export function trackBars(timeline) {
  const total = timeline?.duration_frames || 1;
  return (timeline?.tracks || []).map((track) => ({
    id: track.id,
    label: { presenter: "Голова", screen: "Экран", overlays: "Сцены", captions: "Субтитры", dialogue: "Речь" }[track.id] || track.id,
    count: track.clips.length,
    bars: track.clips.map((c) => ({
      id: c.id,
      left: Math.max(0, Math.min(100, (100 * c.edit_in_frame) / total)),
      width: Math.max(0.3, (100 * (c.edit_out_frame - c.edit_in_frame)) / total),
      enabled: c.enabled !== false,
      label: c.text || c.layout || c.scene_id || c.id,
      scene: c.scene_id || null,
    })),
  }));
}

// QA по серьёзности: ошибки, предупреждения, сведения.
export function qaGroups(items) {
  const out = { error: [], warning: [], info: [] };
  for (const item of items || []) (out[item.severity] || out.info).push(item);
  return out;
}

export const GATE_TONE = { pass: "success", fail: "danger", human_review: "warning", none: "neutral" };
export const GATE_TEXT = { pass: "пройдено", fail: "не пройдено", human_review: "нужен взгляд человека", none: "не проверялось" };

// Что показать у ворот: нужно ли подтверждение сейчас.
export function gateState(project, gate) {
  const g = project?.gates?.[gate];
  if (!g) return { show: false };
  const ready = { edl: !!project.transcript, storyboard: !!project.storyboard, export: !!project.qa }[gate];
  return {
    show: g.required && ready,
    approved: g.approved,
    auto: g.auto,
    text: g.approved ? (g.auto ? "подтверждено автономно" : "подтверждено") : `ждёт подтверждения: ${GATE_LABEL[gate]}`,
  };
}
