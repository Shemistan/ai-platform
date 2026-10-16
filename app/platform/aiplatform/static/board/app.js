// «Выпуски»: все выпуски всех сценариев по группам. У ждущего — что нужно и кнопка действия.
import { $, api, fill, h, statusChip } from "/ui.js";

const API = "/api/studio";
const state = { archived: false, timer: null, busy: new Set(), errors: {} };
// Дополнения над группами (сводка отклика и т.п.): функции data → узел.
const extras = [];

const href = (p) => `/studio/?scenario=${p.scenario}&project=${p.id}`;

function when(ts) {
  return new Date(ts * 1000).toLocaleString("ru", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}

async function act(project, path, body) {
  state.busy.add(project.id);
  delete state.errors[project.id];
  render(state.data);
  try {
    await api("POST", `${API}/projects/${project.id}/${path}`, body ?? {});
  } catch (err) {
    state.errors[project.id] = err.message;
  } finally {
    state.busy.delete(project.id);
    load();
  }
}

// Что можно сделать с выпуском прямо с доски.
function actions(p) {
  const busy = state.busy.has(p.id) || p.running;
  const out = [h("a", { class: "link", href: href(p) }, "Открыть")];
  const need = p.attention;
  if (need?.step === "publish" && p.channels.length && p.verdict !== "не готово") {
    const names = p.channels.map((c) => c.label).join(", ");
    out.unshift(
      h(
        "button",
        { type: "button", class: "small-btn accent", disabled: busy, title: names, onclick: () => act(p, "publish", { channels: p.channels.map((c) => c.id) }) },
        p.channels.length === 1 ? `Опубликовать: ${names}` : `Опубликовать в ${p.channels.length} канала`,
      ),
    );
  } else if (p.group === "drafts" || need?.status === "failed") {
    out.unshift(h("button", { type: "button", class: "small-btn", disabled: busy, onclick: () => act(p, "run", {}) }, "Запустить всё"));
  }
  if (p.running) out.push(h("button", { type: "button", class: "link", onclick: () => act(p, "cancel") }, "Остановить"));
  else
    out.push(
      h(
        "button",
        { type: "button", class: "link", disabled: busy, onclick: () => act(p, "archive", { archived: !p.archived }) },
        p.archived ? "Вернуть из архива" : "В архив",
      ),
    );
  return h("div", { class: "actions" }, ...out);
}

function card(p) {
  const need = p.attention;
  const meta = [
    h("span", {}, p.scenario_label),
    h("span", {}, when(p.updated_at)),
    p.running && p.current ? h("span", {}, `сейчас: ${p.current}`) : null,
    p.published.length ? h("span", {}, `✓ ${p.published.map((r) => r.label).join(", ")}`) : null,
    p.verdict ? h("span", {}, `судья: ${p.verdict}`) : null,
  ];
  return h(
    "article",
    { class: `board-card ${p.group}${need?.status === "failed" ? " failed" : ""}` },
    p.poster ? h("img", { class: "thumb", src: p.poster, alt: "" }) : h("div", { class: "thumb" }),
    h(
      "div",
      { class: "body" },
      h("div", { class: "meta" }, statusChip(p.status)),
      h("a", { class: "title", href: href(p) }, p.title),
      h("div", { class: "meta" }, ...meta),
      need ? h("div", { class: "need" }, h("strong", {}, `${need.label}: `), need.text || (need.status === "failed" ? "ошибка" : "ждёт тебя")) : null,
      state.errors[p.id] ? h("div", { class: "error", role: "alert" }, state.errors[p.id]) : null,
      actions(p),
    ),
  );
}

function render(data) {
  if (!data) return;
  state.data = data;
  fill($("extra"), ...extras.map((fn) => fn(data)));
  const groups = data.groups
    .map(([id, label]) => [id, label, data.projects.filter((p) => p.group === id)])
    .filter(([, , items]) => items.length);
  fill(
    $("groups"),
    groups.length
      ? groups.map(([id, label, items]) =>
          h("section", { class: "board-group", id: `group-${id}` }, h("h2", {}, label, h("span", { class: "muted small" }, String(items.length))), h("div", { class: "board-cards" }, ...items.map(card))),
        )
      : h("div", { class: "panel" }, h("p", { class: "muted" }, state.archived ? "В архиве пусто." : "Выпусков пока нет — начни с дайджеста или статьи.")),
  );
}

async function load() {
  clearTimeout(state.timer);
  try {
    render(await api("GET", `${API}/board${state.archived ? "?archived=1" : ""}`));
    $("error").hidden = true;
  } catch (err) {
    $("error").textContent = `Доска не загрузилась: ${err.message}`;
    $("error").hidden = false;
  }
  const running = state.data?.projects.some((p) => p.running);
  state.timer = setTimeout(load, running ? 2000 : 8000);
}

$("show-archived").addEventListener("change", (e) => {
  state.archived = e.target.checked;
  load();
});

load();
