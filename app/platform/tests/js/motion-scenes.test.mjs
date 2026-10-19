// «Моушн» (PRD-026): разбор времени, статусы и сводка сцен.
import assert from "node:assert/strict";
import { test } from "node:test";
import { loadStatic } from "./load.mjs";

const { parseClock, clock, sceneStatus, sceneSummary, byTime } = await loadStatic("motion/scenes.js");

test("время ролика — из того, что набрал человек", () => {
  assert.equal(parseClock("12:30"), 750);
  assert.equal(parseClock("1:02:05"), 3725);
  assert.equal(parseClock(" 90 "), 90);
  assert.equal(parseClock("1:5,5"), 65.5);
  assert.equal(parseClock("1:75"), null);
  assert.equal(parseClock("завтра"), null);
  assert.equal(parseClock(""), null);
  assert.equal(clock(65), "1:05");
  assert.equal(clock(3725), "1:02:05");
});

test("статус сцены: в работе — какая роль", () => {
  assert.deepEqual(sceneStatus({ status: "working", step: "animate" }), { label: "аниматор пишет код", tone: "info" });
  assert.equal(sceneStatus({ status: "dropped" }).label, "снята техредом");
  assert.equal(sceneStatus({ status: "done", enabled: false }).label, "выключена");
  assert.equal(sceneStatus({ status: "непонятно" }).label, "непонятно");
});

test("сводка и порядок сцен", () => {
  const scenes = [
    { at: 90, status: "done" },
    { at: 30, status: "done", enabled: false },
    { at: 60, status: "dropped" },
    { at: 10, status: "failed" },
  ];
  assert.deepEqual(sceneSummary(scenes), { total: 4, done: 2, packed: 1, dropped: 1, failed: 1 });
  assert.deepEqual(byTime(scenes).map((s) => s.at), [10, 30, 60, 90]);
  assert.deepEqual(sceneSummary(null).total, 0);
});
