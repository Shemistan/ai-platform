"""Внешний чекер дайджеста (tools/digest_check.py) на фейковых ответах API и Telegram."""

import importlib.util
import io
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "digest_check.py"
_spec = importlib.util.spec_from_file_location("digest_check", SCRIPT)
digest_check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(digest_check)

NOW = datetime(2026, 10, 17, 9, 45)
ENV = {"AIP_ALERT_TG_TOKEN": "123:secret", "AIP_ALERT_TG_CHAT": "42"}


def health(enabled=True, time="07:00", day="yesterday", last=None):
    return {
        "ok": True,
        "version": "0.1.0",
        "uptime_s": 5,
        "schedule": {"enabled": enabled, "time": time, "day": day, "last": last or {}},
    }


def digest(day="2026-10-16", status="waiting", attention=None):
    return {
        "id": "p1",
        "title": f"Дайджест за {day}",
        "source": f"digest:{day}",
        "status": status,
        "attention": attention,
    }


class DigestCheckTest(unittest.TestCase):
    def run_check(self, answers, *, now=NOW, env=ENV, send_error=None):
        sent = []

        def fetch(url):
            for suffix, answer in answers.items():
                if url.endswith(suffix):
                    if isinstance(answer, Exception):
                        raise answer
                    return answer
            raise AssertionError(f"неожиданный запрос {url}")

        def send(token, chat, text):
            if send_error:
                raise send_error
            sent.append((token, chat, text))

        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = digest_check.main(env, now=now, fetch=fetch, send=send, secret=lambda _: None)
        return code, sent, out.getvalue() + err.getvalue()

    def test_service_down(self):
        code, sent, log = self.run_check({"/api/health": urllib.error.URLError("Connection refused")})
        self.assertEqual(code, 1)
        self.assertEqual(sent[0][:2], ("123:secret", "42"))
        self.assertIn("Сервис недоступен", sent[0][2])
        self.assertNotIn("secret", log)  # токен в лог не попадает

    def test_digest_missing(self):
        last = {"day": "2026-10-17", "result": "failed", "error": "OSError: диск", "attempts": 3}
        code, sent, _ = self.run_check({"/api/health": health(last=last), "scenario=digest": {"projects": []}})
        self.assertEqual(code, 1)
        self.assertIn("Дайджест не создан", sent[0][2])
        self.assertIn("2026-10-16", sent[0][2])
        self.assertIn("OSError: диск", sent[0][2])

    def test_all_good_is_silent(self):
        answers = {"/api/health": health(), "scenario=digest": {"projects": [digest(), digest("2026-10-15")]}}
        code, sent, log = self.run_check(answers)
        self.assertEqual((code, sent), (0, []))
        self.assertIn("ok", log)

    def test_disabled_or_too_early_is_silent(self):
        self.assertEqual(self.run_check({"/api/health": health(enabled=False)})[:2], (0, []))
        early = datetime(2026, 10, 17, 7, 20)  # 07:00 + 30 мин ещё не наступило
        self.assertEqual(self.run_check({"/api/health": health()}, now=early)[:2], (0, []))

    def test_today_digest_and_failed_status(self):
        failed = digest("2026-10-17", "failed", {"label": "Озвучка", "text": "движок упал"})
        code, sent, _ = self.run_check({"/api/health": health(day="today"), "scenario=digest": {"projects": [failed]}})
        self.assertEqual(code, 1)
        self.assertIn("Дайджест упал", sent[0][2])
        self.assertIn("Озвучка: движок упал", sent[0][2])

    def test_archived_after_successful_run_is_silent(self):
        last = {"day": "2026-10-17", "result": "ok", "attempts": 1}
        answers = {"/api/health": health(last=last), "scenario=digest": {"projects": []}}
        self.assertEqual(self.run_check(answers)[:2], (0, []))

    def test_no_token_or_telegram_failure(self):
        down = {"/api/health": urllib.error.URLError("refused")}
        code, sent, log = self.run_check(down, env={})
        self.assertEqual((code, sent), (2, []))
        self.assertIn("нет токена", log)
        code, _, log = self.run_check(down, send_error=urllib.error.URLError("https://api.telegram.org/bot123:secret"))
        self.assertEqual(code, 2)
        self.assertNotIn("secret", log)


if __name__ == "__main__":
    unittest.main()
