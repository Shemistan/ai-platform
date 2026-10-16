"""Имитация LLM-CLI (`claude -p`) для демо и тестов: промпт со stdin, ответ в stdout.

Задачу узнаёт по первой строке промпта (`### task: poster|plan|short|registry`)
и отвечает детерминированно по тексту: промпт постера, план слайдов на
каждую фразу (картинки вперемешку с динамическими сценами), сценарий шортса из первых фраз, реестр организаций —
текущий список плюс одна демо-запись.

Окружение: FAKE_LLM_FAIL=1 — упасть; FAKE_LLM_GARBAGE=1 — ответить не JSON.
"""

from __future__ import annotations

import json
import os
import re
import sys

_PARA = re.compile(r"^\[(\d+)\]\s*(.+)$", re.M)
_SENT = re.compile(r"\((\d+)\)\s*(.+?)(?=\s*\(\d+\)\s|$)")
KINDS = ("image", "bullets", "image", "stat", "image", "chart", "image", "flow", "image", "compare")
DEMO_ENTRY = {
    "id": "demo-corp",
    "name": "Демо-корпорация",
    "status": "undesirable",
    "aliases": ["Демо-корпораци*", "Demo Corp"],
    "disclaimer": "Демо-корпорация — вымышленная запись демо-режима",
    "basis": "демо",
    "source": "",
}


def _words(text: str, n: int) -> str:
    words = text.split()
    return " ".join(words[:n]) + ("…" if len(words) > n else "")


def _slide(n: int, para: int, sent: int, text: str) -> dict:
    """Слайд демо-плана на фразу: виды по кругу — картинка, затем динамическая сцена, и так далее."""
    words = text.split()
    kind = KINDS[n % len(KINDS)]
    out = {"para": para, "sent": sent, "kind": kind, "title": _words(text, 6)}
    if kind == "image":
        out["bullets"] = [_words(" ".join(words[6:]), 8)] if len(words) > 6 else []
        out["image_prompt"] = f"editorial illustration, scene {para}.{sent}: {_words(text, 10)}"
        # Как просит промпт: ролики — только в ключевых моментах (здесь — у картинок со второго абзаца).
        out["video_prompt"] = f"slow dolly-in, scene {para}.{sent}: {_words(text, 10)}" if para >= 1 else ""
    elif kind == "bullets":
        out["bullets"] = [_words(" ".join(words[i : i + 4]), 4) for i in range(0, min(len(words), 12), 4)]
    elif kind == "stat":
        out |= {"value": len(text) % 90 + 10, "suffix": "%", "label": _words(text, 8)}
    elif kind == "chart":
        lens = [len(w) for w in words[:5]] or [1, 2]
        out |= {
            "chart": "line" if n % 2 else "bar",
            "unit": "",
            "data": [{"label": f"{2020 + i}", "value": v} for i, v in enumerate(lens)],
        }
    elif kind == "flow":
        out["steps"] = [_words(w, 1) for w in words[:3]] or ["Шаг"]
    elif kind == "compare":
        out |= {
            "left": {"title": "Было", "items": [_words(text, 3)]},
            "right": {"title": "Стало", "items": [_words(" ".join(words[3:]) or text, 3)]},
        }
    return out


def _body(prompt: str) -> str:
    marker = "Текст ролика:" if "Текст ролика:" in prompt else "Текст:"
    return prompt.split(marker, 1)[-1].strip()


def answer(prompt: str) -> str:
    task = prompt.splitlines()[0].removeprefix("### task: ").strip() if prompt else ""
    if task == "poster":
        topic = _words(_body(prompt), 12)
        return f"cinematic wide shot illustrating: {topic}, dramatic light, vivid colors, no text"
    if os.environ.get("FAKE_LLM_GARBAGE"):
        return "Извини, не могу ответить в JSON."
    if task == "plan":
        paras = [(num, _SENT.findall(text) or [("0", text)]) for num, text in _PARA.findall(prompt)]
        slides = []
        for num, sentences in paras:
            for sent, text in sentences:
                slides.append(_slide(len(slides), int(num), int(sent), text.strip()))
        first = paras[0][1][0][1] if paras else ""
        data = {
            "title": f"Видео: {_words(first, 6) if paras else 'без названия'}",
            "post": "**Главное** за день.",
            "slides": slides,
        }
        return "Вот план:\n```json\n" + json.dumps(data, ensure_ascii=False, indent=2) + "\n```"
    if task == "short":
        sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", _body(prompt)) if s.strip()]
        beats = [{"title": _words(s, 4), "text": _words(s, 20)} for s in sentences[:3]] or [
            {"title": "Итог", "text": "Коротко о главном."}
        ]
        beats.append({"title": "Подписывайся", "text": "Подписывайся, чтобы не пропустить следующий выпуск."})
        return json.dumps({"beats": beats}, ensure_ascii=False)
    if task == "registry":
        current = json.loads(prompt.rsplit("```json", 1)[1].split("```", 1)[0])
        entries = [e for e in current["entries"] if e.get("id") != DEMO_ENTRY["id"]] + [DEMO_ENTRY]
        data = {"entries": entries, "notes": "Демо: добавлена вымышленная запись, проверь её перед применением."}
        return "```json\n" + json.dumps(data, ensure_ascii=False, indent=2) + "\n```"
    return "Не знаю такой задачи."


def main() -> int:
    prompt = sys.stdin.read()
    if os.environ.get("FAKE_LLM_FAIL"):
        print("Error: fake failure requested by FAKE_LLM_FAIL", file=sys.stderr)
        return 1
    print(answer(prompt))
    return 0


if __name__ == "__main__":
    sys.exit(main())
