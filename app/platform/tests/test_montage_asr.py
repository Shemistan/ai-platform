"""Расшифровка монтажа (PRD-020): каталог моделей, argv воркера, точки разреза, сравнение моделей."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from itertools import pairwise
from pathlib import Path
from unittest import mock

from aiplatform.fakes import fake_asr
from aiplatform.montage import asr, asr_worker, compare, transcript
from aiplatform.montage.config import MontageConfig
from tests.fixtures import NEEDS_FFMPEG


def config(root: Path, fake: bool = False) -> MontageConfig:
    return MontageConfig(
        data_dir=root / "data",
        backend="fake" if fake else "mlx",
        asr_python=root / "env" / "bin" / "python",
        models_dir=root / "models",
        ffmpeg="ffmpeg",
        font="",
        llm_cmd=("claude",),
        hyperframes_cmd=("hyperframes",),
        max_upload_bytes=2**30,
    )


def sine_wav(path: Path, seconds: float) -> None:
    argv = ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"sine=frequency=220:duration={seconds}",
            "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(path)]  # fmt: skip
    subprocess.run(argv, check=True)


class CatalogTest(unittest.TestCase):
    def test_three_models_turbo_first_with_pinned_revisions(self):
        self.assertEqual([m.id for m in asr.CATALOG], ["whisper-large-v3-turbo", "whisper-large-v3", "qwen3-asr-1.7b"])
        self.assertEqual(asr.DEFAULT_ID, "whisper-large-v3-turbo")
        for model in asr.CATALOG:
            for weights in (model.weights, model.aligner, model.processor):
                if weights:
                    self.assertRegex(weights.revision, r"^[0-9a-f]{40}$", weights.repo)
        self.assertIsNotNone(asr.get("qwen3-asr-1.7b").aligner)
        self.assertIsNone(asr.get("nope"))

    def test_setup_script_reads_the_same_catalog(self):
        lines = asr.catalog_lines()
        self.assertEqual(len(lines[2].split()), 5, "у Qwen — модель и выравниватель")
        self.assertTrue(lines[0].startswith("whisper-large-v3-turbo mlx-community/whisper-large-v3-turbo "))

    def test_installed_needs_env_and_every_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = config(Path(tmp))
            qwen = asr.get("qwen3-asr-1.7b")
            self.assertEqual(len(asr.missing(qwen, cfg)), 3)
            cfg.asr_python.parent.mkdir(parents=True)
            cfg.asr_python.touch()
            for weights in (qwen.weights, qwen.aligner):
                folder = asr.weights_dir(cfg, weights)
                folder.mkdir(parents=True)
                for name in weights.files[:-1]:
                    (folder / name).touch()
            self.assertEqual(len(asr.missing(qwen, cfg)), 2, "недокачанные веса — не установлена")
            for weights in (qwen.weights, qwen.aligner):
                (asr.weights_dir(cfg, weights) / weights.files[-1]).touch()
            self.assertTrue(asr.installed(qwen, cfg))
            self.assertFalse(asr.installed(asr.get("whisper-large-v3"), cfg))
            # Whisper: веса без токенизатора openai — не установлена (BE-015).
            whisper = asr.get("whisper-large-v3-turbo")
            folder = asr.weights_dir(cfg, whisper.weights)
            folder.mkdir(parents=True)
            for name in whisper.weights.files:
                (folder / name).touch()
            self.assertEqual(asr.missing(whisper, cfg), [f"токенизатора openai/whisper-large-v3-turbo в {folder}"])
            for name in whisper.processor.files:
                (folder / name).touch()
            self.assertTrue(asr.installed(whisper, cfg))
            self.assertTrue(asr.installed(qwen, config(Path("/nonexistent"), fake=True)))
            self.assertEqual([m["installed"] for m in asr.public(cfg)], [True, False, True], "public — по каталогу")


class ArgvTest(unittest.TestCase):
    def test_whisper_and_qwen_argv(self):
        cfg = config(Path("/r"))
        argv = asr.build_argv(cfg, asr.get("whisper-large-v3-turbo"), Path("a.wav"), Path("o.json"),
                              splits=[290.12345, 581.7], hotwords=["Claude Code", "a|b", " "])  # fmt: skip
        self.assertEqual(argv[:2], [str(cfg.asr_python), str(Path(asr_worker.__file__))])
        self.assertEqual(argv[argv.index("--model") + 1], "/r/models/asr/whisper-large-v3-turbo")
        self.assertEqual(argv[argv.index("--language") + 1], "ru")
        self.assertEqual(argv[argv.index("--splits") + 1], "290.123,581.700")
        self.assertEqual(argv[argv.index("--hotwords") + 1], "Claude Code|a b")
        self.assertNotIn("--aligner", argv)
        qwen = asr.build_argv(cfg, asr.get("qwen3-asr-1.7b"), Path("a.wav"), Path("o.json"))
        self.assertEqual(qwen[qwen.index("--language") + 1], "Russian")
        self.assertEqual(qwen[qwen.index("--aligner") + 1], "/r/models/asr/Qwen3-ForcedAligner-0.6B-bf16")
        self.assertNotIn("--splits", qwen)

    def test_progress_lines(self):
        self.assertEqual(asr.parse_progress("PROGRESS 42.5 кусок 3 из 7"), (42.5, "кусок 3 из 7"))
        self.assertEqual(asr.parse_progress("PROGRESS 120"), (100.0, ""))
        self.assertIsNone(asr.parse_progress("Loading weights…"))


class WorkerPureTest(unittest.TestCase):
    def test_spans(self):
        self.assertEqual(asr_worker.spans(10.0, []), [(0.0, 10.0)])
        self.assertEqual(asr_worker.spans(10.0, [7.0, 3.0, 3.0, 12.0, 0.0]), [(0.0, 3.0), (3.0, 7.0), (7.0, 10.0)])

    def test_punctuation_comes_back_after_alignment(self):
        text = "Привет, это — Claude Code! Ну… да."
        items = [("Привет", 0.1, 0.5), ("это", 0.6, 0.8), ("Claude", 1.0, 1.3), ("Code", 1.3, 1.6),
                 ("Ну", 2.0, 2.2), ("да", 2.5, 2.4)]  # fmt: skip
        words = asr_worker.restore_punctuation(text, items)
        self.assertEqual([w["w"] for w in words], ["Привет,", "это —", "Claude", "Code!", "Ну…", "да."])
        self.assertEqual(words[-1]["e"], 2.5, "конец не раньше начала")
        mismatch = asr_worker.restore_punctuation("один два", [("один", 0, 1), ("три", 1, 2)])
        self.assertEqual([w["w"] for w in mismatch], ["один", "три"])


class TranscriptTest(unittest.TestCase):
    def test_silences_from_ffmpeg(self):
        lines = [
            "[silencedetect @ 0x1] silence_start: -0.01",
            "[silencedetect @ 0x1] silence_end: 0.42 | silence_duration: 0.43",
            "size=N/A time=00:00:01.00",
            "[silencedetect @ 0x1] silence_start: 5.5",
        ]
        self.assertEqual(transcript.parse_silences(lines, 7.0), [(0.0, 0.42), (5.5, 7.0)])
        self.assertEqual(transcript.parse_silences(lines), [(0.0, 0.42)])

    def test_split_points_land_in_long_silences(self):
        silences = [(100, 101), (200, 203), (290, 290.5), (450, 452), (700, 700.4)]
        points = transcript.split_points(800, silences, max_chunk=300)
        self.assertEqual(points[0], 201.5, "самая длинная тишина во второй половине окна")
        self.assertEqual(points[1], 451.0)
        self.assertTrue(all(b - a <= 300 for a, b in pairwise([0, *points, 800])))
        self.assertEqual(transcript.split_points(650, [], max_chunk=300), [300, 600])
        self.assertEqual(transcript.split_points(200, silences), [])

    def test_gaps(self):
        words = [{"w": "а", "s": 0.5, "e": 1.0}, {"w": "б", "s": 1.0, "e": 1.5}, {"w": "в", "s": 3.0, "e": 3.2}]
        self.assertEqual(transcript.gaps(words, 4.0), [(0, 0.0, 0.5), (2, 1.5, 3.0), (3, 3.2, 4.0)])

    def test_wer_and_latin(self):
        ref = transcript.normalize("Ставим Claude Code и FFmpeg, ёлки-палки!")
        self.assertEqual(ref, ["ставим", "claude", "code", "и", "ffmpeg", "елки", "палки"])
        hyp = transcript.normalize("ставим клод code и ffmpeg елки палки палки")
        self.assertAlmostEqual(transcript.wer(ref, hyp), 2 / 7)
        self.assertEqual(transcript.latin_recall(ref, hyp), (2, 3))
        self.assertEqual(transcript.wer([], []), 0.0)
        self.assertEqual(transcript.wer(ref, ref), 0.0)

    def test_words_in_silence(self):
        words = [{"w": "а", "s": 0, "e": 1}, {"w": "б", "s": 2, "e": 3}, {"w": "в", "s": 4, "e": 4}]
        self.assertAlmostEqual(transcript.words_in_silence(words, [(1.8, 3.2)]), 2 / 3)


class CompareTest(unittest.TestCase):
    def test_markdown_is_stripped(self):
        md = "# Обзор\n\n- **Claude** [Code](https://x)\n1. `ffmpeg` режет\n```\ncode\n```\n> цитата"
        self.assertEqual(transcript.normalize(compare.strip_markdown(md)),
                         ["обзор", "claude", "code", "ffmpeg", "режет", "цитата"])  # fmt: skip

    def test_table_and_verdict(self):
        rows = [
            {"model": "A", "wer": 0.1, "latin": "3/4", "latin_share": 0.75, "silent": 0.02, "rtf": 0.2, "seconds": 60},
            {"model": "B", "wer": 0.08, "latin": "—", "latin_share": None, "silent": 0.01, "rtf": 0.5, "seconds": 150},
        ]
        self.assertIn("| A | 10.0% | 3/4 | 2.0% | 60 с | 0.20 |", compare.table(rows))
        self.assertIn("B", compare.best(rows))


@NEEDS_FFMPEG
class FakeWorkerTest(unittest.TestCase):
    def test_fake_worker_speaks_the_protocol(self):
        with tempfile.TemporaryDirectory() as tmp:
            wav, out = Path(tmp) / "a.wav", Path(tmp) / "o.json"
            sine_wav(wav, 12)
            argv = [sys.executable, fake_asr.__file__, "--kind", "whisper", "--model", "m", "--audio", str(wav),
                    "--out", str(out), "--splits", "6"]  # fmt: skip
            proc = subprocess.run(argv, capture_output=True, text=True, check=True)
            steps = [asr.parse_progress(line) for line in proc.stdout.splitlines()]
            self.assertEqual([s[0] for s in steps], [50.0, 100.0])
            data = json.loads(out.read_text())
            self.assertAlmostEqual(data["duration"], 12, delta=0.05)
            self.assertEqual(data["words"][0]["w"], "Привет,")
            self.assertTrue(all(w["e"] <= 12 for w in data["words"]))

    def test_compare_end_to_end_on_fakes(self):
        with tempfile.TemporaryDirectory() as tmp:
            rec, ref = Path(tmp) / "rec.wav", Path(tmp) / "teleprompter.md"
            sine_wav(rec, 30)
            spoken = " ".join(w["w"] for w in fake_asr.words_for(30.0, fake_asr.TEXT))
            ref.write_text(f"- **{spoken}**\n", encoding="utf-8")
            env = {"AIP_BACKEND": "fake", "AIP_DATA_DIR": tmp}
            with mock.patch.dict(os.environ, env), mock.patch("builtins.print"):
                self.assertEqual(compare.main([str(rec), str(ref), "--models", "whisper-large-v3-turbo"]), 0)
            report = (Path(tmp) / "rec.asr-compare.md").read_text()
            self.assertIn("| Whisper Large V3 Turbo | 0.0% |", report)
            self.assertTrue((Path(tmp) / "rec.whisper-large-v3-turbo.txt").is_file())


if __name__ == "__main__":
    unittest.main()
