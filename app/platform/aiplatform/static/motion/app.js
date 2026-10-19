// Вкладка «Моушн»: видео → расшифровка → команда агентов → отдельные анимации с таймкодами. Без сборки.

import { $, api, chip, confirmAction, fill, h, icon, jobProgress, logBlock, poller, statusChip, toast, toneOf, uploadFile } from "/ui.js";
import { stageRows, summary } from "../montage/stages.js";
import { byTime, clock, parseClock, sceneStatus, sceneSummary } from "./scenes.js";

const API = "/api/motion";
const STATUS_LABEL = { draft: "черновик" };

const state = {
  config: null,
  projects: [],
  montage: null, // готовые ролики «Монтажа» — загружаются при первом показе формы
  selectedId: new URLSearchParams(location.search).get("id"),
  project: null,
  upload: null, // { name, percent, error }
};

const isActive = (p) => p?.status === "queued" || p?.status === "running";
const statusChipOf = (status) => (STATUS_LABEL[status] ? chip(STATUS_LABEL[status], "neutral") : statusChip(status));
const layoutLabel = (id) => state.config?.layouts.find((l) => l.id === id)?.label || "";

// ---------- список ----------

let listSignature = "";

function renderList() {
  const signature = JSON.stringify([state.selectedId, state.projects.map((p) => [p.id, p.title, p.status, Math.round(p.progress?.percent || 0), p.scenes_done])]);
  if (signature === listSignature) return;
  listSignature = signature;
  if (!state.projects.length) return fill($("projects"), h("li", { class: "empty" }, "Пока пусто. Нажми «Новый проект»."));
  fill($("projects"), ...state.projects.map((p) =>
    h("li", {},
      h("button", {
          type: "button",
          class: `history-item${p.id === state.selectedId ? " current" : ""}`,
          "aria-current": p.id === state.selectedId ? "true" : null,
          onclick: () => select(p.id),
        },
        h("span", { class: "history-text" },
          h("span", { class: "history-title" }, p.title),
          h("span", { class: "muted small" },
            new Date(p.created_at * 1000).toLocaleString("ru-RU", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }),
            p.source ? ` · ${clock(p.source.duration)}` : "",
            p.scenes_done ? ` · анимаций ${p.scenes_done}` : ""),
        ),
        p.status === "running" ? chip(`${Math.round(p.progress.percent)}%`, toneOf("running")) : statusChipOf(p.status),
      ),
    ),
  ));
}

async function select(id) {
  state.selectedId = id;
  history.replaceState(null, "", id ? `?id=${id}` : location.pathname);
  renderList();
  await refresh();
}

async function newProject(body = {}) {
  try {
    // Модель расшифровки не шлём: сервер подставит установленную и не выключенную.
    const { model, ...defaults } = state.config.defaults;
    const project = await api("POST", `${API}/projects`, { title: "", options: defaults, ...body });
    state.projects.unshift(project);
    await select(project.id);
    return project;
  } catch (err) {
    setBanner(err.message, "error");
    return null;
  }
}

async function refresh(signal) {
  if (!state.selectedId) return renderProject();
  try {
    state.project = await api("GET", `${API}/projects/${state.selectedId}`, undefined, { signal });
  } catch (err) {
    if (err.status === 404) return select(null);
    throw err;
  }
  renderProject();
}

// ---------- карточка проекта ----------

let renderedKey = "";

function renderProject() {
  const root = $("project");
  const p = state.project && state.project.id === state.selectedId ? state.project : null;
  if (!p) {
    renderedKey = "";
    return fill(root, intro());
  }
  // Карточка перерисовывается только при изменениях (не сбиваем ввод и не перезапускаем превью сцен);
  // пока идёт работа, каждый опрос обновляет лишь блок «Работа».
  const key = JSON.stringify([p.id, p.status, p.updated_at, !!state.upload, !!state.montage]);
  if (key === renderedKey) {
    if (isActive(p)) $("work")?.replaceWith(progress(p));
    return;
  }
  renderedKey = key;
  const blocks = [head(p)];
  if (!isActive(p) && !p.design) blocks.push(setup(p));
  if (isActive(p)) blocks.push(progress(p));
  if (p.status === "failed") blocks.push(h("section", { class: "panel stack" }, h("p", { class: "job-error", role: "alert" }, p.error), p.design ? restart(p) : null));
  if (p.status === "canceled") blocks.push(h("section", { class: "panel stack" }, h("p", { class: "muted" }, "Отменено."), p.design ? restart(p) : null));
  if (p.scenes?.length) blocks.push(scenesPanel(p));
  if (p.design && !isActive(p)) blocks.push(addScene(p));
  fill(root, ...blocks);
}

function intro() {
  const roles = state.config?.roles || [];
  return h("section", { class: "panel stack" },
    h("h2", {}, "Анимации к твоему видео"),
    h("p", { class: "muted" }, "Загрузи готовый ролик или возьми его из «Монтажа». Платформа расшифрует речь, команда агентов найдёт места, где ты объясняешь механизм, и нарисует анимацию к каждому: канал с буфером, путь пакета по слоям, схему. Исходник не меняется — на выходе отдельные ролики с прозрачным фоном и таймкоды, куда их поставить в монтажке."),
    roles.length ? h("ol", { class: "team" }, ...roles.map((r) => h("li", {}, h("b", {}, r.label), " — ", ROLE_HINT[r.id] || ""))) : null,
    h("div", { class: "toolbar" }, h("button", { type: "button", class: "btn btn--primary", onclick: () => newProject() }, "Новый проект")),
  );
}

const ROLE_HINT = {
  editor: "читает всю расшифровку и выбирает моменты, где картинка объяснит лучше слов",
  designer: "задаёт один стиль на выпуск: палитру, шрифты, персонажей вроде гофера",
  director: "ставит сцену: что появляется на каждом твоём слове, формат, камера",
  expert: "проверяет правду о технологии: порядок слоёв, семантику каналов; снимает лишнее",
  animator: "пишет HTML, SVG и GSAP по раскадровке; HyperFrames рендерит прозрачный MOV",
};

function head(p) {
  const title = h("input", { type: "text", value: p.title, maxlength: "120", class: "title-input", "aria-label": "Название проекта" });
  title.addEventListener("change", () => update(p, { title: title.value }));
  return h("section", { class: "panel" },
    h("div", { class: "field-head" },
      h("label", { class: "field grow" }, h("span", { class: "field-label" }, "Название"), title),
      h("div", { class: "toolbar" },
        statusChipOf(p.status),
        p.backend === "fake" ? chip("Демо — без моделей", "warning") : null,
        h("button", { type: "button", class: "btn btn--link btn--danger", onclick: () => remove(p), disabled: isActive(p) }, "Удалить"),
      ),
    ),
    p.topic ? h("p", { class: "muted" }, `Тема: ${p.topic}`) : null,
    p.design ? designLine(p.design) : null,
  );
}

function designLine(design) {
  return h("div", { class: "design-line" },
    h("span", { class: "muted small" }, `Стиль «${design.name}»`),
    h("span", { class: "swatches", "aria-hidden": "true" }, ...Object.values(design.palette || {}).map((c) => h("span", { class: "swatch", style: `background:${c}` }))),
    design.characters?.length ? h("span", { class: "muted small" }, `Персонажи: ${design.characters.map((c) => c.name).join(", ")}`) : null,
  );
}

async function update(p, body) {
  try {
    state.project = await api("POST", `${API}/projects/${p.id}`, body);
    renderedKey = "";
    renderProject();
    listPoll.now();
  } catch (err) {
    setBanner(err.message, "error");
  }
}

async function remove(p) {
  const ok = await confirmAction({ title: `Удалить «${p.title}»?`, body: "Удалятся видео проекта, расшифровка и все анимации.", confirmLabel: "Удалить", danger: true });
  if (!ok) return;
  try {
    await api("POST", `${API}/projects/${p.id}/delete`);
    state.projects = state.projects.filter((x) => x.id !== p.id);
    toast("Проект удалён");
    await select(null);
  } catch (err) {
    setBanner(err.message, "error");
  }
}

// ---------- видео и настройки ----------

function sourceCard(p) {
  const meta = p.source;
  const up = state.upload;
  const input = h("input", { type: "file", accept: "video/mp4,video/quicktime,video/webm,.mp4,.mov,.webm", hidden: true });
  input.addEventListener("change", () => input.files[0] && upload(p, input.files[0]));
  const card = h("div", { class: `source${meta ? " ready" : ""}` },
    h("span", { class: "field-label" }, "Видео"),
    meta
      ? h("video", { src: p.source_url, controls: true, preload: "metadata", playsinline: true })
      : h("p", { class: "muted small" }, "Готовый ролик, куда пойдут анимации. MP4, MOV или WebM. Его не изменим."),
    meta ? h("span", { class: "muted small" }, `${clock(meta.duration)} · ${meta.width}×${meta.height} · ${Math.round(meta.fps)} к/с${p.origin?.montage ? " · из «Монтажа», расшифровка готова" : ""}`) : null,
    up ? h("span", { class: up.error ? "form-error" : "muted small", "data-upload": "1" }, up.error ? `${up.name}: ${up.error}` : `${up.name} — ${up.percent}%`) : null,
    h("div", { class: "toolbar" },
      h("button", { type: "button", class: "btn btn--secondary btn--sm", disabled: !!up && !up.error, onclick: () => input.click() }, meta ? "Заменить" : "Выбрать файл"),
      input,
    ),
  );
  card.addEventListener("dragover", (e) => (e.preventDefault(), card.classList.add("drag")));
  card.addEventListener("dragleave", () => card.classList.remove("drag"));
  card.addEventListener("drop", (e) => {
    e.preventDefault();
    card.classList.remove("drag");
    const file = e.dataTransfer.files[0];
    if (file) upload(p, file);
  });
  return card;
}

async function upload(p, file) {
  state.upload = { name: file.name, percent: 0, error: null };
  renderedKey = "";
  renderProject();
  try {
    state.project = await uploadFile(`${API}/projects/${p.id}/source`, file, {
      limit: state.config.max_upload_bytes,
      onProgress: ({ percent }) => {
        state.upload.percent = percent;
        const row = document.querySelector("[data-upload]");
        if (row) row.textContent = `${file.name} — ${percent}%`;
      },
    });
    state.upload = null;
  } catch (err) {
    if (err.kind === "abort") state.upload = null;
    else state.upload.error = err.message;
  }
  renderedKey = "";
  renderProject();
  listPoll.now();
}

function fromMontage(p) {
  if (state.montage === null) {
    api("GET", `${API}/montage`).then((r) => ((state.montage = r.projects), (renderedKey = ""), renderProject())).catch(() => (state.montage = []));
    return h("p", { class: "muted small" }, "Ищу готовые ролики «Монтажа»…");
  }
  if (!state.montage.length) return h("p", { class: "muted small" }, "Готовых роликов в «Монтаже» нет.");
  const pick = h("select", { "aria-label": "Ролик «Монтажа»" }, ...state.montage.map((m) => h("option", { value: m.id }, `${m.title}${m.duration ? ` · ${clock(m.duration)}` : ""}`)));
  const take = h("button", { type: "button", class: "btn btn--secondary btn--sm" }, "Взять");
  take.addEventListener("click", async () => {
    take.disabled = true;
    const created = await newProject({ montage: pick.value, options: { ...p.options } });
    if (created && !p.source) {
      await api("POST", `${API}/projects/${p.id}/delete`).catch(() => {});
      state.projects = state.projects.filter((x) => x.id !== p.id);
      renderList();
    }
    take.disabled = false;
  });
  return h("div", { class: "toolbar" }, pick, take);
}

function setup(p) {
  const cfg = state.config;
  const o = { ...p.options };
  const montage = !!p.origin?.montage;
  const model = h("select", { disabled: montage },
    ...cfg.models.map((m) => h("option", { value: m.id, selected: m.id === o.model, disabled: !m.installed }, m.installed ? `${m.label} — ${m.note}` : `${m.label} — не установлена`)));
  const scenes = h("input", { type: "number", min: "0", max: "30", step: "1", value: String(o.max_scenes) });
  const brief = h("textarea", { rows: 3, maxlength: String(cfg.max_brief), placeholder: "О чём ролик и чего хочешь: «про каналы в Go — покажи буфер и блокировки гофером», «без сцен на весь кадр»" }, o.brief);
  const terms = h("textarea", { rows: 2, placeholder: "goroutine, select, TCP, Ethernet — английские слова и имена из ролика" }, o.terms);
  const size = { value: o.size };
  const error = h("p", { class: "form-error", role: "alert", hidden: true });
  const start = h("button", { type: "button", class: "btn btn--primary", disabled: !p.source }, p.status === "draft" ? "Начать" : "Начать заново");
  start.addEventListener("click", async () => {
    const count = Number(scenes.value);
    if (!Number.isInteger(count) || count < 0 || count > 30) return ((error.textContent = "Сцен — целое от 0 (сам решит) до 30."), (error.hidden = false));
    error.hidden = true;
    start.disabled = true;
    try {
      const options = { max_scenes: count, size: size.value, brief: brief.value, terms: terms.value, ...(montage ? {} : { model: model.value }) };
      await api("POST", `${API}/projects/${p.id}`, { options });
      state.project = await api("POST", `${API}/projects/${p.id}/start`);
      renderedKey = "";
      renderProject();
      listPoll.now();
    } catch (err) {
      error.textContent = err.message;
      error.hidden = false;
      start.disabled = false;
    }
  });
  return h("section", { class: "panel stack" },
    h("h2", {}, "Видео"),
    h("div", { class: "sources" }, sourceCard(p)),
    p.source ? null : h("div", { class: "field" }, h("span", { class: "field-label" }, "Или готовый ролик из «Монтажа» ", h("span", { class: "muted" }, "— без копии и без второй расшифровки")), fromMontage(p)),
    h("h2", {}, "Как делать"),
    h("label", { class: "field" }, h("span", { class: "field-label" }, "О чём ролик и пожелания ", h("span", { class: "muted" }, "— читают все агенты")), brief),
    h("label", { class: "field" }, h("span", { class: "field-label" }, "Сколько анимаций, не больше ", h("span", { class: "muted" }, "— 0: агенты решат сами")), h("span", { class: "narrow" }, scenes)),
    h("fieldset", { class: "field" }, h("legend", { class: "field-label" }, "Размер роликов"), radios("size", cfg.sizes, o.size, (v) => (size.value = v))),
    montage ? null : h("label", { class: "field" }, h("span", { class: "field-label" }, "Расшифровка"), model),
    montage ? null : h("label", { class: "field" }, h("span", { class: "field-label" }, "Термины ", h("span", { class: "muted" }, "— подсказка расшифровке")), terms),
    error,
    h("div", { class: "toolbar" }, start),
  );
}

function radios(name, items, current, onPick) {
  return h("div", { class: "segmented segmented--radio" }, ...items.map((item) =>
    h("label", { class: "seg" },
      h("input", { type: "radio", name, value: item.id, checked: item.id === current, onchange: () => onPick(item.id) }),
      h("span", {}, item.label),
    ),
  ));
}

// ---------- работа ----------

const STAGE_ICONS = { done: "check", failed: "close", canceled: "close" };

function progress(p) {
  const now = Date.now() / 1000;
  const info = summary(p, now);
  const rows = stageRows(p.stages, now);
  return h("section", { class: "panel stack", id: "work" },
    h("h2", {}, "Работа"),
    jobProgress(p, { onCancel: () => act(p, "cancel") }),
    rows.length ? h("p", { class: "muted small stage-summary" }, [info.step, info.elapsed && `прошло ${info.elapsed}`, info.left].filter(Boolean).join(" · ")) : null,
    rows.length ? h("ol", { class: "stages" }, ...rows.map(stageRow)) : null,
    logBlock(p.log),
  );
}

function stageRow(s) {
  return h("li", { class: `stage-row stage-row--${s.status}` },
    h("span", { class: "stage-icon", "aria-hidden": "true" }, STAGE_ICONS[s.status] ? icon(STAGE_ICONS[s.status]) : h("span", { class: "stage-dot" })),
    h("div", { class: "stage-body" },
      h("div", { class: "stage-head" },
        h("span", { class: "stage-label" }, s.label, h("span", { class: "sr-only" }, ` — ${s.status === "running" ? "идёт" : s.note || "впереди"}`)),
        h("span", { class: "muted small" }, s.status === "running" ? `${s.percent}% · ${s.note}` : s.note),
      ),
      s.status === "running" ? h("div", { class: "bar", role: "progressbar", "aria-label": s.label, "aria-valuemin": "0", "aria-valuemax": "100", "aria-valuenow": String(s.percent) },
        h("div", { class: "bar-fill", style: `width:${s.percent}%` })) : null,
      s.detail && s.status === "running" ? h("span", { class: "muted small" }, s.detail) : null,
    ),
  );
}

function restart(p) {
  const again = async () => {
    const ok = await confirmAction({ title: "Прогнать заново целиком?", body: "Агенты заново выберут моменты и стиль; нынешние анимации пропадут.", confirmLabel: "Прогнать" });
    if (ok) act(p, "start");
  };
  return h("div", { class: "toolbar" }, h("button", { type: "button", class: "btn btn--link", onclick: again }, "Прогнать заново целиком"));
}

async function act(p, action, body) {
  try {
    state.project = await api("POST", `${API}/projects/${p.id}/${action}`, body);
    renderedKey = "";
    renderProject();
    listPoll.now();
    return true;
  } catch (err) {
    setBanner(err.message, "error");
    return false;
  }
}

// ---------- сцены ----------

function scenesPanel(p) {
  const s = sceneSummary(p.scenes);
  const busy = isActive(p);
  return h("section", { class: "panel stack" },
    h("div", { class: "field-head" },
      h("h2", {}, "Анимации"),
      p.package_url ? h("a", { class: "btn btn--primary", href: p.package_url }, `Скачать пакет: ${s.packed} MOV + таймкоды`) : null,
    ),
    h("ul", { class: "facts" },
      h("li", {}, h("b", {}, String(s.done)), h("span", { class: "muted" }, ` готово из ${s.total}`)),
      s.dropped ? h("li", {}, h("b", {}, String(s.dropped)), h("span", { class: "muted" }, " снял техред")) : null,
      s.failed ? h("li", {}, h("b", {}, String(s.failed)), h("span", { class: "muted" }, " не получились")) : null,
    ),
    h("p", { class: "muted small" }, "Ролики — MOV ProRes 4444 с прозрачным фоном: положи на дорожку над своим видео с указанного таймкода. Превью ниже — как это ляжет на кадр."),
    h("ol", { class: "scene-list" }, ...byTime(p.scenes).map((scene) => sceneCard(p, scene, busy))),
    busy ? null : restart(p),
  );
}

function sceneCard(p, scene, busy) {
  const st = sceneStatus(scene);
  const media = scene.urls?.["preview.mp4"]
    ? h("video", { class: "scene-video", src: `${scene.urls["preview.mp4"]}?v=${Math.round(scene.made_at || 0)}`, poster: scene.urls["poster.jpg"], controls: true, preload: "none", playsinline: true })
    : h("div", { class: "scene-video scene-video--empty" }, scene.status === "working" ? "в работе…" : "нет превью");
  const feedback = h("textarea", { rows: 2, maxlength: String(state.config.max_feedback), placeholder: "Что поправить: «пакет должен идти сверху вниз», «на весь кадр», «гофер крупнее»" });
  const redo = h("button", { type: "button", class: "btn btn--secondary btn--sm", disabled: busy }, "Переделать сцену");
  redo.addEventListener("click", () => (feedback.value.trim() ? act(p, `scenes/${scene.id}/redo`, { text: feedback.value }) : feedback.focus()));
  const inPackage = h("input", { type: "checkbox", checked: scene.enabled !== false, disabled: busy || scene.status !== "done" });
  inPackage.addEventListener("change", () => act(p, `scenes/${scene.id}`, { enabled: inPackage.checked }));
  const issues = scene.issues || [];
  return h("li", { class: `scene scene--${scene.status}${scene.enabled === false ? " scene--off" : ""}` },
    media,
    h("div", { class: "scene-body" },
      h("div", { class: "scene-head" },
        h("span", { class: "scene-time" }, scene.timecode, " → ", scene.timecode_end),
        chip(st.label, st.tone),
      ),
      h("h3", {}, scene.title, scene.layout ? h("span", { class: "muted small" }, ` · ${layoutLabel(scene.layout)}`) : null),
      h("p", { class: "muted small" }, scene.concept),
      scene.idea ? h("p", { class: "small" }, scene.idea) : null,
      scene.error ? h("p", { class: "form-error" }, scene.error) : null,
      issues.length ? h("p", { class: "small callout" }, `${scene.status === "dropped" ? "Почему снята" : "Техред"}: ${issues.join("; ")}`) : null,
      scene.beats?.length ? h("details", { class: "disclosure" }, h("summary", {}, `Раскадровка — ${scene.beats.length} битов`),
        h("ol", { class: "beats" }, ...scene.beats.map((b) => h("li", {}, h("span", { class: "scene-time" }, `+${b.t.toFixed(1)} с`), ` ${b.action}`, b.text ? h("b", {}, ` «${b.text}»`) : null)))) : null,
      h("div", { class: "toolbar" },
        scene.urls?.["scene.mov"] ? h("a", { class: "btn btn--secondary btn--sm", href: scene.urls["scene.mov"] }, scene.file_name) : null,
        scene.urls?.["index.html"] ? h("a", { class: "btn btn--link btn--sm", href: scene.urls["index.html"] }, "Код сцены") : null,
        scene.status === "done" ? h("label", { class: "check" }, inPackage, h("span", {}, "В пакете")) : null,
      ),
      h("details", { class: "disclosure" }, h("summary", {}, scene.status === "dropped" ? "Всё равно сделать — с пожеланием" : "Поправить сцену"),
        h("div", { class: "stack" }, feedback, h("div", { class: "toolbar" }, redo))),
    ),
  );
}

function addScene(p) {
  const at = h("input", { type: "text", inputmode: "numeric", placeholder: "12:30", "aria-label": "Время в видео" });
  const text = h("textarea", { rows: 2, maxlength: String(state.config.max_feedback), placeholder: "Что показать: «как select выбирает готовый канал»" });
  const error = h("p", { class: "form-error", role: "alert", hidden: true });
  const add = h("button", { type: "button", class: "btn btn--secondary" }, "Добавить сцену");
  add.addEventListener("click", async () => {
    const seconds = parseClock(at.value);
    if (seconds === null) return ((error.textContent = "Время — как 12:30 или 1:02:05."), (error.hidden = false));
    if (!text.value.trim()) return ((error.textContent = "Напиши, что показать."), (error.hidden = false));
    error.hidden = true;
    if (await act(p, "scenes", { at: seconds, text: text.value })) toast("Сцена в работе");
  });
  return h("details", { class: "disclosure panel" },
    h("summary", {}, "Добавить свою сцену"),
    h("div", { class: "stack" },
      h("p", { class: "muted small" }, "Режиссёр, техред и аниматор поставят её по твоим словам вокруг этого момента."),
      h("label", { class: "field narrow" }, h("span", { class: "field-label" }, "Момент в видео"), at),
      h("label", { class: "field" }, h("span", { class: "field-label" }, "Что показать"), text),
      error,
      h("div", { class: "toolbar" }, add),
    ),
  );
}

// ---------- опрос и запуск ----------

const listPoll = poller({
  run: async (signal) => {
    state.projects = (await api("GET", `${API}/projects`, undefined, { signal })).projects;
    renderList();
    if (state.selectedId) await refresh(signal);
    setBanner(null);
  },
  fast: 1000,
  slow: 5000,
  isActive: () => state.projects.some(isActive),
  onError: (err) => setBanner(err.message, "error"),
});

let configBanner = null;
const BANNER_TONE = { warn: "warning", error: "danger" };

function setBanner(text, kind) {
  const banner = $("banner");
  const shown = text ? { text, kind } : configBanner;
  banner.hidden = !shown;
  if (!shown) return;
  banner.className = `banner${BANNER_TONE[shown.kind] ? ` tone-${BANNER_TONE[shown.kind]}` : ""}`;
  banner.setAttribute("role", shown.kind === "error" ? "alert" : "status");
  banner.replaceChildren(...[].concat(shown.text));
}

async function init() {
  try {
    state.config = await api("GET", `${API}/config`);
  } catch (err) {
    return setBanner(err.message, "error");
  }
  const cfg = state.config;
  $("team-chip").textContent = cfg.backend === "fake" ? "демо, без моделей" : `агентов ${cfg.roles.length} · по ${cfg.parallel} сцены сразу`;
  if (cfg.backend === "fake") {
    configBanner = { kind: "warn", text: "Демо-режим: агенты и рендер — имитация, а не модели. Превью и пакет — настоящие файлы." };
  } else if (!cfg.hyperframes_available) {
    configBanner = { kind: "warn", text: `Рисовать нечем: ${cfg.install_hint}` };
  } else if (!cfg.llm_available) {
    configBanner = { kind: "warn", text: "Нет claude CLI — агентам думать нечем (AIP_LLM_CMD)." };
  }
  setBanner(null);
  $("new-project").addEventListener("click", () => newProject());
  renderProject();
  listPoll.start();
}

init();
