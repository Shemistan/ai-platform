// Центр активности шапки (UX-036): подписи кнопки, видеокарты и строки задачи.
import assert from "node:assert/strict";
import { test } from "node:test";
import { loadStatic } from "./load.mjs";

const { activityLine, activitySummary } = await loadStatic("core/activity.js");

test("кнопка и видеокарта: пусто и занято с очередью", () => {
  assert.deepEqual(activitySummary({}), { count: 0, label: "Задач нет", gpu: "Видеокарта свободна" });
  const busy = activitySummary({ activity: [{}, {}], gpu: "Фото", waiting: 1 });
  assert.equal(busy.label, "Задачи: 2");
  assert.equal(busy.gpu, "Видеокарту занимает «Фото» · ждут очереди: 1");
});

test("строка задачи: очередь, этап с процентом, без данных", () => {
  assert.equal(activityLine({ status: "queued", stage: "В очереди" }), "в очереди");
  assert.equal(activityLine({ status: "running", stage: "Шаг 3/9", percent: 33.4 }), "Шаг 3/9 · 33%");
  assert.equal(activityLine({ status: "running", stage: "", percent: null }), "идёт");
});
