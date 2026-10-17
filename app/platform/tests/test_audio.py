"""Вкладка «Аудио»: проверка запроса, задача через fake_narrate, HTTP."""

import json
import shutil
import time
import unittest
from pathlib import Path

from aiplatform import config as platform_config
from aiplatform.audio import config as audio_config
from aiplatform.audio import jobs as audio_jobs
from aiplatform.audio import voices as audio_voices
from aiplatform.audio.jobs import AudioSpec, Progress, ValidationError, build_spec, parse_tempo
from aiplatform.audio.service import AudioModule
from aiplatform.core import media
from tests.helpers import PlatformCase

TEXT = "Первый абзац про новости дня.\n\nВторой абзац: релиз Go 1.25 и что в нём нового.\n\nТретий абзац."


def audio_only(ctx):
    return [AudioModule(ctx)]


class SpecTest(unittest.TestCase):
    def test_defaults(self):
        spec = build_spec({"text": "  привет  "})
        self.assertEqual(spec, AudioSpec(text="привет\n", engine="higgs", voice="Charon", speed=None))

    def test_engine_voice_speed(self):
        spec = build_spec({"text": "т", "engine": "supertonic", "voice": "F2", "speed": 1.25})
        self.assertEqual((spec.engine, spec.voice, spec.speed), ("supertonic", "F2", 1.25))
        # У silero скорость не настраивается — не передаём её вовсе.
        self.assertIsNone(build_spec({"text": "т", "engine": "silero", "speed": 1.5}).speed)
        # Темп скачиваемого mp3: пусто — 1, запятая как точка, вне 0,5–2 — ошибка.
        self.assertEqual((parse_tempo(None), parse_tempo(""), parse_tempo("1,75")), (1.0, 1.0, 1.75))
        for bad in ("быстро", "0.25", "3"):
            with self.assertRaises(ValidationError):
                parse_tempo(bad)
        # Shem-v2 — клон второй записи живого голоса: есть у всех движков-клонов.
        self.assertEqual(build_spec({"text": "т", "engine": "higgs", "voice": "Shem-v2"}).voice, "Shem-v2")

    def test_rejects(self):
        cases = [
            ({"text": ""}, "Нужен текст"),
            ({"text": "т", "engine": "nope"}, "Неизвестный движок"),
            ({"text": "т", "engine": "silero", "voice": "Charon"}, "нет голоса"),
            ({"text": "т", "speed": 3}, "Скорость"),
            ({"text": "т", "speed": True}, "Скорость"),
            ({"text": "т" * (audio_jobs.MAX_TEXT + 1)}, "длиннее"),
            ([], "не объект"),
        ]
        for payload, message in cases:
            with self.subTest(message), self.assertRaisesRegex(ValidationError, message):
                build_spec(payload)

    def test_argv_matches_narrate_say(self):
        config = audio_config.load(platform_config.load({"AIP_NARRATE_CMD": "/opt/narrate", "AIP_DATA_DIR": "/tmp/x"}))
        self.assertEqual(config.narrate_cmd, ("/opt/narrate",))
        manager = audio_jobs.AudioJobs.__new__(audio_jobs.AudioJobs)
        manager._config = config
        argv = manager.argv(AudioSpec("т", "supertonic", "M1", 1.1), Path("/s.txt"), Path("/o.mp3"))
        self.assertEqual(
            argv,
            ["/opt/narrate", "say", "/s.txt", "--engine", "supertonic", "--voice", "M1", "--speed", "1.1"]
            + ["--out", "/o.mp3", "--no-verify"],
        )

    def test_progress_parser(self):
        p = Progress()
        p.feed("source.txt абзац 2/4")
        self.assertEqual(p.snapshot()["stage"], "Абзац 2 из 4")
        p.feed("[2/4]   3.0 с аудио за    1 с  текст")
        self.assertEqual(p.snapshot()["percent"], 47.5)
        p.feed("синтез: 3 с, склеиваю 4 клипов")
        self.assertEqual(p.snapshot()["stage"], "Склейка и mp3")


class AudioServerCase(PlatformCase):
    modules = staticmethod(audio_only)
    process_env = {"FAKE_TTS_STEP_DELAY": "0.01", "FAKE_TTS_FAIL": ""}

    def wait(self, job_id):
        return self.poll(f"/api/audio/jobs/{job_id}", lambda j: j["status"] not in ("queued", "running"))


class AudioServerTest(AudioServerCase):
    def test_config(self):
        status, cfg = self.json("GET", "/api/audio/config")
        self.assertEqual(status, 200)
        self.assertEqual(cfg["default_engine"], "higgs")
        self.assertIn("supertonic", [e["id"] for e in cfg["engines"]])
        self.assertTrue(cfg["runner_available"])

    def test_text_to_mp3(self):
        status, job = self.json("POST", "/api/audio/jobs", {"text": TEXT, "engine": "silero", "voice": "baya"})
        self.assertEqual(status, 201, job)
        self.assertEqual((job["engine_label"], job["voice"], job["status"]), ("Silero", "baya", "queued"))
        done = self.wait(job["id"])
        self.assertEqual(done["status"], "done", done)
        self.assertEqual(done["text"], TEXT + "\n")
        self.assertEqual(done["progress"]["percent"], 100.0)
        self.assertTrue(any("абзац 3/3" in line for line in done["log"]), done["log"])
        status, headers, audio = self.call("GET", done["audio_url"])
        self.assertEqual((status, headers["Content-Type"]), (200, "audio/mpeg"))
        self.assertGreater(len(audio), 100)
        if shutil.which("ffmpeg"):
            self.assertGreater(done["duration_s"], 3)
        _, headers, _ = self.call("GET", done["audio_url"] + "?download=1")
        self.assertIn("attachment", headers["Content-Disposition"])
        _, listing = self.json("GET", "/api/audio/jobs")
        self.assertEqual([j["id"] for j in listing["jobs"]], [job["id"]])

    @unittest.skipUnless(shutil.which("ffmpeg"), "нужен ffmpeg")
    def test_download_at_speed(self):
        _, job = self.json("POST", "/api/audio/jobs", {"text": TEXT, "engine": "silero", "voice": "baya"})
        done = self.wait(job["id"])
        status, headers, fast = self.call("GET", done["audio_url"] + "?download=1&speed=1.5")
        self.assertEqual(status, 200)
        self.assertIn(f"audio-{job['id']}-x1.5.mp3", headers["Content-Disposition"])
        path = self.config.data_dir / "audio" / "jobs" / job["id"]
        seconds = media.duration(path / "output@1.5.mp3")
        self.assertAlmostEqual(seconds, done["duration_s"] / 1.5, delta=0.3)
        # Второй раз — из готового файла; ×1 — исходный mp3.
        self.assertEqual(self.call("GET", done["audio_url"] + "?speed=1,5")[2], fast)
        _, headers, _ = self.call("GET", done["audio_url"] + "?download=1&speed=1")
        self.assertIn(f"audio-{job['id']}.mp3", headers["Content-Disposition"])
        self.assertEqual(self.json("GET", done["audio_url"] + "?speed=3")[0], 400)

    def test_bad_requests(self):
        self.assertEqual(self.json("POST", "/api/audio/jobs", {"text": ""})[0], 400)
        self.assertEqual(self.json("POST", "/api/audio/jobs", {"text": "т", "engine": "x"})[0], 400)
        self.assertEqual(self.json("GET", "/api/audio/jobs/nope")[0], 404)
        self.assertEqual(self.json("GET", "/api/audio/jobs/nope/audio")[0], 404)

    def test_gpu_gate(self):
        with self.app.ctx.gate.hold("Видео"):
            _, job = self.json("POST", "/api/audio/jobs", {"text": TEXT})
            waiting = self.poll(f"/api/audio/jobs/{job['id']}", lambda j: "Ждёт GPU" in j["progress"]["stage"])
            self.assertIn("Видео", waiting["progress"]["stage"])
        self.assertEqual(self.wait(job["id"])["status"], "done")


class AudioFailureTest(AudioServerCase):
    process_env = {"FAKE_TTS_STEP_DELAY": "0.01", "FAKE_TTS_FAIL": "1"}

    def test_failure_reports_message(self):
        _, job = self.json("POST", "/api/audio/jobs", {"text": TEXT})
        failed = self.wait(job["id"])
        self.assertEqual(failed["status"], "failed")
        self.assertIn("fake failure requested", failed["error"])
        self.assertIsNone(failed["audio_url"])


class AudioEngineFeaturesTest(AudioServerCase):
    """Произношение, файлы, библиотека, проверка и образцы голосов — через мост движка."""

    def test_config_has_engine_state(self):
        _, cfg = self.json("GET", "/api/audio/config")
        by_id = {e["id"]: e for e in cfg["engines"]}
        self.assertIn("модель не скачана", by_id["fish"]["problem"])
        self.assertIsNone(by_id["higgs"]["problem"])
        self.assertTrue(by_id["espeech"]["steps"])
        self.assertEqual(by_id["espeech"]["default_steps"], 32)
        self.assertTrue(cfg["verify_available"])
        self.assertIsNone(cfg["engines_error"])

    def test_preview_and_split(self):
        status, preview = self.json("POST", "/api/audio/preview", {"text": "Docker 5\n"})
        self.assertEqual(status, 200, preview)
        self.assertEqual(preview["script"], "докер пять\n")
        self.assertEqual(preview["changed"], 1)
        _, split = self.json("POST", "/api/audio/split", {"text": "# Раз\n\nтекст\n\n# Два\n\nтекст\n"})
        self.assertEqual([a["title"] for a in split["articles"]], ["Раз", "Два"])
        self.assertEqual(self.json("POST", "/api/audio/preview", {})[0], 400)

    def test_library_texts(self):
        _, library = self.json("GET", "/api/audio/library")
        kinds = {i["kind"] for i in library["items"]}
        self.assertEqual(kinds, {"article", "digest"})
        article = next(i for i in library["items"] if i["kind"] == "article")
        _, item = self.json("GET", f"/api/audio/library/{article['id']}")
        self.assertTrue(item["text"].strip())
        self.assertEqual(self.json("GET", "/api/audio/library/nope")[0], 404)

    def test_batch_steps_and_verify(self):
        articles = [{"title": "Первая", "text": "Раз два три."}, {"title": "Вторая", "text": "Четыре пять."}]
        payload = {"articles": articles, "engine": "espeech", "voice": "Charon", "steps": 16, "verify": True}
        status, body = self.json("POST", "/api/audio/jobs/batch", payload)
        self.assertEqual(status, 201, body)
        self.assertEqual([j["title"] for j in body["jobs"]], ["Первая", "Вторая"])
        self.assertEqual(body["jobs"][0]["steps"], 16)
        done = self.wait(body["jobs"][1]["id"])
        self.assertEqual(done["status"], "done", done)
        self.assertEqual(done["check"]["overall"], 0.93)
        self.assertEqual(done["check"]["weak"][0]["line"], 1)
        self.assertEqual(self.json("POST", "/api/audio/jobs/batch", {"articles": []})[0], 400)
        self.assertEqual(self.json("POST", "/api/audio/jobs", {"text": "т", "engine": "espeech", "steps": 500})[0], 400)

    def test_no_verify_by_default(self):
        _, job = self.json("POST", "/api/audio/jobs", {"text": TEXT, "engine": "silero", "voice": "baya"})
        self.assertIsNone(self.wait(job["id"])["check"])

    def test_voices_listing_sample_and_record(self):
        _, data = self.json("GET", "/api/audio/voices")
        by_stem = {v["stem"]: v for v in data["voices"]}
        self.assertIsNone(by_stem["m1"]["record"])
        status, headers, _ = self.call("GET", by_stem["charon"]["sample_url"])
        self.assertEqual((status, headers["Content-Type"]), (200, "audio/wav"))
        self.assertEqual(self.json("POST", "/api/audio/voices/m1", {})[0], 400)
        self.assertEqual(self.json("POST", "/api/audio/voices/shem", {})[0], 400)

        wav = (self.config.data_dir / "fake-narrator" / "voices" / "m1.wav").read_bytes()
        status, _, raw = self.call("POST", "/api/audio/uploads", wav, {"Content-Type": "audio/wav"})
        self.assertEqual(status, 201, raw)
        upload = json.loads(raw)["id"]
        self.assertEqual(self.json("POST", "/api/audio/voices/shem", {"upload": upload})[0], 400)
        # CMP-010: без подтверждения согласия — 400, задача не стартует.
        body = {"upload": upload, "text": "Мой голос."}
        for consent in (None, {"owner": "Аня"}, {"confirmed": True}, {"confirmed": True, "owner": "Аня", "form": "x"}):
            status, err = self.json("POST", "/api/audio/voices/shem", body | ({"consent": consent} if consent else {}))
            self.assertEqual(status, 400, (consent, err))
        consent = {"confirmed": True, "owner": "Аня", "form": "self"}
        status, task = self.json("POST", "/api/audio/voices/shem", body | {"consent": consent})
        self.assertEqual(status, 202, task)
        done = self.poll(f"/api/audio/tasks/{task['id']}", lambda t: t["status"] not in ("queued", "running"))
        self.assertEqual(done["status"], "done", done)
        _, data = self.json("GET", "/api/audio/voices")
        shem = next(v for v in data["voices"] if v["stem"] == "shem")
        self.assertTrue(shem["custom"])
        self.assertEqual(shem["text"], "Мой голос.")
        self.assertEqual(shem["consent"], "live")
        # Образец и запись о согласии есть, исходной загрузки в uploads/ нет.
        voices = self.config.data_dir / "fake-narrator" / "voices"
        record = json.loads((voices / "shem.consent.json").read_text(encoding="utf-8"))
        self.assertEqual((record["kind"], record["owner"], record["form"]), ("live", "Аня", "self"))
        self.assertIsNone(self.app.ctx.uploads.path(upload))
        self.assertEqual(self.call("GET", f"/api/audio/uploads/{upload}")[0], 404)

    def test_gemini_record_needs_key(self):
        _, task = self.json("POST", "/api/audio/voices/charon", {})
        done = self.poll(f"/api/audio/tasks/{task['id']}", lambda t: t["status"] not in ("queued", "running"))
        self.assertEqual(done["status"], "failed")
        self.assertIn("GEMINI_API_KEY", done["error"])


class AudioCancelTest(AudioServerCase):
    process_env = {"FAKE_TTS_STEP_DELAY": "1", "FAKE_TTS_FAIL": ""}

    def test_cancel_running(self):
        _, job = self.json("POST", "/api/audio/jobs", {"text": TEXT})
        self.poll(f"/api/audio/jobs/{job['id']}", lambda j: j["status"] == "running")
        self.json("POST", f"/api/audio/jobs/{job['id']}/cancel")
        self.assertEqual(self.wait(job["id"])["status"], "canceled")


def live_engines(ready: set[str], models_dir_exists: bool = True) -> dict:
    """Ответ моста engines: у движков вне ready — проблема «нет весов»."""
    return {
        "engines": [{"id": e, "problem": None if e in ready else "нет модели"} for e in audio_voices.ENGINES],
        "models_dir_exists": models_dir_exists,
        "models_hint": "папки моделей /nowhere нет — подключи диск или задай $NARRATOR_MODELS_DIR",
    }


class DefaultEngineTest(AudioServerCase):
    """DX-004: движок по умолчанию — тот, чьи веса стоят; нет весов — баннер с командой."""

    def given(self, info: dict) -> AudioModule:
        module = self.app.modules["audio"]
        module._engines = (time.time(), info)
        return module

    def test_after_make_models_default_is_espeech(self):
        module = self.given(live_engines({"espeech", "silero", "supertonic"}))
        self.assertEqual(module.config.default_engine, "espeech")
        self.assertEqual(module.problems(), [])
        self.assertEqual(build_spec({"text": "т"}, module.config.default_engine).engine, "espeech")

    def test_higgs_first_when_installed(self):
        module = self.given(live_engines({"higgs", "espeech", "silero"}))
        self.assertEqual(module.config.default_engine, "higgs")

    def test_no_weights_banner_names_command(self):
        module = self.given(live_engines(set(), models_dir_exists=False))
        self.assertEqual(module.config.default_engine, "higgs")
        [problem] = module.problems()
        self.assertIn("Higgs", problem)
        self.assertIn("make models-mlx E=higgs", problem)
        self.assertIn("NARRATOR_MODELS_DIR", problem)

    def test_bridge_error_keeps_builtin_default(self):
        module = self.given({"error": "мост упал"})
        self.assertEqual(module.config.default_engine, "higgs")
        self.assertEqual(module.problems(), [])


class ExplicitEngineTest(AudioServerCase):
    extra_env = {"AIP_AUDIO_ENGINE": "supertonic"}

    def test_explicit_engine_wins_and_reports_missing_weights(self):
        module = self.app.modules["audio"]
        module._engines = (time.time(), live_engines({"espeech"}))
        self.assertEqual(module.config.default_engine, "supertonic")
        [problem] = module.problems()
        self.assertIn("Supertonic", problem)
        self.assertIn("cd app/narrator && make models", problem)


if __name__ == "__main__":
    unittest.main()
