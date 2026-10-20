"""Итеративное ревью «Монтажа» (ITERATIVE_REVIEW_SPEC, PRD-028): политика, состояния, версии, API запусков."""

import json
import tempfile
import unittest
from pathlib import Path

from aiplatform.montage import schemas
from aiplatform.montage.review import compare, issues, planner, policy, state, versions
from aiplatform.montage.service import MontageModule
from tests.fixtures import FAST, NEEDS_FFMPEG
from tests.helpers import PlatformCase


class PolicyTest(unittest.TestCase):
    def test_defaults_and_cleaning(self):
        clean = policy.clean({})
        self.assertEqual(schemas.validate("review_policy", clean), [])
        self.assertEqual((clean["max_rounds"], clean["mode"]), (2, "manual_approval"))
        self.assertFalse(clean["allow_narrative_edits"])
        custom = policy.clean({"max_rounds": 5, "mode": "autonomous", "review_profile": "educational",
                               "protected_ranges": [{"from": 3, "to": 9}], "locked_scenes": ["scene_002", "scene_002"],
                               "reviewer_roles": ["sound", "technical_qa"], "max_gpu_minutes": 1})  # fmt: skip
        self.assertEqual(custom["locked_scenes"], ["scene_002"])
        self.assertEqual(custom["max_gpu_minutes"], 1.0)
        self.assertEqual(schemas.validate("review_policy", custom), [])
        bads = ({"max_rounds": 6}, {"max_rounds": 0}, {"mode": "yolo"}, {"protected_ranges": [{"from": 5, "to": 1}]},
                {"reviewer_roles": ["ghost"]}, {"reviewer_roles": []}, {"quality_target": 2},
                {"max_render_attempts_per_issue": 1.5})  # fmt: skip
        for bad in bads:
            with self.assertRaises(policy.PolicyError, msg=bad):
                policy.clean(bad)

    def test_safety_protection_and_budget(self):
        self.assertTrue(policy.is_safe("scene_extend", {"frames": 30}))
        self.assertFalse(policy.is_safe("scene_extend", {"frames": 300}))
        self.assertFalse(policy.is_safe("scene_disable"))
        self.assertFalse(policy.is_safe("cut_words", {"from": 1, "to": 2}))
        rules = policy.clean({"protected_ranges": [{"from": 10, "to": 20}], "locked_scenes": ["scene_003"],
                              "protected_claims": ["claim_002"]})  # fmt: skip
        self.assertIn("заблокирована", policy.protected(rules, scene_id="scene_003"))
        self.assertIn("защищённом", policy.protected(rules, words=(18, 25)))
        self.assertEqual(policy.protected(rules, words=(21, 25)), "")
        self.assertIn("claim_002", policy.protected(rules, claim_ids=["claim_002"]))
        used = {"gpu_s": 70.0, "wall_s": 10.0, "new_assets": 0}
        self.assertIn("тяжёлых процессов", policy.exhausted(policy.clean({"max_gpu_minutes": 1}), used))
        self.assertEqual(policy.exhausted(policy.clean({"max_gpu_minutes": 0}), used), "", "0 — без лимита")
        self.assertIn("ассетов", policy.exhausted(policy.clean({"max_new_assets": 0}), used))
        self.assertAlmostEqual(sum(policy.weights(rules).values()), 1.0, places=6)


class StateTest(unittest.TestCase):
    def test_transitions_history_and_checkpoint(self):
        run = state.new_run("p1", policy.clean({}), lock={"project_updated_at": 1.0, "revision": 0})
        self.assertEqual(run["state"], "idle")
        state.advance(run, "preparing", reason="start")
        state.advance(run, "reviewing")
        with self.assertRaises(state.StateError):
            state.advance(run, "accepted")
        state.advance(run, "paused", reason="user")
        self.assertEqual(run["checkpoint"]["state"], "reviewing", "пауза не двигает чекпоинт")
        state.advance(run, "reviewing", reason="resume")
        self.assertEqual([h["to"] for h in run["history"]], ["preparing", "reviewing", "paused", "reviewing"])
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            state.checkpoint(folder, run)
            self.assertEqual(state.load_run(folder, run["run_id"])["state"], "reviewing")
            self.assertEqual([r["run_id"] for r in state.list_runs(folder)], [run["run_id"]])
            paused = state.interrupted(folder)
            self.assertEqual((paused[0]["state"], paused[0]["blocking_reason"]), ("paused", state.INTERRUPTED))
            self.assertEqual(state.load_run(folder, run["run_id"])["checkpoint"]["state"], "reviewing")
            self.assertEqual(state.interrupted(folder), [], "второй старт ничего не трогает")

    def test_rounds(self):
        run = state.new_run("p1", policy.clean({}), lock={"project_updated_at": 1.0})
        state.advance(run, "preparing")
        item = state.new_round(run, "V0")
        run["round"] = item["n"]
        self.assertEqual((item["n"], item["round_id"], len(item["stages"])), (1, "round-001", 7))
        state.round_stage(item, "analyze", "running", "роли")
        state.round_stage(item, "consolidate", "running")
        self.assertEqual([s["status"] for s in item["stages"][:3]], ["done", "running", "pending"])
        state.round_stage(item, "approve", "awaiting", "ждёт человека")
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            state.save_round(folder, run, item)
            state.checkpoint(folder, run)
            self.assertEqual(state.load_round(folder, run["run_id"], 1)["stages"][2]["status"], "awaiting")
            self.assertEqual(run["rounds"][0]["round_id"], "round-001")
            self.assertEqual(state.load_run(folder, run["run_id"])["rounds"][0]["n"], 1)
        public = state.public(run)
        self.assertEqual((public["rounds_planned"], public["rounds_done"], public["active"]), (2, 0, True))


@NEEDS_FFMPEG
class VersionsTest(unittest.TestCase):
    def setUp(self):
        from tests.test_montage import make_video

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        make_video(self.root / "output.mp4", 2)
        (self.root / "timeline.json").write_text(json.dumps({"duration_frames": 60, "fps": 30, "tracks": []}))
        self.board("aaa", 30)
        (self.root / "qa_report.json").write_text(json.dumps({"gates": {"mechanical": "pass"}, "counts": {"error": 0}}))

    def board(self, digest: str, frames: int) -> None:
        scene = {"id": "scene_001", "hash": digest, "edit_start_frame": 0, "duration_frames": frames, "enabled": True}
        (self.root / "storyboard.json").write_text(json.dumps({"scenes": [scene]}))

    def test_save_is_immutable_restore_and_diff(self):
        v0 = versions.save(self.root, 0, round_n=0, parent=None, accepted=True, ffmpeg="ffmpeg")
        self.assertEqual(schemas.validate("review_version", v0), [])
        self.assertEqual((v0["label"], v0["parent"], v0["output"]["duration"] > 1.5), ("V0", None, True))
        with self.assertRaises(versions.VersionError):
            versions.save(self.root, 0, round_n=1, parent="V0", accepted=None, ffmpeg="ffmpeg")
        # «Пересборка»: новый output.mp4 (новый inode) и правленая сцена — V0 не меняется.
        from tests.test_montage import make_video

        make_video(self.root / "output.tmp.mp4", 3)
        (self.root / "output.tmp.mp4").replace(self.root / "output.mp4")
        self.board("bbb", 45)
        self.assertIsNone(versions.integrity(self.root, 0))
        v1 = versions.save(self.root, 1, round_n=1, parent="V0", accepted=None, ffmpeg="ffmpeg")
        self.assertNotEqual(v1["output"]["sha256"], v0["output"]["sha256"])
        diff = versions.diff(self.root, 0, 1)
        self.assertEqual(diff["scenes_changed"], ["scene_001"])
        self.assertTrue(diff["output_changed"])
        self.assertEqual(diff["frames_changed"], [{"from": 0, "to": 30}, {"from": 0, "to": 45}])
        versions.mark(self.root, 1, accepted=False, reason="хуже")
        self.assertEqual([v["accepted"] for v in versions.listing(self.root)], [True, False])
        restored = versions.restore(self.root, 0)
        self.assertEqual(restored["label"], "V0")
        self.assertEqual(json.loads((self.root / "storyboard.json").read_text())["scenes"][0]["hash"], "aaa")
        from aiplatform.montage import decisions

        self.assertEqual(decisions.file_sha(self.root / "output.mp4"), v0["output"]["sha256"])
        self.assertIsNone(versions.integrity(self.root, 1), "откат не трогает кандидата")
        self.assertEqual(versions.number("V12"), 12)
        self.assertIsNone(versions.number("x"))
        self.assertIsNone(versions.file(self.root, 0, "../secret"))


def montage_only(ctx):
    return [MontageModule(ctx)]


@NEEDS_FFMPEG
class ReviewApiTest(PlatformCase):
    modules = staticmethod(montage_only)
    process_env = FAST

    @classmethod
    def setUpClass(cls):
        from tests.test_montage import make_video

        cls.media = tempfile.TemporaryDirectory()
        cls.head = Path(cls.media.name) / "head.mp4"
        make_video(cls.head, 12)

    @classmethod
    def tearDownClass(cls):
        cls.media.cleanup()

    def folder(self, pid) -> Path:
        return Path(self.tmp.name) / "montage" / "projects" / pid

    def settle(self, pid, statuses, timeout=240):
        return self.poll(f"/api/montage/projects/{pid}", lambda p: p["status"] in statuses, timeout=timeout)

    def rendered(self) -> str:
        payload = {"title": "Ревью", "options": {"check": False, "mode": "autonomous"}}
        pid = self.json("POST", "/api/montage/projects", payload)[1]["id"]
        self.assertEqual(self.json("POST", f"/api/montage/projects/{pid}/sources/head", self.head.read_bytes())[0], 201)
        self.json("POST", f"/api/montage/projects/{pid}/start")
        done = self.settle(pid, ("done", "failed"))
        self.assertEqual(done["status"], "done", done)
        return pid

    def test_create_control_versions_and_stale_lock(self):
        base = "/api/montage/projects"
        _, cfg = self.json("GET", f"{base[:-9]}/config")
        self.assertEqual([m["id"] for m in cfg["review"]["modes"]], ["manual_approval", "auto_safe", "autonomous"])
        pid = self.json("POST", base, {"title": "Пустой"})[1]["id"]
        self.assertEqual(self.json("POST", f"{base}/{pid}/review-runs", {})[0], 409, "ролика ещё нет")
        pid = self.rendered()
        self.assertEqual(self.json("POST", f"{base}/{pid}/review-runs", {"max_rounds": 9})[0], 400)
        status, run = self.json("POST", f"{base}/{pid}/review-runs", {"max_rounds": 3, "mode": "auto_safe"})
        self.assertEqual(status, 201, run)
        self.assertEqual((run["state"], run["versions"], run["accepted_version"]), ("idle", ["V0"], "V0"))
        self.assertEqual(run["capabilities"]["vision"], "unavailable", "VLM на машине нет — честно")
        self.assertEqual(self.json("POST", f"{base}/{pid}/review-runs", {})[0], 409, "один запуск на проект")
        _, listing = self.json("GET", f"{base}/{pid}/review-runs")
        self.assertEqual([r["run_id"] for r in listing["runs"]], [run["run_id"]])
        _, vers = self.json("GET", f"{base}/{pid}/versions")
        self.assertEqual([v["label"] for v in vers["versions"]], ["V0"])
        status, _, body = self.call("GET", f"{base}/{pid}/versions/V0/video")
        self.assertEqual((status, body[:4] != b""), (200, True))
        self.assertEqual(self.call("GET", f"{base}/{pid}/versions/V7/video")[0], 404)
        rid = run["run_id"]
        self.assertEqual(self.json("POST", f"{base}/{pid}/review-runs/{rid}/resume")[0], 409)
        # Ролик без раскадровки: роли находят лишь то, что нельзя исполнить, — раунд честно завершается.
        status, started = self.json("POST", f"{base}/{pid}/review-runs/{rid}/start")
        self.assertEqual(status, 200, started)
        done = self.settle(pid, ("done", "failed"))
        self.assertEqual(done["status"], "done", done["error"])
        got = self.poll(f"{base}/{pid}/review-runs/{rid}", lambda r: r["final"] or r["waiting"])
        self.assertEqual(got["state"], "completed", got)
        # Без сцен и субтитров в кадре правки либо неисполнимы, либо не меняют ролик (sha256 тот же) — обе
        # причины остановки честные; V0 цел.
        self.assertIn(got["stop_reason"], ("no_executable_actions", "no_material_gain"), got)
        self.assertIn(got["rounds"][0]["decision"], ("no_actions", "rejected"))
        self.assertIsNone(versions.integrity(self.folder(pid), 0))
        self.assertTrue((self.folder(pid) / "review" / "bundles" / "V0" / "bundle.json").is_file())
        self.assertEqual(self.json("POST", f"{base}/{pid}/review-runs/{rid}/stop")[0], 409, "уже завершён")
        # Новый запуск: ролик тот же → baseline остаётся V0; правка плана снаружи делает запуск устаревшим.
        _, run2 = self.json("POST", f"{base}/{pid}/review-runs", {})
        self.assertEqual(run2["versions"], ["V0"])
        self.json("POST", f"{base}/{pid}/plan", {"cuts": [{"from": 0, "to": 0, "kind": "human"}], "layout": []})
        status, err = self.json("POST", f"{base}/{pid}/review-runs/{run2['run_id']}/start")
        self.assertEqual(status, 409, err)
        self.assertIn("устарел", err["error"])
        self.assertEqual(self.json("GET", f"{base}/{pid}/review-runs/{run2['run_id']}")[1]["state"], "blocked")
        # Рестарт сервиса: активный запуск → paused с причиной.
        from aiplatform.montage.projects import MontageProjects

        folder = Path(self.tmp.name) / "montage" / "projects" / pid
        run3 = state.load_run(folder, run2["run_id"])
        run3["state"] = "reviewing"
        state.checkpoint(folder, run3)
        module = self.app.modules["montage"]
        again = MontageProjects(module.config, module.ctx.gate)
        try:
            reloaded = again.review.get(pid, run2["run_id"])
            self.assertEqual((reloaded["state"], reloaded["blocking_reason"]), ("paused", state.INTERRUPTED))
        finally:
            again.close()


def scene(sid: str, start: int, frames: int = 120, claims=("claim_001",)) -> dict:
    return {"id": sid, "edit_start_frame": start, "duration_frames": frames, "hash": "h" + sid[-3:],
            "enabled": True, "status": "rendered", "props": {"title": "x"}, "template_id": "chapter_title",
            "grounding_claim_ids": list(claims), "fps": {"num": 30, "den": 1}}  # fmt: skip


def make_issue(role: str, n: int, **kw) -> dict:
    base = {"round_id": "round-001", "revision_id": "V0", "category": "motion_timing", "severity": "major",
            "confidence": 0.8, "scene_ids": ["scene_001"], "start_frame": 100, "end_frame": 200,
            "evidence": [{"type": "transcript", "words": {"from": 1, "to": 5}}], "observation": "сцена кончается рано",
            "change": {"op": "scene_extend", "scene_id": "scene_001", "frames": 30}}  # fmt: skip
    return issues.make(role, n, **(base | kw))


class IssuesTest(unittest.TestCase):
    def test_make_validate_and_fingerprint(self):
        item = make_issue("motion_director", 1)
        self.assertEqual(schemas.validate("review_issue", item), [])
        self.assertEqual(item["issue_id"], "rev-001-motion_director-001")
        self.assertEqual((item["rank"], item["evidence_type"]), ("clarity", "transcript"))
        self.assertEqual(item["fingerprint"], "motion_timing:scene_001:scene_extend")
        self.assertEqual(issues.validate(item, duration_frames=1000, scene_ids={"scene_001"}, n_words=50), [])
        bad = make_issue("motion_director", 2, scene_ids=["scene_009"], evidence=[], end_frame=50)
        problems = issues.validate(bad, duration_frames=1000, scene_ids={"scene_001"}, n_words=50)
        self.assertTrue(any("scene_009" in p for p in problems))
        self.assertTrue(any("evidence" in p for p in problems))
        self.assertTrue(any("end_frame" in p for p in problems))
        cut = make_issue("narrative", 3, change={"op": "cut_words", "from": 10, "to": 80}, scene_ids=[])
        self.assertTrue(
            any("cut_words" in p for p in issues.validate(cut, duration_frames=1000, scene_ids=set(), n_words=50))
        )
        self.assertEqual(issues.rank_of("qa_black_frames"), "technical")
        self.assertEqual(issues.rank_of("factual_grounding"), "factual")
        self.assertEqual(issues.rank_of("something_else"), "aesthetic")
        strong = make_issue("sound", 4, evidence=[{"type": "heuristic"}, {"type": "observed_audio", "value": "x"}])
        self.assertEqual(strong["evidence_type"], "observed_audio")

    def test_dedup_merges_roles_and_evidence(self):
        a = make_issue("motion_director", 1, confidence=0.7)
        b = make_issue(
            "executive", 1, confidence=0.9, severity="minor", evidence=[{"type": "heuristic", "value": "кадр"}]
        )
        c = make_issue("executive", 2, scene_ids=["scene_002"], change={"op": "scene_extend", "scene_id": "scene_002"})
        out = issues.dedup([a, b, c])
        self.assertEqual(len(out), 2)
        merged = out[0]
        self.assertEqual(merged["reviewer_roles"], ["motion_director", "executive"])
        self.assertEqual(merged["confidence"], 0.9)
        self.assertEqual(merged["severity"], "major", "серьёзность — самая строгая")
        self.assertEqual(len(merged["evidence"]), 2)
        self.assertEqual(merged["merged_from"], [b["issue_id"]])
        # Без сцен — по пересечению диапазона кадров ≥ 50 %.
        g1 = make_issue(
            "sound", 1, category="silence", scene_ids=[], start_frame=100, end_frame=200, change={"op": "none"}
        )
        g2 = make_issue(
            "accessibility", 1, category="silence", scene_ids=[], start_frame=150, end_frame=260, change={"op": "none"}
        )
        g3 = make_issue(
            "accessibility", 2, category="silence", scene_ids=[], start_frame=600, end_frame=700, change={"op": "none"}
        )
        self.assertEqual(len(issues.dedup([g1, g2, g3])), 2)

    def test_conflicts_arbiter_and_order(self):
        extend = make_issue("motion_director", 1, confidence=0.9)
        shorten = make_issue(
            "retention", 1, confidence=0.88, change={"op": "scene_shorten", "scene_id": "scene_001", "frames": 20}
        )
        pairs = issues.conflicts([extend, shorten])
        self.assertEqual(len(pairs), 1)
        self.assertEqual(extend["conflicts_with"], [shorten["issue_id"]])
        log = issues.arbitrate([extend, shorten])
        self.assertEqual((extend["status"], shorten["status"]), ("deferred", "deferred"), "ничья → человек")
        self.assertIsNone(log[0]["winner"])
        # Выше ранг побеждает: техническое против эстетического.
        tech = make_issue("technical_qa", 1, category="qa_scene_failed", severity="major", confidence=0.6,
                          change={"op": "scene_disable", "scene_id": "scene_001"})  # fmt: skip
        pretty = make_issue("art_director", 1, category="style_drift", severity="minor", confidence=0.95)
        log = issues.arbitrate([tech, pretty])
        self.assertEqual(
            (tech["status"], pretty["status"], log[0]["winner"]), ("proposed", "rejected", tech["issue_id"])
        )
        self.assertIn("арбитр", pretty["status_reason"])
        ordered = sorted([pretty, tech, make_issue("x", 2, category="factual_grounding")], key=issues.order_key)
        self.assertEqual([i["rank"] for i in ordered], ["technical", "factual", "aesthetic"])
        self.assertFalse(issues.opposed(make_issue("a", 1), make_issue("b", 1)), "одинаковые правки — не конфликт")

    def test_policy_protection_and_approval_need(self):
        rules = policy.clean({"locked_scenes": ["scene_001"], "protected_claims": ["claim_007"]})
        locked = make_issue("motion_director", 1)
        claim = make_issue(
            "educational", 1, scene_ids=["scene_002"], change={"op": "scene_disable", "scene_id": "scene_002"}
        )
        speech = make_issue("narrative", 1, scene_ids=[], change={"op": "cut_words", "from": 3, "to": 4})
        free = make_issue(
            "motion_director", 2, scene_ids=["scene_003"], change={"op": "scene_extend", "scene_id": "scene_003"}
        )
        scenes = {"scene_001": scene("scene_001", 0), "scene_002": scene("scene_002", 300, claims=("claim_007",)),
                  "scene_003": scene("scene_003", 600)}  # fmt: skip
        issues.apply_policy([locked, claim, speech, free], rules, scenes=scenes)
        self.assertIn("заблокирована", locked["status_reason"])
        self.assertIn("claim_007", claim["status_reason"])
        self.assertIn("allow_narrative_edits", speech["status_reason"])
        self.assertEqual(free["status"], "proposed")
        self.assertFalse(issues.needs_approval(free), "улика по словам — проверяемая")
        blind = make_issue("composition", 1, evidence=[{"type": "heuristic", "value": "кадр"}])
        self.assertTrue(issues.needs_approval(blind))
        self.assertEqual(issues.counts([locked, claim, speech, free])["rejected"], 3)


class PlannerTest(unittest.TestCase):
    SCENES = [scene("scene_001", 100), scene("scene_002", 1000)]

    def plan(self, items, mode="auto_safe", used=None, previous=(), **policy_extra):
        rules = policy.clean({"mode": mode, **policy_extra})
        return planner.plan(items, policy=rules, scenes=self.SCENES, fps=30, total_frames=3000, duration_s=100.0,
                            round_id="round-001", revision_id="V0", budget_used=used or {"gpu_s": 0, "wall_s": 0, "new_assets": 0},
                            previous_fingerprints=set(previous))  # fmt: skip

    def test_actions_dag_estimate_and_modes(self):
        extend = make_issue("motion_director", 1)
        shift = make_issue("composition", 1, category="composition_intro", severity="minor",
                           evidence=[{"type": "heuristic", "value": "кадр"}],
                           change={"op": "scene_shift", "scene_id": "scene_001", "frames": 40})  # fmt: skip
        words = make_issue("narrative", 1, scene_ids=[], category="caption_speed",
                           change={"op": "caption_words", "max_words": 2})  # fmt: skip
        words2 = make_issue("accessibility", 1, scene_ids=[], category="caption_speed",
                            change={"op": "caption_words", "max_words": 1})  # fmt: skip
        data = self.plan([extend, shift, words, words2], mode="auto_safe")
        self.assertEqual(schemas.validate("repair_plan", data), [])
        by_op = {a["op"]: a for a in data["actions"]}
        self.assertEqual(set(by_op), {"scene_extend", "scene_shift", "caption_words"}, "одинаковые правки слиты")
        self.assertEqual(by_op["caption_words"]["params"]["max_words"], 1)
        self.assertEqual(len(by_op["caption_words"]["issue_ids"]), 2)
        self.assertEqual(
            by_op["scene_shift"]["depends_on"], [by_op["scene_extend"]["action_id"]], "одна сцена — по очереди"
        )
        self.assertEqual(by_op["scene_extend"]["estimate"]["assets"], 1)
        self.assertEqual(by_op["scene_extend"]["affected_frames"], [{"from": 100, "to": 220}, {"from": 100, "to": 250}])
        self.assertEqual(by_op["scene_extend"]["asset_hash_before"], "h001")
        self.assertEqual((by_op["scene_extend"]["status"], by_op["scene_extend"]["approval"]), ("approved", "auto"))
        self.assertEqual((by_op["scene_shift"]["status"], by_op["scene_shift"]["approval"]), ("deferred", "required"),
                         "только эвристическая улика → ждёт человека, остальное идёт")  # fmt: skip
        self.assertEqual(data["approval"], "not_needed")
        self.assertEqual(extend["status"], "accepted")
        manual = self.plan([make_issue("motion_director", 1), make_issue("narrative", 1, scene_ids=[],
                            change={"op": "caption_words", "max_words": 2})], mode="manual_approval")  # fmt: skip
        self.assertEqual(manual["approval"], "required")
        self.assertTrue(all(a["status"] == "proposed" and a["approval"] == "required" for a in manual["actions"]))
        disable = make_issue("educational", 1, category="factual_grounding", change={"op": "scene_disable", "scene_id": "scene_002"},
                             scene_ids=["scene_002"])  # fmt: skip
        auto = self.plan([disable], mode="autonomous")
        self.assertEqual(
            auto["approval"], "required", "смысловое удаление в autonomous — ворота, если больше нечего делать"
        )
        self.assertEqual(auto["actions"][0]["status"], "proposed")
        ordered = planner.ready(data)
        self.assertEqual(
            [a["op"] for a in ordered][:2],
            ["scene_extend", "caption_words"]
            if ordered[1]["op"] == "caption_words"
            else ["scene_extend", "scene_shift"],
        )

    def test_budget_and_oscillation(self):
        a = make_issue("motion_director", 1)
        b = make_issue(
            "motion_director",
            2,
            scene_ids=["scene_002"],
            change={"op": "scene_extend", "scene_id": "scene_002", "frames": 30},
        )
        data = self.plan([a, b], max_new_assets=1)
        statuses = [x["status"] for x in data["actions"]]
        self.assertEqual(statuses, ["approved", "deferred"])
        self.assertFalse(data["estimate"]["within_budget"])
        self.assertIn("бюджет", data["actions"][1]["status_reason"])
        tight = self.plan([make_issue("motion_director", 1)], max_gpu_minutes=0.01)
        self.assertEqual(tight["actions"][0]["status"], "deferred")
        back = self.plan([make_issue("motion_director", 1)], previous=["scene_001:scene_shorten"])
        self.assertEqual(back["actions"], [])
        self.assertTrue(any("осцилляция" in n for n in back["policy"]["notes"]))

    def test_human_approval(self):
        a = make_issue("motion_director", 1)
        b = make_issue(
            "narrative", 1, scene_ids=[], category="caption_speed", change={"op": "caption_words", "max_words": 2}
        )
        data = self.plan([a, b], mode="manual_approval")
        planner.approve(data, [a, b], decisions={a["issue_id"]: "rejected"}, approve_all=True)
        by_op = {x["op"]: x for x in data["actions"]}
        self.assertEqual((by_op["scene_extend"]["status"], by_op["scene_extend"]["approval"]), ("rejected", "denied"))
        self.assertEqual((by_op["caption_words"]["status"], by_op["caption_words"]["approval"]), ("approved", "given"))
        self.assertEqual((data["approval"], data["approved_by"]), ("given", "human"))
        self.assertEqual((a["status"], b["status"]), ("rejected", "accepted"))
        patch = planner.scene_patch(by_op["scene_extend"], scene("scene_001", 100), 30, 3000)
        self.assertEqual(patch, {"id": "scene_001", "edit_start_frame": 100, "duration_frames": 150})
        self.assertEqual(planner.scene_patch({"op": "scene_disable", "params": {}}, scene("scene_001", 100), 30, 3000),
                         {"id": "scene_001", "enabled": False})  # fmt: skip


class CompareDecideTest(unittest.TestCase):
    def test_decisions(self):
        ok = [{"id": "file", "ok": True, "detail": ""}]
        bad = [{"id": "file", "ok": True, "detail": ""}, {"id": "av_sync", "ok": False, "detail": "x"}]
        obj = {"baseline": {}, "candidate": {}, "worse": [], "better": []}
        judge = {"mode": "judge", "weighted_delta": 0.2}
        rules = policy.clean({})
        self.assertEqual(compare.decide(bad, obj, judge, fixed=["i"], regressed=[], policy=rules)[0], "rejected")
        self.assertEqual(
            compare.decide(ok, obj, judge, fixed=["i"], regressed=[], policy=rules, identical=True)[2],
            compare.IDENTICAL,
        )
        worse = {**obj, "worse": ["qa_errors"]}
        self.assertEqual(compare.decide(ok, worse, judge, fixed=["i"], regressed=[], policy=rules)[0], "rejected")
        self.assertEqual(compare.decide(ok, obj, judge, fixed=["i"], regressed=[], policy=rules)[0], "accepted")
        self.assertEqual(
            compare.decide(ok, obj, {"mode": "judge", "weighted_delta": -0.3}, fixed=["i"], regressed=[], policy=rules)[
                0
            ],
            "rejected",
        )
        self.assertEqual(
            compare.decide(ok, obj, {"mode": "judge", "weighted_delta": 0.0}, fixed=[], regressed=[], policy=rules)[0],
            "ambiguous",
        )
        human = {"mode": "human_review", "weighted_delta": None}
        self.assertEqual(compare.decide(ok, obj, human, fixed=["i"], regressed=[], policy=rules)[0], "ambiguous")
        self.assertEqual(
            compare.decide(ok, {**obj, "better": ["qa_warnings"]}, human, fixed=["i"], regressed=[], policy=rules)[0],
            "accepted",
        )
        self.assertEqual(compare.decide(ok, obj, human, fixed=[], regressed=["r"], policy=rules)[0], "rejected")
        subj = compare.subjective(rules, None, "prompt")
        self.assertEqual(subj["mode"], "human_review")
        data = {
            "items": [{"dimension": "clarity", "a": 3, "b": 5, "evidence": "e"}, {"dimension": "zzz", "a": 1, "b": 1}]
        }
        subj = compare.subjective(rules, lambda _p: (data, "fake", "v1"), "prompt")
        self.assertEqual(len(subj["items"]), 1)
        self.assertAlmostEqual(subj["weighted_delta"], 0.2 * 2 / 4, places=3)


if __name__ == "__main__":
    unittest.main()
