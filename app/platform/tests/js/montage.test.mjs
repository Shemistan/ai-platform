// Экран подтверждения «Монтажа» (PRD-021): фразы, вырезки по клику, планы, форма.
import assert from "node:assert/strict";
import { test } from "node:test";
import { loadStatic } from "./load.mjs";

const { clock, cutOf, layoutAt, phrases, projectPayload, setLayout, setupSummary, togglePhrase, toggleWord } = await loadStatic("montage/plan.js");

const w = (text, s, e) => ({ w: text, s, e });
const WORDS = [w("раз", 0.5, 0.8), w("два.", 0.9, 1.2), w("три", 1.3, 1.5), w("четыре", 2.5, 2.9), w("пять", 3.0, 3.2)];

test("фразы рвутся на точке и паузе", () => {
  const out = phrases(WORDS);
  assert.deepEqual(out.map((p) => [p.first, p.last]), [[0, 1], [2, 2], [3, 4]]);
  assert.equal(out[2].gapBefore.toFixed(1), "1.0");
});

test("клик по слову: вырезать, вернуть, Shift — растянуть", () => {
  let cuts = toggleWord([], 3);
  assert.deepEqual(cuts, [{ from: 3, to: 3, kind: "human", reason: "вырезал человек" }]);
  cuts = toggleWord(cuts, 1, { extend: true, last: cuts[0] });
  assert.deepEqual(cuts.map((c) => [c.from, c.to]), [[1, 3]]);
  assert.deepEqual(toggleWord(cuts, 2), [], "клик внутри — вырезка снимается целиком");
  assert.deepEqual(cutOf([{ from: 1, to: 2 }], 4), [-1, 0, 0, -1]);
});

test("фраза целиком: вырезать и вернуть, хвосты чужих вырезок целы", () => {
  const phrase = { first: 2, last: 3 };
  let cuts = togglePhrase([{ from: 0, to: 2, kind: "retake", reason: "" }], phrase);
  assert.deepEqual(cuts.map((c) => [c.from, c.to, c.kind]), [[0, 1, "retake"], [2, 3, "human"]], "начало дубля вне фразы осталось");
  cuts = togglePhrase([{ from: 0, to: 4, kind: "extra", reason: "" }], phrase);
  assert.deepEqual(cuts.map((c) => [c.from, c.to]), [[0, 1], [4, 4]]);
});

test("планы: отметка на фразе, без повторов, голова с начала", () => {
  let layout = setLayout([{ from: 0, layout: "head" }], 5, "split");
  assert.deepEqual(layout, [{ from: 0, layout: "head" }, { from: 5, layout: "split" }]);
  layout = setLayout(layout, 9, "split");
  assert.equal(layout.length, 2, "тот же план подряд — без новой отметки");
  assert.equal(layoutAt(layout, 7), "split");
  assert.deepEqual(setLayout([], 3, "screen")[0], { from: 0, layout: "head" });
});

test("время и форма", () => {
  assert.equal(clock(83.4), "1:23");
  assert.equal(clock(3725), "1:02:05");
  assert.match(projectPayload({ maxPause: "5" }).error, /пауза/);
  const { payload } = projectPayload({ title: " Обзор ", model: "m", terms: " a ", grade: "none", auto: 1, maxPause: "0,8" });
  assert.deepEqual(payload, { title: "Обзор", options: { model: "m", terms: "a", grade: "none", auto: true, subtitles: false, check: false, motion: false, max_pause: 0.8 } });
});

test("сводка «Как монтировать»: модель, пауза, включённое и режим (UX-033)", () => {
  const cfg = { models: [{ id: "turbo", label: "Whisper Turbo" }], modes: [{ id: "reviewed", label: "С подтверждением" }] };
  assert.equal(
    setupSummary({ model: "turbo", max_pause: 0.6, check: true, subtitles: false, motion: false, mode: "reviewed" }, cfg),
    "Whisper Turbo · пауза до 0,6 с · проверка · С подтверждением",
  );
  assert.equal(setupSummary({ model: "x", max_pause: "1" }), "x · пауза до 1 с");
});
