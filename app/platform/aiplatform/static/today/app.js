// «Сегодня» (UX-031): одно поле «что делаем», до трёх выпусков, которые ждут тебя, и что идёт сейчас.
// Своего API нет — страница собирает существующие: доска студии, /api/status, «Статьи», «Монтаж».
import { activityLine } from "/core/activity.js";
import { greeting, guessIntent, pickAttention } from "/core/today.js";
import { $, api, fill, h, poller, uploadFile } from "/ui.js";

const studioHref = (p) => `/studio/?scenario=${p.scenario}&project=${p.id}`;
const state = { file: null, intent: null };

$("greeting").textContent = greeting(new Date().getHours());

function showError(message, ...extra) {
  const box = $("error");
  box.replaceChildren(message || "", ...extra);
  box.hidden = !message;
}

// Предложение под полем: что сделаем с введённым — одна строка и одна кнопка.
function suggest() {
  state.intent = guessIntent(state.file || $("what").value);
  const intent = state.intent;
  $("suggest").textContent = intent ? intent.label : "Ссылка — видео по статье, короткий текст — пост, видео — монтаж.";
  $("go").hidden = !intent?.action;
  $("go").textContent = intent?.action || "";
  showError("");
}

// Создать выпуск студии; тот же материал уже есть (409) — ссылка на него вместо дубля.
async function createProject(payload) {
  try {
    return await api("POST", "/api/studio/projects", { run: true, ...payload });
  } catch (err) {
    const existing = err.data?.existing;
    if (!existing) throw err;
    showError(`${err.message} `, h("a", { href: studioHref(existing) }, "Открыть"));
    return null;
  }
}

// Ссылка: «Статьи» скачивают текст, из него — выпуск «Видео по статье», как кнопка в карточке статьи.
async function fromUrl(url) {
  const card = await api("POST", "/api/library/articles", { url });
  if (!card.has_script) {
    showError("Текст скачан, но к озвучке не готов. ", h("a", { href: "/library/" }, "Открыть в «Статьях»"));
    return null;
  }
  const settings = { source_license: card.license || "none", source_url: url };
  return createProject({ scenario: "article", title: card.title, text: card.script, settings });
}

// Видео: новый проект «Монтажа» с умолчаниями и файл «головой».
async function fromVideo(file) {
  const cfg = await api("GET", "/api/montage/config");
  const title = file.name.replace(/\.[^.]+$/, "");
  const project = await api("POST", "/api/montage/projects", { title, options: cfg.defaults });
  $("go").textContent = "Загружаю…";
  await uploadFile(`/api/montage/projects/${project.id}/sources/head`, file, {
    limit: cfg.max_upload_bytes,
    onProgress: ({ percent }) => ($("go").textContent = `Загружаю… ${percent}%`),
  });
  location.href = `/montage/?id=${project.id}`;
}

async function go(event) {
  event.preventDefault();
  const intent = state.intent;
  if (!intent?.action) return;
  const text = $("what").value.trim();
  $("go").disabled = true;
  try {
    let project = null;
    if (intent.kind === "montage") return await fromVideo(state.file);
    if (intent.kind === "url") project = await fromUrl(text);
    if (intent.kind === "post") project = await createProject({ scenario: "post", title: "", post: text, text });
    if (intent.kind === "article") project = await createProject({ scenario: "article", title: "", text });
    if (project) location.href = studioHref(project);
  } catch (err) {
    showError(err.message);
  } finally {
    $("go").disabled = false;
    if (intent.action) $("go").textContent = intent.action;
  }
}

function takeFile(file) {
  state.file = file || null;
  if (file) $("what").value = "";
  suggest();
}

const form = $("input");
form.addEventListener("submit", go);
$("what").addEventListener("input", () => {
  state.file = null;
  suggest();
});
$("pick").addEventListener("click", () => $("file").click());
$("file").addEventListener("change", () => takeFile($("file").files[0]));
form.addEventListener("dragover", (e) => (e.preventDefault(), form.classList.add("drag")));
form.addEventListener("dragleave", () => form.classList.remove("drag"));
form.addEventListener("drop", (e) => {
  e.preventDefault();
  form.classList.remove("drag");
  takeFile(e.dataTransfer.files[0]);
});

// «Ждут тебя»: до трёх карточек, у каждой одно действие — открыть выпуск.
function card(p) {
  return h(
    "article",
    { class: "today-card" },
    p.poster ? h("img", { src: p.poster, alt: "" }) : h("div", { class: "ph" }),
    h("div", {}, h("h3", {}, p.title), h("p", {}, p.attention?.text || p.scenario_label)),  // attention — {step, label, text}
    h("a", { class: "btn btn--secondary btn--sm", href: studioHref(p) }, "Открыть"),
  );
}

async function loadBoard(signal) {
  const data = await api("GET", "/api/studio/board", undefined, { signal });
  const { shown, more } = pickAttention(data.projects);
  fill($("waiting"), shown.length ? shown.map(card) : h("p", { class: "muted" }, "Ничего не ждёт — можно начинать новое."));
  $("all").textContent = more ? `Все выпуски — ещё ${more} ждут тебя` : "Все выпуски";
}

async function loadRunning(signal) {
  const { activity = [] } = await api("GET", "/api/status", undefined, { signal });
  $("running-block").hidden = !activity.length;
  fill(
    $("running"),
    activity.map((item) =>
      h("li", {}, h("a", { href: item.href }, h("strong", {}, `${item.tab} · ${item.title || "без названия"}`), h("span", { class: "muted small" }, activityLine(item)))),
    ),
  );
}

poller({ run: loadBoard, fast: 15000, slow: 15000 }).start();
poller({ run: loadRunning, fast: 3000, slow: 6000, isActive: () => !$("running-block").hidden }).start();
suggest();
