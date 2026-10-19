// Этапы работы «Монтажа» (PRD-025): подписи времени, строки списка, сводка.
import assert from "node:assert/strict";
import { test } from "node:test";
import { loadStatic } from "./load.mjs";

const { span, stageRows, summary } = await loadStatic("montage/stages.js");

test("длительность для людей", () => {
  assert.equal(span(40.4), "40 с");
  assert.equal(span(750), "13 мин");
  assert.equal(span(3720), "1 ч 2 мин");
  assert.equal(span(7200), "2 ч");
  assert.equal(span(null), "");
});

const STAGES = [
  { id: "audio", label: "Звук из записи", status: "done", percent: 100, started_at: 1000, finished_at: 1030 },
  { id: "transcribe", label: "Расшифровка", status: "running", percent: 41.6, detail: "кусок 3 из 14", started_at: 1030, eta_s: 600 },
  { id: "plan", label: "План монтажа (ИИ)", status: "pending", percent: 0 },
  { id: "motion", label: "Моушн-вставки", status: "skipped", percent: 0 },
];

test("строки: сколько заняло, сколько осталось, что впереди", () => {
  const rows = stageRows(STAGES, 1130);
  assert.deepEqual(rows.map((r) => [r.status, r.note]), [["done", "30 с"], ["running", "2 мин · осталось ≈ 10 мин"], ["pending", ""], ["skipped", "пропущен"]]);
  assert.equal(rows[1].percent, 42);
  assert.equal(rows[1].detail, "кусок 3 из 14");
});

test("сводка: этап N из M, прошло с начала, осталось всего", () => {
  assert.deepEqual(summary({ stages: STAGES, eta_s: 1500 }, 1130), { step: "Этап 2 из 4", elapsed: "2 мин", left: "осталось ≈ 25 мин" });
  assert.equal(summary({ stages: STAGES, eta_s: null }, 1130).left, "оценка появится по ходу");
  assert.deepEqual(summary({}, 0), { step: "", elapsed: "", left: "" });
});
