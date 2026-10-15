"""Ядро: GPU-замок, загрузки, Range, запуск подпроцессов."""

import sys
import threading
import time
import unittest

from aiplatform.core import gate as gate_module
from aiplatform.core import http, runner, uploads


class GateTest(unittest.TestCase):
    def test_fifo_and_holder(self):
        gate = gate_module.Gate()
        order = []
        release = threading.Event()
        waits = []

        def first():
            with gate.hold("Видео"):
                order.append("Видео")
                release.wait(5)

        def second():
            with gate.hold("Фото", on_wait=waits.append):
                order.append("Фото")

        t1 = threading.Thread(target=first)
        t1.start()
        while gate.holder() != "Видео":
            time.sleep(0.01)
        t2 = threading.Thread(target=second)
        t2.start()
        while gate.waiting() < 1:
            time.sleep(0.01)
        self.assertEqual(order, ["Видео"])
        release.set()
        t1.join(5)
        t2.join(5)
        self.assertEqual(order, ["Видео", "Фото"])
        self.assertEqual(waits, ["Видео"])
        self.assertIsNone(gate.holder())

    def test_cancel_while_waiting(self):
        gate = gate_module.Gate()
        cancel = threading.Event()
        errors = []
        with gate.hold("Аудио"):

            def waiter():
                try:
                    with gate.hold("Фото", cancel=cancel):
                        pass
                except runner.Canceled:
                    errors.append("canceled")

            t = threading.Thread(target=waiter)
            t.start()
            cancel.set()
            t.join(5)
        self.assertEqual(errors, ["canceled"])
        self.assertEqual(gate.waiting(), 0)
        with gate.hold("Видео"):  # замок не завис
            self.assertEqual(gate.holder(), "Видео")

    def test_no_wait_callback_when_free(self):
        gate = gate_module.Gate()
        waits = []
        with gate.hold("Фото", on_wait=waits.append):
            pass
        self.assertEqual(waits, [])


class UploadsTest(unittest.TestCase):
    def test_sniff(self):
        self.assertEqual(uploads.sniff(b"\x89PNG\r\n\x1a\n...."), "png")
        self.assertEqual(uploads.sniff(b"ID3\x04rest"), "mp3")
        self.assertEqual(uploads.sniff(b"\xff\xfb\x90\x00"), "mp3")
        self.assertEqual(uploads.sniff(b"RIFF\x00\x00\x00\x00WAVEfmt "), "wav")
        self.assertEqual(uploads.sniff(b"\x00\x00\x00\x20ftypM4A "), "m4a")
        self.assertIsNone(uploads.sniff(b"hello"))

    def test_kinds_filter(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            store = uploads.UploadStore(Path(tmp))
            with self.assertRaisesRegex(uploads.UploadError, "PNG"):
                store.save(b"ID3\x04audio", kinds=(uploads.IMAGE,))
            upload_id = store.save(b"ID3\x04audio", kinds=(uploads.AUDIO,))
            self.assertEqual(store.path(upload_id).suffix, ".mp3")
            self.assertIsNone(store.path("../../etc/passwd"))


class RangeTest(unittest.TestCase):
    def test_parse_range(self):
        self.assertEqual(http.parse_range("bytes=0-9", 100), (0, 9))
        self.assertEqual(http.parse_range("bytes=90-", 100), (90, 99))
        self.assertEqual(http.parse_range("bytes=-10", 100), (90, 99))
        self.assertIsNone(http.parse_range("bytes=200-", 100))
        self.assertIsNone(http.parse_range("bytes=-", 100))


class RunnerTest(unittest.TestCase):
    def test_stdin_and_lines(self):
        lines = []
        code = "import sys; data = sys.stdin.read(); print('got', len(data)); print('ok')"
        runner.run([sys.executable, "-c", code], lines.append, threading.Event(), stdin="привет".encode())
        self.assertEqual(lines, ["got 6", "ok"])

    def test_failure_and_missing(self):
        with self.assertRaises(runner.RunFailed):
            runner.run([sys.executable, "-c", "raise SystemExit(3)"], lambda _: None, threading.Event())
        with self.assertRaisesRegex(runner.RunFailed, "не найдена. Подсказка"):
            runner.run(["нет-такой-команды"], lambda _: None, threading.Event(), missing_hint="Подсказка")

    def test_cancel(self):
        cancel = threading.Event()
        timer = threading.Timer(0.3, cancel.set)
        timer.start()
        started = time.time()
        with self.assertRaises(runner.Canceled):
            runner.run([sys.executable, "-c", "import time; time.sleep(30)"], lambda _: None, cancel)
        self.assertLess(time.time() - started, 10)
        timer.cancel()


if __name__ == "__main__":
    unittest.main()
