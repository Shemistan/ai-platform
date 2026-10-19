"""Итеративное ревью «Монтажа» (ITERATIVE_REVIEW_SPEC, PRD-028): политика, состояния, версии, API запусков."""

import json
import tempfile
import unittest
from pathlib import Path

from aiplatform.montage import schemas
from aiplatform.montage.review import policy, state, versions
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
        # Стадий раунда в этой сборке нет: запуск честно встаёт в blocked, а не выдумывает результат.
        status, started = self.json("POST", f"{base}/{pid}/review-runs/{rid}/start")
        self.assertEqual(status, 200, started)
        done = self.settle(pid, ("done", "failed"))
        self.assertEqual(done["status"], "done")
        got = self.poll(f"{base}/{pid}/review-runs/{rid}", lambda r: r["state"] in ("blocked", "completed"))
        self.assertEqual(got["state"], "blocked")
        self.assertTrue(got["blocking_reason"])
        self.assertEqual(self.json("POST", f"{base}/{pid}/review-runs/{rid}/stop")[0], 200)
        self.assertEqual(self.json("GET", f"{base}/{pid}/review-runs/{rid}")[1]["state"], "cancelled")
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


if __name__ == "__main__":
    unittest.main()
