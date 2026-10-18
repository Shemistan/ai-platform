// Логика формы «Диктора» без DOM: что уходит на сервер и почему форма не отправляется.
// Те же правила проверяет сервер (avatar/jobs.py, avatar/characters.py) — здесь подсказка до отправки.

export const MAX_NAME = 80;
export const MAX_OWNER = 200;

const squash = (text) => String(text ?? "").split(/\s+/).filter(Boolean).join(" ");

// Согласие на лицо с фото: { confirmed, owner, form } → { consent } или { error }.
export function faceConsent({ confirmed, owner, form } = {}) {
  if (confirmed !== true) return { error: "Подтверди, что это твоё лицо или у тебя есть письменное согласие человека на фото." };
  const name = squash(owner);
  if (!name) return { error: "Напиши, чьё лицо на фото." };
  if (name.length > MAX_OWNER) return { error: `Имя — до ${MAX_OWNER} знаков.` };
  if (form !== "self" && form !== "written") return { error: "Выбери: твоё лицо или по письменному согласию." };
  return { consent: { confirmed: true, owner: name, form } };
}

// Новый персонаж: { uploadId, name, kind, consent } → { payload } или { error }.
export function characterPayload({ uploadId, name, kind, consent } = {}) {
  if (!uploadId) return { error: "Загрузи картинку персонажа." };
  const clean = squash(name);
  if (!clean) return { error: "Дай персонажу имя." };
  if (clean.length > MAX_NAME) return { error: `Имя персонажа — до ${MAX_NAME} знаков.` };
  if (kind !== "photo" && kind !== "drawn") return { error: "Выбери вид персонажа." };
  const payload = { upload_id: uploadId, name: clean, kind };
  if (kind === "photo") {
    const checked = faceConsent(consent);
    if (checked.error) return checked;
    payload.consent = checked.consent;
  }
  return { payload };
}

// Задача: что выбрано в форме → { payload } или { error }. maxText — из /api/avatar/config.
export function jobPayload({ character, source, text, engine, voice, speed, uploadId, model, size, frame, enhance, maxText = Infinity } = {}) {
  if (!character) return { error: "Выбери персонажа — или добавь его." };
  const payload = { character, source, model, size: Number(size), frame, enhance: Boolean(enhance) };
  if (source === "text") {
    const body = String(text ?? "").trim();
    if (!body) return { error: "Напиши текст, который скажет персонаж." };
    if (body.length > maxText) return { error: `Текст длиннее ${maxText} знаков — раздели его.` };
    Object.assign(payload, { text: body, engine, voice: voice || null });
    if (speed) payload.speed = Number(speed);
  } else if (source === "audio") {
    if (!uploadId) return { error: "Загрузи озвучку — mp3, wav или m4a." };
    payload.upload_id = uploadId;
  } else {
    return { error: "Выбери, откуда речь: текст или готовая озвучка." };
  }
  return { payload };
}
