"""Проекты монтажа: хранение на диске, очередь из одного воркера, конвейер.

prepare: звук → сдвиг экрана → тишина → расшифровка (GPU-замок) → манифест источников → план LLM
         (кроме режима manual) → ворота edl (ждёт человека; autonomous — сам)
render:  сегменты (кэш) → склейка → субтитры → финал → проверка (тех. + повторная ASR + LLM)
         → до MAX_ROUNDS кругов исправлений → моушн-графика → готово
revise:  замечание человека → LLM переделывает план и формулирует урок → ждёт человека

Режимы (MASTER_SPEC §1): reviewed — ворота ждут человека; autonomous — проходятся сами с записью
в журнале решений; manual — план вырезок строит только человек. Каждое решение LLM и каждые ворота
пишутся в decisions.jsonl (montage/decisions.py).
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
from aiplatform.montage import (
    asr,
    decisions,
    documents,
    edl,
    exports,
    knowledge,
    motion,
    narrative,
    plan,
    qa,
    render,
    revisions,
    scenes,
    stages,
    storyboard,
    sync,
    templates,
    timeline,
    timemap,
    transcript,
)
from aiplatform.montage import schemas as montage_schemas
from aiplatform.montage import style as style_module
from aiplatform.montage.config import MontageConfig
from aiplatform.studio import llm

SCHEMA = 2  # 2: mode и approvals вместо галочки auto (PRD-027)
FIELDS = schema.require("id", "status", "created_at", "progress", types={"id": str, "status": str})
ACTIVE = ("queued", "running")
MODES = {"reviewed": "С подтверждением", "autonomous": "Автономно", "manual": "Вручную"}
GATES = ("edl", "storyboard", "export")
INTERRUPTED = "Сервис был перезапущен во время работы — запусти шаг ещё раз (кэш сегментов и сцен цел)."
LOG_TAIL = 200
# Строка прогресса ffmpeg (-progress pipe:1): сколько готово, мкс; остальные «ключ=значение» — шум.
_FF_TIME = re.compile(r"^out_time_(?:us|ms)=(\d+)$")
_FF_KEY = re.compile(r"^[a-z0-9_]+=\s*\S*$")  # «bitrate= 256.0kbits/s» — с пробелом
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
_SCENE_ID = re.compile(r"^scene_\d{3}$")


def storyboard_id_ok(sid: str) -> bool:
    return bool(_SCENE_ID.match(sid or ""))


DEFAULT_OPTIONS = {
    "model": asr.DEFAULT_ID,
    "terms": "",
    "mode": "reviewed",  # reviewed | autonomous | manual (MASTER_SPEC §1)
    "subtitles": False,  # субтитры в кадре (SRT рядом — всегда)
    "grade": "none",
    "check": True,  # повторная расшифровка результата и поиск пропущенного
    "motion": False,  # моушн-вставки (HyperFrames)
    "max_pause": edl.MAX_PAUSE_S,
}


def migrate_v1(data: dict) -> dict:
    """Схема 1 → 2: галочка auto → mode, пустые ворота."""
    options = data.get("options") or {}
    if "mode" not in options:
        options["mode"] = "autonomous" if options.pop("auto", False) else "reviewed"
    else:
        options.pop("auto", None)
    data["options"] = options
    data.setdefault("approvals", {gate: None for gate in GATES})
    return data


MIGRATIONS = {1: migrate_v1, 0: migrate_v1}


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
    if "auto" in data:  # старые клиенты: галочка «собрать без подтверждения»
        if not isinstance(data["auto"], bool):
            raise ValidationError("auto — true или false.")
        out["mode"] = "autonomous" if data["auto"] else "reviewed"
    if "mode" in data:
        if data["mode"] not in MODES:
            raise ValidationError(f"Режим — один из: {', '.join(MODES)}.")
        out["mode"] = data["mode"]
    for key in ("subtitles", "check", "motion"):
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
        view["mode"] = project["options"]["mode"]
        view["options"]["auto"] = project["options"]["mode"] == "autonomous"  # старые клиенты
        view["gates"] = self._gates(project)
        view["sources_url"] = {
            role: f"{self._url}/projects/{pid}/source/{role}" for role in ROLES if project["sources"].get(role)
        }
        now = time.time()
        view["eta_s"] = stages.remaining(view["stages"], now) if project["status"] == "running" else None
        for stage in view["stages"]:
            stage["eta_s"] = stages.stage_remaining(stage, now)
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

    def decisions(self, pid: str, limit: int = 200) -> list[dict] | None:
        if pid not in self._projects:
            return None
        return decisions.read(self.dir(pid), limit)

    def timemap_view(self, pid: str) -> dict | None:
        if pid not in self._projects:
            return None
        return self._read_json(pid, "timemap.json") or {"segments": [], "duration_frames": 0, "fps": render.FPS}

    # ---------- ворота (MASTER_SPEC §1, §6) ----------

    def _gates(self, project: dict) -> dict:
        """Состояние ворот для UI: нужно ли подтверждение и дано ли оно."""
        mode = project["options"]["mode"]
        approvals = project.get("approvals") or {}
        out = {}
        for gate in GATES:
            given = approvals.get(gate)
            out[gate] = {
                "required": mode != "autonomous",
                "approved": bool(given),
                "auto": bool(given and given.get("auto")),
                "at": given.get("at") if given else None,
            }
        return out

    def _pass_gate(self, project: dict, gate: str, *, auto: bool, reason: str = "") -> None:
        """Записать прохождение ворот (человек или autonomous) в проект и журнал решений."""
        project.setdefault("approvals", {g: None for g in GATES})[gate] = {
            "at": time.time(), "by": "autonomous" if auto else "human", "auto": auto, "reason": reason,
        }  # fmt: skip
        decisions.record(self.dir(project["id"]), "gate", f"approve:{gate}", auto=auto,
                         rationale=reason or ("autonomous mode" if auto else "approved by human"))  # fmt: skip
        self._history(project, "approve", f"{gate}: {'автономно' if auto else 'подтверждено'}")

    def _reset_gate(self, project: dict, gate: str) -> None:
        if (project.get("approvals") or {}).get(gate):
            project["approvals"][gate] = None

    def approve(self, pid: str, gate: object) -> dict | None:
        """Человек подтверждает ворота; edl — после плана, storyboard — после раскадровки, export — после QA."""
        if gate not in GATES:
            raise ValidationError(f"Ворота — одни из: {', '.join(GATES)}.")
        with self._lock:
            project = self._projects.get(pid)
            if project is None:
                return None
            self._idle(project)
            ready = {
                "edl": (self.dir(pid) / "plan.json").is_file(),
                "storyboard": (self.dir(pid) / "storyboard.json").is_file(),
                "export": (self.dir(pid) / "qa_report.json").is_file(),
            }
            if not ready[gate]:
                raise Conflict({"edl": "Плана ещё нет.", "storyboard": "Раскадровки ещё нет.",
                                "export": "QA-отчёта ещё нет: сначала собери ролик."}[gate])  # fmt: skip
            self._pass_gate(project, gate, auto=False)
            if gate == "edl" and project["status"] == "review":
                project["progress"] = {"stage": "План подтверждён — можно собирать", "percent": 100.0}
            self._touch(project)
            self._snapshot(project, f"approve {gate}")
            return self._view(project)

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
        options = clean_options(data.get("options"), DEFAULT_OPTIONS | {"model": asr.default_id(self.config)})
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
            "approvals": {gate: None for gate in GATES},
            "sync": None,
            "screen_offset": 0.0,
            "transcript": None,
            "plan": None,
            "output": None,
            "check": None,
            "motion": None,
            "storyboard": None,  # сводка сцен (storyboard.summary)
            "timeline": None,  # сводка дорожек (timeline.summary)
            "qa": None,  # ворота и счётчики qa_report.json
            "exports": 0,  # номер последнего экспорта
            "progress": {"stage": "Ждёт записи", "percent": 0.0},
            "stages": [],  # этапы текущего действия (stages.py, PRD-025)
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

    def story(self, pid: str) -> dict | None:
        """Знания → биты → стиль → раскадровка (MASTER_SPEC §5.3–5.4); потом ворота storyboard."""
        return self._enqueue(pid, "story", need_words=True)

    def redo_scene(self, pid: str, sid: str, text: object) -> dict | None:
        """Одна сцена заново по замечанию автора: раскадровщик только для её бита."""
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_FEEDBACK:
            raise ValidationError(f"Замечание — непустой текст до {MAX_FEEDBACK} знаков.")
        board = self._read_json(pid, "storyboard.json") or {}
        if not any(s["id"] == sid for s in board.get("scenes") or []):
            raise Conflict("Такой сцены нет.")
        return self._enqueue(pid, "scene", json.dumps({"scene": sid, "text": text.strip()}), need_words=True)

    # ---------- материалы, раскадровка, стиль (чтение и правки) ----------

    def documents(self, pid: str) -> list[dict] | None:
        if pid not in self._projects:
            return None
        return documents.load(self.dir(pid))

    def add_document(self, pid: str, name: str, data: bytes) -> dict | None:
        with self._lock:
            project = self._projects.get(pid)
            if project is None:
                return None
            self._idle(project)
        try:
            record = documents.add(self.dir(pid), name, data)
        except documents.DocumentError as exc:
            raise ValidationError(str(exc)) from exc
        with self._lock:
            self._invalidate_knowledge(project)
            self._history(project, "document", f"{record['name']} ({record['status']})")
            self._touch(project)
            self._snapshot(project, f"document {record['id']}")
        return record

    def remove_document(self, pid: str, doc_id: str) -> bool:
        with self._lock:
            project = self._projects.get(pid)
            if project is None:
                return False
            self._idle(project)
        removed = documents.remove(self.dir(pid), doc_id)
        if removed:
            with self._lock:
                self._invalidate_knowledge(project)
                self._touch(project)
        return removed

    def _invalidate_knowledge(self, project: dict) -> None:
        """Материалы изменились — карта знаний и всё после неё строятся заново (§6)."""
        folder = self.dir(project["id"])
        for name in ("chunks.json", "knowledge.json"):
            (folder / name).unlink(missing_ok=True)
        self._invalidate_after_plan(project)

    def story_view(self, pid: str) -> dict | None:
        """Знания, биты, стиль, сцены и сводка — для экрана раскадровки."""
        if pid not in self._projects:
            return None
        board = self._read_json(pid, "storyboard.json") or {}
        items = board.get("scenes") or []
        tm = self._read_json(pid, "timemap.json") or {}
        return {
            "knowledge": self._read_json(pid, "knowledge.json") or {"claims": []},
            "beats": self._read_json(pid, "beats.json") or {"beats": [], "pacing": {}},
            "style": style_module.load(self.dir(pid)),
            "scenes": items,
            "summary": storyboard.summary(items),
            "duration_frames": tm.get("duration_frames", 0),
            "fps": tm.get("fps", render.FPS),
            "templates": templates.public(),
            "made_at": board.get("made_at"),
            "flagged": knowledge.flagged((self._read_json(pid, "knowledge.json") or {}).get("claims") or []),
        }

    def save_storyboard(self, pid: str, payload: object) -> dict | None:
        """Правки сцен человеком: enabled, кадры, шаблон, props — по id."""
        data = payload if isinstance(payload, dict) else {}
        with self._lock:
            project = self._projects.get(pid)
            if project is None:
                return None
            self._idle(project)
        board = self._read_json(pid, "storyboard.json")
        if not board:
            raise Conflict("Раскадровки ещё нет: нажми «Раскадровка».")
        tm = self._read_json(pid, "timemap.json") or {}
        max_frames = int(tm.get("duration_frames") or 0)
        patches = {p.get("id"): p for p in data.get("scenes") or [] if isinstance(p, dict)}
        changed = []
        items = []
        for scene in board["scenes"]:
            patch = patches.get(scene["id"])
            if patch is None:
                items.append(scene)
                continue
            try:
                new = storyboard.edit(scene, patch, max_frames)
            except storyboard.EditError as exc:
                raise ValidationError(f"{scene['id']}: {exc}") from exc
            items.append(new)
            if new != scene:
                changed.append(scene["id"])
        self._write_storyboard(project, items, source="human")
        with self._lock:
            if changed:
                self._reset_gate(project, "storyboard")
                decisions.record(self.dir(pid), "human", "storyboard:edit", ids=changed, rationale="правка сцен")
                self._history(project, "edit", f"сцены поправлены: {', '.join(changed)}")
            self._touch(project)
            if changed:
                self._snapshot(project, "storyboard edited")
            return self._view(project)

    def save_style(self, pid: str, payload: object) -> dict | None:
        """Правка стиль-библии → новая ревизия → сцены перерисуются (§6)."""
        with self._lock:
            project = self._projects.get(pid)
            if project is None:
                return None
            self._idle(project)
        folder = self.dir(pid)
        current = style_module.load(folder) or style_module.DEFAULT
        new = style_module.save(folder, style_module.clean(payload, current))
        board = self._read_json(pid, "storyboard.json")
        if board:
            self._write_storyboard(project, storyboard.with_style(board["scenes"], new), source="style")
        with self._lock:
            decisions.record(folder, "human", "style:edit", output=new, ids=[new["revision"]])
            self._history(project, "style", f"стиль {new['revision']}: {new['name']}")
            self._touch(project)
            self._snapshot(project, f"style {new['revision']}")
        return new

    def scene_file(self, pid: str, sid: str, name: str) -> Path | None:
        if pid not in self._projects or not storyboard_id_ok(sid):
            return None
        path = scenes.folder_of(self.dir(pid), sid) / name
        return path if path.is_file() else None

    # ---------- таймлайн, QA, экспорт, ревизии (MASTER_SPEC §5.7–5.8, §8) ----------

    def timeline_view(self, pid: str) -> dict | None:
        if pid not in self._projects:
            return None
        data = self._read_json(pid, "timeline.json")
        return data or {
            "schema_version": montage_schemas.SCHEMA_VERSION,
            "fps": render.FPS,
            "duration_frames": 0,
            "tracks": [],
        }

    def qa_view(self, pid: str) -> dict | None:
        if pid not in self._projects:
            return None
        return qa.load(self.dir(pid)) or {
            "items": [],
            "gates": {"mechanical": "none", "semantic": "none"},
            "counts": {},
        }

    def export(self, pid: str, payload: object) -> dict | None:
        """Экспорт по пресету; при `mechanical: fail` — только с override_reason (§5.8), причина в журнал."""
        data = payload if isinstance(payload, dict) else {}
        preset_id = data.get("preset") or exports.DEFAULT
        try:
            exports.preset(preset_id)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        reason = data.get("override_reason") or ""
        if not isinstance(reason, str) or len(reason) > MAX_FEEDBACK:
            raise ValidationError("Причина — строка.")
        with self._lock:
            project = self._projects.get(pid)
            if project is None:
                return None
            self._idle(project)
            if not (self.dir(pid) / "output.mp4").is_file():
                raise Conflict("Ролик ещё не собран.")
            report = qa.load(self.dir(pid)) or {}
            gate = (report.get("gates") or {}).get("mechanical", "none")
            if gate == "fail" and not reason.strip():
                raise Conflict("QA нашёл ошибки — экспорт только с причиной (override_reason), она попадёт в журнал.")
        return self._enqueue(pid, "export", json.dumps({"preset": preset_id, "override_reason": reason.strip()}))

    def exports_list(self, pid: str) -> list[dict] | None:
        if pid not in self._projects:
            return None
        return exports.listing(self.dir(pid), f"{self._url}/projects/{pid}/exports")

    def export_file(self, pid: str, n: str, name: str) -> Path | None:
        if pid not in self._projects or not n.isdigit():
            return None
        folder = self.dir(pid) / exports.DIR / n
        if name == "file":
            found = [p for p in folder.glob("export.*") if p.is_file()] if folder.is_dir() else []
            return found[0] if found else None
        path = folder / name
        return path if path.is_file() else None

    def revisions_list(self, pid: str) -> list[dict] | None:
        if pid not in self._projects:
            return None
        return revisions.listing(self.dir(pid))

    def restore(self, pid: str, n: object) -> dict | None:
        """Вернуть манифесты снимка; медиа и кэш не трогаются, сам откат — новый снимок."""
        if not isinstance(n, int) or isinstance(n, bool) or n < 1:
            raise ValidationError("Номер ревизии — целое число.")
        with self._lock:
            project = self._projects.get(pid)
            if project is None:
                return None
            self._idle(project)
            folder = self.dir(pid)
            restored = revisions.restore(folder, n)
            if restored is None:
                raise Conflict("Такой ревизии нет.")
            keep = {k: project[k] for k in ("id", "created_at", "log", "sources", "backend")}
            fresh = {**project, **{k: v for k, v in restored.items() if k != "schema"}, **keep}
            if fresh.get("status") in ACTIVE:
                fresh["status"] = "interrupted"
            project.clear()
            project.update(fresh)
            project.setdefault("approvals", {gate: None for gate in GATES})
            decisions.record(folder, "human", "revision:restore", ids=[str(n)], rationale=f"откат к ревизии {n}")
            self._history(project, "restore", f"ревизия {n}")
            self._touch(project)
            self._snapshot(project, f"restore {n}")
            return self._view(project)

    def _snapshot(self, project: dict, label: str) -> None:
        try:
            revisions.snapshot(self.dir(project["id"]), label)
        except (OSError, ValueError) as exc:  # снимок — не повод ронять действие
            project["log"].append(f"ревизия не записана: {exc}")

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
            self._reset_gate(project, "edl")  # правка плана — новое решение, подтверждение заново (§6)
        self._write_edl(project, words, cuts, layout)
        with self._lock:
            self._set_plan_summary(project, words, cuts, layout)
            self._invalidate_after_plan(project)
            decisions.record(self.dir(pid), "human", "plan:edit", output=new, rationale="план поправлен вручную",
                             ids=[f"cut:{c['from']}-{c['to']}" for c in cuts][:50])  # fmt: skip
            self._history(project, "edit", f"план поправлен вручную: вырезок {len(cuts)}")
            self._touch(project)
            self._snapshot(project, "plan edited")
            return self._view(project)

    def _invalidate_after_plan(self, project: dict) -> None:
        """Новый план вырезок: карта времени перестроена (_write_edl), а биты и раскадровка, привязанные к
        старым кадрам, недействительны (§6); рендеры сцен остаются в кэше по хэшу."""
        folder = self.dir(project["id"])
        for name in ("beats.json", "storyboard.json", "timeline.json"):
            (folder / name).unlink(missing_ok=True)
        self._reset_gate(project, "storyboard")

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
            mode = project["options"]["mode"]
            approvals = project.get("approvals") or {}
            if action in ("render", "story") and mode != "autonomous" and not approvals.get("edl"):
                raise Conflict("План вырезок не подтверждён: нажми «Подтвердить план» (или выбери режим «Автономно»).")
            if action in ("replan", "revise") and mode == "manual":
                raise Conflict("В режиме «Вручную» план ИИ не строится — правь вырезки в тексте.")
            if action == "scene" and not (self.dir(pid) / "storyboard.json").is_file():
                raise Conflict("Раскадровки ещё нет.")
            if action == "export" and not (self.dir(pid) / "output.mp4").is_file():
                raise Conflict("Ролик ещё не собран.")
            model = asr.get(project["options"]["model"])
            if action == "prepare" and model is not None and model.id in self.config.disabled_models():
                raise Conflict(f"Модель {model.label} выключена во вкладке «Настройки» — выбери другую.")
            if action == "prepare" and model is not None and not asr.installed(model, self.config):
                raise Conflict(f"Модель {model.label} не установлена: make models M={model.id}.")
            has_board = (self.dir(pid) / "storyboard.json").is_file()
            if action == "render" and has_board and mode != "autonomous" and not approvals.get("storyboard"):
                raise Conflict("Раскадровка не подтверждена: проверь сцены и нажми «Утвердить раскадровку».")
            needs_llm = (
                (action == "prepare" and mode != "manual")
                or action in ("replan", "revise", "story", "scene")
                or (
                    action == "render"
                    and (project["options"]["check"] or (project["options"]["motion"] and not has_board))
                )
            )
            if needs_llm and not self.config.llm_available():
                raise Conflict(llm.missing_cli(self.config.llm_cmd[0]))
            event = threading.Event()
            self._cancels[pid] = event
            project["status"] = "queued"
            project["action"] = action
            project["error"] = None
            project["progress"] = {"stage": "В очереди", "percent": 0.0}
            screen = project["sources"].get("screen") or {}
            ids = stages.plan_for(action, project["options"], screen_audio=bool(screen.get("has_audio")),
                                  storyboard=has_board)  # fmt: skip
            project["stages"] = stages.new(ids)
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
            project, upgraded = schema.load_record(
                path, SCHEMA, check=FIELDS, migrations=MIGRATIONS, problems=self.load_problems, label=label
            )
            if project is None:
                continue
            project.setdefault("stages", [])
            project.setdefault("approvals", {gate: None for gate in GATES})
            if project["status"] in ACTIVE:
                project["status"] = "interrupted"
                project["error"] = INTERRUPTED
                project["progress"]["stage"] = "Прервано"
                stages.finish(project["stages"], "failed")
                self._save(project)
            elif upgraded:
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
        elif status == "review":
            project["progress"] = {"stage": "Ждёт подтверждения плана", "percent": 100.0}
        elif status == "waiting_approval":
            project["progress"] = {"stage": stage or "Ждёт подтверждения", "percent": 100.0}
        elif status == "blocked":
            percent = project["progress"].get("percent", 0.0)
            project["progress"] = {"stage": stage or "Заблокировано", "percent": percent}
        else:
            project["progress"]["stage"] = stage or ("Ошибка" if status == "failed" else "Отменено")
        self._touch(project)

    def _stage(self, project: dict, cancel: threading.Event, stage_id: str, percent: float, detail: str = "",
               *, restart: bool = False) -> None:  # fmt: skip
        """Этап идёт: процент внутри него и деталь; общая строка progress — для списка роликов.

        restart — процент этапа можно сбросить (второй круг проверки).
        """
        with self._lock:
            if cancel.is_set():
                return
            items = project.setdefault("stages", [])
            if restart:
                for item in items:
                    if item["id"] == stage_id:
                        item["percent"] = 0.0
            stages.advance(items, stage_id, percent, detail)
            label = stages.STAGES.get(stage_id, (stage_id, 0))[0]
            project["progress"] = {"stage": f"{label}: {detail}" if detail else label, "percent": stages.overall(items)}

    def _ffmpeg_run(self, argv: list[str], cancel: threading.Event, total_s: float,
                    on_percent: Callable[[float], None], lines: list[str] | None = None) -> list[str]:  # fmt: skip
        """ffmpeg с -progress: доля обработанного времени к total_s → on_percent; остальное — в lines."""
        lines = [] if lines is None else lines
        argv = [argv[0], "-progress", "pipe:1", "-nostats", *argv[1:]]

        def on_line(line: str) -> None:
            text = line.strip()
            found = _FF_TIME.match(text)
            if found:
                if total_s > 0:
                    on_percent(min(100.0, int(found[1]) / 1e6 / total_s * 100))
            elif text and not _FF_KEY.match(text):
                lines.append(line)

        runner.run(argv, on_line, cancel, kind="render")
        return lines

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
        mode = project["options"]["mode"]
        auto = mode == "autonomous"
        stage_text = None
        try:
            if action == "prepare":
                self._prepare(project, cancel)
            elif action == "replan":
                self._plan(project, cancel)
            elif action == "revise":
                self._revise(project, arg, cancel)
            if action == "prepare" and auto:
                with self._lock:
                    self._pass_gate(project, "edl", auto=True)
            # Раскадровка: по кнопке, а в autonomous с графикой — сразу после плана.
            if action == "story" or (action == "prepare" and auto and project["options"]["motion"]):
                self._story(project, cancel)
            elif action == "scene":
                self._redo_scene(project, json.loads(arg), cancel)
            made_board = action in ("story", "scene") or (action == "prepare" and auto and project["options"]["motion"])
            if made_board and auto:
                with self._lock:
                    self._pass_gate(project, "storyboard", auto=True)
            if action == "render" or (auto and action in ("prepare", "story", "scene")):
                self._render(project, cancel)
                status = "done"
                if auto:
                    self._auto_export(project, cancel)
            elif action == "export":
                self._export(project, json.loads(arg), cancel)
                status = "done"
            elif action in ("story", "scene"):
                status, stage_text = "waiting_approval", "Ждёт подтверждения раскадровки"
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
                self._finish(project, status, stage=stage_text)
        with self._lock:
            self._snapshot(project, action)

    # ---------- шаги ----------

    def _ffmpeg(self, args: list[str], cancel: threading.Event, what: str, total_s: float = 0.0,
                on_percent: Callable[[float], None] | None = None) -> list[str]:  # fmt: skip
        lines: list[str] = []
        argv = [self.config.ffmpeg, "-y", "-loglevel", "error", *args]
        try:
            self._ffmpeg_run(argv, cancel, total_s, on_percent or (lambda _: None), lines)
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

    def _audio(self, source: Path, wav: Path, cancel: threading.Event, duration: float = 0.0,
               on_percent: Callable[[float], None] | None = None) -> None:  # fmt: skip
        self._ffmpeg(["-i", str(source), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav)], cancel,
                     "звук для расшифровки", duration, on_percent)  # fmt: skip

    def _silences(self, wav: Path, duration: float, cancel: threading.Event,
                  on_percent: Callable[[float], None] | None = None) -> list[tuple[float, float]]:  # fmt: skip
        argv = transcript.silence_argv(self.config.ffmpeg, wav)
        lines = self._ffmpeg_run(argv, cancel, duration, on_percent or (lambda _: None))
        return transcript.parse_silences(lines, duration)

    def _transcribe(
        self, project: dict, wav: Path, out: Path, silences, cancel: threading.Event, stage: str, lo: float,
        hi: float, what: str = "",
    ) -> dict:  # fmt: skip
        """Воркер расшифровки; его PROGRESS — доля [lo, hi] процентов этапа stage."""
        model = asr.get(project["options"]["model"]) or asr.CATALOG[0]
        duration = media.duration(wav, self.config.ffmpeg)
        hotwords = [*plan.terms(self.lessons()), *split_terms(project["options"]["terms"])]
        argv = asr.build_argv(
            self.config, model, wav, out, splits=transcript.split_points(duration, silences), hotwords=hotwords
        )

        def on_line(line: str) -> None:
            step = asr.parse_progress(line)
            if step:
                detail = f"{what}{step[1] or model.label}"
                self._stage(project, cancel, stage, lo + (hi - lo) * step[0] / 100, detail)
            elif line.strip():
                self._note(project, line)

        def on_wait(holder: str) -> None:
            self._stage(project, cancel, stage, lo, f"{what}ждёт GPU (занят: {holder})")

        with self._gate.hold(GPU_OWNER, on_wait, cancel):
            self._stage(project, cancel, stage, lo, f"{what}{model.label}")
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
        duration = project["sources"]["head"]["duration"]
        self._stage(project, cancel, "audio", 0)
        wav = folder / "audio.wav"
        self._audio(head, wav, cancel, duration, lambda pct: self._stage(project, cancel, "audio", pct))
        screen = self.source(pid, "screen")
        if screen is not None and project["sources"]["screen"].get("has_audio"):
            self._stage(project, cancel, "sync", 0)
            found = sync.detect(self.config.ffmpeg, head, screen)
            with self._lock:
                project["sync"] = found
                if found["method"] == "audio":
                    project["screen_offset"] = found["offset"]
            self._note(project, f"Сдвиг экрана: {found['offset']:+.2f} с ({found['method']}, {found['score']:.2f})")
        self._stage(project, cancel, "silences", 0)
        silences = self._silences(wav, duration, cancel, lambda pct: self._stage(project, cancel, "silences", pct))
        atomic_write(folder / "silences.json", json.dumps(silences))
        data = self._transcribe(project, wav, folder / "words.json", silences, cancel, "transcribe", 0, 100)
        data["source_id"] = "head"
        data["schema_version"] = montage_schemas.SCHEMA_VERSION
        atomic_write(folder / "words.json", json.dumps(data, ensure_ascii=False))
        self._source_manifest(project)
        with self._lock:
            project["transcript"] = {
                "model": data["model"],
                "words": len(data["words"]),
                "elapsed_s": data.get("elapsed_s"),
            }
            self._history(project, "transcribe", f"слов: {len(data['words'])}")
            self._touch(project)
        if project["options"]["mode"] == "manual":
            # Человек режет сам: пустой план, чтобы экран подтверждения открылся без LLM.
            self._store_plan(project, data["words"], {"cuts": [], "layout": [{"from": 0, "layout": "head"}],
                                                      "notes": "Режим «Вручную»: отметь вырезки сам.", "lesson": ""},
                             "manual")  # fmt: skip
            return
        self._plan(project, cancel)

    def _source_manifest(self, project: dict) -> None:
        """source_manifest.json (§5.1): sha256 и probe каждой записи — provenance экспорта."""
        pid = project["id"]
        items = []
        for role, meta in project["sources"].items():
            path = self.source(pid, role)
            if not meta or path is None:
                continue
            items.append({**meta, "role": role, "sha256": decisions.file_sha(path)})
        montage_schemas.check("source_media", items, many=True)
        data = {"schema_version": montage_schemas.SCHEMA_VERSION, "made_at": time.time(), "sources": items}
        atomic_write(self.dir(pid) / "source_manifest.json", json.dumps(data, ensure_ascii=False, indent=2))

    def _llm(self, prompt: str, project: dict, cancel: threading.Event, *, role: str = "", ids=()) -> object:
        """Вызов LLM; с role — запись в журнал решений (модель, версия промпта, хэши)."""
        answer = llm.complete(prompt, cmd=self.config.llm_cmd, cancel=cancel)
        data = llm.extract_json(answer)
        if role:
            decisions.record(self.dir(project["id"]), role, f"{role}:llm", model=" ".join(self.config.llm_cmd),
                             prompt=prompt, inputs=prompt, output=data, ids=ids)  # fmt: skip
        return data

    def _plan(self, project: dict, cancel: threading.Event) -> None:
        pid = project["id"]
        words = (self._read_json(pid, "words.json") or {}).get("words") or []
        has_screen = bool(project["sources"].get("screen"))
        self._stage(project, cancel, "plan", 0, f"ИИ читает {len(words)} слов")
        prompt = plan.plan_prompt(plan.pack(words), plan.base_rules(), self.lessons(), has_screen, project["title"])
        result = plan.parse_plan(self._llm(prompt, project, cancel, role="rough_cut"), len(words), has_screen)
        self._store_plan(project, words, result, "plan")

    def _revise(self, project: dict, feedback: str, cancel: threading.Event) -> None:
        pid = project["id"]
        words = (self._read_json(pid, "words.json") or {}).get("words") or []
        has_screen = bool(project["sources"].get("screen"))
        current = self._read_json(pid, "plan.json") or {}
        self._stage(project, cancel, "plan", 0, "переделываю по замечанию")
        prompt = plan.revise_prompt(
            plan.pack(words), plan.base_rules(), self.lessons(), has_screen,
            current.get("cuts") or [], current.get("layout") or [{"from": 0, "layout": "head"}], feedback,
        )  # fmt: skip
        result = plan.parse_plan(self._llm(prompt, project, cancel, role="rough_cut_revise"), len(words), has_screen)
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
        folder = self.dir(project["id"])
        atomic_write(folder / "plan.json", json.dumps(data, ensure_ascii=False))
        self._write_edl(project, words, result["cuts"], result["layout"])
        with self._lock:
            project["plan"] = {"notes": result["notes"]}
            self._set_plan_summary(project, words, result["cuts"], result["layout"])
            self._reset_gate(project, "edl")
            self._invalidate_after_plan(project)
            self._history(project, "plan", f"вырезок {len(result['cuts'])}")
            self._touch(project)

    def _write_edl(self, project: dict, words: list[dict], cuts: list[dict], layout: list[dict]) -> None:
        """edl.json (EditDecision, §7) и timemap.json (§4) по текущему плану."""
        pid = project["id"]
        duration = project["sources"]["head"]["duration"]
        silences = [tuple(s) for s in self._read_json(pid, "silences.json", [])]
        result = edl.build(words, duration, cuts, layout, silences, max_pause=project["options"]["max_pause"])
        items = edl.decisions_of(words, cuts, layout, approved=bool((project.get("approvals") or {}).get("edl")))
        montage_schemas.check("edit_decision", items, many=True)
        atomic_write(self.dir(pid) / "edl.json", json.dumps(
            {"schema_version": montage_schemas.SCHEMA_VERSION, "source_id": "head", "decisions": items,
             "segments": result.as_list()}, ensure_ascii=False))  # fmt: skip
        tm = timemap.TimeMap.from_edl(result, "head", source_duration=duration)
        problems = tm.check()
        if problems:
            raise runner.RunFailed("Карта времени не сошлась: " + "; ".join(problems[:3]))
        montage_schemas.check("time_map_segment", [s.as_dict() for s in tm.segments], many=True)
        atomic_write(self.dir(pid) / "timemap.json", json.dumps(tm.as_dict(), ensure_ascii=False))

    # ---------- раскадровка: знания → биты → стиль → сцены (MASTER_SPEC §5.3–5.4) ----------

    def _edit_words(self, project: dict) -> tuple[timemap.TimeMap, list[dict], list[dict]]:
        """(карта времени, слова исходника, слова результата с кадрами) по текущему плану."""
        pid = project["id"]
        words = (self._read_json(pid, "words.json") or {}).get("words") or []
        data = self._read_json(pid, "timemap.json")
        if not data:
            current = self._read_json(pid, "plan.json") or {}
            self._write_edl(
                project, words, current.get("cuts") or [], current.get("layout") or [{"from": 0, "layout": "head"}]
            )
            data = self._read_json(pid, "timemap.json") or {}
        tm = timemap.TimeMap.load(data)
        return tm, words, tm.words_in_edit(words)

    def _story(self, project: dict, cancel: threading.Event) -> None:
        pid = project["id"]
        folder = self.dir(pid)
        title = project["title"]
        self._stage(project, cancel, "timemap", 0)
        tm, _words, out_words = self._edit_words(project)
        version = montage_schemas.SCHEMA_VERSION
        self._dump(folder / "transcript_edit.json", {"schema_version": version, "fps": tm.fps, "words": out_words})
        packed = plan.pack([{k: w[k] for k in ("w", "s", "e")} for w in out_words])
        # Знания: материалы → фрагменты → утверждения с цитатами.
        chunks = documents.chunks_of(folder)
        atomic_write(folder / "chunks.json", json.dumps({"chunks": chunks}, ensure_ascii=False))
        self._stage(
            project, cancel, "knowledge", 0, f"материалов: {len(documents.load(folder))}, фрагментов: {len(chunks)}"
        )
        raw = self._llm(knowledge.prompt(packed, chunks, title), project, cancel, role="research")
        claims = knowledge.clean(raw, out_words, chunks)
        montage_schemas.check("knowledge_claim", claims, many=True)
        self._dump(folder / "knowledge.json", {"schema_version": version, "claims": claims, "chunks": len(chunks)})
        # Биты.
        self._stage(project, cancel, "beats", 0, f"утверждений: {len(claims)}")
        raw = self._llm(narrative.prompt(packed, claims, title), project, cancel, role="narrative")
        beats = narrative.clean(raw, out_words, claims)
        montage_schemas.check("narrative_beat", beats, many=True)
        pacing = narrative.pacing(beats, tm.fps)
        self._dump(folder / "beats.json", {"schema_version": version, "beats": beats, "pacing": pacing})
        # Стиль: есть — оставляем (человек мог править), нет — спрашиваем арт-директора.
        style = style_module.load(folder)
        if style is None:
            self._stage(project, cancel, "style", 0)
            topic = beats[0]["goal"] if beats else title
            raw = self._llm(style_module.prompt(topic, narrative.pack_beats(beats)[:4000], title), project, cancel,
                            role="art_director")  # fmt: skip
            style = style_module.save(folder, style_module.clean(raw))
        else:
            self._stage(project, cancel, "style", 100, f"стиль {style['revision']} уже есть")
        # Раскадровка.
        self._stage(project, cancel, "storyboard", 0, f"битов: {len(beats)}")
        raw = self._llm(storyboard.prompt(beats, claims, style, title), project, cancel, role="storyboard")
        items = storyboard.clean(raw, beats, claims, style, out_words, tm.fps)
        self._write_storyboard(project, items, source="llm")
        with self._lock:
            self._reset_gate(project, "storyboard")
            self._history(project, "storyboard", f"сцен {len(items)} из {len(beats)} битов")
            self._touch(project)

    def _redo_scene(self, project: dict, arg: dict, cancel: threading.Event) -> None:
        """Одна сцена заново: раскадровщик видит только её бит и замечание автора."""
        pid = project["id"]
        folder = self.dir(pid)
        board = self._read_json(pid, "storyboard.json") or {}
        items = list(board.get("scenes") or [])
        old = next(s for s in items if s["id"] == arg["scene"])
        tm, _words, out_words = self._edit_words(project)
        beats = [b for b in (self._read_json(pid, "beats.json") or {}).get("beats") or [] if b["id"] == old["beat_id"]]
        claims = (self._read_json(pid, "knowledge.json") or {}).get("claims") or []
        style = style_module.load(folder) or style_module.save(folder, style_module.clean({}))
        self._stage(project, cancel, "storyboard", 0, f"сцена {old['id']} по замечанию")
        shown = json.dumps({k: old[k] for k in ("template_id", "props", "intent")}, ensure_ascii=False)
        text = storyboard.prompt(beats, claims, style, project["title"]) + (
            f"\nПрошлая сцена:\n{shown}\nЗамечание автора — переделай сцену с учётом него:\n{arg['text']}\n"
        )
        raw = self._llm(text, project, cancel, role="storyboard", ids=[old["id"]])
        new = storyboard.clean(raw, beats, claims, style, out_words, tm.fps)
        if not new:
            raise runner.RunFailed("Раскадровщик не предложил сцену по замечанию — попробуй сформулировать иначе.")
        scene = {**new[0], "id": old["id"], "enabled": True}
        scene["hash"] = storyboard.scene_hash(scene)
        items = [scene if s["id"] == old["id"] else s for s in items]
        self._write_storyboard(project, items, source="redo")
        with self._lock:
            self._reset_gate(project, "storyboard")
            self._history(project, "feedback", f"{old['id']}: {arg['text'][:80]}")
            self._touch(project)

    @staticmethod
    def _dump(path: Path, data: dict) -> None:
        atomic_write(path, json.dumps(data, ensure_ascii=False))

    def _write_storyboard(self, project: dict, items: list[dict], *, source: str) -> None:
        montage_schemas.check("storyboard_scene", items, many=True)
        data = {
            "schema_version": montage_schemas.SCHEMA_VERSION,
            "scenes": items,
            "source": source,
            "made_at": time.time(),
        }
        atomic_write(self.dir(project["id"]) / "storyboard.json", json.dumps(data, ensure_ascii=False, indent=1))
        with self._lock:
            project["storyboard"] = storyboard.summary(items)

    def _scenes(
        self, project: dict, out_words: list[dict], cancel: threading.Event
    ) -> tuple[list[render.Overlay], dict]:
        """Сцены раскадровки для финала: перепривязка к кадрам, рендер по хэшу (§5.6), наложения."""
        pid = project["id"]
        folder = self.dir(pid)
        board = self._read_json(pid, "storyboard.json") or {}
        beats = (self._read_json(pid, "beats.json") or {}).get("beats") or []
        style = style_module.load(folder) or style_module.DEFAULT
        items = storyboard.reanchor(board.get("scenes") or [], out_words, beats)
        self._stage(project, cancel, "scenes", 0, f"сцен: {sum(1 for s in items if s.get('enabled', True))}")
        items = scenes.render_all(
            self.config, folder, items, style, cancel,
            note=lambda line: self._note(project, line),
            progress=lambda detail, percent: self._stage(project, cancel, "scenes", percent * 0.9, detail),
        )  # fmt: skip
        self._write_storyboard(project, items, source=board.get("source", "llm"))
        summary = storyboard.summary(items)
        blocked = [s for s in items if s["status"] == "blocked"]
        if blocked:
            summary["skipped"] = blocked[0].get("error") or "сцены заблокированы"
        return scenes.overlays(folder, items), summary

    # ---------- сборка ----------

    def _render(self, project: dict, cancel: threading.Event) -> None:
        pid = project["id"]
        folder = self.dir(pid)
        words = (self._read_json(pid, "words.json") or {}).get("words") or []
        current = self._read_json(pid, "plan.json") or {}
        cuts = list(current.get("cuts") or [])
        layout = current.get("layout") or [{"from": 0, "layout": "head"}]
        rounds: list[dict] = []
        for n in range(MAX_ROUNDS + 1):
            # Первая сборка — свои этапы; пересборка по найденному — внутри этапа «Проверка».
            result, out_words = self._assemble(project, words, cuts, layout, cancel, rebuild=n > 0)
            if not project["options"]["check"] or n == MAX_ROUNDS:
                found = self._tech_check(project, cancel) if not rounds else []
                rounds.append({"round": n + 1, "issues": [], "tech": found})
                break
            self._stage(project, cancel, "check", 0, f"круг {n + 1}: паузы в результате", restart=n > 0)
            tech = self._tech_check(project, cancel)
            issues = self._asr_check(project, out_words, cancel, n + 1)
            rounds.append({"round": n + 1, "issues": issues, "tech": tech})
            if not issues:
                break
            cuts = edl.clean_cuts([*cuts, *issues], len(words), default_kind="check")
            current["cuts"] = cuts
            atomic_write(folder / "plan.json", json.dumps(current, ensure_ascii=False))
            self._write_edl(project, words, cuts, layout)  # карта времени — по финальным вырезкам (§4)
            self._note(project, f"Проверка нашла {len(issues)}, пересобираю")
        info = None
        inserts: list[dict] = []
        board_scenes: list[dict] = []
        has_board = (folder / "storyboard.json").is_file()
        tm, _w, frame_words = self._edit_words(project)
        if has_board:
            self._scenes(project, frame_words, cancel)
            board_scenes = (self._read_json(pid, "storyboard.json") or {}).get("scenes") or []
            info = storyboard.summary(board_scenes)
            blocked = [s for s in board_scenes if s["status"] == "blocked"]
            if blocked:
                info["skipped"] = blocked[0].get("error") or "сцены заблокированы"
            stage_id = "scenes"
        elif project["options"]["motion"]:
            quick, info = motion.run(
                self.config, project, folder, out_words, result.duration, cancel,
                ask=lambda text: self._llm(text, project, cancel, role="motion"),
                note=lambda line: self._note(project, line),
                progress=lambda detail, percent: self._stage(project, cancel, "motion", percent, detail),
            )  # fmt: skip
            inserts = [{"at": o.start, "duration": o.duration, "path": str(o.path.relative_to(folder))} for o in quick]
            stage_id = "motion"
        else:
            stage_id = "final"
        # Таймлайн-манифест (§5.7): финал берёт наложения из него, а не из раскадровки напрямую.
        captions = render.chunks(out_words)
        manifest = timeline.build(tm, board_scenes, captions, has_screen=bool(project["sources"].get("screen")),
                                  inserts=inserts)  # fmt: skip
        problems = timeline.check(manifest)
        if problems:
            raise runner.RunFailed("Таймлайн не сошёлся: " + "; ".join(problems[:3]))
        atomic_write(folder / "timeline.json", json.dumps(manifest, ensure_ascii=False))
        overlays = timeline.overlays(manifest, folder)
        if overlays:
            self._final(project, out_words, overlays, cancel, stage=(stage_id, 90, 100, "наложение"))
        self._qa(project, manifest, board_scenes, captions, rounds, cancel)
        with self._lock:
            project["output"] = {
                "duration": round(media.duration(folder / "output.mp4", self.config.ffmpeg), 1),
                "segments": len(result.segments),
                "size": (folder / "output.mp4").stat().st_size,
            }
            project["check"] = {"rounds": rounds}
            project["motion"] = info
            project["timeline"] = timeline.summary(manifest)
            self._set_plan_summary(project, words, cuts, layout)
            fixed = sum(len(r["issues"]) for r in rounds)
            self._history(project, "render", f"{result.duration:.0f} с, исправлено проверкой: {fixed}")
            self._touch(project)

    def _qa(self, project, manifest, board_scenes, captions, rounds, cancel) -> None:
        """qa_report.json (§5.8): механика по файлу и манифестам, семантика по кругам проверки."""
        folder = self.dir(project["id"])
        self._stage(project, cancel, "qa", 0, "механика: ffprobe, чёрные кадры, паузы, пик")
        style = style_module.load(folder)
        items = qa.mechanical(self.config, folder, project, manifest, board_scenes, captions, style, cancel)
        sem_items, mode = qa.semantic(project, rounds, llm_available=self.config.llm_available(), max_rounds=MAX_ROUNDS)
        attempts = max((r["round"] for r in rounds if r["issues"]), default=0)
        report = qa.report(items, sem_items, mode, attempts)
        atomic_write(folder / qa.FILE, json.dumps(report, ensure_ascii=False, indent=1))
        decisions.record(
            folder,
            "technical_qa",
            "qa:report",
            output=report["gates"],
            rationale=f"ошибок {report['counts']['error']}, предупр. {report['counts']['warning']}",
        )
        with self._lock:
            project["qa"] = {"gates": report["gates"], "counts": report["counts"], "attempts": attempts}
            self._reset_gate(project, "export")
            self._history(
                project, "qa", f"механика {report['gates']['mechanical']}, семантика {report['gates']['semantic']}"
            )
        self._stage(project, cancel, "qa", 100)

    def _auto_export(self, project: dict, cancel: threading.Event) -> None:
        """autonomous: экспорт по умолчанию, если механические ворота прошли (§5.8)."""
        report = qa.load(self.dir(project["id"])) or {}
        if (report.get("gates") or {}).get("mechanical") != "pass":
            self._note(project, "Экспорт пропущен: QA нашёл ошибки — посмотри отчёт и экспортируй с причиной.")
            return
        with self._lock:
            self._pass_gate(project, "export", auto=True)
        self._export(project, {"preset": exports.DEFAULT, "override_reason": ""}, cancel)

    def _export(self, project: dict, arg: dict, cancel: threading.Event) -> None:
        pid = project["id"]
        folder = self.dir(pid)
        preset_id = arg["preset"]
        reason = arg.get("override_reason") or ""
        n = exports.next_number(folder)
        target = folder / exports.DIR / str(n)
        target.mkdir(parents=True, exist_ok=True)
        out = target / f"export.{exports.preset(preset_id)['container']}"
        total_s = media.duration(folder / "output.mp4", self.config.ffmpeg)
        self._stage(project, cancel, "export", 0, exports.preset(preset_id)["label"])
        tmp = target / f"export.tmp.{out.suffix[1:]}"
        argv = exports.argv(preset_id, self.config.ffmpeg, folder / "output.mp4", tmp)
        on_pct = lambda pct: self._stage(project, cancel, "export", pct * 0.9, "кодирование")  # noqa: E731
        lines = self._ffmpeg_run(argv, cancel, total_s, on_pct)
        for line in lines:
            self._note(project, line)
        tmp.replace(out)
        with self._lock:
            if not (project.get("approvals") or {}).get("export"):
                self._pass_gate(project, "export", auto=False, reason=reason)
        if reason:
            decisions.record(folder, "human", "export:override", rationale=reason, ids=[str(n)])
        data = exports.provenance(project, folder, preset_id, out, override=reason)
        atomic_write(target / "provenance.json", json.dumps(data, ensure_ascii=False, indent=1))
        exports.archive(folder, target / "project.zip")
        with self._lock:
            project["exports"] = n
            self._history(
                project, "export", f"#{n} {exports.preset(preset_id)['label']}" + (" (override)" if reason else "")
            )
        self._stage(project, cancel, "export", 100)

    def _assemble(self, project, words, cuts, layout, cancel, *, rebuild=False) -> tuple[edl.Edl, list[dict]]:
        """Сегменты (из кэша, если были) → склейка → субтитры → финал без вставок.

        Первая сборка идёт этапами segments/join/final; rebuild — пересборка после проверки, её ход
        пишется деталью этапа «Проверка».
        """

        def step(stage_id: str, percent: float, detail: str = "") -> None:
            if rebuild:
                self._stage(project, cancel, "check", 90, f"пересборка: {detail or stages.STAGES[stage_id][0]}")
            else:
                self._stage(project, cancel, stage_id, percent, detail)

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
        total_s = sum(seg.end - seg.start for seg in result.segments) or 1.0
        done_s = 0.0
        step("segments", 0, f"0 из {total}")
        for n, seg in enumerate(result.segments, 1):
            start, end = render.frame(seg.start), render.frame(seg.end)  # целые кадры: склейка без набегания
            spec = render.SegmentSpec(start, end, seg.layout, project["options"]["grade"], head, screen,
                                      project["screen_offset"])  # fmt: skip
            part = cache / f"{spec.key()}.mp4"
            length = seg.end - seg.start
            if not part.is_file():
                tmp = part.with_suffix(".tmp.mp4")
                lines = self._ffmpeg_run(
                    render.segment_argv(self.config.ffmpeg, spec, tmp, mask), cancel, length,
                    lambda pct, base=done_s, length=length, n=n: step(
                        "segments", 100 * (base + length * pct / 100) / total_s, f"кусок {n} из {total}"),
                )  # fmt: skip
                for line in lines:
                    self._note(project, line)
                tmp.replace(part)
            parts.append(part)
            done_s += length
            step("segments", 100 * done_s / total_s, f"{n} из {total}")
        used = {p.name for p in parts}
        for old in cache.glob("*.mp4"):  # кэш — только для текущего плана
            if old.name not in used:
                old.unlink(missing_ok=True)
        listing = folder / "segments.txt"
        listing.write_text(render.concat_list(parts), encoding="utf-8")
        step("join", 0)
        self._ffmpeg(["-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", str(folder / "joined.mp4")],
                     cancel, "склейка", total_s, lambda pct: step("join", pct))  # fmt: skip
        out_words = edl.output_words(words, result)
        items = render.chunks(out_words)
        atomic_write(folder / "subtitles.srt", render.srt(items))
        atomic_write(folder / "subtitles.vtt", render.vtt(items))
        self._final(project, out_words, [], cancel, stage=None if not rebuild else ("check", 90, 100, "пересборка"))
        return result, out_words

    def _final(self, project, out_words, overlays, cancel, *, stage: tuple | None = None) -> None:
        """Финальный проход; stage — (этап, от %, до %, деталь), по умолчанию — свой этап final."""
        folder = self.dir(project["id"])
        stage_id, lo, hi, detail = stage or ("final", 0, 100, "")
        ass = None
        if project["options"]["subtitles"]:
            if media.has_filter(self.config.ffmpeg, "subtitles"):
                ass = folder / "subtitles.ass"
                atomic_write(ass, render.ass(render.chunks(out_words), self.config.font))
            else:
                self._note(project, "Субтитры в кадр пропущены: в ffmpeg нет фильтра subtitles (нужен ffmpeg-full).")
        self._stage(project, cancel, stage_id, lo, detail)
        tmp = folder / "output.tmp.mp4"
        argv = render.final_argv(self.config.ffmpeg, folder / "joined.mp4", tmp, subtitles=ass, overlays=overlays)
        total_s = media.duration(folder / "joined.mp4", self.config.ffmpeg)

        def on_percent(pct: float) -> None:
            self._stage(project, cancel, stage_id, lo + (hi - lo) * pct / 100, detail)

        lines = self._ffmpeg_run(argv, cancel, total_s, on_percent)
        for line in lines:
            self._note(project, line)
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

    def _asr_check(self, project: dict, out_words: list[dict], cancel: threading.Event, n: int = 1) -> list[dict]:
        """Повторная расшифровка результата → LLM ищет пропущенное → вырезки в индексах исходника.

        Доли этапа «Проверка»: звук 0–10, паузы 10–15, расшифровка 15–80, ИИ 80–90, пересборка 90–100.
        """
        folder = self.dir(project["id"])
        wav = folder / "check.wav"
        out = folder / "output.mp4"
        length = media.duration(out, self.config.ffmpeg)
        say = f"круг {n}: "
        self._audio(out, wav, cancel, length,
                    lambda pct: self._stage(project, cancel, "check", pct / 10, f"{say}звук результата"))  # fmt: skip
        duration = media.duration(wav, self.config.ffmpeg)
        silences = self._silences(
            wav, duration, cancel, lambda pct: self._stage(project, cancel, "check", 10 + pct / 20, f"{say}паузы")
        )
        data = self._transcribe(project, wav, folder / "check.json", silences, cancel, "check", 15, 80,
                                f"{say}расшифровка результата, ")  # fmt: skip
        wav.unlink(missing_ok=True)
        heard = data["words"]
        self._stage(project, cancel, "check", 80, f"{say}ИИ ищет пропущенное")
        prompt = plan.check_prompt(plan.pack(heard), plan.base_rules(), self.lessons())
        issues = plan.parse_issues(self._llm(prompt, project, cancel, role="semantic_qa"), len(heard))
        # Индексы услышанного → время результата → слова результата → индексы исходника.
        mapped: list[dict] = []
        for issue in issues:
            a, b = heard[issue["from"]]["s"], heard[issue["to"]]["e"]
            src = [w["src"] for w in out_words if w["e"] > a and w["s"] < b]
            if src:
                mapped.append({**issue, "from": min(src), "to": max(src), "kind": "check"})
        return mapped
