// Экраны MASTER_SPEC «Монтажа» (PRD-027): карточки сцен, правки, биты, полосы таймлайна, QA, ворота.
import assert from "node:assert/strict";
import { test } from "node:test";
import { loadStatic } from "./load.mjs";

const { beatRows, gateState, parseClock, qaGroups, sceneCard, scenePatch, trackBars } = await loadStatic("montage/storyboard.js");

const SCENE = { id: "scene_002", intent: "Буфер канала", template_id: "flow_diagram", edit_start_frame: 900, duration_frames: 120,
  status: "rendered", enabled: true, score: 20, rationale: "механизм", grounding_claim_ids: ["claim_001"] };
const TEMPLATES = [{ id: "flow_diagram", label: "Схема-поток" }];

test("карточка сцены: подписи, время, оценка", () => {
  const card = sceneCard(SCENE, TEMPLATES, 30);
  assert.equal(card.template, "Схема-поток");
  assert.equal(card.at, "0:30");
  assert.equal(card.seconds, 4);
  assert.equal(card.statusLabel, "отрисована");
  assert.equal(card.score, "20/25");
  assert.equal(card.scoreTone, "success");
  assert.equal(sceneCard({ ...SCENE, score: 14, status: "blocked", enabled: false }, [], 30).tone, "warning");
});

test("правка сцены: секунды в кадры, границы", () => {
  assert.deepEqual(scenePatch(SCENE, { start: "1:00", seconds: "5" }, 30, 9000).patch, { id: "scene_002", edit_start_frame: 1800, duration_frames: 150 });
  assert.deepEqual(scenePatch(SCENE, { enabled: false }, 30, 9000).patch, { id: "scene_002", enabled: false });
  assert.match(scenePatch(SCENE, { seconds: "20" }, 30, 9000).error, /от 2,5 до 12/);
  assert.match(scenePatch(SCENE, { start: "abc" }, 30, 9000).error, /время/);
  assert.match(scenePatch(SCENE, { start: "4:59" }, 30, 9000).error, /конец ролика/);
  assert.equal(parseClock("1:02:05"), 3725);
  assert.equal(parseClock("83.5"), 83.5);
  assert.equal(parseClock(""), null);
});

test("биты и полосы таймлайна", () => {
  const rows = beatRows([{ id: "beat_001", start_frame: 0, end_frame: 300, purpose: "hook", goal: "зачем", priority: 4, claims: ["c"] },
    { id: "beat_002", start_frame: 300, end_frame: 300, purpose: "zzz", goal: "", priority: 1, claims: [] }], 30);
  assert.deepEqual(rows.map((r) => [r.purpose, r.claims, r.cut]), [["зацепка", 1, false], ["zzz", 0, true]]);
  const bars = trackBars({ duration_frames: 1000, tracks: [{ id: "presenter", clips: [{ id: "seg_001", edit_in_frame: 0, edit_out_frame: 500, layout: "head" }] },
    { id: "overlays", clips: [{ id: "scene_001", scene_id: "scene_001", edit_in_frame: 250, edit_out_frame: 2000, enabled: false }] }] });
  assert.equal(bars[0].label, "Голова");
  assert.deepEqual([bars[0].bars[0].left, bars[0].bars[0].width], [0, 50]);
  assert.equal(bars[1].bars[0].enabled, false);
  assert.equal(bars[1].bars[0].scene, "scene_001");
  assert.deepEqual(trackBars(null), []);
});

test("QA по серьёзности и ворота", () => {
  const groups = qaGroups([{ severity: "error" }, { severity: "warning" }, { severity: "weird" }]);
  assert.deepEqual([groups.error.length, groups.warning.length, groups.info.length], [1, 1, 1]);
  const project = { transcript: { words: 5 }, storyboard: null, qa: null, gates: { edl: { required: true, approved: false }, storyboard: { required: true, approved: false }, export: { required: false, approved: true, auto: true } } };
  assert.deepEqual(gateState(project, "edl"), { show: true, approved: false, auto: undefined, text: "ждёт подтверждения: план вырезок" });
  assert.equal(gateState(project, "storyboard").show, false, "раскадровки ещё нет");
  assert.equal(gateState({ ...project, qa: {} }, "export").text, "подтверждено автономно");
  assert.deepEqual(gateState({}, "edl"), { show: false });
});
