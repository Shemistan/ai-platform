"""Уведомления человеку: выпуск ждёт его, упал или опубликован.

Каждое уведомление пишется строкой в `<data>/notifications.log` (с ротацией,
как лог процесса). Если в настройках уведомления включены, ещё и
показывается:

- в Telegram — сообщением в личный чат владельца (`notify.telegram_chat` в
  настройках), тем же ботом, что публикует в каналы (токен ищет сценарий
  через `telegram`, см. StudioModule);
- на Mac — командой AIP_NOTIFY_CMD (заголовок — первая строка stdin, текст —
  остальное), а без неё на macOS — системным уведомлением через osascript.

Итог каждой доставки (ок или ошибка) — в лог процесса: сбой уведомления не
глотается молча. В демо — только журнал.
"""

from __future__ import annotations

import contextlib
import json
import logging
import shlex
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path

from aiplatform.core import log

LOG = "notifications.log"
TELEGRAM_API = "https://api.telegram.org"
TELEGRAM_TIMEOUT_S = 15
_OSASCRIPT = (
    "osascript",
    "-e",
    "on run argv",
    "-e",
    'display notification (item 2 of argv) with title "AI Platform" subtitle (item 1 of argv)',
    "-e",
    "end run",
)

# Куда слать в Telegram: (токен, поля чата для Bot API) или None — не настроено.
TelegramTarget = Callable[[], tuple[str, dict[str, str]] | None]
# Отправка: (url, поля формы) → ответ Bot API; подменяется в тестах.
TelegramPost = Callable[[str, dict[str, str]], dict]


def _telegram_post(url: str, fields: dict[str, str]) -> dict:
    req = urllib.request.Request(url, data=urllib.parse.urlencode(fields).encode(), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=TELEGRAM_TIMEOUT_S) as resp:  # noqa: S310 — адрес Bot API
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:  # 400/403: в теле — причина от Telegram
        try:
            return json.loads(exc.read() or b"{}")
        except json.JSONDecodeError:
            return {"ok": False, "description": f"HTTP {exc.code}"}


class Notifier:
    def __init__(
        self,
        data_dir: Path,
        cmd: str = "",
        *,
        fake: bool = False,
        telegram: TelegramTarget | None = None,
        telegram_post: TelegramPost | None = None,
    ) -> None:
        self.log_path = data_dir / LOG
        self.cmd = shlex.split(cmd) if cmd else []
        self.fake = fake
        self.telegram = telegram  # задаёт вкладка сценариев: она знает, где токен бота
        self.telegram_post = telegram_post or _telegram_post
        self._lock = threading.Lock()

    def send(self, title: str, text: str, *, show: bool, telegram: bool = True) -> list[dict]:
        """Записать в журнал и, если show, показать. Вернуть итоги доставок: [{via, ok, error}].

        telegram=False — в Telegram не слать: владелец уже получил там своё сообщение (превью
        выпуска с кнопками, PRD-001). Сбой показа не мешает работе, но пишется в лог процесса."""
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with self._lock, contextlib.suppress(OSError):
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            log.rotate(self.log_path)
            with self.log_path.open("a", encoding="utf-8") as fh:
                fh.write(f"{stamp}\t{title}\t{' '.join(text.split())}\n")
        if not show:
            return []
        results = []
        target = self._telegram_target() if telegram else None
        if target is not None:
            results.append(self._send_telegram(target, title, text))
        if not self.fake:
            local = self._show_local(title, text)
            if local is not None:
                results.append(local)
        for result in results:
            log.event(
                "notify.sent" if result["ok"] else "notify.failed",
                level=logging.INFO if result["ok"] else logging.WARNING,
                via=result["via"],
                title=title,
                error=result.get("error"),
            )
        return results

    def _telegram_target(self) -> tuple[str, dict[str, str]] | None:
        if self.telegram is None:
            return None
        try:
            return self.telegram()
        except Exception as exc:  # noqa: BLE001 — битые настройки каналов не роняют уведомление
            log.event("notify.failed", level=logging.WARNING, via="telegram", error=f"{type(exc).__name__}: {exc}")
            return None

    def _send_telegram(self, target: tuple[str, dict[str, str]], title: str, text: str) -> dict:
        token, chat = target
        if not token:
            return {"via": "telegram", "ok": False, "error": "нет токена бота"}
        fields = {**chat, "text": f"{title}\n{text}".strip(), "disable_web_page_preview": "true"}
        try:
            answer = self.telegram_post(f"{TELEGRAM_API}/bot{token}/sendMessage", fields)
        except Exception as exc:  # noqa: BLE001 — сеть, таймаут; str(exc) может нести URL с токеном
            return {
                "via": "telegram",
                "ok": False,
                "error": "Нет связи с Telegram. Проверь интернет или VPN.",
                "detail": type(exc).__name__,
            }
        if not answer.get("ok"):
            description = str(answer.get("description") or "ответ без ok").replace(token, "***")
            return {"via": "telegram", "ok": False, "error": f"Telegram ответил «{description}»"}
        return {"via": "telegram", "ok": True, "error": None}

    def _show_local(self, title: str, text: str) -> dict | None:
        if self.cmd:
            argv, stdin = self.cmd, f"{title}\n{text}".encode()
        elif sys.platform == "darwin":
            argv, stdin = [*_OSASCRIPT, title, text], None
        else:
            return None
        via = "cmd" if self.cmd else "osascript"
        try:
            done = subprocess.run(argv, input=stdin, capture_output=True, timeout=15, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            return {"via": via, "ok": False, "error": f"{type(exc).__name__}: {exc}"}
        if done.returncode != 0:
            tail = done.stderr.decode("utf-8", "replace").strip()[-200:]
            return {"via": via, "ok": False, "error": f"код {done.returncode}: {tail}".strip(": ")}
        return {"via": via, "ok": True, "error": None}

    def recent(self, limit: int = 20) -> list[dict]:
        try:
            lines = self.log_path.read_text(encoding="utf-8").splitlines()[-limit:]
        except OSError:
            return []
        out = []
        for line in reversed(lines):
            at, _, rest = line.partition("\t")
            title, _, text = rest.partition("\t")
            out.append({"at": at, "title": title, "text": text})
        return out
