"""Вкладка «Аудио»: проверка запроса, задача через fake_narrate, HTTP."""

import shutil
import unittest
from pathlib import Path

from aiplatform import config as platform_config
from aiplatform.audio import config as audio_config
from aiplatform.audio import jobs as audio_jobs
from aiplatform.audio.jobs import AudioSpec, Progress, ValidationError, build_spec
from aiplatform.audio.service import AudioModule
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


class AudioCancelTest(AudioServerCase):
    process_env = {"FAKE_TTS_STEP_DELAY": "1", "FAKE_TTS_FAIL": ""}

    def test_cancel_running(self):
        _, job = self.json("POST", "/api/audio/jobs", {"text": TEXT})
        self.poll(f"/api/audio/jobs/{job['id']}", lambda j: j["status"] == "running")
        self.json("POST", f"/api/audio/jobs/{job['id']}/cancel")
        self.assertEqual(self.wait(job["id"])["status"], "canceled")


if __name__ == "__main__":
    unittest.main()
