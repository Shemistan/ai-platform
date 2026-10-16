"""Вкладка «Настройки»: выключенные модели, движки и голоса пропадают из выбора и не принимаются."""

import json
import unittest

from aiplatform.core import settings as settings_module
from tests.helpers import PlatformCase


class SettingsStoreTest(unittest.TestCase):
    def test_normalize_drops_garbage(self):
        data = settings_module.normalize(
            {"disabled": {"photo": ["a", 1, "a"], "video": "x", "voices": {"higgs": ["M1"], "fish": [], 3: ["y"]}}}
        )
        self.assertEqual(data["disabled"]["photo"], ["a"])
        self.assertEqual(data["disabled"]["video"], [])
        self.assertEqual(data["disabled"]["voices"], {"higgs": ["M1"]})
        self.assertEqual(settings_module.normalize("мусор")["disabled"]["audio"], [])


class SettingsTabTest(PlatformCase):
    def toggle(self, **body):
        return self.json("POST", "/api/settings/toggle", body)

    def test_state_lists_full_catalog(self):
        status, body = self.json("GET", "/api/settings/state")
        self.assertEqual(status, 200, body)
        photo = {m["id"]: m for m in body["sections"]["photo"]}
        self.assertTrue(photo["z-image-turbo"]["enabled"])
        self.assertTrue(photo["z-image-turbo"]["installed"])
        self.assertIn("ltx-2.5-q4", [m["id"] for m in body["sections"]["video"]])
        higgs = next(e for e in body["sections"]["audio"] if e["id"] == "higgs")
        self.assertEqual(higgs["default_voice"], "Charon")
        self.assertTrue(all(v["enabled"] for v in higgs["voices"]))
        self.assertIn("settings", [t["id"] for t in self.json("GET", "/api/config")[1]["tabs"]])

    def test_photo_model_disabled(self):
        _, body = self.toggle(kind="photo", id="z-image-turbo", enabled=False)
        photo = {m["id"]: m for m in body["sections"]["photo"]}
        self.assertFalse(photo["z-image-turbo"]["enabled"])
        self.assertTrue(photo["z-image-turbo"]["installed"])  # каталог показывает и выключенное

        _, cfg = self.json("GET", "/api/photo/config")
        self.assertNotIn("z-image-turbo", [m["id"] for m in cfg["models"]])
        status, err = self.json("POST", "/api/photo/jobs", {"prompt": "кот", "model": "z-image-turbo"})
        self.assertEqual(status, 400)
        self.assertIn("выключена", err["error"])
        _, studio = self.json("GET", "/api/studio/config")
        self.assertNotIn("z-image-turbo", [m["id"] for m in studio["image_models"]])

        self.toggle(kind="photo", id="z-image-turbo", enabled=True)
        _, cfg = self.json("GET", "/api/photo/config")
        self.assertIn("z-image-turbo", [m["id"] for m in cfg["models"]])

    def test_mode_without_models(self):
        # Расширять кадр умеет только Klein: выключили — у режима нет модели, но конфиг жив.
        self.toggle(kind="photo", id="flux2-klein-9b", enabled=False)
        status, cfg = self.json("GET", "/api/photo/config")
        self.assertEqual(status, 200, cfg)
        self.assertIsNone(cfg["default_models"]["outpaint"])

    def test_all_video_disabled(self):
        for model in self.json("GET", "/api/settings/state")[1]["sections"]["video"]:
            self.toggle(kind="video", id=model["id"], enabled=False)
        status, cfg = self.json("GET", "/api/video/config")
        self.assertEqual(status, 200, cfg)
        self.assertEqual((cfg["models"], cfg["default_model"]), ([], None))
        status, err = self.json("POST", "/api/video/jobs", {"prompt": "море"})
        self.assertEqual(status, 400)
        self.assertIn("Настройки", err["error"])
        self.assertTrue(any("модели видео" in p for p in self.json("GET", "/api/config")[1]["problems"]))
        _, studio = self.json("GET", "/api/studio/config")
        self.assertIsNone(studio["defaults"]["video_model"])

    def test_engine_and_voice_disabled(self):
        self.toggle(kind="voice", engine="higgs", id="Charon", enabled=False)
        _, cfg = self.json("GET", "/api/audio/config")
        higgs = next(e for e in cfg["engines"] if e["id"] == "higgs")
        self.assertNotIn("Charon", higgs["voices"])
        self.assertNotEqual(higgs["default_voice"], "Charon")
        status, err = self.json("POST", "/api/audio/jobs", {"text": "Привет.", "engine": "higgs", "voice": "Charon"})
        self.assertEqual(status, 400)
        self.assertIn("Голос Charon", err["error"])

        self.toggle(kind="audio", id="higgs", enabled=False)
        _, cfg = self.json("GET", "/api/audio/config")
        self.assertNotIn("higgs", [e["id"] for e in cfg["engines"]])
        self.assertNotEqual(cfg["default_engine"], "higgs")
        status, err = self.json("POST", "/api/audio/jobs", {"text": "Привет.", "engine": "higgs"})
        self.assertEqual(status, 400)
        self.assertIn("выключен", err["error"])
        _, studio = self.json("GET", "/api/studio/config")
        self.assertNotIn("higgs", [e["id"] for e in studio["engines"]])
        self.assertNotEqual(studio["defaults"]["engine"], "higgs")

    def test_bad_toggles(self):
        for body in (
            {"kind": "photo", "id": "nope", "enabled": False},
            {"kind": "photo", "id": "z-image-turbo", "enabled": "нет"},
            {"kind": "voice", "engine": "higgs", "id": "nope", "enabled": False},
            {"kind": "voice", "engine": "nope", "id": "M1", "enabled": False},
            {"kind": "что-то", "id": "x", "enabled": False},
        ):
            with self.subTest(body=body):
                self.assertEqual(self.toggle(**body)[0], 400)

    def test_manual_edit_is_picked_up(self):
        path = self.config.data_dir / "settings.json"
        path.write_text(json.dumps({"disabled": {"video": ["wan-2.2-t2v-q4"]}}), encoding="utf-8")
        _, cfg = self.json("GET", "/api/video/config")
        self.assertNotIn("wan-2.2-t2v-q4", [m["id"] for m in cfg["models"]])
        path.write_text("{битый json", encoding="utf-8")
        _, cfg = self.json("GET", "/api/video/config")
        self.assertIn("wan-2.2-t2v-q4", [m["id"] for m in cfg["models"]])


if __name__ == "__main__":
    unittest.main()
