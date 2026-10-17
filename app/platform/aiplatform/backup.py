"""Бэкап невосстановимых данных и восстановление: `make backup`, `make restore FILE=… TO=…`.

Что в архиве (только маленькие текстовые файлы и базы — без медиа и весов):

- `data/…` — каталог данных платформы (AIP_DATA_DIR): settings.json,
  channels.json, расписание, project.json / published.json / тексты
  выпусков, журнал запусков; без задач вкладок, кэша, загрузок, логов;
- `platform/local.mk`, `platform/channels.json` — настройки рядом с Makefile;
- `narrator-library/…` — библиотека narrator ($NARRATOR_LIBRARY или
  `<AIP_NARRATOR_DIR>/library`): статьи, база новостей, подписчики бота и
  курсор getUpdates (telegram.json); без mp3/wav/mp4. Токен бота не кладём:
  его можно перевыпустить в BotFather.

Архив `aiplatform-<дата>-<время>.tar.gz.age` шифруется `age` на открытый
ключ (AIP_BACKUP_AGE_RECIPIENT или файл AIP_BACKUP_AGE_RECIPIENTS_FILE) и
кладётся в AIP_BACKUP_DIR (iCloud Drive, второй диск). Архивы старше
AIP_BACKUP_KEEP_DAYS (30) там удаляются. Итог успешного бэкапа —
`<data>/backup.json`; его возраст показывает /api/ready (проверка backup).

Восстановление — только в пустой каталог: `age -d -i <ключ>` (AIP_BACKUP_AGE_IDENTITY,
по умолчанию ~/.config/aiplatform/backup-key.txt) и распаковка. Дальше —
`AIP_DATA_DIR=<TO>/data make run` (см. docs/runbook.md).
"""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import sys
import tarfile
import time
from collections.abc import Mapping
from pathlib import Path

from aiplatform import config as config_module
from aiplatform.audio import config as audio_config
from aiplatform.config import SERVICE_DIR, pick

STAMP = "backup.json"
PREFIX = "aiplatform-"
SUFFIX = ".tar.gz.age"
KEEP_DAYS = 30
DAY_S = 86400
STALE_H = 48  # старше — /api/ready: backup не ок
MAX_FILE_BYTES = 50 * 2**20  # база новостей ~5 МБ; больше — это не то, что мы бэкапим
TEXT = {".json", ".jsonl", ".md", ".txt", ".db", ".csv", ".yaml", ".yml"}
# Каталоги данных, которые восстановимы или временны: задачи вкладок, кэш, загрузки, pid-файлы, логи.
SKIP_DATA = {"photo", "video", "audio", "cache", "uploads", "run", "logs"}
SKIP_NAMES = {"bot_token.md", ".DS_Store"}
DEFAULT_IDENTITY = "~/.config/aiplatform/backup-key.txt"


class BackupError(RuntimeError):
    """Понятная ошибка для человека: что не так и что сделать."""


def narrator_library(env: Mapping[str, str], cfg: config_module.Config) -> Path:
    """Где библиотека narrator: как library_root() движка, без git — бэкап зовут из основного checkout."""
    explicit = env.get("NARRATOR_LIBRARY")
    if explicit:
        return Path(explicit).expanduser().resolve()
    return audio_config.narrator_dir(cfg) / "library"


def _wanted(path: Path) -> bool:
    if path.name in SKIP_NAMES or path.suffix.lower() not in TEXT or path.is_symlink():
        return False
    try:
        return path.stat().st_size <= MAX_FILE_BYTES
    except OSError:
        return False


def collect(cfg: config_module.Config) -> list[tuple[Path, str]]:
    """Что положить в архив: (файл на диске, имя в архиве)."""
    out: list[tuple[Path, str]] = []
    data = cfg.data_dir
    if data.is_dir():
        for path in sorted(data.rglob("*")):
            rel = path.relative_to(data)
            if rel.parts[0] in SKIP_DATA or not path.is_file() or not _wanted(path):
                continue
            out.append((path, f"data/{rel.as_posix()}"))
    for name in ("local.mk", "channels.json"):
        path = SERVICE_DIR / name
        if path.is_file():
            out.append((path, f"platform/{name}"))
    library = narrator_library(cfg.env, cfg)
    if library.is_dir() and library.resolve() != data.resolve():
        for path in sorted(library.rglob("*")):
            if path.is_file() and _wanted(path):
                out.append((path, f"narrator-library/{path.relative_to(library).as_posix()}"))
    return out


def _age(env: Mapping[str, str]) -> str:
    return pick(env, "AIP_BACKUP_AGE", default="age")


def _recipients(env: Mapping[str, str]) -> list[str]:
    if env.get("AIP_BACKUP_AGE_RECIPIENT"):
        return ["-r", env["AIP_BACKUP_AGE_RECIPIENT"]]
    if env.get("AIP_BACKUP_AGE_RECIPIENTS_FILE"):
        return ["-R", str(Path(env["AIP_BACKUP_AGE_RECIPIENTS_FILE"]).expanduser())]
    raise BackupError(
        "Не задан ключ шифрования: AIP_BACKUP_AGE_RECIPIENT=age1… (открытый ключ из "
        "`age-keygen -o ~/.config/aiplatform/backup-key.txt`) в local.mk."
    )


def _run_age(argv: list[str], data: bytes) -> bytes:
    try:
        done = subprocess.run(argv, input=data, capture_output=True, timeout=300, check=False)
    except FileNotFoundError as exc:
        raise BackupError(f"Не найден {argv[0]} — поставь: brew install age.") from exc
    if done.returncode != 0:
        raise BackupError(f"age: {done.stderr.decode('utf-8', 'replace').strip()[-300:]}")
    return done.stdout


def create(cfg: config_module.Config, *, now: float | None = None) -> dict:
    """Собрать, зашифровать, положить в AIP_BACKUP_DIR, удалить старые. Вернуть итог (он же в backup.json)."""
    env = cfg.env
    target = env.get("AIP_BACKUP_DIR")
    if not target:
        raise BackupError("Не задан AIP_BACKUP_DIR — куда класть архивы (iCloud Drive, второй диск), в local.mk.")
    recipients = _recipients(env)
    now = now or time.time()
    files = collect(cfg)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for path, name in files:
            tar.add(path, arcname=name, recursive=False)
    sealed = _run_age([_age(env), *recipients], buf.getvalue())
    folder = Path(target).expanduser()
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{PREFIX}{time.strftime('%Y%m%d-%H%M%S', time.localtime(now))}{SUFFIX}"
    tmp = folder / f".{name}.tmp"
    tmp.write_bytes(sealed)
    tmp.replace(folder / name)
    removed = rotate(folder, int(pick(env, "AIP_BACKUP_KEEP_DAYS", default=str(KEEP_DAYS))), now=now)
    result = {"at": now, "file": str(folder / name), "bytes": len(sealed), "files": len(files), "removed": removed}
    stamp = cfg.data_dir / STAMP
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.with_suffix(".tmp").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    stamp.with_suffix(".tmp").replace(stamp)
    return result


def rotate(folder: Path, keep_days: int, *, now: float | None = None) -> list[str]:
    """Удалить наши архивы старше keep_days (по времени изменения файла)."""
    cutoff = (now or time.time()) - keep_days * DAY_S
    removed = []
    for path in sorted(folder.glob(f"{PREFIX}*{SUFFIX}")):
        if path.stat().st_mtime < cutoff:
            path.unlink(missing_ok=True)
            removed.append(path.name)
    return removed


def restore(archive: Path, to: Path, env: Mapping[str, str]) -> list[str]:
    """Расшифровать и распаковать в пустой каталог. Вернуть список файлов."""
    if not archive.is_file():
        raise BackupError(f"Нет файла {archive}.")
    if to.exists() and any(to.iterdir()):
        raise BackupError(f"{to} не пустой — восстанавливаем только в пустой каталог.")
    identity = Path(pick(env, "AIP_BACKUP_AGE_IDENTITY", default=DEFAULT_IDENTITY)).expanduser()
    plain = _run_age([_age(env), "-d", "-i", str(identity)], archive.read_bytes())
    to.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(plain), mode="r:gz") as tar:
        names = tar.getnames()
        if hasattr(tarfile, "data_filter"):  # 3.11.4+: без абсолютных путей, ссылок наружу и спецфайлов
            tar.extractall(to, filter="data")
        else:
            bad = [n for n in names if n.startswith("/") or ".." in Path(n).parts]
            if bad:
                raise BackupError(f"В архиве подозрительные пути: {bad[:3]}.")
            tar.extractall(to)  # noqa: S202 — пути проверены выше
        return names


def status(data_dir: Path, env: Mapping[str, str], *, now: float | None = None) -> dict:
    """Для /api/ready: {name: backup, ok, detail} — возраст последнего успешного бэкапа."""
    try:
        stamp = json.loads((data_dir / STAMP).read_text(encoding="utf-8"))
        at = float(stamp["at"])
    except (OSError, ValueError, KeyError, TypeError):
        stamp, at = None, None
    if at is None:
        configured = bool(env.get("AIP_BACKUP_DIR"))
        detail = "бэкапа ещё не было — make backup" if configured else "бэкап не настроен (AIP_BACKUP_DIR)"
        return {"name": "backup", "ok": not configured, "detail": detail, "age_h": None}
    age_h = ((now or time.time()) - at) / 3600
    ok = age_h <= STALE_H
    detail = f"последний бэкап {age_h:.0f} ч назад: {Path(stamp.get('file') or '').name}"
    return {"name": "backup", "ok": ok, "detail": detail, "age_h": round(age_h, 1)}


def migrate(env: Mapping[str, str], *, legacy: Path = config_module.LEGACY_DATA_DIR) -> Path:
    """Перенос data/ из рабочей копии в ~/Library/Application Support/aiplatform (копия, старое не удаляется)."""
    target = config_module.user_data_dir(env)
    if not legacy.is_dir():
        raise BackupError(f"Нет старого каталога {legacy} — переносить нечего.")
    if target.exists() and any(target.iterdir()):
        raise BackupError(f"{target} уже не пустой — перенос уже был? Сравни каталоги руками.")
    shutil.copytree(legacy, target, symlinks=True, dirs_exist_ok=True)
    old_channels = SERVICE_DIR / "channels.json"
    if old_channels.is_file() and not (target / "channels.json").exists():
        shutil.copy2(old_channels, target / "channels.json")
    return target


def main(argv: list[str]) -> int:
    cfg = config_module.load()
    try:
        match argv:
            case ["create"]:
                result = create(cfg)
                print(f"бэкап: {result['file']} — {result['files']} файлов, {result['bytes'] // 1024} КБ")
                for name in result["removed"]:
                    print(f"  удалён старый: {name}")
            case ["restore", archive, to]:
                names = restore(Path(archive).expanduser(), Path(to).expanduser(), cfg.env)
                print(f"восстановлено {len(names)} файлов в {to}")
                print(f"запуск на них: AIP_DATA_DIR={Path(to).expanduser() / 'data'} make run")
            case ["migrate"]:
                target = migrate(cfg.env)
                print(f"данные скопированы в {target}; старый {config_module.LEGACY_DATA_DIR} не удалён.")
                print("перезапусти сервис (launchctl kickstart -k gui/$(id -u)/com.aiplatform.server), проверь")
                print("доску и каналы — потом старый каталог можно удалить.")
            case _:
                print("использование: python -m aiplatform.backup create | restore <архив> <пустой каталог> | migrate")
                return 2
    except BackupError as exc:
        print(f"⛔ {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
