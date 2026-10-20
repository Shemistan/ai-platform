// Панель прогресса «Монтажа» (PRD-028): дерево, ETA, обзор, агенты, виртуализация, логи, ресурсы.
import assert from "node:assert/strict";
import { test } from "node:test";
import { loadStatic } from "./load.mjs";

const { agentCards, etaLabel, flatten, logQuery, logRow, mergeEvents, overview, resourceCards, runHistory, windowRows } =
  await loadStatic("montage/progress.js");

const TREE = { id: "p1", kind: "project", title: "Ролик", status: "running", children: [
  { id: "run_1", kind: "run", title: "render", status: "running", percent: 40, elapsed_s: 70, eta: { seconds: 120, low: 90, high: 150, confidence: "medium" },
    counts: { total: 2, completed: 1, running: 1, failed: 0, skipped: 0, remaining: 1 }, children: [
      { id: "run_1:render", kind: "phase", title: "render", status: "running", children: [
        { id: "run_1:segments", kind: "stage", title: "Сборка кусков", status: "completed", percent: 100, children: [] },
        { id: "run_1:final", kind: "stage", title: "Финал", status: "running", percent: 40, current_action: "наложение", units: { completed_units: 12, total_units: 30, unit: "seconds" },
          children: [{ id: "job", kind: "job", title: "ffmpeg", status: "running", metrics: { render_fps: 18.5 }, children: [
            { id: "t", kind: "task", title: "scene_001", status: "failed", blocking_reason: "рендер упал", attempt: 2, children: [] }] }] },
      ] }] }] };

test("дерево → строки, свёртка, подписи", () => {
  const rows = flatten(TREE);
  assert.deepEqual(rows.map((r) => r.depth), [0, 1, 2, 3, 3, 4, 5]);
  assert.equal(rows[4].units, "12/30 seconds");
  assert.equal(rows[5].metrics, "18.5 fps");
  assert.equal(rows[6].blocking, "рендер упал");
  assert.equal(rows[6].attempt, "попытка 2");
  assert.equal(flatten(TREE, new Set(["run_1"])).length, 2);
  assert.equal(rows[1].eta, "≈ 2 мин (2 мин–3 мин), оценка средняя");
  assert.equal(etaLabel(null), "пока неизвестно");
  assert.equal(etaLabel({ basis: "idle" }), "");
});

test("обзор и история запусков", () => {
  const o = overview(TREE, { round: 1, rounds_planned: 3 });
  assert.equal(o.stages, "1/2");
  assert.equal(o.tasks, "0/1");
  assert.equal(o.current, "Финал — наложение");
  assert.equal(o.round, "1 / 3");
  assert.match(o.lastError, /scene_001/);
  const events = [
    { seq: 1, run_id: "r1", phase_id: "render", type: "run_started", status: "running", occurred_at: 100, stage_id: "segments" },
    { seq: 2, run_id: "r1", type: "run_finished", status: "completed", occurred_at: 160, reason: "" },
    { seq: 3, run_id: "r2", phase_id: "review", round_id: "round-001", type: "run_queued", status: "queued", occurred_at: 170 },
  ];
  const history = runHistory(events);
  assert.equal(history[0].id, "r2");
  assert.equal(history[1].duration, "1 мин");
  assert.equal(history[1].stages, 1);
});

test("агенты, виртуализация, логи, ресурсы", () => {
  const cards = agentCards([
    { agent_id: "sound", type: "task_started", status: "running", message: "смотрит", occurred_at: 1 },
    { agent_id: "sound", type: "reviewer_finished", status: "completed", message: "готово", occurred_at: 2, metrics: { issues: 2, elapsed_s: 1.5 } },
  ], [{ id: "sound", label: "Звук" }]);
  assert.deepEqual([cards[0].label, cards[0].done, cards[0].issues, cards[0].latency, cards[0].statusLabel], ["Звук", 1, 2, 1.5, "готово"]);
  const w = windowRows(100000, 28 * 5000, 560, 28, 8);
  assert.equal(w.first, 4992);
  assert.equal(w.last, 4992 + 20 + 16);
  assert.equal(w.height, 2800000);
  const row = logRow({ seq: 7, occurred_at: 0, severity: "error", type: "job_failed", status: "failed", stage_id: "final", job_id: "j1", message: "упал", progress: { unit: "seconds", completed_units: 12 } });
  assert.equal(row.where, "final · j1");
  assert.equal(row.tone, "danger");
  assert.equal(row.seconds, 12);
  assert.equal(logQuery({ severity: "error", q: "" }, 40, 100), "severity=error&cursor=40&limit=100");
  const merged = mergeEvents([{ seq: 1 }, { seq: 3 }], [{ seq: 3 }, { seq: 2 }, { seq: 4 }], 3);
  assert.deepEqual(merged.map((e) => e.seq), [2, 3, 4]);
  const cards2 = resourceCards({ cpu: { load_1m: 3.2, cores: 10 }, ram: { total_bytes: 32 * 2 ** 30, used_bytes: 16 * 2 ** 30 }, disk: { free_bytes: 100 * 2 ** 30 },
    gpu: { status: "unavailable", reason: "нет API" }, queue: { depth: 1, gpu_waiting: 0 }, render: { fps: 20, speed: 1.2 }, retries: 1 });
  assert.deepEqual(cards2.map((c) => c.id), ["cpu", "ram", "disk", "gpu", "queue", "render", "retries"]);
  assert.equal(cards2[1].value, "16.0 ГБ из 32.0 ГБ");
  assert.equal(cards2[3].value, "нет метрик");
});
