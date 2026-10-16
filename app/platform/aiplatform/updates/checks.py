"""Проверка обновлений моделей и пайплайнов: что стоит у нас и что вышло в источнике.

Источники — те же, откуда ставят setup-model.sh photogen/videogen и narrator:
PyPI (mflux), релизы и теги GitHub (Flux2CLI, ltx-2-mlx), репозитории весов
на Hugging Face, git-репозиторий narrator. Установленная версия читается из
путей установки (`mflux-0.22.0`, `flux2cli-v3.1.0`, `ltx-2-mlx-v0.16.2`,
`wan-video-mlx-86bfc98`), у весов — время установки. Ничего не ставим сами:
показываем, что обновить и как.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path

from aiplatform.photo import models as photo_models
from aiplatform.video import models as video_models

PYPI, RELEASE, TAG, HF, GIT = "pypi", "release", "tag", "hf", "git"
UPDATE, OK, UNKNOWN, ERROR = "update", "ok", "unknown", "error"
TIMEOUT_S = 20
FRESH_DAYS = 1  # веса на HF новее установки меньше чем на день — это та же загрузка
PHOTOGEN_SETUP = "11_photo_generator/app/photogen/setup-model.sh"
VIDEOGEN_SETUP = "10_video_generator/app/videogen/setup-model.sh"

Fetch = Callable[[str], object]  # url → разобранный JSON; ошибка сети — исключение


@dataclass(frozen=True)
class Component:
    id: str
    group: str
    label: str
    kind: str
    source: str  # пакет PyPI, owner/repo на GitHub или HF, каталог git
    installed: str = ""  # версия или ревизия; пусто — не знаем
    installed_at: float | None = None  # у весов: когда поставлены (mtime отметки)
    hint: str = ""

    @property
    def url(self) -> str:
        return {
            PYPI: f"https://pypi.org/project/{self.source}/",
            RELEASE: f"https://github.com/{self.source}/releases",
            TAG: f"https://github.com/{self.source}/tags",
            HF: f"https://huggingface.co/{self.source}",
        }.get(self.kind, "")


@dataclass
class Result:
    component: Component
    status: str
    latest: str = ""
    message: str = ""
    extra: dict = field(default_factory=dict)

    def public(self) -> dict:
        c = self.component
        return {
            "id": c.id,
            "group": c.group,
            "label": c.label,
            "kind": c.kind,
            "installed": c.installed,
            "latest": self.latest,
            "status": self.status,
            "message": self.message,
            "hint": c.hint if self.status == UPDATE else "",
            "url": c.url,
        }


def version_key(value: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", value)[:4])


def _version_in(text: str, prefix: str) -> str:
    match = re.search(rf"{re.escape(prefix)}v?(\d+(?:\.\d+)+)", text, re.I)
    return match.group(1) if match else ""


def _mflux_version(mflux_bin: Path | None) -> str:
    if mflux_bin is None:
        return ""
    found = _version_in(str(mflux_bin), "mflux-")
    if found:
        return found
    for info in sorted(mflux_bin.parent.glob("lib/python*/site-packages/mflux-*.dist-info")):
        return _version_in(info.name, "mflux-")
    return ""


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def components(photo, video, audio, *, demo: dict[str, str] | None = None) -> list[Component]:
    """Что проверять: пайплайны и веса вкладок «Фото», «Видео» и движки «Аудио».

    photo, video, audio — их конфиги. demo — установленные версии для демо-режима
    (там вместо пайплайнов фейковые команды и версии из путей не прочитать).
    """
    demo = demo or {}
    flux2 = " ".join(photo.flux2_cmd)
    ltx, wan = " ".join(video.ltx_cmd), " ".join(video.wan_cmd)
    wan_rev = re.search(r"wan-video-mlx-([0-9a-f]{7,40})", wan)
    out = [
        Component(
            "mflux",
            "Фото",
            "mflux — пайплайн Qwen, Z-Image, Krea, Ideogram и увеличения SeedVR2",
            PYPI,
            "mflux",
            demo.get("mflux") or _mflux_version(photo.mflux_bin),
            hint=f"Подними MFLUX_VERSION в {PHOTOGEN_SETUP} и перезапусти его.",
        ),
        Component(
            "flux2cli",
            "Фото",
            "Flux2CLI — пайплайн FLUX.2",
            RELEASE,
            "VincentGourbin/flux-2-swift-mlx",
            demo.get("flux2cli") or _version_in(flux2, "flux2cli-"),
            hint=f"Подними FLUX2_VERSION в {PHOTOGEN_SETUP} и перезапусти его.",
        ),
    ]
    models_dir = photo.models_dir
    for model in photo_models.CATALOG:
        if model.backend != photo_models.MFLUX:
            continue  # веса FLUX.2 качает и обновляет сам Flux2CLI
        marker = photo_models.marker(model, models_dir) if models_dir else None
        out.append(
            Component(
                f"photo-{model.id}",
                "Фото",
                f"веса {model.label}",
                HF,
                f"mflux-community/{model.weights}",
                installed="установлены" if (demo or (marker and marker.is_file())) else "",
                installed_at=_demo_time(demo) if demo else (_mtime(marker) if marker else None),
                hint=f"Удали {marker.name if marker else 'отметку'} в каталоге весов и запусти "
                f"{PHOTOGEN_SETUP} {model.id}.",
            )
        )
    out.append(
        Component(
            "ltx-2-mlx",
            "Видео",
            "ltx-2-mlx — пайплайн LTX",
            TAG,
            "dgrauet/ltx-2-mlx",
            demo.get("ltx-2-mlx") or _version_in(ltx, "ltx-2-mlx-"),
            hint=f"Подними LTX_VERSION в {VIDEOGEN_SETUP} и перезапусти его.",
        )
    )
    for model in video_models.CATALOG:
        local = video.models_dir / model.repo.rsplit("/", 1)[1] if video.models_dir else None
        if model.pipeline == "wan":
            installed = demo.get("wan") or (wan_rev.group(1) if wan_rev else "")
            hint = f"Ревизия закреплена: подними WAN_REVISION в {VIDEOGEN_SETUP} и перезапусти его."
        else:
            installed = "установлены" if demo or (local and local.is_dir()) else ""
            hint = f"Удали каталог {local.name if local else 'весов'} и запусти {VIDEOGEN_SETUP} {model.id}."
        out.append(
            Component(
                f"video-{model.id}",
                "Видео",
                f"веса {model.label}",
                HF,
                model.repo,
                installed,
                installed_at=_demo_time(demo) if demo else (_mtime(local) if local else None),
                hint=hint,
            )
        )
    out.append(
        Component(
            "narrator",
            "Озвучка",
            "narrator — все движки озвучки (Higgs, ESpeech, Silero…) и сбор новостей",
            GIT,
            str(audio.narrator_dir),
            demo.get("narrator", ""),
            hint=f"git -C {audio.narrator_dir} pull, затем make setup в narrator, если менялись зависимости.",
        )
    )
    return out


def _demo_time(demo: dict[str, str]) -> float:
    return datetime.fromisoformat(demo.get("installed_at", "2026-01-01")).replace(tzinfo=UTC).timestamp()


def http_fetch(url: str) -> object:
    headers = {"User-Agent": "aiplatform-updates", "Accept": "application/json"}
    token = os.environ.get("HF_TOKEN")
    if token and url.startswith("https://huggingface.co/"):
        headers["Authorization"] = f"Bearer {token}"  # закрытые репозитории весов (LTX-2.5)
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _compare(c: Component, latest: str) -> Result:
    if not c.installed:
        return Result(c, UNKNOWN, latest, "Не нашёл установленную версию — сверь вручную.")
    if version_key(latest) > version_key(c.installed):
        return Result(c, UPDATE, latest, f"Вышла {latest}, у нас {c.installed}.")
    return Result(c, OK, latest, "Последняя версия.")


def _hf(c: Component, data: dict) -> Result:
    sha = str(data.get("sha") or "")
    modified = str(data.get("lastModified") or "")
    day = modified[:10]
    if re.fullmatch(r"[0-9a-f]{7,40}", c.installed or ""):  # закреплённая ревизия
        if sha and (sha.startswith(c.installed) or c.installed.startswith(sha[:7])):
            return Result(c, OK, sha[:7], "Ревизия совпадает с последней.")
        return Result(c, UPDATE, sha[:7], f"В репозитории новая ревизия {sha[:7]} от {day}, у нас {c.installed[:7]}.")
    if not c.installed:
        return Result(c, UNKNOWN, day, "Веса не установлены.")
    if c.installed_at is None or not modified:
        return Result(c, OK, day, f"Последнее изменение в репозитории — {day}.")
    remote = datetime.fromisoformat(modified.replace("Z", "+00:00")).timestamp()
    if remote > c.installed_at + FRESH_DAYS * 86400:
        since = datetime.fromtimestamp(c.installed_at, UTC).date().isoformat()
        return Result(c, UPDATE, day, f"Веса обновлены {day}, у нас поставлены {since}.")
    return Result(c, OK, day, "Новее установленных весов нет.")


Git = Callable[[list[str]], str]  # аргументы git → stdout


def _git(c: Component, git: Git | None) -> Result:
    if git is None:
        return Result(c, UNKNOWN, "", f"{c.source} — не git-репозиторий: обновляй вручную.")
    head = git(["rev-parse", "--short", "HEAD"]).strip()
    c = replace(c, installed=head)
    git(["fetch", "--quiet"])
    behind = git(["rev-list", "--count", "HEAD..@{upstream}"]).strip()
    if not behind.isdigit():
        return Result(c, UNKNOWN, "", "У ветки нет удалённой — сравнить не с чем.")
    if int(behind):
        latest = git(["rev-parse", "--short", "@{upstream}"]).strip()
        return Result(c, UPDATE, latest, f"Позади удалённой ветки на {behind} коммит(ов).")
    return Result(c, OK, head, "Совпадает с удалённой веткой.")


def run_git(folder: str) -> Git | None:
    """git в каталоге folder; None — это не git-репозиторий."""
    if not (Path(folder) / ".git").exists():
        return None

    def git(args: list[str]) -> str:
        done = subprocess.run(
            ["git", "-C", folder, *args], capture_output=True, text=True, timeout=TIMEOUT_S * 3, check=False
        )
        if done.returncode and args[0] != "rev-list":
            raise RuntimeError((done.stderr or done.stdout).strip() or f"git {args[0]} упал")
        return done.stdout

    return git


def check(c: Component, fetch: Fetch, git: Callable[[str], Git | None] = run_git) -> Result:
    """Одна проверка; любая ошибка сети или формата — статус error с текстом, а не исключение."""
    try:
        match c.kind:
            case "pypi":
                data = fetch(f"https://pypi.org/pypi/{c.source}/json")
                return _compare(c, str(data["info"]["version"]))
            case "release":
                data = fetch(f"https://api.github.com/repos/{c.source}/releases/latest")
                return _compare(c, str(data["tag_name"]).lstrip("v"))
            case "tag":
                data = fetch(f"https://api.github.com/repos/{c.source}/tags?per_page=50")
                tags = [str(t["name"]) for t in data if re.search(r"\d", str(t.get("name")))]
                if not tags:
                    return Result(c, UNKNOWN, "", "В репозитории нет тегов с версией.")
                return _compare(c, max(tags, key=version_key).lstrip("v"))
            case "hf":
                return _hf(c, fetch(f"https://huggingface.co/api/models/{c.source}"))
            case "git":
                return _git(c, git(c.source))
    except urllib.error.HTTPError as exc:
        reason = "нет доступа — нужен HF_TOKEN с принятой лицензией" if exc.code in (401, 403) else exc.reason
        return Result(c, ERROR, "", f"{c.url or c.source}: HTTP {exc.code}, {reason}.")
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError) as exc:
        return Result(c, ERROR, "", f"Не удалось проверить: {exc}")
    return Result(c, UNKNOWN, "", "Неизвестный источник.")
