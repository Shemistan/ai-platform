"""Сквозные тесты вкладки «Фото»: HTTP-сервер платформы + fake_gen вместо моделей."""

import json
import tempfile
import time
import unittest
from pathlib import Path

from aiplatform import config as platform_config
from aiplatform import server as server_module
from aiplatform.core import uploads
from aiplatform.fakes import fake_gen
from aiplatform.photo import config as config_module
from aiplatform.photo import models, params
from aiplatform.photo.service import PhotoModule
from tests.helpers import PlatformCase, isolated_config


def png(width, height, seed=1):
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "x.png"
        fake_gen.write_png(str(path), width, height, seed)
        return path.read_bytes()


def photo_only(ctx):
    return [PhotoModule(ctx)]


def load(env):
    return config_module.load(platform_config.load(isolated_config(env)))


class ServerCase(PlatformCase):
    step_delay = "0.01"
    fail_flag = ""
    modules = staticmethod(photo_only)

    def setUp(self):
        self.process_env = {"FAKE_GEN_STEP_DELAY": self.step_delay, "FAKE_GEN_FAIL": self.fail_flag}
        super().setUp()

    def upload(self, width=512, height=384):
        status, body = self.json("POST", "/api/photo/uploads", png(width, height))
        self.assertEqual(status, 201, body)
        self.assertEqual((body["width"], body["height"]), (width, height))
        return body["id"]

    def wait(self, job_id, statuses=("done", "failed", "canceled"), timeout=20):
        deadline = time.time() + timeout
        seen = []
        while time.time() < deadline:
            _, job = self.json("GET", f"/api/photo/jobs/{job_id}")
            seen.append(job)
            if job["status"] in statuses:
                return job, seen
            time.sleep(0.02)
        self.fail(f"задача не дошла до {statuses}: {seen[-1]}")

    def run_job(self, payload):
        status, job = self.json("POST", "/api/photo/jobs", payload)
        self.assertEqual(status, 201, job)
        done, seen = self.wait(job["id"])
        self.assertEqual(done["status"], "done", done)
        _, _, image = self.call("GET", done["image_url"])
        return done, seen, image


class HappyPathTest(ServerCase):
    def test_txt2img_runs_to_done_and_serves_image(self):
        done, seen, image = self.run_job({"prompt": "кот", "model": "z-image-turbo", "size": "768x768"})
        self.assertEqual((done["mode"], done["model_label"], done["steps"]), ("txt2img", "Z-Image Turbo", 9))
        self.assertGreaterEqual(done["seed"], 0, "случайный seed фиксируется в задаче")
        self.assertEqual(done["progress"]["percent"], 100.0)
        percents = [j["progress"]["percent"] for j in seen]
        self.assertEqual(percents, sorted(percents))
        self.assertFalse(any("%|" in line for line in done["log"]), "полосы tqdm в лог не попадают")
        self.assertEqual(uploads.png_size(image), (768, 768))

        # Время генерации: считается от старта процесса и после завершения не растёт.
        self.assertGreater(done["elapsed_s"], 0)
        self.assertAlmostEqual(done["elapsed_s"], done["finished_at"] - done["started_at"], places=0)
        time.sleep(0.15)
        self.assertEqual(self.json("GET", f"/api/photo/jobs/{done['id']}")[1]["elapsed_s"], done["elapsed_s"])

        status, headers, _ = self.call("GET", done["image_url"])
        self.assertEqual((status, headers["Content-Type"], headers["Content-Disposition"]), (200, "image/png", None))
        status, headers, attached = self.call("GET", done["image_url"] + "?download=1")
        self.assertEqual(headers["Content-Disposition"], f'attachment; filename="photo-{done["id"]}.png"')
        self.assertEqual(attached, image)

        _, listing = self.json("GET", "/api/photo/jobs")
        self.assertEqual([j["id"] for j in listing["jobs"]], [done["id"]])
        self.assertNotIn("log", listing["jobs"][0])

    def test_img2img_on_both_pipelines(self):
        source = self.upload(512, 384)
        done, _, image = self.run_job({"prompt": "портрет", "mode": "img2img", "source": source, "model": "krea-2"})
        self.assertEqual((done["width"], done["height"], done["strength"]), (512, 384, 0.6))
        self.assertEqual(done["source_url"], f"/api/photo/uploads/{source}")
        self.assertEqual(uploads.png_size(image), (512, 384))
        done, _, image = self.run_job(
            {"prompt": "портрет", "mode": "img2img", "source": source, "model": "flux2-klein-9b"}
        )
        self.assertEqual(uploads.png_size(image), (512, 384))
        self.assertTrue(any("Output saved to" in line for line in done["log"]), done["log"])

    def test_inpaint(self):
        source, mask = self.upload(512, 384), self.upload(512, 384)
        done, _, image = self.run_job(
            {"prompt": "закат", "mode": "inpaint", "source": source, "mask": mask, "model": "flux2-klein-9b"}
        )
        self.assertEqual((done["mask_url"], done["strength"]), (f"/api/photo/uploads/{mask}", 1.0))
        self.assertEqual(uploads.png_size(image), (512, 384))
        status, err = self.json(
            "POST",
            "/api/photo/jobs",
            {"prompt": "p", "mode": "inpaint", "source": source, "mask": self.upload(256, 256)},
        )
        self.assertEqual(status, 400)
        self.assertIn("не совпадает", err["error"])

    def test_outpaint_extends_canvas(self):
        source = self.upload(512, 384)
        done, _, image = self.run_job(
            {"prompt": "небо", "mode": "outpaint", "source": source, "padding": {"left": 128, "right": 128, "top": 64}}
        )
        self.assertEqual(done["model"], "flux2-klein-9b", "единственная модель с outpaint выбирается сама")
        self.assertEqual(done["padding"], {"left": 128, "top": 64, "right": 128, "bottom": 0})
        self.assertEqual((done["width"], done["height"]), (768, 448))
        self.assertEqual(uploads.png_size(image), (768, 448))

    def test_every_model_generates(self):
        for model in models.CATALOG:
            with self.subTest(model.id):
                done, _, _ = self.run_job({"prompt": "кот", "model": model.id, "size": "512x512", "detail": "draft"})
                self.assertEqual(done["model"], model.id)

    def test_job_survives_restart(self):
        done, _, _ = self.run_job({"prompt": "p", "size": "512x512"})
        reloaded = server_module.App(self.config, photo_only)
        self.addCleanup(reloaded.close)
        jobs = reloaded.modules["photo"].jobs
        self.assertEqual(jobs.get(done["id"])["status"], "done")
        self.assertIsNotNone(jobs.image_path(done["id"]))

    def test_static_and_config(self):
        status, headers, body = self.call("GET", "/photo/")
        self.assertEqual(status, 200)
        self.assertIn(b"/nav.js", body)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertEqual(self.call("GET", "/../server.py")[0], 404)
        status, cfg = self.json("GET", "/api/photo/config")
        self.assertEqual((status, cfg["backend"], cfg["runners"]), (200, "fake", {"flux2": True, "mflux": True}))
        self.assertEqual(len(cfg["models"]), 6)
        defaults = cfg["default_models"]
        self.assertEqual((defaults["txt2img"], defaults["outpaint"]), ("flux2-dev", "flux2-klein-9b"))
        self.assertEqual([m["id"] for m in cfg["modes"]], list(models.MODES))
        klein = next(m for m in cfg["models"] if m["id"] == "flux2-klein-9b")
        self.assertEqual(
            (klein["modes"], klein["guidance"], klein["strength_modes"]), (list(models.MODES), None, ["inpaint"])
        )
        qwen = next(m for m in cfg["models"] if m["id"] == "qwen-image-2512")
        self.assertEqual((qwen["guidance"]["default"], qwen["negative"], qwen["steps"]["standard"]), (3.5, True, 20))


class ValidationTest(ServerCase):
    def test_bad_requests(self):
        self.assertEqual(self.json("POST", "/api/photo/uploads", b"GIF89a....")[0], 400)
        self.assertEqual(self.json("POST", "/api/photo/jobs", {"prompt": ""})[0], 400)
        self.assertEqual(self.json("POST", "/api/photo/jobs", b"{not json")[0], 400)
        status, body = self.json("POST", "/api/photo/jobs", {"prompt": "p", "mode": "img2img", "source": "0" * 32})
        self.assertEqual(status, 400)
        self.assertIn("не найдено", body["error"])
        self.assertEqual(self.json("GET", "/api/photo/jobs/nope")[0], 404)
        self.assertEqual(self.json("GET", "/api/photo/jobs/nope/image")[0], 404)
        self.assertEqual(self.call("GET", "/api/photo/uploads/..%2f..%2fjob")[0], 404)

    def test_jpeg_upload_has_no_size_and_cannot_be_a_source(self):
        status, body = self.json("POST", "/api/photo/uploads", b"\xff\xd8\xff\xe0" + b"\x00" * 64)
        self.assertEqual((status, body["width"]), (201, None))
        status, err = self.json("POST", "/api/photo/jobs", {"prompt": "p", "mode": "img2img", "source": body["id"]})
        self.assertEqual(status, 400, err)

    def test_cross_origin_post_rejected(self):
        status, _ = self.json("POST", "/api/photo/jobs", {"prompt": "p"}, {"Origin": "https://evil.example"})
        self.assertEqual(status, 403)


class FailureTest(ServerCase):
    fail_flag = "1"

    def test_failed_run_reports_last_message(self):
        for model in ("z-image-turbo", "flux2-klein-9b"):
            with self.subTest(model):
                _, job = self.json("POST", "/api/photo/jobs", {"prompt": "p", "model": model, "size": "512x512"})
                failed, _ = self.wait(job["id"])
                self.assertEqual(failed["status"], "failed")
                self.assertIn("кодом 1", failed["error"])
                self.assertIn("fake failure requested", failed["error"])
                self.assertIsNone(failed["image_url"])


class CancelTest(ServerCase):
    step_delay = "0.5"

    def test_cancel_running_and_queued(self):
        _, running = self.json("POST", "/api/photo/jobs", {"prompt": "первая", "size": "512x512"})
        _, queued = self.json("POST", "/api/photo/jobs", {"prompt": "вторая", "size": "512x512"})
        self.wait(running["id"], statuses=("running",))
        _, view = self.json("GET", f"/api/photo/jobs/{queued['id']}")
        self.assertEqual((view["status"], view["queue_position"]), ("queued", 1))

        _, view = self.json("POST", f"/api/photo/jobs/{queued['id']}/cancel")
        self.assertEqual(view["status"], "canceled")
        started = time.time()
        self.json("POST", f"/api/photo/jobs/{running['id']}/cancel")
        canceled, _ = self.wait(running["id"])
        self.assertEqual(canceled["status"], "canceled")
        self.assertLess(time.time() - started, 5, "отмена должна убивать процесс, а не ждать его")
        self.assertFalse((Path(self.tmp.name) / "photo" / "jobs" / running["id"] / "output.png").exists())


class GpuGateTest(ServerCase):
    def test_job_waits_while_gpu_is_busy(self):
        gate = self.app.ctx.gate
        with gate.hold("Видео"):
            _, job = self.json("POST", "/api/photo/jobs", {"prompt": "кот", "size": "512x512"})
            waiting = self.poll(f"/api/photo/jobs/{job['id']}", lambda j: "Ждёт GPU" in j["progress"]["stage"])
            self.assertEqual(waiting["status"], "running")
            self.assertIn("Видео", waiting["progress"]["stage"])
            self.assertEqual(self.json("GET", "/api/status")[1]["gpu"], "Видео")
        done, _ = self.wait(job["id"])
        self.assertEqual(done["status"], "done")

    def test_cancel_while_waiting_for_gpu(self):
        with self.app.ctx.gate.hold("Аудио"):
            _, job = self.json("POST", "/api/photo/jobs", {"prompt": "кот", "size": "512x512"})
            self.poll(f"/api/photo/jobs/{job['id']}", lambda j: "Ждёт GPU" in j["progress"]["stage"])
            self.json("POST", f"/api/photo/jobs/{job['id']}/cancel")
            canceled, _ = self.wait(job["id"])
        self.assertEqual(canceled["status"], "canceled")


class RestartRecoveryTest(unittest.TestCase):
    def test_unfinished_jobs_are_marked_failed_on_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "photo" / "jobs" / "abcdef012345"
            folder.mkdir(parents=True)
            job = {
                "id": "abcdef012345", "status": "running", "created_at": 1.0, "started_at": 2.0,
                "finished_at": None, "progress": {"stage": "x", "step": 1, "steps": 8, "percent": 20.0},
                "error": None, "log": [],
            }  # fmt: skip
            (folder / "job.json").write_text(json.dumps(job), encoding="utf-8")
            config = platform_config.load({"AIP_BACKEND": "fake", "AIP_DATA_DIR": tmp})
            app = server_module.App(config, photo_only)
            self.addCleanup(app.close)
            loaded = app.modules["photo"].jobs.get("abcdef012345")
            self.assertEqual(loaded["status"], "failed")
            self.assertIn("перезапущен", loaded["error"])


class ConfigTest(unittest.TestCase):
    def test_empty_settings_fall_back_to_defaults(self):
        # make экспортирует переменные пустыми, пока нет local.mk.
        cfg = load({"PHOTOGEN_MFLUX_BIN": "", "PHOTOGEN_FLUX2_CMD": "", "PHOTOGEN_MODELS_DIR": ""})
        self.assertEqual((cfg.flux2_cmd, cfg.mflux_bin, cfg.backend), (("Flux2CLI",), None, "real"))
        self.assertEqual(str(cfg.models_dir), "/Volumes/External SSD/local_models")
        self.assertEqual(cfg.mflux_cmd("mflux-generate-qwen"), ("mflux-generate-qwen",))

    def test_pipelines_come_from_setup(self):
        cfg = load({"PHOTOGEN_MFLUX_BIN": "/env/bin", "PHOTOGEN_FLUX2_CMD": "/opt/Flux2CLI"})
        self.assertEqual(cfg.mflux_cmd("mflux-generate-krea2"), ("/env/bin/mflux-generate-krea2",))
        self.assertEqual(cfg.flux2(), ("/opt/Flux2CLI",))

    def test_not_installed_model_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = load({"AIP_DATA_DIR": tmp, "PHOTOGEN_MODELS_DIR": str(Path(tmp) / "models")})
            self.assertFalse(any(m.installed for m in cfg.models()))
            with self.assertRaisesRegex(params.ValidationError, r"make models M=krea-2"):
                params.build_spec({"prompt": "кот", "model": "krea-2"}, cfg.models(), lambda _: None)


if __name__ == "__main__":
    unittest.main()
