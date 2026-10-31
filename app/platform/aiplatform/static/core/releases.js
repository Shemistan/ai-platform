// Выпуски студии в пространствах (PRD-033): создать выпуск и альбом выпусков — «Новости», «Статьи», «Сегодня».
// Своего API нет: создание — POST /api/studio/projects, альбом — доска (GET /api/studio/board), просмотр — выпуск.
import { album } from "/core/album.js";
import { minutes } from "/core/format.js";
import { api, h } from "/ui.js";

export const studioHref = (p) => `/studio/?scenario=${p.scenario}&project=${p.id}`;

const when = (ts) => new Date(ts * 1000).toLocaleDateString("ru", { day: "numeric", month: "long", year: "numeric" });

// Создать и запустить выпуск. Тот же материал уже есть (409) — { existing } вместо исключения.
export async function createRelease(payload) {
  try {
    return { project: await api("POST", "/api/studio/projects", { run: true, ...payload }) };
  } catch (err) {
    if (err.data?.existing) return { existing: err.data.existing, message: err.message };
    throw err;
  }
}

// Статья по ссылке: «Статьи» скачивают текст, из него — выпуск «Видео по статье», как в карточке статьи.
export async function articleFromUrl(url) {
  const card = await api("POST", "/api/library/articles", { url });
  if (!card.has_script) throw new Error("Текст скачан, но к озвучке не готов — открой его во вкладке «Статьи».");
  return createRelease({ scenario: "article", title: card.title, text: card.script, settings: { source_license: card.license || "none", source_url: url } });
}

const PUBLISHED = new Set(["published", "uploaded"]);

function card(item) {
  const p = item.project;
  const facts = [p.scenario_label, p.video_s ? minutes(p.video_s) : null, PUBLISHED.has(p.group) ? "опубликован" : p.group === "attention" ? "ждёт тебя" : null];
  return [
    h("div", { class: "album-thumb" }, p.poster ? h("img", { src: p.poster, alt: "", loading: "lazy" }) : null),
    h("span", { class: "album-caption" }, p.title),
    h("span", { class: "muted small" }, facts.filter(Boolean).join(" · ")),
  ];
}

async function view(item) {
  const p = await api("GET", `/api/studio/projects/${item.project.id}`);
  const files = p.files || {};
  const sent = (p.published || []).filter((r) => r.sent || r.url).map((r) => r.label || r.channel);
  return h(
    "article",
    { class: "album-item" },
    h(
      "div",
      { class: "album-media" },
      files.video
        ? h("video", { src: files.video, poster: files.poster || null, controls: true, preload: "metadata", playsinline: true })
        : files.poster
          ? h("img", { src: files.poster, alt: p.title })
          : h("p", { class: "muted" }, "Видео ещё не собрано."),
    ),
    h(
      "div",
      { class: "album-meta" },
      h("h3", {}, p.title),
      h("p", { class: "muted small" }, [p.scenario_label, when(p.created_at)].join(" · ")),
      sent.length ? h("p", { class: "small" }, `Опубликовано: ${sent.join(", ")}`) : null,
      p.artifacts?.post ? h("details", { class: "disclosure" }, h("summary", {}, "Подпись"), h("pre", { class: "muted source-text" }, p.artifacts.post)) : null,
      h(
        "div",
        { class: "toolbar" },
        h("a", { class: "btn btn--primary btn--sm", href: studioHref(p) }, "Открыть в студии"),
        files.video ? h("a", { class: "btn btn--secondary btn--sm", href: `${files.video.split("?")[0]}?download=1` }, "Скачать видео") : null,
      ),
    ),
  );
}

// Альбом выпусков: scenarios — [{ id, label }] (вид элемента = сценарий); фильтр «Всё» + по сценарию.
export function releasesAlbum({ root, scenarios, empty }) {
  const ids = new Set(scenarios.map((s) => s.id));
  const load = async () => {
    const data = await api("GET", "/api/studio/board");
    return data.projects
      .filter((p) => ids.has(p.scenario))
      .map((p) => ({ id: p.id, kind: p.scenario, project: p }))
      .sort((a, b) => b.project.created_at - a.project.created_at);
  };
  const filters = scenarios.length > 1 ? [{ id: "all", label: "Всё" }, ...scenarios] : [{ id: "all", label: "Всё" }];
  return album({ root, filters, load, card, view, empty });
}
