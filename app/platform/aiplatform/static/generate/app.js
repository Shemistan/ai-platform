// Пространство «Генерация» (PRD-033): входы в мастерские и альбом готовых фото, видео и аудио.
// Альбом собирается из очередей вкладок: GET /api/<вкладка>/jobs, только готовые.
import { album } from "/core/album.js";
import { api, h, poller } from "/ui.js";

const KINDS = {
  photo: { label: "Фото", url: (j) => j.image_url, title: (j) => j.prompt },
  video: { label: "Видео", url: (j) => j.video_url, title: (j) => j.prompt },
  audio: { label: "Аудио", url: (j) => j.audio_url, title: (j) => j.title || j.text },
};
const LIMIT = 60; // на вкладку: альбом — недавнее, вся история — в мастерской

const when = (ts) => new Date(ts * 1000).toLocaleString("ru", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });

async function load() {
  const lists = await Promise.all(
    Object.keys(KINDS).map((kind) =>
      api("GET", `/api/${kind}/jobs?limit=${LIMIT}`)
        .then(({ jobs }) => jobs.map((job) => ({ job, kind })))
        .catch(() => []),  // вкладка недоступна — альбом без неё, а не пустой
    ),
  );
  return lists
    .flat()
    .filter(({ job, kind }) => job.status === "done" && KINDS[kind].url(job))
    .map(({ job, kind }) => ({ id: `${kind}-${job.id}`, kind, job, at: job.finished_at || job.created_at }))
    .sort((a, b) => b.at - a.at);
}

function media(item, preview) {
  const url = KINDS[item.kind].url(item.job);
  if (item.kind === "photo") return h("img", { src: url, alt: preview ? "" : KINDS.photo.title(item.job) || "Картинка", loading: "lazy" });
  if (item.kind === "video") return h("video", { src: preview ? `${url}#t=0.1` : url, preload: "metadata", muted: preview, controls: !preview, playsinline: true });
  return preview ? h("div", { class: "album-audio" }, "♪") : h("audio", { src: url, controls: true, preload: "metadata" });
}

function card(item) {
  const title = KINDS[item.kind].title(item.job) || KINDS[item.kind].label;
  return [h("div", { class: "album-thumb" }, media(item, true)), h("span", { class: "album-caption" }, title), h("span", { class: "muted small" }, `${KINDS[item.kind].label} · ${when(item.at)}`)];
}

function view(item) {
  const job = item.job;
  const url = KINDS[item.kind].url(job);
  const facts = [job.model_label || job.model, job.width && job.height ? `${job.width}×${job.height}` : null, job.duration ? `${job.duration} с` : null, when(item.at)];
  return h(
    "article",
    { class: "album-item" },
    h("div", { class: "album-media" }, media(item, false)),
    h("div", { class: "album-meta" },
      h("p", {}, KINDS[item.kind].title(job) || ""),
      h("p", { class: "muted small" }, facts.filter(Boolean).join(" · ")),
      h("div", { class: "toolbar" },
        h("a", { class: "btn btn--primary btn--sm", href: url, download: "" }, "Скачать"),
        h("a", { class: "btn btn--secondary btn--sm", href: `/${item.kind}/` }, `Открыть «${KINDS[item.kind].label}»`))),
  );
}

const gallery = album({
  root: document.getElementById("album"),
  filters: [{ id: "all", label: "Всё" }, ...Object.entries(KINDS).map(([id, k]) => ({ id, label: k.label }))],
  load,
  card,
  view,
  empty: "Готовых генераций пока нет — начни с «Фото», «Видео» или «Аудио».",
});
gallery.start().then(() => poller({ run: () => gallery.refresh(), fast: 20000, slow: 20000 }).start());
