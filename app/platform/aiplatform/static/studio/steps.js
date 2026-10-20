// Студия: карточки шагов — свёрнутые готовые, раскрытые активные; тело и сводка шага, перезапуск.
import { $, busyClick, confirmAction, countLabel, h, progressBar, statusChip, toast, toneOf } from "/ui.js";
import { state, visibleSteps } from "./state.js";
import { STEP_FIELDS, readSettings, stepSettings } from "./settings.js";
import { captionCounter } from "./create.js";
import { SLIDE_KINDS, copyButton, dropZone, editor, mediaRows, promptRow, slideItems, upload, uploader } from "./media.js";
import { publishBody, publishPick } from "./publish.js";
import { act, show } from "./app.js";

// Перезапуск сбрасывает все зависимые шаги — это десятки минут GPU, поэтому сначала спрашиваем.
export async function rerunStep(stepId) {
  const project = state.project;
  const step = project.steps.find((s) => s.id === stepId);
  if (step.status !== "pending") {
    const visible = visibleSteps(project);
    const resets = visible.filter((s) => (step.dependents || []).includes(s.id) && s.status !== "pending");
    const ok = await confirmAction({
      title: `Перезапустить «${step.label}»?`,
      body: resets.length
        ? [h("p", {}, "Сбросятся и соберутся заново:"), h("ul", { class: "confirm-list" }, ...resets.map((s) => h("li", {}, `${visible.indexOf(s) + 1}. ${s.label}`)))]
        : h("p", {}, "Другие шаги не затронуты."),
      confirmLabel: "Перезапустить",
      danger: resets.length > 0,
    });
    if (!ok) return;
  }
  const names = STEP_FIELDS[step.id];
  const settings = names && document.getElementById(`step-${step.id}-settings`) ? readSettings(`step-${step.id}`, names) : undefined;
  if (await act("run", { step: step.id, settings })) {
    delete state.stepDrafts[step.id];
    toast(`Шаг «${step.label}» запущен`, "info");
  }
  state.gen += 1; // параметры шага — из сохранённых настроек, а не из набросков
  show(state.project);
}

function jobLink(step) {
  if (!step.job) return null;
  const tab = { photo: "Фото", video: "Видео", audio: "Аудио" }[step.job.tab] || step.job.tab;
  return h("a", { class: "btn--link", href: `/${step.job.tab}/` }, `задача на вкладке «${tab}»`);
}

const VERDICT_TONE = { готово: "success", "готово с оговорками": "warning", "не готово": "danger" };

// Вердикт судьи дайджеста: отдельный запуск claude проверил выпуск по первоисточникам.
function verdictBox(review) {
  if (!review) return null;
  const label = review.verdict ? `Судья: ${review.verdict}` : "Судья: вердикта нет";
  return h(
    "div",
    { class: "note verdict" },
    h("span", { class: `chip tone-${VERDICT_TONE[review.verdict] || "neutral"}` }, label),
    review.verdict === "не готово" ? h("span", { class: "hint" }, " Публикация ждёт: поправь выпуск или разреши её в шаге «Публикация».") : null,
    h("details", { class: "disclosure" }, h("summary", {}, "Отчёт судьи"), h("pre", { class: "log" }, review.text)),
  );
}

function disclaimerBox() {
  const found = state.project.disclaimers || [];
  if (!found.length) return null;
  if (!state.project.settings.disclaimers) {
    return h("p", { class: "hint" }, `В тексте есть ${found.map((d) => d.name).join(", ")} — пометки выключены в настройках выпуска.`);
  }
  return h(
    "div",
    { class: "note disclaimers" },
    h("strong", {}, "Пометки на видео и в посте"),
    h("ul", {}, ...found.map((d) => h("li", {}, d.note))),
  );
}

function stepBody(step) {
  const p = state.project;
  const a = p.artifacts;
  const f = p.files;
  switch (step.id) {
    case "text":
      if (p.scenario === "post") {
        const captionArea = editor("post", a.post, 6);
        return [
          h("p", { class: "field-label" }, "Подпись к посту"),
          captionArea,
          captionCounter(captionArea.querySelector("textarea")),
          h("details", { class: "disclosure" }, h("summary", {}, "Текст для озвучки"), editor("text", a.text, 6)),
        ];
      }
      return [
        verdictBox(p.review),
        a.text || step.status !== "pending" ? editor("text", a.text, 12) : h("p", { class: "muted" }, "Текст появится после сбора новостей."),
        // Статья, как её вставили: из неё LLM готовит текст для озвучки (PRD-029).
        a.source && a.source !== a.text
          ? h("details", { class: "disclosure" }, h("summary", {}, "Исходная статья"), h("pre", { class: "muted source-text" }, a.source))
          : null,
      ];
    case "poster_prompt":
      return a.poster_prompt
        ? [
            editor("poster_prompt", a.poster_prompt, 3),
            copyButton(a.poster_prompt),
          ]
        : [];
    case "poster":
      if (p.settings.poster_mode === "upload" && a.poster_prompt) {
        return [h("div", { class: "prompt-rows" }, promptRow("Постер — первый кадр", a.poster_prompt, dropZone("poster", f.poster, (file) => upload("poster", file))))];
      }
      return [
        f.poster ? h("img", { class: "poster-img", src: f.poster, alt: "Постер" }) : null,
        h("div", { class: "actions" }, uploader("poster", "image/png,image/jpeg,image/webp", f.poster ? "Заменить своей картинкой" : "Загрузить постер"), jobLink(step)),
      ];
    case "plan": {
      if (!a.plan) return [];
      const cards = a.plan.slides.map((s, i) => {
        const key = String(i + 1).padStart(2, "0");
        const img = f.images[key];
        const kind = s.kind || (s.image_prompt ? "image" : "bullets");
        const where = `абзац ${s.para + 1}, фраза ${(s.sent || 0) + 1} · ${SLIDE_KINDS[kind] || kind}`;
        const items = slideItems(s);
        return h(
          "div",
          { class: "slide-card" },
          img ? h("img", { src: img, alt: "" }) : h("div", { class: "ph" }, kind === "image" ? "" : SLIDE_KINDS[kind] || ""),
          h("div", {}, h("strong", {}, s.title), h("div", { class: "para" }, where), items.length ? h("ul", {}, ...items.map((b) => h("li", {}, b))) : null),
        );
      });
      const dynamic = a.plan.slides.filter((s) => (s.kind || (s.image_prompt ? "image" : "bullets")) !== "image").length;
      return [
        a.plan.title ? h("p", {}, h("span", { class: "muted" }, "Название: "), a.plan.title) : null,
        h("p", { class: "muted small" }, `${countLabel(a.plan.slides.length, "слайд", "слайда", "слайдов")}: ${a.plan.slides.length - dynamic} с картинкой, ${dynamic} динамических`),
        h("div", { class: "slides-list" }, ...cards),
        h("details", { class: "disclosure" }, h("summary", {}, "Править план (JSON)"), editor("plan", JSON.stringify(a.plan, null, 2), 14, true)),
      ];
    }
    case "images":
      if (!p.settings.draw_images) return [];
      return [step.status === "running" ? jobLink(step) : null, ...mediaRows("images")];
    case "clips":
      if (!p.settings.clips) return [];
      return [step.status === "running" ? jobLink(step) : null, ...mediaRows("clips")];
    case "voice":
      return [
        f.voice ? h("audio", { controls: true, src: f.voice, preload: "metadata" }) : null,
        h("div", { class: "actions" }, uploader("voice", "audio/mpeg,audio/wav,audio/mp4,.m4a", f.voice ? "Заменить своим mp3" : "Загрузить озвучку"), jobLink(step)),
      ];
    case "render":
      return [
        disclaimerBox(),
        ...(f.video
          ? [h("video", { controls: true, src: f.video, preload: "metadata", poster: f.poster || null }), h("a", { class: "btn--link", href: `${f.video.split("?")[0]}?download=1` }, "Скачать видео")]
          : []),
      ];
    case "short":
      return [
        a.short ? h("details", { class: "disclosure" }, h("summary", {}, `Сценарий шортса: ${countLabel(a.short.beats.length, "часть", "части", "частей")}`), editor("short", JSON.stringify(a.short, null, 2), 10, true)) : null,
        f.short_video
          ? h("div", {}, h("video", { class: "vertical-video", controls: true, src: f.short_video, preload: "metadata" }), h("p", {}, h("a", { class: "btn--link", href: `${f.short_video.split("?")[0]}?download=1` }, "Скачать шортс 9:16")))
          : null,
      ];
    case "publish":
      return publishBody();
    default:
      return [];
  }
}

// Готовые и пропущенные шаги по умолчанию свёрнуты в строку; раскрытие руками помнится между опросами.
export function collapsed(step) {
  const own = state.expanded[`${state.project.id}:${step.id}`];
  if (own !== undefined) return !own;
  return step.status === "done" || step.status === "skipped";
}

function toggleStep(stepId) {
  const step = state.project.steps.find((s) => s.id === stepId);
  state.expanded[`${state.project.id}:${stepId}`] = collapsed(step);
  show(state.project);
}

// Прокрутить к шагу (раскрыв его) — из степпера и главной кнопки шапки.
export function goToStep(stepId) {
  const step = state.project.steps.find((s) => s.id === stepId);
  if (step && collapsed(step)) {
    state.expanded[`${state.project.id}:${stepId}`] = true;
    show(state.project);
  }
  const node = $(`step-${stepId}`);
  if (!node) return;
  node.scrollIntoView({ behavior: "smooth", block: "start" });
  node.focus({ preventScroll: true });
}

// Сводка свёрнутого шага одной строкой: «71 слайд, 26 с картинкой».
function stepSummary(step) {
  const p = state.project;
  const a = p.artifacts;
  const f = p.files;
  if (step.status === "skipped") return step.note || "";
  switch (step.id) {
    case "text": {
      const words = (p.scenario === "post" ? a.post : a.text)?.split(/\s+/).filter(Boolean).length || 0;
      return [countLabel(words, "слово", "слова", "слов"), p.review?.verdict ? `судья: ${p.review.verdict}` : ""].filter(Boolean).join(" · ");
    }
    case "poster_prompt":
      return a.poster_prompt ? `${a.poster_prompt.slice(0, 80)}${a.poster_prompt.length > 80 ? "…" : ""}` : "";
    case "poster":
      return f.poster ? "постер готов" : "";
    case "plan": {
      if (!a.plan) return "";
      const n = a.plan.slides.length;
      const withImage = a.plan.slides.filter((s) => (s.kind || (s.image_prompt ? "image" : "bullets")) === "image").length;
      return `${countLabel(n, "слайд", "слайда", "слайдов")}, ${withImage} с картинкой`;
    }
    case "images":
      return countLabel(Object.keys(f.images || {}).length, "картинка", "картинки", "картинок");
    case "clips":
      return countLabel(p.slides.filter((s) => s.clip).length, "ролик", "ролика", "роликов");
    case "voice":
      return f.voice ? "озвучка готова" : "";
    case "render":
      return f.video ? "видео собрано" : "";
    case "short":
      return f.short_video ? "шортс собран" : "";
    case "publish": {
      const sent = (p.published || []).filter((r) => r.sent).map((r) => r.label);
      return sent.length ? `опубликовано: ${sent.join(", ")}` : "";
    }
    default:
      return "";
  }
}

export function renderStep(step) {
  const p = state.project;
  const approveLabel = { text: "Продолжить", images: "Продолжить без недостающих", clips: "Продолжить без недостающих" }[step.id];
  const localMode = (step.id === "images" && p.settings.images_mode !== "upload") || (step.id === "clips" && p.settings.clips_mode !== "upload");
  const canApprove = step.status === "waiting" && approveLabel && !localMode;
  const folded = collapsed(step);
  const bodyId = `step-${step.id}-body`;
  const head = h(
    "div",
    { class: "step-head" },
    h("h3", {}, step.label),
    statusChip(step.status),
    folded ? h("span", { class: "step-summary muted small" }, stepSummary(step)) : null,
    h("button", { type: "button", class: "btn btn--link", "aria-expanded": String(!folded), "aria-controls": bodyId, onclick: () => toggleStep(step.id) }, folded ? "Развернуть" : "Свернуть"),
    step.status !== "running"
      ? step.status === "pending"
        ? h("button", { type: "button", class: "btn btn--link", disabled: p.running, onclick: busyClick(() => rerunStep(step.id)) }, "Выполнить шаг")
        : h("button", { type: "button", class: "btn btn--secondary btn--sm", disabled: p.running, onclick: busyClick(() => rerunStep(step.id)) }, "Перезапустить шаг")
      : null,
  );
  const attrs = { class: `step ${step.status} tone-${toneOf(step.status)}${folded ? " collapsed" : ""}`, id: `step-${step.id}`, tabindex: "-1" };
  if (folded) return h("article", attrs, head);
  return h(
    "article",
    attrs,
    head,
    h(
      "div",
      { class: "step-main", id: bodyId },
      step.status === "running" ? progressBar(step.progress || { stage: "…", percent: 0 }) : null,
      step.error ? h("p", { class: "job-error" }, step.error) : null,
      step.note ? h("p", { class: "step-note" }, step.note) : null,
      step.status === "pending" && step.blocked_by.length
        ? h("p", { class: "hint" }, `Начнётся, когда будет готово: ${step.blocked_by.join(", ")}.`)
        : null,
      canApprove ? h("button", { type: "button", class: "btn btn--primary", onclick: busyClick(() => act("approve", { step: step.id })) }, approveLabel) : null,
      stepSettings(step),
      h("div", { class: "step-body" }, ...stepBody(step)),
    ),
  );
}

// Данные выпуска, от которых зависит тело шага (кроме самого шага, настроек и running).
export const STEP_DATA = {
  text: (p) => [p.artifacts.text, p.artifacts.post, p.review],
  poster_prompt: (p) => [p.artifacts.poster_prompt],
  poster: (p) => [p.artifacts.poster_prompt, p.files.poster],
  plan: (p) => [p.artifacts.plan, p.files.images],
  images: (p) => [p.slides],
  clips: (p) => [p.slides],
  voice: (p) => [p.files.voice],
  render: (p) => [p.files.video, p.files.poster, p.disclaimers],
  short: (p) => [p.artifacts.short, p.files.short_video],
  publish: (p) => {
    const pick = publishPick(p);
    const extra = ["source_url", "source_author", "source_license", "ad", "erid"].map((k) => p.settings[k]);
    return [p.artifacts.post, p.published, p.review, state.config.channels, [...pick.channels], pick.force, pick.ignoreVerdict, p.series_of, p.running, ...extra];
  },
};
