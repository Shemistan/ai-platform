"""Версия схемы файлов данных, миграции и карантин битых записей (BE-008, ADR 0009)."""

import json
import tempfile
import unittest
from pathlib import Path

from aiplatform import config as config_module
from aiplatform import server as server_module
from aiplatform.core import schema
from aiplatform.core import settings as settings_module
from aiplatform.studio import publish, steps
from aiplatform.studio.pipeline import PROJECT_SCHEMA
from tests.helpers import PlatformCase, isolated_config


class SchemaUnitTest(unittest.TestCase):
    def test_upgrade_chain_and_stamp(self):
        migrations = {0: lambda d: d | {"b": 1}, 1: lambda d: d | {"c": d["b"] + 1}}
        self.assertEqual(schema.upgrade({"a": 0}, 2, migrations), {"a": 0, "b": 1, "c": 2, "schema": 2})
        self.assertEqual(schema.upgrade({"schema": 2, "x": 1}, 2, migrations), {"schema": 2, "x": 1})
        self.assertEqual(list(schema.stamp({"x": 1, "schema": 0}, 3)), ["schema", "x"])
        with self.assertRaises(schema.TooNew):
            schema.upgrade({"schema": 5}, 2)
        with self.assertRaises(schema.Broken):
            schema.upgrade({"schema": "два"}, 2)

    def test_wrap_unwrap(self):
        self.assertEqual(schema.unwrap([1], "records"), [1])
        self.assertEqual(schema.unwrap(schema.wrap([1], "records", 1), "records"), [1])
        with self.assertRaises(schema.Broken):
            schema.unwrap({"schema": 1}, "records")

    def test_load_record_quarantine_and_too_new(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            check = schema.require("id")
            good, bad, newer = folder / "a.json", folder / "b.json", folder / "c.json"
            good.write_text('{"id": "a"}')
            bad.write_text('{"name": "без id"}')
            newer.write_text('{"id": "c", "schema": 9}')
            problems: list[str] = []
            loaded = schema.load_record(good, 1, check=check, problems=problems)
            self.assertEqual(loaded, ({"id": "a", "schema": 1}, True))
            self.assertEqual(schema.load_record(bad, 1, check=check, problems=problems), (None, False))
            self.assertFalse(bad.exists())
            self.assertTrue((folder / "b.json.bad").exists())
            self.assertEqual(schema.load_record(newer, 1, check=check, problems=problems), (None, False))
            self.assertEqual(newer.read_text(), '{"id": "c", "schema": 9}', "файл новее кода не тронут")
            self.assertEqual(len(problems), 2)
            self.assertIn("битая", problems[0])
            self.assertIn("более новой версией", problems[1])


class ListFilesTest(unittest.TestCase):
    def test_published_reads_old_list_and_writes_wrapper(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / steps.PUBLISHED).write_text(json.dumps([{"channel": "tg", "ok": True}]))
            self.assertEqual(steps.load_published(folder), [{"channel": "tg", "ok": True}])
            steps.update_published(folder, lambda records: [*records, {"channel": "yt"}])
            saved = json.loads((folder / steps.PUBLISHED).read_text())
            self.assertEqual(saved["schema"], steps.PUBLISHED_SCHEMA)
            self.assertEqual([r["channel"] for r in steps.load_published(folder)], ["tg", "yt"])

    def test_published_newer_is_read_but_not_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            newer = json.dumps({"schema": 99, "records": [{"channel": "tg"}]})
            (folder / steps.PUBLISHED).write_text(newer)
            self.assertEqual(steps.load_published(folder), [{"channel": "tg"}])
            with self.assertRaises(schema.TooNew):
                steps.update_published(folder, lambda records: [])
            self.assertEqual((folder / steps.PUBLISHED).read_text(), newer)

    def test_channels_old_list_and_wrapper(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "channels.json"
            path.write_text(json.dumps([{"id": "tg", "type": "telegram", "chat_id": "@x"}]))
            channels = publish.load_channels(path)
            publish.save_channels(path, channels)
            saved = json.loads(path.read_text())
            self.assertEqual((saved["schema"], saved["channels"][0]["id"]), (publish.CHANNELS_SCHEMA, "tg"))
            self.assertEqual([c.id for c in publish.load_channels(path)], ["tg"])

    def test_unwrap_for_rollback(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            folder = data / "studio" / "projects" / "0123456789ab"
            folder.mkdir(parents=True)
            steps.save_published(folder, [{"channel": "tg"}])
            publish.save_channels(data / "channels.json", [])
            self.assertEqual(schema.main(["unwrap", tmp]), 0)
            self.assertEqual(json.loads((folder / steps.PUBLISHED).read_text()), [{"channel": "tg"}])
            self.assertEqual(json.loads((data / "channels.json").read_text()), [])
            self.assertEqual(schema.unwrap_lists(data), [], "повтор ничего не меняет")


class SettingsSchemaTest(unittest.TestCase):
    def test_write_stamps_and_newer_file_is_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = settings_module.Settings(Path(tmp) / "settings.json")
            store.change(lambda data: data["stats"].update(auto=False))
            self.assertEqual(json.loads(store.path.read_text())["schema"], settings_module.SCHEMA)
            newer = json.dumps({"schema": 99, "stats": {"auto": False}})
            store.path.write_text(newer)
            self.assertFalse(store.load()["stats"]["auto"], "читается как есть")
            self.assertIn("более новой версией", store.problem)
            with self.assertRaises(schema.TooNew):
                store.change(lambda data: None)
            self.assertEqual(store.path.read_text(), newer)


class StartupQuarantineTest(unittest.TestCase):
    """Битые записи не мешают старту: остальное грузится, причина — в problems."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data = Path(tmp.name)
        self.config = config_module.load(
            isolated_config({"AIP_BACKEND": "fake", "AIP_DATA_DIR": tmp.name, "AIP_PORT": "0"})
        )

    def start(self):
        httpd, app = server_module.make_server(self.config, server_module.default_modules)
        self.addCleanup(httpd.server_close)
        self.addCleanup(app.close)
        return app

    def test_broken_and_old_projects_and_jobs(self):
        app = self.start()
        studio_root = app.modules["studio"].studio.root
        jobs_dir = app.modules["photo"].config.jobs_dir
        app.close()
        old = {
            "id": "0123456789ab",
            "scenario": "post",
            "title": "Старый",
            "created_at": 1.0,
            "running": False,
            "settings": {},
            "steps": {"text": {"status": "done"}},
        }
        for pid, body in (
            ("0123456789ab", json.dumps(old)),  # схема 0: без schema
            ("bbbbbbbbbbbb", json.dumps({"id": "bbbbbbbbbbbb", "title": "без steps"})),
            ("cccccccccccc", "{не json"),
            ("dddddddddddd", json.dumps(old | {"id": "dddddddddddd", "schema": PROJECT_SCHEMA + 1})),
        ):
            (studio_root / pid).mkdir(parents=True)
            (studio_root / pid / "project.json").write_text(body)
        (jobs_dir / "eeeeeeeeeeee").mkdir(parents=True)
        (jobs_dir / "eeeeeeeeeeee" / "job.json").write_text('{"id": "eeeeeeeeeeee"}')

        app = self.start()
        studio = app.modules["studio"].studio
        self.assertEqual([p["id"] for p in studio.list()], ["0123456789ab"])
        self.assertTrue((studio_root / "bbbbbbbbbbbb" / "project.json.bad").exists())
        self.assertTrue((studio_root / "cccccccccccc" / "project.json.bad").exists())
        self.assertTrue((studio_root / "dddddddddddd" / "project.json").exists(), "новее кода — не тронут")
        self.assertTrue((jobs_dir / "eeeeeeeeeeee" / "job.json.bad").exists())
        self.assertEqual(app.modules["photo"].jobs.list(), [])
        problems = "\n".join(app.problems())
        for needle in ("Выпуск bbbbbbbbbbbb", "Выпуск cccccccccccc", "Выпуск dddddddddddd", "Задача eeeeeeeeeeee"):
            self.assertIn(needle, problems)
        # Старый выпуск получает schema при первой записи.
        studio.update("0123456789ab", lambda p: set())
        saved = json.loads((studio_root / "0123456789ab" / "project.json").read_text())
        self.assertEqual(saved["schema"], PROJECT_SCHEMA)


class SettingsHttpTest(PlatformCase):
    def test_newer_settings_is_409(self):
        (self.config.data_dir / "settings.json").write_text(json.dumps({"schema": 99}))
        status, body = self.json("POST", "/api/settings/toggle", {"kind": "photo", "id": "x", "enabled": True})
        self.assertIn(status, (400, 409), body)
        status, body = self.json("POST", "/api/settings/section/stats", {"auto": False})
        self.assertEqual((status, body["code"]), (409, "schema_too_new"), body)


if __name__ == "__main__":
    unittest.main()
