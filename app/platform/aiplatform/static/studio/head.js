// Студия: шапка выпуска — главное действие по состоянию и степпер шагов.
import { busyClick, h, moreMenu, STATUS, statusChip, toneOf } from "/ui.js";
import { headPlan, state, visibleSteps } from "./state.js";
import { cloneProject, makeFinal } from "./create.js";
import { publishNow, publishPick } from "./publish.js";
import { goToStep, rerunStep } from "./steps.js";
import { act, reloadList } from "./app.js";

const UPLOADS = {
  poster: ["poster_mode", "Загрузить постер"],
  images: ["images_mode", "Загрузить картинки"],
  clips: ["clips_mode", "Загрузить ролики"],
  voice: ["voice_mode", "Загрузить озвучку"],
};

// Что выпуску нужно дальше: { label, run, runAll } или null, если делать нечего.
function nextAction(project) {
  if (project.running) return null;
  const steps = visibleSteps(project);
  const need = steps.find((s) => s.status === "waiting" || s.status === "failed");
  if (need?.status === "failed") return { label: `Повторить шаг ${steps.indexOf(need) + 1}: ${need.label}`, run: () => rerunStep(need.id) };
  if (need) {
    if (need.id === "publish") {
      const blocked = project.review?.verdict === "не готово" && !publishPick(project).ignoreVerdict;
      return blocked ? { label: "К публикации: судья против", run: () => goToStep("publish") } : { label: "Опубликовать…", run: publishNow };
    }
    if (need.id === "text" && project.settings.review_text) return { label: "Текст ок, продолжить", run: () => act("approve", { step: "text" }) };
    const [mode, label] = UPLOADS[need.id] || [];
    if (mode && project.settings[mode] === "upload") return { label, run: () => goToStep(need.id) };
    return { label: `К шагу «${need.label}»`, run: () => goToStep(need.id) };
  }
  if (steps.some((s) => s.status === "pending")) return { label: "Запустить всё", run: () => act("run", {}), runAll: true };
  return null;
}

function overallStatus(project) {
  const steps = project.steps;
  if (project.running) return "running";
  if (steps.some((s) => s.status === "failed")) return "failed";
  if (steps.some((s) => s.status === "waiting")) return "waiting";
  return steps.every((s) => s.status === "done" || s.status === "skipped") ? "done" : "pending";
}

// Действия шапки по id из headPlan: подпись и что делать.
function headActions(project, next) {
  return {
    next: next && { label: next.label, run: next.run },
    run_all: { label: "Запустить всё", run: () => act("run", {}) },
    make_final: { label: "Сделать чистовик", run: makeFinal },  // дорисовать картинки и ролики, собрать в полном кадре
    cancel: { label: "Остановить", run: () => act("cancel") },
    clone: { label: "Копия выпуска", run: cloneProject },  // те же настройки и результаты до сборки — другой вариант
    archive: {
      label: project.archived ? "Вернуть из архива" : "В архив",
      run: async () => (await act("archive", { archived: !state.project.archived })) && reloadList(),
    },
  };
}

// Шапка: статус, одна главная кнопка, остальное — в «⋯» (DS-019).
export function renderHead(project) {
  const next = nextAction(project);
  const plan = headPlan(project, next);
  const actions = headActions(project, next);
  const button = (id, kind) => h("button", { type: "button", class: `btn btn--${kind}`, onclick: busyClick(actions[id].run) }, actions[id].label);
  return h(
    "div",
    { class: "panel project-head" },
    h("h2", {}, project.title),
    statusChip(overallStatus(project)),
    project.settings.draft ? h("span", { class: "chip tone-warning" }, "черновик") : null,
    project.archived ? h("span", { class: "chip tone-neutral" }, "в архиве") : null,
    ...plan.visible.map((id) => button(id, "secondary")),
    plan.primary ? button(plan.primary, "primary") : null,
    moreMenu(plan.menu.map((id) => ({ label: actions[id].label, onClick: busyClick(actions[id].run) })), { label: "Ещё действия с выпуском" }),
    h("p", { class: "form-error", id: "project-error", role: "alert", hidden: true }),
  );
}

const MARKS = { done: "✓", skipped: "↷", running: "●", waiting: "!", failed: "✗", pending: "○" };

// Карта пайплайна: липкая строка шагов со статусами, клик — к шагу.
export function renderStepper(project) {
  const steps = visibleSteps(project);
  return h(
    "nav",
    { class: "stepper", "aria-label": "Шаги выпуска" },
    h(
      "ol",
      {},
      ...steps.map((s, i) =>
        h(
          "li",
          {},
          h(
            "button",
            { type: "button", class: `stepper-item ${s.status} tone-${toneOf(s.status)}`, title: STATUS[s.status] || s.status, onclick: () => goToStep(s.id) },
            h("span", { class: "mark", "aria-hidden": "true" }, MARKS[s.status] || "○"),
            h("span", {}, `${i + 1}. ${s.label}`),
            h("span", { class: "sr-only" }, `: ${STATUS[s.status] || s.status}`),
          ),
        ),
      ),
    ),
  );
}
