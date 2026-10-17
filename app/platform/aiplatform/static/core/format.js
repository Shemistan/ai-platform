// Форматтеры без DOM: длительности, даты, склонение по числу. Страницы берут их через ui.js
// (реэкспорт) или напрямую; тесты — tests/js/format.test.mjs (FE-009).

// 75 → «1 мин 15 с», 42.4 → «42 с»; null/undefined → "".
export function seconds(value) {
  if (value === null || value === undefined) return "";
  const s = Math.round(value);
  return s >= 60 ? `${Math.floor(s / 60)} мин ${s % 60} с` : `${s} с`;
}

// Длина ролика: 75 → «1:15», 42 → «42 с»; 0/null → "".
export function minutes(value) {
  if (!value) return "";
  const m = Math.floor(value / 60);
  const s = Math.round(value % 60);
  return m ? `${m}:${String(s).padStart(2, "0")}` : `${s} с`;
}

// «12 окт.», с годом — если не этот. Дата без времени («2026-10-12») — полдень, чтобы пояс не сдвинул день.
// now — для тестов.
export function shortDate(value, now = new Date()) {
  if (!value) return "";
  const d = new Date(value.length <= 10 ? `${value}T12:00:00` : value);
  if (Number.isNaN(d.getTime())) return value;
  const opts = { day: "numeric", month: "short" };
  if (d.getFullYear() !== now.getFullYear()) opts.year = "numeric";
  return d.toLocaleDateString("ru-RU", opts);
}

// Форма слова по числу: plural(3, "слайд", "слайда", "слайдов") → «слайда».
export function plural(n, one, few, many) {
  const mod10 = Math.abs(n) % 10;
  const mod100 = Math.abs(n) % 100;
  if (mod10 === 1 && mod100 !== 11) return one;
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return few;
  return many;
}

// Число со словом: countLabel(21, "слово", "слова", "слов") → «21 слово».
export const countLabel = (n, one, few, many) => `${n} ${plural(n, one, few, many)}`;
