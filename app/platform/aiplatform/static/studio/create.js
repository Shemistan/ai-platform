// Студия: форма нового выпуска, оценка времени и проверка до запуска, серия и копия выпуска.
import { $, api, busyClick, confirmAction, countLabel, debounce, fill, fmtNum, h } from "/ui.js";
import { API, CAPTION_LIMIT, scenario, state } from "./state.js";
import { readSettings, settingsForm } from "./settings.js";
import { act, alertBox, loadProjects, open, show } from "./app.js";

function duration(s) {
  if (!s) return "меньше минуты";
  const m = Math.round(s / 60);
  if (m < 1) return "меньше минуты";
  return m >= 60 ? `${Math.floor(m / 60)} ч ${m % 60} мин` : `${m} мин`;
}

// «≈ 1 ч 40 мин: картинки 18 шт. — 36 мин, озвучка — 50 мин…» и предупреждения: что сломается или будет ждать.
export function checkBox(data, { done = false } = {}) {
  if (!data) return null;
  const { estimate: est, preflight = [] } = data;
  const parts = (est?.parts || []).map((p) => `${p.label}${p.count ? ` (${p.count})` : ""} — ${duration(p.seconds)}`);
  const head = done || !est?.parts?.length ? null : h("p", {}, h("strong", {}, `≈ ${duration(est.total_s)}`), h("span", { class: "muted" }, ` до готового видео${est.video_s ? `, ролик ~${duration(est.video_s)}` : ""}`));
  const items = preflight.map((x) => h("li", { class: x.level }, x.text));
  if (!head && !items.length) return null;
  return h(
    "div",
    { class: "check-box" },
    head,
    parts.length && !done ? h("details", { class: "disclosure" }, h("summary", {}, "Из чего складывается"), h("ul", { class: "small" }, ...parts.map((t) => h("li", {}, t))), h("p", { class: "hint" }, est.from_history ? `По истории задач этой машины (${est.from_history}).` : "Истории задач ещё нет — оценка по умолчанию.")) : null,
    items.length ? h("ul", { class: "preflight" }, ...items) : null,
  );
}

export const refreshCreateCheck = debounce(async () => {
  const box = $("create-check");
  if (!box) return;
  const payload = { scenario, settings: readSettings("new") };
  if (scenario !== "digest") Object.assign(payload, { text: $("text")?.value || "", post: $("post")?.value || "" });
  try {
    box.replaceChildren(checkBox(await api("POST", `${API}/preflight`, payload)) || "");
  } catch {
    box.replaceChildren();
  }
}, 400);

export function renderCreate() {
  const yesterday = new Date(Date.now() - 86400000).toISOString().slice(0, 10);
  $("source-fields").replaceChildren(
    ...(scenario === "digest"
      ? [
          h(
            "label",
            { class: "field" },
            h("span", { class: "field-label" }, "День выпуска"),
            h("input", { type: "date", id: "day", value: yesterday, max: new Date().toISOString().slice(0, 10) }),
            h("span", { class: "hint" }, "narrator обойдёт источники и напишет пересказ дня — это занимает несколько минут."),
          ),
        ]
      : scenario === "monthly"
        ? [h("label", { class: "field" }, h("span", { class: "field-label" }, "Месяц обзора"), h("input", { type: "month", id: "month", value: new Date().toISOString().slice(0, 7), max: new Date().toISOString().slice(0, 7) }))]
      : scenario === "weekly"
        ? [h("p", { class: "hint" }, "Обзор текущей недели: агент сам отберёт события — это занимает десятки минут.")]
      : scenario === "post"
        ? postFields()
        : [
          h("label", { class: "field" }, h("span", { class: "field-label" }, "Заголовок"), h("input", { type: "text", id: "title", placeholder: "необязательно — возьмём из текста" })),
          h(
            "label",
            { class: "field" },
            h("span", { class: "field-label" }, "Текст статьи"),
            h("textarea", { id: "text", rows: 10, placeholder: "Готовый текст для озвучки. Пустая строка — новый абзац; слайды привязываются к абзацам." }),
          ),
        ]),
  );
  const defaults = { ...state.config.defaults, ...(state.config.scenario_defaults?.[scenario] || {}) };
  fill(
    $("create-settings"),
    // Откуда умолчания (UX-034): у дайджеста — общие с расписанием, правятся в «Настройках».
    scenario === "digest"
      ? h("p", { class: "hint" }, "Умолчания дайджеста — общие с расписанием, меняются в ", h("a", { href: "/settings/" }, "«Настройках»"), ". Здесь — только для этого выпуска.")
      : null,
    settingsForm(defaults, "new"),
  );
  refreshCreateCheck();
}

// Подпись поста со счётчиком: Telegram берёт под видео до 1024 знаков.
export function captionCounter(area) {
  const count = h("span", { class: "hint" });
  const update = () => {
    const visible = area.value.replace(/\*\*/g, "").replace(/\[([^\]]+)\]\([^)]+\)/g, "$1").length;
    count.textContent = `${countLabel(visible, "знак", "знака", "знаков")} из ${fmtNum(CAPTION_LIMIT)}${visible > CAPTION_LIMIT ? " — длинно: вместо подписи уйдёт заголовок" : ""}`;
    count.classList.toggle("warn", visible > CAPTION_LIMIT);
  };
  area.addEventListener("input", update);
  update();
  return count;
}

function postFields() {
  const post = h("textarea", { id: "post", rows: 7, placeholder: "**Жирный** и [ссылка](https://…) — как в post.md дайджеста." });
  return [
    h("label", { class: "field" }, h("span", { class: "field-label" }, "Заголовок"), h("input", { type: "text", id: "title", placeholder: "необязательно — из первой строки подписи" })),
    h("label", { class: "field" }, h("span", { class: "field-label" }, "Подпись к посту"), post, captionCounter(post)),
    h(
      "details",
      { class: "disclosure" },
      h("summary", {}, "Текст для озвучки — если озвучиваем здесь"),
      h("textarea", { id: "text", rows: 5, placeholder: "Пусто — озвучим подпись. С «Загружу свой mp3» не нужен." }),
    ),
    h("p", { class: "hint" }, "Постер и mp3 перетащишь в шаги выпуска после создания."),
  ];
}

export async function create(event, forceNew = false) {
  event?.preventDefault();
  const error = $("create-error");
  error.hidden = true;
  const payload = { scenario, settings: readSettings("new"), run: true, force_new: forceNew };
  if (scenario === "digest") payload.day = $("day").value;
  else if (scenario === "monthly") payload.month = $("month").value;
  else if (scenario === "post") Object.assign(payload, { title: $("title").value, post: $("post").value, text: $("text").value });
  else if (scenario !== "weekly") Object.assign(payload, { title: $("title").value, text: $("text").value });
  $("create-btn").disabled = true;
  try {
    const project = await api("POST", `${API}/projects`, payload);
    if (scenario !== "digest") for (const id of ["text", "post"]) if ($(id)) $(id).value = "";
    await loadProjects();
    show(project);
  } catch (err) {
    const existing = err.data?.existing;
    // Тот же день или тот же текст: открыть готовый выпуск вместо новой генерации — или всё же создать новый.
    error.replaceChildren(
      err.message,
      ...(existing
        ? [
            " ",
            h("button", { type: "button", class: "btn btn--link", onclick: () => (existing.scenario === scenario ? open(existing.id) : (location.href = `?scenario=${existing.scenario}&project=${existing.id}`)) }, "Открыть"),
            " · ",
            h("button", { type: "button", class: "btn btn--link", onclick: busyClick(() => create(null, true)) }, existing.similar ? "Создать всё равно" : "Создать новый"),
          ]
        : []),
    );
    error.hidden = false;
  } finally {
    $("create-btn").disabled = false;
  }
}

export async function makeFinal() {
  if (await act("settings", { draft: false })) await act("run", {});
}

// Длинная статья: серия выпусков по главам или выжимка на 5–8 минут — отдельным выпуском.
export function longTextActions(project) {
  if (project.scenario !== "article" || !project.preflight?.some((x) => x.text.includes("серии"))) return null;
  const summary = project.summary || {};
  return h(
    "div",
    { class: "actions" },
    h("button", { type: "button", class: "btn btn--secondary", disabled: project.running, onclick: busyClick(splitProject) }, "Разбить на серию"),
    h("button", { type: "button", class: "btn btn--secondary", disabled: summary.status === "running", onclick: busyClick(() => act("summary")) }, summary.status === "running" ? "Выжимка пишется…" : "Сделать выжимку"),
    summary.status === "done" ? h("a", { class: "btn--link", href: `?scenario=article&project=${summary.project}` }, "Открыть выжимку") : null,
    summary.status === "failed" ? h("span", { class: "job-error" }, summary.error) : null,
  );
}

async function splitProject() {
  try {
    const { projects } = await api("POST", `${API}/projects/${state.project.id}/split`, {});
    await loadProjects();
    await open(projects[0].id);
  } catch (err) {
    alertBox(err.message);
  }
}

// Копия — вариант оригинала (AN-009/AN-019): «что меняли» уходит в variant, доска сравнивает варианты.
export async function cloneProject() {
  const what = h("input", { type: "text", id: "variant-what", maxlength: "200", placeholder: "например: другая обложка, короче на минуту", "aria-label": "Что меняли в копии" });
  const ok = await confirmAction({
    title: "Копия выпуска",
    body: [h("label", { class: "field-label", for: "variant-what" }, "Что меняем в копии (необязательно)"), what, h("p", { class: "hint" }, "На доске копии одного выпуска сравниваются рядом: просмотры и подписчики по вариантам.")],
    confirmLabel: "Сделать копию",
  });
  if (!ok) return;
  try {
    const variant = what.value.trim();
    const copy = await api("POST", `${API}/projects/${state.project.id}/clone`, variant ? { variant } : {});
    await loadProjects();
    await open(copy.id);
  } catch (err) {
    alertBox(err.message);
  }
}
