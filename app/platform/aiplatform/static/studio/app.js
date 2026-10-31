// Сценарии «Дайджест новостей», «Видео по статье» и «Пост в канал»: выпуск — цепочка шагов от текста до публикации.
// Каждый шаг виден, его результат можно поправить, загрузить свой или перезапустить шаг.
// Сборка страницы: действия над выпуском, отрисовка по блокам и опрос. Зоны — в соседних модулях (FE-010).
import { $, announce, api, busy as busyRequest, captureFocus, debounce, h, loadLast, logBlock, poller, reconcile, reportError, restoreFocus, safe, STATUS, statusChip } from "/ui.js";
import { API, HINTS, scenario, state, visibleSteps } from "./state.js";
import { renderSettings } from "./settings.js";
import { checkBox, create, longTextActions, refreshCreateCheck, renderCreate } from "./create.js";
import { publishPick } from "./publish.js";
import { STEP_DATA, collapsed, renderStep } from "./steps.js";
import { renderHead, renderStepper } from "./head.js";

export async function loadProjects() {
  const { projects } = await api("GET", `${API}/projects?scenario=${scenario}`);
  state.projects = projects;
  $("projects").replaceChildren(
    ...(projects.length
      ? projects.map((p) =>
          h(
            "li",
            {},
            h(
              "button",
              { type: "button", class: `history-item${state.project?.id === p.id ? " current" : ""}`, onclick: () => open(p.id) },
              h("span", { class: "history-text" }, h("span", { class: "history-title" }, p.title), h("span", { class: "muted small" }, new Date(p.created_at * 1000).toLocaleString("ru"))),
              statusChip(p.status),
            ),
          ),
        )
      : [h("li", { class: "muted" }, "Пока нет выпусков.")]),
  );
}

// Фоновое обновление списка: сбой — в общую плашку ошибок, без «Uncaught (in promise)».
export const reloadList = safe(() => loadProjects());

export async function open(id) {
  state.drafts = {};
  state.stepDrafts = {};
  show(await api("GET", `${API}/projects/${id}`));
  reloadList();
  history.replaceState(null, "", `?scenario=${scenario}&project=${id}`);
}

// Один запрос на действие: повторный клик (или клик по кнопке, которую перерисовка подменила
// новой) до ответа ждёт первый запрос, а не шлёт второй POST.
export function act(path, body) {
  const id = state.project.id;
  return busyRequest(null, async () => {
    try {
      show(await api("POST", `${API}/projects/${id}/${path}`, body ?? {}));
      return true;
    } catch (err) {
      alertBox(err.message);
      return false;
    }
  }, `act:${id}:${path}`);
}

export function alertBox(message) {
  const box = $("project-error");
  if (box) {
    box.textContent = message;
    box.hidden = false;
  }
}

// Скринридеру — только переходы шагов («готово», «ошибка», «ждёт вас»), не каждую перерисовку.
function announceChanges(project) {
  const now = Object.fromEntries(project.steps.map((s) => [s.id, s.status]));
  const before = state.statuses?.id === project.id ? state.statuses.steps : null;
  state.statuses = { id: project.id, steps: now };
  if (!before) return;
  const changed = visibleSteps(project).filter((s) => before[s.id] !== s.status && ["done", "failed", "waiting"].includes(s.status));
  if (changed.length) announce(changed.map((s) => `Шаг «${s.label}»: ${STATUS[s.status]}`).join(". "));
}

export function show(project) {
  state.project = project;
  announceChanges(project);
  const saved = captureFocus($("project"));
  const base = [state.gen, project.id];
  const sig = (...parts) => JSON.stringify([base, ...parts]);
  const statuses = project.steps.map((s) => s.status);
  const check = checkBox(project, { done: !project.estimate?.parts?.length });
  const root = $("project");
  reconcile(
    root,
    [
      {
        key: "head",
        sig: sig(project.title, project.running, statuses, project.settings, project.archived, project.review?.verdict, publishPick(project).ignoreVerdict),
        build: () => renderHead(project),
      },
      { key: "settings", sig: sig(project.settings, state.config.channels, project.running), build: () => renderSettings(project) },
      check
        ? { key: "check", sig: sig(project.estimate, project.preflight, project.summary, project.running, project.scenario), build: () => h("div", { class: "panel" }, check, longTextActions(project)) }
        : null,
      { key: "stepper", sig: sig(statuses), build: () => renderStepper(project) },
      { key: "steps", sig: sig(), build: () => h("div", { class: "steps" }) },
      { key: "log", sig: sig(project.log), build: () => h("div", { class: "panel" }, logBlock(project.log, "Журнал выпуска") || h("p", { class: "muted small" }, "Журнал пуст.")) },
    ].filter(Boolean),
    project.id,
  );
  reconcile(
    root.querySelector(':scope > [data-key="steps"]'),
    visibleSteps(project).map((step) => ({
      key: step.id,
      sig: sig(step, project.running, project.settings, collapsed(step), STEP_DATA[step.id]?.(project)),
      build: () => renderStep(step),
    })),
    project.id,
  );
  restoreFocus(saved);
  schedule();
}

// Опрос открытого выпуска: раз в секунду, пока он идёт, раз в 5 с в простое (poller из ui.js:
// в скрытой вкладке — раз в минуту, после сбоя связи — реже). show() откладывает следующий опрос.
const projectPoll = poller({ run: (signal) => refresh(signal), fast: 1000, slow: 5000, isActive: () => state.project?.running });

// Список выпусков слева: раз в 10 с; в скрытой вкладке не опрашивается, при возврате — сразу.
const listPoll = poller({ run: () => loadProjects(), fast: 10000, slow: 10000, hiddenMs: Infinity, onError: (err, n) => n === 1 && reportError(err) });

function schedule() {
  projectPoll.later();
}

// Пока человек тащит файл, страницу не перерисовываем — иначе окно под курсором пропадёт.
document.addEventListener("dragover", () => (state.dragging = Date.now()));

async function refresh(signal) {
  if (!state.project || Date.now() - state.dragging < 1500) return;
  const { id, running } = state.project;
  const fresh = await api("GET", `${API}/projects/${id}`, undefined, { signal });
  if (state.project.id !== id) return; // пока ждали ответ, открыли другой выпуск
  // Блоки с полем в фокусе не пересобираются или возвращают фокус и курсор — см. reconcile.
  show(fresh);
  if (fresh.running !== running) reloadList();
}

// Липкий степпер встаёт под шапку платформы: её высота зависит от ширины окна.
function trackNavHeight() {
  const nav = document.querySelector(".platform-nav");
  if (nav) document.documentElement.style.setProperty("--nav-h", `${nav.offsetHeight}px`);
}

window.addEventListener("resize", debounce(trackNavHeight, 150));

async function init() {
  trackNavHeight();
  // Название сценария сразу — из последней шапки (nav.js кладёт её в localStorage), не «Сценарий» до ответа.
  const cached = loadLast("nav-tabs")?.data?.find?.((t) => t.id === scenario)?.label;
  if (cached) $("scenario-title").textContent = cached;
  $("scenario-hint").textContent = HINTS[scenario];
  state.config = await api("GET", `${API}/config`);
  $("scenario-title").textContent = state.config.scenarios.find((s) => s.id === scenario)?.label || scenario;
  document.title = `${$("scenario-title").textContent} — AI Platform`;
  renderCreate();
  $("create").addEventListener("submit", create);
  $("create").addEventListener("input", refreshCreateCheck);
  $("create").addEventListener("change", refreshCreateCheck);
  await loadProjects();
  const wanted = new URLSearchParams(location.search).get("project");
  const first = wanted || state.projects[0]?.id;
  if (first) await open(first).catch(() => {});
  projectPoll.start(1000);
  listPoll.start(10000);
}

init().catch((err) => {
  // Причина — в консоль; человеку — что сделать (TXT-003).
  console.error(err);
  $("project").replaceChildren(h("p", { class: "job-error" }, "Не удалось загрузить выпуски. Проверь, что сервис запущен, и обнови страницу."));
});
