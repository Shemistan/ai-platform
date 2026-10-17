"""Вкладки сценариев «Дайджест новостей», «Видео по статье» и «Пост в канал»: API под /api/studio/."""

from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import re
import threading
import time
from dataclasses import dataclass, field
from http import HTTPStatus

from aiplatform.audio import voices
from aiplatform.core import events, log, media
from aiplatform.core import uploads as uploads_module
from aiplatform.core.fsutil import atomic_write
from aiplatform.core.http import HttpError, Request, bad_request, not_found
from aiplatform.photo import models as photo_models
from aiplatform.studio import config as studio_config
from aiplatform.studio import estimate as estimate_module
from aiplatform.studio import metrics, news, prompts, publish, schedule, series, slides, stats, steps
from aiplatform.studio.cache import MediaCache, place
from aiplatform.studio.llm import StudioError, complete, missing_cli
from aiplatform.studio.pipeline import (
    LABELS,
    STEP_IDS,
    Conflict,
    ProjectNotFound,
    Studio,
    attention,
    blockers,
    dependents,
)
from aiplatform.studio.render import Renderer
from aiplatform.studio.review import Review
from aiplatform.updates import registry
from aiplatform.video import models as video_models

PREFIX = "studio"
URL_BASE = f"/api/{PREFIX}"
SCENARIOS = {"digest": "Дайджест новостей", "article": "Видео по статье", "post": "Пост в канал"}
DIGEST_CHANNEL = "tg-voice-news"  # канал дайджеста у narrator (NEWS_CHANNEL = VOICE_NEWS)
MAX_TEXT = 100_000
MAX_BODY = 4 * MAX_TEXT + 64 * 1024
POSTER_MODES = ("local", "upload")
IMAGES_MODES = ("local", "upload")
CLIPS_MODES = ("local", "upload")
UPSCALE_CHOICES = ("none", *steps.UPSCALE_TARGETS)
VOICE_MODES = ("tts", "upload")
# Настройка → шаг, который она меняет (его и зависящие — заново).
SETTING_STEPS = {
    "poster_mode": "poster",
    "draw_images": "images",
    "images_mode": "images",
    "image_model": ("poster", "images"),
    "upscale_images": ("poster", "images"),
    "clips": "clips",
    "clips_mode": "clips",
    "video_model": "clips",
    "voice_mode": "voice",
    "engine": "voice",
    "voice": "voice",
    "speed": "voice",
    "shorts": "short",
    "channels": "publish",
    "auto_publish": "publish",
    "post_format": "publish",
    "disclaimers": ("render", "short"),
    "ai_badge": ("render", "short"),  # плашка «ИИ» в углу видео (CMP-011)
    "branding": ("render", "short"),
    "tempo": ("render", "short"),
    "slide_s": "plan",  # ориентир для LLM: сколько держится слайд
    "reading": ("render", "short"),  # время на чтение: слайд не сменится, пока не прочитан
    "draft": ("images", "clips", "render", "short"),  # черновик: без картинок и роликов, половинный кадр
    "frame": "render",  # готовые картинки остаются — кадр берёт их с обрезкой; новые рисуются под кадр
    "review_text": None,
    # «Видео по статье»: источник — в конце поста и на последнем слайде (CMP-005).
    "source_url": ("render", "short"),
    "source_author": ("render", "short"),
}
# Правка файла на странице → (файл, шаг, чей это результат).
ARTIFACTS = {
    "text": (steps.TEXT, "text"),
    "post": (steps.POST, "publish"),
    "poster_prompt": (steps.POSTER_PROMPT, "poster_prompt"),
    "plan": (steps.PLAN, "plan"),
    "short": (steps.SHORT, "short"),
}
_FILE = re.compile(
    r"^(poster\.(png|jpg|webp)|voice\.(mp3|wav|m4a)|video\.mp4|short\.mp4|short/voice\.mp3"
    r"|images/\d{2,3}\.(png|jpg|webp)|clips/\d{2,3}\.(mp4|mov|webm)"
    r"|publish/[a-z0-9_-]+/(video\.mp4|title\.txt|description\.txt))$"
)
_SLIDE = re.compile(r"^\d{2,3}$")  # ключ слайда — f"{n:02d}": 01…99, дальше 100…
RATES_TTL_S = 60
IDEMPOTENCY_KEEP = 200  # сколько последних Idempotency-Key помним
IDEMPOTENCY_WAIT_S = 30  # повтор ждёт, пока первый запрос с тем же ключом создаёт выпуск


@dataclass
class _Once:
    """Создание по Idempotency-Key: готово ли и какой выпуск вышел."""

    done: threading.Event = field(default_factory=threading.Event)
    project_id: str | None = None


SUMMARY_WORDS = 900  # ~7 минут озвучки
# Копия выпуска: что не переносится (итоги сборки и публикации) и чьи статусы переносятся.
CLONE_SKIP = {
    "project.json",
    steps.PUBLISHED,
    "publish",
    steps.VIDEO,
    steps.SHORT_VIDEO,
    "video-cover.jpg",
    "video-thumb.jpg",
}
CLONE_STEPS = ("text", "poster_prompt", "poster", "plan", "images", "clips", "voice")
# Группы доски «Выпуски» в порядке показа.
BOARD_GROUPS = {
    "attention": "Ждут тебя",
    "running": "Идут",
    "uploaded": "Выгружено, залей руками",
    "published": "Опубликованы",
    "drafts": "Черновики",
}
# Видео готово, а публикации нет — это тоже ждёт человека: выбрать каналы и опубликовать.
READY_TO_PUBLISH = {
    "step": "publish",
    "label": LABELS["publish"],
    "status": "pending",
    "text": "Видео готово — выбери каналы и опубликуй.",
}


def prose(answer: str) -> str:
    """Текст ответа LLM абзацами: без ```-обрамления. Пустые строки вывод CLI теряет (runner режет
    по переводам строк), поэтому каждая непустая строка — свой абзац, как LLM их и пишет."""
    lines = [ln.strip() for ln in answer.strip().splitlines() if ln.strip() and not ln.strip().startswith("```")]
    return "\n\n".join(lines)


def source_id(scenario: str, *, day: str | None = None, text: str = "") -> str:
    """По чему узнаём тот же материал: у дайджеста — день, у статьи и поста — текст без лишних пробелов."""
    if scenario == "digest":
        return f"digest:{day}"
    return f"{scenario}:" + hashlib.sha256(" ".join(text.split()).encode("utf-8")).hexdigest()[:16]


def board_group(item: dict, project: dict, published: list[dict] | None = None) -> str:
    """Куда выпуск на доске: идёт, ждёт человека, опубликован, выгружен для ручной заливки или черновик.

    Опубликован — только если пост вышел: Telegram sent или пакет, залитый руками со ссылкой (uploaded).
    Пакет без ссылки — «Выгружено, залей руками»: файл до площадки мог и не дойти (AN-003).
    Готовое видео без публикации — «Ждут тебя»."""
    records = published or []
    if item["running"]:
        return "running"
    if item["attention"]:
        return "attention"
    if any(steps.is_live(r) for r in records):
        return "published"
    if any(steps.publication_state(r) == steps.PACKAGED for r in records):
        return "uploaded"
    if project.get("steps", {}).get("render", {}).get("status") == "done":
        return "attention"
    return "drafts"


def _read_text(path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _write_json(path, data: object) -> None:
    atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def video_seconds(project: dict) -> float | None:
    """Длина готового видео: озвучка из сборки с учётом скорости."""
    render = project.get("steps", {}).get("render", {})
    if not render.get("media_s"):
        return None
    return render["media_s"] / float(project.get("settings", {}).get("tempo") or 1.0)


def _read_json_list(path) -> list[dict]:
    try:
        data = json.loads(_read_text(path) or "[]")
    except json.JSONDecodeError:
        return []
    return [r for r in data if isinstance(r, dict) and r.get("channel")] if isinstance(data, list) else []


class StudioModule:
    prefix = PREFIX
    label = "Сценарии"

    def nav(self) -> list[dict]:
        """Вкладки шапки: доска «Выпуски» со счётчиком ждущих и по вкладке на сценарий."""
        board = {"id": "board", "label": "Выпуски", "href": "/board/", "badge": "attention"}
        scenarios = [
            {"id": k, "label": v, "href": f"/studio/?scenario={k}"} | ({"default": True} if k == "article" else {})
            for k, v in SCENARIOS.items()
        ]
        return [board, *scenarios]

    def __init__(self, ctx) -> None:
        self.ctx = ctx
        self.config = studio_config.load(ctx.config)
        self.events = events.EventLog(ctx.config.data_dir / events.FILE)  # журнал событий (AN-002)
        self.studio = Studio(self.config.projects_dir, steps.STEPS, journal=self.events)
        self.studio.on_stop = self._on_stop
        ctx.notifier.telegram = self.owner_telegram
        self.review = Review(self)  # превью и кнопки публикации в личке владельца (PRD-001)
        self.scheduler = schedule.Scheduler(self, self.config.data_dir, self.config.schedule_tick_s)
        self._rates: tuple[float, estimate_module.Rates] | None = None
        self._durations: dict[tuple[str, int], float] = {}
        self._stats_lock = threading.Lock()
        self._create_lock = threading.Lock()  # проверка дубля + создание выпуска (create_unique)
        self._idem_lock = threading.Lock()
        self._idem: dict[str, _Once] = {}  # Idempotency-Key → создание (последние IDEMPOTENCY_KEEP)
        self._stats_thread: threading.Thread | None = None
        self.scheduler.daily["stats"] = self._daily_stats
        self.scheduler.daily["audit"] = self._daily_audit

    def _daily_audit(self) -> str | None:
        """Ротация журнала комплаенса: записи старше года — в архив (CMP-015)."""
        moved = self.studio.audit.rotate()
        return f"журнал комплаенса: в архив {moved}" if moved else None

    def bind(self) -> None:
        """Соседние вкладки готовы — сценарии будут ставить задачи в их очереди."""
        self.studio.services = steps.Services(
            config=self.config,
            photo=self.ctx.modules["photo"].jobs,
            audio=self.ctx.modules["audio"].jobs,
            video=self.ctx.modules["video"].jobs,
            renderer=Renderer(self.config.ffmpeg, self.config.font, self.config.render_scale),
            cache=MediaCache(self.config.cache_dir),
            space=self.ctx.gate.check,
        )
        channels = self.ctx.modules.get("channels")
        if channels is not None:  # нажатия кнопок бота приходят из `bot serve` этой вкладки
            channels.serve.on_update = self.review.handle
        self.scheduler.start()
        self._after_restart()

    def _after_restart(self) -> None:
        """Выпуски, прерванные остановкой сервиса: сказать человеку и (auto) один раз продолжить."""
        interrupted, self.studio.interrupted = self.studio.interrupted, []
        for project_id, step_id in interrupted:
            project = self.studio.get(project_id)
            if project is None or project.get("archived"):
                continue
            label = LABELS.get(step_id, step_id)
            resumed = self.studio.resume_interrupted(project_id, step_id)
            more = "продолжаю шаг сам" if resumed else "запусти шаг заново, когда проверишь"
            log.event("project.interrupted", level=logging.WARNING, project=project_id, step=step_id, resumed=resumed)
            self.notify(
                f"Выпуск прерван перезапуском: {project['title']}",
                f"Шаг «{label}» не закончился — {more}.",
                important=True,
            )

    def _daily_stats(self) -> str | None:
        if not self.ctx.config.settings.load()["stats"]["auto"]:
            return None
        return self.refresh_stats_now()

    # --- уведомления ---

    def notify(self, title: str, text: str, *, important: bool, telegram: bool = True) -> None:
        """В журнал уведомлений всегда; показать — если важно и уведомления включены в настройках.
        telegram=False — владелец уже получил в Telegram превью выпуска, второе сообщение не нужно."""
        show = important and self.ctx.config.settings.load()["notify"]["enabled"]
        self.ctx.notifier.send(title, text, show=show, telegram=telegram)

    def owner_channel(self) -> publish.Channel | None:
        """Личный чат владельца из настроек как канал Telegram: токен — тем же путём, что у каналов."""
        chat = self.ctx.config.settings.load()["notify"]["telegram_chat"]
        if not chat:
            return None
        token_file = "" if self.config.fake else str(self.config.narrator_dir / publish.NARRATOR_TOKEN_FILE)
        return publish.Channel(id="owner", label="Владелец", type=publish.TELEGRAM, chat_id=chat, token_file=token_file)

    def owner_telegram(self) -> tuple[str, dict[str, str]] | None:
        """Для Notifier: (токен, чат) или None. В демо не шлём ничего наружу."""
        channel = None if self.config.fake else self.owner_channel()
        return (channel.token(), channel.target()) if channel else None

    def _on_stop(self, project: dict, before: dict[str, str]) -> None:
        """Запуск выпуска закончился: опубликован — сказать; ждёт тебя или упал — тоже, если это новое."""
        if project.get("archived"):
            return
        publish_state = project["steps"]["publish"]["status"]
        if publish_state == "done" and before.get("publish") != "done":
            records = steps.load_published(self.studio.project_dir(project["id"]))
            if any(steps.is_live(r) for r in records):
                self.notify("Опубликовано", project["title"], important=True)
            else:  # только пакеты для ручной загрузки: выпуск ещё не вышел
                self.notify("Пакет готов — залей руками", project["title"], important=True)
            return
        need = attention(project)
        if need and before.get(need["step"]) != need["status"]:
            head = "Выпуск упал" if need["status"] == "failed" else "Выпуск ждёт тебя"
            previewed = need["step"] == "publish" and need["text"] in steps.REVIEW_NOTES and self._preview(project)
            text = f"{project['title']} — {need['label']}: {need['text']}".strip(" :")
            self.notify(head, text, important=True, telegram=not previewed)

    def _preview(self, project: dict) -> bool:
        """Превью с кнопками владельцу в Telegram; сбой — в лог, тогда уйдёт обычное уведомление."""
        try:
            return self.review.preview(project)
        except Exception as exc:  # noqa: BLE001 — сеть, файлы выпуска: уведомление всё равно уйдёт
            log.event(
                "review.failed", level=logging.WARNING, project=project["id"], error=f"{type(exc).__name__}: {exc}"
            )
            return False

    # --- описание ---

    def channels(self) -> list[publish.Channel]:
        """Каналы только читаем; правит их вкладка «Каналы» через ChannelStore (/api/channels)."""
        try:
            return self.config.channels()
        except ValueError:
            return []

    def channels_or_503(self) -> list[publish.Channel]:
        """Для действий, которым каналы нужны: битый channels.json — ошибка сервера (503), не запроса."""
        try:
            return self.config.channels()
        except ValueError as exc:
            raise HttpError(
                HTTPStatus.SERVICE_UNAVAILABLE, f"Каналы: файл настроек сломан — {exc}", code="config_broken"
            ) from exc

    def publish_now(self, project_id: str, payload: object) -> dict:
        """Кнопка «Опубликовать», как `make news-publish`: выбранные каналы, повторно — с force."""
        if not isinstance(payload, dict):
            raise bad_request("Тело запроса — не объект JSON.")
        chosen, force = payload.get("channels"), payload.get("force", False)
        ignore_verdict = payload.get("ignore_verdict", False)
        if not isinstance(chosen, list) or not chosen or not all(isinstance(c, str) for c in chosen):
            raise bad_request("Выбери хотя бы один канал.")
        unknown = [c for c in chosen if c not in {ch.id for ch in self.channels_or_503()}]
        if unknown:
            raise bad_request(f"Нет таких каналов: {', '.join(unknown)}.")
        if not isinstance(force, bool) or not isinstance(ignore_verdict, bool):
            raise bad_request("force и ignore_verdict — true или false.")
        # Пометки выключены, а в тексте есть упоминания из реестра: публикация — с причиной (CMP-004).
        ignore_disclaimers = payload.get("ignore_disclaimers") or ""
        if not isinstance(ignore_disclaimers, str) or len(ignore_disclaimers) > 500:
            raise bad_request("ignore_disclaimers — причина публикации без пометок, строка до 500 знаков.")

        def apply(project: dict) -> set[str]:
            project["settings"]["channels"] = list(dict.fromkeys(chosen))
            return {"publish"}

        self.studio.update(project_id, apply)
        state = {"approved": True, "force": force, "ignore_verdict": ignore_verdict}
        state["ignore_disclaimers"] = " ".join(ignore_disclaimers.split())
        self.studio.run(project_id, "publish", state=state)
        return self.view(project_id)

    def resolve_publish(self, project_id: str, payload: object) -> dict:
        """«Пост есть» / «Поста нет» по каналу, где ответа Telegram не было; затем публикация
        продолжается: досылает только недостающее. Нажатие — и есть подтверждение публикации."""
        if not isinstance(payload, dict) or not isinstance(payload.get("channel"), str):
            raise bad_request("Укажи канал.")
        posted = payload.get("posted")
        if not isinstance(posted, bool):
            raise bad_request("posted — true или false.")
        folder = self.studio.project_dir(project_id)

        def apply(project: dict) -> set[str]:
            # Под замком студии и только когда выпуск не запущен: шаг публикации журнал сейчас не пишет.
            records = steps.load_published(folder)
            found = next((r for r in records if r["channel"] == payload["channel"]), None)
            if not steps.is_unclear(found):
                raise bad_request("По этому каналу нечего решать: статус отправки известен.")
            fixed = steps.resolve_record(found, posted)
            steps.save_published(folder, [fixed if r is found else r for r in records])
            if fixed.get("state") == steps.SENT:  # «Пост есть» — публикация состоялась, отметил человек
                self.events.emit(
                    "publication.sent",
                    actor=events.actor(),
                    project_id=project["id"],
                    scenario=project.get("scenario"),
                    step="publish",
                    channel_id=fixed["channel"],
                    publication_id=fixed.get("attempt_id"),
                    source=publish.TELEGRAM,
                    props={"resolved": True},
                )
            return set()

        with steps.published_lock(folder):  # журнал не поменяется между чтением и записью в apply
            project = self.studio.update(project_id, apply)
        chosen = project["settings"].get("channels") or []
        records = {r["channel"]: r for r in steps.load_published(folder)}
        if not any(steps.is_unclear(records.get(c)) for c in chosen):
            ignore = bool(project["steps"]["publish"].get("ignore_verdict"))
            self.studio.run(project_id, "publish", state={"approved": True, "ignore_verdict": ignore})
        return self.view(project_id)

    # --- доска выпусков ---

    def board(self, archived: bool = False) -> dict:
        """Все выпуски всех сценариев по группам: ждёт тебя, идёт, опубликовано, черновики."""
        known = {c.id: c for c in self.channels()}
        channels = {c.id: c.label for c in known.values()}
        facts = self._facts()
        items = []
        for item in self.studio.list(archived=archived):
            project = self.studio.get(item["id"]) or {}
            folder = self.studio.project_dir(item["id"])
            published = _read_json_list(folder / steps.PUBLISHED)
            chosen = project.get("settings", {}).get("channels") or []
            review = _read_text(folder / steps.NARRATOR_REVIEW)
            group = board_group(item, project, published)
            items.append(
                item
                | {
                    "scenario_label": SCENARIOS.get(item["scenario"], item["scenario"]),
                    "group": group,
                    "attention": item["attention"] or (READY_TO_PUBLISH if group == "attention" else None),
                    "channels": [
                        {"id": c, "label": channels.get(c, c), "type": known[c].type if c in known else None}
                        for c in chosen
                    ],
                    # Начало подписи — показать в подтверждении публикации с доски.
                    "caption": _read_text(folder / steps.POST).strip()[:300],
                    "published": [
                        {
                            "channel": r["channel"],
                            "label": r.get("label") or channels.get(r["channel"], r["channel"]),
                            "type": known[r["channel"]].type if r["channel"] in known else None,
                            "format": known[r["channel"]].format if r["channel"] in known else None,
                            "public": r.get("public"),
                            "message_id": r.get("message_id"),
                            "state": steps.publication_state(r),
                            "external_url": r.get("external_url"),
                            "published_at": stats.sent_at(r),
                            **stats.reach(r, facts),
                        }
                        for r in published
                        if r.get("ok")
                    ],
                    "video_s": video_seconds(project),
                    "verdict": steps.verdict(review) if review else None,
                    "poster": self._poster_url(item["id"], folder),
                }
            )
        counts = {g: sum(i["group"] == g for i in items) for g in BOARD_GROUPS}
        return {
            "groups": list(BOARD_GROUPS.items()),
            "scenarios": [{"id": k, "label": v} for k, v in SCENARIOS.items()],
            "counts": counts,
            "projects": items,
            "response": stats.summary(items),
            "stats": {"running": self._stats_thread is not None, "at": self.scheduler.state().get("stats_at")},
        }

    # --- отклик ---

    def refresh_stats(self) -> dict:
        """«Обновить отклик»: подписчики и просмотры в фоне — сеть бывает медленной."""
        with self._stats_lock:
            if self._stats_thread is None:
                self._stats_thread = threading.Thread(target=self._refresh_worker, name="studio-stats", daemon=True)
                self._stats_thread.start()
        return {"running": True}

    def _refresh_worker(self) -> None:
        try:
            self.refresh_stats_now()
        finally:
            with self._stats_lock:
                self._stats_thread = None

    def refresh_stats_now(self) -> str:
        """Пройти все опубликованные в Telegram выпуски и дописать снимок отклика."""
        refresher = stats.Refresher({c.id: c for c in self.channels()})
        updated = 0
        for item in self.studio.list() + self.studio.list(archived=True):
            folder = self.studio.project_dir(item["id"])
            records = _read_json_list(folder / steps.PUBLISHED)
            # Сеть — вне замка: она бывает медленной, а публикация в это время пишет журнал.
            fresh = [refresher.record(r) if r.get("ok") else r for r in records]
            snaps = {
                old["channel"]: (old, new)
                for old, new in zip(records, fresh, strict=True)
                if len(new.get("stats") or []) > len(old.get("stats") or [])
            }
            if not snaps:
                continue
            added: list[dict] = []

            def merge(current: list[dict], snaps=snaps, added=added) -> list[dict]:
                # Под замком выпуска — свежий журнал: снимок дописывается к той же отправке, а записи,
                # появившиеся за время похода в сеть (новый канал публикации), остаются как есть.
                out = []
                for r in current:
                    old, new = snaps.get(r["channel"], (None, None))
                    if new is not None and r.get("message_id") == old.get("message_id"):
                        # Что узнал отклик (публичность канала и т. п.) и новый снимок — поверх свежей записи.
                        delta = {k: v for k, v in new.items() if k != "stats" and old.get(k) != v}
                        history = [*(r.get("stats") or []), new["stats"][-1]][-stats.KEEP :]
                        r = r | delta | {"stats": history}
                        added.append(r)
                    out.append(r)
                return out

            steps.update_published(folder, merge)
            if added:
                updated += 1
            for record in added:
                self._snapshot_event(item, record, events.actor(events.SYSTEM))
        self.scheduler.note("stats_at", time.strftime("%Y-%m-%d %H:%M"))
        return f"отклик: обновлено выпусков {updated}"

    def manual_stats(self, project_id: str, payload: object) -> dict:
        """Просмотры, внесённые руками: у пакетов для YouTube, TikTok и у приватных каналов."""
        if not isinstance(payload, dict):
            raise bad_request("Тело запроса — не объект JSON.")
        channel, views = payload.get("channel"), payload.get("views")
        if isinstance(views, bool) or not isinstance(views, int) or views < 0:
            raise bad_request("views — целое число от 0.")
        if self.studio.get(project_id) is None:
            raise not_found("Проект не найден.")
        folder = self.studio.project_dir(project_id)

        def add(records: list[dict]) -> list[dict]:
            if channel not in {r["channel"] for r in records}:
                raise bad_request("В этот канал выпуск не публиковался.")
            snapshot = {"views": views, "members": None, "manual": True}
            return [stats.add_snapshot(r, snapshot) if r["channel"] == channel else r for r in records]

        records = steps.update_published(folder, add)
        project = self.studio.get(project_id) or {}
        record = next(r for r in records if r["channel"] == channel)
        self._snapshot_event(project, record, events.actor())
        return self.view(project_id)

    def uploaded(self, project_id: str, payload: object) -> dict:
        """«Залил»: автор сам залил пакет на площадку и дал ссылку на ролик — теперь выпуск опубликован."""
        if not isinstance(payload, dict) or not isinstance(payload.get("channel"), str):
            raise bad_request("Укажи канал.")
        try:
            link = publish.external_video(payload.get("url"))
        except ValueError as exc:
            raise bad_request(str(exc)) from exc
        folder = self.studio.project_dir(project_id)
        done: dict = {}

        def apply(project: dict) -> set[str]:
            # Под замком студии и только когда выпуск не запущен: шаг публикации журнал сейчас не пишет.
            records = steps.load_published(folder)
            found = next((r for r in records if r["channel"] == payload["channel"]), None)
            if found is None or steps.publication_state(found) not in (steps.PACKAGED, steps.UPLOADED):
                raise bad_request("В этот канал не выгружался пакет для ручной загрузки.")
            # Поправить ссылку можно; время публикации — первое «Залил».
            published_at = found.get("published_at") or time.time()
            fixed = found | link | {"state": steps.UPLOADED, "published_at": published_at}
            steps.save_published(folder, [fixed if r is found else r for r in records])
            done.update(project=project, record=fixed, again=found.get("state") == steps.UPLOADED)
            return set()

        with steps.published_lock(folder):  # журнал не поменяется между чтением и записью в apply
            self.studio.update(project_id, apply)
        record = done["record"]
        self.events.emit(
            "publication.uploaded",
            actor=events.actor(),
            project_id=project_id,
            scenario=done["project"].get("scenario"),
            step="publish",
            channel_id=record["channel"],
            publication_id=record.get("attempt_id"),
            source=publish.MANUAL,
            props={"platform": record["platform"], "external_id": record["external_id"], "corrected": done["again"]},
        )
        return self.view(project_id)

    def _snapshot_event(self, project: dict, record: dict, actor: str) -> None:
        """Последний снимок отклика записи — в журнал событий, по событию на метрику."""
        snapshot = (record.get("stats") or [{}])[-1]
        for metric in ("views", "members"):
            if snapshot.get(metric) is None:
                continue
            self.events.emit(
                "stats.snapshot",
                actor=actor,
                ts=snapshot.get("at"),
                project_id=project.get("id"),
                scenario=project.get("scenario"),
                channel_id=record["channel"],
                publication_id=record.get("attempt_id"),
                metric=metric,
                value=snapshot[metric],
                age_h=snapshot.get("age_h"),
                source="manual" if snapshot.get("manual") else "telegram",
            )

    def _facts(self) -> stats.Facts:
        """Подписчики и посты по каналам из всех выпусков, архивных тоже: прирост канала общий."""
        records: list[dict] = []
        for item in self.studio.list() + self.studio.list(archived=True):
            records += _read_json_list(self.studio.project_dir(item["id"]) / steps.PUBLISHED)
        return stats.Facts(records)

    def _poster_url(self, project_id: str, folder) -> str | None:
        path = steps.find(folder, "poster", steps.IMAGE_EXT)
        if path is None:
            return None
        return f"{URL_BASE}/projects/{project_id}/files/{path.name}?v={path.stat().st_mtime_ns}"

    def badges(self) -> dict:
        """Для шапки: сколько выпусков ждут человека."""
        return {"studio_attention": sum(1 for p in self.studio.list() if p["attention"])}

    def archive(self, project_id: str, payload: object) -> dict:
        archived = payload.get("archived", True) if isinstance(payload, dict) else True
        if not isinstance(archived, bool):
            raise bad_request("archived — true или false.")
        self.studio.archive(project_id, archived)
        return self.view(project_id)

    def image_models(self) -> list[dict]:
        found = self.ctx.modules["photo"].config.models()
        return [
            {"id": m.id, "label": m.label, "installed": m.installed}
            for m in found
            if photo_models.TXT2IMG in m.model.modes
        ]

    def default_image_model(self) -> str | None:
        """AIP_STUDIO_IMAGE_MODEL, если её не выключили в настройках; иначе первая включённая."""
        found = self.image_models()
        ids = [m["id"] for m in found]
        if self.config.image_model in ids:
            return self.config.image_model
        return next((m["id"] for m in found if m["installed"]), ids[0] if ids else None)

    def video_models(self) -> list[dict]:
        found = self.ctx.modules["video"].config.models()
        return [{"id": m.id, "label": m.label, "installed": m.installed} for m in found]

    def defaults(self) -> dict:
        audio = self.ctx.modules["audio"].config
        return {
            "poster_mode": "local",
            "draw_images": True,
            "images_mode": "local",
            "image_model": self.default_image_model(),
            "upscale_images": "none",
            "clips": False,
            "clips_mode": "local",
            "video_model": getattr(video_models.default(self.ctx.modules["video"].config.models()), "id", None),
            "voice_mode": "tts",
            "engine": audio.default_engine,
            "voice": "",
            "speed": None,
            "shorts": False,
            "review_text": False,
            "draft": False,
            "disclaimers": True,
            "ai_badge": True,
            "branding": False,
            "tempo": 1.0,
            "slide_s": slides.TARGET_SLIDE_S,
            "reading": "normal",
            "frame": "landscape",
            "auto_publish": False,
            "post_format": "video",
            "channels": [],
            "source_url": "",
            "source_author": "",
        }

    def scenario_defaults(self, scenario: str) -> dict:
        """Чем настройки сценария отличаются от общих: их и показывает форма нового выпуска."""
        if scenario == "digest":
            # Как в narrator: оформление канала (плашка «подпишись», плей) и обложка из ChatGPT
            # по промпту выпуска — постер загружает человек.
            out: dict = {"branding": True, "poster_mode": "upload", "tempo": 1.25, "frame": "poster", "slide_s": 8}
            usual = [c.id for c in self.channels() if c.id == DIGEST_CHANNEL]  # VOICE_NEWS narrator
            if usual:
                out["channels"] = usual
            return out
        if scenario == "post":
            # Пост — из готового: свой постер и свой mp3, оформление канала, кадр по постеру.
            return {
                "branding": True,
                "poster_mode": "upload",
                "voice_mode": "upload",
                "frame": "poster",
                "draw_images": False,
                "clips": False,
                "shorts": False,
            }
        return {}

    def describe(self) -> dict:
        return {
            "scenario_defaults": {k: self.scenario_defaults(k) for k in SCENARIOS},
            "scenarios": [{"id": k, "label": v} for k, v in SCENARIOS.items()],
            "upload_limits": uploads_module.limits(),
            "steps": [{"id": s, "label": LABELS[s]} for s in STEP_IDS],
            "defaults": self.defaults(),
            "engines": [e.public() for e in self.ctx.modules["audio"].config.engines().values()],
            "image_models": self.image_models(),
            "video_models": self.video_models(),
            "channels": [c.public() for c in self.channels()],
            "channels_file": str(self.config.channels_file),
            "llm": {
                "cmd": self.config.llm_cmd[0] if not self.config.fake else "fake",
                "ok": self.config.llm_available(),
            },
            "narrator_dir": str(self.config.narrator_dir),
        }

    def problems(self) -> list[str]:
        out = []
        if not self.config.llm_available():
            out.append(missing_cli(self.config.llm_cmd[0]))
        # Битый channels.json в баннер выносит вкладка «Каналы» — владелец файла (ChannelStore).
        out += [f"Дайджест: канал {c.label} — {c.problem()}." for c in self._digest_channels() if c.problem()]
        return out

    def _digest_channels(self) -> list[publish.Channel]:
        """Каналы Telegram, куда по умолчанию публикуется дайджест."""
        chosen = set(self.scenario_defaults("digest").get("channels") or [])
        return [c for c in self.channels() if c.id in chosen and c.type == publish.TELEGRAM]

    def health_stats(self) -> dict:
        """«Здоровье» за 7 и 28 дней по журналу событий (REL-010): упавшие шаги, сборка, дайджест вовремя."""
        deadline = self.ctx.config.env.get("AIP_DIGEST_DEADLINE") or metrics.DEFAULT_DEADLINE
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", deadline):
            deadline = metrics.DEFAULT_DEADLINE
        return metrics.report(self.events.read(), deadline=deadline) | {"file": str(self.events.path)}

    def ready_checks(self) -> list[dict]:
        """Для /api/ready: поток расписания жив, у каналов дайджеста есть токен, последний дайджест не упал."""
        checks = []
        tick = self.scheduler.tick_s
        if tick <= 0:
            checks.append(
                {"name": "schedule", "ok": True, "detail": "поток расписания выключен (AIP_SCHEDULE_TICK_S=0)"}
            )
        else:
            last = self.scheduler.state().get("last_tick_at") or "ещё не было"
            checks.append(
                {
                    "name": "schedule",
                    "ok": self.scheduler.alive(),
                    "detail": f"последний тик: {last}, интервал {tick:g} с",
                }
            )
        broken = [f"{c.label}: {c.problem()}" for c in self._digest_channels() if c.problem()]
        checks.append({"name": "token", "ok": not broken, "detail": "; ".join(broken) or "токен каналов найден"})
        run = self.scheduler.digest_run()
        failed = run.get("result") == "failed" and run.get("day") == time.strftime("%Y-%m-%d")
        detail = f"{run.get('day')}: {run.get('result')}" + (f" — {run.get('error')}" if failed else "")
        checks.append({"name": "last_digest", "ok": not failed, "detail": detail if run else "ещё не было"})
        return checks

    # --- настройки ---

    def check_settings(self, raw: object, base: dict) -> dict:
        if raw is None:
            return dict(base)
        if not isinstance(raw, dict):
            raise bad_request("settings — объект.")
        out = dict(base)
        for key, value in raw.items():
            if key not in SETTING_STEPS:
                raise bad_request(f"Неизвестная настройка {key}.")
            out[key] = value
        if out["poster_mode"] not in POSTER_MODES:
            raise bad_request("poster_mode — local или upload.")
        if out["images_mode"] not in IMAGES_MODES:
            raise bad_request("images_mode — local или upload.")
        if out["clips_mode"] not in CLIPS_MODES:
            raise bad_request("clips_mode — local или upload.")
        if out["voice_mode"] not in VOICE_MODES:
            raise bad_request("voice_mode — tts или upload.")
        if out["frame"] not in steps.FRAMES:
            raise bad_request("frame — landscape, square или poster.")
        if out["post_format"] not in publish.POST_FORMATS:
            raise bad_request("post_format — video или photo.")
        if out["upscale_images"] not in UPSCALE_CHOICES:
            raise bad_request("upscale_images — none, 2k или 4k.")
        bools = (
            "draw_images", "clips", "shorts", "review_text", "auto_publish", "disclaimers", "ai_badge", "branding",
            "draft",
        )  # fmt: skip
        for key in bools:
            if not isinstance(out[key], bool):
                raise bad_request(f"{key} — true или false.")
        tempo = out["tempo"]
        if isinstance(tempo, bool) or not isinstance(tempo, int | float) or float(tempo) not in steps.TEMPOS:
            raise bad_request(f"tempo — одно из: {', '.join(f'{t:g}' for t in steps.TEMPOS)}.")
        out["tempo"] = float(tempo)
        slide_s = out.get("slide_s", slides.TARGET_SLIDE_S)
        if isinstance(slide_s, bool) or slide_s not in slides.SLIDE_SECONDS:
            raise bad_request(f"slide_s — одно из: {', '.join(map(str, slides.SLIDE_SECONDS))}.")
        out["slide_s"] = int(slide_s)
        out.setdefault("reading", "normal")
        if out["reading"] not in slides.READING:
            raise bad_request(f"reading — одно из: {', '.join(slides.READING)}.")
        engine = self.ctx.modules["audio"].config.engines().get(out["engine"])
        if engine is None:
            raise bad_request(voices.unknown(out["engine"]))
        if out["voice"] and out["voice"] not in engine.voices:
            raise bad_request(voices.unknown(engine.id, out["voice"]))
        speed = out["speed"]
        if speed not in (None, "") and (isinstance(speed, bool) or not isinstance(speed, int | float)):
            raise bad_request("speed — число или null.")
        if speed not in (None, ""):
            low, high = voices.SPEED_RANGE
            if not low <= speed <= high:
                raise bad_request(f"Скорость — от {low:g} до {high:g}.")
        else:
            out["speed"] = None
        # Пустой список — все модели выключены в настройках: выпуск без картинок и роликов можно.
        image_ids = {m["id"] for m in self.image_models()}
        if out["image_model"] not in image_ids and (image_ids or out["image_model"] is not None):
            raise bad_request(photo_models.unknown(out["image_model"], "Неизвестная модель картинок."))
        video_ids = {m["id"] for m in self.video_models()}
        if out["video_model"] not in video_ids and (video_ids or out["video_model"] is not None):
            raise bad_request(video_models.unknown(out["video_model"], "Неизвестная модель видео."))
        for key, limit in (("source_url", 500), ("source_author", 200)):
            value = out.get(key) or ""
            if not isinstance(value, str) or len(value) > limit:
                raise bad_request(f"{key} — строка до {limit} знаков.")
            out[key] = " ".join(value.split())
        if out["source_url"] and not re.fullmatch(r"https?://\S+", out["source_url"]):
            raise bad_request("Ссылка на источник — вида https://…")
        known = {c.id for c in self.channels()}
        if not isinstance(out["channels"], list) or not all(isinstance(c, str) for c in out["channels"]):
            raise bad_request("channels — список id каналов.")
        unknown = [c for c in out["channels"] if c not in known]
        if unknown:
            raise bad_request(f"Нет таких каналов: {', '.join(unknown)}.")
        return out

    # --- проекты ---

    def create(self, payload: object) -> dict:
        if not isinstance(payload, dict):
            raise bad_request("Тело запроса — не объект JSON.")
        scenario = payload.get("scenario")
        if scenario not in SCENARIOS:
            raise bad_request("Сценарий — digest, article или post.")
        # У дайджеста, как в narrator: оформление канала (плашка «подпишись», плей) и обложка
        # из ChatGPT по промпту выпуска — постер загружает человек.
        base = self.defaults() | self.scenario_defaults(scenario)
        settings = self.check_settings(payload.get("settings"), base)
        title = " ".join(str(payload.get("title") or "").split())[:200]
        extra: dict = {"auto_title": not title}
        force_new = payload.get("force_new", False)
        if not isinstance(force_new, bool):
            raise bad_request("force_new — true или false.")
        if scenario == "digest":
            try:
                day = news.parse_day(payload.get("day"))
            except ValueError as exc:
                raise bad_request(str(exc)) from exc
            extra["day"] = day.isoformat()
            extra["source"] = source_id(scenario, day=extra["day"])
            title = title or f"Дайджест новостей за {day.strftime('%d.%m.%Y')}"
            extra["auto_title"] = False
            project = self.create_unique(scenario, title, settings, extra, force_new=force_new)
        elif scenario == "post":
            post = payload.get("post")
            if not isinstance(post, str) or not post.strip():
                raise bad_request("Напиши подпись к посту.")
            text = payload.get("text") if isinstance(payload.get("text"), str) else ""
            if len(post) > MAX_TEXT or len(text) > MAX_TEXT:
                raise bad_request(f"Текст длиннее {MAX_TEXT} знаков.")
            first = publish.plain_text(post).splitlines()[0] if publish.plain_text(post) else ""
            title = title or first[:90] or "Пост в канал"
            extra["source"] = source_id(scenario, text=f"{post}\n{text}")
            project = self.create_unique(scenario, title, settings, extra, force_new=force_new)
            folder = self.studio.project_dir(project["id"])
            atomic_write(folder / steps.POST, post.strip() + "\n")
            if text.strip():
                atomic_write(folder / steps.TEXT, text.strip() + "\n")
        else:
            text = payload.get("text")
            if not isinstance(text, str) or not text.strip():
                raise bad_request("Вставь текст статьи.")
            if len(text) > MAX_TEXT:
                raise bad_request(f"Текст длиннее {MAX_TEXT} знаков.")
            paras = slides.paragraphs(text)
            title = title or (paras[0][:90] if paras else "Видео по статье")
            extra["source"] = source_id(scenario, text=text)
            project = self.create_unique(scenario, title, settings, extra, force_new=force_new)
            (self.studio.project_dir(project["id"]) / steps.TEXT).write_text(text.strip() + "\n", encoding="utf-8")
        if payload.get("run"):
            self.studio.run(project["id"])
        return self.view(project["id"])

    def create_unique(self, scenario: str, title: str, settings: dict, extra: dict, *, force_new: bool) -> dict:
        """Проверка дубля по source и создание — под одним замком: двойной сабмит формы или
        расписание вместе с ручным запуском не дают два выпуска на один материал (второй — 409)."""
        with self._create_lock:
            self._no_duplicate(extra["source"], force_new)
            return self.studio.create(scenario, title, settings, extra)

    def create_once(self, payload: object, key: str | None) -> dict:
        """POST /projects с Idempotency-Key: повтор с тем же ключом отдаёт уже созданный выпуск."""
        if not key:
            return self.create(payload)
        key = key.strip()[:200]
        with self._idem_lock:
            once = self._idem.get(key)
            owner = once is None
            if owner:
                once = self._idem[key] = _Once()
        if not owner:
            once.done.wait(IDEMPOTENCY_WAIT_S)
            if once.project_id and self.studio.get(once.project_id) is not None:
                return self.view(once.project_id)
            return self.create(payload)
        try:
            view = self.create(payload)
            once.project_id = view["id"]
            return view
        except BaseException:
            with self._idem_lock:
                self._idem.pop(key, None)  # не создали — ключ свободен для нового захода
            raise
        finally:
            once.done.set()
            with self._idem_lock:
                while len(self._idem) > IDEMPOTENCY_KEEP:
                    self._idem.pop(next(iter(self._idem)))

    def _no_duplicate(self, source: str, force_new: bool) -> None:
        """Выпуск по тому же дню или тексту уже есть — предложить открыть его (force_new — создать новый)."""
        if force_new:
            return
        existing = next((p for p in self.studio.list() if self.source_of(p) == source), None)
        if existing is not None:
            raise HttpError(
                HTTPStatus.CONFLICT,
                f"Уже есть выпуск «{existing['title']}» по этому же материалу — открой его или создай новый.",
                {"existing": {"id": existing["id"], "title": existing["title"], "scenario": existing["scenario"]}},
            )

    def source_of(self, item: dict) -> str | None:
        """Источник выпуска; у выпусков из прошлых версий — по дню или по тексту на диске."""
        if item.get("source"):
            return item["source"]
        project = self.studio.get(item["id"]) or {}
        folder = self.studio.project_dir(item["id"])
        if project.get("scenario") == "digest" and project.get("day"):
            return source_id("digest", day=project["day"])
        if project.get("scenario") == "article":
            return source_id("article", text=_read_text(folder / steps.TEXT))
        if project.get("scenario") == "post":
            return source_id("post", text=f"{_read_text(folder / steps.POST)}\n")
        return None

    def clone(self, project_id: str) -> dict:
        """Копия выпуска: те же настройки и готовые результаты до сборки; сборка и публикация — заново."""
        source = self.studio.get(project_id)
        if source is None:
            raise not_found("Проект не найден.")
        if source["running"]:
            raise HttpError(HTTPStatus.CONFLICT, "Проект сейчас выполняется — дождись или останови его.")
        extra = {k: source[k] for k in ("day", "source", "auto_title") if k in source}
        copy = self.studio.create(source["scenario"], f"{source['title']} (копия)", source["settings"], extra)
        src, dst = self.studio.project_dir(project_id), self.studio.project_dir(copy["id"])
        for path in src.rglob("*"):
            rel = path.relative_to(src)
            if not path.is_file() or rel.parts[0] in CLONE_SKIP or path.name.endswith(".tmp"):
                continue
            place(path, dst / rel)
        kept = {s: st for s, st in source["steps"].items() if s in CLONE_STEPS and st["status"] in ("done", "skipped")}

        def apply(project: dict) -> set[str]:
            for step_id, state in kept.items():
                project["steps"][step_id] = {k: v for k, v in state.items() if k not in ("fresh", "poke")}
            return set()

        self.studio.update(copy["id"], apply)
        return self.view(copy["id"])

    # --- длинная статья: серия и выжимка ---

    def _article_source(self, project_id: str) -> tuple[dict, str]:
        project = self.studio.get(project_id)
        if project is None:
            raise not_found("Проект не найден.")
        if project["scenario"] != "article":
            raise bad_request("Серия и выжимка — для выпусков «Видео по статье».")
        text = _read_text(self.studio.project_dir(project_id) / steps.TEXT).strip()
        if not text:
            raise bad_request("Нет текста статьи.")
        return project, text

    def split(self, project_id: str, payload: object) -> dict:
        """Серия: по выпуску на часть (по главам, ~8 мин), с теми же настройками. Исходный — в архив."""
        project, text = self._article_source(project_id)
        parts = series.split(text)
        if len(parts) < 2:
            raise bad_request("Статья и так короткая — делить нечего.")
        run = isinstance(payload, dict) and payload.get("run") is True
        created = []
        for n, part in enumerate(parts, start=1):
            title = f"{project['title']} — часть {n} из {len(parts)}"
            if part.title:
                title += f": {part.title}"
            extra = {"auto_title": False, "source": source_id("article", text=part.text), "series_of": project_id}
            new = self.studio.create("article", title[:200], project["settings"], extra)
            (self.studio.project_dir(new["id"]) / steps.TEXT).write_text(part.text + "\n", encoding="utf-8")
            if run:
                self.studio.run(new["id"])
            created.append({"id": new["id"], "title": new["title"], "words": part.words})
        if not project["running"]:
            self.studio.archive(project_id, True)
        return {"projects": created}

    def summarize(self, project_id: str) -> dict:
        """Выжимка: LLM в фоне пишет текст на 5–8 минут, из него — новый выпуск. Ход — в журнале исходного."""
        project, text = self._article_source(project_id)
        if (project.get("summary") or {}).get("status") == "running":
            raise HttpError(HTTPStatus.CONFLICT, "Выжимка уже пишется.")
        self.studio.annotate(project_id, summary={"status": "running"})
        self.studio.log(project_id, "Выжимка: LLM пишет текст на 5–8 минут…")

        def work() -> None:
            try:
                answer = complete(
                    prompts.summary(text, project["title"], SUMMARY_WORDS),
                    cmd=self.config.llm_cmd,
                    cancel=threading.Event(),
                )
                short = prose(answer)
                if len(short.split()) < 50:
                    raise StudioError("LLM вернула слишком короткую выжимку.")
                extra = {"auto_title": False, "source": source_id("article", text=short), "summary_of": project_id}
                title = f"{project['title']} — коротко"[:200]
                new = self.studio.create("article", title, project["settings"], extra)
                (self.studio.project_dir(new["id"]) / steps.TEXT).write_text(short + "\n", encoding="utf-8")
                self.studio.annotate(project_id, summary={"status": "done", "project": new["id"]})
                self.studio.log(project_id, f"Выжимка готова: {len(short.split())} слов — выпуск «{title}».")
            except StudioError as exc:
                self.studio.annotate(project_id, summary={"status": "failed", "error": str(exc)})
                self.studio.log(project_id, f"Выжимка: {exc}")

        threading.Thread(target=work, name=f"summary-{project_id}", daemon=True).start()
        return self.view(project_id)

    def view(self, project_id: str) -> dict:
        project = self.studio.get(project_id)
        if project is None:
            raise not_found("Проект не найден.")
        folder = self.studio.project_dir(project_id)

        def read(name: str) -> str:
            try:
                return (folder / name).read_text(encoding="utf-8")
            except OSError:
                return ""

        def as_json(name: str) -> object:
            try:
                return json.loads(read(name)) if read(name) else None
            except json.JSONDecodeError:
                return None

        def url(path) -> str | None:
            if path is None or not path.is_file():
                return None
            rel = path.relative_to(folder).as_posix()
            return f"{URL_BASE}/projects/{project_id}/files/{rel}?v={path.stat().st_mtime_ns}"

        project["settings"] = self.defaults() | project["settings"]  # выпуски из версий без новых настроек
        project["scenario_label"] = SCENARIOS[project["scenario"]]
        raw = project["steps"]
        project["steps"] = [
            {
                "id": s,
                "label": LABELS[s],
                **raw[s],
                # Чего шаг ждёт от других шагов и можно ли сейчас класть ему файлы.
                "blocked_by": [LABELS[b] for b in blockers(project, s)],
                "locked": any(raw[d]["status"] == "running" for d in [s, *dependents(s)]),
                # Что сбросит перезапуск шага — для подтверждения «Перезапустить шаг».
                "dependents": dependents(s),
            }
            for s in STEP_IDS
        ]
        project["artifacts"] = {
            "text": read(steps.TEXT),
            "post": read(steps.POST),
            "poster_prompt": read(steps.POSTER_PROMPT),
            "plan": as_json(steps.PLAN),
            "short": as_json(steps.SHORT),
        }
        project["slides"] = self._slides(folder, url)
        project["files"] = {
            "poster": url(steps.find(folder, "poster", steps.IMAGE_EXT)),
            "images": {s["key"]: s["image"] for s in project["slides"] if s["image"]},
            "voice": url(steps.find(folder, "voice", steps.AUDIO_EXT)),
            "video": url(folder / steps.VIDEO),
            "short_voice": url(folder / "short" / "voice.mp3"),
            "short_video": url(folder / steps.SHORT_VIDEO),
        }
        published = as_json(steps.PUBLISHED)
        if isinstance(published, list):  # отклик в окнах 24ч/72ч/7д — рядом с записью, не поверх её полей
            facts = self._facts()
            published = [r | {"reach": stats.reach(r, facts)} if isinstance(r, dict) else r for r in published]
        project["published"] = published
        review = read(steps.NARRATOR_REVIEW)
        project["review"] = {"verdict": steps.verdict(review), "text": review} if review else None
        found = registry.mentioned(f"{project['title']}\n{read(steps.TEXT)}\n{read(steps.POST)}", self.banned())
        project["disclaimers"] = [{"name": e.name, "note": e.disclaimer + "."} for e in found]
        # Куда ведут ссылки поста и какие из них не из исходника — видно до публикации (SEC-013).
        project["links"] = steps.post_links(project["scenario"], project["settings"], read, read(steps.POST))
        plan = self._plan(folder) or None
        voice = steps.find(folder, "voice", steps.AUDIO_EXT)
        project["estimate"] = estimate_module.estimate(
            scenario=project["scenario"],
            settings=project["settings"],
            text=read(steps.TEXT),
            plan=plan,
            statuses={s: raw[s]["status"] for s in STEP_IDS},
            have_images=len(project["files"]["images"]),
            rates=self.rates(),
            audio_s=self._duration(voice) if voice is not None else None,
        )
        project["preflight"] = self.preflight(project["scenario"], project["settings"], read(steps.TEXT))
        return project

    # --- оценка и проверка до запуска ---

    def _duration(self, path) -> float:
        """Длительность озвучки для оценки; ffprobe — только когда файл сменился."""
        stamp = (str(path), path.stat().st_mtime_ns)
        if stamp not in self._durations:
            self._durations = {stamp: media.duration(path, self.config.ffmpeg)}
        return self._durations[stamp]

    def rates(self) -> estimate_module.Rates:
        """Ставки по истории задач; пересчёт не чаще раза в минуту — страница выпуска опрашивает часто."""
        now = time.monotonic()
        if self._rates is None or now - self._rates[0] > RATES_TTL_S:
            modules = self.ctx.modules
            projects = [self.studio.get(p["id"]) or {} for p in self.studio.list()]
            found = estimate_module.rates(
                modules["photo"].jobs.list(), modules["video"].jobs.list(), modules["audio"].jobs.list(), projects
            )
            self._rates = (now, found)
        return self._rates[1]

    def preflight(self, scenario: str, settings: dict, text: str) -> list[dict]:
        """Что помешает выпуску дойти до канала или задержит его — видно до «Запустить всё».
        warn — сломается или не опубликуется, info — будет ждать тебя."""
        out: list[dict] = []

        def warn(message: str) -> None:
            out.append({"level": "warn", "text": message})

        def info(message: str) -> None:
            out.append({"level": "info", "text": message})

        channels = {c.id: c for c in self.channels()}
        for channel_id in settings.get("channels") or []:
            channel = channels.get(channel_id)
            if channel is None:
                warn(f"Канала {channel_id} больше нет — убери его из выбранных.")
                continue
            if channel.format == publish.VERTICAL and (scenario == "post" or not settings.get("shorts")):
                if scenario == "post":
                    warn(f"«{channel.label}» ждёт вертикальное видео 9:16, а у поста его нет — выбери канал 16:9.")
                else:
                    warn(f"«{channel.label}» ждёт шортс 9:16, а он выключен — включи «Шортс» или убери канал.")
            if channel.problem():
                warn(f"«{channel.label}»: {channel.problem()}.")
        if not settings.get("channels"):
            info("Каналы не выбраны — публикация будет ждать выбора.")
        elif stale := steps.registry_stale(self.config):
            warn(stale)
        if not self.config.llm_available():
            warn(f"LLM-команда «{self.config.llm_cmd[0]}» не найдена — промпты и слайды не написать.")
        draws = settings.get("draw_images") and settings.get("images_mode") == "local" and not settings.get("draft")
        if scenario != "post" and (settings.get("poster_mode") == "local" or draws):
            model = next((m for m in self.image_models() if m["id"] == settings.get("image_model")), None)
            if model is not None and not model["installed"]:
                warn(f"Модель картинок «{model['label']}» не установлена.")
        if settings.get("clips") and settings.get("clips_mode") == "local" and not settings.get("draft"):
            model = next((m for m in self.video_models() if m["id"] == settings.get("video_model")), None)
            if model is not None and not model["installed"]:
                warn(f"Модель видео «{model['label']}» не установлена.")
        waits = []
        if settings.get("poster_mode") == "upload":
            waits.append("постер")
        if scenario != "post" and settings.get("draw_images") and settings.get("images_mode") == "upload":
            waits.append("картинки слайдов")
        if scenario != "post" and settings.get("clips") and settings.get("clips_mode") == "upload":
            waits.append("ролики")
        if settings.get("voice_mode") == "upload":
            waits.append("озвучку")
        if waits:
            info(f"Выпуск будет ждать твои файлы: {', '.join(waits)}.")
        if settings.get("review_text"):
            info("Пауза после текста — выпуск продолжится после «Продолжить».")
        words = len(text.split())
        if scenario == "article" and estimate_module.voice_seconds(words) > estimate_module.LONG_VOICE_S:
            minutes = round(estimate_module.voice_seconds(words) / 60)
            warn(
                f"Озвучка ~{minutes} мин — сборка займёт часы, а длинное видео досматривают редко. "
                "Подумай о серии по главам или выжимке на 5–8 минут."
            )
        return out

    def check_draft(self, payload: object) -> dict:
        """Проверка и оценка для формы «Новый выпуск» — до создания."""
        if not isinstance(payload, dict):
            raise bad_request("Тело запроса — не объект JSON.")
        scenario = payload.get("scenario")
        if scenario not in SCENARIOS:
            raise bad_request("Сценарий — digest, article или post.")
        settings = self.check_settings(payload.get("settings"), self.defaults() | self.scenario_defaults(scenario))
        text = payload.get("text") if isinstance(payload.get("text"), str) else ""
        text = text[:MAX_TEXT]
        if scenario == "post" and not text.strip():
            text = publish.plain_text(str(payload.get("post") or ""))
        found = estimate_module.estimate(
            scenario=scenario,
            settings=settings,
            text=text,
            plan=None,
            statuses={s: "pending" for s in STEP_IDS},
            have_images=0,
            rates=self.rates(),
        )
        return {"estimate": found, "preflight": self.preflight(scenario, settings, text)}

    def banned(self) -> list[registry.Entry]:
        return steps.banned(self.config)

    @staticmethod
    def _plan(folder) -> list[slides.Slide]:
        try:
            data = json.loads((folder / steps.PLAN).read_text(encoding="utf-8"))
            paras = slides.paragraphs((folder / steps.TEXT).read_text(encoding="utf-8"))
            return slides.parse_plan(data, paras or 1)
        except (OSError, json.JSONDecodeError, StudioError):
            return []

    def _slides(self, folder, url) -> list[dict]:
        """Слайды для страницы: промпты картинки и видео и файлы, сделанные по ним."""
        out = []
        plan = self._plan(folder)
        chosen = dict(steps.media_prompts(steps.CLIPS, plan))  # роликов — не больше MAX_CLIPS
        for n, slide in enumerate(plan, start=1):
            image_prompt, video_prompt = slide.image_prompt, chosen.get(n, "")
            out.append(
                {
                    "key": f"{n:02d}",
                    "title": slide.title,
                    "image_prompt": image_prompt,
                    "video_prompt": video_prompt,
                    "image": url(steps.slide_media(folder / steps.IMAGES, n, steps.IMAGES, image_prompt)),
                    "clip": url(steps.slide_media(folder / steps.CLIPS, n, steps.CLIPS, video_prompt)),
                }
            )
        return out

    def upload_slide(self, project_id: str, kind: str, key: str, data: bytes) -> dict:
        """Картинка или видео слайда от человека. Все на месте — шаг готов, сборка заново."""
        if kind not in steps.MEDIA or not _SLIDE.match(key):
            raise not_found("Загружать можно images/NN или clips/NN.")
        if kind == steps.IMAGES:
            ext = uploads_module.sniff(data) if data else None
            if ext not in uploads_module.IMAGE_MIME:
                raise bad_request(uploads_module._accepted((uploads_module.IMAGE,)))
            if len(data) > uploads_module.MAX_UPLOAD_BYTES:
                raise bad_request(f"Картинка больше {uploads_module.MAX_UPLOAD_BYTES // 2**20} МБ.")
        else:
            ext = uploads_module.sniff_video(data) if data else None
            if ext is None:
                raise bad_request("Поддерживаются только видео MP4, MOV, WebM.")
        self._guard_step(project_id, kind)
        folder = self.studio.project_dir(project_id)
        n = int(key)
        prompts = dict(steps.media_prompts(kind, self._plan(folder)))
        if n not in prompts:
            raise not_found(f"В плане нет слайда {n}, которому нужны {steps.MEDIA[kind][1]}.")
        with self.studio.idle(project_id, kind):  # шаг не стартует между проверкой и записью
            steps.save_slide_media(folder / kind, n, ext, data, prompts[n], kind)
        have = sum(steps.slide_media(folder / kind, m, kind, p) is not None for m, p in prompts.items())
        note = f"Загружено вручную: {have} из {len(prompts)}."
        state = (self.studio.get(project_id) or {}).get("steps", {}).get(kind, {})
        if have == len(prompts) or state.get("status") == "done":
            self.studio.mark_done(project_id, kind, note=note, resume=True)
        else:
            self.studio.note(project_id, kind, note)
        return self.view(project_id)

    def save_artifact(self, project_id: str, name: str, payload: object) -> dict:
        if name not in ARTIFACTS:
            raise not_found("Нет такого результата.")
        if not isinstance(payload, dict) or not isinstance(payload.get("text"), str):
            raise bad_request("Нужно поле text.")
        filename, step = ARTIFACTS[name]
        folder = self.studio.project_dir(project_id)
        text = payload["text"]
        if len(text) > MAX_TEXT:
            raise bad_request(f"Длиннее {MAX_TEXT} знаков.")
        if name == "text" and not text.strip():
            raise bad_request("Текст пуст.")
        if name == "plan":
            try:
                data = json.loads(text)
                plan = slides.parse_plan(data, slides.paragraphs((folder / steps.TEXT).read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError, StudioError) as exc:
                raise bad_request(f"План не годится: {exc}") from exc
            data = data if isinstance(data, dict) else {"slides": data}
            text = json.dumps(data | {"slides": [s.to_dict() for s in plan]}, ensure_ascii=False, indent=2)
        if name == "short":
            try:
                beats = steps.parse_short(json.loads(text))
            except (json.JSONDecodeError, StudioError) as exc:
                raise bad_request(f"Сценарий шортса не годится: {exc}") from exc
            text = json.dumps({"beats": beats}, ensure_ascii=False, indent=2)
        with self.studio.idle(project_id):  # выпуск не запустится между проверкой и записью
            atomic_write(folder / filename, text.strip() + "\n")
        if name == "short":

            def keep_script(project: dict) -> set[str]:
                project["steps"]["short"]["keep"] = True
                return {"short"}

            self.studio.update(project_id, keep_script)
        elif name == "post":
            self.studio.update(project_id, lambda project: {"publish"})
        else:
            self.studio.mark_done(project_id, step, note="Поправлено вручную.")
        return self.view(project_id)

    def upload(self, project_id: str, what: str, data: bytes) -> dict:
        kinds = {"poster": (uploads_module.IMAGE,), "voice": (uploads_module.AUDIO,)}.get(what)
        if kinds is None:
            raise not_found("Загружать можно poster или voice.")
        ext = uploads_module.sniff(data) if data else None
        if ext is None or uploads_module.kind_of(ext) not in kinds:
            raise bad_request(uploads_module._accepted(kinds))
        self._guard_step(project_id, what)
        folder = self.studio.project_dir(project_id)
        stems = steps.IMAGE_EXT if what == "poster" else steps.AUDIO_EXT
        target = folder / f"{what}.{ext}"
        with self.studio.idle(project_id, what):
            # Сначала новый файл целиком, потом — прежний в другом формате: сбой между ними
            # не оставит выпуск без постера или озвучки.
            atomic_write(target, data)
            for old in (folder / f"{what}{e}" for e in stems):
                if old != target:
                    old.unlink(missing_ok=True)
            (folder / f"{what}.json").unlink(missing_ok=True)  # постер больше не из задачи «Фото»
        self.studio.mark_done(project_id, what, note="Загружено вручную.", resume=True)
        return self.view(project_id)

    def _guard(self, project_id: str) -> None:
        project = self.studio.get(project_id)
        if project is None:
            raise not_found("Проект не найден.")
        if project["running"]:
            raise HttpError(HTTPStatus.CONFLICT, "Проект сейчас выполняется — дождись или останови его.")

    def _guard_step(self, project_id: str, step_id: str) -> None:
        """Файл для шага можно класть и во время запуска — если шаг и зависящие от него не выполняются."""
        if self.studio.get(project_id) is None:
            raise not_found("Проект не найден.")
        try:
            self.studio.check_idle(project_id, step_id)
        except Conflict as exc:
            raise HttpError(HTTPStatus.CONFLICT, str(exc)) from exc

    def update_settings(self, project_id: str, payload: object) -> dict:
        project = self.studio.get(project_id)
        if project is None:
            raise not_found("Проект не найден.")
        # Выпуск из прошлой версии: недостающие настройки — по умолчанию, отменённые — выбрасываем.
        base = {k: v for k, v in (self.defaults() | project["settings"]).items() if k in SETTING_STEPS}
        new = self.check_settings(payload, base)
        changed = {k for k in new if new[k] != base[k]}

        def apply(p: dict) -> set[str]:
            p["settings"] = new
            touched = (SETTING_STEPS[k] for k in changed)
            return {s for v in touched if v for s in ((v,) if isinstance(v, str) else v)}

        self.studio.update(project_id, apply)
        return self.view(project_id)

    def rerun(self, project_id: str, payload: object) -> dict:
        """«Выполнить/Перезапустить шаг»: с settings — сначала параметры из карточки шага."""
        only = payload.get("step") if isinstance(payload, dict) else None
        if only is not None and only not in STEP_IDS:
            raise bad_request("Нет такого шага.")
        settings = payload.get("settings") if isinstance(payload, dict) else None
        if settings is not None:
            if only is None:
                raise bad_request("settings передают вместе с шагом.")
            self._guard(project_id)
            self.update_settings(project_id, settings)
        self.studio.run(project_id, only)
        return self.view(project_id)

    # --- HTTP ---

    def handle(self, req: Request) -> None:
        try:
            self._route(req)
        except Conflict as exc:
            raise HttpError(HTTPStatus.CONFLICT, str(exc)) from exc
        except ProjectNotFound as exc:
            raise not_found("Проект не найден.") from exc

    @staticmethod
    def _follow(project_id: str) -> dict[str, str]:
        return {"Location": f"/api/studio/projects/{project_id}"}

    def _route(self, req: Request) -> None:
        match (req.method, req.sub):
            case ("GET", ["config"]):
                return req.send_json(self.describe())
            case ("GET", ["projects"]):
                return req.send_json({"projects": self.studio.list(req.arg("scenario"))})
            case ("POST", ["preflight"]):
                return req.send_json(self.check_draft(req.json(MAX_BODY)))
            case ("POST", ["stats", "refresh"]):
                return req.send_json(self.refresh_stats(), HTTPStatus.ACCEPTED)
            case ("POST", ["projects", pid, "stats"]):
                return req.send_json(self.manual_stats(pid, req.json()))
            case ("GET", ["health", "stats"]):
                return req.send_json(self.health_stats())
            case ("GET", ["board"]):
                return req.send_json(self.board(archived=req.arg("archived") in ("1", "true")))
            case ("POST", ["projects", pid, "split"]):
                return req.send_json(self.split(pid, req.json() or {}), HTTPStatus.CREATED)
            case ("POST", ["projects", pid, "summary"]):
                return req.send_json(self.summarize(pid), HTTPStatus.ACCEPTED)
            case ("POST", ["projects", pid, "clone"]):
                copy = self.clone(pid)
                return req.send_json(copy, HTTPStatus.CREATED, self._follow(copy["id"]))
            case ("POST", ["projects", pid, "archive"]):
                return req.send_json(self.archive(pid, req.json() or {}))
            case ("POST", ["projects"]):
                key = req.headers.get("Idempotency-Key")
                made = self.create_once(req.json(MAX_BODY), key)
                return req.send_json(made, HTTPStatus.CREATED, self._follow(made["id"]))
            case ("GET", ["projects", pid]):
                return req.send_json(self.view(pid))
            case ("POST", ["projects", pid, "settings"]):
                return req.send_json(self.update_settings(pid, req.json()))
            case ("POST", ["projects", pid, "run"]):
                payload = req.json() or {}
                # Асинхронное действие: 202 и ссылка на выпуск, за которым следить (docs/api.md).
                return req.send_json(self.rerun(pid, payload), HTTPStatus.ACCEPTED, self._follow(pid))
            case ("POST", ["projects", pid, "cancel"]):
                self.studio.cancel(pid)
                return req.send_json(self.view(pid))
            case ("POST", ["projects", pid, "approve"]):
                payload = req.json() or {}
                step = payload.get("step") if isinstance(payload, dict) else None
                if step not in STEP_IDS:
                    raise bad_request("Нет такого шага.")
                self.studio.approve(pid, step)
                return req.send_json(self.view(pid))
            case ("POST", ["projects", pid, "publish"]):
                return req.send_json(self.publish_now(pid, req.json()), HTTPStatus.ACCEPTED, self._follow(pid))
            case ("POST", ["projects", pid, "publish", "resolve"]):
                return req.send_json(self.resolve_publish(pid, req.json()))
            case ("POST", ["projects", pid, "uploaded"]):
                return req.send_json(self.uploaded(pid, req.json()))
            case ("POST", ["projects", pid, "artifacts", name]):
                return req.send_json(self.save_artifact(pid, name, req.json(MAX_BODY)))
            case ("POST", ["projects", pid, "upload", what]):
                data = req.body(uploads_module.MAX_AUDIO_BYTES, "Файл больше 200 МБ.")
                return req.send_json(self.upload(pid, what, data))
            case ("POST", ["projects", pid, "upload", kind, key]):
                data = req.body(uploads_module.MAX_AUDIO_BYTES, "Файл больше 200 МБ.")
                return req.send_json(self.upload_slide(pid, kind, key, data))
            case ("GET", ["projects", pid, "files", *rest]):
                return self._send_file(req, pid, "/".join(rest))
        raise not_found()

    def _send_file(self, req: Request, project_id: str, rel: str) -> None:
        if self.studio.get(project_id) is None or not _FILE.match(rel):
            raise not_found("Файл не найден.")
        folder = self.studio.project_dir(project_id).resolve()
        path = (folder / rel).resolve()
        if folder not in path.parents or not path.is_file():
            raise not_found("Файл не найден.")
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if ctype.startswith("text/"):
            ctype += "; charset=utf-8"
        attachment = f"{project_id}-{rel.replace('/', '-')}" if "download" in req.query else None
        req.send_file(path, ctype, ranged=True, attachment=attachment, cache="no-cache")

    def close(self) -> None:
        self.scheduler.close()
        self.studio.close()
