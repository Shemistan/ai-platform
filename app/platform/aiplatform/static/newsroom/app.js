// Пространство «Новости» (PRD-033): выпуск за день, неделю или месяц одной кнопкой и альбом готовых выпусков.
import { createRelease, releasesAlbum, studioHref } from "/core/releases.js";
import { $, busyClick, h, poller } from "/ui.js";

const thisMonth = new Date().toISOString().slice(0, 7);
Object.assign($("month"), { value: thisMonth, max: thisMonth });

function showError(...parts) {
  $("error").replaceChildren(...parts);
  $("error").hidden = !parts.length;
}

// Создать выпуск и перейти в него; уже есть за этот период — ссылка на него.
async function start(payload) {
  showError();
  try {
    const { project, existing, message } = await createRelease(payload);
    if (project) location.href = studioHref(project);
    else showError(`${message} `, h("a", { href: studioHref(existing) }, "Открыть"));
  } catch (err) {
    showError(err.message);
  }
}

$("new-digest").addEventListener("click", busyClick(() => start({ scenario: "digest" })));
$("new-weekly").addEventListener("click", busyClick(() => start({ scenario: "weekly" })));
$("new-monthly").addEventListener("click", busyClick(() => start({ scenario: "monthly", month: $("month").value })));

const gallery = releasesAlbum({
  root: $("album"),
  scenarios: [
    { id: "digest", label: "День" },
    { id: "weekly", label: "Неделя" },
    { id: "monthly", label: "Месяц" },
  ],
  empty: "Выпусков новостей пока нет — начни с «День».",
});
gallery.start().then(() => poller({ run: () => gallery.refresh(), fast: 20000, slow: 20000 }).start());
