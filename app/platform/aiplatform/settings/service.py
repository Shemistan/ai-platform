"""Вкладка «Настройки»: общие настройки платформы, API под /api/settings/.

Что включено (модели фото и видео, движки озвучки и их голоса), расписание
дайджеста, уведомления, отклик каналов и уборка. Хранятся в
<data>/settings.json (core/settings.py) и действуют сразу, без перезапуска.
"""

from __future__ import annotations

from aiplatform.audio import voices
from aiplatform.core import settings as settings_module
from aiplatform.core.cleanup import Cleaner
from aiplatform.core.http import Request, bad_request, not_found
from aiplatform.photo import models as photo_models
from aiplatform.video import models as video_models

PREFIX = "settings"
CATALOGS = {
    "photo": {m.id: m.label for m in photo_models.CATALOG},
    "video": {m.id: m.label for m in video_models.CATALOG},
    "audio": {e.id: e.label for e in voices.ENGINES.values()},
}
KIND_LABELS = {"photo": "модели фото", "video": "модели видео", "audio": "движки озвучки"}
# Разделы настроек вне «что выключено»: имя → путь к словарю в settings.json.
SECTIONS = {"schedule": ("schedule", "digest"), "notify": ("notify",), "stats": ("stats",), "cleanup": ("cleanup",)}


class SettingsModule:
    prefix = PREFIX
    label = "Настройки"

    def __init__(self, ctx) -> None:
        self.ctx = ctx
        self.settings = ctx.config.settings
        self.cleaner = Cleaner(ctx)

    def bind(self) -> None:
        """Уборка по флажку — ежедневной работой расписания сценариев."""
        studio = self.ctx.modules.get("studio")
        if studio is not None:
            studio.scheduler.daily["cleanup"] = self._daily_cleanup

    def _daily_cleanup(self) -> str | None:
        plan = self.settings.load()["cleanup"]
        if not plan["auto"]:
            return None
        found = self.cleaner.run(plan["days"], dry=False)
        return f"уборка: освобождено {found['bytes'] // 2**20} МБ"

    def cleanup(self, run: bool) -> dict:
        days = self.settings.load()["cleanup"]["days"]
        found = self.cleaner.run(days, dry=not run)
        return {"days": days, "done": run, "found": found, "usage": self.cleaner.usage()}

    def describe(self) -> dict:
        return {"file": str(self.settings.path)}

    def problems(self) -> list[str]:
        data = self.settings.load()["disabled"]
        off = {kind: set(catalog) <= set(data[kind]) for kind, catalog in CATALOGS.items()}
        off["audio"] = not voices.enabled(self.settings)  # движок без голосов тоже выключен
        off_kinds = [kind for kind, all_off in off.items() if all_off]
        return [f"Настройки: выключены все {KIND_LABELS[kind]} — во вкладках их не выбрать." for kind in off_kinds]

    # --- данные ---

    def _installed(self, kind: str) -> dict[str, bool]:
        """Что стоит на этой машине — по полному каталогу, без учёта выключенного."""
        module = self.ctx.modules.get(kind)
        if module is None or kind == "audio":
            return {}
        config = module.config
        if kind == "photo":
            found = photo_models.resolve(models_dir=config.models_dir, fake=config.fake)
        else:
            found = video_models.resolve(models_dir=config.models_dir, explicit=config.model, fake=config.fake)
        return {m.id: m.installed for m in found}

    def state(self) -> dict:
        data = self.settings.load()["disabled"]
        sections = {}
        for kind, catalog_models in (("photo", photo_models.CATALOG), ("video", video_models.CATALOG)):
            installed = self._installed(kind)
            sections[kind] = [
                {
                    "id": m.id,
                    "label": m.label,
                    "note": m.note,
                    "installed": installed.get(m.id),
                    "enabled": m.id not in data[kind],
                }
                for m in catalog_models
            ]
        off_voices = data["voices"]
        sections["audio"] = [
            {
                "id": e.id,
                "label": e.label,
                "note": e.note,
                "enabled": e.id not in data["audio"],
                "default_voice": e.default_voice,
                "voices": [{"id": v, "enabled": v not in off_voices.get(e.id, [])} for v in e.voices],
            }
            for e in voices.ENGINES.values()
        ]
        full = self.settings.load()
        studio = self.ctx.modules.get("studio")
        return {
            "sections": sections,
            "file": str(self.settings.path),
            "schedule": full["schedule"]["digest"],
            "notify": full["notify"],
            "stats": full["stats"],
            "cleanup": full["cleanup"] | {"choices": list(settings_module.CLEANUP_DAYS)},
            "last_runs": studio.scheduler.state() if studio else {},
            "notifications": self.ctx.notifier.recent(10),
        }

    # --- маршруты ---

    def handle(self, req: Request) -> None:
        match (req.method, req.sub):
            case ("GET", ["state"]):
                return req.send_json(self.state())
            case ("POST", ["toggle"]):
                return req.send_json(self.toggle(req.json()))
            case ("POST", ["section", name]) if name in SECTIONS:
                return req.send_json(self.update_section(name, req.json()))
            case ("GET", ["cleanup"]):
                return req.send_json(self.cleanup(run=False))
            case ("POST", ["cleanup", "run"]):
                return req.send_json(self.cleanup(run=True))
            case ("POST", ["notify-test"]):
                self.ctx.notifier.send("Проверка уведомлений", "Так будет выглядеть уведомление о выпуске.", show=True)
                return req.send_json(self.state())
        raise not_found()

    def update_section(self, name: str, payload: object) -> dict:
        """Часть раздела: {enabled, time, day} расписания, {enabled} уведомлений и т.п. Мусор — 400."""
        if not isinstance(payload, dict) or not payload:
            raise bad_request("Тело запроса — непустой объект JSON.")
        path = SECTIONS[name]

        def section(data: dict) -> dict:
            for part in path:
                data = data[part]
            return data

        current = section(self.settings.load())
        unknown = [k for k in payload if k not in current]
        if unknown:
            raise bad_request(f"Нет такой настройки: {', '.join(unknown)}.")
        checked = section(settings_module.normalize(self.settings.load() | _nest(path, current | payload)))
        wrong = [k for k in payload if checked[k] != payload[k]]
        if wrong:
            raise bad_request(f"Неверное значение: {', '.join(wrong)}.")
        self.settings.change(lambda data: section(data).update(payload))
        return self.state()

    def toggle(self, payload: object) -> dict:
        """{kind: photo|video|audio, id, enabled} или {kind: voice, engine, id, enabled}."""
        if not isinstance(payload, dict):
            raise bad_request("Тело запроса — не объект JSON.")
        kind, item, enabled = payload.get("kind"), payload.get("id"), payload.get("enabled")
        if not isinstance(enabled, bool):
            raise bad_request("enabled — true или false.")
        if kind == "voice":
            engine = voices.ENGINES.get(payload.get("engine")) if isinstance(payload.get("engine"), str) else None
            if engine is None:
                raise bad_request("Неизвестный движок озвучки.")
            if item not in engine.voices:
                raise bad_request(f"У движка {engine.label} нет голоса {item!r}.")

            def apply(data: dict) -> None:
                names = set(data["disabled"]["voices"].get(engine.id, []))
                names = names - {item} if enabled else names | {item}
                data["disabled"]["voices"][engine.id] = sorted(names)

        elif kind in CATALOGS:
            if item not in CATALOGS[kind]:
                raise bad_request(f"Нет такого в каталоге: {KIND_LABELS[kind]} — {item!r}.")

            def apply(data: dict) -> None:
                names = set(data["disabled"][kind])
                data["disabled"][kind] = sorted(names - {item} if enabled else names | {item})

        else:
            raise bad_request("kind — photo, video, audio или voice.")
        self.settings.change(apply)
        return self.state()

    def close(self) -> None:
        pass


def _nest(path: tuple[str, ...], value: dict) -> dict:
    """("schedule", "digest"), {…} → {"schedule": {"digest": {…}}}."""
    for part in reversed(path):
        value = {part: value}
    return value
