// «Настройки дайджеста» (PRD-034): умолчания следующих выпусков. Поля — те же, что в форме студии;
// на сервер уходит только то, что поменяли, поэтому нетронутые модель и голос следуют общему умолчанию.
import { fieldsOf, readFields } from "/studio/fields.js";
import { $, api, fill, h, toast } from "/ui.js";

const PREFIX = "digest";
const API = "/api/settings/section";
// Раздел настроек → его поля; engine читается вместе с голосом.
const DEFAULTS = [
  "poster_mode", "poster_deadline", "draw_images", "images_mode", "image_model", "upscale_images", "clips", "clips_mode",
  "video_model", "voice_mode", "engine", "tempo", "shorts", "slide_s", "reading", "slide_motion", "frame", "branding",
  "ai_badge", "disclaimers", "review_text", "evening",
]; // prettier-ignore
const NAMES = [...DEFAULTS, "channels", "auto_publish"];

let saved = {};

function showError(message) {
  $("error").textContent = message || "";
  $("error").hidden = !message;
}

function form(values, cfg) {
  const f = fieldsOf(values, PREFIX, cfg);
  const group = (legend, ...children) => h("fieldset", {}, h("legend", {}, legend), ...children);
  return h(
    "form",
    { class: "settings-form", onchange: save, onsubmit: (e) => e.preventDefault() },
    group("Новости", f.evening()),
    group("Постер — первый кадр", f.poster_mode(), f.poster_deadline()),
    group("Картинки слайдов", f.draw_images(), f.images_mode(), f.image_model(), f.upscale_images()),
    group("Ролики", f.clips(), f.clips_mode(), f.video_model()),
    group("Озвучка", f.voice_mode(), f.engine(), f.tempo()),
    group("Шортс", f.shorts()),
    group("Слайды", f.slide_s(), f.reading(), f.slide_motion()),
    group("Оформление", f.frame(), f.branding(), f.ai_badge(), f.disclaimers()),
    group("Текст", f.review_text()),
    group("Публикация", f.channels(), f.auto_publish(), h("p", { class: "hint" }, "Автопубликация действует только на выпуск по расписанию: созданный руками ждёт твоего нажатия.")),
  );
}

async function save() {
  const now = readFields(PREFIX, NAMES);
  const differs = (name) => JSON.stringify(now[name]) !== JSON.stringify(saved[name]);
  const names = [...DEFAULTS.flatMap((n) => (n === "engine" ? ["engine", "voice"] : [n]))];
  const defaults = Object.fromEntries(names.filter(differs).map((n) => [n, now[n]]));
  try {
    if (Object.keys(defaults).length) await api("POST", `${API}/digest_defaults`, defaults);
    if (differs("channels")) await api("POST", `${API}/default_channels`, { digest: now.channels });
    if (differs("auto_publish")) await api("POST", `${API}/schedule`, { auto_publish: now.auto_publish });
    saved = now;
    showError("");
    toast("Сохранено — для следующих выпусков");
  } catch (err) {
    showError(err.message);
    load(); // в форме — снова то, что действует на самом деле
  }
}

async function load() {
  try {
    const [cfg, state] = await Promise.all([api("GET", "/api/studio/config"), api("GET", "/api/settings/state")]);
    const values = { ...cfg.defaults, ...cfg.scenario_defaults.digest, auto_publish: state.schedule.auto_publish };
    fill($("form"), form(values, cfg));
    saved = readFields(PREFIX, NAMES);
  } catch (err) {
    showError(`Настройки не загрузились: ${err.message}`);
  }
}

load();
