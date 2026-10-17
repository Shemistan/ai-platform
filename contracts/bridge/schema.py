"""Схемы ответов `narrate bridge <cmd>` — общий контракт фейка платформы и настоящего моста (QA-004).

<cmd>.schema.json — подмножество JSON Schema: type (строка или список), required, properties,
items. Лишние ключи разрешены: новое поле в ответе контракт не ломает, пропавшее или
сменившее тип — ломает. Без зависимостей: проверку зовут тесты обеих сторон
(app/platform/tests/test_bridge_contract.py, app/narrator/tests/test_platform_contract.py).
"""

from __future__ import annotations

import json
from pathlib import Path

DIR = Path(__file__).resolve().parent
_TYPES = {
    "object": dict,
    "array": list,
    "string": str,
    "boolean": bool,
    "null": type(None),
}


def commands() -> list[str]:
    """Команды моста, для которых есть схема."""
    return sorted(p.name.removesuffix(".schema.json") for p in DIR.glob("*.schema.json"))


def load(command: str) -> dict:
    return json.loads((DIR / f"{command}.schema.json").read_text(encoding="utf-8"))


def _is(value: object, kind: str) -> bool:
    if kind == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    return isinstance(value, _TYPES[kind])


def errors(value: object, schema: dict, path: str = "$") -> list[str]:
    """Расхождения ответа со схемой: «$.articles[0].id: нет ключа» и т. п.; пусто — ответ годен."""
    kinds = schema.get("type")
    if kinds is not None:
        kinds = [kinds] if isinstance(kinds, str) else kinds
        if not any(_is(value, k) for k in kinds):
            return [f"{path}: ждали {'|'.join(kinds)}, пришло {type(value).__name__}"]
    out: list[str] = []
    if isinstance(value, dict):
        for key in schema.get("required", ()):
            if key not in value:
                out.append(f"{path}: нет ключа {key!r}")
        for key, sub in schema.get("properties", {}).items():
            if key in value:
                out += errors(value[key], sub, f"{path}.{key}")
    if isinstance(value, list) and "items" in schema:
        for n, item in enumerate(value):
            out += errors(item, schema["items"], f"{path}[{n}]")
    return out


def check(command: str, value: object) -> list[str]:
    return errors(value, load(command))
