"""Имитация LLM-CLI (`claude -p`) для демо и тестов: промпт со stdin, ответ в stdout.

Задачу узнаёт по первой строке промпта (`### task: poster|plan|short|summary|registry|montage_*`)
и отвечает детерминированно по тексту: промпт постера, план слайдов на
каждую фразу (картинки вперемешку с динамическими сценами), сценарий шортса из первых фраз, выжимку из
первых фраз абзацев, реестр организаций —
текущий список плюс одна демо-запись; монтаж — вырезки по простым правилам (паразиты, повторы,
команды в кадр «покажи экран», «вырежи это»), FAKE_LLM_MONTAGE_ISSUE=1 — проверка находит оговорку.

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
    if task == "summary":
        paras = [p.strip() for p in _body(prompt).split("\n\n") if p.strip() and not p.startswith("#")]
        firsts = [re.split(r"(?<=[.!?])\s+", p)[0] for p in paras[:8]] or ["Коротко о главном."]
        return "Выжимка. " + "\n\n".join(firsts) + "\n\nИтог: главное — в этих абзацах."
    if task == "registry":
        current = json.loads(prompt.rsplit("```json", 1)[1].split("```", 1)[0])
        entries = [e for e in current["entries"] if e.get("id") != DEMO_ENTRY["id"]] + [DEMO_ENTRY]
        data = {"entries": entries, "notes": "Демо: добавлена вымышленная запись, проверь её перед применением."}
        return "```json\n" + json.dumps(data, ensure_ascii=False, indent=2) + "\n```"
    if task.startswith("montage_"):
        return json.dumps(montage(task, prompt), ensure_ascii=False)
    return "Не знаю такой задачи."


# ---------- монтаж: план по простым правилам вместо модели ----------

_LINE = re.compile(r"^\[(\d+)\]\s+\d+(?:\.\d+)?\s+(.+)$", re.M)
FILLERS = {"э-э", "ээ", "эм", "ну"}


def _bare(word: str) -> str:
    return re.sub(r"[^\w-]", "", word.lower())


def _phrases(prompt: str) -> list[tuple[int, list[str]]]:
    body = prompt.split("Расшифровка:", 1)[-1]
    return [(int(first), text.split()) for first, text in _LINE.findall(body)]


_TIMED = re.compile(r"^\[\d+\]\s+(\d+(?:\.\d+)?)\s+(.+)$", re.M)


def motion(prompt: str) -> dict:
    """Вставки: подпись к первой фразе с латиницей и заголовок через 25+ с — в пределах ролика."""
    duration = float(re.search(r"Длительность ролика: (\d+)", prompt)[1])
    body = prompt.split("Расшифровка:", 1)[-1]
    inserts: list[dict] = []
    for at_raw, text in _TIMED.findall(body):
        at = float(at_raw)
        if at < 3 or at + 4 > duration - 5 or (inserts and at < inserts[-1]["at"] + 25):
            continue
        latin = " ".join(w for w in text.split() if re.search(r"[A-Za-z]", w)).strip(",.")
        if latin and not inserts:
            fields = {"title": latin, "subtitle": "демо-вставка"}
            inserts.append({"at": at, "duration": 4, "template": "lower_third", "fields": fields})
        elif inserts:
            inserts.append({"at": at, "duration": 4, "template": "title", "fields": {"title": _words(text, 4)}})
    return {"inserts": inserts}


def montage(task: str, prompt: str) -> dict:
    if task == "montage_motion":
        return motion(prompt)
    if task == "montage_check":
        issues = (
            [{"from": 0, "to": 0, "kind": "slip", "reason": "демо: оговорка"}]
            if os.environ.get("FAKE_LLM_MONTAGE_ISSUE")
            else []
        )
        return {"issues": issues}
    phrases = _phrases(prompt)
    screen = "Есть запись экрана" in prompt
    cuts, layout = [], []
    for n, (first, words) in enumerate(phrases):
        bare = [_bare(w) for w in words]
        if bare and bare[0] in FILLERS:
            cuts.append({"from": first, "to": first, "kind": "filler", "reason": "демо: слово-паразит"})
        for k in (1, 2, 3):  # «новый режим, новый режим» — первый повтор — дубль
            for i in range(len(bare) - 2 * k + 1):
                if bare[i : i + k] == bare[i + k : i + 2 * k]:
                    cuts.append(
                        {"from": first + i, "to": first + i + k - 1, "kind": "retake", "reason": "демо: повтор"}
                    )
        text = " ".join(bare)
        if text in ("покажи экран", "переключи на меня"):
            cuts.append({"from": first, "to": first + len(words) - 1, "kind": "command", "reason": "демо: команда"})
            if screen and n + 1 < len(phrases):
                layout.append({"from": phrases[n + 1][0], "layout": "split" if "экран" in text else "head"})
        if text == "вырежи это":
            cuts.append({"from": first, "to": first + len(words) - 1, "kind": "command", "reason": "демо: команда"})
            if n:
                prev_first, prev_words = phrases[n - 1]
                cuts.append({"from": prev_first, "to": prev_first + len(prev_words) - 1, "kind": "command",
                             "reason": "демо: просили вырезать"})  # fmt: skip
    out = {"cuts": cuts, "layout": [{"from": 0, "layout": "head"}, *layout] if screen else [], "notes": "демо-план"}
    if task == "montage_revise":
        feedback = prompt.split("Замечание человека к монтажу:", 1)[-1].strip().splitlines()[0]
        out["lesson"] = f"демо-правило: {_words(feedback, 8)}"
    return out


def main() -> int:
    prompt = sys.stdin.read()
    if os.environ.get("FAKE_LLM_FAIL"):
        print("Error: fake failure requested by FAKE_LLM_FAIL", file=sys.stderr)
        return 1
    print(answer(prompt))
    return 0


if __name__ == "__main__":
    sys.exit(main())
