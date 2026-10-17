// «Выпуски»: все выпуски всех сценариев по группам. У ждущего — что нужно и кнопка действия.
import { $, announce, api, busyClick, captureFocus, confirmPublish, fill, fmtNum, h, plural, poller, reconcile, restoreFocus, STATUS, statusChip, toneOf } from "/ui.js";

const API = "/api/studio";
const state = { archived: false, busy: new Set(), errors: {}, statuses: null };
// Дополнения над группами (сводка отклика и т.п.): sig(data) — подпись блока, build(data) → узел.
const extras = [{ key: "response", sig: (d) => [d.stats, d.response], build: (d) => responsePanel(d) }];

const href = (p) => `/studio/?scenario=${p.scenario}&project=${p.id}`;

function when(ts) {
  return new Date(ts * 1000).toLocaleString("ru", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}

// → true, если сервер принял действие.
async function act(project, path, body) {
  if (state.busy.has(project.id)) return false; // запрос по выпуску уже идёт — второй POST не шлём
  state.busy.add(project.id);
  delete state.errors[project.id];
  render(state.data);
  try {
    await api("POST", `${API}/projects/${project.id}/${path}`, body ?? {});
    return true;
  } catch (err) {
    state.errors[project.id] = err.message;
    return false;
  } finally {
    state.busy.delete(project.id);
    boardPoll.now();
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
  const out = [h("a", { class: "btn--link", href: href(p) }, "Открыть")];
  const need = p.attention;
  if (need?.step === "publish" && p.channels.length && p.verdict !== "не готово") {
    const names = p.channels.map((c) => c.label).join(", ");
    out.unshift(
      h(
        "button",
        { type: "button", class: "btn btn--secondary btn--sm", disabled: busy, title: names, onclick: () => publish(p) },
        p.channels.length === 1 ? `Опубликовать: ${names}…` : `Опубликовать в ${p.channels.length} ${plural(p.channels.length, "канал", "канала", "каналов")}…`,
      ),
    );
  } else if (p.group === "drafts" || need?.status === "failed") {
    out.unshift(h("button", { type: "button", class: "btn btn--ghost btn--sm", disabled: busy, onclick: () => act(p, "run", {}) }, "Запустить всё"));
  }
  if (p.running) out.push(h("button", { type: "button", class: "btn btn--link", disabled: state.busy.has(p.id), onclick: () => act(p, "cancel") }, "Остановить"));
  else
    out.push(
      h(
        "button",
        { type: "button", class: "btn btn--link", disabled: busy, onclick: () => act(p, "archive", { archived: !p.archived }) },
        p.archived ? "Вернуть из архива" : "В архив",
      ),
    );
  return h("div", { class: "actions" }, ...out);
}

// Полностью и с разрядами: «12 400», а не «12.4K» (TXT-002).
const number = (n) => fmtNum(Math.round(n));

const known = (v) => v !== null && v !== undefined;
const signed = (n) => `${n >= 0 ? "+" : ""}${number(n)}`;

// Куда ушёл выпуск и что там с ним: просмотры (всего и за 24ч/72ч/7д), прирост подписчиков за сутки
// после поста; руками — у пакетов и приватных каналов.
// Поля ввода с id: перерисовка карточки по опросу переносит набранное и фокус (reconcile в ui.js).
const fieldId = (kind, p, r) => `${kind}-${p.id}-${r.channel}`.replace(/[^\w-]/g, "_");

// Отправить значение поля; принято — поле очищается, чтобы перенос правок не вернул старый текст.
async function submitField(p, path, id, body) {
  if (await act(p, path, body)) {
    const el = document.getElementById(id);
    if (el) el.value = "";
  }
}

function reachRow(p, r) {
  const facts = [];
  if (known(r.views)) facts.push(`${number(r.views)} ${plural(r.views, "просмотр", "просмотра", "просмотров")}`);
  const spans = [["24ч", r.views_24h], ["72ч", r.views_72h], ["7д", r.views_7d]].filter(([, v]) => known(v));
  if (spans.length) facts.push(spans.map(([k, v]) => `${k}: ${number(v)}`).join(", "));
  else if (known(r.views) && known(r.age_h)) facts.push("окно 24ч не закрыто");
  if (known(r.growth_24h_attr)) facts.push(`${signed(r.growth_24h_attr)} ${plural(r.growth_24h_attr, "подписчик", "подписчика", "подписчиков")} за сутки${r.growth_trend ? "" : " (без тренда)"}`);
  // Пакет для ручной загрузки опубликован, только когда автор залил его и дал ссылку на ролик.
  if (r.state === "packaged") {
    const id = fieldId("link", p, r);
    const link = h("input", { type: "url", id, class: "views-input", placeholder: "ссылка на ролик", "aria-label": `Ссылка на ролик: ${r.label}` });
    return h(
      "li",
      {},
      h("span", {}, `⇪ ${r.label}: пакет готов — залей и вставь ссылку`),
      h(
        "span",
        { class: "manual-views" },
        link,
        h("button", { type: "button", class: "btn btn--ghost btn--sm", onclick: () => link.value.trim() && submitField(p, "uploaded", id, { channel: r.channel, url: link.value.trim() }) }, "Залил"),
      ),
    );
  }
  const manual = r.type === "manual" || (r.type === "telegram" && r.public === false);
  const id = fieldId("views", p, r);
  const input = h("input", { type: "number", min: "0", id, class: "views-input", placeholder: "просмотры", "aria-label": `Просмотры: ${r.label}` });
  return h(
    "li",
    {},
    h("span", {}, `✓ ${r.label}`),
    r.republish ? h("span", { class: "muted" }, " · опубликовано повторно") : null,
    r.external_url ? h("a", { class: "btn--link", href: r.external_url, target: "_blank", rel: "noopener" }, " ролик") : null,
    facts.length ? h("span", { class: "muted" }, ` · ${facts.join(" · ")}`) : null,
    manual
      ? h(
          "span",
          { class: "manual-views" },
          input,
          h("button", { type: "button", class: "btn btn--ghost btn--sm", onclick: () => input.value !== "" && submitField(p, "stats", id, { channel: r.channel, publication_id: r.publication_id, views: Number(input.value) }) }, "Сохранить"),
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
      class: "btn btn--ghost btn--sm",
      disabled: s.running,
      onclick: busyClick(async () => {
        try {
          await api("POST", `${API}/stats/refresh`);
        } catch (err) {
          $("error").textContent = err.message;
          $("error").hidden = false;
        }
        boardPoll.now();
      }, "stats-refresh"),
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

// Тон карточки — из общего словаря статусов (toneOf): ждёт тебя — warning, шаг упал — danger,
// идёт — info, опубликован — success, остальные группы — neutral.
const GROUP_STATUS = { running: "running", published: "done" };
function cardTone(p) {
  if (p.group === "attention") return toneOf(p.attention?.status === "failed" ? "failed" : "waiting");
  return toneOf(GROUP_STATUS[p.group]);
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
    { class: `board-card tone-${cardTone(p)}` },
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

// Подпись карточки: всё, что она показывает. Совпала — узел (и набранное в его полях) остаётся как есть.
const cardSig = (p) => JSON.stringify([p, state.busy.has(p.id), state.errors[p.id] || null]);

// Перерисовка по опросу не пересобирает неизменившиеся блоки, а у изменившихся переносит
// набранное в полях и фокус: ввод просмотров и ссылки «Залил» переживает тики (FE-012).
// Кнопки «новый выпуск» по сценариям — из ответа сервера: названия сценариев живут только там (FE-007).
function renderNewLinks(data) {
  const box = $("new-links");
  if (box.childElementCount || !data.scenarios) return;
  fill(box, ...data.scenarios.map((s, i) => h("a", { class: i ? "btn btn--secondary" : "btn btn--primary", href: `/studio/?scenario=${s.id}` }, s.label)));
}

function render(data) {
  if (!data) return;
  state.data = data;
  renderNewLinks(data);
  const saved = captureFocus(document.body);
  reconcile(
    $("extra"),
    extras.map(({ key, sig, build }) => ({ key, sig: JSON.stringify(sig(data)), build: () => build(data) })),
    "extra",
  );
  const groups = data.groups
    .map(([id, label]) => [id, label, data.projects.filter((p) => p.group === id)])
    .filter(([, , items]) => items.length);
  const root = $("groups");
  if (!groups.length) {
    fill(root, h("div", { class: "panel" }, h("p", { class: "muted" }, state.archived ? "В архиве пусто." : "Выпусков пока нет — начни с дайджеста или статьи.")));
  } else {
    const scope = state.archived ? "archived" : "active";
    reconcile(
      root,
      groups.map(([id, label]) => ({
        key: `group-${id}`,
        sig: JSON.stringify([label]),
        build: () => h("section", { class: "board-group", id: `group-${id}` }, h("h2", {}, label, h("span", { class: "muted small count" })), h("div", { class: "board-cards" })),
      })),
      scope,
    );
    for (const [id, , items] of groups) {
      const section = root.querySelector(`:scope > [data-key="group-${id}"]`);
      section.querySelector(".count").textContent = String(items.length);
      reconcile(
        section.querySelector(".board-cards"),
        items.map((p) => ({ key: p.id, sig: cardSig(p), build: () => card(p) })),
        scope,
      );
    }
  }
  restoreFocus(saved);
}

// Опрос доски: раз в 2 с, пока что-то идёт, раз в 8 с в простое (poller из ui.js: в скрытой
// вкладке — раз в минуту, после сбоя — реже). Свои действия зовут boardPoll.now().
const boardPoll = poller({
  run: load,
  fast: 2000,
  slow: 8000,
  isActive: () => state.data?.projects.some((p) => p.running),
  onError: (err) => {
    $("error").textContent = `Доска не загрузилась: ${err.message}`;
    $("error").hidden = false;
  },
});

async function load(signal) {
  const data = await api("GET", `${API}/board${state.archived ? "?archived=1" : ""}`, undefined, { signal });
  announceChanges(data);
  render(data);
  $("error").hidden = true;
}

$("show-archived").addEventListener("change", (e) => {
  state.archived = e.target.checked;
  boardPoll.now();
});

boardPoll.start();
