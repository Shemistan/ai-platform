"""Проекты «Моушна»: хранение на диске, очередь из одного воркера, конвейер агентов.

make:  звук → паузы → расшифровка (GPU-замок; из «Монтажа» — готовая) → редактор смысла → дизайнер →
       сцены параллельно (режиссёр → техред → аниматор → рендер → превью) → пакет
scene: одна сцена заново по замечанию автора (режиссёр → техред → аниматор → рендер → превью) → пакет
add:   новая сцена в указанный момент по просьбе автора → пакет

Исходник не меняется; результат — scenes/<id>/scene.mov (ProRes 4444 с альфой) и package.zip.
"""

from __future__ import annotations

import copy
import json
import queue
import re
import shutil
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from aiplatform.core import log, media, runner, schema
from aiplatform.core import uploads as uploads_module
from aiplatform.core.fsutil import atomic_write
from aiplatform.core.gate import Gate
from aiplatform.montage import asr, stages, transcript
from aiplatform.montage import motion as montage_motion
from aiplatform.motion import agents, scenes, source
from aiplatform.motion.config import MotionConfig
from aiplatform.studio import llm

SCHEMA = 1
FIELDS = schema.require("id", "status", "created_at", "progress", types={"id": str, "status": str})
ACTIVE = ("queued", "running")
LOG_TAIL = 200
GPU_OWNER = "Моушн"
MAX_TITLE = 120
MAX_BRIEF = 2000
MAX_TERMS = 2000
MAX_FEEDBACK = 4000
MAX_FPS = 60
ANIMATE_TRIES = 2  # аниматор: первая версия + одна починка по ошибке проверки или рендера
PREVIEW_HEIGHT = 720
SIZES = {"1080": "1080p", "source": "Как у исходника"}
STAGES = {
    "audio": ("Звук из видео", 3),
    "silences": ("Паузы", 2),
    "transcribe": ("Расшифровка", 30),
    "moments": ("Редактор смысла: где нужны вставки", 8),
    "design": ("Дизайнер: язык выпуска", 4),
    "scenes": ("Сцены: режиссёр → техред → аниматор → рендер", 50),
    "package": ("Пакет для монтажки", 3),
}
STEPS = {
    "storyboard": "режиссёр ставит сцену",
    "review": "техред проверяет",
    "animate": "аниматор пишет код",
    "render": "рендер",
    "preview": "превью",
}
STEP_SHARE = {"storyboard": 0.0, "review": 0.2, "animate": 0.35, "render": 0.6, "preview": 0.9}
INSTALL_HINT = montage_motion.INSTALL_HINT
_ID = re.compile(r"^[0-9a-f]{12}$")
_SCENE = re.compile(r"^[0-9a-f]{8}$")
DEFAULT_OPTIONS = {"model": asr.DEFAULT_ID, "max_scenes": 0, "size": "1080", "brief": "", "terms": ""}
SCENE_FILES = {
    "preview.mp4": "video/mp4",
    "poster.jpg": "image/jpeg",
    "scene.mov": "video/quicktime",
    "index.html": "text/plain; charset=utf-8",  # исходник для правки — не страница
}


class ValidationError(ValueError):
    pass


class Conflict(ValueError):
    """Действие не к месту: проект занят или ещё не дошёл до нужного шага."""


def auto_scenes(duration: float) -> int:
    """Сколько сцен просить, если автор не задал: одна на ~2,5 минуты, от 3 до 15."""
    return int(min(15, max(3, round(duration / 150))))


def clean_options(payload: object, base: dict | None = None) -> dict:
    data = payload if isinstance(payload, dict) else {}
    out = dict(base or DEFAULT_OPTIONS)
    if "model" in data:
        if asr.get(data["model"]) is None:
            raise ValidationError("Неизвестная модель расшифровки.")
        out["model"] = data["model"]
    if "max_scenes" in data:
        value = data["max_scenes"]
        if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 30:
            raise ValidationError("Сцен — целое от 0 (сам решит) до 30.")
        out["max_scenes"] = value
    if "size" in data:
        if data["size"] not in SIZES:
            raise ValidationError(f"Размер — один из: {', '.join(SIZES)}.")
        out["size"] = data["size"]
    for key, limit, what in (("brief", MAX_BRIEF, "Пожелания"), ("terms", MAX_TERMS, "Термины")):
        if key in data:
            if not isinstance(data[key], str) or len(data[key]) > limit:
                raise ValidationError(f"{what} — строка до {limit} знаков.")
            out[key] = data[key].strip()
    return out


def new_stages(ids: list[str]) -> list[dict]:
    return [
        {"id": i, "label": STAGES[i][0], "weight": STAGES[i][1], "status": "pending", "percent": 0.0, "detail": "",
         "started_at": None, "finished_at": None}
        for i in ids
    ]  # fmt: skip


class MotionProjects:
    def __init__(self, config: MotionConfig, gate: Gate, url_base: str = "/api/motion") -> None:
        self.config = config
        self._gate = gate
        self._url = url_base
        self._lock = threading.RLock()
        self._render_lock = threading.Lock()  # HyperFrames — по одному: браузер тяжёлый
        self._package_lock = threading.Lock()  # package.zip пишут воркер и переключатель «В пакете»
        self._projects: dict[str, dict] = {}
        self._cancels: dict[str, threading.Event] = {}
        # Элемент очереди несёт своё событие отмены: отменённый и поставленный заново проект не запустит старое.
        self._queue: queue.Queue[tuple[str, str, dict, threading.Event] | None] = queue.Queue()
        self._batches: dict[str, list[dict]] = {}  # сцены текущего действия — для процента этапа
        config.projects_dir.mkdir(parents=True, exist_ok=True)
        self.load_problems: list[str] = []
        self._load()
        self._worker = threading.Thread(target=self._work, name="motion-worker", daemon=True)
        self._worker.start()

    # ---------- чтение ----------

    def dir(self, pid: str) -> Path:
        return self.config.projects_dir / pid

    def get(self, pid: str) -> dict | None:
        with self._lock:
            project = self._projects.get(pid)
            return self._view(project) if project else None

    def active(self) -> list[dict]:
        """Идущие и ждущие очереди — для центра активности шапки (UX-036): без копии проекта."""
        with self._lock:
            return [{"id": p["id"], "title": p.get("title", ""), "status": p["status"]} for p in self._projects.values()
                    if p["status"] in ACTIVE]  # fmt: skip

    def list(self) -> list[dict]:
        with self._lock:
            items = sorted(self._projects.values(), key=lambda p: p["created_at"], reverse=True)
            out = []
            for p in items:
                view = {k: v for k, v in self._view(p).items() if k not in ("log", "scenes")}
                view["scenes_done"] = sum(1 for s in p["scenes"] if s["status"] == "done")
                out.append(view)
            return out

    def _view(self, project: dict) -> dict:
        view = copy.deepcopy(project)
        pid = project["id"]
        base = f"{self._url}/projects/{pid}"
        folder = self.dir(pid)
        view["source_url"] = f"{base}/source" if project.get("source") else None
        view["package_url"] = f"{base}/package" if (folder / "package.zip").is_file() else None
        fps = (project.get("source") or {}).get("fps") or 30
        for scene in view["scenes"]:
            sid = scene["id"]
            scene["urls"] = {
                name: f"{base}/scenes/{sid}/{name}"
                for name in SCENE_FILES
                if (folder / "scenes" / sid / name).is_file()
            }
            scene["timecode"] = scenes.timecode(scene["at"], fps)
            scene["timecode_end"] = scenes.timecode(scene["end"], fps)
            scene["file_name"] = scenes.file_name(scene, fps)
        now = time.time()
        view["eta_s"] = stages.remaining(view["stages"], now) if project["status"] == "running" else None
        for stage in view["stages"]:
            stage["eta_s"] = stages.stage_remaining(stage, now)
        if project["status"] == "queued":
            view["queue_position"] = sum(
                1
                for p in self._projects.values()
                if p["status"] in ACTIVE and p.get("queued_at", 0) < project.get("queued_at", 0)
            )
        return view

    def _read_json(self, pid: str, name: str, default=None):
        try:
            return json.loads((self.dir(pid) / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return default

    def words(self, pid: str) -> list[dict]:
        return (self._read_json(pid, "words.json") or {}).get("words") or []

    def file(self, pid: str, name: str) -> Path | None:
        if pid not in self._projects:
            return None
        path = self.dir(pid) / name
        return path if path.is_file() else None

    def source_file(self, pid: str) -> Path | None:
        with self._lock:
            project = self._projects.get(pid)
            meta = project.get("source") if project else None
        return self.file(pid, f"source.{meta['ext']}") if meta else None

    def scene_file(self, pid: str, sid: str, name: str) -> Path | None:
        if not _SCENE.match(sid) or name not in SCENE_FILES:
            return None
        return self.file(pid, f"scenes/{sid}/{name}")

    def montage_list(self) -> list[dict]:
        return source.finished(self.config.montage_projects_dir)

    # ---------- запись ----------

    def create(self, payload: object) -> dict:
        data = payload if isinstance(payload, dict) else {}
        title = data.get("title", "")
        if not isinstance(title, str) or len(title.strip()) > MAX_TITLE:
            raise ValidationError(f"Название — строка до {MAX_TITLE} знаков.")
        options = clean_options(data.get("options"), DEFAULT_OPTIONS | {"model": asr.default_id(self.config.montage)})
        origin = data.get("montage")
        montage_dir = None
        if origin is not None:
            if not isinstance(origin, str) or not _ID.match(origin):
                raise ValidationError("montage — id ролика «Монтажа».")
            found = next((p for p in self.montage_list() if p["id"] == origin), None)
            if found is None:
                raise ValidationError("Ролик «Монтажа» не найден или ещё не собран.")
            montage_dir = self.config.montage_projects_dir / origin
            title = title or found["title"]
        pid = uuid.uuid4().hex[:12]
        now = time.time()
        project = {
            "id": pid,
            "title": " ".join(title.split()) or time.strftime("Моушн %d.%m %H:%M"),
            "status": "draft",
            "action": None,
            "created_at": now,
            "updated_at": now,
            "backend": self.config.montage.backend,
            "options": options,
            "origin": {"montage": origin} if origin else None,
            "source": None,
            "transcript": None,
            "topic": "",
            "design": None,
            "scenes": [],
            "dropped": [],
            "progress": {"stage": "Ждёт видео", "percent": 0.0},
            "stages": [],
            "error": None,
            "history": [],
            "log": [],
        }
        if montage_dir is not None:
            folder = self.dir(pid)
            folder.mkdir(parents=True, exist_ok=True)
            target = folder / "source.mp4"
            source.link(montage_dir / "output.mp4", target)
            words = source.output_words(montage_dir)
            if not words:
                shutil.rmtree(folder, ignore_errors=True)
                raise ValidationError("В ролике «Монтажа» нет расшифровки.")
            atomic_write(folder / "words.json", json.dumps({"words": words, "model": "montage"}, ensure_ascii=False))
            project["source"] = {**self._probe(target), "ext": "mp4"}
            project["transcript"] = {"model": "montage", "words": len(words)}
            project["progress"] = {"stage": "Видео и расшифровка из «Монтажа»", "percent": 0.0}
        with self._lock:
            self._projects[pid] = project
            self._cancels[pid] = threading.Event()
            self._save(project)
            return self._view(project)

    def update(self, pid: str, payload: object) -> dict | None:
        data = payload if isinstance(payload, dict) else {}
        with self._lock:
            project = self._projects.get(pid)
            if project is None:
                return None
            self._idle(project)
            if "title" in data:
                title = data["title"]
                if not isinstance(title, str) or not title.strip() or len(title) > MAX_TITLE:
                    raise ValidationError(f"Название — непустая строка до {MAX_TITLE} знаков.")
                project["title"] = " ".join(title.split())
            if "options" in data:
                project["options"] = clean_options(data["options"], project["options"])
            self._touch(project)
            return self._view(project)

    def save_source(self, pid: str, tmp: Path) -> dict | None:
        """Видео (уже на диске, body_to_file) → source.<ext>; расшифровка прошлого видео — в сторону."""
        try:
            with self._lock:
                project = self._projects.get(pid)
                if project is None:
                    return None
                self._idle(project)
            ext = uploads_module.sniff_video(uploads_module.head(tmp)) if tmp.stat().st_size else None
            if ext is None:
                raise ValidationError("Поддерживаются видео MP4, MOV, WebM.")
            meta = self._probe(tmp)
            if not meta["has_audio"]:
                raise ValidationError("В видео нет звука — без речи агентам не о чем думать.")
            folder = self.dir(pid)
            for old in folder.glob("source.*"):
                old.unlink()
            (folder / "words.json").unlink(missing_ok=True)
            tmp.replace(folder / f"source.{ext}")
        finally:
            tmp.unlink(missing_ok=True)
        with self._lock:
            project = self._projects.get(pid)
            if project is None:
                return None
            project["source"] = {**meta, "ext": ext}
            project["origin"] = None
            project["transcript"] = None
            project["progress"] = {"stage": "Видео загружено", "percent": 0.0}
            self._history(project, "source", f"видео: {scenes.clock(meta['duration'])}")
            self._touch(project)
            return self._view(project)

    def start(self, pid: str) -> dict | None:
        return self._enqueue(pid, "make", {})

    def redo_scene(self, pid: str, sid: str, text: object) -> dict | None:
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_FEEDBACK:
            raise ValidationError(f"Замечание — непустой текст до {MAX_FEEDBACK} знаков.")
        with self._lock:
            project = self._projects.get(pid)
            if project is not None and not any(s["id"] == sid for s in project["scenes"]):
                raise ValidationError("Нет такой сцены.")
        return self._enqueue(pid, "scene", {"scene": sid, "text": text.strip()})

    def add_scene(self, pid: str, payload: object) -> dict | None:
        data = payload if isinstance(payload, dict) else {}
        at, text = data.get("at"), data.get("text")
        if not isinstance(at, int | float) or isinstance(at, bool) or at < 0:
            raise ValidationError("at — секунда ролика.")
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_FEEDBACK:
            raise ValidationError(f"Что показать — непустой текст до {MAX_FEEDBACK} знаков.")
        with self._lock:
            project = self._projects.get(pid)
            if project is not None and project.get("source") and at > project["source"]["duration"]:
                raise ValidationError("Момент дальше конца видео.")
        return self._enqueue(pid, "add", {"at": float(at), "text": text.strip()})

    def toggle_scene(self, pid: str, sid: str, enabled: object) -> dict | None:
        if not isinstance(enabled, bool):
            raise ValidationError("enabled — true или false.")
        with self._lock:
            project = self._projects.get(pid)
            if project is None:
                return None
            self._idle(project)
            scene = next((s for s in project["scenes"] if s["id"] == sid), None)
            if scene is None:
                raise ValidationError("Нет такой сцены.")
            scene["enabled"] = enabled
            self._touch(project)
        self._package(project)
        return self.get(pid)

    def cancel(self, pid: str) -> dict | None:
        with self._lock:
            project = self._projects.get(pid)
            if project is None:
                return None
            if project["status"] == "queued":
                self._finish(project, "canceled", stage="Отменено")
            elif project["status"] == "running":
                project["progress"]["stage"] = "Отмена…"
            event = self._cancels.get(pid)
            if event:
                event.set()
            return self._view(project)

    def delete(self, pid: str) -> bool:
        with self._lock:
            project = self._projects.get(pid)
            if project is None:
                return False
            if project["status"] in ACTIVE:
                raise Conflict("Проект в работе — сначала отмени.")
            del self._projects[pid]
            self._cancels.pop(pid, None)
        shutil.rmtree(self.dir(pid), ignore_errors=True)
        return True

    def close(self) -> None:
        with self._lock:
            for event in self._cancels.values():
                event.set()
        self._queue.put(None)
        self._worker.join(timeout=15)

    # ---------- внутреннее: состояние ----------

    def _idle(self, project: dict) -> None:
        if project["status"] in ACTIVE:
            raise Conflict("Проект в работе — дождись или отмени.")

    def _enqueue(self, pid: str, action: str, arg: dict) -> dict | None:
        with self._lock:
            project = self._projects.get(pid)
            if project is None:
                return None
            self._idle(project)
            if not project.get("source"):
                raise Conflict("Загрузи видео или возьми ролик из «Монтажа».")
            has_words = (self.dir(pid) / "words.json").is_file()
            if action != "make" and not (has_words and project.get("design")):
                raise Conflict("Сначала прогон целиком: нажми «Начать».")
            model = asr.get(project["options"]["model"])
            if action == "make" and not has_words and model is not None:
                if model.id in self.config.montage.disabled_models():
                    raise Conflict(f"Модель {model.label} выключена во вкладке «Настройки» — выбери другую.")
                if not asr.installed(model, self.config.montage):
                    raise Conflict(f"Модель {model.label} не установлена: make models M={model.id}.")
            if not self.config.montage.llm_available():
                raise Conflict(llm.missing_cli(self.config.montage.llm_cmd[0]))
            if not self.config.montage.hyperframes_available():
                raise Conflict(INSTALL_HINT)
            event = threading.Event()
            self._cancels[pid] = event
            project["status"] = "queued"
            project["action"] = action
            project["error"] = None
            project["progress"] = {"stage": "В очереди", "percent": 0.0}
            if action == "make":
                head = [] if has_words else ["audio", "silences", "transcribe"]
                ids = [*head, "moments", "design", "scenes", "package"]
            else:
                ids = ["scenes", "package"]
            project["stages"] = new_stages(ids)
            self._touch(project)
            project["queued_at"] = time.time()
            self._queue.put((pid, action, arg, event))
            return self._view(project)

    def _touch(self, project: dict) -> None:
        project["updated_at"] = time.time()
        self._save(project)

    def _save(self, project: dict) -> None:
        folder = self.dir(project["id"])
        folder.mkdir(parents=True, exist_ok=True)
        data = schema.stamp(project, SCHEMA)
        atomic_write(folder / "project.json", json.dumps(data, ensure_ascii=False, indent=2))

    def _load(self) -> None:
        for path in self.config.projects_dir.glob("*/project.json"):
            if not _ID.match(path.parent.name):
                continue
            label = f"Моушн {path.parent.name}"
            project, _ = schema.load_record(path, SCHEMA, check=FIELDS, problems=self.load_problems, label=label)
            if project is None:
                continue
            project.setdefault("stages", [])
            project.setdefault("scenes", [])
            if project["status"] in ACTIVE:
                project["status"] = "failed"
                project["error"] = "Сервис был перезапущен во время работы — запусти ещё раз."
                project["progress"]["stage"] = "Ошибка"
                stages.finish(project["stages"], "failed")
                for scene in project["scenes"]:
                    if scene["status"] in ("queued", "working"):
                        scene.update(status="failed", error="прервано перезапуском", step=None)
                self._save(project)
            self._projects[project["id"]] = project
            self._cancels[project["id"]] = threading.Event()

    def _history(self, project: dict, action: str, text: str) -> None:
        project.setdefault("history", []).append({"at": time.time(), "action": action, "text": text})
        del project["history"][:-50]

    def _finish(self, project: dict, status: str, *, error: str | None = None, stage: str | None = None) -> None:
        project["status"] = status
        project["error"] = error
        stages.finish(project.setdefault("stages", []), status)
        if status == "done":
            project["progress"] = {"stage": "Готово", "percent": 100.0}
        else:
            project["progress"]["stage"] = stage or ("Ошибка" if status == "failed" else "Отменено")
        for scene in project["scenes"]:
            if scene["status"] in ("queued", "working"):
                scene.update(status="canceled" if status == "canceled" else "failed", step=None)
                scene["error"] = scene.get("error") or ("отменено" if status == "canceled" else "не доделана")
        self._touch(project)

    def _stage(self, project: dict, cancel: threading.Event, stage_id: str, percent: float, detail: str = "") -> None:
        with self._lock:
            if cancel.is_set():
                return
            items = project.setdefault("stages", [])
            stages.advance(items, stage_id, percent, detail)
            label = STAGES.get(stage_id, (stage_id, 0))[0]
            project["progress"] = {"stage": f"{label}: {detail}" if detail else label, "percent": stages.overall(items)}

    def _note(self, project: dict, line: str) -> None:
        with self._lock:
            project["log"].append(line.rstrip()[:500])
            del project["log"][:-LOG_TAIL]

    # ---------- внутреннее: воркер ----------

    def _work(self) -> None:
        while (item := self._queue.get()) is not None:
            pid, action, arg, event = item
            with self._lock:
                project = self._projects.get(pid)
                cancel = self._cancels.get(pid)
                if project is None or cancel is not event or project["status"] != "queued":
                    continue
                project["status"] = "running"
                project["progress"] = {"stage": "Запуск", "percent": 0.0}
                self._touch(project)
            self._run(project, action, arg, cancel)

    def _run(self, project: dict, action: str, arg: dict, cancel: threading.Event) -> None:
        try:
            if action == "make":
                self._make(project, cancel)
            elif action == "scene":
                self._redo(project, arg["scene"], arg["text"], cancel)
            elif action == "add":
                self._add(project, arg["at"], arg["text"], cancel)
            self._stage(project, cancel, "package", 0)
            self._package(project)
            if cancel.is_set():
                raise runner.Canceled
        except runner.Canceled:
            with self._lock:
                self._finish(project, "canceled")
        except (runner.RunFailed, llm.StudioError, agents.AgentError, ValueError) as exc:
            with self._lock:
                self._finish(project, "failed", error=str(exc))
        except Exception as exc:  # noqa: BLE001 — воркер не должен умереть из-за одного проекта
            with self._lock:
                log.internal_error(exc, task=project["id"])
                project["log"].append(f"✗ {type(exc).__name__}: {exc}")
                self._finish(project, "failed", error=log.TASK_INTERNAL)
        else:
            with self._lock:
                self._finish(project, "done")

    # ---------- шаги: расшифровка ----------

    def _ffmpeg(self, args: list[str], cancel: threading.Event, what: str) -> None:
        lines: list[str] = []
        try:
            runner.run([self.config.ffmpeg, "-y", "-loglevel", "error", *args], lines.append, cancel, kind="render")
        except runner.RunFailed as exc:
            raise runner.RunFailed(f"ffmpeg ({what}): {(lines or [str(exc)])[-1].strip()}") from exc

    def _probe(self, path: Path) -> dict:
        argv = [media.ffprobe_for(self.config.ffmpeg), "-v", "error", "-show_entries",
                "format=duration:stream=codec_type,width,height,avg_frame_rate", "-of", "json", str(path)]  # fmt: skip
        try:
            info = json.loads(subprocess.run(argv, capture_output=True, text=True, check=True, timeout=60).stdout)
        except (subprocess.SubprocessError, ValueError, OSError) as exc:
            raise ValidationError("Файл не читается как видео.") from exc
        streams = info.get("streams") or []
        video = next((s for s in streams if s.get("codec_type") == "video"), None)
        if video is None:
            raise ValidationError("В файле нет видео.")
        duration = float((info.get("format") or {}).get("duration") or 0)
        if duration <= 0:
            raise ValidationError("Не удалось узнать длительность видео.")
        try:
            num, den = (video.get("avg_frame_rate") or "30/1").split("/")
            fps = float(num) / float(den or 1)
        except (ValueError, ZeroDivisionError):
            fps = 30.0
        return {
            "duration": round(duration, 3),
            "width": int(video.get("width") or 0),
            "height": int(video.get("height") or 0),
            "fps": round(fps, 3) if 1 <= fps <= 240 else 30.0,
            "has_audio": any(s.get("codec_type") == "audio" for s in streams),
            "size": path.stat().st_size,
        }

    def _transcribe(self, project: dict, cancel: threading.Event) -> None:
        pid = project["id"]
        folder = self.dir(pid)
        video = self.source_file(pid)
        if video is None:
            raise runner.RunFailed("Видео пропало из каталога проекта — загрузи его заново.")
        self._stage(project, cancel, "audio", 0)
        wav = folder / "audio.wav"
        self._ffmpeg(["-i", str(video), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav)], cancel,
                     "звук для расшифровки")  # fmt: skip
        self._stage(project, cancel, "silences", 0)
        duration = media.duration(wav, self.config.ffmpeg)
        lines: list[str] = []
        runner.run(transcript.silence_argv(self.config.ffmpeg, wav), lines.append, cancel, kind="render")
        silences = transcript.parse_silences(lines, duration)
        model = asr.get(project["options"]["model"]) or asr.CATALOG[0]
        hotwords = [t.strip() for t in re.split(r"[,\n;]", project["options"]["terms"]) if t.strip()]
        out = folder / "words.json"
        argv = asr.build_argv(self.config.montage, model, wav, out, splits=transcript.split_points(duration, silences),
                              hotwords=hotwords)  # fmt: skip

        def on_line(line: str) -> None:
            step = asr.parse_progress(line)
            if step:
                self._stage(project, cancel, "transcribe", step[0], step[1] or model.label)
            elif line.strip():
                self._note(project, line)

        def on_wait(holder: str) -> None:
            self._stage(project, cancel, "transcribe", 0, f"ждёт GPU (занят: {holder})")

        with self._gate.hold(GPU_OWNER, on_wait, cancel):
            self._stage(project, cancel, "transcribe", 0, model.label)
            runner.run(argv, on_line, cancel, missing_hint=asr_hint(), kind="asr")
        try:
            data = json.loads(out.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise runner.RunFailed("Расшифровка завершилась, но слов нет: файл результата не прочитался.") from exc
        words = [w for w in data.get("words") or [] if isinstance(w, dict) and str(w.get("w", "")).strip()]
        if not words:
            raise runner.RunFailed("Расшифровка не нашла речи в видео.")
        atomic_write(out, json.dumps({"words": words, "model": model.id}, ensure_ascii=False))
        wav.unlink(missing_ok=True)
        with self._lock:
            project["transcript"] = {"model": model.id, "words": len(words), "elapsed_s": data.get("elapsed_s")}
            self._history(project, "transcribe", f"слов: {len(words)}")
            self._touch(project)

    # ---------- шаги: агенты ----------

    def _ask(self, prompt: str, cancel: threading.Event) -> object:
        return llm.extract_json(llm.complete(prompt, cmd=self.config.montage.llm_cmd, cancel=cancel))

    def _make(self, project: dict, cancel: threading.Event) -> None:
        pid = project["id"]
        if not (self.dir(pid) / "words.json").is_file():
            self._transcribe(project, cancel)
        words = self.words(pid)
        duration = project["source"]["duration"]
        opts = project["options"]
        limit = opts["max_scenes"] or auto_scenes(duration)
        self._stage(project, cancel, "moments", 5, f"читает {len(words)} слов")
        prompt = agents.editor_prompt(words, duration, limit, project["title"], opts["brief"])
        topic, moments = agents.clean_moments(self._ask(prompt, cancel), words, duration, limit)
        atomic_write(self.dir(pid) / "moments.json", json.dumps({"topic": topic, "moments": moments},
                                                               ensure_ascii=False, indent=1))  # fmt: skip
        if not moments:
            raise agents.AgentError("Редактор смысла не нашёл моментов, где анимация поможет.")
        self._note(project, f"Редактор: тема «{topic}», моментов {len(moments)}")
        self._stage(project, cancel, "design", 10, "палитра, шрифты, персонажи")
        design = agents.clean_design(self._ask(agents.designer_prompt(topic, moments, project["title"], opts["brief"]),
                                               cancel))  # fmt: skip
        atomic_write(self.dir(pid) / "design.json", json.dumps(design, ensure_ascii=False, indent=1))
        old = self.dir(pid) / "scenes"
        shutil.rmtree(old, ignore_errors=True)
        with self._lock:
            project["topic"] = topic
            cast = [{k: c[k] for k in ("id", "name")} for c in design["characters"]]
            project["design"] = {"name": design["name"], "palette": design["palette"], "characters": cast}
            project["dropped"] = []
            project["scenes"] = [self._new_scene(n, m) for n, m in enumerate(moments, 1)]
            self._history(project, "agents", f"тема: {topic}; моментов: {len(moments)}; стиль: {design['name']}")
            self._touch(project)
        self._run_scenes(project, [(s, "") for s in project["scenes"]], cancel)

    def _new_scene(self, n: int, moment: dict) -> dict:
        return {
            "id": uuid.uuid4().hex[:8],
            "n": n,
            "moment": moment,
            "title": moment["title"],
            "concept": moment["concept"],
            "at": moment["at"],
            "end": moment["at"] + agents.MIN_SCENE_S,
            "layout": "",
            "status": "queued",
            "step": None,
            "enabled": True,
            "issues": [],
            "feedback": [],
            "error": None,
            "attempts": 0,
        }

    def _redo(self, project: dict, sid: str, text: str, cancel: threading.Event) -> None:
        with self._lock:
            scene = next((s for s in project["scenes"] if s["id"] == sid), None)
            if scene is None:
                raise ValidationError("Сцена пропала.")
            before = copy.deepcopy(scene) if scene["status"] == "done" else None
            scene.update(status="queued", step=None, error=None)
            scene["feedback"] = [*scene.get("feedback", []), text][-10:]
            self._history(project, "feedback", f"сцена {scene['n']}: {text}")
            self._touch(project)
        try:
            self._run_scenes(project, [(scene, text)], cancel)
        finally:
            # Переделка не удалась или отменена — прежняя готовая сцена остаётся (её файлы не трогались).
            if before is not None and scene["status"] != "done":
                why = scene.get("error") or "отменено"
                with self._lock:
                    scene.clear()
                    scene.update(before, feedback=[*before.get("feedback", []), text][-10:])
                    scene["error"] = f"переделка не удалась: {why}"
                    self._touch(project)

    def _add(self, project: dict, at: float, text: str, cancel: threading.Event) -> None:
        words = self.words(project["id"])
        a = scenes.word_index_at(words, at)
        b = scenes.word_index_at(words, words[a]["s"] + 20)
        moment = {"from": a, "to": max(a, b), "title": agents._text(text, 40), "concept": agents._text(text, 300),
                  "kind": "mechanism", "score": 10, "why": "просьба автора", "at": round(words[a]["s"], 2)}  # fmt: skip
        with self._lock:
            scene = self._new_scene(max((s["n"] for s in project["scenes"]), default=0) + 1, moment)
            scene["feedback"] = [text]
            project["scenes"].append(scene)
            self._history(project, "add", f"сцена {scene['n']} на {scenes.clock(at)}: {text}")
            self._touch(project)
        self._run_scenes(project, [(scene, text)], cancel)

    def _run_scenes(self, project: dict, items: list[tuple[dict, str]], cancel: threading.Event) -> None:
        """Сцены параллельно (AIP_MOTION_PARALLEL): у каждой свой путь по ролям; упавшая не роняет остальные."""
        pid = project["id"]
        words = self.words(pid)
        design = agents.clean_design(self._read_json(pid, "design.json"))
        topic = project.get("topic") or ""
        total = len(items)
        self._batches[pid] = [s for s, _ in items]
        self._scenes_progress(project, cancel, self._batches[pid])

        def one(item: tuple[dict, str]) -> None:
            scene, feedback = item
            try:
                self._scene(project, scene, words, design, topic, feedback, cancel)
            except runner.Canceled:
                return
            except (runner.RunFailed, llm.StudioError, agents.AgentError, ValueError) as exc:
                self._scene_state(project, scene, status="failed", step=None, error=str(exc)[:500])
                self._note(project, f"Сцена {scene['n']}: {exc}")
            except Exception as exc:  # noqa: BLE001 — одна сцена не роняет выпуск
                log.internal_error(exc, task=f"{pid}/{scene['id']}")
                self._scene_state(project, scene, status="failed", step=None, error=log.TASK_INTERNAL)
            finally:
                self._scenes_progress(project, cancel, self._batches[pid])

        with ThreadPoolExecutor(max_workers=min(self.config.parallel, total or 1)) as pool:
            list(pool.map(one, items))
        if cancel.is_set():
            raise runner.Canceled
        with self._lock:
            done = sum(1 for s, _ in items if s["status"] == "done")
            self._history(project, "scenes", f"готово {done} из {total}")
            self._touch(project)

    def _scenes_progress(self, project: dict, cancel: threading.Event, items: list[dict]) -> None:
        with self._lock:
            parts = []
            for s in items:
                if s["status"] in ("done", "failed", "dropped", "canceled"):
                    parts.append(1.0)
                else:
                    parts.append(STEP_SHARE.get(s.get("step") or "", 0.0))
            finished = sum(1 for s in items if s["status"] in ("done", "failed", "dropped"))
            busy = [f"{s['n']}: {STEPS[s['step']]}" for s in items if s.get("step") in STEPS][:3]
            detail = f"готово {finished} из {len(items)}" + (f" · {'; '.join(busy)}" if busy else "")
        self._stage(project, cancel, "scenes", 100 * sum(parts) / max(1, len(items)), detail)

    def _scene_state(self, project: dict, scene: dict, **fields) -> None:
        with self._lock:
            scene.update(fields)
            self._touch(project)

    def _step(self, project: dict, scene: dict, step: str, cancel: threading.Event) -> None:
        if cancel.is_set():
            raise runner.Canceled
        self._scene_state(project, scene, status="working", step=step)
        self._scenes_progress(project, cancel, self._batches.get(project["id"]) or [scene])

    def _scene(self, project, scene, words, design, topic, feedback, cancel) -> None:
        pid = project["id"]
        folder = self.dir(pid) / "scenes" / scene["id"]
        folder.mkdir(parents=True, exist_ok=True)
        # Готовую сцену переделываем рядом, в next/: файлы заменяются только целиком удавшейся версией.
        work = folder / "next" if (folder / "scene.mov").is_file() else folder
        shutil.rmtree(folder / "next", ignore_errors=True)
        work.mkdir(exist_ok=True)
        try:
            self._scene_in(project, scene, words, design, topic, feedback, cancel, folder, work)
        finally:
            if work != folder:
                shutil.rmtree(work, ignore_errors=True)

    def _scene_in(self, project, scene, words, design, topic, feedback, cancel, folder: Path, work: Path) -> None:
        moment = scene["moment"]
        opts = project["options"]
        current = self._read(folder / "storyboard.json") if feedback else None
        self._step(project, scene, "storyboard", cancel)
        prompt = agents.director_prompt(words, moment, design, topic, feedback=feedback, current=current,
                                        title=project["title"], brief=opts["brief"])  # fmt: skip
        board = agents.clean_storyboard(self._ask(prompt, cancel), words, moment, design)
        self._step(project, scene, "review", cancel)
        verdict, board, issues = agents.apply_review(
            self._ask(agents.expert_prompt(words, moment, board, topic, project["title"]), cancel),
            words, moment, board, design,
        )  # fmt: skip
        if verdict == "drop" and not feedback:
            self._scene_state(project, scene, status="dropped", step=None, issues=issues, error=None)
            self._note(project, f"Сцена {scene['n']} снята техредом: {issues[0] if issues else ''}")
            return
        atomic_write(work / "storyboard.json", json.dumps(board, ensure_ascii=False, indent=1))
        self._scene_state(project, scene, title=board["title"], layout=board["layout"], at=board["at"],
                          end=board["end"], idea=board["idea"], verdict=verdict, issues=issues,
                          beats=[{k: b[k] for k in ("t", "action", "text")} for b in board["beats"]])  # fmt: skip
        src = project["source"]
        size = agents.frame_size(src["width"], src["height"], opts["size"])
        fps = min(MAX_FPS, max(1, round(src.get("fps") or 30)))
        error, code = "", None
        for attempt in range(1, ANIMATE_TRIES + 1):
            self._step(project, scene, "animate", cancel)
            prompt = agents.animator_prompt(board, design, topic, size, error=error, previous=code)
            answer = self._ask(prompt, cancel)
            code = answer if isinstance(answer, dict) else {"answer": str(answer)[:2000]}
            try:
                code = agents.clean_code(answer)
                self._js_check(code, cancel)
            except agents.AgentError as exc:
                error = str(exc)
                self._note(project, f"Сцена {scene['n']}, аниматор (попытка {attempt}): {error}")
                if attempt == ANIMATE_TRIES:
                    raise
                continue
            atomic_write(work / "index.html", agents.composition(board, code, design, size))
            atomic_write(work / "code.json", json.dumps(code, ensure_ascii=False))
            self._step(project, scene, "render", cancel)
            try:
                self._render(project, work, fps, cancel)
                break
            except runner.RunFailed as exc:
                error = f"рендер упал: {exc}"
                self._note(project, f"Сцена {scene['n']}, {error}")
                if attempt == ANIMATE_TRIES:
                    raise
        self._step(project, scene, "preview", cancel)
        self._preview(project, work, board, cancel)
        if work != folder:
            for path in work.iterdir():
                path.replace(folder / path.name)
        self._scene_state(project, scene, status="done", step=None, error=None, attempts=attempt,
                          made_at=time.time())  # fmt: skip

    @staticmethod
    def _read(path: Path):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _js_check(self, code: dict, cancel: threading.Event) -> None:
        """Синтаксис тела таймлайна — `node --check` до рендера: дешевле, чем ждать браузер."""
        node = self.config.node()
        if node is None or self.config.fake:
            return
        with _scratch(self.config.data_dir) as tmp:
            script = tmp / "check.js"
            script.write_text(agents.js_check_source(code), encoding="utf-8")
            lines: list[str] = []
            try:
                runner.run([node, "--check", str(script)], lines.append, cancel, timeout_s=30)
            except runner.RunFailed as exc:
                detail = " ".join(x.strip() for x in lines if "SyntaxError" in x or "^" not in x)[-400:]
                raise agents.AgentError(f"синтаксическая ошибка в js: {detail or exc}") from exc

    def _render(self, project: dict, folder: Path, fps: int, cancel: threading.Event) -> None:
        mov = folder / "scene.mov"
        mov.unlink(missing_ok=True)
        argv = [*self.config.montage.hyperframes_cmd, "render", "--composition", "index.html", "--format", "mov",
                "--fps", str(fps), "--output", str(mov), "--quiet"]  # fmt: skip
        lines: list[str] = []

        def on_line(line: str) -> None:
            if line.strip():
                lines.append(line.strip())
                self._note(project, line)

        with self._render_lock, self._gate.hold(GPU_OWNER, None, cancel):
            try:
                runner.run(argv, on_line, cancel, cwd=folder, missing_hint=INSTALL_HINT, kind="render")
            except runner.RunFailed as exc:
                raise runner.RunFailed(" ".join(lines[-3:])[-500:] or str(exc)) from exc
        if not mov.is_file() or not mov.stat().st_size:
            raise runner.RunFailed("HyperFrames закончил, но ролика нет.")

    def _preview(self, project: dict, folder: Path, board: dict, cancel: threading.Event) -> None:
        """Кусок исходника с наложенной сценой, 720p H.264 — посмотреть в браузере; кадр-обложка из середины."""
        video = self.source_file(project["id"])
        if video is None:
            return
        length = board["end"] - board["at"]
        graph = (f"[0:v]scale=-2:{PREVIEW_HEIGHT},setsar=1[b];[1:v]scale=-2:{PREVIEW_HEIGHT}[o];"
                 "[b][o]overlay=0:0:eof_action=pass:format=auto,format=yuv420p[v]")  # fmt: skip
        tmp = folder / "preview.tmp.mp4"
        mov = folder / "scene.mov"
        self._ffmpeg(["-ss", f"{board['at']:.3f}", "-t", f"{length:.3f}", "-i", str(video), "-i", str(mov),
                      "-filter_complex", graph, "-map", "[v]", "-map", "0:a?", "-c:v", "libx264", "-preset", "veryfast",
                      "-crf", "24", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", "-shortest", str(tmp)],
                     cancel, "превью")  # fmt: skip
        tmp.replace(folder / "preview.mp4")
        self._ffmpeg(["-ss", f"{min(length * 0.6, length - 0.2):.2f}", "-i", str(folder / "preview.mp4"), "-frames:v",
                      "1", "-q:v", "4", str(folder / "poster.jpg")], cancel, "обложка")  # fmt: skip

    def _package(self, project: dict) -> None:
        pid = project["id"]
        folder = self.dir(pid)
        with self._lock:
            items = copy.deepcopy(project["scenes"])
            fps = (project.get("source") or {}).get("fps") or 30
            title = project["title"]
            meta = {"title": title, "topic": project.get("topic", ""), "source": project.get("source"),
                    "design": project.get("design")}  # fmt: skip
        rows = scenes.rows(items, fps)
        target = folder / "package.zip"
        if not rows:
            with self._package_lock:
                target.unlink(missing_ok=True)
            return
        movs = {s["n"]: folder / "scenes" / s["id"] / "scene.mov" for s in items}
        with self._package_lock:
            scenes.package(target, rows, movs, title, fps, meta)


def asr_hint() -> str:
    return "Поставь модель расшифровки: make models M=whisper-large-v3-turbo в app/platform."


class _scratch:
    """Временный каталог внутри данных модуля (sandbox и права — те же, что у проекта)."""

    def __init__(self, root: Path) -> None:
        self.path = root / "tmp" / uuid.uuid4().hex[:8]

    def __enter__(self) -> Path:
        self.path.mkdir(parents=True, exist_ok=True)
        return self.path

    def __exit__(self, *exc) -> None:
        shutil.rmtree(self.path, ignore_errors=True)
