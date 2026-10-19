"""Основание MASTER_SPEC в «Монтаже» (PRD-027): схемы, TimeMap, журнал решений, реестр возможностей,
режимы и ворота через API на фейках."""

import json
import tempfile
import unittest
from pathlib import Path

from aiplatform import config as config_module
from aiplatform.montage import capabilities, decisions, edl, schemas, timemap
from aiplatform.montage import config as montage_config
from aiplatform.montage.projects import MontageProjects
from aiplatform.montage.service import MontageModule
from tests.fixtures import FAST, NEEDS_FFMPEG
from tests.helpers import PlatformCase, isolated_config


def w(text: str, s: float, e: float) -> dict:
    return {"w": text, "s": s, "e": e}


WORDS = [w("раз", 0.5, 0.8), w("два.", 0.9, 1.2), w("три", 3.2, 3.5), w("четыре", 3.6, 4.0), w("пять.", 4.1, 4.5)]


class SchemasTest(unittest.TestCase):
    def test_every_schema_has_a_valid_example(self):
        names = schemas.names()
        self.assertGreaterEqual(len(names), 16, names)
        for name in names:
            self.assertEqual(schemas.validate(name, schemas.example(name)), [], name)

    def test_errors_are_specific(self):
        scene = schemas.example("storyboard_scene")
        del scene["beat_id"]
        scene["status"] = "весёлый"
        scene["necessity"]["clarity"] = 9
        found = schemas.validate("storyboard_scene", scene)
        self.assertTrue(any("нет ключа 'beat_id'" in e for e in found), found)
        self.assertTrue(any("$.status" in e for e in found), found)
        self.assertTrue(any("$.necessity.clarity" in e and "больше 5" in e for e in found), found)
        self.assertEqual(schemas.validate("transcript_word", {"w": "", "s": 1, "e": 0.5}), ["$.w: короче 1"])
        with self.assertRaises(schemas.SchemaError):
            schemas.check("qa_item", [{"id": "x"}], many=True)
        self.assertEqual(schemas.validate("time_map_segment", 5)[0], "$: ждали object, пришло int")


class TimeMapTest(unittest.TestCase):
    def build(self, cuts=()):
        result = edl.build(WORDS, 5.0, list(cuts), [{"from": 0, "layout": "head"}])
        return result, timemap.TimeMap.from_edl(result, "head", source_duration=5.0)

    def test_frames_are_integer_and_contiguous(self):
        result, tm = self.build([{"from": 3, "to": 3, "kind": "slip", "reason": ""}])
        self.assertEqual(len(tm.segments), 3)
        self.assertEqual(tm.check(), [])
        self.assertEqual(tm.segments[0].edit_frame_start, 0)
        for a, b in zip(tm.segments, tm.segments[1:], strict=False):
            self.assertEqual(a.edit_frame_end, b.edit_frame_start)
        self.assertEqual(tm.duration_frames, sum(s.frames for s in tm.segments))
        self.assertAlmostEqual(tm.duration_s, result.duration, delta=3 / 30)
        # Столько же кадров, сколько вырежет рендер: границы на сетке кадров по отдельности (render.frame).
        for seg in tm.segments:
            self.assertEqual(seg.frames, round(seg.source_pts_end * 30) - round(seg.source_pts_start * 30))
        self.assertEqual(schemas.validate_many("time_map_segment", [s.as_dict() for s in tm.segments]), [])

    def test_round_trip_and_cut_words(self):
        _, tm = self.build([{"from": 1, "to": 2, "kind": "retake", "reason": ""}])
        frame = tm.to_edit_frame(3.7)  # «четыре»
        self.assertIsNotNone(frame)
        source = tm.to_source(frame)
        self.assertEqual(source[0], "head")
        self.assertAlmostEqual(source[1], 3.7, delta=1.5 / 30)
        self.assertIsNone(tm.to_edit_frame(1.0), "«два» вырезано")
        self.assertIsNone(tm.to_source(tm.duration_frames + 5))
        words = tm.words_in_edit(WORDS)
        self.assertEqual([x["src"] for x in words], [0, 3, 4])
        self.assertTrue(all(a["end_frame"] <= b["start_frame"] for a, b in zip(words, words[1:], strict=False)))
        self.assertTrue(all(x["start_frame"] <= x["end_frame"] for x in words))
        again = timemap.TimeMap.load(json.loads(json.dumps(tm.as_dict())))
        self.assertEqual([s.as_dict() for s in again.segments], [s.as_dict() for s in tm.segments])

    def test_check_finds_violations(self):
        tm = timemap.TimeMap(source_duration=5.0, segments=[
            timemap.Segment("head", 2.0, 1.0, 0, 30),
            timemap.Segment("head", 0.5, 1.5, 31, 61),
            timemap.Segment("head", 4.0, 6.0, 61, 121),
        ])  # fmt: skip
        found = tm.check()
        self.assertTrue(any("отрицательный интервал исходника" in e for e in found), found)
        self.assertTrue(any("кадры не подряд" in e for e in found), found)
        self.assertTrue(any("за границами исходника" in e for e in found), found)

    def test_edit_decisions_from_cuts(self):
        cuts = [{"from": 1, "to": 2, "kind": "retake", "reason": "дубль"}]
        items = edl.decisions_of(WORDS, cuts, [{"from": 0, "layout": "head"}, {"from": 3, "layout": "split"}])
        self.assertEqual(schemas.validate_many("edit_decision", items), [])
        self.assertEqual([i["action"] for i in items], ["remove", "layout", "layout"])
        self.assertEqual((items[0]["source_in"], items[0]["source_out"], items[0]["words"]), (0.9, 3.5, [1, 2]))


class DecisionsTest(unittest.TestCase):
    def test_record_and_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            prompt = "### task: montage_plan\nП"
            first = decisions.record(folder, "rough_cut", "plan", model="fake", prompt=prompt, inputs={"a": 1},
                                     output={"cuts": []}, confidence=0.8, rationale="  x  y ")  # fmt: skip
            decisions.record(folder, "gate", "approve:edl", auto=True)
            items = decisions.read(folder)
            self.assertEqual([i["action"] for i in items], ["plan", "approve:edl"])
            self.assertEqual(items[0]["rationale"], "x y")
            self.assertEqual(len(first["prompt_version"]), 12)
            self.assertEqual(first["prompt_version"], decisions.sha(prompt.encode("utf-8"))[:12])
            self.assertEqual(items[0]["input_hash"], decisions.sha({"a": 1})[:16])
            self.assertTrue(items[1]["auto"])
            self.assertEqual(decisions.read(folder, limit=1)[0]["action"], "approve:edl")


def montage_only(ctx):
    return [MontageModule(ctx)]


class CapabilitiesTest(unittest.TestCase):
    def load(self, env):
        return montage_config.load(config_module.load(isolated_config(env)))

    def test_fake_backend_is_all_available_and_routes(self):
        cfg = self.load({"AIP_BACKEND": "fake"})
        items = {i["id"]: i for i in capabilities.registry(cfg)}
        self.assertEqual(schemas.validate_many("capability", list(items.values())), [])
        self.assertEqual(items["llm"]["status"], "available")
        self.assertEqual(items["hyperframes"]["status"], "available")
        self.assertEqual(items["docx"]["status"], "available")
        self.assertEqual(capabilities.route(cfg, "scene_render")[0], "hyperframes")
        self.assertEqual(capabilities.route(cfg, "scene_3d")[0], "blocked")
        self.assertEqual(capabilities.route(cfg, "document_docx"), ("docx", ""))

    def test_missing_tools_are_unavailable_not_faked(self):
        env = {"AIP_BACKEND": "", "AIP_LLM_CMD": "/nonexistent/llm", "AIP_MONTAGE_HYPERFRAMES_CMD": "/nonexistent/hf"}
        cfg = self.load(env)
        items = {i["id"]: i for i in capabilities.registry(cfg)}
        self.assertEqual(items["llm"]["status"], "unavailable")
        self.assertIn("AIP_LLM_CMD", items["llm"]["hint"])
        self.assertEqual(items["hyperframes"]["status"], "unavailable")
        engine, reason = capabilities.route(cfg, "scene_render", list(items.values()))
        self.assertEqual(engine, "blocked")
        self.assertIn("make models M=hyperframes", reason)
        self.assertEqual(capabilities.route(cfg, "plan", list(items.values()))[0], "blocked")
        self.assertEqual(capabilities.route(cfg, "transcribe", list(items.values()))[0], "blocked")

    @NEEDS_FFMPEG
    def test_probe_runs_real_commands(self):
        cfg = self.load({"AIP_BACKEND": "fake"})
        items = {i["id"]: i for i in capabilities.probe(cfg)}
        self.assertIsNotNone(items["ffmpeg"]["probed_at"])
        self.assertTrue(items["ffmpeg"]["version"].startswith("ffmpeg version"), items["ffmpeg"])
        self.assertEqual(items["ffmpeg"]["status"], "available")


@NEEDS_FFMPEG
class ModesApiTest(PlatformCase):
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

    def project(self, **options) -> str:
        status, project = self.json("POST", "/api/montage/projects", {"title": "Режимы", "options": options})
        self.assertEqual(status, 201, project)
        pid = project["id"]
        status, body = self.json("POST", f"/api/montage/projects/{pid}/sources/head", self.head.read_bytes())
        self.assertEqual(status, 201, body)
        return pid

    def settle(self, pid, statuses=("review", "done", "failed", "canceled"), timeout=120):
        return self.poll(f"/api/montage/projects/{pid}", lambda p: p["status"] in statuses, timeout=timeout)

    def folder(self, pid) -> Path:
        return Path(self.tmp.name) / "montage" / "projects" / pid

    def test_config_lists_modes_gates_and_capabilities(self):
        _, cfg = self.json("GET", "/api/montage/config")
        self.assertEqual([m["id"] for m in cfg["modes"]], ["reviewed", "autonomous", "manual"])
        self.assertEqual(cfg["gates"], ["edl", "storyboard", "export"])
        status, caps = self.json("GET", "/api/montage/capabilities")
        self.assertEqual(status, 200)
        self.assertEqual(caps["routes"]["scene_render"]["engine"], "hyperframes")
        status, probed = self.json("POST", "/api/montage/capabilities/probe")
        self.assertEqual(status, 200)
        self.assertTrue(all(i["probed_at"] for i in probed["items"]))

    def test_reviewed_mode_needs_approval_before_render(self):
        pid = self.project(mode="reviewed", check=False)
        self.json("POST", f"/api/montage/projects/{pid}/start")
        ready = self.settle(pid)
        self.assertEqual(ready["status"], "review", ready)
        self.assertEqual(ready["gates"]["edl"], {"required": True, "approved": False, "auto": False, "at": None})
        status, err = self.json("POST", f"/api/montage/projects/{pid}/render")
        self.assertEqual(status, 409, err)
        self.assertIn("не подтверждён", err["error"])
        folder = self.folder(pid)
        for name in ("edl.json", "timemap.json", "source_manifest.json"):
            self.assertTrue((folder / name).is_file(), name)
        manifest = json.loads((folder / "source_manifest.json").read_text())
        self.assertEqual(len(manifest["sources"][0]["sha256"]), 64)
        status, tm = self.json("GET", f"/api/montage/projects/{pid}/timemap")
        self.assertEqual(status, 200)
        self.assertGreater(tm["duration_frames"], 0)
        self.assertEqual(self.json("POST", f"/api/montage/projects/{pid}/approve", {"gate": "export"})[0], 409)
        self.assertEqual(self.json("POST", f"/api/montage/projects/{pid}/approve", {"gate": "x"})[0], 400)
        status, approved = self.json("POST", f"/api/montage/projects/{pid}/approve", {"gate": "edl"})
        self.assertEqual(status, 200, approved)
        self.assertTrue(approved["gates"]["edl"]["approved"])
        self.assertFalse(approved["gates"]["edl"]["auto"])
        # Правка плана снимает подтверждение: это новое решение.
        _, view = self.json("GET", f"/api/montage/projects/{pid}/plan")
        edited = {"cuts": view["cuts"][1:], "layout": view["layout"]}
        _, saved = self.json("POST", f"/api/montage/projects/{pid}/plan", edited)
        self.assertFalse(saved["gates"]["edl"]["approved"])
        self.json("POST", f"/api/montage/projects/{pid}/approve", {"gate": "edl"})
        self.assertEqual(self.json("POST", f"/api/montage/projects/{pid}/render")[0], 200)
        done = self.settle(pid, ("done", "failed"))
        self.assertEqual(done["status"], "done", done)
        _, log = self.json("GET", f"/api/montage/projects/{pid}/decisions")
        actions = [d["action"] for d in log["decisions"]]
        self.assertIn("rough_cut:llm", actions)
        self.assertEqual(actions.count("approve:edl"), 2)
        self.assertIn("plan:edit", actions)
        first = next(d for d in log["decisions"] if d["action"] == "rough_cut:llm")
        self.assertEqual(len(first["prompt_version"]), 12)
        self.assertTrue(first["output_hash"])

    def test_autonomous_mode_passes_gates_itself(self):
        pid = self.project(mode="autonomous", check=False)
        self.json("POST", f"/api/montage/projects/{pid}/start")
        done = self.settle(pid, ("done", "failed"))
        self.assertEqual(done["status"], "done", done)
        gate_view = done["gates"]["edl"]
        self.assertEqual((gate_view["required"], gate_view["approved"], gate_view["auto"]), (False, True, True))
        self.assertTrue(done["options"]["auto"], "старый флаг для старых клиентов")
        log = decisions.read(self.folder(pid))
        gate = next(d for d in log if d["action"] == "approve:edl")
        self.assertTrue(gate["auto"])
        self.assertEqual(gate["rationale"], "autonomous mode")

    def test_manual_mode_skips_llm_plan(self):
        pid = self.project(mode="manual")
        self.json("POST", f"/api/montage/projects/{pid}/start")
        ready = self.settle(pid)
        self.assertEqual(ready["status"], "review", ready)
        self.assertEqual([s["id"] for s in ready["stages"]], ["audio", "silences", "transcribe"])
        _, view = self.json("GET", f"/api/montage/projects/{pid}/plan")
        self.assertEqual(view["cuts"], [])
        self.assertIn("Вручную", view["notes"])
        self.assertEqual(self.json("POST", f"/api/montage/projects/{pid}/replan")[0], 409)
        self.assertNotIn("rough_cut:llm", [d["action"] for d in decisions.read(self.folder(pid))])

    def test_old_project_file_is_migrated(self):
        pid = self.project()
        path = self.folder(pid) / "project.json"
        data = json.loads(path.read_text())
        data["schema"] = 1
        data["options"].pop("mode")
        data["options"]["auto"] = True
        data.pop("approvals")
        data["status"] = "running"
        path.write_text(json.dumps(data))
        module = self.app.modules["montage"]
        reloaded = MontageProjects(module.config, module.ctx.gate)
        project = reloaded.get(pid)
        reloaded.close()
        self.assertEqual(project["mode"], "autonomous")
        self.assertEqual(project["status"], "interrupted")
        self.assertIn("перезапущен", project["error"])
        self.assertEqual(project["gates"]["storyboard"]["approved"], False)
        self.assertEqual(json.loads(path.read_text())["schema"], 2)


if __name__ == "__main__":
    unittest.main()
