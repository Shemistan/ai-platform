// Пространство «Статьи» (PRD-033): ссылка или текст → «Видео по статье»; альбом выпусков по статьям и постов.
import { articleFromUrl, createRelease, releasesAlbum, studioHref } from "/core/releases.js";
import { $, h, poller } from "/ui.js";

function showError(...parts) {
  $("error").replaceChildren(...parts);
  $("error").hidden = !parts.length;
}

$("input").addEventListener("submit", async (event) => {
  event.preventDefault();
  const text = $("what").value.trim();
  if (!text) return $("what").focus();
  showError();
  $("go").disabled = true;
  $("go").textContent = "Готовлю…";
  try {
    const url = /^https?:\/\/\S+$/.test(text);
    const { project, existing, message } = url ? await articleFromUrl(text) : await createRelease({ scenario: "article", title: "", text });
    if (project) location.href = studioHref(project);
    else showError(`${message} `, h("a", { href: studioHref(existing) }, "Открыть"));
  } catch (err) {
    showError(err.message);
  } finally {
    $("go").disabled = false;
    $("go").textContent = "Сделать видео";
  }
});

const gallery = releasesAlbum({
  root: $("album"),
  scenarios: [
    { id: "article", label: "Видео по статьям" },
    { id: "post", label: "Посты" },
  ],
  empty: "Видео по статьям пока нет — вставь ссылку или текст выше.",
});
gallery.start().then(() => poller({ run: () => gallery.refresh(), fast: 20000, slow: 20000 }).start());
