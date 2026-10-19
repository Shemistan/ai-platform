"""Таймлайн, QA, экспорт и ревизии «Монтажа» по MASTER_SPEC (PRD-027)."""

import json
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from aiplatform.core import media
from aiplatform.montage import edl, exports, qa, render, revisions, schemas, timeline, timemap
from aiplatform.montage.service import MontageModule
from tests.fixtures import FAST, NEEDS_FFMPEG
from tests.helpers import PlatformCase


def w(text: str, s: float, e: float) -> dict:
    return {"w": text, "s": s, "e": e}


WORDS = [w("раз", 0.5, 0.8), w("два.", 0.9, 1.2), w("три", 3.2, 3.5), w("четыре", 3.6, 4.0), w("пять.", 4.1, 4.5)]


def scene(sid: str, start: int, frames: int, status: str = "rendered", enabled: bool = True) -> dict:
    return {"id": sid, "edit_start_frame": start, "duration_frames": frames, "status": status, "enabled": enabled,
            "hash": "abc", "fps": {"num": 30, "den": 1}}  # fmt: skip


class TimelineTest(unittest.TestCase):
    def setUp(self):
        result = edl.build(
            WORDS, 5.0, [{"from": 3, "to": 3, "kind": "slip", "reason": ""}], [{"from": 0, "layout": "split"}]
        )
        self.tm = timemap.TimeMap.from_edl(result, "head", source_duration=5.0)
        self.captions = render.chunks(edl.output_words(WORDS, result))

    def test_tracks_from_timemap_scenes_and_captions(self):
        scenes = [
            scene("scene_001", 0, 30),
            scene("scene_002", 40, 10, "blocked"),
            scene("scene_003", 10, 5, enabled=False),
        ]
        data = timeline.build(
            self.tm,
            scenes,
            self.captions,
            has_screen=True,
            inserts=[{"at": 1.0, "duration": 0.5, "path": "motion/01/insert.mov"}],
        )
        self.assertEqual(schemas.validate("timeline", data), [])
        tracks = {t["id"]: t["clips"] for t in data["tracks"]}
        self.assertEqual(set(tracks), {"presenter", "screen", "overlays", "captions", "dialogue"})
        self.assertEqual(len(tracks["presenter"]), 3)
        self.assertEqual(len(tracks["screen"]), 3, "план split — экран на каждом сегменте")
        self.assertEqual([c["id"] for c in tracks["overlays"]], ["scene_001", "scene_002", "insert_01"])
        self.assertEqual([c["enabled"] for c in tracks["overlays"]], [True, False, True])
        self.assertEqual(tracks["overlays"][0]["path"], "scenes/scene_001/abc.mov")
        self.assertEqual(tracks["dialogue"][0]["gain_db"], 0.0)
        self.assertTrue(tracks["captions"] and tracks["captions"][0]["text"])
        self.assertEqual(timeline.check(data), [])
        no_screen = timeline.build(self.tm, [], [], has_screen=False)
        self.assertNotIn("screen", {t["id"] for t in no_screen["tracks"]})
        self.assertEqual(timeline.summary(no_screen)["presenter"], 3)

    def test_check_and_overlays_need_files(self):
        data = timeline.build(self.tm, [scene("scene_001", 0, 10_000)], [], has_screen=False)
        self.assertTrue(any("за концом" in e for e in timeline.check(data)))
        data["tracks"][0]["clips"].append({**data["tracks"][0]["clips"][0], "id": "dup"})
        self.assertTrue(any("пересекается" in e for e in timeline.check(data)))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            good = timeline.build(self.tm, [scene("scene_001", 0, 30)], [], has_screen=False)
            self.assertEqual(timeline.overlays(good, root), [], "файла нет — наложения нет")
            (root / "scenes" / "scene_001").mkdir(parents=True)
            (root / "scenes" / "scene_001" / "abc.mov").write_bytes(b"x")
            found = timeline.overlays(good, root)
            self.assertEqual((found[0].start, found[0].duration), (0.0, 1.0))


class SubtitlesTest(unittest.TestCase):
    def test_vtt(self):
        text = render.vtt([{"text": "Привет", "s": 0, "e": 1.2}, {"text": "мир", "s": 61.5, "e": 62}])
        self.assertTrue(text.startswith("WEBVTT\n\n00:00:00.000 --> 00:00:01.200\nПривет\n"))
        self.assertIn("00:01:01.500 --> 00:01:02.000\nмир", text)


@NEEDS_FFMPEG
class QaTest(unittest.TestCase):
    def test_parse_analysis_and_report_gates(self):
        lines = [
            "[blackdetect @ 0x1] black_start:2 black_end:3.5 black_duration:1.5",
            "[silencedetect @ 0x2] silence_start: 5",
            "[silencedetect @ 0x2] silence_end: 6.4 | silence_duration: 1.4",
            "[Parsed_astats_1 @ 0x3] Peak level dB: -0.02",
            "[Parsed_astats_1 @ 0x3] Peak level dB: -3.1",
        ]
        found = qa.parse_analysis(lines, 10.0)
        self.assertEqual((found["black"], found["silence"], found["peak_db"]), ([(2.0, 3.5)], [(5.0, 6.4)], -0.02))
        items = [
            qa._item(1, "mechanical", "black_frames", "error", "x"),
            qa._item(2, "mechanical", "silence", "warning", "y"),
        ]
        report = qa.report(items, [], "human_review", 0)
        self.assertEqual(schemas.validate_many("qa_item", report["items"]), [])
        self.assertEqual(report["gates"], {"mechanical": "fail", "semantic": "human_review"})
        self.assertEqual(report["counts"], {"error": 1, "warning": 1, "info": 0})
        ok = qa.report([items[1]], [], "pass", 1)
        self.assertEqual(ok["gates"]["mechanical"], "pass")

    def test_semantic_from_rounds(self):
        project = {"options": {"check": True}}
        rounds = [{"round": 1, "issues": [{"from": 1, "to": 2, "kind": "slip", "reason": "оговорка"}], "tech": []},
                  {"round": 2, "issues": [], "tech": []}]  # fmt: skip
        items, mode = qa.semantic(project, rounds, llm_available=True, max_rounds=2)
        self.assertEqual((mode, items[0]["status"], items[0]["attempts"]), ("pass", "fixed", 1))
        issue = {"from": 3, "to": 3, "kind": "slip", "reason": "ещё"}
        stuck = [*rounds[:1], {"round": 2, "issues": [issue], "tech": []}, {"round": 3, "issues": [issue], "tech": []}]
        items, mode = qa.semantic(project, stuck, llm_available=True, max_rounds=2)
        self.assertEqual(mode, "fail")
        self.assertEqual(items[-1]["status"], "escalated")
        self.assertEqual(qa.semantic(project, rounds, llm_available=False, max_rounds=2), ([], "human_review"))
        self.assertEqual(
            qa.semantic({"options": {"check": False}}, [], llm_available=True, max_rounds=2)[1], "human_review"
        )

    def test_mechanical_on_black_and_loud_video(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            argv = ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=black:s=1920x1080:r=30:d=3",
                    "-f", "lavfi", "-i", "sine=frequency=330:duration=3", "-af", "volume=20dB", "-shortest",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-ar", "48000",
                    str(folder / "output.mp4")]  # fmt: skip
            subprocess.run(argv, check=True)
            from aiplatform.montage import config as montage_config

            cfg = mock.Mock(spec=montage_config.MontageConfig)
            cfg.ffmpeg, cfg.font = "ffmpeg", ""
            project = {"options": {"max_pause": 0.6, "subtitles": True}}
            manifest = {"fps": 30, "duration_frames": 90, "tracks": []}
            scenes = [scene("scene_001", 60, 90, "rendered"), scene("scene_002", 0, 10, "blocked")]
            import threading

            items = qa.mechanical(cfg, folder, project, manifest, scenes, [{"text": "x" * 50, "s": 0, "e": 1}], None,
                                  threading.Event())  # fmt: skip
            checks = {i["check"]: i for i in items}
            self.assertIn("black_frames", checks)
            self.assertEqual(checks["black_frames"]["severity"], "error")
            self.assertIn("clipping", checks)
            self.assertIn("scene_bounds", checks)
            self.assertIn("asset_missing", checks)
            self.assertIn("scene_blocked", checks)
            self.assertIn("caption_overflow", checks)
            self.assertIn("font_missing", checks)
            self.assertNotIn("duration", checks, "3 с = 90 кадров")
            self.assertEqual(schemas.validate_many("qa_item", items), [])


class ExportsAndRevisionsTest(unittest.TestCase):
    def test_argv_and_listing(self):
        copy = exports.argv("youtube_1080p", "ffmpeg", Path("o.mp4"), Path("e.mp4"))
        self.assertIn("copy", copy)
        vertical = exports.argv("vertical_1080x1920", "ffmpeg", Path("o.mp4"), Path("e.mp4"))
        self.assertIn("crop=ih*9/16:ih,scale=1080:1920,setsar=1", vertical)
        self.assertIn("prores_ks", exports.argv("prores_master", "ffmpeg", Path("o.mp4"), Path("e.mov")))
        with self.assertRaises(ValueError):
            exports.preset("dvd")
        for p in exports.PRESETS.values():
            self.assertEqual(schemas.validate("export_preset", p), [], p["id"])
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(exports.listing(Path(tmp), "/x"), [])
            self.assertEqual(exports.next_number(Path(tmp)), 1)

    def test_snapshot_restore_and_keep(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "project.json").write_text(json.dumps({"id": "a", "status": "review", "log": ["x"]}))
            (folder / "plan.json").write_text(json.dumps({"cuts": [1]}))
            first = revisions.snapshot(folder, "  plan   ok ")
            self.assertEqual(
                (first["n"], first["label"], first["files"]), (1, "plan ok", ["plan.json", "project.json"])
            )
            self.assertEqual(schemas.validate("revision", first), [])
            (folder / "plan.json").write_text(json.dumps({"cuts": []}))
            (folder / "storyboard.json").write_text(json.dumps({"scenes": []}))
            second = revisions.snapshot(folder, "story")
            self.assertNotEqual(first["project_hash"], second["project_hash"])
            self.assertEqual([r["n"] for r in revisions.listing(folder)], [1, 2])
            restored = revisions.restore(folder, 1)
            self.assertEqual(restored["status"], "review")
            self.assertNotIn("log", restored, "журнал в снимок не входит")
            self.assertEqual(json.loads((folder / "plan.json").read_text()), {"cuts": [1]})
            self.assertFalse((folder / "storyboard.json").exists(), "в снимке 1 раскадровки не было")
            self.assertIsNone(revisions.restore(folder, 9))
            with mock.patch.object(revisions, "KEEP", 3):
                for k in range(4):
                    revisions.snapshot(folder, f"n{k}")
            self.assertEqual([r["n"] for r in revisions.listing(folder)], [4, 5, 6])


def montage_only(ctx):
    return [MontageModule(ctx)]


@NEEDS_FFMPEG
class QaExportApiTest(PlatformCase):
    modules = staticmethod(montage_only)
    process_env = FAST

    @classmethod
    def setUpClass(cls):
        from tests.test_montage import make_video

        cls.media = tempfile.TemporaryDirectory()
        cls.head = Path(cls.media.name) / "head.mp4"
        make_video(cls.head, 40)

    @classmethod
    def tearDownClass(cls):
        cls.media.cleanup()

    def folder(self, pid) -> Path:
        return Path(self.tmp.name) / "montage" / "projects" / pid

    def settle(self, pid, statuses, timeout=240):
        return self.poll(f"/api/montage/projects/{pid}", lambda p: p["status"] in statuses, timeout=timeout)

    def rendered(self, **options) -> str:
        payload = {"title": "QA", "options": {"check": False, **options}}
        pid = self.json("POST", "/api/montage/projects", payload)[1]["id"]
        self.assertEqual(self.json("POST", f"/api/montage/projects/{pid}/sources/head", self.head.read_bytes())[0], 201)
        self.json("POST", f"/api/montage/projects/{pid}/start")
        return pid

    def test_timeline_qa_export_revisions_and_restart(self):
        pid = self.rendered(mode="reviewed")
        self.settle(pid, ("review", "failed"))
        self.json("POST", f"/api/montage/projects/{pid}/approve", {"gate": "edl"})
        self.assertEqual(self.json("POST", f"/api/montage/projects/{pid}/export")[0], 409, "ролика ещё нет")
        self.json("POST", f"/api/montage/projects/{pid}/render")
        done = self.settle(pid, ("done", "failed"))
        self.assertEqual(done["status"], "done", done)
        self.assertEqual([s["id"] for s in done["stages"]], ["segments", "join", "final", "qa"])
        self.assertEqual(done["qa"]["gates"], {"mechanical": "pass", "semantic": "human_review"})
        self.assertGreater(done["timeline"]["presenter"], 0)
        _, tl = self.json("GET", f"/api/montage/projects/{pid}/timeline")
        self.assertEqual(schemas.validate("timeline", tl), [])
        _, report = self.json("GET", f"/api/montage/projects/{pid}/qa")
        self.assertEqual(report["gates"]["mechanical"], "pass")
        folder = self.folder(pid)
        self.assertTrue((folder / "subtitles.vtt").is_file())
        status, _, vtt = self.call("GET", f"/api/montage/projects/{pid}/subtitles.vtt")
        self.assertEqual(status, 200)
        self.assertTrue(vtt.startswith(b"WEBVTT"))
        # Экспорт по умолчанию и вертикаль.
        self.assertEqual(self.json("POST", f"/api/montage/projects/{pid}/export", {"preset": "dvd"})[0], 400)
        status, body = self.json("POST", f"/api/montage/projects/{pid}/export", {"preset": "vertical_1080x1920"})
        self.assertEqual(status, 200, body)
        done = self.settle(pid, ("done", "failed"))
        self.assertEqual(done["status"], "done", done)
        self.assertEqual(done["exports"], 1)
        self.assertTrue(done["gates"]["export"]["approved"])
        _, listing = self.json("GET", f"/api/montage/projects/{pid}/exports")
        item = listing["exports"][0]
        self.assertEqual((item["n"], item["preset"], len(item["sha256"])), (1, "vertical_1080x1920", 64))
        status, headers, _ = self.call("GET", item["file_url"])
        self.assertEqual((status, headers["Content-Type"]), (200, "video/mp4"))
        export_path = folder / "exports" / "1" / "export.mp4"
        self.assertEqual(media.video_size(export_path), (1080, 1920))
        self.assertAlmostEqual(media.duration(export_path), done["output"]["duration"], delta=0.3)
        _, prov = self.json("GET", item["provenance_url"])
        self.assertEqual(prov["output"]["sha256"], item["sha256"])
        self.assertEqual(len(prov["sources"][0]["sha256"]), 64)
        self.assertIn(
            "rough_cut", " ".join(prov["llm"]["models"]) + " ".join(prov["llm"]["prompt_versions"]) + "rough_cut"
        )
        self.assertEqual(prov["qa"]["gates"]["mechanical"], "pass")
        status, _, archive = self.call("GET", item["archive_url"])
        self.assertEqual(status, 200)
        with zipfile.ZipFile(Path(self.tmp.name) / "a.zip", "w") as zf:  # пишем, чтобы прочитать байты через zipfile
            zf.writestr("x", "y")
        names = zipfile.ZipFile(folder / "exports" / "1" / "project.zip").namelist()
        self.assertTrue(
            {"project.json", "plan.json", "timemap.json", "qa_report.json", "subtitles.srt"} <= set(names), names
        )
        self.assertFalse(any(n.endswith(".mp4") for n in names), "медиа в архив не идёт")
        # Ревизии: снимки после действий, откат возвращает план.
        _, revs = self.json("GET", f"/api/montage/projects/{pid}/revisions")
        labels = [r["label"] for r in revs["revisions"]]
        self.assertTrue({"prepare", "render", "export"} <= set(labels), labels)
        _, plan = self.json("GET", f"/api/montage/projects/{pid}/plan")
        cuts_before = plan["cuts"]
        self.json("POST", f"/api/montage/projects/{pid}/plan", {"cuts": [], "layout": plan["layout"]})
        _, revs = self.json("GET", f"/api/montage/projects/{pid}/revisions")
        self.assertEqual(revs["revisions"][-1]["label"], "plan edited")
        target = next(r["n"] for r in revs["revisions"] if r["label"] == "render")
        self.assertEqual(self.json("POST", f"/api/montage/projects/{pid}/revisions/999/restore")[0], 409)
        status, restored = self.json("POST", f"/api/montage/projects/{pid}/revisions/{target}/restore")
        self.assertEqual(status, 200, restored)
        _, plan2 = self.json("GET", f"/api/montage/projects/{pid}/plan")
        self.assertEqual(plan2["cuts"], cuts_before)
        self.assertEqual(
            self.json("GET", f"/api/montage/projects/{pid}/revisions")[1]["revisions"][-1]["label"], f"restore {target}"
        )
        # Рестарт сервиса: проект, таймлайн и история на месте (§11).
        module = self.app.modules["montage"]
        from aiplatform.montage.projects import MontageProjects

        again = MontageProjects(module.config, module.ctx.gate)
        try:
            project = again.get(pid)
            self.assertEqual(project["status"], "done")
            self.assertEqual(project["timeline"], done["timeline"])
            self.assertEqual(len(again.revisions_list(pid)), len(revs["revisions"]) + 1)
            self.assertEqual(again.exports_list(pid)[0]["n"], 1)
        finally:
            again.close()

    def test_export_gate_override_and_autonomous_export(self):
        pid = self.rendered(mode="autonomous")
        done = self.settle(pid, ("done", "failed"))
        self.assertEqual(done["status"], "done", done)
        self.assertEqual(done["exports"], 1, "autonomous экспортирует сам, если QA прошёл")
        self.assertTrue(done["gates"]["export"]["auto"])
        self.assertIn("export", [s["id"] for s in done["stages"]])
        # Подделываем красные ворота: без причины — 409, с причиной — экспорт и запись в журнале.
        folder = self.folder(pid)
        report = json.loads((folder / "qa_report.json").read_text())
        report["gates"]["mechanical"] = "fail"
        (folder / "qa_report.json").write_text(json.dumps(report))
        status, err = self.json("POST", f"/api/montage/projects/{pid}/export", {"preset": "youtube_1080p"})
        self.assertEqual(status, 409, err)
        self.assertIn("override_reason", err["error"])
        payload = {"preset": "youtube_1080p", "override_reason": "чёрный кадр — это титр, так задумано"}
        self.assertEqual(self.json("POST", f"/api/montage/projects/{pid}/export", payload)[0], 200)
        done = self.settle(pid, ("done", "failed"))
        self.assertEqual(done["exports"], 2)
        _, log = self.json("GET", f"/api/montage/projects/{pid}/decisions")
        override = [d for d in log["decisions"] if d["action"] == "export:override"]
        self.assertEqual(override[-1]["rationale"], payload["override_reason"])
        _, listing = self.json("GET", f"/api/montage/projects/{pid}/exports")
        self.assertEqual(listing["exports"][1]["override_reason"], payload["override_reason"])


if __name__ == "__main__":
    unittest.main()
