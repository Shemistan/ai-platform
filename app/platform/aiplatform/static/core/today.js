// «Сегодня» (UX-031): чистые функции страницы — без DOM, тесты в node.

export const POST_LIMIT = 1024; // подпись Telegram под видео — как CAPTION_LIMIT студии

// Что сделать с тем, что положили в поле: { kind, label, action } или null, если пусто.
// kind: url — статья по ссылке, post — короткий текст, article — длинный текст, montage — видеофайл.
export function guessIntent(input) {
  if (input && typeof input === "object") {
    const video = /^video\//.test(input.type || "") || /\.(mp4|mov|webm)$/i.test(input.name || "");
    return video
      ? { kind: "montage", label: `«${input.name}» — смонтировать ролик из своей записи.`, action: "Смонтировать" }
      : { kind: null, label: "Сюда — видео со своей записью. Картинки и звук — во вкладках «Инструментов».", action: null };
  }
  const text = String(input || "").trim();
  if (!text) return null;
  if (/^https?:\/\/\S+$/.test(text)) return { kind: "url", label: "Ссылка на статью — скачаю текст и сделаю видео по нему.", action: "Сделать видео" };
  if (text.length <= POST_LIMIT) return { kind: "post", label: "Короткий текст — пост в канал: подпись и озвучка.", action: "Сделать пост" };
  return { kind: "article", label: "Длинный текст — видео по статье: постер, слайды, озвучка.", action: "Сделать видео" };
}

// «Ждут тебя» на доске (group attention) — самые свежие n; остальное — по ссылке «Все выпуски».
export function pickAttention(projects, n = 3) {
  const waiting = (projects || []).filter((p) => p.group === "attention");
  waiting.sort((a, b) => (b.updated_at || 0) - (a.updated_at || 0));
  return { shown: waiting.slice(0, n), more: Math.max(0, waiting.length - n) };
}

export function greeting(hour) {
  if (hour >= 5 && hour < 12) return "Доброе утро";
  if (hour >= 12 && hour < 18) return "Добрый день";
  if (hour >= 18 && hour < 23) return "Добрый вечер";
  return "Доброй ночи";
}
