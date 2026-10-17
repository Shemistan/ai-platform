"""Ревью и публикация выпуска из Telegram-бота (PRD-001).

Выпуск встал на «Публикация: ждёт» — бот шлёт владельцу в личку (`notify.telegram_chat` в
настройках) то, что уйдёт в канал: видео до 50 МБ (больше — шортс, иначе постер) с подписью
поста. Ответом на него — сообщение со списком каналов и кнопками «Опубликовать в N каналов»,
«Поправить подпись», «В архив».

Нажатия и ответы приходят из `narrate bot serve --updates`: getUpdates у бота один (BE-002),
serve печатает их строкой `update {json}`, вкладка «Каналы» (BotServe) отдаёт сюда — handle().
Кнопки и ответы не от владельца игнорируются: ни ответа, ни действия.

- «Опубликовать» сначала спрашивает подтверждение со списком каналов (как UX-001); «Да» идёт
  тем же путём, что кнопка в студии, — publish_now: approved и журнал published.json (BE-001).
- У каждого превью свой одноразовый ключ: второе «Да» и кнопки устаревшего превью ничего не шлют.
- Правка подписи — ответом на превью (или на просьбу бота) новым текстом: он ложится в post.md.
- Подпись превью собирает steps.final_post — та же, что уйдёт в канал: со сносками реестра и
  «Источником» статьи (PRD-016).
- Публикация упала уже после «Да» — новое превью с «Повторить» и новым ключом (PRD-016): повтор
  идёт тем же путём и досылает только то, что не ушло (журнал BE-001).

В демо Telegram — DemoTelegram в памяти: сценарий проходит без сети, вызовы видно в .calls.
"""

from __future__ import annotations

import html
import json
import logging
import secrets
import threading
import time
from collections.abc import Callable
from pathlib import Path

from aiplatform.core import log
from aiplatform.core.fsutil import atomic_write
from aiplatform.core.http import HttpError
from aiplatform.studio import publish, steps
from aiplatform.studio.llm import StudioError
from aiplatform.studio.pipeline import Conflict, ProjectNotFound

FILE = "telegram-review.json"  # превью по выпускам: ключ кнопок и id сообщений в чате владельца
PREFIX = "rv"  # callback_data: rv:<действие>:<выпуск>:<ключ> — до 64 байт, как велит Telegram
MESSAGE_LIMIT = 4096
NOTE_LIMIT = 200  # текст всплывашки answerCallbackQuery

# (метод Bot API, поля, файлы) → ответ Bot API; в демо и тестах — DemoTelegram.
Transport = Callable[[str, dict, dict[str, Path]], dict]


class DemoTelegram:
    """Telegram в памяти: пишет вызовы и отвечает ok с новым message_id."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict, dict[str, str]]] = []
        self._lock = threading.Lock()

    def __call__(self, method: str, fields: dict, files: dict[str, Path]) -> dict:
        with self._lock:
            self.calls.append((method, dict(fields), {k: Path(v).name for k, v in files.items()}))
            return {"ok": True, "result": {"message_id": 1000 + len(self.calls)}}

    def methods(self) -> list[str]:
        with self._lock:
            return [m for m, _, _ in self.calls]


def http_transport(channel: publish.Channel) -> Transport:
    """Настоящий Bot API: тот же токен, повторы и защита от дублей, что у публикации."""

    def call(method: str, fields: dict, files: dict[str, Path]) -> dict:
        token = channel.token()
        if not token:
            raise StudioError(channel.problem() or "нет токена бота")
        body, ctype = publish.multipart({k: str(v) for k, v in fields.items()}, files)
        answer = publish.call_api(f"{publish.telegram_api()}/bot{token}/{method}", body, ctype, None, safe=False)
        if not answer.get("ok"):
            answer = answer | {"description": str(answer.get("description", "")).replace(token, "***")}
        return answer

    return call


def plural(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "канал"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return "канала"
    return "каналов"


def is_owner(user: dict, owner: str) -> bool:
    """Нажал ли владелец: chat id личного чата = id пользователя, либо его @username."""
    if not owner or not isinstance(user, dict):
        return False
    if owner.startswith("@"):
        name = user.get("username")
        return isinstance(name, str) and f"@{name}".lower() == owner.lower()
    return str(user.get("id", "")) == owner


def _button(text: str, action: str, project_id: str, nonce: str) -> dict:
    return {"text": text, "callback_data": f"{PREFIX}:{action}:{project_id}:{nonce}"}


class Review:
    """Превью выпуска владельцу и разбор его нажатий; module — StudioModule."""

    def __init__(self, module) -> None:
        self.module = module
        self.path: Path = module.config.data_dir / FILE
        self._lock = threading.RLock()  # одно нажатие за раз: «Да» дважды подряд не опубликует дважды
        self.transport: Transport | None = DemoTelegram() if module.config.fake else None

    # --- состояние ---

    def _load(self) -> dict[str, dict]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, data: dict[str, dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(self.path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")

    def record(self, project_id: str) -> dict | None:
        with self._lock:
            return self._load().get(project_id)

    def _put(self, project_id: str, rec: dict | None) -> None:
        data = self._load()
        if rec is None:
            data.pop(project_id, None)
        else:
            data[project_id] = rec
        self._save(data)

    # --- Telegram ---

    def owner(self) -> str:
        notify = self.module.ctx.config.settings.load()["notify"]
        return notify["telegram_chat"] if notify.get("enabled") else ""

    def _call(self, method: str, fields: dict, files: dict[str, Path] | None = None) -> dict:
        transport = self.transport
        if transport is None:
            channel = self.module.owner_channel()
            if channel is None:
                raise StudioError("не задан личный чат владельца")
            transport = http_transport(channel)
        answer = transport(method, fields, files or {})
        if not answer.get("ok"):
            raise StudioError(f"Telegram ответил «{answer.get('description', 'ошибка')}»")
        result = answer.get("result")
        return result if isinstance(result, dict) else {}

    def _quiet(self, method: str, fields: dict) -> None:
        """Необязательный вызов (всплывашка, правка подписи): сбой — в лог, не в нажатие."""
        try:
            self._call(method, fields)
        except Exception as exc:  # noqa: BLE001 — сеть, ответ Telegram: нажатие уже обработано
            log.event("review.failed", level=logging.WARNING, method=method, error=f"{type(exc).__name__}: {exc}")

    def _answer(self, query: dict, text: str = "") -> None:
        fields = {"callback_query_id": query.get("id", "")}
        if text:
            fields["text"] = text[:NOTE_LIMIT]
        self._quiet("answerCallbackQuery", fields)

    def _panel(self, rec: dict, text: str, keyboard: list[list[dict]] | None) -> None:
        fields = {"chat_id": rec["chat_id"], "message_id": rec["panel_id"], "text": text[:MESSAGE_LIMIT]}
        fields["reply_markup"] = json.dumps({"inline_keyboard": keyboard or []}, ensure_ascii=False)
        self._quiet("editMessageText", fields)

    # --- превью ---

    def _media(self, project: dict, folder: Path) -> tuple[str, str, Path, str] | None:
        """Что показать: (метод, поле файла, файл, пометка). Видео до 50 МБ, иначе шортс или постер."""
        poster = steps.find(folder, "poster", steps.IMAGE_EXT)
        photo = ("sendPhoto", "photo", poster) if poster and poster.stat().st_size <= publish.PHOTO_LIMIT else None
        if project.get("settings", {}).get("post_format") == "photo" and photo:
            return (*photo, "")
        video, short = folder / steps.VIDEO, folder / steps.SHORT_VIDEO
        if video.is_file() and video.stat().st_size <= publish.BOT_VIDEO_LIMIT:
            return "sendVideo", "video", video, ""
        big = "Видео больше 50 МБ — бот такие не шлёт"
        if short.is_file() and short.stat().st_size <= publish.BOT_VIDEO_LIMIT:
            return "sendVideo", "video", short, f"{big}, показан шортс." if video.is_file() else ""
        if photo:
            return (*photo, f"{big}, показан постер." if video.is_file() else "Видео нет — показан постер.")
        return None

    def _labels(self, chosen: list[str]) -> list[str]:
        known = {c.id: c.label for c in self.module.channels()}
        return [known.get(c, c) for c in chosen]

    def _keyboard(self, project_id: str, nonce: str, count: int) -> list[list[dict]]:
        rows = [[_button(f"Опубликовать в {count} {plural(count)}", "pub", project_id, nonce)]] if count else []
        rows.append(
            [
                _button("Поправить подпись", "edit", project_id, nonce),
                _button("В архив", "arch", project_id, nonce),
            ]
        )
        return rows

    def _panel_text(self, title: str, labels: list[str], note: str = "") -> str:
        lines = [f"Выпуск ждёт публикации: «{title}»"]
        lines.append(f"Каналы: {', '.join(labels)}" if labels else "Каналы не выбраны — выбери их в студии.")
        if note:
            lines.append(note)
        lines.append("Поправить подпись — ответь на превью новым текстом.")
        return "\n".join(lines)

    def _caption(self, project: dict, folder: Path, title: str) -> tuple[str, str]:
        """Подпись превью как в канале (steps.final_post) и пометка, если собрать её не вышло."""

        def read(name: str) -> str:
            try:
                return (folder / name).read_text(encoding="utf-8")
            except OSError:
                return ""

        problem = ""
        try:
            post = steps.final_post(project, read, self.module.config)
        except StudioError as exc:  # у статьи нет источника — показываем тело, публикация откажет
            post, problem = read(steps.POST), f"Не опубликуется: {exc}"
        try:
            return publish.caption(post, title), problem
        except StudioError as exc:
            return f"<b>{html.escape(title)}</b>", problem or str(exc)

    def _failed_text(self, title: str, labels: list[str], failure: str, note: str = "") -> str:
        lines = [f"Публикация «{title}» не удалась: {failure}"]
        if labels:
            lines.append(f"Каналы: {', '.join(labels)}")
        lines.append("«Повторить» отправит только то, что ещё не ушло.")
        if note:
            lines.append(note)
        return "\n".join(lines)

    def _retry_keyboard(self, project_id: str, nonce: str, count: int) -> list[list[dict]]:
        rows = [[_button("Повторить", "ok", project_id, nonce)]] if count else []
        return rows + self._keyboard(project_id, nonce, 0)

    def retry(self, project: dict, failure: str) -> bool:
        """Публикация упала после «Да» в боте (ключ превью погашен) — новое превью с «Повторить».
        False — «Да» в боте не нажимали (запускали из студии) или личный чат не задан."""
        rec = self.record(project["id"])
        if not rec or not rec.get("used"):
            return False
        return self.preview(project, failure=failure or "ошибка без описания")

    def preview(self, project: dict, *, failure: str = "") -> bool:
        """Шлёт владельцу превью выпуска с кнопками. False — личный чат не задан.
        failure — публикация после «Да» упала: в превью причина и «Повторить» (PRD-016)."""
        owner = self.owner()
        if not owner:
            return False
        project_id = project["id"]
        folder = self.module.studio.project_dir(project_id)
        chosen = list(project.get("settings", {}).get("channels") or [])
        title = project.get("title") or project_id
        caption, problem = self._caption(project, folder, title)
        found = self._media(project, folder)
        with self._lock:
            if found:
                method, field, path, note = found
                fields = {"chat_id": owner, "caption": caption, "parse_mode": "HTML"}
                if method == "sendVideo":
                    fields["supports_streaming"] = "true"
                shown = self._call(method, fields, {field: path})
            else:
                note = "Видео и постера нет — ниже только подпись."
                shown = self._call("sendMessage", {"chat_id": owner, "text": caption, "parse_mode": "HTML"})
            nonce = secrets.token_hex(4)
            note = "\n".join(n for n in (problem, note) if n)
            if failure:
                text = self._failed_text(title, self._labels(chosen), failure, note)
                keyboard = self._retry_keyboard(project_id, nonce, len(chosen))
            else:
                text = self._panel_text(title, self._labels(chosen), note)
                keyboard = self._keyboard(project_id, nonce, len(chosen))
            text = text[:MESSAGE_LIMIT]
            fields = {"chat_id": owner, "text": text}
            fields["reply_markup"] = json.dumps({"inline_keyboard": keyboard}, ensure_ascii=False)
            if shown.get("message_id"):
                fields["reply_to_message_id"] = shown["message_id"]
            panel = self._call("sendMessage", fields)
            rec = {
                "nonce": nonce,
                "chat_id": owner,
                "media_id": shown.get("message_id"),
                "panel_id": panel.get("message_id"),
                "prompt_id": None,
                "channels": chosen,
                "title": title,
                "text": text,
                "caption": bool(found),  # превью — медиа с подписью: подпись можно поправить на месте
                "used": False,
                "at": time.time(),
            }
            self._put(project_id, rec)
        log.event("review.preview", project=project_id, channels=len(chosen), retry=bool(failure))
        self._ensure_serve()
        return True

    def _ensure_serve(self) -> None:
        """Нажатия слушает `bot serve` вкладки «Каналы» — не запущен, запускаем (кроме демо)."""
        channels = self.module.ctx.modules.get("channels")
        serve = getattr(channels, "serve", None)
        if self.module.config.fake or serve is None or serve.view()["running"]:
            return
        serve.start()

    # --- нажатия и ответы ---

    def handle(self, update: object) -> None:
        """Одно обновление из getUpdates: нажатие кнопки или ответ на сообщение бота."""
        if not isinstance(update, dict):
            return
        owner = self.owner()
        if isinstance(update.get("callback_query"), dict):
            self._callback(update["callback_query"], owner)
        elif isinstance(update.get("message"), dict):
            self._message(update["message"], owner)

    def _callback(self, query: dict, owner: str) -> None:
        if not is_owner(query.get("from") or {}, owner):
            log.event("review.foreign", level=logging.WARNING, kind="callback")
            return
        parts = str(query.get("data") or "").split(":")
        if len(parts) != 4 or parts[0] != PREFIX:
            self._answer(query, "Эта кнопка мне незнакома.")
            return
        _, action, project_id, nonce = parts
        with self._lock:
            rec = self._load().get(project_id)
            if rec is None or rec.get("nonce") != nonce:
                self._answer(query, "Это превью устарело — открой выпуск в студии.")
                return
            handler = {
                "pub": self._ask,
                "no": self._cancel,
                "ok": self._publish,
                "edit": self._edit,
                "arch": self._archive,
            }.get(action)
            if handler is None:
                self._answer(query, "Эта кнопка мне незнакома.")
                return
            handler(query, project_id, rec)

    def _ask(self, query: dict, project_id: str, rec: dict) -> None:
        """Подтверждение со списком каналов, как в студии (UX-001)."""
        if rec.get("used"):
            self._answer(query, "Уже отправлено в публикацию.")
            return
        labels = self._labels(rec["channels"])
        if not labels:
            self._answer(query, "Каналы не выбраны — выбери их в студии.")
            return
        text = f"Опубликовать «{rec['title']}» в {len(labels)} {plural(len(labels))}?\n" + "\n".join(
            f"• {label}" for label in labels
        )
        nonce = rec["nonce"]
        keyboard = [[_button("Да, опубликовать", "ok", project_id, nonce), _button("Отмена", "no", project_id, nonce)]]
        self._panel(rec, text, keyboard)
        self._answer(query)

    def _cancel(self, query: dict, project_id: str, rec: dict) -> None:
        if not rec.get("used"):
            self._panel(rec, rec["text"], self._keyboard(project_id, rec["nonce"], len(rec["channels"])))
        self._answer(query, "Отменено.")

    def _publish(self, query: dict, project_id: str, rec: dict) -> None:
        """«Да, опубликовать» — тот же путь, что кнопка в студии; ключ превью гасится до отправки."""
        if rec.get("used"):
            self._answer(query, "Уже опубликовано — повтор не шлю.")
            return
        self._put(project_id, rec | {"used": True})
        try:
            self.module.publish_now(project_id, {"channels": rec["channels"]})
        except (HttpError, Conflict, ProjectNotFound, StudioError) as exc:
            self._put(project_id, rec)  # не ушло — кнопка снова в силе
            reason = getattr(exc, "message", None) or str(exc)
            self._panel(
                rec,
                f"{rec['text']}\n\nНе опубликовано: {reason}",
                self._keyboard(project_id, rec["nonce"], len(rec["channels"])),
            )
            self._answer(query, f"Не опубликовано: {reason}")
            log.event("review.publish_failed", level=logging.WARNING, project=project_id, error=reason)
            return
        labels = ", ".join(self._labels(rec["channels"]))
        self._panel(rec, f"Публикую «{rec['title']}» в: {labels}. Итог пришлю отдельно.", None)
        self._answer(query, "Публикую.")
        log.event("review.published", project=project_id, channels=len(rec["channels"]))

    def _edit(self, query: dict, project_id: str, rec: dict) -> None:
        fields = {
            "chat_id": rec["chat_id"],
            "text": f"Пришли новую подпись к «{rec['title']}» ответом на это сообщение.",
            "reply_markup": json.dumps({"force_reply": True}),
        }
        try:
            prompt = self._call("sendMessage", fields)
        except StudioError as exc:
            self._answer(query, str(exc))
            return
        self._put(project_id, rec | {"prompt_id": prompt.get("message_id")})
        self._answer(query)

    def _archive(self, query: dict, project_id: str, rec: dict) -> None:
        try:
            self.module.archive(project_id, {"archived": True})
        except (HttpError, Conflict, ProjectNotFound) as exc:
            self._answer(query, getattr(exc, "message", None) or str(exc))
            return
        self._put(project_id, None)  # кнопки этого превью больше ничего не делают
        self._panel(rec, f"«{rec['title']}» — в архиве. Вернуть можно в студии.", None)
        self._answer(query, "В архиве.")

    def _message(self, message: dict, owner: str) -> None:
        """Ответ владельца на превью или на просьбу бота — новая подпись поста выпуска."""
        reply = message.get("reply_to_message")
        if not isinstance(reply, dict):
            return
        if not is_owner(message.get("from") or {}, owner):
            log.event("review.foreign", level=logging.WARNING, kind="message")
            return
        replied = reply.get("message_id")
        with self._lock:
            found = next(
                (
                    (pid, rec)
                    for pid, rec in self._load().items()
                    if replied is not None
                    and replied in (rec.get("media_id"), rec.get("panel_id"), rec.get("prompt_id"))
                ),
                None,
            )
            if found is None:
                return
            project_id, rec = found
            text = message.get("text") if isinstance(message.get("text"), str) else ""
            reply_to = {"chat_id": rec["chat_id"], "reply_to_message_id": message.get("message_id")}
            if not text.strip():
                self._quiet("sendMessage", reply_to | {"text": "Подпись — текстом: пришли её ответом на превью."})
                return
            if rec.get("used"):
                self._quiet("sendMessage", reply_to | {"text": "Выпуск уже ушёл в публикацию — подпись не меняю."})
                return
            try:
                self.module.save_artifact(project_id, "post", {"text": text})
            except (HttpError, Conflict, ProjectNotFound) as exc:
                reason = getattr(exc, "message", None) or str(exc)
                self._quiet("sendMessage", reply_to | {"text": f"Подпись не сохранена: {reason}"})
                return
            log.event("review.caption", project=project_id)
            project = self.module.studio.get(project_id)
            if rec.get("caption") and rec.get("media_id") and project is not None:
                folder = self.module.studio.project_dir(project_id)
                caption, _ = self._caption(project, folder, rec["title"])  # со сносками и источником
                if caption:
                    fields = {"chat_id": rec["chat_id"], "message_id": rec["media_id"]}
                    self._quiet("editMessageCaption", fields | {"caption": caption, "parse_mode": "HTML"})
            note = "Подпись обновлена. Опубликовать — кнопкой под превью."
            self._quiet("sendMessage", reply_to | {"text": note})
