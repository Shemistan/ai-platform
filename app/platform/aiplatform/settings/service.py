"""Вкладка «Настройки»: общие настройки платформы, API под /api/settings/.

Пока — что включено: модели фото и видео, движки озвучки и их голоса.
Хранятся в <data>/settings.json (core/settings.py); выключенное пропадает из
выбора во вкладках и сценариях сразу, без перезапуска.
"""

from __future__ import annotations

from aiplatform.audio import voices
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


class SettingsModule:
    prefix = PREFIX
    label = "Настройки"

    def __init__(self, ctx) -> None:
        self.ctx = ctx
        self.settings = ctx.config.settings

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
        return {"sections": sections, "file": str(self.settings.path)}

    # --- маршруты ---

    def handle(self, req: Request) -> None:
        match (req.method, req.sub):
            case ("GET", ["state"]):
                return req.send_json(self.state())
            case ("POST", ["toggle"]):
                return req.send_json(self.toggle(req.json()))
        raise not_found()

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
