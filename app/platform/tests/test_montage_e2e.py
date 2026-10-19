"""Сквозная приёмка MASTER_SPEC §11 (PRD-027): полный путь на фейках и путь без моделей.

1. Проект → запись → ASR → утверждение EDL → раскадровка (≥3 разных шаблона) → утверждение →
   сборка → QA → проигрываемый mp4 (ffprobe, длительность в кадр от таймлайна) → экспорт 16:9 и 9:16 →
   рестарт сервиса: проект, таймлайн, ревизии и экспорт на месте; повторная сборка ничего не
   перерисовывает.
2. Настоящий бэкенд без ASR, LLM и HyperFrames: честные unavailable/blocked, запуск отказывает
   с подсказкой, поддельных артефактов нет.
3. Субтитры RU/EN с длинными терминами: VTT/SRT целы, переполнение строки ловит QA.
"""

import json
import tempfile
import unittest
from pathlib import Path

from aiplatform.core import media
from aiplatform.montage import qa, render, schemas, timemap
from aiplatform.montage.projects import MontageProjects
from aiplatform.montage.service import MontageModule
from tests.fixtures import FAST, NEEDS_FFMPEG
from tests.helpers import PlatformCase


def montage_only(ctx):
    return [MontageModule(ctx)]


@NEEDS_FFMPEG
class EndToEndTest(PlatformCase):
    modules = staticmethod(montage_only)
    process_env = FAST

    @classmethod
    def setUpClass(cls):
        from tests.test_montage import make_video

        cls.media = tempfile.TemporaryDirectory()
        cls.head = Path(cls.media.name) / "head.mp4"
        make_video(cls.head, 150)  # 2,5 минуты — нижняя граница фикстуры из §11

    @classmethod
    def tearDownClass(cls):
        cls.media.cleanup()

    def folder(self, pid) -> Path:
        return Path(self.tmp.name) / "montage" / "projects" / pid

    def settle(self, pid, statuses, timeout=300):
        return self.poll(f"/api/montage/projects/{pid}", lambda p: p["status"] in statuses, timeout=timeout)

    def test_full_path_restart_and_no_rerender(self):
        base = "/api/montage/projects"
        status, project = self.json("POST", base, {"title": "Приёмка", "options": {"mode": "reviewed", "check": False}})
        self.assertEqual(status, 201, project)
        pid = project["id"]
        self.assertEqual(schemas.validate("project", json.loads((self.folder(pid) / "project.json").read_text())), [])
        self.assertEqual(self.json("POST", f"{base}/{pid}/sources/head", self.head.read_bytes())[0], 201)
        self.json("POST", f"{base}/{pid}/start")
        self.assertEqual(self.settle(pid, ("review", "failed"))["status"], "review")
        # Манифесты стадий — по схемам (§11: 100% schema-valid).
        folder = self.folder(pid)
        words = json.loads((folder / "words.json").read_text())["words"]
        self.assertEqual(schemas.validate_many("transcript_word", words), [])
        self.assertEqual(
            schemas.validate_many("edit_decision", json.loads((folder / "edl.json").read_text())["decisions"]), []
        )
        tm = timemap.TimeMap.load(json.loads((folder / "timemap.json").read_text()))
        self.assertEqual(tm.check(), [])
        self.json("POST", f"{base}/{pid}/approve", {"gate": "edl"})
        self.json("POST", f"{base}/{pid}/storyboard/run")
        self.assertEqual(self.settle(pid, ("waiting_approval", "failed"))["status"], "waiting_approval")
        _, view = self.json("GET", f"{base}/{pid}/storyboard")
        checks = (("knowledge_claim", view["knowledge"]["claims"]), ("narrative_beat", view["beats"]["beats"]),
                  ("storyboard_scene", view["scenes"]))  # fmt: skip
        for name, items in checks:
            self.assertEqual(schemas.validate_many(name, items), [], name)
        self.assertEqual(schemas.validate("style_bible", view["style"]), [])
        self.assertGreaterEqual(len({s["template_id"] for s in view["scenes"]}), 3, view["summary"])
        self.json("POST", f"{base}/{pid}/approve", {"gate": "storyboard"})
        self.json("POST", f"{base}/{pid}/render")
        done = self.settle(pid, ("done", "failed"))
        self.assertEqual(done["status"], "done", done)
        self.assertEqual(done["qa"]["gates"]["mechanical"], "pass", done["qa"])
        self.assertGreaterEqual(done["motion"]["by_status"].get("rendered", 0), 3, done["motion"])
        # Ролик проигрывается: ffprobe видит видео 1920×1080 и звук, длительность — в кадр от таймлайна
        # (плюс документированный хвост loudnorm, docs/TIMEBASE.md).
        info = qa.probe_streams(folder / "output.mp4", "ffmpeg")
        timeline = json.loads((folder / "timeline.json").read_text())
        self.assertEqual(schemas.validate("timeline", timeline), [])
        self.assertTrue(info["video"] and info["audio"])
        self.assertEqual((info["width"], info["height"], info["fps"]), (render.W, render.H, 30.0))
        expected = timeline["duration_frames"] / 30
        self.assertGreaterEqual(info["duration"], expected - 1 / 30)
        self.assertLessEqual(info["duration"], expected + 1 / 30 + qa.LOUDNORM_TAIL_S)
        self.assertEqual(
            schemas.validate_many("qa_item", json.loads((folder / "qa_report.json").read_text())["items"]), []
        )
        # Экспорт 16:9 и 9:16.
        for preset in ("youtube_1080p", "vertical_1080x1920"):
            self.assertEqual(self.json("POST", f"{base}/{pid}/export", {"preset": preset})[0], 200)
            self.assertEqual(self.settle(pid, ("done", "failed"))["status"], "done")
        _, listing = self.json("GET", f"{base}/{pid}/exports")
        self.assertEqual([e["preset"] for e in listing["exports"]], ["youtube_1080p", "vertical_1080x1920"])
        self.assertEqual(media.video_size(folder / "exports" / "2" / "export.mp4"), (1080, 1920))
        self.assertEqual(
            schemas.validate(
                "export_preset", json.loads((folder / "exports" / "1" / "provenance.json").read_text())["preset"]
            ),
            [],
        )
        # Повторная сборка: сцены и сегменты из кэша (хэши), файлы не трогаются.
        movs = {p: p.stat().st_mtime_ns for p in folder.glob("scenes/*/*.mov")}
        segs = {p: p.stat().st_mtime_ns for p in folder.glob("segments/*.mp4")}
        self.json("POST", f"{base}/{pid}/render")
        self.assertEqual(self.settle(pid, ("done", "failed"))["status"], "done")
        self.assertEqual({p: p.stat().st_mtime_ns for p in folder.glob("scenes/*/*.mov")}, movs)
        self.assertEqual({p: p.stat().st_mtime_ns for p in folder.glob("segments/*.mp4")}, segs)
        # Рестарт: новый экземпляр на том же каталоге — проект, таймлайн, ревизии, экспорт целы (§11).
        _, revs = self.json("GET", f"{base}/{pid}/revisions")
        module = self.app.modules["montage"]
        again = MontageProjects(module.config, module.ctx.gate)
        try:
            project = again.get(pid)
            self.assertEqual(project["status"], "done")
            self.assertEqual(project["timeline"], done["timeline"])
            self.assertEqual(again.timeline_view(pid)["duration_frames"], timeline["duration_frames"])
            self.assertEqual(len(again.revisions_list(pid)), len(revs["revisions"]))
            self.assertEqual(len(again.exports_list(pid)), 2)
            self.assertGreaterEqual(len(again.story_view(pid)["scenes"]), 3)
        finally:
            again.close()

    def test_captions_ru_en_long_terms(self):
        text = ("Планировщик Go использует goroutine и channel. Kubernetes HorizontalPodAutoscaler масштабирует "
                "deployments по CPU. Это supercalifragilisticexpialidocious-технология.")  # fmt: skip
        words = [{"w": w, "s": i * 0.3, "e": i * 0.3 + 0.25} for i, w in enumerate(text.split())]
        items = render.chunks(words)
        srt, vtt = render.srt(items), render.vtt(items)
        self.assertIn("goroutine", srt)
        self.assertTrue(vtt.startswith("WEBVTT"))
        self.assertIn("HorizontalPodAutoscaler", vtt)
        self.assertTrue(all(c["e"] > c["s"] for c in items))
        long = [c for c in items if len(c["text"]) > 42]
        self.assertTrue(long, "длинные термины дают строку длиннее лимита — её ловит QA")
        self.assertIn("supercalifragilisticexpialidocious-технология.", [c["text"] for c in items][-1])


@NEEDS_FFMPEG
class NoModelsTest(PlatformCase):
    """Настоящий бэкенд без единой модели: честные статусы, никаких подделок (§3, §11)."""

    modules = staticmethod(montage_only)
    extra_env = {"AIP_BACKEND": "", "AIP_LLM_CMD": "/nonexistent/llm", "AIP_MONTAGE_HYPERFRAMES_CMD": "/nonexistent/hf",
                 "AIP_MONTAGE_ASR_PYTHON": "/nonexistent/python"}  # fmt: skip

    def test_everything_is_unavailable_and_nothing_is_faked(self):
        from tests.test_montage import make_video

        _, caps = self.json("GET", "/api/montage/capabilities")
        by_id = {i["id"]: i for i in caps["items"]}
        self.assertEqual(by_id["llm"]["status"], "unavailable")
        self.assertEqual(by_id["hyperframes"]["status"], "unavailable")
        self.assertTrue(all(i["status"] == "unavailable" for i in caps["items"] if i["kind"] == "asr"))
        self.assertEqual(
            {caps["routes"][t]["engine"] for t in ("transcribe", "plan", "story", "scene_render")}, {"blocked"}
        )
        self.assertTrue(all(caps["routes"][t]["reason"] for t in ("transcribe", "plan", "story", "scene_render")))
        _, cfg = self.json("GET", "/api/montage/config")
        self.assertFalse(cfg["llm_available"] or cfg["hyperframes_available"])
        status, project = self.json("POST", "/api/montage/projects", {"title": "Без моделей"})
        self.assertEqual(status, 201)
        pid = project["id"]
        with tempfile.TemporaryDirectory() as tmp:
            head = Path(tmp) / "head.mp4"
            make_video(head, 3)
            self.assertEqual(self.json("POST", f"/api/montage/projects/{pid}/sources/head", head.read_bytes())[0], 201)
        status, err = self.json("POST", f"/api/montage/projects/{pid}/start")
        self.assertEqual(status, 409, err)
        self.assertIn("make models", err["error"])
        folder = Path(self.tmp.name) / "montage" / "projects" / pid
        self.assertEqual(
            sorted(p.name for p in folder.iterdir()), ["project.json", "sources"], "ничего не сгенерировано"
        )
        problems = self.app.modules["montage"].problems()
        self.assertTrue(any("модели расшифровки" in p for p in problems), problems)


if __name__ == "__main__":
    unittest.main()
