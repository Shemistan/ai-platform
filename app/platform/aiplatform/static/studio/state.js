// Студия: адрес API, сценарий из адреса и всё изменяемое состояние страницы (FE-010).

export const API = "/api/studio";

// Сценарий из адреса; неизвестный — «article». Названия сценариев — из /api/studio/config (FE-007).
const KNOWN_SCENARIOS = new Set(["digest", "article", "post"]);

const wantedScenario = new URLSearchParams(location.search).get("scenario");

export const scenario = KNOWN_SCENARIOS.has(wantedScenario) ? wantedScenario : "article";

// У поста нет слайдов: эти шаги всегда пропущены — их не показываем.
const HIDDEN_STEPS = { post: new Set(["plan", "images", "clips", "short"]) };

export const CAPTION_LIMIT = 1024;

export const HINTS = {
  digest:
    "narrator собирает свежие новости и пишет пересказ → постер → слайды с картинками → озвучка → видео 16:9 (и шортс 9:16) → публикация.",
  article: "Вставь готовую статью → постер → слайды с картинками → озвучка → видео 16:9 (и шортс 9:16) → публикация.",
  post: "Свой постер, своя озвучка (или озвучка подписи) и подпись → одно видео с постером под звук или картинка с аудио → в каналы.",
};

// Всё изменяемое состояние страницы — здесь, по полю с назначением.
export const state = {
  config: null, // ответ /api/studio/config: модели, каналы, умолчания, названия сценариев
  projects: [], // список выпусков сценария слева (loadProjects)
  project: null, // открытый выпуск — последний ответ сервера (show)
  gen: 0, // поколение отрисовки: +1 заставляет перерисовать все блоки (после сохранения правки, нового канала)
  drafts: {}, // несохранённые правки редакторов: имя артефакта → текст (editor)
  stepDrafts: {}, // наброски «Параметров шага»: id шага → настройки, пока шаг не перезапустили
  openSettings: new Set(), // id шагов с раскрытыми «Параметрами шага»
  publish: {}, // выбор в шаге «Публикация» по выпускам: id → {channels, force, ignoreVerdict, noDisclaimers}
  expanded: {}, // раскрытие шагов руками: «id выпуска:id шага» → true/false, живёт между перерисовками
  statuses: null, // статусы шагов на прошлой отрисовке — для объявлений скринридеру
  dragging: 0, // когда последний раз тащили файл над страницей: опрос не перерисовывает под курсором
};

// Файлы шагу можно класть и во время запуска — пока сам шаг и зависящие от него не выполняются.
export const stepLocked = (id) => Boolean(state.project.steps.find((s) => s.id === id)?.locked);

export const visibleSteps = (project) => project.steps.filter((s) => !HIDDEN_STEPS[project.scenario]?.has(s.id));
