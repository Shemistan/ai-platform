// Вкладка «Статьи»: библиотека статей движка — текст для озвучки, озвучка, проверка, канал.
import { $, narratorApi as api, chip, fill, h, minutes, shortDate, taskCard } from "/ui.js";

const API = "/api/library";
const QUALITY = { verbatim: "текст дословно", pasted: "текст прислан", webfetch: "текст по пересказу WebFetch" };
const state = { data: null, audio: null, card: null, selected: null, filter: "all", search: "", polling: false };

const isActive = (t) => t.status === "queued" || t.status === "running";

function plural(n, one, few, many) {
  const mod10 = n % 10;
  const mod100 = n % 100;
  if (mod10 === 1 && mod100 !== 11) return one;
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return few;
  return many;
}

function segmented(box, onPick) {
  for (const b of box.querySelectorAll("button")) {
    b.addEventListener("click", () => {
      for (const other of box.querySelectorAll("button")) other.setAttribute("aria-pressed", String(other === b));
      onPick(b.value);
    });
  }
}

function pressSegment(box, value) {
  for (const b of box.querySelectorAll("button")) b.setAttribute("aria-pressed", String(b.value === value));
}

// --- сводка и пачка ---

function pending(a) {
  return !a.has_script || a.stale;
}

function unsent(a) {
  return a.has_audio && !a.sent;
}

function renderStats() {
  const list = state.data.articles;
  const stats = [
    [list.length, plural(list.length, "статья", "статьи", "статей")],
    [list.filter((a) => a.has_audio).length, "озвучено"],
    [list.filter((a) => a.has_script && a.stale).length, "ждут озвучки"],
    [list.filter((a) => !a.has_script).length, "без текста для озвучки"],
    [state.data.unsent, "не ушли в канал"],
  ];
  fill($("stats"), ...stats.map(([n, label]) => h("div", { class: "stat" }, h("b", {}, String(n)), h("span", {}, label))));
  const waiting = list.filter((a) => a.has_script && a.stale).length;
  const running = state.data.tasks.some((t) => isActive(t) && (t.kind === "render" || t.kind === "unsent"));
  fill(
    $("bulk"),
    h("button", { type: "button", class: "primary", disabled: !waiting || running, onclick: () => render(false) }, waiting ? `Озвучить новые (${waiting})` : "Новых нет"),
    h("button", { type: "button", class: "small-btn", disabled: running || !list.length, onclick: () => render(true) }, "Переозвучить все"),
    h("button", { type: "button", class: "small-btn accent", disabled: !state.data.unsent || running, onclick: () => bulkUnsent("send") }, `Отправить неотправленные (${state.data.unsent})`),
    h("button", { type: "button", class: "small-btn", disabled: !state.data.unsent || running, onclick: () => bulkUnsent("mark") }, "Считать отправленными"),
    h("span", { class: "hint" }, `Озвучка — движком «Аудио» по умолчанию (${defaultEngineLabel()}), с проверкой распознаванием и отправкой в канал статей.`),
  );
  const recent = state.data.tasks.filter((t) => isActive(t) || Date.now() / 1000 - (t.finished_at || 0) < 600).slice(0, 4);
  fill($("tasks"), ...recent.map((t) => taskCard(t, { onCancel: cancelTask })));
}

function engineLabel(id) {
  return state.audio?.engines.find((e) => e.id === id)?.label || id;
}

function defaultEngineLabel() {
  return state.audio ? engineLabel(state.audio.default_engine) : "…";
}

async function render(everything) {
  const cfg = state.audio;
  await api("POST", `${API}/render`, { all: everything, engine: cfg?.default_engine });
  await refresh();
}

async function bulkUnsent(mode) {
  await api("POST", `${API}/unsent/${mode}`);
  await refresh();
}

async function cancelTask(task) {
  await api("POST", `${API}/tasks/${task.id}/cancel`);
  await refresh();
}

// --- список ---

function statusChips(a) {
  const out = [];
  if (!a.has_script) out.push(chip("нужен текст", "warn"));
  else if (a.stale) out.push(chip(a.has_audio ? "текст новее озвучки" : "ждёт озвучки", "info"));
  else out.push(chip("озвучена", "ok"));
  if (a.check !== null && a.check !== undefined) out.push(chip(`проверка ${a.check}%`, a.check >= 85 ? "ok" : "warn"));
  if (a.has_audio) out.push(a.sent ? chip("в канале", "ok") : chip("не в канале"));
  return out;
}

function renderList() {
  const q = state.search.trim().toLowerCase();
  const shown = state.data.articles.filter((a) => {
    if (state.filter === "pending" && !pending(a)) return false;
    if (state.filter === "unsent" && !unsent(a)) return false;
    return !q || a.title.toLowerCase().includes(q);
  });
  if (!shown.length) {
    const empty = state.data.articles.length ? "Под фильтр ничего не попало." : "Статей пока нет — добавь первую по ссылке или своим текстом.";
    fill($("articles"), h("p", { class: "empty" }, empty));
    return;
  }
  // Список перерисовывается целиком: фокус с клавиатуры возвращаем на ту же статью.
  const focused = document.activeElement?.closest?.("#articles .row-link")?.dataset.article;
  fill(
    $("articles"),
    h(
      "div",
      { class: "table-wrap" },
      h(
        "table",
        { class: "data-table" },
        h(
          "tbody",
          {},
          ...shown.map((a) =>
            h(
              "tr",
              // Клик по строке — для мыши; с клавиатуры статью открывает кнопка-название (клик всплывает к строке).
              { class: `clickable${a.id === state.selected ? " current" : ""}`, onclick: () => select(a.id) },
              h(
                "td",
                {},
                h("button", { type: "button", class: "row-link", "data-article": a.id, "aria-current": a.id === state.selected ? "true" : null }, a.title),
                h("span", { class: "sub" }, [shortDate(a.added), a.duration_sec ? minutes(a.duration_sec) : "", a.engine ? engineLabel(a.engine) : ""].filter(Boolean).join(" · ")),
                h("span", { class: "toolbar", style: "margin-top:var(--sp-2)" }, ...statusChips(a)),
              ),
            ),
          ),
        ),
      ),
    ),
  );
  if (focused) [...$("articles").querySelectorAll(".row-link")].find((b) => b.dataset.article === focused)?.focus();
}

// --- карточка ---

function section(n, title, ...children) {
  return h("div", { class: "card" }, h("div", { class: "card-head" }, h("h3", {}, `${n}. ${title}`)), ...children);
}

function engineSelect() {
  const cfg = state.audio;
  const engineEl = h("select", { "aria-label": "Движок" });
  const voiceEl = h("select", { "aria-label": "Голос" });
  if (!cfg) return { engineEl, voiceEl, value: () => ({}) };
  const fillVoices = () => {
    const e = cfg.engines.find((x) => x.id === engineEl.value);
    fill(voiceEl, ...e.voices.map((v) => h("option", { value: v, selected: v === e.default_voice }, v)));
  };
  fill(engineEl, ...cfg.engines.map((e) => h("option", { value: e.id, disabled: Boolean(e.problem), selected: e.id === cfg.default_engine }, e.problem ? `${e.label} — недоступен` : e.label)));
  engineEl.addEventListener("change", fillVoices);
  fillVoices();
  return { engineEl, voiceEl, value: () => ({ engine: engineEl.value, voice: voiceEl.value }) };
}

function renderCard() {
  const box = $("card");
  const c = state.card;
  if (!c) {
    fill(box, h("p", { class: "empty" }, "Выбери статью слева — здесь будут её текст, озвучка, проверка и отправка."));
    return;
  }
  const busy = (c.tasks || []).filter(isActive);
  const locked = busy.length > 0;
  const error = h("p", { class: "form-error", hidden: true });
  const fail = (err) => {
    error.textContent = err.message;
    error.hidden = false;
  };
  const run = (action, body = {}) => async () => {
    error.hidden = true;
    try {
      await api("POST", `${API}/articles/${encodeURIComponent(c.id)}/${action}`, body);
      await refresh();
    } catch (err) {
      fail(err);
    }
  };

  const title = h("input", { type: "text", value: c.title, "aria-label": "Название", class: "title-input" });
  const summary = h("input", { type: "text", value: c.summary || "", placeholder: "1–3 предложения: о чём статья и главный вывод" });
  const script = h("textarea", { rows: 12, class: "mono", placeholder: "Текст, который прочитает модель. Его можно взять из исходного или поручить агенту." });
  script.value = c.script || "";
  const save = async () => {
    error.hidden = true;
    try {
      state.card = await api("POST", `${API}/articles/${encodeURIComponent(c.id)}`, { title: title.value, summary: summary.value, script: script.value });
      await refresh();
    } catch (err) {
      fail(err);
    }
  };
  const fromSource = () => {
    script.value = (c.source || "")
      .split("\n")
      .filter((line) => !/^- (ссылка|автор|дата):/.test(line))
      .join("\n")
      .trim();
  };

  const { engineEl, voiceEl, value } = engineSelect();
  const send = h("input", { type: "checkbox" });
  const channel = h("select", { "aria-label": "Канал" }, h("option", { value: "" }, "канал статей (обычный)"), ...state.data.channels.map((name) => h("option", { value: name }, name)));
  const players = Object.entries(c.audio_urls || {}).map(([engine, url]) =>
    h("div", { class: "field" }, h("div", { class: "field-head" }, h("span", { class: "field-label" }, engineLabel(engine)),h("a", { class: "link", href: `${url}?download=1` }, "Скачать mp3")), h("audio", { controls: true, preload: "none", src: url })),
  );

  fill(
    box,
    h(
      "div",
      { class: "job" },
      title,
      h(
        "div",
        { class: "toolbar" },
        h("span", { class: "muted small" }, shortDate(c.added)),
        chip(QUALITY[c.source_quality] || c.source_quality),
        c.url ? h("a", { class: "link", href: c.url, target: "_blank", rel: "noopener" }, "оригинал ↗") : null,
      ),
      ...busy.map((t) => taskCard(t, { onCancel: cancelTask })),
      error,
      section(
        1,
        "Текст для озвучки",
        h("label", { class: "field" }, h("span", { class: "field-label" }, "Описание — в реестре и в посте канала"), summary),
        script,
        h(
          "div",
          { class: "toolbar" },
          h("button", { type: "button", class: "secondary", disabled: locked, onclick: save }, "Сохранить"),
          c.source ? h("button", { type: "button", class: "small-btn", disabled: locked, onclick: fromSource }, "Взять исходный текст") : null,
          c.has_source ? h("button", { type: "button", class: "small-btn accent", disabled: locked, onclick: run("prepare") }, "Подготовить агентом") : null,
        ),
        h("p", { class: "hint" }, "Агент пишет полный пересказ для уха по правилам озвучки и заполняет описание. Цифры и латиницу движок и так переведёт в произношение."),
        c.source ? h("details", { class: "inline" }, h("summary", {}, "Исходный текст (source.md)"), h("pre", { class: "preview-text" }, c.source)) : null,
      ),
      section(
        2,
        "Озвучка",
        ...players,
        players.length ? null : h("p", { class: "hint" }, c.has_script ? "Ещё не озвучена." : "Сначала нужен текст для озвучки."),
        h("div", { class: "settings" }, h("label", { class: "field" }, h("span", { class: "field-label" }, "Движок"), engineEl), h("label", { class: "field" }, h("span", { class: "field-label" }, "Голос"), voiceEl)),
        h("label", { class: "check" }, send, h("span", {}, "Сразу отправить в канал статей")),
        h(
          "div",
          { class: "toolbar" },
          h("button", { type: "button", class: "primary", disabled: locked || !c.has_script || !summary.value.trim(), onclick: () => run("synth", { ...value(), send: send.checked })() }, c.has_audio ? "Переозвучить" : "Озвучить"),
          !summary.value.trim() && c.has_script ? h("span", { class: "hint warn" }, "Нужно описание — сохрани его выше.") : null,
        ),
      ),
      section(
        3,
        "Проверка распознаванием",
        c.check !== null && c.check !== undefined
          ? h("div", { class: "check-head" }, h("span", { class: `score ${c.check >= 85 ? "ok" : "warn"}` }, `${c.check}%`), h("span", { class: "muted" }, "совпадение по словам"))
          : h("p", { class: "hint" }, "Озвучка распознаётся обратно в текст: так видно, где модель исказила слова."),
        c.check_report ? h("details", { class: "inline" }, h("summary", {}, "Отчёт проверки"), h("pre", { class: "preview-text" }, c.check_report)) : null,
        h("div", { class: "toolbar" }, h("button", { type: "button", class: "secondary", disabled: locked || !c.has_audio, onclick: run("verify") }, "Проверить")),
      ),
      section(
        4,
        "Публикация",
        h("div", { class: "toolbar" }, c.has_audio ? (c.sent ? chip("в канале", "ok") : chip("ещё не в канале")) : chip("нечего отправлять")),
        h("div", { class: "toolbar" }, channel, h("button", { type: "button", class: "secondary", disabled: locked || !c.has_audio, onclick: () => run("send", { channel: channel.value })() }, "Отправить")),
        h("p", { class: "hint" }, "Уходит mp3 с описанием статьи. Повторная отправка в тот же канал — по этой кнопке."),
        h("div", { class: "toolbar" }, h("button", { type: "button", class: "small-btn accent", disabled: !c.has_script, onclick: () => makeVideo(c, script.value) }, "Сделать видео по статье →")),
      ),
    ),
  );
}

async function makeVideo(c, text) {
  const project = await api("POST", "/api/studio/projects", { scenario: "article", title: c.title, text: text || c.script });
  location.href = `/studio/?scenario=article&project=${project.id}`;
}

async function select(id) {
  state.selected = id;
  renderList();
  try {
    state.card = await api("GET", `${API}/articles/${encodeURIComponent(id)}`);
  } catch (err) {
    state.card = null;
    fill($("card"), h("p", { class: "job-error" }, err.message));
    return;
  }
  history.replaceState(null, "", `?article=${encodeURIComponent(id)}`);
  renderCard();
}

// --- добавление ---

async function addArticle(payload) {
  const error = $("add-error");
  error.hidden = true;
  try {
    const card = await api("POST", `${API}/articles`, payload);
    await refresh();
    state.card = card;
    state.selected = card.id;
    renderList();
    renderCard();
    return true;
  } catch (err) {
    error.textContent = err.message;
    error.hidden = false;
    return false;
  }
}

// --- данные ---

async function refresh() {
  try {
    state.data = await api("GET", `${API}/state`);
    state.failures = 0;
  } catch (err) {
    state.failures = (state.failures || 0) + 1;
    if (state.data) fill($("tasks"), h("p", { class: "job-error" }, `Нет связи с сервисом (${err.message}) — обновлю, когда он вернётся.`));
    throw err;
  } finally {
    schedule();
  }
  renderStats();
  renderList();
  if (state.selected) {
    try {
      const fresh = await api("GET", `${API}/articles/${encodeURIComponent(state.selected)}`);
      const editing = document.activeElement && $("card").contains(document.activeElement);
      const changed = JSON.stringify(fresh.tasks) !== JSON.stringify(state.card?.tasks) || fresh.has_audio !== state.card?.has_audio || fresh.check !== state.card?.check;
      state.card = fresh;
      if (!editing || changed) renderCard();
    } catch {
      /* статья могла пропасть */
    }
  }
}

// Пока идёт задача — опрос раз в 1,5 с; после сбоя связи — с растущей паузой до 30 с, пока не ответит.
function schedule() {
  const active = state.data?.tasks.some(isActive);
  if ((active || state.failures) && !state.polling) {
    state.polling = true;
    setTimeout(
      () => {
        state.polling = false;
        refresh().catch(() => {});
      },
      state.failures ? Math.min(1500 * 2 ** state.failures, 30000) : 1500,
    );
  }
}

async function init() {
  segmented($("add-mode"), (mode) => {
    $("add-url").hidden = mode !== "url";
    $("add-text").hidden = mode !== "text";
  });
  segmented($("filter"), (value) => {
    state.filter = value;
    renderList();
  });
  $("search").addEventListener("input", () => {
    state.search = $("search").value;
    renderList();
  });
  $("add-url").addEventListener("submit", async (e) => {
    e.preventDefault();
    $("url-btn").disabled = true;
    $("url-btn").textContent = "Скачиваю…";
    if (await addArticle({ url: $("url").value })) $("url").value = "";
    $("url-btn").disabled = false;
    $("url-btn").textContent = "Скачать";
  });
  $("add-text").addEventListener("submit", async (e) => {
    e.preventDefault();
    const payload = { title: $("new-title").value, text: $("new-text").value, summary: $("new-summary").value, url: $("new-url").value };
    if (await addArticle(payload)) for (const id of ["new-title", "new-text", "new-summary", "new-url"]) $(id).value = "";
  });
  const params = new URLSearchParams(location.search);
  if (params.get("filter")) {
    state.filter = params.get("filter");
    pressSegment($("filter"), state.filter);
  }
  try {
    state.audio = await api("GET", "/api/audio/config");
  } catch {
    state.audio = null;
  }
  await refresh();
  if (params.get("article")) select(params.get("article"));
}

init().catch((err) => fill($("articles"), h("p", { class: "job-error" }, `Не удалось загрузить библиотеку: ${err.message}`)));
