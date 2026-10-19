// Вкладка «Монтаж»: ролики, записи, расшифровка с вырезками на подтверждение, сборка и итог. Без сборки.

import { $, api, chip, confirmAction, debounce, fill, h, icon, jobProgress, logBlock, modal, poller, statusChip, toast, toneOf, uploadFile } from "/ui.js";
import { clock, cutOf, layoutAt, phrases, projectPayload, setLayout, togglePhrase, toggleWord } from "./plan.js";
import { stageRows, summary } from "./stages.js";

const API = "/api/montage";
const STATUS_LABEL = { draft: "черновик", review: "ждёт вас" };
const STATUS_TONE = { draft: "neutral", review: "warning" };

const state = {
  config: null,
  projects: [],
  selectedId: new URLSearchParams(location.search).get("id"),
  project: null,
  plan: null, // GET plan: слова, вырезки, планы, сводка
  planFor: null, // id + updated_at, для которых загружен план
  lastCut: null, // последняя своя вырезка — Shift+клик тянет её
  saving: "",
  uploads: {}, // role → { name, percent, error }
};

const isActive = (p) => p?.status === "queued" || p?.status === "running";
const statusChipOf = (status) => (STATUS_LABEL[status] ? chip(STATUS_LABEL[status], STATUS_TONE[status]) : statusChip(status));
const kindLabel = (id) => state.config?.kinds.find((k) => k.id === id)?.label || id;

// ---------- список ----------

let listSignature = "";

function renderList() {
  const signature = JSON.stringify([state.selectedId, state.projects.map((p) => [p.id, p.title, p.status, Math.round(p.progress?.percent || 0)])]);
  if (signature === listSignature) return;
  listSignature = signature;
  if (!state.projects.length) return fill($("projects"), h("li", { class: "empty" }, "Пока пусто. Нажми «Новый ролик»."));
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
            p.output ? ` · ${clock(p.output.duration)}` : p.sources?.head ? ` · запись ${clock(p.sources.head.duration)}` : ""),
        ),
        p.status === "running" ? chip(`${Math.round(p.progress.percent)}%`, toneOf("running")) : statusChipOf(p.status),
      ),
    ),
  ));
}

async function select(id) {
  state.selectedId = id;
  history.replaceState(null, "", id ? `?id=${id}` : location.pathname);
  state.plan = null;
  state.planFor = null;
  renderList();
  await refresh();
}

async function newProject() {
  try {
    const project = await api("POST", `${API}/projects`, { title: "", options: state.config.defaults });
    state.projects.unshift(project);
    await select(project.id);
  } catch (err) {
    setBanner(err.message, "error");
  }
}

// ---------- загрузка проекта ----------

async function refresh(signal) {
  if (!state.selectedId) return renderProject();
  let project;
  try {
    project = await api("GET", `${API}/projects/${state.selectedId}`, undefined, { signal });
  } catch (err) {
    if (err.status === 404) return select(null);
    throw err;
  }
  state.project = project;
  const needsPlan = ["review", "done"].includes(project.status) || (project.plan && !isActive(project));
  const key = `${project.id}:${project.updated_at}`;
  if (needsPlan && state.planFor !== key && !state.saving) {
    state.plan = await api("GET", `${API}/projects/${project.id}/plan`, undefined, { signal });
    state.planFor = key;
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
    return fill(root, h("section", { class: "panel" }, h("h2", {}, "Монтаж своей записи"),
      h("p", { class: "muted" }, "Загрузи сырую запись — «голову» и, если показываешь экран, запись экрана. Платформа расшифрует речь, ИИ предложит, что вырезать: паузы, оговорки, дубли, твои команды в кадр. Ты проверишь план — и получишь готовый ролик."),
      h("button", { type: "button", class: "btn btn--primary", onclick: newProject }, "Новый ролик")));
  }
  // Пока идёт работа, перерисовываем только прогресс: так не сбивается ввод и не дёргается расшифровка.
  const key = JSON.stringify([p.id, p.status, p.updated_at, state.planFor, Object.keys(state.uploads).length]);
  if (key === renderedKey && !isActive(p)) return;
  renderedKey = key;
  const blocks = [head(p)];
  if (p.status === "draft" || ((p.status === "failed" || p.status === "canceled") && !p.transcript)) blocks.push(setup(p));
  if (isActive(p)) blocks.push(progress(p));
  if (p.status === "failed") blocks.push(h("section", { class: "panel" }, h("p", { class: "job-error", role: "alert" }, p.error), retry(p)));
  if (p.status === "canceled") blocks.push(h("section", { class: "panel" }, h("p", { class: "muted" }, "Отменено."), retry(p)));
  if (p.status === "done") blocks.push(result(p));
  if (state.plan && !isActive(p) && p.transcript) blocks.push(review(p));
  fill(root, ...blocks);
}

function head(p) {
  const title = h("input", { type: "text", value: p.title, maxlength: "120", class: "title-input", "aria-label": "Название ролика" });
  title.addEventListener("change", () => update(p, { title: title.value }));
  return h("section", { class: "panel" },
    h("div", { class: "field-head" },
      h("label", { class: "field grow" }, h("span", { class: "field-label" }, "Название"), title),
      h("div", { class: "toolbar" },
        statusChipOf(p.status),
        p.backend === "fake" ? chip("Демо — без модели", "warning") : null,
        h("button", { type: "button", class: "btn btn--link btn--danger", onclick: () => remove(p), disabled: isActive(p) }, "Удалить"),
      ),
    ),
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
  const ok = await confirmAction({ title: `Удалить «${p.title}»?`, body: "Удалятся записи, расшифровка и готовый ролик.", confirmLabel: "Удалить", danger: true });
  if (!ok) return;
  try {
    await api("POST", `${API}/projects/${p.id}/delete`);
    state.projects = state.projects.filter((x) => x.id !== p.id);
    toast("Ролик удалён");
    await select(null);
  } catch (err) {
    setBanner(err.message, "error");
  }
}

// ---------- записи и настройки ----------

function sourceCard(p, role, label, hint) {
  const meta = p.sources?.[role];
  const up = state.uploads[role];
  const input = h("input", { type: "file", accept: "video/mp4,video/quicktime,video/webm,.mp4,.mov,.webm", hidden: true });
  input.addEventListener("change", () => input.files[0] && upload(p, role, input.files[0]));
  const card = h("div", { class: `source${meta ? " ready" : ""}` },
    h("span", { class: "field-label" }, label),
    meta
      ? h("video", { src: p.sources_url?.[role], controls: true, preload: "metadata", playsinline: true })
      : h("p", { class: "muted small" }, hint),
    meta ? h("span", { class: "muted small" }, `${clock(meta.duration)} · ${meta.width}×${meta.height}${meta.has_audio ? "" : " · без звука"}`) : null,
    up ? h("span", { class: up.error ? "form-error" : "muted small", "data-upload": role }, up.error ? `${up.name}: ${up.error}` : `${up.name} — ${up.percent}%`) : null,
    h("div", { class: "toolbar" },
      h("button", { type: "button", class: "btn btn--secondary btn--sm", disabled: !!up && !up.error, onclick: () => input.click() }, meta ? "Заменить" : "Выбрать файл"),
      meta && role === "screen" ? h("button", { type: "button", class: "btn btn--link btn--sm", onclick: () => dropSource(p, role) }, "Убрать") : null,
      input,
    ),
  );
  card.addEventListener("dragover", (e) => (e.preventDefault(), card.classList.add("drag")));
  card.addEventListener("dragleave", () => card.classList.remove("drag"));
  card.addEventListener("drop", (e) => {
    e.preventDefault();
    card.classList.remove("drag");
    const file = e.dataTransfer.files[0];
    if (file) upload(p, role, file);
  });
  return card;
}

async function upload(p, role, file) {
  state.uploads[role] = { name: file.name, percent: 0, error: null };
  renderedKey = "";
  renderProject();
  try {
    const project = await uploadFile(`${API}/projects/${p.id}/sources/${role}`, file, {
      limit: state.config.max_upload_bytes,
      onProgress: ({ percent }) => {
        state.uploads[role].percent = percent;
        const row = document.querySelector(`[data-upload="${role}"]`);
        if (row) row.textContent = `${file.name} — ${percent}%`;
      },
    });
    delete state.uploads[role];
    state.project = project;
  } catch (err) {
    if (err.kind === "abort") delete state.uploads[role];
    else state.uploads[role].error = err.message;
  }
  renderedKey = "";
  renderProject();
  listPoll.now();
}

async function dropSource(p, role) {
  try {
    state.project = await api("POST", `${API}/projects/${p.id}/sources/${role}/delete`);
    renderedKey = "";
    renderProject();
  } catch (err) {
    setBanner(err.message, "error");
  }
}

function radios(name, items, current, onPick) {
  return h("div", { class: "segmented segmented--radio" }, ...items.map((item) =>
    h("label", { class: "seg" },
      h("input", { type: "radio", name, value: item.id, checked: item.id === current, onchange: () => onPick(item.id) }),
      h("span", {}, item.label),
    ),
  ));
}

function setup(p) {
  const cfg = state.config;
  const o = { ...p.options };
  const model = h("select", {}, ...cfg.models.map((m) =>
    h("option", { value: m.id, selected: m.id === o.model, disabled: !m.installed }, m.installed ? `${m.label} — ${m.note}` : `${m.label} — не установлена`)));
  const terms = h("textarea", { rows: 2, placeholder: "Claude Code, OpenRouter, Hyperframes — английские слова и имена из этого ролика" }, o.terms);
  const pause = h("input", { type: "number", min: "0.3", max: "3", step: "0.1", value: String(o.max_pause) });
  const box = (key, text, hint) => {
    const input = h("input", { type: "checkbox", checked: !!o[key], onchange: () => (o[key] = input.checked) });
    return h("label", { class: "check" }, input, h("span", {}, text, hint ? h("span", { class: "muted" }, ` — ${hint}`) : null));
  };
  const error = h("p", { class: "form-error", role: "alert", hidden: true });
  const start = h("button", { type: "button", class: "btn btn--primary" }, p.status === "draft" ? "Начать" : "Начать заново");
  start.addEventListener("click", async () => {
    const { payload, error: problem } = projectPayload({ ...o, title: p.title, model: model.value, terms: terms.value, maxPause: pause.value });
    if (problem) return ((error.textContent = problem), (error.hidden = false));
    error.hidden = true;
    start.disabled = true;
    try {
      await api("POST", `${API}/projects/${p.id}`, { options: payload.options });
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
    h("h2", {}, "Записи"),
    h("div", { class: "sources" },
      sourceCard(p, "head", "Голова", "Обязательно: ты в кадре, звук — твой голос. MP4, MOV или WebM."),
      sourceCard(p, "screen", "Экран — по желанию", "Запись экрана той же сессии: монтаж сам будет переключать «голову» и «экран»."),
    ),
    h("h2", {}, "Как монтировать"),
    h("label", { class: "field" }, h("span", { class: "field-label" }, "Расшифровка"), model),
    h("label", { class: "field" }, h("span", { class: "field-label" }, "Термины ролика ", h("span", { class: "muted" }, "— подсказка расшифровке; общие — в «Правилах монтажа»")), terms),
    h("fieldset", { class: "field" }, h("legend", { class: "field-label" }, "Цвет головы"), radios("grade", cfg.grades, o.grade, (g) => (o.grade = g))),
    h("label", { class: "field narrow" }, h("span", { class: "field-label" }, "Самая длинная пауза, с"), pause),
    box("check", "Проверить результат", "расшифровать готовый ролик ещё раз и вырезать пропущенное (до двух кругов)"),
    box("subtitles", "Субтитры в кадре", "файл .srt для YouTube будет в любом случае"),
    box("motion", "Моушн-графика", cfg.hyperframes_available ? "заголовки, цифры и списки поверх ролика" : "HyperFrames не установлен: make models M=hyperframes (нужен Node 22+)"),
    box("auto", "Собрать без подтверждения плана"),
    error,
    h("div", { class: "toolbar" }, start),
  );
}

// Общая полоса с «этап N из M · прошло · осталось» и список этапов действия (PRD-025).
function progress(p) {
  const now = Date.now() / 1000;
  const info = summary(p, now);
  const rows = stageRows(p.stages, now);
  return h("section", { class: "panel stack" },
    h("h2", {}, "Работа"),
    jobProgress(p, { onCancel: () => act(p, "cancel") }),
    rows.length ? h("p", { class: "muted small stage-summary" }, [info.step, info.elapsed && `прошло ${info.elapsed}`, info.left].filter(Boolean).join(" · ")) : null,
    rows.length ? h("ol", { class: "stages" }, ...rows.map(stageRow)) : null,
    logBlock(p.log),
  );
}

// Готово — галочка, упал или отменён — крестик; идёт, впереди и пропущен — точка своего вида (style.css).
const STAGE_ICONS = { done: "check", failed: "close", canceled: "close" };

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

function retry(p) {
  return h("div", { class: "toolbar" },
    p.transcript ? h("button", { type: "button", class: "btn btn--primary", onclick: () => act(p, "render") }, "Собрать") : null,
    p.transcript ? h("button", { type: "button", class: "btn btn--secondary", onclick: () => act(p, "replan") }, "Переделать план") : null,
  );
}

async function act(p, action, body) {
  try {
    state.project = await api("POST", `${API}/projects/${p.id}/${action}`, body);
    renderedKey = "";
    renderProject();
    listPoll.now();
  } catch (err) {
    setBanner(err.message, "error");
  }
}

// ---------- итог ----------

function result(p) {
  const rounds = p.check?.rounds || [];
  const fixed = rounds.flatMap((r) => r.issues);
  const tech = rounds.flatMap((r) => r.tech || []);
  return h("section", { class: "panel stack" },
    h("h2", {}, "Готовый ролик"),
    h("video", { class: "player", src: `${p.video_url}?v=${Math.round(p.updated_at)}`, controls: true, preload: "metadata", playsinline: true }),
    h("ul", { class: "facts" },
      h("li", {}, h("b", {}, clock(p.output.duration)), h("span", { class: "muted" }, ` из ${clock(p.sources.head.duration)} записи`)),
      h("li", {}, h("b", {}, String(p.plan?.cuts ?? 0)), h("span", { class: "muted" }, " вырезок")),
      h("li", {}, h("b", {}, String(p.output.segments)), h("span", { class: "muted" }, " склеек")),
    ),
    fixed.length ? h("p", {}, `Проверка нашла и вырезала ещё ${fixed.length}: `, fixed.map((i) => i.reason || kindLabel(i.kind)).join("; ")) : rounds.length ? h("p", { class: "muted" }, "Проверка готового ролика ничего не нашла.") : null,
    tech.length ? h("div", { class: "callout" }, "Длинные паузы в результате — посмотри эти места:\n", tech.join("\n")) : null,
    p.motion?.skipped ? h("p", { class: "muted" }, `Моушн-графика: ${p.motion.skipped}`) : null,
    p.motion?.inserts
      ? h("p", { class: "muted" }, `Моушн-вставок: ${p.motion.inserts}${p.motion.failed ? `, не отрисовалось: ${p.motion.failed}` : ""} — `,
          (p.motion.items || []).map((i) => `${clock(i.at)} ${i.fields.title || i.fields.value || i.fields.text}`).join(" · "))
      : null,
    h("div", { class: "toolbar" },
      h("a", { class: "btn btn--primary", href: `${p.video_url}?download=1` }, "Скачать mp4"),
      h("a", { class: "btn btn--secondary", href: p.srt_url }, "Субтитры .srt"),
    ),
  );
}

// ---------- план: расшифровка с вырезками ----------

let player = null;

function seek(t) {
  if (!player) return;
  player.currentTime = Math.max(0, t - 0.3);
  player.play().catch(() => {});
}

const saveLater = debounce(() => savePlan(), 500);

async function savePlan() {
  const p = state.project;
  state.saving = "Сохраняю…";
  renderSaveState();
  try {
    await api("POST", `${API}/projects/${p.id}/plan`, { cuts: state.plan.cuts, layout: state.plan.layout });
    state.plan = await api("GET", `${API}/projects/${p.id}/plan`);
    state.project = await api("GET", `${API}/projects/${p.id}`);
    state.planFor = `${state.project.id}:${state.project.updated_at}`;
    state.saving = "";
    renderedKey = "";
    renderProject();
  } catch (err) {
    state.saving = "";
    setBanner(err.message, "error");
  }
}

function renderSaveState() {
  const node = document.getElementById("save-state");
  if (node) node.textContent = state.saving || "Правки сохраняются сами";
}

function edit(change) {
  state.plan = { ...state.plan, ...change(state.plan) };
  state.saving = "Есть несохранённые правки…";
  renderTranscript();
  renderSaveState();
  saveLater();
}

function renderTranscript() {
  const box = document.getElementById("transcript");
  if (!box) return;
  const plan = state.plan;
  const map = cutOf(plan.cuts, plan.words.length);
  const layouts = state.config.layouts;
  const rows = [];
  for (const ph of phrases(plan.words)) {
    if (ph.gapBefore >= 1 && ph.first > 0) rows.push(h("div", { class: "pause" }, `пауза ${ph.gapBefore.toFixed(1)} с`));
    let allCut = true;
    const words = [];
    for (let i = ph.first; i <= ph.last; i++) {
      const n = map[i];
      if (n < 0) allCut = false;
      const cut = n >= 0 ? plan.cuts[n] : null;
      words.push(h("span", {
        class: `word${cut ? ` cut k-${cut.kind}` : ""}`,
        title: cut ? `${kindLabel(cut.kind)}${cut.reason ? `: ${cut.reason}` : ""} — клик вернёт` : "Клик — вырезать, Shift+клик — до этого слова",
        onclick: (e) => edit((pl) => {
          const cuts = toggleWord(pl.cuts, i, { extend: e.shiftKey, last: state.lastCut });
          state.lastCut = cuts.find((c) => c.from <= i && i <= c.to && c.kind === "human") || null;
          return { cuts };
        }),
      }, plan.words[i].w), " ");
    }
    const mode = layoutAt(plan.layout, ph.first);
    const select = plan.has_screen
      ? h("select", { "aria-label": "План с этой фразы", onchange: (e) => edit((pl) => ({ layout: setLayout(pl.layout, ph.first, e.target.value) })) },
          ...layouts.map((l) => h("option", { value: l.id, selected: l.id === mode }, l.label)))
      : null;
    rows.push(h("div", { class: `phrase${allCut ? " out" : ""}` },
      h("button", { type: "button", class: "phrase-time", title: "Послушать в записи", onclick: () => seek(ph.start) }, clock(ph.start)),
      select,
      h("div", { class: "words" }, ...words),
      h("button", { type: "button", class: "btn btn--link btn--sm", onclick: () => edit((pl) => ({ cuts: togglePhrase(pl.cuts, ph) })) }, allCut ? "Вернуть" : "Вырезать"),
    ));
  }
  fill(box, ...rows);
}

function review(p) {
  const plan = state.plan;
  const s = plan.summary;
  const feedback = h("textarea", { rows: 3, maxlength: String(state.config.max_feedback), placeholder: "Что не так с монтажом: «оставляй „вот“, когда я показываю на экран», «режь дубли жёстче»… ИИ переделает план и запомнит правило." });
  const busy = isActive(p);
  player = p.sources_url?.head ? h("video", { class: "player player--source", src: p.sources_url.head, controls: true, preload: "metadata", playsinline: true }) : null;
  const byKind = Object.entries(s.by_kind || {}).map(([k, n]) => `${kindLabel(k)} — ${n}`).join(", ");
  const panel = h("section", { class: "panel stack" },
    h("div", { class: "field-head" },
      h("h2", {}, p.status === "done" ? "План монтажа" : "Проверь план монтажа"),
      h("span", { class: "muted save-state", id: "save-state" }, state.saving || "Правки сохраняются сами"),
    ),
    h("ul", { class: "facts" },
      h("li", {}, h("b", {}, `${clock(s.source_s)} → ${clock(s.result_s)}`)),
      h("li", {}, h("b", {}, String(s.cuts)), h("span", { class: "muted" }, ` вырезок${byKind ? ` (${byKind})` : ""}`)),
      h("li", {}, h("b", {}, String(s.segments)), h("span", { class: "muted" }, " кусков")),
    ),
    plan.notes ? h("div", { class: "callout" }, plan.notes) : null,
    player,
    h("p", { class: "muted small" }, "Зачёркнутое вырежется. Клик по слову — вырезать или вернуть, Shift+клик — растянуть свою вырезку, время слева — послушать в записи. Паузы длиннее заданной ужмутся сами."),
    h("div", { class: "transcript", id: "transcript" }),
    h("div", { class: "toolbar" },
      h("button", { type: "button", class: "btn btn--primary", disabled: busy, onclick: () => act(p, "render") }, p.status === "done" ? "Пересобрать" : "Собрать ролик"),
      h("button", { type: "button", class: "btn btn--secondary", disabled: busy, onclick: () => act(p, "replan") }, "Переделать план заново"),
    ),
    h("label", { class: "field" }, h("span", { class: "field-label" }, "Замечание к монтажу"), feedback),
    h("div", { class: "toolbar" },
      h("button", { type: "button", class: "btn btn--secondary", disabled: busy, onclick: () => feedback.value.trim() ? act(p, "revise", { text: feedback.value }) : feedback.focus() }, "Переделать с замечанием"),
    ),
  );
  queueMicrotask(renderTranscript);
  return p.status === "done" ? h("details", { class: "disclosure" }, h("summary", {}, "План монтажа — посмотреть и поправить"), panel) : panel;
}

// ---------- правила ----------

async function openRules() {
  let rules;
  try {
    rules = await api("GET", `${API}/rules`);
  } catch (err) {
    return setBanner(err.message, "error");
  }
  const text = h("textarea", {}, rules.lessons);
  const error = h("p", { class: "form-error", role: "alert", hidden: true });
  const save = h("button", { type: "submit", class: "btn btn--primary" }, "Сохранить");
  const body = h("form", { class: "rules-form", novalidate: true },
    h("p", { class: "muted" }, "Сюда сами дописываются выводы из твоих замечаний — так следующий ролик не повторит старых ошибок. Раздел «Термины» подсказывает расшифровке английские слова и имена."),
    text,
    h("details", { class: "disclosure" }, h("summary", {}, "Базовые правила (в коде платформы)"), h("pre", {}, rules.base)),
    error, save,
  );
  const dialog = modal("Правила монтажа", body);
  body.addEventListener("submit", async (e) => {
    e.preventDefault();
    save.disabled = true;
    try {
      await api("POST", `${API}/rules`, { lessons: text.value });
      dialog.close();
      toast("Правила сохранены");
    } catch (err) {
      error.textContent = err.message;
      error.hidden = false;
      save.disabled = false;
    }
  });
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
  const installed = cfg.models.filter((m) => m.installed);
  $("model-chip").textContent = cfg.backend === "fake" ? "демо, без модели" : installed.length ? installed.map((m) => m.label).join(" · ") : "расшифровка не установлена";
  if (cfg.backend === "fake") {
    configBanner = { kind: "warn", text: "Демо-режим: расшифровка и план — имитация по простым правилам, а не модели. Сборка ролика — настоящая." };
  } else if (!installed.length) {
    configBanner = { kind: "warn", text: `Модель расшифровки не установлена — монтировать нечем. ${cfg.install_hint}` };
  } else if (!cfg.llm_available) {
    configBanner = { kind: "warn", text: "Нет claude CLI — план монтажа строить нечем (AIP_LLM_CMD)." };
  }
  setBanner(null);
  $("new-project").addEventListener("click", newProject);
  $("rules-open").addEventListener("click", openRules);
  renderProject();
  listPoll.start();
}

init();
