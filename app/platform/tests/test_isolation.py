"""Страж: тесты не видят боевой app/platform/data и диск моделей."""

import os
import unittest
from pathlib import Path

from aiplatform import config as platform_config
from aiplatform.photo import config as photo_config
from aiplatform.video import config as video_config
from tests.helpers import isolated_config


class IsolationTest(unittest.TestCase):
    def test_service_data_dirs_are_refused(self):
        for env in ({}, {"AIP_DATA_DIR": "data"}, {"AIP_DATA_DIR": "data-demo"}, {"AIP_DATA_DIR": ""}):
            with self.subTest(env=env), self.assertRaisesRegex(RuntimeError, "боевым каталогом данных"):
                platform_config.load(env)

    def test_process_env_points_away_from_service(self):
        config = platform_config.load()
        self.assertNotIn(config.data_dir, set(platform_config.SERVICE_DATA_DIRS))
        self.assertFalse(Path(os.environ["AIP_MODELS_DIR"]).exists())

    def test_isolated_config_fills_data_and_models_dirs(self):
        config = platform_config.load(isolated_config({}))
        self.assertTrue(config.data_dir.is_dir())
        self.assertNotEqual(config.data_dir, platform_config.SERVICE_DIR / "data")
        models_dir = Path(isolated_config({})["AIP_MODELS_DIR"])
        self.assertFalse(models_dir.exists())
        self.assertEqual(video_config.load(config).models_dir, models_dir)
        self.assertEqual(photo_config.load(config).models_dir, models_dir)

    def test_env_file_points_away_from_repo(self):
        path = platform_config.env_file(os.environ)
        self.assertNotEqual(path, platform_config.REPO_DIR / platform_config.ENV_FILE)
        self.assertFalse(path.exists())
        self.assertEqual(platform_config.env_file({}), platform_config.REPO_DIR / platform_config.ENV_FILE)
        self.assertTrue((platform_config.REPO_DIR / "app" / "platform").is_dir())

    def test_explicit_values_are_kept(self):
        env = isolated_config({"AIP_DATA_DIR": "/tmp/x", "PHOTOGEN_MODELS_DIR": ""})
        self.assertEqual(env, {"AIP_DATA_DIR": "/tmp/x", "PHOTOGEN_MODELS_DIR": ""})


if __name__ == "__main__":
    unittest.main()
