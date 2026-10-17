// Студия: редакторы артефактов и загрузка своих файлов — постер, озвучка, картинки и ролики слайдов.
import { api, busyClick, h, toast, UPLOAD_LIMITS, uploadFile } from "/ui.js";
import { API, state, stepLocked } from "./state.js";
import { alertBox, show } from "./app.js";

async function saveArtifact(name, text) {
  try {
    const fresh = await api("POST", `${API}/projects/${state.project.id}/artifacts/${name}`, { text });
    delete state.drafts[name];
    toast("Сохранено");
    state.gen += 1;
    show(fresh);
  } catch (err) {
    alertBox(err.message);
  }
}

// Свой постер или озвучка: прогресс и «Отменить» — в лотке загрузок (uploadFile, FE-003).
// Сервер принимает здесь до лимита медиа (озвучка); отмена возвращает шаг как был.
export async function upload(what, file) {
  try {
    show(await uploadFile(`${API}/projects/${state.project.id}/upload/${what}`, file, { limit: (state.config?.upload_limits || UPLOAD_LIMITS).media }));
  } catch (err) {
    if (err.kind !== "abort") alertBox(err.message);
  }
}

export function editor(name, value, rows = 8, mono = false) {
  // id — чтобы после перерисовки вернуть фокус и курсор; текст живёт в state.drafts.
  const area = h("textarea", { id: `editor-${name}`, rows, class: mono ? "mono" : null, oninput: (e) => (state.drafts[name] = e.target.value) });
  area.value = state.drafts[name] ?? value ?? "";
  return h(
    "div",
    { class: "field" },
    area,
    h(
      "div",
      { class: "actions" },
      h("button", { type: "button", class: "btn btn--secondary", disabled: state.project.running, onclick: busyClick(() => saveArtifact(name, area.value)) }, "Сохранить правку"),
      h("span", { class: "hint" }, "Правка сбросит шаги, которые от неё зависят."),
    ),
  );
}

export function uploader(what, accept, label) {
  return h(
    "label",
    { class: "upload btn btn--secondary" },
    label,
    h("input", { type: "file", accept, hidden: true, disabled: stepLocked(what), onchange: (e) => e.target.files[0] && upload(what, e.target.files[0]) }),
  );
}

async function uploadSlide(kind, key, file) {
  const limits = (state.config?.upload_limits || UPLOAD_LIMITS);
  try {
    show(await uploadFile(`${API}/projects/${state.project.id}/upload/${kind}/${key}`, file, { limit: kind === "clips" ? limits.media : limits.image }));
  } catch (err) {
    if (err.kind !== "abort") alertBox(err.message);
  }
}

const MEDIA = {
  images: { accept: "image/png,image/jpeg,image/webp", hint: "Перетащи картинку (PNG, JPEG, WebP) или нажми" },
  clips: { accept: "video/mp4,video/quicktime,video/webm", hint: "Перетащи видео (MP4, MOV, WebM) или нажми" },
  poster: { accept: "image/png,image/jpeg,image/webp", hint: "Перетащи постер (PNG, JPEG, WebP) или нажми" },
};

// Окно для готового файла: перетащить или выбрать. Внутри — превью того, что уже лежит.
export function dropZone(kind, url, onFile) {
  const busy = stepLocked(kind);
  const input = h("input", { type: "file", accept: MEDIA[kind].accept, hidden: true, onchange: (e) => e.target.files[0] && onFile(e.target.files[0]) });
  let preview = h("span", { class: "drop-hint" }, MEDIA[kind].hint);
  if (url && kind === "clips") {
    preview = h("video", { src: url, loop: true, autoplay: true, playsinline: true, preload: "metadata" });
    preview.muted = true; // атрибут muted у созданного скриптом элемента автоплей не разрешает
  } else if (url) {
    preview = h("img", { src: url, alt: "" });
  }
  const zone = h(
    "div",
    {
      class: `drop-zone${url ? " filled" : ""}${busy ? " disabled" : ""}`,
      role: "button",
      tabindex: busy ? "-1" : "0",
      title: url ? "Перетащи другой файл, чтобы заменить" : null,
      onclick: () => !busy && input.click(),
      onkeydown: (e) => (e.key === "Enter" || e.key === " ") && !busy && (e.preventDefault(), input.click()),
      ondragover: (e) => {
        if (busy) return;
        e.preventDefault();
        zone.classList.add("over");
      },
      ondragleave: () => zone.classList.remove("over"),
      ondrop: (e) => {
        e.preventDefault();
        zone.classList.remove("over");
        const file = e.dataTransfer.files[0];
        if (file && !busy) onFile(file);
      },
    },
    preview,
    input,
  );
  return zone;
}

export function copyButton(text) {
  return h(
    "button",
    {
      type: "button",
      class: "btn btn--link",
      onclick: async (e) => {
        const btn = e.currentTarget;
        try {
          await navigator.clipboard.writeText(text);
          btn.textContent = "Скопировано";
          toast("Промпт скопирован");
        } catch {
          btn.textContent = "Не скопировалось";
          toast("Не удалось скопировать: браузер не дал доступ к буферу обмена", "danger");
        }
        setTimeout(() => (btn.textContent = "Скопировать промпт"), 1500);
      },
    },
    "Скопировать промпт",
  );
}

// Строка «промпт слева — окно для готового файла справа».
export function promptRow(label, prompt, zone) {
  return h(
    "div",
    { class: "prompt-row" },
    h("div", { class: "prompt-side" }, h("strong", {}, label), h("p", { class: "prompt-text" }, prompt), copyButton(prompt)),
    zone,
  );
}

// Виды слайдов плана: картинка или динамическая сцена, которую рисует сборка.
export const SLIDE_KINDS = { image: "картинка", bullets: "пункты", stat: "число", chart: "график", flow: "схема", compare: "сравнение", quote: "цитата" };

export function slideItems(s) {
  const num = (p) => `${p.label}: ${p.value}${s.unit ? " " + s.unit : ""}`;
  switch (s.kind) {
    case "stat":
      return [`${s.prefix || ""}${s.value}${s.suffix || ""} — ${s.label || ""}`];
    case "chart":
      return (s.data || []).map(num);
    case "flow":
      return [(s.steps || []).join(" → ")];
    case "compare":
      return [`${s.left?.title || "Было"}: ${(s.left?.items || []).join("; ")}`, `${s.right?.title || "Стало"}: ${(s.right?.items || []).join("; ")}`];
    case "quote":
      return [`«${s.text}»${s.author ? " — " + s.author : ""}`];
    default:
      return s.bullets || [];
  }
}

export function mediaRows(kind) {
  const field = kind === "clips" ? "video_prompt" : "image_prompt";
  const fileField = kind === "clips" ? "clip" : "image";
  const rows = state.project.slides
    .filter((s) => s[field])
    .map((s) => promptRow(`Слайд ${Number(s.key)} · ${s.title}`, s[field], dropZone(kind, s[fileField], (file) => uploadSlide(kind, s.key, file))));
  return rows.length ? [h("div", { class: "prompt-rows" }, ...rows)] : [];
}
