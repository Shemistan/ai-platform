"""Схемы данных «Монтажа» (MASTER_SPEC §7): JSON Schema в contracts/montage/, проверка без зависимостей.

Подмножество JSON Schema: type (строка или список), required, properties, additionalProperties
(false), items, minItems/maxItems, enum, const, minimum/maximum, minLength/maxLength, pattern.
Лишние ключи разрешены, если схема не говорит `additionalProperties: false`: новое поле контракт не
ломает, пропавшее или сменившее тип — ломает. Пишущий код зовёт `check` перед записью файла;
тесты проверяют примеры из схем и ответы фейков.
"""

from __future__ import annotations

import json
import re
from functools import cache
from pathlib import Path

DIR = Path(__file__).resolve().parents[4] / "contracts" / "montage"  # корень репозитория
SCHEMA_VERSION = "1.0"  # поле schema_version внутри файлов конвейера
_TYPES = {"object": dict, "array": list, "string": str, "boolean": bool, "null": type(None)}


class SchemaError(ValueError):
    """Данные не по схеме; текст — первые расхождения."""


def names() -> list[str]:
    return sorted(p.name.removesuffix(".schema.json") for p in DIR.glob("*.schema.json"))


@cache
def load(name: str) -> dict:
    path = DIR / f"{name}.schema.json"
    if not path.is_file():
        raise KeyError(f"нет схемы {name}")
    return json.loads(path.read_text(encoding="utf-8"))


def _is(value: object, kind: str) -> bool:
    if kind == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    return isinstance(value, _TYPES[kind])


def errors(value: object, schema: dict, path: str = "$") -> list[str]:
    """Расхождения значения со схемой: «$.scenes[0].id: нет ключа» и т. п.; пусто — годно."""
    kinds = schema.get("type")
    if kinds is not None:
        kinds = [kinds] if isinstance(kinds, str) else kinds
        if not any(_is(value, k) for k in kinds):
            return [f"{path}: ждали {'|'.join(kinds)}, пришло {type(value).__name__}"]
    out: list[str] = []
    if "enum" in schema and value not in schema["enum"]:
        out.append(f"{path}: {value!r} не из {schema['enum']}")
    if "const" in schema and value != schema["const"]:
        out.append(f"{path}: ждали {schema['const']!r}")
    if isinstance(value, int | float) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            out.append(f"{path}: {value} меньше {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            out.append(f"{path}: {value} больше {schema['maximum']}")
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            out.append(f"{path}: короче {schema['minLength']}")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            out.append(f"{path}: длиннее {schema['maxLength']}")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            out.append(f"{path}: {value!r} не по шаблону {schema['pattern']}")
    if isinstance(value, dict):
        for key in schema.get("required", ()):
            if key not in value:
                out.append(f"{path}: нет ключа {key!r}")
        props = schema.get("properties", {})
        for key, sub in props.items():
            if key in value:
                out += errors(value[key], sub, f"{path}.{key}")
        if schema.get("additionalProperties") is False:
            out += [f"{path}: лишний ключ {k!r}" for k in value if k not in props]
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            out.append(f"{path}: меньше {schema['minItems']} элементов")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            out.append(f"{path}: больше {schema['maxItems']} элементов")
        if "items" in schema:
            for n, item in enumerate(value):
                out += errors(item, schema["items"], f"{path}[{n}]")
    return out


def validate(name: str, value: object) -> list[str]:
    return errors(value, load(name))


def validate_many(name: str, values: list) -> list[str]:
    out: list[str] = []
    for n, item in enumerate(values):
        out += [e.replace("$", f"$[{n}]", 1) for e in validate(name, item)]
    return out


def check(name: str, value: object, *, many: bool = False) -> None:
    """Бросить SchemaError с первыми расхождениями; годные данные — молча."""
    found = validate_many(name, value) if many else validate(name, value)  # type: ignore[arg-type]
    if found:
        raise SchemaError(f"{name}: " + "; ".join(found[:5]) + (" …" if len(found) > 5 else ""))


def example(name: str) -> dict:
    return json.loads(json.dumps(load(name)["examples"][0]))
