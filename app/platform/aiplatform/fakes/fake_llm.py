"""Имитация LLM-CLI (`claude -p`) для демо и тестов: промпт со stdin, ответ в stdout.

Задачу узнаёт по первой строке промпта (`### task: poster|plan|short|summary|registry|montage_*`)
и отвечает детерминированно по тексту: промпт постера, план слайдов на
каждую фразу (картинки вперемешку с динамическими сценами), сценарий шортса из первых фраз, выжимку из
первых фраз абзацев, реестр организаций —
текущий список плюс одна демо-запись; монтаж — вырезки по простым правилам (паразиты, повторы,
команды в кадр «покажи экран», «вырежи это»), FAKE_LLM_MONTAGE_ISSUE=1 — проверка находит оговорку;
знания, биты, стиль и раскадровка «Монтажа» по MASTER_SPEC — `story()`.

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
    if task.startswith("motion_"):
        return json.dumps(motion_team(task, prompt), ensure_ascii=False)
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


STORY_TEMPLATES = (
    "chapter_title", "step_list", "data_chart", "flow_diagram", "code_highlight", "comparison", "concept_cards",
    "timeline", "task_scheduler", "kinetic_typography", "callout_zoom", "lower_third", "stat", "quote",
)  # fmt: skip
_BEAT_LINE = re.compile(r"^- (beat_\d+) \[(\d+)–(\d+)\] \w+ \([^)]*\): [^«]*«([^»]*)»((?: claim_\d+)*)$", re.M)


def _story_props(tid: str, text: str, n: int) -> dict:
    words = text.split()
    title = _words(text, 3)
    if tid == "chapter_title":
        return {"title": title, "kicker": f"Часть {n + 1}"}
    if tid == "step_list":
        return {"title": title, "steps": [_words(" ".join(words[i : i + 3]), 3) for i in range(0, 9, 3)]}
    if tid == "data_chart":
        series = [{"label": _words(w, 1)[:12], "value": len(w)} for w in words[:4]]
        return {"title": title, "unit": "", "kind": "bar", "series": series}
    if tid == "flow_diagram":
        return {"title": title, "nodes": [w[:18] for w in words[:4]], "direction": "right"}
    if tid == "code_highlight":
        return {
            "language": "go",
            "code": "ch := make(chan int, 2)\nch <- 1\nch <- 2",
            "highlight": [1],
            "caption": title,
        }
    if tid == "comparison":
        right = [_words(" ".join(words[3:]) or text, 3)]
        return {"left_title": "Было", "right_title": "Стало", "left": [_words(text, 3)], "right": right}
    if tid == "concept_cards":
        return {"title": title, "cards": [{"title": _words(w, 1)[:24], "text": _words(text, 4)} for w in words[:3]]}
    if tid == "timeline":
        return {
            "title": title,
            "events": [{"label": f"{2020 + i}", "text": _words(w, 1)} for i, w in enumerate(words[:3])],
        }
    if tid == "task_scheduler":
        return {"title": title, "workers": ["P0", "P1"], "tasks": [w[:12] for w in words[:4]]}
    if tid == "kinetic_typography":
        return {"text": _words(text, 8), "accent": words[:1]}
    if tid == "callout_zoom":
        return {"text": title, "x_pct": 70, "y_pct": 40, "radius_pct": 12}
    if tid == "stat":
        return {"value": str(len(words)), "label": title}
    if tid == "quote":
        return {"text": _words(text, 10), "author": "автор"}
    return {"title": title, "subtitle": "демо-подпись"}


def story(task: str, prompt: str) -> dict:
    """Знания, биты, стиль, раскадровка по простым правилам (MASTER_SPEC через фейк).

    Знания — утверждение на каждую третью фразу (цитата из первого фрагмента материалов, если есть;
    фрагмент со словом «противоречие» → contradicted). Биты — по две фразы, виды по кругу. Стиль —
    DEMO_DESIGN. Раскадровка — на каждый бит свой шаблон по кругу, score 20; FAKE_LLM_STORY_RISK=1 —
    первая сцена с factual_risk 5 без утверждений (её отбросит чистка)."""
    phrases = _phrases(prompt)
    if task == "montage_knowledge":
        chunks = re.findall(r"^\[(doc_\d+)#(\d+)\](?: \([^)]*\))? (.+)$", prompt.split("Материалы:", 1)[-1], re.M)
        claims = []
        for n, (first, words) in enumerate(phrases):
            if n % 3:
                continue
            citations, status = [], "none"
            if chunks:
                doc, k, text = chunks[0]
                citations = [{"doc": doc, "chunk": int(k), "quote": _words(text, 8)}]
                status = "contradicted" if "противоречие" in text.lower() else "none"
            claims.append({"statement": " ".join(words), "transcript": [first, first + len(words) - 1],
                           "citations": citations, "confidence": 0.8, "contradiction_status": status})  # fmt: skip
        return {"claims": claims}
    if task == "montage_beats":
        kinds = ("hook", "setup", "explanation", "demo", "explanation", "recap", "cta")
        claims = re.findall(r"^- (claim_\d+)", prompt, re.M)
        beats = []
        for n in range(0, len(phrases), 2):
            group = phrases[n : n + 2]
            first = group[0][0]
            last = group[-1][0] + len(group[-1][1]) - 1
            k = n // 2
            beats.append({
                "from": first, "to": last, "purpose": kinds[k % len(kinds)],
                "goal": f"демо: {_words(' '.join(group[0][1]), 5)}", "difficulty": k % 5 + 1,
                "keywords": [_bare(group[0][1][0])] if group[0][1] else [], "visuals_needed": "схема",
                "claims": claims[k : k + 1], "priority": 3 + k % 3, "confidence": 0.7,
            })  # fmt: skip
        return {"beats": beats}
    if task == "montage_style":
        motion_spec = {"enter": "power3.out", "exit": "power2.in", "enter_s": 0.4, "exit_s": 0.3}
        return {**DEMO_DESIGN, "motion": motion_spec, "captions": {"size": 60, "margin": 100}, "density": "medium"}
    scenes = []
    for n, (bid, _a, _b, text, claims) in enumerate(_BEAT_LINE.findall(prompt)):
        tid = STORY_TEMPLATES[n % len(STORY_TEMPLATES)]
        risk = 5 if (n == 0 and os.environ.get("FAKE_LLM_STORY_RISK")) else 2
        necessity = {"clarity": 4, "novelty": 4, "cognitive_load": 2, "production_cost": 2, "factual_risk": risk}
        scenes.append({
            "beat_id": bid, "template_id": tid, "props": _story_props(tid, text, n), "intent": f"демо: {_words(text, 3)}",  # noqa: E501
            "claim_ids": [] if risk == 5 else claims.split(), "necessity": necessity, "rationale": "демо-раскадровка",
            "start_word": None, "duration_s": 4 + n % 3,
        })  # fmt: skip
    return {"scenes": scenes}


def montage(task: str, prompt: str) -> dict:
    if task == "montage_motion":
        return motion(prompt)
    if task in ("montage_knowledge", "montage_beats", "montage_style", "montage_storyboard"):
        return story(task, prompt)
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


# ---------- моушн: команда агентов по простым правилам ----------

DEMO_DESIGN = {
    "name": "Демо-стиль",
    "palette": {
        "bg": "#101522",
        "surface": "#1b2335",
        "ink": "#f1f4fa",
        "muted": "#8d98ad",
        "accent": "#56c8f5",
        "accent2": "#ffb454",
        "ok": "#5fd38d",
        "warn": "#ffd166",
        "bad": "#ff6b6b",
    },  # fmt: skip
    "font": "system",
    "mono": "sf-mono",
    "background": "тёмный фон с сеткой точек",
    "panel": "полупрозрачная подложка",
    "motion": "мягкие выходы",
    "characters": [{"id": "gopher", "name": "Гофер", "look": "голубой круг с глазами"}],
}


def motion_team(task: str, prompt: str) -> dict:
    """Роли «Моушна»: моменты — каждая третья фраза от 2-й секунды; раскадровка — три бита на слова момента;
    техред соглашается; аниматор — карточка с заголовком. FAKE_LLM_MOTION_BADCODE=1 — первая версия кода с
    таймером (проверка просит починить), FAKE_LLM_MOTION_DROP=1 — техред снимает сцены."""
    if task == "motion_moments":
        body = prompt.split("Расшифровка:", 1)[-1]
        found = re.findall(r"^\[(\d+)\]\s+(\d+(?:\.\d+)?)\s+(.+)$", body, re.M)
        lines = [(int(i), float(t), text) for i, t, text in found]
        moments = []
        for n, (first, at, text) in enumerate(lines):
            if at < 2 or n % 3:
                continue
            last = lines[n + 1][0] - 1 if n + 1 < len(lines) else first + len(text.split()) - 1
            moments.append({"from": first, "to": max(first, last), "title": _words(text, 3), "kind": "mechanism",
                            "concept": f"демо: {_words(text, 8)}", "why": "демо", "score": 8})  # fmt: skip
        return {"topic": "демо-тема", "moments": moments}
    if task == "motion_design":
        return DEMO_DESIGN
    if task == "motion_storyboard":
        a, b = (int(x) for x in re.search(r"со слова \[(\d+)\] до \[(\d+)\]", prompt).groups())
        layout = ("side", "lower", "full")[a % 3]
        title = "Правка" if "Замечание автора" in prompt else f"Сцена {a}"
        beats = [{"word": w, "action": f"демо-действие на слове {w}", "text": f"шаг {k}"}
                 for k, w in enumerate(sorted({a, (a + b) // 2, b}), 1)]  # fmt: skip
        if len(beats) < 2:
            beats.append({"word": b + 1, "action": "демо-итог", "text": "итог"})
        return {"title": title, "layout": layout, "idea": "демо-нить", "from": a, "to": b, "hold": 1,
                "characters": ["gopher"], "camera": "", "enter": "fade", "exit": "fade", "beats": beats}  # fmt: skip
    if task == "motion_review":
        if os.environ.get("FAKE_LLM_MOTION_DROP"):
            return {"verdict": "drop", "issues": ["демо: не о том"], "reason": "демо: техред снял сцену"}
        return {"verdict": "ok", "issues": []}
    if task == "motion_animate":
        title = re.search(r"Сцена «(.+?)»", prompt)[1]
        js = 'tl.from("#scene .card", {opacity: 0, y: 30, duration: 0.4}, 0);'
        if os.environ.get("FAKE_LLM_MOTION_BADCODE") and "Прошлая версия кода" not in prompt:
            js += "setTimeout(() => 0, 10);"
        return {"html": f'<div class="card">{title}</div>', "css": "#scene .card{font-size:48px}", "js": js}
    return {}


def main() -> int:
    prompt = sys.stdin.read()
    if os.environ.get("FAKE_LLM_FAIL"):
        print("Error: fake failure requested by FAKE_LLM_FAIL", file=sys.stderr)
        return 1
    print(answer(prompt))
    return 0


if __name__ == "__main__":
    sys.exit(main())
