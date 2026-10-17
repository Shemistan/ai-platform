#!/usr/bin/env python3
"""Внешний чекер дайджеста: пишет владельцу в Telegram напрямую, мимо платформы.

Все алерты платформы шлёт её же процесс — а он и есть точка отказа. Этот
скрипт запускает отдельный LaunchAgent (tools/digest-check.plist.in) раз в
день, через 30 мин после времени дайджеста:

- /api/health не ответил → «Сервис недоступен»;
- расписание включено, а дайджеста за день нет → «Дайджест не создан»;
- дайджест есть, но упал → «Дайджест упал»;
- всё хорошо или расписание выключено → тишина.

Только стандартная библиотека. Токен бота и чат — из env
(AIP_ALERT_TG_TOKEN, AIP_ALERT_TG_CHAT) или из Keychain (служба
aiplatform-digest-check, учётки telegram-token и telegram-chat). В лог — только
факт отправки, без токена. Адрес платформы — AIP_CHECK_URL
(http://127.0.0.1:8780), запас после времени дайджеста — AIP_CHECK_GRACE_MIN (30).

Код выхода: 0 — всё хорошо или рано проверять, 1 — отправлен алерт,
2 — алерт нужен, но отправить не вышло (нет токена, Telegram не ответил).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta

DEFAULT_URL = "http://127.0.0.1:8780"
DEFAULT_GRACE_MIN = 30
TIMEOUT_S = 15
KEYCHAIN_SERVICE = "aiplatform-digest-check"
TELEGRAM_API = "https://api.telegram.org"

Fetch = Callable[[str], dict]
Send = Callable[[str, str, str], None]  # токен, чат, текст


def fetch_json(url: str) -> dict:
    req = urllib.request.Request(url)
    if os.environ.get("AIP_AUTH_TOKEN"):  # платформа с токеном доступа (SEC-007)
        req.add_header("Authorization", f"Bearer {os.environ['AIP_AUTH_TOKEN']}")
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:  # noqa: S310 — свой localhost
        data = json.loads(resp.read().decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{url}: ждали JSON-объект")
    return data


def telegram_send(token: str, chat: str, text: str) -> None:
    body = urllib.parse.urlencode({"chat_id": chat, "text": text, "disable_web_page_preview": "true"}).encode()
    req = urllib.request.Request(f"{TELEGRAM_API}/bot{token}/sendMessage", data=body, method="POST")
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:  # noqa: S310
        answer = json.loads(resp.read().decode("utf-8"))
    if not answer.get("ok"):
        raise RuntimeError(f"Telegram: {answer.get('description') or 'ответ без ok'}")


def keychain(account: str) -> str | None:
    """Секрет из Keychain входа: `security add-generic-password -s aiplatform-digest-check -a <account> -w`."""
    try:
        done = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", account, "-w"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if done.returncode != 0:
        return None
    return done.stdout.strip() or None


def problem(now: datetime, base: str, grace: timedelta, fetch: Fetch) -> tuple[str, str] | None:
    """Что не так: (заголовок, подробности) или None."""
    try:
        health = fetch(f"{base}/api/health")
    except Exception as exc:  # отказ соединения, таймаут, не JSON — для чекера всё одно
        return "Сервис недоступен", f"{base}/api/health не ответил: {exc}"
    plan = health.get("schedule") or {}
    if not plan.get("enabled"):
        return None
    hour, minute = (int(x) for x in str(plan.get("time", "07:00")).split(":"))
    due = now.replace(hour=hour, minute=minute, second=0, microsecond=0) + grace
    if now < due:
        return None  # рано: расписание ещё может успеть
    day = (now.date() - timedelta(days=1) if plan.get("day") == "yesterday" else now.date()).isoformat()
    try:
        projects = fetch(f"{base}/api/studio/projects?scenario=digest").get("projects") or []
    except Exception as exc:
        return "Сервис недоступен", f"список выпусков не отдаётся: {exc}"
    mine = [p for p in projects if p.get("source") == f"digest:{day}"]
    last = plan.get("last") or {}
    if not mine:
        if last.get("day") == now.date().isoformat() and last.get("result") == "ok":
            return None  # выпуск убран в архив или удалён человеком — это его решение
        why = f"последний запуск: {last.get('day') or 'не было'}"
        if last.get("result") == "failed":
            why += f", ошибка: {last.get('error')}, попыток {last.get('attempts')}"
        return "Дайджест не создан", f"За {day} выпуска нет к {due:%H:%M}; {why}."
    if mine[0].get("status") == "failed":
        need = mine[0].get("attention") or {}
        detail = f"{need.get('label', '')}: {need.get('text', '')}".strip(" :")
        return "Дайджест упал", f"«{mine[0].get('title')}» — {detail or 'шаг с ошибкой'}."
    return None


def main(
    env: Mapping[str, str] | None = None,
    *,
    now: datetime | None = None,
    fetch: Fetch = fetch_json,
    send: Send = telegram_send,
    secret: Callable[[str], str | None] = keychain,
) -> int:
    env = os.environ if env is None else env
    now = now or datetime.now()
    base = env.get("AIP_CHECK_URL", DEFAULT_URL).rstrip("/")
    grace = timedelta(minutes=int(env.get("AIP_CHECK_GRACE_MIN", DEFAULT_GRACE_MIN)))
    found = problem(now, base, grace, fetch)
    if found is None:
        print(f"{now:%Y-%m-%d %H:%M} ok")
        return 0
    title, text = found
    token = env.get("AIP_ALERT_TG_TOKEN") or secret("telegram-token")
    chat = env.get("AIP_ALERT_TG_CHAT") or secret("telegram-chat")
    if not token or not chat:
        print(f"{now:%Y-%m-%d %H:%M} {title}: нет токена или чата — алерт не отправлен", file=sys.stderr)
        return 2
    try:
        send(token, chat, f"⚠ ai-platform: {title}\n{text}")
    except Exception as exc:
        # str(exc) от urllib может содержать URL с токеном — пишем только тип.
        print(f"{now:%Y-%m-%d %H:%M} {title}: Telegram не принял ({type(exc).__name__})", file=sys.stderr)
        return 2
    print(f"{now:%Y-%m-%d %H:%M} отправлено: {title}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
