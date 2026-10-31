"""Вкладка «Расшифровка» (PRD-032): форматы текста, сигнатуры файлов, путь через API на фейках."""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from aiplatform.montage import asr
from aiplatform.transcribe import formats
from aiplatform.transcribe.jobs import clean_options, sniff
from aiplatform.transcribe.service import TranscribeModule
from tests.fixtures import FAST, NEEDS_FFMPEG
from tests.helpers import PlatformCase
from tests.test_montage import make_video


def w(text: str, s: float, e: float) -> dict:
    return {"w": text, "s": s, "e": e}


WORDS = [
    w("Привет,", 0.3, 0.6), w("это", 0.7, 0.9), w("обзор.", 1.0, 1.4),
    w("Начнём", 3.0, 3.4), w("с", 3.5, 3.6), w("главного.", 3.7, 4.2),  # пауза 1,6 с после конца фразы
    w("Дальше", 4.4, 4.8), w("итоги", 7.6, 8.0),  # пауза 2,8 с посреди фразы
    w("и", 8.1, 8.2), w("планы.", 8.3, 8.8),
]  # fmt: skip


class FormatsTest(unittest.TestCase):
    def test_paragraphs_break_on_pauses(self):
        paras = formats.paragraphs(WORDS)
        self.assertEqual(
            [p["text"] for p in paras], ["Привет, это обзор.", "Начнём с главного. Дальше", "итоги и планы."]
        )
        self.assertEqual((paras[1]["s"], paras[1]["e"]), (3.0, 4.8))
        self.assertEqual(formats.paragraphs([]), [])

    def test_long_monologue_is_split_at_sentence_end(self):
        words = [w(f"слово{n}" + ("." if n % 10 == 9 else ""), n * 0.4, n * 0.4 + 0.3) for n in range(300)]
        paras = formats.paragraphs(words)
        self.assertGreater(len(paras), 1)
        self.assertTrue(all(p["text"].endswith(".") for p in paras))
        self.assertTrue(all(len(p["text"].split()) <= formats.MAX_PARAGRAPH_WORDS + 10 for p in paras))

    def test_text_markdown_and_subtitles(self):
        self.assertEqual(formats.txt(WORDS), "Привет, это обзор.\n\nНачнём с главного. Дальше\n\nитоги и планы.\n")
        md = formats.md(WORDS, "Обзор")
        self.assertTrue(md.startswith("# Обзор\n\n**[0:00]** Привет, это обзор."))
        self.assertIn("**[0:07]** итоги и планы.", md)
        srt = formats.srt(WORDS)
        self.assertTrue(srt.startswith("1\n00:00:00,300 --> 00:00:01,400\nПривет, это обзор.\n"))
        self.assertTrue(formats.vtt(WORDS).startswith("WEBVTT\n\n00:00:00.300 --> 00:00:01.400\n"))
        self.assertEqual(formats.clock(3725.5), "1:02:05")


class OptionsTest(unittest.TestCase):
    def test_sniff_accepts_video_and_audio(self):
        self.assertEqual(sniff(b"\x00\x00\x00\x20ftypisom" + b"\x00" * 20), "mp4")
        self.assertEqual(sniff(b"\x00\x00\x00\x20ftypM4A " + b"\x00" * 20), "m4a")
        self.assertEqual(sniff(b"ID3\x04" + b"\x00" * 20), "mp3")
        self.assertEqual(sniff(b"OggS\x00" + b"\x00" * 20), "ogg")
        self.assertEqual(sniff(b"fLaC\x00" + b"\x00" * 20), "flac")
        self.assertEqual(sniff(b"RIFF\x00\x00\x00\x00WAVEfmt "), "wav")
        self.assertIsNone(sniff(b"\x89PNG\r\n\x1a\n" + b"\x00" * 20), "картинка — не запись")

    def test_clean_options(self):
        out = clean_options({"model": "qwen3-asr-1.7b", "language": "en", "terms": " ffmpeg "})
        self.assertEqual(out, {"model": "qwen3-asr-1.7b", "language": "en", "terms": "ffmpeg"})
        for bad in ({"model": "x"}, {"language": "de"}, {"terms": 5}):
            with self.assertRaises(ValueError):
                clean_options(bad)


def transcribe_only(ctx):
    return [TranscribeModule(ctx)]


@NEEDS_FFMPEG
class TranscribeApiTest(PlatformCase):
    modules = staticmethod(transcribe_only)
    process_env = FAST

    @classmethod
    def setUpClass(cls):
        cls.media = tempfile.TemporaryDirectory()
        cls.video = Path(cls.media.name) / "talk.mp4"
        make_video(cls.video, 12)
        cls.audio = Path(cls.media.name) / "talk.mp3"
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=330:duration=8",
                        str(cls.audio)], check=True)  # fmt: skip
        cls.silent = Path(cls.media.name) / "silent.mp4"
        make_video(cls.silent, 3, audio=False)

    @classmethod
    def tearDownClass(cls):
        cls.media.cleanup()

    def settle(self, jid, timeout=60):
        return self.poll(f"/api/transcribe/jobs/{jid}", lambda j: j["status"] in ("done", "failed", "canceled"),
                         timeout=timeout)  # fmt: skip

    def job(self, **body):
        status, job = self.json("POST", "/api/transcribe/jobs", {"title": "Обзор", **body})
        self.assertEqual(status, 201, job)
        return job

    def test_config_and_nav(self):
        status, cfg = self.json("GET", "/api/transcribe/config")
        self.assertEqual(status, 200)
        self.assertEqual(cfg["backend"], "fake")
        self.assertEqual([lang["id"] for lang in cfg["languages"]], ["ru", "en"])
        self.assertEqual(cfg["formats"], ["txt", "md", "srt", "vtt", "json"])
        self.assertIn("mp3", cfg["accept"])
        status, platform = self.json("GET", "/api/config")
        tab = next(t for t in platform["tabs"] if t["id"] == "transcribe")
        self.assertEqual((tab["href"], tab["group"]), ("/transcribe/", "generate"))
        status, _, page = self.call("GET", "/transcribe/")
        self.assertEqual(status, 200)
        self.assertIn("Расшифровка", page.decode())

    def test_video_to_text_and_files(self):
        jid = self.job(options={"language": "ru", "terms": "Claude Code"})["id"]
        self.assertEqual(self.json("POST", f"/api/transcribe/jobs/{jid}/start", {})[0], 409, "без файла нельзя")
        status, job = self.json("POST", f"/api/transcribe/jobs/{jid}/source", self.video.read_bytes(),
                                {"X-File-Name": "%D0%BE%D0%B1%D0%B7%D0%BE%D1%80.mp4"})  # fmt: skip
        self.assertEqual(status, 201, job)
        self.assertEqual((job["source"]["kind"], job["source"]["name"]), ("video", "обзор.mp4"))
        self.json("POST", f"/api/transcribe/jobs/{jid}/start", {})
        job = self.settle(jid)
        self.assertEqual(job["status"], "done", (job["error"], job["log"][-5:]))
        self.assertEqual([s["status"] for s in job["stages"]], ["done"] * 4)
        self.assertGreater(job["result"]["words"], 5)
        self.assertTrue(job["text"].startswith("Привет, это обзор недели."))
        self.assertEqual(job["paragraphs"][0]["s"], 0.3)
        self.assertEqual(set(job["files"]), {"txt", "md", "srt", "vtt", "json"})
        status, headers, raw = self.call("GET", job["files"]["srt"])
        self.assertEqual(status, 200)
        self.assertIn(f'filename="transcript-{jid}.srt"', headers["Content-Disposition"])
        self.assertTrue(raw.decode().startswith("1\n00:00:00,300 --> "))
        status, headers, raw = self.call("GET", job["files"]["txt"] + "?inline=1")
        self.assertNotIn("Content-Disposition", headers)
        self.assertEqual(raw.decode(), job["text"])
        words = json.loads(self.call("GET", job["files"]["json"])[2])
        self.assertEqual((words["language"], words["model"]), ("ru", asr.DEFAULT_ID))
        folder = Path(self.tmp.name) / "transcribe" / "jobs" / jid
        self.assertFalse((folder / "audio.wav").exists(), "временный звук убран")
        status, listing = self.json("GET", "/api/transcribe/jobs")
        self.assertNotIn("text", listing["jobs"][0], "список — без полного текста")
        self.assertTrue(listing["jobs"][0]["result"]["preview"].startswith("Привет"))

    def test_audio_rerun_with_other_language_and_delete(self):
        jid = self.job()["id"]
        self.assertEqual(self.call("POST", f"/api/transcribe/jobs/{jid}/source", self.audio.read_bytes())[0], 201)
        self.json("POST", f"/api/transcribe/jobs/{jid}/start", {})
        self.assertEqual(self.settle(jid)["status"], "done")
        status, job = self.json("POST", f"/api/transcribe/jobs/{jid}/start", {"language": "en"})
        self.assertEqual(status, 200, job)
        job = self.settle(jid)
        self.assertEqual((job["status"], job["options"]["language"], job["source"]["kind"]), ("done", "en", "audio"))
        status, headers, _ = self.call("GET", f"/api/transcribe/jobs/{jid}/source")
        self.assertEqual((status, headers["Content-Type"]), (200, "audio/mpeg"))
        self.assertEqual(self.json("POST", f"/api/transcribe/jobs/{jid}/delete")[0], 200)
        self.assertEqual(self.json("GET", f"/api/transcribe/jobs/{jid}")[0], 404)
        self.assertFalse((Path(self.tmp.name) / "transcribe" / "jobs" / jid).exists())

    def test_rejects_bad_files(self):
        jid = self.job()["id"]
        status, err = self.json("POST", f"/api/transcribe/jobs/{jid}/source", b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
        self.assertEqual(status, 400)
        self.assertIn("MP3", err["error"])
        status, err = self.json("POST", f"/api/transcribe/jobs/{jid}/source", self.silent.read_bytes())
        self.assertEqual(status, 400)
        self.assertIn("нет звука", err["error"])
        self.assertEqual(self.json("POST", f"/api/transcribe/jobs/{jid}/start", {"model": "nope"})[0], 400)


@NEEDS_FFMPEG
class TranscribeFailureTest(PlatformCase):
    modules = staticmethod(transcribe_only)
    process_env = FAST | {"FAKE_ASR_FAIL": "1"}

    def test_worker_failure_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "a.wav"
            subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "sine=duration=3", str(audio)],
                           check=True)  # fmt: skip
            _, job = self.json("POST", "/api/transcribe/jobs", {})
            self.call("POST", f"/api/transcribe/jobs/{job['id']}/source", audio.read_bytes())
        self.json("POST", f"/api/transcribe/jobs/{job['id']}/start", {})
        done = self.poll(f"/api/transcribe/jobs/{job['id']}", lambda j: j["status"] in ("done", "failed"), timeout=30)
        self.assertEqual(done["status"], "failed")
        self.assertTrue(done["error"])
        self.assertEqual(done["files"], {})
