"""Снапшоты `--help` настоящих CLI генерации: `make contracts` (QA-005).

Тест tests/test_cli_contracts.py собирает argv всех режимов теми же build_argv, что в
бою, и сверяет каждый флаг со снапшотом: переименованный в пайплайне флаг роняет тест,
а не первую генерацию. Снапшоты лежат в tests/contracts/<имя>-help.txt и пересъёмываются
этой командой после обновления пайплайна (README «Установка моделей»).

Команды берутся из тех же настроек, что у сервиса (local.mk → AIP_PHOTO_*, AIP_VIDEO_*).
Python-пайплайны импортируют mlx при старте и без GPU падают раньше справки — тогда
справка снимается через tools/stub_mlx_help.py (mlx подменён, парсер настоящий).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

from aiplatform import config as platform_config
from aiplatform.photo import config as photo_config
from aiplatform.photo import models as photo_models
from aiplatform.photo import upscale
from aiplatform.video import config as video_config

SERVICE_DIR = Path(__file__).resolve().parent.parent
SNAPSHOTS = SERVICE_DIR / "tests" / "contracts"
STUB = SERVICE_DIR / "tools" / "stub_mlx_help.py"
FLUX2_SUBCOMMANDS = ("t2i", "i2i", "inpaint", "outpaint")
_FLAG = re.compile(r"(?<![\w-])--[A-Za-z0-9][\w-]*")


def flags(text: str) -> set[str]:
    """Длинные флаги из справки: --model, --no-audio, --text-quant…"""
    return set(_FLAG.findall(text))


def argv_flags(argv: list[str]) -> list[str]:
    """Флаги, которые argv передаёт CLI: «--x» и «--x=значение»; всё после «--» — позиционное."""
    out = []
    for arg in argv:
        if arg == "--":
            break
        if arg.startswith("--"):
            out.append(arg.split("=", 1)[0])
    return out


def snapshot_path(name: str) -> Path:
    return SNAPSHOTS / f"{name}-help.txt"


def targets(env: dict[str, str]) -> dict[str, list[str]]:
    """Имя снапшота → команда справки (без --help)."""
    cfg = platform_config.load(env)
    photo = photo_config.load(cfg)
    video = video_config.load(cfg)
    out: dict[str, list[str]] = {}
    commands = [m.command for m in photo_models.CATALOG if m.backend == photo_models.MFLUX]
    for command in [*dict.fromkeys(commands), upscale.COMMAND]:
        out[command] = list(photo.mflux_cmd(command))
    for sub in FLUX2_SUBCOMMANDS:
        out[f"flux2-{sub}"] = [*photo.flux2(), sub]
    out["ltx-2-mlx-generate"] = [*video.ltx_cmd, "generate"]
    out["pocketai-wan-video"] = list(video.wan_cmd)
    return out


def _python_script(cmd: list[str]) -> tuple[Path, Path, list[str]] | None:
    """(python окружения, скрипт, хвост argv) для консольного Python-скрипта или None."""
    if cmd[:2] == ["uv", "run"] and "--project" in cmd:
        project = Path(cmd[cmd.index("--project") + 1]).expanduser()
        rest = [a for a in cmd[cmd.index("--project") + 2 :] if a]
        bin_dir, name, tail = project / ".venv" / "bin", rest[0], rest[1:]
    else:
        script = Path(cmd[0]).expanduser()
        if script.parent.name != "bin":
            return None
        bin_dir, name, tail = script.parent, script.name, cmd[1:]
    python, script = bin_dir / "python", bin_dir / name
    return (python, script, tail) if python.exists() and script.is_file() else None


def _own_mlx_packages(script: Path) -> str:
    """Пакеты самого пайплайна с префиксом mlx (from mlx_video… у Wan) — их stub не трогает."""
    try:
        text = script.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return ",".join(sorted(set(re.findall(r"^from (mlx\w*)\.", text, re.M)) - {"mlx"}))


def capture(cmd: list[str]) -> tuple[str, str]:
    """(справка, как снята); без GPU — через stub. Нет справки — RuntimeError с хвостом вывода."""
    run = subprocess.run([*cmd, "--help"], capture_output=True, text=True, timeout=300)
    text = run.stdout + run.stderr
    if run.returncode == 0 and flags(text):
        return run.stdout or run.stderr, "--help"
    found = _python_script(cmd)
    if found:
        python, script, tail = found
        env = os.environ | {"STUB_KEEP": _own_mlx_packages(script)}
        stub = [str(python), str(STUB), str(script), *tail, "--help"]
        run = subprocess.run(stub, capture_output=True, text=True, timeout=300, env=env)
        if run.returncode == 0 and flags(run.stdout):
            return run.stdout, "--help через tools/stub_mlx_help.py: mlx без GPU не импортируется"
        text = run.stdout + run.stderr
    raise RuntimeError(f"{' '.join(cmd)} --help: {text.strip()[-400:]}")


def main(argv: list[str] | None = None) -> int:
    only = set(argv if argv is not None else sys.argv[1:])
    SNAPSHOTS.mkdir(parents=True, exist_ok=True)
    failed = []
    for name, cmd in targets(dict(os.environ)).items():
        if only and name not in only:
            continue
        try:
            text, via = capture(cmd)
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            failed.append(name)
            print(f"⛔ {name}: {exc}", file=sys.stderr)
            continue
        # Путь команды — машины автора, в снапшот не пишем: только имя и способ съёмки.
        head = f"# {name}: {via}; снято {date.today().isoformat()} (make contracts)\n"
        snapshot_path(name).write_text(head + text, encoding="utf-8")
        print(f"{name}: {len(flags(text))} флагов → {snapshot_path(name).relative_to(SERVICE_DIR)}")
    if failed:
        print("не сняты: " + ", ".join(failed) + " — установлен ли пайплайн (make models)?", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
