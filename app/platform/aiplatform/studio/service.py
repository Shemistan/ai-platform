"""Вкладки сценариев «Дайджест новостей», «Видео по статье» и «Пост в канал»: API под /api/studio/."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import re
from http import HTTPStatus

from aiplatform.audio import voices
from aiplatform.core import uploads as uploads_module
from aiplatform.core.http import HttpError, Request, bad_request, not_found
from aiplatform.photo import models as photo_models
from aiplatform.studio import config as studio_config
from aiplatform.studio import news, publish, slides, steps
from aiplatform.studio.cache import MediaCache, place
from aiplatform.studio.llm import StudioError
from aiplatform.studio.pipeline import LABELS, STEP_IDS, Conflict, Studio, blockers, dependents
from aiplatform.studio.render import Renderer
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
    "branding": ("render", "short"),
    "tempo": ("render", "short"),
    "frame": "render",  # готовые картинки остаются — кадр берёт их с обрезкой; новые рисуются под кадр
    "review_text": None,
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
    r"|images/\d{2}\.(png|jpg|webp)|clips/\d{2}\.(mp4|mov|webm)"
    r"|publish/[a-z0-9_-]+/(video\.mp4|title\.txt|description\.txt))$"
)
_SLIDE = re.compile(r"^\d{2}$")
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
BOARD_GROUPS = {"attention": "Ждут тебя", "running": "Идут", "published": "Опубликованы", "drafts": "Черновики"}


def source_id(scenario: str, *, day: str | None = None, text: str = "") -> str:
    """По чему узнаём тот же материал: у дайджеста — день, у статьи и поста — текст без лишних пробелов."""
    if scenario == "digest":
        return f"digest:{day}"
    return f"{scenario}:" + hashlib.sha256(" ".join(text.split()).encode("utf-8")).hexdigest()[:16]


def board_group(item: dict, project: dict) -> str:
    """Куда выпуск на доске: идёт, ждёт человека, опубликован или черновик (не начат, остановлен)."""
    if item["running"]:
        return "running"
    if item["attention"]:
        return "attention"
    if project.get("steps", {}).get("publish", {}).get("status") == "done":
        return "published"
    return "drafts"


def _read_text(path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _read_json_list(path) -> list[dict]:
    try:
        data = json.loads(_read_text(path) or "[]")
    except json.JSONDecodeError:
        return []
    return [r for r in data if isinstance(r, dict) and r.get("channel")] if isinstance(data, list) else []


class StudioModule:
    prefix = PREFIX
    label = "Сценарии"

    def __init__(self, ctx) -> None:
        self.ctx = ctx
        self.config = studio_config.load(ctx.config)
        self.studio = Studio(self.config.projects_dir, steps.STEPS)

    def bind(self) -> None:
        """Соседние вкладки готовы — сценарии будут ставить задачи в их очереди."""
        self.studio.services = steps.Services(
            config=self.config,
            photo=self.ctx.modules["photo"].jobs,
            audio=self.ctx.modules["audio"].jobs,
            video=self.ctx.modules["video"].jobs,
            renderer=Renderer(self.config.ffmpeg, self.config.font, self.config.render_scale),
            cache=MediaCache(self.config.cache_dir),
        )

    # --- описание ---

    def channels(self) -> list[publish.Channel]:
        try:
            return self.config.channels()
        except ValueError:
            return []

    def add_channel(self, payload: object) -> dict:
        """Канал Telegram со страницы: список целиком записывается в channels.json."""
        if not isinstance(payload, dict):
            raise bad_request("Тело запроса — не объект JSON.")
        current = self._channels_or_400()
        try:
            channel = publish.new_channel(
                current,
                str(payload.get("label") or ""),
                str(payload.get("chat_id") or ""),
                str(payload.get("format") or publish.LANDSCAPE),
            )
        except ValueError as exc:
            raise bad_request(str(exc)) from exc
        publish.save_channels(self.config.channels_file, [*current, channel])
        return {"channels": [c.public() for c in self.channels()], "added": channel.id}

    def delete_channel(self, channel_id: str) -> dict:
        current = self._channels_or_400()
        if channel_id not in {c.id for c in current}:
            raise not_found("Нет такого канала.")
        publish.save_channels(self.config.channels_file, [c for c in current if c.id != channel_id])
        return {"channels": [c.public() for c in self.channels()]}

    def _channels_or_400(self) -> list[publish.Channel]:
        try:
            return self.config.channels()
        except ValueError as exc:
            raise bad_request(f"Каналы: {exc}") from exc

    def publish_now(self, project_id: str, payload: object) -> dict:
        """Кнопка «Опубликовать», как `make news-publish`: выбранные каналы, повторно — с force."""
        if not isinstance(payload, dict):
            raise bad_request("Тело запроса — не объект JSON.")
        chosen, force = payload.get("channels"), payload.get("force", False)
        ignore_verdict = payload.get("ignore_verdict", False)
        if not isinstance(chosen, list) or not chosen or not all(isinstance(c, str) for c in chosen):
            raise bad_request("Выбери хотя бы один канал.")
        unknown = [c for c in chosen if c not in {ch.id for ch in self.channels()}]
        if unknown:
            raise bad_request(f"Нет таких каналов: {', '.join(unknown)}.")
        if not isinstance(force, bool) or not isinstance(ignore_verdict, bool):
            raise bad_request("force и ignore_verdict — true или false.")

        def apply(project: dict) -> set[str]:
            project["settings"]["channels"] = list(dict.fromkeys(chosen))
            return {"publish"}

        self.studio.update(project_id, apply)
        state = {"approved": True, "force": force, "ignore_verdict": ignore_verdict}
        self.studio.run(project_id, "publish", state=state)
        return self.view(project_id)

    # --- доска выпусков ---

    def board(self, archived: bool = False) -> dict:
        """Все выпуски всех сценариев по группам: ждёт тебя, идёт, опубликовано, черновики."""
        channels = {c.id: c.label for c in self.channels()}
        items = []
        for item in self.studio.list(archived=archived):
            project = self.studio.get(item["id"]) or {}
            folder = self.studio.project_dir(item["id"])
            published = _read_json_list(folder / steps.PUBLISHED)
            chosen = project.get("settings", {}).get("channels") or []
            review = _read_text(folder / steps.NARRATOR_REVIEW)
            items.append(
                item
                | {
                    "scenario_label": SCENARIOS.get(item["scenario"], item["scenario"]),
                    "group": board_group(item, project),
                    "channels": [{"id": c, "label": channels.get(c, c)} for c in chosen],
                    "published": [
                        {"channel": r["channel"], "label": r.get("label") or channels.get(r["channel"], r["channel"])}
                        for r in published
                        if r.get("ok") and not r.get("skipped")
                    ],
                    "verdict": steps.verdict(review) if review else None,
                    "poster": self._poster_url(item["id"], folder),
                }
            )
        counts = {g: sum(i["group"] == g for i in items) for g in BOARD_GROUPS}
        return {"groups": list(BOARD_GROUPS.items()), "counts": counts, "projects": items}

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
            "disclaimers": True,
            "branding": False,
            "tempo": 1.0,
            "frame": "landscape",
            "auto_publish": False,
            "post_format": "video",
            "channels": [],
        }

    def scenario_defaults(self, scenario: str) -> dict:
        """Чем настройки сценария отличаются от общих: их и показывает форма нового выпуска."""
        if scenario == "digest":
            # Как в narrator: оформление канала (плашка «подпишись», плей) и обложка из ChatGPT
            # по промпту выпуска — постер загружает человек.
            out: dict = {"branding": True, "poster_mode": "upload", "tempo": 1.25, "frame": "poster"}
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
            out.append(f"Сценарии: LLM-команда «{self.config.llm_cmd[0]}» не найдена — задай AIP_LLM_CMD.")
        try:
            self.config.channels()
        except ValueError as exc:
            out.append(f"Каналы: {exc}")
        return out

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
        for key in ("draw_images", "clips", "shorts", "review_text", "auto_publish", "disclaimers", "branding"):
            if not isinstance(out[key], bool):
                raise bad_request(f"{key} — true или false.")
        tempo = out["tempo"]
        if isinstance(tempo, bool) or not isinstance(tempo, int | float) or float(tempo) not in steps.TEMPOS:
            raise bad_request(f"tempo — одно из: {', '.join(f'{t:g}' for t in steps.TEMPOS)}.")
        out["tempo"] = float(tempo)
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
            self._no_duplicate(extra["source"], force_new)
            project = self.studio.create(scenario, title, settings, extra)
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
            self._no_duplicate(extra["source"], force_new)
            project = self.studio.create(scenario, title, settings, extra)
            folder = self.studio.project_dir(project["id"])
            (folder / steps.POST).write_text(post.strip() + "\n", encoding="utf-8")
            if text.strip():
                (folder / steps.TEXT).write_text(text.strip() + "\n", encoding="utf-8")
        else:
            text = payload.get("text")
            if not isinstance(text, str) or not text.strip():
                raise bad_request("Вставь текст статьи.")
            if len(text) > MAX_TEXT:
                raise bad_request(f"Текст длиннее {MAX_TEXT} знаков.")
            paras = slides.paragraphs(text)
            title = title or (paras[0][:90] if paras else "Видео по статье")
            extra["source"] = source_id(scenario, text=text)
            self._no_duplicate(extra["source"], force_new)
            project = self.studio.create(scenario, title, settings, extra)
            (self.studio.project_dir(project["id"]) / steps.TEXT).write_text(text.strip() + "\n", encoding="utf-8")
        if payload.get("run"):
            self.studio.run(project["id"])
        return self.view(project["id"])

    def _no_duplicate(self, source: str, force_new: bool) -> None:
        """Выпуск по тому же дню или тексту уже есть — предложить открыть его (force_new — создать новый)."""
        if force_new:
            return
        existing = next((p for p in self.studio.list() if self._source(p) == source), None)
        if existing is not None:
            raise HttpError(
                HTTPStatus.CONFLICT,
                f"Уже есть выпуск «{existing['title']}» по этому же материалу — открой его или создай новый.",
                {"existing": {"id": existing["id"], "title": existing["title"], "scenario": existing["scenario"]}},
            )

    def _source(self, item: dict) -> str | None:
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
        project["published"] = as_json(steps.PUBLISHED)
        review = read(steps.NARRATOR_REVIEW)
        project["review"] = {"verdict": steps.verdict(review), "text": review} if review else None
        found = registry.mentioned(f"{project['title']}\n{read(steps.TEXT)}\n{read(steps.POST)}", self.banned())
        project["disclaimers"] = [{"name": e.name, "note": e.disclaimer + "."} for e in found]
        return project

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
        self._guard(project_id)
        path = folder / filename
        path.write_text(text.strip() + "\n", encoding="utf-8")
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
        for old in (folder / f"{what}{e}" for e in stems):
            old.unlink(missing_ok=True)
        (folder / f"{what}.{ext}").write_bytes(data)
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
        except KeyError as exc:
            raise not_found("Проект не найден.") from exc

    def _route(self, req: Request) -> None:
        match (req.method, req.sub):
            case ("GET", ["config"]):
                return req.send_json(self.describe())
            case ("GET", ["projects"]):
                return req.send_json({"projects": self.studio.list(req.arg("scenario"))})
            case ("GET", ["board"]):
                return req.send_json(self.board(archived=req.arg("archived") in ("1", "true")))
            case ("POST", ["projects", pid, "clone"]):
                return req.send_json(self.clone(pid), HTTPStatus.CREATED)
            case ("POST", ["projects", pid, "archive"]):
                return req.send_json(self.archive(pid, req.json() or {}))
            case ("POST", ["projects"]):
                return req.send_json(self.create(req.json(MAX_BODY)), HTTPStatus.CREATED)
            case ("GET", ["projects", pid]):
                return req.send_json(self.view(pid))
            case ("POST", ["projects", pid, "settings"]):
                return req.send_json(self.update_settings(pid, req.json()))
            case ("POST", ["projects", pid, "run"]):
                payload = req.json() or {}
                return req.send_json(self.rerun(pid, payload))
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
                return req.send_json(self.publish_now(pid, req.json()))
            case ("POST", ["channels"]):
                return req.send_json(self.add_channel(req.json()), HTTPStatus.CREATED)
            case ("POST", ["channels", channel_id, "delete"]):
                return req.send_json(self.delete_channel(channel_id))
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
        self.studio.close()
