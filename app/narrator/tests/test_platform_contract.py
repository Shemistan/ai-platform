"""Контракт платформа ⇄ настоящий narrate (QA-004).

Платформа в тестах ходит в свой фейк (app/platform/aiplatform/fakes/fake_narrate.py), здесь —
проверка, что фейк и настоящий CLI не разошлись:
- команды и флаги фейка есть в настоящем парсере `narrate` (переименовали флаг — тест падает);
- команды, которые платформа зовёт в коде (`.argv("…")`, `.bridge("…")`), есть в парсере;
- argv, собранный функциями платформы (озвучка, движки), разбирается настоящим парсером;
- ответы настоящего моста на изолированной библиотеке годны по схемам contracts/bridge/
  (те же схемы проверяют ответы фейка в тестах платформы).
"""

import argparse
import importlib.util
import io
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from narrator import cli

ROOT = Path(__file__).resolve().parents[3]
PLATFORM = ROOT / "app" / "platform"
FAKE_ONLY = {"make"}  # у фейка — цели Makefile движка; настоящий make зовётся мимо CLI


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


schema = _load("bridge_schema", ROOT / "contracts" / "bridge" / "schema.py")
fake = _load("fake_narrate", PLATFORM / "aiplatform" / "fakes" / "fake_narrate.py")


def subparsers(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    return {}


def options(parser: argparse.ArgumentParser) -> set[str]:
    return {o for a in parser._actions for o in a.option_strings}


def positionals(parser: argparse.ArgumentParser) -> int:
    return sum(
        1
        for a in parser._actions
        if not a.option_strings and not isinstance(a, argparse._SubParsersAction)
    )


def walk(fake_p, real_p, path=()):
    """Расхождения: команда или флаг фейка, которых нет у настоящего narrate."""
    out = []
    missing = options(fake_p) - options(real_p)
    if missing:
        out.append(f"narrate {' '.join(path)}: нет флагов {sorted(missing)}")
    if path and positionals(fake_p) != positionals(real_p):
        where = " ".join(path)
        out.append(
            f"narrate {where}: позиционных {positionals(real_p)}, у фейка {positionals(fake_p)}"
        )
    real_subs = subparsers(real_p)
    for name, sub in subparsers(fake_p).items():
        if not path and name in FAKE_ONLY:
            continue
        if name not in real_subs:
            out.append(f"narrate {' '.join((*path, name))}: нет такой команды")
            continue
        out += walk(sub, real_subs[name], (*path, name))
    return out


def test_fake_cli_matches_real_parser():
    problems = walk(fake.parser(), cli.build_parser())
    assert not problems, "фейк платформы разошёлся с narrate:\n" + "\n".join(problems)


def test_commands_called_by_platform_exist():
    """Первые аргументы `.argv("…")` и `.bridge("…")` в коде платформы — команды narrate."""
    real = subparsers(cli.build_parser())
    bridge = subparsers(real["bridge"])
    called, bridged = set(), set()
    for path in (PLATFORM / "aiplatform").rglob("*.py"):
        if "fakes" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        called |= set(re.findall(r"narrator\.argv\(\"([\w-]+)\"", text))
        bridged |= set(re.findall(r"\.bridge\(\"([\w-]+)\"", text))
    assert called >= {"synth", "bot", "news", "voice"}, called  # поиск не сломался
    assert called <= set(real), f"нет команд narrate: {sorted(called - set(real))}"
    assert bridged <= set(bridge), f"нет команд моста: {sorted(bridged - set(bridge))}"


@pytest.fixture
def platform_modules(monkeypatch):
    monkeypatch.syspath_prepend(str(PLATFORM))
    from aiplatform.audio import jobs, voices
    from aiplatform.library.service import engine_args

    return jobs, voices, engine_args


def test_platform_argv_parses_with_real_parser(platform_modules):
    jobs, voices, engine_args = platform_modules
    parser = cli.build_parser()
    argvs = []
    for engine in voices.ENGINES.values():
        payload = {"engine": engine.id, "voice": engine.voices[0], "speed": 1.1, "steps": 16}
        args = engine_args(payload)
        argvs += [["synth", "abc", *args], ["render", *args]]
        spec = jobs.AudioSpec("Текст.", engine.id, engine.voices[0], 1.1, 16, verify=True)
        say = jobs.AudioJobs.argv(
            SimpleNamespace(_config=SimpleNamespace(narrate_cmd=())),
            spec,
            Path("in.txt"),
            Path("out.mp3"),
        )
        argvs.append(say)
        spec = jobs.AudioSpec("Текст.", engine.id, engine.voices[0], None, None, verify=False)
        argvs.append(
            jobs.AudioJobs.argv(
                SimpleNamespace(_config=SimpleNamespace(narrate_cmd=())), spec, Path("a"), Path("b")
            )
        )
    argvs += [
        ["bot", "send", "abc", "--force"],
        ["bot", "unsent", "--mark"],
        ["bot", "serve", "--updates"],
        ["news", "collect"],
        ["voice", "shem"],
        ["verify", "abc"],
    ]
    for argv in argvs:
        try:
            parser.parse_args(argv)
        except SystemExit as exc:  # argparse отказал: флаг или команда, которых нет у narrate
            raise AssertionError(f"narrate не принял argv платформы: {argv}") from exc


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / "library"
    root.mkdir()
    monkeypatch.setenv("NARRATOR_LIBRARY", str(root))
    monkeypatch.setenv("NARRATOR_MODELS_DIR", str(tmp_path / "models"))
    monkeypatch.setenv("NARRATOR_NEWS_DIR", str(tmp_path / "news"))
    monkeypatch.delenv("NARRATOR_TELEGRAM_TOKEN", raising=False)
    monkeypatch.setenv("NARRATOR_TELEGRAM_TOKEN_FILE", str(tmp_path / "нет-токена.md"))
    return root


def bridge(capsys, monkeypatch, *args, stdin=""):
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    code = cli.main(["bridge", *args])
    return code, json.loads(capsys.readouterr().out)


def test_real_bridge_answers_match_schemas(capsys, monkeypatch, home):
    new = json.dumps({"title": "Очереди", "text": "Брокер — не база.", "url": "https://x.dev/q"})
    code, created = bridge(capsys, monkeypatch, "article-new", stdin=new)
    assert code == 0, created
    article_id = created["id"]
    calls = {
        "version": ((), ""),
        "engines": ((), ""),
        "voices": ((), ""),
        "models": ((), ""),
        "preview": ((), "Go 1.26 вышел 8 октября."),
        "split": ((), "# А\n\nтекст\n\n# Б\n\nтекст\n"),
        "library": ((), ""),
        "article": ((article_id,), ""),
        "article-save": ((article_id,), json.dumps({"summary": "О брокерах."})),
        "sources": ((), ""),
        "failed": ((), ""),
        "top": ((), ""),
        "issue": ((), ""),
        "reviews": ((), ""),
        "bot": ((), ""),
        "subscribers": ((), ""),
        "unsent": ((), ""),
    }
    problems = schema.check("article-new", created)
    for name, (args, stdin) in calls.items():
        code, out = bridge(capsys, monkeypatch, name, *args, stdin=stdin)
        assert code == 0, (name, out)
        problems += [f"{name}: {p}" for p in schema.check(name, out)]
    assert not problems, "ответ моста разошёлся со схемой contracts/bridge:\n" + "\n".join(problems)
    # Схемы без живого прогона здесь: fetch (сеть), review (нужен готовый обзор) — их сверяет фейк.
    assert set(schema.commands()) == {*calls, "article-new", "fetch", "review"}


def test_renamed_flag_is_caught():
    """Флаг, переименованный в narrate без правки платформы, ловится сверкой парсеров."""
    fake_p, real_p = argparse.ArgumentParser(), argparse.ArgumentParser()
    fake_sub, real_sub = fake_p.add_subparsers(), real_p.add_subparsers()
    fake_sub.add_parser("bot").add_argument("--force")
    real_sub.add_parser("bot").add_argument("--forced")
    assert walk(fake_p, real_p) == ["narrate bot: нет флагов ['--force']"]
