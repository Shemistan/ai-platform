// «Выпуски»: все выпуски всех сценариев по группам. У ждущего — что нужно и кнопка действия.
import { $, announce, api, confirmPublish, fill, h, STATUS, statusChip } from "/ui.js";

const API = "/api/studio";
const state = { archived: false, timer: null, busy: new Set(), errors: {}, statuses: null };
// Дополнения над группами (сводка отклика и т.п.): функции data → узел.
const extras = [responsePanel];

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

// Публикация с доски необратима: только после подтверждения с каналами, подписью и вердиктом.
async function publish(p) {
  const ok = await confirmPublish({ channels: p.channels, caption: p.caption || "", verdict: p.verdict });
  if (ok) act(p, "publish", { channels: p.channels.map((c) => c.id) });
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
        { type: "button", class: "small-btn accent", disabled: busy, title: names, onclick: () => publish(p) },
        p.channels.length === 1 ? `Опубликовать: ${names}…` : `Опубликовать в ${p.channels.length} канала…`,
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

const number = (n) => (n >= 10000 ? `${(n / 1000).toFixed(n >= 100000 ? 0 : 1)}K` : String(n));

const known = (v) => v !== null && v !== undefined;
const signed = (n) => `${n >= 0 ? "+" : ""}${n}`;

// Куда ушёл выпуск и что там с ним: просмотры (всего и за 24ч/72ч/7д), прирост подписчиков за сутки
// после поста; руками — у пакетов и приватных каналов.
function reachRow(p, r) {
  const facts = [];
  if (known(r.views)) facts.push(`${number(r.views)} просм.`);
  const spans = [["24ч", r.views_24h], ["72ч", r.views_72h], ["7д", r.views_7d]].filter(([, v]) => known(v));
  if (spans.length) facts.push(spans.map(([k, v]) => `${k}: ${number(v)}`).join(", "));
  else if (known(r.views) && known(r.age_h)) facts.push("окно 24ч не закрыто");
  if (known(r.growth_24h_attr)) facts.push(`${signed(r.growth_24h_attr)} подп. за сутки${r.growth_trend ? "" : " (без тренда)"}`);
  // Пакет для ручной загрузки опубликован, только когда автор залил его и дал ссылку на ролик.
  if (r.state === "packaged") {
    const link = h("input", { type: "url", class: "views-input", placeholder: "ссылка на ролик", "aria-label": `Ссылка на ролик: ${r.label}` });
    return h(
      "li",
      {},
      h("span", {}, `⇪ ${r.label}: пакет готов — залей и вставь ссылку`),
      h(
        "span",
        { class: "manual-views" },
        link,
        h("button", { type: "button", class: "small-btn", onclick: () => link.value.trim() && act(p, "uploaded", { channel: r.channel, url: link.value.trim() }) }, "Залил"),
      ),
    );
  }
  const manual = r.type === "manual" || (r.type === "telegram" && r.public === false);
  const input = h("input", { type: "number", min: "0", class: "views-input", placeholder: "просмотры", "aria-label": `Просмотры: ${r.label}` });
  return h(
    "li",
    {},
    h("span", {}, `✓ ${r.label}`),
    r.republish ? h("span", { class: "muted" }, " · опубликовано повторно") : null,
    r.external_url ? h("a", { class: "link", href: r.external_url, target: "_blank", rel: "noopener" }, " ролик") : null,
    facts.length ? h("span", { class: "muted" }, ` · ${facts.join(" · ")}`) : null,
    manual
      ? h(
          "span",
          { class: "manual-views" },
          input,
          h("button", { type: "button", class: "small-btn", onclick: () => input.value !== "" && act(p, "stats", { channel: r.channel, publication_id: r.publication_id, views: Number(input.value) }) }, "Сохранить"),
        )
      : null,
  );
}

// «Что работает»: средние просмотры за 24ч и прирост за сутки по сценарию, длине и формату —
// только посты с закрытым окном 24ч.
function responsePanel(data) {
  const s = data.stats || {};
  const refresh = h(
    "button",
    {
      type: "button",
      class: "small-btn",
      disabled: s.running,
      onclick: async () => {
        try {
          await api("POST", `${API}/stats/refresh`);
        } catch (err) {
          $("error").textContent = err.message;
          $("error").hidden = false;
        }
        load();
      },
    },
    s.running ? "Обновляю…" : "Обновить отклик",
  );
  const tables = (data.response || []).map((d) =>
    h(
      "table",
      { class: "reach-table" },
      h("caption", {}, d.dimension),
      h("thead", {}, h("tr", {}, h("th", {}, ""), h("th", {}, "постов"), h("th", {}, "просмотров за 24ч"), h("th", {}, "подписчиков за сутки"))),
      h(
        "tbody",
        {},
        ...d.rows.map((r) => h("tr", {}, h("th", {}, r.label), h("td", {}, String(r.posts)), h("td", {}, number(r.avg_views)), h("td", {}, r.avg_growth === null ? "—" : signed(r.avg_growth)))),
      ),
    ),
  );
  return h(
    "section",
    { class: "panel response" },
    h("div", { class: "response-head" }, h("h2", {}, "Что работает"), refresh, h("span", { class: "muted small" }, s.at ? `обновлено ${s.at}` : "ещё не обновлялось")),
    tables.length
      ? h("div", { class: "reach-tables" }, ...tables)
      : h("p", { class: "hint" }, "Постов с закрытым окном 24ч пока нет. Публичные каналы Telegram подтягиваются сами (раз в сутки или кнопкой), у пакетов для YouTube и TikTok и у приватных каналов просмотры вносятся руками в карточке выпуска."),
  );
}

function card(p) {
  const need = p.attention;
  const meta = [
    h("span", {}, p.scenario_label),
    h("span", {}, when(p.updated_at)),
    p.running && p.current ? h("span", {}, `сейчас: ${p.current}`) : null,

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
      p.published.length ? h("ul", { class: "reach" }, ...p.published.map((r) => reachRow(p, r))) : null,
      state.errors[p.id] ? h("div", { class: "error", role: "alert" }, state.errors[p.id]) : null,
      actions(p),
    ),
  );
}

// Скринридеру — только смена статуса выпуска, не каждая перерисовка доски.
function announceChanges(data) {
  const now = Object.fromEntries(data.projects.map((p) => [p.id, p.status]));
  const before = state.statuses;
  state.statuses = now;
  if (!before) return;
  const changed = data.projects.filter((p) => p.id in before && before[p.id] !== p.status);
  if (changed.length) announce(changed.map((p) => `${p.title}: ${STATUS[p.status] || p.status}`).join(". "));
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
    const data = await api("GET", `${API}/board${state.archived ? "?archived=1" : ""}`);
    announceChanges(data);
    render(data);
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
