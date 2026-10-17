"""Согласие владельца голоса на клонирование (CMP-009).

Живой голос — личные данные и готовый материал для дипфейка. Клонировать его
можно только с записью о согласии рядом с образцом: `<stem>.consent.json`
(`shem.wav` → `shem.consent.json`). Формат — `docs/voice-consent.md`:

    {"version": 1, "kind": "live", "owner": "чей голос",
     "date": "2026-10-17", "form": "self" | "written",
     "purposes": ["озвучка статей и дайджестов narrator"],
     "expires": "2027-10-17" или null}

Синтетические голоса (`SYNTHETIC`: M1 из Supertonic, Charon из Gemini TTS)
ничьи — им запись не нужна, они помечены `kind: synthetic` в коде.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

VERSION = 1
SUFFIX = ".consent.json"
SYNTHETIC = frozenset({"m1", "charon"})  # образцы не живых людей: согласие не требуется
FORMS = ("self", "written")  # свой голос / письменное согласие владельца
PURPOSE = "озвучка статей и дайджестов narrator"
DOC = "app/narrator/docs/voice-consent.md"


class ConsentError(ValueError):
    """Нет записи о согласии, она битая или истекла."""


def path_for(wav: Path) -> Path:
    return wav.with_name(wav.stem + SUFFIX)


def make(owner: str, form: str, today: date | None = None, expires: str | None = None) -> dict:
    """Запись о согласии на живой голос; ConsentError — если поля не годятся."""
    owner = " ".join((owner or "").split())
    if not owner:
        raise ConsentError("согласие: укажи, чей это голос")
    if form not in FORMS:
        raise ConsentError(f"согласие: форма {form!r}, ждём {' или '.join(FORMS)}")
    if expires is not None:
        _day(expires, "expires")
    return {
        "version": VERSION,
        "kind": "live",
        "owner": owner,
        "date": (today or date.today()).isoformat(),
        "form": form,
        "purposes": [PURPOSE],
        "expires": expires,
    }


def write(wav: Path, record: dict) -> Path:
    path = path_for(wav)
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def check(stem: str, wav: Path, today: date | None = None) -> dict:
    """Согласие на клон образца wav. Синтетический голос — без файла; иначе ConsentError."""
    if stem in SYNTHETIC:
        return {"version": VERSION, "kind": "synthetic"}
    path = path_for(wav)
    if not path.exists():
        raise ConsentError(
            f"голос {stem} — живой, клонировать без согласия владельца нельзя: нет {path} "
            f"(./narrate voice-consent {stem} --owner «чей голос» --form self|written; "
            f"формат — {DOC})"
        )
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ConsentError(f"{path}: не читается ({e})") from e
    if not isinstance(record, dict) or record.get("kind") not in ("live", "synthetic"):
        raise ConsentError(f"{path}: нет поля kind (live или synthetic) — формат в {DOC}")
    if record["kind"] == "live":
        if not str(record.get("owner") or "").strip() or record.get("form") not in FORMS:
            raise ConsentError(f"{path}: нужны owner и form ({' или '.join(FORMS)})")
        _day(record.get("date"), "date", path)
        if record.get("expires"):
            until = _day(record["expires"], "expires", path)
            if until < (today or date.today()):
                raise ConsentError(f"{path}: согласие истекло {until.isoformat()}")
    return record


def _day(value, name: str, path: Path | None = None) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError as e:
        where = f"{path}: " if path else "согласие: "
        raise ConsentError(f"{where}{name} — дата ГГГГ-ММ-ДД, а не {value!r}") from e
