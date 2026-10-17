// Расчёт ключевых кадров из video/frames.js: длина, лимит, расстановка, разбор времени, проверки (FE-009).
import assert from "node:assert/strict";
import { test } from "node:test";
import { loadStatic } from "./load.mjs";

const { frameAt, frameCount, frameProblems, keyframeLimit, spread } = await loadStatic("video/frames.js");

test("frameCount: секунды · fps плюс нулевой кадр", () => {
  assert.equal(frameCount(4, 24), 97);
  assert.equal(frameCount(0, 24), 1);
  assert.equal(frameCount(10, 25), 251);
});

test("keyframeLimit: по зазору и по лимиту модели", () => {
  assert.equal(keyframeLimit(97, 16), 7);
  assert.equal(keyframeLimit(97, 16, 4), 4);
  assert.equal(keyframeLimit(97, 16, undefined), 7);
  assert.equal(keyframeLimit(97, 16, null), 7);
  assert.equal(keyframeLimit(1, 16), 1);
});

test("spread: один кадр в начале, остальные равномерно до конца", () => {
  assert.deepEqual(spread(0, 96), []);
  assert.deepEqual(spread(1, 96), [0]);
  assert.deepEqual(spread(2, 96), [0, 96]);
  assert.deepEqual(spread(3, 96), [0, 48, 96]);
  assert.deepEqual(spread(4, 100), [0, 33, 67, 100]);
});

test("frameAt: секунды с запятой, отрицательные — к нулю, не число — null", () => {
  assert.equal(frameAt("1,5", 24), 36);
  assert.equal(frameAt("2.25", 24), 54);
  assert.equal(frameAt(-3, 24), 0);
  assert.equal(frameAt("abc", 24), null);
  assert.equal(frameAt("Infinity", 24), null);
});

test("frameProblems: начало, конец ролика, зазор и ошибка загрузки", () => {
  const opts = { gap: 16, last: 96, fps: 24 };
  assert.deepEqual(
    frameProblems([{ frameIdx: 0 }, { frameIdx: 10 }, { frameIdx: 200 }, { frameIdx: 50, error: "не загрузилось" }], opts),
    [null, "Нужно позже кадра 1 хотя бы на 0.67 с", "Позже конца ролика", "не загрузилось"],
  );
  assert.deepEqual(frameProblems([{ frameIdx: 5 }], opts), ["Первый кадр всегда в начале"]);
  assert.deepEqual(frameProblems([{ frameIdx: 0 }, { frameIdx: 48 }, { frameIdx: 96 }], opts), [null, null, null]);
});
