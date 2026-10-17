// Форматтеры core/format.js (их же отдаёт ui.js): края — 0, null, большие значения (FE-009, QA-025).
import assert from "node:assert/strict";
import { test } from "node:test";
import { loadStatic } from "./load.mjs";

const { countLabel, minutes, plural, seconds, shortDate } = await loadStatic("core/format.js");

test("seconds: пусто без значения, секунды и минуты с секундами", () => {
  assert.equal(seconds(null), "");
  assert.equal(seconds(undefined), "");
  assert.equal(seconds(0), "0 с");
  assert.equal(seconds(42.4), "42 с");
  assert.equal(seconds(59.6), "1 мин 0 с");
  assert.equal(seconds(75), "1 мин 15 с");
  assert.equal(seconds(3725), "62 мин 5 с");
});

test("minutes: 0 и null — пусто, короткое — секундами, длинное — м:сс", () => {
  assert.equal(minutes(0), "");
  assert.equal(minutes(null), "");
  assert.equal(minutes(undefined), "");
  assert.equal(minutes(42), "42 с");
  assert.equal(minutes(75), "1:15");
  assert.equal(minutes(605), "10:05");
  assert.equal(minutes(3 * 3600 + 5), "180:05");
});

test("shortDate: день и месяц, год — только у не текущего", () => {
  const now = new Date("2026-06-01T12:00:00");
  assert.equal(shortDate("", now), "");
  assert.equal(shortDate(null, now), "");
  assert.equal(shortDate("не дата", now), "не дата");
  const same = shortDate("2026-10-12", now);
  assert.match(same, /^12 окт/);
  assert.doesNotMatch(same, /2026/);
  assert.match(shortDate("2025-10-12", now), /^12 окт.*2025/);
  // Дата со временем — тот же день, пояс не сдвигает дату без времени.
  assert.match(shortDate("2026-01-01T09:30:00", now), /^1 янв/);
  assert.match(shortDate("2026-01-01", now), /^1 янв/);
});

test("plural: формы по последним цифрам, 11–14 — «много»", () => {
  const word = (n) => plural(n, "слово", "слова", "слов");
  const cases = { 0: "слов", 1: "слово", 2: "слова", 4: "слова", 5: "слов", 11: "слов", 12: "слов", 14: "слов", 21: "слово", 22: "слова", 25: "слов", 101: "слово", 111: "слов", 1000000: "слов" };
  for (const [n, expected] of Object.entries(cases)) assert.equal(word(Number(n)), expected, `n=${n}`);
});

test("countLabel: число со словом", () => {
  assert.equal(countLabel(71, "слайд", "слайда", "слайдов"), "71 слайд");
  assert.equal(countLabel(3, "картинка", "картинки", "картинок"), "3 картинки");
  assert.equal(countLabel(0, "ролик", "ролика", "роликов"), "0 роликов");
});

test("ui.js отдаёт те же форматтеры", async () => {
  globalThis.document ??= {};
  const ui = await loadStatic("ui.js");
  assert.equal(ui.seconds, seconds);
  assert.equal(ui.minutes, minutes);
  assert.equal(ui.shortDate, shortDate);
  assert.equal(ui.plural, plural);
});
