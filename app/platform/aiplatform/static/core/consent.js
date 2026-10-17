// Согласие владельца голоса для записи образца (CMP-010): без галочки и имени форма не уходит.
// Те же правила проверяет сервер (audio/service.py voice_consent) — это подсказка до отправки.

export const CONSENT_FORMS = { self: "Это мой голос", written: "Есть письменное согласие владельца" };

// { confirmed, owner, form } → { consent } для POST /api/audio/voices/<stem> или { error }.
export function voiceConsent({ confirmed, owner, form } = {}) {
  if (confirmed !== true) return { error: "Подтверди, что это твой голос или у тебя есть письменное согласие владельца." };
  const name = String(owner ?? "").split(/\s+/).filter(Boolean).join(" ");
  if (!name) return { error: "Напиши, чей это голос." };
  if (name.length > 200) return { error: "Имя владельца голоса — до 200 знаков." };
  if (!(form in CONSENT_FORMS)) return { error: "Выбери, чей голос: твой или по письменному согласию." };
  return { consent: { confirmed: true, owner: name, form } };
}
