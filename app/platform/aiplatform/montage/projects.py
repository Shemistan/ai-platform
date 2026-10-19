"""Проекты монтажа: хранение на диске, очередь из одного воркера, конвейер.

prepare: звук → сдвиг экрана → тишина → расшифровка (GPU-замок) → план LLM → ждёт человека
render:  сегменты (кэш) → склейка → субтитры → финал → проверка (тех. + повторная ASR + LLM)
         → до MAX_ROUNDS кругов исправлений → моушн-графика → готово
revise:  замечание человека → LLM переделывает план и формулирует урок → ждёт человека
"""

from __future__ import annotations

import copy
import json
import queue
import re
import shutil
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from aiplatform.core import log, media, runner, schema
from aiplatform.core import uploads as uploads_module
from aiplatform.core.fsutil import atomic_write
from aiplatform.core.gate import Gate
from aiplatform.montage import asr, edl, motion, plan, render, sync, transcript
from aiplatform.montage.config import MontageConfig
from aiplatform.studio import llm

SCHEMA = 1
FIELDS = schema.require("id", "status", "created_at", "progress", types={"id": str, "status": str})
ACTIVE = ("queued", "running")
LOG_TAIL = 200
GPU_OWNER = "Монтаж"
ROLES = {"head": "Голова", "screen": "Экран"}
MAX_ROUNDS = 2  # кругов «проверка → исправление → пересборка»
MAX_TITLE = 120
MAX_TERMS = 2000
MAX_FEEDBACK = 4000
INSTALL_HINT = (
    "Поставь модель расшифровки: make models M=whisper-large-v3-turbo в app/platform (README, «Установка моделей»)."
)
_ID = re.compile(r"^[0-9a-f]{12}$")
DEFAULT_OPTIONS = {
    "model": asr.DEFAULT_ID,
    "terms": "",
    "auto": False,  # собирать без подтверждения плана
    "subtitles": False,  # субтитры в кадре (SRT рядом — всегда)
    "grade": "none",
    "check": True,  # повторная расшифровка результата и поиск пропущенного
    "motion": False,  # моушн-вставки (HyperFrames)
    "max_pause": edl.MAX_PAUSE_S,
}


class ValidationError(ValueError):
    pass


class Conflict(ValueError):
    """Действие не к месту: проект занят или ещё не дошёл до нужного шага."""


def clean_options(payload: object, base: dict | None = None) -> dict:
    data = payload if isinstance(payload, dict) else {}
    out = dict(base or DEFAULT_OPTIONS)
    if "model" in data:
        if asr.get(data["model"]) is None:
            raise ValidationError("Неизвестная модель расшифровки.")
        out["model"] = data["model"]
    if "terms" in data:
        if not isinstance(data["terms"], str) or len(data["terms"]) > MAX_TERMS:
            raise ValidationError(f"Термины — строка до {MAX_TERMS} знаков.")
        out["terms"] = data["terms"].strip()
    for key in ("auto", "subtitles", "check", "motion"):
        if key in data:
            if not isinstance(data[key], bool):
                raise ValidationError(f"{key} — true или false.")
            out[key] = data[key]
    if "grade" in data:
        if data["grade"] not in render.GRADES:
            raise ValidationError(f"Цветокоррекция — одна из: {', '.join(render.GRADES)}.")
        out["grade"] = data["grade"]
    if "max_pause" in data:
        value = data["max_pause"]
        if not isinstance(value, int | float) or isinstance(value, bool) or not 0.3 <= value <= 3:
            raise ValidationError("Самая длинная пауза — от 0,3 до 3 секунд.")
        out["max_pause"] = float(value)
    return out


def split_terms(text: str) -> list[str]:
    return [t.strip() for t in re.split(r"[,\n;]", text or "") if t.strip()]


class MontageProjects:
    def __init__(self, config: MontageConfig, gate: Gate, url_base: str = "/api/montage") -> None:
        self.config = config
        self._gate = gate
        self._url = url_base
        self._lock = threading.Lock()
        self._projects: dict[str, dict] = {}
        self._cancels: dict[str, threading.Event] = {}
        self._queue: queue.Queue[tuple[str, str, str] | None] = queue.Queue()
        config.projects_dir.mkdir(parents=True, exist_ok=True)
        self.load_problems: list[str] = []
        self._load()
        self._worker = threading.Thread(target=self._work, name="montage-worker", daemon=True)
        self._worker.start()

    # ---------- чтение ----------

    def dir(self, pid: str) -> Path:
        return self.config.projects_dir / pid

    def get(self, pid: str) -> dict | None:
        with self._lock:
            project = self._projects.get(pid)
            return self._view(project) if project else None

    def list(self) -> list[dict]:
        with self._lock:
            items = sorted(self._projects.values(), key=lambda p: p["created_at"], reverse=True)
            return [{k: v for k, v in self._view(p).items() if k != "log"} for p in items]

    def _view(self, project: dict) -> dict:
        view = copy.deepcopy(project)
        pid = project["id"]
        done = project.get("output") is not None and (self.dir(pid) / "output.mp4").is_file()
        view["video_url"] = f"{self._url}/projects/{pid}/video" if done else None
        view["srt_url"] = f"{self._url}/projects/{pid}/subtitles.srt" if done else None
        view["sources_url"] = {
            role: f"{self._url}/projects/{pid}/source/{role}" for role in ROLES if project["sources"].get(role)
        }
        if project["status"] == "queued":
            view["queue_position"] = sum(
                1 for p in self._projects.values() if p["status"] in ACTIVE and p["updated_at"] < project["updated_at"]
            )
        return view

    def _read_json(self, pid: str, name: str, default=None):
        path = self.dir(pid) / name
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return default

    def plan_view(self, pid: str) -> dict | None:
        """Всё для экрана подтверждения: слова, вырезки, планы, заметки, сводка и сегменты."""
        project = self.get(pid)
        if project is None:
            return None
        words = (self._read_json(pid, "words.json") or {}).get("words") or []
        data = self._read_json(pid, "plan.json") or {}
        cuts, layout = data.get("cuts") or [], data.get("layout") or [{"from": 0, "layout": "head"}]
        duration = project["sources"]["head"]["duration"] if project["sources"].get("head") else 0.0
        silences = [tuple(s) for s in self._read_json(pid, "silences.json", [])]
        result = edl.build(words, duration, cuts, layout, silences, max_pause=project["options"]["max_pause"])
        return {
            "words": words,
            "cuts": cuts,
            "layout": layout,
            "notes": data.get("notes", ""),
            "summary": edl.summary(words, duration, cuts, result),
            "segments": result.as_list(),
            "has_screen": bool(project["sources"].get("screen")),
            "kinds": edl.KINDS,
            "layouts": edl.LAYOUTS,
        }

    def file(self, pid: str, name: str) -> Path | None:
        if pid not in self._projects:
            return None
        path = self.dir(pid) / name
        return path if path.is_file() else None

    def source(self, pid: str, role: str) -> Path | None:
        with self._lock:
            project = self._projects.get(pid)
            meta = project["sources"].get(role) if project else None
        return self.file(pid, f"sources/{role}.{meta['ext']}") if meta else None

    # ---------- запись ----------

    def create(self, payload: object) -> dict:
        data = payload if isinstance(payload, dict) else {}
        title = data.get("title", "")
        if not isinstance(title, str) or len(title.strip()) > MAX_TITLE:
            raise ValidationError(f"Название — строка до {MAX_TITLE} знаков.")
        options = clean_options(data.get("options"))
        pid = uuid.uuid4().hex[:12]
        now = time.time()
        project = {
            "id": pid,
            "title": " ".join(title.split()) or time.strftime("Монтаж %d.%m %H:%M"),
            "status": "draft",
            "action": None,
            "created_at": now,
            "updated_at": now,
            "backend": self.config.backend,
            "options": options,
            "sources": {"head": None, "screen": None},
            "sync": None,
            "screen_offset": 0.0,
            "transcript": None,
            "plan": None,
            "output": None,
            "check": None,
            "motion": None,
            "progress": {"stage": "Ждёт записи", "percent": 0.0},
            "error": None,
            "history": [],
            "log": [],
        }
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
            if "screen_offset" in data:
                value = data["screen_offset"]
                if not isinstance(value, int | float) or isinstance(value, bool) or abs(value) > 3600:
                    raise ValidationError("Сдвиг экрана — число секунд, по модулю до часа.")
                project["screen_offset"] = round(float(value), 3)
            self._touch(project)
            return self._view(project)

    def save_source(self, pid: str, role: str, tmp: Path) -> dict | None:
        """Запись «голова» или «экран» (уже на диске, body_to_file) → в каталог проекта."""
        try:
            if role not in ROLES:
                raise ValidationError("Источник — head (голова) или screen (экран).")
            with self._lock:
                project = self._projects.get(pid)
                if project is None:
                    return None
                self._idle(project)
            ext = uploads_module.sniff_video(uploads_module.head(tmp)) if tmp.stat().st_size else None
            if ext is None:
                raise ValidationError("Поддерживаются видео MP4, MOV, WebM.")
            meta = self._probe(tmp)
            if role == "head" and not meta["has_audio"]:
                raise ValidationError("В записи «голова» нет звука — без него нечего расшифровывать.")
            folder = self.dir(pid) / "sources"
            folder.mkdir(parents=True, exist_ok=True)
            for old in folder.glob(f"{role}.*"):
                old.unlink()
            target = folder / f"{role}.{ext}"
            tmp.replace(target)
        finally:
            tmp.unlink(missing_ok=True)
        with self._lock:
            project = self._projects.get(pid)
            if project is None:
                return None
            project["sources"][role] = {**meta, "ext": ext}
            if role == "head":
                project["progress"] = {"stage": "Запись загружена", "percent": 0.0}
            self._history(project, "source", f"{ROLES[role]}: {meta['duration']:.0f} с")
            self._touch(project)
            return self._view(project)

    def remove_source(self, pid: str, role: str) -> dict | None:
        with self._lock:
            project = self._projects.get(pid)
            if project is None:
                return None
            self._idle(project)
            meta = project["sources"].get(role)
            if meta:
                (self.dir(pid) / "sources" / f"{role}.{meta['ext']}").unlink(missing_ok=True)
            project["sources"][role] = None
            self._touch(project)
            return self._view(project)

    def start(self, pid: str) -> dict | None:
        return self._enqueue(pid, "prepare", need_head=True)

    def replan(self, pid: str) -> dict | None:
        return self._enqueue(pid, "replan", need_words=True)

    def render(self, pid: str) -> dict | None:
        return self._enqueue(pid, "render", need_words=True)

    def revise(self, pid: str, text: object) -> dict | None:
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_FEEDBACK:
            raise ValidationError(f"Замечание — непустой текст до {MAX_FEEDBACK} знаков.")
        return self._enqueue(pid, "revise", text.strip(), need_words=True)

    def save_plan(self, pid: str, payload: object) -> dict | None:
        """Правка плана человеком: вырезки и планы целиком."""
        data = payload if isinstance(payload, dict) else {}
        with self._lock:
            project = self._projects.get(pid)
            if project is None:
                return None
            self._idle(project)
        words = (self._read_json(pid, "words.json") or {}).get("words")
        if not words:
            raise Conflict("Плана ещё нет: сначала расшифровка.")
        has_screen = bool(project["sources"].get("screen"))
        current = self._read_json(pid, "plan.json") or {}
        cuts = edl.clean_cuts(data.get("cuts"), len(words), default_kind="human")
        layout = edl.clean_layout(data.get("layout"), len(words), has_screen)
        new = {**current, "cuts": cuts, "layout": layout, "edited_at": time.time()}
        atomic_write(self.dir(pid) / "plan.json", json.dumps(new, ensure_ascii=False))
        with self._lock:
            self._set_plan_summary(project, words, cuts, layout)
            self._history(project, "edit", f"план поправлен вручную: вырезок {len(cuts)}")
            self._touch(project)
            return self._view(project)

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

    # ---------- уроки ----------

    def lessons(self) -> str:
        return plan.lessons(self.config.lessons_file)

    def save_lessons(self, text: object) -> str:
        if not isinstance(text, str) or len(text) > 100_000:
            raise ValidationError("Правила — текст до 100 000 знаков.")
        self.config.lessons_file.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(self.config.lessons_file, text)
        return text

    # ---------- внутреннее: состояние ----------

    def _idle(self, project: dict) -> None:
        if project["status"] in ACTIVE:
            raise Conflict("Проект в работе — дождись или отмени.")

    def _enqueue(self, pid: str, action: str, arg: str = "", *, need_head=False, need_words=False) -> dict | None:
        with self._lock:
            project = self._projects.get(pid)
            if project is None:
                return None
            self._idle(project)
            if need_head and not project["sources"].get("head"):
                raise Conflict("Загрузи запись «голова».")
            if need_words and not (self.dir(pid) / "words.json").is_file():
                raise Conflict("Сначала расшифровка: нажми «Начать».")
            model = asr.get(project["options"]["model"])
            if action == "prepare" and model is not None and not asr.installed(model, self.config):
                raise Conflict(f"Модель {model.label} не установлена: make models M={model.id}.")
            needs_llm = action != "render" or project["options"]["check"] or project["options"]["motion"]
            if needs_llm and not self.config.llm_available():
                raise Conflict(llm.missing_cli(self.config.llm_cmd[0]))
            event = threading.Event()
            self._cancels[pid] = event
            project["status"] = "queued"
            project["action"] = action
            project["error"] = None
            project["progress"] = {"stage": "В очереди", "percent": 0.0}
            self._touch(project)
            self._queue.put((pid, action, arg))
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
            label = f"Монтаж {path.parent.name}"
            project, _ = schema.load_record(path, SCHEMA, check=FIELDS, problems=self.load_problems, label=label)
            if project is None:
                continue
            if project["status"] in ACTIVE:
                project["status"] = "failed"
                project["error"] = "Сервис был перезапущен во время работы — запусти шаг ещё раз."
                project["progress"]["stage"] = "Ошибка"
                self._save(project)
            self._projects[project["id"]] = project
            self._cancels[project["id"]] = threading.Event()

    def _history(self, project: dict, action: str, text: str) -> None:
        project.setdefault("history", []).append({"at": time.time(), "action": action, "text": text})
        del project["history"][:-50]

    def _finish(self, project: dict, status: str, *, error: str | None = None, stage: str | None = None) -> None:
        project["status"] = status
        project["error"] = error
        if status == "done":
            project["progress"] = {"stage": "Готово", "percent": 100.0}
        elif status == "review":
            project["progress"] = {"stage": "Ждёт подтверждения плана", "percent": 100.0}
        else:
            project["progress"]["stage"] = stage or ("Ошибка" if status == "failed" else "Отменено")
        self._touch(project)

    def _progress(self, project: dict, cancel: threading.Event, stage: str, percent: float) -> None:
        with self._lock:
            if not cancel.is_set():
                project["progress"] = {"stage": stage, "percent": round(max(0.0, min(percent, 100.0)), 1)}

    def _note(self, project: dict, line: str) -> None:
        with self._lock:
            project["log"].append(line.rstrip()[:500])
            del project["log"][:-LOG_TAIL]

    def _set_plan_summary(self, project: dict, words: list[dict], cuts: list[dict], layout: list[dict]) -> None:
        duration = project["sources"]["head"]["duration"]
        silences = [tuple(s) for s in self._read_json(project["id"], "silences.json", [])]
        result = edl.build(words, duration, cuts, layout, silences, max_pause=project["options"]["max_pause"])
        project["plan"] = {**(project.get("plan") or {}), **edl.summary(words, duration, cuts, result)}

    # ---------- внутреннее: воркер ----------

    def _work(self) -> None:
        while (item := self._queue.get()) is not None:
            pid, action, arg = item
            with self._lock:
                project = self._projects.get(pid)
                cancel = self._cancels.get(pid)
                if project is None or cancel is None or project["status"] != "queued":
                    continue
                project["status"] = "running"
                project["progress"] = {"stage": "Запуск", "percent": 0.0}
                self._touch(project)
            self._run(project, action, arg, cancel)

    def _run(self, project: dict, action: str, arg: str, cancel: threading.Event) -> None:
        try:
            if action == "prepare":
                self._prepare(project, cancel)
            elif action == "replan":
                self._plan(project, cancel, 0.0)
            elif action == "revise":
                self._revise(project, arg, cancel)
            if action == "render" or (action == "prepare" and project["options"]["auto"]):
                self._render(project, cancel)
                status = "done"
            else:
                status = "review"
        except runner.Canceled:
            with self._lock:
                self._finish(project, "canceled")
        except (runner.RunFailed, llm.StudioError, ValueError) as exc:
            with self._lock:
                self._finish(project, "failed", error=str(exc))
        except Exception as exc:  # noqa: BLE001 — воркер не должен умереть из-за одного проекта
            with self._lock:
                log.internal_error(exc, task=project["id"])
                project["log"].append(f"✗ {type(exc).__name__}: {exc}")
                self._finish(project, "failed", error=log.TASK_INTERNAL)
        else:
            with self._lock:
                self._finish(project, status)

    # ---------- шаги ----------

    def _ffmpeg(self, args: list[str], cancel: threading.Event, what: str) -> list[str]:
        lines: list[str] = []
        argv = [self.config.ffmpeg, "-y", "-loglevel", "error", *args]
        try:
            runner.run(argv, lines.append, cancel, kind="render")
        except runner.RunFailed as exc:
            raise runner.RunFailed(f"ffmpeg ({what}): {(lines or [str(exc)])[-1]}") from exc
        return lines

    def _probe(self, path: Path) -> dict:
        ffprobe = media.ffprobe_for(self.config.ffmpeg)
        import subprocess

        argv = [ffprobe, "-v", "error", "-show_entries", "format=duration:stream=codec_type,width,height",
                "-of", "json", str(path)]  # fmt: skip
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
        return {
            "duration": round(duration, 3),
            "width": int(video.get("width") or 0),
            "height": int(video.get("height") or 0),
            "has_audio": any(s.get("codec_type") == "audio" for s in streams),
            "size": path.stat().st_size,
        }

    def _audio(self, source: Path, wav: Path, cancel: threading.Event) -> None:
        self._ffmpeg(["-i", str(source), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav)], cancel,
                     "звук для расшифровки")  # fmt: skip

    def _silences(self, wav: Path, duration: float, cancel: threading.Event) -> list[tuple[float, float]]:
        lines: list[str] = []
        argv = transcript.silence_argv(self.config.ffmpeg, wav)
        runner.run(argv, lines.append, cancel, kind="render")
        return transcript.parse_silences(lines, duration)

    def _transcribe(
        self, project: dict, wav: Path, out: Path, silences, cancel: threading.Event, lo: float, hi: float, what: str
    ) -> dict:
        model = asr.get(project["options"]["model"]) or asr.CATALOG[0]
        duration = media.duration(wav, self.config.ffmpeg)
        hotwords = [*plan.terms(self.lessons()), *split_terms(project["options"]["terms"])]
        argv = asr.build_argv(
            self.config, model, wav, out, splits=transcript.split_points(duration, silences), hotwords=hotwords
        )

        def on_line(line: str) -> None:
            step = asr.parse_progress(line)
            if step:
                self._progress(project, cancel, f"{what}: {step[1] or model.label}", lo + (hi - lo) * step[0] / 100)
            elif line.strip():
                self._note(project, line)

        def on_wait(holder: str) -> None:
            self._progress(project, cancel, f"Ждёт GPU (занят: {holder})", lo)

        with self._gate.hold(GPU_OWNER, on_wait, cancel):
            self._progress(project, cancel, f"{what}: {model.label}", lo)
            runner.run(argv, on_line, cancel, missing_hint=INSTALL_HINT, kind="asr")
        try:
            data = json.loads(out.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise runner.RunFailed("Расшифровка завершилась, но слов нет: файл результата не прочитался.") from exc
        words = [w for w in data.get("words") or [] if isinstance(w, dict) and str(w.get("w", "")).strip()]
        if not words:
            raise runner.RunFailed("Расшифровка не нашла речи в записи.")
        data["words"] = words
        data["model"] = model.id
        return data

    def _prepare(self, project: dict, cancel: threading.Event) -> None:
        pid = project["id"]
        folder = self.dir(pid)
        head = self.source(pid, "head")
        if head is None:
            raise runner.RunFailed("Запись «голова» пропала из каталога проекта — загрузи её заново.")
        self._progress(project, cancel, "Звук", 1)
        wav = folder / "audio.wav"
        self._audio(head, wav, cancel)
        duration = project["sources"]["head"]["duration"]
        screen = self.source(pid, "screen")
        if screen is not None and project["sources"]["screen"].get("has_audio"):
            self._progress(project, cancel, "Сдвиг экрана по звуку", 5)
            found = sync.detect(self.config.ffmpeg, head, screen)
            with self._lock:
                project["sync"] = found
                if found["method"] == "audio":
                    project["screen_offset"] = found["offset"]
            self._note(project, f"Сдвиг экрана: {found['offset']:+.2f} с ({found['method']}, {found['score']:.2f})")
        self._progress(project, cancel, "Паузы", 9)
        silences = self._silences(wav, duration, cancel)
        atomic_write(folder / "silences.json", json.dumps(silences))
        data = self._transcribe(project, wav, folder / "words.json", silences, cancel, 10, 85, "Расшифровка")
        atomic_write(folder / "words.json", json.dumps(data, ensure_ascii=False))
        with self._lock:
            project["transcript"] = {
                "model": data["model"],
                "words": len(data["words"]),
                "elapsed_s": data.get("elapsed_s"),
            }
            self._history(project, "transcribe", f"слов: {len(data['words'])}")
            self._touch(project)
        self._plan(project, cancel, 85)

    def _llm(self, prompt: str, project: dict, cancel: threading.Event) -> object:
        answer = llm.complete(prompt, cmd=self.config.llm_cmd, cancel=cancel)
        return llm.extract_json(answer)

    def _plan(self, project: dict, cancel: threading.Event, lo: float) -> None:
        pid = project["id"]
        words = (self._read_json(pid, "words.json") or {}).get("words") or []
        has_screen = bool(project["sources"].get("screen"))
        self._progress(project, cancel, "План монтажа (ИИ)", lo)
        prompt = plan.plan_prompt(plan.pack(words), plan.base_rules(), self.lessons(), has_screen, project["title"])
        result = plan.parse_plan(self._llm(prompt, project, cancel), len(words), has_screen)
        self._store_plan(project, words, result, "plan")

    def _revise(self, project: dict, feedback: str, cancel: threading.Event) -> None:
        pid = project["id"]
        words = (self._read_json(pid, "words.json") or {}).get("words") or []
        has_screen = bool(project["sources"].get("screen"))
        current = self._read_json(pid, "plan.json") or {}
        self._progress(project, cancel, "Переделываю план по замечанию (ИИ)", 10)
        prompt = plan.revise_prompt(
            plan.pack(words), plan.base_rules(), self.lessons(), has_screen,
            current.get("cuts") or [], current.get("layout") or [{"from": 0, "layout": "head"}], feedback,
        )  # fmt: skip
        result = plan.parse_plan(self._llm(prompt, project, cancel), len(words), has_screen)
        if result["lesson"]:
            text = plan.add_lesson(self.lessons(), result["lesson"])
            self.save_lessons(text)
        with self._lock:
            self._history(project, "feedback", feedback)
            if result["lesson"]:
                self._history(project, "lesson", result["lesson"])
        self._store_plan(project, words, result, "revise")

    def _store_plan(self, project: dict, words: list[dict], result: dict, source: str) -> None:
        data = {"cuts": result["cuts"], "layout": result["layout"], "notes": result["notes"], "source": source,
                "made_at": time.time()}  # fmt: skip
        atomic_write(self.dir(project["id"]) / "plan.json", json.dumps(data, ensure_ascii=False))
        with self._lock:
            project["plan"] = {"notes": result["notes"]}
            self._set_plan_summary(project, words, result["cuts"], result["layout"])
            self._history(project, "plan", f"вырезок {len(result['cuts'])}")
            self._touch(project)

    def _render(self, project: dict, cancel: threading.Event) -> None:
        pid = project["id"]
        folder = self.dir(pid)
        words = (self._read_json(pid, "words.json") or {}).get("words") or []
        current = self._read_json(pid, "plan.json") or {}
        cuts = list(current.get("cuts") or [])
        layout = current.get("layout") or [{"from": 0, "layout": "head"}]
        rounds: list[dict] = []
        for n in range(MAX_ROUNDS + 1):
            result, out_words = self._assemble(project, words, cuts, layout, cancel)
            if not project["options"]["check"] or n == MAX_ROUNDS:
                found = self._tech_check(project, cancel) if not rounds else []
                rounds.append({"round": n + 1, "issues": [], "tech": found})
                break
            self._progress(project, cancel, f"Проверка, круг {n + 1}", 80)
            tech = self._tech_check(project, cancel)
            issues = self._asr_check(project, out_words, cancel)
            rounds.append({"round": n + 1, "issues": issues, "tech": tech})
            if not issues:
                break
            cuts = edl.clean_cuts([*cuts, *issues], len(words), default_kind="check")
            current["cuts"] = cuts
            atomic_write(folder / "plan.json", json.dumps(current, ensure_ascii=False))
            self._note(project, f"Проверка нашла {len(issues)}, пересобираю")
        info = None
        if project["options"]["motion"]:
            overlays, info = motion.run(
                self.config, project, folder, out_words, result.duration, cancel,
                ask=lambda text: self._llm(text, project, cancel),
                note=lambda line: self._note(project, line),
                progress=lambda stage, percent: self._progress(project, cancel, stage, percent),
            )  # fmt: skip
            if overlays:
                self._final(project, out_words, overlays, cancel)
        with self._lock:
            project["output"] = {
                "duration": round(media.duration(folder / "output.mp4", self.config.ffmpeg), 1),
                "segments": len(result.segments),
                "size": (folder / "output.mp4").stat().st_size,
            }
            project["check"] = {"rounds": rounds}
            project["motion"] = info
            self._set_plan_summary(project, words, cuts, layout)
            fixed = sum(len(r["issues"]) for r in rounds)
            self._history(project, "render", f"{result.duration:.0f} с, исправлено проверкой: {fixed}")
            self._touch(project)

    def _assemble(self, project, words, cuts, layout, cancel) -> tuple[edl.Edl, list[dict]]:
        """Сегменты (из кэша, если были) → склейка → субтитры → финал без вставок."""
        pid = project["id"]
        folder = self.dir(pid)
        duration = project["sources"]["head"]["duration"]
        silences = [tuple(s) for s in self._read_json(pid, "silences.json", [])]
        result = edl.build(words, duration, cuts, layout, silences, max_pause=project["options"]["max_pause"])
        if not result.segments:
            raise runner.RunFailed("После вырезок ничего не осталось — поправь план.")
        atomic_write(folder / "segments.json", json.dumps(result.as_list()))
        head, screen = self.source(pid, "head"), self.source(pid, "screen")
        if head is None:
            raise runner.RunFailed("Запись «голова» пропала из каталога проекта.")
        cache = folder / "segments"
        cache.mkdir(exist_ok=True)
        mask = folder / "mask.png"
        if screen is not None and any(s.layout == "split" for s in result.segments) and not mask.is_file():
            runner.run(render.mask_argv(self.config.ffmpeg, mask), self._note_line(project), cancel, kind="render")
        parts: list[Path] = []
        total = len(result.segments)
        for n, seg in enumerate(result.segments, 1):
            start, end = render.frame(seg.start), render.frame(seg.end)  # целые кадры: склейка без набегания
            spec = render.SegmentSpec(start, end, seg.layout, project["options"]["grade"], head, screen,
                                      project["screen_offset"])  # fmt: skip
            part = cache / f"{spec.key()}.mp4"
            if not part.is_file():
                tmp = part.with_suffix(".tmp.mp4")
                runner.run(render.segment_argv(self.config.ffmpeg, spec, tmp, mask), self._note_line(project),
                           cancel, kind="render")  # fmt: skip
                tmp.replace(part)
            parts.append(part)
            self._progress(project, cancel, f"Сборка: кусок {n} из {total}", 70 * n / total)
        used = {p.name for p in parts}
        for old in cache.glob("*.mp4"):  # кэш — только для текущего плана
            if old.name not in used:
                old.unlink(missing_ok=True)
        listing = folder / "segments.txt"
        listing.write_text(render.concat_list(parts), encoding="utf-8")
        self._progress(project, cancel, "Склейка", 72)
        self._ffmpeg(["-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", str(folder / "joined.mp4")],
                     cancel, "склейка")  # fmt: skip
        out_words = edl.output_words(words, result)
        items = render.chunks(out_words)
        atomic_write(folder / "subtitles.srt", render.srt(items))
        self._final(project, out_words, [], cancel)
        return result, out_words

    def _final(self, project, out_words, overlays, cancel) -> None:
        folder = self.dir(project["id"])
        ass = None
        if project["options"]["subtitles"]:
            if media.has_filter(self.config.ffmpeg, "subtitles"):
                ass = folder / "subtitles.ass"
                atomic_write(ass, render.ass(render.chunks(out_words), self.config.font))
            else:
                self._note(project, "Субтитры в кадр пропущены: в ffmpeg нет фильтра subtitles (нужен ffmpeg-full).")
        self._progress(project, cancel, "Финальный проход: громкость" + (", вставки" if overlays else ""), 75)
        tmp = folder / "output.tmp.mp4"
        argv = render.final_argv(self.config.ffmpeg, folder / "joined.mp4", tmp, subtitles=ass, overlays=overlays)
        runner.run(argv, self._note_line(project), cancel, kind="render")
        tmp.replace(folder / "output.mp4")

    def _note_line(self, project: dict) -> Callable[[str], None]:
        return lambda line: self._note(project, line) if line.strip() else None

    def _tech_check(self, project: dict, cancel: threading.Event) -> list[str]:
        """Длинные паузы в результате: монтаж их пропустил (или это тишина без слов)."""
        folder = self.dir(project["id"])
        out = folder / "output.mp4"
        limit = project["options"]["max_pause"] + 0.3
        lines: list[str] = []
        runner.run(transcript.silence_argv(self.config.ffmpeg, out, min_s=limit), lines.append, cancel, kind="render")
        found = transcript.parse_silences(lines, media.duration(out, self.config.ffmpeg))
        return [f"пауза {b - a:.1f} с на {a:.1f} с" for a, b in found if b - a >= limit and a > 0.5]

    def _asr_check(self, project: dict, out_words: list[dict], cancel: threading.Event) -> list[dict]:
        """Повторная расшифровка результата → LLM ищет пропущенное → вырезки в индексах исходника."""
        folder = self.dir(project["id"])
        wav = folder / "check.wav"
        self._audio(folder / "output.mp4", wav, cancel)
        duration = media.duration(wav, self.config.ffmpeg)
        silences = self._silences(wav, duration, cancel)
        data = self._transcribe(project, wav, folder / "check.json", silences, cancel, 82, 92, "Проверка")
        wav.unlink(missing_ok=True)
        heard = data["words"]
        self._progress(project, cancel, "Проверка: ищу пропущенное (ИИ)", 93)
        prompt = plan.check_prompt(plan.pack(heard), plan.base_rules(), self.lessons())
        issues = plan.parse_issues(self._llm(prompt, project, cancel), len(heard))
        # Индексы услышанного → время результата → слова результата → индексы исходника.
        mapped: list[dict] = []
        for issue in issues:
            a, b = heard[issue["from"]]["s"], heard[issue["to"]]["e"]
            src = [w["src"] for w in out_words if w["e"] > a and w["s"] < b]
            if src:
                mapped.append({**issue, "from": min(src), "to": max(src), "kind": "check"})
        return mapped
