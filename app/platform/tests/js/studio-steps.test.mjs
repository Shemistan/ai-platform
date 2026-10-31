// Студия: тихий «Перезапустить шаг» (UX-032) и одна главная кнопка в шапке выпуска (DS-019).
import assert from "node:assert/strict";
import { test } from "node:test";
import { loadStatic } from "./load.mjs";

globalThis.location ??= { search: "?scenario=article", href: "http://127.0.0.1/studio/?scenario=article" };
const { headPlan, quietRerun } = await loadStatic("studio/state.js");

test("готовый и пропущенный свёрнутый шаг — тихая кнопка", () => {
  assert.equal(quietRerun({ status: "done" }, true), true);
  assert.equal(quietRerun({ status: "skipped" }, true), true);
});

test("раскрытый, упавший и ждущий — кнопка видна всегда", () => {
  assert.equal(quietRerun({ status: "done" }, false), false);
  assert.equal(quietRerun({ status: "failed" }, true), false);
  assert.equal(quietRerun({ status: "waiting" }, true), false);
});

test("шапка выпуска: не больше одной главной кнопки в любом состоянии", () => {
  const base = { running: false, settings: { draft: false } };
  const cases = [
    [{ ...base, running: true }, null, { primary: null, visible: ["cancel"], menu: [] }],
    [base, { label: "Опубликовать…" }, { primary: "next", visible: [], menu: ["run_all", "clone", "archive"] }],
    [base, { label: "Запустить всё", runAll: true }, { primary: "run_all", visible: [], menu: ["clone", "archive"] }],
    [{ ...base, settings: { draft: true } }, null, { primary: "make_final", visible: [], menu: ["run_all", "clone", "archive"] }],
    [base, null, { primary: null, visible: [], menu: ["run_all", "clone", "archive"] }],
  ];
  for (const [project, next, want] of cases) assert.deepEqual(headPlan(project, next), want);
});
