// plural/fmtNum/countLabel из core/format.js: склонение после числа и разряды (TXT-002); близнец — core/words.py.
import assert from "node:assert/strict";
import { test } from "node:test";
import { loadStatic } from "./load.mjs";

const { plural, fmtNum, countLabel } = await loadStatic("core/format.js");
const CHANNEL = ["канал", "канала", "каналов"];

test("формы после 1/2/5/11/21/22/25/111", () => {
  const want = { 0: "каналов", 1: "канал", 2: "канала", 5: "каналов", 11: "каналов", 12: "каналов", 14: "каналов", 21: "канал", 22: "канала", 25: "каналов", 111: "каналов" };
  for (const [n, form] of Object.entries(want)) assert.equal(plural(Number(n), ...CHANNEL), form, `n=${n}`);
});

test("«Опубликовать в N каналов»", () => {
  assert.deepEqual([1, 2, 5, 21].map((n) => `в ${n} ${plural(n, ...CHANNEL)}`), ["в 1 канал", "в 2 канала", "в 5 каналов", "в 21 канал"]);
});

test("разряды через неразрывный пробел", () => {
  assert.equal(fmtNum(1200), "1 200");
  assert.equal(countLabel(1200, "просмотр", "просмотра", "просмотров"), "1 200 просмотров");
  assert.equal(countLabel(-3, "подписчик", "подписчика", "подписчиков"), "-3 подписчика");
});
