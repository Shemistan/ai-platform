"""Моушн-вставки монтажа (PRD-022): разбор ответа LLM, композиции HyperFrames, шаг в конвейере на фейках."""

import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from aiplatform.core import media
from aiplatform.montage import motion
from aiplatform.montage.config import MontageConfig
from aiplatform.montage.service import MontageModule
from tests.fixtures import FAST, NEEDS_FFMPEG
from tests.helpers import PlatformCase


def insert(at, template="lower_third", duration=4, **fields):
    return {"at": at, "duration": duration, "template": template, "fields": fields or {"title": "Video Use"}}


class CleanTest(unittest.TestCase):
    def test_rules_of_placement(self):
        data = {
            "inserts": [
                insert(1.0),  # слишком рано
                insert(10.0),
                insert(20.0),  # теснее 25 с к предыдущей
                insert(40.0, "list", title="Инструменты", items=["Video Use", "HyperFrames", "", "ffmpeg", "x", "y"]),
                insert(70.0, "list", title="Мало", items=["один"]),  # меньше двух пунктов
                insert(80.0, "stat", duration=30, value="20 000+ звёзд на GitHub", label="у HyperFrames"),
                insert(110.0, "unknown"),
                insert(117.0),  # не помещается до конца ролика
                "мусор",
            ]
        }
        out = motion.clean_inserts(data, 120.0)
        self.assertEqual(
            [(i["at"], i["template"]) for i in out], [(10.0, "lower_third"), (40.0, "list"), (80.0, "stat")]
        )
        self.assertEqual(out[1]["fields"]["items"], ["Video Use", "HyperFrames", "ffmpeg"])
        self.assertEqual(out[2]["duration"], motion.MAX_S)
        self.assertEqual(out[2]["fields"]["value"], "20 000+…")
        self.assertEqual(motion.clean_inserts([], 100), [])
        self.assertEqual(motion.clean_inserts({"inserts": [insert(10, title="")]}, 100), [], "без текста — нет вставки")

    def test_composition_is_a_hyperframes_document(self):
        item = insert(10.0, "title", title="Монтаж <без> «ручной» работы", kicker="Часть 2")
        document = motion.composition(item, "/* css */")
        self.assertIn('id="stage" data-composition-id="insert" data-start="0" data-duration="4"', document)
        self.assertIn('class="clip" data-start="0" data-duration="4"', document)
        self.assertIn("gsap.timeline({ paused: true })", document)
        self.assertIn("window.__timelines.insert = tl;", document)
        self.assertIn("Монтаж &lt;без&gt; «ручной» работы", document)
        self.assertIn("xPercent: -50", document)
        self.assertEqual(motion.first_text(document), "Часть 2 Монтаж <без> «ручной» работы")
        kinds = (
            ("lower_third", {"title": "A", "subtitle": "B"}),
            ("stat", {"value": "9", "label": "x"}),
            ("list", {"title": "L", "items": ["a", "b"]}),
            ("quote", {"text": "q", "author": ""}),
        )
        for kind, fields in kinds:
            with self.subTest(kind=kind):
                self.assertIn('class="card', motion.composition(insert(5, kind, **fields), ""))

    def test_render_argv_and_missing_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = MontageConfig(Path(tmp), "mlx", Path("/x"), Path("/m"), "ffmpeg", "", ("claude",),
                                ("/nonexistent/node", "/nonexistent/hf.mjs"), 2**30)  # fmt: skip
            argv = motion.render_argv(cfg, Path(tmp))
            self.assertEqual(argv[:4], ["/nonexistent/node", "/nonexistent/hf.mjs", "render", "--composition"])
            self.assertEqual(argv[argv.index("--format") + 1], "mov")
            overlays, info = motion.run(cfg, {}, Path(tmp), [], 60, threading.Event(), ask=None, note=print,
                                        progress=lambda *_: None)  # fmt: skip
            self.assertEqual(overlays, [])
            self.assertIn("make models M=hyperframes", info["skipped"])


def montage_only(ctx):
    return [MontageModule(ctx)]


@NEEDS_FFMPEG
class MotionPipelineTest(PlatformCase):
    modules = staticmethod(montage_only)
    process_env = FAST

    def test_inserts_are_rendered_and_overlaid(self):
        with tempfile.TemporaryDirectory() as tmp:
            head = Path(tmp) / "head.mp4"
            video = "testsrc=size=640x360:rate=30:duration=45"
            subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", video,
                            "-f", "lavfi", "-i", "sine=frequency=330:duration=45", "-shortest", "-c:v", "libx264",
                            "-pix_fmt", "yuv420p", "-c:a", "aac", str(head)], check=True)  # fmt: skip
            # reviewed без раскадровки: быстрые вставки по расшифровке (autonomous + motion идёт через раскадровку).
            options = {"mode": "reviewed", "check": False, "motion": True}
            _, project = self.json("POST", "/api/montage/projects", {"title": "С графикой", "options": options})
            pid = project["id"]
            status, _ = self.json("POST", f"/api/montage/projects/{pid}/sources/head", head.read_bytes())
            self.assertEqual(status, 201)
        self.json("POST", f"/api/montage/projects/{pid}/start")
        self.poll(f"/api/montage/projects/{pid}", lambda p: p["status"] in ("review", "failed"), timeout=180)
        self.assertEqual(self.json("POST", f"/api/montage/projects/{pid}/approve", {"gate": "edl"})[0], 200)
        self.assertEqual(self.json("POST", f"/api/montage/projects/{pid}/render")[0], 200)
        done = self.poll(f"/api/montage/projects/{pid}", lambda p: p["status"] in ("done", "failed"), timeout=180)
        self.assertEqual(done["status"], "done", done)
        self.assertGreaterEqual(done["motion"]["inserts"], 1, done["motion"])
        self.assertEqual(done["motion"]["items"][0]["template"], "lower_third")
        folder = Path(self.tmp.name) / "montage" / "projects" / pid
        self.assertTrue((folder / "motion" / "01" / "index.html").is_file())
        self.assertTrue((folder / "motion" / "01" / "insert.mov").is_file())
        self.assertAlmostEqual(media.duration(folder / "output.mp4"), done["output"]["duration"], delta=0.3)


if __name__ == "__main__":
    unittest.main()
