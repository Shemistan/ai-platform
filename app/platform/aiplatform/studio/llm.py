"""LLM через CLI: промпт — на stdin, ответ — stdout. По умолчанию `claude -p`.

Команда задаётся AIP_LLM_CMD, так что подойдёт любой CLI с тем же
контрактом (например, обёртка над локальной моделью). Ответ с JSON
разбирает extract_json: берёт ```json-блок или первый объект/массив.
"""

from __future__ import annotations

import json
import re
import threading

from aiplatform.core import runner

INSTALL_HINT = "Нужен CLI модели: claude (Claude Code) или своя команда в AIP_LLM_CMD."
_FENCE = re.compile(r"```(?:json)?\s*\n(.*?)\n```", re.S)


class StudioError(RuntimeError):
    """Шаг сценария не удался; текст показывается пользователю."""


def complete(prompt: str, *, cmd: tuple[str, ...], cancel: threading.Event, on_line=None) -> str:
    """Ответ модели целиком. Падение CLI → StudioError с последними строками вывода."""
    lines: list[str] = []

    def collect(line: str) -> None:
        lines.append(line)
        if on_line:
            on_line(line)

    try:
        runner.run(list(cmd), collect, cancel, stdin=prompt.encode("utf-8"), missing_hint=INSTALL_HINT, kind="llm")
    except runner.RunFailed as exc:
        tail = " ".join(x.strip() for x in lines[-3:] if x.strip())
        raise StudioError(f"LLM: {exc}{' — ' + tail if tail else ''}") from exc
    answer = "\n".join(lines).strip()
    if not answer:
        raise StudioError("LLM вернула пустой ответ.")
    return answer


def extract_json(text: str) -> object:
    """JSON из ответа модели: ```json-блок, иначе от первой { или [ до парной последней."""
    candidates = [m.group(1) for m in _FENCE.finditer(text)]
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = text.find(opener), text.rfind(closer)
        if start != -1 and end > start:
            candidates.append(text[start : end + 1])
    for raw in candidates:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            continue
    raise StudioError("LLM ответила не JSON — перезапусти шаг или поправь результат руками.")


def plain(text: str) -> str:
    """Одна строка без обрамляющих кавычек и markdown — для промпта картинки."""
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip() and not ln.strip().startswith("```")]
    joined = " ".join(lines)
    return joined.strip().strip("\"'«»`").strip()
