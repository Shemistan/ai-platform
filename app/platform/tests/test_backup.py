"""Бэкап и восстановление (REL-012): состав архива, шифрование age (фейк), ротация, restore, /api/ready."""

import json
import os
import tarfile
import tempfile
import time
import unittest
from pathlib import Path

from aiplatform import backup
from aiplatform import config as config_module
from tests import TMP_ROOT
from tests.helpers import PlatformCase, isolated_config

# Фейковый age: шифрование — дописать заголовок, расшифровка (-d) — снять его. Ключ проверяется.
FAKE_AGE = """#!/bin/sh
case "$1" in
-d) [ "$2" = "-i" ] && [ -f "$3" ] || { echo "нет ключа" >&2; exit 1; }
    tail -c +9 ;;
-r) printf 'AGEFAKE\\n'; cat ;;
*) echo "плохие аргументы: $*" >&2; exit 2 ;;
esac
"""


class BackupTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=TMP_ROOT)
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.data = self.root / "data"
        self.library = self.root / "library"
        self.out = self.root / "backups"
        age = self.root / "age"
        age.write_text(FAKE_AGE)
        age.chmod(0o755)
        self.key = self.root / "key.txt"
        self.key.write_text("AGE-SECRET-KEY-FAKE\n")
        self.env = isolated_config(
            {
                "AIP_BACKEND": "fake",
                "AIP_DATA_DIR": str(self.data),
                "NARRATOR_LIBRARY": str(self.library),
                "AIP_BACKUP_DIR": str(self.out),
                "AIP_BACKUP_AGE": str(age),
                "AIP_BACKUP_AGE_RECIPIENT": "age1fake",
                "AIP_BACKUP_AGE_IDENTITY": str(self.key),
            }
        )
        self.cfg = config_module.load(self.env)
        self.put(self.data / "settings.json", '{"notify": {"enabled": true}}')
        self.put(self.data / "channels.json", '{"channels": []}')
        project = self.data / "studio" / "projects" / "0123456789ab"
        self.put(project / "project.json", '{"id": "0123456789ab"}')
        self.put(project / "published.json", "[]")
        self.put(project / "text.md", "текст")
        self.put(project / "video.mp4", "видео")  # медиа — не в архиве
        self.put(project / "images" / "01.png", "картинка")
        self.put(self.data / "photo" / "jobs" / "x" / "job.json", "{}")  # задачи вкладок восстановимы
        self.put(self.data / "cache" / "a.json", "{}")
        self.put(self.library / "telegram.json", '{"offset": 7, "subscribers": []}')
        self.put(self.library / "news" / "news.db", "SQLite")
        self.put(self.library / "2026" / "a" / "audio.mp3", "звук")
        self.put(self.library / "bot_token.md", "секрет")

    @staticmethod
    def put(path: Path, text: str):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def test_create_encrypts_small_files_only_and_rotates(self):
        old = self.out / f"{backup.PREFIX}20200101-000000{backup.SUFFIX}"
        self.put(old, "старый")
        os.utime(old, (time.time() - 31 * 86400,) * 2)
        result = backup.create(self.cfg)
        archive = Path(result["file"])
        self.assertTrue(archive.name.endswith(".tar.gz.age"))
        self.assertEqual(result["removed"], [old.name])
        self.assertFalse(old.exists())
        raw = archive.read_bytes()
        self.assertTrue(raw.startswith(b"AGEFAKE\n"), "архив прошёл через age")
        with tarfile.open(fileobj=__import__("io").BytesIO(raw[8:]), mode="r:gz") as tar:
            names = set(tar.getnames())
        self.assertIn("data/settings.json", names)
        self.assertIn("data/studio/projects/0123456789ab/project.json", names)
        self.assertIn("data/studio/projects/0123456789ab/published.json", names)
        self.assertIn("data/studio/projects/0123456789ab/text.md", names)
        self.assertIn("narrator-library/telegram.json", names)
        self.assertIn("narrator-library/news/news.db", names)
        for name in names:
            self.assertFalse(name.endswith((".mp4", ".png", ".mp3")), name)
            self.assertNotIn("bot_token", name)
            self.assertFalse(name.startswith(("data/photo/", "data/cache/")), name)
        stamp = json.loads((self.data / backup.STAMP).read_text(encoding="utf-8"))
        self.assertEqual(stamp["file"], str(archive))

    def test_restore_into_empty_dir_brings_back_same_data(self):
        archive = Path(backup.create(self.cfg)["file"])
        target = self.root / "restored"
        backup.restore(archive, target, self.env)
        self.assertEqual(
            (target / "narrator-library" / "telegram.json").read_text(encoding="utf-8"),
            '{"offset": 7, "subscribers": []}',
        )
        # Платформа на восстановленных данных видит тот же выпуск и те же настройки.
        restored = config_module.load(isolated_config({"AIP_BACKEND": "fake", "AIP_DATA_DIR": str(target / "data")}))
        self.assertTrue(restored.settings.load()["notify"]["enabled"])
        self.assertTrue((restored.data_dir / "studio" / "projects" / "0123456789ab" / "project.json").is_file())
        with self.assertRaisesRegex(backup.BackupError, "не пустой"):
            backup.restore(archive, target, self.env)

    def test_clear_errors_without_dir_or_key(self):
        cfg = config_module.load({**self.env, "AIP_BACKUP_DIR": ""})
        with self.assertRaisesRegex(backup.BackupError, "AIP_BACKUP_DIR"):
            backup.create(cfg)
        cfg = config_module.load({**self.env, "AIP_BACKUP_AGE_RECIPIENT": ""})
        with self.assertRaisesRegex(backup.BackupError, "AIP_BACKUP_AGE_RECIPIENT"):
            backup.create(cfg)

    def test_status_age(self):
        self.assertEqual(backup.status(self.data, {})["ok"], True)  # не настроен — не мешает готовности
        self.assertEqual(backup.status(self.data, {"AIP_BACKUP_DIR": "x"})["ok"], False)
        backup.create(self.cfg)
        fresh = backup.status(self.data, self.env)
        self.assertTrue(fresh["ok"])
        self.assertLess(fresh["age_h"], 1)
        stale = backup.status(self.data, self.env, now=time.time() + 3 * 86400)
        self.assertFalse(stale["ok"])
        self.assertIn("72 ч назад", stale["detail"])


class DataDirTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=TMP_ROOT)
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name) / "home"
        self.legacy = Path(tmp.name) / "checkout" / "data"

    def test_default_outside_working_copy_with_legacy_fallback(self):
        env = {"HOME": str(self.home)}
        new = self.home / "Library" / "Application Support" / "aiplatform"
        self.assertEqual(config_module.default_data_dir(env, self.legacy), new)  # ничего нет — новый путь
        self.legacy.mkdir(parents=True)
        self.assertEqual(config_module.default_data_dir(env, self.legacy), self.legacy)  # до переноса — старый
        new.mkdir(parents=True)
        self.assertEqual(config_module.default_data_dir(env, self.legacy), new)

    def test_migrate_copies_and_keeps_old(self):
        (self.legacy / "studio").mkdir(parents=True)
        (self.legacy / "settings.json").write_text("{}")
        (self.legacy / "studio" / "schedule.json").write_text("{}")
        target = backup.migrate({"HOME": str(self.home)}, legacy=self.legacy)
        self.assertTrue((target / "studio" / "schedule.json").is_file())
        self.assertTrue((self.legacy / "settings.json").is_file(), "старое не удаляется")
        with self.assertRaisesRegex(backup.BackupError, "не пустой"):
            backup.migrate({"HOME": str(self.home)}, legacy=self.legacy)

    def test_migrate_to_own_dir_copies_and_checks(self):
        (self.legacy / "montage" / "projects" / "p1").mkdir(parents=True)
        (self.legacy / "settings.json").write_text("{}")
        (self.legacy / "montage" / "projects" / "p1" / "output.mp4").write_bytes(b"\0" * 1024)
        target = backup.migrate({"HOME": str(self.home)}, legacy=self.legacy, target=self.home / "ssd" / "data")
        self.assertEqual((target / "montage" / "projects" / "p1" / "output.mp4").stat().st_size, 1024)
        self.assertTrue((self.legacy / "settings.json").is_file(), "старое не удаляется")
        with self.assertRaisesRegex(backup.BackupError, "не пустой"):
            backup.migrate({"HOME": str(self.home)}, legacy=self.legacy, target=target)
        with self.assertRaisesRegex(backup.BackupError, "внутри"):
            backup.migrate({"HOME": str(self.home)}, legacy=self.legacy, target=self.legacy / "copy")

    def test_migrate_refuses_missing_volume(self):
        self.legacy.mkdir(parents=True)
        with self.assertRaisesRegex(backup.BackupError, "не подключён"):
            backup.migrate({}, legacy=self.legacy, target=Path("/Volumes/aip-test-absent-disk/data"))

    def test_data_dir_on_missing_volume_refuses_start(self):
        with self.assertRaisesRegex(config_module.ConfigError, "не подключён"):
            config_module.load({"AIP_DATA_DIR": "/Volumes/aip-test-absent-disk/aiplatform-data"})
        self.assertIsNone(config_module.missing_volume(self.home / "data"))

    def test_tests_refuse_user_data_dir(self):
        with self.assertRaisesRegex(RuntimeError, "боевым каталогом данных"):
            config_module.load({"AIP_DATA_DIR": str(config_module.user_data_dir(os.environ))})


class ReadyBackupTest(PlatformCase):
    def test_ready_shows_backup_age(self):
        (Path(self.tmp.name) / backup.STAMP).write_text(
            json.dumps({"at": time.time() - 3600, "file": "/x/aiplatform-1.tar.gz.age"}), encoding="utf-8"
        )
        _, body = self.json("GET", "/api/ready")
        check = next(c for c in body["checks"] if c["name"] == "backup")
        self.assertTrue(check["ok"], check)
        self.assertIn("1 ч назад", check["detail"])


if __name__ == "__main__":
    unittest.main()
