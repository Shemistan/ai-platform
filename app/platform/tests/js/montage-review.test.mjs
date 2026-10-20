// Панель ревью «Монтажа» (PRD-028): фильтры замечаний, карточки, раунды, план, сравнение, A/B.
import assert from "node:assert/strict";
import { test } from "node:test";
import { loadStatic } from "./load.mjs";

const { abPair, abSync, changeLabel, comparisonView, filterIssues, filterOptions, issueCard, planRows, roundRow, runSummary, versionRow } =
  await loadStatic("montage/review.js");

const ISSUE = { issue_id: "rev-001-motion_director-001", severity: "major", confidence: 0.8, scene_ids: ["scene_001"], start_frame: 90,
  end_frame: 150, reviewer_roles: ["motion_director", "executive"], reviewer_role: "motion_director", status: "proposed", observation: "сцена висит",
  evidence: [{ type: "heuristic", artifact_id: "frames/f000149.jpg" }, { type: "transcript" }], evidence_type: "transcript", degraded: true,
  change: { op: "scene_shorten", scene_id: "scene_001", frames: 10 }, conflicts_with: [] };
const OTHER = { ...ISSUE, issue_id: "rev-001-sound-001", severity: "suggestion", confidence: 0.9, scene_ids: [], reviewer_roles: ["sound"],
  status: "rejected", change: { op: "none" }, evidence_type: "observed_audio", degraded: false };

test("фильтры и порядок замечаний", () => {
  const all = filterIssues([OTHER, ISSUE]);
  assert.deepEqual(all.map((i) => i.issue_id), ["rev-001-motion_director-001", "rev-001-sound-001"], "серьёзные первыми");
  assert.equal(filterIssues([OTHER, ISSUE], { scene: "scene_001" }).length, 1);
  assert.equal(filterIssues([OTHER, ISSUE], { role: "executive" }).length, 1);
  assert.equal(filterIssues([OTHER, ISSUE], { status: "rejected" })[0].issue_id, "rev-001-sound-001");
  assert.deepEqual(filterOptions([OTHER, ISSUE]), { scenes: ["scene_001"], roles: ["executive", "motion_director", "sound"] });
});

test("карточка замечания: таймкод, улики, правка, деградация", () => {
  const card = issueCard(ISSUE, 30);
  assert.equal(card.at, "0:03");
  assert.deepEqual(card.frames, ["frames/f000149.jpg"]);
  assert.equal(card.change, "укоротить scene_001 на 10 кадров");
  assert.equal(card.evidence, "слова");
  assert.ok(card.degraded && card.canDecide);
  assert.equal(card.severity, "серьёзно");
  assert.equal(issueCard(OTHER).change, "без автоправки");
  assert.equal(changeLabel({ op: "caption_words", max_words: 2 }), "субтитры до 2 слов");
  assert.equal(changeLabel({ op: "cut_words", from: 3, to: 9 }), "вырезать слова 3–9");
});

test("раунд, запуск, план, сравнение", () => {
  const round = roundRow({ n: 1, base_version: "V0", candidate_version: "V1", stages: [{ id: "analyze", status: "done" }, { id: "approve", status: "awaiting" }],
    counts: { total: 4, open: 1, accepted: 2, rejected: 1, fixed: 0 }, scenes_touched: ["scene_001"], decision: "accepted", started_at: 10, finished_at: 70 });
  assert.equal(round.title, "Раунд 1 · V0 → V1");
  assert.deepEqual(round.stages.map((s) => s.tone), ["success", "warning"]);
  assert.equal(round.decision, "принят");
  assert.equal(round.elapsed, 60);
  const run = runSummary({ state: "awaiting_approval", round: 1, rounds_planned: 3, rounds_done: 0, stop_reason: null, versions: ["V0"], accepted_version: "V0",
    active: false, waiting: true, final: false, budget: { gpu_s: 12 } });
  assert.equal(run.state, "ждёт утверждения плана");
  assert.ok(run.waitingPlan && run.canPause && run.canStop && !run.canResume);
  assert.equal(runSummary({ state: "completed", stop_reason: "budget", rounds_planned: 2, final: true }).stop, "бюджет исчерпан");
  const rows = planRows({ actions: [{ action_id: "act-001", op: "scene_extend", scene_id: "scene_002", params: { frames: 30 }, status: "approved",
    approval: "auto", safe: true, estimate: { render_s: 12, assets: 1 }, issue_ids: ["x"] }] });
  assert.equal(rows[0].label, "удлинить scene_002 на 30 кадров");
  assert.equal(rows[0].cost, "≈12 с, ассетов 1");
  const cmp = comparisonView({ decision: "rejected", decided_by: "gates", reason: "av_sync", hard_ok: false,
    hard_gates: [{ id: "file", ok: true, detail: "" }, { id: "av_sync", ok: false, detail: "0.4 с" }],
    objective: { baseline: { qa_errors: 0 }, candidate: { qa_errors: 1 }, worse: ["qa_errors"], better: [] },
    subjective: { mode: "judge", items: [{ dimension: "motion", a: 3, b: 4, evidence: "сцена", weight: 0.2 }], weighted_delta: 0.05, profile: "balanced" },
    fixed_issues: [], regressed_issues: [], baseline: "V0", candidate: "V1" });
  assert.equal(cmp.metrics[0].tone, "danger");
  assert.equal(cmp.subjective[0].b, 4);
  assert.equal(cmp.tone, "warning");
  assert.equal(comparisonView({ pending: true }), null);
});

test("A/B: синхронизация и пара версий", () => {
  assert.equal(abSync(10.0, 10.05), null);
  assert.equal(abSync(10.0, 11.0), 10.0);
  const versions = [{ label: "V0" }, { label: "V1" }, { label: "V2" }];
  assert.deepEqual(abPair({ accepted_version: "V2" }, versions, { base_version: "V1", candidate_version: "V2" }), { a: "V1", b: "V2", labels: ["V0", "V1", "V2"] });
  assert.deepEqual(abPair({ accepted_version: "V0" }, versions, null).b, "V1");
  assert.equal(versionRow({ label: "V1", accepted: false, output: { size: 10, duration: 5, sha256: "abcdef0123456789" } }).status, "отклонена");
});
