"""Сквозные тесты вкладки «Видео»: HTTP-сервер платформы + fake_ltx вместо модели."""

import json
import tempfile
import time
import unittest
from pathlib import Path

from aiplatform import config as platform_config
from aiplatform import server as server_module
from aiplatform.video import config as config_module
from aiplatform.video import timeline
from aiplatform.video.service import VideoModule
from tests.helpers import PlatformCase, isolated_config

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def video_only(ctx):
    return [VideoModule(ctx)]


def load(env):
    return config_module.load(platform_config.load(isolated_config(env)))


class ServerCase(PlatformCase):
    step_delay = "0.01"
    fail_flag = ""
    modules = staticmethod(video_only)
    extra_env = {"AIP_MODELS_DIR": "/nonexistent-models"}  # не зависеть от весов на этой машине

    def setUp(self):
        self.process_env = {
            "FAKE_LTX_STEP_DELAY": self.step_delay,
            "FAKE_LTX_NO_FFMPEG": "1",
            "FAKE_LTX_FAIL": self.fail_flag,
        }
        super().setUp()

    def upload(self):
        status, body = self.json("POST", "/api/video/uploads", PNG)
        self.assertEqual(status, 201, body)
        return body["id"]

    def wait(self, job_id, statuses=("done", "failed", "canceled"), timeout=20):
        deadline = time.time() + timeout
        seen = []
        while time.time() < deadline:
            _, job = self.json("GET", f"/api/video/jobs/{job_id}")
            seen.append(job)
            if job["status"] in statuses:
                return job, seen
            time.sleep(0.02)
        self.fail(f"задача не дошла до {statuses}: {seen[-1]}")


class HappyPathTest(ServerCase):
    def test_keyframes_job_runs_to_done_and_serves_video(self):
        first, second = self.upload(), self.upload()
        status, job = self.json(
            "POST", "/api/video/jobs", {"prompt": "кот идёт", "frames": [{"upload_id": first}, {"upload_id": second}]}
        )
        self.assertEqual(status, 201, job)
        self.assertEqual([(f["upload_id"], f["frame_idx"]) for f in job["frames"]], [(first, 0), (second, 96)])
        self.assertGreaterEqual(job["seed"], 0, "случайный seed фиксируется в задаче")

        done, seen = self.wait(job["id"])
        self.assertEqual(done["status"], "done", done)
        self.assertEqual(done["progress"]["percent"], 100.0)
        percents = [j["progress"]["percent"] for j in seen]
        self.assertEqual(percents, sorted(percents))
        self.assertTrue(any("Saved:" in line for line in done["log"]))
        self.assertFalse(any("Denoising" in line for line in done["log"]), "полосы tqdm в лог не попадают")

        status, headers, video = self.call("GET", done["video_url"])
        self.assertEqual((status, headers["Content-Type"]), (200, "video/mp4"))
        status, headers, part = self.call("GET", done["video_url"], headers={"Range": "bytes=4-11"})
        self.assertEqual((status, part), (206, video[4:12]))
        self.assertEqual(headers["Content-Range"], f"bytes 4-11/{len(video)}")
        status, _, _ = self.call("GET", done["video_url"], headers={"Range": f"bytes={len(video)}-"})
        self.assertEqual(status, 416)

        _, listing = self.json("GET", "/api/video/jobs")
        self.assertEqual([j["id"] for j in listing["jobs"]], [job["id"]])

    def test_job_survives_restart(self):
        _, job = self.json("POST", "/api/video/jobs", {"prompt": "p"})
        self.wait(job["id"])
        reloaded = server_module.App(self.config, video_only)
        self.addCleanup(reloaded.close)
        jobs = reloaded.modules["video"].jobs
        self.assertEqual(jobs.get(job["id"])["status"], "done")
        self.assertIsNotNone(jobs.video_path(job["id"]))

    def test_static_and_config(self):
        status, headers, body = self.call("GET", "/video/")
        self.assertEqual(status, 200)
        self.assertIn(b"/nav.js", body)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertEqual(self.call("GET", "/../server.py")[0], 404)
        status, cfg = self.json("GET", "/api/video/config")
        self.assertEqual((status, cfg["backend"], cfg["runner_available"]), (200, "fake", True))

    def test_empty_settings_fall_back_to_defaults(self):
        # make экспортирует VIDEOGEN_LTX_CMD и VIDEOGEN_MODEL пустыми, пока нет local.mk.
        cfg = load({"VIDEOGEN_LTX_CMD": "", "VIDEOGEN_MODEL": "", "VIDEOGEN_MODELS_DIR": ""})
        self.assertEqual((cfg.ltx_cmd, cfg.model), (("ltx-2-mlx",), ""))
        self.assertEqual(str(cfg.models_dir), platform_config.DEFAULT_MODELS_DIR)

    def test_chosen_model_reaches_pipeline(self):
        status, cfg = self.json("GET", "/api/video/config")
        self.assertEqual([m["id"] for m in cfg["models"]], ["ltx-2.3-q4", "ltx-2.5-q4", "wan-2.2-t2v-q4"])
        self.assertEqual(cfg["default_model"], "ltx-2.5-q4")
        status, job = self.json("POST", "/api/video/jobs", {"prompt": "кот"})
        self.assertEqual((status, job["model"]), (201, "ltx-2.5-q4"), "без выбора — модель по умолчанию")
        status, job = self.json("POST", "/api/video/jobs", {"prompt": "кот", "model": "ltx-2.3-q4"})
        self.assertEqual((status, job["model"], job["model_label"]), (201, "ltx-2.3-q4", "LTX-2.3 Q4"))
        done, _ = self.wait(job["id"])
        self.assertTrue(any("dgrauet/ltx-2.3-mlx-q4" in line for line in done["log"]), done["log"])
        status, err = self.json("POST", "/api/video/jobs", {"prompt": "кот", "model": "нет такой"})
        self.assertEqual(status, 400, err)


class WanTest(ServerCase):
    def test_prompt_only_job_runs_through_wan_pipeline(self):
        _, cfg = self.json("GET", "/api/video/config")
        wan = next(m for m in cfg["models"] if m["id"] == "wan-2.2-t2v-q4")
        self.assertEqual((wan["fps"], wan["durations_s"]), (16, [2, 3, 4, 5]))
        self.assertEqual((wan["max_keyframes"], wan["audio"]), (0, False))

        status, job = self.json("POST", "/api/video/jobs", {"prompt": "кот", "model": wan["id"], "duration_s": 5})
        self.assertEqual(status, 201, job)
        self.assertEqual((job["num_frames"], job["fps"], job["audio"]), (81, 16, False))
        done, seen = self.wait(job["id"])
        self.assertEqual(done["status"], "done", done)
        self.assertIn("Шумоподавление", {j["progress"]["stage"] for j in seen})
        self.assertFalse(any("\x1b" in line for line in done["log"]), done["log"])
        self.assertEqual(self.call("GET", done["video_url"])[0], 200)

    def test_frames_and_long_clips_are_refused(self):
        for payload in ({"frames": [{"upload_id": self.upload()}]}, {"duration_s": 8}):
            status, body = self.json("POST", "/api/video/jobs", {"prompt": "кот", "model": "wan-2.2-t2v-q4", **payload})
            self.assertEqual(status, 400, body)


class ValidationTest(ServerCase):
    def test_bad_requests(self):
        self.assertEqual(self.json("POST", "/api/video/uploads", b"GIF89a....")[0], 400)
        self.assertEqual(self.json("POST", "/api/video/jobs", {"prompt": ""})[0], 400)
        self.assertEqual(self.json("POST", "/api/video/jobs", b"{not json")[0], 400)
        status, body = self.json("POST", "/api/video/jobs", {"prompt": "p", "frames": [{"upload_id": "0" * 32}]})
        self.assertEqual(status, 400)
        self.assertIn("Кадр 1", body["error"])
        self.assertEqual(self.json("GET", "/api/video/jobs/nope")[0], 404)
        self.assertEqual(self.call("GET", "/api/video/uploads/..%2f..%2fjob")[0], 404)

    def test_cross_origin_post_rejected(self):
        status, _ = self.json("POST", "/api/video/jobs", {"prompt": "p"}, {"Origin": "https://evil.example"})
        self.assertEqual(status, 403)


class FailureTest(ServerCase):
    fail_flag = "1"

    def test_failed_run_reports_last_message(self):
        _, job = self.json("POST", "/api/video/jobs", {"prompt": "p"})
        failed, _ = self.wait(job["id"])
        self.assertEqual(failed["status"], "failed")
        self.assertIn("кодом 1", failed["error"])
        self.assertIn("fake failure requested", failed["error"])
        self.assertIsNone(failed["video_url"])


class CancelTest(ServerCase):
    step_delay = "0.5"

    def test_cancel_running_and_queued(self):
        _, running = self.json("POST", "/api/video/jobs", {"prompt": "первая"})
        _, queued = self.json("POST", "/api/video/jobs", {"prompt": "вторая"})
        self.wait(running["id"], statuses=("running",))
        _, view = self.json("GET", f"/api/video/jobs/{queued['id']}")
        self.assertEqual((view["status"], view["queue_position"]), ("queued", 1))

        _, view = self.json("POST", f"/api/video/jobs/{queued['id']}/cancel")
        self.assertEqual(view["status"], "canceled")
        started = time.time()
        self.json("POST", f"/api/video/jobs/{running['id']}/cancel")
        canceled, _ = self.wait(running["id"])
        self.assertEqual(canceled["status"], "canceled")
        self.assertLess(time.time() - started, 5, "отмена должна убивать процесс, а не ждать его")
        self.assertFalse((Path(self.tmp.name) / "video" / "jobs" / running["id"] / "output.mp4").exists())


class RestartRecoveryTest(unittest.TestCase):
    def test_unfinished_jobs_are_marked_failed_on_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "video" / "jobs" / "abcdef012345"
            folder.mkdir(parents=True)
            job = {
                "id": "abcdef012345", "status": "running", "created_at": 1.0, "started_at": 2.0,
                "finished_at": None, "progress": {"stage": "x", "step": 1, "steps": 8, "percent": 20.0},
                "error": None, "log": [],
            }  # fmt: skip
            (folder / "job.json").write_text(json.dumps(job), encoding="utf-8")
            config = platform_config.load({"AIP_BACKEND": "fake", "AIP_DATA_DIR": tmp})
            app = server_module.App(config, video_only)
            self.addCleanup(app.close)
            loaded = app.modules["video"].jobs.get("abcdef012345")
            self.assertEqual(loaded["status"], "failed")
            self.assertIn("перезапущен", loaded["error"])


class ModelsTest(unittest.TestCase):
    def resolve(self, env):
        return {m.id: (m.path, m.installed) for m in load(env).models()}

    def test_installed_models_found_next_to_configured_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            old, new = Path(tmp, "ltx-2.3-mlx-q4"), Path(tmp, "ltx-2.5-mlx-q4")
            old.mkdir()
            # local.mk до появления выбора модели: каталога моделей нет, только VIDEOGEN_MODEL
            found = self.resolve({"VIDEOGEN_MODEL": str(old), "VIDEOGEN_MODELS_DIR": ""})
            self.assertEqual(found["ltx-2.3-q4"], (str(old), True))
            self.assertEqual(found["ltx-2.5-q4"], ("dgrauet/ltx-2.5-mlx-q4", False))
            self.assertFalse(found["wan-2.2-t2v-q4"][1])
            self.assertIsNone(load({"VIDEOGEN_MODELS_DIR": tmp}).gemma())
            Path(tmp, "gemma-3-12b-it-4bit").mkdir()
            self.assertEqual(load({"VIDEOGEN_MODELS_DIR": tmp}).gemma(), str(Path(tmp, "gemma-3-12b-it-4bit")))
            new.mkdir()
            Path(tmp, "Wan2.2-T2V-A14B-MLX").mkdir()  # без подкаталога q4 веса не считаются установленными
            self.assertFalse(self.resolve({"VIDEOGEN_MODELS_DIR": tmp})["wan-2.2-t2v-q4"][1])
            Path(tmp, "Wan2.2-T2V-A14B-MLX", "q4").mkdir()
            self.assertEqual(
                self.resolve({"VIDEOGEN_MODELS_DIR": tmp})["wan-2.2-t2v-q4"],
                (str(Path(tmp, "Wan2.2-T2V-A14B-MLX", "q4")), True),
            )
            self.assertEqual(self.resolve({"VIDEOGEN_MODELS_DIR": tmp})["ltx-2.5-q4"], (str(new), True))

    def test_nothing_installed_and_custom_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(any(installed for _, installed in self.resolve({"AIP_MODELS_DIR": tmp}).values()))
        found = load({"VIDEOGEN_MODEL": "dgrauet/ltx-2.3-mlx-q8"}).models()
        self.assertEqual((found[0].id, found[0].path, found[0].installed), ("custom", "dgrauet/ltx-2.3-mlx-q8", True))

    def test_not_installed_model_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = platform_config.load({"AIP_DATA_DIR": tmp, "AIP_MODELS_DIR": tmp})
            app = server_module.App(config, video_only)
            self.addCleanup(app.close)
            with self.assertRaisesRegex(timeline.ValidationError, r"setup-model\.sh ltx-2\.5-q4"):
                app.modules["video"].jobs.submit(timeline.build_spec({"prompt": "кот", "model": "ltx-2.5-q4"}))


if __name__ == "__main__":
    unittest.main()
