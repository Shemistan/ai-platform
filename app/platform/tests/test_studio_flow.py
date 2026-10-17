"""Сценарии целиком через HTTP: фейковые фото, TTS, LLM и новости, настоящий ffmpeg."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from aiplatform.core import media
from aiplatform.fakes import fake_gen
from tests.helpers import PlatformCase

ARTICLE = """Как устроен сборщик мусора в Go

Сборщик мусора в Go работает параллельно с программой. Он помечает живые объекты и освобождает остальные.

В новой версии паузы стали короче. Это важно для сервисов с жёсткими требованиями к задержкам.

Итог: обновляйтесь и измеряйте профили. Подписывайтесь на канал."""

FAST = {
    "FAKE_GEN_STEP_DELAY": "0.005",
    "FAKE_TTS_STEP_DELAY": "0.01",
    "FAKE_TTS_WORDS_PER_SECOND": "10",
    "FAKE_NEWS_DELAY": "0",
    "FAKE_LLM_FAIL": "",
    "FAKE_LLM_GARBAGE": "",
    "FAKE_NEWS_EMPTY": "",
}
SOURCE = {"source_url": "https://example.dev/gc", "source_author": "Иван Петров"}
NEEDS_FFMPEG = unittest.skipUnless(shutil.which("ffmpeg"), "нужен ffmpeg")


def png_bytes() -> bytes:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "p.png"
        fake_gen.write_png(str(path), 64, 36, 5)
        return path.read_bytes()


class StudioCase(PlatformCase):
    process_env = FAST
    extra_env = {"AIP_RENDER_SCALE": "0.25"}

    def create(self, **payload):
        if payload.get("scenario") == "article":  # без источника статья не публикуется (CMP-005)
            payload["settings"] = SOURCE | (payload.get("settings") or {})
        status, project = self.json("POST", "/api/studio/projects", payload)
        self.assertEqual(status, 201, project)
        return project

    def settle(self, pid, timeout=90):
        return self.poll(f"/api/studio/projects/{pid}", lambda p: not p["running"], timeout=timeout, interval=0.1)

    @staticmethod
    def statuses(project):
        return {s["id"]: s["status"] for s in project["steps"]}

    def folder(self, pid) -> Path:
        return Path(self.tmp.name) / "studio" / "projects" / pid


@NEEDS_FFMPEG
class ArticleFlowTest(StudioCase):
    def test_article_to_videos_and_manual_publish(self):
        settings = {"shorts": True, "channels": ["download-16x9", "download-9x16"]}
        project = self.create(scenario="article", text=ARTICLE, settings=settings, run=True)
        self.assertEqual(project["title"], "Как устроен сборщик мусора в Go")
        done = self.settle(project["id"])
        steps = self.statuses(done)
        self.assertEqual(steps["publish"], "waiting", done["log"])
        self.assertEqual(steps["clips"], "skipped", "видео для слайдов по умолчанию выключены")
        self.assertTrue(all(v == "done" for k, v in steps.items() if k not in ("publish", "clips")), steps)

        folder = self.folder(project["id"])
        voice_s = media.duration(folder / "voice.mp3")
        self.assertEqual(media.video_size(folder / "video.mp4"), (480, 270))
        self.assertAlmostEqual(media.duration(folder / "video.mp4"), voice_s, delta=0.6)
        self.assertEqual(media.video_size(folder / "short.mp4"), (270, 480))
        plan = done["artifacts"]["plan"]
        # Слайд на каждую фразу, картинки вперемешку с динамическими сценами; рисуются только картинки.
        self.assertEqual(len(plan["slides"]), 7)
        drawn = [f"{n:02d}" for n, s in enumerate(plan["slides"], start=1) if s["kind"] == "image"]
        self.assertEqual(drawn, ["01", "03", "05", "07"])
        self.assertEqual({s["kind"] for s in plan["slides"]}, {"image", "bullets", "stat", "chart"})
        self.assertEqual(sorted(done["files"]["images"]), drawn)
        self.assertTrue(done["artifacts"]["poster_prompt"].startswith("cinematic"))
        self.assertIn("Главное", done["artifacts"]["post"])
        self.assertEqual(len(done["artifacts"]["short"]["beats"]), 4)
        self.assertEqual(self.call("GET", done["files"]["video"])[0], 200)
        # Задачи сценария видны в истории вкладок.
        _, photos = self.json("GET", "/api/photo/jobs")
        self.assertEqual(len(photos["jobs"]), 5)  # постер + 4 слайда
        _, audios = self.json("GET", "/api/audio/jobs")
        self.assertEqual(len(audios["jobs"]), 2)  # видео + шортс

        status, after = self.json("POST", f"/api/studio/projects/{project['id']}/approve", {"step": "publish"})
        self.assertEqual(status, 200, after)
        published = self.settle(project["id"])
        self.assertEqual(self.statuses(published)["publish"], "done", published["log"])
        self.assertEqual([r["channel"] for r in published["published"]], ["download-16x9", "download-9x16"])
        status, _, body = self.call(
            "GET", f"/api/studio/projects/{project['id']}/files/publish/download-9x16/video.mp4"
        )
        self.assertEqual((status, body), (200, (folder / "short.mp4").read_bytes()))

        # Повторный запуск готового проекта ничего не переделывает.
        self.json("POST", f"/api/studio/projects/{project['id']}/run", {})
        again = self.settle(project["id"])
        self.assertEqual(self.json("GET", "/api/photo/jobs")[1]["jobs"].__len__(), 5)
        self.assertEqual(self.statuses(again), self.statuses(published))

        # Правка текста сбрасывает всё, что от него зависит.
        status, edited = self.json(
            "POST", f"/api/studio/projects/{project['id']}/artifacts/text", {"text": ARTICLE + "\n\nЕщё абзац."}
        )
        self.assertEqual(status, 200, edited)
        steps = self.statuses(edited)
        self.assertEqual(steps["text"], "done")
        self.assertTrue(all(v == "pending" for k, v in steps.items() if k != "text"), steps)

    def test_poster_upload_does_not_block_other_steps(self):
        project = self.create(scenario="article", text=ARTICLE, settings={"poster_mode": "upload"}, run=True)
        waiting = self.settle(project["id"])
        steps = self.statuses(waiting)
        # Постер ждёт человека, но всё, что от него не зависит, сделано.
        self.assertEqual(steps["poster"], "waiting")
        self.assertEqual((steps["plan"], steps["images"], steps["voice"]), ("done", "done", "done"), waiting["log"])
        render = next(s for s in waiting["steps"] if s["id"] == "render")
        self.assertEqual((render["status"], render["blocked_by"]), ("pending", ["Постер — первый кадр"]))
        poster_step = next(s for s in waiting["steps"] if s["id"] == "poster")
        self.assertIn("загрузи", poster_step["note"])

        status, body = self.json("POST", f"/api/studio/projects/{project['id']}/upload/poster", b"not an image")
        self.assertEqual(status, 400, body)
        status, _, after = self.call("POST", f"/api/studio/projects/{project['id']}/upload/poster", png_bytes())
        self.assertEqual(status, 200, after)
        self.assertEqual(self.statuses(json.loads(after))["poster"], "done")

        # Без «Запустить всё»: загрузка последнего недостающего сама продолжает выпуск.
        done = self.settle(project["id"])
        self.assertEqual(self.statuses(done)["render"], "done", done["log"])
        self.assertTrue(self.folder(project["id"]).joinpath("poster.png").is_file())

    def test_voice_upload_and_no_images(self):
        settings = {"voice_mode": "upload", "draw_images": False}
        project = self.create(scenario="article", text=ARTICLE, settings=settings, run=True)
        waiting = self.settle(project["id"])
        self.assertEqual(self.statuses(waiting)["voice"], "waiting")
        self.assertEqual(self.statuses(waiting)["images"], "skipped")
        mp3 = Path(self.tmp.name) / "v.mp3"
        from aiplatform.fakes import fake_narrate

        fake_narrate.write_mp3(str(mp3), 5.0)
        status, _ = self.json("POST", f"/api/studio/projects/{project['id']}/upload/voice", mp3.read_bytes())
        self.assertEqual(status, 200)
        self.json("POST", f"/api/studio/projects/{project['id']}/run", {})
        done = self.settle(project["id"])
        self.assertEqual(self.statuses(done)["render"], "done", done["log"])
        self.assertAlmostEqual(media.duration(self.folder(project["id"]) / "video.mp4"), 5.0, delta=0.6)

    def test_rerun_single_step_and_settings_reset(self):
        project = self.create(scenario="article", text=ARTICLE, run=True)
        self.settle(project["id"])
        pid = project["id"]
        status, view = self.json("POST", f"/api/studio/projects/{pid}/settings", {"shorts": True})
        self.assertEqual(status, 200, view)
        self.assertEqual((self.statuses(view)["short"], self.statuses(view)["render"]), ("pending", "done"))
        self.json("POST", f"/api/studio/projects/{pid}/run", {"step": "short"})
        done = self.settle(pid)
        self.assertEqual(self.statuses(done)["short"], "done", done["log"])
        # Сценарий шортса, поправленный руками, не переписывается LLM.
        beats = {"beats": [{"title": "Свой", "text": "Свой текст шортса из пяти слов."}]}
        status, view = self.json("POST", f"/api/studio/projects/{pid}/artifacts/short", {"text": json.dumps(beats)})
        self.assertEqual((status, self.statuses(view)["short"]), (200, "pending"))
        self.json("POST", f"/api/studio/projects/{pid}/run", {"step": "short"})
        done = self.settle(pid)
        self.assertEqual(done["artifacts"]["short"]["beats"][0]["title"], "Свой")
        script = (self.folder(pid) / "short" / "script.md").read_text(encoding="utf-8")
        self.assertEqual(script.strip(), "Свой текст шортса из пяти слов.")


@NEEDS_FFMPEG
class DigestFlowTest(StudioCase):
    def test_digest_from_fake_narrator(self):
        project = self.create(scenario="digest", day="2026-10-01", settings={"poster_mode": "local"}, run=True)
        self.assertEqual(project["title"], "Дайджест новостей за 01.10.2026")
        done = self.settle(project["id"])
        steps = self.statuses(done)
        self.assertEqual(steps["publish"], "waiting", done["log"])
        self.assertEqual(steps["short"], "skipped")
        self.assertIn("релиз языка Go", done["artifacts"]["text"])
        self.assertIn("Дайджест дня", done["artifacts"]["post"], "пост из narrator не перезаписывается планом")
        self.assertTrue(any("выпуск:" in line for line in done["log"]))
        self.assertIsNotNone(done["files"]["video"])


@NEEDS_FFMPEG
class DigestVerdictTest(StudioCase):
    process_env = FAST | {"FAKE_NEWS_VERDICT": "не готово"}

    def test_not_ready_holds_publish(self):
        settings = {"poster_mode": "local", "channels": ["download-16x9"], "auto_publish": True}
        project = self.create(scenario="digest", day="2026-10-03", settings=settings, run=True)
        done = self.settle(project["id"])
        self.assertEqual(done["review"]["verdict"], "не готово")
        publish = next(s for s in done["steps"] if s["id"] == "publish")
        self.assertEqual(publish["status"], "waiting", done["log"])
        self.assertIn("Судья сказал «не готово»", publish["note"])
        self.assertTrue(any("судья: не готово" in line for line in done["log"]))

        status, _ = self.json("POST", f"/api/studio/projects/{project['id']}/publish", {"channels": ["download-16x9"]})
        self.assertEqual(status, 200)
        self.assertEqual(self.statuses(self.settle(project["id"]))["publish"], "waiting")
        body = {"channels": ["download-16x9"], "ignore_verdict": True}
        self.json("POST", f"/api/studio/projects/{project['id']}/publish", body)
        self.assertEqual(self.statuses(self.settle(project["id"]))["publish"], "done")


class DigestEmptyTest(StudioCase):
    process_env = FAST | {"FAKE_NEWS_EMPTY": "1"}

    def test_no_digest_fails_text_step(self):
        project = self.create(scenario="digest", day="2026-10-02", run=True)
        done = self.settle(project["id"])
        text = done["steps"][0]
        self.assertEqual(text["status"], "failed")
        self.assertIn("Дайджеста за 2026-10-02 нет", text["error"])
        self.assertEqual(self.statuses(done)["poster_prompt"], "pending")


class LlmGarbageTest(StudioCase):
    process_env = FAST | {"FAKE_LLM_GARBAGE": "1"}

    def test_plan_fails_with_message(self):
        project = self.create(scenario="article", text=ARTICLE, settings={"poster_mode": "upload"})
        self.json("POST", f"/api/studio/projects/{project['id']}/run", {"step": "plan"})
        done = self.settle(project["id"])
        plan = next(s for s in done["steps"] if s["id"] == "plan")
        self.assertEqual(plan["status"], "failed")
        self.assertIn("не JSON", plan["error"])
        # Человек правит план руками — шаг готов.
        fixed = {"slides": [{"para": 0, "title": "Свой слайд"}]}
        status, view = self.json(
            "POST", f"/api/studio/projects/{project['id']}/artifacts/plan", {"text": json.dumps(fixed)}
        )
        self.assertEqual((status, self.statuses(view)["plan"]), (200, "done"))
        status, body = self.json("POST", f"/api/studio/projects/{project['id']}/artifacts/plan", {"text": "{"})
        self.assertEqual(status, 400, body)


class SlowStudioTest(StudioCase):
    process_env = FAST | {"FAKE_TTS_STEP_DELAY": "1"}

    def test_double_run_conflict_and_cancel(self):
        settings = {"poster_mode": "upload", "draw_images": False}
        project = self.create(scenario="article", text=ARTICLE, settings=settings)
        pid = project["id"]
        self.json("POST", f"/api/studio/projects/{pid}/run", {"step": "voice"})
        status, body = self.json("POST", f"/api/studio/projects/{pid}/run", {})
        self.assertEqual(status, 409, body)
        status, body = self.json("POST", f"/api/studio/projects/{pid}/artifacts/text", {"text": "новый"})
        self.assertEqual(status, 409, body)
        self.poll("/api/audio/jobs", lambda b: any(j["status"] == "running" for j in b["jobs"]))
        self.json("POST", f"/api/studio/projects/{pid}/cancel", {})
        done = self.settle(pid, timeout=20)
        self.assertEqual(self.statuses(done)["voice"], "pending")
        # Отмена проекта отменяет и задачу вкладки.
        self.poll("/api/audio/jobs", lambda b: b["jobs"][0]["status"] == "canceled", timeout=10)


class TelegramWithoutTokenTest(StudioCase):
    def setUp(self):
        channels = Path(tempfile.mkdtemp()) / "channels.json"
        self.addCleanup(shutil.rmtree, channels.parent)
        channels.write_text(
            json.dumps(
                [{"id": "tg", "label": "TG", "type": "telegram", "chat_id": "@x", "token_env": "AIP_TG_NO_SUCH"}]
            ),
            encoding="utf-8",
        )
        self.extra_env = StudioCase.extra_env | {"AIP_CHANNELS": str(channels)}
        super().setUp()

    def test_config_shows_problem_and_publish_fails(self):
        _, cfg = self.json("GET", "/api/studio/config")
        self.assertEqual(cfg["channels"][0]["problem"], "нет токена в переменной AIP_TG_NO_SUCH")
        project = self.create(scenario="article", text=ARTICLE, settings={"channels": ["tg"], "auto_publish": True})
        folder = self.folder(project["id"])
        (folder / "video.mp4").write_bytes(b"x")
        self.json("POST", f"/api/studio/projects/{project['id']}/run", {"step": "publish"})
        done = self.settle(project["id"])
        publish = next(s for s in done["steps"] if s["id"] == "publish")
        self.assertEqual(publish["status"], "failed")
        self.assertIn("нет токена", publish["error"])


class ValidationTest(StudioCase):
    def test_bad_requests(self):
        cases = [
            ({"scenario": "x"}, "digest, article или post"),
            ({"scenario": "article", "text": "  "}, "текст"),
            ({"scenario": "digest", "day": "2999-01-01"}, "не наступил"),
            ({"scenario": "article", "text": "т", "settings": {"poster_mode": "x"}}, "poster_mode"),
            ({"scenario": "article", "text": "т", "settings": {"nope": 1}}, "Неизвестная настройка"),
            ({"scenario": "article", "text": "т", "settings": {"channels": ["nope"]}}, "Нет таких каналов"),
            ({"scenario": "article", "text": "т", "settings": {"engine": "silero", "voice": "Charon"}}, "нет голоса"),
            ({"scenario": "article", "text": "т", "settings": {"image_model": "nope"}}, "модель картинок"),
        ]
        for payload, message in cases:
            with self.subTest(message):
                status, body = self.json("POST", "/api/studio/projects", payload)
                self.assertEqual(status, 400, body)
                self.assertIn(message, body["error"])

    def test_files_and_unknowns(self):
        project = self.create(scenario="article", text="Один абзац.")
        pid = project["id"]
        self.assertEqual(self.json("GET", "/api/studio/projects/nope")[0], 404)
        self.assertEqual(self.call("GET", f"/api/studio/projects/{pid}/files/project.json")[0], 404)
        self.assertEqual(self.call("GET", f"/api/studio/projects/{pid}/files/../../x")[0], 404)
        self.assertEqual(self.call("GET", f"/api/studio/projects/{pid}/files/video.mp4")[0], 404)
        self.assertEqual(self.json("POST", f"/api/studio/projects/{pid}/run", {"step": "nope"})[0], 400)
        self.assertEqual(self.json("POST", f"/api/studio/projects/{pid}/approve", {"step": "publish"})[0], 409)
        self.assertEqual(self.json("POST", f"/api/studio/projects/{pid}/artifacts/nope", {"text": "x"})[0], 404)
        _, listing = self.json("GET", "/api/studio/projects?scenario=article")
        self.assertEqual([p["id"] for p in listing["projects"]], [pid])
        self.assertEqual(self.json("GET", "/api/studio/projects?scenario=digest")[1]["projects"], [])

    def test_config(self):
        status, cfg = self.json("GET", "/api/studio/config")
        self.assertEqual(status, 200)
        self.assertEqual([s["id"] for s in cfg["scenarios"]], ["digest", "article", "post"])
        self.assertEqual(cfg["defaults"]["image_model"], "z-image-turbo")
        self.assertEqual([c["id"] for c in cfg["channels"]], ["download-16x9", "download-9x16"])
        self.assertTrue(cfg["llm"]["ok"])

    def test_restart_marks_running_step_failed(self):
        project = self.create(scenario="article", text="Абзац.")
        path = self.folder(project["id"]) / "project.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["running"] = True
        data["steps"]["plan"]["status"] = "running"
        path.write_text(json.dumps(data), encoding="utf-8")
        from aiplatform import server as server_module

        app = server_module.App(self.config)
        self.addCleanup(app.close)
        loaded = app.modules["studio"].studio.get(project["id"])
        self.assertFalse(loaded["running"])
        self.assertEqual(loaded["steps"]["plan"]["status"], "failed")


class StudioErrorsTest(StudioCase):
    """Нет проекта — 404; KeyError из бага в коде — 500, а не «Проект не найден» (BE-003)."""

    def test_missing_project_is_404(self):
        status, body = self.json("POST", "/api/studio/projects/nope/run", {})
        self.assertEqual((status, body["error"]), (404, "Проект не найден."))

    def test_bug_keyerror_is_not_404(self):
        project = self.create(scenario="article", text=ARTICLE)

        def broken(project_id):
            return {}["steps"]

        self.app.modules["studio"].view = broken
        with self.assertLogs("aiplatform", "ERROR"):
            status, body = self.json("GET", f"/api/studio/projects/{project['id']}")
        self.assertEqual((status, body["code"]), (500, "internal"))


if __name__ == "__main__":
    unittest.main()
