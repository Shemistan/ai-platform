"""Журнал комплаенс-действий audit.jsonl: публикация, реестр, голос, ротация (CMP-015)."""

import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from aiplatform.core import audit
from aiplatform.updates import registry
from tests.helpers import PlatformCase
from tests.test_audio import AudioServerCase
from tests.test_studio_flow import NEEDS_FFMPEG, StudioCase

META_POST = "**Очки Meta**\n\nКомпания Meta показала очки — [подробнее](https://example.dev/q)"
META_NOTE = (
    "Компания Meta (продукты Facebook и Instagram) признана экстремистской организацией и запрещена на территории РФ."
)


class AuditLogTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.log = audit.AuditLog(Path(tmp.name) / audit.FILE)

    def test_append_only_0600(self):
        self.log.write("publish", channel="tg", message_id=1)
        first = self.log.path.read_text(encoding="utf-8")
        self.log.write("delete", project_id="x")
        self.assertTrue(self.log.path.read_text(encoding="utf-8").startswith(first), "строки только дописываются")
        self.assertEqual(oct(os.stat(self.log.path).st_mode & 0o777), "0o600")
        self.assertEqual([r["action"] for r in self.log.read()], ["publish", "delete"])
        self.assertEqual(self.log.read()[0]["actor"], "local")

    def test_rotation_moves_year_old(self):
        now = time.time()
        old = {"ts": "2025-01-01T00:00:00", "at": now - 400 * 86400, "actor": "local", "action": "publish"}
        self.log.path.write_text(json.dumps(old) + "\n", encoding="utf-8")
        self.log.write("publish", channel="tg")
        self.assertEqual(self.log.rotate(now), 1)
        self.assertEqual(len(self.log.read()), 1)
        archive = self.log.path.with_name(audit.ARCHIVE)
        self.assertEqual(json.loads(archive.read_text(encoding="utf-8"))["at"], old["at"])
        self.assertEqual(self.log.rotate(now), 0)

    def test_footnotes(self):
        self.assertEqual(audit.footnotes(f"Пост.\n\n* {META_NOTE}\n"), [META_NOTE])


class RegistryAuditTest(PlatformCase):
    def test_registry_apply_and_edit_are_logged(self):
        log = audit.AuditLog(Path(self.tmp.name) / audit.FILE)
        self.json("POST", "/api/updates/registry/check")
        self.poll("/api/updates/state", lambda b: b["registry"]["check"].get("status") in ("done", "failed"))
        self.assertEqual(self.json("POST", "/api/updates/registry/apply")[0], 200)
        entries = registry.Registry(registry.DEFAULT_FILE).entries()[:1]
        text = json.dumps({"entries": [e.stored() for e in entries]})
        self.assertEqual(self.json("POST", "/api/updates/registry", {"text": text})[0], 200)
        applied, edited = [r for r in log.read() if r["action"] == "registry_apply"]
        self.assertEqual((applied["how"], applied["diff"]["added"]), ("proposal", ["demo-corp"]))
        self.assertEqual((edited["how"], edited["entries"]), ("edit", 1))
        self.assertEqual(len(edited["sha256"]), 64)
        self.assertTrue(applied["registry"])


class VoiceAuditTest(AudioServerCase):
    def test_voice_record_is_logged(self):
        log = audit.AuditLog(Path(self.tmp.name) / audit.FILE)
        self.json("GET", "/api/audio/voices")  # фейковый narrator раскладывает образцы
        wav = (self.config.data_dir / "fake-narrator" / "voices" / "m1.wav").read_bytes()
        status, _, raw = self.call("POST", "/api/audio/uploads", wav, {"Content-Type": "audio/wav"})
        self.assertEqual(status, 201, raw)
        body = {"upload": json.loads(raw)["id"], "text": "Мой голос."}
        status, task = self.json("POST", "/api/audio/voices/shem", body)
        self.assertEqual(status, 202, task)
        self.poll(f"/api/audio/tasks/{task['id']}", lambda t: t["status"] not in ("queued", "running"))
        deadline = time.time() + 5
        while not log.read() and time.time() < deadline:
            time.sleep(0.05)
        (record,) = log.read()
        self.assertEqual((record["action"], record["stem"], record["source"]), ("voice_record", "shem", "upload"))
        self.assertEqual(len(record["upload_sha256"]), 64)
        self.assertEqual(record["text_sha256"], audit.sha256_text("Мой голос."))


@NEEDS_FFMPEG
class PublishAuditTest(StudioCase):
    def test_publish_logs_caption_hash_and_footnotes(self):
        settings = {"channels": ["download-16x9"], "auto_publish": True, "branding": False}
        project = self.create(scenario="post", post=META_POST, settings=settings, run=True)
        pid = project["id"]
        self.settle(pid)
        from tests.test_compliance import PostNotesFlowTest

        PostNotesFlowTest.upload(self, pid)
        done = self.settle(pid)
        self.assertEqual(self.statuses(done)["publish"], "done", done["log"])
        (record,) = [r for r in audit.AuditLog(Path(self.tmp.name) / audit.FILE).read() if r["action"] == "publish"]
        self.assertEqual((record["project_id"], record["channel"]), (pid, "download-16x9"))
        self.assertEqual(len(record["caption_sha256"]), 64)
        self.assertEqual(record["footnotes"], [META_NOTE])
        self.assertEqual(record["registry"], registry.Registry(registry.DEFAULT_FILE).read()[1])
        self.assertTrue(all(len(h) == 64 for h in record["media"].values()), record["media"])
